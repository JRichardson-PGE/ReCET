"""CalcSavings: annual, lifecycle and first-year savings per measure and the savings weights (CET's OutputSavings / SavedSavings)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .releases import Release
from .sql import group_sum, one_over

THERM_CONV = 29.307111111


def _weighted(s: pd.DataFrame, kwh: str, thm: str) -> np.ndarray:
    """The savings weight: each measure's share of its program's positive first-year gross kWh + therms (in kWh), else 1/count."""
    k = np.where(s[kwh] > 0, s[kwh], 0.0)
    t = np.where(s[thm] > 0, THERM_CONV * s[thm], 0.0)
    g = pd.DataFrame({"PrgID": s.PrgID, "k": k, "t": t}).groupby("PrgID", sort=False)
    sk, st, n = s.PrgID.map(g.k.sum()), s.PrgID.map(g.t.sum()), s.PrgID.map(g.size())
    return np.where((sk != 0) | (st != 0), (k + t) / (sk + st), one_over(n))


def _calc_savings_2022(e: pd.DataFrame, me_bens) -> pd.DataFrame:
    """22.1 and before: annual, lifecycle, goal-attainment and first-year savings (no water, no site / water split)."""
    q, R, U = e.Qty, e.RUL, e.EUL

    def annual(ir, rr, a1, a2, ntg=1.0):
        dual = np.where(U > 0, q * ntg * (ir * rr * a1 * R + ir * rr * a2 * (U - R)) / U, 0.0)
        return np.where(R > 0, dual, np.where(U > 0, q * ir * rr * ntg * a1, 0.0))

    def lifecycle(ir, rr, a1, a2, ntg=1.0):
        return np.select([(R == 0) & (U >= 1), (R > 0) & (U >= 1), (R == 0) & (U < 1)],
                         [q * ntg * U * ir * rr * a1, q * ntg * U * ir * rr * (a1 * R + a2 * (U - R)) / U, q * ntg * U * ir * rr * a1],
                         q * ntg * ir * rr * (R * a1 + U * a2))

    def goal(ir, rr, a1):
        return np.where(U > 1, 1.0, U) * q * ir * rr * a1

    def first_year(ir, rr, a1, a2, ntg=1.0):
        c1 = np.select([R >= 1, (R > 0) & (R <= 1), (R == 0) & (U >= 1)], [1.0, R, 1.0], 1.0)
        c2 = np.select([(R > 0) & (R <= 1) & (U < 1), (R > 0) & (R <= 1) & ((R + U) >= 1)], [U - R, 1 - R], 0.0)
        return c1 * q * ntg * ir * rr * a1 + c2 * q * ntg * ir * rr * a2

    kwh, kw, thm = (e.IRkWh, e.RRkWh, e.kWh1, e.kWh2), (e.IRkW, e.RRkW, e.kW1, e.kW2), (e.IRThm, e.RRThm, e.Thm1, e.Thm2)
    nk, nkw, nt = e.NTGRkWh + me_bens, e.NTGRkW + me_bens, e.NTGRThm + me_bens
    s = pd.DataFrame({
        "PA": e.PA, "PrgID": e.PrgID, "CET_ID": e.CET_ID,
        "GrossKWh": annual(*kwh), "GrossKW": annual(*kw), "GrossThm": annual(*thm),
        "NetKWh": annual(*kwh, nk), "NetKW": annual(*kw, nkw), "NetThm": annual(*thm, nt),
        "LifecycleGrossKWh": lifecycle(*kwh), "LifecycleGrossThm": lifecycle(*thm),
        "LifecycleNetKWh": lifecycle(*kwh, nk), "LifecycleNetThm": lifecycle(*thm, nt),
        "GoalAttainmentKWh": goal(*kwh[:3]), "GoalAttainmentKW": goal(*kw[:3]), "GoalAttainmentThm": goal(*thm[:3]),
        "FirstYearGrossKWh": first_year(*kwh), "FirstYearGrossKW": first_year(*kw), "FirstYearGrossThm": first_year(*thm),
        "FirstYearNetKWh": first_year(*kwh, nk), "FirstYearNetKW": first_year(*kw, nkw), "FirstYearNetThm": first_year(*thm, nt),
    })
    cols = [c for c in s.columns if c not in ("PA", "PrgID", "CET_ID")]
    s = group_sum(s, ["PA", "PrgID", "CET_ID"], cols)
    s["WeightedSavings"] = _weighted(s, "FirstYearGrossKWh", "FirstYearGrossThm")
    return s


def calc_savings(e: pd.DataFrame, release: Release, me_bens: float) -> pd.DataFrame:
    if release.savings == "2022":
        return _calc_savings_2022(e, me_bens)
    q, R, U = e.Qty, e.RUL, e.EUL
    single = R.fillna(0) == 0

    def annual(ir, rr, a1, a2, ntg=1.0):
        return np.where(single, q * ntg * ir * rr * a1, q * ntg * ir * rr * (a1 * R + a2 * (U - R)) / U)

    def lifecycle(ir, rr, a1, a2, ntg=1.0, eul_twice=False):
        # CET error kept (24.1): the net kWh lifecycle savings of a dual-baseline measure are multiplied by EUL once more
        dual = q * ntg * ir * rr * (a1 * R + a2 * (U - R)) * (U if eul_twice else 1.0)
        return np.where(single, q * ntg * U * ir * rr * a1, dual)

    def first_year(ir, rr, a1, a2, ntg=1.0, rul_ge1=False, water2=False):
        # CET's tests differ by a hair between columns (RUL >= 1 / RUL > 1, RUL <= 1 / RUL < 1); copied as written
        c1 = np.select([(R >= 1) if rul_ge1 else (R > 1), (R > 0) & (R < 1), (R == 0) & (U < 1)], [1, R, U], 1)
        c2 = np.select([(R > 0) & ((R <= 1) if water2 else (R < 1)) & (U < 1), (R > 0) & (R < 1) & (U >= 1)], [U - R, 1 - R], 0)
        return c1 * q * ntg * ir * rr * a1 + c2 * q * ntg * ir * rr * a2

    kwh = (e.IRkWh, e.RRkWh)
    tot1, tot2 = e.kWh1 + e.kWhWater1, e.kWh2 + e.kWhWater2
    s = {"PA": e.PA, "PrgID": e.PrgID, "CET_ID": e.CET_ID}
    nets = {"Gross": (1.0, 1.0, 1.0), "Net": (e.NTGRkWh + me_bens, e.NTGRkW + me_bens, e.NTGRThm + me_bens)}
    for pre, (a, b, c) in nets.items():
        s[f"Annual{pre}kWh"] = annual(*kwh, tot1, tot2, a)
        s[f"Annual{pre}kWhSite"] = annual(*kwh, e.kWh1, e.kWh2, a)
        s[f"Annual{pre}kWhWater"] = annual(*kwh, e.kWhWater1, e.kWhWater2, a)
        s[f"Annual{pre}kW"] = annual(e.IRkW, e.RRkW, e.kW1, e.kW2, b)
        s[f"Annual{pre}Thm"] = annual(e.IRThm, e.RRThm, e.Thm1, e.Thm2, c)
    for pre, (a, b, c) in nets.items():
        twice = release.savings_dual_eul_error and pre == "Net"
        s[f"Lifecycle{pre}kWh"] = lifecycle(*kwh, tot1, tot2, a, twice)
        s[f"Lifecycle{pre}kWhSite"] = lifecycle(*kwh, e.kWh1, e.kWh2, a, twice)
        s[f"Lifecycle{pre}kWhWater"] = lifecycle(*kwh, e.kWhWater1, e.kWhWater2, a, twice)
        s[f"Lifecycle{pre}Thm"] = lifecycle(e.IRThm, e.RRThm, e.Thm1, e.Thm2, c)
    for pre, (a, b, c) in nets.items():
        gross = pre == "Gross"
        s[f"FirstYear{pre}kWh"] = first_year(*kwh, tot1, tot2, a, rul_ge1=gross)
        s[f"FirstYear{pre}kWhSite"] = first_year(*kwh, e.kWh1, e.kWh2, a)
        s[f"FirstYear{pre}kWhWater"] = first_year(*kwh, e.kWhWater1, e.kWhWater2, a, water2=gross)
        s[f"FirstYear{pre}kW"] = first_year(e.IRkW, e.RRkW, e.kW1, e.kW2, b)
        s[f"FirstYear{pre}Thm"] = first_year(e.IRThm, e.RRThm, e.Thm1, e.Thm2, c)
    s = pd.DataFrame(s)
    cols = [c for c in s.columns if c not in ("PA", "PrgID", "CET_ID")]
    s = group_sum(s, ["PA", "PrgID", "CET_ID"], cols)
    s["WeightedSavings"] = _weighted(s, "FirstYearGrosskWh", "FirstYearGrossThm")
    return s
