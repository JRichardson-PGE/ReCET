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

Python 3.11+. Needs `numpy`, `pandas`, `pyarrow` (installed automatically); the window uses `tkinter`, which ships with the python.org installers for Windows and macOS
(on some Linux distributions: `apt install python3-tk`).

## The reference data folder (the big files)

The electric ACC files (~1 GB each for the 2024 and 2026 ACCs; the TRC-only 2021 and 2022 ones are smaller and optional) and the CEDARS 8,760-hour profile library (~9 GB) are too large for GitHub, so they are **not in the package**. They live in a
*reference data folder* you can put anywhere (another drive, a share) and change at any time:

* the **Reference data folder** box in the window (with *Save as default*), or
* `recet data set-dir D:\cedars_reference_data`, or the environment variable `RECET_DATA`, or `--data-dir` on any command.

    recet data status                       what is there and what is missing
    recet data verify D:\cedars_reference_data --full     check sizes and SHA-256 against manifest.json
    recet data build D:\cedars_reference_data             copy (or --link) the files the tool currently finds into a new folder
                       [--pack-version 2026-10-30] [--draft 2028]   name the pack; mark an ACC as a draft (analysis runs only)
    recet data draft D:\cedars_reference_data 2028 [--final]      mark an ACC in a built folder as draft, or final
    recet data tie-out D:\cedars_reference_data --acc 2024 [--source-totals totals.csv]
                                                          the acceptance report: structure, control totals against the
                                                          source workbook's, reference measures engine vs tests/reference_calc.py
    recet data pack D:\cedars_reference_data D:\parts     split it into <= 4 GB parts (+ SHA256SUMS) for transfer
    recet data unpack D:\parts E:\cedars_reference_data   rebuild it on the other machine
    recet data consolidate-profiles D:\cedars_reference_data D:\consolidated
                                                          the profile library with each profile whose every zone's 8,760 is
                                                          identical stored once, every name kept; checked name by name.
                                                          Into a reference folder (e.g. a `data build --link` copy) it replaces the two
                                                          library files and updates that folder's manifest; the linked source is untouched

Everything small (the run parameters and CET tables, gas profile library, the four gas ACCs, statewide-allocation factors, the CEDARS Programs list and filing rules) is inside the package.

The tool also keeps a rebuildable **cache** of what it derives from those files (the ACC as memory-mapped arrays, the impact profiles a job used) in `~/.recet/cache` so later runs start fast;
`recet data clear-cache` removes it and `RECET_NO_CACHE=1` runs without it.

## In the window

1. **Job inputs** - one Excel job workbook (sheets Measure, ProgramCost, RunParameters, SWAllocation, Programs, GasProfiles, Profiles8760), or `Measure.csv` and optional
   `ProgramCost.csv`; an output folder.
2. **ACC version** - 2021, 2022, 2024 or 2026 (the 2021 and 2022 ACCs are TRC only), or type a new label; and CET's *First Year*.
3. **Separate inputs** - 8,760 electric profiles, gas monthly profiles, run parameters (discount rates, base year, gas CO2 per PA), SW allocation factors,
   program metadata (the CEDARS Programs list), an electric ACC, a gas ACC. Each has a **Template…** button. *A supplied item that matches a built-in
   one replaces it; one that matches nothing is added.* Formats and matching rules: `recet/docs/INPUT_FORMATS.md` (Help > Documentation).
4. **Modes** - calculate ACC components separately; produce a combined 8760 electric load profile; apply 5% market effects (benefits and costs together); include societal
   tests (SCB/SCH); include emissions; allocate statewide programs; leave Exclude_From_CE programs out of the summary; run a second ACC version and compare measure by measure; compare
   with a CET outputs file; preflight only; CEDARS filing checks (Quarterly Claims or Budget filing: CEDARS' own upload checks, shipped as it publishes them in
   `recet/data/cet_rules/`, flag the records it would reject or warn on; they are still priced; `recet/filing_rules.py`).

**Run** starts the job in its own process and streams the log. Results go to `<output folder>/<run name>_acc<version>/`; every run also writes `preflight.txt`, `preflight_findings.xlsx` (the input check as a workbook: findings and flagged cells) and
`run_settings.json` so it can be reproduced. **Settings > Save settings** writes a JSON that `recet run --config job.json` reproduces.

## Documentation

In the package (`recet/docs`, and **Help > Documentation** in the window): `USER_GUIDE.md` (every control, the command line, common jobs), `INPUT_FORMATS.md`, `INPUT_FIELDS.md` (every input field),
`OUTPUT_FIELDS.md` (every output field with its complete formula in terms of the input fields, its unit and a worked number), `CALCULATIONS.md` (every formula, the validation against CET, the limitations, a worked example), `COST_TESTS.md` (the cost tests and value streams as a versioned definition set), `WEB_GUIDE.md` (the web app's pages) and `ARCHITECTURE.md` (requirements, layers and modules, the run flow, data, the web app's deployment and security, the performance design). Start with `docs/README.md`.
What changed in each release is in [`CHANGELOG.md`](CHANGELOG.md).

## Web app

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

On the server it runs in Docker (`Dockerfile`, `docker-compose.yml`) behind a reverse proxy (nginx), with the reference data folder mounted
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
    recet cet-version list                                  # the prior CET releases ReCET reproduces, and their ACCs
    recet cet-version run --release 24.3 --acc 2022 --measures Measure.csv --programcost ProgramCost.csv --me-bens 0.05 --me-cost 0.05 --out cet243

`recet cet-version run` prices a job exactly as that CET release did, on its own quarterly tables and with its errors (CALCULATIONS C20); it needs the
releases' table export in `<reference data folder>/cet_versions/`. ReCET's own runs are unchanged by it.

## Batch worker: a job spec in, outputs and a receipt out

`recet worker run job.json` is the contract a batch system (the CEDARS platform's Calculations module) calls: it reads a versioned job spec (schema:
`recet worker schema`, `recet/data/job_spec/job_spec_v1.schema.json`; rules: `recet/jobspec.py`), fetches the inputs from local paths, `file://` or
`s3://` (`pip install ".[s3]"`; boto3 is imported only for S3), refuses the job when its engine release, a pinned input or reference file, or the
allocation table differs from what it asks for, runs it, and sends the run folder with `receipt.json` to the spec's outputs URI.

    {"spec_version": 1, "job_id": "2028-claims-pge-0001",
     "engine": {"release": "0.1.93", "calculation": "recet"},
     "inputs": {"job_zip": {"uri": "s3://bucket/jobs/0001/job.zip", "sha256": "..."}},
     "settings": {"acc_version": "2024", "acc_purpose": "claims"},
     "packs": {"files": {"acc/2024/electric_hourly.parquet": {"sha256": "..."}}},
     "allocation_version": "statewide_factors_20260918",
     "outputs": {"uri": "s3://bucket/runs/0001/", "format": "csv.zip"},
     "context": {"platform_run_id": 123}}

The receipt names the engine (release, calculation, image and commit from `RECET_IMAGE` / `RECET_COMMIT`, Python and library versions), every input's
URI, SHA-256, size and row count, the full settings, the ACC and the ACC each record was priced on, every reference file used with its SHA-256 (from the
reference folder's `manifest.json`, else computed) and the packaged data's hash, the run parameters and statewide factor table, the filing rule set,
every output file's SHA-256, the totals and notes, and the spec's `context` (the caller's own references) as given. While the job runs, the worker
sends `progress.json` (stage, parts done, fraction, seconds left; progress.py) to the outputs URI at each stage change and at most every 10 s, and `log.txt`
(what the run printed, each line timed) with it when it has grown; the whole log goes with the outputs at the end and is listed in the receipt. A spec with `compare_with` prices the job on both ACCs
(`run_acc<ACC>/` each) and writes the comparison beside them, as `recet run --compare-with` does; the receipt's totals are the primary ACC's. Exit codes: 0 succeeded, 1 the run failed (a receipt is still written), 2 the spec was refused.
`recet worker validate job.json` checks a spec without running it.

`recet worker check check.json` runs a filing's rules in this release instead of pricing it (`recet/checkjob.py`): the CEDARS platform keeps each
filing's rules as data and has the worker of the release its specification names check the filing, so the findings come from the release that prices it.
The check spec gives the rules (each with its definition), the tables by URI (UTF-8 CSV, read as text, optionally pinned by SHA-256), and what the wider
rule language reads (the filing year, the upload date, who files, each table's key columns, the program records, value lists the engine does not ship,
changes over the shipped lists); `findings.json` (each failing rule's rows and values, and notes on rules that could not be checked) and `receipt.json`
go to its outputs URI. Same exit codes. The rule language (`recet/filing_rules.py`) reads CEDARS' rule files as before and, for rules kept as data,
conditions on another table of the filing (linked by the tables' keys), on the program record or on who files, groups of conditions (all / any, one
level), and tests for text, flags, dates, a date within so many years of the upload and a code in effect on a date.

The worker image (`Dockerfile.worker`: the locked `requirements/worker.txt`, which is the engine plus boto3, and no web app) is published for each
engine release: push the tag `vX.Y.Z` matching `recet/__init__.py` and `.github/workflows/worker-image.yml` builds it for amd64 and arm64 as
`ghcr.io/jrichardson-pge/recet-worker:X.Y.Z`, with `RECET_IMAGE` and `RECET_COMMIT` set so every receipt names the image. Mount the reference data
read-only at `/data`; S3 credentials come from the environment or the instance / task role:

    docker run --rm -v /path/to/reference:/data:ro ghcr.io/jrichardson-pge/recet-worker:X.Y.Z run s3://bucket/jobs/0001/job.json

## From Python

    from recet.jobs import RunConfig, run_job
    result = run_job(RunConfig(measures="Measure.csv", programcost="ProgramCost.csv", out_dir="results", acc_version="2026", components=True))
    result.frame            # the measure-level table; None for a job run in parts (more rows than chunk_size): read result.files["measure_level"]
    result.files            # every file written

## Performance

The target is at least 1,000,000 records priced on 8,760 profiles in 15 minutes. A 1,000,000-record job (four real PG&E 2028-2031 filings repeated, 2024 ACC,
societal tests and emissions on) takes about 7 minutes on the web server (2 ARM vCPUs, 2 GiB; peak 1.01 GiB in its 1.4 GB container) and under 5 minutes on
a 16-thread laptop (about 1 GB committed). Turning the societal tests off saves about 15%. A job runs in parts of 50,000 measures (`chunk_size`), so memory
follows the part size, not the job size; how that and the rest of the performance design work, with the figures, is in `recet/docs/ARCHITECTURE.md`
(sections 1.4 and 9). The web app's own limits (upload size, one worker, its server's memory) are separate.

## Tests

    pip install -e ".[test]"  &&  pytest        # from the repository root; the unit tests need no reference data

or, with the exact versions CI and the Docker image use (`requirements/README.md`; every file checked against its hash):

    pip install --require-hashes -r requirements/test.txt  &&  pip install --no-deps -e .  &&  pytest

CI tests Python 3.11 and 3.12 with the locked versions; it also runs on 3.14. The pricing methodology and its validation against real CET runs are in
`recet/docs/CALCULATIONS.md`; the research behind it (ACC extraction, CEDARS reproduction, CET findings) stays in the CEDARS review research repository.
