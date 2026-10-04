# Normalization and comparison rules

Every rule that turns a printed string into something comparable, and every
threshold that turns a difference into a verdict. All of it is deterministic pure
Python — no model is consulted anywhere in this file's subject matter, and no
value here is computed by a model.

The reason this document exists: a reconciliation tool is only as good as its
silences. Every rule below either resolves a variance *and says so*, or refuses to
resolve it and warns. Nothing quietly normalizes a fact into a different fact.

---

## 1. Numbers first, because everything else depends on them

`app/normalize/number.py`

The same quantity is printed as `12,450.00`, `12.450,00`, `12 450,00`, `12'450.00`
and `12,450` depending on who made the document. A silent misparse corrupts every
comparison downstream, so ambiguity is resolved **and flagged**:

| Printed | Parsed | Flagged |
|---|---|---|
| `12,450.00` | 12450.00 | — |
| `12.450,00` | 12450.00 | — (a trailing `,dd` group settles the convention) |
| `12 450,00` | 12450.00 | `strip_grouping`; NBSP and thin space are included |
| `12'450.00` | 12450.00 | `strip_grouping` (Swiss convention) |
| `12,450` | 12450.0 | **ambiguous** — `assume_thousands_grouping`, with a warning naming the alternative |
| `1.234` | 1234.0 | **ambiguous** — same rule, same warning |

The last two rows are the honest ones. Where the convention cannot be determined
from the string alone, the value is parsed to the thousands reading **and** carries
a warning that reaches the report. The engine does not pretend to know. Fed into
`normalize_weight` without a unit, the same string fails outright rather than
guessing twice: `ok=False`, with both the separator warning and "no weight unit
found".

## 2. Weight

`app/normalize/weight.py` — canonical unit is kg.

Recognized, with their factors: `kg kgs kgm kilo(s) kilogram(s)` (1.0),
`g gram(s)` (0.001), `mt m/t tonne(s) metric ton(s)` (1000), `lb lbs lbm pound(s)`
and `#` (0.45359237).

`ton / tons / t` are in `AMBIGUOUS_UNITS`, and only the metric factor is
implemented: `12 TON` becomes 12,000 kg with the warning

> `'TON' is ambiguous (metric tonne vs short ton); assumed metric tonne (1000 kg)`

A document that meant US short tons is therefore wrong by ~9 %, and the warning is
the only defense against it. That is a deliberate limit, not an oversight: picking
between three factors needs context the string does not contain, and a silent
choice would be a fabricated number on a customs document.

`#` is a legitimate pounds marking on a weight line (`NET 5#` → 2.268 kg). The
unit is matched unless it is glued to a letter, so `KGS` is not read as `#` +
noise — but that also means a stray `ITEM #12` reaching a *weight* field would
read as 12 pounds. It does not happen in practice because the field the model
attributed to weight is the only text normalized this way; it is worth knowing if
that attribution is ever wrong, which is what the confidence flag is for.

## 3. Quantity: count and package basis stay separate

`app/normalize/quantity.py` returns `(count, unit)` as two fields, and the
comparator treats them separately.

`1,200 pieces` versus `100 cartons` is **not a conflict**. One counts items, the
other counts packaging. This is the single most common legitimate difference in
real trade documents, and collapsing it would make the tool cry wolf on every
shipment. The status is `suspicious` with a reason naming both bases, so a human
sees the two readings rather than a verdict about them.

## 4. Money: never convert

`app/normalize/money.py`

- Amount and currency are parsed separately; `USD 8,420.00` → `(8420.00, USD)`.
- **Different currencies are always a hard conflict**, with the reason "cannot
  compare across currencies". No rate lookup, no FX conversion. A conversion would
  add a network dependency, a rate-freshness problem and a number nobody can audit,
  in exchange for turning a correct "these do not match" into a plausible lie.
- `SAY ... ONLY` spelled-out amount lines are stripped before parsing, because
  they repeat the figure — in words and sometimes in digits — and the repeat is not
  a second opinion.
- Three-letter runs that are not currencies (`FOB CIF CFR EXW FCA FAS DAP DDP DPU
  CPT CIP SAY ONLY TOTAL AMT VAT GST TAX NET GRS KGS LBS MTQ PCQ ...`) are excluded
  from ISO 4217 matching, since a naive `[A-Z]{3}` scan reads `FOB` as a currency.

## 5. Container numbers: the one field that grades itself

`app/normalize/container.py` implements ISO 6346 in full. The 11th character is a
mod-11 check digit over the first ten, with letter values `A=10, B=12, C=13…`
skipping every multiple of 11, each position weighted by `2**position`.

That makes a container number *self-verifying*: a single misread digit is
detectable with zero ground truth and no second model call. When two documents
disagree and exactly one number fails its own checksum, **the checksum arbitrates**
— the failing side is marked the outlier and the reason says so. This is the only
place in the engine where a conflict resolves itself, and it resolves on
arithmetic the printer was already committed to, not on an opinion.

## 6. HS codes: compare six digits, print ten

`app/normalize/hs_code.py`

HS codes are harmonized internationally to the **first six digits**; digits 7–10
are national subdivisions that legitimately differ between the exporter's and the
importer's paperwork. So `compare_key` carries six digits while `canonical` keeps
the full printed form with its grouping: `85.17.62 (+4 national digits)`.

Comparing full-length strings would produce a false conflict on almost every real
shipment. No tariff database is consulted: this tool reconciles documents, it does
not classify goods, and claiming the latter would be a much bigger lie than
silence about it.

## 7. Party names: the only fuzzy comparison in the system

`app/normalize/party.py` + `app/compare/__init__.py`

Word reordering and legal-entity suffixes are the dominant real-world variance, so
exact matching is useless. The normalizer produces a folded, suffix-stripped token
set; the comparator scores pairs with `rapidfuzz.token_set_ratio`.

| Score | Status |
|---|---|
| 100 | consistent |
| 90–99 | consistent, with the spelling difference shown |
| 70–89 | **suspicious** — a human decides |
| < 70 | conflict |

This band is deliberately the only fuzzy judgement in the engine, and it is the
only place where "close" can mean "agree". Two consequences worth stating: a
near-miss between two genuinely different companies can land in the 70–89 band and
be called suspicious rather than wrong, which is why the band is reviewable rather
than final; and the same similarity logic is pointedly *not* used for goods lines
(`docs/ARCHITECTURE.md`, `app/lineitems.py`), where `H-40` and `H-41` are two parts
and a fuzzy band would hide the only real error.

## 8. Categoricals: Incoterms, references, country of origin

`app/normalize/categorical.py`

Identifier-shaped values whose whole job is to make the same fact written three
ways compare equal. Each builds a `compare_key` only from what was printed, and
**warns rather than guesses** when input is unrecognized — a value quietly mapped to
the wrong country is worse for a customs check than one flagged unread.

Details that came from observed model output rather than from theory:
- `P.O. No. (4410-DE)` — parentheses are treated as delimiters, not deletable
  content. Deleting them used to destroy the identifier.
- `U.S.A.` → `USA`; the inter-character dots between capitals are collapsed.
- `PRC` and `P R C` map to `CN`, because that is what exporters print.
- A reference is tokenized on non-alphanumerics, so `4410-DE`, `4410 DE` and
  `4410/DE` hash alike.

## 9. Tolerances: the whole table

`app/compare/__init__.py`, frozen constants, referenced directly by tests.

| Kind | Consistent | Suspicious | Conflict |
|---|---|---|---|
| weight | within 0.1 % **or** 1 kg, whichever is more generous | within 2 % | beyond that |
| money | within 0.01 % (≈ 1 cent on 10,000) | within 1 % | beyond that, or any currency mismatch |
| count | identical after unit-basis split | different package basis | different count |
| container | identical `compare_key` | — | any difference, unless one side fails ISO 6346 |
| hs code | first six digits equal | — | six digits differ |
| party | `token_set_ratio` ≥ 90 | 70–89 | < 70 |
| categorical | `compare_key` equal | — | differ |

`LOW_CONFIDENCE = 0.5` works in one direction only, and it works against the
system's own good news: a comparison that came out `consistent` is **downgraded to
`suspicious`** when any document reported confidence below 0.5 on that field, with
the reason naming which one and telling the reader to verify the source text. A
conflict is never softened by it. Absent fields are excluded from the check — an
absent value legitimately has confidence 0, and counting it would flag every
comparison where one document simply does not carry the field.

## 10. Absence

`present: false` is preserved end to end. A field printed on one document only is
reported as single-source with the explicit reason "there is nothing to reconcile
it against" — never as a conflict, and never as agreement. Where a document type
legitimately does not carry a field (an HS code on a packing list, say) the value
is marked `expected_here: false` so the absence reads as expected rather than as a
gap in the extraction.

The rule behind all of it: the system may report what the documents say, and may
report that it cannot tell. It may not fill a gap because the gap looked unlikely.

---

## Changing a rule

1. Change the normalizer or the constant.
2. Bump `NORMALIZER_VERSION` or `COMPARATOR_VERSION` in `app/config.py`. A cached
   response must never be silently reconciled against a newer ruleset — that is
   what the version tags are for.
3. Run `python -m pytest tests/ -q` and then `python scripts/evaluate.py --check`,
   which re-scores every captured extraction against ground truth. Several floors
   there are set at measured values, so a rule that quietly degrades accuracy fails
   the gate rather than shipping.
4. Update this file. The thresholds in the table above are the contract; if they
   disagree with the code, one of them is a bug.
