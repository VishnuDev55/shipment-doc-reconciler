"""Application settings. Single source of truth for configuration.

Reads .env (gitignored). .env.example is the committed template.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Version tags. Bump when behaviour changes so a cached response can never be
# silently reconciled against an incompatible ruleset.
PROMPT_VERSION = "extract-v4"
NORMALIZER_VERSION = "norm-v1"
COMPARATOR_VERSION = "cmp-v1"
# Line items are a separate contract read from the same page images. It has to
# carry its own version tag or its cache entries land in the scalar namespace:
# the key is a hash of (prompt_version, provider, model, pages), and the
# hand-authored fallback tier matches on page hashes alone.
#   * "lineitems-v2" adds the price and per-row weight columns, so a v1 response
#     cannot answer a v2 lookup -- it would silently return rows with no amounts,
#     and the arithmetic checks would read that as "nothing to check".
LINEITEMS_PROMPT_VERSION = "lineitems-v2"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    nebius_api_key: str = Field(default="", alias="NEBIUS_API_KEY")
    nebius_base_url: str = Field(
        default="https://api.tokenfactory.nebius.com/v1/", alias="NEBIUS_BASE_URL"
    )

    # NVIDIA build.nvidia.com — hosts the vision model that reads documents.
    nvidia_api_key: str = Field(default="", alias="NVIDIA_API_KEY")
    nvidia_base_url: str = Field(
        default="https://integrate.api.nvidia.com/v1/", alias="NVIDIA_BASE_URL"
    )

    # Which provider reads the document images. "nvidia" is the default;
    # "nebius" switches to MiniCPM-V on Token Factory as a fallback if the
    # NVIDIA key is unavailable or its shared workers stay saturated.
    vision_provider: str = Field(default="nvidia", alias="VISION_PROVIDER")
    # Override the model ids without touching code. Empty = provider default.
    nvidia_vision_model: str = Field(default="", alias="NVIDIA_VISION_MODEL")
    nebius_vision_model: str = Field(default="", alias="NEBIUS_VISION_MODEL")
    nebius_summary_model: str = Field(default="", alias="NEBIUS_SUMMARY_MODEL")

    # The optional executive-summary call. Never blocks or fails a shipment.
    enable_summary: bool = Field(default=True, alias="ENABLE_SUMMARY")

    # Legacy alias still honoured by .env: the vision model for the nebius path.
    nebius_model: str = Field(default="", alias="NEBIUS_MODEL")

    # Replay extractions from the on-disk response cache instead of calling the
    # API. Off by default — the app is meant to read real documents. Set
    # DEMO_MODE=true for the keyless hosted demo, where the bundled cache
    # replays real captured model output.
    demo_mode: bool = Field(default=False, alias="DEMO_MODE")

    # Allow the GUI to apply provider keys and demo mode to the running process.
    # Such a key then serves every visitor, so set this false on a public
    # deployment and keep using environment secrets there.
    gui_config_enabled: bool = Field(default=True, alias="GUI_CONFIG_ENABLED")

    # Optional shared secret for the two config endpoints. The Configure drawer
    # writes provider keys into this process and there are no user sessions, so
    # without a token anyone who can reach the port can set the key everyone
    # uses. Blank means "local single-user use, no token" -- which is right on a
    # laptop and wrong on a shared host. Prefer GUI_CONFIG_ENABLED=false for a
    # public deployment; use a token when you want remote configuration to stay
    # possible but closed.
    config_token: str = Field(default="", alias="CONFIG_TOKEN")

    # Comma-separated keys that let this app be exposed as a real API rather than
    # only a demo UI: every data-bearing route then requires one of them in
    # `X-API-Key` or `Authorization: Bearer`. Blank means open, which is right
    # for a laptop and for the keyless hosted demo, and wrong for anything
    # reachable by others -- a shipment report is other people's commercial data.
    # Compared in constant time and never echoed back or logged.
    api_keys: str = Field(default="", alias="API_KEYS")

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in (self.api_keys or "").split(",") if k.strip()]

    # A model's opinion on comparisons the deterministic engine called
    # ambiguous, and a draft email describing the discrepancies. Both are
    # additive: neither can change a status, a verdict or a shipment state.
    enable_adjudication: bool = Field(default=True, alias="ENABLE_ADJUDICATION")
    enable_briefing: bool = Field(default=True, alias="ENABLE_BRIEFING")

    # Read every page with a second provider and flag the fields the two did not
    # agree on. Off by default because it doubles the token spend of a run, and
    # the result is a review marker rather than a better answer.
    consensus: bool = Field(default=False, alias="CONSENSUS")

    # A second, separate read of every page that transcribes the goods lines
    # rather than the totals, so the report can say *which line* disagrees. Off
    # by default: it is one more vision call per document, and no scalar verdict
    # depends on it. The matched-items section is appended after the run is
    # already complete, so enabling it cannot change a field status, a risk
    # score or the shipment verdict.
    enable_line_items: bool = Field(default=False, alias="ENABLE_LINE_ITEMS")
    # 'nvidia' | 'nebius' | blank for "whichever the primary run is not".
    consensus_provider: str = Field(default="", alias="CONSENSUS_PROVIDER")

    runs_dir: Path = Field(default=PROJECT_ROOT / "runs", alias="RUNS_DIR")
    cache_dir: Path = Field(default=PROJECT_ROOT / "fixtures" / "responses", alias="CACHE_DIR")

    max_documents: int = Field(default=5, alias="MAX_DOCUMENTS")

    # Age after which runs under RUNS_DIR are deleted at startup. Zero -- the
    # default -- keeps everything, because silently discarding somebody's
    # shipment history is worse than an unbounded directory they can clean
    # themselves. Set it on a public instance, where runs are other people's
    # commercial documents and disk is not free.
    run_ttl_hours: int = Field(default=0, alias="RUN_TTL_HOURS")

    # Requests per minute per client to POST /api/shipments -- the only endpoint
    # that spends money, since each run fans out to a vision model per document.
    # Reading reports is unlimited on purpose: the browser polls it every second
    # and throttling that would only break the UI. 0 disables it, which is what
    # you want behind a reverse proxy that rate-limits properly.
    run_limit_per_minute: int = Field(default=30, alias="RUN_LIMIT_PER_MINUTE")

    # Comma-separated origins allowed to call the API from a browser. Empty --
    # the default -- sends no CORS headers at all, so the app stays same-origin
    # only. This is not a security boundary for the API key; it decides whether
    # some other site's JavaScript can ride a user's session.
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")

    # Reports held in process memory so a poll does not re-read disk. Bounded:
    # an instance that has served 50,000 shipments should not still be holding
    # all 50,000.
    max_reports_in_memory: int = Field(default=128, alias="MAX_REPORTS_IN_MEMORY")
    max_pages_per_document: int = Field(default=6, alias="MAX_PAGES_PER_DOCUMENT")
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")
    image_long_edge: int = Field(default=1600, alias="IMAGE_LONG_EDGE")
    jpeg_quality: int = Field(default=85, alias="JPEG_QUALITY")

    request_timeout_seconds: float = Field(default=180.0, alias="REQUEST_TIMEOUT_SECONDS")
    # Hard total deadline for one HTTP round-trip, enforced with asyncio.wait_for
    # around the whole call. request_timeout_seconds is the SDK's per-read
    # timeout, which does not bound a request that opens a connection and never
    # sends headers; this does. A hung call becomes a TimeoutError and falls into
    # the retry ladder instead of blocking a run forever.
    request_deadline_seconds: float = Field(default=240.0, alias="REQUEST_DEADLINE_SECONDS")
    # Circuit breaker: after this many consecutive transport failures (503 /
    # timeout / deadline) a provider is marked degraded and calls fail fast for
    # the cooldown, then a single half-open trial decides recovery.
    circuit_breaker_threshold: int = Field(default=3, alias="CIRCUIT_BREAKER_THRESHOLD")
    circuit_breaker_cooldown_seconds: float = Field(
        default=30.0, alias="CIRCUIT_BREAKER_COOLDOWN_SECONDS")
    max_concurrency: int = Field(default=4, alias="MAX_CONCURRENCY")

    # How closely a quoted evidence line has to resemble the page's own text to
    # count as real, 0-100 (rapidfuzz partial_ratio). 90 is deliberately not 100:
    # a model that copies "Nordwind Industrial Supply GmbH" from a line reading
    # "CONSIGNEE Nordwind Industrial Supply GmbH" has told the truth about where
    # it looked, and demanding an exact reproduction would fail honest quotes
    # while proving nothing. Below ~85 the check starts accepting lines that are
    # merely similar, which is the failure it exists to catch.
    evidence_min_ratio: int = Field(default=90, alias="EVIDENCE_MIN_RATIO", ge=0, le=100)

    # --- Internal arithmetic tolerances (app/mathcheck.py) -----------------
    # Money is absolute, not relative. A percentage of a large invoice is bigger
    # than a rounding error -- 0.5 % of 5,508 is 27 -- so a relative band would
    # have passed the defect this project found in its own fixture data, where a
    # row printed 5,510.00 for a product of 5,508.00. Two cents allows for the
    # per-line rounding a real invoice does; it does not allow for being wrong.
    math_tolerance_money: float = Field(default=0.02, alias="MATH_TOLERANCE_MONEY", ge=0)
    math_tolerance_weight_rel: float = Field(
        default=0.005, alias="MATH_TOLERANCE_WEIGHT_REL", ge=0)
    math_tolerance_weight_kg: float = Field(
        default=1.0, alias="MATH_TOLERANCE_WEIGHT_KG", ge=0)
    # Net may equal gross only if the packaging weighs nothing, which is
    # impossible for cartons on pallets. Same constant the cross-document rule
    # uses, so the two cannot disagree about what "impossible" means.
    math_min_net_margin_kg: float = Field(default=0.0, alias="MATH_MIN_NET_MARGIN_KG")

    # Price per million tokens, used only to annotate the transparency panel.
    # Zero means "not configured", and the panel then declines to show a cost at
    # all rather than guess one: a stale rate on an audit screen is worse than no
    # rate. Both providers meter differently and change prices without notice.
    usd_per_mtok: float = Field(default=0.0, alias="USD_PER_MTOK")

    # --- Model discovery ---------------------------------------------------
    # Ask each host what it actually serves before trusting a configured name.
    # Off in DEMO_MODE regardless: there is nothing to discover without a key.
    model_discovery: bool = Field(default=True, alias="MODEL_DISCOVERY")
    model_discovery_ttl_seconds: int = Field(
        default=900, alias="MODEL_DISCOVERY_TTL_SECONDS"
    )
    # Operator-named models, highest priority — tried before the configured
    # default and before anything discovered. Comma-separated.
    vision_model_allow: str = Field(default="", alias="VISION_MODEL_ALLOW")
    summary_model_allow: str = Field(default="", alias="SUMMARY_MODEL_ALLOW")

    @property
    def has_nebius_key(self) -> bool:
        return _usable(self.nebius_api_key)

    @property
    def has_nvidia_key(self) -> bool:
        return _usable(self.nvidia_api_key)

    @property
    def has_api_key(self) -> bool:
        """Backwards-compatible alias: does the Nebius side work?"""
        return self.has_nebius_key

    @property
    def vision_ready(self) -> bool:
        """Can the configured vision provider actually be called?"""
        return self.has_nvidia_key if self.vision_provider != "nebius" else self.has_nebius_key

    @property
    def summary_ready(self) -> bool:
        return self.enable_summary and self.has_nebius_key


def _usable(key: str) -> bool:
    stripped = (key or "").strip()
    return bool(stripped) and stripped not in {
        "your-api-key-here",
        "paste-your-key-here",
        "test-key-not-real",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
