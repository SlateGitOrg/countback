"""Static and numeric validation of the Tableau workbook.

Checks, in order
1. the XML parses;
2. every ``[Field]`` in every calculation resolves to a CSV column or to another calculation
   of the same datasource (no cycles), and every datasource CSV exists with the columns the
   workbook declares;
3. every calculated field name is in the metric registry;
4. every worksheet's datasource, column, column-instance and shelf references resolve, and
   every dashboard zone names a worksheet;
5. each calculation is evaluated with the mini evaluator on every row of its CSV and compared
   with the Python reference from the registry.
"""

from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from countback.metrics import METRICS
from countback.twb_eval import EvalError, evaluate, field_refs, parse

TOLERANCE = 1e-6
_SHELF_REF = re.compile(r"\[([^\]]+)\]\.\[([^\]]+)\]")


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    datasources: int = 0
    calculations: int = 0
    worksheets: int = 0
    dashboards: int = 0
    rows_evaluated: int = 0
    max_abs_diff: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if not self.ok:
            lines = [f"FAILED: {len(self.errors)} problem(s)"]
            lines += [f"  - {e}" for e in self.errors]
            return "\n".join(lines)
        return (
            f"OK: {self.datasources} datasources, {self.calculations} calculated fields, "
            f"{self.worksheets} worksheets, {self.dashboards} dashboard; "
            f"evaluated {self.calculations} calculations on {self.rows_evaluated} rows "
            f"(max abs diff vs reference {self.max_abs_diff:.2e})"
        )


@dataclass
class _Source:
    name: str
    internal: str
    csv_path: Path | None
    columns: set[str]
    calcs: dict[str, str]  # internal name -> formula
    captions: dict[str, str]  # internal name -> user-facing name


def _strip(name: str) -> str:
    return name[1:-1] if name.startswith("[") and name.endswith("]") else name


def _load_sources(root: ET.Element, base: Path, report: Report) -> dict[str, _Source]:
    sources: dict[str, _Source] = {}
    for ds in root.findall("./datasources/datasource"):
        internal = ds.get("name", "")
        label = ds.get("caption") or internal
        connection = ds.find(".//connection[@class='textscan']")
        if connection is None:
            report.errors.append(f"datasource {label!r} has no textscan connection")
            continue
        path = base / connection.get("directory", "") / connection.get("filename", "")
        columns = {c.get("name", "") for c in ds.findall(".//relation/columns/column")}
        calcs, captions = {}, {}
        for column in ds.findall("./column"):
            calculation = column.find("calculation")
            if calculation is not None:
                internal_name = _strip(column.get("name", ""))
                calcs[internal_name] = calculation.get("formula", "")
                captions[internal_name] = column.get("caption") or internal_name
        sources[internal] = _Source(label, internal, path, columns, calcs, captions)
        if not path.is_file():
            report.errors.append(f"datasource {label!r}: CSV {path} does not exist")
        else:
            with path.open(newline="") as handle:
                header = next(csv.reader(handle), [])
            missing = sorted(columns - set(header))
            if missing:
                report.errors.append(
                    f"datasource {label!r}: declared columns missing from {path.name}: {missing}"
                )
    report.datasources = len(sources)
    return sources


def _check_calculations(sources: dict[str, _Source], report: Report) -> dict[str, dict]:
    """Resolve references in every calculation; returns the parsed trees per datasource."""
    trees: dict[str, dict] = {}
    for source in sources.values():
        trees[source.internal] = {}
        for name, formula in source.calcs.items():
            report.calculations += 1
            shown = source.captions[name]
            if shown not in METRICS:
                report.errors.append(
                    f"calculated field {shown!r} in {source.name!r} is not in the metric registry"
                )
            try:
                tree = parse(formula)
            except EvalError as exc:
                report.errors.append(f"calculation {shown!r}: {exc}")
                continue
            trees[source.internal][name] = tree
            for ref in field_refs(tree):
                if ref not in source.columns and ref not in source.calcs:
                    report.errors.append(
                        f"calculation {shown!r} in {source.name!r} references unknown field [{ref}]"
                    )
        for name in trees[source.internal]:
            if _cycle(name, trees[source.internal], set()):
                report.errors.append(f"calculation {name!r} refers to itself")
    return trees


def _cycle(name: str, trees: dict, path: set[str]) -> bool:
    if name in path:
        return True
    for ref in field_refs(trees[name]):
        if ref in trees and _cycle(ref, trees, path | {name}):
            return True
    return False


def _check_worksheets(root: ET.Element, sources: dict[str, _Source], report: Report) -> None:
    sheet_names = set()
    for sheet in root.findall("./worksheets/worksheet"):
        name = sheet.get("name", "")
        sheet_names.add(name)
        report.worksheets += 1
        instances: dict[str, set[str]] = {}
        for decl in sheet.findall("./table/view/datasources/datasource"):
            if decl.get("name") not in sources:
                report.errors.append(
                    f"worksheet {name!r}: datasource {decl.get('name')!r} is not defined"
                )
        for deps in sheet.findall("./table/view/datasource-dependencies"):
            ds_name = deps.get("datasource", "")
            source = sources.get(ds_name)
            if source is None:
                report.errors.append(
                    f"worksheet {name!r}: dependencies on undefined datasource {ds_name!r}"
                )
                continue
            known = source.columns | set(source.calcs)
            instances[ds_name] = set()
            for column in deps.findall("column"):
                field_name = _strip(column.get("name", ""))
                if field_name not in known:
                    report.errors.append(
                        f"worksheet {name!r}: column [{field_name}] not in datasource "
                        f"{source.name!r}"
                    )
            for inst in deps.findall("column-instance"):
                field_name = _strip(inst.get("column", ""))
                instances[ds_name].add(_strip(inst.get("name", "")))
                if field_name not in known:
                    report.errors.append(
                        f"worksheet {name!r}: column-instance of [{field_name}] not in "
                        f"datasource {source.name!r}"
                    )
        for text in _shelf_texts(sheet):
            for ds_name, instance in _SHELF_REF.findall(text):
                if ds_name not in sources:
                    report.errors.append(
                        f"worksheet {name!r}: shelf names unknown datasource [{ds_name}]"
                    )
                elif instance not in instances.get(ds_name, set()):
                    report.errors.append(
                        f"worksheet {name!r}: [{instance}] is used but not declared in its "
                        f"datasource dependencies"
                    )
    dashboards = root.findall("./dashboards/dashboard")
    report.dashboards = len(dashboards)
    for dashboard in dashboards:
        for zone in dashboard.iter("zone"):
            zone_name = zone.get("name")
            if zone_name and zone_name not in sheet_names:
                report.errors.append(
                    f"dashboard {dashboard.get('name')!r}: zone {zone_name!r} is not a worksheet"
                )


def _shelf_texts(sheet: ET.Element) -> list[str]:
    texts = [(e.text or "") for e in sheet.iter() if e.tag in ("rows", "cols")]
    for element in sheet.iter():
        for attribute in ("column", "level"):
            value = element.get(attribute)
            if value and "].[" in value:
                texts.append(value)
    return texts


def _read_rows(path: Path) -> list[dict[str, float | None]]:
    rows = []
    with path.open(newline="") as handle:
        for raw in csv.DictReader(handle):
            row: dict[str, float | None] = {}
            for key, value in raw.items():
                try:
                    row[key] = float(value)
                except (TypeError, ValueError):
                    row[key] = None
            rows.append(row)
    return rows


def _evaluate_against_reference(
    sources: dict[str, _Source], trees: dict[str, dict], report: Report
) -> None:
    for source in sources.values():
        if not trees[source.internal] or source.csv_path is None or not source.csv_path.is_file():
            continue
        rows = _read_rows(source.csv_path)
        report.rows_evaluated = max(report.rows_evaluated, len(rows))
        own = trees[source.internal]

        def lookup(name: str, own=own):
            return own.get(name)

        for name, tree in own.items():
            shown = source.captions[name]
            if shown not in METRICS or any(
                r not in source.columns | set(own) for r in field_refs(tree)
            ):
                continue
            metric = METRICS[shown]
            mismatches = 0
            for number, row in enumerate(rows, start=2):
                try:
                    got = evaluate(tree, [row], lookup)
                except EvalError as exc:
                    report.errors.append(f"calculation {shown!r}: {exc}")
                    break
                want = metric.reference(row)
                if (got is None) != (want is None):
                    mismatches += 1
                    detail = f"{got!r} vs reference {want!r}"
                elif got is not None and abs(got - want) > TOLERANCE:
                    mismatches += 1
                    detail = f"{got!r} vs reference {want!r}"
                else:
                    if got is not None:
                        report.max_abs_diff = max(report.max_abs_diff, abs(got - want))
                    continue
                if mismatches <= 3:
                    report.errors.append(
                        f"calculation {shown!r} differs from its reference on CSV line "
                        f"{number}: {detail}"
                    )


def validate_workbook(path: str | Path) -> Report:
    """Validate the workbook at ``path``; the CSVs are resolved relative to its folder."""
    path = Path(path)
    report = Report()
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        report.errors.append(f"cannot parse {path.name}: {exc}")
        return report
    sources = _load_sources(root, path.parent, report)
    trees = _check_calculations(sources, report)
    _check_worksheets(root, sources, report)
    _evaluate_against_reference(sources, trees, report)
    return report
