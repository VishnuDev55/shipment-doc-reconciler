"""Arithmetic inside a single document.

Cross-document comparison asks "do these papers agree". A shipment can pass that
and still be wrong: an invoice whose line amounts do not add up to its own total
is internally inconsistent no matter how faithfully the packing list repeats it,
and every other document agreeing with a wrong total only confirms that the same
mistake was copied.

So these checks look at one document at a time, using only what that document
printed:

  ``line_total_math``            quantity x unit price = the printed line amount
  ``line_total_sum``             the sum of the line amounts = the printed total
  ``row_weight_sum``             the sum of per-row weights = the printed total weight
  ``net_above_gross_in_document``  a document's own net weight does not exceed its gross

All four are pure functions over normalized values. The model is asked to copy
amounts verbatim and explicitly not to correct them (see
``app.prompt.line_items_user_text``): a model that quietly fixes a bad sum on the
way through destroys the only evidence that the document is wrong.

Tolerances come from config. Note that the money checks use an absolute tolerance,
not a relative one. A percentage of a large invoice is bigger than a rounding
error -- 0.5 % of 5,508 is 27 -- so a relative band would have passed the exact
defect this project found in its own fixture data, where a line printed 5,510.00
for a product of 5,508.00.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import Settings, get_settings
from .lineitems import LineItem
from .models import DocumentReport, FieldKey, InternalCheck, Report
from .normalize.money import normalize_money
from .normalize.weight import normalize_weight

# Rules that need goods rows. When none were read, each of these reports not_run
# rather than disappearing: an absent record reads as "checked and passed" to
# anything downstream that counts statuses, and "we never looked" is a different
# fact that has to stay visible.
LINE_DEPENDENT = ("line_total_math", "line_total_sum", "row_weight_sum")


def _effective_value(doc: DocumentReport, key: FieldKey) -> str | None:
    """The value this document states for a field, after any human correction."""
    field_obj = doc.overrides.get(key) or (
        doc.extraction.fields.get(key) if doc.extraction else None)
    if field_obj is None or not field_obj.present or field_obj.value is None:
        return None
    return str(field_obj.value)


def _money(doc: DocumentReport, key: FieldKey) -> tuple[float | None, str | None, str]:
    raw = _effective_value(doc, key)
    if raw is None:
        return None, None, ""
    result = normalize_money(raw)
    return result.amount, result.currency, raw


def _weight(doc: DocumentReport, key: FieldKey) -> tuple[float | None, str]:
    raw = _effective_value(doc, key)
    if raw is None:
        return None, ""
    return normalize_weight(raw).value_kg, raw


def _check(code: str, doc: DocumentReport, *, ok: bool, detail: str,
           fields: Sequence[FieldKey] = (), evidence: Sequence[str] = ()) -> InternalCheck:
    return InternalCheck(
        code=code, document_id=doc.id, document_label=doc.label,
        status="pass" if ok else "fail", detail=detail,
        fields=list(fields), evidence=list(evidence)[:6],
    )


AMBIGUOUS_RULE = "assume_thousands_grouping"


def _read_money(raw: str) -> tuple[float | None, str | None, bool]:
    """(amount, currency, ambiguous).

    The third flag matters more than it looks. `normalize_money` resolves an
    ambiguous separator to the thousands convention and warns -- so a unit price
    printed as "2.675" is read as 2675. Multiplying that out and calling the
    difference an arithmetic error would be reporting a bug in the document that
    is really a limit of reading it, so ambiguous rows are set aside and named
    rather than judged.
    """
    if not (raw or "").strip():
        return None, None, False
    result = normalize_money(raw)
    return result.amount, result.currency, AMBIGUOUS_RULE in result.rules_applied


def _line_math(doc: DocumentReport, items: list[LineItem], *, money_tol: float
               ) -> InternalCheck | None:
    priced, ambiguous = [], 0
    for item in items:
        price, _pc, price_amb = _read_money(item.raw_unit_price)
        amount, _ac, amount_amb = _read_money(item.raw_line_total)
        if item.quantity is None or price is None or amount is None:
            continue
        if price_amb or amount_amb:
            ambiguous += 1
            continue
        priced.append((item, price, amount))
    if not priced and not ambiguous:
        return None

    bad: list[str] = []
    bad_rows: list[LineItem] = []
    for item, price, amount in priced:
        expected = round(item.quantity * price, 2)                  # type: ignore[operator]
        if abs(expected - amount) > money_tol:
            bad.append(f"{item.label!r}: {item.raw_quantity} x {item.raw_unit_price} "
                       f"= {expected:,.2f}, the row prints {item.raw_line_total}")
            bad_rows.append(item)

    caveat = (f" ({ambiguous} row(s) set aside: a price printed with an ambiguous "
              f"separator, e.g. '2.675', cannot be read as a decimal with "
              f"certainty)") if ambiguous else ""
    if not priced:
        return InternalCheck(
            code="line_total_math", document_id=doc.id, document_label=doc.label,
            status="not_run",
            detail=f"every priced row had an ambiguous number separator{caveat}",
        )
    return InternalCheck(
        code="line_total_math", document_id=doc.id, document_label=doc.label,
        status="pass" if not bad else "fail",
        detail=("; ".join(bad) if bad
                else f"{len(priced)} priced row(s) multiply out") + caveat,
        fields=["quantity", "invoice_value"],
        evidence=[i.evidence for i in (bad_rows or [p[0] for p in priced])][:6],
    )


def _line_sum(doc: DocumentReport, items: list[LineItem], *, money_tol: float
              ) -> InternalCheck | None:
    amounts: list[tuple[LineItem, float, str | None]] = []
    for item in items:
        amount, currency, ambiguous = _read_money(item.raw_line_total)
        if amount is not None and not ambiguous:
            amounts.append((item, amount, currency))
    stated, stated_currency, stated_raw = _money(doc, "invoice_value")
    if not amounts or stated is None:
        return None

    evidence = [i.evidence for i, _a, _c in amounts]

    # Adding across currencies is not a sum, it is a coincidence. The rule that
    # forbids converting also forbids pretending the columns line up.
    present = {c for _i, _a, c in amounts if c}
    if stated_currency:
        present.add(stated_currency)
    if len(present) > 1:
        return InternalCheck(
            code="line_total_sum", document_id=doc.id, document_label=doc.label,
            status="fail",
            detail=("the goods lines are printed in more than one currency ("
                    + ", ".join(sorted(present)) + "), so they cannot be added to "
                    "check the total -- this app never converts between currencies"),
            fields=["invoice_value"], evidence=evidence[:6],
        )

    total = round(sum(a for _i, a, _c in amounts), 2)
    ok = abs(total - stated) <= money_tol
    detail = (f"the goods lines sum to {total:,.2f} but the document states {stated_raw}"
              if not ok else
              f"{len(amounts)} line(s) sum to the stated {stated_raw}")
    return _check("line_total_sum", doc, ok=ok, detail=detail,
                  fields=("invoice_value",), evidence=evidence)


def _weight_sum(doc: DocumentReport, items: list[LineItem], *, rel: float,
                abs_kg: float) -> list[InternalCheck]:
    """Per-row gross and net against the document's own stated totals."""
    out: list[InternalCheck] = []
    for key, getter, label in (
        ("gross_weight", (lambda i: i.gross_kg), "gross"),
        ("net_weight", (lambda i: i.net_kg), "net"),
    ):
        rows = [v for v in (getter(i) for i in items) if v is not None]
        stated, raw = _weight(doc, key)          # type: ignore[arg-type]
        if not rows or stated is None:
            continue
        total = round(sum(rows), 3)
        tolerance = max(abs_kg, rel * abs(stated))
        ok = abs(total - stated) <= tolerance
        out.append(_check(
            "row_weight_sum", doc, ok=ok,
            detail=(f"the {label} weights on {len(rows)} row(s) add to {total:,.2f} kg "
                    f"but the document states {raw} "
                    f"(tolerance ±{tolerance:,.2f} kg)" if not ok else
                    f"{label} rows add to {total:,.2f} kg against the stated {raw}"),
            fields=(key,),                       # type: ignore[arg-type]
            evidence=[i.evidence for i in items if getter(i) is not None],
        ))
    return out


def _net_vs_gross(doc: DocumentReport, *, margin_kg: float) -> InternalCheck | None:
    """One document's own net against its own gross.

    Distinct from ``rules.net_above_gross``, which compares the majority value
    across documents. A shipment where every paper repeats the same impossible
    pair passes that check and fails this one, which is the point of having both.
    """
    gross, gross_raw = _weight(doc, "gross_weight")
    net, net_raw = _weight(doc, "net_weight")
    if gross is None or net is None:
        return None
    ok = net <= gross + margin_kg
    return _check(
        "net_above_gross_in_document", doc, ok=ok,
        detail=(f"{doc.label} states net {net_raw} against gross {gross_raw}; "
                f"packaging has to weigh something" if not ok else
                f"net {net_raw} is within gross {gross_raw}"),
        fields=("net_weight", "gross_weight"),
        evidence=[gross_raw, net_raw],
    )


def internal_checks(report: Report, lines: dict[str, list[LineItem]], *,
                    settings: Settings | None = None) -> list[InternalCheck]:
    """Every internal arithmetic check for one shipment, in document order.

    `lines` maps a document id to the rows read from it. A document absent from
    the mapping, or mapped to an empty list, produces `not_run` records rather
    than silence.
    """
    settings = settings or get_settings()
    money_tol = settings.math_tolerance_money
    rel = settings.math_tolerance_weight_rel
    abs_kg = settings.math_tolerance_weight_kg

    out: list[InternalCheck] = []
    for doc in report.documents:
        items = lines.get(doc.id) or []

        gross_net = _net_vs_gross(doc, margin_kg=settings.math_min_net_margin_kg)
        if gross_net is not None:
            out.append(gross_net)

        if not items:
            out.extend(InternalCheck(
                code=code, document_id=doc.id, document_label=doc.label,
                status="not_run",
                detail="no goods rows were read from this document, so this was "
                       "not checked -- which is not the same as passing",
            ) for code in LINE_DEPENDENT)
            continue

        produced = {
            "line_total_math": _line_math(doc, items, money_tol=money_tol),
            "line_total_sum": _line_sum(doc, items, money_tol=money_tol),
            "row_weight_sum": None,
        }
        for check in (produced["line_total_math"], produced["line_total_sum"]):
            if check is not None:
                out.append(check)

        weights = _weight_sum(doc, items, rel=rel, abs_kg=abs_kg)
        if weights:
            out.extend(weights)
        else:
            out.append(InternalCheck(
                code="row_weight_sum", document_id=doc.id, document_label=doc.label,
                status="not_run",
                detail="the document prints no per-row weights to add up",
            ))

    out.sort(key=lambda c: (c.status != "fail", c.status != "not_run",
                            c.document_label, c.code))
    return out
