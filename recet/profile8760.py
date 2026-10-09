"""Combined 8,760-hour electric load profile of a portfolio ("produce a combined 8760 electric load profile").

For each requested calendar year Y the net electric savings of every measure that is still delivering savings in Y, spread over the year by the measure's
8,760 shape, summed over the portfolio:

    kWh(h, Y) = sum over measures m of  w_m(Y) * shape_m(h, Y)
    w_m(Y)    = annual net kWh delivered by m in Y  =  IR x RR x NTG x kWh/unit x units, using the 1st-baseline value until the measure's RUL ends and the
                2nd-baseline value after, times the fraction of Y the measure is in service (claim quarter to claim quarter + EUL; a claim in Q3 delivers half of its
                first year, the last, part-year of a fractional EUL is prorated).

Positive kWh = savings, negative = added load (fuel substitution). Market effects are an accounting adder, not physical savings, and are NOT included.

Shapes and calendar:
* Every measure uses its 8,760 profile (CEDARS library -- which includes the Gen-based TOU H-factor 8,760s -- or user-supplied), aligned to the ACC's 2018
  weekdays exactly as the pricing does (`real_impact_profiles.shift_for_year`).
* A measure with no shape is left out and listed in the returned `unshaped` table (its kWh is what the totals row calls "unshaped").
* `hour_of_year` is the ACC hour index (1 = Jan 1 00:00); `datetime` labels it on the ACC's calendar, 2018 (every forecast year of the ACC follows 2018's
  weekdays), so the same hour carries the same weekday in every year's column.

Output frame: hour_of_year, datetime (ACC calendar, 2018), then per year `net_kwh_<Y>` (all IOUs) and `net_kwh_<Y>_<PA>` for each PA present.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from .pipeline import real_impact_profiles, system_zones

HOURS = 8760


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def weights_for_year(m, year: int) -> float:
    """Annual-equivalent net kWh that measure `m` (a row/itertuples record) delivers in calendar `year`."""
    start = float(m.claim_year) + (float(m.claim_quarter) - 1.0) / 4.0
    eul = float(m.eul_years_exact)
    end = start + eul
    rul = float(m.rul_years_exact)
    y0, y1 = float(year), float(year) + 1.0
    if rul > 0 and rul < eul:
        w1 = _overlap(y0, y1, start, start + rul)
        w2 = _overlap(y0, y1, start + rul, end)
    else:
        w1, w2 = _overlap(y0, y1, start, end), 0.0
    return w1 * float(m.shape_scale) + w2 * float(m.shape_scale_2nd)


def portfolio_profile(measures: pd.DataFrame, years: list[int], log: Callable[[str], None] = print) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(profile frame, unshaped-measures table, summary dict). See the module docstring."""
    years = sorted(int(y) for y in years)
    pas = sorted(measures["PA"].astype(str).unique())
    shape_cache: dict[tuple, tuple | None] = {}

    def shape_for(m):
        key = (m.shape_key, m.PA, m.target_sector, str(m.climate_zone))
        if key not in shape_cache:
            zone = system_zones.priced_zone(m.shape_key, m.PA, m.target_sector, str(m.climate_zone))   # System -> the profile's System zone
            hit = real_impact_profiles.resolve_shape(m.shape_key, m.PA, m.target_sector, zone)
            shape_cache[key] = None if hit is None else real_impact_profiles.shift_for_year(hit[0], 0, int(hit[1]))   # aligned to the ACC calendar once
        return shape_cache[key]

    # weight per (shape key, PA) per year, so each distinct shape is added once per year
    acc: dict[tuple[int, str], np.ndarray] = {(y, pa): np.zeros(HOURS) for y in years for pa in pas}
    unshaped: list[dict] = []
    kwh_total = {y: 0.0 for y in years}
    kwh_unshaped = {y: 0.0 for y in years}
    groups: dict[tuple, dict[int, float]] = {}
    for m in measures.itertuples(index=False):
        if float(m.shape_scale) == 0 and float(m.shape_scale_2nd) == 0:
            continue
        ws = {y: weights_for_year(m, y) for y in years}
        if not any(ws.values()):
            continue
        hit = shape_for(m)
        for y in years:
            kwh_total[y] += ws[y]
        if hit is None:
            unshaped.append({"measure_id": m.measure_id, "PA": m.PA, "shape_key": m.shape_key, "target_sector": m.target_sector,
                             "climate_zone": m.climate_zone, **{f"kwh_{y}": ws[y] for y in years}})
            for y in years:
                kwh_unshaped[y] += ws[y]
            continue
        g = groups.setdefault((m.shape_key, m.PA, m.target_sector, str(m.climate_zone)), {y: 0.0 for y in years})
        for y in years:
            g[y] += ws[y]
    for key, wy in groups.items():
        shape = shape_cache[key]
        for y in years:
            if wy[y]:
                acc[(y, key[1])] += wy[y] * shape

    df = pd.DataFrame({"hour_of_year": np.arange(1, HOURS + 1)})
    df.insert(1, "datetime", pd.date_range(f"{real_impact_profiles.ACC_CALENDAR_YEAR}-01-01", periods=HOURS, freq="h"))
    for y in years:
        df[f"net_kwh_{y}"] = sum(acc[(y, pa)] for pa in pas)
        for pa in pas:
            df[f"net_kwh_{y}_{pa}"] = acc[(y, pa)]
    summary = {y: {"delivered_net_kwh": kwh_total[y], "in_profile_kwh": float(df[f"net_kwh_{y}"].sum()), "unshaped_kwh": kwh_unshaped[y],
                   "peak_hour_kwh": float(df[f"net_kwh_{y}"].max()), "peak_hour_of_year": int(df.loc[df[f"net_kwh_{y}"].idxmax(), "hour_of_year"])}
               for y in years}
    if unshaped:
        log(f"  {len(unshaped)} measure(s) have no 8,760 shape and are left out of the profile (listed in the unshaped table)")
    return df, pd.DataFrame(unshaped), summary
