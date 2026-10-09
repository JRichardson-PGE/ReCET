"""Graphical interface: `python -m recet` (or `recet gui`) opens this window.

Tabs: Job (a job workbook or the CET files, output folder, ACC version), Edit inputs (edit a small job's Measure and ProgramCost tables cell by cell and run
them: gui_editor.py), Supplied inputs (profiles, run parameters, SW allocation factors, ACC files -- each with a
"Template" button and a live check), Options (the modes), Reference data (the large files), Log (the running job's output) and Results (the finished run by PA at five levels,
with saved column views: gui_results.py). A header carries a light / dark switch; the Run / Stop bar stays at the bottom. The look is the Sun Valley theme (`sv-ttk`, Windows 11 style); without it
the window falls back to the system theme. Jobs run in a separate process, so the window stays responsive and the log streams live. Settings are remembered between
sessions and can be saved to / loaded from a JSON file (the same file `recet run --config` reads).

`App.to_config()` / `App.apply_config()` convert between the window and a `jobs.RunConfig`; everything else is layout.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from . import __version__, gui_editor, gui_results, paths, templates, versions
from .jobs import RunConfig

STATE_KEY = "gui"
THEME_KEY = "gui_theme"
INPUTS_KEY = "gui_job_inputs"            # the Job tab's input type and the paths of BOTH types (the hidden type's paths are kept, not run)
DOCS_DIR = Path(__file__).resolve().parent / "docs"
DOCUMENTS = [("User guide", "USER_GUIDE.md"), ("Input formats (supplied files)", "INPUT_FORMATS.md"), ("Input field reference", "INPUT_FIELDS.md"),
             ("Output field reference", "OUTPUT_FIELDS.md"), ("How every output is calculated", "CALCULATIONS.md")]
OK_COLOR, BAD_COLOR, MUTED = "#1a7f37", "#c62828", "#6b6b6b"
try:
    import sv_ttk
except ImportError:                       # optional at run time: the window still works with the system theme
    sv_ttk = None


def _check_file(name: str, path: str) -> tuple[bool, str]:
    """(usable, short description) for one chosen file. Runs in a worker thread."""
    from . import overrides, workbook
    p = Path(path)
    if not p.exists():
        return False, "file not found"
    try:
        if name in ("measures", "programcost"):
            head = p.read_text(encoding="utf-8-sig", errors="replace").split("\n", 1)[0]
            if "|" not in head:
                return False, "not pipe-delimited (CET files use '|')"
            need = ("CEInputID", "PrgID", "ClaimYearQuarter") if name == "measures" else ("PrgID", "PrgYear")
            cols = [c.strip() for c in head.split("|")]
            missing = [c for c in need if c not in cols]
            n = sum(1 for _ in open(p, encoding="utf-8-sig", errors="replace")) - 1
            return (False, f"missing {missing}") if missing else (True, f"{n:,} rows, {len(cols)} columns")
        if name == "workbook":
            found = workbook.sheet_names(p)
            if "Measure" not in found:
                return False, "no Measure sheet"
            return True, "sheets: " + ", ".join(found)
        if name == "profiles_8760_file":
            u = overrides.load_user_profiles(p)
            return True, f"{len(u)} profile(s)"
        if name == "profiles_gas_file":
            prof, _ = overrides.load_user_gas_profiles(p)
            return True, f"{len(prof)} profile(s)"
        if name == "run_parameters_file":
            t, _ = overrides.load_run_parameters(p)
            return True, "PAs: " + ", ".join(t.index)
        if name == "sw_factors_file":
            t, notes = overrides.load_sw_factors(p)
            return True, f"{len(t)} rows, {t['ProgramID'].nunique()} programs" + (" (see notes in the log)" if notes else "")
        if name == "programs_file":
            from . import programs
            t, notes = programs.load_programs(p)
            return True, f"{len(t):,} programs" + (" (see notes in the log)" if notes else "")
        if name == "acc_electric_file":
            chk = overrides.check_electric_acc(p)
            return chk.ok, (f"years {chk.info.get('years', '?')}" if chk.ok else "; ".join(chk.errors)[:200])
        if name == "acc_gas_file":
            chk = overrides.check_gas_acc(p)
            return chk.ok, (f"years {chk.info.get('years', '?')}" if chk.ok else "; ".join(chk.errors)[:200])
        if name == "cet_outputs":
            return True, f"{p.stat().st_size / 1e6:,.1f} MB"
    except Exception as exc:                                   # a bad file: show why, never crash the window
        return False, str(exc)
    return True, ""


class App:
    OK, WARN, BAD = OK_COLOR, "#9a6700", BAD_COLOR
    CHECKED = ("workbook", "measures", "programcost", "profiles_8760_file", "profiles_gas_file", "run_parameters_file", "sw_factors_file", "programs_file",
               "acc_electric_file", "acc_gas_file")

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"ReCET {__version__}")
        root.minsize(1060, 720)
        self.theme = paths.read_config().get(THEME_KEY, "light")
        self._apply_theme(self.theme)
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue()
        self.run_dir_hint: str = ""
        self.last_run_dir: Path | None = None
        self.v: dict[str, tk.Variable] = {}
        self.file_status: dict[str, ttk.Label] = {}
        self.file_detail: dict[str, str] = {}
        self._build()
        self.apply_config(RunConfig.from_dict(paths.read_config().get(STATE_KEY, {})))
        self._restore_job_inputs()
        if not self.v["reference_dir"].get():
            self.v["reference_dir"].set(str(paths.reference_dir() or ""))
        self._refresh_data_status()
        for name in self.CHECKED:
            if self.v[name].get().strip():
                self._start_check(name)
        root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._pump)

    # ------------------------------------------------------------------------------------------------------------------ theme
    def _apply_theme(self, theme: str) -> None:
        if sv_ttk is not None:
            sv_ttk.set_theme(theme)
        else:
            style = ttk.Style()
            style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 15))
        style.configure("Sub.TLabel", foreground=MUTED)
        style.configure("Hint.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Run.TButton", font=("Segoe UI Semibold", 10))
        if hasattr(self, "log"):
            self._style_text(self.log)
        if hasattr(self, "editor"):
            self.editor.set_theme(theme)
        if hasattr(self, "results_tab"):
            self.results_tab.set_theme(theme)

    def _style_text(self, widget) -> None:
        dark = self.theme == "dark"
        widget.config(background="#1c1c1c" if dark else "#fafafa", foreground="#e6e6e6" if dark else "#1f1f1f", insertbackground="#e6e6e6" if dark else "#1f1f1f",
                      relief="flat", borderwidth=0, highlightthickness=0)

    def _toggle_theme(self) -> None:
        self.theme = "dark" if self.theme == "light" else "light"
        self._apply_theme(self.theme)
        self.theme_btn.config(text="Light mode" if self.theme == "dark" else "Dark mode")
        try:
            paths.save_config(**{THEME_KEY: self.theme})
        except OSError:
            pass

    # ------------------------------------------------------------------------------------------------------------------ layout helpers
    def _var(self, name: str, kind=tk.StringVar, value=None):
        self.v[name] = kind(value=value) if value is not None else kind()
        return self.v[name]

    def _file_row(self, parent, row: int, label: str, name: str, *, save: bool = False, folder: bool = False, types=None, template: str | None = None,
                  optional: bool = True) -> list:
        """A labelled path box with Browse (and Template, and a live check). Returns the row's widgets."""
        widgets = [ttk.Label(parent, text=label)]
        widgets[0].grid(row=row, column=0, sticky="w", padx=(12, 8), pady=4)
        entry = ttk.Entry(parent, textvariable=self._var(name), width=56)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        widgets.append(entry)

        def browse():
            initial = self.v[name].get() or None
            if folder:
                p = filedialog.askdirectory(initialdir=initial, title=label)
            elif save:
                p = filedialog.asksaveasfilename(initialfile=initial, title=label)
            else:
                p = filedialog.askopenfilename(title=label, filetypes=types or [("All files", "*.*")])
            if p:
                self.v[name].set(p)
                if name == "reference_dir":
                    self._refresh_data_status()
                if name in self.CHECKED:
                    self._start_check(name)
        widgets.append(ttk.Button(parent, text="Browse…", command=browse))
        widgets[-1].grid(row=row, column=2, padx=6)
        col = 3
        if template:
            widgets.append(ttk.Button(parent, text="Template…", command=lambda: self._save_template(template)))
            widgets[-1].grid(row=row, column=3, padx=(0, 6))
            col = 4
        if name in self.CHECKED:
            entry.bind("<FocusOut>", lambda _e: self._start_check(name))
            entry.bind("<Return>", lambda _e: self._start_check(name))
            st = ttk.Label(parent, text="", width=34, cursor="hand2")
            st.grid(row=row, column=col, sticky="w", padx=(4, 12))
            st.bind("<Button-1>", lambda _e: self._show_check(name))
            self.file_status[name] = st
            widgets.append(st)
        parent.columnconfigure(1, weight=1)
        return widgets

    def _section(self, parent, text: str) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text=text, padding=(6, 8))
        f.pack(fill="x", padx=14, pady=(10, 4))
        return f

    # ------------------------------------------------------------------------------------------------------------------ layout
    def _build(self) -> None:
        root = self.root
        menubar = tk.Menu(root)
        m = tk.Menu(menubar, tearoff=0)
        m.add_command(label="Save all templates to a folder…", command=self._save_all_templates)
        m.add_command(label="Export a library 8,760 profile to edit…", command=self._export_profile)
        menubar.add_cascade(label="Templates", menu=m)
        m = tk.Menu(menubar, tearoff=0)
        m.add_command(label="Save settings…", command=self._save_settings)
        m.add_command(label="Load settings…", command=self._load_settings)
        menubar.add_cascade(label="Settings", menu=m)
        m = tk.Menu(menubar, tearoff=0)
        docs = tk.Menu(m, tearoff=0)
        for title, fname in DOCUMENTS:
            docs.add_command(label=title, command=lambda t=title, f=fname: self._show_doc(t, f))
        docs.add_separator()
        docs.add_command(label="Open the documentation folder", command=self._open_docs_folder)
        m.add_cascade(label="Documentation", menu=docs)
        m.add_command(label="About", command=lambda: messagebox.showinfo("About", f"recet {__version__}\nreference data: {paths.reference_dir_source()}"))
        menubar.add_cascade(label="Help", menu=m)
        root.config(menu=menubar)

        # header
        head = ttk.Frame(root, padding=(16, 12, 16, 4))
        head.pack(fill="x")
        ttk.Label(head, text="ReCET", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text=f"  version {__version__}", style="Sub.TLabel").pack(side="left", pady=(6, 0))
        self.theme_btn = ttk.Button(head, text="Light mode" if self.theme == "dark" else "Dark mode", command=self._toggle_theme)
        self.theme_btn.pack(side="right")

        # action bar (packed before the notebook so it always stays visible)
        bar = ttk.Frame(root, padding=(16, 8, 16, 12))
        bar.pack(side="bottom", fill="x")
        self.run_btn = ttk.Button(bar, text="Run", width=14, style="Accent.TButton" if sv_ttk else "Run.TButton", command=self._run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="Stop", width=10, command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.open_btn = ttk.Button(bar, text="Open results folder", command=self._open_results, state="disabled")
        self.open_btn.pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=180)
        self.progress.pack(side="right")
        self.status = ttk.Label(bar, text="Ready", style="Sub.TLabel")
        self.status.pack(side="right", padx=12)

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(4, 0))
        tabs = {}
        for key, title in (("job", "Job"), ("edit", "Edit inputs"), ("inputs", "Supplied inputs"), ("options", "Options"), ("data", "Reference data"), ("log", "Log"), ("results", "Results")):
            tabs[key] = ttk.Frame(self.nb, padding=(4, 4))
            self.nb.add(tabs[key], text=title)
        self.tabs = tabs

        # --- Job tab
        f = self._section(tabs["job"], "Job inputs - a job workbook, or the CET files")
        ttk.Label(f, text="Input type").grid(row=0, column=0, sticky="w", padx=(12, 8), pady=(2, 6))
        modes = ttk.Frame(f)
        modes.grid(row=0, column=1, columnspan=4, sticky="w", pady=(2, 6))
        self.input_mode = tk.StringVar(value="excel")
        for value, text in (("excel", "Excel job workbook (.xlsx)"), ("csv", "CET files (Measure.csv, ProgramCost.csv)")):
            ttk.Radiobutton(modes, text=text, value=value, variable=self.input_mode, command=self._show_input_mode).pack(side="left", padx=(0, 18))
        excel = self._file_row(f, 1, "Job workbook (.xlsx)", "workbook", template="workbook", types=[("Excel workbook", "*.xlsx *.xlsm"), ("All files", "*.*")])
        hint = ttk.Label(f, text="Sheets: Measure, ProgramCost, RunParameters, SWAllocation, Programs, GasProfiles, Profiles8760. A file chosen on the Supplied "
                                 "inputs tab wins over its sheet.", style="Hint.TLabel")
        hint.grid(row=2, column=1, columnspan=4, sticky="w", pady=(0, 6))
        csv = self._file_row(f, 3, "Measure.csv", "measures", types=[("CET Measure.csv", "*.csv *.txt"), ("All files", "*.*")])
        pc_row = self._file_row(f, 4, "ProgramCost.csv (optional)", "programcost", types=[("CET ProgramCost.csv", "*.csv *.txt"), ("All files", "*.*")])
        self._input_rows = {"excel": excel + [hint], "csv": csv + pc_row}
        # keep the path boxes in the same place whichever type is shown: the label column is as wide as its widest label (padx 12 + 8)
        f.columnconfigure(0, minsize=max(w.winfo_reqwidth() for w in (excel[0], csv[0], pc_row[0])) + 20)
        self._show_input_mode()
        f = self._section(tabs["job"], "Results")
        self._file_row(f, 0, "Output folder", "out_dir", folder=True, optional=False)
        ttk.Label(f, text="Run name (optional)").grid(row=1, column=0, sticky="w", padx=(12, 8), pady=4)
        ttk.Entry(f, textvariable=self._var("run_name"), width=30).grid(row=1, column=1, sticky="w", pady=4)
        f = self._section(tabs["job"], "ACC version")
        ttk.Label(f, text="Run against").grid(row=0, column=0, sticky="w", padx=(12, 8), pady=4)
        choices = [f"{k}  -  {label}" for k, label in versions.choices()]
        ttk.Combobox(f, textvariable=self._var("acc_version"), values=choices, width=46).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(f, text="(or type a new label and supply its files below)", style="Hint.TLabel").grid(row=0, column=2, sticky="w", padx=8)
        ttk.Label(f, text="First Year (blank = earliest claim year)").grid(row=1, column=0, sticky="w", padx=(12, 8), pady=4)
        ttk.Entry(f, textvariable=self._var("base_year"), width=8).grid(row=1, column=1, sticky="w", pady=4)

        # --- Edit inputs tab
        self.editor = gui_editor.EditorTab(self, tabs["edit"])

        # --- Supplied inputs tab
        f = self._section(tabs["inputs"], "Separate inputs (optional) - a match REPLACES the built-in data")
        self._file_row(f, 0, "8,760 electric profiles", "profiles_8760_file", template="profiles-8760", types=[("Profiles", "*.csv *.parquet"), ("All files", "*.*")])
        self._file_row(f, 1, "Gas monthly profiles", "profiles_gas_file", template="profiles-gas", types=[("Profiles", "*.csv *.parquet"), ("All files", "*.*")])
        self._file_row(f, 2, "Run parameters", "run_parameters_file", template="run-parameters", types=[("Run parameters", "*.csv *.xlsx"), ("All files", "*.*")])
        self._file_row(f, 3, "SW allocation factors", "sw_factors_file", template="sw-factors", types=[("SW factors", "*.csv *.xlsx"), ("All files", "*.*")])
        self._file_row(f, 4, "Program metadata", "programs_file", template="programs", types=[("Programs", "*.csv *.xlsx"), ("All files", "*.*")])
        self._file_row(f, 5, "Electric ACC file", "acc_electric_file", template="acc-electric", types=[("ACC", "*.parquet *.csv"), ("All files", "*.*")])
        self._file_row(f, 6, "Gas ACC file", "acc_gas_file", template="acc-gas", types=[("ACC", "*.parquet *.csv"), ("All files", "*.*")])
        ttk.Label(f, text="Run-parameters version for a NEW ACC label").grid(row=7, column=0, sticky="w", padx=(12, 8), pady=4)
        ttk.Entry(f, textvariable=self._var("e3_version"), width=8).grid(row=7, column=1, sticky="w", pady=4)
        ttk.Label(tabs["inputs"], text="Each file is checked when you choose it: click its status to see the details.", style="Hint.TLabel").pack(anchor="w", padx=18,
                                                                                                                                              pady=(4, 0))

        # --- Options tab
        holder = ttk.Frame(tabs["options"])
        holder.pack(fill="both", expand=True)
        left = self._section(holder, "Modes")
        left.pack_forget()
        left.pack(side="left", fill="both", expand=True, padx=(14, 6), pady=10)
        right = ttk.LabelFrame(holder, text="Comparisons and output", padding=(6, 8))
        right.pack(side="left", fill="both", expand=True, padx=(6, 14), pady=10)
        rows = {"left": 0, "right": 0}

        def check(name, text, value=False, hint="", where="left"):
            frame = left if where == "left" else right
            ttk.Checkbutton(frame, text=text, variable=self._var(name, tk.BooleanVar, value)).grid(row=rows[where], column=0, sticky="w", padx=10, pady=(6, 0))
            rows[where] += 1
            if hint:
                ttk.Label(frame, text=hint, style="Hint.TLabel", wraplength=420, justify="left").grid(row=rows[where], column=0, sticky="w", padx=40)
                rows[where] += 1

        def sub(frame, where):
            row = ttk.Frame(frame)
            row.grid(row=rows[where], column=0, sticky="w", padx=40, pady=(2, 4))
            rows[where] += 1
            return row

        check("components", "Calculate ACC components separately", hint="Prices energy, capacity, T&D, GHG ... (and gas market, T&D, CO2 ...) one by one; adds ElecValue_* / GasValue_* columns that sum to the totals.")
        check("portfolio_8760", "Produce combined 8760 electric load profile", hint="Portfolio net kWh by hour for the years below (default: First Year).")
        row = sub(left, "left")
        ttk.Label(row, text="Year(s):").pack(side="left")
        ttk.Entry(row, textvariable=self._var("profile_years"), width=22).pack(side="left", padx=6)
        check("market_effects", "Apply 5% market effects (benefits and costs)",
              hint="CET's Market Effects: NTG + 0.05 on benefits (electric, gas, water energy, other benefits, tax credits) and on costs (other costs, participant cost).")
        check("include_societal", "Include societal cost tests (SCB / SCH)", True, "Turn off for a run about 3x faster; the SCB/SCH columns are then blank.")
        check("emissions", "Include emissions (CO2 / NOx / PM10)", True)
        check("allocate_statewide", "Allocate statewide (SW_) programs across the IOUs", hint="Splits each statewide program's measures and costs by the allocation factors.")
        check("apply_exclude_from_ce", "Leave Exclude_From_CE programs out of the portfolio summary",
              hint="Uses the program metadata's Exclude_From_CE flag. Those measures are still priced and listed; the summary shows them in one 'excluded' row.")
        check("compare", "Also run another ACC version and compare measure by measure", where="right")
        row = sub(right, "right")
        ttk.Label(row, text="Compare with:").pack(side="left")
        ttk.Combobox(row, textvariable=self._var("compare_with"), values=[k for k, _ in versions.choices()], width=10).pack(side="left", padx=6)
        check("compare_cet", "Compare with a CET outputs file", where="right")
        row = sub(right, "right")
        ttk.Entry(row, textvariable=self._var("cet_outputs"), width=36).pack(side="left")
        ttk.Button(row, text="Browse…", command=lambda: self.v["cet_outputs"].set(filedialog.askopenfilename() or self.v["cet_outputs"].get())).pack(side="left", padx=6)
        check("preflight_only", "Preflight check only (do not price)", hint="Reads the inputs and reports what would and would not be priced.", where="right")
        row = sub(right, "right")
        ttk.Label(row, text="Output format:").pack(side="left")
        ttk.Combobox(row, textvariable=self._var("output_format", tk.StringVar, "csv"), values=["csv", "xlsx", "parquet"], width=8, state="readonly").pack(side="left", padx=6)

        # --- Reference data tab
        f = self._section(tabs["data"], "Reference data folder (the large ACC and profile files)")
        self._file_row(f, 0, "Folder", "reference_dir", folder=True)
        ttk.Button(f, text="Save as default", command=self._save_data_dir).grid(row=1, column=1, sticky="w", pady=4)
        self.data_status = ttk.Label(f, text="", wraplength=760, justify="left")
        self.data_status.grid(row=2, column=0, columnspan=4, sticky="w", padx=12, pady=(4, 6))

        # --- Log tab
        self.log = scrolledtext.ScrolledText(tabs["log"], height=20, font=("Consolas", 9), state="disabled", wrap="none")
        self.log.pack(fill="both", expand=True, padx=10, pady=10)
        self._style_text(self.log)

        # --- Results tab (gui_results.py: PA filter, levels, saved views)
        self.results_tab = gui_results.ResultsTab(self, tabs["results"])

    # ------------------------------------------------------------------------------------------------------------------ config <-> window
    def to_config(self) -> RunConfig:
        g = lambda n: self.v[n].get()
        blank = lambda s: (str(s).strip() or None)

        def int_or_none(s):
            s = str(s).strip()
            return int(s) if s else None

        years = [int(t) for t in str(g("profile_years")).replace(",", " ").split() if t.strip()]
        acc_text = str(g("acc_version")).strip()
        excel = self.input_mode.get() == "excel"           # only the selected input type is run; the other type's boxes keep their paths
        return RunConfig(
            measures="" if excel else str(g("measures")).strip(), programcost=None if excel else blank(g("programcost")),
            workbook=blank(g("workbook")) if excel else None,
            out_dir=str(g("out_dir")).strip() or "recet_results", run_name=str(g("run_name")).strip(),
            acc_version=acc_text.split()[0] if acc_text else "2024",
            acc_electric_file=blank(g("acc_electric_file")), acc_gas_file=blank(g("acc_gas_file")), e3_version=int_or_none(g("e3_version")),
            profiles_8760_file=blank(g("profiles_8760_file")), profiles_gas_file=blank(g("profiles_gas_file")), reference_dir=blank(g("reference_dir")),
            run_parameters_file=blank(g("run_parameters_file")), sw_factors_file=blank(g("sw_factors_file")), programs_file=blank(g("programs_file")),
            components=bool(g("components")), portfolio_8760=bool(g("portfolio_8760")), profile_years=years,
            market_effects=0.05 if g("market_effects") else 0.0,
            include_societal=bool(g("include_societal")), emissions=bool(g("emissions")), allocate_statewide=bool(g("allocate_statewide")),
            apply_exclude_from_ce=bool(g("apply_exclude_from_ce")), base_year=int_or_none(g("base_year")),
            cet_outputs=blank(g("cet_outputs")) if g("compare_cet") else None,
            compare_with=(blank(g("compare_with")) if g("compare") else None), output_format=str(g("output_format")) or "csv", preflight_only=bool(g("preflight_only")))

    def apply_config(self, c: RunConfig) -> None:
        """Fill the window from a RunConfig. The input type follows what the config names (a workbook only -> Excel, a Measure.csv -> CET files); the boxes of
        the type the config does not use keep their current paths."""
        s = lambda n, val: self.v[n].set("" if val is None else val)
        if c.measures:
            s("measures", c.measures), s("programcost", c.programcost)
            self._set_input_mode("csv")
        elif c.workbook:
            s("workbook", c.workbook)
            self._set_input_mode("excel")
        s("out_dir", c.out_dir if c.out_dir != "recet_results" else str(Path.home() / "recet_results"))
        s("run_name", c.run_name)
        label = dict(versions.choices()).get(c.acc_version)
        s("acc_version", f"{c.acc_version}  -  {label}" if label else c.acc_version)
        s("base_year", c.base_year), s("acc_electric_file", c.acc_electric_file), s("acc_gas_file", c.acc_gas_file), s("e3_version", c.e3_version)
        s("profiles_8760_file", c.profiles_8760_file), s("profiles_gas_file", c.profiles_gas_file), s("reference_dir", c.reference_dir)
        s("run_parameters_file", c.run_parameters_file), s("sw_factors_file", c.sw_factors_file), s("programs_file", c.programs_file)
        s("components", c.components), s("portfolio_8760", c.portfolio_8760), s("profile_years", " ".join(map(str, c.profile_years)))
        s("market_effects", bool(c.market_effects)), s("include_societal", c.include_societal)
        s("emissions", c.emissions), s("allocate_statewide", c.allocate_statewide), s("apply_exclude_from_ce", c.apply_exclude_from_ce)
        s("compare", bool(c.compare_with)), s("compare_with", c.compare_with or ("2026" if c.acc_version == "2024" else "2024"))
        s("compare_cet", bool(c.cet_outputs)), s("cet_outputs", c.cet_outputs), s("output_format", c.output_format), s("preflight_only", c.preflight_only)

    # ------------------------------------------------------------------------------------------------------------------ input type
    def _set_input_mode(self, mode: str) -> None:
        self.input_mode.set(mode)
        self._show_input_mode()

    def _show_input_mode(self) -> None:
        mode = self.input_mode.get()
        for key, widgets in self._input_rows.items():
            for w in widgets:
                w.grid() if key == mode else w.grid_remove()

    def _restore_job_inputs(self) -> None:
        """The input type and both types' paths as they were when the window last closed (a run's config holds only the selected type's)."""
        saved = paths.read_config().get(INPUTS_KEY) or {}
        for name in ("workbook", "measures", "programcost"):
            if saved.get(name):
                self.v[name].set(saved[name])
        if saved.get("mode") in ("excel", "csv"):
            self._set_input_mode(saved["mode"])

    # ------------------------------------------------------------------------------------------------------------------ file checks
    def _start_check(self, name: str) -> None:
        path = self.v[name].get().strip()
        label = self.file_status.get(name)
        if label is None:
            return
        if not path:
            label.config(text="")
            self.file_detail.pop(name, None)
            return
        label.config(text="checking…", foreground=MUTED)
        if name == "reference_dir":
            return
        paths.set_reference_dir(self.v["reference_dir"].get().strip() or None)
        threading.Thread(target=lambda: self.q.put(("check", (name, path, *_check_file(name, path)))), daemon=True).start()

    def _show_check(self, name: str) -> None:
        if name in self.file_detail:
            messagebox.showinfo("File check", self.file_detail[name])

    # ------------------------------------------------------------------------------------------------------------------ actions
    def _refresh_data_status(self) -> None:
        d = self.v["reference_dir"].get().strip()
        paths.set_reference_dir(d or None)
        missing = paths.missing_required()
        if missing:
            txt = "✗  Missing from the reference folder: " + ", ".join(missing) + "  (see `recet data status`; a run using the built-in ACC or profile library needs them)"
        else:
            txt = "✓  All required reference files found" + (f"  ({paths.reference_dir_source()})" if paths.reference_dir() else "  (using the research checkout's files)")
        self.data_status.config(text=txt, foreground=BAD_COLOR if missing else OK_COLOR)

    def _save_data_dir(self) -> None:
        d = self.v["reference_dir"].get().strip()
        if d:
            paths.save_config(data_dir=d)
            self._refresh_data_status()
            messagebox.showinfo("Saved", f"{d} will be used as the default reference data folder on this machine.")

    def _save_template(self, kind: str) -> None:
        names = {"profiles-8760": "template_electric_8760_profiles.csv", "profiles-gas": "template_gas_monthly_profiles.csv",
                 "acc-electric": "template_acc_electric.csv", "acc-gas": "template_acc_gas.csv", "run-parameters": "template_run_parameters.csv",
                 "sw-factors": "template_sw_allocation_factors.csv", "programs": "template_programs.csv", "workbook": "template_job_workbook.xlsx"}
        types = [("Excel workbook", "*.xlsx")] if kind == "workbook" else [("CSV", "*.csv"), ("Parquet", "*.parquet")]
        p = filedialog.asksaveasfilename(title="Save template as", initialfile=names[kind], defaultextension=types[0][1][1:], filetypes=types)
        if not p:
            return
        self._refresh_data_status()
        try:
            key = self.v["acc_version"].get().split()[0]
            out = templates.write_template(kind, p, key if key in versions.BUILTIN else "2026")
        except Exception as exc:
            messagebox.showerror("Template", str(exc))
            return
        messagebox.showinfo("Template saved", f"{out}\n\nFill it in, then choose it in the matching box. Details: Help > Documentation > Input formats.")

    def _save_all_templates(self) -> None:
        d = filedialog.askdirectory(title="Folder for the templates")
        if d:
            self._refresh_data_status()
            files = templates.write_all(d)
            messagebox.showinfo("Templates saved", "\n".join(str(f) for f in files))

    def _export_profile(self) -> None:
        end_use = simpledialog.askstring("Export library profile", "End use (E3MeaElecEndUseShape), exactly as in Measure.csv:", parent=self.root)
        if not end_use:
            return
        pa = simpledialog.askstring("Export library profile", "IOU (PGE, SCE, SDGE or SCG):", parent=self.root)
        sector = simpledialog.askstring("Export library profile", "Target sector (E3TargetSector), e.g. Residential:", parent=self.root)
        if not (pa and sector):
            return
        p = filedialog.asksaveasfilename(title="Save profile as", defaultextension=".csv", initialfile="library_profile_to_edit.csv")
        if not p:
            return
        try:
            self._refresh_data_status()
            out = templates.export_library_profile(end_use, pa, sector, p)
        except Exception as exc:
            messagebox.showerror("Export library profile", str(exc))
            return
        messagebox.showinfo("Saved", f"{out}\n\nEdit the impact column, then supply the file as '8,760 electric profiles': it replaces the library profile.")

    def _show_doc(self, title: str, filename: str) -> None:
        """A documentation page (Markdown, shown as text) in its own window."""
        doc = DOCS_DIR / filename
        win = tk.Toplevel(self.root)
        win.title(title)
        txt = scrolledtext.ScrolledText(win, width=130, height=44, font=("Consolas", 9), wrap="word")
        txt.pack(fill="both", expand=True)
        self._style_text(txt)
        txt.insert("1.0", doc.read_text(encoding="utf-8") if doc.exists() else f"{filename} not found in {DOCS_DIR}")
        txt.config(state="disabled")

    def _open_docs_folder(self) -> None:
        if DOCS_DIR.exists():
            os.startfile(DOCS_DIR) if hasattr(os, "startfile") else subprocess.Popen(["xdg-open", str(DOCS_DIR)])

    def _save_settings(self) -> None:
        p = filedialog.asksaveasfilename(title="Save settings", defaultextension=".json", filetypes=[("JSON", "*.json")], initialfile="recet_job.json")
        if p:
            Path(p).write_text(self.to_config().to_json(), encoding="utf-8")

    def _load_settings(self) -> None:
        p = filedialog.askopenfilename(title="Load settings", filetypes=[("JSON", "*.json")])
        if p:
            self.apply_config(RunConfig.from_dict(json.loads(Path(p).read_text(encoding="utf-8"))))
            self._refresh_data_status()
            for name in self.CHECKED:
                self._start_check(name)

    def _append(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _run(self, preflight_only: bool = False) -> None:
        if self.proc is not None:
            return
        cfg = self.to_config()
        editing = self.editor.loaded and self.editor.use_var.get()
        if not editing:
            if not cfg.measures and not cfg.workbook:
                what = "a job workbook" if self.input_mode.get() == "excel" else "a Measure.csv file"
                messagebox.showwarning("Missing input", f"Choose {what} on the Job tab first (or switch the input type, or edit a job on the Edit inputs tab).")
                return
            for label, f in (("Job workbook", cfg.workbook), ("Measure file", cfg.measures)):
                if f and not Path(f).exists():
                    messagebox.showwarning("Missing input", f"{label} not found:\n{f}")
                    return
        self._remember(cfg)
        if preflight_only:
            cfg.preflight_only = True
        cfg = self.editor.apply_to(cfg) if editing else cfg
        if cfg is None:
            return
        out = Path(cfg.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        cfg_path = out / "_gui_job.json"
        cfg_path.write_text(cfg.to_json(), encoding="utf-8")
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self._append(f"recet {__version__}  -  {'preflight' if cfg.preflight_only else 'run'}  -  ACC {cfg.acc_version}"
                     + (f"  vs  {cfg.compare_with}" if cfg.compare_with else ""))
        if editing:
            self._append(f"inputs: the edited tables from the Edit inputs tab, saved in {Path(cfg.measures).parent}")
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-m", "recet", "run", "--config", str(cfg_path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.run_dir_hint = str(out)
        self.last_run_dir = None
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.open_btn.config(state="disabled")
        self.status.config(text="Running…")
        self.progress.start(12)
        self.nb.select(self.tabs["log"])

    def _reader(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.q.put(("line", line.rstrip("\n")))
        self.q.put(("done", proc.wait()))

    def _pump(self) -> None:
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "line":
                    self._append(val)
                    m = re.search(r"(?:results in|comparison files in) (.+)$", val)
                    if m:
                        self.last_run_dir = Path(m.group(1).strip())
                elif kind == "check":
                    name, path, ok, text = val
                    if self.v[name].get().strip() == path and name in self.file_status:
                        self.file_status[name].config(text=("✓  " if ok else "✗  ") + (text if len(text) < 44 else text[:42] + "…"),
                                                      foreground=OK_COLOR if ok else BAD_COLOR)
                        self.file_detail[name] = f"{path}\n\n{'Usable' if ok else 'Not usable'}: {text}"
                else:
                    self.progress.stop()
                    self.run_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.open_btn.config(state="normal")
                    self.status.config(text="Finished" if val == 0 else f"Stopped/failed (exit {val})")
                    self._append("\n" + ("finished." if val == 0 else f"exit code {val} - see the messages above"))
                    self.proc = None
                    if val == 0 and self.last_run_dir is not None and self._show_results(self.last_run_dir):
                        self.nb.select(self.tabs["results"])
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    # ------------------------------------------------------------------------------------------------------------------ results
    def _show_results(self, run_dir: Path) -> bool:
        """Show a run folder in the Results tab (gui_results.py). False if it holds neither a measure-level table nor a comparison summary."""
        return self.results_tab.load(Path(run_dir))

    def _choose_results(self) -> None:
        d = filedialog.askdirectory(title="A run folder (with measure_level_*)")
        if d and not self._show_results(Path(d)):
            messagebox.showwarning("Results", "No measure_level_* or comparison_summary_* file in that folder.")

    def _open_measure_table(self) -> None:
        d = self.last_run_dir
        files = sorted(d.glob("measure_level_*.*")) if d else []
        if not files:
            messagebox.showinfo("Results", "No measure-level table yet: run a job or load a results folder.")
            return
        os.startfile(files[0]) if hasattr(os, "startfile") else subprocess.Popen(["xdg-open", str(files[0])])

    def _stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self._append("stopping…")

    def _open_results(self) -> None:
        d = str(self.last_run_dir) if self.last_run_dir else (self.run_dir_hint or self.v["out_dir"].get())
        if d and Path(d).exists():
            os.startfile(d) if hasattr(os, "startfile") else subprocess.Popen(["xdg-open", d])

    def _remember(self, cfg: RunConfig) -> None:
        try:
            paths.save_config(**{STATE_KEY: json.loads(cfg.to_json()),
                                 INPUTS_KEY: {"mode": self.input_mode.get(), **{n: self.v[n].get() for n in ("workbook", "measures", "programcost")}}})
        except OSError:
            pass

    def _close(self) -> None:
        if self.proc is not None and not messagebox.askyesno("Run in progress", "A run is still going. Stop it and close?"):
            return
        if getattr(self.editor, "dirty", False) and not messagebox.askyesno(
                "Unsaved edits", "The Edit inputs tab has unsaved edits. Close anyway?\n\n(Each run keeps a copy of the tables it used in <output folder>/_edited_inputs.)"):
            return
        self._stop()
        try:
            self._remember(self.to_config())
        except Exception:
            pass
        self.root.destroy()


def launch() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    launch()
