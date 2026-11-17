"""ReCET in the browser: a small web app over the same run machinery as the window (`jobs.RunConfig`, `python -m recet run --config`).

* `config.Settings`   where the app keeps its state and how it behaves (environment variables, see that module)
* `db`                the SQLite store: users, sessions, setup links, runs, drafts, saved views, audit log
* `auth`              passwords (argon2), sessions, CSRF tokens and login rate limits
* `runner`            the run queue: worker thread(s) that run each queued job as `python -m recet run --config` in its own process
* `app`               the FastAPI pages and JSON endpoints, mounted under `Settings.prefix` (`/ReCET`)
* `edit_api`          the Edit inputs page's endpoints over `recet/editor.py`
* `cli`               `recet-web serve | create-admin | reset-link | warm-cache`

Install with `pip install "recet[web]"`. The tkinter window is not used here.
"""
