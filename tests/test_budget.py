"""The daily spend cap, and the ledger it is computed from.

This is the module that decides whether an upload gets answered or refused on a
public instance, so the tests are written against hand-derived dollars rather
than against the code's own arithmetic: a rate table read back from the function
that uses it would pass whatever the table says.

The break each file-level test guards is named in its docstring.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import budget
from app.config import Settings

REPO = Path(__file__).resolve().parents[1]

NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
OMNI = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        _env_file=None,
        demo_mode=True,
        nebius_api_key="",
        nvidia_api_key="",
        runs_dir=tmp_path / "runs",
        cache_dir=REPO / "fixtures" / "responses",
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# the price table
# ---------------------------------------------------------------------------


def test_a_priced_model_bills_at_its_published_rate() -> None:
    """Break caught: a rate is edited or a provider key is misspelled, so a call
    silently bills at the fallback instead of the catalog price."""
    assert budget.cost_of("nebius", NANO, 1_000_000, 0, None) == pytest.approx(0.06)
    assert budget.cost_of("nebius", NANO, 0, 1_000_000, None) == pytest.approx(0.24)
    # 1M in + 1M out is the sum of the two, not the average of anything.
    assert budget.cost_of("nebius", NANO, 1_000_000, 1_000_000, None) == pytest.approx(0.30)


def test_an_unpriced_model_bills_at_the_conservative_fallback() -> None:
    """Break caught: a new model is added to an endpoint and the cap stops
    counting it, so the deployment spends without noticing."""
    assert budget.cost_of("nebius", "some/new-model", 1_000_000, 0, None) == pytest.approx(0.10)


def test_a_blended_rate_overrides_the_table() -> None:
    """Break caught: USD_PER_MTOK is set and then ignored, which would price a
    run by a rate the operator already overrode."""
    assert budget.cost_of("nebius", NANO, 500_000, 500_000, 2.0) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def test_a_recorded_call_counts_toward_todays_spend(tmp_path) -> None:
    settings = make_settings(tmp_path)
    ok = budget.record_call(settings, "nebius", NANO,
                            {"prompt": 1_000_000, "completion": 1_000_000}, note="test")
    assert ok is True
    assert budget.spent_today(settings) == pytest.approx(0.30)


def test_yesterdays_spend_does_not_count_against_today(tmp_path) -> None:
    """Break caught: the cap sums the whole file instead of the day, so a long
    running instance stops accepting uploads forever after one busy morning."""
    settings = make_settings(tmp_path)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    path = budget.log_path(settings.runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"calls": [
        {"at": yesterday, "provider": "nebius", "model": NANO,
         "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000,
         "total_tokens": 2_000_000, "note": "old"},
    ], "updated": yesterday}), encoding="utf-8")
    assert budget.spent_today(settings) == pytest.approx(0.0)


def test_a_corrupt_ledger_reads_as_no_spend_rather_than_crashing(tmp_path) -> None:
    """Break caught: a half-written JSON log (the container was killed mid-write)
    turns every upload into a 500."""
    settings = make_settings(tmp_path)
    path = budget.log_path(settings.runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"calls": [tru', encoding="utf-8")
    assert budget.spent_today(settings) == 0.0
    assert budget.record_call(settings, "nebius", NANO, {"prompt": 10, "completion": 5}) is True


def test_the_cap_trips_at_the_limit_not_above_it(tmp_path) -> None:
    """Break caught: `>` instead of `>=`, which lets one more call through than
    the operator asked for."""
    settings = make_settings(tmp_path, usd_daily_cap=0.30)
    budget.record_call(settings, "nebius", NANO,
                       {"prompt": 1_000_000, "completion": 1_000_000})
    assert budget.cap_reached(settings) is True

    under = make_settings(tmp_path / "under", usd_daily_cap=0.31)
    budget.record_call(under, "nebius", NANO,
                       {"prompt": 1_000_000, "completion": 1_000_000})
    assert budget.cap_reached(under) is False


def test_a_cap_of_zero_disables_the_check(tmp_path) -> None:
    """Break caught: the off switch is mistaken for "spend nothing", which would
    refuse every upload on an instance that meant to lift the cap."""
    settings = make_settings(tmp_path, usd_daily_cap=0.0)
    budget.record_call(settings, "nebius", NANO,
                       {"prompt": 10_000_000, "completion": 10_000_000})
    assert budget.cap_reached(settings) is False


def test_the_default_cap_stops_the_third_dollar_of_spend(tmp_path) -> None:
    """Break caught: the default is raised or the alias stops binding, and the
    public instance is silently uncapped.

    Priced by hand against the table's own NVIDIA row ($0.10 per 1M prompt
    tokens): 19M tokens is $1.90, which is under a $2.00 day, and 20M is exactly
    $2.00, which is not.
    """
    settings = make_settings(tmp_path)
    assert settings.usd_daily_cap == pytest.approx(2.00)

    under = make_settings(tmp_path / "under")
    budget.record_call(under, "nvidia", OMNI, {"prompt": 19_000_000, "completion": 0})
    assert budget.cap_reached(under) is False

    budget.record_call(settings, "nvidia", OMNI, {"prompt": 20_000_000, "completion": 0})
    assert budget.cap_reached(settings) is True


def test_concurrent_calls_are_all_counted(tmp_path) -> None:
    """Break caught: two documents of one shipment finish together, both read the
    ledger, both write it back, and one call vanishes -- an undercount, which is
    the wrong direction for a cap.

    Verified by mutation: with the ledger's lock removed this test reports fewer
    than eight calls, which is how it knows it is guarding something.
    """
    from concurrent.futures import ThreadPoolExecutor

    settings = make_settings(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: budget.record_call(settings, "nebius", NANO,
                                         {"prompt": 100_000, "completion": 100_000}),
            range(8)))
    assert results == [True] * 8
    # 8 x (100k x $0.06/M + 100k x $0.24/M) = 8 x $0.03
    assert budget.spent_today(settings) == pytest.approx(0.24)


def test_the_cap_is_configurable_by_environment(monkeypatch, tmp_path) -> None:
    """Break caught: render.yaml sets USD_DAILY_CAP and nothing reads it, so the
    deployment runs on the default while the operator believes otherwise."""
    monkeypatch.setenv("USD_DAILY_CAP", "5.50")
    assert Settings(_env_file=None).usd_daily_cap == pytest.approx(5.50)


def test_a_ledger_that_cannot_be_written_never_sinks_a_request(tmp_path, monkeypatch) -> None:
    """Break caught: the bookkeeping fails and takes the shipment with it. A
    read-only or vanished volume must degrade to "cannot tell", not to a 500."""
    settings = make_settings(tmp_path)
    monkeypatch.setattr(Path, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert budget.record_call(settings, "nebius", NANO, {"prompt": 10, "completion": 5}) is False
    assert budget.spent_today(settings) == 0.0


# ---------------------------------------------------------------------------
# one price table, two consumers
# ---------------------------------------------------------------------------


def test_the_spend_cli_reports_what_the_app_recorded(tmp_path, monkeypatch, capsys) -> None:
    """Break caught: scripts/spend.py grows its own table and the live harnesses
    and the running app start reporting different dollars for the same calls."""
    import importlib.util

    settings = make_settings(tmp_path)
    budget.record_call(settings, "nebius", NANO,
                       {"prompt": 1_000_000, "completion": 1_000_000})

    spec = importlib.util.spec_from_file_location(
        "spend_cli", REPO / "scripts" / "spend.py")
    assert spec and spec.loader
    spend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(spend)
    spend.LOG = budget.log_path(settings.runs_dir)
    monkeypatch.setattr(spend, "_blended_rate", lambda: None)

    assert spend.main([]) == 0
    out = capsys.readouterr().out
    assert "$0.3000" in out
    assert "calls          : 1" in out


# ---------------------------------------------------------------------------
# reservations: the part of the cap that has not reached the ledger yet
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_gate():
    """Reservations are process state, and this file's tests all share one
    process. Without this, a reservation taken by an early test silently
    withholds budget from a later one that never asked for it."""
    budget.reset_pending()
    yield
    budget.reset_pending()


def test_a_reservation_holds_money_the_ledger_has_not_spent_yet(tmp_path) -> None:
    """Break caught: N uploads arriving together all read the same empty ledger,
    all pass the cap check, and all spend. The cap then means nothing except on
    a slow day."""
    settings = make_settings(tmp_path, usd_daily_cap=0.30)
    assert budget.spent_today(settings) == 0.0

    first = budget.reserve(settings, key="a", shipment_id="a", docs=1, amount=0.20)
    assert first is not None
    assert budget.spent_today(settings) == 0.0, "a reservation is not a receipt"
    assert budget.reserve(settings, key="b", shipment_id="b", docs=1,
                          amount=0.20) is None, "0.20 more does not fit 0.30 of a day"


def test_a_refused_reservation_leaves_the_day_unchanged(tmp_path) -> None:
    """Break caught: a refusal that still writes its reservation would shrink the
    cap on every rejected attempt, and one busy caller could lock out a whole
    instance by trying too hard."""
    settings = make_settings(tmp_path, usd_daily_cap=0.30)
    budget.reserve(settings, key="a", shipment_id="a", docs=1, amount=0.20)
    for i in range(5):
        assert budget.reserve(settings, key=f"junk{i}", shipment_id="x", docs=1,
                              amount=0.20) is None
    assert budget.pending_total() == pytest.approx(0.20)


def test_a_leaked_reservation_self_heals_at_its_deadline(tmp_path) -> None:
    """Break caught: a reservation released only by a callback that never fires
    (the process was killed, the task was GC'd) would hold the cap until a
    restart, which on a long-lived instance is never."""
    clock = iter([0.0] * 4)
    gate = budget.SpendGate(clock=lambda: next(clock))
    settings = make_settings(tmp_path, usd_daily_cap=0.30,
                             spend_reservation_ttl_seconds=100.0)

    assert gate.reserve(settings, key="a", shipment_id="a", docs=1, amount=0.20)
    assert gate.reserve(settings, key="b", shipment_id="b", docs=1, amount=0.20) is None

    # Past the deadline, read from a fresh clock: the stale one is gone by
    # itself, and the second run now fits.
    late = 500.0
    clock = iter([late, late, late, late])
    gate._clock = lambda: next(clock)
    assert gate.pending() == []
    assert gate.reserve(settings, key="c", shipment_id="c", docs=1, amount=0.20)


def test_a_reservation_writes_nothing_to_the_ledger(tmp_path) -> None:
    """Break caught: persisting reservations means a crashed process wedges the
    cap for the next one, and the ledger stops meaning "what was spent"."""
    settings = make_settings(tmp_path, usd_daily_cap=1.00)
    budget.reserve(settings, key="a", shipment_id="a", docs=2, amount=0.40)

    path = budget.log_path(settings.runs_dir)
    assert not path.exists(), "the day's money is spent by calls, not by intentions"


def test_a_finished_run_counts_what_it_spent_not_what_it_reserved(tmp_path) -> None:
    """Break caught: an estimate that stays on the cap after the run completes
    charges the day twice -- once for the guess, once for the invoices."""
    settings = make_settings(tmp_path, usd_daily_cap=0.30)
    res = budget.reserve(settings, key="a", shipment_id="a", docs=1, amount=0.25)
    assert res is not None
    budget.record_call(settings, "nebius", NANO, {"prompt": 100_000, "completion": 0})

    budget.release("a")
    assert budget.pending_total() == 0.0
    # $0.006 of a $0.30 day: the room comes back as the real number, not the guess.
    assert budget.reserve(settings, key="b", shipment_id="b", docs=1, amount=0.25)


def test_releasing_twice_is_not_an_error(tmp_path) -> None:
    """Break caught: a done-callback and a finally both release, and the second
    one raising would replace a finished run with a 500 in the logs."""
    settings = make_settings(tmp_path, usd_daily_cap=1.00)
    budget.reserve(settings, key="a", shipment_id="a", docs=1, amount=0.10)
    budget.release("a")
    budget.release("a")
    assert budget.pending_total() == 0.0


def test_a_cap_of_zero_reserves_nothing_and_refuses_nothing(tmp_path) -> None:
    """Break caught: the off switch read as "zero money may be committed", which
    would refuse every upload on an instance that meant to lift the cap."""
    settings = make_settings(tmp_path, usd_daily_cap=0.0)
    res = budget.reserve(settings, key="a", shipment_id="a", docs=5, amount=99.0)
    assert res is not None, "None means refused; an uncapped day never refuses"
    assert res.amount == 0.0
    assert budget.pending_total() == 0.0


def test_one_document_always_fits_an_empty_day(tmp_path) -> None:
    """Break caught: an estimate generous enough to exceed the cap on its own
    means nobody can ever upload again -- a worse outage than the race fixed."""
    settings = make_settings(tmp_path)  # the shipped default cap
    one = budget.estimate_run_usd(settings, 1, provider="nvidia")
    assert one < settings.usd_daily_cap
    assert budget.reserve(settings, key="a", shipment_id="a", docs=1, amount=one)


def test_the_estimate_prices_the_settings_it_was_given(tmp_path) -> None:
    """Break caught: pricing from module constants while render.yaml narrows the
    instance means every reservation is wrong in the same direction."""
    narrow = make_settings(tmp_path, max_pages_per_document=4, usd_daily_cap=2.0)
    wide = make_settings(tmp_path / "wide", max_pages_per_document=6, usd_daily_cap=2.0)
    assert budget.estimate_run_usd(wide, 2) > budget.estimate_run_usd(narrow, 2)

    plain = budget.estimate_run_usd(narrow, 2)
    both = budget.estimate_run_usd(
        make_settings(tmp_path / "c", max_pages_per_document=4, consensus=True), 2)
    assert both == pytest.approx(plain * 2), "consensus reads every page twice"

    extra = budget.estimate_run_usd(
        make_settings(tmp_path / "l", max_pages_per_document=4, enable_line_items=True), 2)
    assert extra > plain, "line items are one more read per document"


def test_the_reserved_ladder_matches_the_ladder_the_client_climbs() -> None:
    """Break caught: budget.py repeats the backoff lengths because importing
    .client would cycle, and a ladder that grows in one file and not the other
    under-reserves every run."""
    from app.client import BACKOFF_SCHEDULE

    for provider, schedule in BACKOFF_SCHEDULE.items():
        assert budget.BACKOFF_ATTEMPTS[provider] == len(schedule)
