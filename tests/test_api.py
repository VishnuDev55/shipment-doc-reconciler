"""HTTP tests against the real FastAPI app in DEMO_MODE. Zero API calls."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.export import build_zip
from app.fields import FIELD_ORDER
from app.main import app, configure, reconciler
from app.models import Report
from app.orchestrator import Reconciler

REPO = Path(__file__).resolve().parents[1]
INDEX = json.loads((REPO / "fixtures" / "index.json").read_text())


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        demo_mode=True,
        nebius_api_key="",
        runs_dir=tmp_path / "runs",
        cache_dir=REPO / "fixtures" / "responses",
    )
    configure(Reconciler(settings))
    with TestClient(app) as c:
        yield c


def fixture_files(name: str) -> list[tuple[str, bytes, str]]:
    out = []
    for doc in INDEX["shipments"][name]["documents"]:
        for fname in doc["files"]:
            out.append((fname, (REPO / "fixtures" / "images" / fname).read_bytes(), doc["doc_type"]))
    return out


# ---------------------------------------------------------------------------


def test_health_reports_demo_mode(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["demo_mode"] is True
    assert body["api_key_present"] is False
    assert body["base_url"] == "https://api.tokenfactory.nebius.com/v1/"


def test_index_serves_the_ui(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Shipment Doc Reconciler" in res.text
    assert "/static/app.js" in res.text


def test_static_assets_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_fixtures_endpoint_lists_three_shipments(client):
    body = client.get("/api/fixtures").json()
    assert set(body["shipments"]) == {
        "shipment_clean",
        "shipment_conflict",
        "shipment_messy",
    }


def test_upload_then_poll_report(client):
    files = fixture_files("shipment_conflict")
    payload = [("files", (f, io.BytesIO(b), "image/png")) for f, b, _ in files]
    types = json.dumps([d for _, _, d in files])

    res = client.post("/api/shipments", data={"doc_types": types}, files=payload)
    assert res.status_code == 201, res.text
    sid = res.json()["shipment_id"]
    assert len(sid) >= 12

    report = client.get(f"/api/shipments/{sid}/report").json()
    assert report["state"] in ("running", "done", "partial")
    # TestClient runs the task synchronously enough that a second poll settles.
    for _ in range(20):
        report = client.get(f"/api/shipments/{sid}/report").json()
        if report["state"] != "running":
            break
    assert report["state"] == "done"
    assert report["summary"]["verdict"] == "conflicts"
    # One comparison per contracted field, whatever that number is today.
    assert len(report["comparisons"]) == len(FIELD_ORDER)


def test_fixture_route_matches_upload_route(client):
    """The demo shortcut must produce the same verdicts as a real upload."""
    sid_fixture = client.get("/api/fixture/shipment_conflict").json()["shipment_id"]
    report_fixture = client.get(f"/api/shipments/{sid_fixture}/report").json()

    files = fixture_files("shipment_conflict")
    sid_upload = client.post(
        "/api/shipments",
        data={"doc_types": json.dumps([d for _, _, d in files])},
        files=[("files", (f, io.BytesIO(b), "image/png")) for f, b, _ in files],
    ).json()["shipment_id"]
    report_upload = client.get(f"/api/shipments/{sid_upload}/report").json()

    assert {c["field"]: c["status"] for c in report_fixture["comparisons"]} == {
        c["field"]: c["status"] for c in report_upload["comparisons"]
    }


def test_unknown_shipment_is_404(client):
    assert client.get("/api/shipments/doesnotexist/report").status_code == 404


def test_unknown_fixture_is_404(client):
    assert client.get("/api/fixture/not_a_shipment").status_code == 404


def test_too_many_files_rejected(client):
    one = fixture_files("shipment_clean")[0]
    payload = [("files", (one[0], io.BytesIO(one[1]), "image/png")) for _ in range(6)]
    res = client.post("/api/shipments", data={"doc_types": "[]"}, files=payload)
    assert res.status_code == 400
    assert "at most 5" in res.json()["detail"]


def test_zip_export_contains_all_four_artifacts(client):
    sid = client.get("/api/fixture/shipment_conflict").json()["shipment_id"]
    res = client.get(f"/api/shipments/{sid}/export")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert "attachment" in res.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(res.content))
    names = set(zf.namelist())
    assert {"report.json", "report.csv", "report.html", "README.txt"} <= names

    embedded = json.loads(zf.read("report.json"))
    assert embedded["shipment_id"] == sid
    assert embedded["summary"]["conflict"] >= 2

    csv_text = zf.read("report.csv").decode()
    assert "gross_weight" in csv_text
    assert len(csv_text.strip().splitlines()) > 6

    html_text = zf.read("report.html").decode()
    assert "ISO 6346" in html_text
    assert "data:image/jpeg;base64," in html_text, "evidence images must be embedded"
    assert "cdn." not in html_text and "http://" not in html_text.split("DOCTYPE")[1][:4000]

    assert any(n.startswith("pages/") for n in names), "page evidence must ship in the zip"


def test_the_export_manifest_survives_a_boring_report(client):
    """README.txt used to disappear on the majority of runs.

    Its content was one concatenation with conditionals inside it, which parsed as
    `(everything + excluded) if unverified else ""` -- so a report with no excluded
    fields got a manifest that began at the blank line under the header. The
    archive that describes an export stopped describing it, and nothing noticed
    because the file was still there.
    """
    report = Report(shipment_id="plain", created_at="t", updated_at="t")
    blob = build_zip(report, None)
    readme = zipfile.ZipFile(io.BytesIO(blob)).read("README.txt").decode()

    assert readme.startswith("Shipment Doc Reconciler export")
    for line in ("shipment_id :", "verdict     :", "status      :", "demo_mode   :",
                 "drafts      :"):
        assert line in readme, line
    assert "report.json" in readme and "pages/" in readme
    assert "excluded    :" not in readme, "nothing was excluded on this report"
    assert "corrections/" not in readme, "this report has no drafts to list"


def test_page_images_are_served(client):
    report = client.get("/api/fixture/shipment_clean").json()
    sid = report["shipment_id"]
    body = client.get(f"/api/shipments/{sid}/report").json()
    image_path = body["documents"][0]["pages"][0]["image_path"]
    res = client.get(f"/page/{sid}/{image_path}")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert len(res.content) > 1000


def test_page_route_blocks_traversal(client):
    sid = client.get("/api/fixture/shipment_clean").json()["shipment_id"]
    assert client.get(f"/page/{sid}/../secret.txt").status_code == 404
    assert client.get(f"/page/{sid}/..%2F..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
    # A sibling run directory must not be readable through this route either.
    assert client.get(f"/page/{sid}/../../{sid}/report.json").status_code in (400, 404)


def test_page_route_blocks_traversal_through_the_id_segment(tmp_path, client):
    """The traversal above only probes `path`. The id was the wider hole.

    `base` was computed from the unvalidated id, so an id of ".." put the base
    one level above runs/ and a file living there satisfied "am I under base".
    The existing test asked for a `secret.txt` that was never written, so its
    404 proved nothing; this one writes it and must still be refused.

    Asserted against the handler because the HTTP layer normalises dot segments
    out of a URL before it can reach the route.
    """
    from fastapi import HTTPException

    from app.main import serve_page

    (tmp_path / "secret.txt").write_text("would have been served before the fix")
    assert (tmp_path / "runs").is_dir()

    with pytest.raises(HTTPException) as exc:
        serve_page("..", "secret.txt")
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException):
        serve_page("../../etc", "passwd")

    # An unknown-but-well-formed id must 404 too: no run, no file reads at all.
    with pytest.raises(HTTPException) as exc:
        serve_page("zzzzzzzzzzzzzzzzzz", "0.jpg")
    assert exc.value.status_code == 404

    assert client.get("/page/%2e%2e/secret.txt").status_code in (400, 404)


@pytest.mark.parametrize("bad_id", ["a.b", "a b", "x" * 25, "sid.txt"])
def test_no_route_answers_a_malformed_id_with_a_500(client, bad_id):
    """dir() raising ValueError must never reach a browser as a 500.

    Validating the id at the chokepoint is the fix, but 11 callers sit behind
    it, and a route that forgets to gate would turn a junk URL into a stack
    trace. That is a worse leak than the 404 it should be.

    Only shapes that survive URL normalisation are probed here, so each of these
    really does reach a handler and get refused by the store.
    """
    for url in (
        f"/api/shipments/{bad_id}/report",
        f"/api/shipments/{bad_id}/corrections",
        f"/api/shipments/{bad_id}/export",
        f"/api/shipments/{bad_id}/report.pdf",
        f"/api/shipments/{bad_id}/raw/anything.json",
        f"/page/{bad_id}/0.jpg",
    ):
        res = client.get(url)
        assert res.status_code == 404, url
    res = client.post(f"/api/shipments/{bad_id}/retry", json={"document_id": "d1"})
    assert res.status_code == 404
    res = client.post(
        f"/api/shipments/{bad_id}/documents/d1/fields/total_amount",
        json={"value": "1"},
    )
    assert res.status_code == 404


def test_a_real_page_still_serves_after_the_guard(client):
    """The traversal fixes must not quietly break the one route they protect."""
    sid = client.get("/api/fixture/shipment_clean").json()["shipment_id"]
    body = client.get(f"/api/shipments/{sid}/report").json()
    image_path = body["documents"][0]["pages"][0]["image_path"]
    res = client.get(f"/page/{sid}/{image_path}")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"


def test_retry_endpoint_round_trips(client):
    sid = client.get("/api/fixture/shipment_clean").json()["shipment_id"]
    body = client.get(f"/api/shipments/{sid}/report").json()
    doc_id = body["documents"][0]["id"]
    res = client.post(f"/api/shipments/{sid}/retry", json={"document_id": doc_id})
    assert res.status_code == 200
    assert res.json()["shipment_id"] == sid


def test_retry_unknown_document_is_404(client):
    sid = client.get("/api/fixture/shipment_clean").json()["shipment_id"]
    res = client.post(f"/api/shipments/{sid}/retry", json={"document_id": "nope"})
    assert res.status_code == 404


def test_bad_doc_types_json_rejected(client):
    one = fixture_files("shipment_clean")[0]
    res = client.post(
        "/api/shipments",
        data={"doc_types": "not-json"},
        files=[("files", (one[0], io.BytesIO(one[1]), "image/png"))],
    )
    assert res.status_code == 400


def test_report_json_is_valid_against_the_model(client):
    """The frontend polls this shape; a schema drift would break the UI silently."""
    from app.models import Report

    sid = client.get("/api/fixture/shipment_messy").json()["shipment_id"]
    raw = client.get(f"/api/shipments/{sid}/report")
    parsed = Report.model_validate(raw.json())
    assert parsed.summary.total_fields == len(FIELD_ORDER)
    assert parsed.run.prompt_version


def test_health_reports_both_providers(client):
    """The UI reads vision/summary sub-objects to label itself honestly, so the
    two-provider split must be visible in /api/health."""
    body = client.get("/api/health").json()
    assert body["vision"]["provider"] == "nvidia"
    assert "integrate.api.nvidia.com" in body["vision"]["host"]
    assert body["vision"]["key_present"] is False  # test fixture has no keys
    assert body["summary"]["provider"] == "nebius"
    assert "tokenfactory.nebius.com" in body["summary"]["host"]
    assert body["summary"]["enabled"] is True
    # Legacy keys retained so nothing that read the old shape breaks.
    assert body["model"] == body["vision"]["model"]
    assert "api_key_present" in body


def test_health_reports_mode_version_and_commit(client):
    """Health is the page a load balancer and a cold-starting operator both
    read, so it carries what they need to triage: which mode, which build,
    which revision -- and never the key."""
    body = client.get("/api/health").json()
    assert body["mode"] == "demo"
    assert body["version"] == "1.0.0"
    # Either a real revision (running inside this checkout) or the honest
    # "unknown" (running in a container built without .git). Never empty.
    assert isinstance(body["git_commit"], str) and body["git_commit"]


def test_health_says_configured_but_never_the_key(client):
    """A keyless instance reports not-configured as a boolean. The key itself
    must not appear anywhere in the payload -- a health endpoint is the most
    likely thing to get scraped, and `configured: true` is all an operator
    needs to know."""
    body = client.get("/api/health").json()
    assert body["vision"]["configured"] is False
    assert body["summary"]["configured"] is False
    blob = json.dumps(body)
    assert "sk-" not in blob
    assert "nvapi-" not in blob


def test_health_reports_a_degraded_provider(client):
    """The circuit breaker's state is part of readiness: an operator watching
    /api/health must see when a host has tripped open, so they stop waiting on
    it."""
    rec = reconciler()
    rec.client._consecutive_failures = rec.settings.circuit_breaker_threshold
    body = client.get("/api/health").json()
    assert body["vision"]["degraded"] is True
    assert body["summary"]["degraded"] is False


def test_ui_serves_the_narrative_section(client):
    page = client.get("/").text
    assert 'id="narrative-card"' in page
    assert "AI-generated" in page
