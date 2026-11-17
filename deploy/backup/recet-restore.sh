#!/usr/bin/env bash
# Restore the ReCET web app's state from the S3 backup (deploy/backup/README.md, "Restore") into a folder: the database copy (the latest, or the one
# named) and the state mirror. It never touches the running app: you stop the container and copy the folder into the volume yourself.
#
#   recet-restore.sh <dest folder> [db/recet_web-YYYYMMDDTHHMMSSZ.sqlite3.gz]
#   RECET_BACKUP_BUCKET   s3://bucket/prefix   (required)
set -euo pipefail

BUCKET="${RECET_BACKUP_BUCKET:?set RECET_BACKUP_BUCKET to s3://bucket/prefix}"
BUCKET="${BUCKET%/}"
DEST="${1:?usage: recet-restore.sh <dest folder> [db/<copy>.sqlite3.gz]}"
KEY="${2:-}"
log() { echo "$(date -u +%FT%TZ) recet-restore: $*"; }

[[ ! -e "$DEST" || -z "$(ls -A "$DEST" 2>/dev/null)" ]] || { log "$DEST is not empty; restore into a new folder"; exit 1; }
mkdir -p "$DEST"
if [[ -z "$KEY" ]]; then
  KEY="$(aws s3 cp "$BUCKET/last_backup.json" - | python3 -c 'import json, sys; print(json.load(sys.stdin)["database"])')"
fi
WANT="$(aws s3api head-object --bucket "$(echo "$BUCKET" | cut -d/ -f3)" --key "$(echo "$BUCKET/$KEY" | cut -d/ -f4-)" --query 'Metadata.sha256' --output text 2>/dev/null || true)"

aws s3 sync --only-show-errors "$BUCKET/state/" "$DEST/"
aws s3 cp --only-show-errors "$BUCKET/$KEY" "$DEST/recet_web.sqlite3.gz"
if [[ -n "$WANT" && "$WANT" != "None" ]]; then
  GOT="$(sha256sum "$DEST/recet_web.sqlite3.gz" | cut -d' ' -f1)"
  [[ "$GOT" == "$WANT" ]] || { log "the database copy's SHA-256 $GOT is not the recorded $WANT"; exit 1; }
fi
gunzip "$DEST/recet_web.sqlite3.gz"
python3 - "$DEST/recet_web.sqlite3" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
ok = c.execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok":
    sys.exit(f"integrity check failed: {ok}")
names = [t for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users', 'runs')")]
print(" ".join(f"{t}={c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}" for t in sorted(names)))
PY
log "restored $KEY and the state mirror into $DEST"
