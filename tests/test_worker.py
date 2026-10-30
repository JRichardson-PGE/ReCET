"""`recet worker run`: a job spec (jobspec.py) is checked, its inputs fetched (local or s3://), the job run and its outputs sent with a receipt.json that
names exactly what produced them; a refused spec runs nothing (exit 2), a failed run still gets a receipt (exit 1)."""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from recet import __version__, jobspec, storage, worker
from conftest import make_electric_acc, make_flat_profile_csv
from test_chunked import _job


def _sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture
def job(tmp_path):
    m, pc = _job(tmp_path, n=6)
    acc = make_electric_acc(tmp_path / "acc.parquet", years=(2027, 2028, 2029, 2030, 2031, 2032), transmission=5.0)
    prof = make_flat_profile_csv(tmp_path / "p.csv")
    return {"measures": m, "programcost": pc, "acc": acc, "profiles": prof}


def _spec(job, tmp_path, **kw):
    spec = {"spec_version": 1, "job_id": "job-0001", "engine": {"release": __version__, "calculation": "recet"},
            "inputs": {"measures": str(job["measures"]), "programcost": {"uri": str(job["programcost"]), "sha256": _sha(job["programcost"])},
                       "acc_electric": str(job["acc"]), "profiles_8760": job["profiles"].as_uri()},
            "settings": {"acc_version": "2026", "emissions": False}, "outputs": {"uri": str(tmp_path / "results"), "format": "parquet"}}
    spec.update(kw)
    f = tmp_path / "job.json"
    f.write_text(json.dumps(spec), encoding="utf-8")
    return f


def test_a_local_job_runs_and_its_receipt_names_everything(job, tmp_path):
    code, receipt = worker.run(str(_spec(job, tmp_path)), log=lambda s: None)
    assert code == 0 and receipt["status"] == "succeeded" and receipt["error"] is None
    out = tmp_path / "results"
    on_disk = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
    assert on_disk["job_id"] == "job-0001" and on_disk["receipt_version"] == worker.RECEIPT_VERSION
    assert receipt["engine"]["release"] == __version__ and receipt["engine"]["calculation"]["key"] == "recet"
    assert receipt["inputs"]["measures"] == {"uri": str(job["measures"]), "sha256": _sha(job["measures"]), "bytes": job["measures"].stat().st_size, "rows": 6}
    assert receipt["inputs"]["programcost"]["sha256"] == _sha(job["programcost"])
    assert receipt["settings"]["acc_version"] == "2026" and receipt["settings"]["measures"] == "<input measures>"
    assert receipt["acc"]["version"] == "2026" and sum(r["measures"] for r in receipt["acc"]["by_record"]) == 6
    assert receipt["allocation"]["table"].startswith("statewide_factors_") and receipt["run_parameters"]["source"] == "built-in"
    assert len(receipt["packs"]["package_data_sha256"]) == 64
    ml = next(k for k in receipt["outputs"] if k.startswith("measure_level"))
    assert receipt["outputs"][ml]["sha256"] == _sha(out / ml) and receipt["outputs"][ml]["uri"] == str(out / ml)
    assert receipt["totals"] and "TotalSystemBenefit" in receipt["totals"]


def test_a_job_zip(job, tmp_path):
    z = tmp_path / "job.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(job["measures"], "Measure.csv")
        zf.write(job["programcost"], "ProgramCost.csv")
    spec = _spec(job, tmp_path)
    d = json.loads(spec.read_text())
    d["inputs"] = {"job_zip": str(z), "acc_electric": str(job["acc"]), "profiles_8760": str(job["profiles"])}
    spec.write_text(json.dumps(d))
    code, receipt = worker.run(str(spec), log=lambda s: None)
    assert code == 0 and receipt["inputs"]["job_zip/measures"]["rows"] == 6 and receipt["inputs"]["job_zip"]["sha256"] == _sha(z)


class FakeS3:
    """boto3's download_file / upload_file over a folder."""

    def __init__(self, root: Path):
        self.root = root

    def download_file(self, bucket, key, dest):
        src = self.root / bucket / key
        if not src.exists():
            raise FileNotFoundError(key)
        Path(dest).write_bytes(src.read_bytes())

    def upload_file(self, src, bucket, key):
        dest = self.root / bucket / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(src).read_bytes())


def test_s3_inputs_and_outputs(job, tmp_path, monkeypatch):
    s3 = tmp_path / "s3"
    for name in ("measures", "programcost"):
        (s3 / "bucket" / "jobs").mkdir(parents=True, exist_ok=True)
        (s3 / "bucket" / "jobs" / Path(job[name]).name).write_bytes(Path(job[name]).read_bytes())
    monkeypatch.setattr(storage, "_S3", storage.S3Storage(client=FakeS3(s3)))
    spec = json.loads(_spec(job, tmp_path).read_text())
    spec["inputs"].update(measures="s3://bucket/jobs/Measure.csv", programcost="s3://bucket/jobs/ProgramCost.csv")
    spec["outputs"]["uri"] = "s3://bucket/runs/job-0001/"
    (s3 / "bucket" / "specs").mkdir()
    (s3 / "bucket" / "specs" / "job.json").write_text(json.dumps(spec))
    code, receipt = worker.run("s3://bucket/specs/job.json", log=lambda s: None)
    assert code == 0
    assert json.loads((s3 / "bucket" / "runs" / "job-0001" / "receipt.json").read_text())["status"] == "succeeded"
    assert receipt["inputs"]["measures"]["uri"] == "s3://bucket/jobs/Measure.csv"
    assert all(r["uri"].startswith("s3://bucket/runs/job-0001/") for r in receipt["outputs"].values())


@pytest.mark.parametrize("change, why", [
    ({"engine": {"release": "0.0.1"}}, "this worker is recet"),
    ({"allocation_version": "statewide_factors_19990101"}, "allocation_version"),
    ({"packs": {"files": {"acc/2024/electric_hourly.parquet": {"sha256": "0" * 64}}}}, "not in the reference folder"),
    ({"settings": {"out_dir": "/tmp"}}, "has its own block"),
])
def test_refused_specs_run_nothing(job, tmp_path, capsys, change, why):
    code, receipt = worker.run(str(_spec(job, tmp_path, **change)), log=lambda s: None)
    assert code == 2 and receipt is None and why in capsys.readouterr().err
    assert not (tmp_path / "results").exists()


def test_a_pinned_input_that_differs_is_refused(job, tmp_path, capsys):
    spec = json.loads(_spec(job, tmp_path).read_text())
    spec["inputs"]["measures"] = {"uri": str(job["measures"]), "sha256": "a" * 64}
    (tmp_path / "job.json").write_text(json.dumps(spec))
    assert worker.run(str(tmp_path / "job.json"), log=lambda s: None)[0] == 2 and "is not the pinned" in capsys.readouterr().err


def test_pinned_reference_files(job, tmp_path, capsys):
    ref = tmp_path / "ref"
    (ref / "acc" / "2024").mkdir(parents=True)
    f = make_electric_acc(ref / "acc" / "2024" / "electric_hourly.parquet", years=(2027, 2028, 2029, 2030, 2031, 2032))
    (ref / "manifest.json").write_text(json.dumps({"format": 1, "files": {"acc/2024/electric_hourly.parquet": {"size": f.stat().st_size, "sha256": _sha(f)}}}))
    good = {"reference_dir": str(ref), "files": {"acc/2024/electric_hourly.parquet": {"sha256": _sha(f), "version": "2024 final"}}}
    spec = json.loads(_spec(job, tmp_path, packs=good).read_text())
    del spec["inputs"]["acc_electric"]
    spec["settings"]["acc_version"] = "2024"
    (tmp_path / "job.json").write_text(json.dumps(spec))
    code, receipt = worker.run(str(tmp_path / "job.json"), log=lambda s: None)
    assert code == 0
    assert receipt["packs"]["files"]["acc/2024/electric_hourly.parquet"] == {"bytes": f.stat().st_size, "sha256": _sha(f), "sha256_from": "manifest.json",
                                                                             "version": "2024 final"}
    spec["packs"]["files"]["acc/2024/electric_hourly.parquet"]["sha256"] = "b" * 64
    (tmp_path / "job.json").write_text(json.dumps(spec))
    assert worker.run(str(tmp_path / "job.json"), log=lambda s: None)[0] == 2 and "not the pinned" in capsys.readouterr().err


def test_a_failed_run_still_gets_a_receipt(job, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(job["measures"].read_text().replace("2028Q1", "2028Q9"))
    spec = json.loads(_spec(job, tmp_path).read_text())
    spec["inputs"]["measures"] = str(bad)
    (tmp_path / "job.json").write_text(json.dumps(spec))
    code, receipt = worker.run(str(tmp_path / "job.json"), log=lambda s: None)
    assert code == 1 and receipt["status"] == "failed" and "blocking problems" in receipt["error"]
    assert (tmp_path / "results" / "receipt.json").exists() and (tmp_path / "results" / "preflight.txt").exists()


def test_validate():
    ok = {"spec_version": 1, "job_id": "a", "inputs": {"measures": "m.csv"}, "outputs": {"uri": "out"}}
    assert jobspec.validate(ok) == []
    bad = dict(ok, job_id="-x", inputs={"measures": "m.csv", "job_zip": "j.zip", "nope": "x"}, settings={"chunk_size": "50", "acc_version": 2024, "zzz": 1},
               outputs={"uri": "out", "format": "docx"}, extra=1)
    errors = "\n".join(jobspec.validate(bad))
    for text in ("unknown top-level key 'extra'", "job_id", "unknown input 'nope'", "exactly one of measures, job_zip or workbook",
                 "settings.chunk_size: expected int", "settings.zzz", "outputs: uri and format"):
        assert text in errors, text
    assert "acc_version" not in errors                                 # a number is accepted for a text setting


def test_the_schema_matches_the_validator():
    s = jobspec.schema()
    assert set(s["properties"]) == jobspec.TOP
    assert set(s["properties"]["inputs"]["properties"]) == set(jobspec.INPUTS)
    assert s["properties"]["spec_version"]["const"] == jobspec.SPEC_VERSION
    assert s["properties"]["outputs"]["properties"]["format"]["enum"] == list(jobspec.OUTPUT_FORMATS)


def test_cli(job, tmp_path, capsys):
    from recet import cli
    spec = _spec(job, tmp_path)
    with pytest.raises(SystemExit) as e:
        cli.main(["worker", "validate", str(spec)])
    assert e.value.code == 0 and "valid" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["worker", "run", str(spec)])
    assert e.value.code == 0 and (tmp_path / "results" / "receipt.json").exists()


def test_context_progress_and_a_supplied_allocation_table(job, tmp_path):
    from recet.pipeline import statewide_allocation
    sw = tmp_path / "sw_factors_v42.csv"
    sw.write_bytes(statewide_allocation.FACTORS_CSV.read_bytes())
    spec = json.loads(_spec(job, tmp_path, context={"platform_run_id": 123, "submission_version": "claims/PGE/2028Q1/v2"},
                            allocation_version=_sha(sw)).read_text())
    spec["inputs"]["sw_factors"] = str(sw)
    (tmp_path / "job.json").write_text(json.dumps(spec))
    code, receipt = worker.run(str(tmp_path / "job.json"), log=lambda s: None)
    assert code == 0 and receipt["context"] == {"platform_run_id": 123, "submission_version": "claims/PGE/2028Q1/v2"}
    assert receipt["allocation"] == {"table": "supplied", "file": str(sw), "sha256": _sha(sw), "requested": _sha(sw), "applied": False, "mode": "merge"}
    progress = json.loads((tmp_path / "results" / "progress.json").read_text())
    assert progress["stage"] == "done" and progress["fraction"] == 1.0


def test_the_progress_sender(tmp_path):
    local, dest = tmp_path / "progress.json", tmp_path / "out" / "progress.json"
    s = worker.ProgressSender(local, str(dest), every=3600, poll=3600)
    s.send()
    assert s.sends == 0 and not dest.exists()                          # nothing reported yet
    for stage, done, sends in (("pricing", 0, 1), ("pricing", 1, 1), ("writing", 0, 2), ("writing", 0, 2)):
        local.write_text(json.dumps({"stage": stage, "done": done}))
        s.send()
        assert s.sends == sends, (stage, done)                         # a new stage is sent at once; parts within one wait for `every`
    local.write_text(json.dumps({"stage": "writing", "done": 3}))
    s.send(force=True)
    assert s.sends == 3 and json.loads(dest.read_text())["done"] == 3


def test_file_uris_round_trip(tmp_path):
    p = tmp_path / "a folder" / "Measure.csv"
    assert storage.LocalStorage.path(p.as_uri()) == p                 # on Windows as_uri() is file:///C:/..., which once read as /C:/...
    assert storage.LocalStorage.path(str(p)) == p
    assert storage.LocalStorage.path("file://localhost" + p.as_uri()[len("file://"):]) == p
    if os.name == "nt":
        assert storage.LocalStorage.path("file://server/share/x.csv") == Path(r"\\server\share\x.csv")


def test_a_pinned_allocation_table_replaces_the_builtin_one(job, tmp_path):
    from recet.pipeline import statewide_allocation
    sw = tmp_path / "sw_factors_v43.csv"
    sw.write_bytes(statewide_allocation.FACTORS_CSV.read_bytes())
    spec = json.loads(_spec(job, tmp_path, allocation_version=_sha(sw)).read_text())
    spec["inputs"]["sw_factors"] = str(sw)
    spec["settings"].update(allocate_statewide=True, sw_factors_replace=True)
    (tmp_path / "job.json").write_text(json.dumps(spec))
    code, receipt = worker.run(str(tmp_path / "job.json"), log=lambda s: None)
    assert code == 0 and receipt["settings"]["sw_factors_replace"] is True
    assert receipt["allocation"]["mode"] == "replace" and receipt["allocation"]["applied"] is True and receipt["allocation"]["sha256"] == _sha(sw)
