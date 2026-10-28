"""Output templates (BRD BR-06.10): a named list of measure-level columns, in order. A run given one writes only those columns, in that order, to its
measure-level file (RunConfig.output_template); the Results tables still read every column (the run keeps the full table in its _results folder).

    catalog()                    every column a template can name, grouped as OUTPUT_FIELDS.md groups them, with its definition (field_docs)
    BUILTIN                      the templates that ship with ReCET
    check(columns)               (known, unknown) names
    read_file(path)              a template saved as a file: JSON {"name": ..., "columns": [...]}, or text with one column per line
    load_saved() / save(...)     the window's saved templates (the tool's config file); the web app keeps each user's in its database
    default_name() / set_default the template new runs start with (the window's config file; the web app keeps each user's in its database)
    KINDS, kind(group)           the field picker's categories: input, program and output fields (output by OUTPUT_FIELDS.md group)
    current(columns)             a template's columns under the names a run writes now (columns renamed in 0.1.50 accepted by their old names)

A column the run did not produce (an SCT column with the Societal Cost Tests off, a component column without components mode) is left out of the file, and
the run says so.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from . import field_docs, paths, templates

CONFIG_KEY = "output_templates"
DEFAULT_KEY = "default_output_template"
ALL = "All columns"
KINDS = ("Input fields", "Program fields", "Output fields")   # the field picker's categories (kind()); each output group can also be picked on its own
BUILTIN: dict[str, list[str]] = {
    "Key results": ["CEInputID", "PrgID", "MeasDescription", "ClaimYearQuarter", "IOU_AC_Territory", "Used_ACCVersion", "ElecPricingStatus", "GasPricingStatus",
                    "TotalSystemBenefit", "ElecBen", "GasBen", "OtherBen", "TRCCost", "PACCost", "TRCRatio", "PACRatio", "RIMRatio", "SCBRatio", "SCHRatio",
                    "FirstYearNetKWh", "FirstYearNetKW", "FirstYearNetThm", "LifecycleNetElecCO2", "LifecycleNetGasCO2", "CreditedPA"],
    "Participant costs": ["CEInputID", "PrgID", "MeasDescription", "ClaimYearQuarter", "GrossMeasureCost", "GrossMeasCostPV", "ExcessIncentives",
                          "ExcessIncentivesPV", "GrossParticipantCost", "GrossParticipantCostAdjustedPV", "NetParticipantCost", "NetParticipantCostAdjusted",
                          "NetParticipantCostPV", "RebatesandIncents", "RebatesandIncentsPV", "TRCCost", "TRCRatio"],
}


@lru_cache(maxsize=None)
def catalog() -> list[tuple[str, str, str, str]]:
    """(group, column, definition, documentation link) for every measure-level column, in the order a run writes them: the input columns, then each
    OUTPUT_FIELDS.md section 1 group. Per-component columns (ElecValue_<component>) are not listed: they depend on the ACC."""
    out = [("Input columns", c, *_def(c)) for c in ["Used_ACCVersion", *templates.MEASURE_COLUMNS]]
    text = (field_docs.DOCS / "OUTPUT_FIELDS.md").read_text(encoding="utf-8")
    text = text[text.index("## 1. "):text.index("## 2. ")]
    group, seen = "", {c for _g, c, _d, _l in out}
    for line in text.splitlines():
        m = re.match(r"^### \d+\.\d+ (.+)$", line)
        if m:
            group = re.sub(r"\s*\(.*$", "", re.sub(r"`", "", m.group(1))).strip()
            continue
        names = re.findall(r"`([^`]+)`", line[5:]) if line.startswith("#### ") else (
            re.findall(r"`([^`]+)`", line.split("|")[1]) if line.startswith("|") and not re.match(r"^\|\s*-", line) else [])
        for n in names:
            if n in seen or "<" in n or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", n) or n.startswith(("ElecValue_", "GasValue_")):
                continue
            if group.startswith("Used_ACCVersion") or n in templates.MEASURE_COLUMNS:
                continue
            seen.add(n)
            out.append((group, n, *_def(n)))
    return out


def _def(column: str) -> tuple[str, str]:
    d = field_docs.definition(column)
    return (d.text, d.link) if d else ("", "")


def kind(group: str) -> str:
    """The picker category of a catalog group: the input columns, the program metadata (Prg_*) or an output group."""
    return "Input fields" if group == "Input columns" else "Program fields" if group == "Program metadata" else "Output fields"


def names() -> set[str]:
    return {c for _g, c, _d, _l in catalog()}


def current(columns) -> list[str]:
    """The columns under the names a run writes now, in order and without repeats: an old name (NetElecCO2, CreditedIOU ...) becomes the new one and a column
    ReCET no longer writes (poc_PA, ACCCheck ...) is dropped (fieldnames.output_name)."""
    from .fieldnames import output_name
    out = []
    for c in columns:
        n = output_name(c)
        if n and n not in out:
            out.append(n)
    return out


def check(columns) -> tuple[list[str], list[str]]:
    """(the names a template may use, in order and without repeats; the ones it may not). ElecValue_ / GasValue_ component columns are allowed, and an old
    name of a renamed column is taken as its new name; a column no longer written is not allowed."""
    from .fieldnames import OUTPUT_RENAMES
    ok, bad, known = [], [], names()
    for c in columns:
        c = str(c).strip()
        c = OUTPUT_RENAMES.get(c, c)
        if not c or c in ok:
            continue
        (ok if c in known or c.startswith(("ElecValue_", "GasValue_")) else bad).append(c)
    return ok, bad


def parse(text: str) -> list[str]:
    """Column names from text: one per line, or separated by commas; blank lines and '#' comments ignored."""
    out = []
    for line in str(text).splitlines():
        line = line.split("#", 1)[0]
        out += [c.strip() for c in line.split(",") if c.strip()]
    return out


def read_file(path: str | Path) -> tuple[str, list[str]]:
    """(name, columns) of a template file: JSON {"name": ..., "columns": [...]}, or text (one column per line; the name is the file's stem)."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() == ".json":
        d = json.loads(raw)
        return str(d.get("name") or p.stem), [str(c) for c in d.get("columns", [])]
    return p.stem, parse(raw)


def load_saved() -> dict[str, list[str]]:
    """The window's saved templates (the tool's config file), built-ins first."""
    saved = paths.read_config().get(CONFIG_KEY) or {}
    out = dict(BUILTIN)
    for name, cols in saved.items():
        if name not in BUILTIN and isinstance(cols, list):
            out[str(name)] = current(cols)
    return out


def save(name: str, columns: list[str] | None) -> None:
    """Save (or with columns None, delete) one of the window's templates. Built-ins cannot be changed."""
    if name in BUILTIN or name == ALL:
        raise ValueError(f"{name!r} is a built-in template")
    saved = {k: v for k, v in (paths.read_config().get(CONFIG_KEY) or {}).items() if k != name}
    if columns is not None:
        saved[name] = list(columns)
    paths.save_config(**{CONFIG_KEY: saved})


def default_name(saved: dict[str, list[str]] | None = None) -> str:
    """The window's default template for new runs: ALL unless one was set and still exists. (Until a default is set, the window keeps the template of
    its last run.)"""
    name = str(paths.read_config().get(DEFAULT_KEY) or "")
    return name if name in (load_saved() if saved is None else saved) else ALL


def set_default(name: str) -> None:
    """Make a template (or ALL) the one new runs start with."""
    if name != ALL and name not in load_saved():
        raise ValueError(f"no template named {name!r}")
    paths.save_config(**{DEFAULT_KEY: name})
