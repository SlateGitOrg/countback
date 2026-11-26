from __future__ import annotations

import shutil

import pytest
from typer.testing import CliRunner

from countback.cli import app

from .conftest import REPO

runner = CliRunner()


def test_generate_writes_the_data_files(tmp_path):
    result = runner.invoke(app, ["generate", "--seed", "3", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "invoices.csv").is_file() and (tmp_path / "truth.json").is_file()
    assert "invoices" in result.output


def test_aging_prints_buckets_that_add_up(data_dir):
    result = runner.invoke(app, ["aging", "--data", str(data_dir), "--month", "48"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("AR aging at month 48 (2025-12-31)")
    total = next(line for line in lines if line.startswith("All"))
    figures = [int(x.replace(",", "")) for x in total.split()[1:]]
    open_count, ar, buckets = figures[0], figures[1], figures[2:]
    assert open_count > 100
    assert abs(sum(buckets) - ar) <= 3  # each bucket is rounded to whole dollars


def test_dso_table_shows_true_lag_next_to_the_estimates(data_dir):
    result = runner.invoke(app, ["dso", "--data", str(data_dir), "--last", "3"])
    assert result.exit_code == 0, result.output
    assert "true lag" in result.output
    assert len(result.output.splitlines()) == 2 + 3


def test_dso_rejects_an_unknown_segment(data_dir):
    result = runner.invoke(app, ["dso", "--data", str(data_dir), "--segment", "retail"])
    assert result.exit_code == 2


def test_bridge_prints_the_mix_and_behaviour_split(data_dir):
    result = runner.invoke(app, ["bridge", "--data", str(data_dir), "--from", "29", "--to", "33"])
    assert result.exit_code == 0, result.output
    assert "behaviour share" in result.output
    assert "residual" in result.output


def test_bridge_rejects_a_month_outside_the_data(data_dir):
    result = runner.invoke(app, ["bridge", "--data", str(data_dir), "--from", "1", "--to", "99"])
    assert result.exit_code == 2


def test_missing_data_folder_gives_a_hint(tmp_path):
    result = runner.invoke(app, ["aging", "--data", str(tmp_path / "nope")])
    assert result.exit_code == 2


def test_export_then_validate_round_trip(data_dir, tmp_path):
    out = tmp_path / "tableau"
    export = runner.invoke(app, ["export-tableau", "--data", str(data_dir), "--out", str(out)])
    assert export.exit_code == 0, export.output
    check = runner.invoke(app, ["validate-twb", str(out / "Countback.twb")])
    assert check.exit_code == 0, check.output
    assert check.output.startswith("OK: 2 datasources, 4 calculated fields")


def test_validate_twb_exits_non_zero_on_a_broken_workbook(tmp_path):
    shutil.copytree(REPO / "tableau", tmp_path / "t")
    twb = tmp_path / "t" / "Countback.twb"
    twb.write_text(twb.read_text().replace("SUM([ar_total])", "SUM([AR Totl])", 1))
    result = runner.invoke(app, ["validate-twb", str(twb)])
    assert result.exit_code == 1
    assert "[AR Totl]" in result.output


@pytest.mark.parametrize(
    "command", ["generate", "aging", "dso", "bridge", "export-tableau", "validate-twb"]
)
def test_every_command_has_help(command):
    assert runner.invoke(app, [command, "--help"]).exit_code == 0
