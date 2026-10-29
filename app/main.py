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
import json
import os
from contextlib import asynccontextmanager
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
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import budget
from . import ingest
from .auth import (SESSION_TTL_SECONDS, _issue_session, _sessions, key_is_valid,
                   require_api_key, secret_matches)
from .config import PROJECT_ROOT, get_settings
from .config_view import _require_config_token, config_view, sanitize_config_updates
from .correct import CorrectionError, active_corrections, correction_trail
from .export import build_zip, fixture_manifest
from .models import (ConfigUpdate, CorrectionRequest, Report, RetryRequest, SessionRequest)
from .orchestrator import Reconciler
from .pdf import render_pdf
from .ratelimit import _client_key, _limiter_for
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


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Refuse to boot a live instance that has no key to check anyone against.

    `require_api_key` returns immediately when API_KEYS is blank -- that is what
    lets the bundled demo and every laptop work without a key -- and the same
    rule on an instance deployed with DEMO_MODE=false leaves every data-bearing
    route open to the internet, including the one that reads files from disk.
    The app cannot tell the two apart from inside a request, and nothing in the
    logs says which one you have, so it says it here instead.

    Reads the injected reconciler rather than get_settings(), so a test that
    configures its own instance is judged by the settings it actually set.
    """
    settings = active_settings()
    if (not settings.demo_mode and not settings.api_key_list
            and not settings.allow_open_live_deploy):
        raise RuntimeError(
            "refusing to start in live mode with API_KEYS empty: every route, "
            "including /page/ which reads files from disk, would be open to "
            "anyone who can reach this port. Set API_KEYS to at least one key "
            "(docs/SECURITY.md calls that the minimum for a live deployment), "
            "or run with DEMO_MODE=true, or -- if this instance really is "
            "behind something that authenticates -- say so explicitly with "
            "ALLOW_OPEN_LIVE_DEPLOY=1."
        )
    yield


app = FastAPI(
    title="Shipment Doc Reconciler",
    lifespan=_lifespan,
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


def reconciler() -> Reconciler:
    global _reconciler
    if _reconciler is None:
        _reconciler = Reconciler(get_settings())
    return _reconciler


def configure(rec: Reconciler) -> None:
    """Inject a configured reconciler. Used by tests."""
    global _reconciler
    _reconciler = rec
    # Reservations are projected spend for the instance that made them, and a
    # new reconciler is a different instance with a different cap. Carrying the
    # previous one's reservations over would make an unrelated earlier test the
    # reason this one saw a 429 -- the same reason _limiter_for() re-arms the
    # rate limiter when the injected limit changes.
    budget.reset_pending()


def active_settings():
    """Settings of the live reconciler, so an injected instance is authoritative.

    Reading get_settings() directly would make /api/health report the process
    environment rather than the configuration actually serving requests.
    """
    return reconciler().settings


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
    if not key or not any(secret_matches(key, k) for k in allowed):
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
def health(request: Request) -> dict:
    """Two providers are in play; report each one's readiness separately so the
    UI can be truthful about which parts of the app can actually run.

    Fast on purpose: no provider is contacted, no disk is written, and the one
    subprocess-ish thing (the git revision) is cached. A load balancer polls
    this every few seconds, and a keyless instance must still answer 200 --
    "unconfigured" is a state the operator fixes in the dashboard, not a reason
    to restart the container. Provider status is `configured`/`not configured`
    booleans only; the key itself is never echoed.

    On a keyed deployment a stranger gets status, mode and `api_protected` and
    nothing else. Those three are what a load balancer and the UI need; the model
    ids, hostnames, version and retention window are a description of what to
    target, and they are not somebody's health check.
    """
    rec = reconciler()
    settings = rec.settings
    body: dict = {
        "status": "ok",
        # `mode` is the human-facing word; `demo_mode` stays for anything that
        # already read the boolean.
        "mode": "demo" if settings.demo_mode else "live",
        "demo_mode": settings.demo_mode,
        # The UI reads this to decide whether to ask for a key before it can
        # show anything. Health itself stays open so a load balancer can poll it
        # without holding a secret.
        "api_protected": bool(settings.api_key_list),
    }
    if not key_is_valid(request):
        return body

    vision = rec.client.endpoint
    summary = rec.summary_client.endpoint
    body.update({
        "version": APP_VERSION,
        "git_commit": git_commit(),
        # Hours after which uploaded runs are deleted at startup. Zero means they
        # are kept, which is what a laptop wants; the page reads this to say so
        # rather than carrying a second, hand-typed claim about retention.
        "run_ttl_hours": settings.run_ttl_hours,
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
    })
    return body


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

    updates = sanitize_config_updates(
        body.model_dump(exclude_unset=True, exclude_none=True)
    )

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


def _cap_refusal(exc: budget.SpendCapReached) -> HTTPException:
    """Turn a refused reservation into the 429 a caller can act on.

    The message is written by `budget`, because the numbers in it -- what the day
    has cost, what is reserved, what this run would have cost -- are only visible
    together at the moment the gate refused.
    """
    return HTTPException(
        429,
        detail=str(exc),
        headers={"Retry-After": str(budget.seconds_until_utc_midnight())},
    )


def _gate_spending(request: Request):
    """Refuse a request that would bill the operator, before it bills anything.

    Two endpoints reach a live model: creating a shipment and retrying one
    document. They share this gate so a retry cannot be a hole in a control that
    the upload has -- which is exactly what it was.

    The money check comes first: it is the one refusal that no retry or wait of
    seconds can clear, and it applies to every caller at once rather than to one
    client's minute. Demo mode is exempt because a replay spends nothing --
    blocking it would break a laptop whose ledger happens to hold an old day.
    """
    settings = active_settings()
    if not settings.demo_mode and budget.cap_reached(settings):
        spent = budget.spent_today(settings)
        retry_after = budget.seconds_until_utc_midnight()
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily spend cap reached: ${spent:.2f} of "
                f"${settings.usd_daily_cap:.2f} spent today (UTC). Uploads resume "
                "after 00:00 UTC. The bundled sample shipments still work from "
                "cache, and they cost nothing."
            ),
            headers={"Retry-After": str(retry_after)},
        )
    limiter = _limiter_for()
    allowed, retry_in = limiter.check(_client_key(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"too many shipments: {limiter.limit} per minute. Retry in "
                   f"{max(1, int(retry_in) + 1)}s.",
            headers={"Retry-After": str(max(1, int(retry_in) + 1))},
        )
    return settings


@app.post("/api/shipments", dependencies=[Depends(require_api_key)])
async def create_shipment(
    request: Request,
    files: list[UploadFile] = File(...),
    doc_types: str = Form(""),
) -> JSONResponse:
    """Upload 1-5 documents for one shipment. Returns the shipment id to poll."""
    settings = _gate_spending(request)
    declared: list[str | None] = []
    if doc_types:
        try:
            parsed = json.loads(doc_types)
            declared = [d if d else None for d in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            raise HTTPException(400, "doc_types must be a JSON array of strings")

    if len(files) > settings.max_documents:
        raise HTTPException(
            400,
            f"this instance accepts at most {settings.max_documents} documents per "
            f"shipment; you sent {len(files)}. Split the shipment, or put several "
            "pages in one file.",
        )

    uploads: list[tuple[str, str | None, bytes]] = []
    for i, upload in enumerate(files):
        data = await _read_capped(upload, settings.max_upload_bytes)
        declared_type = declared[i] if i < len(declared) else None
        name = upload.filename or f"document{i + 1}"
        if settings.reject_oversize_pages and settings.max_pages_per_document > 0:
            # Counted here, before the pipeline spends a vision call on a file it
            # is only going to read part of. A single-document failure inside the
            # run would be a 201 with a failed row; an upload this instance will
            # not accept deserves a 400 that says which file and why.
            try:
                pages = await run_in_threadpool(
                    ingest.page_count, name, data)
            except ValueError:
                pages = 0  # unreadable here; the pipeline reports that properly
            if pages > settings.max_pages_per_document:
                raise HTTPException(
                    400,
                    f"{name} has {pages} pages; this instance reads at most "
                    f"{settings.max_pages_per_document} per document. Split the "
                    "file, or upload the pages that matter.",
                )
        uploads.append((name, declared_type, data))

    try:
        shipment_id, report = await reconciler().create_shipment(uploads)
    except budget.SpendCapReached as exc:
        # The door check above compares the ledger with the cap; this refusal
        # came from inside the dispatcher, where reservations are visible.
        raise _cap_refusal(exc) from exc
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
async def retry_document(shipment_id: str, request: Request,
                         body: RetryRequest) -> dict:
    # A retry re-reads a document through the vision model, so it is the second
    # path to the operator's bill and was the one nobody gated.
    _gate_spending(request)
    try:
        report = await reconciler().retry_document(shipment_id, body.document_id)
    except budget.SpendCapReached as exc:
        raise _cap_refusal(exc) from exc
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
    # Interpolated into a header, so its alphabet matters: reaching here means
    # store.read() found a report, and store.read() only accepts ids matching
    # [A-Za-z0-9_-]{1,24}. No quote, CR or LF can survive that, so the filename
    # needs no separate sanitizer.
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
    rows, skipped = reconciler().store.list_shipments()
    return {"shipments": rows, "skipped": skipped}


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

    # A sample is a promise, not a mode: clicking one must never spend money,
    # whatever DEMO_MODE says. A separate reconciler whose settings force demo
    # mode keeps the live clients of the real instance untouched, and the report
    # records both facts (demo_mode for how it was read, sample for why).
    # The document cap is a visitor-upload policy (the endpoint enforces it on
    # POST /api/shipments); a bundled sample is served, not uploaded, so the
    # sample reconciler lifts that cap to its own document count. Shipping four
    # documents and then 500ing because an operator lowered MAX_DOCUMENTS to
    # three would make the built-in demo hostage to the upload limit.
    base = reconciler().settings
    rec = Reconciler(base.model_copy(update={
        "demo_mode": True,
        "max_documents": max(base.max_documents, len(uploads)),
    }))

    shipment_id, _ = await rec.create_shipment(uploads, page_texts=texts)
    await rec.wait(shipment_id, timeout=600)
    report = rec.get(shipment_id)
    if report is not None and not report.run.sample:
        report.run.sample = True
        rec.store.write(report)
    return JSONResponse(
        {
            "shipment_id": shipment_id,
            "state": report.state if report else "failed",
            "documents": len(report.documents) if report else 0,
            "demo_mode": report.run.demo_mode if report else None,
            "sample": report.run.sample if report else None,
        },
        status_code=201,
    )


@app.get("/page/{shipment_id}/{path:path}", dependencies=[Depends(require_api_key)])
def serve_page(shipment_id: str, path: str) -> FileResponse:
    """Serve normalized page images used as evidence."""
    rec = reconciler()
    # exists() is False for a malformed id, so dir() below can only be handed a
    # name that matches the run-id shape. This also stops the route from ever
    # resolving a path for a run that does not exist.
    if not rec.store.exists(shipment_id):
        raise HTTPException(404, "no such page")
    base = rec.store.dir(shipment_id)
    target = (base / path).resolve()
    # Two containment tests, not one. `is_relative_to(base)` checks the suffix,
    # but before the id was validated a shipment_id of ".." *made* base the
    # repository, so that check passed while pointing outside runs/. Anchoring
    # the second test to the runs root means the answer no longer depends on a
    # directory name the caller chose.
    if not target.is_relative_to(base) or not target.is_relative_to(rec.store.root_resolved):
        raise HTTPException(404, "no such page")
    if not target.is_file():
        raise HTTPException(404, "no such page")
    return FileResponse(target, media_type="image/jpeg")
