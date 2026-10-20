"""Client behaviour with a fake transport. Zero real API calls.

The live endpoint has never been contacted (no key). These tests pin down the
things that would otherwise be guesses: the exact response_format payload sent
to Nebius, the ladder's call count, cache reuse, and that DEMO_MODE cannot
reach the network even by accident.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from openai import APIStatusError

from app.cache import cache_key
from app.client import BACKOFF_SCHEDULE, ExtractionClient
from app.config import Settings
from app.fields import FIELD_ORDER
from app.ingest import ingest
from app.models import extraction_json_schema

REPO = Path(__file__).resolve().parents[1]
IMAGE = REPO / "fixtures" / "images" / "invoice_clean.png"


def make_settings(tmp_path, **over) -> Settings:
    """Extraction-client tests default to the Nebius vision path.

    These tests are about provider-agnostic machinery (ladder, probe, cache,
    backoff, DEMO_MODE safety), so pinning one provider keeps their existing
    assertions valid. Provider routing itself is covered in test_providers.py.
    """
    base = dict(
        _env_file=None,
        demo_mode=False,
        vision_provider="nebius",
        nebius_api_key="test-key-not-real",
        nebius_model="nvidia/Nemotron-Nano-12B-V2-VL",
        runs_dir=tmp_path / "runs",
        cache_dir=tmp_path / "cache",
    )
    base.update(over)
    return Settings(**base)


def pages() -> list:
    return ingest("invoice_clean.png", IMAGE.read_bytes())


class FakeCompletions:
    def __init__(self, script: list[str]):
        self.script = script
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(text, Exception):
            raise text
        return _completion(text)


class FakeChat:
    def __init__(self, script):
        self.completions = FakeCompletions(script)


class FakeClient:
    """Stands in for AsyncOpenAI."""

    def __init__(self, script):
        self.chat = FakeChat(script)


def _completion(text: str):
    from types import SimpleNamespace

    msg = SimpleNamespace(content=text, refusal=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None)


def good_response() -> str:
    fields = {
        key: {
            "present": False,
            "value": None,
            "evidence_text": "",
            "page_index": 1,
            "confidence": 0.0,
        }
        for key in FIELD_ORDER
    }
    fields["gross_weight"] = {
        "present": True,
        "value": "12,450.00 KGS",
        "evidence_text": "TOTAL GROSS WT 12,450.00 KGS",
        "page_index": 1,
        "confidence": 0.95,
    }
    return json.dumps(
        {"doc_type": "commercial_invoice", "doc_type_reason": "header", "fields": fields}
    )


def install(client: ExtractionClient, script: list) -> FakeClient:
    fake = FakeClient(script)
    # Bypass the property by writing the private attribute directly.
    client._client = fake
    return fake


def status_error(code: int) -> APIStatusError:
    import httpx

    request = httpx.Request("POST", "https://api.tokenfactory.nebius.com/v1/chat/completions")
    response = httpx.Response(code, request=request, content=b'{"error":"x"}')
    # APIStatusError takes response/body only; there is no request kwarg.
    return APIStatusError("boom", response=response, body={"error": "x"})


# ---------------------------------------------------------------------------
# payload shape — the thing that must not be assumed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_mode_sends_wrapped_schema_that_the_live_api_accepts(tmp_path):
    """Live-verified 2026-09-23: Token Factory docs show a BARE schema, but the
    endpoint 422s with 'response_format.json_schema.name: Field required'.
    It wants OpenAI's wrapped {name, strict, schema}. This test pins the shape
    that actually works, so a future 'cleanup' back to the docs form fails here.
    """
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "strict"
    c._probed = True
    fake = install(c, [good_response()])

    await c.extract_document(document_id="d1", pages=pages())

    sent = fake.chat.completions.calls[0]["response_format"]
    assert sent["type"] == "json_schema"
    inner = sent["json_schema"]
    assert inner["name"] == "shipment_extraction"
    assert inner["strict"] is True
    assert inner["schema"] == extraction_json_schema()


@pytest.mark.asyncio
async def test_json_object_mode_sends_no_schema(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "json_object"
    c._probed = True
    fake = install(c, [good_response()])

    await c.extract_document(document_id="d1", pages=pages())
    assert fake.chat.completions.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_plain_mode_omits_response_format_entirely(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, [good_response()])

    await c.extract_document(document_id="d1", pages=pages())
    assert "response_format" not in fake.chat.completions.calls[0]


@pytest.mark.asyncio
async def test_vision_parts_use_documented_image_url_shape(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, [good_response()])

    await c.extract_document(document_id="d1", pages=pages())
    content = fake.chat.completions.calls[0]["messages"][1]["content"]
    images = [p for p in content if p.get("type") == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # Page markers must precede each image so page_index is meaningful.
    assert any(p.get("type") == "text" and "PAGE 1" in p.get("text", "") for p in content)


@pytest.mark.asyncio
async def test_model_and_base_url_come_from_settings(tmp_path):
    c = ExtractionClient(make_settings(tmp_path, nebius_model="some/Exact-ID"))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, [good_response()])
    await c.extract_document(document_id="d1", pages=pages())
    assert fake.chat.completions.calls[0]["model"] == "some/Exact-ID"


@pytest.mark.asyncio
async def test_temperature_is_zero_for_extraction(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, [good_response()])
    await c.extract_document(document_id="d1", pages=pages())
    assert fake.chat.completions.calls[0]["temperature"] == 0.0


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_accepts_strict_when_endpoint_honours_it(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    install(c, ['{"ok": true}'])
    assert await c.probe() == "strict"


@pytest.mark.asyncio
async def test_probe_downgrades_when_strict_is_rejected(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    install(c, [status_error(400), '{"ok": true}'])
    assert await c.probe() == "json_object"


@pytest.mark.asyncio
async def test_probe_falls_back_to_plain_when_both_rejected(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    install(c, [status_error(400), status_error(400)])
    assert await c.probe() == "plain"


@pytest.mark.asyncio
async def test_probe_downgrades_when_json_is_unusable(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    install(c, ["I cannot produce JSON", "I cannot produce JSON"])
    assert await c.probe() == "plain"


@pytest.mark.asyncio
async def test_probe_is_cached_and_not_repeated(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    fake = install(c, ['{"ok": true}', good_response()])
    assert await c.probe() == "strict"
    await c.extract_document(document_id="d1", pages=pages())
    # one probe call + one extraction call; no second probe
    assert len(fake.chat.completions.calls) == 2


# ---------------------------------------------------------------------------
# ladder: call counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_response_makes_exactly_one_call(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, [good_response()])
    out = await c.extract_document(document_id="d1", pages=pages())
    assert out.status == "ok" and len(fake.chat.completions.calls) == 1


@pytest.mark.asyncio
async def test_fenced_output_is_repaired_without_an_extra_call(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, ["```json\n" + good_response() + "\n```"])
    out = await c.extract_document(document_id="d1", pages=pages())
    assert out.status == "ok"
    assert len(fake.chat.completions.calls) == 1, "rung 2 is free"


@pytest.mark.asyncio
async def test_rung_4_costs_exactly_one_extra_call(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, ["total gibberish", good_response()])
    out = await c.extract_document(document_id="d1", pages=pages())
    assert out.status == "ok"
    assert len(fake.chat.completions.calls) == 2
    assert [a.rung for a in out.attempts] == ["initial", "corrected"]
    # The correction message must feed the bad output and the reason back.
    correction = fake.chat.completions.calls[1]["messages"]
    assert correction[2]["role"] == "assistant" and "total gibberish" in correction[2]["content"]
    assert "could not be used" in correction[3]["content"]


@pytest.mark.asyncio
async def test_rung_5_uses_the_simplified_schema(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, ["gibberish", "still gibberish", good_response()])
    out = await c.extract_document(document_id="d1", pages=pages())
    assert out.status == "ok"
    assert len(fake.chat.completions.calls) == 3
    assert out.used_fallback_schema is True
    assert [a.rung for a in out.attempts] == ["initial", "corrected", "simplified"]


@pytest.mark.asyncio
async def test_gives_up_cleanly_after_the_ladder(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, ["junk", "junk", "junk"])
    out = await c.extract_document(document_id="d1", pages=pages())
    assert out.status == "failed"
    assert out.response is None
    assert len(fake.chat.completions.calls) == 3
    assert "gave up" in out.error


@pytest.mark.asyncio
async def test_one_bad_field_yields_partial_not_failure(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    install(c, [good_response().replace('"hs_code": {', '"hs_code": "junk", "x": {')])
    out = await c.extract_document(document_id="d1", pages=pages())
    assert out.status in ("partial", "ok")
    assert out.response is not None


# ---------------------------------------------------------------------------
# transport vs ladder separation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_retries_at_transport_layer_without_burning_a_ladder_rung(tmp_path, monkeypatch):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, [status_error(429), status_error(429), good_response()])

    slept: list[float] = []

    async def fake_sleep(t):
        slept.append(t)

    monkeypatch.setattr("app.client.asyncio.sleep", fake_sleep)
    out = await c.extract_document(document_id="d1", pages=pages())

    assert out.status == "ok"
    assert len(out.attempts) == 1, "three transports, one ladder rung"
    assert out.attempts[0].rung == "initial"
    assert len(slept) == 2, "backoff slept between transport attempts"
    assert len(fake.chat.completions.calls) == 3, "two retried transports + one success"


def test_backoff_schedule_is_per_provider():
    """Nebius cleared inside 3 attempts live. NVIDIA's shared workers returned
    503 ResourceExhausted five times before succeeding, so it needs a longer
    ladder -- a 3-attempt schedule would fail real extractions."""
    assert BACKOFF_SCHEDULE["nebius"] == (1.0, 4.0, 12.0)
    assert len(BACKOFF_SCHEDULE["nvidia"]) >= 5


@pytest.mark.asyncio
async def test_400_is_not_retried_at_transport_level(tmp_path, monkeypatch):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, [status_error(400), good_response()])

    async def no_sleep(_t):
        return

    monkeypatch.setattr("app.client.asyncio.sleep", no_sleep)
    out = await c.extract_document(document_id="d1", pages=pages())
    # A 400 is a request problem, not a blip: it surfaces immediately.
    assert out.status == "failed"
    assert len(fake.chat.completions.calls) == 1
    assert "transport failed" in out.error


@pytest.mark.asyncio
async def test_persistent_5xx_reports_transport_failure(tmp_path, monkeypatch):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    install(c, [status_error(503)] * 5)

    async def no_sleep(_t):
        return

    monkeypatch.setattr("app.client.asyncio.sleep", no_sleep)
    out = await c.extract_document(document_id="d1", pages=pages())
    assert out.status == "failed"
    assert "503" in out.error


class NeverRespondingCompletions:
    """A transport that opens and then never answers — the silent-hang case.

    The SDK's read timeout does not cover this (measured live: the omni host did
    it under load); the request_deadline_seconds deadline must.
    """

    async def create(self, **kwargs):
        await asyncio.Event().wait()  # never resolves


async def test_a_silent_hang_is_bounded_by_the_deadline(tmp_path, monkeypatch):
    from types import SimpleNamespace

    c = ExtractionClient(make_settings(tmp_path, request_deadline_seconds=0.2))
    c.json_mode = "plain"
    c._probed = True
    c._client = SimpleNamespace(chat=SimpleNamespace(
        completions=NeverRespondingCompletions()))

    async def no_sleep(_t):
        return

    monkeypatch.setattr("app.client.asyncio.sleep", no_sleep)
    out = await c.extract_document(document_id="d1", pages=pages())
    assert out.status == "failed"
    assert "deadline exceeded" in out.error
    # Every schedule slot became a deadline expiry, not a hang: the ladder ran.
    assert out.attempts and all("deadline exceeded" in a.note for a in out.attempts)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_identical_document_is_served_from_cache(tmp_path):
    c = ExtractionClient(make_settings(tmp_path))
    c.json_mode = "plain"
    c._probed = True
    fake = install(c, [good_response(), good_response()])

    first = await c.extract_document(document_id="d1", pages=pages())
    second = await c.extract_document(document_id="d2", pages=pages())

    assert first.used_cache is False
    assert second.used_cache is True
    assert len(fake.chat.completions.calls) == 1, "a cached document must not re-bill"


@pytest.mark.asyncio
async def test_cache_key_changes_with_prompt_version(tmp_path):
    ps = [p.sha256 for p in pages()]
    model = "nvidia/Nemotron-Nano-12B-V2-VL"
    assert cache_key(ps, model, "extract-v1") != cache_key(ps, model, "extract-v2")


@pytest.mark.asyncio
async def test_cache_key_changes_with_model(tmp_path):
    ps = [p.sha256 for p in pages()]
    assert cache_key(ps, "model-a") != cache_key(ps, "model-b")


@pytest.mark.asyncio
async def test_raw_response_is_persisted_per_attempt(tmp_path):
    raw_dir = tmp_path / "raw"
    c = ExtractionClient(make_settings(tmp_path), raw_dir=raw_dir)
    c.json_mode = "plain"
    c._probed = True
    install(c, ["garbage first", good_response()])
    await c.extract_document(document_id="d1", pages=pages())

    written = {p.name: p.read_text() for p in raw_dir.iterdir()}
    assert len(written) == 2, "failed attempts must be kept too"
    initial = next(t for n, t in written.items() if "initial" in n)
    corrected = next(t for n, t in written.items() if "corrected" in n)
    assert "garbage first" in initial
    assert '"doc_type"' in corrected


# ---------------------------------------------------------------------------
# DEMO_MODE safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_mode_never_constructs_a_network_client(tmp_path):
    c = ExtractionClient(make_settings(tmp_path, demo_mode=True, nebius_api_key=""))
    with pytest.raises(RuntimeError, match="DEMO_MODE is on"):
        _ = c.client


@pytest.mark.asyncio
async def test_demo_mode_serves_from_cache_without_network(tmp_path):
    real_cache = REPO / "fixtures" / "responses"
    c = ExtractionClient(
        make_settings(tmp_path, demo_mode=True, nebius_api_key="", cache_dir=real_cache)
    )
    out = await c.extract_document(document_id="d1", pages=pages())
    assert out.status == "ok"
    assert out.used_cache is True
    assert out.json_mode == "cache"


@pytest.mark.asyncio
async def test_demo_mode_probe_does_not_call_the_api(tmp_path):
    c = ExtractionClient(make_settings(tmp_path, demo_mode=True, nebius_api_key=""))
    assert await c.probe() == "cache"


@pytest.mark.asyncio
async def test_missing_key_raises_a_useful_error(tmp_path):
    c = ExtractionClient(make_settings(tmp_path, demo_mode=False, nebius_api_key=""))
    with pytest.raises(RuntimeError, match="NEBIUS_API_KEY is not set"):
        _ = c.client


@pytest.mark.asyncio
async def test_placeholder_key_is_rejected(tmp_path):
    c = ExtractionClient(
        make_settings(tmp_path, demo_mode=False, nebius_api_key="your-api-key-here")
    )
    with pytest.raises(RuntimeError, match="NEBIUS_API_KEY is not set"):
        _ = c.client


# ---------------------------------------------------------------------------
# semaphore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_is_capped_by_the_semaphore(tmp_path):
    c = ExtractionClient(make_settings(tmp_path, max_concurrency=2))
    c.json_mode = "plain"
    c._probed = True

    live = 0
    peak = 0

    fake = install(c, [good_response()] * 10)
    original = fake.chat.completions.create

    async def counting(**kwargs):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return await original(**kwargs)

    fake.chat.completions.create = counting

    ps = pages()
    await asyncio.gather(*(c.extract_document(document_id=f"d{i}", pages=ps) for i in range(6)))
    assert peak <= 2, f"semaphore breached: peak concurrency {peak}"
