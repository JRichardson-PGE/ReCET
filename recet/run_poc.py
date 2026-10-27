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
from .pipeline.engine import (
    BatchedPricingResult,
    price_bill_impact,
    price_gas_benefit,
    price_other_ben_cost,
    price_participant_cost,
    price_real_hourly,
)

def _ids_in(df: pd.DataFrame | None) -> set:
    """The measure_ids present in a pricing result; empty when nothing was priced (such a frame has no columns at all)."""
    return set() if df is None or df.empty else set(df["measure_id"].unique())          # unique first: a per-measure engine frame has a row per quarter


ROUTE_COLUMNS = ["shape_key", "PA", "target_sector", "climate_zone"]   # what decides a measure's electric shape and avoided-cost series


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


def split_by_baseline_shape(measures: pd.DataFrame) -> pd.DataFrame:
    """A measure whose 2nd-baseline years have their own electric shape (shape_key_2nd, from AC_Elec_Impact_Profile_2nd_Baseline) becomes two rows with the same measure_id:
    the 1st-baseline part on its own shape (2nd-baseline kWh set to 0) and the 2nd-baseline part on the 2nd shape (1st-baseline kWh, gas and water set to 0).
    The baseline phasing is linear in the two quantities, so the two parts add up to the measure; with one shape the measure is left as one row. Only early
    replacements (0 < RUL < EUL) have a 2nd baseline to split. Sum results by measure_id afterwards."""
    if "shape_key_2nd" not in measures.columns:
        return measures
    rul, eul = measures["rul_years_exact"], measures["eul_years_exact"]
    split = (measures["shape_key_2nd"] != measures["shape_key"]) & (rul > 0) & (rul < eul)
    if not split.any():
        return measures
    first, second = measures[split].copy(), measures[split].copy()
    for c in ("shape_scale_2nd", "unit_kwh_2nd", "annual_net_kwh_2nd"):
        first[c] = 0.0
    second["shape_key"] = second["shape_key_2nd"]
    for c in ("shape_scale", "unit_kwh_1st", "annual_net_kwh_1st", "annual_net_kwh", "unit_therm_1st", "unit_therm_2nd", "annual_net_therms_1st",
              "annual_net_therms_2nd", "annual_net_therms", "kwh_water_1st", "kwh_water_2nd", "water_scale_1st", "water_scale_2nd"):
        if c in second.columns:
            second[c] = 0.0
    out = pd.concat([measures[~split], first, second])
    return out.sort_index(kind="stable").reset_index(drop=True)         # each measure where it was, its 1st-baseline part before its 2nd


def _sum_by_measure(df: pd.DataFrame, value_col: str, name: str) -> pd.Series:
    """Lifetime value per measure_id. An empty result (no measure of this fuel priced -- e.g. an electric-only job has no gas rows) is an empty Series that still
    carries the measure_id index, so it merges like any other."""
    if df is None or df.empty:
        return pd.Series(dtype=float, name=name, index=pd.Index([], name="measure_id"))
    return df.groupby("measure_id")[value_col].sum().rename(name)


def _apply_market_effects_electric(measures: pd.DataFrame, meb: float) -> pd.DataFrame:
    """A copy of `measures` with the electric benefit's net-to-gross factor NTGRkWh replaced by NTGRkWh + MEBens (CET's run-level Market Effects, 0% or 5%).
    The hourly pricer reads the NTG-baked `shape_scale`/`shape_scale_2nd`, so those (and `annual_net_kwh_*`, kept consistent) are scaled by (NTG+MEB)/NTG.
    A measure with NTG = 0 is left alone (its net value is 0 and gross is not recoverable). Costs are not touched: see `run_participant_cost`."""
    m = measures.copy()
    if meb:
        ntg = m["ntgr_kwh"]
        factor = ((ntg + meb) / ntg).where(ntg > 0, 1.0)
        for c in ("shape_scale", "shape_scale_2nd", "annual_net_kwh_1st", "annual_net_kwh_2nd", "annual_net_kwh"):
            m[c] = m[c] * factor
    return m


def _apply_market_effects_gas(measures: pd.DataFrame, meb: float) -> pd.DataFrame:
    """Same for gas: NTGRThm -> NTGRThm + MEBens on the NTG-baked annual net therms (CalcCE: Qty x (NTGRThm + MEBens) x IRThm x RRThm x Thm x Gas)."""
    m = measures.copy()
    if meb:
        ntg = m["ntgr_therm"]
        factor = ((ntg + meb) / ntg).where(ntg > 0, 1.0)
        for c in ("annual_net_therms_1st", "annual_net_therms_2nd", "annual_net_therms"):
            m[c] = m[c] * factor
    return m


def run_real_hourly(measures: pd.DataFrame, cost_test: str = "TRC", acc_version: int = 2024,
                     base_year: int | None = None,
                     discount_rate_fn=discount_settings.get_discount_rate_qtr,
                     market_effects_bens: float = 0.0, component: str | None = None,
                     gen_only: bool = False):
    """
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

    base_year defaults to the MIN real claim_year across `measures` -- this
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
    (collected into the returned BatchedPricingResult's
    `unmatched_measure_ids`) and excluded from pricing, never priced
    against a fabricated shape.

    discount_rate_fn (2026-09-17, for SCB/SCH): defaults to the primary
    quarterly rate, but the real societal cost tests discount their own
    avoided-cost PV term with a genuinely different rate
    (`discount_settings.get_discount_rate_qtr_sct`) -- pass that in when
    cost_test is "SCT Base"/"SCT High" so this function's discounting
    matches which cost test it's actually pricing.

    Returns the BatchedPricingResult.
    """
    measures = _apply_market_effects_electric(measures, market_effects_bens)
    if base_year is None:
        base_year = int(measures["claim_year"].min())

    other_measures = split_by_baseline_shape(measures.reset_index(drop=True))     # a separate 2nd-baseline shape prices as a second part
    # AC_Climate_Zone = System is priced in the profile's System zone (pipeline/system_zones.py): its shape AND its prices come from that real zone
    other_measures["price_zone"] = [system_zones.priced_zone(k, pa, ts, cz) for k, pa, ts, cz in
                                    zip(other_measures["shape_key"], other_measures["PA"], other_measures["target_sector"], other_measures["climate_zone"])]

    match_cache: dict[tuple, tuple | None] = {}   # (shape key, PA, sector, zone) -> (shape, represents_year, source) or None: one library / user-profile lookup per route

    def get_shape(m):
        key = (m["shape_key"], m["PA"], m["target_sector"], m["price_zone"])
        if key not in match_cache:
            match_cache[key] = real_impact_profiles.resolve_shape(m["shape_key"], m["PA"], m["target_sector"], m["price_zone"])   # (shape, year, source)
        hit = match_cache[key]
        if hit is None:
            return None
        return hit[0], hit[1]

    acc_cache: dict[tuple, dict] = {}

    def get_acc_years(m):
        key = (m["PA"], m["price_zone"])
        if key not in acc_cache:
            if gen_only:
                acc_cache[key] = real_avoided_cost_v2.get_gen_td_series(m["PA"], m["price_zone"], cost_test)[0]
            else:
                series, _used_blended = real_avoided_cost_v2.get_acc_series(m["PA"], m["price_zone"], cost_test, component)
                acc_cache[key] = series
        return acc_cache[key]

    rate_cache: dict[str, float] = {}

    def get_discount_rate_qtr(m):
        pa = m["PA"]
        if pa not in rate_cache:
            rate_cache[pa] = discount_rate_fn(pa, acc_version)
        return rate_cache[pa]

    batched = price_real_hourly(
        other_measures, get_shape, get_acc_years, real_impact_profiles.shift_for_year,
        get_discount_rate_qtr, base_year,
        route_key=_route_keys(other_measures, ROUTE_COLUMNS),                # shape / ACC series / rate are looked up once per route, not once per measure
    )

    hourly_source = [(match_cache.get(k) or (None, None, ""))[2] for k in zip(other_measures["shape_key"], other_measures["PA"], other_measures["target_sector"],
                                                                          other_measures["price_zone"])]      # where each measure's 8,760 shape came from (user profile / library ...)
    batched.route = {}
    for mid, src in zip(other_measures["measure_id"], hourly_source):        # a measure split by baseline reports its 1st-baseline shape
        batched.route.setdefault(mid, f"hourly profile | {src}")
    return batched


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

    Only measures with a nonzero annual_net_therms AND a real matching gas
    shape are priced -- most of the small 5-measure sample has no real gas
    savings at all (only electric), so an empty or near-empty result there
    is expected, not a bug.

    Returns (gas_result_df, n_gas_measures, n_matched_shape).
    """
    if base_year is None:
        base_year = int(measures["claim_year"].min())
    measures = _apply_market_effects_gas(measures, market_effects_bens)

    shape_cache: dict[str, np.ndarray | None] = {}

    def get_gas_shape(m):
        key = m["gas_profile_key"]
        if pd.isna(key):
            return None
        if key not in shape_cache:
            shape_cache[key] = gas_savings_profiles.get_shape(key)
        return shape_cache[key]

    acc_cache: dict[tuple, dict[int, np.ndarray]] = {}

    def get_gas_acc_years(m):
        key = (m["PA"], m["gas_sector"], m["gas_profile_key"])
        if key not in acc_cache:
            acc_cache[key] = gas_avoided_cost.get_years_for_measure(
                m["PA"], m["gas_sector"], m["gas_profile_key"], cost_test, scc, component
            )
        return acc_cache[key]

    rate_cache: dict[str, float] = {}

    def get_discount_rate_qtr(m):
        pa = m["PA"]
        if pa not in rate_cache:
            rate_cache[pa] = discount_rate_fn(pa, acc_version)
        return rate_cache[pa]

    has_gas_savings = (measures["annual_net_therms_1st"] != 0) | (measures["annual_net_therms_2nd"] != 0)
    n_gas_measures = int(has_gas_savings.sum())
    n_matched_shape = int(measures.loc[has_gas_savings, "gas_profile_key"]
                           .apply(lambda k: not pd.isna(k) and gas_savings_profiles.get_shape(k) is not None).sum())

    result = price_gas_benefit(measures, get_gas_shape, get_gas_acc_years, get_discount_rate_qtr, base_year,
                               route_key=_route_keys(measures, ["gas_profile_key", "PA", "gas_sector"]))   # profile / gas ACC / rate looked up once per route
    return result, n_gas_measures, n_matched_shape


def run_bill_impact(measures: pd.DataFrame, acc_version: int = 2024):
    """
    Wires up the real CET retail rate schedules (pipeline/rate_schedules.py)
    for the RIM test's bill-impact columns -- see engine.price_bill_impact's
    docstring for the full mechanics (annual, not quarterly; sign-based
    Reduc/Incr classification).

    Returns a DataFrame with one row per measure: measure_id, LifecycleBillReducElec,
    LifecycleBillIncrElec, LifecycleBillReducGas, LifecycleBillIncrGas -- NOT a measure_year-per-quarter
    series like this POC's other run_* functions, since the real quantity
    itself is a single lifetime PV number per measure (see price_bill_impact).
    """
    elec_cache: dict[tuple, dict[int, float] | None] = {}

    def get_elec_rate_years(m):
        key = (m["PA"], m["target_sector"], acc_version)
        if key not in elec_cache:
            elec_cache[key] = rate_schedules.get_elec_rate_years(m["PA"], m["target_sector"], acc_version)
        return elec_cache[key]

    gas_cache: dict[tuple, dict[int, float] | None] = {}

    def get_gas_rate_years(m):
        key = (m["PA"], m["gas_sector"], acc_version)
        if key not in gas_cache:
            gas_cache[key] = rate_schedules.get_gas_rate_years(m["PA"], m["gas_sector"], acc_version)
        return gas_cache[key]

    rate_cache: dict[str, float] = {}

    def get_discount_rate_annual(m):
        pa = m["PA"]
        if pa not in rate_cache:
            rate_cache[pa] = discount_settings.get_discount_rate_annual(pa, acc_version)
        return rate_cache[pa]

    return price_bill_impact(measures, get_elec_rate_years, get_gas_rate_years, get_discount_rate_annual,
                             settings_base_year=discount_settings.get_settings("PGE", acc_version)["settings_base_year"])


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
        base_year = int(measures["claim_year"].min())

    rate_cache: dict[str, float] = {}

    def get_discount_rate_qtr(m):
        pa = m["PA"]
        if pa not in rate_cache:
            rate_cache[pa] = discount_rate_fn(pa, acc_version)
        return rate_cache[pa]

    out = price_participant_cost(measures, get_discount_rate_qtr, base_year, market_effects_cost=market_effects_cost)
    # Two participant costs, as CET exports them (CalcCost, CET_v25.1_documentation_extracted.txt:16820-16828): NetParticipantCostPV WITHOUT the market-effects
    # term and NetParticipantCostAdjustedPV = NetParticipantCostPV + MarkEffectPlusExcessIncPV. The TRC / SCB / SCH costs use the adjusted one (it is CalcCE's
    # internal NetParticipantCostPV, :15127-15134). Confirmed on CET jobs 121842 (0%) / 121841 (5%), 2026-10-08: the exported NetParticipantCostPV is identical in
    # both, NetParticipantCostAdjustedPV and TRCCost rise by exactly MarkEffectPlusExcessIncPV. (Until then the POC overwrote NetParticipantCostPV with the adjusted
    # value, so its column differed from CET's whenever market effects were on; TRCCost was right.)
    return out


def _split_ben_supply(value: pd.Series, is_fuel_sub: pd.Series) -> tuple[pd.Series, pd.Series]:
    """CalcCE's Ben / SupplyCost split. Fuel-substitution measures (AllElec-New, FuelSub-ToElec, FuelSub-ToGas) book a positive
    value as benefit and a negative one as a positive supply cost; every other measure keeps its SIGNED value in the benefit
    column and has no supply cost (ISNULL(ft.FuelType,0)='0' branch of CalcCE's ElecBen/GasBen and its
    `NOT ISNULL(ft.FuelType,0)='0'` guard on ElecSupplyCost/GasSupplyCost). The old unconditional clip was only correct for
    all-fuel-sub filings (SWWH025) and put every non-fuel-sub measure's negative savings into supply cost."""
    is_fuel_sub = is_fuel_sub.fillna(False).astype(bool)
    ben = value.where(~is_fuel_sub, value.clip(lower=0.0))
    supply = (-value).clip(lower=0.0).where(is_fuel_sub, 0.0)
    return ben, supply


def run_water_energy(measures: pd.DataFrame, cost_test: str = "TRC", acc_version: int = 2024, base_year: int | None = None,
                     discount_rate_fn=discount_settings.get_discount_rate_qtr, market_effects_bens: float = 0.0):
    """Lifetime PV of the WATER ENERGY of every measure that has any (UnitkWhIOUWater1st/2ndBaseline != 0): the IOU-supplied energy embedded in the water a measure
    saves (positive) or uses (negative), priced exactly like the electric benefit but with kWhWater1/2 in place of kWh1/2 and the Gen term only (CalcCE's
    WaterEnergyBen: Qty x (NTGRkWh + MEBens) x IRkWh x RRkWh x (kWhWater1 x Gen_1 + kWhWater2 x Gen_2)). The measure's own electric end-use profile and climate zone
    carry over, on the 8,760 path like the electric benefit, but on the Gen term ALONE: unlike ElecBen there is no T&D term, so total avoided cost minus
    transmission and distribution (run_real_hourly gen_only). Returns (signed value per measure_id named `water_value`, the BatchedPricingResult for the status columns)."""
    w = measures[(measures["kwh_water_1st"] != 0) | (measures["kwh_water_2nd"] != 0)]
    if w.empty:
        return _sum_by_measure(None, "annual_value", "water_value"), BatchedPricingResult(
            measure_year=pd.DataFrame(columns=["measure_id", "annual_value"]), portfolio_hourly=np.zeros(8760), n_batches=0,
            elapsed_seconds=0.0, batch_size=0)
    w = w.reset_index(drop=True).copy()
    w["shape_scale"], w["shape_scale_2nd"] = w["water_scale_1st"], w["water_scale_2nd"]
    if "shape_key_water" in w.columns:                                     # AC_Elec_Impact_Profile_Water (blank = the measure's own shape), for both baselines
        w["shape_key"] = w["shape_key_2nd"] = w["shape_key_water"]
    if real_impact_profiles.user_profiles() is not None:
        # a kWh-per-unit profile (apply_scaled_profiles) is in kWh, not shares: divide by its annual kWh S so the water energy is spread by its shares.
        # A load-shifting-only profile (S = 0) has no shares, so its water energy is not priced (preflight warns: zero_net_profile_water).
        zones = [system_zones.priced_zone(k, pa, ts, cz) for k, pa, ts, cz in zip(w["shape_key"], w["PA"], w["target_sector"], w["climate_zone"])]
        annual = pd.Series([real_impact_profiles.scaled_profile(k, pa, ts, z) for k, pa, ts, z in zip(w["shape_key"], w["PA"], w["target_sector"], zones)],
                           index=w.index, dtype=float)
        scaled = annual.notna()
        div = annual.where(annual != 0)
        for c in ("shape_scale", "shape_scale_2nd"):
            w.loc[scaled, c] = (w[c] / div).fillna(0.0)[scaled]
    batched = run_real_hourly(w, cost_test=cost_test, acc_version=acc_version, base_year=base_year, discount_rate_fn=discount_rate_fn,
                                 market_effects_bens=market_effects_bens, gen_only=True)
    return _sum_by_measure(batched.measure_year, "annual_value", "water_value"), batched


def _split_water(value: pd.Series, kwh_water_1st: pd.Series) -> tuple[pd.Series, pd.Series]:
    """CalcCE's WaterEnergyBen / WaterEnergyCost split, decided by the SIGN OF THE 1ST-BASELINE water kWh (not of the value): a measure with kWhWater1 > 0 books its
    value as a benefit, one with kWhWater1 < 0 books the negated value as a (positive) cost, and kWhWater1 = 0 books neither."""
    return value.where(kwh_water_1st > 0, 0.0), (-value).where(kwh_water_1st < 0, 0.0)


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

    Returns one row per measure: measure_id, ElecBen, ElecSupplyCost,
    GasBen, GasSupplyCost, OtherBen, OtherCost, TaxCredits, TRCCost,
    PACCost, RIMCost, TRCRatio, PACRatio, RIMRatio.
    """
    if base_year is None:
        base_year = int(measures["claim_year"].min())

    batched = run_real_hourly(measures, acc_version=acc_version, base_year=base_year, market_effects_bens=market_effects_bens)
    elec_value = _sum_by_measure(batched.measure_year, "annual_value", "elec_value")

    gas_result, _n_gas, _n_matched = run_gas_benefit(measures, acc_version=acc_version, base_year=base_year, market_effects_bens=market_effects_bens)
    gas_value = _sum_by_measure(gas_result, "gas_benefit", "gas_value")
    water_value, water_batched = run_water_energy(measures, acc_version=acc_version, base_year=base_year, market_effects_bens=market_effects_bens)

    participant = run_participant_cost(measures, acc_version=acc_version, base_year=base_year, market_effects_cost=market_effects_cost)
    other = price_other_ben_cost(measures, market_effects_bens, market_effects_cost)
    # Bill impacts take market effects too: CalcCE prices them on Qty x (NTGRkWh + MEBens) / (NTGRThm + MEBens) (:15885-15974). Confirmed on CET jobs 121842 /
    # 121841: every BillReduc rises by (NTG + 0.05) / NTG. (Until 2026-10-08 the POC left them unscaled.)
    bill_impact = run_bill_impact(_apply_market_effects_gas(_apply_market_effects_electric(measures, market_effects_bens), market_effects_bens),
                                  acc_version=acc_version)

    df = measures[["measure_id", "is_fuel_sub"]].merge(elec_value, on="measure_id", how="left")
    df = df.merge(gas_value, on="measure_id", how="left")
    df = df.merge(water_value, on="measure_id", how="left")
    df = df.merge(participant[["measure_id", "RebatesandIncents", "RebatesandIncentsPV", "NetParticipantCostPV", "MarkEffectPlusExcessIncPV",
                               "NetParticipantCostAdjustedPV", "GrossParticipantCostPV"] + PARTICIPANT_EXTRA], on="measure_id", how="left")
    df = df.merge(other, on="measure_id", how="left")
    df = df.merge(bill_impact, on="measure_id", how="left")
    df[["elec_value", "gas_value", "water_value"]] = df[["elec_value", "gas_value", "water_value"]].fillna(0.0)

    # Reporting only: a benefit of 0 can mean "no savings" or "the POC could not price this measure" -- say which. Does not touch any value.
    ids = df["measure_id"]
    by_id = measures.set_index("measure_id")
    has_elec = ids.map((by_id[["annual_net_kwh_1st", "annual_net_kwh_2nd", "annual_net_kw_1st", "annual_net_kw_2nd", "shape_scale", "shape_scale_2nd"]] != 0)
                       .any(axis=1)).astype(bool)
    has_gas = ids.map((by_id[["annual_net_therms_1st", "annual_net_therms_2nd"]] != 0).any(axis=1)).astype(bool)
    route = ids.map(batched.route).fillna("")
    route_kind = route.str.split(" | ", regex=False).str[0]                       # always "hourly profile" (the 8,760 path is the only electric route)
    route_source = route.str.split(" | ", regex=False).str[1].fillna(route_kind)   # user profile / library / library (sector-loose match) / ...
    elec_priced = ids.isin(_ids_in(batched.measure_year))
    df["ElecPricingStatus"] = np.select(
        [~has_elec, ids.isin(set(batched.unmatched_measure_ids)) & elec_priced, ids.isin(set(batched.unmatched_measure_ids)), elec_priced],
        ["no electric savings", "PARTLY PRICED: no hourly profile for one baseline's shape", "UNPRICED: no hourly profile", "priced: " + route_kind],
        default="UNPRICED: no value returned")
    df["ProfileSource"] = route_source.where(elec_priced & has_elec, "")
    # AC_Climate_Zone = System: the real zone it was priced in, and whether that came from the catalog or was calculated for this run (pipeline/system_zones.py)
    is_sys = ids.map(by_id["climate_zone"]).map(system_zones.is_system).astype(bool) & elec_priced & has_elec
    choice = [system_zones.resolve(by_id.at[i, "shape_key"], by_id.at[i, "PA"], by_id.at[i, "target_sector"]) if s else None for i, s in zip(ids, is_sys)]
    df["SystemClimateZone"] = pd.Series([c.zone if c else None for c in choice], index=df.index, dtype=object)
    df["SystemZoneSource"] = pd.Series([(c.source if c.zone else "no zone could be priced: System shape and blended prices") if c else None for c in choice],
                                       index=df.index, dtype=object)
    gas_priced = ids.isin(_ids_in(gas_result))
    df["GasPricingStatus"] = np.select(
        [~has_gas, gas_priced], ["no gas savings", "priced"], default="UNPRICED: no gas savings profile match")
    df["GasProfileSource"] = ids.map(by_id["gas_profile_key"]).map(
        lambda k: "" if pd.isna(k) else (gas_savings_profiles.source(str(k)) or "")).where(gas_priced & has_gas, "")
    has_water = ids.map((by_id[["kwh_water_1st", "kwh_water_2nd"]] != 0).any(axis=1)).astype(bool)
    df["WaterPricingStatus"] = np.select(
        [~has_water, ids.isin(_ids_in(water_batched.measure_year))], ["no water energy", "priced"],
        default="UNPRICED: no hourly profile")

    df["ElecBen"], df["ElecSupplyCost"] = _split_ben_supply(df["elec_value"], df["is_fuel_sub"])
    df["GasBen"], df["GasSupplyCost"] = _split_ben_supply(df["gas_value"], df["is_fuel_sub"])
    df["WaterEnergyBen"], df["WaterEnergyCost"] = _split_water(df["water_value"], ids.map(by_id["kwh_water_1st"]))

    supply_and_other_cost = df["ElecSupplyCost"] + df["GasSupplyCost"] + df["WaterEnergyCost"] + df["OtherCost"]
    df["PACCost"] = df["RebatesandIncentsPV"] + supply_and_other_cost
    df["TRCCost"] = df["RebatesandIncentsPV"] + df["NetParticipantCostAdjustedPV"] + supply_and_other_cost
    df["RIMCost"] = df["LifecycleBillReducElec"] + df["LifecycleBillReducGas"] + df["PACCost"]

    total_ben = df["ElecBen"] + df["GasBen"] + df["WaterEnergyBen"] + df["OtherBen"]
    df["TRCRatio"] = (total_ben + df["TaxCredits"]) / df["TRCCost"]
    df["PACRatio"] = total_ben / df["PACCost"]
    df["RIMRatio"] = (total_ben + df["LifecycleBillIncrElec"] + df["LifecycleBillIncrGas"]) / df["RIMCost"]
    df = df.merge(participant_cost_test(measures, df, acc_version, base_year), on="measure_id", how="left")

    return df[["measure_id", "ElecPricingStatus", "ProfileSource", "SystemClimateZone", "SystemZoneSource", "GasPricingStatus", "GasProfileSource", "WaterPricingStatus",
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
    gross = measures.copy()
    gross["annual_net_kwh_1st"] = gross["num_units"] * gross["unit_kwh_1st"]
    gross["annual_net_kwh_2nd"] = gross["num_units"] * gross["unit_kwh_2nd"]
    gross["annual_net_therms_1st"] = gross["num_units"] * gross["unit_therm_1st"] * gross["ir_therm"] * gross["rr_therm"]
    gross["annual_net_therms_2nd"] = gross["num_units"] * gross["unit_therm_2nd"] * gross["ir_therm"] * gross["rr_therm"]
    bills = run_bill_impact(gross, acc_version=acc_version).rename(columns={c: f"{c}Gross" for c in ("LifecycleBillReducElec", "LifecycleBillIncrElec", "LifecycleBillReducGas", "LifecycleBillIncrGas")})
    rates = {pa: discount_settings.get_discount_rate_qtr(pa, acc_version) for pa in measures["PA"].unique()}
    qm = (measures["claim_year"] - base_year) * 4 + (measures["claim_quarter"] - 1)
    pvf = (1.0 + measures["PA"].map(rates)) ** qm
    out = measures[["measure_id"]].assign(_pvf=pvf.to_numpy()).merge(bills, on="measure_id", how="left")
    out = out.merge(priced[["measure_id", "RebatesandIncentsPV", "GrossParticipantCostPV", "TaxCreditsGross"]], on="measure_id", how="left")
    gross_cols = ["LifecycleBillReducElecGross", "LifecycleBillIncrElecGross", "LifecycleBillReducGasGross", "LifecycleBillIncrGasGross"]
    out[gross_cols] = out[gross_cols].fillna(0.0)
    out["PCTBen"] = out["LifecycleBillReducElecGross"] + out["LifecycleBillReducGasGross"] + out["RebatesandIncentsPV"] + out["TaxCreditsGross"] / out["_pvf"]
    out["PCTCost"] = out["GrossParticipantCostPV"] + out["RebatesandIncentsPV"] + out["LifecycleBillIncrElecGross"] + out["LifecycleBillIncrGasGross"]
    out["PCTRatio"] = (out["PCTBen"] / out["PCTCost"].where(out["PCTCost"] != 0)).fillna(0.0)
    return out[["measure_id"] + gross_cols + ["PCTBen", "PCTCost", "PCTRatio"]]


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

    Returns one row per measure: measure_id, ElecBen_SB, ElecBen_SH,
    ElecSupplyCost_SB, ElecSupplyCost_SH, GasBen_SB, GasBen_SH,
    GasSupplyCost_SB, GasSupplyCost_SH, SCBCost, SCHCost, SCBRatio, SCHRatio.
    """
    if base_year is None:
        base_year = int(measures["claim_year"].min())
    sct_rate = discount_settings.get_discount_rate_qtr_sct

    def elec_value_for(cost_test):
        b = run_real_hourly(measures.reset_index(drop=True), cost_test=cost_test, acc_version=acc_version, base_year=base_year,
                               discount_rate_fn=sct_rate, market_effects_bens=market_effects_bens)
        return _sum_by_measure(b.measure_year, "annual_value", "elec_value")

    def gas_value_for(scc):
        gas_result, _n, _m = run_gas_benefit(measures, cost_test="SCT", acc_version=acc_version,
                                              base_year=base_year, scc=scc, discount_rate_fn=sct_rate,
                                              market_effects_bens=market_effects_bens)
        return _sum_by_measure(gas_result, "gas_benefit", "gas_value")

    elec_sb = elec_value_for("SCT Base").rename("elec_value_sb")
    elec_sh = elec_value_for("SCT High").rename("elec_value_sh")
    gas_sb = gas_value_for("Base").rename("gas_value_sb")
    gas_sh = gas_value_for("High").rename("gas_value_sh")

    def water_value_for(cost_test):
        return run_water_energy(measures, cost_test=cost_test, acc_version=acc_version, base_year=base_year, discount_rate_fn=sct_rate,
                                market_effects_bens=market_effects_bens)[0]

    water_sb = water_value_for("SCT Base").rename("water_value_sb")
    water_sh = water_value_for("SCT High").rename("water_value_sh")

    participant_sct = run_participant_cost(measures, acc_version=acc_version, base_year=base_year,
                                            discount_rate_fn=sct_rate, market_effects_cost=market_effects_cost)
    other = price_other_ben_cost(measures, market_effects_bens, market_effects_cost)

    df = measures[["measure_id", "is_fuel_sub"]].merge(elec_sb, on="measure_id", how="left")
    df = df.merge(elec_sh, on="measure_id", how="left")
    df = df.merge(gas_sb, on="measure_id", how="left")
    df = df.merge(gas_sh, on="measure_id", how="left")
    df = df.merge(water_sb, on="measure_id", how="left")
    df = df.merge(water_sh, on="measure_id", how="left")
    df = df.merge(participant_sct[["measure_id", "RebatesandIncentsPV", "NetParticipantCostPV", "NetParticipantCostAdjustedPV", "GrossParticipantCostPV"]],
                  on="measure_id", how="left")
    df = df.merge(other, on="measure_id", how="left")
    value_cols = ["elec_value_sb", "elec_value_sh", "gas_value_sb", "gas_value_sh", "water_value_sb", "water_value_sh"]
    df[value_cols] = df[value_cols].fillna(0.0)

    df["ElecBen_SB"], df["ElecSupplyCost_SB"] = _split_ben_supply(df["elec_value_sb"], df["is_fuel_sub"])
    df["ElecBen_SH"], df["ElecSupplyCost_SH"] = _split_ben_supply(df["elec_value_sh"], df["is_fuel_sub"])
    df["GasBen_SB"], df["GasSupplyCost_SB"] = _split_ben_supply(df["gas_value_sb"], df["is_fuel_sub"])
    df["GasBen_SH"], df["GasSupplyCost_SH"] = _split_ben_supply(df["gas_value_sh"], df["is_fuel_sub"])
    kwh_water_1st = df["measure_id"].map(measures.set_index("measure_id")["kwh_water_1st"])
    df["WaterEnergyBen_SB"], df["WaterEnergyCost_SB"] = _split_water(df["water_value_sb"], kwh_water_1st)
    df["WaterEnergyBen_SH"], df["WaterEnergyCost_SH"] = _split_water(df["water_value_sh"], kwh_water_1st)

    incentive_pv_sct = df["RebatesandIncentsPV"]
    net_participant_pv_sct = df["NetParticipantCostAdjustedPV"]          # with the market-effects term, as the TRC cost
    df["SCBCost"] = (incentive_pv_sct + net_participant_pv_sct + df["ElecSupplyCost_SB"] + df["GasSupplyCost_SB"] + df["WaterEnergyCost_SB"]
                     + df["OtherCost"])
    df["SCHCost"] = (incentive_pv_sct + net_participant_pv_sct + df["ElecSupplyCost_SH"] + df["GasSupplyCost_SH"] + df["WaterEnergyCost_SH"]
                     + df["OtherCost"])

    df["SCBRatio"] = (df["ElecBen_SB"] + df["GasBen_SB"] + df["WaterEnergyBen_SB"] + df["OtherBen"]) / df["SCBCost"]
    df["SCHRatio"] = (df["ElecBen_SH"] + df["GasBen_SH"] + df["WaterEnergyBen_SH"] + df["OtherBen"]) / df["SCHCost"]

    return df[["measure_id", "ElecBen_SB", "ElecBen_SH", "ElecSupplyCost_SB", "ElecSupplyCost_SH",
               "GasBen_SB", "GasBen_SH", "GasSupplyCost_SB", "GasSupplyCost_SH",
               "WaterEnergyBen_SB", "WaterEnergyBen_SH", "WaterEnergyCost_SB", "WaterEnergyCost_SH",
               "RebatesandIncentsPV", "NetParticipantCostPV", "NetParticipantCostAdjustedPV", "GrossParticipantCostPV",
               "SCBCost", "SCHCost", "SCBRatio", "SCHRatio"]]


SOCIETAL_COLUMN_PATTERN = r"(_SB|_SH)(_sct)?$|^SCB|^SCH|_sct$"


def _first_year_weights(rul: pd.Series, eul: pd.Series) -> tuple[pd.Series, pd.Series]:
    """CalcSavings' share of the first year on the 1st and 2nd baseline (CET_v25.1_documentation_extracted.txt, "First Year Net"): a measure whose RUL or
    EUL is under a year spends only part of its first year on the 1st baseline, and the rest (up to its EUL) on the 2nd. RUL = 1 exactly falls to the
    ELSE branches (1, 0), as in the SQL."""
    w1 = np.select([rul > 1, (rul > 0) & (rul < 1), (rul == 0) & (eul < 1)], [1.0, rul, eul], default=1.0)
    w2 = np.select([(rul > 0) & (rul < 1) & (eul < 1), (rul > 0) & (rul < 1) & (eul >= 1)], [eul - rul, 1.0 - rul], default=0.0)
    return pd.Series(w1, index=rul.index), pd.Series(w2, index=rul.index)


def _savings_quantities(m: pd.DataFrame) -> dict[str, pd.Series]:
    """Per fuel: (units x installation x realization, 1st-baseline amount, 2nd-baseline amount, NTG) -- the pieces every CalcSavings column is built from."""
    q = m["num_units"].astype(float)
    return {
        "KWh": (q * m["ir_kwh"] * m["rr_kwh"], m["unit_kwh_1st"] + m["kwh_water_1st"], m["unit_kwh_2nd"] + m["kwh_water_2nd"], m["ntgr_kwh"]),
        "KWhSite": (q * m["ir_kwh"] * m["rr_kwh"], m["unit_kwh_1st"], m["unit_kwh_2nd"], m["ntgr_kwh"]),
        "KWhWater": (q * m["ir_kwh"] * m["rr_kwh"], m["kwh_water_1st"], m["kwh_water_2nd"], m["ntgr_kwh"]),
        "KW": (q * m["ir_kw"] * m["rr_kw"], m["unit_kw_1st"], m["unit_kw_2nd"], m["ntgr_kw"]),
        "Thm": (q * m["ir_therm"] * m["rr_therm"], m["unit_therm_1st"], m["unit_therm_2nd"], m["ntgr_therm"]),
    }


def savings_outputs(measures: pd.DataFrame, market_effects_bens: float = 0.0) -> pd.DataFrame:
    """CET's savings quantities per measure (CalcSavings, CET_v25.1_documentation_extracted.txt:20804-21356), gross and net (net = x (NTG + MEB)):

        FirstYear = Q x IR x RR x ( w1 x a1 + w2 x a2 )                           (w1 / w2 from _first_year_weights)
        Lifecycle = Q x IR x RR x EUL x a1                                       when RUL = 0
                  = Q x IR x RR x ( a1 x RUL + a2 x (EUL - RUL) )                 when RUL > 0
        Annual    = Lifecycle / EUL                                              (the lifetime average)

    for a = kWh (site + IOU water: `KWh`), site kWh only (`KWhSite`), IOU water kWh only (`KWhWater`), kW (`KW`; no Lifecycle column) and therms (`Thm`).
    Uses the raw EUL_Yrs, as CalcSavings does (not the pricing's 0.25-year floor). Quantities, not dollars; nothing is priced."""
    m = measures
    rul = m["rul_years_exact"].astype(float)
    eul = m["eul_years_raw"].astype(float) if "eul_years_raw" in m else m["eul_years_exact"].astype(float)
    w1, w2 = _first_year_weights(rul, eul)
    meb = float(market_effects_bens or 0.0)
    out = {"measure_id": m["measure_id"].to_numpy()}
    for name, (base, a1, a2, ntg) in _savings_quantities(m).items():
        first = base * (w1 * a1 + w2 * a2)
        life = base * pd.Series(np.where(rul > 0, a1 * rul + a2 * (eul - rul), eul * a1), index=m.index)
        annual = (life / eul.where(eul != 0)).fillna(0.0)
        net = ntg + meb
        out[f"FirstYearGross{name}"], out[f"FirstYearNet{name}"] = first.to_numpy(), (first * net).to_numpy()
        if name != "KW":
            out[f"LifecycleGross{name}"], out[f"LifecycleNet{name}"] = life.to_numpy(), (life * net).to_numpy()
        out[f"AnnualGross{name}"], out[f"AnnualNet{name}"] = annual.to_numpy(), (annual * net).to_numpy()
    order = [f"{p}{gn}{f}" for p in ("FirstYear", "Lifecycle", "Annual") for gn in ("Gross", "Net") for f in ("KWh", "KWhSite", "KWhWater", "KW", "Thm")
             if not (p == "Lifecycle" and f == "KW")]
    return pd.DataFrame(out)[["measure_id"] + order]


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
        return pd.DataFrame(columns=["measure_id", "DiscountedSavingsGrosskWh", "DiscountedSavingsNetkWh", "DiscountedSavingsGrossThm", "DiscountedSavingsNetThm"])
    ra = m["PA"].map({pa: discount_settings.get_discount_rate_annual(pa, acc_version) for pa in m["PA"].unique()}).astype(float)
    rq = m["PA"].map({pa: discount_settings.get_discount_rate_qtr(pa, acc_version) for pa in m["PA"].unique()}).astype(float)
    raf = 1.0 + ra
    qm = (m["claim_year"] - base_year) * 4 + (m["claim_quarter"] - 1)
    pv = (1.0 + rq) ** qm
    rul = m["rul_years_exact"].astype(float)
    eul = m["eul_years_raw"].astype(float) if "eul_years_raw" in m else m["eul_years_exact"].astype(float)

    def annuity(n):
        return 1.0 + (1.0 - raf ** -(n - 1.0)) / ra

    out = {"measure_id": m["measure_id"].to_numpy()}
    q = _savings_quantities(m)
    meb = float(market_effects_bens or 0.0)
    for name, key in (("kWh", "KWhSite"), ("Thm", "Thm")):
        base, a1, a2, ntg = q[key]
        dual = a1 * annuity(rul) + a2 * annuity(eul - rul) / raf ** rul
        single = a1 * annuity(eul)
        gross = base * pd.Series(np.where(rul > 0, dual, single), index=m.index) / pv
        gross = gross.where((a1 != 0) & (eul != 0) & eul.notna(), 0.0).fillna(0.0)
        out[f"DiscountedSavingsGross{name}"] = gross.to_numpy()
        out[f"DiscountedSavingsNet{name}"] = (gross * (ntg + meb)).to_numpy()
    return pd.DataFrame(out)[["measure_id", "DiscountedSavingsGrosskWh", "DiscountedSavingsNetkWh", "DiscountedSavingsGrossThm", "DiscountedSavingsNetThm"]]


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
        base_year = int(measures["claim_year"].min())
    d = price_measure_values(measures, acc_version=acc_version, base_year=base_year, market_effects_bens=market_effects_bens,
                             market_effects_cost=market_effects_cost, include_societal=include_societal)
    return apply_program_costs(d, measures, program_costs, acc_version=acc_version, base_year=base_year, market_effects_bens=market_effects_bens,
                               include_societal=include_societal)


def price_measure_values(measures: pd.DataFrame, acc_version: int, base_year: int, market_effects_bens: float = 0.0, market_effects_cost: float = 0.0,
                         include_societal: bool = True) -> pd.DataFrame:
    """The measure-level half of run_full_cost_effectiveness: every value that depends on the ACC (benefits, supply costs, participant PVs, bill impacts, the
    societal variants), one row per measure, before program costs are shared out. Measures priced on different ACC versions (BRD BR-06.5) are priced here
    one group at a time, each under its own ACC, and the groups are joined before apply_program_costs."""
    trc = run_cost_effectiveness(measures, acc_version=acc_version, base_year=base_year,
                                 market_effects_bens=market_effects_bens, market_effects_cost=market_effects_cost)
    if include_societal:
        sct = run_societal_cost_effectiveness(measures, acc_version=acc_version, base_year=base_year,
                                              market_effects_bens=market_effects_bens, market_effects_cost=market_effects_cost)
    else:   # placeholders so the shared cost-test arithmetic runs; every societal column is blanked again below
        sct = measures[["measure_id"]].assign(**{c: 0.0 for c in (
            "ElecBen_SB", "ElecBen_SH", "ElecSupplyCost_SB", "ElecSupplyCost_SH", "GasBen_SB", "GasBen_SH", "GasSupplyCost_SB", "GasSupplyCost_SH",
            "WaterEnergyBen_SB", "WaterEnergyBen_SH", "WaterEnergyCost_SB", "WaterEnergyCost_SH",
            "RebatesandIncentsPV", "NetParticipantCostPV", "NetParticipantCostAdjustedPV", "GrossParticipantCostPV", "SCBCost", "SCHCost", "SCBRatio",
            "SCHRatio")})
    sct = sct.rename(columns={"RebatesandIncentsPV": "RebatesandIncentsPV_sct", "NetParticipantCostPV": "NetParticipantCostPV_sct",
                              "NetParticipantCostAdjustedPV": "NetParticipantCostAdjustedPV_sct", "GrossParticipantCostPV": "GrossParticipantCostPV_sct"})
    sct = sct.drop(columns=["SCBCost", "SCHCost", "SCBRatio", "SCHRatio"])
    trc = trc.drop(columns=["TRCCost", "PACCost", "RIMCost", "TRCRatio", "PACRatio", "RIMRatio"])
    d = measures[["measure_id", "prg_id", "NTGRkWh", "ntgr_therm"]].copy()
    d["NTGRkWh"] = d["NTGRkWh"] + market_effects_bens          # the Gross columns undo the NTG that the benefit was scaled by
    d["ntgr_therm"] = d["ntgr_therm"] + market_effects_bens
    ds = discounted_savings(measures, acc_version, base_year, market_effects_bens)       # the levelized columns' denominators, at this group's ACC rates
    return d.merge(trc, on="measure_id").merge(sct, on="measure_id").merge(ds, on="measure_id", how="left")


def program_share_totals(d: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Each program's totals of the amounts its program-level shares divide up (the benefits behind WeightedBenefits and the cost tests' program-cost shares),
    over `d` (price_measure_values). A chunked run adds these up over its chunks
    (cost_tests.combine_share_totals) and passes the sum to apply_program_costs, so every measure gets the share it has in the whole job."""
    return cost_tests.share_totals(cost_tests.share_inputs(d), d["prg_id"])


def apply_program_costs(d: pd.DataFrame, measures: pd.DataFrame, program_costs: pd.DataFrame | None, acc_version: int, base_year: int,
                        market_effects_bens: float = 0.0, include_societal: bool = True, totals: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    """The program-level half: share each program's costs over its measures (all of them, whatever ACC version priced them) and finish every cost test.
    Program costs are discounted with the run's own ACC version's rates (`acc_version`). `totals` (program_share_totals over the whole job) is for a run that
    passes the job in parts; None = `d` is the whole job."""
    prog = cost_tests.program_cost_sums(program_costs, base_year, acc_version)
    out = cost_tests.compute_cost_tests(d, prog, totals)
    out = out.merge(savings_outputs(measures, market_effects_bens), on="measure_id", how="left")
    if not include_societal:
        out[[c for c in out.columns if re.search(SOCIETAL_COLUMN_PATTERN, c)]] = float("nan")
    return out


def apply_scaled_profiles(measures: pd.DataFrame, allocated: bool = False) -> pd.DataFrame:
    """Measures priced on a user 8,760 profile that is already scaled -- kWh per unit per hour, `profile_basis = kwh_per_unit` (BRD BR-06.6). The profile is
    priced as given: its hourly kWh per unit x NumUnits x NTG x IR x RR, so for a baseline priced on such a profile

        shape_scale(_2nd) = NumUnits x NTGRkWh x IR x RR            (the profile itself carries the kWh)

    and its annual kWh per unit is the profile's own sum S (0 for a load-shifting-only profile), which replaces UnitkWh1stBaseline (UnitkWh2ndBaseline) in the
    annual quantities: annual_net_kwh_* (first-year kWh, bill impacts). The 2nd baseline is priced on its own shape (shape_key_2nd, AC_Elec_Impact_Profile_2nd_Baseline)
    when it has one; when it shares the 1st baseline's scaled profile it keeps the file's ratio r2 = UnitkWh2ndBaseline / UnitkWh1stBaseline (1 when
    UnitkWh1stBaseline is 0): shape_scale_2nd = shape_scale x r2 and S x r2 kWh. Baselines on any other shape are unchanged. With statewide allocation
    (`allocated`), an allocated row is multiplied by its electric factor, as its UnitkWh fields were."""
    if real_impact_profiles.user_profiles() is None or measures.empty:
        return measures

    def annual_kwh(keys):
        zones = [system_zones.priced_zone(k, pa, ts, cz) for k, pa, ts, cz in zip(keys, measures["PA"], measures["target_sector"], measures["climate_zone"])]
        return pd.Series([real_impact_profiles.scaled_profile(k, pa, ts, z) for k, pa, ts, z in zip(keys, measures["PA"], measures["target_sector"], zones)],
                         index=measures.index, dtype=float)
    s1 = annual_kwh(measures["shape_key"])
    key2 = measures["shape_key_2nd"] if "shape_key_2nd" in measures.columns else measures["shape_key"]
    same2 = key2 == measures["shape_key"]
    s2 = s1.where(same2, annual_kwh(key2))
    if s1.isna().all() and s2.isna().all():
        return measures
    m = measures.copy()
    f = statewide_allocation.electric_factors(m["prg_id"], m["claim_year"]) if allocated else pd.Series(1.0, index=m.index)
    u1, u2 = m["unit_kwh_1st"].astype(float), m["unit_kwh_2nd"].astype(float)
    r2 = (u2 / u1.where(u1 != 0)).fillna(1.0)
    for c in ("unit_kwh_1st", "unit_kwh_2nd", "annual_net_kwh_1st", "annual_net_kwh_2nd", "annual_net_kwh", "shape_scale", "shape_scale_2nd"):
        m[c] = m[c].astype(float)                     # a whole-number file reads as int64, which cannot take the profile's kWh
    per_unit = m["num_units"] * f * m["NTGRkWh"] * m["ir_kwh"] * m["rr_kwh"]
    first = s1.notna()
    m.loc[first, "unit_kwh_1st"], m.loc[first, "shape_scale"] = (s1 * f)[first], per_unit[first]
    shared = same2 & first                            # the 2nd baseline on the same scaled profile: the file's 2nd / 1st ratio
    m.loc[shared, "unit_kwh_2nd"], m.loc[shared, "shape_scale_2nd"] = (s1 * f * r2)[shared], (per_unit * r2)[shared]
    own = ~same2 & s2.notna()                         # the 2nd baseline on its own scaled profile
    m.loc[own, "unit_kwh_2nd"], m.loc[own, "shape_scale_2nd"] = (s2 * f)[own], per_unit[own]
    changed = first | shared | own
    for b in ("1st", "2nd"):
        m.loc[changed, f"annual_net_kwh_{b}"] = (m["num_units"] * m[f"unit_kwh_{b}"] * m["NTGRkWh"])[changed]
    m.loc[changed, "annual_net_kwh"] = m.loc[changed, "annual_net_kwh_1st"]
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
