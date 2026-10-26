# ReCET

**ReCET** ("reset") is a rebuilt version of the CEDARS Cost Effectiveness Tool (CET). It prices every measure hour by hour (8,760 hours a year) instead of on
quarterly avoided costs, and runs the same cost tests as the CET (TRC, PAC, RIM, SCT Base/High, TSB) plus the Participant Cost Test, which the CET does not yet have. It started as the proof of concept in the CEDARS
review research repository and was moved here on its own.

It prices CET-format inputs (`Measure.csv`, `ProgramCost.csv`; CET's legacy E3 field names such as `E3ClimateZone` are accepted alongside ReCET's `AC_...` names) on the **2021**, **2022**, **2024** or **2026 ACC** (the 2026 ACC is the final v1b release; the 2021 and
2022 ACCs are TRC only) and writes one row per
measure with every input column and every output, from a graphical interface or the command line.

## Install and run

    pip install recet-<version>-py3-none-any.whl        # or, from a checkout:  pip install -e .
    python -m recet                                   # opens the window   (also: recet, recet-gui)

Python 3.10+. Needs `numpy`, `pandas`, `pyarrow` (installed automatically); the window uses `tkinter`, which ships with the python.org installers for Windows and macOS
(on some Linux distributions: `apt install python3-tk`).

## The reference data folder (the big files)

The two electric ACC files (~1 GB each) and the CEDARS 8,760-hour profile library (~9 GB) are too large for GitHub, so they are **not in the package**. They live in a
*reference data folder* you can put anywhere (another drive, a share) and change at any time:

* the **Reference data folder** box in the window (with *Save as default*), or
* `recet data set-dir D:\cedars_reference_data`, or the environment variable `RECET_DATA`, or `--data-dir` on any command.

    recet data status                       what is there and what is missing
    recet data verify D:\cedars_reference_data --full     check sizes and SHA-256 against manifest.json
    recet data build D:\cedars_reference_data             copy (or --link) the files the tool currently finds into a new folder
    recet data pack D:\cedars_reference_data D:\parts     split it into <= 4 GB parts (+ SHA256SUMS) for transfer
    recet data unpack D:\parts E:\cedars_reference_data   rebuild it on the other machine

Everything small (E3 settings and tables, gas profile library, both gas ACCs, statewide-allocation factors) is inside the package.

The tool also keeps a rebuildable **cache** of what it derives from those files (the ACC as memory-mapped arrays, the impact profiles a job used) in `~/.recet/cache` so later runs start fast;
`recet data clear-cache` removes it and `RECET_NO_CACHE=1` runs without it.

## In the window

1. **Job inputs** - one Excel job workbook (sheets Measure, ProgramCost, RunParameters, SWAllocation, GasProfiles, Profiles8760), or `Measure.csv` and optional
   `ProgramCost.csv`; an output folder.
2. **ACC version** - 2024 or 2026 (or type a new label), and CET's *First Year*.
3. **Separate inputs** - 8,760 electric profiles, gas monthly profiles, run parameters (discount rates, base year, gas CO2 per PA), SW allocation factors,
   program metadata (the CEDARS Programs list), an electric ACC, a gas ACC. Each has a **Template…** button. *A supplied item that matches a built-in
   one replaces it; one that matches nothing is added.* Formats and matching rules: `recet/docs/INPUT_FORMATS.md` (Help > Input format guide).
4. **Modes** - calculate ACC components separately; produce a combined 8760 electric load profile; apply 5% market effects (optionally to costs too); include societal
   tests (SCB/SCH); include emissions; allocate statewide programs; leave Exclude_From_CE programs out of the summary; run a second ACC version and compare measure by measure; compare
   with a CET outputs file; preflight only; CEDARS filing checks (Quarterly Claims or Budget filing: CEDARS' own upload checks, shipped as it publishes them in
   `recet/data/cet_rules/`, flag the records it would reject or warn on; they are still priced; `recet/filing_rules.py`).

**Run** starts the job in its own process and streams the log. Results go to `<output folder>/<run name>_acc<version>/`; every run also writes `preflight.txt`, `preflight_findings.xlsx` (the input check as a workbook: findings and flagged cells) and
`run_settings.json` so it can be reproduced. **Settings > Save settings** writes a JSON that `recet run --config job.json` reproduces.

## Documentation

In the package (`recet/docs`, and **Help > Documentation** in the window): `USER_GUIDE.md` (every control, the command line, common jobs), `INPUT_FORMATS.md`, `INPUT_FIELDS.md` (every input field),
`OUTPUT_FIELDS.md` (every output field with its complete formula in terms of the input fields, its unit and a worked number) and `CALCULATIONS.md` (every formula, the validation against CET, the limitations, a worked example). Start with `docs/README.md`.

## Web app (jakemoto.com/ReCET)

`recet/web` is the same tool as a password-protected web app: New run (upload a workbook, or the CET files in one .zip: Measure.csv and an optional
ProgramCost.csv, as CET takes them; a bare CSV is not accepted), Runs (queue, log, results tables,
downloads, the input check and headline numbers on each run's page, "Rerun with different settings", which starts New run from a run's files and settings, and
"Edit inputs and rerun", which opens a run's inputs and settings as a new draft), Edit inputs (the window's grid editor, up to 500
measures, saved as you type, with a "Validate inputs" button that runs a run's full input check on the draft without pricing it and outlines the cells it flags) and Docs
(`docs/WEB_GUIDE.md` first, then the documents below, plus a Value lists page showing every dropdown field's codes; admins can add, retire or
unretire codes there, and the changes reach Edit inputs' dropdowns and checks at once).
Input checks (preflight-only runs) go ahead of full runs in the queue. A queued run shows its place in the queue and when it should start and finish
(`recet/web/eta.py`: estimated from its size and options, the running runs' own estimates, and how this server's recent runs compared with theirs). A running run shows a progress bar with its stage, parts done and an estimate of
the time left on its page and in the runs list (`recet/progress.py`; the stages are weighted by their share of a 1,000,000-measure run on the server, by output
format, and the time left counts down between updates; each update is also kept in the run folder's `progress_log.jsonl`). The
results tables are built from per-program sums kept beside the results (about 0.1 GB and under a second for a 1,000,000-measure run, against 6.9 GB
committed to read the whole table), and the measure-level tab is off for runs of more than 50,000 measures (the table is in the downloads).
Accounts are by invitation only.

    pip install -e ".[web]"
    recet-web create-admin you@example.com      # prints a one-time link to choose a password
    recet-web serve                             # http://localhost:3000/ReCET (set RECET_WEB_SECURE_COOKIES=0 for plain http)

On the server it runs in Docker (`Dockerfile`, `docker-compose.yml`) behind the jakemoto.com edge nginx, with the reference data folder mounted
read-only. Settings are `RECET_WEB_*` environment variables (`recet/web/config.py`). Uploads are capped at 150 MB per file (`RECET_WEB_MAX_UPLOAD_MB`) and a job zip at
1,024 MB unzipped (`RECET_WEB_MAX_UNZIPPED_MB`); a 1,000,000-row Measure.csv (603 MB) zips to 38 MB. Its CSV outputs
are zipped too (`<table>.csv.zip`; the 1M measure-level CSV is 3.5 GB, 0.74 GB zipped), and "Download all" and the inputs download are built on disk and streamed, not
held in memory. Excel output is for jobs of up to 50,000 measures (`excel_max_rows`); a larger job writes every table as zipped CSV instead, and the run log says so. Parquet output is written in 200,000-row groups (`parquet_row_group`; the 1M measure-level parquet is 253 MB) and is not zipped. The edge nginx in front of it must allow the same
upload size (`client_max_body_size`). `recet-web warm-cache` builds the ACC caches ahead of the first run;
`recet-web users` and `recet-web reset-link EMAIL` manage accounts from the command line (also on the Users page for admins).

## Command line

    recet run --measures Measure.csv --programcost ProgramCost.csv --acc 2026 --out results
    recet run --measures Measure.csv --acc 2024 --compare-with 2026 --out results       # two runs + per-measure comparison
    recet run --measures Measure.csv --acc 2026 --profiles-8760 my_shapes.csv --acc-electric-file my_acc.parquet --components --portfolio-8760
    recet preflight --measures Measure.csv --acc 2026
    recet preflight --measures Measure.csv --programcost ProgramCost.csv --acc 2026 --filing-validation budget_filing --filing-year 2028   # before a CEDARS upload
    recet template all --out templates
    recet check acc-electric my_acc.parquet

## From Python

    from recet.jobs import RunConfig, run_job
    result = run_job(RunConfig(measures="Measure.csv", programcost="ProgramCost.csv", out_dir="results", acc_version="2026", components=True))
    result.frame            # the measure-level table; None for a job run in parts (more rows than chunk_size): read result.files["measure_level"]
    result.files            # every file written

## Performance

The replacement BRD's target (NFR Performance) is at least 1,000,000 records priced on 8,760 profiles in 15 minutes or less. Measured with
`poc/benchmark_recet_scale.py` in the CEDARS review research repository: the four real PG&E 2028-2031 jobs repeated to 1,000,000 records, the 2024 ACC,
societal tests and emissions on, one process on a 16-thread laptop with 11.6 GB of memory (version 0.1.34, 2026-10-23). The job is run in 50 parts of
20,000 measures (the default `chunk_size`, see the user guide's "What a run does"):

| Stage | Seconds |
|---|---|
| splitting Measure.csv into parts and reading their column types | 29 |
| preflight checks (part by part) | 39 |
| pass 1: load and price each part (TRC, SCB, SCH, PAC, RIM, bill impacts, emissions) | 174 |
| pass 2: program costs over the whole job, assemble and write (parquet) | 59 |
| **total** | **301 (5.0 minutes, about 3,300 records a second)** |

Peak memory was **0.96 GB committed** (1.27 GB in RAM, which includes pages of the memory-mapped ACC and profile files that the system can drop and re-read).
How it came down, all on the same job:

| Version | How the job is run | Peak committed | Time |
|---|---|---|---|
| 0.1.32 | as one piece | 17.8 GB | 4.5 min |
| 0.1.33 | in parts of 50,000 | 2.28 GB | 4.6 min |
| 0.1.33 | in parts of 20,000 | 1.74 GB | 5.1 min |
| 0.1.34 | in parts of 20,000, one BLAS thread, ACC series read in place | **0.96 GB** | 5.0 min |

0.1.37 prices each measure's lifetime value per route instead of building a row per quarter of its life (CALCULATIONS.md C5.1), reads profile metadata from a
cached index, reads each route's first measure in one lookup for all routes and shifts each route's profile once. Measured against 0.1.36 in the same sitting:
pricing 173 -> 105 s, the whole run 298 -> 229 s (3.8 minutes), peak memory unchanged; every output equal to 2.2e-12. With 100,000-row parts pricing's own
peak falls from 2.11 to 1.20 GB, but writing then peaks at 2.29 GB, so 20,000 stays the default. Since 0.1.36 the 1M run peaks at 1.65 GB
because the measure-level parquet is joined into 200,000-row groups (`parquet_row_group`). On the web server (a t4g.small, 2 ARM vCPUs, 0.1.36, 50,000-row
groups, zipped CSV) the same job took 13.1 minutes and peaked at 1.05 GiB in its container.

As one piece the run expanded the whole job into about 54 million measure-quarter rows at once, more than the laptop's RAM, so part of it was paged to disk
(the 7.4 GB first reported for it was the peak held in RAM, not what the run needed). In 0.1.34, `import recet` sets `OPENBLAS_NUM_THREADS=1` (numpy's BLAS
reserved a 32 MB buffer per CPU thread, 0.5 GB here, and the pricing does not use its threads), and the ACC's hourly series are read straight from the
memory-mapped cache instead of being copied (`acc_store.scaled_column`). Every output of the run in parts equals the one-piece run's (worst difference 1e-13, in a
levelized ratio). Before 0.1.32 the same job took 8.6 minutes (pricing 401 s): the bill-impact, participant-cost, other-benefit and emissions steps
looped over measures one row at a time and are now computed on whole columns (`pipeline/engine.py`, `pipeline/emissions.py`; the row-by-row versions are kept
as `..._per_measure` references that the tests compare them with). The web app's own limits (upload size, one worker, its server's memory) are separate.

## Tests

    pip install -e ".[test]"  &&  pytest        # from the repository root; the unit tests need no reference data

Tested on Python 3.14 with pandas 3.0, numpy 2.5, pyarrow 25. The pricing methodology and its validation against real CET runs are in
`recet/docs/CALCULATIONS.md`; the research behind it (ACC extraction, CEDARS reproduction, CET findings) stays in the CEDARS review research repository.
