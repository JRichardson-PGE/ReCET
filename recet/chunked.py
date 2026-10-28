"""Running a large job in parts, so that the run holds about one part's worth of data at a time whatever the job's size.

jobs.run_job switches to this when Measure.csv has more rows than RunConfig.chunk_size (default CHUNK_SIZE). The file is first split into parts of that many
rows (delimited.split_file) in the run folder's `_parts` folder, with the column types the whole file would have (delimited.part_dtypes), and preflight checks
the parts (preflight.check_inputs). Then two passes over the parts:

  pass 1, per part: load its measures; price every measure-level value (run_poc.price_measure_values: benefits, supply costs, participant and bill-impact PVs,
          the societal variants, discounted savings), the emissions and (components mode) the ACC components; write them to `_parts/values`; add the part's
          program totals (run_poc.program_share_totals) to the job's; and in combined-8760 mode take the part's hourly profile.
  pass 2, per part: load the measures again and read their values back; finish the cost tests with the WHOLE job's program totals (run_poc.apply_program_costs
          with `totals`), so each measure gets the share of its program's costs it gets in the whole job; assemble the part's measure-level rows and append them
          to the output file (outputs.TableWriter); add them to the portfolio summary (outputs.SummaryParts) and the CET comparison.

Every value equals the one a whole-job run gives, to rounding: the program totals and the summary sums are added in a different order. The measure-level table
is only written to its file (RunResult.frame is None). The `_parts` folder is deleted at the end.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import pandas as pd

CHUNK_SIZE = 50_000          # measures per part (0.1.56, as the web server ran; was 20,000); a job with more rows than this is run in parts (RunConfig.chunk_size; 0 = never)

Log = Callable[[str], None]


def acc_groups(acc, versions_in_file: set) -> dict:
    """ACC version key -> resolved ACC for every version the job's measures name, the run's own first (as run_job orders them)."""
    from . import versions
    from .pipeline import data_loading
    named = {k for k in data_loading.acc_versions(pd.Series(sorted(versions_in_file, key=str), dtype=object)) if k} if versions_in_file else set()
    keys = {acc.key} | named
    return {k: (acc if k == acc.key else versions.resolve_acc(k)) for k in sorted(keys, key=lambda k: (k != acc.key, k))}


def run_parts(cfg, acc, run_dir: Path, result, parts: list, dtypes: dict, values: dict, log: Log, t_start: float):
    """Price and write the job in `parts` (see the module docstring). `values`: the distinct ClaimYearQuarter and ACCVersion values of the whole file
    (delimited.part_dtypes). Fills and returns `result`."""
    from . import components as comp_mod, jobs, outputs, profile8760, progress, results_view, run_poc, runtime
    from .pipeline import cost_tests, emissions

    tag = f"acc{acc.key}"
    excel = jobs.excel_limit(cfg, result, log)
    spill = run_dir / "_parts" / "values"
    spill.mkdir(parents=True, exist_ok=True)
    base_year = cfg.base_year or min(int(str(v)[:4]) for v in values["ClaimYearQuarter"])
    meb = float(cfg.market_effects or 0.0)
    groups = acc_groups(acc, values.get("ACCVersion", set()))
    log(f"{sum(n for _f, _s, n in parts):,} rows in {len(parts)} parts of up to {cfg.chunk_size:,}, First Year {base_year}, "
        f"market effects {meb:.0%} on benefits and costs")
    if len(groups) > 1:
        log("measures name these ACC versions: " + ", ".join(g.label for g in groups.values()) + f"; program costs are discounted at the {acc.label}'s rates")
        if cfg.acc_electric_file or cfg.acc_gas_file:
            log(f"note: the supplied ACC file(s) replace the {acc.label} only; measures on other versions use those versions' built-in files")
    societal = jobs.societal_available(groups, acc, cfg.include_societal, result, log)
    if societal != cfg.include_societal:
        progress.replan(societal=societal)                       # an ACC with no societal avoided costs: pricing is shorter
    program = cost_tests.load_program_costs(Path(cfg.programcost), allocate_statewide=cfg.allocate_statewide) if cfg.programcost else None
    years = (cfg.profile_years or [base_year]) if cfg.portfolio_8760 else []

    def load(f):
        measures, _ = run_poc.load_job(f, None, allocate_statewide=cfg.allocate_statewide, dtypes=dtypes)
        measures["Used_ACCVersion_Key"] = measures["ACCVersion"].replace("", acc.key)    # the ACC each measure is priced on (blank = this run's)
        return measures

    # ---- pass 1: every measure-level value, part by part
    log("\n== pricing (TRC" + (", SCB, SCH" if societal else "") + f", PAC, RIM, bill impacts) in {len(parts)} parts ==")
    t0 = time.time()
    progress.report("pricing", 0, len(parts))
    totals, n_measures = None, 0
    comp_checks, profiles = [], []
    for i, (f, start, n) in enumerate(parts):
        tp = time.time()
        measures = load(f)
        vals, ems, comps = [], [], []
        for k, g in groups.items():
            sub = measures[measures["Used_ACCVersion_Key"] == k].reset_index(drop=True) if len(groups) > 1 else measures
            if sub.empty:
                continue
            if g is not acc:
                runtime.switch_acc(g)
            vals.append(run_poc.price_measure_values(sub, acc_version=g.settings_acc, base_year=base_year, market_effects_bens=meb, market_effects_cost=meb,
                                                     include_societal=societal))
            if cfg.emissions:
                ems.append(emissions.price_emissions(sub, acc_version=g.settings_acc, market_effects_bens=meb))
            if cfg.components:
                comps.append(comp_mod.price_components(sub, acc_version=g.settings_acc, base_year=base_year, market_effects_bens=meb, log=lambda s: None))
        if len(groups) > 1:
            runtime.switch_acc(acc)
        d = pd.concat(vals, ignore_index=True) if len(vals) > 1 else vals[0]
        totals = cost_tests.combine_share_totals(totals, run_poc.program_share_totals(d))
        d.to_parquet(spill / f"values_{i:04d}.parquet", index=False)
        if ems:
            pd.concat(ems, ignore_index=True).to_parquet(spill / f"emissions_{i:04d}.parquet", index=False)
        if comps:
            c = pd.concat(comps, ignore_index=True)
            c.to_parquet(spill / f"components_{i:04d}.parquet", index=False)
            comp_checks.append((d[["CEInputID", "ElecBen", "ElecSupplyCost", "GasBen", "GasSupplyCost"]], c))
        if years:
            profiles.append(profile8760.portfolio_profile(measures, years, log=lambda s: None))
        n_measures += len(measures)
        log(f"  part {i + 1}/{len(parts)}: rows {start + 1:,}-{start + n:,}, {len(measures):,} measures, {time.time() - tp:.0f}s")
        progress.report("pricing", i + 1, len(parts))
        del measures, d, vals, ems, comps
    log(f"priced in {time.time() - t0:.0f}s")
    used = jobs.run_format(cfg, n_measures, log)                 # before any table is written: an Excel run over the row limit writes every table as zipped CSV
    if used != cfg.output_format:
        result.notes.append(f"Excel output was chosen, but {n_measures:,} measures is over the {cfg.excel_max_rows:,}-row limit, so every table is "
                            f"{dict(csv='CSV', parquet='parquet').get(used, 'zipped CSV')}")
        cfg.output_format = used

    if cfg.components:
        log("\n== ACC components (each priced separately) ==")
        res_c = pd.concat([r for r, _c in comp_checks], ignore_index=True)
        comps_all = pd.concat([c for _r, c in comp_checks], ignore_index=True)
        chk = comp_mod.check_sums(res_c, comps_all)
        for fuel, r in chk.items():
            log(f"  {fuel}: {r['components']} components; sum of components vs total: portfolio {r['portfolio_sum_of_components']:,.0f} vs {r['portfolio_total']:,.0f}, "
                f"worst measure residual {r['max_abs_residual']:.3g} ({r['measures_over_tolerance']} measure(s) over tolerance)")
        table = comp_mod.portfolio_table(comps_all)
        for fuel, r in chk.items():
            table.loc[len(table)] = {"fuel": "Electric" if fuel == "Elec" else "Gas", "component": "(check) total minus sum of components",
                                     "portfolio_value_usd": r["portfolio_total"] - r["portfolio_sum_of_components"], "share_of_fuel_total": None}
        result.files["components_summary"] = outputs.write_table(table, run_dir / f"components_summary_{tag}", cfg.output_format, excel)
        del res_c, comps_all, comp_checks

    # ---- pass 2: program costs shared over the whole job, then the measure-level rows, part by part
    log("\n== writing results ==")
    progress.report("writing", 0, len(parts))                       # the parts; then the "file" stage finishes the measure-level file
    tw = time.time()
    writer = outputs.TableWriter(run_dir / f"measure_level_{tag}", cfg.output_format, n_measures, cfg.parquet_row_group, keep_parquet=True, excel=excel,
                                 select=cfg.output_template or None)
    summary_parts = outputs.SummaryParts(cfg.apply_exclude_from_ce)
    from . import key_metrics
    km_parts = []                                                   # the key-metrics sums, a few rows a part (key_metrics.py)
    result_sums, result_columns = [], None
    cet = outputs.read_cet_outputs(cfg.cet_outputs) if cfg.cet_outputs else None
    cet_writer = outputs.TableWriter(run_dir / f"cet_comparison_measures_{tag}", cfg.output_format, n_measures, cfg.parquet_row_group, excel=excel) if cet is not None else None
    cet_sums: dict[str, tuple[float, float]] = {}
    matched = 0
    labels = {k: g.label for k, g in groups.items()}
    for i, (f, _start, _n) in enumerate(parts):
        measures = load(f)
        res = run_poc.apply_program_costs(pd.read_parquet(spill / f"values_{i:04d}.parquet"), measures, program, acc_version=acc.settings_acc,
                                          base_year=base_year, market_effects_bens=meb, include_societal=societal, totals=totals)
        if cfg.emissions:
            res = res.merge(pd.read_parquet(spill / f"emissions_{i:04d}.parquet"), on="CEInputID", how="left")
        frame = outputs.assemble_measure_level(outputs.read_raw(f, cfg.allocate_statewide), measures, res,
                                               measures["Used_ACCVersion_Key"].map(labels) if len(groups) > 1 else acc.label,
                                               credited=outputs.credited_pa(measures, cfg.programcost, cfg.allocate_statewide))
        if cfg.components:
            frame = frame.merge(pd.read_parquet(spill / f"components_{i:04d}.parquet"), on="CEInputID", how="left")
            frame = frame[[c for c in frame.columns if c != "CreditedPA"] + ["CreditedPA"]]
        frame = outputs.attach_programs(frame, measures["PrgID"])
        writer.write(frame)
        summary_parts.add(frame)
        km_parts.append(key_metrics.sums(frame))
        result_sums.append(results_view.group_sums(frame))           # the Results page's sums, a few hundred rows a part (results_view)
        result_columns = result_columns or list(frame.columns)
        if cet is not None:
            per, sums, m = outputs.compare_to_cet_part(frame, cet)
            cet_writer.write(per)
            matched += m
            for c, (a, b) in sums.items():
                pa, pb = cet_sums.get(c, (0.0, 0.0))
                cet_sums[c] = (pa + a, pb + b)
        del measures, res, frame
        progress.report("writing", i + 1, len(parts))
    tc = time.time()
    log(f"  program costs shared and {len(parts)} parts assembled in {tc - tw:.0f}s")
    progress.report("file", 0, 1)
    result.files["measure_level"] = writer.close(lambda j, c: progress.report("file", progress.file_share(j, c, writer.fmt), 1))
    ts = time.time()
    log(f"  measure-level table finished ({cfg.output_format}) in {ts - tc:.0f}s")
    jobs.write_template_files(cfg, writer, key_metrics.combine(km_parts), run_dir, tag, excel, result, log)
    summary = summary_parts.result()
    result.files["portfolio_summary"] = outputs.write_table(summary, run_dir / f"portfolio_summary_{tag}", cfg.output_format, excel)
    log(f"  portfolio summary in {time.time() - ts:.0f}s")
    result.totals = {k: float(v) for k, v in summary.iloc[0].items() if isinstance(v, (int, float)) and k != "measures"}
    result.frame = None
    result.notes.append(f"run in {len(parts)} parts of up to {cfg.chunk_size:,} measures: the measure-level table is in its file only")

    if years:
        log(f"\n== combined 8,760 electric profile, year(s) {years} ==")
        prof, unshaped, psum = profile8760.combine_profiles(profiles, years)
        if len(unshaped):
            log(f"  {len(unshaped)} measure(s) have no 8,760 shape and are left out of the profile (listed in the unshaped table)")
        result.files["portfolio_8760"] = outputs.write_table(prof, run_dir / f"portfolio_8760_{tag}", cfg.output_format, excel)
        if len(unshaped):
            result.files["portfolio_8760_unshaped"] = outputs.write_table(unshaped, run_dir / f"portfolio_8760_unshaped_{tag}", cfg.output_format, excel)
        for y, s in psum.items():
            log(f"  {y}: {s['in_profile_kwh']:,.0f} net kWh in the profile ({s['unshaped_kwh']:,.0f} kWh of unshaped measures left out); "
                f"peak hour {s['peak_hour_of_year']} at {s['peak_hour_kwh']:,.0f} kWh")

    if cet is not None:
        log(f"\n== comparison with CET outputs {cfg.cet_outputs} ==")
        result.files["cet_comparison_measures"] = cet_writer.close()
        csum = outputs.cet_comparison_summary(cet_sums, matched, n_measures, len(cet))
        result.files["cet_comparison_summary"] = outputs.write_table(csum, run_dir / f"cet_comparison_summary_{tag}", cfg.output_format, excel)
        log(csum.attrs.get("matched", ""))
        log(csum.to_string(index=False, float_format=lambda v: f"{v:,.4f}" if abs(v) < 100 else f"{v:,.0f}"))

    jobs.write_settings(cfg, acc, run_dir, result, t_start, n_measures, base_year, parts={"chunk_size": cfg.chunk_size, "count": len(parts)})
    progress.report("finishing")
    results_view.keep_group_sums(run_dir, sums=results_view.combine_parts(result_sums, result_columns))   # added up part by part above: no read-back
    log(f"\ndone in {time.time() - t_start:.0f}s -- results in {run_dir}")
    return result
