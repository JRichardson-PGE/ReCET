"""A run's progress, for the web app's progress bar and time estimate (BRD BR-06.2).

A run reports the stage it is in and, in a run in parts, how many of the stage's parts are done (`report`). When the environment variable RECET_PROGRESS_FILE
names a file, each report rewrites that file as JSON; otherwise reporting does nothing, so the window and the command line are unaffected. The web runner points
it at the run folder's `progress.json` and the run page and the runs list read it (`read`, `describe`).

The bar's fraction weights each stage by its share of a 1,000,000-measure run on the server (0.1.38: splitting 17 s, preflight 71 s, pricing 210 s, writing
106 s of 407 s), and moves within a stage as its parts finish. A run in one piece reports its stages without parts, so its bar moves in steps. The estimate of
the time left is the time so far x (1 - fraction) / fraction: rough while the fraction is small, so none is given before 3% or 5 s.

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

# stage: (label, share of the run); a stage a run skips (splitting in a run in one piece) is passed over
STAGES = {
    "preparing": ("Reading the inputs", 0.01),
    "splitting": ("Splitting Measure.csv into parts", 0.04),
    "preflight": ("Checking the inputs", 0.17),
    "loading": ("Loading the measures", 0.03),
    "pricing": ("Pricing", 0.48),
    "writing": ("Writing the results", 0.26),
    "finishing": ("Finishing", 0.01),
}
COMPARE_SHARE = 0.95                       # a comparison's two runs fill this much of the bar; comparing them the rest
_START: dict[str, float] = {}
_acc = 0.0
for _k, (_label, _w) in STAGES.items():
    _START[_k] = _acc
    _acc += _w
_T0 = time.time()


def _slice() -> tuple[int, int] | None:
    try:
        i, n = (int(x) for x in os.environ.get(SLICE_ENV, "").split("/"))
        return (i, n) if 0 <= i < n else None
    except ValueError:
        return None


def fraction(stage: str, done: int | None = None, total: int | None = None) -> float:
    """The share of the run done at `stage` with `done` of its `total` parts finished."""
    if stage == "done":
        return 1.0
    if stage == "comparing":
        return COMPARE_SHARE
    start, w = _START.get(stage, 0.0), STAGES.get(stage, ("", 0.0))[1]
    within = min(1.0, max(0.0, done / total)) if done is not None and total else 0.0
    return min(1.0, start + w * within)


def report(stage: str, done: int | None = None, total: int | None = None) -> None:
    """Record that the run is at `stage` (a key of STAGES, "comparing" or "done"), `done` of `total` parts through it. Does nothing unless RECET_PROGRESS_FILE
    is set; never raises (a progress file that cannot be written must not stop a run)."""
    path = os.environ.get(ENV)
    if not path:
        return
    now = time.time()
    f_local = fraction(stage, done, total)
    sl = _slice()
    if sl is not None:
        i, n = sl
        f = (i + f_local) / n * COMPARE_SHARE
        elapsed_local = now - _T0
        left = (elapsed_local * (1 - f_local) / f_local + (n - i - 1) * elapsed_local / f_local
                if f_local >= 0.03 and elapsed_local >= 5 else None)
        label = f"run {i + 1} of {n}: " + (STAGES[stage][0] if stage in STAGES else stage)
    else:
        f = f_local
        elapsed = now - _T0
        left = elapsed * (1 - f) / f if f >= 0.03 and elapsed >= 5 and stage != "done" else None
        label = STAGES[stage][0] if stage in STAGES else {"comparing": "Comparing the two runs", "done": "Done"}.get(stage, stage)
    data = {"stage": stage, "label": label, "done": done, "total": total, "fraction": round(f, 4),
            "seconds_left": None if left is None else round(left), "updated": now}
    try:
        p = Path(path)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:                         # e.g. the web app reading the file at that moment on Windows: the next report writes it
        pass


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
    if s is not None:
        text += " · about " + (f"{max(1, round(s / 60))} min" if s >= 90 else f"{max(5, round(s / 5) * 5)} s") + " left"
    return text
