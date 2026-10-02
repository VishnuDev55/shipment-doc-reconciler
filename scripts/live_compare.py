"""Live-vs-authored comparison.

Runs bundled fixtures through the REAL pipeline with an EMPTY cache dir, so
every extraction is a genuine API call, then diffs the live response against
the hand-authored cache entry for the same document.

    DEMO_MODE=false NEBIUS_MODEL=<vision-model> python scripts/live_compare.py

Writes real live responses into fixtures/live_responses/ so they can be reused
as regression fixtures.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cache import cache_key  # noqa: E402
from app.providers import vision_endpoint  # noqa: E402
from app.client import ExtractionClient  # noqa: E402
from app.config import PROMPT_VERSION, Settings  # noqa: E402
from app.ingest import ingest  # noqa: E402
from app.normalize import normalize_field  # noqa: E402

AUTHORED_DIR = ROOT / "fixtures" / "responses"
LIVE_DIR = ROOT / "fixtures" / "live_responses"
IMAGES = ROOT / "fixtures" / "images"


def load_authored(page_sha: list[str], model_note: str) -> dict | None:
    """Find the hand-authored entry for these pages.

    The authored cache is keyed on the model in .env at build time, which may
    differ from the live model, so fall back to matching on page hashes alone.
    """
    for path in sorted(AUTHORED_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if payload.get("page_sha256") == page_sha:
            return payload.get("response")
    return None


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

    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    # Empty cache dir => every call is real. Kept OUTSIDE live_responses so the
    # captured fixtures directory contains only files meant to be committed.
    live_settings = settings.model_copy(
        update={"demo_mode": False, "cache_dir": ROOT / ".live_runcache"}
    )
    client = ExtractionClient(live_settings, raw_dir=LIVE_DIR / "raw")

    mode = await client.probe(force=True)
    print(f"json mode: {mode}\n")

    targets = json.loads((ROOT / "fixtures" / "index.json").read_text())["shipments"]
    total_diffs = 0
    calls = 0

    for name in ("shipment_conflict", "shipment_messy"):
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
            calls += 1
            if outcome.response is None:
                print(f"  {fname}: FAILED to parse -> {outcome.error}")
                continue

            page_sha = [p.sha256 for p in res["pages"]]
            authored = load_authored(page_sha, doc["doc_type"])
            live = outcome.response
            (LIVE_DIR / f"{Path(fname).stem}.json").write_text(
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
            if authored is None:
                print("     no authored entry matched these page hashes")
                continue

            if live.doc_type != authored["doc_type"]:
                print(f"     DIFF doc_type: live={live.doc_type} authored={authored['doc_type']}")
                total_diffs += 1
            else:
                print(f"     match doc_type           {live.doc_type!r}")

            for key, field in live.fields.items():
                d = diff_field(key, field, authored["fields"])
                for note in d:
                    print(f"     DIFF {key}: {note}")
                    total_diffs += 1
                if not d and field.present:
                    norm = normalize_field(key, field)
                    print(f"     match {key:18} {field.value!r:26} -> {norm.canonical!r}")

    print(f"\n{calls} live calls made. {total_diffs} field-level differences vs authored.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
