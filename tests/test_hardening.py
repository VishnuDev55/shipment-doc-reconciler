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
import re
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
# R14: the same ceiling, applied to what a PDF *declares* before it is drawn
# ---------------------------------------------------------------------------


def _pdf(page_points: tuple[float, float], pages: int = 1) -> bytes:
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=page_points)
    for _ in range(pages):
        c.drawString(40, max(page_points[1] - 40, 10), "page")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_a_page_that_declares_a_ludicrous_size_is_never_drawn_at_full_scale(monkeypatch):
    """Break caught: a sub-MiB PDF may declare a 14400x14400 pt page, and
    `rasterize_pdf` handed every page to pypdfium2 at a fixed scale of 2.0 --
    28800x28800 px, ~830 MP, ~3 GB of bitmap per page. The ceiling that refuses
    such an image never saw it, because it guards the Pillow path.

    Asserted on the scale actually passed to `render`, because the page that
    comes back is resized to the long edge either way: the output is small with
    or without the fix, and only the allocation is the vulnerability.
    """
    raw = _pdf((14400, 14400))
    drawn: list[tuple[float, float, float]] = []
    real_document = ingest_module.pdfium.PdfDocument

    def spy(raw):
        doc = real_document(raw)

        class _Page:
            def __init__(self, page):
                self._page = page

            def get_size(self):
                return self._page.get_size()

            def render(self, **kw):
                w, h = self._page.get_size()
                drawn.append((w, h, float(kw.get("scale", 1.0))))
                return self._page.render(**kw)

        class _Doc:
            def __len__(self):
                return len(doc)

            def __getitem__(self, i):
                return _Page(doc[i])

            def close(self):
                doc.close()

        return _Doc()

    monkeypatch.setattr(ingest_module.pdfium, "PdfDocument", spy)
    ingest_module.rasterize_pdf(raw, max_pages=1)

    assert drawn, "the page was never drawn, so nothing was measured"
    for w, h, scale in drawn:
        assert (w * scale) * (h * scale) <= ingest_module.MAX_IMAGE_PIXELS, (
            f"a {w:.0f}x{h:.0f} pt page drawn at {scale} asks for "
            f"{(w * scale) * (h * scale) / 1e6:.0f} MP"
        )


def test_the_declared_size_decides_the_scale_before_the_page_is_drawn():
    """The clamp is derived from the page's own numbers, so a big page is drawn
    smaller rather than refused: every page is resized to the long edge moments
    later anyway, and a reader of a 14400 pt poster still gets their document."""
    scale = ingest_module._render_scale(14400, 14400, scale=2.0, long_edge=1600)
    assert (14400 * scale) * (14400 * scale) <= ingest_module.MAX_IMAGE_PIXELS
    assert 14400 * scale <= 1600 * 2, "no point drawing larger than twice the output"

    # An ordinary A4 page is untouched by all of this.
    assert ingest_module._render_scale(595, 842, scale=2.0, long_edge=1600) == 2.0


def test_a_pdf_that_takes_too_long_to_draw_is_stopped(monkeypatch):
    """Break caught: an unbounded render loop turns one document into the whole
    instance's CPU, and the upload timeout does not cover a sync threadpool call
    that is already running.

    The clock is moved rather than the deadline set to zero: a Windows
    monotonic clock can report the same tick twice, and a check that passes at
    0.0 s elapsed would let this test pass by accident.
    """
    raw = _pdf((2000, 2000), pages=4)

    class _Clock:
        """Replaces `ingest.time` (the name in that module, not the stdlib
        module) so the moved clock cannot affect anything else in the run."""

        def __init__(self, ticks):
            self._ticks = iter(ticks)

        def monotonic(self):
            return next(self._ticks)

    monkeypatch.setattr(ingest_module, "time",
                        _Clock([0.0, 0.0, 1.0, 500.0, 500.0, 500.0]))

    with pytest.raises(ValueError, match="too long"):
        ingest_module.rasterize_pdf(raw, max_pages=6, deadline_seconds=30.0)



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


def test_a_retry_is_refused_when_the_day_has_no_money_left(tmp_path):
    """Break caught: /retry reaches a live vision call but was never gated, so a
    shipment refused at the door could still be paid for, one document at a time,
    through the back of it."""
    from app import budget

    settings = settings_for(tmp_path, demo_mode=False, usd_daily_cap=0.05)
    configure(Reconciler(settings))
    client = TestClient(app)
    sid = client.get("/api/fixture/shipment_clean").json()["shipment_id"]
    doc_id = client.get(f"/api/shipments/{sid}/report").json()["documents"][0]["id"]
    before = _doc_status(client, sid, doc_id)

    # Spend the day through the real ledger path.
    budget.record_call(settings, "nebius", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                       {"prompt": 1_000_000, "completion": 0})

    res = client.post(f"/api/shipments/{sid}/retry", json={"document_id": doc_id})
    assert res.status_code == 429, res.text
    assert "Retry-After" in res.headers
    # Refusal must happen before the row is touched: a document marked "running"
    # by a retry that never ran is a lie the reviewer polls for.
    assert _doc_status(client, sid, doc_id) == before


def _doc_status(client, sid: str, doc_id: str) -> str:
    body = client.get(f"/api/shipments/{sid}/report").json()
    return next(d["status"] for d in body["documents"] if d["id"] == doc_id)


def test_a_retry_holds_its_estimate_against_the_cap(tmp_path):
    """Break caught: two retries arriving together each read a ledger that has
    not been written yet, both pass, and both bill."""
    from app import budget

    settings = settings_for(tmp_path, demo_mode=False)
    # A day that fits exactly one document's worst case, derived from the
    # estimate the gate itself uses: what is under test is whether a second run
    # can see the first's intention, not what a page costs.
    one = budget.estimate_run_usd(settings, 1, provider="nvidia")
    settings = settings_for(tmp_path, demo_mode=False, usd_daily_cap=one * 1.5)
    rec = Reconciler(settings)
    configure(rec)
    client = TestClient(app)
    sid = client.get("/api/fixture/shipment_clean").json()["shipment_id"]
    ids = [d["id"] for d in client.get(f"/api/shipments/{sid}/report").json()["documents"]]

    # One document's worth of reservation, then a second that cannot fit.
    rec._reserve_spend(sid, 1, key=f"{sid}:{ids[0]}")
    assert budget.pending_total() > 0.0
    with pytest.raises(budget.SpendCapReached):
        rec._reserve_spend(sid, 1, key=f"{sid}:{ids[1]}")


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


# ---------------------------------------------------------------------------
# R16: the standalone report is the one artifact with no CSP
# ---------------------------------------------------------------------------


def _hostile_report(value: str):
    """A one-conflict report carrying attacker-chosen text in the reason, the
    document label, the raw value and the canonical value.

    Built through the real models rather than a dict, because the shapes the
    template reads are the shapes that have to be escaped.
    """
    from app.models import Comparison, ComparisonValue, Report, RunInfo

    report = Report(shipment_id="s", created_at="now", updated_at="now")
    report.run = RunInfo(prompt_version="t", normalizer_version="t",
                         comparator_version="t", model="m", vision_provider="nvidia",
                         summary_model="s", summary_provider="nebius",
                         demo_mode=True, json_schema_mode="strict",
                         result_source="authored", live_calls=0, usd_per_mtok=0.0)
    report.comparisons = [Comparison(
        field="hs_code", label="HS code", kind="hs_code", status="conflict",
        reason=value,
        values=[ComparisonValue(document_id="d", document_label=value,
                                doc_type="other", page_index=1, raw_value=value,
                                canonical=value, compare_key=value, confidence=0.9,
                                warnings=[])],
    )]
    return report


@pytest.mark.parametrize("value", [
    "<script>alert(1)</script>",
    "</td></tr><script>document.cookie</script>",
    '"><img src=x onerror=alert(1)>',
    "]]>&<xmp>-",
])
def test_document_text_cannot_mark_up_the_standalone_report(value):
    """Break caught: docs/SECURITY.md cited `tests/test_lineitems.py` as the pin
    for this, and no such assertion existed anywhere.

    The standalone HTML leaves the app as a file -- mailed, saved to a desktop,
    opened from a USB stick -- so unlike the served pages it inherits no
    Content-Security-Policy header. Escaping is the only thing between a
    document's text and execution, which is precisely why it needs a test rather
    than a sentence in a doc.
    """
    import html

    from app.export import standalone_html

    page = standalone_html(_hostile_report(value))
    # quote=True, because these values land inside attributes as well as between
    # tags -- a check that only escaped `<` and `>` would pass a payload built out
    # of a closing quote.
    assert html.escape(value) in page, "the value must survive as text"
    assert "<script" not in page.lower(), "and must not arrive as an element"
    assert "<img src=x" not in page, "no element may be assembled from document text"


# ---------------------------------------------------------------------------
# R17: a filename is text, and text does not get to contain line breaks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hostile", [
    "invoice\r\nApproved by: accounts.png",
    "invoice\n\nURGENT: pay to IBAN X.png",
    "invoice\tTOTAL: 0.png",
    "invoice\x00\x1b[2J.png",
])
def test_a_filename_cannot_put_control_characters_into_a_label(hostile):
    """Break caught: `short_label` copied the filename into the label, and the
    label is rendered into the draft email and the report. A carriage return in a
    name is enough to start a new line of the letter or forge a header-shaped
    line in a text client, and a tab or an escape sequence can reorder what a
    terminal shows about a shipment.
    """
    from app.orchestrator import short_label

    label = short_label(hostile, 1)
    assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in label), repr(label)
    assert label.strip(), "the label must not be emptied by the cleaning"


def test_an_ordinary_filename_keeps_its_unicode():
    """The cleaning must not be a sledgehammer: accented names and CJK are data
    a reviewer needs, and only control characters are being removed."""
    from app.orchestrator import short_label

    assert "Gutschrift" in short_label("Gutschrift Müller.pdf", 1)
    assert "発票" in short_label("発票.pdf", 1)


# ---------------------------------------------------------------------------
# R18: a spreadsheet is a program, and a cell can be a program too
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    '=HYPERLINK("http://evil.test","click me")',
    "+1+cmd|' /C calc'!A0",
    "-2+3",
    "@SUM(1+1)*cmd|' /C calc'!A0",
    "\t=1+1",
    "\r=1+1",
])
def test_a_document_value_cannot_start_a_formula_in_the_csv_export(payload):
    """Break caught: report.csv is written from document text with no cell
    treatment, so a field whose value begins `=`, `+`, `-`, `@`, tab or CR opens
    in Excel, LibreOffice or Sheets as a formula. The attacker does not need the
    reviewer to click anything -- the payload runs when the file is opened, and
    the reviewer was sent it precisely because they are the person with access.
    """
    import csv as csv_module
    import io

    from app.export import report_csv

    rows = list(csv_module.reader(io.StringIO(report_csv(_hostile_report(payload)))))
    header, first = rows[0], rows[1]
    value = first[header.index("raw_value")]

    assert not value.startswith(("=", "+", "-", "@", "\t", "\r")), value
    assert payload in value, "the value must still be readable, not dropped"


def test_a_negative_amount_is_still_a_number_not_a_formula():
    """The blunt fix -- prefix every `-` -- would put a quote in front of every
    negative amount in every export. Only a `-` that does not parse as a number
    is a formula."""
    import csv as csv_module
    import io

    from app.export import report_csv

    rows = list(csv_module.reader(io.StringIO(report_csv(_hostile_report("-1,234.00")))))
    header, first = rows[0], rows[1]
    value = first[header.index("raw_value")]
    assert value == "-1,234.00"


# ---------------------------------------------------------------------------
# R19: who the limiter thinks you are is not your decision
# ---------------------------------------------------------------------------


def _request_with(headers: dict[str, str]):
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "POST", "path": "/api/shipments",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("203.0.113.9", 51234),
    })


def test_a_caller_cannot_pick_their_own_rate_limit_bucket(tmp_path):
    """Break caught: `_client_key` always believed the first hop of
    X-Forwarded-For, so a caller could send a different value on every request
    and never share a bucket with themselves. `RUN_LIMIT_PER_MINUTE` was
    therefore a limit on well-behaved clients only.

    Believing the header is only correct when something you control sets it, so
    it is now off unless the deployment says a trusted proxy is in front.
    """
    from app.ratelimit import _client_key

    configure(Reconciler(settings_for(tmp_path)))
    spoofed = _request_with({"x-forwarded-for": "198.51.100.7, 10.0.0.1"})
    assert _client_key(spoofed) == "ip:203.0.113.9", (
        "the peer address is the only identity a caller cannot choose"
    )


def test_a_trusted_proxy_still_gets_to_say_who_the_client_is(tmp_path):
    """The other half: behind a proxy the peer address is the proxy's, so every
    visitor would share one bucket. Naming the proxy is how a deployment opts
    back in to the forwarded identity."""
    from app.ratelimit import _client_key

    configure(Reconciler(settings_for(tmp_path, trusted_proxy=True)))
    forwarded = _request_with({"x-forwarded-for": "198.51.100.7, 10.0.0.1"})
    assert _client_key(forwarded) == "ip:198.51.100.7"


def test_a_supplied_key_still_wins_over_the_address(tmp_path):
    """A key is a better identity than an address when one is offered, and that
    does not change: a shared key is one budget by design."""
    from app.ratelimit import _client_key

    configure(Reconciler(settings_for(tmp_path)))
    keyed = _request_with({"x-api-key": "k-live-000000000042",
                           "x-forwarded-for": "198.51.100.7"})
    assert _client_key(keyed) == "key:000000000042"


# ---------------------------------------------------------------------------
# R20: the export is the artifact that leaves the building, so it carries its own policy
# ---------------------------------------------------------------------------


def test_the_standalone_report_ships_a_content_security_policy():
    """Break caught: the served pages get a CSP header from `security_headers`,
    and the standalone report gets nothing -- because it is not served, it is
    saved, mailed and opened from a desktop. It therefore has no origin, no
    header and no server, which makes an inline policy the only place a rule can
    live. The escaping is the actual control; this is the second lock, for the
    markup that a future editor adds without escaping.
    """
    from app.export import standalone_html

    page = standalone_html(_hostile_report("plain"))

    match = re.search(
        r'<meta http-equiv="Content-Security-Policy" content="([^"]+)"', page, re.I)
    assert match, "the standalone export must carry a CSP of its own"
    policy = match.group(1)

    assert "default-src 'none'" in policy, policy
    assert "script-src" not in policy, (
        "no directive may re-permit scripts; default-src 'none' is the whole point"
    )
    assert "style-src 'unsafe-inline'" in policy, "the layout is inline <style>"
    assert "img-src data:" in policy, "evidence pages are embedded as data: URIs"

    head = page.split("</head>", 1)[0]
    assert "Content-Security-Policy" in head, "a meta CSP outside <head> is ignored"
