"""Month-end receivable snapshots and aging buckets, computed in DuckDB SQL.

An invoice is open at a month-end if it is dated on or before that day and was not paid on or
before it. Days past due are counted from the due date; an invoice due on the snapshot day is
still current.
"""

from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from countback.generator import SEGMENTS

ALL = "All"
BUCKETS = ("ar_current", "ar_1_30", "ar_31_60", "ar_61_90", "ar_90_plus")
BUCKET_LABELS = {
    "ar_current": "current",
    "ar_1_30": "1-30",
    "ar_31_60": "31-60",
    "ar_61_90": "61-90",
    "ar_90_plus": "90+",
}
DEFAULT_WARMUP = 6

_LOAD_SQL = """
CREATE TABLE invoices AS
SELECT invoice_id, customer, segment, invoice_date, due_date, amount, paid_date
FROM read_csv(
    ?, header = true,
    columns = {
        'invoice_id': 'VARCHAR', 'customer': 'VARCHAR', 'segment': 'VARCHAR',
        'invoice_date': 'DATE', 'due_date': 'DATE', 'amount': 'DOUBLE', 'paid_date': 'DATE'
    }
)
"""

_SNAPSHOT_SQL = """
WITH open_at AS (
    SELECT m.abs_idx, i.segment, i.amount, date_diff('day', i.due_date, m.month_end) AS dpd
    FROM months m
    JOIN invoices i
      ON i.invoice_date <= m.month_end
     AND (i.paid_date IS NULL OR i.paid_date > m.month_end)
)
SELECT
    abs_idx,
    COALESCE(segment, 'All') AS segment,
    SUM(amount) AS ar_total,
    SUM(CASE WHEN dpd <= 0 THEN amount ELSE 0 END) AS ar_current,
    SUM(CASE WHEN dpd BETWEEN 1 AND 30 THEN amount ELSE 0 END) AS ar_1_30,
    SUM(CASE WHEN dpd BETWEEN 31 AND 60 THEN amount ELSE 0 END) AS ar_31_60,
    SUM(CASE WHEN dpd BETWEEN 61 AND 90 THEN amount ELSE 0 END) AS ar_61_90,
    SUM(CASE WHEN dpd > 90 THEN amount ELSE 0 END) AS ar_90_plus,
    COUNT(*) AS open_invoices
FROM open_at
GROUP BY GROUPING SETS ((abs_idx, segment), (abs_idx))
"""

_SALES_SQL = """
SELECT
    m.abs_idx,
    COALESCE(i.segment, 'All') AS segment,
    SUM(i.amount) AS sales
FROM months m
JOIN invoices i ON i.invoice_date BETWEEN m.month_start AND m.month_end
GROUP BY GROUPING SETS ((m.abs_idx, i.segment), (m.abs_idx))
"""


def ordered_segments(found: list[str]) -> list[str]:
    known = [s for s in SEGMENTS if s in found]
    return known + sorted(s for s in found if s not in SEGMENTS)


class Ledger:
    """Invoices loaded into DuckDB plus the month calendar they are snapshotted on.

    ``abs_idx`` numbers every month from the first invoice month (0). ``month_index`` is the
    reporting index: month 1 is the first month after ``warmup`` months of history, which exist
    only so the first reported month-end has sales to count back through.
    """

    def __init__(self, invoices_csv: str | Path, warmup: int = DEFAULT_WARMUP):
        self.warmup = warmup
        self.con = duckdb.connect()
        self.con.execute(_LOAD_SQL, [str(invoices_csv)])
        first, last = self.con.execute(
            "SELECT MIN(invoice_date), MAX(invoice_date) FROM invoices"
        ).fetchall()[0]
        self.months = self._calendar(first, last, warmup)
        self.con.register("months", self.months)
        self.segments = ordered_segments(
            [r[0] for r in self.con.execute("SELECT DISTINCT segment FROM invoices").fetchall()]
        )

    @staticmethod
    def _calendar(first, last, warmup: int) -> pd.DataFrame:
        rows = []
        year, month = first.year, first.month
        abs_idx = 0
        while (year, month) <= (last.year, last.month):
            days = calendar.monthrange(year, month)[1]
            rows.append(
                {
                    "abs_idx": abs_idx,
                    "month_index": abs_idx - warmup + 1,
                    "month_start": date(year, month, 1),
                    "month_end": date(year, month, days),
                    "days": days,
                }
            )
            abs_idx += 1
            year, month = (year, month + 1) if month < 12 else (year + 1, 1)
        return pd.DataFrame(rows)

    @property
    def last_month(self) -> int:
        return int(self.months["month_index"].max())

    def snapshots(self) -> pd.DataFrame:
        """AR and aging buckets for every (month, segment) plus the All rows."""
        frame = self.con.execute(_SNAPSHOT_SQL).df()
        return self._complete(frame, ["ar_total", *BUCKETS, "open_invoices"])

    def sales(self) -> pd.DataFrame:
        return self._complete(self.con.execute(_SALES_SQL).df(), ["sales"])

    def _complete(self, frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
        """Fill (month, segment) combinations with no activity with zeros."""
        grid = pd.MultiIndex.from_product(
            [self.months["abs_idx"], [*self.segments, ALL]], names=["abs_idx", "segment"]
        )
        frame = frame.set_index(["abs_idx", "segment"]).reindex(grid)[columns].fillna(0.0)
        return frame.reset_index()
