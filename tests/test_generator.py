from __future__ import annotations

import numpy as np
import pandas as pd

from countback import generator as g


def test_generation_is_deterministic_and_seed_dependent():
    a, b = g.generate(3), g.generate(3)
    assert a.invoices.equals(b.invoices)
    assert a.truth == b.truth
    assert not g.generate(4).invoices["amount"].equals(a.invoices["amount"])


def test_population_and_volume(data_dir):
    frame = pd.read_csv(data_dir / "invoices.csv")
    assert 24_000 <= len(frame) <= 26_000
    assert frame["customer"].nunique() == 420
    assert frame.groupby("segment")["customer"].nunique().to_dict() == g.CUSTOMERS
    assert (frame["due_date"] > frame["invoice_date"]).all()
    assert frame["invoice_id"].is_unique


def test_seasonality_is_shared_and_peaks_at_2_5x(ledger):
    sales = ledger.sales()
    sales = sales.merge(ledger.months[["abs_idx", "month_start"]], on="abs_idx")
    sales["calendar_month"] = pd.to_datetime(sales["month_start"]).dt.month
    total = sales[(sales["segment"] == "All")]
    # Compare the months before the mix shift so the segment mix is constant.
    total = total.merge(ledger.months[["abs_idx", "month_index"]], on="abs_idx")
    total = total[(total["month_index"] >= 1) & (total["month_index"] <= 35)]
    by_month = total.groupby("calendar_month")["sales"].mean()
    ratio = by_month.loc[5:8].mean() / by_month.loc[[12, 1, 2]].mean()
    assert 2.2 <= ratio <= 2.8
    for segment in g.SEGMENTS:
        seg = sales[sales["segment"] == segment].groupby("calendar_month")["sales"].mean()
        assert 2.0 <= seg.loc[5:8].mean() / seg.loc[[12, 1, 2]].mean() <= 3.0


def test_realised_pay_lags_match_the_documented_means_and_cv(data_dir):
    frame = pd.read_csv(
        data_dir / "invoices.csv", parse_dates=["invoice_date", "paid_date"]
    ).dropna(subset=["paid_date"])
    frame["lag"] = (frame["paid_date"] - frame["invoice_date"]).dt.days
    early = frame[frame["invoice_date"] < "2024-06-01"]  # before month 30
    for segment in g.SEGMENTS:
        lags = early.loc[early["segment"] == segment, "lag"]
        assert abs(lags.mean() - g.MEAN_LAG_DAYS[segment]) < 2.0
        assert lags.std() / lags.mean() <= 0.5


def test_homebuilder_lags_step_up_by_15_days_from_month_30(data_dir):
    frame = pd.read_csv(
        data_dir / "invoices.csv", parse_dates=["invoice_date", "paid_date"]
    ).dropna(subset=["paid_date"])
    frame["lag"] = (frame["paid_date"] - frame["invoice_date"]).dt.days
    hb = frame[frame["segment"] == "homebuilders"]
    # Month 30 is June 2024. Exclude the last months, where only fast payers are observed.
    before = hb[(hb["invoice_date"] >= "2023-06-01") & (hb["invoice_date"] < "2024-06-01")]
    after = hb[(hb["invoice_date"] >= "2024-06-01") & (hb["invoice_date"] < "2025-06-01")]
    assert 13 <= after["lag"].mean() - before["lag"].mean() <= 17


def test_dealer_share_ramps_from_a_quarter_to_a_half(metrics):
    sales = metrics[metrics["segment"] != "All"].pivot(
        index="month_index", columns="segment", values="sales"
    )
    share = sales["dealers"] / sales.sum(axis=1)
    assert share.loc[1:35].between(0.21, 0.29).all()
    assert share.loc[48] > 0.46
    assert share.loc[48] - share.loc[36] > 0.18
    assert share.loc[36] < share.loc[42] < share.loc[48]


def test_the_dispute_invoice_is_one_contractor_invoice_held_150_days(data_dir, truth):
    frame = pd.read_csv(data_dir / "invoices.csv", parse_dates=["invoice_date", "paid_date"])
    event = truth["events"]["dispute"]
    row = frame[frame["invoice_id"] == event["invoice_id"]].iloc[0]
    assert row["segment"] == "contractors"
    assert row["amount"] == 180_000
    assert (row["paid_date"] - row["invoice_date"]).days == 150
    assert row["invoice_date"].strftime("%Y-%m") == "2025-04"  # month 40
    assert (frame["amount"] >= 100_000).sum() == 1


def test_paid_dates_after_the_data_end_are_left_empty(data_dir):
    frame = pd.read_csv(data_dir / "invoices.csv", parse_dates=["invoice_date", "paid_date"])
    assert frame["paid_date"].max() <= pd.Timestamp("2025-12-31")
    last_month = frame[frame["invoice_date"] >= "2025-12-01"]
    assert last_month["paid_date"].isna().mean() > 0.5


def test_truth_records_lags_for_every_month_and_the_event_months(truth):
    assert sorted(map(int, truth["mean_lag"])) == list(range(1, 49))
    assert truth["mean_lag"]["10"]["dealers"] == 66.0
    assert truth["mean_lag"]["29"]["homebuilders"] == 50.0
    assert truth["mean_lag"]["30"]["homebuilders"] == 65.0
    assert truth["events"]["homebuilder_slowdown"]["from_month"] == 30
    assert truth["events"]["dealer_mix_shift"]["to_month"] == 48
    assert truth["events"]["dispute"]["month"] == 40
    all_lags = np.array([truth["mean_lag"][str(m)]["All"] for m in range(1, 30)])
    assert 48 < all_lags.mean() < 53
