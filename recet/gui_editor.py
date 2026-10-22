"""The window's "Edit inputs" tab: edit a small job's Measure and ProgramCost tables cell by cell, then run them.

Uses `tksheet` for the grid (copy / paste with Excel, undo, dropdowns, cell highlights and notes). Without it the tab says how to install it and the rest of the
window works as before. The editing logic (loading, dropdown values, checks, saving, column order) is in `editor.py`; this module is layout and events only.

* Load from Job tab / New job fill the grid (jobs of more than editor.MAX_EDIT_ROWS measures are refused: edit those offline).
* Column order (editor.display_layout): CEInputID, MeasCode and MeasDescription (PrgID for program costs) are pinned on the left; to their right, scrolling, come
  the columns that change a result, then the ones that change nothing (greyed headers). The files are still written in CET's column order.
  Pinning is two tksheet grids side by side (`SplitGrid`) whose vertical scrolling is linked; rows are only added or removed with the buttons, which change both.
* Dropdowns offer CET's value lists and, for E3MeaElecEndUseShape, only end uses with an 8,760 profile for the row's IOU and target sector. Typing any other value
  is allowed; it is then flagged.
* Every edit re-checks the tables (editor.check_tables): red = the run would stop, amber = it runs but CET would reject the value or the measure is not priced.
  Hover a coloured cell for the reason; the list under the grid has them all (double-click to go to the cell).
* With "Run with the edited tables" ticked, Run writes the tables into <output folder>/_edited_inputs/<time>/ and runs on them (the job workbook's other sheets
  still apply). Full check runs the real preflight on them without pricing.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pandas as pd

from . import editor

try:
    from tksheet import Sheet, formatter, num2alpha
except ImportError:                        # optional: the tab explains how to install it
    Sheet = None

SEVERITY_COLORS = {"light": {"error": "#f6c9cd", "warning": "#fbe6a6", "info": "#dbe9f7"},
                   "dark": {"error": "#6b2a30", "warning": "#5e4c15", "info": "#22405e"}}
# Header colours: (bg, fg) of a column that changes a result ("calc") and of one that changes nothing ("none"); the legend above the grid uses the same.
HEADER_COLORS = {"light": {"calc": ("#b9e2c4", "#14432a"), "none": ("#bdd7f1", "#163d66")},
                 "dark": {"calc": ("#1f4a2c", "#bfe8cb"), "none": ("#1c3554", "#c3d9f2")}}
TABLES = ("Measure", "ProgramCost")
# No row insert / delete / resize from the grid itself: rows change only through the buttons, so the pinned and scrolling grids keep the same rows.
BINDINGS = ("single_select", "drag_select", "select_all", "row_select", "column_select", "arrowkeys", "right_click_popup_menu", "rc_select", "copy", "cut",
            "paste", "delete", "undo", "edit_cell", "ctrl_select", "column_width_resize", "double_click_column_resize", "find", "replace")
# Hover notes: a header shows what the column affects (editor.column_tooltip), a flagged cell shows its problems. tooltips=False makes tksheet show just the note
# (with True it shows a Cell / Note pair of tabs) and only where there is one.
NOTE_OPTIONS = {"tooltips": False, "tooltip_hover_delay": 450, "tooltip_width": 400, "tooltip_height": 190}
FROZEN_WIDTHS = {"CEInputID": 105, "MeasCode": 90, "MeasDescription": 210, "PrgID": 170}


def _keep_text(value, **_kw) -> str:
    return "" if value is None else str(value)


def _shown_number(value, **_kw) -> str:
    return editor.display_number(value)


class _YOnly:
    """Put in a tksheet's synced-scroll set: passes vertical scrolling to `target` and ignores horizontal scrolling (tksheet's own sync_scroll links both)."""

    def __init__(self, target):
        self.target = target

    def yview(self, *args):
        self.target.MT.set_yviews(*args, move_synced=False)

    def xview(self, *args):
        pass


class SplitGrid:
    """One table shown as a pinned grid (left) and a scrolling grid (right) with the same rows. Columns are addressed by name."""

    def __init__(self, parent, table: str, theme: str, on_modified, on_selected):
        self.table = table
        self.frame = ttk.Frame(parent)
        self.on_selected = on_selected
        self.frozen: list[str] = []
        self.scroll: list[str] = []
        self.no_effect: set[str] = set()
        self.last = None                    # the grid the user last selected in
        self._mirroring = False
        tk_theme = "dark" if theme == "dark" else "light blue"
        # The pinned grid has no scrollbars; a spacer under it stands in for the scrolling grid's horizontal scrollbar, so both show the same height of rows
        # and their linked vertical scrolling (by fraction) lines rows up all the way to the bottom.
        self.left_box = ttk.Frame(self.frame)
        self.spacer = tk.Frame(self.left_box, height=0)
        self.left = Sheet(self.left_box, data=[], headers=[], theme=tk_theme, **NOTE_OPTIONS, show_x_scrollbar=False, show_y_scrollbar=False, max_undos=100,
                          width=300)
        self.right = Sheet(self.frame, data=[], headers=[], theme=tk_theme, **NOTE_OPTIONS, show_x_scrollbar=True, show_y_scrollbar=True, max_undos=100,
                           show_row_index=False, default_column_width=110)
        for s in (self.left, self.right):
            for b in BINDINGS:
                try:
                    s.enable_bindings(b)
                except Exception:              # a binding name this tksheet version does not know
                    pass
            s.set_options(paste_can_expand_x=False, paste_can_expand_y=False)
            s.bind("<<SheetModified>>", lambda _e: on_modified(table))
            s.bind("<<SheetSelect>>", lambda _e, s=s: self._select(s))
        self.left.set_options(empty_horizontal=0)           # no blank strip after the pinned columns (tksheet leaves one by default)
        self.left.bind("<Map>", lambda _e: self.frame.after_idle(self._fit_left), add="+")
        self.left.MT.synced_scrolls.add(_YOnly(self.right))
        self.right.MT.synced_scrolls.add(_YOnly(self.left))
        self.spacer.pack(side="bottom", fill="x")
        self.left.pack(side="top", fill="both", expand=True)
        self.left_box.pack(side="left", fill="y")
        self.right.bind("<Configure>", lambda _e: self.frame.after_idle(self._sync_spacer), add="+")
        ttk.Separator(self.frame, orient="vertical").pack(side="left", fill="y")
        self.right.pack(side="left", fill="both", expand=True)

    # --- data
    @property
    def columns(self) -> list[str]:
        return self.frozen + self.scroll

    def set_frame(self, df: pd.DataFrame, theme: str) -> None:
        self.frozen, self.scroll, self.no_effect = editor.display_layout(self.table, list(df.columns))
        for s, cols in ((self.left, self.frozen), (self.right, self.scroll)):
            s.headers(list(cols))
            s.set_sheet_data(df[cols].values.tolist() if len(df) else [], reset_col_positions=True, delete_options=True)
            s.reset_undos()
        for i, c in enumerate(self.frozen):
            self.left.column_width(i, FROZEN_WIDTHS.get(c, 120))
        self.paint_headers(theme)
        self.format_numbers()
        self._fit_left()

    def raw_value(self, row: int, col: str) -> str:
        s, c = self.locate(col)
        if s is None or row >= self.n_rows():
            return ""
        v = s.get_cell_data(row, c)
        return "" if v is None else str(v)

    def frame_df(self) -> pd.DataFrame:
        parts = []
        for s, cols in ((self.left, self.frozen), (self.right, self.scroll)):
            rows = [["" if v is None else str(v) for v in r] + [""] * (len(cols) - len(r)) for r in s.get_sheet_data()]
            parts.append(pd.DataFrame(rows, columns=cols, dtype=str) if rows else pd.DataFrame(columns=cols, dtype=str))
        return pd.concat(parts, axis=1)

    def n_rows(self) -> int:
        return self.right.get_total_rows()

    def locate(self, col: str):
        if col in self.frozen:
            return self.left, self.frozen.index(col)
        if col in self.scroll:
            return self.right, self.scroll.index(col)
        return None, None

    def _fit_left(self, _event=None) -> None:
        """Size the pinned grid so its columns just fit. The non-table part of its width (row labels, borders) is measured once it is on screen; before
        that (tab hidden) a first estimate is used and the <Map> binding corrects it. Never bound to <Configure>: resizing would re-trigger it."""
        self.left.set_width_of_index_to_text()
        need = sum(self.left.get_column_widths())
        if self.left.winfo_ismapped() and self.left.MT.winfo_width() > 1:
            overhead = self.left.winfo_width() - self.left.MT.winfo_width()
        else:
            overhead = self.left.RI.current_width + 6
        self.left.config(width=need + overhead + 3)

    def _sync_spacer(self) -> None:
        h = self.right.xscroll.winfo_height() if self.right.xscroll_showing else 0
        if int(self.spacer.cget("height")) != h:
            self.spacer.config(height=h)

    def paint_headers(self, theme: str) -> None:
        colors = HEADER_COLORS["dark" if theme == "dark" else "light"]
        for s, cols in ((self.left, self.frozen), (self.right, self.scroll)):
            s.dehighlight_all(cells=False, rows=False, columns=False, header=True, index=False, redraw=False)
            for i, c in enumerate(cols):
                bg, fg = colors["none" if c in self.no_effect else "calc"]
                span = s.span(num2alpha(i), header=True, table=False)
                s.highlight(span, bg=bg, fg=fg, redraw=False)
                s.note(span, note=editor.column_tooltip(self.table, c))
            s.redraw()

    def format_numbers(self) -> None:
        """Numeric columns: right-aligned and shown with editor.display_number (trailing zeros dropped, at most 6 decimals). Display only - the cell keeps
        its text exactly, which is what the cell editor shows and what is checked and saved."""
        nums = editor.numeric_columns(self.table)
        fmt = formatter(datatypes=str, format_function=_keep_text, to_str_function=_shown_number, nullable=True)
        for s, cols in ((self.left, self.frozen), (self.right, self.scroll)):
            for i, c in enumerate(cols):
                if c in nums:
                    span = s.span(num2alpha(i))
                    s.format(span, formatter_options=fmt, redraw=False)
                    s.align(span, align="e", redraw=False)

    # --- rows (both grids, never on the undo stack: an undo that removed a row from one grid only would misalign them)
    def insert_rows(self, rows: list[dict], at: int) -> None:
        for s, cols in ((self.left, self.frozen), (self.right, self.scroll)):
            s.insert_rows([[r.get(c, "") for c in cols] for r in rows], idx=at, undo=False)
            s.reset_undos()

    def delete_rows(self, rows: list[int]) -> None:
        for s in (self.left, self.right):
            s.delete_rows(sorted(rows), undo=False)
            s.reset_undos()

    def selected_rows(self) -> list[int]:
        s = self.last or self.right
        rows = set(s.get_selected_rows()) | {r for r, _c in s.get_selected_cells()}
        cur = s.get_currently_selected()
        if cur and not rows and cur.row is not None:
            rows.add(cur.row)
        return sorted(r for r in rows if r < self.n_rows())

    # --- selection and navigation
    def _select(self, s) -> None:
        if self._mirroring:
            return
        self.last = s
        other = self.right if s is self.left else self.left
        self._mirroring = True
        try:
            other.deselect("all", redraw=False)
            for r in sorted(s.get_selected_rows()):            # a whole-row selection shows on both sides
                other.add_row_selection(r, redraw=False, run_binding_func=False, set_as_current=False)
            other.redraw()
        finally:
            self.frame.after_idle(self._end_mirror)
        cur = s.get_currently_selected()
        if cur and cur.column is not None:
            cols = self.frozen if s is self.left else self.scroll
            if cur.column < len(cols):
                self.on_selected(self.table, cur.row, cols[cur.column])

    def _end_mirror(self) -> None:
        self._mirroring = False

    def goto(self, row: int, col: str) -> None:
        s, c = self.locate(col)
        if s is None:
            s, c = self.right, 0
        s.see(row, c)
        s.select_cell(row, c)
        s.focus_set()

    def see_row(self, row: int) -> None:
        self.right.see(row, 0, keep_xscroll=True)
        self.right.select_row(row)

    # --- options by column name
    def dropdown(self, col: str, values: list[str], row: int | None = None) -> None:
        s, c = self.locate(col)
        if s is None:
            return
        span = s.span(num2alpha(c)) if row is None else s.span(row, c)
        s.dropdown(span, values=values, edit_data=False, set_value="", state="normal", validate_input=False, redraw=False)

    def clear_marks(self, marked: set[tuple[str, int]]) -> None:
        for s in (self.left, self.right):
            # dehighlight_all, not dehighlight(span): in tksheet 7.6 dehighlight(span) also drops the cell's dropdown
            s.dehighlight_all(rows=False, columns=False, header=False, index=False, redraw=False)
        n = self.n_rows()
        for row, col in marked:
            s, c = self.locate(col)
            if s is not None and row < n:
                s.note(s.span(row, c), note=None)

    def mark(self, row: int, col: str, color: str, note: str) -> None:
        s, c = self.locate(col)
        if s is not None:
            s.highlight(s.span(row, c), bg=color, redraw=False)
            s.note(s.span(row, c), note=note)

    def set_row_labels(self) -> None:
        self.left.row_index([str(i + 1) for i in range(self.n_rows())])
        self._fit_left()

    def redraw(self) -> None:
        self.left.redraw()
        self.right.redraw()

    def change_theme(self, theme: str) -> None:
        for s in (self.left, self.right):
            s.change_theme("dark" if theme == "dark" else "light blue")
        self.paint_headers(theme)


class EditorTab:
    def __init__(self, app, parent: ttk.Frame):
        self.app = app
        self.parent = parent
        self.choices = editor.ChoiceSource()
        self.job: editor.LoadedJob | None = None
        self.dirty = False
        self.issues: list[editor.CellIssue] = []
        self._marked: dict[str, set[tuple[int, str]]] = {t: set() for t in TABLES}
        self._q: queue.Queue = queue.Queue()
        self._check_after = None
        self.current = "Measure"
        self.use_var = tk.BooleanVar(value=False)
        self.grids: dict[str, SplitGrid] = {}
        if Sheet is None:
            ttk.Label(parent, text="The input editor needs the 'tksheet' package:\n\n    python -m pip install tksheet\n\nthen restart the window.",
                      justify="left", padding=20).pack(anchor="w")
            return
        self._build()
        parent.after(150, self._poll)

    # ------------------------------------------------------------------------------------------------------------------ layout
    def _build(self) -> None:
        p = self.parent
        top = ttk.Frame(p, padding=(10, 8, 10, 2))
        top.pack(fill="x")
        ttk.Button(top, text="Load from Job tab", command=self.load_from_job).pack(side="left")
        ttk.Button(top, text="New job", command=self.new_job).pack(side="left", padx=6)
        ttk.Button(top, text="Save as workbook…", command=self.save_workbook).pack(side="left", padx=(12, 0))
        ttk.Button(top, text="Save as CET files…", command=self.save_cet).pack(side="left", padx=6)
        ttk.Checkbutton(top, text="Run with the edited tables", variable=self.use_var).pack(side="right")
        row = ttk.Frame(p, padding=(12, 0, 12, 4))
        row.pack(fill="x")
        self.source = ttk.Label(row, text=f"Nothing loaded. Load the Job tab's inputs (up to {editor.MAX_EDIT_ROWS} measures) or start a new job.",
                                style="Sub.TLabel")
        self.source.pack(side="left")
        legend = ttk.Frame(row)
        legend.pack(side="right")
        ttk.Label(legend, text="Column headers:", style="Sub.TLabel").pack(side="left", padx=(0, 6))
        self.legend_chips = {}
        for kind, text in (("calc", "changes results"), ("none", "no effect on results")):
            chip = tk.Label(legend, text=f"  {text}  ", font=("Segoe UI", 9), padx=2, pady=1)
            chip.pack(side="left", padx=(0, 6))
            self.legend_chips[kind] = chip
        self._paint_legend()

        bar = ttk.Frame(p, padding=(10, 2, 10, 4))
        bar.pack(fill="x")
        self.table_var = tk.StringVar(value="Measure")
        self.table_btns = {}
        for t, label in (("Measure", "Measures"), ("ProgramCost", "Program costs")):
            b = ttk.Radiobutton(bar, text=label, value=t, variable=self.table_var, command=lambda t=t: self.show(t))
            b.pack(side="left", padx=(0, 10))
            self.table_btns[t] = (b, label)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(bar, text="Add row", command=self.add_row).pack(side="left")
        ttk.Button(bar, text="Duplicate selected", command=self.duplicate_rows).pack(side="left", padx=6)
        ttk.Button(bar, text="Delete selected", command=self.delete_rows).pack(side="left")
        ttk.Button(bar, text="Full check (preflight)", command=self.full_check).pack(side="right")
        self.summary = ttk.Label(bar, text="", padding=(0, 0, 12, 0))
        self.summary.pack(side="right")

        pane = ttk.PanedWindow(p, orient="vertical")
        pane.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        grid_frame = ttk.Frame(pane)
        pane.add(grid_frame, weight=4)
        for t in TABLES:
            self.grids[t] = SplitGrid(grid_frame, t, self.app.theme, self._modified, self._selected)
        self.grids["Measure"].frame.pack(fill="both", expand=True)

        low = ttk.Frame(pane)
        pane.add(low, weight=1)
        self.help = ttk.Label(low, text="Pinned on the left: the measure's ID, code and description. Then the columns that change a result; greyed headers at the "
                                        "right end change nothing (carried to the output only).", style="Hint.TLabel", wraplength=1000, justify="left",
                              padding=(2, 4))
        self.help.pack(fill="x")
        tv_frame = ttk.Frame(low)
        tv_frame.pack(fill="both", expand=True)
        self.problems = ttk.Treeview(tv_frame, columns=("sev", "table", "row", "column", "message"), show="headings", height=6)
        for c, w, txt in (("sev", 70, "Severity"), ("table", 95, "Table"), ("row", 170, "Row"), ("column", 170, "Column"), ("message", 640, "Problem")):
            self.problems.heading(c, text=txt)
            self.problems.column(c, width=w, stretch=(c == "message"), anchor="w")
        ysb = ttk.Scrollbar(tv_frame, orient="vertical", command=self.problems.yview)
        self.problems.configure(yscrollcommand=ysb.set)
        self.problems.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.problems.bind("<Double-1>", self._goto_problem)
        self.problems.bind("<Return>", self._goto_problem)

    # ------------------------------------------------------------------------------------------------------------------ loading
    def _confirm_discard(self) -> bool:
        return not self.dirty or messagebox.askyesno("Unsaved edits", "Discard the edits in the input editor?")

    def load_from_job(self) -> None:
        if Sheet is None or not self._confirm_discard():
            return
        cfg = self.app.to_config()
        try:
            job = editor.load_tables(cfg.measures or None, cfg.programcost, cfg.workbook)
        except Exception as exc:
            messagebox.showwarning("Load inputs", str(exc))
            return
        self._set_job(job)

    def new_job(self) -> None:
        if Sheet is None or not self._confirm_discard():
            return
        self._set_job(editor.new_job())

    def _set_job(self, job: editor.LoadedJob) -> None:
        self.job = job
        for t in TABLES:
            self.grids[t].set_frame(job.measures if t == "Measure" else job.programcost, self.app.theme)
        self._marked = {t: set() for t in TABLES}
        self._apply_column_dropdowns()
        self.dirty = False
        self.use_var.set(True)
        self.table_var.set("Measure")
        self.show("Measure")
        self._refresh_source()
        self._start_profile_load()
        self.recheck()

    def _start_profile_load(self) -> None:
        cfg = self.app.to_config()
        from . import paths
        paths.set_reference_dir(cfg.reference_dir)
        self.help.config(text="Loading the profile library for the end-use dropdowns and checks…")

        def work():
            self.choices.load_profiles(cfg.profiles_8760_file, cfg.profiles_gas_file)
            self._q.put("profiles")
        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                if self._q.get_nowait() == "profiles":
                    self.help.config(text=self.choices.catalog_note)
                    self._apply_column_dropdowns()
                    self._apply_row_dropdowns()
                    self.recheck()
        except queue.Empty:
            pass
        self.parent.after(150, self._poll)

    # ------------------------------------------------------------------------------------------------------------------ dropdowns
    def _years(self) -> list[int]:
        df = self.table("Measure")
        return sorted({y for y in (editor.claim_year(v) for v in df.get("ClaimYearQuarter", [])) if y})

    def _apply_column_dropdowns(self) -> None:
        years = self._years()
        for t in TABLES:
            g = self.grids[t]
            for name in g.columns:
                vals = self.choices.column_choices(t, name, years)
                if vals:
                    g.dropdown(name, [""] + list(vals))
            g.redraw()

    def _apply_row_dropdowns(self) -> None:
        """End-use dropdowns depend on the row's IOU and target sector."""
        g = self.grids["Measure"]
        cols = [c for c in editor.END_USE_COLUMNS if c in g.columns]
        if not cols:
            return
        df = self.table("Measure")
        cache: dict[tuple[str, str], list[str]] = {}
        for r, (iou, sector) in enumerate(zip(df["IOU_AC_Territory"], df["E3TargetSector"])):
            key = (str(iou), str(sector))
            if key not in cache:
                cache[key] = self.choices.end_use_choices(*key)
            for c in cols:
                g.dropdown(c, [""] + cache[key], row=r)
        g.redraw()

    # ------------------------------------------------------------------------------------------------------------------ table access
    def table(self, t: str) -> pd.DataFrame:
        """The table in CET's column order (extra input columns last), whatever order the grid shows."""
        return editor.conform(self.grids[t].frame_df(), editor.MEASURE_COLUMNS if t == "Measure" else editor.PROGRAMCOST_COLUMNS)

    @property
    def loaded(self) -> bool:
        return self.job is not None and bool(self.grids)

    def show(self, t: str) -> None:
        if not self.grids:
            return
        for g in self.grids.values():
            g.frame.pack_forget()
        self.grids[t].frame.pack(fill="both", expand=True)
        self.current = t

    # ------------------------------------------------------------------------------------------------------------------ row actions
    def add_row(self) -> None:
        if not self.loaded:
            return
        g = self.grids[self.current]
        df = self.table(self.current)
        sel = g.selected_rows()
        like = df.iloc[sel[-1]].to_dict() if sel else (df.iloc[-1].to_dict() if len(df) else None)
        if self.current == "Measure":
            if len(df) >= editor.MAX_EDIT_ROWS:
                messagebox.showinfo("Add row", f"The editor is for jobs of up to {editor.MAX_EDIT_ROWS} measures.")
                return
            row = editor.new_measure_row(like, df["CEInputID"].tolist())
        else:
            row = {c: "" for c in df.columns}
            if like:
                row.update({k: like.get(k, "") for k in ("PrgID", "PrgYear", "ClaimYearQuarter", "PA")})
        self._insert([row], (sel[-1] + 1) if sel else len(df))

    def duplicate_rows(self) -> None:
        if not self.loaded:
            return
        sel = self.grids[self.current].selected_rows()
        if not sel:
            messagebox.showinfo("Duplicate", "Select one or more rows (or a cell in them) first.")
            return
        df = self.table(self.current)
        if self.current == "Measure" and len(df) + len(sel) > editor.MAX_EDIT_ROWS:
            messagebox.showinfo("Duplicate", f"The editor is for jobs of up to {editor.MAX_EDIT_ROWS} measures.")
            return
        ids = df["CEInputID"].tolist() if self.current == "Measure" else []
        new = []
        for r in sel:
            row = df.iloc[r].to_dict()
            if self.current == "Measure":
                row["CEInputID"] = editor.unique_id(row["CEInputID"] or "NEW", ids)
                ids.append(row["CEInputID"])
            new.append(row)
        self._insert(new, sel[-1] + 1)

    def _insert(self, rows: list[dict], at: int) -> None:
        g = self.grids[self.current]
        g.insert_rows(rows, at)
        g.see_row(at)
        self._modified(self.current)

    def delete_rows(self) -> None:
        if not self.loaded:
            return
        sel = self.grids[self.current].selected_rows()
        if sel and messagebox.askyesno("Delete rows", f"Delete {len(sel)} row(s) from {self.current}?"):
            self.grids[self.current].delete_rows(sel)
            self._modified(self.current)

    # ------------------------------------------------------------------------------------------------------------------ events and checks
    def _modified(self, t: str) -> None:
        self.dirty = True
        self._refresh_source()
        if self._check_after is not None:
            self.parent.after_cancel(self._check_after)
        self._check_after = self.parent.after(250, self._after_edit)

    def _after_edit(self) -> None:
        self._check_after = None
        if self.choices.profiles_loaded:
            self._apply_row_dropdowns()
        self.recheck()

    def recheck(self) -> None:
        if not self.loaded:
            return
        tables = {t: self.table(t) for t in TABLES}
        self.issues = editor.check_tables(tables["Measure"], tables["ProgramCost"], self.choices if self.choices.profiles_loaded else None)
        colors = SEVERITY_COLORS["dark" if self.app.theme == "dark" else "light"]
        rank = {"error": 0, "warning": 1, "info": 2}
        by_cell: dict[tuple[str, int, str], list[editor.CellIssue]] = {}
        for i in self.issues:
            by_cell.setdefault((i.table, i.row, i.column), []).append(i)
        for t in TABLES:
            self.grids[t].clear_marks(self._marked[t])
            self._marked[t] = set()
        for (t, r, col), lst in by_cell.items():
            lst.sort(key=lambda x: rank[x.severity])
            self.grids[t].mark(r, col, colors[lst[0].severity], "\n".join(f"{x.severity.upper()}: {x.message}" for x in lst))
            self._marked[t].add((r, col))
        for t in TABLES:
            self.grids[t].set_row_labels()
            self.grids[t].redraw()
        self.problems.delete(*self.problems.get_children())
        for k, i in enumerate(sorted(self.issues, key=lambda x: (rank[x.severity], x.table, x.row))):
            key = tables[i.table]
            who = key.iloc[i.row]["CEInputID" if i.table == "Measure" else "PrgID"] if i.row < len(key) else ""
            self.problems.insert("", "end", iid=str(k), values=(i.severity, i.table, f"{i.row + 1}  {who}", i.column, i.message),
                                 tags=(f"{i.table}|{i.row}|{i.column}",))
        n_err = sum(i.severity == "error" for i in self.issues)
        n_warn = sum(i.severity == "warning" for i in self.issues)
        self.summary.config(text=("✓ no problems" if not (n_err or n_warn) else f"{n_err} error(s), {n_warn} warning(s)"),
                            foreground=self.app.BAD if n_err else (self.app.WARN if n_warn else self.app.OK))
        for t, (b, label) in self.table_btns.items():
            b.config(text=f"{label} ({len(tables[t])})")

    def _selected(self, t: str, row: int, col: str) -> None:
        g = self.grids[t]
        if col == "CEInputID":
            note = "  (the row's ID: no effect on any value)"
        else:
            note = "  (no effect on any result: carried to the output only)" if col in g.no_effect else "  (changes results)"
        text = f"{col}{note}:  {editor.field_help().get(col, '')}"
        raw = g.raw_value(row, col)
        if col in editor.numeric_columns(t) and raw:
            try:
                rounded = float(editor.display_number(raw)) != float(raw)
            except ValueError:
                rounded = False
            if rounded:
                text += f"\nFull value: {raw} (shown rounded to {editor.DISPLAY_DECIMALS} decimals; the full value is what is used and saved)"
        here = [i for i in self.issues if i.table == t and i.row == row and i.column == col]
        if here:
            text += "\n" + "\n".join(f"{i.severity.upper()}: {i.message}" for i in here)
        self.help.config(text=text)

    def _goto_problem(self, _e=None) -> None:
        sel = self.problems.selection()
        if not sel:
            return
        t, r, col = self.problems.item(sel[0], "tags")[0].split("|", 2)
        self.table_var.set(t)
        self.show(t)
        self.grids[t].goto(int(r), col)

    def _refresh_source(self) -> None:
        if self.job is None:
            return
        self.source.config(text=f"Editing: {self.job.source}" + ("   (edited, not saved)" if self.dirty else "")
                           + (f"   -  other sheets of {Path(self.job.base_workbook).name} still apply to a run" if self.job.base_workbook else ""))

    def _paint_legend(self) -> None:
        colors = HEADER_COLORS["dark" if self.app.theme == "dark" else "light"]
        for kind, chip in self.legend_chips.items():
            bg, fg = colors[kind]
            chip.config(background=bg, foreground=fg)

    def set_theme(self, theme: str) -> None:
        if hasattr(self, "legend_chips"):
            self._paint_legend()
        for g in self.grids.values():
            g.change_theme(theme)
        if self.loaded:
            self.recheck()

    # ------------------------------------------------------------------------------------------------------------------ saving and running
    def save_workbook(self) -> None:
        if not self.loaded:
            return
        p = filedialog.asksaveasfilename(title="Save the job as a workbook", defaultextension=".xlsx", filetypes=[("Excel workbook", "*.xlsx")],
                                         initialfile="edited_job.xlsx")
        if not p:
            return
        try:
            editor.write_workbook(self.table("Measure"), self.table("ProgramCost"), p, self.job.base_workbook)
        except Exception as exc:
            messagebox.showerror("Save", str(exc))
            return
        self.dirty = False
        self._refresh_source()
        if messagebox.askyesno("Saved", f"{p}\n\nUse this workbook on the Job tab (input type: Excel job workbook)?"):
            self.app.v["workbook"].set(p)
            self.app._set_input_mode("excel")
            self.app._start_check("workbook")
            self.job.base_workbook = p
            self._refresh_source()

    def save_cet(self) -> None:
        if not self.loaded:
            return
        d = filedialog.askdirectory(title="Folder for Measure.csv and ProgramCost.csv")
        if not d:
            return
        existing = [f for f in ("Measure.csv", "ProgramCost.csv") if (Path(d) / f).exists()]
        if existing and not messagebox.askyesno("Overwrite?", f"{', '.join(existing)} already exist in {d}. Overwrite?"):
            return
        try:
            files = editor.write_cet_files(self.table("Measure"), self.table("ProgramCost"), d)
        except Exception as exc:
            messagebox.showerror("Save", str(exc))
            return
        self.dirty = False
        self._refresh_source()
        messagebox.showinfo("Saved", "\n".join(str(f) for f in files.values() if f))

    def counts(self) -> tuple[int, int]:
        return sum(i.severity == "error" for i in self.issues), sum(i.severity == "warning" for i in self.issues)

    def apply_to(self, cfg):
        """The RunConfig to run with the edited tables (written to <out_dir>/_edited_inputs/<time>/), or None if the run should not start."""
        if not (self.loaded and self.use_var.get()):
            return cfg
        self.recheck()
        n_err, _ = self.counts()
        if n_err:
            self.app.nb.select(self.parent)
            messagebox.showwarning("Edited inputs", f"The edited tables have {n_err} error(s) that would stop the run (red cells; see the list under the grid). "
                                                    "Fix them, or untick 'Run with the edited tables'.")
            return None
        from .jobs import RunConfig
        from dataclasses import asdict
        given = {f: getattr(cfg, f) for f in ("run_parameters_file", "sw_factors_file", "profiles_gas_file", "programs_file", "profiles_8760_file")}
        try:
            updates, _folder = editor.prepare_run_inputs(self.table("Measure"), self.table("ProgramCost"), cfg.out_dir, self.job.base_workbook, given)
        except Exception as exc:
            messagebox.showerror("Edited inputs", str(exc))
            return None
        return RunConfig.from_dict({**asdict(cfg), **updates})

    def full_check(self) -> None:
        if not self.loaded:
            messagebox.showinfo("Full check", "Load or start a job first.")
            return
        if not self.use_var.get():
            self.use_var.set(True)
        self.app._run(preflight_only=True)
