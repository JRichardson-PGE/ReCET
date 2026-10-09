"""
Real CEDARS gas load-shape profiles (`data/gas_savings_profiles.csv`, copied
verbatim from the "Gas Savings Profiles.xlsx" Jake provided) -- the gas
counterpart to `real_impact_profiles.py`'s electric shapes, but monthly
(12 values, not 8,760) and, per Jake, needing no day-of-week/calendar
shift at all: "Because it is monthly there is no need for date shifting."

Each named profile's 12 monthly `Impact` values are NORMALIZED (sum to
1.0), matching every other shape contract in this POC (`synthetic_curves.
load_shape()`, `real_impact_profiles.get_shape()`) -- multiply by a
measure's own annual_net_therms at pricing time, don't bake magnitude in
here.

Matches a measure to its real profile via CET's own `E3GasSavProfile`
field, which uses the EXACT same names as this file's `Profile` column
("Annual", "Winter Only", "Summer Only" -- confirmed against both bundled
real datasets).

FIXED 2026-09-15: an earlier version of this file had a real data-entry
error -- two different profile blocks both named "Summer Only-FuelSub-
ToElec" (one genuinely summer-shaped, one flat/uniform). Jake corrected it
to a proper 6-distinct-name set including "Annual-FuelSub-ToElec" (the
flat one's real intended name). is_ambiguous_name() is kept as a standing
safety check against this class of error recurring silently, not because
a known collision exists today.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from .. import paths

PROFILES_CSV = paths.package_data("gas_savings_profiles.csv")

MONTHS_PER_YEAR = 12

# User-supplied monthly profiles (recet/overrides.py): {profile name: normalized length-12 array}. A name that matches a library profile
# REPLACES it; a new name is added.
_USER: dict[str, np.ndarray] = {}


def set_user_profiles(profiles: dict[str, np.ndarray] | None) -> None:
    global _USER
    _USER = dict(profiles or {})


def user_profile_names() -> frozenset[str]:
    return frozenset(_USER)


def source(profile_name: str) -> str | None:
    """"user profile", "library", or None if the name is in neither."""
    if profile_name in _USER:
        return "user profile"
    return "library" if _library_shape(profile_name) is not None else None


def is_available() -> bool:
    return PROFILES_CSV.exists() or bool(_USER)


@lru_cache(maxsize=1)
def _table() -> pd.DataFrame:
    return pd.read_csv(PROFILES_CSV)


@lru_cache(maxsize=1)
def _ambiguous_names() -> frozenset[str]:
    df = _table()
    counts = df.groupby("Profile")["Month"].count()  # 12 rows per well-formed profile
    return frozenset(counts[counts > MONTHS_PER_YEAR].index)


def is_ambiguous_name(profile_name: str) -> bool:
    return profile_name in _ambiguous_names()


def get_shape(profile_name: str) -> np.ndarray | None:
    """NORMALIZED (sums to ~1.0) length-12 array, Jan..Dec, or None if the name is in neither the user-supplied profiles (which win) nor the
    library. For an ambiguous library name (see module docstring) this is whichever definition appears LAST in the source
    file -- check is_ambiguous_name() first if that matters to the caller."""
    if profile_name in _USER:
        return _USER[profile_name]
    return _library_shape(profile_name)


@lru_cache(maxsize=32)
def _library_shape(profile_name: str) -> np.ndarray | None:
    df = _table()
    sub = df[df["Profile"] == profile_name]
    if sub.empty:
        return None
    sub = sub.drop_duplicates(subset="Month", keep="last").sort_values("Month")
    if len(sub) != MONTHS_PER_YEAR:
        return None
    return sub["Impact"].to_numpy(dtype=np.float64)
