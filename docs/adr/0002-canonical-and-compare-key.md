# 0002 — `canonical` and `compare_key` are two values, not one

**Status:** accepted

## Context

A normalized value has two audiences. A person needs `12,450.000 kg` — grouped,
unit-suffixed, obviously the same thing as the printed `12,450.00 KGS`. The engine
needs something stable to test equality on, and formatting is not stability.

Collapsing them into one "normalized" string is the tempting design, and it fails
in both directions: either the comparison key grows formatting sensitivity (so
`12.450,00` conflicts with `12,450.00`), or the display value gets flattened into a
slug nobody can read.

## Decision

`NormalizedField` carries both. `canonical` is for the UI, the CSV and the PDF;
`compare_key` is the only thing `app/compare/__init__.py` ever looks at. Tests
assert on both, separately.

## Consequences

- Display changes cannot break comparison, and comparison changes cannot leak into
  display.
- Every normalizer must produce two things, and must be explicit about which one
  loses information. HS codes are the sharpest example: `compare_key` carries six
  digits because digits 7-10 are national and legitimately differ, while
  `canonical` prints `85.17.62 (+4 national digits)` so the reader can see what was
  dropped and why.
- A reviewer who disagrees with a verdict can be shown `canonical` for both
  documents and the key that made them equal, which is the whole argument in two
  strings.
