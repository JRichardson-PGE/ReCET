"""Regenerate requirements/{engine,worker,web,test}.txt from pyproject.toml with uv (see requirements/README.md). `--check` exits 1 when a file would change."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOCKS = {"engine": [], "worker": ["--extra", "s3"], "web": ["--extra", "web"], "test": ["--extra", "web", "--extra", "test"]}
PYTHON = "3.11"                         # the oldest Python the lock serves (CI tests 3.11 and 3.12; the laptop runs 3.14)


def compile_lock(name: str, out: Path, upgrade: bool) -> None:
    uv = shutil.which("uv")
    if uv is None:
        sys.exit("uv is not installed: pip install uv")
    cmd = [uv, "pip", "compile", "pyproject.toml", *LOCKS[name], "--universal", "--python-version", PYTHON, "--generate-hashes", "--no-header",
           "--quiet", "-o", str(out)]
    if upgrade:
        cmd.append("--upgrade")
    elif (HERE / f"{name}.txt").exists() and out != HERE / f"{name}.txt":
        shutil.copyfile(HERE / f"{name}.txt", out)          # uv keeps the versions already in the output file
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--upgrade", action="store_true", help="take the newest versions pyproject.toml allows")
    ap.add_argument("--check", action="store_true", help="only check that the files match pyproject.toml")
    a = ap.parse_args()
    stale = []
    for name in LOCKS:
        if a.check:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / f"{name}.txt"
                compile_lock(name, out, upgrade=False)
                if out.read_text(encoding="utf-8") != (HERE / f"{name}.txt").read_text(encoding="utf-8"):
                    stale.append(name)
        else:
            compile_lock(name, HERE / f"{name}.txt", a.upgrade)
    if stale:
        print(f"requirements/{', '.join(stale)}.txt no longer match pyproject.toml: run `python requirements/lock.py` and commit the result")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
