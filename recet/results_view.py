"""The Results tab's tables: a run's measure-level results filtered to one PA and to the program years picked, and shown at six levels, with columns chosen by
a saved view. No tkinter here.

Levels (`LEVELS`): measure, program (PrgID), program year (the claim year, `Used_Year`; BRD BR-04.1), program sector (the program's PrimarySector from the
Programs list), program segment (its ProgramSegment) and portfolio. `years` (a list of program years; empty = every year) limits every level to those
years, so a multi-year filing's tests and TSB can be totalled over the years chosen. Every level above the measure is a sum over its measures; ratio columns are recomputed from the summed benefits and costs (`outputs.group_ratios`,
the same definition the portfolio summary and CET's run receipt use), never averaged.

The PA of a measure (`pa_of`) is its program's administrator: the `PA` of its ProgramCost row (`CreditedPA`), else the program's PA in the Programs list
(`Prg_PA`), else the IOU territory the measure is priced in. So a REN or CCA program filters under the REN or CCA, and an unallocated statewide program under SW.

Columns: `catalog(frame)` lists what a view can show - the run's additive output columns (benefits, costs, savings, emissions, bill impacts, components ...),
the recomputed ratios and a few derived sums (total CO2 avoided). Inputs, derived pricing inputs (`Used_Year`, `AC_Net_kWh_1st_Baseline` ...), measure-level ratios, shares and statuses are not offered: they do not
add up across measures. `DEFAULT_VIEW` is the built-in view; users save their own (`load_views` / `save_views`, kept in the tool's config file).

Memory (`RunResults`): every level above the measure is built from the run's GROUP SUMS -- one row per (PA, PrgID, program name, sector, segment, year) with each
summable column added up and the measure count -- not from the measure rows. For a results file they are computed by reading the file in batches of only the
columns needed and kept beside it (`<RESULTS_DIR>/group_sums2_<file>.parquet`), so a 1,000,000-measure run is never loaded whole (it took about 4 GB). The measure
level is offered only for a run of at most `MAX_RUN_MEASURE_ROWS` measures, and shows at most `MAX_MEASURE_ROWS` rows (pick one PA); its rows are read from the
file the same way, filtered to the PA as they are read. A larger run's measure-level table is in its download only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import outputs, paths, templates

MAX_MEASURE_ROWS = 5000            # above this many measures (after the PA filter) the measure-level tab is turned off: open the measure-level file instead
MAX_RUN_MEASURE_ROWS = 50_000      # a run with more measures than this has no measure-level tab at all (its measure-level table is in the download only)
BATCH_ROWS = 20_000                # rows read at a time from a results file
KEYS = ["_pa", "_prg", "_prgname", "_sector", "_segment", "_year"]      # the group-sum keys (names start with "_" so the catalog never offers them)
COUNT = "_n"
CUBE_VERSION = 2                   # in the kept group-sums file's name: change it when the group sums change, so old files are rebuilt (2: + _year, 0.1.60)
LEVELS = [("measure", "Measure level"), ("program", "Program level"), ("year", "Program year level"), ("sector", "Program sector level"),
          ("segment", "Program segment level"), ("portfolio", "Portfolio level")]
YEAR_COLUMNS = ("Used_Year", "poc_year")      # a measure's program year (poc_year: a run written before 0.1.50)
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
    Column("TotalCO2Lifecycle", "CO2 avoided, lifecycle (t)", "derived", "tonnes", ("LifecycleNetElecCO2", "LifecycleNetGasCO2"),
           "Net electric + net gas CO2 avoided over the measures' lives, metric tons"),
    Column("TotalCO2FirstYear", "CO2 avoided, first year (t)", "derived", "tonnes", ("FirstYearNetElecCO2", "FirstYearNetGasCO2"),
           "Net electric + net gas CO2 avoided in the measures' first year, metric tons"),
]
# Friendlier labels (and formats) for the sum columns most views use; any other additive output column is offered under its own name.
LABELS = {
    "TotalExpenditures": ("Budget / expenditures ($)", "usd"), "TotalSystemBenefit": ("Total system benefit ($)", "usd"),
    "FirstYearNetKWh": ("First-year net kWh", "energy"), "FirstYearNetKW": ("First-year net kW", "kw"), "FirstYearNetThm": ("First-year net therms", "energy"),
    "RebatesandIncents": ("Rebates and incentives ($)", "usd"), "WeightedProgramCost": ("Program costs ($)", "usd"),
    "ElecBen": ("Electric benefits ($)", "usd"), "GasBen": ("Gas benefits ($)", "usd"), "TRCCost": ("TRC cost ($)", "usd"), "PACCost": ("PAC cost ($)", "usd"),
    "RIMCost": ("RIM cost ($)", "usd"), "SCBCost": ("SCT Base cost ($)", "usd"), "SCHCost": ("SCT High cost ($)", "usd"),
    "TRCBen": ("TRC benefits ($)", "usd"), "PACBen": ("PAC benefits ($)", "usd"), "RIMBen": ("RIM benefits ($)", "usd"),
    "SCBBen": ("SCT Base benefits ($)", "usd"), "SCHBen": ("SCT High benefits ($)", "usd"),
    "PCTBen": ("PCT benefits ($)", "usd"), "PCTCost": ("PCT cost ($)", "usd"),
}
NOT_ADDITIVE = {"WeightedBenefits", "WeightedElecAlloc", "WeightedSavings"}     # WeightedSavings: a run written before 0.1.50
_INPUTS = set(templates.MEASURE_COLUMNS)
_DERIVED = {"Used_ACCVersion", *outputs.DERIVED_OUTPUTS}


def pa_of(frame: pd.DataFrame) -> pd.Series:
    """Each measure's PA: CreditedPA, else Prg_PA, else the IOU territory (see the module docstring)."""
    out = pd.Series("", index=frame.index, dtype=object)
    for col in ("CreditedPA", "CreditedIOU", "Prg_PA", "IOU_AC_Territory", "poc_PA"):      # CreditedIOU / poc_PA: a run written before 0.1.50
        if col in frame:
            v = frame[col].fillna("").astype(str).str.strip()
            out = out.where(out != "", v)
    return out.replace("", "(blank)")


def _is_ratio_column(c: str) -> bool:
    """Per-measure ratios, which a group total cannot add up: benefit-cost ratios and the levelized values (dollars per discounted kWh or therm, CalcCost's Lev...)."""
    return "Ratio" in c or c.endswith("_ratio") or c.startswith("Lev")


def _summable(c: str) -> bool:
    """A column a group total may add up (when it is numeric): not an input, a derived pricing input (Used_Year, AC_Net_kWh_1st_Baseline ...; poc_ before
    0.1.50), a Prg_ field, a share, a per-measure ratio or a group-sum key."""
    return not (c in _INPUTS or c in _DERIVED or c.startswith(("poc_", "Prg_", "_")) or c in NOT_ADDITIVE or _is_ratio_column(c))


def catalog(frame: pd.DataFrame) -> dict[str, Column]:
    """Every column a view can show for this run, in a stable order: the default view's columns first, then ratios, derived sums, and the other additive outputs."""
    sums: dict[str, Column] = {}
    for c in frame.columns:
        if not _summable(c) or not pd.api.types.is_numeric_dtype(frame[c]):
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


_WORDS = {"Elec": "electric", "Ben": "benefits", "Bens": "benefits", "Incr": "increase", "Reduc": "reduction", "Incents": "incentives", "Inc": "incentives",
          "Thm": "therms", "Mark": "market", "Effect": "effects", "Rebatesand": "rebates and", "Qty": "quantity", "Meas": "measure", "Wtd": "weighted",
          "Prg": "program", "Gen": "generation", "Seq": "sequestration"}
_KEEP = {"PV", "CO2", "NOx", "PM10", "TRC", "PAC", "RIM", "PCT", "SCB", "SCH", "kWh", "kW", "IOU"}
_SUFFIXES = [("_SB", " (SCT Base)"), ("_SH", " (SCT High)"), ("_SCT", " (SCT)"), ("_NoAdmin", " (no admin)"), ("NoAdmin", " (no admin)")]


def display_label(col: Column) -> str:
    """The web page's name for a column: its label when it has one, else its output-column name in words ("ElecSupplyCost_SB" -> "Electric supply cost
    (SCT Base) ($)"). Only for display: downloads keep each column's label, which for these columns is the output-column name itself."""
    if col.label != col.key:
        return col.label
    import re
    base, tail = col.key, ""
    for suf, words in _SUFFIXES:
        if base.endswith(suf):
            base, tail = base[: -len(suf)], words
            break
    toks = re.findall(r"CO2|NOx|PM10|TRC|PAC|RIM|SCB|SCH|PCT|IOU|PV|KWh|kWh|KW|kW|[A-Z][a-z]+|[a-z]+|[A-Z]+|\d+", base)
    words = []
    for t in toks:
        t = {"KWh": "kWh", "KW": "kW"}.get(t, t)
        words.append(t if t in _KEEP else _WORDS.get(t, t.lower()))
    text = " ".join(words) or col.key
    text = text[:1].upper() + text[1:] + tail
    return text + {"usd": " ($)", "tonnes": " (t)", "lbs": " (lb)"}.get(col.fmt, "")


GROUP_ORDER = ["Ratios", "Benefits and bill impacts", "Costs", "Savings", "Emissions", "Societal Cost Tests (SCT)", "Other"]


def column_group(col: Column) -> str:
    """The Results column picker's group for a column (one of GROUP_ORDER)."""
    k = col.key
    if col.kind == "ratio":
        return "Ratios"
    if any(x in k for x in ("CO2", "NOx", "PM10")):
        return "Emissions"
    if k.endswith(("_SB", "_SH", "_SCT")) or k.startswith(("SCB", "SCH")):
        return "Societal Cost Tests (SCT)"
    if col.fmt in ("energy", "kw") or "Savings" in k:
        return "Savings"
    if "Ben" in k or "Bill" in k:
        return "Benefits and bill impacts"
    if any(x in k for x in ("Cost", "Incent", "Rebate", "Expenditure", "Tax", "Excess")):
        return "Costs"
    return "Other"


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


def filter_pa(frame: pd.DataFrame, pa: str, years=None) -> pd.DataFrame:
    """The measures of one PA (ALL_PAS = every one) in the program years given (None or empty = every year)."""
    f = frame if pa in (None, "", ALL_PAS) else frame[pa_of(frame) == pa]
    if years:
        f = f[year_of(f).isin({str(y) for y in years})]
    return f


def year_of(frame: pd.DataFrame) -> pd.Series:
    """Each measure's program year as text (its claim year, `Used_Year`), else the first four characters of ClaimYearQuarter; "(blank)" if neither."""
    for col in YEAR_COLUMNS:
        if col in frame:
            return pd.to_numeric(frame[col], errors="coerce").astype("Int64").astype(str).replace("<NA>", "(blank)")
    if "ClaimYearQuarter" in frame:
        return frame["ClaimYearQuarter"].fillna("").astype(str).str[:4].replace("", "(blank)")
    return pd.Series("(blank)", index=frame.index)


def parse_years(values) -> list[str]:
    """Program years from a request or a setting: any mix of lists and comma-separated text, as sorted unique four-digit years ("" -> every year)."""
    out = set()
    for v in values if isinstance(values, (list, tuple, set)) else [values]:
        for y in str(v or "").split(","):
            y = y.strip()
            if len(y) == 4 and y.isdigit():
                out.add(y)
    return sorted(out)


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


# ----------------------------------------------------------------------------------------------------------------------------- group sums
def _key_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Each measure's group-sum keys (KEYS): its PA, PrgID and program name (when the table has them), program sector and segment."""
    k = pd.DataFrame({"_pa": pa_of(frame).to_numpy()}, index=frame.index)
    if "PrgID" in frame:
        k["_prg"] = frame["PrgID"].astype(str).to_numpy()
    if "Prg_ProgramName" in frame:
        k["_prgname"] = frame["Prg_ProgramName"].fillna("").astype(str).to_numpy()
    k["_sector"] = _group_key(frame, "sector").to_numpy()
    k["_segment"] = _group_key(frame, "segment").to_numpy()
    k["_year"] = year_of(frame).to_numpy()
    return k


def group_sums(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per distinct (PA, PrgID, program name, sector, segment, program year): COUNT measures and every summable numeric column added up (NaN where it is NaN in
    all of the group's measures, as a sum over the measures would be)."""
    cols = [c for c in frame.columns if _summable(c) and pd.api.types.is_numeric_dtype(frame[c])]
    k = _key_frame(frame)
    keys = list(k.columns)
    x = pd.concat([k, frame[cols].astype(float)], axis=1)
    g = x.groupby(keys, sort=False, dropna=False)
    out = pd.concat([g[cols].sum(min_count=1), g.size().rename(COUNT)], axis=1).copy()   # one block: a wide sum came out fragmented and warned
    return out.reset_index()


def _combine(parts: list[pd.DataFrame]) -> pd.DataFrame:
    """group_sums of a whole table from those of its batches."""
    if len(parts) == 1:
        return parts[0]
    allp = pd.concat(parts, ignore_index=True)
    keys = [c for c in KEYS if c in allp]
    cols = [c for c in allp.columns if c not in keys]
    return allp.groupby(keys, sort=False, dropna=False)[cols].sum(min_count=1).reset_index()


KEY_SOURCES = {"CreditedPA", "CreditedIOU", "Prg_PA", "IOU_AC_Territory", "poc_PA", "PrgID", "Prg_ProgramName", "Prg_PrimarySector", "Prg_ProgramSegment",
               *YEAR_COLUMNS, "ClaimYearQuarter"}


def _needed_for_sums(schema) -> list[str]:
    import pyarrow as pa
    def num(t):
        return pa.types.is_floating(t) or pa.types.is_integer(t) or pa.types.is_boolean(t)
    return [f.name for f in schema if f.name in KEY_SOURCES or (_summable(f.name) and num(f.type))]


def build_group_sums(path: Path) -> pd.DataFrame:
    """group_sums of a results file, reading a parquet file in batches of only the needed columns (any other format is small enough to read whole)."""
    path = Path(path)
    if path.suffix.lower() != ".parquet":
        return group_sums(outputs.read_table(path))
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(path)
    cols = _needed_for_sums(pf.schema_arrow)
    parts = [group_sums(b.to_pandas()) for b in pf.iter_batches(batch_size=BATCH_ROWS, columns=cols, use_threads=False)]
    return _combine(parts) if parts else group_sums(pf.schema_arrow.empty_table().select(cols).to_pandas())


def _kept_path(path: Path) -> Path:
    folder = path.parent if path.parent.name == outputs.RESULTS_DIR else path.parent / outputs.RESULTS_DIR
    return folder / f"group_sums{CUBE_VERSION}_{path.name}.parquet"     # not "measure_level_*": results_source must never pick it


def group_sums_for(path: Path) -> pd.DataFrame:
    """build_group_sums(path), kept beside the run's results (outputs.RESULTS_DIR, which downloads leave out) and rebuilt when the file is newer."""
    path = Path(path)
    kept = _kept_path(path)
    try:
        if kept.exists() and kept.stat().st_mtime >= path.stat().st_mtime:
            return pd.read_parquet(kept)
    except (OSError, ValueError):
        pass
    return _keep(build_group_sums(path), kept)


def _keep(sums: pd.DataFrame, kept: Path) -> pd.DataFrame:
    try:
        kept.parent.mkdir(parents=True, exist_ok=True)
        tmp = kept.with_name(kept.name + ".tmp")
        sums.to_parquet(tmp, index=False)
        tmp.replace(kept)
    except OSError:                                  # a read-only results folder: use the sums without keeping them
        pass
    return sums


def combine_parts(parts: list[pd.DataFrame], columns) -> pd.DataFrame:
    """group_sums of a whole table from the group_sums of its parts. `columns` (the table's columns, in order) orders the result as group_sums of the
    whole table would be: a column a part skipped (e.g. all blank, so not numeric there) would otherwise move to the end."""
    s = _combine(parts)
    keys = [c for c in KEYS if c in s]
    return s[keys + [c for c in columns if c in s and c not in keys and c != COUNT] + [COUNT]]


def keep_group_sums(run_dir: Path, frame: pd.DataFrame | None = None, sums: pd.DataFrame | None = None) -> None:
    """Called at the end of a run, after its results file is written: keep the group sums of that file (outputs.results_source) so its Results page
    opens without reading it. Given as `sums` by a run in parts (combine_parts of each part's group_sums, taken as the part is written), else from `frame`
    when the run holds its measure-level table in memory, else read back from the file."""
    source = outputs.results_source(run_dir)
    if source is None:
        return
    if sums is None and frame is not None:
        sums = group_sums(frame)
    _keep(sums, _kept_path(source)) if sums is not None else group_sums_for(source)


RATIO_INPUTS = {"ElecBen", "GasBen", "WaterEnergyBen", "OtherBen", "ElecBen_SB", "GasBen_SB", "WaterEnergyBen_SB", "ElecBen_SH", "GasBen_SH", "WaterEnergyBen_SH",
                "TaxCredits", "LifecycleBillIncrElec", "LifecycleBillIncrGas", "TRCCost", "PACCost", "RIMCost", "SCBCost", "SCHCost", "TRCCostNoAdmin", "PACCostNoAdmin",
                "RIMCostNoAdmin", "SCBCostNoAdmin", "SCHCostNoAdmin", "PCTBen", "PCTCost",
                "TRCBen", "PACBen", "RIMBen", "SCBBen", "SCHBen"}     # every column outputs._ratio_parts reads


# ----------------------------------------------------------------------------------------------------------------------------- a run's results
class RunResults:
    """A run's Results tables: the group sums (every level above the measure, the PA list, the catalog) and, for the measure level, either the measure rows
    in memory (`from_frame`) or the results file they are read from (`from_file`). See the module docstring."""

    def __init__(self, sums: pd.DataFrame, frame: pd.DataFrame | None = None, path: Path | None = None):
        self.sums, self.frame, self.path = sums, frame, path
        self.n = int(sums[COUNT].sum()) if len(sums) else 0

    @classmethod
    def from_frame(cls, frame: pd.DataFrame) -> "RunResults":
        return cls(group_sums(frame), frame=frame)

    @classmethod
    def from_file(cls, path: Path) -> "RunResults":
        return cls(group_sums_for(path), path=Path(path))

    def catalog(self) -> dict[str, Column]:
        return catalog(self.sums)

    def pas(self) -> list[str]:
        return [ALL_PAS] + sorted(self.sums["_pa"].unique()) if len(self.sums) else [ALL_PAS]

    def years(self) -> list[str]:
        """The run's program years, in order (BR-04.1): the choices of the years filter."""
        return sorted(self.sums["_year"].astype(str).unique()) if len(self.sums) and "_year" in self.sums else []

    def _of(self, pa: str, years=None) -> pd.DataFrame:
        s = self.sums if pa in (None, "", ALL_PAS) else self.sums[self.sums["_pa"] == pa]
        if years and "_year" in s:
            s = s[s["_year"].astype(str).isin({str(y) for y in years})]
        return s

    def count(self, pa: str, years=None) -> int:
        s = self._of(pa, years)
        return int(s[COUNT].sum()) if len(s) else 0

    def measure_level_available(self) -> bool:
        """False for a run too large to show measure by measure at all (MAX_RUN_MEASURE_ROWS): its measure-level table is in the download only."""
        return self.n <= MAX_RUN_MEASURE_ROWS

    def measure_level_allowed(self, pa: str, years=None) -> bool:
        return self.measure_level_available() and self.count(pa, years) <= MAX_MEASURE_ROWS

    def table(self, level: str, pa: str, keys: list[str], years=None) -> pd.DataFrame:
        """The rows of one level for one PA and the program years given (None or empty = every year), with identifier columns first and then `keys`
        (unknown keys are skipped). Values are numbers, unformatted. The measure level must be allowed (measure_level_allowed): it raises otherwise."""
        cat = self.catalog()
        cols = [cat[k] for k in keys if k in cat]
        if level == "measure":
            if not self.measure_level_allowed(pa, years):
                raise ValueError(f"the measure level is off for this run ({self.count(pa, years):,} measures)")
            return _measure_table(self._measure_rows(pa, cols, years), cols)
        s = self._of(pa, years)
        if level == "portfolio":
            label = "All PAs" if pa in (None, "", ALL_PAS) else pa
            row = {"Portfolio": label}
            if years:
                row["Program years"] = ", ".join(str(y) for y in sorted(years))
            return pd.DataFrame([{**row, "Measures": int(s[COUNT].sum()) if len(s) else 0, **_values(s, cols)}])
        key = {"program": "_prg", "year": "_year", "sector": "_sector", "segment": "_segment"}[level]
        name = {"program": "PrgID", "year": "Program year", "sector": "Program sector", "segment": "Program segment"}[level]
        rows = []
        if key in s:
            for k, g in s.groupby(key, sort=True):
                row = {name: k}
                if level == "program":
                    row["Program name"] = str(g["_prgname"].iloc[0]) if "_prgname" in g else ""
                    row["PA"] = ", ".join(sorted(g["_pa"].unique()))
                row["Measures"] = int(g[COUNT].sum())
                row.update(_values(g, cols))
                rows.append(row)
        return pd.DataFrame(rows)

    def _measure_rows(self, pa: str, cols: list[Column], years=None) -> pd.DataFrame:
        """The measure rows of one PA and those years: from memory, or read from the results file in batches of only the columns the table uses."""
        if self.frame is not None:
            return filter_pa(self.frame, pa, years)
        if self.path.suffix.lower() != ".parquet":
            return filter_pa(outputs.read_table(self.path), pa, years)
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(self.path)
        want = {"CEInputID", "MeasDescription", "PrgID", *(KEY_SOURCES & {"CreditedPA", "CreditedIOU", "Prg_PA", "IOU_AC_Territory", "poc_PA", *YEAR_COLUMNS,
                                                                        "ClaimYearQuarter"})}
        for c in cols:
            want |= set(c.parts) if c.kind == "derived" else (RATIO_INPUTS if c.kind == "ratio" else {c.key})
        names = [n for n in pf.schema_arrow.names if n in want]
        out = [filter_pa(b.to_pandas(), pa, years) for b in pf.iter_batches(batch_size=BATCH_ROWS, columns=names, use_threads=False)]
        return pd.concat(out, ignore_index=True) if out else pf.schema_arrow.empty_table().select(names).to_pandas()


def _measure_table(f: pd.DataFrame, cols: list[Column]) -> pd.DataFrame:
    ids = pd.DataFrame({"CEInputID": f.get("CEInputID", pd.Series(index=f.index, dtype=object)),
                        "Description": f.get("MeasDescription", pd.Series("", index=f.index)), "PrgID": f.get("PrgID", pd.Series("", index=f.index)),
                        "PA": pa_of(f), "Program year": year_of(f)})
    return pd.concat([ids, _row_values(f, cols)], axis=1).reset_index(drop=True)


def table(frame: pd.DataFrame, level: str, pa: str, keys: list[str], years=None) -> pd.DataFrame:
    """RunResults.from_frame(frame).table(...), without the measure-level size limits (a caller holding the whole frame decides that itself)."""
    if level == "measure":
        cat = catalog(frame)
        return _measure_table(filter_pa(frame, pa, years), [cat[k] for k in keys if k in cat])
    return RunResults.from_frame(frame).table(level, pa, keys, years)


def measure_level_allowed(frame: pd.DataFrame, pa: str, years=None) -> bool:
    return len(frame) <= MAX_RUN_MEASURE_ROWS and len(filter_pa(frame, pa, years)) <= MAX_MEASURE_ROWS


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
            views[name] = current_keys(keys)
    current = saved.get("current") if saved.get("current") in views else DEFAULT_VIEW_NAME
    return views, current


def current_keys(keys) -> list[str]:
    """A saved view's column keys under the names a run writes now: a column renamed in 0.1.50 (NetElecCO2 -> FirstYearNetElecCO2 ...) gets its new name,
    one no longer written is dropped (fieldnames.output_name)."""
    from .fieldnames import output_name
    out = []
    for k in keys:
        n = output_name(str(k))
        if n and n not in out:
            out.append(n)
    return out


def save_views(views: dict[str, list[str]], current: str) -> None:
    paths.save_config(**{VIEWS_KEY: {"views": {k: v for k, v in views.items() if k != DEFAULT_VIEW_NAME}, "current": current}})


# ----------------------------------------------------------------------------------------------------------------------------- two runs side by side
COMPARE_LEVELS = [("portfolio", "Portfolio"), ("year", "Program year"), ("segment", "Program segment"), ("sector", "Program sector"), ("program", "Program")]
SIDES = ("a", "b", "diff", "pct")


def compare(a: RunResults, b: RunResults, level: str, keys: list[str], pa: str = ALL_PAS, years=None) -> pd.DataFrame:
    """Two runs side by side at one level (BRD BR-06.11, any two runs, not only two ACCs of one job): one row per group present in either run, matched
    on the group (the PrgID, program year, sector or segment; the portfolio is one row), and for each key `<key>__a`, `<key>__b`, `<key>__diff` (b - a) and
    `<key>__pct` (diff / |a|, blank when a is 0; ratios are recomputed in each run from its own sums, as everywhere). Measures counts as a key too.
    Keys only one run has are blank for the other."""
    ta, tb = a.table(level, pa, keys, years), b.table(level, pa, keys, years)
    idcol = {"portfolio": "Portfolio", "program": "PrgID", "year": "Program year", "sector": "Program sector", "segment": "Program segment"}[level]
    for t in (ta, tb):
        if level == "portfolio" and len(t):
            t[idcol] = "All PAs" if pa in (None, "", ALL_PAS) else pa
    extra = [c for c in ("Program name", "PA") if c in ta or c in tb]
    values = ["Measures"] + [k for k in keys if k in ta.columns or k in tb.columns]
    ja = ta.set_index(idcol) if len(ta) else pd.DataFrame(columns=values + extra).rename_axis(idcol)
    jb = tb.set_index(idcol) if len(tb) else pd.DataFrame(columns=values + extra).rename_axis(idcol)
    index = list(dict.fromkeys(list(ja.index) + list(jb.index)))
    out = pd.DataFrame(index=pd.Index(index, name=idcol))
    for c in extra:
        out[c] = ja[c].reindex(index).fillna(jb[c].reindex(index)) if c in ja else jb[c].reindex(index)
    for k in values:
        va = pd.to_numeric(ja[k], errors="coerce").reindex(index) if k in ja else pd.Series(float("nan"), index=index)
        vb = pd.to_numeric(jb[k], errors="coerce").reindex(index) if k in jb else pd.Series(float("nan"), index=index)
        out[f"{k}__a"], out[f"{k}__b"] = va.to_numpy(), vb.to_numpy()
        out[f"{k}__diff"] = (vb - va).to_numpy()
        out[f"{k}__pct"] = ((vb - va) / va.abs()).where(va != 0).to_numpy()
    return out.reset_index()
