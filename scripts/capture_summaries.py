"""Capture real AI summaries for the bundled fixtures.

Builds each report from the existing extraction cache (no vision calls), then
makes ONE live Nebius Nemotron call per shipment and stores the narrative keyed
on its verdict signature. After this, DEMO_MODE replays genuine model-written
summaries with no key.

    make capture-summaries
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.narrative_cache import NarrativeCache  # noqa: E402
from app.client import SummaryClient  # noqa: E402
from app.orchestrator import Reconciler  # noqa: E402
from app.summary import build_summary_prompt, summary_is_plausible  # noqa: E402


async def main() -> int:
    if not Path(".env").is_file():
        print("BLOCKED: no .env")
        return 2

    # Read only the keys from .env; everything else is set explicitly so this
    # script never inherits DEMO_MODE=false and starts calling the vision API.
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    # Extractions are replayed from the authored/live cache (demo_mode=True);
    # the summary itself is written by a dedicated LIVE client below. Keeping
    # the two settings separate is what stops this script spending money on
    # vision calls it does not need.
    settings = Settings(
        _env_file=None,
        demo_mode=True,
        nebius_api_key=env.get("NEBIUS_API_KEY", ""),
        nebius_base_url=env.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
        nvidia_api_key="",
        runs_dir=ROOT / "runs" / "_capture_summary",
        cache_dir=ROOT / "fixtures" / "responses",
        enable_summary=True,
    )
    if not settings.has_nebius_key:
        print("BLOCKED: NEBIUS_API_KEY not set (the summary runs on Nebius)")
        return 2

    live_settings = settings.model_copy(update={"demo_mode": False})

    index = json.loads((ROOT / "fixtures" / "index.json").read_text())["shipments"]
    cache = NarrativeCache(settings)
    summary_client = SummaryClient(live_settings)
    endpoint = summary_client.endpoint
    print(f"summary model: {endpoint.model} @ {endpoint.host}")
    written = 0

    for name, entry in index.items():
        uploads = [
            (f, d["doc_type"], (ROOT / "fixtures" / "images" / f).read_bytes())
            for d in entry["documents"]
            for f in d["files"]
        ]
        rec = Reconciler(settings)
        sid, _ = await rec.create_shipment(uploads)
        await rec.wait(sid, timeout=120)
        report = rec.get(sid)
        assert report is not None

        existing = cache.get(report, endpoint.model)
        if existing and not existing["authored"]:
            print(f"{name}: narrative already cached live, skipping call")
            continue

        print(f"\n=== {name}  verdict={report.summary.verdict} ===")
        try:
            text, usage = await summary_client.summarize(build_summary_prompt(report))
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            continue

        if not summary_is_plausible(text):
            print(f"  REJECTED as implausible: {text[:120]!r}")
            continue

        path = cache.put(report, model=endpoint.model, provider=endpoint.provider,
                         text=text.strip())
        written += 1
        print(f"  model: {endpoint.model}")
        print(f"  text : {text.strip()}")
        print(f"  usage: {usage}")
        print(f"  saved: {path.name}")

    print(f"\n{written} narrative(s) captured -> {cache.directory}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
