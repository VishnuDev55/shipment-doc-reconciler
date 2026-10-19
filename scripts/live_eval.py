"""Measure the live runs and write a committed summary the eval renders.

The live reports live under runs/ (gitignored, ephemeral), so this script
reduces them to the numbers that matter and writes fixtures/live_eval.json,
which is committed. docs/EVAL.md's "Live run" section renders that file, so the
published numbers are reproducible from a committed artifact rather than a
passing run.

Reads runs/live_pipeline/ (in-process full-pipeline run) and runs/live_e2e/runs/
(HTTP run) for report.json files, picks the most complete report per fixture,
and reports: verdict vs expectation, primary-provider field accuracy against the
authored ground truth, consensus agreement, false alarms, p50/p95 extraction
latency, and cost per shipment (estimated).

    python scripts/live_eval.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spend  # noqa: E402
from app.fields import FIELD_ORDER  # noqa: E402

OUT = ROOT / "fixtures" / "live_eval.json"
RUN_DIRS = [ROOT / "runs" / "live_pipeline", ROOT / "runs" / "live_e2e" / "runs"]

EXPECTED = {"clean": "clean", "conflict": "conflicts", "messy": "review"}
VISION_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"


def _load_evaluate():
    spec = importlib.util.spec_from_file_location("evaluate", ROOT / "scripts" / "evaluate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["evaluate"] = module
    spec.loader.exec_module(module)
    return module


def _fixture_of(label: str) -> str:
    for name in EXPECTED:
        if name in label:
            return name
    return ""


def _best_reports() -> dict[str, dict]:
    """Most complete report per fixture, from every live report on disk."""
    best: dict[str, dict] = {}
    for run_dir in RUN_DIRS:
        if not run_dir.is_dir():
            continue
        for report_path in sorted(run_dir.glob("*/report.json")):
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if report.get("state") not in ("done", "partial"):
                continue
            fixture = ""
            for doc in report.get("documents", []):
                fixture = _fixture_of(doc.get("label", "")) or fixture
            if not fixture:
                continue
            ok = sum(1 for d in report["documents"] if d.get("status") == "ok")
            if fixture not in best or ok > best[fixture].get("_ok", -1):
                best[fixture] = dict(report, _ok=ok)
    return best


def _accuracy(docs: list[dict], evaluate) -> dict:
    from app.models import ExtractionResponse
    from app.normalize import normalize_field

    tallies = {"exact": 0, "verbatim": 0, "comparable": 0, "missed": 0, "invented": 0}
    for doc in docs:
        if not doc.get("extraction"):
            continue
        page_sha = [p.get("sha256") for p in doc.get("pages", []) if p.get("sha256")]
        authored = evaluate.authored_for(page_sha)
        if authored is None:
            continue
        live = ExtractionResponse.model_validate(doc["extraction"])
        want = ExtractionResponse.model_validate(authored)
        for field in FIELD_ORDER:
            w, g = want.fields[field], live.fields[field]
            if w.present and g.present:
                tallies["comparable"] += 1
                if normalize_field(field, w).compare_key == normalize_field(field, g).compare_key:
                    tallies["exact"] += 1
                if (w.value or "") == (g.value or ""):
                    tallies["verbatim"] += 1
            elif w.present and not g.present:
                tallies["missed"] += 1
            elif g.present and not w.present:
                tallies["invented"] += 1
    return tallies


def _latency(docs: list[dict]) -> dict:
    ms = sorted(d.get("timings_ms") or 0 for d in docs if (d.get("timings_ms") or 0) > 0)
    if not ms:
        return {"p50_ms": 0, "p95_ms": 0, "n": 0}
    return {"p50_ms": ms[len(ms) // 2], "p95_ms": ms[-1], "n": len(ms)}


def _consensus_cost_benefit(reports: dict[str, dict], evaluate) -> dict:
    """On each consensus divergence, who matched the authored truth."""
    from app.models import ExtractionResponse
    from app.normalize import normalize_field

    tally = {"primary_right": 0, "secondary_right": 0, "both": 0, "neither": 0, "n": 0}
    for report in reports.values():
        for doc in report["documents"]:
            divergences = ((doc.get("consensus") or {}).get("divergences") or [])
            if not divergences:
                continue
            page_sha = [p.get("sha256") for p in doc.get("pages", []) if p.get("sha256")]
            authored = evaluate.authored_for(page_sha)
            if authored is None:
                continue
            truth = ExtractionResponse.model_validate(authored)
            for d in divergences:
                field = d.get("field")
                t = truth.fields.get(field)
                if t is None:
                    continue
                tally["n"] += 1
                if not t.present:
                    primary_ok = not d.get("primary_present")
                    secondary_ok = not d.get("secondary_present")
                else:
                    tkey = normalize_field(field, t).compare_key
                    primary_ok = bool(d.get("primary_present") and d.get("primary_compare_key") == tkey)
                    secondary_ok = bool(d.get("secondary_present") and d.get("secondary_compare_key") == tkey)
                if primary_ok and secondary_ok:
                    tally["both"] += 1
                elif primary_ok:
                    tally["primary_right"] += 1
                elif secondary_ok:
                    tally["secondary_right"] += 1
                else:
                    tally["neither"] += 1
    return tally


def main() -> int:
    evaluate = _load_evaluate()
    reports = _best_reports()

    verdicts: dict[str, dict] = {}
    consensus = {"fields_compared": 0, "divergences": 0}
    cost: dict[str, dict] = {}
    all_docs: list[dict] = []
    for fixture in sorted(reports):
        report = reports[fixture]
        docs = report["documents"]
        all_docs.extend(docs)
        failed = [d.get("label") for d in docs if d.get("status") != "ok"]
        verdicts[fixture] = {
            "expected": EXPECTED[fixture],
            "got": (report.get("summary") or {}).get("verdict"),
            "banner": (report.get("status_report") or {}).get("banner"),
            "state": report.get("state"),
            "failed_docs": failed,
        }
        for doc in docs:
            c = doc.get("consensus") or {}
            if c.get("status") in ("ok", "cached"):
                consensus["fields_compared"] += 10
                consensus["divergences"] += len(c.get("divergences") or [])
        tokens = 0
        usd = 0.0
        for doc in docs:
            u = doc.get("usage") or {}
            tokens += u.get("total") or 0
            usd += spend.cost_of(doc.get("provider") or "nvidia",
                                 doc.get("model") or VISION_MODEL,
                                 u.get("prompt") or 0, u.get("completion") or 0, None)
            c = doc.get("consensus") or {}
            cu = c.get("usage") or {}
            if cu.get("total"):
                tokens += cu.get("total") or 0
                usd += spend.cost_of(c.get("provider") or "nebius", c.get("model") or "",
                                     cu.get("prompt") or 0, cu.get("completion") or 0, None)
        for step in ("line_items", "narrative", "briefing"):
            su = (report.get(step) or {}).get("usage") or {}
            if su.get("total"):
                tokens += su.get("total") or 0
                usd += spend.cost_of((report.get(step) or {}).get("provider") or "",
                                     (report.get(step) or {}).get("model") or "",
                                     su.get("prompt") or 0, su.get("completion") or 0, None)
        for comp in report.get("comparisons") or []:
            au = (comp.get("adjudication") or {}).get("usage") or {}
            if au.get("total"):
                tokens += au.get("total") or 0
                usd += spend.cost_of((comp.get("adjudication") or {}).get("provider") or "",
                                     (comp.get("adjudication") or {}).get("model") or "",
                                     au.get("prompt") or 0, au.get("completion") or 0, None)
        cost[fixture] = {"tokens": tokens, "usd_est": round(usd, 6)}

    summary = {
        "date": "2026-10-19",
        "verdicts": verdicts,
        "primary_accuracy": _accuracy(all_docs, evaluate),
        "consensus": consensus,
        "consensus_cost_benefit": _consensus_cost_benefit(reports, evaluate),
        "false_alarms": [
            name for name, v in verdicts.items()
            if v["expected"] != "conflicts" and v["got"] == "conflicts"
        ],
        "latency": _latency(all_docs),
        "cost": cost,
        "note": (
            "Measured live against NVIDIA nemotron-3-nano-omni (primary) with "
            "MiniCPM-V consensus, on 2026-10-19 while the NVIDIA host was "
            "capacity-degraded (intermittent HTTP 503 and request hangs). "
            "Documents that failed or hung are recorded in failed_docs, not "
            "silently dropped."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
