"""Shapley bridge of a change in the segment-weighted countback DSO index.

The index is I = sum over segments of w x D, where w is the segment's share of trailing
three-month sales and D its countback DSO. Between two months the change in I has two causes:
who the customers are (the weights moved) and how they pay (the segment DSOs moved). Splitting
the product w x D by two-factor Shapley values gives, per segment,

    mix       = (w1 - w0) x (D0 + D1) / 2
    behaviour = (w0 + w1) / 2 x (D1 - D0)

and mix + behaviour equals w1 x D1 - w0 x D0 exactly, so nothing is left unexplained. The
portfolio countback DSO (countback on total AR over total sales) is not exactly I; the
difference is reported as the residual, not folded into either factor.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from countback.aging import ALL


@dataclass(frozen=True)
class SegmentBridge:
    segment: str
    weight_from: float
    weight_to: float
    dso_from: float
    dso_to: float
    mix: float
    behaviour: float

    @property
    def change(self) -> float:
        return self.weight_to * self.dso_to - self.weight_from * self.dso_from


@dataclass(frozen=True)
class Bridge:
    month_from: int
    month_to: int
    segments: tuple[SegmentBridge, ...]
    index_from: float
    index_to: float
    portfolio_from: float
    portfolio_to: float

    @property
    def change(self) -> float:
        return self.index_to - self.index_from

    @property
    def mix(self) -> float:
        return sum(s.mix for s in self.segments)

    @property
    def behaviour(self) -> float:
        return sum(s.behaviour for s in self.segments)

    @property
    def mix_share(self) -> float | None:
        return self.mix / self.change if abs(self.change) > 1e-9 else None

    @property
    def behaviour_share(self) -> float | None:
        return self.behaviour / self.change if abs(self.change) > 1e-9 else None

    @property
    def residual_from(self) -> float:
        return self.portfolio_from - self.index_from

    @property
    def residual_to(self) -> float:
        return self.portfolio_to - self.index_to


def _snapshot(metrics: pd.DataFrame, month: int) -> pd.DataFrame:
    rows = metrics[(metrics["month_index"] == month) & (metrics["segment"] != ALL)]
    if rows.empty:
        raise ValueError(f"month {month} is not in the metrics table")
    return rows.set_index("segment")


def shapley_bridge(metrics: pd.DataFrame, month_from: int, month_to: int) -> Bridge:
    """Bridge the index between two reported months of a ``build_metrics`` table."""
    start, end = _snapshot(metrics, month_from), _snapshot(metrics, month_to)
    w_start = start["sales_3m"] / start["sales_3m"].sum()
    w_end = end["sales_3m"] / end["sales_3m"].sum()
    segments = []
    for segment in start.index:
        w0, w1 = float(w_start[segment]), float(w_end[segment])
        d0, d1 = (
            float(start.loc[segment, "dso_countback"]),
            float(end.loc[segment, "dso_countback"]),
        )
        segments.append(
            SegmentBridge(
                segment=segment,
                weight_from=w0,
                weight_to=w1,
                dso_from=d0,
                dso_to=d1,
                mix=(w1 - w0) * (d0 + d1) / 2.0,
                behaviour=(w0 + w1) / 2.0 * (d1 - d0),
            )
        )

    def portfolio(month: int) -> float:
        row = metrics[(metrics["month_index"] == month) & (metrics["segment"] == ALL)]
        return float(row["dso_countback"].iloc[0])

    return Bridge(
        month_from=month_from,
        month_to=month_to,
        segments=tuple(segments),
        index_from=sum(s.weight_from * s.dso_from for s in segments),
        index_to=sum(s.weight_to * s.dso_to for s in segments),
        portfolio_from=portfolio(month_from),
        portfolio_to=portfolio(month_to),
    )
