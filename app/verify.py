"""Check a quoted line against what the page actually says.

The extraction contract already asks for three things per field: the value, the
verbatim line it came from, and the page. Two of those are trusted; this module
stops trusting the third. It answers one question -- is this quote real? -- and
says so in a way the report can show.

Four outcomes, because three would force a lie somewhere:

  ``absent``          nothing to check; the field reports no value
  ``verified``        the quote and the value are both on the page
  ``unverified``      a text source exists and the quote or the value is not in it
                      -> the field is dropped from comparison and shown as absent
  ``not_checkable``   no text source exists (a photo, a scan, a flat image)
                      -> the field stays in the comparison, labelled

The distinction between the last two is the whole point. Treating "we could not
look" as "we looked and it was wrong" would quietly empty every report built
from phone photos, which is most of them. Treating it as "fine" would claim a
check that never happened.

Two gates, not one. A quote can be real while the value next to it is invented
-- copy a genuine line, change the number -- so the value is checked against the
page separately. And because models reformat what they copy ("12.450,00" for a
page printing "12,450.00"), the value gate compares digits with grouping
punctuation removed rather than demanding a literal string, so a reformat is not
mistaken for a fabrication.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from rapidfuzz import fuzz

from .models import ExtractedField, ExtractionResponse, FieldVerification
from .textlayer import PageText

_WS = re.compile(r"\s+")
# Everything that is not a letter or a digit: commas, dots, spaces, apostrophes.
# Stripping these is what makes 12,450.00 / 12.450,00 / 12 450,00 compare equal,
# which is the same tolerance the normalizers apply to values.
_NON_ALNUM = re.compile(r"[^0-9a-z]+", re.UNICODE)


def normalize_for_match(text: str) -> str:
    """Accent-folded, case-folded, whitespace-collapsed -- punctuation kept.

    Deliberately less aggressive than the description fold in
    ``app.lineitems``: here we are asking "did this string get copied from
    there", and punctuation is part of the evidence.
    """
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _WS.sub(" ", folded.casefold()).strip()


def squash(text: str) -> str:
    """Normalized *and* separator-free, for comparing numbers written apart."""
    return _NON_ALNUM.sub("", normalize_for_match(text).replace("'", ""))


def _page_of(pages: Sequence[PageText], index: int) -> list[PageText]:
    """The cited page first, then the rest.

    A model that quotes a real line but reports the wrong page has made a
    navigation error, not a fabrication, and the reader should still get the
    value. The tier records which happened.
    """
    cited = [p for p in pages if p.index == index]
    return cited + [p for p in pages if p.index != index]


def _spread(quote: str, page: PageText) -> bool:
    """Do the quote's words appear on the page, in order, across line breaks?

    A table cell has no line of its own that reads like a sentence, so a model
    citing one often joins the column header to the value: for a certificate of
    origin whose layer reads `3. HS code  4. Origin criterion` then `8517.62 P`,
    the honest quote "3. HS CODE 8517.62" matches no single line. Rejecting that
    would discard a correct extraction, which is a worse failure than the one
    this check exists to catch -- so the words are allowed to come from
    consecutive lines, in order, of the same page.
    """
    tokens = normalize_for_match(quote).split()
    if len(tokens) < 2:
        return False
    stream = normalize_for_match(page.text).split()
    i = 0
    for token in tokens:
        while i < len(stream) and stream[i] != token:
            i += 1
        if i >= len(stream):
            return False
        i += 1
    return True


def _match_quote(quote: str, pages: Sequence[PageText], threshold: int
                 ) -> tuple[str, str, float, int]:
    """(tier, page_source, score, page_index). tier '' means no match at all."""
    needle = normalize_for_match(quote)
    if not needle:
        return "", "", 0.0, 0
    for page in pages:
        lines = [normalize_for_match(ln) for ln in page.lines if ln.strip()]
        if any(needle in ln for ln in lines):
            return "line", page.source, 100.0, page.index
        if needle in normalize_for_match(page.text):
            # Label cell and value cell of one table row: the text layer splits
            # them across lines, a person transcribing the row writes them as one.
            return "row", page.source, 100.0, page.index
        squashed = squash(quote)
        if squashed and squashed in squash(page.text):
            return "row", page.source, 100.0, page.index
        if _spread(quote, page):
            return "spread", page.source, 100.0, page.index
        best = max((fuzz.partial_ratio(needle, ln) for ln in lines), default=0.0)
        if best >= threshold:
            return "fuzzy", page.source, float(best), page.index
    return "", "", 0.0, 0


def _value_on_page(value: str, pages: Sequence[PageText], threshold: int) -> bool:
    """The second gate: the value itself has to be somewhere in the text."""
    if not value.strip():
        return True                      # present with an empty value is its own problem
    if squash(value) and any(squash(value) in squash(p.text) for p in pages):
        return True
    needle = normalize_for_match(value)
    return any(fuzz.partial_ratio(needle, normalize_for_match(p.text)) >= threshold
               for p in pages)


def verify_field(field: ExtractedField, pages: Sequence[PageText], *,
                 threshold: int = 90) -> FieldVerification:
    """One field against one document's text. Never raises."""
    if not field.present or field.value is None:
        return FieldVerification(state="absent")

    usable = [p for p in pages if p.usable]
    if not usable:
        return FieldVerification(state="not_checkable", tier="no_text_source",
                                 detail="no text source for these pages")

    ordered = _page_of(usable, field.page_index)
    tier, source, score, matched_page = _match_quote(field.evidence_text, ordered,
                                                     threshold)
    if not tier:
        return FieldVerification(state="unverified", tier="quote_absent", score=0.0,
                                 detail="the quoted line does not appear on the page")

    cited_ok = matched_page == field.page_index
    if not _value_on_page(field.value or "", ordered, threshold):
        return FieldVerification(state="unverified", tier="value_absent", score=score,
                                 matched_page=matched_page,
                                 detail="the quoted line is real but the value is not "
                                        "printed on any page")

    detail = ""
    if not cited_ok:
        detail = f"found on page {matched_page}, not the cited page {field.page_index}"
    return FieldVerification(state="verified", tier=f"{tier}:{source}", score=score,
                             matched_page=matched_page, detail=detail)


def verify_document(response: ExtractionResponse, pages: Sequence[PageText], *,
                    threshold: int = 90) -> dict[str, FieldVerification]:
    """Every field of one document, in contract order."""
    return {
        key: verify_field(response.fields[key], pages, threshold=threshold)
        for key in response.fields
    }
