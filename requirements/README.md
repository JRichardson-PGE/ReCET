# Locked dependencies

Exact versions, with SHA-256 hashes, of everything ReCET installs, resolved from `pyproject.toml` for every platform (Linux amd64 / arm64, Windows,
macOS) and Python 3.11 and later:

| File | What | Used by |
|---|---|---|
| `engine.txt` | the engine, command line and window (`pip install .`) | installs without S3 |
| `worker.txt` | the engine plus boto3 (`.[s3]`) | the worker image (`Dockerfile.worker`) |
| `web.txt` | the engine plus the web app (`.[web]`) | the Dockerfile |
| `test.txt` | the engine, the web app and the tests (`.[web,test]`) | CI |

Install from one, then ReCET itself without dependencies:

    pip install --require-hashes -r requirements/test.txt
    pip install --no-deps -e .

A plain `pip install -e ".[test]"` still works and takes the newest versions `pyproject.toml` allows.

## Updating

After changing a dependency in `pyproject.toml`, or to take newer versions, regenerate all four (needs [uv](https://docs.astral.sh/uv/)) and commit them
with the change; CI (`.github/workflows/lock.yml`) fails when they no longer match `pyproject.toml`:

    python requirements/lock.py            # keep the pinned versions where pyproject.toml still allows them
    python requirements/lock.py --upgrade  # take the newest versions
