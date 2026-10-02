"""ISO 6346 container number validation, implemented in full.

This is the best feature in the project: a container number is
*self-verifying*. The 11th character is a mod-11 check digit over the first
ten, so a single misread digit can be detected with zero ground truth and
zero second model call. When two documents disagree and exactly one number
fails its own checksum, the checksum arbitrates the conflict.

Letter values per ISO 6346: A=10, B=12, C=13 ... skipping every multiple of
11 (11, 22, 33). Each of the first ten characters is weighted by 2**position;
the check digit is (weighted sum mod 11) mod 10.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_LETTER_VALUE: dict[str, int] = {}
_value = 10
for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    if _value % 11 == 0:
        _value += 1
    _LETTER_VALUE[_ch] = _value
    _value += 1

PATTERN = re.compile(r"^[A-Z]{4}[0-9]{7}$")
_ALNUM = re.compile(r"[^A-Z0-9]")


@dataclass
class ContainerResult:
    raw: str
    canonical: str | None = None
    compare_key: str | None = None
    pattern_valid: bool = False
    check_digit_valid: bool = False
    expected_check_digit: int | None = None
    actual_check_digit: int | None = None
    owner_prefix: str | None = None
    rules_applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.pattern_valid and self.check_digit_valid


def char_value(ch: str) -> int:
    if ch.isdigit():
        return int(ch)
    return _LETTER_VALUE[ch]


def compute_check_digit(body10: str) -> int:
    """ISO 6346 check digit for the first 10 characters."""
    total = sum(char_value(c) * (2**i) for i, c in enumerate(body10))
    return (total % 11) % 10


def normalize_container(raw: str | None) -> ContainerResult:
    text = (raw or "").strip()
    res = ContainerResult(raw=text)
    if not text:
        res.warnings.append("empty value")
        return res

    cleaned = _ALNUM.sub("", text.upper())
    if cleaned != text:
        res.rules_applied.append("strip_to_alphanumeric_upper")

    res.canonical = cleaned
    if not PATTERN.match(cleaned):
        res.pattern_valid = False
        res.check_digit_valid = False
        res.detail = (
            f"'{cleaned}' does not match ISO 6346 shape (4 letters + 7 digits)"
        )
        res.warnings.append(res.detail)
        # Still give the UI something to compare on.
        res.compare_key = f"container:{cleaned}" if cleaned else None
        res.rules_applied.append("iso6346_pattern_failed")
        return res

    res.pattern_valid = True
    res.owner_prefix = cleaned[:4]
    body, actual = cleaned[:10], int(cleaned[10])
    expected = compute_check_digit(body)

    res.expected_check_digit = expected
    res.actual_check_digit = actual
    res.compare_key = f"container:{cleaned}"
    res.rules_applied.append("iso6346_check_digit")

    if expected == actual:
        res.check_digit_valid = True
        res.detail = "ISO 6346 check digit verified"
    else:
        res.check_digit_valid = False
        res.detail = (
            f"ISO 6346 check digit mismatch: {cleaned} ends in {actual} but the "
            f"computed check digit is {expected} (likely a misread of one character)"
        )
        res.warnings.append(res.detail)
    return res
