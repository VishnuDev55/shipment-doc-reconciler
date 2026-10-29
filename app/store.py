"""Filesystem-backed run store. No database.

./runs/{shipment_id}/
    report.json        the single polled artifact
    meta.json          creation time, declared doc types
    uploads/           original bytes, for reproducibility
    pages/             normalized page JPEGs, served as evidence
    raw/               every model response, including failed attempts
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .models import Report

log = logging.getLogger("sdr.store")

# Enough to outlast a held handle, small enough that a genuinely stuck file
# still surfaces as an error instead of a stalled request.
_REPLACE_ATTEMPTS = 5

# The shape of a run id, and therefore the shape of a run directory name.
# `new_shipment_id()` emits 16 chars from A-Za-z0-9-_ ; the class also admits
# short hand-written ids that tests and scripts pass straight to RunStore.
#
# No dot and no separator is the security property: dir() is where every
# caller-supplied id meets the filesystem, and rejecting "." / ".." / "a/b"
# there means no route, export, or script can join its way out of runs/ by
# naming a directory. Length is capped at 24 so an absurd id cannot become an
# absurd path.
_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,24}\Z")


def new_shipment_id() -> str:
    """Unguessable ID is the whole access-control story for a public demo."""
    return secrets.token_urlsafe(12)


def is_shipment_id(value: str) -> bool:
    """True if `value` is shaped like a run id, i.e. can name a run directory."""
    return bool(_ID_RE.match(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Resolved once, so every containment check downstream is measured
        # against an absolute runs root: `is_relative_to` is only trustworthy
        # when neither side depends on the cwd or an unresolved symlink.
        self.root_resolved = self.root.resolve()
        # Set by the reconciler to fan report changes out to SSE listeners.
        # Optional by design: scripts and tests write runs without a server.
        self.on_write: Callable[[Report], None] | None = None

    def purge_older_than(self, hours: int) -> list[str]:
        """Delete runs whose meta says they are older than `hours`. Returns the ids.

        Age comes from meta.json's created_at, falling back to the directory's
        own mtime for runs written before meta existed. Anything unreadable is
        left alone: this deletes other people's evidence, so a doubtful case is
        a keep, never a remove.
        """
        from datetime import timedelta

        if hours <= 0 or not self.root.is_dir():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        removed: list[str] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            created = self._created_at(entry)
            if created is None or created > cutoff:
                continue
            try:
                shutil.rmtree(entry)
                removed.append(entry.name)
            except OSError as exc:  # pragma: no cover -- disk/permissions
                log.warning("could not purge run %s: %s", entry.name, exc)
        return removed

    def _created_at(self, run_dir: Path):
        try:
            meta = json.loads((run_dir / "meta.json").read_text())
            stamp = str(meta.get("created_at") or "")
            if stamp:
                return datetime.fromisoformat(stamp)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        try:
            return datetime.fromtimestamp(run_dir.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    def dir(self, shipment_id: str) -> Path:
        """The run directory for an id, always inside the runs root.

        Raises ValueError if the id is not a run id. This is the one join
        between a caller-supplied id and the filesystem, so validating here is
        what keeps every route, export and script that reaches a run honest --
        an id of ".." used to resolve to the repository itself.
        """
        if not _ID_RE.match(shipment_id):
            raise ValueError("not a shipment id")
        return self.root_resolved / shipment_id

    def exists(self, shipment_id: str) -> bool:
        """Whether a run has a report. False for an illegal id, not an error:
        callers use this as the gate before they touch the filesystem."""
        if not _ID_RE.match(shipment_id):
            return False
        return (self.dir(shipment_id) / "report.json").is_file()

    def create(self, shipment_id: str) -> Path:
        d = self.dir(shipment_id)
        for sub in ("uploads", "pages", "raw"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        return d

    def write(self, report: Report) -> Path:
        report.updated_at = _now()
        path = self.dir(report.shipment_id) / "report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: the frontend polls this file, and a half-written
        # JSON read mid-poll would surface as a spurious parse error.
        #
        # The temp name is unique per write, not fixed. Every settled document
        # of one shipment funnels through this method from the thread pool, and
        # with a shared name the first rename moves the file the second one is
        # about to rename: that writer then fails on a file it can no longer
        # find, and the report loses an update.
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".report-", suffix=".tmp")
        tmp = Path(name)
        try:
            # Pinned to utf-8, not the locale default. Report text carries the values
            # as printed -- umlauts in a consignee, an em dash in a reason -- and on
            # Windows the default codec is cp1252, which writes those as bytes no
            # UTF-8 reader can decode. A run directory then stops being portable
            # between this machine and the container it deploys to.
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(report.model_dump_json(indent=2))
            _replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)  # already gone once the rename succeeded
        if self.on_write is not None:
            # Every mutation of a report already funnels through here, so this is
            # the one place a progress listener can hook without touching the
            # dozen call sites that settle a run.
            self.on_write(report)
        return path

    def read(self, shipment_id: str) -> Report | None:
        # A malformed id names no run, so it reads as one rather than raising:
        # every caller already turns None into a 404, and a ValueError here
        # would surface to a browser as a 500 for a URL nobody can fix.
        if not is_shipment_id(shipment_id):
            return None
        path = self.dir(shipment_id) / "report.json"
        if not path.is_file():
            return None
        try:
            return Report.model_validate_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return None

    def write_meta(self, shipment_id: str, meta: dict) -> None:
        (self.dir(shipment_id) / "meta.json").write_text(json.dumps(meta, indent=2))

    def read_meta(self, shipment_id: str) -> dict:
        path = self.dir(shipment_id) / "meta.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def save_upload(self, shipment_id: str, filename: str, data: bytes) -> Path:
        safe = _safe_name(filename)
        path = self.dir(shipment_id) / "uploads" / f"{secrets.token_hex(3)}_{safe}"
        path.write_bytes(data)
        return path

    def save_pages(self, shipment_id: str, document_id: str, pages) -> list[str]:
        """Persist normalized page JPEGs and return web-relative paths."""
        out: list[str] = []
        base = self.dir(shipment_id) / "pages"
        base.mkdir(parents=True, exist_ok=True)
        for page in pages:
            name = f"{_safe_name(document_id)}_{page.index}.jpg"
            (base / name).write_bytes(page.jpeg)
            out.append(f"pages/{name}")
        return out

    def list_shipments(self, limit: int = 20) -> tuple[list[dict], int]:
        """Readable runs plus a count of runs that could not be read.

        One unreadable report must never empty the whole history. Older runs on
        Windows were written in the locale codec, so a report can be valid JSON
        that is not valid UTF-8; that case is repaired in place. Genuinely
        corrupt files are skipped and counted, because hiding them entirely is
        how a corrupt run becomes invisible instead of visible.
        """
        rows: list[dict] = []
        skipped = 0
        for d in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            report = d / "report.json"
            if not report.is_file():
                continue
            try:
                raw = report.read_bytes()
            except OSError as exc:
                skipped += 1
                log.warning("could not read run %s: %s", d.name, exc)
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                # A legacy run written with the Windows locale codec. Re-save it
                # as UTF-8 so the repair happens once, not on every request.
                try:
                    text = raw.decode("cp1252")
                    report.write_text(text, encoding="utf-8")
                except (UnicodeDecodeError, OSError) as exc:
                    skipped += 1
                    log.warning("could not decode run %s: %s", d.name, exc)
                    continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                skipped += 1
                log.warning("could not parse run %s: %s", d.name, exc)
                continue
            if not isinstance(data, dict):
                skipped += 1
                log.warning("run %s has a non-object report", d.name)
                continue
            rows.append(
                {
                    "shipment_id": d.name,
                    "state": data.get("state"),
                    "documents": len(data.get("documents", []) or []),
                    "summary": data.get("summary") or {},
                    "updated_at": data.get("updated_at"),
                }
            )
            if len(rows) >= limit:
                break
        return rows, skipped


def _safe_name(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_." else "_" for c in Path(name).name)
    return keep[:80] or "file"


def _replace(tmp: Path, path: Path) -> None:
    """Rename into place, tolerating the Windows rename-onto-a-held-file error.

    Every settled document funnels a write for the same shipment through the
    thread pool, so two threads can reach ``report.json`` at once. POSIX silently
    replaces the target; Windows raises PermissionError for the instant another
    handle is still open on it. Retrying a bounded number of times turns that
    race into a slightly later write; a plain loop would turn it into a hang, so
    the last attempt propagates.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
