# 0006 — Goods-line matching is exact, never fuzzy

**Status:** accepted

## Context

The party-name comparator uses `token_set_ratio` bands, and it works: legal entity
names vary by ordering, suffix and spelling. The same tool is sitting right there
for matching descriptions of goods, where the variance looks similar.

## Decision

No similarity scoring anywhere in `app/lineitems.py`. Two rows are the same row when
they carry the same SKU, or when both carry none and their descriptions fold to the
same token set — case, accents, punctuation, word order and package words removed,
every number kept. Quantities compare exactly; a differing package basis is its own
status rather than a conflict.

## Consequences

- `CNC aluminium housing H-40` and `... H-41` are two rows. A band that scored them
  at 0.95 would have merged them and deleted the only finding in the shipment —
  which is the specific error this feature exists to catch.
- The accepted cost: `ALUMINIUM` and `ALUMINUM` fold apart and appear as two
  uncheckable rows. There is a test asserting that, and the reasoning is that two
  rows a person can reconcile beats one row that quietly merged.
- Package words (`CTN`, `PCS`, `PALLET`, `SAY`, …) are stripped from the fold but
  numbers are never, so `100 CTN CNC HOUSING` and `CNC HOUSING (100 CARTONS)` match
  while `H-40`/`H-41` do not.
- The fold is sorted and de-duplicated, because a wrapped cell and a single-line
  cell list the same words in different orders.
- A document that returned no goods lines is named as *not read*, never as "does not
  list the item". Saying the latter would be a claim about contents nobody looked
  at, and it was in the code before a test caught it.
