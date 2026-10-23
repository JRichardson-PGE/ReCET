"""The documented formulas (docs/CALCULATIONS.md) against the tool.

`reference_calc.py` re-implements every formula in plain loops; here a synthetic three-measure job is priced by the tool and by that reference, and every documented
output has to agree. The job is built to exercise the branches the documentation describes: an hourly profile on a non-default calendar, year-varying and
cost-test-varying avoided costs, ACC years that run out before the measure life does, a dual-baseline measure with a fractional EUL and a mid-year claim, a fuel-
substitution measure (supply costs, negative water use), incentives above measure cost (excess incentives), program costs shared across three measures, statewide-free
market effects on benefits and costs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import reference_calc as ref
from recet import templates, versions
from recet.jobs import RunConfig, run_job
from recet.pipeline.engine import price_bill_impact

E3_VERSION = 2027   # the raw CET export rows (Version 2027) the 2026 ACC's settings come from; reference_calc reads them directly, so this also checks
                    # the tool's ACC -> CET version mapping
assert versions.CET_VERSION_TO_ACC[E3_VERSION] == versions.BUILTIN["2026"].acc
YEARS = (2028, 2029)                                           # shorter than the longest measure life on purpose: later years use the last one
I = np.arange(8760)
HOD, DOY = I % 24, I // 24
COMPONENTS = ("energy", "generation_capacity", "avoided_as_procurement", "losses", "methane_leakage", "ghg_cap_and_invest", "ghg_adder", "air_quality_adder",
              "transmission", "distribution")
GHG_MULT = {"TRC": 1.0, "SCT Base": 1.5, "SCT High": 2.5}       # the societal rows differ from TRC in the GHG term
EU_COM = "1 = Commercial Indoor Lighting"                       # has Emissions-table rows (PGE, COMMERCIAL, CZ13) so electric NOx / PM10 are non-zero
EU_RES = "TEST_RES_SHAPE"                                       # has none: NOx = PM10 = 0
REPRESENTS = 2012


def _components(year: int, mult: float) -> dict[str, np.ndarray]:
    peak = ((HOD >= 16) & (HOD <= 20)).astype(float)
    return {"energy": (40 + 3 * (year - 2027)) * (1 + 0.6 * peak), "generation_capacity": 15 * peak * ((DOY >= 150) & (DOY < 270)),
            "avoided_as_procurement": 0.5 + 0 * I, "losses": 1.0 + 0.5 * peak, "methane_leakage": 0.8 + 0 * I, "ghg_cap_and_invest": 6 + 0.01 * DOY,
            "ghg_adder": mult * (10 + 2 * (year - 2027)) * (1 + 0.2 * (HOD < 6)), "air_quality_adder": 0.3 + 0 * I, "transmission": 4 + 0 * I,
            "distribution": 6 * (HOD >= 14)}


def _emission_rate(year: int) -> np.ndarray:
    return 0.0002 * (1 + 0.5 * (HOD >= 17)) * (1 - 0.03 * (year - 2027))


def make_acc(path):
    frames = []
    for test, mult in GHG_MULT.items():
        for y in YEARS:
            c = _components(y, mult)
            df = pd.DataFrame({"iou": "PG&E", "climate_zone": "CZ13", "cost_test": test, "year": y, "hour_of_year": I + 1, **c})
            df["total_avoided_cost"] = sum(c.values())
            df["emission_rate_tonnes_per_kwh"] = _emission_rate(y)
            frames.append(df)
    pd.concat(frames, ignore_index=True).to_parquet(path, index=False)


def elec_acc(test: str, gen_only: bool = False) -> dict[int, np.ndarray]:
    out = {}
    for y in YEARS:
        c = _components(y, GHG_MULT[test])
        out[y] = (sum(c.values()) - (c["transmission"] + c["distribution"] if gen_only else 0)) / 1000.0
    return out


GAS_MONTH = np.arange(1, 13)


def _gas_parts(year: int, scc: str | None):
    mult = {None: 1.0, "Base": 2.0, "High": 3.0}[scc]
    return {"market": 0.5 + 0.02 * GAS_MONTH + 0.03 * (year - 2027), "td": np.where((GAS_MONTH <= 3) | (GAS_MONTH >= 11), 0.3, 0.0), "nox": 0.2, "co2": mult * 0.7,
            "upstream_methane": 0.04, "behind_meter_methane": 0.03, "air_quality_adder": 0.01}


def make_gas_acc(path):
    rows = []
    for gas_class in ("Commercial", "Residential"):
        for test, scc in (("TRC", None), ("SCT", "Base"), ("SCT", "High")):
            for y in YEARS:
                p = _gas_parts(y, scc)
                row = {"utility": "PG&E", "class": gas_class, "cost_test": test, "scc": scc if scc else "", "year": y, "nox": p["nox"], "co2": p["co2"],
                       "upstream_methane": p["upstream_methane"], "behind_meter_methane": p["behind_meter_methane"], "air_quality_adder": p["air_quality_adder"]}
                for m, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")):
                    row[f"market_{name}"], row[f"td_{name}"] = p["market"][m], p["td"][m]
                rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)


def gas_acc(scc: str | None, with_btm: bool) -> dict[int, np.ndarray]:
    out = {}
    for y in YEARS:
        p = _gas_parts(y, scc)
        out[y] = p["market"] + p["td"] + p["nox"] + p["co2"] + p["upstream_methane"] + p["air_quality_adder"] + (p["behind_meter_methane"] if with_btm else 0.0)
    return out


def _shape(kind: str) -> np.ndarray:
    w = 1 + 0.5 * np.sin(2 * np.pi * I / 24) + (0.3 * np.sin(2 * np.pi * I / 8760) if kind == "com" else 0.2 * np.cos(2 * np.pi * I / 168))
    return w / w.sum()


def make_profiles(path):
    frames = []
    for eu, sector, kind in ((EU_COM, "Commercial", "com"), (EU_RES, "Residential", "res")):
        frames.append(pd.DataFrame({"end_use_name": eu, "pa": "PGE", "target_sector": sector, "climate_zone": "ALL", "hour_of_year": I + 1, "impact": _shape(kind),
                                    "represents_year": REPRESENTS}))
    pd.concat(frames, ignore_index=True).to_csv(path, index=False)


MEASURES = {
    "EX1": dict(PrgID="PRG1", ClaimYearQuarter="2028Q3", Sector="Com", AC_Climate_Zone="13", AC_Gas_Impact_Profile="Winter Only", AC_Gas_Target_Sector="Commercial",
                AC_Elec_Impact_Profile=EU_COM, AC_Elec_Target_Sector="Commercial", IOU_AC_Territory="PGE", EUL_Yrs="2.4", RUL_Yrs="1", NumUnits="10",
                UnitkWh1stBaseline="1000", UnitkWh2ndBaseline="600", UnitkW1stBaseline="0.5", UnitkW2ndBaseline="0.3", UnitTherm1stBaseline="50", UnitTherm2ndBaseline="30",
                UnitkWhIOUWater1stBaseline="4", UnitkWhIOUWater2ndBaseline="2.5", NTGRkWh="0.8", NTGRkW="0.8", NTGRTherm="0.9", NTGRCost="0.9",
                InstallationRatekWh="0.95", RealizationRatekWh="0.9", InstallationRateTherm="1", RealizationRateTherm="0.9",
                UnitMeaCost1stBaseline="300", UnitMeaCost2ndBaseline="180", UnitIncentiveToOthers="20", UnitEndUserRebate="40", UnitDirectInstallLab="10",
                UnitDirectInstallMat="5", MeasInflation="0.02", UnitGasInfraBens="2.5", UnitRefrigCosts="2", UnitRefrigBens="1.5", UnitMiscCosts="3", UnitMiscBens="0.5",
                UnitTaxCredits="7", CombustionType="Residential Furnaces (<0.3):Uncontrolled", MeasDescription="dual baseline, fractional life, mid-year claim"),
    "EX2": dict(PrgID="PRG1", ClaimYearQuarter="2028Q3", Sector="Res", AC_Climate_Zone="13", AC_Gas_Impact_Profile="Winter Only-FuelSub-ToElec", AC_Gas_Target_Sector="Residential",
                AC_Elec_Impact_Profile=EU_RES, AC_Elec_Target_Sector="Residential", IOU_AC_Territory="PGE", EUL_Yrs="3", RUL_Yrs="0", NumUnits="5",
                UnitkWh1stBaseline="-1500", UnitkWh2ndBaseline="0", UnitTherm1stBaseline="120", UnitTherm2ndBaseline="0", UnitkWhIOUWater1stBaseline="-3",
                UnitkWhIOUWater2ndBaseline="0", NTGRkWh="0.75", NTGRkW="0.75", NTGRTherm="0.85", NTGRCost="1", UnitMeaCost1stBaseline="800",
                UnitMeaCost2ndBaseline="0", UnitIncentiveToOthers="150", UnitEndUserRebate="150", FuelID="FuelSub-ToElec", MeasImpactType="Deem-FuelSub",
                UnitRefrigCosts="4", MeasDescription="fuel substitution: added electric load and water use, saved gas"),
    "EX3": dict(PrgID="PRG1", ClaimYearQuarter="2028Q3", Sector="Res", AC_Climate_Zone="13", AC_Gas_Target_Sector="Residential", AC_Elec_Impact_Profile=EU_RES,
                AC_Elec_Target_Sector="Residential", IOU_AC_Territory="PGE", EUL_Yrs="4.25", RUL_Yrs="0", NumUnits="20", UnitkWh1stBaseline="100", NTGRkWh="1",
                NTGRkW="1", NTGRCost="1", UnitMeaCost1stBaseline="50", UnitIncentiveToOthers="60", UnitEndUserRebate="30",
                MeasDescription="incentives above measure cost (excess incentives)"),
}
PROGRAM_ROWS = [
    dict(PrgID="PRG1", PA="PGE", PrgYear="2028", ClaimYearQuarter="2028Q1", AdminCostsOverheadAndGA="1000", MarketingOutreach="250", EMV="100", UserInputIncentive="50"),
    dict(PrgID="PRG1", PA="PGE", PrgYear="2029", ClaimYearQuarter="2029Q1", AdminCostsOverheadAndGA="800", AdminCostsOther="120", DIActivity="60",
         CostsRecoveredFromOtherSources="-40"),
]
PROGRAM_HEADER = ["PrgID", "PA", "PrgYear", "ClaimYearQuarter", "AdminCostsOverheadAndGA", "AdminCostsOther", "MarketingOutreach", "DIActivity", "DIInstallation",
                  "DIHardwareAndMaterials", "DIRebateAndInspection", "EMV", "UserInputIncentive", "CostsRecoveredFromOtherSources", "OnBillFinancing"]


def write_measures(path, measures: dict[str, dict]):
    lines = ["|".join(templates._CET_MEASURE_HEADER)]
    base = {"NTGRkW": "1", "NTGRTherm": "", "NTGRCost": "", "RealizationRatekW": "1", "RealizationRatekWh": "1", "RealizationRateTherm": "1", "InstallationRatekW": "1",
            "InstallationRatekWh": "1", "InstallationRateTherm": "1", "MeasInflation": "0", "UnitTherm1stBaseline": "0", "UnitTherm2ndBaseline": "0",
            "UnitkW1stBaseline": "0", "UnitkW2ndBaseline": "0", "UnitkWh2ndBaseline": "0"}
    for cid, fields in measures.items():
        row = {c: "" for c in templates._CET_MEASURE_HEADER}
        row.update(base)
        row.update(fields)
        row["CEInputID"] = cid
        lines.append("|".join(row[c] for c in templates._CET_MEASURE_HEADER))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_program(path):
    lines = ["|".join(PROGRAM_HEADER)] + ["|".join(r.get(c, "") for c in PROGRAM_HEADER) for r in PROGRAM_ROWS]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------------------------------------- the reference run
def reference_outputs(meb: float, mec: float) -> dict[str, dict]:
    st, first_year = ref.settings("PGE", E3_VERSION), 2028
    program = pd.DataFrame(PROGRAM_ROWS)
    for c in ref.COST_FIELDS:
        program[c] = pd.to_numeric(program[c]) if c in program else np.nan
    program["Year"] = program["ClaimYearQuarter"].str[:4].astype(int)
    gas_shares = pd.read_csv(ref.paths.package_data("gas_savings_profiles.csv"))
    rows, per = [], {}
    for cid, raw in MEASURES.items():
        d = ref.derive(raw)
        shape = _shape("com" if raw["AC_Elec_Impact_Profile"] == EU_COM else "res")
        w1 = d["w1"]
        e = {}
        sfx = {"": ("TRC", None, st["qtr"], st["annual"]), "_SB": ("SCT Base", "Base", st["qtr_sct"], st["annual_sct"]), "_SH": ("SCT High", "High", st["qtr_sct"], st["annual_sct"])}
        has_gas = d["therms_1st"] != 0 or d["therms_2nd"] != 0
        for s, (test, scc, q, _a) in sfx.items():
            v_e = ref.elec_value(d, shape, REPRESENTS, elec_acc(test), q, first_year, d["shape_scale_1st"], d["shape_scale_2nd"], meb)
            v_w = ref.elec_value(d, shape, REPRESENTS, elec_acc(test, gen_only=True), q, first_year, d["water_scale_1st"], d["water_scale_2nd"], meb)
            v_g = 0.0
            if has_gas:
                gshare = gas_shares[gas_shares.Profile == raw["AC_Gas_Impact_Profile"]].sort_values("Month").Impact.to_numpy()
                btm = raw["AC_Gas_Target_Sector"] == "Residential" and "FuelSub" in raw["AC_Gas_Impact_Profile"]
                v_g = ref.gas_value(d, gshare, gas_acc(scc, btm), q, first_year, meb)
            e[f"ElecBen{s}"], e[f"ElecSupplyCost{s}"] = ref.split_ben_supply(v_e, d["is_fuel_sub"])
            e[f"GasBen{s}"], e[f"GasSupplyCost{s}"] = ref.split_ben_supply(v_g, d["is_fuel_sub"])
            e[f"WaterEnergyBen{s}"], e[f"WaterEnergyCost{s}"] = ref.split_water(v_w, w1)
        e.update(ref.other(raw, d, meb, mec))
        p1, p2 = ref.participant(raw, d, st["qtr"], first_year, mec), ref.participant(raw, d, st["qtr_sct"], first_year, mec)
        e.update(p1)
        e.update({f"{k}_sct": v for k, v in p2.items() if k not in ("MarkEffectPlusExcessIncPV", "RebatesandIncents")})   # the tool has no _sct column of those
        e.update(ref.first_year_net(raw, d, meb))
        rates_e = ref.rate_series("elec", "PGE", E3_VERSION, raw["AC_Elec_Target_Sector"])
        rates_g = ref.rate_series("gas", "PGE", E3_VERSION, raw["AC_Gas_Target_Sector"])
        fe = (d["ntg_kwh"] + meb) / d["ntg_kwh"] if (meb and d["ntg_kwh"] > 0) else 1.0           # C9: bill impacts use NTG + MEB as well
        ft = (d["ntg_therm"] + meb) / d["ntg_therm"] if (meb and d["ntg_therm"] > 0) else 1.0
        pv_e = ref.bill_pv(d, fe * d["shape_scale_1st"], fe * d["shape_scale_2nd"], rates_e, st["annual"], st["base_year"])
        pv_g = ref.bill_pv(d, ft * d["therms_1st"], ft * d["therms_2nd"], rates_g, st["annual"], st["base_year"])
        e["BillReducElec"], e["BillIncrElec"] = ref.split_ben_supply(pv_e, d["is_fuel_sub"])
        e["BillReducGas"], e["BillIncrGas"] = ref.split_ben_supply(pv_g, d["is_fuel_sub"])
        gross_e = ref.bill_pv(d, d["qty"] * d["kwh1"] * d["irr_kwh"], d["qty"] * d["kwh2"] * d["irr_kwh"], rates_e, st["annual"], st["base_year"])
        gross_g = ref.bill_pv(d, d["qty"] * d["th1"] * d["irr_therm"], d["qty"] * d["th2"] * d["irr_therm"], rates_g, st["annual"], st["base_year"])
        bg = {}
        bg["BillReducElecGross"], bg["BillIncrElecGross"] = ref.split_ben_supply(gross_e, d["is_fuel_sub"])
        bg["BillReducGasGross"], bg["BillIncrGasGross"] = ref.split_ben_supply(gross_g, d["is_fuel_sub"])
        e.update(ref.participant_test(d, p1, e["TaxCreditsGross"], bg, st["qtr"], first_year))
        e["prg"], e["ntg_kwh"], e["ntg_therm"] = raw["PrgID"], d["ntg_kwh"], d["ntg_therm"]
        nox, pm10 = ref.nox_pm10("PGE", E3_VERSION, raw["AC_Elec_Target_Sector"], raw["AC_Elec_Impact_Profile"], raw["AC_Climate_Zone"])
        nox_gas = 0.0092 if raw.get("CombustionType") else 0.0
        e.update(ref.emissions(d, ref.hourly_co2_quarters(shape, REPRESENTS, {y: _emission_rate(y) for y in YEARS}), nox, pm10, st["co2_gas"], nox_gas, meb))
        e["poc_shape_scale"], e["poc_shape_scale_2nd"] = d["shape_scale_1st"], d["shape_scale_2nd"]
        e["poc_annual_net_therms_1st"], e["poc_water_scale_1st"] = d["therms_1st"], d["water_scale_1st"]
        rows.append(e)
        per[cid] = e
    for cid, out in zip(MEASURES, ref.cost_tests(rows, program, first_year, st, meb)):
        per[cid] = out
    return per


def _job(tmp_path, meb, mec):
    write_measures(tmp_path / "Measure.csv", MEASURES)
    write_program(tmp_path / "ProgramCost.csv")
    make_acc(tmp_path / "acc.parquet")
    make_gas_acc(tmp_path / "gas.csv")
    make_profiles(tmp_path / "profiles.csv")
    cfg = RunConfig(measures=str(tmp_path / "Measure.csv"), programcost=str(tmp_path / "ProgramCost.csv"), out_dir=str(tmp_path / "out"), run_name="ex", acc_version="2026",
                    acc_electric_file=str(tmp_path / "acc.parquet"), acc_gas_file=str(tmp_path / "gas.csv"), profiles_8760_file=str(tmp_path / "profiles.csv"),
                    market_effects=meb)
    assert mec == meb, "one market-effects option: the same adder on benefits and costs"
    return run_job(cfg, log=lambda s: None)


@pytest.mark.parametrize("meb,mec", [(0.0, 0.0), (0.05, 0.05)])
def test_every_documented_output_matches_the_reference_formulas(tmp_path, meb, mec):
    result = _job(tmp_path, meb, mec)
    frame = result.frame.set_index("CEInputID")
    expected = reference_outputs(meb, mec)
    checked = 0
    for cid, exp in expected.items():
        assert frame.loc[cid, "ElecPricingStatus"] == "priced: hourly profile"
        for name, want in exp.items():
            if name in ("prg", "ntg_kwh", "ntg_therm"):
                continue
            assert name in frame.columns, f"{name} is documented/expected but not in the output"
            got = frame.loc[cid, name]
            assert got == pytest.approx(want, rel=1e-9, abs=1e-9), f"{cid} {name}: tool {got!r} vs reference {want!r}"
            checked += 1
    assert checked > 3 * 100                                     # every output family was compared, for every measure
    # the branches the example was built for really were taken
    assert expected["EX2"]["ElecSupplyCost"] > 0 and expected["EX2"]["ElecBen"] == 0                  # fuel substitution: added load is a supply cost
    assert expected["EX2"]["WaterEnergyCost"] > 0 and expected["EX1"]["WaterEnergyBen"] > 0
    assert expected["EX3"]["GrossParticipantCostPV"] < 0 < expected["EX3"]["NetParticipantCostPV"] - expected["EX3"]["GrossParticipantCostPV"]   # excess incentives
    assert expected["EX1"]["NetElecNOx"] > 0 and expected["EX1"]["NetGasNOx"] > 0

    # portfolio summary (C16.3): CET's portfolio ratios are summed benefits (+ tax credits / bill increases) over the summed cost
    summ = pd.read_csv(result.files["portfolio_summary"]).iloc[0]
    tot = lambda k: sum(e[k] for e in expected.values())
    ben = tot("ElecBen") + tot("GasBen") + tot("WaterEnergyBen") + tot("OtherBen")
    assert summ["TRC_ratio"] == pytest.approx((ben + tot("TaxCredits")) / tot("TRCCost"), rel=1e-9)
    assert summ["PAC_ratio"] == pytest.approx(ben / tot("PACCost"), rel=1e-9)
    assert summ["RIM_ratio"] == pytest.approx((ben + tot("BillIncrElec") + tot("BillIncrGas")) / tot("RIMCost"), rel=1e-9)
    assert summ["SCB_ratio"] == pytest.approx((tot("ElecBen_SB") + tot("GasBen_SB") + tot("WaterEnergyBen_SB") + tot("OtherBen")) / tot("SCBCost"), rel=1e-9)
    assert summ["TotalSystemBenefit"] == pytest.approx(tot("TotalSystemBenefit"), rel=1e-9)
    assert summ["PCT_ratio"] == pytest.approx(tot("PCTBen") / tot("PCTCost"), rel=1e-9)


# ---------------------------------------------------------------------------------------------------------------------------------------------- regressions found while documenting
def _bill_measure(eul, rul, s1=1000.0, s2=0.0):
    return pd.DataFrame([{"measure_id": "m", "claim_year": 2028, "rul_years_exact": rul, "eul_years_exact": eul, "ir_kwh": 1.0, "rr_kwh": 1.0, "annual_net_kwh_1st": s1,
                          "annual_net_kwh_2nd": s2, "annual_net_therms_1st": 0.0, "annual_net_therms_2nd": 0.0, "is_fuel_sub": False}])


def test_bill_impact_of_a_single_baseline_measure_with_a_fractional_life():
    """A single-baseline measure's fractional last year is 1st baseline: a 3.5-year life must be worth more than a 3-year one (it was priced at ~0.7x CET)."""
    rates = {y: 0.20 for y in range(2024, 2060)}
    raf = 1.07
    run = lambda eul: price_bill_impact(_bill_measure(eul, 0.0), lambda m: rates, lambda m: None, lambda m: 0.07, 2024).iloc[0]["BillReducElec"]
    assert run(3.0) == pytest.approx(sum(1000 * 0.2 / raf ** k for k in range(3)))
    assert run(3.5) == pytest.approx(sum(1000 * 0.2 / raf ** k for k in range(3)) + 0.5 * 1000 * 0.2 / raf ** (3 + 4))     # the last year keeps CalcCE's Raf^Qy exponent
    assert run(3.5) > run(3.0)


def test_market_effects_on_costs_reach_the_participant_cost(tmp_path):
    """The TRC, SCB and SCH costs include MECost x (GrossMeasCostPV + ExcessIncentivesPV) (CalcCE's internal NetParticipantCostPV); the exported
    NetParticipantCostPV does not, NetParticipantCostAdjustedPV does (CalcCost's two columns; checked on CET jobs 121842 / 121841)."""
    from conftest import make_electric_acc, make_flat_profile_csv, make_measure_csv
    write = lambda name, **kw: make_measure_csv(tmp_path / name, UnitMeaCost1stBaseline="100", UnitkWh1stBaseline="0", **kw)
    common = dict(out_dir=str(tmp_path / "out"), acc_version="2026", acc_electric_file=str(make_electric_acc(tmp_path / "a.parquet")),
                  profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False)
    base = run_job(RunConfig(measures=str(write("m.csv")), run_name="b", **common), log=lambda s: None).frame.iloc[0]
    me = run_job(RunConfig(measures=str(write("m.csv")), run_name="c", market_effects=0.05, **common), log=lambda s: None).frame.iloc[0]
    assert base["TRCCost"] == pytest.approx(100.0)
    for c in ("TRCCost", "SCBCost", "SCHCost"):
        assert me[c] == pytest.approx(105.0), c                    # 100 + 0.05 x (100 + 0)
    assert me["GrossParticipantCostPV"] == pytest.approx(100.0)   # the gross column has no market-effects term
    assert me["NetParticipantCostPV"] == pytest.approx(100.0) and me["MarkEffectPlusExcessIncPV"] == pytest.approx(5.0)
    assert me["NetParticipantCostAdjustedPV"] == pytest.approx(105.0)


# ---------------------------------------------------------------------------------------------------------------------------------------------- the library route
LIB_ZONE13 = np.exp(np.sin(2 * np.pi * I / 24))                # the stub library's shapes (normalised below)
LIB_SYSTEM = 1 + 0.5 * np.cos(2 * np.pi * I / 168)
LIB_TOU = np.where((DOY % 7 < 5) & (HOD >= 12) & (HOD < 18), 3.0, 1.0)   # flat within "periods"; a weekday peak on a Wednesday-start (2025) week


def make_stub_library(folder):
    """A CEDARS-style library in a reference folder: LIB_SHAPE (PGE, Residential; zone 13 and System), LIB_LOOSE (PGE, Non_Res) and LIB_TOU (PGE, Commercial;
    a TOU-style 8,760 drawn on 2025, the year the ACC's TOU map follows)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    (folder / "impact_profiles").mkdir(parents=True)
    cat = pd.DataFrame([
        {"id": 1, "end_use_name": "LIB_SHAPE", "pa": "Pacific Gas & Electric (PGE)", "iou_ac_territory": "PGE", "target_sector": "Residential", "climate_zones": "13, System",
         "start_year": 2016, "explicit_end_year": np.nan, "description": "", "year_represented": np.nan, "source_impact_type": "calculated", "ac_version": 2026},
        {"id": 2, "end_use_name": "LIB_LOOSE", "pa": "Pacific Gas & Electric (PGE)", "iou_ac_territory": "PGE", "target_sector": "Non_Res", "climate_zones": "System",
         "start_year": 2016, "explicit_end_year": np.nan, "description": "", "year_represented": 2010.0, "source_impact_type": "calculated", "ac_version": 2026},
        {"id": 3, "end_use_name": "LIB_TOU", "pa": "Pacific Gas & Electric (PGE)", "iou_ac_territory": "PGE", "target_sector": "COMMERCIAL", "climate_zones": "13",
         "start_year": 2016, "explicit_end_year": np.nan, "description": "", "year_represented": 2025.0, "source_impact_type": "calculated", "ac_version": 2026}])
    cat.to_parquet(folder / "impact_profiles" / "catalog.parquet", index=False)
    schema = pa.schema([("impact_profile_id", pa.int32()), ("climate_zone", pa.string()), ("hour_of_year", pa.int16()), ("impact", pa.float64())])
    with pq.ParquetWriter(str(folder / "impact_profiles" / "hourly.parquet"), schema) as w:
        for pid, zones in ((1, (("13", LIB_ZONE13), ("System", LIB_SYSTEM))), (2, (("System", LIB_SYSTEM),)), (3, (("13", LIB_TOU),))):
            df = pd.concat([pd.DataFrame({"impact_profile_id": np.int32(pid), "climate_zone": z, "hour_of_year": (I + 1).astype(np.int16), "impact": a / a.sum()}) for z, a in zones])
            w.write_table(pa.Table.from_pandas(df, schema=schema, preserve_index=False))     # one row group per profile: the library's index reads them from metadata


LIB_MEASURES = {
    "LIB1": dict(MEASURES["EX3"], AC_Elec_Impact_Profile="LIB_SHAPE", MeasDescription="library profile, exact match, own zone"),
    "LIB2": dict(MEASURES["EX3"], AC_Elec_Impact_Profile="LIB_SHAPE", AC_Climate_Zone="5", MeasDescription="library profile, zone falls back to System"),
    "LIB3": dict(MEASURES["EX3"], AC_Elec_Impact_Profile="LIB_LOOSE", MeasDescription="library profile, sector-loose match"),
    "TOU1": dict(MEASURES["EX1"], AC_Elec_Impact_Profile="LIB_TOU", MeasDescription="TOU-style 8,760 in the library (represents 2025): the ordinary 8,760 path"),
    "TOU2": dict(MEASURES["EX2"], AC_Elec_Impact_Profile="21 = Res. Wtr. Heating", AC_Elec_Target_Sector="Res_New_Construction", FuelID="", MeasImpactType="",
                 MeasDescription="TOU end use with no 8,760 in the library: not priced (there is no separate TOU route)"),
    "NONE1": dict(MEASURES["EX3"], AC_Elec_Impact_Profile="NO SUCH SHAPE", MeasDescription="no profile at all: not priced"),
}
for m_ in ("LIB1", "LIB2", "LIB3", "TOU1", "TOU2", "NONE1"):
    LIB_MEASURES[m_]["PrgID"] = "PRG1"


@pytest.mark.parametrize("meb", [0.0, 0.05])
def test_library_route_matches_the_reference_formulas(tmp_path, meb):
    from recet import paths
    make_stub_library(tmp_path / "ref")
    write_measures(tmp_path / "Measure.csv", LIB_MEASURES)
    make_acc(tmp_path / "acc.parquet")
    make_gas_acc(tmp_path / "gas.csv")
    cfg = RunConfig(measures=str(tmp_path / "Measure.csv"), out_dir=str(tmp_path / "out"), run_name="lib", acc_version="2026", acc_electric_file=str(tmp_path / "acc.parquet"),
                    acc_gas_file=str(tmp_path / "gas.csv"), reference_dir=str(tmp_path / "ref"), market_effects=meb)
    frame = run_job(cfg, log=lambda s: None).frame.set_index("CEInputID")
    paths.set_reference_dir(None)

    st, first_year = ref.settings("PGE", E3_VERSION), 2028
    # --- where each profile came from, and the library shapes (C4.1)
    assert frame.loc["LIB1", "ProfileSource"] == "library" and frame.loc["LIB2", "ProfileSource"] == "library" and frame.loc["LIB3", "ProfileSource"] == "library (sector-loose match)"
    assert frame.loc["TOU1", "ProfileSource"] == "library" and frame.loc["TOU1", "ElecPricingStatus"] == "priced: hourly profile"
    for cid in ("TOU2", "NONE1"):
        assert frame.loc[cid, "ElecPricingStatus"].startswith("UNPRICED") and frame.loc[cid, "ElecBen"] == 0
    for cid, arr, rep in (("LIB1", LIB_ZONE13, 2009), ("LIB2", LIB_SYSTEM, 2009), ("LIB3", LIB_SYSTEM, 2010), ("TOU1", LIB_TOU, 2025)):
        d = ref.derive(LIB_MEASURES[cid])
        for s, test, q in (("", "TRC", st["qtr"]), ("_SB", "SCT Base", st["qtr_sct"]), ("_SH", "SCT High", st["qtr_sct"])):
            want = ref.elec_value(d, arr / arr.sum(), rep, elec_acc(test), q, first_year, d["shape_scale_1st"], d["shape_scale_2nd"], meb)
            assert frame.loc[cid, f"ElecBen{s}"] == pytest.approx(want, rel=1e-9), (cid, s)
