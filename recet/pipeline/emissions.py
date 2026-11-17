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
    nox_pm10_by_key, nox_by_combustion_type = _nox_pm10_index(acc_version), _combustion_nox()
    from .. import run_poc
    rows: list[dict] = []
    for m in run_poc.split_by_baseline_shape(measures).itertuples(index=False):      # a separate 2nd-baseline profile: two parts, summed below
        NumUnits = float(m.NumUnits)
        NTGRkWh_plus_MEBens, NTGRTherm_plus_MEBens = float(m.NTGRkWh) + market_effects_bens, float(m.NTGRTherm) + market_effects_bens
        ir_x_rr_kwh = float(m.InstallationRatekWh) * float(m.RealizationRatekWh)
        ir_x_rr_therm = float(m.InstallationRateTherm) * float(m.RealizationRateTherm)
        profile, iou, sector, zone = m.Used_AC_Elec_Impact_Profile, m.IOU_AC_Territory, m.AC_Elec_Target_Sector, str(m.AC_Climate_Zone)
        kwh_per_unit_1st, kwh_per_unit_2nd = float(m.Used_UnitkWh1stBaseline), float(m.Used_UnitkWh2ndBaseline)
        if _is_kwh_per_unit(iou, sector, profile, zone):
            # the profile is already kWh per unit per hour: it is multiplied by what the electric benefit multiplies it by (AC_Net_kWh_ScaledProfile_*), per unit
            units_x_ntg_x_ir_x_rr = NumUnits * float(m.NTGRkWh) * ir_x_rr_kwh
            kwh_per_unit_1st, kwh_per_unit_2nd = ((float(m.AC_Net_kWh_ScaledProfile_1st_Baseline) / units_x_ntg_x_ir_x_rr,
                                                   float(m.AC_Net_kWh_ScaledProfile_2nd_Baseline) / units_x_ntg_x_ir_x_rr) if units_x_ntg_x_ir_x_rr else (1.0, 1.0))
        UnitTherm1stBaseline, UnitTherm2ndBaseline = float(m.UnitTherm1stBaseline), float(m.UnitTherm2ndBaseline)
        EUL_Yrs = float(m.EUL_Yrs)
        rul_for_emissions = float(m.RUL_Yrs) if float(m.RUL_Yrs) < EUL_Yrs else 0.0           # CalcCE's InputMeasurevw: RUL >= EUL counts as no RUL
        rul_quarters, eul_quarters = rul_for_emissions * 4.0, EUL_Yrs * 4.0                   # CalcEmissions' RULq / EULq
        install_quarter_index = int(m.Used_Year) * 4 + int(m.Used_Quarter)                   # the claim quarter's Qidx

        co2_quarters = _hourly_co2_quarters(iou, sector, profile, zone)
        nox_per_kwh, pm10_per_kwh = nox_pm10_by_key.get((iou, str(sector).upper(), profile, zone.upper()), (0.0, 0.0))
        first_year = {"co2": 0.0, "nox": 0.0, "pm10": 0.0}
        lifecycle = {"co2": 0.0, "nox": 0.0, "pm10": 0.0}
        if co2_quarters:
            counted_quarter = 0
            for quarter_index, co2_tonnes_per_kwh in co2_quarters:
                if quarter_index < install_quarter_index or not (quarter_index < install_quarter_index + eul_quarters):
                    continue
                counted_quarter += 1
                AnnualkWh = _annual_kwh(quarter_index - install_quarter_index, kwh_per_unit_1st, kwh_per_unit_2nd, rul_quarters, eul_quarters)
                quarter_emissions = {"co2": co2_tonnes_per_kwh * AnnualkWh, "nox": nox_per_kwh * AnnualkWh / 4.0, "pm10": pm10_per_kwh * AnnualkWh / 4.0}
                for k, v in quarter_emissions.items():
                    lifecycle[k] += v
                    if counted_quarter <= 4:
                        first_year[k] += v

        CO2Gas = discount_settings.get_co2_gas(iou, acc_version)
        combustion_nox_per_therm = nox_by_combustion_type.get(m.CombustionType, 0.0) if isinstance(m.CombustionType, str) else 0.0
        first_year_therms = _gas_first_year(UnitTherm1stBaseline, UnitTherm2ndBaseline, rul_for_emissions, EUL_Yrs)
        lifecycle_therms = _gas_lifecycle(UnitTherm1stBaseline, UnitTherm2ndBaseline, rul_for_emissions, EUL_Yrs)

        def elec(sums, is_net):
            elec_quantity_factor = NumUnits * (NTGRkWh_plus_MEBens if is_net else 1.0) * ir_x_rr_kwh
            return elec_quantity_factor * sums["co2"], elec_quantity_factor * sums["nox"], elec_quantity_factor * sums["pm10"]

        def gas(therms, per_therm, is_net):
            return NumUnits * (NTGRTherm_plus_MEBens if is_net else 1.0) * ir_x_rr_therm * per_therm * therms

        row = {"CEInputID": m.CEInputID}
        for scope, sums, therms in (("FirstYear", first_year, first_year_therms), ("Lifecycle", lifecycle, lifecycle_therms)):
            for is_net, net_or_gross in ((True, "Net"), (False, "Gross")):
                co2, nox, pm10 = elec(sums, is_net)
                row[f"{scope}{net_or_gross}ElecCO2"] = co2
                row[f"{scope}{net_or_gross}ElecNOx"] = nox
                row[f"{scope}{net_or_gross}ElecPM10"] = pm10
                row[f"{scope}{net_or_gross}GasCO2"] = gas(therms, CO2Gas, is_net)
                row[f"{scope}{net_or_gross}GasNOx"] = gas(therms, combustion_nox_per_therm, is_net)
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.groupby("CEInputID", sort=False).sum().reset_index() if len(out) and out["CEInputID"].duplicated().any() else out


# ------------------------------------------------------------------------------------------------------------------------------------------------------------
# The array version of price_emissions_per_measure (the reference: read it for the CalcEmissions mechanics; tests/test_performance.py requires the two to agree).
# Per route (IOU_AC_Territory, AC_Elec_Target_Sector, profile, AC_Climate_Zone) the quarterly CO2 series is built once; each measure's sum over the quarters of its life is then a matrix
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
    nox_pm10_by_key, nox_by_combustion_type = _nox_pm10_index(acc_version), _combustion_nox()
    m = run_poc.split_by_baseline_shape(measures).reset_index(drop=True)
    n = len(m)
    if n == 0:
        return price_emissions_per_measure(measures, acc_version, market_effects_bens)

    def _floats(c):
        return m[c].astype(float).to_numpy()

    NumUnits = _floats("NumUnits")
    ir_x_rr_kwh = _floats("InstallationRatekWh") * _floats("RealizationRatekWh")
    ir_x_rr_therm = _floats("InstallationRateTherm") * _floats("RealizationRateTherm")
    NTGRkWh, NTGRTherm = _floats("NTGRkWh"), _floats("NTGRTherm")
    kwh_per_unit_1st, kwh_per_unit_2nd = _floats("Used_UnitkWh1stBaseline"), _floats("Used_UnitkWh2ndBaseline")
    UnitTherm1stBaseline, UnitTherm2ndBaseline = _floats("UnitTherm1stBaseline"), _floats("UnitTherm2ndBaseline")
    EUL_Yrs = _floats("EUL_Yrs")
    RUL_Yrs = _floats("RUL_Yrs")
    rul_for_emissions = np.where(RUL_Yrs < EUL_Yrs, RUL_Yrs, 0.0)                              # CalcCE's InputMeasurevw: RUL >= EUL counts as no RUL
    rul_quarters, eul_quarters = rul_for_emissions * 4.0, EUL_Yrs * 4.0                       # CalcEmissions' RULq / EULq
    install_quarter_index = m["Used_Year"].astype(int).to_numpy() * 4 + m["Used_Quarter"].astype(int).to_numpy()   # the claim quarter's Qidx
    IOU_AC_Territory = m["IOU_AC_Territory"].astype(str).to_numpy()
    AC_Elec_Target_Sector = m["AC_Elec_Target_Sector"].astype(str).to_numpy()
    Used_AC_Elec_Impact_Profile = m["Used_AC_Elec_Impact_Profile"].astype(str).to_numpy()
    AC_Climate_Zone = m["AC_Climate_Zone"].astype(str).to_numpy()
    routes = pd.Series(list(zip(IOU_AC_Territory, AC_Elec_Target_Sector, Used_AC_Elec_Impact_Profile, AC_Climate_Zone)))
    route_code, distinct_routes = pd.factorize(routes)

    on_kwh_per_unit_profile = np.zeros(n, dtype=bool)
    nox_per_kwh, pm10_per_kwh = np.zeros(n), np.zeros(n)
    sums = {k: np.zeros(n) for k in ("first_year_co2_per_unit", "lifecycle_co2_per_unit", "first_year_kwh_per_unit", "lifecycle_kwh_per_unit")}
    raw_iou = m["IOU_AC_Territory"].to_numpy(dtype=object)
    raw_sector = m["AC_Elec_Target_Sector"].to_numpy(dtype=object)
    raw_profile = m["Used_AC_Elec_Impact_Profile"].to_numpy(dtype=object)
    for r in range(len(distinct_routes)):
        route_rows = np.flatnonzero(route_code == r)
        route_first_row = route_rows[0]                       # the lookups get the group's own values, as the reference passes them (a blank profile stays blank)
        r_iou, r_sector, r_profile, r_zone = raw_iou[route_first_row], raw_sector[route_first_row], raw_profile[route_first_row], AC_Climate_Zone[route_first_row]
        nox, pm10 = nox_pm10_by_key.get((r_iou, str(r_sector).upper(), r_profile, r_zone.upper()), (0.0, 0.0))
        nox_per_kwh[route_rows], pm10_per_kwh[route_rows] = nox, pm10
        if _is_kwh_per_unit(r_iou, r_sector, r_profile, r_zone):
            on_kwh_per_unit_profile[route_rows] = True
        co2_quarters = _hourly_co2_quarters(r_iou, r_sector, r_profile, r_zone)
        if not co2_quarters:
            continue
        quarter_index = np.array([q for q, _ in co2_quarters])
        co2_tonnes_per_kwh = np.array([c for _, c in co2_quarters])
        for start in range(0, len(route_rows), CHUNK):
            chunk_rows = route_rows[start:start + CHUNK]
            kwh_1st, kwh_2nd = kwh_per_unit_1st[chunk_rows].copy(), kwh_per_unit_2nd[chunk_rows].copy()
            if on_kwh_per_unit_profile[chunk_rows].any():          # the profile is kWh per unit per hour: see price_emissions_per_measure
                on_profile = on_kwh_per_unit_profile[chunk_rows]
                units_x_ntg_x_ir_x_rr = NumUnits[chunk_rows] * NTGRkWh[chunk_rows] * ir_x_rr_kwh[chunk_rows]
                scale_1st = _floats("AC_Net_kWh_ScaledProfile_1st_Baseline")[chunk_rows]
                scale_2nd = _floats("AC_Net_kWh_ScaledProfile_2nd_Baseline")[chunk_rows]
                with np.errstate(divide="ignore", invalid="ignore"):
                    kwh_1st = np.where(on_profile, np.where(units_x_ntg_x_ir_x_rr != 0, scale_1st / units_x_ntg_x_ir_x_rr, 1.0), kwh_1st)
                    kwh_2nd = np.where(on_profile, np.where(units_x_ntg_x_ir_x_rr != 0, scale_2nd / units_x_ntg_x_ir_x_rr, 1.0), kwh_2nd)
            quarters_since_install = quarter_index[None, :] - install_quarter_index[chunk_rows, None]
            in_life = (quarters_since_install >= 0) & (quarters_since_install < eul_quarters[chunk_rows, None])
            in_first_year = in_life & (np.cumsum(in_life, axis=1) <= 4)
            AnnualkWh = _annual_kwh_array(quarters_since_install, kwh_1st, kwh_2nd, rul_quarters[chunk_rows], eul_quarters[chunk_rows])
            sums["lifecycle_co2_per_unit"][chunk_rows] = (np.where(in_life, AnnualkWh, 0.0) * co2_tonnes_per_kwh).sum(axis=1)
            sums["first_year_co2_per_unit"][chunk_rows] = (np.where(in_first_year, AnnualkWh, 0.0) * co2_tonnes_per_kwh).sum(axis=1)
            sums["lifecycle_kwh_per_unit"][chunk_rows] = np.where(in_life, AnnualkWh, 0.0).sum(axis=1)
            sums["first_year_kwh_per_unit"][chunk_rows] = np.where(in_first_year, AnnualkWh, 0.0).sum(axis=1)

    CO2Gas = m["IOU_AC_Territory"].map({i: discount_settings.get_co2_gas(i, acc_version) for i in m["IOU_AC_Territory"].unique()}).astype(float).to_numpy()
    CombustionType = m["CombustionType"]
    combustion_nox_per_therm = (np.array([nox_by_combustion_type.get(c, 0.0) if isinstance(c, str) else 0.0 for c in CombustionType.to_numpy()])
                                if len(CombustionType) else np.zeros(n))
    rul, eul, thm1, thm2 = rul_for_emissions, EUL_Yrs, UnitTherm1stBaseline, UnitTherm2ndBaseline
    first_year_therms_per_unit = np.select([(rul > 0) & (rul >= 1), (rul > 0) & (eul >= 1), rul > 0, (eul > 0) & (eul > 1), eul > 0],
                                           [thm1, rul * thm1 + (1 - rul) * thm2, rul * thm1 + (eul - rul) * thm2, thm1, eul * thm1], default=0.0)
    lifecycle_therms_per_unit = np.select([rul > 0, eul > 0], [rul * thm1 + (eul - rul) * thm2, eul * thm1], default=0.0)

    out = {"CEInputID": m["CEInputID"].to_numpy()}
    for scope, co2_per_unit, kwh_per_unit, therms_per_unit in (
            ("FirstYear", sums["first_year_co2_per_unit"], sums["first_year_kwh_per_unit"], first_year_therms_per_unit),
            ("Lifecycle", sums["lifecycle_co2_per_unit"], sums["lifecycle_kwh_per_unit"], lifecycle_therms_per_unit)):
        for is_net, net_or_gross in ((True, "Net"), (False, "Gross")):
            elec_quantity_factor = NumUnits * ((NTGRkWh + market_effects_bens) if is_net else 1.0) * ir_x_rr_kwh
            gas_quantity_factor = NumUnits * ((NTGRTherm + market_effects_bens) if is_net else 1.0) * ir_x_rr_therm
            out[f"{scope}{net_or_gross}ElecCO2"] = elec_quantity_factor * co2_per_unit
            out[f"{scope}{net_or_gross}ElecNOx"] = elec_quantity_factor * nox_per_kwh * kwh_per_unit / 4.0
            out[f"{scope}{net_or_gross}ElecPM10"] = elec_quantity_factor * pm10_per_kwh * kwh_per_unit / 4.0
            out[f"{scope}{net_or_gross}GasCO2"] = gas_quantity_factor * CO2Gas * therms_per_unit
            out[f"{scope}{net_or_gross}GasNOx"] = gas_quantity_factor * combustion_nox_per_therm * therms_per_unit
    res = pd.DataFrame(out)
    return res.groupby("CEInputID", sort=False).sum().reset_index() if res["CEInputID"].duplicated().any() else res
