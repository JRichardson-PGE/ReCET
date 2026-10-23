# ReCET

ReCET ("reset") is the rebuilt CEDARS Cost Effectiveness Tool: it prices CET-format inputs (Measure / ProgramCost, or one Excel job workbook)
hour by hour on the ACC and writes CET's outputs per measure, with a tkinter GUI (`python -m recet`) and a CLI (`recet run|preflight|template|check|data`).
It was moved out of the CEDARS review research repository (`JRichardson-PGE/CEDARS-Research`, folder `poc/`, package then called `cedars_poc`) on
2026-10-09 with a fresh history. The research behind it (ACC extraction and validation, CEDARS quarterly-avoided-cost reproduction, CET error findings,
TOU work) stays in that repo; its CLAUDE.md and `poc/README.md` hold the evidence for the methodology.

## Rules that keep the tool honest

- The docs in `recet/docs/` are checked by tests. Change a formula and you change `CALCULATIONS.md`, the field's entry in `OUTPUT_FIELDS.md`, and
  `tests/reference_calc.py` (an independent loop implementation) with it. `tests/test_output_docs.py` re-runs the worked examples in `tests/doc_examples.py`.
- Pricing arithmetic exists twice: the grouped versions (`engine.price_real_hourly`, `price_gas_benefit`) and the per-measure references
  (`..._per_measure`). Change both; the tests compare them.
- Reproduce CET's real behaviour unless a deliberate, documented exception says otherwise (e.g. SCB/SCH priced on the real SCT rows, not CET's TRC copy).
- Large files are never in git: the electric ACC parquets (2021, 2022, 2024, 2026) and the ~9 GB impact-profile library live in the *reference data folder*
  (`recet data status`, `RECET_DATA`, `~/.recet/config.json`). Small tables ship in `recet/data/`.
- Research-only code does not belong here. On 2026-10-09 the first proof of concept's pieces (synthetic shapes and pricers, the first-generation ACC
  loaders, the TOU quarterly profiles, the research datasets, the old `run_poc` command line) moved to the research repo's `poc/research_lib`, and
  the retired TOU/SCG quarterly routes, the CET-CO2 comparison option and the research-checkout file fallback were removed. Research scripts in
  CEDARS-Research import `recet` for the tool's functions and `research_lib` for the rest.
- The Edit inputs tab is for small jobs (up to 500 measures); larger files are edited offline.
- Tests: `pip install -e ".[test]"` then `pytest` from the repo root; they need no reference data.
- ACC versions are keyed by ACC year (2021, 2022, 2024, 2026), and run parameters / E3 tables are picked by ACC version. Users see both names (BRD BR-11.7,
  Jake 2026-10-22): the ACC version (the year E3 released the calculator) and CET's Avoided Cost Version (the first year it was in effect for claims or
  forecasts), e.g. "2024 ACC (Avoided Cost Version 2026)"; the 2026 ACC is Avoided Cost Version 2028 although CET's draft tables number it 2027
  (`versions.acc_label` / `AVOIDED_COST_VERSION`; tables are still read through `versions.CET_VERSION_TO_ACC`). Inside the packaged CET exports CET's
  numbers are mapped on read.
- Each ACC's hourly data follow one calendar's weekdays: 2018 for 2024/2026, 2020 for 2021/2022 (read from the file's hour stamps,
  `real_impact_profiles.acc_calendar_year`). An ACC year always has 8,760 hours (the last day of a leap year is dropped); never add leap-day handling.
- The 2021 and 2022 ACCs are TRC only: SCT columns are left blank. ReCET keeps its own behind-the-meter methane rule (Residential fuel-substitution
  profiles only) on every vintage, although CET's Version 2024 adds it to all Residential gas and Version 2022 never does.

- Optional ReCET-only Measure columns (not CET fields; `templates.RECET_MEASURE_COLUMNS`) follow CET's 74: `AC_Elec_Impact_Profile_2nd_Baseline` (2nd-baseline shape,
  priced as a second part, `run_poc.split_by_baseline_shape`), `AC_Elec_Impact_Profile_Water` (water-energy shape) and `ACCVersion` (an ACC version per
  measure). Blank = the default. Editor CET files include them only when used.
- Seven Measure fields are renamed from their E3-calculator names (Jake, 2026-10-22): `recet/fieldnames.py`. Inputs may use either name (read under the new
  name by `pipeline/delimited.read_cet_csv`); outputs, templates and workbooks use the new names; the editor's CET files use CET's names (`to_cet`).
- ReCET's copies of CET tables drop CET's E3 prefix (Jake, 2026-10-23): `data/run_parameters.csv` (CET E3Settings) and `data/cet_tables/*.parquet`
  (emissions, combustion_type, rate_schedule_*). The five value lists for the AC_ fields are `data/value_lists/ac_*.csv`; the web DB moves saved admin
  changes from the old list names on start-up (`web/db.VALUE_LIST_RENAMES`). References to CET's own tables (E3AvoidedCostElecCO2Seq..., dbo.E3...) keep CET's names.
- Every CET measure-level output column is produced (0.1.31, 2026-10-23) except JobID and PA (= IOU_AC_Territory): savings quantities and WeightedSavings
  (run_poc.savings_outputs / weighted_savings), discounted savings per ACC group (run_poc.discounted_savings), nominal participant-cost columns (engine), Wtd* /
  ProgramCosts / WeightedElecAlloc / lifecycle net benefits / Lev* (cost_tests). Deliberate differences: LevBenElec is TRC ElecBen per kWh (CET's export is
  ElecBen_SB: a column-order mix-up in CalcCost); gross discounted savings on dual-baseline measures use the net formula (CET's has two errors). C17 items 8-10.
- 0.1.32 (2026-10-23): price_bill_impact, price_participant_cost, price_other_ben_cost (engine) and price_emissions (emissions) are column-wise; their
  row-by-row originals are kept as *_per_measure references (tests/test_performance.py, and the re-check showed every output unchanged to 3e-14). Change the
  arithmetic in both. 1,000,000 records: 4.5 min, 7.4 GB peak (README Performance).
- 0.1.33 (2026-10-23): a job with more Measure.csv rows than `RunConfig.chunk_size` (50,000; 0 = never) runs in parts (`recet/chunked.py`): split once into
  `<run>/_parts` with the whole file's column types (`delimited.split_file`, `part_dtypes`), preflight per part with the whole-file checks done once
  (`preflight._check_parts`), pass 1 prices each part and adds up the program totals (`run_poc.program_share_totals`), pass 2 shares program costs with those
  totals (`apply_program_costs(..., totals=)`), writes parts (`outputs.TableWriter`) and sums the summary (`outputs.SummaryParts`). RunResult.frame is None then.
  Anything new that needs the whole job (a program-level share, a summary, a cross-row check) must get a parts version too; `tests/test_chunked.py` compares
  every output file of a run in parts with the whole-job run. 1,000,000 records: 4.6 min, 2.3 GB committed (was 17.8 GB; the 7.4 GB first reported was RAM only).
- 0.1.34 (2026-10-23): default `chunk_size` 20,000; `import recet` sets `OPENBLAS_NUM_THREADS=1` unless set (numpy's OpenBLAS reserves a buffer per thread; the
  pricing gains nothing from them), so import recet before numpy in new entry points; `AccStore.series` returns read-only views of a memory-mapped, already-divided
  cache file (`scaled_column`) instead of copies (bit-identical; copies only without the cache), so never write into a series in place. 1,000,000 records:
  5.0 min, 0.96 GB committed.
