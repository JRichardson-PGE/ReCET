# ReCET

**ReCET** ("reset") is a rebuilt version of the CEDARS Cost Effectiveness Tool (CET). It prices every measure hour by hour (8,760 hours a year) instead of on
quarterly avoided costs, and runs the same cost tests as the CET (TRC, PAC, RIM, SCT Base/High, TSB). It started as the proof of concept in the CEDARS
review research repository and was moved here on its own.

It prices CET-format inputs (`Measure.csv`, `ProgramCost.csv`) on the **2021**, **2022**, **2024** or **2026 ACC** (the 2026 ACC is the final v1b release; the 2021 and
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
   with a CET outputs file; preflight only.

**Run** starts the job in its own process and streams the log. Results go to `<output folder>/<run name>_acc<version>/`; every run also writes `preflight.txt` and
`run_settings.json` so it can be reproduced. **Settings > Save settings** writes a JSON that `recet run --config job.json` reproduces.

## Documentation

In the package (`recet/docs`, and **Help > Documentation** in the window): `USER_GUIDE.md` (every control, the command line, common jobs), `INPUT_FORMATS.md`, `INPUT_FIELDS.md` (every input field),
`OUTPUT_FIELDS.md` (every output field with its complete formula in terms of the input fields, its unit and a worked number) and `CALCULATIONS.md` (every formula, the validation against CET, the limitations, a worked example). Start with `docs/README.md`.

## Web app (example.org/ReCET)

`recet/web` is the same tool as a password-protected web app: New run (upload a workbook or CET files), Runs (queue, log, results tables,
downloads, and "Edit inputs and rerun", which opens a run's inputs and settings as a new draft), Edit inputs (the window's grid editor, up to 500
measures, saved as you type, with a "Validate inputs" button that runs a run's full input check on the draft without pricing it and outlines the cells it flags) and Docs (the documents below, plus a Value lists page showing every dropdown field's codes; admins can add, retire or
unretire codes there, and the changes reach Edit inputs' dropdowns and checks at once).
Input checks (preflight-only runs) go ahead of full runs in the queue. Accounts are by invitation only.

    pip install -e ".[web]"
    recet-web create-admin you@example.com      # prints a one-time link to choose a password
    recet-web serve                             # http://localhost:3000/ReCET (set RECET_WEB_SECURE_COOKIES=0 for plain http)

On the server it runs in Docker (`Dockerfile`, `docker-compose.yml`) behind the example.org edge nginx, with the reference data folder mounted
read-only. Settings are `RECET_WEB_*` environment variables (`recet/web/config.py`). `recet-web warm-cache` builds the ACC caches ahead of the first run;
`recet-web users` and `recet-web reset-link EMAIL` manage accounts from the command line (also on the Users page for admins).

## Command line

    recet run --measures Measure.csv --programcost ProgramCost.csv --acc 2026 --out results
    recet run --measures Measure.csv --acc 2024 --compare-with 2026 --out results       # two runs + per-measure comparison
    recet run --measures Measure.csv --acc 2026 --profiles-8760 my_shapes.csv --acc-electric-file my_acc.parquet --components --portfolio-8760
    recet preflight --measures Measure.csv --acc 2026
    recet template all --out templates
    recet check acc-electric my_acc.parquet

## From Python

    from recet.jobs import RunConfig, run_job
    result = run_job(RunConfig(measures="Measure.csv", programcost="ProgramCost.csv", out_dir="results", acc_version="2026", components=True))
    result.frame            # the measure-level table
    result.files            # every file written

## Tests

    pip install -e ".[test]"  &&  pytest        # from the repository root; the unit tests need no reference data

Tested on Python 3.14 with pandas 3.0, numpy 2.5, pyarrow 25. The pricing methodology and its validation against real CET runs are in
`recet/docs/CALCULATIONS.md`; the research behind it (ACC extraction, CEDARS reproduction, CET findings) stays in the CEDARS review research repository.
