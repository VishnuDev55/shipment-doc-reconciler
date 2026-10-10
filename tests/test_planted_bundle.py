"""The planted-error bundle: its shape, its score, and its one declared miss.

`docs/PLANTED.md` carries the numbers; this file carries the guarantees that
make the numbers mean something. Three things could quietly rot here, and each is
pinned: the bundle on disk drifting from the cases in `fixtures/planted.py`, the
scorer becoming unable to say "wrong", and a case whose expectation was written to
match whatever the code happens to do.

The scoring runs are all offline. Every response is hand-authored under
`fixtures/planted/responses`, so a shipment here costs nothing and returns the
same verdict every time -- which is the entire reason the bundle exists rather
than a live re-run of the same paperwork.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.models import Report
from app.orchestrator import Reconciler

REPO = Path(__file__).resolve().parents[1]
PLANTED = REPO / "fixtures" / "planted"

_spec = importlib.util.spec_from_file_location(
    "evaluate_planted", REPO / "scripts" / "evaluate_planted.py")
assert _spec and _spec.loader
evaluator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evaluator)

CASES = evaluator.CASES
score = evaluator.score

OUTCOMES = {"ready", "quiet", "review", "blocked"}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None, demo_mode=True, vision_provider="nvidia",
        nvidia_api_key="", nebius_api_key="", enable_line_items=True,
        runs_dir=tmp_path / "runs", cache_dir=PLANTED / "responses",
    )


async def _run(tmp_path: Path, case: dict) -> Report:
    uploads, texts = evaluator._uploads(case)
    rec = Reconciler(settings=_settings(tmp_path))
    sid, _ = await rec.create_shipment(uploads, page_texts=texts)
    await rec.wait(sid, timeout=180)
    return rec.get(sid)


def _case(name: str) -> dict:
    return next(c for c in CASES if c["name"] == name)


# ---------------------------------------------------------------------------
# the bundle's own shape
# ---------------------------------------------------------------------------


def test_every_case_declares_an_expectation_the_scorer_understands():
    for case in CASES:
        expect = case["expect"]
        assert expect["outcome"] in OUTCOMES, case["name"]
        assert case["note"].strip(), f"{case['name']} has no explanation"
        assert case["group"] in {"clean", "tricky", "planted", "miss"}, case["group"]
        if expect["outcome"] in {"review", "blocked"}:
            assert expect.get("code") or expect.get("check") or expect.get("excluded"), (
                f"{case['name']} says the report should stop, without naming the "
                f"finding that has to appear for it to be believed")


def test_the_groups_are_populated_enough_to_mean_what_they_say():
    """Counts, not vibes: a bundle that quietly loses its clean cases stops
    measuring false alarms and starts measuring nothing."""
    counts = {group: sum(1 for c in CASES if c["group"] == group)
              for group in ("clean", "tricky", "planted", "miss")}
    assert counts["clean"] >= 3, counts
    assert counts["tricky"] >= 5, counts
    assert counts["planted"] >= 8, counts
    assert counts["miss"] == 1, counts


def test_exactly_one_planted_error_per_planted_case():
    """The bundle's claim is "one thing is wrong, and it was found".

    Each case names one finding code, and any narrowing of it -- which rule
    failed, which field was withdrawn -- has to accompany that code rather than
    stand alone. `math:fail` is one code shared by four arithmetic rules, so
    without the narrowing a single arithmetic error would score every arithmetic
    case, and the bundle would report 9/9 while testing one thing three times.
    """
    for case in CASES:
        if case["group"] != "planted":
            continue
        expect = case["expect"]
        codes = [k for k in ("code",) if expect.get(k)]
        assert codes == ["code"], f"{case['name']} names no finding: {expect}"
        assert expect.get("level") in ("critical", "warning"), (
            f"{case['name']} plants an error without saying how loud it must be")
        if expect.get("check"):
            assert expect["code"] == "math:fail", (
                f"{case['name']} narrows a check under a code that does not share "
                f"one: {expect['code']}")
        if expect.get("excluded"):
            assert expect["code"] == "evidence:excluded", (
                f"{case['name']} names a withdrawn field under {expect['code']}")


def test_the_rendered_bundle_matches_the_cases_that_generated_it():
    """Every case the code declares has pages on disk, and the index agrees with
    the case about what is expected of them.

    Pages are stored once and shared, so two cases that print the same document
    read the same file -- which is what makes one authored response per page
    correct rather than lucky.
    """
    index = json.loads((PLANTED / "index.json").read_text(encoding="utf-8"))
    assert sorted(index["cases"]) == sorted(c["name"] for c in CASES)

    slots, distinct = 0, set()
    for case in CASES:
        entry = index["cases"][case["name"]]
        assert entry["expect"] == case["expect"], case["name"]
        assert len(entry["documents"]) == len(case["docs"]), case["name"]
        for doc in entry["documents"]:
            slots += 1
            distinct.add(doc["file"])
            image = PLANTED / "images" / doc["file"]
            assert image.is_file(), doc["file"]
            sidecar = PLANTED / "text" / f"{Path(doc['file']).stem}.json"
            assert sidecar.is_file(), (
                f"{doc['file']} has no text sidecar, so its quotes cannot be "
                f"checked and its case would score for the wrong reason")

    assert len(distinct) <= slots, "the bundle should share identical pages"


def test_no_two_authored_responses_claim_different_answers_for_one_page():
    """The hazard content-sharing exposes, asserted rather than assumed.

    A cache key is a hash of the page images, so two documents that print the
    same words can have only one response. A case that plants a claim the page
    does not print therefore has to differ from a plain page in something it
    prints, or the score would depend on which file the cache read first.
    """
    by_pages: dict[tuple, int] = {}
    for path in sorted((PLANTED / "responses").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        identity = (tuple(payload["page_sha256"]), payload["prompt_version"])
        by_pages[identity] = by_pages.get(identity, 0) + 1
    clashes = [key for key, count in by_pages.items() if count > 1]
    assert not clashes, f"{len(clashes)} page set(s) carry two authored responses"


def test_no_planted_response_claims_a_live_model():
    """The bundle's honesty in one assertion: if a live capture ever lands here,
    the score stops describing the engine and starts describing a model's mood."""
    for path in sorted((PLANTED / "responses").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert "HAND-AUTHORED" in payload["_provenance"].upper(), path.name


# ---------------------------------------------------------------------------
# the scorer can say "wrong"
# ---------------------------------------------------------------------------


def _report(banner: str, *, codes: tuple[str, ...] = (), level: str = "warning",
            verdict: str = "clean") -> Report:
    from app.models import SeverityFinding, StatusReport

    report = Report(shipment_id="s", created_at="t", updated_at="t")
    report.summary.verdict = verdict
    report.status_report = StatusReport(
        banner=banner, verdict=verdict,
        findings=[SeverityFinding(code=c, severity=level, source="comparison",
                                  label=c, detail="", documents=["Invoice"])
                  for c in codes])
    return report


def test_a_wrong_expectation_fails_instead_of_agreeing():
    """Without this, a scorer that always returned True would still print 9/9."""
    good = _case("planted_container")
    assert score(good, _report("blocked", codes=("comparison:container_number:conflict",),
                               level="critical"))[0] is True
    assert score(good, _report("review", codes=("comparison:container_number:conflict",),
                               level="warning"))[0] is False
    assert score(good, _report("ready"))[0] is False
    assert score(_case("clean_three_documents"),
                 _report("review", codes=("comparison:hs_code:conflict",)))[0] is False


def test_a_math_case_is_only_satisfied_by_the_rule_it_planted():
    """`math:fail` is one code shared by several rules. A bundle where any
    arithmetic error scored every arithmetic case would report 9/9 while testing
    one thing three times."""
    case = _case("planted_line_sum")
    report = _report("review", codes=("math:fail",))
    report.internal_checks = []
    ok, detail = score(case, report)
    assert ok is False, detail

    from app.models import InternalCheck

    report.internal_checks = [InternalCheck(
        code="line_total_math", document_id="d1", document_label="Invoice",
        status="fail", detail="a different rule failed")]
    assert score(case, report)[0] is False

    report.internal_checks = [InternalCheck(
        code="line_total_sum", document_id="d1", document_label="Invoice",
        status="fail", detail="rows add to 8,420.00, total says 9,420.00")]
    assert score(case, report)[0] is True


def test_a_quiet_case_tolerates_the_verdict_floor_but_not_a_warning():
    """A difference of basis leaves the banner at review because the verdict said
    suspicious. That is not a false alarm; a warning about the cargo is."""
    quiet = {"expect": {"outcome": "quiet"}, "name": "x", "group": "tricky",
             "category": "counting basis", "note": "n"}
    assert score(quiet, _report("review"))[0] is True
    assert score(quiet, _report("review", codes=("comparison:quantity:suspicious",)))[0] \
        is False


# ---------------------------------------------------------------------------
# the bundle, run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_planted_container_blocks_the_shipment(tmp_path):
    case = _case("planted_container")
    report = await _run(tmp_path, case)
    ok, detail = score(case, report)
    assert ok, detail
    assert report.status_report.banner == "blocked"
    assert report.run.live_calls == 0
    assert report.run.result_source == "authored", (
        "the bundle must be answered by hand-authored responses, not a capture")


@pytest.mark.asyncio
async def test_the_clean_reference_case_says_ready(tmp_path):
    case = _case("clean_three_documents")
    report = await _run(tmp_path, case)
    ok, detail = score(case, report)
    assert ok, detail
    assert report.status_report.critical == 0
    assert report.status_report.warning == 0


@pytest.mark.asyncio
async def test_the_designed_miss_is_a_miss_and_is_declared_as_one(tmp_path):
    """The case the engine cannot catch, asserted in both directions: the report
    says ready, and the bundle calls that a miss rather than a pass."""
    case = _case("miss_inside_tolerance")
    report = await _run(tmp_path, case)
    assert report.status_report.banner == "ready"
    assert case["expect"]["caught"] is False
    assert score(case, report)[0] is True, "the scorer must not count it as a catch"
    comparison = next(c for c in report.comparisons if c.field == "gross_weight")
    assert comparison.status == "consistent", comparison.reason


@pytest.mark.asyncio
async def test_the_invented_value_is_withdrawn_rather_than_turned_into_a_conflict(tmp_path):
    case = _case("planted_invented_value")
    report = await _run(tmp_path, case)
    ok, detail = score(case, report)
    assert ok, detail
    assert [u.field for u in report.unverified] == ["po_number"]
    comparison = next(c for c in report.comparisons if c.field == "po_number")
    assert comparison.status != "conflict", comparison.reason
