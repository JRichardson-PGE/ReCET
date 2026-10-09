"""
Synthetic-but-realistic hourly avoided-cost and load-shape curves.

THESE ARE PLACEHOLDERS, CLEARLY LABELED AS SUCH. Real values live in:
  - reference-docs/2026_acc/2026 ACC/2026 ACC SERVM Hourly Values_v1a.xlsx
    (real hourly avoided-cost data, by zone/year)
  - eTRM's own per-measure impact profiles (caetrm.com), pullable via its
    public API for real load shapes.

Wiring in real data means writing a loader that produces the same shape this
module produces (a dict of key -> numpy array of length HOURS_PER_YEAR) and
swapping it in -- the pricing engine (engine.py) doesn't care where the
arrays came from.

The TOU shape used here (peak hours ~9x off-peak) is not arbitrary: it
matches the real finding already documented in this review's CLAUDE.md (the
TSB Technical Guidance doc's own Figure 3: "1 MWh saved in hour 19 is worth
~9x the same MWh in hour 11").
"""
from __future__ import annotations

import hashlib

import numpy as np

HOURS_PER_YEAR = 8760
PEAK_HOURS = set(range(17, 21))  # 5pm-9pm, matches TSB doc's cited peak window


def _seed_from_key(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)


def avoided_cost_curve(subset_key: str) -> np.ndarray:
    """
    $/kWh for every hour of a representative year. Peak hours (5-9pm) are
    ~9x off-peak, consistent with the real TSB Technical Guidance figure
    already cited in this review. Small per-subset variation is seeded from
    the subset key so different climate-zone/sector subsets aren't identical.
    """
    rng = np.random.default_rng(_seed_from_key(subset_key))
    base_off_peak = 0.05 + rng.uniform(-0.01, 0.01)
    base_peak = base_off_peak * 9.0

    hours = np.arange(HOURS_PER_YEAR)
    hour_of_day = hours % 24
    day_of_year = hours // 24

    is_peak = np.isin(hour_of_day, list(PEAK_HOURS))
    curve = np.where(is_peak, base_peak, base_off_peak)

    # Mild summer seasonality (higher avoided cost in summer months), since
    # real ACC data shows this and a flat curve would understate realism.
    seasonal = 1.0 + 0.25 * np.sin((day_of_year - 172) / 365 * 2 * np.pi) ** 2
    curve = curve * seasonal
    return curve


def load_shape(shape_key: str) -> np.ndarray:
    """
    NORMALIZED hourly distribution (sums to 1.0) for a named shape -- a
    day-part-weighted pattern, deterministic per shape_key. Deliberately
    takes no annual_kwh: shape_key is a PATTERN (real E3/DEER named shapes
    like "28 = Res. New Const. Lighting" are legitimately reused across
    many measures with different savings magnitudes), and scaling by each
    measure's own annual_net_kwh happens separately, at pricing time (see
    engine.py). Conflating the two -- baking one measure's kWh into an
    array cached by shape_key alone -- silently corrupts every OTHER
    measure sharing that shape_key; this function's signature exists
    specifically to make that bug structurally impossible.
    """
    rng = np.random.default_rng(_seed_from_key(shape_key))
    hours = np.arange(HOURS_PER_YEAR)
    hour_of_day = hours % 24

    # Random-but-deterministic day-part weights (morning/midday/evening/night),
    # gives each shape key a distinct-but-plausible daily profile.
    day_part_weights = rng.dirichlet(alpha=[2, 3, 3, 1])  # sums to 1
    bins = [(6, 10), (10, 17), (17, 22), (22, 30)]  # 30 wraps past midnight
    weight_per_hour = np.zeros(24)
    for (start, end), w in zip(bins, day_part_weights):
        for h in range(start, end):
            weight_per_hour[h % 24] += w / (end - start)
    weight_per_hour = weight_per_hour / weight_per_hour.sum()

    hourly_weight = weight_per_hour[hour_of_day]
    return hourly_weight / hourly_weight.sum()  # sums to 1.0


def load_shape_batch(annual_kwh: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """
    Vectorized generator for a whole batch of measures at once: returns an
    (n_measures, HOURS_PER_YEAR) array. Used by the benchmark's scale test,
    where generating one array per measure via a Python-level loop (like
    load_shape() above) is the actual bottleneck at real scale -- this is
    the same "generate this batch's arrays, use them, let them go" pattern
    the proposed architecture's Glue/Spark batch worker would use, and it's
    also just how you'd write this in numpy regardless of architecture.

    Deliberately NOT seeded per-measure-key (unlike load_shape()) -- at
    benchmark scale we only need realistic-shaped, varied data fast, not
    reproducibility per individual synthetic measure.
    """
    n = len(annual_kwh)
    day_part_weights = rng.dirichlet(alpha=[2, 3, 3, 1], size=n)  # (n, 4), sums to 1 per row
    bins = [(6, 10), (10, 17), (17, 22), (22, 24)]

    weight_per_hour = np.zeros((n, 24))
    for (start, end), col in zip(bins, range(4)):
        weight_per_hour[:, start:end] += day_part_weights[:, col:col + 1] / (end - start)
    weight_per_hour /= weight_per_hour.sum(axis=1, keepdims=True)

    # 24-hour pattern repeats exactly 365 times (24 * 365 == HOURS_PER_YEAR).
    hourly_weight = np.tile(weight_per_hour, 365)
    return hourly_weight * annual_kwh[:, None]


def load_shifting_shape() -> np.ndarray:
    """
    NORMALIZED load-shifting pattern: moves exactly 1 unit of energy from
    peak hours to off-peak hours, net annual change == 0 by construction.
    Like load_shape() above, this takes no magnitude parameter -- multiply
    by however many kWh/year a specific measure shifts (its `shape_scale`)
    at pricing time. This is the demo case that CET's net-then-shape
    calculation prices at $0 and the batched hourly pipeline prices
    correctly (Gap #4 from the CET review).
    """
    hours = np.arange(HOURS_PER_YEAR)
    hour_of_day = hours % 24
    is_peak = np.isin(hour_of_day, list(PEAK_HOURS))
    n_peak_hours = int(is_peak.sum())
    n_offpeak_hours = HOURS_PER_YEAR - n_peak_hours

    per_hour_reduction = 1.0 / n_peak_hours
    per_hour_increase = 1.0 / n_offpeak_hours

    # shape[hour] is kWh SAVED that hour per unit shifted: positive at peak
    # (usage drops there), negative off-peak (usage rises there instead).
    shape = np.where(is_peak, per_hour_reduction, -per_hour_increase)
    assert abs(shape.sum()) < 1e-9, "load-shifting shape must net to ~0 by construction"
    return shape
