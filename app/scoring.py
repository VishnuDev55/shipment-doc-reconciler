"""A 0-100 shipment risk score that shows its working.

One number is easy to read and impossible to argue with, so the score is never
presented alone: every point carries a contribution naming the rule that awarded
it and the value that triggered it. The customs-risk level is *derived from this
score*, not computed alongside it, so the strip and the number cannot disagree.

Deterministic by construction. Inputs are the comparison statuses, the coverage
of expected fields, unreadable documents and the cross-field findings. No model
opinion, no learned weights, no probability. Re-run the same report and you get
the same number, which is the only reason an auditor could act on it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .fields import FieldKey
from .models import Report, RiskContribution, RiskNote

# Points per condition. Sized so a single classification-relevant conflict
# (HS code or container) clears the high band on its own, a commercial conflict
# lands in the middle, and nothing is ever rounded up to look worse.
POINTS = {
    "hs_code_conflict": 60,
    "hs_code_absent": 55,
    "hs_code_suspicious": 30,
    "container_conflict": 60,
    "container_suspicious": 30,
    "value_conflict": 60,
    "weight_conflict": 40,
    "quantity_conflict": 35,
    "party_conflict": 30,
    "unreadable_document": 35,
    "single_source_field": 5,
    "sanity_risk": 30,
    "sanity_warn": 10,
}

# Caps stop volume from drowning severity: five lightly-checked fields should not
# outscore one bad HS code.
MAX_SINGLE_SOURCE = 15
MAX_SANITY = 60
MAX_UNREADABLE = 70

HIGH_AT = 55
MEDIUM_AT = 25


@dataclass(frozen=True)
class _Award:
    code: str
    points: int
    detail: str
    fields: tuple[FieldKey, ...]


def _awards(report: Report) -> list[_Award]:
    out: list[_Award] = []
    got = {comp.field: comp for comp in report.comparisons}

    hs = got.get("hs_code")
    if hs is not None:
        if hs.status == "conflict":
            out.append(_Award("hs_code_conflict", POINTS["hs_code_conflict"],
                              f"HS codes disagree: {hs.reason}", ("hs_code",)))
        elif hs.status == "absent":
            out.append(_Award("hs_code_absent", POINTS["hs_code_absent"],
                              "no document states an HS code, so classification "
                              "cannot be verified", ("hs_code",)))
        elif hs.status == "suspicious":
            out.append(_Award("hs_code_suspicious", POINTS["hs_code_suspicious"],
                              hs.reason, ("hs_code",)))

    container = got.get("container_number")
    if container is not None:
        if container.status == "conflict":
            out.append(_Award("container_conflict", POINTS["container_conflict"],
                              f"container numbers disagree, and at least one fails "
                              f"its ISO 6346 check digit: {container.reason}",
                              ("container_number",)))
        elif container.status == "suspicious":
            out.append(_Award("container_suspicious", POINTS["container_suspicious"],
                              container.reason, ("container_number",)))

    for field, code in (("invoice_value", "value_conflict"),
                        ("gross_weight", "weight_conflict"),
                        ("quantity", "quantity_conflict"),
                        ("consignee_name", "party_conflict")):
        comp = got.get(field)
        if comp is not None and comp.status == "conflict":
            out.append(_Award(code, POINTS[code], comp.reason, (field,)))

    failed = [d for d in report.documents if d.status == "failed"]
    if failed:
        out.append(_Award(
            "unreadable_document",
            min(POINTS["unreadable_document"] * len(failed), MAX_UNREADABLE),
            f"{len(failed)} of {len(report.documents)} document(s) could not be "
            f"read, so coverage is incomplete",
            (),
        ))

    single = [comp for comp in report.comparisons if comp.status == "single_source"]
    if single:
        out.append(_Award(
            "single_source_field",
            min(POINTS["single_source_field"] * len(single), MAX_SINGLE_SOURCE),
            f"{len(single)} field(s) appear on only one document and were never "
            f"cross-checked: " + ", ".join(c.label.lower() for c in single),
            tuple(c.field for c in single),
        ))

    for finding in report.sanity:
        if finding.level == "risk":
            out.append(_Award(f"sanity_{finding.code}", POINTS["sanity_risk"],
                              finding.explanation, tuple(finding.fields)))
        elif finding.level == "warn":
            out.append(_Award(f"sanity_{finding.code}", POINTS["sanity_warn"],
                              finding.explanation, tuple(finding.fields)))
    return out


def score_report(report: Report) -> RiskNote:
    """Sum the awards into a capped score with its contributions attached.

    The band is derived from the score rather than decided alongside it, so the
    strip and the number cannot disagree about the same shipment.
    """
    awards = _awards(report)
    sanity_points = sum(a.points for a in awards if a.code.startswith("sanity_"))
    if sanity_points > MAX_SANITY:
        # Scale the sanity block down rather than dropping findings, so the list
        # still shows everything that fired.
        factor = MAX_SANITY / sanity_points
        awards = [
            replace(a, points=int(round(a.points * factor)))
            if a.code.startswith("sanity_") else a
            for a in awards
        ]

    score = min(100, max(0, sum(a.points for a in awards)))
    band = "high" if score >= HIGH_AT else "medium" if score >= MEDIUM_AT else "low"
    reasons = [f"{a.code.replace('_', ' ')}: {a.detail}" for a in awards] or [
        "no classification-relevant disagreement detected"
    ]
    return RiskNote(
        level=band,
        reasons=reasons,
        score=score,
        high_at=HIGH_AT,
        medium_at=MEDIUM_AT,
        method="deterministic-v1",
        contributions=[
            RiskContribution(code=a.code, points=a.points, detail=a.detail,
                             fields=list(a.fields))
            for a in awards
        ],
    )
