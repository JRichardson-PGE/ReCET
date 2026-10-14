"""`recet-web`: run and administer the web app.

    recet-web serve [--host 0.0.0.0] [--port 3000]       start the app (one process; runs execute in child processes, Settings.workers at a time)
    recet-web create-admin EMAIL [--name NAME]          add an admin (or make an existing user one) and print a link to choose a password
    recet-web reset-link EMAIL                          print a new one-time link for someone to choose a (new) password
    recet-web users                                     list the accounts
    recet-web warm-cache [--acc 2024 2026] [--all]      build the ACC caches before the first run (one ACC at a time, to keep memory low)

Settings come from RECET_WEB_* environment variables (recet/web/config.py). Links are printed with --base-url (default https://example.org) + the prefix.
"""
from __future__ import annotations

import argparse
import sys
import time

from .config import Settings

WARM_COLUMNS = ["total_avoided_cost", "td", "emission_rate_tonnes_per_kwh"]     # what a standard run reads; --all adds every component


def _link(s: Settings, base: str, token: str) -> str:
    return base.rstrip("/") + s.prefix + "/setup/" + token


def _db(s: Settings):
    from .db import Database
    return Database(s.db_path)


def cmd_serve(a, s: Settings) -> int:
    import uvicorn

    from .app import create_app
    uvicorn.run(create_app(s), host=a.host, port=a.port, log_level="info", proxy_headers=False, server_header=False, timeout_graceful_shutdown=10)
    return 0


def cmd_create_admin(a, s: Settings) -> int:
    from . import auth
    db = _db(s)
    u = auth.user_by_email(db, a.email)
    if u is None:
        uid = auth.create_user(db, a.email, a.name or "", "admin")
        db.audit("user added", None, detail=f"{a.email} as admin (command line)")
    else:
        uid = u["id"]
        db.run("UPDATE users SET role = 'admin', disabled = 0 WHERE id = ?", (uid,))
        db.audit("user changed", None, detail=f"{a.email} made admin (command line)")
    token = auth.new_setup_link(db, uid, s.setup_link_hours)
    print(f"{a.email} is an admin. Open this link within {s.setup_link_hours} hours to choose a password (it works once):")
    print(_link(s, a.base_url, token))
    return 0


def cmd_reset_link(a, s: Settings) -> int:
    from . import auth
    db = _db(s)
    u = auth.user_by_email(db, a.email)
    if u is None:
        print(f"no account for {a.email}", file=sys.stderr)
        return 1
    token = auth.new_setup_link(db, u["id"], s.setup_link_hours)
    db.audit("setup link made", None, detail=f"{a.email} (command line)")
    print(f"Link for {a.email} (works once, for {s.setup_link_hours} hours; the current password keeps working until it is used):")
    print(_link(s, a.base_url, token))
    return 0


def cmd_users(_a, s: Settings) -> int:
    db = _db(s)
    rows = db.all("SELECT * FROM users ORDER BY email")
    if not rows:
        print("no accounts yet: run `recet-web create-admin EMAIL`")
    for u in rows:
        state = "disabled" if u["disabled"] else ("no password yet" if not u["password_hash"] else "active")
        print(f"{u['email']:<40} {u['role']:<6} {state}")
    return 0


def cmd_warm_cache(a, s: Settings) -> int:
    import pyarrow.parquet as pq

    from .. import paths
    from ..pipeline import acc_store
    if paths.cache_dir() is None:
        print("the ACC cache is turned off (RECET_NO_CACHE): nothing to warm", file=sys.stderr)
        return 1
    failed = 0
    for acc in a.acc or list(s.acc_versions):
        p = paths.ref_path(f"acc_electric_{acc}") if f"acc_electric_{acc}" in paths.REF_FILES else None
        if p is None:
            print(f"ACC {acc}: electric file not found in the reference data folder ({paths.reference_dir()})", file=sys.stderr)
            failed += 1
            continue
        t0 = time.time()
        store = acc_store.AccStore(p)
        cols = WARM_COLUMNS
        if a.all:
            import pyarrow.types as pt
            schema = pq.ParquetFile(str(p)).schema_arrow
            cols = WARM_COLUMNS + [f.name for f in schema if f.name not in acc_store.KEY_COLUMNS and (pt.is_floating(f.type) or pt.is_integer(f.type))]
        for c in dict.fromkeys(cols):
            store.column(c)
            store._columns.pop(c, None)                  # drop the memory map; the .npy file stays
            print(f"ACC {acc}: {c} ready", flush=True)
        print(f"ACC {acc}: cache built in {time.time() - t0:.0f} s ({store.dir})", flush=True)
        del store
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="recet-web", description="ReCET web app")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="start the web app")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=3000)
    for name, help_ in (("create-admin", "add an admin and print a setup link"), ("reset-link", "print a new setup link for an account")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("email")
        p.add_argument("--base-url", default="https://example.org")
        if name == "create-admin":
            p.add_argument("--name", default="")
    sub.add_parser("users", help="list the accounts")
    p = sub.add_parser("warm-cache", help="build the ACC caches")
    p.add_argument("--acc", nargs="*", help="ACC versions (default: the ones the app offers)")
    p.add_argument("--all", action="store_true", help="every value column, not only the ones a standard run reads")
    a = ap.parse_args(argv)
    s = Settings.from_env()
    return {"serve": cmd_serve, "create-admin": cmd_create_admin, "reset-link": cmd_reset_link, "users": cmd_users,
            "warm-cache": cmd_warm_cache}[a.cmd](a, s)


if __name__ == "__main__":
    sys.exit(main())
