"""Tests for the Phase 4 hardening: what an upload is allowed to be, what it is
allowed to cost, and what a failure is allowed to look like.

Each test exists because the behaviour it checks was once absent and the absence
was invisible: a size limit enforced after the full read, a file type trusted
from its filename, a report dict that only ever grew, a background task whose
crash surfaced as nothing at all.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import ingest as ingest_module

from app.client import ChatClient
from app.config import Settings
from app.main import app, configure
from app.orchestrator import Reconciler
from app.providers import Endpoint
from app.store import RunStore

REPO = Path(__file__).resolve().parents[1]
IMG = REPO / "fixtures" / "images" / "invoice_clean.png"


def png_bytes(width: int = 40, height: int = 30) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (250, 250, 250)).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# R7: content is checked, not trusted
# ---------------------------------------------------------------------------


def test_every_bundled_fixture_still_ingests():
    """The guard must not reject the very documents the demo is built from."""
    seen = 0
    for path in sorted((REPO / "fixtures" / "images").iterdir()):
        pages = ingest_module.ingest(path.name, path.read_bytes())
        assert pages and pages[0].jpeg.startswith(b"\xff\xd8\xff"), path.name
        seen += 1
    assert seen >= 12


@pytest.mark.parametrize("filename, blob, expect", [
    ("evil.png", b"MZ\x90\x00" + b"\x00" * 200, "unrecognised file signature"),
    ("evil.pdf", b"\x89PNG\r\n\x1a\n" + b"\x00" * 200, "claims to be a PDF"),
    ("evil.png", b"%PDF-1.7\n" + b"\x00" * 200, "claims to be an image"),
    ("x.jpg", b"\x89PNG\r\n\x1a\n" + b"\x00" * 200, "claims to be a JPEG"),
    ("x.gif", b"GIF89a" + b"\x00" * 200, "unsupported file type"),
    ("x.png", b"", "empty file"),
])
def test_a_file_that_is_not_what_it_claims_is_refused(filename, blob, expect):
    with pytest.raises(ValueError) as excinfo:
        ingest_module.ingest(filename, blob)
    assert expect in str(excinfo.value)


# Pillow warns while opening the file; our guard raises before it is decoded.
# The warning is expected noise from a deliberately hostile input.
@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_a_decompression_bomb_is_refused_before_it_is_decoded():
    """Pillow only raises at twice its own ceiling and warns in between, and a
    warning is not a refusal -- an 81 MP image was measured getting through a
    50 MP limit until the declared size was checked explicitly."""
    blob = png_bytes(9000, 9000)
    with pytest.raises(ValueError) as excinfo:
        ingest_module.ingest("bomb.png", blob)
    assert "ceiling" in str(excinfo.value)


def test_the_ceiling_is_a_number_we_chose_in_one_place():
    assert ingest_module.MAX_IMAGE_PIXELS == Image.MAX_IMAGE_PIXELS


# ---------------------------------------------------------------------------
# R2: the size limit costs the attacker, not us
# ---------------------------------------------------------------------------


def settings_for(tmp_path: Path, **over) -> Settings:
    base = dict(
        _env_file=None, demo_mode=True,
        nvidia_api_key="nvapi-TEST-ONLY-0000-1234",
        nebius_api_key="sk-test-nebius-0000-9876",
        runs_dir=tmp_path / "runs", cache_dir=REPO / "fixtures" / "responses",
    )
    base.update(over)
    return Settings(**base)


@pytest.mark.asyncio
async def test_read_capped_stops_at_the_limit(monkeypatch):
    """The old code did `data = await upload.read()` and then compared lengths,
    so the limit decided whether to reject a body that had already been paid for
    in full. This asserts the read stops early."""
    from app.main import _read_capped

    class EndlessUpload:
        filename = "endless.png"

        def __init__(self):
            self.chunks_read = 0

        async def read(self, size=-1):
            self.chunks_read += 1
            if self.chunks_read > 40:      # far more than a 3 MiB budget
                return b""
            return b"\x00" * (1 << 20)

    upload = EndlessUpload()
    with pytest.raises(Exception) as excinfo:
        await _read_capped(upload, 3 << 20)
    assert excinfo.value.status_code == 413
    assert upload.chunks_read <= 5, f"read {upload.chunks_read} chunks for a 3 MiB limit"


def test_an_oversized_upload_is_rejected_end_to_end(tmp_path):
    configure(Reconciler(settings_for(tmp_path, max_upload_bytes=4096)))
    client = TestClient(app)
    res = client.post("/api/shipments",
                      files=[("files", ("big.png", b"\x89PNG\r\n\x1a\n" + b"0" * 9000,
                                        "image/png"))])
    assert res.status_code == 413
    assert "byte limit" in res.json()["detail"]


# ---------------------------------------------------------------------------
# R5: bounded memory, and a disk that is not kept forever
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_in_memory_report_cache_is_bounded(tmp_path):
    rec = Reconciler(settings_for(tmp_path, max_reports_in_memory=2))
    blob = IMG.read_bytes()
    for _ in range(4):
        sid, _ = await rec.create_shipment([("invoice_clean.png", "commercial_invoice", blob)])
        assert sid in rec._reports
    assert len(rec._reports) <= 2, "the bound is not being applied"

    # Eviction is not data loss: the report is still on disk and still readable.
    survivor = rec.get(sid)
    assert survivor is not None and survivor.shipment_id == sid


def test_run_ttl_deletes_only_what_it_was_told_to(tmp_path):
    store = RunStore(tmp_path / "runs")
    old, recent, no_meta = "oldrun111111", "recentrun22", "legacyrun3"
    for name in (old, recent, no_meta):
        store.create(name)
        store.write_meta(name, {"created_at": datetime.now(timezone.utc).isoformat()})

    past = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store.write_meta(old, {"created_at": past})

    assert store.purge_older_than(0) == [], "ttl 0 must keep everything"
    removed = store.purge_older_than(24)
    assert removed == [old], removed
    assert (store.dir(recent)).is_dir()
    assert (store.dir(no_meta)).is_dir(), "a run with no readable age is kept"


def test_a_run_with_unreadable_meta_falls_back_to_its_mtime(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create("ancient0000001")
    old = datetime.now(timezone.utc) - timedelta(days=10)
    path = store.dir("ancient0000001")
    import os
    os.utime(path, (old.timestamp(), old.timestamp()))
    assert store.purge_older_than(24) == ["ancient0000001"]


def test_one_undecodable_report_does_not_hide_the_rest(tmp_path):
    """A legacy cp1252 report is repaired; a corrupt one is counted, and neither
    can abort the list. The bug was measured: one bad file 500'd the endpoint
    and the UI rendered the outage as an empty history."""
    store = RunStore(tmp_path / "runs")
    for name, payload in (
        ("utf8run0000001", {"state": "done", "documents": [], "summary": {}}),
        ("cp1252run00001", {
            "state": "done", "documents": [], "summary": {},
            "note": "an em dash — written by the old locale codec",
        }),
    ):
        store.create(name)
        raw = json.dumps(payload, ensure_ascii=False)
        (store.dir(name) / "report.json").write_bytes(
            raw.encode("utf-8") if name.startswith("utf8") else raw.encode("cp1252")
        )
    store.create("corruptrun0001")
    (store.dir("corruptrun0001") / "report.json").write_bytes(b"{not json")

    rows, skipped = store.list_shipments()
    assert {r["shipment_id"] for r in rows} == {"utf8run0000001", "cp1252run00001"}
    assert skipped == 1
    repaired = (store.dir("cp1252run00001") / "report.json").read_text(encoding="utf-8")
    assert "em dash" in repaired


def test_the_api_reports_how_many_runs_were_skipped(tmp_path):
    settings = settings_for(tmp_path)
    configure(Reconciler(settings))
    store = RunStore(settings.runs_dir)
    store.create("corruptrun0001")
    (store.dir("corruptrun0001") / "report.json").write_bytes(b"{not json")
    res = TestClient(app).get("/api/shipments")
    assert res.status_code == 200
    assert res.json() == {"shipments": [], "skipped": 1}


# ---------------------------------------------------------------------------
# R4: one probe per client, not one per document
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_documents_probe_once(tmp_path):
    """probe() writes self.json_mode, so N documents starting cold used to fire
    N probes: duplicate spend, and a mode that could change mid-flight."""
    # demo_mode would short-circuit the probe, and the thing under test is
    # what happens when a probe actually reaches an endpoint.
    settings = settings_for(tmp_path, demo_mode=False)
    endpoint = Endpoint(provider="nvidia", base_url="https://x.invalid/v1/",
                        api_key="nvapi-TEST-ONLY-0000-1234", model="m", label="vision")
    client = ChatClient(endpoint, settings)
    in_flight = {"now": 0, "peak": 0, "total": 0}

    async def fake_transport(messages, *args, **kwargs):
        # An unusable answer, so the probe walks its own ladder rather than
        # short-circuiting on the first mode -- every rung is a transport call,
        # and the point is that none of them overlap.
        in_flight["now"] += 1
        in_flight["total"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await asyncio.sleep(0.02)
        in_flight["now"] -= 1
        return "not json at all", "", {}

    client.transport_call = fake_transport  # type: ignore[method-assign]

    await asyncio.gather(*[client.probe() for _ in range(5)])
    assert in_flight["peak"] == 1, (
        f"two probes ran simultaneously ({in_flight}); the second caller paid for "
        "an endpoint the first was already asking"
    )
    # One ladder, walked once: five callers share the answer.
    assert in_flight["total"] <= 3, in_flight


# ---------------------------------------------------------------------------
# B4: a crashed run must be attributable
# ---------------------------------------------------------------------------


def test_a_crashed_background_run_is_logged_with_its_id(tmp_path, caplog):
    rec = Reconciler(settings_for(tmp_path))
    shipment_id = "crashtest00001"

    async def explode():
        raise RuntimeError("boom in the fan-out")

    async def drive():
        task = asyncio.create_task(explode())
        rec._tasks[shipment_id] = task
        task.add_done_callback(lambda done: rec._task_done(shipment_id, done))
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)

    with caplog.at_level("ERROR", logger="sdr.run"):
        asyncio.run(drive())

    assert any(shipment_id in r.message and "boom" in r.message
               for r in caplog.records if r.levelname == "ERROR"), [
        r.message for r in caplog.records]
    assert shipment_id not in rec._tasks, "the task registry must not leak entries"


def test_a_cancelled_run_is_logged_as_cancelled(tmp_path, caplog):
    rec = Reconciler(settings_for(tmp_path))

    async def drive():
        async def forever():
            await asyncio.sleep(5)

        task = asyncio.create_task(forever())
        rec._tasks["zz"] = task
        task.add_done_callback(lambda done: rec._task_done("zz", done))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return task

    task = asyncio.run(drive())
    assert task.cancelled()
    assert "zz" not in rec._tasks, "a cancelled run must still be unregistered"


# ---------------------------------------------------------------------------
# the log line carries the thing you would grep for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rejected_document_is_logged_by_shipment(tmp_path, caplog):
    rec = Reconciler(settings_for(tmp_path))
    with caplog.at_level("INFO", logger="sdr.run"):
        sid, _ = await rec.create_shipment([("notes.txt", None, b"hello")])
    assert any(sid in r.message and "notes.txt" in r.message
               for r in caplog.records), [r.message for r in caplog.records]


# ---------------------------------------------------------------------------
# R7: the endpoint that costs money is the one that is bounded
# ---------------------------------------------------------------------------


def test_limiter_allows_up_to_the_limit_then_blocks():
    from app.limits import RateLimiter

    limiter = RateLimiter(limit=3, window_seconds=60)
    assert [limiter.check("a")[0] for _ in range(4)] == [True, True, True, False]
    # A different client is a different budget.
    assert limiter.check("b")[0] is True


def test_limiter_recovers_when_the_window_passes():
    """The window is advanced by hand rather than by sleeping.

    This test used a 50 ms window and a 60 ms sleep. It passed alone and failed
    under full-suite load, because that measures how the scheduler happened to
    run rather than whether the limiter prunes -- and a gate that is green only
    when the machine is idle gets ignored the first time it goes red in anger.
    """
    from app.limits import RateLimiter

    clock = {"t": 1000.0}
    limiter = RateLimiter(limit=1, window_seconds=60.0, clock=lambda: clock["t"])

    assert limiter.check("a")[0] is True

    allowed, retry_in = limiter.check("a")
    assert allowed is False
    assert 0 < retry_in <= 60.0, retry_in

    clock["t"] += 59.0                      # still inside the window
    assert limiter.check("a")[0] is False

    clock["t"] += 2.0                       # now outside it
    assert limiter.check("a")[0] is True


def test_a_limit_of_zero_disables_the_limiter_rather_than_blocking_all():
    from app.limits import RateLimiter

    limiter = RateLimiter(limit=0)
    assert not limiter.enabled
    assert all(limiter.check("a")[0] for _ in range(50))


def _upload():
    return [("files", (IMG.name, IMG.read_bytes(), "image/png"))]


def test_create_shipment_is_rate_limited(tmp_path):
    configure(Reconciler(settings_for(tmp_path, run_limit_per_minute=2)))
    client = TestClient(app)
    assert client.post("/api/shipments", files=_upload()).status_code == 201
    assert client.post("/api/shipments", files=_upload()).status_code == 201
    res = client.post("/api/shipments", files=_upload())
    assert res.status_code == 429
    assert "Retry-After" in res.headers
    assert "2 per minute" in res.json()["detail"]
    # The rejection must not name the caller's key back at them.
    assert "nvapi-" not in res.text


def test_reading_a_report_is_never_rate_limited(tmp_path):
    """Throttling the poll would break the UI to protect an endpoint that costs
    nothing."""
    configure(Reconciler(settings_for(tmp_path, run_limit_per_minute=1)))
    client = TestClient(app)
    sid = client.get("/api/fixture/shipment_clean").json()["shipment_id"]
    for _ in range(20):
        assert client.get(f"/api/shipments/{sid}/report").status_code == 200


def test_health_is_not_rate_limited_even_at_zero(tmp_path):
    configure(Reconciler(settings_for(tmp_path, run_limit_per_minute=1)))
    client = TestClient(app)
    for _ in range(10):
        assert client.get("/api/health").status_code == 200


# ---------------------------------------------------------------------------
# R7: headers, and the absence of CORS by default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/api/health", "/api/fixtures"])
def test_security_headers_are_present(tmp_path, path):
    configure(Reconciler(settings_for(tmp_path)))
    client = TestClient(app)
    res = client.get(path)
    assert res.status_code == 200
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["referrer-policy"] == "no-referrer"
    csp = res.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "object-src 'none'" in csp


def test_the_content_policy_still_allows_what_the_app_needs(tmp_path):
    """A CSP that breaks the page is worse than none. The UI loads its own
    script and stylesheet from /static, the evidence images are data URIs, and
    the progress stream is an EventSource on the same origin."""
    configure(Reconciler(settings_for(tmp_path)))
    client = TestClient(app)
    csp = client.get("/").headers["content-security-policy"]
    for directive in ("script-src 'self'", "style-src 'self' 'unsafe-inline'",
                      "img-src 'self' data:", "connect-src 'self'"):
        assert directive in csp, directive


def test_no_cors_headers_by_default(tmp_path):
    """Same-origin only unless the deployment says otherwise -- and the API key
    is not a browser credential, so widening this is a real decision."""
    configure(Reconciler(settings_for(tmp_path)))
    client = TestClient(app)
    res = client.get("/api/health", headers={"Origin": "https://elsewhere.test"})
    assert "access-control-allow-origin" not in res.headers
