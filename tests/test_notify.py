"""Run emails (recet/web/notify.py, BRD BR-06.2): one email to the owner when a run finishes or fails, none for a passing input check or with the
Account setting off, nothing at all without an SMTP server; and what is handed to the SMTP server."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from recet.web import notify
from recet.web.app import create_app
from recet.web.config import Settings
from conftest import make_electric_acc, make_flat_profile_csv
from test_web import P, _csrf, _job, _login, _start_run, _user


@pytest.fixture()
def mailweb(tmp_path, monkeypatch):
    s = Settings(state_dir=tmp_path / "web", start_workers=False, secure_cookies=False, trust_proxy=False, smtp_host="smtp.example.com",
                 smtp_from="recet@example.com", base_url="https://example.com",
                 run_defaults={"acc_electric_file": str(make_electric_acc(tmp_path / "acc.parquet")),
                               "profiles_8760_file": str(make_flat_profile_csv(tmp_path / "p.csv"))})
    app = create_app(s)
    sent = []
    monkeypatch.setattr(notify, "send", lambda settings, msg: sent.append(msg))
    return app, app.state.ctx, sent


def _work(ctx):
    assert ctx.runner.work_once()
    for t in ctx.runner.mail_threads:
        t.join(10)


def test_a_finished_and_a_failed_run_are_emailed(mailweb, tmp_path):
    app, ctx, sent = mailweb
    _user(ctx, "m@example.com")
    c = _login(app, "m@example.com")
    run_id = _start_run(c, tmp_path, label="my job")
    _work(ctx)
    assert len(sent) == 1
    msg = sent[0]
    body = msg.get_content()
    assert msg["To"] == "m@example.com" and msg["From"] == "recet@example.com" and msg["Subject"].startswith("ReCET run finished: ")
    assert f"https://example.com{P}/runs/{run_id}" in body and "Total system benefit: $" in body and "Measures priced: 1" in body       # no costs: no ratios
    r = c.post(f"{P}/runs", data={"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files"}, files={"job_zip": _job(b"not|a\nreal|file\n")})
    _work(ctx)
    assert len(sent) == 2 and "failed" in sent[1]["Subject"] and "What happened:" in sent[1].get_content()
    assert ctx.db.one("SELECT COUNT(*) n FROM audit WHERE event = 'run email sent'")["n"] == 2


def test_no_email_for_a_passing_input_check_or_when_turned_off(mailweb, tmp_path):
    app, ctx, sent = mailweb
    _user(ctx, "q@example.com")
    c = _login(app, "q@example.com")
    _start_run(c, tmp_path, preflight_only="on")
    _work(ctx)
    assert sent == []
    page = c.get(f"{P}/account").text
    assert "Email me when a run finishes or fails" in page and "checked" in page.split("notify_runs")[1][:40]
    r = c.post(f"{P}/account/notifications", data={"csrf": _csrf(c, f"{P}/account")})
    assert r.status_code == 303
    assert "checked" not in c.get(f"{P}/account").text.split('name="notify_runs"')[1][:40]
    _start_run(c, tmp_path)
    _work(ctx)
    assert sent == []
    c.post(f"{P}/account/notifications", data={"csrf": _csrf(c, f"{P}/account"), "notify_runs": "1"})
    _start_run(c, tmp_path)
    _work(ctx)
    assert len(sent) == 1


def test_nothing_is_sent_without_an_smtp_server(tmp_path):
    s = Settings(state_dir=tmp_path / "web", start_workers=False)
    assert not notify.enabled(s)


def test_send_uses_starttls_and_signs_in(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            calls.append(("quit",))

        def starttls(self, context=None):
            calls.append(("starttls",))

        def login(self, user, pw):
            calls.append(("login", user, pw))

        def send_message(self, msg):
            calls.append(("send", msg["To"]))
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    s = Settings(smtp_host="mail.example.com", smtp_user="u@example.com", smtp_password="pw")
    from email.message import EmailMessage
    m = EmailMessage()
    m["To"] = "x@example.com"
    notify.send(s, m)
    assert calls == [("connect", "mail.example.com", 587), ("starttls",), ("login", "u@example.com", "pw"), ("send", "x@example.com"), ("quit",)]
