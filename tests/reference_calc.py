"""An independent, plain implementation of the formulas in `recet/docs/CALCULATIONS.md`.

It exists to keep the documentation honest: `test_calcs.py` prices a small synthetic job with the tool and with this module (which shares no pricing code with the
tool -- loops and numpy only, reading the same raw data tables) and requires every documented output to agree. If a formula in CALCULATIONS.md changes, this file and
the document change together.

Section numbers in the comments (C1 ... C15) are the section numbers of CALCULATIONS.md.
"""
from __future__ import annotations

import datetime
import math

import numpy as np
import pandas as pd

from recet import paths

QUARTER_HOURS = [(0, 2160), (2160, 4344), (4344, 6552), (6552, 8760)]      # C2: the ACC's 2018 calendar quarters, as 0-based hour slices
GAS_QUARTER_MONTHS = [(0, 3), (3, 6), (6, 9), (9, 12)]
COST_FIELDS = ["AdminCostsOverheadAndGA", "AdminCostsOther", "MarketingOutreach", "DIActivity", "DIInstallation", "DIHardwareAndMaterials", "DIRebateAndInspection",
               "EMV", "UserInputIncentive", "CostsRecoveredFromOtherSources"]


# ---------------------------------------------------------------------------------------------------------------- C2: time
def quarter_schedule(claim_year: int, claim_quarter: int, eul_years: float):
    """[(k, calendar year, quarter index 0-3, weight)] for every quarter the measure delivers savings; the last, partial quarter has weight < 1."""
    eq = eul_years * 4.0
    full = int(eq)
    rem = eq - full
    n = full + (1 if rem > 1e-9 else 0)
    out = []
    for k in range(n):
        a = (claim_quarter - 1) + k
        out.append((k, claim_year + a // 4, a % 4, rem if (k == n - 1 and rem > 1e-9) else 1.0))
    return out


def phased(k: int, rul_quarters: float, s1: float, s2: float) -> float:
    """C2.3: the 1st-baseline value before the RUL, the 2nd-baseline value after it, a blend in the quarter that contains it."""
    if rul_quarters <= 0 or k + 1 <= rul_quarters:
        return s1
    if k >= rul_quarters:
        return s2
    w = rul_quarters - k
    return w * s1 + (1.0 - w) * s2


ACC_YEAR = 2018                                                              # C4.2: every ACC forecast year follows 2018's weekdays


def shift_shape(shape: np.ndarray, target_year: int, represents_year: int) -> np.ndarray:
    """C4.2: re-align a profile drawn on `represents_year`'s calendar onto the ACC's (2018's) weekdays: rotate by the weekday difference taken the short way
    round the week (-3..+3 days). The same for every `target_year` (accepted only to mirror the tool's signature)."""
    d = (datetime.date(ACC_YEAR, 1, 1).weekday() - datetime.date(represents_year, 1, 1).weekday()) % 7
    if d > 3:
        d -= 7
    return np.roll(shape, -24 * d)


def nearest(years, y):
    return min(sorted(years), key=lambda v: abs(v - y))


# ---------------------------------------------------------------------------------------------------------------- settings tables
def settings(pa: str, version: int) -> dict:
    t = pd.read_csv(paths.package_data("run_parameters.csv"))
    t["PA"] = t["PA"].str.strip()
    r = t[(t.PA == pa) & (t.Version == version)].iloc[0]
    return {"annual": float(r.DiscountRateAnnual), "qtr": float(r.DiscountRateQtr), "annual_sct": float(r.DiscountRateAnnual_SCT),
            "qtr_sct": float(r.DiscountRateQtr_SCT), "base_year": int(r.BaseYear), "co2_gas": float(r.CO2Gas)}


def rate_series(kind: str, pa: str, version: int, sector: str) -> dict[int, float]:
    d = paths.package_data("cet_tables")
    if kind == "elec":
        mp = pd.read_parquet(d / "rate_schedule_elec_mapping.parquet")
        sched = mp[(mp.PA == pa) & (mp.Version == version) & (mp.TargetSector.astype(str).str.upper() == sector.upper())].Schedule.iloc[0]
        rt = pd.read_parquet(d / "rate_schedule_elec.parquet")
        rt = rt[(rt.PA == pa) & (rt.Version == version) & (rt.Schedule == sched)].dropna(subset=["Year"])
        return dict(zip(rt.Year.astype(int), rt.RateE))
    mp = pd.read_parquet(d / "rate_schedule_gas_mapping.parquet")
    sched = mp[(mp.PA == pa) & (mp.Version == version) & (mp.GasSector == sector)].GasRateSchedule.iloc[0]
    rt = pd.read_parquet(d / "rate_schedule_gas.parquet")
    rt = rt[(rt.PA == pa) & (rt.Version == version) & (rt.Schedule == sched)]
    return dict(zip(rt.Year.astype(int), rt.RateG))


def nox_pm10(pa: str, version: int, sector: str, end_use: str, zone: str) -> tuple[float, float]:
    e = pd.read_parquet(paths.package_data("cet_tables") / "emissions.parquet")
    e = e[(e.Version == version) & (e.PA == pa) & (e.TS.astype(str).str.upper() == sector.upper()) & (e.EU == end_use) & (e.CZ.astype(str).str.upper() == zone.upper())]
    return (float(e.NOx.iloc[0]), float(e.PM10.iloc[0])) if len(e) else (0.0, 0.0)


# ---------------------------------------------------------------------------------------------------------------- C1: derived fields
def derive(r: dict) -> dict:
    """`r`: raw Measure.csv values as floats/strings (blank -> None). Returns the poc_* fields the pricing uses."""
    f = lambda k, d=0.0: d if r.get(k) in (None, "") else float(r[k])
    d = {"qty": f("NumUnits"), "kwh1": f("UnitkWh1stBaseline"), "kwh2": f("UnitkWh2ndBaseline"), "kw1": f("UnitkW1stBaseline"), "kw2": f("UnitkW2ndBaseline"),
         "th1": f("UnitTherm1stBaseline"), "th2": f("UnitTherm2ndBaseline"), "w1": f("UnitkWhIOUWater1stBaseline"), "w2": f("UnitkWhIOUWater2ndBaseline")}
    d["ntg_kwh"] = float(r["NTGRkWh"])
    d["ntg_therm"] = f("NTGRTherm", d["ntg_kwh"])
    d["ntg_cost"] = f("NTGRCost", d["ntg_kwh"])
    d["ntg_kw"] = f("NTGRkW", d["ntg_kwh"])
    d["irr_kw"] = f("InstallationRatekW", 1.0) * f("RealizationRatekW", 1.0)
    d["irr_kwh"] = f("InstallationRatekWh", 1.0) * f("RealizationRatekWh", 1.0)
    d["irr_therm"] = f("InstallationRateTherm", 1.0) * f("RealizationRateTherm", 1.0)
    d["eul"] = max(float(r["EUL_Yrs"]), 0.25)
    d["rul"] = max(f("RUL_Yrs"), 0.0)
    d["claim_year"], d["claim_quarter"] = int(str(r["ClaimYearQuarter"])[:4]), int(str(r["ClaimYearQuarter"])[-1])
    d["shape_scale_1st"] = d["qty"] * d["kwh1"] * d["ntg_kwh"] * d["irr_kwh"]
    d["shape_scale_2nd"] = d["qty"] * d["kwh2"] * d["ntg_kwh"] * d["irr_kwh"]
    d["therms_1st"] = d["qty"] * d["th1"] * d["ntg_therm"] * d["irr_therm"]
    d["therms_2nd"] = d["qty"] * d["th2"] * d["ntg_therm"] * d["irr_therm"]
    d["water_scale_1st"] = d["qty"] * d["w1"] * d["ntg_kwh"] * d["irr_kwh"]
    d["water_scale_2nd"] = d["qty"] * d["w2"] * d["ntg_kwh"] * d["irr_kwh"]
    fuel_sub = str(r.get("FuelID") or "") in ("AllElec-New", "FuelSub-ToElec", "FuelSub-ToGas") or "FuelSub" in str(r.get("MeasImpactType") or "")
    d["is_fuel_sub"] = fuel_sub
    return d


# ---------------------------------------------------------------------------------------------------------------- C5: fuel values
def elec_value(d, shape, represents_year, acc, rate_qtr, first_year, scale_1st, scale_2nd, meb=0.0, ntg=None):
    """C5.1: lifetime PV of the hourly electric benefit. `acc`: {year: array(8760) in $/kWh}. Market effects scale both baselines by (NTG + MEB) / NTG."""
    ntg = d["ntg_kwh"] if ntg is None else ntg
    factor = (ntg + meb) / ntg if (meb and ntg > 0) else 1.0
    total = 0.0
    for k, y, q, w in quarter_schedule(d["claim_year"], d["claim_quarter"], d["eul"]):
        a, b = QUARTER_HOURS[q]
        price = acc[nearest(acc.keys(), y)]
        hourly = shift_shape(shape, y, represents_year) * price
        s = phased(k, d["rul"] * 4.0, scale_1st * factor, scale_2nd * factor)
        total += s * hourly[a:b].sum() * w / (1.0 + rate_qtr) ** ((y - first_year) * 4 + q)
    return total


def gas_value(d, shape, acc, rate_qtr, first_year, meb=0.0):
    """C5.4: lifetime PV of the gas benefit. `acc`: {year: array(12) in $/therm}."""
    factor = (d["ntg_therm"] + meb) / d["ntg_therm"] if (meb and d["ntg_therm"] > 0) else 1.0
    total = 0.0
    lo, hi = min(acc), max(acc)
    for k, y, q, w in quarter_schedule(d["claim_year"], d["claim_quarter"], d["eul"]):
        a, b = GAS_QUARTER_MONTHS[q]
        therms = phased(k, d["rul"] * 4.0, d["therms_1st"] * factor, d["therms_2nd"] * factor)
        monthly = shape * therms * acc[min(max(y, lo), hi)]
        total += monthly[a:b].sum() * w / (1.0 + rate_qtr) ** ((y - first_year) * 4 + q)
    return total


def split_ben_supply(v: float, fuel_sub: bool) -> tuple[float, float]:
    """C6.1"""
    return (max(v, 0.0), max(-v, 0.0)) if fuel_sub else (v, 0.0)


def split_water(v: float, w1: float) -> tuple[float, float]:
    """C6.2"""
    return (v if w1 > 0 else 0.0, -v if w1 < 0 else 0.0)


# ---------------------------------------------------------------------------------------------------------------- C7: other benefits and costs
def other(r, d, meb, mec):
    f = lambda k: 0.0 if r.get(k) in (None, "") else float(r[k])
    bens = f("UnitGasInfraBens") + f("UnitRefrigBens") + f("UnitMiscBens")
    costs = f("UnitRefrigCosts") + f("UnitMiscCosts")
    q, n = d["qty"], d["ntg_kwh"]
    return {"OtherBen": q * (n + meb) * bens, "OtherCost": q * (n + mec) * costs, "TaxCredits": q * (n + meb) * f("UnitTaxCredits"),
            "OtherBenGross": q * bens, "OtherCostGross": q * costs, "TaxCreditsGross": q * f("UnitTaxCredits")}


# ---------------------------------------------------------------------------------------------------------------- C8: participant costs
def participant(r, d, rate_qtr, first_year, mec):
    f = lambda k: 0.0 if r.get(k) in (None, "") else float(r[k])
    q = d["qty"]
    rqf = 1.0 + rate_qtr
    qm = (d["claim_year"] - first_year) * 4 + (d["claim_quarter"] - 1)
    pvf = rqf ** qm
    incents_di = q * (f("UnitIncentiveToOthers") + f("UnitDirectInstallLab") + f("UnitDirectInstallMat"))
    rebates = q * f("UnitEndUserRebate")
    unit_cost, incr_cost, rul = f("UnitMeaCost1stBaseline"), f("UnitMeaCost2ndBaseline"), d["rul"]
    gross_meas = q * (incr_cost if rul > 0 else unit_cost)
    pv_sub = (unit_cost - incr_cost) * ((1.0 + f("MeasInflation") / 4.0) / rqf) ** (rul * 4.0) if rul > 0 else 0.0
    gross_meas_pv = q * (unit_cost - pv_sub) / pvf
    excess_pv = max(incents_di / pvf - gross_meas_pv, 0.0)
    mark_pv = mec * (gross_meas_pv + excess_pv)
    gross_pv = gross_meas_pv - (incents_di + rebates) / pvf
    net_pv = d["ntg_cost"] * (gross_pv + excess_pv)
    return {"RebatesandIncents": incents_di + rebates, "RebatesandIncentsPV": (incents_di + rebates) / pvf, "GrossParticipantCostPV": gross_pv, "NetParticipantCostPV": net_pv,
            "MarkEffectPlusExcessIncPV": mark_pv, "NetParticipantCostAdjustedPV": net_pv + mark_pv}


# ---------------------------------------------------------------------------------------------------------------- C19: first-year net savings
def first_year_net(r, d, meb):
    """C19, written out branch by branch (EUL as filed, not floored)."""
    f = lambda k: 0.0 if r.get(k) in (None, "") else float(r[k])
    eul, rul = float(r["EUL_Yrs"]), d["rul"]
    if rul > 1:
        w1 = 1.0
    elif 0 < rul < 1:
        w1 = rul
    elif rul == 0 and eul < 1:
        w1 = eul
    else:
        w1 = 1.0
    if 0 < rul < 1 and eul < 1:
        w2 = eul - rul
    elif 0 < rul < 1:
        w2 = 1.0 - rul
    else:
        w2 = 0.0
    q = d["qty"]
    return {"FirstYearNetKWh": q * (d["ntg_kwh"] + meb) * d["irr_kwh"] * (w1 * (d["kwh1"] + d["w1"]) + w2 * (d["kwh2"] + d["w2"])),
            "FirstYearNetKW": q * (d["ntg_kw"] + meb) * d["irr_kw"] * (w1 * d["kw1"] + w2 * d["kw2"]),
            "FirstYearNetThm": q * (d["ntg_therm"] + meb) * d["irr_therm"] * (w1 * d["th1"] + w2 * d["th2"])}


# ---------------------------------------------------------------------------------------------------------------- C9: bill impacts
def bill_pv(d, s1, s2, rates, rate_annual, base_year):
    """C9: annual PV of the bill change; the boundary and every 2nd-baseline year are discounted by Raf^(Year - BaseYear) (CalcCE's Qy), the pure 1st-baseline years by
    Raf^(years since claim)."""
    raf = 1.0 + rate_annual
    yr1 = d["claim_year"] - base_year
    eul, rul = d["eul"], d["rul"]
    full = int(eul)
    rem = eul - full
    n = full + (1 if rem > 1e-9 else 0)
    floor_rul = int(rul) if rul > 0 else full
    boundary = (rul - floor_rul) if rul > 0 else 1.0
    total = 0.0
    for k in range(min(floor_rul, n)):
        if d["claim_year"] + k in rates:
            total += s1 * rates[d["claim_year"] + k] / raf ** k
    for k in range(floor_rul, n):
        if d["claim_year"] + k not in rates:
            continue
        w = rem if (k == n - 1 and rem > 1e-9) else 1.0
        v = boundary * s1 + (1.0 - boundary) * s2 if k == floor_rul else s2
        total += w * v * rates[d["claim_year"] + k] / raf ** (k + yr1)
    return total


def participant_test(d, part, tax_credits_gross, bills_gross, rate_qtr, first_year):
    """C12: the Participant Cost Test on gross bill impacts (`bills_gross`: the four ...Gross bill columns)."""
    pvf = (1.0 + rate_qtr) ** ((d["claim_year"] - first_year) * 4 + (d["claim_quarter"] - 1))
    ben = bills_gross["BillReducElecGross"] + bills_gross["BillReducGasGross"] + part["RebatesandIncentsPV"] + tax_credits_gross / pvf
    cost = part["GrossParticipantCostPV"] + part["RebatesandIncentsPV"] + bills_gross["BillIncrElecGross"] + bills_gross["BillIncrGasGross"]
    return {**bills_gross, "PCTBen": ben, "PCTCost": cost, "PCTRatio": ben / cost if cost else 0.0}


# ---------------------------------------------------------------------------------------------------------------- C14: emissions
def annual_kwh(k, kwh1, kwh2, rulq, eulq):
    if rulq == 0:
        if kwh1 == 0:
            return 0.0
        if k < eulq < k + 1:
            return kwh1 * (eulq % 1)
        return kwh1
    if kwh1 == 0 and kwh2 == 0:
        return 0.0
    if rulq > k + 1:
        return kwh1
    if rulq < k and eulq > k + 1:
        return kwh2
    if k < rulq < k + 1 and k < eulq < k + 1:
        return kwh1 * (rulq % 1) + kwh2 * (eulq - rulq)
    if k < rulq < k + 1:
        return kwh1 * (rulq % 1) + kwh2 * (1 - rulq % 1)
    if k < eulq < k + 1:
        return kwh2 * (eulq - math.floor(eulq))
    return kwh2


def hourly_co2_quarters(shape, represents_year, rates_t_per_kwh):
    """C14.1, hourly profile: [(quarter index = 4 x year + quarter, tonnes CO2 per annual kWh in that quarter)]. `rates_t_per_kwh`: {year: array(8760)}."""
    quarters = []
    for y in sorted(rates_t_per_kwh):
        sh = shift_shape(shape, y, represents_year)
        for q, (a, b) in enumerate(QUARTER_HOURS):
            quarters.append((y * 4 + q + 1, float((sh * rates_t_per_kwh[y])[a:b].sum())))
    return quarters


def emissions(d, quarters, nox, pm10, co2_gas, nox_gas, meb):
    """C14.2-C14.3. `quarters`: from hourly_co2_quarters."""
    eul = d["eul"]
    rul = d["rul"] if d["rul"] < eul else 0.0
    rulq, eulq = rul * 4.0, eul * 4.0
    install = d["claim_year"] * 4 + d["claim_quarter"]
    fy = {"co2": 0.0, "nox": 0.0, "pm": 0.0}
    lc = dict(fy)
    seq = 0
    for qidx, co2 in quarters:
        if qidx < install or not (qidx < install + eulq):
            continue
        seq += 1
        ak = annual_kwh(qidx - install, d["kwh1"], d["kwh2"], rulq, eulq)
        for key, v in (("co2", co2 * ak), ("nox", nox * ak / 4.0), ("pm", pm10 * ak / 4.0)):
            lc[key] += v
            if seq <= 4:
                fy[key] += v

    def gas_first(t1, t2):
        if rul > 0:
            if rul >= 1:
                return t1
            return rul * t1 + (1 - rul) * t2 if eul >= 1 else rul * t1 + (eul - rul) * t2
        return (t1 if eul > 1 else eul * t1) if eul > 0 else 0.0

    def gas_life(t1, t2):
        return rul * t1 + (eul - rul) * t2 if rul > 0 else (eul * t1 if eul > 0 else 0.0)

    out = {}
    for scope, e, g in (("", fy, gas_first(d["th1"], d["th2"])), ("Lifecycle", lc, gas_life(d["th1"], d["th2"]))):
        for net, tag in ((True, "Net"), (False, "Gross")):
            fe = d["qty"] * ((d["ntg_kwh"] + meb) if net else 1.0) * d["irr_kwh"]
            fg = d["qty"] * ((d["ntg_therm"] + meb) if net else 1.0) * d["irr_therm"]
            out[f"{tag}ElecCO2{scope}"], out[f"{tag}ElecNOx{scope}"], out[f"{tag}PM10{scope}"] = fe * e["co2"], fe * e["nox"], fe * e["pm"]
            out[f"{tag}GasCO2{scope}"], out[f"{tag}GasNOx{scope}"] = fg * co2_gas * g, fg * nox_gas * g
    return out


# ---------------------------------------------------------------------------------------------------------------- C10-C13: program costs and the cost tests
def cost_tests(rows: list[dict], program: pd.DataFrame | None, first_year: int, st: dict, meb: float) -> list[dict]:
    """`rows`: one dict per measure holding the pre-program values (ElecBen ... TaxCredits, participant PVs, bill impacts, `prg`, `ntg_kwh`, `ntg_therm`).
    Adds every Gross / NoAdmin / weighting / cost / ratio output, exactly as documented in C10-C12."""
    def ratio(n, dn):
        return n / dn if dn else 0.0

    pos = lambda v: v if v > 0 else 0.0
    sums: dict[str, dict] = {}
    if program is not None:
        for prg, g in program.groupby("PrgID"):
            tot = g[COST_FIELDS].fillna(0.0).sum(axis=1)
            n = g["Year"] - first_year
            sums[prg] = {"SumCosts": float(tot.sum()), "npv": float((tot / (1 + st["annual"]) ** n).sum()), "npv_sct": float((tot / (1 + st["annual_sct"]) ** n).sum())}
    zero = {"SumCosts": 0.0, "npv": 0.0, "npv_sct": 0.0}

    def share(rows_, kind_cols, gross=False):
        ben = [sum(pos(r[c]) for c in kind_cols(r, gross)) for r in rows_]
        res = []
        for r, b in zip(rows_, ben):
            same = [x for x, rr in zip(ben, rows_) if rr["prg"] == r["prg"]]
            tot = sum(same)
            res.append(b / tot if tot != 0 else 1.0 / len(same))
        return res

    trc_cols = lambda r, g: ["ElecBenGross", "GasBenGross", "WaterEnergyBenGross", "OtherBenGross"] if g else ["ElecBen", "GasBen", "WaterEnergyBen", "OtherBen"]
    sb_cols = lambda r, g: ["ElecBenGross_SB", "GasBenGross_SB", "WaterEnergyBenGross_SB", "OtherBenGross"] if g else ["ElecBen_SB", "GasBen_SB", "WaterEnergyBen_SB", "OtherBen"]
    sh_cols = lambda r, g: ["ElecBenGross_SH", "GasBenGross_SH", "WaterEnergyBenGross_SH", "OtherBenGross"] if g else ["ElecBen_SH", "GasBen_SH", "WaterEnergyBen_SH", "OtherBen"]
    out = []
    for i, r in enumerate(rows):
        r = dict(r)
        ps = sums.get(r["prg"], zero)
        ne, nt = r["ntg_kwh"] + meb, r["ntg_therm"] + meb
        for sfx in ("", "_SB", "_SH"):                                          # C10 Gross
            r[f"ElecBenGross{sfx}"] = ratio(r[f"ElecBen{sfx}"], ne)
            r[f"GasBenGross{sfx}"] = ratio(r[f"GasBen{sfx}"], nt)
            r[f"ElecSupplyCostGross{sfx}"] = ratio(r[f"ElecSupplyCost{sfx}"], ne)
            r[f"GasSupplyCostGross{sfx}"] = ratio(r[f"GasSupplyCost{sfx}"], nt)
            r[f"WaterEnergyBenGross{sfx}"] = ratio(r[f"WaterEnergyBen{sfx}"], ne)
            r[f"WaterEnergyCostGross{sfx}"] = ratio(r[f"WaterEnergyCost{sfx}"], ne)
        out.append(r)
    sh_trc, sh_trc_g = share(out, trc_cols), share(out, trc_cols, True)
    sh_sb, sh_sb_g = share(out, sb_cols), share(out, sb_cols, True)
    sh_sh, sh_sh_g = share(out, sh_cols), share(out, sh_cols, True)
    for i, r in enumerate(out):
        ps = sums.get(r["prg"], zero)
        r["TotalSystemBenefit"] = (r["ElecBen"] + r["GasBen"] + r["WaterEnergyBen"] + r["OtherBen"]) - (r["ElecSupplyCost"] + r["GasSupplyCost"] + r["WaterEnergyCost"] + r["OtherCost"])
        r["TotalSystemBenefitGross"] = ((r["ElecBenGross"] + r["GasBenGross"] + r["WaterEnergyBenGross"] + r["OtherBenGross"])
                                        - (r["ElecSupplyCostGross"] + r["GasSupplyCostGross"] + r["WaterEnergyCostGross"] + r["OtherCostGross"]))
        r["WeightedBenefits"] = sh_trc[i]
        r["WeightedProgramCost"] = sh_trc[i] * ps["SumCosts"]
        r["TotalExpenditures"] = r["RebatesandIncents"] + r["WeightedProgramCost"]                                   # C11.3
        sup = r["ElecSupplyCost"] + r["GasSupplyCost"] + r["WaterEnergyCost"] + r["OtherCost"]
        sup_g = r["ElecSupplyCostGross"] + r["GasSupplyCostGross"] + r["WaterEnergyCostGross"] + r["OtherCostGross"]
        inc, inc_s = r["RebatesandIncentsPV"], r["RebatesandIncentsPV_sct"]
        r["PACCostNoAdmin"] = inc + sup
        r["PACCost"] = sh_trc[i] * ps["npv"] + r["PACCostNoAdmin"]
        r["TRCCostNoAdmin"] = inc + r["NetParticipantCostAdjustedPV"] + sup
        r["TRCCost"] = sh_trc[i] * ps["npv"] + r["TRCCostNoAdmin"]
        r["TRCCostGross"] = sh_trc_g[i] * ps["npv"] + inc + r["GrossParticipantCostPV"] + sup_g
        r["RIMCostNoAdmin"] = r["BillReducElec"] + r["BillReducGas"] + r["PACCostNoAdmin"]
        r["RIMCost"] = r["BillReducElec"] + r["BillReducGas"] + r["PACCost"]
        for name, s, sh, sh_g in (("SCB", "_SB", sh_sb, sh_sb_g), ("SCH", "_SH", sh_sh, sh_sh_g)):
            sup_s = r[f"ElecSupplyCost{s}"] + r[f"GasSupplyCost{s}"] + r[f"WaterEnergyCost{s}"] + r["OtherCost"]
            sup_sg = r[f"ElecSupplyCostGross{s}"] + r[f"GasSupplyCostGross{s}"] + r[f"WaterEnergyCostGross{s}"] + r["OtherCostGross"]
            r[f"{name}CostNoAdmin"] = inc_s + r["NetParticipantCostAdjustedPV_sct"] + sup_s
            r[f"{name}Cost"] = sh[i] * ps["npv_sct"] + r[f"{name}CostNoAdmin"]
            r[f"{name}CostGross"] = sh_g[i] * ps["npv_sct"] + inc_s + r["GrossParticipantCostPV_sct"] + sup_sg
        b = r["ElecBen"] + r["GasBen"] + r["WaterEnergyBen"] + r["OtherBen"]
        r["TRCRatio"], r["TRCRatioNoAdmin"] = ratio(b + r["TaxCredits"], r["TRCCost"]), ratio(b + r["TaxCredits"], r["TRCCostNoAdmin"])
        r["PACRatio"], r["PACRatioNoAdmin"] = ratio(b, r["PACCost"]), ratio(b, r["PACCostNoAdmin"])
        rim_b = b + r["BillIncrElec"] + r["BillIncrGas"]
        r["RIMRatio"], r["RIMRatioNoAdmin"] = ratio(rim_b, r["RIMCost"]), ratio(rim_b, r["RIMCostNoAdmin"])
        b_sb = r["ElecBen_SB"] + r["GasBen_SB"] + r["WaterEnergyBen_SB"] + r["OtherBen"]
        b_sh = r["ElecBen_SH"] + r["GasBen_SH"] + r["WaterEnergyBen_SH"] + r["OtherBen"]
        r["SCBRatio"], r["SCHRatio"] = ratio(b_sb, r["SCBCost"]), ratio(b_sh, r["SCHCost"])
        r["SCBRatioNoAdmin"], r["SCHRatioNoAdmin"] = ratio(b_sb, r["SCBCostNoAdmin"]), ratio(b_sh, r["SCHCostNoAdmin"])
    return out
