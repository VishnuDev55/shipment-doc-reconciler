"""The runtime-configuration view: masked keys and the config-token gate.

A key is never returned in full: on a public deployment a readable config
endpoint would turn "anyone can open the page" into "anyone can read your
provider key". The mask is also how POST tells "left alone" from "a key someone
actually typed".
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from .auth import secret_matches
from .config import _usable

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
    from .main import reconciler  # local import: main imports this module

    expected = reconciler().settings.config_token.strip()
    if not expected:
        return
    supplied = request.headers.get("x-config-token", "")
    if not secret_matches(supplied, expected):
        raise HTTPException(401, "a valid X-Config-Token header is required for this deployment")


def config_view() -> dict:
    from .main import reconciler  # local import: main imports this module

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


def sanitize_config_updates(updates: dict) -> dict:
    """Drop masked placeholders and refuse template keys before a reconfigure.

    Mutates and returns ``updates``: a masked field is removed (the browser
    re-submits the mask to mean "left alone"), and a real-looking placeholder is
    refused rather than stored.
    """
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
    return updates
