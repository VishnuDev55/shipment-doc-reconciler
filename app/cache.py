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
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import COMMITTED_CACHE_DIR, PROMPT_VERSION

AUTHORED_MARKER = "HAND-AUTHORED"


def purge_cache_older_than(directory: Path, hours: int) -> list[str]:
    """Delete cache entries older than `hours`. Returns the relative names.

    Zero -- the default -- keeps everything, the same rule `RUN_TTL_HOURS` uses:
    deleting somebody's cache is a decision, and a deployment makes it by setting
    a number, not by upgrading.

    The committed fixture tree is refused by name. It is tracked, reviewed
    provenance that the demo replays byte-for-byte, and one script points its
    write directory at it on purpose -- so a `LIVE_CACHE_TTL_HOURS` set on a
    machine whose `LIVE_CACHE_DIR` was left pointing at the fixtures must be a
    no-op, not a deletion.
    """
    if hours <= 0:
        return []
    root = Path(directory)
    try:
        if root.resolve() == COMMITTED_CACHE_DIR.resolve():
            return []
    except OSError:
        return []
    if not root.is_dir():
        return []

    cutoff = time.time() - hours * 3600
    removed: list[str] = []
    for path in sorted(root.rglob("*.json")):
        try:
            if path.stat().st_mtime > cutoff:
                continue
            path.unlink()
        except OSError:
            continue
        removed.append(str(path.relative_to(root)).replace("\\", "/"))

    # Leave the directory as it was found, minus what was pruned: an empty
    # narratives/ is recreated by the next put, and thousands of empty directories
    # are worse than none.
    for child in sorted((p for p in root.rglob("*") if p.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
        try:
            child.rmdir()
        except OSError:
            pass
    return removed


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
    """Model responses, read from one or more trees and written to one.

    The split exists because the committed tree is reviewed provenance and a live
    run is not. `fixtures/responses` is git-tracked on purpose -- 36 of its files
    are deliberate captures that docs/ARCHITECTURE.md counts and the demo replays
    byte-for-byte -- and until now a live run wrote its own answers into that same
    directory. One `git add -A` then committed somebody's real shipment as if it
    were a hand-authored fixture, and the "this run came from a captured
    response" claim stopped meaning anything you could check.

    Reads check the committed tree *first*, so a reviewed capture can never be
    shadowed by a machine-generated file of the same key.
    """

    def __init__(self, directory: Path | Sequence[Path], write_dir: Path | None = None):
        # A bare Path keeps the ~5 existing construction sites (and every test
        # that hands in a tmp_path) working: read and write are then the same
        # directory, which is what each of those callers means.
        if isinstance(directory, (str, Path)):
            self.read_dirs: list[Path] = [Path(directory)]
        else:
            self.read_dirs = [Path(d) for d in directory]
        self.write_dir = Path(write_dir) if write_dir is not None else self.read_dirs[0]
        # Kept for callers that read `.directory` (scripts do): the tree a lookup
        # is most likely to answer from, which is the committed one.
        self.directory = self.read_dirs[0]
        # The authored fallback may only scan reviewed trees. A live tree is
        # unbounded -- it grows with every run anybody makes against this
        # instance -- and globbing it on every cache miss would turn a demo
        # lookup into a directory walk. In the single-directory case there is no
        # separate live tree, so that one directory is both.
        self.authored_dirs = [d for d in self.read_dirs if d != self.write_dir] or [
            self.write_dir]

    def path(self, key: str) -> Path:
        """Where a *new* entry for this key would go: always the write tree."""
        return self.write_dir / f"{key}.json"

    def read_path(self, key: str) -> Path | None:
        """The first tree that actually holds this key, or None.

        Order is the security-relevant part: the committed capture is consulted
        before anything a live run produced.
        """
        name = f"{key}.json"
        for directory in self.read_dirs:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        return None

    def _load(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            # A corrupt cache entry is never fatal; treat it as a miss.
            return None
        return payload if isinstance(payload, dict) else None

    def is_authored(self, payload: dict[str, Any]) -> bool:
        return AUTHORED_MARKER in str(payload.get("_provenance", "")).upper()

    def _read_payload(self, key: str) -> dict[str, Any] | None:
        """The stored entry for `key`, from the first tree that has it."""
        found = self.read_path(key)
        return self._load(found) if found is not None else None

    def get(
        self,
        key: str,
        *,
        page_sha256s: list[str] | None = None,
        provider: str = "",
        model: str = "",
        prompt_version: str = PROMPT_VERSION,
    ) -> dict[str, Any] | None:
        """Return the stored response object, or None.

        Exact key first. If that misses, fall back to a hand-authored fixture
        covering the same pages — model- and provider-agnostic by design, so
        flipping VISION_PROVIDER does not silently break DEMO_MODE. Live
        responses are NEVER eligible for this tier.
        """
        payload = self._read_payload(key)

        if payload is None and page_sha256s:
            payload = self._find_authored(page_sha256s, prompt_version=prompt_version)

        if payload is None:
            return None
        response = payload.get("response")
        return response if isinstance(response, dict) else None

    def _find_authored(
        self, page_sha256s: list[str], *, prompt_version: str = PROMPT_VERSION
    ) -> dict[str, Any] | None:
        """Authored fixtures for these pages, under this contract only.

        The page-hash match has to be qualified by prompt version: a line-item
        fixture and a scalar fixture cover the same images, so an unqualified
        fallback would hand the goods-lines shape to a lookup that wants the
        twelve-field object. The shape it would return parses — every key just
        comes back absent — so the damage would be invisible.
        """
        candidates = [d for d in self.authored_dirs if d.is_dir()]
        for directory in candidates:
            for path in sorted(directory.glob("*.json")):
                payload = self._load(path)
                if payload is None or not self.is_authored(payload):
                    continue
                if payload.get("prompt_version") != prompt_version:
                    continue
                if payload.get("page_sha256") == list(page_sha256s):
                    return payload
        return None

    def find_authored_by_pages(
        self, page_sha256s: list[str], *, prompt_version: str = PROMPT_VERSION
    ) -> dict[str, Any] | None:
        """Public accessor for the authored tier (used by tests and scripts)."""
        payload = self._find_authored(page_sha256s, prompt_version=prompt_version)
        if payload is None:
            return None
        response = payload.get("response")
        return response if isinstance(response, dict) else None

    def provenance_of(
        self,
        key: str,
        *,
        page_sha256s: list[str] | None = None,
        prompt_version: str = PROMPT_VERSION,
    ) -> str:
        """'authored' | 'live' | 'none' — who produced the values we would serve.

        Used to label a report honestly: replaying a hand-authored fixture is
        not evidence that a model can read the document.
        """
        payload = self._read_payload(key)
        if payload is None and page_sha256s:
            payload = self._find_authored(page_sha256s, prompt_version=prompt_version)
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
        payload = self._read_payload(key)
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
        prompt_version: str = PROMPT_VERSION,
    ) -> Path:
        self.write_dir.mkdir(parents=True, exist_ok=True)
        path = self.path(key)
        path.write_text(
            json.dumps(
                {
                    "cache_key": key,
                    "provider": provider,
                    "model": model,
                    "prompt_version": prompt_version,
                    "page_sha256": page_sha256s,
                    "response": response,
                    "raw_text": raw_text[:20000],
                    "_provenance": provenance,
                },
                indent=2,
            )
        )
        return path
