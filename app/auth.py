"""API-key and session-cookie authentication.

The one dependency every data-bearing route depends on, plus the short-lived
in-process session that lets the browser UI hold a key without putting it in
every request's headers. Nothing here echoes or logs a key: a 401 body that
repeats a caller's key would put that key in somebody's access log.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

API_KEY_HEADER = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Required when the deployment sets API_KEYS. Also accepted as "
                "`Authorization: Bearer <key>`.",
)

# Long enough to survive a demo and a working session, short enough that losing
# the process invalidating it is not an outage. In-process by design: this app
# is one worker with a filesystem store, and a cookie that outlived a restart
# would be a secret with no way to revoke it.
SESSION_TTL_SECONDS = 12 * 60 * 60
_sessions: dict[str, float] = {}


def _issue_session() -> str:
    import secrets
    import time

    now = time.time()
    for token in [t for t, expires in _sessions.items() if expires <= now]:
        _sessions.pop(token, None)
    token = secrets.token_urlsafe(32)
    _sessions[token] = now + SESSION_TTL_SECONDS
    return token


def _session_valid(request: Request) -> bool:
    import time

    token = request.cookies.get("sdr_session") or ""
    expires = _sessions.get(token)
    if not expires:
        return False
    if expires < time.time():
        _sessions.pop(token, None)
        return False
    return True


def secret_matches(supplied: str, expected: str) -> bool:
    """Constant-time equality for two secrets, whatever characters they carry.

    `hmac.compare_digest` raises TypeError on a non-ASCII str. A header is decoded
    from raw bytes, and a JSON body can carry any Unicode at all, so a request with
    an accented character in it turned this check into an unhandled 500 -- from
    inside authentication, before anything had been authenticated. That is a
    traceback anyone can provoke.

    Comparing UTF-8 bytes uses the same C routine, so the comparison stays
    constant-time and "nearly right" is still indistinguishable from "wrong".
    `surrogatepass` because a JSON string can carry a lone surrogate, which plain
    UTF-8 encoding refuses.
    """
    return hmac.compare_digest(
        supplied.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def key_is_valid(request: Request, supplied: str | None = None) -> bool:
    """Whether this request carries a configured key, or a live session.

    The same test `require_api_key` applies, exposed for an endpoint that stays
    open but answers a stranger differently from an operator -- /api/health is the
    only one. An open deployment returns True for everyone: there is nothing to
    check against, and that is the documented default.
    """
    from .main import active_settings  # local import: main imports this module

    allowed = active_settings().api_key_list
    if not allowed:
        return True
    if _session_valid(request):
        return True

    if supplied is None:
        # FastAPI hands `require_api_key` the resolved header; a caller with only
        # a Request still gets the same answer.
        supplied = request.headers.get("x-api-key")
    candidates = [supplied] if supplied else []
    bearer = (request.headers.get("authorization") or "").strip()
    if bearer[:7].lower() == "bearer ":
        candidates.append(bearer[7:].strip())

    for given in (c.strip() for c in candidates if c and c.strip()):
        if any(secret_matches(given, key) for key in allowed):
            return True
    return False


def require_api_key(request: Request,
                    supplied: str | None = Depends(API_KEY_HEADER)) -> None:
    """Accept the request only if it carries a configured key or a live session.

    An empty API_KEYS means an open deployment, which is the documented default
    for the keyless demo, so this returns rather than refusing. When keys exist
    the comparison is constant-time against each of them, and neither the value
    supplied nor the value expected is echoed back or logged: a 401 body that
    repeats a caller's key would put that key in somebody's access log.
    """
    if key_is_valid(request, supplied):
        return
    raise HTTPException(
        status_code=401,
        detail="this deployment requires an API key: send X-API-Key or "
               "Authorization: Bearer, or POST the key to /api/session",
        headers={"WWW-Authenticate": "X-API-Key"},
    )
