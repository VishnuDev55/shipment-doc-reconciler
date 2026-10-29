"""Live writes stay out of the tracked fixture tree, and reads still prefer it.

`fixtures/responses` is git-tracked on purpose: its files are reviewed captures
that DEMO_MODE replays byte-for-byte and docs/ARCHITECTURE.md counts. A live run
used to write its answers into that same directory, which meant two things -- one
`git add -A` could commit another company's shipment as if it were a hand-authored
sample, and a machine-generated file could sit in front of a reviewed one under the
same key.

So the cache is now two things: trees to read, in a fixed order, and one tree to
write. These tests hold both halves of that promise.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from app.cache import ResponseCache
from app.client import ExtractionClient
from app.config import COMMITTED_CACHE_DIR, Settings, cache_write_dir
from app.ingest import ingest
from app.models import Report
from app.narrative_cache import NarrativeCache

REPO = Path(__file__).resolve().parents[1]
IMG = REPO / "fixtures" / "images" / "invoice_clean.png"

from tests.test_client import good_response  # noqa: E402  (the canned 12-field body)


def _tree_state(root: Path) -> dict[str, str]:
    """(relative path -> sha256) for every file under `root`.

    Digests rather than mtimes: Windows ACLs are unreliable enough that "make the
    tree read-only and see if it errors" would pass without proving anything, and
    `_store_success` swallows OSError, so a failed write would look like success.
    """
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# where a write is allowed to land
# ---------------------------------------------------------------------------


def test_reading_the_committed_tree_never_means_writing_to_it() -> None:
    """Break caught: `CACHE_DIR` was both the read location and the write
    location, so a live run added files to a reviewed fixture directory."""
    settings = Settings(_env_file=None, cache_dir=COMMITTED_CACHE_DIR)
    written = cache_write_dir(settings)

    assert written != COMMITTED_CACHE_DIR
    assert COMMITTED_CACHE_DIR not in written.parents


def test_a_cache_dir_of_your_own_still_writes_to_itself() -> None:
    """The exception has to stay narrow. An instance that points CACHE_DIR at a
    volume of its own is not reading reviewed provenance, and making it set a
    second variable to get ordinary behavior would be a tax on everybody who is
    not deploying the demo."""
    settings = Settings(_env_file=None, cache_dir=REPO / "somewhere_else")
    assert cache_write_dir(settings) == REPO / "somewhere_else"


def test_naming_the_write_directory_overrides_the_rule(tmp_path) -> None:
    settings = Settings(
        _env_file=None, cache_dir=COMMITTED_CACHE_DIR, live_cache_dir=tmp_path / "live")
    assert cache_write_dir(settings) == tmp_path / "live"


def test_the_live_cache_directory_is_gitignored() -> None:
    """A directory that is untracked but not ignored is one `git add .` away from
    being committed, which is the failure this whole change exists to prevent."""
    probe = REPO / ".live_cache" / "responses" / "probe.json"
    res = subprocess.run(
        ["git", "check-ignore", "-q", str(probe)],
        cwd=str(REPO), capture_output=True, text=True,
    )
    assert res.returncode == 0, ".live_cache/ must be in .gitignore"


# ---------------------------------------------------------------------------
# the read order
# ---------------------------------------------------------------------------


def _entry(key: str, *, provenance: str, value: str, pages: list[str]) -> dict:
    return {
        "cache_key": key,
        "provider": "nebius",
        "model": "m",
        "prompt_version": "v1",
        "page_sha256": pages,
        "response": {"fields": {"gross_weight": {"value": value}}},
        "raw_text": "",
        "_provenance": provenance,
    }


def test_the_committed_capture_wins_over_a_live_recapture(tmp_path) -> None:
    """Same key, two trees. The reviewed file has to be the one served, or a live
    run could quietly replace the answer the demo is judged on."""
    committed, live = tmp_path / "committed", tmp_path / "live"
    committed.mkdir()
    (committed / "k.json").write_text(
        json.dumps(_entry("k", provenance="HAND-AUTHORED fixture", value="12,450.00 KGS",
                          pages=["p1"])), encoding="utf-8")
    cache = ResponseCache([committed, live], write_dir=live)
    cache.put("k", {"fields": {"gross_weight": {"value": "REWRITTEN BY A MODEL"}}},
              model="m", provider="nebius", page_sha256s=["p1"], raw_text="")

    served = cache.get("k")
    assert served is not None
    assert served["fields"]["gross_weight"]["value"] == "12,450.00 KGS"
    assert (live / "k.json").is_file(), "the write still happened, just not in the tree"


def test_the_authored_fallback_never_walks_the_live_tree(tmp_path) -> None:
    """The page-hash fallback globs a whole directory on every miss. Restricted to
    reviewed trees, that is bounded by what a human committed; left general, it is
    bounded by how long an instance has been live."""
    committed, live = tmp_path / "committed", tmp_path / "live"
    committed.mkdir()
    live.mkdir()
    decoy = _entry("d", provenance="HAND-AUTHORED fixture", value="decoy", pages=["p1"])
    (live / "d.json").write_text(json.dumps(decoy), encoding="utf-8")

    cache = ResponseCache([committed, live], write_dir=live)
    assert cache.find_authored_by_pages(["p1"], prompt_version="v1") is None

    # An exact key in the same live tree still resolves; only the scan is scoped.
    assert cache.get("d") is not None


def test_one_directory_still_means_read_and_write_are_the_same(tmp_path) -> None:
    """The single-path form is what five tests and every script construct. Making
    it mean "read here, write to the repo's live cache" would scatter fixtures
    across the machine depending on who built the object."""
    cache = ResponseCache(tmp_path)
    cache.put("k", {"fields": {}}, model="m", provider="p", page_sha256s=["x"],
              raw_text="t")
    assert (tmp_path / "k.json").is_file()
    assert cache.get("k") is not None


# ---------------------------------------------------------------------------
# a live run, end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_live_run_leaves_the_tracked_fixture_tree_byte_identical(
    tmp_path, monkeypatch
) -> None:
    """Break caught: every live extraction wrote its response into
    `fixtures/responses`, the directory git tracks as reviewed provenance.

    The tracked tree is snapshotted by digest, a live run is driven against a
    faked transport that answers with usage-bearing completions (so the cache
    write and the spend record both fire), and the tree must come back
    indistinguishable while the live directory has grown.
    """
    before = _tree_state(REPO / "fixtures")

    settings = Settings(
        _env_file=None,
        demo_mode=False,
        nvidia_api_key="nvapi-TEST-ONLY-0000-1234",
        vision_provider="nvidia",
        runs_dir=tmp_path / "runs",
        cache_dir=COMMITTED_CACHE_DIR,
        live_cache_dir=tmp_path / "live",
    )
    client = ExtractionClient(settings)

    async def fake_transport(messages, **kw):
        # Records the way the real transport does, so the ledger assertion below
        # is about the run and not about what this fake forgot to do.
        usage = {"prompt": 1200, "completion": 300, "total": 1500}
        client._record_spend(usage, "extraction")
        return good_response(), "json", usage

    monkeypatch.setattr(client, "transport_call", fake_transport)
    client._probed = True
    client.json_mode = "plain"

    pages = ingest(str(IMG.name), IMG.read_bytes(), long_edge=800, max_pages=1)
    outcome = await client.extract_document(document_id="doc1", pages=pages)
    assert outcome.status == "ok", outcome.error

    assert _tree_state(REPO / "fixtures") == before, (
        "a live run changed, added or removed something under fixtures/"
    )
    written = list((tmp_path / "live").rglob("*.json"))
    assert written, f"the run produced no cache entry in {tmp_path / 'live'}"

    ledger = tmp_path / "runs" / "spend_log.json"
    assert ledger.is_file(), "the call was billed, so it has to be in the ledger"


def test_a_narrative_capture_lands_in_the_live_tree(tmp_path) -> None:
    """Three of the committed narratives are hand-authored captures. A live demo
    run regenerating one must not overwrite them."""
    committed = tmp_path / "committed"
    (committed / "narratives").mkdir(parents=True)
    settings = Settings(_env_file=None, cache_dir=committed,
                        live_cache_dir=tmp_path / "live")
    cache = NarrativeCache(settings)

    path = cache.put(Report(shipment_id="s", created_at="now", updated_at="now"),
                     model="m", provider="p", text="a paragraph")
    assert path.is_file()
    assert str(tmp_path / "live") in str(path)
    assert not list((committed / "narratives").glob("*.json"))


# ---------------------------------------------------------------------------
# pruning: off unless asked, and never the committed tree
# ---------------------------------------------------------------------------


def _age(path: Path, hours: float) -> None:
    import os
    import time

    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def test_old_entries_are_pruned_and_fresh_ones_are_kept(tmp_path):
    """Break caught: nothing ever pruned a cache. `purge_older_than` covers the
    runs directory and runs once at startup; the responses a live instance writes
    accumulated until the disk decided."""
    from app.cache import purge_cache_older_than

    cache = tmp_path / "live"
    (cache / "narratives").mkdir(parents=True)
    stale = cache / "old.json"
    stale.write_text("{}", encoding="utf-8")
    fresh = cache / "new.json"
    fresh.write_text("{}", encoding="utf-8")
    stale_narrative = cache / "narratives" / "old.json"
    stale_narrative.write_text("{}", encoding="utf-8")
    _age(stale, 48)
    _age(stale_narrative, 48)

    removed = purge_cache_older_than(cache, 24)

    assert sorted(removed) == ["narratives/old.json", "old.json"]
    assert fresh.is_file(), "a fresh entry is the whole point of a cache"
    assert not stale.is_file()


def test_a_ttl_of_zero_prunes_nothing(tmp_path):
    """The default has to be inert: deleting somebody's cache is a decision, and
    zero is how a deployment says it has not made one."""
    from app.cache import purge_cache_older_than

    cache = tmp_path / "live"
    cache.mkdir()
    stale = cache / "old.json"
    stale.write_text("{}", encoding="utf-8")
    _age(stale, 24 * 365)

    assert purge_cache_older_than(cache, 0) == []
    assert stale.is_file()


def test_the_committed_fixture_tree_is_never_pruned(tmp_path):
    """Break caught: a deployment that points the write directory at the reviewed
    tree (which `scripts/capture_summaries.py` does on purpose) would delete the
    fixtures the demo replays. Pruning refuses that directory by name."""
    from app.cache import purge_cache_older_than

    fixture_copy = tmp_path / "responses"
    fixture_copy.mkdir()
    old = fixture_copy / "captured.json"
    old.write_text("{}", encoding="utf-8")
    _age(old, 24 * 365)

    assert purge_cache_older_than(COMMITTED_CACHE_DIR, 1) == []
    assert old.is_file(), "a scratch tree is prunable; the tracked one is not"


def test_the_reconciler_prunes_the_live_cache_when_a_ttl_is_set(tmp_path):
    """The setting has to reach the cache, or it is decoration."""
    from app.orchestrator import Reconciler

    write_dir = tmp_path / "live"
    write_dir.mkdir()
    stale = write_dir / "old.json"
    stale.write_text("{}", encoding="utf-8")
    _age(stale, 48)

    settings = Settings(_env_file=None, demo_mode=True, runs_dir=tmp_path / "runs",
                        cache_dir=COMMITTED_CACHE_DIR, live_cache_dir=write_dir,
                        live_cache_ttl_hours=24)
    Reconciler(settings)

    assert not stale.is_file()
