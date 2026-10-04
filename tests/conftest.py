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

# No host, no address, no port -- with one precisely bounded exception, and the
# reason it is bounded is a real incident.
#
# Loopback used to be exempt outright. Measured on the development machine on
# 2026-10-03: a process listening on 127.0.0.1 was forwarding straight to the
# public internet, so a live-mode ChatClient built inside the guarded suite
# completed a genuine HTTPS request to integrate.api.nvidia.com and got HTTP 200
# back -- and never called socket.getaddrinfo on the way, because it only ever
# connected to 127.0.0.1. The guard saw a loopback address, waved it through, and
# the guarantee the whole suite rests on was false while looking true.
#
# Loopback cannot simply be refused either: Starlette's TestClient starts an
# anyio blocking portal, and on Windows that portal is a loopback socket pair,
# so refusing it takes out every API test in the repo. The distinction that
# matters is not the address but who opened it, so the guard records every port
# this process binds and permits a connection only to one of those. An external
# listener on 127.0.0.1:2080 is then refused on the same grounds as a remote
# host, while a socket this test created for itself keeps working.


def _refuse(what: str, target: object) -> None:
    raise AssertionError(
        f"{what}: refused to reach {target!r}. This test suite makes zero "
        "network calls: use demo_mode with the fixtures cache, or inject a "
        "fake client. Loopback is refused unless this process bound that port "
        "itself: a local listener was measured forwarding provider traffic "
        "straight to the internet. See tests/conftest.py."
    )


_self_bound_ports: set[int] = set()
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", ""}


def _loopback_port(address: object) -> int | None:
    """The port of a loopback address, or None if it is not loopback at all."""
    if not isinstance(address, tuple) or len(address) < 2:
        return None
    host = str(address[0])
    if host not in _LOOPBACK and not host.startswith("127."):
        return None
    try:
        return int(address[1])
    except (TypeError, ValueError):
        return None


def _check_address(what: str, address: object) -> None:
    # A filesystem path (AF_UNIX) cannot leave the machine, so it stays
    # permitted. Loopback is permitted only for a port this process bound
    # itself; everything else -- any remote host, and any local listener it
    # did not create -- is refused.
    if isinstance(address, str):
        return
    port = _loopback_port(address)
    if port is not None and port in _self_bound_ports:
        return
    _refuse(what, address)


_bind = socket.socket.bind
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_create_connection = socket.create_connection
_getaddrinfo = socket.getaddrinfo
_gethostbyname = socket.gethostbyname


def _guarded_bind(self, address, *args, **kwargs):
    """Remember the loopback port this socket is about to own.

    Port 0 means "pick one", so the number is read back from getsockname() after
    the bind succeeds -- recording it before would store 0 and authorize
    nothing.
    """
    result = _bind(self, address, *args, **kwargs)
    if _loopback_port(address) is not None:
        try:
            _self_bound_ports.add(int(self.getsockname()[1]))
        except OSError:
            pass
    return result


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
    # A loopback *name* is allowed to resolve: turning "localhost" into
    # 127.0.0.1 puts nothing on a wire, asyncio does it for internal plumbing,
    # and refusing it would make a blocked test indistinguishable from a broken
    # guard. What is refused is resolution of anything else, because the only
    # reason to look up a remote name is to reach it. The address is where the
    # real gate lives -- see _check_address.
    text = "" if host is None else str(host)
    if text in _LOOPBACK_NAMES or text.startswith("127."):
        return _getaddrinfo(host, *args, **kwargs)
    _refuse("socket.getaddrinfo", host)


def _guarded_gethostbyname(host):
    text = "" if host is None else str(host)
    if text in _LOOPBACK_NAMES or text.startswith("127."):
        return _gethostbyname(host)
    _refuse("socket.gethostbyname", host)


@pytest.fixture(autouse=True, scope="session")
def no_network() -> object:
    """Patch the socket layer for the whole session, then restore it."""
    # pytest.MonkeyPatch rather than the monkeypatch fixture: the built-in one
    # is function-scoped and cannot be requested by a session-scoped fixture.
    monkeypatch = pytest.MonkeyPatch()
    # A proxy discovered from the environment would be a second way out, so it
    # is removed rather than relied upon not to be set.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WSS_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "wss_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "bind", _guarded_bind)
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", _guarded_gethostbyname)
    yield True
    monkeypatch.undo()
