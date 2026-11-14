"""The wider rule language (recet/filing_rules.py, 0.1.95) and the check job (recet/checkjob.py, `recet worker check`): conditions on another table of
the filing, the program record and the filer; groups; the text, flag and date tests; rules that cannot be checked are noted; the check job gives the
same findings as running the rules here."""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from recet import __version__, checkjob, editor, worker
from recet import filing_rules as fr

CLAIM = pd.DataFrame({
    "ClaimID": ["PGE-2027-1", "PGE-2027-2", "PGE-2027-3", "PGE-2027-4"],
    "ParticipantID": ["", "", "P9", ""],
    "CCAREN_Flag": ["1", "0", "", "true"],
    "InstallationDate": ["2020-01-05", "2010-03-01", "4/1/2027", ""],
    "BldgType": ["Com", "Com", "", "Res"],
    "Note": ["has NMEC in it", "", "nmec", "x"],
})
MEASURE = pd.DataFrame({"ClaimID": ["pge-2027-1", "PGE-2027-2", "PGE-2027-2", "PGE-2027-3"], "MeasImpactType": ["Cust-SEM", "Deemed", "Cust-SEM", "Cust-NMEC"],
                        "PrgID": ["P1", "P2", "P2", "P1"]})
PROGRAMS = pd.DataFrame({"PrgID": ["P1", "P2"], "Category": ["Energy Savings Assistance", "Core"]})
KEYS = {"Claim": ["ClaimID"], "Measure": ["ClaimID"]}


def _rule(rid, field, rule, value="", conds=(), connector="all", table="Claim", severity="reject", **extra):
    spec = {"field": field, "rule": rule, "value": value, "connector": connector, "conds": [c if isinstance(c, dict) else list(c) for c in conds], **extra}
    return fr.Rule(rid, severity, table, field, fr.describe(spec, table), "rule", "test", spec)


def _ctx(**kw):
    base = dict(filing_year=2027, upload_date=date(2027, 4, 30), filer={"PA": "MCE", "PAType": "CCA"}, programs=PROGRAMS, keys=KEYS)
    return fr.Context(**(base | kw))


def _fails(rule, table="Claim", tables=None, ctx=None):
    tables = tables or {"Claim": CLAIM, "Measure": MEASURE}
    out = fr.check_tables(fr.RuleSet("t", "x", None, [rule]), tables, ctx or _ctx())
    results, notes = out[table]
    return (list(results[0].fail) if results else [False] * len(tables[table])), notes


def test_condition_on_another_table_links_by_key_any_case_first_record():
    fail, _ = _fails(_rule("A", "ParticipantID", "is not null", conds=[("Measure", "MeasImpactType", "in", "['cust-sem']")]))
    # claim 1's measure (key in another case) is SEM; claim 2's first measure is Deemed (the first linked record counts); claim 4 has none
    assert fail == [True, False, False, False]


def test_condition_from_the_other_side_uses_the_tables_own_key():
    rule = _rule("B", "MeasImpactType", "does not equal", "'Cust-NMEC'", conds=[("Claim", "ParticipantID", "is not null", "")], table="Measure")
    fail, _ = _fails(rule, table="Measure")
    assert fail == [False, False, False, True]


def test_condition_on_the_program_and_the_filer():
    prog = _rule("C", "MeasImpactType", "equals", "'Deemed'", conds=[("Program", "Category", "contains", "savings assistance")], table="Measure")
    assert _fails(prog, table="Measure")[0] == [True, False, False, True]
    filer = _rule("D", "CCAREN_Flag", "is true", conds=[("Filer", "PAType", "in", "['CCA', 'REN']")])
    assert _fails(filer)[0] == [False, True, True, False]
    assert _fails(filer, ctx=_ctx(filer={"PA": "PGE", "PAType": "IOU"}))[0] == [False] * 4


def test_groups_all_and_any_one_level():
    rule = _rule("E", "ParticipantID", "is not null", connector="all", conds=[
        ("Claim", "BldgType", "equals", "'Com'"),
        {"connector": "any", "conds": [["Measure", "MeasImpactType", "contains", "SEM"], ["Claim", "Note", "contains", "nmec"]]},
    ])
    assert _fails(rule)[0] == [True, False, False, False]
    nested = _rule("F", "ParticipantID", "is not null", conds=[{"connector": "any", "conds": [{"connector": "all", "conds": []}]}])
    with pytest.raises(ValueError, match="one level"):
        _fails(nested)
    assert "(Measure's MeasImpactType contains SEM or Note contains nmec)" in rule.text


def test_text_flag_and_date_tests():
    assert _fails(_rule("G", "Note", "does not contain", "NMEC"))[0] == [True, True, True, False]   # a test on a blank fails, as in SQL
    assert _fails(_rule("H", "CCAREN_Flag", "is not true"))[0] == [True, False, False, True]
    assert _fails(_rule("I", "InstallationDate", "is on or after", "2015-01-01"))[0] == [False, True, False, True]   # a blank date fails a test
    assert _fails(_rule("J", "InstallationDate", "is on or before", "UploadDate"))[0] == [False, False, False, True]
    within = _rule("K", "InstallationDate", "within years of upload", "9")
    assert _fails(within)[0] == [False, True, False, True]
    when = _rule("L", "ParticipantID", "is not null", conds=[("Claim", "InstallationDate", "is after", "2019-12-31")])
    assert _fails(when)[0] == [True, False, False, False]


def test_code_in_effect_on_a_date_with_a_list_given_with_the_check():
    lists = {"Bldg": [{"code": "Com", "start_year": 2015, "end_year": None}, {"code": "Res", "start_year": 2015, "end_year": 2020}]}
    rule = _rule("M", "BldgType", "in effect on", "InstallationDate", list="Bldg")
    # 2010 is before Com's first year; Res has no date (any year it was ever in effect); a blank code is left to NotEmpty
    assert _fails(rule, ctx=_ctx(lists=lists))[0] == [False, True, False, False]
    assert "BldgType should be a code in effect on InstallationDate (Bldg value list)" == rule.text


def test_what_a_rule_reads_but_was_not_given_is_noted_not_failed():
    cases = [
        (_rule("N", "ParticipantID", "is not null", conds=[("Site", "SiteState", "equals", "'CA'")]), "the filing has no Site table"),
        (_rule("O", "ParticipantID", "is not null", conds=[("Program", "Category", "equals", "'x'")]), "Claim has no PrgID"),
        (_rule("P", "InstallationDate", "within years of upload", "9"), "the upload date was not given"),
        (_rule("Q", "CCAREN_Flag", "is true", conds=[("Filer", "PAType", "equals", "'CCA'")]), "who files was not given"),
    ]
    for rule, why in cases:
        fail, notes = _fails(rule, ctx=_ctx(upload_date=None, filer=None))
        assert fail == [False] * 4
        assert any(why in n for n in notes), (why, notes)
    fail, notes = _fails(_rule("R", "ParticipantID", "is not null", conds=[("Measure", "MeasImpactType", "equals", "'x'")]), ctx=_ctx(keys={}))
    assert any("no key links Claim to Measure" in n for n in notes)


def test_ids_of_plain_rules_are_the_ids_cedars_rules_have():
    for name in fr.SETS:
        for r in fr.rule_set(name).rules:
            if r.kind != "rule":
                continue
            s = r.spec
            assert fr.rule_id(r.severity, r.table, s["field"], s["rule"], s["value"], s["connector"], s["conds"]) == r.id
            assert fr._describe(s) == r.text                                # CEDARS' wording, with no table named on its conditions
    grouped = [["Claim", "A", "equals", "1"], {"connector": "any", "conds": [["Measure", "B", "equals", "1"], ["Claim", "C", "is null", ""]]}]
    a = fr.rule_id("reject", "Claim", "X", "is not null", "", "all", grouped)
    b = fr.rule_id("reject", "Claim", "X", "is not null", "", "all", list(reversed(grouped)))
    c = fr.rule_id("reject", "Claim", "X", "is not null", "", "any", grouped)
    assert a == b != c and a.startswith("V")
    assert len(fr.conditions({"conds": grouped})) == 3


def test_applies_matches_the_rule_runner():
    rule = _rule("S", "ParticipantID", "is not null", conds=[("Measure", "MeasImpactType", "contains", "sem")])
    held = fr.applies(rule.spec, "Claim", CLAIM, _ctx(tables={"Claim": CLAIM, "Measure": MEASURE}))
    assert list(held) == [True, False, False, False]


# ------------------------------------------------------------------ the check job


def _spec(tmp_path, rules, tables, **over):
    folder = tmp_path / "in"
    folder.mkdir(exist_ok=True)
    uris = {}
    for name, df in tables.items():
        f = folder / f"{name}.csv"
        df.to_csv(f, index=False)
        uris[name] = str(f)
    spec = {
        "check_spec_version": 1, "job_id": "check-1", "engine": {"release": __version__},
        "rule_set": {"code": "claims", "version": "test", "rules": [
            {"id": r.id, "severity": r.severity, "table": r.table, "field": r.field, "text": r.text, "kind": r.kind, "source": r.source,
             "definition": r.spec} for r in rules]},
        "tables": uris,
        "context": {"filing_year": 2027, "upload_date": "2027-04-30", "filer": {"PA": "MCE", "PAType": "CCA"}, "keys": KEYS,
                    "programs": PROGRAMS.to_dict("records")},
        "outputs": {"uri": str(tmp_path / "out")},
        "caller": {"platform_check_id": "abc"},
    }
    spec.update(over)
    path = tmp_path / "check.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return spec, path


RULES = [
    _rule("A", "ParticipantID", "is not null", conds=[("Measure", "MeasImpactType", "in", "['cust-sem']")]),
    _rule("D", "CCAREN_Flag", "is true", conds=[("Filer", "PAType", "in", "['CCA', 'REN']")], severity="warning"),
    _rule("C", "MeasImpactType", "equals", "'Deemed'", conds=[("Program", "Category", "contains", "savings assistance")], table="Measure"),
]


def test_check_job_gives_the_same_findings_as_the_rules_run_here(tmp_path):
    spec, path = _spec(tmp_path, RULES, {"Claim": CLAIM, "Measure": MEASURE})
    code, receipt = worker.main(["check", str(path)]), None
    assert code == 0
    findings = json.loads((tmp_path / "out" / "findings.json").read_text())
    receipt = json.loads((tmp_path / "out" / "receipt.json").read_text())
    here = fr.check_tables(fr.RuleSet("t", "x", None, RULES), {"Claim": CLAIM, "Measure": MEASURE}, _ctx())
    for name, (results, _notes) in here.items():
        got = {r["rule_id"]: r["rows"] for r in findings["tables"][name]["results"]}
        assert got == {r.rule.id: list(np.flatnonzero(r.fail)) for r in results}
    assert findings["tables"]["Claim"]["results"][0]["actual"] == [""]          # A fails on claim 1, whose ParticipantID is blank
    assert receipt["kind"] == "check" and receipt["status"] == "succeeded" and receipt["caller"] == {"platform_check_id": "abc"}
    assert receipt["engine"]["release"] == __version__ and receipt["tables"]["Claim"]["rows"] == 4 and receipt["findings"] == 5
    assert receipt["outputs"]["findings.json"]["sha256"]
    assert checkjob.run_check(spec, {"Claim": CLAIM, "Measure": MEASURE}, library=False)["tables"] == findings["tables"]


def test_check_job_skips_rules_and_restores_value_list_changes(tmp_path):
    editor.set_value_list_changes({"pa": {"ZZZ": {"kind": "added", "start_year": 2020, "end_year": None, "description": ""}}})
    try:
        spec, _ = _spec(tmp_path, RULES, {"Claim": CLAIM, "Measure": MEASURE}, skip={"Claim": ["A"]})
        spec["context"]["value_list_changes"] = {}
        out = checkjob.run_check(spec, {"Claim": CLAIM, "Measure": MEASURE}, library=False)
        assert [r["rule_id"] for r in out["tables"]["Claim"]["results"]] == ["D"]
        assert "ZZZ" in editor._VALUE_LIST_CHANGES["pa"]                         # put back after the check
    finally:
        editor.set_value_list_changes({})


def test_check_job_refuses_a_bad_spec_or_another_release(tmp_path, capsys):
    _spec_, path = _spec(tmp_path, RULES, {"Claim": CLAIM}, engine={"release": "0.0.1"})
    assert worker.main(["check", str(path)]) == 2
    assert "this worker is recet" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
    bad = checkjob.validate({"check_spec_version": 2, "job_id": "", "rule_set": {"rules": [{"id": "x"}]}, "tables": {}, "context": {"when": 1},
                             "outputs": {}})
    assert any("check_spec_version" in e for e in bad) and any("rules[0]" in e for e in bad) and any("unknown key 'when'" in e for e in bad)
    assert any(e.startswith("tables") for e in bad) and any(e.startswith("outputs") for e in bad)
    _spec_, path = _spec(tmp_path, RULES, {"Claim": CLAIM})
    spec = json.loads(path.read_text())
    spec["tables"]["Claim"] = {"uri": spec["tables"]["Claim"], "sha256": "0" * 64}
    path.write_text(json.dumps(spec))
    assert worker.main(["check", str(path)]) == 2
    assert "is not the pinned" in capsys.readouterr().err


def test_a_failed_check_still_writes_its_receipt(tmp_path):
    broken = _rule("Z", "ParticipantID", "is not null", conds=[{"connector": "any", "conds": [{"connector": "all", "conds": []}]}])
    _spec_, path = _spec(tmp_path, [broken], {"Claim": CLAIM})
    assert worker.main(["check", str(path)]) == 1
    receipt = json.loads((tmp_path / "out" / "receipt.json").read_text())
    assert receipt["status"] == "failed" and "one level" in receipt["error"] and not (tmp_path / "out" / "findings.json").exists()
