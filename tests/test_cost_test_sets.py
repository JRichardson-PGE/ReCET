"""Cost-test definition sets (cost_test_sets.py, recet/docs/COST_TESTS.md): loading and checking a set, the set in force for a program year, and the
run's pinned set (Used_CostTestSet, run_settings.json, the worker's receipt, filing runs, drafts, the program-year warning)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from recet import cost_test_sets as cts
from recet.jobs import RunConfig, run_job
from conftest import make_electric_acc, make_flat_profile_csv, make_measure_csv

EE = cts.SETS_DIR / "EE-2026" / cts.DEFINITION


def _data() -> dict:
    return yaml.safe_load(EE.read_text(encoding="utf-8"))


def _write(tmp_path: Path, data: dict, name: str = "custom.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


def _cfg(tmp_path, **kw):
    base = dict(measures=str(make_measure_csv(tmp_path / "Measure.csv")), out_dir=str(tmp_path / "out"), run_name="t", acc_version="2026",
                acc_electric_file=str(make_electric_acc(tmp_path / "acc.parquet")), profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")),
                emissions=False)
    base.update(kw)
    return RunConfig(**base)


# ---------------------------------------------------------------------------------------------------------------------------- the shipped sets
def test_every_shipped_set_loads_and_is_named_by_its_folder():
    sets = cts.builtin()
    assert "EE-2026" in sets
    for sid, s in sets.items():
        assert s.id == sid and s.builtin and len(s.sha256) == 64
        assert (cts.SETS_DIR / sid / "CHANGES.md").exists(), f"{sid} has no CHANGES.md"
        assert cts.validate(yaml.safe_load((cts.SETS_DIR / sid / cts.DEFINITION).read_text(encoding="utf-8"))) == []


def test_ee_2026_is_todays_tests():
    s = cts.load("EE-2026")
    assert list(s.tests) == ["TRC", "PAC", "RIM", "SCB", "SCH", "PCT"] and s.headline_tests() == ["TRC", "PAC"]
    assert s.workstream == "EE" and not s.draft and cts.years(s) == (None, None)
    # the data dictionary's cost-test attribute is read from the set (field_attributes.benefit_of / cost_of)
    from recet import field_attributes
    assert field_attributes.benefit_of(s)["ElecBen_SB"] == "SCB" and field_attributes.benefit_of(s)["OtherBen"] == "TRC, PAC, RIM, SCB, SCH"
    assert field_attributes.cost_of(s)["RebatesandIncentsPV_SCT"] == "SCB, SCH" and field_attributes.cost_of(s)["LifecycleBillReducElec"] == "RIM"
    assert s.column("ElecBen", "sct_base") == "ElecBen_SB" and s.column("ElecBen", "sct_high", gross=True) == "ElecBenGross_SH"
    assert s.column("RebatesandIncentsPV", "sct_base") == "RebatesandIncentsPV_SCT" and s.column("OtherBen", "sct_high") == "OtherBen"
    assert s.column("NetParticipantCostAdjustedPV", "sct_base", gross=True) == "GrossParticipantCostPV_SCT"
    assert s.tests["SCB"].outputs() == ["SCBBen", "SCBCost", "SCBCostNoAdmin", "SCBCostGross", "SCBRatio", "SCBRatioNoAdmin"]
    assert s.tests["RIM"].outputs()[-3:] == ["LevRIMCost", "LevNetBenRIMElec", "LevNetBenRIMGas"]
    assert s.tests["PCT"].outputs() == ["PCTBen", "PCTCost", "PCTRatio"]
    assert "TaxCreditsGrossPV" in s.stream_columns()


def test_every_column_the_set_names_is_one_a_run_writes(tmp_path):
    """The set and the engine agree: each output column of EE-2026 and each stream column it reads (in its pricing, gross forms too) is a column of the
    measure-level table, except the internal ones; so the evaluator of the next step has every input it names."""
    frame = run_job(_cfg(tmp_path), log=lambda s: None).frame
    s = cts.load("EE-2026")
    internal = {s.column(n, p) for n, st in s.streams.items() if st.internal for p in st.pricings}
    assert not [c for c in s.output_columns() if c not in frame], [c for c in s.output_columns() if c not in frame]
    from recet.pipeline import cost_tests
    gross_kept = set(cost_tests._with_gross(frame.assign(NTGRkWh_plus_MEBens=1.0, NTGRTherm_plus_MEBens=1.0)).columns)
    missing = [c for c in s.stream_columns() if c not in internal and c not in frame and c not in gross_kept]
    assert not missing, missing


def test_the_schema_matches_the_validator():
    schema = json.loads(cts.SCHEMA_FILE.read_text(encoding="utf-8"))
    assert set(schema["properties"]) == cts.TOP
    assert schema["properties"]["schema_version"]["const"] == cts.SCHEMA_VERSION
    assert set(schema["properties"]["streams"]["additionalProperties"]["properties"]) == cts.STREAM_KEYS
    assert set(schema["properties"]["tests"]["additionalProperties"]["properties"]) == cts.TEST_KEYS
    assert schema["properties"]["id"]["pattern"] == cts.ID.pattern
    st = schema["properties"]["streams"]["additionalProperties"]["properties"]
    assert tuple(st["source"]["enum"]) == cts.SOURCES and tuple(st["basis"]["enum"]) == cts.BASES and tuple(st["fuel"]["enum"]) == cts.FUELS
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(schema)
    for sid in cts.builtin():
        jsonschema.validate(yaml.safe_load((cts.SETS_DIR / sid / cts.DEFINITION).read_text(encoding="utf-8")), schema)


# ---------------------------------------------------------------------------------------------------------------------------- checking a set
@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(id="ee-2026"), "id:"),
    (lambda d: d.update(id="IQP-2027"), "must start with its workstream"),
    (lambda d: d["tests"]["SCB"]["benefits"].append("TaxCredits"), "TaxCredits is not priced in 'sct_base'"),
    (lambda d: d["tests"]["TRC"]["costs"].append("NoSuchStream"), "no stream 'NoSuchStream'"),
    (lambda d: d["tests"]["PCT"].update(variants=["Gross"]), "has no gross form"),
    (lambda d: d["tests"]["PCT"].update(variants=["NoAdmin"]), "NoAdmin leaves out the program costs"),
    (lambda d: d["tests"]["PAC"].update(program_costs={"share": "SCB"}), "SCB is priced in 'sct_base'"),
    (lambda d: d["tests"]["PAC"].update(program_costs={"share": "RIM"}), "RIM does not compute its own share"),
    (lambda d: d["tests"]["RIM"].update(levelized=["NoAdmin", "Gross"]), "levelized"),
    (lambda d: d["streams"]["OtherBen"].update(basis="monthly"), "streams.OtherBen.basis"),
    (lambda d: d["streams"]["ElecBen"].update(columns=["UnitkWh1stBaseline"]), "for source: input"),
    (lambda d: d["pricings"]["primary"].update(discount_rate_annual="DiscountRate"), "discount_rate_annual"),
    (lambda d: d.update(program_years={"from": 2030, "to": 2028}), "to is before from"),
    (lambda d: d.update(colour="blue"), "unknown top-level key 'colour'"),
])
def test_a_bad_set_is_refused_with_its_problem(tmp_path, change, message):
    d = _data()
    change(d)
    with pytest.raises(cts.SetError) as e:
        cts.read_file(_write(tmp_path, d))
    assert message in str(e.value)


def test_a_custom_set_with_its_own_test(tmp_path):
    d = _data()
    d.update(id="EE-2026-jst", label="With a JST")
    d["tests"]["JST"] = {"label": "Jurisdiction-specific test", "pricing": "primary", "benefits": ["ElecBen", "GasBen"],
                         "costs": ["RebatesandIncentsPV"], "program_costs": {"share": "TRC"}, "variants": ["NoAdmin", "Gross"]}
    s = cts.read_file(_write(tmp_path, d, "jst.yaml"))
    assert not s.builtin and s.run_label == "custom: jst.yaml"
    assert s.tests["JST"].outputs() == ["JSTBen", "JSTCost", "JSTCostNoAdmin", "JSTCostGross", "JSTRatio", "JSTRatioNoAdmin"]
    assert "JSTBen = ElecBen + GasBen" in cts.describe(s).replace("  ", " ")


# ---------------------------------------------------------------------------------------------------------------------------- program years
def _folder(tmp_path, *sets: dict) -> Path:
    root = tmp_path / "sets"
    for d in sets:
        (root / d["id"]).mkdir(parents=True)
        (root / d["id"] / cts.DEFINITION).write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    return root


def test_the_set_in_force_follows_the_program_year(tmp_path):
    a = _data()
    b = copy.deepcopy(a)
    b.update(id="EE-2028", label="2028 tests", program_years={"from": 2028, "to": None})
    c = copy.deepcopy(a)
    c.update(id="EE-2030-draft", label="draft", draft=True, program_years={"from": 2030, "to": None})
    folder = _folder(tmp_path, a, b, c)
    assert cts.in_force(2027, folder=folder).id == "EE-2026" and cts.in_force(2028, folder=folder).id == "EE-2028"
    assert cts.in_force(2031, folder=folder).id == "EE-2028"                      # a draft is never the default
    assert cts.in_force(None, folder=folder).id == "EE-2028"
    sets = cts.builtin(folder)
    assert cts.years(sets["EE-2026"], folder) == (None, 2027) and cts.years_text(sets["EE-2026"], folder) == "program years up to 2027"
    assert not cts.covers(sets["EE-2026"], 2029, folder) and cts.covers(sets["EE-2028"], 2029, folder)
    assert [i for i, _t in cts.catalog(folder)] == ["EE-2028", "EE-2026", "EE-2030-draft"]


def test_a_set_outside_the_jobs_years_is_a_warning(tmp_path, monkeypatch):
    a = _data()
    b = copy.deepcopy(a)
    b.update(id="EE-2030", label="2030 tests", program_years={"from": 2030, "to": None})
    folder = _folder(tmp_path, a, b)
    monkeypatch.setattr(cts, "SETS_DIR", folder)
    r = cts.resolve("EE-2030", 2028, program_years=[2028, 2029, 2030])
    assert r.chosen and any("cost_tests_not_for_program_year" in w and "2028, 2029" in w for w in r.warnings)
    r = cts.resolve("", 2029)
    assert r.set.id == "EE-2026" and not r.chosen and not r.warnings and r.record()["chosen"] == "in force for program year 2029"


# ---------------------------------------------------------------------------------------------------------------------------- a run pins its set
def test_a_run_records_its_set(tmp_path):
    r = run_job(_cfg(tmp_path), log=lambda s: None)
    assert r.frame.columns[:2].tolist() == ["Used_ACCVersion", "Used_CostTestSet"] and (r.frame["Used_CostTestSet"] == "EE-2026").all()
    rec = json.loads((r.run_dir / "run_settings.json").read_text())["cost_tests"]
    assert rec["id"] == "EE-2026" and rec["source"] == "built-in" and rec["sha256"] == cts.load("EE-2026").sha256
    assert rec["chosen"] == "in force for program year 2028" and rec["tests"] == ["TRC", "PAC", "RIM", "SCB", "SCH", "PCT"]


def test_a_custom_set_file_is_for_analysis_runs_only(tmp_path):
    d = _data()
    d.update(id="EE-2026-custom", label="Custom")
    f = _write(tmp_path, d)
    r = run_job(_cfg(tmp_path, cost_test_set=str(f)), log=lambda s: None)
    assert (r.frame["Used_CostTestSet"] == "custom: custom.yaml").all()
    rec = json.loads((r.run_dir / "run_settings.json").read_text())["cost_tests"]
    assert rec["source"] == str(f) and rec["chosen"] == "run option"
    for kw in ({"filing_validation": "claims"}, {"acc_purpose": "claims"}):
        with pytest.raises(cts.SetError, match="filing run"):
            run_job(_cfg(tmp_path, cost_test_set=str(f), run_name="f", **kw), log=lambda s: None)


def test_a_draft_set_is_labelled_and_refused_for_a_filing(tmp_path, monkeypatch):
    a = _data()
    b = copy.deepcopy(a)
    b.update(id="EE-2028-draft", label="Draft", draft=True, program_years={"from": 2028, "to": None})
    monkeypatch.setattr(cts, "SETS_DIR", _folder(tmp_path, a, b))
    r = run_job(_cfg(tmp_path, cost_test_set="EE-2028-draft"), log=lambda s: None)
    assert (r.frame["Used_CostTestSet"] == "EE-2028-draft (draft)").all() and any("DRAFT set" in n for n in r.notes)
    with pytest.raises(cts.SetError, match="DRAFT"):
        run_job(_cfg(tmp_path, cost_test_set="EE-2028-draft", acc_purpose="claims", run_name="f"), log=lambda s: None)


def test_an_unknown_set_stops_before_pricing(tmp_path):
    with pytest.raises(cts.SetError, match="no built-in cost-test set 'EE-1999'"):
        run_job(_cfg(tmp_path, cost_test_set="EE-1999"), log=lambda s: None)


def test_a_replica_refuses_a_set():
    from recet.cet_versions import job as cet_job
    assert "cost_test_set" in dict(cet_job.REFUSED)


def test_the_cli(tmp_path, capsys):
    from recet import cli
    assert cli._cmd_cost_tests(type("A", (), {"action": "list", "set": None})()) == 0
    assert "EE-2026" in capsys.readouterr().out
    assert cli._cmd_cost_tests(type("A", (), {"action": "show", "set": "EE-2026"})()) == 0
    assert "TRCBen  = ElecBen + GasBen + WaterEnergyBen + OtherBen + TaxCredits" in capsys.readouterr().out
    d = _data()
    d["tests"]["TRC"]["costs"].append("Nope")
    assert cli._cmd_cost_tests(type("A", (), {"action": "check", "set": str(_write(tmp_path, d))})()) == 2
    assert "no stream 'Nope'" in capsys.readouterr().err
    a = cli.build_parser().parse_args(["run", "--measures", "m.csv", "--cost-tests", "EE-2026"])
    assert cli._config_from_args(a).cost_test_set == "EE-2026"


# ---------------------------------------------------------------------------------------------------------------------------- the job spec
def test_the_job_spec_names_a_set_once():
    from recet import jobspec
    ok = {"spec_version": 1, "job_id": "j", "inputs": {"measures": "m.csv"}, "outputs": {"uri": "out"}}
    assert jobspec.validate({**ok, "settings": {"cost_test_set": "EE-2026"}}) == []
    assert jobspec.validate({**ok, "inputs": {"measures": "m.csv", "cost_tests": "set.yaml"}}) == []
    errors = jobspec.validate({**ok, "inputs": {"measures": "m.csv", "cost_tests": "set.yaml"}, "settings": {"cost_test_set": "EE-2026"}})
    assert any("name the cost-test set once" in e for e in errors)
    assert "cost_test_set" in jobspec.settings_fields()
