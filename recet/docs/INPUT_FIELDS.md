# Input field reference

Every input `recet` reads, field by field: what it means, its unit, whether the column must be present, and where it is used (the `C..` numbers are sections of
[CALCULATIONS.md](CALCULATIONS.md)). Formats of the *supplied* files, matching rules and templates are in [INPUT_FORMATS.md](INPUT_FORMATS.md); what each GUI control does is in
[USER_GUIDE.md](USER_GUIDE.md); the outputs are in [OUTPUT_FIELDS.md](OUTPUT_FIELDS.md).

* [1. `Measure.csv`](#1-measurecsv) - the job's measures (CET format)
* [2. `ProgramCost.csv`](#2-programcostcsv) - program-level costs (CET format)
* [3. Run settings](#3-run-settings) - options chosen in the GUI, on the command line or in a saved settings file
* [4. Supplied input files](#4-supplied-input-files) - 8,760 profiles, gas monthly profiles, electric ACC, gas ACC
* [5. Reference data files](#5-reference-data-files-the-large-files) - the large files kept outside the package
* [6. Built-in tables](#6-built-in-tables-shipped-inside-the-package) - small tables shipped inside the package
* [7. CET outputs file](#7-cet-outputs-file-for-comparison) - the optional file to compare against

**Reading the tables.** *Required* = the column must exist in the file (a missing one stops the run and the preflight names it; a *blank cell* in a numeric column is read as the default stated in "Blank means").
*Carried* = read if present and copied to the output unchanged, never used in a calculation. Numeric columns must hold numbers or be blank; text in them is a blocking preflight error.
Four values cannot be blank because the arithmetic has no default for them - `NumUnits`, `EUL_Yrs`, `NTGRkWh` and `UnitkWh1stBaseline` (plus `UnitkWh2ndBaseline` when `RUL_Yrs > 0`); a blank is a blocking preflight error.
The preflight also **warns** (the run still proceeds) when a value is outside the range CET's own QC allows: the four `NTGR*` in (0, 1.5], `EUL_Yrs` in [0, 40], `RUL_Yrs` in [0, 30], the three `RealizationRate*` in (0, 2],
the three `InstallationRate*` in (0, 1], `MeasInflation` in [0, 1], the two `UnitMeaCost*` at least 0, and `RUL_Yrs` not below `EUL_Yrs`.

---------------------------------------------------------------------------------------------------------------------------------

## 1. `Measure.csv`

The same file you upload to the CET: pipe (`|`) delimited (or comma-delimited, see [INPUT_FORMATS.md](INPUT_FORMATS.md)), one header row, UTF-8, one row per `CEInputID`. All 74 CET columns are echoed to the measure-level output in the input's own text form.

**Field names: CET's legacy names are accepted.** Seven fields carry names from the E3 calculator the CET grew out of; ReCET names them for what they are.
A file may use **either name** for each of them (in `Measure.csv`, a comma-delimited file or the job workbook's Measure sheet; the legacy name is matched
without regard to case, as CET does). It is read under ReCET's name, and every output, template and saved workbook uses ReCET's name. A file that has both
names of the same field is rejected, since which one to use would be a guess. Files the Edit inputs tab saves for the CET (*Save as CET files*) use CET's names,
so they can be uploaded to the CET as they are.

| ReCET name | CET's name (also accepted) | What it is |
|---|---|---|
| `AC_Climate_Zone` | `E3ClimateZone` | the climate zone whose avoided costs and profile shape price the measure |
| `AC_Elec_Impact_Profile` | `E3MeaElecEndUseShape` | the electric impact profile (8,760 end-use shape) |
| `AC_Elec_Impact_Profile_2nd_Baseline` | `E3MeaElecEndUseShape2nd` | the electric impact profile of the 2nd-baseline years (ReCET only; not a CET field) |
| `AC_Elec_Impact_Profile_Water` | `E3MeaElecEndUseShapeWater` | the electric impact profile of the water energy (ReCET only; not a CET field) |
| `AC_Elec_Target_Sector` | `E3TargetSector` | the electric target sector (profile match and retail rate) |
| `AC_Gas_Impact_Profile` | `E3GasSavProfile` | the gas monthly impact profile |
| `AC_Gas_Target_Sector` | `E3GasSector` | the gas target sector (gas ACC class and retail rate) |

### 1.1 Identification and program link

| Field | Type / unit | Column | Description and use |
|---|---|---|---|
| `CEInputID` | text, unique | required | The measure's unique, persistent ID. The key of every output row and the join key to a CET outputs file. Blank or duplicate values stop the run. |
| `ACCVersion` | text | optional, **not a CET column** | The ACC version this measure is priced on: `2021`, `2022`, `2024` or `2026` (`2024 ACC` is also accepted). Blank, or a file without the column, means the run's "ACC version" setting. Measures on different versions are priced in one run, each on its own ACC's avoided costs, run parameters and E3 tables; program costs are then shared over all of them and discounted at the run's version's rates (CALCULATIONS C3.5). A supplied ACC file replaces only the run's own version. Reported per measure in `POC_ACC` and `poc_acc_version`. |
| `PrgID` | text | required | The program the measure belongs to; must equal a `PrgID` in `ProgramCost.csv` (and vice versa). Groups measures for the program-cost share (C11) and, through the program's `PA` in `ProgramCost.csv`, gives `CreditedIOU`. |
| `MeasDescription` | text | required | A description of the measure. Carried to the output; used only as a label. |
| `MeasureID` | text | carried | The measure's unique ID in the source workpaper. |
| `MeasCode` | text | carried | The program administrator's own measure code. |
| `ClaimYearQuarter` | `YYYYQn`, e.g. `2028Q3` | required | The quarter of the claim. Its year and quarter set when savings start and how they are discounted (C2). Must match the pattern or the run stops. |

### 1.2 Classification (carried, no effect on any result)

| Field | Type | Column | Description |
|---|---|---|---|
| `Sector` | text | required | Standard ex-ante sector (e.g. `Ind`). Only used to form the label `poc_subset_key`. |
| `DeliveryType` | text | carried | The method used to deliver the measure to the customer. |
| `BldgType` | text | carried | Standard ex-ante building type (sub-sector). |
| `MeasAppType` | text | carried | Measure application type (e.g. `ROB`). |
| `TechGroup`, `TechType` | text | carried | Standard technology group and type (e.g. `CoolRoof`). |
| `UseCategory`, `UseSubCategory` | text | carried | Standard end-use category and sub-category. |
| `WaterUse` | text | carried | Whether the water savings are indoor or outdoor. |
| `PreDesc`, `StdDesc` | text | carried | Descriptions of the pre-existing and standard (baseline) technologies. |
| `SourceDesc` | text | carried | The workpaper or DEER ID, with revision. |
| `Version` | text | carried | The workpaper / DEER version (e.g. `2013 Ex-Ante`). |
| `NormUnit` | text | carried | The normalizing unit that `NumUnits` counts (e.g. `Lamp`). |
| `NTG_ID`, `EUL_ID`, `RUL_ID`, `GSIA_ID` | text | carried | References to the ex-ante NTG, EUL, RUL and gross-savings/installation-adjustment tables. The *values* (`NTGR*`, `EUL_Yrs`, ...) are what the tool uses. |
| `Residential_Flag`, `Upstream_Flag` | 0/1 | carried | Flags for residential sites and upstream claims. |
| `MiscCostsDesc`, `MiscBensDesc` | text | carried | Descriptions of the miscellaneous costs and benefits. |
| `Comments` | text | carried | Free-text comments. |

### 1.3 Keys that select prices, profiles and rates

| Field | Type | Column | Description and use |
|---|---|---|---|
| `IOU_AC_Territory` | `PGE` `SCE` `SDGE` `SCG` | required | The utility whose avoided costs apply (the avoided-cost territory; formerly `PA`). Selects the ACC utility (C3.1, C3.2), the discount rate (C2.4), the retail rates (C9) and the profile match. Any other value is a blocking error. |
| `AC_Climate_Zone` | text, e.g. `13`, `3A` | required | The California climate zone. Selects the ACC zone (C3.1) and the profile's zone (C4.1). |
| `AC_Elec_Impact_Profile` | text | required | The name of the electric end-use load shape. Must equal a supplied or library profile's `end_use_name` (the library includes the TOU end uses as 8,760s) or the measure's electricity is **not priced** (C4.1). |
| `AC_Elec_Impact_Profile_2nd_Baseline` | text | optional, **not a CET column** | The electric end-use shape of the 2nd-baseline years of an early replacement (`0 < RUL_Yrs < EUL_Yrs`). Blank, or a file without the column, means `AC_Elec_Impact_Profile`. The 1st-baseline years are priced on `AC_Elec_Impact_Profile` and the 2nd-baseline years on this shape (CALCULATIONS C4.6). |
| `AC_Elec_Impact_Profile_Water` | text | optional, **not a CET column** | The electric end-use shape the measure's water energy (`UnitkWhIOUWater*`) is spread over, for both baselines. Blank, or a file without the column, means `AC_Elec_Impact_Profile`. Matched like `AC_Elec_Impact_Profile` (supplied or library profile, IOU, target sector, climate zone). See CALCULATIONS C4.6. |
| `AC_Elec_Target_Sector` | text | required | The target sector (e.g. `Residential`, `Non_Res`). Part of the profile match, and selects the retail rate schedule (C9) and the NOx/PM10 factors (C14). |
| `AC_Gas_Impact_Profile` | text | required | The name of the gas monthly savings profile (e.g. `Annual`, `Winter Only`). A name that matches nothing leaves gas savings **unpriced** (C4.3). A `...FuelSub...` name on a residential measure adds behind-the-meter methane (C3.2). |
| `AC_Gas_Target_Sector` | `Residential` `Commercial` `Core` | required | The gas customer class. Selects the gas ACC class (C3.2) and the gas rate schedule (C9). Must be one of the three for any measure with gas savings. |
| `FuelID` | text | optional | A DEER value list that identifies fuel substitution. `AllElec-New`, `FuelSub-ToElec` and `FuelSub-ToGas` mark the measure as fuel substitution (C1.5). |
| `MeasImpactType` | text | optional | The impact type. A value containing `FuelSub` also marks fuel substitution. |
| `CombustionType` | text | optional | The gas combustion type (a `LookupCode` of the combustion table, e.g. `Residential Furnaces (<0.3):Uncontrolled`). Gives the gas NOx rate (C14.3); blank or unknown means gas NOx = 0. |
| `RateScheduleElec`, `RateScheduleGas` | text | carried | CET fields that override a measure's retail rate schedule. **Not used**: the tool always maps the schedule from `IOU_AC_Territory` and the target / gas sector (C9). |

### 1.4 Quantities

| Field | Type / unit | Column | Blank means | Description and use |
|---|---|---|---|---|
| `NumUnits` | number, units of `NormUnit` | required | error | The number of units claimed. Multiplies every per-unit quantity and cost (`Q`). |
| `UnitkWh1stBaseline` | kWh per unit per year | required | error (enter 0 for none) | Electric energy savings per unit against the 1st baseline; negative = added load. Priced in C5.1 (`kWh1`). |
| `UnitkWh2ndBaseline` | kWh per unit per year | required | 0 (error when `RUL_Yrs > 0`) | The same against the 2nd baseline; used only after the RUL of an early-replacement measure (`kWh2`). |
| `UnitkW1stBaseline`, `UnitkW2ndBaseline` | kW per unit | required | 0 | Demand savings. Reported in `poc_annual_net_kw*` only: pricing is on kWh through the 8,760 shape, which carries the T&D value. |
| `UnitTherm1stBaseline`, `UnitTherm2ndBaseline` | therms per unit per year | required | 0 | Gas savings against the 1st / 2nd baseline; negative = added gas use (`Th1`, `Th2`; C5.4). |
| `UnitkWhIOUWater1stBaseline`, `UnitkWhIOUWater2ndBaseline` | kWh per unit per year | optional | 0 | The IOU-supplied energy embedded in the water a measure saves (positive) or uses (negative) (`Wt1`, `Wt2`; C5.5). If both columns are missing, water energy is 0. |

### 1.5 Useful life

| Field | Type / unit | Column | Blank means | Description and use |
|---|---|---|---|---|
| `EUL_Yrs` | years (fractional allowed) | required | error | The effective useful life. Savings run from the claim quarter for `EUL` years, in whole quarters with the last one prorated (C2.1). A value below 0.25 is treated as 0.25. |
| `RUL_Yrs` | years | required | 0 | The remaining useful life of the pre-existing equipment. 0 means a single baseline for the whole EUL; a positive value makes the measure an early replacement: 1st-baseline savings until the RUL, 2nd-baseline after (C2.3). |

### 1.6 Net-to-gross, realization and installation rates

| Field | Type | Column | Blank means | Description and use |
|---|---|---|---|---|
| `NTGRkWh` | fraction | required | error | The kWh net-to-gross ratio. Scales electric, water and other benefits and the electric emissions (C1.2, C7). |
| `NTGRkW` | fraction | required | `NTGRkWh` | The kW net-to-gross ratio; reported in `poc_annual_net_kw*` only. |
| `NTGRTherm` | fraction | required | `NTGRkWh`, then 1 | The therm net-to-gross ratio (C1.3). |
| `NTGRCost` | fraction | required | `NTGRkWh`, then 1 | The cost net-to-gross ratio; scales the participant cost (C8). |
| `RealizationRatekWh`, `RealizationRatekW`, `RealizationRateTherm` | fraction | required | 1 | The weighted realization rate for the claim. Multiplies the savings of that fuel (C1.2, C1.3). |
| `InstallationRatekWh`, `InstallationRatekW`, `InstallationRateTherm` | fraction | required | 1 | The weighted installation rate for the claim. Multiplies the savings of that fuel. |

### 1.7 Costs, incentives and inflation

All are dollars **per unit** (multiplied by `NumUnits`), blank = 0.

| Field | Unit | Column | Description and use |
|---|---|---|---|
| `UnitMeaCost1stBaseline` | $ per unit | required | The incremental measure cost over code / standard practice for a single-baseline measure (the desktop CET's "Gross Measure Cost"). Also used for the excess-incentive test (C8). |
| `UnitMeaCost2ndBaseline` | $ per unit | required | For an early-replacement measure (`RUL > 0`) the measure cost that counts in the cost tests (the desktop CET's "Gross Measure Cost_ER"); ignored when `RUL = 0` (C8). |
| `UnitDirectInstallLab` | $ per unit | required | Direct-install labor paid by the program. Counted as an incentive (C8). |
| `UnitDirectInstallMat` | $ per unit | required | Direct-install materials paid by the program. Counted as an incentive. |
| `UnitIncentiveToOthers` | $ per unit | required | Incentives paid to parties other than the end user. Counted as an incentive. |
| `UnitEndUserRebate` | $ per unit | required | Rebates paid to the end user. Counted in `RebatesandIncentsPV` and `GrossParticipantCostPV`, but not in the excess-incentive test. |
| `MeasInflation` | fraction per year, e.g. 0.02 | required | A measure-level inflation rate for the measure cost. Used only for early-replacement measures (`RUL > 0`), in `GrossMeasCostPV` (C8). |

### 1.8 Other benefits, other costs, tax credits

Dollars per unit, undiscounted, blank = 0 (C7).

| Field | Unit | Column | Description and use |
|---|---|---|---|
| `UnitGasInfraBens` | $ per unit | required | Benefits from offsetting gas infrastructure costs. In `OtherBen`. |
| `UnitRefrigBens` | $ per unit | required | Benefits of decreased refrigerant use or a lower-GWP refrigerant. In `OtherBen`. |
| `UnitMiscBens` | $ per unit | required | Other benefits. In `OtherBen`. |
| `UnitRefrigCosts` | $ per unit | required | Costs of increased refrigerant use or a higher-GWP refrigerant. In `OtherCost`. |
| `UnitMiscCosts` | $ per unit | required | Other costs. In `OtherCost`. |
| `UnitTaxCredits` | $ per unit | required | Federal (IRA) tax incentives. Summed into the TRC ratio's numerator with the other benefits (`TaxCredits`); not part of any cost or of `TotalSystemBenefit`. |

---------------------------------------------------------------------------------------------------------------------------------

## 2. `ProgramCost.csv`

Optional. One row per program per year; pipe or comma delimited. Every `PrgID` must appear in both files. Dollar amounts are **nominal dollars for the whole program-year** (not per unit). All ten cost fields are summed (C11.1);
blank = 0.

| Field | Type / unit | Description and use |
|---|---|---|
| `PrgID` | text | Program identifier; links to the measures. |
| `PA` | `PGE` `SCE` `SDGE` `SCG` (or `SW`) | The program administrator; selects the discount rate for this row's costs. A `SW` (statewide) row is treated as `SCE` unless statewide allocation is on (which sets it to each IOU). |
| `PrgYear` | year | The year of `ClaimYearQuarter`. Carried. |
| `ClaimYearQuarter` | `YYYYQn` | Its year is the year the cost is incurred and discounted from (C11.1). |
| `AdminCostsOverheadAndGA` | $ | Administration, overhead and general administration. |
| `AdminCostsOther` | $ | Other administration costs. |
| `MarketingOutreach` | $ | Marketing and outreach. |
| `DIActivity` | $ | Direct-implementation activity costs. |
| `DIInstallation` | $ | Direct-implementation installation costs. |
| `DIHardwareAndMaterials` | $ | Direct-implementation hardware and materials. |
| `DIRebateAndInspection` | $ | Direct-implementation rebate and inspection costs. |
| `EMV` | $ | Evaluation, measurement and verification. |
| `UserInputIncentive` | $ | Program-level incentive rebates. |
| `CostsRecoveredFromOtherSources` | $ | Costs recovered from other sources (enter as a negative to reduce the total, as CET does). |
| `OnBillFinancing` | $ | On-bill financing. **Not** included in the cost sum (CET's `SumCosts` omits it). |

---------------------------------------------------------------------------------------------------------------------------------

## 3. Run settings

Chosen in the GUI (each control is described in [USER_GUIDE.md](USER_GUIDE.md#the-gui-control-by-control)), on the command line, or in a saved settings file (`recet run --config job.json`; the GUI's *Settings > Save settings* writes one).
The JSON key is the name in `RunConfig`; command-line flags override a settings file.

| JSON key | GUI control | Command-line flag | Default | Meaning |
|---|---|---|---|---|
| `measures` | Measure.csv | `--measures` | (required unless a workbook is given) | Path to the CET `Measure.csv`. |
| `programcost` | ProgramCost.csv | `--programcost` | none | Path to `ProgramCost.csv`; without it there are no program costs (NoAdmin = regular costs, `WeightedProgramCost` = 0). |
| `workbook` | Job workbook (.xlsx) | `--workbook` | none | An Excel job workbook (section 4.7) used instead of the separate files; each of its data sheets supplies the matching input. A file given separately wins over its sheet. |
| `out_dir` | Output folder | `--out` | `recet_results` (GUI: `~/recet_results`) | Folder in which the run's folder is created. |
| `run_name` | Run name | `--name` | a timestamp | Prefix of the run folder name. |
| `acc_version` | Run against | `--acc` | `2024` | `2021`, `2022`, `2024` or `2026` (the ACC of that year; 2021 and 2022 are TRC only), or a new label defined by supplied ACC files. |
| `acc_electric_file`, `acc_gas_file` | Electric ACC file, Gas ACC file | `--acc-electric-file`, `--acc-gas-file` | none | Supplied ACC file(s); replace the built-in file of `acc_version`, or define a new version. |
| `settings_from` | Run parameters of ACC version (for a NEW ACC label) | `--settings-from` | `2026` | The ACC version whose built-in run parameters and other E3 tables (discount rates, emissions, rate schedules) a *new* ACC label uses. A built-in version uses its own unless this is set. Saved configurations from before 0.1.17 with `e3_version` (CET's numbering) are converted. |
| `run_parameters_file` | Run parameters | `--run-parameters` | none | Run parameters per PA (section 4.5): a listed PA's discount rates, base year and gas CO2 replace the built-in values for this run. |
| `programs_file` | Program metadata | `--programs` | none | Program metadata in the CEDARS Programs list layout (section 4.8): a `PrgID` replaces the built-in row, a new one is added. |
| `sw_factors_file` | SW allocation factors | `--sw-factors` | none | Statewide allocation factors (section 4.6): a (`Year`, `ProgramID`) row replaces the built-in row, a new one is added. Used only with `allocate_statewide`. |
| `profiles_8760_file`, `profiles_gas_file` | 8,760 electric profiles, Gas monthly profiles | `--profiles-8760`, `--profiles-gas` | none | Supplied profile files; a match replaces the library profile, a non-match is added. |
| `normalize_profiles` | (settings file / flag only) | `--no-normalize-profiles` | true | Rescale supplied profiles that do not sum to 1 (false = stop with an error instead). |
| `reference_dir` | Reference data folder | `--data-dir` | saved default | Folder holding the large reference files (section 5). |
| `components` | Calculate ACC components separately | `--components` | false | Price each ACC component separately (C16.1). |
| `portfolio_8760` | Produce combined 8760 electric load profile | `--portfolio-8760` | false | Write the portfolio's hourly net kWh (C16.2). |
| `profile_years` | Year(s) | `--profile-years` | First Year | Calendar years of the combined profile. |
| `market_effects` | Apply 5% market effects (benefits and costs) | `--market-effects` | 0.0 | 0 or 0.05, added to the NTG of benefits and of costs (CET's `MEBens` and `MECost` together; C13). |
| `include_societal` | Include societal cost tests | `--no-societal` (turns it off) | true | Compute SCB / SCH; off = those columns blank and the run about 3x faster. |
| `emissions` | Include emissions | `--no-emissions` (turns it off) | true | Compute the CO2 / NOx / PM10 columns (C14). |
| `allocate_statewide` | Allocate statewide (SW_) programs | `--allocate-statewide` | false | Split statewide programs across the IOUs before pricing (C15). |
| `apply_exclude_from_ce` | Leave Exclude_From_CE programs out of the portfolio summary | `--apply-exclude-from-ce` | false | Measures of programs whose `Exclude_From_CE` is true in the program metadata are left out of every summary row and shown in one `excluded` row (OUTPUT_FIELDS 2.2). They are still priced and in the measure-level table. |
| `base_year` | First Year | `--base-year` | earliest claim year | CET's First Year: the reference point of all discounting (C2.5). |
| `cet_outputs` | Compare with a CET outputs file | `--cet-outputs` | none | Path to a CET `*_outputs.csv` to compare against (section 7). |
| `compare_with` | Also run another ACC version | `--compare-with` | none | A second built-in ACC version to run and compare measure by measure. |
| `output_format` | Output format | `--format` | `csv` | `csv`, `csv.zip`, `xlsx` or `parquet` for every table written. `csv.zip` writes each CSV inside its own zip (`<table>.csv.zip` holding `<table>.csv`), about a fifth of the size: a 1,000,000-measure measure-level table is 3.5 GB as CSV and 0.74 GB zipped; the web app always uses it when CSV is chosen. `xlsx` writes one formatted Excel file per table (a table longer than Excel's 1,048,575 rows is written as `csv` instead, with a warning). |
| `preflight_only` | Preflight check only | `--preflight-only` / `preflight` | false | Check the inputs and stop before pricing. |
| `chunk_size` | (none) | `--chunk-size` | 20,000 | A job whose `Measure.csv` has more rows than this is run in parts of this many rows, so the run holds about one part at a time (0.96 GB peak at 1,000,000 records, against 17.8 GB as one piece; larger parts are a little faster and use more memory: 2.3 GB at 50,000; README Performance, USER_GUIDE "What a run does"). The files written are the same as a whole-job run's, values equal to rounding. 0 = always run the whole job at once. |
| `subset_step` | (none) | (none) | 1 | Test hook: price every Nth measure only. Program costs then weight a partial job; never use for a deliverable. |

---------------------------------------------------------------------------------------------------------------------------------

## 4. Supplied input files

Templates for each: GUI **Template...** buttons or `recet template ...`. Matching rules (a match *replaces* the built-in item, a non-match *adds* one) are in [INPUT_FORMATS.md](INPUT_FORMATS.md).

### 4.1 8,760-hour electric profiles (CSV or Parquet, long format)

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `end_use_name` | text | yes | Must equal the measure's `AC_Elec_Impact_Profile` (case-insensitive). |
| `pa` | `PGE` `SCE` `SDGE` `SCG`; blank / `*` / `ALL` = any | yes | The measure's `IOU_AC_Territory`. |
| `target_sector` | text; blank / `*` / `ALL` = any | yes | The measure's `AC_Elec_Target_Sector`. |
| `climate_zone` | `1`..`16`, `3A`, `3B`; `System` or `ALL` = every zone without its own rows | yes | The measure's `AC_Climate_Zone`. |
| `hour_of_year` | integer 1-8760 | yes | The hour, exactly once per (profile, zone). Hour 1 is 00:00 on 1 January of `represents_year`. |
| `impact` | fraction of the measure's annual net kWh | yes | The share of the year's savings that falls in the hour; each zone's 8,760 values should sum to 1 (a shape summing to about 0, i.e. pure load shifting, cannot be rescaled and is rejected). |
| `represents_year` | calendar year | optional | The year whose weekdays the shape follows (same on every row of a profile); default 2009. The shape is rotated once onto the ACC's weekdays (2018 or 2020, C4.2). |

### 4.2 Gas monthly profiles (CSV or Parquet)

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `Profile` | text | yes | Must equal the measure's `AC_Gas_Impact_Profile`. |
| `Month` | 1-12 or Jan..Dec | yes | Exactly one row per month per profile. |
| `Impact` | fraction of annual therms | yes | The month's share; the 12 values sum to 1 (rescaled, with a note, if not). |

### 4.3 Electric ACC (Parquet recommended; CSV/xlsx for small files)

One row per `(iou, climate_zone, cost_test, year, hour_of_year)`; dollar columns are **$/MWh**.

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `iou` | `PG&E` `SCE` `SDG&E` | yes | The utility (`PGE`, `SDGE` accepted in CSV and canonicalized). |
| `climate_zone` | `CZ1`..`CZ16` (`CZ3A`, `CZ3B`) | yes | The climate zone. Only the zones the utility serves need rows. |
| `cost_test` | `TRC`, `SCT Base`, `SCT High` | yes | `TRC` rows are required; the two SCT rows are needed for SCB / SCH only. |
| `year` | consecutive calendar years | yes | The pricing year. |
| `hour_of_year` | 1-8760 | yes | Every (iou, zone, cost test, year) block must have all 8,760 hours. |
| `total_avoided_cost` | $/MWh | yes | The sum of the component columns below (checked to 1e-6). |
| `transmission`, `distribution` | $/MWh | for water energy | The T&D part; `Gen` = total minus these (water energy is priced on `Gen`). |
| `emission_rate_tonnes_per_kwh` | metric tons CO2 per kWh | for electric CO2 | The grid emission rate of that hour. |
| `energy`, `generation_capacity`, `avoided_as_procurement`, `losses`, `methane_leakage`, `ghg_cap_and_trade`, `ghg_portfolio_rebalancing` (2024 ACC) or `ghg_cap_and_invest` (2026 ACC), `ghg_adder`, `air_quality_adder` | $/MWh | for components mode | The avoided-cost components. |
| `price_zone`, `datetime`, `month`, `day`, `hour` | - | no | Ignored. |

### 4.4 Gas ACC (CSV or Parquet)

One row per `(utility, class, cost_test, scc, year)`; dollar columns are **$/therm**.

| Column | Type / unit | Description |
|---|---|---|
| `utility` | `PG&E`, `Socal Gas` (or `SoCalGas`), `SDG&E` | The gas utility. |
| `class` | `Residential`, `Commercial`, `Total Core` | The customer class (`AC_Gas_Target_Sector`: Residential, Commercial, Core). |
| `cost_test` | `TRC` or `SCT` | `TRC` rows are required. |
| `scc` | blank for `TRC`; `Base` or `High` for `SCT` | The social-cost-of-carbon scenario. |
| `year` | consecutive calendar years | The pricing year; years outside the file use its nearest year. |
| `market_jan` ... `market_dec` | $/therm | Commodity price by month. |
| `td_jan` ... `td_dec` | $/therm | Transportation / distribution by month. |
| `nox`, `co2`, `upstream_methane`, `air_quality_adder` | $/therm | Annual values (used in every month). |
| `behind_meter_methane` | $/therm | Annual value; included only for residential fuel-substitution profiles (C3.2). |

---------------------------------------------------------------------------------------------------------------------------------

### 4.5 Run parameters (CSV, Parquet or xlsx)

The built-in run parameters are the RunParameters table (section 6). A supplied file replaces them for the PAs it lists; other PAs keep the built-in values. The template
(`recet template run-parameters`) is the built-in rows of the chosen ACC version.

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `PA` | `PGE` `SCE` `SDGE` `SCG` (`PG&E` / `SDG&E` / `SoCalGas` accepted) | yes | The PA whose built-in run parameters this row replaces. One row per PA. |
| `DiscountRateAnnual` | fraction (0.0766 = 7.66%) | yes | Annual discount rate: program costs and bill impacts (C2.4, C9, C11). |
| `DiscountRateQtr` | fraction | optional (default `DiscountRateAnnual / 4`) | Quarterly rate for measure-level streams (C2.4). CET stores the annual rate / 4. |
| `DiscountRateAnnual_SCT` | fraction | yes | Annual societal discount rate (SCB / SCH). |
| `DiscountRateQtr_SCT` | fraction | optional (default `DiscountRateAnnual_SCT / 4`) | Quarterly societal rate. |
| `BaseYear` | year | optional (default: the built-in value for the run's version) | CalcCE's `Yr1` base for the bill-impact window (C9). |
| `CO2Gas` | metric tons CO2 per therm | yes | Gas CO2 factor for the emission columns (C14). |

### 4.6 SW allocation factors (CSV, Parquet or xlsx)

The built-in table's layout (section 6); the template (`recet template sw-factors`) is the whole built-in table. A row whose (`Year`, `ProgramID`) is in the built-in
table replaces it; any other row is added. Factor sums that are not 1 (or 0) are reported in the log.

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `Year` | year | yes | The claim year the factors apply to (a measure's `ClaimYearQuarter` year; a program cost's `PrgYear`). |
| `ProgramID` | text | yes | The statewide program's `PrgID`. |
| `PGEGasFactor`, `SDGEGasFactor`, `SCEGasFactor`, `SCGGasFactor` | fraction | yes | Each IOU's share of the program's therms; should sum to 1 (or all be 0 when the program has no gas). |
| `PGEElectricFactor`, `SDGEElectricFactor`, `SCEElectricFactor`, `SCGElectricFactor` | fraction | yes | Each IOU's share of the program's kWh, kW and water kWh; should sum to 1 (or all 0). |
| `PGEWeightedFactor`, `SDGEWeightedFactor`, `SCEWeightedFactor`, `SCGWeightedFactor` | fraction | one of these or `PercentElectric` | Each IOU's share of costs and other benefits. If absent, computed as `PercentElectric x ElectricFactor + (1 - PercentElectric) x GasFactor`. |
| `PercentElectric` | fraction | see above | The program's electric share, used only to compute the weighted factors. |
| `ProgramName`, `Summary` | text | optional | Carried, not used. |

### 4.7 Job workbook (xlsx)

One Excel file instead of the separate files (`recet template workbook`). Sheet names are matched ignoring case, spaces and underscores; other sheets are ignored, and
a sheet with only a header row counts as not given.

| Sheet | Required | Same columns as |
|---|---|---|
| `Measure` | yes | `Measure.csv` (section 1) |
| `ProgramCost` | no | `ProgramCost.csv` (section 2) |
| `RunParameters` | no | section 4.5 |
| `SWAllocation` | no | section 4.6 |
| `Programs` | no | section 4.8 |
| `GasProfiles` | no | section 4.2 |
| `Profiles8760` | no | section 4.1 (Excel fits about 119 profile x zone blocks) |

Cells are read as CET text: a whole number without decimals (`13`, not `13.0`), other numbers exactly, empty cells blank, `TRUE`/`FALSE` as `1`/`0`; no cell may contain `|`.
The ACC files are not sheets: supply them as separate files.

### 4.8 Program metadata (CSV or xlsx)

The CEDARS Programs list layout (the Programs page's download); the template (`recet template programs`) is the whole built-in table. A row whose `PrgID` is in the
built-in table replaces that row as a whole; any other row is added. Only `PA` and `PrgID` are required; a missing column is blank and an extra column is carried as
`Prg_<column>`. Values are text, kept as written; a flag counts as true when it reads `True`, `1`, `Yes`, `Y` or `T` (any case). The metadata changes no priced value
(OUTPUT_FIELDS 1.14). Statewide programs are listed once per funding IOU (`PGE_SW_...`, `SCE_SW_...`, `SDGE_SW_...`, `SCG_SW_...`) plus an `SW_...` row.

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `PA` | text | yes | The program administrator. |
| `PrgID` | text | yes | The program ID, unique; matched to the measure's `PrgID` as priced. |
| `ProgramName`, `ProgramStatus`, `ProgramSegment`, `StatewideProgram`, `PrimarySector`, `Sector`, `BPSector`, `ProgramImplementer`, `ProgramCategory`, `ImplementationContractor`, `ParentProgram`, `DirectInstall`, `Financing` | text | no | Descriptive fields (OUTPUT_FIELDS 1.14). `ProgramSegment` gives the summary's `program segment` rows. |
| `StartYear`, `EndYear` | year | no | The program's years; a claim year outside them is reported by the preflight (`claim_outside_program_years`). |
| `Resource_Flag`, `NonResource_Flag`, `Deemed_Flag`, `Custom_Flag`, `Upstream_Flag`, `Midstream_Flag`, `Downstream_Flag`, `Audit_Flag`, `Exclude_From_Budget`, `Exclude_From_Statewide_Flag` | flag | no | Carried. |
| `Exclude_From_CE` | flag | no | Used by `apply_exclude_from_ce`. |

## 5. Reference data files (the large files)

Kept in the *reference data folder* (GUI box, `--data-dir`, `RECET_DATA`, or `recet data set-dir`), not in the package. `recet data status` lists what is found.

| File in the folder | Size | Contents |
|---|---|---|
| `acc/2024/electric_hourly.parquet` | ~1 GB | The 2024 ACC's hourly electric avoided costs; the layout of section 4.3. |
| `acc/2026/electric_hourly.parquet` | ~1 GB | The 2026 ACC's (final, v1b), same layout. |
| `impact_profiles/catalog.parquet` | small | The CEDARS impact-profile catalog: one row per profile. Columns used: `id` (profile ID), `end_use_name` (= `AC_Elec_Impact_Profile`), `iou_ac_territory` (`PGE`...), `target_sector`, `year_represented` (the calendar year the shape follows; blank = 2009). Other columns (`pa` long name, `climate_zones`, `start_year`, `explicit_end_year`, `description`, `source_impact_type`, `ac_version`) are informational. |
| `impact_profiles/hourly.parquet` | ~9 GB | The 8,760-hour profiles: `impact_profile_id` (matches `id`), `climate_zone` (a zone or `System`), `hour_of_year` (1-8760), `impact` (fraction of annual savings in that hour; each (profile, zone) sums to 1). One row group per profile. |

---------------------------------------------------------------------------------------------------------------------------------

## 6. Built-in tables (shipped inside the package)

ReCET's names drop the `E3` prefix of the CET tables they copy; the CET name is given in brackets.

| File (`recet/data/...`) | Columns used | Role |
|---|---|---|
| `run_parameters.csv` (RunParameters; CET `E3Settings`) | `PA`, `Version`, `DiscountRateAnnual`, `DiscountRateQtr` (annual / 4), `DiscountRateAnnual_SCT`, `DiscountRateQtr_SCT`, `BaseYear` (2024), `CO2Gas` (metric tons CO2 per therm, 0.00531) | The built-in **run parameters**: discount rates and the gas CO2 constant, by utility and CET `Version`, read by ACC version through the mapping in C3.3 (C2.4, C9, C14). `AvoidedCostVersion` is the publication date; `NOxGas` and the `Societal...` rate columns are not used. |
| `cet_tables/emissions.parquet` (Emissions; CET `E3Emissions`) | `PA`, `Version`, `TS` (target sector), `EU` (end use), `CZ`, `NOx`, `PM10` | Electric NOx and PM10 factors per annual kWh; no matching row means 0 (C14.3). Its `CO2` column is used only for the CET-table comparison option. |
| `cet_tables/combustion_type.parquet` (CombustionType; CET `E3CombustionType`) | `LookupCode` (= `CombustionType`), `NOx` (per therm) | Gas NOx factor (C14.3). |
| `cet_tables/rate_schedule_elec.parquet` (RateScheduleElec; CET `E3RateScheduleElec`) | `PA`, `Version`, `Year`, `Schedule`, `RateE` ($/kWh) | Retail electric rates by year (C9). |
| `cet_tables/rate_schedule_elec_mapping.parquet` (RateScheduleElecMapping; CET `E3RateScheduleElecMapping`) | `PA`, `Version`, `TargetSector`, `Schedule` | Maps a target sector to its schedule. |
| `cet_tables/rate_schedule_gas.parquet`, `rate_schedule_gas_mapping.parquet` (RateScheduleGas, RateScheduleGasMapping; CET `E3RateScheduleGas`, `E3RateScheduleGasMapping`) | `PA`, `Version`, `Year`, `Schedule`, `RateG` ($/therm); `GasSector`, `GasRateSchedule` | The same for gas. |
| `gas_savings_profiles.csv` | `Profile`, `Month`, `Impact` | The library of gas monthly profiles (C4.3). |
| `acc/2024/gas_components.parquet`, `acc/2026/gas_components.parquet` | the gas ACC layout of section 4.4 | The two gas ACCs, extracted from E3's workbooks. |
| `statewide_allocation/statewide_factors_20260918.csv` | `Year`, `ProgramID`, `PercentElectric`, `<IOU>GasFactor`, `<IOU>ElectricFactor`, `<IOU>WeightedFactor` for PGE, SDGE, SCE, SCG | The IOU shares of each statewide program (C15). |
| `programs/cedars_programs_20261003.csv` | the columns of section 4.8 | The built-in program metadata: CEDARS' Programs list of 2026-10-03 (1,416 programs), without its `ProgramManager` column. |
| `statewide_allocation/field_rules.csv` | `table`, `field`, `rule` | Which factor (or pass-through) applies to each field of `Measure.csv`, `ProgramCost.csv` and the CET output (C15). |

---------------------------------------------------------------------------------------------------------------------------------

## 7. CET outputs file (for comparison)

Optional. A CET run's `*_outputs.csv` (pipe or comma delimited) with a `CET_ID` column; measures are matched to the POC's `CEInputID`. Columns compared when present: `TotalSystemBenefit`, `ElecBen`, `GasBen`, `WaterEnergyBen` (+ `_SB`, `_SH`,
`Gross`), `OtherBen`, `ElecSupplyCost`, `GasSupplyCost`, `WaterEnergyCost`, `TRCCost`, `PACCost`, `RIMCost`, `SCBCost`, `SCHCost`, `TRCRatio`, `PACRatio`, `RIMRatio`, `NetElecCO2`, `NetGasCO2`, `BillReducElec`, `BillReducGas`. The comparison is only
meaningful when the job is run the way the CET run was (same First Year, ACC version, and statewide allocation).
