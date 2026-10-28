"""Statewide allocation reconciles (BRD BR-06.7 and its acceptance scenario): statewide_reconciliation_<acc> adds each statewide program's allocated
fields back up, and the IOU shares' priced benefits add up to the unallocated program's wherever its inputs do."""
from __future__ import annotations

import pandas as pd
import pytest

from recet import outputs
from recet.jobs import run_job
from test_calcs import make_gas_acc
from test_chunked import CHUNK, _cfg as _chunk_cfg, _job

QUIET = dict(log=lambda s: None)
FACTORS = {"SW_A": ({"PGE": 0.25, "SCE": 0.75}, {"PGE": 0.4, "SCG": 0.6}),      # (electric, gas) factors by IOU
           "SW_B": ({"PGE": 0.6, "SDGE": 0.4}, {})}                             # all-electric: every gas factor 0


def _cfg(tmp_path, m, pc, **kw):
    gas = tmp_path / "gas.csv"
    if not gas.exists():
        make_gas_acc(gas)
    return _chunk_cfg(tmp_path, m, pc, acc_gas_file=str(gas), **kw)


def _sw_job(tmp_path):
    m, pc = _job(tmp_path)
    meas = pd.read_csv(m, sep="|", dtype=str, keep_default_na=False)
    meas["PrgID"] = ["SW_A" if (i // 2) % 2 else "SW_B" for i in range(len(meas))]
    meas["UnitTherm1stBaseline"] = [str(3 + i % 5) for i in range(len(meas))]
    meas["AC_Gas_Impact_Profile"] = "Winter Only"
    meas.to_csv(m, sep="|", index=False)
    cost = pd.read_csv(pc, sep="|", dtype=str, keep_default_na=False)
    cost = cost[cost["PrgID"].isin(["PRG0", "PRG1"])].copy()
    cost["PrgID"] = cost["PrgID"].map({"PRG0": "SW_B", "PRG1": "SW_A"})
    cost.to_csv(pc, sep="|", index=False)
    rows = []
    for year in (2028, 2029):
        for prg, (elec, gas) in FACTORS.items():
            r = {"Year": year, "ProgramID": prg, "PercentElectric": 1.0 if not gas else 0.5}
            for iou in ("PGE", "SDGE", "SCE", "SCG"):
                r[f"{iou}ElectricFactor"], r[f"{iou}GasFactor"] = elec.get(iou, 0.0), gas.get(iou, 0.0)
            rows.append(r)
    sw = tmp_path / "sw.csv"
    pd.DataFrame(rows).to_csv(sw, index=False)
    return m, pc, sw


def test_allocation_reconciles_to_the_unallocated_totals(tmp_path):
    m, pc, sw = _sw_job(tmp_path)
    plain = run_job(_cfg(tmp_path, m, pc, run_name="plain", chunk_size=0), **QUIET)
    split = run_job(_cfg(tmp_path, m, pc, run_name="split", chunk_size=0, allocate_statewide=True, sw_factors_file=str(sw)), **QUIET)
    assert "statewide_reconciliation" not in plain.files
    t = outputs.read_table(split.files["statewide_reconciliation"])
    assert set(t["PrgID"]) == {"SW_A", "SW_B"} and set(t["Year"]) == {2028, 2029} and set(t["table"]) == {"Measure", "ProgramCost"}

    # every allocated field adds back up, except SW_B's therms: its gas factors are all 0, so they are in no IOU's share
    bad = t[~t["reconciles"].astype(bool)]
    assert set(zip(bad["PrgID"], bad["field"])) == {("SW_B", "UnitTherm1stBaseline")}
    assert (bad["allocated_total"] == 0).all() and bad["note"].str.contains("is 0").all()
    meas = pd.read_csv(m, sep="|")
    kwh = t[(t["field"] == "UnitkWh1stBaseline")].groupby("PrgID")["unallocated_total"].sum()
    expect = (meas["NumUnits"] * meas["UnitkWh1stBaseline"]).groupby(meas["PrgID"]).sum()
    assert kwh.to_dict() == pytest.approx(expect.to_dict(), rel=1e-12)
    sa = t[(t["PrgID"] == "SW_A") & (t["field"] == "UnitkWh1stBaseline")]
    assert (sa["PGE"] / sa["unallocated_total"]).tolist() == pytest.approx([0.25, 0.25]) and (sa["SDGE"] == 0).all()
    assert any("SW_B" in n and "UnitTherm1stBaseline" in n for n in split.notes)

    # and the priced benefits follow the inputs: equal where the inputs reconcile, short by exactly the dropped therms where they do not
    a, b = plain.frame, split.frame
    prg = b["PrgID"].str.partition("_")[2]
    assert b["ElecBen"].sum() == pytest.approx(a["ElecBen"].sum(), rel=1e-12)
    assert b.loc[prg == "SW_A", "GasBen"].sum() == pytest.approx(a.loc[a["PrgID"] == "SW_A", "GasBen"].sum(), rel=1e-12)
    assert a.loc[a["PrgID"] == "SW_B", "GasBen"].sum() > 0 and b.loc[prg == "SW_B", "GasBen"].sum() == 0
    assert b["TotalSystemBenefit"].sum() == pytest.approx(a["TotalSystemBenefit"].sum() - a.loc[a["PrgID"] == "SW_B", "GasBen"].sum(), rel=1e-9)


def test_a_run_in_parts_reconciles_the_same(tmp_path):
    m, pc, sw = _sw_job(tmp_path)
    whole = run_job(_cfg(tmp_path, m, pc, run_name="whole", chunk_size=0, allocate_statewide=True, sw_factors_file=str(sw)), **QUIET)
    parts = run_job(_cfg(tmp_path, m, pc, run_name="parts", chunk_size=CHUNK, allocate_statewide=True, sw_factors_file=str(sw)), **QUIET)
    a, b = (outputs.read_table(r.files["statewide_reconciliation"]) for r in (whole, parts))
    pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-12)
