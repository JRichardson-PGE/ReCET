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


def electric_factors(prg_ids: pd.Series, years: pd.Series) -> pd.Series:
    """For allocated measure rows (PrgID `<IOU>_<statewide program>`), the electric factor allocation applied to their kWh fields; 1 for every other row."""
    factors = load_factors()
    out = []
    for prg, year in zip(prg_ids.astype(str), years.astype(int)):
        iou, _, orig = prg.partition("_")
        col = f"{iou}ElectricFactor"
        out.append(float(factors.at[(year, orig), col]) if iou in IOUS and (year, orig) in factors.index and col in factors.columns else 1.0)
    return pd.Series(out, index=prg_ids.index)


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
              "unmatched_sw_programs": unmatched_sw, "fields_without_a_rule_passed_through": unruled,
              "reconciliation": _totals(sw, years[is_sw], prg[is_sw], parts[1:], fields, table)}
    return result, report


def _quantity(df: pd.DataFrame, field: str, table: str) -> np.ndarray:
    """What a field adds up to: on measures, NumUnits x the per-unit field (NumUnits is passed through); on program costs, the field itself."""
    v = pd.to_numeric(df[field], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    if table == "measure" and "NumUnits" in df.columns:
        v = v * pd.to_numeric(df["NumUnits"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    return v


def _totals(sw: pd.DataFrame, years: pd.Series, prg: pd.Series, iou_parts: list[pd.DataFrame], fields: list[str], table: str) -> pd.DataFrame:
    """Per (Year, statewide PrgID, field): the statewide rows' total before allocation and each IOU's allocated rows' total, from the rows _allocate
    actually produced (each IOU part keeps the statewide row's index, so it maps back to the row's year and program). Added up over files by
    `reconciliation`."""
    cols = ["Year", "PrgID", "field", "unallocated_total", *IOUS]
    if not len(sw) or not fields:
        return pd.DataFrame(columns=cols)
    key = pd.DataFrame({"Year": years.to_numpy(), "PrgID": prg.to_numpy()}, index=sw.index)
    out = []
    for f in fields:
        t = key.assign(field=f, unallocated_total=_quantity(sw, f, table))
        for iou, part in zip(IOUS, iou_parts):
            t[iou] = pd.Series(_quantity(part, f, table), index=part.index).reindex(sw.index).fillna(0.0).to_numpy()
        out.append(t.groupby(["Year", "PrgID", "field"], as_index=False, sort=False)[["unallocated_total", *IOUS]].sum())
    return pd.concat(out, ignore_index=True)[cols]


RECONCILE_TOLERANCE = 1e-9          # relative: an allocated total within this of the unallocated one reconciles


def reconciliation(measure_files: list, programcost_file=None, factors: pd.DataFrame | None = None) -> pd.DataFrame:
    """The allocation's reconciliation (BRD BR-06.7): for each statewide program, year and allocated field, the total before allocation, each IOU's
    share, their sum and the difference. `measure_files`: the job's Measure.csv, or the parts of a run in parts (their totals are added up).

    The check is on the inputs, which is where allocation happens: every quantity and cost is then priced as filed, and pricing is linear in them, so
    the IOU shares' benefits add up to the program's whenever its inputs do (tests/test_statewide_reconciliation.py prices both). A field does not
    reconcile when its factors for that program and year do not sum to 1, most often because every IOU's factor for that fuel is 0 (for example
    therms on a program whose gas factors are all 0): that quantity is then dropped from every share."""
    from ..templates import with_defaults
    from .delimited import read_cet_csv
    factors = load_factors() if factors is None else factors
    pieces = []
    for f in measure_files:
        _, rep = allocate_measure_inputs(with_defaults(read_cet_csv(Path(f), low_memory=False)), factors=factors)
        pieces.append(rep["reconciliation"].assign(table="Measure"))
    if programcost_file:
        _, rep = allocate_program_costs(read_cet_csv(Path(programcost_file), low_memory=False), factors=factors)
        pieces.append(rep["reconciliation"].assign(table="ProgramCost"))
    cols = ["table", "Year", "PrgID", "field", "factor", "factor_sum", "unallocated_total", *IOUS, "allocated_total", "difference", "reconciles", "note"]
    pieces = [p for p in pieces if len(p)]
    if not pieces:
        return pd.DataFrame(columns=cols)
    t = pd.concat(pieces, ignore_index=True).groupby(["table", "Year", "PrgID", "field"], as_index=False, sort=False)[["unallocated_total", *IOUS]].sum()
    rules = load_field_rules()
    t["factor"] = [rules["measure" if tb == "Measure" else "program_cost"].get(f, "") for tb, f in zip(t["table"], t["field"])]
    t["factor_sum"] = [float(sum(factors.at[(y, p), f"{iou}{_RULE_TO_COLUMN[r]}"] for iou in IOUS)) if r in _RULE_TO_COLUMN else np.nan
                       for y, p, r in zip(t["Year"], t["PrgID"], t["factor"])]
    t["allocated_total"] = t[IOUS].sum(axis=1)
    t["difference"] = t["allocated_total"] - t["unallocated_total"]
    t["reconciles"] = t["difference"].abs() <= RECONCILE_TOLERANCE * t["unallocated_total"].abs().clip(lower=1.0)

    def note(r) -> str:
        if r["reconciles"]:
            return ""
        if abs(r["factor_sum"]) < 1e-12:
            return f"every IOU's {r['factor'].lower()} is 0 for this program and year, so this quantity is in no IOU's share"
        return f"the IOUs' {r['factor'].lower()}s sum to {r['factor_sum']:.4f}, not 1"
    t["note"] = t.apply(note, axis=1) if len(t) else ""
    return t.sort_values(["table", "Year", "PrgID"], kind="stable").reset_index(drop=True)[cols]


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
