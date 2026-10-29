"""CalcCE as CET 21.0 has it: electric and gas benefits, TRC / PAC costs, bill impacts and ratios (no supply costs, no
other benefits, no fuel substitution). Its SQL is a different procedure from 22.1's, so it has its own module; the bill
terms and the program-cost sums are the later releases' (the SQL is the same).

Each lifetime part (first-baseline quarters, the fractional quarter, the second-baseline split, quarters and fraction) is
its own LEFT JOIN on CET_ID, so the parts are merged in turn as SQL joins them (a part with two rows for a measure counts
the measure twice, as CET does) and the products summed per measure."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .calc_ce import _program_costs, _source, bills
from .releases import Release
from .sql import dec_pow, divide_or_zero, key, one_over, pos, trunc, z
from .tables import QuarterlyTables


def _elec_view(e: pd.DataFrame, s: pd.DataFrame, t: QuarterlyTables, first_year: int) -> pd.DataFrame:
    """AvoidedCostElecvw: the rebased table joined on PA, end use, climate zone and target sector ('0' = every sector)."""
    cols = ["PA", "Version", "TS", "EU", "CZ", "Qtr", "Qac", "Gen", "TD", "DSType"]
    src = _source(t.ac_elec, cols, first_year, t.base_year, t.max_qac_elec)
    src = src.assign(pa_k=key(src.PA), v_k=key(src.Version.astype(str)), ts_k=key(src.TS.astype(str)), eu_k=key(src.EU), cz_k=key(src.CZ))
    src = src.merge(s[["pa_k", "v_k", "Rqf"]], on=["pa_k", "v_k"])
    im = e.assign(ts_k=key(e.TS), eu_k=key(e.EU), cz_k=key(e.CZ.str.rstrip()))
    v = src.drop(columns=["PA", "TS", "EU", "CZ", "Version"]).merge(
        im.rename(columns={"ts_k": "e_ts_k"}), on=["pa_k", "eu_k", "cz_k"])
    v = v[(v.ts_k == v.e_ts_k) | (v.ts_k == "0")].reset_index(drop=True)
    kw = (v.DSType.astype("string").str.rstrip().str.casefold() == "kw").fillna(False).to_numpy()
    v["DS1"] = np.where(kw, v.kW1, v.kWh1)
    v["DS2"] = np.where(kw, v.kW2, v.kWh2)
    v["d"] = dec_pow(v.Rqf, v.Qac)
    return v


def _gas_view(e: pd.DataFrame, s: pd.DataFrame, t: QuarterlyTables, first_year: int, raw: pd.DataFrame) -> pd.DataFrame:
    """AvoidedCostGasvw: joined on ACGasKey = PA + GasSector + GasSavingsProfile (NULL when any of them is)."""
    cols = ["PA", "Version", "GS", "GP", "Qtr", "Qac", "Total", "ACGasKey"]
    src = _source(t.ac_gas, cols, first_year, t.base_year, t.max_qac_gas)
    src = src.assign(pa_k=key(src.PA), v_k=key(src.Version.astype(str)), gk=key(src.ACGasKey))
    src = src.merge(s[["pa_k", "v_k", "Rqf"]], on=["pa_k", "v_k"])
    gk = key(raw.PA + raw.E3GasSector + raw.E3GasSavProfile)
    im = e.drop(columns=["pa_k"]).assign(gk=gk.to_numpy())
    v = src.drop(columns=["PA", "GS", "GP", "Version", "ACGasKey"]).merge(im, on="gk")
    v["d"] = dec_pow(v.Rqf, v.Qac)
    return v


def _parts(v: pd.DataFrame, values: list[str], ds: bool) -> list[pd.DataFrame]:
    """ac / frac / frac2_1 / ac2 / frac2_2 (gac / gfrac / ...): each measure's rows of each subquery."""
    lo, i1, i2 = v.Qm, trunc(v.Qm + v.eulq1), trunc(v.Qm + v.eulq2)
    f1, f2 = v.eulq1 - trunc(v.eulq1), v.eulq2 - trunc(v.eulq2)
    disc = {c: v[c] / v.d for c in values}

    def grouped(mask, sfx, by):
        r = v.loc[mask]
        g = pd.DataFrame({"CET_ID": r.CET_ID, **{f"{c}{sfx}": disc[c][mask] for c in values}})
        keys = ["CET_ID"] + [b for b in by if b in r.columns]
        for b in by:
            g[b] = r[b]
        out = g.groupby(keys, sort=False, dropna=False)[[f"{c}{sfx}" for c in values]].sum(min_count=1).reset_index()
        for c in values:
            out[f"{c}{sfx}"] = out[f"{c}{sfx}"].fillna(0)
        return out

    def rows(mask, factor, sfx):
        r = pd.DataFrame({"CET_ID": v.CET_ID[mask]})
        for c in values:
            r[f"{c}{sfx}"] = factor[mask] * disc[c][mask]
        return r

    guard = (f1 > 0) & ((v.eulq2 > 0) if ds else True)
    split = pd.Series(np.where(guard, 1 - f1, 0.0), index=v.index)
    at1 = v.Qac == i1
    p = [grouped((v.Qac >= lo) & (v.Qac <= v.Qm + trunc(v.eulq1) - 1), "1", ["DS1", "NTGRkW"] if ds else ["NTGRThm"]),
         rows(at1, f1, "Frac1")]
    s21 = rows(at1, split, "Frac2_1")
    for c in values:          # CASE ... ELSE 0
        s21.loc[~guard[at1].to_numpy(), f"{c}Frac2_1"] = 0.0
    p += [s21,
          grouped((v.Qac >= v.Qm + v.eulq1) & (v.Qac <= v.Qm + v.eulq2 - 1), "2", ["DS2", "NTGRkW"] if ds else ["NTGRThm"]),
          rows(v.Qac == i2, f2, "Frac2_2")]
    for part, d in zip([p[0], p[3]], ["DS1", "DS2"]):
        if d in part.columns:
            part[d] = part[d].fillna(0)
    return [x.drop(columns=[c for c in ("NTGRkW", "NTGRThm") if c in x.columns]) for x in p]


def benefits(e, s, t, raw, first_year, me_bens) -> pd.DataFrame:
    E = e.merge(s[["pa_k", "Rqf", "Raf"]], on="pa_k", how="inner")
    J = E[["CET_ID"]].copy()
    for part in _parts(_elec_view(e, s, t, first_year), ["Gen", "TD"], True) + _parts(_gas_view(e, s, t, first_year, raw), ["Total"], False):
        J = J.merge(part, on="CET_ID", how="left")
    J = J.merge(E, on="CET_ID", how="left")
    mb = me_bens
    nk, nkw, nt = J.NTGRkWh + mb, J.NTGRkW + mb, J.NTGRThm + mb
    k = dec_pow(J.Rqf, J.Qm - 1)
    dq = dec_pow(J.Rqf, J.Qm)
    g1, t1 = J.Gen1 + J.GenFrac1, J.TD1 + J.TDFrac1
    g2 = z(J.Gen2) + z(J.GenFrac2_1) + z(J.GenFrac2_2)
    t2 = z(J.TD2) + z(J.TDFrac2_1) + z(J.TDFrac2_2)
    gas1, gas2 = J.Total1 + z(J.TotalFrac1), z(J.Total2) + z(J.TotalFrac2_1) + z(J.TotalFrac2_2)
    kwh_r = J.IRkWh * J.RRkWh
    r = pd.DataFrame({"PA": J.PA, "PrgID": J.PrgID, "CET_ID": J.CET_ID})
    r["ElecBen"] = k * (J.Qty * ((nk * kwh_r * J.kWh1 * g1 + J.IRkW * J.RRkW * nkw * J.DS1 * t1)
                                 + kwh_r * (nk * J.kWh2 * g2 + nkw * z(J.DS2) * t2)) / dq)
    r["GasBen"] = k * (J.Qty * ((nt * J.IRThm * J.RRThm * J.Thm1 * gas1) + J.IRThm * J.RRThm * (nt * J.Thm2 * gas2)) / dq)
    # CET error kept: the gross benefits multiply the first baseline by (1 + market effects) and leave the kW part unadjusted
    r["ElecBenGross"] = k * (J.Qty * (((1 + mb) * kwh_r * J.kWh1 * g1 + J.DS1 * t1) + (kwh_r * J.kWh2 * g2 + z(J.DS2) * t2)) / dq)
    r["GasBenGross"] = k * (J.Qty * (((1 + mb) * J.IRThm * J.RRThm * J.Thm1 * gas1) + (J.IRThm * J.RRThm * J.Thm2 * gas2)) / dq)
    cols = ["ElecBen", "GasBen", "ElecBenGross", "GasBenGross"]
    ce = r.groupby(["PA", "PrgID", "CET_ID"], sort=False, dropna=False)[cols].sum(min_count=1).reset_index()
    ce[cols] = ce[cols].fillna(0)
    return ce


def costs(ce: pd.DataFrame, e: pd.DataFrame, s: pd.DataFrame, programs: pd.DataFrame, first_year: int, me_cost: float) -> pd.DataFrame:
    pcs = _program_costs(programs, s, first_year)
    E = e.merge(s[["pa_k", "Rqf"]], on="pa_k", how="inner")
    dqm = dec_pow(E.Rqf, E.Qm)
    adj = np.where(E.RUL > 0, (E.UnitMeasureGrossCost - E.MeasIncrCost) * ((1 + E.MeasInflation / 4) / E.Rqf) ** E.rulq, 0.0)
    ri = pd.DataFrame({"CET_ID": E.CET_ID, "RebatesPV": E.Qty * E.EndUserRebate / dqm,
                       "IncentsAndDIPV": E.Qty * (E.IncentiveToOthers + E.DILaborCost + E.DIMaterialCost) / dqm,
                       "GrossMeasCostPV": E.Qty * (E.UnitMeasureGrossCost - adj) / dqm})
    ri = ri.groupby("CET_ID", sort=False).sum(min_count=1).reset_index()

    bs = pd.DataFrame({"PrgID": ce.PrgID, "SumElecBen": pos(ce.ElecBen), "SumGasBen": pos(ce.GasBen)}).groupby("PrgID", sort=False).sum()
    cc = ce.groupby("PrgID", sort=False).size().rename("ClaimCount")
    X = ce.merge(E[["CET_ID", "Qty", "Qm", "Rqf", "IncentiveToOthers", "DILaborCost", "DIMaterialCost", "EndUserRebate", "NTGRCost"]],
                 on="CET_ID", how="inner")
    X = X.assign(prg_k=key(X.PrgID)).merge(pcs, on="prg_k", how="left").merge(ri, on="CET_ID", how="left")
    X = X.join(bs, on="PrgID").join(cc, on="PrgID")
    ex = np.where(X.IncentsAndDIPV <= X.GrossMeasCostPV, 0.0, X.IncentsAndDIPV - X.GrossMeasCostPV)
    ex = pd.Series(ex, index=X.index).where(X.IncentsAndDIPV.notna() & X.GrossMeasCostPV.notna())
    gross_adj = X.GrossMeasCostPV - (X.IncentsAndDIPV + X.RebatesPV) + ex
    net_pc = X.NTGRCost * gross_adj + me_cost * (X.GrossMeasCostPV + ex)
    tot = X.SumElecBen + X.SumGasBen
    w = np.where(tot != 0, (pos(X.ElecBen) + pos(X.GasBen)) / tot.where(tot != 0, 1.0), one_over(X.ClaimCount))
    pac_na = X.Qty * (X.IncentiveToOthers + X.DILaborCost + X.DIMaterialCost + X.EndUserRebate) / dec_pow(X.Rqf, X.Qm)
    sbp = pos(X.ElecBen) + pos(X.GasBen)
    o = {"PACCost": w * z(X.SumCostsNPV) + pac_na}
    o["TRCCost"] = o["PACCost"] + net_pc
    # CET error kept: the calculations CTE names its third column trc_costgross but selects the TRC cost there
    o["TRCCostGross"] = o["TRCCost"]
    o["PACCostNoAdmin"] = pac_na
    o["TRCCostNoAdmin"] = pac_na + net_pc
    o["WeightedBenefits"] = w
    o["WeightedElecAlloc"] = divide_or_zero(pos(X.ElecBen), sbp)
    o["WeightedProgramCost"] = w * z(X.SumCosts)
    return ce.merge(X[["CET_ID"]].assign(**o), on="CET_ID", how="left")


def calc_ce(e, s, programs, t, release: Release, first_year, me_bens, me_cost, raw) -> pd.DataFrame:
    ce = benefits(e, s, t, raw, first_year, me_bens)
    ce = costs(ce, e, s, programs, first_year, me_cost)
    ce = bills(ce, e, s, t, release, me_bens)
    b = ce.ElecBen + ce.GasBen
    return ce.assign(TRCRatio=divide_or_zero(b, ce.TRCCost), PACRatio=divide_or_zero(b, ce.PACCost),
                     TRCRatioNoAdmin=divide_or_zero(b, ce.TRCCostNoAdmin), PACRatioNoAdmin=divide_or_zero(b, ce.PACCostNoAdmin))
