"""Measure field names: ReCET's names for the avoided-cost keys, and the legacy names CET still uses.

The CET's Measure.csv names these fields after the E3 calculator it grew out of (E3ClimateZone, E3MeaElecEndUseShape, ...). ReCET names them for what they
are. Every input may use either name: `to_recet` renames the legacy columns when a file is read, so the rest of the program only sees ReCET's names. Files
written for upload to the CET use CET's names again (`to_cet`); the two ReCET-only profile columns have no CET name and keep ReCET's.
"""
from __future__ import annotations

import pandas as pd

# legacy name -> ReCET's name
LEGACY_TO_RECET = {
    "E3ClimateZone": "AC_Climate_Zone",
    "E3MeaElecEndUseShape": "AC_Elec_Impact_Profile",
    "E3MeaElecEndUseShape2nd": "AC_Elec_Impact_Profile_2nd_Baseline",
    "E3MeaElecEndUseShapeWater": "AC_Elec_Impact_Profile_Water",
    "E3TargetSector": "AC_Elec_Target_Sector",
    "E3GasSavProfile": "AC_Gas_Impact_Profile",
    "E3GasSector": "AC_Gas_Target_Sector",
}
# ReCET's name -> CET's name, for the five that are CET fields
RECET_TO_CET = {"AC_Climate_Zone": "E3ClimateZone", "AC_Elec_Impact_Profile": "E3MeaElecEndUseShape", "AC_Elec_Target_Sector": "E3TargetSector",
                "AC_Gas_Impact_Profile": "E3GasSavProfile", "AC_Gas_Target_Sector": "E3GasSector"}


def recet_name(column: str) -> str:
    """ReCET's name for a column (its own name if it is not a renamed field). Case-insensitive on the legacy name, as CET is."""
    c = str(column).strip()
    for old, new in LEGACY_TO_RECET.items():
        if c.lower() == old.lower():
            return new
    return c


def to_recet(df: pd.DataFrame) -> pd.DataFrame:
    """The frame with legacy column names replaced by ReCET's. A file holding both names of one field is an error (which one to use would be a guess)."""
    renames = {c: recet_name(c) for c in df.columns if recet_name(c) != str(c).strip()}
    if not renames:
        return df
    clash = sorted(f"{old} / {new}" for old, new in renames.items() if new in df.columns)
    if clash:
        raise ValueError(f"the file has both the legacy and the ReCET name of the same field: {', '.join(clash)}; keep one")
    return df.rename(columns=renames)


def to_cet(df: pd.DataFrame) -> pd.DataFrame:
    """The frame with CET's names for the CET fields (for a file to upload to the CET)."""
    return df.rename(columns={c: RECET_TO_CET[c] for c in df.columns if c in RECET_TO_CET})
