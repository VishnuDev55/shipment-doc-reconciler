"""Regression tests built from REAL captured API responses.

fixtures/live_responses/*.json were captured by scripts/live_compare.py against
the live Token Factory endpoint on 2026-09-23. They are not hand-authored.

These exist because real model output differs from what I assumed while
building offline, in ways that matter:

  * the model sometimes invents a digit ("13,200.000 KGS" for a document that
    prints "13,200.00 KGS") and repeats the same invention in evidence_text;
  * it drops Incoterms prefixes it was told to copy verbatim;
  * it returns confidence 1.0 almost universally, so confidence is a weak
    signal and cannot be the only guard against a bad read.

The point of these tests is that none of the above changes a comparison
verdict, because normalization is deterministic and tolerant.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.fields import FIELD_ORDER
from app.models import ExtractionResponse
from app.normalize import normalize_field

LIVE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "live_responses"

pytestmark = pytest.mark.skipif(
    not LIVE_DIR.is_dir(), reason="run scripts/live_compare.py to capture live responses"
)


def live_files() -> list[Path]:
    return sorted(p for p in LIVE_DIR.glob("*.json"))


def load(name: str) -> dict:
    path = LIVE_DIR / f"{name}.json"
    if not path.is_file():
        pytest.skip(f"{path.name} not captured")
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Every captured live response must parse through the real model cleanly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", live_files(), ids=lambda p: p.stem)
def test_live_response_validates_without_repair(path: Path):
    payload = json.loads(path.read_text())
    resp = ExtractionResponse.model_validate(payload["response"])
    assert set(resp.fields) == set(FIELD_ORDER), "live output must be structurally complete"
    assert payload["_provenance"].startswith("LIVE API response")


@pytest.mark.parametrize("path", live_files(), ids=lambda p: p.stem)
def test_live_response_normalizes_without_crashing(path: Path):
    resp = ExtractionResponse.model_validate(json.loads(path.read_text())["response"])
    for key, field in resp.fields.items():
        norm = normalize_field(key, field)
        if field.present and field.value:
            # A present value that will not normalize must say why, not fail silently.
            if not norm.ok:
                assert norm.warnings, f"{path.stem}/{key}: unnormalizable with no warning"


# ---------------------------------------------------------------------------
# The specific real-world quirks observed, pinned as behaviour.
# ---------------------------------------------------------------------------


def test_weight_normalizes_whatever_the_model_returned():
    """The durable claim is that normalization absorbs transcription variance,
    not that one particular model made one particular mistake.

    Observed across captures: MiniCPM-V returned '13,200.000 KGS' (an invented
    digit, repeated in its own evidence quote); the NVIDIA omni model returned
    '13,200.00 KGS' correctly. Both must normalize to the same compare_key.
    """
    live = load("bol_conflict")
    field = ExtractionResponse.model_validate(live["response"]).fields["gross_weight"]
    norm = normalize_field("gross_weight", field)
    assert norm.ok
    assert norm.compare_key == "weight:13200.000"
    assert norm.canonical == "13200.000 kg"


def test_invented_digit_is_absorbed_by_the_normalizer():
    """Regression for the specific transcription error seen live, applied to a
    synthetic input so it does not depend on which model is captured."""
    from app.models import ExtractedField

    bad = ExtractedField(present=True, value="13,200.000 KGS",
                         evidence_text="GROSS WEIGHT 13,200.000 KGS",
                         page_index=1, confidence=1.0)
    good = ExtractedField(present=True, value="13,200.00 KGS",
                          evidence_text="GROSS WEIGHT 13,200.00 KGS",
                          page_index=1, confidence=1.0)
    assert normalize_field("gross_weight", bad).compare_key == \
        normalize_field("gross_weight", good).compare_key == "weight:13200.000"


def test_raw_value_is_preserved_alongside_the_normalized_one():
    """The report carries the model's verbatim string AND the canonical value,
    so a mis-transcription stays visible instead of being silently corrected.
    NormalizedField deliberately holds no raw text -- that separation is the
    point, and it is what stops the UI string-comparing display values."""
    live = load("bol_conflict")
    field = ExtractionResponse.model_validate(live["response"]).fields["gross_weight"]
    norm = normalize_field("gross_weight", field)

    assert field.value  # raw lives on the extracted field
    assert not hasattr(norm, "raw")  # normalized view never carries it
    assert norm.canonical and norm.compare_key.startswith("weight:")


def test_dropped_incoterm_still_yields_the_same_money_key():
    """Instructed to copy verbatim, a live model returned 'USD 8,420.00' for a
    line printing 'FOB NINGBO USD 8,420.00'. Both must normalize identically,
    so this variance cannot flip a comparison."""
    from app.normalize.money import normalize_money

    verbatim = normalize_money("FOB NINGBO USD 8,420.00")
    dropped = normalize_money("USD 8,420.00")
    assert verbatim.compare_key == dropped.compare_key == "money:USD:8420.00"


def test_confidence_is_recorded_but_never_decides_a_conflict():
    """Confidence is a self-report, so it is used only to escalate an otherwise
    *consistent* field to suspicious -- never to create or dissolve a conflict.

    The rationale is empirical: across captures, confidence ranged 0.91-1.00 and
    one model reported 1.00 on a value it had in fact mis-transcribed. The
    ISO 6346 checksum and the tolerance table are what catch errors.
    """
    from app.compare import LOW_CONFIDENCE, compare_field, FieldObservation
    from app.models import ExtractedField

    def obs(doc_id: str, value: str, conf: float) -> FieldObservation:
        f = ExtractedField(present=True, value=value, evidence_text="GROSS WEIGHT",
                           page_index=1, confidence=conf)
        return FieldObservation(
            document_id=doc_id, document_label=doc_id, doc_type="bill_of_lading",
            extracted=f, normalized=normalize_field("gross_weight", f),
        )

    # Agreeing values, one low-confidence -> escalated to suspicious.
    escalated = compare_field(
        "gross_weight",
        [obs("a", "12,450.00 KGS", LOW_CONFIDENCE - 0.2), obs("b", "12,450.00 KGS", 0.95)],
    )
    assert escalated.status == "suspicious"
    assert "confidence" in escalated.reason

    # Agreeing values, both confident -> consistent.
    calm = compare_field(
        "gross_weight",
        [obs("a", "12,450.00 KGS", 0.95), obs("b", "12,450.00 KGS", 0.99)],
    )
    assert calm.status == "consistent"

    # A real disagreement is still a conflict even when both sides are confident.
    conflict = compare_field(
        "gross_weight",
        [obs("a", "12,450.00 KGS", 1.0), obs("b", "13,200.00 KGS", 1.0)],
    )
    assert conflict.status == "conflict"


def test_cross_page_evidence_matches_the_real_document():
    """The messy packing list puts its TOTAL rows on page 2. The live model
    correctly reported page_index 2 there and page 1 for the container number,
    which is what made the fixture worth building."""
    live = load("packing_list_messy")
    resp = ExtractionResponse.model_validate(live["response"])
    assert resp.fields["gross_weight"].page_index == 2
    assert resp.fields["quantity"].page_index == 2
    assert resp.fields["container_number"].page_index == 1
    assert resp.fields["consignee_name"].page_index == 1


def test_live_extraction_reproduces_the_planted_conflict():
    """End-to-end sanity: the live values, once normalized, still produce the
    conflict the fixture was designed to demonstrate."""
    from app.compare import compare_field
    from app.compare import FieldObservation

    docs = []
    for name, dtype in (
        ("packing_list_conflict", "packing_list"),
        ("bol_conflict", "bill_of_lading"),
    ):
        resp = ExtractionResponse.model_validate(load(name)["response"])
        for key in ("gross_weight",):
            docs.append(
                FieldObservation(
                    document_id=name,
                    document_label=name,
                    doc_type=dtype,
                    extracted=resp.fields[key],
                    normalized=normalize_field(key, resp.fields[key]),
                )
            )
    result = compare_field("gross_weight", docs)
    assert result.status == "conflict"
    assert "%" in result.reason
