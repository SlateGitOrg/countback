"""Registry of the Tableau calculated fields and their Python reference implementations.

The workbook builder takes each formula from here, and ``validate-twb`` evaluates the formulas
found in the workbook and compares them with ``reference`` on every exported row. A calculated
field whose name is not registered is an error.

Formulas are restricted to what the mini evaluator supports: field references, numbers, SUM,
ZN, + - * / and parentheses.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

Row = Mapping[str, float]


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


@dataclass(frozen=True)
class Metric:
    name: str
    formula: str
    reference: Callable[[Row], float | None]
    description: str


def simple_dso(row: Row) -> float | None:
    return _ratio(row["ar_total"], row["sales_per_day_3m"])


def add(row: Row) -> float:
    return row["dso_countback"] - row["bpdso_countback"]


def percent_current(row: Row) -> float | None:
    return _ratio(row["ar_current"], row["ar_total"])


def past_due_60_plus_share(row: Row) -> float | None:
    return _ratio(row["ar_61_90"] + row["ar_90_plus"], row["ar_total"])


METRICS: dict[str, Metric] = {
    m.name: m
    for m in (
        Metric(
            "Simple DSO",
            "SUM([ar_total]) / SUM([sales_per_day_3m])",
            simple_dso,
            "AR divided by trailing three-month sales per day",
        ),
        Metric(
            "ADD",
            "SUM([dso_countback]) - SUM([bpdso_countback])",
            add,
            "Average days delinquent: countback DSO minus countback BPDSO",
        ),
        Metric(
            "Percent Current",
            "ZN(SUM([ar_current])) / SUM([ar_total])",
            percent_current,
            "Share of AR that is not yet past due",
        ),
        Metric(
            "Past Due 60 Plus Share",
            "(SUM([ar_61_90]) + SUM([ar_90_plus])) / SUM([ar_total])",
            past_due_60_plus_share,
            "Share of AR more than 60 days past due",
        ),
    )
}
