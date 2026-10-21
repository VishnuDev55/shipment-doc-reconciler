"""Prove the cross-provider fallback live: the primary host unreachable, Nebius delivers.

    python scripts/live_fallback_check.py

The primary base URL is pointed at a closed local port, so every NVIDIA attempt
is a transport failure and no NVIDIA token is spent. The document must still
arrive, stamped with the host that actually read it, plus the routing note the
report renders. The Nebius call is recorded in the spend log.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spend  # noqa: E402
from app.config import Settings  # noqa: E402
from app.orchestrator import Reconciler  # noqa: E402
from app.providers import vision_endpoint  # noqa: E402

FIXTURE = ROOT / "fixtures" / "images" / "invoice_clean.png"
DEAD_PRIMARY = "http://127.0.0.1:9/v1/"  # discard port: connection refused


async def main() -> int:
    if not (ROOT / ".env").is_file():
        print("BLOCKED: no .env")
        return 2
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    cache_dir = ROOT / "runs" / "fallback_cache"
    shutil.rmtree(cache_dir, ignore_errors=True)  # force a real call, not a replay

    settings = Settings(
        _env_file=None,
        demo_mode=False,
        model_discovery=False,
        vision_provider="nvidia",
        nvidia_api_key=env.get("NVIDIA_API_KEY", ""),
        nebius_api_key=env.get("NEBIUS_API_KEY", ""),
        nvidia_base_url=DEAD_PRIMARY,
        nebius_vision_model=env.get("NEBIUS_VISION_MODEL", ""),
        enable_summary=False,
        enable_adjudication=False,
        enable_briefing=False,
        consensus=False,
        enable_line_items=False,
        runs_dir=ROOT / "runs" / "fallback_check",
        cache_dir=cache_dir,
    )
    fallback = vision_endpoint(settings.model_copy(update={"vision_provider": "nebius"}))
    if not fallback.api_key.strip():
        print("BLOCKED: NEBIUS_API_KEY not set")
        return 2

    rec = Reconciler(settings)
    print(f"primary  : {rec.client.endpoint.provider} @ {rec.client.endpoint.host} (dead)")
    print(f"fallback : {fallback.provider} / {fallback.model}\n")

    sid, _ = await rec.create_shipment(
        [("invoice_clean.png", "commercial_invoice", FIXTURE.read_bytes())]
    )
    await rec.wait(sid, timeout=300)
    report = rec.get(sid)
    doc = report.documents[0]

    usage = doc.usage
    if usage and (usage.prompt or usage.completion or usage.total):
        spend.record(doc.provider, doc.model,
                     {"prompt": usage.prompt, "completion": usage.completion},
                     note="live fallback check")

    print(f"doc status : {doc.status}")
    print(f"doc read by: {doc.provider} / {doc.model}")
    print(f"run primary: {report.run.vision_provider}")
    print(f"notes      : {doc.notes}")
    print(f"tokens     : {usage.total if usage else 0}")
    print(f"attempts   : {doc.attempts}")

    ok = (
        doc.status in ("ok", "partial", "cached")
        and doc.provider == "nebius"
        and any("read on nebius instead" in n for n in doc.notes)
        and report.run.vision_provider == "nvidia"
    )
    print("\nFALLBACK LIVE CHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
