# NVDA screen-reader walk-through

Windows only. Guidepup drives NVDA and a Chrome window together and records what NVDA says after each key press; the test writes one transcript
per page to `results/<flow>.json` (the key, the phrases spoken, the focused element, the address). It checks nothing by itself: read the transcripts
against the WCAG 2.2 AA rules in CLAUDE.md and ARCHITECTURE 1.4 / 8.7.

The app it tests is this checkout's `recet`, served by `serve_a11y.py` on a throwaway state (synthetic ACC, one admin, one finished run, one
Edit inputs draft with a negative EUL). No reference data and no real account are used, and nothing is sent anywhere.

## Set up once

Node 20+, Chrome, and the ReCET web extra (`pip install -e ".[web,test]"`). Then, in this folder:

```powershell
npm ci
npm run setup-nvda
```

`setup-nvda` (`guidepup install nvda`) downloads a portable NVDA into `%LOCALAPPDATA%\guidepup`; it changes no Windows setting. Your own
NVDA stays as it is (quit it before a run).

Guidepup finds the Chrome window by pressing Alt+Esc until NVDA reports its title; the spec first brings the window to the front by its title
(`focusWindow.ps1`), because on a busy desktop Guidepup's 10 tries did not reach it.

## Run

Quit any NVDA that is already running, leave the machine alone while it runs (NVDA needs the browser in front), then in this folder:

```powershell
$env:RECET_PYTHON = "python"   # the Python that has ReCET's web extra, if not the one on PATH
npm test
```

About 5 minutes. Transcripts go to `results/`.
