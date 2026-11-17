"""CET's input views: MappingMeasurevw + InputMeasurevw, InputProgramvw and Settingsvw, as RunCET generates them for a
CEDARS-format job (SourceType 'CEDARSDatabase'). Column names follow CET's views so the calculation modules read like the SQL."""
from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from .releases import Release
from .sql import NaN, key, number

PROGRAM_COST_FIELDS = ["AdminCostsOverheadAndGA", "AdminCostsOther", "MarketingOutreach", "DIActivity", "DIInstallation",
                       "DIHardwareAndMaterials", "DIRebateAndInspection", "EMV", "UserInputIncentive", "CostsRecoveredFromOtherSources"]


def _col(m: pd.DataFrame, name: str) -> pd.Series:
    return m[name] if name in m.columns else pd.Series([None] * len(m), index=m.index, dtype=object)


def _blank(s: pd.Series) -> pd.Series:
    return s.where(s.notna() & (s.astype(str) != ""), None)


def version_key(s) -> pd.Series:
    """A Version column compared as CET compares it (some tables keep it as a float: 2026.0 = '2026')."""
    s = pd.Series(s).astype("string").str.strip()
    as_num = pd.to_numeric(s, errors="coerce")
    whole = as_num.notna() & (as_num == np.floor(as_num))
    return s.where(~whole, as_num.astype("Float64").astype("Int64").astype("string")).str.casefold()


def input_measure(m: pd.DataFrame, release: Release, first_year: int, avc: str) -> pd.DataFrame:
    """InputMeasurevw from the job's Measure rows (CET's CEDARS input names, blanks as NULL)."""
    m = m.reset_index(drop=True).apply(_blank)
    e = pd.DataFrame({"CET_ID": m.CEInputID, "PA": m.PA, "PrgID": m.PrgID})
    imp = _col(m, "MeasImpactType").fillna("").astype(str).str.lower()
    if release.fuel_sub_types:
        ft = _col(m, "FuelType")
        if release.fuel_type_from_impact:
            ft = ft.where(ft.notna(), pd.Series(np.where(imp.str.endswith("fuelsub"), "FuelSub-ToElec", None), index=m.index))
        e["FuelType"] = ft
        fs = key(ft).isin([t.casefold() for t in release.fuel_sub_types]).fillna(False).astype(bool)
        e["is_fs"], e["not_fs"] = fs, ~fs        # ISNULL(ft.FuelType, 0) = '0' is never NULL
    else:
        # MeasImpactType LIKE '%FuelSub' OR LIKE '%NC-AE': a NULL MeasImpactType makes both tests NULL (neither branch)
        e["FuelType"] = None
        known = _col(m, "MeasImpactType").notna()
        fs = pd.Series(False, index=m.index)
        for ending in release.fuel_sub_impact_types:
            fs |= imp.str.endswith(ending.lower())
        e["is_fs"], e["not_fs"] = known & fs, known & ~fs
    e["MeasImpactType"] = _col(m, "MeasImpactType")
    shape = _col(m, "E3MeaElecEndUseShape")
    sl = shape.fillna("").astype(str).str.lower()
    nonres, res = sl.str.startswith("non_res:deer"), sl.str.startswith("res:deer")
    ts = _col(m, "E3TargetSector").where(~(nonres | res), np.where(nonres, "Non_res", "Res"))
    eu = shape.astype(object).copy()
    eu[nonres] = shape[nonres].str.replace("Non_res:", "", case=False, regex=False)
    eu[res] = shape[res].str.replace("res:", "", case=False, regex=False)
    e["TS"] = ts.fillna("")
    e["EU"] = eu.fillna("")
    e["CZ"] = _col(m, "E3ClimateZone").fillna("")
    e["GS"] = _col(m, "E3GasSector").fillna("")
    e["GP"] = _col(m, "E3GasSavProfile").fillna("")
    e["ElecRateSchedule"] = _col(m, "RateScheduleElec").fillna("")
    e["GasRateSchedule"] = _col(m, "RateScheduleGas").fillna("")
    e["CombType"] = _col(m, "CombustionType")
    e["AVCVersion"] = str(avc)
    e["Qtr"] = m.ClaimYearQuarter
    y = m.ClaimYearQuarter.str[:4].astype(int)
    q = m.ClaimYearQuarter.str[5].astype(int)
    e["Qm"] = (y - first_year) * 4 + q - (0 if release.qm_one_based else 1)
    e["Qy"] = y - first_year
    e["Qty"] = number(_col(m, "NumUnits"), 0)
    for a, b in [("kW1", "UnitkW1stBaseline"), ("kWh1", "UnitkWh1stBaseline"), ("Thm1", "UnitTherm1stBaseline"),
                 ("kW2", "UnitkW2ndBaseline"), ("kWh2", "UnitkWh2ndBaseline"), ("Thm2", "UnitTherm2ndBaseline"),
                 ("kWhWater1", "UnitkWhIOUWater1stBaseline"), ("kWhWater2", "UnitkWhIOUWater2ndBaseline")]:
        e[a] = number(_col(m, b), 0)
    if not release.water:
        e["kWhWater1"] = e["kWhWater2"] = 0.0
    ntg_kwh = number(_col(m, "NTGRkWh"))
    e["NTGRkW"] = number(_col(m, "NTGRkW")).fillna(ntg_kwh).fillna(1)
    e["NTGRkWh"] = ntg_kwh.fillna(1)
    e["NTGRThm"] = number(_col(m, "NTGRTherm")).fillna(ntg_kwh).fillna(1)
    e["NTGRCost"] = number(_col(m, "NTGRCost")).fillna(ntg_kwh).fillna(1)
    e["IR"] = number(_col(m, "InstallationRatekWh"), 1)
    e["IRkW"] = number(_col(m, "InstallationRatekW"), 1)
    e["IRkWh"] = number(_col(m, "InstallationRatekWh"), 1)
    e["IRThm"] = number(_col(m, "InstallationRateTherm"), 1)
    e["RR"] = number(_col(m, "RealizationRatekWh"), 1)
    e["RRkWh"] = number(_col(m, "RealizationRatekWh"), 1)
    e["RRkW"] = number(_col(m, "RealizationRatekW"), 1)
    e["RRThm"] = number(_col(m, "RealizationRateTherm"), 1)
    e["IncentiveToOthers"] = number(_col(m, "UnitIncentiveToOthers"), 0)
    e["EndUserRebate"] = number(_col(m, "UnitEndUserRebate"), 0)
    e["DILaborCost"] = number(_col(m, "UnitDirectInstallLab"), 0)
    e["DIMaterialCost"] = number(_col(m, "UnitDirectInstallMat"), 0)
    e["UnitMeasureGrossCost"] = number(_col(m, "UnitMeaCost1stBaseline"), 0)
    e["UnitMeasureGrossCost_ER"] = number(_col(m, "UnitMeaCost2ndBaseline"), 0)
    EUL, RUL = number(_col(m, "EUL_Yrs")), number(_col(m, "RUL_Yrs"))
    eul0, rul0 = EUL.fillna(0), RUL.fillna(0)
    # ISNULL(CASE WHEN RUL >= EUL THEN 0 ELSE RUL END, 0): NULL (so 0) when either is NULL
    rul_in = pd.Series(np.where(RUL >= EUL, 0.0, RUL), index=m.index).where(RUL.notna() & EUL.notna(), NaN).fillna(0)
    e["eulq"] = eul0 * 4
    e["eul1"] = np.where(rul0 >= eul0, rul0, np.where(rul0 > 0, RUL, eul0))
    e["eulq1"] = e.eul1 * 4
    e["eul2"] = np.where(rul_in > 0, eul0, 0.0)
    e["eulq2"] = e.eul2 * 4
    e["rulq"] = rul_in * 4
    e["EUL"] = np.where(rul0 >= eul0, rul0, eul0)
    e["RUL"] = rul_in
    e["MeasIncrCost"] = e.UnitMeasureGrossCost_ER
    e["MeasInflation"] = number(_col(m, "MeasInflation"), 0)
    for c in ["UnitGasInfraBens", "UnitRefrigCosts", "UnitRefrigBens", "UnitMiscCosts", "UnitMiscBens"]:
        e[c] = number(_col(m, c))
    e["UnitTaxCredits"] = number(_col(m, "UnitTaxCredits")) if release.tax_credits else NaN
    e["pa_k"] = key(e.PA)
    return e


def input_program(p: pd.DataFrame, release: Release) -> pd.DataFrame:
    """InputProgramvw"""
    p = p.reset_index(drop=True).apply(_blank)

    def clean(s):
        s = s.astype("string")
        return s.str.replace("\r", "", regex=False).str.replace("\n", "", regex=False).str.strip() if release.trim_program_keys else s

    c = pd.DataFrame({"PrgID": clean(p.PrgID), "PA": clean(p.PA), "Year": p.ClaimYearQuarter.str[:4].astype(int)})
    for k in PROGRAM_COST_FIELDS:
        c[k] = number(_col(p, k), 0)
    c["pa_k"] = key(c.PA)
    c["prg_k"] = key(c.PrgID)
    return c


def settings(st: pd.DataFrame, avc: str) -> pd.DataFrame:
    """Settingsvw for the run's Version: the decimal(18,6) TRC rates as exact decimals plus one, the societal rates as floats."""
    s = st[version_key(st.Version) == version_key(pd.Series([str(avc)]))[0]].reset_index(drop=True)
    out = pd.DataFrame({"PA": s.PA, "Version": s.Version.astype(str).str.strip()})
    out["Ra"] = s.DiscountRateAnnual.map(lambda v: float(Decimal(str(v))))
    out["Raf"] = s.DiscountRateAnnual.map(lambda v: float(Decimal(str(v)) + 1))
    out["Rqf"] = s.DiscountRateQtr.map(lambda v: float(Decimal(str(v)) + 1))
    if "SocietalDiscountRateQtr" in s.columns:
        out["Raf_SCT"] = s.SocietalDiscountRateAnnual.astype(float) + 1
        out["Rqf_SCT"] = s.SocietalDiscountRateQtr.astype(float) + 1
    else:
        out["Raf_SCT"] = out.Raf
        out["Rqf_SCT"] = out.Rqf
    out["BaseYear"] = s.BaseYear.astype(int)
    out["CO2Gas"] = s.CO2Gas.astype(float)
    out["pa_k"] = key(out.PA)
    out["v_k"] = version_key(out.Version)
    return out
