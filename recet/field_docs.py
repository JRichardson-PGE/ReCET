"""Each measure-level column's definition, read from the packaged documents (OUTPUT_FIELDS.md, and INPUT_FIELDS.md for the echoed input columns), so a written
table can carry a definition and a link for every column (BRD BR-06.10).

    definition("ElecBen") -> Definition("ElecBen", "OUTPUT_FIELDS.md", "elecben", "The lifetime value of the measure's net electric savings ...")

An OUTPUT_FIELDS.md entry is a `####` heading naming one or more columns, whose first paragraph is the definition (or, when the columns are defined one by
one, a list of lines "- `Column`: definition"), or a table row whose first cell names the columns and whose second cell is the definition (the link is then the section's heading). `anchor` is the heading's id as the web app's Docs pages make it
(Python-Markdown's toc slug), so `/docs/OUTPUT_FIELDS.md#elecben` opens the entry. Columns written per ACC component (`ElecValue_<component>`) share one
definition. No document is read until a definition is asked for, and each is read once.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DOCS = Path(__file__).with_name("docs")
PATTERNS = {"ElecValue_": "ElecValue_<component>", "GasValue_": "GasValue_<component>"}


@dataclass(frozen=True)
class Definition:
    column: str
    doc: str            # the document file, e.g. "OUTPUT_FIELDS.md"
    anchor: str         # the heading id in that document
    text: str           # the definition, plain text

    @property
    def link(self) -> str:
        """The relative link to the entry, from the docs folder or the web app's /docs/ pages."""
        return f"{self.doc}#{self.anchor}" if self.anchor else self.doc


def slug(heading: str) -> str:
    """Python-Markdown's toc id for a heading's text: markup dropped, lower case, punctuation removed, spaces and dashes joined by '-'."""
    text = re.sub(r"[`*_]", "", heading).strip()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def _plain(text: str) -> str:
    """Markdown to plain text: links to their words, emphasis and code marks dropped, whitespace collapsed."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("**", "").replace("`", "").replace("\\|", "|")
    return re.sub(r"\s+", " ", text).strip()


def _cells(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


@lru_cache(maxsize=None)
def _output_entries() -> dict[str, Definition]:
    """Every column OUTPUT_FIELDS.md section 1 defines."""
    doc = "OUTPUT_FIELDS.md"
    text = (DOCS / doc).read_text(encoding="utf-8")
    text = text[text.index("## 1. "):text.index("## 2. ")]
    out: dict[str, Definition] = {}
    lines = text.splitlines()
    section = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(#{2,4}) (.+)$", line)
        if m:
            section = slug(m.group(2))
            if m.group(1) == "####":
                names = re.findall(r"`([^`]+)`", m.group(2))
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                para = []
                while j < len(lines) and lines[j].strip() and not lines[j].startswith(("#", "|", "```")):
                    para.append(lines[j])
                    j += 1
                own = {}                                   # "- `Column`: definition" lines: one definition per column
                for p in para:
                    b = re.match(r"^- `([^`]+)`: (.+)$", p)
                    if b:
                        own[b.group(1)] = b.group(2)
                for n in names:
                    out.setdefault(n, Definition(n, doc, section, _plain(own.get(n) or " ".join(para))))
            i += 1
            continue
        if line.startswith("|") and not re.match(r"^\|\s*-", line):
            cells = _cells(line)
            names = re.findall(r"`([^`]+)`", cells[0]) if cells else []
            if names and len(cells) > 1:
                for n in names:
                    out.setdefault(n, Definition(n, doc, section, _plain(cells[1])))
        i += 1
    return out


@lru_cache(maxsize=None)
def _input_entries() -> dict[str, Definition]:
    """Every Measure.csv column INPUT_FIELDS.md section 1 defines: its first cell names it, its last cell describes it."""
    doc = "INPUT_FIELDS.md"
    text = (DOCS / doc).read_text(encoding="utf-8")
    start = text.index("## 1. `Measure.csv`")
    text = text[start:text.index("## 2. ", start)]
    out: dict[str, Definition] = {}
    section = slug("1. `Measure.csv`")
    names_table = False                                  # the table of CET's legacy names: not a definition
    for line in text.splitlines():
        m = re.match(r"^(#{2,4}) (.+)$", line)
        if m:
            section = slug(m.group(2))
            continue
        if not line.startswith("|"):
            names_table = False
            continue
        if line.startswith("| ReCET name"):
            names_table = True
        if not names_table and not re.match(r"^\|\s*-", line):
            cells = _cells(line)
            names = re.findall(r"`([^`]+)`", cells[0]) if cells else []
            if names and len(cells) > 1:
                for n in names:
                    out.setdefault(n, Definition(n, doc, section, _plain(cells[-1])))
    return out


def definition(column: str) -> Definition | None:
    """The column's definition: an echoed Measure.csv column's from INPUT_FIELDS.md, else an output entry, else None."""
    from .templates import MEASURE_COLUMNS
    if column in MEASURE_COLUMNS and column in _input_entries():
        return _input_entries()[column]
    outs = _output_entries()
    if column in outs:
        return outs[column]
    for prefix, name in PATTERNS.items():
        if column.startswith(prefix) and name in outs:
            d = outs[name]
            return Definition(column, d.doc, d.anchor, d.text)
    return _input_entries().get(column)


def table(columns, base_url: str = "") -> "pd.DataFrame":
    """One row per column, in order: position, column, definition and link (`base_url` + the document link, e.g. a web app's '/docs/'). A column without
    an entry gets a blank definition."""
    import pandas as pd
    rows = []
    for k, c in enumerate(columns, start=1):
        d = definition(c)
        rows.append({"position": k, "column": c, "definition": d.text if d else "", "documentation": f"{base_url}{d.link}" if d else ""})
    return pd.DataFrame(rows, columns=["position", "column", "definition", "documentation"])
