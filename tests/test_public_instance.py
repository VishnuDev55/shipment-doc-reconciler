"""What the public instance refuses, and what it still serves.

These are the promises a deployed URL makes: a hard daily spend ceiling, sample
shipments that never cost anything, upload caps that say what they are, and a
health endpoint a load balancer can poll while the app is in live mode.

Every test here runs with `DEMO_MODE=false` unless it says otherwise, because
the behaviour being pinned only exists in live mode -- and none of it may change
what a local `DEMO_MODE=true` run does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import budget
from app.config import Settings
from app.main import app, configure
from app.orchestrator import Reconciler

REPO = Path(__file__).resolve().parents[1]
INDEX = json.loads((REPO / "fixtures" / "index.json").read_text())
NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


def live_settings(tmp_path: Path, **overrides) -> Settings:
    """A deployed instance: live mode, no GUI config, no keys in the test."""
    base = dict(
        _env_file=None,
        demo_mode=False,
        vision_provider="nvidia",
        nvidia_api_key="",
        nebius_api_key="",
        runs_dir=tmp_path / "runs",
        cache_dir=REPO / "fixtures" / "responses",
        gui_config_enabled=False,
        model_discovery=False,
        run_limit_per_minute=0,
    )
    base.update(overrides)
    return Settings(**base)


def upload_files(name: str = "shipment_clean") -> list[tuple[str, tuple[str, bytes, str]]]:
    out = []
    for doc in INDEX["shipments"][name]["documents"]:
        for fname in doc["files"]:
            out.append(("files", (fname,
                                  (REPO / "fixtures" / "images" / fname).read_bytes(),
                                  "image/png")))
    return out


def spend(settings: Settings, usd_worth_of_tokens: int = 1_000_000) -> None:
    """Put real dollars in today's ledger through the same call path a live run
    uses, so the cap is tested against the ledger rather than a stub of it."""
    budget.record_call(settings, "nebius", NANO,
                       {"prompt": usd_worth_of_tokens, "completion": 0})


@pytest.fixture()
def make_client(tmp_path):
    clients = []

    def _make(**overrides):
        settings = live_settings(tmp_path, **overrides)
        configure(Reconciler(settings))
        client = TestClient(app)
        clients.append(client)
        return client, settings

    yield _make
    for client in clients:
        client.close()


# ---------------------------------------------------------------------------
# the daily cap
# ---------------------------------------------------------------------------


def test_an_over_cap_upload_is_refused_with_the_reason(make_client):
    """Break caught: the cap is computed and never enforced, so the deployment
    spends past its ceiling while reporting that it has one."""
    client, settings = make_client(usd_daily_cap=0.01)
    spend(settings)  # one million prompt tokens at $0.06/M = $0.06

    res = client.post("/api/shipments", files=upload_files())

    assert res.status_code == 429, res.text
    detail = res.json()["detail"]
    # Case-folded: the wording may be re-styled; the three facts may not vanish.
    assert "daily spend cap" in detail.lower()
    assert "$0.01" in detail, "the refusal must name the cap it hit"
    assert "sample shipments" in detail.lower(), (
        "a refused visitor needs to know what still works: "
        f"got {detail!r}"
    )
    assert int(res.headers.get("Retry-After", "0")) > 0


def test_an_under_cap_upload_is_not_refused_for_money(make_client):
    """Break caught: the check is inverted or off by a factor of a million, and a
    deployment with money left in the day refuses every upload."""
    client, settings = make_client(usd_daily_cap=2.00)
    spend(settings, usd_worth_of_tokens=1_000_000)  # $0.06 of a $2.00 day

    res = client.post("/api/shipments", files=upload_files())

    # It fails later, on the missing key -- which is the point: the cap did not
    # stop it, the absent key did.
    assert res.status_code != 429
    assert "daily spend cap" not in res.text


def test_a_local_demo_run_is_never_blocked_by_the_cap(make_client):
    """Break caught: DEMO_MODE picks up the cap check and a laptop with an old
    ledger can no longer run the bundled demo."""
    client, settings = make_client(demo_mode=True, usd_daily_cap=0.01)
    spend(settings, usd_worth_of_tokens=10_000_000)

    res = client.get("/api/fixture/shipment_clean")

    assert res.status_code == 201, res.text


# ---------------------------------------------------------------------------
# sample shipments are always replays
# ---------------------------------------------------------------------------


def test_a_sample_shipment_replays_from_cache_even_in_live_mode(make_client):
    """Break caught: on a live deployment the sample buttons make real API calls,
    so a judge clicking through the three examples spends the operator's money
    and can exhaust the day's cap before uploading anything of their own."""
    client, _settings = make_client()

    res = client.get("/api/fixture/shipment_clean")
    assert res.status_code == 201, res.text
    body = res.json()
    report = client.get(f"/api/shipments/{body['shipment_id']}/report").json()

    assert report["run"]["demo_mode"] is True, (
        "a sample must be replayed on a forced-demo reconciler, not just labelled"
    )
    assert report["run"]["live_calls"] == 0
    assert report["run"]["result_source"] == "live", (
        "the cached values are real model output, so the source is 'live' -- "
        "replayed proves modality, not authorship"
    )
    assert report["run"]["sample"] is True


def test_samples_keep_working_after_the_cap_is_spent(make_client):
    """Break caught: the cap blocks the sample buttons too, so a visitor who
    arrives at the end of a busy day sees nothing at all on the demo."""
    client, settings = make_client(usd_daily_cap=0.01)
    spend(settings)

    refused = client.post("/api/shipments", files=upload_files())
    assert refused.status_code == 429

    sample = client.get("/api/fixture/shipment_conflict")
    assert sample.status_code == 201, sample.text
    # "Done", not merely "accepted": a live-mode sample that fails every document
    # and still returns 201 would pass a status-code-only assertion.
    assert sample.json()["state"] == "done", sample.json()
    report = client.get(f"/api/shipments/{sample.json()['shipment_id']}/report").json()
    assert report["run"]["live_calls"] == 0


def test_an_uploaded_run_is_not_labelled_a_sample(make_client):
    """Break caught: the sample stamp is applied to every run, so a visitor's own
    document is described as a bundled example."""
    client, _settings = make_client(demo_mode=True)
    res = client.post("/api/shipments", files=upload_files())
    assert res.status_code == 201, res.text
    report = client.get(f"/api/shipments/{res.json()['shipment_id']}/report").json()
    assert report["run"]["sample"] is False


def test_the_exported_html_says_the_run_was_a_sample(make_client):
    """Break caught: the label lives only in the browser, so the HTML and PDF a
    visitor downloads present a bundled example as their own shipment."""
    import io
    import zipfile

    client, _settings = make_client()
    sample = client.get("/api/fixture/shipment_clean").json()
    blob = client.get(f"/api/shipments/{sample['shipment_id']}/export").content

    html = zipfile.ZipFile(io.BytesIO(blob)).read("report.html").decode("utf-8")
    assert "sample: cached model output" in html


# ---------------------------------------------------------------------------
# upload caps, and the pages a cap does not read
# ---------------------------------------------------------------------------


def five_page_pdf() -> bytes:
    """A real 5-page PDF, built the same way the report renderer builds one."""
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    for page in range(1, 6):
        pdf.drawString(72, 720, f"COMMERCIAL INVOICE — page {page}")
        pdf.drawString(72, 700, "TOTAL GROSS WT 12,450.00 KGS")
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


def test_a_document_over_the_instance_limit_is_refused_by_name(make_client):
    """Break caught: the refusal names no limit, so a visitor cannot tell whether
    they sent too many files or the app broke."""
    client, _settings = make_client(demo_mode=True, max_documents=3)
    image = (REPO / "fixtures" / "images" / "invoice_clean.png").read_bytes()
    files = [("files", (f"invoice_copy_{i}.png", image, "image/png")) for i in range(4)]

    res = client.post("/api/shipments", files=files)

    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert "3" in detail, f"the refusal must name the limit: {detail!r}"
    assert "4" in detail, f"the refusal should say what arrived: {detail!r}"


def test_a_document_with_too_many_pages_is_refused_when_the_instance_says_so(make_client):
    """Break caught: the page cap silently truncates, so a five-page invoice is
    read as four and the report never mentions the fifth -- where the total was."""
    client, _settings = make_client(demo_mode=True, max_pages_per_document=4,
                                    reject_oversize_pages=True)
    files = [("files", ("invoice_5_pages.pdf", five_page_pdf(), "application/pdf"))]

    res = client.post("/api/shipments", files=files)

    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert "invoice_5_pages.pdf" in detail
    assert "5" in detail and "4" in detail, detail


def test_a_truncated_document_says_which_pages_it_did_not_read(make_client):
    """Break caught: with the hard refusal off, the read is still truncated --
    and a report that does not say so is a verdict about pages nobody read."""
    client, _settings = make_client(demo_mode=True, max_pages_per_document=4,
                                    reject_oversize_pages=False)
    files = [("files", ("invoice_5_pages.pdf", five_page_pdf(), "application/pdf"))]

    res = client.post("/api/shipments", files=files)
    assert res.status_code == 201, res.text
    report = client.get(f"/api/shipments/{res.json()['shipment_id']}/report").json()
    doc = report["documents"][0]

    assert len(doc["pages"]) == 4, "the cap still applies to the read"
    notes = " ".join(doc["notes"])
    assert "1 page" in notes and "4-page limit" in notes, (
        f"the dropped page is undisclosed: {doc['notes']!r}"
    )


# ---------------------------------------------------------------------------
# the load balancer, and what the page says about retention
# ---------------------------------------------------------------------------


def test_health_passes_in_live_mode_without_keys(make_client):
    """Break caught: health needs a key, or probes a provider, and Render marks a
    perfectly healthy live instance as down -- or restarts it in a loop."""
    client, _settings = make_client()

    res = client.get("/api/health")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok"
    assert body["mode"] == "live"
    assert body["demo_mode"] is False
    # Nothing was reached: the socket guard would have refused it, and the body
    # says so per role rather than pretending either host answered.
    assert body["vision"]["key_present"] is False
    assert body["summary"]["key_present"] is False


def test_health_reports_how_long_uploaded_runs_live(make_client):
    """Break caught: the page promises a retention the server does not apply, or
    applies one the page never mentions. They read from the same number."""
    client, _settings = make_client(run_ttl_hours=24)
    assert client.get("/api/health").json()["run_ttl_hours"] == 24

    client_zero, _ = make_client()
    assert client_zero.get("/api/health").json()["run_ttl_hours"] == 0


def test_bundled_samples_are_not_subject_to_the_upload_document_cap(make_client):
    """Break caught: MAX_DOCUMENTS=3 (the public upload cap) makes every sample
    500, because each ships four documents and the sample route feeds them
    through the same create_shipment that enforces the visitor cap. A sample is
    served, not uploaded, so an operator lowering the upload limit must not break
    the built-in demo."""
    client, _settings = make_client(max_documents=3)

    for name in ("clean", "conflict", "messy"):
        res = client.get(f"/api/fixture/shipment_{name}")
        assert res.status_code == 201, f"{name}: {res.text}"
        body = res.json()
        assert body["state"] == "done", f"{name} did not finish: {body}"
        assert body["sample"] is True
        report = client.get(f"/api/shipments/{body['shipment_id']}/report").json()
        assert report["run"]["live_calls"] == 0, f"{name} spent money"


def test_a_sample_run_is_written_under_runs_dir_even_when_it_does_not_exist(tmp_path):
    """Break caught: the store assumes RUNS_DIR exists, so a freshly created
    Render volume (/tmp/shipment-runs) 500s the first sample until someone
    mkdirs it by hand."""
    from app.main import configure
    from app.orchestrator import Reconciler

    target = tmp_path / "not_yet_a_dir" / "shipment-runs"  # deliberately uncreated
    settings = live_settings(tmp_path, runs_dir=target)
    configure(Reconciler(settings))
    client = TestClient(app)
    try:
        res = client.get("/api/fixture/shipment_clean")
        assert res.status_code == 201, res.text
        sid = res.json()["shipment_id"]
        assert (target / sid).is_dir(), "the run directory was not created on demand"
    finally:
        client.close()



# ---------------------------------------------------------------------------
# the container must carry what the samples read
# ---------------------------------------------------------------------------


def test_the_docker_image_carries_the_fixture_paths_the_samples_read():
    """Break caught: a path in app/main.py moves out of `fixtures/` (or the
    Dockerfile stops copying `fixtures`), and the sample endpoint 500s only
    inside the image where the local tree has the file.

    This is a static check by necessity -- the test suite has no Docker daemon --
    but it checks the same relationship at the boundary: every directory the
    fixture route reads must live under a top-level directory the Dockerfile
    copies.
    """
    from app import main as main_module

    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    copied = [line.split()[1] for line in dockerfile.splitlines()
              if line.startswith("COPY ") and not line.startswith("COPY requirements")]
    assert "fixtures" in copied, "the Dockerfile must copy the fixtures the samples read"

    for path in (main_module.FIXTURE_INDEX, main_module.FIXTURE_IMAGES,
                 main_module.FIXTURE_TEXT):
        rel = path.relative_to(REPO).parts[0]
        assert rel in copied, f"{path} is read at runtime but its tree is not COPY'd"
