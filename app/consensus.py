"""Second-model consensus: a disagreement flag, never a tie-break.

Running two providers over the same page and comparing their answers tells you
which fields are hard to read. It does not tell you which one is right, and this
module does not pretend otherwise: the comparison is deterministic Python over
the normalized values, and a divergence only ever marks a field
`needs review`. The primary extraction remains the one the report is built from.

Both sides are real model output. The shipped cache holds a captured NVIDIA
response and a captured Nebius response for every fixture document, so consensus
can run offline against genuine transcriptions rather than invented ones.
"""

from __future__ import annotations

from .fields import FIELD_ORDER
from .models import DocumentReport, FieldDivergence


def divergences_between(
    primary: DocumentReport, secondary: DocumentReport
) -> list[FieldDivergence]:
    """Every field the two runs did not agree on, by compare_key.

    compare_key, not canonical: two spellings of the same party are a match by
    design, and reporting them as a divergence would undercut the normalizer the
    whole app depends on.
    """
    out: list[FieldDivergence] = []
    for key in FIELD_ORDER:
        left = primary.normalized.get(key)
        right = secondary.normalized.get(key)
        left_key = left.compare_key if left is not None else None
        right_key = right.compare_key if right is not None else None
        if left_key == right_key:
            continue
        out.append(
            FieldDivergence(
                field=key,
                # A normalized field with no canonical value is one this reader
                # could not answer, which is a coverage disagreement rather than
                # a transcription one.
                primary_present=left is not None and left.canonical is not None,
                secondary_present=right is not None and right.canonical is not None,
                primary_canonical=left.canonical if left else None,
                secondary_canonical=right.canonical if right else None,
                primary_compare_key=left_key,
                secondary_compare_key=right_key,
            )
        )
    return out


def summarize(divergences: list[FieldDivergence]) -> str:
    """One line a reviewer can act on, naming the fields rather than a score."""
    if not divergences:
        return "both readers agreed on every field"
    names = ", ".join(d.field.replace("_", " ") for d in divergences)
    return f"{len(divergences)} field(s) disagree between readers: {names}"
