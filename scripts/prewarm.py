"""Pre-warm before a demo: ping both providers and fill the narrative cache.

A demo should never depend on live latency. This pings the two hosts (a free
/v1/models call each) and makes one live Nebius summary call per demo shipment
whose narrative is not already cached, so a later demo run replays everything
from cache. The vision side is already captured under fixtures/live_responses;
no extraction calls are made here.

    make prewarm
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
from app import discovery  # noqa: E402
from app.client import SummaryClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.narrative_cache import NarrativeCache  # noqa: E402
from app.orchestrator import Reconciler  # noqa: E402
from app.providers import summary_endpoint, vision_endpoint  # noqa: E402
from app.summary import build_summary_prompt, summary_is_plausible  # noqa: E402


async def main() -> int:
    if not Path(".env").is_file():
        print("BLOCKED: no .env")
        return 2
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    settings = Settings(
        _env_file=None,
        demo_mode=True,  # the reports replay from cache; only the summary is live
        vision_provider=env.get("VISION_PROVIDER", "nvidia"),
        nvidia_api_key=env.get("NVIDIA_API_KEY", ""),
        nebius_api_key=env.get("NEBIUS_API_KEY", ""),
        nvidia_base_url=env.get("NVIDIA_BASE_URL",
                                 "https://integrate.api.nvidia.com/v1/"),
        nebius_base_url=env.get("NEBIUS_BASE_URL",
                                "https://api.tokenfactory.nebius.com/v1/"),
        runs_dir=ROOT / "runs" / "prewarm",
        cache_dir=ROOT / "fixtures" / "responses",
    )

    # 1. Ping both providers — free /v1/models calls.
    print("pinging providers…")
    for label, endpoint in (("vision", vision_endpoint(settings)),
                            ("summary", summary_endpoint(settings))):
        try:
            catalog = await discovery.fetch(endpoint, timeout=15)
            print(f"  {label}: {endpoint.model} @ {endpoint.host} — "
                  f"{len(catalog.models)} models listed")
        except discovery.DiscoveryError as exc:
            print(f"  {label}: UNREACHABLE — {exc}")

    if not settings.has_nebius_key:
        print("BLOCKED: NEBIUS_API_KEY not set (the summary runs on Nebius)")
        return 2

    # 2. Fill the narrative cache for the three demo shipments.
    live = settings.model_copy(update={"demo_mode": False})
    client = SummaryClient(live)
    cache = NarrativeCache(settings)
    index = json.loads((ROOT / "fixtures" / "index.json").read_text())["shipments"]
    written = 0
    for name in ("shipment_clean", "shipment_conflict", "shipment_messy"):
        uploads = [(f, d["doc_type"], (ROOT / "fixtures" / "images" / f).read_bytes())
                   for d in index[name]["documents"] for f in d["files"]]
        rec = Reconciler(settings)
        sid, _ = await rec.create_shipment(uploads)
        await rec.wait(sid, timeout=120)
        report = rec.get(sid)
        assert report is not None
        if cache.get(report, client.endpoint.model):
            print(f"  {name}: narrative already cached, skipping")
            continue
        try:
            text, usage = await client.summarize(build_summary_prompt(report))
        except Exception as exc:  # noqa: BLE001
            print(f"  {name}: FAILED {type(exc).__name__}: {exc}")
            continue
        if not summary_is_plausible(text):
            print(f"  {name}: REJECTED as implausible")
            continue
        cache.put(report, model=client.endpoint.model, provider=client.endpoint.provider,
                  text=text.strip())
        spend.record("nebius", client.endpoint.model, usage, note=f"prewarm {name}")
        written += 1
        print(f"  {name}: captured ({usage['total']} tokens)")

    print(f"\nprewarm done: {written} narrative(s) captured")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
