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


# ------------------------------------------------------------------------------------------------------------------------------------- output columns
# Measure-level output columns renamed in 0.1.50 (from the field dictionary): the name used before -> the name ReCET writes now. Old names are still
# accepted wherever a user names output columns (output templates, saved Results views), and a CET outputs file's own names are read through this map.
OUTPUT_RENAMES = {
    "POC_ACC": "Used_ACCVersion",
    "poc_claim_year": "Used_Year",
    "poc_claim_quarter": "Used_Quarter",
    "poc_shape_scale": "AC_kWh_1st_Baseline",
    "poc_shape_scale_2nd": "AC_kWh_2nd_Baseline",
    "poc_annual_net_therms_1st": "AC_Therms_1st_Baseline",
    "poc_annual_net_therms_2nd": "AC_Therms_2nd_Baseline",
    "poc_water_scale_1st": "AC_kWh_Water_1st_Baseline",
    "poc_water_scale_2nd": "AC_kWh_Water_2nd_Baseline",
    "poc_is_fuel_sub": "Fuel_Sub_Flag",
    "BillReducElec": "LifecycleBillReducElec",
    "BillIncrElec": "LifecycleBillIncrElec",
    "BillReducGas": "LifecycleBillReducGas",
    "BillIncrGas": "LifecycleBillIncrGas",
    "BillReducElecGross": "LifecycleBillReducElecGross",
    "BillIncrElecGross": "LifecycleBillIncrElecGross",
    "BillReducGasGross": "LifecycleBillReducGasGross",
    "BillIncrGasGross": "LifecycleBillIncrGasGross",
    "NetElecCO2": "FirstYearNetElecCO2",
    "GrossElecCO2": "FirstYearGrossElecCO2",
    "NetElecCO2Lifecycle": "LifecycleNetElecCO2",
    "GrossElecCO2Lifecycle": "LifecycleGrossElecCO2",
    "NetElecNOx": "FirstYearNetElecNOx",
    "GrossElecNOx": "FirstYearGrossElecNOx",
    "NetElecNOxLifecycle": "LifecycleNetElecNOx",
    "GrossElecNOxLifecycle": "LifecycleGrossElecNOx",
    "NetPM10": "FirstYearNetElecPM10",
    "GrossPM10": "FirstYearGrossElecPM10",
    "NetPM10Lifecycle": "LifecycleNetElecPM10",
    "GrossPM10Lifecycle": "LifecycleGrossElecPM10",
    "NetGasCO2": "FirstYearNetGasCO2",
    "GrossGasCO2": "FirstYearGrossGasCO2",
    "NetGasCO2Lifecycle": "LifecycleNetGasCO2",
    "GrossGasCO2Lifecycle": "LifecycleGrossGasCO2",
    "NetGasNOx": "FirstYearNetGasNOx",
    "GrossGasNOx": "FirstYearGrossGasNOx",
    "NetGasNOxLifecycle": "LifecycleNetGasNOx",
    "GrossGasNOxLifecycle": "LifecycleGrossGasNOx",
    "CreditedIOU": "CreditedPA",
}
# Measure-level columns no longer written (0.1.50): restatements of an input or of another output, and the per-record checks, which are in the
# preflight findings and filing_validation files instead. A user's template or view naming one simply drops it.
OUTPUT_REMOVED = frozenset({
    "poc_PA", "poc_climate_zone", "poc_target_sector", "poc_shape_key", "poc_shape_key_2nd", "poc_shape_key_water", "poc_gas_profile_key", "poc_gas_sector",
    "poc_subset_key", "poc_eul_years_exact", "poc_eul_years", "poc_rul_years_exact", "poc_NTGRkWh", "poc_ntgr_kwh", "poc_ntgr_kw", "poc_ntgr_therm",
    "poc_ntgr_cost", "poc_ir_kwh", "poc_rr_kwh", "poc_ir_kw", "poc_rr_kw", "poc_ir_therm", "poc_rr_therm", "poc_annual_net_kwh_1st", "poc_annual_net_kwh_2nd",
    "poc_annual_net_kwh", "poc_annual_net_kw_1st", "poc_annual_net_kw_2nd", "poc_annual_net_kw", "poc_annual_net_therms", "poc_kwh_water_1st",
    "poc_kwh_water_2nd", "poc_water_net_kwh_1st", "poc_water_net_kwh_2nd", "poc_fuel_type", "poc_acc_version",
    "ProgramCosts", "WeightedSavings", "FilingValidation", "FilingRules", "ACCCheck",
})
# ReCET's name -> CET's name, for the renamed columns CET's outputs file also has (the bill impacts without Gross, and the emissions)
OUTPUT_TO_CET = {OUTPUT_RENAMES[old]: old for old in OUTPUT_RENAMES if old in ("BillReducElec", "BillIncrElec", "BillReducGas", "BillIncrGas")
                 or old.startswith(("Net", "Gross"))}


def output_name(column: str) -> str:
    """The current name of a measure-level output column given by an old name (its own name otherwise); "" for a column ReCET no longer writes."""
    c = str(column).strip()
    if c in OUTPUT_REMOVED:
        return ""
    return OUTPUT_RENAMES.get(c, c)


def cet_outputs_to_recet(df: pd.DataFrame) -> pd.DataFrame:
    """A CET outputs table with ReCET's names for the columns ReCET renamed (CET keeps BillReducElec, NetElecCO2 ...)."""
    back = {cet: recet for recet, cet in OUTPUT_TO_CET.items()}
    return df.rename(columns={c: back[c] for c in df.columns if c in back})
