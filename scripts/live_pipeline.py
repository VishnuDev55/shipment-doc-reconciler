"""Full live pipeline run: the three demo shipments, everything on, nothing cached.

The same Reconciler the HTTP API wraps, driven in-process so the measurement is
robust (the subprocess-HTTP driver crashed on this machine). DEMO_MODE=false,
consensus on, goods lines on, advisor and briefing on, scratch cache — every
extraction, consensus read, line-items read and summary is a live call. Sidecar
text is supplied exactly as the demo fixture endpoint does, so evidence checks
run too.

    python scripts/live_pipeline.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spend  # noqa: E402
from app.config import Settings  # noqa: E402
from app.orchestrator import Reconciler  # noqa: E402
from app.textlayer import load_sidecar  # noqa: E402

EXPECTED = {
    "shipment_clean": "clean",
    "shipment_conflict": "conflicts",
    "shipment_messy": "review",
}


def record_report_spend(report) -> int:
    added = 0

    def rec(provider, model, usage, note):
        nonlocal added
        if usage is None:
            return
        # In-process the usage fields are pydantic TokenUsage, not a dict.
        if hasattr(usage, "prompt"):
            usage = {"prompt": usage.prompt, "completion": usage.completion}
        if not (usage.get("prompt") or usage.get("completion") or usage.get("total")):
            return
        spend.record(provider or "", model or "", usage, note=note)
        added += 1

    for doc in report.documents:
        rec(doc.provider, doc.model, doc.usage, f"extract {doc.label}")
        c = doc.consensus
        if c and c.usage and c.usage.total:
            rec(c.provider, c.model, c.usage, f"consensus {doc.label}")
    n = report.narrative
    rec(n.provider, n.model, n.usage, "summary")
    b = report.briefing
    rec(b.provider, b.model, b.usage, "briefing")
    for comp in report.comparisons:
        a = comp.adjudication
        if a and a.usage and a.usage.total:
            rec(a.provider, a.model, a.usage, f"advisor {comp.field}")
    li = report.line_items
    if li and li.usage and li.usage.total:
        rec(li.provider, li.model, li.usage, "line items")
    return added


def summarize(name: str, report) -> None:
    run = report.run
    s = report.summary
    st = report.status_report
    n = report.narrative
    b = report.briefing
    li = report.line_items
    print(f"\n===== {name} =====")
    print(f"verdict: {s.verdict}  banner: {st.banner}  risk: {st.risk_score}/100  "
          f"state: {report.state}  live_calls={run.live_calls} "
          f"result_source={run.result_source}")
    for doc in report.documents:
        u = doc.usage
        c = doc.consensus
        extra = ""
        if c is not None and c.status in ("ok", "cached"):
            extra = (f"  consensus={c.provider}/{c.model} src={c.source} "
                     f"divergences={len(c.divergences)}")
        print(f"  {doc.label:22} {doc.status:8} {doc.provider}/{doc.model} "
              f"src={doc.source} tok={u.total} {doc.timings_ms / 1000:6.1f}s{extra}")
    print(f"  summary: {n.status} src={n.source} tok={n.usage.total} "
          f"latency={n.latency_ms / 1000:.1f}s")
    print(f"  briefing: {b.status} src={b.source}")
    print(f"  line_items: {li.status} src={li.source} tok={li.usage.total} "
          f"latency={li.latency_ms / 1000:.1f}s")
    print(f"  advisor notes: "
          f"{sum(1 for u in report.unverified if u.advisor_note)} on withdrawn values")


async def main() -> int:
    if not Path(".env").is_file():
        print("BLOCKED: no .env")
        return 2
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    settings = Settings(
        _env_file=None,
        demo_mode=False,
        vision_provider=env.get("VISION_PROVIDER", "nvidia"),
        nvidia_api_key=env.get("NVIDIA_API_KEY", ""),
        nebius_api_key=env.get("NEBIUS_API_KEY", ""),
        nvidia_base_url=env.get("NVIDIA_BASE_URL",
                                 "https://integrate.api.nvidia.com/v1/"),
        nebius_base_url=env.get("NEBIUS_BASE_URL",
                                "https://api.tokenfactory.nebius.com/v1/"),
        consensus=True,
        enable_line_items=True,
        enable_adjudication=True,
        enable_briefing=True,
        enable_summary=True,
        runs_dir=ROOT / "runs" / "live_pipeline",
        cache_dir=ROOT / "runs" / "live_pipeline_cache",
    )
    if not settings.has_nvidia_key or not settings.has_nebius_key:
        print("BLOCKED: both API keys are required")
        return 2

    rec = Reconciler(settings)
    print(f"vision: {rec.client.endpoint.provider} / {rec.client.endpoint.model}")
    print(f"summary: {rec.summary_client.endpoint.provider} / "
          f"{rec.summary_client.endpoint.model}")

    manifest = json.loads((ROOT / "fixtures" / "index.json").read_text())
    names = sys.argv[1:] or list(EXPECTED)
    results = {}
    for name in names:
        if name not in EXPECTED:
            print(f"unknown shipment {name!r}; choose from {sorted(EXPECTED)}")
            return 2
        expected = EXPECTED[name]
        docs = manifest["shipments"][name]["documents"]
        uploads, texts = [], {}
        for d in docs:
            for f in d["files"]:
                uploads.append((f, d["doc_type"],
                                (ROOT / "fixtures" / "images" / f).read_bytes()))
                side = load_sidecar(ROOT / "fixtures" / "text" / f"{Path(f).stem}.json")
                if side:
                    texts[f] = side
        sid, _ = await rec.create_shipment(uploads, page_texts=texts)
        await rec.wait(sid, timeout=2400)
        report = rec.get(sid)
        assert report is not None
        summarize(name, report)
        record_report_spend(report)
        verdict = report.summary.verdict
        ok = verdict == expected
        results[name] = (verdict, ok)
        print(f"  expected {expected!r}, got {verdict!r} -> "
              f"{'PASS' if ok else 'FAIL'}")

    print("\n===== verdict check =====")
    for name, (verdict, ok) in results.items():
        print(f"  {name:20} {verdict:10} {'PASS' if ok else 'FAIL'}")
    return 0 if all(ok for _, ok in results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
