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

import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

HOURS_PER_YEAR = 8760

@dataclass
class BatchedPricingResult:
    # price_real_hourly: one row per priced measure, its lifetime discounted value in annual_value.
    # price_real_hourly_per_measure: one row per priced (measure_id, year, quarter), the quarter's discounted value in annual_value.
    measure_year: pd.DataFrame
    portfolio_hourly: np.ndarray        # length HOURS_PER_YEAR, this run's aggregate
    n_batches: int
    elapsed_seconds: float
    batch_size: int
    # Only populated by price_real_hourly() -- how many measures got a real
    # CEDARS impact-profile shape vs. fell back to a synthetic placeholder
    # (n_fallback_shape is 0 whenever the caller passes fallback_shape=None,
    # i.e. synthetic substitution is disabled -- see run_poc.py's
    # 2026-09-16 change: unmatched measures are now flagged, not faked).
    n_real_shape: int = 0
    n_fallback_shape: int = 0
    unmatched_measure_ids: list = field(default_factory=list)  # populated by price_real_hourly() when fallback_shape is None
    # measure_id -> how run_poc.run_real_hourly routed it ("hourly profile | <profile source>"); reporting only.
    route: dict = field(default_factory=dict)


# Real 2018 (non-leap) calendar quarter boundaries, in 0-based hour-of-year --
# same convention CEDARS QAC Validation/pipeline.py uses (90/91/92/92 days,
# NOT a flat 2,190-hour split). Real CalcCE prices and discounts at THIS
# granularity (quarterly), not annually -- see price_real_hourly's docstring.
QUARTER_BOUNDS = [(0, 2160), (2160, 4344), (4344, 6552), (6552, 8760)]


def _phased_scale(k: int, rul_quarters: float, scale_1st: float, scale_2nd: float) -> float:
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
        return scale_1st
    if k >= rul_quarters:
        return scale_2nd
    frac_1st = rul_quarters - k
    return frac_1st * scale_1st + (1.0 - frac_1st) * scale_2nd


def price_real_hourly_per_measure(
    measures: pd.DataFrame,
    get_shape: Callable[[pd.Series], tuple[np.ndarray, int] | None],
    get_acc_years: Callable[[pd.Series], dict[int, np.ndarray]],
    shift_fn: Callable[[np.ndarray, int, int], np.ndarray],
    get_discount_rate_qtr: Callable[[pd.Series], float],
    base_year: int,
    fallback_shape: Callable[[pd.Series], np.ndarray] | None = None,
) -> BatchedPricingResult:
    """
    THE PER-MEASURE VERSION: the reference for `price_real_hourly` below (which gives the same values, organised by distinct route so that the shift-and-sum work is done
    once per route instead of once per measure) and the only one that can also fill the portfolio 8,760 accumulator or use a fallback shape. Tests require the two to agree.

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
    None if no real CEDARS profile could be matched -- fallback_shape is
    used instead (represents_year defaults to the measure's own claim
    year, i.e. "assume no shift needed", since a synthetic shape has no
    real represents_year to shift from).

    fallback_shape=None (2026-09-16, Jake's direction): a measure with no
    real 8760-hour profile match is FLAGGED, not silently priced against a
    synthetic placeholder -- its measure_id is collected into the returned
    result's `unmatched_measure_ids` and it's excluded from pricing
    entirely (no row emitted, same as before this list existed -- the
    `else: continue` branch already did this, it just didn't record WHICH
    measures got skipped). Pass a real fallback_shape callable only for a
    deliberately-synthetic measure that isn't meant to represent real
    CEDARS data at all (e.g. run_poc.py's own demo load-shifting measure),
    not as a general substitute for an unmatched real measure.

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
    start = time.perf_counter()
    portfolio_hourly = np.zeros(HOURS_PER_YEAR)
    rows: list[dict] = []
    n_real_shape = 0
    n_fallback_shape = 0
    unmatched_measure_ids: list = []

    for _, m in measures.iterrows():
        shape_result = get_shape(m)
        if shape_result is not None:
            shape, represents_year = shape_result
            n_real_shape += 1
        elif fallback_shape is not None:
            shape, represents_year = fallback_shape(m), int(m["claim_year"])
            n_fallback_shape += 1
        else:
            unmatched_measure_ids.append(m["measure_id"])
            continue

        acc_years = get_acc_years(m)
        available_years = sorted(acc_years)
        claim_year = int(m["claim_year"])
        claim_quarter = int(m["claim_quarter"])  # 1-4, real ClaimYearQuarter field
        rqf = 1.0 + get_discount_rate_qtr(m)

        eul_quarters = float(m["eul_years_exact"]) * 4.0
        full_quarters = int(eul_quarters)
        frac_remainder = eul_quarters - full_quarters  # prorates the final quarter, per real CalcCE
        n_billed_quarters = full_quarters + (1 if frac_remainder > 1e-9 else 0)
        rul_quarters = float(m["rul_years_exact"]) * 4.0
        scale_1st = float(m["shape_scale"])
        scale_2nd = float(m["shape_scale_2nd"])

        # shape*acc_year is cached per calendar year (unscaled) -- the
        # RUL-phased scale is applied per-quarter below, since two quarters
        # in the SAME calendar year can straddle the RUL boundary and need
        # different scales even though they share one hourly array.
        year_hourly_cache: dict[int, np.ndarray] = {}
        for k in range(n_billed_quarters):
            abs_quarter = (claim_quarter - 1) + k  # 0-based quarters elapsed since claim_year's own Q1
            target_year = claim_year + abs_quarter // 4
            quarter_idx = abs_quarter % 4  # 0=Q1 .. 3=Q4

            if target_year not in year_hourly_cache:
                acc_year = min(available_years, key=lambda y: abs(y - target_year))
                shifted = shift_fn(shape, target_year, represents_year)
                year_hourly_cache[target_year] = shifted * acc_years[acc_year]
            scale = _phased_scale(k, rul_quarters, scale_1st, scale_2nd)
            hourly_value = year_hourly_cache[target_year] * scale

            q_start, q_end = QUARTER_BOUNDS[quarter_idx]
            is_last = k == n_billed_quarters - 1
            frac = frac_remainder if (is_last and frac_remainder > 1e-9) else 1.0

            quarter_value = float(hourly_value[q_start:q_end].sum()) * frac
            qac = (target_year - base_year) * 4 + quarter_idx  # quarters since BaseYear's own Q1
            discount_factor = 1.0 / (rqf ** qac)

            rows.append({
                "measure_id": m["measure_id"],
                "year": target_year,
                "quarter": quarter_idx + 1,
                "annual_value": quarter_value * discount_factor,
            })
            portfolio_hourly[q_start:q_end] += hourly_value[q_start:q_end] * frac

    elapsed = time.perf_counter() - start
    return BatchedPricingResult(
        measure_year=pd.DataFrame(rows),
        portfolio_hourly=portfolio_hourly,
        n_batches=1,
        elapsed_seconds=elapsed,
        batch_size=len(measures),
        n_real_shape=n_real_shape,
        n_fallback_shape=n_fallback_shape,
        unmatched_measure_ids=unmatched_measure_ids,
    )


def _quarter_rows(claim_year: np.ndarray, claim_quarter: np.ndarray, eul_years: np.ndarray, rul_years: np.ndarray, scale_1st: np.ndarray, scale_2nd: np.ndarray,
                  rqf: np.ndarray, base_year: int):
    """Expand measures (one entry per measure in every input array) into one row per (measure, quarter of its life), with everything CalcCE applies per quarter:
    returns (measure index, calendar year, quarter 0-3, weight, phased scale, discount factor).
      weight  = 1, or the fraction of the last quarter of a fractional life (`(eulq - floor(eulq))`);
      scale   = `_phased_scale`: the 1st-baseline quantity before the RUL, the 2nd after, a blend in the quarter that contains it;
      discount = 1 / Rqf ^ Qac, Qac = calendar quarters from Q1 of the First Year.
    The same arithmetic as the per-measure loops, evaluated on flat arrays."""
    eq = eul_years * 4.0                                                                  # EUL in quarters
    full = eq.astype(np.int64)
    rem = eq - full                                                                       # the last, partial quarter
    n_q = full + (rem > 1e-9)
    rul_q = rul_years * 4.0
    total = int(n_q.sum())
    m_idx = np.repeat(np.arange(len(n_q)), n_q)
    k = np.arange(total) - np.repeat(np.cumsum(n_q) - n_q, n_q)
    a = (claim_quarter[m_idx] - 1) + k                                                     # 0-based quarters elapsed since the claim year's Q1
    year = claim_year[m_idx] + a // 4
    quarter = a % 4                                                                        # 0 = Q1 .. 3 = Q4
    weight = np.where((k == n_q[m_idx] - 1) & (rem[m_idx] > 1e-9), rem[m_idx], 1.0)
    rq = rul_q[m_idx]
    blend = rq - k
    scale = np.where((rq <= 0) | (k + 1 <= rq), scale_1st[m_idx], np.where(k >= rq, scale_2nd[m_idx], blend * scale_1st[m_idx] + (1.0 - blend) * scale_2nd[m_idx]))
    discount = 1.0 / (rqf[m_idx] ** ((year - base_year) * 4 + quarter))
    return m_idx, year, quarter, weight, scale, discount


def _lifetime_values(g_m: np.ndarray, claim_year: np.ndarray, claim_quarter: np.ndarray, eul_years: np.ndarray, rul_years: np.ndarray,
                     scale_1st: np.ndarray, scale_2nd: np.ndarray, rqf_group: np.ndarray, base_year: int,
                     fill_unit: Callable[[int, list[int]], np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Lifetime discounted value of each measure (one entry per measure in every input array; g_m = its route), without a row per measure-quarter.

    The per-measure value is  SUM_k scale(k) * weight(k) * discount(k) * U(route, year_k, quarter_k)  (`_quarter_rows`), and scale(k) is the 1st-baseline quantity
    times f(k) plus the 2nd times 1 - f(k), with f(k) = 1 before the RUL, 0 after and the blend in the quarter that holds it. So
        value = scale_1st * A + scale_2nd * B,   A = SUM_k f(k) * weight(k) * discount(k) * U,   B = SUM_k (1 - f(k)) * weight(k) * discount(k) * U
    where A and B depend only on (route, claim year, claim quarter, EUL, RUL) -- the route fixes the discount rate. Every quarter is still discounted on its own,
    exactly as before; A and B are computed once per distinct key instead of once per measure.

    fill_unit(g, years) -> array (len(years), 4): U for route g in each listed year's four quarters.
    Returns (value per measure, has_life per measure: False where the EUL gives no quarter at all, which the per-measure versions leave without a row)."""
    keys = np.column_stack([g_m, claim_year, claim_quarter, eul_years, rul_years]).astype(np.float64)
    uniq, inverse = np.unique(keys, axis=0, return_inverse=True)
    inverse = inverse.ravel()
    n_keys = len(uniq)
    k_g = uniq[:, 0].astype(np.int64)
    k_idx, year, quarter, weight, f1, discount = _quarter_rows(
        uniq[:, 1].astype(np.int64), uniq[:, 2].astype(np.int64), uniq[:, 3], uniq[:, 4], np.ones(n_keys), np.zeros(n_keys), rqf_group[k_g], base_year)
    has_life = np.bincount(k_idx, minlength=n_keys) > 0
    if not len(k_idx):
        return np.zeros(len(g_m)), has_life[inverse]

    # --- U(route, year, quarter), once per route and year
    y0, y1 = int(year.min()), int(year.max())
    unit = np.zeros((int(k_g.max()) + 1, y1 - y0 + 1, 4))
    g_row = k_g[k_idx]
    order = np.argsort(g_row, kind="stable")
    for rows in np.split(order, np.flatnonzero(np.diff(g_row[order])) + 1):
        g = int(g_row[rows[0]])
        years = [int(y) for y in np.unique(year[rows])]
        unit[g, [y - y0 for y in years]] = fill_unit(g, years)

    wdu = weight * discount * unit[g_row, year - y0, quarter]
    a = np.bincount(k_idx, weights=f1 * wdu, minlength=n_keys)
    b = np.bincount(k_idx, weights=(1.0 - f1) * wdu, minlength=n_keys)
    return scale_1st * a[inverse] + scale_2nd * b[inverse], has_life[inverse]


def price_real_hourly(
    measures: pd.DataFrame,
    get_shape: Callable[[pd.Series], tuple[np.ndarray, int] | None],
    get_acc_years: Callable[[pd.Series], dict[int, np.ndarray]],
    shift_fn: Callable[[np.ndarray, int, int], np.ndarray],
    get_discount_rate_qtr: Callable[[pd.Series], float],
    base_year: int,
    fallback_shape: Callable[[pd.Series], np.ndarray] | None = None,
    route_key=None,
    need_portfolio_hourly: bool = False,
) -> BatchedPricingResult:
    """The same pricing as `price_real_hourly_per_measure` (read its docstring for the mechanics and the lessons behind each step), organised so that work is done once
    per DISTINCT SITUATION instead of once per measure.

    A measure's lifetime value is  SUM over its quarters k of  phased_scale(k) * weight(k) * discount(k) * U,  where
        U = SUM over the hours of quarter(k) of  (calendar-aligned shape)(h) * (avoided cost of the nearest ACC year)(h)
    depends only on the measure's shape, its avoided-cost series (IOU and zone), the pricing year and the quarter -- never on how many kWh the measure saves. Real jobs
    have few of those: 169 distinct (shape, IOU, sector, zone) routes in a 5,518-measure job, 48 in a 73,024-measure filing. So U is computed once per (route, year,
    quarter), and the sum over quarters once per distinct (route, claim year, claim quarter, EUL, RUL) -- `_lifetime_values` -- with no row per measure-quarter.

    route_key: one hashable per measure (aligned with `measures`) naming its route; `get_shape`, `get_acc_years` and `get_discount_rate_qtr` are called once per route, on
    that route's first measure, and must give the same answer for every measure of it. None = every measure is its own route. The result has one row per priced
    measure (measure_id, annual_value = its lifetime discounted value): the per-measure version's (measure_id, year, quarter, annual_value) rows summed by measure, to
    rounding (the multiplications are ordered differently).

    need_portfolio_hourly / fallback_shape: the portfolio 8,760 accumulator and synthetic fallbacks need each measure's hourly array, so they run the per-measure
    version."""
    if need_portfolio_hourly or fallback_shape is not None:
        return price_real_hourly_per_measure(measures, get_shape, get_acc_years, shift_fn, get_discount_rate_qtr, base_year, fallback_shape)
    start = time.perf_counter()
    n = len(measures)
    ids = measures["measure_id"].to_numpy()
    if route_key is None:
        codes = np.arange(n)
    else:
        codes, _uniques = pd.factorize(pd.Series(list(route_key), dtype=object))
    n_groups = int(codes.max()) + 1 if n else 0
    _, first_pos = np.unique(codes, return_index=True) if n else (None, np.array([], dtype=int))

    # --- one shape / ACC series / rate per route, read off the route's first measure (one row lookup for all of them, not one per route)
    shapes: dict[int, tuple[np.ndarray, int]] = {}
    acc: dict[int, dict[int, np.ndarray]] = {}
    rqf_group = np.ones(n_groups)
    for g, rep in enumerate(measures.iloc[first_pos].to_dict("records") if n else []):
        hit = get_shape(rep)
        if hit is None:
            continue
        shapes[g] = hit
        acc[g] = get_acc_years(rep)
        rqf_group[g] = 1.0 + get_discount_rate_qtr(rep)
    matched_group = np.zeros(max(n_groups, 1), dtype=bool)
    matched_group[list(shapes)] = True
    matched = matched_group[codes] if n else np.zeros(0, dtype=bool)
    unmatched_ids = ids[~matched].tolist()

    empty = pd.DataFrame({"measure_id": pd.Series(dtype=object), "annual_value": pd.Series(dtype=float)})
    if not matched.any():
        return BatchedPricingResult(measure_year=empty, portfolio_hourly=np.zeros(HOURS_PER_YEAR), n_batches=1, elapsed_seconds=time.perf_counter() - start,
                                    batch_size=n, n_real_shape=0, n_fallback_shape=0, unmatched_measure_ids=unmatched_ids)

    once = getattr(shift_fn, "year_independent", False)       # the production shift (real_impact_profiles.shift_for_year) ignores the year: shift once per route

    def fill_unit(g: int, years: list[int]) -> np.ndarray:
        """U(route g, year, quarter): the four quarterly sums of shape x price, against the nearest ACC year."""
        shape, represents_year = shapes[g]
        series = acc[g]
        available = sorted(series)
        out = np.zeros((len(years), 4))
        aligned = shift_fn(shape, years[0], represents_year) if once else None
        for i, y in enumerate(years):
            nearest = min(available, key=lambda v: abs(v - y))
            hourly = (aligned if once else shift_fn(shape, y, represents_year)) * series[nearest]
            for q, (lo, hi) in enumerate(QUARTER_BOUNDS):
                out[i, q] = float(hourly[lo:hi].sum())
        return out

    value, has_life = _lifetime_values(
        codes[matched], measures["claim_year"].to_numpy()[matched].astype(np.int64), measures["claim_quarter"].to_numpy()[matched].astype(np.int64),
        measures["eul_years_exact"].to_numpy(dtype=np.float64)[matched], measures["rul_years_exact"].to_numpy(dtype=np.float64)[matched],
        measures["shape_scale"].to_numpy(dtype=np.float64)[matched], measures["shape_scale_2nd"].to_numpy(dtype=np.float64)[matched], rqf_group, base_year, fill_unit)
    measure_year = pd.DataFrame({"measure_id": ids[matched][has_life], "annual_value": value[has_life]})
    return BatchedPricingResult(measure_year=measure_year, portfolio_hourly=np.zeros(HOURS_PER_YEAR), n_batches=1, elapsed_seconds=time.perf_counter() - start,
                                batch_size=n, n_real_shape=int(matched.sum()), n_fallback_shape=0, unmatched_measure_ids=unmatched_ids)


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
    (pipeline/gas_savings_profiles.py) times its real annual_net_therms,
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

    Measures with no real gas shape match or zero annual_net_therms
    (BOTH baseline phases -- see below) are skipped (no row emitted)
    rather than priced at a meaningless zero.

    DUAL-BASELINE / accelerated-replacement measures (2026-09-16 fix, same
    root cause and fix as the electric side -- see engine._phased_scale()
    and data_loading.py's _derive_fields() docstring for the full
    real-world evidence): annual_net_therms_1st applies for quarters within
    the pre-existing equipment's Remaining Useful Life, annual_net_therms_2nd
    for quarters after. rul_years_exact<=0 (not an early replacement, the
    normal case) always uses annual_net_therms_1st for the whole EUL.
    """
    rows: list[dict] = []

    for _, m in measures.iterrows():
        therms_1st = float(m["annual_net_therms_1st"])
        therms_2nd = float(m["annual_net_therms_2nd"])
        if therms_1st == 0 and therms_2nd == 0:
            continue
        shape = get_gas_shape(m)
        if shape is None:
            continue

        acc_years = get_gas_acc_years(m)
        rul_quarters = float(m["rul_years_exact"]) * 4.0

        claim_year = int(m["claim_year"])
        claim_quarter = int(m["claim_quarter"])
        rqf = 1.0 + get_discount_rate_qtr(m)

        eul_quarters = float(m["eul_years_exact"]) * 4.0
        full_quarters = int(eul_quarters)
        frac_remainder = eul_quarters - full_quarters
        n_billed_quarters = full_quarters + (1 if frac_remainder > 1e-9 else 0)

        for k in range(n_billed_quarters):
            abs_quarter = (claim_quarter - 1) + k
            target_year = claim_year + abs_quarter // 4
            quarter_idx = abs_quarter % 4
            is_last = k == n_billed_quarters - 1
            frac = frac_remainder if (is_last and frac_remainder > 1e-9) else 1.0

            annual_net_therms = _phased_scale(k, rul_quarters, therms_1st, therms_2nd)
            clamped_year = min(max(target_year, min(acc_years)), max(acc_years))
            monthly_acc = acc_years[clamped_year]
            monthly_value = shape * annual_net_therms * monthly_acc
            a, b = GAS_QUARTER_MONTHS[quarter_idx]
            quarter_value = monthly_value[a:b].sum()

            qac = (target_year - base_year) * 4 + quarter_idx
            discount_factor = 1.0 / (rqf ** qac)

            rows.append({
                "measure_id": m["measure_id"],
                "year": target_year,
                "quarter": quarter_idx + 1,
                "gas_benefit": quarter_value * frac * discount_factor,
            })

    return pd.DataFrame(rows)


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
    Returns one row per priced measure (measure_id, gas_benefit = its lifetime discounted value): the per-measure version's rows summed by measure. Measures with no
    therms or no matching profile get no row."""
    n = len(measures)
    therms_1st = measures["annual_net_therms_1st"].to_numpy(dtype=np.float64)
    therms_2nd = measures["annual_net_therms_2nd"].to_numpy(dtype=np.float64)
    has_gas = (therms_1st != 0) | (therms_2nd != 0)
    empty = pd.DataFrame({"measure_id": pd.Series(dtype=object), "gas_benefit": pd.Series(dtype=float)})
    if not has_gas.any():
        return empty
    codes = np.arange(n) if route_key is None else pd.factorize(pd.Series(list(route_key), dtype=object))[0]
    n_groups = int(codes.max()) + 1
    gas_pos = np.flatnonzero(has_gas)
    groups_with_gas, first = np.unique(codes[gas_pos], return_index=True)              # each route's first measure with gas savings
    shapes: dict[int, np.ndarray] = {}
    acc: dict[int, dict[int, np.ndarray]] = {}
    rqf_group = np.ones(n_groups)
    for g, rep in zip(groups_with_gas.tolist(), measures.iloc[gas_pos[first]].to_dict("records")):
        shape = get_gas_shape(rep)
        if shape is None:
            continue
        shapes[g] = shape
        acc[g] = get_gas_acc_years(rep)
        rqf_group[g] = 1.0 + get_discount_rate_qtr(rep)
    priced_group = np.zeros(n_groups, dtype=bool)
    priced_group[list(shapes)] = True
    priced = has_gas & priced_group[codes]
    if not priced.any():
        return empty
    def fill_unit(g: int, years: list[int]) -> np.ndarray:
        shape, series = shapes[g], acc[g]
        lo, hi = min(series), max(series)                                      # years outside the gas ACC use its first / last year
        out = np.zeros((len(years), 4))
        for i, y in enumerate(years):
            weighted = shape * series[min(max(y, lo), hi)]
            for q, (a, b) in enumerate(GAS_QUARTER_MONTHS):
                out[i, q] = float(weighted[a:b].sum())
        return out

    value, has_life = _lifetime_values(
        codes[priced], measures["claim_year"].to_numpy()[priced].astype(np.int64), measures["claim_quarter"].to_numpy()[priced].astype(np.int64),
        measures["eul_years_exact"].to_numpy(dtype=np.float64)[priced], measures["rul_years_exact"].to_numpy(dtype=np.float64)[priced],
        therms_1st[priced], therms_2nd[priced], rqf_group, base_year, fill_unit)
    return pd.DataFrame({"measure_id": measures["measure_id"].to_numpy()[priced][has_life], "gas_benefit": value[has_life]})


def price_bill_impact_per_measure(
    measures: pd.DataFrame,
    get_elec_rate_years: Callable[[pd.Series], dict[int, float] | None],
    get_gas_rate_years: Callable[[pd.Series], dict[int, float] | None],
    get_discount_rate_annual: Callable[[pd.Series], float],
    settings_base_year: int = 2024,
) -> pd.DataFrame:
    """
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
    it at scale. `_phased_scale()` isn't used here for this reason -- its
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
        claim_year = int(m["claim_year"])
        yr1 = claim_year - settings_base_year  # CalcCE's own Yr1 (RunParameters BaseYear: 2024 for Versions 2026 and 2027) -- see docstring's discount-asymmetry note
        raf = 1.0 + get_discount_rate_annual(m)
        rul_years = float(m["rul_years_exact"])
        eul_years = float(m["eul_years_exact"])
        full_years = int(eul_years)
        frac_remainder = eul_years - full_years
        n_billed_years = full_years + (1 if frac_remainder > 1e-9 else 0)
        # eul1 (real CalcCE field, InputMeasurevw): RUL if 0 < RUL < EUL, else
        # the WHOLE EUL -- i.e. RUL_Yrs==0 means "1st baseline for 100% of the
        # EUL", same convention as _phased_scale() elsewhere in this file.
        floor_rul = int(rul_years) if rul_years > 0 else full_years
        # Share of the boundary year that is still 1st baseline: the fractional part of RUL, 0 when RUL is a whole number, and 1 when RUL <= 0 (a single-baseline
        # measure's fractional last year is entirely 1st baseline). Until 2026-09-21 this was `rul_years - floor_rul`, which for RUL = 0 is -full_years: a
        # single-baseline measure with a fractional EUL (e.g. 4.97 years) booked a NEGATIVE last year and priced at ~0.70x CET (BillReduc/RIM).
        frac_1st_boundary = (rul_years - floor_rul) if rul_years > 0 else 1.0

        def pv_sum(scale_1st: float, scale_2nd: float, rate_years: dict[int, float] | None) -> float | None:
            if rate_years is None:
                return None
            total = 0.0
            # RE1: pure 1st-baseline years, discounted by k = Qy - Yr1
            for k in range(min(floor_rul, n_billed_years)):
                rate = rate_years.get(claim_year + k)
                if rate is not None:
                    total += scale_1st * rate / (raf ** k)
            # boundary year + RE2: 2nd-baseline (and the boundary's 1st-baseline
            # remainder), discounted by Qy = k + yr1 instead -- see docstring
            for k in range(floor_rul, n_billed_years):
                rate = rate_years.get(claim_year + k)
                if rate is None:
                    continue
                is_last = k == n_billed_years - 1
                frac = frac_remainder if (is_last and frac_remainder > 1e-9) else 1.0
                value = frac_1st_boundary * scale_1st + (1.0 - frac_1st_boundary) * scale_2nd if k == floor_rul else scale_2nd
                total += frac * value * rate / (raf ** (k + yr1))
            return total

        # CalcCE's RIM bill impact multiplies by IR x RR (electric) too; annual_net_therms already carries IRThm x RRThm.
        irr_kwh = float(m["ir_kwh"]) * float(m["rr_kwh"])
        elec_pv = pv_sum(float(m["annual_net_kwh_1st"]) * irr_kwh, float(m["annual_net_kwh_2nd"]) * irr_kwh, get_elec_rate_years(m))
        gas_pv = pv_sum(float(m["annual_net_therms_1st"]), float(m["annual_net_therms_2nd"]), get_gas_rate_years(m))

        # Reduc/Incr split (CalcCE 15880-15992): a fuel-substitution measure books a negative value as an INCREASE (Incr, positive);
        # every other measure keeps its signed value in Reduc and has Incr = 0 (`ISNULL(ft.FuelType,0)='0'` branch).
        is_fs = bool(m.get("is_fuel_sub", True))  # default True = the original (fuel-sub-only) behavior for frames without the flag

        def split(pv):
            if pv is None:
                return 0.0, 0.0
            return (max(pv, 0.0), max(-pv, 0.0)) if is_fs else (pv, 0.0)

        reduc_e, incr_e = split(elec_pv)
        reduc_g, incr_g = split(gas_pv)
        rows.append({
            "measure_id": m["measure_id"],
            "LifecycleBillReducElec": reduc_e,
            "LifecycleBillIncrElec": incr_e,
            "LifecycleBillReducGas": reduc_g,
            "LifecycleBillIncrGas": incr_g,
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
    1st-baseline measure cost (`unit_meas_cost_1st`) when RUL_Yrs==0, but
    only the 2nd-baseline cost (`unit_meas_cost_2nd`) when RUL_Yrs>0 (the
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
        qty = float(m["num_units"])
        rqf = 1.0 + get_discount_rate_qtr(m)
        qm = (int(m["claim_year"]) - base_year) * 4 + (int(m["claim_quarter"]) - 1)
        pv_factor = rqf ** qm

        incentive_to_others = qty * float(m["unit_incentive_to_others"])
        di_labor = qty * float(m["unit_di_labor"])
        di_material = qty * float(m["unit_di_material"])
        end_user_rebate = qty * float(m["unit_end_user_rebate"])
        rebates_and_incents = incentive_to_others + di_labor + di_material + end_user_rebate
        rebates_and_incents_pv = rebates_and_incents / pv_factor

        incents_and_di = incentive_to_others + di_labor + di_material
        rebates = end_user_rebate
        incents_and_di_pv = incents_and_di / pv_factor
        rebates_pv = rebates / pv_factor

        unit_gross_cost = float(m["unit_meas_cost_1st"])
        meas_incr_cost = float(m["unit_meas_cost_2nd"])
        rul_years = float(m["rul_years_exact"])
        gross_meas_cost = qty * (unit_gross_cost - (unit_gross_cost - meas_incr_cost if rul_years > 0 else 0.0))
        if rul_years > 0:
            inflation_factor = ((1.0 + float(m["meas_inflation"]) / 4.0) / rqf) ** (rul_years * 4.0)
            pv_subtraction = (unit_gross_cost - meas_incr_cost) * inflation_factor
        else:
            pv_subtraction = 0.0
        gross_meas_cost_pv = qty * (unit_gross_cost - pv_subtraction) / pv_factor

        excess_incentives = max(incents_and_di - gross_meas_cost, 0.0) if incents_and_di > gross_meas_cost else 0.0
        excess_incentives_pv = max(incents_and_di_pv - gross_meas_cost_pv, 0.0) if incents_and_di_pv > gross_meas_cost_pv else 0.0

        mark_effect_plus_excess_inc = market_effects_cost * (gross_meas_cost + excess_incentives_pv)
        mark_effect_plus_excess_inc_pv = market_effects_cost * (gross_meas_cost_pv + excess_incentives_pv)

        gross_participant_cost = gross_meas_cost - (incents_and_di + rebates)
        gross_participant_cost_pv = gross_meas_cost_pv - (incents_and_di_pv + rebates_pv)
        gross_participant_cost_adjusted = gross_participant_cost + excess_incentives + mark_effect_plus_excess_inc
        gross_participant_cost_adjusted_pv = gross_participant_cost_pv + excess_incentives_pv + mark_effect_plus_excess_inc_pv

        ntgr_cost = float(m["ntgr_cost"])
        net_participant_cost = ntgr_cost * (gross_participant_cost + excess_incentives)
        net_participant_cost_pv = ntgr_cost * (gross_participant_cost_pv + excess_incentives_pv)
        net_participant_cost_adjusted = net_participant_cost + mark_effect_plus_excess_inc
        net_participant_cost_adjusted_pv = net_participant_cost_pv + mark_effect_plus_excess_inc_pv

        rows.append({
            "measure_id": m["measure_id"],
            "IncentiveToOthers": incentive_to_others,
            "DILaborCost": di_labor,
            "DIMaterialCost": di_material,
            "EndUserRebate": end_user_rebate,
            "RebatesandIncents": rebates_and_incents,
            "RebatesandIncentsPV": rebates_and_incents_pv,
            "GrossMeasureCost": gross_meas_cost,
            "GrossMeasCostPV": gross_meas_cost_pv,
            "ExcessIncentives": excess_incentives,
            "ExcessIncentivesPV": excess_incentives_pv,
            "MarkEffectPlusExcessInc": mark_effect_plus_excess_inc,
            "MarkEffectPlusExcessIncPV": mark_effect_plus_excess_inc_pv,
            "GrossParticipantCost": gross_participant_cost,
            "GrossParticipantCostPV": gross_participant_cost_pv,
            "GrossParticipantCostAdjusted": gross_participant_cost_adjusted,
            "GrossParticipantCostAdjustedPV": gross_participant_cost_adjusted_pv,
            "NetParticipantCost": net_participant_cost,
            "NetParticipantCostPV": net_participant_cost_pv,
            "NetParticipantCostAdjusted": net_participant_cost_adjusted,
            "NetParticipantCostAdjustedPV": net_participant_cost_adjusted_pv,
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
        qty = float(m["num_units"])
        ntgr_kwh = float(m["NTGRkWh"])
        other_ben = qty * (ntgr_kwh + market_effects_bens) * (
            float(m["unit_gas_infra_bens"]) + float(m["unit_refrig_bens"]) + float(m["unit_misc_bens"])
        )
        other_cost = qty * (ntgr_kwh + market_effects_cost) * (
            float(m["unit_refrig_costs"]) + float(m["unit_misc_costs"])
        )
        tax_credits = qty * (ntgr_kwh + market_effects_bens) * float(m["unit_tax_credits"])
        gross_ben = qty * (float(m["unit_gas_infra_bens"]) + float(m["unit_refrig_bens"]) + float(m["unit_misc_bens"]))
        gross_cost = qty * (float(m["unit_refrig_costs"]) + float(m["unit_misc_costs"]))
        rows.append({
            "measure_id": m["measure_id"],
            "OtherBen": other_ben,
            "OtherCost": other_cost,
            "TaxCredits": tax_credits,
            # Gross = same formulas with no NTG / market-effects factor (CalcCE lines 13862, 13936, 14417)
            "OtherBenGross": gross_ben,
            "OtherCostGross": gross_cost,
            "TaxCreditsGross": qty * float(m["unit_tax_credits"]),
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------------------------------------------------------------------------
# Array versions: the arithmetic of the *_per_measure functions above (read their docstrings for the CalcCE mechanics), on whole columns at once. A loop of a
# few hundred thousand rows through DataFrame.iterrows is what made these three the slowest steps of a large run (about 55% of the pricing time at 100,000
# records); here only the bill impact loops, over the years of a life (at most a few dozen), not over measures.

def _col(m: pd.DataFrame, name: str, default: float = 0.0) -> np.ndarray:
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
    settings_base_year: int = 2024,
) -> pd.DataFrame:
    """price_bill_impact_per_measure on arrays. The callbacks are called once per distinct (PA, target_sector), (PA, gas_sector) and PA (the fields they read
    in run_poc.run_bill_impact). Years with no rate count 0, a fuel with no rate table gives 0, exactly as in the reference."""
    m = measures
    n_rows = len(m)
    if n_rows == 0:
        return pd.DataFrame(columns=["measure_id", "LifecycleBillReducElec", "LifecycleBillIncrElec", "LifecycleBillReducGas", "LifecycleBillIncrGas"])
    claim_year = m["claim_year"].astype(int).to_numpy()
    yr1 = claim_year - settings_base_year
    raf = 1.0 + np.asarray(_by_key(m, ["PA"], get_discount_rate_annual), dtype=float)
    rul = _col(m, "rul_years_exact")
    eul = _col(m, "eul_years_exact")
    full = np.floor(eul).astype(int)
    frac_rem = eul - full
    n_years = full + (frac_rem > 1e-9).astype(int)
    floor_rul = np.where(rul > 0, np.floor(rul), full).astype(int)
    f1 = np.where(rul > 0, rul - floor_rul, 1.0)
    k_max = int(n_years.max()) if n_rows else 0

    def rate_matrix(tables: list) -> tuple[np.ndarray, np.ndarray]:
        """(rates[i, k] for the year claim_year[i] + k, 0 where missing; has_table[i])"""
        r = np.zeros((n_rows, max(k_max, 1)))
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

    def pv_sum(s1: np.ndarray, s2: np.ndarray, rates: np.ndarray) -> np.ndarray:
        total = np.zeros(n_rows)
        for k in range(k_max):
            rate = rates[:, k]
            first = k < np.minimum(floor_rul, n_years)                                   # RE1: discounted by k
            total += np.where(first, s1 * rate / raf ** k, 0.0)
            after = (k >= floor_rul) & (k < n_years)                                      # boundary year and RE2: discounted by k + Yr1 (CalcCE's Qy)
            frac = np.where((k == n_years - 1) & (frac_rem > 1e-9), frac_rem, 1.0)
            value = np.where(k == floor_rul, f1 * s1 + (1.0 - f1) * s2, s2)
            total += np.where(after, frac * value * rate / raf ** (k + yr1), 0.0)
        return total

    irr = _col(m, "ir_kwh", 1.0) * _col(m, "rr_kwh", 1.0)
    rates_e, has_e = rate_matrix(_by_key(m, ["PA", "target_sector"], get_elec_rate_years))
    rates_g, has_g = rate_matrix(_by_key(m, ["PA", "gas_sector"], get_gas_rate_years))
    pv_e = np.where(has_e, pv_sum(_col(m, "annual_net_kwh_1st") * irr, _col(m, "annual_net_kwh_2nd") * irr, rates_e), 0.0)
    pv_g = np.where(has_g, pv_sum(_col(m, "annual_net_therms_1st"), _col(m, "annual_net_therms_2nd"), rates_g), 0.0)
    fs = m["is_fuel_sub"].fillna(True).astype(bool).to_numpy() if "is_fuel_sub" in m.columns else np.ones(n_rows, dtype=bool)
    return pd.DataFrame({
        "measure_id": m["measure_id"].to_numpy(),
        "LifecycleBillReducElec": np.where(fs, np.maximum(pv_e, 0.0), pv_e),
        "LifecycleBillIncrElec": np.where(fs, np.maximum(-pv_e, 0.0), 0.0),
        "LifecycleBillReducGas": np.where(fs, np.maximum(pv_g, 0.0), pv_g),
        "LifecycleBillIncrGas": np.where(fs, np.maximum(-pv_g, 0.0), 0.0),
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
    qty = _col(m, "num_units")
    rqf = 1.0 + np.asarray(_by_key(m, ["PA"], get_discount_rate_qtr), dtype=float)
    qm = (m["claim_year"].astype(int).to_numpy() - base_year) * 4 + (m["claim_quarter"].astype(int).to_numpy() - 1)
    pvf = rqf ** qm
    ito, lab, mat, reb = (qty * _col(m, c) for c in ("unit_incentive_to_others", "unit_di_labor", "unit_di_material", "unit_end_user_rebate"))
    incents_di = ito + lab + mat
    unit_gross, incr = _col(m, "unit_meas_cost_1st"), _col(m, "unit_meas_cost_2nd")
    rul = _col(m, "rul_years_exact")
    gross_meas = qty * np.where(rul > 0, incr, unit_gross)
    pv_sub = np.where(rul > 0, (unit_gross - incr) * ((1.0 + _col(m, "meas_inflation") / 4.0) / rqf) ** (rul * 4.0), 0.0)
    gross_meas_pv = qty * (unit_gross - pv_sub) / pvf
    excess = np.maximum(incents_di - gross_meas, 0.0)
    excess_pv = np.maximum(incents_di / pvf - gross_meas_pv, 0.0)
    mark = market_effects_cost * (gross_meas + excess_pv)
    mark_pv = market_effects_cost * (gross_meas_pv + excess_pv)
    gross = gross_meas - (incents_di + reb)
    gross_pv = gross_meas_pv - (incents_di + reb) / pvf
    ntgr_cost = _col(m, "ntgr_cost")
    net, net_pv = ntgr_cost * (gross + excess), ntgr_cost * (gross_pv + excess_pv)
    return pd.DataFrame({
        "measure_id": m["measure_id"].to_numpy(),
        "IncentiveToOthers": ito, "DILaborCost": lab, "DIMaterialCost": mat, "EndUserRebate": reb,
        "RebatesandIncents": incents_di + reb, "RebatesandIncentsPV": (incents_di + reb) / pvf,
        "GrossMeasureCost": gross_meas, "GrossMeasCostPV": gross_meas_pv,
        "ExcessIncentives": excess, "ExcessIncentivesPV": excess_pv,
        "MarkEffectPlusExcessInc": mark, "MarkEffectPlusExcessIncPV": mark_pv,
        "GrossParticipantCost": gross, "GrossParticipantCostPV": gross_pv,
        "GrossParticipantCostAdjusted": gross + excess + mark, "GrossParticipantCostAdjustedPV": gross_pv + excess_pv + mark_pv,
        "NetParticipantCost": net, "NetParticipantCostPV": net_pv,
        "NetParticipantCostAdjusted": net + mark, "NetParticipantCostAdjustedPV": net_pv + mark_pv,
    })


def price_other_ben_cost(
    measures: pd.DataFrame,
    market_effects_bens: float = 0.0,
    market_effects_cost: float = 0.0,
) -> pd.DataFrame:
    """price_other_ben_cost_per_measure on arrays."""
    m = measures
    qty, ntg = _col(m, "num_units"), _col(m, "NTGRkWh")
    bens = _col(m, "unit_gas_infra_bens") + _col(m, "unit_refrig_bens") + _col(m, "unit_misc_bens")
    costs = _col(m, "unit_refrig_costs") + _col(m, "unit_misc_costs")
    tax = _col(m, "unit_tax_credits")
    return pd.DataFrame({
        "measure_id": m["measure_id"].to_numpy(),
        "OtherBen": qty * (ntg + market_effects_bens) * bens,
        "OtherCost": qty * (ntg + market_effects_cost) * costs,
        "TaxCredits": qty * (ntg + market_effects_bens) * tax,
        "OtherBenGross": qty * bens,
        "OtherCostGross": qty * costs,
        "TaxCreditsGross": qty * tax,
    })
