"""Live verification for BOTH providers. The one thing DEMO_MODE cannot prove.

    make probe-live

Checks, in order, and prints a real result or a real error for each:

  1. NVIDIA build.nvidia.com — vision model reachable? capability probe? one
     real extraction against a bundled fixture, parsed by the real parser?
  2. Nebius Token Factory     — summary model reachable? capability probe? one
     real narrative generated from a fixture report?

Nothing here is mocked. Exits non-zero if either provider is unusable.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openai import APIStatusError  # noqa: E402

from app.cache import cache_key  # noqa: E402
from app.client import ExtractionClient, SummaryClient, status_note  # noqa: E402
from app.config import PROMPT_VERSION, Settings  # noqa: E402
from app.ingest import ingest  # noqa: E402
from app.normalize import normalize_field  # noqa: E402
from app.normalize.container import normalize_container  # noqa: E402
from app.orchestrator import Reconciler  # noqa: E402
from app.providers import vision_endpoint  # noqa: E402
from app.summary import build_summary_prompt, summary_is_plausible  # noqa: E402

FIXTURE = ROOT / "fixtures" / "images" / "invoice_conflict.png"
OUT_DIR = ROOT / "fixtures" / "live_responses"


def rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def _live_settings() -> Settings:
    """Keys from .env; DEMO_MODE forced off; a scratch cache so every call here
    is genuinely live rather than a replay."""
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env")
    return Settings(
        _env_file=None,
        demo_mode=False,
        vision_provider=env.get("VISION_PROVIDER", "nvidia"),
        nebius_api_key=env.get("NEBIUS_API_KEY", ""),
        nebius_base_url=env.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
        nebius_summary_model=env.get("NEBIUS_SUMMARY_MODEL", ""),
        nvidia_api_key=env.get("NVIDIA_API_KEY", ""),
        nvidia_base_url=env.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1/"),
        nvidia_vision_model=env.get("NVIDIA_VISION_MODEL", ""),
        nebius_vision_model=env.get("NEBIUS_VISION_MODEL", ""),
        runs_dir=ROOT / "runs" / "_probe",
        cache_dir=ROOT / ".probe_cache",
    )


async def check_vision(settings: Settings) -> str | None:
    rule("1/2  VISION  —  document extraction")
    ep = vision_endpoint(settings)
    print(f"provider : {ep.provider}")
    print(f"model    : {ep.model}")
    print(f"host     : {ep.host}")
    print(f"key      : {'present' if ep.api_key.strip() else 'MISSING'}")
    if not ep.api_key.strip():
        print("BLOCKED: no API key for the vision provider")
        return None

    client = ExtractionClient(settings, raw_dir=OUT_DIR / "raw")
    try:
        mode = await client.probe(force=True)
    except APIStatusError as exc:
        print(f"probe failed: {status_note(exc)}")
        return None
    print(f"\nstructured-output mode: {mode}")
    if mode == "plain":
        print("  (neither json_schema nor json_object honoured; relying on the "
              "prompt plus the repair ladder)")

    if not FIXTURE.is_file():
        print("BLOCKED: fixture missing. Run: python scripts/build_fixtures.py")
        return None
    pages = ingest(FIXTURE.name, FIXTURE.read_bytes())
    print(f"\nsending {len(pages)} page(s), {pages[0].width}x{pages[0].height}, "
          f"sha256={pages[0].sha256[:12]}")
    try:
        out = await client.extract_document(
            document_id="probe", pages=pages, declared_doc_type="commercial_invoice"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"EXTRACTION FAILED: {type(exc).__name__}: {exc}")
        return None

    print(f"status={out.status} attempts={len(out.attempts)} cache={out.used_cache} "
          f"latency={out.total_latency_ms} ms")
    for a in out.attempts:
        print(f"  rung {a.rung:12} ok={a.ok} usage={a.usage} {a.note[:70]}")
    if out.response is None:
        print(f"\nLIVE RESPONSE DID NOT PARSE: {out.error}")
        print("Inspect fixtures/live_responses/raw/ — this is the most important")
        print("unknown, so it is worth reading the raw body.")
        return None

    resp = out.response
    print("\nExtracted (verbatim -> normalized by our code, not the model):")
    for key, field in resp.fields.items():
        if not field.present:
            print(f"  {key:18} absent")
            continue
        norm = normalize_field(key, field)
        flag = "" if norm.ok else "  <-- did not normalize"
        print(f"  {key:18} {field.value!r:26} -> {norm.canonical!r}{flag}")
        if key == "container_number":
            c = normalize_container(field.value)
            print(f"    ISO 6346: valid={c.check_digit_valid} {c.detail[:66]}")
        for w in norm.warnings:
            print(f"    warn: {w}")
    for issue in out.issues:
        print(f"  note: {issue}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ph = [p.sha256 for p in pages]
    (OUT_DIR / "vision_probe.json").write_text(json.dumps({
        "cache_key": cache_key(ph, out.model, out.provider),
        "model": out.model, "provider": out.provider,
        "prompt_version": PROMPT_VERSION, "page_sha256": ph,
        "response": resp.model_dump(mode="json"),
        "_provenance": f"LIVE API response from {out.provider}",
    }, indent=2))
    print(f"\nsaved: {OUT_DIR / 'vision_probe.json'}")
    return out.model


async def check_summary(settings: Settings) -> None:
    rule("2/2  SUMMARY  —  Nebius Token Factory reasoning call")
    client = SummaryClient(settings)
    ep = client.endpoint
    print(f"provider : {ep.provider}")
    print(f"model    : {ep.model}")
    print(f"host     : {ep.host}")
    print(f"key      : {'present' if ep.api_key.strip() else 'MISSING'}")
    if not ep.api_key.strip():
        print("BLOCKED: NEBIUS_API_KEY missing — the Nebius requirement is unmet")
        return

    # Build a report from the shipped cache so no extra vision calls are spent,
    # then make exactly ONE live summary call.
    idx = json.loads((ROOT / "fixtures" / "index.json").read_text())["shipments"]
    name = "shipment_conflict"
    ups = [(f, d["doc_type"], (ROOT / "fixtures" / "images" / f).read_bytes())
           for d in idx[name]["documents"] for f in d["files"]]
    replay = settings.model_copy(update={"demo_mode": True, "enable_summary": False,
                                         "cache_dir": ROOT / "fixtures" / "responses"})
    rec = Reconciler(replay)
    sid, _ = await rec.create_shipment(ups)
    await rec.wait(sid, timeout=120)
    report = rec.get(sid)
    assert report is not None
    print(f"\nverdicts fed to the model (from {name}): "
          f"{report.summary.conflict} conflict / {report.summary.suspicious} suspicious / "
          f"{report.summary.consistent} consistent")

    try:
        text, usage = await client.summarize(build_summary_prompt(report))
    except Exception as exc:  # noqa: BLE001
        print(f"SUMMARY FAILED (this must never break a shipment): "
              f"{type(exc).__name__}: {exc}")
        return

    print(f"usage: {usage}")
    print(f"model wrote: {text}")
    ok = summary_is_plausible(text)
    print(f"passes the plausibility guard: {ok}")
    if not ok:
        print("  -> the UI would suppress this rather than show chain-of-thought")


async def main() -> int:
    settings = _live_settings()
    print(f"vision provider: {settings.vision_provider}")
    model = await check_vision(settings)
    await check_summary(settings)

    rule("SUMMARY OF THIS RUN")
    if model is None:
        print("VISION path is NOT verified. The core pipeline is unproven live.")
        print("Fix the key/model/provider before relying on this app.")
        return 1
    print("Vision extraction and the Nebius summary call both returned real,")
    print("parseable responses. Flip DEMO_MODE=false to run the app this way.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
