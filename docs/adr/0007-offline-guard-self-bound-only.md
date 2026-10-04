# 0007 — The offline guard permits only ports this process bound

**Status:** accepted

## Context

The suite has always been offline — by convention. The convention was made a
mechanism by patching `socket` for the session. The first version allowed loopback,
"so a test can bind an ephemeral port if it needs to".

That allowance was false. Something on the development machine listens on
`127.0.0.1` and forwards to the internet. A real HTTPS request to a provider
completed **inside the guarded suite**: it never resolved a hostname through
`getaddrinfo` and never connected to a non-loopback address, so the wall was waved
through while the guarantee was not held.

## Decision

Refuse everything except AF_UNIX and loopback ports **this process opened**. The
guard wraps `bind()` and records each ephemeral port from `getsockname()`; a connect
to a loopback port is permitted only if it is in that set. Resolving a loopback
*name* is still allowed — it puts nothing on a wire, and asyncio does it for its own
plumbing.

## Consequences

- An external listener on loopback is refused on the same grounds as a remote host,
  which is the property the first version lacked.
- The naive fix — refuse loopback outright — broke every API test, because
  Starlette's `TestClient` starts an anyio blocking portal that *is* a loopback
  socket pair on Windows. The distinction that matters is not the address but who
  opened it.
- The guarantee is self-verifying: `tests/test_offline_guard.py` asserts the guard
  fires for each patched entry point, so a refactor that silently un-patches it
  fails a test rather than quietly restoring the hole.
- Browser tests that genuinely need sockets live in `tests_e2e/`, outside
  `testpaths`, and skip loudly if the guard is ever active in their session.
