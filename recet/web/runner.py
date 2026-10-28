"""The run queue. A run is a row in `runs` plus a folder `<state>/runs/<id>/`:

    inputs/       the uploaded files (fixed names, never the uploader's)
    config.json   the RunConfig the web form produced
    log.txt       everything the run printed
    progress.json the run's stage, parts done and time left (recet/progress.py), for the progress bar
    progress_log.jsonl every progress report with the seconds since the run started, to check the estimates against the real finish
    out/          the run's out_dir; `result_dir` (from the "results in ..." line) points inside it

`Settings.workers` threads (default 1) each claim the oldest queued run and execute it exactly as the window does, `python -m recet run --config config.json`, in its
own process group, so a run's memory and caches are its own and a Stop kills it and anything it started (a comparison runs two child processes). Raising the
worker count is the only change needed to run jobs side by side; the claim is a single SQL transaction, so two workers never take the same run.

When a run ends, its owner is emailed (notify.py) if the server has an SMTP server set and the owner has not turned it off; the email is sent on its own
thread, so a slow mail server never holds the worker.

A cleanup thread deletes runs older than `retention_days` (files gone, the row kept as 'expired' for the audit trail) and drafts untouched for `draft_days`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import progress
from .config import Settings
from .db import Database

RESULT_LINE = re.compile(r"(?:results in|comparison files in) (.+)$")
ACTIVE = ("queued", "running")


class Runner:
    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.mail_threads: list[threading.Thread] = []      # run emails being sent (tests join them)
        self.s.runs_dir.mkdir(parents=True, exist_ok=True)
        self.s.drafts_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.recover()
        for i in range(self.s.workers):
            t = threading.Thread(target=self._worker, name=f"recet-run-{i + 1}", daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._janitor, name="recet-cleanup", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def recover(self) -> None:
        """A run left 'running' by a restart has no process any more: mark it failed so its owner can start it again."""
        self.db.run("UPDATE runs SET status = 'failed', finished_at = ?, message = 'stopped: the server restarted during the run' WHERE status = 'running'",
                    (time.time(),))

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.work_once():
                    self._stop.wait(1.0)
            except Exception as exc:                         # never let one bad run kill the worker
                print(f"recet-web worker error: {exc}", file=sys.stderr, flush=True)
                self._stop.wait(5.0)

    def _janitor(self) -> None:
        while not self._stop.is_set():
            try:
                self.cleanup()
            except Exception as exc:
                print(f"recet-web cleanup error: {exc}", file=sys.stderr, flush=True)
            self._stop.wait(3600)

    # ------------------------------------------------------------------------------------------------------------------------- queue
    def folder(self, run_id: str) -> Path:
        return self.s.runs_dir / run_id

    def position(self, run_id: str) -> int | None:
        """1 = next to start. None if the run is not queued. Input checks (preflight-only runs) go ahead of full runs (see claim)."""
        row = self.db.one("SELECT created_at, status, preflight_only FROM runs WHERE id = ?", (run_id,))
        if row is None or row["status"] != "queued":
            return None
        if row["preflight_only"]:
            sql = "SELECT COUNT(*) n FROM runs WHERE status = 'queued' AND preflight_only = 1 AND created_at <= ?"
        else:
            sql = "SELECT COUNT(*) n FROM runs WHERE status = 'queued' AND (preflight_only = 1 OR created_at <= ?)"
        return self.db.one(sql, (row["created_at"],))["n"]

    def claim(self):
        """Take the next queued run (atomically) and mark it running: input checks (preflight-only runs, a few seconds each) first, then the oldest run,
        so checking a job's inputs never waits behind a queue of full runs (it still waits for a run that has already started)."""
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM runs WHERE status = 'queued' ORDER BY preflight_only DESC, created_at LIMIT 1").fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            c.execute("UPDATE runs SET status = 'running', started_at = ? WHERE id = ?", (time.time(), row["id"]))
            c.execute("COMMIT")
            return row

    def work_once(self) -> bool:
        row = self.claim()
        if row is None:
            return False
        self.execute(row["id"])
        return True

    def cancel(self, run_id: str) -> None:
        row = self.db.one("SELECT status FROM runs WHERE id = ?", (run_id,))
        if row is None:
            return
        if row["status"] == "queued":
            self.db.run("UPDATE runs SET status = 'cancelled', finished_at = ?, message = 'stopped before it started' WHERE id = ? AND status = 'queued'",
                        (time.time(), run_id))
        elif row["status"] == "running":
            self.db.run("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))

    # ------------------------------------------------------------------------------------------------------------------------- one run
    def execute(self, run_id: str) -> None:
        d = self.folder(run_id)
        log_path = d / "log.txt"
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", **{progress.ENV: str(d / progress.FILE_NAME)})
        cmd = [sys.executable, "-m", "recet", "run", "--config", str(d / "config.json")]
        result_dir: list[str] = []
        status, message, code = "failed", "", None
        with open(log_path, "a", encoding="utf-8") as log:
            try:
                proc = subprocess.Popen(cmd, cwd=str(d), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                        bufsize=1, env=env, start_new_session=(os.name == "posix"))
            except OSError as exc:
                log.write(f"\ncould not start the run: {exc}\n")
                self._finish(run_id, "failed", None, None, f"could not start: {exc}")
                return

            def read():
                assert proc.stdout is not None
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    m = RESULT_LINE.search(line.rstrip("\n"))
                    if m:
                        result_dir.append(m.group(1).strip())

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            deadline = time.time() + self.s.run_timeout
            while proc.poll() is None:
                if self.db.one("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,))["cancel_requested"]:
                    _kill(proc)
                    status, message = "cancelled", "stopped by its owner"
                    break
                if time.time() > deadline:
                    _kill(proc)
                    status, message = "failed", f"stopped: ran longer than {self.s.run_timeout // 60} minutes"
                    break
                time.sleep(0.5)
            code = proc.wait()
            reader.join(timeout=10)
            if not message:
                status = "finished" if code == 0 else "failed"
                message = "" if code == 0 else f"exit code {code}: see the log"
            log.write(f"\n{'finished.' if status == 'finished' else message}\n")
        self._finish(run_id, status, code, self._result_dir(d, result_dir), message)

    def _result_dir(self, d: Path, from_log: list[str]) -> str | None:
        out = (d / "out").resolve()
        for cand in reversed(from_log):
            p = Path(cand)
            p = (p if p.is_absolute() else d / p).resolve()
            if p.is_dir() and p.is_relative_to(out):
                return str(p.relative_to(d.resolve()))
        for pattern in ("comparison_summary_*", "measure_level_*", "preflight.txt"):     # e.g. a preflight-only run prints no "results in" line
            hits = sorted(out.rglob(pattern)) if out.exists() else []
            if hits:
                return str(hits[0].parent.relative_to(d.resolve()))
        return None

    def _finish(self, run_id, status, code, result_dir, message) -> None:
        self.db.run("UPDATE runs SET status = ?, exit_code = ?, result_dir = ?, message = ?, finished_at = ? WHERE id = ?",
                    (status, code, result_dir, message, time.time(), run_id))
        from . import notify
        if notify.enabled(self.s):
            t = threading.Thread(target=self._notify, args=(run_id,), name=f"recet-email-{run_id}", daemon=True)
            t.start()
            self.mail_threads.append(t)

    def _notify(self, run_id: str) -> None:
        from . import notify
        try:
            notify.run_ended(self.s, self.db, run_id)
        except Exception as exc:                             # an email must never break the queue
            print(f"recet-web email error: {exc}", file=sys.stderr, flush=True)

    # ------------------------------------------------------------------------------------------------------------------------- cleanup
    def cleanup(self, now: float | None = None) -> dict:
        now = now or time.time()
        n_runs = n_drafts = 0
        for row in self.db.all("SELECT id FROM runs WHERE status NOT IN ('queued', 'running', 'expired') AND created_at < ?",
                               (now - self.s.retention_days * 86400,)):
            shutil.rmtree(self.folder(row["id"]), ignore_errors=True)
            self.db.run("UPDATE runs SET status = 'expired', result_dir = NULL WHERE id = ?", (row["id"],))
            n_runs += 1
        for row in self.db.all("SELECT id FROM drafts WHERE updated_at < ?", (now - self.s.draft_days * 86400,)):
            shutil.rmtree(self.s.drafts_dir / row["id"], ignore_errors=True)
            self.db.run("DELETE FROM drafts WHERE id = ?", (row["id"],))
            n_drafts += 1
        for f in (self.s.state_dir / "tmp").glob("download_*.zip"):           # download zips left by a client that disconnected before the file was removed
            try:
                if f.stat().st_mtime < now - 3600:
                    f.unlink()
            except OSError:
                pass
        from . import auth
        auth.prune(self.db)
        return {"runs": n_runs, "drafts": n_drafts}


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            pass


def write_config(folder: Path, cfg: dict) -> Path:
    p = folder / "config.json"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p
