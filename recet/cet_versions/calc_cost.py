"""CalcCost: participant costs, weighted program costs, discounted savings and levelized costs per measure
(CET's OutputCost / SavedCost)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .inputs import PROGRAM_COST_FIELDS
from .releases import Release
from .sql import NaN, dec_pow, divide_or_zero, key, z


def calc_cost(e: pd.DataFrame, s: pd.DataFrame, programs: pd.DataFrame, ce: pd.DataFrame, release: Release,
              me_bens: float, me_cost: float) -> pd.DataFrame:
    F = PROGRAM_COST_FIELDS
    detail = programs.groupby("prg_k", sort=False)[F].sum()          # ProgramCostDetail: every PA, every year, nominal
    detail["SumProgramCosts"] = programs.assign(t=sum(programs[k] for k in F)).groupby("prg_k", sort=False).t.sum()

    E = e.merge(s[["pa_k", "Ra", "Raf", "Rqf"]], on="pa_k", how="inner")
    dqm = dec_pow(E.Rqf, E.Qm)
    q = E.Qty
    inc = E.IncentiveToOthers + E.DILaborCost + E.DIMaterialCost
    adj = np.where(E.RUL > 0, E.UnitMeasureGrossCost - E.MeasIncrCost, 0.0)
    adj_pv = np.where(E.RUL > 0, (E.UnitMeasureGrossCost - E.MeasIncrCost) * ((1 + E.MeasInflation / 4) / E.Rqf) ** E.rulq, 0.0)
    ri = pd.DataFrame({"PrgID": E.PrgID, "CET_ID": E.CET_ID, "NTGRCost": E.NTGRCost,
                       "IncentiveToOthers": q * E.IncentiveToOthers, "DILaborCost": q * E.DILaborCost,
                       "DIMaterialCost": q * E.DIMaterialCost, "EndUserRebate": q * E.EndUserRebate,
                       "Rebates": q * E.EndUserRebate, "IncentsAndDI": q * inc,
                       "RebatesPV": q * E.EndUserRebate / dqm, "IncentsAndDIPV": q * inc / dqm,
                       "GrossMeasCost": q * (E.UnitMeasureGrossCost - adj),
                       "GrossMeasCostPV": q * (E.UnitMeasureGrossCost - adj_pv) / dqm,
                       "RebatesandIncents": q * (inc + E.EndUserRebate),
                       "RebatesandIncentsPV": q * (inc + E.EndUserRebate) / dqm})
    ri = ri.groupby(["PrgID", "CET_ID", "NTGRCost"], sort=False, dropna=False).sum(min_count=1).reset_index()

    P = ce[["CET_ID"]].merge(ri.drop(columns="PrgID"), on="CET_ID", how="left")

    def excess(i, g):  # CASE WHEN i <= g THEN 0 ELSE i - g END (NULL stays NULL)
        return np.where(i.isna() | g.isna(), NaN, np.where(i <= g, 0.0, i - g))

    P["ExcessIncentives"] = excess(P.IncentsAndDI, P.GrossMeasCost)
    P["ExcessIncentivesPV"] = excess(P.IncentsAndDIPV, P.GrossMeasCostPV)
    # CET error kept: the nominal market-effect term adds the discounted excess incentive
    P["MarkEffectPlusExcessInc"] = me_cost * (P.GrossMeasCost + P.ExcessIncentivesPV)
    P["MarkEffectPlusExcessIncPV"] = me_cost * (P.GrossMeasCostPV + P.ExcessIncentivesPV)
    P["GrossParticipantCost"] = P.GrossMeasCost - (P.IncentsAndDI + P.Rebates)
    P["GrossParticipantCostPV"] = P.GrossMeasCostPV - (P.IncentsAndDIPV + P.RebatesPV)
    P["GrossParticipantCostAdjusted"] = P.GrossParticipantCost + P.ExcessIncentives + P.MarkEffectPlusExcessInc
    P["GrossParticipantCostAdjustedPV"] = P.GrossParticipantCostPV + P.ExcessIncentivesPV + P.MarkEffectPlusExcessIncPV
    P["NetParticipantCost"] = P.NTGRCost * (P.GrossParticipantCost + P.ExcessIncentives)
    P["NetParticipantCostPV"] = P.NTGRCost * (P.GrossParticipantCostPV + P.ExcessIncentivesPV)
    P["NetParticipantCostAdjusted"] = P.NetParticipantCost + P.MarkEffectPlusExcessInc
    P["NetParticipantCostAdjustedPV"] = P.NetParticipantCostPV + P.MarkEffectPlusExcessIncPV

    # DiscountedSavings: its nested CASEs have no ELSE, so a measure with no first-baseline kWh (therms) gets NULL, later 0
    U, R, Ra = E.EUL, E.RUL, E.Ra
    pe1, pr1, pr = dec_pow(E.Raf, U - 1), dec_pow(E.Raf, R - 1), dec_pow(E.Raf, R)
    per, per1 = dec_pow(E.Raf, U - R), dec_pow(E.Raf, (U - R) - 1)
    ok = (Ra != 0) & (pe1 != 0) & (z(U) != 0) & (dqm != 0)
    dual = z(R) > 0
    ek, et = E.IRkWh * E.RRkWh, E.IRThm * E.RRThm
    single = lambda f1, n=1.0: n * q * (f1 * (1 + (1 - 1 / pe1) / Ra)) / dqm
    first = lambda f1: f1 * (1 + (1 - 1 / pr1) / Ra)
    # CET's four dual-baseline formulas differ: the gross kWh one discounts the second baseline over EUL - RUL years, the
    # net ones over EUL - RUL - 1, and the gross therm one divides by POWER(Raf, RUL) inside the annuity instead of after it
    ds = pd.DataFrame({"CET_ID": E.CET_ID})
    nk, nt = E.NTGRkWh + me_bens, E.NTGRThm + me_bens
    ds["DiscountedSavingsGrosskWh"] = np.where(dual, q * (first(ek * E.kWh1) + ek * E.kWh2 * (1 + (1 - 1 / per) / Ra) / pr) / dqm, single(ek * E.kWh1))
    ds["DiscountedSavingsNetkWh"] = np.where(dual, nk * q * (first(ek * E.kWh1) + ek * E.kWh2 * (1 + (1 - 1 / per1) / Ra) / pr) / dqm, single(ek * E.kWh1, nk))
    ds["DiscountedSavingsGrossThm"] = np.where(dual, q * (first(et * E.Thm1) + et * E.Thm2 * (1 + (1 - 1 / per1 / pr) / Ra)) / dqm, single(et * E.Thm1))
    ds["DiscountedSavingsNetThm"] = np.where(dual, nt * q * (first(et * E.Thm1) + et * E.Thm2 * (1 + (1 - 1 / per1) / Ra) / pr) / dqm, single(et * E.Thm1, nt))
    for c, base in [("DiscountedSavingsGrosskWh", E.kWh1), ("DiscountedSavingsNetkWh", E.kWh1),
                    ("DiscountedSavingsGrossThm", E.Thm1), ("DiscountedSavingsNetThm", E.Thm1)]:
        ds[c] = np.where(ok & (base != 0), ds[c], NaN)
    ds = ds.groupby("CET_ID", sort=False).sum(min_count=1)

    L = ce.join(ds, on="CET_ID")
    dk, dt = L.DiscountedSavingsNetkWh, L.DiscountedSavingsNetThm
    w = L.WeightedElecAlloc
    lv = pd.DataFrame({"CET_ID": L.CET_ID})
    sct = release.sct != "none"
    for t, b in ([("SCB", "ElecBen_SB"), ("SCH", "ElecBen_SH")] if sct else []) + [("TRC", "ElecBen"), ("PAC", "ElecBen")]:
        lv[f"{t}LifecycleNetBen"] = z(L[b] + L.GasBen) - z(L[f"{t}Cost"])
    if sct:
        # CET error kept: two positional column lists rotate the levelized electric benefits (LevBenElec is ElecBen_SB per kWh)
        lv["LevBenElec"], lv["LevBenElec_SB"], lv["LevBenElec_SH"] = (divide_or_zero(z(L[c]), dk) for c in ["ElecBen_SB", "ElecBen_SH", "ElecBen"])
    else:
        lv["LevBenElec"] = divide_or_zero(z(L.ElecBen), dk)
    lv["LevBenGas"] = divide_or_zero(z(L.GasBen), dt)
    for t in (["SCB", "SCH"] if sct else []) + ["TRC", "PAC"]:
        lv[f"Lev{t}Cost"] = divide_or_zero(z(L[f"{t}Cost"]) * w, dk)
        lv[f"Lev{t}CostNoAdmin"] = divide_or_zero(z(L[f"{t}CostNoAdmin"]) * w, dk)
    lv["LevRIMCost"] = divide_or_zero(z(L.RIMCost) * w, dk)
    for t, b in ([("SCB", "ElecBen_SB"), ("SCH", "ElecBen_SH")] if sct else []) + [("TRC", "ElecBen"), ("PAC", "ElecBen")]:
        lv[f"LevNetBen{t}Elec"] = divide_or_zero(z(L[b]) - z(L[f"{t}Cost"]) * w, dk)
        lv[f"LevNetBen{t}ElecNoAdmin"] = divide_or_zero(z(L[b]) - z(L[f"{t}CostNoAdmin"]) * w, dk)
    for t in ["TRC", "PAC"]:
        lv[f"LevNetBen{t}Gas"] = divide_or_zero(z(L.GasBen) - z(L[f"{t}Cost"]) * (1 - w), dt)
        lv[f"LevNetBen{t}GasNoAdmin"] = divide_or_zero(z(L.GasBen) - z(L[f"{t}CostNoAdmin"]) * (1 - w), dt)
    lv["LevNetBenRIMElec"] = divide_or_zero(z(L.ElecBen) - z(L.RIMCost) * w, dk)
    lv["LevNetBenRIMGas"] = divide_or_zero(z(L.GasBen) - z(L.RIMCost) * (1 - w), dt)

    # the final SELECT: every measure, whether or not its PA has settings (no Version filter)
    O = e[["PA", "PrgID", "CET_ID"]].assign(prg_k=key(e.PrgID))
    O = O.merge(ri.drop(columns=["PrgID", "NTGRCost"]), on="CET_ID", how="left")
    O = O.merge(P.drop(columns=[c for c in ri.columns if c not in ("CET_ID",) and c in P.columns]), on="CET_ID", how="left")
    O = O.merge(ce[["CET_ID", "WeightedBenefits"]], on="CET_ID", how="left").join(detail, on="prg_k")
    for k in F:
        O["Wtd" + k] = O[k] * O.WeightedBenefits
    O["ProgramCosts"] = O.SumProgramCosts * O.WeightedBenefits
    O["TotalExpenditures"] = O.IncentiveToOthers + O.DILaborCost + O.DIMaterialCost + O.EndUserRebate + O.ProgramCosts
    O["GrossMeasureCost"] = O.GrossMeasCost
    O = O.join(ds, on="CET_ID").merge(lv, on="CET_ID", how="left")
    for c in list(ds.columns) + list(lv.columns[1:]):
        O[c] = z(O[c])
    return O
