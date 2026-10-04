"""Pydantic v2 models. These are the source of truth for:

  * the JSON schema sent to the model (ExtractionResponse)
  * parsing/repairing whatever the model actually returned
  * normalized values, comparison results, and the persisted report
  * request validation on the upload route

Design constraints that must not drift:
  * `fields` is an OBJECT keyed by the fixed names in app/fields.py, so
    structural completeness does not depend on the model remembering to include
    them.
  * the model's `value` is always a string (or null) — never a typed number.
    All parsing and arithmetic happens in normalizers, on our side.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import COMPARATOR_VERSION, NORMALIZER_VERSION, PROMPT_VERSION
from .fields import DOC_TYPES, FIELD_ORDER, DocType, FieldKey

# ---------------------------------------------------------------------------
# Extraction contract
# ---------------------------------------------------------------------------

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class ExtractedField(BaseModel):
    """One field as reported by the model. Verbatim, untyped, never inferred."""

    model_config = ConfigDict(extra="forbid")

    present: bool = False
    value: str | None = None
    evidence_text: str = ""
    page_index: int = Field(default=1, ge=1)
    confidence: Confidence = 0.0

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value_to_string(cls, v: object) -> object:
        """The model will sometimes return 12450 instead of "12450".

        Coercing here keeps the "value is always a string" invariant true
        without failing the whole document over a JSON type slip.
        """
        if v is None or isinstance(v, str):
            return v
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, (list, tuple)):
            return " ".join(str(x) for x in v)
        return str(v)

    @field_validator("evidence_text", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> str:
        return "" if v is None else str(v)

    @model_validator(mode="after")
    def _present_and_value_agree(self) -> "ExtractedField":
        """Enforce the contract regardless of which path built this object.

        present=false with a value is a contradiction that would otherwise
        reach the UI as "absent, but here's a number". present=true with no
        value is the same problem inverted. Both are resolved toward "absent",
        because that direction never invents data.
        """
        if not self.present and self.value is not None:
            self.value = None
        elif self.present and self.value is None:
            self.present = False
        return self


class ExtractionResponse(BaseModel):
    """What the model must return for one document (all its pages, one call)."""

    model_config = ConfigDict(extra="forbid")

    doc_type: DocType = "other"
    doc_type_reason: str = ""
    fields: dict[FieldKey, ExtractedField] = Field(
        default_factory=dict,
        json_schema_extra={
            "propertyNames": {"enum": list(FIELD_ORDER)},
        },
    )

    @field_validator("fields")
    @classmethod
    def _all_keys_present(cls, v: dict[FieldKey, ExtractedField]) -> dict:
        """Every key must be on the object that reaches this point.

        Not a guard against schema growth: app/parse.py fills absent keys as
        `present: false` and reports unknown ones before validation runs, which
        is why cached responses written under an earlier contract still parse
        (pinned in tests/test_parse.py). This exists so that any path building an
        ExtractionResponse directly cannot silently omit a field.
        """
        missing = set(FIELD_ORDER) - set(v)
        if missing:
            raise ValueError(f"missing field keys: {sorted(missing)}")
        return v


def extraction_json_schema() -> dict:
    """The JSON Schema sent to the endpoint.

    Written by hand rather than emitted from model_json_schema() because the
    generated version carries Pydantic metadata ($defs, title noise) and, more
    importantly, we need every key required and additionalProperties false for
    strict mode to be honoured.
    """
    field_obj = {
        "type": "object",
        "additionalProperties": False,
        "description": (
            "Verbatim extraction. Copy text exactly as printed: do not convert "
            "units, reformat numbers, or clean spacing."
        ),
        "properties": {
            "present": {
                "type": "boolean",
                "description": "true only if the value is visibly printed on the document.",
            },
            "value": {
                "type": ["string", "null"],
                "description": (
                    "The verbatim printed string, including units and punctuation as "
                    "printed. null when present=false."
                ),
            },
            "evidence_text": {
                "type": "string",
                "description": "The full printed line the value came from, copied verbatim.",
            },
            "page_index": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based page number where the value was found.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "0-1. Below 0.5 if text is present but hard to read.",
            },
        },
        "required": ["present", "value", "evidence_text", "page_index", "confidence"],
    }

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
            "doc_type_reason": {
                "type": "string",
                "description": "Short reason for the document type chosen.",
            },
            "fields": {
                "type": "object",
                "additionalProperties": False,
                "properties": {key: field_obj for key in FIELD_ORDER},
                "required": list(FIELD_ORDER),
            },
        },
        "required": ["doc_type", "doc_type_reason", "fields"],
    }


# ---------------------------------------------------------------------------
# Line items (opt-in second read)
# ---------------------------------------------------------------------------

# A real trade document can run to dozens of goods lines; the cap exists so a
# model that starts transcribing the terms-and-conditions paragraph cannot make
# the response unbounded.
MAX_LINE_ITEMS = 60


class ExtractedLineItem(BaseModel):
    """One goods line as printed. Everything is a verbatim string.

    `quantity` keeps the unit the document printed ("60 CTNS") because the unit
    is part of what gets compared: 60 pieces and 6 cartons are the same goods on
    different bases, and stripping the unit would hide that.
    """

    model_config = ConfigDict(extra="ignore")

    description: str = ""
    sku: str = ""
    quantity: str = ""

    @field_validator("description", "sku", "quantity", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> str:
        return "" if v is None else str(v)


class LineItemsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[ExtractedLineItem] = Field(default_factory=list)


def line_items_json_schema() -> dict:
    """Strict-mode schema for the line-item read. See extraction_json_schema()."""
    item = {
        "type": "object",
        "additionalProperties": False,
        "description": (
            "One row of the goods table, copied exactly as printed. Do not merge "
            "two rows, do not split a wrapped row, do not restate the total."
        ),
        "properties": {
            "description": {
                "type": "string",
                "description": "The printed goods description for this row, verbatim. '' if blank.",
            },
            "sku": {
                "type": "string",
                "description": (
                    "The printed item number / SKU / HS-code-per-row for this row, verbatim. "
                    "'' when the row has none — never invent one from the item sequence."
                ),
            },
            "quantity": {
                "type": "string",
                "description": (
                    "The printed quantity for this row, verbatim, including the unit as "
                    "printed (e.g. '60 CTNS', '1,200 PCS'). '' when the row has none."
                ),
            },
        },
        "required": ["description", "sku", "quantity"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "maxItems": MAX_LINE_ITEMS,
                "description": "Every goods row, in printed order. [] when the document lists none.",
                "items": item,
            }
        },
        "required": ["items"],
    }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class NormalizedField(BaseModel):
    canonical: str | None = None
    compare_key: str | None = None
    rules_applied: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    ok: bool = True


class NormalizedDocument(BaseModel):
    """Normalized view of one document's fields, plus the raw extraction."""

    raw: ExtractionResponse
    normalized: dict[FieldKey, NormalizedField] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

Status = Literal["consistent", "suspicious", "conflict", "single_source", "absent"]

STATUS_ORDER = {"consistent": 0, "single_source": 1, "suspicious": 2, "conflict": 3, "absent": 0}


class ComparisonValue(BaseModel):
    document_id: str
    document_label: str
    doc_type: DocType
    page_index: int
    raw_value: str | None
    canonical: str | None
    compare_key: str | None
    confidence: Confidence
    is_outlier: bool = False
    warnings: list[str] = Field(default_factory=list)
    expected_here: bool = True


class Comparison(BaseModel):
    field: FieldKey
    label: str
    kind: str
    status: Status
    reason: str
    values: list[ComparisonValue] = Field(default_factory=list)
    majority_compare_key: str | None = None
    # Advisory only. `status` and `reason` come out of deterministic code and
    # this field can never rewrite them; it records what a model would have
    # said, for the human who has to decide.
    adjudication: Adjudication | None = None


# ---------------------------------------------------------------------------
# Documents / report
# ---------------------------------------------------------------------------

DocStatus = Literal["ok", "partial", "failed", "pending", "running", "cached"]


class TokenUsage(BaseModel):
    """What the provider reported for the calls that actually happened.

    Zeroes are meaningful, not missing: a document served from the cache made no
    call on this run, so its usage is zero even though the cached values were
    once produced by a model. The panel must show `source` alongside this, never
    this alone.
    """

    prompt: int = 0
    completion: int = 0
    total: int = 0


class Adjudication(BaseModel):
    """A model's opinion on a comparison the engine called ambiguous.

    Additive and advisory by construction: it sits beside `Comparison.status`
    and has no path to changing it. Skipping or failing is a normal outcome and
    must never alter the shipment's state or verdict — the same rule the
    narrative follows, for the same reason.
    """

    status: Literal["pending", "ok", "failed", "skipped", "cached"] = "pending"
    # true / false / null-as-abstention. Kept tri-state because a model that
    # declines to answer is informative; one that guessed is not.
    same: bool | None = None
    confidence: float = 0.0
    rationale: str = ""
    model: str = ""
    provider: str = ""
    # 'generated' | 'cached' | 'none'
    source: str = "none"
    error: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0
    # The opinion was formed on values a human has since corrected: kept on file,
    # but no longer a comment on the current verdict.
    superseded_at: str | None = None


class SanityFinding(BaseModel):
    """One cross-field observation the per-field comparisons cannot see.

    Deterministic, and `explanation` quotes both the computed number and the
    threshold it broke so the finding can be disputed rather than trusted.
    """

    code: str
    level: Literal["info", "warn", "risk"] = "warn"
    fields: list[FieldKey] = Field(default_factory=list)
    explanation: str = ""


class RiskContribution(BaseModel):
    """The points one rule added, and why.

    A score nobody can decompose is a vibe. Every point in `RiskNote.score`
    traces to one of these.
    """

    code: str
    points: int
    detail: str
    fields: list[FieldKey] = Field(default_factory=list)


class RiskNote(BaseModel):
    """Customs exposure, computed from the engine's own verdicts.

    `source` is pinned to "deterministic" because a customs-risk statement an
    auditor cannot trace to a rule is worth less than none, and every input here
    is already a deterministic comparison result. No arithmetic, no currency
    conversion, no invented severity.

    `level` is derived from `score` rather than decided separately, so the strip
    and the number can never disagree about the same shipment.
    """

    level: Literal["low", "medium", "high"] = "low"
    reasons: list[str] = Field(default_factory=list)
    source: str = "deterministic"
    score: int = 0
    contributions: list[RiskContribution] = Field(default_factory=list)
    high_at: int = 55
    medium_at: int = 25
    method: str = "deterministic-v1"


class Briefing(BaseModel):
    """Draft discrepancy email for the supplier, written from verdicts only.

    A restatement of decided facts, so it can be omitted entirely without
    weakening the report. Like the narrative it can never change a state.
    """

    status: Literal["pending", "ok", "failed", "skipped", "cached"] = "pending"
    text: str | None = None
    model: str = ""
    provider: str = ""
    source: str = "none"
    error: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0


class FieldDivergence(BaseModel):
    """One field the two readers did not agree on.

    Presence is tracked separately from value: a field one model left blank and
    the other filled in is a coverage disagreement, which is a different problem
    from two transcriptions of the same line.
    """

    field: FieldKey
    primary_present: bool = False
    secondary_present: bool = False
    primary_canonical: str | None = None
    secondary_canonical: str | None = None
    primary_compare_key: str | None = None
    secondary_compare_key: str | None = None


class ConsensusResult(BaseModel):
    """The second reader's report on the same pages.

    Advisory. It marks fields for human review and has no path to changing a
    comparison status — two models agreeing is not proof, and two disagreeing is
    not a verdict either.
    """

    status: Literal["pending", "ok", "skipped", "failed", "cached"] = "pending"
    provider: str = ""
    model: str = ""
    # 'generated' | 'cached_live' | 'cached_authored' | 'none'
    source: str = "none"
    divergences: list[FieldDivergence] = Field(default_factory=list)
    summary: str = ""
    error: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0


class Correction(BaseModel):
    """A human replacing a transcribed value.

    The model's own answer is never overwritten: `original_value` keeps it
    verbatim, so a correction is reversible and the report still shows what the
    reader said. Only the derived comparison changes, immediately and
    deterministically — correcting a value must not cost another API call.
    """

    field: FieldKey
    document_id: str
    original_value: str | None = None
    original_present: bool = False
    corrected_value: str
    actor: str = "reviewer"
    at: str = ""
    reverted_at: str | None = None
    note: str = ""


class PageRef(BaseModel):
    index: int
    image_path: str
    sha256: str
    width: int
    height: int


class DocumentReport(BaseModel):
    id: str
    label: str
    filename: str
    declared_doc_type: DocType | None = None
    detected_doc_type: DocType | None = None
    status: DocStatus = "pending"
    attempts: int = 0
    error: str | None = None
    pages: list[PageRef] = Field(default_factory=list)
    extraction: ExtractionResponse | None = None
    normalized: dict[FieldKey, NormalizedField] = Field(default_factory=dict)
    timings_ms: int = 0
    used_cache: bool = False
    used_fallback_schema: bool = False
    # The model's own doc_type differed from what the user declared. The
    # declaration wins for comparison; this flag only surfaces the disagreement.
    doc_type_disagrees: bool = False
    # 'live_api' | 'cached_live' | 'cached_authored' | 'none'
    source: str = "none"
    # Which provider and model answered this document. RunInfo stamps the
    # configured defaults; these record what actually served each document, so
    # a per-document answer cannot be credited to the wrong host.
    provider: str = ""
    model: str = ""
    usage: TokenUsage = Field(default_factory=TokenUsage)
    # Basenames under runs/{id}/raw/, in call order. Names only, never content:
    # a raw body is untrusted document-derived text.
    raw_files: list[str] = Field(default_factory=list)
    # Set only when a second provider was asked to read the same pages.
    consensus: ConsensusResult | None = None
    # Human-entered values, overlaying the model's. `extraction` is left exactly
    # as the model answered; these take precedence when a field is read for
    # comparison, so the correction is visible as a correction.
    overrides: dict[FieldKey, ExtractedField] = Field(default_factory=dict)
    corrections: list[Correction] = Field(default_factory=list)


class RunInfo(BaseModel):
    """Version tags. A cached response must never be silently reconciled
    against a newer, incompatible ruleset."""

    prompt_version: str = PROMPT_VERSION
    normalizer_version: str = NORMALIZER_VERSION
    comparator_version: str = COMPARATOR_VERSION
    # `model` is the vision model that read the documents; the summary model is
    # recorded separately because two providers are in play.
    model: str = ""
    vision_provider: str = ""
    summary_model: str = ""
    summary_provider: str = ""
    demo_mode: bool = False
    json_schema_mode: str = "unknown"
    # Where the extractions in this report actually came from. Derived from the
    # documents, not from the DEMO_MODE setting: a live run that hits the cache
    # is still cache-sourced, and a DEMO_MODE run is not "live-verified".
    # Must stay in sync with the values app.orchestrator.result_source returns.
    result_source: Literal["authored", "live", "live+authored", "none"] = "none"
    # How many documents were read by a real API call *during this run*.
    # Zero means the whole report is a replay from cache, even when the cached
    # values were themselves produced by a model earlier. Conflating the two
    # would let a no-key demo claim "live result" to a judge.
    live_calls: int = 0
    # Blended price per million tokens, echoed so a report can price itself
    # without the reader having to trust the deployment's environment. Zero
    # means not configured, and the panel then shows no cost rather than a guess.
    usd_per_mtok: float = 0.0
    # Whether the vision model was confirmed against the host's own /models
    # list: 'configured' (confirmed present), 'discovered' (the configured name
    # is not served, so a listed model was adopted), 'unverified' (no key, or
    # the catalog could not be read). A report should never imply a model was
    # reachable when nothing checked.
    model_selection: str = "unverified"
    model_note: str = ""


class Summary(BaseModel):
    total_fields: int = 0
    consistent: int = 0
    suspicious: int = 0
    conflict: int = 0
    single_source: int = 0
    absent: int = 0
    documents_ok: int = 0
    documents_failed: int = 0
    verdict: Literal["clean", "review", "conflicts", "running", "empty"] = "empty"


class Narrative(BaseModel):
    """Optional AI-written executive summary, generated once per shipment.

    Additive only. It restates verdicts produced by deterministic code and can
    never change one. Absent (status="skipped"/"failed") is a normal outcome
    and must not affect the shipment's state or verdict.
    """

    status: Literal["pending", "ok", "failed", "skipped", "cached"] = "pending"
    text: str | None = None
    model: str = ""
    provider: str = ""
    error: str | None = None
    generated_at: str | None = None
    # 'generated' = written by a model on this run; 'cached' = reused from a
    # previous run's stored narrative.
    source: str = "none"
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0
    # Set when the text came from a call made on this run, and cleared when it
    # did not: a cached narrative has no fresh cost, and must not appear to.
    # Basename under runs/{id}/raw/.
    raw_file: str | None = None
    # Set when a human corrected a value after this was written. The paragraph
    # stays on screen -- it is still what a model produced -- but it must not read
    # as a description of the current verdicts.
    superseded_at: str | None = None


class LineItemMatch(BaseModel):
    """One goods line, as seen by every document that stated it.

    `quantities` maps a document label to the string that document printed, so a
    disagreement is shown as two quotes rather than as a computed delta — the
    reader has to be able to see what each paper actually said.
    """

    key: str
    label: str
    documents: list[str] = Field(default_factory=list)
    quantities: dict[str, str] = Field(default_factory=dict)
    # 'matched' | 'quantity_conflict' | 'unit_differs' | 'only_here'
    status: str = "matched"
    detail: str = ""


class LineItemsSection(BaseModel):
    """The goods-line picture. Additive, and off unless asked for.

    `enabled` is kept separately from `status` because "nobody asked" and "asked,
    found nothing" are different things to show a reviewer; collapsing them would
    read as a failure on every run that left the flag off.
    """

    enabled: bool = False
    status: Literal["disabled", "pending", "ok", "partial", "skipped", "failed"] = "disabled"
    # 'live_api' | 'cached_live' | 'cached_authored' | 'none', same meaning as on
    # DocumentReport.source. Never derived from the DEMO_MODE flag.
    source: str = "none"
    model: str = ""
    provider: str = ""
    documents: list[str] = Field(default_factory=list)
    item_counts: dict[str, int] = Field(default_factory=dict)
    matches: list[LineItemMatch] = Field(default_factory=list)
    matched: int = 0
    conflicts: int = 0
    uncheckable: int = 0
    note: str = ""
    error: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0
    # Basenames under runs/{id}/raw/ for the line-item calls only.
    raw_files: list[str] = Field(default_factory=list)


class Report(BaseModel):
    shipment_id: str
    created_at: str
    updated_at: str
    state: Literal["pending", "running", "done", "partial", "failed"] = "pending"
    run: RunInfo = Field(default_factory=RunInfo)
    documents: list[DocumentReport] = Field(default_factory=list)
    comparisons: list[Comparison] = Field(default_factory=list)
    summary: Summary = Field(default_factory=Summary)
    narrative: Narrative = Field(default_factory=Narrative)
    risk: RiskNote = Field(default_factory=RiskNote)
    # Cross-field sanity, computed after the comparisons and before the risk
    # strip, so a risk level that ignores them would be understating.
    sanity: list[SanityFinding] = Field(default_factory=list)
    briefing: Briefing = Field(default_factory=Briefing)
    # Written after the comparisons are settled, and never read by the engine.
    line_items: LineItemsSection = Field(default_factory=LineItemsSection)


# ---------------------------------------------------------------------------
# API request models
# ---------------------------------------------------------------------------


class SessionRequest(BaseModel):
    """An API key offered to /api/session in exchange for a browser cookie.

    Only ever compared and forgotten -- the key is not stored, echoed, or
    reflected in any response body.
    """

    key: str = ""


class RetryRequest(BaseModel):
    document_id: str


class CorrectionRequest(BaseModel):
    """A reviewer replacing one field's transcribed value.

    `actor` is free text, not an identity: this app has no accounts, so the trail
    records who they said they were. That is still worth keeping — an unattributed
    change is harder to defend to an auditor than a claimed one.
    """

    value: str = Field(min_length=1, max_length=400)
    actor: str = Field(default="reviewer", max_length=60)
    note: str = Field(default="", max_length=400)


class ConfigUpdate(BaseModel):
    """What the GUI may change on a running process. A null field means leave it.

    Sending a key as an empty string clears it. The frontend echoes back what
    GET returned, and GET only ever yields a mask, so a value still holding the
    mask marker is dropped rather than installed as a bogus key.
    """

    nvidia_api_key: str | None = None
    nebius_api_key: str | None = None
    vision_provider: Literal["nvidia", "nebius"] | None = None
    nvidia_vision_model: str | None = None
    nebius_vision_model: str | None = None
    nebius_summary_model: str | None = None
    demo_mode: bool | None = None
    enable_summary: bool | None = None
