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
    def accepts_images(self) -> bool:
        # Nebius reports "text+image->text"; NVIDIA's /v1/models uses "type".
        # Matching on the presence of "image" before the arrow covers both.
        left = self.modality.split("->", 1)[0].lower() if self.modality else ""
        return "image" in left or "vision" in self.modality.lower()

    def speaks_text(self) -> bool:
        right = self.modality.split("->", 1)[1].lower() if "->" in self.modality else ""
        return (not right) or "text" in right


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


def parse_catalog(payload: object) -> list[ModelInfo]:
    """Accept both envelope shapes these hosts actually return.

    NVIDIA wraps in {"models": [...]}, the OpenAI-compatible Nebius in
    {"data": [...]}. Reading only one would make the other look like a host
    with zero models, and "zero models" would silently keep a dead default.
    """
    if isinstance(payload, dict):
        rows = payload.get("models") or payload.get("data") or []
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
        modality = row.get("modality") or row.get("type") or row.get("supported_content") or ""
        if isinstance(modality, list):
            modality = "+".join(str(m) for m in modality)
        out.append(ModelInfo(id=str(ident), modality=str(modality),
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

    Sorted by preference band then id, so two runs on the same catalog choose the
    same model; a set-iteration order would make the choice unrepeatable.
    """
    eligible = [m for m in models if (m.accepts_images if role == "vision" else m.speaks_text())]
    if not eligible:
        return None
    patterns = _VISION_PREFERRED if role == "vision" else _SUMMARY_PREFERRED
    return sorted(eligible, key=lambda m: (_score(m, patterns), m.id))[0]


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
    present = {m.id for m in catalog}

    for candidate in allowed:
        if candidate in present:
            return Selection(
                role=role, provider=provider, host=host, requested=requested,
                chosen=candidate, status="configured",
                note="selected from MODEL_ALLOW list" if candidate != requested else "",
            )

    if requested in present:
        return Selection(
            role=role, provider=provider, host=host, requested=requested,
            chosen=requested, status="configured", available=available,
        )

    best = pick(catalog, role)
    if best is None:
        return Selection(
            role=role, provider=provider, host=host, requested=requested,
            chosen=requested, status="unverified", available=available,
            note=(f"'{requested}' is not listed by {host}, and {host} reports no "
                  f"model that accepts {'images' if role == 'vision' else 'text output'}"),
        )

    return Selection(
        role=role, provider=provider, host=host, requested=requested,
        chosen=best.id, status="discovered", available=available,
        note=f"'{requested}' is not served by {host}; using '{best.id}'",
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
