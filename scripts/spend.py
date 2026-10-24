"""Spend log for live API calls.

Tokens are exact — taken from the `usage` object each provider response carries.
Dollars come from **`app/budget.py`**, the same table the running app enforces its
daily cap with: two price tables is how a script and a deployment start reporting
different dollars for the same call.

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

if __package__ in (None, ""):  # runnable as `python scripts/spend.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.budget import cost_of  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "runs" / "spend_log.json"

# The budget for one live *round*: the total an operator authorises before
# stopping, and the per-provider share of it. Distinct from USD_DAILY_CAP, which
# is a deployment's ceiling enforced by the app at request time.
BUDGET_USD = 20.0
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


def summary() -> dict:
    data = _load()
    blended = _blended_rate()
    total = 0.0
    today = 0.0
    day = time.strftime("%Y-%m-%d", time.gmtime())
    per_provider: dict[str, float] = {}
    per_model: dict[str, float] = {}
    for c in data["calls"]:
        est = cost_of(c["provider"], c["model"],
                      c["prompt_tokens"], c["completion_tokens"], blended)
        total += est
        if str(c.get("at", ""))[:10] == day:
            today += est
        per_provider[c["provider"]] = per_provider.get(c["provider"], 0.0) + est
        key = f"{c['provider']} / {c['model']}"
        per_model[key] = per_model.get(key, 0.0) + est
    return {
        "calls": len(data["calls"]),
        "total_tokens": sum(c["total_tokens"] for c in data["calls"]),
        "total_usd_est": total,
        # The same UTC-day figure app/budget.py compares against USD_DAILY_CAP,
        # so an operator reading this sees what the running app would enforce.
        "today_usd_est": today,
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
    print(f"today UTC      : ${s['today_usd_est']:.4f} (what USD_DAILY_CAP is compared against)")
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
