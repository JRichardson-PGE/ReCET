"""NOT USED BY THE POC'S PRICING since 2026-10-02 (Jake: the 8,760 path only): TOU end uses are priced from the Gen-based
TOU 8,760s in the impact-profile library (poc/tou/add_tou_8760_to_library.py). Kept for the research scripts under poc/tou/.


TOU (time-of-use) impact profiles: reproduce CEDARS' quarterly Gen / T&D values from H-factors, and build an 8,760
shape. Reverse-engineered 2026-09-19 from CEDARS output (see poc/tou/README.md for the evidence); PG&E and SCE.

How CEDARS computes a TOU profile (established, not documented anywhere):
  1. Each TOU period P (summer peak/partial/off, winter partial/off) has a fixed hourly membership on a 1991 calendar
     (data/tou_maps/*_map_1991.npy: 8760 ints, 1=SmrOn 2=SmrPtl 3=SmrOff 5=WtrPtl 6=WtrOff).
  2. The map is applied to the ACC's 8760 hour index with a fixed shift of +25 hours (+1 day, +1 hour), the same every
     year (the +1 hour is the ACC's DST label offset; the +1 day is an apparent weekday misalignment - see README).
  3. Each period gets ONE price per year: the mean of the ACC hourly price over the period's hours (hourly detail is
     discarded). A quarter's value is that price times the share of the period's hours that fall in the quarter (f).
  4. Gen (kWh-share):  sum_P share_P * f(P,q) * mean_P(Gen $/kWh)
     T&D (PG&E residential, kW per annual kWh factors): sum_P kwPerKwh_P * f(P,q) * mean_P(T&D $/kWh)
     T&D (PG&E commercial, SCE; relative-kW factors):   0.001 * sum_P kwRel_P * f(P,q) * sum_{h in P}(T&D $/kWh)
"""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
import numpy as np
import pandas as pd

from .. import paths
MAP_DIR = paths.package_data("tou")
QUARTER_BOUNDS = [(0, 2160), (2160, 4344), (4344, 6552), (6552, 8760)]   # ACC's real 2018 quarters
PERIODS = (1, 2, 3, 5, 6)          # SmrOn, SmrPtl, SmrOff, WtrPtl, WtrOff (period 4, winter on-peak, has no hours)
ALIGN_SHIFT_HOURS = 25             # map index = ACC index + 25
KAPPA = 0.001                      # T&D scale for relative-kW factors = 1/1000 exactly ($/MWh -> $/kWh on the period-SUMMED T&D). Not fitted: it gives PG&E
                                   # commercial T&D 0.005% error vs CEDARS (an earlier fitted 0.000997 gave 0.30%)

def load_map(iou: str) -> np.ndarray:
    """1991-calendar period map (8760 ints) for 'PGE' or 'SCE'."""
    return np.load(MAP_DIR / f"{iou.lower()}_map_1991.npy")

def aligned_map(iou: str) -> np.ndarray:
    """Period of each ACC hour index (what CEDARS effectively uses)."""
    return np.roll(load_map(iou), -ALIGN_SHIFT_HOURS)

def quarter_fractions(iou: str) -> dict[int, np.ndarray]:
    """f(P,q): share of period P's hours in each quarter, on the (unshifted) 1991 map."""
    m = load_map(iou)
    return {p: np.array([(m[a:b] == p).sum() for a, b in QUARTER_BOUNDS]) / (m == p).sum() for p in PERIODS}

def period_hours(iou: str) -> dict[int, int]:
    m = load_map(iou); return {p: int((m == p).sum()) for p in PERIODS}

def shape_8760(shares, iou: str) -> np.ndarray:
    """8,760 energy shape (sums to 1): each period's share spread evenly over that period's hours, on ACC hour index.
    CEDARS itself multiplies the period-MEAN price by these shares, so applying this shape to hourly prices is more
    accurate than CEDARS but will not match it."""
    m = aligned_map(iou); out = np.zeros(8760)
    for share, p in zip(shares, PERIODS):
        sel = m == p
        if sel.any(): out[sel] = share / sel.sum()
    return out

def quarterly(iou: str, factors, series: np.ndarray, kind: str) -> np.ndarray:
    """Quarterly values, shape (years, 4). series is (years, 8760) in $/kWh.
    kind: 'gen' or 'td_per_kwh' (factor x mean price), or 'td_rel_kw' (KAPPA x factor x period-sum price)."""
    m = aligned_map(iou); fr = quarter_fractions(iou); out = 0
    for fac, p in zip(factors, PERIODS):
        sel = m == p
        if not sel.any(): continue
        v = series[:, sel].sum(1) * KAPPA if kind == "td_rel_kw" else series[:, sel].mean(1)
        out = out + fac * np.outer(v, fr[p])
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Factor library + per-measure quarterly rates (used by run_poc.run_real_hourly / emissions for measures with no hourly profile)
FACTORS_CSV = MAP_DIR / "tou_factors.csv"     # built by poc/tou/build_factors.py from the E3 viewer files ONLY (no CEDARS/CET-derived values)
_ZONE_SUBSTITUTE = {"SCE": {"5": "9"}}        # same documented stand-in as real_avoided_cost_v2.MISSING_ZONE_SUBSTITUTE


@lru_cache(maxsize=1)
def _factor_index() -> dict[tuple[str, str, str, str], dict]:
    df = pd.read_csv(FACTORS_CSV, dtype={"zone": str})
    df = df[df["status"] == "ok"]      # rows whose energy shares do not sum to 1 (e.g. SCE Residential Pool Pumps) are never used
    return {(r["pa"], str(r["target_sector"]).lower(), str(r["end_use"]).lower(), r["zone"]): r for r in df.to_dict("records")}


def lookup(pa: str, target_sector: str, end_use: str, climate_zone: str) -> dict | None:
    """Factor row for (PA, target sector, end use, zone) or None. PG&E rows have no zone dimension ('All'). Only PGE and SCE
    TOU profiles are supported (SDG&E's map is unresolved; SCG measures use CET's SCG table instead)."""
    if pa not in ("PGE", "SCE"):
        return None
    idx, ts, eu, z = _factor_index(), str(target_sector).lower(), str(end_use).lower(), str(climate_zone)
    for zone in (z, "All", _ZONE_SUBSTITUTE.get(pa, {}).get(z, z)):
        row = idx.get((pa, ts, eu, zone))
        if row is not None:
            return row
    return None


def _factors(row: dict) -> tuple[list[float], list[float]]:
    return [float(row[f"e{i}"]) for i in range(5)], [float(row[f"kw{i}"]) for i in range(5)]


@lru_cache(maxsize=512)
def system_zone(pa: str, target_sector: str, end_use: str) -> str | None:
    """CEDARS' generic 'System' climate zone for a TOU profile is NOT a blend: it repeats the T&D of the one real zone with the
    LOWEST total T&D (found 2026-09-20: true for 63/63 PG&E and 385/386 SCE TOU profiles in CEDARS' quarterly table; Gen is
    identical in every zone). Returns that zone for (pa, sector, end use), chosen on TRC T&D summed over all quarters."""
    from . import real_avoided_cost_v2 as acc
    iou = acc.PA_TO_IOU_LABEL[pa]
    best, best_total = None, None
    for cz_label in acc._covered_zones()[iou]:
        zone = cz_label.replace("CZ", "")
        r = quarterly_rates(pa, target_sector, end_use, zone, "TRC")
        if r is None:
            continue
        total = float(r["td"].sum())
        if best_total is None or total < best_total:
            best, best_total = zone, total
    return best


@lru_cache(maxsize=4096)
def quarterly_rates(pa: str, target_sector: str, end_use: str, climate_zone: str, cost_test: str = "TRC", component: str | None = None) -> dict | None:
    """Quarterly TOU avoided-cost rates for one measure key, or None if it is not a supported TOU profile.
    Returns {years, gen (Y,4) $/kWh, td (Y,4), ds_type ('kW'|'kWh'), source}. `gen` multiplies kWh savings; `td` multiplies
    DS = kW savings if ds_type == 'kW' else kWh savings (CalcCE's AvoidedCostElecvw DS1/DS2).
    component=<ACC component column>: the rates of that component alone (transmission/distribution fill `td`, every other component fills `gen`),
    so the components' priced values add up to the total's. The System-zone choice never depends on the component."""
    if str(climate_zone) == "System":
        zone = system_zone(pa, target_sector, end_use)
        return None if zone is None else quarterly_rates(pa, target_sector, end_use, zone, cost_test, component)
    row = lookup(pa, target_sector, end_use, climate_zone)
    if row is None:
        return None
    from . import real_avoided_cost_v2 as acc
    gen_by_year, td_by_year = acc.get_gen_td_series(pa, climate_zone, cost_test, component)
    years = sorted(gen_by_year)
    G = np.array([gen_by_year[y] for y in years]); TD = np.array([td_by_year[y] for y in years])
    shares, kw = _factors(row)
    kind = "td_per_kwh" if row["kw_form"] == "per_kwh" else "td_rel_kw"
    return {"years": years, "gen": quarterly(pa, shares, G, "gen"), "td": quarterly(pa, kw, TD, kind),
            "ds_type": row["ds_type"], "source": row["source"]}


@lru_cache(maxsize=4096)
def co2_quarterly(pa: str, target_sector: str, end_use: str, climate_zone: str) -> tuple[list[int], np.ndarray] | None:
    """(years, (Y,4) tonnes CO2 per kWh) for a TOU profile: energy shares applied to the ACC's hourly grid emission rate
    (period-mean, same convention as Gen)."""
    if str(climate_zone) == "System":
        climate_zone = system_zone(pa, target_sector, end_use)
        if climate_zone is None:
            return None
    row = lookup(pa, target_sector, end_use, climate_zone)
    if row is None:
        return None
    from . import real_avoided_cost_v2 as acc
    rates = acc.get_emission_rate_series(pa, climate_zone)
    years = sorted(rates)
    shares, _ = _factors(row)
    return years, quarterly(pa, shares, np.array([rates[y] for y in years]), "gen")
