"""Quantity normalization: count and package kind kept SEPARATE.

This is the rule that decides whether the tool looks trustworthy or produces
false alarms. "1,200 pieces" and "100 cartons" are not a conflict — one counts
items, the other counts packaging. It is the single most common legitimate
discrepancy in real trade documents, so the comparator must be able to tell
"different unit basis" apart from "different number".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .number import extract_number_token, parse_number

PACKAGE_KINDS: dict[str, str] = {
    "PC": "piece",
    "PCS": "piece",
    "PIECE": "piece",
    "PIECES": "piece",
    "EA": "piece",
    "UNIT": "piece",
    "UNITS": "piece",
    "CTN": "carton",
    "CTNS": "carton",
    "CARTON": "carton",
    "CARTONS": "carton",
    "CTNRS": "carton",
    "BOX": "box",
    "BOXES": "box",
    "PLT": "pallet",
    "PLTS": "pallet",
    "PALLET": "pallet",
    "PALLETS": "pallet",
    "CASE": "case",
    "CASES": "case",
    "CS": "case",
    "CRATE": "crate",
    "CRATES": "crate",
    "DRUM": "drum",
    "DRUMS": "drum",
    "BAG": "bag",
    "BAGS": "bag",
    "SACK": "bag",
    "SACKS": "bag",
    "ROLL": "roll",
    "ROLLS": "roll",
    "BUNDLE": "bundle",
    "BUNDLES": "bundle",
    "PPT": "package",
    "PACKAGE": "package",
    "PACKAGES": "package",
    "PKGS": "package",
    "PKG": "package",
    "ST": "set",
    "SETS": "set",
    "SET": "set",
    # German and French package words: a German invoice's "100 KARTONS" and a
    # French "100 COLIS" are the same packaging as an English "100 CARTONS",
    # so they must compare on the unit rather than reading as "unknown".
    "KARTON": "carton",
    "KARTONS": "carton",
    "KISTE": "case",
    "KISTEN": "case",
    "PALETTE": "pallet",
    "PALETTEN": "pallet",
    "SÄCKE": "bag",
    "SACKE": "bag",
    "ROLLE": "roll",
    "ROLLEN": "roll",
    "BUND": "bundle",
    "BÜNDEL": "bundle",
    "BUNDEL": "bundle",
    "PAKET": "package",
    "PAKETE": "package",
    "GEBINDE": "package",
    "STÜCK": "piece",
    "STUECK": "piece",
    "COLIS": "package",
    "PALETTES": "pallet",
    "CAISSE": "case",
    "CAISSES": "case",
    "CAGEOT": "crate",
    "CAGEOTS": "crate",
    "SACS": "bag",
    "ROULEAU": "roll",
    "ROULEAUX": "roll",
}

# Longest-first so "PIECES" wins over "PC" inside the same word boundary.
_UNIT_ALT = "|".join(sorted((re.escape(k) for k in PACKAGE_KINDS), key=len, reverse=True))
_UNIT_RE = re.compile(rf"(?<![A-Za-z])({_UNIT_ALT})(?![A-Za-z])", re.IGNORECASE)

# Weight/volume units that must not be mistaken for a package count.
_NOT_A_COUNT = re.compile(r"(?<![A-Za-z])(KGS?|LBS?|MT|CBM|M3|TONS?|G|GRAMS?)(?![A-Za-z])", re.I)


@dataclass
class QuantityResult:
    raw: str
    count: int | None = None
    unit: str | None = None
    canonical: str | None = None
    compare_key: str | None = None
    rules_applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.count is not None


def normalize_quantity(raw: str | None) -> QuantityResult:
    text = (raw or "").strip()
    res = QuantityResult(raw=text)
    if not text:
        res.warnings.append("empty value")
        return res

    if _NOT_A_COUNT.search(text) and not _UNIT_RE.search(text):
        res.warnings.append(f"'{text}' looks like a weight/volume figure, not a count")
        return res

    token = extract_number_token(text)
    if token is None:
        res.warnings.append(f"no number found in '{text}'")
        return res

    num = parse_number(token)
    if not num.ok or num.value is None:
        res.warnings.extend(num.warnings)
        return res
    res.rules_applied.extend(num.rules_applied)
    if num.ambiguous:
        res.warnings.extend(num.warnings)

    if not num.is_integer:
        res.warnings.append(f"non-integer count '{num.value:g}' in '{text}'")
    res.count = int(round(num.value))

    unit_match = _UNIT_RE.search(text)
    if unit_match:
        res.unit = PACKAGE_KINDS[unit_match.group(1).upper()]
        res.rules_applied.append("package_kind_detected")
    else:
        res.rules_applied.append("no_package_kind")

    if res.unit:
        res.canonical = f"{res.count:,g} {res.unit}{'s' if res.count != 1 and not res.unit.endswith('s') else ''}"
        res.compare_key = f"count:{res.count}:{res.unit}"
    else:
        res.canonical = f"{res.count:,g}"
        res.compare_key = f"count:{res.count}:unknown"
    return res
