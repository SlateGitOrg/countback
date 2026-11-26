from __future__ import annotations

import pandas as pd
import pytest

from countback.aging import BUCKETS, Ledger

from .helpers import write_invoices


def test_bucket_boundaries_on_a_hand_built_ledger(tmp_path):
    # Snapshot is 2023-01-31; days past due = snapshot - due date.
    def invoice(n, dpd, paid=""):
        due = pd.Timestamp("2023-01-31") - pd.Timedelta(days=dpd)
        return (f"I{n}", "c", "dealers", "2022-10-01", due.date().isoformat(), 100.0, paid)

    rows = [
        invoice(1, -5),  # not yet due
        invoice(2, 0),  # due today: still current
        invoice(3, 1),
        invoice(4, 30),
        invoice(5, 31),
        invoice(6, 60),
        invoice(7, 61),
        invoice(8, 90),
        invoice(9, 91),
        invoice(10, 10, paid="2023-01-31"),  # paid on the snapshot day: closed
        invoice(11, 10, paid="2023-02-01"),  # paid the day after: still open
        # Dated and paid on the snapshot day; it only extends the calendar to January.
        ("I12", "c", "dealers", "2023-01-31", "2023-03-02", 50.0, "2023-01-31"),
    ]
    ledger = Ledger(write_invoices(tmp_path / "invoices.csv", rows), warmup=0)
    snap = ledger.snapshots()
    last = snap[(snap["abs_idx"] == snap["abs_idx"].max()) & (snap["segment"] == "All")].iloc[0]
    assert last["ar_current"] == 200.0
    assert last["ar_1_30"] == 300.0  # dpd 1, 30 and the open invoice at 10
    assert last["ar_31_60"] == 200.0
    assert last["ar_61_90"] == 200.0
    assert last["ar_90_plus"] == 100.0
    assert last["ar_total"] == 1_000.0
    assert last["open_invoices"] == 10


def test_buckets_open_invoices_and_ar_agree_at_every_month_end(ledger):
    """Sum of buckets = AR = sum of open invoices, recomputed independently in pandas."""
    snap = ledger.snapshots()
    frame = ledger.con.execute("SELECT * FROM invoices").df()
    frame["invoice_date"] = pd.to_datetime(frame["invoice_date"])
    frame["paid_date"] = pd.to_datetime(frame["paid_date"])
    for _, month in ledger.months.iterrows():
        end = pd.Timestamp(month["month_end"])
        is_open = (frame["invoice_date"] <= end) & (
            frame["paid_date"].isna() | (frame["paid_date"] > end)
        )
        expected_ar = frame.loc[is_open, "amount"].sum()
        expected_count = int(is_open.sum())
        row = snap[(snap["abs_idx"] == month["abs_idx"]) & (snap["segment"] == "All")].iloc[0]
        assert row[list(BUCKETS)].sum() == pytest.approx(row["ar_total"], abs=1e-6)
        assert row["ar_total"] == pytest.approx(expected_ar, abs=1e-6)
        assert row["open_invoices"] == expected_count


def test_segment_rows_add_up_to_the_total_row(ledger):
    snap = ledger.snapshots()
    by_segment = snap[snap["segment"] != "All"].groupby("abs_idx")["ar_total"].sum()
    total = snap[snap["segment"] == "All"].set_index("abs_idx")["ar_total"]
    assert (by_segment - total).abs().max() < 1e-6


def test_warmup_months_are_excluded_from_reporting(ledger, metrics):
    assert ledger.months["month_index"].min() == -5
    assert metrics["month_index"].min() == 1
    assert metrics["month_index"].max() == 48
    assert set(metrics["segment"]) == {"dealers", "contractors", "homebuilders", "All"}
