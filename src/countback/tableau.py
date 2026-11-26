"""Export the DSO tables as CSV and build the Tableau workbook (``.twb``) that reads them.

The workbook is plain XML: two ``textscan`` datasources, the calculated fields from
``countback.metrics``, four worksheets and one dashboard. Tableau cannot run in CI, so the file
is checked statically and by evaluation in ``countback.twb_validate``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from countback.aging import ALL
from countback.bridge import shapley_bridge
from countback.metrics import METRICS

MONTHLY_FILE = "dso_monthly.csv"
BRIDGE_FILE = "dso_bridge.csv"
WORKBOOK_FILE = "Countback.twb"
DATA_DIR = "data"
FEATURED_BRIDGES = ((29, 33),)
VERSION = "18.1"

MONTHLY_COLUMNS = [
    "month_index",
    "month_end",
    "segment",
    "sales",
    "sales_per_day_3m",
    "ar_total",
    "ar_current",
    "ar_1_30",
    "ar_31_60",
    "ar_61_90",
    "ar_90_plus",
    "dso_countback",
    "bpdso_countback",
    "dso_flagged",
]
BRIDGE_COLUMNS = [
    "from_month",
    "to_month",
    "kind",
    "segment",
    "w_from",
    "w_to",
    "dso_from",
    "dso_to",
    "mix",
    "behaviour",
    "change",
    "index_from",
    "index_to",
    "portfolio_from",
    "portfolio_to",
]


MONEY_COLUMNS = ["sales", "ar_total", "ar_current", "ar_1_30", "ar_31_60", "ar_61_90", "ar_90_plus"]


def monthly_frame(metrics: pd.DataFrame) -> pd.DataFrame:
    frame = metrics[MONTHLY_COLUMNS].copy()
    frame["month_end"] = pd.to_datetime(frame["month_end"]).dt.strftime("%Y-%m-%d")
    frame[MONEY_COLUMNS] = frame[MONEY_COLUMNS].round(2)
    for column in ("sales_per_day_3m", "dso_countback", "bpdso_countback"):
        frame[column] = frame[column].round(6)
    return frame


def bridge_frame(
    metrics: pd.DataFrame, extra: tuple[tuple[int, int], ...] = FEATURED_BRIDGES
) -> pd.DataFrame:
    """Year-over-year bridges for every month with a prior year, plus the featured pairs."""
    last = int(metrics["month_index"].max())
    pairs = [(m - 12, m, "yoy") for m in range(13, last + 1)]
    pairs += [(a, b, "featured") for a, b in extra]
    rows = []
    for start, end, kind in pairs:
        bridge = shapley_bridge(metrics, start, end)
        common = {
            "from_month": start,
            "to_month": end,
            "kind": kind,
            "index_from": bridge.index_from,
            "index_to": bridge.index_to,
            "portfolio_from": bridge.portfolio_from,
            "portfolio_to": bridge.portfolio_to,
        }
        for seg in bridge.segments:
            rows.append(
                {
                    **common,
                    "segment": seg.segment,
                    "w_from": seg.weight_from,
                    "w_to": seg.weight_to,
                    "dso_from": seg.dso_from,
                    "dso_to": seg.dso_to,
                    "mix": seg.mix,
                    "behaviour": seg.behaviour,
                    "change": seg.change,
                }
            )
        rows.append(
            {
                **common,
                "segment": ALL,
                "w_from": 1.0,
                "w_to": 1.0,
                "dso_from": bridge.index_from,
                "dso_to": bridge.index_to,
                "mix": bridge.mix,
                "behaviour": bridge.behaviour,
                "change": bridge.change,
            }
        )
    frame = pd.DataFrame(rows)[BRIDGE_COLUMNS]
    numeric = [c for c in BRIDGE_COLUMNS if frame[c].dtype.kind == "f"]
    frame[numeric] = frame[numeric].round(6)
    return frame


# --- workbook ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """A CSV datasource: its file, typed columns and the calculated fields defined on it."""

    name: str
    filename: str
    columns: list[tuple[str, str]]  # (column, datatype)
    calcs: tuple[str, ...] = ()

    @property
    def internal(self) -> str:
        return f"federated.{self.name}"

    def datatype(self, column: str) -> str:
        if column in self.calcs:
            return "real"
        return dict(self.columns)[column]


def _datatype(series: pd.Series, name: str) -> str:
    if name == "month_end":
        return "date"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "real"
    return "string"


def sources_for(monthly: pd.DataFrame, bridge: pd.DataFrame) -> tuple[Source, Source]:
    def typed(frame: pd.DataFrame) -> list[tuple[str, str]]:
        return [(c, _datatype(frame[c], c)) for c in frame.columns]

    return (
        Source("dso_monthly", MONTHLY_FILE, typed(monthly), tuple(METRICS)),
        Source("dso_bridge", BRIDGE_FILE, typed(bridge)),
    )


_DIMENSIONS = {"segment", "month_index", "month_end", "kind", "from_month", "to_month"}
# kind -> (derivation, instance prefix, instance suffix, field type)
_INSTANCE = {
    "sum": ("Sum", "sum", "qk", "quantitative"),
    "calc": ("User", "usr", "qk", "quantitative"),
    "dim": ("None", "none", "nk", "nominal"),
    "cont": ("None", "none", "qk", "quantitative"),
}


@dataclass(frozen=True)
class Ref:
    kind: str  # sum | calc | dim | cont
    field: str

    def instance(self) -> str:
        _, prefix, suffix, _ = _INSTANCE[self.kind]
        return f"[{prefix}:{self.field}:{suffix}]"


def _datasource_element(source: Source) -> ET.Element:
    ds = ET.Element(
        "datasource",
        {
            "caption": source.name,
            "inline": "true",
            "name": source.internal,
            "version": VERSION,
        },
    )
    connection = ET.SubElement(ds, "connection", {"class": "federated"})
    named = ET.SubElement(connection, "named-connections")
    nc = ET.SubElement(
        named, "named-connection", {"caption": DATA_DIR, "name": f"textscan.{source.name}"}
    )
    ET.SubElement(
        nc,
        "connection",
        {
            "class": "textscan",
            "directory": DATA_DIR,
            "filename": source.filename,
            "password": "",
            "server": "",
        },
    )
    relation = ET.SubElement(
        connection,
        "relation",
        {
            "connection": f"textscan.{source.name}",
            "name": source.filename,
            "table": f"[{source.filename.removesuffix('.csv')}#csv]",
            "type": "table",
        },
    )
    columns = ET.SubElement(
        relation,
        "columns",
        {"character-set": "UTF-8", "header": "yes", "locale": "en_US", "separator": ","},
    )
    for ordinal, (name, datatype) in enumerate(source.columns):
        ET.SubElement(
            columns,
            "column",
            {"datatype": datatype, "name": name, "ordinal": str(ordinal)},
        )
    for name in source.calcs:
        ds.append(_calc_column(name))
    return ds


def _calc_column(name: str) -> ET.Element:
    column = ET.Element(
        "column",
        {
            "caption": name,
            "datatype": "real",
            "name": f"[{name}]",
            "role": "measure",
            "type": "quantitative",
        },
    )
    ET.SubElement(column, "calculation", {"class": "tableau", "formula": METRICS[name].formula})
    return column


def _dependencies(source: Source, refs: list[Ref]) -> ET.Element:
    deps = ET.Element("datasource-dependencies", {"datasource": source.internal})
    seen: set[str] = set()
    for ref in refs:
        if ref.field in seen:
            continue
        seen.add(ref.field)
        if ref.field in source.calcs:
            deps.append(_calc_column(ref.field))
        else:
            dimension = ref.field in _DIMENSIONS
            ET.SubElement(
                deps,
                "column",
                {
                    "datatype": source.datatype(ref.field),
                    "name": f"[{ref.field}]",
                    "role": "dimension" if dimension else "measure",
                    "type": "ordinal"
                    if ref.field == "month_index"
                    else ("nominal" if dimension else "quantitative"),
                },
            )
    done: set[str] = set()
    for ref in refs:
        if ref.instance() in done:
            continue
        done.add(ref.instance())
        derivation, _, _, ftype = _INSTANCE[ref.kind]
        ET.SubElement(
            deps,
            "column-instance",
            {
                "column": f"[{ref.field}]",
                "derivation": derivation,
                "name": ref.instance(),
                "pivot": "key",
                "type": ftype,
            },
        )
    return deps


def _shelf(source: Source, refs: list[Ref]) -> str:
    parts = [f"[{source.internal}].{ref.instance()}" for ref in refs]
    return parts[0] if len(parts) == 1 else "(" + " + ".join(parts) + ")"


def _segment_filter(source: Source, *, only_all: bool) -> ET.Element:
    column = f"[{source.internal}].[none:segment:nk]"
    flt = ET.Element("filter", {"class": "categorical", "column": column})
    attrs = {"user:ui-domain": "database", "user:ui-marker": "enumerate"}
    if only_all:
        ET.SubElement(
            flt,
            "groupfilter",
            {
                "function": "member",
                "level": "[none:segment:nk]",
                "member": '"All"',
                "user:ui-enumeration": "inclusive",
                **attrs,
            },
        )
    else:
        group = ET.SubElement(
            flt, "groupfilter", {"function": "except", "user:ui-enumeration": "exclusive", **attrs}
        )
        ET.SubElement(
            group, "groupfilter", {"function": "level-members", "level": "[none:segment:nk]"}
        )
        ET.SubElement(
            group,
            "groupfilter",
            {"function": "member", "level": "[none:segment:nk]", "member": '"All"'},
        )
    return flt


def _worksheet(
    name: str,
    source: Source,
    rows: list[Ref],
    cols: list[Ref],
    mark: str,
    *,
    only_all: bool,
    color: Ref | None = None,
    tooltips: tuple[Ref, ...] = (),
    extra_filter: tuple[Ref, str] | None = None,
) -> ET.Element:
    sheet = ET.Element("worksheet", {"name": name})
    table = ET.SubElement(sheet, "table")
    view = ET.SubElement(table, "view")
    datasources = ET.SubElement(view, "datasources")
    ET.SubElement(datasources, "datasource", {"caption": source.name, "name": source.internal})
    used = [*rows, *cols, Ref("dim", "segment")]
    if color:
        used.append(color)
    used.extend(tooltips)
    if extra_filter:
        used.append(extra_filter[0])
    view.append(_dependencies(source, used))
    view.append(_segment_filter(source, only_all=only_all))
    if extra_filter:
        ref, member = extra_filter
        flt = ET.SubElement(
            view,
            "filter",
            {"class": "categorical", "column": f"[{source.internal}].{ref.instance()}"},
        )
        ET.SubElement(
            flt,
            "groupfilter",
            {
                "function": "member",
                "level": ref.instance(),
                "member": f'"{member}"',
                "user:ui-domain": "database",
                "user:ui-enumeration": "inclusive",
                "user:ui-marker": "enumerate",
            },
        )
    panes = ET.SubElement(table, "panes")
    pane = ET.SubElement(panes, "pane")
    pane_view = ET.SubElement(pane, "view")
    ET.SubElement(pane_view, "breakdown", {"value": "auto"})
    ET.SubElement(pane, "mark", {"class": mark})
    if color or tooltips:
        encodings = ET.SubElement(pane, "encodings")
        if color:
            ET.SubElement(encodings, "color", {"column": f"[{source.internal}].{color.instance()}"})
        for tip in tooltips:
            ET.SubElement(encodings, "tooltip", {"column": f"[{source.internal}].{tip.instance()}"})
    ET.SubElement(table, "rows").text = _shelf(source, rows)
    ET.SubElement(table, "cols").text = _shelf(source, cols)
    return sheet


WORKSHEETS = ("DSO method comparison", "ADD by segment", "Aging mix", "DSO bridge")
DASHBOARD = "Countback DSO"


def build_workbook(monthly: pd.DataFrame, bridge: pd.DataFrame) -> ET.ElementTree:
    month_src, bridge_src = sources_for(monthly, bridge)
    root = ET.Element(
        "workbook",
        {
            "original-version": VERSION,
            "source-platform": "win",
            "version": VERSION,
            "xmlns:user": "http://www.tableausoftware.com/xml/user",
        },
    )
    datasources = ET.SubElement(root, "datasources")
    datasources.append(_datasource_element(month_src))
    datasources.append(_datasource_element(bridge_src))

    month = Ref("cont", "month_index")
    buckets = [
        Ref("sum", c) for c in ("ar_current", "ar_1_30", "ar_31_60", "ar_61_90", "ar_90_plus")
    ]
    worksheets = ET.SubElement(root, "worksheets")
    worksheets.append(
        _worksheet(
            WORKSHEETS[0],
            month_src,
            [Ref("calc", "Simple DSO"), Ref("sum", "dso_countback"), Ref("sum", "bpdso_countback")],
            [month],
            "Line",
            only_all=True,
        )
    )
    worksheets.append(
        _worksheet(
            WORKSHEETS[1],
            month_src,
            [Ref("calc", "ADD")],
            [month],
            "Line",
            only_all=False,
            color=Ref("dim", "segment"),
        )
    )
    worksheets.append(
        _worksheet(
            WORKSHEETS[2],
            month_src,
            buckets,
            [month],
            "Area",
            only_all=True,
            tooltips=(Ref("calc", "Percent Current"), Ref("calc", "Past Due 60 Plus Share")),
        )
    )
    worksheets.append(
        _worksheet(
            WORKSHEETS[3],
            bridge_src,
            [Ref("sum", "mix"), Ref("sum", "behaviour")],
            [Ref("cont", "to_month")],
            "Bar",
            only_all=True,
            extra_filter=(Ref("dim", "kind"), "yoy"),
        )
    )

    dashboards = ET.SubElement(root, "dashboards")
    dashboard = ET.SubElement(dashboards, "dashboard", {"name": DASHBOARD})
    ET.SubElement(
        dashboard,
        "size",
        {"maxheight": "800", "maxwidth": "1200", "minheight": "800", "minwidth": "1200"},
    )
    zones = ET.SubElement(dashboard, "zones")
    outer = ET.SubElement(
        zones,
        "zone",
        {"h": "100000", "id": "1", "type-v2": "layout-basic", "w": "100000", "x": "0", "y": "0"},
    )
    for n, sheet_name in enumerate(WORKSHEETS):
        ET.SubElement(
            outer,
            "zone",
            {
                "h": "50000",
                "id": str(n + 2),
                "name": sheet_name,
                "w": "50000",
                "x": str(50000 * (n % 2)),
                "y": str(50000 * (n // 2)),
            },
        )

    windows = ET.SubElement(root, "windows", {"source-height": "30"})
    for sheet_name in WORKSHEETS:
        window = ET.SubElement(windows, "window", {"class": "worksheet", "name": sheet_name})
        ET.SubElement(window, "cards")
    dash_window = ET.SubElement(
        windows, "window", {"class": "dashboard", "maximized": "true", "name": DASHBOARD}
    )
    ET.SubElement(dash_window, "viewpoints")
    ET.SubElement(dash_window, "active", {"id": "-1"})

    ET.indent(root, space="  ")
    return ET.ElementTree(root)


def export(metrics: pd.DataFrame, out: Path) -> dict[str, Path]:
    """Write the two CSVs and the workbook under ``out``; returns the paths written."""
    data_dir = out / DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    monthly = monthly_frame(metrics)
    bridge = bridge_frame(metrics)
    paths = {
        "monthly": data_dir / MONTHLY_FILE,
        "bridge": data_dir / BRIDGE_FILE,
        "workbook": out / WORKBOOK_FILE,
    }
    monthly.to_csv(paths["monthly"], index=False, lineterminator="\n")
    bridge.to_csv(paths["bridge"], index=False, lineterminator="\n")
    monthly_read = pd.read_csv(paths["monthly"])
    bridge_read = pd.read_csv(paths["bridge"])
    build_workbook(monthly_read, bridge_read).write(
        paths["workbook"], encoding="utf-8", xml_declaration=True
    )
    return paths
