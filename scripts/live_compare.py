"""Live-vs-authored comparison.

Runs bundled fixtures through the REAL pipeline with an EMPTY cache dir, so
every extraction is a genuine API call, then diffs the live response against
the hand-authored cache entry for the same document.

    DEMO_MODE=false NEBIUS_MODEL=<vision-model> python scripts/live_compare.py
    python scripts/live_compare.py --diff-only   # re-check captures, no API calls

Writes real live responses into fixtures/live_responses/<provider>/ so they can
be reused as regression fixtures. Running it once per VISION_PROVIDER keeps both
hosts' genuine output on disk instead of letting the second overwrite the first.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cache import cache_key  # noqa: E402
from app.providers import vision_endpoint  # noqa: E402
from app.client import ExtractionClient  # noqa: E402
from app.config import PROMPT_VERSION, Settings  # noqa: E402
from app.ingest import ingest  # noqa: E402
from app.cache import AUTHORED_MARKER  # noqa: E402
from app.models import ExtractionResponse  # noqa: E402
from app.normalize import normalize_field  # noqa: E402

AUTHORED_DIR = ROOT / "fixtures" / "responses"
BUNDLE_DIR = AUTHORED_DIR  # what DEMO_MODE replays; refreshed with --seed-cache
LIVE_DIR = ROOT / "fixtures" / "live_responses"
IMAGES = ROOT / "fixtures" / "images"


def load_authored(page_sha: list[str], model_note: str) -> dict | None:
    """Find the hand-authored entry for these pages.

    The authored cache is keyed on the model in .env at build time, which may
    differ from the live model, so page hashes are the join. But only an
    authored entry may be the baseline: the same directory holds live captures of
    these very pages, and matching on hashes alone returned another provider's
    model output as "authored" -- which turned a model-vs-fixture comparison into
    a model-vs-model one, and made the diff count depend on which filename
    sorted first.
    """
    for path in sorted(AUTHORED_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if AUTHORED_MARKER not in str(payload.get("_provenance", "")).upper():
            continue
        if payload.get("page_sha256") == page_sha:
            return payload.get("response")
    return None


def print_diff(live: ExtractionResponse, authored: dict | None) -> int:
    """One document's authored-vs-live comparison, printed. Returns diff count."""
    if authored is None:
        print("     no authored entry matched these page hashes")
        return 0
    total = 0
    if live.doc_type != authored["doc_type"]:
        print(f"     DIFF doc_type: live={live.doc_type} "
              f"authored={authored['doc_type']}")
        total += 1
    else:
        print(f"     match doc_type           {live.doc_type!r}")
    for key, field in live.fields.items():
        notes = diff_field(key, field, authored["fields"])
        for note in notes:
            print(f"     DIFF {key}: {note}")
        total += len(notes)
        if not notes and field.present:
            norm = normalize_field(key, field)
            print(f"     match {key:18} {field.value!r:26} -> {norm.canonical!r}")
    return total


def diff_existing(out_dir: Path) -> int:
    """Re-compare captures already on disk. Zero API calls.

    A comparison bug should be fixable without re-billing eighteen extractions to
    find out whether the data changed or the tool did.
    """
    files = [f for f in sorted(out_dir.glob("*.json"))]
    if not files:
        print(f"nothing captured in {out_dir.relative_to(ROOT)} yet")
        return 1
    total = 0
    for path in files:
        payload = json.loads(path.read_text())
        live = ExtractionResponse.model_validate(payload["response"])
        print(f"\n  {path.stem}  ({payload.get('provider')}/{payload.get('model')}"
              f", {payload.get('prompt_version')})")
        total += print_diff(
            live, load_authored(payload.get("page_sha256") or [], live.doc_type)
        )
    print(f"\n{len(files)} captured document(s) re-compared offline. "
          f"{total} field-level differences vs authored.")
    return 0


async def run_doc(client: ExtractionClient, fname: str, doc_type: str) -> dict:
    path = IMAGES / fname
    pages = ingest(path.name, path.read_bytes())
    outcome = await client.extract_document(
        document_id=path.stem, pages=pages, declared_doc_type=doc_type
    )
    return {"outcome": outcome, "pages": pages}


def diff_field(key: str, live, authored_fields: dict) -> list[str]:
    notes = []
    auth = authored_fields.get(key)
    if auth is None:
        return [f"{key}: not present in authored data"]

    if live.present != auth["present"]:
        notes.append(f"present: live={live.present} authored={auth['present']}")
        return notes
    if not live.present:
        return notes
    if (live.value or "") != (auth["value"] or ""):
        notes.append(f"value: live={live.value!r} authored={auth['value']!r}")
    if live.page_index != auth["page_index"]:
        notes.append(f"page: live={live.page_index} authored={auth['page_index']}")
    if abs(live.confidence - auth["confidence"]) > 0.25:
        notes.append(f"confidence: live={live.confidence} authored={auth['confidence']}")
    return notes


async def main() -> int:
    settings = Settings()
    ep = vision_endpoint(settings)
    if not ep.api_key.strip():
        print(f"BLOCKED: no API key for vision provider '{ep.provider}'")
        return 2
    print(f"vision: {ep.model} @ {ep.host}")

    out_dir = LIVE_DIR / ep.provider
    out_dir.mkdir(parents=True, exist_ok=True)
    # An EMPTY scratch cache, rebuilt every run, so every call here is a real
    # one. Pointing the client at the bundled cache would not achieve that: the
    # cache also falls back to a hand-authored fixture matched on page hashes
    # alone, which is how an earlier version of this script "captured" nine
    # authored responses and stamped them "LIVE API response". --seed-cache
    # therefore copies finished live entries in afterwards, not captures in place.
    # One scratch dir per provider: two capture runs (one per host) must not
    # clear each other's in-flight cache or mix their seeded entries.
    cache_home = ROOT / ".live_runcache" / ep.provider
    if cache_home.is_dir():
        shutil.rmtree(cache_home)
    live_settings = settings.model_copy(
        update={"demo_mode": False, "cache_dir": cache_home}
    )
    if "--diff-only" in sys.argv:
        return diff_existing(out_dir)

    client = ExtractionClient(live_settings, raw_dir=out_dir / "raw")

    mode = await client.probe(force=True)
    print(f"json mode: {mode}\n")

    targets = json.loads((ROOT / "fixtures" / "index.json").read_text())["shipments"]
    total_diffs = 0
    calls = 0
    not_captured = 0

    # All nine documents, not the interesting two: a captured fixture set that
    # only covers conflicts cannot show what a clean shipment looks like from a
    # given model, and that is the baseline every difference is measured against.
    for name in ("shipment_clean", "shipment_conflict", "shipment_messy"):
        print("=" * 74)
        print(name)
        print("=" * 74)
        for doc in targets[name]["documents"]:
            fname = doc["files"][0]
            try:
                res = await run_doc(client, fname, doc["doc_type"])
            except Exception as exc:  # noqa: BLE001
                print(f"  {fname}: CALL FAILED {type(exc).__name__}: {exc}")
                continue
            outcome = res["outcome"]
            if outcome.response is None:
                print(f"  {fname}: FAILED to parse -> {outcome.error}")
                not_captured += 1
                continue
            if outcome.source != "live_api" or outcome.used_cache:
                # A replay is not a capture. These fixtures are worth committing
                # only because a model really produced them, so refuse to write
                # one out under a LIVE provenance label.
                print(f"  {fname}: NOT A LIVE CALL (source="
                      f"{outcome.source}, cache={outcome.used_cache}) -- refusing")
                not_captured += 1
                continue
            calls += 1

            page_sha = [p.sha256 for p in res["pages"]]
            authored = load_authored(page_sha, doc["doc_type"])
            live = outcome.response
            (out_dir / f"{Path(fname).stem}.json").write_text(
                json.dumps(
                    {
                        "cache_key": cache_key(page_sha, client.endpoint.model,
                                               client.endpoint.provider),
                        "model": client.endpoint.model,
                        "provider": client.endpoint.provider,
                        "prompt_version": PROMPT_VERSION,
                        "page_sha256": page_sha,
                        "response": live.model_dump(mode="json"),
                        "_provenance": "LIVE API response, captured by scripts/live_compare.py",
                    },
                    indent=2,
                )
            )

            print(f"\n  {fname}  ({outcome.status}, {len(outcome.attempts)} attempt(s), "
                  f"{outcome.total_latency_ms} ms, cache={outcome.used_cache})")
            total_diffs += print_diff(live, authored)

    if "--seed-cache" in sys.argv:
        copied = 0
        for entry in sorted(cache_home.glob("*.json")):
            shutil.copy2(entry, BUNDLE_DIR / entry.name)
            copied += 1
        print(f"seeded {copied} live-captured entr(ies) into "
              f"{BUNDLE_DIR.relative_to(ROOT)} for DEMO_MODE")

    if not_captured:
        print(f"\n{calls} genuine live call(s); {not_captured} document(s) were NOT "
              "captured. Nothing partial was written as live.")
        return 1
    print(f"{calls} live calls made, each one a real API call. "
          f"{total_diffs} field-level differences vs authored.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
