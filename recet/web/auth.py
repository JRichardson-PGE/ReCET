"""Accounts: argon2 password hashes, server-side sessions, one-time setup links, CSRF tokens and login rate limits.

A session cookie holds a random token; the database keeps only its SHA-256, so a copy of the database cannot be replayed as a cookie. Disabling a user or resetting
their password deletes their sessions. Setup links work the same way (hashed, one use, `Settings.setup_link_hours` to live).
"""
from __future__ import annotations

import hashlib
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .db import Database

_ph = PasswordHasher()
MIN_PASSWORD = 10
# Login rate limits: failures in the last LOCK_WINDOW seconds
LOCK_WINDOW = 15 * 60
MAX_FAILS_PER_ACCOUNT = 5
MAX_FAILS_PER_IP = 20
_DUMMY_HASH = _ph.hash("not a real password, used to keep failed logins slow")


def _h(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def password_problem(pw: str, again: str | None = None) -> str | None:
    if again is not None and pw != again:
        return "The two passwords do not match."
    if len(pw) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(pw) > 256:
        return "That password is too long."
    return None


# ----------------------------------------------------------------------------------------------------------------------------- users
def create_user(db: Database, email: str, name: str = "", role: str = "user") -> int:
    email = email.strip()
    if "@" not in email or len(email) > 254:
        raise ValueError("enter a valid email address")
    if role not in ("admin", "user"):
        raise ValueError("role must be admin or user")
    if db.one("SELECT 1 FROM users WHERE email = ?", (email,)):
        raise ValueError(f"{email} already has an account")
    return db.run("INSERT INTO users (email, name, role, created_at) VALUES (?,?,?,?)", (email, name.strip(), role, time.time()))


def get_user(db: Database, user_id: int):
    return db.one("SELECT * FROM users WHERE id = ?", (user_id,))


def user_by_email(db: Database, email: str):
    return db.one("SELECT * FROM users WHERE email = ?", (email.strip(),))


def set_password(db: Database, user_id: int, pw: str) -> None:
    db.run("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(pw), user_id))
    end_sessions(db, user_id)


def end_sessions(db: Database, user_id: int) -> None:
    db.run("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# ----------------------------------------------------------------------------------------------------------------------------- setup links
def new_setup_link(db: Database, user_id: int, hours: int) -> str:
    """A one-time token for the person to choose a password. Any older unused link of theirs stops working."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.run("DELETE FROM setup_links WHERE user_id = ?", (user_id,))
    db.run("INSERT INTO setup_links (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)", (_h(token), user_id, now, now + hours * 3600))
    return token


def setup_link_user(db: Database, token: str):
    """The user a setup link belongs to, or None if it is unknown, used or expired."""
    row = db.one("SELECT * FROM setup_links WHERE token_hash = ?", (_h(token),))
    if row is None or row["used_at"] or row["expires_at"] < time.time():
        return None
    u = get_user(db, row["user_id"])
    return None if u is None or u["disabled"] else u


def use_setup_link(db: Database, token: str, pw: str):
    u = setup_link_user(db, token)
    if u is None:
        return None
    set_password(db, u["id"], pw)
    db.run("UPDATE setup_links SET used_at = ? WHERE token_hash = ?", (time.time(), _h(token)))
    return u


# ----------------------------------------------------------------------------------------------------------------------------- login
def login_blocked(db: Database, email: str, ip: str) -> bool:
    since = time.time() - LOCK_WINDOW
    acct = db.one("SELECT COUNT(*) n FROM login_failures WHERE email = ? COLLATE NOCASE AND at > ?", (email.strip(), since))["n"]
    by_ip = db.one("SELECT COUNT(*) n FROM login_failures WHERE ip = ? AND at > ?", (ip, since))["n"]
    return acct >= MAX_FAILS_PER_ACCOUNT or by_ip >= MAX_FAILS_PER_IP


def check_login(db: Database, email: str, pw: str, ip: str):
    """The user if the email and password are right and the account is active; else None (and the failure is counted)."""
    u = user_by_email(db, email)
    ok = False
    try:
        if u is not None and u["password_hash"] and not u["disabled"]:
            ok = _ph.verify(u["password_hash"], pw)
        else:
            _ph.verify(_DUMMY_HASH, pw + "x")          # same cost as a real check, so timing does not reveal which emails exist
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        ok = False
    if not ok:
        db.run("INSERT INTO login_failures (at, ip, email) VALUES (?,?,?)", (time.time(), ip, email.strip()[:254]))
        return None
    if _ph.check_needs_rehash(u["password_hash"]):
        db.run("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(pw), u["id"]))
    db.run("DELETE FROM login_failures WHERE email = ? COLLATE NOCASE", (u["email"],))
    db.run("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), u["id"]))
    return u


def prune(db: Database) -> None:
    now = time.time()
    db.run("DELETE FROM login_failures WHERE at < ?", (now - 24 * 3600,))
    db.run("DELETE FROM sessions WHERE expires_at < ?", (now,))
    db.run("DELETE FROM setup_links WHERE expires_at < ?", (now - 7 * 24 * 3600,))


# ----------------------------------------------------------------------------------------------------------------------------- sessions
def new_session(db: Database, user_id: int, days: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.run("INSERT INTO sessions (token_hash, user_id, csrf, created_at, expires_at) VALUES (?,?,?,?,?)",
           (_h(token), user_id, secrets.token_urlsafe(24), now, now + days * 86400))
    return token


def session_of(db: Database, token: str | None):
    """(user, session) for a cookie token, or (None, None). Disabled users and expired sessions get nothing."""
    if not token:
        return None, None
    s = db.one("SELECT * FROM sessions WHERE token_hash = ?", (_h(token),))
    if s is None or s["expires_at"] < time.time():
        return None, None
    u = get_user(db, s["user_id"])
    if u is None or u["disabled"] or not u["password_hash"]:
        return None, None
    return u, s


def end_session(db: Database, token: str | None) -> None:
    if token:
        db.run("DELETE FROM sessions WHERE token_hash = ?", (_h(token),))


def csrf_ok(expected: str | None, given: str | None) -> bool:
    return bool(expected) and bool(given) and secrets.compare_digest(str(expected), str(given))
