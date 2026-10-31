"""Cost-test definition sets (BRD v0.5 BR-06.8, BR-06.14; recet/docs/COST_TESTS.md): the cost tests and value streams as versioned data.

A set is one YAML file, `recet/data/cost_tests/<id>/definition.yaml`, with four parts: pricings (the avoided-cost rows, discount rates and column
suffixes a stream is priced on), streams (the dollar quantities: engine-priced columns, per-unit input columns, the program costs), aggregates (TSB)
and tests (benefits, numerator adders, costs, the program-cost share, variants and outputs). `cost_test_set.schema.json` beside the sets describes the
file for other tools; `validate` checks the same rules here, plus the cross-references a schema cannot (a stream a test names exists in the test's
pricing, a share names a test that shares program costs), and tests/test_cost_test_sets.py keeps the two in step.

Ids are `<workstream>-<first program year>` (Jake, 2026-10-31: "EE-2026", so that other workstreams such as IQP can add their own sets), with an
optional lower-case tail (`EE-2028-draft`). A published set is never edited: a change is a new set. Every run pins the set it used: RunConfig.cost_test_set
(an id, or a path to a YAML file for an analysis run), the measure-level column Used_CostTestSet, the `cost_tests` block of run_settings.json and the
worker's receipt (with the file's SHA-256). A run that names no set uses the set in force for the job's first program year (`in_force`); a chosen set
outside its program years is a warning, never a stop. A filing run (CEDARS filing checks, or priced for a filing) accepts only a published built-in set:
a supplied file or a draft set is refused (Jake, 2026-10-31: filings use the CPUC's set, which users cannot edit; standalone runs may use their own).

The arithmetic is still `pipeline/cost_tests.py` (0.1.82 records and checks the set; the evaluator that reads it is the next step of COST_TESTS.md's
build plan)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

SETS_DIR = Path(__file__).parent / "data" / "cost_tests"
SCHEMA_FILE = SETS_DIR / "cost_test_set.schema.json"
DEFINITION = "definition.yaml"
SCHEMA_VERSION = 1
DEFAULT_WORKSTREAM = "EE"
DRAFT_LABEL = " (draft)"

ID = re.compile(r"^[A-Z][A-Z0-9]{0,15}-[0-9]{4}(-[a-z0-9]+)*$")
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
SUFFIX = re.compile(r"^(_[A-Za-z0-9]+)?$")
TOP = {"schema_version", "id", "workstream", "version", "label", "description", "draft", "program_years", "pricings", "streams", "aggregates", "tests"}
ACC_ROWS = ("trc", "sct_base", "sct_high")
RATE_COLUMNS = ("DiscountRateQtr", "DiscountRateAnnual", "DiscountRateQtr_SCT", "DiscountRateAnnual_SCT")
SOURCES = ("engine", "input", "program")
KINDS = ("benefit", "cost")
FUELS = ("elec", "gas", "water", "none")
NAMINGS = ("suffix", "participant_suffix", "none")
BASES = ("one_time", "annual_over_life")
NTGS = ("kwh", "therm", "none")
MARKET_EFFECTS = ("benefits", "costs", "none")
VARIANTS = ("NoAdmin", "Gross")
SHARE_RULES = ("positive_benefits",)
STREAM_KEYS = {"source", "kind", "fuel", "pricings", "naming", "gross", "unit", "definition", "internal",
               "columns", "columns_2nd", "basis", "ntg", "market_effects"}
TEST_KEYS = {"label", "pricing", "benefits", "numerator_adders", "costs", "program_costs", "variants", "lifecycle_net_benefit", "levelized", "headline",
             "filing", "definition"}


class SetError(ValueError):
    """A cost-test set that cannot be used: every problem found, one per line."""


@dataclass(frozen=True)
class Pricing:
    id: str
    label: str
    acc_rows: str
    discount_rate_quarterly: str
    discount_rate_annual: str
    suffix: str
    participant_suffix: str


@dataclass(frozen=True)
class Stream:
    name: str
    source: str
    kind: str
    fuel: str = "none"
    pricings: tuple[str, ...] = ()
    naming: str = "none"
    gross: str | None = None
    unit: str = ""
    definition: str = ""
    internal: bool = False
    columns: tuple[str, ...] = ()
    columns_2nd: tuple[str, ...] = ()
    basis: str = ""
    ntg: str = ""
    market_effects: str = ""


@dataclass(frozen=True)
class Aggregate:
    name: str
    pricing: str
    plus: tuple[str, ...]
    minus: tuple[str, ...]
    gross: bool
    unit: str
    definition: str


@dataclass(frozen=True)
class Test:
    id: str
    label: str
    pricing: str
    benefits: tuple[str, ...]
    numerator_adders: tuple[str, ...]
    costs: tuple[str, ...]
    share: str | None                       # "positive_benefits", another test's id, or None (no program costs)
    variants: tuple[str, ...]
    lifecycle_net_benefit: bool
    levelized: tuple[str, ...]              # "" (the test's cost) and/or "NoAdmin"
    headline: bool
    filing: bool
    definition: str

    def outputs(self) -> list[str]:
        """The measure-level columns the test writes, in the order the engine writes them."""
        t = self.id
        out = [f"{t}Ben", f"{t}Cost"]
        if "NoAdmin" in self.variants:
            out.append(f"{t}CostNoAdmin")
        if "Gross" in self.variants:
            out.append(f"{t}CostGross")
        out.append(f"{t}Ratio")
        if "NoAdmin" in self.variants:
            out.append(f"{t}RatioNoAdmin")
        if self.lifecycle_net_benefit:
            out.append(f"{t}LifecycleNetBen")
        for v in self.levelized:
            out += [f"Lev{t}Cost{v}", f"LevNetBen{t}Elec{v}", f"LevNetBen{t}Gas{v}"]
        return out


@dataclass(frozen=True)
class CostTestSet:
    id: str
    workstream: str
    version: str
    label: str
    description: str
    draft: bool
    years_from: int | None
    years_to: int | None
    pricings: dict[str, Pricing]
    streams: dict[str, Stream]
    aggregates: dict[str, Aggregate]
    tests: dict[str, Test]
    sha256: str
    source: str                             # "built-in" or the supplied file's path
    path: Path | None = field(default=None, compare=False)

    @property
    def builtin(self) -> bool:
        return self.source == "built-in"

    @property
    def run_label(self) -> str:
        """What Used_CostTestSet says: a built-in set's id ("EE-2026"), "custom: <file name>" for a supplied one; " (draft)" at the end for a draft."""
        own = self.id if self.builtin else f"custom: {Path(self.source).name}"
        return own + (DRAFT_LABEL if self.draft else "")

    def column(self, stream: str, pricing: str, gross: bool = False) -> str:
        """The column that holds `stream` in `pricing` (its gross form with `gross`)."""
        s, p = self.streams[stream], self.pricings[pricing]
        name = s.name
        if gross:
            if s.gross is None:
                raise KeyError(f"stream {stream} has no gross form")
            name = s.name if s.gross == "same" else f"{s.name}Gross" if s.gross == "divide_ntg" else s.gross
        sfx = {"suffix": p.suffix, "participant_suffix": p.participant_suffix, "none": ""}[s.naming]
        return name + sfx

    def output_columns(self) -> list[str]:
        """Every measure-level column the set's aggregates and tests write."""
        out = []
        for a in self.aggregates.values():
            out += [a.name] + ([f"{a.name}Gross"] if a.gross else [])
        for t in self.tests.values():
            out += t.outputs()
        return out

    def stream_columns(self) -> list[str]:
        """Every column a test or aggregate reads (in its pricing; the gross forms where a Gross variant or aggregate uses them), internal ones too."""
        out: dict[str, None] = {}
        for a in self.aggregates.values():
            for s in (*a.plus, *a.minus):
                out[self.column(s, a.pricing)] = None
                if a.gross:
                    out[self.column(s, a.pricing, gross=True)] = None
        for t in self.tests.values():
            for s in (*t.benefits, *t.numerator_adders, *t.costs):
                if self.streams[s].source == "program":
                    continue
                out[self.column(s, t.pricing)] = None
                if "Gross" in t.variants and s not in t.numerator_adders:
                    out[self.column(s, t.pricing, gross=True)] = None
        return list(out)

    def input_columns(self) -> list[str]:
        """The Measure.csv columns the set's input streams read."""
        out: dict[str, None] = {}
        for s in self.streams.values():
            for c in (*s.columns, *s.columns_2nd):
                out[c] = None
        return list(out)

    def headline_tests(self) -> list[str]:
        return [t for t, v in self.tests.items() if v.headline]

    def record(self) -> dict:
        """The `cost_tests` block of run_settings.json and receipt.json."""
        return {"id": self.id, "workstream": self.workstream, "version": self.version, "label": self.label, "draft": self.draft,
                "source": self.source, "sha256": self.sha256, "program_years": years_text(self), "tests": list(self.tests),
                "used_label": self.run_label}


# ---------------------------------------------------------------------------------------------------------------------------- validating
def _strs(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _year(v) -> bool:
    return v is None or (isinstance(v, int) and not isinstance(v, bool) and 1990 <= v <= 2100)


def validate(data) -> list[str]:
    """Everything wrong with a set's parsed YAML (empty = usable)."""
    if not isinstance(data, dict):
        return ["a cost-test set must be a mapping (YAML keys and values)"]
    errors = [f"unknown top-level key {k!r}" for k in sorted(set(data) - TOP)]
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    sid = data.get("id")
    if not (isinstance(sid, str) and ID.match(sid)):
        errors.append("id: <workstream>-<first program year>, e.g. EE-2026 or EE-2028-draft (upper-case workstream, four-digit year)")
    ws = data.get("workstream")
    if not (isinstance(ws, str) and re.match(r"^[A-Z][A-Z0-9]{0,15}$", ws)):
        errors.append("workstream: upper-case letters and digits, e.g. EE")
    elif isinstance(sid, str) and not sid.startswith(ws + "-"):
        errors.append(f"id {sid!r} must start with its workstream {ws!r} and a dash")
    for k in ("version", "label"):
        if not (isinstance(data.get(k), str) and data[k].strip()):
            errors.append(f"{k}: text is required")
    if "description" in data and not isinstance(data["description"], str):
        errors.append("description: text")
    if not isinstance(data.get("draft", False), bool):
        errors.append("draft: true or false")
    py = data.get("program_years", {})
    if not isinstance(py, dict) or set(py) - {"from", "to"} or not _year(py.get("from")) or not _year(py.get("to")):
        errors.append("program_years: {from: <year or null>, to: <year or null>}")
    elif py.get("from") is not None and py.get("to") is not None and py["to"] < py["from"]:
        errors.append("program_years: to is before from")

    pricings = data.get("pricings")
    if not isinstance(pricings, dict) or not pricings:
        errors.append("pricings: at least one pricing")
        pricings = {}
    for pid, p in pricings.items():
        where = f"pricings.{pid}"
        if not (isinstance(pid, str) and NAME.match(pid)):
            errors.append(f"{where}: a pricing id is a name (letters, digits, _)")
        if not isinstance(p, dict):
            errors.append(f"{where}: a mapping")
            continue
        errors += [f"{where}: unknown key {k!r}" for k in sorted(set(p) - {"label", "acc_rows", "discount_rate_quarterly", "discount_rate_annual",
                                                                            "suffix", "participant_suffix"})]
        if p.get("acc_rows") not in ACC_ROWS:
            errors.append(f"{where}.acc_rows: one of {', '.join(ACC_ROWS)}")
        for k in ("discount_rate_quarterly", "discount_rate_annual"):
            if p.get(k) not in RATE_COLUMNS:
                errors.append(f"{where}.{k}: a run-parameter column ({', '.join(RATE_COLUMNS)})")
        for k in ("suffix", "participant_suffix"):
            if not (isinstance(p.get(k, ""), str) and SUFFIX.match(p.get(k, ""))):
                errors.append(f"{where}.{k}: empty, or _ and letters / digits (e.g. _SB)")

    streams = data.get("streams")
    if not isinstance(streams, dict) or not streams:
        errors.append("streams: at least one stream")
        streams = {}
    for name, s in streams.items():
        where = f"streams.{name}"
        if not (isinstance(name, str) and NAME.match(name)):
            errors.append(f"{where}: a stream name is a column name (letters, digits, _)")
        if not isinstance(s, dict):
            errors.append(f"{where}: a mapping")
            continue
        errors += [f"{where}: unknown key {k!r}" for k in sorted(set(s) - STREAM_KEYS)]
        src = s.get("source")
        if src not in SOURCES:
            errors.append(f"{where}.source: one of {', '.join(SOURCES)}")
        if s.get("kind") not in KINDS:
            errors.append(f"{where}.kind: benefit or cost")
        if "fuel" in s and s["fuel"] not in FUELS:
            errors.append(f"{where}.fuel: one of {', '.join(FUELS)}")
        if src in ("engine", "input"):
            pr = s.get("pricings")
            if not _strs(pr) or not pr:
                errors.append(f"{where}.pricings: the pricings the stream is priced in")
            else:
                errors += [f"{where}.pricings: no pricing {p!r}" for p in pr if p not in pricings]
            if s.get("naming", "none") not in NAMINGS:
                errors.append(f"{where}.naming: one of {', '.join(NAMINGS)}")
            g = s.get("gross")
            if g is not None and not (isinstance(g, str) and (g in ("divide_ntg", "same") or NAME.match(g))):
                errors.append(f"{where}.gross: divide_ntg, same or a column name")
            if g == "divide_ntg" and s.get("fuel", "none") == "none" and src == "engine":
                errors.append(f"{where}.gross: divide_ntg needs the stream's fuel (whose NTG is divided out)")
        if src == "input":
            if not _strs(s.get("columns")) or not s.get("columns"):
                errors.append(f"{where}.columns: the Measure.csv column(s) whose per-unit values are added")
            if "columns_2nd" in s and not _strs(s["columns_2nd"]):
                errors.append(f"{where}.columns_2nd: Measure.csv column names")
            if s.get("basis") not in BASES:
                errors.append(f"{where}.basis: one of {', '.join(BASES)}")
            if s.get("ntg") not in NTGS:
                errors.append(f"{where}.ntg: one of {', '.join(NTGS)}")
            if s.get("market_effects") not in MARKET_EFFECTS:
                errors.append(f"{where}.market_effects: one of {', '.join(MARKET_EFFECTS)}")
        elif any(k in s for k in ("columns", "columns_2nd", "basis", "ntg", "market_effects")):
            errors.append(f"{where}: columns, basis, ntg and market_effects are for source: input")
        if src == "program" and any(k in s for k in ("pricings", "naming", "gross")):
            errors.append(f"{where}: a program stream is priced by the test's own pricing (no pricings, naming or gross)")
        if "internal" in s and not isinstance(s["internal"], bool):
            errors.append(f"{where}.internal: true or false")
        for k in ("unit", "definition"):
            if k in s and not isinstance(s[k], str):
                errors.append(f"{where}.{k}: text")
    if sum(1 for s in streams.values() if isinstance(s, dict) and s.get("source") == "program") > 1:
        errors.append("streams: at most one stream with source: program (the ProgramCost.csv costs)")

    def in_pricing(name: str, pricing: str, where: str, gross: bool = False) -> list[str]:
        s = streams.get(name)
        if not isinstance(s, dict):
            return [f"{where}: no stream {name!r}"]
        if s.get("source") == "program":
            return [f"{where}: {name} is the program costs; a test takes them through program_costs"]
        if pricing not in (s.get("pricings") or []):
            return [f"{where}: {name} is not priced in {pricing!r} (it is priced in {', '.join(s.get('pricings') or []) or 'nothing'})"]
        if gross and s.get("gross") is None:
            return [f"{where}: {name} has no gross form, which the Gross variant needs"]
        return []

    aggregates = data.get("aggregates", {}) or {}
    if not isinstance(aggregates, dict):
        errors.append("aggregates: a mapping")
        aggregates = {}
    for name, a in aggregates.items():
        where = f"aggregates.{name}"
        if not (isinstance(name, str) and NAME.match(name)):
            errors.append(f"{where}: a column name")
        if not isinstance(a, dict):
            errors.append(f"{where}: a mapping")
            continue
        errors += [f"{where}: unknown key {k!r}" for k in sorted(set(a) - {"pricing", "plus", "minus", "gross", "unit", "definition"})]
        if a.get("pricing") not in pricings:
            errors.append(f"{where}.pricing: one of the set's pricings")
            continue
        for k in ("plus", "minus"):
            if not _strs(a.get(k, [])):
                errors.append(f"{where}.{k}: a list of streams")
                continue
            for sname in a.get(k, []):
                errors += in_pricing(sname, a["pricing"], f"{where}.{k}", gross=bool(a.get("gross")))
        if not isinstance(a.get("gross", False), bool):
            errors.append(f"{where}.gross: true or false")

    tests = data.get("tests")
    if not isinstance(tests, dict) or not tests:
        errors.append("tests: at least one test")
        tests = {}
    for tid, t in tests.items():
        where = f"tests.{tid}"
        if not (isinstance(tid, str) and re.match(r"^[A-Z][A-Za-z0-9]{1,15}$", tid)):
            errors.append(f"{where}: a test id starts with a capital letter (TRC, PAC, JST1); it names the columns <T>Ben, <T>Cost, <T>Ratio")
        if not isinstance(t, dict):
            errors.append(f"{where}: a mapping")
            continue
        errors += [f"{where}: unknown key {k!r}" for k in sorted(set(t) - TEST_KEYS)]
        if not (isinstance(t.get("label"), str) and t["label"].strip()):
            errors.append(f"{where}.label: text is required")
        pricing = t.get("pricing")
        if pricing not in pricings:
            errors.append(f"{where}.pricing: one of the set's pricings ({', '.join(pricings)})")
            continue
        variants = t.get("variants", [])
        if not _strs(variants) or set(variants) - set(VARIANTS):
            errors.append(f"{where}.variants: a list of {', '.join(VARIANTS)}")
            variants = []
        gross = "Gross" in variants
        for k, required in (("benefits", True), ("numerator_adders", False), ("costs", True)):
            v = t.get(k, [])
            if not _strs(v) or (required and not v):
                errors.append(f"{where}.{k}: a list of streams" + (" (at least one)" if required else ""))
                continue
            for sname in v:
                errors += in_pricing(sname, pricing, f"{where}.{k}", gross=gross and k != "numerator_adders")
        pc = t.get("program_costs", "none")
        if pc not in (None, "none"):
            if not isinstance(pc, dict) or set(pc) != {"share"}:
                errors.append(f"{where}.program_costs: none, or {{share: positive_benefits | <another test>}}")
            elif pc["share"] not in SHARE_RULES:
                other = tests.get(pc["share"])
                if not isinstance(other, dict):
                    errors.append(f"{where}.program_costs.share: positive_benefits or another test's id")
                elif not isinstance(other.get("program_costs"), dict) or other["program_costs"].get("share") != "positive_benefits":
                    errors.append(f"{where}.program_costs.share: {pc['share']} does not compute its own share (share: positive_benefits)")
                elif other.get("pricing") != pricing:
                    errors.append(f"{where}.program_costs.share: {pc['share']} is priced in {other.get('pricing')!r}, this test in {pricing!r}")
                elif gross and "Gross" not in (other.get("variants") or []):
                    errors.append(f"{where}.program_costs.share: {pc['share']} has no Gross variant to share the Gross costs on")
            if not any(isinstance(s, dict) and s.get("source") == "program" for s in streams.values()):
                errors.append(f"{where}.program_costs: the set has no stream with source: program")
        elif "NoAdmin" in variants:
            errors.append(f"{where}.variants: NoAdmin leaves out the program costs, which this test does not have")
        lev = t.get("levelized", [])
        if not _strs(lev) or set(lev) - {"", "NoAdmin"} or ("NoAdmin" in lev and "NoAdmin" not in variants):
            errors.append(f"{where}.levelized: a list of \"\" (the test's cost) and NoAdmin (when the test has that variant)")
        for k in ("lifecycle_net_benefit", "headline", "filing"):
            if k in t and not isinstance(t[k], bool):
                errors.append(f"{where}.{k}: true or false")
        if "definition" in t and not isinstance(t["definition"], str):
            errors.append(f"{where}.definition: text")
    return errors


# ---------------------------------------------------------------------------------------------------------------------------- loading
def _yaml(text: str, where: str):
    import yaml
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SetError(f"{where}: not valid YAML: {exc}") from exc


def _tuple(v) -> tuple:
    return tuple(v or ())


def parse(data: dict, sha256: str, source: str, path: Path | None = None) -> CostTestSet:
    """A CostTestSet from a set's parsed YAML (SetError listing every problem when it is not usable)."""
    errors = validate(data)
    if errors:
        raise SetError(f"cost-test set {data.get('id') if isinstance(data, dict) else '?'!s} ({source}):\n  " + "\n  ".join(errors))
    pricings = {k: Pricing(k, str(p.get("label", k)), p["acc_rows"], p["discount_rate_quarterly"], p["discount_rate_annual"], p.get("suffix", ""),
                           p.get("participant_suffix", "")) for k, p in data["pricings"].items()}
    streams = {k: Stream(k, s["source"], s["kind"], s.get("fuel", "none"), _tuple(s.get("pricings")), s.get("naming", "none"), s.get("gross"),
                         s.get("unit", ""), (s.get("definition") or "").strip(), bool(s.get("internal", False)), _tuple(s.get("columns")),
                         _tuple(s.get("columns_2nd")), s.get("basis", ""), s.get("ntg", ""), s.get("market_effects", ""))
               for k, s in data["streams"].items()}
    aggregates = {k: Aggregate(k, a["pricing"], _tuple(a.get("plus")), _tuple(a.get("minus")), bool(a.get("gross", False)), a.get("unit", ""),
                               (a.get("definition") or "").strip()) for k, a in (data.get("aggregates") or {}).items()}
    tests = {}
    for k, t in data["tests"].items():
        pc = t.get("program_costs", "none")
        tests[k] = Test(k, t["label"], t["pricing"], _tuple(t.get("benefits")), _tuple(t.get("numerator_adders")), _tuple(t.get("costs")),
                        pc["share"] if isinstance(pc, dict) else None, _tuple(t.get("variants")), bool(t.get("lifecycle_net_benefit", False)),
                        _tuple(t.get("levelized")), bool(t.get("headline", False)), bool(t.get("filing", True)), (t.get("definition") or "").strip())
    py = data.get("program_years") or {}
    return CostTestSet(data["id"], data["workstream"], str(data["version"]), data["label"], (data.get("description") or "").strip(),
                       bool(data.get("draft", False)), py.get("from"), py.get("to"), pricings, streams, aggregates, tests, sha256, source, path)


def read_file(path: str | Path, source: str | None = None) -> CostTestSet:
    """A set from a YAML file (or a folder holding definition.yaml)."""
    p = Path(path)
    if p.is_dir():
        p = p / DEFINITION
    if not p.is_file():
        raise SetError(f"cost-test set file {p} not found")
    raw = p.read_bytes()
    return parse(_yaml(raw.decode("utf-8-sig"), str(p)), hashlib.sha256(raw).hexdigest(), source or str(p), p)


@lru_cache(maxsize=None)
def _builtin(folder: str) -> dict[str, CostTestSet]:
    out = {}
    for d in sorted(Path(folder).iterdir()):
        if (d / DEFINITION).is_file():
            s = read_file(d, source="built-in")
            if s.id != d.name:
                raise SetError(f"built-in cost-test set folder {d.name} holds set {s.id!r}: the folder is named by the set's id")
            out[s.id] = s
    return out


def builtin(folder: Path | None = None) -> dict[str, CostTestSet]:
    """{id: set} of the sets shipped in recet/data/cost_tests (or `folder`)."""
    return _builtin(str(folder or SETS_DIR))


def load(ref: str, folder: Path | None = None) -> CostTestSet:
    """A built-in set by id, or a supplied YAML file (a path)."""
    ref = str(ref).strip()
    sets = builtin(folder)
    if ref in sets:
        return sets[ref]
    if ID.match(ref) and not Path(ref).exists():
        raise SetError(f"no built-in cost-test set {ref!r} (built in: {', '.join(sets)})")
    return read_file(ref)


def years(s: CostTestSet, folder: Path | None = None) -> tuple[int | None, int | None]:
    """The program years `s` is in force for: its own `from` / `to`; an open `to` ends the year before the next published set of its workstream starts."""
    if s.years_to is not None or s.draft:
        return s.years_from, s.years_to
    later = sorted(x.years_from for x in builtin(folder).values()
                   if x.workstream == s.workstream and not x.draft and x.id != s.id and x.years_from is not None
                   and (s.years_from is None or x.years_from > s.years_from))
    return s.years_from, (later[0] - 1 if later else None)


def years_text(s: CostTestSet, folder: Path | None = None) -> str:
    a, b = years(s, folder)
    if a is None and b is None:
        return "every program year"
    if a is None:
        return f"program years up to {b}"
    if b is None:
        return f"program years {a} on"
    return f"program years {a}-{b}" if a != b else f"program year {a}"


def covers(s: CostTestSet, year: int, folder: Path | None = None) -> bool:
    a, b = years(s, folder)
    return (a is None or year >= a) and (b is None or year <= b)


def in_force(year: int | None, workstream: str = DEFAULT_WORKSTREAM, folder: Path | None = None) -> CostTestSet:
    """The published built-in set of `workstream` in force for program year `year` (None = the newest). Before every set's first year: the earliest."""
    sets = sorted((s for s in builtin(folder).values() if s.workstream == workstream and not s.draft),
                  key=lambda s: (s.years_from is not None, s.years_from or 0, s.version))
    if not sets:
        raise SetError(f"no published built-in cost-test set for workstream {workstream!r}")
    if year is None:
        return sets[-1]
    for s in reversed(sets):
        if covers(s, int(year), folder):
            return s
    return sets[0]


def catalog(folder: Path | None = None) -> list[tuple[str, str]]:
    """[(id, label)] of the built-in sets, for the run option's select: published sets first, newest first within a workstream."""
    sets = builtin(folder).values()
    return [(s.id, f"{s.id}: {s.label}" + (DRAFT_LABEL if s.draft else "")) for s in
            sorted(sets, key=lambda s: (s.draft, s.workstream != DEFAULT_WORKSTREAM, s.workstream, -(s.years_from or 0), s.id))]


# ---------------------------------------------------------------------------------------------------------------------------- for a run
@dataclass
class Resolved:
    set: CostTestSet
    chosen: bool                            # named by the run (else the default in force)
    first_year: int | None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def record(self) -> dict:
        return {**self.set.record(), "chosen": "run option" if self.chosen else f"in force for program year {self.first_year}",
                **({"warnings": self.warnings} if self.warnings else {})}


def check_for_run(ref: str, filing: bool) -> CostTestSet | None:
    """The set a run names (None = the default, decided once the first program year is known), checked for a filing run: a filing accepts only a
    published built-in set (SetError otherwise). Called before anything is priced."""
    if not str(ref or "").strip():
        return None
    s = load(ref)
    if filing and (not s.builtin or s.draft):
        what = "a DRAFT cost-test set" if s.draft else "a supplied cost-test set file"
        raise SetError(f"{s.run_label} is {what}: a filing run (CEDARS filing checks, or priced for a filing) uses a published built-in set only, the "
                       f"CPUC's, which users cannot edit (built in: {', '.join(i for i, x in builtin().items() if not x.draft)}). "
                       "Use a custom set for an analysis run without those options.")
    return s


def resolve(ref: str, first_year: int | None, filing: bool = False, program_years: list[int] | None = None) -> Resolved:
    """The set a run prices on: `ref` (an id or a file) or, blank, the set in force for `first_year`. Notes say which and why; a set that is not in
    force for every one of `program_years` (default: the first year) is warned about, never refused."""
    chosen = check_for_run(ref, filing)
    s = chosen or in_force(first_year)
    r = Resolved(s, chosen is not None, first_year)
    how = "chosen for this run" if chosen is not None else (f"in force for program year {first_year}" if first_year else "the newest")
    r.notes.append(f"cost tests: {s.run_label}, {s.label} ({how}; {years_text(s)}; tests {', '.join(s.tests)})")
    if s.draft:
        r.warnings.append(f"cost tests: {s.run_label} is a DRAFT set: for analysis only, never for a filing")
    if not s.builtin:
        r.notes.append(f"cost tests: a supplied set file, SHA-256 {s.sha256}")
    outside = sorted({int(y) for y in (program_years or ([first_year] if first_year else [])) if not covers(s, int(y))})
    if outside:
        r.warnings.append(f"cost_tests_not_for_program_year: {s.id} is in force for {years_text(s)}, not for "
                          f"{', '.join(str(y) for y in outside)}; the run goes on with it")
    return r


def describe(s: CostTestSet) -> str:
    """The set as text: its tests, each written as Ben / Cost / Ratio formulas over the columns they read."""
    lines = [f"{s.id}: {s.label}" + (DRAFT_LABEL if s.draft else ""), f"version {s.version}; {years_text(s)}; SHA-256 {s.sha256}"]
    if s.description:
        lines.append(s.description)
    for a in s.aggregates.values():
        lines.append(f"{a.name} = " + " + ".join(s.column(x, a.pricing) for x in a.plus)
                     + "".join(f" - {s.column(x, a.pricing)}" for x in a.minus) + ("  (and a Gross form)" if a.gross else ""))
    for t in s.tests.values():
        p = s.pricings[t.pricing]
        lines.append("")
        lines.append(f"{t.id}, {t.label} ({p.label})" + ("  [headline]" if t.headline else ""))
        lines.append(f"  {t.id}Ben  = " + " + ".join(s.column(x, t.pricing) for x in (*t.benefits, *t.numerator_adders)))
        share = ("" if t.share is None else
                 f" + share x program costs at {p.discount_rate_annual}" + ("" if t.share == "positive_benefits" else f" (the {t.share} share)"))
        lines.append(f"  {t.id}Cost = " + " + ".join(s.column(x, t.pricing) for x in t.costs) + share)
        lines.append(f"  {t.id}Ratio = {t.id}Ben / {t.id}Cost" + (f"; variants {', '.join(t.variants)}" if t.variants else ""))
    return "\n".join(lines)
