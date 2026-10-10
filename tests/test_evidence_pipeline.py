"""An invented field must not be allowed to invent a disagreement.

The acceptance test for evidence verification, run as a contrast: the same
doctored model response goes through the real pipeline twice, once with a text
source for the page and once without. With one, the fabricated value is pulled
out of the comparison and the shipment stays clean. Without one, nothing can be
checked and the false conflict appears -- which is the honest limit of the
feature, asserted here so it cannot be quietly forgotten.

The fabrication is chosen to be the dangerous kind: a PO number that no document
prints, on a bill of lading, while the invoice genuinely states `PO 4410-DE`.
Two documents "disagreeing" about a reference is the single most likely way a
hallucination reaches a customer as a finding.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.config import Settings
from app.orchestrator import Reconciler
from app.textlayer import load_sidecar

REPO = Path(__file__).resolve().parents[1]
INDEX = json.loads((REPO / "fixtures" / "index.json").read_text())
AUTHORED_MODEL = "hand-authored-fixture"
FAKE_PO = "PO 9999-NOPE"


def _uploads_and_texts(name: str):
    uploads, texts = [], {}
    for doc in INDEX["shipments"][name]["documents"]:
        for fname in doc["files"]:
            raw = (REPO / "fixtures" / "images" / fname).read_bytes()
            uploads.append((fname, doc["doc_type"], raw))
            side = load_sidecar(REPO / "fixtures" / "text" / f"{Path(fname).stem}.json")
            if side:
                texts[fname] = side
    return uploads, texts


def _cache_with_invented_bol_po(tmp_path: Path) -> Path:
    """An authored-only cache with the bill of lading claiming a PO number that
    is printed nowhere. Everything else is untouched.

    Authored-only, deliberately: the shipped cache also holds live-captured
    responses, and those win the exact-key lookup. Editing the authored entry
    there would change nothing, and a test that silently asserts against a
    response it never served is worse than no test.
    """
    from app.cache import AUTHORED_MARKER

    target = tmp_path / "responses"
    target.mkdir(parents=True)
    for entry in (REPO / "fixtures" / "responses").glob("*.json"):
        payload = json.loads(entry.read_text(encoding="utf-8"))
        if AUTHORED_MARKER not in str(payload.get("_provenance", "")).upper():
            continue
        shutil.copy2(entry, target / entry.name)

    bol = next(d for d in INDEX["shipments"]["shipment_clean"]["documents"]
               if d["doc_type"] == "bill_of_lading")
    path = target / f"{bol['cache_key']}.json"
    assert path.is_file(), "the clean BOL has no authored cache entry"
    payload = json.loads(path.read_text(encoding="utf-8"))
    fields = payload["response"]["fields"]
    assert not fields["po_number"]["present"], "the clean BOL already states a PO number"
    fields["po_number"] = {
        "present": True, "value": FAKE_PO,
        "evidence_text": f"Purchase order {FAKE_PO}",
        "page_index": 1, "confidence": 0.93,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return target


async def _run(tmp_path: Path, cache_dir: Path, *, with_texts: bool):
    settings = Settings(
        _env_file=None, demo_mode=True, vision_provider="nvidia",
        nvidia_api_key="", nebius_api_key="",
        runs_dir=tmp_path / "runs", cache_dir=cache_dir,
    )
    uploads, texts = _uploads_and_texts("shipment_clean")
    rec = Reconciler(settings=settings)
    sid, _ = await rec.create_shipment(uploads, page_texts=texts if with_texts else None)
    await rec.wait(sid, timeout=120)
    return rec.get(sid)


@pytest.mark.asyncio
async def test_an_invented_value_is_caught_and_creates_no_conflict(tmp_path):
    report = await _run(tmp_path, _cache_with_invented_bol_po(tmp_path), with_texts=True)

    flagged = [u for u in report.unverified if u.field == "po_number"]
    assert len(flagged) == 1, "the fabricated PO number should have been caught"
    assert flagged[0].value == FAKE_PO
    assert flagged[0].document_label.endswith(".png")
    assert "not found" in flagged[0].reason or "does not appear" in flagged[0].reason

    comparison = next(c for c in report.comparisons if c.field == "po_number")
    assert comparison.status != "conflict", (
        f"a hallucinated value reached the report as a disagreement: {comparison.reason}"
    )
    # Only the invoice's real PO number is left standing, so the field is now
    # single-source rather than conflicting -- reported, not hidden.
    assert [v.document_label for v in comparison.values] and comparison.status in {
        "single_source", "consistent"}
    assert report.summary.verdict != "conflicts"
    assert report.state == "done"


@pytest.mark.asyncio
async def test_without_a_text_source_the_same_fabrication_is_not_detectable(tmp_path):
    """The limit, asserted rather than described.

    A degraded photo has no text layer. Nothing in this feature can tell a
    hallucinated PO number from a real one on that input, and the report must
    say 'not checked' rather than imply the evidence held up.
    """
    report = await _run(tmp_path, _cache_with_invented_bol_po(tmp_path), with_texts=False)

    assert report.unverified == []
    comparison = next(c for c in report.comparisons if c.field == "po_number")
    assert comparison.status == "conflict", (
        "expected the unchecked fabrication to produce a false conflict -- that is "
        "the whole reason the text source matters")

    states = {d.id: {k: v.state for k, v in d.verification.items()}
              for d in report.documents}
    bol = next(d for d in report.documents if d.label.endswith("bol_clean.png"))
    assert states[bol.id]["po_number"] == "not_checkable"


@pytest.mark.asyncio
async def test_a_real_extraction_still_verifies_end_to_end(tmp_path):
    """The shipped fixtures, unmodified: nothing flagged, nothing excluded.

    Guards against a verifier that is over-eager -- the same code path that
    catches a fabrication must leave an honest transcription alone.
    """
    report = await _run(tmp_path, REPO / "fixtures" / "responses", with_texts=True)
    assert report.unverified == []
    assert report.summary.verdict == "clean"
    checked = [v for d in report.documents for v in d.verification.values()]
    assert checked, "verification should have run on every document"
    assert all(v.state in {"verified", "absent"} for v in checked), [
        (k, v.state) for d in report.documents for k, v in d.verification.items()
        if v.state not in {"verified", "absent"}]


@pytest.mark.asyncio
async def test_a_correction_outlives_a_failed_check(tmp_path):
    """A reviewer who types a value in is asserting it, not quoting the model.

    Excluding a field because the model's quote was bad must not also throw away
    the human's answer -- that would make the correction UI unable to fix the
    one case it most needs to fix.
    """
    cache = _cache_with_invented_bol_po(tmp_path)
    uploads, texts = _uploads_and_texts("shipment_clean")
    settings = Settings(
        _env_file=None, demo_mode=True, vision_provider="nvidia",
        nvidia_api_key="", nebius_api_key="",
        runs_dir=tmp_path / "runs", cache_dir=cache,
    )
    rec = Reconciler(settings=settings)
    sid, _ = await rec.create_shipment(uploads, page_texts=texts)
    await rec.wait(sid, timeout=120)

    bol = next(d for d in rec.get(sid).documents if d.label.endswith("bol_clean.png"))
    rec.correct_field(sid, bol.id, "po_number", "PO 4410-DE", actor="clerk")

    report = rec.get(sid)
    comparison = next(c for c in report.comparisons if c.field == "po_number")
    # Not merely 'consistent': a typed-in value carries no model confidence, and
    # the low-confidence rule holds a comparison back rather than promoting it.
    # What matters is that the human's value took part at all.
    assert comparison.status in {"consistent", "suspicious"}, comparison.status
    bol_row = next(v for v in comparison.values if v.document_id == bol.id)
    assert bol_row.raw_value == "PO 4410-DE", bol_row.raw_value
    assert sum(1 for v in comparison.values if v.raw_value) == 2, (
        "exactly the invoice and the corrected BOL should be standing")
    assert [u for u in report.unverified if u.field == "po_number"] == []


# ---------------------------------------------------------------------------
# positions: the check has to be visible, not just recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verified_quote_can_be_point_at_on_the_page(tmp_path):
    """Every document in this shipment has a text source, so every verified field
    that quoted one printed line comes back with that line's rectangle. Without
    it the report can say "this is real" but not "here it is"."""
    report = await _run(tmp_path, REPO / "fixtures" / "responses", with_texts=True)
    placed = [(d.label, key) for d in report.documents
              for key, v in d.verification.items() if v.highlightable]
    assert len(placed) >= 20, f"only {len(placed)} values are placeable: {placed}"

    for doc in report.documents:
        for key, v in doc.verification.items():
            if not v.box:
                continue
            x0, y0, x1, y1 = v.box
            assert 0.0 <= x0 < x1 <= 1.0, (doc.label, key, v.box)
            assert 0.0 <= y0 < y1 <= 1.0, (doc.label, key, v.box)
            assert v.box_page == v.matched_page
            assert v.tier.startswith("line"), (doc.label, key, v.tier)


@pytest.mark.asyncio
async def test_a_photo_run_places_nothing_and_says_so(tmp_path):
    """The same shipment with no text source: not one rectangle, and not one
    claim that the evidence was located."""
    report = await _run(tmp_path, REPO / "fixtures" / "responses", with_texts=False)
    boxes = [v.box for d in report.documents for v in d.verification.values() if v.box]
    assert boxes == []


@pytest.mark.asyncio
async def test_the_filed_page_draws_the_boxes_the_app_drew(tmp_path):
    """report.html is the artifact people keep, so it must carry the same
    highlight rather than a weaker version of it -- and must say where the
    coordinates came from."""
    from app.export import standalone_html

    report = await _run(tmp_path, REPO / "fixtures" / "responses", with_texts=True)
    page = standalone_html(report, tmp_path / "runs" / report.shipment_id)
    assert 'class="pgbox"' in page
    assert "positioned from this file" in page

    photo = await _run(tmp_path / "b", REPO / "fixtures" / "responses", with_texts=False)
    plain = standalone_html(photo, tmp_path / "b" / "runs" / photo.shipment_id)
    assert 'class="pgbox"' not in plain
