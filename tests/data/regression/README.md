# Regression fixtures

Real jobs on slices of the real reference data, with their expected outputs (`recet/regress.py`; phase 0 of the CEDARS platform build plan).
`tests/test_regression_gate.py` runs every job here on `ref/` and compares each output table with `jobs/<job>/expected/`, within the tolerance in
`manifest.json` (a number differs when |a - b| > 1e-6 + 1e-9 x |expected|; text must match).

| Job | Records | From |
|---|---|---|
| `c3_three_zones` | 12 | the C3 test job (CEDARS-Research OpenBCA comparison C3) |
| `k10_three_zones` | 1,270 | the 10k server test job (`recet-10k-job/ReCET_10k_job.zip`) |

Both keep only the records in PG&E zone 13, SCE zone 9 (and zone 5, priced on 9) and SDG&E zone 7 (and 6 and 8, priced on 7), so the slice holds three
(utility, zone) pairs of the 2024 ACC. Records in the System zone are left out: their zone is chosen from every zone's prices over the whole ACC, so
their slice would be most of the file (`tests/test_system_zones.py` covers that choice).

`ref/` holds only what the jobs' pricing read: the ACC rows of those pairs, with every value the pricing did not read set to NaN (the file keeps the
original's years, cost tests and columns), the profile library's row groups of the profiles used with only the zones used, and the whole catalog.

## When a check fails

* **An unintended change:** fix the code.
* **A deliberate change of results** (a corrected formula, new data): rewrite the expected outputs and say in the PR which outputs moved and why:

      recet regress check --update

## Rebuilding the slice

Only needed for new jobs or new reference data, on a machine with the full reference data folder. The build runs each job on the full folder,
slices it, runs each job again on the slice in a new process and stops unless every table is equal (1e-12 relative), then replaces this folder
(this README is kept):

    recet regress build --job c3_three_zones=tests/data/regression/jobs/c3_three_zones/inputs ^
                        --job k10_three_zones=tests/data/regression/jobs/k10_three_zones/inputs --acc 2024 --out tests/data/regression
