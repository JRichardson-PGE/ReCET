"""
Local proof-of-concept: prices real CET example-input measures two ways --
the way CET does it today (net annual savings, then shape) and the way the
proposed architecture does it (batched, per-measure, hourly-native) -- and
prints both side by side, plus a synthetic zero-net load-shifting measure
that makes the difference between the two approaches concrete.

Two real datasets are available:
  --dataset small  5 measures, the public CET spec's own example input
                   (deliberately exercises WEN/FuelSub/NC-AE edge cases --
                   see compare_to_cet.py and README.md for the real-CET
                   ground-truth comparison this produced)
  --dataset large  ~5,000 measures, a real statewide PGE-led filing --
                   far more realistic diversity (17 climate zones, 4 IOU
                   territories, 7 sectors, 38 load shapes)

By default this uses REAL hourly avoided-cost data from the actual 2024 ACC
(the version CET has implemented today -- see pipeline/real_avoided_cost.py
and CLAUDE.md's "Terminology gotcha" note on why 2024, not the "2026" label
CET's own UI shows). Load shapes are still synthetic placeholders. Pass
--synthetic to use the old fully-synthetic avoided-cost curve instead.

Run: python run_poc.py
     python run_poc.py --dataset large
     python run_poc.py --year 2026
     python run_poc.py --synthetic
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .pipeline import (
    cost_tests,
    discount_settings,
    emissions,
    gas_avoided_cost,
    gas_savings_profiles,
    rate_schedules,
    real_avoided_cost_v2,
    real_impact_profiles,
    system_zones,
)
from .pipeline import data_loading  # noqa: E402 (module-level use in load_job)
from .pipeline.data_loading import (
    DATA_DIR,
    load_real_sample_measures,
    load_real_shape_matched_measures,
    load_sw_pge_led_measures,
    load_swwh025_measures,
)
from .pipeline.engine import (
    BatchedPricingResult,
    legacy_net_then_shape,
    price_batched_hourly,
    price_bill_impact,
    price_gas_benefit,
    price_other_ben_cost,
    price_participant_cost,
    price_real_hourly,
    price_real_hourly_legacy,
)
from .pipeline.synthetic_curves import avoided_cost_curve, load_shape, load_shifting_shape

DATASETS = {
    "small": ("5 real measures (public CET spec example input)", load_real_sample_measures),
    "large": ("~5,000 real measures (statewide PGE-led filing)", load_sw_pge_led_measures),
    "real_shape_matched": (
        "2,378 real measures (large filing, restricted to real-matched CEDARS impact-profile shapes)",
        load_real_shape_matched_measures,
    ),
    "swwh025": (
        "73,024 real measures (statewide HPWH fuel-substitution filing, 2026-09-16 parallel test)",
        load_swwh025_measures,
    ),
}
ACC_MODES = ["energy", "full", "real_hourly", "synthetic"]


def build_caches(measures: pd.DataFrame, acc_mode: str, acc_year: int) -> tuple[dict, dict, pd.DataFrame]:
    """
    Returns (avoided_cost, load_shapes, measures) -- measures is returned
    because subset_key is overwritten in place:
      acc_mode="energy"    -- real NP-15/SP-15 energy-only $/kWh (subset_key = hub)
      acc_mode="full"       -- energy + capacity + transmission + distribution
                               + GHG stacked (subset_key = PA__climate_zone,
                               see pipeline/real_avoided_cost_full.py)
      acc_mode="synthetic"  -- old fully-synthetic placeholder curve
    """
    from .pipeline import real_avoided_cost, real_avoided_cost_full   # legacy demo modes only: they need openpyxl and the raw 2024 ACC workbooks
    measures = measures.copy()

    if acc_mode == "energy":
        real_curves = real_avoided_cost.load_2024_acc_hourly(acc_year)  # {"NP15": ..., "SP15": ...}
        measures["subset_key"] = measures["PA"].apply(real_avoided_cost.subset_key_to_hub)
        avoided_cost = {hub: real_curves[hub] for hub in measures["subset_key"].unique()}
    elif acc_mode == "full":
        real_curves = real_avoided_cost.load_2024_acc_hourly(acc_year)
        hub_by_pa = {pa: real_curves[real_avoided_cost.subset_key_to_hub(pa)] for pa in measures["PA"].unique()}
        measures["subset_key"] = measures["PA"] + "__" + measures["climate_zone"]
        avoided_cost = {}
        for (pa, cz), subset_key in measures.groupby(["PA", "climate_zone"])["subset_key"].first().items():
            avoided_cost[subset_key] = real_avoided_cost_full.build_full_avoided_cost(
                pa, cz, hub_by_pa[pa], acc_year
            )
    else:
        avoided_cost = {k: avoided_cost_curve(k) for k in measures["subset_key"].unique()}

    # Keyed by shape_key, not measure_id: these are NORMALIZED patterns
    # (sum to 1.0) that legitimately get shared across measures using the
    # same named shape -- see synthetic_curves.load_shape()'s docstring for
    # why baking a specific measure's kWh into this dict would be a bug.
    load_shapes = {key: load_shape(key) for key in measures["shape_key"].unique()}

    return avoided_cost, load_shapes, measures


def add_load_shifting_demo(measures: pd.DataFrame, load_shapes: dict, avoided_cost: dict) -> pd.DataFrame:
    """Append one synthetic measure that shifts 5,000 kWh/year from peak to
    off-peak with zero net annual savings -- the exact case CET's calculation
    order cannot value."""
    demo_subset = measures["subset_key"].iloc[0]
    demo_shape_key = "DEMO_load_shift_zero_net"
    load_shapes[demo_shape_key] = load_shifting_shape()  # normalized: sums to 0, scaled by shape_scale below

    first = measures.iloc[0]
    demo_row = pd.DataFrame([{
        "measure_id": "DEMO_LoadShift1",
        "MeasDescription": "Synthetic demo: shifts 5,000 kWh/yr from peak to off-peak, zero net savings",
        "subset_key": demo_subset,
        "shape_key": demo_shape_key,
        "annual_net_kwh": 0.0,
        "annual_net_kw": 0.0,
        "eul_years": 10,
        "eul_years_exact": 10.0,
        "rul_years_exact": 0.0,  # not an early-replacement measure -- no dual-baseline phase
        "NTGRkWh": 1.0,
        "shape_scale": 5000.0,  # kWh/year shifted -- annual_net_kwh is legitimately 0, this isn't
        "shape_scale_2nd": 5000.0,  # rul_years_exact=0 means this is never actually used, but keep it defined
        # Borrowed from the dataset's first real measure -- only real_hourly
        # mode needs these (its get_acc_years() closure keys off PA/climate_zone),
        # and the demo measure's whole point is its load *shape*, not its location.
        "PA": first.get("PA", "PGE"),
        "climate_zone": first.get("climate_zone", "1"),
        "target_sector": first.get("target_sector", ""),
        "claim_year": first.get("claim_year", 2025),
        "claim_quarter": first.get("claim_quarter", 1),
    }])
    return pd.concat([measures, demo_row], ignore_index=True)


def _ids_in(df: pd.DataFrame | None) -> set:
    """The measure_ids present in a pricing result; empty when nothing was priced (such a frame has no columns at all)."""
    return set() if df is None or df.empty else set(df["measure_id"])


ROUTE_COLUMNS = ["shape_key", "PA", "target_sector", "climate_zone"]   # what decides a measure's electric shape and avoided-cost series


def _route_keys(df: pd.DataFrame, cols: list[str]) -> list[tuple]:
    """One hashable per row: the values of `cols`, plus the measure's own id for the synthetic demo measure (which has a shape of its own)."""
    ids = df["measure_id"].astype(str)
    return list(zip(*[df[c].astype(str).to_numpy() for c in cols], np.where(ids.str.startswith("DEMO_").to_numpy(), ids.to_numpy(), "")))


def _per_route(df: pd.DataFrame, cols: list[str], fn) -> pd.Series:
    """`fn(row) -> bool` for every row of `df`, evaluated once per distinct combination of `cols` (on that group's first row) and once per demo measure, instead of once per row.
    The callbacks used with it depend on nothing but those columns (and, for the demo measure, its id), so the result equals the row-by-row `apply`."""
    if df.empty:
        return pd.Series(False, index=df.index, dtype=bool)
    codes, _uniques = pd.factorize(pd.Series(_route_keys(df, cols), dtype=object))
    _, first = np.unique(codes, return_index=True)
    flags = np.array([bool(fn(df.iloc[p])) for p in first])
    return pd.Series(flags[codes], index=df.index)


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


def run_real_hourly(measures: pd.DataFrame, cost_test: str = "TRC", acc_version: int = 2026,
                     base_year: int | None = None, include_demo: bool = False,
                     discount_rate_fn=discount_settings.get_discount_rate_qtr,
                     market_effects_bens: float = 0.0, component: str | None = None,
                     compute_legacy: bool = True, gen_only: bool = False):
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

    include_demo=False by default (2026-09-16, Jake's direction): the
    synthetic zero-net load-shifting demo measure used to be added to
    every run unconditionally, which was confusing when comparing this
    POC's output against a real parallel CET run on real data (it showed
    up looking like a 74th/5,050th/etc. real measure that "failed to
    match" a profile). Pass include_demo=True to opt back in for the
    original demonstration (see "The point of this POC" in main()).

    NO SYNTHETIC SHAPE FALLBACK for real measures (2026-09-16, Jake's
    direction, replacing the old always-on placeholder substitution): a
    measure with no real 8760-hour CEDARS profile match is now FLAGGED
    (collected into the returned BatchedPricingResult's
    `unmatched_measure_ids`) and excluded from pricing, never priced
    against a fabricated shape. The demo measure (when include_demo=True)
    still gets its own deliberately-synthetic shape, but through get_shape
    directly, not this mechanism -- it was never a real measure to begin
    with, so it isn't something the "no more synthetic fallback" rule is
    about.

    discount_rate_fn (2026-09-17, for SCB/SCH): defaults to the primary
    quarterly rate, but the real societal cost tests discount their own
    avoided-cost PV term with a genuinely different rate
    (`discount_settings.get_discount_rate_qtr_sct`) -- pass that in when
    cost_test is "SCT Base"/"SCT High" so this function's discounting
    matches which cost test it's actually pricing.

    Returns (batched_result, legacy_df).
    """
    measures = _apply_market_effects_electric(measures, market_effects_bens)
    if base_year is None:
        base_year = int(measures["claim_year"].min())
    load_shapes: dict[str, np.ndarray] = {}
    if include_demo:
        measures = add_load_shifting_demo(measures, load_shapes, avoided_cost={})

    other_measures = measures.reset_index(drop=True)
    # E3ClimateZone = System is priced in the profile's System zone (pipeline/system_zones.py): its shape AND its prices come from that real zone
    other_measures["price_zone"] = [system_zones.priced_zone(k, pa, ts, cz) for k, pa, ts, cz in
                                    zip(other_measures["shape_key"], other_measures["PA"], other_measures["target_sector"], other_measures["climate_zone"])]

    match_cache: dict[tuple, tuple | None] = {}   # (shape key, PA, sector, zone) -> (shape, represents_year, source) or None: one library / user-profile lookup per route

    def get_shape(m):
        if str(m["measure_id"]).startswith("DEMO_"):
            return load_shapes[m["shape_key"]], real_impact_profiles.ACC_CALENDAR_YEAR  # demo's own synthetic shape, drawn on the ACC calendar (no shift)
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
        need_portfolio_hourly=compute_legacy or include_demo,                # the 8,760 portfolio accumulator (demo output only) needs every measure's hourly array
    )
    # the "legacy" net-then-shape pricing is only a comparison output (--acc-mode demo); the cost tests never use it, so they pass compute_legacy=False
    legacy = (price_real_hourly_legacy(other_measures, get_shape, get_acc_years, get_discount_rate_qtr, base_year)
              if compute_legacy else pd.DataFrame(columns=["measure_id", "year", "quarter", "annual_value"]))

    hourly_source = [(match_cache.get(k) or (None, None, ""))[2] for k in zip(other_measures["shape_key"], other_measures["PA"], other_measures["target_sector"],
                                                                          other_measures["price_zone"])]      # where each measure's 8,760 shape came from (user profile / library ...)
    batched.route = {mid: f"hourly profile | {src}" for mid, src in zip(other_measures["measure_id"], hourly_source)}
    return batched, legacy


def run_gas_benefit(measures: pd.DataFrame, cost_test: str = "TRC", acc_version: int = 2026, base_year: int | None = None,
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
    E3GasSector since Measure.csv has no real field for it).

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


def run_bill_impact(measures: pd.DataFrame, acc_version: int = 2026):
    """
    Wires up the real CET retail rate schedules (pipeline/rate_schedules.py)
    for the RIM test's bill-impact columns -- see engine.price_bill_impact's
    docstring for the full mechanics (annual, not quarterly; sign-based
    Reduc/Incr classification).

    Returns a DataFrame with one row per measure: measure_id, BillReducElec,
    BillIncrElec, BillReducGas, BillIncrGas -- NOT a measure_year-per-quarter
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


def run_emissions(measures: pd.DataFrame, acc_version: int = 2026):
    """CO2/NOx/PM10 outputs (Net/Gross x Elec/Gas x First-year/Lifecycle) via
    pipeline/emissions.py -- see its docstring. One row per measure."""
    return emissions.price_emissions(measures, acc_version=acc_version)


def run_participant_cost(measures: pd.DataFrame, acc_version: int = 2026, base_year: int | None = None,
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


def run_water_energy(measures: pd.DataFrame, cost_test: str = "TRC", acc_version: int = 2026, base_year: int | None = None,
                     discount_rate_fn=discount_settings.get_discount_rate_qtr, market_effects_bens: float = 0.0):
    """Lifetime PV of the WATER ENERGY of every measure that has any (UnitkWhIOUWater1st/2ndBaseline != 0): the IOU-supplied energy embedded in the water a measure
    saves (positive) or uses (negative), priced exactly like the electric benefit but with kWhWater1/2 in place of kWh1/2 and the Gen term only (CalcCE's
    WaterEnergyBen: Qty x (NTGRkWh + MEBens) x IRkWh x RRkWh x (kWhWater1 x Gen_1 + kWhWater2 x Gen_2)). The measure's own electric end-use profile and climate zone
    carry over, on the 8,760 path like the electric benefit, but on the Gen term ALONE: unlike ElecBen there is no T&D term, so total avoided cost minus
    transmission and distribution (run_real_hourly gen_only). Returns (signed value per measure_id named `water_value`, the BatchedPricingResult for the status columns)."""
    w = measures[(measures["kwh_water_1st"] != 0) | (measures["kwh_water_2nd"] != 0)]
    if w.empty:
        return _sum_by_measure(None, "annual_value", "water_value"), BatchedPricingResult(
            measure_year=pd.DataFrame(columns=["measure_id", "year", "quarter", "annual_value"]), portfolio_hourly=np.zeros(8760), n_batches=0,
            elapsed_seconds=0.0, batch_size=0)
    w = w.reset_index(drop=True).copy()
    w["shape_scale"], w["shape_scale_2nd"] = w["water_scale_1st"], w["water_scale_2nd"]
    batched, _ = run_real_hourly(w, cost_test=cost_test, acc_version=acc_version, base_year=base_year, discount_rate_fn=discount_rate_fn,
                                 market_effects_bens=market_effects_bens, compute_legacy=False, gen_only=True)
    return _sum_by_measure(batched.measure_year, "annual_value", "water_value"), batched


def _split_water(value: pd.Series, kwh_water_1st: pd.Series) -> tuple[pd.Series, pd.Series]:
    """CalcCE's WaterEnergyBen / WaterEnergyCost split, decided by the SIGN OF THE 1ST-BASELINE water kWh (not of the value): a measure with kWhWater1 > 0 books its
    value as a benefit, one with kWhWater1 < 0 books the negated value as a (positive) cost, and kWhWater1 = 0 books neither."""
    return value.where(kwh_water_1st > 0, 0.0), (-value).where(kwh_water_1st < 0, 0.0)


def run_cost_effectiveness(measures: pd.DataFrame, acc_version: int = 2026, base_year: int | None = None,
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

    RIMCost/RIMRatio (2026-09-17): `RIMCost = BillReducElec + BillReducGas
    + PACCost`, `RIMRatio = (ElecBen+GasBen+WaterEnergyBen+OtherBen+
    BillIncrElec+BillIncrGas) / RIMCost` (CET_v25.1_documentation_extracted
    .txt:16115-16168) -- built directly on run_bill_impact's already
    floating-point-exact BillReducElec/BillIncrElec/BillReducGas/
    BillIncrGas, so RIMCost/RIMRatio's own accuracy is bounded only by
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

    batched, _legacy = run_real_hourly(measures, acc_version=acc_version, base_year=base_year, market_effects_bens=market_effects_bens,
                                       compute_legacy=False)
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
                               "NetParticipantCostAdjustedPV", "GrossParticipantCostPV"]], on="measure_id", how="left")
    df = df.merge(other, on="measure_id", how="left")
    df = df.merge(bill_impact, on="measure_id", how="left")
    df[["elec_value", "gas_value", "water_value"]] = df[["elec_value", "gas_value", "water_value"]].fillna(0.0)

    # Reporting only: a benefit of 0 can mean "no savings" or "the POC could not price this measure" -- say which. Does not touch any value.
    ids = df["measure_id"]
    by_id = measures.set_index("measure_id")
    has_elec = ids.map((by_id[["annual_net_kwh_1st", "annual_net_kwh_2nd", "annual_net_kw_1st", "annual_net_kw_2nd"]] != 0).any(axis=1)).astype(bool)
    has_gas = ids.map((by_id[["annual_net_therms_1st", "annual_net_therms_2nd"]] != 0).any(axis=1)).astype(bool)
    route = ids.map(batched.route).fillna("")
    route_kind = route.str.split(" | ", regex=False).str[0]                       # always "hourly profile" (the 8,760 path is the only electric route)
    route_source = route.str.split(" | ", regex=False).str[1].fillna(route_kind)   # user profile / library / library (sector-loose match) / ...
    elec_priced = ids.isin(_ids_in(batched.measure_year))
    df["ElecPricingStatus"] = np.select(
        [~has_elec, ids.isin(set(batched.unmatched_measure_ids)), elec_priced],
        ["no electric savings", "UNPRICED: no hourly profile", "priced: " + route_kind],
        default="UNPRICED: no value returned")
    df["ProfileSource"] = route_source.where(elec_priced & has_elec, "")
    # E3ClimateZone = System: the real zone it was priced in, and whether that came from the catalog or was calculated for this run (pipeline/system_zones.py)
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
    df["RIMCost"] = df["BillReducElec"] + df["BillReducGas"] + df["PACCost"]

    total_ben = df["ElecBen"] + df["GasBen"] + df["WaterEnergyBen"] + df["OtherBen"]
    df["TRCRatio"] = (total_ben + df["TaxCredits"]) / df["TRCCost"]
    df["PACRatio"] = total_ben / df["PACCost"]
    df["RIMRatio"] = (total_ben + df["BillIncrElec"] + df["BillIncrGas"]) / df["RIMCost"]

    return df[["measure_id", "ElecPricingStatus", "ProfileSource", "SystemClimateZone", "SystemZoneSource", "GasPricingStatus", "GasProfileSource", "WaterPricingStatus",
               "ElecBen", "ElecSupplyCost", "GasBen", "GasSupplyCost", "WaterEnergyBen", "WaterEnergyCost",
               "OtherBen", "OtherCost", "TaxCredits", "OtherBenGross", "OtherCostGross", "TaxCreditsGross",
               "RebatesandIncents", "RebatesandIncentsPV", "NetParticipantCostPV", "MarkEffectPlusExcessIncPV", "NetParticipantCostAdjustedPV",
               "GrossParticipantCostPV", "BillReducElec", "BillIncrElec", "BillReducGas", "BillIncrGas",
               "TRCCost", "PACCost", "RIMCost", "TRCRatio", "PACRatio", "RIMRatio"]]


def run_societal_cost_effectiveness(measures: pd.DataFrame, acc_version: int = 2026, base_year: int | None = None,
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
        b, _ = run_real_hourly(measures.reset_index(drop=True), cost_test=cost_test, acc_version=acc_version, base_year=base_year,
                               discount_rate_fn=sct_rate, market_effects_bens=market_effects_bens, compute_legacy=False)
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


def first_year_net_savings(measures: pd.DataFrame, market_effects_bens: float = 0.0) -> pd.DataFrame:
    """CET's first-year NET savings per measure (CalcSavings, "First Year Net"; market effects included as NTG + MEBens):

        FirstYearNetKWh = Q x (NTGRkWh + MEB) x IRkWh x RRkWh x [ w1 x (kWh1 + kWhWater1) + w2 x (kWh2 + kWhWater2) ]
        FirstYearNetKW  = Q x (NTGRkW  + MEB) x IRkW  x RRkW  x [ w1 x kW1 + w2 x kW2 ]
        FirstYearNetThm = Q x (NTGRThm + MEB) x IRThm x RRThm x [ w1 x Thm1 + w2 x Thm2 ]

    with w1 / w2 from _first_year_weights. CET's kWh total includes the IOU water energy (its FirstYearNetKWhSite leaves it out). Uses the raw EUL_Yrs
    (not the 0.25-year floor the pricing applies), as CalcSavings does."""
    m = measures
    rul = m["rul_years_exact"].astype(float)
    eul = m["eul_years_raw"].astype(float) if "eul_years_raw" in m else m["eul_years_exact"].astype(float)
    w1, w2 = _first_year_weights(rul, eul)
    q = m["num_units"].astype(float)
    meb = float(market_effects_bens or 0.0)
    kwh = q * (m["ntgr_kwh"] + meb) * m["ir_kwh"] * m["rr_kwh"] * (w1 * (m["unit_kwh_1st"] + m["kwh_water_1st"]) + w2 * (m["unit_kwh_2nd"] + m["kwh_water_2nd"]))
    kw = q * (m["ntgr_kw"] + meb) * m["ir_kw"] * m["rr_kw"] * (w1 * m["unit_kw_1st"] + w2 * m["unit_kw_2nd"])
    thm = q * (m["ntgr_therm"] + meb) * m["ir_therm"] * m["rr_therm"] * (w1 * m["unit_therm_1st"] + w2 * m["unit_therm_2nd"])
    return pd.DataFrame({"measure_id": m["measure_id"].to_numpy(), "FirstYearNetKWh": kwh.to_numpy(), "FirstYearNetKW": kw.to_numpy(),
                         "FirstYearNetThm": thm.to_numpy()})


def run_full_cost_effectiveness(measures: pd.DataFrame, program_costs: pd.DataFrame | None = None,
                                acc_version: int = 2026, base_year: int | None = None,
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
    d = d.merge(trc, on="measure_id").merge(sct, on="measure_id")
    prog = cost_tests.program_cost_sums(program_costs, base_year, acc_version)
    out = cost_tests.compute_cost_tests(d, prog)
    out = out.merge(first_year_net_savings(measures, market_effects_bens), on="measure_id", how="left")
    if not include_societal:
        out[[c for c in out.columns if re.search(SOCIETAL_COLUMN_PATTERN, c)]] = float("nan")
    return out


def load_job(measure_csv, program_cost_csv=None, allocate_statewide: bool = False):
    """Load one CET job's inputs, optionally splitting its statewide (SW_) programs across the IOUs -- BOTH files together.

    allocate_statewide=True (pipeline/statewide_allocation.py, the inputs route): each SW program's measures are split by the per-fuel
    factors (kWh by the electric factor, therms by the gas factor, ...) and its ProgramCost rows by the per-field rules, with `<IOU>_`
    prepended to CEInputID and PrgID and PA set to the IOU on program-cost rows. Program costs then discount at each IOU's own rate
    and are shared only among that IOU's own measures. Allocating only one of the two files would leave the PrgIDs unmatched, so this
    is the single entry point; misalignment is reported by cost_tests.check_program_alignment and raised here.

    Returns (measures, program_costs or None)."""
    measures = data_loading.load_measures_from_csv(Path(measure_csv), allocate_statewide=allocate_statewide)
    program = cost_tests.load_program_costs(Path(program_cost_csv), allocate_statewide=allocate_statewide) if program_cost_csv else None
    if program is not None:
        gap = cost_tests.check_program_alignment(measures, program)
        if gap["measure_programs_without_costs"] or gap["program_costs_without_measures"]:
            raise ValueError(f"PrgIDs do not line up between the measure and program-cost files: {gap}")
    return measures, program


def main():
    from .pipeline import real_avoided_cost, real_avoided_cost_full   # legacy demo modes only
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=DATASETS.keys(), default="small")
    parser.add_argument("--acc-mode", choices=ACC_MODES, default="energy",
                         help="energy = real energy-only $/kWh; full = energy+capacity+transmission+distribution+GHG stacked; synthetic = old placeholder curve")
    parser.add_argument("--synthetic", action="store_true", help="shorthand for --acc-mode synthetic")
    parser.add_argument("--year", type=int, default=None,
                         help="ACC vintage year to price against (real modes only; defaults to the dataset's own ClaimYearQuarter year)")
    parser.add_argument("--include-demo", action="store_true",
                         help="add the synthetic zero-net load-shifting demo measure (off by default as of "
                              "2026-09-16 -- it used to be added unconditionally, which was confusing when "
                              "comparing this POC's output against a real parallel CET run)")
    args = parser.parse_args()

    acc_mode = "synthetic" if args.synthetic else args.acc_mode
    real_hourly_ready = (
        real_impact_profiles.is_available() and real_avoided_cost_v2.is_available() and discount_settings.is_available()
    )
    if acc_mode == "real_hourly" and not real_hourly_ready:
        print("real_hourly mode needs the merged CEDARS impact-profile library "
              "(poc/data/impact_profiles_all_iou_full_2026-09-14/), the ACC2024 hourly "
              "extraction ('2024 ACC Extraction/output/ACC2024_electric_avoided_costs_hourly.parquet'), "
              "and the real discount-rate settings (poc/data/avoided_cost_settings.csv) -- falling back to full.\n")
        acc_mode = "full"
    if acc_mode not in ("synthetic", "real_hourly") and not real_avoided_cost.is_available():
        print("Real 2024 ACC data not found under reference-docs/2024_acc/ -- falling back to synthetic.\n")
        acc_mode = "synthetic"
    if acc_mode == "full" and not real_avoided_cost_full.is_available():
        print("Extracted full-ACC component CSVs not found under reference-docs/2024_acc/extracted/ -- falling back to energy-only.\n")
        acc_mode = "energy"

    label, loader = DATASETS[args.dataset]
    measures = loader()
    acc_year = args.year if args.year is not None else int(measures["claim_year"].mode().iloc[0])

    if acc_mode == "real_hourly":
        if args.year is not None:
            print("NOTE: --year is ignored in real_hourly mode -- each measure is priced against "
                  "its OWN real ClaimYearQuarter through its own EUL, using that specific year's "
                  "real avoided cost every year (not one fixed vintage year repeated), which is the "
                  "whole point of this mode. Drop --year, or use --acc-mode energy/full to pick one.\n")
        n_real = len(measures)
        base_year = int(measures["claim_year"].min())
        batched, legacy = run_real_hourly(measures, base_year=base_year, include_demo=args.include_demo)
        n_total = n_real + (1 if args.include_demo else 0)
        print(f"Dataset: {label}")
        print(f"Avoided-cost source: REAL 2024 ACC hourly extraction (all years 2024-2054, "
              f"TRC cost test, re-aligned per measure-quarter via the validated day-of-week shift)")
        print(f"Load-shape source: REAL CEDARS impact-profile library "
              f"({batched.n_real_shape} of {n_total} measures matched a real profile; "
              f"{len(batched.unmatched_measure_ids)} flagged -- no real 8,760-hour profile match, "
              f"excluded from pricing rather than substituted with a synthetic placeholder)")
        print(f"Discounting: REAL per-IOU quarterly rate (pipeline/discount_settings.py), "
              f"base year (CET's 'First Year') = {base_year}\n")

        if batched.unmatched_measure_ids:
            flagged_path = DATA_DIR / f"flagged_no_hourly_profile_{args.dataset}.csv"
            pd.DataFrame({"measure_id": batched.unmatched_measure_ids}).to_csv(flagged_path, index=False)
            sample = ", ".join(str(x) for x in batched.unmatched_measure_ids[:10])
            more = f" (+{len(batched.unmatched_measure_ids) - 10} more)" if len(batched.unmatched_measure_ids) > 10 else ""
            print(f"Flagged measure_ids written to {flagged_path}: {sample}{more}\n")

        if batched.measure_year.empty:
            print("No measures had a real 8,760-hour profile match -- nothing to price "
                  "(see the flagged list above).")
            return

        batched_lifetime = batched.measure_year.groupby("measure_id")["annual_value"].sum().rename("batched_hourly_$")
        legacy_lifetime = legacy.groupby("measure_id")["annual_value"].sum().rename("legacy_net_then_shape_$")
        comparison = pd.concat([legacy_lifetime, batched_lifetime], axis=1)
        comparison["difference_$"] = comparison["batched_hourly_$"] - comparison["legacy_net_then_shape_$"]
        comparison = comparison.round(2)

        pd.set_option("display.width", 120)
        demo_rows = comparison.drop(index="DEMO_LoadShift1") if args.include_demo else comparison
        if len(demo_rows) <= 15:
            print("Lifetime discounted $ value per measure, both calculation orders:\n")
            print(demo_rows.to_string())
        else:
            print(f"Lifetime discounted $ value, {len(demo_rows)} measures (showing summary, not every row):\n")
            print(demo_rows.describe().to_string())

        if args.include_demo:
            print("\nDEMO_LoadShift1 (shown separately, it's the outlier by construction):")
            print(comparison.loc[["DEMO_LoadShift1"]].to_string())
            print("\n--- The point of this POC ---")
            print("DEMO_LoadShift1 is the case CET's calculation order cannot value:")
            print(f"  legacy_net_then_shape prices it at ${comparison.loc['DEMO_LoadShift1', 'legacy_net_then_shape_$']:.2f}"
                  f" (nets to ~0 kWh before pricing, so there is nothing to price)")
            print(f"  batched_hourly prices it at ${comparison.loc['DEMO_LoadShift1', 'batched_hourly_$']:.2f}"
                  f" (prices each hour before summing, so shifting load to cheaper hours shows real value)")

        print(f"\nPortfolio 8,760-hour accumulator: sum = {batched.portfolio_hourly.sum():.2f}, "
              f"peak hour value = {batched.portfolio_hourly.max():.4f}, "
              f"{len(batched.measure_year):,} measure-year rows priced (one per real avoided-cost "
              f"year across each measure's EUL, not one flat rate), {batched.elapsed_seconds*1000:.1f} ms.")

        gas_ready = gas_avoided_cost.is_available() and gas_savings_profiles.is_available()
        if gas_ready:
            gas_result, n_gas_measures, n_gas_matched = run_gas_benefit(measures, base_year=base_year)
            print(f"\n--- Gas benefit (new, 2026-09-15) ---")
            print(f"Gas ACC source: REAL 2024 Gas ACC monthly extraction, per-year rates 2024-2054 "
                  f"(TRC cost test, no day-of-week shift needed -- see 2024 Gas ACC Extraction/README)")
            print(f"Gas shape source: REAL CEDARS gas savings profiles "
                  f"({n_gas_matched} of {n_gas_measures} measures with real therm savings matched a real "
                  f"monthly shape)")
            if gas_result.empty:
                print("No gas benefit priced for this dataset (no measure had both a nonzero "
                      "annual_net_therms and a matched gas shape).")
            else:
                gas_lifetime = gas_result.groupby("measure_id")["gas_benefit"].sum().rename("gas_benefit_$").round(2)
                if len(gas_lifetime) <= 15:
                    print("\nLifetime discounted gas $ value per measure:\n")
                    print(gas_lifetime.to_string())
                else:
                    print(f"\nLifetime discounted gas $ value, {len(gas_lifetime)} measures with real gas "
                          f"savings (showing summary):\n")
                    print(gas_lifetime.describe().to_string())
                print(f"\nTotal portfolio gas benefit: ${gas_lifetime.sum():,.2f} across "
                      f"{len(gas_lifetime)} measures, {len(gas_result):,} measure-quarter rows priced.")
        else:
            print("\n(Gas benefit skipped -- run '2024 Gas ACC Extraction/code/extract_gas.py' and "
                  "ensure poc/data/gas_savings_profiles.csv exists to enable it.)")

        print("\nNOTE: this is the fully-real mode -- real per-measure kWh/therms, real CEDARS "
              "impact-profile and gas-savings shapes, and real hourly/monthly ACC data. "
              f"See compare_to_cet.py --dataset {args.dataset} --acc-mode real_hourly to check the "
              "electric numbers against real CET ElecBen (gas ground truth not yet wired in).")
        return

    avoided_cost, load_shapes, measures = build_caches(measures, acc_mode, acc_year)
    n_real = len(measures)
    if args.include_demo:
        measures = add_load_shifting_demo(measures, load_shapes, avoided_cost)

    acc_label = {
        "energy": f"REAL 2024 ACC energy-only $/kWh (year {acc_year})",
        "full": f"REAL 2024 ACC FULL (energy+capacity+transmission+distribution+GHG) (year {acc_year})",
        "synthetic": "SYNTHETIC placeholder curve",
    }[acc_mode]
    demo_note = " + 1 synthetic load-shifting demo measure" if args.include_demo else ""
    print(f"Dataset: {label}")
    print(f"Avoided-cost source: {acc_label}")
    print(f"Loaded {n_real} real measures{demo_note}. "
          f"Avoided-cost subsets: {len(avoided_cost)}. Load shapes: {len(load_shapes)}.\n")

    batched = price_batched_hourly(measures, load_shapes, avoided_cost, batch_size=500)
    legacy = legacy_net_then_shape(measures, load_shapes, avoided_cost)

    batched_lifetime = batched.measure_year.groupby("measure_id")["annual_value"].sum().rename("batched_hourly_$")
    legacy_lifetime = legacy.groupby("measure_id")["annual_value"].sum().rename("legacy_net_then_shape_$")

    comparison = pd.concat([legacy_lifetime, batched_lifetime], axis=1)
    comparison["difference_$"] = comparison["batched_hourly_$"] - comparison["legacy_net_then_shape_$"]
    comparison = comparison.round(2)

    pd.set_option("display.width", 120)
    demo_rows = comparison.drop(index="DEMO_LoadShift1") if args.include_demo else comparison
    if len(demo_rows) <= 15:
        print("Lifetime discounted $ value per measure, both calculation orders:\n")
        print(demo_rows.to_string())
    else:
        print(f"Lifetime discounted $ value, {len(demo_rows)} measures (showing summary, not every row):\n")
        print(demo_rows.describe().to_string())

    if args.include_demo:
        print("\nDEMO_LoadShift1 (shown separately, it's the outlier by construction):")
        print(comparison.loc[["DEMO_LoadShift1"]].to_string())
        print("\n--- The point of this POC ---")
        print("DEMO_LoadShift1 is the case CET's calculation order cannot value:")
        print(f"  legacy_net_then_shape prices it at ${comparison.loc['DEMO_LoadShift1', 'legacy_net_then_shape_$']:.2f}"
              f" (nets to ~0 kWh before pricing, so there is nothing to price)")
        print(f"  batched_hourly prices it at ${comparison.loc['DEMO_LoadShift1', 'batched_hourly_$']:.2f}"
              f" (prices each hour before summing, so shifting load to cheaper hours shows real value)")

    print(f"\nPortfolio 8,760-hour accumulator: sum = {batched.portfolio_hourly.sum():.2f}, "
          f"peak hour value = {batched.portfolio_hourly.max():.4f}, "
          f"processed in {batched.n_batches} batch(es) of up to {batched.batch_size} measures, "
          f"{batched.elapsed_seconds*1000:.1f} ms.")

    print(f"\nNOTE: annual_net_kwh/kW use real fields from real CET input data. Avoided-cost source: {acc_label}.")
    print("Load shapes are still a synthetic placeholder (see pipeline/synthetic_curves.py for how")
    print(f"to swap in real eTRM data). See compare_to_cet.py --dataset {args.dataset} to check these")
    print("numbers against real CET ElecBen (ground truth available for both datasets).")


if __name__ == "__main__":
    main()
