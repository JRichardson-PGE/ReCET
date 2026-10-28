"""PrgIDs that do not line up between Measure.csv and ProgramCost.csv (BRD BR-06.16): the run goes on with warnings. A measure whose program has no
ProgramCost row is priced with no program costs; a ProgramCost row with no measures is charged to none (CALCULATIONS C17 item 11)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from recet import outputs
from recet.jobs import run_job
from test_chunked import CHUNK, _cfg, _job, _same

QUIET = dict(log=lambda s: None)


def _costs(pc, path, drop=(), zero=(), extra=None):
    cost = pd.read_csv(pc, sep="|", dtype=str, keep_default_na=False)
    cost = cost[~cost["PrgID"].isin(drop)].copy()
    num = [c for c in cost.columns if c not in ("PrgID", "PrgYear", "ClaimYearQuarter", "PA")]
    cost.loc[cost["PrgID"].isin(zero), num] = "0"
    if extra is not None:
        add = cost.iloc[:2].copy()
        add["PrgID"] = extra
        cost = pd.concat([cost, add], ignore_index=True)
    cost.to_csv(path, sep="|", index=False)
    return path, cost


@pytest.mark.parametrize("chunk", [0, CHUNK])
def test_unmatched_prgids_warn_and_still_price(tmp_path, chunk):
    m, pc = _job(tmp_path)
    loose, cost = _costs(pc, tmp_path / "loose.csv", drop=["PRG2"], extra="PRG9")       # PRG2's measures have no cost row; PRG9 has no measures
    aligned, _ = _costs(pc, tmp_path / "aligned.csv", zero=["PRG2"])                     # the same job, aligned, with PRG2's costs 0
    run = run_job(_cfg(tmp_path, m, loose, run_name="loose", chunk_size=chunk), **QUIET)
    ref = run_job(_cfg(tmp_path, m, aligned, run_name="aligned", chunk_size=chunk), **QUIET)

    issues = {i["code"]: i for i in json.loads((run.run_dir / "preflight.json").read_text())["issues"]}
    assert issues["measures_without_program_costs"]["severity"] == "warning" and issues["measures_without_program_costs"]["examples"] == ["PRG2"]
    n_prg2 = sum(1 for i in range(40) if i % 3 == 2)
    assert issues["measures_without_program_costs"]["message"].startswith(f"{n_prg2} measure(s) in 1 program(s)")
    assert len(issues["measures_without_program_costs"]["cells"]) == n_prg2
    orphan = cost[cost["PrgID"] == "PRG9"]
    dollars = orphan.drop(columns=["PrgID", "PrgYear", "ClaimYearQuarter", "PA", "OnBillFinancing"]).astype(float).to_numpy().sum()
    assert issues["program_costs_without_measures"]["severity"] == "warning" and f"${dollars:,.0f}" in issues["program_costs_without_measures"]["message"]
    assert not any(i["severity"] == "error" for i in issues.values())

    a, b = (outputs.read_table(outputs.results_source(r.run_dir)) for r in (run, ref))
    assert (a.loc[a["PrgID"] == "PRG2", "WeightedProgramCost"] == 0).all()
    assert (a.loc[a["PrgID"] == "PRG2", "CreditedPA"].fillna("") == "").all()       # no ProgramCost row to credit it to
    _same(b.drop(columns="CreditedPA"), a.drop(columns="CreditedPA"))              # otherwise the aligned job with PRG2's costs at 0 and no PRG9
    assert run.totals == pytest.approx(ref.totals, rel=1e-12)
