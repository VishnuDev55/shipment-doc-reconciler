"""End-to-end tests through the real pipeline in DEMO_MODE.

These exercise ingest -> cache -> normalize -> compare -> report against the
bundled fixtures. Zero API calls: DEMO_MODE serves the hand-authored response
cache, which is exactly what makes them fast and deterministic.

What these prove: the pipeline is wired correctly and the fixtures produce the
intended verdicts. What they do NOT prove: that the live model returns anything
like the authored responses. See PROGRESS.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.fields import FIELD_ORDER
from app.models import DocumentReport, ExtractionResponse, Report, RunInfo
from app.orchestrator import Reconciler, refresh_report, result_source

REPO = Path(__file__).resolve().parents[1]
INDEX = json.loads((REPO / "fixtures" / "index.json").read_text())


def settings_for(tmp_path: Path, demo: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        demo_mode=demo,
        nebius_api_key="",
        runs_dir=tmp_path / "runs",
        cache_dir=REPO / "fixtures" / "responses",
    )


async def run_shipment(tmp_path: Path, name: str, cache_dir: Path | None = None
) -> tuple[Reconciler, str]:
    entry = INDEX["shipments"][name]
    uploads = []
    for doc in entry["documents"]:
        for fname in doc["files"]:
            raw = (REPO / "fixtures" / "images" / fname).read_bytes()
            uploads.append((fname, doc["doc_type"], raw))

    settings = settings_for(tmp_path)
    if cache_dir is not None:
        settings = settings.model_copy(update={"cache_dir": cache_dir})
    rec = Reconciler(settings=settings)
    shipment_id, _ = await rec.create_shipment(uploads)
    await rec.wait(shipment_id, timeout=60)
    return rec, shipment_id


def authored_only_cache(tmp_path):
    """A cache dir containing ONLY the hand-authored fixtures.

    The shipped cache also holds real captured model responses, which correctly
    take priority at lookup time. Tests that assert "authored" provenance must
    therefore point at an authored-only directory, or they are no longer testing
    the thing they were written to test.
    """
    dst = tmp_path / "authored_only"
    dst.mkdir(parents=True, exist_ok=True)
    for src in (REPO / "fixtures" / "responses").glob("*.json"):
        if "HAND-AUTHORED" in json.loads(src.read_text())["_provenance"].upper():
            (dst / src.name).write_bytes(src.read_bytes())
    return dst


def _live_capture(name: str) -> tuple[Path, dict]:
    """A real captured API response and the provider that produced it, so
    provenance tests use real shapes. Captures live one directory per vision
    provider; the first is taken because these tests care about the shape, not
    about which host drew it -- except where the provider is asserted below."""
    for path in sorted((REPO / "fixtures" / "live_responses").glob(f"*/{name}.json")):
        return path, json.loads(path.read_text())
    raise AssertionError(f"{name}: no live capture found")


def _live_response(name: str) -> dict:
    return _live_capture(name)[1]["response"]


def by_field(report, key: str):
    return next(c for c in report.comparisons if c.field == key)


def _rules(report, document_id: str, field: str) -> list[str]:
    doc = next(d for d in report.documents if d.id == document_id)
    return doc.normalized[field].rules_applied


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shipment_clean_is_all_green(tmp_path):
    rec, sid = await run_shipment(tmp_path, "shipment_clean")
    report = rec.get(sid)
    assert report is not None
    assert report.state == "done"
    assert report.summary.documents_ok == 3
    assert report.summary.conflict == 0

    statuses = {c.field: c.status for c in report.comparisons}
    assert statuses["gross_weight"] == "consistent"
    assert statuses["quantity"] == "consistent"
    assert statuses["container_number"] == "consistent"
    assert statuses["consignee_name"] == "consistent"
    assert statuses["invoice_value"] == "single_source"  # invoice only
    assert statuses["hs_code"] == "single_source"
    assert report.summary.verdict == "clean"


@pytest.mark.asyncio
async def test_multi_page_document_is_one_upload_and_one_extraction(tmp_path):
    """Regression: a 2-page document must be ingested as a single artifact.

    Keying the cache on separately-ingested pages meant the real upload path
    (one file per document) missed the cache and silently dropped a document,
    turning a 3-document comparison into a 2-document one.
    """
    rec, sid = await run_shipment(tmp_path, "shipment_messy")
    report = rec.get(sid)

    assert len(report.documents) == 3
    assert report.summary.documents_ok == 3, "no document may silently vanish"
    assert all(d.used_cache for d in report.documents)

    pl = next(d for d in report.documents if d.filename.endswith(".pdf"))
    assert len(pl.pages) == 2, "the PDF must ingest as two pages"

    # The authored fixture puts the packing-list totals on page 2.
    totals_page = pl.extraction.fields["gross_weight"].page_index
    assert totals_page == 2, "evidence must be able to cite a page beyond the first"
    assert (rec.store.dir(sid) / pl.pages[1].image_path).is_file()


@pytest.mark.asyncio
async def test_clean_run_serves_everything_from_cache(tmp_path):
    rec, sid = await run_shipment(tmp_path, "shipment_clean")
    report = rec.get(sid)
    assert all(d.used_cache for d in report.documents)
    assert all(d.status == "cached" for d in report.documents)
    assert report.run.demo_mode is True


@pytest.mark.asyncio
async def test_shipment_conflict_flags_the_planted_errors(tmp_path):
    rec, sid = await run_shipment(tmp_path, "shipment_conflict")
    report = rec.get(sid)
    assert report.state == "done"
    assert report.summary.conflict >= 2

    weight = by_field(report, "gross_weight")
    assert weight.status == "conflict"
    assert "%" in weight.reason

    container = by_field(report, "container_number")
    assert container.status == "conflict"
    assert "ARBITRATES" in container.reason

    consignee = by_field(report, "consignee_name")
    assert consignee.status == "consistent", "a legal-suffix variation must not be a conflict"
    assert report.summary.verdict == "conflicts"


@pytest.mark.asyncio
async def test_checksum_arbitration_names_the_culprit(tmp_path):
    """The demo's best moment: one digit differs, and the checksum says which
    document is wrong without any ground truth."""
    rec, sid = await run_shipment(tmp_path, "shipment_conflict")
    report = rec.get(sid)
    container = by_field(report, "container_number")

    bad = next(v for v in container.values if v.canonical == "MSKU3067328")
    good = next(v for v in container.values if v.canonical == "MSKU3067322")

    assert "check digit mismatch" in " ".join(bad.warnings)
    assert bad.warnings and not good.warnings
    assert "MSKU3067328" in container.reason
    assert "more likely correct" in container.reason
    assert "MSKU3067322" in container.reason


@pytest.mark.asyncio
async def test_weight_outlier_is_attributed_to_the_minority(tmp_path):
    rec, sid = await run_shipment(tmp_path, "shipment_conflict")
    report = rec.get(sid)
    weight = by_field(report, "gross_weight")

    assert weight.majority_compare_key == "weight:12450.000"
    outliers = [v.document_label for v in weight.values if v.is_outlier]
    assert len(outliers) == 1
    assert outliers[0].startswith("bol_"), f"expected the BOL as outlier, got {outliers}"


@pytest.mark.asyncio
async def test_shipment_messy_proves_the_normalizer_works(tmp_path):
    """The point of this fixture: raw strings differ everywhere, yet the
    normalized values agree. If the normalizer were a no-op this would fail."""
    rec, sid = await run_shipment(tmp_path, "shipment_messy")
    report = rec.get(sid)

    weight = by_field(report, "gross_weight")
    raws = {v.raw_value for v in weight.values}
    assert len(raws) > 1, "fixture should present varied raw formats"

    kgs = [float(v.compare_key.split(":")[1]) for v in weight.values if v.compare_key]
    assert max(kgs) - min(kgs) <= 1.0, (
        f"European comma and LBS must normalise to the same mass within "
        f"tolerance, got {kgs}"
    )
    assert weight.status == "consistent"

    # 27,447.00 LBS is the same mass as 12,450.00 KGS to within 0.25 kg; the
    # raw strings are wildly different, so only the normalizer can see that.
    lbs = next(v for v in weight.values if v.raw_value and "LBS" in v.raw_value)
    assert lbs.canonical == "12449.750 kg"
    assert "unit_lbs_to_kg" in _rules(report, lbs.document_id, "gross_weight")

    qty = by_field(report, "quantity")
    assert qty.status == "suspicious"
    assert "package basis" in qty.reason, "pieces vs cartons must not read as a conflict"

    hs = by_field(report, "hs_code")
    assert hs.status == "single_source"


@pytest.mark.asyncio
async def test_evidence_points_at_a_real_page(tmp_path):
    rec, sid = await run_shipment(tmp_path, "shipment_clean")
    report = rec.get(sid)
    for doc in report.documents:
        assert doc.pages, "every document must carry viewable page images"
        for page_ref in doc.pages:
            assert (rec.store.dir(sid) / page_ref.image_path).is_file()
        if doc.extraction:
            for key, field in doc.extraction.fields.items():
                if field.present:
                    assert 1 <= field.page_index <= len(doc.pages)
                    assert field.evidence_text, f"{doc.id}/{key} present but no evidence quote"


@pytest.mark.asyncio
async def test_report_records_versions_and_model(tmp_path):
    rec, sid = await run_shipment(tmp_path, "shipment_clean")
    report = rec.get(sid)
    assert report.run.prompt_version
    assert report.run.normalizer_version
    assert report.run.comparator_version
    assert report.run.model
    assert report.run.json_schema_mode == "cache"


@pytest.mark.asyncio
async def test_report_is_written_to_disk_and_reloadable(tmp_path):
    rec, sid = await run_shipment(tmp_path, "shipment_conflict")
    rec.get(sid)
    path = rec.store.dir(sid) / "report.json"
    assert path.is_file()
    reloaded = rec.store.read(sid)
    assert reloaded is not None
    assert len(reloaded.comparisons) == len(FIELD_ORDER)
    assert reloaded.summary.conflict == rec.get(sid).summary.conflict


@pytest.mark.asyncio
async def test_rejects_too_many_documents(tmp_path):
    rec = Reconciler(settings=settings_for(tmp_path))
    fname = INDEX["shipments"]["shipment_clean"]["documents"][0]["files"][0]
    raw = (REPO / "fixtures" / "images" / fname).read_bytes()
    with pytest.raises(ValueError, match="at most"):
        await rec.create_shipment([(fname, None, raw)] * 6)


@pytest.mark.asyncio
async def test_uningestable_file_fails_only_itself(tmp_path):
    """A 2-of-3 result is useful. One bad upload must not abort the shipment."""
    entry = INDEX["shipments"]["shipment_clean"]
    uploads = []
    for doc in entry["documents"]:
        for fname in doc["files"]:
            uploads.append(
                (fname, doc["doc_type"], (REPO / "fixtures" / "images" / fname).read_bytes())
            )
    uploads.append(("garbage.gif", None, b"not an image"))

    rec = Reconciler(settings=settings_for(tmp_path))
    sid, _ = await rec.create_shipment(uploads)
    await rec.wait(sid, timeout=60)
    report = rec.get(sid)

    # 3 good + 1 rejected: the shipment is usable but incomplete, so "partial"
    # is the honest state. The comparison must still be clean.
    assert report.state == "partial"
    assert report.summary.documents_ok == 3
    assert report.summary.documents_failed == 1
    bad = next(d for d in report.documents if d.status == "failed")
    assert "unsupported file type" in bad.error
    assert report.summary.conflict == 0
    assert by_field(report, "gross_weight").status == "consistent"


@pytest.mark.asyncio
async def test_demo_mode_without_cache_fails_with_actionable_message(tmp_path):
    """A cold cache in DEMO_MODE must say what to do, not hang or fake success."""
    entry = INDEX["shipments"]["shipment_clean"]
    fname = entry["documents"][0]["files"][0]
    raw = (REPO / "fixtures" / "images" / fname).read_bytes()

    rec = Reconciler(settings=settings_for(tmp_path))
    # Point the cache at an empty dir so this fixture is a guaranteed miss.
    rec.settings.cache_dir = tmp_path / "nothing_here"
    from app.client import ExtractionClient

    rec.client = ExtractionClient(rec.settings)
    rec.summary_client = type(rec.summary_client)(rec.settings)

    sid, _ = await rec.create_shipment([(fname, "commercial_invoice", raw)])
    await rec.wait(sid, timeout=60)
    report = rec.get(sid)
    doc = report.documents[0]
    assert doc.status == "failed"
    assert "DEMO_MODE is on" in doc.error
    assert report.state == "failed"


# ---------------------------------------------------------------------------
# Provenance: the report must say where the values actually came from.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authored_cache_is_labelled_authored_not_live(tmp_path):
    rec, sid = await run_shipment(tmp_path, "shipment_clean", cache_dir=authored_only_cache(tmp_path))
    report = rec.get(sid)
    assert report.run.result_source == "authored"
    assert all(d.source == "cached_authored" for d in report.documents)
    assert report.run.live_calls == 0


@pytest.mark.asyncio
async def test_cached_authored_value_is_never_reported_as_live(tmp_path):
    """The trap this guards: DEMO_MODE=false, so the code believes it is live,
    but the values are served from the hand-authored fixture cache. Reporting
    'live' here would be a false claim about the model."""
    entry = INDEX["shipments"]["shipment_clean"]
    uploads = [
        (f, d["doc_type"], (REPO / "fixtures" / "images" / f).read_bytes())
        for d in entry["documents"]
        for f in d["files"]
    ]
    settings = Settings(
        _env_file=None,
        demo_mode=False,                      # "live" mode...
        nebius_api_key="test-key-not-real",
        cache_dir=authored_only_cache(tmp_path),   # ...served from authored cache
        runs_dir=tmp_path / "runs",
    )
    rec = Reconciler(settings)
    sid, _ = await rec.create_shipment(uploads)
    await rec.wait(sid, timeout=60)
    report = rec.get(sid)

    assert report.run.demo_mode is False
    assert report.run.result_source == "authored", (
        "a cached hand-authored value must not be labelled live"
    )


def test_result_source_classifies_provenance_combinations():
    payload = ExtractionResponse.model_validate(_live_response("bol_conflict"))

    def doc(source):
        return DocumentReport(id=source, label=source, filename="x.png",
                              source=source, extraction=payload)

    assert result_source([]) == "none"
    assert result_source([DocumentReport(id="a", label="a", filename="a.png")]) == "none"
    assert result_source([doc("live_api")]) == "live"
    assert result_source([doc("cached_live")]) == "live"
    assert result_source([doc("cached_authored")]) == "authored"
    assert result_source([doc("live_api"), doc("cached_authored")]) == "live+authored"
    # A document that failed contributes nothing, so it must not dilute the label.
    assert result_source([doc("live_api"),
                          DocumentReport(id="f", label="f", filename="f.png",
                                         status="failed")]) == "live"


def _refresh(report):
    """refresh_report needs a client for endpoint identity; stub the two fields
    the export actually reads rather than constructing a real client."""
    from types import SimpleNamespace

    client = SimpleNamespace(
        endpoint=SimpleNamespace(model="some/model", provider="nvidia"),
        json_mode="strict",
        settings=SimpleNamespace(demo_mode=False),
    )
    return refresh_report(report, client)


def test_export_banner_states_the_right_kind_of_provenance():
    """Four distinct cases, because 'the model read it' and 'we called the model
    just now' are different claims and the report must not blur them."""
    from app.export import standalone_html

    payload = ExtractionResponse.model_validate(_live_response("bol_conflict"))

    def rep(*sources, live_calls=0):
        # 'live+authored' is a computed rollup, never a per-document value, so
        # build it from a real mix of documents.
        docs = [
            DocumentReport(id=f"d{i}", label=f"d{i}", filename=f"d{i}.png",
                           source=src, extraction=payload)
            for i, src in enumerate(sources, 1)
        ]
        r = Report(shipment_id="s", created_at="x", updated_at="x", documents=docs,
                   run=RunInfo(demo_mode=False))
        r.run.live_calls = live_calls
        return standalone_html(_refresh(r))

    fresh = rep("live_api", live_calls=1)
    assert "Live — 1 document(s) read by" in fresh
    assert "hand-authored" not in fresh

    replay = rep("cached_live")
    assert "replayed from cache" in replay
    assert "no API call was made for this run" in replay
    # A replay must not use the word "Live" as its headline claim.
    assert ">Live —" not in replay

    authored = rep("cached_authored")
    assert "hand-authored" in authored and "replayed from cache" not in authored

    mixed = rep("live_api", "cached_authored", live_calls=1)
    assert "Mixed provenance" in mixed


def test_live_calls_counted_per_run_not_per_cache_hit():
    """live_calls counts documents read on THIS run; cached_live is model-backed
    but contributes zero, which is what makes the replay banner correct."""
    payload = ExtractionResponse.model_validate(_live_response("bol_conflict"))
    docs = [
        DocumentReport(id="a", label="a", filename="a.png", source="live_api",
                       extraction=payload),
        DocumentReport(id="b", label="b", filename="b.png", source="cached_live",
                       extraction=payload),
    ]
    r = Report(shipment_id="s", created_at="x", updated_at="x", documents=docs)
    _refresh(r)
    assert r.run.live_calls == 1
    assert r.run.result_source == "live"


@pytest.mark.asyncio
async def test_demo_mode_survives_a_change_of_model_id(tmp_path):
    """The cache key includes the model string, so pointing NEBIUS_MODEL at a
    different model silently broke every authored fixture and DEMO_MODE died.

    Authored fixtures are not model output, so they are matched on page hashes
    alone. Live responses must stay model-keyed -- one model's answer is not
    another's -- so this test also asserts that a cached *live* entry is not
    reused across models.
    """
    entry = INDEX["shipments"]["shipment_clean"]
    uploads = [
        (f, d["doc_type"], (REPO / "fixtures" / "images" / f).read_bytes())
        for d in entry["documents"]
        for f in d["files"]
    ]

    async def run(model_id):
        settings = Settings(
            _env_file=None,
            demo_mode=True,
            nebius_api_key="",
            nebius_model=model_id,
            runs_dir=tmp_path / f"runs-{model_id.replace('/', '_')}",
            cache_dir=only,
        )
        rec = Reconciler(settings)
        sid, _ = await rec.create_shipment(uploads)
        await rec.wait(sid, timeout=60)
        return rec.get(sid)

    # Authored-only cache: this test is about the model-id-independence of the
    # authored fallback, which the shipped cache's live entries would mask.
    only = authored_only_cache(tmp_path)
    default = await run("openbmb/MiniCPM-V-4_5")
    other = await run("nvidia/Nemotron-Nano-12B-V2-VL")

    assert default.run.result_source == "authored"
    assert other.run.result_source == "authored", (
        "DEMO_MODE broke for a different model id"
    )
    assert other.summary.documents_ok == 3


def test_live_cache_entries_stay_model_keyed(tmp_path):
    """A captured live response must NOT be served for a different model."""
    from app.cache import ResponseCache, cache_key

    src, payload = _live_capture("bol_conflict")
    provider = src.parent.name
    page_hashes = payload["page_sha256"]

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = ResponseCache(cache_dir)
    cache.put(
        cache_key(page_hashes, "model-A", provider),
        payload["response"],
        model="model-A",
        provider=provider,
        page_sha256s=page_hashes,
        raw_text="{}",
        provenance="live API response",
    )

    assert cache.get(cache_key(page_hashes, "model-A", provider)) is not None
    assert cache.get(cache_key(page_hashes, "model-B", provider)) is None, (
        "a live answer leaked across models"
    )
    # ...and the authored fallback must not pick it up either.
    assert cache.find_authored_by_pages(page_hashes) is None
    # Provider is part of the key for the same reason model is: two hosts can
    # serve the same model id and answer differently.
    assert cache.get(cache_key(page_hashes, "model-A", provider + "-other")) is None
