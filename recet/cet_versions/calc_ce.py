"""CalcCE: benefits, supply costs, the cost tests' costs, bill impacts and ratios per measure (CET's OutputCE / SavedCE)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .inputs import PROGRAM_COST_FIELDS, version_key
from .releases import Release
from .sql import dec_pow, divide_or_zero, group_sum, key, one_over, pos, trunc, z
from .tables import QuarterlyTables

ELEC = ["Gen", "TD"]
SCT_SFX = ["_SB", "_SH"]


def _source(ac: pd.DataFrame, cols: list[str], first_year: int, base_year: int, max_qac: int) -> pd.DataFrame:
    """AvoidedCostSource*vw: the table rebased to FirstYear, UNION its last (FirstYear - BaseYear) years unshifted (padding)."""
    cols = [c for c in cols if c in ac.columns]
    qtrs = max((first_year - base_year) * 4, 0)
    if qtrs == 0:
        return ac[cols].drop_duplicates()
    a = ac.loc[ac.Qac > qtrs - 1, cols].copy()
    a["Qac"] = a.Qac - qtrs
    b = ac.loc[ac.Qac > max_qac - qtrs, cols]
    return pd.concat([a, b]).drop_duplicates()


def _discount(df: pd.DataFrame, release: Release, societal: bool) -> np.ndarray:
    if societal and release.sct == "full":
        return df.Rqf_SCT.to_numpy() ** df.Qac.to_numpy()
    return dec_pow(df.Rqf, df.Qac)


def _lifetime_sums(v: pd.DataFrame, values: list[str], keys: list[str], release: Release) -> tuple[pd.DataFrame, pd.DataFrame]:
    """ace_1 / ace_2 (acg_1 / acg_2): each measure's discounted quarters over its first and second baseline lives.

    `v` holds the view rows with their discounted values in `values` and the demand columns DS1 / DS2 (electric only).
    Each part is a UNION (duplicate rows dropped) grouped on CET_ID and, for electric, DS. Before 24.1 the second life's
    first quarter is 0 unless the first life ends inside a quarter (electric: and there is a second life), and the
    electric middle quarters start at Qm + eulq1 (not truncated)."""
    lo, i1, i2 = v.Qm, v.Qm + trunc(v.eulq1), v.Qm + trunc(v.eulq2)
    f1 = v.eulq1 - np.floor(v.eulq1)
    f2 = v.eulq2 - np.floor(v.eulq2)
    elec = "DS1" in v.columns

    def part(mask, factor=None, isnull0=False, ds=None):
        r = v.loc[mask]
        out = pd.DataFrame({"CET_ID": r.CET_ID})
        for c in values:
            x = r[c] if factor is None else factor[mask] * r[c]
            out[c] = x.fillna(0) if isnull0 else x
        if elec:
            out["DS"] = ds(r)
        return out

    at1 = v.Qac == i1
    first = [part((v.Qac >= lo) & (v.Qac <= i1 - 1), isnull0=True, ds=lambda r: r.DS1.fillna(0)),
             part(at1, factor=f1, ds=lambda r: r.DS1)]
    if release.lifetime_sums == "guarded":
        ok = (f1 > 0) & ((v.eulq2 > 0) if elec else True)
        split = part(at1, factor=pd.Series(np.where(ok, 1 - f1, 0.0), index=v.index), ds=lambda r: r.DS2.fillna(0))
        for c in values:          # CASE ... ELSE 0: a NULL cost still gives 0 when the guard fails
            split.loc[~ok[at1].to_numpy(), c] = 0.0
        middle = (v.Qac >= v.Qm + v.eulq1) if elec else (v.Qac >= i1 + 1)
    else:
        split = part(at1, factor=1 - f1, ds=lambda r: r.DS2.fillna(0))
        middle = v.Qac >= i1 + 1
    upper = (v.Qac <= v.Qm + v.eulq2 - 1) if (elec and release.lifetime_sums == "guarded") else (v.Qac <= i2 - 1)
    second = [split,
              part(middle & upper, isnull0=True, ds=lambda r: r.DS2.fillna(0)),
              part(v.Qac == i2, factor=f2, ds=lambda r: r.DS2.fillna(0))]
    return (group_sum(pd.concat(first).drop_duplicates(), keys, values),
            group_sum(pd.concat(second).drop_duplicates(), keys, values))


def _suffixed(df: pd.DataFrame, sfx: str) -> pd.DataFrame:
    return df.rename(columns={c: c + sfx for c in df.columns if c != "CET_ID"})


def benefits(e: pd.DataFrame, s: pd.DataFrame, t: QuarterlyTables, release: Release, first_year: int,
             me_bens: float, me_cost: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The first INSERT INTO #OutputCE: benefits and supply costs per measure. Returns (#OutputCE, the joined measure rows)."""
    sct_e = release.sct in ("elec", "full")
    sct_g = release.sct == "full"
    se = s[["pa_k", "v_k", "Version", "Raf", "Rqf", "Raf_SCT", "Rqf_SCT"]]
    base_year = t.base_year

    # AvoidedCostElecvw
    ecols = ["PA", "Version", "TS", "EU", "CZ", "Qtr", "Qac", "Gen", "TD"] + (["Gen_SB", "TD_SB", "Gen_SH", "TD_SH"] if sct_e else []) + ["DSType", "ACElecKey"]
    src = _source(t.ac_elec, ecols, first_year, base_year, t.max_qac_elec)
    src = src.assign(pa_k=key(src.PA), ts_k=key(src.TS), eu_k=key(src.EU), cz_k=key(src.CZ), v_k=version_key(src.Version))
    im = e.merge(se, on="pa_k", how="inner")
    im = im.assign(ts_k=key(im.TS), eu_k=key(im.EU), cz_k=key(im.CZ))
    av = im.merge(src.drop(columns=["PA", "TS", "EU", "CZ", "Version"]), on=["pa_k", "ts_k", "eu_k", "cz_k", "v_k"])
    av = av[(av.Qac >= av.Qm) & (av.Qac <= av.Qm + trunc(av.eulq))].reset_index(drop=True)
    if release.sct_elec_versions:
        other = ~version_key(av.Version).isin([v.casefold() for v in release.sct_elec_versions])
        for c in ["Gen_SB", "Gen_SH", "TD_SB", "TD_SH"]:
            av.loc[other, c] = 0.0
    kw = (av.DSType.astype("string").str.rstrip().str.casefold() == "kw").fillna(False).to_numpy()
    av["DS1"] = np.where(kw, av.kW1, av.kWh1)
    av["DS2"] = np.where(kw, av.kW2, av.kWh2)
    trc, soc = _discount(av, release, False), _discount(av, release, True)
    evals = ["Gen", "TD"] + ([f"{b}{x}" for x in SCT_SFX for b in ELEC] if sct_e else [])
    for c in evals:
        av[c] = av[c] / (soc if c.endswith(("_SB", "_SH")) else trc)
    ace1, ace2 = _lifetime_sums(av, evals, ["CET_ID", "DS"], release)

    # AvoidedCostGasvw
    gvals = ["Total"] + (["Total_SB", "Total_SH"] if sct_g else [])
    gcols = ["ID", "PA", "Version", "FirstYear", "GS", "GP", "ComboField", "Qtr", "Qac", "Commodity", "TD", "Total", "ACGasKey"] + (
        ["Commodity_SB", "TD_SB", "Total_SB", "Commodity_SH", "TD_SH", "Total_SH"] if sct_g else [])
    gsrc = _source(t.ac_gas, gcols, first_year, base_year, t.max_qac_gas)
    gsrc = gsrc.assign(pa_k=key(gsrc.PA), gs_k=key(gsrc.GS), gp_k=key(gsrc.GP), v_k=version_key(gsrc.Version))
    img = e.merge(se, on="pa_k", how="inner")
    img = img.assign(gs_k=key(img.GS), gp_k=key(img.GP))
    gv = img.merge(gsrc[["pa_k", "gs_k", "gp_k", "v_k", "Qac"] + gvals], on=["pa_k", "gs_k", "gp_k", "v_k"])
    gv = gv[(gv.Qac >= gv.Qm) & (gv.Qac <= gv.Qm + trunc(gv.eulq))].reset_index(drop=True)
    trc, soc = _discount(gv, release, False), _discount(gv, release, True)
    gv["Gas"] = gv.Total / trc
    names = ["Gas"]
    if sct_g:
        gv["Gas_SB"], gv["Gas_SH"] = gv.Total_SB / soc, gv.Total_SH / soc
        names += ["Gas_SB", "Gas_SH"]
    acg1, acg2 = _lifetime_sums(gv[["CET_ID", "Qac", "Qm", "eulq1", "eulq2"] + names], names, ["CET_ID"], release)

    E = e.merge(se[["pa_k"]], on="pa_k", how="inner")          # WHERE s.Version = @AVCVersion
    for part, sfx in [(ace1, "_1"), (ace2, "_2"), (acg1, "_1"), (acg2, "_2")]:
        E = E.merge(_suffixed(part, sfx), on="CET_ID", how="left")
    not_fs, is_fs = E.not_fs.to_numpy(), E.is_fs.to_numpy()
    nk, nkw, nth = E.NTGRkWh + me_bens, E.NTGRkW + me_bens, E.NTGRThm + me_bens
    out = {"PA": E.PA, "PrgID": E.PrgID, "CET_ID": E.CET_ID}
    q = E.Qty

    def where(cond, x):
        return np.where(cond, z(x), 0.0)

    for sfx in [""] + (SCT_SFX if sct_e else []):
        g1, g2, t1, t2 = E[f"Gen{sfx}_1"], E[f"Gen{sfx}_2"], E[f"TD{sfx}_1"], E[f"TD{sfx}_2"]
        d1, d2 = E.DS_1, E.DS_2
        priced = E.kWh1 * g1 + E.kWh2 * z(g2)
        sign = priced if release.sign_test == "priced" else E.kWh1
        cond = not_fs | (sign > 0)
        # CET error kept: the net benefit multiplies kWh2 by ace_2.Gen without ISNULL, so a measure whose second-baseline
        # sum is missing (life past the table's end, or RUL > EUL) gets no net electric benefit at all
        out[f"ElecBen{sfx}"] = where(cond, q * (nk * E.IRkWh * E.RRkWh * (E.kWh1 * g1 + E.kWh2 * g2) + nkw * E.IRkW * E.RRkW * (d1 * t1 + z(d2) * z(t2))))
        out[f"ElecBenGross{sfx}"] = where(cond, q * (E.IRkWh * E.RRkWh * priced + E.IRkW * E.RRkW * (d1 * t1 + z(d2) * z(t2))))
        scond = is_fs & (sign < 0)
        out[f"ElecSupplyCost{sfx}"] = where(scond, -q * (nk * E.IRkWh * E.RRkWh * priced + nkw * E.IRkW * E.RRkW * (d1 * t1 + d2 * z(t2))))
        out[f"ElecSupplyCostGross{sfx}"] = where(scond, -q * (E.IRkWh * E.RRkWh * priced + E.IRkW * E.RRkW * (d1 * t1 + d2 * z(t2))))
        if sfx == "" or sct_g:
            wnet = q * nk * E.IRkWh * E.RRkWh * (E.kWhWater1 * g1 + E.kWhWater2 * g2)
            wgross = q * E.IRkWh * E.RRkWh * (E.kWhWater1 * g1 + E.kWhWater2 * g2)
            out[f"WaterEnergyBen{sfx}"] = where(E.kWhWater1 > 0, wnet)
            out[f"WaterEnergyBenGross{sfx}"] = where(E.kWhWater1 > 0, wgross)
            out[f"WaterEnergyCost{sfx}"] = where(E.kWhWater1 < 0, -wnet)
            out[f"WaterEnergyCostGross{sfx}"] = where(E.kWhWater1 < 0, -wgross)
    for sfx in [""] + (SCT_SFX if sct_g else []):
        gg1, gg2 = E[f"Gas{sfx}_1"], E[f"Gas{sfx}_2"]
        priced = E.Thm1 * gg1 + E.Thm2 * z(gg2)
        sign = priced if release.sign_test == "priced" else E.Thm1
        cond, scond = not_fs | (sign > 0), is_fs & (sign < 0)
        out[f"GasBen{sfx}"] = where(cond, q * nth * E.IRThm * E.RRThm * priced)
        out[f"GasBenGross{sfx}"] = where(cond, q * E.IRThm * E.RRThm * priced)
        out[f"GasSupplyCost{sfx}"] = where(scond, -q * nth * E.IRThm * E.RRThm * priced)
    if release.tax_credits:
        out["TaxCredits"] = q * nk * z(E.UnitTaxCredits)
        out["TaxCreditsGross"] = q * z(E.UnitTaxCredits)
    other_b = z(E.UnitGasInfraBens) + z(E.UnitRefrigBens) + (z(E.UnitMiscBens) if release.misc_other else 0.0)
    other_c = z(E.UnitRefrigCosts) + (z(E.UnitMiscCosts) if release.misc_other else 0.0)
    out["OtherBen"], out["OtherBenGross"] = q * nk * other_b, q * other_b
    out["OtherCost"], out["OtherCostGross"] = q * (E.NTGRkWh + me_cost) * other_c, q * other_c
    r = pd.DataFrame(out)
    cols = [c for c in r.columns if c not in ("PA", "PrgID", "CET_ID")]
    ce = group_sum(r, ["PA", "PrgID", "CET_ID"], cols)
    ce[cols] = ce[cols].fillna(0)
    # CET error kept: the gross gas supply costs are set from the net ones (GasSupplyCostGross = ISNULL(GasSupplyCost, 0))
    for sfx in [""] + (SCT_SFX if sct_g else []):
        ce[f"GasSupplyCostGross{sfx}"] = ce[f"GasSupplyCost{sfx}"]
    return ce, E


def _program_costs(c: pd.DataFrame, s: pd.DataFrame, first_year: int) -> pd.DataFrame:
    """ProgramCosts / ProgramCostsSum: the program costs per PrgID (over every PA and year) and their present values."""
    c = c.merge(s[["pa_k", "Raf", "Raf_SCT"]], on="pa_k", how="inner")
    c = c.assign(tot=sum(c[k] for k in PROGRAM_COST_FIELDS))
    g = c.groupby(["prg_k", "Year", "Raf", "Raf_SCT"], sort=False).tot.sum().reset_index()
    g["SumCostsNPV"] = g.tot / dec_pow(g.Raf, g.Year - first_year)
    return g.groupby("prg_k", sort=False).agg(SumCosts=("tot", "sum"), SumCostsNPV=("SumCostsNPV", "sum")).reset_index()


def costs(ce: pd.DataFrame, e: pd.DataFrame, s: pd.DataFrame, programs: pd.DataFrame, release: Release, first_year: int,
          me_cost: float) -> pd.DataFrame:
    """'Updating SC, TRC, and PAC costs': program-cost shares, participant costs and each test's cost."""
    sct_e, sct_g = release.sct in ("elec", "full"), release.sct == "full"
    pcs = _program_costs(programs, s, first_year)
    E = e.merge(s[["pa_k", "Raf", "Rqf", "Raf_SCT", "Rqf_SCT"]], on="pa_k", how="inner")
    dqm = dec_pow(E.Rqf, E.Qm)
    inc = E.IncentiveToOthers + E.DILaborCost + E.DIMaterialCost

    def gross_cost_pv(rqf, dq):
        adj = np.where(E.RUL > 0, (E.UnitMeasureGrossCost - E.MeasIncrCost) * ((1 + E.MeasInflation / 4) / rqf) ** E.rulq, 0.0)
        return E.Qty * (E.UnitMeasureGrossCost - adj) / dq

    ri = pd.DataFrame({"PrgID": E.PrgID, "CET_ID": E.CET_ID, "RebatesPV": E.Qty * E.EndUserRebate / dqm,
                       "IncentsAndDIPV": E.Qty * inc / dqm, "GrossMeasCostPV": gross_cost_pv(E.Rqf, dqm)})
    if sct_g:
        dqm_s = E.Rqf_SCT ** E.Qm
        ri["RebatesPV_SCT"] = E.Qty * E.EndUserRebate / dqm_s
        ri["IncentsAndDIPV_SCT"] = E.Qty * inc / dqm_s
        ri["GrossMeasCostPV_SCT"] = gross_cost_pv(E.Rqf_SCT, dqm_s)
    ri = ri.groupby(["PrgID", "CET_ID"], sort=False).sum(min_count=1).reset_index().drop(columns="PrgID")

    # BenefitsSum (per PrgID) and ClaimCount
    ben_cols = [c for c in ce.columns if ("Ben" in c and "Bill" not in c) or c.startswith("TaxCredits")]
    bs = pd.DataFrame({"PrgID": ce.PrgID, **{"Sum" + c: pos(ce[c]) for c in ben_cols}}).groupby("PrgID", sort=False).sum()
    if release.tax_credits:
        # CET error kept: BenefitsSum's column list is out of step with its SELECT (CTE names are positional), so
        # SumElecBenGross holds the positive TaxCreditsGross
        bs["SumElecBenGross"] = bs.SumTaxCreditsGross
    cc = ce.groupby("PrgID", sort=False).size().rename("ClaimCount")

    X = ce.merge(e[["CET_ID", "pa_k", "Qty", "Qm", "IncentiveToOthers", "DILaborCost", "DIMaterialCost", "EndUserRebate", "NTGRCost"]],
                 on="CET_ID", how="left")
    X = X.merge(s[["pa_k", "Rqf", "Rqf_SCT"]], on="pa_k", how="inner")
    X = X.assign(prg_k=key(X.PrgID)).merge(pcs, on="prg_k", how="left").merge(ri, on="CET_ID", how="left")
    X = X.join(bs, on="PrgID").join(cc, on="PrgID")

    def excess(i, g):
        return np.where((i - g) > 0, i - g, 0.0)

    ex = excess(X.IncentsAndDIPV, X.GrossMeasCostPV)
    gross_pc = X.GrossMeasCostPV - (X.IncentsAndDIPV + X.RebatesPV) + ex
    net_pc = X.NTGRCost * gross_pc + me_cost * (X.GrossMeasCostPV + ex)
    if sct_g:
        ex_s = excess(X.IncentsAndDIPV_SCT, X.GrossMeasCostPV_SCT)
        gross_pc_s = X.GrossMeasCostPV_SCT - (X.IncentsAndDIPV_SCT + X.RebatesPV_SCT) + ex_s
        net_pc_s = X.NTGRCost * gross_pc_s + me_cost * (X.GrossMeasCostPV_SCT + ex_s)
    else:
        gross_pc_s, net_pc_s = gross_pc, net_pc

    even = one_over(X.ClaimCount)

    def share(test_sum, parts, sums):
        den = sum(X[c] for c in sums)
        num = sum(pos(X[c]) for c in parts)
        return np.where(test_sum != 0, num / np.where(den != 0, den, 1.0), even)

    def parts(sfx, gross=False):
        g = "Gross" if gross else ""
        gs = sfx if sct_g else ""
        return [f"ElecBen{g}{sfx}", f"GasBen{g}{gs}", f"WaterEnergyBen{g}{gs}", f"OtherBen{g}"]

    def sums(sfx, gross=False):
        return ["Sum" + c for c in parts(sfx, gross)]

    trc_sum = sum(X[c] for c in sums(""))
    W = share(trc_sum, parts(""), sums(""))
    # CET error kept: PAC's test adds the measure's own water and other benefits to the program's electric and gas sums
    W_pac = share(X.SumElecBen + X.SumGasBen + X.WaterEnergyBen + X.OtherBen, parts(""), sums(""))
    W_g = share(sum(X[c] for c in sums("", True)), parts("", True), sums("", True))
    npv = z(X.SumCostsNPV)
    pa_inc = X.Qty * (X.IncentiveToOthers + X.DILaborCost + X.DIMaterialCost + X.EndUserRebate)
    inc_pv = pa_inc / dec_pow(X.Rqf, X.Qm)
    inc_pv_s = pa_inc / (X.Rqf_SCT ** X.Qm) if sct_g else inc_pv

    def supply(sfx):
        gs = sfx if sct_g else ""
        return X[f"ElecSupplyCost{sfx}"] + X[f"GasSupplyCost{gs}"] + X[f"WaterEnergyCost{gs}"]

    o = pd.DataFrame({"CET_ID": X.CET_ID})
    o["TotalSystemBenefit"] = (X.ElecBen + X.GasBen + X.WaterEnergyBen + X.OtherBen) - (supply("") + X.OtherCost)
    o["TotalSystemBenefitGross"] = (X.ElecBenGross + X.GasBenGross + X.WaterEnergyBenGross + X.OtherBenGross) - (
        X.ElecSupplyCostGross + X.GasSupplyCostGross + X.WaterEnergyCostGross + X.OtherCostGross)
    o["PACCost"] = W_pac * npv + inc_pv + supply("") + X.OtherCost
    o["PACCostNoAdmin"] = inc_pv + supply("") + X.OtherCost
    o["TRCCost"] = W * npv + inc_pv + net_pc + supply("") + X.OtherCost
    o["TRCCostGross"] = W_g * npv + inc_pv + gross_pc + supply("") + X.OtherCostGross
    o["TRCCostNoAdmin"] = inc_pv + net_pc + supply("") + X.OtherCost
    if sct_e:
        for t, sfx in [("SCB", "_SB"), ("SCH", "_SH")]:
            Wt = share(sum(X[c] for c in sums(sfx)), parts(sfx), sums(sfx))
            Wtg = share(sum(X[c] for c in sums(sfx, True)), parts(sfx, True), sums(sfx, True))
            # the societal tests' program-cost share is of the TRC present value (SumCostsNPV), in every release
            o[f"{t}Cost"] = Wt * npv + inc_pv_s + net_pc_s + supply(sfx) + X.OtherCost
            o[f"{t}CostGross"] = Wtg * npv + inc_pv_s + gross_pc_s + supply(sfx) + X.OtherCostGross
            o[f"{t}CostNoAdmin"] = inc_pv_s + net_pc_s + supply(sfx) + X.OtherCost
    o["WeightedBenefits"] = W
    # BenPos: SumBenPos is the positive TRC benefits
    sbp = pos(X.ElecBen) + pos(X.GasBen) + pos(X.WaterEnergyBen) + pos(X.OtherBen)
    o["WeightedElecAlloc"] = divide_or_zero(pos(X.ElecBen), sbp)
    o["WeightedProgramCost"] = W * z(X.SumCosts)
    return ce.merge(o, on="CET_ID", how="left")


def _rate_view(e: pd.DataFrame, s: pd.DataFrame, rates: pd.DataFrame, mapping: pd.DataFrame, release: Release, fuel: str) -> pd.DataFrame:
    """E3RateScheduleElecvw / E3RateScheduleGasvw: each measure's rates by year, from its mapped schedule (or its own)."""
    rcol = "RateE" if fuel == "elec" else "RateG"
    E = e.merge(s[["pa_k", "v_k", "Raf"]], on="pa_k", how="inner")
    if fuel == "elec":
        E = E[version_key(E.AVCVersion) == E.v_k]
    r = rates.assign(pa_k=key(rates.PA), v_k=version_key(rates.Version), sch_k=key(rates.Schedule),
                     Qy=pd.to_numeric(rates.Qy), **{rcol: pd.to_numeric(rates[rcol])})
    m = mapping.assign(pa_k=key(mapping.PA), v_k=version_key(mapping.Version))
    if fuel == "elec":
        m = m.assign(seg_k=key(m.TargetSector), sch_k=key(m.Schedule))
        own, seg = "ElecRateSchedule", key(E.TS)
        if not release.elec_rate_by_target_sector:
            seg = seg.where(E.pa_k.isin(["pge", "sdge"]), "all")
    else:
        m = m.assign(seg_k=key(m.GasSector), sch_k=key(m.GasRateSchedule))
        own, seg = "GasRateSchedule", key(E.GS)
    E = E.assign(seg_k=seg.to_numpy())
    named_ok = release.gas_named_schedule or fuel == "elec"
    everyone = not named_ok and not release.gas_named_excluded
    mapped = E[(E[own].fillna("") == "") | everyone].merge(m[["pa_k", "v_k", "seg_k", "sch_k"]], on=["pa_k", "v_k", "seg_k"])
    parts = [mapped]
    if named_ok:
        named = E[E[own].fillna("") != ""]
        parts.append(named.assign(sch_k=key(named[own])))
    v = pd.concat(parts).merge(r[["pa_k", "v_k", "sch_k", "Qy", rcol]], on=["pa_k", "v_k", "sch_k"])
    yr = v.Qtr.str[:4].astype(int)
    v["Yr1"] = yr - release.rate_yr1_base if release.rate_yr1_base is not None else v.Qy_m
    return v[["CET_ID", "Raf", "Qy", "sch_k", rcol, "Yr1", "eul1", "eul2", "EUL"]].drop_duplicates()


def _bill_terms(v: pd.DataFrame, rcol: str) -> dict[str, pd.DataFrame]:
    """RE / REfrac1 / REfrac2_1 / RE2 / REfrac2_2 (and the gas ones): each measure's discounted rates over its lives."""
    out = {}
    a = v[(v.Qy >= v.Yr1) & (v.Qy <= v.Yr1 + trunc(v.eul1) - 1)]
    out["1"] = a.assign(x=a[rcol] / dec_pow(a.Raf, a.Qy - a.Yr1)).groupby(["CET_ID", "eul1"], sort=False).x.sum().reset_index()
    b = v[v.Qy == trunc(v.Yr1 + v.eul1)]
    # CET error kept: the fractional years are discounted to the table's first year (POWER(Raf, Qy)), not the claim year
    f = b.eul1 - trunc(b.eul1)
    out["Frac1"] = b.assign(x=f * b[rcol] / dec_pow(b.Raf, b.Qy)).drop_duplicates(["CET_ID", "eul1", rcol, "Raf", "Qy", "x"])
    out["Frac2_1"] = b.assign(x=np.where((b.eul2 > 0) & (f > 0), (1 - f) * (b[rcol] / dec_pow(b.Raf, b.Qy)), 0.0)).drop_duplicates(
        ["CET_ID", "eul1", "eul2", rcol, "Raf", "Qy", "x"])
    c = v[(v.Qy >= v.Yr1 + v.eul1) & (v.Qy <= v.Yr1 + v.eul2 - 1)]
    out["2"] = c.assign(x=c[rcol] / dec_pow(c.Raf, c.Qy)).groupby("CET_ID", sort=False).x.sum().reset_index()
    d = v[v.Qy == trunc(v.Yr1 + v.eul2)]
    out["Frac2_2"] = d.assign(x=(d.eul2 - trunc(d.eul2)) * (d[rcol] / dec_pow(d.Raf, d.Qy)))
    return {k: t[["CET_ID", "x"]] for k, t in out.items()}


def bills(ce: pd.DataFrame, e: pd.DataFrame, s: pd.DataFrame, t: QuarterlyTables, release: Release, me_bens: float) -> pd.DataFrame:
    """'Calculating RIM Cost': lifetime bill reductions (and, from 25.1, increases) and the RIM cost."""
    e = e.assign(Qy_m=e.Qy)
    E = e.merge(s[["pa_k"]], on="pa_k", how="inner")
    for fuel, rates, mapping, rcol in [("elec", t.rates_elec, t.rate_map_elec, "RateE"), ("gas", t.rates_gas, t.rate_map_gas, "RateG")]:
        terms = _bill_terms(_rate_view(e.drop(columns="Qy"), s, rates, mapping, release, fuel), rcol)
        for k, part in terms.items():
            E = E.merge(part.rename(columns={"x": rcol + k}), on="CET_ID", how="left")
    el = E.kWh1 * (z(E.RateE1) + z(E.RateEFrac1)) + E.kWh2 * (z(E.RateEFrac2_1) + z(E.RateE2) + z(E.RateEFrac2_2))
    gs = E.Thm1 * (z(E.RateG1) + z(E.RateGFrac1)) + E.Thm2 * (z(E.RateGFrac2_1) + z(E.RateG2) + z(E.RateGFrac2_2))
    be = z(E.Qty * E.IR * E.RR * (E.NTGRkWh + me_bens) * el)
    bg = z(E.Qty * E.IR * E.RR * (E.NTGRThm + me_bens) * gs)
    b = pd.DataFrame({"CET_ID": E.CET_ID})
    if release.bills == "split":
        b["BillReducElec"] = np.where(E.not_fs | (el > 0), be, 0.0)
        b["BillIncrElec"] = np.where(E.is_fs & (el < 0), -be, 0.0)
        b["BillReducGas"] = np.where(E.not_fs | (gs > 0), bg, 0.0)
        b["BillIncrGas"] = np.where(E.is_fs & (gs < 0), -bg, 0.0)
    else:
        b["BillReducElec"], b["BillReducGas"] = be, bg
    b = b.groupby("CET_ID", sort=False).sum().reset_index()
    ce = ce.merge(b, on="CET_ID", how="left")
    ce["RIMCost"] = ce.BillReducElec + ce.BillReducGas + ce.PACCost
    ce["RIMCostNoAdmin"] = ce.BillReducElec + ce.BillReducGas + ce.PACCostNoAdmin
    return ce


def ratios(ce: pd.DataFrame, release: Release) -> pd.DataFrame:
    """'Updating SC, TRC, PAC, and RIM ratios'"""
    sct_g = release.sct == "full"
    b = ce.ElecBen + ce.GasBen + ce.WaterEnergyBen + ce.OtherBen
    trc_b = b + ce.TaxCredits if release.tax_credits else b
    rim_b = b + ce.BillIncrElec + ce.BillIncrGas if release.bills == "split" else b
    r = {}
    if release.sct != "none":
        for t, sfx in [("SCB", "_SB"), ("SCH", "_SH")]:
            gs = sfx if sct_g else ""
            sb = ce[f"ElecBen{sfx}"] + ce[f"GasBen{gs}"] + ce[f"WaterEnergyBen{gs}"] + ce.OtherBen
            r[f"{t}Ratio"], r[f"{t}RatioNoAdmin"] = divide_or_zero(sb, ce[f"{t}Cost"]), divide_or_zero(sb, ce[f"{t}CostNoAdmin"])
    r["TRCRatio"], r["TRCRatioNoAdmin"] = divide_or_zero(trc_b, ce.TRCCost), divide_or_zero(trc_b, ce.TRCCostNoAdmin)
    r["PACRatio"], r["PACRatioNoAdmin"] = divide_or_zero(b, ce.PACCost), divide_or_zero(b, ce.PACCostNoAdmin)
    r["RIMRatio"], r["RIMRatioNoAdmin"] = divide_or_zero(rim_b, ce.RIMCost), divide_or_zero(rim_b, ce.RIMCostNoAdmin)
    return ce.assign(**r)


def calc_ce(e, s, programs, t, release, first_year, me_bens, me_cost) -> pd.DataFrame:
    ce, _ = benefits(e, s, t, release, first_year, me_bens, me_cost)
    ce = costs(ce, e, s, programs, release, first_year, me_cost)
    ce = bills(ce, e, s, t, release, me_bens)
    return ratios(ce, release)
