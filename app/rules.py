"""Cross-field sanity: rules no single-field comparison can see.

Each field is reconciled across documents on its own. These checks read two or
more fields *together* and ask whether the shipment is physically and
commercially possible — 48 pieces at 12,450 kg is a match on both fields and an
absurdity as a pair.

Everything here is arithmetic in Python over the engine's own canonical keys,
which are ours to parse because we wrote their format (`weight:12450.000`,
`money:USD:48200.00`, `count:48:ea`). The model never computes anything; it
transcribes, and these rules divide. Every threshold is a named constant and
every explanation quotes the number and the bound it broke, so a finding can be
disputed in one line.

A finding is advisory. It cannot set a comparison status; it feeds the customs
risk strip, which is itself deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .fields import FieldKey
from .models import Report, SanityFinding

# Implied mass of one piece. Above this, either the count or the weight is wrong;
# a 40-foot container is ~26,000 kg of cargo, so 2,000 kg per piece is already
# generous for anything shipped as discrete units.
MAX_KG_PER_UNIT = 2000.0
# Below this on a real shipment, the count is almost certainly a carton count and
# the weight a pallet total — a basis mismatch, not a typo.
MIN_KG_PER_UNIT = 0.0005
# Implied price per piece. Deliberately wide: this catches unit-basis errors
# (pallets vs pieces) and transcription slips, not luxury pricing.
MAX_VALUE_PER_UNIT = 250_000.0
# HS codes are internationally harmonised to six digits; anything shorter cannot
# be a classification, only a partial one.
MIN_HS_DIGITS = 6
# Net weight above gross weight is not a rounding question: packaging cannot have
# negative mass. Two documents may state either one, but both are totals for the
# same shipment, so the pair is comparable whatever basis each was printed on.
MIN_NET_MARGIN_KG = 0.0

_WEIGHT_RE = re.compile(r"^weight:(-?\d+(?:\.\d+)?)$")
_COUNT_RE = re.compile(r"^count:(-?\d+(?:\.\d+)?):(.+)$")
_MONEY_RE = re.compile(r"^money:([A-Z]{3}|UNKNOWN):(-?\d+(?:\.\d+)?)$")


@dataclass(frozen=True)
class Reading:
    """One field's decided value, plus which documents said it."""

    field: FieldKey
    compare_key: str
    canonical: str
    documents: tuple[str, ...]
    # More than one document stated this field, so it was reconcilable.
    cross_checked: bool = False

    @property
    def weight_kg(self) -> float | None:
        match = _WEIGHT_RE.match(self.compare_key)
        return float(match.group(1)) if match else None

    @property
    def count(self) -> float | None:
        match = _COUNT_RE.match(self.compare_key)
        return float(match.group(1)) if match else None

    @property
    def count_unit(self) -> str | None:
        match = _COUNT_RE.match(self.compare_key)
        return match.group(2) if match else None

    @property
    def money(self) -> tuple[str, float] | None:
        match = _MONEY_RE.match(self.compare_key)
        if not match:
            return None
        return match.group(1), float(match.group(2))


def readings(report: Report) -> dict[FieldKey, Reading]:
    """The majority value per field, or the only value if there is no majority.

    Deliberately reads the comparison rather than re-deriving from documents: the
    comparison already decided which value wins and why, and a second opinion
    here would be a second decision.
    """
    out: dict[FieldKey, Reading] = {}
    for comp in report.comparisons:
        stated = [v for v in comp.values if v.compare_key]
        if not stated:
            continue
        winner = comp.majority_compare_key or stated[0].compare_key
        agreed = tuple(v.document_label for v in stated if v.compare_key == winner)
        out[comp.field] = Reading(
            field=comp.field,
            compare_key=winner,
            canonical=next(v.canonical for v in stated if v.compare_key == winner),
            documents=agreed,
            cross_checked=len({v.document_id for v in stated}) > 1,
        )
    return out


def _finding(
    code: str, level: str, fields: list[FieldKey], explanation: str
) -> SanityFinding:
    return SanityFinding(
        code=code, level=level, fields=fields, explanation=explanation
    )


def sanity_checks(report: Report) -> list[SanityFinding]:
    """Run every cross-field rule. Pure; ordering is stable."""
    got = readings(report)
    out: list[SanityFinding] = []

    weight = got.get("gross_weight")
    quantity = got.get("quantity")
    money = got.get("invoice_value")

    if weight and quantity:
        kg, units = weight.weight_kg, quantity.count
        if kg is not None and units:
            per_unit = kg / units
            if per_unit > MAX_KG_PER_UNIT:
                out.append(_finding(
                    "unit_mass_high", "risk", ["gross_weight", "quantity"],
                    f"{weight.canonical} over {quantity.canonical} implies "
                    f"{per_unit:,.1f} kg per piece, above the {MAX_KG_PER_UNIT:,.0f} kg "
                    f"bound for discrete cargo. One of the two is read on the wrong "
                    f"basis (cartons counted against a pallet weight is the usual cause).",
                ))
            elif 0 < per_unit < MIN_KG_PER_UNIT:
                out.append(_finding(
                    "unit_mass_low", "warn", ["gross_weight", "quantity"],
                    f"{weight.canonical} over {quantity.canonical} implies "
                    f"{per_unit:.6f} kg per piece, below {MIN_KG_PER_UNIT} kg. "
                    f"Weight and count are unlikely to describe the same basis.",
                ))

    if money and quantity:
        parsed = money.money
        units = quantity.count
        if parsed and units:
            currency, amount = parsed
            per_unit = amount / units
            if currency != "UNKNOWN" and per_unit > MAX_VALUE_PER_UNIT:
                out.append(_finding(
                    "unit_price_high", "warn", ["invoice_value", "quantity"],
                    f"{money.canonical} over {quantity.canonical} implies "
                    f"{currency} {per_unit:,.2f} per piece, above the "
                    f"{MAX_VALUE_PER_UNIT:,.0f} bound. Currencies are never "
                    f"converted, so this compares like for like only.",
                ))

    net = got.get("net_weight")
    if weight and net:
        gross_kg, net_kg = weight.weight_kg, net.weight_kg
        if gross_kg is not None and net_kg is not None and net_kg > gross_kg + MIN_NET_MARGIN_KG:
            out.append(_finding(
                "net_above_gross", "risk", ["net_weight", "gross_weight"],
                f"Net weight {net.canonical} exceeds gross weight "
                f"{weight.canonical}. Both are totals for this shipment, and "
                f"packaging cannot have negative mass, so one of the two is "
                f"transcribed wrongly or read off the wrong basis.",
            ))

    if money:
        parsed = money.money
        if parsed and parsed[0] == "UNKNOWN":
            out.append(_finding(
                "value_without_currency", "risk", ["invoice_value"],
                f"Invoice value {money.canonical} carries no currency code. The "
                f"value cannot be reconciled against another document, and no "
                f"currency is inferred.",
            ))

    hs = got.get("hs_code")
    if hs:
        digits = "".join(ch for ch in (hs.compare_key or "") if ch.isdigit())
        if len(digits) < MIN_HS_DIGITS:
            out.append(_finding(
                "hs_code_short", "warn", ["hs_code"],
                f"HS code {hs.canonical} has {len(digits)} digit(s); the "
                f"internationally harmonised minimum is {MIN_HS_DIGITS}. A shorter "
                f"code is a partial heading, not a classification.",
            ))

    bills = [d for d in report.documents
             if (d.declared_doc_type or d.detected_doc_type) == "bill_of_lading"]
    container = got.get("container_number")
    if bills and container is None:
        out.append(_finding(
            "bill_of_lading_without_container", "warn", ["container_number"],
            f"{len(bills)} bill(s) of lading were read but no document stated a "
            f"container number, so the shipment cannot be tied to equipment.",
        ))

    # Coverage gaps are stated, not silently dropped: "we could not check" is a
    # different claim from "we checked and it agrees".
    unchecked = [
        reading.field for reading in got.values()
        if not reading.cross_checked
    ]
    if unchecked:
        names = ", ".join(field.replace("_", " ") for field in sorted(unchecked))
        out.append(_finding(
            "single_source_fields", "info", list(sorted(unchecked)),
            f"Not cross-checked because only one document stated: {names}. "
            f"Agreement between two readers is what the comparison measures.",
        ))
    return out


def raises_risk(findings: list[SanityFinding]) -> bool:
    return any(f.level == "risk" for f in findings)
