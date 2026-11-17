"""
Real, monthly gas avoided costs from the 2024 Gas ACC model, extracted by
`2024 Gas ACC Extraction/code/extract_gas_v4_components.py` (see that
directory's README and that script's own module docstring for the full
methodology and QC) -- the gas counterpart to `real_avoided_cost_v2.py`'s
electric hourly costs, at the granularity gas actually needs (monthly,
per Jake: "there is no need for date shifting").

COMPONENT-LEVEL STORAGE (v4, 2026-09-15): Jake's direction -- "for the
actual POC tool I want the avoided costs to be stored at the component
level to allow a future tool to select components to be included... We
won't need to split out T&D and commodity at that point either, since the
future tool running component level will be able to drive this detail."
So this module no longer reads a pre-toggled Commodity+T&D bundle
(v2/v3's `behind_meter` row-doubling is gone too) -- it reads all SEVEN
real components separately (`get_components_for_measure()`) and only sums
them into one $/therm number (`get_monthly_series_for_measure()`) as a
default convenience for the current pipeline's own pricing math, per
Jake's "we can aggregate the components into totals before multiplying
out gasben if it is helpful for efficiency." A future component-selection
tool should call `get_components_for_measure()` directly and sum whatever
subset it wants instead.

THE SEVEN COMPONENTS (confirmed by reading User Dashboard row 59's actual
formula, not assumed): `market` (Market/Commodity, Utility-specific
gross-up applied) and `td` (T&D) are the only two that vary MONTH to
month within a year -- both stored as 12-value arrays. `nox`, `co2`,
`upstream_methane`, `behind_meter_methane`, and `air_quality_adder` are
each a single FLAT annual $/therm value (row-pinned in the workbook's own
formula, i.e. identical for every month of a given year) -- stored as one
scalar per year, not 12 identical columns. None of these five depend on
Utility at all (only Class, Cost Test, SCC, and Year); only `market` and
`td` do.

BEHIND-THE-METER METHANE, still real but conditional: excluded from the
default total EXCEPT for Residential fuel-substitution-to-electric
("...-FuelSub-ToElec") gas savings profiles, which DO use it (Appendix
B's Figure B-4 is specific to that combo -- confirmed by Jake). Since it's
now its own stored component (not a second extracted row), this is just a
selection rule in `default_components_for_measure()`, not an extraction
concern.

VALIDATION: summing (market+td+nox+co2+upstream_methane+air_quality_adder)
off the v4 component table reproduces the v3 bundled table's non-
behind_meter output to float64 precision (max abs diff ~6e-14) -- the
component split changed how the data is STORED, not the real numbers
themselves, which were already validated against CET's own production
export (see extract_gas_v3_multiyear.py and qc_v2_vs_ground_truth.py):
T&D matches to float64 precision; Commodity (market+environment+upstream
methane+air quality) is within ~0.7-2.9% depending on year, LOWER than
CET's real ground truth (see that script's docstring for the still-being-
chased root cause -- a small, compounding difference in the Commodity
price chain's escalation rate, isolated to Commodity, not T&D).

MATCHING a measure to a real (Utility, Class) combination: CET's
Measure.csv gives real `IOU_AC_Territory` (-> Utility) and `AC_Gas_Target_Sector`
(-> Class). End Use / Emission Control are NOT free dimensions here --
confirmed by Appendix B step 5 (Residential -> "Residential Furnace";
Commercial/Total Core -> "Small Boiler (<100MMBtu/Hr)"; Emission Control
always "Uncontrolled") and already baked into the extraction, so this
module only needs to resolve Utility and Class.

UTILITY MAPPING: the Gas ACC only covers PG&E / Socal Gas / SDG&E (SCE has
no gas utility of its own). SCE-territory measures with real gas savings
fall back to Socal Gas (SCE's electric territory overlaps SoCalGas's real
gas-serving area in Southern California) -- the mirror image of
`real_avoided_cost_v2.py`'s existing SCG-falls-back-to-SCE convention on
the electric side.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from .. import paths

# Which gas ACC extraction is used is chosen at run time (paths.configure_runtime / recet.runtime.configure): the 2024 Gas ACC by default, the
# 2026 one (packaged as recet/data/acc/2026/gas_components.parquet, 2026-2055; the final v1b Gas Model is unchanged from the draft, so the file is too), or a user-supplied file with
# the same layout. The covered years are read from the table, so a different extraction can start and end in different years.


def _pq() -> Path:
    p = paths.current_acc_gas()
    if p is None:
        raise FileNotFoundError("gas ACC parquet not found (packaged: recet/data/acc/<version>/gas_components.parquet)")
    return p


MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

# Monthly (12-array) vs annual (scalar, broadcast to 12) components -- see module docstring.
MONTHLY_COMPONENTS = ("market", "td")
ANNUAL_COMPONENTS = ("nox", "co2", "upstream_methane", "behind_meter_methane", "air_quality_adder")
ALL_COMPONENTS = MONTHLY_COMPONENTS + ANNUAL_COMPONENTS

PA_TO_UTILITY = {"PGE": "PG&E", "SCG": "Socal Gas", "SDGE": "SDG&E", "SCE": "Socal Gas"}
GAS_SECTOR_TO_CLASS = {"RESIDENTIAL": "Residential", "COMMERCIAL": "Commercial", "CORE": "Total Core"}


def is_available() -> bool:
    return paths.current_acc_gas() is not None


@lru_cache(maxsize=1)
def _table() -> pd.DataFrame:
    return pd.read_parquet(_pq())


def available_cost_tests() -> tuple[str, ...]:
    """The cost tests the loaded gas extraction has rows for: ("TRC",) for the 2021 and 2022 Gas ACCs, ("TRC", "SCT") for 2024 and 2026."""
    return tuple(sorted(_table()["cost_test"].astype(str).unique(), key=lambda t: (t != "TRC", t)))


def available_components() -> tuple[str, ...]:
    """The components the loaded extraction has columns for. The 2021 and 2022 Gas ACCs have no air quality adder; a missing component is priced as zero."""
    cols = set(_table().columns)
    return tuple(c for c in ALL_COMPONENTS if c in cols or f"{c}_jan" in cols)


def year_range() -> tuple[int, int]:
    """First and last year the loaded extraction covers (2024-2054 for the 2024 Gas ACC, 2026-2055 for the 2026 one); years outside clamp to it."""
    years = _table()["year"]
    return int(years.min()), int(years.max())


def utility_for_pa(pa: str) -> str:
    key = pa.upper().replace("&", "")
    if key not in PA_TO_UTILITY:
        raise KeyError(f"No gas utility mapping for PA {pa!r} -- add it to PA_TO_UTILITY")
    return PA_TO_UTILITY[key]


def class_for_gas_sector(gas_sector: str) -> str:
    key = str(gas_sector).upper()
    if key not in GAS_SECTOR_TO_CLASS:
        raise KeyError(f"No Gas ACC Class mapping for AC_Gas_Target_Sector {gas_sector!r}")
    return GAS_SECTOR_TO_CLASS[key]


def is_fuel_sub_profile(gas_profile_key: str | None) -> bool:
    """Real profile names ending "...-FuelSub-ToElec" (Annual/SummerOnly/
    WinterOnly variants) -- the only ones where behind-the-meter methane
    leakage is real per Appendix B's Figure B-4."""
    return isinstance(gas_profile_key, str) and "FuelSub" in gas_profile_key


def default_components_for_measure(gas_sector: str, gas_profile_key: str | None) -> tuple[str, ...]:
    """The component subset the current pipeline prices with, absent a
    future component-selection tool: everything except behind-meter
    methane, which is only real for Residential FuelSub-ToElec profiles
    (Appendix B's Figure B-4) -- Jake's confirmed rule, not a free choice."""
    base = ("market", "td", "nox", "co2", "upstream_methane", "air_quality_adder")
    if class_for_gas_sector(gas_sector) == "Residential" and is_fuel_sub_profile(gas_profile_key):
        return base + ("behind_meter_methane",)
    return base


@lru_cache(maxsize=128)
def get_components_for_measure(pa: str, gas_sector: str, year: int, cost_test: str = "TRC", scc: str = "") -> dict[str, np.ndarray]:
    """All seven real components for one (utility, class, year) combo, each
    as a 12-value (Jan..Dec) $/therm array -- `market`/`td` genuinely vary
    by month; the other five are a single annual value broadcast across
    all 12 months (see module docstring for why that's not a storage
    artifact -- the workbook's own formula pins them flat). A future
    component-selection tool should call this directly."""
    utility = utility_for_pa(pa)
    gas_class = class_for_gas_sector(gas_sector)
    first_year, last_year = year_range()
    clamped_year = min(max(int(year), first_year), last_year)
    components = _component_index().get((utility, gas_class, cost_test, scc if cost_test == "SCT" else "", clamped_year))
    if components is None:
        raise KeyError(f"No gas ACC row for {(utility, gas_class, cost_test, scc, clamped_year)}")
    return components


@lru_cache(maxsize=1)
def _component_index() -> dict[tuple, dict[str, np.ndarray]]:
    """(utility, class, cost_test, scc, year) -> the seven components as 12-month arrays, built once from the table (the first row wins, as the old per-call filter
    did; `scc` only distinguishes the SCT rows). The arrays are shared between callers, so they are read-only."""
    index: dict[tuple, dict[str, np.ndarray]] = {}
    for row in _table().to_dict("records"):
        key = (row["utility"], row["class"], row["cost_test"], row["scc"] if row["cost_test"] == "SCT" else "", int(row["year"]))
        if key in index:
            continue
        comps: dict[str, np.ndarray] = {}
        for name in MONTHLY_COMPONENTS:
            comps[name] = np.array([row[f"{name}_{m}"] for m in MONTHS], dtype=np.float64)
        for name in ANNUAL_COMPONENTS:
            # A component the extraction does not have (the 2021/2022 Gas ACCs have no air quality adder) is zero.
            comps[name] = np.full(12, float(row[name]) if name in row else 0.0, dtype=np.float64)
        for arr in comps.values():
            arr.setflags(write=False)
        index[key] = comps
    return index


def get_monthly_series(pa: str, gas_sector: str, year: int, cost_test: str = "TRC", scc: str = "", components: tuple[str, ...] | None = None) -> np.ndarray:
    """Real nominal $/therm for ONE year, Jan..Dec, summed across
    `components` (default: `default_components_for_measure`'s selection,
    keyed by gas_sector alone -- pass an explicit `components` tuple to
    override, e.g. for a future component-picker tool)."""
    if components is None:
        components = ("market", "td", "nox", "co2", "upstream_methane", "air_quality_adder")
    parts = get_components_for_measure(pa, gas_sector, year, cost_test, scc)
    return sum(parts[name] for name in components)


def get_monthly_series_for_measure(pa: str, gas_sector: str, gas_profile_key: str | None, year: int, cost_test: str = "TRC", scc: str = "",
                                   component: str | None = None) -> np.ndarray:
    """Same as `get_monthly_series()`, but picks the component subset
    automatically via `default_components_for_measure()` -- True behind-
    meter-methane inclusion only for Residential + a "...-FuelSub-ToElec"
    gas savings profile, per Appendix B's Figure B-4.

    component=<name>: just that one component, or zeros if the measure's default selection does not include it (behind-the-meter methane
    outside residential fuel substitution), so the seven components always add up to the total."""
    components = default_components_for_measure(gas_sector, gas_profile_key)
    if component is not None:
        if component not in components:
            return np.zeros(12)
        components = (component,)
    return get_monthly_series(pa, gas_sector, year, cost_test, scc, components)


def get_years_for_measure(pa: str, gas_sector: str, gas_profile_key: str | None, cost_test: str = "TRC", scc: str = "",
                          component: str | None = None) -> dict[int, np.ndarray]:
    """Every year the extraction covers (2024-2054 or 2026-2055) at once, as
    {year: 12-month array} -- for engine.price_gas_benefit's per-year lookup
    across a measure's EUL, mirroring real_avoided_cost_v2.get_acc_series()'s
    dict-of-years shape on the electric side."""
    first_year, last_year = year_range()
    return {y: get_monthly_series_for_measure(pa, gas_sector, gas_profile_key, y, cost_test, scc, component) for y in range(first_year, last_year + 1)}
