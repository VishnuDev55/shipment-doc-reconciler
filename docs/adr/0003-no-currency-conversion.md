# 0003 — Never convert currencies

**Status:** accepted

## Context

Two documents stating `USD 8,420.00` and `EUR 7,600.00` look like a conflict. The
tempting fix is a conversion so they agree.

## Decision

Different currencies are always a hard conflict, with the reason "cannot compare
across currencies". No rate lookup, no conversion, no FX table in the repo.

## Consequences

- A network dependency, a rate-freshness problem and an unauditable multiplier never
  enter a report whose entire value is auditability.
- Some real shipments are reported as conflicting when the underlying values agree
  after conversion. That is the correct outcome for this tool: the documents do not
  agree, and reconciling them is a human decision with a rate the human chooses and
  can defend.
- The rule is cheap enough that it costs nothing to keep, which is not true of the
  alternative.
