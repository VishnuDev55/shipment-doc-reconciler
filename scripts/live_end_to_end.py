"""Full live end-to-end run: the three demo shipments through the real HTTP API.

Starts a real uvicorn with DEMO_MODE=false, consensus on, goods lines on, the
advisor and briefing on, and a scratch cache so nothing replays — every
extraction is a live call. Uploads each fixture via POST /api/shipments and
polls the report the way a real client would, prints per-document
latency/tokens/cost, checks the verdicts against the expected ones, and records
the spend.

    python scripts/live_end_to_end.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spend  # noqa: E402

# Verdicts the bundled fixtures are built to produce. These are the expectation;
# a live model may miss one (which is exactly what this run is for measuring).
EXPECTED = {
    "shipment_clean": "clean",
    "shipment_conflict": "conflicts",
    "shipment_messy": "review",
}

TERMINAL = {"done", "partial", "failed"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def record_report_spend(report: dict) -> int:
    """Add every model step's usage from one report to the spend log."""
    added = 0
    run = report.get("run") or {}

    def rec(provider, model, usage, note):
        nonlocal added
        if not usage or not (usage.get("prompt") or usage.get("completion")
                             or usage.get("total")):
            return
        spend.record(provider or "", model or "", usage, note=note)
        added += 1

    for doc in report.get("documents") or []:
        rec(doc.get("provider"), doc.get("model"), doc.get("usage"),
            f"extract {doc.get('label')}")
        consensus = doc.get("consensus") or {}
        if consensus.get("usage"):
            rec(consensus.get("provider"), consensus.get("model"),
                consensus.get("usage"), f"consensus {doc.get('label')}")
    narrative = report.get("narrative") or {}
    rec(narrative.get("provider") or run.get("summary_provider"),
        narrative.get("model") or run.get("summary_model"),
        narrative.get("usage"), "summary")
    briefing = report.get("briefing") or {}
    rec(briefing.get("provider"), briefing.get("model"), briefing.get("usage"),
        "briefing")
    for comp in report.get("comparisons") or []:
        adjudication = comp.get("adjudication") or {}
        if adjudication.get("usage"):
            rec(adjudication.get("provider"), adjudication.get("model"),
                adjudication.get("usage"), f"advisor {comp.get('field')}")
    line_items = report.get("line_items") or {}
    if line_items.get("usage"):
        rec(line_items.get("provider"), line_items.get("model"),
            line_items.get("usage"), "line items")
    return added


def summarize(name: str, report: dict) -> None:
    run = report.get("run") or {}
    summary = report.get("summary") or {}
    status = report.get("status_report") or {}
    print(f"\n===== {name} =====")
    print(f"verdict: {summary.get('verdict')}  banner: {status.get('banner')}  "
          f"risk: {status.get('risk_score')}/100  state: {report.get('state')}")
    print(f"providers: vision={run.get('vision_provider')} "
          f"model={run.get('model')}  live_calls={run.get('live_calls')} "
          f"result_source={run.get('result_source')}  model_selection={run.get('model_selection')}")
    for doc in report.get("documents") or []:
        u = doc.get("usage") or {}
        consensus = doc.get("consensus") or {}
        extra = ""
        if consensus.get("status") in ("ok", "cached"):
            divergences = len(consensus.get("divergences") or [])
            extra = f"  consensus={consensus.get('provider')}/{consensus.get('model')} "
            extra += f"src={consensus.get('source')} divergences={divergences}"
        print(f"  {doc.get('label'):22} {doc.get('status'):8} "
              f"{doc.get('provider'):7}/{doc.get('model'):45} "
              f"src={doc.get('source'):14} tok={u.get('total', 0):5} "
              f"{doc.get('timings_ms', 0) / 1000:6.1f}s{extra}")
    narrative = report.get("narrative") or {}
    briefing = report.get("briefing") or {}
    print(f"  summary: {narrative.get('status')} src={narrative.get('source')} "
          f"tok={(narrative.get('usage') or {}).get('total', 0)}")
    print(f"  briefing: {briefing.get('status')} src={briefing.get('source')}")
    print(f"  advisor notes: "
          f"{sum(1 for u in report.get('unverified') or [] if u.get('advisor_note'))} "
          f"on withdrawn values")


def main() -> int:
    if not Path(".env").is_file():
        print("BLOCKED: no .env")
        return 2
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    scratch = ROOT / "runs" / "live_e2e"
    port = _free_port()
    child_env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "DEMO_MODE": "false",
        "VISION_PROVIDER": env.get("VISION_PROVIDER", "nvidia"),
        "NVIDIA_API_KEY": env.get("NVIDIA_API_KEY", ""),
        "NEBIUS_API_KEY": env.get("NEBIUS_API_KEY", ""),
        "NVIDIA_BASE_URL": env.get("NVIDIA_BASE_URL",
                                   "https://integrate.api.nvidia.com/v1/"),
        "NEBIUS_BASE_URL": env.get("NEBIUS_BASE_URL",
                                   "https://api.tokenfactory.nebius.com/v1/"),
        "CONSENSUS": "true",
        "ENABLE_LINE_ITEMS": "true",
        "ENABLE_ADJUDICATION": "true",
        "ENABLE_BRIEFING": "true",
        "ENABLE_SUMMARY": "true",
        "CACHE_DIR": str(scratch / "cache"),
        "RUNS_DIR": str(scratch / "runs"),
        "API_KEYS": "",
    }
    scratch.mkdir(parents=True, exist_ok=True)
    log = open(scratch / "server.log", "wb")  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=child_env, stdout=log, stderr=log,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 120
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"server exited early (code {proc.returncode}); log tail:")
                print(Path(scratch / "server.log").read_text(errors="replace")[-3000:])
                return 1
            try:
                httpx.get(base + "/api/health", timeout=3)
                break
            except (httpx.HTTPError, OSError):
                time.sleep(0.5)
        else:
            print("server never became healthy")
            return 1

        manifest = json.loads((ROOT / "fixtures" / "index.json").read_text())
        results = {}
        for name, expected in EXPECTED.items():
            started = time.monotonic()
            docs = manifest["shipments"][name]["documents"]
            files = [
                ("files", (f, (ROOT / "fixtures" / "images" / f).read_bytes(),
                           "image/png"))
                for d in docs for f in d["files"]
            ]
            doc_types = [d["doc_type"] for d in docs for _ in d["files"]]
            created = httpx.post(
                base + "/api/shipments", files=files,
                data={"doc_types": json.dumps(doc_types)}, timeout=120,
            ).json()
            sid = created["shipment_id"]
            report = poll_until_terminal(base, sid, budget=2400)
            elapsed = time.monotonic() - started
            summarize(name, report)
            record_report_spend(report)
            verdict = (report.get("summary") or {}).get("verdict")
            ok = verdict == expected
            results[name] = (verdict, ok)
            print(f"  expected {expected!r}, got {verdict!r} -> "
                  f"{'PASS' if ok else 'FAIL'}  (wall clock {elapsed:.0f}s)")

        print("\n===== verdict check =====")
        for name, (verdict, ok) in results.items():
            print(f"  {name:20} {verdict:10} {'PASS' if ok else 'FAIL'}")
        return 0 if all(ok for _, ok in results.values()) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        log.close()


def poll_until_terminal(base: str, sid: str, budget: float) -> dict:
    """Poll the report until the run is fully finished.

    `state` flips to done/partial as soon as the documents are extracted, but the
    run is not over until the trailing steps — goods lines, the advisor, the
    briefing and the summary — have written too. The summary is the last step, so
    a report whose narrative has left `pending` is the finished one; stopping
    earlier (as a first version of this driver did) captures a report whose
    summary is still pending and can kill still-running prose by shutting the
    server down.
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        report = httpx.get(base + f"/api/shipments/{sid}/report", timeout=30).json()
        state = report.get("state")
        narrative = (report.get("narrative") or {}).get("status")
        line_items = (report.get("line_items") or {}).get("status")
        if state in TERMINAL and narrative != "pending" and line_items != "pending":
            return report
        time.sleep(2.0)
    raise SystemExit(f"shipment {sid} did not finish within {budget:.0f}s")


if __name__ == "__main__":
    sys.exit(main())
