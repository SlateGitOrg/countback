"""Simple DSO, countback DSO, best-possible DSO and average days delinquent.

Countback DSO walks backwards from the snapshot month through monthly sales until the
receivable balance is used up, counting the days of every month it consumes. Unlike the simple
formula (AR over trailing sales) it measures the age of the receivables, so a sales ramp that
has not been collected yet does not read as customers paying slower.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from countback.aging import ALL, BUCKETS, Ledger

TRAILING_MONTHS = 3


@dataclass(frozen=True)
class Countback:
    days: float
    flagged: bool  # True when AR exceeds all available sales history


def countback_days(
    balance: float, sales_newest_first: Sequence[float], days_newest_first: Sequence[int]
) -> Countback:
    """Days of sales, counted back from the newest month, that add up to ``balance``.

    Whole months are consumed at their full day count and the last month fractionally. If the
    history is exhausted first, the total day count of the history is returned with
    ``flagged=True`` instead of being silently capped.
    """
    remaining = float(balance)
    days = 0.0
    if remaining <= 0.0:
        return Countback(0.0, False)
    slack = 1e-9 * max(1.0, abs(balance))  # float noise when AR equals all sales on record
    for sales, month_days in zip(sales_newest_first, days_newest_first, strict=True):
        if sales > 0.0 and sales >= remaining - slack:
            return Countback(days + min(remaining / sales, 1.0) * month_days, False)
        remaining -= sales
        days += month_days
    return Countback(days, True)


def build_metrics(ledger: Ledger) -> pd.DataFrame:
    """One row per reported month and segment (including ``All``) with every DSO metric."""
    snapshots = ledger.snapshots()
    sales = ledger.sales()
    frame = snapshots.merge(sales, on=["abs_idx", "segment"], how="left")
    frame = frame.merge(
        ledger.months[["abs_idx", "month_index", "month_end", "days"]], on="abs_idx"
    )
    frame = frame.sort_values(["segment", "abs_idx"]).reset_index(drop=True)

    parts = []
    for _, grp in frame.groupby("segment", sort=False):
        grp = grp.sort_values("abs_idx").copy()
        sales_arr = grp["sales"].to_numpy()
        days_arr = grp["days"].to_numpy()
        window = TRAILING_MONTHS
        grp["sales_3m"] = pd.Series(sales_arr, index=grp.index).rolling(window).sum()
        grp["days_3m"] = pd.Series(days_arr, index=grp.index).rolling(window).sum()
        total = np.full(len(grp), np.nan)
        current = np.full(len(grp), np.nan)
        flag = np.zeros(len(grp), dtype=int)
        for pos in range(len(grp)):
            newest_sales = sales_arr[pos::-1]
            newest_days = days_arr[pos::-1]
            full = countback_days(grp["ar_total"].iloc[pos], newest_sales, newest_days)
            cur = countback_days(grp["ar_current"].iloc[pos], newest_sales, newest_days)
            total[pos], current[pos], flag[pos] = full.days, cur.days, int(full.flagged)
        grp["dso_countback"] = total
        grp["bpdso_countback"] = current
        grp["dso_flagged"] = flag
        parts.append(grp)
    out = pd.concat(parts)
    out["sales_per_day_3m"] = out["sales_3m"] / out["days_3m"]
    out["dso_simple"] = out["ar_total"] / out["sales_per_day_3m"]
    out["add"] = out["dso_countback"] - out["bpdso_countback"]
    out = out[out["month_index"] >= 1]
    columns = [
        "month_index",
        "month_end",
        "segment",
        "sales",
        "sales_3m",
        "sales_per_day_3m",
        "ar_total",
        *BUCKETS,
        "dso_simple",
        "dso_countback",
        "bpdso_countback",
        "add",
        "dso_flagged",
    ]
    out = out[columns]
    order = {s: i for i, s in enumerate([*ledger.segments, ALL])}
    out = out.assign(_o=out["segment"].map(order)).sort_values(["month_index", "_o"])
    return out.drop(columns="_o").reset_index(drop=True)
