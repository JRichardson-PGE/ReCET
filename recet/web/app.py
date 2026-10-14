"""The web app: FastAPI pages (Jinja templates) mounted under `Settings.prefix` (`/ReCET`).

Every page except the login and setup-link pages needs a signed-in user. A run belongs to the person who started it: other users get "not found", admins see
everything. Forms carry the session's CSRF token; the Edit inputs page sends it as the X-CSRF-Token header. The app is mounted (not proxied with a root_path),
so the same paths work behind the edge nginx (which passes /ReCET/... through unchanged) and on localhost.
"""
from __future__ import annotations

import io
import json
import math
import secrets
import shutil
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import fields as dc_fields
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from . import auth
from .config import Settings
from .db import Database
from .runner import Runner, write_config

HERE = Path(__file__).resolve().parent
SESSION_COOKIE = "recet_session"
LOGIN_CSRF_COOKIE = "recet_login_csrf"
DOCS_DIR = HERE.parent / "docs"
DOC_ORDER = ["README.md", "USER_GUIDE.md", "INPUT_FORMATS.md", "INPUT_FIELDS.md", "OUTPUT_FIELDS.md", "CALCULATIONS.md"]

# Upload fields of the New run form: field -> (RunConfig field, accepted extensions, label)
UPLOADS = {
    "workbook": ("workbook", (".xlsx", ".xlsm"), "Job workbook"),
    "measures": ("measures", (".csv", ".txt"), "Measure.csv"),
    "programcost": ("programcost", (".csv", ".txt"), "ProgramCost.csv"),
    "profiles_8760": ("profiles_8760_file", (".csv", ".parquet", ".xlsx"), "8,760 electric profiles"),
    "profiles_gas": ("profiles_gas_file", (".csv", ".parquet", ".xlsx"), "Gas monthly profiles"),
    "run_parameters": ("run_parameters_file", (".csv", ".parquet", ".xlsx"), "Run parameters"),
    "sw_factors": ("sw_factors_file", (".csv", ".parquet", ".xlsx"), "Statewide allocation factors"),
    "programs": ("programs_file", (".csv", ".parquet", ".xlsx"), "Program metadata"),
    "cet_outputs": ("cet_outputs", (".csv",), "CET outputs to compare against"),
}
TEMPLATE_FILES = {
    "workbook": ("template_job_workbook.xlsx", "Job workbook (Measure, ProgramCost and the optional sheets)"),
    "measure": ("template_Measure_header.csv", "Measure.csv header"),
    "run-parameters": ("template_run_parameters.csv", "Run parameters"),
    "sw-factors": ("template_sw_allocation_factors.csv", "Statewide allocation factors"),
    "programs": ("template_programs.csv", "Program metadata"),
    "profiles-8760": ("template_electric_8760_profiles.csv", "8,760 electric profiles"),
    "profiles-gas": ("template_gas_monthly_profiles.csv", "Gas monthly profiles"),
}
STATUS_TEXT = {"queued": "Queued", "running": "Running", "finished": "Finished", "failed": "Failed", "cancelled": "Stopped", "expired": "Expired"}


class NotSignedIn(Exception):
    pass


class Ctx:
    """What every request handler needs: settings, database, run queue, templates, and small helpers."""

    def __init__(self, s: Settings):
        self.s = s
        self.db = Database(s.db_path)
        self.runner = Runner(s, self.db)
        self.templates = Jinja2Templates(directory=str(HERE / "templates"))
        self.templates.env.globals.update(u=self.u, version=__version__, zip=zip, status_text=STATUS_TEXT, fmt_time=fmt_time, fmt_size=fmt_size)
        self._frames: dict[str, tuple[float, object]] = {}

    def u(self, path: str = "/") -> str:
        """A link inside the app: the prefix (/ReCET, exact casing) + path."""
        return self.s.prefix + (path if path.startswith("/") else "/" + path)

    def ip(self, request: Request) -> str:
        if self.s.trust_proxy:
            real = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            if real:
                return real[:64]
        return request.client.host if request.client else ""

    def user(self, request: Request):
        u, sess = auth.session_of(self.db, request.cookies.get(SESSION_COOKIE))
        request.state.user, request.state.session = u, sess
        return u

    def need_user(self, request: Request):
        u = self.user(request)
        if u is None:
            raise NotSignedIn()
        return u

    def need_admin(self, request: Request):
        u = self.need_user(request)
        if u["role"] != "admin":
            raise HTTPException(404)
        return u

    def check_csrf(self, request: Request, given: str | None) -> None:
        sess = getattr(request.state, "session", None)
        if sess is None or not auth.csrf_ok(sess["csrf"], given):
            raise HTTPException(400, "The form expired. Go back, reload the page and try again.")

    def render(self, request: Request, name: str, status_code: int = 200, **kw) -> HTMLResponse:
        sess = getattr(request.state, "session", None)
        kw.setdefault("user", getattr(request.state, "user", None))
        kw.setdefault("csrf", sess["csrf"] if sess else "")
        return self.templates.TemplateResponse(request, name, kw, status_code=status_code)

    def redirect(self, path: str) -> RedirectResponse:
        return RedirectResponse(self.u(path), status_code=303)

    def set_cookie(self, resp: Response, name: str, value: str, max_age: int | None) -> None:
        resp.set_cookie(name, value, max_age=max_age, path=self.s.prefix or "/", secure=self.s.secure_cookies, httponly=True, samesite="lax")

    # --- runs
    def run_for(self, request: Request, run_id: str):
        """The run if this user may see it (owner or admin), else 404."""
        u = self.need_user(request)
        row = self.db.one("SELECT r.*, u.email AS owner_email FROM runs r JOIN users u ON u.id = r.user_id WHERE r.id = ?", (run_id,))
        if row is None or (row["user_id"] != u["id"] and u["role"] != "admin"):
            raise HTTPException(404)
        return row

    def result_path(self, run, sub: str = "") -> Path | None:
        if not run["result_dir"]:
            return None
        base = (self.runner.folder(run["id"]) / run["result_dir"]).resolve()
        p = (base / sub).resolve() if sub else base
        if not p.is_dir() or not p.is_relative_to(base):
            return None
        return p

    def frame(self, path: Path):
        """A results table, cached by path and modification time (a few recent ones)."""
        from .. import outputs
        key, mtime = str(path), path.stat().st_mtime
        hit = self._frames.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        f = outputs.read_table(path)
        self._frames[key] = (mtime, f)
        while len(self._frames) > 4:
            self._frames.pop(next(iter(self._frames)))
        return f


def fmt_time(t) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else ""


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return str(n)


def _safe_next(prefix: str, nxt: str | None) -> str:
    """Only same-app paths after login (no open redirects)."""
    if nxt and nxt.startswith(prefix + "/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return prefix + "/"


async def save_upload(up: UploadFile, dest: Path, limit: int) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await up.read(1024 * 1024)
            if not chunk:
                break
            n += len(chunk)
            if n > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise ValueError(f"{up.filename} is larger than the {limit // (1024 * 1024)} MB limit")
            f.write(chunk)
    return n


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


# --------------------------------------------------------------------------------------------------------------------------------- the app
def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings.from_env()
    ctx = Ctx(s)

    @asynccontextmanager
    async def lifespan(_app):
        if s.start_workers:
            ctx.runner.start()
            from . import edit_api
            edit_api.warm_choices()                      # the profile catalog for the editor's dropdowns, read once in the background
        yield
        ctx.runner.stop()

    app = FastAPI(title="ReCET", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.exception_handler(NotSignedIn)
    async def _not_signed_in(request: Request, _exc):
        if request.url.path.startswith(s.prefix + "/edit/api") or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"error": "signed out: reload the page and sign in again"}, status_code=401)
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(ctx.u("/login") + f"?next={nxt}", status_code=303)

    @app.exception_handler(StarletteHTTPException)                      # also the router's own 404 / 405
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(s.prefix + "/edit/api"):
            return JSONResponse({"error": exc.detail if isinstance(exc.detail, str) else "error"}, status_code=exc.status_code)
        ctx.user(request)
        msg = exc.detail if exc.status_code != 404 else "Not found."
        return ctx.render(request, "error.html", status_code=exc.status_code, message=msg)

    # ----------------------------------------------------------------------------------------------------------------- sign-in
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = ""):
        if ctx.user(request):
            return RedirectResponse(_safe_next(s.prefix, next), status_code=303)
        token = secrets.token_urlsafe(24)
        resp = ctx.render(request, "login.html", next=next, login_csrf=token, error="")
        ctx.set_cookie(resp, LOGIN_CSRF_COOKIE, token, 3600)
        return resp

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        email, pw, nxt = str(form.get("email", "")), str(form.get("password", "")), str(form.get("next", ""))
        if not auth.csrf_ok(request.cookies.get(LOGIN_CSRF_COOKIE), str(form.get("login_csrf", ""))):
            return RedirectResponse(ctx.u("/login") + "?expired=1", status_code=303)
        ip = ctx.ip(request)
        token = secrets.token_urlsafe(24)
        if auth.login_blocked(ctx.db, email, ip):
            ctx.db.audit("login blocked", detail=email[:254], ip=ip)
            resp = ctx.render(request, "login.html", status_code=429, next=nxt, login_csrf=token,
                              error="Too many failed sign-ins. Wait 15 minutes and try again.")
        else:
            u = auth.check_login(ctx.db, email, pw, ip)
            if u is not None:
                ctx.db.audit("sign in", u, ip=ip)
                resp = RedirectResponse(_safe_next(s.prefix, nxt), status_code=303)
                ctx.set_cookie(resp, SESSION_COOKIE, auth.new_session(ctx.db, u["id"], s.session_days), s.session_days * 86400)
                resp.delete_cookie(LOGIN_CSRF_COOKIE, path=s.prefix or "/")
                return resp
            ctx.db.audit("sign-in failed", detail=email[:254], ip=ip)
            resp = ctx.render(request, "login.html", status_code=401, next=nxt, login_csrf=token, error="That email and password do not match an active account.")
        ctx.set_cookie(resp, LOGIN_CSRF_COOKIE, token, 3600)
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        u = ctx.user(request)
        form = await request.form()
        if u:
            ctx.check_csrf(request, str(form.get("csrf", "")))
            ctx.db.audit("sign out", u, ip=ctx.ip(request))
        auth.end_session(ctx.db, request.cookies.get(SESSION_COOKIE))
        resp = ctx.redirect("/login")
        resp.delete_cookie(SESSION_COOKIE, path=s.prefix or "/")
        return resp

    @app.get("/setup/{token}", response_class=HTMLResponse)
    async def setup_page(request: Request, token: str):
        ctx.user(request)
        u = auth.setup_link_user(ctx.db, token)
        return ctx.render(request, "setup.html", status_code=200 if u else 404, link_user=u, token=token, error="")

    @app.post("/setup/{token}")
    async def setup(request: Request, token: str):
        ctx.user(request)
        form = await request.form()
        u = auth.setup_link_user(ctx.db, token)
        if u is None:
            return ctx.render(request, "setup.html", status_code=404, link_user=None, token=token, error="")
        pw, again = str(form.get("password", "")), str(form.get("password2", ""))
        problem = auth.password_problem(pw, again)
        if problem:
            return ctx.render(request, "setup.html", status_code=400, link_user=u, token=token, error=problem)
        auth.use_setup_link(ctx.db, token, pw)
        ctx.db.audit("password set from link", u, ip=ctx.ip(request))
        resp = ctx.redirect("/")
        ctx.set_cookie(resp, SESSION_COOKIE, auth.new_session(ctx.db, u["id"], s.session_days), s.session_days * 86400)
        return resp

    @app.get("/account", response_class=HTMLResponse)
    async def account_page(request: Request, done: str = ""):
        ctx.need_user(request)
        return ctx.render(request, "account.html", error="", done=done)

    @app.post("/account")
    async def account(request: Request):
        u = ctx.need_user(request)
        form = await request.form()
        ctx.check_csrf(request, str(form.get("csrf", "")))
        if auth.check_login(ctx.db, u["email"], str(form.get("current", "")), ctx.ip(request)) is None:
            return ctx.render(request, "account.html", status_code=400, error="Your current password is not right.", done="")
        pw, again = str(form.get("password", "")), str(form.get("password2", ""))
        problem = auth.password_problem(pw, again)
        if problem:
            return ctx.render(request, "account.html", status_code=400, error=problem, done="")
        auth.set_password(ctx.db, u["id"], pw)          # ends every session, this one too: sign in again with a fresh one
        ctx.db.audit("password changed", u, ip=ctx.ip(request))
        resp = ctx.redirect("/account?done=1")
        ctx.set_cookie(resp, SESSION_COOKIE, auth.new_session(ctx.db, u["id"], s.session_days), s.session_days * 86400)
        return resp

    # ----------------------------------------------------------------------------------------------------------------- admin
    def setup_url(request: Request, token: str) -> str:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("host") or request.url.netloc
        return f"{proto}://{host}{ctx.u('/setup/' + token)}"

    def users_page(request: Request, **kw):
        users = ctx.db.all("SELECT u.*, (SELECT COUNT(*) FROM runs r WHERE r.user_id = u.id) AS n_runs FROM users u ORDER BY u.role, u.email")
        kw.setdefault("error", "")
        kw.setdefault("link", "")
        kw.setdefault("link_for", "")
        return ctx.render(request, "admin_users.html", users=users, link_hours=s.setup_link_hours, **kw)

    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users(request: Request):
        ctx.need_admin(request)
        return users_page(request)

    @app.post("/admin/users")
    async def admin_add_user(request: Request):
        admin = ctx.need_admin(request)
        form = await request.form()
        ctx.check_csrf(request, str(form.get("csrf", "")))
        email, name, role = str(form.get("email", "")).strip(), str(form.get("name", "")), str(form.get("role", "user"))
        try:
            uid = auth.create_user(ctx.db, email, name, role)
        except ValueError as exc:
            return users_page(request, error=str(exc).capitalize() + ".")
        token = auth.new_setup_link(ctx.db, uid, s.setup_link_hours)
        ctx.db.audit("user added", admin, detail=f"{email} ({role})", ip=ctx.ip(request))
        return users_page(request, link=setup_url(request, token), link_for=email)

    @app.post("/admin/users/{user_id}/{action}")
    async def admin_user_action(request: Request, user_id: int, action: str):
        admin = ctx.need_admin(request)
        form = await request.form()
        ctx.check_csrf(request, str(form.get("csrf", "")))
        target = auth.get_user(ctx.db, user_id)
        if target is None:
            raise HTTPException(404)
        admins = ctx.db.one("SELECT COUNT(*) n FROM users WHERE role = 'admin' AND disabled = 0")["n"]
        last_admin = target["role"] == "admin" and not target["disabled"] and admins <= 1
        if action == "disable":
            if target["id"] == admin["id"] or last_admin:
                return users_page(request, error="You cannot disable your own account or the last admin.")
            ctx.db.run("UPDATE users SET disabled = 1 WHERE id = ?", (user_id,))
            auth.end_sessions(ctx.db, user_id)
        elif action == "enable":
            ctx.db.run("UPDATE users SET disabled = 0 WHERE id = ?", (user_id,))
        elif action == "role":
            role = str(form.get("role", "user"))
            if role not in ("admin", "user"):
                raise HTTPException(400, "Unknown role.")
            if role == "user" and (target["id"] == admin["id"] or last_admin):
                return users_page(request, error="You cannot remove your own admin role or the last admin.")
            ctx.db.run("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        elif action == "reset":
            ctx.db.run("UPDATE users SET password_hash = NULL WHERE id = ?", (user_id,))
            auth.end_sessions(ctx.db, user_id)
            token = auth.new_setup_link(ctx.db, user_id, s.setup_link_hours)
            ctx.db.audit("password reset", admin, detail=target["email"], ip=ctx.ip(request))
            return users_page(request, link=setup_url(request, token), link_for=target["email"])
        else:
            raise HTTPException(404)
        ctx.db.audit(f"user {action}", admin, detail=target["email"], ip=ctx.ip(request))
        return ctx.redirect("/admin/users")

    @app.get("/admin/audit", response_class=HTMLResponse)
    async def admin_audit(request: Request):
        ctx.need_admin(request)
        rows = ctx.db.all("SELECT * FROM audit ORDER BY id DESC LIMIT 500")
        return ctx.render(request, "admin_audit.html", rows=rows)

    # ----------------------------------------------------------------------------------------------------------------- runs
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        ctx.need_user(request)
        return ctx.redirect("/runs")

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_list(request: Request, all: int = 0):
        u = ctx.need_user(request)
        everyone = bool(all) and u["role"] == "admin"
        rows = ctx.db.all("SELECT r.*, u.email AS owner_email FROM runs r JOIN users u ON u.id = r.user_id "
                          + ("" if everyone else "WHERE r.user_id = ? ") + "ORDER BY r.created_at DESC LIMIT 200", () if everyone else (u["id"],))
        return ctx.render(request, "runs.html", runs=rows, everyone=everyone, retention=s.retention_days)

    def new_run_page(request: Request, status_code=200, **kw):
        from .. import editor
        draft = kw.pop("draft", None)
        kw.setdefault("error", "")
        kw.setdefault("form", {})
        return ctx.render(request, "new_run.html", status_code=status_code, accs=s.acc_versions, uploads=UPLOADS, templates_=TEMPLATE_FILES,
                          max_mb=s.max_upload_mb, draft=draft, max_rows=editor.MAX_EDIT_ROWS, **kw)

    @app.get("/runs/new", response_class=HTMLResponse)
    async def new_run(request: Request, draft: str = ""):
        u = ctx.need_user(request)
        d = None
        if draft:
            d = ctx.db.one("SELECT * FROM drafts WHERE id = ? AND user_id = ?", (draft, u["id"]))
            if d is None:
                raise HTTPException(404)
        return new_run_page(request, draft=d)

    @app.post("/runs")
    async def create_run(request: Request):
        from .. import jobs
        u = ctx.need_user(request)
        form = await request.form(max_files=20, max_fields=200, max_part_size=s.max_upload_bytes + 1024)
        ctx.check_csrf(request, str(form.get("csrf", "")))
        values = {k: v for k, v in form.items() if not isinstance(v, UploadFile)}
        draft_id = str(form.get("draft", "")).strip()
        draft = ctx.db.one("SELECT * FROM drafts WHERE id = ? AND user_id = ?", (draft_id, u["id"])) if draft_id else None
        if draft_id and draft is None:
            raise HTTPException(404)

        def fail(msg: str):
            return new_run_page(request, status_code=400, error=msg, form=values, draft=draft)

        # --- options
        cfg: dict = {}
        acc = str(form.get("acc_version", ""))
        if acc not in s.acc_versions:
            return fail("Choose an ACC version.")
        cfg["acc_version"] = acc
        cmp_ = str(form.get("compare_with", "")).strip()
        if cmp_:
            if cmp_ not in s.acc_versions or cmp_ == acc:
                return fail("Compare with must be a different ACC version.")
            cfg["compare_with"] = cmp_
        by = str(form.get("base_year", "")).strip()
        if by:
            if not by.isdigit() or not 2000 <= int(by) <= 2100:
                return fail("First Year must be a year such as 2028 (or blank for the earliest claim year).")
            cfg["base_year"] = int(by)
        me = str(form.get("market_effects", "0"))
        if me not in ("0", "0.05"):
            return fail("Market effects must be 0% or 5%.")
        cfg["market_effects"] = float(me)
        for flag in ("components", "portfolio_8760", "allocate_statewide", "apply_exclude_from_ce", "preflight_only"):
            cfg[flag] = form.get(flag) == "on"
        cfg["include_societal"] = form.get("include_societal") == "on"
        cfg["emissions"] = form.get("emissions") == "on"
        if cfg["portfolio_8760"]:
            years = str(form.get("profile_years", "")).replace(",", " ").split()
            if not all(y.isdigit() and 2000 <= int(y) <= 2100 for y in years):
                return fail("Profile years must be years such as 2028 2029.")
            cfg["profile_years"] = [int(y) for y in years]
        fmt_ = str(form.get("output_format", "csv"))
        if fmt_ not in ("csv", "xlsx", "parquet"):
            return fail("Unknown output format.")
        cfg["output_format"] = fmt_
        label = str(form.get("label", "")).strip()[:120]

        # --- inputs
        mode = "draft" if draft else str(form.get("input_mode", "workbook"))
        ups = {k: form.get(k) for k in UPLOADS}
        ups = {k: v for k, v in ups.items() if isinstance(v, UploadFile) and v.filename}
        for k, v in ups.items():
            if Path(v.filename).suffix.lower() not in UPLOADS[k][1]:
                return fail(f"{UPLOADS[k][2]}: use a {' or '.join(UPLOADS[k][1])} file ({v.filename} was given).")
        if mode == "workbook":
            ups.pop("measures", None)
            ups.pop("programcost", None)
            if "workbook" not in ups:
                return fail("Choose a job workbook (or switch to CET files).")
        elif mode == "files":
            ups.pop("workbook", None)
            if "measures" not in ups:
                return fail("Choose a Measure.csv (or switch to a job workbook).")
        else:
            ups.pop("workbook", None)
            ups.pop("measures", None)
            ups.pop("programcost", None)

        run_id = new_run_id()
        folder = ctx.runner.folder(run_id)
        names = []
        try:
            for k, up in ups.items():
                dest = folder / "inputs" / f"{k}{Path(up.filename).suffix.lower()}"
                await save_upload(up, dest, s.max_upload_bytes)
                cfg[UPLOADS[k][0]] = str(dest)
                names.append(f"{UPLOADS[k][2]}: {Path(up.filename).name}")
            if draft is not None:
                from . import edit_api
                problem, updates = edit_api.prepare_draft_run(ctx, draft, folder, {f: cfg.get(f) for f in ("run_parameters_file", "sw_factors_file",
                                                                                                              "profiles_gas_file", "programs_file",
                                                                                                              "profiles_8760_file")})
                if problem:
                    shutil.rmtree(folder, ignore_errors=True)
                    return fail(problem)
                cfg.update(updates)
                names.insert(0, f"Edited inputs: {draft['name']}")
        except Exception as exc:
            shutil.rmtree(folder, ignore_errors=True)
            return fail(str(exc))

        known = {f.name for f in dc_fields(jobs.RunConfig)}
        full = {k: v for k, v in {**s.run_defaults, **cfg}.items() if k in known}
        full.update(out_dir=str(folder / "out"), run_name="run")
        folder.mkdir(parents=True, exist_ok=True)
        write_config(folder, jobs.RunConfig.from_dict(full).__dict__)
        (folder / "log.txt").write_text(f"recet {__version__}  -  {'preflight' if cfg['preflight_only'] else 'run'}  -  ACC {acc}"
                                        + (f" vs {cmp_}" if cmp_ else "") + "\n" + "\n".join(names) + "\n\n", encoding="utf-8")
        inputs_text = "; ".join(names)
        ctx.db.run("INSERT INTO runs (id, user_id, label, status, acc_version, compare_with, preflight_only, inputs, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (run_id, u["id"], label, "queued", acc, cmp_ or None, int(cfg["preflight_only"]), inputs_text, time.time()))
        ctx.db.audit("run queued", u, detail=f"{run_id}: ACC {acc}{' vs ' + cmp_ if cmp_ else ''}; {inputs_text}", ip=ctx.ip(request))
        return ctx.redirect(f"/runs/{run_id}")

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: str):
        run = ctx.run_for(request, run_id)
        return ctx.render(request, "run.html", run=run, position=ctx.runner.position(run_id), files=_files(run), retention=s.retention_days)

    def _files(run) -> list[tuple[str, int]]:
        p = ctx.result_path(run)
        if p is None:
            return []
        out = []
        for f in sorted(p.rglob("*")):
            if f.is_file() and "_prepared_inputs" not in f.parts and not f.name.startswith("_config"):
                out.append((str(f.relative_to(p)), f.stat().st_size))
        return out

    @app.get("/runs/{run_id}/log")
    async def run_log(request: Request, run_id: str, offset: int = 0):
        run = ctx.run_for(request, run_id)
        p = ctx.runner.folder(run_id) / "log.txt"
        text, end = "", offset
        if p.exists():
            with open(p, "rb") as f:
                f.seek(max(0, offset))
                data = f.read(256 * 1024)
            text, end = data.decode("utf-8", errors="replace"), max(0, offset) + len(data)
        return JSONResponse({"text": text, "offset": end, "status": run["status"], "status_text": STATUS_TEXT.get(run["status"], run["status"]),
                             "position": ctx.runner.position(run_id), "message": run["message"], "has_results": bool(run["result_dir"])})

    @app.post("/runs/{run_id}/cancel")
    async def run_cancel(request: Request, run_id: str):
        run = ctx.run_for(request, run_id)
        form = await request.form()
        ctx.check_csrf(request, str(form.get("csrf", "")))
        ctx.runner.cancel(run["id"])
        ctx.db.audit("run stopped", request.state.user, detail=run_id, ip=ctx.ip(request))
        return ctx.redirect(f"/runs/{run_id}")

    @app.post("/runs/{run_id}/delete")
    async def run_delete(request: Request, run_id: str):
        run = ctx.run_for(request, run_id)
        form = await request.form()
        ctx.check_csrf(request, str(form.get("csrf", "")))
        if run["status"] in ("queued", "running"):
            raise HTTPException(400, "Stop the run before deleting it.")
        shutil.rmtree(ctx.runner.folder(run_id), ignore_errors=True)
        ctx.db.run("UPDATE runs SET status = 'expired', result_dir = NULL, message = 'deleted by its owner' WHERE id = ?", (run_id,))
        ctx.db.audit("run deleted", request.state.user, detail=run_id, ip=ctx.ip(request))
        return ctx.redirect("/runs")

    @app.get("/runs/{run_id}/files/{path:path}")
    async def run_file(request: Request, run_id: str, path: str):
        run = ctx.run_for(request, run_id)
        base = ctx.result_path(run)
        if base is None:
            raise HTTPException(404)
        f = (base / path).resolve()
        if not f.is_file() or not f.is_relative_to(base):
            raise HTTPException(404)
        return FileResponse(f, filename=f.name)

    @app.get("/runs/{run_id}/zip")
    async def run_zip(request: Request, run_id: str):
        run = ctx.run_for(request, run_id)
        base = ctx.result_path(run)
        if base is None:
            raise HTTPException(404)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, _n in _files(run):
                z.write(base / rel, rel)
            log = ctx.runner.folder(run_id) / "log.txt"
            if log.exists():
                z.write(log, "log.txt")
        buf.seek(0)
        name = f"recet_{run_id}.zip"
        return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ----------------------------------------------------------------------------------------------------------------- results
    def user_views(uid: int) -> dict[str, list[str]]:
        from .. import results_view as rv
        views = {rv.DEFAULT_VIEW_NAME: list(rv.DEFAULT_VIEW)}
        for r in ctx.db.all("SELECT name, keys FROM views WHERE user_id = ? ORDER BY name", (uid,)):
            try:
                views[r["name"]] = [str(k) for k in json.loads(r["keys"])]
            except ValueError:
                pass
        return views

    @app.get("/runs/{run_id}/results", response_class=HTMLResponse)
    async def run_results(request: Request, run_id: str, level: str = "portfolio", pa: str = "", view: str = "", sub: str = "", columns: str = ""):
        from .. import results_view as rv
        run = ctx.run_for(request, run_id)
        u = request.state.user
        base = ctx.result_path(run)
        if base is None:
            raise HTTPException(404, "This run has no results.")
        folder = ctx.result_path(run, sub) if sub else base
        if folder is None:
            raise HTTPException(404)
        subruns = sorted(str(p.parent.relative_to(base)) for p in base.glob("*/measure_level_*.*"))
        measure_files = sorted(folder.glob("measure_level_*.*"))
        comparison = sorted(folder.glob("comparison_summary_*.*"))
        kw = dict(run=run, sub=sub, subruns=subruns, levels=rv.LEVELS, level=level, plain=None, plain_name="")
        if not measure_files:
            plain_file = comparison[0] if comparison else next(iter(sorted(folder.glob("portfolio_summary_*.*"))), None)
            if plain_file is None:
                return ctx.render(request, "results.html", **kw, note="This run wrote no results tables (a preflight-only run, or it stopped early).",
                                  table=None)
            df = ctx.frame(plain_file)
            kw.update(plain=df, plain_name=plain_file.name)
            return ctx.render(request, "results.html", **kw, note="", table=None)
        frame = ctx.frame(measure_files[0])
        pas = rv.available_pas(frame)
        pa = pa if pa in pas else rv.ALL_PAS
        views = user_views(u["id"])
        cat = rv.catalog(frame)
        if columns:                                          # an unsaved choice from the column picker
            keys = [k for k in columns.split(",") if k in cat]
            view = ""
        else:
            view = view if view in views else rv.DEFAULT_VIEW_NAME
            keys = views[view]
        levels_ok = {k for k, _ in rv.LEVELS}
        level = level if level in levels_ok else "portfolio"
        note = ""
        table = None
        if level == "measure" and not rv.measure_level_allowed(frame, pa):
            note = f"The measure level is off above {rv.MAX_MEASURE_ROWS:,} measures: download the measure-level file instead, or pick one PA."
        else:
            table = rv.table(frame, level, pa, keys)
        shown = [cat[k] for k in keys if k in cat]
        kw.update(level=level, pa=pa, pas=pas, views=views, view=view, keys=keys, catalog=cat, shown=shown, file_name=measure_files[0].name,
                  n_measures=len(frame))
        return ctx.render(request, "results.html", **kw, note=note, table=table, rv=rv)

    @app.get("/runs/{run_id}/results.csv")
    async def run_results_csv(request: Request, run_id: str, level: str = "portfolio", pa: str = "", view: str = "", sub: str = "", columns: str = ""):
        from .. import results_view as rv
        run = ctx.run_for(request, run_id)
        base = ctx.result_path(run, sub) if sub else ctx.result_path(run)
        files = sorted(base.glob("measure_level_*.*")) if base else []
        if not files:
            raise HTTPException(404)
        frame = ctx.frame(files[0])
        cat = rv.catalog(frame)
        views = user_views(request.state.user["id"])
        keys = [k for k in columns.split(",") if k in cat] if columns else views.get(view, views[rv.DEFAULT_VIEW_NAME])
        pa = pa if pa in rv.available_pas(frame) else rv.ALL_PAS
        level = level if level in {k for k, _ in rv.LEVELS} else "portfolio"
        if level == "measure" and not rv.measure_level_allowed(frame, pa):
            raise HTTPException(400, "The measure level is too large to show: download the measure-level file instead.")
        t = rv.table(frame, level, pa, keys).rename(columns={k: c.label for k, c in cat.items()})
        name = f"recet_{run_id}_{level}.csv"
        return Response(t.to_csv(index=False), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/views")
    async def save_view(request: Request):
        u = ctx.need_user(request)
        form = await request.form()
        ctx.check_csrf(request, str(form.get("csrf", "")))
        from .. import results_view as rv
        name = str(form.get("name", "")).strip()[:60]
        back = str(form.get("back", ""))
        keys = [str(k) for k in form.getlist("keys")][:200]
        if form.get("delete"):
            ctx.db.run("DELETE FROM views WHERE user_id = ? AND name = ?", (u["id"], name))
            name = rv.DEFAULT_VIEW_NAME
        elif not name or name == rv.DEFAULT_VIEW_NAME or not keys:
            raise HTTPException(400, "Give the view a name (not 'Default') and pick at least one column.")
        else:
            ctx.db.run("INSERT OR REPLACE INTO views (user_id, name, keys) VALUES (?,?,?)", (u["id"], name, json.dumps(keys)))
        target = _safe_next(s.prefix, back)
        sep = "&" if "?" in target else "?"
        from urllib.parse import quote
        return RedirectResponse(f"{target}{sep}view={quote(name)}", status_code=303)

    # ----------------------------------------------------------------------------------------------------------------- templates and docs
    @app.get("/templates/{kind}")
    async def template_file(request: Request, kind: str):
        from .. import templates
        ctx.need_user(request)
        if kind not in TEMPLATE_FILES:
            raise HTTPException(404)
        acc = s.acc_versions[-1]
        out = s.state_dir / "templates" / acc / TEMPLATE_FILES[kind][0]
        if not out.exists():
            tmp = out.with_name(f".{secrets.token_hex(4)}{out.suffix}")
            templates.write_template(kind, tmp, acc)
            tmp.replace(out)
        return FileResponse(out, filename=out.name)

    @app.get("/docs", response_class=HTMLResponse)
    async def docs_index(request: Request):
        ctx.need_user(request)
        docs = [d for d in DOC_ORDER if (DOCS_DIR / d).exists()] + sorted(p.name for p in DOCS_DIR.glob("*.md") if p.name not in DOC_ORDER)
        return ctx.render(request, "docs.html", docs=docs, html="", title="Documentation", name="")

    @app.get("/docs/{name}", response_class=HTMLResponse)
    async def doc(request: Request, name: str):
        import markdown
        ctx.need_user(request)
        p = (DOCS_DIR / name).resolve()
        if p.suffix != ".md" or not p.is_file() or p.parent != DOCS_DIR.resolve():
            raise HTTPException(404)
        html = markdown.markdown(p.read_text(encoding="utf-8"), extensions=["tables", "fenced_code", "toc", "sane_lists"])
        docs = [d for d in DOC_ORDER if (DOCS_DIR / d).exists()] + sorted(x.name for x in DOCS_DIR.glob("*.md") if x.name not in DOC_ORDER)
        return ctx.render(request, "docs.html", docs=docs, html=html, title=name.removesuffix(".md").replace("_", " ").title(), name=name)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz():
        ctx.db.one("SELECT 1")
        return "ok"

    from . import edit_api
    edit_api.register(app, ctx)

    # ----------------------------------------------------------------------------------------------------------------- the outer app
    outer = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    outer.state.ctx = ctx

    @outer.middleware("http")
    async def headers(request: Request, call_next):
        path = request.url.path
        low, pre = path.lower(), s.prefix.lower()
        if s.prefix and (low == pre or low.startswith(pre + "/")) and not path.startswith(s.prefix):
            fixed = s.prefix + path[len(s.prefix):]           # /recet/runs -> /ReCET/runs: cookies are scoped to the exact casing
            q = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(fixed + q, status_code=301)
        if s.prefix and path == s.prefix:                     # /ReCET -> /ReCET/ as a relative redirect (the mount's own would name http://host)
            q = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(s.prefix + "/" + q, status_code=307)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                           "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if not path.startswith(s.prefix + "/static/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @outer.get("/healthz", response_class=PlainTextResponse)
    async def outer_health():
        return "ok"

    if s.prefix:
        @outer.get("/")
        async def outer_root():
            return RedirectResponse(s.prefix + "/", status_code=307)
        outer.mount(s.prefix, app)
    else:
        outer.mount("", app)
    return outer


def _nan_to_none(v):
    return None if isinstance(v, float) and math.isnan(v) else v
