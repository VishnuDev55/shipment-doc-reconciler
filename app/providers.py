"""Provider endpoints.

Two different hosts, two different jobs:

  * VISION   — reads the document images. Default is NVIDIA's Nemotron omni
    model on build.nvidia.com. MiniCPM-V on Nebius Token Factory is a
    configurable fallback (VISION_PROVIDER=nebius).
  * SUMMARY  — one optional plain-English paragraph per shipment, on a Nebius
    Token Factory Nemotron text model. This is the runtime call that satisfies
    the "runs on Nebius Token Factory" requirement.

Both speak an OpenAI-compatible /chat/completions API, but neither implements
it identically — see client.py for the shapes that were verified live.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config import Settings

ProviderName = Literal["nvidia", "nebius"]

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1/"

# Defaults, not assumptions. app/discovery.py checks each host's /models list
# before a run extracts and adopts a served model if this name is gone — which
# is not hypothetical: the model this brief asked for, and the one recorded
# below as verified on 2026-09-23, both went away mid-project (410 Gone on
# build.nvidia.com, 404 on Token Factory).
NVIDIA_VISION_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"

# Nebius fallback vision model. Live-verified earlier: text+image->text,
# json_mode + structured_outputs, 32K context.
NEBIUS_FALLBACK_VISION_MODEL = "openbmb/MiniCPM-V-4_5"

# Nebius-hosted NVIDIA open model for the executive summary. Live-verified:
# returns clean prose in `content`. Nemotron-3_5-Lightning was rejected — it
# dumps its chain-of-thought into `content` and hit the token cap.
NEBIUS_SUMMARY_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


@dataclass(frozen=True)
class Endpoint:
    """Everything needed to call one model on one host."""

    provider: ProviderName
    base_url: str
    api_key: str
    model: str
    label: str

    @property
    def host(self) -> str:
        return self.base_url.split("//", 1)[-1].split("/", 1)[0]

    def __str__(self) -> str:  # never include the key
        return f"{self.label}={self.provider}:{self.model}@{self.host}"


def vision_endpoint(settings: Settings) -> Endpoint:
    """The model that reads document images."""
    if settings.vision_provider == "nebius":
        # NEBIUS_VISION_MODEL wins; NEBIUS_MODEL is the older name for the same
        # thing and keeps existing .env files working.
        model = (
            settings.nebius_vision_model
            or settings.nebius_model
            or NEBIUS_FALLBACK_VISION_MODEL
        )
        return Endpoint(
            provider="nebius",
            base_url=settings.nebius_base_url,
            api_key=settings.nebius_api_key,
            model=model,
            label="vision",
        )
    return Endpoint(
        provider="nvidia",
        base_url=settings.nvidia_base_url,
        api_key=settings.nvidia_api_key,
        model=settings.nvidia_vision_model or NVIDIA_VISION_MODEL,
        label="vision",
    )


def summary_endpoint(settings: Settings) -> Endpoint:
    """The model that writes the optional executive summary. Always Nebius."""
    return Endpoint(
        provider="nebius",
        base_url=settings.nebius_base_url,
        api_key=settings.nebius_api_key,
        model=settings.nebius_summary_model or NEBIUS_SUMMARY_MODEL,
        label="summary",
    )
