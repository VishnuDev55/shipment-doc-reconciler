"""The optional shared secret on the config endpoints (docs/AUDIT.md R1).

Without it, POST /api/config lets anyone who can reach the port replace the
provider keys every other visitor uses. A token is the smallest thing that closes
that while keeping remote configuration possible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, configure
from app.orchestrator import Reconciler

REPO = Path(__file__).resolve().parents[1]

TOKEN = "local-config-token-abc123"
REAL_KEY = "nvapi-TEST-ONLY-0000-1234"


@pytest.fixture()
def make(tmp_path):
    """A client over a freshly configured reconciler."""
    def _make(**over):
        base = dict(
            _env_file=None,
            demo_mode=True,
            nvidia_api_key="",
            nebius_api_key="",
            runs_dir=tmp_path / "runs",
            cache_dir=REPO / "fixtures" / "responses",
        )
        base.update(over)
        configure(Reconciler(Settings(**base)))
        with TestClient(app) as client:
            return client
    return _make


def auth(token: str = TOKEN) -> dict:
    return {"X-Config-Token": token}


# ---------------------------------------------------------------------------
# unset token: nothing changes for the local single-user case
# ---------------------------------------------------------------------------


def test_no_token_configured_means_no_token_needed(make):
    client = make()
    body = client.get("/api/config").json()
    assert body["token_required"] is False
    assert client.get("/api/config").status_code == 200
    assert client.post("/api/config", json={"vision_provider": "nebius"}).status_code == 200


def test_a_blank_or_whitespace_token_counts_as_unset(make):
    for value in ("", "   ", "\t"):
        client = make(config_token=value)
        assert client.get("/api/config").json()["token_required"] is False
        assert client.get("/api/config").status_code == 200


# ---------------------------------------------------------------------------
# set token
# ---------------------------------------------------------------------------


def test_get_requires_the_token_once_one_is_set(make):
    client = make(config_token=TOKEN)
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/config", headers=auth()).status_code == 200
    assert client.get("/api/config", headers=auth("wrong")).status_code == 401


def test_post_requires_the_token_and_changes_nothing_without_it(make):
    client = make(config_token=TOKEN)
    refused = client.post("/api/config", json={"nvidia_api_key": REAL_KEY})
    assert refused.status_code == 401

    allowed = client.get("/api/config", headers=auth()).json()
    assert allowed["keys"]["nvidia"]["set"] is False, "an unauthenticated write must not apply"


def test_the_right_token_lets_the_write_through(make):
    client = make(config_token=TOKEN)
    res = client.post("/api/config", json={"nvidia_api_key": REAL_KEY}, headers=auth())
    assert res.status_code == 200
    assert res.json()["keys"]["nvidia"]["set"] is True


def test_a_token_never_appears_in_a_response_body(make):
    client = make(config_token=TOKEN)
    for response in (
        client.get("/api/config", headers=auth()),
        client.post("/api/config", json={"vision_provider": "nebius"}, headers=auth()),
        client.post("/api/config", json={"vision_provider": "bogus"}, headers=auth()),
        client.get("/api/config"),
    ):
        assert TOKEN not in json.dumps(response.json()), response.text


def test_a_disabled_deployment_still_reports_403_first(make):
    """The more actionable message wins: on a locked instance the fix is an env
    var, not a token field."""
    client = make(config_token=TOKEN, gui_config_enabled=False)
    res = client.post("/api/config", json={"vision_provider": "nebius"})
    assert res.status_code == 403
    assert "GUI_CONFIG_ENABLED" in res.json()["detail"]


def test_health_does_not_leak_or_require_a_token(make):
    """Health is the container's liveness probe; it cannot carry a secret and
    must not start answering 401."""
    client = make(config_token=TOKEN)
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert TOKEN not in json.dumps(body)


def test_a_non_ascii_config_token_is_a_401_not_a_traceback(make):
    """Break caught: `hmac.compare_digest` raises TypeError on a non-ASCII str,
    so a token header carrying an accented byte crashed the check instead of
    failing it. Same hole as the API-key path, reached through the other secret.
    """
    from starlette.requests import Request

    from app.config_view import _require_config_token
    from fastapi import HTTPException

    make(config_token=TOKEN)  # configures the reconciler the check reads
    request = Request({"type": "http", "method": "GET", "path": "/api/config",
                       "headers": [(b"x-config-token", "tоkü".encode())]})
    with pytest.raises(HTTPException) as exc:
        _require_config_token(request)
    assert exc.value.status_code == 401
