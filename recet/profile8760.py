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
    start = float(m.Used_Year) + (float(m.Used_Quarter) - 1.0) / 4.0
    EUL_Yrs = float(m.EUL_Yrs)
    end = start + EUL_Yrs
    RUL_Yrs = float(m.RUL_Yrs)
    y0, y1 = float(year), float(year) + 1.0
    if RUL_Yrs > 0 and RUL_Yrs < EUL_Yrs:
        share_1st = _overlap(y0, y1, start, start + RUL_Yrs)
        share_2nd = _overlap(y0, y1, start + RUL_Yrs, end)
    else:
        share_1st, share_2nd = _overlap(y0, y1, start, end), 0.0
    return share_1st * float(m.AC_Net_kWh_ScaledProfile_1st_Baseline) + share_2nd * float(m.AC_Net_kWh_ScaledProfile_2nd_Baseline)


def portfolio_profile(measures: pd.DataFrame, years: list[int], log: Callable[[str], None] = print) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(profile frame, unshaped-measures table, summary dict). See the module docstring."""
    years = sorted(int(y) for y in years)
    pas = sorted(measures["IOU_AC_Territory"].astype(str).unique())
    shape_cache: dict[tuple, tuple | None] = {}

    def shape_for(m):
        key = (m.Used_AC_Elec_Impact_Profile, m.IOU_AC_Territory, m.AC_Elec_Target_Sector, str(m.AC_Climate_Zone))
        if key not in shape_cache:
            zone = system_zones.priced_zone(*key)                                   # System -> the profile's System zone
            hit = real_impact_profiles.resolve_shape(*key[:3], zone)
            shape_cache[key] = None if hit is None else real_impact_profiles.shift_for_year(hit[0], 0, int(hit[1]))   # aligned to the ACC calendar once
        return shape_cache[key]

    # weight per (shape key, PA) per year, so each distinct shape is added once per year
    acc: dict[tuple[int, str], np.ndarray] = {(y, pa): np.zeros(HOURS) for y in years for pa in pas}
    unshaped: list[dict] = []
    kwh_total = {y: 0.0 for y in years}
    kwh_unshaped = {y: 0.0 for y in years}
    groups: dict[tuple, dict[int, float]] = {}
    from . import run_poc
    for m in run_poc.split_by_baseline_shape(measures).itertuples(index=False):     # a separate 2nd-baseline shape adds its own part
        if float(m.AC_Net_kWh_ScaledProfile_1st_Baseline) == 0 and float(m.AC_Net_kWh_ScaledProfile_2nd_Baseline) == 0:
            continue
        ws = {y: weights_for_year(m, y) for y in years}
        if not any(ws.values()):
            continue
        hit = shape_for(m)
        for y in years:
            kwh_total[y] += ws[y]
        if hit is None:
            unshaped.append({"CEInputID": m.CEInputID, "IOU_AC_Territory": m.IOU_AC_Territory, "AC_Elec_Impact_Profile": m.Used_AC_Elec_Impact_Profile,
                             "AC_Elec_Target_Sector": m.AC_Elec_Target_Sector, "AC_Climate_Zone": m.AC_Climate_Zone, **{f"kwh_{y}": ws[y] for y in years}})
            for y in years:
                kwh_unshaped[y] += ws[y]
            continue
        g = groups.setdefault((m.Used_AC_Elec_Impact_Profile, m.IOU_AC_Territory, m.AC_Elec_Target_Sector, str(m.AC_Climate_Zone)), {y: 0.0 for y in years})
        for y in years:
            g[y] += ws[y]
    for key, wy in groups.items():
        shape = shape_cache[key]
        for y in years:
            if wy[y]:
                acc[(y, key[1])] += wy[y] * shape

    df = pd.DataFrame({"hour_of_year": np.arange(1, HOURS + 1)})
    df.insert(1, "datetime", pd.date_range(f"{real_impact_profiles.acc_calendar_year()}-01-01", periods=HOURS, freq="h"))
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


def combine_profiles(parts: list[tuple[pd.DataFrame, pd.DataFrame, dict]], years: list[int]) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """portfolio_profile's (profile, unshaped, summary) for a whole job from the same for each of its parts (a chunked run): the hourly kWh add up per PA."""
    years = sorted(int(y) for y in years)
    pas = sorted({c.rsplit("_", 1)[1] for p, _u, _s in parts for c in p.columns if c.startswith("net_kwh_") and c.count("_") == 3})
    df = parts[0][0][["hour_of_year", "datetime"]].copy()
    for y in years:
        by_pa = {pa: sum((p[f"net_kwh_{y}_{pa}"].to_numpy() for p, _u, _s in parts if f"net_kwh_{y}_{pa}" in p), np.zeros(HOURS)) for pa in pas}
        df[f"net_kwh_{y}"] = sum(by_pa[pa] for pa in pas) if pas else np.zeros(HOURS)
        for pa in pas:
            df[f"net_kwh_{y}_{pa}"] = by_pa[pa]
    summary = {y: {"delivered_net_kwh": sum(s[y]["delivered_net_kwh"] for _p, _u, s in parts), "in_profile_kwh": float(df[f"net_kwh_{y}"].sum()),
                   "unshaped_kwh": sum(s[y]["unshaped_kwh"] for _p, _u, s in parts), "peak_hour_kwh": float(df[f"net_kwh_{y}"].max()),
                   "peak_hour_of_year": int(df.loc[df[f"net_kwh_{y}"].idxmax(), "hour_of_year"])} for y in years}
    unshaped = [u for _p, u, _s in parts if len(u)]
    return df, (pd.concat(unshaped, ignore_index=True) if unshaped else pd.DataFrame()), summary
