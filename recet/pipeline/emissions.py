"""
CO2 / NOx / PM10 emissions outputs, ported from CET's real `CalcEmissions`
stored procedure (CET_v25.1_documentation_extracted.txt:17484-19100).

ELECTRIC CO2 SOURCE (2026-09-18, Jake): by default computed from the ACC's
  own hourly emission rate x the real impact profile, NOT from CET's table
  (see price_emissions' co2_source). Validated against CET's `CO2` column at
  ~0.03% aggregate (up to ~1% per quarter). NOx/PM10 have no ACC source and
  still come from CET's `E3Emissions`.

DATA (real CET tables, exported by Jake 2026-09-17/09-18):
  - `E3AvoidedCostElecCO2Seq2026`'s `CO2` column (co2_source="cet" only): a QUARTERLY electric CO2
    rate per (PA, TS, EU, CZ, Qtr), extracted for all PAs to
    `data/cet_backup_tables_2026-09-17/e3_co2_quarterly.parquet`.
  - `E3Emissions`: flat NOx and PM10 rates per (PA, TS, EU, CZ). NOx/PM10
    are joined to the quarterly CO2 rows by CET's `E3EmissionsSourcevw`, so
    a key missing from E3Emissions gets NOx = PM10 = 0 (e.g. every SCE and
    SCG row for the DEER:Res_ClothesDishWasher end use) -- real behavior,
    confirmed against real CET output, not a gap.
  - `E3CombustionType`: NOx (lbs/therm) per combustion type, for gas NOx.
    A measure with no CombustionType (all of SWWH025) gets gas NOx = 0.
  - `E3Settings.CO2Gas` for gas CO2 (0.00531 metric tons/therm for Version 2026; CO2 is in metric tons, NOx and PM10 in lbs).
  E3Emissions Versions 2026 and 2027 carry identical NOx/PM10 values, so
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
from pathlib import Path

import pandas as pd

from .. import paths
from . import discount_settings, real_avoided_cost_v2, real_impact_profiles, system_zones
from .engine import QUARTER_BOUNDS

DATA_DIR = paths.package_data("e3_tables")
# CET's own quarterly CO2 table is a COMPARISON input (co2_source="cet") only; it is not shipped with the package.
CET_CO2_TABLE = paths.DEV_DATA_DIR / "cet_backup_tables_2026-09-17" / "e3_co2_quarterly.parquet"


def is_available() -> bool:
    return all((DATA_DIR / f).exists() for f in ("e3_emissions.parquet", "e3_combustion_type.parquet"))


@lru_cache(maxsize=1)
def _co2_index() -> dict[tuple[str, str, str, str], list[tuple[int, float]]]:
    """(PA, TS, EU, CZ) -> [(Qidx, CO2), ...] sorted by quarter, CO2 summed
    across duplicate (kW+kWh) rows for the same quarter."""
    d = pd.read_parquet(CET_CO2_TABLE, columns=["PA", "TS", "EU", "CZ", "Qtr", "CO2"])
    d["qidx"] = d["Qtr"].str[:4].astype(int) * 4 + d["Qtr"].str[-1].astype(int)
    d = d.groupby(["PA", "TS", "EU", "CZ", "qidx"], sort=True)["CO2"].sum().reset_index()
    out: dict[tuple[str, str, str, str], list[tuple[int, float]]] = {}
    for key, sub in d.groupby(["PA", "TS", "EU", "CZ"], sort=False):
        out[key] = list(zip(sub["qidx"].tolist(), sub["CO2"].tolist()))
    return out


@lru_cache(maxsize=4)
def _nox_pm10_index(acc_version: int = 2026) -> dict[tuple[str, str, str, str], tuple[float, float]]:
    e = pd.read_parquet(DATA_DIR / "e3_emissions.parquet")
    e = e[e["Version"] == acc_version].drop_duplicates(subset=["PA", "TS", "EU", "CZ"])
    # TS upper-cased: CET's join is case-insensitive (SQL_Latin1_General_CP1_CI_AS) but Measure.csv spells some sectors
    # 'Commercial' where E3Emissions has 'COMMERCIAL' (PG&E numbered TOU end uses), and 'System' vs 'SYSTEM' for the zone.
    return {(r.PA, str(r.TS).upper(), r.EU, str(r.CZ).upper()): (float(r.NOx), float(r.PM10)) for r in e.itertuples()}


@lru_cache(maxsize=1)
def _combustion_nox() -> dict[str, float]:
    c = pd.read_parquet(DATA_DIR / "e3_combustion_type.parquet")
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
    for year in sorted(rates):
        shifted = real_impact_profiles.shift_for_year(shape, year, represents)
        weighted = shifted * rates[year]
        for q, (a, b) in enumerate(QUARTER_BOUNDS):
            out.append((year * 4 + q + 1, float(weighted[a:b].sum())))
    return out


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


def price_emissions(measures: pd.DataFrame, acc_version: int = 2026, market_effects_bens: float = 0.0,
                    co2_source: str = "hourly") -> pd.DataFrame:
    """One row per measure with CET's emissions output columns (Net/Gross x
    Elec/Gas x CO2/NOx, PM10, each First-year and Lifecycle).

    co2_source="hourly" (default): electric CO2 from the ACC's own hourly
    emission rate x the measure's real impact profile (see
    _hourly_co2_quarters). This POC replaces CET, so CET's tables are a
    comparison point, not an input. co2_source="cet" reads CET's quarterly
    `CO2` column instead (matches real CET to ~1e-15; kept for comparison).
    NOx/PM10 still come from `E3Emissions` -- the ACC has no electric NOx/
    PM10 rates, so this remains a CET-table dependency (see module docstring)."""
    # CET's quarterly CO2 table is only read for the comparison option co2_source="cet"; the default computes CO2 from the ACC's hourly emission rate
    co2_idx = _co2_index() if co2_source == "cet" else {}
    nox_idx, comb = _nox_pm10_index(acc_version), _combustion_nox()
    rows: list[dict] = []
    for m in measures.itertuples(index=False):
        qty = float(m.num_units)
        ntg_kwh, ntg_thm = float(m.NTGRkWh) + market_effects_bens, float(m.ntgr_therm) + market_effects_bens
        irr_kwh = float(m.ir_kwh) * float(m.rr_kwh)
        irr_thm = float(m.ir_therm) * float(m.rr_therm)
        kwh1, kwh2 = float(m.unit_kwh_1st), float(m.unit_kwh_2nd)
        thm1, thm2 = float(m.unit_therm_1st), float(m.unit_therm_2nd)
        eul = float(m.eul_years_exact)
        rul = float(m.rul_years_exact) if float(m.rul_years_exact) < eul else 0.0
        rulq, eulq = rul * 4.0, eul * 4.0
        install_q = int(m.claim_year) * 4 + int(m.claim_quarter)

        key = (m.PA, m.target_sector, m.shape_key, str(m.climate_zone))
        quarters = (_hourly_co2_quarters(m.PA, m.target_sector, m.shape_key, str(m.climate_zone))
                    if co2_source == "hourly" else co2_idx.get(key))
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
    return pd.DataFrame(rows)
