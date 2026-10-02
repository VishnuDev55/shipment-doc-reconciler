"""Human corrections: verdicts move instantly, the model's answer survives.

The guarantees worth testing are not the happy path — they are that a correction
costs no API call, that reverting is exact, and that the trail cannot be quietly
rewritten.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.correct import CorrectionError, apply_correction
from app.main import app, configure
from app.orchestrator import Reconciler

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(tmp_path):
    rec = Reconciler(Settings(
        _env_file=None, demo_mode=True, nvidia_api_key="", nebius_api_key="",
        runs_dir=tmp_path / "runs", cache_dir=REPO / "fixtures" / "responses"))
    configure(rec)
    with TestClient(app) as client:
        sid = client.get("/api/fixture/shipment_conflict").json()["shipment_id"]
        yield client, rec, sid


def bol(report):
    return next(d for d in report["documents"] if "bol" in d["filename"])


def cmp_of(report, name):
    return next(c for c in report["comparisons"] if c["field"] == name)


# ---------------------------------------------------------------------------
# the headline behaviour
# ---------------------------------------------------------------------------


def test_correcting_the_disagreeing_document_resolves_the_conflict(env):
    client, _, sid = env
    before = client.get(f"/api/shipments/{sid}/report").json()
    assert cmp_of(before, "gross_weight")["status"] == "conflict"

    after = client.post(
        f"/api/shipments/{sid}/documents/{bol(before)['id']}/fields/gross_weight",
        json={"value": "12,450.00 KGS", "actor": "vishnu"}).json()

    assert cmp_of(after, "gross_weight")["status"] == "consistent"
    assert after["summary"]["conflict"] == before["summary"]["conflict"] - 1


def test_a_correction_costs_no_api_call(env):
    """The whole point of deterministic downstream: fixing a value must not
    re-bill the provider."""
    client, _, sid = env
    before = client.get(f"/api/shipments/{sid}/report").json()

    after = client.post(
        f"/api/shipments/{sid}/documents/{bol(before)['id']}/fields/gross_weight",
        json={"value": "12,450.00 KGS"}).json()

    assert after["run"]["live_calls"] == before["run"]["live_calls"] == 0
    for doc in after["documents"]:
        assert doc["usage"] == {"prompt": 0, "completion": 0, "total": 0}
        assert doc["timings_ms"] == next(
            d["timings_ms"] for d in before["documents"] if d["id"] == doc["id"])


def test_the_models_answer_is_preserved_verbatim_alongside_the_correction(env):
    client, _, sid = env
    before = client.get(f"/api/shipments/{sid}/report").json()
    doc_id = bol(before)["id"]
    original = next(d for d in before["documents"] if d["id"] == doc_id)
    model_said = original["extraction"]["fields"]["gross_weight"]["value"]

    after = client.post(
        f"/api/shipments/{sid}/documents/{doc_id}/fields/gross_weight",
        json={"value": "12,450.00 KGS"}).json()
    corrected = next(d for d in after["documents"] if d["id"] == doc_id)

    assert corrected["extraction"]["fields"]["gross_weight"]["value"] == model_said
    assert corrected["overrides"]["gross_weight"]["value"] == "12,450.00 KGS"
    assert corrected["normalized"]["gross_weight"]["canonical"] == "12450.000 kg"


def test_reverting_restores_the_original_verdict_exactly(env):
    client, _, sid = env
    before = client.get(f"/api/shipments/{sid}/report").json()
    doc_id = bol(before)["id"]

    changed = client.post(
        f"/api/shipments/{sid}/documents/{doc_id}/fields/gross_weight",
        json={"value": "12,450.00 KGS"}).json()
    assert cmp_of(changed, "gross_weight")["status"] == "consistent"

    back = client.post(
        f"/api/shipments/{sid}/documents/{doc_id}/fields/gross_weight/revert").json()

    def snapshot(r):
        return [(c["field"], c["status"], c["reason"],
                 [(v["document_id"], v["canonical"]) for v in c["values"]])
                for c in r["comparisons"]]

    assert snapshot(back) == snapshot(before)
    assert back["risk"]["score"] == before["risk"]["score"]


def test_the_trail_is_append_only(env):
    """Reverting must not erase history, or an audit of who changed what is
    worth nothing."""
    client, _, sid = env
    doc_id = bol(client.get(f"/api/shipments/{sid}/report").json())["id"]

    client.post(f"/api/shipments/{sid}/documents/{doc_id}/fields/gross_weight",
                json={"value": "12,450.00 KGS", "actor": "alex"})
    client.post(f"/api/shipments/{sid}/documents/{doc_id}/fields/gross_weight/revert")

    trail = client.get(f"/api/shipments/{sid}/corrections").json()
    assert len(trail["corrections"]) == 1
    assert trail["active"] == 0
    entry = trail["corrections"][0]
    assert entry["actor"] == "alex"
    assert entry["reverted_at"], "a reverted entry must still say it was reverted"
    assert entry["original_value"] == "13,200.00 KGS"


def test_risk_score_recomputes_from_the_corrected_values(env):
    client, _, sid = env
    before = client.get(f"/api/shipments/{sid}/report").json()
    doc_id = bol(before)["id"]

    after = client.post(
        f"/api/shipments/{sid}/documents/{doc_id}/fields/gross_weight",
        json={"value": "12,450.00 KGS"}).json()

    assert after["risk"]["score"] < before["risk"]["score"]
    assert "weight_conflict" not in {c["code"] for c in after["risk"]["contributions"]}


def test_a_written_back_value_can_clear_a_cross_field_finding(env):
    """Sanity is recomputed too, so a basis error a human fixes stops being
    reported."""
    client, rec, sid = env
    report = rec.get(sid)
    doc = report.documents[0]
    apply_correction(report, doc.id, "quantity", "1:1 pallet")
    rec._resettle(report)
    assert all(f.code != "unit_mass_high" for f in report.sanity)


def test_the_corrected_value_is_persisted_not_just_returned(env, tmp_path):
    client, _, sid = env
    doc_id = bol(client.get(f"/api/shipments/{sid}/report").json())["id"]
    client.post(f"/api/shipments/{sid}/documents/{doc_id}/fields/gross_weight",
                json={"value": "12,450.00 KGS", "actor": "vishnu"})

    stored = json.loads((tmp_path / "runs" / sid / "report.json").read_text(encoding="utf-8"))
    assert len(corrections_of(stored)) == 1


def corrections_of(report) -> list:
    return [c for d in report["documents"] for c in d.get("corrections", [])]


def test_a_correction_publishes_to_live_sse_listeners(env):
    client, rec, sid = env
    queue = rec.subscribe(sid)
    while not queue.empty():
        queue.get_nowait()

    doc_id = bol(client.get(f"/api/shipments/{sid}/report").json())["id"]
    client.post(f"/api/shipments/{sid}/documents/{doc_id}/fields/gross_weight",
                json={"value": "12,450.00 KGS"})

    pushed = queue.get_nowait()
    assert cmp_of(pushed.model_dump(), "gross_weight")["status"] == "consistent"
    rec.unsubscribe(sid, queue)


def test_narrative_is_marked_superseded_not_deleted(env):
    """A paragraph written about the old verdicts is still model output; it just
    must not read as current."""
    client, rec, sid = env
    report = rec.get(sid)
    report.narrative.status = "ok"
    report.narrative.text = "The gross weight disagrees across documents."
    doc_id = bol(client.get(f"/api/shipments/{sid}/report").json())["id"]

    after = client.post(
        f"/api/shipments/{sid}/documents/{doc_id}/fields/gross_weight",
        json={"value": "12,450.00 KGS"}).json()

    assert after["narrative"]["text"]
    assert after["narrative"]["superseded_at"]


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_an_empty_correction_is_refused(env):
    client, _, sid = env
    doc_id = bol(client.get(f"/api/shipments/{sid}/report").json())["id"]
    res = client.post(f"/api/shipments/{sid}/documents/{doc_id}/fields/gross_weight",
                      json={"value": "   "})
    assert res.status_code == 400
    assert "deletion" in res.json()["detail"]


def test_an_unknown_field_is_refused(env):
    client, _, sid = env
    doc_id = bol(client.get(f"/api/shipments/{sid}/report").json())["id"]
    assert client.post(
        f"/api/shipments/{sid}/documents/{doc_id}/fields/banana",
        json={"value": "x"}).status_code == 400


def test_an_unknown_document_is_refused(env):
    client, _, sid = env
    res = client.post(f"/api/shipments/{sid}/documents/nope/fields/gross_weight",
                      json={"value": "x"})
    assert res.status_code == 400


def test_an_unknown_shipment_is_404(env):
    client, _, _ = env
    assert client.post("/api/shipments/nosuch/documents/d1/fields/gross_weight",
                       json={"value": "x"}).status_code == 404


def test_reverting_with_nothing_active_is_refused(env):
    client, _, sid = env
    doc_id = bol(client.get(f"/api/shipments/{sid}/report").json())["id"]
    res = client.post(
        f"/api/shipments/{sid}/documents/{doc_id}/fields/gross_weight/revert")
    assert res.status_code == 400
    assert "no active correction" in res.json()["detail"]


def test_a_document_that_never_parsed_cannot_be_corrected(env, tmp_path):
    rec = Reconciler(Settings(
        _env_file=None, demo_mode=True, runs_dir=tmp_path / "r2",
        cache_dir=tmp_path / "empty"))
    configure(rec)
    with TestClient(app) as client:
        sid, report = await_run(client, rec)
        doc = report.documents[0]
        assert doc.extraction is None
        with pytest.raises(CorrectionError, match="no extraction"):
            apply_correction(report, doc.id, "gross_weight", "1 kg")


def await_run(client, rec):
    import asyncio
    from .test_pipeline import INDEX

    uploads = [(f, d["doc_type"],
                (REPO / "fixtures" / "images" / f).read_bytes())
               for d in INDEX["shipments"]["shipment_clean"]["documents"]
               for f in d["files"]]

    async def go():
        return await rec.create_shipment(uploads)

    sid, report = asyncio.run(go())
    return sid, report
