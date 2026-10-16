"""Evidence the run keeps: raw bodies, the host that answered, and token cost.

docs/AUDIT.md B1: this data was produced and then thrown away. `ExtractionClient`
was built without `raw_dir`, so `runs/{id}/raw/` was empty in every production
run; per-attempt token usage was summed nowhere and the summary's usage was
assigned to `_usage` and dropped. The transparency panel cannot be honest without
these, so each one is pinned here rather than merely wired up.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.client import ExtractionClient, ExtractionOutcome, AttemptLog
from app.config import Settings
from app.main import app, configure
from app.models import DocumentReport, Narrative, Report, TokenUsage
from app.orchestrator import Reconciler, apply_outcome

from .test_client import good_response, make_settings, pages
from .test_pipeline import run_shipment, settings_for


class UsageCompletions:
    """Like test_client's fake, but reports token usage.

    A separate class on purpose: `usage=None` is what the existing fake returns,
    and reusing it here would make every usage assertion vacuously true.
    """

    def __init__(self, script):
        self.script = script
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        text, (prompt, completion) = self.script[
            min(len(self.calls) - 1, len(self.script) - 1)
        ]
        message = SimpleNamespace(content=text, refusal=None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        usage = SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )
        return SimpleNamespace(choices=[choice], usage=usage)


class UsageClient:
    def __init__(self, script):
        self.chat = SimpleNamespace(completions=UsageCompletions(script))


def live_client(tmp_path, **over) -> ExtractionClient:
    """A live-mode client whose transport is a script and whose probe is done."""
    client = ExtractionClient(make_settings(tmp_path, **over))
    client.json_mode = "plain"
    client._probed = True
    return client


def live_summary_settings(tmp_path):
    """Live mode with a usable key, and an empty narrative cache.

    The shipped cache holds real AI narratives keyed on verdict signature, so
    without redirecting it the summary would be served from disk and the
    transport double would never be called.
    """
    return settings_for(tmp_path).model_copy(
        update={
            "demo_mode": False,
            "nebius_api_key": "test-key-not-real",
            "enable_summary": True,
            "cache_dir": tmp_path / "narratives",
        }
    )


async def populated_report(tmp_path):
    """A real run through the real pipeline: _write_summary needs comparisons.

    The narrative is reset because a demo run fills it from the shipped
    `responses/narratives` cache, and a summary test that started from that
    would be asserting against text it never asked for.
    """
    rec, shipment_id = await run_shipment(tmp_path, "shipment_clean")
    report = rec.get(shipment_id)
    assert report.comparisons and report.summary.total_fields
    report.narrative = Narrative()
    return report


# ---------------------------------------------------------------------------
# the B1 regression: does the evidence reach the disk at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_model_bodies_land_in_the_run_directory(tmp_path):
    rec, shipment_id = await run_shipment(tmp_path, "shipment_clean")
    report = rec.get(shipment_id)

    raw_dir = tmp_path / "runs" / shipment_id / "raw"
    assert raw_dir.is_dir(), "the run created no raw/ directory"
    on_disk = {p.name for p in raw_dir.iterdir()}
    assert on_disk, "runs/{id}/raw/ is empty — the exact B1 defect"

    for doc in report.documents:
        assert doc.raw_files, f"{doc.label} recorded no raw output"
        for name in doc.raw_files:
            assert name in on_disk, f"{name} listed in the report but absent from disk"


@pytest.mark.asyncio
async def test_the_persisted_body_is_the_models_json_answer(tmp_path):
    """Not a re-serialization: what the model emitted, parseable on its own."""
    rec, shipment_id = await run_shipment(tmp_path, "shipment_conflict")
    report = rec.get(shipment_id)
    doc = report.documents[0]
    body = (tmp_path / "runs" / shipment_id / "raw" / doc.raw_files[0]).read_text()
    payload = json.loads(body)
    assert "fields" in payload and "doc_type" in payload


@pytest.mark.asyncio
async def test_each_document_names_the_host_and_model_that_answered(tmp_path):
    rec, shipment_id = await run_shipment(tmp_path, "shipment_clean")
    report = rec.get(shipment_id)
    for doc in report.documents:
        assert doc.provider in {"nvidia", "nebius"}
        assert doc.model
        assert doc.source.startswith("cached_"), "fixture replay must not claim live"


@pytest.mark.asyncio
async def test_a_replayed_document_costs_nothing_but_still_has_evidence(tmp_path):
    """Zero tokens and zero evidence are different claims; keep them separate.

    A cache replay made no call on this run, so usage is legitimately zero —
    while the cached body remains the model's real output. If a future change
    suppressed the raw write for cached documents, the panel would have nothing
    to show for its own provenance label.
    """
    rec, shipment_id = await run_shipment(tmp_path, "shipment_clean")
    doc = rec.get(shipment_id).documents[0]
    assert doc.usage.total == 0
    assert doc.raw_files


# ---------------------------------------------------------------------------
# the per-call directory, which is what makes a shared client safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_call_raw_dir_wins_over_the_client_default(tmp_path):
    """Two concurrent runs must not write into each other's evidence.

    The server builds one ExtractionClient for every shipment, so a
    constructor-level raw_dir would funnel all runs into whichever created it
    first. This is the invariant the override exists to protect.
    """
    default_dir = tmp_path / "default"
    run_dir = tmp_path / "this_run"
    client = ExtractionClient(make_settings(tmp_path), raw_dir=default_dir)
    client.json_mode = "plain"
    client._probed = True
    client._client = UsageClient([(good_response(), (10, 5))])

    await client.extract_document(
        document_id="d1", pages=pages(), raw_dir=run_dir
    )

    assert {p.name for p in run_dir.iterdir()}, "the per-call directory received nothing"
    assert not default_dir.exists(), "the client default leaked a file into another run"


@pytest.mark.asyncio
async def test_usage_is_summed_across_every_ladder_rung(tmp_path):
    """The ladder bills each rung, so reporting only the last one understates cost."""
    client = live_client(tmp_path)
    client._client = UsageClient(
        [("{ not json at all", (100, 20)), (good_response(), (50, 30))]
    )

    outcome = await client.extract_document(document_id="d1", pages=pages())
    assert len(outcome.attempts) == 2, "expected the corrected rung to fire"

    doc = DocumentReport(id="d1", label="Invoice", filename="inv.png")
    apply_outcome(doc, outcome)
    assert doc.usage == TokenUsage(prompt=150, completion=50, total=200)
    assert doc.timings_ms == sum(a.latency_ms for a in outcome.attempts)


def test_apply_outcome_carries_host_usage_and_raw_files():
    doc = DocumentReport(id="d1", label="Invoice", filename="inv.png")
    outcome = ExtractionOutcome(
        response=None,
        status="failed",
        attempts=[
            AttemptLog(rung="initial", raw_text="x", ok=False,
                       usage={"prompt": 7, "completion": 3, "total": 10}, latency_ms=250),
            AttemptLog(rung="corrected", raw_text="y", ok=False,
                       usage={"prompt": 5, "completion": 5, "total": 10}),
        ],
        issues=[],
        dropped_fields=[],
        used_cache=False,
        used_fallback_schema=False,
        error="unparsable",
        provider="nebius",
        model="openbmb/MiniCPM-V-4_5",
        raw_files=["d1_initial.txt", "d1_corrected.txt"],
    )

    apply_outcome(doc, outcome)

    assert doc.provider == "nebius"
    assert doc.model == "openbmb/MiniCPM-V-4_5"
    assert doc.usage == TokenUsage(prompt=12, completion=8, total=20)
    assert doc.raw_files == ["d1_initial.txt", "d1_corrected.txt"]
    assert doc.timings_ms == 250, "latency from attempts, not from wall clock"


# ---------------------------------------------------------------------------
# the summary: usage used to be assigned to `_usage` and discarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_usage_reaches_the_narrative(tmp_path):
    """The whole reason this test exists: usage was assigned to `_usage` and lost."""
    report = await populated_report(tmp_path)
    rec = Reconciler(live_summary_settings(tmp_path))

    async def fake_summarize(prompt):
        assert prompt  # the real verdict-bearing prompt, built by the caller
        # Must clear the 40-character floor in summary_is_plausible, or the
        # guard rejects it and the test proves nothing about usage.
        return (
            "All three documents agree on weight, container and value. "
            "No discrepancies need attention.",
            {"prompt": 400, "completion": 60, "total": 460},
        )

    rec.summary_client.summarize = fake_summarize
    await rec._write_summary(report)

    assert report.narrative.status == "ok"
    assert report.narrative.source == "generated"
    assert report.narrative.usage == TokenUsage(prompt=400, completion=60, total=460)


@pytest.mark.asyncio
async def test_summary_usage_is_not_invented_when_the_answer_is_rejected(tmp_path):
    """The point is the absence.

    A paragraph rejected by `summary_is_plausible` produced no answer, so it
    cannot carry a token count as though it had. Recording the cost of a
    rejection would be honest about spend but misleading next to the text field,
    which is empty.
    """
    report = await populated_report(tmp_path)
    rec = Reconciler(live_summary_settings(tmp_path))

    async def implausible(prompt):
        # "analysis:" is one of the chain-of-thought markers the guard rejects.
        return "analysis: " + "let me check the tokens again. " * 5, {
            "prompt": 900, "completion": 300, "total": 1200
        }

    rec.summary_client.summarize = implausible
    await rec._write_summary(report)

    assert report.narrative.status == "failed"
    assert report.narrative.text is None
    assert report.narrative.usage.total == 0


# ---------------------------------------------------------------------------
# the route that hands a body to the browser
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def api(tmp_path):
    rec = Reconciler(
        Settings(
            _env_file=None,
            demo_mode=True,
            nvidia_api_key="",
            nebius_api_key="",
            runs_dir=tmp_path / "runs",
            cache_dir=REPO / "fixtures" / "responses",
        )
    )
    configure(rec)
    with TestClient(app) as client:
        yield client


def one_run(client) -> tuple[str, str]:
    report = client.get("/api/fixture/shipment_conflict").json()
    sid = report["shipment_id"]
    name = client.get(f"/api/shipments/{sid}/report").json()["documents"][0]["raw_files"][0]
    return sid, name


def test_raw_route_serves_a_body_the_report_listed(api):
    sid, name = one_run(api)
    res = api.get(f"/api/shipments/{sid}/raw/{name}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert "doc_type" in res.text


def test_raw_route_refuses_a_name_the_report_never_listed(api):
    sid, _ = one_run(api)
    assert api.get(f"/api/shipments/{sid}/raw/zzz.txt").status_code == 404


def test_raw_route_blocks_traversal(api):
    sid, _ = one_run(api)
    for probe in (
        "..%2Freport.json",
        "..%2F..%2F..%2Fetc%2Fpasswd",
        "../report.json",
    ):
        assert api.get(f"/api/shipments/{sid}/raw/{probe}").status_code == 404, probe


def test_raw_route_404s_for_an_unknown_run(api):
    assert api.get("/api/shipments/nosuch/raw/anything.txt").status_code == 404


# ---------------------------------------------------------------------------
# the shared step table that UI, HTML and PDF all read
# ---------------------------------------------------------------------------


def _step_report() -> Report:
    from app.models import (
        Adjudication, Briefing, Comparison, ConsensusResult, RunInfo, Summary,
    )

    report = Report(shipment_id="s1", created_at="now", updated_at="now")
    report.run = RunInfo(
        prompt_version="t", normalizer_version="t", comparator_version="t",
        model="nvidia/nemotron", vision_provider="nvidia",
        summary_model="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        summary_provider="nebius", demo_mode=False, json_schema_mode="strict",
        result_source="live", live_calls=3, usd_per_mtok=2.0,
    )
    doc = DocumentReport(
        id="d1", label="Invoice", filename="inv.png", status="ok", attempts=1,
        timings_ms=900, source="live_api", provider="nvidia",
        model="nvidia/nemotron", usage=TokenUsage(prompt=100, completion=50, total=150),
    )
    doc.consensus = ConsensusResult(
        status="ok", provider="nebius", model="openbmb/MiniCPM-V-4_5",
        source="generated", latency_ms=800,
        usage=TokenUsage(prompt=90, completion=60, total=150),
    )
    report.documents = [doc]
    report.summary = Summary(verdict="review")
    report.narrative = Narrative(
        status="ok", text="All three documents agree on the main fields.",
        model="m", provider="nebius", source="generated", latency_ms=700,
        usage=TokenUsage(prompt=10, completion=5, total=15),
    )
    report.briefing = Briefing(
        status="ok", text="Dear supplier, please confirm the consignee.",
        model="m", provider="nebius", source="generated", latency_ms=600,
        usage=TokenUsage(prompt=8, completion=4, total=12),
    )
    comp = Comparison(field="consignee_name", label="Consignee", kind="party_name",
                      status="suspicious", reason="similar", majority_compare_key="ACME",
                      values=[])
    comp.adjudication = Adjudication(
        status="ok", same=True, confidence=0.8,
        rationale="Same legal entity; only the corporate suffix differs.",
        model="m", provider="nebius", source="generated", latency_ms=500,
        usage=TokenUsage(prompt=5, completion=2, total=7),
    )
    report.comparisons = [comp]
    return report


def test_model_steps_lists_every_step_in_run_order():
    from app.transparency import model_steps

    steps = model_steps(_step_report())
    assert [s["step"] for s in steps] == [
        "Invoice",
        "second reader · Invoice",
        "Executive summary",
        "Draft discrepancy email",
        "Second opinion · Consignee",
    ]
    assert [s["source"] for s in steps] == [
        "live_api", "generated", "live_api", "live_api", "live_api",
    ]


def test_totals_sum_across_every_step():
    from app.transparency import model_steps, totals

    tokens, ms = totals(model_steps(_step_report()))
    assert tokens == 150 + 150 + 15 + 12 + 7
    assert ms == 900 + 800 + 700 + 600 + 500


def test_powered_by_is_honest_about_what_ran():
    from app.transparency import powered_by

    report = _step_report()
    assert "3 live API call(s)" in powered_by(report)
    report.run.live_calls = 0
    assert "no live call" in powered_by(report)


def test_cost_line_refuses_to_invent_a_price():
    from app.transparency import cost_line

    report = _step_report()
    assert "≈$" in cost_line(report, 1_000_000, 2_000)
    report.run.usd_per_mtok = 0.0
    assert "not configured" in cost_line(report, 1_000_000, 2_000)
    assert "$" not in cost_line(report, 1_000_000, 2_000)


def test_pdf_and_standalone_html_print_the_same_model_steps():
    from app.export import standalone_html
    from app.pdf import report_html

    report = _step_report()
    page = report_html(report)
    assert "Model steps" in page
    assert "Powered by Nebius Token Factory + NVIDIA Nemotron" in page
    assert "Second opinion" in page
    html_page = standalone_html(report)
    assert "Model steps" in html_page
    assert "Powered by Nebius Token Factory + NVIDIA Nemotron" in html_page
