"""Cache for AI-written executive summaries.

Keyed on a *verdict signature* rather than page hashes: the summary describes
the comparison outcome, so two shipments that reconcile to the same set of
verdicts and numbers get the same summary. That keeps DEMO_MODE able to replay
a genuinely model-written narrative without a key, exactly as the extraction
cache does.

Stored beside the extraction cache but in its own subdirectory, so the two
never collide on a filename.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import Settings, cache_write_dir
from .models import Report
from .summary import SUMMARY_VERSION


def verdict_signature(report: Report) -> str:
    """Stable fingerprint of everything the summary is derived from."""
    parts = [f"{c.field}={c.status}" for c in sorted(report.comparisons, key=lambda c: c.field)]
    # Numbers quoted in reasons are part of the claim, so include them: a
    # different weight discrepancy should not reuse the same summary.
    parts += [f"reason:{c.field}={c.reason}" for c in sorted(report.comparisons, key=lambda c: c.field)]
    s = report.summary
    parts.append(f"counts:{s.conflict}/{s.suspicious}/{s.consistent}/{s.single_source}/{s.absent}")
    parts.append(f"docs:{s.documents_ok}/{s.documents_failed}")
    parts.append(f"verdict:{s.verdict}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def narrative_key(report: Report, model: str) -> str:
    return hashlib.sha256(
        f"{SUMMARY_VERSION}\x00{model}\x00{verdict_signature(report)}".encode()
    ).hexdigest()[:32]


class NarrativeCache:
    """Written summaries, read from the committed tree first and written to the
    live one. Same split, and for the same reason, as `ResponseCache`: three of
    these narratives are committed fixtures whose provenance the report claims."""

    def __init__(self, settings: Settings):
        write_root = cache_write_dir(settings)
        self.read_dirs = [
            Path(settings.cache_dir) / "narratives",
            Path(write_root) / "narratives",
        ]
        self.write_dir = self.read_dirs[-1]
        # Scripts reach for `.directory` when they report where a capture landed.
        self.directory = self.read_dirs[0]

    def path(self, key: str) -> Path:
        return self.write_dir / f"{key}.json"

    def _find(self, key: str) -> Path | None:
        name = f"{key}.json"
        for directory in self.read_dirs:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        return None

    def get(self, report: Report, model: str) -> dict[str, Any] | None:
        path = self._find(narrative_key(report, model))
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return {
            "text": text,
            "model": payload.get("model", model),
            "provider": payload.get("provider", ""),
            "authored": "HAND-AUTHORED" in str(payload.get("_provenance", "")).upper(),
        }

    def put(self, report: Report, *, model: str, provider: str, text: str) -> Path:
        self.write_dir.mkdir(parents=True, exist_ok=True)
        path = self.path(narrative_key(report, model))
        path.write_text(
            json.dumps(
                {
                    "key": path.stem,
                    "model": model,
                    "provider": provider,
                    "summary_version": SUMMARY_VERSION,
                    "verdict_signature": verdict_signature(report),
                    "text": text,
                    "_provenance": "live API narrative",
                },
                indent=2,
            )
        )
        return path
