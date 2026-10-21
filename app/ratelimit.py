"""Rate limiting for the one endpoint that spends money."""

from __future__ import annotations

from fastapi import Request

from .limits import RateLimiter

# Budget for the one endpoint that spends money. Owned per process, and reset
# whenever a new reconciler is injected, so an unrelated test cannot be the
# reason a 429 appeared.
_limiter = RateLimiter()


def _limiter_for() -> RateLimiter:
    from .main import active_settings  # local import: main imports this module

    limit = active_settings().run_limit_per_minute
    if _limiter.limit != limit:
        _limiter.limit = limit
        _limiter.reset()
    return _limiter


def _client_key(request: Request) -> str:
    """Who to charge the budget to.

    The API key when one was supplied, because that is the only identity that
    exists here; otherwise the peer address, taking the first hop of
    X-Forwarded-For since a proxy is the normal way to reach this app. A shared
    key is one budget by design -- if it is being shared, that is the deployment
    problem to fix, not something to hide here.
    """
    key = (request.headers.get("x-api-key") or "").strip()
    if not key:
        auth = (request.headers.get("authorization") or "").strip()
        if auth[:7].lower() == "bearer ":
            key = auth[7:].strip()
    if key:
        return "key:" + key[-12:]
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return "ip:" + forwarded.split(",")[0].strip()
    client = request.client
    return "ip:" + (client.host if client else "unknown")
