"""Prompt for the optional executive summary.

One call per shipment, never per field. This is the only place a model is
asked to write prose; every per-field verdict still comes from the
deterministic comparison engine. The summary is additive and must never be
able to change, soften or contradict a verdict — so it is fed the verdicts
rather than the documents, and asked only to restate them.
"""

from __future__ import annotations

from .fields import FIELD_LABEL
from .models import Report

SUMMARY_VERSION = "summary-v1"

# "/no_think" is Nemotron's documented reasoning toggle. Live-verified on
# NVIDIA-Nemotron-3-Nano-30B-A3B: it cuts reasoning tokens ~17% (1218 -> 1013)
# and produces a cleaner `content` answer. Reasoning is still generated, so the
# plausibility guard below is what actually protects the UI.
SYSTEM = (
    "You write short status updates for logistics auditors. You restate the "
    "facts you are given and never invent numbers, causes, or recommendations "
    "that are not present in them. Answer in plain prose only. /no_think"
)

# A reasoning model will sometimes ignore "no preamble" and emit its planning as
# the answer -- observed live, where it spent the whole token budget narrating
# and returned "Check: Sentence 1 uses ... That's exactly the numbers." A single
# retry with the failure named is enough to recover most of the time.
RETRY_SYSTEM = (
    "Write exactly two sentences of plain prose for a logistics auditor. "
    "Output ONLY those two sentences. Your entire reply is the two sentences: "
    "no analysis, no self-check, no quotation marks, no preamble, no explanation "
    "of what you are about to write or did write. /no_think"
)


def build_summary_prompt(report: Report) -> str:
    lines = []
    for comp in report.comparisons:
        detail = comp.reason.strip()
        lines.append(f"- {FIELD_LABEL.get(comp.field, comp.label)}: {comp.status}. {detail}")

    s = report.summary
    doc_note = (
        f"{s.documents_failed} of {s.documents_ok + s.documents_failed} uploaded "
        f"documents failed to extract."
        if s.documents_failed
        else f"{s.documents_ok} documents extracted successfully."
    )

    # Precomputed so the model never has to do arithmetic or interpret status
    # names. Live-verified failure: asked for "how many agree and how many need
    # attention", the model counted the 2 single-source fields as needing
    # attention on a shipment with no problems at all. Same class of error the
    # extraction prompt forbids, so the same rule applies here -- we supply the
    # derived numbers, the model only phrases them.
    needs_attention = s.conflict + s.suspicious
    agrees = s.consistent

    return f"""A shipment's trade documents were cross-checked. Write EXACTLY two plain
sentences of English for an auditor reading this report. No preamble, no markdown,
no bullet points, no headings, no quotation marks, no restating these instructions.

Sentence 1 must state that {agrees} fields agree and {needs_attention} fields need
attention, using those exact numbers. Use correct English: write "1 field" not
"1 fields", and "0 fields" is fine. Do not copy any placeholder syntax.
Sentence 2 must name the specific discrepancies that need attention, or say that
none do.

Only use the facts below. Do not compute anything, do not suggest causes, do not
recommend actions, and do not mention any number that is not listed here.

DOCUMENTS
{doc_note}

FIELD VERDICTS (already computed by deterministic code — do not contradict them)
{chr(10).join(lines)}

PRECOMPUTED COUNTS (use these, do not recount the list above)
agree: {agrees}
need attention (conflict + suspicious): {needs_attention}
conflict: {s.conflict}
suspicious: {s.suspicious}
single-source, informational only, NOT a problem: {s.single_source}
absent: {s.absent}
overall verdict: {s.verdict}"""


# Markers of a model talking to itself rather than answering. Observed live:
# "Sentence 2: \"...That's second sentence. Check constraints: ..." -- the
# Nano model narrated its own instruction-following when the prompt was long.
_COT_MARKERS = (
    "that's",
    "check constraints",
    "we used",
    "must be plain",
    "so we output",
    "thus sentence",
    "let me",
    "i need to",
    "first, i",
    "the user wants",
    "ensure exactly",
    "no extra punctuation",
    "here is",
    "here's",
    "sure,",
    "certainly",
    "as an ai",
    "step 1",
    "analysis:",
)


def summary_is_plausible(text: str) -> bool:
    """Reject empty, over-long, refused, or chain-of-thought output.

    Deliberately conservative: showing no summary is better than showing a
    model's internal monologue to an auditor.
    """
    stripped = (text or "").strip()
    if len(stripped) < 40:
        return False
    if len(stripped) > 900:  # two sentences should never approach this
        return False
    lowered = stripped.lower()
    if any(m in lowered for m in _COT_MARKERS):
        return False
    # A reasoning leak usually contains quoted self-talk or markdown scaffolding.
    if ':"' in lowered or lowered.count('"') > 2 or lowered.lstrip().startswith(("#", "-", "*")):
        return False
    sentences = sum(stripped.count(c) for c in ".!?")
    if sentences > 6:
        return False
    # A reasoning model can spend the whole token budget thinking and emit a
    # truncated answer -- observed live: "...need attention. Gross" (49 chars,
    # no CoT markers, so every check above passed). Requiring terminal
    # punctuation catches it, and a dangling final clause is worse than none.
    if not stripped.endswith((".", "!", "?")):
        return False
    last = stripped.rstrip(".!?").rsplit(".", 1)[-1].strip()
    if len(last.split()) < 3:
        return False
    return True
