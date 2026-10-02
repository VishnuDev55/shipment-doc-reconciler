"""Second-reader consensus: a review marker, never a tie-break.

The interesting failure this guards against is two providers being asked to agree
and the app treating agreement as truth. Nothing here can move a comparison
status, and these tests assert that by snapshotting the deterministic output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.consensus import divergences_between, summarize
from app.models import DocumentReport, NormalizedField
from app.orchestrator import Reconciler

REPO = Path(__file__).resolve().parents[1]


def doc(**normalized: tuple[str, str]) -> DocumentReport:
    """A document with normalized fields given as (canonical, compare_key)."""
    report = DocumentReport(id="d1", label="Invoice", filename="inv.png")
    report.normalized = {
        name: NormalizedField(canonical=canonical, compare_key=compare_key)
        for name, (canonical, compare_key) in normalized.items()
    }
    return report


# ---------------------------------------------------------------------------
# the comparison is deterministic and uses compare_key, not spelling
# ---------------------------------------------------------------------------


def test_identical_readings_produce_no_divergence():
    a = doc(gross_weight=("12,450.00 KGS", "12450.0|kg"), quantity=("48 EA", "48|ea"))
    b = doc(gross_weight=("12,450.00 KGS", "12450.0|kg"), quantity=("48 EA", "48|ea"))
    assert divergences_between(a, b) == []
    assert summarize([]) == "both readers agreed on every field"


def test_two_spellings_that_normalize_alike_are_not_a_disagreement():
    """Reporting these as divergent would undercut the normalizer everything
    else in the app relies on."""
    a = doc(consignee_name=("NORDWIND IMPORT GmbH", "nordwind import"))
    b = doc(consignee_name=("Nordwind Import GmbH & Co", "nordwind import"))
    assert divergences_between(a, b) == []


def test_a_real_value_disagreement_is_flagged_with_both_sides():
    a = doc(gross_weight=("12,450.00 KGS", "12450.0|kg"))
    b = doc(gross_weight=("12,540.00 KGS", "12540.0|kg"))
    found = divergences_between(a, b)
    assert [d.field for d in found] == ["gross_weight"]
    assert found[0].primary_canonical == "12,450.00 KGS"
    assert found[0].secondary_canonical == "12,540.00 KGS"
    assert found[0].primary_present and found[0].secondary_present


def test_a_field_one_reader_left_blank_is_a_coverage_divergence():
    """Presence is tracked apart from value, or "unreadable" hides inside
    "disagreeing"."""
    a = doc(hs_code=("8517.62", "8517.62"))
    b = doc(hs_code=(None, None))
    found = divergences_between(a, b)
    assert len(found) == 1
    assert found[0].primary_present is True
    assert found[0].secondary_present is False


def test_a_field_the_second_reader_omitted_entirely_counts():
    a = doc(invoice_value=("USD 48,200.00", "48200.0|usd"))
    b = doc()
    found = divergences_between(a, b)
    assert [d.field for d in found] == ["invoice_value"]
    assert found[0].secondary_present is False


def test_summary_names_the_fields():
    a = doc(gross_weight=("1 KGS", "1|kg"), hs_code=("8517", "8517"))
    b = doc(gross_weight=("2 KGS", "2|kg"), hs_code=("8518", "8518"))
    text = summarize(divergences_between(a, b))
    assert text.startswith("2 field(s) disagree")
    assert "gross weight" in text and "hs code" in text


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def settings_for(tmp_path, **over) -> Settings:
    base = dict(
        _env_file=None,
        demo_mode=True,
        vision_provider="nvidia",
        nvidia_api_key="",
        nebius_api_key="",
        runs_dir=tmp_path / "runs",
        cache_dir=REPO / "fixtures" / "responses",
    )
    base.update(over)
    return Settings(**base)


def uploads_for(name: str) -> list[tuple[str, str | None, bytes]]:
    index = json.loads((REPO / "fixtures" / "index.json").read_text())
    out = []
    for doc_entry in index["shipments"][name]["documents"]:
        for fname in doc_entry["files"]:
            out.append((fname, doc_entry["doc_type"],
                        (REPO / "fixtures" / "images" / fname).read_bytes()))
    return out


@pytest.mark.asyncio
async def test_consensus_off_by_default_leaves_documents_untouched(tmp_path):
    rec = Reconciler(settings_for(tmp_path))
    sid, _ = await rec.create_shipment(uploads_for("shipment_clean"))
    await rec.wait(sid, timeout=60)
    for d in rec.get(sid).documents:
        assert d.consensus is None
    assert rec._consensus_clients == {}


@pytest.mark.asyncio
async def test_consensus_on_reads_both_cached_providers(tmp_path):
    """Both sides are real captured model output, so this runs offline honestly."""
    rec = Reconciler(settings_for(tmp_path, consensus=True))
    sid, _ = await rec.create_shipment(uploads_for("shipment_clean"))
    await rec.wait(sid, timeout=90)

    report = rec.get(sid)
    for d in report.documents:
        c = d.consensus
        assert c is not None
        assert c.status == "cached"
        assert c.provider == "nebius", "primary was nvidia, so the second reader is the other host"
        assert c.source == "cached_live"
        assert isinstance(c.divergences, list)

    raw = {p.name for p in (tmp_path / "runs" / sid / "raw").iterdir()}
    assert any(n.endswith("_alt_cache.txt") for n in raw), raw


@pytest.mark.asyncio
async def test_the_second_provider_can_be_named_explicitly(tmp_path):
    rec = Reconciler(settings_for(tmp_path, consensus=True, consensus_provider="nvidia"))
    sid, _ = await rec.create_shipment(uploads_for("shipment_clean"))
    await rec.wait(sid, timeout=90)
    assert rec.get(sid).documents[0].consensus.provider == "nvidia"


@pytest.mark.asyncio
async def test_one_client_per_secondary_provider(tmp_path):
    """A fresh ExtractionClient runs a capability probe; per document that would
    double the spend consensus is already paying for."""
    rec = Reconciler(settings_for(tmp_path, consensus=True))
    sid, _ = await rec.create_shipment(uploads_for("shipment_messy"))
    await rec.wait(sid, timeout=90)
    assert list(rec._consensus_clients) == ["nebius"]


@pytest.mark.asyncio
async def test_consensus_cannot_change_a_verdict(tmp_path):
    rec_off = Reconciler(settings_for(tmp_path))
    sid_off, _ = await rec_off.create_shipment(uploads_for("shipment_conflict"))
    await rec_off.wait(sid_off, timeout=60)

    cache_dir = settings_for(tmp_path).cache_dir
    rec_on = Reconciler(settings_for(tmp_path / "on", consensus=True,
                                       cache_dir=cache_dir))
    sid_on, _ = await rec_on.create_shipment(uploads_for("shipment_conflict"))
    await rec_on.wait(sid_on, timeout=90)

    def snapshot(report):
        return [(c.field, c.status, c.reason,
                 [(v.document_id, v.compare_key) for v in c.values])
                for c in report.comparisons]

    assert snapshot(rec_on.get(sid_on)) == snapshot(rec_off.get(sid_off))
    assert rec_on.get(sid_on).summary.verdict == rec_off.get(sid_off).summary.verdict


@pytest.mark.asyncio
async def test_a_second_reader_that_cannot_answer_is_recorded_not_fatal(tmp_path):
    """Point the secondary at a cache with no entry for it. DEMO_MODE then has
    nothing to replay, and the run must still complete."""
    empty = tmp_path / "empty_cache"
    empty.mkdir()
    rec = Reconciler(settings_for(tmp_path, consensus=True, cache_dir=empty))
    sid, report = await rec.create_shipment(uploads_for("shipment_clean"))
    await rec.wait(sid, timeout=60)

    finished = rec.get(sid)
    assert finished.state in ("done", "partial", "failed")
    for d in finished.documents:
        # The primary also has no cache here, so it fails; the point is that the
        # run reached a terminal state rather than hanging or raising.
        assert d.status in ("failed", "ok", "partial")
        if d.consensus is not None:
            assert d.consensus.status in ("failed", "skipped")


@pytest.mark.asyncio
async def test_a_missing_live_key_skips_the_second_reader(tmp_path):
    rec = Reconciler(settings_for(
        tmp_path, demo_mode=False, consensus=True,
        nebius_api_key="", nvidia_api_key="nvapi-TEST-ONLY-0000-1234",
    ))
    document = DocumentReport(id="d1", label="Invoice", filename="inv.png")
    from app.ingest import ingest

    pages = ingest("invoice_clean.png",
                   (REPO / "fixtures" / "images" / "invoice_clean.png").read_bytes())
    await rec._run_consensus(document, pages, tmp_path / "raw")

    assert document.consensus.status == "skipped"
    assert "API_KEY not set" in document.consensus.error
