from __future__ import annotations

import pytest

from .helpers import total_series


def test_bpdso_never_exceeds_dso_and_add_is_not_negative(metrics):
    assert (metrics["bpdso_countback"] <= metrics["dso_countback"] + 1e-9).all()
    assert (metrics["add"] >= -1e-9).all()


def test_no_reported_month_runs_out_of_sales_history(metrics):
    assert metrics["dso_flagged"].sum() == 0
    assert metrics["dso_countback"].notna().all()


def test_countback_tracks_true_lag_while_behaviour_is_constant(metrics, truth):
    countback = total_series(metrics, "dso_countback")
    for month in range(1, 30):
        true_lag = truth["mean_lag"][str(month)]["All"]
        assert abs(countback[month] - true_lag) <= 4.0, month


def test_simple_dso_swings_with_the_season_while_behaviour_is_constant(metrics):
    simple = total_series(metrics, "dso_simple")
    for first in (1, 13):
        year = simple.loc[first : first + 11]
        assert year.max() - year.min() >= 12.0
    countback = total_series(metrics, "dso_countback")
    assert countback.loc[1:29].max() - countback.loc[1:29].min() < 10.0


def test_countback_range_is_narrower_than_simple_range_every_year(metrics):
    simple = total_series(metrics, "dso_simple")
    countback = total_series(metrics, "dso_countback")
    for first in (1, 13):
        sl = slice(first, first + 11)
        assert (countback.loc[sl].max() - countback.loc[sl].min()) < (
            simple.loc[sl].max() - simple.loc[sl].min()
        )


def test_homebuilder_countback_rises_at_least_10_days_from_month_29_to_33(metrics):
    homebuilders = total_series(metrics, "dso_countback", "homebuilders")
    assert homebuilders[33] - homebuilders[29] >= 10.0


def test_segment_countback_matches_each_segments_true_lag_before_the_events(metrics, truth):
    for segment, tolerance in (("dealers", 5.0), ("contractors", 4.0), ("homebuilders", 5.0)):
        series = total_series(metrics, "dso_countback", segment)
        errors = [abs(series[m] - truth["mean_lag"][str(m)][segment]) for m in range(1, 30)]
        assert sum(errors) / len(errors) < tolerance


def test_simple_dso_is_ar_over_trailing_three_month_sales(metrics):
    row = metrics[(metrics["segment"] == "dealers") & (metrics["month_index"] == 20)].iloc[0]
    assert row["dso_simple"] == pytest.approx(row["ar_total"] / row["sales_per_day_3m"])
    window = metrics[(metrics["segment"] == "dealers") & metrics["month_index"].between(18, 20)]
    assert row["sales_3m"] == pytest.approx(window["sales"].sum())


def test_past_due_balance_is_what_separates_countback_dso_from_bpdso(metrics):
    last = metrics[(metrics["segment"] == "All") & (metrics["month_index"] == 48)].iloc[0]
    assert last["add"] > 5.0
    assert last["ar_total"] - last["ar_current"] > 0
