"""Live Super-VL verification pass: the demo shipments and the planted bundle.

One pass of the real pipeline with `nvidia/Nemotron-3.5-Super-VL` as the primary
vision model on Nebius. Nothing is replayed: the cache directory is fresh per
pass, so every document is genuinely read.

    python scripts/live_super_verify.py --consensus off --out runs/live_super/off1.json
    python scripts/live_super_verify.py --consensus on  --out runs/live_super/on1.json

The demo shipments are scored field by field against the hand-authored ground
truth (the same comparison `scripts/evaluate.py` makes). The planted bundle is
scored with `scripts/evaluate_planted.py`'s own rule, so a live transcription is
judged on exactly the same terms as the offline one: did the engine still catch
the error that was planted. Consensus, when on, is MiniCPM-V on the same host --
a different model, not a different vendor.

Every call's tokens go to the spend log. The per-client capability probe is the
one thing not attributable to a document, and is not counted here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import evaluate  # noqa: E402
import evaluate_planted as planted  # noqa: E402
import spend  # noqa: E402
from app.client import ExtractionClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.models import Report  # noqa: E402
from app.orchestrator import Reconciler  # noqa: E402

PRIMARY = "nvidia/Nemotron-3.5-Super-VL"
SECOND_READER = "openbmb/MiniCPM-V-4_5"


def _usage_of(doc) -> dict:
    u = doc.usage
    return {"prompt": u.prompt, "completion": u.completion}


def _charge(tag: str, provider: str, model: str, usage: dict, cached: bool) -> None:
    if cached or not usage:
        return
    if not (usage.get("prompt") or usage.get("completion")):
        return
    spend.record(provider, model, usage, note=f"super-vl {tag}")


def build_settings(cache_dir: Path, runs_dir: Path, *, consensus: bool,
                   line_items: bool, env: dict) -> Settings:
    return Settings(
        _env_file=None,
        demo_mode=False,
        model_discovery=False,
        vision_provider="nebius",
        nebius_api_key=env.get("NEBIUS_API_KEY", ""),
        # Blank on purpose: nothing in this pass may reach NVIDIA, so the
        # measurement is Super-VL's own work and not a fallback's.
        nvidia_api_key="",
        nebius_vision_model=PRIMARY,
        nebius_summary_model=env.get("NEBIUS_SUMMARY_MODEL", ""),
        enable_summary=False,
        enable_adjudication=False,
        enable_briefing=False,
        consensus=consensus,
        consensus_provider="nebius" if consensus else "",
        enable_line_items=line_items,
        runs_dir=runs_dir,
        cache_dir=cache_dir,
    )


def make_reconciler(settings: Settings, *, consensus: bool) -> Reconciler:
    rec = Reconciler(settings)
    if consensus:
        # Consensus reuses the host's client cache, and that client would carry
        # the primary's model id. Pin the second reader to MiniCPM-V explicitly
        # so "second reader" means a different model, not the same one twice.
        flipped = settings.model_copy(update={"nebius_vision_model": SECOND_READER})
        rec._consensus_clients["nebius"] = ExtractionClient(flipped)
    return rec


def _doc_timings(report: Report) -> list[int]:
    return [d.timings_ms for d in report.documents if d.timings_ms]


async def run_demo(rec: Reconciler, index: dict, out: list[dict]) -> None:
    for name, entry in index["shipments"].items():
        uploads = [(f, d["doc_type"], (ROOT / "fixtures" / "images" / f).read_bytes())
                   for d in entry["documents"] for f in d["files"]]
        sid, _ = await rec.create_shipment(uploads)
        await rec.wait(sid, timeout=600)
        report = rec.get(sid)
        assert report is not None
        exact = verbatim = comparable = invented = missed = 0
        per_doc = []
        for doc in report.documents:
            truth = evaluate.authored_for([p.sha256 for p in doc.pages])
            if truth and doc.extraction:
                from app.models import ExtractionResponse
                tallies = evaluate.compare(ExtractionResponse.model_validate(truth),
                                           doc.extraction)
                for t in tallies.values():
                    exact += t.exact
                    verbatim += t.verbatim
                    comparable += t.comparable
                    invented += t.invented
                    missed += t.missed
            _charge("demo", doc.provider, doc.model, _usage_of(doc), doc.used_cache)
            c = doc.consensus
            if c is not None and c.status in ("ok", "generated", "cached"):
                _charge("consensus", c.provider, c.model,
                        {"prompt": c.usage.prompt, "completion": c.usage.completion},
                        c.status == "cached")
            per_doc.append({"label": doc.label, "status": doc.status,
                            "provider": doc.provider, "model": doc.model,
                            "cached": doc.used_cache, "ms": doc.timings_ms,
                            "consensus": (c.status if c else None),
                            "divergences": (len(c.divergences) if c else 0)})
        divs = sum(len(d.consensus.divergences) for d in report.documents
                   if d.consensus and d.consensus.divergences)
        out.append({
            "kind": "demo", "name": name, "state": report.state,
            "verdict": report.summary.verdict,
            "banner": report.status_report.banner,
            "expected": evaluate.expected_verdict(name),
            "exact": exact, "comparable": comparable, "verbatim": verbatim,
            "invented": invented, "missed": missed,
            "timings_ms": _doc_timings(report),
            "tokens": sum(d.usage.total for d in report.documents if d.usage),
            "divergences": divs, "documents": per_doc,
        })


async def run_planted(rec: Reconciler, out: list[dict]) -> None:
    for case in planted.CASES:
        uploads, texts = planted._uploads(case)
        sid, _ = await rec.create_shipment(uploads, page_texts=texts)
        await rec.wait(sid, timeout=600)
        report = rec.get(sid)
        assert report is not None
        caught, why = planted.score(case, report)
        for doc in report.documents:
            _charge("planted", doc.provider, doc.model, _usage_of(doc), doc.used_cache)
        out.append({
            "kind": "planted", "name": case["name"], "category": case.get("category"),
            "state": report.state, "verdict": report.summary.verdict,
            "banner": report.status_report.banner,
            "expected": case["expect"]["outcome"], "caught": caught, "why": why,
            "timings_ms": _doc_timings(report),
            "tokens": sum(d.usage.total for d in report.documents if d.usage),
            "documents": [{"label": d.label, "status": d.status,
                           "provider": d.provider, "cached": d.used_cache,
                           "ms": d.timings_ms} for d in report.documents],
        })


async def main(consensus: bool, out_path: Path) -> int:
    if not (ROOT / ".env").is_file():
        print("BLOCKED: no .env")
        return 2
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    if not (env.get("NEBIUS_API_KEY") or "").strip():
        print("BLOCKED: NEBIUS_API_KEY not set")
        return 2

    tag = "on" if consensus else "off"
    scratch = ROOT / "runs" / "live_super" / f"cache_{tag}_{out_path.stem[-1]}"
    shutil.rmtree(scratch, ignore_errors=True)  # a repeat must be a real repeat
    index = json.loads((ROOT / "fixtures" / "index.json").read_text(encoding="utf-8"))

    demo_rec = make_reconciler(
        build_settings(scratch, ROOT / "runs" / "live_super" / "runs",
                       consensus=consensus, line_items=False, env=env),
        consensus=consensus)
    planted_rec = make_reconciler(
        build_settings(scratch, ROOT / "runs" / "live_super" / "runs",
                       consensus=consensus, line_items=True, env=env),
        consensus=consensus)

    mode = await demo_rec.client.probe(force=True)
    print(f"primary {PRIMARY} json mode: {mode}; consensus={tag}")

    rows: list[dict] = []
    await run_demo(demo_rec, index, rows)
    await run_planted(planted_rec, rows)

    times = sorted(t for r in rows for t in r["timings_ms"])
    demo = [r for r in rows if r["kind"] == "demo"]
    planted_rows = [r for r in rows if r["kind"] == "planted"]
    summary = {
        "consensus": tag,
        "primary_model": PRIMARY,
        "second_reader": SECOND_READER if consensus else None,
        "documents_read": len(times),
        "verdicts_ok": sum(1 for r in demo if r["verdict"] == r["expected"]),
        "verdicts_total": len(demo),
        "exact": sum(r["exact"] for r in demo),
        "comparable": sum(r["comparable"] for r in demo),
        "verbatim": sum(r["verbatim"] for r in demo),
        "invented": sum(r["invented"] for r in demo),
        "missed": sum(r["missed"] for r in demo),
        "planted_caught": sum(1 for r in planted_rows if r["caught"]),
        "planted_total": len(planted_rows),
        "p50_ms": statistics.median(times) if times else 0,
        "p95_ms": times[max(0, int(len(times) * 0.95) - 1)] if times else 0,
        "tokens": sum(r["tokens"] for r in rows),
        "shipments": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"wrote {out_path.relative_to(ROOT)}: verdicts "
          f"{summary['verdicts_ok']}/{summary['verdicts_total']}, "
          f"invented {summary['invented']}, planted {summary['planted_caught']}/"
          f"{summary['planted_total']}, p50 {summary['p50_ms']:.0f} ms")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--consensus", choices=("off", "on"), required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.consensus == "on", Path(args.out))))
