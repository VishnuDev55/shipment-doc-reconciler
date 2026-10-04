# 0001 — The model transcribes; deterministic Python decides

**Status:** accepted (predates this record; the audit confirmed it holds)

## Context

The obvious design for document reconciliation is to hand the documents to a model
and ask which ones disagree. It is a few hundred lines, it demos well, and it is
unusable: the answer is not reproducible, cannot be argued with, and a model that
misreads a digit produces a confident wrong verdict.

## Decision

A vision model's entire output surface is a JSON object of strings and booleans
(`ExtractedField`). It is never asked to compute, convert, compare or judge. Every
normalization, tolerance and verdict lives in pure Python with frozen constants.

## Consequences

- Verdicts are reproducible and unit-testable without a network call, which is what
  makes the zero-network test suite possible at all.
- Model error is bounded to transcription. A misread digit still produces a wrong
  comparison, but the wrongness is traceable to a specific string in a specific
  document, and the raw response is on disk.
- It costs more code: eight normalizer modules and a comparator table that a single
  prompt would have replaced.
- The failure mode that survives is **invention**, not arithmetic. See
  `docs/EVAL.md`: one provider fabricated 6 field values that no document prints,
  and those changed a shipment verdict. No architecture removes that; only a
  different model does.
