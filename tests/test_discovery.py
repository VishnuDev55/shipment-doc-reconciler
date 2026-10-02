"""Model discovery, tested against catalogs rather than live hosts.

No API key exists on this machine, so every test here feeds a catalog through
httpx.MockTransport. That verifies the decision logic and the request shape; it
does not verify what NVIDIA and Nebius actually list today. See
docs/AUDIT.md R9 — the live half of this is unverified by construction.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.discovery import DiscoveryError, ModelInfo, fetch, parse_catalog, pick, resolve
from app.orchestrator import Reconciler
from app.providers import Endpoint

NVIDIA_HOST = "integrate.api.nvidia.com"
NEBIUS_HOST = "api.tokenfactory.nebius.com"

CONFIGURED_VISION = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"


def nvidia_endpoint(**over) -> Endpoint:
    base = dict(
        provider="nvidia",
        base_url="https://integrate.api.nvidia.com/v1/",
        api_key="nvapi-TEST-ONLY-0000-1234",
        model=CONFIGURED_VISION,
        label="vision",
    )
    base.update(over)
    return Endpoint(**base)


def nebius_endpoint(**over) -> Endpoint:
    base = dict(
        provider="nebius",
        base_url="https://api.tokenfactory.nebius.com/v1/",
        api_key="sk-test-nebius-0000-9876",
        model="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        label="summary",
    )
    base.update(over)
    return Endpoint(**base)


def handler(body, status: int = 200, capture: list | None = None):
    def _h(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(_h)


# ---------------------------------------------------------------------------
# parsing: the two hosts do not agree on a shape
# ---------------------------------------------------------------------------


def test_parses_nvidias_models_envelope():
    """The real payload, captured from integrate.api.nvidia.com on 2026-10-02.

    81 rows, each only {id, object, created, owned_by}: NVIDIA publishes no
    modality at all, and /v1/models/{id} returns the same four fields. So on
    this host image capability can only be inferred from the name, and the test
    says so rather than pretending the host declared it.
    """
    rows = parse_catalog({"object": "list", "data": [
        {"id": "meta/llama-3.2-11b-vision-instruct", "object": "model",
         "created": 735790403, "owned_by": "meta"},
        {"id": "adept/fuyu-8b", "object": "model", "created": 735790403,
         "owned_by": "adept"},
    ]})
    assert [m.id for m in rows] == ["meta/llama-3.2-11b-vision-instruct", "adept/fuyu-8b"]
    assert all(not m.declares_modality for m in rows)
    assert rows[0].accepts_images, "matched on 'vision' in the name"
    assert not rows[1].accepts_images


def test_the_legacy_models_wrapper_is_still_parsed():
    """Not what NVIDIA returns today, but cheap to keep accepting."""
    rows = parse_catalog({"models": [{"id": "a", "type": "image_text_to_text"}]})
    assert rows[0].accepts_images and rows[0].declares_modality


def test_embedding_models_are_not_picked_as_chat_vision_fallbacks():
    """A '-vl' embedding model really is vision-language, and really is useless
    as a chat completion endpoint."""
    rows = parse_catalog({"data": [
        {"id": "nvidia/llama-nemotron-embed-vl-1b-v2"},
        {"id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"},
    ]})
    assert [m.accepts_images for m in rows] == [False, True]
    assert pick(rows, "vision").id.startswith("nvidia/nemotron-3-nano-omni")


def test_a_declared_capability_beats_an_inferred_one():
    """Ordering must never let a confident guess outrank what a host stated."""
    rows = parse_catalog({"data": [
        {"id": "nvidia/Nemotron-VL-Huge", "modality": "text->text"},
        {"id": "openbmb/MiniCPM-V-4_5", "modality": "text+image->text"},
    ]})
    assert pick(rows, "vision").id == "openbmb/MiniCPM-V-4_5"


def test_inference_is_recorded_on_the_selection_so_it_cannot_impersonate_a_declaration():
    rows = parse_catalog({"data": [
        {"id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"}]})
    chosen = resolve(role="vision", provider="nvidia", host=NVIDIA_HOST,
                     requested="gone", catalog=rows)
    assert chosen.status == "discovered" and chosen.inferred is True
    assert "inferred" in chosen.note and "no modality metadata" in chosen.note

    declared = resolve(role="vision", provider="nebius", host=NEBIUS_HOST,
                       requested="gone",
                       catalog=parse_catalog({"data": [{"id": "m", "modality": "text+image->text"}]}))
    assert declared.inferred is False


def test_parses_openai_data_envelope_with_modality():
    rows = parse_catalog(
        {"data": [{"id": "openbmb/MiniCPM-V-4_5", "modality": "text+image->text"},
                  {"id": "meta-llama/Llama-3.3-70B-Instruct", "modality": "text->text"}]}
    )
    assert rows[0].accepts_images
    assert not rows[1].accepts_images


def test_unusable_rows_are_dropped_rather_than_guessed():
    """No identifier, or not an object at all: skipped, never invented.

    `name` is accepted as well as `id` because hosts are not consistent about
    which key they use; either one is an identifier the host actually stated.
    """
    rows = parse_catalog(
        {"data": [{"name": "called-by-name"}, {"id": "called-by-id"},
                  {"modality": "text->text"}, "junk", 7, None]}
    )
    assert [m.id for m in rows] == ["called-by-name", "called-by-id"]


# ---------------------------------------------------------------------------
# choosing
# ---------------------------------------------------------------------------


def vision_catalog() -> list[ModelInfo]:
    return [
        ModelInfo(id="meta-llama/Llama-3.3-70B", modality="text->text"),
        ModelInfo(id="openbmb/MiniCPM-V-4_5", modality="text+image->text"),
        ModelInfo(id="nvidia/Nemotron-VL-Nano", modality="text+image->text"),
    ]


def test_configured_model_is_kept_when_the_host_lists_it():
    s = resolve(role="vision", provider="nebius", host=NEBIUS_HOST,
                requested="openbmb/MiniCPM-V-4_5", catalog=vision_catalog())
    assert (s.status, s.chosen) == ("configured", "openbmb/MiniCPM-V-4_5")


def test_a_retired_model_triggers_a_discovered_fallback():
    """The exact failure this repo already paid for by hand."""
    s = resolve(role="vision", provider="nebius", host=NEBIUS_HOST,
                requested="nvidia/Nemotron-Nano-12B-V2-VL", catalog=vision_catalog())
    assert s.status == "discovered"
    assert s.chosen == "nvidia/Nemotron-VL-Nano"
    assert "not served" in s.note


def test_nemotron_is_preferred_over_other_vision_models():
    s = resolve(role="vision", provider="nebius", host=NEBIUS_HOST,
                requested="gone", catalog=list(reversed(vision_catalog())))
    assert s.chosen == "nvidia/Nemotron-VL-Nano"


def test_choice_is_independent_of_catalog_order():
    forward = pick(vision_catalog(), "vision")
    backward = pick(list(reversed(vision_catalog())), "vision")
    assert forward.id == backward.id, "a set-iteration order would make runs unreproducible"


def test_operator_allow_list_outranks_discovery():
    s = resolve(role="vision", provider="nebius", host=NEBIUS_HOST,
                requested="gone", catalog=vision_catalog(),
                allow="openbmb/MiniCPM-V-4_5")
    assert (s.status, s.chosen) == ("configured", "openbmb/MiniCPM-V-4_5")


def test_a_host_with_no_vision_model_is_reported_not_invented():
    text_only = [ModelInfo(id="a", modality="text->text")]
    s = resolve(role="vision", provider="nebius", host=NEBIUS_HOST,
                requested="gone", catalog=text_only)
    assert s.status == "unverified"
    assert s.chosen == "gone", "must not silently run on a text-only model"
    assert "no model identifiable as able to accept images" in s.note


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_sends_the_key_and_reads_the_catalog():
    seen: list[httpx.Request] = []
    transport = handler({"models": [{"id": CONFIGURED_VISION, "type": "image_text"}]},
                        capture=seen)
    catalog = await fetch(nvidia_endpoint(), transport=transport)

    assert [m.id for m in catalog.models] == [CONFIGURED_VISION]
    assert seen[0].headers["authorization"] == "Bearer nvapi-TEST-ONLY-0000-1234"
    assert seen[0].url.path == "/v1/models"


@pytest.mark.asyncio
async def test_only_nebius_is_asked_for_verbose_modality():
    """Modality comes free on NVIDIA; asking Token Factory for it is the whole
    reason the vision filter works there."""
    seen: list[httpx.Request] = []
    await fetch(nebius_endpoint(),
                transport=handler({"data": [{"id": "x", "modality": "text+image->text"}]},
                                  capture=seen))
    assert seen[0].url.params["verbose"] == "true"

    seen.clear()
    await fetch(nvidia_endpoint(),
                transport=handler({"models": [{"id": "x"}]}, capture=seen))
    assert "verbose" not in seen[0].url.params


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_errors_become_discovery_errors(status):
    with pytest.raises(DiscoveryError):
        await fetch(nvidia_endpoint(), transport=handler({"error": "x"}, status=status))


@pytest.mark.asyncio
async def test_an_empty_catalog_counts_as_failure_not_as_no_models():
    """A shape change would otherwise be read as "this host serves nothing" and
    quietly strand the configured model."""
    with pytest.raises(DiscoveryError):
        await fetch(nvidia_endpoint(), transport=handler({"unexpected": []}))


@pytest.mark.asyncio
async def test_a_non_json_body_is_a_discovery_error():
    def _h(request):
        return httpx.Response(200, text="<html>login</html>")

    with pytest.raises(DiscoveryError):
        await fetch(nvidia_endpoint(), transport=httpx.MockTransport(_h))


# ---------------------------------------------------------------------------
# integration with the reconciler
# ---------------------------------------------------------------------------


def live_settings(tmp_path, **over) -> Settings:
    base = dict(
        _env_file=None,
        demo_mode=False,
        vision_provider="nvidia",
        nvidia_api_key="nvapi-TEST-ONLY-0000-1234",
        nebius_api_key="sk-test-nebius-0000-9876",
        runs_dir=tmp_path / "runs",
        cache_dir=tmp_path / "cache",
    )
    base.update(over)
    return Settings(**base)


def two_host_transport(vision_body, summary_body):
    def _h(request: httpx.Request) -> httpx.Response:
        body = summary_body if NEBIUS_HOST in request.url.host else vision_body
        return httpx.Response(200, json=body)

    return httpx.MockTransport(_h)


@pytest.mark.asyncio
async def test_discovery_adopts_a_served_model_and_rebuilds_the_client(tmp_path):
    rec = Reconciler(live_settings(tmp_path))
    before = rec.client.endpoint.model
    transport = two_host_transport(
        {"models": [{"id": "nvidia/replaced-vision-9b", "type": "image_text_to_text"}]},
        {"data": [{"id": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                   "modality": "text->text"}]},
    )

    await rec.refresh_models(transport=transport)

    assert rec.client is not None
    assert rec.client.endpoint.model == "nvidia/replaced-vision-9b"
    assert rec.client.endpoint.model != before
    assert rec.summary_client.endpoint.model == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    selections = {s["role"]: s for s in rec.model_selections()}
    assert selections["vision"]["status"] == "discovered"
    assert selections["summary"]["status"] == "configured"


@pytest.mark.asyncio
async def test_a_confirmed_model_leaves_the_client_alone(tmp_path):
    rec = Reconciler(live_settings(tmp_path))
    client_before = rec.client
    transport = two_host_transport(
        {"models": [{"id": CONFIGURED_VISION, "type": "image_text_to_text"}]},
        {"data": [{"id": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                   "modality": "text->text"}]},
    )

    await rec.refresh_models(transport=transport)

    assert rec.client is client_before, "rebuilding without a change re-triggers the probe"


@pytest.mark.asyncio
async def test_an_unreachable_host_changes_nothing(tmp_path):
    rec = Reconciler(live_settings(tmp_path))

    def _h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    await rec.refresh_models(transport=httpx.MockTransport(_h))

    assert rec.client.endpoint.model == CONFIGURED_VISION
    selections = {s["role"]: s for s in rec.model_selections()}
    assert selections["vision"]["status"] == "unverified"
    assert "HTTP 500" in selections["vision"]["note"]


@pytest.mark.asyncio
async def test_demo_mode_makes_no_discovery_call_at_all(tmp_path):
    """The socket guard would catch a leak, but the intent belongs in the code."""
    rec = Reconciler(live_settings(tmp_path, demo_mode=True))
    calls: list[str] = []

    def _h(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"models": []})

    await rec.refresh_models(transport=httpx.MockTransport(_h))
    assert calls == []
    assert rec.model_selections() == []


@pytest.mark.asyncio
async def test_a_missing_key_is_recorded_as_unverified_without_a_call(tmp_path):
    rec = Reconciler(live_settings(tmp_path, nvidia_api_key="", nebius_api_key=""))
    calls: list[str] = []

    def _h(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"models": []})

    await rec.refresh_models(transport=httpx.MockTransport(_h))

    assert calls == []
    assert {s["status"] for s in rec.model_selections()} == {"unverified"}


@pytest.mark.asyncio
async def test_the_catalog_is_reused_until_the_ttl_expires(tmp_path):
    rec = Reconciler(live_settings(tmp_path, model_discovery_ttl_seconds=3600))
    hits: list[str] = []

    def _h(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        body = ({"data": [{"id": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                           "modality": "text->text"}]}
                if NEBIUS_HOST in request.url.host else
               {"models": [{"id": CONFIGURED_VISION, "type": "image_text"}]})
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(_h)
    await rec.refresh_models(transport=transport)
    first = len(hits)
    await rec.refresh_models(transport=transport)

    assert len(hits) == first, "a second run should not re-query within the TTL"
    await rec.refresh_models(transport=transport, force=True)
    assert len(hits) > first, "force must actually bypass the TTL"


@pytest.mark.asyncio
async def test_an_unexpected_failure_cannot_sink_the_run(tmp_path):
    """Discovery is advisory. Whatever it hits, the configured model stands.

    Not hypothetical: a host changing its payload shape raises something this
    module has never seen, and a startup check that can abort a shipment is
    worse than no check at all.
    """
    rec = Reconciler(live_settings(tmp_path))

    def _h(request: httpx.Request) -> httpx.Response:
        raise TypeError("unmapped payload shape")

    await rec.refresh_models(transport=httpx.MockTransport(_h))

    assert rec.client.endpoint.model == CONFIGURED_VISION
    selections = {s["role"]: s for s in rec.model_selections()}
    assert selections["vision"]["status"] == "unverified"
    assert "TypeError" in selections["vision"]["note"]


@pytest.mark.asyncio
async def test_the_reconciler_can_report_selections_without_any_fetch(tmp_path):
    rec = Reconciler(live_settings(tmp_path))
    assert rec.model_selections() == []
