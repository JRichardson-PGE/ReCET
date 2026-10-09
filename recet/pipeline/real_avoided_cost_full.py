"""
FULL real electric avoided cost: energy + generation capacity + transmission
+ distribution + GHG, stacked -- an attempt to close the systematic ~0.5x
shortfall found comparing pipeline.real_avoided_cost's energy-only value
against real CET ElecBen (see poc/README.md).

All components extracted directly from the real 2024 ACC Electric Model
(reference-docs/2024_acc/2024-ACC-Electric-Model-v1b.xlsb, read via pyxlsb
-- an 84MB binary workbook, so extraction was run once and cached as CSVs
under reference-docs/2024_acc/extracted/, not re-parsed on every import)
plus the real GHG $/tonne and Generation Capacity $/kW-yr values already
sitting in the bundled acc-cpuc Python model's own
results/2024ACC_TRC/avoided_costs.csv.

COMPONENTS AND HOW THEY'RE COMBINED:
  1. Energy: real NP-15/SP-15 hourly $/kWh (pipeline.real_avoided_cost),
     grossed up by a real system losses factor (~1.0724, from the Electric
     Model's Losses sheet -- applies uniformly across TOU periods/utilities
     in this vintage, confirmed by direct inspection, not assumed).
  2. Generation Capacity: real annual $/kW-yr (statewide, one value per
     year) x real hourly allocation factors (also statewide, one 8,760-row
     table per year, sums to 1.0) -- the standard "hourly allocation of
     capacity value" technique this review's CLAUDE.md already documented
     from the 2026 ACC review; confirmed present in the 2024 vintage too.
  3. Transmission: real annual $/kW-yr BY UTILITY (PG&E/SCE/SDG&E), no
     separate allocation table found in the workbook -- APPROXIMATION:
     reuses the same Generation Capacity allocation shape (defensible,
     since transmission capacity need is also driven by coincident peak
     demand, but not independently verified against the real methodology).
  4. Distribution: real annual $/kW-yr BY UTILITY AND CLIMATE ZONE for
     PG&E (10 explicit zones); SCE and SDG&E only had a single blended
     "All" value in the block this review located (a dedicated SDG&E
     zone-level block's header was found but its data rows were not
     located within the time spent -- see CLAUDE.md). Same allocation-
     shape approximation as Transmission.
  5. GHG: real $/tonne (acc-cpuc's TRC case) x real grid emissions
     intensity (tonnes CO2/MWh, sparse RESOLVE-IRP-sourced years,
     linearly interpolated / flat-extrapolated after the last point) --
     APPLIED FLAT across all hours (not hourly-varying), since the real
     workbook's emissions data found was an annual system average, not an
     hourly marginal-dispatch emissions rate. A genuinely hourly GHG
     adder would need marginal-unit dispatch modeling, out of scope here.

STILL NOT INCLUDED: Ancillary Services (SERVM AS Prices file exists but
wasn't wired in -- real AS prices are typically a small fraction of energy
value); any real distinction between near-term/long-term distribution
phases beyond what the source table already pre-combined; SCE/SDG&E
climate-zone-level distribution granularity (using utility-level "All").
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

EXTRACTED_DIR = Path(__file__).resolve().parents[3] / "reference-docs" / "2024_acc" / "extracted"
GHG_PRICE_CSV = (
    Path(__file__).resolve().parents[3] / "reference-docs" / "2024_acc"
    / "integrated_calc_model_extract" / "acc-cpuc" / "results" / "2024ACC_TRC" / "avoided_costs.csv"
)
HOURS_PER_YEAR = 8760
LOSS_FACTOR = 1.0724  # confirmed flat across TOU periods/utilities in this vintage's Losses sheet

PA_TO_TRANSMISSION_COL = {"PGE": "PGE", "SCE": "SCE", "SDGE": "SDGE", "SCG": "SCE"}
PA_TO_DISTRIBUTION_COL = {"PGE": None, "SCE": "SCE_All", "SDGE": "SDGE_All", "SCG": "SCE_All"}  # PGE resolved by zone


def is_available() -> bool:
    return EXTRACTED_DIR.exists() and GHG_PRICE_CSV.exists()


@lru_cache(maxsize=1)
def _load_gen_cap_annual() -> pd.Series:
    df = pd.read_csv(EXTRACTED_DIR / "extracted_gen_cap_annual.csv")
    return df.set_index("year")["trc_dollar_per_kw_yr"]


@lru_cache(maxsize=8)
def _load_gen_cap_allocation(year: int) -> np.ndarray:
    df = pd.read_csv(EXTRACTED_DIR / "extracted_gen_cap_allocation.csv", usecols=["hour_idx", str(year)])
    return df[str(year)].to_numpy()


@lru_cache(maxsize=1)
def _load_transmission_annual() -> pd.DataFrame:
    return pd.read_csv(EXTRACTED_DIR / "extracted_transmission.csv").set_index("year")


@lru_cache(maxsize=1)
def _load_distribution_annual() -> pd.DataFrame:
    return pd.read_csv(EXTRACTED_DIR / "extracted_distribution.csv").set_index("year")


@lru_cache(maxsize=1)
def _load_ghg_price_annual() -> pd.Series:
    df = pd.read_csv(GHG_PRICE_CSV)
    return df.set_index("Year")["GHG ($2022/tonne)"]


@lru_cache(maxsize=1)
def _load_ghg_intensity_annual() -> pd.Series:
    return pd.read_csv(EXTRACTED_DIR / "extracted_ghg_intensity.csv").set_index("year")["tonnes_per_mwh"]


def _interp_at_year(series: pd.Series, year: int) -> float:
    """Linear interpolation between known years; flat before the first / after the last."""
    years = series.index.to_numpy()
    if year <= years.min():
        return float(series.iloc[0])
    if year >= years.max():
        return float(series.iloc[-1])
    return float(np.interp(year, years, series.to_numpy()))


def distribution_dollar_per_kw_yr(pa: str, climate_zone: str, year: int, dist_table: pd.DataFrame) -> float:
    col = PA_TO_DISTRIBUTION_COL.get(pa.upper())
    if col is None:  # PGE: resolve by real climate zone, falling back to the PGE-wide average if not listed
        zone_col = f"PGE_CZ{climate_zone}"
        pge_cols = [c for c in dist_table.columns if c.startswith("PGE_")]
        if zone_col in pge_cols:
            col = zone_col
        else:
            return float(dist_table.loc[year, pge_cols].mean())
    return float(dist_table.loc[year, col])


def build_full_avoided_cost(pa: str, climate_zone: str, hub_energy_curve: np.ndarray, year: int) -> np.ndarray:
    """
    Returns an 8,760-length $/kWh array combining energy + generation
    capacity + transmission + distribution + GHG for one (utility, real
    climate zone, year) combination. hub_energy_curve is that PA's real
    NP-15/SP-15 hourly $/kWh series (from real_avoided_cost.py) for `year`.
    """
    energy = hub_energy_curve * LOSS_FACTOR

    gen_cap_annual = _load_gen_cap_annual()
    gen_cap_alloc = _load_gen_cap_allocation(year)
    gen_cap_hourly = _interp_at_year(gen_cap_annual, year) * gen_cap_alloc

    trans_table = _load_transmission_annual()
    trans_col = PA_TO_TRANSMISSION_COL[pa.upper()]
    trans_annual = _interp_at_year(trans_table[trans_col], year)
    trans_hourly = trans_annual * gen_cap_alloc  # approximation: same allocation shape as gen capacity

    dist_table = _load_distribution_annual()
    dist_annual = distribution_dollar_per_kw_yr(pa, climate_zone, year, dist_table)
    dist_hourly = dist_annual * gen_cap_alloc  # same approximation

    ghg_price = _interp_at_year(_load_ghg_price_annual(), year)
    ghg_intensity = _interp_at_year(_load_ghg_intensity_annual(), year)
    ghg_flat_per_kwh = ghg_price * ghg_intensity / 1000.0  # $/tonne * tonnes/MWh / 1000 = $/kWh
    ghg_hourly = np.full(HOURS_PER_YEAR, ghg_flat_per_kwh)  # flat: annual-average intensity, not hourly marginal

    return energy + gen_cap_hourly + trans_hourly + dist_hourly + ghg_hourly
