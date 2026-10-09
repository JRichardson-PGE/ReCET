"""
The core comparison this whole POC exists to run: price a set of measures
two ways --

  legacy_net_then_shape()  -- what CET does today: net the measure's annual
      kWh savings to a single number FIRST, then (conceptually) apply a
      shape/rate. A load-shifting measure with zero net annual savings has
      nothing left to shape, so it prices at $0 regardless of its real
      hour-to-hour avoided-cost value. This mirrors CET Modernization
      Review Section 1.2, item #4.

  price_batched_hourly()  -- the proposed architecture: multiply the
      measure's own hourly array by its subset's cached avoided-cost array
      BEFORE summing, processed in bounded batches so memory/compute never
      depends on holding all measures' hourly arrays at once. This is
      Section 5's batched per-measure pricing layer.

Both operate on the same real per-measure inputs; only the order of
operations differs. That's the entire architectural claim being tested.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .synthetic_curves import HOURS_PER_YEAR

DISCOUNT_RATE = 0.03  # matches the discount_factor term in the review's layer-4 formula


@dataclass
class BatchedPricingResult:
    # Schema depends on which function produced this: price_batched_hourly()
    # returns (measure_id, year, annual_value) rows; price_batched_hourly_
    # vectorized() returns (measure_id, lifetime_value) rows (closed-form
    # summed, not exploded per year -- see that function's docstring).
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


def price_batched_hourly(
    measures: pd.DataFrame,
    load_shapes: dict[str, np.ndarray],
    avoided_cost: dict[str, np.ndarray],
    batch_size: int = 500,
) -> BatchedPricingResult:
    """
    Process `measures` in chunks of `batch_size`. Each batch is priced
    in-memory (vectorized numpy, no persistence of the (measure x hour)
    grain past the batch) and immediately reduced into:
      (a) a (measure_id, year) row appended to the measure-year output, and
      (b) an add into the running, fixed-size portfolio_hourly accumulator.

    Memory footprint per batch is O(batch_size x 8760), never
    O(all_measures x 8760) -- this is the whole point.
    """
    start = time.perf_counter()
    portfolio_hourly = np.zeros(HOURS_PER_YEAR)
    measure_year_rows: list[dict] = []
    n_batches = 0

    for batch_start in range(0, len(measures), batch_size):
        batch = measures.iloc[batch_start:batch_start + batch_size]
        n_batches += 1

        for _, m in batch.iterrows():
            shape = load_shapes[m["shape_key"]] * m["shape_scale"]  # normalized pattern -> this measure's own magnitude
            ac = avoided_cost[m["subset_key"]]
            hourly_value = shape * ac  # the vectorized per-measure dot-product, elementwise
            annual_value_year1 = float(hourly_value.sum())

            portfolio_hourly += hourly_value  # fixed-size accumulator, never grows

            for year in range(1, m["eul_years"] + 1):
                discount_factor = 1.0 / ((1 + DISCOUNT_RATE) ** (year - 1))
                measure_year_rows.append({
                    "measure_id": m["measure_id"],
                    "year": year,
                    "annual_value": annual_value_year1 * discount_factor,
                })

    elapsed = time.perf_counter() - start
    return BatchedPricingResult(
        measure_year=pd.DataFrame(measure_year_rows),
        portfolio_hourly=portfolio_hourly,
        n_batches=n_batches,
        elapsed_seconds=elapsed,
        batch_size=batch_size,
    )


def price_batched_hourly_vectorized(
    measures: pd.DataFrame,
    avoided_cost: dict[str, np.ndarray],
    batch_size: int,
    rng: np.random.Generator,
) -> BatchedPricingResult:
    """
    Same architecture as price_batched_hourly(), but for real-scale
    benchmarking: load shapes are generated FRESH per batch (via
    synthetic_curves.load_shape_batch, fully vectorized) and discarded once
    that batch is priced -- never held for all measures at once. This is
    the actual memory-bounded behavior the batching claim depends on;
    price_batched_hourly()'s pre-built load_shapes dict is fine for a
    handful of real measures but would not scale, which is exactly why this
    separate path exists for the benchmark.

    Per-batch work is grouped by avoided-cost subset so the (few) avoided-
    cost arrays are never broadcast out to a full (batch_size x 8760)
    matrix -- only the batch's own (already-necessary) shape matrix is that
    large.
    """
    from .synthetic_curves import load_shape_batch  # local import: benchmark-only path

    start = time.perf_counter()
    portfolio_hourly = np.zeros(HOURS_PER_YEAR)
    lifetime_rows: list[dict] = []
    n_batches = 0

    for batch_start in range(0, len(measures), batch_size):
        batch = measures.iloc[batch_start:batch_start + batch_size].reset_index(drop=True)
        n_batches += 1

        shapes = load_shape_batch(batch["annual_net_kwh"].to_numpy(), rng)  # (len(batch), HOURS_PER_YEAR)
        hourly_value = np.empty_like(shapes)

        for subset_key, group in batch.groupby("subset_key"):
            idx = group.index.to_numpy()
            hourly_value[idx] = shapes[idx] * avoided_cost[subset_key]  # broadcasts (H,) against (k,H)

        annual_value_year1 = hourly_value.sum(axis=1)
        portfolio_hourly += hourly_value.sum(axis=0)

        eul = batch["eul_years"].to_numpy()
        # Closed-form sum of a discounted geometric series over each measure's
        # EUL, instead of materializing one row per (measure, year) -- avoids
        # a second source of unnecessary memory growth at real scale.
        r = DISCOUNT_RATE
        lifetime_value = annual_value_year1 * (1 - (1 / (1 + r)) ** eul) / (1 - 1 / (1 + r))

        lifetime_rows.append(pd.DataFrame({
            "measure_id": batch["measure_id"].to_numpy(),
            "lifetime_value": lifetime_value,
        }))

    elapsed = time.perf_counter() - start
    return BatchedPricingResult(
        measure_year=pd.concat(lifetime_rows, ignore_index=True),
        portfolio_hourly=portfolio_hourly,
        n_batches=n_batches,
        elapsed_seconds=elapsed,
        batch_size=batch_size,
    )


def legacy_net_then_shape(
    measures: pd.DataFrame,
    load_shapes: dict[str, np.ndarray],
    avoided_cost: dict[str, np.ndarray],
) -> pd.DataFrame:
    """
    Simulates CET's current calculation order: net the measure's annual kWh
    savings to one number first (== load_shapes[shape_key].sum() * shape_scale,
    which is ~0 for a load-shifting measure by construction since its
    normalized pattern sums to 0), THEN multiply by a single blended
    avoided-cost rate for that subset. No hourly join, no per-hour
    dimension -- matches the real gap: quarterized/annual aggregation
    happens before CalcCE ever sees hourly values.
    """
    rows = []
    for _, m in measures.iterrows():
        net_annual_kwh = float(load_shapes[m["shape_key"]].sum()) * m["shape_scale"]
        blended_rate = float(avoided_cost[m["subset_key"]].mean())
        for year in range(1, m["eul_years"] + 1):
            discount_factor = 1.0 / ((1 + DISCOUNT_RATE) ** (year - 1))
            rows.append({
                "measure_id": m["measure_id"],
                "year": year,
                "annual_value": net_annual_kwh * blended_rate * discount_factor,
            })
    return pd.DataFrame(rows)


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
       price_batched_hourly does) silently mis-prices any measure whose EUL
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

    Not vectorized/batched like price_batched_hourly_vectorized -- this is
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
    quarter), and every measure's quarters are then expanded into flat arrays and evaluated with a handful of vectorised operations.

    route_key: one hashable per measure (aligned with `measures`) naming its route; `get_shape`, `get_acc_years` and `get_discount_rate_qtr` are called once per route, on
    that route's first measure, and must give the same answer for every measure of it. None = every measure is its own route. The result is the per-measure rows the
    per-measure version returns (measure_id, year, quarter, annual_value), to rounding (the multiplications are ordered differently).

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

    # --- one shape / ACC series / rate per route
    shapes: dict[int, tuple[np.ndarray, int]] = {}
    acc: dict[int, dict[int, np.ndarray]] = {}
    rqf_group = np.ones(n_groups)
    for g, pos in enumerate(first_pos):
        rep = measures.iloc[pos]
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

    empty = pd.DataFrame({"measure_id": pd.Series(dtype=object), "year": pd.Series(dtype=np.int64), "quarter": pd.Series(dtype=np.int64),
                          "annual_value": pd.Series(dtype=float)})
    if not matched.any():
        return BatchedPricingResult(measure_year=empty, portfolio_hourly=np.zeros(HOURS_PER_YEAR), n_batches=1, elapsed_seconds=time.perf_counter() - start,
                                    batch_size=n, n_real_shape=0, n_fallback_shape=0, unmatched_measure_ids=unmatched_ids)

    # --- every matched measure's quarters as flat arrays
    g_m = codes[matched]
    m_idx, year, quarter, weight, scale, discount = _quarter_rows(
        measures["claim_year"].to_numpy()[matched].astype(np.int64), measures["claim_quarter"].to_numpy()[matched].astype(np.int64),
        measures["eul_years_exact"].to_numpy(dtype=np.float64)[matched], measures["rul_years_exact"].to_numpy(dtype=np.float64)[matched],
        measures["shape_scale"].to_numpy(dtype=np.float64)[matched], measures["shape_scale_2nd"].to_numpy(dtype=np.float64)[matched], rqf_group[g_m], base_year)

    # --- U(route, year, quarter): the four quarterly sums of shape x price, once per route and year
    y0, y1 = int(year.min()), int(year.max())
    unit = np.zeros((n_groups, y1 - y0 + 1, 4))
    g_row = g_m[m_idx]
    order = np.argsort(g_row, kind="stable")
    for rows in np.split(order, np.flatnonzero(np.diff(g_row[order])) + 1):
        g = int(g_row[rows[0]])
        shape, represents_year = shapes[g]
        series = acc[g]
        available = sorted(series)
        for y in np.unique(year[rows]):
            y = int(y)
            nearest = min(available, key=lambda v: abs(v - y))
            hourly = shift_fn(shape, y, represents_year) * series[nearest]
            for q, (lo, hi) in enumerate(QUARTER_BOUNDS):
                unit[g, y - y0, q] = float(hourly[lo:hi].sum())

    value = scale * weight * discount * unit[g_row, year - y0, quarter]
    measure_year = pd.DataFrame({"measure_id": ids[matched][m_idx], "year": year, "quarter": quarter + 1, "annual_value": value})
    return BatchedPricingResult(measure_year=measure_year, portfolio_hourly=np.zeros(HOURS_PER_YEAR), n_batches=1, elapsed_seconds=time.perf_counter() - start,
                                batch_size=n, n_real_shape=int(matched.sum()), n_fallback_shape=0, unmatched_measure_ids=unmatched_ids)


def price_real_hourly_legacy(
    measures: pd.DataFrame,
    get_shape: Callable[[pd.Series], tuple[np.ndarray, int] | None],
    get_acc_years: Callable[[pd.Series], dict[int, np.ndarray]],
    get_discount_rate_qtr: Callable[[pd.Series], float],
    base_year: int,
    fallback_shape: Callable[[pd.Series], np.ndarray] | None = None,
) -> pd.DataFrame:
    """
    legacy_net_then_shape()'s real-data counterpart, using the same real
    shape/avoided-cost/discount-rate sources as price_real_hourly() so the
    two are an apples-to-apples comparison: net the measure's annual kWh
    first (from its real profile, at its OWN claim year's calendar --
    there's no hourly join to misalign in this branch, so no shift is
    needed for the netting step itself), then multiply by ONE blended
    avoided-cost rate for its claim year, applied across every EUL quarter
    (still real quarter-precise discounting -- that part of the fix applies
    regardless of calculation order, it's not what this comparison is
    testing). This mirrors legacy_net_then_shape's real limitation: no
    year-varying rate, no hourly dimension at all -- just done with real
    inputs instead of synthetic ones.

    The net annual kWh itself still applies the real RUL-based dual-
    baseline phase (see engine._phased_scale()) -- that's a real fact about
    what the measure saves each year, not part of the "calculation order"
    flaw this function exists to isolate, so both branches of the
    comparison get it right and only the ORDER of operations differs.
    """
    rows = []
    for _, m in measures.iterrows():
        shape_result = get_shape(m)
        if shape_result is not None:
            shape, _represents_year = shape_result
        elif fallback_shape is not None:
            shape = fallback_shape(m)
        else:
            continue

        claim_year = int(m["claim_year"])
        claim_quarter = int(m["claim_quarter"])
        shape_sum = float(shape.sum())
        rul_quarters = float(m["rul_years_exact"]) * 4.0
        scale_1st = float(m["shape_scale"])
        scale_2nd = float(m["shape_scale_2nd"])
        rqf = 1.0 + get_discount_rate_qtr(m)

        acc_years = get_acc_years(m)
        acc_year = min(acc_years, key=lambda y: abs(y - claim_year))
        blended_rate = float(acc_years[acc_year].mean())

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

            scale = _phased_scale(k, rul_quarters, scale_1st, scale_2nd)
            net_quarterly_value = shape_sum * scale * blended_rate / 4.0

            qac = (target_year - base_year) * 4 + quarter_idx
            discount_factor = 1.0 / (rqf ** qac)
            rows.append({
                "measure_id": m["measure_id"],
                "year": target_year,
                "quarter": quarter_idx + 1,
                "annual_value": net_quarterly_value * frac * discount_factor,
            })
    return pd.DataFrame(rows)


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
    and U depends only on the measure's profile, IOU, gas sector and the pricing year, so it is computed once per (route, year, quarter) and every measure's quarters are
    evaluated as flat arrays. route_key: one hashable per measure naming its route (the callbacks are called once per route, on its first measure with gas savings, and
    must give the same answer for every measure of it); None = every measure is its own route. Measures with no therms or no matching profile get no rows."""
    n = len(measures)
    therms_1st = measures["annual_net_therms_1st"].to_numpy(dtype=np.float64)
    therms_2nd = measures["annual_net_therms_2nd"].to_numpy(dtype=np.float64)
    has_gas = (therms_1st != 0) | (therms_2nd != 0)
    empty = pd.DataFrame({"measure_id": pd.Series(dtype=object), "year": pd.Series(dtype=np.int64), "quarter": pd.Series(dtype=np.int64), "gas_benefit": pd.Series(dtype=float)})
    if not has_gas.any():
        return empty
    codes = np.arange(n) if route_key is None else pd.factorize(pd.Series(list(route_key), dtype=object))[0]
    n_groups = int(codes.max()) + 1
    first_with_gas: dict[int, int] = {}
    for pos in np.flatnonzero(has_gas):
        first_with_gas.setdefault(int(codes[pos]), int(pos))
    shapes: dict[int, np.ndarray] = {}
    acc: dict[int, dict[int, np.ndarray]] = {}
    rqf_group = np.ones(n_groups)
    for g, pos in first_with_gas.items():
        rep = measures.iloc[pos]
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
    g_m = codes[priced]
    m_idx, year, quarter, weight, scale, discount = _quarter_rows(
        measures["claim_year"].to_numpy()[priced].astype(np.int64), measures["claim_quarter"].to_numpy()[priced].astype(np.int64),
        measures["eul_years_exact"].to_numpy(dtype=np.float64)[priced], measures["rul_years_exact"].to_numpy(dtype=np.float64)[priced],
        therms_1st[priced], therms_2nd[priced], rqf_group[g_m], base_year)

    y0, y1 = int(year.min()), int(year.max())
    unit = np.zeros((n_groups, y1 - y0 + 1, 4))
    g_row = g_m[m_idx]
    order = np.argsort(g_row, kind="stable")
    for rows in np.split(order, np.flatnonzero(np.diff(g_row[order])) + 1):
        g = int(g_row[rows[0]])
        shape, series = shapes[g], acc[g]
        lo, hi = min(series), max(series)                                      # years outside the gas ACC use its first / last year
        for y in np.unique(year[rows]):
            y = int(y)
            weighted = shape * series[min(max(y, lo), hi)]
            for q, (a, b) in enumerate(GAS_QUARTER_MONTHS):
                unit[g, y - y0, q] = float(weighted[a:b].sum())

    value = scale * weight * discount * unit[g_row, year - y0, quarter]
    return pd.DataFrame({"measure_id": measures["measure_id"].to_numpy()[priced][m_idx], "year": year, "quarter": quarter + 1, "gas_benefit": value})


def price_scg_electric_quarterly(   # NOT USED by the POC since 2026-10-02 (8,760 path only); kept for research scripts
    measures: pd.DataFrame,
    get_quarterly_rates: Callable[[pd.Series], dict[tuple[int, int], float]],
    get_discount_rate_qtr: Callable[[pd.Series], float],
    base_year: int,
) -> pd.DataFrame:
    """
    Real electric benefit for SCG-attributed measures, using CET's own
    real quarterly avoided-cost impact combination table directly (see
    pipeline/scg_electric_avoided_cost.py's docstring for the full
    real-world evidence this replaces an earlier PG&E-substitution
    approach that never fully closed a real ~7-10% gap). Unlike
    price_real_hourly, there's no hourly shape/shift/aggregation step
    here: get_quarterly_rates already returns the impact-profile-weighted
    $/kWh-equivalent rate per (year, quarter) directly, so this just
    multiplies by the measure's own (RUL-phased) annual net kWh and
    discounts -- structurally identical to price_gas_benefit, since both
    real data sources are quarterly-native, not hourly.

    Same dual-baseline RUL phasing as price_real_hourly/price_gas_benefit
    (see engine._phased_scale()).
    """
    rows: list[dict] = []

    for _, m in measures.iterrows():
        rates = get_quarterly_rates(m)
        irr = float(m["ir_kwh"]) * float(m["rr_kwh"])  # CalcCE's IRkWh x RRkWh
        scale_1st = float(m["annual_net_kwh_1st"]) * irr
        scale_2nd = float(m["annual_net_kwh_2nd"]) * irr
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

            rate = rates.get((target_year, quarter_idx + 1))
            if rate is None:
                continue  # outside the source table's real 2024-2054 coverage

            scale = _phased_scale(k, rul_quarters, scale_1st, scale_2nd)
            qac = (target_year - base_year) * 4 + quarter_idx
            discount_factor = 1.0 / (rqf ** qac)

            rows.append({
                "measure_id": m["measure_id"],
                "year": target_year,
                "quarter": quarter_idx + 1,
                "annual_value": scale * rate * frac * discount_factor,
            })

    return pd.DataFrame(rows)


def price_tou_electric(   # NOT USED by the POC since 2026-10-02 (8,760 path only); kept for research scripts
    measures: pd.DataFrame,
    get_tou_rates: Callable[[pd.Series], dict | None],
    get_discount_rate_qtr: Callable[[pd.Series], float],
    base_year: int,
    market_effects_bens: float = 0.0,
) -> tuple[pd.DataFrame, list]:
    """
    Electric benefit for measures whose impact profile is a TOU (H-factor) profile -- quarterly-native, like the SCG path,
    but with CalcCE's TWO-TERM electric benefit (CET_v25.1_documentation_extracted.txt:13415-13450):

        ElecBen = Qty * [ (NTGRkWh+MEBens) * IRkWh * RRkWh * (kWh1*Gen_1 + kWh2*Gen_2)
                        + (NTGRkW +MEBens) * IRkW  * RRkW  * (DS1 *TD_1  + DS2 *TD_2 ) ]

    where DS = the measure's kW savings when the avoided-cost row's DSType is 'kW' (PG&E commercial/industrial/agricultural
    TOU, all SCE TOU) and its kWh savings when DSType is 'kWh' (PG&E residential TOU). The rate table's TD column therefore
    means "$ per kW" for kW rows and "$ per kWh" for kWh rows (pipeline/tou_profiles.py). The older bundled (Gen+TD) x kWh
    pricing is only correct for kWh rows.

    get_tou_rates(measure) -> tou_profiles.quarterly_rates() dict, or None if the measure isn't a supported TOU profile
    (its id is returned in the second element so the caller can flag it -- never priced from a synthetic shape).
    Same dual-baseline RUL phasing, fractional last quarter and quarterly discounting as price_real_hourly.
    """
    rows: list[dict] = []
    unmatched: list = []
    for _, m in measures.iterrows():
        tou = get_tou_rates(m)
        if tou is None:
            unmatched.append(m["measure_id"])
            continue
        years = {y: i for i, y in enumerate(tou["years"])}
        qty = float(m["num_units"])
        is_kw = tou["ds_type"] == "kW"
        kwh1, kwh2 = float(m["unit_kwh_1st"]), float(m["unit_kwh_2nd"])
        kw1, kw2 = float(m["unit_kw_1st"]), float(m["unit_kw_2nd"])
        ds1, ds2 = (kw1, kw2) if is_kw else (kwh1, kwh2)
        f_kwh = qty * (float(m["ntgr_kwh"]) + market_effects_bens) * float(m["ir_kwh"]) * float(m["rr_kwh"])
        f_kw = qty * (float(m["ntgr_kw"]) + market_effects_bens) * float(m["ir_kw"]) * float(m["rr_kw"])
        gen_1, gen_2 = f_kwh * kwh1, f_kwh * kwh2
        td_1, td_2 = f_kw * ds1, f_kw * ds2
        rul_quarters = float(m["rul_years_exact"]) * 4.0
        claim_year, claim_quarter = int(m["claim_year"]), int(m["claim_quarter"])
        rqf = 1.0 + get_discount_rate_qtr(m)
        eul_quarters = float(m["eul_years_exact"]) * 4.0
        full_quarters = int(eul_quarters)
        frac_remainder = eul_quarters - full_quarters
        n_billed = full_quarters + (1 if frac_remainder > 1e-9 else 0)
        for k in range(n_billed):
            abs_quarter = (claim_quarter - 1) + k
            target_year = claim_year + abs_quarter // 4
            quarter_idx = abs_quarter % 4
            yi = years.get(target_year)
            if yi is None:
                continue  # outside the ACC's 2024-2054 coverage
            frac = frac_remainder if (k == n_billed - 1 and frac_remainder > 1e-9) else 1.0
            value = (_phased_scale(k, rul_quarters, gen_1, gen_2) * tou["gen"][yi, quarter_idx]
                     + _phased_scale(k, rul_quarters, td_1, td_2) * tou["td"][yi, quarter_idx])
            qac = (target_year - base_year) * 4 + quarter_idx
            rows.append({"measure_id": m["measure_id"], "year": target_year, "quarter": quarter_idx + 1,
                         "annual_value": value * frac / (rqf ** qac)})
    return pd.DataFrame(rows, columns=["measure_id", "year", "quarter", "annual_value"]), unmatched


def price_bill_impact(
    measures: pd.DataFrame,
    get_elec_rate_years: Callable[[pd.Series], dict[int, float] | None],
    get_gas_rate_years: Callable[[pd.Series], dict[int, float] | None],
    get_discount_rate_annual: Callable[[pd.Series], float],
    settings_base_year: int = 2024,
) -> pd.DataFrame:
    """
    Real ratepayer bill impact (`BillReducElec`/`BillIncrElec`/
    `BillReducGas`/`BillIncrGas` -- the RIM test's other half, alongside
    system avoided cost: `RIMCost = BillReducElec + BillReducGas +
    PACCost`), traced directly from `CalcCE`'s real SQL
    (CET_v25.1_documentation_extracted.txt:15859-16106).

    UNLIKE every other benefit stream in this POC, the real calc here is
    ANNUAL, not quarterly: pipeline/rate_schedules.py's `RateE`/`RateG`
    values are one-per-calendar-year (the real `E3RateScheduleElec[/Gas]`
    tables' "Qy" field, despite its name, increments by year, not
    quarter -- confirmed empirically), and `CalcCE`'s own PV-sum for this
    piece discounts with the ANNUAL `Raf` (1+DiscountRateAnnual), not the
    quarterly `Rqf` this POC's other pricing functions use -- consistent
    with discount_settings.py's documented distinction between
    quarterly-granularity and annual-granularity benefit streams.

    A REAL, CONFIRMED ASYMMETRY in CalcCE's own SQL (found 2026-09-17,
    chasing a RUL-cohort-specific ~7-9% overshoot that only appeared for
    measures with RUL_Yrs > 0 -- RUL=0 measures matched CET's real
    BillIncrElec/BillReducGas EXACTLY on the first attempt): the "pure 1st
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
    SCE/RUL=8.33 SWWH025 measure's actual `BillIncrElec` before trusting
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
    matching gas schedule still gets a real BillIncrElec, just no
    BillReducGas row.
    """
    rows: list[dict] = []

    for _, m in measures.iterrows():
        claim_year = int(m["claim_year"])
        yr1 = claim_year - settings_base_year  # CalcCE's own Yr1 (E3Settings.BaseYear: 2024 for Versions 2026 and 2027) -- see docstring's discount-asymmetry note
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
            "BillReducElec": reduc_e,
            "BillIncrElec": incr_e,
            "BillReducGas": reduc_g,
            "BillIncrGas": incr_g,
        })

    return pd.DataFrame(rows)


def price_participant_cost(
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


def price_other_ben_cost(
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
