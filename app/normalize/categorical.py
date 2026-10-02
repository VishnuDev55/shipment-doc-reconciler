"""Normalizers for the categorical fields: Incoterms, references, origin.

No arithmetic anywhere: these are identifier-shaped values whose whole job is to
make the same fact written three ways compare equal. Each returns a `compare_key`
built only from what was printed, and warns rather than guesses when it cannot
recognize the input -- a value quietly mapped to the wrong country is worse for a
customs check than one flagged as unread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Res:
    """Same shape the other normalizer modules return, so normalize_field can
    read canonical / compare_key / rules_applied / warnings / ok uniformly."""

    raw: str
    canonical: str | None = None
    compare_key: str | None = None
    rules_applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ok: bool = True

_PARENTHETICAL = re.compile(r"\(([^()]*)\)")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_INCOTERM_CODE = re.compile(r"\b(EX[WZ]|FCA|FAS|FOB|CPT|CIP|CFR|CIF|DAP|DPU|DDP|DAF|DES|DEQ)\b")
# The place is whatever follows whichever code matched, so retired codes keep it
# too -- a code-specific alternation silently dropped the place from
# "DAF someplace", which is how this was first written and tested.
_PLACE_TAIL = r"[\s,:-]*([A-Z][A-Z .,'-]{1,40})"

# Incoterms are revised, and the 2010 set renamed some terms. Two codes denote the
# same obligation for reconciliation purposes; the revision is recorded, not
# silently unified.
_INCOTERM_REVISION = {"DAF": "1990/2000 revision", "DES": "1990/2000 revision",
                      "DEQ": "1990/2000 revision"}

# Label words on a purchase-order reference are scaffolding, not identity.
_REF_LABELS = frozenset({
    "PO", "PON", "PURCHASE", "ORDER", "ORD", "REF", "REFS", "REFERENCE",
    "NO", "NR", "NUM", "NUMBER", "R",
})

_COUNTRY_WORDS = {
    "DE": {"DE", "DEU", "GERMANY", "GERMAN", "BUNDESREPUBLIK DEUTSCHLAND", "DEUTSCHLAND"},
    "CN": {"CN", "CHN", "CHINA", "CHINESE", "PEOPLE S REPUBLIC OF CHINA"},
    "US": {"US", "USA", "U S A", "UNITED STATES", "AMERICAN", "U S"},
    "IN": {"IN", "IND", "INDIA", "INDIAN"},
    "NL": {"NL", "NLD", "NETHERLANDS", "HOLLAND", "DUTCH"},
    "GB": {"GB", "UK", "GBR", "UNITED KINGDOM", "BRITISH", "ENGLAND", "GREAT BRITAIN"},
    "FR": {"FR", "FRA", "FRANCE", "FRENCH"},
    "JP": {"JP", "JPN", "JAPAN", "JAPANESE"},
    "KR": {"KR", "KOR", "KOREA", "SOUTH KOREA", "REPUBLIC OF KOREA", "KOREAN"},
    "VN": {"VN", "VNM", "VIETNAM", "VIET NAM", "VIETNAMESE"},
    "IT": {"IT", "ITA", "ITALY", "ITALIAN"},
    "TR": {"TR", "TUR", "TURKEY", "TÜRKIYE", "TURKISH"},
    "TW": {"TW", "TWN", "TAIWAN", "TAIWANESE"},
    "MY": {"MY", "MYS", "MALAYSIA", "MALAYSIAN"},
    "MX": {"MX", "MEX", "MEXICO", "MEXICAN"},
    "BR": {"BR", "BRA", "BRAZIL", "BRAZILIAN"},
    "PL": {"PL", "POL", "POLAND", "POLISH"},
    "ES": {"ES", "ESP", "SPAIN", "SPANISH"},
    "SE": {"SE", "SWE", "SWEDEN", "SWEDISH"},
    "CH": {"CH", "CHE", "SWITZERLAND", "SWISS"},
    "AT": {"AT", "AUT", "AUSTRIA", "AUSTRIAN"},
    "BE": {"BE", "BEL", "BELGIUM", "BELGIAN"},
    "SG": {"SG", "SGP", "SINGAPORE", "SINGAPORIAN"},
    "TH": {"TH", "THA", "THAILAND", "THAI"},
}
# Longest-first so "UNITED KINGDOM" is not consumed as "UNITED STATES" reasoning
# nor "SOUTH KOREA" as "KOREA" by an earlier match.
_BY_WORD = {
    word: code
    for code, words in _COUNTRY_WORDS.items()
    for word in words
}


def normalize_incoterm(text: str) -> Res:
    raw = " ".join(text.split())
    upper = raw.upper()
    warnings: list[str] = []

    match = _INCOTERM_CODE.search(upper)
    if match is None:
        # Keep the printed string comparable to itself rather than inventing a
        # code the document did not state.
        return Res(
            raw=text,
            canonical=raw.title(),
            compare_key=f"incoterm?:{_NON_ALNUM.sub('', upper)}",
            warnings=[f"no recognised Incoterms code in {raw!r}; compared verbatim"],
        )

    code = match.group(0)
    if code in _INCOTERM_REVISION:
        warnings.append(f"{code} is from the {_INCOTERM_REVISION[code]}")

    tail = upper[match.end():]
    place_match = re.match(_PLACE_TAIL, tail)
    place = " ".join(place_match.group(1).split()).strip(" .,") if place_match else ""
    if not place:
        warnings.append("Incoterms code printed without a named place")

    rules = [f"code {code}"] + ([f"place {place}"] if place else [])
    return Res(
        raw=text,
        canonical=f"{code} {place}".strip(),
        compare_key=f"incoterm:{code}:{place}",
        rules_applied=rules,
        warnings=warnings,
    )


def normalize_reference(text: str) -> Res:
    """Purchase-order and similar identifiers: punctuation is noise, digits are not.

    Deliberately not fuzzy-compared. "4471-B" and "4471-C" are two different
    orders, and a similarity score would call them near matches.
    """
    # Label words are scaffolding, not identity: leaving them in meant
    # "PO No. 4471-B" and "4471B" -- the same order -- compared unequal, which is
    # the one thing this normalizer exists to prevent.
    upper = " ".join(text.split()).upper()
    tokens = re.split("[^A-Z0-9]+", _PARENTHETICAL.sub("", upper))
    tight = "".join(tok for tok in tokens if tok and tok not in _REF_LABELS)
    warnings = []
    if not tight:
        return Res(raw=text, canonical=upper, compare_key=None,
                   warnings=[f"no usable characters in {text!r}"])
    digits = sum(ch.isdigit() for ch in tight)
    if digits < 3:
        warnings.append("reference has fewer than 3 digits; check it is not a partial read")
    return Res(raw=text, canonical=upper, compare_key=f"ref:{tight}",
               rules_applied=[f"stripped to {tight}"], warnings=warnings)


def normalize_country(text: str) -> Res:
    # "Made in Germany" and "Country of origin: Germany" must land on one key, so
    # the scaffolding comes off before anything is matched.
    stripped = strip_made_in(text)
    upper = " ".join(stripped.upper().split())
    compact = " ".join(_NON_ALNUM.sub(" ", upper).split())

    # Two- and three-letter codes first: an ISO code on the document is the
    # strongest signal and must not be shadowed by a substring of a longer name.
    code = None
    if compact in _BY_WORD:
        code = _BY_WORD[compact]
    else:
        tokens = [t for t in compact.split(" ") if t]
        for token in sorted(tokens, key=len, reverse=True):
            if token in _BY_WORD:
                code = _BY_WORD[token]
                break
        if code is None:
            for word in sorted(_BY_WORD, key=len, reverse=True):
                if " " in word and word in compact:
                    code = _BY_WORD[word]
                    break

    if code is None:
        return Res(
            raw=text,
            canonical=upper.title(),
            compare_key=f"country?:{compact}",
            warnings=[f"country {text!r} not recognised; compared verbatim"],
        )

    warnings = [f"matched {upper!r} to {code}"] if compact != code else []
    return Res(raw=text, canonical=code, compare_key=f"country:{code}",
               rules_applied=[f"normalised to ISO-2 {code}"], warnings=warnings)


def strip_made_in(text: str) -> str:
    """Remove the "MADE IN" scaffolding before matching.

    Kept separate from normalize_country's call sites' expectations: it exists so
    'Made in Germany' and 'Country of origin: Germany' land on the same key.
    """
    out = re.sub(r"\bMADE\s+IN\b|\bORIGIN\b|\bPAYS\s+D.?ORIGINE?\b", " ", text, flags=re.I)
    return _PARENTHETICAL.sub(lambda m: m.group(1), out).strip(" :,")
