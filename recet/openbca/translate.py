"""A CET job (Measure.csv, ProgramCost.csv) as an OpenBCA job priced on the same ACC (step 3 of the OpenBCA module: comparisons C3 and C4).

    translate()   writes <out>/program_input.xlsx (OpenBCA's Program Input workbook), <out>/jurisdiction/ (ca_config's four tables) and
                  <out>/translation.csv (one row per measure: what was sent, and every known way OpenBCA will price it differently from ReCET)

Everything that decides a measure's electric value is taken from ReCET's own pricing path, so a difference in the comparison is a difference in how the two
tools calculate, not in what they were given:
    load shape        the measure's 8,760 profile as ReCET resolves it (real_impact_profiles.resolve_shape on the measure's route) and aligns it to the ACC's
                      weekdays (shift_for_year); one OpenBCA load shape per distinct (profile, IOU, sector, zone)
    avoided costs     the series ReCET prices that route on (real_avoided_cost_v2.get_acc_series, $/kWh, TRC rows), one subset per (IOU, zone) used, every year
                      a measure's life reaches; a year beyond the ACC takes the nearest ACC year, as ReCET does
    savings           Unit Quantity = NumUnits, Net-to-gross Ratio = NTGRkWh, Adj. Gross Annual Electric Savings = AC_Net_kWh_ScaledProfile_1st_Baseline /
                      (NumUnits x NTGRkWh): the product OpenBCA forms is ReCET's priced quantity
    discount rate     Measure-Specific Discount Rate = 4 x the PA's DiscountRateQtr (OpenBCA discounts quarterly at rate / 4, as CalcCE does)
    costs             Utility Incentive Upfront = UnitEndUserRebate + UnitIncentiveToOthers + UnitDirectInstallLab + UnitDirectInstallMat; Incremental Cost Upfront =
                      UnitMeaCost1stBaseline (UnitMeaCost2ndBaseline for an early replacement, CET's nominal measure cost); ProgramCost rows -> Program Admin
                      Costs (CET's ten summed fields, C11.1) in the row's year. Under OpenBCA's "CA TRC" treatment the measure costs are then NTG x measure
                      cost + (1 - NTG) x incentive, CET's TRC cost when NTGRCost = NTGRkWh and incentives do not exceed the measure cost; under "USI" (PAC) the
                      incentives.

Known differences, flagged per measure in translation.csv (`differences`), never "fixed" on either side:
    start_quarter     OpenBCA discounts from the measure's own start quarter, CalcCE from Q1 of the job's First Year: OpenBCA's benefit is higher by
                      (1 + r/4) ^ quarters-since-First-Year-Q1, and CET discounts the upfront costs that OpenBCA leaves undiscounted
    fractional_eul    OpenBCA casts EUL to INTEGER (DuckDB rounds); CalcCE prices the part quarter
    fractional_units  OpenBCA's lifecycle kWh casts Unit Quantity to INTEGER (values are not affected)
    ntg_cost          one NTG in OpenBCA (NTGRkWh sent); CET's costs use NTGRCost
    dual_baseline     no 2nd baseline in OpenBCA: the 1st-baseline savings run the whole EUL
    excess_incentive  CET adds NTGRCost x the incentives above the measure cost to TRC; OpenBCA has no such term
    gas               therms are not sent (C3 / C4 compare electric values and costs only)
    other             tax credits, refrigerant / misc / gas-infrastructure benefits and costs are not sent
    unpriced          ReCET found no profile: sent with no electric savings
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import ca_config, workbook

PROGRAM_COST_FIELDS = ["AdminCostsOverheadAndGA", "AdminCostsOther", "MarketingOutreach", "DIActivity", "DIInstallation", "DIHardwareAndMaterials",
                       "DIRebateAndInspection", "EMV", "UserInputIncentive", "CostsRecoveredFromOtherSources"]   # CET's program-cost sum (C11.1)
INCENTIVE_FIELDS = ["UnitEndUserRebate", "UnitIncentiveToOthers", "UnitDirectInstallLab", "UnitDirectInstallMat"]
# The flags that change a compared value (electric value or test cost) beyond the start-quarter factor; the rest (fractional_units, gas, other) touch only
# OpenBCA's lifecycle kWh or values that are not compared (C4, 2026-10-28: every measure without one of these within 5e-5 after the start-quarter factor).
VALUE_DIFFERENCES = {"fractional_eul", "ntg_cost", "dual_baseline", "excess_incentive", "unpriced"}
OTHER_FIELDS = ["UnitTaxCredits", "UnitRefrigCosts", "UnitRefrigBens", "UnitMiscCosts", "UnitMiscBens", "UnitGasInfraBens"]


@dataclass
class Translation:
    program_input: Path
    jurisdiction: Path
    table: pd.DataFrame          # translation.csv


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


def _routes(measures: pd.DataFrame) -> pd.DataFrame:
    """Each measure's 1st-baseline electric route as run_poc.run_real_hourly prices it: profile, IOU, sector and the zone it is priced in."""
    from ..pipeline import system_zones
    m = measures
    out = pd.DataFrame({"profile": m["AC_Elec_Impact_Profile"].astype(str).to_numpy(), "iou": m["IOU_AC_Territory"].astype(str).to_numpy(),
                        "sector": m["AC_Elec_Target_Sector"].astype(str).to_numpy()}, index=m.index)
    out["zone"] = [str(system_zones.priced_zone(k, iou, ts, cz)) for k, iou, ts, cz in
                   zip(m["AC_Elec_Impact_Profile"], m["IOU_AC_Territory"], m["AC_Elec_Target_Sector"], m["AC_Climate_Zone"])]
    return out


def _nearest(series: dict[int, np.ndarray], year: int) -> np.ndarray:
    return series[min(series, key=lambda y: abs(y - year))]


def translate(cfg, out_dir: str | Path, test: str = "TRC", log=lambda s: None) -> Translation:
    """`cfg` is the jobs.RunConfig of the ReCET run to compare with (its measures, programcost, ACC version and supplied files, reference folder,
    base_year): the pipeline is set up from it exactly as that run is (jobs.prepare). Statewide allocation and per-measure ACC versions are not supported."""
    from .. import jobs, run_poc
    from ..pipeline import discount_settings, real_avoided_cost_v2, real_impact_profiles
    if test not in ca_config.COST_TREATMENTS:
        raise ValueError(f"test must be one of {sorted(ca_config.COST_TREATMENTS)}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    acc, _, _ = jobs.prepare(cfg, out, log)
    acc_version = acc.settings_acc
    measures, program_costs = run_poc.load_job(cfg.measures, cfg.programcost)
    m = measures.reset_index(drop=True)
    if "ACCVersion" in m and m["ACCVersion"].astype(str).str.strip().replace("nan", "").ne("").any():
        raise ValueError("per-measure ACCVersion is not supported in an OpenBCA translation")
    first_year = int(cfg.base_year if cfg.base_year is not None else m["Used_Year"].min())
    routes = _routes(m)

    # --- load shapes and avoided-cost subsets, once per route / (IOU, zone)
    shapes: dict[str, np.ndarray] = {}
    shape_of_route: dict[tuple, str | None] = {}
    for key in dict.fromkeys(map(tuple, routes.to_numpy())):
        hit = real_impact_profiles.resolve_shape(*key)
        if hit is None:
            shape_of_route[key] = None
            continue
        name = f"S{len(shapes) + 1}"
        shapes[name] = real_impact_profiles.shift_for_year(np.asarray(hit[0], dtype=float), first_year, hit[1])
        shape_of_route[key] = name
    shape_name = [shape_of_route[k] for k in map(tuple, routes.to_numpy())]
    subset = [ca_config.subset_name(iou, f"CZ{z}") for iou, z in zip(routes["iou"], routes["zone"])]

    claim_year = m["Used_Year"].astype(int).to_numpy()
    claim_quarter = m["Used_Quarter"].astype(int).to_numpy()
    eul, rul = _num(m["EUL_Yrs"]), _num(m["RUL_Yrs"])
    units, ntg, ntg_cost = _num(m["NumUnits"]), _num(m["NTGRkWh"]), _num(m["NTGRCost"])
    priced_kwh = _num(m["AC_Net_kWh_ScaledProfile_1st_Baseline"])
    per_unit_gross = np.where((units * ntg) != 0, priced_kwh / (units * ntg).where((units * ntg) != 0, 1.0), 0.0)
    unpriced = np.array([s is None for s in shape_name])
    per_unit_gross[unpriced] = 0.0
    early = (rul > 0) & (rul < eul)
    incentive = sum(_num(m[c]) for c in INCENTIVE_FIELDS if c in m)
    measure_cost = _num(m["UnitMeaCost1stBaseline"]).where(~early, _num(m["UnitMeaCost2ndBaseline"]))
    rate = np.array([4.0 * discount_settings.get_discount_rate_qtr(iou, acc_version) for iou in m["IOU_AC_Territory"]])
    eul_int = eul.round().astype(int)   # what OpenBCA's ::INT cast makes of it

    subsets_needed = {}
    for s, iou, z, y, e in zip(subset, routes["iou"], routes["zone"], claim_year, eul_int):
        lo, hi = subsets_needed.get(s, (iou, z, y, y + e))[2:]
        subsets_needed[s] = (iou, z, min(lo, y), max(hi, y + e + 1))
    rows = []
    for s, (iou, z, lo, hi) in subsets_needed.items():
        series = real_avoided_cost_v2.get_acc_series(iou, z, "TRC")[0]
        for year in range(lo, hi + 1):
            rows.append(pd.DataFrame({"avoided_cost_subset": s, "year": year, "hour_of_year": np.arange(8760), "value": _nearest(series, year)}))
    acc = pd.concat(rows, ignore_index=True)
    hour = acc["hour_of_year"].to_numpy()
    month = np.searchsorted(np.cumsum([31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]) * 24, hour, side="right") + 1
    avoided = pd.DataFrame({"avoided_cost": ca_config.ELECTRIC_STREAM, "avoided_cost_subset": acc["avoided_cost_subset"],
                            "year": acc["year"].astype("int32"), "quarter": pd.array([pd.NA] * len(acc), dtype="Int32"), "month": month.astype("int32"),
                            "day_of_year": pd.array([pd.NA] * len(acc), dtype="Int32"), "hour_of_day": (hour % 24 + 1).astype("int32"),
                            "hour_of_year": hour.astype("int32"), "avoided_cost_value": acc["value"].astype(float)})
    jur = out / "jurisdiction"
    jur.mkdir(exist_ok=True)
    tables = {"avoided_costs_ts": avoided, "value_stream_groups": ca_config.value_stream_groups(),
              "global_parameters": ca_config.global_parameters(test, float(np.median(rate)), first_year),
              "first_year_avoided_costs_by_id": ca_config.first_year_avoided_costs_by_id()}
    for name, df in tables.items():
        df.to_parquet(jur / f"{name}.parquet", index=False)

    # --- the Program Input workbook
    ob = pd.DataFrame({
        "Unique ID": m["CEInputID"].astype(str), "Program Name": m["PrgID"].astype(str), "Measure ID": m["CEInputID"].astype(str),
        "Measure Name": m["MeasDescription"].astype(str) if "MeasDescription" in m else "", "Avoided Cost Subset ": subset,
        "Start Year": claim_year, "Start Quarter": claim_quarter, "Measure-Specific Discount Rate": rate, "Unit Quantity": units.to_numpy(),
        "Estimated Useful Life (Years)": eul.to_numpy(), "Net-to-gross Ratio": ntg.to_numpy(), "Utility Incentive Upfront  ($)": incentive.to_numpy(),
        "Incremental Cost Upfront  ($)": measure_cost.to_numpy(),
        "Electric Savings Load Shape": [s or next(iter(shapes), "S1") for s in shape_name], "Adj. Gross Annual Electric Savings (kWh)": per_unit_gross,
    })
    programs = None
    if program_costs is not None and len(program_costs):
        pc = program_costs.copy()
        pc["Program Admin Costs ($)"] = sum(_num(pc[c]) for c in PROGRAM_COST_FIELDS if c in pc)
        pc["Program Year"] = pc["Used_Year"].astype(int)
        pc["Program Name"] = pc["PrgID"].astype(str)
        programs = pc.groupby(["Program Name", "Program Year"], as_index=False)["Program Admin Costs ($)"].sum()
    if not shapes:
        shapes = {"S1": np.full(8760, 1 / 8760)}
    path = workbook.write_program_input(out / "program_input.xlsx", ob, programs, shapes)

    # --- what was sent and how OpenBCA will differ
    other = sum((_num(m[c]) != 0) for c in OTHER_FIELDS if c in m)
    therms = sum((_num(m[c]) != 0) for c in ("UnitTherm1stBaseline", "UnitTherm2ndBaseline") if c in m)
    flags = {
        "start_quarter": (claim_year - first_year) * 4 + claim_quarter - 1 != 0,
        "fractional_eul": (eul - eul.round()).abs() > 1e-9,
        "fractional_units": (units - units.round()).abs() > 1e-9,
        "ntg_cost": (ntg_cost - ntg).abs() > 1e-9,
        "dual_baseline": early.to_numpy(),
        "excess_incentive": (sum(_num(m[c]) for c in INCENTIVE_FIELDS[1:] if c in m) - measure_cost) > 1e-9,   # CET's IncentsDI: rebates excluded
        "gas": therms > 0,
        "other": other > 0,
        "unpriced": unpriced,
    }
    table = ob[["Unique ID", "Program Name", "Avoided Cost Subset ", "Start Year", "Start Quarter", "Electric Savings Load Shape"]].rename(
        columns={"Unique ID": "CEInputID", "Program Name": "PrgID", "Avoided Cost Subset ": "subset", "Electric Savings Load Shape": "load_shape"})
    table["profile"] = routes["profile"].to_numpy()
    table["quarters_after_first_year_q1"] = (claim_year - first_year) * 4 + claim_quarter - 1
    table["discount_rate"] = rate
    table["differences"] = [", ".join(k for k, v in flags.items() if np.asarray(v)[i]) for i in range(len(m))]
    table.to_csv(out / "translation.csv", index=False)
    return Translation(program_input=path, jurisdiction=jur, table=table)


def matched(table: pd.DataFrame) -> pd.Series:
    """Measures with no known difference: OpenBCA and ReCET should agree on them to rounding (C3)."""
    return table["differences"].fillna("") == ""


def compare_with_recet(recet_measures: pd.DataFrame, openbca_summary: pd.DataFrame, test: str, table: pd.DataFrame) -> pd.DataFrame:
    """One row per measure: ReCET's net electric value and its test cost without program costs (both less the supply costs, which OpenBCA books as negative
    benefits) beside OpenBCA's own `ELECTRIC Total ($)` and `-total_costs` from results_summary_by_id (read, never recomputed), their ratios, the
    start-quarter factor (1 + r/4) ^ quarters (OpenBCA discounts from the measure's start quarter) and each ratio divided by it (`*_after_start_quarter`:
    1 to rounding wherever no other difference is flagged)."""
    r = recet_measures.copy()
    r["CEInputID"] = r["CEInputID"].astype(str)
    sup = sum(_num(r[c]) for c in ("ElecSupplyCost", "GasSupplyCost", "WaterEnergyCost", "OtherCost") if c in r)
    r["recet_electric"] = _num(r["ElecBen"]) - _num(r["ElecSupplyCost"])
    r["recet_cost"] = _num(r[f"{test}CostNoAdmin"]) - sup
    o = openbca_summary[openbca_summary["type"] == "Measure"].copy()
    o["CEInputID"] = o["id"].astype(str)
    o["openbca_electric"] = _num(o["ELECTRIC Total ($)"]) if "ELECTRIC Total ($)" in o else 0.0
    o["openbca_cost"] = -_num(o["total_costs"])
    both = r[["CEInputID", "PrgID", "recet_electric", "recet_cost"]].merge(o[["CEInputID", "openbca_electric", "openbca_cost"]], on="CEInputID", how="outer")
    t = table.astype({"CEInputID": str})[["CEInputID", "differences", "quarters_after_first_year_q1", "discount_rate"]]
    both = both.merge(t, on="CEInputID", how="left")
    both["start_quarter_factor"] = (1.0 + both["discount_rate"] / 4.0) ** both["quarters_after_first_year_q1"]
    for k in ("electric", "cost"):
        both[f"{k}_ratio"] = both[f"openbca_{k}"] / both[f"recet_{k}"].where(both[f"recet_{k}"].abs() > 1e-9)
        both[f"{k}_ratio_after_start_quarter"] = both[f"{k}_ratio"] / both["start_quarter_factor"]
    both["differences"] = both["differences"].astype(object).fillna("")
    return both


def summarize(compare: pd.DataFrame, recet_measures: pd.DataFrame, openbca_jst: pd.DataFrame, openbca_summary: pd.DataFrame, test: str) -> dict:
    """Portfolio figures for one test: both tools' sums, OpenBCA's own jst_ratio beside ReCET's ratio on the same terms (supply costs as negative benefits,
    gas left out), and how far the measures with no VALUE_DIFFERENCES flag are apart once the start quarter is allowed for."""
    r = recet_measures
    sup = sum(_num(r[c]).sum() for c in ("ElecSupplyCost", "GasSupplyCost", "WaterEnergyCost", "OtherCost") if c in r)
    program = float((_num(r[f"{test}Cost"]) - _num(r[f"{test}CostNoAdmin"])).sum())
    elec = float((_num(r["ElecBen"]) - _num(r["ElecSupplyCost"])).sum())
    clean = ~compare["differences"].str.split(", ").apply(lambda flags: bool(set(flags) & VALUE_DIFFERENCES))
    dev = lambda col: float((compare.loc[clean, col] - 1).abs().max()) if compare.loc[clean, col].notna().any() else float("nan")
    prog = openbca_summary[openbca_summary["type"] == "Program"]
    return {
        "test": test, "measures": len(compare), "measures_without_value_differences": int(clean.sum()),
        "recet_electric": elec, "openbca_electric": float(compare["openbca_electric"].sum()),
        "recet_measure_costs": float(compare["recet_cost"].sum()), "openbca_measure_costs": float(compare["openbca_cost"].sum()),
        "recet_program_costs": program, "openbca_program_costs": float(-_num(prog["total_costs"]).sum()),
        "recet_ratio_on_openbca_terms": elec / (float(compare["recet_cost"].sum()) + program) if (compare["recet_cost"].sum() + program) else float("nan"),
        "recet_ratio": float(_num(r[f"{test}Ben"]).sum() / _num(r[f"{test}Cost"]).sum()) if _num(r[f"{test}Cost"]).sum() else float("nan"),
        "recet_gas_net_not_sent": float((_num(r["GasBen"]) - _num(r["GasSupplyCost"])).sum()),
        "openbca_jst_ratio": float(openbca_jst["jst_ratio"].iloc[0]),
        "max_electric_deviation_after_start_quarter": dev("electric_ratio_after_start_quarter"),
        "max_cost_deviation_after_start_quarter": dev("cost_ratio_after_start_quarter"),
    }


def compare_job(cfg, out_dir: str | Path, checkout=None, log=print, tests=("TRC", "PAC")) -> pd.DataFrame:
    """Comparisons C3 / C4 on one CET job: a ReCET run of `cfg` (no societal tests or emissions; parquet), then for each test a translation and an
    OpenBCA run, compared measure by measure (<out>/compare_<test>.csv) and in total (<out>/summary.csv, returned)."""
    import dataclasses
    from .. import jobs
    from . import run
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    rcfg = dataclasses.replace(cfg, out_dir=str(out / "recet"), run_name="recet", include_societal=False, emissions=False, output_format="parquet",
                               output_template=[], chunk_size=0)
    res = jobs.run_job(rcfg, log=lambda s: None)
    recet_measures = res.frame
    rows = []
    for test in tests:
        t = translate(dataclasses.replace(cfg), out / f"openbca_{test}", test=test)
        ob = run.run_openbca(t.program_input, None, out / f"openbca_{test}", "run", checkout=checkout, jurisdiction=t.jurisdiction, log=lambda s: None)
        summary = pd.read_parquet(ob.run_dir / "openbca" / "export" / "openbca_results_summary_by_id.parquet")
        jst = pd.read_parquet(ob.run_dir / "openbca" / "export" / "openbca_jst_ratio.parquet")
        c = compare_with_recet(recet_measures, summary, test, t.table)
        c.to_csv(out / f"compare_{test}.csv", index=False)
        rows.append(summarize(c, recet_measures, jst, summary, test))
        log(f"{test}: OpenBCA jst_ratio {rows[-1]['openbca_jst_ratio']:.6f}, ReCET on the same terms {rows[-1]['recet_ratio_on_openbca_terms']:.6f}; "
            f"OpenBCA run {ob.seconds:.0f} s")
    s = pd.DataFrame(rows)
    s.to_csv(out / "summary.csv", index=False)
    return s
