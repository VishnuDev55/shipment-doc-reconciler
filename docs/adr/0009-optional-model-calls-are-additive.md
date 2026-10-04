# 0009 — Advisory model calls can never change a verdict

**Status:** accepted

## Context

Three features ask a model for judgement rather than transcription: an executive
summary, a second opinion on comparisons the engine called ambiguous, and a draft
email to the supplier. Each is genuinely useful and each is a place where a model
could silently overwrite a deterministic result.

## Decision

All three are additive by construction, and the type system is shaped to make the
non-overwriting path the easy one:

- They write to their own fields (`Narrative`, `Adjudication`, `Briefing`,
  `ConsensusResult`, `LineItemsSection`). None of them is read by
  `app/compare/__init__.py` or `app/scoring.py`.
- `Comparison.status` and `Comparison.reason` come from deterministic code;
  `adjudication` sits **beside** them and records what a model would have said.
- Skipping, failing or being refused is a normal outcome and cannot change a
  shipment's `state` or `summary.verdict`.
- `Adjudication.same` is tri-state (`true`/`false`/`null`) because a model that
  declines is informative and a model that guessed is not.
- A human correction sets `superseded_at`: the opinion stays on file but stops
  reading as a comment on the current verdict.

## Consequences

- The line-item section is written **after** the report has been settled and
  persisted, and a test runs the same shipment twice and asserts the comparisons,
  summary, risk contributions, sanity findings and every document dump are identical
  with the flag on and off.
- Cost is explicit and opt-in where it is real: consensus doubles the vision spend
  (`CONSENSUS=false`), goods lines add one call per document
  (`ENABLE_LINE_ITEMS=false`).
- The UI has to say what each block is, every time: `AI-generated`, `AI draft`,
  `hand-authored fixture — no model read these lines`, and for risk, "scored by rule
  from the verdicts above — no model was asked". A judgement that reads like a fact
  is the failure this ADR exists to prevent.
