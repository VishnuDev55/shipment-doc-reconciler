"""Human corrections: overlay a value, re-compare instantly, keep the trail.

Two rules shape this module.

**Correcting must not cost an API call.** Everything downstream — normalization,
comparison, cross-field sanity, the risk score — is pure Python over the field
values, so a corrected value flows through them in milliseconds. Only a re-extract
from the images would cost anything, and nothing here does that.

**The model's answer is never overwritten.** `DocumentReport.extraction` keeps
what the reader returned, verbatim, forever. A correction lives in `overrides`
beside it, and reverting is just re-normalizing the original — which is why no
"before" snapshot has to be stored to make reversal safe. The trail in
`corrections` is append-only: reverted entries stay, stamped with when.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .fields import FIELD_ORDER, FieldKey
from .models import Correction, DocumentReport, ExtractedField, Report
from .normalize import normalize_field


class CorrectionError(ValueError):
    """Raised for a correction that cannot be applied. Never silent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _document(report: Report, document_id: str) -> DocumentReport:
    doc = next((d for d in report.documents if d.id == document_id), None)
    if doc is None:
        raise CorrectionError(f"no document {document_id!r} in this shipment")
    return doc


def _field(doc: DocumentReport, field: str) -> FieldKey:
    if field not in FIELD_ORDER:
        raise CorrectionError(
            f"{field!r} is not one of the reconciled fields: {', '.join(FIELD_ORDER)}"
        )
    return field  # type: ignore[return-value]


def _restore(doc: DocumentReport, field: FieldKey) -> None:
    """Drop the override and re-normalize whatever the model actually said."""
    doc.overrides.pop(field, None)
    original = doc.extraction.fields.get(field) if doc.extraction else None
    if original is None:
        doc.normalized.pop(field, None)
        return
    doc.normalized[field] = normalize_field(field, original)


def active_corrections(report: Report) -> list[Correction]:
    return [
        entry
        for doc in report.documents
        for entry in doc.corrections
        if entry.reverted_at is None
    ]


def correction_trail(report: Report) -> list[Correction]:
    """Every entry, reverted or not, oldest first: the audit trail."""
    entries = [entry for doc in report.documents for entry in doc.corrections]
    return sorted(entries, key=lambda e: (e.at, e.document_id, e.field))


def apply_correction(
    report: Report, document_id: str, field: str, value: str,
    *, actor: str = "reviewer", note: str = ""
) -> Correction:
    doc = _document(report, document_id)
    key = _field(doc, field)

    if doc.extraction is None:
        raise CorrectionError(
            f"{doc.label} has no extraction to correct — retry the document first"
        )
    cleaned = (value or "").strip()
    if not cleaned:
        raise CorrectionError(
            "an empty value is a deletion, not a correction; revert instead"
        )
    if len(cleaned) > 400:
        raise CorrectionError("correction is too long to be a field value")

    original = doc.extraction.fields.get(key)
    override = (
        original.model_copy(update={"value": cleaned, "present": True})
        if original is not None
        else ExtractedField(
            present=True, value=cleaned,
            page_index=doc.pages[0].index if doc.pages else 1,
            # Not the model's confidence: this is a human assertion, and claiming
            # a machine believed it at 0.9 would be a fabrication.
            confidence=1.0,
            evidence_text="entered by a reviewer",
        )
    )

    entry = Correction(
        field=key,
        document_id=doc.id,
        original_value=original.value if original is not None else None,
        original_present=original.present if original is not None else False,
        corrected_value=cleaned,
        actor=(actor or "reviewer").strip()[:60],
        at=_now(),
        note=note.strip()[:400],
    )

    doc.overrides[key] = override
    doc.normalized[key] = normalize_field(key, override)
    doc.corrections.append(entry)

    # Anything an LLM said about the pre-correction values is still true of those
    # values, so it is kept -- but it must stop reading like a comment on now.
    for comp in report.comparisons:
        if comp.field == key and comp.adjudication is not None:
            comp.adjudication.superseded_at = entry.at
    if report.narrative.status in ("ok", "cached") and report.narrative.text:
        report.narrative.superseded_at = entry.at
    return entry


def revert_corrections(
    report: Report, document_id: str, field: str, *, actor: str = "reviewer"
) -> list[Correction]:
    """Undo every active correction on one field, keeping the history."""
    doc = _document(report, document_id)
    key = _field(doc, field)
    touched = [e for e in doc.corrections if e.field == key and e.reverted_at is None]
    if not touched:
        raise CorrectionError(f"{doc.label} has no active correction for {key}")

    stamp = _now()
    for entry in touched:
        entry.reverted_at = stamp
        entry.note = (f"{entry.note} (reverted by {(actor or 'reviewer').strip()[:60]}"
                      f" {stamp})").strip()
    _restore(doc, key)
    return touched
