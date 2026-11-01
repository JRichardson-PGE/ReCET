"""
Loads a CET Measure.csv (pipe delimited) into the measure frame the pricing reads (_derive_fields): the Measure fields under their own names, plus the derived
values named for the output fields they are written as (Used_Year, Used_Quarter, AC_Net_kWh_1st_Baseline ..., Fuel_Sub_Flag) or Used_<field>.
load_measures_from_csv can first split statewide programs across the IOUs (pipeline/statewide_allocation.py).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .delimited import read_cet_csv


def load_measures_from_csv(measure_csv_path: Path, allocate_statewide: bool = False, dtypes: dict | None = None,
                           extra_columns: tuple[str, ...] | list[str] = ()) -> pd.DataFrame:
    """Generic loader: any real CET Measure.csv (pipe- or comma-delimited) in, derived-fields DataFrame out.

    allocate_statewide=True first splits any statewide-program rows across the
    IOUs (pipeline/statewide_allocation.py): one row per IOU with `<IOU>_`
    prepended to CEInputID and the per-field factors applied to the RAW
    columns, before any derived field is computed. Non-statewide rows are
    untouched.

    dtypes: column types to read with instead of inferring them from the file (delimited.part_dtypes): a part of a larger file is read with the types the
    whole file would get, so it loads exactly as those rows do in the whole file.

    extra_columns: Measure columns beyond CET's that the run's cost-test set reads (input_streams.extra_columns): kept in the frame as numbers, a blank or a
    missing column = 0 (jobs.check_input_columns stops the run first on a value that is not a number). With statewide allocation they take the Weighted
    Factor, as CET's per-unit benefit and cost fields do (UnitMiscBens ...)."""
    df = read_cet_csv(measure_csv_path, low_memory=False, **({"dtype": dtypes} if dtypes else {}))
    from ..templates import with_defaults
    df = with_defaults(df)                          # the minimal input set: a missing descriptive column is read as blank (BR-06.15)
    if allocate_statewide:
        from .statewide_allocation import allocate_measure_inputs, warn_unmatched
        rules = None
        if extra_columns:
            from .statewide_allocation import load_field_rules
            rules = dict(load_field_rules()["measure"])
            rules.update({c: "Weighted Factor" for c in extra_columns if c not in rules})
        df, report = allocate_measure_inputs(df, rules=rules)
        warn_unmatched(report, "Measure.csv")
    return _derive_fields(df, extra_columns)


def acc_versions(col: pd.Series) -> pd.Series:
    """The ACCVersion column as version keys: "2024 ACC" and 2024.0 (the column read as numbers because it has blanks) both give "2024"; blank gives ""."""
    v = col.astype(str).str.strip().str.replace(r"(?i)\s*ACC$", "", regex=True).str.replace(r"\.0$", "", regex=True)
    return v.where(~col.isna() & (v.str.lower() != "nan"), "")


# Quantities the electric, water and gas pricing apply to each baseline: per unit (Used_UnitkWh...) and the measure's net annual amounts (AC_Net_...).
FIRST_BASELINE_QUANTITIES = ["Used_UnitkWh1stBaseline", "AC_Net_kWh_1st_Baseline", "AC_Net_kWh_ScaledProfile_1st_Baseline", "UnitTherm1stBaseline",
                             "AC_Net_Therms_1st_Baseline", "UnitkWhIOUWater1stBaseline", "AC_Net_kWh_Water_1st_Baseline"]
SECOND_BASELINE_QUANTITIES = ["Used_UnitkWh2ndBaseline", "AC_Net_kWh_2nd_Baseline", "AC_Net_kWh_ScaledProfile_2nd_Baseline", "UnitTherm2ndBaseline",
                              "AC_Net_Therms_2nd_Baseline", "UnitkWhIOUWater2ndBaseline", "AC_Net_kWh_Water_2nd_Baseline"]
# CET-optional Measure fields (no NotEmpty in CET's spec): a blank is read as 0. Every other numeric field CET requires, and the preflight fails a blank.
OPTIONAL_ZERO = ["UnitkWhIOUWater1stBaseline", "UnitkWhIOUWater2ndBaseline", "UnitDirectInstallLab", "UnitDirectInstallMat", "UnitEndUserRebate",
                 "UnitIncentiveToOthers", "UnitTaxCredits", "MeasInflation"]
FUEL_SUB_FUEL_IDS = ("AllElec-New", "FuelSub-ToElec", "FuelSub-ToGas")
COLUMNS = [
    "CEInputID", "PrgID", "MeasDescription", "IOU_AC_Territory", "AC_Climate_Zone", "AC_Elec_Target_Sector", "AC_Elec_Impact_Profile",
    "AC_Elec_Impact_Profile_2nd_Baseline", "AC_Elec_Impact_Profile_Water", "AC_Gas_Impact_Profile", "AC_Gas_Target_Sector", "Used_Year", "Used_Quarter",
    "EUL_Yrs", "RUL_Yrs", "NumUnits",
    "UnitkWh1stBaseline", "UnitkWh2ndBaseline", "Used_UnitkWh1stBaseline", "Used_UnitkWh2ndBaseline", "UnitkW1stBaseline", "UnitkW2ndBaseline",
    "UnitTherm1stBaseline", "UnitTherm2ndBaseline", "UnitkWhIOUWater1stBaseline", "UnitkWhIOUWater2ndBaseline",
    "NTGRkWh", "NTGRkW", "NTGRTherm", "NTGRCost", "InstallationRatekWh", "RealizationRatekWh", "InstallationRatekW", "RealizationRatekW",
    "InstallationRateTherm", "RealizationRateTherm",
    "UnitMeaCost1stBaseline", "UnitMeaCost2ndBaseline", "UnitIncentiveToOthers", "UnitEndUserRebate", "UnitDirectInstallLab", "UnitDirectInstallMat",
    "MeasInflation", "UnitGasInfraBens", "UnitRefrigCosts", "UnitRefrigBens", "UnitMiscCosts", "UnitMiscBens", "UnitTaxCredits", "CombustionType",
    "AC_Net_kWh_1st_Baseline", "AC_Net_kWh_2nd_Baseline", "AC_Net_kWh_ScaledProfile_1st_Baseline", "AC_Net_kWh_ScaledProfile_2nd_Baseline",
    "AC_Net_Therms_1st_Baseline", "AC_Net_Therms_2nd_Baseline", "AC_Net_kWh_Water_1st_Baseline", "AC_Net_kWh_Water_2nd_Baseline",
    "Fuel_Sub_Flag", "Scaled_Profile_Flag", "ACCVersion",
]
# ReCET-only columns for a true-up (BRD BR-05.6): the electric profile of the claim it adjusts (its parent), and that claim's 2nd-baseline profile (blank =
# AC_Elec_Impact_Profile_Parent). A record that names a parent is also priced on the parent's profile, and its incremental columns are the difference
# (run_poc.parent_profile_measures, CALCULATIONS C21). Kept in the frame only when the file has one of the columns, so a job without them is unchanged.
PARENT_PROFILE_COLUMNS = ("AC_Elec_Impact_Profile_Parent", "AC_Elec_Impact_Profile_Parent_2nd_Baseline")
PARENT_WATER_PROFILE = "Parent_Used_AC_Elec_Impact_Profile_Water"   # the water-energy profile on the parent's profile: the file's, else the parent's


def _derive_fields(df: pd.DataFrame, extra_columns: tuple[str, ...] | list[str] = ()) -> pd.DataFrame:
    """The measure frame the pricing reads: the Measure fields under their own names (text keys stripped, CET-optional blanks read as 0) and the few derived values,
    named for the output field they are written as, or Used_<field> for a value derived from a field.

    The preflight fails a blank in any field CET marks NotEmpty, so those fields are used as filed; without the preflight a blank turns that measure's results blank.

    DUAL BASELINE: UnitkWh1stBaseline and UnitkWh2ndBaseline (and the kW / therm pairs) are each a per-unit annual savings for a different part of the measure's
    life, not two usage levels to subtract: the 1st baseline (against the existing equipment) from the claim quarter for RUL_Yrs years, the 2nd (against code)
    from then to EUL_Yrs. RUL_Yrs = 0 means not an early replacement: the 1st baseline for the whole EUL (confirmed on CET's SWWH025 output; engine._phased_scale
    blends the quarter holding the RUL)."""
    df = df.copy()
    for c in OPTIONAL_ZERO:
        df[c] = df[c].fillna(0.0) if c in df.columns else 0.0
    df["PrgID"] = df["PrgID"].astype(str).str.strip()
    for c in ("AC_Climate_Zone", "AC_Elec_Target_Sector", "AC_Elec_Impact_Profile"):
        df[c] = df[c].astype(str)
    # ReCET-only columns: a separate electric profile for the 2nd-baseline years and for the water energy. Blank = the measure's AC_Elec_Impact_Profile.
    given = {}
    for c in ("AC_Elec_Impact_Profile_2nd_Baseline", "AC_Elec_Impact_Profile_Water"):
        given[c] = _text(df, c)
        df[c] = given[c].where(given[c] != "", df["AC_Elec_Impact_Profile"])
    parent_columns = []
    if any(c in df.columns for c in PARENT_PROFILE_COLUMNS):         # a true-up's parent profile (BR-05.6): blank = no parent
        parent, parent_2nd = _text(df, PARENT_PROFILE_COLUMNS[0]), _text(df, PARENT_PROFILE_COLUMNS[1])
        df[PARENT_PROFILE_COLUMNS[0]] = parent
        df[PARENT_PROFILE_COLUMNS[1]] = parent_2nd.where((parent_2nd != "") | (parent == ""), parent)
        given_water = given["AC_Elec_Impact_Profile_Water"]
        df[PARENT_WATER_PROFILE] = given_water.where(given_water != "", parent)
        parent_columns = [*PARENT_PROFILE_COLUMNS, PARENT_WATER_PROFILE]
    df["CombustionType"] = df["CombustionType"] if "CombustionType" in df.columns else None
    df["EUL_Yrs"] = df["EUL_Yrs"].astype(float)
    df["RUL_Yrs"] = df["RUL_Yrs"].astype(float)
    df["Used_Year"] = df["ClaimYearQuarter"].astype(str).str[:4].astype(int)
    df["Used_Quarter"] = df["ClaimYearQuarter"].astype(str).str[-1].astype(int)     # the quarter (1-4) the benefit stream starts in, not always Q1

    # per-unit kWh actually used: the filed value, or (run_poc.apply_scaled_profiles) the annual kWh of a kWh-per-unit profile
    df["Used_UnitkWh1stBaseline"] = df["UnitkWh1stBaseline"].astype(float)
    df["Used_UnitkWh2ndBaseline"] = df["UnitkWh2ndBaseline"].astype(float)
    df["Scaled_Profile_Flag"] = False
    # The net annual quantities each baseline is priced on: NumUnits x per-unit amount x NTG x installation rate x realization rate (CalcCE's Qty x NTGR x IR x RR).
    for b, n in (("1st", "1st"), ("2nd", "2nd")):
        kwh = df["NumUnits"] * df[f"Used_UnitkWh{n}Baseline"] * df["NTGRkWh"] * df["InstallationRatekWh"] * df["RealizationRatekWh"]
        df[f"AC_Net_kWh_{b}_Baseline"] = kwh
        df[f"AC_Net_kWh_ScaledProfile_{b}_Baseline"] = kwh          # what a share profile (summing to 1) is multiplied by; differs on a kWh-per-unit profile
        df[f"AC_Net_Therms_{b}_Baseline"] = (df["NumUnits"] * df[f"UnitTherm{n}Baseline"] * df["NTGRTherm"] * df["InstallationRateTherm"]
                                             * df["RealizationRateTherm"])
        df[f"AC_Net_kWh_Water_{b}_Baseline"] = (df["NumUnits"] * df[f"UnitkWhIOUWater{n}Baseline"] * df["NTGRkWh"] * df["InstallationRatekWh"]
                                                * df["RealizationRatekWh"])

    # CalcCE books a negative value as a positive supply cost only for fuel-substitution measures (its FuelTypesForFuelSubstitution join). EMPIRICAL rule (CET run
    # 121219, 5,518 measures, 0 misses): FuelID is one of the three types OR MeasImpactType contains "FuelSub" (Deem-WP-FuelSub / Deem-DEER-FuelSub rows with a
    # blank FuelID were treated as fuel-sub: 301 of 504 supply-cost measures). See run_poc._split_ben_supply.
    fuel_sub_by_fuel_id = df["FuelID"].isin(FUEL_SUB_FUEL_IDS) if "FuelID" in df.columns else False
    fuel_sub_by_impact_type = df["MeasImpactType"].astype(str).str.contains("FuelSub") if "MeasImpactType" in df.columns else False
    df["Fuel_Sub_Flag"] = fuel_sub_by_fuel_id | fuel_sub_by_impact_type
    # ACCVersion (ReCET-only column, BRD BR-06.5): the ACC this measure is priced on, spelt as a version key ("2024 ACC" -> "2024"). Blank = the run's ACC.
    df["ACCVersion"] = acc_versions(df["ACCVersion"]) if "ACCVersion" in df.columns else ""
    extra = [c for c in dict.fromkeys(extra_columns) if c not in COLUMNS]     # a cost-test set's input-stream columns (pipeline/input_streams.py)
    for c in extra:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df.columns else 0.0
    return df[COLUMNS + parent_columns + extra]


def _text(df: pd.DataFrame, c: str) -> pd.Series:
    """A text column stripped, a blank or a missing column as ""."""
    return df[c].where(df[c].notna(), "").astype(str).str.strip() if c in df.columns else pd.Series("", index=df.index)
