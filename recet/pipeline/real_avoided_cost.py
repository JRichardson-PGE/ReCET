"""
Loads REAL hourly electric avoided-cost data from the actual 2024 Avoided
Cost Calculator (the version CET has implemented today -- the 2026 draft
this review looked at earlier is NOT yet live in CET, so 2024 is the
correct vintage to validate this POC against).

Source file: reference-docs/2024_acc/2024-ACC-SERVM-Energy-Prices-v1b.xlsx,
sheet "Nominal Prices for Restore" -- E3's own final, $/kWh-scaled hourly
price series (already floor/capped between $0 and $1/kWh by E3, no unit
conversion needed), for the two CAISO trading hubs the workbook provides:
NP-15 (Northern California) and SP-15 (Southern California), each for
every year 2023-2054, 8,760 hours each.

WHAT THIS DOES NOT INCLUDE (documented scope limit, not an oversight): the
real, full electric avoided cost CET actually uses layers T&D loss factors,
GHG adders, and generation-capacity value on top of this base energy price
(computed in the Electric Model / Integrated Calculation workbooks), and
differentiates by climate zone + IOU territory more finely than just
NP-15/SP-15. This loader gives the real *base energy* price signal --
enough to test whether the batched-hourly architecture reproduces sensible
results against real market data, not a full reimplementation of E3's
complete ACC methodology.

PA -> hub mapping used here: PG&E -> NP-15, SCE/SDG&E -> SP-15. This
matches standard CAISO zonal convention and is sufficient for the POC's
real 5-measure sample, which is 100% PG&E (see ProgramCost_real_sample.csv).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import openpyxl

ACC_DIR = Path(__file__).resolve().parents[3] / "reference-docs" / "2024_acc"
SERVM_ENERGY_FILE = ACC_DIR / "2024-ACC-SERVM-Energy-Prices-v1b.xlsx"
SHEET_NAME = "Nominal Prices for Restore"

# Confirmed by direct inspection of the workbook: header row 4 (0-indexed)
# has 'NP-15 ...' at column 4 and 'SP-15 ...' at column 36, each followed by
# 32 year columns (2023-2054); 8,760 hourly data rows start at sheet row 6.
NP15_YEAR_COL_START = 4
SP15_YEAR_COL_START = 36
FIRST_YEAR = 2023
DATA_START_ROW = 6
HOURS_PER_YEAR = 8760

PA_TO_HUB = {
    "PGE": "NP15",
    "SCE": "SP15",
    "SDGE": "SP15",
    "SCG": "SP15",  # gas-only utility, but its electric-serving overlap is Southern California
}


def is_available() -> bool:
    return SERVM_ENERGY_FILE.exists()


def load_2024_acc_hourly(year: int) -> dict[str, np.ndarray]:
    """Returns {'NP15': array(8760,), 'SP15': array(8760,)} of real $/kWh for `year`."""
    if not is_available():
        raise FileNotFoundError(
            f"{SERVM_ENERGY_FILE} not found -- expected the real 2024 ACC SERVM Energy "
            "Prices file to be copied into reference-docs/2024_acc/."
        )
    if not (FIRST_YEAR <= year <= FIRST_YEAR + 31):
        raise ValueError(f"year {year} outside the workbook's 2023-2054 range")

    np15_col = NP15_YEAR_COL_START + (year - FIRST_YEAR)
    sp15_col = SP15_YEAR_COL_START + (year - FIRST_YEAR)

    wb = openpyxl.load_workbook(SERVM_ENERGY_FILE, read_only=True, data_only=True)
    ws = wb[SHEET_NAME]

    np15 = np.empty(HOURS_PER_YEAR)
    sp15 = np.empty(HOURS_PER_YEAR)
    for i, row in enumerate(ws.iter_rows(
        min_row=DATA_START_ROW, max_row=DATA_START_ROW + HOURS_PER_YEAR - 1, values_only=True
    )):
        np15[i] = row[np15_col]
        sp15[i] = row[sp15_col]
    wb.close()

    return {"NP15": np15, "SP15": sp15}


def subset_key_to_hub(pa: str) -> str:
    hub = PA_TO_HUB.get(pa.upper().replace("&", ""))
    if hub is None:
        raise KeyError(f"No NP-15/SP-15 mapping for PA {pa!r} -- add it to PA_TO_HUB")
    return hub
