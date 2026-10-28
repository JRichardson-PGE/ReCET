"""Sharing a run read-only with another user, and comparing any two runs (BRD BR-06.11)."""
from __future__ import annotations

import io

import pandas as pd

from recet import results_view as rv
from test_web import P, _csrf, _login, _start_run, _user, web  # noqa: F401  (web is a fixture)


def _finished(ctx, c, tmp_path, **form) -> str:
    run_id = _start_run(c, tmp_path, **form)
    assert ctx.runner.work_once()
    assert ctx.db.one("SELECT status FROM runs WHERE id = ?", (run_id,))["status"] == "finished"
    return run_id


def test_a_shared_run_can_be_read_but_not_changed(web, tmp_path):
    app, ctx = web
    _user(ctx, "owner@example.com")
    _user(ctx, "reader@example.com")
    owner = _login(app, "owner@example.com")
    run_id = _finished(ctx, owner, tmp_path, label="shared run")
    reader = _login(app, "reader@example.com")
    assert reader.get(f"{P}/runs/{run_id}").status_code == 404
    token = _csrf(owner, f"{P}/runs/{run_id}")
    assert owner.post(f"{P}/runs/{run_id}/share", data={"csrf": token, "email": "nobody@example.com"}).status_code == 400
    assert owner.post(f"{P}/runs/{run_id}/share", data={"csrf": token, "email": "owner@example.com"}).status_code == 400
    assert owner.post(f"{P}/runs/{run_id}/share", data={"csrf": token, "email": "Reader@Example.com"}).status_code == 303
    assert "reader@example.com" in owner.get(f"{P}/runs/{run_id}").text
    # the reader sees it under Shared with you, reads its pages and files, but cannot stop, delete or share it
    assert "Shared with you" in reader.get(f"{P}/runs").text and "shared run" in reader.get(f"{P}/runs").text
    page = reader.get(f"{P}/runs/{run_id}")
    assert page.status_code == 200 and ">Delete<" not in page.text and "Share with" not in page.text
    assert reader.get(f"{P}/runs/{run_id}/results?level=program").status_code == 200
    assert reader.get(f"{P}/runs/{run_id}/zip").status_code == 200
    rtoken = _csrf(reader, f"{P}/runs/{run_id}")
    assert reader.post(f"{P}/runs/{run_id}/delete", data={"csrf": rtoken}).status_code == 404
    assert reader.post(f"{P}/runs/{run_id}/share", data={"csrf": rtoken, "email": "reader@example.com"}).status_code == 404
    assert ctx.db.one("SELECT status FROM runs WHERE id = ?", (run_id,))["status"] == "finished"
    events = [r["event"] for r in ctx.db.all("SELECT event FROM audit")]
    assert "run shared" in events
    # stop sharing
    uid = ctx.db.one("SELECT id FROM users WHERE email = 'reader@example.com'")["id"]
    assert owner.post(f"{P}/runs/{run_id}/unshare", data={"csrf": token, "user_id": str(uid)}).status_code == 303
    assert reader.get(f"{P}/runs/{run_id}").status_code == 404


def test_compare_two_runs_side_by_side(web, tmp_path):
    app, ctx = web
    _user(ctx, "cmp@example.com")
    c = _login(app, "cmp@example.com")
    a = _finished(ctx, c, tmp_path, label="first")
    b = _finished(ctx, c, tmp_path, label="second", market_effects="0.05")
    assert "Compare with another run" in c.get(f"{P}/runs/{a}").text
    page = c.get(f"{P}/runs/{a}/compare?with={b}&level=program")
    assert page.status_code == 200 and "Difference" in page.text and "second" in page.text
    csv = c.get(f"{P}/runs/{a}/compare.csv?with={b}&level=portfolio&columns=TotalSystemBenefit")
    assert csv.status_code == 200
    t = pd.read_csv(io.StringIO(csv.text))
    tsb = [col for col in t.columns if col.startswith("Total") or "TotalSystemBenefit" in col]
    assert len(tsb) == 4, t.columns
    va, vb, diff = (float(t[col].iloc[0]) for col in tsb[:3])
    assert abs(diff - (vb - va)) < 1e-6 and vb > va                     # market effects add benefits
    # a run the user cannot see cannot be compared
    _user(ctx, "else@example.com")
    other = _login(app, "else@example.com")
    assert other.get(f"{P}/runs/{a}/compare?with={b}").status_code == 404


def test_compare_matches_groups_and_leaves_missing_ones_blank():
    fa = pd.DataFrame({"PrgID": ["P1", "P2"], "IOU_AC_Territory": ["PGE", "PGE"], "CreditedPA": ["PGE", "PGE"], "Used_Year": [2028, 2028],
                       "TotalSystemBenefit": [10.0, 5.0]})
    fb = pd.DataFrame({"PrgID": ["P1", "P3"], "IOU_AC_Territory": ["PGE", "PGE"], "CreditedPA": ["PGE", "PGE"], "Used_Year": [2028, 2028],
                       "TotalSystemBenefit": [12.0, 7.0]})
    t = rv.compare(rv.RunResults.from_frame(fa), rv.RunResults.from_frame(fb), "program", ["TotalSystemBenefit"]).set_index("PrgID")
    assert t.loc["P1", "TotalSystemBenefit__diff"] == 2.0 and abs(t.loc["P1", "TotalSystemBenefit__pct"] - 0.2) < 1e-12
    assert pd.isna(t.loc["P2", "TotalSystemBenefit__b"]) and pd.isna(t.loc["P3", "TotalSystemBenefit__a"])
    p = rv.compare(rv.RunResults.from_frame(fa), rv.RunResults.from_frame(fb), "portfolio", ["TotalSystemBenefit"])
    assert len(p) == 1 and p["TotalSystemBenefit__a"].iloc[0] == 15.0 and p["TotalSystemBenefit__b"].iloc[0] == 19.0
    assert p["Measures__a"].iloc[0] == 2
