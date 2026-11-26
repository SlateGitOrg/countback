from __future__ import annotations

import pytest

from countback.aging import Ledger
from countback.dso import Countback, build_metrics, countback_days

from .helpers import write_invoices


def test_hand_fixture_counts_back_to_45_days():
    # Jan 100,000 / Feb 120,000 / Mar 150,000 (non-leap year), AR 210,000:
    # all of March (31 days) plus half of February (0.5 x 28 days).
    result = countback_days(210_000, [150_000, 120_000, 100_000], [31, 28, 31])
    assert result == Countback(days=45.0, flagged=False)


def test_ar_exceeding_history_returns_full_day_count_and_flags():
    result = countback_days(400_000, [150_000, 120_000, 100_000], [31, 28, 31])
    assert result.days == 90
    assert result.flagged is True


def test_ar_equal_to_all_sales_is_not_flagged():
    result = countback_days(370_000, [150_000, 120_000, 100_000], [31, 28, 31])
    assert result.days == pytest.approx(90.0)
    assert result.flagged is False


def test_float_noise_at_the_history_boundary_does_not_flag():
    sales = [0.1] * 7
    result = countback_days(sum(sales) + 1e-13, sales, [30] * 7)
    assert result.flagged is False
    assert result.days == pytest.approx(210.0)


def test_zero_ar_is_zero_days():
    assert countback_days(0.0, [100.0], [31]) == Countback(0.0, False)


def test_months_without_sales_still_count_their_days():
    # AR is older than a month with no sales at all: its 30 days are walked through.
    result = countback_days(50.0, [0.0, 100.0], [30, 31])
    assert result.days == pytest.approx(30 + 0.5 * 31)


def test_partial_first_month():
    assert countback_days(75.0, [150.0], [30]).days == pytest.approx(15.0)


def test_ledger_reproduces_the_hand_fixture(tmp_path):
    path = write_invoices(
        tmp_path / "invoices.csv",
        [
            ("A", "c1", "dealers", "2023-01-10", "2023-02-09", 100_000, "2023-02-10"),
            ("B1", "c1", "dealers", "2023-02-15", "2023-03-17", 60_000, "2023-03-05"),
            ("B2", "c1", "dealers", "2023-02-15", "2023-03-17", 60_000, ""),
            ("C", "c1", "dealers", "2023-03-20", "2023-04-19", 150_000, ""),
        ],
    )
    metrics = build_metrics(Ledger(path, warmup=0))
    march = metrics[(metrics["month_index"] == 3) & (metrics["segment"] == "All")].iloc[0]
    assert march["ar_total"] == 210_000
    assert march["dso_countback"] == pytest.approx(45.0)
    # Only the March invoice is current (150,000 = all of March): 31 days. B2 is 14 days late.
    assert march["bpdso_countback"] == pytest.approx(31.0)
    assert march["add"] == pytest.approx(14.0)
    assert march["ar_1_30"] == 60_000
    # Simple DSO: AR over trailing three-month sales per day (370,000 over 90 days).
    assert march["dso_simple"] == pytest.approx(210_000 / (370_000 / 90))
    assert march["dso_flagged"] == 0
