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


def require_api_key(request: Request,
                    supplied: str | None = Depends(API_KEY_HEADER)) -> None:
    """Accept the request only if it carries a configured key or a live session.

    An empty API_KEYS means an open deployment, which is the documented default
    for the keyless demo, so this returns rather than refusing. When keys exist
    the comparison is constant-time against each of them, and neither the value
    supplied nor the value expected is echoed back or logged: a 401 body that
    repeats a caller's key would put that key in somebody's access log.
    """
    from .main import active_settings  # local import: main imports this module

    allowed = active_settings().api_key_list
    if not allowed:
        return

    if _session_valid(request):
        return

    candidates = [supplied] if supplied else []
    bearer = (request.headers.get("authorization") or "").strip()
    if bearer[:7].lower() == "bearer ":
        candidates.append(bearer[7:].strip())

    for given in (c.strip() for c in candidates if c and c.strip()):
        if any(hmac.compare_digest(given, key) for key in allowed):
            return
    raise HTTPException(
        status_code=401,
        detail="this deployment requires an API key: send X-API-Key or "
               "Authorization: Bearer, or POST the key to /api/session",
        headers={"WWW-Authenticate": "X-API-Key"},
    )
