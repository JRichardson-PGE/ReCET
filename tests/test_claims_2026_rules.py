"""CEDARS' 2026 claims data rules on Measure records (the claims rule set of 2026-11-27, `claims_rules.2026.json` beside CEDARS' CET files of
2026-03-12): what each rule changes or adds, and how ReCET's own rule file is read."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from recet import filing_rules as fr
from recet.fieldnames import recet_name

NEW, OLD = fr.rule_set("claims", "2026-11-27"), fr.rule_set("claims", "2026-03-12")
DOC = json.loads(next(NEW.folder.glob("*_rules*.json")).read_text())


def _rule(field: str, rs: fr.RuleSet = NEW, **spec) -> fr.Rule:
    return next(r for r in rs.rules if r.kind == "rule" and r.field == field and all(r.spec.get(k) == v for k, v in spec.items()))


def _fails(r: fr.Rule, rows: list[dict], claims: list[dict] | None = None) -> list[bool]:
    tables = {"Claim": pd.DataFrame(claims).astype(str)} if claims else None
    ctx = fr.Context(filing_year=2027, tables=tables, keys={"Claim": ["ClaimID"]} if claims else None)
    res, notes = fr.check_frame(fr.RuleSet("claims", NEW.version, NEW.folder, [r]), "Measure", pd.DataFrame([{recet_name(k): v for k, v in row.items()} for row in rows]).astype(str), ctx)
    assert not notes, notes
    return res[0].fail.tolist() if res else [False] * len(rows)          # a rule no record fails gives no result


def test_the_new_version_is_the_cet_files_with_the_2026_rules_in_place_of_three():
    old, new = {r.id for r in OLD.rules}, {r.id for r in NEW.rules}
    assert old - new == set(DOC["replaces"]) == {"V398e39", "Vc9f441", "Vefbcee"}
    assert len(new - old) == len(DOC["rules"]) and len(NEW.rules) == len(OLD.rules) - 3 + len(DOC["rules"])
    for d in DOC["rules"]:                                                  # each says where it comes from
        assert d["source"].startswith(("CEDARS 2026 claims data rules, line", "The budget filing rule"))


def test_rules_shared_with_the_budget_filing_set_keep_its_ids():
    budget = {r.id for r in fr.rule_set("budget_filing").rules}
    assert {"V1c9f94", "V9fc8e4", "V87e136"} <= budget & {r.id for r in NEW.rules}


@pytest.mark.parametrize("field", ["E3GasSector", "E3TargetSector"])
def test_a_codes_and_standards_claim_needs_no_avoided_cost_sector(field):
    r = next(x for x in NEW.rules if x.field == field and x.spec.get("rule") == "is not null")
    rows = [{"UnitTherm1stBaseline": "5", "UnitkWh1stBaseline": "5", "DeliveryType": dt, field: ""} for dt in ("C&S", "DnDeemed")]
    assert _fails(r, rows) == [False, True]
    assert _fails(r, [{"UnitTherm1stBaseline": "0", "UnitkWh1stBaseline": "0", "UnitkW1stBaseline": "0", "UnitkWhIOUWater1stBaseline": "0",
                       "DeliveryType": "DnDeemed", field: ""}]) == [False]


def test_rul_below_eul_applies_only_when_both_are_above_zero():
    r = _rule("RUL_Yrs", rule="is less than")
    rows = [{"RUL_Yrs": rul, "EUL_Yrs": eul} for rul, eul in (("5", "4"), ("5", "0"), ("0", "4"), ("3", "4"))]
    assert _fails(r, rows) == [True, False, False, False]                   # CET's rule failed (5, 0) too


def test_the_avoided_cost_territory_is_an_iou():
    r = _rule("IOU_AC_Territory")
    assert _fails(r, [{"IOU_AC_Territory": t} for t in ("PGE", "sdge", "SCG", "SCE", "MCE", "")]) == [False, False, False, False, True, True]
    # a blank fails it, as a blank fails every test (the field is required: NotEmpty says so too)


@pytest.mark.parametrize("field", ["EUL_ID", "GSIA_ID", "Version"])
def test_a_deemed_claims_measure_names_its_ids(field):
    r = _rule(field, rule="is not null")
    rows = [{"ClaimID": c, field: ""} for c in ("C1", "C2", "C3")]
    claims = [{"ClaimID": "C1", "ClaimType": "Deemed"}, {"ClaimID": "C2", "ClaimType": "Custom"}]
    assert _fails(r, rows, claims) == [True, False, False]                  # C3 has no claim record: nothing says it is deemed
    assert "When Claim's ClaimType equals 'Deemed'" in r.text


def test_a_deemed_claim_with_a_remaining_life_names_its_rul_id():
    r = _rule("RUL_ID", rule="is not null")
    rows = [{"ClaimID": "C1", "RUL_Yrs": y, "RUL_ID": ""} for y in ("3", "0")]
    assert _fails(r, rows, [{"ClaimID": "C1", "ClaimType": "Deemed"}]) == [True, False]


def test_descriptions_and_second_baseline_cost():
    assert _fails(_rule("PreDesc"), [{"RUL_Yrs": "2", "PreDesc": ""}, {"RUL_Yrs": "0", "PreDesc": ""}]) == [True, False]
    zero = dict.fromkeys(["UnitkW1stBaseline", "UnitkWh1stBaseline", "UnitkWhIOUWater1stBaseline", "UnitTherm1stBaseline", "UnitMeaCost1stBaseline"], "0")
    assert _fails(_rule("StdDesc"), [zero | {"UnitMeaCost1stBaseline": "100", "StdDesc": ""}, zero | {"StdDesc": ""}]) == [True, False]
    second = dict.fromkeys(["UnitkW2ndBaseline", "UnitkWh2ndBaseline", "UnitTherm2ndBaseline", "UnitkWhIOUWater2ndBaseline"], "0")
    r = _rule("UnitMeaCost2ndBaseline", rule="is greater than")
    assert _fails(r, [second | {"UnitkWh2ndBaseline": "4", "UnitMeaCost2ndBaseline": "0"}, second | {"UnitMeaCost2ndBaseline": "0"}]) == [True, False]


def test_a_rule_file_with_an_unknown_severity_is_refused(tmp_path):
    f = tmp_path / "x_rules.json"
    f.write_text(json.dumps({"rules": [{"severity": "info", "table": "Measure", "field": "PreDesc", "rule": "is not null"}]}))
    with pytest.raises(ValueError, match="neither reject nor warning"):
        fr._read_json_rules(f)
