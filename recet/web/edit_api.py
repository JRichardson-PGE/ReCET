"""The Edit inputs page: a small job's Measure and ProgramCost tables edited in the browser, saved as a draft, then run from the New run page.

A draft belongs to one user and lives in `<state>/drafts/<id>/`:

    draft.json   {"rev": n, "tables": {"Measure": {"columns": [...], "rows": [[...], ...]}, "ProgramCost": {...}}}
    base.xlsx    the job workbook the draft came from, if any: its other sheets (RunParameters, Programs ...) still apply when the draft is run
    rerun.json   for a draft made from a run ("Edit inputs and rerun"): {"run": id, "settings": New run form values, "supplied": {RunConfig field: file name}}
    supplied/    that run's supplied files (profiles, run parameters ...), offered again on the New run page

The editing rules are the desktop window's (`recet.editor`): the same column order, dropdown values, 500-measure limit and cell checks. Every save re-checks the
tables and returns the problems; a draft with an error (a problem that would stop the run) cannot be started. Drafts untouched for `Settings.draft_days` are
deleted by the run queue's cleanup.

Pages (forms carry the CSRF token): GET /edit (list, or the grid with ?draft=), POST /edit (new draft), POST /edit/{id}/delete, GET /edit/{id}/download.zip|.xlsx.
A finished (or failed) run's inputs become a draft with POST /runs/{id}/edit (in app.py, through `draft_from_run`).
JSON (X-CSRF-Token header on writes): GET /edit/api/{id}, PUT /edit/api/{id}, GET /edit/api/end-uses?iou=&sector=.
"""
from __future__ import annotations

import io
import json
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import pandas as pd
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .. import editor

TABLES = ("Measure", "ProgramCost")
MAX_DRAFTS_PER_USER = 50
MAX_BODY_BYTES = 8 * 1024 * 1024        # a 500-measure job is about 0.5 MB of JSON
MAX_CELL_CHARS = 2000
MAX_COLUMNS = 300
MAX_PROGRAMCOST_ROWS = 5000
MAX_ISSUES_SHOWN = 5

_choices: editor.ChoiceSource | None = None
_choices_lock = threading.Lock()


def choices() -> editor.ChoiceSource:
    """Dropdown values and profile lookups, shared by every user; the profile catalog is read once (a few seconds), on first use or at start-up."""
    global _choices
    with _choices_lock:
        if _choices is None:
            c = editor.ChoiceSource()
            c.load_profiles()
            _choices = c
        return _choices


def warm_choices() -> None:
    threading.Thread(target=choices, name="recet-edit-choices", daemon=True).start()


# ------------------------------------------------------------------------------------------------------------------------- draft storage
def _columns(t: str) -> list[str]:
    return editor.MEASURE_COLUMNS if t == "Measure" else editor.PROGRAMCOST_COLUMNS


def _folder(ctx, draft_id: str) -> Path:
    return ctx.s.drafts_dir / draft_id


def _frame(table: dict | None, t: str) -> pd.DataFrame:
    if not table:
        return editor.conform(None, _columns(t))
    df = pd.DataFrame(table.get("rows", []), columns=table.get("columns", []), dtype=str)
    return editor.conform(df, _columns(t))


def _table(df: pd.DataFrame) -> dict:
    df = df.fillna("").astype(str)
    return {"columns": list(df.columns), "rows": df.values.tolist()}


def load_draft(ctx, draft_id: str) -> tuple[dict, pd.DataFrame, pd.DataFrame, str | None]:
    """(stored json, Measure frame, ProgramCost frame, base workbook path or None)."""
    folder = _folder(ctx, draft_id)
    data = json.loads((folder / "draft.json").read_text(encoding="utf-8"))
    base = folder / "base.xlsx"
    return data, _frame(data["tables"].get("Measure"), "Measure"), _frame(data["tables"].get("ProgramCost"), "ProgramCost"), (str(base) if base.exists() else None)


def _write(folder: Path, rev: int, m: pd.DataFrame, pc: pd.DataFrame) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "draft.json.tmp"
    tmp.write_text(json.dumps({"rev": rev, "tables": {"Measure": _table(m), "ProgramCost": _table(pc)}}), encoding="utf-8")
    tmp.replace(folder / "draft.json")


def _draft_for(ctx, request: Request, draft_id: str):
    u = ctx.need_user(request)
    row = ctx.db.one("SELECT * FROM drafts WHERE id = ? AND user_id = ?", (draft_id, u["id"]))
    if row is None or not (_folder(ctx, draft_id) / "draft.json").exists():
        raise HTTPException(404)
    return u, row


def _issues(m: pd.DataFrame, pc: pd.DataFrame) -> list[dict]:
    return [{"table": i.table, "row": i.row, "column": i.column, "severity": i.severity, "message": i.message}
            for i in editor.check_tables(m, pc, choices())]


def _years(m: pd.DataFrame) -> list[int]:
    return sorted({y for y in (editor.claim_year(v) for v in m.get("ClaimYearQuarter", [])) if y})


def _column_choices(m: pd.DataFrame, cols: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
    c, years = choices(), _years(m)
    out: dict[str, dict[str, list[str]]] = {}
    for t in TABLES:
        out[t] = {}
        for col in cols[t]:
            vals = c.column_choices(t, col, years)
            if vals:
                out[t][col] = [""] + list(vals)
    return out


def _parse_tables(body: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The two tables from a save request, checked for shape and size (not for content: that is check_tables's job)."""
    tables = body.get("tables")
    if not isinstance(tables, dict):
        raise HTTPException(400, "tables missing")
    frames = {}
    for t in TABLES:
        tab = tables.get(t) or {"columns": _columns(t), "rows": []}
        cols, rows = tab.get("columns"), tab.get("rows")
        if not isinstance(cols, list) or not isinstance(rows, list) or not 0 < len(cols) <= MAX_COLUMNS:
            raise HTTPException(400, f"{t}: columns and rows are required")
        if any(not isinstance(c, str) or not c.strip() or len(c) > 100 for c in cols) or len(set(cols)) != len(cols):
            raise HTTPException(400, f"{t}: bad column names")
        limit = editor.MAX_EDIT_ROWS if t == "Measure" else MAX_PROGRAMCOST_ROWS
        if len(rows) > limit:
            raise HTTPException(400, f"{t}: the editor is for jobs of up to {limit:,} rows")
        clean = []
        for r in rows:
            if not isinstance(r, list) or len(r) != len(cols):
                raise HTTPException(400, f"{t}: every row needs one value per column")
            vals = ["" if v is None else str(v) for v in r]
            if any(len(v) > MAX_CELL_CHARS for v in vals):
                raise HTTPException(400, f"{t}: a cell is longer than {MAX_CELL_CHARS} characters")
            clean.append(vals)
        frames[t] = editor.conform(pd.DataFrame(clean, columns=cols, dtype=str), _columns(t))
    return frames["Measure"], frames["ProgramCost"]


# ------------------------------------------------------------------------------------------------------------------------- run support
# RunConfig fields of the supplied files a run can carry over to a rerun (the New run page's optional uploads)
CARRY_FIELDS = ("profiles_8760_file", "profiles_gas_file", "run_parameters_file", "sw_factors_file", "programs_file", "cet_outputs")
SETTING_FLAGS = ("components", "portfolio_8760", "allocate_statewide", "apply_exclude_from_ce", "preflight_only", "include_societal", "emissions")


def load_rerun(ctx, draft_id: str) -> dict | None:
    """What a draft made from a run carries: {"run", "settings", "supplied"}, or None for an ordinary draft."""
    p = _folder(ctx, draft_id) / "rerun.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def supplied_path(ctx, draft_id: str, name: str) -> Path | None:
    p = (_folder(ctx, draft_id) / "supplied" / name).resolve()
    return p if p.is_file() and p.is_relative_to((_folder(ctx, draft_id) / "supplied").resolve()) else None


def _run_settings(cfg: dict, label: str) -> dict:
    """A run's options as New run form values, so a rerun starts from the same settings."""
    out = {"acc_version": str(cfg.get("acc_version") or ""), "compare_with": str(cfg.get("compare_with") or ""),
           "base_year": str(cfg.get("base_year") or ""), "market_effects": "0.05" if float(cfg.get("market_effects") or 0) > 0 else "0",
           "output_format": str(cfg.get("output_format") or "csv"), "label": (f"{label} (edited)" if label else "")[:120],
           "profile_years": " ".join(str(y) for y in cfg.get("profile_years") or [])}
    for f in SETTING_FLAGS:
        if cfg.get(f):
            out[f] = "on"
    return out


def draft_from_run(ctx, run, user) -> str:
    """A new draft (owned by `user`) holding the Measure and ProgramCost tables a run used, its job workbook (whose other sheets still apply), its supplied
    files and its settings. Raises editor.EditorError when the run's files are gone or the job is too big for the editor."""
    run_folder = ctx.runner.folder(run["id"])
    inputs = (run_folder / "inputs").resolve()
    cfg_path = run_folder / "config.json"
    if not cfg_path.exists():
        raise editor.EditorError("this run's files have been deleted, so its inputs cannot be edited")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    def own(field: str) -> Path | None:
        """The run's file for a field, only if it is one of the run's own inputs (not a server default)."""
        v = cfg.get(field)
        if not v:
            return None
        p = Path(v).resolve()
        return p if p.is_file() and p.is_relative_to(inputs) else None

    n = ctx.db.one("SELECT COUNT(*) n FROM drafts WHERE user_id = ?", (user["id"],))["n"]
    if n >= MAX_DRAFTS_PER_USER:
        raise editor.EditorError(f"you have {n} drafts; delete some on the Edit inputs page first (the limit is {MAX_DRAFTS_PER_USER})")
    wb, meas, pcost = own("workbook"), own("measures"), own("programcost")
    if wb is None and meas is None:
        raise editor.EditorError("this run's input files are missing")
    job = editor.load_tables(str(meas) if meas else None, str(pcost) if pcost else None, str(wb) if wb else None)
    draft_id = secrets.token_hex(8)
    folder = _folder(ctx, draft_id)
    try:
        folder.mkdir(parents=True)
        if wb is not None:
            shutil.copyfile(wb, folder / "base.xlsx")
        supplied = {}
        for f in CARRY_FIELDS:
            p = own(f)
            if p is not None:
                (folder / "supplied").mkdir(exist_ok=True)
                name = f"{f}{p.suffix.lower()}"
                shutil.copyfile(p, folder / "supplied" / name)
                supplied[f] = name
        label = run["label"] or ""
        (folder / "rerun.json").write_text(json.dumps({"run": run["id"], "settings": _run_settings(cfg, label), "supplied": supplied}), encoding="utf-8")
        _write(folder, 1, job.measures, job.programcost)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    title = label or f"Run {run['id']}"
    now = time.time()
    ctx.db.run("INSERT INTO drafts (id, user_id, name, source, created_at, updated_at) VALUES (?,?,?,?,?,?)",
               (draft_id, user["id"], f"{title} (edited)"[:120], f"inputs of run {title}"[:500], now, now))
    return draft_id


def prepare_draft_run(ctx, draft, run_folder: Path, given: dict) -> tuple[str | None, dict]:
    """(problem, RunConfig updates) for running a draft. A problem (the cells that would stop the run) means no run."""
    _data, m, pc, base = load_draft(ctx, draft["id"])
    errors = [i for i in editor.check_tables(m, pc, choices()) if i.severity == "error"]
    if errors:
        first = "; ".join(f"{i.table} row {i.row + 1}, {i.column}: {i.message}" for i in errors[:MAX_ISSUES_SHOWN])
        more = f" (and {len(errors) - MAX_ISSUES_SHOWN} more)" if len(errors) > MAX_ISSUES_SHOWN else ""
        return (f"The edited inputs have {len(errors)} problem(s) that would stop the run: {first}{more}. Fix them on the Edit inputs page.", {})
    try:
        updates, _folder_ = editor.prepare_run_inputs(m, pc, run_folder / "inputs", base, given)
    except editor.EditorError as exc:
        return str(exc), {}
    return None, updates


# ------------------------------------------------------------------------------------------------------------------------- routes
def register(app, ctx) -> None:
    s = ctx.s

    def list_page(request: Request, status_code: int = 200, error: str = ""):
        u = ctx.need_user(request)
        drafts = ctx.db.all("SELECT * FROM drafts WHERE user_id = ? ORDER BY updated_at DESC", (u["id"],))
        return ctx.render(request, "edit_list.html", status_code=status_code, drafts=drafts, error=error, max_rows=editor.MAX_EDIT_ROWS,
                          max_mb=s.max_upload_mb, draft_days=s.draft_days)

    @app.get("/edit", response_class=HTMLResponse)
    async def edit_page(request: Request, draft: str = ""):
        if not draft:
            return list_page(request)
        _u, row = _draft_for(ctx, request, draft)
        warm_choices()
        return ctx.render(request, "edit.html", draft=row, max_rows=editor.MAX_EDIT_ROWS)

    @app.post("/edit")
    async def create_draft(request: Request):
        u = ctx.need_user(request)
        form = await request.form(max_files=3, max_fields=20, max_part_size=s.max_upload_bytes + 1024)
        ctx.check_csrf(request, str(form.get("csrf", "")))
        n = ctx.db.one("SELECT COUNT(*) n FROM drafts WHERE user_id = ?", (u["id"],))["n"]
        if n >= MAX_DRAFTS_PER_USER:
            return list_page(request, 400, f"You have {n} drafts; delete some first (the limit is {MAX_DRAFTS_PER_USER}).")
        mode = str(form.get("mode", "new"))
        name = str(form.get("name", "")).strip()[:120]
        draft_id = secrets.token_hex(8)
        folder = _folder(ctx, draft_id)
        try:
            if mode == "new":
                job = editor.new_job()
                source = "new job (template example rows)"
            elif mode == "upload":
                job, source = await _load_upload(form, folder, s.max_upload_bytes)
            else:
                raise editor.EditorError("unknown way to start a draft")
            _write(folder, 1, job.measures, job.programcost)
        except (editor.EditorError, ValueError) as exc:
            shutil.rmtree(folder, ignore_errors=True)
            return list_page(request, 400, str(exc))
        except Exception as exc:                                    # an unreadable upload: say so, keep the page
            shutil.rmtree(folder, ignore_errors=True)
            return list_page(request, 400, f"Could not read that file: {exc}")
        now = time.time()
        ctx.db.run("INSERT INTO drafts (id, user_id, name, source, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                   (draft_id, u["id"], name or source[:120], source[:500], now, now))
        ctx.db.audit("draft created", u, detail=f"{draft_id}: {source}", ip=ctx.ip(request))
        return ctx.redirect(f"/edit?draft={draft_id}")

    @app.post("/edit/{draft_id}/delete")
    async def delete_draft(request: Request, draft_id: str):
        u, row = _draft_for(ctx, request, draft_id)
        form = await request.form()
        ctx.check_csrf(request, str(form.get("csrf", "")))
        shutil.rmtree(_folder(ctx, draft_id), ignore_errors=True)
        ctx.db.run("DELETE FROM drafts WHERE id = ?", (draft_id,))
        ctx.db.audit("draft deleted", u, detail=f"{draft_id}: {row['name']}", ip=ctx.ip(request))
        return ctx.redirect("/edit")

    @app.post("/edit/{draft_id}/rename")
    async def rename_draft(request: Request, draft_id: str):
        _u, _row = _draft_for(ctx, request, draft_id)
        form = await request.form()
        ctx.check_csrf(request, str(form.get("csrf", "")))
        name = str(form.get("name", "")).strip()[:120]
        if name:
            ctx.db.run("UPDATE drafts SET name = ? WHERE id = ?", (name, draft_id))
        return ctx.redirect(f"/edit?draft={draft_id}")

    @app.get("/edit/{draft_id}/download.{kind}")
    def download_draft(request: Request, draft_id: str, kind: str):
        _u, row = _draft_for(ctx, request, draft_id)
        _data, m, pc, base = load_draft(ctx, draft_id)
        stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in row["name"])[:60] or "draft"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                if kind == "zip":
                    files = editor.write_cet_files(m, pc, tmp)
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                        for f in files.values():
                            if f is not None:
                                z.write(f, f.name)
                    data, media = buf.getvalue(), "application/zip"
                elif kind == "xlsx":
                    data = editor.write_workbook(m, pc, Path(tmp) / "job.xlsx", base).read_bytes()
                    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                else:
                    raise HTTPException(404)
        except editor.EditorError as exc:
            raise HTTPException(400, str(exc))
        return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{stem}.{kind}"'})

    # --- JSON
    def need_api_csrf(request: Request) -> None:
        ctx.check_csrf(request, request.headers.get("x-csrf-token"))

    @app.get("/edit/api/end-uses")
    def end_uses(request: Request, iou: str = "", sector: str = ""):
        ctx.need_user(request)
        return {"values": [""] + choices().end_use_choices(iou[:20], sector[:40])}

    @app.get("/edit/api/{draft_id}")
    def get_draft(request: Request, draft_id: str):
        _u, row = _draft_for(ctx, request, draft_id)
        data, m, pc, base = load_draft(ctx, draft_id)
        c = choices()
        help_ = editor.field_help()
        layout = {}
        frames = {"Measure": m, "ProgramCost": pc}
        for t in TABLES:
            pinned, scroll, no_effect = editor.display_layout(t, list(frames[t].columns))
            layout[t] = {"pinned": pinned, "scroll": scroll, "no_effect": sorted(no_effect),
                         "numeric": sorted(editor.numeric_columns(t) & set(frames[t].columns)),
                         "tooltips": {col: editor.column_tooltip(t, col) for col in pinned + scroll},
                         "help": {col: help_.get(col, "") for col in pinned + scroll}}
        return {"id": row["id"], "name": row["name"], "source": row["source"], "has_base": base is not None, "rev": data.get("rev", 1),
                "tables": {t: _table(frames[t]) for t in TABLES}, "layout": layout,
                "choices": _column_choices(m, {t: list(frames[t].columns) for t in TABLES}),
                "issues": _issues(m, pc), "profiles_note": c.catalog_note, "max_rows": editor.MAX_EDIT_ROWS,
                "new_measure": {"defaults": editor.NEW_MEASURE_DEFAULTS,
                                "like": ["PrgID", "ClaimYearQuarter", "IOU_AC_Territory", "E3ClimateZone", "E3TargetSector", "E3GasSector", "Sector", "Version"]},
                "new_programcost_like": ["PrgID", "PrgYear", "ClaimYearQuarter", "PA"]}

    @app.put("/edit/api/{draft_id}")
    async def save_draft(request: Request, draft_id: str):
        u, row = _draft_for(ctx, request, draft_id)
        need_api_csrf(request)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(413, "too large for the editor")
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "not JSON")
        m, pc = _parse_tables(body)
        # one save at a time per draft; a stale tab (an older rev) is refused rather than overwriting newer edits
        with _draft_lock(draft_id):
            data, *_ = load_draft(ctx, draft_id)
            rev = int(data.get("rev", 1))
            if body.get("rev") is not None and int(body["rev"]) != rev:
                return JSONResponse({"error": "This draft was changed in another window. Reload the page to see the latest version.", "rev": rev},
                                    status_code=409)
            _write(_folder(ctx, draft_id), rev + 1, m, pc)
            ctx.db.run("UPDATE drafts SET updated_at = ? WHERE id = ?", (time.time(), draft_id))
        issues = await run_in_threadpool(_issues, m, pc)
        ch = await run_in_threadpool(_column_choices, m, {"Measure": list(m.columns), "ProgramCost": list(pc.columns)})
        return {"rev": rev + 1, "issues": issues, "choices": ch, "saved_at": time.time()}


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _draft_lock(draft_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(draft_id, threading.Lock())


async def _load_upload(form, folder: Path, limit: int) -> tuple[editor.LoadedJob, str]:
    """A draft from an uploaded job workbook, or a Measure.csv (and optional ProgramCost.csv)."""
    from .app import save_upload
    wb, meas, pcost = (form.get(k) for k in ("workbook", "measures", "programcost"))
    wb = wb if isinstance(wb, UploadFile) and wb.filename else None
    meas = meas if isinstance(meas, UploadFile) and meas.filename else None
    pcost = pcost if isinstance(pcost, UploadFile) and pcost.filename else None
    if wb is None and meas is None:
        raise editor.EditorError("choose a job workbook or a Measure.csv")
    folder.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    names = []
    if wb is not None:
        if Path(wb.filename).suffix.lower() not in (".xlsx", ".xlsm"):
            raise editor.EditorError("the job workbook must be an .xlsx or .xlsm file")
        await save_upload(wb, folder / "base.xlsx", limit)
        paths["workbook"] = str(folder / "base.xlsx")
        names.append(Path(wb.filename).name)
    for key, up in (("measures", meas), ("programcost", pcost)):
        if up is not None:
            if Path(up.filename).suffix.lower() not in (".csv", ".txt"):
                raise editor.EditorError(f"{up.filename}: use a pipe-delimited .csv file")
            dest = folder / f"upload_{key}.csv"
            await save_upload(up, dest, limit)
            paths[key] = str(dest)
            names.append(Path(up.filename).name)
    try:
        job = editor.load_tables(paths.get("measures"), paths.get("programcost"), paths.get("workbook"))
    finally:
        for key in ("measures", "programcost"):
            (folder / f"upload_{key}.csv").unlink(missing_ok=True)
    return job, " + ".join(names)
