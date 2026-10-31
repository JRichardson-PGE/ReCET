# Cost tests as configuration

ReCET's cost tests and value streams are defined in a versioned **cost-test definition set**, a YAML file shipped in the package, and every run records
the set it used (BRD v0.5 BR-06.8: "cost tests and value streams as configuration, so new tests can be added without code changes"; BR-06.14 and D14:
non-energy benefits). This page is the repository's statement of the design; the module is `recet/cost_test_sets.py`.

* [1. What a set is](#1-what-a-set-is)
* [2. The parts of a set](#2-the-parts-of-a-set)
* [3. How a set is read](#3-how-a-set-is-read)
* [4. Which set a run uses, and how it is recorded](#4-which-set-a-run-uses-and-how-it-is-recorded)
* [5. Custom sets](#5-custom-sets)
* [6. What stays in code](#6-what-stays-in-code)
* [7. Build steps](#7-build-steps)

---------------------------------------------------------------------------------------------------------------------------------

## 1. What a set is

A set is one file, `recet/data/cost_tests/<id>/definition.yaml`, with a `CHANGES.md` beside it. Its id is `<workstream>-<first program year>`: `EE-2026`
is the energy-efficiency set, and another workstream (for example income-qualified programs, IQP) adds its own sets under its own prefix. A draft carries
a lower-case tail (`EE-2028-draft`) and `draft: true`.

The first set, **EE-2026**, is the TRC, PAC, RIM, SCB, SCH and PCT ReCET has priced since 0.1.53, written out as data, with the four suspected CalcCE
errors still not copied (CALCULATIONS C17). It is in force for every program year until a later EE set's `program_years.from`.

A published set is never edited: a change is a new set (a new folder), so a run on an old set can always be repeated from the package alone, as the ACC
tables and run parameters can. `recet cost-tests list` lists the sets shipped, `recet cost-tests show EE-2026` writes a set out as formulas, and
`recet cost-tests check FILE` checks a file.

`data/cost_tests/cost_test_set.schema.json` (JSON Schema 2020-12) describes the file for other tools; `cost_test_sets.validate` checks the same rules
and the cross-references a schema cannot express (section 3).

## 2. The parts of a set

| Part | Fields | Meaning |
|---|---|---|
| header | `schema_version` (1), `id`, `workstream`, `version`, `label`, `description`, `draft`, `program_years` (`from`, `to`; null = open) | What the set is and the program years it is in force for. An open `to` ends the year before the next published set of the workstream starts. |
| `pricings` | `acc_rows` (`trc`, `sct_base`, `sct_high`), `discount_rate_quarterly`, `discount_rate_annual` (run-parameter columns), `suffix`, `participant_suffix` | How a stream is priced for a test, and how its column is named: `ElecBen_SB`, `RebatesandIncentsPV_SCT`. |
| `streams`, `source: engine` | `kind` (benefit, cost), `fuel` (elec, gas, water, none), `pricings`, `naming` (suffix, participant_suffix, none), `gross` (divide_ntg, same, or the gross column), `internal`, `unit`, `definition` | A column the pricing produces. `fuel` picks the NTG a Gross form divides out (water energy uses the kWh NTG). |
| `streams`, `source: input` | `columns` (Measure fields; blank = 0), `columns_2nd`, `basis` (`one_time`, `annual_over_life`), `ntg` (kwh, therm, none), `market_effects` (benefits, costs, none), and the engine fields | NumUnits x (NTG + market effects) x the per-unit values. `OtherBen`, `OtherCost` and `TaxCredits` are written this way in EE-2026; a non-energy benefit will be the same kind of entry. |
| `streams`, `source: program` | `unit`, `definition` | The ProgramCost.csv costs, discounted annually at the test's pricing rate and shared out by the test's share rule (C11). |
| `aggregates` | `pricing`, `plus`, `minus`, `gross` | A signed sum written as its own column: `TotalSystemBenefit` (and `TotalSystemBenefitGross`). |
| `tests` | `label`, `pricing`, `benefits`, `numerator_adders`, `costs`, `program_costs` (`none` or `{share: positive_benefits \| <test>}`), `variants` (NoAdmin, Gross), `lifecycle_net_benefit`, `levelized`, `headline`, `filing`, `definition` | `<T>Ratio = <T>Ben / <T>Cost`; see section 3 for the columns a test writes. |

A test writes `<T>Ben` (benefits plus numerator adders), `<T>Cost` (costs plus its share of the program costs), `<T>CostNoAdmin` and `<T>RatioNoAdmin`
with the NoAdmin variant, `<T>CostGross` with the Gross variant, `<T>Ratio`, `<T>LifecycleNetBen` with `lifecycle_net_benefit`, and for each entry of
`levelized` (`""` and / or `NoAdmin`) `Lev<T>Cost<v>`, `LevNetBen<T>Elec<v>` and `LevNetBen<T>Gas<v>`. For EE-2026 these are exactly the columns of
OUTPUT_FIELDS.md 1.8 to 1.10.

## 3. How a set is read

* A stream named under a test is taken in that test's pricing: `ElecBen` under SCB is `ElecBen_SB`, a participant PV under SCB its `_SCT` form, and
  `OtherBen` (naming none) is the same column in every pricing. A stream the pricing does not have is an error when the set is loaded, never a silent zero.
* `program_costs: {share: positive_benefits}` shares the program costs by the positive parts of the test's own benefits (not its numerator adders) in its
  own pricing, over the program's (1 / the program's measure count when they sum to 0): CALCULATIONS C11.2. `share: TRC` reuses that test's share (it
  must compute its own, in the same pricing). `NoAdmin` leaves the program costs out, so it needs a test with program costs.
* The Gross variant replaces every stream by its gross form and shares on the gross benefits; every stream of such a test must have a gross form.
* The portfolio ratio of a test is the sum of its `<T>Ben` over the sum of its `<T>Cost`.
* `headline` marks the tests the run page, key metrics and New run summary show (TRC and PAC in EE-2026); `filing` (default true) marks the tests a filing
  template may require.

## 4. Which set a run uses, and how it is recorded

| Where | What |
|---|---|
| Run option | `cost_test_set` (INPUT_FIELDS.md section 3): a built-in id, or the path of a set's YAML file. CLI `--cost-tests`; New run's "Cost tests" select (and a Cost-test set upload under Supplied files); the desktop window's Cost tests box. |
| Default | Blank = the built-in set in force for the job's first program year (`cost_test_sets.in_force`), so a 2027 claims job and a 2028 one can price on different sets without anyone choosing. |
| Program years | A set that is not in force for every program year of the job is a warning (`cost_tests_not_for_program_year`, in the log and the run's notes), never a stop, as the ACC check is (BR-06.5). |
| Measure level | `Used_CostTestSet`, after `Used_ACCVersion`: the id, `custom: <file name>` for a supplied set, ` (draft)` at the end for a draft. |
| `run_settings.json` | the `cost_tests` block (OUTPUT_FIELDS 2.9): id, workstream, version, label, draft, source, the file's SHA-256, program years, tests, how it was chosen, warnings. |
| Worker | a job spec names a built-in set in `settings.cost_test_set`, or supplies a file in `inputs.cost_tests` (pinned by SHA-256 like any input); `receipt.json` has the same `cost_tests` block. A spec whose set cannot be used is refused (exit 2). |
| CET replicas | a replica's tests are its release's own: it refuses `cost_test_set`, and its run_settings.json says `cost_tests: fixed by CET <release>`. |
| Regression gate | the fixtures' manifest records the default set's id and SHA-256; `recet regress check` fails when the default set changed and the fixtures were not rebuilt with `--update`. |

## 5. Custom sets

Filings use the CPUC's set, which users cannot edit; anyone running ReCET by itself may price on a set of their own (Jake, 2026-10-31). So:

* A run with CEDARS filing checks (`filing_validation`) or priced for a filing (`acc_purpose`) accepts only a **published built-in** set: a supplied
  file or a draft set is refused before anything is priced, with a message naming the built-in sets.
* Any other run may name a set file. Its `Used_CostTestSet` names the file, and its SHA-256 is in run_settings.json, so the run says exactly what it priced.
* A draft built-in set is labelled `(draft)` in every output and noted, as a draft ACC is (BR-11.8).

## 6. What stays in code

The engine still prices every stream (CALCULATIONS C5 to C9, C14, C19): a set only says how priced streams are added, shared and divided, and
`pipeline/test_evaluator.py` is the one place that adds, shares and divides them. The
program-cost NPV, the share mechanics (positive parts, per-program totals, the 1 / count fallback, totals over the parts of a large job), the
`Weighted*` / `Wtd*` / `TotalExpenditures` columns (CET's program-cost components, on the headline test's share), the levelized formula and the CET
replicas stay code.

## 7. Build steps

1. **Sets and pinning** (0.1.83): this page, the EE-2026 set, the schema, `cost_test_sets.py`, the run option, `Used_CostTestSet`, the run_settings.json
   and receipt block, the job spec, filing-run and draft handling, the program-year warning. The arithmetic is unchanged: `pipeline/cost_tests.py` still
   prices, and a test checks that every column the set names is a column a run writes.
2. **The evaluator** (0.1.84): `pipeline/test_evaluator.py` prices every aggregate and test of the run's set (`cost_tests.compute_cost_tests(...,
   cset)`); 0.1.81's arithmetic is kept, frozen, as `tests/reference_cost_tests.py`, and tests/test_test_evaluator.py holds EE-2026 to it within 1e-9 on
   every column (in one piece and in parts, with and without program costs, market effects, zero NTGs). The PCT is the set's (the engine writes only
   its participant streams, `TaxCreditsGrossPV` among them, internal); CALCULATIONS C12 carries `test_evaluator.formulas` of the set in force, checked by a
   test. The measure-level columns now follow the set: the aggregates, the `Weighted*` block, then each test's columns together (OUTPUT_FIELDS 2.1).
3. **Consumers from the set**: the portfolio summary ratios, Results, key metrics, field attributes and definitions, output templates, allocation default
   rules and the web pages take their test list from the run's set.
4. **Input streams**: the `one_time` and `annual_over_life` bases priced from the set, template / editor / preflight support for the Measure columns a set
   names, and a synthetic set with a non-energy benefit and a jurisdiction-specific test.
5. **CEDARS platform**: the reference-data pack kind `cost_tests`, its admin page, the set in the job spec and on the run record, and the filing template's
   required set.
