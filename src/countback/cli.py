"""Countback DSO: generate data, age receivables, bridge DSO changes, export and check Tableau."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import typer

from countback import generator, tableau
from countback.aging import ALL, BUCKET_LABELS, BUCKETS, DEFAULT_WARMUP, Ledger
from countback.bridge import shapley_bridge
from countback.dso import build_metrics
from countback.twb_validate import validate_workbook

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)

DataOption = typer.Option(Path("data"), "--data", help="Folder holding invoices.csv.")
WarmupOption = typer.Option(
    DEFAULT_WARMUP, "--warmup", help="Months of history before reporting month 1."
)


def _metrics(data: Path, warmup: int) -> tuple[Ledger, pd.DataFrame]:
    csv_path = data / "invoices.csv"
    if not csv_path.is_file():
        typer.echo(f"{csv_path} not found; run `countback generate --out {data}` first.", err=True)
        raise typer.Exit(2)
    ledger = Ledger(csv_path, warmup)
    return ledger, build_metrics(ledger)


def _truth(data: Path) -> dict | None:
    path = data / "truth.json"
    return json.loads(path.read_text()) if path.is_file() else None


def _money(value: float) -> str:
    return f"{value:,.0f}"


@app.command()
def generate(
    seed: int = typer.Option(7, help="Random seed."),
    out: Path = typer.Option(Path("data"), help="Output folder."),
    dispute: bool = typer.Option(True, help="Hold the 180,000 contractor invoice in dispute."),
) -> None:
    """Write invoices.csv and truth.json from the seeded generator."""
    data = generator.generate(seed, dispute)
    generator.write(data, out)
    inv = data.invoices
    typer.echo(f"wrote {len(inv):,} invoices to {out}/invoices.csv and {out}/truth.json")


@app.command()
def aging(
    data: Path = DataOption,
    month: int | None = typer.Option(None, help="Reporting month index (default: last)."),
    warmup: int = WarmupOption,
) -> None:
    """Open invoices and aging buckets at a month-end, by segment and in total."""
    ledger, metrics = _metrics(data, warmup)
    month = month or ledger.last_month
    rows = metrics[metrics["month_index"] == month]
    if rows.empty:
        typer.echo(f"month {month} is outside 1..{ledger.last_month}", err=True)
        raise typer.Exit(2)
    snapshots = ledger.snapshots().merge(ledger.months[["abs_idx", "month_index"]], on="abs_idx")
    open_count = snapshots[snapshots["month_index"] == month].set_index("segment")["open_invoices"]
    end = rows["month_end"].iloc[0]
    typer.echo(f"AR aging at month {month} ({end})")
    header = f"{'segment':<14}{'open':>7}{'AR':>14}" + "".join(
        f"{BUCKET_LABELS[b]:>13}" for b in BUCKETS
    )
    typer.echo(header)
    for _, row in rows.iterrows():
        line = (
            f"{row['segment']:<14}{int(open_count[row['segment']]):>7}{_money(row['ar_total']):>14}"
        )
        line += "".join(f"{_money(row[b]):>13}" for b in BUCKETS)
        typer.echo(line)


@app.command()
def dso(
    data: Path = DataOption,
    segment: str = typer.Option(ALL, help="Segment name or All."),
    last: int = typer.Option(12, help="Number of most recent months to show."),
    warmup: int = WarmupOption,
) -> None:
    """Simple DSO, countback DSO, BPDSO and ADD by month (days)."""
    ledger, metrics = _metrics(data, warmup)
    rows = metrics[metrics["segment"] == segment]
    if rows.empty:
        typer.echo(f"unknown segment {segment!r}; choose from {[*ledger.segments, ALL]}", err=True)
        raise typer.Exit(2)
    truth = _truth(data)
    rows = rows.tail(last)
    columns = f"{'month':>6}{'end':>12}{'simple':>9}{'countback':>11}{'BPDSO':>8}{'ADD':>7}"
    if truth:
        columns += f"{'true lag':>10}"
    typer.echo(f"{segment}: days")
    typer.echo(columns)
    flagged = []
    for _, row in rows.iterrows():
        line = (
            f"{int(row['month_index']):>6}{str(row['month_end']):>12}{row['dso_simple']:>9.1f}"
            f"{row['dso_countback']:>11.1f}{row['bpdso_countback']:>8.1f}{row['add']:>7.1f}"
        )
        if truth:
            line += f"{truth['mean_lag'][str(int(row['month_index']))][segment]:>10.1f}"
        if row["dso_flagged"]:
            line += "  *"
            flagged.append(int(row["month_index"]))
        typer.echo(line)
    if flagged:
        typer.echo(f"* AR exceeds all available sales history in months {flagged}; not capped")


@app.command()
def bridge(
    data: Path = DataOption,
    month_from: int = typer.Option(..., "--from", help="Starting month index."),
    month_to: int = typer.Option(..., "--to", help="Ending month index."),
    warmup: int = WarmupOption,
) -> None:
    """Split the change in the segment-weighted countback DSO into mix and behaviour."""
    _, metrics = _metrics(data, warmup)
    try:
        result = shapley_bridge(metrics, month_from, month_to)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Shapley bridge, month {month_from} -> {month_to} (days)")
    typer.echo(
        f"{'segment':<14}{'w from':>8}{'w to':>8}{'DSO from':>10}{'DSO to':>8}"
        f"{'mix':>8}{'behaviour':>11}"
    )
    for seg in result.segments:
        typer.echo(
            f"{seg.segment:<14}{seg.weight_from:>8.3f}{seg.weight_to:>8.3f}"
            f"{seg.dso_from:>10.1f}{seg.dso_to:>8.1f}{seg.mix:>8.2f}{seg.behaviour:>11.2f}"
        )
    typer.echo(
        f"index I: {result.index_from:.2f} -> {result.index_to:.2f}  change {result.change:+.2f}"
    )
    total = result.mix + result.behaviour
    typer.echo(f"  mix {result.mix:+.2f}  behaviour {result.behaviour:+.2f}  (sum {total:+.2f})")
    if result.mix_share is not None:
        typer.echo(
            f"  mix share {result.mix_share:.0%}  behaviour share {result.behaviour_share:.0%}"
        )
    typer.echo(
        f"portfolio countback DSO: {result.portfolio_from:.2f} -> {result.portfolio_to:.2f}; "
        f"residual (portfolio - I): {result.residual_from:+.2f} -> {result.residual_to:+.2f}"
    )


@app.command("export-tableau")
def export_tableau(
    data: Path = DataOption,
    out: Path = typer.Option(Path("tableau"), help="Output folder for the workbook and data/."),
    warmup: int = WarmupOption,
) -> None:
    """Write tableau/data/dso_monthly.csv, dso_bridge.csv and tableau/Countback.twb."""
    _, metrics = _metrics(data, warmup)
    paths = tableau.export(metrics, out)
    for label, path in paths.items():
        typer.echo(f"wrote {label}: {path}")


@app.command("validate-twb")
def validate_twb(
    path: Path = typer.Argument(Path("tableau") / tableau.WORKBOOK_FILE, help="Workbook to check."),
) -> None:
    """Check references and evaluate every calculated field against the Python reference."""
    report = validate_workbook(path)
    typer.echo(report.summary())
    if not report.ok:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
