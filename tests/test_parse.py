"""Retry ladder rungs 1-3 (local, free). Zero API calls."""

from __future__ import annotations

import json

import pytest

from app.fields import FIELD_ORDER
from app.models import DocumentReport
from app.normalize import normalize_field
from app.parse import first_balanced_object, parse_extraction


def good_payload(**overrides) -> dict:
    fields = {
        key: {
            "present": False,
            "value": None,
            "evidence_text": "",
            "page_index": 1,
            "confidence": 0.0,
        }
        for key in FIELD_ORDER
    }
    fields["gross_weight"] = {
        "present": True,
        "value": "12,450.00 KGS",
        "evidence_text": "TOTAL GROSS WT 12,450.00 KGS",
        "page_index": 1,
        "confidence": 0.96,
    }
    payload = {"doc_type": "packing_list", "doc_type_reason": "header", "fields": fields}
    payload.update(overrides)
    return payload


def complete(payload: dict) -> None:
    """Every rung-3 object must be structurally complete."""
    assert set(payload["fields"]) == set(FIELD_ORDER)


# ---------------------------------------------------------------------------
# Rung 1
# ---------------------------------------------------------------------------


def test_clean_json_passes_straight_through():
    res = parse_extraction(json.dumps(good_payload()))
    assert res.ok and not res.repaired and not res.coerced and not res.needs_retry
    assert res.response.fields["gross_weight"].value == "12,450.00 KGS"
    assert res.response.doc_type == "packing_list"


# ---------------------------------------------------------------------------
# Rung 2: repair without an API call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "messy",
    [
        "```json\n%s\n```" % json.dumps(good_payload()),
        "```\n%s```" % json.dumps(good_payload()),
        "Here is the extraction you asked for:\n%s\nHope that helps!" % json.dumps(good_payload()),
        json.dumps(good_payload()).replace('"confidence": 0.96', '"confidence": 0.96,'),
        json.dumps(good_payload()).replace('"packing_list"', "“packing_list”"),
    ],
    ids=["fenced-json", "bare-fence", "prose-wrapped", "trailing-comma", "smart-quotes"],
)
def test_repaired_without_retry(messy):
    res = parse_extraction(messy)
    assert res.ok, f"should have repaired: {res.error}"
    assert res.repaired
    assert not res.needs_retry
    complete(res.response.model_dump())


def test_balanced_object_ignores_brace_inside_string():
    payload = good_payload()
    payload["fields"]["gross_weight"]["evidence_text"] = 'weird { brace } here'
    text = "noise before " + json.dumps(payload) + " noise after"
    assert json.loads(first_balanced_object(text)) == payload


def test_escaped_quote_does_not_end_string_early():
    payload = good_payload()
    payload["fields"]["gross_weight"]["value"] = 'a "quoted" {value}'
    found = first_balanced_object(json.dumps(payload))
    assert json.loads(found) == payload


# ---------------------------------------------------------------------------
# Rung 3: coercion
# ---------------------------------------------------------------------------


def test_missing_field_key_becomes_absent_not_failed():
    payload = good_payload()
    del payload["fields"]["hs_code"]
    res = parse_extraction(json.dumps(payload))
    assert res.ok and not res.needs_retry
    assert res.response.fields["hs_code"].present is False
    assert res.response.fields["hs_code"].value is None
    complete(res.response.model_dump())


def test_numeric_value_is_stringified_not_rejected():
    payload = good_payload()
    payload["fields"]["quantity"] = {
        "present": True, "value": 1200, "evidence_text": "x", "page_index": 1, "confidence": 0.9
    }
    res = parse_extraction(json.dumps(payload))
    assert res.ok
    assert res.response.fields["quantity"].value == "1200"


def test_out_of_range_confidence_is_clamped():
    payload = good_payload()
    payload["fields"]["quantity"] = {
        "present": True, "value": "10", "evidence_text": "x", "page_index": 1, "confidence": 1.8
    }
    res = parse_extraction(json.dumps(payload))
    assert res.ok
    assert res.response.fields["quantity"].confidence == 1.0


def test_negative_confidence_clamped_to_zero():
    payload = good_payload()
    payload["fields"]["quantity"] = {
        "present": True, "value": "10", "evidence_text": "x", "page_index": 1, "confidence": -3
    }
    res = parse_extraction(json.dumps(payload))
    assert res.ok and res.response.fields["quantity"].confidence == 0.0


def test_unknown_doc_type_becomes_other():
    res = parse_extraction(json.dumps(good_payload(doc_type="bill_of_lading_v3")))
    assert res.ok and res.response.doc_type == "other"
    assert any("doc_type" in i for i in res.issues)


def test_unexpected_extra_field_key_is_ignored_not_fatal():
    """A key outside the contract is dropped and reported, not fatal.

    `vessel_name` is what a model really does invent when the document shows a
    vessel: it is not one of ours, and an earlier version of this test used
    net_weight as the stand-in extra key, which stopped being true the moment
    net_weight joined the field set.
    """
    payload = good_payload()
    payload["fields"]["vessel_name"] = {
        "present": True, "value": "MV HANWEI EXPRESS", "evidence_text": "x",
        "page_index": 1, "confidence": 0.9,
    }
    res = parse_extraction(json.dumps(payload))
    assert res.ok
    assert "vessel_name" not in res.response.fields
    assert any("unexpected" in i for i in res.issues)


def test_one_unrecoverable_field_drops_only_that_field():
    """The rule that shapes the ladder: a bad field must not sink the document."""
    payload = good_payload()
    payload["fields"]["hs_code"] = "i refuse to be an object"
    res = parse_extraction(json.dumps(payload))
    assert res.ok
    assert res.dropped_fields == ["hs_code"]
    assert res.partial
    assert res.response.fields["hs_code"].present is False
    # the good field survived
    assert res.response.fields["gross_weight"].value == "12,450.00 KGS"


def test_page_index_zero_is_repaired_to_one():
    payload = good_payload()
    payload["fields"]["quantity"] = {
        "present": True, "value": "10", "evidence_text": "x", "page_index": 0, "confidence": 0.5
    }
    res = parse_extraction(json.dumps(payload))
    assert res.ok and res.response.fields["quantity"].page_index == 1


def test_present_false_forces_value_null():
    payload = good_payload()
    payload["fields"]["quantity"] = {
        "present": False, "value": "123", "evidence_text": "x", "page_index": 1, "confidence": 0.5
    }
    res = parse_extraction(json.dumps(payload))
    assert res.ok and res.response.fields["quantity"].value is None


# ---------------------------------------------------------------------------
# When local recovery genuinely cannot work -> ask for a retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "the document was unclear, sorry",
        "{ this is not json at all",
        "[1, 2, 3]",
        json.dumps({"doc_type": "invoice"}),  # no fields object
    ],
)
def test_unrecoverable_input_requests_retry(bad):
    res = parse_extraction(bad)
    assert not res.ok
    assert res.needs_retry
    assert res.error


def test_parse_never_raises_on_binary_noise():
    for junk in ["\x00\x01\x02", "{" * 500, '"unterminated', "[[[", "null", "42"]:
        res = parse_extraction(junk)
        assert isinstance(res.needs_retry, bool)


# ---------------------------------------------------------------------------
# contract evolution: adding a field must not invalidate what was already captured
# ---------------------------------------------------------------------------


def _only(one_field: dict) -> str:
    return json.dumps({"doc_type": "commercial_invoice", "fields": one_field})


WEIGHT = {
    "gross_weight": {
        "present": True, "value": "12,450.00 KGS",
        "evidence_text": "TOTAL GROSS WT 12,450.00 KGS", "page_index": 1,
        "confidence": 0.95,
    }
}


def test_a_response_missing_newer_keys_still_parses():
    """A capture written before a field existed is legitimate history, not a
    malformed answer. Rejecting it would mean adding a field breaks every
    cached response and, with them, the keyless demo."""
    res = parse_extraction(_only(WEIGHT))
    assert res.ok, res.error
    fields = res.response.fields
    assert fields["gross_weight"].present is True
    for key in FIELD_ORDER:
        if key != "gross_weight":
            assert fields[key].present is False
            assert fields[key].value is None


def test_absent_keys_are_filled_as_not_stated_rather_than_guessed():
    """The filled-in fields must be indistinguishable from "the document did not
    print it" and never carry an invented value."""
    res = parse_extraction(_only(WEIGHT))
    filled = res.response.fields["container_number"]
    assert filled.present is False and filled.value is None
    assert filled.evidence_text == ""


def test_extra_keys_are_ignored_and_reported_not_silently_accepted():
    """A model inventing a field name is a contract slip worth surfacing.

    The repair layer drops it and says so, rather than passing it through to a
    schema that would reject the whole document over one stray key.
    """
    payload = _only({**WEIGHT, "not_a_field": {"present": True, "value": "x"}})
    res = parse_extraction(payload)
    assert res.ok
    assert "not_a_field" not in res.response.fields
    assert any("not_a_field" in issue for issue in res.issues), res.issues


def test_the_validator_still_refuses_a_directly_built_partial_response():
    """Parse repairs; the model itself does not. Anything constructing an
    ExtractionResponse directly must account for every key."""
    from pydantic import ValidationError

    from app.models import ExtractionResponse

    with pytest.raises(ValidationError, match="missing field keys"):
        ExtractionResponse(doc_type="commercial_invoice", fields={
            "gross_weight": {
                "present": True, "value": "1 kg", "evidence_text": "x",
                "page_index": 1, "confidence": 1.0,
            }
        })


def test_normalizers_run_over_the_filled_contract():
    res = parse_extraction(_only(WEIGHT))
    doc = DocumentReport(id="d1", label="Invoice", filename="inv.png")
    doc.extraction = res.response
    doc.normalized = {
        key: normalize_field(key, res.response.fields[key]) for key in FIELD_ORDER
    }
    assert doc.normalized["gross_weight"].compare_key == "weight:12450.000"
    assert doc.normalized["hs_code"].compare_key is None
