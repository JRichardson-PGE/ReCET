"""
RUN PARAMETERS (user-facing name, 2026-10-03): discount rates, base year and gas CO2 per PA. The built-in values are CET's E3Settings table; a user-supplied
run-parameters file (overrides.load_run_parameters, set_user_parameters below) replaces the row of every PA it lists.

Real, per-IOU CET discounting settings -- `dbo.E3Settings` in the actual
CET database (confirmed via `reference-docs/CET_v25.1_documentation_extracted.txt`:
`Settingsvw` selects `DiscountRateAnnual`/`DiscountRateQtr`/their `_SCT`
variants and `BaseYear` from `E3Settings`, keyed by PA and avoided-cost
`Version`). Values themselves (`data/avoided_cost_settings.csv`) came
directly from Jake, who has real CEDARS/CET practitioner access to this
table.

WHY THIS MATTERS (found 2026-09-15, confirmed against the real CalcCE SQL):
this POC's engine.py used to discount every measure at one flat, made-up
3%/year rate -- not calibrated to anything real. The real CalcCE procedure
instead:
  - looks up DiscountRateAnnual/DiscountRateQtr PER PA (each IOU has its
    own real WACC-derived rate -- PGE 7.66%/yr, SCE 7.65%/yr, SCG 7.38%/yr,
    SDGE 7.36%/yr, confirmed identical across every ACC Version in the real
    settings table, i.e. this doesn't drift much release to release);
  - discounts measure-level (quarterly-granularity) benefit/cost streams
    QUARTERLY, via `.../ POWER(Rqf, Qac)` where `Rqf = 1 + DiscountRateQtr`
    and `Qac` is a quarter-index counted from a BaseYear reference point --
    confirmed: `DiscountRateQtr` is simply `DiscountRateAnnual / 4` (a flat
    nominal quarterly rate, not a compounded fourth root), so this module
    exposes it directly rather than deriving it;
  - discounts program-level (annual-granularity, e.g. ProgramCost.csv's
    admin costs) streams ANNUALLY instead, via `Raf^(Year-FirstYear)` --
    this POC doesn't price ProgramCost.csv line items at all (out of scope,
    same as always), so only the quarterly rate matters here.

BASE YEAR, the other half of this fix: `E3Settings.BaseYear` is a static
per-Version value (e.g. 2024 for Version "2026" -- consistent with this
review's already-documented "Avoided Cost Version 2026 is really the 2024
ACC file" finding), but Jake confirmed the REAL discounting base year for
a given CET run is that run's own "First Year" receipt field, which
overrides the static settings-table value for that specific job. Both real
CET runs this POC compares against report First Year == that dataset's own
single claim year (2025 for the small sample, 2031 for the large filing) --
see pipeline/engine.py's price_real_hourly for how this is used.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

# The full dbo.E3Settings export (every ACC Version 2013-2027, one row per PA), supplied by Jake 2026-09-20. It replaces the earlier 20-row
# avoided_cost_settings.csv, which had no Version 2027 (the 2026 ACC's version, AvoidedCostVersion 5/25/2026).
from .. import paths
SETTINGS_CSV = paths.package_data("E3Settings.csv")

PA_ALIASES = {"PGE": "PGE", "SCE": "SCE", "SCG": "SCG", "SDGE": "SDGE"}


def is_available() -> bool:
    return SETTINGS_CSV.exists()


@lru_cache(maxsize=1)
def _table() -> pd.DataFrame:
    df = pd.read_csv(SETTINGS_CSV)
    df["PA"] = df["PA"].str.strip()
    df = df.rename(columns={"Version": "ACC Version"})          # the callers below use the older column name
    return df.drop_duplicates(subset=["PA", "ACC Version"], keep="first").reset_index(drop=True)   # SJCE 2022 appears twice in the export


def known_versions() -> list[int]:
    """ACC versions that have settings rows (2013-2027)."""
    return sorted(int(v) for v in _table()["ACC Version"].unique())


def get_settings(pa: str, acc_version: int = 2026) -> dict:
    """Returns the real E3Settings row for (pa, acc_version) as a dict:
    discount_rate_annual, discount_rate_qtr (and the _sct variants, unused
    here but kept for completeness -- Jake confirmed SCT uses a DIFFERENT,
    lower discount rate than the primary TRC-style calculations this POC
    compares against, so never substitute one for the other)."""
    pa_key = PA_ALIASES.get(pa.upper().replace("&", ""), pa.upper())
    return dict(_settings_row(pa_key, int(acc_version))[0])


# --- user-supplied RUN PARAMETERS (recet.overrides.load_run_parameters): a PA in this table replaces the built-in E3Settings row for every version ---
_USER = None


def set_user_parameters(table) -> None:
    """`table`: DataFrame indexed by PA with DiscountRateAnnual, DiscountRateQtr, DiscountRateAnnual_SCT, DiscountRateQtr_SCT, BaseYear (may be NaN), CO2Gas; or
    None to clear. Takes effect for lookups after the next cache clear (runtime.apply clears every cache)."""
    global _USER
    _USER = table
    _settings_row.cache_clear()


def user_parameters():
    return _USER


@lru_cache(maxsize=512)
def _settings_row(pa_key: str, acc_version: int) -> tuple[dict, float]:
    """(the settings dict, CO2Gas) of one (PA, Version) row, looked up in the table once (callers ask per measure, and a pandas filter per call dominated the
    emissions time). A user-supplied run-parameters row for the PA wins; its blank BaseYear takes the built-in value for this version."""
    table = _table()
    row = table[(table["PA"] == pa_key) & (table["ACC Version"] == acc_version)]
    if _USER is not None and pa_key in _USER.index:
        u = _USER.loc[pa_key]
        base = u["BaseYear"]
        if pd.isna(base):
            same_version = table[table["ACC Version"] == acc_version]
            if not row.empty:
                base = row.iloc[0]["BaseYear"]
            elif not same_version.empty:
                base = same_version["BaseYear"].mode().iloc[0]
            else:
                raise KeyError(f"run parameters for PA={pa_key!r} give no BaseYear and E3 version {acc_version} has no built-in rows to take it from")
        return ({
            "discount_rate_annual": float(u["DiscountRateAnnual"]),
            "discount_rate_qtr": float(u["DiscountRateQtr"]),
            "discount_rate_annual_sct": float(u["DiscountRateAnnual_SCT"]),
            "discount_rate_qtr_sct": float(u["DiscountRateQtr_SCT"]),
            "settings_base_year": int(base),
        }, float(u["CO2Gas"]))
    if row.empty:
        raise KeyError(f"No run parameters (E3Settings) for PA={pa_key!r}, version={acc_version!r}: supply them in a run-parameters file")
    row = row.iloc[0]
    return ({
        "discount_rate_annual": float(row["DiscountRateAnnual"]),
        "discount_rate_qtr": float(row["DiscountRateQtr"]),
        "discount_rate_annual_sct": float(row["DiscountRateAnnual_SCT"]),
        "discount_rate_qtr_sct": float(row["DiscountRateQtr_SCT"]),
        "settings_base_year": int(row["BaseYear"]),
    }, float(row["CO2Gas"]))


def get_discount_rate_qtr(pa: str, acc_version: int = 2026) -> float:
    """SCG has no electric ACC (see real_avoided_cost_v2.PA_TO_IOU_LABEL) but
    DOES have its own real discount rate row here -- CET still discounts an
    SCG-territory electric measure's (SCE-fallback-priced) benefit stream at
    SCG's own rate, since discounting is a PA property, not an ACC-source
    property. Callers should pass the measure's own real PA, not whatever
    ACC fallback IOU real_avoided_cost_v2 used for the dollar values."""
    return get_settings(pa, acc_version)["discount_rate_qtr"]


def get_discount_rate_annual(pa: str, acc_version: int = 2026) -> float:
    """Annual (not quarterly) discount rate -- for the few real CalcCE
    streams that discount annually instead of quarterly, e.g. the RIM
    test's bill-impact PV sum (engine.price_bill_impact; real retail rate
    schedules are themselves annual-granularity, see
    pipeline/rate_schedules.py)."""
    return get_settings(pa, acc_version)["discount_rate_annual"]


def get_discount_rate_qtr_sct(pa: str, acc_version: int = 2026) -> float:
    """The SEPARATE, real SOCIETAL quarterly discount rate (`Rqf_SCT` in
    the real SQL) -- used for the societal cost tests (SCB/SCH), confirmed
    genuinely lower than the primary TRC/PAC/RIM rate `get_discount_rate_
    qtr` returns (CET_v25.1_documentation_extracted.txt:14460-14465 etc.:
    `ace_1.Gen_SB`/`Gen_SH`, the SCB/SCH avoided-cost PV terms, discount
    with `Rqf_SCT` specifically, not the regular `Rqf` every other cost
    test uses)."""
    return get_settings(pa, acc_version)["discount_rate_qtr_sct"]


def get_co2_gas(pa: str, acc_version: int = 2026) -> float:
    """`E3Settings.CO2Gas` (Settingsvw.CO2Gas): metric tons CO2 per therm of gas
    saved, used by CalcEmissions for NetGasCO2/GrossGasCO2. 0.00531 for
    Version 2026 (older versions differ, e.g. 0.00585 for 2020)."""
    pa_key = PA_ALIASES.get(pa.upper().replace("&", ""), pa.upper())
    return _settings_row(pa_key, int(acc_version))[1]
