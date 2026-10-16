"""Tests for the runtime configuration the GUI applies.

These cover the two things that actually bite: a masked key must never leave
the server, and applying a change must reach the live clients rather than only
the settings object.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.client import ConfigError
from app.config import Settings
from app.main import app, configure, reconciler
from app.orchestrator import Reconciler

REPO = Path(__file__).resolve().parents[1]

# Deliberately dashed into short segments: a 40-character run after the prefix
# would look like live credential material to a secret scanner, and this repo
# states in SUBMISSION.md that a scan found none.
REAL_KEY = "nvapi-TEST-ONLY-0000-1234"
OTHER_KEY = "sk-test-nebius-0000-9876"


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        demo_mode=True,
        nvidia_api_key="",
        nebius_api_key="",
        runs_dir=tmp_path / "runs",
        cache_dir=REPO / "fixtures" / "responses",
    )
    configure(Reconciler(settings))
    with TestClient(app) as c:
        yield c


def post(client, payload):
    return client.post("/api/config", json=payload)


# ---------------------------------------------------------------------------
# reading configuration
# ---------------------------------------------------------------------------


def test_config_starts_empty(client):
    body = client.get("/api/config").json()
    assert body["demo_mode"] is True
    assert body["editable"] is True
    assert body["keys"]["nvidia"] == {"set": False, "masked": ""}
    assert body["keys"]["nebius"]["set"] is False


def test_full_key_never_leaves_the_server(client):
    assert post(client, {"nvidia_api_key": REAL_KEY}).status_code == 200

    body = client.get("/api/config").json()
    assert REAL_KEY not in json.dumps(body)
    assert body["keys"]["nvidia"]["set"] is True
    # Prefix and tail only: enough to tell two keys apart, not to use one.
    assert body["keys"]["nvidia"]["masked"] == "nvapi…1234"


# ---------------------------------------------------------------------------
# applying configuration
# ---------------------------------------------------------------------------


def test_applied_key_reaches_the_live_client(client):
    post(client, {"nvidia_api_key": REAL_KEY})

    rec = reconciler()
    assert rec.client.endpoint.api_key == REAL_KEY
    assert rec.settings.has_nvidia_key is True
    assert client.get("/api/health").json()["vision"]["key_present"] is True


def test_leaving_demo_mode_rebuilds_the_client(client):
    """json_mode and the probe flag are set at construction, so a settings-only
    mutation would leave the client behaving as if demo mode were still on."""
    post(client, {"nvidia_api_key": REAL_KEY})
    assert reconciler().client.json_mode == "cache"

    post(client, {"demo_mode": False})

    live = reconciler().client
    assert live.json_mode == "plain"
    assert live._probed is False
    assert live.endpoint.api_key == REAL_KEY
    assert client.get("/api/health").json()["demo_mode"] is False


def test_demo_mode_can_be_turned_back_on(client):
    post(client, {"nvidia_api_key": REAL_KEY, "demo_mode": False})
    post(client, {"demo_mode": True})

    assert reconciler().settings.demo_mode is True
    assert reconciler().client.json_mode == "cache"


def test_provider_switch_changes_both_endpoint_sides(client):
    post(client, {"nebius_api_key": OTHER_KEY, "vision_provider": "nebius"})

    rec = reconciler()
    assert rec.client.endpoint.provider == "nebius"
    assert rec.client.endpoint.api_key == OTHER_KEY
    assert rec.summary_client.endpoint.provider == "nebius"


def test_summary_model_override_resolves(client):
    post(client, {"nebius_summary_model": "some-other-model"})

    assert reconciler().summary_client.endpoint.model == "some-other-model"


def test_key_can_be_cleared(client):
    post(client, {"nvidia_api_key": REAL_KEY})
    assert post(client, {"nvidia_api_key": ""}).status_code == 200

    assert reconciler().settings.nvidia_api_key == ""
    assert client.get("/api/config").json()["keys"]["nvidia"]["masked"] == ""


def test_surrounding_whitespace_is_stripped(client):
    post(client, {"nvidia_api_key": f"  {REAL_KEY}  "})

    assert reconciler().settings.nvidia_api_key == REAL_KEY


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_echoed_mask_is_not_treated_as_a_key(client):
    """The form re-submits untouched fields. Re-sending the mask must not
    install it as the key, and must not count as a change either."""
    post(client, {"nvidia_api_key": REAL_KEY})
    masked = client.get("/api/config").json()["keys"]["nvidia"]["masked"]

    res = post(client, {"nvidia_api_key": masked})

    assert res.status_code == 400
    assert reconciler().settings.nvidia_api_key == REAL_KEY


def test_placeholder_key_rejected(client):
    res = post(client, {"nebius_api_key": "your-api-key-here"})

    assert res.status_code == 400
    assert "placeholder" in res.json()["detail"]
    assert reconciler().settings.nebius_api_key == ""


def test_nothing_to_change_is_an_error(client):
    assert post(client, {}).status_code == 400


def test_unknown_provider_rejected(client):
    assert post(client, {"vision_provider": "openai"}).status_code == 422


def test_runtime_config_can_be_switched_off(tmp_path):
    settings = Settings(
        _env_file=None,
        demo_mode=True,
        gui_config_enabled=False,
        runs_dir=tmp_path / "runs",
        cache_dir=REPO / "fixtures" / "responses",
    )
    configure(Reconciler(settings))
    with TestClient(app) as locked:
        assert locked.get("/api/config").status_code == 200

        res = locked.post("/api/config", json={"nvidia_api_key": REAL_KEY})

        assert res.status_code == 403
        assert "GUI_CONFIG_ENABLED" in res.json()["detail"]
        assert reconciler().settings.nvidia_api_key == ""


def test_paths_and_limits_are_not_runtime_configurable():
    """Re-reading runs_dir mid-run would strand a shipment's evidence, so the
    mechanism refuses it even though no HTTP field exposes it."""
    rec = Reconciler(Settings(_env_file=None, demo_mode=True))

    with pytest.raises(ValueError, match="runs_dir"):
        rec.reconfigure({"runs_dir": "/tmp/elsewhere"})


# ---------------------------------------------------------------------------
# the default is live
# ---------------------------------------------------------------------------


def test_demo_mode_is_off_unless_the_environment_asks_for_it():
    """Read the declared default rather than building Settings, which would
    also absorb whatever DEMO_MODE happens to be set to in the environment."""
    assert Settings.model_fields["demo_mode"].default is False


def test_live_mode_without_a_key_refuses_instead_of_calling_the_provider(client):
    """The failure the default flip exposes: no key must be a clear local
    ConfigError, not a signed request that comes back 401."""
    post(client, {"demo_mode": False})

    with pytest.raises(ConfigError, match="API_KEY"):
        reconciler().client.client


def test_demo_mode_still_works_as_an_environment_setting(client):
    """The machinery is inert, not deleted: DEMO_MODE=true must still replay the
    cache, which is how the keyless hosted demo keeps running."""
    post(client, {"demo_mode": True})
    res = client.get("/api/fixture/shipment_conflict")

    assert res.status_code == 201
    assert res.json()["demo_mode"] is True


def test_consensus_is_exposed_and_toggleable(client):
    """The second-reader switch in the drawer writes through to the settings the
    orchestrator reads, so a ticked box actually changes what a run does."""
    body = client.get("/api/config").json()
    assert body["consensus"] is False  # off by default

    res = post(client, {"consensus": True})
    assert res.status_code == 200
    assert res.json()["consensus"] is True
    assert reconciler().settings.consensus is True

    res = post(client, {"consensus": False})
    assert res.status_code == 200
    assert reconciler().settings.consensus is False
