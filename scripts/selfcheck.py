"""Demo rehearsal. Runs all three bundled shipments through the real pipeline
in DEMO_MODE and prints the verdict table you should see on stage.

    make probe        (or: python scripts/selfcheck.py)

Zero API calls, zero browser. If this prints the expected pattern, the UI will
too, because it renders the same report.json.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.fields import FIELD_ORDER  # noqa: E402
from app.orchestrator import Reconciler  # noqa: E402

ICON = {
    "consistent": "[ OK ]",
    "suspicious": "[WARN]",
    "conflict": "[FAIL]",
    "single_source": "[ 1x ]",
    "absent": "[ -- ]",
}

# What each fixture is supposed to demonstrate. A mismatch is a real regression,
# not a cosmetic difference — it means a comparator or normalizer changed.
#
# hs_code is "consistent" rather than "single_source" because the certificate of
# origin states it alongside the invoice: the field a wrong classification rests
# on is now checkable, which is the point of shipping that document.
EXPECT: dict[str, dict[str, str]] = {
    "shipment_clean": {
        "quantity": "consistent",
        "gross_weight": "consistent",
        "container_number": "consistent",
        "consignee_name": "consistent",
        "invoice_value": "single_source",
        "hs_code": "consistent",
        "net_weight": "consistent",
        "incoterms": "single_source",
        "po_number": "single_source",
        "country_of_origin": "consistent",
    },
    "shipment_conflict": {
        "quantity": "consistent",
        "gross_weight": "conflict",
        "container_number": "conflict",
        "consignee_name": "consistent",
        "invoice_value": "single_source",
        "hs_code": "consistent",
        "net_weight": "consistent",
        "incoterms": "single_source",
        "po_number": "single_source",
        "country_of_origin": "consistent",
    },
    "shipment_messy": {
        "quantity": "suspicious",
        "gross_weight": "consistent",
        "container_number": "consistent",
        "consignee_name": "consistent",
        "invoice_value": "single_source",
        "hs_code": "consistent",
        # Same mass, printed as KGS on the packing list and LBS on the waybill:
        # this row is the demo that unit conversion happens in deterministic code.
        "net_weight": "consistent",
        "incoterms": "single_source",
        "po_number": "single_source",
        # "Made in P.R.C." on the invoice, "CHINA" on the packing list.
        "country_of_origin": "consistent",
    },
}


async def run_one(name: str, entry: dict, settings: Settings) -> list[str]:
    uploads = []
    for doc in entry["documents"]:
        for fname in doc["files"]:
            path = ROOT / "fixtures" / "images" / fname
            uploads.append((fname, doc["doc_type"], path.read_bytes()))

    rec = Reconciler(settings)
    sid, _ = await rec.create_shipment(uploads)
    await rec.wait(sid, timeout=120)
    report = rec.get(sid)
    assert report is not None, f"{name}: no report produced"

    problems: list[str] = []
    print(f"\n{'=' * 74}")
    print(f"{name}   state={report.state}   verdict={report.summary.verdict}")
    print(f"{'=' * 74}")

    if report.summary.documents_ok != len(report.documents):
        problems.append(
            f"only {report.summary.documents_ok}/{len(report.documents)} documents extracted"
        )

    got = {c.field: c for c in report.comparisons}
    # Every field the engine extracts must have a stated expectation here, or a
    # new field could ship with the demo never saying what it should do with it.
    for field in sorted(set(FIELD_ORDER) - set(EXPECT[name])):
        problems.append(f"{field}: no expectation recorded in EXPECT[{name!r}]")
    for field, expected in EXPECT[name].items():
        comp = got.get(field)
        if comp is None:
            problems.append(f"{field}: missing from comparisons")
            continue
        mark = "  " if comp.status == expected else "!!"
        print(f" {mark} {ICON[comp.status]} {field:18} {comp.reason[:112]}")
        if comp.status != expected:
            problems.append(f"{field}: expected {expected}, got {comp.status}")

    # The two moments the demo video rests on.
    if name == "shipment_conflict":
        container = got["container_number"]
        if "ARBITRATES" not in container.reason:
            problems.append("checksum arbitration text missing")
        else:
            print("    -> arbitration: " + container.reason.split("ARBITRATES:")[-1].strip()[:118])
        weight = got["gross_weight"]
        outliers = [v.document_label for v in weight.values if v.is_outlier]
        print(f"    -> weight outlier attributed to: {outliers or 'nobody'}")
        if len(outliers) != 1:
            problems.append(f"expected exactly one weight outlier, got {outliers}")

    if name == "shipment_messy":
        weight = got["gross_weight"]
        raws = sorted({str(v.raw_value) for v in weight.values if v.raw_value})
        print(f"    -> raw forms reconciled: {raws}")
        if len(raws) < 3:
            problems.append("expected >=3 raw weight spellings to normalise together")

    return problems


async def main() -> int:
    index_path = ROOT / "fixtures" / "index.json"
    if not index_path.is_file():
        print("fixtures/index.json missing. Run: python scripts/build_fixtures.py")
        return 2
    shipments = json.loads(index_path.read_text())["shipments"]

    settings = Settings(_env_file=None, demo_mode=True, runs_dir=ROOT / "runs" / "_selfcheck")
    print(f"DEMO_MODE self-check — {len(shipments)} bundled shipments, zero API calls")

    all_problems: dict[str, list[str]] = {}
    for name, entry in shipments.items():
        if name not in EXPECT:
            print(f"  skipping {name}: no expectations defined")
            continue
        all_problems[name] = await run_one(name, entry, settings)

    print(f"\n{'=' * 74}")
    failed = {k: v for k, v in all_problems.items() if v}
    if failed:
        print("SELF-CHECK FAILED")
        for name, probs in failed.items():
            for p in probs:
                print(f"  {name}: {p}")
        return 1
    print("SELF-CHECK PASSED — every fixture produced the intended pattern.")

    # Report what the cache actually contains, since "served from cache" no
    # longer implies "hand-authored": real captured model responses now live
    # alongside the authored fixtures and take priority at lookup time.
    entries = list((ROOT / "fixtures" / "responses").glob("*.json"))
    docs = {tuple(json.loads(e.read_text())["page_sha256"]) for e in entries}
    authored = sum(
        1 for e in entries
        if "HAND-AUTHORED" in json.loads(e.read_text()).get("_provenance", "").upper()
    )
    print(f"Cache: {len(entries)} entries covering {len(docs)} documents — "
          f"{len(entries) - authored} live-captured + {authored} hand-authored.")
    print("Live-captured entries mean the demo replays real model output with no key;")
    print("authored entries are the portable fallback for any other model id.")
    print("To re-verify against the endpoint: make probe-live / make probe-diff.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
