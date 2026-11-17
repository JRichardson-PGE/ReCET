"""CalcEmissions: first-year and lifecycle CO2, NOx and PM10 per measure (CET's OutputEmissions / SavedEmissions)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .inputs import version_key
from .releases import Release
from .sql import NaN, dec10_mod1, key, z
from .tables import QuarterlyTables


def _qidx(qtr: pd.Series) -> pd.Series:
    """LEFT(Qtr, 4) * 4 + RIGHT(Qtr, 1)"""
    return qtr.str[:4].astype(int) * 4 + qtr.str[-1].astype(int)


def _source(t: QuarterlyTables, avc: str) -> pd.DataFrame:
    """E3EmissionsSourcevw: the avoided-cost table's quarterly CO2 with E3Emissions' NOx and PM10 per key, and the key it is
    matched on (PA + target sector + end use + climate zone, with DEER end uses' sector prefixes moved into the sector)."""
    co = t.ac_elec[["PA", "Version", "TS", "EU", "CZ", "Qtr", "CO2"]]
    em = t.emissions[["PA", "Version", "TS", "EU", "CZ", "NOx", "PM10"]]
    ks = ["PA", "Version", "TS", "EU", "CZ"]
    co = co.assign(**{c + "_k": key(co[c]) for c in ks})
    em = em.assign(**{c + "_k": key(em[c]) for c in ks})
    src = co.merge(em.drop(columns=ks), on=[c + "_k" for c in ks], how="left")
    src.loc[version_key(src.Version) != str(avc).casefold(), ["NOx", "PM10"]] = NaN
    eu = src.EU.astype(str)
    el = eu.str.lower()
    nonres, res = el.str.startswith("non_res:deer"), el.str.startswith("res:deer")
    ts = src.TS.where(~(nonres | res), np.where(nonres, "Non_res", "Res"))
    eu2 = eu.copy()
    eu2[nonres] = eu[nonres].str.replace("Non_Res:", "", case=False, regex=False)
    eu2[res] = eu[res].str.replace("res:", "", case=False, regex=False)
    src["cat_k"] = key(src.PA.astype(str) + ts.astype(str) + eu2 + src.CZ.astype(str))
    src["v_k"] = version_key(src.Version)
    src["EmQidx"] = _qidx(src.Qtr)
    return src.rename(columns={"Qtr": "EmQtr"})[["cat_k", "v_k", "EmQtr", "EmQidx", "CO2", "NOx", "PM10"]]


def calc_emissions(e: pd.DataFrame, s: pd.DataFrame, t: QuarterlyTables, release: Release, avc: str, me_bens: float) -> pd.DataFrame:
    E = e.assign(cat_k=key(e.PA + e.TS + e.EU + e.CZ.str.rstrip()), Qidx=_qidx(e.Qtr), RULq=e.rulq, EULq=e.eulq)
    # only the measure columns the quarterly rows need: the join has a row per measure and emission quarter
    cols = ["CET_ID", "pa_k", "cat_k", "Qidx", "RULq", "EULq", "kWh1", "kWh2", "kWhWater1", "kWhWater2", "Qty", "IRkWh", "RRkWh", "NTGRkWh"]
    im = E[cols].merge(s[["pa_k", "v_k"]], on="pa_k", how="inner")
    j = im.merge(t.cached(("emissions", str(avc)), lambda: _source(t, avc)), on=["cat_k", "v_k"])
    j = j[(j.EmQidx >= j.Qidx) & (j.EmQidx < j.Qidx + j.EULq)]
    # a LEFT JOIN: a measure with no emission quarters (no matching key, or no life) keeps one row with NULL rates, so 0
    # (against the distinct CET_IDs: `j` has a row per quarter, and isin over millions of strings is slow)
    j = pd.concat([j, im[~im.CET_ID.isin(j.CET_ID.unique())]], ignore_index=True).sort_values(["CET_ID", "EmQtr"], kind="stable")
    j["SeqQtr"] = j.groupby("CET_ID").cumcount() + 1
    w1, w2 = (j.kWhWater1, j.kWhWater2) if release.emissions_water else (0.0, 0.0)
    k1, k2 = j.kWh1, j.kWh2
    a, b, rq, eq = j.Qidx, j.EmQidx, j.RULq, j.EULq
    in_q = lambda x: (a + x > b) & (a + x < b + 1)
    s1, s2 = z(k1) + z(w1), z(k2) + z(w2)
    no_rul = np.select([s1 == 0, in_q(eq)], [0.0, s1 * dec10_mod1(eq)], s1)
    with_rul = np.select(
        [(s1 == 0) & (s2 == 0), a + rq > b + 1, (a + rq < b) & (a + eq > b + 1), in_q(rq) & in_q(eq), in_q(rq), in_q(eq)],
        [0.0, s1, s2, s1 * dec10_mod1(rq) + s2 * (eq - rq), s1 * dec10_mod1(rq) + s2 * (1 - dec10_mod1(rq)), s2 * (eq - np.floor(eq))],
        s2)
    j["AnnualkWh"] = np.where(z(rq) == 0, no_rul, with_rul)
    base = j.Qty * j.IRkWh * j.RRkWh
    nk = j.NTGRkWh + me_bens
    q = pd.DataFrame({"CET_ID": j.CET_ID, "SeqQtr": j.SeqQtr,
                      "NetElecCO2": base * nk * z(j.CO2 * j.AnnualkWh), "GrossElecCO2": base * z(j.CO2 * j.AnnualkWh),
                      "NetElecNOx": base * nk * z(j.NOx * j.AnnualkWh / 4), "GrossElecNOx": base * z(j.NOx * j.AnnualkWh / 4),
                      "NetPM10": base * nk * z(j.PM10 * j.AnnualkWh / 4), "GrossPM10": base * z(j.PM10 * j.AnnualkWh / 4)})
    vc = list(q.columns[2:])
    first = q[q.SeqQtr <= 4].groupby("CET_ID")[vc].sum(min_count=1)
    life = q.groupby("CET_ID")[vc].sum(min_count=1).add_suffix("Lifecycle")

    # gas: every measure (this SELECT has no Version filter)
    G = E.merge(s[["pa_k", "CO2Gas"]], on="pa_k", how="left")
    if release.nox_gas_lookup:
        cmb = t.combustion_type.assign(lk=key(t.combustion_type.LookupCode), NOxGas=pd.to_numeric(t.combustion_type.NOx))
        G = G.assign(lk=key(G.CombType)).merge(cmb[["lk", "NOxGas"]], on="lk", how="left")
    else:
        G["NOxGas"] = release.nox_gas_constant
    R, U, t1, t2 = G.RUL, G.EUL, z(G.Thm1), z(G.Thm2)

    def first_thm(dual):
        return np.select([dual & (R >= 1), dual & (R < 1) & (U >= 1), dual, U > 1, U > 0],
                         [t1, R * t1 + (1 - R) * t2, R * t1 + (U - R) * t2, t1, U * G.Thm1], 0.0)

    life_thm = np.select([z(R) > 0, U > 0], [R * t1 + (U - R) * t2, U * t1], 0.0)
    nt = G.NTGRThm + me_bens
    gb = G.Qty * G.IRThm * G.RRThm
    out = pd.DataFrame({"PA": G.PA, "PrgID": G.PrgID, "CET_ID": G.CET_ID})
    # CET's net gas CO2 tests ISNULL(RULq, 0) > 0, the others ISNULL(RUL, 0) > 0 (the same measures)
    out["NetGasCO2"] = z(gb * nt * G.CO2Gas * first_thm(z(G.RULq) > 0))
    out["GrossGasCO2"] = z(gb * G.CO2Gas * first_thm(z(R) > 0))
    out["NetGasCO2Lifecycle"] = z(gb * nt * G.CO2Gas * life_thm)
    out["GrossGasCO2Lifecycle"] = z(gb * G.CO2Gas * life_thm)
    out["NetGasNOx"] = z(gb * nt * G.NOxGas * first_thm(z(R) > 0))
    out["GrossGasNOx"] = z(gb * G.NOxGas * first_thm(z(R) > 0))
    out["NetGasNOxLifecycle"] = z(gb * nt * G.NOxGas * life_thm)
    out["GrossGasNOxLifecycle"] = z(gb * G.NOxGas * life_thm)
    return out.join(first, on="CET_ID").join(life, on="CET_ID")
