"""Money normalization: currency detection + number parse. Never FX conversion.

Different currencies across documents is always a hard conflict with the
reason "cannot compare across currencies". That is both the correct behaviour
and zero effort — building FX conversion here would add a network dependency
and a rate-freshness problem for no demo value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .number import parse_number

ISO_CODE = re.compile(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])")

CURRENCY_SYMBOLS = {
    "US$": "USD",
    "A$": "USD",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "R$": "BRL",
    "₹": "INR",
    "元": "CNY",
    "CHF": "CHF",
    "CNY": "CNY",
}

# Three-letter runs that look like ISO 4217 but are not currencies.
NOT_CURRENCY = {
    "FOB", "CIF", "CFR", "EXW", "FCA", "FAS", "DAP", "DDP", "DPU", "CPT", "CIP",
    "SAY", "ONLY", "TOTAL", "AMT", "VAT", "GST", "TAX", "NET", "GRS", "KGS",
    "LBS", "MTQ", "PCQ", "SET", "THE", "AND", "FOR", "PER", "PVT", "LTD", "INC",
    "CORP", "GMBH", "SRL", "BHD", "SDN", "PTE", "TNC", "PLC", "LLC", "CBM", "HS",
    "SC", "NO", "AT", "IN", "OF", "TO", "FROM", "SAY", "USD$", "CODE", "DESCRIPTION",
}

INCOTERM = re.compile(
    r"(?<![A-Za-z])(?:FOB|CIF|CFR|EXW|FCA|FAS|DAP|DPU|DDP|CPT|CIP)(?![A-Za-z])",
    re.IGNORECASE,
)
# The spelled-out amount line ("SAY US DOLLARS ... ONLY") often repeats the
# figure in words and sometimes in digits, so it is removed before parsing.
SAY_ONLY = re.compile(r"\bSAY\b.*?\bONLY\b", re.IGNORECASE | re.DOTALL)
NUMERIC_TOKEN = re.compile(r"\d[\d.,' \u00a0\u202f]*\d|\d")


@dataclass
class MoneyResult:
    raw: str
    amount: float | None = None
    currency: str | None = None
    canonical: str | None = None
    compare_key: str | None = None
    rules_applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.amount is not None


def _detect_currency(text: str) -> str | None:
    for m in ISO_CODE.finditer(text):
        code = m.group(1).upper()
        if code in NOT_CURRENCY or len(code) != 3:
            continue
        return code
    for sym, code in sorted(CURRENCY_SYMBOLS.items(), key=lambda kv: -len(kv[0])):
        if sym in text:
            return code
    return None


def normalize_money(raw: str | None) -> MoneyResult:
    text = (raw or "").strip()
    res = MoneyResult(raw=text)
    if not text:
        res.warnings.append("empty value")
        return res

    # Currency is detected on the FULL string, before any prose is stripped,
    # so "FOB SHANGHAI USD 8,420.00" still yields USD.
    res.currency = _detect_currency(text)
    if res.currency:
        res.rules_applied.append("currency_detected")
    else:
        res.warnings.append(
            f"no currency code or symbol found in '{text}'; cannot confirm two "
            "figures are in the same currency"
        )

    cleaned = text
    if SAY_ONLY.search(cleaned):
        cleaned = SAY_ONLY.sub(" ", cleaned)
        res.rules_applied.append("strip_prose")
    if INCOTERM.search(cleaned):
        cleaned = INCOTERM.sub(" ", cleaned)
        if "strip_prose" not in res.rules_applied:
            res.rules_applied.append("strip_prose")

    # The amount is the LAST numeric token in the string: totals are printed
    # after any labels, codes, or Incoterms.
    tokens = NUMERIC_TOKEN.findall(cleaned)
    if not tokens:
        res.warnings.append(f"no amount found in '{text}'")
        return res

    num = parse_number(tokens[-1].strip())
    if not num.ok or num.value is None:
        res.warnings.extend(num.warnings or [f"unparseable amount in '{text}'"])
        return res
    res.rules_applied.extend(num.rules_applied)
    if num.ambiguous:
        res.warnings.extend(num.warnings)

    res.amount = round(num.value, 2)
    if res.currency:
        res.canonical = f"{res.amount:,.2f} {res.currency}"
        res.compare_key = f"money:{res.currency}:{res.amount:.2f}"
    else:
        res.canonical = f"{res.amount:,.2f}"
        res.compare_key = f"money:UNKNOWN:{res.amount:.2f}"
    return res
