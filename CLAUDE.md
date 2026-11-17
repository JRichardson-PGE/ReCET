# ReCET

ReCET ("reset") is the rebuilt CEDARS Cost Effectiveness Tool: it prices CET-format inputs (Measure / ProgramCost, or one Excel job workbook)
hour by hour on the ACC and writes CET's outputs per measure, with a tkinter GUI (`python -m recet`), a web app (`recet/web`), a CLI (`recet run|preflight|template|check|data|...`) and a batch worker (`recet worker`).
It was moved out of the CEDARS review research repository (`JRichardson-PGE/CEDARS-Research`, folder `poc/`, package then called `cedars_poc`) on
2026-10-09 with a fresh history. The research behind it (ACC extraction and validation, CEDARS quarterly-avoided-cost reproduction, CET error findings,
TOU work) stays in that repo; its CLAUDE.md and `poc/README.md` hold the evidence for the methodology.

## Rules that keep the tool honest

- The repository is public (MIT, 2026-10-30). Nothing non-public goes into a commit, its message or a PR: no secrets, server addresses, SSH users or key
  paths, bucket names, personal emails or laptop paths, real PII, or the development vendor's name. Commit under a noreply address. CI's secret scan
  (`gitleaks.toml`: gitleaks' defaults plus those patterns, over the whole history, and a noreply check on every author) enforces it.
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
- Dependencies are locked (phase 0 of the CEDARS platform build plan, 2026-10-30): `requirements/{engine,worker,web,test}.txt`, every platform and Python 3.11+,
  with hashes, made by `python requirements/lock.py` (uv) from pyproject.toml. The Dockerfile installs `web.txt` with `--require-hashes`, then ReCET with
  `--no-deps`; the Lockfile workflow fails when the files no longer match pyproject.toml. `Dockerfile.worker` installs `worker.txt` (engine + boto3): the batch worker image, published
  per release tag vX.Y.Z (= `__version__`, checked) to ghcr.io/jrichardson-pge/recet-worker:X.Y.Z by `.github/workflows/worker-image.yml`. Change a dependency in pyproject.toml and re-run lock.py in the
  same PR (`--upgrade` to take newer versions, and run the tests on them).
- ACC versions are keyed by ACC year (2021, 2022, 2024, 2026), and run parameters / E3 tables are picked by ACC version. The web pages name a version by
  its ACC year only, "2026 ACC" (`versions.acc_short`, `web/app.acc_name`; Jake 2026-10-26, retiring the "Avoided Cost Version" label), and New run
  has a "Which ACC applies to my filing?" table instead: `versions.BUDGET_FILING_ACC` (filing, program years, ACC, basis; CET's resolution table through
  2027, then Jake's 2026-10-26 schedule, the last two rows inferred) and `versions.claims_acc()` (claims = the ACC of the year's final budget filing, one per
  program year, through CLAIMS_LAST_YEAR 2035). The page shows it compactly (Jake: the two full tables were too big): `versions.acc_summary()`, one row per
  ACC 2021-2028 with its filings' short names and its claim years; dimmed = not in versions.BUILTIN (not by the server's ACC list). That table is also the source for the preflight's ACC check (0.1.47). The desktop window and the outputs'
  POC_ACC label still show both names (`versions.acc_label`, "2024 ACC (Avoided Cost Version 2026)", as of 2026-10-22); the 2026 ACC is Avoided
  Cost Version 2028 although CET's draft tables number it 2027 (`AVOIDED_COST_VERSION`; tables are still read through `versions.CET_VERSION_TO_ACC`).
  Inside the packaged CET exports CET's numbers are mapped on read.
- Each ACC's hourly data follow one calendar's weekdays: 2018 for 2024/2026, 2020 for 2021/2022 (read from the file's hour stamps,
  `real_impact_profiles.acc_calendar_year`). An ACC year always has 8,760 hours (the last day of a leap year is dropped); never add leap-day handling.
- The 2021 and 2022 ACCs are TRC only: SCT columns are left blank. ReCET keeps its own behind-the-meter methane rule (Residential fuel-substitution
  profiles only) on every vintage, although CET's Version 2024 adds it to all Residential gas and Version 2022 never does.

- Optional ReCET-only Measure columns (not CET fields; `templates.RECET_MEASURE_COLUMNS`) follow CET's 74: `AC_Elec_Impact_Profile_2nd_Baseline` (2nd-baseline shape,
  priced as a second part, `run_poc.split_by_baseline_shape`), `AC_Elec_Impact_Profile_Water` (water-energy shape) and `ACCVersion` (an ACC version per
  measure), then `AC_Elec_Impact_Profile_Parent` / `AC_Elec_Impact_Profile_Parent_2nd_Baseline` (a true-up's parent profiles, 0.1.89). Blank = the default.
  Editor CET files include them only when used.
- Seven Measure fields are renamed from their E3-calculator names (Jake, 2026-10-22): `recet/fieldnames.py`. Inputs may use either name (read under the new
  name by `pipeline/delimited.read_cet_csv`); outputs, templates and workbooks use the new names; the editor's CET files use CET's names (`to_cet`).
- ReCET's copies of CET tables drop CET's E3 prefix (Jake, 2026-10-23): `data/run_parameters.csv` (CET E3Settings) and `data/cet_tables/*.parquet`
  (emissions, combustion_type, rate_schedule_*). The five value lists for the AC_ fields are `data/value_lists/ac_*.csv`; the web DB moves saved admin
  changes from the old list names on start-up (`web/db.VALUE_LIST_RENAMES`). References to CET's own tables (E3AvoidedCostElecCO2Seq..., dbo.E3...) keep CET's names.

## Rules for new code

- Every release adds an entry at the top of `CHANGELOG.md` and bumps `recet/__init__.py`'s `__version__` in the same PR. Earlier releases' notes are
  there, not here: read a release's entry before changing what it built.
- Anything that needs the whole job (a program-level share, a summary, a cross-row check) needs a parts version too (`recet/chunked.py`);
  `tests/test_chunked.py` compares every output file of a run in parts with the whole-job run.
- `import recet` before numpy in a new entry point (it sets `OPENBLAS_NUM_THREADS=1`).
- `AccStore.series` returns read-only views of a memory-mapped cache, and every name of a stored impact profile shares one shape dict: never write into
  either in place.
- Never put a non-input value under an input's name (NTG + MEBens is `NTGRkWh_plus_MEBens`); market effects are a factor applied when a pricer's
  `Priced_Quantity_*` / `Billed_*` columns are filled, never a rewrite of `AC_Net_*`.
- Cost-test arithmetic lives only in `pipeline/test_evaluator.py`: a new test is a new cost-test set, and a published set is never edited (a change is a new
  folder). Pass the run's set (`result.cost_tests.set`) to anything new that summarizes tests.
- A deliberate change to outputs updates the regression fixtures (`recet regress check --update`) and the PR says why.
- Web pages: never disable the focused control (use aria-disabled); announce through `window.recetAnnounce`, not a new live region; vendored scripts only
  (the CSP allows same-origin scripts), never a CDN.
