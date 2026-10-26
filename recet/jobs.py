"""Running one job end to end: `RunConfig` in, a folder of result files out.

    from recet.jobs import RunConfig, run_job
    result = run_job(RunConfig(measures="Measure.csv", programcost="ProgramCost.csv", out_dir="results", acc_version="2026"))

Files written to the run folder (`<out_dir>/<run name>_<acc>/`):

  measure_level_<acc>.csv        (or .csv.zip / .xlsx / .parquet, per output_format) one row per measure: the raw input columns, the derived poc_* fields, pricing status, every output (see outputs.py)
  portfolio_summary_<acc>.csv    totals and ratios overall, by IOU territory, by credited IOU and by program segment
  preflight.txt                  the input check that ran first
  preflight.json                 the same check as JSON (ok, stats, issues), read by the web app's Validate inputs panel
  run_settings.json              everything needed to reproduce the run (options, resolved ACC files, supplied files with sizes, package version, timings)
  components_summary_<acc>.csv   (components mode) portfolio value of each ACC component, with the sum-check against the totals
  portfolio_8760_<acc>.csv       (combined 8760 mode) the portfolio's hourly net kWh; portfolio_8760_unshaped_<acc>.csv lists measures left out
  cet_comparison_*.csv           (when a CET outputs file is given) POC vs CET, per measure and in total
  comparison_<A>_vs_<B>.csv      (compare mode) per-measure and portfolio differences between two ACC versions (see `run_compare`)

A job with more Measure.csv rows than `chunk_size` is run in parts of that size (recet/chunked.py), so its memory stays about that of one part; the files are
the same, and RunResult.frame is None (the measure-level table is only in its file).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

Log = Callable[[str], None]


@dataclass
class RunConfig:
    # --- inputs
    measures: str = ""                       # CET Measure.csv (pipe- or comma-delimited)
    programcost: str | None = None           # CET ProgramCost.csv (optional)
    workbook: str | None = None              # an Excel job workbook (Measure, ProgramCost, RunParameters, SWAllocation, GasProfiles, Profiles8760 sheets) instead of files
    out_dir: str = "recet_results"
    run_name: str = ""                       # folder name prefix; default is a timestamp
    # --- ACC version and supplied ACC files
    acc_version: str = "2024"                # "2024", "2026", or a new label defined by the supplied files
    acc_electric_file: str | None = None     # replaces the built-in electric ACC for acc_version (or defines a new version)
    acc_gas_file: str | None = None
    settings_from: int | None = None         # the ACC version whose run parameters / E3 tables a NEW acc label uses (default: the newest built-in)
    # --- supplied profiles
    profiles_8760_file: str | None = None
    profiles_gas_file: str | None = None
    run_parameters_file: str | None = None   # run parameters per PA (discount rates, base year, gas CO2): a listed PA replaces the built-in values
    sw_factors_file: str | None = None       # statewide allocation factors: a (Year, ProgramID) row replaces the built-in row, a new one is added
    programs_file: str | None = None         # program metadata (CEDARS Programs list layout): a PrgID replaces the built-in row, a new one is added
    normalize_profiles: bool = True          # rescale supplied profiles that do not sum to 1 (else it is an error)
    # --- where the big reference files are
    reference_dir: str | None = None
    # --- modes
    components: bool = False                 # price each ACC component separately
    portfolio_8760: bool = False             # combined 8,760 electric load profile
    profile_years: list[int] = field(default_factory=list)   # calendar years of the combined profile (default: First Year)
    market_effects: float = 0.0              # CET's Market Effects: 0 or 0.05, applied to benefits AND costs (CET's MEBens and MECost set together)
    include_societal: bool = True            # SCB / SCH (about two thirds of the run time)
    emissions: bool = True                   # CO2 / NOx / PM10 columns
    allocate_statewide: bool = False         # split statewide (SW_) programs across the IOUs
    apply_exclude_from_ce: bool = False      # leave programs flagged Exclude_From_CE out of the portfolio summary (they are still priced)
    base_year: int | None = None             # CET's "First Year" (default: earliest claim year)
    cet_outputs: str | None = None           # a CET *_outputs.csv to compare against
    compare_with: str | None = None          # another ACC version key: run it too and write a per-measure comparison
    output_format: str = "csv"               # csv | csv.zip (each CSV inside a zip; the web app's choice) | parquet | xlsx
    excel_max_rows: int = 50_000             # an xlsx table with more rows than this is written as excel_fallback instead (outputs.ExcelLimit)
    excel_fallback: str = "csv.zip"          # csv.zip | csv | parquet: every table of a run whose measures are over excel_max_rows (run_format)
    preflight_only: bool = False
    filing_validation: str = ""              # "" | "claims" | "budget_filing": also apply CEDARS' upload checks for that filing (filing_rules.py); flags, never stops
    filing_year: int | None = None           # the filing's year for the rules that name it (default: each record's claim year; ProgramCost: the earliest)
    parquet_row_group: int = 200_000         # rows per row group when a run in parts writes parquet (outputs.TableWriter): bigger compresses better, holds more memory
    chunk_size: int = 20_000                 # a job with more Measure.csv rows than this is run in parts of this many rows, to bound memory (chunked.py); 0 = never
    # --- test hook (not shown in the GUI)
    subset_step: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> "RunConfig":
        names = {f.name for f in fields(cls)}
        d = dict(d)
        legacy = d.pop("e3_version", None)            # configs saved before 0.1.17 named the E3 tables by CET's version number
        if legacy is not None and d.get("settings_from") is None:
            from .versions import acc_of_cet_version
            d["settings_from"] = acc_of_cet_version(legacy)
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


@dataclass
class RunResult:
    run_dir: Path
    files: dict[str, Path] = field(default_factory=dict)
    totals: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    frame: pd.DataFrame | None = None
    filing: pd.DataFrame | None = None       # with filing_validation: the failing records (preflight.PreflightReport.filing)
    filing_rules: dict | None = None         # with filing_validation: the rule set used


def _size(p: str | Path | None) -> str:
    if not p or not Path(p).exists():
        return "(none)"
    return f"{Path(p).stat().st_size / 1e6:,.1f} MB"


def _run_folder(cfg: RunConfig, acc_key: str) -> Path:
    stamp = cfg.run_name.strip() or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    d = Path(cfg.out_dir) / f"{stamp}_acc{acc_key}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def prepare(cfg: RunConfig, run_dir: Path, log: Log):
    """Load and validate the supplied inputs, resolve the ACC version and point the pipeline at it. Returns (acc, user_profiles, user_gas).
    With a job workbook, its sheets are first written out as files and `cfg` is updated to point at them (a file chosen separately wins over its sheet)."""
    from . import overrides, paths, programs, runtime, versions, workbook
    if cfg.reference_dir:
        paths.set_reference_dir(cfg.reference_dir)
    work = run_dir / "_prepared_inputs"
    if cfg.workbook:
        log(f"reading job workbook {cfg.workbook}")
        for fld, f in workbook.extract(cfg.workbook, work / "workbook").items():
            sheet = next(k for k, v in workbook.SHEETS.items() if v[0] == fld)
            if getattr(cfg, fld):
                log(f"note: sheet {sheet} ignored: {fld} was given separately ({getattr(cfg, fld)})")
            else:
                setattr(cfg, fld, str(f))
                log(f"  sheet {sheet} -> {f.name}")
    elec = gas = None
    if cfg.acc_electric_file:
        log(f"checking supplied electric ACC file {cfg.acc_electric_file}")
        chk = overrides.check_electric_acc(cfg.acc_electric_file)
        log(chk.text())
        chk.raise_if_bad("supplied electric ACC file")
        elec = overrides.prepare_electric_acc(cfg.acc_electric_file, work)
    if cfg.acc_gas_file:
        log(f"checking supplied gas ACC file {cfg.acc_gas_file}")
        chk = overrides.check_gas_acc(cfg.acc_gas_file)
        log(chk.text())
        chk.raise_if_bad("supplied gas ACC file")
        gas = overrides.prepare_gas_acc(cfg.acc_gas_file, work)
    acc = versions.resolve_acc(cfg.acc_version, elec, gas, cfg.settings_from)
    for n in acc.notes:
        log(f"note: {n}")
    log(acc.describe())
    if acc.electric is None or not Path(acc.electric).exists():
        raise FileNotFoundError(f"electric ACC file for version {acc.key!r} not found. Set the reference data folder (currently: {paths.reference_dir_source()}) "
                                f"or supply an electric ACC file.")
    user_profiles = user_gas = None
    if cfg.profiles_8760_file:
        user_profiles = overrides.load_user_profiles(cfg.profiles_8760_file, normalize=cfg.normalize_profiles)
        log(f"loaded {len(user_profiles)} supplied 8,760 profile(s):\n{user_profiles.summary().to_string(index=False)}")
        for n in user_profiles.notes:
            log(f"note: {n}")
    if cfg.profiles_gas_file:
        user_gas, notes = overrides.load_user_gas_profiles(cfg.profiles_gas_file, normalize=cfg.normalize_profiles)
        log(f"loaded {len(user_gas)} supplied gas monthly profile(s): {', '.join(user_gas)}")
        for n in notes:
            log(f"note: {n}")
    run_params = sw = progs = None
    if cfg.run_parameters_file:
        run_params, notes = overrides.load_run_parameters(cfg.run_parameters_file)
        log(f"loaded run parameters for {', '.join(run_params.index)} (these PAs' built-in values are replaced):\n{run_params.to_string()}")
        for n in notes:
            log(f"note: {n}")
    if cfg.sw_factors_file:
        sw, notes = overrides.load_sw_factors(cfg.sw_factors_file)
        log(f"loaded {len(sw)} statewide allocation factor row(s) ({sw['ProgramID'].nunique()} programs, years {sw['Year'].min()}-{sw['Year'].max()}); "
            "matching (Year, ProgramID) rows replace the built-in ones")
        if not cfg.allocate_statewide:
            log("note: statewide allocation is off for this run, so the supplied factors are not used")
        for n in notes:
            log(f"note: {n}")
    if cfg.programs_file:
        progs, notes = programs.load_programs(cfg.programs_file)
        log(f"loaded program metadata for {len(progs):,} program(s); matching PrgIDs replace the built-in rows")
        for n in notes:
            log(f"note: {n}")
    runtime.apply(acc, user_profiles=user_profiles, user_gas_profiles=user_gas, run_parameters=run_params, sw_factors=sw, programs=progs,
                  reference_dir=cfg.reference_dir)
    return acc, user_profiles, user_gas


def societal_available(groups: dict, acc, wanted: bool, result: RunResult, log: Log) -> bool:
    """Whether the societal tests can be priced: wanted, and every ACC version in `groups` has SCT Base / SCT High avoided costs (else a note says why not)."""
    if not wanted:
        return False
    from . import runtime
    from .pipeline import gas_avoided_cost, real_avoided_cost_v2
    ok = True
    for k, g in groups.items():
        if g is not acc:
            runtime.switch_acc(g)
        e_tests, g_tests = real_avoided_cost_v2.available_cost_tests(), gas_avoided_cost.available_cost_tests()
        if not {"SCT Base", "SCT High"} <= set(e_tests) or "SCT" not in g_tests:
            ok = False
            note = (f"the {g.label} has no Societal Cost Test avoided costs (electric: {', '.join(e_tests)}; gas: {', '.join(g_tests)}): "
                    f"the SCT Base / SCT High columns (SB, SH, SCB, SCH) are left blank")
            result.notes.append(note)
            log(f"note: {note}")
            break
    runtime.switch_acc(acc)
    return ok


def excel_limit(cfg: RunConfig, result: RunResult, log: Log):
    """The run's outputs.ExcelLimit: its notes (a table written as CSV instead of Excel) go to the run log and RunResult.notes."""
    from . import outputs

    def notify(msg: str) -> None:
        result.notes.append(msg)
        log(f"note: {msg}")
    return outputs.ExcelLimit(cfg.excel_max_rows, cfg.excel_fallback, notify)


def run_format(cfg: RunConfig, rows: int, log: Log) -> str:
    """The format the run's tables are written in: cfg.output_format, except that an Excel run with more measures than excel_max_rows writes EVERY table as
    excel_fallback (zipped CSV by default), so a run's files are all one format. The measure-level table is the run's longest; outputs.ExcelLimit still
    guards any single table. Called once the number of measures (after statewide allocation) is known, before any table is written."""
    from . import outputs
    limit = min(int(cfg.excel_max_rows), outputs.EXCEL_MAX_ROWS)
    if cfg.output_format != "xlsx" or rows <= limit:
        return cfg.output_format
    fallback = cfg.excel_fallback if cfg.excel_fallback in outputs.FORMATS and cfg.excel_fallback != "xlsx" else "csv.zip"
    name = {"csv.zip": "zipped CSV", "csv": "CSV"}.get(fallback, fallback)
    log(f"note: {rows:,} measures is more than the {limit:,}-row limit for Excel output, so every table of this run is written as {name} instead")
    return fallback


def write_settings(cfg: RunConfig, acc, run_dir: Path, result: RunResult, t_start: float, measures_priced: int, base_year: int, parts: dict | None = None) -> None:
    """run_settings.json: everything needed to reproduce the run. `parts`: how a chunked run split the job."""
    from . import __version__, paths
    ref = paths.ref_status()
    settings = {
        "package_version": __version__, "finished": datetime.now().isoformat(timespec="seconds"), "seconds": round(time.time() - t_start),
        "config": asdict(cfg),
        "acc": {"key": acc.key, "label": acc.label, "avoided_cost_version": acc.avoided_cost_version, "settings_acc_version": acc.settings_acc, "electric": str(acc.electric), "electric_source": acc.electric_source,
                "electric_size": _size(acc.electric), "gas": str(acc.gas), "gas_source": acc.gas_source, "notes": acc.notes},
        "reference_dir": {"path": str(paths.reference_dir() or ""), "source": paths.reference_dir_source(), "files": ref},
        "supplied_files": {k: {"path": v, "size": _size(v)} for k, v in (("workbook", cfg.workbook), ("profiles_8760", cfg.profiles_8760_file),
                                                                       ("profiles_gas", cfg.profiles_gas_file), ("run_parameters", cfg.run_parameters_file),
                                                                       ("sw_factors", cfg.sw_factors_file), ("programs", cfg.programs_file),
                                                                       ("acc_electric", cfg.acc_electric_file),
                                                                       ("acc_gas", cfg.acc_gas_file)) if v},
        "inputs": {"measures": str(cfg.measures), "programcost": str(cfg.programcost or ""), "measures_priced": measures_priced, "first_year": base_year},
        "totals": result.totals,
    }
    if parts:
        settings["parts"] = parts
    if result.filing_rules:
        settings["filing_validation"] = result.filing_rules
    if result.notes:
        settings["notes"] = result.notes
    (run_dir / "run_settings.json").write_text(json.dumps(settings, indent=2, default=str), encoding="utf-8")
    result.files["run_settings"] = run_dir / "run_settings.json"


def write_findings(rep, cfg: RunConfig, run_dir: Path) -> Path | None:
    """preflight_findings.xlsx: the input check as a workbook to review (preflight.PreflightReport.write_xlsx), with each flagged cell's record ID. The IDs
    are read (one column of the file) only when some finding points at that file. A workbook that cannot be written is left out; the run goes on."""
    from .pipeline import delimited
    tables = {t for i in rep.issues for t, _r, _c in i.cells}
    measure_ids = program_ids = None
    try:
        if "Measure" in tables and cfg.measures and Path(cfg.measures).exists():
            measure_ids = delimited.read_cet_csv(cfg.measures, dtype=str, keep_default_na=False, usecols=lambda c: c.strip() == "CEInputID")["CEInputID"].tolist()
        if "ProgramCost" in tables and cfg.programcost and Path(cfg.programcost).exists():
            pc = delimited.read_cet_csv(cfg.programcost, dtype=str, keep_default_na=False)
            if {"PrgID", "PrgYear"} <= set(pc.columns):
                program_ids = (pc["PrgID"] + " " + pc["PrgYear"]).tolist()
        return rep.write_xlsx(run_dir / "preflight_findings.xlsx", measure_ids, program_ids)
    except Exception:                               # e.g. an unreadable file: preflight.txt and the log still have every finding
        return None


def run_job(cfg: RunConfig, log: Log = print) -> RunResult:
    t_start = time.time()
    if not cfg.measures and not cfg.workbook:
        raise ValueError("no Measure.csv or job workbook given")
    cfg = RunConfig.from_dict(asdict(cfg))          # prepare() may fill in files from a workbook; leave the caller's config as it was

    from . import progress
    progress.report("preparing")
    run_dir = _run_folder(cfg, str(cfg.acc_version))
    result = RunResult(run_dir=run_dir)
    acc, user_profiles, user_gas = prepare(cfg, run_dir, log)
    tag = f"acc{acc.key}"

    # ---- a large job is run in parts (chunked.py): split Measure.csv once, here
    from .pipeline import delimited
    parts = dtypes = values = None
    parts_dir = run_dir / "_parts"
    if cfg.chunk_size and cfg.subset_step <= 1 and Path(cfg.measures).exists() and delimited.detect_sep(cfg.measures):
        n_rows = delimited.count_rows(cfg.measures)
        if n_rows > cfg.chunk_size:
            log(f"splitting Measure.csv ({n_rows:,} rows) into parts of {cfg.chunk_size:,} rows")
            progress.report("splitting")
            ts = time.time()
            parts = delimited.split_file(cfg.measures, cfg.chunk_size, parts_dir / "measures")
            tt = time.time()
            dtypes, values = delimited.part_dtypes(parts, ("ClaimYearQuarter", "ACCVersion"))
            log(f"  split into {len(parts)} parts in {tt - ts:.0f}s; column types of the whole file read in {time.time() - tt:.0f}s")
    fmt = cfg.output_format
    if fmt == "xlsx" and parts is not None and n_rows > cfg.excel_max_rows:
        fmt = cfg.excel_fallback                                   # what run_format / outputs.ExcelLimit will write instead
    progress.plan(parts is not None, fmt, cfg.include_societal, cfg.emissions, bool(cfg.filing_validation))   # the bar's stage weights (progress.py)
    try:
        out = _run(cfg, acc, run_dir, result, tag, log, t_start, parts, dtypes, values)
    finally:
        if parts is not None:
            size = sum(f.stat().st_size for f in parts_dir.rglob("*") if f.is_file())
            log(f"(the parts folder, kept reads included, used {size / 1e6:,.0f} MB of disk; deleted)")
            shutil.rmtree(parts_dir, ignore_errors=True)
    progress.report("done")
    return out


def _run(cfg: RunConfig, acc, run_dir: Path, result: RunResult, tag: str, log: Log, t_start: float, parts, dtypes, values) -> RunResult:
    from . import components as comp_mod, outputs, profile8760, preflight, progress, run_poc
    from .pipeline import emissions
    excel = excel_limit(cfg, result, log)

    # ---- preflight
    log("\n== preflight ==")
    progress.report("preflight", 0, len(parts) if parts is not None else None)
    tp = time.time()
    rep = preflight.check_inputs(cfg.measures, cfg.programcost, acc=acc, allocate_statewide=cfg.allocate_statewide, base_year=cfg.base_year,
                                 apply_exclude_from_ce=cfg.apply_exclude_from_ce, parts=parts, dtypes=dtypes,
                                 filing_rules=cfg.filing_validation or None, filing_year=cfg.filing_year)
    log(f"checked in {time.time() - tp:.0f}s" + (f" ({len(parts)} parts)" if parts is not None else ""))
    (run_dir / "preflight.txt").write_text(rep.text(), encoding="utf-8")
    (run_dir / "preflight.json").write_text(json.dumps(rep.to_dict()), encoding="utf-8")
    result.files["preflight"] = run_dir / "preflight.txt"
    findings = write_findings(rep, cfg, run_dir)
    if findings is not None:
        result.files["preflight_findings"] = findings
    if rep.filing is not None:                                       # CEDARS' upload checks: every failing record, and the rule set used
        result.filing, result.filing_rules = rep.filing, rep.filing_rules
        result.files["filing_validation"] = outputs.write_table(outputs.filing_table(rep.filing, cfg.filing_validation), run_dir / f"filing_validation_{tag}",
                                                                cfg.output_format, excel)
    log(rep.text())
    if not rep.ok:
        raise RuntimeError("preflight found blocking problems (see above); nothing was priced")
    if cfg.preflight_only:
        log("\npreflight only: stopping before pricing")
        return result
    if parts is not None:
        from . import chunked
        return chunked.run_parts(cfg, acc, run_dir, result, parts, dtypes, values, log, t_start)

    # ---- load and price
    log("\n== loading ==")
    progress.report("loading")
    measures, program = run_poc.load_job(cfg.measures, cfg.programcost, allocate_statewide=cfg.allocate_statewide)
    raw = outputs.read_raw(cfg.measures, cfg.allocate_statewide)
    if cfg.subset_step > 1:
        measures = measures.iloc[::cfg.subset_step].reset_index(drop=True)
        log(f"SUBSET RUN: every {cfg.subset_step}th measure only ({len(measures):,}); program costs then weight a partial job")
    base_year = cfg.base_year or int(measures["claim_year"].min())
    meb = float(cfg.market_effects or 0.0)
    mec = meb                                               # one setting: the same adder on benefits (MEBens) and on costs (MECost)
    log(f"{len(measures):,} measures, First Year {base_year}, market effects {meb:.0%} on benefits and costs")
    used = run_format(cfg, len(measures), log)
    if used != cfg.output_format:
        result.notes.append(f"Excel output was chosen, but {len(measures):,} measures is over the {cfg.excel_max_rows:,}-row limit, so every table is "
                            f"{dict(csv='CSV', parquet='parquet').get(used, 'zipped CSV')}")
        cfg.output_format = used                                   # cfg is this run's own copy (run_job)

    # ---- ACC versions: a measure may name its own (ACCVersion column, BRD BR-06.5); blank = this run's version
    from . import runtime, versions
    measures["acc_version"] = measures["acc_version"].replace("", acc.key)
    groups = {k: (acc if k == acc.key else versions.resolve_acc(k)) for k in sorted(measures["acc_version"].unique(), key=lambda k: (k != acc.key, k))}
    if len(groups) > 1:
        log("measures by ACC version: " + ", ".join(f"{g.label} {int((measures['acc_version'] == k).sum()):,}" for k, g in groups.items())
            + f"; program costs are discounted at the {acc.label}'s rates")
        if cfg.acc_electric_file or cfg.acc_gas_file:
            log(f"note: the supplied ACC file(s) replace the {acc.label} only; measures on other versions use those versions' built-in files")

    societal = societal_available(groups, acc, cfg.include_societal, result, log)
    if societal != cfg.include_societal:
        progress.replan(societal=societal)
    log("\n== pricing (TRC" + (", SCB, SCH" if societal else "") + ", PAC, RIM, bill impacts) ==")
    progress.report("pricing")
    t0 = time.time()
    parts, em_parts, comp_parts = [], [], []
    for k, g in groups.items():
        sub = measures[measures["acc_version"] == k].reset_index(drop=True) if len(groups) > 1 else measures
        if g is not acc:
            runtime.switch_acc(g)
            log(f"  pricing {len(sub):,} measure(s) on the {g.label}")
        parts.append(run_poc.price_measure_values(sub, acc_version=g.settings_acc, base_year=base_year, market_effects_bens=meb, market_effects_cost=mec,
                                                  include_societal=societal))
        if cfg.emissions:
            em_parts.append(emissions.price_emissions(sub, acc_version=g.settings_acc, market_effects_bens=meb))
        if cfg.components:
            comp_parts.append(comp_mod.price_components(sub, acc_version=g.settings_acc, base_year=base_year, market_effects_bens=meb, log=log))
    if len(groups) > 1:
        runtime.switch_acc(acc)
    res = run_poc.apply_program_costs(pd.concat(parts, ignore_index=True), measures, program, acc_version=acc.settings_acc, base_year=base_year,
                                      market_effects_bens=meb, include_societal=societal)
    log(f"priced in {time.time() - t0:.0f}s")
    if cfg.emissions:
        log("\n== emissions ==")
        res = res.merge(pd.concat(em_parts, ignore_index=True), on="measure_id", how="left")

    comps = None
    if cfg.components:
        log("\n== ACC components (each priced separately) ==")
        comps = pd.concat(comp_parts, ignore_index=True) if len(comp_parts) > 1 else comp_parts[0]
        chk = comp_mod.check_sums(res, comps)
        for fuel, r in chk.items():
            log(f"  {fuel}: {r['components']} components; sum of components vs total: portfolio {r['portfolio_sum_of_components']:,.0f} vs {r['portfolio_total']:,.0f}, "
                f"worst measure residual {r['max_abs_residual']:.3g} ({r['measures_over_tolerance']} measure(s) over tolerance)")
        table = comp_mod.portfolio_table(comps)
        for fuel, r in chk.items():
            table.loc[len(table)] = {"fuel": "Electric" if fuel == "Elec" else "Gas", "component": "(check) total minus sum of components",
                                     "portfolio_value_usd": r["portfolio_total"] - r["portfolio_sum_of_components"], "share_of_fuel_total": None}
        result.files["components_summary"] = outputs.write_table(table, run_dir / f"components_summary_{tag}", cfg.output_format, excel)

    # ---- assemble and write
    log("\n== writing results ==")
    acc_labels = measures["acc_version"].map({k: g.label for k, g in groups.items()})
    frame = outputs.assemble_measure_level(raw, measures, res, acc_labels if len(groups) > 1 else acc.label,
                                           credited=outputs.credited_iou(measures, cfg.programcost, cfg.allocate_statewide))
    if comps is not None:
        frame = frame.merge(comps, left_on="CEInputID", right_on="measure_id", how="left").drop(columns="measure_id")
        cols = [c for c in frame.columns if c != "CreditedIOU"] + ["CreditedIOU"]
        frame = frame[cols]
    frame = outputs.attach_programs(frame, measures["prg_id"])
    if result.filing is not None:
        frame = outputs.attach_filing(frame, outputs.filing_lookup(result.filing))
    result.frame = frame
    progress.report("writing")
    writer = outputs.TableWriter(run_dir / f"measure_level_{tag}", cfg.output_format, len(frame), cfg.parquet_row_group, keep_parquet=True, excel=excel)
    writer.write(frame)
    result.files["measure_level"] = writer.close()                   # csv / csv.zip: parquet first (kept for the Results tables), then CSV
    summary = outputs.summarize(frame, cfg.apply_exclude_from_ce)
    result.files["portfolio_summary"] = outputs.write_table(summary, run_dir / f"portfolio_summary_{tag}", cfg.output_format, excel)
    result.totals = {k: float(v) for k, v in summary.iloc[0].items() if isinstance(v, (int, float)) and k != "measures"}

    if cfg.portfolio_8760:
        years = cfg.profile_years or [base_year]
        log(f"\n== combined 8,760 electric profile, year(s) {years} ==")
        prof, unshaped, psum = profile8760.portfolio_profile(measures, years, log=log)
        result.files["portfolio_8760"] = outputs.write_table(prof, run_dir / f"portfolio_8760_{tag}", cfg.output_format, excel)
        if len(unshaped):
            result.files["portfolio_8760_unshaped"] = outputs.write_table(unshaped, run_dir / f"portfolio_8760_unshaped_{tag}", cfg.output_format, excel)
        for y, s in psum.items():
            log(f"  {y}: {s['in_profile_kwh']:,.0f} net kWh in the profile ({s['unshaped_kwh']:,.0f} kWh of unshaped measures left out); "
                f"peak hour {s['peak_hour_of_year']} at {s['peak_hour_kwh']:,.0f} kWh")

    if cfg.cet_outputs:
        log(f"\n== comparison with CET outputs {cfg.cet_outputs} ==")
        per, csum = outputs.compare_to_cet(frame, cfg.cet_outputs)
        result.files["cet_comparison_measures"] = outputs.write_table(per, run_dir / f"cet_comparison_measures_{tag}", cfg.output_format, excel)
        result.files["cet_comparison_summary"] = outputs.write_table(csum, run_dir / f"cet_comparison_summary_{tag}", cfg.output_format, excel)
        log(csum.attrs.get("matched", ""))
        log(csum.to_string(index=False, float_format=lambda v: f"{v:,.4f}" if abs(v) < 100 else f"{v:,.0f}"))

    # ---- record
    write_settings(cfg, acc, run_dir, result, t_start, len(measures), base_year)
    progress.report("finishing")
    from . import results_view
    results_view.keep_group_sums(run_dir, frame)                     # the Results page then opens without reading the measure-level file
    log(f"\ndone in {time.time() - t_start:.0f}s -- results in {run_dir}")
    return result


def run_compare(cfg: RunConfig, log: Log = print) -> RunResult:
    """Run `cfg` (its acc_version, with any supplied ACC/profile files) and again against `cfg.compare_with` (built-in files; the same supplied PROFILES), each in its own
    process so nothing is shared between them, then write a per-measure comparison of the two."""
    from . import outputs, progress
    if not cfg.compare_with or cfg.compare_with == cfg.acc_version:
        raise ValueError("compare_with must name a different ACC version")
    folder = Path(cfg.out_dir) / (cfg.run_name.strip() or datetime.now().strftime("compare_%Y%m%d_%H%M%S"))
    folder.mkdir(parents=True, exist_ok=True)
    frames, results = {}, {}
    for key, is_primary in ((cfg.acc_version, True), (cfg.compare_with, False)):
        sub = RunConfig.from_dict({**asdict(cfg), "compare_with": None, "out_dir": str(folder), "run_name": f"run",
                                   **({} if is_primary else {"acc_version": key, "acc_electric_file": None, "acc_gas_file": None, "cet_outputs": None})})
        cfg_path = folder / f"_config_acc{key}.json"
        cfg_path.write_text(sub.to_json(), encoding="utf-8")
        log(f"\n######## ACC version {key} ########")
        env = dict(os.environ, **{progress.SLICE_ENV: f"{0 if is_primary else 1}/2"})       # each run fills its half of the progress bar (progress.py)
        proc = subprocess.Popen([sys.executable, "-m", "recet", "run", "--config", str(cfg_path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                env=env)
        assert proc.stdout is not None
        for line in proc.stdout:
            log(line.rstrip("\n"))
        if proc.wait() != 0:
            raise RuntimeError(f"run for ACC version {key} failed (exit code {proc.returncode})")
        run_dir = folder / f"run_acc{key}"
        frames[key] = outputs.read_table(outputs.results_source(run_dir))
        results[key] = run_dir
    a, b = cfg.acc_version, cfg.compare_with
    progress.report("comparing")
    used = run_format(cfg, max(len(f) for f in frames.values()), log)             # the two runs decided the same way for their own files
    if used != cfg.output_format:
        cfg = RunConfig.from_dict({**asdict(cfg), "output_format": used})
    res = RunResult(run_dir=folder)
    excel = excel_limit(cfg, res, log)
    per, summ = outputs.compare_frames(frames[a], frames[b], f"acc{a}", f"acc{b}")
    res.files["comparison_measures"] = outputs.write_table(per, folder / f"comparison_acc{a}_vs_acc{b}", cfg.output_format, excel)
    res.files["comparison_summary"] = outputs.write_table(summ, folder / f"comparison_summary_acc{a}_vs_acc{b}", cfg.output_format, excel)
    for key, d in results.items():
        res.files[f"run_acc{key}"] = d
    log("\n== portfolio comparison ==\n" + summ.to_string(index=False, float_format=lambda v: f"{v:,.3f}" if abs(v) < 100 else f"{v:,.0f}"))
    log(f"\ncomparison files in {folder}")
    progress.report("done")
    return res
