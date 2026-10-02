"""Party-name normalization (consignee).

Word reordering and extra legal-entity suffixes are the dominant real-world
variance across trade documents, so exact string matching is useless here.
This module produces a folded, suffix-stripped token set; the comparator
scores pairs of those with rapidfuzz token_set_ratio.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Stripped repeatedly from the END of the name only — removing "Inc" from the
# middle of a name would corrupt it. Deliberately excludes connectors, which
# are handled as trailing tokens below.
LEGAL_SUFFIXES = {
    "LTD", "LTDA", "LIMITED", "LLC", "LLP", "INC", "CO", "CORP", "CORPORATION",
    "COMPANY", "PTE", "SDN", "BHD", "GMBH", "MBH", "AG", "SA", "SRL", "SPA",
    "NV", "BV", "AB", "AS", "OOO", "PTY", "PLC", "KG", "OHG", "SPZOO", "SRO",
    "DOOO", "TNC", "CV", "SL", "SNC", "SAS", "EURL", "JSC",
}

# Punctuation is dropped except & and -, which carry meaning in company names.
_STRIP_PUNCT = re.compile(r"[^\w&\-]+", re.UNICODE)


@dataclass
class PartyResult:
    raw: str
    normalized: str = ""
    tokens: list[str] = field(default_factory=list)
    canonical: str | None = None
    compare_key: str | None = None
    rules_applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.tokens)


def fold_accents(text: str) -> str:
    """NFKD decompose, then drop the combining marks. 'Müller' -> 'Muller'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_party(raw: str | None) -> PartyResult:
    text = (raw or "").strip()
    res = PartyResult(raw=text)
    if not text:
        res.warnings.append("empty value")
        return res

    out = fold_accents(text).upper()
    res.rules_applied.append("fold_accents_upper")

    out = re.sub(r"\bAND\b", "&", out)
    out = _STRIP_PUNCT.sub(" ", out).strip()
    res.rules_applied.append("strip_punctuation_keep_amp_hyphen")

    tokens = out.split()

    stripped = False
    while tokens and tokens[-1].rstrip(".") in LEGAL_SUFFIXES:
        tokens.pop()
        stripped = True
    while tokens and tokens[-1] == "&":
        tokens.pop()
        stripped = True
    if stripped:
        res.rules_applied.append("strip_legal_suffix")

    res.tokens = tokens
    res.normalized = " ".join(tokens)

    if not tokens:
        res.warnings.append(f"'{text}' reduced to nothing after suffix stripping")
        return res

    # compare_key is the SORTED token set, so a pure reordering matches
    # exactly; anything fuzzier is the comparator's job.
    res.compare_key = "party:" + " ".join(sorted(tokens))
    res.canonical = text
    return res
