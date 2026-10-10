"""Comparison engine. Pure functions, no I/O, no model calls.

Touches ONLY compare_key for deciding agreement, and ONLY canonical/raw_value
when composing the human-readable reason. The two never mix.

Every result carries a TEMPLATED reason string with the actual numbers
interpolated. Deterministic, free, auditable — and the difference between
output that reads like a product and output that reads like a raw diff.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from rapidfuzz import fuzz

from ..fields import FIELD_KIND, FIELD_LABEL, FIELD_ORDER, EXPECTED, FieldKey
from ..models import Comparison, ComparisonValue, ExtractedField, NormalizedField

# ---------------------------------------------------------------------------
# Frozen thresholds. One place, referenced by tests.
# ---------------------------------------------------------------------------

WEIGHT_TOLERANCE_CONSISTENT_REL = 0.001  # 0.1 %
WEIGHT_TOLERANCE_CONSISTENT_ABS_KG = 1.0  # ... or 1 kg, whichever is more generous
WEIGHT_TOLERANCE_SUSPICIOUS_REL = 0.02  # 2 % -> rounding-ish, still worth a look

MONEY_TOLERANCE_CONSISTENT_REL = 0.0001  # money is exact; 1 cent on 10k
MONEY_TOLERANCE_SUSPICIOUS_REL = 0.01

PARTY_EXACT = 100
PARTY_FUZZY_CONSISTENT = 90
PARTY_SUSPICIOUS = 70  # 70-89 -> suspicious, < 70 -> conflict

LOW_CONFIDENCE = 0.5


@dataclass
class FieldObservation:
    """One document's view of one field, ready to compare."""

    document_id: str
    document_label: str
    doc_type: str
    extracted: ExtractedField
    normalized: NormalizedField

    @property
    def usable(self) -> bool:
        return (
            self.extracted.present
            and self.extracted.value is not None
            and self.normalized.compare_key is not None
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RELATIVE_KINDS = {"weight", "money"}


def _rel_diff(a: float, b: float) -> float:
    base = max(abs(a), abs(b))
    if base == 0:
        return 0.0
    return abs(a - b) / base


def _num_from_compare_key(compare_key: str | None) -> float | None:
    """weight:12450.000 / money:USD:8420.00 / count:1200:piece -> leading number."""
    if not compare_key:
        return None
    parts = compare_key.split(":")
    for p in parts[1:]:
        try:
            return float(p)
        except ValueError:
            continue
    return None


def _unit_from_compare_key(compare_key: str | None) -> str | None:
    if compare_key and compare_key.startswith("count:"):
        parts = compare_key.split(":")
        if len(parts) == 3:
            return parts[2]
    return None


def _currency_from_compare_key(compare_key: str | None) -> str | None:
    if compare_key and compare_key.startswith("money:"):
        return compare_key.split(":")[1]
    return None


# Package words split into two bases. Comparing across bases is usually fine
# (1,200 pieces == 100 cartons of 12); comparing within a base is meaningful.
_ITEM_UNITS = {"piece", "set", "unit"}


def package_basis(unit: str | None) -> str:
    if unit is None or unit == "unknown":
        return "unknown"
    return "item" if unit in _ITEM_UNITS else "package"


def _majority(values: list[ComparisonValue]) -> str | None:
    """Most common compare_key. Ties resolve to the first seen, which keeps
    output stable across runs."""
    keys = [v.compare_key for v in values if v.compare_key]
    if not keys:
        return None
    counts = Counter(keys)
    best = max(counts.values())
    for k in keys:
        if counts[k] == best:
            return k
    return None


def _mark_outliers(values: list[ComparisonValue], majority_key: str | None) -> None:
    if majority_key is None:
        return
    counts = Counter(v.compare_key for v in values)
    # Only call something an outlier when a real majority exists (3+ docs).
    if len(values) < 3:
        return
    for v in values:
        if v.compare_key != majority_key and counts[v.compare_key] < len(values) / 2:
            v.is_outlier = True


# ---------------------------------------------------------------------------
# Per-kind comparators
# ---------------------------------------------------------------------------


def _compare_numeric(
    kind: str,
    obs: list[FieldObservation],
    values: list[ComparisonValue],
    *,
    label: str = "value",
) -> tuple[str, str]:
    rel_consistent = (
        WEIGHT_TOLERANCE_CONSISTENT_REL if kind == "weight" else MONEY_TOLERANCE_CONSISTENT_REL
    )
    rel_suspicious = (
        WEIGHT_TOLERANCE_SUSPICIOUS_REL if kind == "weight" else MONEY_TOLERANCE_SUSPICIOUS_REL
    )

    nums = [_num_from_compare_key(v.compare_key) for v in values]
    if any(n is None for n in nums):
        return "suspicious", "one or more values did not normalize to a number"

    # Money in different currencies can never be reconciled.
    if kind == "money":
        currencies = {_currency_from_compare_key(v.compare_key) for v in values}
        currencies.discard(None)
        if len(currencies) > 1:
            listed = ", ".join(
                f"{v.document_label}={v.canonical}" for v in values
            )
            return "conflict", (
                f"cannot compare across currencies ({listed}); "
                "no FX conversion is applied by design"
            )

    lo, hi = min(nums), max(nums)  # type: ignore[type-var]
    rel = _rel_diff(lo, hi)
    spread = f"{lo:,.2f} vs {hi:,.2f}"

    if kind == "weight" and (hi - lo) <= WEIGHT_TOLERANCE_CONSISTENT_ABS_KG:
        return "consistent", f"{label} agrees within {WEIGHT_TOLERANCE_CONSISTENT_ABS_KG:g} kg ({spread})"
    if rel <= rel_consistent:
        return "consistent", f"values agree within {rel_consistent:.1%} ({spread})"
    if rel <= rel_suspicious:
        return "suspicious", (
            f"values differ by {rel:.2%} ({spread}) — within the "
            f"{rel_suspicious:.0%} rounding band, worth a human glance"
        )
    return "conflict", (
        f"values differ by {rel:.2%} ({spread}) — above the "
        f"{rel_suspicious:.0%} materiality threshold for a container shipment"
    )


def _compare_count(
    obs: list[FieldObservation], values: list[ComparisonValue]
) -> tuple[str, str]:
    units = [_unit_from_compare_key(v.compare_key) for v in values]
    counts = {_num_from_compare_key(v.compare_key) for v in values}
    listed = ", ".join(f"{v.document_label}={v.canonical}" for v in values)

    # Only "individual items vs packaging" is a benign difference of basis.
    # Two different *packaging* types (100 cartons vs 20 pallets) is a real
    # conflict, because both documents are counting the same physical boxes.
    bases = {package_basis(u) for u in units}
    if len(bases) == 2 and "item" in bases and "package" in bases:
        return "suspicious", (
            f"different package basis, so the counts are not directly comparable "
            f"({listed}) — one document counts individual items, another counts "
            "packaging. This is normal on real trade documents, not necessarily an error."
        )

    if len(counts) == 1 and len(bases) == 1:
        only = next(iter(counts))
        return "consistent", f"quantity agrees across {len(values)} documents ({only:,.0f})"

    if len(counts) == 1:
        return "suspicious", (
            f"same count ({next(iter(counts)):,.0f}) but printed with different "
            f"package words ({listed}); likely the same goods, worth a glance"
        )

    lo, hi = min(counts), max(counts)  # type: ignore[type-var]
    return "conflict", (
        f"quantity differs on a {hi - lo:,.0f} unit gap ({listed}) — the documents "
        "count the same kind of unit, so this is a real discrepancy"
    )


def _compare_party(
    obs: list[FieldObservation], values: list[ComparisonValue]
) -> tuple[str, str]:
    keys = [v.compare_key for v in values]
    if len(set(keys)) == 1:
        return "consistent", (
            f"consignee identical after normalization across {len(values)} documents "
            f"({values[0].canonical})"
        )

    # Score every pair on the folded name; the worst pair decides the status.
    names = [v.canonical or "" for v in values]
    worst = 100
    worst_pair = ""
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            score = fuzz.token_set_ratio(names[i], names[j])
            if score < worst:
                worst = score
                worst_pair = f"{values[i].document_label} vs {values[j].document_label}"

    detail = ", ".join(f"{v.document_label}={v.raw_value!r}" for v in values)
    if worst >= PARTY_FUZZY_CONSISTENT:
        return "consistent", (
            f"consignee names differ only in formatting or legal suffix "
            f"(similarity {worst}/100, {worst_pair}): {detail}"
        )
    if worst >= PARTY_SUSPICIOUS:
        return "suspicious", (
            f"consignee names are similar but not equivalent (similarity "
            f"{worst}/100, {worst_pair}): {detail}"
        )
    return "conflict", (
        f"consignee names look like different parties (similarity {worst}/100, "
        f"{worst_pair}): {detail}"
    )


def _compare_container(
    obs: list[FieldObservation], values: list[ComparisonValue]
) -> tuple[str, str]:
    """ISO 6346 gives us arbitration power: the field validates itself."""
    from ..normalize.container import normalize_container

    keys = [v.compare_key for v in values]
    if len(set(keys)) == 1:
        only = values[0].canonical or ""
        res = normalize_container(only)
        if res.check_digit_valid:
            return "consistent", (
                f"container number {only} agrees across {len(values)} documents and "
                "passes its ISO 6346 check digit"
            )
        return "suspicious", (
            f"container number {only} agrees across documents but FAILS its own "
            f"ISO 6346 check digit — {res.detail}"
        )

    verdicts = [(v, normalize_container(v.canonical or v.raw_value or "")) for v in values]
    valid = [v for v, r in verdicts if r.check_digit_valid]
    invalid = [v for v, r in verdicts if not r.check_digit_valid]

    listed = ", ".join(
        f"{v.document_label}={v.canonical or v.raw_value} "
        f"[{'checksum OK' if r.check_digit_valid else 'checksum FAILS'}]"
        for v, r in verdicts
    )

    if len(invalid) == 1 and len(valid) >= 1:
        bad = invalid[0]
        good = valid[0]
        bad_res = next(r for v, r in verdicts if v is bad)
        return "conflict", (
            f"container numbers disagree: {listed}. The ISO 6346 check digit "
            f"ARBITRATES: {bad.document_label} prints check digit "
            f"{bad_res.actual_check_digit} where the algorithm computes "
            f"{bad_res.expected_check_digit}, so that value is internally "
            f"inconsistent. {good.canonical} ({good.document_label}) is the more "
            f"likely correct container."
        )
    if not valid:
        return "conflict", (
            f"container numbers disagree AND none passes its ISO 6346 check digit: "
            f"{listed}. At least one is misread; cannot say which."
        )
    return "conflict", f"container numbers disagree: {listed}"


def _compare_exact(
    obs: list[FieldObservation], values: list[ComparisonValue]
) -> tuple[str, str]:
    keys = {v.compare_key for v in values}
    listed = ", ".join(f"{v.document_label}={v.canonical or v.raw_value}" for v in values)
    if len(keys) == 1:
        return "consistent", f"values match across {len(values)} documents ({values[0].canonical})"
    return "conflict", f"values differ: {listed}"


def _arbitration(kind: str, status: str, values: list[ComparisonValue]) -> str | None:
    """Who decided a disagreement, when it was not a vote.

    Recomputed from the normalizer rather than read out of the reason sentence:
    the reason is prose we format, and a downstream consumer that trusted it would
    break the day the wording improved. A container number carries its own check
    digit, so exactly one side failing it settles which paper is inconsistent --
    and a draft that asked the wrong party to reissue would be a false accusation
    in a filed artifact.
    """
    if kind != "container_number" or status not in ("conflict", "suspicious"):
        return None
    from ..normalize.container import normalize_container

    verdicts = [(v, normalize_container(v.canonical or v.raw_value or ""))
                for v in values if v.compare_key]
    if not verdicts:
        return None
    invalid = [v for v, r in verdicts if not r.check_digit_valid]
    valid = [v for v, r in verdicts if r.check_digit_valid]

    if status == "conflict":
        # Two sides, and one of them is internally inconsistent. That settles
        # which paper is wrong; a vote would not, and on a 1-1 split neither
        # would the majority.
        if len({v.compare_key for v, _ in verdicts}) < 2:
            return None
        return "iso_6346_check_digit" if len(invalid) == 1 and valid else None

    # Suspicious on this kind means the papers agree and the number fails its own
    # check digit -- the field disagreeing with itself, which is the same kind of
    # certainty and belongs beside it rather than in the reason text.
    return "iso_6346_check_digit" if not valid else None


COMPARATORS = {
    "weight": _compare_numeric,
    "money": _compare_numeric,
    "count": _compare_count,
    "party_name": _compare_party,
    "container_number": _compare_container,
    "hs_code": _compare_exact,
    # Categorical identifiers: exact on the normalized key. No fuzzy band --
    # two PO numbers being "90% similar" means they are different orders.
    "incoterm": _compare_exact,
    "reference": _compare_exact,
    "country": _compare_exact,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def compare_field(key: FieldKey, obs: list[FieldObservation]) -> Comparison:
    kind = FIELD_KIND[key]
    usable = [o for o in obs if o.usable]

    values = [
        ComparisonValue(
            document_id=o.document_id,
            document_label=o.document_label,
            doc_type=o.doc_type,  # type: ignore[arg-type]
            page_index=o.extracted.page_index,
            raw_value=o.extracted.value,
            canonical=o.normalized.canonical,
            compare_key=o.normalized.compare_key,
            confidence=o.extracted.confidence,
            warnings=list(o.normalized.warnings),
            expected_here=key in EXPECTED.get(o.doc_type, frozenset()),  # type: ignore[arg-type]
        )
        for o in obs
    ]

    comparison = Comparison(
        field=key, label=FIELD_LABEL[key], kind=kind, status="absent", reason="", values=values
    )

    # --- cardinality gate, before any comparator runs ---
    if not usable:
        expected_any = any(
            key in EXPECTED.get(o.doc_type, frozenset())  # type: ignore[arg-type]
            for o in obs
        )
        if expected_any:
            comparison.status = "absent"
            comparison.reason = (
                f"{FIELD_LABEL[key]} was not found on any document, though it is "
                f"normally present on {FIELD_LABEL[key].lower()} sources "
                f"({', '.join(sorted({o.doc_type for o in obs}))}). Worth checking "
                "the scan quality."
            )
        else:
            comparison.status = "absent"
            comparison.reason = (
                f"{FIELD_LABEL[key]} is not printed on these document types "
                "(normally found on: "
                f"{_typically_in(key)}), so its absence is expected, not a problem."
            )
        return comparison

    if len(usable) == 1:
        comparison.status = "single_source"
        comparison.majority_compare_key = values[0].compare_key
        comparison.reason = (
            f"{FIELD_LABEL[key]} appears on only one document "
            f"({values[0].document_label}), so there is nothing to reconcile it "
            "against. Shown for reference, not flagged."
        )
        return comparison

    comparator = COMPARATORS[kind]
    # Comparators see ONLY usable values. An absent field is not a candidate
    # for disagreement — feeding it in would make "not printed on this document
    # type" look like a checksum failure or a null number.
    usable_values = [v for v, o in zip(values, obs) if o.usable]
    if kind in RELATIVE_KINDS:
        # One comparator serves gross weight and net weight, so the row names
        # itself here: "gross weight agrees" beside a net-weight verdict reads as
        # though the engine had checked the wrong field.
        status, reason = comparator(  # type: ignore[call-arg]
            kind, obs, usable_values, label=FIELD_LABEL[key].lower()
        )
    else:
        status, reason = comparator(obs, usable_values)

    majority = _majority(usable_values)
    _mark_outliers(usable_values, majority)
    comparison.status = status  # type: ignore[assignment]
    comparison.reason = reason
    comparison.majority_compare_key = majority
    comparison.arbitrated_by = _arbitration(kind, status, usable_values)

    # A low-confidence read is worth surfacing even when everything agrees.
    # Only usable values count: an absent field legitimately has confidence 0
    # and would otherwise flag every comparison where one doc lacks the field.
    low = [v.document_label for v in usable_values if v.confidence < LOW_CONFIDENCE]
    if low and status == "consistent":
        comparison.status = "suspicious"  # type: ignore[assignment]
        comparison.reason += (
            f" Note: {', '.join(low)} reported confidence below "
            f"{LOW_CONFIDENCE:g} on this field, so verify the source text."
        )
    return comparison


def _typically_in(key: FieldKey) -> str:
    from ..fields import TYPICALLY_IN

    return TYPICALLY_IN[key]


def compare_shipment(
    documents: list[tuple[str, str, str, dict, dict]],
) -> list[Comparison]:
    """documents: list of (id, label, doc_type, extracted_fields, normalized_fields)."""
    comparisons: list[Comparison] = []
    for key in FIELD_ORDER:
        obs = [
            FieldObservation(
                document_id=doc_id,
                document_label=label,
                doc_type=doc_type,
                extracted=extracted[key],
                normalized=normalized[key],
            )
            for doc_id, label, doc_type, extracted, normalized in documents
            if key in extracted and key in normalized
        ]
        comparisons.append(compare_field(key, obs))
    return comparisons
