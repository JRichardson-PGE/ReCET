"""The "System" climate zone: which real climate zone a measure with E3ClimateZone = System is priced in.

RULE (Jake, 2026-10-09). A profile's System zone is the climate zone, out of the real zones that profile has, whose shape and avoided costs give the LOWEST
total electric avoided cost over the full length of the avoided cost data:

    total(z) = sum over every year y in the ACC file of  sum over hours h of  shape_z[h] x total_avoided_cost(PA, z, TRC, y, h)

where shape_z is the profile's own normalized 8,760 for zone z (it sums to 1, so this is $ per annual kWh), re-aligned once to the ACC's weekdays (2018, or
2020 for the 2021/2022 ACCs) exactly as the pricing does (real_impact_profiles.shift_to_acc), and total_avoided_cost is the ACC's Gen + T&D for the measure's PA in zone z, resolved by the same rules
every other price lookup uses (real_avoided_cost_v2.get_acc_series: SCG routing, the documented SCE CZ5 / SDG&E CZ6 / CZ8 stand-ins). Undiscounted. The TRC
cost test decides the zone and that zone is then used for every cost test and component, so a measure's SCT and component values are priced in the same zone
as its TRC value. A zone whose prices the ACC cannot supply (the old blended fallback) is not an option. Ties go to the first zone in natural order (1, 2, 3A,
3B, 4, ... 16); a tie only happens when two zones have the same shape and the same prices, so the choice does not change any value.

The System zone replaces BOTH the shape and the prices: the measure is priced exactly as if its E3ClimateZone were the chosen zone. The library's own
"System" rows (CEDARS' published System shapes) are no longer used for System measures; they remain only as get_shape()'s fallback for a real zone a profile
does not have.

A choice depends only on the profile and the ACC file, so it is CATALOGED: recet/data/system_zones/system_zones.csv.gz holds the choice for every library
profile with hourly data under each built-in ACC version (build: `recet data build-system-zones`, about 4 minutes). A run uses the catalog when it prices a
library profile against a built-in ACC file that matches the one the catalog was built from (build_info.json records file sizes), and calculates the choice on
the fly otherwise -- a user-supplied 8,760 profile, a user-supplied ACC file, or reference data that differs from the catalog's. The calculation is the same
function either way (zone_totals), so a catalog entry and an on-the-fly choice can never disagree.

User-supplied profiles: the options are the zones the file gives shapes for. A file entry with only ALL (or SYSTEM) rows uses that one shape for every zone the
measure's PA has prices for, so the choice is then decided by the prices alone. A user file's own SYSTEM rows are not used as a System measure's shape.

NOx and PM10 are unaffected: CET looks them up in E3Emissions by the measure's literal climate zone, which has System rows (pipeline/emissions.py).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from .. import paths
from . import real_avoided_cost_v2 as acc
from . import real_impact_profiles as rip

SYSTEM = "SYSTEM"
DECIDING_COST_TEST = "TRC"
PAS = ("PGE", "SCE", "SDGE", "SCG")
CATALOG_NAME = "system_zones.csv.gz"
BUILD_INFO_NAME = "build_info.json"


def is_system(zone) -> bool:
    return str(zone).strip().upper() == SYSTEM


def zone_sort_key(zone: str) -> tuple[int, str]:
    m = re.fullmatch(r"(\d+)(.*)", str(zone))
    return (int(m.group(1)), m.group(2)) if m else (10_000, str(zone))


@dataclass(frozen=True)
class Choice:
    zone: str | None                                     # the System zone, or None when no zone could be priced
    source: str                                          # "catalog" or "calculated (...)"
    totals: dict[str, float] = field(default_factory=dict, compare=False)   # zone -> total ($ per annual kWh, undiscounted, all ACC years); empty for catalog hits


# ----------------------------------------------------------------------------------------------------------------------------- the calculation
@lru_cache(maxsize=256)
def price_sum(pa: str, zone: str) -> np.ndarray | None:
    """Sum over every ACC year of the TRC total avoided cost ($/kWh) for one PA and zone, hour by hour; None when the ACC has no prices for that zone (the
    blended fallback is never an option). Uses the run's current electric ACC file."""
    series, blended = acc.get_acc_series(pa, zone, DECIDING_COST_TEST)
    if blended or not series:
        return None
    return np.sum(np.stack([series[y] for y in sorted(series)]), axis=0)


def priceable_zones(pa: str) -> list[str]:
    """Every zone the PA has prices for in the current ACC (its own covered zones, the documented stand-ins, and for SCG the zones its routing reaches)."""
    cands = {z.removeprefix("CZ") for zones in acc._covered_zones().values() for z in zones} | {"5", "6", "8"}
    return sorted((z for z in cands if price_sum(pa, z) is not None), key=zone_sort_key)


def zone_totals(pa: str, shapes: dict[str, np.ndarray], represents_year: int, sums=None, acc_year: int | None = None) -> dict[str, float]:
    """{zone: total} for every option: the profile's real zones (System excluded) that the ACC can price. `sums` (zone -> price sum) and `acc_year` (that
    ACC's calendar) are only passed by the catalog build, which holds several ACC versions at once; otherwise the current ACC is read through price_sum() and
    its calendar through real_impact_profiles.acc_calendar_year()."""
    out: dict[str, float] = {}
    for z in sorted((z for z in shapes if not is_system(z)), key=zone_sort_key):
        s = sums.get(z) if sums is not None else price_sum(pa, z)
        if s is None:
            continue
        out[z] = float(rip.shift_to_acc(shapes[z], represents_year, acc_year) @ s)
    return out


def pick(totals: dict[str, float]) -> str | None:
    """The lowest total; ties go to the first zone in natural order."""
    if not totals:
        return None
    best = min(totals.values())
    return min((z for z, v in totals.items() if v == best), key=zone_sort_key)


# ----------------------------------------------------------------------------------------------------------------------------- the catalog
def catalog_dir() -> Path:
    return paths.package_data("system_zones")


def _builtin_acc_key() -> str | None:
    """The built-in ACC version whose file the run is using, or None for a user-supplied ACC."""
    from .. import versions
    cur = paths.current_acc_electric()
    if cur is None:
        return None
    for v in versions.BUILTIN.values():
        p = paths.ref_path(v.electric_key)
        if p is not None and p.exists() and p.resolve() == Path(cur).resolve():
            return v.key
    return None


@lru_cache(maxsize=1)
def _build_info() -> dict:
    f = catalog_dir() / BUILD_INFO_NAME
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


@lru_cache(maxsize=1)
def _catalog() -> dict[tuple[str, int], str]:
    f = catalog_dir() / CATALOG_NAME
    if not f.exists():
        return {}
    df = pd.read_csv(f, dtype={"acc_version": str, "system_zone": str})
    return dict(zip(zip(df["acc_version"], df["profile_id"].astype(int)), df["system_zone"]))


@lru_cache(maxsize=1)
def catalog_usable() -> tuple[str | None, str]:
    """(built-in ACC key the catalog can be used for, reason if it cannot). The catalog applies only when the run's ACC file and the profile library are the
    files it was built from (same sizes as recorded in build_info.json)."""
    key = _builtin_acc_key()
    if key is None:
        return None, "supplied ACC"
    info = _build_info()
    if not info or key not in info.get("acc", {}):
        return None, "no catalog for this ACC version"
    lib = paths.ref_path("impact_hourly")
    acc_file = paths.current_acc_electric()
    if lib is None or lib.stat().st_size != info.get("library_bytes") or Path(acc_file).stat().st_size != info["acc"][key]["electric_bytes"]:
        return None, "reference data differs from the catalog's"
    return key, ""


# ----------------------------------------------------------------------------------------------------------------------------- resolving a measure
def _user_options(entry, pa: str) -> dict[str, np.ndarray]:
    real = {z: s for z, s in entry.zones.items() if z not in (SYSTEM, "ALL")}
    if real:
        return real
    shape = entry.zones.get("ALL", entry.zones.get(SYSTEM))
    return {} if shape is None else {z: shape for z in priceable_zones(pa)}


@lru_cache(maxsize=4096)
def resolve(shape_key: str, pa: str, target_sector: str) -> Choice:
    """The System zone for a measure's profile (E3MeaElecEndUseShape, IOU_AC_Territory, E3TargetSector) under the run's ACC -- the same profile the measure's
    real-zone lookups would use (a matching user profile first, then the library)."""
    user = rip.user_profiles()
    entry = user.match(shape_key, pa, target_sector) if user is not None else None
    if entry is not None:
        totals = zone_totals(pa, _user_options(entry, pa), entry.represents_year)
        return Choice(pick(totals), "calculated (user profile)", totals)
    pid, _exact = rip.match_profile_id(shape_key, pa, target_sector)
    if pid is None:
        return Choice(None, "no profile")
    acc_key, why = catalog_usable()
    if acc_key is not None:
        hit = _catalog().get((acc_key, int(pid)))
        if hit is not None and not pd.isna(hit):
            return Choice(str(hit), "catalog")
        why = why or "profile not in catalog"
    totals = zone_totals(pa, rip._profile_shapes_by_zone(pid), rip.profile_meta(pid)["represents_year"])
    return Choice(pick(totals), f"calculated ({why})", totals)


def priced_zone(shape_key: str, pa: str, target_sector: str, climate_zone: str) -> str:
    """The zone a measure is priced in: its own E3ClimateZone, or for System the resolved zone. If no zone can be resolved the measure keeps "System" and
    falls back to the old behaviour (the library's System shape, the PA's blended prices)."""
    if not is_system(climate_zone):
        return climate_zone
    return resolve(shape_key, pa, target_sector).zone or climate_zone


# ----------------------------------------------------------------------------------------------------------------------------- building the catalog
def build_catalog(out_dir: Path | None = None, version_keys: tuple[str, ...] | None = None, log=print) -> Path:
    """Write system_zones.csv.gz and build_info.json: the System zone of every library profile with hourly data, under each built-in ACC version.

    Columns: acc_version, profile_id, pa, target_sector, end_use_name, represents_year, system_zone, system_zone_total, runner_up_zone, runner_up_total,
    n_options, zone_totals ("zone=total;..." for every option, $ per annual kWh, undiscounted, all ACC years). A profile none of whose zones can be priced has a
    blank system_zone."""
    from .. import runtime, versions
    out_dir = Path(out_dir) if out_dir else catalog_dir()
    keys = version_keys or tuple(versions.BUILTIN)
    lib = paths.ref_path("impact_hourly")
    if lib is None:
        raise FileNotFoundError("profile library not found: set the reference data folder")

    sums: dict[str, dict[tuple[str, str], np.ndarray]] = {}
    calendars: dict[str, int] = {}
    info = {"library_bytes": lib.stat().st_size, "deciding_cost_test": DECIDING_COST_TEST, "acc": {}}
    saved = paths._runtime["acc_electric"]
    try:
        for k in keys:
            f = paths.ref_path(versions.BUILTIN[k].electric_key)
            if f is None:
                raise FileNotFoundError(f"built-in ACC {k} electric file not found")
            paths.configure_runtime(acc_electric=f)
            runtime.clear_caches()
            sums[k] = {(pa, z): price_sum(pa, z) for pa in PAS for z in priceable_zones(pa)}
            years = sorted(acc.get_acc_series("PGE", "1", DECIDING_COST_TEST)[0])
            calendars[k] = rip.acc_calendar_year()                      # 2018 for 2024/2026, 2020 for 2021/2022: the profiles are aligned to it below
            info["acc"][k] = {"electric_bytes": f.stat().st_size, "years": [years[0], years[-1]], "calendar_year": calendars[k]}
            log(f"ACC {k}: price sums for {len(sums[k])} PA x zone options, years {years[0]}-{years[-1]}")
    finally:
        paths.configure_runtime(acc_electric=saved)
        runtime.clear_caches()

    import pyarrow.parquet as pq
    cat = rip._catalog().set_index("id")
    index = rip._row_group_index()
    pf = pq.ParquetFile(str(lib))
    rows = []
    for n, (pid, gi) in enumerate(sorted(index.items())):
        if pid not in cat.index:
            continue
        meta = cat.loc[pid]
        pa = str(meta["iou_ac_territory"])
        if pa not in PAS:
            continue
        shapes = rip.shapes_from_frame(pf.read_row_group(gi).to_pandas())
        ry = int(meta["represents_year"])
        for k in keys:
            s = {z: sums[k].get((pa, z)) for z in shapes}
            totals = zone_totals(pa, shapes, ry, sums=s, acc_year=calendars[k])
            zone = pick(totals)
            ranked = sorted(totals, key=lambda z: (totals[z], zone_sort_key(z)))
            rows.append(dict(acc_version=k, profile_id=int(pid), pa=pa, target_sector=meta["target_sector"], end_use_name=meta["end_use_name"],
                             represents_year=ry, system_zone=zone, system_zone_total=totals.get(zone, np.nan) if zone else np.nan,
                             runner_up_zone=ranked[1] if len(ranked) > 1 else None, runner_up_total=totals[ranked[1]] if len(ranked) > 1 else np.nan,
                             n_options=len(totals), zone_totals=";".join(f"{z}={totals[z]:.10g}" for z in sorted(totals, key=zone_sort_key))))
        if (n + 1) % 2000 == 0:
            log(f"  {n + 1:,} / {len(index):,} profiles")
    df = pd.DataFrame(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / CATALOG_NAME, index=False, float_format="%.10g")
    info["profiles"] = int(df["profile_id"].nunique())
    (out_dir / BUILD_INFO_NAME).write_text(json.dumps(info, indent=2), encoding="utf-8")
    _catalog.cache_clear()
    _build_info.cache_clear()
    catalog_usable.cache_clear()
    log(f"wrote {out_dir / CATALOG_NAME}: {len(df):,} rows ({info['profiles']:,} profiles x {len(keys)} ACC versions); "
        f"{int(df['system_zone'].isna().sum())} rows with no priceable zone")
    return out_dir / CATALOG_NAME
