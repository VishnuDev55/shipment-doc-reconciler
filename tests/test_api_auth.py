"""The API-key gate: what it refuses, what it must never leak, and what it must
not forget.

Zero network calls: every request goes through TestClient against the app with a
fake key configured. The point of these tests is not that 401 exists -- it is
that a route added next month cannot quietly ship unprotected, and that a
rejection never writes somebody's secret into a body, a header or a log.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, configure
from app.orchestrator import Reconciler

REPO = Path(__file__).resolve().parents[1]
KEY_A = "sdr-alpha-key-0000000000"
KEY_B = "sdr-bravo-key-1111111111"


def client_for(monkeypatch, api_keys: str) -> TestClient:
    monkeypatch.setenv("API_KEYS", api_keys)
    settings = Settings(
        _env_file=None, demo_mode=True, api_keys=api_keys,
        nvidia_api_key="nvapi-TEST-ONLY-0000-1234",
        nebius_api_key="sk-test-nebius-0000-9876",
        runs_dir=REPO / "runs", cache_dir=REPO / "fixtures" / "responses",
    )
    configure(Reconciler(settings))
    # One client per call: cookies must not drift between tests and pretend a
    # session opened here is still open there.
    return TestClient(app)


@pytest.fixture()
def open_client(monkeypatch):
    return client_for(monkeypatch, "")


@pytest.fixture()
def guarded(monkeypatch):
    return client_for(monkeypatch, f"{KEY_A}, {KEY_B}")


# ---------------------------------------------------------------------------
# the default stays open, on purpose
# ---------------------------------------------------------------------------


def test_an_open_deployment_needs_no_key(open_client):
    assert open_client.get("/api/models").status_code == 200
    assert open_client.get("/api/fixtures").status_code == 200


def test_open_mode_reports_itself_as_unprotected(open_client):
    assert open_client.get("/api/health").json()["api_protected"] is False


def test_protected_mode_says_so_on_the_open_endpoint(guarded):
    """The UI decides whether to ask for a key from this flag, so health has to
    answer without one -- a bootstrap that needs the secret it is asking for is
    a locked loop."""
    body = guarded.get("/api/health").json()
    assert body["api_protected"] is True
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# refusal and acceptance
# ---------------------------------------------------------------------------


def test_no_credentials_is_refused(guarded):
    res = guarded.get("/api/models")
    assert res.status_code == 401
    assert "X-API-Key" in res.headers.get("www-authenticate", "")


def test_a_wrong_key_is_refused(guarded):
    assert guarded.get("/api/models", headers={"X-API-Key": "not-a-key"}).status_code == 401


@pytest.mark.parametrize("key", [KEY_A, KEY_B])
def test_any_configured_key_is_accepted(guarded, key):
    assert guarded.get("/api/models", headers={"X-API-Key": key}).status_code == 200


def test_bearer_is_accepted_too(guarded):
    """Machine clients arrive either way; only one of those is a browser
    convention, and neither should be a surprise to whoever reads the docs."""
    res = guarded.get("/api/models", headers={"Authorization": f"Bearer {KEY_B}"})
    assert res.status_code == 200


def test_a_bearer_token_that_is_not_a_key_is_refused(guarded):
    res = guarded.get("/api/models", headers={"Authorization": "Basic " + KEY_A})
    assert res.status_code == 401


def test_a_partial_key_is_refused(guarded):
    """Whitespace and truncation are the classic near-misses: keys are compared
    whole, not as prefixes."""
    assert guarded.get("/api/models", headers={"X-API-Key": KEY_A[:-2]}).status_code == 401


def test_the_refusal_never_echoes_a_secret(guarded):
    """A 401 body that repeats the caller's key puts that key into whatever log
    the client writes, which is how a secret becomes a breadcrumb."""
    supplied = "sdr-guessed-key-9999999999"
    res = guarded.get("/api/models", headers={"X-API-Key": supplied})
    text = res.text + str(res.headers)
    assert supplied not in text
    assert KEY_A not in text and KEY_B not in text


def test_health_stays_open_when_everything_else_is_not(guarded):
    """A load balancer is not a client with a secret."""
    assert guarded.get("/api/health").status_code == 200


# ---------------------------------------------------------------------------
# the session cookie the browser UI needs
# ---------------------------------------------------------------------------


def test_a_key_buys_a_session_and_the_key_is_then_unnecessary(guarded):
    res = guarded.post("/api/session", json={"key": KEY_A})
    assert res.status_code == 200
    assert res.json()["expires_in_seconds"] > 0

    # A fresh request from the same client, with no header at all.
    assert guarded.get("/api/models").status_code == 200


def test_the_session_cookie_is_httponly_and_strict(guarded):
    """Read the attribute off the Set-Cookie header itself: the cookie object's
    flags are a client-side interpretation, and this is a server contract."""
    res = guarded.post("/api/session", json={"key": KEY_A})
    header = res.headers.get("set-cookie", "").lower()
    assert "sdr_session=" in header
    assert "httponly" in header
    assert "samesite=strict" in header
    assert KEY_A not in header, "the session must be a nonce, not the secret"


def test_a_bad_key_opens_nothing(guarded):
    res = guarded.post("/api/session", json={"key": "wrong"})
    assert res.status_code == 401
    assert "sdr_session" not in guarded.cookies


def test_an_open_deployment_refuses_a_session_it_does_not_need(open_client):
    res = open_client.post("/api/session", json={"key": "anything"})
    assert res.status_code == 400


def test_closing_a_session_revokes_it(guarded):
    guarded.post("/api/session", json={"key": KEY_A})
    assert guarded.get("/api/models").status_code == 200
    guarded.delete("/api/session")
    assert guarded.get("/api/models").status_code == 401


# ---------------------------------------------------------------------------
# the guarantee that outlives this file
# ---------------------------------------------------------------------------

EXEMPT = {"/api/health", "/api/session"}


def route_checks(route) -> list:
    """The dependency callables attached to a route by its decorator.

    FastAPI stores decorator dependencies as `Depends(...)` objects on
    `route.dependencies` (attribute `dependency`), and as `Dependant` objects on
    `route.dependant.dependencies` (attribute `call`). Both shapes are read here
    because asserting on the wrong one silently reports every route as
    unprotected -- which is the one failure mode this file exists to catch.
    """
    found = []
    for dep in list(getattr(route, "dependencies", []) or []):
        target = getattr(dep, "dependency", None) or getattr(dep, "call", None)
        if target is not None:
            found.append(target)
    for dep in list(getattr(getattr(route, "dependant", None), "dependencies", []) or []):
        target = getattr(dep, "call", None) or getattr(dep, "dependency", None)
        if target is not None:
            found.append(target)
    return found


def data_routes():
    for route in app.routes:
        path = getattr(route, "path", "")
        if (path.startswith("/api/") or path.startswith("/page/")) and path not in EXEMPT:
            yield path, route


def ungated_data_routes():
    from app import main as main_module

    return sorted({path for path, route in data_routes()
                   if main_module.require_api_key not in route_checks(route)})


def test_every_data_route_is_gated():
    """The failure this guards is not a broken check but a missing one: a new
    route added next month, working fine in the open demo, and unprotected in
    front of other people's shipments."""
    assert not ungated_data_routes(), (
        f"routes serving data with no key check: {ungated_data_routes()}"
    )


def test_the_route_scan_is_not_vacuous():
    """A test that iterates nothing passes forever. Assert the scan saw the
    routes it claims to have protected."""
    paths = {path for path, _ in data_routes()}
    assert len(paths) >= 15, paths
    assert "/api/shipments/{shipment_id}/report.pdf" in paths
    assert "/page/{shipment_id}/{path:path}" in paths


def test_the_open_routes_are_only_the_two_that_must_be():
    from app import main as main_module

    open_routes = sorted({getattr(r, "path", "") for r in app.routes
                          if getattr(r, "path", "").startswith("/api/")
                          and main_module.require_api_key not in route_checks(r)})
    assert open_routes == ["/api/health", "/api/session"], open_routes


def test_the_documented_api_declares_the_scheme():
    """The docs are the contract. If /docs shows no way to authenticate, the
    first thing a caller does is send a key in a query string, and query strings
    end up in access logs."""
    spec = app.openapi()
    schemes = spec["components"]["securitySchemes"]
    assert "APIKeyHeader" in schemes
    assert schemes["APIKeyHeader"]["name"] == "X-API-Key"
    assert "Bearer" in schemes["APIKeyHeader"]["description"]
    assert spec["paths"]["/api/models"]["get"].get("security")
    assert not spec["paths"]["/api/health"]["get"].get("security")


def test_a_key_in_the_query_string_is_not_accepted(guarded):
    """Deliberately unsupported. A URL is logged by proxies, kept in browser
    history and leaked in Referer headers, so a key that travels there is a key
    that has already leaked -- even though it would have been the easiest way to
    make the download links work."""
    assert guarded.get(f"/api/models?api_key={KEY_A}").status_code == 401
    assert guarded.get(f"/api/models?key={KEY_A}").status_code == 401


# ---------------------------------------------------------------------------
# a header is bytes, and bytes are not always ASCII
# ---------------------------------------------------------------------------


def _asgi_request(headers: list[tuple[bytes, bytes]]):
    """The request as the server actually hands it over: raw header bytes.

    Starlette decodes those as latin-1, so a client that sends UTF-8 in a header
    produces a `str` with non-ASCII characters in it -- which is a value httpx
    will not build, and therefore the only way to reach this path honestly.
    """
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/api/models",
                    "headers": headers})


def test_a_non_ascii_api_key_header_is_refused_rather_than_crashing(guarded):
    """Break caught: `hmac.compare_digest` raises TypeError on a non-ASCII str,
    so a request with an accented byte in `X-API-Key` produced a 500 from inside
    the auth check -- before anything was authenticated. That is a traceback for
    a request anybody can send, and it distinguishes "almost right" from "wrong"
    if the exception's shape varies. It has to be the same 401 as any other bad
    key.
    """
    from fastapi import HTTPException

    from app.auth import require_api_key

    request = _asgi_request([(b"x-api-key", "kлüч".encode())])
    # FastAPI resolves the header dependency and passes the decoded value, so a
    # direct call has to hand over the same thing rather than the Depends object.
    with pytest.raises(HTTPException) as exc:
        require_api_key(request, supplied=request.headers.get("x-api-key"))
    assert exc.value.status_code == 401


def test_a_non_ascii_bearer_token_is_refused_rather_than_crashing(guarded):
    """The Authorization header takes the same path and had the same crash."""
    from fastapi import HTTPException

    from app.auth import require_api_key

    request = _asgi_request([(b"authorization", b"Bearer " + "kлüч".encode())])
    with pytest.raises(HTTPException) as exc:
        require_api_key(request, supplied=None)
    assert exc.value.status_code == 401


def test_a_non_ascii_key_in_the_session_body_is_a_401(guarded):
    """The browser path: the key arrives as JSON, which will happily carry any
    Unicode the caller likes, and reaches the same constant-time comparison."""
    res = guarded.post("/api/session", json={"key": "kлüч-not-a-key"})
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# health stays open, but stops describing the deployment to strangers
# ---------------------------------------------------------------------------


def test_health_tells_a_keyed_deployment_less_to_a_stranger(guarded):
    """Break caught: /api/health answered with the version, the commit, both
    provider hostnames, the model ids and the retention window of a keyed
    instance, to anyone who asked. A load balancer needs `status`; a UI needs to
    know a key is required. Neither needs the model ids, and together they are a
    description of what to target and what to imitate.
    """
    body = guarded.get("/api/health").json()

    assert body["status"] == "ok", "the health check must still answer 200"
    assert body["api_protected"] is True, "the UI reads this to ask for a key"
    for leaked in ("version", "git_commit", "model", "base_url",
                   "vision", "summary", "run_ttl_hours"):
        assert leaked not in body, f"{leaked} is deployment detail, not health"


def test_health_still_describes_the_instance_to_a_key_holder(guarded):
    """The gate must not hide the detail from the operator's own monitoring, or
    the fix turns a fingerprint into an outage."""
    body = guarded.get("/api/health", headers={"X-API-Key": KEY_A}).json()

    assert body["status"] == "ok"
    assert "version" in body
    assert body["vision"]["provider"]


def test_an_open_instance_keeps_describing_itself(open_client):
    """Open is a deliberate configuration (the keyless demo). Nothing is being
    protected there, and the page relies on the detail to explain the mode."""
    body = open_client.get("/api/health").json()
    assert body["api_protected"] is False
    assert "version" in body
