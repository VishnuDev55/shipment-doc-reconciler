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
    exists here and a caller cannot invent a different one to get a fresh bucket.
    Otherwise the peer address -- unless a trusted proxy is declared, in which
    case the first hop of `X-Forwarded-For`, since behind a proxy the peer is the
    proxy and every visitor would otherwise share one budget.

    The header is only read when `TRUSTED_PROXY` says a proxy you control sets
    it. Believing it unconditionally made the limit optional: any caller could
    rotate the value per request, and a shared key is one budget by design.
    """
    key = (request.headers.get("x-api-key") or "").strip()
    if not key:
        auth = (request.headers.get("authorization") or "").strip()
        if auth[:7].lower() == "bearer ":
            key = auth[7:].strip()
    if key:
        return "key:" + key[-12:]
    if _trusted_proxy():
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return "ip:" + forwarded.split(",")[0].strip()
    client = request.client
    return "ip:" + (client.host if client else "unknown")


def _trusted_proxy() -> bool:
    from .main import active_settings  # local import: main imports this module

    return bool(active_settings().trusted_proxy)
