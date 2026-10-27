"""Output columns renamed or dropped in 0.1.50 (fieldnames.OUTPUT_RENAMES / OUTPUT_REMOVED): old names still work where a user names columns, and a CET
outputs file, which keeps CET's names, still lines up with ReCET's."""
from __future__ import annotations

import pandas as pd
import pytest

from recet import fieldnames, output_templates, outputs, results_view


def test_old_names_map_to_new_ones_and_dropped_ones_go():
    assert fieldnames.output_name("NetElecCO2") == "FirstYearNetElecCO2"
    assert fieldnames.output_name("GrossPM10Lifecycle") == "LifecycleGrossElecPM10"
    assert fieldnames.output_name("poc_water_scale_2nd") == "AC_kWh_Water_2nd_Baseline"
    assert fieldnames.output_name("CreditedIOU") == "CreditedPA" and fieldnames.output_name("POC_ACC") == "Used_ACCVersion"
    assert fieldnames.output_name("poc_PA") == "" and fieldnames.output_name("ACCCheck") == "" and fieldnames.output_name("WeightedSavings") == ""
    assert fieldnames.output_name("ElecBen") == "ElecBen"
    assert len(set(fieldnames.OUTPUT_RENAMES.values())) == len(fieldnames.OUTPUT_RENAMES)        # no two old names share a new one
    assert not set(fieldnames.OUTPUT_RENAMES) & fieldnames.OUTPUT_REMOVED


def test_a_template_or_view_saved_with_old_names_still_works():
    assert output_templates.current(["CEInputID", "NetElecCO2", "poc_PA", "FirstYearNetElecCO2", "BillReducGas"]) == \
        ["CEInputID", "FirstYearNetElecCO2", "LifecycleBillReducGas"]
    ok, bad = output_templates.check(["CEInputID", "NetGasNOxLifecycle", "ACCCheck"])
    assert ok == ["CEInputID", "LifecycleNetGasNOx"] and bad == ["ACCCheck"]
    assert results_view.current_keys(["TRC_ratio", "BillReducElec", "ProgramCosts"]) == ["TRC_ratio", "LifecycleBillReducElec"]


def test_the_cet_comparison_reads_cets_own_names(tmp_path):
    cet = tmp_path / "cet_outputs.csv"
    pd.DataFrame({"CET_ID": ["M1", "M2"], "ElecBen": [10.0, 20.0], "NetElecCO2": [1.0, 2.0], "BillReducElec": [5.0, 6.0]}).to_csv(cet, sep="|", index=False)
    frame = pd.DataFrame({"CEInputID": ["M1", "M2"], "PrgID": ["P", "P"], "MeasDescription": ["", ""], "ElecBen": [10.0, 22.0],
                          "FirstYearNetElecCO2": [1.0, 2.0], "LifecycleBillReducElec": [5.0, 6.0]})
    per, summary = outputs.compare_to_cet(frame, cet)
    s = summary.set_index("metric")
    assert s.loc["FirstYearNetElecCO2", "CET"] == pytest.approx(3.0) and s.loc["LifecycleBillReducElec", "POC/CET"] == pytest.approx(1.0)
    assert s.loc["ElecBen", "POC/CET"] == pytest.approx(32 / 30)
