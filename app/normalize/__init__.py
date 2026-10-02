"""Dispatch a raw extracted value to the right normalizer by field kind.

Produces the NormalizedField that the rest of the app uses. Two hard rules
enforced here, because violating either is the classic bug in this genre of
tool:

  * `canonical` is for humans, `compare_key` is for the engine. The comparator
    touches ONLY compare_key; the UI touches ONLY canonical/raw_value. They are
    produced together and never derived from each other.
  * normalization NEVER calls the model and never fails loudly enough to kill a
    document. A bad value becomes ok=False plus a warning.
"""

from __future__ import annotations

from ..fields import FIELD_KIND, FIELD_ORDER, FieldKey
from ..models import ExtractedField, NormalizedField
from .categorical import (
    normalize_country,
    normalize_incoterm,
    normalize_reference,
)
from .container import normalize_container
from .hs_code import normalize_hs_code
from .money import normalize_money
from .party import normalize_party
from .quantity import normalize_quantity
from .weight import normalize_weight

__all__ = [
    "normalize_container",
    "normalize_field",
    "normalize_hs_code",
    "normalize_money",
    "normalize_incoterm",
    "normalize_country",
    "normalize_party",
    "normalize_quantity",
    "normalize_weight",
]


def normalize_field(key: FieldKey, extracted: ExtractedField) -> NormalizedField:
    kind = FIELD_KIND[key]

    if not extracted.present or extracted.value is None:
        return NormalizedField(ok=True, canonical=None, compare_key=None)

    text = extracted.value
    if kind == "weight":
        res = normalize_weight(text)
    elif kind == "count":
        res = normalize_quantity(text)
    elif kind == "container_number":
        res = normalize_container(text)
    elif kind == "hs_code":
        res = normalize_hs_code(text)
    elif kind == "party_name":
        res = normalize_party(text)
    elif kind == "money":
        res = normalize_money(text)
    elif kind == "incoterm":
        res = normalize_incoterm(text)
    elif kind == "reference":
        res = normalize_reference(text)
    elif kind == "country":
        res = normalize_country(text)
    else:  # pragma: no cover - FIELD_KIND is exhaustive
        raise ValueError(f"unknown field kind {kind!r}")

    return NormalizedField(
        canonical=res.canonical,
        compare_key=res.compare_key,
        rules_applied=list(res.rules_applied),
        warnings=list(res.warnings),
        ok=bool(res.ok),
    )


def normalize_document_fields(
    fields: dict[FieldKey, ExtractedField],
) -> dict[FieldKey, NormalizedField]:
    return {key: normalize_field(key, fields[key]) for key in FIELD_ORDER if key in fields}
