"""FastAPI application: the reconciliation UI and the API underneath it.

Upload documents, read one JSON report, watch it progress over SSE, retry a
document, correct a field, and export the result as ZIP, PDF or CSV. The same
routes serve the browser and any other client; nothing here is UI-specific.

When `API_KEYS` is configured every data-bearing route requires one of those
keys, sent as `X-API-Key` or `Authorization: Bearer` by a client that can set
headers, or held in a short session cookie by the browser UI. `/api/health`
stays open on purpose: a load balancer has to be able to ask whether the service
is up without holding a secret.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from functools import lru_cache
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request, UploadFile)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT, _usable, get_settings
from .correct import CorrectionError, active_corrections, correction_trail
from .limits import RateLimiter
from .export import build_zip, fixture_manifest
from .pdf import render_pdf
from .models import (ConfigUpdate, CorrectionRequest, Report, RetryRequest, SessionRequest)
from .orchestrator import Reconciler
from .textlayer import PageText, load_sidecar

STATIC_DIR = PROJECT_ROOT / "web"
FIXTURE_INDEX = PROJECT_ROOT / "fixtures" / "index.json"
FIXTURE_IMAGES = PROJECT_ROOT / "fixtures" / "images"
# The text each fixture page was rendered from. A degraded PNG carries no text
# layer, so without this the bundled demo would report every field as
# "not checkable" and the evidence check could never be seen working offline.
FIXTURE_TEXT = PROJECT_ROOT / "fixtures" / "text"

APP_VERSION = "1.0.0"


@lru_cache(maxsize=1)
def git_commit() -> str:
    """The revision this process is running, best-effort and never slow.

    /api/health is polled by load balancers every few seconds, so this must not
    shell out per call; it is cached for the process lifetime and reads only
    files or the environment. Deploy platforms already put the commit in the
    environment and win here (Render: RENDER_GIT_COMMIT, Actions: GITHUB_SHA,
    and GIT_COMMIT, which the Dockerfile bakes in because the image does not
    COPY .git). A local checkout reads it out of .git. Anywhere else it is
    "unknown" rather than a made-up revision.
    """
    for name in ("RENDER_GIT_COMMIT", "GITHUB_SHA", "GIT_COMMIT", "SOURCE_VERSION"):
        value = os.environ.get(name)
        if value:
            return value
    try:
        head = PROJECT_ROOT / ".git" / "HEAD"
        if head.is_file():
            ref = head.read_text(encoding="utf-8").strip()
            if ref.startswith("ref: "):
                target = PROJECT_ROOT / ".git" / ref[5:]
                if target.is_file():
                    return target.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return "unknown"


app = FastAPI(
    title="Shipment Doc Reconciler",
    description=(
        "Cross-check ten fields across trade documents: quantity, gross weight, "
        "net weight, container number, HS code, consignee, invoice value, "
        "Incoterms, PO number and country of origin. Extraction is model-driven; "
        "normalization, comparison and the risk score are deterministic Python. "
        "Requests need an API key when the deployment sets API_KEYS; "
        "/api/health is always open."
    ),
    version=APP_VERSION,
)

_reconciler: Reconciler | None = None

# Budget for the one endpoint that spends money. Owned per process, and reset
# whenever a new reconciler is injected, so an unrelated test cannot be the
# reason a 429 appeared.
_limiter = RateLimiter()


def _limiter_for() -> RateLimiter:
    limit = active_settings().run_limit_per_minute
    if _limiter.limit != limit:
        _limiter.limit = limit
        _limiter.reset()
    return _limiter


def _client_key(request: Request) -> str:
    """Who to charge the budget to.

    The API key when one was supplied, because that is the only identity that
    exists here; otherwise the peer address, taking the first hop of
    X-Forwarded-For since a proxy is the normal way to reach this app. A shared
    key is one budget by design -- if it is being shared, that is the deployment
    problem to fix, not something to hide here.
    """
    key = (request.headers.get("x-api-key") or "").strip()
    if not key:
        auth = (request.headers.get("authorization") or "").strip()
        if auth[:7].lower() == "bearer ":
            key = auth[7:].strip()
    if key:
        return "key:" + key[-12:]
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return "ip:" + forwarded.split(",")[0].strip()
    client = request.client
    return "ip:" + (client.host if client else "unknown")


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


API_KEY_HEADER = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Required when the deployment sets API_KEYS. Also accepted as "
                "`Authorization: Bearer <key>`.",
)

# Long enough to survive a demo and a working session, short enough that losing
# the process invalidating it is not an outage. In-process by design: this app
# is one worker with a filesystem store, and a cookie that outlived a restart
# would be a secret with no way to revoke it.
SESSION_TTL_SECONDS = 12 * 60 * 60
_sessions: dict[str, float] = {}


def _issue_session() -> str:
    import secrets
    import time

    now = time.time()
    for token in [t for t, expires in _sessions.items() if expires <= now]:
        _sessions.pop(token, None)
    token = secrets.token_urlsafe(32)
    _sessions[token] = now + SESSION_TTL_SECONDS
    return token


def _session_valid(request: Request) -> bool:
    import time

    token = request.cookies.get("sdr_session") or ""
    expires = _sessions.get(token)
    if not expires:
        return False
    if expires < time.time():
        _sessions.pop(token, None)
        return False
    return True


def require_api_key(request: Request,
                    supplied: str | None = Depends(API_KEY_HEADER)) -> None:
    """Accept the request only if it carries a configured key or a live session.

    An empty API_KEYS means an open deployment, which is the documented default
    for the keyless demo, so this returns rather than refusing. When keys exist
    the comparison is constant-time against each of them, and neither the value
    supplied nor the value expected is echoed back or logged: a 401 body that
    repeats a caller's key would put that key in somebody's access log.
    """
    allowed = active_settings().api_key_list
    if not allowed:
        return

    if _session_valid(request):
        return

    candidates = [supplied] if supplied else []
    bearer = (request.headers.get("authorization") or "").strip()
    if bearer[:7].lower() == "bearer ":
        candidates.append(bearer[7:].strip())

    for given in (c.strip() for c in candidates if c and c.strip()):
        if any(hmac.compare_digest(given, key) for key in allowed):
            return
    raise HTTPException(
        status_code=401,
        detail="this deployment requires an API key: send X-API-Key or "
               "Authorization: Bearer, or POST the key to /api/session",
        headers={"WWW-Authenticate": "X-API-Key"},
    )


@app.post("/api/session", tags=["auth"])
def open_session(body: SessionRequest, response: Response) -> dict:
    """Trade an API key for the session cookie the browser UI needs.

    SameSite=Strict plus a JSON body plus no state change on GET is what makes a
    cookie here safe to accept: a cross-site request cannot carry the cookie in a
    way that reaches this handler. The key itself is never stored or returned --
    only checked, then forgotten in favour of a random nonce.
    """
    allowed = active_settings().api_key_list
    key = (body.key or "").strip()
    if not allowed:
        raise HTTPException(400, "this deployment is open: no API key is required")
    if not key or not any(hmac.compare_digest(key, k) for k in allowed):
        raise HTTPException(401, "that key is not valid for this deployment")
    token = _issue_session()
    response.set_cookie(
        "sdr_session", token, max_age=SESSION_TTL_SECONDS, httponly=True,
        samesite="strict", path="/",
    )
    return {"session": "opened", "expires_in_seconds": SESSION_TTL_SECONDS}


@app.delete("/api/session", tags=["auth"])
def close_session(request: Request, response: Response) -> dict:
    token = request.cookies.get("sdr_session") or ""
    _sessions.pop(token, None)
    response.delete_cookie("sdr_session", path="/")
    return {"session": "closed"}


# Cross-origin calls are opt-in and decided at startup: which origins may call
# from a browser is deployment configuration, not something to renegotiate per
# request, and an unset CORS_ORIGINS leaves the app same-origin only.
_allowed_origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
if _allowed_origins:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=True,  # the session cookie is how a browser auths
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["X-API-Key", "X-Config-Token", "Content-Type"],
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """The minimum that makes an accidentally public deployment less damaging.

    nosniff stops a browser deciding an uploaded document is a script;
    X-Frame-Options stops a click-jacking frame around the Configure drawer;
    no-referrer keeps a shipment id out of the site the page links onward to.

    The Content-Security-Policy is same-origin for scripts and allows inline
    styles on purpose: the exported report embeds its own CSS so it opens
    anywhere, which is the feature rather than an oversight to close.
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


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
    UI can be truthful about which parts of the app can actually run.

    Fast on purpose: no provider is contacted, no disk is written, and the one
    subprocess-ish thing (the git revision) is cached. A load balancer polls
    this every few seconds, and a keyless instance must still answer 200 --
    "unconfigured" is a state the operator fixes in the dashboard, not a reason
    to restart the container. Provider status is `configured`/`not configured`
    booleans only; the key itself is never echoed.
    """
    rec = reconciler()
    settings = rec.settings
    vision = rec.client.endpoint
    summary = rec.summary_client.endpoint
    return {
        "status": "ok",
        # `mode` is the human-facing word; `demo_mode` stays for anything that
        # already read the boolean.
        "mode": "demo" if settings.demo_mode else "live",
        "demo_mode": settings.demo_mode,
        "version": APP_VERSION,
        "git_commit": git_commit(),
        # The UI reads this to decide whether to ask for a key before it can
        # show anything. Health itself stays open so a load balancer can poll it
        # without holding a secret.
        "api_protected": bool(settings.api_key_list),
        # Kept for backwards compatibility with anything reading the old shape.
        "model": vision.model,
        "api_key_present": settings.has_nebius_key,
        "base_url": settings.nebius_base_url,
        "vision": {
            "provider": vision.provider,
            "model": vision.model,
            "host": vision.host,
            "configured": bool(vision.api_key.strip()),
            "key_present": bool(vision.api_key.strip()),
            "degraded": rec.client.degraded,
        },
        "summary": {
            "provider": summary.provider,
            "model": summary.model,
            "host": summary.host,
            "configured": bool(summary.api_key.strip()),
            "key_present": bool(summary.api_key.strip()),
            "degraded": rec.summary_client.degraded,
            "enabled": settings.enable_summary,
        },
        # Whether those model names have been confirmed against what each host
        # actually serves. Reported separately because "configured" and
        # "reachable" are different claims, and conflating them is how a dead
        # model turns into a mystery.
        "models": rec.model_selections(),
    }


@app.get("/api/models", dependencies=[Depends(require_api_key)])
def models() -> dict:
    """What this app decided to call, per role, and why."""
    rec = reconciler()
    return {
        "demo_mode": rec.settings.demo_mode,
        "discovery_enabled": rec.settings.model_discovery,
        "selections": rec.model_selections(),
    }


@app.post("/api/models/refresh", dependencies=[Depends(require_api_key)])
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
        "consensus": s.consensus,
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


@app.get("/api/config", dependencies=[Depends(require_api_key)])
def get_config(request: Request) -> dict:
    """Runtime configuration, keys masked.

    A key is never returned in full: on a public deployment a readable config
    endpoint would turn "anyone can open the page" into "anyone can read your
    provider key".
    """
    _require_config_token(request)
    return config_view()


@app.post("/api/config", dependencies=[Depends(require_api_key)])
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


@app.get("/api/fixtures", dependencies=[Depends(require_api_key)])
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


READ_CHUNK = 1 << 20  # 1 MiB


async def _read_capped(upload: UploadFile, limit: int) -> bytes:
    """Read an upload, refusing it the moment it passes the limit.

    ``await upload.read()`` followed by a length check reads the whole body into
    memory first, so the limit only ever decides whether to reject something that
    has already been paid for -- and five files at a generous limit is a request
    that can exhaust the worker. Reading in chunks aborts at limit + 1 byte.
    """
    buffer = bytearray()
    while True:
        piece = await upload.read(READ_CHUNK)
        if not piece:
            return bytes(buffer)
        buffer.extend(piece)
        if len(buffer) > limit:
            raise HTTPException(
                413, f"{upload.filename} exceeds the {limit} byte limit"
            )


@app.post("/api/shipments", dependencies=[Depends(require_api_key)])
async def create_shipment(
    request: Request,
    files: list[UploadFile] = File(...),
    doc_types: str = Form(""),
) -> JSONResponse:
    """Upload 1-5 documents for one shipment. Returns the shipment id to poll."""
    limiter = _limiter_for()
    allowed, retry_in = limiter.check(_client_key(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"too many shipments: {limiter.limit} per minute. Retry in "
                   f"{max(1, int(retry_in) + 1)}s.",
            headers={"Retry-After": str(max(1, int(retry_in) + 1))},
        )
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
        data = await _read_capped(upload, settings.max_upload_bytes)
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


@app.get("/api/shipments/{shipment_id}/report", response_model=Report, dependencies=[Depends(require_api_key)])
def get_report(shipment_id: str) -> Report:
    report = reconciler().get(shipment_id)
    if report is None:
        raise HTTPException(404, "unknown shipment id")
    return report


@app.post("/api/shipments/{shipment_id}/documents/{document_id}/fields/{field}", dependencies=[Depends(require_api_key)])
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


@app.post("/api/shipments/{shipment_id}/documents/{document_id}/fields/{field}/revert", dependencies=[Depends(require_api_key)])
def revert_field(shipment_id: str, document_id: str, field: str) -> Report:
    """Undo the active corrections on one field. History is kept, not deleted."""
    rec = reconciler()
    try:
        return rec.revert_field(shipment_id, document_id, field)
    except KeyError:
        raise HTTPException(404, "unknown shipment id")
    except CorrectionError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/shipments/{shipment_id}/corrections", dependencies=[Depends(require_api_key)])
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


@app.get("/api/shipments/{shipment_id}/raw/{name}", response_class=PlainTextResponse, dependencies=[Depends(require_api_key)])
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


@app.get("/api/shipments/{shipment_id}/events", dependencies=[Depends(require_api_key)])
async def shipment_events(shipment_id: str) -> StreamingResponse:
    """Server-sent events: per-document stage transitions, then the report on
    each settle, then a close.

    Additive, not a replacement. The 1-second poll still works and the frontend
    falls back to it if this stream never opens, so nothing here is load-bearing
    for a run's lifetime: a client that disconnects unsubscribes in `finally`
    and the extraction continues either way. `event: stage` frames are progress
    (queued/reading/verifying/comparing/done/failed); `event: report` frames
    carry the decisions.
    """
    rec = reconciler()
    if rec.get(shipment_id) is None:
        raise HTTPException(404, "unknown shipment id")

    queue = rec.subscribe(shipment_id)

    async def stream():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    # A comment line keeps intermediaries from buffering the
                    # stream shut during a slow 30-90 s extraction.
                    yield ": ping\n\n"
                    continue
                if isinstance(item, Report):
                    yield f"event: report\ndata: {item.model_dump_json()}\n\n"
                    if item.state in TERMINAL_STATES:
                        yield "event: done\ndata: {}\n\n"
                        return
                else:
                    # A per-document stage event (see Reconciler._emit_stage).
                    # Progress only: the report frames above carry the decisions.
                    yield f"event: stage\ndata: {json.dumps(item)}\n\n"
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


@app.post("/api/shipments/{shipment_id}/retry", dependencies=[Depends(require_api_key)])
async def retry_document(shipment_id: str, body: RetryRequest) -> dict:
    report = await reconciler().retry_document(shipment_id, body.document_id)
    if report is None:
        raise HTTPException(404, "unknown shipment or document")
    return {"shipment_id": shipment_id, "state": report.state}


@app.get("/api/shipments/{shipment_id}/export", dependencies=[Depends(require_api_key)])
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


@app.get("/api/shipments/{shipment_id}/report.pdf",
         dependencies=[Depends(require_api_key)])
def get_report_pdf(shipment_id: str) -> Response:
    """The same report, laid out for print and filing.

    Rendered from the Report model rather than converted from the web page, so
    what an auditor files is the verdict, the reason, the provenance line and
    the corrections trail -- not whatever a browser happened to be styling.
    """
    rec = reconciler()
    report = rec.get(shipment_id)
    if report is None:
        raise HTTPException(404, "unknown shipment id")
    try:
        pdf = render_pdf(report)
    except Exception as exc:  # noqa: BLE001
        # A failed artifact is not a failed reconciliation; say which one broke.
        raise HTTPException(500, f"PDF rendering failed: {type(exc).__name__}")
    filename = f"reconciliation_{shipment_id}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/shipments", dependencies=[Depends(require_api_key)])
def list_shipments() -> dict:
    return {"shipments": reconciler().store.list_shipments()}


@app.get("/api/fixture/{name}", dependencies=[Depends(require_api_key)])
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
    texts: dict[str, list[PageText]] = {}
    for doc in entry["documents"]:
        for fname in doc["files"]:
            path = FIXTURE_IMAGES / Path(fname).name
            if not path.is_file():
                raise HTTPException(500, f"fixture image missing: {fname}")
            uploads.append((path.name, doc["doc_type"], path.read_bytes()))
            loaded = load_sidecar(FIXTURE_TEXT / f"{Path(fname).stem}.json")
            if loaded:
                texts[path.name] = loaded

    rec = reconciler()
    shipment_id, _ = await rec.create_shipment(uploads, page_texts=texts)
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


@app.get("/page/{shipment_id}/{path:path}", dependencies=[Depends(require_api_key)])
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
