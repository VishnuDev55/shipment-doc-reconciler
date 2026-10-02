"""Number parsing for international trade documents.

The single highest-value rule in this project: the same quantity is printed as
12,450.00 / 12.450,00 / 12 450,00 / 12'450.00 depending on who made the
document. A silent misparse here corrupts every comparison downstream, so
ambiguity is never resolved silently — it is resolved to the thousands
convention AND flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# OCR digit confusions. Applied only as a last resort.
OCR_MAP = {"O": "0", "I": "1", "l": "1", "S": "5", "B": "8", "Z": "2"}


@dataclass
class NumberResult:
    raw: str
    value: float | None = None
    ambiguous: bool = False
    is_integer: bool = False
    rules_applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value is not None


def _strip_grouping_separators(text: str) -> tuple[str, bool]:
    """Replace space/apostrophe thousands grouping with nothing.

    Only when the separator is followed by exactly three digits, so a decimal
    comma is never mistaken for grouping.
    """
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        changed = True
        return ""

    out = re.sub(r"[\s'\u00a0\u202f](?=\d{3}(?:\D|$))", repl, text)
    return out, changed


def _parse_digits_only(text: str) -> tuple[float | None, bool]:
    """Parse a digit string that may use , and . as separators.

    Rules, in order:
      * both ',' and '.' present -> the RIGHTMOST separator is the decimal
        mark and the other is thousands grouping. This makes "12.450,00" and
        "12,450.00" both parse to 12450.00.
      * exactly one separator -> ambiguous only when it is followed by exactly
        three digits and the total digit count is <= 7 ("1,200").
      * anything else -> no parse.
    """
    if not text:
        return None, False

    has_comma = "," in text
    has_dot = "." in text

    if has_comma and has_dot:
        if text.count(",") + text.count(".") > 3:
            return None, False
        if text[-1] not in ",.":
            rightmost = max(text.rfind(","), text.rfind("."))
            int_part = text[:rightmost].replace(",", "").replace(".", "")
            frac_part = text[rightmost + 1 :]
            if not int_part.isdigit() or not frac_part.isdigit():
                return None, False
            try:
                return float(f"{int_part}.{frac_part}"), False
            except ValueError:
                return None, False
        return None, False

    if text.count(",") > 1 or text.count(".") > 1:
        # "1,234,567" is pure thousands grouping; "1.2.3" is garbage.
        sep = "," if has_comma else "."
        parts = text.split(sep)
        if all(len(p) == 3 for p in parts[1:]) and parts[0].isdigit():
            digits = "".join(parts)
            return (float(digits), False) if digits.isdigit() else (None, False)
        return None, False

    if has_comma or has_dot:
        sep = "," if has_comma else "."
        int_part, _, frac = text.partition(sep)
        if not int_part.isdigit() or (frac and not frac.isdigit()):
            return None, False
        total_digits = len(int_part) + len(frac)
        # Ambiguous only when a single separator is followed by exactly three
        # digits and the whole thing is short. A leading-zero integer part
        # ("0.500") is unambiguously a decimal, never thousands grouping.
        ambiguous = (
            len(frac) == 3 and total_digits <= 7 and int_part != "0"
        )
        try:
            if ambiguous:
                # Spec: default to the THOUSANDS convention, and warn loudly.
                # "1,200" and "1.200" both -> 1200.
                return float(int_part + frac), True
            return float(f"{int_part}.{frac}" if frac else int_part), False
        except ValueError:
            return None, False

    try:
        return float(text), False
    except ValueError:
        return None, False


def parse_number(raw: str | None) -> NumberResult:
    if raw is None:
        return NumberResult(raw="", warnings=["no value"])
    original = str(raw).strip()
    res = NumberResult(raw=original)
    if not original:
        res.warnings.append("empty value")
        return res

    text = original.replace("\u00a0", " ")

    # Rule: space/apostrophe grouping -> strip and recurse.
    stripped, did_strip = _strip_grouping_separators(text)
    if did_strip:
        inner = parse_number(stripped)
        if inner.ok:
            res.value = inner.value
            res.ambiguous = inner.ambiguous
            res.is_integer = inner.is_integer
            res.rules_applied = ["strip_grouping", *inner.rules_applied]
            res.warnings = list(inner.warnings)
            return res
        text = stripped

    value, ambiguous = _parse_digits_only(text)
    if value is None:
        # Last resort: OCR digit repair, and only if EXACTLY ONE single
        # substitution produces a valid parse. Never applied to a token that
        # already parses cleanly.
        repaired: list[tuple[str, float, bool]] = []
        seen: set[str] = set()
        for i, ch in enumerate(text):
            if ch in OCR_MAP:
                cand = text[:i] + OCR_MAP[ch] + text[i + 1 :]
                if cand in seen:
                    continue
                seen.add(cand)
                v, amb = _parse_digits_only(cand)
                if v is not None:
                    repaired.append((cand, v, amb))
        if len(repaired) == 1:
            cand, value, ambiguous = repaired[0]
            res.rules_applied.append("ocr_repair")
            res.warnings.append(
                f"OCR repair applied: '{text}' read as '{cand}'"
            )
        elif len(repaired) > 1:
            res.warnings.append(
                f"ambiguous OCR corruption in '{text}': {len(repaired)} single-char fixes possible"
            )
            return res

    if value is None:
        res.warnings.append(f"not a parseable number: '{text}'")
        return res

    if ambiguous:
        res.ambiguous = True
        res.rules_applied.append("assume_thousands_grouping")
        res.warnings.append(
            f"ambiguous separator in '{original}': read as {value:g} "
            "(thousands convention); could be a European decimal"
        )

    res.value = value
    res.is_integer = float(value).is_integer()
    return res


def extract_number_token(text: str) -> str | None:
    """Pull the first numeric-looking token out of a string like '12,450.00 KGS'."""
    if not text:
        return None
    m = re.search(r"\d[\d.,' \u00a0\u202f]*\d|\d", text)
    if not m:
        return None
    return m.group(0).strip()
