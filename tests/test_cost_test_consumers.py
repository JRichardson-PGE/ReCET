"""What a run writes besides the measure-level tests follows its cost-test set (COST_TESTS.md step 3): the portfolio summary's ratios, the key metrics, the
field definitions' cost-test attribute, the Results page's ratio columns and the run page's tiles; and a run keeps its set in its folder (cost_tests.yaml),
so all of them can be rebuilt from the run alone. EE-2026 runs are unchanged (the regression gate)."""
from __future__ import annotations

import json
import shutil

import numpy as np
import pandas as pd
import pytest

from recet import cost_test_sets as cts, key_metrics, outputs, results_view as rv
from recet.jobs import run_job
from test_chunked import _cfg as _job_cfg, _job
from test_cost_test_sets import _data, _write

QUIET = dict(log=lambda s: None)


def _jst_set(tmp_path):
    d = _data()
    d.update(id="EE-2026-jst", label="With a JST")
    d["tests"]["JST"] = {"label": "Jurisdiction-specific test", "pricing": "primary", "benefits": ["ElecBen", "GasBen"],
                         "costs": ["RebatesandIncentsPV"], "program_costs": {"share": "TRC"}, "variants": ["NoAdmin"]}
    return _write(tmp_path, d, "jst.yaml")


def _cfg(tmp_path, **kw):
    m, pc = _job(tmp_path)                                         # 40 measures over three programs with admin costs (tests/test_chunked.py)
    return _job_cfg(tmp_path, m, pc, **kw)


@pytest.fixture
def jst_run(tmp_path):
    return run_job(_cfg(tmp_path, cost_test_set=str(_jst_set(tmp_path)), output_format="csv", chunk_size=0), **QUIET)


def test_a_new_test_reaches_every_output(jst_run):
    r = jst_run
    m = outputs.read_table(r.files["measure_level"])
    np.testing.assert_allclose(m["JSTBen"], m["ElecBen"] + m["GasBen"], rtol=1e-12)
    np.testing.assert_allclose(m["JSTCostNoAdmin"], m["RebatesandIncentsPV"], rtol=1e-12)
    np.testing.assert_allclose(m["JSTCost"] - m["JSTCostNoAdmin"], m["PACCost"] - m["PACCostNoAdmin"], rtol=1e-12, atol=1e-9)   # TRC's share, as PAC
    summ = outputs.read_table(r.files["portfolio_summary"]).iloc[0]
    assert summ["JST_ratio"] == pytest.approx(m["JSTBen"].sum() / m["JSTCost"].sum(), rel=1e-12)
    assert summ["JSTBen"] == pytest.approx(m["JSTBen"].sum(), rel=1e-12) and summ["JSTCost"] == pytest.approx(m["JSTCost"].sum(), rel=1e-12)
    assert "JST_NoAdmin_ratio" not in summ.index                 # NoAdmin ratios in the summary: headline tests only (TRC, PAC)
    km = outputs.read_table(r.files["key_metrics"]).set_index("item")
    assert km.loc["JST ratio", "value"] == pytest.approx(summ["JST_ratio"], rel=1e-12)
    fd = outputs.read_table(r.files["field_definitions"]).set_index("column")
    assert fd.loc["ElecBen", "cost_tests"] == "benefit in TRC, PAC, RIM, JST"
    assert fd.loc["JSTRatio", "cost_tests"] == "JST result"


def test_the_run_keeps_its_set_and_the_results_page_reads_it(jst_run):
    r = jst_run
    kept = r.run_dir / cts.RUN_FILE
    assert kept.is_file() and json.loads((r.run_dir / "run_settings.json").read_text())["cost_tests"]["sha256"] == cts.read_file(kept).sha256
    s = cts.for_run(r.run_dir)
    assert "JST" in s.tests and s.run_label == "custom: jst.yaml"
    res = rv.RunResults.from_file(outputs.results_source(r.run_dir))
    cat = res.catalog()
    assert cat["JST_ratio"].label == "JST" and "JST_NoAdmin_ratio" in cat and cat["JSTCost"].label == "JST cost ($)"
    t = res.table("portfolio", rv.ALL_PAS, ["JST_ratio", "TRC_ratio"])
    summ = outputs.read_table(r.files["portfolio_summary"]).iloc[0]
    assert t.iloc[0]["JST_ratio"] == pytest.approx(summ["JST_ratio"], rel=1e-12) and t.iloc[0]["TRC_ratio"] == pytest.approx(summ["TRC_ratio"], rel=1e-12)


def test_a_new_test_in_parts_equals_the_whole_job(tmp_path):
    f = _jst_set(tmp_path)
    whole = run_job(_cfg(tmp_path, cost_test_set=str(f), run_name="whole", chunk_size=0, output_format="csv"), **QUIET)
    parts = run_job(_cfg(tmp_path, cost_test_set=str(f), run_name="parts", chunk_size=7, output_format="csv"), **QUIET)
    a, b = (outputs.read_table(x.files["portfolio_summary"]) for x in (whole, parts))
    for c in ("JST_ratio", "JSTBen", "JSTCost", "TRC_ratio"):
        np.testing.assert_allclose(b[c].astype(float), a[c].astype(float), rtol=1e-12, err_msg=c)
    ka, kb = (outputs.read_table(x.files["key_metrics"]).set_index("item") for x in (whole, parts))
    assert kb.loc["JST ratio", "value"] == pytest.approx(ka.loc["JST ratio", "value"], rel=1e-12)


def test_an_older_run_reads_as_the_ee_set(jst_run, tmp_path):
    old = tmp_path / "old_run"
    shutil.copytree(jst_run.run_dir, old)
    (old / cts.RUN_FILE).unlink()
    assert cts.for_run(old).id == "EE-2026"                       # names a set that is not built in, and kept no file: what every release before priced
    st = json.loads((old / "run_settings.json").read_text())
    st.pop("cost_tests")
    (old / "run_settings.json").write_text(json.dumps(st))
    assert cts.for_run(old) is cts.in_force(None)


def test_the_ee_set_gives_the_tables_ee_runs_always_had():
    s = cts.load("EE-2026")
    assert cts.summary_ratios(s) == ["TRC_ratio", "PAC_ratio", "RIM_ratio", "SCB_ratio", "SCH_ratio", "TRC_NoAdmin_ratio", "PAC_NoAdmin_ratio", "PCT_ratio"]
    assert set(cts.summary_ratios(s)) == set(outputs.SUMMARY_RATIOS)
    assert [c.key for c in rv.ratio_columns(s)] == ["TRC_ratio", "PAC_ratio", "RIM_ratio", "SCB_ratio", "SCH_ratio", "PCT_ratio", "TRC_NoAdmin_ratio",
                                                    "PAC_NoAdmin_ratio", "RIM_NoAdmin_ratio", "SCB_NoAdmin_ratio", "SCH_NoAdmin_ratio"]
    assert cts.tile_tests(s) == ["TRC", "PAC", "RIM", "SCB"]
    assert key_metrics.columns(s) == ["section", "item", "measures", "value", "TotalSystemBenefit", "share_of_TSB", "TRCCost", "share_of_TRC_cost",
                                      "TRC_ratio", "PAC_ratio", "definition", "documentation"]
    assert [h[0] for h in key_metrics.headlines(s)][2:9] == ["TRC ratio", "PAC ratio", "RIM ratio", "SCT Base ratio", "SCT High ratio", "PCT ratio",
                                                             "TRC cost ($)"]


def test_a_table_without_test_numerators_builds_them_from_the_set():
    """A measure-level table written before 0.1.53 has no <test>Ben: the ratios build their numerators from the set's benefits and adders."""
    g = pd.DataFrame({"ElecBen": [10.0, 5.0], "GasBen": [1.0, 0.0], "WaterEnergyBen": [0.0, 0.0], "OtherBen": [0.0, 1.0], "TaxCredits": [2.0, 0.0],
                      "TRCCost": [4.0, 4.0], "PACCost": [2.0, 2.0]})
    r = outputs.group_ratios(g, cts.load("EE-2026"))
    assert r["TRC_ratio"] == pytest.approx(19.0 / 8.0) and r["PAC_ratio"] == pytest.approx(17.0 / 4.0)
