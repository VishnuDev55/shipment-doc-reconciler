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
import time
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Reservations: the part of the cap that has not reached the ledger yet
# ---------------------------------------------------------------------------
#
# `cap_reached` reads a file, and the file only gains a row once a provider has
# answered. So N uploads that arrive together all read the same empty ledger,
# all pass, and all spend. The cap could be exceeded by a factor of N by a
# caller doing nothing cleverer than pressing enter on a form.
#
# A reservation closes that window by counting *projected* spend: money this
# process has decided is likely to be spent but has not yet seen. It lives in
# memory only, and that is the load-bearing property. Persisted state would
# have to be cleaned up, and a process that dies holding a reservation would
# wedge the day for everyone -- a worse failure than the race it fixes. Instead:
#
#   * `reserve()` never awaits, so one event-loop turn cannot interleave with
#     another writer's. The lock is for the threaded `record_call` path, not for
#     the race.
#   * every reservation expires on its own at READ time, so a leaked one stops
#     counting by itself and there is no sweeper task that can fail to run.
#   * a restart clears the table, which is correct: a dead process is not going
#     to spend anything.

# A page is sent as an image, so the prompt is dominated by visual tokens rather
# than text. 1500/page reproduces the measured ~6,000-token document read in
# docs/FEATURES.md at the 4-page ceiling render.yaml deploys, so the figure comes
# from this repo's own logs and not from a provider's marketing page.
ESTIMATED_TOKENS_PER_PAGE = 1500
ESTIMATED_COMPLETION_TOKENS_PER_READ = 900

# Ladder lengths mirror client.BACKOFF_SCHEDULE: Nebius cleared inside 3 live
# attempts, NVIDIA's shared workers needed 5. Repeated here rather than imported
# because .client imports this module to record spend, so importing back is a
# cycle; the numbers are covered by test_budget pinning them to the schedule.
BACKOFF_ATTEMPTS = {"nebius": 3, "nvidia": 5}


def estimate_run_usd(settings: Settings, docs: int, *, provider: str = "nvidia",
                     model: str = "") -> float:
    """What a run of `docs` documents could cost at worst, in USD.

    Reads `settings`, not module constants: `max_documents` is 5 in config.py and
    3 in render.yaml, `max_pages_per_document` 6 and 4, and reserving against the
    wrong pair misprices every run in one direction or the other.

    Deliberately pessimistic, because the two errors are not equal: an
    underestimate spends somebody's money past their cap, an overestimate refuses
    an upload that would have been cheap. So it prices the full ladder the
    transport climbs when a provider is flaky, both reads when consensus is on,
    and the extra read when line items are on.
    """
    docs = max(int(docs), 1)
    pages = max(int(settings.max_pages_per_document or 1), 1)
    reads = docs * BACKOFF_ATTEMPTS.get(provider, 3)
    if settings.consensus:
        reads *= 2
    if settings.enable_line_items:
        reads += docs

    prompt = reads * pages * ESTIMATED_TOKENS_PER_PAGE
    completion = reads * ESTIMATED_COMPLETION_TOKENS_PER_READ
    blended = float(settings.usd_per_mtok or 0.0) or None
    return round(cost_of(provider, model, prompt, completion, blended), 6)


@dataclass(frozen=True)
class Reservation:
    key: str
    amount: float
    shipment_id: str
    docs: int
    deadline: float


class SpendCapReached(Exception):
    """Raised at the point of dispatch, not at the door.

    The route's ledger check cannot see a reservation, and a run that is refused
    after the uploads are read still has to tell the caller why. Carrying both
    numbers lets the 429 say what the day has already cost, what is still
    reserved against it, and what this run would have cost -- the last of which
    is the only figure that tells a caller to upload fewer documents rather than
    to simply come back later.
    """

    def __init__(self, *, spent: float, reserved: float, amount: float, cap: float):
        self.spent = spent
        self.reserved = reserved
        self.amount = amount
        self.cap = cap
        super().__init__(
            f"Daily spend cap reached: ${spent:.2f} spent today (UTC) and "
            f"${reserved:.2f} of the ${cap:.2f} cap is reserved by runs already "
            f"in this instance. This shipment is estimated at ${amount:.2f}. "
            "Uploads resume after 00:00 UTC. The bundled sample shipments still "
            "work from cache, and they cost nothing."
        )


class SpendGate:
    """Projected spend, held in memory, keyed by what will spend it.

    `clock` is injectable for the same reason RateLimiter takes one: a TTL test
    that sleeps measures the scheduler, not the gate.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._pending: dict[str, Reservation] = {}
        self._lock = threading.Lock()

    def reserve(self, settings: Settings, *, key: str, shipment_id: str, docs: int,
                amount: float,
                now: datetime | None = None) -> Reservation | None:
        """Take `amount` off the day's remaining cap, or return None.

        Synchronous end to end. Nothing between reading the projected total and
        writing the reservation may yield the loop, which is the whole reason the
        race is closed: an `await` in the middle would put us back where the
        ledger check was.

        Returns a zero-cost reservation rather than None when there is no cap, so
        `None` means exactly one thing to the caller: refused.
        """
        cap = float(settings.usd_daily_cap or 0.0)
        ttl = max(float(settings.spend_reservation_ttl_seconds or 0.0), 1.0)
        with self._lock:
            self._prune()
            if cap <= 0:
                return Reservation(key=key, amount=0.0, shipment_id=shipment_id,
                                   docs=docs, deadline=self._clock() + ttl)
            projected = spent_today(settings, now=now) + self._total_locked()
            if projected + amount > cap:
                return None
            res = Reservation(key=key, amount=amount, shipment_id=shipment_id,
                              docs=docs, deadline=self._clock() + ttl)
            self._pending[key] = res
            return res

    def release(self, key: str) -> None:
        with self._lock:
            self._pending.pop(key, None)

    def pending(self) -> list[Reservation]:
        with self._lock:
            self._prune()
            return list(self._pending.values())

    def pending_total(self) -> float:
        with self._lock:
            self._prune()
            return self._total_locked()

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()

    def _total_locked(self) -> float:
        return sum(r.amount for r in self._pending.values())

    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, r in self._pending.items() if r.deadline <= now]:
            del self._pending[key]


GATE = SpendGate()


def reserve(settings: Settings, *, key: str, shipment_id: str, docs: int,
            amount: float, now: datetime | None = None) -> Reservation | None:
    """Module-level handle on the one gate this process shares.

    A caller that constructed its own SpendGate would be reserving against a
    projection nobody else can see, which is exactly the hole this closes.
    """
    return GATE.reserve(settings, key=key, shipment_id=shipment_id, docs=docs,
                        amount=amount, now=now)


def release(key: str) -> None:
    GATE.release(key)


def pending_reservations() -> list[Reservation]:
    return GATE.pending()


def pending_total() -> float:
    return GATE.pending_total()


def reset_pending() -> None:
    """Forget every reservation. Called by `configure()` so one test's held
    projection cannot 429 the next test's upload."""
    GATE.reset()

