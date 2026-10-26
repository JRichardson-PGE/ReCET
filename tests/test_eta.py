"""A queued run's estimated start and finish (recet/web/eta.py): the duration model, the walk through the queue, and the pages that show it."""
from __future__ import annotations

import json
import time

import pytest

from recet.web import eta
from test_web import P, _login, _start_run, _user, web  # noqa: F401  (the fixture)

BIG = {"measures": 1_000_000, "preflight_only": False, "compare": False, "include_societal": True, "emissions": True, "output_format": "csv.zip"}


def test_the_model_follows_size_and_options():
    full = eta.model_seconds(BIG)
    assert full == pytest.approx(eta.OVERHEAD_SECONDS + 413, abs=1)              # the 1M server run's stage seconds (412 s on 0.1.40)
    assert eta.model_seconds(dict(BIG, measures=500_000)) == pytest.approx(eta.OVERHEAD_SECONDS + 413 / 2, abs=1)
    assert eta.model_seconds(dict(BIG, output_format="parquet")) < full
    assert eta.model_seconds(dict(BIG, include_societal=False)) < eta.model_seconds(dict(BIG, emissions=False)) < full
    assert eta.model_seconds(dict(BIG, compare=True)) == pytest.approx(2 * full)
    assert eta.model_seconds(dict(BIG, preflight_only=True)) == pytest.approx(eta.OVERHEAD_SECONDS + 3 + 15 + 70, abs=1)
    assert eta.model_seconds(None) == eta.model_seconds(dict(BIG, measures=None)) == eta.UNKNOWN_SECONDS
    assert eta.model_seconds(dict(BIG, filing=True)) == pytest.approx(full + 8)                 # CEDARS filing checks: 8 s more preflight


def test_describe():
    assert eta.describe({"starts_in": 0, "finishes_in": 40, "seconds": 40}) == "starts next, done in about 40 s (estimated)"
    assert eta.describe({"starts_in": 360, "finishes_in": 780, "seconds": 420}) == "starts in about 6 min, done in about 13 min (estimated)"
    assert eta.describe(None) == ""


def _set_size(ctx, run_id, **kw):
    (ctx.runner.folder(run_id) / eta.ESTIMATE_FILE).write_text(json.dumps(dict(BIG, **kw)))


def test_the_queue_walk(web, tmp_path):
    app, ctx = web
    _user(ctx, "q@example.com")
    c = _login(app, "q@example.com")
    a, b, check = _start_run(c, tmp_path), _start_run(c, tmp_path), _start_run(c, tmp_path, preflight_only="on")
    assert json.loads((ctx.runner.folder(a) / eta.ESTIMATE_FILE).read_text())["measures"] == 1     # counted when queued
    for r in (a, b):
        _set_size(ctx, r)
    _set_size(ctx, check, measures=10_000, preflight_only=True)
    full, quick = eta.model_seconds(BIG), eta.model_seconds(dict(BIG, measures=10_000, preflight_only=True))
    ctx.db.run("UPDATE runs SET status = 'running', started_at = ? WHERE id = ?", (time.time() - 100, a))
    # the running run has reported no time left yet: its size estimate less the time it has run
    q = ctx.queue_eta()
    assert set(q) == {b, check}
    assert q[check]["starts_in"] == pytest.approx(full - 100, abs=2)              # the input check goes next, ahead of b
    assert q[b]["starts_in"] == pytest.approx(full - 100 + quick, abs=2) and q[b]["finishes_in"] == pytest.approx(full - 100 + quick + full, abs=2)
    # its own progress report wins
    (ctx.runner.folder(a) / "progress.json").write_text(json.dumps({"stage": "pricing", "label": "Pricing", "done": 1, "total": 2, "fraction": 0.5,
                                                                    "seconds_left": 50, "updated": time.time()}))
    q = ctx.queue_eta()
    assert q[check]["starts_in"] == pytest.approx(50, abs=2) and q[b]["finishes_in"] == pytest.approx(50 + quick + full, abs=2)
    # two workers: b starts as soon as the second one is free
    ctx.s.workers = 2
    assert ctx.queue_eta()[check]["starts_in"] == 0 and ctx.queue_eta()[b]["starts_in"] == pytest.approx(quick, abs=2)
    ctx.s.workers = 1
    # the pages
    page = c.get(f"{P}/runs/{b}").text
    assert "Waiting in the queue: number 2; starts in about" in page and "(estimated)" in page
    assert "(estimated)" in c.get(f"{P}/runs/{b}/log").json()["wait"]
    assert "starts in about" in c.get(f"{P}/runs").text


def test_calibration_learns_from_finished_runs(web, tmp_path):
    app, ctx = web
    _user(ctx, "k@example.com")
    c = _login(app, "k@example.com")
    assert eta.calibration(ctx.db, ctx.s.runs_dir) == 1.0
    full = eta.model_seconds(BIG)
    for took in (2 * full, 2 * full, 3 * full):
        r = _start_run(c, tmp_path)
        _set_size(ctx, r)
        ctx.db.run("UPDATE runs SET status = 'finished', started_at = 1000, finished_at = ? WHERE id = ?", (1000 + took, r))
    small = _start_run(c, tmp_path)                                                # too small to say anything about the rate
    ctx.db.run("UPDATE runs SET status = 'finished', started_at = 1000, finished_at = 1500 WHERE id = ?", (small,))
    assert eta.calibration(ctx.db, ctx.s.runs_dir) == pytest.approx(2.0)          # the median
    queued = _start_run(c, tmp_path)
    _set_size(ctx, queued)
    assert ctx.queue_eta()[queued]["seconds"] == pytest.approx(2 * full, abs=1)
