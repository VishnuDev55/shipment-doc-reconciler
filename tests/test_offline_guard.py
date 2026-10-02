"""Proves the offline guard in conftest.py actually fires.

Without this file the guard is load-bearing but unverified: a refactor could
stop patching something and the suite would go quietly back to being offline
"by convention". These tests assert the wall exists, and that it does not
extend to loopback, which in-process transport depends on.
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app

# The real provider hosts, read from the shipped defaults rather than hardcoded
# here, so the assertion tracks configuration instead of a remembered string.
settings = Settings(_env_file=None)
PROVIDER_HOSTS = sorted(
    {
        urlparse(settings.nvidia_base_url).hostname,
        urlparse(settings.nebius_base_url).hostname,
    }
    - {None}
)


@pytest.mark.parametrize("host", PROVIDER_HOSTS)
def test_provider_hosts_cannot_be_resolved(host):
    with pytest.raises(AssertionError, match="zero"):
        socket.getaddrinfo(host, 443)


@pytest.mark.parametrize("host", PROVIDER_HOSTS)
def test_provider_hosts_cannot_be_dialed(host):
    with pytest.raises(AssertionError, match="zero"):
        socket.create_connection((host, 443), timeout=1)


def test_tcp_connect_to_a_public_address_is_refused():
    with pytest.raises(AssertionError, match="zero"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("1.1.1.1", 443))


def test_connect_ex_is_refused_too():
    # Some libraries use connect_ex, which returns an errno instead of raising.
    # Guarding only connect() would leave that door open.
    with pytest.raises(AssertionError, match="zero"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect_ex(("8.8.8.8", 53))


def test_gethostbyname_is_refused():
    with pytest.raises(AssertionError, match="zero"):
        socket.gethostbyname("example.com")


def test_loopback_resolution_is_still_allowed():
    # The guard must not over-block: refusing loopback would break binding an
    # ephemeral port, and makes the failure mode indistinguishable from a bug.
    assert socket.getaddrinfo("127.0.0.1", 0)
    assert socket.getaddrinfo("localhost", 0)


def test_loopback_connection_still_works():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect(server.getsockname())
        peer, _ = server.accept()
        client.sendall(b"local")
        assert peer.recv(8) == b"local"
        client.close()
        peer.close()
    finally:
        server.close()


async def test_a_live_mode_client_cannot_escape_to_the_network():
    """The exact scenario the convention-only guarantee could not prevent.

    Live mode, a key that passes the placeholder check, a real client object
    pointed at a real provider URL: the request has to die on the socket wall,
    not on a credit balance. max_attempts=1 so a regression that somehow got
    past the guard fails instead of backing off and retrying.
    """
    from app.client import ChatClient
    from app.providers import vision_endpoint

    live = Settings(
        _env_file=None,
        demo_mode=False,
        nvidia_api_key="nvapi-TEST-ONLY-0000-1234",
    )
    chat = ChatClient(vision_endpoint(live), live)
    with pytest.raises(AssertionError, match="zero"):
        await chat.transport_call(
            [{"role": "user", "content": "hi"}],
            response_format=None,
            max_attempts=1,
        )


def test_the_in_process_http_surface_still_works():
    """The whole suite drives FastAPI through TestClient; that must survive.

    httpx's ASGITransport never opens a socket, which is the only reason the
    guard is safe to apply session-wide. If a change ever moves these tests to
    a real server on a port, this fails loudly instead of silently skipping.
    """
    client = TestClient(app, base_url="http://testserver")
    assert client.get("/api/health").status_code == 200
