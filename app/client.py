"""Model clients for two providers: NVIDIA build.nvidia.com and Nebius Token
Factory.

Both expose an OpenAI-compatible /chat/completions endpoint, and both differ
from the OpenAI spec in ways that were established by calling them, not by
reading docs:

  * response_format json_schema envelope. Both hosts reject the *bare* schema
    their own docs show. NVIDIA: 400 "missing field `name`". Nebius: 422
    "response_format.json_schema.name: Field required". Both accept the
    standard wrapped {name, strict, schema}. A startup probe still decides
    strict / json_object / plain per endpoint rather than assuming.

  * reasoning_content. Nemotron models emit chain-of-thought in a
    `reasoning_content` (NVIDIA, Nebius-Nano) or `reasoning` (Nebius) field,
    and `content` can be empty or None even on HTTP 200. `_message_text()`
    handles all of it; naively reading `.content` yields empty extractions.

  * transient 503 saturation on NVIDIA shared workers. Live-verified:
    "ResourceExhausted: Worker local total request limit reached (16/16)"
    cleared only on the 5th attempt. NVIDIA therefore gets a longer backoff
    ladder than Nebius, where 3 attempts sufficed.

Transport failures retry below the parse ladder, so a 503 never burns a rung.
In DEMO_MODE no network client is ever constructed.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from .cache import ResponseCache, cache_key
from .config import Settings, get_settings
from .fields import FieldKey
from .ingest import PageImage
from .models import ExtractionResponse, extraction_json_schema
from .parse import ParseResult, parse_extraction
from .prompt import correction_messages, messages_for, simplified_messages
from .providers import Endpoint, summary_endpoint, vision_endpoint

JsonMode = Literal["strict", "json_object", "plain", "cache"]

# Backoff per transport attempt, by provider. Nebius cleared inside 3; NVIDIA's
# shared workers needed 5 live, so it gets a longer ladder.
BACKOFF_SCHEDULE: dict[str, tuple[float, ...]] = {
    "nebius": (1.0, 4.0, 12.0),
    "nvidia": (2.0, 5.0, 10.0, 20.0, 30.0),
}

# Retryable transport statuses. 400/404/410/422 are NOT here: 410 is a retired
# model (permanent), 400/422 mean the request itself was wrong.
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ConfigError(RuntimeError):
    """A permanent local misconfiguration (no key, DEMO_MODE on).

    Distinct from a transport failure: retrying it three times just delays the
    same error and, worse, hides it behind a generic 'transport failed' string.
    """


# Values that mean "not configured yet". Sending one of these produces a
# confusing 401 from the API instead of a clear local error.
PLACEHOLDER_KEYS = {"", "your-api-key-here", "paste-your-key-here", "changeme"}


def _key_is_real(key: str) -> bool:
    return (key or "").strip() not in PLACEHOLDER_KEYS


class AllTransportAttemptsFailed(RuntimeError):
    def __init__(self, attempts: list[str]):
        self.attempts = attempts
        super().__init__("transport failed after retries: " + " | ".join(attempts))


@dataclass
class AttemptLog:
    rung: str
    raw_text: str
    ok: bool
    note: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0


@dataclass
class ExtractionOutcome:
    response: ExtractionResponse | None
    status: Literal["ok", "partial", "failed"]
    attempts: list[AttemptLog]
    issues: list[str]
    dropped_fields: list[FieldKey]
    used_cache: bool
    used_fallback_schema: bool
    error: str | None = None
    json_mode: JsonMode = "plain"
    # 'live_api' (real call now) | 'cached_live' (real call earlier) |
    # 'cached_authored' (hand-written fixture) | 'none'. Kept separate from
    # used_cache so a cached authored fixture can never be reported as live.
    source: str = "none"
    provider: str = ""
    model: str = ""
    # Basenames of the raw bodies written for this document, in call order.
    raw_files: list[str] = field(default_factory=list)

    @property
    def total_latency_ms(self) -> int:
        return sum(a.latency_ms for a in self.attempts)


def _message_text(message: Any) -> tuple[str, str]:
    """Pull usable text out of a chat message, handling reasoning-only replies.

    Returns (text, field_used). Nemotron models sometimes answer in
    reasoning_content with an empty content, and sometimes duplicate their
    chain-of-thought into content. We prefer content; if it is empty we fall
    back to the reasoning field, because for structured extraction a JSON body
    inside reasoning is still recoverable while an empty string is not.
    """
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        return content, "content"

    for alt in ("reasoning_content", "reasoning"):
        value = getattr(message, alt, None)
        if isinstance(value, str) and value.strip():
            return value, alt

    return (content or "") if isinstance(content, str) else "", "content"


class ChatClient:
    """Shared transport, capability probe and backoff for one endpoint."""

    def __init__(self, endpoint: Endpoint, settings: Settings, *, raw_dir: Path | None = None):
        self.endpoint = endpoint
        self.settings = settings
        self.raw_dir = raw_dir
        self.json_mode: JsonMode = "cache" if settings.demo_mode else "plain"
        self._probed = settings.demo_mode
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self.settings.demo_mode:
            raise ConfigError(
                f"DEMO_MODE is on: no network client is created. Set DEMO_MODE=false "
                f"and provide {self.endpoint.provider.upper()}_API_KEY to call the live endpoint."
            )
        if not _key_is_real(self.endpoint.api_key):
            raise ConfigError(
                f"{self.endpoint.provider.upper()}_API_KEY is not set to a real value, "
                f"which the '{self.endpoint.label}' role needs from {self.endpoint.host}. "
                "Copy .env.example to .env and add a key."
            )
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.endpoint.base_url,
                api_key=self.endpoint.api_key,
                timeout=self.settings.request_timeout_seconds,
                max_retries=0,  # we own retries; see _transport_call
            )
        return self._client

    def response_format(self) -> dict[str, Any] | None:
        if self.json_mode == "strict":
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": "shipment_extraction",
                    "strict": True,
                    "schema": extraction_json_schema(),
                },
            }
        if self.json_mode == "json_object":
            return {"type": "json_object"}
        return None

    async def transport_call(
        self,
        messages: list[dict],
        *,
        response_format: dict | None,
        max_tokens: int = 1600,
        temperature: float = 0.0,
        max_attempts: int | None = None,
    ) -> tuple[str, str, dict[str, int]]:
        """Raw (text, field_used, usage). Exponential backoff, no ladder logic.

        max_attempts caps the schedule. The capability probe passes 1-2 because
        it runs its own retry loop: nesting a 3x probe retry inside a 5x
        transport ladder would spend 15 API calls and ~200s of sleeps to answer
        one yes/no question.
        """
        schedule = BACKOFF_SCHEDULE.get(self.endpoint.provider, BACKOFF_SCHEDULE["nebius"])
        if max_attempts is not None:
            schedule = schedule[:max_attempts]
        attempts: list[str] = []
        for i in range(len(schedule)):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.endpoint.model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                if response_format is not None:
                    kwargs["response_format"] = response_format
                completion = await self.client.chat.completions.create(**kwargs)
                choice = completion.choices[0]
                message = choice.message
                if getattr(message, "refusal", None):
                    raise RuntimeError(f"model refused: {message.refusal}")
                text, used = _message_text(message)
                usage = getattr(completion, "usage", None)
                usage_dict = {
                    "prompt": getattr(usage, "prompt_tokens", 0) or 0,
                    "completion": getattr(usage, "completion_tokens", 0) or 0,
                    "total": getattr(usage, "total_tokens", 0) or 0,
                } if usage else {}
                if not text.strip():
                    raise RuntimeError(
                        f"empty completion (content and reasoning both blank; "
                        f"finish_reason={getattr(choice, 'finish_reason', None)})"
                    )
                return text, used, usage_dict
            except (APITimeoutError, APIConnectionError) as exc:
                attempts.append(f"{type(exc).__name__}: {exc}")
            except APIStatusError as exc:
                attempts.append(f"HTTP {exc.status_code}")
                if exc.status_code not in RETRYABLE_STATUS:
                    raise
            except ConfigError:
                raise  # permanent; retrying just delays the same failure
            except RuntimeError as exc:
                # Empty completions from a saturated worker are worth retrying.
                attempts.append(str(exc))
            if i < len(schedule) - 1:
                await asyncio.sleep(schedule[i] + random.uniform(0, 0.5))
        raise AllTransportAttemptsFailed(attempts)

    async def probe(self, *, force: bool = False) -> JsonMode:
        """Decide once what structured-output mode this endpoint honours."""
        if self.settings.demo_mode:
            self.json_mode = "cache"
            self._probed = True
            return self.json_mode
        if self._probed and not force:
            return self.json_mode

        probe_prompt = 'Reply with exactly this JSON and nothing else: {"ok": true}'
        # Capacity budget is shared across modes: a busy endpoint should not
        # make us give up after one 503, but we also must not hammer it.
        capacity_budget = 3

        for mode in ("strict", "json_object"):
            self.json_mode = mode  # type: ignore[assignment]
            tried = 0
            while True:
                tried += 1
                try:
                    raw, _, _ = await self.transport_call(
                        [{"role": "user", "content": probe_prompt}],
                        response_format=self.response_format(),
                        max_tokens=200,
                        max_attempts=2,
                    )
                except (APIStatusError, AllTransportAttemptsFailed) as exc:
                    note = status_note(exc)
                    if is_rejected_by_schema_param(exc):
                        # A real answer: this endpoint does not implement the
                        # mode. Move down to the next one.
                        probe_log(f"{self.endpoint.label}/{self.endpoint.model}: "
                                  f"{mode} rejected ({note})")
                        break
                    if is_capacity_failure(exc) and capacity_budget and tried < 3:
                        # "Busy", not "unsupported". Probing again for the same
                        # verdict beats recording one we never actually observed:
                        # live, a 503 storm made the probe downgrade a model
                        # that had accepted strict json_schema minutes earlier.
                        capacity_budget -= 1
                        probe_log(f"{self.endpoint.label}/{self.endpoint.model}: "
                                  f"{mode} hit capacity ({note}); retrying probe")
                        continue
                    probe_log(f"{self.endpoint.label}/{self.endpoint.model}: "
                              f"{mode} inconclusive ({note}); staying conservative")
                    break
                else:
                    if parse_ok_for_probe(raw):
                        self._probed = True
                        probe_log(f"{self.endpoint.label}/{self.endpoint.model}: "
                                  f"honours {mode}")
                        return self.json_mode
                    probe_log(f"{self.endpoint.label}/{self.endpoint.model}: "
                              f"{mode} accepted but returned unusable JSON; downgrading")
                    break

        self.json_mode = "plain"
        self._probed = True
        probe_log(f"{self.endpoint.label}/{self.endpoint.model}: no structured-output "
                  f"mode honoured; relying on prompt only")
        return self.json_mode

    def persist_raw(
        self, stem: str, raw: str, raw_dir: Path | None = None
    ) -> str | None:
        """Every raw response goes to disk, including failed ones.

        ``raw_dir`` overrides the client's own. The server shares one client
        across every shipment, so a run's raw bodies have to be chosen per call;
        scripts that build a client per purpose keep using the constructor value.
        Returns the filename written, or None when nothing was written.
        """
        target = raw_dir or self.raw_dir
        if target is None:
            return None
        safe = "".join(c for c in stem if c.isalnum() or c in "-_")
        name = f"{safe}.txt"
        try:
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_text(raw)
        except OSError:
            return None
        return name


class ExtractionClient(ChatClient):
    """Vision extraction endpoint. One call per document, all its pages."""

    def __init__(self, settings: Settings | None = None, *, raw_dir: Path | None = None):
        self.settings = settings or get_settings()
        super().__init__(vision_endpoint(self.settings), self.settings, raw_dir=raw_dir)
        self.cache = ResponseCache(self.settings.cache_dir)
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)

    async def extract_document(
        self,
        *,
        document_id: str,
        pages: list[PageImage],
        declared_doc_type: str | None = None,
        raw_dir: Path | None = None,
    ) -> ExtractionOutcome:
        async with self._semaphore:
            return await self._extract_locked(
                document_id=document_id,
                pages=pages,
                declared_doc_type=declared_doc_type,
                raw_dir=raw_dir,
            )

    async def _extract_locked(
        self,
        *,
        document_id: str,
        pages: list[PageImage],
        declared_doc_type: str | None,
        raw_dir: Path | None = None,
    ) -> ExtractionOutcome:
        page_hashes = [p.sha256 for p in pages]
        key = cache_key(page_hashes, self.endpoint.model, self.endpoint.provider)
        logs: list[AttemptLog] = []
        raw_files: list[str] = []

        def keep(stem: str, body: str) -> None:
            written = self.persist_raw(stem, body, raw_dir=raw_dir)
            if written:
                raw_files.append(written)

        cached = self.cache.get(key, page_sha256s=page_hashes)
        if cached is not None:
            tier = self.cache.provenance_of(key, page_sha256s=page_hashes)
            entry_source = "cached_live" if tier == "live" else "cached_authored"
            parsed = parse_extraction(json.dumps(cached))
            # A replayed body is still the model's output — it was captured from
            # a real call — so it goes on disk too. `source` is what says which
            # kind of evidence the reader is looking at.
            keep(f"{document_id}_cache", json.dumps(cached))
            if parsed.ok:
                return ExtractionOutcome(
                    response=parsed.response,
                    status="partial" if parsed.dropped_fields else "ok",
                    attempts=[AttemptLog(rung="cache", raw_text=json.dumps(cached), ok=True,
                                         note=f"served from response cache ({tier})")],
                    issues=parsed.issues,
                    dropped_fields=parsed.dropped_fields,
                    used_cache=True,
                    used_fallback_schema=False,
                    json_mode=self.json_mode,
                    source=entry_source,
                    provider=self.endpoint.provider,
                    model=self.endpoint.model,
                    raw_files=raw_files,
                )
            if self.settings.demo_mode:
                return ExtractionOutcome(
                    response=None, status="failed",
                    attempts=[AttemptLog(rung="cache", raw_text=json.dumps(cached), ok=False,
                                         note="cached fixture failed to parse")],
                    issues=parsed.issues, dropped_fields=parsed.dropped_fields,
                    used_cache=True, used_fallback_schema=False,
                    error=f"cached response unparsable: {parsed.error}",
                    json_mode="cache",
                    provider=self.endpoint.provider, model=self.endpoint.model,
                    raw_files=raw_files,
                )

        if self.settings.demo_mode:
            return ExtractionOutcome(
                response=None, status="failed", attempts=[], issues=[], dropped_fields=[],
                used_cache=False, used_fallback_schema=False,
                error=(
                    "DEMO_MODE is on but no cached response exists for these images. "
                    "Use a bundled fixture, or set DEMO_MODE=false and provide the "
                    f"{self.endpoint.provider.upper()}_API_KEY for the vision provider."
                ),
                json_mode="cache",
                provider=self.endpoint.provider, model=self.endpoint.model,
            )

        await self.probe()
        uris = [p.data_uri for p in pages]
        messages = messages_for(declared_doc_type, uris)
        fmt = self.response_format()

        # Rungs 1-3 run inside parse_extraction on each call's output; rungs 4
        # and 5 are the two extra calls. Walked by index because the list grows.
        stages: list[tuple[str, list[dict], dict | None]] = [("initial", messages, fmt)]
        bad_reason = ""
        stage_i = 0

        while stage_i < len(stages):
            rung, msgs, response_format = stages[stage_i]
            stage_i += 1
            started = time.monotonic()
            try:
                raw, field_used, usage = await self.transport_call(
                    msgs, response_format=response_format
                )
            except (APIStatusError, AllTransportAttemptsFailed, RuntimeError) as exc:
                note = status_note(exc)
                logs.append(AttemptLog(rung=rung, raw_text="", ok=False, note=note,
                                        latency_ms=int((time.monotonic() - started) * 1000)))
                keep(f"{document_id}_{rung}", f"[transport error] {note}")
                return ExtractionOutcome(
                    response=None, status="failed", attempts=logs, issues=[note],
                    dropped_fields=[], used_cache=False, used_fallback_schema=False,
                    error=f"transport failed: {note}", json_mode=self.json_mode,
                    provider=self.endpoint.provider, model=self.endpoint.model,
                    raw_files=raw_files,
                )

            latency = int((time.monotonic() - started) * 1000)
            keep(f"{document_id}_{rung}", raw)
            parsed: ParseResult = parse_extraction(raw)
            if field_used != "content":
                parsed.issues.append(f"answer read from '{field_used}' (content was empty)")
            logs.append(AttemptLog(
                rung=rung, raw_text=raw, ok=parsed.ok, usage=usage,
                note=parsed.error or "; ".join(parsed.issues[:3]), latency_ms=latency))

            if parsed.ok:
                self._store_success(key, parsed, raw, page_hashes)
                return ExtractionOutcome(
                    response=parsed.response,
                    status="partial" if parsed.dropped_fields else "ok",
                    attempts=logs, issues=parsed.issues,
                    dropped_fields=parsed.dropped_fields,
                    used_cache=False, used_fallback_schema=(rung == "simplified"),
                    json_mode=self.json_mode, source="live_api",
                    provider=self.endpoint.provider, model=self.endpoint.model,
                    raw_files=raw_files,
                )

            bad_text, bad_reason = raw, parsed.error or "unparsable"
            if rung == "initial":
                stages.append(("corrected", correction_messages(bad_text, bad_reason, msgs), fmt))
            elif rung == "corrected":
                stages.append(("simplified", simplified_messages(declared_doc_type, uris), None))

        return ExtractionOutcome(
            response=None, status="failed", attempts=logs,
            issues=[f"rung {a.rung}: {a.note}" for a in logs],
            dropped_fields=[], used_cache=False, used_fallback_schema=False,
            error=f"gave up after {len(logs)} attempts; last error: {bad_reason}",
            json_mode=self.json_mode,
            provider=self.endpoint.provider, model=self.endpoint.model,
            raw_files=raw_files,
        )

    def _store_success(
        self, key: str, parsed: ParseResult, raw: str, page_hashes: list[str]
    ) -> None:
        assert parsed.response is not None
        try:
            self.cache.put(
                key,
                parsed.response.model_dump(mode="json"),
                model=self.endpoint.model,
                provider=self.endpoint.provider,
                page_sha256s=page_hashes,
                raw_text=raw,
            )
        except OSError:
            # A cache write failure must not lose a successful extraction.
            pass


class SummaryClient(ChatClient):
    """One optional executive-summary call per shipment, on Nebius.

    Deliberately separate from the extraction path: it never runs inside the
    document semaphore, never retries the ladder, and its failure must never
    change a shipment's outcome.
    """

    def __init__(self, settings: Settings | None = None, *, raw_dir: Path | None = None):
        self.settings = settings or get_settings()
        super().__init__(summary_endpoint(self.settings), self.settings, raw_dir=raw_dir)

    async def summarize(self, prompt: str) -> tuple[str, dict[str, int]]:
        """Returns (text, usage). Raises on failure; the caller must catch.

        One retry when the model returns chain-of-thought instead of prose. This
        is not rare: live, the same prompt yielded clean sentences on one call
        and a 900-token self-check monologue on the next.
        """
        from .summary import RETRY_SYSTEM, SYSTEM, summary_is_plausible

        total = {"prompt": 0, "completion": 0, "total": 0}
        text = ""
        for system_prompt in (SYSTEM, RETRY_SYSTEM):
            raw, field_used, usage = await self.transport_call(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                response_format=None,
                max_tokens=900,
                temperature=0.3,
            )
            for k, v in (usage or {}).items():
                total[k] = total.get(k, 0) + v
            # A reasoning model may bury the prose in reasoning_content. Prefer
            # the answer field we actually got text from, but strip
            # chain-of-thought that leaked into `content`.
            text = raw.strip()
            if field_used != "content":
                text = _best_sentence_slice(text)
            if summary_is_plausible(text):
                return text, total
        return text, total

    async def judge(
        self, system: str, prompt: str, *, max_tokens: int = 700, temperature: float = 0.0
    ) -> tuple[str, dict[str, int]]:
        """One model reply, as raw text, for callers that parse their own shape.

        Separate from summarize() on purpose: adjudication has a JSON contract
        and a draft email has a prose one, and neither should inherit the
        summary's plausibility guard by accident.
        """
        raw, _used, usage = await self.transport_call(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            response_format=None,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return raw, usage or {}


def _best_sentence_slice(text: str) -> str:
    """Trim chain-of-thought preamble down to the trailing prose answer."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return text.strip()
    # Reasoning traces are markdown-ish lists/headers; the answer is prose.
    prose = [l for l in lines if not re_is_structural(l)]
    return " ".join(prose[-3:]).strip() if prose else lines[-1]


def re_is_structural(line: str) -> bool:
    import re

    return bool(re.match(r"^\s*(\d+[.)]|[-*]|#{1,6}\s|\*\*|Step |Analysis|Thought)", line))


def parse_ok_for_probe(raw: str) -> bool:
    try:
        return isinstance(json.loads(raw.strip()), dict)
    except (json.JSONDecodeError, ValueError):
        from .parse import load_json_object

        obj, _, _, _ = load_json_object(raw)
        return obj is not None


def is_capacity_failure(exc: Exception) -> bool:
    """503 / timeouts mean 'busy', not 'unsupported'.

    Verified live on build.nvidia.com: the omni model returns
    'ResourceExhausted: Worker local total request limit reached (16/16)' under
    load and then serves the same request fine seconds later.
    """
    if isinstance(exc, APIStatusError):
        return exc.status_code in {429, 500, 502, 503, 504}
    if isinstance(exc, AllTransportAttemptsFailed):
        return any(code in " ".join(exc.attempts)
                   for code in ("503", "504", "429", "Timeout", "timed out"))
    return False


def is_rejected_by_schema_param(exc: Exception) -> bool:
    """400/403/422 on the probe means that response_format mode is unsupported."""
    if isinstance(exc, APIStatusError):
        return exc.status_code in (400, 403, 422)
    return False


def status_note(exc: Exception) -> str:
    if isinstance(exc, APIStatusError):
        return f"HTTP {exc.status_code}: {str(exc.body)[:300]}"
    if isinstance(exc, AllTransportAttemptsFailed):
        return " | ".join(exc.attempts)
    return f"{type(exc).__name__}: {exc}"


def probe_log(message: str) -> None:
    import sys

    print(f"[probe] {message}", file=sys.stderr)
