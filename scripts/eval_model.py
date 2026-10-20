"""Run one vision model live over the fixtures and stress set, and score it.

Evaluates a single model (default: Nebius nvidia/Nemotron-3.5-Super-VL) against
the hand-authored ground truth (12 fixture documents) and the stress-set truth
manifests (4 documents), TWICE per document to see run-to-run variation. Reports
verbatim and compare_key accuracy, invented/absent behaviour, the
structured-output mode the probe chose, reasoning-token usage, p50/p95 latency,
and cost per document. Zero cache: every extraction is a live call.

    VISION_PROVIDER=nebius NEBIUS_VISION_MODEL=nvidia/Nemotron-3.5-Super-VL \
        python scripts/eval_model.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spend  # noqa: E402
from app.client import ExtractionClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.fields import FIELD_ORDER  # noqa: E402
from app.ingest import ingest  # noqa: E402
from app.models import ExtractionResponse, ExtractedField  # noqa: E402
from app.normalize import normalize_field  # noqa: E402

STRESS = ROOT / "runs" / "stress"
RUNS = 2  # per-document repeats for run-to-run variation


def _load_evaluate():
    spec = importlib.util.spec_from_file_location("evaluate", ROOT / "scripts" / "evaluate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["evaluate"] = module
    spec.loader.exec_module(module)
    return module


def _truth_for_fixtures(evaluate) -> dict[str, ExtractionResponse]:
    index = json.loads((ROOT / "fixtures" / "index.json").read_text(encoding="utf-8"))
    out: dict[str, ExtractionResponse] = {}
    for _shipment, entry in index["shipments"].items():
        for doc in entry["documents"]:
            authored = evaluate.authored_for(doc.get("page_sha256", []))
            if authored is None:
                path = ROOT / "fixtures" / "responses" / f"{doc['cache_key']}.json"
                if path.is_file():
                    authored = json.loads(path.read_text(encoding="utf-8")).get("response")
            for f in doc["files"]:
                out[f] = ExtractionResponse.model_validate(authored)
    return out


def _tally(live: ExtractionResponse, want: ExtractionResponse) -> dict:
    t = {"exact": 0, "verbatim": 0, "comparable": 0, "missed": 0, "invented": 0,
         "absent_ok": 0, "absent_total": 0}
    for field in FIELD_ORDER:
        w, g = want.fields[field], live.fields[field]
        if w.present and g.present:
            t["comparable"] += 1
            if normalize_field(field, w).compare_key == normalize_field(field, g).compare_key:
                t["exact"] += 1
            if (w.value or "") == (g.value or ""):
                t["verbatim"] += 1
        elif w.present and not g.present:
            t["missed"] += 1
        elif g.present and not w.present:
            t["invented"] += 1
        else:
            t["absent_ok"] += 1  # neither present: correct absence
            t["absent_total"] += 1
    return t


def _add(a: dict, b: dict) -> dict:
    return {k: a[k] + b[k] for k in a}


async def _extract_once(client: ExtractionClient, filename: str, data: bytes,
                        declared: str | None) -> dict:
    pages = ingest(filename, data)
    started = time.monotonic()
    out = await client.extract_document(
        document_id=filename, pages=pages, declared_doc_type=declared,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    usage = {}
    for a in out.attempts:
        for k in ("prompt", "completion", "total"):
            usage[k] = usage.get(k, 0) + (a.usage.get(k, 0) if a.usage else 0)
    return {"response": out.response, "status": out.status, "elapsed_ms": elapsed_ms,
            "usage": usage, "error": out.error, "model": out.model}


async def main() -> int:
    if not Path(".env").is_file():
        print("BLOCKED: no .env")
        return 2
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    model = (env.get("NEBIUS_VISION_MODEL") or env.get("NEBIUS_MODEL")
             or "nvidia/Nemotron-3.5-Super-VL")
    settings = Settings(
        _env_file=None,
        demo_mode=False,
        vision_provider="nebius",
        nebius_api_key=env.get("NEBIUS_API_KEY", ""),
        nebius_base_url=env.get("NEBIUS_BASE_URL",
                                "https://api.tokenfactory.nebius.com/v1/"),
        nebius_vision_model=model,
        nvidia_api_key="",
        enable_summary=False, enable_adjudication=False, enable_briefing=False,
        consensus=False, enable_line_items=False,
        runs_dir=ROOT / "runs" / "eval_model",
        cache_dir=ROOT / "runs" / "eval_model_cache",
    )
    if not settings.has_nebius_key:
        print("BLOCKED: NEBIUS_API_KEY not set")
        return 2

    client = ExtractionClient(settings)
    mode = await client.probe(force=True)
    print(f"model: {model}")
    print(f"structured-output mode: {mode}\n")

    evaluate = _load_evaluate()
    truth = _truth_for_fixtures(evaluate)

    # (filename, declared_doc_type, bytes, authored-or-truth-json, label)
    jobs: list[tuple[str, str | None, bytes, ExtractionResponse | dict, str]] = []
    index = json.loads((ROOT / "fixtures" / "index.json").read_text(encoding="utf-8"))
    for _shipment, entry in index["shipments"].items():
        for doc in entry["documents"]:
            for f in doc["files"]:
                jobs.append((f, doc["doc_type"],
                             (ROOT / "fixtures" / "images" / f).read_bytes(),
                             truth[f], f"fixture {f}"))
    for tp in sorted(STRESS.glob("*.truth.json")):
        name = tp.stem.split(".", 1)[0]
        img = next(STRESS.glob(f"{name}.*"), None)
        if img is None or img.suffix not in (".png", ".pdf"):
            continue
        payload = json.loads(tp.read_text(encoding="utf-8"))
        jobs.append((img.name, payload["doc_type"], img.read_bytes(), payload, f"stress {name}"))

    aggregate: dict[str, dict] = {}
    latencies: list[int] = []
    reasoning_tokens = 0
    for run in range(RUNS):
        for filename, declared, data, want, label in jobs:
            r = await _extract_once(client, filename, data, declared)
            latencies.append(r["elapsed_ms"])
            tok = r["usage"].get("total", 0)
            reasoning_tokens += r["usage"].get("completion", 0)
            # cost from the usage fields as returned (image tokens are inside
            # prompt_tokens; no separate image breakdown is assumed).
            cost = spend.cost_of("nebius", model,
                                 r["usage"].get("prompt", 0),
                                 r["usage"].get("completion", 0), None)
            spend.record("nebius", model, r["usage"], note=f"{label} run{run + 1}")
            agg = aggregate.setdefault(label, {
                "runs": [], "tally": {"exact": 0, "verbatim": 0, "comparable": 0,
                                      "missed": 0, "invented": 0,
                                      "absent_ok": 0, "absent_total": 0},
                "tokens": 0, "cost": 0.0})
            agg["tokens"] += tok
            agg["cost"] += cost
            if r["response"] is None:
                agg["runs"].append(f"failed: {r['error'][:80]}")
                continue
            if isinstance(want, ExtractionResponse):
                agg["tally"] = _add(agg["tally"], _tally(r["response"], want))
                agg["runs"].append("ok")
            else:
                # stress truth: a dict of field -> raw value or None
                agg["runs"].append("ok")
                for field in FIELD_ORDER:
                    w = want["fields"].get(field)
                    ext = r["response"].fields.get(field)
                    present = bool(ext and ext.present)
                    if w is None:
                        agg["tally"]["absent_total"] += 1
                        if not present:
                            agg["tally"]["absent_ok"] += 1
                        else:
                            agg["tally"]["invented"] += 1
                    elif present:
                        agg["tally"]["comparable"] += 1
                        exp = normalize_field(field, ExtractedField(
                            present=True, value=w, evidence_text=w,
                            page_index=1, confidence=1.0)).compare_key
                        if normalize_field(field, ext).compare_key == exp:
                            agg["tally"]["exact"] += 1
                        if (w or "") == (ext.value or ""):
                            agg["tally"]["verbatim"] += 1
                    else:
                        agg["tally"]["missed"] += 1

    print(f"\n{'document':32} {'runs':14} {'exact/cmp':10} {'verbatim':9} "
          f"{'inv':4} {'miss':5} {'tokens':8} {'cost':8}")
    total = {"exact": 0, "verbatim": 0, "comparable": 0, "missed": 0,
             "invented": 0, "absent_ok": 0, "absent_total": 0}
    grand_tokens = 0
    grand_cost = 0.0
    for label, agg in sorted(aggregate.items()):
        t = agg["tally"]
        for k in total:
            total[k] += t[k]
        grand_tokens += agg["tokens"]
        grand_cost += agg["cost"]
        print(f"{label:32} {','.join(agg['runs']):14} "
              f"{t['exact']}/{t['comparable']:<8} {t['verbatim']:>4}{'':4} "
              f"{t['invented']:>3} {t['missed']:>4} {agg['tokens']:>7} "
              f"${agg['cost']:.4f}")

    lat = sorted(latencies)
    p50 = lat[len(lat) // 2] if lat else 0
    p95 = lat[int(len(lat) * 0.95) - 1] if len(lat) >= 20 else (lat[-1] if lat else 0)
    print(f"\ntotal: {total['exact']}/{total['comparable']} exact, "
          f"{total['verbatim']} verbatim, {total['missed']} missed, "
          f"{total['invented']} invented, {total['absent_ok']}/{total['absent_total']} "
          f"absent-correct")
    print(f"latency p50 {p50 / 1000:.1f}s, p95 {p95 / 1000:.1f}s, n={len(lat)}")
    print(f"reasoning (completion) tokens: {reasoning_tokens}, total tokens: {grand_tokens}")
    print(f"cost: ${grand_cost:.4f}  mode: {mode}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
