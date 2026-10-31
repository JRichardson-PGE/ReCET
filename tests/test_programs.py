"""Program metadata (recet.programs): the built-in CEDARS Programs list, the statewide matching rule, supplied tables, the summary rows and the preflight."""
from __future__ import annotations

import pandas as pd
import pytest

import test_calcs as tc
from recet import outputs, preflight, programs
from recet.jobs import RunConfig, run_job


def test_builtin_table_has_every_program_once_and_no_staff_names():
    t = programs.builtin()
    assert len(t) == 1416 and t["PrgID"].is_unique
    assert "ProgramManager" not in t.columns
    assert list(t.columns) == programs.COLUMNS


def test_allocated_statewide_share_gets_its_own_iou_row():
    m = programs.lookup(["SCE_SW_HVAC_Up", "PGE_SW_HVAC_Up"])
    assert list(m["Prg_MatchedOn"]) == ["SCE_SW_HVAC_Up", "PGE_SW_HVAC_Up"]
    assert list(m["Prg_PA"]) == ["SCE", "PGE"]
    t = programs.builtin().set_index("PrgID")
    assert m.loc[0, "Prg_ProgramName"] == t.loc["SCE_SW_HVAC_Up", "ProgramName"]


def test_unallocated_statewide_row_is_filled_only_where_the_iou_copies_agree():
    t = programs.builtin().set_index("PrgID")
    copies = t.loc[[f"{i}_SW_CSA_Bldg" for i in programs.IOUS if f"{i}_SW_CSA_Bldg" in t.index]]
    assert t.loc["SW_CSA_Bldg", "ProgramSegment"] == "" and copies["ProgramSegment"].nunique() == 1
    assert copies["ProgramName"].nunique() > 1
    m = programs.lookup(["SW_CSA_Bldg"]).iloc[0]
    assert m["Prg_MatchedOn"] == "SW_CSA_Bldg + agreeing IOU copies"
    assert m["Prg_PA"] == "SW"
    assert m["Prg_ProgramSegment"] == copies["ProgramSegment"].iloc[0]
    assert m["Prg_ProgramName"] == t.loc["SW_CSA_Bldg", "ProgramName"]             # the copies differ: the SW row's own value (blank) stays


def test_unknown_program_is_blank():
    m = programs.lookup(["NOT_A_PROGRAM"]).iloc[0]
    assert m["Prg_MatchedOn"] == "(not found)" and m["Prg_ProgramSegment"] == "" and m["Prg_PA"] == ""


def test_supplied_rows_replace_and_extend(tmp_path):
    f = tmp_path / "p.csv"
    pd.DataFrame([{"PA": "PGE", "PrgID": "PGE_Pub_009", "ProgramSegment": "equity", "MyTag": "x"},
                  {"PA": "PGE", "PrgID": "NEW1", "ProgramSegment": "market_support", "MyTag": "y"}]).to_csv(f, index=False)
    table, notes = programs.load_programs(f)
    assert any("MyTag" in n for n in notes)
    programs.set_user_programs(table)
    m = programs.lookup(["PGE_Pub_009", "NEW1", "PGE_Ag_001"])
    assert list(m["Prg_ProgramSegment"])[:2] == ["equity", "market_support"]
    assert m.loc[0, "Prg_ProgramName"] == ""                                        # a supplied row replaces the built-in row as a whole
    assert list(m["Prg_MyTag"]) == ["x", "y", ""]
    assert m.loc[2, "Prg_MatchedOn"] == "PGE_Ag_001"                               # the rest of the built-in table is still there


def test_bad_tables_are_refused(tmp_path):
    f = tmp_path / "p.csv"
    pd.DataFrame([{"PA": "PGE", "Name": "x"}]).to_csv(f, index=False)
    with pytest.raises(programs.ProgramsError, match="PrgID"):
        programs.load_programs(f)
    pd.DataFrame([{"PA": "PGE", "PrgID": "A"}, {"PA": "SCE", "PrgID": "A"}]).to_csv(f, index=False)
    with pytest.raises(programs.ProgramsError, match="repeated"):
        programs.load_programs(f)


def test_summary_segment_rows_and_exclude_from_ce():
    frame = pd.DataFrame({"IOU_AC_Territory": ["PGE", "PGE", "SCE"], "CreditedPA": ["PGE", "PGE", "SCE"], "Prg_ProgramSegment": ["equity", "", "equity"],
                          "Prg_Exclude_From_CE": ["False", "TRUE", "false"], "ElecBen": [1.0, 10.0, 100.0], "TRCCost": [1.0, 1.0, 1.0]})
    s = outputs.summarize(frame)
    seg = s[s["group"] == "program segment"].set_index("value")["ElecBen"]
    assert seg.to_dict() == {"(blank)": 10.0, "equity": 101.0}
    assert s.iloc[0]["ElecBen"] == 111.0 and "excluded" not in set(s["group"])
    s = outputs.summarize(frame, apply_exclude_from_ce=True)
    assert s.iloc[0]["ElecBen"] == 101.0 and s.iloc[0]["measures"] == 2
    ex = s[s["group"] == "excluded"].iloc[0]
    assert ex["ElecBen"] == 10.0 and ex["measures"] == 1
    assert "(blank)" not in set(s.loc[s["group"] == "program segment", "value"])


def test_run_attaches_metadata_and_preflight_reports_programs(tmp_path):
    tc.write_measures(tmp_path / "Measure.csv", {"EX1": tc.MEASURES["EX1"]})
    tc.write_program(tmp_path / "ProgramCost.csv")
    tc.make_acc(tmp_path / "acc.parquet")
    tc.make_gas_acc(tmp_path / "gas.csv")
    tc.make_profiles(tmp_path / "profiles.csv")
    prg = tc.MEASURES["EX1"]["PrgID"]
    pd.DataFrame([{"PA": "SCE", "PrgID": prg, "ProgramStatus": "closed", "ProgramSegment": "equity", "StartYear": "2030", "Exclude_From_CE": "True"}]).to_csv(
        tmp_path / "programs.csv", index=False)
    cfg = RunConfig(measures=str(tmp_path / "Measure.csv"), programcost=str(tmp_path / "ProgramCost.csv"), out_dir=str(tmp_path / "out"), run_name="p", acc_version="2026",
                    acc_electric_file=str(tmp_path / "acc.parquet"), acc_gas_file=str(tmp_path / "gas.csv"), profiles_8760_file=str(tmp_path / "profiles.csv"),
                    programs_file=str(tmp_path / "programs.csv"), apply_exclude_from_ce=True)
    res = run_job(cfg, log=lambda s: None)
    row = res.frame.iloc[0]
    assert row["Prg_MatchedOn"] == prg and row["Prg_ProgramSegment"] == "equity" and row["Prg_PA"] == "SCE"
    assert list(res.frame.columns).index("Prg_MatchedOn") == list(res.frame.columns).index("CreditedPA") + 1
    text = res.files["preflight"].read_text(encoding="utf-8")
    for code in ("claim_outside_program_years", "program_closed", "program_excluded_from_ce", "program_pa_differs"):
        assert code in text, code
    summ = pd.read_csv(res.files["portfolio_summary"])
    assert summ.iloc[0]["measures"] == 0 and summ.iloc[-1]["group"] == "excluded" and summ.iloc[-1]["measures"] == 1


def test_a_whole_supplied_table_replaces_the_builtin_one(tmp_path):
    f = tmp_path / "p.csv"
    pd.DataFrame([{"PA": "PGE", "PrgID": "NEW1", "ProgramSegment": "equity", "MyTag": "x"}]).to_csv(f, index=False)
    table, _ = programs.load_programs(f)
    try:
        programs.set_user_programs(table, replace=True)
        m = programs.lookup(["NEW1", "PGE_Ag_001"])
        assert list(m["Prg_MatchedOn"]) == ["NEW1", "(not found)"]                # no built-in row is used
        assert list(m["Prg_ProgramSegment"]) == ["equity", ""] and list(m["Prg_MyTag"]) == ["x", ""]
        assert {f"Prg_{c}" for c in programs.COLUMNS if c != "PrgID"} <= set(m.columns)   # the same Prg_ columns as with the built-in table
        with pytest.raises(programs.ProgramsError, match="needs a supplied table"):
            programs.set_user_programs(None, replace=True)
        f.write_text("PA,PrgID\n")
        with pytest.raises(programs.ProgramsError, match="no rows"):
            programs.load_programs(f)
        empty, _ = programs.load_programs(f, allow_empty=True)                    # a registry that holds no programs yet
        programs.set_user_programs(empty, replace=True)
        assert programs.lookup(["PGE_Ag_001"]).iloc[0]["Prg_MatchedOn"] == "(not found)"
    finally:
        programs.set_user_programs(None)


def test_run_with_programs_replace_uses_only_the_supplied_table(tmp_path):
    tc.write_measures(tmp_path / "Measure.csv", {"EX1": tc.MEASURES["EX1"]})
    tc.write_program(tmp_path / "ProgramCost.csv")
    tc.make_acc(tmp_path / "acc.parquet")
    tc.make_gas_acc(tmp_path / "gas.csv")
    tc.make_profiles(tmp_path / "profiles.csv")
    prg = tc.MEASURES["EX1"]["PrgID"]
    pd.DataFrame([{"PA": "PGE", "PrgID": prg, "ProgramSegment": "equity"}]).to_csv(tmp_path / "programs.csv", index=False)
    base = dict(measures=str(tmp_path / "Measure.csv"), programcost=str(tmp_path / "ProgramCost.csv"), out_dir=str(tmp_path / "out"), acc_version="2026",
                acc_electric_file=str(tmp_path / "acc.parquet"), acc_gas_file=str(tmp_path / "gas.csv"), profiles_8760_file=str(tmp_path / "profiles.csv"))
    with pytest.raises(ValueError, match="programs_replace needs a supplied program table"):
        run_job(RunConfig(**base, run_name="r0", programs_replace=True), log=lambda s: None)
    lines = []
    try:
        res = run_job(RunConfig(**base, run_name="r1", programs_file=str(tmp_path / "programs.csv"), programs_replace=True), log=lines.append)
        assert res.frame.iloc[0]["Prg_ProgramSegment"] == "equity" and "Prg_Sector" in res.frame.columns
        assert programs.lookup(["PGE_Ag_001"]).iloc[0]["Prg_MatchedOn"] == "(not found)"     # a built-in program is unknown to the run
        assert any("the built-in CEDARS Programs list is not used" in s for s in lines)
        pd.DataFrame(columns=["PA", "PrgID"]).to_csv(tmp_path / "programs.csv", index=False)  # an empty registry
        res = run_job(RunConfig(**base, run_name="r2", programs_file=str(tmp_path / "programs.csv"), programs_replace=True), log=lambda s: None)
        assert res.frame.iloc[0]["Prg_MatchedOn"] == "(not found)"
        assert "program_not_in_metadata" in res.files["preflight"].read_text(encoding="utf-8")
    finally:
        programs.set_user_programs(None)
