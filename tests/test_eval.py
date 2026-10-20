"""The evaluation harness, tested.

Two distinct worries. One: the numbers must be reproducible offline from the
captures in the repo, so a reviewer can re-run them without a key. Two: the
document that quotes them must not drift away from the data -- a docs file that
reports last week's accuracy is worse than no file, because it looks checked.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("evaluate", REPO / "scripts" / "evaluate.py")
assert _spec and _spec.loader
evaluate = importlib.util.module_from_spec(_spec)
# Registered before execution: @dataclass resolves string annotations through
# sys.modules[cls.__module__], so a module that exists only in a local name
# blows up at class-creation time rather than failing a test.
sys.modules["evaluate"] = evaluate
_spec.loader.exec_module(evaluate)


@pytest.fixture(scope="module")
def results() -> list:
    return evaluate.run()


# ---------------------------------------------------------------------------
# the harness measures what it claims
# ---------------------------------------------------------------------------


def test_both_providers_are_scored(results):
    assert {r.provider for r in results} == {"nvidia", "nebius"}, (
        "the submission's 'both paths live-verified' claim rests on both being here"
    )


def test_every_document_and_field_was_compared(results):
    for result in results:
        assert result.total.comparable == 75, result.provider
        # 12 documents x 10 fields = 120 slots; 45 of them are fields the
        # documents genuinely do not print, which are counted as missed/invented
        # rather than silently dropped.
        assert result.total.missed == 0, (
            f"{result.provider}: a printed field went unreported and the demo "
            "would not have shown it"
        )


def test_normalization_absorbs_every_transcription_difference(results):
    """100% exact beside ~99% verbatim is the thesis of the project stated as a
    number: models transcribe loosely, and none of it moves a comparison."""
    for result in results:
        assert result.total.rate == 100.0, result.provider
        assert result.total.exact > result.total.verbatim or (
            result.total.exact == result.total.verbatim
        )


def test_the_fallback_provider_invents_fields_and_says_so(results):
    """The count is a finding, not a failure.

    MiniCPM-V reads every printed value correctly and additionally reports an
    origin on each bill of lading (it reads the port of loading) and, on one
    packing list, a PO number (it reads INVOICE REFERENCE). Pinning the number
    means the next provider change is visible instead of deniable.
    """
    by_provider = {r.provider: r for r in results}
    assert by_provider["nvidia"].total.invented == 0
    assert by_provider["nebius"].total.invented == 4
    per_field = by_provider["nebius"].per_field
    assert per_field["po_number"].invented == 1
    assert per_field["country_of_origin"].invented == 3


def test_verdict_agreement_differs_by_provider(results):
    by_provider = {r.provider: r for r in results}
    assert len(by_provider["nvidia"].verdicts_ok) == 3
    assert len(by_provider["nebius"].verdicts_ok) == 2, (
        "the invented fields make the messy fixture read as conflicting; if"
        " that ever stops being true, this number and docs/EVAL.md both change"
    )
    assert all("MISMATCH" not in row for row in by_provider["nvidia"].verdicts_ok)


def test_the_floors_hold_against_the_committed_captures(results):
    assert evaluate.check(results) == []


# ---------------------------------------------------------------------------
# the check itself must be able to fail
# ---------------------------------------------------------------------------


# A gate that cannot fail is decoration; test_check_catches_each_kind_of_regression
# below drives every branch of it with a deliberately worse result.


def test_check_catches_each_kind_of_regression():
    ok = next(r for r in _synthetic())
    assert evaluate.check([ok]) == []

    invented = _clone(ok)
    invented.total.invented += 1
    assert any("invented" in p for p in evaluate.check([invented]))

    missed = _clone(ok)
    missed.total.missed += 1
    assert any("missed" in p for p in evaluate.check([missed]))

    exact = _clone(ok)
    exact.total.exact -= 1
    assert any("exact" in p for p in evaluate.check([exact]))

    verdicts = _clone(ok)
    verdicts.verdicts_ok.pop()
    assert any("verdict" in p for p in evaluate.check([verdicts]))

    unknown = _clone(ok)
    unknown.provider = "some-new-host"
    assert any("no recorded floor" in p for p in evaluate.check([unknown]))


def _clone(result):
    copy = evaluate.Result(provider=result.provider)
    copy.total = evaluate.Tally(**result.total.__dict__)
    copy.per_field = {k: evaluate.Tally(**v.__dict__) for k, v in result.per_field.items()}
    copy.verdicts_ok = list(result.verdicts_ok)
    copy.verdicts_bad = list(result.verdicts_bad)
    return copy


def _synthetic():
    for provider, floor in evaluate.FLOORS.items():
        result = evaluate.Result(provider=provider)
        comparable = 75
        result.total = evaluate.Tally(
            exact=comparable, verbatim=comparable - 1, comparable=comparable,
            invented=floor["invented"], missed=floor["missed"],
        )
        result.verdicts_ok = [f"s{i}" for i in range(floor["verdicts"])]
        yield result


# ---------------------------------------------------------------------------
# ground truth must be ground truth
# ---------------------------------------------------------------------------


def test_authored_lookup_refuses_a_live_entry_of_the_same_pages(tmp_path, monkeypatch):
    """The bug this guards is real and was measured: the cache directory holds
    live captures of the same pages, and matching on page hashes alone returned
    another provider's model output as "ground truth"."""
    pages = ["a" * 64]
    authored = tmp_path / "authored.json"
    authored.write_text(json.dumps({
        "page_sha256": pages, "response": {"doc_type": "other"},
        "_provenance": "HAND-AUTHORED fixture response",
    }), encoding="utf-8")
    impostor = tmp_path / "impostor.json"
    impostor.write_text(json.dumps({
        "page_sha256": pages, "response": {"doc_type": "other"},
        "_provenance": "LIVE API response, captured by scripts/live_compare.py",
    }), encoding="utf-8")

    monkeypatch.setattr(evaluate, "AUTHORED_DIR", tmp_path)
    found = evaluate.authored_for(pages)
    assert found is not None
    # Whichever file sorts first, the answer must be the authored one.
    assert json.loads(authored.read_text())["response"] == found


def test_comparing_two_identical_responses_is_exact_and_verbatim():
    index = json.loads((REPO / "fixtures" / "index.json").read_text(encoding="utf-8"))
    key = index["shipments"]["shipment_clean"]["documents"][0]["cache_key"]
    payload = json.loads(
        (REPO / "fixtures" / "responses" / f"{key}.json").read_text(encoding="utf-8")
    )
    truth = evaluate.ExtractionResponse.model_validate(payload["response"])
    tallies = evaluate.compare(truth, truth)
    assert sum(t.exact for t in tallies.values()) == sum(
        t.comparable for t in tallies.values())
    assert sum(t.invented for t in tallies.values()) == 0
    assert sum(t.missed for t in tallies.values()) == 0


# ---------------------------------------------------------------------------
# the document must match the data
# ---------------------------------------------------------------------------


def test_docs_eval_md_is_current(results):
    """A quoted number that no longer matches its source is the exact failure
    mode this whole project is a long argument against."""
    expected = evaluate.render(results)
    actual = (REPO / "docs" / "EVAL.md").read_text(encoding="utf-8")
    assert actual.strip() == expected.strip(), (
        "docs/EVAL.md is stale -- run `python scripts/evaluate.py --write`"
    )


def test_the_document_states_its_own_limits():
    text = (REPO / "docs" / "EVAL.md").read_text(encoding="utf-8")
    assert "synthetic" in text
    assert "not scored" in text.lower()
    assert "Generated by scripts/evaluate.py" in text
