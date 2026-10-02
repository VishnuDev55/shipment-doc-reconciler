"""Mechanical enforcement of the suite's zero-network-call guarantee.

The suite has always been offline, but only by convention: tests either build
Settings(demo_mode=True) or inject a fake into ChatClient._client. Nothing
prevented a future test from constructing a real client with a real-looking key
and spending someone's credits in CI. This fixture makes that impossible.

See tests/test_offline_guard.py for the self-verifying half of the guarantee.
"""

from __future__ import annotations

import socket

import pytest

# Loopback is allowed so a test can bind an ephemeral port if it ever needs to.
# Nothing else can be reached, resolved, or dialed.
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", ""}
_LOOPBACK_PREFIX = "127."


def _refuse(what: str, target: object) -> None:
    raise AssertionError(
        f"{what}: refused to reach {target!r}. This test suite makes zero "
        "network calls: use demo_mode with the fixtures cache, or inject a "
        "fake client. See tests/conftest.py."
    )


def _check_address(what: str, address: object) -> None:
    # AF_UNIX takes a filesystem path; it never leaves the machine.
    if isinstance(address, str):
        return
    if not isinstance(address, tuple) or not address:
        return
    host = str(address[0])
    if host in _LOOPBACK_NAMES or host.startswith(_LOOPBACK_PREFIX):
        return
    _refuse(what, address)


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_create_connection = socket.create_connection
_getaddrinfo = socket.getaddrinfo
_gethostbyname = socket.gethostbyname


def _guarded_connect(self, address, *args, **kwargs):
    _check_address("socket.connect", address)
    return _connect(self, address, *args, **kwargs)


def _guarded_connect_ex(self, address, *args, **kwargs):
    _check_address("socket.connect_ex", address)
    return _connect_ex(self, address, *args, **kwargs)


def _guarded_create_connection(address, *args, **kwargs):
    _check_address("socket.create_connection", address)
    return _create_connection(address, *args, **kwargs)


def _guarded_getaddrinfo(host, *args, **kwargs):
    text = "" if host is None else str(host)
    if text in _LOOPBACK_NAMES or text.startswith(_LOOPBACK_PREFIX):
        return _getaddrinfo(host, *args, **kwargs)
    _refuse("socket.getaddrinfo", host)


def _guarded_gethostbyname(host):
    text = "" if host is None else str(host)
    if text in _LOOPBACK_NAMES or text.startswith(_LOOPBACK_PREFIX):
        return _gethostbyname(host)
    _refuse("socket.gethostbyname", host)


@pytest.fixture(autouse=True, scope="session")
def no_network() -> object:
    """Patch the socket layer for the whole session, then restore it."""
    # pytest.MonkeyPatch rather than the monkeypatch fixture: the built-in one
    # is function-scoped and cannot be requested by a session-scoped fixture.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", _guarded_gethostbyname)
    yield True
    monkeypatch.undo()
