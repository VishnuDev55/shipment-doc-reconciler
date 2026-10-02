"""Pydantic v2 models. These are the source of truth for:

  * the JSON schema sent to the model (ExtractionResponse)
  * parsing/repairing whatever the model actually returned
  * normalized values, comparison results, and the persisted report
  * request validation on the upload route

Design constraints that must not drift:
  * `fields` is an OBJECT keyed by the six fixed names, so structural
    completeness does not depend on the model remembering to include them.
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


# ---------------------------------------------------------------------------
# Documents / report
# ---------------------------------------------------------------------------

DocStatus = Literal["ok", "partial", "failed", "pending", "running", "cached"]


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


# ---------------------------------------------------------------------------
# API request models
# ---------------------------------------------------------------------------


class RetryRequest(BaseModel):
    document_id: str


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
