"""Two-provider behaviour: routing, cache isolation, reasoning-field handling,
and the rule that the AI summary can never affect a shipment's outcome.

Zero live calls. Everything here runs against fake transports.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cache import ResponseCache, cache_key
from app.client import ExtractionClient, ExtractionOutcome, _message_text
from app.config import Settings
from app.models import DocumentReport, ExtractionResponse, Report, RunInfo
from app.normalize import normalize_field
from app.orchestrator import (
    Reconciler,
    derive_state,
    refresh_report,
)
DOC_TYPES_SET = {"commercial_invoice", "packing_list", "bill_of_lading", "other"}

from app.providers import (
    NEBIUS_FALLBACK_VISION_MODEL,
    NVIDIA_VISION_MODEL,
    summary_endpoint,
    vision_endpoint,
)
from app.fields import FIELD_ORDER
from app.summary import build_summary_prompt, summary_is_plausible

REPO = Path(__file__).resolve().parents[1]


def base(tmp_path, **over) -> Settings:
    d = dict(
        _env_file=None,
        demo_mode=False,
        nebius_api_key="nb-key",
        nvidia_api_key="nv-key",
        runs_dir=tmp_path / "runs",
        cache_dir=tmp_path / "cache",
    )
    d.update(over)
    return Settings(**d)


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def test_vision_defaults_to_nvidia_build(tmp_path):
    ep = vision_endpoint(base(tmp_path))
    assert ep.provider == "nvidia"
    assert ep.model == NVIDIA_VISION_MODEL
    assert "integrate.api.nvidia.com" in ep.base_url
    assert ep.api_key == "nv-key"


def test_vision_falls_back_to_nebius_by_config(tmp_path):
    ep = vision_endpoint(base(tmp_path, vision_provider="nebius"))
    assert ep.provider == "nebius"
    assert ep.model == NEBIUS_FALLBACK_VISION_MODEL
    assert "tokenfactory.nebius.com" in ep.base_url


def test_legacy_nebius_model_alias_still_selects_the_vision_model(tmp_path):
    ep = vision_endpoint(base(tmp_path, vision_provider="nebius", nebius_model="some/VL"))
    assert ep.model == "some/VL"


def test_summary_always_runs_on_nebius_regardless_of_vision_provider(tmp_path):
    for provider in ("nvidia", "nebius"):
        ep = summary_endpoint(base(tmp_path, vision_provider=provider))
        assert ep.provider == "nebius", "the Nebius requirement must not depend on vision routing"
        assert "tokenfactory.nebius.com" in ep.base_url


def test_model_ids_overridable_without_code_changes(tmp_path):
    ep = vision_endpoint(base(tmp_path, nvidia_vision_model="nvidia/some-future-vl"))
    assert ep.model == "nvidia/some-future-vl"


def test_endpoint_str_never_leaks_the_key():
    from app.providers import Endpoint

    ep = Endpoint(provider="nvidia", base_url="https://h/v1/", api_key="secret-abc123",
                  model="m", label="vision")
    assert "secret" not in str(ep)
    assert "abc123" not in str(ep)


# ---------------------------------------------------------------------------
# cache isolation between providers
# ---------------------------------------------------------------------------


def test_cache_key_cannot_collide_across_providers(tmp_path):
    """Two hosts could in principle serve the same model id. A response from
    one must never be replayed as if it came from the other."""
    pages = ["a" * 64]
    assert cache_key(pages, "nvidia/x", "nvidia") != cache_key(pages, "nvidia/x", "nebius")


@pytest.mark.asyncio
async def test_live_response_is_not_served_to_the_other_provider(tmp_path):
    pages = ["b" * 64]
    cache = ResponseCache(tmp_path / "c")
    cache.put(cache_key(pages, "shared/model", "nvidia"), {"fields": {}},
              model="shared/model", provider="nvidia", page_sha256s=pages, raw_text="{}")

    assert cache.get(cache_key(pages, "shared/model", "nvidia")) is not None
    assert cache.get(cache_key(pages, "shared/model", "nebius")) is None


def test_authored_fixture_is_provider_agnostic(tmp_path):
    """Hand-authored fixtures are not model output, so they must resolve for
    either provider -- that is what keeps DEMO_MODE alive when VISION_PROVIDER
    flips."""
    pages = ["c" * 64]
    cache = ResponseCache(tmp_path / "c")
    cache.put(
        cache_key(pages, "any/model", "nvidia"),
        {"fields": {}},
        model="any/model",
        provider="nvidia",
        page_sha256s=pages,
        raw_text="{}",
        provenance="HAND-AUTHORED fixture response",
    )
    for provider in ("nvidia", "nebius", "whoever"):
        assert cache.get(cache_key(pages, "x", provider), page_sha256s=pages) is not None
        assert cache.provenance_of(cache_key(pages, "x", provider), page_sha256s=pages) == "authored"


# ---------------------------------------------------------------------------
# reasoning_content handling -- the quirk that would silently break extraction
# ---------------------------------------------------------------------------


def test_content_is_preferred_when_present():
    msg = SimpleNamespace(content='{"ok": true}', reasoning_content="thinking...")
    text, used = _message_text(msg)
    assert text == '{"ok": true}' and used == "content"


def test_reasoning_content_used_when_content_empty():
    """Nebius Nemotron Nano returns content=None with the answer in `reasoning`.
    Reading .content alone would yield an empty extraction on HTTP 200."""
    msg = SimpleNamespace(content=None, reasoning_content='{"ok": true}')
    text, used = _message_text(msg)
    assert text == '{"ok": true}' and used == "reasoning_content"

    msg2 = SimpleNamespace(content="", reasoning='{"ok": 1}', reasoning_content=None)
    text2, used2 = _message_text(msg2)
    assert used2 == "reasoning" and text2 == '{"ok": 1}'


def test_whitespace_only_content_counts_as_empty():
    msg = SimpleNamespace(content="   \n ", reasoning_content="real answer")
    text, used = _message_text(msg)
    assert used == "reasoning_content" and text == "real answer"


def test_all_fields_empty_returns_empty_for_retry():
    msg = SimpleNamespace(content=None, reasoning_content=None, reasoning=None)
    text, used = _message_text(msg)
    assert text == "" and used == "content"


@pytest.mark.asyncio
async def test_empty_completion_is_retried_then_reported(tmp_path, monkeypatch):
    """A saturated worker can return 200 with no text. That must be retried,
    not accepted as a successful empty extraction."""
    c = ExtractionClient(base(tmp_path, vision_provider="nebius"))
    c.json_mode = "plain"
    c._probed = True

    calls = {"n": 0}

    async def fake_create(**kwargs):
        calls["n"] += 1
        content = None if calls["n"] < 3 else '{"ok": true}'
        msg = SimpleNamespace(content=content, refusal=None, reasoning_content=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    c._client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=fake_create)))

    async def no_sleep(_t):
        return None

    monkeypatch.setattr("app.client.asyncio.sleep", no_sleep)
    text, used, usage = await c.transport_call([{"role": "user", "content": "x"}],
                                              response_format=None)
    assert text == '{"ok": true}'
    assert calls["n"] == 3, "empty completions should have been retried"


# ---------------------------------------------------------------------------
# the summary must never be able to change a reconciliation
# ---------------------------------------------------------------------------


def _report_with_docs():
    docs = [
        DocumentReport(id="d1", label="d1", filename="d1.png", status="ok",
                       source="live_api"),
    ]
    r = Report(shipment_id="s", created_at="x", updated_at="x", documents=docs,
               run=RunInfo(model="m", vision_provider="nvidia"))
    return r


def test_summary_failure_leaves_state_and_verdict_untouched():
    """The whole contract of the optional feature: it can fail and the shipment
    still reports normally."""
    from app.compare import Comparison

    r = _report_with_docs()
    r.comparisons = [Comparison(field="gross_weight", label="GW", kind="weight",
                                status="conflict", reason="differs by 5%")]
    from app.orchestrator import build_summary

    r.summary = build_summary(r.documents, r.comparisons)
    r.state = derive_state(r.documents, r.summary)
    before = (r.state, r.summary.verdict, r.summary.conflict)

    r.narrative.status = "failed"
    r.narrative.error = "boom"

    assert (r.state, r.summary.verdict, r.summary.conflict) == before


@pytest.mark.asyncio
async def test_summary_exception_is_swallowed_and_logged(tmp_path):
    rec = Reconciler(base(tmp_path, enable_summary=True))

    async def explode(prompt):
        raise RuntimeError("connection reset")

    rec.summary_client.summarize = explode
    report = _report_with_docs()
    from app.compare import Comparison
    from app.orchestrator import build_summary

    report.comparisons = [Comparison(field="hs_code", label="HS", kind="hs_code",
                                     status="consistent", reason="matches")]
    report.summary = build_summary(report.documents, report.comparisons)

    await rec._write_summary(report)

    assert report.narrative.status == "failed"
    assert "connection reset" in report.narrative.error
    assert report.narrative.text is None


@pytest.mark.asyncio
async def test_summary_disabled_is_skipped_not_failed(tmp_path):
    rec = Reconciler(base(tmp_path, enable_summary=False))
    report = _report_with_docs()
    await rec._write_summary(report)
    assert report.narrative.status == "skipped"
    assert "ENABLE_SUMMARY" in report.narrative.error


@pytest.mark.asyncio
async def test_demo_mode_without_cached_narrative_skips_cleanly(tmp_path):
    """DEMO_MODE must not attempt a network call for the summary."""
    rec = Reconciler(base(tmp_path, demo_mode=True, nebius_api_key="", nvidia_api_key=""))
    report = _report_with_docs()
    from app.compare import Comparison
    from app.orchestrator import build_summary

    report.comparisons = [Comparison(field="quantity", label="Q", kind="count",
                                     status="consistent", reason="agrees")]
    report.summary = build_summary(report.documents, report.comparisons)
    await rec._write_summary(report)
    assert report.narrative.status == "skipped"
    assert "DEMO_MODE" in report.narrative.error


@pytest.mark.asyncio
async def test_implausible_model_text_is_rejected(tmp_path):
    """A reasoning model that dumps a monologue must not be shown as a summary."""
    rec = Reconciler(base(tmp_path))

    async def rambling(prompt):
        return ("Here's a thinking process:\n" + "step one. " * 20, {})

    rec.summary_client.summarize = rambling
    report = _report_with_docs()
    from app.compare import Comparison
    from app.orchestrator import build_summary

    report.comparisons = [Comparison(field="quantity", label="Q", kind="count",
                                     status="consistent", reason="agrees")]
    report.summary = build_summary(report.documents, report.comparisons)
    await rec._write_summary(report)
    assert report.narrative.status == "failed"
    assert "implausible" in report.narrative.error


# ---------------------------------------------------------------------------
# summary prompt construction
# ---------------------------------------------------------------------------


def test_summary_prompt_carries_verdicts_and_counts():
    from app.compare import Comparison
    from app.orchestrator import build_summary

    r = _report_with_docs()
    r.comparisons = [
        Comparison(field="gross_weight", label="Gross weight", kind="weight",
                   status="conflict", reason="values differ by 5.68% (12,450 vs 13,200)"),
        Comparison(field="container_number", label="Container no.", kind="container_number",
                   status="conflict", reason="ISO 6346 check digit ARBITRATES"),
    ]
    r.summary = build_summary(r.documents, r.comparisons)
    prompt = build_summary_prompt(r)

    # Verdicts are carried with human labels, not raw keys -- this prompt goes
    # to a prose model, not to code.
    assert "Gross weight: conflict" in prompt
    assert "5.68%" in prompt
    assert "ARBITRATES" in prompt
    # Counts are precomputed and labelled so the model never has to do arithmetic
    # or interpret status names (it got this wrong live).
    assert "conflict: 2" in prompt
    assert "need attention (conflict + suspicious): 2" in prompt
    assert "single-source, informational only, NOT a problem" in prompt
    # The prompt must forbid inventing, since the verdicts are already computed.
    assert "do not contradict" in prompt.lower() or "Do not contradict" in prompt


def test_summary_plausibility_guard():
    assert not summary_is_plausible("")
    assert not summary_is_plausible("too short")
    assert not summary_is_plausible("Here is your summary: blah " * 10)
    assert not summary_is_plausible("x" * 5000)
    assert summary_is_plausible(
        "The quantity is consistent, but the gross weight differs by 5.68% and the "
        "container number fails the ISO 6346 check digit on the packing list."
    )


# ---------------------------------------------------------------------------
# report records both providers
# ---------------------------------------------------------------------------


def test_report_records_both_providers_separately(tmp_path):
    c = ExtractionClient(base(tmp_path))
    r = _report_with_docs()
    refresh_report(r, c)
    assert r.run.model == NVIDIA_VISION_MODEL
    assert r.run.vision_provider == "nvidia"
    # The summary model is a different model on a different host.
    assert r.run.summary_provider == "nebius" or r.run.summary_provider == ""


# ---------------------------------------------------------------------------
# The two live-verified prompt/behaviour fixes
# ---------------------------------------------------------------------------


def test_example_output_is_not_copyable():
    """Live-verified failure: the shape example carried a real-looking doc_type
    and reason, and the model copied both verbatim onto a page whose heading
    plainly said PACKING LIST. The example must show structure, not content."""
    from app.prompt import example_output

    ex = example_output()
    assert "COMMERCIAL INVOICE" not in ex["doc_type_reason"].upper()
    assert ex["doc_type"] not in DOC_TYPES_SET, "example must not name a real type"
    # Shape is still fully specified so the model knows what to emit.
    assert set(ex["fields"]) == set(FIELD_ORDER)
    for f in ex["fields"].values():
        assert set(f) == {"present", "value", "evidence_text", "page_index", "confidence"}


def test_summary_prompt_mentions_no_real_document_type():
    from app.prompt import example_output
    from app.summary import SYSTEM

    assert "/no_think" in SYSTEM, "Nemotron reasoning toggle, verified to cut tokens"
    assert "COMMERCIAL INVOICE" not in example_output()["doc_type_reason"]


def test_declared_doc_type_wins_over_model_guess(tmp_path):
    """A user who says 'this is a packing list' must not have their expectation
    silently rewritten by a model that guessed 'commercial_invoice'."""
    from app.models import ExtractionResponse
    from app.models import effective_doc_type

    payload = ExtractionResponse.model_validate(
        {"doc_type": "commercial_invoice", "doc_type_reason": "x",
         "fields": {k: {"present": False, "value": None, "evidence_text": "",
                        "page_index": 1, "confidence": 0.0} for k in FIELD_ORDER}}
    )
    doc = DocumentReport(id="d", label="d", filename="d.png",
                         declared_doc_type="packing_list",
                         detected_doc_type=payload.doc_type, extraction=payload)
    assert effective_doc_type(doc) == "packing_list"

    # Undeclared documents fall back to the model's detection.
    doc2 = DocumentReport(id="e", label="e", filename="e.png",
                          detected_doc_type="bill_of_lading")
    assert effective_doc_type(doc2) == "bill_of_lading"


def test_doc_type_disagreement_is_recorded_not_hidden():
    from app.models import ExtractionResponse
    from app.orchestrator import apply_outcome
    from app.client import ExtractionOutcome

    payload = ExtractionResponse.model_validate(
        {"doc_type": "commercial_invoice", "doc_type_reason": "x",
         "fields": {k: {"present": False, "value": None, "evidence_text": "",
                        "page_index": 1, "confidence": 0.0} for k in FIELD_ORDER}}
    )
    doc = DocumentReport(id="d", label="d", filename="d.png",
                         declared_doc_type="packing_list")
    outcome = ExtractionOutcome(
        response=payload, status="ok", attempts=[], issues=[], dropped_fields=[],
        used_cache=False, used_fallback_schema=False, source="live_api",
    )
    apply_outcome(doc, outcome)
    assert doc.doc_type_disagrees is True
    assert doc.detected_doc_type == "commercial_invoice"
    assert doc.status == "ok", "a disagreement is a signal, not a failure"


def test_absent_expected_field_uses_declared_type_for_severity():
    """HS code is expected on an invoice, not on a packing list. If the model
    mislabels a packing list as an invoice, an absent HS code must not start
    reading as a problem."""
    from app.orchestrator import build_comparisons

    def doc(doc_id, declared, detected, has_hs):
        fields = {k: {"present": False, "value": None, "evidence_text": "",
                      "page_index": 1, "confidence": 0.0} for k in FIELD_ORDER}
        if has_hs:
            fields["hs_code"] = {"present": True, "value": "8517.62.00",
                                 "evidence_text": "HS code", "page_index": 1,
                                 "confidence": 0.9}
        payload = ExtractionResponse.model_validate(
            {"doc_type": detected, "doc_type_reason": "x", "fields": fields})
        # The real pipeline fills `normalized` in apply_outcome; build it the
        # same way so this test exercises the comparison, not a half-made doc.
        normalized = {k: normalize_field(k, payload.fields[k]) for k in FIELD_ORDER}
        return DocumentReport(id=doc_id, label=doc_id, filename=doc_id + ".png",
                              declared_doc_type=declared, detected_doc_type=detected,
                              status="ok", extraction=payload, normalized=normalized)

    docs = [doc("inv", "commercial_invoice", "commercial_invoice", True),
            doc("pl", "packing_list", "commercial_invoice", False)]
    comparisons = build_comparisons(docs)
    hs = next(c for c in comparisons if c.field == "hs_code")
    assert hs.status == "single_source", "must not be a conflict or an alarming absence"


# ---------------------------------------------------------------------------
# Probe: capacity is not capability
# ---------------------------------------------------------------------------


def _status_error(code: int):
    """APIStatusError takes response/body only -- there is no `request` kwarg."""
    import httpx
    from openai import APIStatusError

    req = httpx.Request("POST", "https://x.example/v1/chat/completions")
    resp = httpx.Response(code, request=req, content=b'{"error":"x"}')
    return APIStatusError("err", response=resp, body={"error": "x"})


@pytest.mark.asyncio
async def test_probe_retries_strict_after_503_instead_of_downgrading(tmp_path, monkeypatch):
    """Live-observed regression: a 503 ResourceExhausted storm made the probe
    conclude the endpoint did not support strict json_schema, when it had
    accepted it moments earlier. Capacity must not be read as capability."""
    def err503():
        return _status_error(503)

    def ok(text):
        msg = SimpleNamespace(content=text, refusal=None, reasoning_content=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))

    script = [err503(), ok('{"ok": true}')]
    c = ExtractionClient(base(tmp_path, vision_provider="nebius"))
    calls = {"n": 0}

    async def create(**kwargs):
        item = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    c._client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))

    async def no_sleep(_t):
        return None

    monkeypatch.setattr("app.client.asyncio.sleep", no_sleep)
    mode = await c.probe(force=True)
    assert mode == "strict", "a 503 must not permanently downgrade the probe"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_probe_does_move_on_when_schema_param_is_rejected(tmp_path):
    """400/422 IS a capability answer, so it must advance to json_object."""
    def err400():
        return _status_error(400)

    def ok(text):
        msg = SimpleNamespace(content=text, refusal=None, reasoning_content=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))

    script = [err400(), ok('{"ok": true}')]
    c = ExtractionClient(base(tmp_path, vision_provider="nebius"))
    n = {"i": 0}

    async def create(**kwargs):
        item = script[min(n["i"], len(script) - 1)]
        n["i"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    c._client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))

    async def no_sleep(_t):
        return None

    mode = await c.probe(force=True)
    assert mode == "json_object"


@pytest.mark.asyncio
async def test_summary_retries_once_when_model_returns_chain_of_thought(tmp_path):
    """Live-observed: the same prompt yielded clean prose on one call and a
    900-token self-check monologue on the next. The client retries once."""
    from app.client import SummaryClient

    cot = (
        'Check: Sentence 1 uses "2 fields agree and 2 fields need attention." '
        "That's exactly the numbers. It uses the words agree and need attention."
    )
    good = "2 fields agree and 2 fields need attention. Gross weight conflicts."
    responses = [cot, good]
    seen_systems = []

    async def create(**kwargs):
        seen_systems.append(kwargs["messages"][0]["content"])
        text = responses[min(len(seen_systems) - 1, len(responses) - 1)]
        msg = SimpleNamespace(content=text, refusal=None, reasoning_content=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30))

    c = SummaryClient(base(tmp_path))
    c._client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))

    text, usage = await c.summarize("summarise this")
    assert text == good, "should have retried past the chain-of-thought reply"
    assert len(seen_systems) == 2, "exactly one retry, not more"
    assert "/no_think" in seen_systems[0] and "/no_think" in seen_systems[1]
    assert usage["prompt"] == 20, "usage accumulates across both attempts"


@pytest.mark.asyncio
async def test_summary_returns_last_attempt_if_still_bad_and_caller_rejects(tmp_path):
    """The retry must not loop. If both attempts are unusable, the caller's
    plausibility check turns it into status='failed', never a crash."""
    from app.client import SummaryClient

    cot = "Let me think. That's the numbers. It uses the words agree here. " * 3
    calls = {"n": 0}

    async def create(**kwargs):
        calls["n"] += 1
        msg = SimpleNamespace(content=cot, refusal=None, reasoning_content=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))

    c = SummaryClient(base(tmp_path))
    c._client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))

    text, _ = await c.summarize("summarise this")
    assert calls["n"] == 2, "must stop after one retry, not loop forever"

    rec = Reconciler(base(tmp_path))
    async def explode(prompt):
        return (text, {})
    rec.summary_client.summarize = explode
    report = _report_with_docs()
    from app.compare import Comparison
    from app.orchestrator import build_summary
    report.comparisons = [Comparison(field="quantity", label="Q", kind="count",
                                     status="consistent", reason="agrees")]
    report.summary = build_summary(report.documents, report.comparisons)
    await rec._write_summary(report)
    assert report.narrative.status == "failed"
    assert report.narrative.text is None


# ---------------------------------------------------------------------------
# cross-provider fallback: a document the primary host failed is not lost
# ---------------------------------------------------------------------------


def _pages():
    from app.ingest import ingest

    return ingest("invoice_clean.png",
                  (REPO / "fixtures" / "images" / "invoice_clean.png").read_bytes())


def _payload() -> ExtractionResponse:
    """A full extraction body from the fixture cache (captured model output)."""
    index = json.loads((REPO / "fixtures" / "index.json").read_text(encoding="utf-8"))
    doc = index["shipments"]["shipment_clean"]["documents"][0]
    body = json.loads(
        (REPO / "fixtures" / "responses" / f"{doc['cache_key']}.json")
        .read_text(encoding="utf-8")
    )["response"]
    return ExtractionResponse.model_validate(body)


def _ok(provider: str, model: str) -> ExtractionOutcome:
    return ExtractionOutcome(
        response=_payload(), status="ok", attempts=[], issues=[], dropped_fields=[],
        used_cache=False, used_fallback_schema=False, source="live_api",
        provider=provider, model=model,
    )


def _failed(provider: str, model: str, error: str) -> ExtractionOutcome:
    return ExtractionOutcome(
        response=None, status="failed", attempts=[], issues=[], dropped_fields=[],
        used_cache=False, used_fallback_schema=False, error=error,
        provider=provider, model=model,
    )


def _capture_stages(rec: Reconciler) -> list[dict]:
    """Collect stage events instead of publishing them to SSE listeners."""
    events: list[dict] = []

    def capture(shipment_id, **kwargs):
        events.append(kwargs)

    rec._emit_stage = capture  # type: ignore[method-assign]
    return events


async def _read(rec: Reconciler, tmp_path, document_id="d1"):
    return await rec._extract_with_fallback(
        shipment_id="s1", document_id=document_id, document_label="Invoice",
        pages=_pages(), declared_doc_type="commercial_invoice",
        raw_dir=tmp_path / "raw",
    )


@pytest.mark.asyncio
async def test_a_document_the_primary_host_failed_reads_on_the_other(tmp_path):
    rec = Reconciler(base(tmp_path))
    events = _capture_stages(rec)
    asked: list[dict] = []

    async def primary_dies(**kwargs):
        raise RuntimeError("worker local total request limit reached")

    async def other_reads(**kwargs):
        asked.append(kwargs)
        return _ok("nebius", NEBIUS_FALLBACK_VISION_MODEL)

    rec.client.extract_document = primary_dies  # type: ignore[method-assign]
    rec._consensus_client("nebius").extract_document = other_reads  # type: ignore[method-assign]

    res = await _read(rec, tmp_path)

    assert isinstance(res, ExtractionOutcome) and res.response is not None
    assert res.provider == "nebius", "the surviving answer must be credited to the host that gave it"
    assert res.model == NEBIUS_FALLBACK_VISION_MODEL
    assert [e["provider"] for e in events] == ["nvidia", "nebius"], (
        "the timeline has to show the document moving to the other host"
    )
    # The fallback's evidence is written under its own name, so a later read of
    # the primary's raw body cannot be confused with the one that answered.
    assert asked[0]["document_id"] == "d1_fb"
    assert asked[0]["pages"][0].sha256 == _pages()[0].sha256, (
        "the same images must be handed over, not a fresh upload"
    )


@pytest.mark.asyncio
async def test_an_answer_the_primary_did_produce_is_never_rewritten(tmp_path):
    """A partial extraction is still the configured provider's answer. Routing
    it elsewhere would trade a usable document for someone else's opinion."""
    rec = Reconciler(base(tmp_path))
    asked: list[dict] = []

    async def primary_answers(**kwargs):
        return _ok("nvidia", NVIDIA_VISION_MODEL)

    async def other_reads(**kwargs):
        asked.append(kwargs)
        return _ok("nebius", NEBIUS_FALLBACK_VISION_MODEL)

    rec.client.extract_document = primary_answers  # type: ignore[method-assign]
    rec._consensus_client("nebius").extract_document = other_reads  # type: ignore[method-assign]

    res = await _read(rec, tmp_path)
    assert res.provider == "nvidia"
    assert asked == [], "the other host must not be paid for a document that was answered"


@pytest.mark.asyncio
async def test_the_fallback_can_be_switched_off(tmp_path):
    rec = Reconciler(base(tmp_path, vision_fallback=False))
    _capture_stages(rec)

    async def primary_dies(**kwargs):
        raise RuntimeError("boom")

    rec.client.extract_document = primary_dies  # type: ignore[method-assign]

    res = await _read(rec, tmp_path)
    assert isinstance(res, RuntimeError) and "boom" in str(res)
    assert rec._consensus_clients == {}, "an off switch that still built a client is not off"


@pytest.mark.asyncio
async def test_a_host_without_a_key_is_not_asked_to_fall_back(tmp_path):
    rec = Reconciler(base(tmp_path, nebius_api_key=""))
    _capture_stages(rec)

    async def primary_dies(**kwargs):
        raise RuntimeError("boom")

    rec.client.extract_document = primary_dies  # type: ignore[method-assign]

    res = await _read(rec, tmp_path)
    assert isinstance(res, RuntimeError) and "boom" in str(res)
    assert "fallback" not in str(res)


@pytest.mark.asyncio
async def test_when_both_hosts_fail_the_error_names_both(tmp_path):
    rec = Reconciler(base(tmp_path))
    _capture_stages(rec)

    async def primary_dies(**kwargs):
        raise RuntimeError("deadline exceeded")

    async def other_fails(**kwargs):
        return _failed("nebius", NEBIUS_FALLBACK_VISION_MODEL, "circuit breaker open")

    rec.client.extract_document = primary_dies  # type: ignore[method-assign]
    rec._consensus_client("nebius").extract_document = other_fails  # type: ignore[method-assign]

    res = await _read(rec, tmp_path)
    assert isinstance(res, RuntimeError)
    assert "deadline exceeded" in str(res)
    assert "nebius" in str(res) and "circuit breaker open" in str(res)


@pytest.mark.asyncio
async def test_a_run_that_loses_a_host_still_delivers_the_document(tmp_path):
    """The whole point of the routing, end to end: the shipment finishes with a
    document a dead primary provider would otherwise have taken with it."""
    rec = Reconciler(base(tmp_path, model_discovery=False, enable_summary=False,
                          enable_adjudication=False, enable_briefing=False))

    async def primary_dies(**kwargs):
        return _failed("nvidia", NVIDIA_VISION_MODEL, "transport failed: HTTP 503")

    async def other_reads(**kwargs):
        return _ok("nebius", NEBIUS_FALLBACK_VISION_MODEL)

    rec.client.extract_document = primary_dies  # type: ignore[method-assign]
    rec._consensus_client("nebius").extract_document = other_reads  # type: ignore[method-assign]

    blob = (REPO / "fixtures" / "images" / "invoice_clean.png").read_bytes()
    sid, _ = await rec.create_shipment([("invoice_clean.png", "commercial_invoice", blob)])
    await rec.wait(sid, timeout=60)

    report = rec.get(sid)
    doc = report.documents[0]
    assert doc.status in ("ok", "partial")
    assert doc.extraction is not None
    assert doc.provider == "nebius"
    assert report.run.vision_provider == "nvidia", (
        "the configured primary stays on the run; the fallback is per document"
    )
    assert any("read on nebius instead" in i for i in doc.notes), doc.notes

    # The routing note must be visible in the artifacts, not just the model: a
    # report that shows a nebius answer on an nvidia run without saying why is
    # not honest.
    from app.export import standalone_html
    from app.pdf import report_html

    assert "read on nebius instead" in standalone_html(report)
    assert "read on nebius instead" in report_html(report)


@pytest.mark.asyncio
async def test_an_open_breaker_on_both_hosts_is_reported_not_retried(tmp_path):
    """The other host's breaker being open is not a reason to try it anyway;
    that is what the breaker exists to prevent."""
    from app.client import ProviderDegraded

    rec = Reconciler(base(tmp_path))
    _capture_stages(rec)

    async def primary_dies(**kwargs):
        raise ProviderDegraded("circuit breaker open")

    rec.client.extract_document = primary_dies  # type: ignore[method-assign]
    other = rec._consensus_client("nebius")
    other._consecutive_failures = other.settings.circuit_breaker_threshold

    res = await _read(rec, tmp_path)
    assert isinstance(res, ProviderDegraded), (
        "the primary's breaker-open error is what the report must show"
    )
    assert "fallback" not in str(res), "a degraded host must not be called at all"


def test_consensus_provider_pinning_to_the_primary_disables_fallback(tmp_path):
    """CONSENSUS_PROVIDER can point the second reader at the primary's host;
    there is then nowhere to fall back to, and paying the same host twice is
    worse than failing honestly."""
    rec = Reconciler(base(tmp_path))
    assert rec._fallback_client() is not None

    pinned = Reconciler(base(tmp_path, consensus_provider="nvidia"))
    assert pinned._fallback_client() is None
