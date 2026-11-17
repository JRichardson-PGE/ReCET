"""The window's output-template editor (Outputs tab, Edit templates…): a two-list field picker over output_templates.catalog().

* Top: Template (a saved template, a built-in to copy, or a new name), Delete template, and the default for new runs.
* Left: Show (all fields, input fields, program fields, output fields, or one output group) and a search box over names and definitions; the fields of
  that choice not yet in the template, by group.
* Middle: Add, Add shown, Remove, Remove all. Right: the template's columns in file order, with Top, Up, Down and Bottom.
* Bottom: the definition of the last field clicked, Save template, Close.

Double-click moves a field across; Enter adds, Delete removes, Alt+Up / Alt+Down move. Built-in templates cannot be changed: save a copy under another name.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from . import output_templates as ot

ALL_FIELDS = "All fields"


class TemplateEditor:
    def __init__(self, app, on_change):
        """`app` is the main window (theme, the Output template box); on_change(name) is called after a save, delete or default change with the template
        the run box should show."""
        self.app, self.on_change = app, on_change
        self.fields = ot.catalog()
        self.by_name = {c: (g, d) for g, c, d, _l in self.fields}
        self.chosen: list[str] = []
        self.shown_avail: list[str] = []
        self.shown_sel: list[str] = []
        self.win = top = tk.Toplevel(app.root)
        top.title("Output templates")
        top.transient(app.root)
        top.geometry("980x640")
        top.minsize(760, 480)
        dark = app.theme == "dark"
        lb = dict(background="#1c1c1c" if dark else "#ffffff", foreground="#e6e6e6" if dark else "#1f1f1f", selectbackground="#2f5d8a" if dark else "#cfe3f7",
                  selectforeground="#ffffff" if dark else "#000000", activestyle="none", selectmode="extended", exportselection=False, font=("Consolas", 9))
        self.group_fg = "#9a9a9a" if dark else "#6b6b66"

        row = ttk.Frame(top, padding=(12, 10, 12, 2))
        row.pack(fill="x")
        ttk.Label(row, text="Template:").pack(side="left")
        saved = ot.load_saved()
        start = app.v["output_template"].get()
        self.name = tk.StringVar(value=start if start in saved else "")
        self.name_box = ttk.Combobox(row, textvariable=self.name, values=list(saved), width=32)
        self.name_box.pack(side="left", padx=6)
        self.name_box.bind("<<ComboboxSelected>>", lambda _e: self._load(self.name.get()))
        self.del_btn = ttk.Button(row, text="Delete template", command=self._delete)
        self.del_btn.pack(side="left")
        ttk.Label(row, text="Default for new runs:").pack(side="left", padx=(24, 0))
        self.default = tk.StringVar(value=ot.default_name(saved))
        self.default_box = ttk.Combobox(row, textvariable=self.default, values=[ot.ALL, *saved], width=24, state="readonly")
        self.default_box.pack(side="left", padx=6)
        self.default_box.bind("<<ComboboxSelected>>", lambda _e: self._set_default())
        ttk.Label(top, text="Type a new name to start a template; a built-in (Key results, Participant costs) can be copied under another name.",
                  style="Hint.TLabel", padding=(12, 0)).pack(fill="x")

        body = ttk.Frame(top, padding=(12, 6, 12, 4))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, uniform="lists")
        body.columnconfigure(2, weight=1, uniform="lists")
        body.rowconfigure(1, weight=1)

        left_head = ttk.Frame(body)
        left_head.grid(row=0, column=0, sticky="ew")
        left_head.columnconfigure(1, weight=1)
        ttk.Label(left_head, text="Show:").grid(row=0, column=0, sticky="w")
        groups = list(dict.fromkeys(g for g, *_ in self.fields if ot.kind(g) == "Output fields"))
        self.kind = tk.StringVar(value=ALL_FIELDS)
        kind_box = ttk.Combobox(left_head, textvariable=self.kind, state="readonly", values=[ALL_FIELDS, *ot.KINDS, *[f"   {g}" for g in groups]])
        kind_box.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        kind_box.bind("<<ComboboxSelected>>", lambda _e: self._fill_avail())
        self.find = tk.StringVar()
        ttk.Label(left_head, text="Search:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(left_head, textvariable=self.find).grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(4, 0))
        self.find.trace_add("write", lambda *_: self._fill_avail())

        right_head = ttk.Frame(body)
        right_head.grid(row=0, column=2, columnspan=2, sticky="sew")
        ttk.Label(right_head, text="Template columns, in file order").pack(side="left")
        self.sel_count = ttk.Label(right_head, text="", style="Hint.TLabel")
        self.sel_count.pack(side="right")

        self.avail = self._listbox(body, 0, lb)
        self.sel = self._listbox(body, 2, lb)
        self.avail_count = ttk.Label(body, text="", style="Hint.TLabel")
        self.avail_count.grid(row=2, column=0, sticky="w")

        mid = ttk.Frame(body, padding=(8, 24, 8, 0))
        mid.grid(row=1, column=1, sticky="n")
        for text, cmd in (("Add →", self._add), ("Add shown »", self._add_shown), ("← Remove", self._remove), ("« Remove all", self._clear)):
            ttk.Button(mid, text=text, command=cmd, width=13).pack(pady=3)
        moves = ttk.Frame(body, padding=(8, 24, 0, 0))
        moves.grid(row=1, column=3, sticky="n")
        for text, how in (("Top", "top"), ("Up", "up"), ("Down", "down"), ("Bottom", "bottom")):
            ttk.Button(moves, text=text, command=lambda h=how: self._move(h), width=8).pack(pady=3)

        self.avail.bind("<Double-1>", lambda _e: self._add())
        self.avail.bind("<Return>", lambda _e: self._add())
        self.sel.bind("<Double-1>", lambda _e: self._remove())
        self.sel.bind("<Delete>", lambda _e: self._remove())
        self.sel.bind("<Alt-Up>", lambda _e: (self._move("up"), "break")[1])
        self.sel.bind("<Alt-Down>", lambda _e: (self._move("down"), "break")[1])
        self.avail.bind("<<ListboxSelect>>", lambda _e: self._define(self.avail, self.shown_avail))
        self.sel.bind("<<ListboxSelect>>", lambda _e: self._define(self.sel, self.shown_sel))

        self.definition = ttk.Label(top, text="Click a field to see its definition.", style="Hint.TLabel", wraplength=940, justify="left", padding=(12, 2))
        self.definition.pack(fill="x")
        bottom = ttk.Frame(top, padding=(12, 6, 12, 12))
        bottom.pack(fill="x")
        self.make_default = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text="Use for new runs (default)", variable=self.make_default).pack(side="left")
        ttk.Button(bottom, text="Close", command=top.destroy).pack(side="right")
        ttk.Button(bottom, text="Save template", command=self._save).pack(side="right", padx=6)
        self._load(self.name.get())

    def _listbox(self, parent, column: int, colors: dict) -> tk.Listbox:
        frame = ttk.Frame(parent)
        frame.grid(row=1, column=column, sticky="nsew", pady=(4, 2))
        box = tk.Listbox(frame, **colors)
        bar = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=bar.set)
        box.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return box

    # ------------------------------------------------------------------------------------------------------------------ lists
    def _load(self, name: str) -> None:
        self.chosen = list(ot.load_saved().get(name, []))
        builtin = name in ot.BUILTIN
        self.del_btn.config(state="disabled" if builtin or not name else "normal")
        self.make_default.set(bool(name) and not builtin and ot.default_name() == name)
        self._fill()

    def _fill(self, keep: list[str] | None = None) -> None:
        self.sel.delete(0, "end")
        self.shown_sel = list(self.chosen)
        for i, c in enumerate(self.chosen):
            self.sel.insert("end", c)
            if c not in self.by_name and not c.startswith(("ElecValue_", "GasValue_")):
                self.sel.itemconfig(i, foreground="#c2410c")
            if keep and c in keep:
                self.sel.selection_set(i)
        n = len(self.chosen)
        self.sel_count.config(text=f"{n} column{'' if n == 1 else 's'}")
        self._fill_avail()

    def _fill_avail(self) -> None:
        kind, q = self.kind.get().strip(), self.find.get().strip().lower()
        taken = set(self.chosen)
        self.avail.delete(0, "end")
        self.shown_avail, group, count = [], None, 0
        for g, c, d, _l in self.fields:
            if c in taken or (q and q not in c.lower() and q not in d.lower()):
                continue
            if kind in ot.KINDS and ot.kind(g) != kind or kind not in ot.KINDS and kind != ALL_FIELDS and g != kind:
                continue
            if g != group:                                   # a group heading (not selectable: _picked skips it)
                self.avail.insert("end", f"{g}")
                self.avail.itemconfig("end", foreground=self.group_fg, selectforeground=self.group_fg)
                self.shown_avail.append("")
                group = g
            self.avail.insert("end", f"   {c}")
            self.shown_avail.append(c)
            count += 1
        self.avail_count.config(text=f"{count} field{'' if count == 1 else 's'} shown")

    @staticmethod
    def _picked(box: tk.Listbox, names: list[str]) -> list[str]:
        return [names[i] for i in box.curselection() if names[i]]

    def _define(self, box: tk.Listbox, names: list[str]) -> None:
        picked = self._picked(box, names)
        if picked:
            c = picked[-1]
            g, d = self.by_name.get(c, ("", ""))
            self.definition.config(text=f"{c} ({g}): {d}" if g else f"{c}: not in the field list (a per-component column, or a name ReCET does not write).")

    def _add(self, names: list[str] | None = None) -> None:
        names = self._picked(self.avail, self.shown_avail) if names is None else names
        self.chosen += [c for c in names if c not in self.chosen]
        self._fill(names)
        if names:
            self.sel.see(self.chosen.index(names[-1]))

    def _add_shown(self) -> None:
        self._add([c for c in self.shown_avail if c])

    def _remove(self) -> None:
        drop = set(self._picked(self.sel, self.shown_sel))
        self.chosen = [c for c in self.chosen if c not in drop]
        self._fill()

    def _clear(self) -> None:
        if self.chosen and messagebox.askyesno("Output templates", "Remove every column from the template?", parent=self.win):
            self.chosen = []
            self._fill()

    def _move(self, how: str) -> None:
        names = self._picked(self.sel, self.shown_sel)
        if not names:
            return
        self.chosen = move(self.chosen, names, how)
        self._fill(names)
        self.sel.see(self.chosen.index(names[0] if how in ("top", "up") else names[-1]))

    # ------------------------------------------------------------------------------------------------------------------ saving
    def _refresh(self, keep: str) -> None:
        saved = ot.load_saved()
        self.name_box.configure(values=list(saved))
        self.default_box.configure(values=[ot.ALL, *saved])
        self.default.set(ot.default_name(saved))
        self.on_change(keep)

    def _save(self) -> None:
        cols, bad = ot.check(self.chosen)
        n = self.name.get().strip()
        if bad:
            messagebox.showerror("Output templates", f"Not columns ReCET writes: {', '.join(bad)}", parent=self.win)
        elif not n or not cols:
            messagebox.showerror("Output templates", "Give the template a name and at least one column.", parent=self.win)
        else:
            try:
                ot.save(n, cols)
            except ValueError as exc:
                messagebox.showerror("Output templates", f"{exc}: save it under another name.", parent=self.win)
                return
            if self.make_default.get():
                ot.set_default(n)
            self.del_btn.config(state="normal")
            self._refresh(n)

    def _delete(self) -> None:
        try:
            ot.save(self.name.get().strip(), None)
        except ValueError as exc:
            messagebox.showerror("Output templates", str(exc), parent=self.win)
            return
        self.name.set("")
        self._refresh(ot.default_name())
        self._load("")

    def _set_default(self) -> None:
        ot.set_default(self.default.get())
        self.make_default.set(self.default.get() == self.name.get().strip())
        self._refresh(self.default.get())


def move(columns: list[str], names: list[str], how: str) -> list[str]:
    """columns with `names` moved: "top" / "bottom" (kept in their order), or "up" / "down" one place each (a block stops at either end)."""
    cols, picked = list(columns), [c for c in columns if c in names]
    if how in ("top", "bottom"):
        rest = [c for c in cols if c not in names]
        return picked + rest if how == "top" else rest + picked
    step = -1 if how == "up" else 1
    idx = [cols.index(c) for c in picked]
    for i in (idx if step < 0 else reversed(idx)):
        j = i + step
        if 0 <= j < len(cols) and cols[j] not in names:
            cols[i], cols[j] = cols[j], cols[i]
    return cols
