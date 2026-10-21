"""The web app's settings, read from environment variables (all optional).

RECET_WEB_STATE          folder for the database, uploads, runs and drafts (default ~/.recet/web)
RECET_WEB_PREFIX         URL path the app lives under (default /ReCET); every page, asset and cookie uses it
RECET_WEB_SECURE_COOKIES 1 (default) sends the session cookie over HTTPS only; set 0 to try the app over plain http on another machine
RECET_WEB_TRUST_PROXY    1 (default) takes the client address from X-Real-IP (set by the edge nginx) for login rate limits and the audit log
RECET_WEB_WORKERS        how many runs may execute at once (default 1: the server has 1.8 GB of memory)
RECET_WEB_ACC_VERSIONS   the ACC versions offered (default "2024,2026")
RECET_WEB_RETENTION_DAYS days a run's uploads and results are kept (default 7)
RECET_WEB_DRAFT_DAYS     days an untouched Edit inputs draft is kept (default 30)
RECET_WEB_MAX_UPLOAD_MB  largest accepted upload, per file (default 50)
RECET_WEB_RUN_TIMEOUT    seconds before a run is stopped (default 7200)
RECET_WEB_RUN_DEFAULTS   JSON merged into every run's RunConfig (tests use it to supply a synthetic ACC; not needed in production)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip().lower() not in ("0", "false", "no", "off")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


@dataclass
class Settings:
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("RECET_HOME") or Path.home() / ".recet") / "web")
    prefix: str = "/ReCET"
    secure_cookies: bool = True
    trust_proxy: bool = True
    workers: int = 1
    acc_versions: tuple[str, ...] = ("2024", "2026")
    retention_days: int = 7
    draft_days: int = 30
    max_upload_mb: int = 50
    run_timeout: int = 7200
    run_defaults: dict = field(default_factory=dict)
    start_workers: bool = True          # tests turn the background threads off and drive the queue by hand
    session_days: int = 14
    setup_link_hours: int = 72

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        if os.environ.get("RECET_WEB_STATE"):
            s.state_dir = Path(os.environ["RECET_WEB_STATE"]).expanduser()
        prefix = os.environ.get("RECET_WEB_PREFIX", s.prefix).strip()
        s.prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""
        s.secure_cookies = _flag("RECET_WEB_SECURE_COOKIES", True)
        s.trust_proxy = _flag("RECET_WEB_TRUST_PROXY", True)
        s.workers = max(1, _int("RECET_WEB_WORKERS", 1))
        accs = os.environ.get("RECET_WEB_ACC_VERSIONS", "")
        if accs.strip():
            s.acc_versions = tuple(a.strip() for a in accs.split(",") if a.strip())
        s.retention_days = _int("RECET_WEB_RETENTION_DAYS", 7)
        s.draft_days = _int("RECET_WEB_DRAFT_DAYS", 30)
        s.max_upload_mb = _int("RECET_WEB_MAX_UPLOAD_MB", 50)
        s.run_timeout = _int("RECET_WEB_RUN_TIMEOUT", 7200)
        if os.environ.get("RECET_WEB_RUN_DEFAULTS", "").strip():
            s.run_defaults = json.loads(os.environ["RECET_WEB_RUN_DEFAULTS"])
        return s

    @property
    def db_path(self) -> Path:
        return self.state_dir / "recet_web.sqlite3"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def drafts_dir(self) -> Path:
        return self.state_dir / "drafts"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024
