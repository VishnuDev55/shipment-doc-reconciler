"""Line-item and SKU matching across documents. Opt-in, deterministic, no model.

The scalar fields answer "does the total agree". This answers the question a
reconciliation clerk actually asks on the phone: *which line* is different. A
shipment where the invoice and the packing list both say 100 cartons can still
be wrong -- 60 of one item and 40 of another on one document, 50/50 on the
other, and every total matches.

Matching is done here, in Python, on values the model only transcribed. Two
items are the same item when they carry the same SKU, or when they carry no SKU
and their descriptions fold to the same tokens. Nothing is scored on similarity:
"CNC aluminium housing H-40" and "CNC aluminium housing H-41" are two parts, and
a fuzzy band would call them a near match and hide the one error that matters.

Quantity comparison is exact on the parsed number, because a line quantity is a
count, not a measurement -- there is no tolerance to grant. The unit is compared
too: 60 pieces against 6 cartons of 10 is the same goods on a different basis,
and that is reported as a basis difference rather than a conflict.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .normalize.quantity import normalize_quantity

# Words that appear on almost every goods line and carry no identity. Removing
# them is what lets "100 CTN CNC HOUSING" and "CNC HOUSING (100 CARTONS)" fold
# together. Numbers are never in this set -- a number is the information.
_NOISE_TOKENS = frozenset({
    "ITEM", "ITEMS", "NO", "NR", "DESCRIPTION", "OF", "THE", "AND", "TOTAL",
    "CTN", "CTNS", "CARTON", "CARTONS", "PCS", "PC", "PIECE", "PIECES", "SET",
    "SETS", "PKG", "PKGS", "PACK", "PACKS", "BOX", "BOXES", "PALLET", "PALLETS",
    "SAY", "APPROX", "CA", "CIRCA", "EACH", "PER",
})

_PUNCT = re.compile(r"[^A-Z0-9 ]+")


def fold_description(text: str) -> str:
    """Accent-folded, upper-cased, punctuation-free, unit-and-label-free tokens.

    Sorted and de-duplicated, because a wrapped cell and a single-line cell list
    the same words in different orders, and "HOUSING HOUSING" printed across two
    lines is one item. Order carries no identity in a goods description; the
    numbers do, and every number survives.
    """
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    upper = re.sub(r"\s+", " ", folded.upper().replace("&", " ")).strip()
    upper = _PUNCT.sub(" ", upper)
    tokens = {t for t in upper.split(" ") if t and t not in _NOISE_TOKENS}
    return " ".join(sorted(tokens))


@dataclass
class LineItem:
    """One transcribed line. `raw_*` is what the document printed."""

    raw_description: str = ""
    raw_sku: str = ""
    raw_quantity: str = ""

    @property
    def description(self) -> str:
        return fold_description(self.raw_description)

    @property
    def sku(self) -> str:
        return re.sub(r"[^A-Z0-9]+", "", (self.raw_sku or "").upper())

    @property
    def key(self) -> str:
        """What makes this item itself. SKU wins when the document states one."""
        return f"sku:{self.sku}" if self.sku else f"desc:{self.description}"

    @property
    def quantity(self) -> float | None:
        return normalize_quantity(self.raw_quantity).count

    @property
    def unit(self) -> str:
        result = normalize_quantity(self.raw_quantity)
        return (result.unit or "").upper()

    @property
    def label(self) -> str:
        return self.raw_sku.strip() or self.raw_description.strip() or "(blank line)"


@dataclass
class ItemMatch:
    """One item, as seen by the documents that stated it."""

    key: str
    label: str
    quantities: dict[str, str] = field(default_factory=dict)   # doc -> printed
    documents: list[str] = field(default_factory=list)
    status: str = "matched"      # matched | quantity_conflict | unit_differs | only_here
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == "matched"


@dataclass
class LineItemReport:
    """The whole cross-document picture, and how much of it can be trusted."""

    matches: list[ItemMatch] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def matched(self) -> int:
        return sum(1 for m in self.matches if m.status == "matched")

    @property
    def conflicts(self) -> int:
        return sum(1 for m in self.matches
                   if m.status in ("quantity_conflict", "unit_differs"))

    @property
    def uncheckable(self) -> int:
        return sum(1 for m in self.matches if m.status == "only_here")

    @property
    def usable(self) -> bool:
        """Fewer than two documents with items means there is nothing to match."""
        return len(self.documents) >= 2


def match_line_items(sets: dict[str, list[LineItem]]) -> LineItemReport:
    """Compare item sets across documents. `sets` maps a document label to items.

    Insertion order is preserved throughout, so the same input always produces
    the same report -- a reconciliation artifact that reorders itself between two
    views of the same data cannot be compared to itself.
    """
    report = LineItemReport(documents=[d for d, items in sets.items() if items])
    if not report.documents:
        report.skipped_reason = "no document returned any line items"
        return report
    if len(report.documents) < 2:
        report.skipped_reason = (
            f"only {report.documents[0]} returned line items, so there is nothing "
            "to match them against"
        )

    ordered: dict[str, ItemMatch] = {}
    for doc in sets:
        for item in sets[doc]:
            if not item.description and not item.sku:
                continue                      # a blank row, not an item
            match = ordered.get(item.key)
            if match is None:
                match = ItemMatch(key=item.key, label=item.label)
                ordered[item.key] = match
            # Guarded on the match, not on a per-document set: a document with
            # three rows must credit itself to all three.
            if doc not in match.documents:
                match.documents.append(doc)
            printed = item.raw_quantity or "(no quantity printed)"
            # A repeated key inside one document is itself a finding, and
            # overwriting it silently would hide it.
            previous = match.quantities.get(doc)
            if previous is not None and previous != printed:
                match.status = "quantity_conflict"
                match.detail = (f"{doc} lists this item twice with different "
                                f"quantities ({previous} and {printed})")
            match.quantities[doc] = printed

    report.matches = list(ordered.values())
    checked = report.documents
    for match in report.matches:
        _classify(match, sets, checked)
    report.matches.sort(key=lambda m: (m.status != "quantity_conflict",
                                       m.status != "unit_differs", m.label))
    if report.skipped_reason:
        for match in report.matches:
            if match.status == "matched":
                match.status = "only_here"
                match.detail = report.skipped_reason
    return report


def _classify(match: ItemMatch, sets: dict[str, list[LineItem]], checked: list[str]) -> None:
    """`checked` is the documents that actually returned goods lines.

    A document that was never read for lines cannot be said to "list no such
    item" -- that is a claim about its contents, and nobody looked. It gets named
    separately, as not-read, which is the only honest thing to say about it.
    """
    if match.status == "quantity_conflict":
        return
    if len(match.documents) < 2:
        match.status = "only_here"
        others = [d for d in checked if d not in match.documents]
        unread = [d for d in sets if d not in checked]
        detail = f"stated on {match.documents[0]} only"
        if others:
            detail += f"; not printed on {', '.join(others)}"
        if unread:
            detail += (f" ({', '.join(unread)} returned no goods lines, so they take"
                       " no part in this comparison)")
        match.detail = detail
        return

    quantities = {}
    units = {}
    for doc, _printed in match.quantities.items():
        item = next((i for i in sets[doc] if i.key == match.key), None)
        if item is None:
            continue
        quantities[doc] = item.quantity
        units[doc] = item.unit

    known = {d: q for d, q in quantities.items() if q is not None}
    if len(set(known.values())) > 1:
        match.status = "quantity_conflict"
        spread = ", ".join(f"{d}={match.quantities[d]}" for d in match.documents)
        match.detail = f"the same line is counted differently: {spread}"
        return

    if len({u for u in units.values() if u}) > 1:
        match.status = "unit_differs"
        spread = ", ".join(f"{d}={units[d] or '?'}" for d in match.documents)
        match.detail = (f"same count on a different package basis ({spread}); the "
                        "totals may still agree while the lines do not")
        return

    if any(q is None for q in quantities.values()):
        missing = [d for d, q in quantities.items() if q is None]
        match.detail = ("quantity not readable on " + ", ".join(missing)
                        + "; compared on the lines that were")
