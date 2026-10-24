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


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _job(measure: bytes, programcost: bytes | None = None, name: str = "job.zip"):
    """The job upload: one .zip with Measure.csv (and ProgramCost.csv), as the New run page takes it."""
    return (name, _zip({"Measure.csv": measure, **({"ProgramCost.csv": programcost} if programcost is not None else {})}), "application/zip")


def _start_run(c, tmp_path, **form):
    data = {"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files", "market_effects": "0", "output_format": "csv", **form}
    files = {"job_zip": _job(make_measure_csv(tmp_path / "Measure.csv").read_bytes())}
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
    assert c.get(P).headers["location"] == f"{P}/"            # relative, so it keeps https and the host
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
    files = {"job_zip": _job(make_measure_csv(tmp_path / "Measure.csv").read_bytes())}
    r = c.post(f"{P}/runs", data={"csrf": "wrong", "acc_version": "2026", "input_mode": "files"}, files=files)
    assert r.status_code == 400
    assert ctx.db.one("SELECT COUNT(*) n FROM runs")["n"] == 0


def test_bad_options_are_refused(web, tmp_path):
    app, ctx = web
    _user(ctx, "g@example.com")
    c = _login(app, "g@example.com")
    files = {"job_zip": _job(make_measure_csv(tmp_path / "Measure.csv").read_bytes())}
    for bad in ({"acc_version": "2021"}, {"market_effects": "0.5"}, {"compare_with": "2026"}):
        data = {"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files", **bad}
        assert c.post(f"{P}/runs", data=data, files=files).status_code == 400
    r = c.post(f"{P}/runs", data={"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files"},
               files={"job_zip": ("evil.exe", b"x", "application/octet-stream")})
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
    r = c.post(f"{P}/runs", data=data, files={"job_zip": _job(b"not|a\nreal|file\n")})
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
               files={"job_zip": _job(make_measure_csv(tmp_path / "Measure.csv").read_bytes())})
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


def test_edit_inputs_of_a_finished_run_and_rerun(web, tmp_path):
    app, ctx = web
    _user(ctx, "q@example.com")
    _user(ctx, "r@example.com")
    c = _login(app, "q@example.com")
    data = {"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files", "market_effects": "0.05", "output_format": "csv", "label": "base case",
            "emissions": "on"}
    files = {"job_zip": _job(make_measure_csv(tmp_path / "Measure.csv").read_bytes()),
             "profiles_8760": ("shapes.csv", make_flat_profile_csv(tmp_path / "shapes.csv").read_bytes(), "text/csv")}
    run_id = c.post(f"{P}/runs", data=data, files=files).headers["location"].rsplit("/", 1)[-1]
    assert ctx.runner.work_once()
    page = c.get(f"{P}/runs/{run_id}")
    assert "Edit inputs and rerun" in page.text
    assert "measures.csv" in zipfile.ZipFile(io.BytesIO(c.get(f"{P}/runs/{run_id}/inputs.zip").content)).namelist()
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    # another user can neither see nor copy the run
    assert _login(app, "r@example.com").post(f"{P}/runs/{run_id}/edit", data={"csrf": csrf}).status_code == 404
    r = c.post(f"{P}/runs/{run_id}/edit", data={"csrf": csrf})
    assert r.status_code == 303, r.text
    draft = r.headers["location"].split("draft=")[1]
    d = c.get(f"{P}/edit/api/{draft}").json()
    m = d["tables"]["Measure"]
    assert d["name"] == "base case (edited)" and m["rows"][0][m["columns"].index("CEInputID")] == "T1"
    # change a cell, then the New run page starts from the run's settings and offers its supplied file
    m["rows"][0][m["columns"].index("UnitkWh1stBaseline")] = "2000"
    edit_csrf = re.search(r'data-csrf="([^"]+)"', c.get(f"{P}/edit?draft={draft}").text).group(1)
    assert c.put(f"{P}/edit/api/{draft}", json={"rev": d["rev"], "tables": {"Measure": m, "ProgramCost": d["tables"]["ProgramCost"]}},
                 headers={"X-CSRF-Token": edit_csrf}).status_code == 200
    new = c.get(f"{P}/runs/new?draft={draft}").text
    assert 'value="0.05" selected' in new and 'value="base case (edited)"' in new and 'name="keep_profiles_8760_file" checked' in new
    assert 'name="components" >' in new and 'name="emissions" checked' in new and 'name="include_societal" >' in new
    csrf = re.search(r'name="csrf" value="([^"]+)"', new).group(1)
    r = c.post(f"{P}/runs", data={"csrf": csrf, "draft": draft, "acc_version": "2026", "market_effects": "0.05", "output_format": "csv",
                                  "keep_profiles_8760_file": "on"})
    assert r.status_code == 303, r.text
    rerun = r.headers["location"].rsplit("/", 1)[-1]
    assert ctx.runner.work_once()
    assert ctx.db.one("SELECT status FROM runs WHERE id = ?", (rerun,))["status"] == "finished", c.get(f"{P}/runs/{rerun}/log").json()["text"]
    cfg = json.loads((ctx.runner.folder(rerun) / "config.json").read_text())
    assert cfg["profiles_8760_file"].startswith(str(ctx.runner.folder(rerun) / "inputs")) and cfg["market_effects"] == 0.05
    edited = (ctx.runner.folder(rerun) / "inputs").glob("_edited_inputs/*/Measure.csv")
    assert "2000" in next(edited).read_text()
    # a run whose files are gone, and a job too big for the editor, say why on the run page
    big = tmp_path / "big.csv"
    lines = make_measure_csv(tmp_path / "one.csv").read_text().splitlines()
    i = lines[0].split("|").index("CEInputID")
    rows = []
    for k in range(501):
        v = lines[1].split("|")
        v[i] = f"T{k}"
        rows.append("|".join(v))
    big.write_text("\n".join([lines[0], *rows]) + "\n")
    big_run = c.post(f"{P}/runs", data={"csrf": csrf, "acc_version": "2026", "input_mode": "files", "market_effects": "0", "output_format": "csv",
                                        "preflight_only": "on"},
                     files={"job_zip": _job(big.read_bytes())}).headers["location"].rsplit("/", 1)[-1]
    r = c.post(f"{P}/runs/{big_run}/edit", data={"csrf": csrf})
    assert r.status_code == 400 and "up to 500" in r.text and "Download the inputs" in r.text
    import time
    ctx.runner.work_once()
    ctx.runner.cleanup(now=time.time() + 8 * 86400)
    assert "Edit inputs and rerun" not in c.get(f"{P}/runs/{run_id}").text
    assert c.post(f"{P}/runs/{run_id}/edit", data={"csrf": csrf}).status_code == 400


def test_validate_inputs_from_the_editor(web, tmp_path):
    app, ctx = web
    _user(ctx, "v@example.com")
    _user(ctx, "w@example.com")
    c = _login(app, "v@example.com")
    r = c.post(f"{P}/edit", data={"csrf": _csrf(c, f"{P}/edit"), "mode": "upload", "name": "check me"},
               files={"job_zip": _job(make_measure_csv(tmp_path / "Measure.csv").read_bytes())})
    draft = r.headers["location"].split("draft=")[1]
    csrf = re.search(r'data-csrf="([^"]+)"', c.get(f"{P}/edit?draft={draft}").text).group(1)
    d = c.get(f"{P}/edit/api/{draft}").json()
    assert d["check"] is None and d["check_defaults"]["acc_version"] in d["check_defaults"]["acc_versions"]
    body = {"rev": d["rev"], "acc_version": "2026", "allocate_statewide": False}
    assert c.post(f"{P}/edit/api/{draft}/check", json=body).status_code == 400                       # no CSRF header
    assert c.post(f"{P}/edit/api/{draft}/check", json={**body, "acc_version": "1999"}, headers={"X-CSRF-Token": csrf}).status_code == 400
    assert c.post(f"{P}/edit/api/{draft}/check", json={**body, "rev": d["rev"] + 5}, headers={"X-CSRF-Token": csrf}).status_code == 409
    # a full run already waiting: the check still goes first
    full = _start_run(c, tmp_path)
    r = c.post(f"{P}/edit/api/{draft}/check", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    chk = r.json()
    assert chk["rev"] == d["rev"] and chk["status_url"].endswith(f"/runs/{chk['run']}/check")
    assert ctx.runner.position(chk["run"]) == 1 and ctx.runner.position(full) == 2
    st = c.get(chk["status_url"]).json()
    assert st["status"] == "queued" and st["report"] is None
    assert _login(app, "w@example.com").get(chk["status_url"]).status_code == 404                     # another user's check
    assert ctx.runner.work_once()
    assert ctx.db.one("SELECT status FROM runs WHERE id = ?", (full,))["status"] == "queued"
    st = c.get(chk["status_url"]).json()
    assert st["status"] == "finished", st
    rep = st["report"]
    assert rep["ok"] is True and rep["errors"] == 0 and isinstance(rep["issues"], list) and rep["stats"]
    row = ctx.db.one("SELECT * FROM runs WHERE id = ?", (chk["run"],))
    assert row["preflight_only"] == 1 and row["label"] == "Input check: check me" and "Edited inputs: check me" in row["inputs"]
    # the page remembers the latest check, with the revision it checked
    d2 = c.get(f"{P}/edit/api/{draft}").json()
    assert d2["check"]["run"] == chk["run"] and d2["check"]["rev"] == d["rev"]
    # cell errors are reported without queueing anything
    m = d2["tables"]["Measure"]
    m["rows"][0][m["columns"].index("NumUnits")] = ""
    saved = c.put(f"{P}/edit/api/{draft}", json={"rev": d2["rev"], "tables": {"Measure": m, "ProgramCost": d2["tables"]["ProgramCost"]}},
                  headers={"X-CSRF-Token": csrf}).json()
    n = ctx.db.one("SELECT COUNT(*) n FROM runs")["n"]
    r = c.post(f"{P}/edit/api/{draft}/check", json={**body, "rev": saved["rev"]}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400 and "NumUnits" in r.json()["error"] and "marked in red" in r.json()["error"]
    assert ctx.db.one("SELECT COUNT(*) n FROM runs")["n"] == n
    # the report names file rows; row_map turns them into grid rows (the blank grid row is not written to the file)
    m["rows"][0][m["columns"].index("NumUnits")] = "1"
    m["rows"][0][m["columns"].index("NTGRkWh")] = "2"
    m["rows"].insert(0, [""] * len(m["columns"]))
    saved = c.put(f"{P}/edit/api/{draft}", json={"rev": saved["rev"], "tables": {"Measure": m, "ProgramCost": d2["tables"]["ProgramCost"]}},
                  headers={"X-CSRF-Token": csrf}).json()
    chk = c.post(f"{P}/edit/api/{draft}/check", json={**body, "rev": saved["rev"]}, headers={"X-CSRF-Token": csrf}).json()
    assert chk["row_map"] == {"Measure": [1], "ProgramCost": []}
    assert ctx.runner.work_once()
    rep = c.get(chk["status_url"]).json()["report"]
    assert ["Measure", 0, "NTGRkWh"] in [cell for i in rep["issues"] if i["code"] == "outside_cet_range" for cell in i["cells"]]
    assert c.get(f"{P}/edit/api/{draft}").json()["check"]["row_map"] == chk["row_map"]


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


def test_docs_pages_render(web):
    app, ctx = web
    _user(ctx, "docs@example.com")
    c = _login(app, "docs@example.com")
    r = c.get(f"{P}/docs")
    assert r.status_code == 303 and r.headers["location"] == f"{P}/docs/README.md"
    for name in ["README.md", "USER_GUIDE.md", "INPUT_FORMATS.md", "INPUT_FIELDS.md", "OUTPUT_FIELDS.md", "CALCULATIONS.md"]:
        r = c.get(f"{P}/docs/{name}")
        assert r.status_code == 200, name
        assert f'href="{P}/docs/{name}" class="on"' in r.text, name
    assert c.get(f"{P}/docs/missing.md").status_code == 404


def test_value_list_pages_and_admin_changes(web):
    from recet import editor
    app, ctx = web
    _user(ctx, "boss@example.com", role="admin")
    _user(ctx, "pat@example.com")
    boss, pat = _login(app, "boss@example.com"), _login(app, "pat@example.com")

    index = pat.get(f"{P}/docs/value-lists")
    assert index.status_code == 200
    for field in editor.VALUE_LISTS:                      # every mapped dropdown field is listed, from the editor's own mapping
        assert f">{field}<" in index.text, field
    assert "IOU_AC_Territory" in index.text
    page = pat.get(f"{P}/docs/value-lists/sector")
    assert page.status_code == 200 and "<code>Commercial</code>" in page.text and "Add a code" not in page.text
    assert pat.get(f"{P}/docs/value-lists/nope").status_code == 404

    def post(c, **form):
        return c.post(f"{P}/docs/value-lists/sector", data={"csrf": _csrf(c, f"{P}/docs/value-lists/sector"), **form})

    assert post(pat, act="add", code="Zzz").status_code == 404                 # admins only (others get "not found", like the admin pages)
    assert "Add a code" in boss.get(f"{P}/docs/value-lists/sector").text
    assert post(boss, act="add", code="Commercial").status_code == 400         # already active
    assert post(boss, act="add", code="bad|code").status_code == 400

    r = post(boss, act="add", code="Zzz", description="Test sector", start_year="2027")
    assert r.status_code == 303
    assert "Zzz" in editor.active_codes("sector", 2028) and "Zzz" not in editor.active_codes("sector", 2026)
    assert editor.code_status("sector", "zzz", 2028) is None
    assert "Added by admin" in boss.get(f"{P}/docs/value-lists/sector").text

    assert post(boss, act="retire|Commercial", end_year="2026").status_code == 303
    assert "Commercial" not in editor.active_codes("sector", 2028)
    assert "retired by a ReCET admin after 2026" in editor.code_status("sector", "Commercial", 2028)
    assert post(boss, act="reinstate|Commercial").status_code == 303
    assert "Commercial" in editor.active_codes("sector", 2028)

    assert post(boss, act="reinstate|Com").status_code == 303                  # CET retired it; an admin brings it back
    assert editor.code_status("sector", "Com", 2028) is None
    assert post(boss, act="undo|Com").status_code == 303
    assert "retired" in editor.code_status("sector", "Com", 2028)
    page = boss.get(f"{P}/docs/value-lists/sector").text                     # the "Unretire a code" form offers the retired codes
    assert "Unretire a code" in page and '<option value="Com">' in page and '<option value="Commercial">' not in page
    assert post(boss, act="unretire", code="Com").status_code == 303
    assert editor.code_status("sector", "Com", 2028) is None
    assert post(boss, act="unretire", code="Com").status_code == 400          # no longer retired
    assert post(boss, act="undo|Com").status_code == 303

    assert post(boss, act="undo|Zzz").status_code == 303
    assert editor.code_status("sector", "Zzz", 2028) == "not in CET's value list"
    events = [r["event"] for r in ctx.db.all("SELECT event FROM audit WHERE event LIKE 'value list%'")]
    assert events.count("value list add") == 1 and "value list retire" in events

    post(boss, act="add", code="Kept")                                        # survives a restart: loaded from the database at start-up
    editor.set_value_list_changes({})
    create_app(ctx.s)
    assert "Kept" in editor.active_codes("sector", 2028)
    editor.set_value_list_changes({})


# ------------------------------------------------------------------------------------------------------------------------- the job zip
def test_csv_files_must_come_in_a_zip(web, tmp_path):
    """Measure.csv / ProgramCost.csv on their own are refused, on New run and on Edit inputs, with a message saying to zip them (as CET takes a job)."""
    app, ctx = web
    _user(ctx, "z@example.com")
    c = _login(app, "z@example.com")
    m = make_measure_csv(tmp_path / "Measure.csv").read_bytes()
    data = {"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files", "market_effects": "0", "output_format": "csv"}
    for files in ({"measures": ("Measure.csv", m, "text/csv")}, {"job_zip": ("Measure.csv", m, "text/csv")}):
        r = c.post(f"{P}/runs", data={**data, "csrf": _csrf(c)}, files=files)
        assert r.status_code == 400 and "in one .zip" in r.text
    r = c.post(f"{P}/edit", data={"csrf": _csrf(c, f"{P}/edit"), "mode": "upload", "name": ""}, files={"measures": ("Measure.csv", m, "text/csv")})
    assert "in one .zip" in r.text and ctx.db.one("SELECT COUNT(*) n FROM drafts")["n"] == 0
    r = c.post(f"{P}/runs", data={**data, "csrf": _csrf(c)}, files={"job_zip": ("job.zip", _zip({"ProgramCost.csv": b"x"}), "application/zip")})
    assert r.status_code == 400 and "Job zip: the zip has no Measure.csv" in r.text
    assert ctx.db.one("SELECT COUNT(*) n FROM runs")["n"] == 0
    assert not list(ctx.s.runs_dir.glob("*/inputs/*")) if ctx.s.runs_dir.exists() else True          # a refused upload leaves no folder behind


def test_a_zip_with_program_costs_runs_with_them(web, tmp_path):
    app, ctx = web
    _user(ctx, "pc@example.com")
    c = _login(app, "pc@example.com")
    pc = ("PrgID|PrgYear|ClaimYearQuarter|AdminCostsOverheadAndGA|AdminCostsOther|MarketingOutreach|DIActivity|DIInstallation|DIHardwareAndMaterials|"
          "DIRebateAndInspection|EMV|UserInputIncentive|OnBillFinancing|CostsRecoveredFromOtherSources|PA\nPRG1|2028|2028Q4|500|0|0|0|0|0|0|0|0|0|0|PGE\n")
    data = {"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files", "market_effects": "0", "output_format": "csv"}
    r = c.post(f"{P}/runs", data=data, files={"job_zip": _job(make_measure_csv(tmp_path / "Measure.csv").read_bytes(), pc.encode())})
    assert r.status_code == 303, r.text
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    row = ctx.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    assert "Measure.csv, ProgramCost.csv" in row["inputs"]
    assert sorted(f.name for f in (ctx.runner.folder(run_id) / "inputs").iterdir()) == ["measures.csv", "programcost.csv"]    # the zip itself is not kept
    assert ctx.runner.work_once()
    row = ctx.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    assert row["status"] == "finished", c.get(f"{P}/runs/{run_id}/log").json()["text"]
    from recet import outputs
    level = next(ctx.runner.folder(run_id).rglob("measure_level_*.*"))
    assert level.name.endswith(".csv.zip")                                   # the web app zips its CSV outputs
    assert outputs.read_table(level)["WeightedProgramCost"].sum() == pytest.approx(500.0)


def test_csv_outputs_are_zipped_and_downloads_stream_from_disk(web, tmp_path):
    app, ctx = web
    _user(ctx, "dl@example.com")
    c = _login(app, "dl@example.com")
    run_id = _start_run(c, tmp_path)
    assert ctx.runner.work_once()
    files = sorted(f.name for f in ctx.result_path(ctx.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))).iterdir())
    assert [f for f in files if f.startswith(("measure_level_", "portfolio_summary_"))] == ["measure_level_acc2026.csv.zip", "portfolio_summary_acc2026.csv.zip"]
    from recet import outputs
    folder = ctx.result_path(ctx.db.one("SELECT * FROM runs WHERE id = ?", (run_id,)))
    assert (folder / outputs.RESULTS_DIR / "measure_level_acc2026.parquet").exists()          # parquet first, kept for the Results tables
    assert outputs.RESULTS_DIR not in c.get(f"{P}/runs/{run_id}").text                          # but not listed for download
    assert c.get(f"{P}/runs/{run_id}/results").status_code == 200               # the results page reads the kept parquet
    r = c.get(f"{P}/runs/{run_id}/zip")
    assert r.status_code == 200 and r.headers["content-disposition"].endswith(f'recet_{run_id}.zip"')
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "measure_level_acc2026.csv.zip" in z.namelist() and "log.txt" in z.namelist()
    assert not [n for n in z.namelist() if n.startswith(outputs.RESULTS_DIR)]                  # the kept parquet is not downloaded again
    assert z.getinfo("measure_level_acc2026.csv.zip").compress_type == zipfile.ZIP_STORED       # already compressed: stored, not zipped twice
    assert z.getinfo("log.txt").compress_type == zipfile.ZIP_DEFLATED
    inner = zipfile.ZipFile(io.BytesIO(z.read("measure_level_acc2026.csv.zip")))
    assert inner.namelist() == ["measure_level_acc2026.csv"]
    assert "measures.csv" in zipfile.ZipFile(io.BytesIO(c.get(f"{P}/runs/{run_id}/inputs.zip").content)).namelist()
    assert not list((ctx.s.state_dir / "tmp").glob("download_*.zip"))          # the temporary download files are removed after sending
