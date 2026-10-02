"""Response cache.

Keyed on sha256(provider + model + prompt_version + page hashes), so a response
served by NVIDIA build.nvidia.com can never be mistaken for one served by Nebius
Token Factory even if both ever expose the same model id.

Two lookup tiers:

  1. exact key hit — the normal path, and how live responses are reused;
  2. authored fallback — hand-written fixtures are matched on page hashes
     alone, because they are not model output and must keep working whatever
     VISION_PROVIDER / NEBIUS_MODEL is configured. Live responses are NEVER
     eligible for this tier.

Purposes: iterate on normalization/comparison in milliseconds, back DEMO_MODE
with no key, and avoid re-billing a document that already succeeded.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import PROMPT_VERSION

AUTHORED_MARKER = "HAND-AUTHORED"


def cache_key(
    page_sha256s: list[str],
    model: str,
    provider: str = "",
    prompt_version: str = PROMPT_VERSION,
) -> str:
    h = hashlib.sha256()
    for part in (prompt_version, provider, model):
        h.update(part.encode())
        h.update(b"\x00")
    for s in page_sha256s:
        h.update(s.encode())
        h.update(b"\x00")
    return h.hexdigest()[:32]


class ResponseCache:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def _load(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            # A corrupt cache entry is never fatal; treat it as a miss.
            return None
        return payload if isinstance(payload, dict) else None

    def is_authored(self, payload: dict[str, Any]) -> bool:
        return AUTHORED_MARKER in str(payload.get("_provenance", "")).upper()

    def get(
        self,
        key: str,
        *,
        page_sha256s: list[str] | None = None,
        provider: str = "",
        model: str = "",
    ) -> dict[str, Any] | None:
        """Return the stored response object, or None.

        Exact key first. If that misses, fall back to a hand-authored fixture
        covering the same pages — model- and provider-agnostic by design, so
        flipping VISION_PROVIDER does not silently break DEMO_MODE.
        """
        payload = self._load(self.path(key)) if self.path(key).is_file() else None

        if payload is None and page_sha256s:
            payload = self._find_authored(page_sha256s)

        if payload is None:
            return None
        response = payload.get("response")
        return response if isinstance(response, dict) else None

    def _find_authored(self, page_sha256s: list[str]) -> dict[str, Any] | None:
        if not self.directory.is_dir():
            return None
        for path in sorted(self.directory.glob("*.json")):
            payload = self._load(path)
            if payload is None or not self.is_authored(payload):
                continue
            if payload.get("page_sha256") == list(page_sha256s):
                return payload
        return None

    def find_authored_by_pages(self, page_sha256s: list[str]) -> dict[str, Any] | None:
        """Public accessor for the authored tier (used by tests and scripts)."""
        payload = self._find_authored(page_sha256s)
        if payload is None:
            return None
        response = payload.get("response")
        return response if isinstance(response, dict) else None

    def provenance_of(
        self, key: str, *, page_sha256s: list[str] | None = None
    ) -> str:
        """'authored' | 'live' | 'none' — who produced the values we would serve.

        Used to label a report honestly: replaying a hand-authored fixture is
        not evidence that a model can read the document.
        """
        payload = self._load(self.path(key)) if self.path(key).is_file() else None
        if payload is None and page_sha256s:
            payload = self._find_authored(page_sha256s)
        if payload is None:
            return "none"
        return "authored" if self.is_authored(payload) else "live"

    def prompt_version_of(
        self, key: str, *, page_sha256s: list[str] | None = None
    ) -> str:
        """Which extraction contract produced the entry we would serve.

        A cache entry written under an older prompt may predate fields that
        exist now. Reading it lets the report say "these fields were not
        extracted at capture time" instead of showing them as absent and
        implying the document did not state them.
        """
        payload = self._load(self.path(key)) if self.path(key).is_file() else None
        if payload is None and page_sha256s:
            payload = self._find_authored(page_sha256s)
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("prompt_version") or "")

    def put(
        self,
        key: str,
        response: dict[str, Any],
        *,
        model: str,
        provider: str,
        page_sha256s: list[str],
        raw_text: str,
        provenance: str = "live API response",
    ) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path(key)
        path.write_text(
            json.dumps(
                {
                    "cache_key": key,
                    "provider": provider,
                    "model": model,
                    "prompt_version": PROMPT_VERSION,
                    "page_sha256": page_sha256s,
                    "response": response,
                    "raw_text": raw_text[:20000],
                    "_provenance": provenance,
                },
                indent=2,
            )
        )
        return path

    def keys(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(p.stem for p in self.directory.glob("*.json"))
