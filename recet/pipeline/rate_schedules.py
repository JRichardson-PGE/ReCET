"""
Real CET retail rate schedules, for the RIM (Ratepayer Impact Measure) test's
bill-impact columns (`BillReducElec`, `BillIncrElec`, `BillReducGas`,
`BillIncrGas`) -- pulled directly from `dbo.E3RateScheduleElec`,
`dbo.E3RateScheduleElecMapping`, `dbo.E3RateScheduleGas`, and
`dbo.E3RateScheduleGasMapping` (Jake exported all four as CSVs, 2026-09-17,
after this project confirmed via `CET_v25.1_documentation_extracted.txt`
that only the table SCHEMAS -- not the row data -- were already in this
repo; converted to parquet at `poc/data/cet_backup_tables_2026-09-17/`).

Distinct from avoided costs: these are what a customer actually PAYS at
retail (used only for the ratepayer bill-impact side of the RIM test), not
what it costs the utility system to serve them.

SCHEMA, confirmed from the real CREATE TABLE statements (not the PDF's own
garbled "Properties" table renderer, which interleaves columns across two
visual columns and cannot be trusted for column order):
  E3RateScheduleElec: ID, PA, Version, Year, Qy, Schedule, RateE
  E3RateScheduleElecMapping: PA, TargetSector, Schedule, ID, Version
  E3RateScheduleGas: ID, PA, Version, Year, Qy, Schedule, RateG
  E3RateScheduleGasMapping: PA, Version, GasSector, GasRateSchedule

"Qy" is misleadingly named -- despite the name, it's an ANNUAL index, not
quarterly (confirmed empirically: each (PA, Version, Schedule) has exactly
one row per calendar YEAR, Qy incrementing by 1 per year). Version=2026's
Qy=0 lines up with Year=2024 (also confirmed empirically) -- this matches
`CalcCE`'s own view definition (`E3RateScheduleElecvw`,
CET_v25.1_documentation_extracted.txt:9333), which hardcodes
"Yr1 = year(claim_qtr) - 2024" as its PV-sum's lower bound. Because both
Qy and Yr1 share that same "-2024" reference point, `Qy - Yr1` in that
view's discount exponent reduces to simply `Year - claim_year` -- i.e.
plain "years elapsed since the measure's own claim year", with NO
dependency on any run's BaseYear/FirstYear. `engine.price_bill_impact()`
relies on this directly (see its own docstring).

PA values match Measure.csv's raw IOU_AC_Territory string exactly (PGE,
SCE, SDGE, SCG -- no "PG&E"-style relabeling needed, unlike
real_avoided_cost_v2.py's IOU-label conversion for the raw hourly ACC
extraction). PA also legitimately includes CCAs (MCE, BAY, RCEA, SJCE,
PAC, ...) beyond the 4 IOUs -- not exercised by this POC's ground-truth
datasets (all 4 core IOUs only), but real data, not an anomaly.

MAPPING: `CalcCE`'s real E3RateScheduleElecvw/Gasvw join a measure to its
retail rate two ways -- (1) if the measure has its own explicit
RateScheduleElec/RateScheduleGas value set, join straight to that Schedule
name, skipping the mapping table entirely; (2) otherwise, map the measure's
own TargetSector (elec) or E3GasSector (gas) through
E3RateScheduleElecMapping/E3RateScheduleGasMapping to find the Schedule.
Every real measure in this POC's two ground-truth datasets uses path (2)
(RateScheduleElec/RateScheduleGas are blank for all 73,024 SWWH025 rows,
confirmed) -- path (1) is supported here (`explicit_schedule` param) but
unexercised/unvalidated.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from .. import paths, versions
DATA_DIR = paths.package_data("e3_tables")


def _read(name: str) -> pd.DataFrame:
    """One CET rate-schedule export with its Version column (CET's numbering) replaced by the ACC that version holds (versions.CET_VERSION_TO_ACC); rows of
    unmapped versions (2013, 2017) are dropped. The rest of this module keys on that ACC year, still under the column name "Version"."""
    df = pd.read_parquet(DATA_DIR / name)
    df["Version"] = df["Version"].map(versions.acc_of_cet_version)
    return df[df["Version"].notna()].astype({"Version": int}).reset_index(drop=True)

_FILES = [
    "e3_rate_schedule_elec.parquet",
    "e3_rate_schedule_elec_mapping.parquet",
    "e3_rate_schedule_gas.parquet",
    "e3_rate_schedule_gas_mapping.parquet",
]


def is_available() -> bool:
    return all((DATA_DIR / f).exists() for f in _FILES)


@lru_cache(maxsize=1)
def _elec_rates() -> pd.DataFrame:
    return _read("e3_rate_schedule_elec.parquet")


@lru_cache(maxsize=1)
def _elec_mapping() -> pd.DataFrame:
    return _read("e3_rate_schedule_elec_mapping.parquet")


@lru_cache(maxsize=1)
def _gas_rates() -> pd.DataFrame:
    return _read("e3_rate_schedule_gas.parquet")


@lru_cache(maxsize=1)
def _gas_mapping() -> pd.DataFrame:
    return _read("e3_rate_schedule_gas_mapping.parquet")


@lru_cache(maxsize=1)
def _elec_schedule_index() -> dict[tuple[str, int, str], str]:
    m = _elec_mapping()
    return {
        (pa, int(version), str(ts).upper()): schedule
        for pa, ts, schedule, version in zip(m["PA"], m["TargetSector"], m["Schedule"], m["Version"])
    }


@lru_cache(maxsize=1)
def _gas_schedule_index() -> dict[tuple[str, int, str], str]:
    m = _gas_mapping()
    return {
        (pa, int(version), str(sector)): schedule
        for pa, version, sector, schedule in zip(m["PA"], m["Version"], m["GasSector"], m["GasRateSchedule"])
    }


@lru_cache(maxsize=1)
def _elec_rate_index() -> dict[tuple[str, int, str], dict[int, float]]:
    df = _elec_rates().dropna(subset=["Year"])  # 144 rows, all legacy Version=2017 SCE/SCR, genuine gaps in the source export
    index: dict[tuple[str, int, str], dict[int, float]] = {}
    for key, sub in df.groupby(["PA", "Version", "Schedule"], sort=False):
        pa, version, schedule = key
        index[(pa, int(version), schedule)] = dict(zip(sub["Year"].astype(int), sub["RateE"]))
    return index


@lru_cache(maxsize=1)
def _gas_rate_index() -> dict[tuple[str, int, str], dict[int, float]]:
    df = _gas_rates()
    index: dict[tuple[str, int, str], dict[int, float]] = {}
    for key, sub in df.groupby(["PA", "Version", "Schedule"], sort=False):
        pa, version, schedule = key
        index[(pa, int(version), schedule)] = dict(zip(sub["Year"].astype(int), sub["RateG"]))
    return index


def get_elec_rate_years(
    pa: str, target_sector: str, acc_version: int, explicit_schedule: str | None = None
) -> dict[int, float] | None:
    """{year: RateE} for one measure's electric bill-impact rate schedule,
    or None if no mapping/schedule covers it."""
    schedule = explicit_schedule or _elec_schedule_index().get((pa, acc_version, str(target_sector).upper()))
    if schedule is None:
        return None
    return _elec_rate_index().get((pa, acc_version, schedule))


def get_gas_rate_years(
    pa: str, gas_sector: str, acc_version: int, explicit_schedule: str | None = None
) -> dict[int, float] | None:
    """{year: RateG} for one measure's gas bill-impact rate schedule, or
    None if no mapping/schedule covers it."""
    schedule = explicit_schedule or _gas_schedule_index().get((pa, acc_version, str(gas_sector)))
    if schedule is None:
        return None
    return _gas_rate_index().get((pa, acc_version, schedule))
