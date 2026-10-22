"""The Results tab's tables: a run's measure-level results filtered to one PA and shown at five levels, with columns chosen by a saved view. No tkinter here.

Levels (`LEVELS`): measure, program (PrgID), program sector (the program's PrimarySector from the Programs list), program segment (its ProgramSegment) and
portfolio. Every level above the measure is a sum over its measures; ratio columns are recomputed from the summed benefits and costs (`outputs.group_ratios`,
the same definition the portfolio summary and CET's run receipt use), never averaged.

The PA of a measure (`pa_of`) is its program's administrator: the `PA` of its ProgramCost row (`CreditedIOU`), else the program's PA in the Programs list
(`Prg_PA`), else the IOU territory the measure is priced in. So a REN or CCA program filters under the REN or CCA, and an unallocated statewide program under SW.

Columns: `catalog(frame)` lists what a view can show - the run's additive output columns (benefits, costs, savings, emissions, bill impacts, components ...),
the recomputed ratios and a few derived sums (total CO2 avoided). Inputs, `poc_` fields, measure-level ratios, shares and statuses are not offered: they do not
add up across measures. `DEFAULT_VIEW` is the built-in view; users save their own (`load_views` / `save_views`, kept in the tool's config file).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from . import outputs, paths, templates

MAX_MEASURE_ROWS = 5000            # above this many measures (after the PA filter) the measure-level tab is turned off: open the measure-level file instead
LEVELS = [("measure", "Measure level"), ("program", "Program level"), ("sector", "Program sector level"), ("segment", "Program segment level"),
          ("portfolio", "Portfolio level")]
ALL_PAS = "All PAs"
VIEWS_KEY = "gui_result_views"
DEFAULT_VIEW_NAME = "Default"
DEFAULT_VIEW = ["TotalExpenditures", "TotalSystemBenefit", "TRC_ratio", "PAC_ratio", "RIM_ratio", "SCB_ratio", "SCH_ratio", "FirstYearNetKWh", "FirstYearNetKW",
                "FirstYearNetThm", "TotalCO2Lifecycle"]


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str          # "sum" (added up over measures), "ratio" (recomputed from sums), "derived" (a sum of other columns)
    fmt: str           # "usd", "ratio", "energy", "kw", "tonnes", "lbs", "number"
    parts: tuple = ()  # for "derived": the columns added together
    help: str = ""


RATIOS = [
    Column("TRC_ratio", "TRC", "ratio", "ratio", help="Total Resource Cost ratio: (benefits + tax credits) / TRC cost"),
    Column("PAC_ratio", "PAC", "ratio", "ratio", help="Program Administrator Cost ratio: benefits / PAC cost"),
    Column("RIM_ratio", "RIM", "ratio", "ratio", help="Ratepayer Impact Measure ratio: (benefits + bill increases) / RIM cost"),
    Column("SCB_ratio", "SCT Base", "ratio", "ratio", help="Societal Cost Test, base social cost of carbon: SB benefits / SCB cost"),
    Column("SCH_ratio", "SCT High", "ratio", "ratio", help="Societal Cost Test, high social cost of carbon: SH benefits / SCH cost"),
    Column("PCT_ratio", "PCT", "ratio", "ratio", help="Participant Cost Test ratio (gross): (bill reductions + incentives + tax credits) / (measure cost + bill increases)"),
    Column("TRC_NoAdmin_ratio", "TRC (no admin)", "ratio", "ratio", help="TRC ratio without program-level costs"),
    Column("PAC_NoAdmin_ratio", "PAC (no admin)", "ratio", "ratio", help="PAC ratio without program-level costs"),
    Column("RIM_NoAdmin_ratio", "RIM (no admin)", "ratio", "ratio", help="RIM ratio without program-level costs"),
    Column("SCB_NoAdmin_ratio", "SCT Base (no admin)", "ratio", "ratio", help="SCT Base ratio without program-level costs"),
    Column("SCH_NoAdmin_ratio", "SCT High (no admin)", "ratio", "ratio", help="SCT High ratio without program-level costs"),
]
DERIVED = [
    Column("TotalCO2Lifecycle", "CO2 avoided, lifecycle (t)", "derived", "tonnes", ("NetElecCO2Lifecycle", "NetGasCO2Lifecycle"),
           "Net electric + net gas CO2 avoided over the measures' lives, metric tons"),
    Column("TotalCO2FirstYear", "CO2 avoided, first year (t)", "derived", "tonnes", ("NetElecCO2", "NetGasCO2"),
           "Net electric + net gas CO2 avoided in the measures' first year, metric tons"),
]
# Friendlier labels (and formats) for the sum columns most views use; any other additive output column is offered under its own name.
LABELS = {
    "TotalExpenditures": ("Budget / expenditures ($)", "usd"), "TotalSystemBenefit": ("Total system benefit ($)", "usd"),
    "FirstYearNetKWh": ("First-year net kWh", "energy"), "FirstYearNetKW": ("First-year net kW", "kw"), "FirstYearNetThm": ("First-year net therms", "energy"),
    "RebatesandIncents": ("Rebates and incentives ($)", "usd"), "WeightedProgramCost": ("Program costs ($)", "usd"),
    "ElecBen": ("Electric benefits ($)", "usd"), "GasBen": ("Gas benefits ($)", "usd"), "TRCCost": ("TRC cost ($)", "usd"), "PACCost": ("PAC cost ($)", "usd"),
    "RIMCost": ("RIM cost ($)", "usd"), "SCBCost": ("SCT Base cost ($)", "usd"), "SCHCost": ("SCT High cost ($)", "usd"),
    "PCTBen": ("PCT benefits ($)", "usd"), "PCTCost": ("PCT cost ($)", "usd"),
}
NOT_ADDITIVE = {"WeightedBenefits", "WeightedElecAlloc", "WeightedSavings"}


def pa_of(frame: pd.DataFrame) -> pd.Series:
    """Each measure's PA: CreditedIOU, else Prg_PA, else the IOU territory (see the module docstring)."""
    out = pd.Series("", index=frame.index, dtype=object)
    for col in ("CreditedIOU", "Prg_PA", "IOU_AC_Territory", "poc_PA"):
        if col in frame:
            v = frame[col].fillna("").astype(str).str.strip()
            out = out.where(out != "", v)
    return out.replace("", "(blank)")


def _is_ratio_column(c: str) -> bool:
    return "Ratio" in c or c.endswith("_ratio")


def catalog(frame: pd.DataFrame) -> dict[str, Column]:
    """Every column a view can show for this run, in a stable order: the default view's columns first, then ratios, derived sums, and the other additive outputs."""
    inputs = set(templates.MEASURE_COLUMNS)
    sums: dict[str, Column] = {}
    for c in frame.columns:
        if c in inputs or c.startswith(("poc_", "Prg_")) or c in NOT_ADDITIVE or _is_ratio_column(c):
            continue
        if not pd.api.types.is_numeric_dtype(frame[c]):
            continue
        label, fmt = LABELS.get(c, (c, _guess_fmt(c)))
        sums[c] = Column(c, label, "sum", fmt)
    cols: dict[str, Column] = {}
    ratio_ok = {r.key: r for r in RATIOS}
    derived = {d.key: d for d in DERIVED if all(p in frame for p in d.parts)}
    for key in DEFAULT_VIEW:
        col = sums.get(key) or ratio_ok.get(key) or derived.get(key)
        if col is not None:
            cols[key] = col
    for d in (ratio_ok, derived, sums):
        for k, v in d.items():
            cols.setdefault(k, v)
    return cols


def _guess_fmt(c: str) -> str:
    if "CO2" in c:
        return "tonnes"
    if "NOx" in c or "PM10" in c:
        return "lbs"
    if c.endswith(("KWh", "kWh", "Thm", "Therms")):
        return "energy"
    if c.endswith(("KW", "kW")):
        return "kw"
    return "usd"


def available_pas(frame: pd.DataFrame) -> list[str]:
    return [ALL_PAS] + sorted(pa_of(frame).unique())


def filter_pa(frame: pd.DataFrame, pa: str) -> pd.DataFrame:
    return frame if pa in (None, "", ALL_PAS) else frame[pa_of(frame) == pa]


def _group_key(frame: pd.DataFrame, level: str) -> pd.Series | None:
    if level == "program":
        return frame["PrgID"].astype(str) if "PrgID" in frame else None
    col = {"sector": "Prg_PrimarySector", "segment": "Prg_ProgramSegment"}.get(level)
    if col is None:
        return None
    if col not in frame:
        return pd.Series("(not in the program metadata)", index=frame.index)
    return frame[col].fillna("").astype(str).str.strip().replace("", "(blank)")


def _values(g: pd.DataFrame, cols: list[Column]) -> dict:
    ratios = None
    row = {}
    for c in cols:
        if c.kind == "ratio":
            if ratios is None:
                ratios = outputs.group_ratios(g)
            row[c.key] = ratios.get(c.key, math.nan)
        elif c.kind == "derived":
            row[c.key] = float(sum(g[p].sum() for p in c.parts)) if all(p in g for p in c.parts) else math.nan
        else:
            row[c.key] = float(g[c.key].sum()) if c.key in g and g[c.key].notna().any() else math.nan
    return row


def _row_values(f: pd.DataFrame, cols: list[Column]) -> pd.DataFrame:
    """Measure level: each measure's own values (vectorised); ratios from the same definition as the groups (outputs.row_ratios)."""
    out = pd.DataFrame(index=f.index)
    ratios = outputs.row_ratios(f) if any(c.kind == "ratio" for c in cols) else None
    for c in cols:
        if c.kind == "ratio":
            out[c.key] = ratios[c.key] if c.key in ratios else math.nan
        elif c.kind == "derived":
            out[c.key] = sum(f[p].astype(float) for p in c.parts) if all(p in f for p in c.parts) else math.nan
        else:
            out[c.key] = f[c.key].astype(float) if c.key in f else math.nan
    return out


def table(frame: pd.DataFrame, level: str, pa: str, keys: list[str]) -> pd.DataFrame:
    """The rows of one level for one PA, with identifier columns first and then `keys` (unknown keys are skipped). Values are numbers, unformatted."""
    cat = catalog(frame)
    cols = [cat[k] for k in keys if k in cat]
    f = filter_pa(frame, pa)
    pas = pa_of(f)
    if level == "measure":
        ids = pd.DataFrame({"CEInputID": f.get("CEInputID", pd.Series(index=f.index, dtype=object)),
                            "Description": f.get("MeasDescription", pd.Series("", index=f.index)), "PrgID": f.get("PrgID", pd.Series("", index=f.index)),
                            "PA": pas})
        return pd.concat([ids, _row_values(f, cols)], axis=1).reset_index(drop=True)
    if level == "portfolio":
        label = "All PAs" if pa in (None, "", ALL_PAS) else pa
        return pd.DataFrame([{"Portfolio": label, "Measures": len(f), **_values(f, cols)}])
    key = _group_key(f, level)
    name = {"program": "PrgID", "sector": "Program sector", "segment": "Program segment"}[level]
    rows = []
    if key is not None:
        for k, g in f.groupby(key, sort=True):
            row = {name: k}
            if level == "program":
                row["Program name"] = str(g["Prg_ProgramName"].iloc[0]) if "Prg_ProgramName" in g else ""
                row["PA"] = ", ".join(sorted(pas.loc[g.index].unique()))
            row["Measures"] = len(g)
            row.update(_values(g, cols))
            rows.append(row)
    return pd.DataFrame(rows)


def measure_level_allowed(frame: pd.DataFrame, pa: str) -> bool:
    return len(filter_pa(frame, pa)) <= MAX_MEASURE_ROWS


def format_value(col: Column | None, v) -> str:
    """How a number is shown: dollars and energy without decimals and with thousands separators, ratios to 2 decimals, blank for missing."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if col is None or not isinstance(v, (int, float)):
        return str(v)
    fmt = col.fmt
    if fmt == "ratio":
        return f"{v:,.2f}"
    if fmt == "usd":
        return f"${v:,.0f}" if v >= 0 else f"-${-v:,.0f}"
    if fmt == "energy":
        return f"{v:,.0f}"
    if fmt in ("kw", "tonnes", "lbs"):
        return f"{v:,.1f}"
    return f"{v:,.2f}"


# ----------------------------------------------------------------------------------------------------------------------------- saved views
def load_views() -> tuple[dict[str, list[str]], str]:
    """({view name: column keys}, the view last used). The built-in Default view is always there and cannot be changed."""
    saved = paths.read_config().get(VIEWS_KEY) or {}
    views = {DEFAULT_VIEW_NAME: list(DEFAULT_VIEW)}
    for name, keys in (saved.get("views") or {}).items():
        if name != DEFAULT_VIEW_NAME and isinstance(keys, list):
            views[name] = [str(k) for k in keys]
    current = saved.get("current") if saved.get("current") in views else DEFAULT_VIEW_NAME
    return views, current


def save_views(views: dict[str, list[str]], current: str) -> None:
    paths.save_config(**{VIEWS_KEY: {"views": {k: v for k, v in views.items() if k != DEFAULT_VIEW_NAME}, "current": current}})
