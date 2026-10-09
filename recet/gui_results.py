"""The window's Results tab: a finished run's results filtered by PA, at five levels, with columns chosen by a saved view. Layout and events only; the tables
come from `results_view.py`.

* Top: the run shown, Load results from a folder, Open measure-level table, Export table.
* Filter row: PA (All PAs or one administrator), View (the built-in Default or a saved view), Edit views (a dialog to add, remove and order columns and save
  the view under a name).
* Tabs: Measure, Program, Program sector, Program segment and Portfolio level. The measure tab is turned off when the filtered run has more than
  results_view.MAX_MEASURE_ROWS measures (open the measure-level file instead).
* A two-version comparison folder has no measure-level table of its own: its comparison summary is shown on the Portfolio tab as it is.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import pandas as pd

from . import outputs, results_view as rv

try:
    from tksheet import Sheet, num2alpha
except ImportError:                     # tksheet is a dependency; without it the tab says so
    Sheet = None

BINDINGS = ("single_select", "drag_select", "select_all", "row_select", "column_select", "arrowkeys", "copy", "rc_select", "right_click_popup_menu",
            "column_width_resize", "double_click_column_resize", "find")
TEXT_WIDTH_CAP, NUMBER_WIDTH_CAP = 280, 170            # pixels; hover a cut-off cell or widen the column to read all of it
ID_COLUMNS = {"CEInputID", "Description", "PrgID", "PA", "Program name", "Program sector", "Program segment", "Portfolio", "Measures"}


class ResultsTab:
    def __init__(self, app, parent: ttk.Frame):
        self.app = app
        self.parent = parent
        self.frame: pd.DataFrame | None = None
        self.run_dir: Path | None = None
        self.plain: pd.DataFrame | None = None          # a comparison summary, shown as it is
        self.tables: dict[str, pd.DataFrame] = {}
        self.views, current = rv.load_views()
        self._q: queue.Queue = queue.Queue()
        self.sheets: dict[str, Sheet] = {}
        self.tab_frames: dict[str, ttk.Frame] = {}
        self._build(current)
        parent.after(150, self._poll)

    # ------------------------------------------------------------------------------------------------------------------ layout
    def _build(self, current_view: str) -> None:
        p = self.parent
        top = ttk.Frame(p, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        self.title = ttk.Label(top, text="No results yet: run a job.", style="Sub.TLabel")
        self.title.pack(side="left")
        ttk.Button(top, text="Export table…", command=self.export).pack(side="right")
        ttk.Button(top, text="Open measure-level table", command=self.app._open_measure_table).pack(side="right", padx=8)
        ttk.Button(top, text="Load results from a folder…", command=self.app._choose_results).pack(side="right")

        bar = ttk.Frame(p, padding=(10, 2, 10, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="PA").pack(side="left")
        self.pa_var = tk.StringVar(value=rv.ALL_PAS)
        self.pa_box = ttk.Combobox(bar, textvariable=self.pa_var, values=[rv.ALL_PAS], state="readonly", width=16)
        self.pa_box.pack(side="left", padx=(6, 18))
        self.pa_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Label(bar, text="View").pack(side="left")
        self.view_var = tk.StringVar(value=current_view)
        self.view_box = ttk.Combobox(bar, textvariable=self.view_var, values=list(self.views), state="readonly", width=24)
        self.view_box.pack(side="left", padx=(6, 6))
        self.view_box.bind("<<ComboboxSelected>>", lambda _e: self._view_chosen())
        self.edit_btn = ttk.Button(bar, text="Edit views…", command=self.edit_views)
        self.edit_btn.pack(side="left")
        self.status = ttk.Label(bar, text="", style="Hint.TLabel")
        self.status.pack(side="right")

        self.nb = ttk.Notebook(p)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for key, title in rv.LEVELS:
            f = ttk.Frame(self.nb)
            self.nb.add(f, text=title)
            self.tab_frames[key] = f
            if Sheet is None:
                ttk.Label(f, text="The results tables need the 'tksheet' package (python -m pip install tksheet).", padding=20).pack(anchor="w")
                continue
            s = Sheet(f, data=[], headers=[], theme="dark" if self.app.theme == "dark" else "light blue", tooltips=False, tooltip_hover_delay=450,
                      tooltip_width=360, tooltip_height=120, show_x_scrollbar=True, show_y_scrollbar=True)
            for b in BINDINGS:
                try:
                    s.enable_bindings(b)
                except Exception:
                    pass
            s.pack(fill="both", expand=True)
            self.sheets[key] = s
        self.nb.select(self.tab_frames["portfolio"])

    def set_theme(self, theme: str) -> None:
        for s in self.sheets.values():
            s.change_theme("dark" if theme == "dark" else "light blue")

    # ------------------------------------------------------------------------------------------------------------------ loading
    def load(self, run_dir: Path) -> bool:
        """Show a run folder's results (read in the background). False if the folder has neither a measure-level table nor a comparison summary."""
        run_dir = Path(run_dir)
        files = sorted(run_dir.glob("measure_level_*.*"))
        if files:
            self.title.config(text=f"Loading {files[0].name}…")
            self.app.last_run_dir = run_dir

            def work():
                try:
                    self._q.put(("frame", (run_dir, files[0], outputs.read_table(files[0]))))
                except Exception as exc:                    # a damaged file: say so, keep the window running
                    self._q.put(("error", f"{files[0]}: {exc}"))
            threading.Thread(target=work, daemon=True).start()
            return True
        comp = sorted(run_dir.glob("comparison_summary_*.*"))
        if comp:
            self.app.last_run_dir = run_dir
            self._show_plain(run_dir, comp[0], outputs.read_table(comp[0]))
            return True
        return False

    def _poll(self) -> None:
        try:
            while True:
                kind, val = self._q.get_nowait()
                if kind == "frame":
                    run_dir, path, frame = val
                    self.frame, self.run_dir, self.plain = frame, run_dir, None
                    self.title.config(text=f"{path.name}   ({run_dir})")
                    pas = rv.available_pas(frame)
                    self.pa_box.config(values=pas, state="readonly")
                    if self.pa_var.get() not in pas:
                        self.pa_var.set(rv.ALL_PAS)
                    self.view_box.config(state="readonly")
                    self.edit_btn.config(state="normal")
                    self.refresh()
                else:
                    self.title.config(text="Could not read the results")
                    messagebox.showerror("Results", val)
        except queue.Empty:
            pass
        self.parent.after(150, self._poll)

    def _show_plain(self, run_dir: Path, path: Path, df: pd.DataFrame) -> None:
        """A two-version comparison: its summary as it is, on the Portfolio tab; the views apply to a single run's folder."""
        self.frame, self.run_dir, self.plain = None, run_dir, df
        self.title.config(text=f"{path.name}   ({run_dir})")
        self.status.config(text="Two-version comparison: load one version's run folder for the PA filter, levels and views.")
        self.pa_box.config(values=[rv.ALL_PAS], state="disabled")
        self.view_box.config(state="disabled")
        self.edit_btn.config(state="disabled")
        for key, _ in rv.LEVELS:
            self.nb.tab(self.tab_frames[key], state="normal" if key == "portfolio" else "disabled")
        self.tables = {"portfolio": df}
        self._fill("portfolio", df, plain=True)
        self.nb.select(self.tab_frames["portfolio"])

    # ------------------------------------------------------------------------------------------------------------------ tables
    def _view_keys(self) -> list[str]:
        return self.views.get(self.view_var.get(), rv.DEFAULT_VIEW)

    def _view_chosen(self) -> None:
        rv.save_views(self.views, self.view_var.get())
        self.refresh()

    def refresh(self) -> None:
        if self.frame is None:
            return
        pa, keys = self.pa_var.get(), self._view_keys()
        n = len(rv.filter_pa(self.frame, pa))
        allowed = rv.measure_level_allowed(self.frame, pa)
        self.tables = {}
        for key, title in rv.LEVELS:
            frame = self.tab_frames[key]
            if key == "measure":
                self.nb.tab(frame, state="normal" if allowed else "disabled",
                            text=title if allowed else f"{title} (off: {n:,} measures)")
                if not allowed:
                    if self.nb.select() == str(frame):
                        self.nb.select(self.tab_frames["program"])
                    continue
            else:
                self.nb.tab(frame, state="normal")
            t = rv.table(self.frame, key, pa, keys)
            self.tables[key] = t
            self._fill(key, t)
        note = "" if allowed else f"   Measure level is off above {rv.MAX_MEASURE_ROWS:,} measures: use Open measure-level table."
        self.status.config(text=f"{n:,} measures{'' if pa == rv.ALL_PAS else ' for ' + pa}.{note}")

    def _fill(self, key: str, t: pd.DataFrame, plain: bool = False) -> None:
        s = self.sheets.get(key)
        if s is None:
            return
        cat = rv.catalog(self.frame) if (self.frame is not None and not plain) else {}
        headers = [cat[c].label if c in cat else c for c in t.columns]
        rows = []
        for r in t.itertuples(index=False):
            rows.append([rv.format_value(cat.get(c), v) if c in cat else _plain(v) for c, v in zip(t.columns, r)])
        s.headers(headers)
        s.set_sheet_data(rows, reset_col_positions=True, delete_options=True)
        for i, c in enumerate(t.columns):
            span = s.span(num2alpha(i))
            if c not in ID_COLUMNS and not (plain and t[c].dtype == object):
                s.align(span, align="e", redraw=False)
            col = cat.get(c)
            note = f"{col.label}\n{col.help or ('Output column ' + c + ', summed over the measures in the row.')}" if col else c
            s.note(s.span(num2alpha(i), header=True, table=False), note=note)
        s.set_all_column_widths()
        for i, c in enumerate(t.columns):              # long names (program names, descriptions) would push the numbers off screen
            cap = TEXT_WIDTH_CAP if (c in ID_COLUMNS or (plain and t[c].dtype == object)) else NUMBER_WIDTH_CAP
            if s.column_width(i) > cap:
                s.column_width(i, cap)
        s.redraw()

    # ------------------------------------------------------------------------------------------------------------------ actions
    def current_level(self) -> str:
        sel = self.nb.select()
        return next((k for k, f in self.tab_frames.items() if str(f) == sel), "portfolio")

    def export(self) -> None:
        t = self.tables.get(self.current_level())
        if t is None or t.empty:
            messagebox.showinfo("Export table", "Nothing to export: load results first.")
            return
        level = self.current_level()
        pa = self.pa_var.get().replace(" ", "_")
        p = filedialog.asksaveasfilename(title="Export this table", defaultextension=".xlsx", initialfile=f"results_{level}_{pa}.xlsx",
                                         filetypes=[("Excel workbook", "*.xlsx"), ("CSV", "*.csv")])
        if not p:
            return
        cat = rv.catalog(self.frame) if self.frame is not None else {}
        out = t.rename(columns={c: cat[c].label for c in t.columns if c in cat})
        try:
            outputs.write_table(out, Path(p).with_suffix(""), "csv" if p.lower().endswith(".csv") else "xlsx")
        except Exception as exc:
            messagebox.showerror("Export table", str(exc))
            return
        messagebox.showinfo("Export table", f"Saved {p}")

    def edit_views(self) -> None:
        if self.frame is None:
            messagebox.showinfo("Edit views", "Load a run's results first: the columns you can choose come from the run.")
            return
        ViewEditor(self, rv.catalog(self.frame))

    def views_changed(self, current: str) -> None:
        self.view_box.config(values=list(self.views))
        self.view_var.set(current)
        rv.save_views(self.views, current)
        self.refresh()


def _plain(v) -> str:
    if isinstance(v, float):
        return "" if v != v else (f"{v:,.3f}" if abs(v) < 100 else f"{v:,.0f}")
    return str(v)


class ViewEditor:
    """Choose a view's columns: Available on the left, Shown on the right (in order). The Default view cannot be changed, so changes to it are saved under a new
    name."""

    def __init__(self, tab: ResultsTab, cat: dict):
        self.tab, self.cat = tab, cat
        self.win = tk.Toplevel(tab.parent)
        self.win.title("Result views")
        self.win.transient(tab.parent.winfo_toplevel())
        self.win.geometry("760x520")
        dark = tab.app.theme == "dark"
        self.lb_colors = dict(background="#1c1c1c" if dark else "#ffffff", foreground="#e6e6e6" if dark else "#1f1f1f",
                              selectbackground="#2f5d8a" if dark else "#cfe3f7", selectforeground="#ffffff" if dark else "#000000")

        row = ttk.Frame(self.win, padding=(12, 10, 12, 4))
        row.pack(fill="x")
        ttk.Label(row, text="View").pack(side="left")
        self.name_var = tk.StringVar(value=tab.view_var.get())
        self.name_box = ttk.Combobox(row, textvariable=self.name_var, values=list(tab.views), state="readonly", width=28)
        self.name_box.pack(side="left", padx=6)
        self.name_box.bind("<<ComboboxSelected>>", lambda _e: self._load(self.name_var.get()))
        self.del_btn = ttk.Button(row, text="Delete view", command=self._delete)
        self.del_btn.pack(side="left", padx=6)
        ttk.Button(row, text="Reset to default columns", command=lambda: self._set_shown(list(rv.DEFAULT_VIEW))).pack(side="left")

        body = ttk.Frame(self.win, padding=(12, 4, 12, 4))
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Available columns").pack(anchor="w")
        self.filter_var = tk.StringVar()
        ent = ttk.Entry(left, textvariable=self.filter_var)
        ent.pack(fill="x", pady=(2, 4))
        ent.bind("<KeyRelease>", lambda _e: self._fill_available())
        self.avail = tk.Listbox(left, selectmode="extended", activestyle="none", **self.lb_colors)
        self.avail.pack(fill="both", expand=True)
        self.avail.bind("<Double-1>", lambda _e: self._add())
        mid = ttk.Frame(body, padding=(8, 30, 8, 0))
        mid.pack(side="left", fill="y")
        for text, cmd in (("Add →", self._add), ("← Remove", self._remove), ("Move up", lambda: self._move(-1)), ("Move down", lambda: self._move(1))):
            ttk.Button(mid, text=text, command=cmd, width=12).pack(pady=3)
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="Shown columns (in order)").pack(anchor="w")
        self.shown_lb = tk.Listbox(right, selectmode="extended", activestyle="none", **self.lb_colors)
        self.shown_lb.pack(fill="both", expand=True, pady=(26, 0))
        self.shown_lb.bind("<Double-1>", lambda _e: self._remove())
        self.hint = ttk.Label(self.win, text="", style="Hint.TLabel", padding=(12, 2))
        self.hint.pack(fill="x")

        bottom = ttk.Frame(self.win, padding=(12, 6, 12, 12))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Close", command=self.win.destroy).pack(side="right")
        ttk.Button(bottom, text="Save as new view…", command=self._save_as).pack(side="right", padx=6)
        self.save_btn = ttk.Button(bottom, text="Save", command=self._save)
        self.save_btn.pack(side="right")
        self._load(self.name_var.get())

    def _label(self, key: str) -> str:
        c = self.cat.get(key)
        return f"{c.label}   [{key}]" if c is not None and c.label != key else key

    def _load(self, name: str) -> None:
        self._set_shown([k for k in self.tab.views.get(name, rv.DEFAULT_VIEW) if k in self.cat])
        default = name == rv.DEFAULT_VIEW_NAME
        self.del_btn.config(state="disabled" if default else "normal")
        self.save_btn.config(state="disabled" if default else "normal")
        self.hint.config(text="The Default view cannot be changed: use Save as new view." if default else "Double-click a column to move it across.")

    def _set_shown(self, keys: list[str]) -> None:
        self.shown = list(keys)
        self.shown_lb.delete(0, "end")
        for k in self.shown:
            self.shown_lb.insert("end", self._label(k))
        self._fill_available()

    def _fill_available(self) -> None:
        q = self.filter_var.get().strip().lower()
        self.available = [k for k in self.cat if k not in self.shown and (not q or q in k.lower() or q in self.cat[k].label.lower())]
        self.avail.delete(0, "end")
        for k in self.available:
            self.avail.insert("end", self._label(k))

    def _add(self) -> None:
        picks = [self.available[i] for i in self.avail.curselection()]
        self._set_shown(self.shown + picks)

    def _remove(self) -> None:
        drop = {self.shown[i] for i in self.shown_lb.curselection()}
        self._set_shown([k for k in self.shown if k not in drop])

    def _move(self, step: int) -> None:
        sel = list(self.shown_lb.curselection())
        if len(sel) != 1:
            return
        i, j = sel[0], sel[0] + step
        if 0 <= j < len(self.shown):
            self.shown[i], self.shown[j] = self.shown[j], self.shown[i]
            self._set_shown(self.shown)
            self.shown_lb.selection_set(j)

    def _save(self) -> None:
        name = self.name_var.get()
        if name == rv.DEFAULT_VIEW_NAME:
            return self._save_as()
        if not self.shown:
            messagebox.showwarning("Result views", "Choose at least one column.", parent=self.win)
            return
        self.tab.views[name] = list(self.shown)
        self.tab.views_changed(name)

    def _save_as(self) -> None:
        if not self.shown:
            messagebox.showwarning("Result views", "Choose at least one column.", parent=self.win)
            return
        name = simpledialog.askstring("Save view", "Name for this view:", parent=self.win)
        name = (name or "").strip()
        if not name:
            return
        if name == rv.DEFAULT_VIEW_NAME:
            messagebox.showwarning("Result views", "Choose another name: Default is the built-in view.", parent=self.win)
            return
        if name in self.tab.views and not messagebox.askyesno("Result views", f"Replace the view '{name}'?", parent=self.win):
            return
        self.tab.views[name] = list(self.shown)
        self.tab.views_changed(name)
        self.name_box.config(values=list(self.tab.views))
        self.name_var.set(name)
        self._load(name)

    def _delete(self) -> None:
        name = self.name_var.get()
        if name == rv.DEFAULT_VIEW_NAME or not messagebox.askyesno("Result views", f"Delete the view '{name}'?", parent=self.win):
            return
        self.tab.views.pop(name, None)
        self.tab.views_changed(rv.DEFAULT_VIEW_NAME)
        self.name_box.config(values=list(self.tab.views))
        self.name_var.set(rv.DEFAULT_VIEW_NAME)
        self._load(rv.DEFAULT_VIEW_NAME)
