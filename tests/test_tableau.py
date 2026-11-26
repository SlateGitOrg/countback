from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from pathlib import PurePath

import pandas as pd
import pytest

from countback import tableau
from countback.metrics import METRICS
from countback.twb_eval import evaluate, parse
from countback.twb_validate import validate_workbook

from .conftest import REPO


@pytest.fixture(scope="module")
def exported(metrics, tmp_path_factory):
    out = tmp_path_factory.mktemp("tableau")
    tableau.export(metrics, out)
    return out


def _mutate(exported, tmp_path, transform):
    target = tmp_path / "tableau"
    shutil.copytree(exported, target)
    twb = target / tableau.WORKBOOK_FILE
    twb.write_text(transform(twb.read_text()))
    return twb


def test_export_writes_both_csvs_and_the_workbook(exported):
    assert (exported / "data" / "dso_monthly.csv").is_file()
    assert (exported / "data" / "dso_bridge.csv").is_file()
    assert (exported / "Countback.twb").is_file()


def test_monthly_csv_has_one_row_per_month_and_segment_plus_an_all_row(exported):
    frame = pd.read_csv(exported / "data" / "dso_monthly.csv")
    assert list(frame.columns) == tableau.MONTHLY_COLUMNS
    assert len(frame) == 48 * 4
    assert frame.groupby("month_index")["segment"].apply(lambda s: (s == "All").sum()).eq(1).all()
    segments = frame[frame["segment"] != "All"].groupby("month_index")[["sales", "ar_total"]].sum()
    total = frame[frame["segment"] == "All"].set_index("month_index")[["sales", "ar_total"]]
    assert (segments - total).abs().max().max() < 0.05
    bucket_sum = frame[["ar_current", "ar_1_30", "ar_31_60", "ar_61_90", "ar_90_plus"]].sum(axis=1)
    assert (bucket_sum - frame["ar_total"]).abs().max() < 0.05


def test_bridge_csv_is_exact_and_covers_year_over_year_and_featured_pairs(exported):
    frame = pd.read_csv(exported / "data" / "dso_bridge.csv")
    assert list(frame.columns) == tableau.BRIDGE_COLUMNS
    pairs = set(zip(frame["from_month"], frame["to_month"], strict=True))
    assert {(m - 12, m) for m in range(13, 49)} <= pairs
    assert (29, 33) in pairs
    total = frame[frame["segment"] == "All"]
    assert (total["mix"] + total["behaviour"] - total["change"]).abs().max() < 1e-5
    per_segment = frame[frame["segment"] != "All"]
    assert (
        per_segment["mix"] + per_segment["behaviour"] - per_segment["change"]
    ).abs().max() < 1e-5


def test_freshly_exported_workbook_validates(exported):
    report = validate_workbook(exported / "Countback.twb")
    assert report.ok, report.summary()
    assert report.datasources == 2
    assert report.calculations == 4
    assert report.worksheets == 4
    assert report.dashboards == 1
    assert report.rows_evaluated == 48 * 4


def test_shipped_workbook_validates_and_matches_a_fresh_export(exported):
    shipped = REPO / "tableau"
    report = validate_workbook(shipped / "Countback.twb")
    assert report.ok, report.summary()
    assert (shipped / "Countback.twb").read_text() == (exported / "Countback.twb").read_text()
    for name in ("dso_monthly.csv", "dso_bridge.csv"):
        old = pd.read_csv(shipped / "data" / name)
        new = pd.read_csv(exported / "data" / name)
        assert list(old.columns) == list(new.columns)
        numeric = old.select_dtypes("number").columns
        pd.testing.assert_frame_equal(old[numeric], new[numeric], atol=1e-3, rtol=1e-6)


def test_workbook_structure(exported):
    root = ET.parse(exported / "Countback.twb").getroot()
    sources = root.findall("./datasources/datasource")
    assert [s.find(".//connection[@class='textscan']").get("filename") for s in sources] == [
        "dso_monthly.csv",
        "dso_bridge.csv",
    ]
    calcs = {
        c.get("caption"): c.find("calculation").get("formula")
        for c in sources[0].findall("./column")
    }
    assert calcs["Simple DSO"] == "SUM([ar_total]) / SUM([sales_per_day_3m])"
    assert set(calcs) == set(METRICS)
    names = [w.get("name") for w in root.findall("./worksheets/worksheet")]
    assert names == ["DSO method comparison", "ADD by segment", "Aging mix", "DSO bridge"]
    zones = {z.get("name") for z in root.iter("zone") if z.get("name")}
    assert zones == set(names)


def test_misspelt_field_in_a_calculation_fails_and_names_the_field(exported, tmp_path):
    twb = _mutate(
        exported,
        tmp_path,
        lambda text: text.replace(
            "SUM([ar_current])) / SUM([ar_total])", "SUM([ar_current])) / SUM([AR Totl])"
        ),
    )
    report = validate_workbook(twb)
    assert not report.ok
    assert any("[AR Totl]" in e and "Percent Current" in e for e in report.errors)


def test_unregistered_calculated_field_name_fails(exported, tmp_path):
    twb = _mutate(exported, tmp_path, lambda t: t.replace('caption="ADD"', 'caption="Delinquency"'))
    assert any("not in the metric registry" in e for e in validate_workbook(twb).errors)


def test_worksheet_referencing_an_unknown_column_fails(exported, tmp_path):
    twb = _mutate(
        exported,
        tmp_path,
        lambda t: t.replace(
            'column="[month_index]" derivation="None"', 'column="[month_idx]" derivation="None"', 1
        ),
    )
    assert any("[month_idx]" in e for e in validate_workbook(twb).errors)


def test_shelf_using_an_undeclared_instance_fails(exported, tmp_path):
    twb = _mutate(
        exported,
        tmp_path,
        lambda t: t.replace(
            "<cols>[federated.dso_monthly].[none:month_index:qk]",
            "<cols>[federated.dso_monthly].[sum:month_index:qk]",
            1,
        ),
    )
    assert any("sum:month_index:qk" in e for e in validate_workbook(twb).errors)


def test_worksheet_with_an_undefined_datasource_fails(exported, tmp_path):
    twb = _mutate(
        exported,
        tmp_path,
        lambda t: t.replace('datasource="federated.dso_bridge"', 'datasource="federated.nope"'),
    )
    assert any("federated.nope" in e for e in validate_workbook(twb).errors)


def test_dashboard_zone_naming_a_missing_worksheet_fails(exported, tmp_path):
    twb = _mutate(
        exported, tmp_path, lambda t: t.replace('name="Aging mix" w=', 'name="Aging mixx" w=')
    )
    assert any("Aging mixx" in e for e in validate_workbook(twb).errors)


def test_a_calculation_that_disagrees_with_its_reference_is_caught(exported, tmp_path):
    twb = _mutate(
        exported,
        tmp_path,
        lambda t: t.replace(
            "SUM([dso_countback]) - SUM([bpdso_countback])",
            "SUM([dso_countback]) + SUM([bpdso_countback])",
        ),
    )
    report = validate_workbook(twb)
    assert any("'ADD' differs from its reference" in e for e in report.errors)


def test_unparseable_xml_is_reported(tmp_path):
    bad = tmp_path / "bad.twb"
    bad.write_text("<workbook><datasources>")
    report = validate_workbook(bad)
    assert not report.ok and "cannot parse" in report.errors[0]


def test_missing_csv_is_reported(exported, tmp_path):
    target = tmp_path / "t"
    shutil.copytree(exported, target)
    (target / "data" / "dso_monthly.csv").unlink()
    assert any("does not exist" in e for e in validate_workbook(target / "Countback.twb").errors)


def test_evaluated_calculations_equal_the_pipeline_numbers(exported, metrics):
    """Simple DSO, ADD and Percent Current from the workbook formulas vs the Python metrics."""
    frame = pd.read_csv(exported / "data" / "dso_monthly.csv")
    reference = metrics.set_index(["month_index", "segment"])
    root = ET.parse(exported / "Countback.twb").getroot()
    formulas = {
        c.get("caption"): parse(c.find("calculation").get("formula"))
        for c in root.findall("./datasources/datasource")[0].findall("./column")
    }
    for _, row in frame.iterrows():
        key = (int(row["month_index"]), row["segment"])
        values = row.to_dict()
        want = reference.loc[key]
        simple = evaluate(formulas["Simple DSO"], [values], {}.get)
        assert simple == pytest.approx(want["dso_simple"], abs=1e-6)
        assert evaluate(formulas["ADD"], [values], {}.get) == pytest.approx(want["add"], abs=1e-6)
        current = evaluate(formulas["Percent Current"], [values], {}.get)
        assert current == pytest.approx(want["ar_current"] / want["ar_total"], abs=1e-6)


def test_workbook_data_directories_are_relative(exported):
    root = ET.parse(exported / "Countback.twb").getroot()
    directories = [c.get("directory") for c in root.iter("connection") if c.get("directory")]
    assert directories == ["data", "data"]
    assert not any(PurePath(d).is_absolute() or ":" in d for d in directories)
