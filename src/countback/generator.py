"""Seeded synthetic receivables for a seasonal building-materials wholesaler.

The generator knows the true payment behaviour, so the analysis can be scored against it.

Properties
- 420 customers in three segments, 48 reporting months (month 1 = January 2022) preceded by
  six warm-up months so that the first reported month-end has a full sales history to count
  back through.
- One seasonality shared by every segment: April-August at 2.5x the December-February level.
  Payment behaviour does not depend on the season.
- Pay lags are lognormal with a coefficient of variation of 0.35.

Injected events (recorded in truth.json)
- Homebuilders pay 15 days slower on invoices dated from month 30.
- The dealer share of sales rises linearly from 25% to 50% between months 36 and 48 with
  unchanged behaviour.
- One 180,000 contractor invoice dated in month 40 is held in dispute for 150 days.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

SEGMENTS = ("dealers", "contractors", "homebuilders")

ORIGIN_YEAR = 2022
WARMUP_MONTHS = 6
REPORT_MONTHS = 48

CUSTOMERS = {"dealers": 70, "contractors": 250, "homebuilders": 100}
TERMS_DAYS = {"dealers": 60, "contractors": 30, "homebuilders": 45}
MEAN_LAG_DAYS = {"dealers": 66.0, "contractors": 41.0, "homebuilders": 50.0}
LAG_CV = 0.30
MEAN_INVOICE = {"dealers": 14_000.0, "contractors": 10_400.0, "homebuilders": 19_000.0}
AMOUNT_SIGMA = 0.30
BASE_MONTHLY_SALES = 6_000_000.0
BASE_SHARE = {"dealers": 0.25, "contractors": 0.45, "homebuilders": 0.30}
SHARE_NOISE = 0.02

# Relative sales level by calendar month (January first). Peak over trough is 2.5.
SEASON = (1.0, 1.0, 1.6, 2.2, 2.5, 2.5, 2.5, 2.3, 1.7, 1.3, 1.1, 1.0)

HOMEBUILDER_SHIFT_MONTH = 30
HOMEBUILDER_SHIFT_DAYS = 15.0
MIX_START_MONTH = 36
MIX_END_MONTH = 48
DEALER_SHARE_START = 0.25
DEALER_SHARE_END = 0.50

DISPUTE_MONTH = 40
DISPUTE_DAY = 20
DISPUTE_AMOUNT = 180_000.0
DISPUTE_HOLD_DAYS = 150


@dataclass(frozen=True)
class GeneratedData:
    invoices: pd.DataFrame
    truth: dict


def month_of(index: int) -> tuple[int, int]:
    """Calendar (year, month) of month index ``index`` (1 is January of the origin year)."""
    offset = index - 1
    return ORIGIN_YEAR + offset // 12, offset % 12 + 1


def days_in(index: int) -> int:
    year, month = month_of(index)
    return calendar.monthrange(year, month)[1]


def segment_shares(index: int) -> dict[str, float]:
    """Target share of each segment in the sales of month ``index``."""
    ramp = (index - MIX_START_MONTH) / (MIX_END_MONTH - MIX_START_MONTH)
    ramp = min(max(ramp, 0.0), 1.0)
    dealers = DEALER_SHARE_START + (DEALER_SHARE_END - DEALER_SHARE_START) * ramp
    rest = 1.0 - dealers
    other_total = BASE_SHARE["contractors"] + BASE_SHARE["homebuilders"]
    return {
        "dealers": dealers,
        "contractors": rest * BASE_SHARE["contractors"] / other_total,
        "homebuilders": rest * BASE_SHARE["homebuilders"] / other_total,
    }


def true_mean_lag(segment: str, index: int) -> float:
    mean = MEAN_LAG_DAYS[segment]
    if segment == "homebuilders" and index >= HOMEBUILDER_SHIFT_MONTH:
        mean += HOMEBUILDER_SHIFT_DAYS
    return mean


def _lognormal(rng: np.random.Generator, mean: float, cv: float, size: int | None = None):
    sigma2 = np.log1p(cv * cv)
    return rng.lognormal(np.log(mean) - sigma2 / 2.0, np.sqrt(sigma2), size)


def _customer_ids(segment: str) -> list[str]:
    prefix = segment[0].upper()
    return [f"{prefix}{n:03d}" for n in range(1, CUSTOMERS[segment] + 1)]


def generate(seed: int = 7, dispute: bool = True) -> GeneratedData:
    """Generate invoices and the truth record. ``dispute=False`` is the counterfactual run."""
    rng = np.random.default_rng(seed)
    dispute_rng = np.random.default_rng([seed, DISPUTE_MONTH])

    customers = {s: _customer_ids(s) for s in SEGMENTS}
    weights = {}
    for s in SEGMENTS:
        raw = rng.lognormal(0.0, 0.7, len(customers[s]))
        weights[s] = raw / raw.sum()

    season_mean = sum(SEASON) / 12.0
    rows: list[dict] = []
    segment_sales: dict[tuple[int, str], float] = {}

    for index in range(1 - WARMUP_MONTHS, REPORT_MONTHS + 1):
        year, month = month_of(index)
        n_days = days_in(index)
        shares = segment_shares(index)
        season = SEASON[month - 1] / season_mean
        for segment in SEGMENTS:
            target = (
                BASE_MONTHLY_SALES * season * shares[segment] * (1.0 + rng.normal(0.0, SHARE_NOISE))
            )
            n = max(3, int(rng.poisson(target / MEAN_INVOICE[segment])))
            raw = rng.lognormal(0.0, AMOUNT_SIGMA, n)
            amounts = np.round(raw / raw.sum() * target, 2)
            days = rng.integers(1, n_days + 1, n)
            who = rng.choice(len(customers[segment]), n, p=weights[segment])
            lags = np.maximum(
                1, np.rint(_lognormal(rng, true_mean_lag(segment, index), LAG_CV, n))
            ).astype(int)
            terms = TERMS_DAYS[segment]
            for amount, day, w, lag in zip(amounts, days, who, lags, strict=True):
                invoice_date = date(year, month, int(day))
                rows.append(
                    {
                        "customer": customers[segment][w],
                        "segment": segment,
                        "invoice_date": invoice_date,
                        "due_date": invoice_date + timedelta(days=terms),
                        "amount": float(amount),
                        "paid_date": invoice_date + timedelta(days=int(lag)),
                    }
                )
            if index >= 1:
                segment_sales[(index, segment)] = float(amounts.sum())

    dispute_row = _dispute_invoice(dispute_rng, customers["contractors"], dispute)
    rows.append(dispute_row)

    invoices = pd.DataFrame(rows)
    invoices = invoices.sort_values(
        ["invoice_date", "segment", "customer", "amount"], kind="stable"
    )
    invoices = invoices.reset_index(drop=True)
    invoices.insert(0, "invoice_id", [f"INV-{n:06d}" for n in range(1, len(invoices) + 1)])

    data_end = date(*month_of(REPORT_MONTHS), days_in(REPORT_MONTHS))
    invoices["paid_date"] = invoices["paid_date"].where(invoices["paid_date"] <= data_end, None)
    is_dispute = (
        (invoices["amount"] == DISPUTE_AMOUNT)
        & (invoices["invoice_date"] == dispute_row["invoice_date"])
        & (invoices["customer"] == dispute_row["customer"])
    )
    dispute_id = invoices.loc[is_dispute, "invoice_id"].iloc[0]

    truth = _truth(seed, segment_sales, dispute, dispute_row, dispute_id)
    return GeneratedData(invoices=invoices, truth=truth)


def _dispute_invoice(rng: np.random.Generator, contractors: list[str], dispute: bool) -> dict:
    """The disputed invoice. The same random draws are consumed whether or not it is disputed."""
    customer = contractors[int(rng.integers(0, len(contractors)))]
    normal_lag = int(round(_lognormal(rng, MEAN_LAG_DAYS["contractors"], LAG_CV)))
    year, month = month_of(DISPUTE_MONTH)
    remaining = days_in(DISPUTE_MONTH) - DISPUTE_DAY
    # Paid after month-end either way, so every month-end before the next month is identical.
    normal_lag = max(normal_lag, remaining + 1)
    invoice_date = date(year, month, DISPUTE_DAY)
    lag = DISPUTE_HOLD_DAYS if dispute else normal_lag
    return {
        "customer": customer,
        "segment": "contractors",
        "invoice_date": invoice_date,
        "due_date": invoice_date + timedelta(days=TERMS_DAYS["contractors"]),
        "amount": DISPUTE_AMOUNT,
        "paid_date": invoice_date + timedelta(days=lag),
    }


def _truth(
    seed: int,
    segment_sales: dict[tuple[int, str], float],
    dispute: bool,
    dispute_row: dict,
    dispute_id: str,
) -> dict:
    mean_lag: dict[str, dict[str, float]] = {}
    for index in range(1, REPORT_MONTHS + 1):
        entry: dict[str, float] = {}
        lag_sum = amount_sum = 0.0
        for segment in SEGMENTS:
            amount = segment_sales[(index, segment)]
            entry[segment] = true_mean_lag(segment, index)
            lag_sum += amount * entry[segment]
            amount_sum += amount
        entry["All"] = round(lag_sum / amount_sum, 4)
        mean_lag[str(index)] = entry
    return {
        "seed": seed,
        "origin_month": f"{ORIGIN_YEAR}-01",
        "warmup_months": WARMUP_MONTHS,
        "report_months": REPORT_MONTHS,
        "terms_days": TERMS_DAYS,
        "base_mean_lag_days": MEAN_LAG_DAYS,
        "lag_cv": LAG_CV,
        "seasonal_peak_to_trough": max(SEASON) / min(SEASON),
        "mean_lag_definition": (
            "the generator's mean pay lag per segment and month; All weights the segments by "
            "that month's invoiced sales, excluding the disputed invoice"
        ),
        "mean_lag": mean_lag,
        "events": {
            "homebuilder_slowdown": {
                "from_month": HOMEBUILDER_SHIFT_MONTH,
                "extra_days": HOMEBUILDER_SHIFT_DAYS,
            },
            "dealer_mix_shift": {
                "from_month": MIX_START_MONTH,
                "to_month": MIX_END_MONTH,
                "dealer_share_start": DEALER_SHARE_START,
                "dealer_share_end": DEALER_SHARE_END,
            },
            "dispute": {
                "enabled": dispute,
                "month": DISPUTE_MONTH,
                "invoice_id": dispute_id,
                "customer": dispute_row["customer"],
                "amount": DISPUTE_AMOUNT,
                "hold_days": DISPUTE_HOLD_DAYS,
            },
        },
    }


def write(data: GeneratedData, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    frame = data.invoices.copy()
    for column in ("invoice_date", "due_date", "paid_date"):
        frame[column] = frame[column].map(
            lambda d: "" if d is None or pd.isna(d) else d.isoformat()
        )
    frame.to_csv(out / "invoices.csv", index=False, float_format="%.2f")
    (out / "truth.json").write_text(json.dumps(data.truth, indent=1) + "\n")
