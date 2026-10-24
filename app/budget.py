"""What this instance has spent today, and the cap that stops it.

Two consumers, one table. `scripts/spend.py` (the live harnesses an operator runs
by hand) and the running app both price a call through `cost_of`, so the dollars
in a report and the dollars that refuse an upload cannot disagree -- the failure
mode this replaces was a price table in a script that the app could not see.

Tokens are exact: every provider response carries a `usage` object, and the entry
is written when the provider *answers*, not when the JSON parses. A call that
spent 40,000 tokens and returned unparseable text is still 40,000 tokens gone, and
counting only parsed successes would understate the day.

Dollars are estimates with different confidence per provider: Nebius publishes its
rates in its own verbose catalog (`GET /models?verbose=true`, read 2026-10-19), so
its rows are authoritative. build.nvidia.com publishes none, so its row is a
conservative third-party estimate, labelled below. Image tokens bill inside
`prompt_tokens`.

The ledger is `RUNS_DIR/spend_log.json`. On a free Render instance that directory
is ephemeral, so **a redeploy resets the day**. That is a real limit of this
design, not an implementation detail: the cap bounds a day of one container's
life, and the notice on the page says the same thing about the runs beside it.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings

# (input, output) USD per 1M tokens. Nebius rows are authoritative from its
# verbose catalog; the NVIDIA row is an ESTIMATE because that host publishes no
# rate. Anything unlisted bills at FALLBACK, which is deliberately not zero: an
# unknown model must cost something in the ledger, or adding one silently
# uncaps the day.
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

LOG_NAME = "spend_log.json"

# One process, one worker, one filesystem: a lock is enough here, and a lost
# update would be an undercount -- the unsafe direction for a cap. A distributed
# ledger would be a dependency bought for a scale this deployment does not have.
_LOCK = threading.Lock()


def log_path(runs_dir: Path | str) -> Path:
    return Path(runs_dir) / LOG_NAME


def cost_of(provider: str, model: str, prompt: int, completion: int,
            blended: float | None = None) -> float:
    """USD for one call. `blended` is the operator's own USD_PER_MTOK override."""
    if blended is not None:
        return (prompt + completion) * blended / 1e6
    in_rate, out_rate = PRICE_USD_PER_MTOK.get(provider, {}).get(model, FALLBACK)
    return (prompt * in_rate + completion * out_rate) / 1e6


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> list[dict[str, Any]]:
    """The call list, or empty. A ledger that cannot be read is treated as empty
    rather than fatal: the cap is a brake, and a corrupt file must not turn every
    upload into a 500."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict) and isinstance(data.get("calls"), list):
        return data["calls"]
    return []


def entry_cost(entry: dict[str, Any], blended: float | None = None) -> float:
    return cost_of(
        str(entry.get("provider", "")), str(entry.get("model", "")),
        int(entry.get("prompt_tokens") or 0), int(entry.get("completion_tokens") or 0),
        blended,
    )


def record_call(settings: Settings, provider: str, model: str, usage: dict | None,
                *, note: str = "", blended: float | None = None) -> bool:
    """Append one live call. Returns whether the ledger was written.

    Never raises. Bookkeeping that cannot be written is a disclosed gap, not a
    reason to fail a shipment somebody is waiting on.
    """
    usage = usage or {}
    prompt = int(usage.get("prompt") or 0)
    completion = int(usage.get("completion") or 0)
    if prompt == 0 and completion == 0:
        # Nothing to record: a response that reported no usage is not evidence of
        # a cost, and writing zeros would make the ledger look busy without being.
        return False
    total = int(usage.get("total") or prompt + completion)
    entry = {
        "at": _now_stamp(),
        "provider": provider,
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "note": note,
    }
    path = log_path(settings.runs_dir)
    try:
        with _LOCK:
            calls = _load(path)
            calls.append(entry)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"calls": calls, "updated": entry["at"]}, indent=2),
                           encoding="utf-8")
            tmp.replace(path)
    except OSError:
        return False
    return True


def spent_today(settings: Settings, *, now: datetime | None = None,
                blended: float | None = None) -> float:
    """USD booked today (UTC) by this container's ledger."""
    day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    total = 0.0
    for entry in _load(log_path(settings.runs_dir)):
        if str(entry.get("at", ""))[:10] != day:
            continue
        try:
            total += entry_cost(entry, blended)
        except (TypeError, ValueError):
            continue
    return total


def cap_reached(settings: Settings, *, now: datetime | None = None,
                blended: float | None = None) -> bool:
    """True when the day's cap is spent. A cap of 0 means "no cap", matching
    `RUN_LIMIT_PER_MINUTE`."""
    cap = float(settings.usd_daily_cap or 0.0)
    if cap <= 0:
        return False
    return spent_today(settings, now=now, blended=blended) >= cap


def seconds_until_utc_midnight(*, now: datetime | None = None) -> int:
    """How long to tell a refused caller to wait. The cap is a UTC day, so the
    honest Retry-After is the rest of it, not an invented interval."""
    moment = now or datetime.now(timezone.utc)
    nxt = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                               microsecond=0)
    return max(1, int((nxt - moment).total_seconds()))
