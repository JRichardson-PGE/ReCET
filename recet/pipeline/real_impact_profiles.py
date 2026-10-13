"""
Real per-measure hourly load shapes, pulled from the merged CEDARS Impact
Profile Library (poc/merge_all_ious.py's output covering all 4 IOUs,
19,374 real profiles x their real hourly shape per real climate zone),
replacing the first proof of concept's randomly-generated placeholder
shapes (synthetic_curves, now in the CEDARS review research repository's poc/research_lib).

MATCHING a CET Measure.csv row to a real CEDARS impact profile: CET's own
E3MeaElecEndUseShape field ("28 = Res. New Const. Lighting", "DEER:HVAC_Eff_AC",
etc.) is the EXACT same string CEDARS' own catalog stores as `end_use_name`
-- confirmed by direct comparison, not assumed. Combined with the measure's
IOU_AC_Territory and E3TargetSector, (end_use_name, iou_ac_territory,
target_sector) is a UNIQUE key across the whole 20,497-profile catalog
(verified: zero duplicate keys) -- no version-selection heuristic needed
here, unlike CEDARS' quarterly/hourly value-list tables elsewhere in this
review that do need start_year/end_year filtering.

This matches 4,937 of 5,049 measures (97.8%) in the real ~5,000-measure
statewide filing on the exact 3-key match; the rest fall back to a looser
(end_use_name, iou) match ignoring sector (a few dozen measures using
generic "Industrial"/"Commercial"-style sector labels that don't line up
with CEDARS' own target_sector spelling for that catalog entry), or, if
even that misses, to the caller's own synthetic fallback -- logged via
match_profile_id()'s return value, never silent.

Once matched, the profile's raw `impact` column (real per-hour data,
NORMALIZED -- sums to ~1.0 per climate zone, confirmed empirically) is
exactly the same shape contract the synthetic placeholder shapes
promised the pricing engine: a length-8760 array summing to ~1.0,
meant to be scaled by the measure's own annual_net_kwh at pricing time --
nothing in engine.py needs to know the data source changed, only which
functions build the (shape, represents_year) pair per measure.

DAY-OF-WEEK SHIFT (2026-10-02, Jake: the 8,760 path with the correct shift, not CEDARS' errors). A profile is drawn on its own represents_year's calendar
(the catalog's `year_represented`; 2009, the DEER reference year, when blank; 2025 for the TOU H-factor 8,760s), while the ACC's hourly data follow 2018's
weekdays in every forecast year (ACC_CALENDAR_YEAR). shift_for_year() therefore rotates every profile once, by the signed weekday difference to 2018 taken
the short way round (-3..+3 days), and the same aligned shape prices every year. CEDARS instead aligns each forecast year to that year's real Jan-1 weekday
and rotates one day too far for negative offsets; that rule reproduces CEDARS' published quarterly values exactly (kept as cedars_shift_for_year, reference
only) but is not what the POC prices with.

KNOWN DEER DISCREPANCY (same finding, not re-litigated here): DEER-named
profiles (end_use_name starting "DEER:") do not fit this formula as well
as other profiles even with the documented reference year -- flagged via
profile_meta()'s `is_deer`, not special-cased or excluded.
"""
from __future__ import annotations

import datetime
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .. import paths

HOURS_PER_YEAR = 8760
DEFAULT_REPRESENTS_YEAR = 2009  # DEER convention; also the observed catalog default for null year_represented


def _catalog_path() -> Path:
    p = paths.ref_path("impact_catalog")
    if p is None:
        raise FileNotFoundError("CEDARS impact-profile catalog not found: set the reference data folder (see recet/paths.py)")
    return p


def _hourly_path() -> Path:
    p = paths.ref_path("impact_hourly")
    if p is None:
        raise FileNotFoundError("CEDARS hourly impact profiles not found: set the reference data folder (see recet/paths.py)")
    return p


def is_available() -> bool:
    return paths.ref_path("impact_catalog") is not None and paths.ref_path("impact_hourly") is not None


# --- user-supplied 8,760 profiles (recet/overrides.py): a profile that matches a library key REPLACES the library profile ---
_USER = None


def set_user_profiles(user) -> None:
    """`user` is an overrides.UserProfiles (or None to clear). Affects every later resolve_shape() call."""
    global _USER
    _USER = user


def user_profiles():
    return _USER


def resolve_shape(shape_key: str, iou: str, target_sector: str, climate_zone: str):
    """(normalized 8,760 shape, represents_year, source) for one measure, or None if nothing matches.

    A user-supplied profile that matches (end use, IOU, sector) wins over the library's; if it matches but has no shape for this climate zone
    (and no System/ALL zone) the library's profile is used instead and `source` says so. `source` is one of "user profile",
    "library", "library (sector-loose match)", "library (user profile has no shape for this zone)"."""
    zone_gap = False
    if _USER is not None:
        hit = _USER.lookup(shape_key, iou, target_sector, climate_zone)
        if isinstance(hit, str):          # "zone_missing"
            zone_gap = True
        elif hit is not None:
            shape, represents_year = hit
            return shape, represents_year, "user profile"
    pid, exact = match_profile_id(shape_key, iou, target_sector)
    if pid is None:
        return None
    shape = get_shape(pid, climate_zone)
    if shape is None:
        return None
    src = "library (user profile has no shape for this zone)" if zone_gap else ("library" if exact else "library (sector-loose match)")
    return shape, profile_meta(pid)["represents_year"], src


@lru_cache(maxsize=1)
def _catalog() -> pd.DataFrame:
    df = pd.read_parquet(_catalog_path())
    df["_sector_key"] = df["target_sector"].astype(str).str.upper()
    df["is_deer"] = df["end_use_name"].str.startswith("DEER:")
    df["represents_year"] = df["year_represented"].fillna(DEFAULT_REPRESENTS_YEAR).astype(int)
    return df


@lru_cache(maxsize=1)
def _row_group_index() -> dict[int, int]:
    """profile_id -> row-group index in the hourly parquet. Built entirely
    from parquet metadata (min==max per row group, since
    poc/consolidate_profiles.py / poc/repair_merged_hourly_rowgroups.py
    write exactly one row group per profile) -- no data is read, ~0.2s for
    the full 8.9GB/19,374-row-group file.

    Only 19,374 of the catalog's 20,497 profiles are in here. MOST of the
    remaining ~1,123 (all `source_impact_type == "calculated"`) genuinely
    have zero hourly rows -- found the hard way: the small 5-measure
    sample's own shape ("28 = Res. New Const. Lighting" / PGE /
    Residential) matches such a calculated-only profile (id 153) with zero
    hourly rows. _catalog_hourly() below excludes those from matching so
    callers never get a profile id back that then has no shape data.

    CORRECTION 2026-09-16: `source_impact_type == "calculated"` does NOT
    reliably mean "no hourly data" -- Jake caught this directly by opening
    a "calculated"-type profile (id 31, SCE/DEER:Res_ClothesDishWasher) on
    CEDARS' own site and finding real hourly data plainly present, which
    this module had wrongly reported as unavailable. Root cause: 21 of the
    19,374 profiles IN THIS INDEX (ids 23-43, all early SCE DEER-named
    "calculated"-type profiles) have real hourly rows that were corrupted
    by `merge_all_ious.py`'s original 1,000,000-row rebatching bug (see
    `repair_merged_hourly_rowgroups.py`'s docstring for that bug's general
    description) -- a handful of duplicate `hour_of_year` rows (one pair
    with genuinely conflicting `impact` values, not just an exact repeat)
    and one hour missing entirely per (profile, zone). The strict
    `len(sub) != 8760` check `_profile_shapes_by_zone()` used to have
    treated this as "no data" and silently excluded the whole profile --
    which is exactly why 100% of a real 73,024-measure filing's SCE-
    territory heat-pump-water-heater measures (all matching profile 31)
    fell back to a synthetic shape in a 2026-09-16 test run, when they
    should have used real data. Since the original per-IOU source files
    were already deleted (same cleanup pass noted in
    repair_merged_hourly_rowgroups.py's docstring), the true, uncorrupted
    values for the conflicting/missing hours can't be recovered --
    `_profile_shapes_by_zone()` now deduplicates and interpolates small
    gaps (documented there) rather than excluding these 21 profiles
    outright. This was NOT re-scanned for the OTHER ~1,102 "calculated"
    profiles with zero rows in this index at all -- there's no row group
    to repair for those, so the correction only applies to profiles that
    make it into `_row_group_index()` in the first place."""
    pf = pq.ParquetFile(str(_hourly_path()))
    md = pf.metadata
    index: dict[int, int] = {}
    for gi in range(md.num_row_groups):
        stats = md.row_group(gi).column(0).statistics
        index[int(stats.min)] = gi
    return index


@lru_cache(maxsize=1)
def _catalog_hourly() -> pd.DataFrame:
    """Catalog rows restricted to profiles that actually have hourly data
    -- see _row_group_index()'s docstring for why this exclusion matters."""
    cat = _catalog()
    hourly_ids = frozenset(_row_group_index().keys())
    return cat[cat["id"].isin(hourly_ids)]


@lru_cache(maxsize=1)
def _exact_key_index() -> dict[tuple[str, str, str], int]:
    """(end_use_name, iou_ac_territory, TARGET_SECTOR.upper()) -> profile id,
    restricted to profiles with real hourly data. Unique on the full
    catalog (verified, see module docstring); restricting to hourly-
    available profiles only ever removes keys, never creates a collision."""
    cat = _catalog_hourly()
    return dict(zip(zip(cat["end_use_name"], cat["iou_ac_territory"], cat["_sector_key"]), cat["id"]))


@lru_cache(maxsize=1)
def _loose_key_index() -> dict[tuple[str, str], int]:
    """(end_use_name, iou_ac_territory) -> first hourly-available matching
    profile id, ignoring sector -- fallback only, used when the exact
    3-key match misses or lands on a calculated-only (no hourly data)
    profile (see module docstring)."""
    cat = _catalog_hourly()
    out: dict[tuple[str, str], int] = {}
    for end_use, iou, pid in zip(cat["end_use_name"], cat["iou_ac_territory"], cat["id"]):
        out.setdefault((end_use, iou), pid)
    return out


MAX_REPAIRABLE_GAP_HOURS = 5  # see module docstring's "MERGE-PIPELINE DATA CORRUPTION" note


@lru_cache(maxsize=4096)
def _profile_shapes_by_zone(profile_id: int) -> dict[str, np.ndarray]:
    """All climate zones' normalized 8760-length shapes for one real
    profile, read from its single row group. Cached per profile_id since
    the same profile is commonly reused across many measures (e.g. many
    measures sharing one climate zone + end use).

    Repairs small merge-pipeline corruption in place (duplicate/missing
    hour_of_year rows -- see module docstring's "MERGE-PIPELINE DATA
    CORRUPTION" note) rather than rejecting the whole zone: exact-duplicate
    hours are deduplicated (keep first), and if at most
    MAX_REPAIRABLE_GAP_HOURS hours are still missing after that, they're
    filled by linear interpolation from their neighbors. A zone still
    short by more than that after dedup is genuinely excluded, same as
    before -- this repairs a known small defect, it doesn't paper over an
    arbitrarily broken row group."""
    cached = _read_cached_shapes(profile_id)
    if cached is not None:
        return cached
    gi = _row_group_index().get(profile_id)
    if gi is None:
        return {}
    pf = pq.ParquetFile(str(_hourly_path()))
    out = shapes_from_frame(pf.read_row_group(gi).to_pandas())
    _write_cached_shapes(profile_id, out)
    return out


def shapes_from_frame(df: pd.DataFrame) -> dict[str, np.ndarray]:
    """{zone: shape} from one profile's hourly rows (climate_zone, hour_of_year, impact), with the small-gap repair _profile_shapes_by_zone() documents. Shared
    with the System-zone catalog build (system_zones.build_catalog), which reads the library row group by row group."""
    out: dict[str, np.ndarray] = {}
    for cz, sub in df.groupby("climate_zone", sort=False):
        sub = sub.drop_duplicates(subset="hour_of_year", keep="first")
        n_missing = HOURS_PER_YEAR - len(sub)
        if n_missing < 0 or n_missing > MAX_REPAIRABLE_GAP_HOURS:
            continue
        series = pd.Series(sub["impact"].to_numpy(dtype=np.float64), index=sub["hour_of_year"].to_numpy())
        series = series.reindex(range(1, HOURS_PER_YEAR + 1))
        if series.isna().any():
            series = series.interpolate(limit_direction="both")
        out[cz] = series.to_numpy(dtype=np.float64)
    return out


# --- on-disk cache of the shapes a job has needed (paths.cache_dir()): reading a profile means decompressing one row group of the 9 GB library (~0.15 s), and every
# process starts cold. One small .npz per profile, keyed by the library file's path, size and modification time, written atomically; the values are the repaired
# shapes exactly as computed below, so a cache hit is bit-identical to a miss.
@lru_cache(maxsize=1)
def _shape_cache_dir() -> Path | None:
    root = paths.cache_dir()
    if root is None:
        return None
    from .acc_store import signature
    return root / f"profiles-{signature(_hourly_path())}"


def _read_cached_shapes(profile_id: int) -> dict[str, np.ndarray] | None:
    d = _shape_cache_dir()
    f = d / f"{profile_id}.npz" if d is not None else None
    if f is None or not f.exists():
        return None
    try:
        z = np.load(f, allow_pickle=False)
        return dict(zip(z["zones"].tolist(), list(z["shapes"])))
    except (OSError, ValueError, KeyError):
        return None


def _write_cached_shapes(profile_id: int, shapes: dict[str, np.ndarray]) -> None:
    d = _shape_cache_dir()
    if d is None or not shapes:
        return
    tmp = d / f"{profile_id}.{os.getpid()}.tmp.npz"
    try:
        d.mkdir(parents=True, exist_ok=True)
        np.savez(tmp, zones=np.array(list(shapes)), shapes=np.stack(list(shapes.values())))
        os.replace(tmp, d / f"{profile_id}.npz")
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def match_profile_id(shape_key: str, iou: str, target_sector: str) -> tuple[int | None, bool]:
    """Returns (profile_id, exact_match). profile_id is None if even the
    loose (end_use_name, iou) fallback found nothing."""
    key = (shape_key, iou, str(target_sector).upper())
    pid = _exact_key_index().get(key)
    if pid is not None:
        return pid, True
    pid = _loose_key_index().get((shape_key, iou))
    return pid, False


def get_shape(profile_id: int, climate_zone: str) -> np.ndarray | None:
    """Real, NORMALIZED (sums to ~1.0) hourly shape for one profile+zone, or
    None if that profile has no 8,760-row data for that zone. Falls back to
    the profile's 'System' (statewide-blended) zone if its own zone isn't
    present."""
    zones = _profile_shapes_by_zone(profile_id)
    if climate_zone in zones:
        return zones[climate_zone]
    return zones.get("System")


def profile_meta(profile_id: int) -> dict:
    cat = _catalog()
    row = cat.loc[cat["id"] == profile_id]
    if row.empty:
        return {"represents_year": DEFAULT_REPRESENTS_YEAR, "is_deer": False}
    row = row.iloc[0]
    return {"represents_year": int(row["represents_year"]), "is_deer": bool(row["is_deer"])}


ACC_CALENDAR_YEAR = 2018
"""The weekday calendar of the ACC's hourly data. Every forecast year of both the 2024 and the 2026 Electric ACC (draft and final) uses 2018's weekdays (hour 1 = a
Monday): checked 2026-10-02 on the data itself -- distribution/transmission avoided cost falls on days 6-7 of every week in every year, and the 2026 ACC's
generation capacity is zero on exactly those days. So a profile is aligned to 2018 once, and the same aligned shape prices every forecast year."""


def alignment_offset_days(represents_year: int, acc_year: int = ACC_CALENDAR_YEAR) -> int:
    """Days to rotate a profile drawn on `represents_year`'s calendar so its weekdays land on the ACC's (2018's): the signed weekday difference, taken as the
    shorter way round the week (-3..+3), so a profile's seasons move at most three days."""
    d = (datetime.date(acc_year, 1, 1).weekday() - datetime.date(represents_year, 1, 1).weekday()) % 7
    return d - 7 if d > 3 else d


def shift_to_acc(shape: np.ndarray, represents_year: int, acc_year: int = ACC_CALENDAR_YEAR) -> np.ndarray:
    """`shape` re-aligned onto the ACC's weekdays: shifted[i] = shape[(i + 24 d) mod 8760], d = alignment_offset_days(). Exactly d days, either sign; the days that
    run off one end of the year wrap to the other. Example: a 2025 profile (1 Jan = Wednesday) gets d = -2, so its 1 Jan lands on ACC day 3, a Wednesday."""
    d = alignment_offset_days(represents_year, acc_year)
    if d == 0:
        return shape
    return shape[(np.arange(HOURS_PER_YEAR) + 24 * d) % HOURS_PER_YEAR]


def shift_for_year(shape: np.ndarray, target_year: int, represents_year: int) -> np.ndarray:
    """The shift every pricing path uses (engine.price_real_hourly, emissions, profile8760). `target_year` is accepted for the callers' signature and ignored:
    the ACC's hourly data follows 2018's weekdays in every forecast year, so the correct shift does not depend on the year being priced (shift_to_acc)."""
    return shift_to_acc(shape, represents_year)


def cedars_shift_for_year(shape: np.ndarray, target_year: int, represents_year: int) -> np.ndarray:
    """REFERENCE ONLY -- not used by any pricing path. CEDARS' own get_alignment_offset()/align_weeks() (CEDARS_generate_avoided_costs V2/V3.pdf), which
    reproduces CEDARS' published quarterly values exactly (CEDARS QAC Validation/full_8760_alignment_test.py) but has two errors: it aligns the profile to each
    forecast year's real Jan-1 weekday although the ACC data follow 2018's weekdays in every year, and it rotates one day too far for negative offsets (k+1)."""
    tw, rw = datetime.date(target_year, 1, 1).weekday(), datetime.date(represents_year, 1, 1).weekday()
    direct, crossover = abs(rw - tw), (6 - rw) + (tw + 1)
    days = (tw - rw) if direct <= abs(crossover) else crossover
    shift_hours = (days if days >= 0 else days - 1) * 24
    return shape[(np.arange(HOURS_PER_YEAR) + shift_hours) % HOURS_PER_YEAR]
