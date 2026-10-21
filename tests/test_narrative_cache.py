"""The executive-summary cache: verdict-keyed, disk-backed, tolerant of junk.

The narrative cache exists so DEMO_MODE can replay a genuinely model-written
summary without a key. What matters here is that the key is a function of the
verdicts (not the pages), that a hand-authored entry is flagged as authored,
and that a corrupt or blank entry reads as a miss rather than an error.
"""

from __future__ import annotations

import json

from app.config import Settings
from app.models import Comparison, Report
from app.narrative_cache import NarrativeCache, narrative_key, verdict_signature


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, cache_dir=tmp_path / "cache")


def _report(*comparisons: Comparison) -> Report:
    report = Report(shipment_id="s1", created_at="now", updated_at="now")
    report.comparisons = list(comparisons)
    return report


def _comp(field="gross_weight", status="conflict",
          reason="13,200.00 vs 12,450.00") -> Comparison:
    return Comparison(field=field, label="Gross weight", kind="weight",
                      status=status, reason=reason)


def test_verdict_signature_is_stable_and_sensitive_to_the_reason():
    assert verdict_signature(_report(_comp())) == verdict_signature(_report(_comp()))
    # The reason carries the numbers; a different discrepancy is a different
    # summary and must not reuse the same cached narrative.
    other = _report(_comp(reason="13,900.00 vs 12,450.00"))
    assert verdict_signature(_report(_comp())) != verdict_signature(other)


def test_narrative_key_binds_model_and_version():
    report = _report(_comp())
    assert narrative_key(report, "m1") == narrative_key(report, "m1")
    assert narrative_key(report, "m1") != narrative_key(report, "m2")


def test_get_returns_none_when_there_is_no_entry(tmp_path):
    cache = NarrativeCache(_settings(tmp_path))
    assert cache.get(_report(_comp()), "m1") is None


def test_put_then_get_round_trips(tmp_path):
    cache = NarrativeCache(_settings(tmp_path))
    report = _report(_comp())
    cache.put(report, model="m1", provider="nebius", text="A summary.")

    got = cache.get(report, "m1")
    assert got is not None
    assert got["text"] == "A summary."
    assert got["model"] == "m1"
    assert got["provider"] == "nebius"
    assert got["authored"] is False


def test_get_flags_a_hand_authored_entry(tmp_path):
    cache = NarrativeCache(_settings(tmp_path))
    report = _report(_comp())
    path = cache.path(narrative_key(report, "m1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"text": "x", "_provenance": "HAND-AUTHORED fixture"}))

    assert cache.get(report, "m1")["authored"] is True


def test_get_returns_none_on_corrupt_or_blank_entries(tmp_path):
    cache = NarrativeCache(_settings(tmp_path))
    report = _report(_comp())
    path = cache.path(narrative_key(report, "m1"))
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text("{not json", encoding="utf-8")
    assert cache.get(report, "m1") is None

    path.write_text(json.dumps({"text": "   "}), encoding="utf-8")
    assert cache.get(report, "m1") is None
