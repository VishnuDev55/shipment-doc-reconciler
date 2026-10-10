"""Goods-line matching: opt-in, exact, and incapable of moving a verdict.

Two separate risks this suite pins down.

First, the matcher. It answers "which line differs", so the danger is a near-
match that hides a real error: H-40 and H-41 are two parts, and anything that
called them similar enough would let the shipment look clean. Every assertion
below is about a pair either matching exactly or not at all.

Second, the wiring. The feature costs an extra vision call per document, so it
sits behind ENABLE_LINE_ITEMS, off. The default-off path has to produce the
report we already verified, unchanged.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.cache import ResponseCache, cache_key
from app.client import ExtractionClient
from app.config import LINEITEMS_PROMPT_VERSION, PROMPT_VERSION, Settings
from app.ingest import ingest
from app.lineitems import LineItem, fold_description, match_line_items
from app.models import LineItemsResponse, line_items_json_schema
from app.orchestrator import Reconciler
from app.parse import parse_line_items
from app.prompt import line_items_messages

from tests.test_client import install

REPO = Path(__file__).resolve().parents[1]


def item(desc: str = "", sku: str = "", qty: str = "") -> LineItem:
    return LineItem(raw_description=desc, raw_sku=sku, raw_quantity=qty)


def status_of(report, label: str) -> str:
    return next(m.status for m in report.matches if m.label == label)


def key_for(page_sha256s: list[str], version: str) -> str:
    return cache_key(page_sha256s, "nvidia/Qwen-25-VL-7B-Instruct", "nvidia",
                     prompt_version=version)


def fixture_pages(name: str = "invoice_clean.png") -> list:
    return ingest(name, (REPO / "fixtures" / "images" / name).read_bytes())


# ---------------------------------------------------------------------------
# What makes two rows the same row
# ---------------------------------------------------------------------------


def test_a_shared_sku_matches_even_when_the_descriptions_do_not():
    """The document states the identity, so the identity wins."""
    report = match_line_items({
        "invoice.png": [item("CNC aluminium housing, anodised", "H-40", "60 CTNS")],
        "packing.png": [item("Housing", "h 40", "60 CTNS")],
    })
    assert status_of(report, "H-40") == "matched"
    assert report.conflicts == 0


def test_no_sku_means_the_description_has_to_carry_it():
    """Case, punctuation, spacing and word order are presentation; they fold."""
    report = match_line_items({
        "invoice.png": [item("CNC aluminium housing (anodised)", "", "60 CTNS")],
        "packing.png": [item("cnc  ALUMINIUM, HOUSING  ANODISED", "", "60 CTNS")],
    })
    assert report.conflicts == 0
    assert len(report.matches) == 1


def test_a_spelling_variant_of_the_material_is_not_guessed_into_one_row():
    """ALUMINIUM and ALUMINUM are the same metal, but nothing in the matcher can
    know that without a similarity band -- and the moment one exists for letters,
    H-40 and H-41 get one too. Two rows shown apart is a review the clerk can
    do; one row silently merged is a finding deleted."""
    report = match_line_items({
        "invoice.png": [item("CNC aluminium housing", "", "60 CTNS")],
        "packing.png": [item("CNC aluminum housing", "", "60 CTNS")],
    })
    assert len(report.matches) == 2
    assert {m.status for m in report.matches} == {"only_here"}


def test_a_number_one_off_is_never_a_match():
    """The whole reason matching is exact rather than fuzzy.

    A token-similarity band would score these near 1.0, call it a near match,
    and the one error worth finding would disappear.
    """
    report = match_line_items({
        "invoice.png": [item("CNC aluminium housing H-40", "", "60 CTNS")],
        "packing.png": [item("CNC aluminium housing H-41", "", "60 CTNS")],
    })
    assert len(report.matches) == 2
    assert {m.status for m in report.matches} == {"only_here"}
    assert report.matched == 0


def test_fold_description_strips_labels_and_units_but_never_numbers():
    assert fold_description("100 CTN CNC HOUSING") == fold_description("CNC HOUSING (100 CARTONS)")
    assert fold_description("Housing H-40") != fold_description("Housing H-41")
    assert fold_description("Écran LED") == "ECRAN LED"
    assert "40" in fold_description("ITEM NO. 40")


def test_sku_is_compared_with_punctuation_and_case_gone():
    assert item("x", "H-40").key == item("y", "h 40").key == "sku:H40"


def test_a_blank_row_is_not_an_item():
    report = match_line_items({
        "a": [item("WIDGET", "W-1", "5 PCS"), item("", "", "")],
        "b": [item("WIDGET", "W-1", "5 PCS")],
    })
    assert len(report.matches) == 1


# ---------------------------------------------------------------------------
# What a disagreement is allowed to say
# ---------------------------------------------------------------------------


def test_different_counts_on_the_same_line_are_a_conflict():
    report = match_line_items({
        "invoice.png": [item("Widget", "W-1", "60 CTNS")],
        "packing.png": [item("Widget", "W-1", "50 CTNS")],
    })
    assert status_of(report, "W-1") == "quantity_conflict"
    assert "invoice.png=60 CTNS" in report.matches[0].detail
    assert "packing.png=50 CTNS" in report.matches[0].detail


def test_the_same_count_on_a_different_package_basis_is_not_called_a_conflict():
    """60 PCS against 6 CTNS is a basis difference the clerk has to see, but it
    is not the paper disagreeing about how much was shipped."""
    same = match_line_items({
        "invoice.png": [item("Widget", "W-1", "60 PCS")],
        "packing.png": [item("Widget", "W-1", "60 PCS")],
    })
    assert status_of(same, "W-1") == "matched"

    other = match_line_items({
        "invoice.png": [item("Widget", "W-1", "60 PCS")],
        "packing.png": [item("Widget", "W-1", "60 CTNS")],
    })
    assert status_of(other, "W-1") == "unit_differs"
    assert other.conflicts == 1


def test_a_thousand_separator_and_a_space_are_the_same_number():
    report = match_line_items({
        "invoice.png": [item("Widget", "W-1", "1,200 PCS")],
        "packing.png": [item("Widget", "W-1", "1 200 pcs")],
    })
    assert status_of(report, "W-1") == "matched"


def test_an_unreadable_quantity_is_reported_not_guessed():
    report = match_line_items({
        "invoice.png": [item("Widget", "W-1", "60 PCS")],
        "packing.png": [item("Widget", "W-1", "roughly sixty-ish")],
    })
    match = report.matches[0]
    assert match.status == "matched"
    assert "not readable on packing.png" in match.detail


def test_a_line_one_document_omits_is_named_as_uncheckable():
    report = match_line_items({
        "invoice.png": [item("Widget", "W-1", "60 PCS"), item("Gadget", "G-2", "9 PCS")],
        "packing.png": [item("Widget", "W-1", "60 PCS")],
    })
    assert status_of(report, "G-2") == "only_here"
    assert report.uncheckable == 1
    assert "packing.png" in report.matches[0].detail


def test_a_line_repeated_within_one_document_is_a_finding_not_an_overwrite():
    report = match_line_items({
        "invoice.png": [item("Widget", "W-1", "60 PCS"), item("Widget", "W-1", "70 PCS")],
        "packing.png": [item("Widget", "W-1", "60 PCS")],
    })
    assert status_of(report, "W-1") == "quantity_conflict"
    assert "twice" in report.matches[0].detail


def test_a_document_that_returned_no_lines_is_not_credited_with_an_omission():
    """"The other document lists no such item" is a claim about that document's
    contents. If it never yielded goods lines -- a bill of lading, a certificate
    -- nobody read any, and saying so would be inventing a finding."""
    report = match_line_items({
        "invoice.png": [item("Widget", "W-1", "60 PCS")],
        "bol.png": [],
    })
    detail = report.matches[0].detail
    assert "not printed on" not in detail
    assert "list no such item" not in detail
    assert "bol.png returned no goods lines" in detail


def test_an_omission_is_named_apart_from_a_document_that_was_not_read():
    report = match_line_items({
        "invoice.png": [item("Widget", "W-1", "60 PCS"), item("Gadget", "G-2", "9 PCS")],
        "packing.png": [item("Widget", "W-1", "60 PCS")],
        "bol.png": [],
    })
    detail = report.matches[0].detail
    assert "not printed on packing.png" in detail
    assert "bol.png returned no goods lines" in detail


def test_one_document_alone_is_declared_unmatchable():
    """A single goods table cannot be reconciled with anything, and printing it
    as though it were verified is the lie this section exists to avoid."""
    report = match_line_items({"invoice.png": [item("Widget", "W-1", "60 PCS")]})
    assert report.usable is False
    assert "only invoice.png" in report.skipped_reason
    assert all(m.status == "only_here" for m in report.matches)


def test_nothing_at_all_says_so():
    assert match_line_items({}).skipped_reason == "no document returned any line items"


def test_the_same_input_always_produces_the_same_report():
    sets = {
        "b.png": [item("Gadget", "G-2", "9 PCS"), item("Widget", "W-1", "60 PCS")],
        "a.png": [item("Widget", "W-1", "50 PCS"), item("Gadget", "G-2", "9 PCS")],
    }
    first = match_line_items(sets)
    second = match_line_items({k: list(v) for k, v in sets.items()})
    assert [(m.key, m.status, m.detail) for m in first.matches] == [
        (m.key, m.status, m.detail) for m in second.matches
    ]


def test_conflicts_are_listed_before_clean_lines():
    report = match_line_items({
        "a": [item("Alpha", "A-1", "1 PCS"), item("Beta", "B-1", "5 PCS")],
        "b": [item("Alpha", "A-1", "2 PCS"), item("Beta", "B-1", "5 PCS")],
    })
    assert [m.status for m in report.matches] == ["quantity_conflict", "matched"]


# ---------------------------------------------------------------------------
# The second contract stays out of the first one's cache
# ---------------------------------------------------------------------------


def test_the_two_contracts_never_answer_for_each_other(tmp_path):
    """The hand-authored fallback tier matches on page hashes alone, because it
    has to survive a provider switch. Without a contract filter that same
    property would let a goods-lines fixture answer a ten-field lookup -- and
    the shape would parse, with every field absent, so nothing would raise."""
    hashes = ["aa" * 32, "bb" * 32]
    cache = ResponseCache(tmp_path / "responses")
    cache.put(
        key_for(hashes, LINEITEMS_PROMPT_VERSION),
        {"items": [{"description": "Widget", "sku": "W-1", "quantity": "60 PCS"}]},
        model="nvidia/Qwen-25-VL-7B-Instruct", provider="nvidia",
        page_sha256s=hashes, raw_text="{}",
        provenance="HAND-AUTHORED fixture",
        prompt_version=LINEITEMS_PROMPT_VERSION,
    )

    scalar = cache.get(key_for(hashes, PROMPT_VERSION), page_sha256s=hashes)
    assert scalar is None
    assert cache.provenance_of(key_for(hashes, PROMPT_VERSION), page_sha256s=hashes) == "none"

    served = cache.get(key_for(hashes, LINEITEMS_PROMPT_VERSION),
                       page_sha256s=hashes, prompt_version=LINEITEMS_PROMPT_VERSION)
    assert served is not None and "items" in served


def test_the_scalar_lookup_still_finds_its_own_authored_fixture(tmp_path):
    """The filter must not break what it was added to protect: DEMO_MODE."""
    hashes = ["dd" * 32]
    cache = ResponseCache(tmp_path / "r")
    cache.put(
        key_for(hashes, PROMPT_VERSION),
        {"fields": {}},
        model="nvidia/Qwen-25-VL-7B-Instruct", provider="nvidia",
        page_sha256s=hashes, raw_text="{}",
        provenance="HAND-AUTHORED fixture",
        prompt_version=PROMPT_VERSION,
    )
    # Looked up with no prompt_version argument at all, the way the scalar path
    # calls it: the default has to be the scalar contract.
    assert cache.get(key_for(hashes, PROMPT_VERSION), page_sha256s=hashes) == {"fields": {}}


# ---------------------------------------------------------------------------
# Parsing the shape the model actually returns
# ---------------------------------------------------------------------------


def test_one_unreadable_row_does_not_sink_the_document():
    parsed = parse_line_items(json.dumps({"items": [
        {"description": "Widget", "sku": "W-1", "quantity": "60 PCS"},
        "not an object",
        {"description": "   ", "sku": "", "quantity": ""},
        {"description": "Gadget", "sku": None, "quantity": 9},
    ]}))
    assert parsed.ok and parsed.dropped_items == 2
    assert [i.description for i in parsed.response.items] == ["Widget", "Gadget"]
    assert parsed.response.items[1].sku == ""
    assert parsed.response.items[1].quantity == "9"


def test_a_missing_items_key_reads_as_a_document_with_no_goods_table():
    """Not an error, and not retried: a certificate of origin legitimately may
    have no row table, and "no rows" must not become "invented rows"."""
    parsed = parse_line_items('{"foo": 1}')
    assert parsed.ok and parsed.response.items == [] and not parsed.needs_retry


def test_items_that_is_not_an_array_needs_another_call():
    assert parse_line_items('{"items": {"0": {}}}').needs_retry


def test_a_fenced_response_is_repaired_locally():
    parsed = parse_line_items('```json\n{"items": [{"description": "Widget"}]}\n```')
    assert parsed.ok and parsed.response.items[0].description == "Widget"


def test_an_overlong_answer_is_cut_and_said_so():
    rows = [{"description": f"W{i}", "sku": f"W-{i}", "quantity": "1 PCS"} for i in range(80)]
    parsed = parse_line_items(json.dumps({"items": rows}))
    assert len(parsed.response.items) == 60
    assert any("truncated" in i for i in parsed.issues)


def test_the_schema_is_strict_mode_legal():
    """Every property required, additionalProperties false, or the endpoint
    rejects the whole param and the call degrades to plain."""
    schema = line_items_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["items"]
    row = schema["properties"]["items"]["items"]
    assert row["additionalProperties"] is False
    assert sorted(row["required"]) == sorted(
        ["description", "sku", "quantity", "unit_price", "line_total",
         "gross_weight", "net_weight", "evidence_text", "page_index"]), (
        "the price and weight columns are what make an arithmetic check inside a "
        "single document possible, so they are part of the contract")
    LineItemsResponse.model_validate({"items": [{"description": "x"}]})


def test_page_markers_survive_into_the_message():
    msgs = line_items_messages(["u1", "u2"])
    texts = [p["text"] for p in msgs[1]["content"] if p["type"] == "text"]
    assert "--- PAGE 1 ---" in texts and "--- PAGE 2 ---" in texts
    assert "no goods table at all" in texts[0]
    # The example must stay un-copyable, the way the scalar example was fixed.
    assert "<as printed>" in texts[0]


# ---------------------------------------------------------------------------
# The client call, with a fake transport
# ---------------------------------------------------------------------------


def client_settings(tmp_path, **over) -> Settings:
    base = dict(
        _env_file=None,
        demo_mode=False,
        vision_provider="nvidia",
        nvidia_api_key="test-key-not-real",
        nvidia_vision_model="nvidia/Qwen-25-VL-7B-Instruct",
        runs_dir=tmp_path / "runs",
        cache_dir=tmp_path / "cache",
    )
    base.update(over)
    return Settings(**base)


def stub_probe(client: ExtractionClient, mode: str = "strict") -> None:
    """Pin the capability probe so a call count means what the line-item path
    spent and nothing else. The probe's own behaviour is test_client.py's job."""
    client.json_mode = mode
    client._probed = True


@pytest.mark.asyncio
async def test_a_live_line_item_read_is_cached_under_its_own_contract(tmp_path):
    client = ExtractionClient(client_settings(tmp_path))
    body = json.dumps({"items": [{"description": "Widget", "sku": "W-1", "quantity": "60 PCS"}]})
    fake = install(client, [body])
    stub_probe(client)
    pages = fixture_pages()

    out = await client.extract_line_items(document_id="doc1", pages=pages)

    assert out.status == "ok" and out.source == "live_api" and len(out.items) == 1
    sent = fake.chat.completions.calls[0]["messages"]
    assert "GOODS LINES" in sent[1]["content"][0]["text"]

    hashes = [p.sha256 for p in pages]
    stored = json.loads(client.cache.path(key_for(hashes, LINEITEMS_PROMPT_VERSION)).read_text())
    assert stored["prompt_version"] == LINEITEMS_PROMPT_VERSION
    assert stored["_provenance"] == "live API response"

    # Second call replays from cache and makes no request at all.
    calls_before = len(fake.chat.completions.calls)
    again = await client.extract_line_items(document_id="doc1", pages=pages)
    assert again.source == "cached_live" and again.used_cache
    assert len(fake.chat.completions.calls) == calls_before


@pytest.mark.asyncio
async def test_a_line_item_read_does_not_reuse_the_scalar_cache_entry(tmp_path):
    """Same pages, same model: the only thing separating the two lookups is the
    contract tag. If it were dropped, the goods lines would silently be served
    the ten-field answer."""
    client = ExtractionClient(client_settings(tmp_path))
    pages = fixture_pages()
    hashes = [p.sha256 for p in pages]
    client.cache.put(
        key_for(hashes, PROMPT_VERSION),
        {"fields": {}, "doc_type": "commercial_invoice", "doc_type_reason": ""},
        model=client.endpoint.model, provider=client.endpoint.provider,
        page_sha256s=hashes, raw_text="{}",
    )
    fake = install(client, [json.dumps({"items": [{"description": "Widget", "sku": "W-1"}]})])
    stub_probe(client)

    out = await client.extract_line_items(document_id="doc1", pages=pages)

    assert out.used_cache is False and out.status == "ok"
    assert len(fake.chat.completions.calls) == 1, "the scalar entry was not mistaken for it"


@pytest.mark.asyncio
async def test_a_bad_line_item_body_gets_one_correction_then_recovers(tmp_path):
    client = ExtractionClient(client_settings(tmp_path))
    good = json.dumps({"items": [{"description": "Widget", "sku": "W-1", "quantity": "5 PCS"}]})
    fake = install(client, ["total nonsense", good])
    stub_probe(client)

    out = await client.extract_line_items(document_id="doc1", pages=fixture_pages())

    assert out.status == "ok" and len(fake.chat.completions.calls) == 2
    retry = fake.chat.completions.calls[1]["messages"]
    assert retry[2]["role"] == "assistant" and retry[3]["content"].startswith("That response")


@pytest.mark.asyncio
async def test_a_line_item_read_that_never_recovers_is_an_outcome_not_an_exception(tmp_path):
    client = ExtractionClient(client_settings(tmp_path))
    install(client, ["garbage", "still garbage"])
    stub_probe(client)

    out = await client.extract_line_items(document_id="doc1", pages=fixture_pages())

    assert out.status == "failed" and out.items == []
    assert "gave up" in out.error


@pytest.mark.asyncio
async def test_demo_mode_without_a_captured_read_skips_and_says_why(tmp_path):
    client = ExtractionClient(client_settings(tmp_path, demo_mode=True, nvidia_api_key=""))

    out = await client.extract_line_items(document_id="doc1", pages=fixture_pages())

    assert out.status == "skipped" and out.source == "none" and out.items == []
    assert "no captured line-item response exists" in out.error


# ---------------------------------------------------------------------------
# Pipeline: the flag cannot move a verdict
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


def verdicts(report) -> list[tuple[str, str]]:
    return sorted((c.field, c.status) for c in report.comparisons)


@pytest.mark.asyncio
async def test_the_flag_is_off_and_the_report_is_the_one_already_verified(tmp_path):
    rec = Reconciler(settings_for(tmp_path))
    sid, _ = await rec.create_shipment(uploads_for("shipment_clean"))
    await rec.wait(sid, timeout=90)
    report = rec.get(sid)

    assert report.state == "done"
    assert report.line_items.enabled is False
    assert report.line_items.status == "disabled"
    assert report.line_items.matches == []
    assert report.line_items.note == ""


@pytest.mark.asyncio
async def test_turning_it_on_changes_no_verdict_no_risk_score_and_no_document(tmp_path):
    off = Reconciler(settings_for(tmp_path))
    sid_off, _ = await off.create_shipment(uploads_for("shipment_clean"))
    await off.wait(sid_off, timeout=90)
    base = off.get(sid_off)
    snapshot = (
        verdicts(base),
        base.summary.model_dump(),
        base.risk.model_dump(),
        [f.model_dump() for f in base.sanity],
        [d.model_dump() for d in base.documents],
    )

    on = Reconciler(settings_for(tmp_path, enable_line_items=True))
    sid_on, _ = await on.create_shipment(uploads_for("shipment_clean"))
    await on.wait(sid_on, timeout=120)
    changed = on.get(sid_on)

    assert verdicts(changed) == snapshot[0]
    assert changed.summary.model_dump() == snapshot[1]
    assert changed.risk.model_dump() == snapshot[2]
    assert [f.model_dump() for f in changed.sanity] == snapshot[3]
    assert [d.model_dump() for d in changed.documents] == snapshot[4]
    assert changed.state == base.state


@pytest.mark.asyncio
async def test_a_keyless_demo_that_cannot_read_lines_says_so_instead_of_showing_nothing(tmp_path):
    """The honest-empty case: enabled, nothing captured. An empty table with no
    explanation would read as "the documents agree on every line"."""
    rec = Reconciler(settings_for(tmp_path, enable_line_items=True))
    sid, _ = await rec.create_shipment(uploads_for("shipment_clean"))
    await rec.wait(sid, timeout=120)
    section = rec.get(sid).line_items

    assert section.enabled is True
    assert section.status == "skipped"
    assert section.source == "none"
    assert section.matches == []
    assert "no captured line-item response" in section.note


@pytest.mark.asyncio
async def test_a_captured_read_reaches_the_report_as_matches(tmp_path):
    """Seeded by hand, because no fixture image has goods lines captured yet and
    inventing them as model output would be exactly the fake result the project
    rules forbid. What this pins is the plumbing, not any model's accuracy."""
    seeded = tmp_path / "responses"
    shutil.copytree(REPO / "fixtures" / "responses", seeded)
    cache = ResponseCache(seeded)
    pages = fixture_pages("invoice_clean.png")
    hashes = [p.sha256 for p in pages]
    cache.put(
        key_for(hashes, LINEITEMS_PROMPT_VERSION),
        {"items": [
            {"description": "CNC housing", "sku": "H-40", "quantity": "60 CTNS"},
            {"description": "Bracket", "sku": "B-7", "quantity": "10 CTNS"},
        ]},
        model="nvidia/Qwen-25-VL-7B-Instruct", provider="nvidia",
        page_sha256s=hashes, raw_text="{}",
        provenance="HAND-AUTHORED fixture",
        prompt_version=LINEITEMS_PROMPT_VERSION,
    )

    rec = Reconciler(settings_for(tmp_path, enable_line_items=True, cache_dir=seeded))
    sid, _ = await rec.create_shipment(uploads_for("shipment_clean"))
    await rec.wait(sid, timeout=120)
    section = rec.get(sid).line_items

    assert section.status == "skipped", section.note
    assert section.source == "cached_authored"
    assert section.item_counts
    # One document with rows is not a cross-document check, and the report says
    # so instead of listing two items as if they had been reconciled.
    assert all(m.status == "only_here" for m in section.matches)
    assert section.uncheckable == len(section.matches)
    assert section.matches, "the seeded rows should still be visible"
