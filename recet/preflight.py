"""Preflight check of a CET-format input job, before any pricing.

Reads Measure.csv (and ProgramCost.csv) exactly as the run would and reports, without pricing anything:

* format problems that would stop the run (not pipe-delimited, missing columns, duplicate CEInputID, bad ClaimYearQuarter, unknown IOU, non-numeric values),
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


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    count: int = 0
    examples: list = field(default_factory=list)


@dataclass
class PreflightReport:
    issues: list[Issue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def add(self, severity: str, code: str, message: str, count: int = 0, examples=None) -> None:
        self.issues.append(Issue(severity, code, message, count, list(examples or [])[:5]))

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

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{"severity": i.severity, "code": i.code, "count": i.count, "message": i.message, "examples": "; ".join(map(str, i.examples))}
                             for i in self.issues])


def _nonblank_numeric_problems(df: pd.DataFrame, col: str) -> pd.Series:
    raw = df[col]
    parsed = pd.to_numeric(raw, errors="coerce")
    return raw.notna() & parsed.isna() & (raw.astype(str).str.strip() != "")


def _check_programs(rep: PreflightReport, measures: pd.DataFrame, programcost_csv, allocate_statewide: bool, apply_exclude_from_ce: bool) -> None:
    """The measures' programs against the program metadata (programs.py): unknown PrgIDs, claims outside the program's years, closed programs, programs
    flagged Exclude_From_CE, and a ProgramCost PA that differs from the program list's PA. None of these stops the run."""
    from . import outputs, programs
    meta = programs.lookup(measures["prg_id"])
    pid = measures["prg_id"].astype(str).str.strip().to_numpy()
    year = measures["claim_year"].to_numpy()
    missing = meta["Prg_MatchedOn"].to_numpy() == "(not found)"
    rep.stats["programs"] = f"{len(set(pid)):,} ({len(set(pid[missing])):,} not in the program metadata)"
    if missing.any():
        rep.add("warning", "program_not_in_metadata", "PrgID is not in the program metadata (the built-in CEDARS Programs list or the supplied one): its Prg_ columns are "
                "blank and its measures count as segment '(blank)'", int(missing.sum()), sorted(set(pid[missing])))
    start = pd.to_numeric(meta.get("Prg_StartYear"), errors="coerce").to_numpy() if "Prg_StartYear" in meta else None
    end = pd.to_numeric(meta.get("Prg_EndYear"), errors="coerce").to_numpy() if "Prg_EndYear" in meta else None
    if start is not None and end is not None:
        out = (~np.isnan(start) & (year < start)) | (~np.isnan(end) & (year > end))
        if out.any():
            rep.add("warning", "claim_outside_program_years", "claim year is before the program's StartYear or after its EndYear in the program metadata",
                    int(out.sum()), sorted(set(pid[out])))
    if "Prg_ProgramStatus" in meta:
        closed = meta["Prg_ProgramStatus"].str.lower().isin({"closed", "closure_pending"}).to_numpy()
        if closed.any():
            rep.add("info", "program_closed", "the program metadata lists the program as closed or closure_pending", int(closed.sum()), sorted(set(pid[closed])))
    if outputs.EXCLUDE_FLAG in meta:
        flag = programs.is_true(meta[outputs.EXCLUDE_FLAG]).to_numpy()
        if flag.any():
            what = ("left out of the portfolio summary (shown in its 'excluded' row)" if apply_exclude_from_ce
                    else "still in the portfolio summary; tick 'Leave Exclude_From_CE programs out of the portfolio summary' to leave them out")
            rep.add("info", "program_excluded_from_ce", f"the program metadata flags the program Exclude_From_CE: its measures are priced and {what}",
                    int(flag.sum()), sorted(set(pid[flag])))
    if programcost_csv:
        credited = outputs.credited_iou(measures, programcost_csv, allocate_statewide).to_numpy()
        listed = meta["Prg_PA"].to_numpy()
        differ = (credited != "") & ~missing & (credited != listed)
        if differ.any():
            rep.add("info", "program_pa_differs", "the program's PA in ProgramCost.csv (CreditedIOU) differs from its PA in the program metadata", int(differ.sum()),
                    sorted({f"{p}: {c} vs {l}" for p, c, l in zip(pid[differ], credited[differ], listed[differ])}))


def check_inputs(measure_csv: str | Path, programcost_csv: str | Path | None = None, *, acc=None, allocate_statewide: bool = False,
                 base_year: int | None = None, apply_exclude_from_ce: bool = False) -> PreflightReport:
    """`acc` is a versions.ResolvedAcc (for the horizon and E3-version checks); the runtime must already be configured for it (runtime.apply) so that profile
    and TOU coverage reflect this run's user-supplied profiles."""
    rep = PreflightReport()
    p = Path(measure_csv)
    if not p.exists():
        rep.add("error", "file_missing", f"Measure file not found: {p}")
        return rep
    head = p.read_text(encoding="utf-8-sig", errors="replace").splitlines()[:1]
    if head and "|" not in head[0]:
        rep.add("error", "not_pipe_delimited", "The header has no '|' -- CET Measure.csv files are pipe-delimited"
                + (" (this one looks comma-delimited)" if "," in head[0] else ""))
        return rep
    df = pd.read_csv(p, sep="|", low_memory=False, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    rep.stats["measures"] = f"{len(df):,}"
    missing = [c for c in REQUIRED_MEASURE_COLUMNS if c not in df.columns]
    if missing:
        rep.add("error", "missing_columns", f"Measure.csv lacks required column(s): {missing}")
        return rep
    for col, why in OPTIONAL_MEASURE_COLUMNS.items():
        if col not in df.columns:
            rep.add("warning", "missing_optional_column", f"no {col} column: {why}")

    dup = df["CEInputID"][df["CEInputID"].duplicated()]
    if df["CEInputID"].isna().any():
        rep.add("error", "blank_id", "blank CEInputID", int(df["CEInputID"].isna().sum()))
    if len(dup):
        rep.add("error", "duplicate_id", "CEInputID must be unique (results are keyed on it)", int(dup.nunique()), dup.unique())
    bad_cyq = ~df["ClaimYearQuarter"].astype(str).str.match(r"^\d{4}Q[1-4]$")
    if bad_cyq.any():
        rep.add("error", "bad_claim_year_quarter", "ClaimYearQuarter must look like 2028Q1", int(bad_cyq.sum()), df["ClaimYearQuarter"][bad_cyq].unique())
    bad_pa = ~df["IOU_AC_Territory"].isin(VALID_PA)
    if bad_pa.any():
        rep.add("error", "bad_iou", f"IOU_AC_Territory must be one of {sorted(VALID_PA)} (the run prices each measure in that territory)",
                int(bad_pa.sum()), df["IOU_AC_Territory"][bad_pa].unique())
    for col in NUMERIC_COLUMNS + [c for c in ("UnitkWhIOUWater1stBaseline", "UnitkWhIOUWater2ndBaseline") if c in df.columns]:
        bad = _nonblank_numeric_problems(df, col)
        if bad.any():
            rep.add("error", "non_numeric", f"{col} has non-numeric values", int(bad.sum()), df[col][bad].unique())
    for col in NEEDED_VALUES:
        blank = df[col].isna() | (df[col].astype(str).str.strip() == "")
        if blank.any():
            rep.add("error", "blank_required_value", f"{col} is blank (CET requires a value; the measure's results would come out blank)", int(blank.sum()), df["CEInputID"][blank].tolist())
    rul = pd.to_numeric(df["RUL_Yrs"], errors="coerce").fillna(0.0)
    blank2 = (rul > 0) & (df["UnitkWh2ndBaseline"].isna() | (df["UnitkWh2ndBaseline"].astype(str).str.strip() == ""))
    if blank2.any():
        rep.add("error", "blank_second_baseline", "UnitkWh2ndBaseline is blank on early-replacement measures (RUL_Yrs > 0), which are priced on it after the RUL", int(blank2.sum()),
                df["CEInputID"][blank2].tolist())
    if rep.errors:
        return rep
    for col, lo, hi, lo_open, hi_open in CET_RANGES:
        v = pd.to_numeric(df[col], errors="coerce")
        bad = v.notna() & ((v <= lo) if lo_open else (v < lo)) | v.notna() & ((v >= hi) if hi_open else (v > hi))
        if bad.any():
            rng = f"{'(' if lo_open else '['}{lo}, {hi if hi != float('inf') else 'no upper limit'}{')' if hi_open else ']'}"
            rep.add("warning", "outside_cet_range", f"{col} is outside CET's allowed range {rng}: CET would reject this upload", int(bad.sum()), df[col][bad].unique())
    eul = pd.to_numeric(df["EUL_Yrs"], errors="coerce")
    if ((rul > 0) & (rul >= eul)).any():
        rep.add("warning", "rul_not_below_eul", "RUL_Yrs is not below EUL_Yrs; such a measure is treated as single-baseline for its whole life", int(((rul > 0) & (rul >= eul)).sum()))

    from . import run_poc
    from .pipeline import cost_tests, gas_savings_profiles, real_impact_profiles
    try:
        measures, program = run_poc.load_job(p, programcost_csv, allocate_statewide=allocate_statewide)
    except ValueError as exc:
        rep.add("error", "program_alignment", str(exc))
        return rep
    rep.stats["claim years"] = f"{measures['claim_year'].min()}-{measures['claim_year'].max()}"
    rep.stats["First Year (base year) that will be used"] = base_year or int(measures["claim_year"].min())
    if programcost_csv is None:
        rep.add("info", "no_program_costs", "no ProgramCost.csv: NoAdmin and regular cost tests will be equal and WeightedProgramCost is 0")
    else:
        pc = pd.read_csv(programcost_csv, sep="|")
        sw_rows = int((pc["PA"].astype(str).str.upper() == "SW").sum()) if "PA" in pc.columns else 0
        if sw_rows and not allocate_statewide:
            rep.add("info", "statewide_program_costs", "ProgramCost rows with PA = SW are given PA = SCE (what CET does with them); tick 'allocate statewide programs' to split them across the IOUs", sw_rows)
        gap = cost_tests.check_program_alignment(measures, program)
        if gap["measure_programs_without_costs"] or gap["program_costs_without_measures"]:
            rep.add("error", "program_alignment", f"PrgIDs do not line up: {gap}")

    _check_programs(rep, measures, programcost_csv, allocate_statewide, apply_exclude_from_ce)

    # --- electric profile coverage
    elec = measures[(measures["shape_scale"] != 0) | (measures["shape_scale_2nd"] != 0) | (measures["annual_net_kw_1st"] != 0)]
    cat = real_impact_profiles._catalog_hourly() if real_impact_profiles.is_available() else None
    user = real_impact_profiles.user_profiles()
    keys = elec.groupby(["shape_key", "PA", "target_sector"]).size().rename("n").reset_index()
    counts = {"user profile": 0, "library": 0, "library (sector-loose match)": 0, "NO PROFILE": 0}
    missing_examples: list = []
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
    rep.stats["electric-savings measures"] = f"{len(elec):,}"
    for k, v in counts.items():
        if v:
            rep.stats[f"  electric shape: {k}"] = f"{v:,}"
    if counts["NO PROFILE"]:
        rep.add("warning", "no_electric_profile", "electric measures with no 8,760 profile are NOT priced (ElecPricingStatus = UNPRICED); "
                "supply a profile for them or check the E3MeaElecEndUseShape spelling", counts["NO PROFILE"], missing_examples)
    if counts["library (sector-loose match)"]:
        rep.add("info", "loose_profile_match", "matched the library profile on (end use, IOU) only, because E3TargetSector has no exact match", counts["library (sector-loose match)"])
    if cat is None and not counts["user profile"]:
        rep.add("warning", "no_profile_library", "CEDARS impact-profile library not found (reference data folder): measures cannot be matched to 8,760 profiles")

    # --- water energy (priced on the measure's electric profile: a water measure with no electric profile cannot be priced)
    water = measures[(measures["kwh_water_1st"] != 0) | (measures["kwh_water_2nd"] != 0)]
    rep.stats["water-energy measures"] = f"{len(water):,}"
    if len(water):
        no_prof = 0
        eg = []
        for key, g in water.groupby(["shape_key", "PA", "target_sector"]):
            has_user = user is not None and user.match(*key) is not None
            has_lib = cat is not None and real_impact_profiles.match_profile_id(*key)[0] is not None
            if not (has_user or has_lib):
                no_prof += len(g)
                eg.append(" | ".join(map(str, key)))
        if no_prof:
            rep.add("warning", "no_water_profile", "water-energy measures with no electric profile (the water energy is priced on the measure's electric profile) are NOT priced "
                    "(WaterPricingStatus = UNPRICED)", no_prof, eg)

    # --- gas profile coverage
    gas = measures[(measures["annual_net_therms_1st"] != 0) | (measures["annual_net_therms_2nd"] != 0)]
    rep.stats["gas-savings measures"] = f"{len(gas):,}"
    no_gas = gas[gas["gas_profile_key"].isna() | ~gas["gas_profile_key"].map(lambda k: not pd.isna(k) and gas_savings_profiles.get_shape(str(k)) is not None)]
    if len(no_gas):
        rep.add("warning", "no_gas_profile", "gas measures whose E3GasSavProfile is not a known monthly profile are NOT priced (GasPricingStatus = UNPRICED); "
                "supply the profile or fix the name", len(no_gas), no_gas["gas_profile_key"].fillna("(blank)").unique())
    bad_gs = gas[~gas["gas_sector"].astype(str).str.upper().isin(VALID_GAS_SECTORS)]
    if len(bad_gs):
        rep.add("error", "bad_gas_sector", f"E3GasSector must be one of Residential/Commercial/Core for measures with gas savings", len(bad_gs), bad_gs["gas_sector"].unique())

    # --- ACC horizon and E3 version
    if acc is not None:
        from .pipeline import discount_settings, gas_avoided_cost, real_avoided_cost_v2
        if acc.cet_version not in discount_settings.known_versions():
            rep.add("error", "unknown_e3_version", f"E3 tables have no Version {acc.cet_version} (known: {discount_settings.known_versions()})")
        try:
            import pyarrow.parquet as pq
            yrs = pq.read_table(str(real_avoided_cost_v2._pq()), columns=["year"]).column("year").to_numpy()
            e0, e1 = int(yrs.min()), int(yrs.max())
            g0, g1 = gas_avoided_cost.year_range()
            last = (measures["claim_year"] + np.ceil(measures["eul_years_exact"])).astype(int)
            rep.stats["ACC years"] = f"electric {e0}-{e1}, gas {g0}-{g1}"
            early = int((measures["claim_year"] < e0).sum())
            long = int((last > e1 + 1).sum())
            if early:
                rep.add("warning", "before_acc_horizon", f"claim year is before the first ACC year ({e0}): those years are priced with the {e0} values", early)
            if long:
                rep.add("info", "beyond_acc_horizon", f"measure life runs past the last ACC year ({e1}): later years are priced with the {e1} values", long)
        except Exception as exc:                                    # ACC unreadable: the run itself will report it
            rep.add("warning", "acc_unreadable", f"could not read the ACC year range: {exc}")
    return rep
