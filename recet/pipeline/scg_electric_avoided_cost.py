"""NOT USED BY THE POC'S PRICING since 2026-10-02 (Jake: the 8,760 path only): SCG is priced on the hourly ACC with
real_avoided_cost_v2's SCG routing. Kept for comparison scripts.


Real, quarterly electric avoided costs for SCG (Southern California Gas)
measures, pulled directly from `dbo.E3AvoidedCostElecCO2Seq2026` (Jake
supplied `E3AvoidedCostElecCO2Seq2026_all_PAs.csv`, 2026-09-16) --
CET's own real, final "quarterly avoided cost impact combination" table
(Equation 2 of "Custom Measure Load Shape Tools Documentation", DNV, CPUC,
7/31/2025), NOT the raw hourly ACC.

WHY THIS EXISTS (found 2026-09-16, chasing a persistent ~7-10% electric
residual on a live 73,024-measure filing's SCG-attributed measures):
SCG is a gas-only utility with no electric avoided costs of its own in
the raw ACC Electric Model export. Appendix C of the documentation above
says "SCE sells electricity in most of SCG territory except climate zones
04, 05 and 13 where PG&E is the utility selling electricity" -- read at
face value, this suggested simply reusing PG&E's (or SCE's) own raw
hourly CZ data for SCG. That was tried (`real_avoided_cost_v2.py`'s
SCG_ZONES_SERVED_BY_PGE) and DID help (ratio improved from ~1.13 to
~1.07-1.10 depending on which other settings were also borrowed from
PG&E) but never closed the gap, because it was the wrong mechanism
entirely: SCG's real quarterly Gen/TD values are NOT identical to PG&E's
CZ5 -- direct comparison (same TS/EU/CZ/Qtr key) shows SCG's ratio to
PG&E has a real SEASONAL PATTERN (Q1 ~0.90-0.93, Q2 ~0.87-0.89 with TD
ratio ~0.67, Q3 ~1.03-1.05 -- SCG HIGHER than PG&E, Q4 ~0.93-0.95), not a
flat scaling factor. CET's real production database has its own distinct,
precomputed SCG series -- likely reflecting SCG's own real customer/
weather mix even though PG&E is nominally "the utility selling
electricity" there. Using THIS real SCG series directly, with SCG's own
real discount rate and SCG's own real DEER impact profile (id 52 for this
measure's end use, confirmed distinct from PG&E's own profile, though
that distinction turned out to matter far less than the rate data itself)
closed the gap from ~7-10% down to 0.15% -- squarely inside the same
~0.2-0.7% residual band every other IOU's own native measures already
sit in.

STRUCTURAL DIFFERENCE from PG&E/SCE/SDG&E's own real hourly extraction
(`real_avoided_cost_v2.py`): this table is QUARTERLY, not hourly, and it's
already impact-profile-specific (keyed by TS/EU/CZ, not just IOU/CZ) --
it IS the final "avoided cost impact combination" (impact profile x
avoided cost, already summed per quarter, per Equation 2), not raw
avoided-cost data needing a separate profile multiplication. So SCG
measures skip the shape-matching/day-of-week-shift/hourly-aggregation
pipeline entirely (see engine.price_scg_electric_quarterly) -- there's no
hourly resolution to reconstruct, and trying to fabricate one (e.g. by
spreading a quarter's total evenly across its hours and still separately
multiplying by a real CEDARS impact profile shape) would double-apply the
profile, since it's already baked into these Gen/TD/CO2 values.

Only ~500,959 of the source file's rows (out of many millions covering
every PA) are PA=="SCG" -- extracted once into
poc/data/elec_ac_scg_real/scg_electric_ac_2026.{csv,parquet} rather than
re-parsing the 288MB original on every run.

SCOPE (2026-09-17): TRC, SCT Base, and SCT High cost tests all wired in
now (`Gen`/`TD`, `Gen_SB`/`TD_SB`, `Gen_SH`/`TD_SH` -- re-extracted from
the original 288MB source, since the first extraction only kept the TRC
columns), matching `real_avoided_cost_v2.py`'s own `cost_test` string
convention ("TRC"/"SCT Base"/"SCT High") for the SCB/SCH cost-test build-
out. `CO2` has no separate SB/SH column in the source data (added to
every cost test's rate unchanged) -- confirmed cost-test-invariant, not
an oversight.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from .. import paths


def _pq() -> Path:
    p = paths.current_scg_table()
    if p is None:
        raise FileNotFoundError("CET SCG electric table not found (optional file cet_tables/scg_electric_ac_2026.parquet in the reference data folder)")
    return p


_GEN_TD_COLUMNS = {"TRC": ("Gen", "TD"), "SCT Base": ("Gen_SB", "TD_SB"), "SCT High": ("Gen_SH", "TD_SH")}


def is_available() -> bool:
    """False when the table file is missing OR the environment variable POC_NO_CET_TABLES is set. That switch exists for runs on an
    avoided-cost version CET has no table for (e.g. a draft ACC): SCG electric is then priced through the ordinary hourly path on the
    Appendix C proxy (PG&E for CZ 4/5/13, else SCE) instead of CET's SCG quarterly table, which was 7-13% off on TRC when tried in the past."""
    import os
    p = paths.current_scg_table()
    return p is not None and p.exists() and not os.environ.get("POC_NO_CET_TABLES")


@lru_cache(maxsize=8)
def _table(cost_test: str = "TRC", gen_only: bool = False) -> pd.DataFrame:
    gen_col, td_col = _GEN_TD_COLUMNS[cost_test]
    df = pd.read_parquet(_pq(), columns=["TS", "EU", "CZ", "Qtr", gen_col, td_col, "CO2"])
    df["year"] = df["Qtr"].str[:4].astype(int)
    df["quarter"] = df["Qtr"].str[-1].astype(int)
    df["rate"] = df[gen_col] if gen_only else df[gen_col] + df[td_col]   # gen_only: the Gen term alone (what CalcCE uses for water energy)
    return df


@lru_cache(maxsize=8)
def _index(cost_test: str = "TRC", gen_only: bool = False) -> dict[tuple[str, str, str], dict[tuple[int, int], float]]:
    """(target_sector, shape_key, climate_zone) -> {(year, quarter): rate},
    built once so has_series()/get_quarterly_series() are O(1) dict
    lookups instead of an O(n) DataFrame scan per call -- with ~500,000
    real SCG rows and thousands of measures needing a lookup each, the
    naive per-call filter made this the dominant cost of a real_hourly run."""
    df = _table(cost_test, gen_only)
    index: dict[tuple[str, str, str], dict[tuple[int, int], float]] = {}
    for key, sub in df.groupby(["TS", "EU", "CZ"], sort=False):
        index[key] = dict(zip(zip(sub["year"], sub["quarter"]), sub["rate"]))
    return index


def has_series(target_sector: str, shape_key: str, climate_zone: str, cost_test: str = "TRC") -> bool:
    """True if this exact (target_sector, shape_key, climate_zone) combo
    has a real SCG row -- check before get_quarterly_series() to decide
    whether a given SCG measure can use this real data source at all, vs.
    needing some other fallback for a combo the real table doesn't cover.
    Coverage (which (TS,EU,CZ) keys exist) doesn't vary by cost_test, only
    the rate does -- but takes cost_test anyway for a consistent call
    signature with get_quarterly_series()."""
    return (target_sector, shape_key, str(climate_zone)) in _index(cost_test)


def get_quarterly_series(
    target_sector: str, shape_key: str, climate_zone: str, cost_test: str = "TRC", gen_only: bool = False
) -> dict[tuple[int, int], float]:
    """{(year, quarter): real $/kWh-equivalent avoided cost} for one
    (target_sector, shape_key, climate_zone) combo and cost test ("TRC",
    "SCT Base", or "SCT High"), across every (year, quarter) the source
    table covers (2024Q1-2054Q4) -- already impact-profile-weighted, so
    this is used directly against the measure's own annual_net_kwh, with
    no separate shape multiplication."""
    key = (target_sector, shape_key, str(climate_zone))
    series = _index(cost_test, gen_only).get(key)
    if series is None:
        raise KeyError(f"No SCG electric AC row for {key}")
    return series


@lru_cache(maxsize=1)
def _sct_populated_index() -> dict[tuple[str, str, str], bool]:
    """(TS, EU, CZ) -> True if this row's Gen_SB/Gen_SH genuinely differ from
    Gen in at least one quarter. False means CET's table holds TRC values in
    the SCT columns (CLAUDE.md finding 9: a CET data error for legacy DEER-
    named end uses), so those columns must not be used as SCT values."""
    df = pd.read_parquet(_pq(), columns=["TS", "EU", "CZ", "Gen", "Gen_SB", "Gen_SH"])
    df["diff"] = ~(np.isclose(df["Gen"], df["Gen_SB"]) & np.isclose(df["Gen"], df["Gen_SH"]))
    return df.groupby(["TS", "EU", "CZ"], sort=False)["diff"].any().to_dict()


def sct_is_populated(target_sector: str, shape_key: str, climate_zone: str) -> bool:
    if not is_available():
        return False   # no table: the societal test also goes through the hourly path
    return _sct_populated_index().get((target_sector, shape_key, str(climate_zone)), False)
