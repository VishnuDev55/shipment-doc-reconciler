"""FastAPI application.

Routes are deliberately few: upload, poll one JSON report, retry one document,
download the ZIP, and load a bundled fixture. The frontend polls report.json
every second — no websockets, no job queue.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT, _usable, get_settings
from .correct import CorrectionError, active_corrections, correction_trail
from .export import build_zip, fixture_manifest
from .models import ConfigUpdate, CorrectionRequest, Report, RetryRequest
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
        # Whether those model names have been confirmed against what each host
        # actually serves. Reported separately because "configured" and
        # "reachable" are different claims, and conflating them is how a dead
        # model turns into a mystery.
        "models": rec.model_selections(),
    }


@app.get("/api/models")
def models() -> dict:
    """What this app decided to call, per role, and why."""
    rec = reconciler()
    return {
        "demo_mode": rec.settings.demo_mode,
        "discovery_enabled": rec.settings.model_discovery,
        "selections": rec.model_selections(),
    }


@app.post("/api/models/refresh")
async def refresh_models() -> dict:
    """Re-query both hosts now, ignoring the TTL.

    Manual because it is the one route here that makes an outbound call on
    demand; the panel and health read cached state instead.
    """
    rec = reconciler()
    await rec.refresh_models(force=True)
    return {"selections": rec.model_selections(), "demo_mode": rec.settings.demo_mode}


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


def _require_config_token(request: Request) -> None:
    """Reject unless the shared secret matches, when one is configured.

    Constant-time comparison: this is a secret, and an endpoint whose check can
    be timed is an endpoint that can be searched a character at a time.
    """
    expected = reconciler().settings.config_token.strip()
    if not expected:
        return
    supplied = request.headers.get("x-config-token", "")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "a valid X-Config-Token header is required for this deployment")


def config_view() -> dict:
    s = reconciler().settings
    return {
        "editable": s.gui_config_enabled,
        "token_required": bool(s.config_token.strip()),
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
def get_config(request: Request) -> dict:
    """Runtime configuration, keys masked.

    A key is never returned in full: on a public deployment a readable config
    endpoint would turn "anyone can open the page" into "anyone can read your
    provider key".
    """
    _require_config_token(request)
    return config_view()


@app.post("/api/config")
def set_config(request: Request, body: ConfigUpdate) -> dict:
    rec = reconciler()
    if not rec.settings.gui_config_enabled:
        raise HTTPException(
            403,
            "Runtime configuration is disabled on this deployment "
            "(GUI_CONFIG_ENABLED=false). Set NVIDIA_API_KEY / NEBIUS_API_KEY as "
            "environment variables instead.",
        )
    # Checked after the disabled test so the more useful message wins: an
    # operator who sees 401 on a locked deployment goes to the wrong setting.
    _require_config_token(request)

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


@app.post("/api/shipments/{shipment_id}/documents/{document_id}/fields/{field}")
def correct_field(
    shipment_id: str, document_id: str, field: str, body: CorrectionRequest
) -> Report:
    """Replace one transcribed value and re-derive the verdicts, now.

    Returns the whole recomputed report rather than an acknowledgement: the
    comparison, cross-field checks and risk score are pure functions of the
    values, so the reviewer sees the consequence in the same round trip and no
    model is called to produce it.
    """
    rec = reconciler()
    try:
        return rec.correct_field(
            shipment_id, document_id, field, body.value,
            actor=body.actor, note=body.note,
        )
    except KeyError:
        raise HTTPException(404, "unknown shipment id")
    except CorrectionError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/shipments/{shipment_id}/documents/{document_id}/fields/{field}/revert")
def revert_field(shipment_id: str, document_id: str, field: str) -> Report:
    """Undo the active corrections on one field. History is kept, not deleted."""
    rec = reconciler()
    try:
        return rec.revert_field(shipment_id, document_id, field)
    except KeyError:
        raise HTTPException(404, "unknown shipment id")
    except CorrectionError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/shipments/{shipment_id}/corrections")
def corrections(shipment_id: str) -> dict:
    """The append-only trail: every value a human changed, and what the model said."""
    rec = reconciler()
    report = rec.get(shipment_id)
    if report is None:
        raise HTTPException(404, "unknown shipment id")
    return {
        "shipment_id": shipment_id,
        "corrections": [entry.model_dump() for entry in correction_trail(report)],
        "active": len(active_corrections(report)),
    }


@app.get("/api/shipments/{shipment_id}/raw/{name}", response_class=PlainTextResponse)
def get_raw(shipment_id: str, name: str) -> PlainTextResponse:
    """One persisted model body, for the transparency panel.

    The report already lists every legitimate filename, so membership is the
    check — a filename the report never named is not evidence about this run,
    whatever path it would resolve to. Containment is re-asserted anyway rather
    than trusted transitively.
    """
    rec = reconciler()
    report = rec.get(shipment_id)
    if report is None:
        raise HTTPException(404, "unknown shipment id")
    if name not in {f for d in report.documents for f in d.raw_files}:
        raise HTTPException(404, "no raw response by that name for this run")

    base = (rec.store.dir(shipment_id) / "raw").resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404, "no raw response by that name for this run")
    return PlainTextResponse(target.read_text(errors="replace"))


TERMINAL_STATES = frozenset({"done", "partial", "failed"})


@app.get("/api/shipments/{shipment_id}/events")
async def shipment_events(shipment_id: str) -> StreamingResponse:
    """Server-sent events: the report each time it settles, then a close.

    Additive, not a replacement. The 1-second poll still works and the frontend
    falls back to it if this stream never opens, so nothing here is load-bearing
    for a run's lifetime: a client that disconnects unsubscribes in `finally`
    and the extraction continues either way.
    """
    rec = reconciler()
    if rec.get(shipment_id) is None:
        raise HTTPException(404, "unknown shipment id")

    queue = rec.subscribe(shipment_id)

    async def stream():
        try:
            while True:
                try:
                    report = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    # A comment line keeps intermediaries from buffering the
                    # stream shut during a slow 30-90 s extraction.
                    yield ": ping\n\n"
                    continue
                yield f"event: report\ndata: {report.model_dump_json()}\n\n"
                if report.state in TERMINAL_STATES:
                    yield "event: done\ndata: {}\n\n"
                    return
        finally:
            rec.unsubscribe(shipment_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


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
