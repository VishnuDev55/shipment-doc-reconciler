"""FastAPI application.

Routes are deliberately few: upload, poll one JSON report, retry one document,
download the ZIP, and load a bundled fixture. The frontend polls report.json
every second — no websockets, no job queue.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT, _usable, get_settings
from .export import build_zip, fixture_manifest
from .models import ConfigUpdate, Report, RetryRequest
from .orchestrator import Reconciler

STATIC_DIR = PROJECT_ROOT / "web"
FIXTURE_INDEX = PROJECT_ROOT / "fixtures" / "index.json"
FIXTURE_IMAGES = PROJECT_ROOT / "fixtures" / "images"

app = FastAPI(
    title="Shipment Doc Reconciler",
    description=(
        "Cross-check quantity, gross weight, container number, HS code, "
        "consignee and invoice value across trade documents."
    ),
    version="1.0.0",
)

_reconciler: Reconciler | None = None


def reconciler() -> Reconciler:
    global _reconciler
    if _reconciler is None:
        _reconciler = Reconciler(get_settings())
    return _reconciler


def configure(rec: Reconciler) -> None:
    """Inject a configured reconciler. Used by tests."""
    global _reconciler
    _reconciler = rec


def active_settings():
    """Settings of the live reconciler, so an injected instance is authoritative.

    Reading get_settings() directly would make /api/health report the process
    environment rather than the configuration actually serving requests.
    """
    return reconciler().settings


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = STATIC_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(500, "web/index.html missing from the deployment")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict:
    """Two providers are in play; report each one's readiness separately so the
    UI can be truthful about which parts of the app can actually run."""
    rec = reconciler()
    settings = rec.settings
    vision = rec.client.endpoint
    summary = rec.summary_client.endpoint
    return {
        "status": "ok",
        "demo_mode": settings.demo_mode,
        # Kept for backwards compatibility with anything reading the old shape.
        "model": vision.model,
        "api_key_present": settings.has_nebius_key,
        "base_url": settings.nebius_base_url,
        "vision": {
            "provider": vision.provider,
            "model": vision.model,
            "host": vision.host,
            "key_present": bool(vision.api_key.strip()),
        },
        "summary": {
            "provider": summary.provider,
            "model": summary.model,
            "host": summary.host,
            "key_present": bool(summary.api_key.strip()),
            "enabled": settings.enable_summary,
        },
    }


# ---------------------------------------------------------------------------
# runtime configuration
# ---------------------------------------------------------------------------

# GET returns a mask and the browser re-submits untouched fields, so the marker
# is how POST tells "left alone" from "a key someone actually typed".
MASK_MARK = "…"


def _mask(key: str) -> str:
    """Identify a key without revealing it."""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 9:
        return MASK_MARK * 4
    return f"{key[:5]}{MASK_MARK}{key[-4:]}"


def config_view() -> dict:
    s = reconciler().settings
    return {
        "editable": s.gui_config_enabled,
        "demo_mode": s.demo_mode,
        "enable_summary": s.enable_summary,
        "vision_provider": s.vision_provider,
        "keys": {
            "nvidia": {"set": s.has_nvidia_key, "masked": _mask(s.nvidia_api_key)},
            "nebius": {"set": s.has_nebius_key, "masked": _mask(s.nebius_api_key)},
        },
        "models": {
            "nvidia_vision": s.nvidia_vision_model,
            "nebius_vision": s.nebius_vision_model,
            "nebius_summary": s.nebius_summary_model,
        },
    }


@app.get("/api/config")
def get_config() -> dict:
    """Runtime configuration, keys masked.

    A key is never returned in full: on a public deployment a readable config
    endpoint would turn "anyone can open the page" into "anyone can read your
    provider key".
    """
    return config_view()


@app.post("/api/config")
def set_config(body: ConfigUpdate) -> dict:
    rec = reconciler()
    if not rec.settings.gui_config_enabled:
        raise HTTPException(
            403,
            "Runtime configuration is disabled on this deployment "
            "(GUI_CONFIG_ENABLED=false). Set NVIDIA_API_KEY / NEBIUS_API_KEY as "
            "environment variables instead.",
        )

    updates = body.model_dump(exclude_unset=True, exclude_none=True)

    for field in ("nvidia_api_key", "nebius_api_key"):
        if field not in updates:
            continue
        value = updates[field].strip()
        if MASK_MARK in value:
            del updates[field]
            continue
        if value and not _usable(value):
            raise HTTPException(
                400, f"{field}: {value!r} is a template placeholder, not a real key"
            )
        updates[field] = value

    if not updates:
        raise HTTPException(400, "no changes supplied")

    try:
        rec.reconfigure(updates)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return config_view()


@app.get("/api/fixtures")
def fixtures() -> JSONResponse:
    """The bundled demo shipments, so the demo needs no file picker."""
    manifest = fixture_manifest(FIXTURE_INDEX)
    out = {}
    for name, entry in (manifest.get("shipments") or {}).items():
        out[name] = {
            "documents": [
                {
                    "files": d["files"],
                    "doc_type": d["doc_type"],
                    "pages": d["pages"],
                }
                for d in entry["documents"]
            ]
        }
    return JSONResponse(
        {
            "demo_mode": active_settings().demo_mode,
            "note": manifest.get("note", ""),
            "shipments": out,
        }
    )


# ---------------------------------------------------------------------------
# core API
# ---------------------------------------------------------------------------


@app.post("/api/shipments")
async def create_shipment(
    files: list[UploadFile] = File(...),
    doc_types: str = Form(""),
) -> JSONResponse:
    """Upload 1-5 documents for one shipment. Returns the shipment id to poll."""
    declared: list[str | None] = []
    if doc_types:
        try:
            parsed = json.loads(doc_types)
            declared = [d if d else None for d in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            raise HTTPException(400, "doc_types must be a JSON array of strings")

    settings = active_settings()
    if len(files) > settings.max_documents:
        raise HTTPException(
            400, f"at most {settings.max_documents} documents per shipment"
        )

    uploads: list[tuple[str, str | None, bytes]] = []
    for i, upload in enumerate(files):
        data = await upload.read()
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                413, f"{upload.filename} exceeds the {settings.max_upload_bytes} byte limit"
            )
        declared_type = declared[i] if i < len(declared) else None
        uploads.append((upload.filename or f"document{i + 1}", declared_type, data))

    try:
        shipment_id, report = await reconciler().create_shipment(uploads)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return JSONResponse(
        {"shipment_id": shipment_id, "state": report.state, "documents": len(report.documents)},
        status_code=201,
    )


@app.get("/api/shipments/{shipment_id}/report", response_model=Report)
def get_report(shipment_id: str) -> Report:
    report = reconciler().get(shipment_id)
    if report is None:
        raise HTTPException(404, "unknown shipment id")
    return report


@app.post("/api/shipments/{shipment_id}/retry")
async def retry_document(shipment_id: str, body: RetryRequest) -> dict:
    report = await reconciler().retry_document(shipment_id, body.document_id)
    if report is None:
        raise HTTPException(404, "unknown shipment or document")
    return {"shipment_id": shipment_id, "state": report.state}


@app.get("/api/shipments/{shipment_id}/export")
def export_zip(shipment_id: str) -> Response:
    rec = reconciler()
    report = rec.get(shipment_id)
    if report is None:
        raise HTTPException(404, "unknown shipment id")

    archive = build_zip(report, rec.store.dir(shipment_id))
    filename = f"reconciliation_{shipment_id}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/shipments")
def list_shipments() -> dict:
    return {"shipments": reconciler().store.list_shipments()}


@app.get("/api/fixture/{name}")
async def load_fixture(name: str) -> JSONResponse:
    """Server-side fixture load: reads bundled images and creates a shipment.

    Lets the demo run from a URL with no file picking, and proves the fixture
    path goes through exactly the same pipeline as a real upload.
    """
    manifest = fixture_manifest(FIXTURE_INDEX)
    entry = (manifest.get("shipments") or {}).get(name)
    if entry is None:
        raise HTTPException(404, f"no bundled fixture named {name!r}")

    uploads: list[tuple[str, str | None, bytes]] = []
    for doc in entry["documents"]:
        for fname in doc["files"]:
            path = FIXTURE_IMAGES / Path(fname).name
            if not path.is_file():
                raise HTTPException(500, f"fixture image missing: {fname}")
            uploads.append((path.name, doc["doc_type"], path.read_bytes()))

    rec = reconciler()
    shipment_id, _ = await rec.create_shipment(uploads)
    await rec.wait(shipment_id, timeout=600)
    report = rec.get(shipment_id)
    return JSONResponse(
        {
            "shipment_id": shipment_id,
            "state": report.state if report else "failed",
            "documents": len(report.documents) if report else 0,
            "demo_mode": report.run.demo_mode if report else None,
        },
        status_code=201,
    )


@app.get("/page/{shipment_id}/{path:path}")
def serve_page(shipment_id: str, path: str) -> FileResponse:
    """Serve normalized page images used as evidence."""
    rec = reconciler()
    base = rec.store.dir(shipment_id).resolve()
    target = (base / path).resolve()
    # Containment via the stdlib helper. A string prefix test would let
    # runs/abcdef satisfy a check for runs/abc.
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404, "no such page")
    return FileResponse(target, media_type="image/jpeg")
