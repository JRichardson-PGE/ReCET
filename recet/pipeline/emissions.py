"""
CO2 / NOx / PM10 emissions outputs, ported from CET's real `CalcEmissions`
stored procedure (CET_v25.1_documentation_extracted.txt:17484-19100).

ELECTRIC CO2 SOURCE (2026-09-18, Jake): by default computed from the ACC's
  own hourly emission rate x the real impact profile, NOT from CET's table
  Validated against CET's `CO2` column at
  ~0.03% aggregate (up to ~1% per quarter). NOx/PM10 have no ACC source and
  still come from the Emissions table.

DATA (real CET tables, exported by Jake 2026-09-17/09-18; shipped in data/cet_tables/ without CET's E3 prefix):
  - `Emissions` (CET's E3Emissions): flat NOx and PM10 rates per (PA, TS, EU, CZ). NOx/PM10
    are joined to the quarterly CO2 rows by CET's `E3EmissionsSourcevw`, so
    a key missing from Emissions gets NOx = PM10 = 0 (e.g. every SCE and
    SCG row for the DEER:Res_ClothesDishWasher end use) -- real behavior,
    confirmed against real CET output, not a gap.
  - `CombustionType` (CET's E3CombustionType): NOx (lbs/therm) per combustion type, for gas NOx.
    A measure with no CombustionType (all of SWWH025) gets gas NOx = 0.
  - RunParameters `CO2Gas` for gas CO2 (0.00531 metric tons/therm for Version 2026; CO2 is in metric tons, NOx and PM10 in lbs).
  Emissions Versions 2026 and 2027 carry identical NOx/PM10 values, so
  either is used.

ELECTRIC (per measure, quarter by quarter from the install quarter through
EUL): `Qty * (NTGRkWh + MEBens) * IRkWh * RRkWh * rate * AnnualkWh(q)`, where
AnnualkWh(q) is the raw per-unit kWh1/kWh2 blended by the dual-baseline (RUL)
and end-of-EUL rules in the real SQL. CO2 uses `rate * AnnualkWh`; NOx and
PM10 use `rate * AnnualkWh / 4` ("different from CO2 per Brent Bolii @ E3",
per the SQL's own comment). First-year = the first four table quarters;
lifecycle = all of them. Net includes NTG, Gross does not.

GAS: `Qty * NTGRThm * IRThm * RRThm * factor * therms`, with first-year and
lifecycle therm terms exactly as the SQL's CASE blocks.

KNOWN LIMIT: CET's join does not filter DSType, so a key with both kW and
kWh rows would double-count; CO2 rows are summed per quarter here (matching
that duplication) but NOx/PM10 are not multiplied through it. No measure
tested has both.
"""
from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
import pandas as pd

from .. import paths, versions
from . import discount_settings, real_avoided_cost_v2, real_impact_profiles, system_zones
from .engine import QUARTER_BOUNDS

DATA_DIR = paths.package_data("cet_tables")
def is_available() -> bool:
    return all((DATA_DIR / f).exists() for f in ("emissions.parquet", "combustion_type.parquet"))


@lru_cache(maxsize=4)
def _nox_pm10_index(acc_version: int = 2024) -> dict[tuple[str, str, str, str], tuple[float, float]]:
    e = pd.read_parquet(DATA_DIR / "emissions.parquet")
    # The Emissions table's Version is CET's numbering: keep the rows of the version that holds this ACC.
    e = e[e["Version"].map(versions.acc_of_cet_version) == acc_version].drop_duplicates(subset=["PA", "TS", "EU", "CZ"])
    # TS upper-cased: CET's join is case-insensitive (SQL_Latin1_General_CP1_CI_AS) but Measure.csv spells some sectors
    # 'Commercial' where the Emissions table has 'COMMERCIAL' (PG&E numbered TOU end uses), and 'System' vs 'SYSTEM' for the zone.
    return {(r.PA, str(r.TS).upper(), r.EU, str(r.CZ).upper()): (float(r.NOx), float(r.PM10)) for r in e.itertuples()}


@lru_cache(maxsize=1)
def _combustion_nox() -> dict[str, float]:
    c = pd.read_parquet(DATA_DIR / "combustion_type.parquet")
    return dict(zip(c["LookupCode"], c["NOx"]))


@lru_cache(maxsize=4096)
def _hourly_co2_quarters(pa: str, target_sector: str, shape_key: str, climate_zone: str) -> list[tuple[int, float]] | None:
    """[(Qidx, CO2)] for 2024Q1..2054Q4 built the same way as every other
    electric value in this POC: the measure's real CEDARS impact profile
    (aligned to the ACC's 2018 weekdays), times the ACC's own hourly grid
    emission rate for that year, summed per quarter. None if the measure has
    no real matched profile (flagged elsewhere, never a synthetic fallback).
    A System measure uses its System zone's shape and emission rate (pipeline/system_zones.py), as its avoided costs do."""
    climate_zone = system_zones.priced_zone(shape_key, pa, target_sector, climate_zone)
    resolved = real_impact_profiles.resolve_shape(shape_key, pa, target_sector, climate_zone)   # user-supplied profile wins over the library's
    shape = resolved[0] if resolved is not None else None
    if shape is None:
        return None
    represents = resolved[1]
    rates = real_avoided_cost_v2.get_emission_rate_series(pa, climate_zone)
    out: list[tuple[int, float]] = []
    shifted = real_impact_profiles.shift_for_year(shape, 0, represents)        # the shift does not depend on the priced year: once per route
    for year in sorted(rates):
        weighted = shifted * rates[year]
        for q, (a, b) in enumerate(QUARTER_BOUNDS):
            out.append((year * 4 + q + 1, float(weighted[a:b].sum())))
    return out


@lru_cache(maxsize=4096)
def _is_kwh_per_unit(pa: str, target_sector: str, shape_key: str, climate_zone: str) -> bool:
    """True when the measure is priced on a user profile given in kWh per unit per hour (not a share of annual savings)."""
    zone = system_zones.priced_zone(shape_key, pa, target_sector, climate_zone)
    return real_impact_profiles.scaled_profile(shape_key, pa, target_sector, zone) is not None


def _annual_kwh(k: int, kwh1: float, kwh2: float, rulq: float, eulq: float) -> float:
    """CalcEmissions' AnnualkWh CASE for quarter offset k (= em Qidx - install Qidx)."""
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


def _gas_first_year(thm1: float, thm2: float, rul: float, eul: float) -> float:
    if rul > 0:
        if rul >= 1:
            return thm1
        if eul >= 1:
            return rul * thm1 + (1 - rul) * thm2
        return rul * thm1 + (eul - rul) * thm2
    if eul > 0:
        return thm1 if eul > 1 else eul * thm1
    return 0.0


def _gas_lifecycle(thm1: float, thm2: float, rul: float, eul: float) -> float:
    if rul > 0:
        return rul * thm1 + (eul - rul) * thm2
    if eul > 0:
        return eul * thm1
    return 0.0


def price_emissions_per_measure(measures: pd.DataFrame, acc_version: int = 2024, market_effects_bens: float = 0.0) -> pd.DataFrame:
    """One row per measure with CET's emissions output columns (Net/Gross x
    Elec/Gas x CO2/NOx, PM10, each First-year and Lifecycle).

    Electric CO2 comes from the ACC's own hourly emission rate x the
    measure's real impact profile (see _hourly_co2_quarters), not from CET's
    quarterly CO2 table.
    NOx/PM10 still come from the Emissions table -- the ACC has no electric NOx/
    PM10 rates, so this remains a CET-table dependency (see module docstring)."""
    nox_idx, comb = _nox_pm10_index(acc_version), _combustion_nox()
    from .. import run_poc
    rows: list[dict] = []
    for m in run_poc.split_by_baseline_shape(measures).itertuples(index=False):      # a separate 2nd-baseline shape: two parts, summed below
        qty = float(m.num_units)
        ntg_kwh, ntg_thm = float(m.NTGRkWh) + market_effects_bens, float(m.ntgr_therm) + market_effects_bens
        irr_kwh = float(m.ir_kwh) * float(m.rr_kwh)
        irr_thm = float(m.ir_therm) * float(m.rr_therm)
        kwh1, kwh2 = float(m.unit_kwh_1st), float(m.unit_kwh_2nd)
        if _is_kwh_per_unit(m.PA, m.target_sector, m.shape_key, str(m.climate_zone)):
            # the profile is already kWh per unit per hour: it is multiplied by what the electric benefit multiplies it by (shape_scale), per unit
            per_unit = qty * float(m.NTGRkWh) * irr_kwh
            kwh1, kwh2 = (float(m.shape_scale) / per_unit, float(m.shape_scale_2nd) / per_unit) if per_unit else (1.0, 1.0)
        thm1, thm2 = float(m.unit_therm_1st), float(m.unit_therm_2nd)
        eul = float(m.eul_years_exact)
        rul = float(m.rul_years_exact) if float(m.rul_years_exact) < eul else 0.0
        rulq, eulq = rul * 4.0, eul * 4.0
        install_q = int(m.claim_year) * 4 + int(m.claim_quarter)

        quarters = _hourly_co2_quarters(m.PA, m.target_sector, m.shape_key, str(m.climate_zone))
        nox_rate, pm10_rate = nox_idx.get((m.PA, str(m.target_sector).upper(), m.shape_key, str(m.climate_zone).upper()), (0.0, 0.0))
        fy = {"co2": 0.0, "nox": 0.0, "pm": 0.0}
        lc = {"co2": 0.0, "nox": 0.0, "pm": 0.0}
        if quarters:
            seq = 0
            for qidx, co2 in quarters:
                if qidx < install_q or not (qidx < install_q + eulq):
                    continue
                seq += 1
                ak = _annual_kwh(qidx - install_q, kwh1, kwh2, rulq, eulq)
                vals = {"co2": co2 * ak, "nox": nox_rate * ak / 4.0, "pm": pm10_rate * ak / 4.0}
                for k, v in vals.items():
                    lc[k] += v
                    if seq <= 4:
                        fy[k] += v

        co2_gas = discount_settings.get_co2_gas(m.PA, acc_version)
        nox_gas = comb.get(m.combustion_type, 0.0) if isinstance(m.combustion_type, str) else 0.0
        gfy, glc = _gas_first_year(thm1, thm2, rul, eul), _gas_lifecycle(thm1, thm2, rul, eul)

        def elec(d, net):
            f = qty * (ntg_kwh if net else 1.0) * irr_kwh
            return f * d["co2"], f * d["nox"], f * d["pm"]

        def gas(g, factor, net):
            return qty * (ntg_thm if net else 1.0) * irr_thm * factor * g

        r = {"measure_id": m.measure_id}
        for scope, d, g in (("", fy, gfy), ("Lifecycle", lc, glc)):
            for net, tag in ((True, "Net"), (False, "Gross")):
                c, n, p = elec(d, net)
                r[f"{tag}ElecCO2{scope}"] = c
                r[f"{tag}ElecNOx{scope}"] = n
                r[f"{tag}PM10{scope}"] = p
                r[f"{tag}GasCO2{scope}"] = gas(g, co2_gas, net)
                r[f"{tag}GasNOx{scope}"] = gas(g, nox_gas, net)
        rows.append(r)
    out = pd.DataFrame(rows)
    return out.groupby("measure_id", sort=False).sum().reset_index() if len(out) and out["measure_id"].duplicated().any() else out


# ------------------------------------------------------------------------------------------------------------------------------------------------------------
# The array version of price_emissions_per_measure (the reference: read it for the CalcEmissions mechanics; tests/test_performance.py requires the two to agree).
# Per route (PA, target sector, shape, zone) the quarterly CO2 series is built once; each measure's sum over the quarters of its life is then a matrix
# (measures x quarters) product. Rows are taken in chunks so a large route never holds more than CHUNK x quarters numbers at once.

CHUNK = 20_000


def _annual_kwh_array(k: np.ndarray, kwh1: np.ndarray, kwh2: np.ndarray, rulq: np.ndarray, eulq: np.ndarray) -> np.ndarray:
    """_annual_kwh on arrays (k: measures x quarters; the rest one value per measure, broadcast), the same branches in the same order."""
    k1, k2, rq, eq = kwh1[:, None], kwh2[:, None], rulq[:, None], eulq[:, None]
    eul_in = (k < eq) & (eq < k + 1)
    rul_in = (k < rq) & (rq < k + 1)
    single = np.select([k1 == 0, eul_in], [0.0, k1 * np.mod(eq, 1)], default=k1)
    dual = np.select([(k1 == 0) & (k2 == 0), rq > k + 1, (rq < k) & (eq > k + 1), rul_in & eul_in, rul_in, eul_in],
                     [0.0, k1, k2, k1 * np.mod(rq, 1) + k2 * (eq - rq), k1 * np.mod(rq, 1) + k2 * (1 - np.mod(rq, 1)), k2 * (eq - np.floor(eq))],
                     default=k2)
    return np.where(rq == 0, single, dual)


def price_emissions(measures: pd.DataFrame, acc_version: int = 2024, market_effects_bens: float = 0.0) -> pd.DataFrame:
    """price_emissions_per_measure on arrays (same outputs, same order of columns)."""
    from .. import run_poc
    nox_idx, comb = _nox_pm10_index(acc_version), _combustion_nox()
    m = run_poc.split_by_baseline_shape(measures).reset_index(drop=True)
    n = len(m)
    if n == 0:
        return price_emissions_per_measure(measures, acc_version, market_effects_bens)
    f = lambda c: m[c].astype(float).to_numpy()
    qty, irr_kwh, irr_thm = f("num_units"), f("ir_kwh") * f("rr_kwh"), f("ir_therm") * f("rr_therm")
    ntg_kwh, ntg_thm = f("NTGRkWh"), f("ntgr_therm")
    kwh1, kwh2, thm1, thm2 = f("unit_kwh_1st"), f("unit_kwh_2nd"), f("unit_therm_1st"), f("unit_therm_2nd")
    eul = f("eul_years_exact")
    rul_raw = f("rul_years_exact")
    rul = np.where(rul_raw < eul, rul_raw, 0.0)
    rulq, eulq = rul * 4.0, eul * 4.0
    install_q = m["claim_year"].astype(int).to_numpy() * 4 + m["claim_quarter"].astype(int).to_numpy()
    pa, ts, sk = m["PA"].astype(str).to_numpy(), m["target_sector"].astype(str).to_numpy(), m["shape_key"].astype(str).to_numpy()
    cz = m["climate_zone"].astype(str).to_numpy()
    routes = pd.Series(list(zip(pa, ts, sk, cz)))
    codes, uniq = pd.factorize(routes)

    per_unit_kwh = np.zeros(n, dtype=bool)
    nox_rate, pm_rate = np.zeros(n), np.zeros(n)
    sums = {k: np.zeros(n) for k in ("fy_co2", "lc_co2", "fy_ak", "lc_ak")}
    raw_pa, raw_ts, raw_sk = m["PA"].to_numpy(dtype=object), m["target_sector"].to_numpy(dtype=object), m["shape_key"].to_numpy(dtype=object)
    for r in range(len(uniq)):
        rows = np.flatnonzero(codes == r)
        i0 = rows[0]                                          # the lookups get the group's own values, as the reference passes them (a blank shape stays blank)
        r_pa, r_ts, r_sk, r_cz = raw_pa[i0], raw_ts[i0], raw_sk[i0], cz[i0]
        nx, pm = nox_idx.get((r_pa, str(r_ts).upper(), r_sk, r_cz.upper()), (0.0, 0.0))
        nox_rate[rows], pm_rate[rows] = nx, pm
        if _is_kwh_per_unit(r_pa, r_ts, r_sk, r_cz):
            per_unit_kwh[rows] = True
        quarters = _hourly_co2_quarters(r_pa, r_ts, r_sk, r_cz)
        if not quarters:
            continue
        qidx = np.array([q for q, _ in quarters])
        co2 = np.array([c for _, c in quarters])
        for start in range(0, len(rows), CHUNK):
            idx = rows[start:start + CHUNK]
            k1, k2 = kwh1[idx].copy(), kwh2[idx].copy()
            if per_unit_kwh[idx].any():                       # the profile is kWh per unit per hour: see price_emissions_per_measure
                sub = per_unit_kwh[idx]
                pu = qty[idx] * ntg_kwh[idx] * irr_kwh[idx]
                ss1, ss2 = f("shape_scale")[idx], f("shape_scale_2nd")[idx]
                with np.errstate(divide="ignore", invalid="ignore"):
                    k1 = np.where(sub, np.where(pu != 0, ss1 / pu, 1.0), k1)
                    k2 = np.where(sub, np.where(pu != 0, ss2 / pu, 1.0), k2)
            k = qidx[None, :] - install_q[idx, None]
            counted = (k >= 0) & (k < eulq[idx, None])
            first = counted & (np.cumsum(counted, axis=1) <= 4)
            ak = _annual_kwh_array(k, k1, k2, rulq[idx], eulq[idx])
            sums["lc_co2"][idx] = (np.where(counted, ak, 0.0) * co2).sum(axis=1)
            sums["fy_co2"][idx] = (np.where(first, ak, 0.0) * co2).sum(axis=1)
            sums["lc_ak"][idx] = np.where(counted, ak, 0.0).sum(axis=1)
            sums["fy_ak"][idx] = np.where(first, ak, 0.0).sum(axis=1)

    co2_gas = m["PA"].map({p_: discount_settings.get_co2_gas(p_, acc_version) for p_ in m["PA"].unique()}).astype(float).to_numpy()
    ct = m["combustion_type"]
    nox_gas = np.array([comb.get(c, 0.0) if isinstance(c, str) else 0.0 for c in ct.to_numpy()]) if len(ct) else np.zeros(n)
    g_first = np.select([(rul > 0) & (rul >= 1), (rul > 0) & (eul >= 1), rul > 0, (eul > 0) & (eul > 1), eul > 0],
                        [thm1, rul * thm1 + (1 - rul) * thm2, rul * thm1 + (eul - rul) * thm2, thm1, eul * thm1], default=0.0)
    g_life = np.select([rul > 0, eul > 0], [rul * thm1 + (eul - rul) * thm2, eul * thm1], default=0.0)

    out = {"measure_id": m["measure_id"].to_numpy()}
    for scope, co2_s, ak_s, g in (("", sums["fy_co2"], sums["fy_ak"], g_first), ("Lifecycle", sums["lc_co2"], sums["lc_ak"], g_life)):
        for net, tag in ((True, "Net"), (False, "Gross")):
            fe = qty * ((ntg_kwh + market_effects_bens) if net else 1.0) * irr_kwh
            fg = qty * ((ntg_thm + market_effects_bens) if net else 1.0) * irr_thm
            out[f"{tag}ElecCO2{scope}"] = fe * co2_s
            out[f"{tag}ElecNOx{scope}"] = fe * nox_rate * ak_s / 4.0
            out[f"{tag}PM10{scope}"] = fe * pm_rate * ak_s / 4.0
            out[f"{tag}GasCO2{scope}"] = fg * co2_gas * g
            out[f"{tag}GasNOx{scope}"] = fg * nox_gas * g
    res = pd.DataFrame(out)
    return res.groupby("measure_id", sort=False).sum().reset_index() if res["measure_id"].duplicated().any() else res
