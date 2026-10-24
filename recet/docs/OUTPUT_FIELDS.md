# Output field reference

Every column and file `recet` writes: what it means, its unit, **its complete formula in terms of the input fields**, and a worked number.
[INPUT_FIELDS.md](INPUT_FIELDS.md) defines the input fields; [CALCULATIONS.md](CALCULATIONS.md) explains the method behind the formulas (which avoided-cost rows and load shape are
used and why, the validation against CET, the limitations).

This page can be read without leaving it:

* **Section 0** defines, once, the handful of intermediate quantities the formulas are built from: the quarters of a measure's life, the discount factors, the baseline phasing, the
  annual quantities that are priced, the price of a quarter, and the lifetime values `V_elec`, `V_gas` and `V_water`. Each is defined from input fields.
* **Section 1** gives every measure-level column its own entry: meaning, unit, formula, what each symbol in the formula is, the inputs it depends on, and a worked number.
* **Section 2** does the same for every other file a run writes.
* **Section 3** holds the two worked examples behind the numbers in the entries: their inputs, quarter-by-quarter tables, and every output column for both.

Every number on an `Example` line and in section 3 is checked against a real run by the test-suite (`tests/test_output_docs.py`), and so is the arithmetic written on each line, so the examples cannot drift from the tool.

**How an entry reads.**

* **Formula** - the column written out in input-field names. Where a step is shared it uses a named intermediate from section 0 (`V_elec`, `DF(k)`, `phased(...)`).
* **Where** - what each intermediate or symbol in that formula is, defined from input fields (or a pointer to the entry of the output column it comes from).
* **Inputs** - the input fields, run settings and reference data the column depends on. A change in anything not listed cannot change it.
* **Example A / Example B** - the column's value for the two worked examples of section 3, with the arithmetic where it fits on one line.

**Units used throughout.** Money is nominal US dollars. A "PV" is present-valued to the run's First Year at the quarterly discount rate (bill impacts and program costs annually).
Ratios have no unit. "Net" values include net-to-gross (and the market-effects adder if on); "Gross" values do not. Emissions: CO2 in metric tons; NOx and PM10 in pounds.
A column is blank (`NaN`) only where it is switched off (societal columns without SCB/SCH, emissions without "Include emissions", components without components mode)
or cannot be computed.

* [0. Terms used in the formulas](#0-terms-used-in-the-formulas)
* [1. The measure-level table](#1-the-measure-level-table) (`measure_level_acc<version>.csv`)
* [2. Other files in a run folder](#2-other-files-in-a-run-folder)
* [3. The two worked examples](#3-the-two-worked-examples)

---------------------------------------------------------------------------------------------------------------------------------

## 0. Terms used in the formulas

### 0.1 Notation, defaults and run-level values

* **Input fields** are written exactly as in the file (`NumUnits`, `UnitkWh1stBaseline`); **output fields** the same way, and a formula that uses another output column says so.
* **Blank inputs** take the default the tool uses (INPUT_FIELDS.md gives each): a blank quantity (`UnitkWh2ndBaseline`, `UnitTherm1stBaseline`, ...) is 0; a blank
  `InstallationRate...` or `RealizationRate...` is 1; a blank `NTGRkW`, `NTGRTherm` or `NTGRCost` takes `NTGRkWh` (the values used are in `poc_ntgr_kw`, `poc_ntgr_therm`,
  `poc_ntgr_cost`); `EUL_Yrs` is at least 0.25; a blank `RUL_Yrs` is 0.
* **Operators.** `SUM over k ... of` is a sum; `pos(x) = max(x, 0)`; `ratio(n, d) = n / d`, and 0 when `d = 0` (the way CET's ratio columns behave); `^` is a power;
  `floor(x)` rounds down.
* **Run settings.** `F` is the run's **First Year** (the "First Year" box or `--base-year`; blank = the earliest claim year in the job). `MEB` is the market-effects adder on
  benefits and `MEC` the adder on costs: both 0.05 when "Apply 5% market effects (benefits and costs)" is on, both 0 otherwise.
* **Territory and discount rates.** `PA` is `IOU_AC_Territory`. The rates come from CET's E3 settings for `PA` and the ACC version: `DiscountRateAnnual`,
  `DiscountRateQtr` (the annual rate divided by 4), and the societal `DiscountRateAnnual_SCT`, `DiscountRateQtr_SCT`. For PG&E (every built-in ACC):
  0.0766, 0.01915, 0.0503 and 0.012575.

```text
Rqf = 1 + DiscountRateQtr          Rqf_sct = 1 + DiscountRateQtr_SCT          the quarterly growth factors
Raf = 1 + DiscountRateAnnual       Raf_sct = 1 + DiscountRateAnnual_SCT       the annual ones (bill impacts, program costs)
```

### 0.2 The quarters of a measure's life

CET prices in **calendar quarters**. A measure delivers savings in `n` quarters, numbered `k = 0 ... n-1`; `k = 0` is the claim quarter itself.

```text
Y0 = first four characters of ClaimYearQuarter        q0 = its last character (1-4)
EQ = 4 * max(EUL_Yrs, 0.25)                           the useful life in quarters  (poc_eul_years_exact * 4)
n  = floor(EQ) + 1   if EQ - floor(EQ) > 0.000000001, else floor(EQ)       the number of quarters (a part-quarter counts as one)

a(k)       = (q0 - 1) + k
year(k)    = Y0 + floor(a(k) / 4)                     the calendar year of quarter k
quarter(k) = a(k) mod 4                               0 = Q1, 1 = Q2, 2 = Q3, 3 = Q4
w(k)       = EQ - floor(EQ)   if k = n-1 and EQ - floor(EQ) > 0.000000001        the quarter's weight: the last, part-quarter of a fractional life
           = 1                for every other quarter
```

A claim in Q3 delivers two quarters in its first calendar year. A life of 2.4 years is 9.6 quarters: ten quarters, the last weighted 0.6.
The hours of a quarter are numbered on the ACC's 8,760-hour calendar: Q1 = hours 1-2160, Q2 = 2161-4344, Q3 = 4345-6552, Q4 = 6553-8760 (2160, 2184, 2208 and 2208 hours).
*Example A* (claim 2028Q3, `EUL_Yrs` 2.4): `n` = 10, `k = 0` is 2028Q3, and `w(9)` = 0.6. *Example B* (claim 2028Q2, `EUL_Yrs` 2.5): `n` = 10, `k = 0` is 2028Q2. Tables 3.2 and 3.4 list every quarter.

### 0.3 Discounting

```text
Qac(k) = (year(k) - F) * 4 + quarter(k)          calendar quarters from Q1 of the First Year (not from the claim; negative before it)
DF(k)  = 1 / Rqf ^ Qac(k)                             the quarterly discount factor;  DF_sct(k) = 1 / Rqf_sct ^ Qac(k)

Qm  = (Y0 - F) * 4 + (q0 - 1)                         the claim quarter's own Qac
PVF = Rqf ^ Qm                                        one-time amounts at the claim are divided by PVF  (PVF_sct with Rqf_sct)
```

Streams CET holds annually (bill impacts, program costs) use `Raf` and whole years instead (entries `BillReducElec`, `PACCost`).
*Example A*: `F` = 2028, so `DF(0)` = 1 / 1.01915^2 = 0.962773 (2028Q3 is 2 quarters after Q1 of 2028) and `PVF` = 1.01915^2 = 1.038672.

### 0.4 Baseline phasing

A measure may have a second baseline: the savings are measured against the 1st baseline until the remaining useful life (`RUL_Yrs`) ends and against the 2nd baseline after it.
With `RQ = 4 * max(RUL_Yrs, 0)` (RUL counted from the claim quarter), a quantity `a` (1st baseline) and `b` (2nd baseline) is phased as

```text
phased(k; a, b) = a                                    if RQ <= 0 or k + 1 <= RQ      single baseline, or quarter k entirely before the RUL ends
                = b                                    if k >= RQ                     quarter k entirely after it
                = (RQ - k) * a + (1 - (RQ - k)) * b    otherwise                      the quarter that contains the end of the RUL: a blend
```

`RUL_Yrs` of 0 or blank is a single baseline: `a` applies for the whole life and every 2nd-baseline field is ignored. *Example B*: `RUL_Yrs` 1.125, so `RQ` = 4.5; quarters 0-3 use
the 1st baseline, quarter 4 is a half-and-half blend (`RQ - k` = 0.5) and quarters 5-9 use the 2nd baseline.

### 0.5 The annual quantities that are priced

Electricity, gas and the electricity embedded in water are each priced as an annual quantity spread over the year. These are output columns of section 1.2, in kWh per year, therms per year and kWh per year embedded in water, for the 1st and 2nd baseline:

```text
poc_shape_scale           = NumUnits * UnitkWh1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
poc_shape_scale_2nd       = NumUnits * UnitkWh2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
poc_annual_net_therms_1st = NumUnits * UnitTherm1stBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm
poc_annual_net_therms_2nd = NumUnits * UnitTherm2ndBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm
poc_water_scale_1st       = NumUnits * UnitkWhIOUWater1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
poc_water_scale_2nd       = NumUnits * UnitkWhIOUWater2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
```

When the run applies market effects to benefits, each is scaled by `(NTG + MEB) / NTG` (`NTG` = `NTGRkWh` for electric and water, `NTGRTherm` for gas; the scaling is 1 when `MEB = 0`
or `NTG = 0`). Then, in quarter `k`, the quantities in force are

```text
Elec(k) = phased( k ;  poc_shape_scale     * (NTGRkWh + MEB) / NTGRkWh ,
                       poc_shape_scale_2nd * (NTGRkWh + MEB) / NTGRkWh )                            annual kWh in force in quarter k
Gas(k)  = phased( k ;  poc_annual_net_therms_1st * (NTGRTherm + MEB) / NTGRTherm ,
                       poc_annual_net_therms_2nd * (NTGRTherm + MEB) / NTGRTherm )                  annual therms
Wat(k)  = phased( k ;  poc_water_scale_1st * (NTGRkWh + MEB) / NTGRkWh ,
                       poc_water_scale_2nd * (NTGRkWh + MEB) / NTGRkWh )                            annual kWh embedded in water
```

*Example A* (no market effects): `Elec(k)` = 34,200 kWh, `Gas(k)` = 1,620 therms, `Wat(k)` = 205.2 kWh in every quarter. *Example B* (`MEB` = 0.05, second baseline): `Elec(0)` = -27,000 x (0.75 + 0.05) / 0.75
= -28,800 kWh, `Elec(9)` = -18,000 kWh, and `Elec(4)` = -23,400 kWh (the blend). Tables 3.2 and 3.4 give all three quantities in every quarter.

### 0.6 The price of one unit in a quarter

**Electric** (`ElecPricingStatus` = `priced: hourly profile`; the only electric route -- TOU end uses are priced from their 8,760s in the library).

```text
shape(i)  = the measure's 8,760-hour load shape: the share of its annual kWh falling in hour i of the year (the shares sum to 1).
            The profile used is reported in `ProfileSource`; it is rotated once so its weekdays line up with the ACC's, which follow one calendar (2018, or 2020 for the 2021/2022 ACCs) in every
            forecast year (CALCULATIONS C4.1, C4.2).
AC(y, i)  = total_avoided_cost of the ACC row (utility of PA, "CZ" + AC_Climate_Zone, cost-test row, year y, hour i) / 1000
Gen(y, i) = AC(y, i) - (transmission + distribution)(y, i) / 1000
y*(k)     = year(k), or the nearest year in the ACC when year(k) lies outside it
U_e(k)    = SUM over the hours i of quarter(k) of  shape(i) * AC(y*(k), i)
U_w(k)    = the same with Gen in place of AC (water energy is valued on the generation part alone)
```

`AC` and `Gen` are in dollars per kWh; `U_e(k)` and `U_w(k)` in dollars per annual kWh delivered in quarter `k`. The cost-test row is `TRC` for the TRC, PAC and RIM columns, `SCT Base` for the societal-Base (`_SB`) columns and `SCT High` for the societal-High (`_SH`) columns. The utility, zone
substitutions and the price-year rule are in CALCULATIONS C3.1. Because `total_avoided_cost` already contains transmission and distribution, this route has no separate kW term.

**Gas.**

```text
gasshape(m) = the 12 monthly shares (sum 1) of the measure's AC_Gas_Impact_Profile
ACg(y, m)   = market_m + td_m + nox + co2 + upstream_methane + air_quality_adder [ + behind_meter_methane ]       $ per therm, month m of year y
y^(k)       = year(k) clamped to the gas ACC's first and last year
U_g(k)      = SUM over the months m of quarter(k) of  gasshape(m) * ACg(y^(k), m)
```

`ACg` is in dollars per therm and `U_g(k)` in dollars per annual therm delivered in quarter `k`. It comes from the gas ACC row for the utility of `PA`, the class of `AC_Gas_Target_Sector` and the cost test (`TRC`, `SCT Base`, `SCT High`); `behind_meter_methane` is included only for a Residential-class
measure whose `AC_Gas_Impact_Profile` contains `FuelSub`. The months of the quarters are Q1 Jan-Mar, Q2 Apr-Jun, Q3 Jul-Sep, Q4 Oct-Dec.

### 0.7 Lifetime values

The three signed lifetime values that sections 1.4 and 1.6 split into benefits and costs :

```text
V_elec  = SUM over k = 0 ... n-1 of  w(k) * DF(k) * Elec(k) * U_e(k)          $ PV, TRC price rows
V_gas   = SUM over k = 0 ... n-1 of  w(k) * DF(k) * Gas(k)  * U_g(k)
V_water = SUM over k = 0 ... n-1 of  w(k) * DF(k) * Wat(k)  * U_w(k)
```

`V_elec_SB`, `V_gas_SB`, `V_water_SB` (and `_SH`) are the same sums with the `SCT Base` (`SCT High`) price rows and `DF_sct(k)`. A value is negative when the measure adds load or use.
Tables 3.2 and 3.4 show each term of these sums for both examples. The entries `ElecBen`, `GasBen` and `WaterEnergyBen` say how each is split.

---------------------------------------------------------------------------------------------------------------------------------

## 1. The measure-level table

One row per measure, in the order of the input file, column groups left to right. The CSV is UTF-8 with a byte-order mark so Excel opens it directly; Parquet is available with the
"Output format" option. The measure-level files delivered for the 2024-versus-2026 comparison have one extra first column, `Job` (the claim year of the consolidated job each row
came from), added by the export script `poc/tou/export_measure_level.py`, not by the package.

### 1.1 `POC_ACC` and the echoed input columns

#### `POC_ACC`

Which ACC version the row was priced against. Text.

**Formula:** the run's ACC label, which names both the ACC (the year the calculator was released) and CET's Avoided Cost Version (the first year it was in effect
for claims or forecasts): `2021 ACC (Avoided Cost Version 2022)`, `2022 ACC (Avoided Cost Version 2024)`, `2024 ACC (Avoided Cost Version 2026)` or
`2026 ACC (Avoided Cost Version 2028)` for a built-in version, `Custom ACC '<label>'` for a new version defined by supplied files. **Inputs:** the "ACC version" setting (INPUT_FIELDS.md section 3).

#### The 74 `Measure.csv` columns (`CEInputID` ... `Comments`)

Every column of the input file, unchanged, as text (a blank stays blank), under ReCET's names: a file that used CET's legacy names (`E3ClimateZone` ...) gets the `AC_...` names here. Each is described in [INPUT_FIELDS.md](INPUT_FIELDS.md#1-measurecsv). **Formula:** copied.

### 1.2 Derived pricing inputs (`poc_*`)

The fields the pricing runs on. A `poc_` field identical to the raw column it restates is left out (for example `poc_num_units`); `poc_ntgr_cost` appears when `NTGRCost` is blank in the
file. Every formula below is complete: it uses only input fields. Each field's value for the two worked examples is in section 3.5.

| Field | Meaning and unit | Formula from the input fields |
|---|---|---|
| `poc_PA` | Avoided-cost territory (text). | `IOU_AC_Territory` |
| `poc_climate_zone` | Climate zone, as text. | `AC_Climate_Zone` |
| `poc_target_sector` | Target sector for shapes and rates (text). | `AC_Elec_Target_Sector` |
| `poc_shape_key` | Electric end-use shape name (text). | `AC_Elec_Impact_Profile` |
| `poc_shape_key_2nd` | The electric end-use shape of the 2nd-baseline years (text). | `AC_Elec_Impact_Profile_2nd_Baseline` (an optional ReCET column, not a CET field); blank = `AC_Elec_Impact_Profile` |
| `poc_shape_key_water` | The electric end-use shape the water energy is spread over, both baselines (text). | `AC_Elec_Impact_Profile_Water` (optional, not a CET field); blank = `AC_Elec_Impact_Profile` |
| `poc_gas_profile_key` | Gas savings profile name (text). | `AC_Gas_Impact_Profile` |
| `poc_gas_sector` | Gas sector (text). | `AC_Gas_Target_Sector` |
| `poc_subset_key` | A label built from zone and sector; not used in pricing (text). | `"CZ" + AC_Climate_Zone + "_" + Sector` |
| `poc_claim_year` | The claim year (year). | first four characters of `ClaimYearQuarter` |
| `poc_claim_quarter` | The claim quarter, 1-4. | last character of `ClaimYearQuarter` |
| `poc_eul_years_exact` | The useful life used in pricing (years). | `max(EUL_Yrs, 0.25)` |
| `poc_eul_years` | The life rounded to whole years; information only (years). | `max(round(EUL_Yrs), 1)` |
| `poc_rul_years_exact` | The remaining useful life used in pricing; 0 = single baseline (years). | `max(RUL_Yrs, 0)`; blank = 0 |
| `poc_NTGRkWh` | The kWh net-to-gross ratio as given (fraction). | `NTGRkWh` |
| `poc_ntgr_kwh` | The kWh net-to-gross ratio with its default applied (fraction). | `NTGRkWh`; blank = 1 |
| `poc_ntgr_kw` | The kW net-to-gross ratio (fraction). | `NTGRkW`; blank = `NTGRkWh`, then 1 |
| `poc_ntgr_therm` | The therm net-to-gross ratio (fraction). | `NTGRTherm`; blank = `NTGRkWh`, then 1 |
| `poc_ntgr_cost` | The cost net-to-gross ratio (fraction). Present only when `NTGRCost` is blank in the file. | `NTGRCost`; blank = `NTGRkWh`, then 1 |
| `poc_ir_kwh`, `poc_rr_kwh` | Installation and realization rate, kWh (fraction); blank = 1. | `InstallationRatekWh`, `RealizationRatekWh` |
| `poc_ir_kw`, `poc_rr_kw` | The same for kW. | `InstallationRatekW`, `RealizationRatekW` |
| `poc_ir_therm`, `poc_rr_therm` | The same for therms. | `InstallationRateTherm`, `RealizationRateTherm` |
| `poc_annual_net_kwh_1st` | Annual net kWh savings, 1st baseline, net of NTG only (kWh per year). | `NumUnits * UnitkWh1stBaseline * NTGRkWh` |
| `poc_annual_net_kwh_2nd` | The same, 2nd baseline. | `NumUnits * UnitkWh2ndBaseline * NTGRkWh` |
| `poc_annual_net_kwh` | The 1st-baseline value. | `poc_annual_net_kwh_1st` |
| `poc_annual_net_kw_1st` | Annual net kW savings, 1st baseline (kW; information only: pricing uses kWh). | `NumUnits * UnitkW1stBaseline * NTGRkW` |
| `poc_annual_net_kw_2nd` | The same, 2nd baseline. | `NumUnits * UnitkW2ndBaseline * NTGRkW` |
| `poc_annual_net_kw` | The 1st-baseline value. | `poc_annual_net_kw_1st` |
| `poc_shape_scale` | **The annual kWh the electric benefit is priced on**, 1st baseline (kWh per year). | `NumUnits * UnitkWh1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh` |
| `poc_shape_scale_2nd` | The same for the 2nd baseline. | `NumUnits * UnitkWh2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh` |
| `poc_annual_net_therms_1st` | **The annual therms the gas benefit is priced on**, 1st baseline (therms per year). | `NumUnits * UnitTherm1stBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm` |
| `poc_annual_net_therms_2nd` | The same, 2nd baseline. | `NumUnits * UnitTherm2ndBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm` |
| `poc_annual_net_therms` | The 1st-baseline value. | `poc_annual_net_therms_1st` |
| `poc_kwh_water_1st`, `poc_kwh_water_2nd` | Per-unit water-embedded kWh, 1st / 2nd baseline (kWh per unit per year); blank = 0. | `UnitkWhIOUWater1stBaseline`, `UnitkWhIOUWater2ndBaseline` |
| `poc_water_net_kwh_1st` | Net water-embedded kWh, 1st baseline, net of NTG only (kWh per year). | `NumUnits * UnitkWhIOUWater1stBaseline * NTGRkWh` |
| `poc_water_net_kwh_2nd` | The same, 2nd baseline. | `NumUnits * UnitkWhIOUWater2ndBaseline * NTGRkWh` |
| `poc_water_scale_1st` | **The annual kWh the water-energy value is priced on**, 1st baseline (kWh per year). | `NumUnits * UnitkWhIOUWater1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh` |
| `poc_water_scale_2nd` | The same, 2nd baseline. | `NumUnits * UnitkWhIOUWater2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh` |
| `poc_is_fuel_sub` | Whether the measure is treated as fuel substitution, so its negative fuel values become supply costs (true / false). | true if `FuelID` is `AllElec-New`, `FuelSub-ToElec` or `FuelSub-ToGas`, or if `MeasImpactType` contains `FuelSub` |
| `poc_fuel_type` | The `FuelID` as text (`nan` when blank). | `FuelID` |
| `poc_acc_version` | The ACC version the measure was priced on (text): its `ACCVersion` (an optional column, not a CET field), or the run's ACC version when blank. See INPUT_FIELDS.md section 1. | `ACCVersion` without a trailing ` ACC`; blank = the run's "ACC version" setting |

The installation and realization rates enter through `poc_shape_scale*`, `poc_annual_net_therms_*` and `poc_water_scale_*` (what is priced), not through `poc_annual_net_kwh_*` (net of NTG only).

**A measure priced on an already-scaled 8,760 profile** (kWh per unit per hour, `ProfileSource = user profile (kWh per unit)`, CALCULATIONS C4.5) is the exception: its
profile carries the kWh, so `poc_shape_scale = NumUnits * NTGRkWh * InstallationRatekWh * RealizationRatekWh` (times `UnitkWh2ndBaseline / UnitkWh1stBaseline` for
`poc_shape_scale_2nd`), and the measure's annual kWh per unit is the profile's sum (written in its own `poc_` column, since it then differs from `UnitkWh1stBaseline`),
which `poc_annual_net_kwh_*` use. A load-shifting-only profile has an annual kWh of 0.
Unlike electricity, the gas quantity includes its rates and its own NTG (`NTGRTherm`).

### 1.3 Pricing status

A benefit of 0 alone cannot tell "no savings" from "could not price"; these columns say which. Each is decided by the first condition that holds, in the order listed.

| Field | Values and the rule that gives each |
|---|---|
| `ElecPricingStatus` | `no electric savings` when `poc_annual_net_kwh_1st`, `poc_annual_net_kwh_2nd`, `poc_annual_net_kw_1st` and `poc_annual_net_kw_2nd` are all 0; `UNPRICED: no hourly profile` when no shape was found (CALCULATIONS C4.1: no supplied profile, no library profile); `PARTLY PRICED: no hourly profile for one baseline's shape` when the measure has a separate 2nd-baseline shape and only one of its two shapes was found (C4.6); `priced: hourly profile` when the value was produced; otherwise `UNPRICED: no value returned` |
| `ProfileSource` | Where the electric shape came from (CALCULATIONS C4.1): `user profile`, `user profile (kWh per unit)` (an already-scaled profile, C4.5), `library`, `library (sector-loose match)`, or `library (user profile has no shape for this zone)`; blank when nothing was priced or there are no electric savings |
| `SystemClimateZone` | For a measure with `AC_Climate_Zone = System`: the real climate zone it was priced in, the profile's System zone (CALCULATIONS C4.4); blank for every other measure and when nothing was priced |
| `SystemZoneSource` | Where `SystemClimateZone` came from: `catalog` (the shipped System-zone catalog), or `calculated (...)` with the reason (`user profile`, `supplied ACC`, `reference data differs from the catalog's`, `profile not in catalog`); `no zone could be priced: System shape and blended prices` when no zone was an option; blank otherwise |
| `GasPricingStatus` | `no gas savings` when `poc_annual_net_therms_1st` and `poc_annual_net_therms_2nd` are both 0; `priced` when the gas value was computed; otherwise `UNPRICED: no gas savings profile match` (`AC_Gas_Impact_Profile` names no known profile) |
| `GasProfileSource` | Where the gas monthly shape came from: `user profile` or `library`; blank when not priced |
| `WaterPricingStatus` | `no water energy` when `poc_kwh_water_1st` and `poc_kwh_water_2nd` are both 0; `priced` when the water value was computed (it needs the measure's electric profile); otherwise `UNPRICED: no hourly profile` |

### 1.4 Benefits and supply costs (TRC price rows, primary discount rate)

All are present values in dollars. `V_elec`, `V_gas` and `V_water` are the signed lifetime values of section 0.7; each column below says how that value is split.

#### `ElecBen`

**Electric avoided-cost benefit.** Unit: $ (PV at the First Year, primary discount rate).

**Formula**

```text
ElecBen = V_elec              if poc_is_fuel_sub is false          (a negative V_elec stays negative)
ElecBen = pos(V_elec)         if poc_is_fuel_sub is true           (the negative part is booked as ElecSupplyCost)
```

**Where** `V_elec` is the signed lifetime electric value (0.7). Written out for the hourly-profile route:

```text
V_elec = SUM over k = 0 ... n-1 of
           w(k) * DF(k)
           * phased( k ;  poc_shape_scale     * (NTGRkWh + MEB) / NTGRkWh ,
                          poc_shape_scale_2nd * (NTGRkWh + MEB) / NTGRkWh )
           * ( SUM over the hours i of quarter(k) of  shape(i) * total_avoided_cost(y*(k), i) / 1000 )

poc_shape_scale     = NumUnits * UnitkWh1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
poc_shape_scale_2nd = NumUnits * UnitkWh2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
```

`n`, `w(k)`, `year(k)`, `quarter(k)`: 0.2. `DF(k)`: 0.3. `phased`: 0.4. `shape(i)`, the ACC row and `y*(k)`: 0.6. `poc_is_fuel_sub`: 1.2.

**Inputs:** `NumUnits`, `UnitkWh1stBaseline`, `UnitkWh2ndBaseline`, `NTGRkWh`, `InstallationRatekWh`, `RealizationRatekWh`, `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`,
`IOU_AC_Territory`, `AC_Climate_Zone`, `AC_Elec_Target_Sector`, `AC_Elec_Impact_Profile`, `FuelID`, `MeasImpactType`. Settings: First Year, ACC version, market effects; the ACC's hourly prices and the 8,760 profile.

- Example A, `ElecBen`: the sum of the ten rows of table 3.2 = **5,841.50**
- Example B, `ElecBen`: `V_elec` is negative (the measure adds load), so pos(V_elec) = **0.00**

#### `ElecSupplyCost`

**Electric cost booked as a supply cost:** the added electric load of a fuel-substitution measure. Unit: $ (PV).

**Formula**

```text
ElecSupplyCost = pos(-V_elec)     if poc_is_fuel_sub is true
ElecSupplyCost = 0                otherwise
```

**Where** `V_elec` is defined under `ElecBen`. **Inputs:** those of `ElecBen`.

- Example A, `ElecSupplyCost`: not a fuel-substitution measure = **0.00**
- Example B, `ElecSupplyCost`: V_elec = -4172.17 (sum of the ten `V_elec` terms of table 3.4), so max(-V_elec, 0) = **4,172.17**

#### `GasBen`

**Gas avoided-cost benefit.** Unit: $ (PV).

**Formula**

```text
GasBen = V_gas                if poc_is_fuel_sub is false
GasBen = pos(V_gas)           if poc_is_fuel_sub is true
```

**Where**

```text
V_gas = SUM over k = 0 ... n-1 of
          w(k) * DF(k)
          * phased( k ;  poc_annual_net_therms_1st * (NTGRTherm + MEB) / NTGRTherm ,
                         poc_annual_net_therms_2nd * (NTGRTherm + MEB) / NTGRTherm )
          * ( SUM over the months m of quarter(k) of  gasshape(m) * ACg(y^(k), m) )

poc_annual_net_therms_1st = NumUnits * UnitTherm1stBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm
poc_annual_net_therms_2nd = NumUnits * UnitTherm2ndBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm
ACg(y, m) = market_m + td_m + nox + co2 + upstream_methane + air_quality_adder  [ + behind_meter_methane ]
```

`gasshape(m)` is the monthly share of the measure's `AC_Gas_Impact_Profile`; `y^(k)` and the gas ACC row are in 0.6. A measure is gas-priced only if it has gas savings and its profile name matches.
**Inputs:** `NumUnits`, `UnitTherm1stBaseline`, `UnitTherm2ndBaseline`, `NTGRTherm`, `InstallationRateTherm`, `RealizationRateTherm`, `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`,
`IOU_AC_Territory`, `AC_Gas_Target_Sector`, `AC_Gas_Impact_Profile`, `FuelID`, `MeasImpactType`. Settings: First Year, ACC version, market effects; the gas ACC and the gas profile.

- Example A, `GasBen`: the sum of the ten rows of table 3.2 = **3,454.03**
- Example B, `GasBen`: the sum of the ten rows of table 3.4 (`AC_Gas_Impact_Profile` is `Winter Only`, so only quarters containing Jan-Mar and Oct-Dec carry value) = **6,990.56**

#### `GasSupplyCost`

**Gas cost booked as a supply cost:** the added gas use of a fuel-substitution measure. Unit: $ (PV).

**Formula**

```text
GasSupplyCost = pos(-V_gas)       if poc_is_fuel_sub is true
GasSupplyCost = 0                 otherwise
```

**Where** `V_gas` is defined under `GasBen`. **Inputs:** those of `GasBen`.

- Example A, `GasSupplyCost`: not a fuel-substitution measure = **0.00**
- Example B, `GasSupplyCost`: V_gas is positive (the measure saves gas), so max(-V_gas, 0) = **0.00**

#### `WaterEnergyBen`

**Value of the electricity embedded in the water the measure saves.** Unit: $ (PV). It is priced on the measure's own electric profile, on the generation part of the price only.

**Formula**

```text
WaterEnergyBen = V_water     if UnitkWhIOUWater1stBaseline > 0
WaterEnergyBen = 0           otherwise
```

**Where**

```text
V_water = SUM over k = 0 ... n-1 of
            w(k) * DF(k)
            * phased( k ;  poc_water_scale_1st * (NTGRkWh + MEB) / NTGRkWh ,
                           poc_water_scale_2nd * (NTGRkWh + MEB) / NTGRkWh )
            * ( SUM over the hours i of quarter(k) of  shape(i) * Gen(y*(k), i) )

poc_water_scale_1st = NumUnits * UnitkWhIOUWater1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
poc_water_scale_2nd = NumUnits * UnitkWhIOUWater2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
Gen(y, i) = ( total_avoided_cost - transmission - distribution )(y, i) / 1000
```

The sign of the *first-baseline* water kWh decides the column, not the sign of the value. A water measure with no electric profile cannot be priced (`WaterPricingStatus` = `UNPRICED ...`). **Inputs:** `NumUnits`, `UnitkWhIOUWater1stBaseline`,
`UnitkWhIOUWater2ndBaseline`, `NTGRkWh`, `InstallationRatekWh`, `RealizationRatekWh`, `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`, and the electric-profile fields of `ElecBen`.

- Example A, `WaterEnergyBen`: `UnitkWhIOUWater1stBaseline` = 3 > 0, so the sum of the ten `V_water` terms of table 3.2 = **30.67**
- Example B, `WaterEnergyBen`: `UnitkWhIOUWater1stBaseline` = -6 < 0 = **0.00**

#### `WaterEnergyCost`

**Value of the electricity embedded in water the measure uses.** Unit: $ (PV).

**Formula**

```text
WaterEnergyCost = -V_water     if UnitkWhIOUWater1stBaseline < 0
WaterEnergyCost = 0            otherwise
```

**Where** `V_water` is defined under `WaterEnergyBen`. **Inputs:** those of `WaterEnergyBen`.

- Example A, `WaterEnergyCost`: `UnitkWhIOUWater1stBaseline` = 3 (not below 0) = **0.00**
- Example B, `WaterEnergyCost`: `UnitkWhIOUWater1stBaseline` = -6, and the sum of the ten `V_water` terms of table 3.4 is -5.63, so -V_water = **5.63**

#### `OtherBen`, `OtherCost`, `TaxCredits`

**Other benefits** (gas infrastructure, refrigerant, miscellaneous), **other costs** (refrigerant, miscellaneous) and **tax credits.** Unit: $ (**undiscounted**: they are one-time amounts of the
claim). `TaxCredits` enters only the numerator of `TRCRatio`; it is not in `TotalSystemBenefit` or any cost.

**Formula**

```text
OtherBen   = NumUnits * (NTGRkWh + MEB) * (UnitGasInfraBens + UnitRefrigBens + UnitMiscBens)
OtherCost  = NumUnits * (NTGRkWh + MEC) * (UnitRefrigCosts + UnitMiscCosts)
TaxCredits = NumUnits * (NTGRkWh + MEB) * UnitTaxCredits
```

**Inputs:** `NumUnits`, `NTGRkWh`, `UnitGasInfraBens`, `UnitRefrigBens`, `UnitMiscBens`, `UnitRefrigCosts`, `UnitMiscCosts`, `UnitTaxCredits` (blank = 0); the market-effects settings.

- Example A, `OtherBen`: 100 * (0.8 + 0) * (0 + 0 + 2) = **160.00**
- Example A, `OtherCost`: 100 * (0.8 + 0) * (1 + 0) = **80.00**
- Example A, `TaxCredits`: 100 * (0.8 + 0) * 5 = **400.00**
- Example B, `OtherBen`: 10 * (0.75 + 0.05) * (20 + 8 + 12) = **320.00**
- Example B, `OtherCost`: 10 * (0.75 + 0.05) * (10 + 5) = **120.00**
- Example B, `TaxCredits`: 10 * (0.75 + 0.05) * 100 = **800.00**

#### `OtherBenGross`, `OtherCostGross`, `TaxCreditsGross`

The three above **without net-to-gross and without market effects.** Unit: $ (undiscounted).

**Formula**

```text
OtherBenGross   = NumUnits * (UnitGasInfraBens + UnitRefrigBens + UnitMiscBens)
OtherCostGross  = NumUnits * (UnitRefrigCosts + UnitMiscCosts)
TaxCreditsGross = NumUnits * UnitTaxCredits
```

**Inputs:** `NumUnits` and the unit fields of the entry above.

- Example A, `OtherBenGross`: 100 * (0 + 0 + 2) = **200.00**
- Example B, `OtherBenGross`: 10 * (20 + 8 + 12) = **400.00**
- Example A, `OtherCostGross`: 100 * (1 + 0) = **100.00**
- Example B, `OtherCostGross`: 10 * (10 + 5) = **150.00**
- Example A, `TaxCreditsGross`: 100 * 5 = **500.00**
- Example B, `TaxCreditsGross`: 10 * 100 = **1,000.00**

### 1.5 Participant costs and bill impacts (primary rate)

One-time cash flows at the claim quarter, discounted once with `PVF` (0.3); the primary discount rate here, the societal one in 1.6.

#### `RebatesandIncents`

**Incentives, direct-install costs and rebates** paid to or for participants, in nominal dollars (not discounted). Part of `TotalExpenditures`. Unit: $ (nominal).

**Formula**

```text
RebatesandIncents = IncentsDI + Rebates          IncentsDI, Rebates: see RebatesandIncentsPV
```

Matches CET's own `RebatesandIncents` column to the cent on CET jobs 121219, 121841 and 121842. **Inputs:** `NumUnits`, `UnitIncentiveToOthers`, `UnitDirectInstallLab`,
`UnitDirectInstallMat`, `UnitEndUserRebate`.

- Example A, `RebatesandIncents`: 4000 + 2000 = **6,000.00** (100 * (30 + 5 + 5) and 100 * 20)
- Example B, `RebatesandIncents`: 9500 + 4000 = **13,500.00** (10 * (800 + 100 + 50) and 10 * 400)

#### `RebatesandIncentsPV`

**Incentives, direct-install costs and rebates** paid to or for participants, present-valued at the claim quarter. Unit: $ (PV).

**Formula**

```text
RebatesandIncentsPV = ( IncentsDI + Rebates ) / PVF

IncentsDI = NumUnits * ( UnitIncentiveToOthers + UnitDirectInstallLab + UnitDirectInstallMat )
Rebates   = NumUnits * UnitEndUserRebate
PVF       = Rqf ^ Qm          Qm = (Y0 - F) * 4 + (q0 - 1)          Rqf = 1 + DiscountRateQtr(PA)          (section 0.3)
```

**Inputs:** `NumUnits`, `UnitIncentiveToOthers`, `UnitDirectInstallLab`, `UnitDirectInstallMat`, `UnitEndUserRebate`, `ClaimYearQuarter`, `IOU_AC_Territory`; First Year, ACC version (E3 discount rates).

- Example A, `RebatesandIncentsPV`: (4000 + 2000) / 1.01915^2 = **5,776.64**
- Example B, `RebatesandIncentsPV`: (9500 + 4000) / 1.01915^1 = **13,246.33**

#### `GrossParticipantCostPV`

**Participant cost before net-to-gross:** the measure cost less the incentives and rebates. Unit: $ (PV).

**Formula**

```text
GrossParticipantCostPV = GrossMeasCostPV - ( IncentsDI + Rebates ) / PVF

GrossMeasCostPV = NumUnits * ( UnitMeaCost1stBaseline - PVsub ) / PVF
PVsub = 0                                                                                    if RUL_Yrs = 0 (single baseline)
PVsub = ( UnitMeaCost1stBaseline - UnitMeaCost2ndBaseline )
        * ( (1 + MeasInflation / 4) / Rqf ) ^ ( 4 * RUL_Yrs )                                if RUL_Yrs > 0 (early replacement)
```

For an early replacement (`RUL_Yrs` > 0) only the *incremental* cost counts: the normal-replacement cost of the 2nd baseline, brought forward to the claim at the inflation-adjusted quarterly rate,
is taken off. `IncentsDI`, `Rebates`, `PVF`: see `RebatesandIncentsPV`. **Inputs:** those of `RebatesandIncentsPV`, plus `UnitMeaCost1stBaseline`, `UnitMeaCost2ndBaseline`, `MeasInflation`, `RUL_Yrs`.

- Example A, `GrossParticipantCostPV`: 100 * 200 / 1.038667 - (4000 + 2000) / 1.038667 = **13,478.82** (`RUL_Yrs` = 0, so `PVsub` = 0)
- Example B, `GrossParticipantCostPV`: 10 * (9000 - 2817.0661) / 1.01915 - (9500 + 4000) / 1.01915 = **47,421.22** (`RUL_Yrs` > 0, so `PVsub` is needed; it is worked in the next line)
- Check, `PVsub` of example B: (9000 - 6000) * ((1 + 0.02 / 4) / 1.01915)^4.5 = **2,817.0661**

#### `NetParticipantCostPV`

**Participant cost after net-to-gross,** plus the excess-incentive term; **without** the market-effects term (that is `NetParticipantCostAdjustedPV`), as CET exports it. Unit: $ (PV).

**Formula**

```text
NetParticipantCostPV = NTGRCost * ( GrossParticipantCostPV + ExcessIncentivesPV )

ExcessIncentivesPV = pos( IncentsDI / PVF - GrossMeasCostPV )
```

`ExcessIncentivesPV` is the incentives paid beyond the measure cost (end-user rebates are not part of this test). `NTGRCost` blank takes `NTGRkWh`, then 1 (`poc_ntgr_cost`). `GrossParticipantCostPV`, `GrossMeasCostPV`, `IncentsDI`, `PVF`: see the two entries above.
Matches CET's own `NetParticipantCostPV` column to the cent with market effects on and off (CET jobs 121842 / 121841).
**Inputs:** those of `GrossParticipantCostPV`, plus `NTGRCost` (or `NTGRkWh`).

- Example A, `NetParticipantCostPV`: 0.9 * (13478.8183 + 0.0000) = **12,130.94**
- Example B, `NetParticipantCostPV`: 0.8 * (47421.2222 + 0.0000) = **37,936.98**
  (`ExcessIncentivesPV` = pos(9500 / 1.01915 - 60667.55) = 0)

#### `MarkEffectPlusExcessIncPV`

**The market-effects term on the participant cost** (CET's `MECost` term). Unit: $ (PV).

**Formula**

```text
MarkEffectPlusExcessIncPV = MEC * ( GrossMeasCostPV + ExcessIncentivesPV )
```

`MEC` is 0.05 with "Apply 5% market effects (benefits and costs)", else 0 (so this column is then 0). `GrossMeasCostPV`, `ExcessIncentivesPV`: see the two entries above.
**Inputs:** those of `GrossParticipantCostPV`; the market-effects setting.

- Example A, `MarkEffectPlusExcessIncPV`: 0 * (19255.4547 + 0.0000) = **0.00**
- Example B, `MarkEffectPlusExcessIncPV`: 0.05 * (60667.5550 + 0.0000) = **3,033.38**

#### `NetParticipantCostAdjustedPV`

**The participant cost the TRC cost uses:** `NetParticipantCostPV` plus the market-effects term (CET's column of the same name; it is what CET's own TRC calculation adds). Unit: $ (PV).

**Formula**

```text
NetParticipantCostAdjustedPV = NetParticipantCostPV + MarkEffectPlusExcessIncPV
```

**Inputs:** those of the two columns it adds.

- Example A, `NetParticipantCostAdjustedPV`: 12130.9364 + 0.0000 = **12,130.94**
- Example B, `NetParticipantCostAdjustedPV`: 37936.9778 + 3033.3778 = **40,970.36**

#### `IncentiveToOthers`, `DILaborCost`, `DIMaterialCost`, `EndUserRebate`

**The incentive, direct-install and rebate amounts,** in nominal dollars (not discounted): the four parts of `RebatesandIncents`. Unit: $ (nominal).

**Formula**

```text
IncentiveToOthers = NumUnits * UnitIncentiveToOthers
DILaborCost       = NumUnits * UnitDirectInstallLab
DIMaterialCost    = NumUnits * UnitDirectInstallMat
EndUserRebate     = NumUnits * UnitEndUserRebate
```

CET's columns of the same names (CalcCost); they match CET to the cent on CET jobs 121219-121222, 121842 and 121841. **Inputs:** `NumUnits` and the four unit fields.

- Example A, `IncentiveToOthers`: 100 * 30 = **3,000.00**
- Example A, `EndUserRebate`: 100 * 20 = **2,000.00**
- Example B, `DILaborCost`: 10 * 100 = **1,000.00**
- Example B, `DIMaterialCost`: 10 * 50 = **500.00**

#### `GrossMeasureCost`, `GrossMeasCostPV`

**The measure cost the cost tests count,** nominal and present-valued at the claim quarter: the full cost of a single-baseline measure; for an early replacement (`RUL_Yrs` > 0), the
normal-replacement cost. Unit: $ (nominal; PV).

**Formula**

```text
GrossMeasureCost = NumUnits * UnitMeaCost1stBaseline                    if RUL_Yrs = 0
                 = NumUnits * UnitMeaCost2ndBaseline                    if RUL_Yrs > 0
GrossMeasCostPV  = NumUnits * ( UnitMeaCost1stBaseline - PVsub ) / PVF
```

`PVsub` and `PVF`: see `GrossParticipantCostPV` and `RebatesandIncentsPV`. For an early replacement the two are built differently, as in CET: the nominal column is the 2nd-baseline cost, the PV
column the 1st-baseline cost less the 1st-to-2nd difference brought forward at the inflation-adjusted rate. Both match CET to the cent on the jobs above. **Inputs:** `NumUnits`,
`UnitMeaCost1stBaseline`, `UnitMeaCost2ndBaseline`, `MeasInflation`, `RUL_Yrs`, `ClaimYearQuarter`, `IOU_AC_Territory`; First Year, ACC version (discount rates).

- Example A, `GrossMeasureCost`: 100 * 200 = **20,000.00** (`RUL_Yrs` = 0)
- Example A, `GrossMeasCostPV`: 100 * 200 / 1.01915^2 = **19,255.45**
- Example B, `GrossMeasureCost`: 10 * 6000 = **60,000.00** (`RUL_Yrs` > 0: the 2nd-baseline cost)
- Example B, `GrossMeasCostPV`: 10 * (9000 - 2817.0661) / 1.01915 = **60,667.55** (`PVsub` as worked under `GrossParticipantCostPV`)

#### `ExcessIncentives`, `ExcessIncentivesPV`

**Incentives paid beyond the measure cost** (end-user rebates are not part of this test), nominal and PV. Unit: $ (nominal; PV).

**Formula**

```text
ExcessIncentives   = pos( IncentsDI - GrossMeasureCost )
ExcessIncentivesPV = pos( IncentsDI / PVF - GrossMeasCostPV )
```

`IncentsDI`, `PVF`: see `RebatesandIncentsPV`. **Inputs:** those of `RebatesandIncentsPV` and `GrossMeasureCost`.

- Example A, `ExcessIncentives`: max(4000 - 20000, 0) = **0.00**
- Example B, `ExcessIncentivesPV`: max(9500 / 1.01915 - 60667.5550, 0) = **0.00**

#### `MarkEffectPlusExcessInc`

**The market-effects term on the participant cost, nominal.** Unit: $.

**Formula**

```text
MarkEffectPlusExcessInc = MEC * ( GrossMeasureCost + ExcessIncentivesPV )
```

CET adds the *present-valued* excess incentives to the *nominal* measure cost here (CalcCost's `GrossMeasureCostAdjusted` step); ReCET does the same, and the column matches CET's on job
121841 (5% market effects). It is 0 without market effects. **Inputs:** those of `GrossMeasureCost` and `ExcessIncentivesPV`; the market-effects setting.

- Example A, `MarkEffectPlusExcessInc`: 0 * (20000 + 0) = **0.00**
- Example B, `MarkEffectPlusExcessInc`: 0.05 * (60000 + 0) = **3,000.00**

#### `GrossParticipantCost`, `GrossParticipantCostAdjusted`, `GrossParticipantCostAdjustedPV`

**The participant cost before net-to-gross,** nominal, and with the excess-incentive and market-effects terms added (nominal and PV). Unit: $ (nominal; PV).

**Formula**

```text
GrossParticipantCost           = GrossMeasureCost - ( IncentsDI + Rebates )
GrossParticipantCostAdjusted   = GrossParticipantCost + ExcessIncentives + MarkEffectPlusExcessInc
GrossParticipantCostAdjustedPV = GrossParticipantCostPV + ExcessIncentivesPV + MarkEffectPlusExcessIncPV
```

**Inputs:** those of the columns they add.

- Example A, `GrossParticipantCost`: 20000 - (4000 + 2000) = **14,000.00**
- Example A, `GrossParticipantCostAdjustedPV`: 13478.8183 + 0 + 0 = **13,478.82**
- Example B, `GrossParticipantCost`: 60000 - (9500 + 4000) = **46,500.00**
- Example B, `GrossParticipantCostAdjusted`: 46500 + 0 + 3000 = **49,500.00**
- Example B, `GrossParticipantCostAdjustedPV`: 47421.2222 + 0 + 3033.3777 = **50,454.60**

#### `NetParticipantCost`, `NetParticipantCostAdjusted`

**The participant cost after net-to-gross, nominal,** without and with the market-effects term (the nominal counterparts of `NetParticipantCostPV` and `NetParticipantCostAdjustedPV`). Unit: $.

**Formula**

```text
NetParticipantCost         = NTGRCost * ( GrossParticipantCost + ExcessIncentives )
NetParticipantCostAdjusted = NetParticipantCost + MarkEffectPlusExcessInc
```

**Inputs:** those of `GrossParticipantCost`, plus `NTGRCost` (or `NTGRkWh`).

- Example A, `NetParticipantCost`: 0.9 * (14000 + 0) = **12,600.00**
- Example B, `NetParticipantCost`: 0.8 * (46500 + 0) = **37,200.00**
- Example B, `NetParticipantCostAdjusted`: 37200 + 3000 = **40,200.00**

#### `BillReducElec`, `BillIncrElec`, `BillReducGas`, `BillIncrGas`

**The customer's bill change,** the ratepayer cost in the RIM test: the present value of the bill reduction (`...Reduc...`) or, for a fuel-substitution measure that adds load, the bill increase (`...Incr...`).
Unit: $ (PV, **annual** discounting). Unlike section 1.4 this is annual and uses the E3 retail rate schedules; like the benefits it uses net-to-gross plus market effects
(`NTG + MEB`). The measure-level `RateScheduleElec` and `RateScheduleGas` columns are not read.

**Formula**

```text
PV_elec = PV( fe * poc_shape_scale ,            fe * poc_shape_scale_2nd ,       RateE )        fe = ( NTGRkWh + MEB ) / NTGRkWh
PV_gas  = PV( ft * poc_annual_net_therms_1st ,  ft * poc_annual_net_therms_2nd , RateG )        ft = ( NTGRTherm + MEB ) / NTGRTherm
   (fe, ft = 1 when MEB = 0 or the NTG is 0: the poc_ quantities already carry NTG, this swaps it for NTG + MEB, as CET's bill calculation does)

BillReducElec = PV_elec      BillIncrElec = 0               if poc_is_fuel_sub is false
BillReducElec = pos(PV_elec) BillIncrElec = pos(-PV_elec)   if poc_is_fuel_sub is true
BillReducGas, BillIncrGas: the same with PV_gas
```

**Where** `PV(s1, s2, rate)` is the annual stream, with `E = EUL_Yrs`, `R = RUL_Yrs`, `full = floor(E)`, `rem = E - full`, the number of billed years `ny = full + 1` if `rem > 0.000000001` else `full`,
`yr1 = Y0 - BaseYear` (E3 settings `BaseYear`, 2024), `Raf = 1 + DiscountRateAnnual(PA)`:

```text
floor_rul = floor(R) if R > 0 else full
b         = R - floor(R) if R > 0 else 1              the share of the boundary year on the 1st baseline

PV(s1, s2, rate) =   SUM over j = 0 ... min(floor_rul, ny) - 1 of        s1 * rate(Y0 + j) / Raf ^ j
                   + SUM over j = floor_rul ... ny - 1 of                wt(j) * v(j) * rate(Y0 + j) / Raf ^ (j + yr1)
    wt(j) = rem  if j = ny - 1 and rem > 0.000000001, else 1
    v(j)  = b * s1 + (1 - b) * s2    if j = floor_rul,  else s2
```

`RateE` is the retail electric rate ($ per kWh, by calendar year) of the schedule that (`PA`, `AC_Elec_Target_Sector`) maps to and `RateG` the gas rate ($ per therm) for (`PA`, `AC_Gas_Target_Sector`), both from CET's E3
tables. The second sum's exponent `j + yr1` (years since `BaseYear`, not since the claim) reproduces what CET's own calculation does for the boundary year and every 2nd-baseline year, so it is kept.
A measure whose sector has no schedule mapping, or a year outside the schedule's range, gets no bill impact for that fuel.
**Inputs:** the fields of `poc_shape_scale` / `poc_annual_net_therms_*` (0.5), `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`, `IOU_AC_Territory`, `AC_Elec_Target_Sector`, `AC_Gas_Target_Sector`, `FuelID`, `MeasImpactType`; ACC version (E3 rate tables and discount rate); the market-effects setting.
Scaling by market effects matches CET exactly (every `BillReduc` of CET job 121841 is (NTG + 0.05) / NTG times job 121842's).

Example A (single baseline, `EUL_Yrs` 2.4: `ny` = 3, `floor_rul` = 2, `yr1` = 4, so the last, partial year has `wt` = 0.4 and is discounted by `Raf^6`). `RateE` for 2028 / 2029 / 2030 is
0.406249518 / 0.418437003 / 0.430990113 $ per kWh and `RateG` is 2.815469667 / 2.899933757 / 2.986931770 $ per therm:

- Example A, `BillReducElec`: 34200 * 0.406249518 + 34200 * 0.418437003 / 1.0766^1 + 0.4 * 34200 * 0.430990113 / 1.0766^6 = **30,972.49**
- Example A, `BillIncrElec`: not a fuel-substitution measure, so 0 = **0.00**
- Example A, `BillReducGas`: 1620 * 2.815469667 + 1620 * 2.899933757 / 1.0766^1 + 0.4 * 1620 * 2.986931770 / 1.0766^6 = **10,167.71**
- Example A, `BillIncrGas`: not a fuel-substitution measure, so 0 = **0.00**

Example B (`EUL_Yrs` 2.5, `RUL_Yrs` 1.125: `ny` = 3, `floor_rul` = 1, `b` = 0.125 so the boundary year 2029 is 12.5% 1st baseline and 87.5% 2nd baseline, `wt` = 0.5 for 2030, `yr1` = 4). `RateE` for 2028 / 2029 / 2030
is 0.426280328 / 0.439068738 / 0.452240800 and `RateG` is 1.485816933 / 1.530391441 / 1.576303184. Example B has 5% market effects, so the streams are scaled by `fe` and `ft`. The
measure adds electric load, so `PV_elec` is negative:

- Check, electric streams of example B: -27000 * (0.75 + 0.05) / 0.75 = **-28,800** (1st baseline; the 2nd, -16875, becomes -18,000)
- Check, gas streams of example B: 4845 * (0.85 + 0.05) / 0.85 = **5,130** (1st baseline; the 2nd, 3230, becomes 3,420)
- Check, `PV_elec` of example B: -28800 * 0.426280328 + (0.125 * -28800 + 0.875 * -18000) * 0.439068738 / 1.0766^5 + 0.5 * -18000 * 0.452240800 / 1.0766^6 = **-20,764.86**
- Example B, `BillReducElec`: max(0, PV_elec) with PV_elec = -20764.86, so 0 = **0.00**
- Example B, `BillIncrElec`: max(0, -PV_elec) = **20,764.86**
- Check, `PV_gas` of example B: 5130 * 1.485816933 + (0.125 * 5130 + 0.875 * 3420) * 1.530391441 / 1.0766^5 + 0.5 * 3420 * 1.576303184 / 1.0766^6 = **13,198.20**
- Example B, `BillReducGas`: max(0, PV_gas) with PV_gas = 13198.20 = **13,198.20**
- Example B, `BillIncrGas`: max(0, -PV_gas), and PV_gas is positive, so 0 = **0.00**

#### `BillReducElecGross`, `BillIncrElecGross`, `BillReducGasGross`, `BillIncrGasGross`

**The participant's own bill change** for the Participant Cost Test (1.10): the bill impacts above on **gross** savings, without net-to-gross and without market effects
(free riders are participants too). Unit: $ (PV, annual discounting). The CET has no such columns.

**Formula**

```text
PV_elec_gross = PV( NumUnits * UnitkWh1stBaseline * IR * RR ,  NumUnits * UnitkWh2ndBaseline * IR * RR ,  RateE )      IR, RR: InstallationRatekWh, RealizationRatekWh (blank = 1)
PV_gas_gross  = PV( NumUnits * UnitTherm1stBaseline * IRt * RRt ,  NumUnits * UnitTherm2ndBaseline * IRt * RRt ,  RateG )   IRt, RRt: the therm rates (blank = 1)
the Reduc / Incr split and PV(s1, s2, rate): exactly as for BillReducElec ... BillIncrGas above
```

**Inputs:** as for `BillReducElec` ... `BillIncrGas`, without the NTG columns and the market-effects setting.

The gross streams are the net streams of the entry above divided by `NTG + MEB` (example A: 0.8 electric, 0.9 gas; example B: 0.75 + 0.05 and 0.85 + 0.05):

- Example A, `BillReducElecGross`: 42750 * 0.406249518 + 42750 * 0.418437003 / 1.0766^1 + 0.4 * 42750 * 0.430990113 / 1.0766^6 = **38,715.61**
- Example A, `BillIncrElecGross`: not a fuel-substitution measure, so 0 = **0.00**
- Example A, `BillReducGasGross`: 1800 * 2.815469667 + 1800 * 2.899933757 / 1.0766^1 + 0.4 * 1800 * 2.986931770 / 1.0766^6 = **11,297.45**
- Example A, `BillIncrGasGross`: not a fuel-substitution measure, so 0 = **0.00**
- Check, `PV_elec_gross` of example B: -36000 * 0.426280328 + (0.125 * -36000 + 0.875 * -22500) * 0.439068738 / 1.0766^5 + 0.5 * -22500 * 0.452240800 / 1.0766^6 = **-25,956.07**
- Example B, `BillReducElecGross`: max(0, PV_elec_gross) with PV_elec_gross = -25956.07, so 0 = **0.00**
- Example B, `BillIncrElecGross`: max(0, -PV_elec_gross) = **25,956.07**
- Example B, `BillReducGasGross`: 5700 * 1.485816933 + (0.125 * 5700 + 0.875 * 3800) * 1.530391441 / 1.0766^5 + 0.5 * 3800 * 1.576303184 / 1.0766^6 = **14,664.66**
- Example B, `BillIncrGasGross`: max(0, -PV_gas_gross), and PV_gas_gross is positive, so 0 = **0.00**

### 1.6 Societal (SB = Base, SH = High) and societal-rate columns

Blank when "Include societal cost tests" is off. These are the columns of 1.4 and 1.5 repeated for the Societal Cost tests. Two things change, and only these two: the **price rows** are
`SCT Base` (columns ending `_SB`) or `SCT High` (`_SH`) instead of `TRC` (for gas, the `SCT` rows with `scc` = `Base` or `High`), and the **discount factor** is the societal one,
`DF_sct(k) = 1 / Rqf_sct ^ Qac(k)` with `Rqf_sct = 1 + DiscountRateQtr_SCT(PA)` (0.3). Quantities, phasing, market effects and the benefit / supply-cost split are exactly as in 1.4.

#### `ElecBen_SB`, `ElecBen_SH`, `ElecSupplyCost_SB`, `ElecSupplyCost_SH`

**Electric benefit and supply cost, societal Base / High.** Unit: $ (PV at the societal rate).

**Formula**

```text
ElecBen_SB        = V_elec_SB           if poc_is_fuel_sub is false        ElecBen_SB        = pos(V_elec_SB)     if it is true
ElecSupplyCost_SB = 0                   if poc_is_fuel_sub is false        ElecSupplyCost_SB = pos(-V_elec_SB)    if it is true
(ElecBen_SH, ElecSupplyCost_SH: the same with V_elec_SH)

V_elec_SB = SUM over k = 0 ... n-1 of
              w(k) * DF_sct(k)
              * phased( k ;  poc_shape_scale * (NTGRkWh + MEB) / NTGRkWh ,  poc_shape_scale_2nd * (NTGRkWh + MEB) / NTGRkWh )
              * ( SUM over the hours i of quarter(k) of  shape(i) * total_avoided_cost[SCT Base](y*(k), i) / 1000 )
V_elec_SH: the same with the SCT High row
```

For TOU end uses these differ from CET's by design: CET's TOU societal values keep the cap-and-trade carbon component alongside the social cost of carbon (counting carbon twice)
and carry its TOU T&D error; ReCET prices them on the ACC's own SCT rows like every other end use (CALCULATIONS.md C17, deliberate difference 8).
**Inputs:** those of `ElecBen`; the ACC's `SCT Base` / `SCT High` rows; `DiscountRateQtr_SCT`.

- Example A, `ElecBen_SB`: the sum of the ten `V_elec_SB` terms of table 3.6 = **6,688.34**
- Example A, `ElecBen_SH`: the sum of the ten `V_elec_SH` terms of table 3.6 = **7,296.37**
- Example B, `ElecSupplyCost_SB`: -(the sum of the ten `V_elec_SB` terms of table 3.7), the measure adding load = **4,734.01**
- Example B, `ElecSupplyCost_SH`: -(the sum of the ten `V_elec_SH` terms of table 3.7) = **5,164.37**
- Example A, `ElecSupplyCost_SB`: not a fuel-substitution measure = **0.00**
- Example A, `ElecSupplyCost_SH`: not a fuel-substitution measure = **0.00**
- Example B, `ElecBen_SB`: V_elec_SB is negative, so pos(V_elec_SB) = **0.00**
- Example B, `ElecBen_SH`: V_elec_SH is negative, so pos(V_elec_SH) = **0.00**

#### `GasBen_SB`, `GasBen_SH`, `GasSupplyCost_SB`, `GasSupplyCost_SH`

**Gas benefit and supply cost, societal Base / High.** Unit: $ (PV at the societal rate).

**Formula**

```text
GasBen_SB        = V_gas_SB           if poc_is_fuel_sub is false        GasBen_SB        = pos(V_gas_SB)     if it is true
GasSupplyCost_SB = 0                  if poc_is_fuel_sub is false        GasSupplyCost_SB = pos(-V_gas_SB)    if it is true
(GasBen_SH, GasSupplyCost_SH: the same with V_gas_SH)

V_gas_SB = SUM over k = 0 ... n-1 of
             w(k) * DF_sct(k)
             * phased( k ;  poc_annual_net_therms_1st * (NTGRTherm + MEB) / NTGRTherm ,
                            poc_annual_net_therms_2nd * (NTGRTherm + MEB) / NTGRTherm )
             * ( SUM over the months m of quarter(k) of  gasshape(m) * ACg[SCT, scc = Base](y^(k), m) )
V_gas_SH: the same with the SCT rows with scc = High
```

**Inputs:** those of `GasBen`; the gas ACC's `SCT` rows; `DiscountRateQtr_SCT`.

- Example A, `GasBen_SB`: the sum of the ten `V_gas_SB` terms of table 3.6 = **4,314.29**
- Example A, `GasBen_SH`: the sum of the ten `V_gas_SH` terms of table 3.6 = **5,033.34**
- Example B, `GasBen_SB`: the sum of the ten `V_gas_SB` terms of table 3.7 = **8,827.11**
- Example B, `GasBen_SH`: the sum of the ten `V_gas_SH` terms of table 3.7 = **10,432.03**
- Example A, `GasSupplyCost_SB`: not a fuel-substitution measure = **0.00**
- Example B, `GasSupplyCost_SB`: V_gas_SB is positive, so pos(-V_gas_SB) = **0.00**

#### `WaterEnergyBen_SB`, `WaterEnergyBen_SH`, `WaterEnergyCost_SB`, `WaterEnergyCost_SH`

**Water-embedded-energy benefit and cost, societal Base / High.** Unit: $ (PV at the societal rate).

**Formula**

```text
WaterEnergyBen_SB  = V_water_SB   if UnitkWhIOUWater1stBaseline > 0, else 0
WaterEnergyCost_SB = -V_water_SB  if UnitkWhIOUWater1stBaseline < 0, else 0                (_SH the same with V_water_SH)

V_water_SB = SUM over k = 0 ... n-1 of
               w(k) * DF_sct(k)
               * phased( k ; poc_water_scale_1st * (NTGRkWh + MEB) / NTGRkWh , poc_water_scale_2nd * (NTGRkWh + MEB) / NTGRkWh )
               * ( SUM over the hours i of quarter(k) of  shape(i) * Gen[SCT Base](y*(k), i) )
Gen[SCT Base](y, i) = ( total_avoided_cost - transmission - distribution )[SCT Base](y, i) / 1000
```

**Inputs:** those of `WaterEnergyBen`; the ACC's `SCT` rows; `DiscountRateQtr_SCT`.

- Example A, `WaterEnergyBen_SB`: the sum of the ten `V_water_SB` terms of table 3.6 = **35.57**
- Example A, `WaterEnergyBen_SH`: the sum of the ten `V_water_SH` terms of table 3.6 = **39.22**
- Example B, `WaterEnergyCost_SB`: -(the sum of the ten `V_water_SB` terms of table 3.7) = **6.47**
- Example B, `WaterEnergyCost_SH`: -(the sum of the ten `V_water_SH` terms of table 3.7) = **7.13**
- Example A, `WaterEnergyCost_SB`: `UnitkWhIOUWater1stBaseline` = 3, not below 0 = **0.00**
- Example B, `WaterEnergyBen_SB`: `UnitkWhIOUWater1stBaseline` = -6, not above 0 = **0.00**

#### `RebatesandIncentsPV_sct`, `GrossParticipantCostPV_sct`, `NetParticipantCostPV_sct`, `NetParticipantCostAdjustedPV_sct`

**The participant values of 1.5 discounted at the societal rate.** Unit: $ (PV at the societal rate).

**Formula:** exactly the formulas of `RebatesandIncentsPV`, `GrossParticipantCostPV`, `NetParticipantCostPV` and `NetParticipantCostAdjustedPV` with `Rqf_sct` in place of `Rqf` everywhere (in `PVF` and in `PVsub`):

```text
RebatesandIncentsPV_sct    = ( IncentsDI + Rebates ) / PVF_sct                                       PVF_sct = Rqf_sct ^ Qm
GrossParticipantCostPV_sct = GrossMeasCostPV_sct - ( IncentsDI + Rebates ) / PVF_sct
NetParticipantCostPV_sct   = NTGRCost * ( GrossParticipantCostPV_sct + ExcessIncentivesPV_sct )
NetParticipantCostAdjustedPV_sct = NetParticipantCostPV_sct + MEC * ( GrossMeasCostPV_sct + ExcessIncentivesPV_sct )
   GrossMeasCostPV_sct = NumUnits * ( UnitMeaCost1stBaseline - PVsub_sct ) / PVF_sct
   PVsub_sct = ( UnitMeaCost1stBaseline - UnitMeaCost2ndBaseline ) * ( (1 + MeasInflation / 4) / Rqf_sct ) ^ ( 4 * RUL_Yrs )
               if RUL_Yrs > 0, else 0
   ExcessIncentivesPV_sct = pos( IncentsDI / PVF_sct - GrossMeasCostPV_sct )
```

**Inputs:** those of the 1.5 entries; `DiscountRateQtr_SCT`.

- Example A, `RebatesandIncentsPV_sct`: (4000 + 2000) / 1.012575^2 = **5,851.90**
- Example A, `GrossParticipantCostPV_sct`: 100 * 200 / 1.012575^2 - (4000 + 2000) / 1.012575^2 = **13,654.43**
- Example A, `NetParticipantCostPV_sct`: 0.9 * (13654.4319 + 0.0000) = **12,288.99**
- Example B, `RebatesandIncentsPV_sct`: (9500 + 4000) / 1.012575^1 = **13,332.35**
- Example B, `GrossParticipantCostPV_sct`: 10 * (9000 - 2900.3214) / 1.012575^1 - (9500 + 4000) / 1.012575^1 = **46,906.93** (`PVsub_sct` = (9000 - 6000) * ((1 + 0.02 / 4) / 1.012575)^4.5)
- Example A, `NetParticipantCostAdjustedPV_sct`: 12288.9887 + 0.0000 = **12,288.99**
- Example B, `NetParticipantCostPV_sct`: 0.8 * (46906.9312 + 0.0000) = **37,525.54**
- Example B, `NetParticipantCostAdjustedPV_sct`: 37525.5450 + 0.05 * (60239.2769 + 0.0000) = **40,537.51**

### 1.7 Gross variants

The same quantity **without net-to-gross and without the market-effects adder** (`Gross`). Every net value above was scaled by `NTG + MEB`, so gross = net / (`NTG + MEB`):
the value that would come out with `NTG` = 1 and no market effects. Societal versions are blank when SCB/SCH are off. `ratio(n, d)` is 0 when `d = 0`. Unit: $ (PV).

| Field | Formula |
|---|---|
| `ElecBenGross` | `ratio( ElecBen, NTGRkWh + MEB )` |
| `ElecBenGross_SB` | `ratio( ElecBen_SB, NTGRkWh + MEB )` |
| `ElecBenGross_SH` | `ratio( ElecBen_SH, NTGRkWh + MEB )` |
| `ElecSupplyCostGross` | `ratio( ElecSupplyCost, NTGRkWh + MEB )` |
| `ElecSupplyCostGross_SB` | `ratio( ElecSupplyCost_SB, NTGRkWh + MEB )` |
| `ElecSupplyCostGross_SH` | `ratio( ElecSupplyCost_SH, NTGRkWh + MEB )` |
| `GasBenGross` | `ratio( GasBen, NTGRTherm + MEB )` |
| `GasBenGross_SB` | `ratio( GasBen_SB, NTGRTherm + MEB )` |
| `GasBenGross_SH` | `ratio( GasBen_SH, NTGRTherm + MEB )` |
| `GasSupplyCostGross` | `ratio( GasSupplyCost, NTGRTherm + MEB )` |
| `GasSupplyCostGross_SB` | `ratio( GasSupplyCost_SB, NTGRTherm + MEB )` |
| `GasSupplyCostGross_SH` | `ratio( GasSupplyCost_SH, NTGRTherm + MEB )` |
| `WaterEnergyBenGross` | `ratio( WaterEnergyBen, NTGRkWh + MEB )` |
| `WaterEnergyBenGross_SB` | `ratio( WaterEnergyBen_SB, NTGRkWh + MEB )` |
| `WaterEnergyBenGross_SH` | `ratio( WaterEnergyBen_SH, NTGRkWh + MEB )` |
| `WaterEnergyCostGross` | `ratio( WaterEnergyCost, NTGRkWh + MEB )` |
| `WaterEnergyCostGross_SB` | `ratio( WaterEnergyCost_SB, NTGRkWh + MEB )` |
| `WaterEnergyCostGross_SH` | `ratio( WaterEnergyCost_SH, NTGRkWh + MEB )` |

Written out, `ElecBenGross` (for a measure that is not fuel substitution) is the sum of `ElecBen` with `Elec(k)` replaced by `phased( k ; NumUnits * UnitkWh1stBaseline * InstallationRatekWh * RealizationRatekWh, ... )`:
the price of the quarter is unchanged, only the net-to-gross factor is removed. **Inputs:** those of the net column each is divided by, plus `NTGRkWh` or `NTGRTherm` and the market-effects setting.

- Example A, `ElecBenGross`: 5841.4973 / (0.8 + 0) = **7,301.87**
- Example A, `GasBenGross`: 3454.0256 / (0.9 + 0) = **3,837.81**
- Example A, `WaterEnergyBenGross`: 30.6679 / (0.8 + 0) = **38.33**
- Example A, `ElecBenGross_SB`: 6688.3412 / (0.8 + 0) = **8,360.43**
- Example A, `GasBenGross_SH`: 5033.3438 / (0.9 + 0) = **5,592.60**
- Example B, `ElecSupplyCostGross`: 4172.1680 / (0.75 + 0.05) = **5,215.21**
- Example B, `GasBenGross`: 6990.5647 / (0.85 + 0.05) = **7,767.29**
- Example B, `WaterEnergyCostGross`: 5.6259 / (0.75 + 0.05) = **7.03**
- Example B, `ElecSupplyCostGross_SB`: 4734.0060 / (0.75 + 0.05) = **5,917.51**
- Example B, `ElecBenGross`: 0 / (0.75 + 0.05) = **0.00**

### 1.8 Total system benefit and program-cost weighting

#### `TotalSystemBenefit`, `TotalSystemBenefitGross`

**Net system benefit: all benefits less all supply-type costs.** Unit: $ (PV; the "other" terms undiscounted).

**Formula**

```text
TotalSystemBenefit      = ( ElecBen + GasBen + WaterEnergyBen + OtherBen )
                          - ( ElecSupplyCost + GasSupplyCost + WaterEnergyCost + OtherCost )
TotalSystemBenefitGross = ( ElecBenGross + GasBenGross + WaterEnergyBenGross + OtherBenGross )
                          - ( ElecSupplyCostGross + GasSupplyCostGross + WaterEnergyCostGross + OtherCostGross )
```

Each term is the column of that name (1.4, 1.7). Tax credits and participant costs are not in it. **Inputs:** the benefit, supply-cost and other-cost columns named, and through them every input of 1.4 and 1.7.

- Example A, `TotalSystemBenefit`: (5841.4973 + 3454.0256 + 30.6679 + 160.00) - (0.00 + 0.00 + 0.00 + 80.00) = **9,406.19**
- Example B, `TotalSystemBenefit`: (0.0000 + 6990.5647 + 0.0000 + 320.00) - (4172.1680 + 0.0000 + 5.6259 + 120.00) = **3,012.77**
- Example A, `TotalSystemBenefitGross`: (7301.8716 + 3837.8062 + 38.3348 + 200.00) - (0.00 + 0.00 + 0.00 + 100.00) = **11,278.01**
- Example B, `TotalSystemBenefitGross`: (0.0000 + 7767.2941 + 0.0000 + 400.00) - (5215.2100 + 0.0000 + 7.0323 + 150.00) = **2,795.05**

#### `WeightedBenefits`

**The measure's share of its program's positive benefits:** the weight that spreads the program's costs over its measures. Unit: fraction, 0-1.

**Formula**

```text
WeightedBenefits = share_m = B_m / SUM over the measures j of the same program (same PrgID) of B_j        if that sum is not 0
                           = 1 / (the number of measures in the program)                                    if it is 0

B_m = pos(ElecBen) + pos(GasBen) + pos(WaterEnergyBen) + pos(OtherBen)               (the measure's own columns of 1.4)
```

Five further shares are computed the same way from other benefit sets and are used only inside the cost columns of 1.9: `share_gross` (from `ElecBenGross`, `GasBenGross`, `WaterEnergyBenGross`, `OtherBenGross`),
`share_SB` (from `ElecBen_SB`, `GasBen_SB`, `WaterEnergyBen_SB`, `OtherBen`), `share_SH`, `share_SB_gross` and `share_SH_gross`. **Inputs:** the benefit columns and `PrgID`.

- Example A, `WeightedBenefits`: the only measure of program `PRG1`, so 9486.1908 / 9486.1908 = **1.00**
- Example B, `WeightedBenefits`: the only measure of program `PRG2`, so 7310.5647 / 7310.5647 = **1.00**

#### `WeightedProgramCost`

**The measure's share of its program's nominal program cost.** Unit: $ (nominal, undiscounted). 0 without `ProgramCost.csv`.

**Formula**

```text
WeightedProgramCost = share_m * SumCosts

SumCosts = SUM over the ProgramCost rows r with the measure's PrgID of T_r
T_r = AdminCostsOverheadAndGA + AdminCostsOther + MarketingOutreach + DIActivity + DIInstallation
      + DIHardwareAndMaterials + DIRebateAndInspection + EMV + UserInputIncentive + CostsRecoveredFromOtherSources
      (the fields of row r; blank = 0)
```

`OnBillFinancing` is not part of CET's sum. A `ProgramCost` row whose `PA` is `SW` is treated as `SCE`, as CET did in the real runs. `share_m` is `WeightedBenefits`. **Inputs:** `ProgramCost.csv`
(the ten cost fields, `PrgID`, `PA`) and the measure's benefit columns.

- Example A, `WeightedProgramCost`: 1 * ((2000) + (1000)) = **3,000.00** (the 2028 row: 1500 + 500; the 2029 row: 1000)
- Example B, `WeightedProgramCost`: 1 * ((1200) + (600)) = **1,800.00** (the 2028 row: 900 + 300; the 2029 row: 600)

#### `TotalExpenditures`

**The measure's budget:** what the program spends on it, in nominal dollars - its rebates and incentives plus its share of the program costs. This is CET's
`TotalExpenditures` column, and summed over a run it is the **Budget** on CET's run receipt. Unit: $ (nominal, undiscounted).

**Formula**

```text
TotalExpenditures = RebatesandIncents + WeightedProgramCost
```

The run-receipt Budget equals this sum to the cent on CET jobs 121219 ($254,427,868.17) and 121841. A measure's own value depends on its share of program costs
(`WeightedBenefits`), which follows its benefits, so per-measure values match CET only as closely as the benefits do; per-program totals do not depend on the shares.
**Inputs:** those of `RebatesandIncents` and `WeightedProgramCost`.

- Example A, `TotalExpenditures`: 6000.00 + 3000.00 = **9,000.00**
- Example B, `TotalExpenditures`: 13500.00 + 1800.00 = **15,300.00**

#### `WtdAdminCostsOverheadAndGA`, `WtdAdminCostsOther`, `WtdMarketingOutreach`, `WtdDIActivity`, `WtdDIInstallation`, `WtdDIHardwareAndMaterials`, `WtdDIRebateAndInspection`, `WtdEMV`, `WtdUserInputIncentive`, `WtdCostsRecoveredFromOtherSources`, `ProgramCosts`

**The measure's share of each program-cost field,** in nominal dollars, and their sum. Unit: $ (nominal, undiscounted). 0 without `ProgramCost.csv`.

**Formula**

```text
Wtd<field>   = share_m * SUM over the ProgramCost rows r with the measure's PrgID of <field>_r          (blank = 0)
ProgramCosts = the sum of the ten Wtd columns = share_m * SumCosts = WeightedProgramCost
```

`<field>` is each of the ten cost fields of `T_r` (see `WeightedProgramCost`); `share_m` is `WeightedBenefits`. Per program they add up to the program's own totals; a measure's own values follow its
share, so they match CET per measure only as closely as the benefits do (the program totals match exactly on CET jobs 121219-121222). **Inputs:** `ProgramCost.csv` (the ten cost fields,
`PrgID`) and the measure's benefit columns.

- Example A, `WtdAdminCostsOverheadAndGA`: 1 * 1500 = **1,500.00**
- Example A, `WtdEMV`: 1 * 1000 = **1,000.00** (the 2029 row)
- Example A, `ProgramCosts`: 1500 + 500 + 1000 = **3,000.00**
- Example B, `WtdDIActivity`: 1 * 300 = **300.00**
- Example B, `WtdMarketingOutreach`: 1 * 600 = **600.00**
- Example B, `ProgramCosts`: 900 + 300 + 600 = **1,800.00**

#### `WeightedElecAlloc`

**The electric share of the measure's own positive benefits:** the share of its costs the levelized columns (1.16) charge to electricity. Unit: fraction, 0-1.

**Formula**

```text
WeightedElecAlloc = ratio( pos(ElecBen) , pos(ElecBen) + pos(GasBen) + pos(WaterEnergyBen) + pos(OtherBen) )
```

A measure-level share (not across the program). It follows CET's formula exactly: computed from CET's own benefit columns it reproduces CET's `WeightedElecAlloc` on every measure of CET jobs
121219-121222. **Inputs:** the benefit columns of 1.4.

- Example A, `WeightedElecAlloc`: 5841.4973 / (5841.4973 + 3454.0256 + 30.6679 + 160.0000) = **0.615790**
- Example B, `WeightedElecAlloc`: 0 / (0 + 6990.5647 + 0 + 320.0000) = **0.000000** (a fuel-substitution measure with no electric benefit)

### 1.9 Cost-test costs

`NoAdmin` leaves out the program-level term. All are present values in dollars. The quantities used by more than one entry:

```text
Sup         = ElecSupplyCost + GasSupplyCost + WaterEnergyCost + OtherCost
SumCostsNPV = SUM over the program's ProgramCost rows r of  T_r / Raf ^ (Year_r - F)
Year_r = first four characters of the row's ClaimYearQuarter;  Raf = 1 + DiscountRateAnnual(PA of the row)
SumCostsNPV_SCT = the same with Raf_sct = 1 + DiscountRateAnnual_SCT
share_m = WeightedBenefits (1.8)         T_r: see WeightedProgramCost
```

#### `PACCostNoAdmin`, `PACCost`

**Program Administrator Cost test cost.** Unit: $ (PV).

**Formula**

```text
PACCostNoAdmin = RebatesandIncentsPV + Sup
PACCost        = share_m * SumCostsNPV + PACCostNoAdmin
```

**Inputs:** the columns named, `ProgramCost.csv` and the First Year.

- Example A, `PACCostNoAdmin`: 5776.6364 + (0.00 + 0.00 + 0.00 + 80.00) = **5,856.64**
- Example A, `PACCost`: 1 * (2000 / 1.0766^0 + 1000 / 1.0766^1) + 5856.6364 = **8,785.49**
- Example B, `PACCostNoAdmin`: 13246.3327 + (4172.1680 + 0.00 + 5.6259 + 120.00) = **17,544.13**
- Example B, `PACCost`: 1 * (1200 / 1.0766^0 + 600 / 1.0766^1) + 17544.1265 = **19,301.44**

#### `TRCCostNoAdmin`, `TRCCost`

**Total Resource Cost test cost.** Unit: $ (PV).

**Formula**

```text
TRCCostNoAdmin = RebatesandIncentsPV + NetParticipantCostAdjustedPV + Sup
TRCCost        = share_m * SumCostsNPV + TRCCostNoAdmin
```

**Inputs:** `RebatesandIncentsPV`, `NetParticipantCostAdjustedPV`, the four columns of `Sup`, `WeightedBenefits`, `ProgramCost.csv`, the First Year and `DiscountRateAnnual`.

- Example A, `TRCCostNoAdmin`: 5776.6364 + 12130.9364 + 80.00 = **17,987.57**
- Example A, `TRCCost`: 2928.8501 + 17987.5728 = **20,916.42** (`SumCostsNPV` = 2000 / 1.0766^0 + 1000 / 1.0766^1)
- Example B, `TRCCostNoAdmin`: 13246.3327 + 40970.3555 + 4297.7938 = **58,514.48**
- Example B, `TRCCost`: 1757.3101 + 58514.4821 = **60,271.79** (`SumCostsNPV` = 1200 / 1.0766^0 + 600 / 1.0766^1)

#### `TRCCostGross`

**TRC cost on gross values.** Unit: $ (PV).

**Formula**

```text
TRCCostGross = share_gross_m * SumCostsNPV + RebatesandIncentsPV + GrossParticipantCostPV + SupGross
SupGross     = ElecSupplyCostGross + GasSupplyCostGross + WaterEnergyCostGross + OtherCostGross
```

`share_gross_m` is the share of 1.8 computed on the gross benefits. (The tool uses *gross* supply costs here; CET adds the net supply costs to the gross participant cost.)
**Inputs:** `RebatesandIncentsPV`, `GrossParticipantCostPV`, the four Gross columns of `SupGross`, the gross benefit columns, `ProgramCost.csv`, the First Year and `DiscountRateAnnual`.

- Example A, `TRCCostGross`: 2928.8501 + 5776.6364 + 13478.8183 + 100.00 = **22,284.30**
- Example B, `TRCCostGross`: 1757.3101 + 13246.3327 + 47421.2222 + 5372.2423 = **67,797.11**

#### `RIMCostNoAdmin`, `RIMCost`

**Ratepayer Impact Measure test cost:** the program-administrator cost plus the customers' bill reduction. Unit: $ (PV).

**Formula**

```text
RIMCostNoAdmin = BillReducElec + BillReducGas + PACCostNoAdmin
RIMCost        = BillReducElec + BillReducGas + PACCost
```

**Inputs:** the bill-impact columns of 1.5 and the inputs of `PACCost`.

- Example A, `RIMCostNoAdmin`: 30972.4907 + 10167.7073 + 5856.6364 = **46,996.83**
- Example A, `RIMCost`: 30972.4907 + 10167.7073 + 8785.4865 = **49,925.68**
- Example B, `RIMCostNoAdmin`: 0.0000 + 13198.1966 + 17544.1265 = **30,742.32**
- Example B, `RIMCost`: 0.0000 + 13198.1966 + 19301.4366 = **32,499.63**

#### `SCBCostNoAdmin`, `SCBCost`, `SCBCostGross`, `SCHCostNoAdmin`, `SCHCost`, `SCHCostGross`

**Societal Cost test cost, Base (`SCB`) and High (`SCH`).** Unit: $ (PV at the societal rate). Blank when SCB/SCH are off.

**Formula**

```text
SCBCostNoAdmin = RebatesandIncentsPV_sct + NetParticipantCostAdjustedPV_sct + Sup_SB
SCBCost        = share_SB_m * SumCostsNPV_SCT + SCBCostNoAdmin
SCBCostGross   = share_SB_gross_m * SumCostsNPV_SCT + RebatesandIncentsPV_sct + GrossParticipantCostPV_sct + SupSBGross

Sup_SB      = ElecSupplyCost_SB + GasSupplyCost_SB + WaterEnergyCost_SB + OtherCost
SupSBGross  = ElecSupplyCostGross_SB + GasSupplyCostGross_SB + WaterEnergyCostGross_SB + OtherCostGross
share_SB_m  = the share of 1.8 computed on  B_SB = pos(ElecBen_SB) + pos(GasBen_SB) + pos(WaterEnergyBen_SB) + pos(OtherBen)
share_SB_gross_m = the same share computed on the gross versions of those benefits
(SCHCostNoAdmin, SCHCost, SCHCostGross: the same with _SH)
```

`SumCostsNPV_SCT` discounts program costs at the societal rate (CET uses the primary rate for them; see CALCULATIONS C17). **Inputs:** `RebatesandIncentsPV_sct`, `NetParticipantCostAdjustedPV_sct`, `GrossParticipantCostPV_sct`, the `_SB` / `_SH` supply-cost and gross columns, `OtherCost`, `ProgramCost.csv`, the First Year and `DiscountRateAnnual_SCT`.

- Example A, `SCBCostNoAdmin`: 5851.8994 + 12288.9887 + 80.00 = **18,220.89**
- Example A, `SCBCost`: 2952.1089 + 18220.8880 = **21,173.00** (`SumCostsNPV_SCT` = 2000 / 1.0503^0 + 1000 / 1.0503^1)
- Example A, `SCHCost`: 2952.1089 + 18220.8880 = **21,173.00**
- Example B, `SCBCostNoAdmin`: 13332.3458 + 40537.5088 + 4860.4757 = **58,730.33**
- Example B, `SCBCost`: 1771.2654 + 58730.3302 = **60,501.60**
- Example B, `SCHCostNoAdmin`: 13332.3458 + 40537.5088 + 5291.5034 = **59,161.36**
- Example B, `SCHCost`: 1771.2654 + 59161.3579 = **60,932.62**
- Example A, `SCBCostGross`: 2952.1089 + 5851.8994 + 13654.4319 + 100 = **22,558.44** (`SupSBGross` = `OtherCostGross` = 100)

### 1.10 Benefit-cost ratios

`B = ElecBen + GasBen + WaterEnergyBen + OtherBen`. A ratio is 0 when its cost is 0, and blank for the societal ratios when SCB/SCH are off. Unit: ratio (no unit). Supply costs are inside the *costs*, not netted
from the numerator.

#### `TRCRatio`, `TRCRatioNoAdmin`

**Formula**

```text
TRCRatio        = ( B + TaxCredits ) / TRCCost
TRCRatioNoAdmin = ( B + TaxCredits ) / TRCCostNoAdmin
```

**Inputs:** the four benefit columns of `B`, `TaxCredits`, `TRCCost` and `TRCCostNoAdmin`.

- Example A, `TRCRatio`: (9486.1908 + 400.00) / 20916.4229 = **0.4727**
- Example A, `TRCRatioNoAdmin`: (9486.1908 + 400.00) / 17987.5728 = **0.5496**
- Example B, `TRCRatio`: (7310.5647 + 800.00) / 60271.7921 = **0.1346**
- Example B, `TRCRatioNoAdmin`: (7310.5647 + 800.00) / 58514.4821 = **0.1386**

#### `PACRatio`, `PACRatioNoAdmin`

**Formula**

```text
PACRatio        = B / PACCost
PACRatioNoAdmin = B / PACCostNoAdmin
```

**Inputs:** the four benefit columns of `B`, `PACCost` and `PACCostNoAdmin`.

- Example A, `PACRatio`: 9486.1908 / 8785.4865 = **1.0798**
- Example A, `PACRatioNoAdmin`: 9486.1908 / 5856.6364 = **1.6197**
- Example B, `PACRatio`: 7310.5647 / 19301.4366 = **0.3788**
- Example B, `PACRatioNoAdmin`: 7310.5647 / 17544.1265 = **0.4167**

#### `RIMRatio`, `RIMRatioNoAdmin`

**Formula**

```text
RIMRatio        = ( B + BillIncrElec + BillIncrGas ) / RIMCost
RIMRatioNoAdmin = ( B + BillIncrElec + BillIncrGas ) / RIMCostNoAdmin
```

**Inputs:** the four benefit columns of `B`, `BillIncrElec`, `BillIncrGas`, `RIMCost` and `RIMCostNoAdmin`.

- Example A, `RIMRatio`: (9486.1908 + 0.00 + 0.00) / 49925.6844 = **0.1900**
- Example A, `RIMRatioNoAdmin`: (9486.1908 + 0.00 + 0.00) / 46996.8343 = **0.2018**
- Example B, `RIMRatio`: (7310.5647 + 20764.8592 + 0.00) / 32499.6332 = **0.8639**
- Example B, `RIMRatioNoAdmin`: (7310.5647 + 20764.8592 + 0.00) / 30742.3231 = **0.9132**

#### `SCBRatio`, `SCBRatioNoAdmin`, `SCHRatio`, `SCHRatioNoAdmin`

**Formula**

```text
SCBRatio        = ( ElecBen_SB + GasBen_SB + WaterEnergyBen_SB + OtherBen ) / SCBCost
SCBRatioNoAdmin = the same numerator / SCBCostNoAdmin
SCHRatio        = ( ElecBen_SH + GasBen_SH + WaterEnergyBen_SH + OtherBen ) / SCHCost
SCHRatioNoAdmin = the same numerator / SCHCostNoAdmin
```

**Inputs:** the `_SB` / `_SH` benefit columns, `OtherBen`, and the societal cost columns of 1.9.

- Example A, `SCBRatio`: (6688.3412 + 4314.2947 + 35.5698 + 160.00) / 21172.9970 = **0.5289**
- Example A, `SCBRatioNoAdmin`: 11198.2057 / 18220.8880 = **0.6146**
- Example A, `SCHRatio`: (7296.3723 + 5033.3438 + 39.2180 + 160.00) / 21172.9970 = **0.5917**
- Example A, `SCHRatioNoAdmin`: 12528.9340 / 18220.8880 = **0.6876**
- Example B, `SCBRatio`: (0.00 + 8827.1057 + 0.00 + 320.00) / 60501.5955 = **0.1512**
- Example B, `SCBRatioNoAdmin`: 9147.1057 / 58730.3302 = **0.1557**
- Example B, `SCHRatio`: (0.00 + 10432.0340 + 0.00 + 320.00) / 60932.6233 = **0.1765**
- Example B, `SCHRatioNoAdmin`: 10752.0340 / 59161.3579 = **0.1817**

#### `PCTBen`, `PCTCost`, `PCTRatio`

**The Participant Cost Test** (CA Standard Practice Manual, Chapter 2), which the CET does not calculate: the participant's benefits (bill reductions, incentives received,
tax credits) against the participant's costs (the full measure cost and any bill increase), on gross savings and without market effects. `PCTBen` and `PCTCost` are $ (PV);
`PCTRatio` is a ratio (0 when `PCTCost` is 0). Not yet checked against a CPUC definition: CPUC has not published how the CET will calculate the test.

**Formula**

```text
PCTBen   = BillReducElecGross + BillReducGasGross + RebatesandIncentsPV + TaxCreditsGross / PVF
PCTCost  = GrossParticipantCostPV + RebatesandIncentsPV + BillIncrElecGross + BillIncrGasGross          (GrossParticipantCostPV + RebatesandIncentsPV = the measure cost before incentives)
PCTRatio = PCTBen / PCTCost
```

**Where** `PVF = Rqf ^ Qm` is the participant-cost discount factor of `RebatesandIncentsPV` (1.5): the claim quarter at the primary quarterly rate.
**Inputs:** the columns named in the formula and their inputs; no program costs.

- Example A, `PCTBen`: 38715.6133 + 11297.4525 + 5776.6364 + 500 / 1.0386667225 = **56,271.09**
- Example A, `PCTCost`: 13478.8183 + 5776.6364 + 0.00 + 0.00 = **19,255.45**
- Example A, `PCTRatio`: 56271.0886 / 19255.4547 = **2.9223**
- Example B, `PCTBen`: 0.00 + 14664.6629 + 13246.3327 + 1000 / 1.01915 = **28,892.21**
- Example B, `PCTCost`: 47421.2222 + 13246.3327 + 25956.0741 + 0.00 = **86,623.63**
- Example B, `PCTRatio`: 28892.2054 / 86623.6290 = **0.3335**

### 1.11 Emissions

Present when "Include emissions" is on. *First-year* is the measure's **first four quarters** of life (`k` = 0 ... 3, or fewer if the life is shorter), not a calendar year; `...Lifecycle` is all of its quarters. `Net` includes net-to-gross
and `MEB`; `Gross` does not. Units: CO2 metric tons; NOx and PM10 pounds.

**The pieces every emission formula uses**

```text
fe_net   = NumUnits * (NTGRkWh + MEB) * InstallationRatekWh * RealizationRatekWh
fe_gross = NumUnits * InstallationRatekWh * RealizationRatekWh
fg_net   = NumUnits * (NTGRTherm + MEB) * InstallationRateTherm * RealizationRateTherm
fg_gross = NumUnits * InstallationRateTherm * RealizationRateTherm

co2(k)   = tonnes of CO2 emitted per annual kWh delivered in quarter k:
   SUM over the hours i of quarter(k) of  shape(i) * emission_rate_tonnes_per_kwh(year(k), i)     (the ACC's own hourly rate)
   Quarters exist only for years the ACC file has: life beyond its last year adds no emissions.

AnnualkWh(k) = the per-unit annual kWh in force during quarter k, with  EQ = 4 * EUL_Yrs,  RQ = 4 * RUL_Yrs  (RQ = 0 if RUL_Yrs >= EUL_Yrs),
               kWh1 = UnitkWh1stBaseline,  kWh2 = UnitkWh2ndBaseline:
   single baseline (RQ = 0):  0 if kWh1 = 0;  kWh1 * (EQ mod 1) if k < EQ < k + 1 (the last part-quarter);  else kWh1
   dual baseline (RQ > 0):    0 if kWh1 = kWh2 = 0;  kWh1 if RQ > k + 1;  kWh2 if RQ < k and EQ > k + 1;
                              kWh1 * (RQ mod 1) + kWh2 * (EQ - RQ)      if RQ and EQ both fall inside quarter k;
                              kWh1 * (RQ mod 1) + kWh2 * (1 - RQ mod 1) if only RQ falls inside quarter k;
                              kWh2 * (EQ - floor(EQ))                   if only EQ falls inside quarter k;   else kWh2
   counted quarters: k = 0 ... EQ - 1 (the quarters of 0.2)

nox, pm10 = the NOx / PM10 rate (lb per kWh) of the Emissions table (CET's) for the key
            (PA, AC_Elec_Target_Sector, AC_Elec_Impact_Profile, AC_Climate_Zone); 0 where the table has no row
NOxfactor = the NOx factor (lb per therm) of the CombustionType table (CET's) for CombustionType; 0 when CombustionType is blank or unknown
CO2Gas    = metric tons of CO2 per therm, E3 settings for PA (0.00531)
G_firstyear = Th1                                  if RUL >= 1
            = RUL*Th1 + (1 - RUL)*Th2              if 0 < RUL < 1 <= EUL
            = RUL*Th1 + (EUL - RUL)*Th2            if RUL < EUL < 1
            = Th1                                  if RUL = 0 and EUL > 1
            = EUL*Th1                              if RUL = 0 and EUL <= 1
G_lifecycle = RUL*Th1 + (EUL - RUL)*Th2 if RUL > 0;   EUL*Th1 otherwise
     (Th1 = UnitTherm1stBaseline, Th2 = UnitTherm2ndBaseline, EUL = max(EUL_Yrs, 0.25), RUL = max(RUL_Yrs, 0), 0 if RUL >= EUL)
```

#### `NetElecCO2`, `GrossElecCO2`, `NetElecCO2Lifecycle`, `GrossElecCO2Lifecycle`

**Electric CO2 emissions avoided.** Unit: metric tons.

**Formula**

```text
NetElecCO2            = fe_net   * SUM over k = 0 ... min(3, n-1) of  co2(k) * AnnualkWh(k)
GrossElecCO2          = fe_gross * SUM over k = 0 ... min(3, n-1) of  co2(k) * AnnualkWh(k)
NetElecCO2Lifecycle   = fe_net   * SUM over k = 0 ... n-1 of          co2(k) * AnnualkWh(k)
GrossElecCO2Lifecycle = fe_gross * SUM over k = 0 ... n-1 of          co2(k) * AnnualkWh(k)
```

**Inputs:** `NumUnits`, `NTGRkWh`, `InstallationRatekWh`, `RealizationRatekWh`, `UnitkWh1stBaseline`, `UnitkWh2ndBaseline`, `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`, the electric-profile fields of `ElecBen`; the ACC's
`emission_rate_tonnes_per_kwh`; market effects. A negative value is added emissions (added load).

- Example A, `NetElecCO2`: 68.4 * 0.0002 * 500 * (2208 + 2208 + 2160 + 2184) / 8760 = **6.8400**
- Example A, `GrossElecCO2`: 85.50 * 0.0002 * 500 * (2208 + 2208 + 2160 + 2184) / 8760 = **8.5500**
- Example A, `NetElecCO2Lifecycle`: 68.4 * 0.0002 / 8760 * (2208 * 500 + 2208 * 500 + 2160 * 500 + 2184 * 500 + 2208 * 500 + 2208 * 500 + 2160 * 500 + 2184 * 500 + 2208 * 500 + 2208 * 300) = **16.4385** (each quarter's hours times 500; the last part-quarter, k = 9, counts 0.6)
- Example B, `NetElecCO2`: 7.2 * 0.0002 * -4000 * (2184 + 2208 + 2208 + 2160) / 8760 = **-5.7600**
- Example B, `NetElecCO2Lifecycle`: 7.2 * 0.0002 / 8760 * (2184 * -4000 + 2208 * -4000 + 2208 * -4000 + 2160 * -4000 + 2184 * -3250 + 2208 * -2500 + 2208 * -2500 + 2160 * -2500 + 2184 * -2500 + 2208 * -2500) = **-11.4342** (each quarter's hours times `AnnualkWh(k)`: -4000 for k = 0-3, the blend -4000 * 0.5 + -2500 * 0.5 = -3250 for k = 4, -2500 for k = 5-9)

#### `NetElecNOx`, `GrossElecNOx`, `NetElecNOxLifecycle`, `GrossElecNOxLifecycle`, `NetPM10`, `GrossPM10`, `NetPM10Lifecycle`, `GrossPM10Lifecycle`

**Electric NOx and PM10 (particulates under 10 microns) emissions avoided.** Unit: pounds. 0 where the Emissions table (CET's) has no row for the measure's key.

**Formula**

```text
NetElecNOx            = fe_net   * SUM over k = 0 ... min(3, n-1) of  nox  * AnnualkWh(k) / 4
GrossElecNOx          = fe_gross * SUM over k = 0 ... min(3, n-1) of  nox  * AnnualkWh(k) / 4
NetElecNOxLifecycle   = fe_net   * SUM over k = 0 ... n-1 of          nox  * AnnualkWh(k) / 4
GrossElecNOxLifecycle = fe_gross * SUM over k = 0 ... n-1 of          nox  * AnnualkWh(k) / 4
NetPM10               = fe_net   * SUM over k = 0 ... min(3, n-1) of  pm10 * AnnualkWh(k) / 4
GrossPM10             = fe_gross * SUM over k = 0 ... min(3, n-1) of  pm10 * AnnualkWh(k) / 4
NetPM10Lifecycle      = fe_net   * SUM over k = 0 ... n-1 of          pm10 * AnnualkWh(k) / 4
GrossPM10Lifecycle    = fe_gross * SUM over k = 0 ... n-1 of          pm10 * AnnualkWh(k) / 4
```

The `/ 4` is because `nox` and `pm10` are per annual kWh and each quarter carries a quarter of a year. **Inputs:** those of `NetElecCO2`, plus `AC_Elec_Target_Sector`, `AC_Elec_Impact_Profile`, `AC_Climate_Zone`, `IOU_AC_Territory` (the Emissions table key).

- Example A, `NetElecNOx`: 0, because the Emissions table has no row for the key (PGE, Residential, `W_SHAPE`, zone 13) = **0.0000**
- Example A, `NetPM10`: 0, same key = **0.0000**
- Example B, `NetElecNOx`: the key (PGE, Commercial, `3 = Commercial HVAC`, zone 13) has `nox` = 0.00004643: 7.2 * (4 * 0.00004643 * -4000 / 4) = **-1.3372**
- Example B, `NetPM10`: `pm10` = 0.00004643 for the same key: 7.2 * (4 * 0.00004643 * -4000 / 4) = **-1.3372**
- Example B, `NetElecNOxLifecycle`: 7.2 * 0.00004643 / 4 * (-4000 * 4 + -3250 + -2500 * 5) = **-2.6535**

#### `NetGasCO2`, `GrossGasCO2`, `NetGasCO2Lifecycle`, `GrossGasCO2Lifecycle`

**Gas CO2 emissions avoided.** Unit: metric tons.

**Formula**

```text
NetGasCO2            = fg_net   * CO2Gas * G_firstyear          GrossGasCO2            = fg_gross * CO2Gas * G_firstyear
NetGasCO2Lifecycle   = fg_net   * CO2Gas * G_lifecycle          GrossGasCO2Lifecycle   = fg_gross * CO2Gas * G_lifecycle
```

`fg_...`, `CO2Gas`, `G_firstyear`, `G_lifecycle` are defined above. **Inputs:** `NumUnits`, `NTGRTherm`, `InstallationRateTherm`, `RealizationRateTherm`, `UnitTherm1stBaseline`, `UnitTherm2ndBaseline`, `EUL_Yrs`, `RUL_Yrs`,
`IOU_AC_Territory`; ACC version (E3 `CO2Gas`); market effects.

- Example A, `NetGasCO2`: 81 * 0.00531 * 20 = **8.6022** (`fg_net` = 100 * 0.9 * 1 * 0.9; `RUL` = 0, `EUL` = 2.4 > 1, so `G_firstyear` = 20)
- Example A, `NetGasCO2Lifecycle`: 81 * 0.00531 * 48 = **20.6453** (`G_lifecycle` = 2.4 * 20)
- Example B, `NetGasCO2`: 8.55 * 0.00531 * 600 = **27.2403** (`fg_net` = 10 * (0.85 + 0.05) * 1 * 0.95; `RUL` = 1.125 >= 1, so `G_firstyear` = `Th1` = 600)
- Example B, `NetGasCO2Lifecycle`: 8.55 * 0.00531 * 1225 = **55.6156** (`G_lifecycle` = 1.125 * 600 + (2.5 - 1.125) * 400)
- Example B, `GrossGasCO2`: 9.5 * 0.00531 * 600 = **30.2670**

#### `NetGasNOx`, `GrossGasNOx`, `NetGasNOxLifecycle`, `GrossGasNOxLifecycle`

**Gas NOx emissions avoided.** Unit: pounds. 0 when `CombustionType` is blank or unknown.

**Formula**

```text
NetGasNOx            = fg_net   * NOxfactor * G_firstyear       GrossGasNOx            = fg_gross * NOxfactor * G_firstyear
NetGasNOxLifecycle   = fg_net   * NOxfactor * G_lifecycle       GrossGasNOxLifecycle   = fg_gross * NOxfactor * G_lifecycle
```

**Inputs:** those of `NetGasCO2`, plus `CombustionType` (the CombustionType table key, `<combustor type>:<controls>`).

- Example A, `NetGasNOx`: `CombustionType` is blank, so `NOxfactor` = 0 and 81 * 0 * 20 = **0.0000**
- Example B, `NetGasNOx`: `CombustionType` = `Small Boilers (<100 MMBtu/hr Heat Input):Uncontrolled`, `NOxfactor` = 0.0098: 8.55 * 0.0098 * 600 = **50.2740**
- Example B, `GrossGasNOxLifecycle`: 9.5 * 0.0098 * 1225 = **114.0475**

### 1.12 `CreditedIOU`

#### `CreditedIOU`

**The IOU the measure is credited to:** the `PA` of the measure's program in the `ProgramCost.csv` input, exactly as filed. Text. Used for the portfolio summary's
"credited IOU" rows.

* Statewide allocation **off**: the filed `PA`, so an unallocated statewide program (filed with `PA` = `SW`) shows **`SW`**, and an IOU's own program shows that IOU.
* Statewide allocation **on**: the `ProgramCost.csv` rows are allocated like the measures (C15), so each allocated share of a statewide program (`<IOU>_SW_...`) shows
  the IOU it was allocated to (its program row's `PA` is set to that IOU); every other program keeps its filed `PA`.
* No `ProgramCost.csv`, or no row for the program: blank.

**Formula**

```text
CreditedIOU = PA of the ProgramCost row with  PrgID = the measure's PrgID  and  PrgYear = the measure's claim year
            = PA of the first ProgramCost row with that PrgID      if no row has that year
            = blank                                                if there is no such row (or no ProgramCost.csv)
   (with statewide allocation: the allocated ProgramCost rows and the allocated PrgID)
```

**Inputs:** `PrgID`, `ClaimYearQuarter`; `ProgramCost.csv`: `PrgID`, `PrgYear`, `PA`. Settings: "Allocate statewide programs".

- Example A, `CreditedIOU`: `PrgID` = `PRG1`, claim year 2028; the `PRG1` / 2028 program-cost row has `PA` = **PGE**
- Example B, `CreditedIOU`: `PrgID` = `PRG2`, claim year 2028; the `PRG2` / 2028 program-cost row has `PA` = **PGE**

### 1.13 Component columns (only with "Calculate ACC components separately")

Placed before `CreditedIOU`. Each avoided-cost component is priced alone through the same route as the total, so the components always add up. They are signed and **before** the benefit / supply-cost split.

#### `ElecValue_<component>`

**Discounted lifetime dollars of one component of the electric avoided cost.** One column per component present in the electric ACC: `energy`, `generation_capacity`, `avoided_as_procurement`, `losses`,
`methane_leakage`, `ghg_cap_and_trade`, `ghg_cap_and_invest`, `ghg_adder`, `ghg_portfolio_rebalancing`, `air_quality_adder`, `transmission`, `distribution`. Unit: $ (PV, TRC rows).

**Formula** (hourly-profile route; `c` is the component column of the ACC)

```text
ElecValue_c = SUM over k = 0 ... n-1 of
                w(k) * DF(k) * Elec(k) * ( SUM over the hours i of quarter(k) of  shape(i) * c(y*(k), i) / 1000 )
SUM over c of ElecValue_c = V_elec = ElecBen - ElecSupplyCost          (checked on every run and reported in the log)
```

**Inputs:** those of `ElecBen`.

#### `GasValue_<component>`

**Discounted lifetime dollars of one component of the gas avoided cost:** `market`, `td`, `nox`, `co2`, `upstream_methane`, `behind_meter_methane`, `air_quality_adder`. Unit: $ (PV, TRC rows).

**Formula**

```text
GasValue_c = SUM over k = 0 ... n-1 of
               w(k) * DF(k) * Gas(k) * ( SUM over the months m of quarter(k) of  gasshape(m) * c_m(y^(k)) )
               c = the gas ACC component
SUM over c of GasValue_c = V_gas = GasBen - GasSupplyCost
```

`behind_meter_methane` is 0 for a measure that does not include it (0.6). **Inputs:** those of `GasBen`.

### 1.14 Program metadata (`Prg_*`)

The program's row from the program metadata: the CEDARS Programs list built into the package (CEDARS' export of 2026-10-03, 1,416 programs; its `ProgramManager` column,
which holds staff names, is not included), with the rows of a supplied program-metadata file replacing (same `PrgID`) or extending it. Placed after `CreditedIOU`, one
column per column of the table, prefixed `Prg_`. Text, exactly as in the table. **They change no priced value**: they label the measure, give the summary its
`program segment` rows, and feed the Exclude_From_CE option.

#### `Prg_MatchedOn`

**Which program-metadata row the measure's `Prg_` columns come from.** Text.

**Formula**

```text
Prg_MatchedOn = PrgID                                if the table has a row with the measure's PrgID (as priced: with statewide allocation, the allocated <IOU>_SW_... PrgID)
              = PrgID + " + agreeing IOU copies"     if PrgID is a statewide SW_... row and some of its blank fields were filled from its <IOU>_SW_... rows
              = "agreeing IOU copies"                if there is no SW_... row but there are <IOU>_SW_... rows
              = "(not found)"                        otherwise (every Prg_ column is then blank)
```

The CEDARS list carries each statewide program once per funding IOU, with the IOU prepended (`PGE_SW_HVAC_Up`, `SCE_SW_HVAC_Up`, ...), plus a thin `SW_HVAC_Up` row
(`PA` = `SW`, most fields blank). With **Allocate statewide programs** on, each allocated share carries its IOU's `PrgID` and so gets that IOU's own row. With it off, the
measure keeps the `SW_...` `PrgID`; a field that the `SW_...` row leaves blank is filled from the `<IOU>_SW_...` rows **only when every one of them gives the same non-blank
value** (they often differ, for example in `ProgramName` and `ProgramImplementer`), and is left blank otherwise.

**Inputs:** `PrgID`; settings: "Allocate statewide programs", "Program metadata".

- Example A, `Prg_MatchedOn`: the supplied table has a `PRG1` row = **PRG1**
- Example B, `Prg_MatchedOn`: the supplied table has a `PRG2` row = **PRG2**

#### `Prg_<column>`

**The value of one column of the measure's program-metadata row** (see `Prg_MatchedOn`). Blank when the program is not found, or the table leaves it blank. A supplied
table's extra columns are carried the same way (`Prg_<its column name>`).

**Formula**

```text
Prg_X = column X of the program-metadata row chosen for the measure (Prg_MatchedOn)
```

**Inputs:** `PrgID`; the program metadata (built in, or supplied).

| Column | Meaning | Example A | Example B |
|---|---|---|---|
| `Prg_PA` | The program administrator the program list gives (`SW` for a statewide program's `SW_...` row, the IOU for its `<IOU>_SW_...` rows). | PGE | PGE |
| `Prg_ProgramName` | The program's name. | Example Residential Program | Example Commercial Program |
| `Prg_ProgramStatus` | `active`, `new`, `closed`, `closed_to_new_commitments` or `closure_pending`. | active | new |
| `Prg_ProgramSegment` | The portfolio segment: `resource_acquisition`, `market_support`, `equity`, `codes_and_standards` or `evaluation_measurement_verification` (blank on many older programs). Drives the summary's `program segment` rows. | resource_acquisition | market_support |
| `Prg_StatewideProgram` | For a statewide program, its statewide program code and name (for example `SW_HVAC_Up - SW HVAC Upstream Program`); blank otherwise. |  |  |
| `Prg_PrimarySector` | The program's primary sector (`Residential`, `Commercial`, `Industrial`, `Agricultural`, `Public`, `Cross-Cutting`, `Portfolio Support`). | Residential | Commercial |
| `Prg_Sector` | The program's sector in CEDARS' coded form (for example `Commercial (Com)`). | Residential (Res) | Commercial (Com) |
| `Prg_BPSector` | The business-plan sector split, as text (for example `Commercial: 0.900000, Industrial: 0.100000`). | Residential: 1.000000 | Commercial: 1.000000 |
| `Prg_ProgramImplementer` | Who implements it (`IOU Core`, `Third-Party Solicited`, `REN/CCA`, ...). | IOU Core | Third-Party Solicited |
| `Prg_ProgramCategory` | The program category (`New Construction`, `Market Transformation`, `Financing`, ...). | Other | Market Transformation |
| `Prg_ImplementationContractor` | The implementation contractor, where given. |  | Example Implementer |
| `Prg_StartYear` | The program's first year. | 2024 | 2027 |
| `Prg_EndYear` | The program's last year (blank = open-ended). |  | 2031 |
| `Prg_Resource_Flag` | `True` for a resource program. | True | False |
| `Prg_NonResource_Flag` | `True` for a non-resource program. | False | True |
| `Prg_Deemed_Flag` | `True` when the program has deemed measures. | True | False |
| `Prg_Custom_Flag` | `True` when the program has custom measures. | False | True |
| `Prg_Upstream_Flag` | `True` when the program delivers upstream. | False | False |
| `Prg_Midstream_Flag` | `True` when the program delivers midstream. | False | True |
| `Prg_Downstream_Flag` | `True` when the program delivers downstream. | True | False |
| `Prg_DirectInstall` | `Yes`, `Partial` or `No`. | No | Partial |
| `Prg_Audit_Flag` | `True` when the program includes audits. | False | True |
| `Prg_Financing` | `None`, `OBF` or `Other Financing`. | None | OBF |
| `Prg_ParentProgram` | The parent program, where given. | Residential Programs | Commercial Third Party Programs |
| `Prg_Exclude_From_Budget` | CEDARS' flag: `True` when the program is left out of budget totals. | False | False |
| `Prg_Exclude_From_CE` | CEDARS' flag: `True` when the program is left out of cost-effectiveness totals. With **Leave Exclude_From_CE programs out of the portfolio summary** the summary leaves these measures out (2.2). | False | False |
| `Prg_Exclude_From_Statewide_Flag` | CEDARS' flag: `True` when the program is left out of statewide totals. | False | False |

- Example A, `Prg_ProgramSegment`: the `PRG1` row's `ProgramSegment` = **resource_acquisition**
- Example B, `Prg_ProgramSegment`: the `PRG2` row's `ProgramSegment` = **market_support**
- Example B, `Prg_Exclude_From_CE`: the `PRG2` row's `Exclude_From_CE` = **False**

### 1.15 Savings quantities

CET's savings columns (CalcSavings): what the measure saves in its first year, over its life and on average per year, gross and net (net includes net-to-gross plus market effects), after
installation and realization rates. They are quantities, not dollars, and are not priced. `KWh` includes the IOU water energy, `KWhSite` leaves it out and `KWhWater` is the water energy
alone. Every column matches CET's own to rounding on CET jobs 121219-121222 (21,370 measures), 121842 (0% market effects) and 121841 (5%).

#### `FirstYearNetKWh`, `FirstYearNetKWhSite`, `FirstYearNetKWhWater`, `FirstYearNetKW`, `FirstYearNetThm`

**First-year net electric energy (kWh: with the IOU water energy, site only, water only), demand (kW) and gas (therms) savings.**

**Formula**

```text
FirstYearNetKWh      = NumUnits * (NTGRkWh + MEB) * IRkWh * RRkWh * ( w1 * (UnitkWh1stBaseline + UnitkWhIOUWater1stBaseline) + w2 * (UnitkWh2ndBaseline + UnitkWhIOUWater2ndBaseline) )
FirstYearNetKWhSite  = NumUnits * (NTGRkWh + MEB) * IRkWh * RRkWh * ( w1 * UnitkWh1stBaseline + w2 * UnitkWh2ndBaseline )
FirstYearNetKWhWater = NumUnits * (NTGRkWh + MEB) * IRkWh * RRkWh * ( w1 * UnitkWhIOUWater1stBaseline + w2 * UnitkWhIOUWater2ndBaseline )
FirstYearNetKW       = NumUnits * (NTGRkW  + MEB) * IRkW  * RRkW  * ( w1 * UnitkW1stBaseline + w2 * UnitkW2ndBaseline )
FirstYearNetThm      = NumUnits * (NTGRTherm + MEB) * IRThm * RRThm * ( w1 * UnitTherm1stBaseline + w2 * UnitTherm2ndBaseline )

w1 = 1      if RUL_Yrs > 1           w2 = EUL_Yrs - RUL_Yrs   if 0 < RUL_Yrs < 1 and EUL_Yrs < 1
   = RUL_Yrs if 0 < RUL_Yrs < 1         = 1 - RUL_Yrs       if 0 < RUL_Yrs < 1 and EUL_Yrs >= 1
   = EUL_Yrs if RUL_Yrs = 0 and EUL_Yrs < 1                     = 0 otherwise
   = 1 otherwise
```

`IR` / `RR` are the installation and realization rates of each fuel (blank = 1); `NTGRkW` and `NTGRTherm` blank take `NTGRkWh`, then 1. `MEB` is 0.05 with market
effects on, else 0. `EUL_Yrs` is the value as filed (the pricing's 0.25-year floor is not applied here, as in CET). A measure with an RUL of exactly 1 year takes
the "otherwise" branches (w1 = 1, w2 = 0), as CET's SQL does. **Inputs:** `NumUnits`, the 1st/2nd-baseline kWh, kW, therm and IOU water fields, `NTGRkWh`, `NTGRkW`, `NTGRTherm`, the installation
and realization rates, `EUL_Yrs`, `RUL_Yrs`; the market-effects setting.

- Example A, `FirstYearNetKWh`: 100 * 0.8 * 0.95 * 0.9 * (500 + 3) = **34,405.20** (`RUL_Yrs` = 0 and `EUL_Yrs` >= 1, so w1 = 1)
- Example A, `FirstYearNetKWhSite`: 100 * 0.8 * 0.95 * 0.9 * 500 = **34,200.00**
- Example A, `FirstYearNetKW`: 100 * 0.8 * 1 * 1 * 0.2 = **16.00**
- Example A, `FirstYearNetThm`: 100 * 0.9 * 1 * 0.9 * 20 = **1,620.00**
- Example B, `FirstYearNetKWh`: 10 * (0.75 + 0.05) * 1 * 0.9 * (-4000 + -6) = **-28,843.20** (`RUL_Yrs` = 1.125 > 1, so w1 = 1 and w2 = 0)
- Example B, `FirstYearNetKWhWater`: 10 * (0.75 + 0.05) * 1 * 0.9 * -6 = **-43.20**
- Example B, `FirstYearNetKW`: no kW savings, so 0 = **0.00**
- Example B, `FirstYearNetThm`: 10 * (0.85 + 0.05) * 1 * 0.95 * 600 = **5,130.00**

#### `FirstYearGrossKWh`, `FirstYearGrossKWhSite`, `FirstYearGrossKWhWater`, `FirstYearGrossKW`, `FirstYearGrossThm`

**First-year gross savings:** the net columns above without the net-to-gross (and market-effects) factor. Unit: kWh, kW, therms.

**Formula**

```text
FirstYearGross<x> = FirstYearNet<x> without the (NTGR... + MEB) factor, e.g.
FirstYearGrossKWh = NumUnits * IRkWh * RRkWh * ( w1 * (UnitkWh1stBaseline + UnitkWhIOUWater1stBaseline) + w2 * (UnitkWh2ndBaseline + UnitkWhIOUWater2ndBaseline) )
```

**Inputs:** those of the net columns, less the NTG fields and the market-effects setting.

- Example A, `FirstYearGrossKWh`: 100 * 0.95 * 0.9 * (500 + 3) = **43,006.50**
- Example A, `FirstYearGrossKWhWater`: 100 * 0.95 * 0.9 * 3 = **256.50**
- Example B, `FirstYearGrossKWhSite`: 10 * 1 * 0.9 * -4000 = **-36,000.00**
- Example B, `FirstYearGrossThm`: 10 * 1 * 0.95 * 600 = **5,700.00**

#### `LifecycleGrossKWh`, `LifecycleGrossKWhSite`, `LifecycleGrossKWhWater`, `LifecycleGrossThm`, `LifecycleNetKWh`, `LifecycleNetKWhSite`, `LifecycleNetKWhWater`, `LifecycleNetThm`

**Savings over the measure's whole life,** undiscounted: the 1st-baseline amount for the remaining useful life and the 2nd-baseline amount for the rest of the effective useful life. Unit: kWh, therms.

**Formula**

```text
LifecycleGross<x> = Q * IR * RR * EUL_Yrs * a1                                    if RUL_Yrs = 0
                  = Q * IR * RR * ( a1 * RUL_Yrs + a2 * (EUL_Yrs - RUL_Yrs) )      if RUL_Yrs > 0
LifecycleNet<x>   = LifecycleGross<x> * (NTGR + MEB)
```

`Q` is `NumUnits`; `a1` / `a2` are the 1st- and 2nd-baseline amounts of the fuel (for `KWh` the unit kWh plus the IOU water kWh, for `KWhSite` the unit kWh, for `KWhWater` the water kWh, for `Thm`
the unit therms); `IR`, `RR` and `NTGR` are the fuel's installation rate, realization rate and net-to-gross ratio. CET has no lifecycle kW column. **Inputs:** those of the first-year columns.

- Example A, `LifecycleGrossKWh`: 100 * 0.95 * 0.9 * 2.4 * (500 + 3) = **103,215.60**
- Example A, `LifecycleNetThm`: 100 * 0.9 * 1 * 0.9 * 2.4 * 20 = **3,888.00**
- Example B, `LifecycleGrossKWh`: 10 * 1 * 0.9 * ((-4000 + -6) * 1.125 + (-2500 + -4) * (2.5 - 1.125)) = **-71,547.75**
- Example B, `LifecycleNetThm`: 10 * (0.85 + 0.05) * 1 * 0.95 * (600 * 1.125 + 400 * (2.5 - 1.125)) = **10,473.75**

#### `AnnualGrossKWh`, `AnnualGrossKWhSite`, `AnnualGrossKWhWater`, `AnnualGrossKW`, `AnnualGrossThm`, `AnnualNetKWh`, `AnnualNetKWhSite`, `AnnualNetKWhWater`, `AnnualNetKW`, `AnnualNetThm`

**Average annual savings over the measure's life:** the lifecycle amount divided by the effective useful life. Unit: kWh, kW, therms per year.

**Formula**

```text
Annual<x> = Lifecycle<x> / EUL_Yrs                (0 when EUL_Yrs = 0)
AnnualGrossKW = Q * IRkW * RRkW * UnitkW1stBaseline                                                        if RUL_Yrs = 0
              = Q * IRkW * RRkW * ( UnitkW1stBaseline * RUL_Yrs + UnitkW2ndBaseline * (EUL_Yrs - RUL_Yrs) ) / EUL_Yrs   if RUL_Yrs > 0
AnnualNetKW   = AnnualGrossKW * (NTGRkW + MEB)
```

**Inputs:** those of the lifecycle columns (and the kW fields).

- Example A, `AnnualNetKWh`: 82572.4800 / 2.4 = **34,405.20**
- Example A, `AnnualGrossKW`: 100 * 1 * 1 * 0.2 = **20.00**
- Example B, `AnnualGrossThm`: 11637.5000 / 2.5 = **4,655.00**
- Example B, `AnnualNetKWhSite`: -57150.0000 / 2.5 = **-22,860.00**

#### `WeightedSavings`

**The measure's share of its program's positive first-year gross savings,** therms converted to kWh: CET's savings-based weight. No cost test uses it (CET shares program costs by
benefits, `WeightedBenefits`). Unit: fraction, 0-1.

**Formula**

```text
WeightedSavings = S_m / SUM over the measures j of the same program (same PrgID) of S_j        if that sum is not 0
                = 1 / (the number of measures in the program)                                    if it is 0

S_m = pos(FirstYearGrossKWh) + pos(29.307111111 * FirstYearGrossThm)          (29.307111111 kWh per therm, CET's conversion)
```

**Inputs:** those of `FirstYearGrossKWh` and `FirstYearGrossThm`, and `PrgID`.

- Example A, `WeightedSavings`: (43006.5000 + 29.307111111 * 1800) / (43006.5000 + 29.307111111 * 1800) = **1.00** (the only measure of `PRG1`)
- Example B, `WeightedSavings`: (0 + 29.307111111 * 5700) / (0 + 29.307111111 * 5700) = **1.00** (pos(FirstYearGrossKWh) = 0: the measure adds load)

### 1.16 Discounted savings, lifecycle net benefits and levelized values

CET's discounted savings and the "levelized" columns built on them (CalcCost): a benefit, or the electric (gas) share of a cost, per discounted net kWh (therm) saved. All present values are
at the primary discount rate. Each levelized column is 0 when its denominator is 0. Whether the levelized columns are still needed is RAID decision D9 in the replacement BRD.

#### `DiscountedSavingsGrosskWh`, `DiscountedSavingsNetkWh`, `DiscountedSavingsGrossThm`, `DiscountedSavingsNetThm`

**Lifetime site kWh (no water) and therm savings, each year discounted at the annual rate and the whole brought back to the First Year.** Unit: discounted kWh, discounted therms.

**Formula**

```text
DiscountedSavingsGross<f> = Q * IR * RR * a1 * A(EUL_Yrs) / PVF                                                    if RUL_Yrs = 0
                          = Q * IR * RR * ( a1 * A(RUL_Yrs) + a2 * A(EUL_Yrs - RUL_Yrs) / Raf ^ RUL_Yrs ) / PVF    if RUL_Yrs > 0
                          = 0                                                                                       if a1 = 0 or EUL_Yrs = 0
DiscountedSavingsNet<f>   = DiscountedSavingsGross<f> * (NTGR + MEB)

A(n) = 1 + (1 - Raf ^ -(n - 1)) / DiscountRateAnnual          the sum of Raf ^ -t for t = 0 ... n-1 (n may be fractional)
```

`<f>` is `kWh` (`a1` / `a2` = `UnitkWh1stBaseline` / `UnitkWh2ndBaseline`, with the kWh rates and `NTGRkWh`) or `Thm` (the therm fields, rates and `NTGRTherm`). `Raf = 1 + DiscountRateAnnual(PA)`;
`PVF` as in `RebatesandIncentsPV`. `EUL_Yrs` is as filed. The net columns match CET's to 2e-6 (relative) on every measure of CET jobs 121219-121222, 121842 and 121841. CET's *gross* columns
differ from its own net ones on early-replacement measures (its gross kWh counts one 2nd-baseline year too many and its gross therms leave the 2nd baseline undiscounted); ReCET's gross is
the net formula without NTG, so the two agree whenever NTG + MEB = 1, and it equals CET's on every single-baseline measure. **Inputs:** `NumUnits`, the kWh and therm fields, the installation
and realization rates, `NTGRkWh`, `NTGRTherm`, `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`, `IOU_AC_Territory`; First Year, ACC version (discount rates), the market-effects setting.

- Example A, `DiscountedSavingsGrosskWh`: 100 * 0.95 * 0.9 * 500 * (1 + (1 - 1.0766^-1.4) / 0.0766) / 1.01915^2 = **93,907.91** (`RUL_Yrs` = 0, `EUL_Yrs` = 2.4)
- Example A, `DiscountedSavingsNetkWh`: 0.8 * 93907.9082 = **75,126.33**
- Example A, `DiscountedSavingsGrossThm`: 100 * 1 * 0.9 * 20 * (1 + (1 - 1.0766^-1.4) / 0.0766) / 1.01915^2 = **3,954.02**
- Example B, `DiscountedSavingsGrosskWh`: 10 * 1 * 0.9 * (-4000 * (1 + (1 - 1.0766^-0.125) / 0.0766) + -2500 * (1 + (1 - 1.0766^-0.375) / 0.0766) / 1.0766^1.125) / 1.01915 = **-67,117.49** (A(1.125) and A(2.5 - 1.125))
- Example B, `DiscountedSavingsNetThm`: (0.85 + 0.05) * 10917.8371 = **9,826.05**

#### `TRCLifecycleNetBen`, `PACLifecycleNetBen`

**Lifetime net benefit under the TRC and PAC tests,** as CET defines it: the electric and gas benefits less the test's cost (water energy and other benefits are not included). Unit: $ (PV).

**Formula**

```text
TRCLifecycleNetBen = ElecBen + GasBen - TRCCost
PACLifecycleNetBen = ElecBen + GasBen - PACCost
```

**Inputs:** those of `ElecBen`, `GasBen`, `TRCCost` and `PACCost`.

- Example A, `TRCLifecycleNetBen`: 5841.4973 + 3454.0256 - 20916.4229 = **-11,620.90**
- Example A, `PACLifecycleNetBen`: 5841.4973 + 3454.0256 - 8785.4865 = **510.04**
- Example B, `TRCLifecycleNetBen`: 0 + 6990.5647 - 60271.7921 = **-53,281.23**

#### `LevBenElec`, `LevBenGas`

**Benefit per discounted unit saved:** the electric benefit per discounted net kWh and the gas benefit per discounted net therm. Unit: $ per kWh; $ per therm.

**Formula**

```text
LevBenElec = ratio( ElecBen , DiscountedSavingsNetkWh )
LevBenGas  = ratio( GasBen , DiscountedSavingsNetThm )
```

CET's exported `LevBenElec` is instead the **SCT Base** electric benefit per discounted kWh (`ElecBen_SB / DiscountedSavingsNetkWh`, on every measure of CET jobs 121219-121222): CalcCost lists the
three `LevBenElec` columns in a different order from the values it selects, so the TRC value lands under another name. ReCET gives the TRC value its name says (CALCULATIONS.md C17).
`LevBenGas` matches CET's. **Inputs:** those of `ElecBen`, `GasBen` and the discounted savings.

- Example A, `LevBenElec`: 5841.4973 / 75126.3265 = **0.077756**
- Example A, `LevBenGas`: 3454.0256 / 3558.6155 = **0.970609**
- Example B, `LevBenElec`: 0 / -53693.9950 = **0.000000**
- Example B, `LevBenGas`: 6990.5647 / 9826.0534 = **0.711432**

#### `LevTRCCost`, `LevTRCCostNoAdmin`, `LevPACCost`, `LevPACCostNoAdmin`, `LevRIMCost`

**The electric share of a test's cost per discounted net kWh.** Unit: $ per kWh.

**Formula**

```text
Lev<T> = ratio( <T> * WeightedElecAlloc , DiscountedSavingsNetkWh )          <T> = TRCCost, TRCCostNoAdmin, PACCost, PACCostNoAdmin or RIMCost
```

Computed from CET's own cost, allocation and discounted-savings columns, each reproduces CET's on every measure of CET jobs 121219-121222. **Inputs:** those of the cost, `WeightedElecAlloc` and
`DiscountedSavingsNetkWh`.

- Example A, `LevTRCCost`: 20916.4229 * 0.615790 / 75126.3265 = **0.171446**
- Example A, `LevPACCostNoAdmin`: 5856.6364 * 0.615790 / 75126.3265 = **0.048005**
- Example A, `LevRIMCost`: 49925.6844 * 0.615790 / 75126.3265 = **0.409227**
- Example B, `LevTRCCost`: 60271.7921 * 0 / -53693.9950 = **0.000000**

#### `LevNetBenTRCElec`, `LevNetBenTRCGas`, `LevNetBenTRCElecNoAdmin`, `LevNetBenTRCGasNoAdmin`, `LevNetBenPACElec`, `LevNetBenPACGas`, `LevNetBenPACElecNoAdmin`, `LevNetBenPACGasNoAdmin`, `LevNetBenRIMElec`, `LevNetBenRIMGas`

**Net benefit per discounted unit saved,** by fuel: the fuel's benefit less its share of the test's cost (`WeightedElecAlloc` to electricity, the rest to gas). Unit: $ per kWh; $ per therm.

**Formula**

```text
LevNetBen<T>Elec<v> = ratio( ElecBen - <T>Cost<v> * WeightedElecAlloc ,       DiscountedSavingsNetkWh )
LevNetBen<T>Gas<v>  = ratio( GasBen  - <T>Cost<v> * (1 - WeightedElecAlloc) , DiscountedSavingsNetThm )
<T> = TRC, PAC (with <v> = "" or NoAdmin) or RIM (<v> = "")
```

Computed from CET's own columns, each reproduces CET's on every measure of CET jobs 121219-121222. **Inputs:** those of the benefits, costs, `WeightedElecAlloc` and the discounted savings.

- Example A, `LevNetBenTRCElec`: (5841.4973 - 20916.4229 * 0.615790) / 75126.3265 = **-0.093690**
- Example A, `LevNetBenTRCGas`: (3454.0256 - 20916.4229 * (1 - 0.615790)) / 3558.6155 = **-1.287659**
- Example A, `LevNetBenPACGasNoAdmin`: (3454.0256 - 5856.6364 * (1 - 0.615790)) / 3558.6155 = **0.338290**
- Example B, `LevNetBenTRCGas`: (6990.5647 - 60271.7921 * (1 - 0)) / 9826.0534 = **-5.422444**
- Example B, `LevNetBenRIMGas`: (6990.5647 - 32499.6332 * (1 - 0)) / 9826.0534 = **-2.596065**

---------------------------------------------------------------------------------------------------------------------------------

## 2. Other files in a run folder

A run creates `<output folder>/<run name>_acc<version>/` (a timestamp `run_YYYYMMDD_HHMMSS` when no name is given). A two-version run creates `<output folder>/<run name>/` holding `run_acc<A>/`,
`run_acc<B>/` and the comparison files. Every table is written as CSV (UTF-8 with BOM) or Parquet according to "Output format".

### 2.1 `measure_level_acc<version>`

The table of section 1.

### 2.2 `portfolio_summary_acc<version>`

One row for the whole job (`group` = `portfolio`), then one row per `IOU_AC_Territory` (`group` = `IOU territory (PA)`, from `poc_PA`), then one per credited IOU (`group` = `credited IOU`, from `CreditedIOU`; left out when there is no `ProgramCost.csv`),
then one per program segment (`group` = `program segment`, from `Prg_ProgramSegment`; `(blank)` for programs with no segment or not in the program metadata).
Every number is computed over the measures of the row's group.

With **Leave Exclude_From_CE programs out of the portfolio summary** (`apply_exclude_from_ce`), the measures whose `Prg_Exclude_From_CE` is `True` are left out of every one
of those rows and summed in one last row (`group` = `excluded`, `value` = `Exclude_From_CE programs (left out of the rows above)`), so nothing disappears unseen. Without
it they are counted like every other measure.

| Field | Meaning and unit | Formula |
|---|---|---|
| `group` | The kind of row. Text. | `portfolio`, `IOU territory (PA)`, `credited IOU`, `program segment` or `excluded` |
| `value` | The group's value. Text. | `all measures`, the IOU (`PGE`, `SCE`, `SDGE`, `SCG`; `SW` for unallocated statewide programs in the credited-IOU rows), the segment (for example `resource_acquisition`, `(blank)`), or the excluded row's label |
| `measures` | Number of measures in the group. Count. | `COUNT of the measure-level rows in the group` |
| `ElecBen`, `GasBen`, `WaterEnergyBen`, `OtherBen`, `ElecSupplyCost`, `GasSupplyCost`, `WaterEnergyCost`, `TotalSystemBenefit`, `TRCCost`, `PACCost`, `RIMCost`, `SCBCost`, `SCHCost`, `TRCCostNoAdmin`, `PACCostNoAdmin`, `WeightedProgramCost`, `BillReducElec`, `BillReducGas`, `PCTBen`, `PCTCost` | The group's total of the measure-level column of the same name (a column with no values, for example `SCBCost` when SCB/SCH are off, is omitted). $ (`WeightedProgramCost` nominal, the rest PV). | `SUM over the measures of the group of the measure-level column of the same name` |
| `NetElecCO2`, `NetGasCO2` | The group's first-year net CO2. Metric tons. | `SUM over the group of NetElecCO2` (and of `NetGasCO2`) |
| `TRC_ratio` | Portfolio Total Resource Cost ratio, on CET's receipt definition: the summed benefits (supply costs are inside the cost, not netted from the numerator) over the summed cost. Ratio. | `( SUM(ElecBen) + SUM(GasBen) + SUM(WaterEnergyBen) + SUM(OtherBen) + SUM(TaxCredits) ) / SUM(TRCCost)` |
| `TRC_NoAdmin_ratio` | The same over the cost without program costs. Ratio. | `( SUM(ElecBen) + SUM(GasBen) + SUM(WaterEnergyBen) + SUM(OtherBen) + SUM(TaxCredits) ) / SUM(TRCCostNoAdmin)` |
| `PAC_ratio` | Portfolio Program Administrator Cost ratio. Ratio. | `( SUM(ElecBen) + SUM(GasBen) + SUM(WaterEnergyBen) + SUM(OtherBen) ) / SUM(PACCost)` |
| `PAC_NoAdmin_ratio` | The same over the cost without program costs. Ratio. | `( SUM(ElecBen) + SUM(GasBen) + SUM(WaterEnergyBen) + SUM(OtherBen) ) / SUM(PACCostNoAdmin)` |
| `RIM_ratio` | Portfolio Ratepayer Impact Measure ratio. Ratio. | `( SUM(ElecBen) + SUM(GasBen) + SUM(WaterEnergyBen) + SUM(OtherBen) + SUM(BillIncrElec) + SUM(BillIncrGas) ) / SUM(RIMCost)` |
| `SCB_ratio` | Portfolio Societal Cost ratio, Base (omitted when SCB/SCH are off). Ratio. | `( SUM(ElecBen_SB) + SUM(GasBen_SB) + SUM(WaterEnergyBen_SB) + SUM(OtherBen) ) / SUM(SCBCost)` |
| `SCH_ratio` | Portfolio Societal Cost ratio, High (omitted when SCB/SCH are off). Ratio. | `( SUM(ElecBen_SH) + SUM(GasBen_SH) + SUM(WaterEnergyBen_SH) + SUM(OtherBen) ) / SUM(SCHCost)` |
| `PCT_ratio` | Portfolio Participant Cost Test ratio (gross). Ratio. | `SUM(PCTBen) / SUM(PCTCost)` |

Each `SUM` is over the group's measures. A ratio is omitted when a column it needs is blank or its denominator is 0. **Inputs:** the measure-level table only.

- Example A (a portfolio of just that measure), `measures`: `COUNT` of one row = **1**
- Example A, `TRC_ratio`: (5841.4973 + 3454.0256 + 30.6679 + 160.00 + 400.00) / 20916.4229 = **0.4727**
- Example A, `PAC_ratio`: (5841.4973 + 3454.0256 + 30.6679 + 160.00) / 8785.4865 = **1.0798**
- Example B, `RIM_ratio`: (0.0000 + 6990.5647 + 0.0000 + 320.00 + 20764.8592 + 0.00) / 32499.6332 = **0.8639**
- Example B, `SCB_ratio`: (0.00 + 8827.1057 + 0.00 + 320.00) / 60501.5955 = **0.1512**
- Example B, `TotalSystemBenefit`: `SUM` of one row = **3,012.77**

### 2.3 `components_summary_acc<version>` (components mode)

One row per component of each fuel, then a check row per fuel. The measure-level `ElecValue_<component>` and `GasValue_<component>` columns (1.13) are the inputs.

| Field | Meaning and unit | Formula |
|---|---|---|
| `fuel` | `Electric` or `Gas`. Text. | |
| `component` | The component's name (for example `energy`, `transmission`), or `(check) total minus sum of components`. Text. | |
| `portfolio_value_usd` | The portfolio's present value of that component. The check row is the fuel's total minus the sum of its components, which is 0 when the components add up. $ (PV). | component row: `SUM over all measures of ElecValue_<component>` (or `GasValue_<component>`); check row: `SUM(ElecBen) - SUM(ElecSupplyCost) - SUM over components c of portfolio_value_usd(c)` (gas: `SUM(GasBen) - SUM(GasSupplyCost) - ...`) |
| `share_of_fuel_total` | The component's share of its fuel's total (blank on the check row). Fraction. | `portfolio_value_usd(c) / SUM over the fuel's components of portfolio_value_usd` |

- Example A (a one-measure portfolio), `ElecValue_energy`: (the energy component is 70 of the 80 dollars per MWh of the TRC row) 70 / 80 * 5841.4973 = **5,111.31**
- Example A, `ElecValue_transmission`: 5 / 80 * 5841.4973 = **365.09**
- Example A, `ElecValue_distribution`: 5 / 80 * 5841.4973 = **365.09**
- Example A, `GasValue_market`: (the market component is 0.60 of the 1.00 dollars per therm of the TRC row) 0.60 / 1.00 * 3454.0256 = **2,072.42**
- Example A, `GasValue_co2`: 0.10 / 1.00 * 3454.0256 = **345.40**

### 2.4 `portfolio_8760_acc<version>` (combined 8760 mode)

The portfolio's net electric savings hour by hour, for each requested calendar year `Y` (the "Year(s)" setting, `profile_years`; default the First Year).

| Field | Meaning and unit | Formula |
|---|---|---|
| `hour_of_year` | The ACC hour index, 1 = 1 January 00:00. 1-8760. | `1 ... 8760` |
| `datetime` | That index laid on the ACC's calendar `A`: 2018 for the 2024/2026 ACCs, 2020 for the 2021/2022 ACCs (every forecast year of an ACC follows one calendar's weekdays, and the shapes are aligned to it; a 2020 calendar includes 29 February and ends on 30 December). Timestamp. | `A-01-01 00:00 plus (hour_of_year - 1) hours` |
| `net_kwh_<Y>` | The portfolio's net kWh in that hour of calendar year `Y`, all IOUs. Positive = savings, negative = added load. kWh. | `SUM over measures m of weight_m(Y) * shape_m,Y(i)`, `i` = `hour_of_year` |
| `net_kwh_<Y>_<PA>` | The same for the measures of one IOU territory `PA`. kWh. | the same sum over the measures with `IOU_AC_Territory` = `PA` |

**Where**

```text
start_m = Y0 + (q0 - 1) / 4                        the claim quarter as a fraction of a year
end_m   = start_m + max(EUL_Yrs, 0.25)
overlap(A, B) = the length in years of the overlap of the two intervals A and B

if 0 < RUL_Yrs < EUL_Yrs:
   weight_m(Y) = overlap( [Y, Y+1), [start_m, start_m + RUL) ) * poc_shape_scale
                 + overlap( [Y, Y+1), [start_m + RUL, end_m) ) * poc_shape_scale_2nd
otherwise:
   weight_m(Y) = overlap( [Y, Y+1), [start_m, end_m) ) * poc_shape_scale

shape_m,Y(i) = the measure's 8,760 shape for hour i, rotated onto the ACC's weekdays (0.6); the same in every year Y
```

`poc_shape_scale` and `poc_shape_scale_2nd` are the net kWh with installation and realization rates and without market effects (market effects are an accounting adder, not physical savings, so they are
not in this profile). A measure with no 8,760 profile is left out and listed in 2.5. **Inputs:** the fields of `poc_shape_scale` (0.5), `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`, `IOU_AC_Territory`,
the electric-profile fields of `ElecBen`; the "Year(s)" setting.

- Example A, `net_kwh_2028`: overlap([2028, 2029), [2028.5, 2030.9)) = 0.5, so 0.5 * 34200 / 8760 = **1.9521** kWh in every hour (a flat shape)
- Example A, `net_kwh_2029`: overlap([2029, 2030), [2028.5, 2030.9)) = 1, so 1 * 34200 / 8760 = **3.9041** kWh in every hour
- Example A, `net_kwh_2030`: overlap([2030, 2031), [2028.5, 2030.9)) = 0.9, so 0.9 * 34200 / 8760 = **3.5137** kWh in every hour

### 2.5 `portfolio_8760_unshaped_acc<version>` (only when some measures have no shape)

The measures left out of the combined profile because they have no 8,760 profile.

| Field | Meaning and unit | Formula |
|---|---|---|
| `measure_id` | The `CEInputID`. Text. | `CEInputID` |
| `PA`, `shape_key`, `target_sector`, `climate_zone` | The measure's key fields. Text. | `IOU_AC_Territory`, `AC_Elec_Impact_Profile`, `AC_Elec_Target_Sector`, `AC_Climate_Zone` |
| `kwh_<Y>` | The annual-equivalent net kWh the measure delivers in year `Y`: its share of the "unshaped" total. kWh. | `weight_m(Y)` of 2.4 |

### 2.6 `cet_comparison_measures_acc<version>` and `cet_comparison_summary_acc<version>` (with a CET outputs file)

The tool's measure-level table against a real CET run's outputs file, matched on `CEInputID` (= CET's `CET_ID`); only measures in both are compared. `c` is one of `TotalSystemBenefit`, `ElecBen`, `GasBen`, `WaterEnergyBen`,
`WaterEnergyBen_SB`, `WaterEnergyBen_SH`, `WaterEnergyBenGross`, `OtherBen`, `ElecSupplyCost`, `GasSupplyCost`, `WaterEnergyCost`, `TRCCost`, `PACCost`, `RIMCost`, `SCBCost`, `SCHCost`, `TRCRatio`, `PACRatio`, `RIMRatio`,
`NetElecCO2`, `NetGasCO2`, `BillReducElec`, `BillReducGas` (those present in both).

| Field | Meaning and unit | Formula |
|---|---|---|
| `CEInputID`, `PrgID`, `MeasDescription` | The measure (per-measure file). Text. | copied from the measure-level table |
| `<c>_poc`, `<c>_cet` | The tool's and CET's value of column `c`. As `c`. | the measure-level column `c`; CET's column `c` |
| `<c>_diff` | The tool minus CET. As `c`. | `<c>_poc - <c>_cet` |
| `<c>_ratio` | The tool over CET (blank where CET is 0). Ratio. | `<c>_poc / <c>_cet` |
| `metric`, `POC`, `CET`, `POC/CET` | The summary file: each non-ratio column's portfolio total in both and their ratio (blank where CET's total is 0). $ or metric tons; ratio. | `metric` = `c`; `POC` = `SUM(<c>_poc)`; `CET` = `SUM(<c>_cet)`; `POC/CET` = `POC / CET` |

### 2.7 `comparison_acc<A>_vs_acc<B>` and `comparison_summary_acc<A>_vs_acc<B>` (two-version run)

The same job priced under two ACC versions `A` and `B`, matched on `CEInputID` (a measure in only one run is kept, with the other side blank). `c` is one of `ElecBen`, `GasBen`, `WaterEnergyBen`, `OtherBen`,
`TotalSystemBenefit`, `ElecSupplyCost`, `WaterEnergyCost`, `TRCCost`, `PACCost`, `RIMCost`, `SCBCost`, `SCHCost`, `TRCRatio`, `PACRatio`, `RIMRatio`, `SCBRatio`, `SCHRatio`, `NetElecCO2`, `NetGasCO2`, `BillReducElec`,
`BillReducGas` (those present in both).

| Field | Meaning and unit | Formula |
|---|---|---|
| `CEInputID`, `PrgID`, `MeasDescription`, `IOU_AC_Territory` | The measure. Text. | copied from the measure-level table |
| `<c>_acc<A>`, `<c>_acc<B>` | Column `c` under each ACC version. As `c`. | the measure-level column `c` of each run |
| `<c>_delta` | The change from A to B. As `c`. | `<c>_acc<B> - <c>_acc<A>` |
| `<c>_pct` | The relative change (blank where A is 0). Fraction. | `( <c>_acc<B> - <c>_acc<A> ) / abs( <c>_acc<A> )` |
| `ElecPricingStatus_acc<A>`, `ElecPricingStatus_acc<B>`, `GasPricingStatus_acc<A>`, `GasPricingStatus_acc<B>` | The pricing status under each version (1.3). Text. | the measure-level columns of each run |
| `metric`, `acc<A>`, `acc<B>`, `delta`, `pct` | The summary file: each non-ratio column's portfolio total under both versions. $ or metric tons; fraction. | `metric` = `c`; `acc<A>` = `SUM(<c>_acc<A>)`; `acc<B>` = `SUM(<c>_acc<B>)`; `delta` = `acc<B> - acc<A>`; `pct` = `delta / abs(acc<A>)` (blank where `acc<A>` is 0) |

### 2.7b `_results/measure_level_acc<version>.parquet`

When the output format is `csv` or `csv.zip`, the measure-level table is written as parquet first and the CSV is made from it; the parquet is kept in the run
folder's `_results` folder, and the Results tables (window and web app) read it rather than the CSV. Same columns and values as the measure-level file. The web
app does not list it for download.

### 2.8 `preflight.txt`

The input check that ran before pricing: a header (`PREFLIGHT: no blocking problems` or the number of blocking problems), the job's statistics (measure counts, claim years, First Year, how many measures use each electric shape
source, water and gas measure counts, ACC year ranges), then every finding as `ERROR` / `WARNING` / `INFO`, its code, count and examples. Finding codes: `file_missing`, `no_delimiter`, `not_pipe_delimited` (info: the file is comma- or tab-delimited and was read as such), `missing_columns`,
`missing_optional_column`, `blank_id`, `duplicate_id`, `bad_claim_year_quarter`, `bad_iou`, `non_numeric`, `blank_required_value`, `blank_second_baseline`, `outside_cet_range`, `rul_not_below_eul`, `program_alignment`,
`no_program_costs`, `statewide_program_costs`, `bad_acc_version`, `acc_version_file_missing`, `no_electric_profile`, `no_electric_profile_2nd`, `loose_profile_match`, `kwh_per_unit_profile` (info: priced on an already-scaled kWh-per-unit profile), `zero_net_profile_water`, `no_profile_library`, `no_water_profile`, `no_gas_profile`, `bad_gas_sector`, `unknown_settings_version`, `before_acc_horizon`,
`beyond_acc_horizon`, `acc_unreadable`, `program_costs_unreadable` (a job checked in parts could not read `ProgramCost.csv`), and from the program metadata `program_not_in_metadata`, `claim_outside_program_years` (claim year before `StartYear` or after `EndYear`),
`program_closed` (status `closed` or `closure_pending`), `program_excluded_from_ce` and `program_pa_differs` (the `ProgramCost.csv` `PA` differs from the list's `PA`).
Errors stop the run; warnings and info do not.

### 2.9 `run_settings.json`

Everything needed to reproduce the run.

| Key | Meaning |
|---|---|
| `package_version`, `finished`, `seconds` | The `recet` version, the completion time, and the run time in seconds. |
| `config` | Every run setting (the keys of INPUT_FIELDS.md section 3), exactly as used. |
| `acc` | The resolved ACC: `key`, `label`, `settings_acc_version` (the ACC whose run parameters and E3 tables were used), the electric and gas file paths with their `_source` (`built-in`, or that a supplied file replaced it) and `electric_size`, and any `notes` (for example that a new label borrowed a built-in file). |
| `reference_dir` | The reference data folder used, how it was chosen, and the status (found / size) of each large file. |
| `supplied_files` | Each supplied profile / ACC file with its path and size. |
| `inputs` | The measure and program-cost paths, the number of measures priced, and the First Year used. |
| `totals` | The portfolio totals (the first row of the portfolio summary). |

Also written into the run folder: `_prepared_inputs/` (a Parquet conversion of any supplied ACC that was not already Parquet) and, from the GUI, `_gui_job.json` in the output folder (the settings the GUI launched the run with).

---------------------------------------------------------------------------------------------------------------------------------

## 3. The two worked examples

Both are one-measure jobs on flat avoided costs and flat 8,760 load shapes, so every step can be checked with a calculator. Both are re-run by the test-suite (`tests/doc_examples.py`,
`tests/test_output_docs.py`), which compares every number below and in the `Example` lines of sections 1 and 2 with what the tool gives, and checks the arithmetic written on each line.

Common to both: PG&E, climate zone 13, the 2026 ACC (run parameters: `DiscountRateQtr` 0.01915, `DiscountRateAnnual` 0.0766, societal 0.012575 and 0.0503), First Year 2028, every ACC hour of a
year priced alike. Electric avoided costs, in dollars per MWh: `TRC` = energy 70 + transmission 5 + distribution 5 = 80 (so `AC` = 0.080 per kWh and `Gen` = 0.070 per kWh); `SCT Base` 88 (Gen 78); `SCT High` 96 (Gen 86);
the ACC's `emission_rate_tonnes_per_kwh` is 0.0002 in every hour. Gas avoided costs, in dollars per therm and the same in every month: `td` 0.20, `nox` 0.05, `upstream_methane` 0.03, `air_quality_adder` 0.02,
`behind_meter_methane` 0, `co2` 0.10 (`TRC`), 0.30 (`SCT` Base), 0.50 (`SCT` High), and `market` 0.60 for the Residential class and 0.50 for the Commercial class.

### 3.1 Example A: inputs

A residential efficiency measure claimed in 2028Q3: a single baseline, a fractional life, no market effects, not fuel substitution. Non-blank `Measure.csv` fields:

| Field | Value | Field | Value |
|---|---|---|---|
| `CEInputID` | `W1` | `PrgID` | `PRG1` |
| `ClaimYearQuarter` | 2028Q3 | `IOU_AC_Territory` | PGE |
| `AC_Climate_Zone` | 13 | `AC_Elec_Target_Sector` | Residential |
| `AC_Elec_Impact_Profile` | `W_SHAPE` (a flat profile, 1/8760 per hour) | `AC_Gas_Impact_Profile` | `Annual` (1/12 per month) |
| `AC_Gas_Target_Sector` | Residential | `NumUnits` | 100 |
| `EUL_Yrs` / `RUL_Yrs` | 2.4 / 0 | `UnitkWh1stBaseline` | 500 |
| `UnitTherm1stBaseline` | 20 | `UnitkWhIOUWater1stBaseline` | 3 |
| `NTGRkWh` / `NTGRkW` | 0.8 / 0.8 | `NTGRTherm` / `NTGRCost` | 0.9 / 0.9 |
| `InstallationRatekWh` / `RealizationRatekWh` | 0.95 / 0.9 | `InstallationRateTherm` / `RealizationRateTherm` | 1 / 0.9 |
| `UnitMeaCost1stBaseline` | 200 | `UnitIncentiveToOthers` / `UnitEndUserRebate` | 30 / 20 |
| `UnitDirectInstallLab` / `UnitDirectInstallMat` | 5 / 5 | `UnitRefrigCosts` / `UnitMiscBens` / `UnitTaxCredits` | 1 / 2 / 5 |

Program `PRG1` (`ProgramCost.csv`): 2028 row: `AdminCostsOverheadAndGA` 1500 and `MarketingOutreach` 500; 2029 row: `EMV` 1000.

### 3.2 Example A: quarter by quarter (TRC prices, primary discount rate)

`Elec(k)`, `Gas(k)` and `Wat(k)` are the phased annual quantities of 0.5 (no market effects, single baseline, so they are constant); each `V` column is one term of the sum in 0.7:
`w(k) * DF(k) * quantity * U(k)`, with `U_e(k)` = 0.080 * hours / 8760, `U_g(k)` = 1.00 * months / 12 and `U_w(k)` = 0.070 * hours / 8760. Quarter numbering and hours: 0.2.

<!-- oa-quarters-A:start -->
| k | Quarter | Hours | w(k) | DF(k) | Baseline | Elec(k) | V_elec term | Gas(k) | V_gas term | Wat(k) | V_water term |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 2028Q3 | 2208 | 1 | 0.962773 | 1st | 34,200.0 | 663.95 | 1,620.0 | 389.92 | 205.20 | 3.486 |
| 1 | 2028Q4 | 2208 | 1 | 0.944682 | 1st | 34,200.0 | 651.47 | 1,620.0 | 382.60 | 205.20 | 3.420 |
| 2 | 2029Q1 | 2160 | 1 | 0.926931 | 1st | 34,200.0 | 625.34 | 1,620.0 | 375.41 | 205.20 | 3.283 |
| 3 | 2029Q2 | 2184 | 1 | 0.909514 | 1st | 34,200.0 | 620.40 | 1,620.0 | 368.35 | 205.20 | 3.257 |
| 4 | 2029Q3 | 2208 | 1 | 0.892424 | 1st | 34,200.0 | 615.44 | 1,620.0 | 361.43 | 205.20 | 3.231 |
| 5 | 2029Q4 | 2208 | 1 | 0.875655 | 1st | 34,200.0 | 603.87 | 1,620.0 | 354.64 | 205.20 | 3.170 |
| 6 | 2030Q1 | 2160 | 1 | 0.859202 | 1st | 34,200.0 | 579.64 | 1,620.0 | 347.98 | 205.20 | 3.043 |
| 7 | 2030Q2 | 2184 | 1 | 0.843057 | 1st | 34,200.0 | 575.07 | 1,620.0 | 341.44 | 205.20 | 3.019 |
| 8 | 2030Q3 | 2208 | 1 | 0.827216 | 1st | 34,200.0 | 570.47 | 1,620.0 | 335.02 | 205.20 | 2.995 |
| 9 | 2030Q4 | 2208 | 0.6 | 0.811672 | 1st | 34,200.0 | 335.85 | 1,620.0 | 197.24 | 205.20 | 1.763 |
<!-- oa-quarters-A:end -->

Sums: `V_elec` = 5,841.50, `V_gas` = 3,454.03, `V_water` = 30.67. For example, row 0: 34200 x 0.080 x 2208 / 8760 x 0.962773 = 663.95.

### 3.3 Example B: inputs

A commercial gas-to-electric heat pump claimed in 2028Q2: fuel substitution (`FuelID` `FuelSub-ToElec`, so it adds electric load and water use and saves gas), a second baseline that starts part-way through a quarter,
a winter-only gas shape, 5% market effects on benefits and on costs (`MEB` = `MEC` = 0.05), and electric NOx / PM10 and gas NOx rows. Non-blank `Measure.csv` fields:

| Field | Value | Field | Value |
|---|---|---|---|
| `CEInputID` | `W1` | `PrgID` | `PRG2` |
| `ClaimYearQuarter` | 2028Q2 | `IOU_AC_Territory` | PGE |
| `AC_Climate_Zone` | 13 | `AC_Elec_Target_Sector` | Commercial |
| `AC_Elec_Impact_Profile` | `3 = Commercial HVAC` (a flat profile) | `AC_Gas_Impact_Profile` | `Winter Only` (1/6 in Jan, Feb, Mar, Oct, Nov, Dec) |
| `AC_Gas_Target_Sector` | Commercial | `NumUnits` | 10 |
| `EUL_Yrs` / `RUL_Yrs` | 2.5 / 1.125 | `FuelID` / `MeasImpactType` | `FuelSub-ToElec` / `Deem-FuelSub` |
| `UnitkWh1stBaseline` / `UnitkWh2ndBaseline` | -4000 / -2500 | `UnitTherm1stBaseline` / `UnitTherm2ndBaseline` | 600 / 400 |
| `UnitkWhIOUWater1stBaseline` / `...2ndBaseline` | -6 / -4 | `CombustionType` | `Small Boilers (<100 MMBtu/hr Heat Input):Uncontrolled` |
| `NTGRkWh` / `NTGRkW` | 0.75 / 0.75 | `NTGRTherm` / `NTGRCost` | 0.85 / 0.8 |
| `InstallationRatekWh` / `RealizationRatekWh` | 1 / 0.9 | `InstallationRateTherm` / `RealizationRateTherm` | 1 / 0.95 |
| `UnitMeaCost1stBaseline` / `UnitMeaCost2ndBaseline` | 9000 / 6000 | `MeasInflation` | 0.02 |
| `UnitIncentiveToOthers` / `UnitEndUserRebate` | 800 / 400 | `UnitDirectInstallLab` / `UnitDirectInstallMat` | 100 / 50 |
| `UnitGasInfraBens` / `UnitRefrigBens` / `UnitMiscBens` | 20 / 8 / 12 | `UnitRefrigCosts` / `UnitMiscCosts` / `UnitTaxCredits` | 10 / 5 / 100 |

Program `PRG2`: 2028 row: `EMV` 900 and `DIActivity` 300; 2029 row: `MarketingOutreach` 600. The Emissions table gives PG&E commercial zone 13 `3 = Commercial HVAC` a `NOx` and a `PM10` rate of
0.00004643 pounds per kWh; the CombustionType table gives `Small Boilers (<100 MMBtu/hr Heat Input):Uncontrolled` a NOx factor of 0.0098 pounds per therm.

### 3.4 Example B: quarter by quarter (TRC prices, primary discount rate)

Same columns as 3.2, plus `Baseline`: which baseline the quarter is on (0.4; `RUL_Yrs` 1.125 gives `RQ` = 4.5, so quarter 4 is the half-and-half blend). The quantities include the market-effects scaling
(0.5), for example `Elec(0)` = -27000 x (0.75 + 0.05) / 0.75 = -28800. The gas price `U_g(k)` is 0.90 (the Commercial `TRC` total) times the share of the year that the quarter's months carry: 0 for Q2 and Q3, and 3 x 1/6 = 0.5 for Q1 and Q4.

<!-- oa-quarters-B:start -->
| k | Quarter | Hours | w(k) | DF(k) | Baseline | Elec(k) | V_elec term | Gas(k) | V_gas term | Wat(k) | V_water term |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 2028Q2 | 2184 | 1 | 0.981210 | 1st | -28,800.0 | -563.63 | 5,130.0 | 0.00 | -43.20 | -0.740 |
| 1 | 2028Q3 | 2208 | 1 | 0.962773 | 1st | -28,800.0 | -559.12 | 5,130.0 | 0.00 | -43.20 | -0.734 |
| 2 | 2028Q4 | 2208 | 1 | 0.944682 | 1st | -28,800.0 | -548.61 | 5,130.0 | 2,180.80 | -43.20 | -0.720 |
| 3 | 2029Q1 | 2160 | 1 | 0.926931 | 1st | -28,800.0 | -526.60 | 5,130.0 | 2,139.82 | -43.20 | -0.691 |
| 4 | 2029Q2 | 2184 | 1 | 0.909514 | blend 0.500 | -23,400.0 | -424.49 | 4,275.0 | 0.00 | -36.00 | -0.571 |
| 5 | 2029Q3 | 2208 | 1 | 0.892424 | 2nd | -18,000.0 | -323.91 | 3,420.0 | 0.00 | -28.80 | -0.453 |
| 6 | 2029Q4 | 2208 | 1 | 0.875655 | 2nd | -18,000.0 | -317.83 | 3,420.0 | 1,347.63 | -28.80 | -0.445 |
| 7 | 2030Q1 | 2160 | 1 | 0.859202 | 2nd | -18,000.0 | -305.08 | 3,420.0 | 1,322.31 | -28.80 | -0.427 |
| 8 | 2030Q2 | 2184 | 1 | 0.843057 | 2nd | -18,000.0 | -302.67 | 3,420.0 | 0.00 | -28.80 | -0.424 |
| 9 | 2030Q3 | 2208 | 1 | 0.827216 | 2nd | -18,000.0 | -300.25 | 3,420.0 | 0.00 | -28.80 | -0.420 |
<!-- oa-quarters-B:end -->

Sums: `V_elec` = -4,172.17, `V_gas` = 6,990.56, `V_water` = -5.63.

### 3.5 Every output column, both examples

The value of every measure-level column that is not a copied input, for both examples (6 significant digits; `(blank)` = empty). It is the complete set of intermediate `poc_*` fields, benefits, costs, ratios and emissions the entries of section 1
refer to.

<!-- oa-all:start -->
| Field | Example A | Example B |
|---|---|---|
| `POC_ACC` | 2026 ACC (Avoided Cost Version 2028) | 2026 ACC (Avoided Cost Version 2028) |
| `poc_subset_key` | CZ13_Res | CZ13_Com |
| `poc_shape_key` | W_SHAPE | 3 = Commercial HVAC |
| `poc_shape_key_2nd` | W_SHAPE | 3 = Commercial HVAC |
| `poc_shape_key_water` | W_SHAPE | 3 = Commercial HVAC |
| `poc_annual_net_kwh` | 40000 | -30000 |
| `poc_annual_net_kwh_1st` | 40000 | -30000 |
| `poc_annual_net_kwh_2nd` | 0 | -18750 |
| `poc_annual_net_kw` | 16 | 0 |
| `poc_annual_net_kw_1st` | 16 | 0 |
| `poc_annual_net_kw_2nd` | 0 | 0 |
| `poc_eul_years` | 2 | 2 |
| `poc_eul_years_exact` | 2.4 | 2.5 |
| `poc_rul_years_exact` | 0 | 1.125 |
| `poc_NTGRkWh` | 0.8 | 0.75 |
| `poc_shape_scale` | 34200 | -27000 |
| `poc_shape_scale_2nd` | 0 | -16875 |
| `poc_claim_year` | 2028 | 2028 |
| `poc_claim_quarter` | 3 | 2 |
| `poc_climate_zone` | 13 | 13 |
| `poc_target_sector` | Residential | Commercial |
| `poc_annual_net_therms` | 1620 | 4845 |
| `poc_annual_net_therms_1st` | 1620 | 4845 |
| `poc_annual_net_therms_2nd` | 0 | 3230 |
| `poc_gas_profile_key` | Annual | Winter Only |
| `poc_gas_sector` | Residential | Commercial |
| `poc_ntgr_therm` | 0.9 | 0.85 |
| `poc_ir_kwh` | 0.95 | 1 |
| `poc_rr_kwh` | 0.9 | 0.9 |
| `poc_ir_therm` | 1 | 1 |
| `poc_rr_therm` | 0.9 | 0.95 |
| `poc_ntgr_kwh` | 0.8 | 0.75 |
| `poc_ntgr_kw` | 0.8 | 0.75 |
| `poc_ir_kw` | 1 | 1 |
| `poc_rr_kw` | 1 | 1 |
| `poc_fuel_type` | (blank) | FuelSub-ToElec |
| `poc_is_fuel_sub` | false | true |
| `poc_acc_version` | 2026 | 2026 |
| `poc_kwh_water_1st` | 3 | -6 |
| `poc_kwh_water_2nd` | 0 | -4 |
| `poc_water_net_kwh_1st` | 240 | -45 |
| `poc_water_net_kwh_2nd` | 0 | -30 |
| `poc_water_scale_1st` | 205.2 | -40.5 |
| `poc_water_scale_2nd` | 0 | -27 |
| `poc_PA` | PGE | PGE |
| `ElecPricingStatus` | priced: hourly profile | priced: hourly profile |
| `ProfileSource` | user profile | user profile |
| `SystemClimateZone` | (blank) | (blank) |
| `SystemZoneSource` | (blank) | (blank) |
| `GasPricingStatus` | priced | priced |
| `GasProfileSource` | library | library |
| `WaterPricingStatus` | priced | priced |
| `ElecBen` | 5841.5 | 0 |
| `ElecSupplyCost` | 0 | 4172.17 |
| `GasBen` | 3454.03 | 6990.56 |
| `GasSupplyCost` | 0 | 0 |
| `WaterEnergyBen` | 30.6679 | 0 |
| `WaterEnergyCost` | 0 | 5.62586 |
| `OtherBen` | 160 | 320 |
| `OtherCost` | 80 | 120 |
| `TaxCredits` | 400 | 800 |
| `OtherBenGross` | 200 | 400 |
| `OtherCostGross` | 100 | 150 |
| `TaxCreditsGross` | 500 | 1000 |
| `RebatesandIncents` | 6000 | 13500 |
| `RebatesandIncentsPV` | 5776.64 | 13246.3 |
| `NetParticipantCostPV` | 12130.9 | 37937 |
| `MarkEffectPlusExcessIncPV` | 0 | 3033.38 |
| `NetParticipantCostAdjustedPV` | 12130.9 | 40970.4 |
| `GrossParticipantCostPV` | 13478.8 | 47421.2 |
| `IncentiveToOthers` | 3000 | 8000 |
| `DILaborCost` | 500 | 1000 |
| `DIMaterialCost` | 500 | 500 |
| `EndUserRebate` | 2000 | 4000 |
| `GrossMeasureCost` | 20000 | 60000 |
| `ExcessIncentives` | 0 | 0 |
| `MarkEffectPlusExcessInc` | 0 | 3000 |
| `GrossParticipantCost` | 14000 | 46500 |
| `GrossParticipantCostAdjusted` | 14000 | 49500 |
| `NetParticipantCost` | 12600 | 37200 |
| `NetParticipantCostAdjusted` | 12600 | 40200 |
| `GrossMeasCostPV` | 19255.5 | 60667.6 |
| `ExcessIncentivesPV` | 0 | 0 |
| `GrossParticipantCostAdjustedPV` | 13478.8 | 50454.6 |
| `BillReducElec` | 30972.5 | 0 |
| `BillIncrElec` | 0 | 20764.9 |
| `BillReducGas` | 10167.7 | 13198.2 |
| `BillIncrGas` | 0 | 0 |
| `BillReducElecGross` | 38715.6 | 0 |
| `BillIncrElecGross` | 0 | 25956.1 |
| `BillReducGasGross` | 11297.5 | 14664.7 |
| `BillIncrGasGross` | 0 | 0 |
| `PCTBen` | 56271.1 | 28892.2 |
| `PCTCost` | 19255.5 | 86623.6 |
| `PCTRatio` | 2.92235 | 0.333537 |
| `ElecBen_SB` | 6688.34 | 0 |
| `ElecBen_SH` | 7296.37 | 0 |
| `ElecSupplyCost_SB` | 0 | 4734.01 |
| `ElecSupplyCost_SH` | 0 | 5164.37 |
| `GasBen_SB` | 4314.29 | 8827.11 |
| `GasBen_SH` | 5033.34 | 10432 |
| `GasSupplyCost_SB` | 0 | 0 |
| `GasSupplyCost_SH` | 0 | 0 |
| `WaterEnergyBen_SB` | 35.5698 | 0 |
| `WaterEnergyBen_SH` | 39.218 | 0 |
| `WaterEnergyCost_SB` | 0 | 6.46961 |
| `WaterEnergyCost_SH` | 0 | 7.13316 |
| `RebatesandIncentsPV_sct` | 5851.9 | 13332.3 |
| `NetParticipantCostPV_sct` | 12289 | 37525.5 |
| `NetParticipantCostAdjustedPV_sct` | 12289 | 40537.5 |
| `GrossParticipantCostPV_sct` | 13654.4 | 46906.9 |
| `DiscountedSavingsGrosskWh` | 93907.9 | -67117.5 |
| `DiscountedSavingsNetkWh` | 75126.3 | -53694 |
| `DiscountedSavingsGrossThm` | 3954.02 | 10917.8 |
| `DiscountedSavingsNetThm` | 3558.62 | 9826.05 |
| `ElecBenGross` | 7301.87 | 0 |
| `GasBenGross` | 3837.81 | 7767.29 |
| `ElecSupplyCostGross` | 0 | 5215.21 |
| `GasSupplyCostGross` | 0 | 0 |
| `WaterEnergyBenGross` | 38.3348 | 0 |
| `WaterEnergyCostGross` | 0 | 7.03232 |
| `ElecBenGross_SB` | 8360.43 | 0 |
| `GasBenGross_SB` | 4793.66 | 9807.9 |
| `ElecSupplyCostGross_SB` | 0 | 5917.51 |
| `GasSupplyCostGross_SB` | 0 | 0 |
| `WaterEnergyBenGross_SB` | 44.4623 | 0 |
| `WaterEnergyCostGross_SB` | 0 | 8.08701 |
| `ElecBenGross_SH` | 9120.47 | 0 |
| `GasBenGross_SH` | 5592.6 | 11591.1 |
| `ElecSupplyCostGross_SH` | 0 | 6455.46 |
| `GasSupplyCostGross_SH` | 0 | 0 |
| `WaterEnergyBenGross_SH` | 49.0225 | 0 |
| `WaterEnergyCostGross_SH` | 0 | 8.91645 |
| `TotalSystemBenefit` | 9406.19 | 3012.77 |
| `TotalSystemBenefitGross` | 11278 | 2795.05 |
| `WeightedBenefits` | 1 | 1 |
| `WeightedProgramCost` | 3000 | 1800 |
| `WtdAdminCostsOverheadAndGA` | 1500 | 0 |
| `WtdAdminCostsOther` | 0 | 0 |
| `WtdMarketingOutreach` | 500 | 600 |
| `WtdDIActivity` | 0 | 300 |
| `WtdDIInstallation` | 0 | 0 |
| `WtdDIHardwareAndMaterials` | 0 | 0 |
| `WtdDIRebateAndInspection` | 0 | 0 |
| `WtdEMV` | 1000 | 900 |
| `WtdUserInputIncentive` | 0 | 0 |
| `WtdCostsRecoveredFromOtherSources` | 0 | 0 |
| `ProgramCosts` | 3000 | 1800 |
| `WeightedElecAlloc` | 0.61579 | 0 |
| `TotalExpenditures` | 9000 | 15300 |
| `PACCostNoAdmin` | 5856.64 | 17544.1 |
| `PACCost` | 8785.49 | 19301.4 |
| `TRCCostNoAdmin` | 17987.6 | 58514.5 |
| `TRCCost` | 20916.4 | 60271.8 |
| `TRCCostGross` | 22284.3 | 67797.1 |
| `RIMCostNoAdmin` | 46996.8 | 30742.3 |
| `RIMCost` | 49925.7 | 32499.6 |
| `SCBCostNoAdmin` | 18220.9 | 58730.3 |
| `SCBCost` | 21173 | 60501.6 |
| `SCBCostGross` | 22558.4 | 68086.1 |
| `SCHCostNoAdmin` | 18220.9 | 59161.4 |
| `SCHCost` | 21173 | 60932.6 |
| `SCHCostGross` | 22558.4 | 68624.9 |
| `TRCRatio` | 0.472652 | 0.134567 |
| `TRCRatioNoAdmin` | 0.549612 | 0.138608 |
| `PACRatio` | 1.07976 | 0.378758 |
| `PACRatioNoAdmin` | 1.61973 | 0.416696 |
| `RIMRatio` | 0.190006 | 0.863869 |
| `RIMRatioNoAdmin` | 0.201847 | 0.91325 |
| `SCBRatio` | 0.528891 | 0.151188 |
| `SCHRatio` | 0.591741 | 0.176458 |
| `SCBRatioNoAdmin` | 0.614581 | 0.155748 |
| `SCHRatioNoAdmin` | 0.687614 | 0.181741 |
| `TRCLifecycleNetBen` | -11620.9 | -53281.2 |
| `PACLifecycleNetBen` | 510.036 | -12310.9 |
| `LevBenElec` | 0.0777557 | 0 |
| `LevBenGas` | 0.970609 | 0.711432 |
| `LevTRCCost` | 0.171446 | 0 |
| `LevTRCCostNoAdmin` | 0.147439 | 0 |
| `LevPACCost` | 0.0720122 | 0 |
| `LevPACCostNoAdmin` | 0.0480052 | 0 |
| `LevRIMCost` | 0.409227 | 0 |
| `LevNetBenTRCElec` | -0.0936904 | 0 |
| `LevNetBenTRCGas` | -1.28766 | -5.42244 |
| `LevNetBenTRCElecNoAdmin` | -0.0696835 | 0 |
| `LevNetBenTRCGasNoAdmin` | -0.971442 | -5.2436 |
| `LevNetBenPACElec` | 0.00574348 | 0 |
| `LevNetBenPACGas` | 0.0220732 | -1.25288 |
| `LevNetBenPACElecNoAdmin` | 0.0297504 | 0 |
| `LevNetBenPACGasNoAdmin` | 0.33829 | -1.07404 |
| `LevNetBenRIMElec` | -0.331471 | 0 |
| `LevNetBenRIMGas` | -4.41968 | -2.59606 |
| `FirstYearGrossKWh` | 43006.5 | -36054 |
| `FirstYearGrossKWhSite` | 42750 | -36000 |
| `FirstYearGrossKWhWater` | 256.5 | -54 |
| `FirstYearGrossKW` | 20 | 0 |
| `FirstYearGrossThm` | 1800 | 5700 |
| `FirstYearNetKWh` | 34405.2 | -28843.2 |
| `FirstYearNetKWhSite` | 34200 | -28800 |
| `FirstYearNetKWhWater` | 205.2 | -43.2 |
| `FirstYearNetKW` | 16 | 0 |
| `FirstYearNetThm` | 1620 | 5130 |
| `LifecycleGrossKWh` | 103216 | -71547.8 |
| `LifecycleGrossKWhSite` | 102600 | -71437.5 |
| `LifecycleGrossKWhWater` | 615.6 | -110.25 |
| `LifecycleGrossThm` | 4320 | 11637.5 |
| `LifecycleNetKWh` | 82572.5 | -57238.2 |
| `LifecycleNetKWhSite` | 82080 | -57150 |
| `LifecycleNetKWhWater` | 492.48 | -88.2 |
| `LifecycleNetThm` | 3888 | 10473.8 |
| `AnnualGrossKWh` | 43006.5 | -28619.1 |
| `AnnualGrossKWhSite` | 42750 | -28575 |
| `AnnualGrossKWhWater` | 256.5 | -44.1 |
| `AnnualGrossKW` | 20 | 0 |
| `AnnualGrossThm` | 1800 | 4655 |
| `AnnualNetKWh` | 34405.2 | -22895.3 |
| `AnnualNetKWhSite` | 34200 | -22860 |
| `AnnualNetKWhWater` | 205.2 | -35.28 |
| `AnnualNetKW` | 16 | 0 |
| `AnnualNetThm` | 1620 | 4189.5 |
| `WeightedSavings` | 1 | 1 |
| `NetElecCO2` | 6.84 | -5.76 |
| `NetElecNOx` | 0 | -1.33721 |
| `NetPM10` | 0 | -1.33721 |
| `NetGasCO2` | 8.6022 | 27.2403 |
| `NetGasNOx` | 0 | 50.274 |
| `GrossElecCO2` | 8.55 | -7.2 |
| `GrossElecNOx` | 0 | -1.67151 |
| `GrossPM10` | 0 | -1.67151 |
| `GrossGasCO2` | 9.558 | 30.267 |
| `GrossGasNOx` | 0 | 55.86 |
| `NetElecCO2Lifecycle` | 16.4385 | -11.4342 |
| `NetElecNOxLifecycle` | 0 | -2.65352 |
| `NetPM10Lifecycle` | 0 | -2.65352 |
| `NetGasCO2Lifecycle` | 20.6453 | 55.6156 |
| `NetGasNOxLifecycle` | 0 | 102.643 |
| `GrossElecCO2Lifecycle` | 20.5481 | -14.2927 |
| `GrossElecNOxLifecycle` | 0 | -3.3169 |
| `GrossPM10Lifecycle` | 0 | -3.3169 |
| `GrossGasCO2Lifecycle` | 22.9392 | 61.7951 |
| `GrossGasNOxLifecycle` | 0 | 114.047 |
| `CreditedIOU` | PGE | PGE |
| `Prg_MatchedOn` | PRG1 | PRG2 |
| `Prg_PA` | PGE | PGE |
| `Prg_ProgramName` | Example Residential Program | Example Commercial Program |
| `Prg_ProgramStatus` | active | new |
| `Prg_ProgramSegment` | resource_acquisition | market_support |
| `Prg_StatewideProgram` |  |  |
| `Prg_PrimarySector` | Residential | Commercial |
| `Prg_Sector` | Residential (Res) | Commercial (Com) |
| `Prg_BPSector` | Residential: 1.000000 | Commercial: 1.000000 |
| `Prg_ProgramImplementer` | IOU Core | Third-Party Solicited |
| `Prg_ProgramCategory` | Other | Market Transformation |
| `Prg_ImplementationContractor` |  | Example Implementer |
| `Prg_StartYear` | 2024 | 2027 |
| `Prg_EndYear` |  | 2031 |
| `Prg_Resource_Flag` | True | False |
| `Prg_NonResource_Flag` | False | True |
| `Prg_Deemed_Flag` | True | False |
| `Prg_Custom_Flag` | False | True |
| `Prg_Upstream_Flag` | False | False |
| `Prg_Midstream_Flag` | False | True |
| `Prg_Downstream_Flag` | True | False |
| `Prg_DirectInstall` | No | Partial |
| `Prg_Audit_Flag` | False | True |
| `Prg_Financing` | None | OBF |
| `Prg_ParentProgram` | Residential Programs | Commercial Third Party Programs |
| `Prg_Exclude_From_Budget` | False | False |
| `Prg_Exclude_From_CE` | False | False |
| `Prg_Exclude_From_Statewide_Flag` | False | False |
<!-- oa-all:end -->

### 3.6 Example A: the societal-test terms

The same quarters priced on the `SCT Base` and `SCT High` rows and discounted at the societal rate (1.6): `DF_sct(k) = 1 / 1.012575 ^ Qac(k)`.

<!-- oa-societal-A:start -->
| k | Quarter | DF_sct(k) | V_elec_SB | V_elec_SH | V_gas_SB | V_gas_SH | V_water_SB | V_water_SH |
|---|---|---|---|---|---|---|---|---|
| 0 | 2028Q3 | 0.975317 | 739.86 | 807.12 | 474.00 | 553.00 | 3.935 | 4.338 |
| 1 | 2028Q4 | 0.963204 | 730.67 | 797.10 | 468.12 | 546.14 | 3.886 | 4.284 |
| 2 | 2029Q1 | 0.951242 | 705.91 | 770.08 | 462.30 | 539.35 | 3.754 | 4.139 |
| 3 | 2029Q2 | 0.939429 | 704.89 | 768.97 | 456.56 | 532.66 | 3.749 | 4.133 |
| 4 | 2029Q3 | 0.927762 | 703.79 | 767.77 | 450.89 | 526.04 | 3.743 | 4.127 |
| 5 | 2029Q4 | 0.916241 | 695.05 | 758.23 | 445.29 | 519.51 | 3.696 | 4.075 |
| 6 | 2030Q1 | 0.904862 | 671.49 | 732.54 | 439.76 | 513.06 | 3.571 | 3.937 |
| 7 | 2030Q2 | 0.893625 | 670.52 | 731.48 | 434.30 | 506.69 | 3.566 | 3.932 |
| 8 | 2030Q3 | 0.882527 | 669.47 | 730.33 | 428.91 | 500.39 | 3.560 | 3.926 |
| 9 | 2030Q4 | 0.871567 | 396.69 | 432.76 | 254.15 | 296.51 | 2.110 | 2.326 |
<!-- oa-societal-A:end -->

### 3.7 Example B: the societal-test terms

<!-- oa-societal-B:start -->
| k | Quarter | DF_sct(k) | V_elec_SB | V_elec_SH | V_gas_SB | V_gas_SH | V_water_SB | V_water_SH |
|---|---|---|---|---|---|---|---|---|
| 0 | 2028Q2 | 0.987581 | -624.02 | -680.75 | 0.00 | 0.00 | -0.830 | -0.915 |
| 1 | 2028Q3 | 0.975317 | -623.04 | -679.68 | 0.00 | 0.00 | -0.828 | -0.913 |
| 2 | 2028Q4 | 0.963204 | -615.30 | -671.24 | 2,717.68 | 3,211.80 | -0.818 | -0.902 |
| 3 | 2029Q1 | 0.951242 | -594.45 | -648.49 | 2,683.93 | 3,171.92 | -0.790 | -0.871 |
| 4 | 2029Q2 | 0.939429 | -482.29 | -526.14 | 0.00 | 0.00 | -0.658 | -0.725 |
| 5 | 2029Q3 | 0.927762 | -370.41 | -404.09 | 0.00 | 0.00 | -0.525 | -0.579 |
| 6 | 2029Q4 | 0.916241 | -365.81 | -399.07 | 1,723.45 | 2,036.80 | -0.519 | -0.572 |
| 7 | 2030Q1 | 0.904862 | -353.42 | -385.55 | 1,702.05 | 2,011.51 | -0.501 | -0.553 |
| 8 | 2030Q2 | 0.893625 | -352.91 | -384.99 | 0.00 | 0.00 | -0.500 | -0.552 |
| 9 | 2030Q3 | 0.882527 | -352.35 | -384.39 | 0.00 | 0.00 | -0.500 | -0.551 |
<!-- oa-societal-B:end -->
