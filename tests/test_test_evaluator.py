"""The definition-set evaluator (recet/pipeline/test_evaluator.py) against the cost tests as 0.1.81 wrote them out (tests/reference_cost_tests.py): on
EE-2026 every column equals the reference to 1e-9, in one piece and in parts (the programs' share totals over the whole job), with program costs, with
none, with programs that have no positive benefits, with zero NTGs and with market effects."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import reference_cost_tests as ref
from recet import cost_test_sets
from recet.pipeline import cost_tests, test_evaluator

EE = cost_test_sets.load("EE-2026")
GROSS_MADE = {f"{c}Gross{s}" for c in ("ElecBen", "GasBen", "ElecSupplyCost", "GasSupplyCost", "WaterEnergyBen", "WaterEnergyCost")
              for s in ("", "_SB", "_SH")}
EXTRA = ["TaxCreditsGross", "RebatesandIncents", "DiscountedSavingsNetkWh", "DiscountedSavingsNetThm"]


def frame(n: int, seed: int, market_effects: float = 0.0, programs: int = 6) -> pd.DataFrame:
    """n synthetic measures with every column the EE-2026 tests read: values of both signs and exact zeros, some NTGs 0."""
    rng = np.random.default_rng(seed)
    cols = [c for c in EE.stream_columns() if c not in GROSS_MADE] + EXTRA
    d = pd.DataFrame({c: rng.normal(0, 1000, n) * (rng.random(n) > 0.15) for c in dict.fromkeys(cols)})
    for c in ("DiscountedSavingsNetkWh", "DiscountedSavingsNetThm"):
        d[c] = np.abs(d[c]) * (rng.random(n) > 0.2)
    d.insert(0, "CEInputID", [f"m{i}" for i in range(n)])
    d.insert(1, "PrgID", [f"P{i}" for i in rng.integers(0, programs, n)])
    d["NTGRkWh_plus_MEBens"] = np.where(rng.random(n) > 0.1, rng.uniform(0.3, 1.0, n), 0.0) + market_effects
    d["NTGRTherm_plus_MEBens"] = np.where(rng.random(n) > 0.1, rng.uniform(0.3, 1.0, n), 0.0) + market_effects
    # one program whose measures have no positive benefits at all (each gets 1 / its measure count)
    neg = d["PrgID"] == "P0"
    for c in [c for c in d if c.startswith(("ElecBen", "GasBen", "WaterEnergyBen", "OtherBen"))]:
        d.loc[neg, c] = -np.abs(d.loc[neg, c])
    return d


def program(seed: int, programs: int = 6) -> pd.DataFrame:
    rng = np.random.default_rng(seed + 1000)
    ids = [f"P{i}" for i in range(programs - 1)]                     # the last program has no cost row
    p = pd.DataFrame({"PrgID": ids, "SumCosts": rng.uniform(0, 1e5, len(ids)), "SumCostsNPV": rng.uniform(0, 1e5, len(ids)),
                      "SumCostsNPV_SCT": rng.uniform(0, 1e5, len(ids))})
    for f in ref._COST_FIELDS:
        p[f"Sum{f}"] = rng.uniform(0, 1e4, len(ids))
    return p


def reference(d: pd.DataFrame, prog: pd.DataFrame, totals=None) -> pd.DataFrame:
    out = ref.compute_cost_tests(d, prog, totals)
    return pd.concat([out, ref.pct(out)], axis=1)


def assert_equal(new: pd.DataFrame, old: pd.DataFrame):
    expected = set(old.columns) - set(test_evaluator.internal_columns(EE))
    assert set(new.columns) == expected
    for c in sorted(expected):
        a, b = new[c], old[c]
        if not (pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(b)):
            assert a.tolist() == b.tolist(), c
            continue
        np.testing.assert_allclose(a.to_numpy(float), b.to_numpy(float), rtol=1e-9, atol=1e-9, err_msg=c)


@pytest.mark.parametrize("seed, market_effects", [(1, 0.0), (2, 0.05), (3, 0.0)])
def test_ee_2026_equals_the_0_1_81_cost_tests(seed, market_effects):
    d, prog = frame(400, seed, market_effects), program(seed)
    assert_equal(cost_tests.compute_cost_tests(d, prog, cset=EE), reference(d, prog))


def test_with_no_program_costs():
    d = frame(200, 7)
    empty = cost_tests.program_cost_sums(None, 2026)
    assert_equal(cost_tests.compute_cost_tests(d, empty, cset=EE), reference(d, empty))
    out = cost_tests.compute_cost_tests(d, empty, cset=EE)
    for t in ("TRC", "PAC", "RIM", "SCB", "SCH"):
        np.testing.assert_allclose(out[f"{t}Cost"], out[f"{t}CostNoAdmin"], rtol=0, atol=0)


def test_in_parts_with_the_whole_jobs_totals():
    d, prog = frame(600, 11), program(11)
    whole = reference(d, prog)
    parts = [d.iloc[:250], d.iloc[250:]]
    totals = None
    for p in parts:
        totals = cost_tests.combine_share_totals(totals, cost_tests.share_totals(cost_tests.share_inputs(p, EE), p["PrgID"]))
    got = pd.concat([cost_tests.compute_cost_tests(p, prog, totals, EE) for p in parts])
    assert_equal(got, whole)


def test_share_keys_follow_the_set():
    assert set(test_evaluator.share_bases(EE)) == {"TRC", "TRCGross", "SCB", "SCBGross", "SCH", "SCHGross"}
    assert test_evaluator.headline_share(EE) == "TRC"
    assert test_evaluator.share_key(EE, EE.tests["RIM"]) == "TRC" and test_evaluator.share_key(EE, EE.tests["PCT"]) is None


def test_columns_follow_the_set_order():
    d, prog = frame(50, 5), program(5)
    out = cost_tests.compute_cost_tests(d, prog, cset=EE)
    written = [c for c in out.columns if c in set(EE.output_columns())]
    assert written == EE.output_columns()
    assert "TaxCreditsGrossPV" not in out                      # internal: priced for the PCT, never written


def test_a_missing_stream_is_named():
    d, prog = frame(20, 3).drop(columns=["TaxCreditsGrossPV"]), program(3)
    with pytest.raises(KeyError, match="TaxCreditsGrossPV"):
        cost_tests.compute_cost_tests(d, prog, cset=EE)


def test_societal_columns_are_the_sct_tests():
    cols = test_evaluator.societal_columns(EE, {"trc"})
    assert set(cols) == set(EE.tests["SCB"].outputs()) | set(EE.tests["SCH"].outputs())
    assert test_evaluator.societal_columns(EE, {"trc", "sct_base", "sct_high"}) == []


def test_calculations_c12_says_what_the_set_does():
    """CALCULATIONS C12's generated block is test_evaluator.formulas of the set in force (regenerate it when a new set is published)."""
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "recet" / "docs" / "CALCULATIONS.md").read_text(encoding="utf-8")
    m = re.search(r"<!-- cost-test-set (\S+): generated by test_evaluator.formulas -->\n```text\n(.*?)\n```\n<!-- end cost-test-set -->", text, re.S)
    assert m, "C12 has no generated cost-test block"
    assert m.group(1) == cost_test_sets.in_force(None).id
    assert m.group(2) == test_evaluator.formulas(cost_test_sets.load(m.group(1)))
