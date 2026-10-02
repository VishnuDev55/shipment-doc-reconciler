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
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .models import Report


def new_shipment_id() -> str:
    """Unguessable ID is the whole access-control story for a public demo."""
    return secrets.token_urlsafe(12)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, shipment_id: str) -> Path:
        return self.root / shipment_id

    def exists(self, shipment_id: str) -> bool:
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
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(report.model_dump_json(indent=2))
        tmp.replace(path)
        return path

    def read(self, shipment_id: str) -> Report | None:
        path = self.dir(shipment_id) / "report.json"
        if not path.is_file():
            return None
        try:
            return Report.model_validate_json(path.read_text())
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

    def list_shipments(self, limit: int = 20) -> list[dict]:
        rows = []
        for d in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            report = d / "report.json"
            if not report.is_file():
                continue
            try:
                data = json.loads(report.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            rows.append(
                {
                    "shipment_id": d.name,
                    "state": data.get("state"),
                    "documents": len(data.get("documents", [])),
                    "summary": data.get("summary", {}),
                    "updated_at": data.get("updated_at"),
                }
            )
            if len(rows) >= limit:
                break
        return rows


def _safe_name(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_." else "_" for c in Path(name).name)
    return keep[:80] or "file"
