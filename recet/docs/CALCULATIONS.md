# How every output is calculated

This document explains the method behind the formulas `recet` uses: the notation, which avoided-cost rows and load shape are chosen and why, each calculation stage (`C1` ... `C16`), the validation
against CET, the deliberate differences and the limitations. **The formula of each output column, written out in the input fields with every symbol defined and a worked number, is in
[OUTPUT_FIELDS.md](OUTPUT_FIELDS.md)**; this document is the place to look for the reasoning and the rules behind it. [INPUT_FIELDS.md](INPUT_FIELDS.md) defines the input fields.

**The formulas here are executable.** `tests/reference_calc.py` re-implements every one of them in plain loops (no shared pricing code) and
`tests/test_calcs.py` requires the tool to agree with it on about 300 values for a three-measure job that exercises each branch below, with and without
market effects. Section C18 is a worked example whose numbers are also checked by a test (`tests/test_docs.py`).

Contents

* [C0 Notation and conventions](#c0-notation-and-conventions)
* [C1 Derived measure fields (`poc_*`)](#c1-derived-measure-fields-poc_)
* [C2 Time: quarters, useful life, baselines, discounting](#c2-time-quarters-useful-life-baselines-discounting)
* [C3 Avoided-cost data](#c3-avoided-cost-data)
* [C4 Load shapes](#c4-load-shapes)
* [C5 Lifetime value of each fuel](#c5-lifetime-value-of-each-fuel)
* [C6 Benefit / supply-cost split](#c6-benefit--supply-cost-split)
* [C7 Other benefits, other costs and tax credits](#c7-other-benefits-other-costs-and-tax-credits)
* [C8 Participant costs and incentives](#c8-participant-costs-and-incentives)
* [C9 Bill impacts](#c9-bill-impacts)
* [C10 Gross variants](#c10-gross-variants)
* [C11 Program costs and their weighting](#c11-program-costs-and-their-weighting)
* [C12 The cost tests and ratios](#c12-the-cost-tests-and-ratios)
* [C13 Where market effects enter](#c13-where-market-effects-enter)
* [C14 Emissions](#c14-emissions)
* [C15 Statewide allocation](#c15-statewide-allocation)
* [C16 Optional and secondary outputs](#c16-optional-and-secondary-outputs)
* [C17 Validation, deliberate differences from CET, and limitations](#c17-validation-deliberate-differences-from-cet-and-limitations)
* [C18 Worked example](#c18-worked-example)

---------------------------------------------------------------------------------------------------------------------------------

## C0 Notation and conventions

* A **measure** is one row of `Measure.csv` (one `CEInputID`). Everything below is per measure unless it says "program" or "portfolio".
* Money is nominal US dollars. **PV** means present value at the run's First Year (C2.4). Energy is kWh, demand kW, gas therms; electric avoided costs
  are $/MWh in the ACC files and $/kWh in the formulas (`/ 1000`), gas avoided costs are $/therm. Emissions are described in C14 (CO2 in metric tons; NOx and PM10 in pounds).
* `pos(x) = max(x, 0)`. `ratio(n, d) = n / d`, and `0` when `d = 0` (the way CET's ratio columns behave).
* Suffixes: `1st` / `2nd` = first / second baseline; `_SB` / `_SH` = societal test with the Base / High social-cost-of-carbon rows; `_sct` = a value discounted at the
  societal rate; `Gross` = without net-to-gross (C10); `NoAdmin` = without the program-level costs (C11).
* `F` is the run's **First Year** (C2.5). `MEB` and `MEC` are the run's market-effect adders on benefits and on costs, both 0 or both 0.05 (C13).

Symbols used in the formulas (input field in brackets; a blank input takes the default shown):

| Symbol | Meaning | Source |
|---|---|---|
| `Q` | number of units | `NumUnits` |
| `kWh1`, `kWh2` | per-unit annual kWh savings, 1st / 2nd baseline | `UnitkWh1stBaseline`, `UnitkWh2ndBaseline` (blank = 0) |
| `kW1`, `kW2` | per-unit kW savings | `UnitkW1stBaseline`, `UnitkW2ndBaseline` (blank = 0) |
| `Th1`, `Th2` | per-unit annual therm savings | `UnitTherm1stBaseline`, `UnitTherm2ndBaseline` (blank = 0) |
| `Wt1`, `Wt2` | per-unit annual kWh embedded in the water saved (negative: used) | `UnitkWhIOUWater1stBaseline`, `UnitkWhIOUWater2ndBaseline` (blank = 0) |
| `NTGe` | kWh net-to-gross | `NTGRkWh` |
| `NTGkw` | kW net-to-gross | `NTGRkW` (blank = `NTGRkWh`, then 1) |
| `NTGt` | therm net-to-gross | `NTGRTherm` (blank = `NTGRkWh`, then 1) |
| `NTGc` | cost net-to-gross | `NTGRCost` (blank = `NTGRkWh`, then 1) |
| `IRe`, `RRe` | installation and realization rate, kWh | `InstallationRatekWh`, `RealizationRatekWh` (blank = 1) |
| `IRkw`, `RRkw`, `IRt`, `RRt` | the same for kW and therms | `InstallationRatekW`, `RealizationRatekW`, `InstallationRateTherm`, `RealizationRateTherm` (blank = 1) |
| `EUL`, `RUL` | effective and remaining useful life, years | `EUL_Yrs` (at least 0.25), `RUL_Yrs` (blank = 0) |
| `Y0`, `q0` | claim year and claim quarter (1-4) | `ClaimYearQuarter` = `YYYYQn` |
| `PA` | avoided-cost territory | `IOU_AC_Territory` |
| `w(k)`, `DF(k)`, `phased(k; a, b)` | quarter weight, discount factor, baseline phasing | C2 |

---------------------------------------------------------------------------------------------------------------------------------

## C1 Derived measure fields (`poc_*`)

The measure-level output repeats every raw `Measure.csv` column unchanged and then adds the fields the pricing actually uses, prefixed `poc_`. (A `poc_` field is left out
when it is identical to the raw column it restates, for example `poc_num_units`; `poc_ntgr_cost` appears when `NTGRCost` is blank in the file and so differs.)

**C1.1 Keys and time**

```text
poc_PA               = IOU_AC_Territory
poc_climate_zone     = E3ClimateZone (as text)          poc_target_sector = E3TargetSector      poc_shape_key = E3MeaElecEndUseShape
poc_gas_profile_key  = E3GasSavProfile                  poc_gas_sector    = E3GasSector
poc_claim_year       = first four characters of ClaimYearQuarter        poc_claim_quarter = its last character (1-4)
poc_eul_years_exact  = max(EUL_Yrs, 0.25)                               poc_rul_years_exact = max(RUL_Yrs, 0)      (blank RUL = 0)
poc_eul_years        = max(round(EUL_Yrs), 1)      (information only: pricing uses poc_eul_years_exact)
poc_subset_key       = "CZ" + E3ClimateZone + "_" + Sector             (a label; not used in pricing)
```

**C1.2 Electric quantities**

```text
poc_ntgr_kwh   = NTGRkWh (blank = 1)         poc_ntgr_kw = NTGRkW (blank = NTGRkWh, then 1)      poc_NTGRkWh = NTGRkWh (as given)
poc_ir_kwh = IRe    poc_rr_kwh = RRe    poc_ir_kw = IRkw    poc_rr_kw = RRkw

poc_annual_net_kwh_1st = Q * kWh1 * NTGe          poc_annual_net_kwh_2nd = Q * kWh2 * NTGe          poc_annual_net_kwh = poc_annual_net_kwh_1st
poc_annual_net_kw_1st  = Q * kW1  * NTGRkW        poc_annual_net_kw_2nd  = Q * kW2  * NTGRkW        poc_annual_net_kw  = poc_annual_net_kw_1st

poc_shape_scale     = poc_annual_net_kwh_1st * IRe * RRe       <- the annual kWh the electric benefit is priced on (S1e)
poc_shape_scale_2nd = poc_annual_net_kwh_2nd * IRe * RRe                                                  (S2e)
```

`poc_annual_net_kwh*` is net of NTG only; the installation and realization rates enter through `poc_shape_scale*`, which is what is priced (and what the combined 8,760
profile is built from).

**C1.3 Gas quantities**

```text
poc_ntgr_therm = NTGt     poc_ir_therm = IRt     poc_rr_therm = RRt
poc_annual_net_therms_1st = Q * Th1 * NTGt * IRt * RRt          (T1)      poc_annual_net_therms_2nd = Q * Th2 * NTGt * IRt * RRt   (T2)
poc_annual_net_therms     = poc_annual_net_therms_1st
```

Unlike electricity, the gas quantity already includes the installation and realization rates.

**C1.4 Water-energy quantities**

```text
poc_kwh_water_1st = Wt1        poc_kwh_water_2nd = Wt2
poc_water_net_kwh_1st = Q * Wt1 * NTGe         poc_water_net_kwh_2nd = Q * Wt2 * NTGe
poc_water_scale_1st   = poc_water_net_kwh_1st * IRe * RRe        (W1)        poc_water_scale_2nd = poc_water_net_kwh_2nd * IRe * RRe   (W2)
```

**C1.5 Fuel substitution**

```text
poc_fuel_type    = FuelID (as text)
poc_is_fuel_sub  = (FuelID is one of AllElec-New, FuelSub-ToElec, FuelSub-ToGas)  OR  ("FuelSub" appears in MeasImpactType)
```

Only fuel-substitution measures book a negative fuel value as a *supply cost* (C6.1). The `MeasImpactType` rule reproduces what CET does with rows whose `FuelID` is blank
(it matched CET's supply-cost measures in the four real runs with no misses).

---------------------------------------------------------------------------------------------------------------------------------

## C2 Time: quarters, useful life, baselines, discounting

CET prices in **calendar quarters**, not years. Every stream in C5 is a sum over the quarters of the measure's life.

**C2.1 The quarters a measure delivers savings.** Let `EQ = 4 * EUL`, `full = floor(EQ)`, `rem = EQ - full` and `n = full + 1` if `rem > 1e-9`, else `n = full`.
For `k = 0 ... n-1` (k = 0 is the *claim quarter itself*):

```text
a(k)       = (q0 - 1) + k
year(k)    = Y0 + floor(a(k) / 4)
quarter(k) = a(k) mod 4                      (0 = Q1 ... 3 = Q4)
w(k)       = rem   if k = n-1 and rem > 1e-9     (the last, partial quarter of a fractional life)
             1     otherwise
```

A claim in Q3 therefore delivers two quarters in its first calendar year, and a life of 2.4 years (9.6 quarters) is ten quarters, the last weighted 0.6.

**C2.2 Hours in a quarter.** Electric prices are hourly, 8,760 hours a year, on the ACC's own calendar (C4.2). The quarters are hours
`1-2160` (Q1), `2161-4344` (Q2), `4345-6552` (Q3), `6553-8760` (Q4): 2160, 2184, 2208 and 2208 hours.

**C2.3 Baseline phasing.** With `RQ = 4 * RUL` (RUL is counted from the claim quarter), a quantity that differs between baselines, `a` (1st) and `b` (2nd), is

```text
phased(k; a, b) = a                                     if RQ <= 0  or  k + 1 <= RQ        (single baseline, or entirely before the RUL)
                  b                                     if k >= RQ                          (entirely after the RUL)
                  (RQ - k) * a + (1 - (RQ - k)) * b     otherwise                           (the one quarter that contains the RUL)
```

`RUL = 0` (the normal case) means a single baseline: the 1st-baseline value applies for the whole life and the 2nd-baseline fields are ignored.

**C2.4 Discounting.** The discount rates come from the run parameters for the measure's `PA` and the ACC version (C3.3), or from a supplied
run-parameters file for that PA. `DiscountRateQtr` is the nominal
annual rate divided by four (for PG&E, every built-in ACC: 0.0766 / 4 = 0.01915).

```text
Rqf  = 1 + DiscountRateQtr(PA)          Rqf_sct = 1 + DiscountRateQtr_SCT(PA)          Raf = 1 + DiscountRateAnnual(PA)          Raf_sct = 1 + DiscountRateAnnual_SCT(PA)
Qac(k) = (year(k) - F) * 4 + quarter(k)                       DF(k) = 1 / Rqf ^ Qac(k)        (DF_sct(k) with Rqf_sct)
```

`Qac` counts calendar quarters from Q1 of the First Year, not quarters since the claim: a Q3 claim in the First Year starts at `Qac = 2`. A claim year before `F` gives a negative
`Qac` and a factor above 1. Streams that CET holds annually (program costs, bill impacts) use `Raf` and whole years (C9, C11); one-time cash flows use `Qm` (C8).

**C2.5 First Year.** `F` is CET's run-level *First Year*. It is not in the input files; set it in the GUI/`--base-year`, otherwise it is the earliest claim year in the job.

---------------------------------------------------------------------------------------------------------------------------------

## C3 Avoided-cost data

**C3.1 Electric ACC.** One row per `(iou, climate_zone, cost_test, year, hour_of_year)`; dollar columns are $/MWh. `total_avoided_cost` is the sum of the component columns
(`energy`, `generation_capacity`, `avoided_as_procurement`, `losses`, `methane_leakage`, `ghg_cap_and_trade`, `ghg_portfolio_rebalancing`, `ghg_cap_and_invest`, `ghg_adder`,
`air_quality_adder`, `transmission`, `distribution` - the columns present depend on the ACC vintage).

```text
AC(y, i)   = total_avoided_cost(y, i) / 1000                                        $/kWh, hour index i = 0 ... 8759
TD(y, i)   = (transmission + distribution)(y, i) / 1000                             the transmission and distribution part
Gen(y, i)  = AC(y, i) - TD(y, i)                                                    everything else (energy, capacity, GHG, losses, ...)
```

*Which rows.* The test rows are `cost_test = TRC` for TRC, PAC and RIM, `SCT Base` for SCB and `SCT High` for SCH. The row set is chosen from the measure's `PA` and zone:

1. `PA` to an ACC utility: `PGE` = PG&E, `SCE` = SCE, `SDGE` = SDG&E. `SCG` (a gas-only utility) is priced in each zone entirely - generation, T&D and emission
   rate - by **one** utility: SCE where the ACC file has SCE's own rows for that zone, otherwise PG&E where it has PG&E's, otherwise SCE (with the fallbacks below).
   SCE's CZ5 stand-in (item 2) does not count as SCE data, so SCG CZ5 is PG&E. On both built-in ACCs: SCE in 6, 8, 9, 10, 13, 14, 15, 16; PG&E in 1, 2, 3A, 3B, 4, 5,
   11, 12.
2. The zone is `"CZ" + E3ClimateZone`. Three (utility, zone) pairs that the ACC model does not export are stood in for by a documented neighbour: SCE CZ5 uses SCE CZ9;
   SDG&E CZ6 and CZ8 use SDG&E CZ7.
3. `E3ClimateZone = System` is first replaced by the profile's System zone (C4.4), so a System measure is priced on a real zone's rows. A zone that is still not in the
   file for that utility (a System measure none of whose zones can be priced) uses the unweighted mean across the utility's zones in the file.
4. **Year.** A pricing year outside the file's range uses the *nearest* year in the file (a tie takes the earlier year).

**C3.2 Gas ACC.** One row per `(utility, class, cost_test, scc, year)`; dollar columns are $/therm.

```text
utility:  PGE = PG&E   SCG = Socal Gas   SDGE = SDG&E   SCE = Socal Gas (SCE has no gas utility of its own)
class:    E3GasSector  Residential = Residential   Commercial = Commercial   Core = Total Core
rows:     TRC (scc blank) for TRC/PAC/RIM;   SCT with scc = Base for SCB;   SCT with scc = High for SCH
year:     clamped to the first / last year in the file

ACg(y, m) = market_m + td_m + nox + co2 + upstream_methane + air_quality_adder  [ + behind_meter_methane ]         m = 1 ... 12, $/therm
```

`market` and `td` vary by month; the other components are one annual value used in every month. `behind_meter_methane` is included **only** when the class is Residential and the
measure's `E3GasSavProfile` contains `FuelSub` (a residential gas-to-electric fuel-substitution profile).

**C3.3 E3 tables and run parameters.** Discount rates, `CO2Gas`, NOx/PM10 factors and retail rate schedules come from CET's E3 tables and are picked by **ACC
version**. The packaged tables are CET exports, whose `Version` column uses CET's own numbering; each is mapped to the ACC it holds when the table is read:

| CET Version | 2022 | 2024 | 2026 | 2027 |
|---|---|---|---|---|
| ACC | 2021 | 2022 | 2024 | 2026 |

(from each version's release date in `E3Settings.AvoidedCostVersion`; the older versions 2018-2021 map to the 2017-2020 ACCs). The discount rates, base year and
`CO2Gas` (CET's `E3Settings`) are the **run parameters**; a supplied run-parameters file replaces them for the PAs it lists. A built-in ACC uses its own tables, and a new
ACC label borrows the tables of the ACC version you name (default: the newest built-in). The primary rate is `DiscountRateAnnual` (PG&E 7.66%, SCE 7.65%, SCG 7.38%,
SDG&E 7.36%, the same in every built-in ACC); the societal rate `DiscountRateAnnual_SCT` is 5.03% for every utility. `BaseYear`: 2021 (2021 ACC), 2022 (2022 ACC),
2024 (2024 and 2026 ACCs). The rate schedules have no SCG rows for the 2021 and 2022 ACCs, so SCG bill impacts are not priced on those versions.

**C3.4 TRC-only ACCs.** The 2021 and 2022 ACCs have no Societal Cost Test avoided costs (no SCT rows in either file, and no air quality adder). A run on them prices
TRC, PAC and RIM as usual and leaves every SCT Base / SCT High column (`*_SB`, `*_SH`, `SCB*`, `SCH*`) blank, with a note in the log and `run_settings.json`. The same
applies to any supplied ACC file without SCT rows.

---------------------------------------------------------------------------------------------------------------------------------

## C4 Load shapes

**C4.1 Which electric shape a measure uses.** In order, first match wins; the outcome is reported per measure in `ProfileSource`:

| Order | Shape | `ProfileSource` |
|---|---|---|
| 1 | a **supplied 8,760 profile** matching (`E3MeaElecEndUseShape`, `PA`, `E3TargetSector`) | `user profile` |
| 1b | the same, but the supplied profile has no rows for this measure's zone (or `System` / `ALL`): fall through to the library | `library (user profile has no shape for this zone)` |
| 2 | the **CEDARS library** profile with the same end use, `PA` and target sector | `library` |
| 3 | the library profile with the same end use and `PA`, ignoring the sector | `library (sector-loose match)` |
| - | none of the above: the measure is **not priced** (`ElecPricingStatus = UNPRICED ...`), never priced on a made-up shape | (blank) |

A shape is 8,760 non-negative-or-negative shares that sum to 1 (a *fraction of the measure's annual net kWh* in each hour). A library or supplied profile is used for the measure's own
zone (for `E3ClimateZone = System`, the profile's System zone, C4.4), else the profile's own `System` rows. Every electric measure is priced from an 8,760 shape (C5.1); there is no other electric route. The library includes CEDARS' TOU (H-factor) end
uses as 8,760s built from their Gen-based energy shares: each TOU period's share spread evenly over that period's hours on the TOU map of the 2026 Electric ACC v1b
as re-issued on 2026-10-20, which is on the real 2018 calendar (`represents_year` = 2018; 2018 holidays and daylight-time dates). SCE/SCG shares are rescaled to sum
to 1 and SDG&E's winter on-peak share is the remainder to 1. Their catalog rows carry `hourly_source = TOU H-factor 8,760`. (Before 0.1.18 they were drawn on the
earlier ACC map, which followed the 2025 calendar, with `represents_year` = 2025.)

**C4.2 Calendar alignment.** A profile is drawn on the weekdays of the year it represents (`represents_year`; 2009, the DEER reference year, when blank; 2018 for the TOU
8,760s). An ACC's hourly avoided costs follow **one calendar's weekdays in every forecast year**: **2018** for the 2024 and 2026 ACCs (hour 1 is a Monday; the weekly
low in distribution and transmission value falls on days 6-7 of every week of every year) and **2020** for the 2021 and 2022 ACCs (hour 1 is Wednesday 1 January
2020). The calendar year `A` is read from the ACC file's hour stamps (2018 when a supplied file has none). Every profile is rotated **once**, onto `A`, and the same
aligned shape prices every year. With Monday = 0:

```text
d0         = (weekday(1 January A) - weekday(1 January of represents_year)) mod 7
d          = d0 if d0 <= 3, else d0 - 7                         (the short way round: -3 ... +3 days)
shape_y(i) = shape( (i + 24 * d) mod 8760 )                     i = 0 ... 8759, the same for every year y
```

Example: a 2025 profile (1 January a Wednesday) has `d = -2` on the 2024/2026 ACCs, so its 1 January lands on ACC day 3, a Wednesday; on the 2021/2022 ACCs `d = 0`.
The TOU 8,760s (2018) need no rotation on the 2024/2026 ACCs; on the 2021/2022 ACCs they move `d = +2` days onto the 2020 weekdays, so their weekdays line up and
their holidays and season boundaries are two days off 2020's. Leap years: an ACC year always has 8,760 hours (the last day of a leap year is dropped, so the 2020 calendar includes 29 February and ends on
30 December); hour `i` is the same position in every year, and only weekdays are aligned. This is **not** CEDARS' rule: CEDARS aligns the profile to each
forecast year's own 1 January weekday (although the ACC data do not follow those calendars) and rotates one day too far when the offset is negative. Gas is monthly and is not shifted.

**C4.3 Gas monthly shape.** `E3GasSavProfile` names a profile of 12 monthly shares summing to 1 (`Annual`, `Winter Only`, `Summer Only`, and the three `...-FuelSub-ToElec` versions ship with the package;
a supplied profile with the same name replaces it). A gas measure whose profile name matches nothing is not priced (`GasPricingStatus = UNPRICED ...`).

**C4.5 Already-scaled profiles and load shifting (BRD BR-06.6).** A supplied profile with `profile_basis = kwh_per_unit` holds the kWh saved per unit in each hour
(negative = added load) and is priced as given; it is not rescaled. For such a measure

```text
poc_shape_scale     = NumUnits * NTGRkWh * IR * RR                    the profile carries the kWh, so C5's  SUM over hours of shape x price x scale  is the profile's own value
poc_shape_scale_2nd = poc_shape_scale * r2        r2 = UnitkWh2ndBaseline / UnitkWh1stBaseline   (1 when UnitkWh1stBaseline is 0)
annual kWh per unit = S = SUM over hours of the profile                replaces UnitkWh1stBaseline (S x r2 the 2nd) in poc_annual_net_kwh_*, first-year kWh and the bill impacts
```

Emissions use the profile the same way (its hourly kWh x the hourly emission rate). Water energy is spread by the profile's shares, `profile / S`; with `S = 0` it has none and is
not priced. With statewide allocation an allocated row is multiplied by its electric factor, as its UnitkWh fields are. A **load-shifting-only** measure (`S = 0`: kWh saved in some
hours, the same kWh added in others) therefore has a real electric value (the price difference between the hours) in one record, while its annual kWh, first-year kWh and
annual-rate bill impacts are 0. (CET nets annual savings before shaping, so it values such a measure at 0.)

**C4.4 The System climate zone.** A measure with `E3ClimateZone = System` is priced exactly as if its zone were the profile's **System zone**: the real climate zone, out of
the zones the profile has, with the **lowest total electric avoided cost over the full length of the ACC data**:

```text
total(z)    = SUM over every year y in the ACC file, SUM over hours i:  shape_z,aligned(i) * AC_TRC(PA, z, y, i)        $ per annual kWh, undiscounted
System zone = the zone z with the lowest total(z); a tie goes to the first zone in the order 1, 2, 3A, 3B, 4 ... 16
```

`shape_z,aligned` is the profile's own shape for zone z aligned as in C4.2, and `AC_TRC(PA, z, ...)` is the TRC total avoided cost found by the rules of C3.1 (so SCE CZ5 and
SDG&E CZ6/CZ8 are options, priced on their stand-in zones, and SCG's options are the zones its routing reaches). A zone the ACC cannot price is not an option. The TRC cost
test decides the zone; the same zone is then used for the shape, every cost test, every component, water energy and the hourly CO2 (NOx and PM10 still use the literal
`System` key of E3Emissions, as CET does). For a supplied profile the options are the zones it gives; one given only for `ALL` (or `System`) uses that shape in every zone
the measure's `PA` has prices for, so the prices alone decide.

The choice depends only on the profile and the ACC file, so it is **cataloged**: the package ships the System zone of every library profile under each built-in ACC
(`data/system_zones/system_zones.csv.gz`, with every option's total; rebuild with `recet data build-system-zones`). A run uses the catalog for a library profile
priced on a built-in ACC file that matches the one the catalog was built from, and calculates the choice otherwise (a supplied profile or ACC file). Each measure reports
the zone in `SystemClimateZone` and where it came from in `SystemZoneSource`. If no zone can be priced the measure falls back to the profile's `System` rows and the
blended prices of C3.1 item 3.

---------------------------------------------------------------------------------------------------------------------------------

## C5 Lifetime value of each fuel

Each of C5.1-C5.5 produces one signed lifetime PV per measure: `V_elec`, `V_gas`, `V_water`. C5.6 repeats them for the societal tests. C6 then splits each into a benefit and a supply cost.

**C5.1 Electric, hourly profile (the general route).**

```text
V_elec = SUM over k of  phased(k; S1e', S2e') * w(k) * DF(k) * SUM over hours i in quarter(k) of  shape_year(k)(i) * AC(y*(k), i)

   S1e' = poc_shape_scale     * (NTGe + MEB) / NTGe        S2e' = poc_shape_scale_2nd * (NTGe + MEB) / NTGe        (the factor is 1 when MEB = 0 or NTGe = 0)
   y*(k) = the nearest ACC year to year(k)                 shape_year(k) = the calendar-aligned shape of C4.2
```

Units: annual kWh x (share of the year in each hour) x $/kWh = $. *Implementation note:* the inner sum `U` does not depend on the measure's kWh, so the tool computes it once per (shape, IOU, sector, zone) route, pricing year and quarter and
evaluates each measure's quarters with vectorised arithmetic; the result equals the per-measure loop to floating-point rounding (relative differences below 1e-14). Because the ACC's `total_avoided_cost` already contains transmission and distribution, there is no separate kW or T&D term on
this route.

**C5.2 Retired (2026-10-02): the TOU (H-factor) route.** TOU end uses are priced by C5.1 from their 8,760s in the library (C4.1). What follows is the formula the tool used
before, kept for reference: CET's two-term benefit, a **kWh (generation) term** and a **demand-scaled T&D term**.

```text
f_kWh = Q * (NTGe  + MEB) * IRe  * RRe          f_kW = Q * (NTGkw + MEB) * IRkw * RRkw
DS_i  = kW_i   if the row's ds_type = kW  (PG&E commercial / industrial / agricultural and all SCE profiles)
        kWh_i  if ds_type = kWh          (PG&E residential)

V_elec = SUM over k of  w(k) * DF(k) * [ phased(k; f_kWh*kWh1, f_kWh*kWh2) * GenQ(year(k), quarter(k))  +  phased(k; f_kW*DS_1, f_kW*DS_2) * TDQ(year(k), quarter(k)) ]
```

`GenQ` and `TDQ` are quarterly rates built once per (PA, sector, end use, zone, year) from the ACC's hourly Gen and T&D and the profile's five period factors (`e0..e4` energy shares, `kw0..kw4` demand factors):

```text
periods P = 1 summer on-peak, 2 summer partial-peak, 3 summer off-peak, 5 winter partial-peak, 6 winter off-peak       (period 4 has no hours)
period map: each ACC hour i belongs to the period of hour (i + 25) on a 1991 calendar   (a fixed +25 hour alignment)
f(P, q)   = share of period P's hours that fall in quarter q

GenQ(y, q) = SUM_P  e_P  * f(P, q) * mean over hours i in P of Gen(y, i)
TDQ(y, q)  = SUM_P  kw_P * f(P, q) * mean over hours i in P of TD(y, i)                       (kw_form = per_kwh: PG&E residential, kW per annual kWh)
           = SUM_P  kw_P * f(P, q) * 0.001 * (sum over hours i in P of TD(y, i))               (kw_form = rel_kw: relative kW factors)
```

Notes: only PG&E and SCE have TOU factors (a measure for any other territory with no hourly shape is unpriced). A TOU profile's `System` zone is the one real zone with the lowest total T&D.
Unlike C5.1, TOU quarters in a year outside the ACC's range are **skipped**, not priced at the nearest year.

**C5.3 Retired (2026-10-02): CET's SCG table.** SCG measures are priced by C5.1 on the SCG routing of C3.1; CET's table is no longer read. Formerly, with an option on (2024
ACC only), an SCG measure whose (sector, end use, zone) is in the table was priced on CET's quarterly rate, which already includes the profile:

```text
V_elec = SUM over k of  phased(k; A1, A2) * rate(year(k), quarter(k)) * w(k) * DF(k)         A_i = poc_annual_net_kwh_i * IRe * RRe * (NTGe + MEB) / NTGe
rate = Gen + TD from CET's table for the cost test
```


**C5.4 Gas.** (Implementation note: as in C5.1, the sum over a quarter's months depends only on the profile, IOU, gas sector and year, so the tool computes it once per route, year and quarter; the result equals the per-measure loop to rounding.)

```text
V_gas = SUM over k of  w(k) * DF(k) * SUM over months m in quarter(k) of  gasshape(m) * phased(k; T1', T2') * ACg(y^(k), m)

   T1' = poc_annual_net_therms_1st * (NTGt + MEB) / NTGt          T2' likewise          y^(k) = year(k) clamped to the gas ACC's first / last year
   quarter months: Q1 = Jan-Mar, Q2 = Apr-Jun, Q3 = Jul-Sep, Q4 = Oct-Dec
```

A measure is gas-priced only if `T1 != 0` or `T2 != 0` and its profile name matches (C4.3).

**C5.5 Water energy.** The kWh embedded in the water a measure saves (`Wt > 0`) or uses (`Wt < 0`) is valued like electricity but on the **generation term alone** (CalcCE has no T&D term for it):

```text
V_water = the sum of C5.1 with  W1, W2 in place of S1e, S2e  and  Gen(y, i) = AC - TD  in place of AC
```

It uses the measure's own electric profile, zone and market effects. A water measure with no electric profile cannot be priced (`WaterPricingStatus = UNPRICED ...`).
Pricing water on the total avoided cost instead of Gen overstates it by about a quarter (1.23 times CET against 0.98 times CET on the real runs), which is why the Gen-only rule is written out here.

**C5.6 Societal (SB / SH) values.** `V_elec_SB`, `V_gas_SB`, `V_water_SB` (and `_SH`) repeat C5.1-C5.5 with two changes: the electric and water rows are `SCT Base` (`SCT High`) and the gas rows are `SCT` with `scc = Base` (`High`);
and discounting uses `Rqf_sct` (C2.4). When SCB/SCH are switched off these columns are blank.

---------------------------------------------------------------------------------------------------------------------------------

## C6 Benefit / supply-cost split

**C6.1 Electric and gas.**

```text
fuel-substitution measure (poc_is_fuel_sub):   Ben = pos(V)        SupplyCost = pos(-V)
every other measure:                           Ben = V (signed)    SupplyCost = 0

ElecBen, ElecSupplyCost = split of V_elec         GasBen, GasSupplyCost = split of V_gas         (and _SB, _SH from V_elec_SB ... )
```

A fuel-substitution measure that *adds* electric load has a negative `V_elec`; the loss is booked as a positive `ElecSupplyCost`, not a negative benefit. For any other measure a negative value simply stays
in `Ben`.

**C6.2 Water energy.** The sign of the *first-baseline water kWh* decides, not the sign of the value:

```text
WaterEnergyBen  = V_water     if Wt1 > 0, else 0           WaterEnergyCost = -V_water   if Wt1 < 0, else 0           (same for _SB, _SH)
```

---------------------------------------------------------------------------------------------------------------------------------

## C7 Other benefits, other costs and tax credits

Per-unit amounts times units, **undiscounted** (they are one-time amounts in the claim):

```text
OtherBen    = Q * (NTGRkWh + MEB) * (UnitGasInfraBens + UnitRefrigBens + UnitMiscBens)
OtherCost   = Q * (NTGRkWh + MEC) * (UnitRefrigCosts + UnitMiscCosts)                    (UnitGasInfraBens is a benefit-only field)
TaxCredits  = Q * (NTGRkWh + MEB) * UnitTaxCredits

OtherBenGross = Q * (UnitGasInfraBens + UnitRefrigBens + UnitMiscBens)      OtherCostGross = Q * (UnitRefrigCosts + UnitMiscCosts)      TaxCreditsGross = Q * UnitTaxCredits
```

`TaxCredits` is added to the numerator of the TRC ratio only; it is not in `TotalSystemBenefit` or any cost.

---------------------------------------------------------------------------------------------------------------------------------

## C8 Participant costs and incentives

These are one-time cash flows at the claim quarter, discounted once. Let

```text
Qm  = (Y0 - F) * 4 + (q0 - 1)                 PVF = Rqf ^ Qm          (PVF_sct with Rqf_sct)        every "PV" below divides by PVF

IncentsDI = Q * (UnitIncentiveToOthers + UnitDirectInstallLab + UnitDirectInstallMat)              Rebates = Q * UnitEndUserRebate
```

```text
RebatesandIncentsPV = (IncentsDI + Rebates) / PVF

GrossMeasCost   = Q * UnitMeaCost1stBaseline                    if RUL = 0
                = Q * UnitMeaCost2ndBaseline                    if RUL > 0        (an early replacement counts only the normal-replacement cost)
GrossMeasCostPV = Q * (UnitMeaCost1stBaseline - PVsub) / PVF
   PVsub        = 0                                                                                    if RUL = 0
                = (UnitMeaCost1stBaseline - UnitMeaCost2ndBaseline) * ((1 + MeasInflation/4) / Rqf) ^ (4 * RUL)     if RUL > 0

ExcessIncentivesPV = pos( IncentsDI / PVF - GrossMeasCostPV )                        (end-user rebates are not part of the test)
MarkEffectPlusExcessIncPV = MEC * (GrossMeasCostPV + ExcessIncentivesPV)

RebatesandIncents            = IncentsDI + Rebates                                                    (nominal; part of TotalExpenditures, C11.3)
GrossParticipantCostPV       = GrossMeasCostPV - (IncentsDI + Rebates) / PVF
NetParticipantCostPV         = NTGc * (GrossParticipantCostPV + ExcessIncentivesPV)                 (as CET exports it: no market-effects term)
NetParticipantCostAdjustedPV = NetParticipantCostPV + MarkEffectPlusExcessIncPV                     (what the TRC / SCT costs use)
```

The `_sct` columns (`RebatesandIncentsPV_sct`, `GrossParticipantCostPV_sct`, `NetParticipantCostPV_sct`, `NetParticipantCostAdjustedPV_sct`) are the same with `Rqf_sct`.
All of these match CET's own columns to the cent with 0% and 5% market effects (CET jobs 121842 / 121841, 13 measures, four of them accelerated replacements). With `MEC = 0` the market-effects term is 0.

---------------------------------------------------------------------------------------------------------------------------------

## C9 Bill impacts

The ratepayer bill change for RIM. Unlike C5 it is **annual**, not quarterly, and uses the E3 retail rate schedules: `RateE` ($/kWh) for the schedule that (`PA`, `E3TargetSector`) maps to, and `RateG` ($/therm) for
(`PA`, `E3GasSector`). Like the benefits it uses `NTG + MEB` (CalcCE prices the bill on `Qty x (NTGRkWh + MEBens)` and `(NTGRThm + MEBens)`), and the measure-level
`RateScheduleElec` / `RateScheduleGas` columns are not read.

With `yr1 = Y0 - BaseYear` (E3Settings `BaseYear`, 2024), `E = EUL`, `R = RUL`, `full = floor(E)`, `rem = E - full`, `n = full + 1 if rem > 1e-9 else full`:

```text
floor_rul = floor(R) if R > 0 else full                                     boundary share of the 1st baseline:  b = R - floor(R) if R > 0 else 1

PV(s1, s2, rates) =   SUM over k = 0 ... min(floor_rul, n) - 1 of        s1 * rate(Y0 + k) / Raf ^ k
                    + SUM over k = floor_rul ... n - 1 of                 wt * v(k) * rate(Y0 + k) / Raf ^ (k + yr1)
      where  wt = rem if k = n-1 and rem > 1e-9, else 1          v(k) = b * s1 + (1 - b) * s2  if k = floor_rul,   else s2
```

The second sum's exponent `k + yr1` (years since 2024, not since the claim) reproduces what CalcCE's SQL does for the boundary year and every 2nd-baseline year; it was confirmed against real CET output
to the cent, so it is kept rather than "corrected".

```text
elec PV = PV( fe * poc_shape_scale, fe * poc_shape_scale_2nd, RateE )        gas PV = PV( ft * poc_annual_net_therms_1st, ft * poc_annual_net_therms_2nd, RateG )
   fe = (NTGRkWh + MEB) / NTGRkWh,  ft = (NTGRTherm + MEB) / NTGRTherm   (1 when MEB = 0 or the NTG is 0)

fuel-substitution measure:   BillReduc = pos(PV)     BillIncr = pos(-PV)                 every other measure:   BillReduc = PV     BillIncr = 0
BillReducElec, BillIncrElec from the electric PV;    BillReducGas, BillIncrGas from the gas PV
```

A measure whose sector has no schedule mapping (or a year outside the schedule's range) gets no bill impact for that fuel.

---------------------------------------------------------------------------------------------------------------------------------

## C10 Gross variants

A "Gross" value is the same quantity **without net-to-gross** (and without the market-effects adder). Because every net value was scaled by NTG, gross = net / NTG:

```text
ElecBenGross{,_SB,_SH}            = ratio( ElecBen{...},            NTGRkWh + MEB )        ElecSupplyCostGross{...}  = ratio( ElecSupplyCost{...},  NTGRkWh + MEB )
GasBenGross{,_SB,_SH}             = ratio( GasBen{...},             NTGRTherm + MEB )      GasSupplyCostGross{...}   = ratio( GasSupplyCost{...},   NTGRTherm + MEB )
WaterEnergyBenGross{,_SB,_SH}     = ratio( WaterEnergyBen{...},     NTGRkWh + MEB )        WaterEnergyCostGross{...} = ratio( WaterEnergyCost{...}, NTGRkWh + MEB )
```

(`ratio` returns 0 when NTG + MEB = 0.) `OtherBenGross`, `OtherCostGross`, `TaxCreditsGross` are in C7 and `GrossParticipantCostPV` is in C8.
`TotalSystemBenefitGross`, `TRCCostGross`, `SCBCostGross` and `SCHCostGross` are in C12.

---------------------------------------------------------------------------------------------------------------------------------

## C11 Program costs and their weighting

`ProgramCost.csv` costs belong to a *program*, not a measure, and are spread over the program's measures in proportion to their positive benefits.

**C11.1 Program totals.** For each `ProgramCost` row `r` (with `Year` = the first four characters of its `ClaimYearQuarter`; a `PA` of `SW` is treated as `SCE`, what CET did in the real runs):

```text
T_r = AdminCostsOverheadAndGA + AdminCostsOther + MarketingOutreach + DIActivity + DIInstallation + DIHardwareAndMaterials + DIRebateAndInspection + EMV + UserInputIncentive
      + CostsRecoveredFromOtherSources                                      (blank = 0; OnBillFinancing is not part of CET's sum)

SumCosts       (program) = SUM_r T_r
SumCostsNPV    (program) = SUM_r T_r / Raf(PA_r) ^ (Year_r - F)                     annual discounting
SumCostsNPV_SCT(program) = SUM_r T_r / Raf_sct(PA_r) ^ (Year_r - F)
```

**C11.2 A measure's share.** Within a measure's program (`PrgID`), with `B_m = pos(ElecBen) + pos(GasBen) + pos(WaterEnergyBen) + pos(OtherBen)`:

```text
share_m = B_m / SUM over the program's measures of B      if that sum is not 0          share_m = 1 / (number of measures in the program)    if it is 0
```

Five further shares use other benefit sets and are computed the same way: `share_gross` (the Gross benefits), `share_SB` (`ElecBen_SB + GasBen_SB + WaterEnergyBen_SB + OtherBen`), `share_SH`, `share_SB_gross` and `share_SH_gross`.

```text
WeightedBenefits    = share_m
WeightedProgramCost = share_m * SumCosts                 nominal (undiscounted) dollars
```

**C11.3 Budget.** CET's `TotalExpenditures`, which summed over a run is the run receipt's **Budget** (checked to the cent on CET jobs 121219 and 121841):

```text
TotalExpenditures = RebatesandIncents + WeightedProgramCost          nominal dollars
```

---------------------------------------------------------------------------------------------------------------------------------

## C12 The cost tests and ratios

```text
Sup       = ElecSupplyCost + GasSupplyCost + WaterEnergyCost + OtherCost
B         = ElecBen + GasBen + WaterEnergyBen + OtherBen

TotalSystemBenefit      = B - Sup
TotalSystemBenefitGross = (ElecBenGross + GasBenGross + WaterEnergyBenGross + OtherBenGross) - (ElecSupplyCostGross + GasSupplyCostGross + WaterEnergyCostGross + OtherCostGross)
```

**Costs** (`NoAdmin` leaves out the program-level term `share * SumCostsNPV`):

```text
PACCostNoAdmin  = RebatesandIncentsPV + Sup                         PACCost  = share * SumCostsNPV + PACCostNoAdmin
TRCCostNoAdmin  = RebatesandIncentsPV + NetParticipantCostAdjustedPV + Sup  TRCCost  = share * SumCostsNPV + TRCCostNoAdmin
TRCCostGross    = share_gross * SumCostsNPV + RebatesandIncentsPV + GrossParticipantCostPV + SupGross          SupGross = the four Gross supply/other costs summed
RIMCostNoAdmin  = BillReducElec + BillReducGas + PACCostNoAdmin     RIMCost  = BillReducElec + BillReducGas + PACCost

SCBCostNoAdmin  = RebatesandIncentsPV_sct + NetParticipantCostAdjustedPV_sct + SupSB                      SupSB = ElecSupplyCost_SB + GasSupplyCost_SB + WaterEnergyCost_SB + OtherCost
SCBCost         = share_SB * SumCostsNPV_SCT + SCBCostNoAdmin
SCBCostGross    = share_SB_gross * SumCostsNPV_SCT + RebatesandIncentsPV_sct + GrossParticipantCostPV_sct + SupSBGross
SCHCost..., SCHCostNoAdmin, SCHCostGross: the same with _SH
```

**Ratios** (`ratio(n, d)`, so a zero cost gives 0):

```text
TRCRatio = (B + TaxCredits) / TRCCost               TRCRatioNoAdmin = (B + TaxCredits) / TRCCostNoAdmin
PACRatio =  B / PACCost                             PACRatioNoAdmin =  B / PACCostNoAdmin
RIMRatio = (B + BillIncrElec + BillIncrGas) / RIMCost     RIMRatioNoAdmin = (B + BillIncrElec + BillIncrGas) / RIMCostNoAdmin
SCBRatio = (ElecBen_SB + GasBen_SB + WaterEnergyBen_SB + OtherBen) / SCBCost                      SCBRatioNoAdmin = the same over SCBCostNoAdmin
SCHRatio = (ElecBen_SH + GasBen_SH + WaterEnergyBen_SH + OtherBen) / SCHCost                      SCHRatioNoAdmin = the same over SCHCostNoAdmin
```

Water-energy benefits are in every numerator and TSB; water-energy costs are in every cost. Supply costs are in the *cost*, not netted from the ratio's numerator.

**Participant Cost Test** (CA Standard Practice Manual, Chapter 2; the CET does not calculate it). From the participant's side, on gross savings and without market effects:

```text
BillReducElecGross ... BillIncrGasGross = C9's bill impacts with NTG = 1 and MEB = 0 (same rates, discounting and fuel-substitution split)
PCTBen   = BillReducElecGross + BillReducGasGross + RebatesandIncentsPV + TaxCreditsGross / PVF          (PVF of C8)
PCTCost  = GrossParticipantCostPV + RebatesandIncentsPV + BillIncrElecGross + BillIncrGasGross          (= the measure cost before incentives + bill increases)
PCTRatio = PCTBen / PCTCost                                                                              (0 when PCTCost = 0)
```

Not yet checked against a CPUC definition: CPUC has not published how the CET will calculate the test. The portfolio `PCT_ratio` is `SUM(PCTBen) / SUM(PCTCost)`.

---------------------------------------------------------------------------------------------------------------------------------

## C13 Where market effects enter

CET's run-level **Market Effects** adds `MEB` to the net-to-gross of benefits and `MEC` to the net-to-gross of costs. CET stores them as two settings (`MarketEffectBens`,
`MarketEffectCost`); this tool sets both from one option: "Apply 5% market effects (benefits and costs)" (`market_effects`, `--market-effects 0.05`) makes `MEB = MEC = 0.05`,
off makes both 0.

| Quantity | Effect |
|---|---|
| `ElecBen`, `ElecSupplyCost` and every `_SB`/`_SH` version | `NTGRkWh` becomes `NTGRkWh + MEB` (C5.1) |
| `GasBen`, `GasSupplyCost` | `NTGRTherm` becomes `NTGRTherm + MEB` (C5.4) |
| `WaterEnergyBen`, `WaterEnergyCost` | `NTGRkWh + MEB` (C5.5) |
| `OtherBen`, `TaxCredits` | `NTGRkWh + MEB` (C7) |
| `OtherCost` | `NTGRkWh + MEC` (C7) |
| `NetParticipantCostAdjustedPV` (and `_sct`), so the TRC and SCT costs | adds `MEC * (GrossMeasCostPV + ExcessIncentivesPV)` = `MarkEffectPlusExcessIncPV` (C8); `NetParticipantCostPV` itself does not change |
| `...Gross` columns | divided by `NTG + MEB`, i.e. the market-effects adder is undone along with NTG (C10) |
| bill impacts, RIM's bill terms | `NTGRkWh + MEB` / `NTGRTherm + MEB` (C9) |
| net emissions | `NTGRkWh + MEB` / `NTGRTherm + MEB` (C14) |
| combined 8,760 profile | **not** included (market effects are an accounting adder, not physical savings) |

**Validated** (2026-10-08) on CET jobs 121842 (0%) and 121841 (5%), the same 13-measure PG&E job: every column the setting changes in CET changes the same way here
(benefits, net emissions, bill impacts, TRC / SCT costs, participant costs; `NetParticipantCostPV`, the Gross columns and the PAC cost do not change in either), and the
participant-cost columns match CET to the cent. CET's run used its avoided-cost Version 2024, which this tool does not have, so benefit levels were compared as ratios.

---------------------------------------------------------------------------------------------------------------------------------

## C14 Emissions

Emission columns are produced when "Include emissions" is on. "First-year" is the measure's **first four quarters** of life (not the calendar year); "Lifecycle" is all of them; "Net" includes NTG (and `MEB`), "Gross" does not.
Units: **CO2 in metric tons** (confirmed; the ACC's rate is `tonnes/kWh` and `E3Settings.CO2Gas` = 0.00531 is metric tons per therm, so CET's SQL comments that say "lbs" for CO2 are wrong). NOx and PM10 are in **pounds** (confirmed).

**C14.1 Electric CO2 per annual kWh, by quarter** (`co2(qidx)`, `qidx = 4 * year + quarter`, quarter 1-4):

```text
co2(year, q) = SUM over hours i in quarter q of  shape_year(i) * rate(year, i)         rate = the ACC's emission_rate_tonnes_per_kwh (same zone / IOU routing as C3.1)
```

Quarters exist only for years present in the ACC file: life beyond its last year adds **no** emissions (unlike prices, which use the nearest year).

**C14.2 Kilowatt-hours in a quarter.** With `eul = EUL`, `rul = RUL` (0 if `RUL >= EUL`), `rulq = 4 * rul`, `eulq = 4 * eul`, quarter offset `k = qidx - install` where `install = 4 * Y0 + q0`, and per-unit `kWh1, kWh2`:

```text
single baseline (rulq = 0):   AnnualkWh(k) = 0 if kWh1 = 0;   kWh1 * (eulq mod 1) if k < eulq < k + 1 (the last part-quarter);   else kWh1
dual baseline:                kWh1 if rulq > k + 1;      kWh2 if rulq < k and eulq > k + 1;
                              kWh1*(rulq mod 1) + kWh2*(eulq - rulq)     if the RUL and the end of life fall in the same quarter;
                              kWh1*(rulq mod 1) + kWh2*(1 - rulq mod 1)  if only the RUL falls in it;      kWh2*(eulq - floor(eulq))  if only the end of life does;   else kWh2
quarters counted: install <= qidx < install + eulq
```

**C14.3 Assembly.**

```text
fe(net)  = Q * (NTGe + MEB, or 1 if gross) * IRe * RRe                      fg(net) = Q * (NTGt + MEB, or 1 if gross) * IRt * RRt

ElecCO2   = fe * SUM over counted quarters of  co2(qidx)      * AnnualkWh(k)
ElecNOx   = fe * SUM over counted quarters of  nox            * AnnualkWh(k) / 4          nox  = E3Emissions NOx for (PA, target sector, end use, zone)   (0 if there is no row)
PM10      = fe * SUM over counted quarters of  pm10           * AnnualkWh(k) / 4          pm10 = E3Emissions PM10, same key
   first-year: the first four counted quarters;   lifecycle: all of them

GasCO2    = fg * CO2Gas(PA) * G          GasNOx = fg * NOxfactor * G          NOxfactor = E3CombustionType NOx for the measure's CombustionType (0 if blank / unknown)
   G_firstyear = Th1 if rul >= 1;  rul*Th1 + (1 - rul)*Th2 if 0 < rul < 1 <= eul;  rul*Th1 + (eul - rul)*Th2 if rul < eul < 1;  Th1 if rul = 0 and eul > 1;  eul*Th1 if rul = 0 and eul <= 1
   G_lifecycle = rul*Th1 + (eul - rul)*Th2 if rul > 0;   eul*Th1 otherwise
```

`E3Emissions` covers only a slice of the (PA, sector, end use, zone) keys, so NOx and PM10 are 0 for most electric measures (this is CET's own behaviour). Output columns: `NetElecCO2`, `NetElecNOx`, `NetPM10`, `NetGasCO2`, `NetGasNOx`,
the five `Gross...` counterparts, and the ten `...Lifecycle` columns.

---------------------------------------------------------------------------------------------------------------------------------

## C15 Statewide allocation

With "Allocate statewide programs" on, each statewide program's rows are split across the four IOUs **before** any calculation, using CET's allocation factors: a table with one row per (`Year`, `ProgramID`) holding each IOU's
`GasFactor`, `ElectricFactor` and `WeightedFactor` (each set sums to 1 across PGE / SDGE / SCE / SCG), and a rule per field:

| Rule | `Measure.csv` fields | `ProgramCost.csv` fields |
|---|---|---|
| Electric factor (x `ElectricFactor`) | `UnitkW1stBaseline`, `UnitkWh1stBaseline`, `UnitkW2ndBaseline`, `UnitkWh2ndBaseline`, `UnitkWhIOUWater1stBaseline`, `UnitkWhIOUWater2ndBaseline` | - |
| Gas factor (x `GasFactor`) | `UnitTherm1stBaseline`, `UnitTherm2ndBaseline` | - |
| Weighted factor (x `WeightedFactor`) | the unit costs and benefits (`UnitMeaCost...`, `UnitDirectInstall...`, `UnitEndUserRebate`, `UnitIncentiveToOthers`, `UnitGasInfraBens`, `UnitRefrigCosts`, `UnitRefrigBens`, `UnitMiscCosts`, `UnitMiscBens`, `UnitTaxCredits`) | all ten cost fields and `OnBillFinancing` |
| Pass through (unchanged) | everything else | `PA`, `PrgID`, `PrgYear`, `ClaimYearQuarter` |

The factors are the built-in table, with the rows of a supplied SW allocation factors file replacing matching (`Year`, `ProgramID`) rows and adding new ones.
For every IOU with a non-zero factor, one row is made with `<IOU>_` prepended to `CEInputID` and `PrgID` (program-cost `PA` is set to that IOU). A measure is still priced on its own `IOU_AC_Territory`; a program's costs are shared
only among that IOU's measures and discounted at that IOU's rate. Without the option, statewide rows are priced as filed (the way the four real CET runs treated them). `CreditedIOU` is the `PA` of each measure's program in `ProgramCost.csv`: with the option, the IOU each statewide share was allocated to; without it, the filed `PA` (`SW` for a statewide program).

---------------------------------------------------------------------------------------------------------------------------------

## C16 Optional and secondary outputs

**C16.1 "Calculate ACC components separately".** Every fuel value in C5 is a sum over ACC components, so each component can be priced alone through exactly the same route:

```text
ElecValue_<c> = lifetime PV (TRC rows) of C5.1 with the component column c in place of total_avoided_cost      for every component c present in the electric ACC
GasValue_<c>  = lifetime PV (TRC rows) of C5.4 with the component c in place of the total                             c = market, td, nox, co2, upstream_methane, behind_meter_methane, air_quality_adder

SUM_c ElecValue_<c> = ElecBen - ElecSupplyCost        SUM_c GasValue_<c> = GasBen - GasSupplyCost          (checked on every run and reported in the log)
```

`behind_meter_methane` is zero for a measure that does not include it (C3.2). The components are reported *before* the benefit / supply-cost split, so they always add up.
`components_summary_*` totals each `ElecValue_*` / `GasValue_*` over the portfolio with its share of the fuel total and a check row (total minus the sum of components).

**C16.2 "Produce combined 8760 electric load profile".** For each requested calendar year `Y`, the net kWh the portfolio delivers in each hour:

```text
kWh(i, Y) = SUM over measures m of  weight_m(Y) * shape_m,Y(i)
weight_m(Y) = overlap( [Y, Y+1), 1st-baseline period ) * S1e + overlap( [Y, Y+1), 2nd-baseline period ) * S2e
   start = Y0 + (q0 - 1)/4     end = start + EUL     1st-baseline period = [start, start + RUL) (or [start, end) if RUL = 0 or RUL >= EUL)     2nd = [start + RUL, end)     overlap in years
```

`S1e`, `S2e` are `poc_shape_scale*` (net, with IR and RR, without market effects). Each measure's 8,760 profile (supplied or library) is aligned to the ACC's weekdays (C4.2), and the `datetime` column labels the hours on the ACC's calendar (2018 or 2020); a measure with no shape is left out and listed in `portfolio_8760_unshaped_*`. Positive = savings, negative = added load. `net_kwh_<Y>` sums all IOUs, `net_kwh_<Y>_<PA>` by
territory.

**C16.3 Portfolio summary.** `portfolio_summary_*` has one row for all measures, one per `IOU_AC_Territory` (`poc_PA`), one per `CreditedIOU` and one per program segment (`Prg_ProgramSegment`, from the program metadata), with the sum of each of: `ElecBen`, `GasBen`, `WaterEnergyBen`, `OtherBen`, `ElecSupplyCost`, `GasSupplyCost`,
`WaterEnergyCost`, `TotalSystemBenefit`, `TRCCost`, `PACCost`, `RIMCost`, `SCBCost`, `SCHCost`, `TRCCostNoAdmin`, `PACCostNoAdmin`, `WeightedProgramCost`, `BillReducElec`, `BillReducGas`, `PCTBen`, `PCTCost`, `NetElecCO2`, `NetGasCO2`, and portfolio ratios on CET's receipt definition (plus `PCT_ratio = sum(PCTBen) / sum(PCTCost)`):

```text
B      = sum(ElecBen) + sum(GasBen) + sum(WaterEnergyBen) + sum(OtherBen)            B_SB, B_SH: the same with the _SB / _SH benefits
TRC_ratio = (B + sum(TaxCredits)) / sum(TRCCost)         PAC_ratio = B / sum(PACCost)         RIM_ratio = (B + sum(BillIncrElec) + sum(BillIncrGas)) / sum(RIMCost)
SCB_ratio = B_SB / sum(SCBCost)                          SCH_ratio = B_SH / sum(SCHCost)      TRC_NoAdmin_ratio, PAC_NoAdmin_ratio: over the NoAdmin costs
```

A ratio is omitted when its inputs are blank (for example the societal ratios when SCB/SCH are off).

**C16.4 Comparison outputs.** *Two ACC versions* (`comparison_acc<A>_vs_acc<B>`): for each key output `c`, per measure `c_<A>`, `c_<B>`, `c_delta = B - A` and `c_pct = (B - A) / |A|` (blank if `A = 0`); the summary gives each metric's portfolio total under both and the
difference. *Against a CET outputs file* (`cet_comparison_*`): per measure `c_poc`, `c_cet`, `c_diff = poc - cet` and `c_ratio = poc / cet`; the summary gives the portfolio totals and `POC/CET`.

---------------------------------------------------------------------------------------------------------------------------------

## C17 Validation, deliberate differences from CET, and limitations

**Validation.** *These figures were measured before 2026-10-02, with the TOU and SCG-table routes and CEDARS' calendar shift; the tool now prices every
measure on its 8,760 with the 2018 alignment of C4.2 and so departs from CET deliberately (difference 7 below).* The reference is four real CET runs of PG&E's consolidated 2028-2031 portfolio (runs 121219-121222; 21,370 measures; 2024 ACC = CET Version 2026; statewide programs unallocated; First Year = claim year), priced by the tool with
no CET-derived table in the calculation path. Portfolio total, POC / CET, pooled over the four runs (the per-run values differ from these by up to about 0.01, and by up to 0.04 for the societal water values):

| Output | POC / CET | Note |
|---|---|---|
| `GasBen`, `OtherBen`, `RebatesandIncentsPV`, `NetParticipantCostPV`, `WeightedProgramCost` | 1.0000 | |
| `BillReducElec`, `BillIncrElec` | 1.0000 | every measure within 1% (median difference under 0.001%), including dual-baseline and fractional-life measures (within about $1) |
| `BillReducGas` | 0.9995 | |
| `GasBen_SB`, `GasBen_SH` | 1.0000 | |
| `ElecBen` | 0.9821 | of which 1.4% is 96 measures the tool cannot price (below); the rest is a 0.2-0.4% residual |
| `WaterEnergyBen` | 0.9825 | exact where CET's own SCG table is used, so the formula itself is pinned |
| `ElecSupplyCost` | 0.9666 | |
| `TotalSystemBenefit` | 0.9911 | |
| `TRCCost` / `PACCost` / `RIMCost` | 0.9995 / 0.9971 / 0.9998 | |
| `SCBCost`, `SCHCost` | 0.9998 / 1.0000 | |
| portfolio `TRC` / `PAC` / `RIM` ratio (receipt definition) | 0.9913 / 0.9937 / 0.9914 | TRC 1.6013 vs CET 1.6153 |
| `NetElecCO2` | 0.9793 | first year |
| `NetElecCO2Lifecycle` | 0.9191 | |
| `NetGasCO2`, `NetElecNOx`, `NetGasNOx`, `NetPM10` (net, first year) | 1.0000 | |
| `ElecBen_SB`, `ElecBen_SH` | 0.8754 / 0.8780 | **open**: the shortfall is concentrated in SCE TOU-priced measures (which carry ~95% of the electric benefit), whose TRC value is within 0.4%; cause not yet identified |
| `WaterEnergyBen_SB`, `_SH` | 1.0240 / 1.0376 | CET holds `_SB` = `_SH` here (see below) |

Per measure, `GasBen` agrees exactly and `BillReducElec` to within 0.001% (median); 75% of measures are within 1% on `TRCCost`; `ElecBen` is within 5% for 81%.

**Deliberate differences from CET** (the tool does not copy what appear to be CET data errors; each is logged in the project notes):

1. The Gross cost columns (`TRCCostGross`, `SCBCostGross`, `SCHCostGross`) use *gross* supply costs; CET adds the net supply costs to the gross participant cost.
2. `GasSupplyCostGross` is the net value divided by NTG; CET assigns it the net value.
3. `SCBCost` / `SCHCost` discount program costs at the societal rate; CET uses the primary rate for them.
4. Electric `_SB` / `_SH` are priced on the real `SCT Base` / `SCT High` rows for every measure. For legacy DEER-named end uses CET's table holds TRC values in its SCT columns (so CET's SB = SH there).
5. `WeightedElecAlloc` (which CET computes with a `_SB`/`_SH` mix-up) is not produced.
6. Unallocated statewide rows use `PA = SCE` for discounting, as CET did in the real runs; the effect is nil when every cost falls in the First Year.
7. Every electric measure is priced from an 8,760 with the 2018 calendar alignment of C4.2 (2026-10-02). CET instead prices TOU end uses from CEDARS' TOU calculators (Gen at
   full-year period means; T&D on kW with a duplicate /1000, about 1,000 times too small) and shifts other profiles to each forecast year's weekdays with a one-day error on
   negative offsets; it prices SCG from its own quarterly table.

**Limitations.**

* **Unpriced measures.** An electric measure with no supplied profile and no library profile is reported `UNPRICED` and its benefit is 0. Gas measures with a blank or unknown profile name are unpriced the same way (CET also prices those at 0).
* Only the four IOUs are valid `IOU_AC_Territory` values.
* SCG electric is priced on the SCG routing of C3.1: one utility's whole bundle per zone, SCE unless SCE has no data for the zone (then PG&E). This matches CEDARS'
  DEER-path SCG values in zones 4, 5 (PG&E) and 13 (SCE); CEDARS' own SCG hourly files instead pair SCE generation with PG&E T&D in zones 4, 5 and 13, so the POC
  differs from them there. CET's quarterly SCG table is not used.
* Not validated against CET, because the real runs contain none: **market effects at 5%**, **`WaterEnergyCost`** and second-baseline water, the **2021, 2022 and 2026 ACCs** (no CET run has used them), and statewide allocation of program costs.
  These paths are covered by the hand-computed tests in `tests`.
* The gas ACCs are extracted from E3's workbooks, not taken from CET. The project notes record an unexplained 0.7-2.9% gap between the workbook's gas Commodity and CET's own gas-table export; it does not show in the real runs, where `GasBen` agrees to 4 decimals.
* Emissions beyond the last ACC year are not counted (C14.1), and NOx/PM10 come from CET's `E3Emissions`, which covers only part of the keys.
* Input columns that are read but not used: `RateScheduleElec`, `RateScheduleGas` (rate schedules always come from `PA` and sector), and every descriptive column (INPUT_FIELDS.md lists them).
* CET output fields not produced: the savings-quantity columns (`FirstYear/Lifecycle/Annual` Gross/Net `KWh`, `KW`, `Thm`, `KWhWater`, `KWhSite`, `DiscountedSavings...`), `WeightedSavings`, `WeightedElecAlloc`, `WtdAdminCosts...`,
  `ProgramCosts`, `TotalExpenditures`, `TRCLifecycleNetBen`, `PACLifecycleNetBen`, and the levelized `Lev...` columns.

---------------------------------------------------------------------------------------------------------------------------------

## C18 Worked example

One measure, priced against constant (flat) avoided costs and a flat profile so that every step can be checked with a calculator. The job is exactly the one in `tests/test_docs.py`, which re-runs it and checks the numbers below.

**Inputs.** A PG&E residential measure, climate zone 13, claimed in **2028Q3**, First Year 2028, 2026-ACC run parameters (`DiscountRateQtr` = 0.01915, `DiscountRateAnnual` = 0.0766, societal 0.012575 / 0.0503).

| `Measure.csv` field | Value | `Measure.csv` field | Value |
|---|---|---|---|
| `NumUnits` (Q) | 100 | `UnitMeaCost1stBaseline` | 200 |
| `EUL_Yrs` / `RUL_Yrs` | 2.4 / 0 | `UnitIncentiveToOthers` / `UnitEndUserRebate` | 30 / 20 |
| `UnitkWh1stBaseline` | 500 | `UnitDirectInstallLab` / `UnitDirectInstallMat` | 5 / 5 |
| `UnitTherm1stBaseline` | 20 | `UnitRefrigCosts` / `UnitMiscBens` / `UnitTaxCredits` | 1 / 2 / 5 |
| `UnitkWhIOUWater1stBaseline` | 3 | `NTGRkWh` / `NTGRTherm` / `NTGRCost` | 0.8 / 0.9 / 0.9 |
| `E3GasSavProfile` | `Annual` (1/12 per month) | `InstallationRatekWh` / `RealizationRatekWh` | 0.95 / 0.9 |
| shape | flat (1/8760 per hour) | `InstallationRateTherm` / `RealizationRateTherm` | 1 / 0.9 |

ACC: electric $80/MWh (TRC) = energy 70 + transmission 5 + distribution 5, so `AC` = $0.080/kWh and `Gen` = $0.070/kWh; `SCT Base` $88, `SCT High` $96 (Gen 78 / 86). Gas $1.00/therm (TRC), $1.20 (SCT Base), $1.40 (SCT High). Emission rate 0.0002 t/kWh.
Program `PRG1`: 2028 costs 1,500 (admin) + 500 (marketing); 2029 costs 1,000 (EMV). No market effects.

**C1 derived fields.** `poc_shape_scale` = 100 x 500 x 0.8 x (0.95 x 0.9) = **34,200 kWh/yr**; `poc_annual_net_kwh` = 100 x 500 x 0.8 = 40,000 (net of NTG only); `poc_annual_net_therms_1st` = 100 x 20 x 0.9 x (1 x 0.9) = **1,620 therms/yr**;
`poc_water_scale_1st` = 100 x 3 x 0.8 x 0.855 = **205.2 kWh/yr**.

**C2 quarters.** EUL 2.4 years = 9.6 quarters, so ten quarters (k = 0-9), the last weighted 0.6. Claim quarter 3, so k = 0 is 2028Q3. `DF(k) = 1 / 1.01915^Qac`, `Qac = (year - 2028) x 4 + quarter - 1`.

**C5 quarter by quarter** (electric and water: annual kWh x $/kWh x hours/8760; gas: annual therms x $1.00 x months/12):

<!-- worked-example-quarters:start -->
| k | Quarter | Hours | Weight | DF | Electric $ (`V_elec`) | Gas $ (`V_gas`) | Water $ (`V_water`) |
|---|---|---|---|---|---|---|---|
| 0 | 2028Q3 | 2208 | 1.0 | 0.962773 | 663.95 | 389.92 | 3.49 |
| 1 | 2028Q4 | 2208 | 1.0 | 0.944682 | 651.47 | 382.60 | 3.42 |
| 2 | 2029Q1 | 2160 | 1.0 | 0.926931 | 625.34 | 375.41 | 3.28 |
| 3 | 2029Q2 | 2184 | 1.0 | 0.909514 | 620.40 | 368.35 | 3.26 |
| 4 | 2029Q3 | 2208 | 1.0 | 0.892424 | 615.44 | 361.43 | 3.23 |
| 5 | 2029Q4 | 2208 | 1.0 | 0.875655 | 603.87 | 354.64 | 3.17 |
| 6 | 2030Q1 | 2160 | 1.0 | 0.859202 | 579.64 | 347.98 | 3.04 |
| 7 | 2030Q2 | 2184 | 1.0 | 0.843057 | 575.07 | 341.44 | 3.02 |
| 8 | 2030Q3 | 2208 | 1.0 | 0.827216 | 570.47 | 335.02 | 2.99 |
| 9 | 2030Q4 | 2208 | 0.6 | 0.811672 | 335.85 | 197.24 | 1.76 |
<!-- worked-example-quarters:end -->

Sums: `V_elec` = 5,841.50, `V_gas` = 3,454.03, `V_water` = 30.67. (Row 0: 34,200 x 0.080 x 2208/8760 x 0.962773 = 663.95; gas row 0: 1,620 x 1.00 x 3/12 x 0.962773 = 389.92; water row 0: 205.2 x 0.070 x 2208/8760 x 0.962773 = 3.49.)

**C6 / C7 / C8 / C9.** Not a fuel-substitution measure, so `ElecBen` = `V_elec`, `GasBen` = `V_gas`, all supply costs 0; `Wt1 > 0` so `WaterEnergyBen` = `V_water`. `OtherBen` = 100 x (0.8) x (0 + 0 + 2) = 160; `OtherCost` = 100 x 0.8 x 1 = 80; `TaxCredits` = 100 x 0.8 x 5 = 400.
Qm = (2028 - 2028) x 4 + 2 = 2, `PVF` = 1.01915^2 = 1.038672. `IncentsDI` = 100 x 40 = 4,000, `Rebates` = 2,000, so `RebatesandIncentsPV` = 6,000 / 1.038672 = 5,776.64. `GrossMeasCostPV` = 100 x 200 / 1.038672 = 19,255.4;
excess = pos(4,000 / 1.038672 - 19,255.4) = 0; `GrossParticipantCostPV` = 19,255.4 - 5,776.64 = 13,478.82; `NetParticipantCostPV` = 0.9 x 13,478.82 = 12,130.94. Bill impact (C9): yr1 = 4, floor_rul = 2, boundary share 1, three billed years;
`RateE` = 0.406249518 / 0.418437003 / 0.430990113 $/kWh for 2028 / 2029 / 2030, so `BillReducElec` = 34,200 x 0.406249518 + 34,200 x 0.418437003 / 1.0766 + 0.4 x 34,200 x 0.430990113 / 1.0766^6 = 13,893.73 + 13,292.5 + 3,786.6 = 30,972.49.

**C11 / C12.** One measure, so its share is 1: `WeightedBenefits` = 1, `WeightedProgramCost` = 2,000 + 1,000 = 3,000; `SumCostsNPV` = 2,000 + 1,000 / 1.0766 = 2,928.85.
`TotalSystemBenefit` = (5,841.50 + 3,454.03 + 30.67 + 160) - (0 + 0 + 0 + 80) = 9,406.19. `PACCostNoAdmin` = 5,776.64 + 80 = 5,856.64; `PACCost` = 2,928.85 + 5,856.64 = 8,785.49.
`TRCCostNoAdmin` = 5,776.64 + 12,130.94 + 80 = 17,987.57; `TRCCost` = 20,916.42; `TRCRatio` = (9,486.19 + 400) / 20,916.42 = 0.4727 (B = 9,486.19 is the four benefits summed).

**Results of the run:**

<!-- worked-example-outputs:start -->
| Output | Value | Output | Value |
|---|---|---|---|
| `ElecBen` | 5,841.50 | `TotalSystemBenefit` | 9,406.19 |
| `GasBen` | 3,454.03 | `WeightedProgramCost` | 3,000.00 |
| `WaterEnergyBen` | 30.67 | `PACCostNoAdmin` | 5,856.64 |
| `OtherBen` | 160.00 | `PACCost` | 8,785.49 |
| `OtherCost` | 80.00 | `TRCCostNoAdmin` | 17,987.57 |
| `TaxCredits` | 400.00 | `TRCCost` | 20,916.42 |
| `RebatesandIncentsPV` | 5,776.64 | `RIMCost` | 49,925.68 |
| `GrossParticipantCostPV` | 13,478.82 | `SCBCost` | 21,172.997 |
| `NetParticipantCostPV` | 12,130.94 | `SCHCost` | 21,172.997 |
| `BillReducElec` | 30,972.49 | `TRCRatio` | 0.4727 |
| `BillReducGas` | 10,167.71 | `PACRatio` | 1.0798 |
| `ElecBen_SB` | 6,688.34 | `RIMRatio` | 0.1900 |
| `ElecBen_SH` | 7,296.37 | `SCBRatio` | 0.5289 |
| `GasBen_SB` | 4,314.29 | `SCHRatio` | 0.5917 |
| `GasBen_SH` | 5,033.34 | `NetElecCO2` | 6.84 |
| `WaterEnergyBen_SB` | 35.57 | `NetElecCO2Lifecycle` | 16.438 |
| `WaterEnergyBen_SH` | 39.22 | `NetGasCO2` | 8.6022 |
| `ElecBenGross` | 7,301.87 | `NetGasCO2Lifecycle` | 20.645 |
| `GasBenGross` | 3,837.81 | `TotalSystemBenefitGross` | 11,278.01 |
| `RebatesandIncentsPV_sct` | 5,851.90 | `TRCCostGross` | 22,284.30 |
<!-- worked-example-outputs:end -->

Emissions by hand (C14): first year = the first four quarters, hours 2208 + 2208 + 2160 + 2184 = 8,760, so `NetElecCO2` = 68.4 x 0.0002 x 500 x 8,760/8,760 = 6.84 (68.4 = Q x NTG x IR x RR = 100 x 0.8 x 0.855); the lifecycle sum
covers 21,052.8 hours, giving 16.438. `NetGasCO2` = 100 x 0.9 x 0.9 x 0.00531 x 20 = 8.6022 and lifecycle uses 2.4 x 20 = 48 therms: 20.645.
The societal values follow C5.6: for example `ElecBen_SB` reprices the same quarters at $0.088/kWh and `DF_sct` = 1 / 1.012575^Qac.

---------------------------------------------------------------------------------------------------------------------------------

## C19 First-year net savings

CET's first-year net savings (CalcSavings, "First Year Net"), quantities rather than dollars:

```text
FirstYearNetKWh = Q * (NTGRkWh + MEB) * IRe * RRe * ( w1 * (kWh1 + Wt1) + w2 * (kWh2 + Wt2) )
FirstYearNetKW  = Q * (NTGRkW  + MEB) * IRkw * RRkw * ( w1 * kW1 + w2 * kW2 )
FirstYearNetThm = Q * (NTGt + MEB) * IRt * RRt * ( w1 * Th1 + w2 * Th2 )

w1 = 1 if RUL > 1;  RUL if 0 < RUL < 1;  EUL if RUL = 0 and EUL < 1;  1 otherwise
w2 = EUL - RUL if 0 < RUL < 1 and EUL < 1;  1 - RUL if 0 < RUL < 1 and EUL >= 1;  0 otherwise
```

`EUL` here is `EUL_Yrs` as filed (no 0.25-year floor). An RUL of exactly 1 takes the "otherwise" branches, as CET's SQL does. Checked against CET's own columns on jobs
121219, 121842 and 121841 (0% and 5% market effects): equal to rounding for every measure.

