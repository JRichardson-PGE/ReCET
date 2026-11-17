"""deploy/backup: the nightly backup copies a consistent database and mirrors the state folder (without the live database, parts folders or temporary
files), and the restore script brings both back into a new folder, checking the copy's SHA-256. Run against a fake `aws` that keeps "S3" in a folder."""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKUP = Path(__file__).resolve().parents[1] / "deploy" / "backup"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="the scripts run on the Linux server")

FAKE_AWS = r'''#!PYTHON
"""A fake aws CLI: s3 cp, s3 sync (--delete, --exclude), s3api head-object, over the folder $FAKE_S3 (s3://bucket/key -> $FAKE_S3/bucket/key)."""
import fnmatch, json, os, shutil, sys
from pathlib import Path
root = Path(os.environ["FAKE_S3"])
def local(u):
    return root / u[5:] if u.startswith("s3://") else Path(u)
a = [x for x in sys.argv[1:] if x != "--only-show-errors"]
pos = [x for i, x in enumerate(a) if not x.startswith("--") and not (i and a[i - 1] in ("--exclude", "--metadata", "--bucket", "--key", "--query", "--output"))]
if a[:2] == ["s3", "cp"]:
    src, dst = pos[2], pos[3]
    meta = dict(m.split("=", 1) for m in a[a.index("--metadata") + 1].split(",")) if "--metadata" in a else {}
    if dst == "-":
        sys.stdout.write(local(src).read_text()); sys.exit(0)
    d = local(dst); d.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(local(src), d)
    if meta:
        d.with_name(d.name + ".meta.json").write_text(json.dumps(meta))
elif a[:2] == ["s3", "sync"]:
    src, dst = local(pos[2]), local(pos[3])
    ex = [a[i + 1] for i, x in enumerate(a) if x == "--exclude"]
    keep = set()
    for f in src.rglob("*"):
        rel = f.relative_to(src).as_posix()
        if f.is_file() and not any(fnmatch.fnmatch(rel, p) for p in ex):
            keep.add(rel); t = dst / rel; t.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(f, t)
    if "--delete" in a and dst.exists():
        for f in list(dst.rglob("*")):
            if f.is_file() and f.relative_to(dst).as_posix() not in keep:
                f.unlink()
elif a[:2] == ["s3api", "head-object"]:
    b, k = a[a.index("--bucket") + 1], a[a.index("--key") + 1]
    m = root / b / (k + ".meta.json")
    print(json.loads(m.read_text()).get("sha256", "None") if m.exists() else "None")
else:
    sys.exit(f"fake aws: unsupported {a}")
'''


@pytest.fixture
def env(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    aws = bin_ / "aws"
    aws.write_text(FAKE_AWS.replace("PYTHON", sys.executable))
    aws.chmod(0o755)
    state = tmp_path / "state"
    (state / "runs" / "r1" / "_parts").mkdir(parents=True)
    (state / "runs" / "r1" / "measure_level_acc2024.csv.zip").write_bytes(b"results")
    (state / "runs" / "r1" / "_parts" / "p0.csv").write_text("temporary")
    (state / "drafts").mkdir()
    (state / "drafts" / "d1.json").write_text("{}")
    (state / "tmp").mkdir()
    (state / "tmp" / "download.zip").write_bytes(b"zip")
    db = sqlite3.connect(state / "recet_web.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, name TEXT)")
    db.executemany("INSERT INTO users (email) VALUES (?)", [("a@x",), ("b@x",)])
    db.execute("INSERT INTO runs (name) VALUES ('r1')")
    db.commit()                                          # left open, as the running app holds it
    e = dict(os.environ, PATH=f"{bin_}{os.pathsep}{os.environ['PATH']}", FAKE_S3=str(tmp_path / "s3"), RECET_BACKUP_BUCKET="s3://bucket/recet/",
             RECET_STATE_DIR=str(state))
    yield e, tmp_path, state
    db.close()


def _run(script, env, *args):
    return subprocess.run(["bash", str(BACKUP / script), *args], env=env, capture_output=True, text=True)


def test_backup_and_restore(env):
    e, tmp, state = env
    p = _run("recet-backup.sh", e)
    assert p.returncode == 0, p.stderr
    assert "runs=1 users=2" in p.stdout
    s3 = tmp / "s3" / "bucket" / "recet"
    last = json.loads((s3 / "last_backup.json").read_text())
    copy = s3 / last["database"]
    with gzip.open(copy) as fh:
        assert fh.read(16) == b"SQLite format 3\x00"
    mirrored = sorted(f.relative_to(s3 / "state").as_posix() for f in (s3 / "state").rglob("*") if f.is_file())
    assert mirrored == ["drafts/d1.json", "runs/r1/measure_level_acc2024.csv.zip"]

    shutil.rmtree(state / "drafts")                       # a draft cleaned up by retention leaves the mirror on the next night
    assert _run("recet-backup.sh", e).returncode == 0
    assert not (s3 / "state" / "drafts" / "d1.json").exists()

    p = _run("recet-restore.sh", e, str(tmp / "restored"))
    assert p.returncode == 0, p.stderr
    assert "runs=1 users=2" in p.stdout
    r = sqlite3.connect(tmp / "restored" / "recet_web.sqlite3")
    assert r.execute("SELECT email FROM users ORDER BY id").fetchall() == [("a@x",), ("b@x",)]
    r.close()
    assert (tmp / "restored" / "runs" / "r1" / "measure_level_acc2024.csv.zip").read_bytes() == b"results"


def test_restore_refuses_a_changed_copy_and_a_full_folder(env):
    e, tmp, _state = env
    assert _run("recet-backup.sh", e).returncode == 0
    s3 = tmp / "s3" / "bucket" / "recet"
    copy = s3 / json.loads((s3 / "last_backup.json").read_text())["database"]
    copy.write_bytes(copy.read_bytes() + b"x")
    p = _run("recet-restore.sh", e, str(tmp / "r1"))
    assert p.returncode == 1 and "is not the recorded" in p.stdout
    (tmp / "r2").mkdir()
    (tmp / "r2" / "something").write_text("x")
    p = _run("recet-restore.sh", e, str(tmp / "r2"))
    assert p.returncode == 1 and "not empty" in p.stdout


def test_dry_run_and_a_missing_database(env):
    e, tmp, state = env
    p = _run("recet-backup.sh", dict(e, RECET_BACKUP_DRYRUN="1"))
    assert p.returncode == 0 and "aws s3 sync --only-show-errors --delete" in p.stdout and not (tmp / "s3").exists()
    p = _run("recet-backup.sh", dict(e, RECET_STATE_DIR=str(tmp / "nowhere")))
    assert p.returncode == 1 and "no database" in p.stdout
