"""Email when a run ends (BRD BR-06.2, NFR Reliability): the run's owner gets one email when a run finishes or fails.

Sent through the SMTP server the RECET_WEB_SMTP_* settings name (config.py); with no RECET_WEB_SMTP_HOST nothing is sent and nothing else changes. Each
user can turn it off on the Account page (user_settings key `notify_runs`, "0" = off; on by default). Not emailed: a run stopped by its owner, and an
input check (a preflight-only run) that ended normally, which takes seconds and is watched on its page.

The email holds the run's name, its status and time taken, the page's address (RECET_WEB_BASE_URL + the prefix + /runs/<id>; without a base URL only the
path), and for a finished run its headline totals from run_settings.json (TSB, TRC and PAC ratios, measures). Nothing from the inputs, no log text: a
failed run's email gives its short status message only. Each attempt is in the audit log (`run email sent` / `run email failed`); a failure never
touches the run.
"""
from __future__ import annotations

import json
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path

from .config import Settings
from .db import Database

SETTING_KEY = "notify_runs"


def enabled(s: Settings) -> bool:
    return bool(s.smtp_host)


def wants(db: Database, user_id: int) -> bool:
    """The user's Account setting: on unless they turned it off."""
    r = db.one("SELECT value FROM user_settings WHERE user_id = ? AND key = ?", (user_id, SETTING_KEY))
    return r is None or r["value"] != "0"


def set_wants(db: Database, user_id: int, on: bool) -> None:
    db.run("INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)", (user_id, SETTING_KEY, "1" if on else "0"))


def _totals(s: Settings, row) -> dict:
    if not row["result_dir"]:
        return {}
    p = s.runs_dir / row["id"] / row["result_dir"] / "run_settings.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {**(d.get("totals") or {}), "_measures": (d.get("inputs") or {}).get("measures_priced")}


def _minutes(seconds) -> str:
    if seconds is None:
        return "unknown"
    m, sec = divmod(int(round(seconds)), 60)
    return f"{m} min {sec} s" if m else f"{sec} s"


def message(s: Settings, row, to: str) -> EmailMessage:
    """The email for a run row (with its owner's address `to`)."""
    name = row["label"] or row["id"]
    ok = row["status"] == "finished"
    what = "input check" if row["preflight_only"] else "run"
    msg = EmailMessage()
    msg["Subject"] = f"ReCET {what} {'finished' if ok else 'failed'}: {name}"
    msg["From"] = s.smtp_from or s.smtp_user
    msg["To"] = to
    link = f"{s.base_url.rstrip('/')}{s.prefix}/runs/{row['id']}"
    took = (row["finished_at"] - row["started_at"]) if row["finished_at"] and row["started_at"] else None
    lines = [f"Your ReCET {what} \"{name}\" {'finished' if ok else 'failed'} after {_minutes(took)} ({row['acc_version']} ACC).", ""]
    if ok:
        t = _totals(s, row)
        if t.get("_measures") is not None:
            lines.append(f"Measures priced: {int(t['_measures']):,}")
        if t.get("TotalSystemBenefit") is not None:
            lines.append(f"Total system benefit: ${t['TotalSystemBenefit']:,.0f}")
        for k, label in (("TRC_ratio", "TRC ratio"), ("PAC_ratio", "PAC ratio")):
            if t.get(k) is not None:
                lines.append(f"{label}: {t[k]:,.2f}")
    else:
        lines.append(f"What happened: {row['message'] or 'see the log on the run page'}")
    lines += ["", f"Results, files and log: {link}", "", "You get this email because email on run finish is on for your account; turn it off on the Account page."]
    msg.set_content("\n".join(lines))
    return msg


def send(s: Settings, msg: EmailMessage) -> None:
    """Hand the message to the SMTP server (STARTTLS by default; RECET_WEB_SMTP_SECURITY=ssl for port 465, none for a local relay)."""
    ctx = ssl.create_default_context()
    if s.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30, context=ctx)
    else:
        server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
    with server:
        if s.smtp_security == "starttls":
            server.starttls(context=ctx)
        if s.smtp_user:
            server.login(s.smtp_user, s.smtp_password)
        server.send_message(msg)


def run_ended(s: Settings, db: Database, run_id: str) -> bool:
    """Email the owner of a run that has just ended, when that applies (see the module docstring). True if an email was sent."""
    if not enabled(s):
        return False
    row = db.one("SELECT r.*, u.email AS owner_email, u.disabled AS owner_disabled FROM runs r JOIN users u ON u.id = r.user_id WHERE r.id = ?", (run_id,))
    if row is None or row["owner_disabled"] or row["status"] not in ("finished", "failed"):
        return False
    if row["preflight_only"] and row["status"] == "finished":
        return False
    if not wants(db, row["user_id"]):
        return False
    owner = {"id": row["user_id"], "email": row["owner_email"]}
    try:
        send(s, message(s, row, row["owner_email"]))
    except (OSError, smtplib.SMTPException) as exc:
        db.audit("run email failed", owner, detail=f"{run_id}: {exc}"[:500])
        return False
    db.audit("run email sent", owner, detail=f"{run_id} ({row['status']})")
    return True
