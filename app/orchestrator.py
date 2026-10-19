"""Orchestration: uploads -> ingest -> extract -> normalize -> compare -> report.

Concurrency is asyncio.gather over documents; the semaphore lives inside
NebiusClient. The rule that shapes this module: a 2-of-3 result is useful, a
500 error is not. A document that fails is marked failed, still rendered, and
retryable on its own — it never aborts the shipment and never cancels siblings.

The report is held in memory and written out after each document settles, so
the frontend's 1-second poll sees progress without two writers racing on disk.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from .adjudicate import (
    EMAIL_SYSTEM,
    AdjudicationCache,
    apply_verdict,
    briefing_signature,
    build_briefing_prompt,
    draft_is_usable,
    has_discrepancies,
    needs_adjudication,
    parse_verdict,
    verdict_is_usable,
    verdict_signature,
)
from .adjudicate import SYSTEM as JUDGE_SYSTEM
from .adjudicate import UNVERIFIED_SYSTEM
from .adjudicate import build_prompt as build_verdict_prompt
from .adjudicate import build_unverified_prompt
from .adjudicate import parse_unverified_note
from .adjudicate import unverified_note_is_usable
from .adjudicate import unverified_signature
from .client import (
    ExtractionClient,
    ExtractionOutcome,
    SummaryClient,
    _key_is_real,
)
from .compare import FieldObservation, compare_field
from .consensus import divergences_between, summarize
from .config import Settings, get_settings
from .correct import apply_correction, revert_corrections
from .discovery import Catalog, Selection, fetch, resolve
from .fields import FIELD_LABEL, FIELD_ORDER, FieldKey
from .ingest import PageImage, ingest
from .lineitems import LineItem, match_line_items
from .mathcheck import internal_checks
from .models import (
    Adjudication,
    Comparison,
    ConsensusResult,
    DocumentReport,
    LineItemMatch,
    NormalizedField,
    PageRef,
    Report,
    RunInfo,
    Summary,
    TokenUsage,
    UnverifiedField,
)
from .narrative_cache import NarrativeCache
from .normalize import normalize_field
from .providers import summary_endpoint, vision_endpoint
from .rules import sanity_checks
from .scoring import score_report
from .severity import apply_status
from .textlayer import PageText, load_sidecar, page_texts_for, save_sidecar
from .verify import verify_document
from starlette.concurrency import run_in_threadpool

from .store import RunStore, new_shipment_id
from .summary import build_summary_prompt, summary_is_plausible

# Every line carries a shipment id, so a production run can be traced by grepping one
# token instead of correlating timestamps by eye.
log = logging.getLogger("sdr.run")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PreparedDocument:
    """One upload, ingested and ready to extract."""

    def __init__(
        self,
        *,
        doc_id: str,
        label: str,
        filename: str,
        pages: list[PageImage],
        page_paths: list[str],
        declared_doc_type: str | None,
        page_texts: list[PageText] | None = None,
    ):
        self.doc_id = doc_id
        self.label = label
        self.filename = filename
        self.pages = pages
        self.page_paths = page_paths
        self.declared_doc_type = declared_doc_type
        # What the pages say according to something other than the vision model.
        # Empty for a photo or a scan, and that absence is a finding in itself.
        self.page_texts = page_texts or []


# ---------------------------------------------------------------------------
# Pure report assembly — no I/O, so it is directly testable
# ---------------------------------------------------------------------------


def _observations(docs: list[DocumentReport], key: FieldKey) -> list[FieldObservation]:
    obs: list[FieldObservation] = []
    for doc in docs:
        if doc.extraction is None:
            continue
        # A reviewer's correction takes precedence over what the model read,
        # while doc.extraction still holds the model's own answer unchanged.
        corrected = key in doc.overrides
        extracted = doc.overrides.get(key) or doc.extraction.fields.get(key)
        if extracted is None:
            continue
        # A quote that is not on the page is not evidence, and a value built on it
        # must not be allowed to create a disagreement between documents that
        # agree. Only `unverified` excludes -- `not_checkable` means nobody could
        # look, and dropping those would empty every report made from a photo.
        # A human assertion is not the model's claim, so corrections survive.
        check = doc.verification.get(key)
        if check is not None and check.excluded and not corrected:
            continue
        obs.append(
            FieldObservation(
                document_id=doc.id,
                document_label=doc.label,
                doc_type=effective_doc_type(doc),
                extracted=extracted,
                normalized=doc.normalized.get(key)
                or NormalizedField(ok=True, canonical=None, compare_key=None),
            )
        )
    return obs


def collect_unverified(docs: list[DocumentReport]) -> list[UnverifiedField]:
    """Every field removed from the comparison, with the quote that caused it.

    Listed beside the report rather than only subtracted from it: a field that
    quietly disappears is indistinguishable from a field no document printed, and
    that is how a transcription failure turns into a clean-looking shipment.
    """
    out: list[UnverifiedField] = []
    for doc in docs:
        if doc.extraction is None:
            continue
        for key, check in doc.verification.items():
            if not check.excluded or key in doc.overrides:
                continue
            field = doc.extraction.fields.get(key)
            if field is None or not field.present:
                continue
            out.append(UnverifiedField(
                document_id=doc.id,
                document_label=doc.label,
                field=key,
                label=FIELD_LABEL[key],
                value=field.value,
                evidence_text=field.evidence_text,
                page_index=field.page_index,
                reason=check.detail or check.tier,
                advisor_note=check.advisor_note,
                advisor_model=check.advisor_model,
                advisor_provider=check.advisor_provider,
                advisor_source=check.advisor_source,
            ))
    return out


def build_comparisons(docs: list[DocumentReport]) -> list[Comparison]:
    return [compare_field(key, _observations(docs, key)) for key in FIELD_ORDER]


def build_summary(docs: list[DocumentReport], comparisons: list[Comparison]) -> Summary:
    counts = {
        "consistent": 0,
        "suspicious": 0,
        "conflict": 0,
        "single_source": 0,
        "absent": 0,
    }
    for c in comparisons:
        counts[c.status] = counts.get(c.status, 0) + 1

    failed = [d for d in docs if d.status == "failed"]
    ok = [d for d in docs if d.status in ("ok", "cached", "partial")]

    if not docs or all(d.status == "pending" for d in docs):
        verdict = "empty"
    elif any(d.status in ("pending", "running") for d in docs):
        verdict = "running"
    elif counts["conflict"]:
        verdict = "conflicts"
    elif counts["suspicious"] or failed:
        verdict = "review"
    else:
        verdict = "clean"

    return Summary(
        total_fields=len(comparisons),
        documents_ok=len(ok),
        documents_failed=len(failed),
        verdict=verdict,  # type: ignore[arg-type]
        **counts,  # type: ignore[arg-type]
    )


def derive_state(docs: list[DocumentReport], summary: Summary) -> str:
    """Shipment-level state, independent of whether the numbers are trustworthy."""
    if not docs:
        return "failed"
    statuses = {d.status for d in docs}
    if statuses & {"pending", "running"}:
        return "running"
    if statuses == {"failed"}:
        return "failed"
    if "failed" in statuses:
        return "partial"
    return "done"


def refresh_report(report: Report, client: ExtractionClient) -> Report:
    """Recompute derived fields. Never touches report.narrative, so a failed or
    absent summary cannot alter a shipment's outcome."""
    ep = client.endpoint
    report.comparisons = build_comparisons(report.documents)
    report.summary = build_summary(report.documents, report.comparisons)
    # Derived from the same documents the comparisons were built from, in the
    # same pass, so the list of excluded fields can never disagree with the table.
    report.unverified = collect_unverified(report.documents)
    report.state = derive_state(report.documents, report.summary)  # type: ignore[assignment]
    report.run.json_schema_mode = client.json_mode  # type: ignore[assignment]
    report.run.demo_mode = client.settings.demo_mode
    report.run.model = ep.model
    report.run.vision_provider = ep.provider
    report.run.result_source = result_source(report.documents)  # type: ignore[assignment]
    report.run.live_calls = sum(1 for d in report.documents if d.source == "live_api")
    # Recomputed on every write path, so the banner a reader sees is derived from
    # the comparisons on the same page rather than from an earlier run's.
    apply_status(report)
    return report


def result_source(docs: list[DocumentReport]) -> str:
    """Provenance of the values in this report, judged per document.

    'live'            every value came from a real model response (this run or
                      a previously captured one)
    'authored'        every value came from the hand-written fixture cache
    'live+authored'   both, so the honest answer is neither of the above
    'none'            nothing extracted

    A cached *authored* answer is deliberately NOT counted as live: reusing a
    fixture is not evidence that the model can read the document.
    """
    done = [d for d in docs if d.extraction is not None]
    if not done:
        return "none"
    sources = {d.source for d in done}
    model_backed = bool(sources & {"live_api", "cached_live"})
    authored = "cached_authored" in sources
    if model_backed and authored:
        return "live+authored"
    if model_backed:
        return "live"
    return "authored"


def effective_doc_type(doc: DocumentReport) -> str:
    """The document type the comparison engine should treat this doc as.

    The user's declaration wins over the model's guess. Live-verified: on a page
    whose heading plainly reads PACKING LIST, the model reported
    doc_type=commercial_invoice. That guess is kept in `detected_doc_type` as a
    signal (a mismatch is worth surfacing), but it must not silently change
    which fields are treated as expected-to-be-present, or a genuinely absent
    HS code on a packing list starts looking like a problem.
    """
    return doc.declared_doc_type or doc.detected_doc_type or "other"


def _sum_usage(attempts: list) -> TokenUsage:
    """Total tokens reported across the calls that actually happened.

    Summed over attempts rather than taken from the last one, because the ladder
    really does bill each rung. A cached document contributes zeroes.
    """
    prompt = completion = total = 0
    for attempt in attempts:
        usage = attempt.usage or {}
        prompt += int(usage.get("prompt", 0) or 0)
        completion += int(usage.get("completion", 0) or 0)
        total += int(usage.get("total", 0) or 0)
    return TokenUsage(prompt=prompt, completion=completion, total=total)


def _usage_of(usage: dict | None) -> TokenUsage:
    return TokenUsage(
        prompt=int((usage or {}).get("prompt", 0) or 0),
        completion=int((usage or {}).get("completion", 0) or 0),
        total=int((usage or {}).get("total", 0) or 0),
    )


def apply_outcome(target: DocumentReport, res: ExtractionOutcome | Exception) -> None:
    if isinstance(res, Exception):
        target.status = "failed"
        target.error = f"{type(res).__name__}: {res}"
        target.normalized = {}
        return

    target.attempts = len(res.attempts)
    target.timings_ms = res.total_latency_ms
    target.used_cache = res.used_cache
    target.used_fallback_schema = res.used_fallback_schema
    target.source = res.source
    # Per document, not just per run: the answer has to name the host and model
    # that produced *this* row.
    target.provider = res.provider
    target.model = res.model
    target.usage = _sum_usage(res.attempts)
    target.raw_files = list(res.raw_files)

    if res.response is None:
        target.status = "failed"
        target.error = res.error or "extraction failed"
        target.normalized = {}
        return

    target.extraction = res.response
    target.detected_doc_type = res.response.doc_type
    if target.declared_doc_type and res.response.doc_type != target.declared_doc_type:
        # Recorded, not fatal: the declaration still governs comparison.
        target.doc_type_disagrees = True
    target.normalized = {
        key: normalize_field(key, res.response.fields[key])
        for key in FIELD_ORDER
        if key in res.response.fields
    }
    target.status = "partial" if res.dropped_fields else ("cached" if res.used_cache else "ok")
    # Issues are diagnostics, not a failure. Surface them only where they
    # explain a partial result, so a clean run does not look alarming.
    target.error = "; ".join(res.issues[:4]) if res.dropped_fields else None


def load_pages(run_dir: Path, doc: DocumentReport) -> list[PageImage]:
    """Re-read persisted page JPEGs so a retry needs no fresh upload."""
    pages: list[PageImage] = []
    for ref in doc.pages:
        path = run_dir / ref.image_path
        if path.is_file():
            pages.append(
                PageImage(
                    index=ref.index,
                    jpeg=path.read_bytes(),
                    sha256=ref.sha256,
                    width=ref.width,
                    height=ref.height,
                )
            )
    return pages


def short_label(filename: str, index: int) -> str:
    name = Path(filename).name
    stem = name[:32] + "…" if len(name) > 35 else name
    return stem or f"document {index}"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Reconciler:
    def __init__(self, settings: Settings | None = None, store: RunStore | None = None):
        self.settings = settings or get_settings()
        self.store = store or RunStore(self.settings.runs_dir)
        self.client = ExtractionClient(self.settings)
        self.summary_client = SummaryClient(self.settings)
        # Bounded, oldest-evicted. Every report was also written to disk, so an
        # eviction costs a re-read, never data -- while an unbounded dict means a
        # long-lived instance is holding every shipment it has ever served.
        self._reports: OrderedDict[str, Report] = OrderedDict()
        self._purged = self.store.purge_older_than(self.settings.run_ttl_hours)
        if self._purged:
            log.info("purged %d run(s) older than %dh", len(self._purged),
                     self.settings.run_ttl_hours)
        self._tasks: dict[str, asyncio.Task] = {}
        # Discovery results, keyed by host so two roles on one host share a
        # catalog. Both are best-effort: an unreachable host leaves the
        # configured model alone.
        self._catalogs: dict[str, Catalog] = {}
        self._selections: dict[str, Selection] = {}
        # Shipment id -> live SSE listeners. Populated by the report store's
        # on_write hook, so every existing settle point publishes for free.
        self._listeners: dict[str, set[asyncio.Queue]] = {}
        # Shipment id -> monotonic start, so stage events can report elapsed
        # time without re-parsing the report's ISO timestamps per event.
        self._run_started: dict[str, float] = {}
        # One client per secondary provider, built lazily: constructing one runs
        # a capability probe, so a fresh client per document would double the
        # spend consensus is already paying for.
        self._consensus_clients: dict[str, ExtractionClient] = {}
        self.store.on_write = self._publish

    # ------------------------------------------------------------------
    # Progress fan-out
    # ------------------------------------------------------------------

    def _fan_out(self, shipment_id: str, item: object) -> None:
        """Hand one item to every live listener, drop-oldest rather than block.

        A stalled client must never be able to stall the run that is producing
        it, so a full queue loses its oldest entry instead of blocking.
        """
        for queue in tuple(self._listeners.get(shipment_id, ())):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(item)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _publish(self, report: Report) -> None:
        self._fan_out(report.shipment_id, report)

    def _emit_stage(self, shipment_id: str, *, document_id: str,
                    document_label: str, stage: str, provider: str = "",
                    model: str = "") -> None:
        """Publish one per-document stage transition to live SSE listeners.

        A small dict, distinguishable from a `Report` by the SSE endpoint. It is
        progress, not truth: the report events carry the decisions, and a stage
        event can be dropped under backpressure without any run losing anything.
        """
        started = self._run_started.get(shipment_id)
        elapsed_ms = int((time.monotonic() - started) * 1000) if started else 0
        self._fan_out(shipment_id, {
            "type": "stage",
            "shipment_id": shipment_id,
            "document_id": document_id,
            "document_label": document_label,
            "stage": stage,
            "provider": provider,
            "model": model,
            "elapsed_ms": elapsed_ms,
        })

    def subscribe(self, shipment_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._listeners.setdefault(shipment_id, set()).add(queue)
        # Replay current state at once: a client arriving mid-run should see the
        # same picture an immediate poll would.
        current = self._reports.get(shipment_id) or self.store.read(shipment_id)
        if current is not None:
            queue.put_nowait(current)
        return queue

    def unsubscribe(self, shipment_id: str, queue: asyncio.Queue) -> None:
        bucket = self._listeners.get(shipment_id)
        if not bucket:
            return
        bucket.discard(queue)
        if not bucket:
            self._listeners.pop(shipment_id, None)

    # Settings the GUI may change on a running process. Anything path- or
    # limit-shaped stays env-only: re-reading runs_dir mid-run would strand a
    # shipment's evidence between two directories.
    CONFIGURABLE = frozenset(
        {
            "nvidia_api_key",
            "nebius_api_key",
            "vision_provider",
            "nvidia_vision_model",
            "nebius_vision_model",
            "nebius_summary_model",
            "demo_mode",
            "enable_summary",
            "enable_adjudication",
            "enable_briefing",
            "consensus",
        }
    )

    def _text_file(self, shipment_id: str, doc_id: str) -> Path:
        """Where a document's recovered page text is kept for this run.

        Persisted rather than recomputed: a retry has the upload on disk but
        should not have to re-parse a PDF, and verification has to give the same
        answer after a reload as it did on the run that produced the report.
        """
        return self.store.dir(shipment_id) / "text" / f"{doc_id}.json"

    def reconfigure(self, updates: dict[str, object]) -> None:
        """Apply runtime overrides and rebuild the clients that captured the old ones.

        Rebuilding is not optional. The endpoint (which carries the key),
        ``json_mode`` and the capability probe are all resolved in
        ``ChatClient.__init__``, so mutating settings alone would leave live
        calls using the previous key.
        """
        unknown = set(updates) - self.CONFIGURABLE
        if unknown:
            raise ValueError(
                "not configurable at runtime: " + ", ".join(sorted(unknown))
            )

        for name, value in updates.items():
            setattr(self.settings, name, value)

        # An in-flight shipment looks its client up per document, so it may
        # straddle both configurations. Each run already stamps the model and
        # demo_mode it used, so no report is ever mislabelled after the fact.
        self.client = ExtractionClient(self.settings)
        self.summary_client = SummaryClient(self.settings)

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    def model_selections(self) -> list[dict]:
        """What discovery decided, per role. Reads state; makes no calls."""
        return [
            {
                "role": s.role,
                "provider": s.provider,
                "host": s.host,
                "requested": s.requested,
                "chosen": s.chosen,
                "status": s.status,
                "note": s.note,
                "available": list(s.available),
            }
            for s in (self._selections.get("vision"), self._selections.get("summary"))
            if s
        ]

    async def refresh_models(self, *, force: bool = False, transport=None) -> list[Selection]:
        """Check the configured models against what each host actually lists.

        Called before a run extracts rather than at import time. It is the only
        thing that catches a model retiring underneath the app — which already
        happened once here, see providers.py — and it must stay out of DEMO_MODE,
        where there is no key and nothing to discover. TTL-cached per host so a
        busy server asks once a quarter-hour, not once a shipment.

        Never raises. An unreachable host, a bad key or an odd payload leaves the
        configured model in place with the reason recorded, because discovery
        improving the odds is not worth trading certainty for.
        """
        settings = self.settings
        if settings.demo_mode or not settings.model_discovery:
            return list(self._selections.values())

        changed = False
        for role, endpoint in (("vision", vision_endpoint(settings)),
                               ("summary", summary_endpoint(settings))):
            if not _key_is_real(endpoint.api_key):
                self._selections[role] = Selection(
                    role=role, provider=endpoint.provider, host=endpoint.host,
                    requested=endpoint.model, chosen=endpoint.model,
                    status="unverified",
                    note="no key for this host, so the model was not verified",
                )
                continue

            catalog = self._catalogs.get(endpoint.host)
            stale = catalog is None or (
                time.monotonic() - catalog.fetched_at
            ) >= settings.model_discovery_ttl_seconds
            if stale or force:
                try:
                    catalog = await fetch(endpoint, transport=transport)
                except Exception as exc:  # noqa: BLE001 - advisory check, never sinks a run
                    # DiscoveryError is the expected shape; a payload this side
                    # has never seen could raise anything. The rule for the whole
                    # module is that a check that merely improves the odds must
                    # not be able to abort a shipment.
                    self._selections[role] = Selection(
                        role=role, provider=endpoint.provider, host=endpoint.host,
                        requested=endpoint.model, chosen=endpoint.model,
                        status="unverified", note=f"{type(exc).__name__}: {exc}"[:300],
                    )
                    continue
                self._catalogs[endpoint.host] = catalog

            allow = (
                settings.vision_model_allow
                if role == "vision"
                else settings.summary_model_allow
            )
            selection = resolve(
                role=role, provider=endpoint.provider, host=endpoint.host,
                requested=endpoint.model, catalog=catalog.models, allow=allow,
            )
            self._selections[role] = selection
            if selection.status == "discovered" and selection.chosen != endpoint.model:
                self._adopt(role, endpoint.provider, selection.chosen)
                changed = True

        if changed:
            # The endpoint carries the model, so adopting one means rebuilding
            # the clients that captured the old one — the same rule reconfigure
            # follows, for the same reason.
            self.client = ExtractionClient(self.settings)
            self.summary_client = SummaryClient(self.settings)
        return list(self._selections.values())

    def _adopt(self, role: str, provider: str, model: str) -> None:
        field = {
            ("vision", "nvidia"): "nvidia_vision_model",
            ("vision", "nebius"): "nebius_vision_model",
            ("summary", "nebius"): "nebius_summary_model",
        }.get((role, provider))
        if field:
            setattr(self.settings, field, model)

    # ------------------------------------------------------------------

    async def create_shipment(
        self,
        uploads: list[tuple[str, str | None, bytes]],
        page_texts: dict[str, list[PageText]] | None = None,
    ) -> tuple[str, Report]:
        """uploads: (filename, declared_doc_type_or_None, bytes), max 5.

        `page_texts` supplies a text source for a document that cannot carry one
        itself. Only the bundled fixtures need it: their pages are degraded
        rasters, and the text they were rendered from is kept beside them. Passing
        it in rather than having the pipeline go looking for it keeps the
        difference between "this document states its own text" and "we generated
        it" explicit at the call site.
        """
        if not uploads:
            raise ValueError("no documents supplied")
        if len(uploads) > self.settings.max_documents:
            raise ValueError(
                f"at most {self.settings.max_documents} documents per shipment "
                f"(got {len(uploads)})"
            )

        shipment_id = new_shipment_id()
        self.store.create(shipment_id)

        prepared: list[PreparedDocument] = []
        rejected: list[tuple[str, str, str, str | None]] = []

        for i, (filename, declared, data) in enumerate(uploads, start=1):
            doc_id = f"doc{i}"
            label = short_label(filename, i)
            if len(data) > self.settings.max_upload_bytes:
                rejected.append((label, filename, "file exceeds the size limit", declared))
                continue
            # Rasterising a PDF and decoding a photo are CPU-bound C calls, and
            # writing the evidence is disk IO. Both used to run on the event
            # loop, where one 6-page scan stalls every other shipment's
            # in-flight request for its duration.
            await run_in_threadpool(self.store.save_upload, shipment_id, filename, data)
            try:
                pages = await run_in_threadpool(
                    ingest,
                    filename,
                    data,
                    long_edge=self.settings.image_long_edge,
                    quality=self.settings.jpeg_quality,
                    max_pages=self.settings.max_pages_per_document,
                )
            except ValueError as exc:
                log.info("shipment %s: rejected %s: %s", shipment_id, filename, exc)
                rejected.append((label, filename, str(exc), declared))
                continue

            page_paths = await run_in_threadpool(
                self.store.save_pages, shipment_id, doc_id, pages
            )
            # Recover what the pages say from anything other than the model, and
            # keep it: a later retry has the upload on disk but no reason to
            # re-parse it, and verification has to survive the reload.
            texts = await run_in_threadpool(
                page_texts_for, filename, data,
                max_pages=self.settings.max_pages_per_document,
            )
            if not texts and page_texts:
                texts = page_texts.get(Path(filename).name) or []
            if texts:
                await run_in_threadpool(
                    save_sidecar, self._text_file(shipment_id, doc_id), texts
                )
            prepared.append(
                PreparedDocument(
                    doc_id=doc_id,
                    label=label,
                    filename=filename,
                    pages=pages,
                    page_paths=page_paths,
                    declared_doc_type=declared,
                    page_texts=texts,
                )
            )

        documents = [
            DocumentReport(
                id=p.doc_id,
                label=p.label,
                filename=p.filename,
                declared_doc_type=p.declared_doc_type,  # type: ignore[arg-type]
                status="pending",
                pages=[
                    PageRef(
                        index=pg.index,
                        image_path=path,
                        sha256=pg.sha256,
                        width=pg.width,
                        height=pg.height,
                    )
                    for pg, path in zip(p.pages, p.page_paths)
                ],
            )
            for p in prepared
        ]
        # A file we could not even ingest still gets a row, so the user sees
        # which upload was rejected and why rather than a silent drop.
        for label, filename, err, declared in rejected:
            documents.append(
                DocumentReport(
                    id=f"bad{len(documents) + 1}",
                    label=label,
                    filename=filename,
                    declared_doc_type=declared,  # type: ignore[arg-type]
                    status="failed",
                    error=err,
                )
            )

        report = Report(
            shipment_id=shipment_id,
            created_at=_now(),
            updated_at=_now(),
            documents=documents,
            run=RunInfo(
                model=self.client.endpoint.model,
                vision_provider=self.client.endpoint.provider,
                summary_model=self.summary_client.endpoint.model,
                summary_provider=self.summary_client.endpoint.provider,
                demo_mode=self.settings.demo_mode,
                json_schema_mode=self.client.json_mode,  # type: ignore[arg-type]
                usd_per_mtok=self.settings.usd_per_mtok,
            ),
        )
        self._remember(shipment_id, report)
        refresh_report(report, self.client)
        self.store.write(report)
        self._run_started[shipment_id] = time.monotonic()
        for p in prepared:
            self._emit_stage(shipment_id, document_id=p.doc_id, document_label=p.label,
                             stage="queued")

        if prepared:
            task = asyncio.create_task(self._process(shipment_id, prepared))
            self._tasks[shipment_id] = task
            task.add_done_callback(lambda done: self._task_done(shipment_id, done))
        else:
            log.warning("shipment %s: every document was rejected", shipment_id)
        return shipment_id, report

    def _task_done(self, shipment_id: str, task: asyncio.Task) -> None:
        """Surface whatever killed a run.

        A bare create_task swallows exceptions into "Task exception was never
        retrieved", which arrives on stderr some time later with no shipment id
        attached -- so a run that died mid-flight looked like a run that was
        still running. The report on disk is the reader's source of truth; this
        only makes sure the failure is attributable.
        """
        self._tasks.pop(shipment_id, None)
        if task.cancelled():
            log.warning("shipment %s: processing cancelled", shipment_id)
            return
        error = task.exception()
        if error is not None:
            log.error("shipment %s: processing crashed: %s: %s",
                      shipment_id, type(error).__name__, error)

    def _remember(self, shipment_id: str, report: Report) -> None:
        self._reports[shipment_id] = report
        self._reports.move_to_end(shipment_id)
        limit = max(self.settings.max_reports_in_memory, 1)
        while len(self._reports) > limit:
            dropped, _ = self._reports.popitem(last=False)
            log.debug("evicted report %s from memory", dropped)

    def _forget(self, shipment_id: str) -> None:
        self._reports.pop(shipment_id, None)

    # ------------------------------------------------------------------

    async def _process(self, shipment_id: str, prepared: list[PreparedDocument]) -> None:
        report = self._reports[shipment_id]
        by_id = {d.id: d for d in report.documents}
        # One client is shared by every shipment, so the raw-output directory
        # has to be chosen per call — a constructor argument would send two
        # concurrent runs' evidence to whichever created it first.
        raw_dir = self.store.dir(shipment_id) / "raw"

        # Confirm the models before spending anything on them. If a host no
        # longer serves the configured name, refresh_models adopts one it does
        # and rebuilds the clients, so the run's stamps have to be re-read from
        # the endpoints rather than left at whatever create_shipment captured.
        await self.refresh_models()
        report.run.model = self.client.endpoint.model
        report.run.vision_provider = self.client.endpoint.provider
        report.run.summary_model = self.summary_client.endpoint.model
        report.run.summary_provider = self.summary_client.endpoint.provider
        vision = self._selections.get("vision")
        if vision is not None:
            report.run.model_selection = vision.status
            report.run.model_note = vision.note or (
                f"confirmed on {vision.host}" if vision.status == "configured" else ""
            )
        self.store.write(report)

        async def one(p: PreparedDocument) -> None:
            doc = by_id[p.doc_id]
            doc.status = "running"
            self._emit_stage(shipment_id, document_id=doc.id, document_label=doc.label,
                             stage="reading", provider=self.client.endpoint.provider,
                             model=self.client.endpoint.model)
            try:
                res: ExtractionOutcome | Exception = await self.client.extract_document(
                    document_id=p.doc_id,
                    pages=p.pages,
                    declared_doc_type=p.declared_doc_type,
                    raw_dir=raw_dir,
                )
            except Exception as exc:  # noqa: BLE001 - one doc must not kill the run
                res = exc
            apply_outcome(doc, res)
            # Before anything reads the quotes. Exclusion itself happens in
            # _observations, so the comparisons published below are already the
            # filtered ones and no progress state ever shows an unverified field
            # as though it had been reconciled.
            if doc.extraction is not None:
                self._emit_stage(shipment_id, document_id=doc.id,
                                 document_label=doc.label, stage="verifying",
                                 provider=doc.provider, model=doc.model)
                doc.verification = verify_document(
                    doc.extraction, p.page_texts,
                    threshold=self.settings.evidence_min_ratio,
                )
            if (
                self.settings.consensus
                and not isinstance(res, Exception)
                and doc.extraction is not None
            ):
                self._emit_stage(shipment_id, document_id=doc.id,
                                 document_label=doc.label, stage="comparing",
                                 provider=doc.provider, model=doc.model)
                await self._run_consensus(doc, p.pages, raw_dir)
            # Publish progress immediately: with three documents the user
            # watching the poll sees each row land as it finishes.
            refresh_report(report, self.client)
            self._emit_stage(
                shipment_id, document_id=doc.id, document_label=doc.label,
                stage="done" if doc.status in ("ok", "cached") else "failed",
                provider=doc.provider, model=doc.model,
            )
            self.store.write(report)

        await asyncio.gather(*(one(p) for p in prepared))

        # Sanity before risk, so a risk level that ignored the cross-field
        # findings would be understating. Both are pure functions of the
        # comparisons above, so re-running them cannot disagree with anything.
        # The RiskNote carries the score and its contributions, and the level is
        # read off the score.
        report.sanity = sanity_checks(report)
        report.risk = score_report(report)
        # The within-document arithmetic that needs no goods rows -- a document's
        # own net against its own gross -- runs on every shipment. The three rules
        # that do need rows are recorded as not_run here and replaced with real
        # results below if the opt-in read happens, so an absent check is always
        # distinguishable from a passed one.
        report.internal_checks = internal_checks(report, {}, settings=self.settings)
        # Last, because it reads everything above it. Nothing in the severity
        # stage re-decides a value; it only ranks what has already been found.
        apply_status(report)
        self.store.write(report)

        # After the write above, so the goods lines can only ever be read
        # alongside a finished scalar report, never instead of it.
        if self.settings.enable_line_items:
            await self._read_line_items(report, prepared, raw_dir)

        await self._adjudicate(report)
        await self._advise_unverified(report)
        await self._write_briefing(report)
        await self._write_summary(report)
        self.store.write(report)
        self._tasks.pop(shipment_id, None)
        self._run_started.pop(shipment_id, None)

    # ------------------------------------------------------------------
    # Opt-in goods lines. Runs last; can change nothing else.
    # ------------------------------------------------------------------

    async def _read_line_items(
        self, report: Report, prepared: list[PreparedDocument], raw_dir: Path
    ) -> None:
        """Transcribe the goods table on each document and match the rows.

        Called only when ENABLE_LINE_ITEMS is on, and only once the scalar
        report has already been written. Nothing here is read by the comparison
        engine, so every failure path leaves the verdicts exactly as they were —
        the worst outcome is a section that says it could not be filled.
        """
        section = report.line_items
        section.enabled = True
        section.status = "pending"
        section.model = self.client.endpoint.model
        section.provider = self.client.endpoint.provider
        self.store.write(report)

        sets: dict[str, list[LineItem]] = {}
        by_doc: dict[str, list[LineItem]] = {}
        sources: list[str] = []
        # Reason -> the documents it applies to. Four documents that all skipped
        # for the same reason is one sentence, not four, and a reviewer reading a
        # wall of repeated text learns less than one reading a named list.
        problems: dict[str, list[str]] = {}

        def note(label: str, message: str) -> None:
            problems.setdefault(message, []).append(label)

        call_failures = 0
        usage = {"prompt": 0, "completion": 0, "total": 0}
        started = time.monotonic()

        for p in prepared:
            doc = next((d for d in report.documents if d.id == p.doc_id), None)
            if doc is None or doc.extraction is None:
                # A document that failed to extract has no trusted pages to read
                # a second time; re-asking would spend a call on a known failure.
                continue
            # Two uploads with the same filename get the same label, and a label
            # collision here would silently merge two documents' rows into one.
            label = p.label
            if label in sets:
                label = f"{label} ({p.doc_id})"
            try:
                out = await self.client.extract_line_items(
                    document_id=p.doc_id, pages=p.pages, raw_dir=raw_dir
                )
            except Exception as exc:  # noqa: BLE001 - one line read must not sink the section
                note(label, f"{type(exc).__name__}: {str(exc)[:180]}")
                call_failures += 1
                continue
            section.raw_files.extend(out.raw_files)
            for k, v in (out.usage or {}).items():
                usage[k] = usage.get(k, 0) + int(v or 0)
            sets[label] = [
                LineItem(
                    raw_description=i.description, raw_sku=i.sku, raw_quantity=i.quantity,
                    raw_unit_price=i.unit_price, raw_line_total=i.line_total,
                    raw_gross_weight=i.gross_weight, raw_net_weight=i.net_weight,
                    raw_evidence_text=i.evidence_text, page_index=i.page_index,
                )
                for i in out.items
            ]
            by_doc[p.doc_id] = sets[label]
            section.item_counts[label] = len(out.items)
            sources.append(out.source)
            if out.status == "failed":
                call_failures += 1
            if out.status != "ok":
                note(label, out.error or out.status)
            if out.dropped_items:
                note(label, f"{out.dropped_items} row(s) unreadable and dropped")

        section.usage = TokenUsage(**usage)
        section.latency_ms = int((time.monotonic() - started) * 1000)
        section.source = (
            "live_api" if "live_api" in sources
            else "cached_live" if "cached_live" in sources
            else "cached_authored" if "cached_authored" in sources
            else "none"
        )

        matched = match_line_items(sets)
        section.documents = matched.documents
        section.matches = [
            LineItemMatch(
                key=m.key,
                label=m.label,
                documents=list(m.documents),
                quantities=dict(m.quantities),
                status=m.status,
                detail=m.detail,
            )
            for m in matched.matches
        ]
        section.matched = matched.matched
        section.conflicts = matched.conflicts
        section.uncheckable = matched.uncheckable

        # Now that the rows exist, the three line-dependent rules can actually
        # run. Recomputed from the same rows the matcher used, so the arithmetic
        # and the line matching can never disagree about what the document said.
        report.internal_checks = internal_checks(report, by_doc, settings=self.settings)

        reason = "; ".join(
            f"{', '.join(labels)}: {message}" for message, labels in problems.items()
        )
        if matched.skipped_reason:
            reason = f"{reason}; {matched.skipped_reason}" if reason else matched.skipped_reason

        # "skipped" and "failed" are different claims. DEMO_MODE with no captured
        # goods lines skipped; a transport error failed. Collapsing them would
        # make a keyless demo look like a broken API.
        if not matched.usable:
            section.status = "failed" if call_failures else "skipped"
            section.error = reason if call_failures else None
        elif problems:
            section.status = "partial"
        else:
            section.status = "ok"
        section.note = reason
        # After the section's own status is settled: the banner only reads goods
        # lines that were actually recorded, and here they finally are.
        apply_status(report)
        self.store.write(report)

    # ------------------------------------------------------------------
    # Optional AI summary. Never allowed to affect state or verdict.
    # ------------------------------------------------------------------

    async def _write_summary(self, report: Report) -> None:
        settings = self.settings
        narrative = report.narrative

        if not settings.enable_summary:
            narrative.status = "skipped"
            narrative.error = "disabled via ENABLE_SUMMARY"
            self.store.write(report)
            return
        if not report.comparisons or report.summary.total_fields == 0:
            narrative.status = "skipped"
            narrative.error = "nothing to summarise"
            self.store.write(report)
            return

        cache = NarrativeCache(settings)
        endpoint = self.summary_client.endpoint

        cached = cache.get(report, endpoint.model)
        if cached is not None:
            # A hand-written narrative is a demo prop, not model output. Only
            # replay it when we are actually in DEMO_MODE; in live mode we
            # regenerate so the UI never claims a model wrote our fixture text.
            if cached["authored"] and not settings.demo_mode:
                pass
            else:
                narrative.status = "cached"
                narrative.text = cached["text"]
                narrative.model = cached["model"] or endpoint.model
                narrative.provider = cached["provider"] or endpoint.provider
                narrative.source = "authored" if cached["authored"] else "cached"
                narrative.generated_at = report.updated_at
                self.store.write(report)
                return

        if settings.demo_mode:
            narrative.status = "skipped"
            narrative.error = "DEMO_MODE: no cached narrative for these verdicts"
            self.store.write(report)
            return

        if not _key_is_real(endpoint.api_key):
            narrative.status = "skipped"
            narrative.error = f"{endpoint.provider.upper()}_API_KEY not set"
            self.store.write(report)
            return

        started = time.monotonic()
        try:
            text, usage = await self.summary_client.summarize(build_summary_prompt(report))
        except Exception as exc:  # noqa: BLE001 - summary failure must not sink a run
            narrative.status = "failed"
            narrative.error = f"{type(exc).__name__}: {exc}"[:300]
            narrative.model = endpoint.model
            narrative.provider = endpoint.provider
            narrative.latency_ms = int((time.monotonic() - started) * 1000)
            self.store.write(report)
            return

        latency_ms = int((time.monotonic() - started) * 1000)
        if not summary_is_plausible(text):
            narrative.status = "failed"
            narrative.error = "model returned implausible summary text (rejected)"
            narrative.model = endpoint.model
            narrative.provider = endpoint.provider
            narrative.latency_ms = latency_ms
            self.store.write(report)
            return

        narrative.status = "ok"
        narrative.text = text.strip()
        narrative.model = endpoint.model
        narrative.provider = endpoint.provider
        narrative.source = "generated"
        narrative.generated_at = _now()
        narrative.error = None
        narrative.latency_ms = latency_ms
        narrative.usage = TokenUsage(
            prompt=int((usage or {}).get("prompt", 0) or 0),
            completion=int((usage or {}).get("completion", 0) or 0),
            total=int((usage or {}).get("total", 0) or 0),
        )
        try:
            cache.put(report, model=endpoint.model, provider=endpoint.provider, text=text.strip())
        except OSError:
            pass
        self.store.write(report)

    # ------------------------------------------------------------------
    # Ambiguity: a second opinion, and a letter to send.
    # ------------------------------------------------------------------

    async def _adjudicate(self, report: Report) -> None:
        """Ask Nebius about comparisons the engine itself called ambiguous.

        Additive, and never fatal. `status` is untouched: a model that dislikes
        a tolerance outcome or an ISO 6346 result is recorded beside it, because
        only the deterministic answer can be audited and reproduced.
        """
        targets = [c for c in report.comparisons if needs_adjudication(c)]
        if not targets:
            return

        settings = self.settings
        endpoint = self.summary_client.endpoint
        blocked = None
        if not settings.enable_adjudication:
            blocked = "disabled via ENABLE_ADJUDICATION"
        elif settings.demo_mode:
            # No authored verdicts exist: a hand-written "opinion" replayed as
            # though a model formed it would be exactly the kind of fake result
            # this project promises not to produce.
            blocked = "DEMO_MODE: no captured adjudication exists for this comparison"
        elif not _key_is_real(endpoint.api_key):
            blocked = f"{endpoint.provider.upper()}_API_KEY not set"

        if blocked:
            for comp in targets:
                comp.adjudication = Adjudication(status="skipped", error=blocked)
            return

        cache = AdjudicationCache(settings.cache_dir)
        for comp in targets:
            signature = verdict_signature(comp)
            adjudication = Adjudication()
            comp.adjudication = adjudication

            cached = cache.get(signature)
            verdict = cached.get("verdict") if cached else None
            reason = verdict_is_usable(verdict)
            if cached and reason is None:
                apply_verdict(
                    adjudication, verdict,
                    model=cached.get("model", "") or endpoint.model,
                    provider=cached.get("provider", "") or endpoint.provider,
                    source="cached", usage=TokenUsage(), latency_ms=0,
                )
                continue

            # Built outside the try: a defect in prompt construction is a bug,
            # and recording it as "the model failed" would hide it exactly the
            # way B4 hides background-task errors.
            prompt = build_verdict_prompt(comp)
            started = time.monotonic()
            try:
                raw, usage = await self.summary_client.judge(JUDGE_SYSTEM, prompt)
            except Exception as exc:  # noqa: BLE001 - one opinion must not sink a run
                adjudication.status = "failed"
                adjudication.error = f"{type(exc).__name__}: {exc}"[:300]
                adjudication.model = endpoint.model
                adjudication.provider = endpoint.provider
                continue

            adjudication.model = endpoint.model
            adjudication.provider = endpoint.provider
            adjudication.latency_ms = int((time.monotonic() - started) * 1000)
            adjudication.usage = _usage_of(usage)

            parsed = parse_verdict(raw)
            reason = verdict_is_usable(parsed)
            if reason is not None:
                # The call was made and paid for; say so rather than hiding it.
                adjudication.status = "failed"
                adjudication.error = reason
                continue

            cache.put(signature, model=endpoint.model, provider=endpoint.provider,
                      raw=raw, verdict=parsed)
            apply_verdict(
                adjudication, parsed, model=endpoint.model, provider=endpoint.provider,
                source="generated", usage=adjudication.usage,
                latency_ms=adjudication.latency_ms,
            )
        self.store.write(report)

    async def _advise_unverified(self, report: Report) -> None:
        """One short advisory note per value the evidence check withdrew.

        Same contract as `_adjudicate`: additive, never fatal, never a second
        decision. The field stays withdrawn whatever the model says — the note
        only offers the most likely cause of the mismatch, and on any failure it
        stays empty rather than showing half an answer.

        A captured note is real model output, so it replays from the cache in
        exactly the way a captured summary does; only the *live call* is gated
        on demo mode and the key. When nothing is captured and nothing can be
        called, the note stays empty — a hand-written stand-in replayed as
        though a model formed it would be the exact fake result this app
        refuses.
        """
        if not report.unverified:
            return
        settings = self.settings
        endpoint = self.summary_client.endpoint
        if not settings.enable_adjudication:
            return  # the advisor is off; silence, not a marker

        cache = AdjudicationCache(settings.cache_dir, subdir="unverified_notes")
        can_call = not settings.demo_mode and _key_is_real(endpoint.api_key)
        docs_by_id = {d.id: d for d in report.documents}
        for item in report.unverified:
            doc = docs_by_id.get(item.document_id)
            if doc is None or item.field not in doc.verification:
                continue
            check = doc.verification[item.field]
            signature = unverified_signature(
                item.field, item.value, item.evidence_text, item.reason
            )

            cached = cache.get(signature)
            note = cached.get("note") if cached else None
            if isinstance(note, dict) and unverified_note_is_usable(note) is None:
                explanation = note.get("explanation")
                if isinstance(explanation, str):
                    check.advisor_note = explanation
                    check.advisor_model = (cached or {}).get("model") or endpoint.model
                    check.advisor_provider = (cached or {}).get("provider") or endpoint.provider
                    check.advisor_source = "cached"
                    continue

            if not can_call:
                continue

            prompt = build_unverified_prompt(
                item.field, item.label, item.value, item.evidence_text,
                item.page_index, item.reason,
            )
            try:
                raw, _usage = await self.summary_client.judge(
                    UNVERIFIED_SYSTEM, prompt, max_tokens=120, temperature=0.2,
                )
            except Exception:  # noqa: BLE001 - a note is a convenience
                continue

            parsed = parse_unverified_note(raw)
            if parsed is None or unverified_note_is_usable(parsed) is not None:
                continue
            check.advisor_note = parsed["explanation"]
            check.advisor_model = endpoint.model
            check.advisor_provider = endpoint.provider
            check.advisor_source = "generated"
            cache.put(signature, model=endpoint.model, provider=endpoint.provider,
                      raw=raw, note=parsed, _provenance="LIVE API")

        # Rebuild so the notes land in the report shape renderers read.
        report.unverified = collect_unverified(report.documents)

    async def _write_briefing(self, report: Report) -> None:
        """Draft the supplier email, from verdicts the engine already decided.

        Skipped when everything agrees: there is nothing to ask about, and a
        model asked to write a discrepancy letter about a clean shipment will
        invent one.
        """
        briefing = report.briefing
        settings = self.settings
        endpoint = self.summary_client.endpoint

        if not settings.enable_briefing:
            briefing.status = "skipped"
            briefing.error = "disabled via ENABLE_BRIEFING"
            return
        if not has_discrepancies(report):
            briefing.status = "skipped"
            briefing.error = "no discrepancies to raise"
            return
        if settings.demo_mode:
            briefing.status = "skipped"
            briefing.error = "DEMO_MODE: no captured draft for these verdicts"
            return
        if not _key_is_real(endpoint.api_key):
            briefing.status = "skipped"
            briefing.error = f"{endpoint.provider.upper()}_API_KEY not set"
            return

        cache = AdjudicationCache(settings.cache_dir, subdir="briefings")
        signature = briefing_signature(report)
        cached = cache.get(signature)
        if cached and draft_is_usable(cached.get("raw_text", "")) is None:
            briefing.status = "ok"
            briefing.text = cached["raw_text"].strip()
            briefing.model = cached.get("model", "") or endpoint.model
            briefing.provider = cached.get("provider", "") or endpoint.provider
            briefing.source = "cached"
            return

        prompt = build_briefing_prompt(report)
        started = time.monotonic()
        try:
            raw, usage = await self.summary_client.judge(
                EMAIL_SYSTEM, prompt, max_tokens=500, temperature=0.4,
            )
        except Exception as exc:  # noqa: BLE001 - a draft is a convenience
            briefing.status = "failed"
            briefing.error = f"{type(exc).__name__}: {exc}"[:300]
            briefing.model = endpoint.model
            briefing.provider = endpoint.provider
            return

        briefing.model = endpoint.model
        briefing.provider = endpoint.provider
        briefing.latency_ms = int((time.monotonic() - started) * 1000)
        briefing.usage = _usage_of(usage)

        reason = draft_is_usable(raw)
        if reason is not None:
            briefing.status = "failed"
            briefing.error = reason
            return

        briefing.status = "ok"
        briefing.text = raw.strip()
        briefing.source = "generated"
        briefing.error = None
        cache.put(signature, model=endpoint.model, provider=endpoint.provider, raw=raw)

    # ------------------------------------------------------------------
    # Optional second reader
    # ------------------------------------------------------------------

    def _consensus_provider(self) -> str:
        configured = self.settings.consensus_provider
        if configured in ("nvidia", "nebius"):
            return configured
        return "nebius" if self.client.endpoint.provider == "nvidia" else "nvidia"

    def _consensus_client(self, provider: str) -> ExtractionClient:
        client = self._consensus_clients.get(provider)
        if client is None:
            flipped = self.settings.model_copy(
                update={
                    "vision_provider": provider,
                    # Blank the per-provider overrides so flipping the host uses
                    # that host's own default rather than a model name carried
                    # over from the other one.
                    "nvidia_vision_model": self.settings.nvidia_vision_model
                    if provider == "nvidia" else "",
                    "nebius_vision_model": self.settings.nebius_vision_model
                    if provider == "nebius" else "",
                }
            )
            client = ExtractionClient(flipped)
            self._consensus_clients[provider] = client
        return client

    async def _run_consensus(
        self, doc: DocumentReport, pages: list[PageImage], raw_dir: Path
    ) -> None:
        """Have the other provider read the same pages, then compare by rule.

        The comparison is `compare_key` equality in app/consensus.py — no model
        judges the outcome. A divergence is a review marker on the field, and
        agreement is not proof either: the primary extraction stays the one the
        report is built from.
        """
        provider = self._consensus_provider()
        result = ConsensusResult(provider=provider, status="pending")
        doc.consensus = result

        client = self._consensus_client(provider)
        if not self.settings.demo_mode and not _key_is_real(client.endpoint.api_key):
            result.status = "skipped"
            result.model = client.endpoint.model
            result.error = f"{client.endpoint.provider.upper()}_API_KEY not set"
            return

        try:
            outcome = await client.extract_document(
                document_id=f"{doc.id}_alt",
                pages=pages,
                declared_doc_type=doc.declared_doc_type,
                raw_dir=raw_dir,
            )
        except Exception as exc:  # noqa: BLE001 - a second reader is optional
            result.status = "failed"
            result.model = client.endpoint.model
            result.error = f"{type(exc).__name__}: {exc}"[:300]
            return

        result.provider = outcome.provider or provider
        result.model = outcome.model
        result.source = outcome.source
        result.usage = _sum_usage(outcome.attempts)
        result.latency_ms = outcome.total_latency_ms

        if outcome.response is None:
            result.status = "failed"
            result.error = outcome.error or "second reader returned nothing usable"
            return

        secondary = DocumentReport(
            id=f"{doc.id}_alt", label=doc.label, filename=doc.filename
        )
        apply_outcome(secondary, outcome)
        result.divergences = divergences_between(doc, secondary)
        result.summary = summarize(result.divergences)
        result.status = "cached" if outcome.used_cache else "ok"

    # ------------------------------------------------------------------
    # Human corrections
    # ------------------------------------------------------------------

    def _resettle(self, report: Report) -> Report:
        """Re-derive everything downstream of the field values.

        No model is called: normalization, comparison, cross-field sanity and the
        risk score are pure functions of the values, which is exactly why a
        reviewer can fix a transcription and see the verdict move in milliseconds
        instead of paying for another extraction.
        """
        # refresh_report rebuilds the comparisons from scratch, so any second
        # opinion already recorded has to be carried across rather than silently
        # dropped -- it was real output that cost a call.
        prior = {c.field: c.adjudication for c in report.comparisons if c.adjudication}
        report.sanity = sanity_checks(report)
        refresh_report(report, self.client)
        for comp in report.comparisons:
            carried = prior.get(comp.field)
            if carried is not None:
                comp.adjudication = carried
        report.risk = score_report(report)
        apply_status(report)
        return report

    def correct_field(
        self, shipment_id: str, document_id: str, field: str, value: str,
        *, actor: str = "reviewer", note: str = ""
    ) -> Report:
        report = self.get(shipment_id)
        if report is None:
            raise KeyError(shipment_id)
        apply_correction(report, document_id, field, value, actor=actor, note=note)
        self._resettle(report)
        self.store.write(report)
        return report

    def revert_field(
        self, shipment_id: str, document_id: str, field: str, *, actor: str = "reviewer"
    ) -> Report:
        report = self.get(shipment_id)
        if report is None:
            raise KeyError(shipment_id)
        revert_corrections(report, document_id, field, actor=actor)
        self._resettle(report)
        self.store.write(report)
        return report

    # ------------------------------------------------------------------

    async def retry_document(self, shipment_id: str, document_id: str) -> Report | None:
        """Retry one document. Siblings are untouched."""
        report = self.get(shipment_id)
        if report is None:
            return None
        target = next((d for d in report.documents if d.id == document_id), None)
        if target is None:
            return None

        pages = load_pages(self.store.dir(shipment_id), target)
        if not pages:
            target.error = (
                "page images are no longer on disk; re-upload this document"
            )
            self.store.write(report)
            return report

        target.status = "running"
        target.error = None
        refresh_report(report, self.client)
        self.store.write(report)

        try:
            res: ExtractionOutcome | Exception = await self.client.extract_document(
                document_id=target.id,
                pages=pages,
                declared_doc_type=target.declared_doc_type,
                raw_dir=self.store.dir(shipment_id) / "raw",
            )
        except Exception as exc:  # noqa: BLE001
            res = exc

        apply_outcome(target, res)
        if target.extraction is not None:
            # Re-verified from the text saved on the first pass, so a retry gives
            # the same verdict the original run would have. No sidecar means the
            # document had no text source then either, which stays not_checkable.
            target.verification = verify_document(
                target.extraction,
                load_sidecar(self._text_file(shipment_id, target.id)),
                threshold=self.settings.evidence_min_ratio,
            )
        # The cross-field rules and the score are functions of the comparisons
        # this document just changed, so a retry has to re-derive them. Leaving
        # them at the old values would put a banner beside a verdict that was
        # computed from a document that no longer exists.
        report.sanity = sanity_checks(report)
        refresh_report(report, self.client)
        report.risk = score_report(report)
        apply_status(report)
        self.store.write(report)
        return report

    # ------------------------------------------------------------------

    def get(self, shipment_id: str) -> Report | None:
        """Prefer the in-memory copy so a poll never observes a torn write."""
        return self._reports.get(shipment_id) or self.store.read(shipment_id)

    def is_running(self, shipment_id: str) -> bool:
        task = self._tasks.get(shipment_id)
        return bool(task and not task.done())

    async def wait(self, shipment_id: str, timeout: float = 300.0) -> None:
        """Await an in-flight run. Used by tests and the fixture self-check."""
        task = self._tasks.get(shipment_id)
        if task:
            await asyncio.wait_for(task, timeout=timeout)
