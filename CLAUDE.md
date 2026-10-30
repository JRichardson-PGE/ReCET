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
- The web server's state is backed up nightly to S3 by `deploy/backup/recet-backup.sh` (systemd timer, instance role; README there is the setup and
  restore runbook, tests/test_backup_scripts.py runs both scripts against a fake aws). A change to what the web app keeps in its state folder (a new
  database, a folder that must not be mirrored) changes that script and its README too.
- The web app (`recet/web`: templates, `static/*.css`, `static/*.js`, the vendored Tabulator and Mermaid as we use them) must meet WCAG 2.2 Level AA
  (Jake, 2026-10-29; ARCHITECTURE 1.4 and 8.7). Every page change keeps it: a visible label or accessible name on every control, keyboard reach and a
  visible focus for everything a mouse can do (no hover- or double-click-only actions, no `title` as the only way to learn something), status shown in
  text and not by colour alone, 4.5:1 text and 3:1 control-border/focus contrast in BOTH colour schemes, `aria-live` for text that updates on its own,
  targets at least 24 x 24 px, content that reflows at 320 px wide and 200% zoom. Check new colours with a contrast calculator and new pages with axe.
- Tests: `pip install -e ".[test]"` then `pytest` from the repo root; they need no reference data.
- Dependencies are locked (phase 0 of the CEDARS platform build plan, 2026-10-30): `requirements/{engine,web,test}.txt`, every platform and Python 3.11+,
  with hashes, made by `python requirements/lock.py` (uv) from pyproject.toml. The Dockerfile installs `web.txt` with `--require-hashes`, then ReCET with
  `--no-deps`; the Lockfile workflow fails when the files no longer match pyproject.toml. Change a dependency in pyproject.toml and re-run lock.py in the
  same PR (`--upgrade` to take newer versions, and run the tests on them).
- ACC versions are keyed by ACC year (2021, 2022, 2024, 2026), and run parameters / E3 tables are picked by ACC version. The web pages name a version by
  its ACC year only, "2026 ACC" (`versions.acc_short`, `web/app.acc_name`; Jake 2026-10-26, with D12 retiring the "Avoided Cost Version" label), and New run
  has a "Which ACC applies to my filing?" table instead: `versions.BUDGET_FILING_ACC` (filing, program years, ACC, basis; CET's resolution table through
  2027, then Jake's 2026-10-26 schedule, the last two rows inferred) and `versions.claims_acc()` (claims = the ACC of the year's final budget filing, one per
  program year, through CLAIMS_LAST_YEAR 2035). The page shows it compactly (Jake: the two full tables were too big): `versions.acc_summary()`, one row per
  ACC 2021-2028 with its filings' short names and its claim years; dimmed = not in versions.BUILTIN (not by the server's ACC list). That table is also the source for TIER2_PLAN step 2's BR-06.5 flag. The desktop window and the outputs'
  POC_ACC label still show both names (`versions.acc_label`, "2024 ACC (Avoided Cost Version 2026)", BR-11.7 as of 2026-10-22); the 2026 ACC is Avoided
  Cost Version 2028 although CET's draft tables number it 2027 (`AVOIDED_COST_VERSION`; tables are still read through `versions.CET_VERSION_TO_ACC`).
  Inside the packaged CET exports CET's numbers are mapped on read.
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
- 0.1.35 (2026-10-23, Jake): the web app takes a job's CET files only as ONE .zip (Measure.csv required, ProgramCost.csv optional; `web/jobzip.py`), as CET
  does: a bare CSV is refused on New run and Edit inputs. The zip may hold only those two files (any case, .csv/.txt, top level or one folder; macOS/Windows
  extras skipped); each is copied out to `inputs/measures.csv` / `inputs/programcost.csv` (the zip is deleted), byte-counted against
  RECET_WEB_MAX_UNZIPPED_MB (1024). Uploads are capped at 150 MB per file (RECET_WEB_MAX_UPLOAD_MB, was 50); the edge nginx must allow it too.
- 0.1.36 (2026-10-23, Jake): output format `csv.zip` (`outputs.table_path`, `_CsvZipStream`): each CSV written straight into `<table>.csv.zip`, byte-identical
  to the plain CSV inside; `read_table` reads it. The web app always uses it for CSV. Parquet and xlsx are not zipped (10% and 3.5% smaller zipped; parquet with
  zstd instead of snappy would be 71% smaller, not done). Web "Download all" / inputs.zip are built in `<state>/tmp` (`app.zip_to_file`, already-compressed files
  stored) and streamed, never in memory; runner.cleanup removes stale ones.
  Also: a run in parts joins its parquet parts into row groups of `RunConfig.parquet_row_group` (200,000; full groups, remainder carried): the 1M measure-level
  parquet is 253 MB in 5 groups (was 666 MB in 50 groups of 20,000), peak 1.65 GB. docker-compose takes RECET_MEM_LIMIT (1400m; 3000m on a t4g.medium).
  Timing note: the laptop ran about 1.7x slower from mid-afternoon 2026-10-23 (6 s vs 3.5 s per 20,000-row part on the same code), so compare runs only within
  one sitting.
  Also (same PR): CSV is written PARQUET FIRST. TableWriter(csv/csv.zip) joins its parts into parquet, then `write_csv_from_parquet` formats it with pyarrow
  (8x faster than pandas' to_csv) on a second thread while the first compresses (`write_csv`); ISA-L level 1 when `isal` is installed (web extra and
  `.[fast]`; about 6x faster than zlib, files ~11% larger), else zlib 6. Booleans written True/False, timestamps to the second, zip entries dated now. The
  measure-level parquet is kept in `<run>/_results/` (`outputs.RESULTS_DIR`) and the Results tables read it (`outputs.results_source`); the web app hides it.
  pyarrow's number text differs from pandas' (0 vs 0.0, no exponent for small values) with identical values.
  Also (Jake): Excel output is for jobs of up to `RunConfig.excel_max_rows` measures (50,000; writing is ~400 rows/s); a larger Excel job writes EVERY
  table as `excel_fallback` (csv.zip), decided once the measure count is known (`jobs.run_format`, before any table is written), with a note in the log,
  RunResult.notes and run_settings.json. `outputs.ExcelLimit` stays as a per-table guard.
- 0.1.37 (2026-10-24, Jake): lifetime values priced per route. `engine.price_real_hourly` / `price_gas_benefit` no longer expand measures into one row per
  quarter: `_lifetime_values` writes value = scale_1st x A + scale_2nd x B, with A, B (the quarter sums of f(k) or 1 - f(k) x weight x discount x U, each quarter
  still discounted on its own) computed once per distinct (route, claim year, claim quarter, EUL, RUL). They now return ONE ROW PER MEASURE (measure_id plus
  annual_value / gas_benefit = the lifetime value; the *_per_measure references keep their per-quarter rows). Also `profile_meta` reads a cached id index
  (it scanned the catalog per call); the grouped engines read each route's first measure with ONE `iloc[...].to_dict("records")` (callbacks get a dict, not a
  Series); the grouped engine shifts each route's profile once when `shift_fn.year_independent` (set on `shift_for_year`; a year-dependent shift such as
  cedars_shift_for_year is still applied year by year), and emissions shifts once per route. 1M job, 20,000-row parts, same sitting: pricing 173 -> 105 s,
  whole run 298 -> 229 s, peak memory unchanged, outputs equal to 2.2e-12. At 100,000-row parts pricing's peak falls 2.11 -> 1.20 GB (run peak still 2.29 GB,
  set by writing). The earlier "about 2x" estimate held for a one-piece run only. Tried and dropped: a cross-part cache of each route's quarterly sums (saved
  ~3 s of 229; once shifted once, U is cheap). Shifting each profile once per run instead of once per pricing call would save at most ~3.5 s (72 ms a part).
- 0.1.79 (2026-10-30, phase 0 of the CEDARS platform build plan; 0.1.76 record cap, 0.1.78 worker; first numbered 0.1.77): the regression gate. `recet/regress.py`,
  `recet regress build|check|compare`. build (laptop, full reference data): runs each job under `regress.record()` (wraps AccStore.series and
  real_impact_profiles._profile_shapes_by_zone; a value is "read" when its key is read or the dict is iterated), `slice_reference` writes only the ACC
  rows of the (utility, zone) pairs read with every unread value NaN (same years, tests and columns) and the profiles' row groups with only the zones read,
  then reruns each job on the slice in a new process and stops unless every table equals the full run's to 1e-12. check: runs `tests/data/regression`'s
  jobs on its `ref/` against `expected/` (TOLERANCE rtol 1e-9, atol 1e-6; `--update` after a deliberate change; tests/test_regression_gate.py in CI).
  compare: two run folders' tables. Fixture jobs: the C3 and 10k jobs' records in PG&E 13, SCE 9 (5) and SDG&E 7 (6, 8), no System zones (their slice
  would be most of the ACC). The pricing reads every year of a series it uses, so a pair costs about 7 MB of the 2024 ACC (total_avoided_cost, TRC
  emission rate). tests/test_regress_tool.py tests the tool on synthetic data.
- 0.1.78 (2026-10-30, phase 0 of the CEDARS platform build plan; 0.1.76 record cap, 0.1.77 regression gate): the job spec and the worker, the contract
  the platform's Calculations module calls (its job API, run records and S3 layout are the platform's, not ReCET's FastAPI). `recet/jobspec.py` (spec
  version 1; `validate`, kept in step with `data/job_spec/job_spec_v1.schema.json` by tests/test_worker.py), `recet/storage.py` (local, file://, s3:// via
  boto3 only when used; extra `.[s3]`), `recet/worker.py` (`recet worker run|validate|schema`): refuses (exit 2) a wrong release, a pinned input / reference
  file that differs (manifest.json hashes, else computed), an allocation table that differs, an unknown setting; runs `jobs.run_job` in a work folder;
  writes `receipt.json` (RECEIPT_VERSION 1) even when the run fails (exit 1); sends the run folder to outputs.uri. Settings a spec may set:
  `jobspec.settings_fields()` (RunConfig minus files, folders, names, engine, format, subset_step). Asked for by the platform thread: `context` (any object,
  echoed verbatim into the receipt), `progress.json` sent to outputs.uri during the run (`worker.ProgressSender`: each stage change, else at most every 10 s),
  and the receipt's `allocation` names a supplied table's URI, SHA-256 and the `allocation_version` requested (the platform passes the sha256).
  Then (platform thread): `RunConfig.sw_factors_replace` (`--sw-factors-replace`): the supplied factor table is the WHOLE table, no built-in row used
  (`statewide_allocation.set_user_factors(table, replace=True)`), so a pinned allocation version is exactly that table (BR-13.8); receipt `allocation.mode`
  replace / merge. A statewide program the pinned table lacks is not allocated (the loader warns).
- 0.1.76 (2026-10-30, phase 0 of the CEDARS platform build plan; BRD v0.5 BR-06.3): the record cap. `RunConfig.max_records` (2,000,000 =
  `preflight.MAX_RECORDS`; `--max-records`; 0 = no cap): `preflight.check_inputs(max_records=)` counts the rows first and refuses a larger job with the
  single error `too_many_records` (`too_many_records_message`), nothing else checked; run_job does not split such a job; a CET replica refuses it in
  `cet_versions/job._check_record_cap` (ValueError, same message). tests/test_record_cap.py.
- 0.1.75 (2026-10-29, Jake: "CEDARS is always assuming SCE for programs with SW PA in the CET, but SCE is not all of the programs' lead. SCE has
  historically been the largest funder"): a CEDARS upload's statewide ProgramCost rows (PA SW) are costed as SCE's (`cet_versions/job.cet_programs`,
  logged), as CEDARS passes them; a job in CET's input form (Measure PA column) is priced as given. Found by running the two 10k jobs on the published
  CET 25.1 (CET_UI 122716 IOU-only, 122714 with 8,927 statewide records; CET's echoed ProgramCost had the 72 SW rows as SCE): the replica on the raw
  uploads now equals CET's outputs in all 180 exported columns of all 10,000 records of each, to 1e-9 (before: 31 program-cost columns differed on the
  statewide records). Then CET_UI 122738 (35 records under MCE / BAY / SCR / SDRN / SW / PGE programs): CEDARS passes EVERY record's IOU_AC_Territory as
  its PA (CCA / REN records priced at their IOU's rates) and EVERY non-IOU ProgramCost PA as SCE (`job.IOUS`; PGE kept), so cet_inputs takes the
  territory for all records and cet_programs maps any non-IOU PA. tests/test_engine.py: the C3 upload equals RunCET on the SW -> SCE program table, and RunCET's own outputs off the statewide records.
  Then (Jake: "every ProgramCost row whose PA doesn't have settings defined is priced as SCE ... if we define discount rates for BayREN etc in the
  future they will use their own setting"): cet_programs maps a PA to SCE only when it has no E3Settings row at the run's Version (`job.settings_pas`,
  the PAs `inputs.settings` selects; called after the tables are read, in run_job and `recet cet-version run`), not by a list of IOUs. Same result on
  25.1's current Versions (IOUs + PAC; all three CET_UI jobs still 180 / 180); on Versions up to 2022 a CCA keeps its PA and own rates.
- 0.1.74 (2026-10-29, Jake: "make the replicas more memory efficient. Someone could try to run 1M rows thru the replicas too"): a replica prices a job in
  parts, `RunConfig.replica_part_size` (10,000; `--replica-part-size`; 0 = whole; Jake chose a setting of its own rather than `chunk_size`'s 50,000: a
  200k job on 25.1 peaks at 1.2 GB with 10,000, 4.2 GB with 50,000, same time). `cet_versions.price` runs two passes: pass 1 prices each part's benefits and
  adds up the program sums (`calc_ce.program_sums` / `calc_ce_2021.program_sums`: positive benefits and claim count per PrgID; `calc_savings.program_sums`:
  the savings weights' totals), spilling the benefits to parquet; pass 2 finishes each part with the whole job's sums (costs, bills, ratios, savings,
  emissions, CalcCost) and yields it. `run` is `price` over a DataFrame (parts by CET_ID, every row of a CET_ID in one part, output in CET_ID order).
  `cet_versions/parts.py`: Measure.csv split once (delimited.split_file), scanned for the table keys, First Year, repeated CEInputIDs (priced together in
  a last part) and ACCVersion; job.run_job and `recet cet-version run` write Saved*, measure-level, summary, key metrics and Results sums part by part
  (one-part jobs exactly as before). Tables: the avoided-cost parquet is read row group by row group, key columns first (dictionary-encoded), the whole
  group only if a row matches, single-threaded, ID / ACKey skipped, also filtered on climate zone; max Qac from the row-group statistics; Arrow's pool
  released after. Pricing: the quarterly joins carry only the columns they use; the rebased sources are built once per run (`QuarterlyTables.cached`);
  `sql.dec_pow` computes each distinct (rate, n) once; emissions' NULL-row test checks the distinct CET_IDs (pandas' Arrow-string isin over millions
  of rows was 45% of the time). Measured on the laptop, 25.1 (its 23.3M-row 2026 table), C3 rows repeated: 20k measures 4.8 GB / 25 s -> 0.6 GB / 8 s;
  1,000,000 measures 1.32 GB peak / 435 s (`cet-version run`), 1.37 GB / 427 s (`recet run --engine cet-25.1`, every output file). One part: outputs
  bit-identical to 0.1.73; in parts: equal to rounding (5e-16, the program sums' order). Also: a CEInputID on two rows no longer stops the measure-level
  table (each row shows its CET_ID's outputs). tests: every reference run priced 7 measures at a time, the CLI and a run in parts, a repeated CEInputID.
- 0.1.73 (2026-10-29): a replica run takes a CEDARS upload's Measure.csv (the first server run of the 10k job failed: `'DataFrame' object has no attribute
  'PA'`). `cet_versions/job.cet_inputs` builds CET's input table as the reference runs did (CEDARS-Research `poc/cet_reference_jobs.py`): PA = the program's
  ProgramCost PA (else the PrgID prefix), a statewide record's = its IOU_AC_Territory (with a note: CEDARS does not pass SW to CET), FuelID -> FuelType;
  a file that has CET's PA column is used as it is. tests/test_engine.py prices the C3 job in upload form equal to RunCET.
- 0.1.72 (2026-10-29, Jake: "For D13 enable this in ReCET calculations"): the calculation engine. `RunConfig.engine` ("" / "recet" = ReCET, "cet-<release>" =
  a replica), CLI `--engine`, New run's Calculation engine select (with "Which ACCs each engine has"; replica-only ACCs in their own optgroup). `jobs.run_job`
  hands a replica to `cet_versions/job.run_job`, which writes the release's Saved* tables and a measure-level file in ReCET's layout (`measure_level`: inputs,
  Saved* columns under ReCET's names via `cet_outputs_to_recet`, Prg_ columns), the summary and group sums, so Results / Compare work across engines.
  run_settings.json has an `engine` block (`engine_record`); run page and Compare show it (`Ctx.engine_label`). `job.REFUSED`: a replica refuses supplied
  ACC / profile files, statewide allocation, components, 8,760, a second ACC, filing checks and Check inputs only (a filing accepts only ReCET's release);
  the web drops server run_defaults in REFUSED for replicas. File inputs are 24 px tall (axe target-size with every details open; New run, run, Results and Compare clean in both schemes). Replica runs need the parquet export in `<reference folder>/cet_versions/`. tests/test_engine.py.
- 0.1.71 (2026-10-29): `cet_versions.QuarterlyTables.from_release` reads an avoided-cost parquet in 1M-row batches and keeps only the job's PA / end-use
  rows before it becomes a DataFrame (the largest quarter is read from the Qac column alone). The first real 25.1 run on the laptop ran out of memory
  (17.3 GB) loading the whole 23.3M-row 2026 electric table; C3 needs about 1,100 rows. 24.3 (2022 ACC) and 25.0 (2024 ACC) already matched RunCET on the full export.
- 0.1.70 (2026-10-29, Jake; 0.1.69 is the prior-CET-releases PR #58): the screen-reader pass. `tests/screenreader` is an NVDA walk-through of the web
  app (Guidepup 0.36 + Playwright, Windows only, not part of pytest): `serve_a11y.py` serves this checkout on a throwaway state, `recet.nvda.spec.ts` drives
  NVDA and Chrome through sign in, Runs, New run, a run, Results and Edit inputs and writes what NVDA said after each key to `results/<flow>.json` (ignored
  by git). Guidepup finds the browser with Alt+Esc and failed on a busy desktop; `focusWindow.ps1` raises Chrome by its title first, and the title match
  ignores spaces (NVDA says "Re CET"). A locked PC fails every run. Fixes from NVDA 2026.2's transcripts: Run / Results / Compare cards get a visually
  hidden h2 each (only the h1 was a heading); Validate inputs keeps the focus while busy (aria-disabled, not disabled); its result and the row-filter count
  (500 ms after typing pauses) are spoken through one page-wide `#announcer` (base.html; `window.recetAnnounce` in app.js clears it and sets the text 150 ms
  later: NVDA spoke neither as a role=status beside the control, nor a result that replaced "Saving…" at once); New run's ACC select is described by the
  help box's title, not its whole table, and Priced for is named by its label only. The value-list pager hands the focus over when its button disables
  itself. Never disable the focused control; announce through `recetAnnounce`, not a new live region. Edit inputs grid (`edit.js cellFocus`): it is one
  tab stop and the focus sits on the active cell (roving tabindex 0; the scroll box gets -1 while a cell is drawn and hands the focus on), so NVDA
  switches to focus mode by itself and reads each cell with its column and problem; Tab / Shift+Tab leave the grid (Tabulator's Tab moved to the next
  cell: a keyboard trap, 2.1.2), an open editor still moves on with Tab. Moving a cell's tabindex blurs it, so `toCell` notes the focus first. The help line
  is no longer a live region (NVDA read the whole description before every cell); it sits just before the grid. NVDA keeps Escape (edits end with Enter
  or Tab). Guidepup's default capture keeps only the first phrase after a key: the spec sets `nvdaStartOptions: { capture: true }`.
- 0.1.69 (2026-10-29, Jake; plan and evidence in CEDARS-Research `notes/cet_prior_versions.md`): prior CET releases as exact replicas. `recet/cet_versions`
  prices a job as RunCET of CET 21.0, 22.1, 24.1, 24.2, 24.3, 25.0 or 25.1 does, on that release's own quarterly tables, CET's errors included (Jake: "exact
  replicas, labelled"; a deliberate exception to "ReCET does not copy CET errors", for evaluation and history only; ReCET's own engine is untouched and
  shares no code with it). `releases.Release` holds each release's switches; `calc_ce` (22.1+), `calc_ce_2021`, `calc_savings`, `calc_emissions`,
  `calc_cost` port the SQL; `sql.py` copies SQL Server's arithmetic (NULLs, decimal(18,6) POWER rounded to 6 places, 1.000/n, UNION, collation keys that
  ignore case and trailing spaces but not line breaks, positional column lists). Accepted only at 1e-9 on every column of every measure:
  `tests/test_cet_versions.py` over 19 reference runs in `tests/data/cet_versions/` (10 MB, built by CEDARS-Research `poc/cet_version_fixtures.py`).
  Every copied error is marked "CET error kept" and listed in CALCULATIONS C20. Real runs: `recet cet-version run --release R --acc YEAR`, small tables
  shipped in `data/cet_versions/<release>/`, large ones from the parquet export at `<reference folder>/cet_versions/` (`QuarterlyTables.from_release`,
  filtered to the job's PAs and end uses). No web page yet (phase 4).
- 0.1.68 (2026-10-29, Jake; WCAG 2.2 AA audit rows 1, 2, 15, 16: the Edit inputs grid; 0.1.67 is the rest of the pages): `edit.js fixRoles` repairs
  Tabulator 6.3.1's grid roles on tableBuilt (header contents and body table = presentation, the scroll box = the body rowgroup, grid aria-label); the help
  line (`#help`, aria-live, focusable) follows the active cell on arrow keys as well as clicks and leads with the cell's problems (`showHelp(t, col, cellEl)`,
  repeated text not re-announced); flagged cells carry a corner symbol (x stops the run, ! warning) and no-effect headings are italic (not colour alone);
  `:focus-visible` rings on cells, the scroll box and the header; Measure / ProgramCost tabs follow the ARIA tabs pattern (arrows, roving tabindex; the grid's
  redraw takes the focus, so the tab takes it back for 0.5 s); "Show this cell" focuses the cell (`goTo` no longer waits on Tabulator's scroll promises,
  which did not settle for a row already in view, so its flash never ran); the Validate inputs log is a focusable region. Left: column widths change only
  by dragging (2.5.7; the text stays readable in the editor and help line). axe-core 4.10.2: Edit inputs clean in both schemes.
- 0.1.67 (2026-10-29, Jake; WCAG 2.2 AA audit of 0.1.64, all gaps outside the Edit inputs grid; 0.1.66 is the SCT-off PR): `--control` is the border of
  fields and buttons (3:1 in both schemes; `--line` is decoration only), `color-scheme: light dark`, `:focus-visible` ring, skip link (`main#main`), `.sr-only`.
  Results / Compare filters are sent by an Apply button, never on change (3.2.2; `select[data-view]` drops the unsaved columns when the view changes). Run page
  and runs list announce status changes in `#run-announce` / `#runs-announce` and reload at a run's end only when nothing has the focus (`reloadWhenIdle`).
  Help that was only a tooltip is on the page: "What these numbers are" (`tile_help` macro), "Column definitions", Priced for / Check inputs only / why Edit
  inputs and rerun is off. Sortable headings hold a `.sort-btn` button; `<p class="alert">` has role=alert and takes the focus on load; Docs `<pre>` / `<table>`
  get tabindex=0 (app.doc), the ARCHITECTURE diagrams `accTitle` / `accDescr`; admin, audit and value-list tables scroll in `.hscroll` at 320 px. Checked with
  axe-core 4.10.2 on every page in both schemes: only the Tabulator grid still fails.
- 0.1.66 (2026-10-29; 0.1.65 is the OpenBCA PR): the step 0 server checks (CEDARS-Research TIER2_PLAN) found two small things. A run with SCT off
  logged a pandas PerformanceWarning per part: `run_poc` blanked the societal columns one by one (fragmenting the frame, which `assemble_measure_level`'s
  reset_index then warned about); now one `assign(...).copy()`. tests/test_run.py and test_water.py pass with `-W error::pandas.errors.PerformanceWarning`.
  And SCT is about 15% of a run, not "about two thirds" (server, 1M: 392 s with, 334 s without; pricing 190 -> 133 s): New run's label, WEB_GUIDE, jobs.py.
- 0.1.65 (2026-10-28, Jake; 0.1.58-0.1.64 are the BRD-musts stack, PRs #45 and #47-#52; scope: Claude Doc "OpenBCA module for ReCET: scope"): step 1 of the OpenBCA module.
  `recet openbca run|compare` (`recet/openbca/run.py`) runs OpenBCA's own engine (its Integrated pathway: the SQLmesh projects on DuckDB, unchanged) on a
  Program Input + Configuration workbook. ReCET never imports SQLmesh/DuckDB: RECET_OPENBCA_HOME names a pinned checkout (a release tag, `uv sync` done;
  RECET_OPENBCA_PYTHON overrides its .venv), each run copies its config/core/excel_input_parsing into `<run>/openbca/` with the workbooks under OpenBCA's
  fixed names, and `openbca/engine.py` runs in OpenBCA's interpreter, exporting OpenBCA's own outputs (results_summary_by_id, jst_ratio, the five
  validation views) as they stand: nothing recomputed on them (Jake). C1 (v1.0.4 demo, 58 measures): every one of OpenBCA's tables equal to its own
  `model_runners.py` run; 50.6 s, 1.81 GiB peak. Scale (demo measures copied, 4 cores): 580 measures 207 s, 6.2 GiB, 16 GB of DuckDB spill
  (final_value_calculations_ts); 1,160+ ran out of ~28 GB disk. So (Jake, 2026-10-28) OpenBCA is tested on the laptop from the command line only: no web
  page, no server, no performance comparisons (C5 cut); results comparisons only, on a sample (C4 = ~50 measures of the 2028 PG&E job). Step 2 (C2): `openbca/ca_config.py` builds California's
  jurisdiction tables from an ACC file (hourly total_avoided_cost / 1000 per IOU + climate zone, subset "PG&E CZ12"; TRC = "CA TRC", PAC = "USI";
  inflation 0, quarterly discounting, line losses 0) and `run --jurisdiction <folder>` stages replacement parser models that read them (a stub
  Configuration supplies the CUSTOM 1-5 names); `openbca/workbook.py` writes a Program Input. OpenBCA rounds every avoided cost to $0.001/kWh (its
  `::NUMERIC` cast is DECIMAL(18,3); up to 0.03% on annual sums), left as is; C2 on the 2024 ACC equals the ACC rounded that way within one rounding step.
  Step 3: `openbca/translate.py` sends a CET job to OpenBCA on ReCET's own pricing inputs (the route's resolved, ACC-aligned 8,760 per load shape; the
  route's get_acc_series per subset "PGE CZ13", nearest ACC year beyond the file; Unit Quantity x NTG x savings = AC_Net_kWh_ScaledProfile_1st_Baseline;
  rate = 4 x DiscountRateQtr; incentives = rebate + to-others + DI; measure cost = CET's nominal one; ProgramCost's ten fields -> Program Admin Costs), set
  up from the run's RunConfig (`jobs.prepare`), and flags each measure's known differences (`VALUE_DIFFERENCES`: fractional_eul, ntg_cost, dual_baseline,
  excess_incentive, unpriced; plus start_quarter, fractional_units, water, other). `recet openbca compare-recet` (compare_job) runs ReCET and OpenBCA (TRC
  as "CA TRC", PAC as "USI") and writes compare_<test>.csv and summary.csv. OpenBCA discounts from each measure's own start quarter (CalcCE from Q1 of
  the First Year) and leaves upfront costs undiscounted, so a measure's benefits and costs are both higher by (1 + r/4)^quarters; compare after that
  factor. Results 2026-10-28 (2024 ACC, records from the 2028 consolidated job): C3, 20 matched records: electric within 1.7e-4 (the $0.001/kWh
  rounding), costs equal, jst_ratio 0.119238 / 0.147843 vs ReCET on the same terms 0.119238 / 0.147844; C4, a 50-measure sample: 47 within 5e-5
  after the start-quarter factor, the other 3 are a fractional EUL (+9.4%), a dual baseline (x2.29) and an excess incentive (TRC cost x0.15).
  Gas (Jake, same day): each gas profile (gas_savings_profiles.get_shape) goes as a monthly Natural Gas load shape and each (IOU, gas sector, profile)
  route's get_years_for_measure TRC series (clamped to the gas ACC's years) as stream `ca_config.GAS_STREAM` in $/MMBtu (x10), savings in MMBtu; OpenBCA
  has one subset per measure, so a gas measure's is "<elec subset> | G<n>"; one NTG per measure (NTGRkWh, NTGRTherm when no kWh), each fuel's savings
  divided by it so the product is ReCET's priced quantity. C3 gas, 13 matched records (12 dual fuel, PGE/SCE/SCG/SDGE, Annual and Winter Only):
  gas within 6e-6 per measure, jst_ratio 1.370003 / 1.382122 vs 1.369957 / 1.382075. C4 with gas: 20 of 21 gas measures within 3e-6 after the
  start-quarter factor (the fractional EUL one x1.058); jst 0.666 / 1.119 vs 0.655 / 1.078 on the same terms (start quarters and the 3 differences).
  Same PR: run_poc.run_gas_benefit crashed under pandas 3 on a job whose gas profiles are named but none has gas savings (an empty str Series sums to "").
- 0.1.64 (2026-10-28, Jake; BRD review gap 7, BR-08.1): the data dictionary's attributes. `field_docs.table(columns, acc=)` adds
  `recet/field_attributes.py`'s unit, source, version, cost_tests, time_basis, aggregation, rounding, years_in_use to every run's `field_definitions`
  (OUTPUT_FIELDS 2.11). Rules on the column name and the documented "Unit:" text, not a registry; `BENEFIT_OF` / `COST_OF` must follow
  `cost_tests.compute_cost_tests` (tests/test_field_attributes.py adds them up to each test's Ben / CostNoAdmin). The research repo's field dictionary
  workbook does not carry them yet.
- 0.1.63 (2026-10-28, Jake; BRD review gap 6, BR-06.11): compare any two runs and share a run. `results_view.compare(a, b, level, keys, pa, years)`
  (`<key>__a/__b/__diff/__pct`, groups matched on their id, a group in one run only blank for the other; `COMPARE_LEVELS` has no measure level); web
  `/runs/{id}/compare?with=<id>` and `compare.csv`, offered on a finished run's page for the user's own and shared finished runs. Sharing: table
  `run_shares`; `Ctx.run_for` lets a user a run is shared with read it (pages, results, files, compare, rerun as their own run), and `owner=True` (Stop,
  Delete, share / unshare) keeps the rest with the owner or an admin. Runs list: "Shared with you". Audit `run shared` / `run unshared`. tests/test_share_compare.py.
- 0.1.62 (2026-10-28, Jake; BRD review gap 5, BR-06.2): run emails. `recet/web/notify.py`: when a run ends, `Runner._finish` starts a thread that
  emails the owner (finished or failed; not a stopped run or a passing input check) through RECET_WEB_SMTP_* (none set = no email; docker-compose passes
  them from the server's .env), with headline totals from run_settings.json and the RECET_WEB_BASE_URL link. Account page: "Email me when a run finishes
  or fails" (user_settings `notify_runs`, on by default). Audit `run email sent` / `run email failed`. ARCHITECTURE 8.6; tests/test_notify.py.
- 0.1.61 (2026-10-28, Jake; BRD review gap 4, BR-06.13): the combined 8,760 by climate zone. `profile8760.portfolio_profile` adds
  `net_kwh_<Y>_CZ<zone>` (`zone_label`: the filed AC_Climate_Zone, CZ prepended to a bare number), `combine_profiles` adds them up over parts, and
  `profile8760.peaks` writes `portfolio_8760_peaks_<tag>` (per year: portfolio, each PA, each zone; own peak, largest added load, kWh and share at the
  portfolio's peak hour). The peak is the max net-savings hour, not a DEER peak-period definition. tests/test_profile_zones.py.
- 0.1.60 (2026-10-28, Jake; BRD review gap 3, BR-04.1): results by program year. The portfolio summary has a `program year` group (Used_Year;
  `SummaryParts.GROUPS`, which summarize now shares). Results: group-sum key `_year` (CUBE_VERSION 2, so kept sums are rebuilt), a Program year level, and
  `years` (a list; empty = every year) on RunResults.table / count / measure_level_allowed and results_view.table / filter_pa; web `?years=2028&years=2029`
  (checkboxes under Program years, also on results.csv), window: a Program years menu. `year_of` falls back to ClaimYearQuarter for old runs.
- 0.1.59 (2026-10-28, Jake; BRD review gap 2, BR-06.16): PrgIDs that do not line up between Measure.csv and ProgramCost.csv no longer stop the run (CET stops
  it; C17 item 11). `run_poc.load_job` no longer raises; `preflight._program_alignment` warns `measures_without_program_costs` (priced with no program costs,
  CreditedPA blank) and `program_costs_without_measures` (with the nominal dollars no cost test includes), in one piece and in parts. A load ValueError is now
  `inputs_unreadable` (it was mislabelled program_alignment). tests/test_program_alignment.py: the result equals the aligned job with those costs at 0.
- 0.1.58 (2026-10-28, Jake; BRD review gap 1, BR-06.7): statewide allocation reconciles. `statewide_allocation._allocate`'s report carries
  `reconciliation` (per Year, SW PrgID, allocated field: the total before allocation and over each IOU's produced rows; Measure fields as NumUnits x field),
  `statewide_allocation.reconciliation(files, programcost)` adds them over a job's file or parts, and `jobs.write_reconciliation` writes
  `statewide_reconciliation_<tag>` after preflight (OUTPUT_FIELDS 2.13) with a log note per field that does not reconcile (factors not summing to 1, usually
  a fuel whose factors are all 0). tests/test_statewide_reconciliation.py prices a job both ways: benefits add up wherever the inputs do. ARCHITECTURE 1.3
  marks BR-06.8 partly met (tests are code).
- 0.1.57 (2026-10-28, Jake): an RUL_Yrs equal to EUL_Yrs is no RUL, as CalcCE's InputMeasurevw has it (`CASE WHEN RUL >= EUL THEN 0 ELSE RUL END`):
  `engine.rul_in_use` in the electric / gas / water lifetime values, bill impacts and participant cost (grouped and per measure) and in
  `tests/reference_calc.py`. Before, a fractional life blended the 2nd baseline into its last part-quarter / year, and the participant cost took the
  2nd-baseline cost plus the deferred difference; emissions, the combined 8,760 and the savings already treated it as single-baseline (the savings give the
  same values either way). test_calcs' EX4 is such a measure. Same PR: the docs audit of PR #43 (stale names and formulas after 0.1.50-0.1.56).
- 0.1.56 (2026-10-28; made 2026-10-27 for 0.1.52 but pushed after PR #38 merged): default `chunk_size` 50,000 (was 20,000), as the web server
  already ran; about 1 GiB peak at 1M on the server. Diagrams use SVG text labels (`htmlLabels: false` in each diagram's front matter, for GitHub,
  and in docs.js): HTML labels sit in a foreignObject that clips any text wider than Mermaid measured (e.g. a font that loads after measuring).
  No `<b>` in labels (it prints literally as SVG text).
- 0.1.55 (2026-10-28, Jake; plan and workbook in CEDARS-Review `deliverables/recet_renaming/`): the calculation code uses the input and output field names.
  The loader (`data_loading._derive_fields`) keeps the Measure fields under their own names (no `num_units`, `ntgr_kwh`, `shape_key` ... aliases) and names
  each derived value for the output it is written as (`Used_Year`, `AC_Net_kWh_1st_Baseline`, `Fuel_Sub_Flag`) or `Used_<field>` (`Used_UnitkWh1stBaseline`:
  a kWh-per-unit profile's own annual kWh, flagged by the new output `Scaled_Profile_Flag`). Measure side `PA` is now `IOU_AC_Territory`; result frames are
  keyed by `CEInputID`. Rules for new code: never put a non-input value under an input's name (NTG + MEBens is `NTGRkWh_plus_MEBens`; market effects are a
  factor applied when a pricer's `Priced_Quantity_1st/2nd_Baseline` / `Billed_*` columns are filled, never a rewrite of `AC_Net_*`); the profile multiplier is
  `AC_Net_kWh_ScaledProfile_*_Baseline` (= `AC_Net_kWh_*_Baseline` except on a kWh-per-unit profile). Outputs renamed (old names still accepted in templates and
  views): `AC_kWh_*` / `AC_Therms_*` -> `AC_Net_kWh_*` / `AC_Net_Therms_*`, the four `*_sct` participant PVs -> `*_SCT`. Pricing result: `HourlyPricingResult`
  (`values` with `LifetimeValuePV` grouped / `QuarterValuePV` per measure, `unpriced_ids`, `route`); the first proof of concept's batching fields and the
  fallback-shape / portfolio paths are gone. Preflight: every CET NotEmpty numeric field is required (`blank_required_value`; the loader no longer fills NTG / IR /
  RR / costs), negative EUL/RUL (`negative_life`) and RUL > EUL (`rul_above_eul`) are errors, RUL = EUL a warning; `blank_second_baseline` is gone. The 0.25-year
  EUL floor is gone (CalcCE has none). Checked: all eight real jobs of `before_after.py` (2028-2031 consolidated, 2028 allocated with 5% ME and components, 2029 on
  the 2026 ACC, SWWH025 in parts, CET 121841) give every output identical to 0.1.54 (1e-9), the only new column `Scaled_Profile_Flag`.
- 0.1.54 (2026-10-28, Jake): output templates are built with a two-list field picker (web Templates page: `static/app.js` over a JSON catalog in the
  page, the textarea kept as the submitted value and the paste box; window: `gui_templates.py`, replacing the text box). Left list: Show = all / input
  fields / program fields (Prg_*) / output fields / one OUTPUT_FIELDS group (`output_templates.KINDS`, `kind()`), search over names and definitions;
  right list in file order with Top / Up / Down / Bottom. A saved default template starts new runs: web `user_settings` table (key
  `default_output_template`; a fresh New run page preselects it, a rerun keeps its own), window config key `default_output_template` (applied at
  startup over the restored last-run template once a default is set). Skipped 0.1.53 (PR #39, ratio numerators, used it).
- 0.1.53 (2026-10-27, Jake): every ratio has its numerator as its own column, just before its ratios: `TRCBen` (B + TaxCredits), `PACBen` (B), `RIMBen`
  (B + bill increases), `SCBBen` / `SCHBen` (the _SB / _SH benefits + OtherBen); PCT already had `PCTBen`. So `<test>Ratio = <test>Ben / <test>Cost` and
  `<test>RatioNoAdmin = <test>Ben / <test>CostNoAdmin` from two columns of one file (0 when the cost is 0). The portfolio summary, Results group sums and
  two-version comparison carry them too; `outputs._numerators` reads the `<test>Ben` totals, falling back to their parts for files written before 0.1.53.
  Statewide allocation: Weighted Factor, like the costs. Column order changed: SCBBen, SCBRatio, SCBRatioNoAdmin, SCHBen, SCHRatio, SCHRatioNoAdmin.
- 0.1.52 (2026-10-27, Jake): `docs/ARCHITECTURE.md` (requirements, layers, every module, run flow, data, web deployment and security, performance
  design; its module map and web settings table are checked by tests). The web Docs pages draw ```mermaid blocks: Mermaid 11.4.1 is vendored in
  `web/static/vendor/mermaid/` (npm tarball, integrity checked; the CSP allows only same-origin scripts, so never load it from a CDN) and `static/docs.js`
  turns `code.language-mermaid` into diagrams; app.doc loads them only on a page that has one. The desktop window shows the Mermaid source as text.
- 0.1.51 (2026-10-27): `key_metrics.sums` grouped on the key columns first and builds the "PrgID - name" label only for the groups; it built it row by row
  (`apply(axis=1)`), 0.9 s of every 50,000-row part (0.047 s now, same table). The 0.1.50 server run's pass 2 took 109 s against 0.1.43's 57 s because of it.
- 0.1.50 (2026-10-27, Jake's field dictionary, CEDARS-Research `deliverables/recet_field_dictionary/ReCET_Field_Dictionary.xlsx`): output columns renamed and
  dropped, and the input / output definitions and units replaced by Jake's. `fieldnames.OUTPUT_RENAMES` (39: `POC_ACC` -> `Used_ACCVersion`, the kept `poc_`
  values -> `Used_Year`, `AC_kWh_1st_Baseline` ..., bills -> `LifecycleBill...`, emissions -> `FirstYear...` / `Lifecycle...` with PM10 -> `...ElecPM10`,
  `CreditedIOU` -> `CreditedPA`; two of Jake's names were typos, fixed with his OK) and `OUTPUT_REMOVED` (36 `poc_` restatements, `ProgramCosts`,
  `WeightedSavings`, `FilingValidation` / `FilingRules` / `ACCCheck`, which are now only in filing_validation and preflight). Renamed throughout the code, not
  just at write time; `outputs.DERIVED_OUTPUTS` lists the nine derived columns written (`AC_kWh_*_Baseline` = annual net kWh x IR x RR, which differs from
  `shape_scale` on a kWh-per-unit profile). Old names still work in output templates and saved Results views (`output_templates.current`,
  `results_view.current_keys`), a CET outputs file's own names are read as ReCET's (`fieldnames.cet_outputs_to_recet`), and the Results page still opens a
  run written before 0.1.50. The summary group "credited IOU" is now "credited PA". OUTPUT_FIELDS: section 1 entries carry Jake's descriptions (per-column
  "- `Col`: ..." lines where they differ, which `field_docs` reads); 1.17 / 1.18 moved to 2.10 / 2.9b; the rename table is in 2.1. INPUT_FIELDS: Jake's
  definitions, one row per field, with an "In ReCET:" note where ReCET's use differs (e.g. RateScheduleElec/Gas not used; CombustionType has no Res Furnace
  default, blank = gas NOx 0). `run_poc.program_share_totals(d)` no longer takes the measures; `outputs.restated_differs` and chunked's per-part raw re-read are gone.
- 0.1.49 (2026-10-27, Jake; TIER2_PLAN step 4, BR-06.15): the minimal input set. `templates.DESCRIPTIVE_MEASURE_COLUMNS` (MeasDescription, Sector: the
  preflight's required columns that editor.EFFECTS gives no effect, CEInputID aside; a test keeps them in step) may be left out: `templates.with_defaults`
  adds them blank in the loader, `outputs.read_raw` and preflight, which warns `missing_descriptive_column`; a missing calculation column
  (`preflight.CALCULATION_MEASURE_COLUMNS`) is still the `missing_columns` error, now saying it changes the results. `outputs.restated_differs` no longer
  needs the optional columns a derived field restates (a file without CombustionType crashed there before). ReCET does NOT take lifecycle savings (Jake,
  2026-10-27): inputs stay CET's per-unit annual gross values with NTG given separately. A file with only the calculation columns gives the same results
  as the full file (test).
- 0.1.48 (2026-10-27, Jake; TIER2_PLAN step 3, BR-06.10): output templates and the key-metrics summary. `RunConfig.output_template` (column list, in
  order) + `output_template_name`: `outputs.TableWriter(select=)` writes only those columns to the measure-level file; with a template the table always goes
  through parquet and the FULL table is kept in `_results/` (every format), so the Results tables, summary and key metrics see every column; a column the
  run did not produce is left out with a note (`writer.left_out`). `recet/output_templates.py`: `catalog()` (input columns, then OUTPUT_FIELDS section 1
  groups, with definitions), `BUILTIN` (Key results, Participant costs), `check`/`parse`/`read_file`, window templates in the config file
  (`output_templates` key); web: per-user table `output_templates`, page `/output-templates` (nav "Templates"; Add buttons in app.js), New run picker; CLI
  `--output-template NAME_OR_FILE`. `recet/field_docs.py` reads each column's definition from OUTPUT_FIELDS.md (#### entries' first paragraph, or a table
  row's second cell) or INPUT_FIELDS.md (echoed input columns), with the web Docs anchor (Python-Markdown toc slug). Every run writes
  `field_definitions_<tag>` (each column of the file: definition, documentation link) and `key_metrics_<tag>` (`recet/key_metrics.py`: Portfolio headline
  rows with definitions; top 10 programs, program sectors (Prg_PrimarySector) and end uses (UseCategory) by |share of TSB|, with TRC cost share and TRC/PAC
  ratios; a run in parts adds `key_metrics.sums` per part). A ratio whose cost is 0 is blank, as in the summary. The five ratio entries in OUTPUT_FIELDS
  got a definition sentence. Real 2028 job (5,518 measures, parts): key metrics equal the portfolio summary; the SW codes-and-standards programs carry 93% of TSB.
- 0.1.47 (2026-10-27, Jake; TIER2_PLAN step 2, BR-06.5): the ACC check. Run option `acc_purpose` ("Priced for": "" any filing, "claims", or a
  `versions.FILINGS` key, the filing name slugged, e.g. "2028-2035-application"; CLI `--priced-for`, window and New run). `versions.expected_accs(year,
  purpose)`: claims = the ACC of the year's final budget filing (claims_acc); a filing = its ACC within its years, none outside; any = the latest
  application covering the year and every filing after it (so 2028-2029: 2024/2026, 2030-2031: 2024/2026/2028; the 2024-2031 application's 2021 ACC no
  longer counts once the 2028-2035 application covers the year); None = not in the table (not checked). Each record's ACC = its ACCVersion, else the run's
  (`acc_year_of`: a built-in's ACC, or a four-digit new label). Preflight warns `acc_not_for_program_year` per (ACC, program year), cells on ACCVersion when the
  record names it, else ClaimYearQuarter; `PreflightReport.acc_check` (counts by ACC version and program year, added up over parts) goes to preflight.json, the
  "ACC check" stat, `run_settings.json` `acc_check`, and a line on the web run page's input check. Measure-level column `ACCCheck` (ok / not for this program
  year / not checked) after the Prg_ columns (OUTPUT_FIELDS 1.18). Never stops a run. Real 2030 PG&E job on the 2024 ACC: claims 5,100 of 5,100 flagged,
  2028-2035 application 0, any filing 0.
- 0.1.46 (2026-10-26, Jake): web usability pass (the plan is CEDARS-Research `UI_UPDATE_PLAN.md`). Edit inputs: one verdict line at the top of the check
  panel combines the cell checks (every save) with the latest Validate inputs result, which never says "the run can start" once the tables changed; the cell
  list is in the panel (was below the grid); Run these inputs shows the first blocking cell. Run page: Input check card from preflight.json
  (`app.check_summary`; UNPRICED_CODES listed first), portfolio tiles from the group sums (`Ctx.headline`), one settings line (`settings_summary`, from
  config.json), deletion date, log folded once ended. "Rerun with different settings" = `/runs/new?from=<run>`: the run's job files are hard-linked
  (`edit_api.link_or_copy`) into the new run (`input_mode=from_run`), its supplied files kept unless unticked; Edit inputs and rerun is disabled above
  editor.MAX_EDIT_ROWS (size from eta). A blank run name becomes "<file stem> · <ACC> ACC" (a rerun: "<name> (rerun) · ..."); drafts from a run are dated.
  Runs list: expired hidden (`?expired=1`), measures from `eta.known_size` (never counts a file on a page view), run time, deletion date; live rows updated
  from `/runs/live` (the page used to reload, and in fact never did: runs.html did not load app.js). New run: sections Pricing / Outputs / CEDARS filing checks,
  Check inputs only is a submit button named preflight_only (app.js copies the pressed button into a hidden field: a disabled submitter is not sent), XHR
  upload with a percentage. Results: tiles, TSB-by-sector bars, Portfolio level first, sortable / filterable tables (app.js, numbers sort by data-v),
  column picker at the top in `results_view.GROUP_ORDER` groups with `display_label` names (display only: CSV downloads keep Column.label). The kept results
  parquet's folder (outputs.RESULTS_DIR) is no longer offered as a comparison sub-run (it showed a "_results" tab on CSV runs). Docs home = `docs/WEB_GUIDE.md`,
  documents named by title (`app.DOC_TITLES`). `[hidden]` is display:none !important (a styled button ignored the attribute). Same PR: the web pages name
  ACCs "2026 ACC" and New run's "Which ACC applies to my filing?" box shows `versions.BUDGET_FILING_ACC` and `claims_acc()` (see the ACC rule above).
  Also (Jake): preflight `Issue.impact` totals the first-year net kWh / therms of a finding's records (`no_electric_profile`, `loose_profile_match`: shape_scale;
  `no_gas_profile`: annual_net_therms_1st), added up across parts, shown in preflight.txt, preflight.json, the Findings sheet (two columns) and the web pages.
  `loose_profile_match` now names its records (cells on AC_Elec_Target_Sector, examples "end use | IOU | sector") and so does `kwh_per_unit_profile`: every
  finding about records names its cells; the ones without cells are about the whole file or set-up (missing file / columns, delimiter, unreadable ACC or
  ProgramCost, no profile library, unknown run parameters, rules not checked). On the 2028 job: 4 loose matches (102,136 kWh); 36 measures with no gas profile
  carry -26,024 therms (gas increases, so their gas supply cost is left out).
- 0.1.45 (2026-10-26, Jake; 0.1.44 is the queue-estimate PR): CEDARS filing checks in pass 2. The first server runs with them (0.1.43, 1M, csv.zip)
  took 468 s (claims) and 483 s (budget filing) against 425 s without: preflight +7-8 s as expected, but assembly 57 -> 91 / 108 s, because
  `attach_filing` rebuilt dicts from all 139,612 flagged records and copied the 344-column part, in every part (~1 s a part on the laptop). Now
  `outputs.filing_lookup` indexes them once per run and `attach_filing` reindexes each part against it, in place (0.08 s a part). `progress.plan`
  takes `filing` and adds FILING_PREFLIGHT_SECONDS (8) to preflight (the queue estimate too: eta.size keeps `filing`). Peak memory on those server runs: 1.101 / 1.095 GiB with checks, 1.028 without.
  Laptop 1M after the fix, same sitting: 189 s without checks, 195 s with claims checks (preflight 33 -> 36 s, assembly 22 -> 24 s).
- 0.1.44 (2026-10-26, Jake): queued runs show when they should start and finish (`recet/web/eta.py`, BR-06.2). A run's size (measure rows; workbook
  Measure sheet) is counted when it is queued and kept as `estimate.json`; its time = 10 s + the progress weights' seconds per 1M (format, SCT, emissions;
  x2 for a comparison; preflight-only = the input-check stages) x measures / 1M, times a calibration = median actual/estimate over the last 10 finished
  full runs of >= 20,000 measures (only runs with a kept estimate.json; clamped 0.25-4). `queue` walks the queue in claim order (input checks first) over
  `workers` slots, each free when its running run's live time left runs out (else its estimate less its elapsed time). Shown on the run page (and its
  poll, `wait`) and the runs list: "starts in about 6 min, done in about 13 min (estimated)".
- 0.1.43 (2026-10-26, Jake; 0.1.41 / 0.1.42 are the progress-estimate and filing-checks PRs): run downloads. Every run writes `preflight_findings.xlsx`
  (`PreflightReport.write_xlsx`, `jobs.write_findings`): Summary, Findings (errors first) and Cells (file, line = data row + 2, record ID, column; IDs read
  from one column of the file only when a finding points at it: 2 s at 1M on the laptop). The web run page's Files list hides `preflight.txt` and
  `preflight.json` (still written: the runner finds a failed run's folder by preflight.txt, the Validate inputs panel reads the JSON), and adds `run_log.txt`
  (the run's log.txt, route `/runs/{id}/run_log.txt`) and `inputs.zip`. Download everything carries `run_log.txt` (was `log.txt`).
- 0.1.42 (2026-10-26, Jake; 0.1.41 is the progress-estimate PR): CEDARS filing checks (Tier 2 item 9, BR-06.3 / BR-03.6, starts BR-03.1 / BR-03.3).
  `recet/filing_rules.py` reads CEDARS' own source-of-truth files, shipped unchanged in `data/cet_rules/<set>_<effective date>/` (claims_2026-03-12 from
  /cet_ui/spec/, budget_filing_2026-03-11 from /filings/spec/; re-downloaded 2026-10-26, the claims files unchanged, the budget-filing spec newer than the
  copy in the research repo): validation_rules (reject), warning_rules (warning) and the spec's field constraints (NotEmpty, CedarsValueList, CedarsNumRange,
  GreaterThanOrEqualTo, nvarchar length, numeric, bit, CedarsYearQuarter, StartsWith, a two-column primary key). 215 / 221 rules. IDs are content hashes
  (V/W/S + 6 hex), the same in both sets for a shared rule. `RunConfig.filing_validation` ("" | claims | budget_filing) + `filing_year` (CLI, window, New run,
  reruns, Edit inputs' Validate inputs): preflight adds `cet_reject` / `cet_warning` issues with cells, `cet_rule_not_checked` notes, the
  `CET filing validation` stat, `PreflightReport.filing` (failing records) and `.filing_rules`; the run writes `filing_validation_<tag>` and the
  measure-level columns FilingValidation (pass/warning/reject) and FilingRules. Nothing is blocked. `outside_cet_range` is skipped when the rules are on.
  Choices (CEDARS' code is not published): a blank makes a comparison unknown (condition false, test fails), text case-insensitive, value lists by claim
  year, AvoidedCostCombo checked against the profile library (exact end use + IOU + sector; not checked without it), UploadPA-YYYY a plain prefix (CEDARS
  accepted "PGE-2028Q4-..."; a split-on-dash version gave 703 false rejects). Messages follow the one CEDARS wording seen. Found while building: the spec-line
  regex dropped every nvarchar line (`\b` after ")"), caught by a test that counts the spec's constraints; tests also give every rule a passing and a
  failing record. Checked on the 1M job (PG&E's accepted 2028-2031 filings): 0 measures rejected under either set, 139,612 with warnings (the
  UnitMeaCost1stBaseline = 0 warning is the one CEDARS' own upload showed); budget filing rejects 282 ProgramCost rows because the job spans four years.
  Cost at 1M, laptop, warm: preflight 28.6-30.1 s -> 38.0 s.
- 0.1.41 (2026-10-26, Jake): progress estimate. Replaying `progress.fraction` over the 2026-10-26 server log (1M, csv.zip, 412 s) showed it ran low
  all run (-13% at preflight, -19% at pricing, -35% mid-writing) and sat at 98% saying "about 10 s left" for the 58 s the csv.zip conversion took (no
  report inside TableWriter.close). Now: `progress.plan(in_parts, format)` (called in run_job once the split is known) weights stages by their seconds in that
  server run (`SECONDS_IN_PARTS` / `SECONDS_IN_ONE_PIECE`, the file stage by `FILE_SECONDS[format]`: csv.zip 58, parquet 18; csv 45 and xlsx 5 are guesses); a
  skipped stage gets no weight (no "loading" in a run in parts). New "file" stage: chunked reports it from `TableWriter.close(on_progress)` as the parquet parts
  are joined and the CSV batches written (`file_share`); writing's parts are now len(parts), not +1. The web pages count the time left down between reports
  (`progress.live`; "almost done" at 0). Every report is also appended to `progress_log.jsonl` in the run folder (with `elapsed`), to check estimates against
  the real finish. Same replay with the new weights: within 0-4% while more than 30 s are left, but in sample (the weights come from that run).
  Pricing and writing are scaled by `OPTION_FACTORS[(societal, emissions)]` (100k laptop runs: pricing 20 / 12 / 17 / 10 s for both on / no SCT / no
  emissions / neither); `replan(societal=False)` when the ACC has no SCT avoided costs. Before it, a no-SCT run's estimate was up to 73-83% high early on;
  after, the bar's stage shares are within ~2 points of the time shares in all four cases (estimate within about +-20-30%, the misses in the first seconds).
- 0.1.40 (2026-10-25, Jake): a run in parts adds up the Results page's group sums part by part in pass 2 (`results_view.group_sums` of each part's
  frame, `combine_parts` orders them as the whole table's) instead of reading the measure-level file back at the end. On the 0.1.39 server run that read-back
  was the run's memory peak (1.11-1.15 GiB of 1.367 GiB, in its last 7 s, on top of the ~1 GiB the process already held). 1M laptop run, csv.zip, 50,000-row
  parts and groups, same sitting: the step goes 4 s -> 0 s (run 311 -> 305 s), pass 2 unchanged at 37 s; kept sums equal the file's (test).
- 0.1.39 (2026-10-25, Jake): Results page and progress bar. The Results page read the whole measure-level table (1M run: 6.89 GB committed, 2.91 GB RAM,
  too much for the server's 1.4 GB container). `results_view.RunResults` builds every level above the measure from group sums: one row per (PA, PrgID,
  program name, sector, segment) with the summed additive columns and a count, read from the parquet in 20,000-row batches of only the needed columns and
  kept as `_results/group_sums1_<file>.parquet` (never named measure_level_*: outputs.results_source globs that). The run writes it at the end
  (`keep_group_sums`: from the frame in a one-piece run, from the file in a run in parts); the page rebuilds it when missing or older. 1M run: first build
  3.8 s / 0.38 GiB, later loads 0.4 s / 0.08 GiB; tables equal the frame-based ones. The measure level streams only its columns, and is off above
  `MAX_RUN_MEASURE_ROWS` (50,000) for the whole run (web tab disabled, CSV 400; desktop note) besides the 5,000-per-PA limit. Progress: `recet/progress.py`
  writes `progress.json` (stage, parts done, fraction, seconds left) when RECET_PROGRESS_FILE is set (the web runner sets it; window and CLI unaffected);
  stages weighted by the 0.1.38 server run (split 0.04, preflight 0.17, loading 0.03, pricing 0.48, writing 0.26); parts reported in preflight, pricing
  and writing; a comparison gives each child RECET_PROGRESS_SLICE. Shown on the run page (updated with the log poll) and the runs list (reloads every 10 s
  while a run is queued or running). Time left = elapsed x (1 - f) / f, none before 3% or 5 s. No email yet.
- 0.1.38 (2026-10-25, Jake): splitting and preflight. `delimited._first_line` read the WHOLE file to get its header (600 MB on a 1M Measure.csv, on every
  separator check); it reads 64 KB now. `split_file` copies lines for a pipe-delimited file with no quotes (CET's format), rewriting only the header
  (`_split_lines`; blank lines dropped, cp1252 re-encoded; anything else still goes through pandas): 35 -> 2 s. `read_cet_csv` keeps each distinct read
  of a part (a folder with `PARTS_MARKER`, written by split_file) beside it -- text reads (dtype=str) as parquet, typed reads as pickle, both exact -- and
  later identical reads load that: a part was parsed 7 times a run, now 3 (part_dtypes' inference reads are not kept). Preflight reads the raw text once
  (`raw.mask(raw == "")` = the old na_values=[""] read). Also: `_nonblank_numeric_problems` tries one pyarrow cast per column first (exact to_numeric
  check only when a value fails, or casts to NaN); `programs.lookup` uses plain dicts and builds one row per distinct PrgID (0.25 -> 0.09 s a part; the
  built-in table is read once, `programs.builtin()` returns a copy); `outputs.credited_iou` reads the ProgramCost maps once per file (unallocated case).
  The run log now times splitting, column types, preflight, pass 2 assembly, the final table write and the summary, and reports the parts folder's
  disk use (1M job: 1.9 GB, including 1 GB of kept typed reads). 1M job, same sitting: 386 -> 313 s; before pricing 112 -> 79 s; peak memory before
  pricing 0.78 -> 0.27 GB; outputs and preflight report identical to 0.1.37.
  Also the writing stage (pass 2): `SummaryParts.add` sums each grouping with one vectorised groupby (it looped over the groups; summary equal to 1.5e-15),
  `programs.lookup` keeps its row dicts until `set_user_programs` changes the table (`_table_records`), and `credited_iou` is read once per file. 1M job,
  same sitting as main: writing stage 100 -> 62 s (parquet), 153 -> 112 s (csv.zip); whole run 391 -> 297 s / 436 -> 345 s; peak memory unchanged
  (1.63 / 1.77 GB). Tried and dropped: temporary part files without dictionary encoding (0.2 s a part faster to write, but the join's peak 0.4 GB
  higher). Left: compute_cost_tests (~0.2 s a part, spread over many column assignments) and the CSV conversion (~50 s of a csv.zip run).
