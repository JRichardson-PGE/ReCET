"""
Job-level orchestration (the module keeps its proof-of-concept name): load_job reads a CET job, the run_* functions wire each pricing step to
its data (electric 8,760, gas, water energy, bill impacts, participant costs, other benefits/costs), and run_full_cost_effectiveness puts them
together into CET's output columns and cost tests. recet.jobs calls it for every run.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from .pipeline import (
    cost_tests,
    discount_settings,
    gas_avoided_cost,
    gas_savings_profiles,
    rate_schedules,
    real_avoided_cost_v2,
    real_impact_profiles,
    statewide_allocation,
    system_zones,
)
from .pipeline import data_loading  # noqa: E402 (module-level use in load_job)
from .pipeline.data_loading import FIRST_BASELINE_QUANTITIES, SECOND_BASELINE_QUANTITIES
from .pipeline.engine import (
    HourlyPricingResult,
    price_bill_impact,
    price_gas_benefit,
    price_other_ben_cost,
    price_participant_cost,
    price_real_hourly,
)

def _ids_in(df: pd.DataFrame | None) -> set:
    """The CEInputIDs present in a pricing result; empty when nothing was priced (such a frame has no columns at all)."""
    return set() if df is None or df.empty else set(df["CEInputID"].unique())          # unique first: a per-measure engine frame has a row per quarter


# what decides a priced part's electric profile and avoided-cost series
ROUTE_COLUMNS = ["Used_AC_Elec_Impact_Profile", "IOU_AC_Territory", "AC_Elec_Target_Sector", "AC_Climate_Zone"]
ELEC_PROFILES = ("AC_Elec_Impact_Profile", "AC_Elec_Impact_Profile_2nd_Baseline")                     # the profile of each baseline
ELEC_QUANTITIES = ("AC_Net_kWh_ScaledProfile_1st_Baseline", "AC_Net_kWh_ScaledProfile_2nd_Baseline")   # what each baseline's profile is multiplied by
GAS_QUANTITIES = ("AC_Net_Therms_1st_Baseline", "AC_Net_Therms_2nd_Baseline")
BILL_QUANTITIES = ("AC_Net_kWh_1st_Baseline", "AC_Net_kWh_2nd_Baseline", "AC_Net_Therms_1st_Baseline", "AC_Net_Therms_2nd_Baseline")


def _route_keys(df: pd.DataFrame, cols: list[str]) -> list[tuple]:
    """One hashable per row: the values of `cols`."""
    return list(zip(*[df[c].astype(str).to_numpy() for c in cols]))


def _per_route(df: pd.DataFrame, cols: list[str], fn) -> pd.Series:
    """`fn(row) -> bool` for every row of `df`, evaluated once per distinct combination of `cols` (on that group's first row) and once per demo measure, instead of once per row.
    The callbacks used with it depend on nothing but those columns (and, for the demo measure, its id), so the result equals the row-by-row `apply`."""
    if df.empty:
        return pd.Series(False, index=df.index, dtype=bool)
    codes, _uniques = pd.factorize(pd.Series(_route_keys(df, cols), dtype=object))
    _, first = np.unique(codes, return_index=True)
    flags = np.array([bool(fn(df.iloc[p])) for p in first])
    return pd.Series(flags[codes], index=df.index)


def split_by_baseline_shape(measures: pd.DataFrame, profiles: tuple[str, str] = ELEC_PROFILES) -> pd.DataFrame:
    """The parts of the measures that are priced on one electric profile each, with that profile in Used_AC_Elec_Impact_Profile. A measure whose 2nd-baseline years
    have their own profile (`profiles[1]`, AC_Elec_Impact_Profile_2nd_Baseline) becomes two rows with the same CEInputID: the 1st-baseline part on `profiles[0]`
    (every 2nd-baseline quantity set to 0) and the 2nd-baseline part on `profiles[1]` (every 1st-baseline quantity set to 0). The baseline phasing is linear in
    the two quantities, so the parts add up to the measure; with one profile the measure is one row. Only early replacements (0 < RUL_Yrs < EUL_Yrs) have a 2nd
    baseline to split. Sum results by CEInputID afterwards."""
    parts = measures.copy()
    parts["Used_AC_Elec_Impact_Profile"] = parts[profiles[0]]
    has_own_2nd_baseline_profile = (parts[profiles[1]] != parts[profiles[0]]) & (parts["RUL_Yrs"] > 0) & (parts["RUL_Yrs"] < parts["EUL_Yrs"])
    if not has_own_2nd_baseline_profile.any():
        return parts
    first_baseline_part, second_baseline_part = parts[has_own_2nd_baseline_profile].copy(), parts[has_own_2nd_baseline_profile].copy()
    for c in SECOND_BASELINE_QUANTITIES:
        if c in first_baseline_part.columns:
            first_baseline_part[c] = 0.0
    second_baseline_part["Used_AC_Elec_Impact_Profile"] = second_baseline_part[profiles[1]]
    for c in FIRST_BASELINE_QUANTITIES:
        if c in second_baseline_part.columns:
            second_baseline_part[c] = 0.0
    out = pd.concat([parts[~has_own_2nd_baseline_profile], first_baseline_part, second_baseline_part])
    return out.sort_index(kind="stable").reset_index(drop=True)         # each measure where it was, its 1st-baseline part before its 2nd


def _sum_by_measure(df: pd.DataFrame, value_col: str, name: str) -> pd.Series:
    """Lifetime value per CEInputID. An empty result (no measure of this fuel priced -- e.g. an electric-only job has no gas rows) is an empty Series that still
    carries the CEInputID index, so it merges like any other."""
    if df is None or df.empty:
        return pd.Series(dtype=float, name=name, index=pd.Index([], name="CEInputID"))
    return df.groupby("CEInputID")[value_col].sum().rename(name)


def ntg_factor_with_market_effects(ntg: pd.Series, meb: float) -> pd.Series:
    """(NTG + MEBens) / NTG: turns a net quantity (which carries NTG) into the one a benefit is priced on under CET's run-level Market Effects (MEBens, 0% or
    5%; CalcCE: Qty x (NTGRkWh + MEBens) x ...). 1 where NTG = 0 (the net value is 0 and gross is not recoverable). Costs are not touched: see
    `run_participant_cost`."""
    if not meb:
        return pd.Series(1.0, index=ntg.index)
    return ((ntg + meb) / ntg).where(ntg > 0, 1.0)


def run_real_hourly(measures: pd.DataFrame, cost_test: str = "TRC", acc_version: int = 2024,
                     base_year: int | None = None,
                     discount_rate_fn=discount_settings.get_discount_rate_qtr,
                     market_effects_bens: float = 0.0, component: str | None = None,
                     gen_only: bool = False, quantities: tuple[str, str] = ELEC_QUANTITIES, profiles: tuple[str, str] = ELEC_PROFILES):
    """
    quantities / profiles: the columns holding what each baseline's profile is multiplied by, and each baseline's profile (the electric benefit's by default;
    run_water_energy passes the water energy's). The quantities carry NTG, installation and realization rates; market effects are applied here.

    ONE ROUTE ONLY (2026-10-02, Jake): every electric measure is priced on the 8,760 path -- its impact profile (user-supplied, or the library's, which now
    includes the Gen-based TOU H-factor 8,760s) re-aligned to the ACC's 2018 weekdays (real_impact_profiles.shift_for_year), times the hourly ACC, summed
    within the ACC quarters. The TOU quarterly route (CEDARS' calculator values, kW-based T&D) and CET's quarterly SCG table are no longer used; SCG is priced
    on the hourly ACC by real_avoided_cost_v2's SCG routing (SCE generation; PG&E T&D in CZ 4/5/13, SCE elsewhere).

    market_effects_bens (CET's run-level "Market Effects" 0%/5%, `MEBens`): NTGRkWh -> NTGRkWh + MEBens on the electric benefit.
    component: price only that ACC component (a column of the electric ACC, e.g. "energy"; see real_avoided_cost_v2.available_components) instead of the
    total -- the values of all components add up to the total.
    gen_only: price on the Gen term alone (total avoided cost minus transmission and distribution) -- what CalcCE uses for water energy, which has no T&D term.

    Wires up the REAL, per-measure CEDARS impact-profile shapes (pipeline/
    real_impact_profiles.py), the REAL, per-year hourly ACC extraction
    (pipeline/real_avoided_cost_v2.py), and the REAL, per-IOU quarterly
    discount rates (pipeline/discount_settings.py) -- see price_real_hourly's
    docstring for the full "lessons learned" list this now applies:
    per-year-varying avoided cost, the validated day-of-week shift, real
    PA-specific discounting, quarterly (not annual) compounding, and
    fractional-EUL/claim-quarter precision.

    base_year defaults to the MIN Used_Year across `measures` -- this
    mirrors a real CET job's "First Year" receipt field, which Jake
    confirmed is the actual discounting reference point (not the static
    BaseYear column in the settings table). Both of this POC's real ground-
    truth jobs happen to have every measure share one claim year, so this
    equals that claim year exactly; pass base_year explicitly for a batch
    that doesn't (e.g. spans multiple claim years) once its real receipt's
    First Year is known.


    NO SYNTHETIC SHAPE FALLBACK for real measures (2026-09-16, Jake's
    direction, replacing the old always-on placeholder substitution): a
    measure with no real 8760-hour CEDARS profile match is now FLAGGED
    (collected into the returned HourlyPricingResult's
    `unpriced_ids`) and excluded from pricing, never priced
    against a fabricated shape.

    discount_rate_fn (2026-09-17, for SCB/SCH): defaults to the primary
    quarterly rate, but the real societal cost tests discount their own
    avoided-cost PV term with a genuinely different rate
    (`discount_settings.get_discount_rate_qtr_sct`) -- pass that in when
    cost_test is "SCT Base"/"SCT High" so this function's discounting
    matches which cost test it's actually pricing.

    Returns the HourlyPricingResult.
    """
    if base_year is None:
        base_year = int(measures["Used_Year"].min())

    priced_parts = split_by_baseline_shape(measures.reset_index(drop=True), profiles)     # a separate 2nd-baseline profile prices as a second part
    ntg_factor = ntg_factor_with_market_effects(priced_parts["NTGRkWh"], market_effects_bens)
    priced_parts["Priced_Quantity_1st_Baseline"] = priced_parts[quantities[0]] * ntg_factor
    priced_parts["Priced_Quantity_2nd_Baseline"] = priced_parts[quantities[1]] * ntg_factor
    # AC_Climate_Zone = System is priced in the profile's System zone (pipeline/system_zones.py): its profile AND its prices come from that real zone
    priced_parts["Used_Climate_Zone"] = [system_zones.priced_zone(k, pa, ts, cz) for k, pa, ts, cz in
                                         zip(priced_parts["Used_AC_Elec_Impact_Profile"], priced_parts["IOU_AC_Territory"], priced_parts["AC_Elec_Target_Sector"],
                                             priced_parts["AC_Climate_Zone"])]

    profile_by_route: dict[tuple, tuple | None] = {}   # (profile, IOU, sector, zone) -> (profile, represents_year, source) or None: one lookup per route

    def get_shape(m):
        key = (m["Used_AC_Elec_Impact_Profile"], m["IOU_AC_Territory"], m["AC_Elec_Target_Sector"], m["Used_Climate_Zone"])
        if key not in profile_by_route:
            profile_by_route[key] = real_impact_profiles.resolve_shape(*key)   # (profile, year, source)
        hit = profile_by_route[key]
        if hit is None:
            return None
        return hit[0], hit[1]

    acc_by_route: dict[tuple, dict] = {}

    def get_acc_years(m):
        key = (m["IOU_AC_Territory"], m["Used_Climate_Zone"])
        if key not in acc_by_route:
            if gen_only:
                acc_by_route[key] = real_avoided_cost_v2.get_gen_td_series(*key, cost_test)[0]
            else:
                acc_by_route[key] = real_avoided_cost_v2.get_acc_series(*key, cost_test, component)[0]
        return acc_by_route[key]

    discount_rate_qtr_by_iou: dict[str, float] = {}

    def get_discount_rate_qtr(m):
        iou = m["IOU_AC_Territory"]
        if iou not in discount_rate_qtr_by_iou:
            discount_rate_qtr_by_iou[iou] = discount_rate_fn(iou, acc_version)
        return discount_rate_qtr_by_iou[iou]

    elec_pricing = price_real_hourly(
        priced_parts, get_shape, get_acc_years, real_impact_profiles.shift_for_year,
        get_discount_rate_qtr, base_year,
        route_key=_route_keys(priced_parts, ROUTE_COLUMNS),                # profile / ACC series / rate are looked up once per route, not once per measure
    )

    profile_source = [(profile_by_route.get(k) or (None, None, ""))[2] for k in
                      zip(priced_parts["Used_AC_Elec_Impact_Profile"], priced_parts["IOU_AC_Territory"], priced_parts["AC_Elec_Target_Sector"],
                          priced_parts["Used_Climate_Zone"])]      # where each part's 8,760 profile came from (user profile / library ...)
    elec_pricing.route = {}
    for mid, src in zip(priced_parts["CEInputID"], profile_source):        # a measure split by baseline reports its 1st-baseline profile
        elec_pricing.route.setdefault(mid, f"hourly profile | {src}")
    return elec_pricing


def run_gas_benefit(measures: pd.DataFrame, cost_test: str = "TRC", acc_version: int = 2024, base_year: int | None = None,
                     scc: str = "", discount_rate_fn=discount_settings.get_discount_rate_qtr,
                     market_effects_bens: float = 0.0, component: str | None = None):
    """
    market_effects_bens: NTGRThm -> NTGRThm + MEBens (see `_apply_market_effects_gas`). component: price only that gas ACC component
    (market, td, nox, co2, upstream_methane, behind_meter_methane, air_quality_adder); the components add up to the total.

    Wires up the real 2024 Gas ACC monthly avoided costs
    (pipeline/gas_avoided_cost.py) and real CEDARS gas load-shape profiles
    (pipeline/gas_savings_profiles.py) -- see engine.price_gas_benefit's
    docstring for the full mechanics and its stated scope limits (single-
    year gas ACC rate, End Use/Emission Control approximated from
    AC_Gas_Target_Sector since Measure.csv has no real field for it).

    Only measures with a nonzero AC_Net_Therms_1st/2nd_Baseline AND a real matching gas
    shape are priced -- most of the small 5-measure sample has no real gas
    savings at all (only electric), so an empty or near-empty result there
    is expected, not a bug.

    Returns (gas_result_df, n_gas_measures, n_matched_shape).
    """
    if base_year is None:
        base_year = int(measures["Used_Year"].min())
    measures = measures.copy()
    ntg_factor = ntg_factor_with_market_effects(measures["NTGRTherm"], market_effects_bens)
    measures["Priced_Quantity_1st_Baseline"] = measures[GAS_QUANTITIES[0]] * ntg_factor
    measures["Priced_Quantity_2nd_Baseline"] = measures[GAS_QUANTITIES[1]] * ntg_factor

    profile_by_name: dict[str, np.ndarray | None] = {}

    def get_gas_shape(m):
        key = m["AC_Gas_Impact_Profile"]
        if pd.isna(key):
            return None
        if key not in profile_by_name:
            profile_by_name[key] = gas_savings_profiles.get_shape(key)
        return profile_by_name[key]

    acc_by_route: dict[tuple, dict[int, np.ndarray]] = {}

    def get_gas_acc_years(m):
        key = (m["IOU_AC_Territory"], m["AC_Gas_Target_Sector"], m["AC_Gas_Impact_Profile"])
        if key not in acc_by_route:
            acc_by_route[key] = gas_avoided_cost.get_years_for_measure(*key, cost_test, scc, component)
        return acc_by_route[key]

    discount_rate_qtr_by_iou: dict[str, float] = {}

    def get_discount_rate_qtr(m):
        iou = m["IOU_AC_Territory"]
        if iou not in discount_rate_qtr_by_iou:
            discount_rate_qtr_by_iou[iou] = discount_rate_fn(iou, acc_version)
        return discount_rate_qtr_by_iou[iou]

    has_gas_savings = (measures["AC_Net_Therms_1st_Baseline"] != 0) | (measures["AC_Net_Therms_2nd_Baseline"] != 0)
    n_with_gas_savings = int(has_gas_savings.sum())
    n_with_gas_profile = int(measures.loc[has_gas_savings, "AC_Gas_Impact_Profile"]
                             .apply(lambda k: not pd.isna(k) and gas_savings_profiles.get_shape(k) is not None).sum())

    result = price_gas_benefit(measures, get_gas_shape, get_gas_acc_years, get_discount_rate_qtr, base_year,
                               route_key=_route_keys(measures, ["AC_Gas_Impact_Profile", "IOU_AC_Territory", "AC_Gas_Target_Sector"]))   # one lookup per route
    return result, n_with_gas_savings, n_with_gas_profile


def run_bill_impact(measures: pd.DataFrame, acc_version: int = 2024, market_effects_bens: float = 0.0, quantities: tuple[str, str, str, str] = BILL_QUANTITIES):
    """
    quantities: the columns of the annual kWh (1st, 2nd baseline) and therms (1st, 2nd) the bill changes by -- the net ones by default (AC_Net_kWh_1st_Baseline ...);
    participant_cost_test passes the gross ones. market_effects_bens: NTG -> NTG + MEBens on each (CalcCE prices the bill on Qty x (NTGRkWh + MEBens) / (NTGRThm +
    MEBens), :15885-15974).

    Wires up the real CET retail rate schedules (pipeline/rate_schedules.py)
    for the RIM test's bill-impact columns -- see engine.price_bill_impact's
    docstring for the full mechanics (annual, not quarterly; sign-based
    Reduc/Incr classification).

    Returns a DataFrame with one row per measure: CEInputID, LifecycleBillReducElec,
    LifecycleBillIncrElec, LifecycleBillReducGas, LifecycleBillIncrGas -- NOT a per-quarter
    series like this POC's other run_* functions, since the real quantity
    itself is a single lifetime PV number per measure (see price_bill_impact).
    """
    measures = measures.copy()
    elec_factor = ntg_factor_with_market_effects(measures["NTGRkWh"], market_effects_bens)
    gas_factor = ntg_factor_with_market_effects(measures["NTGRTherm"], market_effects_bens)
    for col, q, f in (("Billed_kWh_1st_Baseline", quantities[0], elec_factor), ("Billed_kWh_2nd_Baseline", quantities[1], elec_factor),
                      ("Billed_Therms_1st_Baseline", quantities[2], gas_factor), ("Billed_Therms_2nd_Baseline", quantities[3], gas_factor)):
        measures[col] = measures[q] * f

    elec_rates_by_key: dict[tuple, dict[int, float] | None] = {}

    def get_elec_rate_years(m):
        key = (m["IOU_AC_Territory"], m["AC_Elec_Target_Sector"], acc_version)
        if key not in elec_rates_by_key:
            elec_rates_by_key[key] = rate_schedules.get_elec_rate_years(*key)
        return elec_rates_by_key[key]

    gas_rates_by_key: dict[tuple, dict[int, float] | None] = {}

    def get_gas_rate_years(m):
        key = (m["IOU_AC_Territory"], m["AC_Gas_Target_Sector"], acc_version)
        if key not in gas_rates_by_key:
            gas_rates_by_key[key] = rate_schedules.get_gas_rate_years(*key)
        return gas_rates_by_key[key]

    discount_rate_annual_by_iou: dict[str, float] = {}

    def get_discount_rate_annual(m):
        iou = m["IOU_AC_Territory"]
        if iou not in discount_rate_annual_by_iou:
            discount_rate_annual_by_iou[iou] = discount_settings.get_discount_rate_annual(iou, acc_version)
        return discount_rate_annual_by_iou[iou]

    return price_bill_impact(measures, get_elec_rate_years, get_gas_rate_years, get_discount_rate_annual,
                             run_parameters_base_year=discount_settings.get_settings("PGE", acc_version)["BaseYear"])


def run_participant_cost(measures: pd.DataFrame, acc_version: int = 2024, base_year: int | None = None,
                          discount_rate_fn=discount_settings.get_discount_rate_qtr, market_effects_cost: float = 0.0):
    """Wires up engine.price_participant_cost -- see its docstring for the
    full mechanics and documented scope limits (no ProgramCost.csv, no
    Market Effects). `discount_rate_fn` defaults to the primary quarterly
    rate; pass `discount_settings.get_discount_rate_qtr_sct` to get the
    SCB/SCH cost tests' own `NetParticipantCostPV_SCT`-equivalent instead
    (real SQL: CET_v25.1_documentation_extracted.txt:14891-14916,
    :15135-15136). Returns one row per measure."""
    if base_year is None:
        base_year = int(measures["Used_Year"].min())

    discount_rate_qtr_by_iou: dict[str, float] = {}

    def get_discount_rate_qtr(m):
        iou = m["IOU_AC_Territory"]
        if iou not in discount_rate_qtr_by_iou:
            discount_rate_qtr_by_iou[iou] = discount_rate_fn(iou, acc_version)
        return discount_rate_qtr_by_iou[iou]

    out = price_participant_cost(measures, get_discount_rate_qtr, base_year, market_effects_cost=market_effects_cost)
    # Two participant costs, as CET exports them (CalcCost, CET_v25.1_documentation_extracted.txt:16820-16828): NetParticipantCostPV WITHOUT the market-effects
    # term and NetParticipantCostAdjustedPV = NetParticipantCostPV + MarkEffectPlusExcessIncPV. The TRC / SCB / SCH costs use the adjusted one (it is CalcCE's
    # internal NetParticipantCostPV, :15127-15134). Confirmed on CET jobs 121842 (0%) / 121841 (5%), 2026-10-08: the exported NetParticipantCostPV is identical in
    # both, NetParticipantCostAdjustedPV and TRCCost rise by exactly MarkEffectPlusExcessIncPV. (Until then the POC overwrote NetParticipantCostPV with the adjusted
    # value, so its column differed from CET's whenever market effects were on; TRCCost was right.)
    return out


def _split_ben_supply(value: pd.Series, fuel_sub_flag: pd.Series) -> tuple[pd.Series, pd.Series]:
    """CalcCE's Ben / SupplyCost split. Fuel-substitution measures (Fuel_Sub_Flag: AllElec-New, FuelSub-ToElec, FuelSub-ToGas) book a positive
    value as benefit and a negative one as a positive supply cost; every other measure keeps its SIGNED value in the benefit
    column and has no supply cost (ISNULL(ft.FuelType,0)='0' branch of CalcCE's ElecBen/GasBen and its
    `NOT ISNULL(ft.FuelType,0)='0'` guard on ElecSupplyCost/GasSupplyCost). The old unconditional clip was only correct for
    all-fuel-sub filings (SWWH025) and put every non-fuel-sub measure's negative savings into supply cost."""
    fuel_sub_flag = fuel_sub_flag.fillna(False).astype(bool)
    benefit = value.where(~fuel_sub_flag, value.clip(lower=0.0))
    supply_cost = (-value).clip(lower=0.0).where(fuel_sub_flag, 0.0)
    return benefit, supply_cost


def run_water_energy(measures: pd.DataFrame, cost_test: str = "TRC", acc_version: int = 2024, base_year: int | None = None,
                     discount_rate_fn=discount_settings.get_discount_rate_qtr, market_effects_bens: float = 0.0):
    """Lifetime PV of the WATER ENERGY of every measure that has any (UnitkWhIOUWater1st/2ndBaseline != 0): the IOU-supplied energy embedded in the water a measure
    saves (positive) or uses (negative), priced exactly like the electric benefit but with kWhWater1/2 in place of kWh1/2 and the Gen term only (CalcCE's
    WaterEnergyBen: Qty x (NTGRkWh + MEBens) x IRkWh x RRkWh x (kWhWater1 x Gen_1 + kWhWater2 x Gen_2)). The measure's own electric end-use profile and climate zone
    carry over, on the 8,760 path like the electric benefit, but on the Gen term ALONE: unlike ElecBen there is no T&D term, so total avoided cost minus
    transmission and distribution (run_real_hourly gen_only). Returns (signed value per CEInputID named `WaterEnergyValue`, the HourlyPricingResult for the status
    columns)."""
    water_measures = measures[(measures["UnitkWhIOUWater1stBaseline"] != 0) | (measures["UnitkWhIOUWater2ndBaseline"] != 0)]
    if water_measures.empty:
        return _sum_by_measure(None, "LifetimeValuePV", "WaterEnergyValue"), HourlyPricingResult(values=pd.DataFrame(columns=["CEInputID", "LifetimeValuePV"]))
    water_measures = water_measures.reset_index(drop=True).copy()
    # what the water profile (AC_Elec_Impact_Profile_Water, blank = the measure's own profile) is multiplied by, for both baselines
    for b in ("1st", "2nd"):
        water_measures[f"AC_Net_kWh_Water_ScaledProfile_{b}_Baseline"] = water_measures[f"AC_Net_kWh_Water_{b}_Baseline"]
    if real_impact_profiles.user_profiles() is not None:
        # a kWh-per-unit profile (apply_scaled_profiles) is in kWh, not shares: divide by its annual kWh so the water energy is spread by its shares.
        # A load-shifting-only profile (annual kWh 0) has no shares, so its water energy is not priced (preflight warns: zero_net_profile_water).
        wm = water_measures
        zones = [system_zones.priced_zone(k, iou, ts, cz) for k, iou, ts, cz in
                 zip(wm["AC_Elec_Impact_Profile_Water"], wm["IOU_AC_Territory"], wm["AC_Elec_Target_Sector"], wm["AC_Climate_Zone"])]
        profile_annual_kwh_per_unit = pd.Series([real_impact_profiles.scaled_profile(k, iou, ts, z) for k, iou, ts, z in
                                                 zip(wm["AC_Elec_Impact_Profile_Water"], wm["IOU_AC_Territory"], wm["AC_Elec_Target_Sector"], zones)],
                                                index=wm.index, dtype=float)
        on_kwh_per_unit_profile = profile_annual_kwh_per_unit.notna()
        profile_annual_kwh_nonzero = profile_annual_kwh_per_unit.where(profile_annual_kwh_per_unit != 0)
        for b in ("1st", "2nd"):
            c = f"AC_Net_kWh_Water_ScaledProfile_{b}_Baseline"
            wm.loc[on_kwh_per_unit_profile, c] = (wm[c] / profile_annual_kwh_nonzero).fillna(0.0)[on_kwh_per_unit_profile]
    water_pricing = run_real_hourly(water_measures, cost_test=cost_test, acc_version=acc_version, base_year=base_year, discount_rate_fn=discount_rate_fn,
                                    market_effects_bens=market_effects_bens, gen_only=True,
                                    quantities=("AC_Net_kWh_Water_ScaledProfile_1st_Baseline", "AC_Net_kWh_Water_ScaledProfile_2nd_Baseline"),
                                    profiles=("AC_Elec_Impact_Profile_Water", "AC_Elec_Impact_Profile_Water"))
    return _sum_by_measure(water_pricing.values, "LifetimeValuePV", "WaterEnergyValue"), water_pricing


def _split_water(value: pd.Series, unit_kwh_water_1st: pd.Series) -> tuple[pd.Series, pd.Series]:
    """CalcCE's WaterEnergyBen / WaterEnergyCost split, decided by the SIGN OF UnitkWhIOUWater1stBaseline (not of the value): a measure with it > 0 books its
    value as a benefit, one with it < 0 books the negated value as a (positive) cost, and 0 books neither."""
    return value.where(unit_kwh_water_1st > 0, 0.0), (-value).where(unit_kwh_water_1st < 0, 0.0)


# CalcCost's undiscounted cost components and the rest of its participant-cost columns (CET_v25.1_documentation_extracted.txt:16672-16800), from engine.price_participant_cost
PARTICIPANT_EXTRA = ["IncentiveToOthers", "DILaborCost", "DIMaterialCost", "EndUserRebate", "GrossMeasureCost", "ExcessIncentives", "MarkEffectPlusExcessInc",
                     "GrossParticipantCost", "GrossParticipantCostAdjusted", "NetParticipantCost", "NetParticipantCostAdjusted", "GrossMeasCostPV", "ExcessIncentivesPV",
                     "GrossParticipantCostAdjustedPV"]


def run_cost_effectiveness(measures: pd.DataFrame, acc_version: int = 2024, base_year: int | None = None,
                           market_effects_bens: float = 0.0, market_effects_cost: float = 0.0):
    """
    Wires up TRCCost/PACCost/TRCRatio/PACRatio (and the ElecBen/
    ElecSupplyCost, GasBen/GasSupplyCost, OtherBen/OtherCost splits they're
    built from) on top of pieces this POC already validates independently:
    `run_real_hourly`/`run_gas_benefit` (the ONE lifetime PV number per
    measure, sign-classified into Ben vs SupplyCost exactly like
    price_bill_impact's Reduc/Incr split -- same real "fuel-sub cost gets
    booked as a positive supply cost, not a negative benefit" mechanism
    documented throughout this project), `run_participant_cost`
    (RebatesandIncentsPV, NetParticipantCostPV), and `price_other_ben_cost`
    (OtherBen/OtherCost).

    OUT OF SCOPE, same as `run_participant_cost` and documented the same
    way in discount_settings.py: the program-level admin/marketing/DI
    cost share (ProgramCost.csv) that would otherwise get added on top --
    genuinely 0 for both this POC's real ground-truth jobs, not an
    approximation being glossed over. WaterEnergyBen / WaterEnergyCost are
    priced (2026-09-20; `run_water_energy`) and enter TSB, the PAC/TRC/RIM costs and
    every ratio.

    RIMCost/RIMRatio (2026-09-17): `RIMCost = LifecycleBillReducElec + LifecycleBillReducGas
    + PACCost`, `RIMRatio = (ElecBen+GasBen+WaterEnergyBen+OtherBen+
    LifecycleBillIncrElec+LifecycleBillIncrGas) / RIMCost` (CET_v25.1_documentation_extracted
    .txt:16115-16168) -- built directly on run_bill_impact's already
    floating-point-exact LifecycleBillReducElec/LifecycleBillIncrElec/LifecycleBillReducGas/
    LifecycleBillIncrGas, so RIMCost/RIMRatio's own accuracy is bounded only by
    PACCost's (already-validated) residual.

    SCBCost/SCHCost/SCBRatio/SCHRatio (societal cost tests) are built by
    the separate `run_societal_cost_effectiveness()` below -- they need a
    parallel SOCIETAL discount rate and SB/SH avoided-cost variants, kept
    out of this function to avoid tripling its already-expensive per-
    measure hourly/monthly runs for callers who only want TRC/PAC/RIM.

    Returns one row per measure: CEInputID, ElecBen, ElecSupplyCost,
    GasBen, GasSupplyCost, OtherBen, OtherCost, TaxCredits, TRCCost,
    PACCost, RIMCost, TRCRatio, PACRatio, RIMRatio.
    """
    if base_year is None:
        base_year = int(measures["Used_Year"].min())

    elec_pricing = run_real_hourly(measures, acc_version=acc_version, base_year=base_year, market_effects_bens=market_effects_bens)
    elec_value = _sum_by_measure(elec_pricing.values, "LifetimeValuePV", "ElecValue")

    gas_pricing, _n_gas, _n_matched = run_gas_benefit(measures, acc_version=acc_version, base_year=base_year, market_effects_bens=market_effects_bens)
    gas_value = _sum_by_measure(gas_pricing, "LifetimeValuePV", "GasValue")
    water_value, water_pricing = run_water_energy(measures, acc_version=acc_version, base_year=base_year, market_effects_bens=market_effects_bens)

    participant_costs = run_participant_cost(measures, acc_version=acc_version, base_year=base_year, market_effects_cost=market_effects_cost)
    other_bens_costs = price_other_ben_cost(measures, market_effects_bens, market_effects_cost)
    # Bill impacts take market effects too: CalcCE prices them on Qty x (NTGRkWh + MEBens) / (NTGRThm + MEBens) (:15885-15974). Confirmed on CET jobs 121842 /
    # 121841: every BillReduc rises by (NTG + 0.05) / NTG. (Until 2026-10-08 the POC left them unscaled.)
    bill_impacts = run_bill_impact(measures, acc_version=acc_version, market_effects_bens=market_effects_bens)

    df = measures[["CEInputID", "Fuel_Sub_Flag"]].merge(elec_value, on="CEInputID", how="left")
    df = df.merge(gas_value, on="CEInputID", how="left")
    df = df.merge(water_value, on="CEInputID", how="left")
    df = df.merge(participant_costs[["CEInputID", "RebatesandIncents", "RebatesandIncentsPV", "NetParticipantCostPV", "MarkEffectPlusExcessIncPV",
                                     "NetParticipantCostAdjustedPV", "GrossParticipantCostPV"] + PARTICIPANT_EXTRA], on="CEInputID", how="left")
    df = df.merge(other_bens_costs, on="CEInputID", how="left")
    df = df.merge(bill_impacts, on="CEInputID", how="left")
    df[["ElecValue", "GasValue", "WaterEnergyValue"]] = df[["ElecValue", "GasValue", "WaterEnergyValue"]].fillna(0.0)

    # Reporting only: a benefit of 0 can mean "no savings" or "the POC could not price this measure" -- say which. Does not touch any value.
    ids = df["CEInputID"]
    measures_by_id = measures.set_index("CEInputID")
    net_kw = measures_by_id[["UnitkW1stBaseline", "UnitkW2ndBaseline"]].mul(measures_by_id["NumUnits"] * measures_by_id["NTGRkW"], axis=0)
    has_elec_savings = ids.map(((measures_by_id[["AC_Net_kWh_1st_Baseline", "AC_Net_kWh_2nd_Baseline", *ELEC_QUANTITIES]] != 0).any(axis=1)
                                | (net_kw != 0).any(axis=1))).astype(bool)
    has_gas_savings = ids.map((measures_by_id[list(GAS_QUANTITIES)] != 0).any(axis=1)).astype(bool)
    route = ids.map(elec_pricing.route).fillna("")
    route_kind = route.str.split(" | ", regex=False).str[0]                       # always "hourly profile" (the 8,760 path is the only electric route)
    profile_source = route.str.split(" | ", regex=False).str[1].fillna(route_kind)   # user profile / library / library (sector-loose match) / ...
    elec_priced = ids.isin(_ids_in(elec_pricing.values))
    unpriced = ids.isin(set(elec_pricing.unpriced_ids))
    df["ElecPricingStatus"] = np.select(
        [~has_elec_savings, unpriced & elec_priced, unpriced, elec_priced],
        ["no electric savings", "PARTLY PRICED: no hourly profile for one baseline's shape", "UNPRICED: no hourly profile", "priced: " + route_kind],
        default="UNPRICED: no value returned")
    df["ProfileSource"] = profile_source.where(elec_priced & has_elec_savings, "")
    # AC_Climate_Zone = System: the real zone it was priced in, and whether that came from the catalog or was calculated for this run (pipeline/system_zones.py)
    is_system_zone = ids.map(measures_by_id["AC_Climate_Zone"]).map(system_zones.is_system).astype(bool) & elec_priced & has_elec_savings
    system_zone_choice = [system_zones.resolve(measures_by_id.at[i, "AC_Elec_Impact_Profile"], measures_by_id.at[i, "IOU_AC_Territory"],
                                               measures_by_id.at[i, "AC_Elec_Target_Sector"]) if s else None for i, s in zip(ids, is_system_zone)]
    df["SystemClimateZone"] = pd.Series([c.zone if c else None for c in system_zone_choice], index=df.index, dtype=object)
    df["SystemZoneSource"] = pd.Series([(c.source if c.zone else "no zone could be priced: System shape and blended prices") if c else None
                                        for c in system_zone_choice], index=df.index, dtype=object)
    gas_priced = ids.isin(_ids_in(gas_pricing))
    df["GasPricingStatus"] = np.select(
        [~has_gas_savings, gas_priced], ["no gas savings", "priced"], default="UNPRICED: no gas savings profile match")
    df["GasProfileSource"] = ids.map(measures_by_id["AC_Gas_Impact_Profile"]).map(
        lambda k: "" if pd.isna(k) else (gas_savings_profiles.source(str(k)) or "")).where(gas_priced & has_gas_savings, "")
    has_water_energy = ids.map((measures_by_id[["UnitkWhIOUWater1stBaseline", "UnitkWhIOUWater2ndBaseline"]] != 0).any(axis=1)).astype(bool)
    df["WaterPricingStatus"] = np.select(
        [~has_water_energy, ids.isin(_ids_in(water_pricing.values))], ["no water energy", "priced"],
        default="UNPRICED: no hourly profile")

    df["ElecBen"], df["ElecSupplyCost"] = _split_ben_supply(df["ElecValue"], df["Fuel_Sub_Flag"])
    df["GasBen"], df["GasSupplyCost"] = _split_ben_supply(df["GasValue"], df["Fuel_Sub_Flag"])
    df["WaterEnergyBen"], df["WaterEnergyCost"] = _split_water(df["WaterEnergyValue"], ids.map(measures_by_id["UnitkWhIOUWater1stBaseline"]))

    supply_and_other_costs = df["ElecSupplyCost"] + df["GasSupplyCost"] + df["WaterEnergyCost"] + df["OtherCost"]
    df["PACCost"] = df["RebatesandIncentsPV"] + supply_and_other_costs
    df["TRCCost"] = df["RebatesandIncentsPV"] + df["NetParticipantCostAdjustedPV"] + supply_and_other_costs
    df["RIMCost"] = df["LifecycleBillReducElec"] + df["LifecycleBillReducGas"] + df["PACCost"]

    total_benefits = df["ElecBen"] + df["GasBen"] + df["WaterEnergyBen"] + df["OtherBen"]
    df["TRCRatio"] = (total_benefits + df["TaxCredits"]) / df["TRCCost"]
    df["PACRatio"] = total_benefits / df["PACCost"]
    df["RIMRatio"] = (total_benefits + df["LifecycleBillIncrElec"] + df["LifecycleBillIncrGas"]) / df["RIMCost"]
    df = df.merge(participant_cost_test(measures, df, acc_version, base_year), on="CEInputID", how="left")

    return df[["CEInputID", "ElecPricingStatus", "ProfileSource", "SystemClimateZone", "SystemZoneSource", "GasPricingStatus", "GasProfileSource", "WaterPricingStatus",
               "ElecBen", "ElecSupplyCost", "GasBen", "GasSupplyCost", "WaterEnergyBen", "WaterEnergyCost",
               "OtherBen", "OtherCost", "TaxCredits", "OtherBenGross", "OtherCostGross", "TaxCreditsGross",
               "RebatesandIncents", "RebatesandIncentsPV", "NetParticipantCostPV", "MarkEffectPlusExcessIncPV", "NetParticipantCostAdjustedPV",
               "GrossParticipantCostPV",
               *PARTICIPANT_EXTRA, "LifecycleBillReducElec", "LifecycleBillIncrElec", "LifecycleBillReducGas", "LifecycleBillIncrGas",
               "LifecycleBillReducElecGross", "LifecycleBillIncrElecGross", "LifecycleBillReducGasGross", "LifecycleBillIncrGasGross", "PCTBen", "PCTCost", "PCTRatio",
               "TRCCost", "PACCost", "RIMCost", "TRCRatio", "PACRatio", "RIMRatio"]]


def participant_cost_test(measures: pd.DataFrame, priced: pd.DataFrame, acc_version: int, base_year: int) -> pd.DataFrame:
    """The Participant Cost Test (CA Standard Practice Manual, Chapter 2), which the CET does not calculate (BRD BR-06.8; on CPUC's CET roadmap). From the
    participant's side, on GROSS savings (free riders are participants too) and without market effects:

        PCTBen   = LifecycleBillReducElecGross + LifecycleBillReducGasGross + RebatesandIncentsPV + TaxCreditsGross / PVF       (bill reductions, incentives, tax credits)
        PCTCost  = GrossParticipantCostPV + RebatesandIncentsPV + LifecycleBillIncrElecGross + LifecycleBillIncrGasGross       (the full measure cost, bill increases)
        PCTRatio = PCTBen / PCTCost

    The gross bill impacts are C9's bill impacts with NTG = 1 (same rates, discounting and fuel-substitution split). PVF is the participant-cost discount factor
    of C8 (the claim quarter at the primary rate). Not yet checked against a CPUC specification of the test: CPUC has not published how the CET will define it."""
    m = measures
    # gross (no NTG) annual quantities, with installation and realization rates, as the net AC_Net_* ones
    gross_quantities = {
        "AC_Gross_kWh_1st_Baseline": m["NumUnits"] * m["Used_UnitkWh1stBaseline"] * m["InstallationRatekWh"] * m["RealizationRatekWh"],
        "AC_Gross_kWh_2nd_Baseline": m["NumUnits"] * m["Used_UnitkWh2ndBaseline"] * m["InstallationRatekWh"] * m["RealizationRatekWh"],
        "AC_Gross_Therms_1st_Baseline": m["NumUnits"] * m["UnitTherm1stBaseline"] * m["InstallationRateTherm"] * m["RealizationRateTherm"],
        "AC_Gross_Therms_2nd_Baseline": m["NumUnits"] * m["UnitTherm2ndBaseline"] * m["InstallationRateTherm"] * m["RealizationRateTherm"],
    }
    gross_bill_impacts = run_bill_impact(m.assign(**gross_quantities), acc_version=acc_version, quantities=tuple(gross_quantities)).rename(
        columns={c: f"{c}Gross" for c in ("LifecycleBillReducElec", "LifecycleBillIncrElec", "LifecycleBillReducGas", "LifecycleBillIncrGas")})
    discount_rate_qtr_by_iou = {iou: discount_settings.get_discount_rate_qtr(iou, acc_version) for iou in m["IOU_AC_Territory"].unique()}
    quarters_from_first_year = (m["Used_Year"] - base_year) * 4 + (m["Used_Quarter"] - 1)                                # CalcCE's Qm
    participant_discount_factor = (1.0 + m["IOU_AC_Territory"].map(discount_rate_qtr_by_iou)) ** quarters_from_first_year     # Rqf ^ Qm
    out = m[["CEInputID"]].assign(_participant_discount_factor=participant_discount_factor.to_numpy()).merge(gross_bill_impacts, on="CEInputID", how="left")
    out = out.merge(priced[["CEInputID", "RebatesandIncentsPV", "GrossParticipantCostPV", "TaxCreditsGross"]], on="CEInputID", how="left")
    gross_cols = ["LifecycleBillReducElecGross", "LifecycleBillIncrElecGross", "LifecycleBillReducGasGross", "LifecycleBillIncrGasGross"]
    out[gross_cols] = out[gross_cols].fillna(0.0)
    out["PCTBen"] = (out["LifecycleBillReducElecGross"] + out["LifecycleBillReducGasGross"] + out["RebatesandIncentsPV"]
                     + out["TaxCreditsGross"] / out["_participant_discount_factor"])
    out["PCTCost"] = out["GrossParticipantCostPV"] + out["RebatesandIncentsPV"] + out["LifecycleBillIncrElecGross"] + out["LifecycleBillIncrGasGross"]
    out["PCTRatio"] = (out["PCTBen"] / out["PCTCost"].where(out["PCTCost"] != 0)).fillna(0.0)
    return out[["CEInputID"] + gross_cols + ["PCTBen", "PCTCost", "PCTRatio"]]


def run_societal_cost_effectiveness(measures: pd.DataFrame, acc_version: int = 2024, base_year: int | None = None,
                                    market_effects_bens: float = 0.0, market_effects_cost: float = 0.0):
    """
    Wires up SCBCost/SCHCost/SCBRatio/SCHRatio (the societal cost tests --
    "Base" and "High" social-cost-of-carbon scenarios), structurally
    identical to run_cost_effectiveness's TRCCost but with two real
    differences confirmed directly in `CalcCE`'s SQL
    (CET_v25.1_documentation_extracted.txt:14460-14916): (1) the avoided-
    cost PV terms feeding `ElecBen_SB`/`ElecBen_SH`/`GasBen_SB`/
    `GasBen_SH` and the participant-cost PV terms both discount with the
    SEPARATE societal quarterly rate (`Rqf_SCT` ->
    discount_settings.get_discount_rate_qtr_sct, genuinely lower than the
    primary rate), not the regular `Rqf`; (2) they price against the
    "SCT Base"/"SCT High" avoided-cost rows instead of "TRC" -- for
    electric, `pipeline/real_avoided_cost_v2.py`'s hourly extraction (SCG
    through its SCE/PG&E routing, like TRC);
    for gas, `pipeline/gas_avoided_cost.py`'s existing `cost_test="SCT"` +
    `scc="Base"`/`"High"` (confirmed real, distinct rows -- 279 each --
    not something this POC had to add).

    OtherBen/OtherCost/TaxCredits are cost-test-invariant (same real
    formula as TRC -- no `_SB`/`_SH` columns exist for them in the real
    schema), reused as-is from `price_other_ben_cost`.

    Runs the full electric/gas pricing pipeline TWICE more (once per SB/SH
    cost test) on top of whatever run_cost_effectiveness already did --
    expensive at 50,000-measure scale, call only when SCB/SCH are actually
    wanted.

    KNOWN, INTENTIONAL DIVERGENCE FROM REAL CET (2026-09-18): this function
    prices electric SCB/SCH on the real SCT Base/SCT High hourly rows for
    every measure, and does NOT copy a CET data error. In CET's production
    quarterly table (`E3AvoidedCostElecCO2Seq2026`), legacy DEER-named end
    uses have Gen_SB == Gen_SH == Gen (TRC values in the SCT columns), so
    real CET's ElecSupplyCost_SB == ElecSupplyCost_SH for those measures and
    the SCC / Air Quality Adder / methane terms have no effect on their
    electric side. That is logged as CLAUDE.md finding 9 and is not
    reproduced here. Expect this function to overshoot real CET on
    DEER-matched measures for PG&E/SCE/SDG&E by roughly SB +1.3-2.1%, SH
    +2.8-4.4% (SWWH025/Job 121074, all DEER-matched); pricing those
    measures on TRC rows with the societal rate instead lands within
    0.2-0.7% of CET (the same band as TRC), which is how the cause was
    isolated. T&D is unaffected (TD_SB == TD_SH == TD everywhere tested).
    Gas SB/SH match exactly (1.0000).
    Ruled out along the way: day-of-week shift, ACC extraction correctness,
    flat-vs-hourly AQ Adder, and discount rate choice (CEDARS vs. ACC). See
    poc/README.md section 7.

    Returns one row per measure: CEInputID, ElecBen_SB, ElecBen_SH,
    ElecSupplyCost_SB, ElecSupplyCost_SH, GasBen_SB, GasBen_SH,
    GasSupplyCost_SB, GasSupplyCost_SH, SCBCost, SCHCost, SCBRatio, SCHRatio.
    """
    if base_year is None:
        base_year = int(measures["Used_Year"].min())
    discount_rate_qtr_sct_fn = discount_settings.get_discount_rate_qtr_sct

    def elec_value_for(cost_test):
        priced = run_real_hourly(measures.reset_index(drop=True), cost_test=cost_test, acc_version=acc_version, base_year=base_year,
                                 discount_rate_fn=discount_rate_qtr_sct_fn, market_effects_bens=market_effects_bens)
        return _sum_by_measure(priced.values, "LifetimeValuePV", "ElecValue")

    def gas_value_for(scc):
        gas_pricing, _n, _m = run_gas_benefit(measures, cost_test="SCT", acc_version=acc_version,
                                              base_year=base_year, scc=scc, discount_rate_fn=discount_rate_qtr_sct_fn,
                                              market_effects_bens=market_effects_bens)
        return _sum_by_measure(gas_pricing, "LifetimeValuePV", "GasValue")

    def water_value_for(cost_test):
        return run_water_energy(measures, cost_test=cost_test, acc_version=acc_version, base_year=base_year, discount_rate_fn=discount_rate_qtr_sct_fn,
                                market_effects_bens=market_effects_bens)[0]

    values = [elec_value_for("SCT Base").rename("ElecValue_SB"), elec_value_for("SCT High").rename("ElecValue_SH"),
              gas_value_for("Base").rename("GasValue_SB"), gas_value_for("High").rename("GasValue_SH"),
              water_value_for("SCT Base").rename("WaterEnergyValue_SB"), water_value_for("SCT High").rename("WaterEnergyValue_SH")]

    participant_costs_sct = run_participant_cost(measures, acc_version=acc_version, base_year=base_year,
                                                 discount_rate_fn=discount_rate_qtr_sct_fn, market_effects_cost=market_effects_cost)
    other_bens_costs = price_other_ben_cost(measures, market_effects_bens, market_effects_cost)

    df = measures[["CEInputID", "Fuel_Sub_Flag"]]
    for v in values:
        df = df.merge(v, on="CEInputID", how="left")
    df = df.merge(participant_costs_sct[["CEInputID", "RebatesandIncentsPV", "NetParticipantCostPV", "NetParticipantCostAdjustedPV", "GrossParticipantCostPV"]],
                  on="CEInputID", how="left")
    df = df.merge(other_bens_costs, on="CEInputID", how="left")
    value_cols = [v.name for v in values]
    df[value_cols] = df[value_cols].fillna(0.0)

    for s in ("_SB", "_SH"):
        df[f"ElecBen{s}"], df[f"ElecSupplyCost{s}"] = _split_ben_supply(df[f"ElecValue{s}"], df["Fuel_Sub_Flag"])
        df[f"GasBen{s}"], df[f"GasSupplyCost{s}"] = _split_ben_supply(df[f"GasValue{s}"], df["Fuel_Sub_Flag"])
    unit_kwh_water_1st = df["CEInputID"].map(measures.set_index("CEInputID")["UnitkWhIOUWater1stBaseline"])
    df["WaterEnergyBen_SB"], df["WaterEnergyCost_SB"] = _split_water(df["WaterEnergyValue_SB"], unit_kwh_water_1st)
    df["WaterEnergyBen_SH"], df["WaterEnergyCost_SH"] = _split_water(df["WaterEnergyValue_SH"], unit_kwh_water_1st)

    rebates_and_incents_pv_sct = df["RebatesandIncentsPV"]
    net_participant_cost_adjusted_pv_sct = df["NetParticipantCostAdjustedPV"]          # with the market-effects term, as the TRC cost
    df["SCBCost"] = (rebates_and_incents_pv_sct + net_participant_cost_adjusted_pv_sct + df["ElecSupplyCost_SB"] + df["GasSupplyCost_SB"]
                     + df["WaterEnergyCost_SB"] + df["OtherCost"])
    df["SCHCost"] = (rebates_and_incents_pv_sct + net_participant_cost_adjusted_pv_sct + df["ElecSupplyCost_SH"] + df["GasSupplyCost_SH"]
                     + df["WaterEnergyCost_SH"] + df["OtherCost"])

    df["SCBRatio"] = (df["ElecBen_SB"] + df["GasBen_SB"] + df["WaterEnergyBen_SB"] + df["OtherBen"]) / df["SCBCost"]
    df["SCHRatio"] = (df["ElecBen_SH"] + df["GasBen_SH"] + df["WaterEnergyBen_SH"] + df["OtherBen"]) / df["SCHCost"]

    return df[["CEInputID", "ElecBen_SB", "ElecBen_SH", "ElecSupplyCost_SB", "ElecSupplyCost_SH",
               "GasBen_SB", "GasBen_SH", "GasSupplyCost_SB", "GasSupplyCost_SH",
               "WaterEnergyBen_SB", "WaterEnergyBen_SH", "WaterEnergyCost_SB", "WaterEnergyCost_SH",
               "RebatesandIncentsPV", "NetParticipantCostPV", "NetParticipantCostAdjustedPV", "GrossParticipantCostPV",
               "SCBCost", "SCHCost", "SCBRatio", "SCHRatio"]]


SOCIETAL_COLUMN_PATTERN = r"(_SB|_SH)$|^SCB|^SCH|_SCT$"


def _first_year_weights(RUL_Yrs: pd.Series, EUL_Yrs: pd.Series) -> tuple[pd.Series, pd.Series]:
    """CalcSavings' share of the first year on the 1st and 2nd baseline (CET_v25.1_documentation_extracted.txt, "First Year Net"): a measure whose RUL or
    EUL is under a year spends only part of its first year on the 1st baseline, and the rest (up to its EUL) on the 2nd. RUL = 1 exactly falls to the
    ELSE branches (1, 0), as in the SQL."""
    rul, eul = RUL_Yrs, EUL_Yrs
    first_year_share_1st_baseline = np.select([rul > 1, (rul > 0) & (rul < 1), (rul == 0) & (eul < 1)], [1.0, rul, eul], default=1.0)
    first_year_share_2nd_baseline = np.select([(rul > 0) & (rul < 1) & (eul < 1), (rul > 0) & (rul < 1) & (eul >= 1)], [eul - rul, 1.0 - rul], default=0.0)
    return pd.Series(first_year_share_1st_baseline, index=rul.index), pd.Series(first_year_share_2nd_baseline, index=rul.index)


def _savings_quantities(m: pd.DataFrame) -> dict[str, pd.Series]:
    """Per fuel: (units_x_ir_x_rr, unit_amount_1st_baseline, unit_amount_2nd_baseline, ntg) -- NumUnits x installation x realization rate, the per-unit
    1st- and 2nd-baseline amounts and the NTG: the pieces every CalcSavings column is built from. kWh is Used_UnitkWh (a kWh-per-unit profile's own annual kWh)."""
    units = m["NumUnits"].astype(float)
    units_x_ir_x_rr_kwh = units * m["InstallationRatekWh"] * m["RealizationRatekWh"]
    return {
        "KWh": (units_x_ir_x_rr_kwh, m["Used_UnitkWh1stBaseline"] + m["UnitkWhIOUWater1stBaseline"], m["Used_UnitkWh2ndBaseline"] + m["UnitkWhIOUWater2ndBaseline"],
                m["NTGRkWh"]),
        "KWhSite": (units_x_ir_x_rr_kwh, m["Used_UnitkWh1stBaseline"], m["Used_UnitkWh2ndBaseline"], m["NTGRkWh"]),
        "KWhWater": (units_x_ir_x_rr_kwh, m["UnitkWhIOUWater1stBaseline"], m["UnitkWhIOUWater2ndBaseline"], m["NTGRkWh"]),
        "KW": (units * m["InstallationRatekW"] * m["RealizationRatekW"], m["UnitkW1stBaseline"], m["UnitkW2ndBaseline"], m["NTGRkW"]),
        "Thm": (units * m["InstallationRateTherm"] * m["RealizationRateTherm"], m["UnitTherm1stBaseline"], m["UnitTherm2ndBaseline"], m["NTGRTherm"]),
    }


def savings_outputs(measures: pd.DataFrame, market_effects_bens: float = 0.0) -> pd.DataFrame:
    """CET's savings quantities per measure (CalcSavings, CET_v25.1_documentation_extracted.txt:20804-21356), gross and net (net = x (NTG + MEB)):

        FirstYear = Q x IR x RR x ( w1 x a1 + w2 x a2 )                           (w1 / w2 from _first_year_weights)
        Lifecycle = Q x IR x RR x EUL x a1                                       when RUL = 0
                  = Q x IR x RR x ( a1 x RUL + a2 x (EUL - RUL) )                 when RUL > 0
        Annual    = Lifecycle / EUL                                              (the lifetime average)

    for a = kWh (site + IOU water: `KWh`), site kWh only (`KWhSite`), IOU water kWh only (`KWhWater`), kW (`KW`; no Lifecycle column) and therms (`Thm`).
    Quantities, not dollars; nothing is priced."""
    m = measures
    rul, eul = m["RUL_Yrs"].astype(float), m["EUL_Yrs"].astype(float)
    first_year_share_1st, first_year_share_2nd = _first_year_weights(rul, eul)
    meb = float(market_effects_bens or 0.0)
    out = {"CEInputID": m["CEInputID"].to_numpy()}
    for name, (units_x_ir_x_rr, unit_amount_1st_baseline, unit_amount_2nd_baseline, ntg) in _savings_quantities(m).items():
        first_year_gross = units_x_ir_x_rr * (first_year_share_1st * unit_amount_1st_baseline + first_year_share_2nd * unit_amount_2nd_baseline)
        lifecycle_gross = units_x_ir_x_rr * pd.Series(np.where(rul > 0, unit_amount_1st_baseline * rul + unit_amount_2nd_baseline * (eul - rul),
                                                               eul * unit_amount_1st_baseline), index=m.index)
        annual_gross = (lifecycle_gross / eul.where(eul != 0)).fillna(0.0)
        ntg_plus_meb = ntg + meb
        out[f"FirstYearGross{name}"], out[f"FirstYearNet{name}"] = first_year_gross.to_numpy(), (first_year_gross * ntg_plus_meb).to_numpy()
        if name != "KW":
            out[f"LifecycleGross{name}"], out[f"LifecycleNet{name}"] = lifecycle_gross.to_numpy(), (lifecycle_gross * ntg_plus_meb).to_numpy()
        out[f"AnnualGross{name}"], out[f"AnnualNet{name}"] = annual_gross.to_numpy(), (annual_gross * ntg_plus_meb).to_numpy()
    order = [f"{p}{gn}{f}" for p in ("FirstYear", "Lifecycle", "Annual") for gn in ("Gross", "Net") for f in ("KWh", "KWhSite", "KWhWater", "KW", "Thm")
             if not (p == "Lifecycle" and f == "KW")]
    return pd.DataFrame(out)[["CEInputID"] + order]


def discounted_savings(measures: pd.DataFrame, acc_version: int, base_year: int, market_effects_bens: float = 0.0) -> pd.DataFrame:
    """CalcCost's discounted savings (DiscountedSavings CTE, CET_v25.1_documentation_extracted.txt:16851-16904), the denominators of the levelized columns:
    the lifetime kWh (site only, no water) and therms, each year discounted at the PA's annual rate and the whole brought back to the run's First Year at the
    quarterly rate:

        A(n) = 1 + (1 - Raf^-(n - 1)) / Ra                       (n years, the first undiscounted: sum of Raf^-t for t = 0 .. n-1)
        DS   = Q x IR x RR x a1 x A(EUL) / Rqf^Qm                                          when RUL = 0
             = Q x IR x RR x ( a1 x A(RUL) + a2 x A(EUL - RUL) / Raf^RUL ) / Rqf^Qm          when RUL > 0
        Net  = DS x (NTG + MEB);  0 when the 1st-baseline amount a1 is 0 or EUL is 0 (as CET)

    Ra / Raf / Rqf are the PA's annual rate, 1 + it and 1 + the quarterly rate; Qm the claim quarter's offset from the First Year. CET's Gross versions differ
    from its Net ones on dual-baseline measures (its gross kWh counts one 2nd-baseline year too many, its gross therms leave the 2nd baseline undiscounted;
    finding C-3); ReCET's Gross is the Net formula without the NTG, so the two agree whenever NTG + MEB = 1."""
    m = measures
    if m.empty:
        return pd.DataFrame(columns=["CEInputID", "DiscountedSavingsGrosskWh", "DiscountedSavingsNetkWh", "DiscountedSavingsGrossThm", "DiscountedSavingsNetThm"])
    ious = m["IOU_AC_Territory"]
    DiscountRateAnnual = ious.map({i: discount_settings.get_discount_rate_annual(i, acc_version) for i in ious.unique()}).astype(float)
    DiscountRateQtr = ious.map({i: discount_settings.get_discount_rate_qtr(i, acc_version) for i in ious.unique()}).astype(float)
    annual_discount_factor = 1.0 + DiscountRateAnnual                                                       # CalcCE's Raf
    quarters_from_first_year = (m["Used_Year"] - base_year) * 4 + (m["Used_Quarter"] - 1)                  # CalcCE's Qm
    first_year_discount_factor = (1.0 + DiscountRateQtr) ** quarters_from_first_year                         # Rqf ^ Qm
    rul, eul = m["RUL_Yrs"].astype(float), m["EUL_Yrs"].astype(float)

    def discounted_year_count(n):
        """sum of Raf^-t for t = 0 .. n-1: n years, the first undiscounted"""
        return 1.0 + (1.0 - annual_discount_factor ** -(n - 1.0)) / DiscountRateAnnual

    out = {"CEInputID": m["CEInputID"].to_numpy()}
    quantities = _savings_quantities(m)
    meb = float(market_effects_bens or 0.0)
    for name, key in (("kWh", "KWhSite"), ("Thm", "Thm")):
        units_x_ir_x_rr, amount_1st, amount_2nd, ntg = quantities[key]
        dual_baseline_sum = amount_1st * discounted_year_count(rul) + amount_2nd * discounted_year_count(eul - rul) / annual_discount_factor ** rul
        single_baseline_sum = amount_1st * discounted_year_count(eul)
        gross = units_x_ir_x_rr * pd.Series(np.where(rul > 0, dual_baseline_sum, single_baseline_sum), index=m.index) / first_year_discount_factor
        gross = gross.where((amount_1st != 0) & (eul != 0) & eul.notna(), 0.0).fillna(0.0)
        out[f"DiscountedSavingsGross{name}"] = gross.to_numpy()
        out[f"DiscountedSavingsNet{name}"] = (gross * (ntg + meb)).to_numpy()
    return pd.DataFrame(out)[["CEInputID", "DiscountedSavingsGrosskWh", "DiscountedSavingsNetkWh", "DiscountedSavingsGrossThm", "DiscountedSavingsNetThm"]]


def run_full_cost_effectiveness(measures: pd.DataFrame, program_costs: pd.DataFrame | None = None,
                                acc_version: int = 2024, base_year: int | None = None,
                                market_effects_bens: float = 0.0, market_effects_cost: float = 0.0, include_societal: bool = True):
    """
    market_effects_bens / market_effects_cost: CET's run-level Market Effects (MarketEffectBens / MarketEffectCost, 0 or 0.05). Benefits use
    NTG + MEBens (the Gross columns divide by NTG + MEBens too), costs use MECost. (The window, command line and RunConfig set both from one option,
    jobs.RunConfig.market_effects; the two stay separate here because CET keeps them separate.) Not validated against a CET run made with a nonzero setting.
    include_societal=False skips the two societal pricing passes (about two thirds of the run time); every SB/SH/SCB/SCH column is then blank (NaN).

    Every cost-effectiveness output in one table: the regular TRC/PAC/RIM/SCB/
    SCH costs and ratios plus their Gross and NoAdmin variants, Gross benefits
    and supply costs, TotalSystemBenefit(+Gross) and the program-cost weighting
    (pipeline/cost_tests.py -- see its docstring for definitions and the CET
    quirks deliberately not copied). `program_costs` is a ProgramCost.csv
    DataFrame (cost_tests.load_program_costs); None means no program costs, so
    NoAdmin == regular. Runs the full hourly pipeline for TRC and both societal
    tests -- several minutes at 50,000 measures.
    """
    if base_year is None:
        base_year = int(measures["Used_Year"].min())
    d = price_measure_values(measures, acc_version=acc_version, base_year=base_year, market_effects_bens=market_effects_bens,
                             market_effects_cost=market_effects_cost, include_societal=include_societal)
    return apply_program_costs(d, measures, program_costs, acc_version=acc_version, base_year=base_year, market_effects_bens=market_effects_bens,
                               include_societal=include_societal)


def price_measure_values(measures: pd.DataFrame, acc_version: int, base_year: int, market_effects_bens: float = 0.0, market_effects_cost: float = 0.0,
                         include_societal: bool = True) -> pd.DataFrame:
    """The measure-level half of run_full_cost_effectiveness: every value that depends on the ACC (benefits, supply costs, participant PVs, bill impacts, the
    societal variants), one row per measure, before program costs are shared out. Measures priced on different ACC versions (BRD BR-06.5) are priced here
    one group at a time, each under its own ACC, and the groups are joined before apply_program_costs."""
    trc_values = run_cost_effectiveness(measures, acc_version=acc_version, base_year=base_year,
                                        market_effects_bens=market_effects_bens, market_effects_cost=market_effects_cost)
    if include_societal:
        sct_values = run_societal_cost_effectiveness(measures, acc_version=acc_version, base_year=base_year,
                                                     market_effects_bens=market_effects_bens, market_effects_cost=market_effects_cost)
    else:   # placeholders so the shared cost-test arithmetic runs; every societal column is blanked again below
        sct_values = measures[["CEInputID"]].assign(**{c: 0.0 for c in (
            "ElecBen_SB", "ElecBen_SH", "ElecSupplyCost_SB", "ElecSupplyCost_SH", "GasBen_SB", "GasBen_SH", "GasSupplyCost_SB", "GasSupplyCost_SH",
            "WaterEnergyBen_SB", "WaterEnergyBen_SH", "WaterEnergyCost_SB", "WaterEnergyCost_SH",
            "RebatesandIncentsPV", "NetParticipantCostPV", "NetParticipantCostAdjustedPV", "GrossParticipantCostPV", "SCBCost", "SCHCost", "SCBRatio",
            "SCHRatio")})
    sct_values = sct_values.rename(columns={c: f"{c}_SCT" for c in ("RebatesandIncentsPV", "NetParticipantCostPV", "NetParticipantCostAdjustedPV",
                                                                    "GrossParticipantCostPV")})
    sct_values = sct_values.drop(columns=["SCBCost", "SCHCost", "SCBRatio", "SCHRatio"])
    trc_values = trc_values.drop(columns=["TRCCost", "PACCost", "RIMCost", "TRCRatio", "PACRatio", "RIMRatio"])
    measure_values = measures[["CEInputID", "PrgID"]].copy()
    measure_values["NTGRkWh_plus_MEBens"] = measures["NTGRkWh"] + market_effects_bens          # the Gross columns undo the NTG that the benefit was scaled by
    measure_values["NTGRTherm_plus_MEBens"] = measures["NTGRTherm"] + market_effects_bens
    discounted_savings_values = discounted_savings(measures, acc_version, base_year, market_effects_bens)   # the levelized columns' denominators, at this ACC's rates
    return (measure_values.merge(trc_values, on="CEInputID").merge(sct_values, on="CEInputID")
            .merge(discounted_savings_values, on="CEInputID", how="left"))


def program_share_totals(d: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Each program's totals of the amounts its program-level shares divide up (the benefits behind WeightedBenefits and the cost tests' program-cost shares),
    over `d` (price_measure_values). A chunked run adds these up over its chunks
    (cost_tests.combine_share_totals) and passes the sum to apply_program_costs, so every measure gets the share it has in the whole job."""
    return cost_tests.share_totals(cost_tests.share_inputs(d), d["PrgID"])


def apply_program_costs(d: pd.DataFrame, measures: pd.DataFrame, program_costs: pd.DataFrame | None, acc_version: int, base_year: int,
                        market_effects_bens: float = 0.0, include_societal: bool = True, totals: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    """The program-level half: share each program's costs over its measures (all of them, whatever ACC version priced them) and finish every cost test.
    Program costs are discounted with the run's own ACC version's rates (`acc_version`). `totals` (program_share_totals over the whole job) is for a run that
    passes the job in parts; None = `d` is the whole job."""
    prog = cost_tests.program_cost_sums(program_costs, base_year, acc_version)
    out = cost_tests.compute_cost_tests(d, prog, totals)
    out = out.merge(savings_outputs(measures, market_effects_bens), on="CEInputID", how="left")
    if not include_societal:
        out[[c for c in out.columns if re.search(SOCIETAL_COLUMN_PATTERN, c)]] = float("nan")
    return out


def apply_scaled_profiles(measures: pd.DataFrame, allocated: bool = False) -> pd.DataFrame:
    """Measures priced on a user 8,760 profile that is already scaled -- kWh per unit per hour, `profile_basis = kwh_per_unit` (BRD BR-06.6). Such a profile is the
    unadjusted gross kWh per unit, so it is priced with NumUnits x NTG x IR x RR: for a baseline priced on it

        AC_Net_kWh_ScaledProfile(_1st/_2nd)_Baseline = NumUnits x NTGRkWh x InstallationRatekWh x RealizationRatekWh       (the profile itself carries the kWh)

    and its annual kWh per unit is the profile's own sum S (0 for a load-shifting-only profile), which replaces UnitkWh1stBaseline (UnitkWh2ndBaseline) in
    Used_UnitkWh1stBaseline (Used_UnitkWh2ndBaseline), so the gross quantities (Q x IR x RR x S: first-year, lifecycle, the PCT's gross bill impacts) and the net
    ones (x NTG: AC_Net_kWh_*_Baseline, bill impacts) follow the profile. Scaled_Profile_Flag marks those measures. The 2nd baseline is priced on its own profile
    (AC_Elec_Impact_Profile_2nd_Baseline) when it has one; when it shares the 1st baseline's scaled profile it keeps the file's ratio r2 = UnitkWh2ndBaseline /
    UnitkWh1stBaseline (1 when UnitkWh1stBaseline is 0): its multiplier is the 1st's x r2 and its kWh S x r2. Baselines on any other profile are unchanged. With
    statewide allocation (`allocated`), an allocated row is multiplied by its electric factor, as its UnitkWh fields were."""
    if real_impact_profiles.user_profiles() is None or measures.empty:
        return measures

    def profile_kwh_per_unit(profiles):
        zones = [system_zones.priced_zone(k, iou, ts, cz) for k, iou, ts, cz in
                 zip(profiles, measures["IOU_AC_Territory"], measures["AC_Elec_Target_Sector"], measures["AC_Climate_Zone"])]
        return pd.Series([real_impact_profiles.scaled_profile(k, iou, ts, z) for k, iou, ts, z in
                          zip(profiles, measures["IOU_AC_Territory"], measures["AC_Elec_Target_Sector"], zones)], index=measures.index, dtype=float)
    profile_kwh_per_unit_1st = profile_kwh_per_unit(measures["AC_Elec_Impact_Profile"])
    same_profile_both_baselines = measures["AC_Elec_Impact_Profile_2nd_Baseline"] == measures["AC_Elec_Impact_Profile"]
    profile_kwh_per_unit_2nd = profile_kwh_per_unit_1st.where(same_profile_both_baselines, profile_kwh_per_unit(measures["AC_Elec_Impact_Profile_2nd_Baseline"]))
    if profile_kwh_per_unit_1st.isna().all() and profile_kwh_per_unit_2nd.isna().all():
        return measures
    m = measures.copy()
    statewide_electric_factor = (statewide_allocation.electric_factors(m["PrgID"], m["Used_Year"]) if allocated else pd.Series(1.0, index=m.index))
    filed_1st, filed_2nd = m["UnitkWh1stBaseline"].astype(float), m["UnitkWh2ndBaseline"].astype(float)
    ratio_2nd_to_1st_baseline_kwh = (filed_2nd / filed_1st.where(filed_1st != 0)).fillna(1.0)
    profile_scale_kwh_per_unit = m["NumUnits"] * statewide_electric_factor * m["NTGRkWh"] * m["InstallationRatekWh"] * m["RealizationRatekWh"]
    on_profile_1st = profile_kwh_per_unit_1st.notna()
    m.loc[on_profile_1st, "Used_UnitkWh1stBaseline"] = (profile_kwh_per_unit_1st * statewide_electric_factor)[on_profile_1st]
    m.loc[on_profile_1st, "AC_Net_kWh_ScaledProfile_1st_Baseline"] = profile_scale_kwh_per_unit[on_profile_1st]
    on_profile_2nd_shared = same_profile_both_baselines & on_profile_1st   # the 2nd baseline on the same scaled profile: the file's 2nd / 1st ratio
    m.loc[on_profile_2nd_shared, "Used_UnitkWh2ndBaseline"] = (profile_kwh_per_unit_1st * statewide_electric_factor * ratio_2nd_to_1st_baseline_kwh)[on_profile_2nd_shared]
    m.loc[on_profile_2nd_shared, "AC_Net_kWh_ScaledProfile_2nd_Baseline"] = (profile_scale_kwh_per_unit * ratio_2nd_to_1st_baseline_kwh)[on_profile_2nd_shared]
    on_profile_2nd_own = ~same_profile_both_baselines & profile_kwh_per_unit_2nd.notna()   # the 2nd baseline on its own scaled profile
    m.loc[on_profile_2nd_own, "Used_UnitkWh2ndBaseline"] = (profile_kwh_per_unit_2nd * statewide_electric_factor)[on_profile_2nd_own]
    m.loc[on_profile_2nd_own, "AC_Net_kWh_ScaledProfile_2nd_Baseline"] = profile_scale_kwh_per_unit[on_profile_2nd_own]
    unit_kwh_from_profile = on_profile_1st | on_profile_2nd_shared | on_profile_2nd_own
    for b in ("1st", "2nd"):
        m.loc[unit_kwh_from_profile, f"AC_Net_kWh_{b}_Baseline"] = (m["NumUnits"] * m[f"Used_UnitkWh{b}Baseline"] * m["NTGRkWh"] * m["InstallationRatekWh"]
                                                                    * m["RealizationRatekWh"])[unit_kwh_from_profile]
    m.loc[unit_kwh_from_profile, "Scaled_Profile_Flag"] = True
    return m


def load_job(measure_csv, program_cost_csv=None, allocate_statewide: bool = False, dtypes: dict | None = None):
    """Load one CET job's inputs, optionally splitting its statewide (SW_) programs across the IOUs -- BOTH files together.

    allocate_statewide=True (pipeline/statewide_allocation.py, the inputs route): each SW program's measures are split by the per-fuel
    factors (kWh by the electric factor, therms by the gas factor, ...) and its ProgramCost rows by the per-field rules, with `<IOU>_`
    prepended to CEInputID and PrgID and PA set to the IOU on program-cost rows. Program costs then discount at each IOU's own rate
    and are shared only among that IOU's own measures. Allocating only one of the two files would leave the PrgIDs unmatched, so this
    is the single entry point; misalignment is reported by cost_tests.check_program_alignment and raised here.

    dtypes: the Measure.csv column types to read with (a part of a larger file: see data_loading.load_measures_from_csv).

    Returns (measures, program_costs or None)."""
    measures = apply_scaled_profiles(data_loading.load_measures_from_csv(Path(measure_csv), allocate_statewide=allocate_statewide, dtypes=dtypes),
                                     allocated=allocate_statewide)
    program = cost_tests.load_program_costs(Path(program_cost_csv), allocate_statewide=allocate_statewide) if program_cost_csv else None
    if program is not None:
        gap = cost_tests.check_program_alignment(measures, program)
        if gap["measure_programs_without_costs"] or gap["program_costs_without_measures"]:
            raise ValueError(f"PrgIDs do not line up between the measure and program-cost files: {gap}")
    return measures, program
