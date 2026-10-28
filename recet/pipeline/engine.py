"""
The pricing engine behind every ReCET run.

  price_real_hourly        electric benefit, hour by hour: each measure's 8,760 impact profile (aligned to the ACC calendar) times that year's hourly
                           avoided cost, summed within the ACC quarters and discounted quarterly at the PA's rate. Pricing each hour BEFORE summing is
                           what lets a load-shifting measure (zero net annual kWh) show its real value; CET nets annual savings first and prices it at $0.
                           price_real_hourly_per_measure is its per-measure reference (the tests require the two to agree).
  price_gas_benefit        gas benefit on monthly profiles and the monthly gas ACC (price_gas_benefit_per_measure is its reference).
  price_bill_impact        RIM bill reductions/increases, as CalcCE computes them (price_bill_impact_per_measure is its reference).
  price_participant_cost   participant / measure costs (price_participant_cost_per_measure is its reference).
  price_other_ben_cost     the other benefits and costs: refrigerant, gas infrastructure, misc., tax credits (price_other_ben_cost_per_measure).
  The three are computed on arrays over all measures at once; the *_per_measure versions are the same arithmetic one measure at a time, kept as
  readable references (tests/test_performance.py requires each pair to agree). Change the arithmetic in both.

The research-era pricers that used to live here (synthetic batched pricing, the "legacy net-then-shape" comparison, the TOU and SCG quarterly
routes) were moved to the CEDARS review research repository (poc/research_lib) or removed on 2026-10-09.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

HOURS_PER_YEAR = 8760

@dataclass
class HourlyPricingResult:
    # price_real_hourly: one row per priced measure (CEInputID, LifetimeValuePV = its lifetime discounted value, signed).
    # price_real_hourly_per_measure: one row per priced (CEInputID, Year, Quarter), the quarter's discounted value in QuarterValuePV.
    values: pd.DataFrame
    unpriced_ids: list = field(default_factory=list)   # CEInputIDs with no 8,760 profile (flagged, never priced on a made-up shape)
    # CEInputID -> how run_poc.run_real_hourly routed it ("hourly profile | <profile source>"); reporting only.
    route: dict = field(default_factory=dict)


# Real 2018 (non-leap) calendar quarter boundaries, in 0-based hour-of-year --
# same convention CEDARS QAC Validation/pipeline.py uses (90/91/92/92 days,
# NOT a flat 2,190-hour split). Real CalcCE prices and discounts at THIS
# granularity (quarterly), not annually -- see price_real_hourly's docstring.
QUARTER_BOUNDS = [(0, 2160), (2160, 4344), (4344, 6552), (6552, 8760)]


def rul_in_use(RUL_Yrs, EUL_Yrs):
    """The RUL CalcCE prices with (InputMeasurevw: `CASE WHEN RUL >= EUL THEN 0 ELSE RUL END`): an RUL that is not below the EUL is not an early
    replacement, so the measure is single-baseline for its whole life. Scalars or arrays; RUL_Yrs and EUL_Yrs stay as filed in the measure table."""
    RUL_Yrs, EUL_Yrs = np.asarray(RUL_Yrs, dtype=float), np.asarray(EUL_Yrs, dtype=float)
    return np.where(RUL_Yrs < EUL_Yrs, RUL_Yrs, 0.0)


def _baseline_quantity_in_quarter(k: int, rul_quarters: float, quantity_1st: float, quantity_2nd: float) -> float:
    """RUL-based dual-baseline blend for the k-th (0-based) quarter since a
    measure's claim date (2026-09-16 fix -- found comparing this POC
    against a live CET run on a real 73,024-measure accelerated-replacement
    filing, where the old "just subtract 1st minus 2nd baseline" formula
    silently zeroed out over a quarter of the real measures and
    underpriced gas by >50x on the rest -- see data_loading.py's
    _derive_fields() docstring for the full real-world evidence).

    A quarter fully within the pre-existing equipment's Remaining Useful
    Life (RUL) prices at the "1st baseline" (vs-existing-equipment) net
    savings rate; a quarter after RUL prices at the "2nd baseline"
    (vs-code-replacement) rate, since the old equipment would have been
    replaced by code-minimum equipment anyway once RUL expired. The one
    quarter straddling the RUL boundary (RUL is genuinely fractional in
    real data, e.g. 8.33 years) is linearly blended by how much of it
    falls on each side -- the same precision convention this project
    already uses for EUL's own final partial quarter.

    rul_quarters <= 0 (the normal, non-early-replacement case, and by far
    the majority of real measures) always returns scale_1st for the whole
    EUL -- a pure generalization of this project's original single-
    baseline behavior, not a change to it."""
    if rul_quarters <= 0 or k + 1 <= rul_quarters:
        return quantity_1st
    if k >= rul_quarters:
        return quantity_2nd
    share_on_1st_baseline = rul_quarters - k
    return share_on_1st_baseline * quantity_1st + (1.0 - share_on_1st_baseline) * quantity_2nd


def price_real_hourly_per_measure(
    measures: pd.DataFrame,
    get_shape: Callable[[pd.Series], tuple[np.ndarray, int] | None],
    get_acc_years: Callable[[pd.Series], dict[int, np.ndarray]],
    shift_fn: Callable[[np.ndarray, int, int], np.ndarray],
    get_discount_rate_qtr: Callable[[pd.Series], float],
    base_year: int,
) -> HourlyPricingResult:
    """
    THE PER-MEASURE VERSION: the reference for `price_real_hourly` below (which gives the same values, organised by distinct route so that the shift-and-sum work is done
    once per route instead of once per measure). Tests require the two to agree. Reads Priced_Quantity_1st/2nd_Baseline (the quantity each baseline's profile is
    multiplied by), EUL_Yrs, RUL_Yrs, Used_Year and Used_Quarter.

    The methodology lesson from CEDARS QAC Validation, PLUS the real CET
    discounting mechanism (found 2026-09-15 in the actual CalcCE SQL and
    confirmed by Jake, who has real CEDARS/CET practitioner access):

    1. Real avoided costs vary substantially year to year (GHG price alone:
       ~$24/tonne in 2025 vs. ~$214/tonne in 2031 -- see README), so pricing
       every EUL-year off one static representative-year curve (as
       the first proof of concept's batched pricer did) silently mis-prices any measure whose EUL
       spans years with materially different avoided costs. This prices
       EACH measure-quarter against ITS year's real avoided-cost array, and
       re-aligns the measure's own real impact-profile shape onto that
       year's real calendar first (shift_fn) -- see pipeline/
       real_impact_profiles.py and pipeline/real_avoided_cost_v2.py.

    2. Real CET discounts QUARTERLY (`.../POWER(Rqf, Qac)` in the real
       CalcCE procedure, where `Rqf = 1 + DiscountRateQtr`), not annually,
       and at a REAL, PA-specific rate (`pipeline/discount_settings.py`) --
       this engine used to apply one flat, uncalibrated 3%/year rate to
       every measure regardless of IOU. `Qac` counts quarters from a
       BaseYear reference point, which Jake confirmed is that CET run's own
       "First Year" receipt field (`base_year` here) -- NOT each measure's
       own claim year when a batch mixes claim years, and NOT the static
       BaseYear column in the settings table (which can differ, e.g. it
       reads 2024 for "Version 2026" while a real run's First Year was
       2025 or 2031 in this POC's two ground-truth jobs).

    3. Real EUL is genuinely fractional (e.g. 3.3 years) and CET prices in
       QUARTERS, prorating the final partial quarter by that fraction
       (confirmed: `(eulq1 - FLOOR(eulq1)) * benefit` in the real SQL) --
       this engine used to round EUL to the nearest whole year and price
       whole years only. It also matters WHICH quarter of claim_year a
       measure starts in: a measure claimed in Q4 only accrues one quarter
       of benefit in its first calendar year, not four -- this engine used
       to treat every measure's first year as a full four quarters
       regardless of its real claim_quarter.

    get_shape(measure_row) -> (normalized_8760_shape, represents_year), or
    None if no real CEDARS profile could be matched: such a measure is
    FLAGGED (2026-09-16, Jake's direction), never priced against a made-up
    shape -- its CEInputID is collected into the result's `unpriced_ids`
    and it gets no row.

    get_acc_years(measure_row) -> {year: array(8760,)}; a year outside
    what's returned clamps to the nearest available year.

    get_discount_rate_qtr(measure_row) -> that measure's real PA's nominal
    quarterly discount rate (DiscountRateQtr, already a flat rate, not
    derived from the annual one -- see discount_settings.py).

    Not vectorized like the first proof of concept's scale benchmark -- this is
    a correctness-focused real-data run (thousands, not millions, of
    measures), so a straightforward per-measure Python loop keeps the
    methodology easy to line up against CEDARS QAC Validation's own code
    and the real CalcCE SQL.
    """
    rows: list[dict] = []
    unpriced_ids: list = []

    for _, m in measures.iterrows():
        profile_hit = get_shape(m)
        if profile_hit is None:
            unpriced_ids.append(m["CEInputID"])
            continue
        shape, represents_year = profile_hit

        acc_years = get_acc_years(m)
        acc_years_available = sorted(acc_years)
        claim_year = int(m["Used_Year"])
        claim_quarter = int(m["Used_Quarter"])  # 1-4, from ClaimYearQuarter
        quarterly_discount_factor = 1.0 + get_discount_rate_qtr(m)          # CalcCE's Rqf

        eul_quarters = float(m["EUL_Yrs"]) * 4.0                            # CalcCE's eulq
        whole_quarters = int(eul_quarters)
        last_quarter_fraction = eul_quarters - whole_quarters  # prorates the final quarter, per real CalcCE
        n_priced_quarters = whole_quarters + (1 if last_quarter_fraction > 1e-9 else 0)
        rul_quarters = float(rul_in_use(m["RUL_Yrs"], m["EUL_Yrs"])) * 4.0   # CalcCE's rulq
        quantity_1st = float(m["Priced_Quantity_1st_Baseline"])
        quantity_2nd = float(m["Priced_Quantity_2nd_Baseline"])

        # profile x price is cached per calendar year -- the baseline quantity is applied per quarter below, since two quarters in the SAME calendar year can
        # straddle the RUL boundary and need different quantities even though they share one hourly array.
        profile_x_price_by_year: dict[int, np.ndarray] = {}
        for k in range(n_priced_quarters):
            quarters_since_claim_year = (claim_quarter - 1) + k  # 0-based quarters elapsed since the claim year's own Q1
            priced_year = claim_year + quarters_since_claim_year // 4
            priced_quarter = quarters_since_claim_year % 4  # 0=Q1 .. 3=Q4

            if priced_year not in profile_x_price_by_year:
                nearest_acc_year = min(acc_years_available, key=lambda y: abs(y - priced_year))
                aligned_profile = shift_fn(shape, priced_year, represents_year)
                profile_x_price_by_year[priced_year] = aligned_profile * acc_years[nearest_acc_year]
            quantity_this_quarter = _baseline_quantity_in_quarter(k, rul_quarters, quantity_1st, quantity_2nd)
            hourly_value = profile_x_price_by_year[priced_year] * quantity_this_quarter

            q_start, q_end = QUARTER_BOUNDS[priced_quarter]
            is_last = k == n_priced_quarters - 1
            quarter_weight = last_quarter_fraction if (is_last and last_quarter_fraction > 1e-9) else 1.0

            quarter_value = float(hourly_value[q_start:q_end].sum()) * quarter_weight
            quarters_from_first_year = (priced_year - base_year) * 4 + priced_quarter  # CalcCE's Qac: quarters since the First Year's own Q1
            discount_factor = 1.0 / (quarterly_discount_factor ** quarters_from_first_year)

            rows.append({
                "CEInputID": m["CEInputID"],
                "Year": priced_year,
                "Quarter": priced_quarter + 1,
                "QuarterValuePV": quarter_value * discount_factor,
            })

    return HourlyPricingResult(values=pd.DataFrame(rows, columns=["CEInputID", "Year", "Quarter", "QuarterValuePV"]), unpriced_ids=unpriced_ids)


def _quarter_rows(claim_year: np.ndarray, claim_quarter: np.ndarray, EUL_Yrs: np.ndarray, RUL_Yrs: np.ndarray, quantity_1st: np.ndarray, quantity_2nd: np.ndarray,
                  quarterly_discount_factor: np.ndarray, base_year: int):
    """Expand measures (one entry per measure in every input array) into one row per (measure, quarter of its life), with everything CalcCE applies per quarter:
    returns (row_measure, priced_year, priced_quarter 0-3, quarter_weight, quantity_this_quarter, discount_factor).
      quarter_weight        = 1, or the fraction of the last quarter of a fractional life (`(eulq - floor(eulq))`);
      quantity_this_quarter = `_baseline_quantity_in_quarter`: the 1st-baseline quantity before the RUL, the 2nd after, a blend in the quarter that contains it;
      discount_factor       = 1 / Rqf ^ Qac, Qac = calendar quarters from Q1 of the First Year.
    The same arithmetic as the per-measure loops, evaluated on flat arrays."""
    eul_quarters = EUL_Yrs * 4.0                                                          # CalcCE's eulq
    whole_quarters = eul_quarters.astype(np.int64)
    last_quarter_fraction = eul_quarters - whole_quarters                                 # the last, partial quarter
    n_priced_quarters = whole_quarters + (last_quarter_fraction > 1e-9)
    rul_quarters = RUL_Yrs * 4.0                                                          # CalcCE's rulq
    total = int(n_priced_quarters.sum())
    row_measure = np.repeat(np.arange(len(n_priced_quarters)), n_priced_quarters)
    life_quarter = np.arange(total) - np.repeat(np.cumsum(n_priced_quarters) - n_priced_quarters, n_priced_quarters)   # k: 0-based quarter of life
    quarters_since_claim_year = (claim_quarter[row_measure] - 1) + life_quarter          # 0-based quarters elapsed since the claim year's Q1
    priced_year = claim_year[row_measure] + quarters_since_claim_year // 4
    priced_quarter = quarters_since_claim_year % 4                                         # 0 = Q1 .. 3 = Q4
    is_last = life_quarter == n_priced_quarters[row_measure] - 1
    quarter_weight = np.where(is_last & (last_quarter_fraction[row_measure] > 1e-9), last_quarter_fraction[row_measure], 1.0)
    rq = rul_quarters[row_measure]
    share_on_1st_baseline = rq - life_quarter
    q1, q2 = quantity_1st[row_measure], quantity_2nd[row_measure]
    quantity_this_quarter = np.where((rq <= 0) | (life_quarter + 1 <= rq), q1,
                                     np.where(life_quarter >= rq, q2, share_on_1st_baseline * q1 + (1.0 - share_on_1st_baseline) * q2))
    discount_factor = 1.0 / (quarterly_discount_factor[row_measure] ** ((priced_year - base_year) * 4 + priced_quarter))
    return row_measure, priced_year, priced_quarter, quarter_weight, quantity_this_quarter, discount_factor


def _lifetime_values(route_of_measure: np.ndarray, claim_year: np.ndarray, claim_quarter: np.ndarray, EUL_Yrs: np.ndarray, RUL_Yrs: np.ndarray,
                     quantity_1st: np.ndarray, quantity_2nd: np.ndarray, quarterly_discount_factor_by_route: np.ndarray, base_year: int,
                     fill_value_per_unit_quantity: Callable[[int, list[int]], np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Lifetime discounted value of each measure (one entry per measure in every input array; g_m = its route), without a row per measure-quarter.

    The per-measure value is  SUM_k quantity(k) * weight(k) * discount(k) * U(route, year_k, quarter_k)  (`_quarter_rows`), and quantity(k) is the 1st-baseline
    quantity times f(k) plus the 2nd times 1 - f(k), with f(k) = 1 before the RUL, 0 after and the blend in the quarter that holds it. So
        value = quantity_1st * A + quantity_2nd * B,   A = SUM_k f(k) * weight(k) * discount(k) * U,   B = SUM_k (1 - f(k)) * weight(k) * discount(k) * U
    where A and B depend only on (route, claim year, claim quarter, EUL, RUL) -- the route fixes the discount rate. Every quarter is still discounted on its own,
    exactly as before; A and B are computed once per distinct key instead of once per measure.

    fill_value_per_unit_quantity(g, years) -> array (len(years), 4): U for route g in each listed year's four quarters.
    Returns (value per measure, has_life per measure: False where the EUL gives no quarter at all, which the per-measure versions leave without a row)."""
    life_keys = np.column_stack([route_of_measure, claim_year, claim_quarter, EUL_Yrs, RUL_Yrs]).astype(np.float64)
    distinct_lives, life_of_measure = np.unique(life_keys, axis=0, return_inverse=True)
    life_of_measure = life_of_measure.ravel()
    n_lives = len(distinct_lives)
    route_of_life = distinct_lives[:, 0].astype(np.int64)
    row_life, priced_year, priced_quarter, quarter_weight, share_on_1st_baseline, discount_factor = _quarter_rows(
        distinct_lives[:, 1].astype(np.int64), distinct_lives[:, 2].astype(np.int64), distinct_lives[:, 3], distinct_lives[:, 4], np.ones(n_lives),
        np.zeros(n_lives), quarterly_discount_factor_by_route[route_of_life], base_year)       # quantities 1 and 0: the 1st-baseline share of each quarter
    has_life = np.bincount(row_life, minlength=n_lives) > 0
    if not len(row_life):
        return np.zeros(len(route_of_measure)), has_life[life_of_measure]

    # --- U(route, year, quarter), once per route and year
    first_year, last_year = int(priced_year.min()), int(priced_year.max())
    value_per_unit_quantity = np.zeros((int(route_of_life.max()) + 1, last_year - first_year + 1, 4))
    row_route = route_of_life[row_life]
    by_route = np.argsort(row_route, kind="stable")
    for route_rows in np.split(by_route, np.flatnonzero(np.diff(row_route[by_route])) + 1):
        g = int(row_route[route_rows[0]])
        years = [int(y) for y in np.unique(priced_year[route_rows])]
        value_per_unit_quantity[g, [y - first_year for y in years]] = fill_value_per_unit_quantity(g, years)

    discounted_unit_value = quarter_weight * discount_factor * value_per_unit_quantity[row_route, priced_year - first_year, priced_quarter]
    pv_per_unit_1st_baseline = np.bincount(row_life, weights=share_on_1st_baseline * discounted_unit_value, minlength=n_lives)
    pv_per_unit_2nd_baseline = np.bincount(row_life, weights=(1.0 - share_on_1st_baseline) * discounted_unit_value, minlength=n_lives)
    return (quantity_1st * pv_per_unit_1st_baseline[life_of_measure] + quantity_2nd * pv_per_unit_2nd_baseline[life_of_measure],
            has_life[life_of_measure])


def price_real_hourly(
    measures: pd.DataFrame,
    get_shape: Callable[[pd.Series], tuple[np.ndarray, int] | None],
    get_acc_years: Callable[[pd.Series], dict[int, np.ndarray]],
    shift_fn: Callable[[np.ndarray, int, int], np.ndarray],
    get_discount_rate_qtr: Callable[[pd.Series], float],
    base_year: int,
    route_key=None,
) -> HourlyPricingResult:
    """The same pricing as `price_real_hourly_per_measure` (read its docstring for the mechanics and the lessons behind each step), organised so that work is done once
    per DISTINCT SITUATION instead of once per measure.

    A measure's lifetime value is  SUM over its quarters k of  quantity(k) * weight(k) * discount(k) * U,  where
        U = SUM over the hours of quarter(k) of  (calendar-aligned profile)(h) * (avoided cost of the nearest ACC year)(h)
    depends only on the measure's profile, its avoided-cost series (IOU and zone), the pricing year and the quarter -- never on how many kWh the measure saves. Real
    jobs have few of those: 169 distinct (profile, IOU, sector, zone) routes in a 5,518-measure job, 48 in a 73,024-measure filing. So U is computed once per (route,
    year, quarter), and the sum over quarters once per distinct (route, claim year, claim quarter, EUL, RUL) -- `_lifetime_values` -- with no row per measure-quarter.
    quantity(k) is Priced_Quantity_1st_Baseline / _2nd_Baseline (filled by the caller) phased by the RUL.

    route_key: one hashable per measure (aligned with `measures`) naming its route; `get_shape`, `get_acc_years` and `get_discount_rate_qtr` are called once per route, on
    that route's first measure, and must give the same answer for every measure of it. None = every measure is its own route. The result has one row per priced
    measure (CEInputID, LifetimeValuePV = its lifetime discounted value): the per-measure version's (CEInputID, Year, Quarter, QuarterValuePV) rows summed by measure,
    to rounding (the multiplications are ordered differently)."""
    n = len(measures)
    ids = measures["CEInputID"].to_numpy()
    if route_key is None:
        route_code = np.arange(n)
    else:
        route_code, _uniques = pd.factorize(pd.Series(list(route_key), dtype=object))
    n_routes = int(route_code.max()) + 1 if n else 0
    _, route_first_row = np.unique(route_code, return_index=True) if n else (None, np.array([], dtype=int))

    # --- one profile / ACC series / rate per route, read off the route's first measure (one row lookup for all of them, not one per route)
    profile_by_route: dict[int, tuple[np.ndarray, int]] = {}
    acc_by_route: dict[int, dict[int, np.ndarray]] = {}
    quarterly_discount_factor_by_route = np.ones(n_routes)                     # CalcCE's Rqf = 1 + DiscountRateQtr
    for g, rep in enumerate(measures.iloc[route_first_row].to_dict("records") if n else []):
        hit = get_shape(rep)
        if hit is None:
            continue
        profile_by_route[g] = hit
        acc_by_route[g] = get_acc_years(rep)
        quarterly_discount_factor_by_route[g] = 1.0 + get_discount_rate_qtr(rep)
    route_has_profile = np.zeros(max(n_routes, 1), dtype=bool)
    route_has_profile[list(profile_by_route)] = True
    has_profile = route_has_profile[route_code] if n else np.zeros(0, dtype=bool)
    unpriced_ids = ids[~has_profile].tolist()

    if not has_profile.any():
        return HourlyPricingResult(values=pd.DataFrame({"CEInputID": pd.Series(dtype=object), "LifetimeValuePV": pd.Series(dtype=float)}),
                                   unpriced_ids=unpriced_ids)

    align_once_per_route = getattr(shift_fn, "year_independent", False)       # the production shift (real_impact_profiles.shift_for_year) ignores the year

    def fill_value_per_unit_quantity(g: int, years: list[int]) -> np.ndarray:
        """U(route g, year, quarter): the four quarterly sums of aligned profile x price, against the nearest ACC year."""
        shape, represents_year = profile_by_route[g]
        series = acc_by_route[g]
        acc_years_available = sorted(series)
        out = np.zeros((len(years), 4))
        aligned_profile = shift_fn(shape, years[0], represents_year) if align_once_per_route else None
        for i, y in enumerate(years):
            nearest_acc_year = min(acc_years_available, key=lambda v: abs(v - y))
            profile_x_price = (aligned_profile if align_once_per_route else shift_fn(shape, y, represents_year)) * series[nearest_acc_year]
            for q, (lo, hi) in enumerate(QUARTER_BOUNDS):
                out[i, q] = float(profile_x_price[lo:hi].sum())
        return out

    m = measures
    value, has_life = _lifetime_values(
        route_code[has_profile], m["Used_Year"].to_numpy()[has_profile].astype(np.int64), m["Used_Quarter"].to_numpy()[has_profile].astype(np.int64),
        m["EUL_Yrs"].to_numpy(dtype=np.float64)[has_profile], rul_in_use(m["RUL_Yrs"], m["EUL_Yrs"])[has_profile],
        m["Priced_Quantity_1st_Baseline"].to_numpy(dtype=np.float64)[has_profile], m["Priced_Quantity_2nd_Baseline"].to_numpy(dtype=np.float64)[has_profile],
        quarterly_discount_factor_by_route, base_year, fill_value_per_unit_quantity)
    return HourlyPricingResult(values=pd.DataFrame({"CEInputID": ids[has_profile][has_life], "LifetimeValuePV": value[has_life]}), unpriced_ids=unpriced_ids)


# Real calendar quarters as 0-based [start, end) MONTH slices (Jan..Dec == indices 0..11) --
# the gas counterpart to QUARTER_BOUNDS above, which is in hours instead.
GAS_QUARTER_MONTHS = [(0, 3), (3, 6), (6, 9), (9, 12)]


def price_gas_benefit_per_measure(
    measures: pd.DataFrame,
    get_gas_shape: Callable[[pd.Series], np.ndarray | None],
    get_gas_acc_years: Callable[[pd.Series], dict[int, np.ndarray]],
    get_discount_rate_qtr: Callable[[pd.Series], float],
    base_year: int,
) -> pd.DataFrame:
    """
    THE PER-MEASURE VERSION: the reference for `price_gas_benefit` below, which gives the same values organised by distinct route.

    Real gas benefit: a measure's real monthly gas-savings shape
    (pipeline/gas_savings_profiles.py) times its annual net therms,
    times the real monthly gas avoided cost for THAT quarter's own target
    year (pipeline/gas_avoided_cost.py, extracted from the actual 2024 Gas
    ACC model's real 2024-2054 series), aggregated into real calendar
    quarters (Jan-Mar/Apr-Jun/Jul-Sep/Oct-Dec) and discounted with the SAME
    real per-IOU quarterly mechanism as price_real_hourly (see
    discount_settings.py and that function's docstring for the full
    Qac/base-year mechanics) -- CET applies one discounting apparatus to
    every measure-level benefit stream regardless of fuel.

    NO day-of-week shift, unlike the electric side -- Jake confirmed
    monthly gas data doesn't need it ("there is no need for date
    shifting"), so get_gas_shape's result is used as-is.

    PER-YEAR RATE (2026-09-15, mirrors price_real_hourly's electric-side
    design): a measure whose EUL spans multiple years now prices each
    year's quarters against THAT year's own real gas avoided-cost rate
    (get_gas_acc_years returns {year: 12-month array}, 2024-2054), not one
    flat year reused for the whole EUL like this function used to do
    before the Gas ACC model's real per-year table was found (it was
    sitting right next to the single-year value this used to read, just
    never wired in -- see gas_avoided_cost.py's docstring). Years beyond
    2054 (or before 2024) clamp to the nearest covered year.

    Measures with no real gas shape match or zero annual net therms
    (BOTH baseline phases -- see below) are skipped (no row emitted)
    rather than priced at a meaningless zero.

    DUAL-BASELINE / accelerated-replacement measures (2026-09-16 fix, same
    root cause and fix as the electric side -- see engine._baseline_quantity_in_quarter()
    and data_loading.py's _derive_fields() docstring for the full
    real-world evidence): the 1st-baseline therms apply for quarters within
    the pre-existing equipment's Remaining Useful Life, the 2nd-baseline
    therms for quarters after. RUL_Yrs <= 0 (not an early replacement, the
    normal case) always uses the 1st-baseline therms for the whole EUL.
    The therms are Priced_Quantity_1st_Baseline / _2nd_Baseline (run_poc.run_gas_benefit: AC_Net_Therms_*, with market effects).
    """
    rows: list[dict] = []

    for _, m in measures.iterrows():
        therms_1st = float(m["Priced_Quantity_1st_Baseline"])
        therms_2nd = float(m["Priced_Quantity_2nd_Baseline"])
        if therms_1st == 0 and therms_2nd == 0:
            continue
        shape = get_gas_shape(m)
        if shape is None:
            continue

        acc_years = get_gas_acc_years(m)
        rul_quarters = float(rul_in_use(m["RUL_Yrs"], m["EUL_Yrs"])) * 4.0   # CalcCE's rulq

        claim_year = int(m["Used_Year"])
        claim_quarter = int(m["Used_Quarter"])
        quarterly_discount_factor = 1.0 + get_discount_rate_qtr(m)          # CalcCE's Rqf

        eul_quarters = float(m["EUL_Yrs"]) * 4.0                            # CalcCE's eulq
        whole_quarters = int(eul_quarters)
        last_quarter_fraction = eul_quarters - whole_quarters
        n_priced_quarters = whole_quarters + (1 if last_quarter_fraction > 1e-9 else 0)

        for k in range(n_priced_quarters):
            quarters_since_claim_year = (claim_quarter - 1) + k
            priced_year = claim_year + quarters_since_claim_year // 4
            priced_quarter = quarters_since_claim_year % 4
            is_last = k == n_priced_quarters - 1
            quarter_weight = last_quarter_fraction if (is_last and last_quarter_fraction > 1e-9) else 1.0

            therms_this_quarter = _baseline_quantity_in_quarter(k, rul_quarters, therms_1st, therms_2nd)
            acc_year_used = min(max(priced_year, min(acc_years)), max(acc_years))          # years outside the gas ACC use its first / last year
            monthly_price = acc_years[acc_year_used]
            monthly_value = shape * therms_this_quarter * monthly_price
            first_month, end_month = GAS_QUARTER_MONTHS[priced_quarter]
            quarter_value = monthly_value[first_month:end_month].sum()

            quarters_from_first_year = (priced_year - base_year) * 4 + priced_quarter       # CalcCE's Qac
            discount_factor = 1.0 / (quarterly_discount_factor ** quarters_from_first_year)

            rows.append({
                "CEInputID": m["CEInputID"],
                "Year": priced_year,
                "Quarter": priced_quarter + 1,
                "QuarterValuePV": quarter_value * quarter_weight * discount_factor,
            })

    return pd.DataFrame(rows, columns=["CEInputID", "Year", "Quarter", "QuarterValuePV"])


def price_gas_benefit(
    measures: pd.DataFrame,
    get_gas_shape: Callable[[pd.Series], np.ndarray | None],
    get_gas_acc_years: Callable[[pd.Series], dict[int, np.ndarray]],
    get_discount_rate_qtr: Callable[[pd.Series], float],
    base_year: int,
    route_key=None,
) -> pd.DataFrame:
    """`price_gas_benefit_per_measure` (read its docstring for the mechanics) organised by distinct route, like `price_real_hourly`: the gas value of a quarter is
        therms(k) * U,   U = SUM over the quarter's months of  (monthly share)(m) * ($/therm of the covered year)(m),
    and U depends only on the measure's profile, IOU, gas sector and the pricing year, so it is computed once per (route, year, quarter), and the sum over quarters once
    per distinct (route, claim year, claim quarter, EUL, RUL) (`_lifetime_values`). route_key: one hashable per measure naming its route (the callbacks are called once
    per route, on its first measure with gas savings, and must give the same answer for every measure of it); None = every measure is its own route.
    Returns one row per priced measure (CEInputID, LifetimeValuePV = its lifetime discounted value): the per-measure version's rows summed by measure. Measures with
    no therms or no matching profile get no row."""
    n = len(measures)
    therms_1st = measures["Priced_Quantity_1st_Baseline"].to_numpy(dtype=np.float64)
    therms_2nd = measures["Priced_Quantity_2nd_Baseline"].to_numpy(dtype=np.float64)
    has_gas_savings = (therms_1st != 0) | (therms_2nd != 0)
    empty = pd.DataFrame({"CEInputID": pd.Series(dtype=object), "LifetimeValuePV": pd.Series(dtype=float)})
    if not has_gas_savings.any():
        return empty
    route_code = np.arange(n) if route_key is None else pd.factorize(pd.Series(list(route_key), dtype=object))[0]
    n_routes = int(route_code.max()) + 1
    gas_rows = np.flatnonzero(has_gas_savings)
    gas_routes, gas_route_first_row = np.unique(route_code[gas_rows], return_index=True)              # each route's first measure with gas savings
    profile_by_route: dict[int, np.ndarray] = {}
    acc_by_route: dict[int, dict[int, np.ndarray]] = {}
    quarterly_discount_factor_by_route = np.ones(n_routes)                     # CalcCE's Rqf
    for g, rep in zip(gas_routes.tolist(), measures.iloc[gas_rows[gas_route_first_row]].to_dict("records")):
        shape = get_gas_shape(rep)
        if shape is None:
            continue
        profile_by_route[g] = shape
        acc_by_route[g] = get_gas_acc_years(rep)
        quarterly_discount_factor_by_route[g] = 1.0 + get_discount_rate_qtr(rep)
    route_has_profile = np.zeros(n_routes, dtype=bool)
    route_has_profile[list(profile_by_route)] = True
    priced = has_gas_savings & route_has_profile[route_code]
    if not priced.any():
        return empty

    def fill_value_per_unit_quantity(g: int, years: list[int]) -> np.ndarray:
        shape, series = profile_by_route[g], acc_by_route[g]
        first_acc_year, last_acc_year = min(series), max(series)               # years outside the gas ACC use its first / last year
        out = np.zeros((len(years), 4))
        for i, y in enumerate(years):
            share_x_price = shape * series[min(max(y, first_acc_year), last_acc_year)]
            for q, (first_month, end_month) in enumerate(GAS_QUARTER_MONTHS):
                out[i, q] = float(share_x_price[first_month:end_month].sum())
        return out

    m = measures
    value, has_life = _lifetime_values(
        route_code[priced], m["Used_Year"].to_numpy()[priced].astype(np.int64), m["Used_Quarter"].to_numpy()[priced].astype(np.int64),
        m["EUL_Yrs"].to_numpy(dtype=np.float64)[priced], rul_in_use(m["RUL_Yrs"], m["EUL_Yrs"])[priced],
        therms_1st[priced], therms_2nd[priced], quarterly_discount_factor_by_route, base_year, fill_value_per_unit_quantity)
    return pd.DataFrame({"CEInputID": m["CEInputID"].to_numpy()[priced][has_life], "LifetimeValuePV": value[has_life]})


def price_bill_impact_per_measure(
    measures: pd.DataFrame,
    get_elec_rate_years: Callable[[pd.Series], dict[int, float] | None],
    get_gas_rate_years: Callable[[pd.Series], dict[int, float] | None],
    get_discount_rate_annual: Callable[[pd.Series], float],
    run_parameters_base_year: int = 2024,
) -> pd.DataFrame:
    """
    The quantities billed are Billed_kWh_1st/2nd_Baseline and Billed_Therms_1st/2nd_Baseline (filled by run_poc.run_bill_impact: net annual kWh / therms with
    installation and realization rates, and market effects). run_parameters_base_year is RunParameters' BaseYear (CalcCE's Yr1 = claim year - BaseYear).

    Real ratepayer bill impact (`LifecycleBillReducElec`/`LifecycleBillIncrElec`/
    `LifecycleBillReducGas`/`LifecycleBillIncrGas` -- the RIM test's other half, alongside
    system avoided cost: `RIMCost = LifecycleBillReducElec + LifecycleBillReducGas +
    PACCost`), traced directly from `CalcCE`'s real SQL
    (CET_v25.1_documentation_extracted.txt:15859-16106).

    UNLIKE every other benefit stream in this POC, the real calc here is
    ANNUAL, not quarterly: pipeline/rate_schedules.py's `RateE`/`RateG`
    values are one-per-calendar-year (the real `RateScheduleElec[/Gas]`
    tables' "Qy" field, despite its name, increments by year, not
    quarter -- confirmed empirically), and `CalcCE`'s own PV-sum for this
    piece discounts with the ANNUAL `Raf` (1+DiscountRateAnnual), not the
    quarterly `Rqf` this POC's other pricing functions use -- consistent
    with discount_settings.py's documented distinction between
    quarterly-granularity and annual-granularity benefit streams.

    A REAL, CONFIRMED ASYMMETRY in CalcCE's own SQL (found 2026-09-17,
    chasing a RUL-cohort-specific ~7-9% overshoot that only appeared for
    measures with RUL_Yrs > 0 -- RUL=0 measures matched CET's real
    LifecycleBillIncrElec/LifecycleBillReducGas EXACTLY on the first attempt): the "pure 1st
    baseline" years (`RE1`, `Qy Between Yr1 and Yr1+floor(RUL)-1`) discount
    by `Raf ^ (Qy - Yr1)` -- i.e. plain years-elapsed-since-claim-year, "k"
    in this function's loop -- but the boundary year AND every "2nd
    baseline" year after it (`RateEFrac1`, `RateEFrac2_1`, `RE2`,
    `RateEFrac2_2`) all discount by `Raf ^ Qy` instead, using Qy's own
    "years since calendar 2024" index directly, NOT years-since-claim-year.
    Since `Qy = (Qy-Yr1) + Yr1` and `Yr1 = claim_year - 2024`, this means
    every post-boundary year is discounted `Yr1` extra years relative to
    what the 1st-baseline years get -- a real quirk (bug or not, this is
    what CalcCE's real SQL does) this function must reproduce, not
    "correct". Verified exactly (ratio 0.99999995) against one real
    SCE/RUL=8.33 SWWH025 measure's actual `LifecycleBillIncrElec` before trusting
    it at scale. `_baseline_quantity_in_quarter()` isn't used here for this reason -- its
    single-exponent design can't express two different discount bases for
    the same boundary year, so the split is done directly.

    SIGN CLASSIFICATION: the real SQL splits each measure's single
    lifetime PV total into Reduc (bill goes down) or Incr (bill goes up)
    by its SIGN, with one wrinkle this function doesn't yet replicate: for
    a measure that ISN'T fuel-substitution, the real SQL always books the
    (normally-positive-by-construction) value into the Reduc column
    regardless of sign, whereas this function books a hypothetical
    negative value for such a measure into Incr instead. Every measure in
    this POC's ground-truth datasets is 100% fuel-substitution (SWWH025),
    where the real SQL's own branching is ALREADY pure sign-based
    exactly like this function -- so this simplification is unvalidated
    for a non-fuel-sub filing with unexpectedly-negative savings, not
    wrong for anything tested so far.

    A measure with no rate-schedule coverage (get_*_rate_years returns
    None, or a target year falls outside the table's real span) is
    skipped for that fuel only -- e.g. a fuel-sub-to-elec measure with no
    matching gas schedule still gets a real LifecycleBillIncrElec, just no
    LifecycleBillReducGas row.
    """
    rows: list[dict] = []

    for _, m in measures.iterrows():
        claim_year = int(m["Used_Year"])
        years_from_base_year = claim_year - run_parameters_base_year  # CalcCE's own Yr1 (RunParameters BaseYear: 2024 for Versions 2026 and 2027) -- see docstring
        annual_discount_factor = 1.0 + get_discount_rate_annual(m)               # CalcCE's Raf
        EUL_Yrs = float(m["EUL_Yrs"])
        RUL_Yrs = float(rul_in_use(m["RUL_Yrs"], EUL_Yrs))
        whole_years = int(EUL_Yrs)
        last_year_fraction = EUL_Yrs - whole_years
        n_billed_years = whole_years + (1 if last_year_fraction > 1e-9 else 0)
        # eul1 (real CalcCE field, InputMeasurevw): RUL if 0 < RUL < EUL, else
        # the WHOLE EUL -- i.e. RUL_Yrs==0 (or RUL >= EUL, which rul_in_use makes 0) means "1st baseline for 100% of the
        # EUL", same convention as _baseline_quantity_in_quarter() elsewhere in this file.
        years_on_1st_baseline = int(RUL_Yrs) if RUL_Yrs > 0 else whole_years
        # Share of the boundary year that is still 1st baseline: the fractional part of RUL, 0 when RUL is a whole number, and 1 when RUL <= 0 (a single-baseline
        # measure's fractional last year is entirely 1st baseline). Until 2026-09-21 this was `RUL - floor(RUL)` also for RUL = 0, which is -whole_years: a
        # single-baseline measure with a fractional EUL (e.g. 4.97 years) booked a NEGATIVE last year and priced at ~0.70x CET (BillReduc/RIM).
        boundary_year_share_1st = (RUL_Yrs - years_on_1st_baseline) if RUL_Yrs > 0 else 1.0

        def bill_pv(quantity_1st: float, quantity_2nd: float, retail_rates: dict[int, float] | None) -> float | None:
            if retail_rates is None:
                return None
            total = 0.0
            # RE1: pure 1st-baseline years, discounted by k = Qy - Yr1
            for k in range(min(years_on_1st_baseline, n_billed_years)):
                rate = retail_rates.get(claim_year + k)
                if rate is not None:
                    total += quantity_1st * rate / (annual_discount_factor ** k)
            # boundary year + RE2: 2nd-baseline (and the boundary's 1st-baseline
            # remainder), discounted by Qy = k + Yr1 instead -- see docstring
            for k in range(years_on_1st_baseline, n_billed_years):
                rate = retail_rates.get(claim_year + k)
                if rate is None:
                    continue
                is_last = k == n_billed_years - 1
                year_weight = last_year_fraction if (is_last and last_year_fraction > 1e-9) else 1.0
                quantity_this_year = (boundary_year_share_1st * quantity_1st + (1.0 - boundary_year_share_1st) * quantity_2nd
                                      if k == years_on_1st_baseline else quantity_2nd)
                total += year_weight * quantity_this_year * rate / (annual_discount_factor ** (k + years_from_base_year))
            return total

        elec_bill_pv = bill_pv(float(m["Billed_kWh_1st_Baseline"]), float(m["Billed_kWh_2nd_Baseline"]), get_elec_rate_years(m))
        gas_bill_pv = bill_pv(float(m["Billed_Therms_1st_Baseline"]), float(m["Billed_Therms_2nd_Baseline"]), get_gas_rate_years(m))

        # Reduc/Incr split (CalcCE 15880-15992): a fuel-substitution measure books a negative value as an INCREASE (Incr, positive);
        # every other measure keeps its signed value in Reduc and has Incr = 0 (`ISNULL(ft.FuelType,0)='0'` branch).
        Fuel_Sub_Flag = bool(m.get("Fuel_Sub_Flag", True))  # default True = the original (fuel-sub-only) behavior for frames without the flag

        def split(pv):
            if pv is None:
                return 0.0, 0.0
            return (max(pv, 0.0), max(-pv, 0.0)) if Fuel_Sub_Flag else (pv, 0.0)

        LifecycleBillReducElec, LifecycleBillIncrElec = split(elec_bill_pv)
        LifecycleBillReducGas, LifecycleBillIncrGas = split(gas_bill_pv)
        rows.append({
            "CEInputID": m["CEInputID"],
            "LifecycleBillReducElec": LifecycleBillReducElec,
            "LifecycleBillIncrElec": LifecycleBillIncrElec,
            "LifecycleBillReducGas": LifecycleBillReducGas,
            "LifecycleBillIncrGas": LifecycleBillIncrGas,
        })

    return pd.DataFrame(rows)


def price_participant_cost_per_measure(
    measures: pd.DataFrame,
    get_discount_rate_qtr: Callable[[pd.Series], float],
    base_year: int,
    market_effects_cost: float = 0.0,
) -> pd.DataFrame:
    """
    Real participant-cost columns, traced from `CalcCost`'s
    `RebatesAndIncentives`/`ExcessIncentives`/`GrossMeasureCostAdjusted`/
    `ParticipantCost` CTEs (CET_v25.1_documentation_extracted.txt:16664-
    16836). Program-level admin/marketing/DI costs (`ProgramCost.csv`) are
    explicitly OUT OF SCOPE here, same as everywhere else in this POC (see
    discount_settings.py's docstring) -- both this POC's real ground-truth
    jobs have all-zero ProgramCost.csv rows, so the program-cost-weighted
    share CalcCE/CalcCost would otherwise fold into TRCCost/PACCost is
    genuinely 0 for them, not an approximation being glossed over.

    `Qm`, CalcCost's own one-time (not per-quarter-of-lifetime) discount
    exponent for a measure's incentive/rebate/cost cash flow, is exactly
    this POC's own `(claim_year - base_year) * 4 + (claim_quarter - 1)` --
    confirmed against `MappingMeasurevw`'s real formula (`CET_v25.1_
    documentation_extracted.txt:10969`, hardcoded to a literal "2026" that
    matches this project's real AVCVersion 2026, i.e. this POC's own
    base_year for both ground-truth jobs). Discounted with the QUARTERLY
    `Rqf`, unlike price_bill_impact's annual `Raf` -- incentive/rebate/
    measure-cost cash flows are one-time-at-claim, so there's no dual-
    baseline year-loop here at all, just a single PV division.

    DUAL-BASELINE (RUL>0) measure cost: `GrossMeasCost` counts the FULL
    1st-baseline measure cost (`UnitMeaCost1stBaseline`) when RUL_Yrs==0, but
    only the 2nd-baseline cost (`UnitMeaCost2ndBaseline`) when RUL_Yrs>0 (the
    real SQL's `UnitMeasureGrossCost - (UnitMeasureGrossCost -
    MeasIncrCost)` reduces to exactly `MeasIncrCost` when RUL>0) -- i.e.
    an early-replacement measure's cost-effectiveness only counts the
    normal replacement cost, not the accelerated-replacement premium.
    `GrossMeasCostPV` additionally inflation/discount-adjusts that
    RUL-conditional subtraction by `((1+MeasInflation/4)/Rqf) ^ (RUL*4)`
    before taking it out of the PV total -- implemented literally, not
    re-derived, since `MeasInflation` is 0 for every measure in this
    POC's ground-truth data and this term hasn't been validated non-trivially.

    MARKET EFFECTS: `market_effects_cost` (CalcCE/CalcCost's `@MECost`).
    Validated 2026-10-08 on CET jobs 121842 (0%) / 121841 (5%): every
    participant column here matches CET to the cent at both settings
    (poc/compare_market_effects_to_cet.py). NetParticipantCostPV excludes
    the market-effects term and NetParticipantCostAdjustedPV includes it,
    as CET exports them; the TRC / SCT costs use the adjusted one.

    Every measure in this POC's ground-truth SWWH025 filing has
    `UnitIncentiveToOthers`/`UnitEndUserRebate`/`UnitDirectInstallLab`/
    `UnitDirectInstallMat` genuinely blank (a real no-incentive-program
    filing) and `NTGRCost==1.0` exactly, so `RebatesandIncents`/
    `ExcessIncentives` collapse to 0 and `NetParticipantCost[Adjusted]`
    collapses to exactly `GrossParticipantCost[Adjusted]` for every
    measure tested -- the incentive/rebate/excess-incentive/NTGR-scaling
    branches below are implemented per the real SQL but UNVALIDATED
    against a filing that actually exercises them.
    """
    rows: list[dict] = []

    for _, m in measures.iterrows():
        NumUnits = float(m["NumUnits"])
        quarterly_discount_factor = 1.0 + get_discount_rate_qtr(m)                         # CalcCE's Rqf
        quarters_from_first_year = (int(m["Used_Year"]) - base_year) * 4 + (int(m["Used_Quarter"]) - 1)    # CalcCE's Qm
        participant_discount_factor = quarterly_discount_factor ** quarters_from_first_year  # Rqf ^ Qm: brings a claim-quarter amount back to the First Year

        IncentiveToOthers = NumUnits * float(m["UnitIncentiveToOthers"])
        DILaborCost = NumUnits * float(m["UnitDirectInstallLab"])
        DIMaterialCost = NumUnits * float(m["UnitDirectInstallMat"])
        EndUserRebate = NumUnits * float(m["UnitEndUserRebate"])
        RebatesandIncents = IncentiveToOthers + DILaborCost + DIMaterialCost + EndUserRebate
        RebatesandIncentsPV = RebatesandIncents / participant_discount_factor

        incentives_and_di = IncentiveToOthers + DILaborCost + DIMaterialCost          # the incentives other than end-user rebates
        incentives_and_di_pv = incentives_and_di / participant_discount_factor
        EndUserRebatePV = EndUserRebate / participant_discount_factor

        UnitMeaCost1stBaseline = float(m["UnitMeaCost1stBaseline"])
        UnitMeaCost2ndBaseline = float(m["UnitMeaCost2ndBaseline"])
        RUL_Yrs = float(rul_in_use(m["RUL_Yrs"], m["EUL_Yrs"]))
        GrossMeasureCost = NumUnits * (UnitMeaCost2ndBaseline if RUL_Yrs > 0 else UnitMeaCost1stBaseline)
        if RUL_Yrs > 0:
            # the 1st-minus-2nd-baseline cost difference carried to the end of the RUL (at MeasInflation) and discounted back to the claim quarter
            inflation_discount_over_rul = ((1.0 + float(m["MeasInflation"]) / 4.0) / quarterly_discount_factor) ** (RUL_Yrs * 4.0)
            deferred_cost_difference_pv = (UnitMeaCost1stBaseline - UnitMeaCost2ndBaseline) * inflation_discount_over_rul
        else:
            deferred_cost_difference_pv = 0.0
        GrossMeasCostPV = NumUnits * (UnitMeaCost1stBaseline - deferred_cost_difference_pv) / participant_discount_factor

        ExcessIncentives = max(incentives_and_di - GrossMeasureCost, 0.0) if incentives_and_di > GrossMeasureCost else 0.0
        ExcessIncentivesPV = max(incentives_and_di_pv - GrossMeasCostPV, 0.0) if incentives_and_di_pv > GrossMeasCostPV else 0.0

        MarkEffectPlusExcessInc = market_effects_cost * (GrossMeasureCost + ExcessIncentivesPV)
        MarkEffectPlusExcessIncPV = market_effects_cost * (GrossMeasCostPV + ExcessIncentivesPV)

        GrossParticipantCost = GrossMeasureCost - (incentives_and_di + EndUserRebate)
        GrossParticipantCostPV = GrossMeasCostPV - (incentives_and_di_pv + EndUserRebatePV)
        GrossParticipantCostAdjusted = GrossParticipantCost + ExcessIncentives + MarkEffectPlusExcessInc
        GrossParticipantCostAdjustedPV = GrossParticipantCostPV + ExcessIncentivesPV + MarkEffectPlusExcessIncPV

        NTGRCost = float(m["NTGRCost"])
        NetParticipantCost = NTGRCost * (GrossParticipantCost + ExcessIncentives)
        NetParticipantCostPV = NTGRCost * (GrossParticipantCostPV + ExcessIncentivesPV)
        NetParticipantCostAdjusted = NetParticipantCost + MarkEffectPlusExcessInc
        NetParticipantCostAdjustedPV = NetParticipantCostPV + MarkEffectPlusExcessIncPV

        rows.append({
            "CEInputID": m["CEInputID"],
            "IncentiveToOthers": IncentiveToOthers,
            "DILaborCost": DILaborCost,
            "DIMaterialCost": DIMaterialCost,
            "EndUserRebate": EndUserRebate,
            "RebatesandIncents": RebatesandIncents,
            "RebatesandIncentsPV": RebatesandIncentsPV,
            "GrossMeasureCost": GrossMeasureCost,
            "GrossMeasCostPV": GrossMeasCostPV,
            "ExcessIncentives": ExcessIncentives,
            "ExcessIncentivesPV": ExcessIncentivesPV,
            "MarkEffectPlusExcessInc": MarkEffectPlusExcessInc,
            "MarkEffectPlusExcessIncPV": MarkEffectPlusExcessIncPV,
            "GrossParticipantCost": GrossParticipantCost,
            "GrossParticipantCostPV": GrossParticipantCostPV,
            "GrossParticipantCostAdjusted": GrossParticipantCostAdjusted,
            "GrossParticipantCostAdjustedPV": GrossParticipantCostAdjustedPV,
            "NetParticipantCost": NetParticipantCost,
            "NetParticipantCostPV": NetParticipantCostPV,
            "NetParticipantCostAdjusted": NetParticipantCostAdjusted,
            "NetParticipantCostAdjustedPV": NetParticipantCostAdjustedPV,
        })

    return pd.DataFrame(rows)


def price_other_ben_cost_per_measure(
    measures: pd.DataFrame,
    market_effects_bens: float = 0.0,
    market_effects_cost: float = 0.0,
) -> pd.DataFrame:
    """
    Real `OtherBen`/`OtherCost` (the refrigerant/gas-infrastructure/misc
    catch-all benefit and cost lines feeding TRCCost/PACCost and
    TotalSystemBenefit), traced from `CalcCE`
    (CET_v25.1_documentation_extracted.txt:13681, :14179-14187):
    `OtherBen = Qty * (NTGRkWh + MEBens) * (UnitGasInfraBens + UnitRefrigBens
    + UnitMiscBens)`, `OtherCost = Qty * (NTGRkWh + MECost) * (UnitRefrigCosts
    + UnitMiscCosts)` -- note OtherCost's formula genuinely omits
    UnitGasInfraBens (a benefit-only field) and uses `MECost`, not `MEBens`,
    matching the real SQL's own asymmetry, not a typo introduced here.

    UNDISCOUNTED, unlike every other cost/benefit stream in this file --
    confirmed against SWWH025/Job-121074's own real OtherCost total
    ($12,987,500.00 = 50,000 measures x $259.75 UnitRefrigCosts x 1.0
    NTGRkWh exactly, no PV division anywhere in the real formula).

    Also computes `TaxCredits` (`Qty * (NTGRkWh + MEBens) * UnitTaxCredits`,
    CET_v25.1_documentation_extracted.txt:13598-13600) -- same
    undiscounted shape, folded in here rather than its own function since
    it's 0 for every measure in this POC's ground-truth data (blank
    `UnitTaxCredits`) and TRCRatio is its only real consumer
    (`TRCRatio = (ElecBen+GasBen+WaterEnergyBen+OtherBen+TaxCredits) /
    TRCCost` -- note TaxCredits is NOT added to TRCCost/TRC_Cost itself,
    only to its ratio's numerator).
    """
    rows: list[dict] = []
    for _, m in measures.iterrows():
        NumUnits = float(m["NumUnits"])
        NTGRkWh = float(m["NTGRkWh"])
        unit_other_bens = float(m["UnitGasInfraBens"]) + float(m["UnitRefrigBens"]) + float(m["UnitMiscBens"])
        unit_other_costs = float(m["UnitRefrigCosts"]) + float(m["UnitMiscCosts"])
        rows.append({
            "CEInputID": m["CEInputID"],
            "OtherBen": NumUnits * (NTGRkWh + market_effects_bens) * unit_other_bens,
            "OtherCost": NumUnits * (NTGRkWh + market_effects_cost) * unit_other_costs,
            "TaxCredits": NumUnits * (NTGRkWh + market_effects_bens) * float(m["UnitTaxCredits"]),
            # Gross = same formulas with no NTG / market-effects factor (CalcCE lines 13862, 13936, 14417)
            "OtherBenGross": NumUnits * unit_other_bens,
            "OtherCostGross": NumUnits * unit_other_costs,
            "TaxCreditsGross": NumUnits * float(m["UnitTaxCredits"]),
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------------------------------------------------------------------------
# Array versions: the arithmetic of the *_per_measure functions above (read their docstrings for the CalcCE mechanics), on whole columns at once. A loop of a
# few hundred thousand rows through DataFrame.iterrows is what made these three the slowest steps of a large run (about 55% of the pricing time at 100,000
# records); here only the bill impact loops, over the years of a life (at most a few dozen), not over measures.

def _column_or_default(m: pd.DataFrame, name: str, default: float = 0.0) -> np.ndarray:
    """A column as a float array, or `default` for every row when the frame has no such column."""
    return m[name].astype(float).to_numpy() if name in m.columns else np.full(len(m), default)


def _by_key(m: pd.DataFrame, cols: list[str], fn) -> list:
    """fn(row) evaluated once per distinct value of `cols` (the columns the callback reads) and spread back to every row; the first row of each group is
    passed. With none of `cols` present the whole frame is one group."""
    present = [c for c in cols if c in m.columns]
    if not present:
        v = fn(m.iloc[0]) if len(m) else None
        return [v] * len(m)
    keys = list(zip(*[m[c].to_numpy() for c in present]))
    first: dict = {}
    for i, k in enumerate(keys):
        first.setdefault(k, i)
    vals = {k: fn(m.iloc[i]) for k, i in first.items()}
    return [vals[k] for k in keys]


def price_bill_impact(
    measures: pd.DataFrame,
    get_elec_rate_years: Callable[[pd.Series], dict[int, float] | None],
    get_gas_rate_years: Callable[[pd.Series], dict[int, float] | None],
    get_discount_rate_annual: Callable[[pd.Series], float],
    run_parameters_base_year: int = 2024,
) -> pd.DataFrame:
    """price_bill_impact_per_measure on arrays. The callbacks are called once per distinct (IOU_AC_Territory, AC_Elec_Target_Sector), (IOU_AC_Territory,
    AC_Gas_Target_Sector) and IOU_AC_Territory (the fields they read in run_poc.run_bill_impact). Years with no rate count 0, a fuel with no rate table gives 0,
    exactly as in the reference."""
    m = measures
    n_rows = len(m)
    if n_rows == 0:
        return pd.DataFrame(columns=["CEInputID", "LifecycleBillReducElec", "LifecycleBillIncrElec", "LifecycleBillReducGas", "LifecycleBillIncrGas"])
    claim_year = m["Used_Year"].astype(int).to_numpy()
    years_from_base_year = claim_year - run_parameters_base_year                                        # CalcCE's Yr1
    annual_discount_factor = 1.0 + np.asarray(_by_key(m, ["IOU_AC_Territory"], get_discount_rate_annual), dtype=float)   # CalcCE's Raf
    EUL_Yrs = _column_or_default(m, "EUL_Yrs")
    RUL_Yrs = rul_in_use(_column_or_default(m, "RUL_Yrs"), EUL_Yrs)
    whole_years = np.floor(EUL_Yrs).astype(int)
    last_year_fraction = EUL_Yrs - whole_years
    n_billed_years = whole_years + (last_year_fraction > 1e-9).astype(int)
    years_on_1st_baseline = np.where(RUL_Yrs > 0, np.floor(RUL_Yrs), whole_years).astype(int)
    boundary_year_share_1st = np.where(RUL_Yrs > 0, RUL_Yrs - years_on_1st_baseline, 1.0)
    max_billed_years = int(n_billed_years.max()) if n_rows else 0

    def rate_matrix(tables: list) -> tuple[np.ndarray, np.ndarray]:
        """(rates[i, k] for the year claim_year[i] + k, 0 where missing; has_table[i])"""
        r = np.zeros((n_rows, max(max_billed_years, 1)))
        has = np.array([t is not None for t in tables])
        cache: dict = {}
        for i, (t, y0) in enumerate(zip(tables, claim_year)):
            if t is None:
                continue
            key = (id(t), y0)
            if key not in cache:
                cache[key] = np.array([t.get(int(y0) + k, 0.0) or 0.0 for k in range(r.shape[1])])
            r[i] = cache[key]
        return r, has

    def bill_pv(quantity_1st: np.ndarray, quantity_2nd: np.ndarray, retail_rates: np.ndarray) -> np.ndarray:
        total = np.zeros(n_rows)
        for k in range(max_billed_years):
            rate = retail_rates[:, k]
            in_1st_baseline_years = k < np.minimum(years_on_1st_baseline, n_billed_years)                 # RE1: discounted by k
            total += np.where(in_1st_baseline_years, quantity_1st * rate / annual_discount_factor ** k, 0.0)
            in_boundary_or_2nd_years = (k >= years_on_1st_baseline) & (k < n_billed_years)                 # boundary year and RE2: by k + Yr1 (CalcCE's Qy)
            year_weight = np.where((k == n_billed_years - 1) & (last_year_fraction > 1e-9), last_year_fraction, 1.0)
            quantity_this_year = np.where(k == years_on_1st_baseline,
                                          boundary_year_share_1st * quantity_1st + (1.0 - boundary_year_share_1st) * quantity_2nd, quantity_2nd)
            total += np.where(in_boundary_or_2nd_years,
                              year_weight * quantity_this_year * rate / annual_discount_factor ** (k + years_from_base_year), 0.0)
        return total

    elec_rates, has_elec_rates = rate_matrix(_by_key(m, ["IOU_AC_Territory", "AC_Elec_Target_Sector"], get_elec_rate_years))
    gas_rates, has_gas_rates = rate_matrix(_by_key(m, ["IOU_AC_Territory", "AC_Gas_Target_Sector"], get_gas_rate_years))
    elec_bill_pv = np.where(has_elec_rates, bill_pv(_column_or_default(m, "Billed_kWh_1st_Baseline"), _column_or_default(m, "Billed_kWh_2nd_Baseline"),
                                                    elec_rates), 0.0)
    gas_bill_pv = np.where(has_gas_rates, bill_pv(_column_or_default(m, "Billed_Therms_1st_Baseline"), _column_or_default(m, "Billed_Therms_2nd_Baseline"),
                                                  gas_rates), 0.0)
    Fuel_Sub_Flag = (m["Fuel_Sub_Flag"].fillna(True).astype(bool).to_numpy() if "Fuel_Sub_Flag" in m.columns else np.ones(n_rows, dtype=bool))
    return pd.DataFrame({
        "CEInputID": m["CEInputID"].to_numpy(),
        "LifecycleBillReducElec": np.where(Fuel_Sub_Flag, np.maximum(elec_bill_pv, 0.0), elec_bill_pv),
        "LifecycleBillIncrElec": np.where(Fuel_Sub_Flag, np.maximum(-elec_bill_pv, 0.0), 0.0),
        "LifecycleBillReducGas": np.where(Fuel_Sub_Flag, np.maximum(gas_bill_pv, 0.0), gas_bill_pv),
        "LifecycleBillIncrGas": np.where(Fuel_Sub_Flag, np.maximum(-gas_bill_pv, 0.0), 0.0),
    })


def price_participant_cost(
    measures: pd.DataFrame,
    get_discount_rate_qtr: Callable[[pd.Series], float],
    base_year: int,
    market_effects_cost: float = 0.0,
) -> pd.DataFrame:
    """price_participant_cost_per_measure on arrays; the discount-rate callback is called once per PA."""
    m = measures
    if m.empty:
        return price_participant_cost_per_measure(m, get_discount_rate_qtr, base_year, market_effects_cost)
    NumUnits = _column_or_default(m, "NumUnits")
    quarterly_discount_factor = 1.0 + np.asarray(_by_key(m, ["IOU_AC_Territory"], get_discount_rate_qtr), dtype=float)          # CalcCE's Rqf
    quarters_from_first_year = (m["Used_Year"].astype(int).to_numpy() - base_year) * 4 + (m["Used_Quarter"].astype(int).to_numpy() - 1)   # CalcCE's Qm
    participant_discount_factor = quarterly_discount_factor ** quarters_from_first_year                  # Rqf ^ Qm
    IncentiveToOthers, DILaborCost, DIMaterialCost, EndUserRebate = (
        NumUnits * _column_or_default(m, c) for c in ("UnitIncentiveToOthers", "UnitDirectInstallLab", "UnitDirectInstallMat", "UnitEndUserRebate"))
    incentives_and_di = IncentiveToOthers + DILaborCost + DIMaterialCost                                 # the incentives other than end-user rebates
    UnitMeaCost1stBaseline, UnitMeaCost2ndBaseline = _column_or_default(m, "UnitMeaCost1stBaseline"), _column_or_default(m, "UnitMeaCost2ndBaseline")
    RUL_Yrs = rul_in_use(_column_or_default(m, "RUL_Yrs"), _column_or_default(m, "EUL_Yrs"))
    GrossMeasureCost = NumUnits * np.where(RUL_Yrs > 0, UnitMeaCost2ndBaseline, UnitMeaCost1stBaseline)
    # early replacement: the 1st-minus-2nd-baseline cost difference carried to the end of the RUL (at MeasInflation) and discounted back to the claim quarter
    deferred_cost_difference_pv = np.where(RUL_Yrs > 0, (UnitMeaCost1stBaseline - UnitMeaCost2ndBaseline)
                                           * ((1.0 + _column_or_default(m, "MeasInflation") / 4.0) / quarterly_discount_factor) ** (RUL_Yrs * 4.0), 0.0)
    GrossMeasCostPV = NumUnits * (UnitMeaCost1stBaseline - deferred_cost_difference_pv) / participant_discount_factor
    ExcessIncentives = np.maximum(incentives_and_di - GrossMeasureCost, 0.0)
    ExcessIncentivesPV = np.maximum(incentives_and_di / participant_discount_factor - GrossMeasCostPV, 0.0)
    MarkEffectPlusExcessInc = market_effects_cost * (GrossMeasureCost + ExcessIncentivesPV)
    MarkEffectPlusExcessIncPV = market_effects_cost * (GrossMeasCostPV + ExcessIncentivesPV)
    RebatesandIncents = incentives_and_di + EndUserRebate
    GrossParticipantCost = GrossMeasureCost - RebatesandIncents
    GrossParticipantCostPV = GrossMeasCostPV - RebatesandIncents / participant_discount_factor
    NTGRCost = _column_or_default(m, "NTGRCost")
    NetParticipantCost, NetParticipantCostPV = NTGRCost * (GrossParticipantCost + ExcessIncentives), NTGRCost * (GrossParticipantCostPV + ExcessIncentivesPV)
    return pd.DataFrame({
        "CEInputID": m["CEInputID"].to_numpy(),
        "IncentiveToOthers": IncentiveToOthers, "DILaborCost": DILaborCost, "DIMaterialCost": DIMaterialCost, "EndUserRebate": EndUserRebate,
        "RebatesandIncents": RebatesandIncents, "RebatesandIncentsPV": RebatesandIncents / participant_discount_factor,
        "GrossMeasureCost": GrossMeasureCost, "GrossMeasCostPV": GrossMeasCostPV,
        "ExcessIncentives": ExcessIncentives, "ExcessIncentivesPV": ExcessIncentivesPV,
        "MarkEffectPlusExcessInc": MarkEffectPlusExcessInc, "MarkEffectPlusExcessIncPV": MarkEffectPlusExcessIncPV,
        "GrossParticipantCost": GrossParticipantCost, "GrossParticipantCostPV": GrossParticipantCostPV,
        "GrossParticipantCostAdjusted": GrossParticipantCost + ExcessIncentives + MarkEffectPlusExcessInc,
        "GrossParticipantCostAdjustedPV": GrossParticipantCostPV + ExcessIncentivesPV + MarkEffectPlusExcessIncPV,
        "NetParticipantCost": NetParticipantCost, "NetParticipantCostPV": NetParticipantCostPV,
        "NetParticipantCostAdjusted": NetParticipantCost + MarkEffectPlusExcessInc, "NetParticipantCostAdjustedPV": NetParticipantCostPV + MarkEffectPlusExcessIncPV,
    })


def price_other_ben_cost(
    measures: pd.DataFrame,
    market_effects_bens: float = 0.0,
    market_effects_cost: float = 0.0,
) -> pd.DataFrame:
    """price_other_ben_cost_per_measure on arrays."""
    m = measures
    NumUnits, NTGRkWh = _column_or_default(m, "NumUnits"), _column_or_default(m, "NTGRkWh")
    unit_other_bens = _column_or_default(m, "UnitGasInfraBens") + _column_or_default(m, "UnitRefrigBens") + _column_or_default(m, "UnitMiscBens")
    unit_other_costs = _column_or_default(m, "UnitRefrigCosts") + _column_or_default(m, "UnitMiscCosts")
    UnitTaxCredits = _column_or_default(m, "UnitTaxCredits")
    return pd.DataFrame({
        "CEInputID": m["CEInputID"].to_numpy(),
        "OtherBen": NumUnits * (NTGRkWh + market_effects_bens) * unit_other_bens,
        "OtherCost": NumUnits * (NTGRkWh + market_effects_cost) * unit_other_costs,
        "TaxCredits": NumUnits * (NTGRkWh + market_effects_bens) * UnitTaxCredits,
        "OtherBenGross": NumUnits * unit_other_bens,
        "OtherCostGross": NumUnits * unit_other_costs,
        "TaxCreditsGross": NumUnits * UnitTaxCredits,
    })
