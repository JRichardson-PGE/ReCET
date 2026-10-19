"""Docs > Value lists: every Edit inputs field that has a dropdown and the values it offers, and the admin changes to those lists.

The pages are built on each request from the value lists that ship with ReCET (`recet/data/value_lists/`, CET's own lists) with the admin changes stored in
the app's database (`value_list_changes`) layered over them, so they always show what the editor offers and checks. Admins can add a code, retire one (it stays
valid up to a last claim year, as CET retires codes) or reinstate one, and undo any of their changes. The changes reach the Edit inputs dropdowns and its
"not an active CET value" warnings at once (`editor.set_value_list_changes`); they do not change any calculation, which never reads these lists.
"""
from __future__ import annotations

import time
from datetime import date

import pandas as pd
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import editor, paths, preflight

MAX_CODE_CHARS = 100
MAX_DESCRIPTION_CHARS = 300
FIRST_YEAR, LAST_YEAR = 2000, 2100


def lists() -> dict[str, list[tuple[str, str]]]:
    """Value-list name -> the (table, field) pairs whose dropdown it fills, in the editor's own mapping (so a new mapped field appears here by itself)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for table, mapping in (("Measure", editor.VALUE_LISTS), ("ProgramCost", editor.PROGRAMCOST_VALUE_LISTS)):
        for field, name in mapping.items():
            out.setdefault(name, []).append((table, field))
    return dict(sorted(out.items(), key=lambda kv: kv[1][0][1].lower()))


# Fields with a dropdown that is not a CET value list, and what they offer.
NOTES = {
    "E3MeaElecEndUseShape": "Offers the end uses that have an 8,760 profile in the library (and supplied profile files) for the row's IOU and target sector. "
                            "Values are still checked against the list below.",
    "E3GasSavProfile": "Offers the gas profiles in the library and supplied gas profile files as well as the list below.",
}


def fixed_fields() -> list[dict]:
    return [{"field": "IOU_AC_Territory", "table": "Measure", "values": sorted(preflight.VALID_PA),
             "note": "The IOUs ReCET can price. Fixed by the tool, not a value list."},
            {"field": "Residential_Flag, Upstream_Flag", "table": "Measure", "values": ["0", "1"], "note": "Flags."}]


def sync(ctx) -> None:
    """Load the stored admin changes into the editor (at start-up and after every change)."""
    out: dict[str, dict[str, dict]] = {}
    for r in ctx.db.all("SELECT * FROM value_list_changes"):
        out.setdefault(r["list"], {})[r["code"]] = {"kind": r["kind"], "start_year": r["start_year"], "end_year": r["end_year"], "description": r["description"]}
    editor.set_value_list_changes(out)


def _year(v) -> int | None:
    return None if v is None or pd.isna(v) else int(v)


def _rows(name: str) -> list[dict]:
    this_year = date.today().year
    df = editor.value_list(name)
    rows = []
    for r in df.itertuples(index=False):
        start, end = _year(r.start_year), _year(r.end_year)
        retired = end is not None and end < this_year
        status = f"Retired after {end}" if retired else (f"Valid {start} on" if start and start > this_year else "Active")
        if end is not None and not retired:
            status = f"Active through {end}"
        rows.append({"code": r.code, "description": r.description, "start": start, "end": end, "retired": end is not None, "status": status, "source": r.source})
    return sorted(rows, key=lambda x: editor._natural(x["code"]))


def _source_note() -> str:
    p = paths.package_data("value_lists", "SOURCE.txt")
    try:
        first = p.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return ""
    return first.split("(copied", 1)[0].strip().rstrip(",")


def register(app, ctx) -> None:
    def page(request: Request, name: str, status_code: int = 200, error: str = "", done: str = ""):
        from .app import doc_names
        all_lists = lists()
        if name not in all_lists:
            raise HTTPException(404)
        changes = ctx.db.all("SELECT * FROM value_list_changes WHERE list = ? ORDER BY at DESC", (name,))
        return ctx.render(request, "value_list.html", status_code=status_code, docs=doc_names(), current="value-lists", title=f"Value list: {name}",
                          name=name, fields=all_lists[name], rows=_rows(name), changes=changes, error=error, done=done, this_year=date.today().year,
                          note=" ".join(NOTES[f] for _t, f in all_lists[name] if f in NOTES))

    @app.get("/docs/value-lists", response_class=HTMLResponse)
    async def value_lists_index(request: Request):
        from .app import doc_names
        ctx.need_user(request)
        index = []
        for name, fields in lists().items():
            rows = _rows(name)
            index.append({"name": name, "fields": fields, "n": len(rows), "active": sum(not r["retired"] for r in rows),
                          "changed": sum(r["source"] != "CET" for r in rows)})
        return ctx.render(request, "value_lists.html", docs=doc_names(), current="value-lists", title="Value lists", index=index,
                          fixed=fixed_fields(), source_note=_source_note())

    @app.get("/docs/value-lists/{name}", response_class=HTMLResponse)
    async def value_list_page(request: Request, name: str, done: str = ""):
        ctx.need_user(request)
        return page(request, name, done=done)

    @app.post("/docs/value-lists/{name}")
    async def value_list_change(request: Request, name: str):
        admin = ctx.need_admin(request)
        form = await request.form()
        ctx.check_csrf(request, str(form.get("csrf", "")))
        if name not in lists():
            raise HTTPException(404)
        act = str(form.get("act", ""))
        action, _, code = act.partition("|")
        if action == "add":
            code = str(form.get("code", ""))
        code = code.strip()
        try:
            msg = apply_change(ctx, admin, name, action, code, form)
        except ValueError as exc:
            return page(request, name, status_code=400, error=str(exc))
        sync(ctx)
        ctx.db.audit(f"value list {action}", admin, detail=f"{name}: {code}", ip=ctx.ip(request))
        from urllib.parse import quote
        return ctx.redirect(f"/docs/value-lists/{name}?done={quote(msg)}")


def _int_year(v, what: str, blank_ok: bool) -> int | None:
    s = str(v or "").strip()
    if not s:
        if blank_ok:
            return None
        raise ValueError(f"Enter the {what}.")
    if not s.isdigit() or not FIRST_YEAR <= int(s) <= LAST_YEAR:
        raise ValueError(f"The {what} must be a year between {FIRST_YEAR} and {LAST_YEAR}.")
    return int(s)


def apply_change(ctx, admin, name: str, action: str, code: str, form) -> str:
    """Store one admin change; returns the confirmation shown on the page. Raises ValueError with a message for the admin."""
    if not code:
        raise ValueError("Enter a code.")
    if len(code) > MAX_CODE_CHARS or any(ch in code for ch in "|\r\n\t"):
        raise ValueError(f"A code must be at most {MAX_CODE_CHARS} characters, without | or line breaks (CET files are |-delimited).")
    rows = {r["code"].lower(): r for r in _rows(name)}
    cur = rows.get(code.lower())
    code = cur["code"] if cur else code
    stored = ctx.db.one("SELECT * FROM value_list_changes WHERE list = ? AND lower(code) = lower(?)", (name, code))

    def put(kind, start, end, description=""):
        ctx.db.run("DELETE FROM value_list_changes WHERE list = ? AND lower(code) = lower(?)", (name, code))
        ctx.db.run("INSERT INTO value_list_changes (list, code, kind, start_year, end_year, description, user_id, email, at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (name, code, kind, start, end, description, admin["id"], admin["email"], time.time()))

    if action == "add":
        description = str(form.get("description", "")).strip()[:MAX_DESCRIPTION_CHARS]
        start = _int_year(form.get("start_year"), "first valid claim year", blank_ok=True)
        if cur and not cur["retired"]:
            raise ValueError(f"'{code}' is already in the list and active.")
        if cur:
            return _reinstate(ctx, name, code, cur, stored, put)
        put("added", start, None, description)
        return f"Added '{code}'."
    if cur is None:
        raise ValueError(f"'{code}' is not in the list.")
    if action == "retire":
        end = _int_year(form.get("end_year"), "last valid claim year", blank_ok=False)
        if cur["retired"]:
            raise ValueError(f"'{code}' is already retired.")
        if cur["start"] and end < cur["start"]:
            raise ValueError(f"'{code}' is only valid from {cur['start']}, so its last valid year cannot be {end}.")
        if stored is not None and stored["kind"] == "added":
            put("added", stored["start_year"], end, stored["description"])
        else:
            put("retired", None, end)
        return f"Retired '{code}': valid for claim years up to {end}."
    if action == "reinstate":
        if not cur["retired"]:
            raise ValueError(f"'{code}' is not retired.")
        return _reinstate(ctx, name, code, cur, stored, put)
    if action == "undo":
        if stored is None:
            raise ValueError(f"'{code}' has no admin change to undo.")
        ctx.db.run("DELETE FROM value_list_changes WHERE list = ? AND lower(code) = lower(?)", (name, code))
        return f"Removed '{code}'." if stored["kind"] == "added" else f"'{code}' is back to CET's own entry."
    raise ValueError("Unknown change.")


def _reinstate(ctx, name, code, cur, stored, put) -> str:
    if stored is not None and stored["kind"] == "added":
        put("added", stored["start_year"], None, stored["description"])
    elif stored is not None and stored["kind"] == "retired":
        ctx.db.run("DELETE FROM value_list_changes WHERE list = ? AND lower(code) = lower(?)", (name, code))
    else:
        put("reinstated", None, None)
    return f"Reinstated '{code}'."
