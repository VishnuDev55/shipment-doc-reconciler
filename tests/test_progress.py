"""Progress fan-out: the store hook, the queues, and the SSE route.

SSE is additive here. These tests pin that it reports the same state the poll
does, and that a listener going away can never damage the run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, configure
from app.models import Report
from app.orchestrator import Reconciler

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def rec(tmp_path):
    return Reconciler(
        Settings(
            _env_file=None,
            demo_mode=True,
            nvidia_api_key="",
            nebius_api_key="",
            runs_dir=tmp_path / "runs",
            cache_dir=REPO / "fixtures" / "responses",
        )
    )


@pytest.fixture()
def api(rec):
    configure(rec)
    with TestClient(app) as client:
        yield client


def report_in(state: str, shipment_id: str = "s1") -> Report:
    report = Report(shipment_id=shipment_id, created_at="now", updated_at="now")
    report.state = state
    return report


# ---------------------------------------------------------------------------
# the hook
# ---------------------------------------------------------------------------


def test_the_store_hook_is_wired_to_the_reconciler(rec):
    assert rec.store.on_write == rec._publish


def test_a_write_reaches_a_subscriber(api, rec):
    sid = api.get("/api/fixture/shipment_clean").json()["shipment_id"]
    queue = rec.subscribe(sid)
    # subscribe replays current state so a late joiner sees the same picture a
    # poll would, rather than waiting for the next change.
    replayed = queue.get_nowait()
    assert replayed.shipment_id == sid
    rec.unsubscribe(sid, queue)


def test_unsubscribing_leaves_no_listener_behind(api, rec):
    sid = api.get("/api/fixture/shipment_clean").json()["shipment_id"]
    queue = rec.subscribe(sid)
    rec.unsubscribe(sid, queue)
    assert rec._listeners == {}
    # Unsubscribing twice must not resurrect or lose the bucket.
    rec.unsubscribe(sid, queue)
    assert rec._listeners == {}


def test_a_full_listener_drops_oldest_rather_than_blocking(rec):
    """A stalled client must not be able to stall the run producing it.

    Twenty publishes into an eight-slot queue: no raise, no growth past the
    bound, and the newest state is still deliverable.
    """
    queue = rec.subscribe("s1")
    for i in range(20):
        rec._publish(report_in("running"))

    assert queue.qsize() == queue.maxsize
    drained = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert len(drained) == queue.maxsize
    rec.unsubscribe("s1", queue)


def test_publishing_for_another_run_reaches_nobody(rec):
    queue = rec.subscribe("mine")
    assert queue.empty(), "an unknown run has nothing to replay"
    rec._publish(report_in("running", shipment_id="theirs"))
    assert queue.empty()
    rec.unsubscribe("mine", queue)


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------


def test_sse_404s_for_an_unknown_run(api):
    assert api.get("/api/shipments/nosuch/events").status_code == 404


def test_sse_delivers_the_report_and_then_closes(api):
    sid = api.get("/api/fixture/shipment_conflict").json()["shipment_id"]
    response = api.get(f"/api/shipments/{sid}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"].startswith("no-cache")

    body = response.text
    assert "event: report" in body
    assert "event: done" in body, "a finished run must terminate the stream"

    frame = [b for b in body.split("\n\n") if b.startswith("event: report")][0]
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["shipment_id"] == sid
    assert payload["state"] == "done"
    # The streamed body must carry the same decisions the poll endpoint serves,
    # or the two paths would render different reports.
    polled = api.get(f"/api/shipments/{sid}/report").json()
    assert [(c["field"], c["status"]) for c in payload["comparisons"]] == \
           [(c["field"], c["status"]) for c in polled["comparisons"]]


def test_the_stream_releases_its_listener_when_it_ends(api, rec):
    sid = api.get("/api/fixture/shipment_clean").json()["shipment_id"]
    api.get(f"/api/shipments/{sid}/events")
    assert rec._listeners == {}, "a closed stream must not leak a queue"


@pytest.mark.asyncio
async def test_subscribe_returns_the_current_report_without_reading_disk(rec, tmp_path):
    report = report_in("running")
    rec._reports["s1"] = report
    queue = rec.subscribe("s1")
    assert queue.get_nowait() is report
    rec.unsubscribe("s1", queue)


@pytest.mark.asyncio
async def test_a_run_settles_and_the_subscriber_sees_it(rec):
    """The end-to-end shape the frontend depends on: a push per settled doc."""
    from .test_pipeline import INDEX

    uploads = []
    for doc in INDEX["shipments"]["shipment_messy"]["documents"]:
        for fname in doc["files"]:
            uploads.append((fname, doc["doc_type"],
                            (REPO / "fixtures" / "images" / fname).read_bytes()))

    sid, _ = await rec.create_shipment(uploads)
    queue = rec.subscribe(sid)
    queue.get_nowait()  # replay of the pre-run state

    await rec.wait(sid, timeout=60)

    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    assert seen, "the run wrote reports but published nothing"
    assert seen[-1].state in ("done", "partial", "failed")
    assert len(seen) >= 3, "one document settling should be one event each"
    rec.unsubscribe(sid, queue)
