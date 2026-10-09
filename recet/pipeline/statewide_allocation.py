"""
Statewide (SW) program allocation: split a statewide program's measure inputs,
program costs, or CET outputs across the four IOUs.

INPUTS (both supplied by Jake, 2026-09-18, copied to data/statewide_allocation/):
  statewide_factors_20260918.csv -- one row per (Year, ProgramID): per-IOU
      GasFactor, ElectricFactor and WeightedFactor (Weighted = PercentElectric
      x Electric + (1 - PercentElectric) x Gas; each column set sums to 1
      across PGE/SDGE/SCE/SCG, except a fuel's factors are all 0 where that
      fuel doesn't apply, e.g. gas factors for an all-electric program).
  CEDARS_statewide_allocation_2026-09-17.xlsx -- which rule applies to each
      field of the Measure input, ProgramCost input and CET output tables,
      stored as data/statewide_allocation/field_rules.csv:
        Pass Through     copy the value unchanged to every IOU's row
        Electric Factor  multiply by that IOU's ElectricFactor
        Gas Factor       multiply by that IOU's GasFactor
        Weighted Factor  multiply by that IOU's WeightedFactor

WHAT HAPPENS TO A ROW: a row whose PrgID has a factor row for its year is
replaced by one row per IOU that has any nonzero factor for that program, with
`<IOU>_` prepended to CEInputID (outputs: CET_ID) AND to PrgID, e.g. `PGE_XXX`
/ program `PGE_SW_WP`. The workbook lists PrgID as Pass Through; Jake
overrode that (2026-09-18) so each IOU's program is its own program: allocated
ProgramCost rows stay unique on (PrgID, PrgYear) as CET's upload spec requires,
and each IOU's program costs are shared only among that IOU's own measures
when priced. A row whose PrgID is not in the table for its year is left
untouched (not a statewide program) and reported if the PrgID starts with
`SW_`. The year is the claim year (first four characters of ClaimYearQuarter);
outputs carry no year, so callers pass `year_by_id` (CET_ID -> year).

CONSEQUENCES OF THE RULES AS WRITTEN (worth knowing, not changed here):
  - On MEASURES, `PA` (IOU_AC_Territory, the avoided-cost territory) is Pass
    Through, so an IOU's allocated share is still priced on the measure's own
    territory. On PROGRAM COST rows, `PA` is instead set to the IOU (Jake,
    2026-09-18, matching CEDARS: the row belongs to the administering IOU and
    that IOU's discount rate applies), overriding the workbook's Pass Through.
  - Ratios and WeightedSavings are Pass Through. Benefits/costs in OUTPUTS use
    the Weighted factor, whereas allocating INPUTS scales kWh by the Electric
    factor and therm by the Gas factor, so pricing allocated inputs and
    allocating priced outputs agree only for fields that scale the same way.
  - Field name `PA` in the workbook is the Measure column `IOU_AC_Territory`
    (renamed 2024-08-06).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .. import paths
DATA_DIR = paths.package_data("statewide_allocation")
FACTORS_CSV = DATA_DIR / "statewide_factors_20260918.csv"
RULES_CSV = DATA_DIR / "field_rules.csv"

IOUS = ["PGE", "SDGE", "SCE", "SCG"]
_FIELD_ALIASES = {"measure": {"PA": "IOU_AC_Territory"}}
_RULE_TO_COLUMN = {"Electric Factor": "ElectricFactor", "Gas Factor": "GasFactor", "Weighted Factor": "WeightedFactor"}


# --- user-supplied factors (recet.overrides.load_sw_factors): a (Year, ProgramID) row replaces the built-in row, a new one is added ---
_USER = None


def set_user_factors(table: pd.DataFrame | None) -> None:
    global _USER
    _USER = table


def user_factors() -> pd.DataFrame | None:
    return _USER


def load_factors(path: Path | None = None) -> pd.DataFrame:
    """The factor table indexed by (Year, ProgramID): the built-in file (or `path`), with any user-supplied rows replacing / extending it."""
    df = pd.read_csv(path or FACTORS_CSV)
    df["ProgramID"] = df["ProgramID"].astype(str).str.strip()
    if _USER is not None and path is None:
        user = _USER.copy()
        keep = ~pd.MultiIndex.from_frame(df[["Year", "ProgramID"]]).isin(pd.MultiIndex.from_frame(user[["Year", "ProgramID"]]))
        df = pd.concat([df[keep], user[[c for c in user.columns if c in df.columns or c.endswith("Factor")]]], ignore_index=True)
    return df.set_index(["Year", "ProgramID"])


def load_field_rules(path: Path | None = None) -> dict[str, dict[str, str]]:
    """{table: {field: rule}} for table in measure / program_cost / output."""
    r = pd.read_csv(path or RULES_CSV)
    out: dict[str, dict[str, str]] = {}
    for table, sub in r.groupby("table"):
        out[table] = {_FIELD_ALIASES.get(table, {}).get(f, f): rule for f, rule in zip(sub["field"], sub["rule"])}
    return out


def _claim_year(df: pd.DataFrame, table: str) -> pd.Series:
    if "ClaimYearQuarter" in df.columns:
        return df["ClaimYearQuarter"].astype(str).str[:4].astype(int)
    if table == "program_cost" and "PrgYear" in df.columns:
        return df["PrgYear"].astype(int)
    raise KeyError(f"Cannot determine the year of {table} rows: no ClaimYearQuarter column")


def _allocate(df: pd.DataFrame, table: str, id_cols: list[str], years: pd.Series,
              factors: pd.DataFrame | None, rules: dict[str, str] | None, set_iou_cols: list[str] = ()):
    factors = load_factors() if factors is None else factors
    rules = load_field_rules()[table] if rules is None else rules
    prg = df["PrgID"].astype(str).str.strip()
    years = pd.Series(years, index=df.index).astype(int)
    key = pd.MultiIndex.from_arrays([years, prg])
    is_sw = pd.Series(key.isin(factors.index), index=df.index)

    scaled_fields = {f: _RULE_TO_COLUMN[r] for f, r in rules.items() if r in _RULE_TO_COLUMN and f in df.columns}
    unruled = [c for c in df.columns if c not in rules]
    unmatched_sw = sorted({f"{p} ({y})" for p, y in zip(prg[~is_sw], years[~is_sw]) if p.startswith("SW_")})

    sw = df[is_sw]
    fac = factors.reindex(pd.MultiIndex.from_arrays([years[is_sw], prg[is_sw]]))
    fields = list(scaled_fields)
    parts, order = [df[~is_sw]], [np.column_stack([df.index[~is_sw].to_numpy(), np.full((~is_sw).sum(), -1)])]
    for i, iou in enumerate(IOUS):
        f = {k: fac[f"{iou}{k}"].to_numpy() for k in ("ElectricFactor", "GasFactor", "WeightedFactor")}
        keep = (f["ElectricFactor"] != 0) | (f["GasFactor"] != 0) | (f["WeightedFactor"] != 0)
        part = sw[keep].copy()
        if fields:
            mult = np.column_stack([f[scaled_fields[c]][keep] for c in fields])
            part[fields] = part[fields].to_numpy(dtype=float) * mult
        for c in set_iou_cols:
            if c in part.columns:
                part[c] = iou
        for c in id_cols:
            if c in part.columns:
                part[c] = f"{iou}_" + part[c].astype(str)
        parts.append(part)
        order.append(np.column_stack([part.index.to_numpy(), np.full(len(part), i)]))
    ordering = np.concatenate(order)
    result = pd.concat(parts).iloc[np.lexsort((ordering[:, 1], ordering[:, 0]))].reset_index(drop=True)
    report = {"input_rows": len(df), "statewide_rows": int(is_sw.sum()),
              "allocated_rows": len(result) - int((~is_sw).sum()), "output_rows": len(result),
              "unmatched_sw_programs": unmatched_sw, "fields_without_a_rule_passed_through": unruled}
    return result, report


def warn_unmatched(report: dict, what: str) -> None:
    """A `SW_` PrgID with no factor row for its year is left UNALLOCATED (it is not treated as an error by _allocate). In a job where
    other statewide programs are allocated, that silently mixes allocated and unallocated programs, so say so loudly."""
    if report.get("unmatched_sw_programs"):
        import warnings
        warnings.warn(f"{what}: statewide programs with no allocation factors for their year were NOT allocated: "
                      f"{report['unmatched_sw_programs']}", stacklevel=3)


def allocate_measure_inputs(df_raw: pd.DataFrame, factors: pd.DataFrame | None = None,
                            rules: dict[str, str] | None = None):
    """Raw Measure.csv rows (original column names) -> (allocated rows, report)."""
    return _allocate(df_raw, "measure", ["CEInputID", "PrgID"], _claim_year(df_raw, "measure"), factors, rules)


def allocate_program_costs(df_raw: pd.DataFrame, factors: pd.DataFrame | None = None,
                           rules: dict[str, str] | None = None):
    """Raw ProgramCost.csv rows -> (allocated rows, report). The IOU prefix goes
    on PrgID (there is no other ID column), keeping (PrgID, PrgYear) unique, and
    PA is set to the IOU (a program cost row belongs to the administering IOU,
    which also selects its discount rate)."""
    return _allocate(df_raw, "program_cost", ["PrgID"], _claim_year(df_raw, "program_cost"), factors, rules,
                     set_iou_cols=["PA"])


def allocate_outputs(outputs: pd.DataFrame, year_by_id: dict[str, int] | pd.Series,
                     factors: pd.DataFrame | None = None, rules: dict[str, str] | None = None):
    """CET output rows (CET_ID, PrgID, ...) -> (allocated rows, report).
    `year_by_id` maps each CET_ID to its claim year (outputs don't carry one)."""
    years = outputs["CET_ID"].map(year_by_id)
    if years.isna().any():
        raise KeyError(f"{int(years.isna().sum())} CET_IDs have no year in year_by_id")
    return _allocate(outputs, "output", ["CET_ID", "PrgID"], years, factors, rules)
