"""Capture real Nebius advisor notes for the planted bundle's withdrawn values.

The planted bundle runs offline (authored responses, zero vision calls). This
script replays those cases to find every value the evidence check withdrew, then
makes ONE live Nebius Nemotron call per distinct signature and stores the note
in `fixtures/planted/responses/unverified_notes/` with provenance marking it a
live capture. After this, the planted eval (and any demo run hitting the same
evidence) replays genuine model-written notes with no key.

    python scripts/capture_advisor_notes.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spend  # noqa: E402
from app.adjudicate import (  # noqa: E402
    UNVERIFIED_SYSTEM,
    AdjudicationCache,
    build_unverified_prompt,
    parse_unverified_note,
    unverified_note_is_usable,
    unverified_signature,
)
from app.client import SummaryClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.models import UnverifiedField  # noqa: E402
from app.orchestrator import Reconciler  # noqa: E402
from app.textlayer import load_sidecar  # noqa: E402
from fixtures.planted import CASES  # noqa: E402

PLANTED = ROOT / "fixtures" / "planted"
CACHE = PLANTED / "responses"
DOCS = PLANTED / "index.json"


def _offline_settings(runs_dir: Path) -> Settings:
    return Settings(
        _env_file=None,
        demo_mode=True,
        vision_provider="nvidia",
        nvidia_api_key="",
        nebius_api_key="",
        enable_line_items=True,
        runs_dir=runs_dir,
        cache_dir=CACHE,
    )


def _uploads(case: dict) -> tuple[list, dict]:
    manifest = json.loads(DOCS.read_text(encoding="utf-8"))["cases"]
    entries = manifest[case["name"]]["documents"]
    uploads, texts = [], {}
    for entry in entries:
        name = entry["file"]
        path = PLANTED / "images" / name
        uploads.append((name, entry["doc_type"], path.read_bytes()))
        side = load_sidecar(PLANTED / "text" / f"{Path(name).stem}.json")
        if side:
            texts[name] = side
    return uploads, texts


async def main() -> int:
    if not Path(".env").is_file():
        print("BLOCKED: no .env")
        return 2

    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    settings = Settings(
        _env_file=None,
        demo_mode=True,
        nebius_api_key=env.get("NEBIUS_API_KEY", ""),
        nebius_base_url=env.get("NEBIUS_BASE_URL",
                                "https://api.tokenfactory.nebius.com/v1/"),
        nvidia_api_key="",
        runs_dir=ROOT / "runs" / "_capture_notes",
        cache_dir=CACHE,
    )
    if not settings.has_nebius_key:
        print("BLOCKED: NEBIUS_API_KEY not set (the advisor runs on Nebius)")
        return 2

    live_settings = settings.model_copy(update={"demo_mode": False})
    client = SummaryClient(live_settings)
    endpoint = client.endpoint
    print(f"advisor model: {endpoint.model} @ {endpoint.host}")

    # Find every distinct withdrawn value across the bundle, offline.
    seen: dict[str, UnverifiedField] = {}
    for case in CASES:
        if case["expect"].get("excluded") is None:
            continue
        uploads, texts = _uploads(case)
        rec = Reconciler(settings=_offline_settings(ROOT / "runs" / "_notes" / case["name"]))
        sid, _ = await rec.create_shipment(uploads, page_texts=texts)
        await rec.wait(sid, timeout=120)
        report = rec.get(sid)
        assert report is not None
        for u in report.unverified:
            signature = unverified_signature(u.field, u.value, u.evidence_text, u.reason)
            seen.setdefault(signature, u)
        print(f"{case['name']}: {len(report.unverified)} withdrawn value(s)")

    if not seen:
        print("no withdrawn values to annotate")
        return 0

    cache = AdjudicationCache(CACHE, subdir="unverified_notes")
    written = 0
    for signature, u in seen.items():
        if cache.get(signature):
            print(f"{u.field}: already captured, skipping")
            continue
        prompt = build_unverified_prompt(
            u.field, u.label, u.value, u.evidence_text, u.page_index, u.reason
        )
        try:
            raw, usage = await client.judge(
                UNVERIFIED_SYSTEM, prompt, max_tokens=120, temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{u.field}: FAILED {type(exc).__name__}: {exc}")
            continue
        parsed = parse_unverified_note(raw)
        reason = unverified_note_is_usable(parsed)
        if reason is not None:
            print(f"{u.field}: REJECTED: {reason}")
            continue
        cache.put(signature, model=endpoint.model, provider=endpoint.provider,
                  raw=raw, note=parsed, _provenance="LIVE API")
        spend.record("nebius", endpoint.model, usage, note=f"advisor note: {u.field}")
        written += 1
        print(f"{u.field}: {parsed['explanation']!r}")
        print(f"  usage: {usage}")

    print(f"\n{written} note(s) captured -> {cache.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
