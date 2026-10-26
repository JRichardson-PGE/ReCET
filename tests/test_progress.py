"""The run progress file (recet/progress.py) and the web app's progress bar."""
from __future__ import annotations

import json

import pytest

from recet import progress


@pytest.fixture(autouse=True)
def _default_plan():
    yield
    progress.plan(False)                                                         # plan() is module state: leave it as a fresh import has it


def test_the_bar_moves_forward_through_the_stages():
    progress.plan(True, "csv.zip")
    seq = [("preparing", None, None), ("splitting", None, None), ("preflight", 0, 5), ("preflight", 5, 5), ("pricing", 0, 5), ("pricing", 3, 5),
           ("pricing", 5, 5), ("writing", 0, 6), ("writing", 6, 6), ("file", 0, 1), ("file", 0.5, 1), ("file", 1, 1), ("finishing", None, None),
           ("done", None, None)]
    f = [progress.fraction(*s) for s in seq]
    assert f == sorted(f) and f[0] == 0.0 and f[-1] == 1.0
    assert progress.fraction("preflight", 5, 5) == pytest.approx(progress.fraction("pricing", 0, 5))   # no weight for "loading" in a run in parts
    assert progress.fraction("writing", 6, 6) == pytest.approx(progress.fraction("file", 0, 1))
    assert sum(progress._weights.values()) == pytest.approx(1.0) and progress._weights["loading"] == 0
    assert progress._weights["pricing"] == pytest.approx(210 / 413)              # its share of the 1M server run's seconds
    csv_zip = progress._weights["file"]
    progress.plan(True, "parquet")
    assert progress._weights["file"] < csv_zip and sum(progress._weights.values()) == pytest.approx(1.0)
    progress.plan(False, "csv.zip")                                              # in one piece: no splitting, no file stage
    assert progress._weights["splitting"] == progress._weights["file"] == 0 and progress._weights["loading"] > 0
    assert sum(progress._weights.values()) == pytest.approx(1.0)


def test_the_weights_follow_the_societal_and_emissions_options():
    progress.plan(True, "csv.zip")
    both = progress._weights["pricing"]
    progress.plan(True, "csv.zip", societal=False)
    no_sct = progress._weights["pricing"]
    progress.plan(True, "csv.zip", societal=False, emissions=False)
    assert progress._weights["pricing"] < no_sct < both and sum(progress._weights.values()) == pytest.approx(1.0)
    progress.plan(True, "parquet")
    progress.replan(societal=False)                                              # keeps in_parts and the format
    assert progress._weights["file"] > 0 and progress._weights["pricing"] < both
    f = progress.fraction("preflight", 5, 5)
    progress.plan(True, "parquet")
    assert progress.fraction("preflight", 5, 5) < f                              # a lighter pricing stage only moves the bar forward


def test_filing_checks_lengthen_preflight():
    progress.plan(True, "csv.zip")
    plain = progress._weights["preflight"]
    progress.plan(True, "csv.zip", filing=True)
    assert progress._weights["preflight"] > plain and sum(progress._weights.values()) == pytest.approx(1.0)
    progress.replan(societal=False)
    assert progress._plan["filing"] is True                                      # replan keeps it


def test_the_file_stage_moves_with_the_join_and_the_csv():
    assert progress.file_share(0.5, 0.0, "parquet") == 0.5 and progress.file_share(1.0, 1.0, "parquet") == 1.0
    assert progress.file_share(1.0, 0.0, "csv.zip") == pytest.approx(18 / 58)
    assert progress.file_share(0.0, 0.0, "csv") == 0.0 and progress.file_share(1.0, 1.0, "csv") == pytest.approx(1.0)


def test_the_time_left_counts_down_between_reports():
    p = {"stage": "pricing", "label": "Pricing", "done": 3, "total": 10, "fraction": 0.4, "seconds_left": 100, "updated": 1000.0}
    assert progress.live(p, now=1030.0)["seconds_left"] == 70 and p["seconds_left"] == 100      # a copy
    assert progress.live(p, now=1200.0)["seconds_left"] == 0
    assert progress.describe(progress.live(p, now=1200.0)) == "Pricing: part 3 of 10 · almost done"
    assert progress.live(dict(p, seconds_left=None), now=1200.0)["seconds_left"] is None and progress.live(None) is None


def test_report_writes_the_file_only_when_asked(tmp_path, monkeypatch):
    path = tmp_path / progress.FILE_NAME
    monkeypatch.delenv(progress.ENV, raising=False)
    progress.report("pricing", 1, 4)
    assert not path.exists()
    monkeypatch.setenv(progress.ENV, str(path))
    monkeypatch.setattr(progress, "_T0", progress.time.time() - 300)          # 300 s in
    progress.report("pricing", 2, 4)
    p = json.loads(path.read_text())
    assert p["stage"] == "pricing" and p["done"] == 2 and p["total"] == 4
    assert p["fraction"] == pytest.approx(progress.fraction("pricing", 2, 4), abs=1e-4)
    assert p["seconds_left"] == pytest.approx(300 * (1 - p["fraction"]) / p["fraction"], abs=1)
    assert progress.describe(p).startswith("Pricing: part 2 of 4 · about ") and progress.describe(p).endswith(" min left")
    progress.report("file", 0.25, 1)
    assert progress.read(path)["total"] is None and progress.describe(progress.read(path)).startswith("Finishing the measure-level file · about ")
    log = [json.loads(line) for line in (tmp_path / progress.LOG_NAME).read_text().splitlines()]
    assert [(r["stage"], r["done"], r["total"]) for r in log] == [("pricing", 2, 4), ("file", 0.25, 1)]
    assert log[0]["elapsed"] == pytest.approx(300, abs=1) and log[0]["fraction"] == p["fraction"]
    assert sorted(tmp_path.iterdir()) == sorted([path, tmp_path / progress.LOG_NAME])   # no temporary file left behind


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
    assert order == ["preparing", "splitting", "preflight", "pricing", "writing", "file", "finishing", "done"]
    n = len([1 for s, d, t in seen if s == "pricing" and d])
    assert n == seen[[s for s, *_ in seen].index("pricing")][2] == 6            # every part of the 37 rows in parts of 7
    assert max(d for s, d, t in seen if s == "writing") == 6
    assert [d for s, d, t in seen if s == "file"][-1] == 1.0
    f = [progress.fraction(*s) for s in seen]
    assert f == sorted(f)
    seen.clear()
    run_job(_cfg(tmp_path, m, pc, run_name="z", chunk_size=CHUNK, output_format="csv.zip"), log=lambda s: None)
    file = [d for s, d, t in seen if s == "file"]
    assert file == sorted(file) and file[-1] == pytest.approx(1.0) and any(0.3 < d < 1 for d in file)   # through the join, then the CSV
    assert [progress.fraction(*s) for s in seen] == sorted(progress.fraction(*s) for s in seen)
    seen.clear()
    run_job(_cfg(tmp_path, m, pc, run_name="w", chunk_size=0, output_format="parquet"), log=lambda s: None)
    assert [s for s, *_ in seen] == ["preparing", "preflight", "loading", "pricing", "writing", "finishing", "done"]
