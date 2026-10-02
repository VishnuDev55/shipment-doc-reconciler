"""HS code normalization.

HS codes are internationally harmonized to the FIRST 6 DIGITS; digits 7-10 are
national subdivisions that legitimately differ between the exporter's and the
importer's paperwork. Comparing full-length strings produces false conflicts,
so compare_key carries only the first six digits. No tariff database
validation is attempted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DIGITS = re.compile(r"\d+")


@dataclass
class HSCodeResult:
    raw: str
    digits: str | None = None
    digits6: str | None = None
    canonical: str | None = None
    compare_key: str | None = None
    rules_applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.digits6 is not None


def normalize_hs_code(raw: str | None) -> HSCodeResult:
    text = (raw or "").strip()
    res = HSCodeResult(raw=text)
    if not text:
        res.warnings.append("empty value")
        return res

    digits = "".join(_DIGITS.findall(text))
    if not digits:
        res.warnings.append(f"no digits found in '{text}'")
        return res

    res.rules_applied.append("strip_to_digits")
    res.digits = digits

    if len(digits) < 6:
        res.warnings.append(
            f"'{text}' yields only {len(digits)} digits; an HS code needs at least 6"
        )
        return res

    if len(digits) > 10:
        res.warnings.append(
            f"'{text}' yields {len(digits)} digits — more than a 10-digit HS/HTS code; "
            "may have swallowed an adjacent number"
        )

    res.digits6 = digits[:6]
    res.canonical = f"{digits[:2]}.{digits[2:4]}.{digits[4:6]}"
    if len(digits) > 6:
        res.canonical += f" (+{len(digits) - 6} national digits)"
        res.rules_applied.append("compare_first_6_only")
    res.compare_key = f"hs:{res.digits6}"
    return res
