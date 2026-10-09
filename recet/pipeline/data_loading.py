"""
Loads a CET Measure.csv (pipe delimited) and derives the fields the pricing engine needs (_derive_fields): net kWh / kW / therms per baseline,
EUL and RUL in exact years, the claim year and quarter, the load-shape and avoided-cost keys, the NTG / installation / realization rates, the
fuel-substitution flag and the water-energy terms. load_measures_from_csv can first split statewide programs across the IOUs
(pipeline/statewide_allocation.py).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

def load_measures_from_csv(measure_csv_path: Path, allocate_statewide: bool = False) -> pd.DataFrame:
    """Generic loader: any real CET Measure.csv (pipe-delimited) in, derived-fields DataFrame out.

    allocate_statewide=True first splits any statewide-program rows across the
    IOUs (pipeline/statewide_allocation.py): one row per IOU with `<IOU>_`
    prepended to CEInputID and the per-field factors applied to the RAW
    columns, before any derived field is computed. Non-statewide rows are
    untouched."""
    df = pd.read_csv(measure_csv_path, sep="|", low_memory=False)
    if allocate_statewide:
        from .statewide_allocation import allocate_measure_inputs, warn_unmatched
        df, report = allocate_measure_inputs(df)
        warn_unmatched(report, "Measure.csv")
    return _derive_fields(df)


def _derive_fields(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # DUAL-BASELINE / accelerated-replacement measures (2026-09-16 fix,
    # replacing a real bug found comparing this POC against a live CET run
    # on a real 73,024-measure HPWH filing): UnitkWh1stBaseline and
    # UnitkWh2ndBaseline are NOT two usage levels to subtract -- confirmed
    # directly against real CET output (measure SWWH025-...-CZ01-...:
    # UnitkWh1stBaseline=UnitkWh2ndBaseline=-1830, real CET FirstYearNetKWh
    # = -1830, NOT the 0 this project's old "1st minus 2nd" formula gave).
    # Each is ALREADY a net annual savings value, for a DIFFERENT time
    # period of the measure's life: "1st baseline" (vs the pre-existing
    # equipment) applies from the claim date through that equipment's own
    # Remaining Useful Life (RUL_Yrs); "2nd baseline" (vs a code-compliant
    # replacement -- since the old equipment would have been replaced by
    # code-minimum equipment anyway once RUL expired) applies from RUL_Yrs
    # through the measure's own EUL_Yrs. RUL_Yrs == 0 (the vast majority of
    # real measures) means "not an early replacement" -- 1st baseline
    # applies for the WHOLE EUL and 2nd baseline is genuinely 0 (confirmed:
    # every RUL_Yrs==0 row in the real SWWH025 filing has
    # UnitkWh2ndBaseline==0 exactly), so this is a true generalization, not
    # a behavior change, for every measure that isn't an early replacement.
    # See engine.py's `_phased_scale()` for how RUL_Yrs blends the two
    # values per pricing quarter (RUL_Yrs is genuinely fractional, e.g.
    # 8.33, same as EUL_Yrs already was).
    df["annual_net_kwh_1st"] = df["NumUnits"] * df["UnitkWh1stBaseline"] * df["NTGRkWh"]
    df["annual_net_kwh_2nd"] = df["NumUnits"] * df["UnitkWh2ndBaseline"] * df["NTGRkWh"]
    df["annual_net_kwh"] = df["annual_net_kwh_1st"]  # 1st-baseline value; simpler (non-quarter-aware) modes use this alone

    df["annual_net_kw_1st"] = df["NumUnits"] * df["UnitkW1stBaseline"] * df["NTGRkW"]
    df["annual_net_kw_2nd"] = df["NumUnits"] * df["UnitkW2ndBaseline"] * df["NTGRkW"]
    df["annual_net_kw"] = df["annual_net_kw_1st"]

    # Annual net therm savings: same dual-baseline structure as kWh above,
    # using the real gas-side fields. NaN (no gas savings claimed -- true
    # for most of the small sample and ~14% of the large filing) becomes 0,
    # not dropped, so every measure gets a well-defined (possibly zero) gas value.
    # CalcCE gas benefit: Qty x (NTGRThm + MEBens) x IRThm x RRThm x Thm x Gas, with NTGRThm = COALESCE(NTGRThm, NTGRkWh, 1)
    # (InputMeasurevw) and IRThm/RRThm = ISNULL(., 1). The POC used NTGRTherm.fillna(0) and ignored IR/RR therm: exactly a 1/0.9
    # error on the 53 real 2028 measures with IRThm x RRThm = 0.9 (net gas 1.0029 -> see run_poc/compare_gas_2028).
    _ntg_therm = df["NTGRTherm"].fillna(df["NTGRkWh"]).fillna(1.0)
    _irr_therm = df["InstallationRateTherm"].fillna(1.0) * df["RealizationRateTherm"].fillna(1.0)
    df["annual_net_therms_1st"] = (df["NumUnits"] * df["UnitTherm1stBaseline"].fillna(0) * _ntg_therm * _irr_therm).fillna(0)
    df["annual_net_therms_2nd"] = (df["NumUnits"] * df["UnitTherm2ndBaseline"].fillna(0) * _ntg_therm * _irr_therm).fillna(0)
    df["annual_net_therms"] = df["annual_net_therms_1st"]
    df["gas_profile_key"] = df["E3GasSavProfile"]  # real field: CET's own named monthly gas shape (see gas_savings_profiles.py)
    df["gas_sector"] = df["E3GasSector"]  # real field: Residential/Commercial/Core (see gas_avoided_cost.py)

    # Real field, genuinely fractional (e.g. 8.33) same as EUL_Yrs -- 0 (not
    # NaN) means "not an early replacement" in every real dataset this POC
    # has seen, so no clip(lower=...) floor like eul_years_exact's 0.25:
    # RUL_Yrs==0 is a real, common, meaningful value here, not a data gap.
    df["rul_years_exact"] = df["RUL_Yrs"].fillna(0.0).clip(lower=0.0)

    # Avoided-cost subset key (climate zone + sector): a grouping label only; the hourly ACC series is chosen by PA and climate zone.
    df["subset_key"] = "CZ" + df["E3ClimateZone"].astype(str) + "_" + df["Sector"].astype(str)
    df["climate_zone"] = df["E3ClimateZone"].astype(str)  # real field: the zone whose profile and ACC series price the measure
    df["target_sector"] = df["E3TargetSector"].astype(str)  # real field, needed by real_impact_profiles' catalog match key

    # Load-shape key: E3MeaElecEndUseShape is CET's own real field naming which
    # named electric end-use hourly shape a measure draws from.
    df["shape_key"] = df["E3MeaElecEndUseShape"].astype(str)

    df["measure_id"] = df["CEInputID"]
    df["eul_years"] = df["EUL_Yrs"].round().astype(int).clip(lower=1)
    # Real CalcCE prices in QUARTERS, not whole years -- EUL_Yrs is genuinely
    # fractional in real data (e.g. 3.3), and CET prorates the final partial
    # quarter by that fraction rather than rounding (confirmed in the vendor
    # SQL: `(eulq1 - FLOOR(eulq1)) * benefit`). Kept as its own column,
    # unrounded, for price_real_hourly's quarter-precise discounting
    # (eul_years above is the whole-year version).
    df["eul_years_exact"] = df["EUL_Yrs"].clip(lower=0.25)

    # shape_scale/shape_scale_2nd are what load_shapes[shape_key] (a
    # NORMALIZED 8,760 profile summing to 1 -- see real_impact_profiles.py)
    # gets multiplied by at pricing time, for the pre-RUL and post-RUL
    # phases respectively (see engine._phased_scale()). Kept as their own
    # columns, not just aliases of annual_net_kwh_1st/_2nd, so a
    # demo/synthetic measure can override them (e.g. a load-shifting
    # measure whose annual_net_kwh is legitimately 0 but whose shape_scale
    # is not).
    # CalcCE multiplies the electric benefit by IRkWh x RRkWh (installation / realization rates; ISNULL(.,1)) -- the
    # original hourly path omitted them, harmless while every ground-truth job had both = 1 but a 1-2% bias (up to 40% on
    # individual measures) on real filings (2028 consolidated input, 2026-09-20). Folded into shape_scale here.
    irr_kwh = df["InstallationRatekWh"].fillna(1.0) * df["RealizationRatekWh"].fillna(1.0)
    df["shape_scale"] = df["annual_net_kwh_1st"] * irr_kwh
    df["shape_scale_2nd"] = df["annual_net_kwh_2nd"] * irr_kwh

    # Water energy (CalcCE: kWhWater1/2 = ISNULL(UnitkWhIOUWater1st/2ndBaseline, 0), the IOU-supplied energy embedded in the water a measure saves or uses). It is
    # priced like the electric benefit but with these quantities and the Gen term only (run_poc.run_water_energy). water_scale_* are the NTG x IR x RR-baked
    # counterparts of shape_scale_*.
    w1 = df["UnitkWhIOUWater1stBaseline"].fillna(0.0) if "UnitkWhIOUWater1stBaseline" in df.columns else 0.0
    w2 = df["UnitkWhIOUWater2ndBaseline"].fillna(0.0) if "UnitkWhIOUWater2ndBaseline" in df.columns else 0.0
    df["kwh_water_1st"], df["kwh_water_2nd"] = w1, w2
    df["water_net_kwh_1st"] = df["NumUnits"] * w1 * df["NTGRkWh"]
    df["water_net_kwh_2nd"] = df["NumUnits"] * w2 * df["NTGRkWh"]
    df["water_scale_1st"] = df["water_net_kwh_1st"] * irr_kwh
    df["water_scale_2nd"] = df["water_net_kwh_2nd"] * irr_kwh

    # Participant-cost raw fields (2026-09-17, for engine.price_participant_cost):
    # real Measure.csv fields, previously unused by this POC -- ISNULL(...,0)
    # to match InputMeasurevw's own real NULL-handling exactly (e.g. this
    # filing's UnitIncentiveToOthers/UnitEndUserRebate/UnitDirectInstallLab/
    # UnitDirectInstallMat/MeasInflation/UnitTaxCredits are all genuinely
    # blank -- a real no-incentive-program filing, not a data gap this POC
    # papers over).
    df["num_units"] = df["NumUnits"]
    df["unit_meas_cost_1st"] = df["UnitMeaCost1stBaseline"].fillna(0.0)
    df["unit_meas_cost_2nd"] = df["UnitMeaCost2ndBaseline"].fillna(0.0)
    df["unit_incentive_to_others"] = df["UnitIncentiveToOthers"].fillna(0.0)
    df["unit_end_user_rebate"] = df["UnitEndUserRebate"].fillna(0.0)
    df["unit_di_labor"] = df["UnitDirectInstallLab"].fillna(0.0)
    df["unit_di_material"] = df["UnitDirectInstallMat"].fillna(0.0)
    df["ntgr_cost"] = df["NTGRCost"].fillna(df["NTGRkWh"]).fillna(1.0)  # real Coalesce(NTGRCost,NTGRkWh,1)
    df["meas_inflation"] = df["MeasInflation"].fillna(0.0)
    df["unit_gas_infra_bens"] = df["UnitGasInfraBens"].fillna(0.0)
    df["unit_refrig_costs"] = df["UnitRefrigCosts"].fillna(0.0)
    df["unit_refrig_bens"] = df["UnitRefrigBens"].fillna(0.0)
    df["unit_misc_costs"] = df["UnitMiscCosts"].fillna(0.0)
    df["unit_misc_bens"] = df["UnitMiscBens"].fillna(0.0)
    df["unit_tax_credits"] = df["UnitTaxCredits"].fillna(0.0)

    # Emissions raw fields (2026-09-18, for engine.price_emissions): per-unit
    # 1st/2nd-baseline savings, real installation/realization rates, therm NTG,
    # and the measure's combustion type (real InputMeasurevw NULL-handling).
    df["prg_id"] = df["PrgID"].astype(str).str.strip()
    df["unit_kwh_1st"] = df["UnitkWh1stBaseline"].fillna(0.0)
    df["unit_kwh_2nd"] = df["UnitkWh2ndBaseline"].fillna(0.0)
    df["unit_therm_1st"] = df["UnitTherm1stBaseline"].fillna(0.0)
    df["unit_therm_2nd"] = df["UnitTherm2ndBaseline"].fillna(0.0)
    df["ntgr_therm"] = df["NTGRTherm"].fillna(df["NTGRkWh"]).fillna(1.0)
    df["ir_kwh"] = df["InstallationRatekWh"].fillna(1.0)
    df["rr_kwh"] = df["RealizationRatekWh"].fillna(1.0)
    # kW-side fields (2026-09-20, for the TOU / DSType-kW T&D term): CalcCE prices the T&D part of an electric
    # benefit on DS = kW (when the avoided-cost row's DSType is 'kW') or kWh, with the kW versions of NTG/IR/RR.
    # InputMeasurevw: NTGRkW = COALESCE(NTGRkW, NTGRkWh, 1); IRkW/RRkW = ISNULL(., 1); kW1/kW2 = UESkW/UESkW_ER.
    df["unit_kw_1st"] = df["UnitkW1stBaseline"].fillna(0.0)
    df["unit_kw_2nd"] = df["UnitkW2ndBaseline"].fillna(0.0)
    df["ntgr_kwh"] = df["NTGRkWh"].fillna(1.0)
    df["ntgr_kw"] = df["NTGRkW"].fillna(df["NTGRkWh"]).fillna(1.0)
    df["ir_kw"] = df["InstallationRatekW"].fillna(1.0)
    df["rr_kw"] = df["RealizationRatekW"].fillna(1.0)
    df["ir_therm"] = df["InstallationRateTherm"].fillna(1.0)
    df["rr_therm"] = df["RealizationRateTherm"].fillna(1.0)
    df["combustion_type"] = df["CombustionType"] if "CombustionType" in df.columns else None

    # Fuel type (CalcCE's FuelTypesForFuelSubstitution join): only these types book a negative electric/gas value as a
    # positive SUPPLY COST; every other measure keeps its signed value inside ElecBen/GasBen (CET_v25.1_documentation_
    # extracted.txt:13415-13450 and 13930-13975). See run_poc._split_ben_supply.
    # EMPIRICAL rule (real CET run 121219, 5,518 measures, 0 misses): CET books supply costs for a measure when FuelID is one of the
    # three types OR its MeasImpactType contains "FuelSub" (Deem-WP-FuelSub / Deem-DEER-FuelSub rows have a blank FuelID but were
    # treated as fuel-sub: 301 of 504 supply-cost measures). The SQL only shows FuelType coming from an upload table.
    df["fuel_type"] = df["FuelID"].astype(str) if "FuelID" in df.columns else "None"
    _by_id = df["FuelID"].isin(["AllElec-New", "FuelSub-ToElec", "FuelSub-ToGas"]) if "FuelID" in df.columns else False
    _by_impact = df["MeasImpactType"].astype(str).str.contains("FuelSub") if "MeasImpactType" in df.columns else False
    df["is_fuel_sub"] = _by_id | _by_impact

    df["eul_years_raw"] = df["EUL_Yrs"].astype(float)            # as filed (first-year savings use it; pricing uses eul_years_exact, floored at 0.25)
    df["claim_year"] = df["ClaimYearQuarter"].astype(str).str[:4].astype(int)
    # Real field: which quarter (1-4) of claim_year the measure starts
    # accruing savings in -- CET's benefit stream starts here, not at that
    # year's Q1 (see price_real_hourly's docstring for why this matters:
    # a Q4-claimed measure's "first year" is really one quarter, not four).
    df["claim_quarter"] = df["ClaimYearQuarter"].astype(str).str[-1].astype(int)

    cols = [
        "measure_id", "MeasDescription", "subset_key", "shape_key",
        "annual_net_kwh", "annual_net_kwh_1st", "annual_net_kwh_2nd",
        "annual_net_kw", "annual_net_kw_1st", "annual_net_kw_2nd",
        "eul_years", "eul_years_exact", "rul_years_exact", "NTGRkWh",
        "shape_scale", "shape_scale_2nd",
        "claim_year", "claim_quarter", "climate_zone", "target_sector",
        "annual_net_therms", "annual_net_therms_1st", "annual_net_therms_2nd",
        "gas_profile_key", "gas_sector",
        "num_units", "unit_meas_cost_1st", "unit_meas_cost_2nd",
        "unit_incentive_to_others", "unit_end_user_rebate",
        "unit_di_labor", "unit_di_material", "ntgr_cost", "meas_inflation",
        "unit_gas_infra_bens", "unit_refrig_costs", "unit_refrig_bens",
        "unit_misc_costs", "unit_misc_bens", "unit_tax_credits",
        "unit_kwh_1st", "unit_kwh_2nd", "unit_therm_1st", "unit_therm_2nd", "ntgr_therm",
        "ir_kwh", "rr_kwh", "ir_therm", "rr_therm", "combustion_type", "prg_id",
        "unit_kw_1st", "unit_kw_2nd", "ntgr_kwh", "ntgr_kw", "ir_kw", "rr_kw",
        "fuel_type", "is_fuel_sub", "eul_years_raw",
        "kwh_water_1st", "kwh_water_2nd", "water_net_kwh_1st", "water_net_kwh_2nd", "water_scale_1st", "water_scale_2nd",
    ]
    if "IOU_AC_Territory" in df.columns:
        df["PA"] = df["IOU_AC_Territory"]  # real field, direct on every Measure.csv this POC has seen
        cols.append("PA")  # the IOU whose avoided costs, profiles and discount rate price the measure
    return df[cols]
