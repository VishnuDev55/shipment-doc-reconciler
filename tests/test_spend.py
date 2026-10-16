"""The spend log: exact tokens, estimated dollars, budget guard.

No network here — the log is file IO plus arithmetic, which is exactly the part
that must never be wrong when the number guards a real budget.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("spend", REPO / "scripts" / "spend.py")
spend = importlib.util.module_from_spec(_spec)
sys.modules["spend"] = spend
_spec.loader.exec_module(spend)


@pytest.fixture()
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(spend, "LOG", tmp_path / "spend_log.json")
    return spend


def test_record_and_summary_round_trip(log):
    log.record("nebius", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
               {"prompt": 100, "completion": 40}, note="summary")
    log.record("nvidia", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
               {"prompt": 0, "completion": 0, "total": 0})

    s = log.summary()
    assert s["calls"] == 2
    assert s["total_tokens"] == 140
    # 100*0.05 + 40*0.10 = 5 + 4 = 9 micro-dollars -> 0.000009
    assert s["total_usd_est"] == pytest.approx(0.000009, abs=1e-9)
    assert "nebius" in s["per_provider_usd_est"]


def test_a_zero_usage_entry_costs_nothing(log):
    log.record("nebius", "openbmb/MiniCPM-V-4_5", None)
    assert log.summary()["total_usd_est"] == 0.0


def test_cost_of_uses_the_table_and_a_blended_override():
    table = spend.cost_of("nebius", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                          1_000_000, 1_000_000, blended=None)
    assert table == pytest.approx(0.05 + 0.10, abs=1e-9)
    blended = spend.cost_of("nebius", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                            1_000_000, 1_000_000, blended=1.0)
    assert blended == pytest.approx(2.0, abs=1e-9)


def test_unknown_model_falls_back_to_conservative_rates(log):
    log.record("nvidia", "some/future-model", {"prompt": 1_000_000, "completion": 0})
    assert log.summary()["total_usd_est"] == pytest.approx(0.10, abs=1e-9)
