"""A run's progress, for the web app's progress bar and time estimate (BRD BR-06.2).

A run reports the stage it is in and, where a stage goes part by part, how many of its parts are done (`report`). When the environment variable
RECET_PROGRESS_FILE names a file, each report rewrites that file as JSON and appends the same record, with the seconds since the run started, to
`progress_log.jsonl` beside it (so a run's estimates can be checked against its real finish afterwards); otherwise reporting does nothing, so the window and the
command line are unaffected. The web runner points it at the run folder's `progress.json`, and the run page and the runs list read it (`read`, `live`,
`describe`).

The bar's fraction weights each stage by its seconds in a 1,000,000-measure run on the server (0.1.40, 2026-10-26, 50,000-row parts: before splitting 3 s,
splitting 15 s, preflight 70 s, pricing 210 s, writing the parts 56 s, then finishing the measure-level file: 58 s as csv.zip, 18 s as parquet). `plan` sets
the weights once the run knows whether it runs in parts, its output format and whether it prices the societal tests and emissions: a stage the run skips gets no weight (a run in parts has no "loading" stage; a
run in one piece no "splitting" or "file" stage), and the file stage's weight follows the format. Stages move part by part where they report parts; the file
stage moves as the parquet parts are joined and the CSV batches are written (outputs.TableWriter.close). A run in one piece reports its stages without parts, so
its bar moves in steps. The estimate of the time left is the time so far x (1 - fraction) / fraction at each report: rough while the fraction is small, so none
is given before 3% or 5 s. Between reports `live` counts it down.

A comparison runs its two ACC versions one after the other in child processes; each is given RECET_PROGRESS_SLICE ("0/2", "1/2") and fills its half of the
bar's first 95%, and the comparison itself fills the rest.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ENV = "RECET_PROGRESS_FILE"
SLICE_ENV = "RECET_PROGRESS_SLICE"
FILE_NAME = "progress.json"
LOG_NAME = "progress_log.jsonl"

LABELS = {                                  # in run order
    "preparing": "Reading the inputs",
    "splitting": "Splitting Measure.csv into parts",
    "preflight": "Checking the inputs",
    "loading": "Loading the measures",
    "pricing": "Pricing",
    "writing": "Writing the results",
    "file": "Finishing the measure-level file",
    "finishing": "Finishing",
}
PART_STAGES = ("preflight", "pricing", "writing")          # stages reported as "part k of n" (the file stage reports a share of itself)
# seconds of each stage in the 1M server run above; the file stage's by output format (csv and xlsx not measured: estimates)
SECONDS_IN_PARTS = {"preparing": 3, "splitting": 15, "preflight": 70, "pricing": 210, "writing": 56, "finishing": 1}
SECONDS_IN_ONE_PIECE = {"preparing": 3, "preflight": 70, "loading": 20, "pricing": 210, "writing": 56, "finishing": 1}   # loading: laptop 1M run (22 s)
FILE_SECONDS = {"csv.zip": 58, "parquet": 18, "csv": 45, "xlsx": 5}
FILE_JOIN_SECONDS = 18                      # of the file stage, joining the parquet parts; the rest (csv, csv.zip) writes the CSV from it
# pricing's and writing's seconds without the societal tests and/or emissions, as a share of both on: (societal, emissions) -> (pricing, writing).
# 100,000-record runs on the laptop (2026-10-26, 20,000-row parts, csv.zip): pricing 20 s both on, 12 s no SCT, 17 s no emissions, 10 s neither;
# writing the parts 6, 5, 4, 4 s
OPTION_FACTORS = {(True, True): (1.0, 1.0), (False, True): (0.6, 0.85), (True, False): (0.85, 0.7), (False, False): (0.5, 0.7)}
FILING_PREFLIGHT_SECONDS = 8                # CEDARS filing checks (filing_rules.py) in preflight: 1M server runs of 2026-10-26, 72 s without, 79-80 s with
COMPARE_SHARE = 0.95                        # a comparison's two runs fill this much of the bar; comparing them the rest
_T0 = time.time()
_weights: dict[str, float] = {}
_start: dict[str, float] = {}
_plan: dict = {}


def plan(in_parts: bool, output_format: str = "csv.zip", societal: bool = True, emissions: bool = True, filing: bool = False) -> None:
    """Set the stage weights for this run: in parts or in one piece, the measure-level file's format, and whether it prices the societal tests and
    emissions (both shorten pricing and writing a lot: OPTION_FACTORS)."""
    _plan.update(in_parts=in_parts, output_format=output_format, societal=societal, emissions=emissions, filing=filing)
    secs = dict(SECONDS_IN_PARTS if in_parts else SECONDS_IN_ONE_PIECE)
    if in_parts:
        secs["file"] = FILE_SECONDS.get(output_format, FILE_SECONDS["csv.zip"])
    f_price, f_write = OPTION_FACTORS[(bool(societal), bool(emissions))]
    secs["pricing"] *= f_price
    secs["writing"] *= f_write
    if filing:
        secs["preflight"] += FILING_PREFLIGHT_SECONDS
    total = sum(secs.values())
    _weights.clear()
    _start.clear()
    acc = 0.0
    for k in LABELS:
        w = secs.get(k, 0) / total
        _weights[k], _start[k] = w, acc
        acc += w


plan(False)                                 # until the run says otherwise (the only stage reported before plan() is "preparing", at 0)


def replan(**changes) -> None:
    """plan() again with some of its arguments changed (a run finds out only before pricing that its ACC has no societal avoided costs). Call it before
    pricing reports: a lighter stage ahead only moves the bar forward."""
    plan(**dict(_plan, **changes))


def file_share(joined: float, converted: float, output_format: str) -> float:
    """The share of the file stage done with `joined` of the parquet parts joined and `converted` of the CSV written (both 0-1)."""
    if output_format not in ("csv", "csv.zip"):
        return joined
    j = FILE_JOIN_SECONDS / FILE_SECONDS[output_format]
    return j * joined + (1 - j) * converted


def _slice() -> tuple[int, int] | None:
    try:
        i, n = (int(x) for x in os.environ.get(SLICE_ENV, "").split("/"))
        return (i, n) if 0 <= i < n else None
    except ValueError:
        return None


def fraction(stage: str, done: float | None = None, total: float | None = None) -> float:
    """The share of the run done at `stage` with `done` of its `total` parts finished (or, for the file stage, `done` = its share done and `total` = 1)."""
    if stage == "done":
        return 1.0
    if stage == "comparing":
        return COMPARE_SHARE
    start, w = _start.get(stage, 0.0), _weights.get(stage, 0.0)
    within = min(1.0, max(0.0, done / total)) if done is not None and total else 0.0
    return min(1.0, start + w * within)


def report(stage: str, done: float | None = None, total: float | None = None) -> None:
    """Record that the run is at `stage` (a key of LABELS, "comparing" or "done"), `done` of `total` parts through it. Does nothing unless RECET_PROGRESS_FILE
    is set; never raises (a progress file that cannot be written must not stop a run)."""
    path = os.environ.get(ENV)
    if not path:
        return
    now = time.time()
    f_local = fraction(stage, done, total)
    elapsed_local = now - _T0
    sl = _slice()
    if sl is not None:
        i, n = sl
        f = (i + f_local) / n * COMPARE_SHARE
        left = (elapsed_local * (1 - f_local) / f_local + (n - i - 1) * elapsed_local / f_local
                if f_local >= 0.03 and elapsed_local >= 5 else None)
        label = f"run {i + 1} of {n}: " + LABELS.get(stage, stage)
    else:
        f = f_local
        left = elapsed_local * (1 - f) / f if f >= 0.03 and elapsed_local >= 5 and stage != "done" else None
        label = LABELS.get(stage) or {"comparing": "Comparing the two runs", "done": "Done"}.get(stage, stage)
    parts = stage in PART_STAGES and bool(total)
    data = {"stage": stage, "label": label, "done": int(done or 0) if parts else None, "total": int(total) if parts else None,
            "fraction": round(f, 4), "seconds_left": None if left is None else round(left), "updated": now}
    p = Path(path)
    try:
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:                         # e.g. the web app reading the file at that moment on Windows: the next report writes it
        pass
    try:
        rec = dict(data, done=None if done is None else round(float(done), 4), total=total, elapsed=round(elapsed_local, 2),
                   slice=os.environ.get(SLICE_ENV) or None)
        with open(p.with_name(LOG_NAME), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def live(p: dict | None, now: float | None = None) -> dict | None:
    """`p` (a read progress file) with its time left counted down by the seconds since that report, never below 0."""
    if not p:
        return p
    p = dict(p)
    s, updated = p.get("seconds_left"), p.get("updated")
    if s is not None and updated:
        p["seconds_left"] = max(0, round(s - max(0.0, (time.time() if now is None else now) - updated)))
    return p


def read(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def describe(p: dict | None) -> str:
    """'Pricing: part 12 of 50 · about 4 min left', for the run page and the runs list."""
    if not p:
        return ""
    text = p.get("label", "")
    if p.get("total"):
        text += f": part {min(p.get('done') or 0, p['total']):,} of {p['total']:,}"
    s = p.get("seconds_left")
    if s is not None and s <= 0:
        text += " · almost done"
    elif s is not None:
        text += " · about " + (f"{max(1, round(s / 60))} min" if s >= 90 else f"{max(5, round(s / 5) * 5)} s") + " left"
    return text
