"""The record cap (BRD v0.5 BR-06.3): a job with more Measure.csv records than RunConfig.max_records (2,000,000 by default) is refused in preflight, before
it is split or priced, with one clear message; a CET replica refuses it the same way; 0 = no cap."""
from __future__ import annotations

import pytest

from recet import cli, preflight
from recet.jobs import RunConfig, run_job
from test_chunked import _cfg, _job


def test_the_default_cap_is_two_million():
    assert RunConfig().max_records == preflight.MAX_RECORDS == 2_000_000


def test_preflight_refuses_a_job_over_the_cap(tmp_path):
    m, pc = _job(tmp_path, n=12)
    rep = preflight.check_inputs(m, pc, max_records=11)
    assert not rep.ok
    assert [i.code for i in rep.issues] == ["too_many_records"]          # nothing else is checked
    assert "12 records, more than the 11 a run takes" in rep.issues[0].message
    assert rep.stats["measures"] == "12"
    for cap in (12, 0):                                                    # at the cap, or no cap: checked as usual
        assert "too_many_records" not in {i.code for i in preflight.check_inputs(m, pc, max_records=cap).issues}


def test_a_run_over_the_cap_stops_before_splitting(tmp_path):
    m, pc = _job(tmp_path, n=12)
    with pytest.raises(RuntimeError, match="blocking problems"):
        run_job(_cfg(tmp_path, m, pc, max_records=10, chunk_size=5), log=lambda s: None)
    run = next((tmp_path / "out").iterdir())
    assert "too_many_records" in (run / "preflight.txt").read_text(encoding="utf-8")
    assert not (run / "_parts").exists()


def test_a_run_at_the_cap_is_priced_in_parts(tmp_path):
    m, pc = _job(tmp_path, n=12)
    res = run_job(_cfg(tmp_path, m, pc, max_records=12, chunk_size=5), log=lambda s: None)
    assert res.frame is None and res.files["measure_level"].exists()


def test_a_replica_refuses_a_job_over_the_cap(tmp_path):
    from test_engine import _cfg as replica_cfg
    with pytest.raises(ValueError, match="41 records, more than the 40"):
        run_job(replica_cfg(tmp_path, max_records=40), log=lambda s: None)


def test_cli_max_records(tmp_path):
    a = cli.build_parser().parse_args(["run", "--measures", "m.csv", "--max-records", "500"])
    assert cli._config_from_args(a).max_records == 500
