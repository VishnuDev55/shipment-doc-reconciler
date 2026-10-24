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
