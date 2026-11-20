# ReCET

**ReCET** ("reset") is a rebuilt version of the CEDARS Cost Effectiveness Tool (CET). It prices every measure hour by hour (8,760 hours a
year) instead of on quarterly avoided costs, and runs the same cost tests as the CET (TRC, PAC, RIM, SCT Base/High, TSB) plus the
Participant Cost Test. It takes CET-format inputs (`Measure.csv` and `ProgramCost.csv`, or one Excel job workbook; CET's E3 field names
are accepted) on the **2020**, **2021**, **2022**, **2024** or **2026 ACC** (2020-2022 are TRC only) and writes one row per measure with
every input and every output. It started as the proof of concept in the CEDARS review research repository.

## Install and run

    pip install recet-<version>-py3-none-any.whl        # or, from a checkout:  pip install -e .

Python 3.11+. Its dependencies are installed automatically (`pyproject.toml`); the window needs `tkinter` (in the python.org installers;
on some Linux distributions `apt install python3-tk`).

| Way to run | Start it | Guide |
|---|---|---|
| Desktop window | `python -m recet` (also `recet`, `recet-gui`) | [Desktop and command line](recet/docs/USER_GUIDE.md) |
| Command line | `recet run --measures Measure.csv --programcost ProgramCost.csv --acc 2026 --out results`; `recet preflight ...` checks a job without pricing it | [Desktop and command line](recet/docs/USER_GUIDE.md) (every command and flag) |
| Web app | `pip install -e ".[web]"`, `recet-web create-admin you@example.com`, `recet-web serve` | [Getting started on the web](recet/docs/WEB_GUIDE.md); deployment in [System architecture](recet/docs/ARCHITECTURE.md) section 8 |
| Batch worker | `recet worker run job.json` (the CEDARS platform's calculation contract; image `ghcr.io/jrichardson-pge/recet-worker:X.Y.Z` per release tag) | [Batch worker](recet/docs/WORKER.md) |
| Python | `from recet.jobs import RunConfig, run_job` | [Desktop and command line](recet/docs/USER_GUIDE.md) |

## The reference data folder

The electric ACC files (about 1 GB each for the 2024 and 2026 ACCs) and the CEDARS 8,760-hour profile library (about 9 GB) are too large
for the package. They live in a *reference data folder* you choose: the window's **Reference data folder** box, `recet data set-dir <folder>`,
`RECET_DATA` or `--data-dir`. `recet data status` says what is there and what is missing; the other `recet data` commands (verify, build,
pack, tie-out ...) are in the user guide. Everything small (run parameters, CET tables, gas profiles and ACCs, statewide factors, the CEDARS
Programs list and filing rules) ships inside the package.

## Performance

1,000,000 records on 8,760 profiles take about 7 minutes on the 2-vCPU web server and under 5 minutes on a 16-thread laptop, against a
15-minute target; jobs run in parts of 50,000 measures, so memory follows the part size ([System architecture](recet/docs/ARCHITECTURE.md)
sections 1.4 and 9).

## Documentation

| Document | Read it to learn |
|---|---|
| [Getting started on the web](recet/docs/WEB_GUIDE.md) | the web app's pages |
| [Desktop and command line](recet/docs/USER_GUIDE.md) | the window's every control, the command line, common jobs, troubleshooting |
| [Input formats](recet/docs/INPUT_FORMATS.md) and [Input fields](recet/docs/INPUT_FIELDS.md) | how supplied files are matched and read; every input field |
| [Output fields](recet/docs/OUTPUT_FIELDS.md) | every output, with its complete formula and a worked number |
| [Calculations](recet/docs/CALCULATIONS.md) | the method, the validation against CET, the deliberate differences |
| [Cost tests as configuration](recet/docs/COST_TESTS.md) | the versioned cost-test definition sets |
| [Batch worker](recet/docs/WORKER.md) | the job spec, the receipt and the image |
| [System architecture](recet/docs/ARCHITECTURE.md) | requirements, modules, the run flow, data, the web app's deployment and security, performance |

The documents are inside the package (**Help > Documentation** in the window) and checked by tests against the code. What changed in each
release is in [CHANGELOG.md](CHANGELOG.md).

## Tests

    pip install -e ".[test]"  &&  pytest        # from the repository root; the unit tests need no reference data

or, with the exact versions CI and the Docker image use (`requirements/README.md`; every file checked against its hash):

    pip install --require-hashes -r requirements/test.txt  &&  pip install --no-deps -e .  &&  pytest

CI tests Python 3.11 and 3.12 with the locked versions, and also runs on 3.14. The research behind the method (ACC extraction, CEDARS
reproduction, CET findings) stays in the CEDARS review research repository.
