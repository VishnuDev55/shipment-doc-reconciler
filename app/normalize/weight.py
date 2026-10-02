"""Weight normalization to a canonical unit (kg).

Never lets the model do arithmetic: the model returns "12,450.00 KGS" verbatim
and this module turns it into a comparable number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .number import extract_number_token, parse_number

# factor to multiply by to get kg
UNITS: dict[str, tuple[float, str]] = {
    "kg": (1.0, "kg"),
    "kgs": (1.0, "kg"),
    "kgm": (1.0, "kg"),
    "kilogram": (1.0, "kg"),
    "kilograms": (1.0, "kg"),
    "kilo": (1.0, "kg"),
    "kilos": (1.0, "kg"),
    "g": (0.001, "kg"),
    "gram": (0.001, "kg"),
    "grams": (0.001, "kg"),
    "mt": (1000.0, "kg"),
    "m/t": (1000.0, "kg"),
    "tonne": (1000.0, "kg"),
    "tonnes": (1000.0, "kg"),
    "metricton": (1000.0, "kg"),
    "metrictons": (1000.0, "kg"),
    "t": (1000.0, "kg"),
    "ton": (1000.0, "kg"),
    "tons": (1000.0, "kg"),
    "lb": (0.45359237, "kg"),
    "lbs": (0.45359237, "kg"),
    "lbm": (0.45359237, "kg"),
    "pound": (0.45359237, "kg"),
    "pounds": (0.45359237, "kg"),
    "#": (0.45359237, "kg"),
}

# Units whose metric/imperial intent is genuinely ambiguous in trade docs.
AMBIGUOUS_UNITS = {"ton", "tons", "t"}

_UNIT_RE = re.compile(
    r"(?<![A-Za-z])(KGS?|KGW|KILOGRAMS?|KGMS?|GRAMS?|G|MT|M/T|TONNES?|METRIC\s+TONS?|"
    r"TONS?|T|LBS?|LBM|POUNDS?|\#)(?![A-Za-z])",
    re.IGNORECASE,
)


@dataclass
class WeightResult:
    raw: str
    value_kg: float | None = None
    unit: str | None = None
    canonical: str | None = None
    compare_key: str | None = None
    rules_applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value_kg is not None


def normalize_weight(raw: str | None) -> WeightResult:
    text = (raw or "").strip()
    res = WeightResult(raw=text)
    if not text:
        res.warnings.append("empty value")
        return res

    token = extract_number_token(text)
    if token is None:
        res.warnings.append(f"no number found in '{text}'")
        return res

    num = parse_number(token)
    if not num.ok:
        res.warnings.extend(num.warnings)
        return res
    res.rules_applied.extend(num.rules_applied)
    if num.ambiguous:
        res.warnings.extend(num.warnings)

    unit_match = _UNIT_RE.search(text, pos=text.find(token[-1]) if token else 0)
    if unit_match is None:
        unit_match = _UNIT_RE.search(text)
    if unit_match is None:
        res.warnings.append(
            f"no weight unit found in '{text}'; cannot convert to a common unit"
        )
        return res

    key = re.sub(r"\s+", "", unit_match.group(0)).lower()
    factor, target = UNITS[key]
    res.unit = key
    res.rules_applied.append(f"unit_{key}_to_{target}")
    if key in AMBIGUOUS_UNITS:
        res.warnings.append(
            f"'{unit_match.group(0)}' is ambiguous (metric tonne vs short ton); "
            "assumed metric tonne (1000 kg)"
        )

    res.value_kg = round(num.value * factor, 6)
    res.canonical = f"{res.value_kg:.3f} {target}"
    res.compare_key = f"weight:{res.value_kg:.3f}"
    return res
