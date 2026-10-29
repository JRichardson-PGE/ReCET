"""Serve the web app for the NVDA screen-reader test (tests/screenreader): a throwaway state with one admin, one finished run and one Edit inputs draft.

Run from the repo root: python tests/screenreader/serve_a11y.py. It serves http://127.0.0.1:8765/ReCET until stopped and writes what the test needs
(addresses, the test sign-in) to tests/screenreader/.server.json. Synthetic ACC and flat profile from tests/conftest, so no reference data is needed.
The sign-in is the test suite's own (tests/test_web.py) and exists only in this throwaway state."""
from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))   # this checkout's recet, not whichever one is installed

import recet  # noqa: F401  (before numpy: sets OPENBLAS_NUM_THREADS)
import uvicorn
from fastapi.testclient import TestClient

from conftest import make_electric_acc, make_flat_profile_csv, make_measure_csv
from recet.web import auth
from recet.web.app import create_app
from recet.web.config import Settings

EMAIL, PW, P, PORT = "admin@example.org", "correct horse battery", "/ReCET", 8765


def _zip(name: str, data: bytes) -> tuple[str, bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return "job.zip", buf.getvalue(), "application/zip"


def _measures(tmp: Path) -> bytes:
    """Three measures; T3 has a negative EUL, so the Edit inputs grid shows a cell that stops the run."""
    lines = []
    for i, extra in enumerate(({}, {"UnitkWh1stBaseline": "2500"}, {"EUL_Yrs": "-1"}), 1):
        text = make_measure_csv(tmp / f"m{i}.csv", CEInputID=f"T{i}", **extra).read_text(encoding="utf-8").splitlines()
        lines += text if i == 1 else text[1:]
    return ("\n".join(lines) + "\n").encode("utf-8")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="recet_a11y_"))
    s = Settings(state_dir=tmp / "web", start_workers=False, secure_cookies=False, trust_proxy=False,
                 run_defaults={"acc_electric_file": str(make_electric_acc(tmp / "acc.parquet")),
                               "profiles_8760_file": str(make_flat_profile_csv(tmp / "p.csv"))})
    app = create_app(s)
    ctx = app.state.ctx
    uid = auth.create_user(ctx.db, EMAIL, "Test Admin", "admin")
    auth.set_password(ctx.db, uid, PW)

    c = TestClient(app, base_url="http://testserver", follow_redirects=False)
    tok = re.search(r'name="login_csrf" value="([^"]+)"', c.get(f"{P}/login").text).group(1)
    c.post(f"{P}/login", data={"email": EMAIL, "password": PW, "login_csrf": tok, "next": ""})
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get(f"{P}/runs/new").text).group(1)
    good = make_measure_csv(tmp / "Measure.csv").read_bytes()
    r = c.post(f"{P}/runs", data={"csrf": csrf, "label": "Screen-reader test run", "acc_version": "2026", "input_mode": "files",
                                  "market_effects": "0", "output_format": "csv"}, files={"job_zip": _zip("Measure.csv", good)})
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    ctx.runner.work_once()
    r = c.post(f"{P}/edit", data={"csrf": csrf, "mode": "upload", "name": "Screen-reader test draft"},
               files={"job_zip": _zip("Measure.csv", _measures(tmp))})
    draft = r.headers.get("location", "")
    assert "draft=" in draft, f"draft not created: {r.status_code} {r.text[:300]}"

    base = f"http://127.0.0.1:{PORT}{P}"
    info = {"base": base, "email": EMAIL, "password": PW, "run": f"{base}/runs/{run_id}", "results": f"{base}/runs/{run_id}/results",
            "edit": base + draft.removeprefix(P), "state": str(tmp)}
    (HERE / ".server.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    print(json.dumps(info, indent=1), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
