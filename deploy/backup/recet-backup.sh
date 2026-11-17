#!/usr/bin/env bash
# Nightly backup of the ReCET web app's state on the server to S3 (deploy/backup/README.md): a consistent copy of the SQLite database (accounts, runs,
# shares, saved views, audit log), dated and kept per the bucket's lifecycle, and a mirror of the run, draft and upload folders (the bucket's versioning
# keeps what a later night deletes). The ACC cache is left out: it is rebuilt from the reference data (recet-web warm-cache).
#
# Run as root (it reads the Docker volume's folder); credentials come from the instance's IAM role, never from this file.
#   RECET_BACKUP_BUCKET   s3://bucket/prefix   (required)
#   RECET_STATE_DIR       the volume's web state folder (default: <recet_state volume mountpoint>/web)
#   RECET_BACKUP_DRYRUN   1 = print the aws commands instead of running them
set -euo pipefail

BUCKET="${RECET_BACKUP_BUCKET:?set RECET_BACKUP_BUCKET to s3://bucket/prefix}"
BUCKET="${BUCKET%/}"
if [[ -z "${RECET_STATE_DIR:-}" ]]; then
  RECET_STATE_DIR="$(docker volume inspect recet_state --format '{{ .Mountpoint }}')/web"
fi
STATE="${RECET_STATE_DIR%/}"
DB="$STATE/recet_web.sqlite3"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

aws_() {
  if [[ "${RECET_BACKUP_DRYRUN:-0}" == "1" ]]; then echo "aws $*"; else aws "$@"; fi
}
log() { echo "$(date -u +%FT%TZ) recet-backup: $*"; }

[[ -f "$DB" ]] || { log "no database at $DB"; exit 1; }

# 1. the database: SQLite's online backup API gives a consistent copy while the app runs (WAL mode), then an integrity check of the copy
python3 - "$DB" "$WORK/recet_web.sqlite3" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=60)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for (t,) in dst.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users', 'runs')")}
dst.close(); src.close()
if ok != "ok":
    sys.exit(f"integrity check of the copy failed: {ok}")
print(" ".join(f"{t}={n}" for t, n in sorted(counts.items())))
PY
gzip -9 "$WORK/recet_web.sqlite3"
SHA="$(sha256sum "$WORK/recet_web.sqlite3.gz" | cut -d' ' -f1)"
aws_ s3 cp --only-show-errors "$WORK/recet_web.sqlite3.gz" "$BUCKET/db/recet_web-$STAMP.sqlite3.gz" --metadata "sha256=$SHA"
log "database copied to $BUCKET/db/recet_web-$STAMP.sqlite3.gz (sha256 $SHA)"

# 2. the run, draft and upload folders: a mirror (a run deleted by retention disappears from the mirror; versioning keeps it for the lifecycle's days).
#    Left out: the live database files (copied above), parts folders of a run still running, temporary download zips.
aws_ s3 sync --only-show-errors --delete "$STATE/" "$BUCKET/state/" \
  --exclude "recet_web.sqlite3*" --exclude "*/_parts/*" --exclude "tmp/*" --exclude "*.tmp"
log "state folder mirrored to $BUCKET/state/"

# 3. a marker the monitoring (or a person) can read: when the last backup finished and what it holds
printf '{"finished": "%s", "database": "db/recet_web-%s.sqlite3.gz", "sha256": "%s"}\n' "$(date -u +%FT%TZ)" "$STAMP" "$SHA" > "$WORK/last_backup.json"
aws_ s3 cp --only-show-errors "$WORK/last_backup.json" "$BUCKET/last_backup.json"
log "done"
