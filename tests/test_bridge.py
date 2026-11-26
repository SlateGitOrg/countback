from __future__ import annotations

import pandas as pd
import pytest

from countback.bridge import shapley_bridge


def _frame(month: int, rows: dict[str, tuple[float, float]], portfolio: float) -> list[dict]:
    """rows: segment -> (sales_3m, dso_countback)."""
    out = [
        {"month_index": month, "segment": s, "sales_3m": sales, "dso_countback": dso}
        for s, (sales, dso) in rows.items()
    ]
    out.append(
        {"month_index": month, "segment": "All", "sales_3m": 0.0, "dso_countback": portfolio}
    )
    return out


def test_hand_computed_two_segment_bridge():
    metrics = pd.DataFrame(
        _frame(1, {"a": (50.0, 60.0), "b": (50.0, 40.0)}, 50.5)
        + _frame(2, {"a": (75.0, 66.0), "b": (25.0, 40.0)}, 59.0)
    )
    bridge = shapley_bridge(metrics, 1, 2)
    # I: 0.5x60 + 0.5x40 = 50 -> 0.75x66 + 0.25x40 = 59.5
    assert bridge.index_from == pytest.approx(50.0)
    assert bridge.index_to == pytest.approx(59.5)
    a = next(s for s in bridge.segments if s.segment == "a")
    b = next(s for s in bridge.segments if s.segment == "b")
    assert a.mix == pytest.approx(0.25 * (60 + 66) / 2)
    assert a.behaviour == pytest.approx(0.625 * 6)
    assert b.mix == pytest.approx(-0.25 * 40)
    assert b.behaviour == pytest.approx(0.0)
    assert bridge.mix == pytest.approx(5.75)
    assert bridge.behaviour == pytest.approx(3.75)
    assert bridge.mix + bridge.behaviour == pytest.approx(bridge.change)
    assert bridge.mix_share == pytest.approx(5.75 / 9.5)
    # The portfolio figure is not the index; the gap is reported, not absorbed.
    assert bridge.residual_from == pytest.approx(0.5)
    assert bridge.residual_to == pytest.approx(-0.5)


def test_pure_mix_and_pure_behaviour_cases():
    base = {"a": (50.0, 60.0), "b": (50.0, 40.0)}
    mix_only = pd.DataFrame(
        _frame(1, base, 50) + _frame(2, {"a": (80.0, 60.0), "b": (20.0, 40.0)}, 56)
    )
    result = shapley_bridge(mix_only, 1, 2)
    assert result.behaviour == pytest.approx(0.0)
    assert result.mix == pytest.approx(result.change)
    behaviour_only = pd.DataFrame(
        _frame(1, base, 50) + _frame(2, {"a": (50.0, 70.0), "b": (50.0, 40.0)}, 55)
    )
    result = shapley_bridge(behaviour_only, 1, 2)
    assert result.mix == pytest.approx(0.0)
    assert result.behaviour_share == pytest.approx(1.0)


def test_unknown_month_is_rejected(metrics):
    with pytest.raises(ValueError, match="month 99"):
        shapley_bridge(metrics, 1, 99)


def test_bridge_is_exact_for_every_pair_of_months(metrics):
    for start, end in ((1, 13), (5, 9), (29, 33), (36, 48), (12, 47)):
        bridge = shapley_bridge(metrics, start, end)
        assert bridge.mix + bridge.behaviour == pytest.approx(bridge.change, abs=1e-9)
        for seg in bridge.segments:
            assert seg.mix + seg.behaviour == pytest.approx(seg.change, abs=1e-9)


def test_weights_are_trailing_three_month_sales_shares(metrics):
    bridge = shapley_bridge(metrics, 20, 24)
    assert sum(s.weight_from for s in bridge.segments) == pytest.approx(1.0)
    dealers = next(s for s in bridge.segments if s.segment == "dealers")
    window = metrics[metrics["month_index"].between(18, 20)]
    expected = (
        window[window["segment"] == "dealers"]["sales"].sum()
        / window[window["segment"] != "All"]["sales"].sum()
    )
    assert dealers.weight_from == pytest.approx(expected)


def test_homebuilder_slowdown_is_attributed_to_behaviour(metrics):
    bridge = shapley_bridge(metrics, 29, 33)
    assert bridge.change > 3.0
    assert bridge.behaviour_share >= 0.80
    homebuilders = next(s for s in bridge.segments if s.segment == "homebuilders")
    assert homebuilders.behaviour > 0.9 * bridge.change


def test_dealer_mix_shift_is_attributed_to_mix(metrics):
    bridge = shapley_bridge(metrics, 36, 48)
    assert bridge.mix_share >= 0.60
    assert bridge.mix + bridge.behaviour == pytest.approx(bridge.change, abs=1e-12)
    dealers = next(s for s in bridge.segments if s.segment == "dealers")
    assert dealers.weight_to - dealers.weight_from > 0.18


def test_residual_between_index_and_portfolio_countback_is_small_but_reported(metrics):
    bridge = shapley_bridge(metrics, 36, 48)
    assert abs(bridge.residual_from) < 3.0 and abs(bridge.residual_to) < 3.0
    assert (bridge.portfolio_to - bridge.portfolio_from) == pytest.approx(
        bridge.change + bridge.residual_to - bridge.residual_from
    )
