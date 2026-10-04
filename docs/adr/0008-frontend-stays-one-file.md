# 0008 — The frontend stays one script; browser tests were added instead

**Status:** accepted, with a revisit trigger

## Context

`web/app.js` is ~1,600 lines with nine commented section dividers, no framework and
no build step. Splitting it into ES modules is on every cleanup list, and it was
scheduled here.

## Decision

Do not split it. Add a browser test suite instead, and record why the split as
scoped makes the architecture worse.

The measurement: slicing at the section boundaries and computing the cross-chunk
reference graph produced mutually cyclic modules — `report ↔ run` (rendering calls
`poll`, the run loop calls `renderReport`), `run ↔ intake`, `net ↔ config` (the
transport needs `configHeaders`, the drawer needs `getJSON`), and a longer loop
through `rail → net → config → rail`. `core` is the only clean leaf.

## Consequences

- Eight files with a cycle web is a worse dependency story than one file with one
  scope, so the split would have bought file-count and cost legibility.
- Doing it properly requires an action/event seam between rendering and run control
  first — a redesign, not a move. That is the work, and it is not scheduled.
- What was bought instead: 12 tests that drive real chromium against a real uvicorn,
  covering the golden path, the honesty of the provenance text, the evidence
  lightbox, a correction surviving a reload, the 390px scroll behavior, and the
  browser unlock flow. Those catch the class of regression the split would have
  risked, and they were validated against a deliberate mutation rather than trusted.
- One correction worth keeping in the record: the first pass of that analysis
  reported a `core → report` edge that turned out to be the `showRaw: false`
  property on `state` matching the `showRaw()` function name. Word-boundary matching
  over source text is not a dependency graph, and the conclusion changed when the
  measurement got better.

**Revisit when** the UI grows a second view, or when a change needs to reach into
`run` from a module that is not `main`. That is the signal the seam is missing.
