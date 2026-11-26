from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd


def total_series(metrics: pd.DataFrame, column: str, segment: str = "All") -> pd.Series:
    rows = metrics[metrics["segment"] == segment]
    return rows.set_index("month_index")[column]


def write_invoices(path: Path, rows: list[tuple]) -> Path:
    """Write a hand-built invoices.csv: (id, customer, segment, invoice, due, amount, paid)."""
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["invoice_id", "customer", "segment", "invoice_date", "due_date", "amount", "paid_date"]
        )
        writer.writerows(rows)
    return path
