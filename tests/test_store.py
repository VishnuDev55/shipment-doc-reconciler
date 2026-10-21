"""The filesystem run store: safe names, atomic writes, tolerant reads.

The store is the database, and a run is other people's commercial data, so the
interesting parts are exactly where a bug would be silent: a filename must not
be able to escape the run directory, a half-written report must never be read,
and one corrupt run must never empty the whole history.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.models import Report
from app.store import RunStore, _safe_name, new_shipment_id


def _report(shipment_id="sid") -> Report:
    report = Report(shipment_id=shipment_id, created_at="now", updated_at="now")
    report.documents = []
    return report


def test_safe_name_keeps_only_the_basename():
    assert _safe_name("invoice.pdf") == "invoice.pdf"
    assert _safe_name("../../etc/passwd") == "passwd"
    assert _safe_name("a/b/c") == "c"
    assert _safe_name("in voice!.pdf") == "in_voice_.pdf"


def test_new_shipment_id_is_unguessable_and_unique():
    ids = {new_shipment_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 16 for i in ids)  # token_urlsafe(12) -> 16 base64 chars


def test_write_then_read_round_trips_without_leaving_a_tmp(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create("sid")
    store.write(_report("sid"))

    assert store.exists("sid")
    read = store.read("sid")
    assert read is not None and read.shipment_id == "sid"
    # The write is tmp-then-rename so a poller never reads a half-written JSON.
    assert list((tmp_path / "runs" / "sid").glob("*.tmp")) == []


def test_an_upload_name_cannot_escape_the_run_directory(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create("sid")
    path = store.save_upload("sid", "../../evil.pdf", b"data")
    # The path stays inside runs/sid/uploads/ and the traversal is gone.
    assert path.parent == store.dir("sid") / "uploads"
    assert path.name.endswith("_evil.pdf")
    assert path.read_bytes() == b"data"


def test_list_shipments_skips_and_counts_a_corrupt_report(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.create("good")
    store.write(_report("good"))
    bad = tmp_path / "runs" / "bad"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "report.json").write_text("{not json", encoding="utf-8")

    rows, skipped = store.list_shipments()
    assert [r["shipment_id"] for r in rows] == ["good"]
    assert skipped == 1


def test_list_shipments_repairs_a_cp1252_run_in_place(tmp_path):
    store = RunStore(tmp_path / "runs")
    legacy = tmp_path / "runs" / "legacy"
    legacy.mkdir(parents=True, exist_ok=True)
    payload = {"shipment_id": "legacy", "state": "done", "documents": [],
               "summary": {"conflict": 1}, "updated_at": "x"}
    (legacy / "report.json").write_bytes(json.dumps(payload).encode("cp1252"))

    rows, skipped = store.list_shipments()
    assert skipped == 0
    assert [r["shipment_id"] for r in rows] == ["legacy"]
    # The repair is in place: the file is now valid UTF-8, done once not per request.
    (legacy / "report.json").read_text(encoding="utf-8")


def test_purge_older_than_removes_only_old_runs(tmp_path):
    store = RunStore(tmp_path / "runs")
    for sid, age_hours in (("old", 48), ("new", 0)):
        store.create(sid)
        created = (datetime.now(timezone.utc) - timedelta(hours=age_hours))
        store.write_meta(sid, {"created_at": created.isoformat(timespec="seconds")})
        (store.dir(sid) / "report.json").write_text("{}", encoding="utf-8")

    assert store.purge_older_than(hours=24) == ["old"]
    assert not store.exists("old")
    assert store.exists("new")
