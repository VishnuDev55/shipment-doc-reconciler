# 0004 — The filesystem is the database

**Status:** accepted

## Context

A reconciliation run produces a report, page images, raw model bodies and export
artifacts. A database would give queries, transactions and concurrency control.

## Decision

`runs/{shipment_id}/` holds everything: `report.json`, the normalized page JPEGs,
`raw/` model responses, exports. No database, no ORM, no migration.

## Consequences

- A run is a document about documents, and a directory a person can `ls`, `tar` and
  email is a better match for it than a row.
- Debugging is reading a file. When a report is wrong, `runs/{id}/raw/` contains
  exactly what the model said, unedited.
- Concurrency is handled by holding the report in memory and writing after each
  document settles, with an LRU bound on how many stay resident
  (`MAX_REPORTS_IN_MEMORY`) and an optional age-based purge (`RUN_TTL_HOURS`).
- No cross-run queries. History is a directory listing.
- Not multi-tenant. There is no isolation between run directories, so a hosted
  deployment needs per-tenant roots — recorded in `docs/SECURITY.md`, not hidden.
