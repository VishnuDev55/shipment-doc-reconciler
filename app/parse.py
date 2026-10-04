"""Parse-and-repair ladder, rungs 1-3. All local, all free.

  1. json.loads() directly.
  2. Repair the text, no API call: strip markdown fences, take the first
     balanced {...} span, drop trailing commas, fix smart quotes.
  3. Coerce into the Pydantic model, no API call: fill missing field keys as
     present=false, stringify non-string values, clamp out-of-range confidence,
     map an unknown doc_type to "other".

The rule that shapes this whole module: one bad field must never fail an entire
document. A dropped field is a warning; a failed document is a 500 the user
sees. Returns `needs_retry` so the caller can spend an API call only when local
recovery genuinely cannot produce a usable object.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .fields import DOC_TYPES, FIELD_ORDER, FieldKey
from .models import (
    ExtractedField,
    ExtractionResponse,
    ExtractedLineItem,
    LineItemsResponse,
    MAX_LINE_ITEMS,
)

SMART_QUOTES = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "　": " ",
    "\u00a0": " ",
}

_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


@dataclass
class ParseResult:
    response: ExtractionResponse | None = None
    issues: list[str] = field(default_factory=list)
    dropped_fields: list[FieldKey] = field(default_factory=list)
    repaired: bool = False
    coerced: bool = False
    needs_retry: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.response is not None

    @property
    def partial(self) -> bool:
        return self.response is not None and bool(self.dropped_fields)


# ---------------------------------------------------------------------------
# Rung 2: text repair
# ---------------------------------------------------------------------------


def _fix_smart_quotes(text: str) -> tuple[str, bool]:
    changed = False
    out = []
    for ch in text:
        if ch in SMART_QUOTES:
            out.append(SMART_QUOTES[ch])
            changed = True
        else:
            out.append(ch)
    return "".join(out), changed


def strip_fences(text: str) -> str:
    return _FENCE.sub("", text.strip()).strip()


def first_balanced_object(text: str) -> str | None:
    """Return the first complete {...} span with balanced braces.

    Brace counting respects JSON string literals and escapes, so a "{" inside a
    quoted evidence line cannot trick the scanner into finding a false end.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def repair_json_text(raw: str) -> tuple[str | None, list[str], bool]:
    """Try progressively more aggressive repairs. Returns (text, notes, changed)."""
    notes: list[str] = []
    text = raw.strip()
    if not text:
        return None, ["empty response"], False

    unfenced = strip_fences(text)
    if unfenced != text:
        notes.append("stripped markdown fences")
        text = unfenced
        changed = True
    else:
        changed = False

    quoted, q_changed = _fix_smart_quotes(text)
    if q_changed:
        notes.append("normalised smart quotes")
        text = quoted
        changed = True

    balanced = first_balanced_object(text)
    if balanced is None:
        return None, notes + ["no balanced JSON object found"], changed
    if balanced != text:
        notes.append("extracted first balanced object")
        text = balanced
        changed = True

    try:
        json.loads(text)
        return text, notes, changed
    except json.JSONDecodeError as exc:
        first_error = exc

    decommas = _TRAILING_COMMA.sub(r"\1", text)
    if decommas != text:
        try:
            json.loads(decommas)
            notes.append("removed trailing commas")
            return decommas, notes, True
        except json.JSONDecodeError:
            pass

    # Last local attempt: single quotes for keys/values, common model slip.
    squoted = re.sub(r"(?<=[{,\[])\s*'", '"', decommas)
    squoted = re.sub(r"'\s*(?=[,:}\]])", '"', squoted)
    if squoted != decommas:
        try:
            json.loads(squoted)
            notes.append("converted single quotes")
            return squoted, notes, True
        except json.JSONDecodeError:
            pass

    return None, notes + [f"unrepairable JSON: {first_error}"], changed


# ---------------------------------------------------------------------------
# Rung 1+2
# ---------------------------------------------------------------------------


def load_json_object(raw: str) -> tuple[dict | None, list[str], bool, str | None]:
    """Rung 1, then rung 2. Returns (obj, notes, repaired, error)."""
    direct_error = ""
    try:
        obj = json.loads(raw.strip())
        if isinstance(obj, dict):
            return obj, [], False, None
        return None, [], False, f"top-level JSON is {type(obj).__name__}, not an object"
    except json.JSONDecodeError as exc:
        # Capture the message here: Python deletes the `exc` binding when the
        # except block exits, so referencing it on line 190 would NameError.
        direct_error = str(exc)

    text, notes, changed = repair_json_text(raw)
    if text is None:
        return None, notes, changed, "; ".join(n for n in notes if n) or direct_error
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, notes, changed, f"repaired text still invalid: {exc}"
    if not isinstance(obj, dict):
        return None, notes, changed, "repaired JSON is not an object"
    return obj, notes, changed, None


# ---------------------------------------------------------------------------
# Rung 3: coercion into the model
# ---------------------------------------------------------------------------


def _coerce_field(key: FieldKey, raw: Any, issues: list[str]) -> ExtractedField | None:
    """Return the field, or None to drop it. Dropping one field must not sink
    the document."""
    if raw is None:
        issues.append(f"{key}: missing, recorded as absent")
        return ExtractedField(present=False, value=None)

    if not isinstance(raw, dict):
        issues.append(f"{key}: expected an object, got {type(raw).__name__}")
        return None

    try:
        return ExtractedField.model_validate(raw)
    except ValidationError as exc:
        # Retry each scalar with a safe fallback before giving up on the field.
        present = bool(raw.get("present", False))
        value = raw.get("value")
        if value is not None and not isinstance(value, str):
            value = str(value)

        confidence = raw.get("confidence", 0.0)
        try:
            confidence = min(1.0, max(0.0, float(confidence)))
        except (TypeError, ValueError):
            issues.append(f"{key}: confidence {raw.get('confidence')!r} unreadable, set 0.0")
            confidence = 0.0

        page_index = raw.get("page_index", 1)
        try:
            page_index = max(1, int(page_index))
        except (TypeError, ValueError):
            page_index = 1

        evidence = raw.get("evidence_text", "")
        if not isinstance(evidence, str):
            evidence = str(evidence)

        if not present:
            value = None

        issues.append(f"{key}: repaired after validation error ({exc.errors()[0]['type']})")
        try:
            return ExtractedField(
                present=present,
                value=value,
                evidence_text=evidence,
                page_index=page_index,
                confidence=confidence,
            )
        except ValidationError:
            issues.append(f"{key}: dropped, unrecoverable")
            return None


def coerce_extraction(obj: dict) -> ParseResult:
    """Rung 3. Build a valid ExtractionResponse from a messy-but-parseable dict."""
    result = ParseResult()
    issues: list[str] = []

    raw_fields = obj.get("fields")
    if raw_fields is None:
        result.needs_retry = True
        result.error = "response has no 'fields' object"
        result.issues = issues
        return result
    if not isinstance(raw_fields, dict):
        result.needs_retry = True
        result.error = f"'fields' is {type(raw_fields).__name__}, expected an object"
        result.issues = issues
        return result

    unknown = set(raw_fields) - set(FIELD_ORDER)
    if unknown:
        issues.append(f"ignored unexpected field keys: {sorted(unknown)}")

    fields: dict[FieldKey, ExtractedField] = {}
    for key in FIELD_ORDER:
        coerced = _coerce_field(key, raw_fields.get(key), issues)
        if coerced is None:
            result.dropped_fields.append(key)
            # A dropped field still needs a slot, or the object is structurally
            # incomplete. Record it as absent rather than failing the document.
            fields[key] = ExtractedField(present=False, value=None)
        else:
            fields[key] = coerced

    doc_type = obj.get("doc_type", "other")
    if doc_type not in DOC_TYPES:
        issues.append(f"doc_type {doc_type!r} not recognised, set to 'other'")
        doc_type = "other"

    reason = obj.get("doc_type_reason", "")
    if not isinstance(reason, str):
        reason = str(reason)

    try:
        result.response = ExtractionResponse(
            doc_type=doc_type, doc_type_reason=reason, fields=fields
        )
    except ValidationError as exc:
        result.needs_retry = True
        result.error = f"could not assemble a valid object: {exc}"
        result.issues = issues
        return result

    result.issues = issues
    result.coerced = bool(issues)
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_extraction(raw: str) -> ParseResult:
    """Run rungs 1-3. Never raises; inspect .ok / .needs_retry / .error."""
    if raw is None or not str(raw).strip():
        return ParseResult(needs_retry=True, error="empty model response", issues=["empty"])

    obj, notes, repaired, error = load_json_object(str(raw))
    if obj is None:
        return ParseResult(
            needs_retry=True, repaired=repaired, error=error, issues=notes
        )

    result = coerce_extraction(obj)
    result.repaired = repaired
    if notes:
        result.issues = notes + result.issues
    return result


# ---------------------------------------------------------------------------
# Line items: the same rungs, a different shape
# ---------------------------------------------------------------------------


@dataclass
class LineItemsParseResult:
    response: LineItemsResponse | None = None
    issues: list[str] = field(default_factory=list)
    dropped_items: int = 0
    repaired: bool = False
    needs_retry: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.response is not None


def _coerce_line_item(raw: Any, index: int, issues: list[str]) -> ExtractedLineItem | None:
    if not isinstance(raw, dict):
        issues.append(f"item {index}: expected an object, got {type(raw).__name__}")
        return None
    # extra="ignore" means a stray page_index or unit key costs nothing; a row
    # whose printed text we can read is worth keeping.
    try:
        item = ExtractedLineItem.model_validate(raw)
    except ValidationError as exc:
        issues.append(f"item {index}: dropped ({exc.errors()[0]['type']})")
        return None
    if not (item.description.strip() or item.sku.strip() or item.quantity.strip()):
        issues.append(f"item {index}: dropped, every cell blank")
        return None
    return item


def parse_line_items(raw: str) -> LineItemsParseResult:
    """Parse the goods-lines response. Never raises.

    Same rule as the scalar contract: one unreadable row must not sink the
    document. Unlike the scalar contract there is no fixed key set to complete,
    so a dropped row is reported as a count rather than a list of names.
    """
    result = LineItemsParseResult()
    if raw is None or not str(raw).strip():
        return LineItemsParseResult(needs_retry=True, error="empty model response")

    obj, notes, repaired, error = load_json_object(str(raw))
    result.repaired = repaired
    if obj is None:
        return LineItemsParseResult(
            needs_retry=True, repaired=repaired, error=error, issues=notes
        )

    issues = list(notes)
    raw_items = obj.get("items")
    if raw_items is None:
        issues.append("no 'items' key, read as a document with no goods table")
        result.response = LineItemsResponse(items=[])
        result.issues = issues
        return result
    if not isinstance(raw_items, list):
        result.needs_retry = True
        result.error = f"'items' is {type(raw_items).__name__}, expected an array"
        result.issues = issues
        return result

    items: list[ExtractedLineItem] = []
    for i, entry in enumerate(raw_items):
        coerced = _coerce_line_item(entry, i + 1, issues)
        if coerced is None:
            result.dropped_items += 1
            continue
        items.append(coerced)

    if len(items) > MAX_LINE_ITEMS:
        issues.append(
            f"truncated to the first {MAX_LINE_ITEMS} of {len(items)} rows"
        )
        items = items[:MAX_LINE_ITEMS]

    result.response = LineItemsResponse(items=items)
    result.issues = issues
    return result
