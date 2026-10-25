"""The run progress file (recet/progress.py) and the web app's progress bar."""
from __future__ import annotations

import json

import pytest

from recet import progress


def test_the_bar_moves_forward_through_the_stages():
    seq = [("preparing", None, None), ("splitting", None, None), ("preflight", 0, 5), ("preflight", 5, 5), ("pricing", 0, 5), ("pricing", 3, 5),
           ("pricing", 5, 5), ("writing", 0, 6), ("writing", 6, 6), ("finishing", None, None), ("done", None, None)]
    f = [progress.fraction(*s) for s in seq]
    assert f == sorted(f) and f[0] == 0.0 and f[-1] == 1.0
    assert progress.fraction("pricing", 5, 5) == pytest.approx(progress.fraction("writing", 0, 6))
    assert sum(w for _l, w in progress.STAGES.values()) == pytest.approx(1.0)


def test_report_writes_the_file_only_when_asked(tmp_path, monkeypatch):
    path = tmp_path / progress.FILE_NAME
    monkeypatch.delenv(progress.ENV, raising=False)
    progress.report("pricing", 1, 4)
    assert not path.exists()
    monkeypatch.setenv(progress.ENV, str(path))
    monkeypatch.setattr(progress, "_T0", progress.time.time() - 100)          # 100 s in
    progress.report("pricing", 2, 4)
    p = json.loads(path.read_text())
    assert p["stage"] == "pricing" and p["done"] == 2 and p["total"] == 4
    assert p["fraction"] == pytest.approx(progress.fraction("pricing", 2, 4), abs=1e-4)
    assert p["seconds_left"] == pytest.approx(100 * (1 - p["fraction"]) / p["fraction"], abs=1)
    assert progress.describe(p).startswith("Pricing: part 2 of 4 · about ") and progress.describe(p).endswith(" min left")
    assert list(tmp_path.iterdir()) == [path]                                  # no temporary file left behind


def test_a_comparison_fills_the_bar_run_by_run(tmp_path, monkeypatch):
    path = tmp_path / progress.FILE_NAME
    monkeypatch.setenv(progress.ENV, str(path))
    monkeypatch.setenv(progress.SLICE_ENV, "1/2")
    progress.report("done")
    p = progress.read(path)
    assert p["fraction"] == pytest.approx(progress.COMPARE_SHARE) and p["label"].startswith("run 2 of 2")
    monkeypatch.setenv(progress.SLICE_ENV, "0/2")
    progress.report("preparing")
    assert progress.read(path)["fraction"] == 0.0
    monkeypatch.delenv(progress.SLICE_ENV)
    progress.report("comparing")
    assert progress.read(path)["fraction"] == pytest.approx(progress.COMPARE_SHARE)


def test_a_run_in_parts_reports_every_part(tmp_path, monkeypatch):
    from test_chunked import CHUNK, _cfg, _job
    from recet.jobs import run_job
    seen = []
    monkeypatch.setattr(progress, "report", lambda stage, done=None, total=None: seen.append((stage, done, total)))
    m, pc = _job(tmp_path)
    run_job(_cfg(tmp_path, m, pc, run_name="p", chunk_size=CHUNK, output_format="parquet"), log=lambda s: None)
    stages = [s for s, _d, _t in seen]
    assert stages[0] == "preparing" and stages[-1] == "done"
    order = [s for i, s in enumerate(stages) if i == 0 or stages[i - 1] != s]
    assert order == ["preparing", "splitting", "preflight", "pricing", "writing", "finishing", "done"]
    n = len([1 for s, d, t in seen if s == "pricing" and d])
    assert n == seen[[s for s, *_ in seen].index("pricing")][2] == 6            # every part of the 37 rows in parts of 7
    f = [progress.fraction(*s) for s in seen]
    assert f == sorted(f)
    seen.clear()
    run_job(_cfg(tmp_path, m, pc, run_name="w", chunk_size=0, output_format="parquet"), log=lambda s: None)
    assert [s for s, *_ in seen] == ["preparing", "preflight", "loading", "pricing", "writing", "finishing", "done"]

