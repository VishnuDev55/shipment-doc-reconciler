"""Ask each host what it actually serves, instead of assuming.

`providers.py` records the price of assuming: the brief specified
`nvidia/Nemotron-Nano-12B-V2-VL`, which returned **410 Gone** on
build.nvidia.com and **404** on Token Factory. That was found by hand, with a
live key, mid-project. This module turns that one-off manual check into an
automated one, so a model that retires underneath the app is reported at startup
instead of surfacing as mysterious extraction failures later.

Two deliberate constraints:

* Discovery only *chooses among what the host offers*. It never invents a model
  name, and if the configured model is present it is kept — results stay
  reproducible, and cache keys (which include the model) stay stable.
* A failed discovery changes nothing. No key, no network, or an unparsable
  catalog means the configured default stands, with the reason recorded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .providers import Endpoint

Role = str  # "vision" | "summary"


class DiscoveryError(RuntimeError):
    """The catalog could not be fetched or understood."""


@dataclass(frozen=True)
class ModelInfo:
    id: str
    modality: str = ""
    object: str = "model"

    @property
    def declares_modality(self) -> bool:
        return bool(self.modality)

    @property
    def accepts_images(self) -> bool:
        if self.modality:
            left = self.modality.split("->", 1)[0].lower()
            return "image" in left or "vision" in self.modality.lower()
        return looks_visionary(self.id)

    def speaks_text(self) -> bool:
        if not self.modality:
            # With no declared contract, assume a chat model answers in text:
            # every OpenAI-compatible host here does.
            return True
        right = self.modality.split("->", 1)[1].lower() if "->" in self.modality else ""
        return (not right) or "text" in right


# Checked against the live NVIDIA host on 2026-10-02: GET /v1/models returns only
# {id, object, created, owned_by} for all 81 models, and /v1/models/{id} returns
# the same four fields. NVIDIA publishes no modality at all, so on that host image
# capability can only be inferred from the name -- which is why Selection records
# `inferred` rather than letting an inference read as a host declaration.
_VISION_TOKENS = ("vl", "vision", "omni", "vit", "image", "multimodal")
# A "-vl" in an embedding model is genuinely vision-language, and picking it as a
# chat fallback would produce garbage completions rather than an error.
_NOT_CHAT = ("embed", "retriever", "guard", "rerank", "asr", "tts", "whisper")


def looks_visionary(model_id: str) -> bool:
    lowered = model_id.lower()
    if any(token in lowered for token in _NOT_CHAT):
        return False
    return any(token in lowered for token in _VISION_TOKENS)


@dataclass
class Catalog:
    host: str
    models: list[ModelInfo] = field(default_factory=list)
    fetched_at: float = 0.0

    def ids(self) -> set[str]:
        return {m.id for m in self.models}


@dataclass
class Selection:
    """What discovery decided, kept so the report can be honest about it.

    'configured' — nothing changed.
    'discovered' — swapped to a model the host actually serves.
    'unverified' — catalog could not be read, so the configured default stands.
    """

    role: Role
    provider: str
    host: str
    requested: str
    chosen: str
    status: str
    note: str = ""
    available: tuple[str, ...] = ()
    # True when image capability was guessed from the model name because the host
    # publishes no modality metadata. Without this flag an inference would read
    # as though the host had confirmed it.
    inferred: bool = False


def _row_modality(row: dict) -> str:
    """Read the modality string wherever this host actually puts it.

    Measured live on 2026-10-02: Token Factory with ?verbose=true reports it at
    architecture.modality, not at the row's top level. Reading only the flat
    location made all 25 of its models look undeclared, which downgraded a host
    that *states* a capability to a name guess that then decided nothing. The
    flat key is still tried first so a host that puts it there keeps working.
    """
    architecture = row.get("architecture")
    nested = architecture.get("modality") if isinstance(architecture, dict) else None
    return str(row.get("modality") or nested or "")


def parse_catalog(payload: object) -> list[ModelInfo]:
    """Accept the envelope shapes these hosts actually return.

    Checked live against NVIDIA on 2026-10-02: it returns {"object": "list",
    "data": [...]} with only id/object/created/owned_by per row -- it does not use
    a "models" key after all, and publishes no modality at any depth, so there
    image capability can only be inferred from the name. Nebius uses the same
    "data" envelope and, with ?verbose=true, does declare modality. Reading only
    one shape would make the other look like a host with zero models, and
    "zero models" would silently keep a dead default.
    """
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("models") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    out: list[ModelInfo] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = row.get("id") or row.get("name")
        if not ident:
            continue
        out.append(ModelInfo(id=str(ident), modality=_row_modality(row),
                             object=str(row.get("object", "model"))))
    return out


# Preference order, most capable first. Both are the family the hackathon asks
# for; nothing here is a claim that a model exists, only an ordering of what the
# host reported back.
_VISION_PREFERRED = ("nemotron", "minicpm", "vl", "vision", "omni")
_SUMMARY_PREFERRED = ("nemotron", "llama", "mistral", "qwen")


def _score(model: ModelInfo, patterns: tuple[str, ...]) -> int:
    lowered = model.id.lower()
    for position, pattern in enumerate(patterns):
        if pattern in lowered:
            return position
    return len(patterns)


def pick(models: list[ModelInfo], role: Role) -> ModelInfo | None:
    """Rank what the host serves and take the best, deterministically.

    A declared capability always beats an inferred one; the preference band only
    breaks ties within each group. Sorted by (inferred, band, id), so two runs on
    one catalog choose the same model -- iteration order would not.
    """
    eligible = [m for m in models if (m.accepts_images if role == "vision" else m.speaks_text())]
    if not eligible:
        return None
    patterns = _VISION_PREFERRED if role == "vision" else _SUMMARY_PREFERRED
    return sorted(
        eligible, key=lambda m: (not m.declares_modality, _score(m, patterns), m.id)
    )[0]


def resolve(
    *,
    role: Role,
    provider: str,
    host: str,
    requested: str,
    catalog: list[ModelInfo],
    allow: str = "",
) -> Selection:
    """Decide the model for one role, preferring what was configured.

    `allow` is the operator's list: it outranks both the configured default and
    discovery, because a human naming a model is a decision, not a guess.
    """
    available = tuple(m.id for m in catalog)
    allowed = [a.strip() for a in (allow or "").split(",") if a.strip()]
    by_id = {m.id: m for m in catalog}
    present = set(by_id)

    for candidate in allowed:
        if candidate in present:
            return Selection(
                role=role, provider=provider, host=host, requested=requested,
                chosen=candidate, status="configured",
                note="selected from MODEL_ALLOW list" if candidate != requested else "",
                inferred=not by_id[candidate].declares_modality,
            )

    if requested in present:
        return Selection(
            role=role, provider=provider, host=host, requested=requested,
            chosen=requested, status="configured", available=available,
            inferred=not by_id[requested].declares_modality,
        )

    best = pick(catalog, role)
    if best is None:
        return Selection(
            role=role, provider=provider, host=host, requested=requested,
            chosen=requested, status="unverified", available=available,
            note=(f"'{requested}' is not listed by {host}, and {host} offers no "
                  f"model identifiable as able to accept "
                  f"{'images' if role == 'vision' else 'text output'}"),
        )

    return Selection(
        role=role, provider=provider, host=host, requested=requested,
        chosen=best.id, status="discovered", available=available,
        note=(f"'{requested}' is not served by {host}; using '{best.id}'"
              + (" (image capability inferred from the model name — this host "
                 "publishes no modality metadata)" if not best.declares_modality
                 else "")),
        inferred=not best.declares_modality,
    )


async def fetch(
    endpoint: Endpoint, *, transport=None, timeout: float = 20.0
) -> Catalog:
    """GET {base}/models. Raises DiscoveryError; never returns a partial truth."""
    import httpx

    url = endpoint.base_url.rstrip("/") + "/models"
    params = {"verbose": "true"} if "nebius" in endpoint.host else {}
    headers = {"Authorization": f"Bearer {endpoint.api_key}"}
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            response = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise DiscoveryError(f"{endpoint.host}: {type(exc).__name__}: {exc}") from exc
    if response.status_code == 401:
        raise DiscoveryError(f"{endpoint.host}: 401 unauthorized — the key is not valid for /models")
    if response.status_code >= 400:
        raise DiscoveryError(f"{endpoint.host}: HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise DiscoveryError(f"{endpoint.host}: /models returned a non-JSON body") from exc

    models = parse_catalog(payload)
    if not models:
        # An empty catalog is treated as a failure, not as "this host has
        # nothing": a shape change would otherwise justify silently abandoning
        # the configured model.
        raise DiscoveryError(f"{endpoint.host}: /models listed no models (unexpected shape?)")
    return Catalog(host=endpoint.host, models=models, fetched_at=time.monotonic())
