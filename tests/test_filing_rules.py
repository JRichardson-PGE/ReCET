"""CEDARS' upload checks as a rule library (recet/filing_rules.py): every rule of both shipped sets passes a record that meets it and fails one that does not,
and the run flags the measures CEDARS would reject or warn on."""
from __future__ import annotations

import ast
import json

import numpy as np
import pandas as pd
import pytest

from recet import editor, filing_rules as fr, preflight
from recet.fieldnames import recet_name

YEAR = 2028


def _code(name: str) -> str:
    codes = editor.active_codes(fr.VALUE_LIST_FILES.get(name.lower(), name.lower()), YEAR)
    return "PGE" if name.lower() == "pa" else codes[0]


def _base(rs: fr.RuleSet, table: str) -> dict:
    """A record that meets every field constraint of the set's spec for `table`."""
    row: dict[str, str] = {}
    for r in rs.rules:
        if r.kind != "spec" or r.table != table or r.spec["check"] == "unique":
            continue
        c, k = recet_name(r.field), r.spec["check"]
        if k == "numeric":
            row.setdefault(c, "1")
        elif k == "bit":
            row[c] = "0"
        elif k == "year_quarter":
            row[c] = f"{YEAR}Q1"
        elif k == "value_list":
            row[c] = "PGE_Ag_001" if r.spec["list"].lower() == "prgid" else _code(r.spec["list"])
        elif k == "starts_with":
            row[c] = f"PGE-{YEAR}-1"
        elif k == "max_length":
            row.setdefault(c, "x")
    if table == "Measure":
        row.update(CEInputID=f"PGE-{YEAR}-1", EUL_Yrs="10", RUL_Yrs="0", MeasInflation="0")
    else:
        row.update(PrgYear=str(YEAR), ClaimYearQuarter=f"{YEAR}Q4", PrgID="PGE_Ag_001")
    return row


def _value(rule: str, value: str, hold: bool, row: dict, field: str):
    """Set `field` in `row` so that `field rule value` holds (or not). Returns False if the generator cannot."""
    v = value.strip()
    col = recet_name(field)
    if rule == "is not null":
        row[col] = "x" if hold else ""
        return True
    if v.startswith("["):
        items = ast.literal_eval(v)
        inside = items[0]
        row[col] = ("Deem" if hold else inside) if rule == "not in" else (inside if hold else "Deem")
        return True
    if rule == "ends with string":
        row[col] = f"Deem-{v}" if hold else "Deem"
        return True
    if rule == "matches_year_quarter":
        row[recet_name(v)] = f"{YEAR}Q1"
        row[col] = str(YEAR if hold else YEAR + 1)
        return True
    if v.lower() == "filingyear":
        row[col] = str(YEAR if hold else YEAR + 1)
        return True
    if len(v) >= 2 and v[0] == v[-1] == "'":
        t = v[1:-1]
        row[col] = (t if hold else "zz") if rule == "equals" else ("zz" if hold else t)
        return True
    try:
        x = float(v)
        other = None
    except ValueError:
        x = None
        other = v
    if other is not None:                       # a column, or arithmetic on one: EUL_Yrs, "EUL_Yrs /2.75"
        name = other.split("/")[0].split("*")[0].strip()
        row[recet_name(name)] = "11"
        x = 11 / 2.75 if "/" in other else 11.0
    lo, hi = x - 1, x + 1
    hold_v, fail_v = {"equals": (x, hi), "does not equal": (hi, x), "is less than": (lo, hi), "is less than or equal to": (x, hi),
                      "greater than": (hi, lo), "is greater than": (hi, lo), "is greater than or equal to": (x, lo)}[rule]
    row[col] = repr(float(hold_v if hold else fail_v))
    return True


def _rows(r: fr.Rule, base: dict) -> tuple[dict, dict]:
    """A record that passes rule `r` and one that fails it."""
    good, bad = dict(base), dict(base)
    s = r.spec
    if r.kind == "rule":
        for row in (good, bad):
            for _t, f, rule, v in (s["conds"][:1] if s["connector"] == "any" else s["conds"]):
                _value(rule.lower(), v, True, row, f)
        _value(s["rule"], s["value"], True, good, s["field"])
        _value(s["rule"], s["value"], False, bad, s["field"])
        return good, bad
    c, k = recet_name(r.field), s["check"]
    bad[c] = {"not_empty": "", "numeric": "abc", "bit": "2", "year_quarter": f"{YEAR}Q5", "value_list": "NOT_A_CODE", "starts_with": f"XYZ-{YEAR}-1",
              "max_length": "a" * (s.get("n", 0) + 1)}.get(k, bad.get(c))
    if k == "range":
        lo, hi = s["lo"], s["hi"]
        bad[c] = repr((lo - 1) if lo is not None else hi + 1)
        good[c] = repr(lo + (0.5 if s["lo_open"] or hi is None else 0)) if lo is not None else repr(hi - 1)
        if hi is not None and lo is not None:
            good[c] = repr((lo + hi) / 2)
    return good, bad


@pytest.mark.parametrize("name", list(fr.SETS))
def test_every_rule_passes_a_good_record_and_fails_a_bad_one(name):
    rs = fr.rule_set(name)
    ctx = fr.Context(filing_year=YEAR, library=False)
    checked = 0
    for table in ("Measure", "ProgramCost"):
        base = _base(rs, table)
        for r in (x for x in rs.rules if x.table == table):
            if r.spec.get("rule") == "combo_value_list":
                continue                                                    # needs the profile library: test_the_combo_rule_needs_the_library
            if r.spec.get("check") == "unique":
                df = pd.DataFrame([base, base, dict(base, PrgYear=str(YEAR + 1))]).astype(str)
                res, _ = fr.check_frame(fr.RuleSet(name, rs.version, rs.folder, [r]), table, df, ctx)
                assert res and res[0].fail.tolist() == [True, True, False], r.text
                checked += 1
                continue
            good, bad = _rows(r, base)
            df = pd.DataFrame([good, bad]).astype(str)
            res, notes = fr.check_frame(fr.RuleSet(name, rs.version, rs.folder, [r]), table, df, ctx)
            assert not notes, (r.id, notes)
            assert res and res[0].fail.tolist() == [False, True], f"{r.id} {r.text}: good {good.get(recet_name(r.field))!r}, bad {bad.get(recet_name(r.field))!r}"
            checked += 1
    assert checked == len(rs.rules) - sum(r.spec.get("rule") == "combo_value_list" for r in rs.rules)


@pytest.mark.parametrize("name", list(fr.SETS))
def test_every_constraint_of_the_spec_becomes_a_rule(name):
    import re
    rs = fr.rule_set(name)
    text = next(rs.folder.glob("*_spec*.sql")).read_text()
    fields = re.findall(r"^\s*(\w+)\s+(nvarchar|numeric|bit|CedarsYearQuarter)", text, re.M | re.I)
    kinds = [r.spec["check"] for r in rs.rules if r.kind == "spec"]
    assert len(fields) > 80
    assert kinds.count("not_empty") == len(re.findall(r"\bNotEmpty\b", text))
    assert kinds.count("value_list") == len(re.findall(r"\bCedarsValueList\b", text))
    assert kinds.count("range") == len(re.findall(r"\b(CedarsNumRange|GreaterThanOrEqualTo)\b", text))
    assert kinds.count("max_length") == len(re.findall(r"nvarchar\(\d+\)", text))
    assert kinds.count("numeric") == len(re.findall(r"\snumeric\b", text))
    assert len(rs.rules) == sum(1 for r in rs.rules if r.kind == "rule") + len(kinds)
    rows = sum(len(pd.read_csv(f)) for f in [*rs.folder.glob("validation_rules*.csv"), *rs.folder.glob("warning_rules*.csv")])
    assert sum(r.kind == "rule" for r in rs.rules) in (rows, rows - 1)     # the claims file lists one rule twice: kept once


def test_the_two_sets_share_ids_for_shared_rules():
    claims, budget = ({r.id: r for r in fr.rule_set(n).rules} for n in ("claims", "budget_filing"))
    shared = set(claims) & set(budget)
    assert len(shared) > 100                                               # most spec constraints and rules are the same in both
    assert all(claims[i].text == budget[i].text for i in shared)
    assert "PrgYear should equal FilingYear" in {budget[i].text for i in set(budget) - shared}       # budget filing only
    assert any("MiscCostsDesc" in claims[i].text for i in set(claims) - shared)                        # claims only
    assert len({r.id for r in fr.rule_set("claims").rules}) == len(fr.rule_set("claims").rules)        # unique within a set


def test_rule_sets_are_versioned_by_effective_date():
    assert fr.versions("claims") == ["2026-03-12"] and fr.versions("budget_filing") == ["2026-03-11"]
    d = fr.rule_set("claims").describe()
    assert d["effective_date"] == "2026-03-12" and d["rules"] == d["rejects"] + d["warnings"] and "cet_spec.2026-03-12.sql" in d["files"]
    with pytest.raises(ValueError):
        fr.rule_set("claims", as_of="2020-01-01")
    with pytest.raises(ValueError):
        fr.rule_set("monthly")


def test_upload_pa_year_is_a_plain_prefix():
    rs = fr.rule_set("budget_filing")
    sw = [r for r in rs.rules if r.spec.get("check") == "starts_with"]
    ids = ["PGE-2028-PGE_Ag_001-0001", "PGE-2028Q4-PGE_EMV_002-1", "SW-2028-PGE_SW_IP_Gov-0007", "PGE-2029-x", "XYZ-2028-x", "PGE2028"]
    df = pd.DataFrame({"CEInputID": ids, "ClaimYearQuarter": ["2028Q1"] * len(ids)})
    res, _ = fr.check_frame(fr.RuleSet("budget_filing", rs.version, rs.folder, sw), "Measure", df, fr.Context(None, False))
    assert res[0].fail.tolist() == [False, False, False, True, True, True]   # CEDARS accepted the first three forms in real filings
    res, _ = fr.check_frame(fr.RuleSet("budget_filing", rs.version, rs.folder, sw), "Measure", df, fr.Context(2029, False))
    assert res[0].fail.tolist() == [True, True, True, False, True, True]     # a filing year given: every record must name it


def test_messages_follow_cet_feedback():
    texts = {r.text for r in fr.rule_set("claims").rules}
    assert ("When UnitIncentiveToOthers equals 0 and UnitEndUserRebate equals 0 and UnitDirectInstallLab equals 0 and UnitDirectInstallMat equals 0, "
            "UnitMeaCost1stBaseline should be different from 0") in texts         # the wording CEDARS' own upload feedback uses
    assert "NTGRkWh should be in (0, 1.5]" in texts


def test_the_combo_rule_needs_the_library(monkeypatch):
    rs = fr.rule_set("claims")
    combo = [r for r in rs.rules if r.spec.get("rule") == "combo_value_list"]
    assert len(combo) == 3
    df = pd.DataFrame([dict(_base(rs, "Measure"), UnitkWh1stBaseline="5", UnitkW1stBaseline="0", UnitkWhIOUWater1stBaseline="0", DeliveryType="Down")]).astype(str)
    sub = fr.RuleSet("claims", rs.version, rs.folder, combo)
    _res, notes = fr.check_frame(sub, "Measure", df, fr.Context(YEAR, library=False))
    assert len(notes) == 3 and all("library is not installed" in n for n in notes)
    from recet.pipeline import real_impact_profiles
    monkeypatch.setattr(real_impact_profiles, "match_profile_id", lambda e, i, s: (1, e == "GOOD"))
    df = pd.DataFrame([dict(df.iloc[0], AC_Elec_Impact_Profile="GOOD"), dict(df.iloc[0], AC_Elec_Impact_Profile="BAD")]).astype(str)
    res, _ = fr.check_frame(sub, "Measure", df, fr.Context(YEAR, library=True))
    assert [r.fail.tolist() for r in res] == [[False, True]]                 # only the kWh rule applies (kW and water are 0)


def test_preflight_flags_but_does_not_stop(tmp_path):
    from test_chunked import _job
    m, pc = _job(tmp_path, n=12)
    plain = preflight.check_inputs(m, pc)
    rep = preflight.check_inputs(m, pc, filing_rules="claims")
    assert rep.ok and plain.ok
    assert rep.filing_rules["rule_set"] == "claims" and "CET filing validation" in rep.stats
    codes = {i.code for i in rep.issues}
    assert "cet_reject" in codes                                           # the test job is not a CEDARS-ready file (Sector 'Res', blank DeliveryType ...)
    assert "outside_cet_range" not in codes                                # the filing rules check those ranges under their IDs
    blank_dt = next(i for i in rep.issues if i.code == "cet_reject" and i.message.endswith("record that fails it") and "DeliveryType should not be empty" in i.message)
    assert blank_dt.count == 12 and blank_dt.cells[0] == ("Measure", 0, "DeliveryType")
    f = rep.filing[rep.filing["table"] == "Measure"]
    assert len(f) == 12 and set(f["FilingValidation"]) == {"reject"}
    assert json.loads(json.dumps(rep.to_dict()))["filing_rules"]["effective_date"] == "2026-03-12"


def test_checked_in_parts_gives_the_same_report(tmp_path):
    from test_chunked import _job
    m, pc = _job(tmp_path, n=40)
    whole = preflight.check_inputs(m, pc, filing_rules="budget_filing", filing_year=2028)
    parts = preflight.check_inputs(m, pc, filing_rules="budget_filing", filing_year=2028, chunk_size=7)
    key = lambda rep: sorted((i.code, i.message, i.count) for i in rep.issues if i.code.startswith("cet_"))
    assert key(whole) == key(parts)
    assert whole.filing.sort_values(["table", "id"]).reset_index(drop=True).equals(parts.filing.sort_values(["table", "id"]).reset_index(drop=True))
    pc_rejects = whole.filing[(whole.filing["table"] == "ProgramCost")]
    assert set(pc_rejects["id"]) >= {"PRG0 2029"}                         # PrgYear 2029 against filing year 2028 (and PRG0 is not a CEDARS program)


@pytest.mark.parametrize("chunk", [0, 7])
def test_a_run_flags_its_measures(tmp_path, chunk):
    from test_chunked import CHUNK, _cfg, _job
    from recet.jobs import run_job
    m, pc = _job(tmp_path)
    res = run_job(_cfg(tmp_path, m, pc, run_name=f"f{chunk}", chunk_size=chunk, output_format="parquet", filing_validation="claims"), log=lambda s: None)
    out = pd.read_parquet(res.files["measure_level"])
    table = pd.read_parquet(res.files["filing_validation"])
    assert {"rule_set", "table", "record", "FilingValidation", "FilingRules", "rules_failed"} <= set(table.columns)
    flagged = table[table["table"] == "Measure"]
    assert set(flagged["FilingValidation"]) == {"reject"} and set(flagged["record"]) == set(out["CEInputID"]) and flagged["FilingRules"].str.len().gt(0).all()
    settings = json.loads(res.files["run_settings"].read_text())
    assert settings["filing_validation"]["rule_set"] == "claims" and settings["config"]["filing_validation"] == "claims"
    plain = run_job(_cfg(tmp_path, m, pc, run_name=f"p{chunk}", chunk_size=chunk, output_format="parquet"), log=lambda s: None)
    cols = list(pd.read_parquet(plain.files["measure_level"]).columns)
    assert "FilingValidation" not in cols and list(out.columns) == cols         # the flags are in filing_validation, not the measure table
    assert np.allclose(out["TotalSystemBenefit"], pd.read_parquet(plain.files["measure_level"])["TotalSystemBenefit"])   # priced the same
