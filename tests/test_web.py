"""The web app (recet/web): sign-in and accounts, who may see what, CSRF, the /ReCET path, a whole run from upload to results, and the Edit inputs drafts.

Runs use the synthetic ACC and flat profile of conftest (through Settings.run_defaults) and are executed by calling the queue by hand (runner.work_once), so the
tests need no reference data and no background threads."""
from __future__ import annotations

import io
import json
import re
import zipfile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
from fastapi.testclient import TestClient

from recet.web import auth
from recet.web.app import create_app
from recet.web.config import Settings
from conftest import make_electric_acc, make_flat_profile_csv, make_measure_csv

PW = "correct horse battery"
P = "/ReCET"


@pytest.fixture()
def web(tmp_path):
    s = Settings(state_dir=tmp_path / "web", start_workers=False, secure_cookies=False, trust_proxy=False,
                 run_defaults={"acc_electric_file": str(make_electric_acc(tmp_path / "acc.parquet")),
                               "profiles_8760_file": str(make_flat_profile_csv(tmp_path / "p.csv"))})
    app = create_app(s)
    return app, app.state.ctx


def _client(app) -> TestClient:
    return TestClient(app, base_url="http://testserver", follow_redirects=False)


def _user(ctx, email, role="user", pw=PW) -> int:
    uid = auth.create_user(ctx.db, email, "", role)
    auth.set_password(ctx.db, uid, pw)
    return uid


def _login(app, email, pw=PW) -> TestClient:
    c = _client(app)
    page = c.get(f"{P}/login")
    token = re.search(r'name="login_csrf" value="([^"]+)"', page.text).group(1)
    r = c.post(f"{P}/login", data={"email": email, "password": pw, "login_csrf": token, "next": ""})
    assert r.status_code == 303 and r.headers["location"] == f"{P}/", r.text
    return c


def _csrf(c: TestClient, path=f"{P}/runs/new") -> str:
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)


def _start_run(c, tmp_path, **form):
    data = {"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files", "market_effects": "0", "output_format": "csv", **form}
    files = {"measures": ("Measure.csv", make_measure_csv(tmp_path / "Measure.csv").read_bytes(), "text/csv")}
    r = c.post(f"{P}/runs", data=data, files=files)
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[-1]


# ------------------------------------------------------------------------------------------------------------------------- path and sign-in
def test_root_and_case_redirects(web):
    app, _ctx = web
    c = _client(app)
    assert c.get("/").headers["location"] == f"{P}/"
    r = c.get("/recet/runs?all=1")
    assert r.status_code == 301 and r.headers["location"] == f"{P}/runs?all=1"
    assert c.get("/RECET").headers["location"] == P
    r = c.get(f"{P}/runs")
    assert r.status_code == 303 and r.headers["location"] == f"{P}/login?next={P}/runs"
    assert c.get(f"{P}/healthz").text == "ok"


def test_security_headers(web):
    app, _ctx = web
    r = _client(app).get(f"{P}/login")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"


def test_setup_link_sets_the_password_once(web):
    app, ctx = web
    uid = auth.create_user(ctx.db, "a@example.com", "A", "user")
    token = auth.new_setup_link(ctx.db, uid, 72)
    c = _client(app)
    assert c.get(f"{P}/setup/{token}").status_code == 200
    assert c.post(f"{P}/setup/{token}", data={"password": "short", "password2": "short"}).status_code == 400
    r = c.post(f"{P}/setup/{token}", data={"password": PW, "password2": PW})
    assert r.status_code == 303
    assert c.get(f"{P}/runs").status_code == 200                             # signed in by the link
    assert _client(app).get(f"{P}/setup/{token}").status_code == 404           # used
    _login(app, "a@example.com")


def test_wrong_password_and_lockout(web):
    app, ctx = web
    _user(ctx, "b@example.com")
    c = _client(app)
    for _ in range(auth.MAX_FAILS_PER_ACCOUNT):
        tok = re.search(r'name="login_csrf" value="([^"]+)"', c.get(f"{P}/login").text).group(1)
        assert c.post(f"{P}/login", data={"email": "b@example.com", "password": "nope nope nope", "login_csrf": tok}).status_code == 401
    tok = re.search(r'name="login_csrf" value="([^"]+)"', c.get(f"{P}/login").text).group(1)
    assert c.post(f"{P}/login", data={"email": "b@example.com", "password": PW, "login_csrf": tok}).status_code == 429


def test_login_needs_the_form_token(web):
    app, ctx = web
    _user(ctx, "c@example.com")
    r = _client(app).post(f"{P}/login", data={"email": "c@example.com", "password": PW, "login_csrf": "forged"})
    assert r.status_code == 303 and "expired" in r.headers["location"]


def test_disabled_user_loses_the_session(web):
    app, ctx = web
    uid = _user(ctx, "d@example.com")
    c = _login(app, "d@example.com")
    ctx.db.run("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
    assert c.get(f"{P}/runs").status_code == 303


def test_only_admins_manage_users(web):
    app, ctx = web
    _user(ctx, "admin@example.com", "admin")
    _user(ctx, "e@example.com")
    assert _login(app, "e@example.com").get(f"{P}/admin/users").status_code == 404
    a = _login(app, "admin@example.com")
    r = a.post(f"{P}/admin/users", data={"csrf": _csrf(a, f"{P}/admin/users"), "email": "new@example.com", "name": "New", "role": "user"})
    assert r.status_code == 200 and "/setup/" in r.text
    assert auth.user_by_email(ctx.db, "new@example.com") is not None


# ------------------------------------------------------------------------------------------------------------------------- runs
def test_csrf_is_required(web, tmp_path):
    app, ctx = web
    _user(ctx, "f@example.com")
    c = _login(app, "f@example.com")
    files = {"measures": ("Measure.csv", make_measure_csv(tmp_path / "Measure.csv").read_bytes(), "text/csv")}
    r = c.post(f"{P}/runs", data={"csrf": "wrong", "acc_version": "2026", "input_mode": "files"}, files=files)
    assert r.status_code == 400
    assert ctx.db.one("SELECT COUNT(*) n FROM runs")["n"] == 0


def test_bad_options_are_refused(web, tmp_path):
    app, ctx = web
    _user(ctx, "g@example.com")
    c = _login(app, "g@example.com")
    files = {"measures": ("Measure.csv", make_measure_csv(tmp_path / "Measure.csv").read_bytes(), "text/csv")}
    for bad in ({"acc_version": "2021"}, {"market_effects": "0.5"}, {"compare_with": "2026"}):
        data = {"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files", **bad}
        assert c.post(f"{P}/runs", data=data, files=files).status_code == 400
    r = c.post(f"{P}/runs", data={"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files"},
               files={"measures": ("evil.exe", b"x", "application/octet-stream")})
    assert r.status_code == 400


def test_a_run_from_upload_to_results(web, tmp_path):
    app, ctx = web
    _user(ctx, "h@example.com")
    c = _login(app, "h@example.com")
    run_id = _start_run(c, tmp_path, label="first run")
    assert ctx.db.one("SELECT status FROM runs WHERE id = ?", (run_id,))["status"] == "queued"
    assert ctx.runner.position(run_id) == 1
    assert ctx.runner.work_once()
    row = ctx.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    log = c.get(f"{P}/runs/{run_id}/log").json()
    assert row["status"] == "finished", log
    assert row["result_dir"] and row["result_dir"].startswith("out")
    assert "finished." in log["text"] and log["status"] == "finished"
    page = c.get(f"{P}/runs/{run_id}")
    assert page.status_code == 200 and "first run" in page.text
    res = c.get(f"{P}/runs/{run_id}/results?level=portfolio")
    assert res.status_code == 200 and ("TotalSystemBenefit" in res.text or "Total system benefit" in res.text)
    csv = c.get(f"{P}/runs/{run_id}/results.csv?level=measure")
    assert csv.status_code == 200 and "T1" in csv.text
    z = zipfile.ZipFile(io.BytesIO(c.get(f"{P}/runs/{run_id}/zip").content))
    assert any(n.startswith("measure_level") for n in (p.rsplit("/", 1)[-1] for p in z.namelist()))
    # files stay inside the run folder
    assert c.get(f"{P}/runs/{run_id}/files/../../recet_web.sqlite3").status_code == 404
    assert c.get(f"{P}/runs/{run_id}/files/..%2F..%2Frecet_web.sqlite3").status_code == 404


def test_runs_are_private_to_their_owner(web, tmp_path):
    app, ctx = web
    _user(ctx, "owner@example.com")
    _user(ctx, "other@example.com")
    _user(ctx, "boss@example.com", "admin")
    run_id = _start_run(_login(app, "owner@example.com"), tmp_path)
    other = _login(app, "other@example.com")
    assert other.get(f"{P}/runs/{run_id}").status_code == 404
    assert other.get(f"{P}/runs/{run_id}/log").status_code == 404
    assert run_id not in other.get(f"{P}/runs").text
    assert _login(app, "boss@example.com").get(f"{P}/runs/{run_id}").status_code == 200


def test_cancel_a_queued_run_and_cleanup(web, tmp_path):
    app, ctx = web
    _user(ctx, "i@example.com")
    c = _login(app, "i@example.com")
    run_id = _start_run(c, tmp_path)
    page = c.get(f"{P}/runs/{run_id}")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert c.post(f"{P}/runs/{run_id}/cancel", data={"csrf": csrf}).status_code == 303
    assert ctx.db.one("SELECT status FROM runs WHERE id = ?", (run_id,))["status"] == "cancelled"
    assert not ctx.runner.work_once()
    import time
    assert ctx.runner.cleanup(now=time.time() + 8 * 86400)["runs"] == 1
    assert ctx.db.one("SELECT status FROM runs WHERE id = ?", (run_id,))["status"] == "expired"
    assert not ctx.runner.folder(run_id).exists()


def test_a_failed_run_says_so(web, tmp_path):
    app, ctx = web
    _user(ctx, "j@example.com")
    c = _login(app, "j@example.com")
    data = {"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files"}
    r = c.post(f"{P}/runs", data=data, files={"measures": ("Measure.csv", b"not|a\nreal|file\n", "text/csv")})
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    ctx.runner.work_once()
    row = ctx.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    assert row["status"] == "failed" and row["message"]


# ------------------------------------------------------------------------------------------------------------------------- Edit inputs
def _new_draft(c) -> str:
    r = c.post(f"{P}/edit", data={"csrf": _csrf(c, f"{P}/edit"), "mode": "new", "name": "my job"})
    assert r.status_code == 303, r.text
    return r.headers["location"].split("draft=")[1]


def test_draft_edit_save_and_checks(web):
    app, ctx = web
    _user(ctx, "k@example.com")
    c = _login(app, "k@example.com")
    draft = _new_draft(c)
    page = c.get(f"{P}/edit?draft={draft}")
    assert page.status_code == 200 and "tabulator.min.js" in page.text
    csrf = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
    d = c.get(f"{P}/edit/api/{draft}").json()
    assert d["name"] == "my job" and d["layout"]["Measure"]["pinned"] == ["CEInputID", "MeasCode", "MeasDescription"]
    m = d["tables"]["Measure"]
    cols, rows = m["columns"], m["rows"]
    # a blank NumUnits stops the run
    rows[0][cols.index("NumUnits")] = ""
    body = {"rev": d["rev"], "tables": {"Measure": {"columns": cols, "rows": rows}, "ProgramCost": d["tables"]["ProgramCost"]}}
    assert c.put(f"{P}/edit/api/{draft}", json=body).status_code == 400                      # no CSRF header
    r = c.put(f"{P}/edit/api/{draft}", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert any(i["column"] == "NumUnits" and i["severity"] == "error" for i in saved["issues"])
    assert saved["rev"] == d["rev"] + 1
    # a stale window is refused
    assert c.put(f"{P}/edit/api/{draft}", json=body, headers={"X-CSRF-Token": csrf}).status_code == 409
    # running it is refused while it has errors
    r = c.post(f"{P}/runs", data={"csrf": csrf, "draft": draft, "acc_version": "2026"})
    assert r.status_code == 400 and "NumUnits" in r.text
    # too many rows
    big = {"rev": saved["rev"], "tables": {"Measure": {"columns": cols, "rows": [rows[0]] * 501}, "ProgramCost": d["tables"]["ProgramCost"]}}
    assert c.put(f"{P}/edit/api/{draft}", json=big, headers={"X-CSRF-Token": csrf}).status_code == 400


def test_draft_runs_and_downloads(web, tmp_path):
    app, ctx = web
    _user(ctx, "l@example.com")
    c = _login(app, "l@example.com")
    r = c.post(f"{P}/edit", data={"csrf": _csrf(c, f"{P}/edit"), "mode": "upload", "name": ""},
               files={"measures": ("Measure.csv", make_measure_csv(tmp_path / "Measure.csv").read_bytes(), "text/csv")})
    assert r.status_code == 303, r.text
    draft = r.headers["location"].split("draft=")[1]
    d = c.get(f"{P}/edit/api/{draft}").json()
    assert d["tables"]["Measure"]["rows"][0][d["tables"]["Measure"]["columns"].index("CEInputID")] == "T1"
    assert not [i for i in d["issues"] if i["severity"] == "error"], d["issues"]
    z = zipfile.ZipFile(io.BytesIO(c.get(f"{P}/edit/{draft}/download.zip").content))
    assert "Measure.csv" in z.namelist()
    assert c.get(f"{P}/edit/{draft}/download.xlsx").content[:2] == b"PK"
    page = c.get(f"{P}/runs/new?draft={draft}")
    assert page.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = c.post(f"{P}/runs", data={"csrf": csrf, "draft": draft, "acc_version": "2026", "market_effects": "0", "output_format": "csv"})
    assert r.status_code == 303, r.text
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    ctx.runner.work_once()
    row = ctx.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    assert row["status"] == "finished", c.get(f"{P}/runs/{run_id}/log").json()["text"]
    assert "Edited inputs" in row["inputs"]
    assert list(ctx.runner.folder(run_id).glob("inputs/_edited_inputs/*/Measure.csv"))


def test_drafts_are_private(web):
    app, ctx = web
    _user(ctx, "m@example.com")
    _user(ctx, "n@example.com")
    draft = _new_draft(_login(app, "m@example.com"))
    other = _login(app, "n@example.com")
    assert other.get(f"{P}/edit?draft={draft}").status_code == 404
    assert other.get(f"{P}/edit/api/{draft}").status_code == 404
    assert other.get(f"{P}/edit/{draft}/download.zip").status_code == 404
    assert _client(app).get(f"{P}/edit/api/{draft}").status_code == 401                     # signed out: JSON, not a redirect


def test_end_use_choices(web):
    app, ctx = web
    _user(ctx, "o@example.com")
    c = _login(app, "o@example.com")
    vals = c.get(f"{P}/edit/api/end-uses?iou=PGE&sector=Res").json()["values"]
    assert vals[0] == "" and len(vals) > 1                                                   # no library here: CET's value list


def test_web_cli_create_admin_prints_a_link(tmp_path, monkeypatch, capsys):
    from recet.web import cli
    monkeypatch.setenv("RECET_WEB_STATE", str(tmp_path / "state"))
    assert cli.main(["create-admin", "jake@example.com", "--name", "Jake"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"https://example.org/ReCET/setup/\S+", out)
    assert cli.main(["users"]) == 0
    assert "jake@example.com" in capsys.readouterr().out
    assert cli.main(["reset-link", "nobody@example.com"]) == 1
