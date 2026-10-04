# 0005 — The cache key includes the contract version

**Status:** accepted, extended 2026-10-04

## Context

Responses are cached on `sha256(prompt_version, provider, model, page_hashes)` so a
re-encoded upload of the same page still hits, and so iterating on normalization
costs nothing. Hand-authored fixtures are also matched on page hashes alone — that
is what lets `DEMO_MODE` work under any model id.

That second tier turned out to be a collision waiting to happen.

## Decision

Keep the two-tier lookup, and filter the authored tier on the contract version.

When the opt-in goods-lines read landed, it reused the same page images with a
different output shape. Its cache key differs (it carries `lineitems-v1`), but the
authored fallback ignored the key and matched on page hashes only — so a goods-lines
fixture would have answered a ten-field lookup for the same page. It would not have
raised: the shape parses, every field comes back `present: false`. The report would
have read as "this document states nothing".

`ResponseCache.put` now takes a `prompt_version` (defaulting to the scalar one, so
existing callers are unchanged) and `_find_authored` filters on it.

## Consequences

- A cache entry can never be silently reconciled against a contract it predates.
- Bumping `PROMPT_VERSION` invalidates exact-key hits and, now, the authored tier
  too — which is correct and is the reason `scripts/migrate_cache_keys.py` exists
  and why it carries a prompt-version guard plus an authored-fixture skip.
- Any third contract (a table read, a signature block) must add its own version tag
  rather than reusing `extract-vN`.

The bug was found by reasoning about what the fallback tier does *not* look at, and
pinned by a test that asserts a goods-lines fixture cannot answer a scalar lookup.
