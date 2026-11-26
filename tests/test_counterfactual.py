from __future__ import annotations

import pandas as pd
import pytest

from countback import generator as g


def test_undisputed_run_differs_only_in_the_disputed_invoice():
    disputed = g.generate(7).invoices
    clean = g.generate(7, dispute=False).invoices
    assert len(disputed) == len(clean)
    differing = disputed[~(disputed.fillna("none") == clean.fillna("none")).all(axis=1)]
    assert len(differing) == 1
    assert differing["amount"].iloc[0] == 180_000
    index = differing.index[0]
    assert (disputed.loc[index, "paid_date"] - disputed.loc[index, "invoice_date"]).days == 150
    assert (clean.loc[index, "paid_date"] - clean.loc[index, "invoice_date"]).days < 150


def test_without_the_dispute_contractor_add_is_lower_in_months_42_to_44(
    metrics, undisputed_metrics
):
    disputed = metrics[metrics["segment"] == "contractors"].set_index("month_index")
    clean = undisputed_metrics[undisputed_metrics["segment"] == "contractors"].set_index(
        "month_index"
    )
    for month in (42, 43, 44):
        assert clean.loc[month, "add"] < disputed.loc[month, "add"] - 0.5, month
        assert clean.loc[month, "ar_total"] < disputed.loc[month, "ar_total"]


def test_every_month_before_41_is_unchanged_by_the_dispute(metrics, undisputed_metrics):
    before = metrics[metrics["month_index"] <= 40].reset_index(drop=True)
    clean = undisputed_metrics[undisputed_metrics["month_index"] <= 40].reset_index(drop=True)
    numeric = before.select_dtypes("number").columns
    pd.testing.assert_frame_equal(before[numeric], clean[numeric], check_exact=False, rtol=1e-12)


def test_dispute_shows_up_as_90_plus_ar_in_the_contractor_book(metrics, undisputed_metrics):
    month = 44
    with_dispute = metrics[
        (metrics["segment"] == "contractors") & (metrics["month_index"] == month)
    ]
    without = undisputed_metrics[
        (undisputed_metrics["segment"] == "contractors")
        & (undisputed_metrics["month_index"] == month)
    ]
    assert with_dispute["ar_90_plus"].iloc[0] - without["ar_90_plus"].iloc[0] == pytest.approx(
        180_000
    )
