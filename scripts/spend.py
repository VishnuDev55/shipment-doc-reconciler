"""Spend log for live API calls.

Tokens are exact — taken from the `usage` object each provider response carries.
Dollars use per-token prices: Nebius publishes them in its verbose catalog
(GET /models?verbose=true), so its rows are authoritative (2026-10-19);
build.nvidia.com publishes none, so its row is a conservative third-party
estimate, labelled as such. Image tokens bill as part of prompt_tokens; if a
response reports zero image cost, the full-token figure is the conservative
upper bound.

Keys are never read, written or echoed here. The log lives in
runs/spend_log.json, which is gitignored like the rest of runs/.

Usage:
    python scripts/spend.py                    # print the running total
    python scripts/spend.py --reset            # start a fresh log
    python scripts/spend.py record --provider nebius --model M \
        --prompt 100 --completion 40 --note "summary"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "runs" / "spend_log.json"

# (input, output) USD per million tokens. Nebius rows are authoritative from
# its verbose catalog (/models?verbose=true, 2026-10-19); the NVIDIA row is an
# estimate (build.nvidia.com publishes no rate). Image tokens bill as part of
# prompt_tokens; if a response reports zero image cost, the full-token figure is
# the conservative upper bound.
PRICE_USD_PER_MTOK: dict[str, dict[str, tuple[float, float]]] = {
    "nvidia": {
        # ESTIMATE — build.nvidia.com publishes no rate.
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": (0.10, 0.20),
    },
    "nebius": {
        "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06, 0.24),
        "openbmb/MiniCPM-V-4_5": (0.658, 1.11),
        "nvidia/Nemotron-3.5-Super-VL": (0.30, 0.90),
    },
}
FALLBACK = (0.10, 0.10)

BUDGET_USD = 30.0
BUDGET_PER_PROVIDER_USD = 15.0


def _load() -> dict:
    if LOG.is_file():
        try:
            data = json.loads(LOG.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("calls"), list):
                return data
        except (OSError, ValueError):
            pass
    return {"calls": [], "updated": None}


def _save(data: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(data, indent=2), encoding="utf-8")


def record(provider: str, model: str, usage: dict | None, *, note: str = "") -> dict:
    """Append one call's usage and return the entry."""
    usage = usage or {}
    prompt = int(usage.get("prompt") or 0)
    completion = int(usage.get("completion") or 0)
    total = int(usage.get("total") or prompt + completion)
    data = _load()
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": provider,
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "note": note,
    }
    data["calls"].append(entry)
    data["updated"] = entry["at"]
    _save(data)
    return entry


def _blended_rate() -> float | None:
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    raw = dotenv_values(ROOT / ".env").get("USD_PER_MTOK", "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def cost_of(provider: str, model: str, prompt: int, completion: int,
            blended: float | None) -> float:
    if blended is not None:
        return (prompt + completion) * blended / 1e6
    in_rate, out_rate = PRICE_USD_PER_MTOK.get(provider, {}).get(model, FALLBACK)
    return (prompt * in_rate + completion * out_rate) / 1e6


def summary() -> dict:
    data = _load()
    blended = _blended_rate()
    total = 0.0
    per_provider: dict[str, float] = {}
    per_model: dict[str, float] = {}
    for c in data["calls"]:
        est = cost_of(c["provider"], c["model"],
                      c["prompt_tokens"], c["completion_tokens"], blended)
        total += est
        per_provider[c["provider"]] = per_provider.get(c["provider"], 0.0) + est
        key = f"{c['provider']} / {c['model']}"
        per_model[key] = per_model.get(key, 0.0) + est
    return {
        "calls": len(data["calls"]),
        "total_tokens": sum(c["total_tokens"] for c in data["calls"]),
        "total_usd_est": total,
        "per_provider_usd_est": per_provider,
        "per_model_usd_est": per_model,
        "rate": "blended USD_PER_MTOK" if blended is not None else "price table",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("reset", help="start a fresh log")
    rec = sub.add_parser("record", help="append one call's usage")
    rec.add_argument("--provider", required=True)
    rec.add_argument("--model", required=True)
    rec.add_argument("--prompt", type=int, default=0)
    rec.add_argument("--completion", type=int, default=0)
    rec.add_argument("--note", default="")
    args = parser.parse_args(argv)

    if args.cmd == "reset":
        _save({"calls": [], "updated": None})
        print("spend log reset")
        return 0
    if args.cmd == "record":
        usage = {"prompt": args.prompt, "completion": args.completion}
        record(args.provider, args.model, usage, note=args.note)
        print(f"recorded 1 call: {args.provider} / {args.model} "
              f"({args.prompt}+{args.completion} tokens)")

    s = summary()
    print(f"calls          : {s['calls']}")
    print(f"total tokens   : {s['total_tokens']}")
    print(f"total USD (est): ${s['total_usd_est']:.4f}  [{s['rate']}]")
    for provider, usd in sorted(s["per_provider_usd_est"].items()):
        print(f"  {provider:8} ${usd:.4f}")
    over = s["total_usd_est"] >= BUDGET_USD
    over_provider = [p for p, usd in s["per_provider_usd_est"].items()
                     if usd >= BUDGET_PER_PROVIDER_USD]
    print(f"budget: ${BUDGET_USD:.0f} total / ${BUDGET_PER_PROVIDER_USD:.0f} per provider")
    if over_provider:
        print(f"OVER PROVIDER BUDGET: {', '.join(over_provider)} — stop and report")
    elif over:
        print("OVER TOTAL BUDGET — stop and report")
    else:
        print("ok")
    return 2 if (over or over_provider) else 0


if __name__ == "__main__":
    sys.exit(main())
