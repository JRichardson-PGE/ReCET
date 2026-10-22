"""Preflight check of a CET-format input job, before any pricing.

Reads Measure.csv (and ProgramCost.csv) exactly as the run would and reports, without pricing anything:

* format problems that would stop the run (no recognisable delimiter, missing columns, duplicate CEInputID, bad ClaimYearQuarter, unknown IOU, non-numeric values),
* what the run will and will not be able to price: for every measure with electric savings whether an 8,760 profile (user-supplied or CEDARS library) or a TOU
  H-factor profile exists, for every measure with gas savings whether its monthly profile exists,
* the ACC horizon: claim years before the first ACC year or measure lives that run past the last one,
* PrgID alignment between Measure.csv and ProgramCost.csv, and statewide-program rows.

Severity: "error" = the run cannot proceed as is; "warning" = the run proceeds but some measures are unpriced or priced on a fallback; "info" = worth knowing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .pipeline import delimited

REQUIRED_MEASURE_COLUMNS = [
    "CEInputID", "PrgID", "MeasDescription", "ClaimYearQuarter", "Sector", "E3ClimateZone", "E3GasSavProfile", "E3GasSector", "E3MeaElecEndUseShape",
    "E3TargetSector", "IOU_AC_Territory", "EUL_Yrs", "RUL_Yrs", "NumUnits",
    "UnitkW1stBaseline", "UnitkWh1stBaseline", "UnitTherm1stBaseline", "UnitkW2ndBaseline", "UnitkWh2ndBaseline", "UnitTherm2ndBaseline",
    "NTGRkW", "NTGRkWh", "NTGRTherm", "NTGRCost", "RealizationRatekW", "RealizationRatekWh", "RealizationRateTherm",
    "InstallationRatekW", "InstallationRatekWh", "InstallationRateTherm",
    "UnitMeaCost1stBaseline", "UnitMeaCost2ndBaseline", "UnitIncentiveToOthers", "UnitEndUserRebate", "UnitDirectInstallLab", "UnitDirectInstallMat",
    "MeasInflation", "UnitGasInfraBens", "UnitRefrigCosts", "UnitRefrigBens", "UnitMiscCosts", "UnitMiscBens", "UnitTaxCredits",
]
OPTIONAL_MEASURE_COLUMNS = {
    "FuelID": "fuel-substitution measures are recognised by FuelID or MeasImpactType; without both, none are",
    "MeasImpactType": "fuel-substitution measures are recognised by FuelID or MeasImpactType; without both, none are",
    "CombustionType": "gas NOx emissions need it (they are 0 without)",
    "UnitkWhIOUWater1stBaseline": "water energy benefits/costs are 0 without it and UnitkWhIOUWater2ndBaseline",
    "UnitkWhIOUWater2ndBaseline": "water energy benefits/costs are 0 without it and UnitkWhIOUWater1stBaseline",
}
NUMERIC_COLUMNS = [c for c in REQUIRED_MEASURE_COLUMNS if c.startswith(("Unit", "NTGR", "RealizationRate", "InstallationRate"))
                   or c in ("EUL_Yrs", "RUL_Yrs", "NumUnits", "MeasInflation")]
VALID_PA = {"PGE", "SCE", "SDGE", "SCG"}
# CET's own QC ranges (cet_spec.sql, `CedarsNumRange`): a value outside them would make CET reject the whole upload. Here they are warnings -- the tool still prices the row.
# (column, low, high, low is exclusive, high is exclusive)
CET_RANGES = [("NTGRkW", 0, 1.5, True, False), ("NTGRkWh", 0, 1.5, True, False), ("NTGRTherm", 0, 1.5, True, False), ("NTGRCost", 0, 1.5, True, False),
              ("EUL_Yrs", 0, 40, False, False), ("RUL_Yrs", 0, 30, False, False),
              ("RealizationRatekW", 0, 2, True, False), ("RealizationRatekWh", 0, 2, True, False), ("RealizationRateTherm", 0, 2, True, False),
              ("InstallationRatekW", 0, 1, True, False), ("InstallationRatekWh", 0, 1, True, False), ("InstallationRateTherm", 0, 1, True, False),
              ("MeasInflation", 0, 1, False, False), ("UnitMeaCost1stBaseline", 0, float("inf"), False, False), ("UnitMeaCost2ndBaseline", 0, float("inf"), False, False)]
# Values the arithmetic cannot do without: a blank turns the measure's results blank (CET marks each of these NotEmpty)
NEEDED_VALUES = ("NumUnits", "EUL_Yrs", "NTGRkWh", "UnitkWh1stBaseline")
VALID_GAS_SECTORS = {"RESIDENTIAL", "COMMERCIAL", "CORE"}
MAX_CELLS = 5000          # cells listed per issue (the web editor highlights them)


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    count: int = 0
    examples: list = field(default_factory=list)
    cells: list = field(default_factory=list)     # (table, row, column): where it is, row = 0-based data row of Measure.csv / ProgramCost.csv


@dataclass
class PreflightReport:
    issues: list[Issue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def add(self, severity: str, code: str, message: str, count: int = 0, examples=None, cells=None) -> None:
        self.issues.append(Issue(severity, code, message, count, list(examples or [])[:5], list(cells or [])[:MAX_CELLS]))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self) -> str:
        lines = []
        for sev in ("error", "warning", "info"):
            for i in (x for x in self.issues if x.severity == sev):
                n = f" [{i.count:,}]" if i.count else ""
                ex = f"  e.g. {i.examples}" if i.examples else ""
                lines.append(f"{sev.upper():8s}{i.code}{n}: {i.message}{ex}")
        head = ["PREFLIGHT: " + ("no blocking problems" if self.ok else f"{len(self.errors)} blocking problem(s)")]
        head += [f"  {k}: {v}" for k, v in self.stats.items()]
        return "\n".join(head + [""] + (lines or ["(nothing to report)"]))

    def to_dict(self) -> dict:
        """The report as plain JSON values (the web app's Validate inputs panel reads it from preflight.json)."""
        return {"ok": self.ok, "errors": len(self.errors), "stats": {str(k): str(v) for k, v in self.stats.items()},
                "issues": [{"severity": i.severity, "code": i.code, "message": i.message, "count": int(i.count or 0), "examples": [str(e) for e in i.examples],
                            "cells": [[str(tb), int(r), str(c)] for tb, r, c in i.cells]}
                           for i in self.issues]}

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{"severity": i.severity, "code": i.code, "count": i.count, "message": i.message, "examples": "; ".join(map(str, i.examples))}
                             for i in self.issues])


def _nonblank_numeric_problems(df: pd.DataFrame, col: str) -> pd.Series:
    raw = df[col]
    parsed = pd.to_numeric(raw, errors="coerce")
    return raw.notna() & parsed.isna() & (raw.astype(str).str.strip() != "")


def _cells(table: str, rows, col: str) -> list[tuple[str, int, str]]:
    """(table, row, column) for the rows where a mask is true (or for a list of row numbers)."""
    rows = np.flatnonzero(np.asarray(rows)) if np.asarray(rows).dtype == bool else rows
    return [(table, int(r), col) for r in sorted(set(int(x) for x in rows))]


def _check_programs(rep: PreflightReport, measures: pd.DataFrame, programcost_csv, allocate_statewide: bool, apply_exclude_from_ce: bool,
                    mrows=lambda mask: []) -> None:
    """The measures' programs against the program metadata (programs.py): unknown PrgIDs, claims outside the program's years, closed programs, programs
    flagged Exclude_From_CE, and a ProgramCost PA that differs from the program list's PA. None of these stops the run. `mrows(mask)` turns a mask over
    `measures` into Measure.csv row numbers."""
    from . import outputs, programs
    meta = programs.lookup(measures["prg_id"])
    pid = measures["prg_id"].astype(str).str.strip().to_numpy()
    year = measures["claim_year"].to_numpy()
    missing = meta["Prg_MatchedOn"].to_numpy() == "(not found)"
    rep.stats["programs"] = f"{len(set(pid)):,} ({len(set(pid[missing])):,} not in the program metadata)"
    if missing.any():
        rep.add("warning", "program_not_in_metadata", "PrgID is not in the program metadata (the built-in CEDARS Programs list or the supplied one): its Prg_ columns are "
                "blank and its measures count as segment '(blank)'", int(missing.sum()), sorted(set(pid[missing])), _cells("Measure", mrows(missing), "PrgID"))
    start = pd.to_numeric(meta.get("Prg_StartYear"), errors="coerce").to_numpy() if "Prg_StartYear" in meta else None
    end = pd.to_numeric(meta.get("Prg_EndYear"), errors="coerce").to_numpy() if "Prg_EndYear" in meta else None
    if start is not None and end is not None:
        out = (~np.isnan(start) & (year < start)) | (~np.isnan(end) & (year > end))
        if out.any():
            rep.add("warning", "claim_outside_program_years", "claim year is before the program's StartYear or after its EndYear in the program metadata",
                    int(out.sum()), sorted(set(pid[out])), _cells("Measure", mrows(out), "ClaimYearQuarter"))
    if "Prg_ProgramStatus" in meta:
        closed = meta["Prg_ProgramStatus"].str.lower().isin({"closed", "closure_pending"}).to_numpy()
        if closed.any():
            rep.add("info", "program_closed", "the program metadata lists the program as closed or closure_pending", int(closed.sum()), sorted(set(pid[closed])),
                    _cells("Measure", mrows(closed), "PrgID"))
    if outputs.EXCLUDE_FLAG in meta:
        flag = programs.is_true(meta[outputs.EXCLUDE_FLAG]).to_numpy()
        if flag.any():
            what = ("left out of the portfolio summary (shown in its 'excluded' row)" if apply_exclude_from_ce
                    else "still in the portfolio summary; tick 'Leave Exclude_From_CE programs out of the portfolio summary' to leave them out")
            rep.add("info", "program_excluded_from_ce", f"the program metadata flags the program Exclude_From_CE: its measures are priced and {what}",
                    int(flag.sum()), sorted(set(pid[flag])), _cells("Measure", mrows(flag), "PrgID"))
    if programcost_csv:
        credited = outputs.credited_iou(measures, programcost_csv, allocate_statewide).to_numpy()
        listed = meta["Prg_PA"].to_numpy()
        differ = (credited != "") & ~missing & (credited != listed)
        if differ.any():
            rep.add("info", "program_pa_differs", "the program's PA in ProgramCost.csv (CreditedIOU) differs from its PA in the program metadata", int(differ.sum()),
                    sorted({f"{p}: {c} vs {l}" for p, c, l in zip(pid[differ], credited[differ], listed[differ])}), _cells("Measure", mrows(differ), "PrgID"))


def check_inputs(measure_csv: str | Path, programcost_csv: str | Path | None = None, *, acc=None, allocate_statewide: bool = False,
                 base_year: int | None = None, apply_exclude_from_ce: bool = False) -> PreflightReport:
    """`acc` is a versions.ResolvedAcc (for the horizon and E3-version checks); the runtime must already be configured for it (runtime.apply) so that profile
    and TOU coverage reflect this run's user-supplied profiles."""
    rep = PreflightReport()
    p = Path(measure_csv)
    if not p.exists():
        rep.add("error", "file_missing", f"Measure file not found: {p}")
        return rep
    sep = delimited.detect_sep(p)
    if sep is None:
        rep.add("error", "no_delimiter", "The header has no '|', ',' or tab -- Measure.csv must be pipe-delimited (CET's format) or comma-delimited")
        return rep
    if sep != "|":
        rep.add("info", "not_pipe_delimited", f"Measure.csv is {delimited.describe(sep)}; it is read as such (the CET itself needs pipe-delimited files)")
    df = delimited.read_cet_csv(p, low_memory=False, dtype=str, keep_default_na=False, na_values=[""])
    rep.stats["measures"] = f"{len(df):,}"
    missing = [c for c in REQUIRED_MEASURE_COLUMNS if c not in df.columns]
    if missing:
        rep.add("error", "missing_columns", f"Measure.csv lacks required column(s): {missing}")
        return rep
    for col, why in OPTIONAL_MEASURE_COLUMNS.items():
        if col not in df.columns:
            rep.add("warning", "missing_optional_column", f"no {col} column: {why}")

    dupmask = df["CEInputID"].duplicated(keep=False) & df["CEInputID"].notna()
    dup = df["CEInputID"][df["CEInputID"].duplicated()]
    if df["CEInputID"].isna().any():
        rep.add("error", "blank_id", "blank CEInputID", int(df["CEInputID"].isna().sum()), cells=_cells("Measure", df["CEInputID"].isna(), "CEInputID"))
    if len(dup):
        rep.add("error", "duplicate_id", "CEInputID must be unique (results are keyed on it)", int(dup.nunique()), dup.unique(), _cells("Measure", dupmask, "CEInputID"))
    bad_cyq = ~df["ClaimYearQuarter"].astype(str).str.match(r"^\d{4}Q[1-4]$")
    if bad_cyq.any():
        rep.add("error", "bad_claim_year_quarter", "ClaimYearQuarter must look like 2028Q1", int(bad_cyq.sum()), df["ClaimYearQuarter"][bad_cyq].unique(),
                _cells("Measure", bad_cyq, "ClaimYearQuarter"))
    bad_pa = ~df["IOU_AC_Territory"].isin(VALID_PA)
    if bad_pa.any():
        rep.add("error", "bad_iou", f"IOU_AC_Territory must be one of {sorted(VALID_PA)} (the run prices each measure in that territory)",
                int(bad_pa.sum()), df["IOU_AC_Territory"][bad_pa].unique(), _cells("Measure", bad_pa, "IOU_AC_Territory"))
    for col in NUMERIC_COLUMNS + [c for c in ("UnitkWhIOUWater1stBaseline", "UnitkWhIOUWater2ndBaseline") if c in df.columns]:
        bad = _nonblank_numeric_problems(df, col)
        if bad.any():
            rep.add("error", "non_numeric", f"{col} has non-numeric values", int(bad.sum()), df[col][bad].unique(), _cells("Measure", bad, col))
    for col in NEEDED_VALUES:
        blank = df[col].isna() | (df[col].astype(str).str.strip() == "")
        if blank.any():
            rep.add("error", "blank_required_value", f"{col} is blank (CET requires a value; the measure's results would come out blank)", int(blank.sum()),
                    df["CEInputID"][blank].tolist(), _cells("Measure", blank, col))
    rul = pd.to_numeric(df["RUL_Yrs"], errors="coerce").fillna(0.0)
    blank2 = (rul > 0) & (df["UnitkWh2ndBaseline"].isna() | (df["UnitkWh2ndBaseline"].astype(str).str.strip() == ""))
    if blank2.any():
        rep.add("error", "blank_second_baseline", "UnitkWh2ndBaseline is blank on early-replacement measures (RUL_Yrs > 0), which are priced on it after the RUL", int(blank2.sum()),
                df["CEInputID"][blank2].tolist(), _cells("Measure", blank2, "UnitkWh2ndBaseline"))
    if rep.errors:
        return rep
    for col, lo, hi, lo_open, hi_open in CET_RANGES:
        v = pd.to_numeric(df[col], errors="coerce")
        bad = v.notna() & ((v <= lo) if lo_open else (v < lo)) | v.notna() & ((v >= hi) if hi_open else (v > hi))
        if bad.any():
            rng = f"{'(' if lo_open else '['}{lo}, {hi if hi != float('inf') else 'no upper limit'}{')' if hi_open else ']'}"
            rep.add("warning", "outside_cet_range", f"{col} is outside CET's allowed range {rng}: CET would reject this upload", int(bad.sum()), df[col][bad].unique(),
                    _cells("Measure", bad, col))
    eul = pd.to_numeric(df["EUL_Yrs"], errors="coerce")
    if ((rul > 0) & (rul >= eul)).any():
        rep.add("warning", "rul_not_below_eul", "RUL_Yrs is not below EUL_Yrs; such a measure is treated as single-baseline for its whole life", int(((rul > 0) & (rul >= eul)).sum()),
                cells=_cells("Measure", (rul > 0) & (rul >= eul), "RUL_Yrs"))

    # measure_id (CEInputID, with "<IOU>_" in front after statewide allocation) -> Measure.csv row, for the checks on the loaded measures below
    raw_pos = {str(v).strip(): i for i, v in enumerate(df["CEInputID"])}

    def _raw_row(mid) -> int | None:
        mid = str(mid).strip()
        if mid in raw_pos:
            return raw_pos[mid]
        head, _, rest = mid.partition("_")
        return raw_pos.get(rest) if rest and head in VALID_PA else None

    def mrows(mask) -> list[int]:
        ids = measures["measure_id"].to_numpy()[np.asarray(mask, dtype=bool)] if not isinstance(mask, pd.DataFrame) else mask["measure_id"].to_numpy()
        return [r for r in (_raw_row(m) for m in ids) if r is not None]

    from . import run_poc
    from .pipeline import cost_tests, gas_savings_profiles, real_impact_profiles, system_zones
    try:
        measures, program = run_poc.load_job(p, programcost_csv, allocate_statewide=allocate_statewide)
    except ValueError as exc:
        where: list = []
        try:                                        # point at the PrgIDs that have no partner in the other file
            pc = delimited.read_cet_csv(programcost_csv, dtype=str, keep_default_na=False) if programcost_csv is not None else None
            if pc is not None and "PrgID" in pc.columns:
                mp, cp = df["PrgID"].astype(str).str.strip(), pc["PrgID"].astype(str).str.strip()
                where = _cells("Measure", ~mp.isin(set(cp)), "PrgID") + _cells("ProgramCost", ~cp.isin(set(mp)), "PrgID")
        except Exception:
            pass
        rep.add("error", "program_alignment", str(exc), cells=where)
        return rep
    rep.stats["claim years"] = f"{measures['claim_year'].min()}-{measures['claim_year'].max()}"
    named = measures.loc[measures["acc_version"] != "", "acc_version"]
    if len(named):                                  # the optional ACCVersion column (BRD BR-06.5): each measure may name its own ACC version
        from . import paths, versions
        run_key = acc.key if acc is not None else None
        rep.stats["measures by ACC version"] = ", ".join(f"{k}: {n:,}" for k, n in named.value_counts().sort_index().items())
        for k in sorted(named.unique()):
            rows = mrows(measures["acc_version"] == k)
            if k != run_key and k not in versions.BUILTIN:
                rep.add("error", "bad_acc_version", f"ACCVersion {k!r} is not a built-in ACC version ({', '.join(versions.BUILTIN)}) or this run's version",
                        len(rows), [k], _cells("Measure", rows, "ACCVersion"))
            elif k != run_key and paths.ref_path(versions.BUILTIN[k].electric_key) is None:
                rep.add("error", "acc_version_file_missing", f"the {versions.BUILTIN[k].label} electric file is not in the reference data folder, so the measures "
                        f"that name it cannot be priced", len(rows), [k], _cells("Measure", rows, "ACCVersion"))
    rep.stats["First Year (base year) that will be used"] = base_year or int(measures["claim_year"].min())
    if programcost_csv is None:
        rep.add("info", "no_program_costs", "no ProgramCost.csv: NoAdmin and regular cost tests will be equal and WeightedProgramCost is 0")
    else:
        pc = delimited.read_cet_csv(programcost_csv)
        swmask = (pc["PA"].astype(str).str.upper() == "SW") if "PA" in pc.columns else pd.Series(False, index=pc.index)
        sw_rows = int(swmask.sum())
        if sw_rows and not allocate_statewide:
            rep.add("info", "statewide_program_costs", "ProgramCost rows with PA = SW are given PA = SCE (what CET does with them); tick 'allocate statewide programs' to split them across the IOUs", sw_rows,
                    cells=_cells("ProgramCost", swmask, "PA"))
        gap = cost_tests.check_program_alignment(measures, program)
        if gap["measure_programs_without_costs"] or gap["program_costs_without_measures"]:
            def bare(ids):                                       # with or without the "<IOU>_" that statewide allocation adds
                return set(ids) | {i.partition("_")[2] for i in ids if i.partition("_")[0] in VALID_PA}
            no_meas = bare(gap["program_costs_without_measures"])
            no_cost = measures["prg_id"].astype(str).str.strip().isin(gap["measure_programs_without_costs"])
            where = [("Measure", r, "PrgID") for r in sorted(set(mrows(no_cost)))]
            if "PrgID" in pc.columns:
                where += _cells("ProgramCost", pc["PrgID"].astype(str).str.strip().isin(no_meas), "PrgID")
            rep.add("error", "program_alignment", f"PrgIDs do not line up: {gap}", cells=where)

    _check_programs(rep, measures, programcost_csv, allocate_statewide, apply_exclude_from_ce, mrows)

    # --- electric profile coverage
    elec = measures[(measures["shape_scale"] != 0) | (measures["shape_scale_2nd"] != 0) | (measures["annual_net_kw_1st"] != 0)]
    cat = real_impact_profiles._catalog_hourly() if real_impact_profiles.is_available() else None
    user = real_impact_profiles.user_profiles()
    keys = elec.groupby(["shape_key", "PA", "target_sector"]).size().rename("n").reset_index()
    counts = {"user profile": 0, "library": 0, "library (sector-loose match)": 0, "NO PROFILE": 0}
    missing_examples: list = []
    missing_keys: list = []
    zone_gaps = 0
    for r in keys.itertuples():
        n = int(r.n)
        if user is not None and user.match(r.shape_key, r.PA, r.target_sector) is not None:
            counts["user profile"] += n
            continue
        pid, exact = real_impact_profiles.match_profile_id(r.shape_key, r.PA, r.target_sector) if cat is not None else (None, False)
        if pid is not None:
            counts["library" if exact else "library (sector-loose match)"] += n
            continue
        counts["NO PROFILE"] += n
        missing_examples.append(f"{r.shape_key} | {r.PA} | {r.target_sector}")
        missing_keys.append((r.shape_key, r.PA, r.target_sector))
    rep.stats["electric-savings measures"] = f"{len(elec):,}"
    for k, v in counts.items():
        if v:
            rep.stats[f"  electric shape: {k}"] = f"{v:,}"
    if counts["NO PROFILE"]:
        rep.add("warning", "no_electric_profile", "electric measures with no 8,760 profile are NOT priced (ElecPricingStatus = UNPRICED); "
                "supply a profile for them or check the E3MeaElecEndUseShape spelling", counts["NO PROFILE"], missing_examples,
                _cells("Measure", mrows(elec[pd.MultiIndex.from_frame(elec[["shape_key", "PA", "target_sector"]]).isin(missing_keys)]), "E3MeaElecEndUseShape"))
    if counts["library (sector-loose match)"]:
        rep.add("info", "loose_profile_match", "matched the library profile on (end use, IOU) only, because E3TargetSector has no exact match", counts["library (sector-loose match)"])
    if cat is None and not counts["user profile"]:
        rep.add("warning", "no_profile_library", "CEDARS impact-profile library not found (reference data folder): measures cannot be matched to 8,760 profiles")

    # --- kWh-per-unit profiles (BRD BR-06.6): the measure's electric kWh comes from the profile, not from UnitkWh1stBaseline / UnitkWh2ndBaseline
    if user is not None and any(e.scaled for e in user.entries):
        zones = [system_zones.priced_zone(k, pa, ts, cz) for k, pa, ts, cz in zip(measures["shape_key"], measures["PA"], measures["target_sector"], measures["climate_zone"])]
        scaled = pd.Series([real_impact_profiles.scaled_profile(k, pa, ts, z) is not None for k, pa, ts, z in
                            zip(measures["shape_key"], measures["PA"], measures["target_sector"], zones)], index=measures.index)
        if scaled.any():
            rep.add("info", "kwh_per_unit_profile", "priced on a kWh-per-unit 8,760 profile: the measure's annual kWh is the profile's (its UnitkWh1stBaseline is "
                    "not used; a 2nd baseline keeps the file's UnitkWh2ndBaseline / UnitkWh1stBaseline ratio)", int(scaled.sum()),
                    measures.loc[scaled, "measure_id"].head(5).tolist())
            zero_net = measures[scaled & (measures["unit_kwh_1st"] == 0) & ((measures["kwh_water_1st"] != 0) | (measures["kwh_water_2nd"] != 0))]
            if len(zero_net):
                rep.add("warning", "zero_net_profile_water", "water energy on a load-shifting (zero-net) kWh-per-unit profile is priced on that profile, which nets to "
                        "zero, so its WaterEnergyBen is about 0; give water-energy savings on a separate measure", len(zero_net), zero_net["measure_id"].head(5).tolist(),
                        _cells("Measure", mrows(zero_net), "UnitkWhIOUWater1stBaseline"))

    # --- water energy (priced on the measure's electric profile: a water measure with no electric profile cannot be priced)
    water = measures[(measures["kwh_water_1st"] != 0) | (measures["kwh_water_2nd"] != 0)]
    rep.stats["water-energy measures"] = f"{len(water):,}"
    if len(water):
        no_prof = 0
        eg = []
        where: list = []
        for key, g in water.groupby(["shape_key", "PA", "target_sector"]):
            has_user = user is not None and user.match(*key) is not None
            has_lib = cat is not None and real_impact_profiles.match_profile_id(*key)[0] is not None
            if not (has_user or has_lib):
                no_prof += len(g)
                eg.append(" | ".join(map(str, key)))
                where += _cells("Measure", mrows(g), "E3MeaElecEndUseShape")
        if no_prof:
            rep.add("warning", "no_water_profile", "water-energy measures with no electric profile (the water energy is priced on the measure's electric profile) are NOT priced "
                    "(WaterPricingStatus = UNPRICED)", no_prof, eg, where)

    # --- gas profile coverage
    gas = measures[(measures["annual_net_therms_1st"] != 0) | (measures["annual_net_therms_2nd"] != 0)]
    rep.stats["gas-savings measures"] = f"{len(gas):,}"
    no_gas = gas[gas["gas_profile_key"].isna() | ~gas["gas_profile_key"].map(lambda k: not pd.isna(k) and gas_savings_profiles.get_shape(str(k)) is not None)]
    if len(no_gas):
        rep.add("warning", "no_gas_profile", "gas measures whose E3GasSavProfile is not a known monthly profile are NOT priced (GasPricingStatus = UNPRICED); "
                "supply the profile or fix the name", len(no_gas), no_gas["gas_profile_key"].fillna("(blank)").unique(), _cells("Measure", mrows(no_gas), "E3GasSavProfile"))
    bad_gs = gas[~gas["gas_sector"].astype(str).str.upper().isin(VALID_GAS_SECTORS)]
    if len(bad_gs):
        rep.add("error", "bad_gas_sector", f"E3GasSector must be one of Residential/Commercial/Core for measures with gas savings", len(bad_gs), bad_gs["gas_sector"].unique(),
                _cells("Measure", mrows(bad_gs), "E3GasSector"))

    # --- ACC horizon and run-parameter version
    if acc is not None:
        from .pipeline import discount_settings, gas_avoided_cost, real_avoided_cost_v2
        if acc.settings_acc not in discount_settings.known_versions():
            rep.add("error", "unknown_settings_version", f"no built-in run parameters / E3 tables for the {acc.settings_acc} ACC "
                    f"(known: {discount_settings.known_versions()})")
        try:
            import pyarrow.parquet as pq
            yrs = pq.read_table(str(real_avoided_cost_v2._pq()), columns=["year"]).column("year").to_numpy()
            e0, e1 = int(yrs.min()), int(yrs.max())
            g0, g1 = gas_avoided_cost.year_range()
            last = (measures["claim_year"] + np.ceil(measures["eul_years_exact"])).astype(int)
            rep.stats["ACC years"] = f"electric {e0}-{e1}, gas {g0}-{g1}"
            early_m, long_m = (measures["claim_year"] < e0).to_numpy(), (last > e1 + 1).to_numpy()
            early, long = int(early_m.sum()), int(long_m.sum())
            if early:
                rep.add("warning", "before_acc_horizon", f"claim year is before the first ACC year ({e0}): those years are priced with the {e0} values", early,
                        cells=_cells("Measure", mrows(early_m), "ClaimYearQuarter"))
            if long:
                rep.add("info", "beyond_acc_horizon", f"measure life runs past the last ACC year ({e1}): later years are priced with the {e1} values", long,
                        cells=_cells("Measure", mrows(long_m), "EUL_Yrs"))
        except Exception as exc:                                    # ACC unreadable: the run itself will report it
            rep.add("warning", "acc_unreadable", f"could not read the ACC year range: {exc}")
    return rep
