# Backups of the ReCET web server

Phase 0 of the CEDARS platform build plan: while ReCET still runs on its own server, its state is copied to S3 every night, and this runbook says how to
get it back. Everything the web app keeps is in the Docker volume `recet_state` (`/var/lib/recet` in the container):

| What | Where | Backed up |
|---|---|---|
| Accounts, sessions, runs list, shares, saved views and templates, value-list changes, audit log | `web/recet_web.sqlite3` (WAL mode) | a consistent copy every night, kept 90 days (`db/recet_web-<UTC time>.sqlite3.gz`) |
| Each run's uploads and results | `web/runs/` | mirrored every night; a file removed by retention stays in the bucket's history 30 days |
| Edit inputs drafts, other state | `web/drafts/`, ... | mirrored with the runs |
| The ACC cache | `cache/` | no: rebuilt from the reference data (`recet-web warm-cache`) |
| The reference data | `~/recet-data` (read-only mount) | no: it is a copy of the data pack on the laptop (`recet data pack`) |
| Secrets (`~/recet/.env`: SMTP password) | the server | no: keep them in a password manager, never in S3 or git |

`recet-backup.sh` copies the database with SQLite's online backup API (safe while the app is running), checks the copy (`PRAGMA integrity_check`) and
records its SHA-256 on the object, then mirrors the state folder (`aws s3 sync --delete`, without the live database, parts folders of a running job or
temporary download zips) and writes `last_backup.json`. `recet-restore.sh` brings a copy back into a new folder. Both are tested against a fake `aws`
(`tests/test_backup_scripts.py`).

## One-time setup

These steps change AWS and the server, so Jake runs them. Pick a bucket name (it must be globally unique), e.g. `<you>-recet-backups`, and keep the
bucket in the server's region (`<region>` below).

**1. The bucket** (laptop, PowerShell, with an AWS CLI profile that can create buckets and roles):

    $B = "<bucket>"
    aws s3api create-bucket --bucket $B --region <region> --create-bucket-configuration LocationConstraint=<region>
    aws s3api put-public-access-block --bucket $B --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    aws s3api put-bucket-versioning --bucket $B --versioning-configuration Status=Enabled
    aws s3api put-bucket-encryption --bucket $B --server-side-encryption-configuration '{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"AES256\"}}]}'
    aws s3api put-bucket-lifecycle-configuration --bucket $B --lifecycle-configuration file://deploy/backup/lifecycle.json

**2. Permission for the server** (an instance role, so no key is stored on the box). Replace `BUCKET` in `iam-policy.json` with the bucket's name first.
If the instance already has a role, attach the policy to that role and skip the instance-profile lines.

    (Get-Content deploy/backup/iam-policy.json) -replace 'BUCKET', $B | Set-Content $env:TEMP\recet-backup-policy.json
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' | Set-Content $env:TEMP\trust.json
    aws iam create-role --role-name recet-server --assume-role-policy-document file://$env:TEMP\trust.json
    aws iam put-role-policy --role-name recet-server --policy-name recet-backup --policy-document file://$env:TEMP\recet-backup-policy.json
    aws iam create-instance-profile --instance-profile-name recet-server
    aws iam add-role-to-instance-profile --instance-profile-name recet-server --role-name recet-server
    aws ec2 associate-iam-instance-profile --region <region> --instance-id <the server's instance id> --iam-instance-profile Name=recet-server

**3. The timer on the server** (after a deploy has put `deploy/` in `~/recet`):

    ssh -i <your key file> <user>@<server address>
    sudo install -m 755 ~/recet/deploy/backup/recet-backup.sh ~/recet/deploy/backup/recet-restore.sh /usr/local/bin/
    sudo install -m 644 ~/recet/deploy/backup/recet-backup.service ~/recet/deploy/backup/recet-backup.timer /etc/systemd/system/
    echo 'RECET_BACKUP_BUCKET=s3://<bucket>/recet' | sudo tee /etc/recet-backup.env
    sudo systemctl daemon-reload && sudo systemctl enable --now recet-backup.timer
    sudo systemctl start recet-backup.service && sudo journalctl -u recet-backup -n 20 --no-pager

The last line runs the first backup at once; it should end with `recet-backup: done`. The timer then runs at 10:30 UTC (03:30 Pacific) every night
(`systemctl list-timers recet-backup.timer`). Later deploys that change the scripts need the `install` lines again.

## Checking it

    aws s3 cp s3://<bucket>/recet/last_backup.json -          # when the last backup finished
    sudo journalctl -u recet-backup --since yesterday --no-pager            # on the server

A failed night leaves `last_backup.json` at the night before. A dry run that prints the aws commands: `sudo RECET_BACKUP_DRYRUN=1 RECET_BACKUP_BUCKET=... recet-backup.sh`.

## Restore

**1. Get the copy** onto the server (or any machine with the AWS CLI and python3) into a new, empty folder. With no second argument it takes the
latest; name an older one from `aws s3 ls s3://<bucket>/recet/db/` to go back further. It checks the copy's SHA-256 and integrity and prints
the number of users and runs.

    sudo RECET_BACKUP_BUCKET=s3://<bucket>/recet recet-restore.sh /tmp/recet-restore
    sudo RECET_BACKUP_BUCKET=s3://<bucket>/recet recet-restore.sh /tmp/recet-restore db/recet_web-20261101T103012Z.sqlite3.gz

**2. Put it in place** (stops the app for a minute; keep the old state until the restored app has been checked):

    cd ~/recet && docker compose stop recet
    VOL=$(docker volume inspect recet_state --format '{{ .Mountpoint }}')
    sudo mv "$VOL/web" "$VOL/web.before-restore"
    sudo cp -a /tmp/recet-restore "$VOL/web" && sudo chown -R 10001:10001 "$VOL/web"
    docker compose start recet && docker compose exec -T recet recet --version

Sign in, open a run's results and its files, then `sudo rm -rf "$VOL/web.before-restore" /tmp/recet-restore`. To restore on a NEW server: deploy ReCET
there first (tar route, reference data in `~/recet-data`, `docker compose up -d --build` once so the volume exists), then steps 1 and 2.

**3. Practise it** once a quarter on a scratch folder (step 1 only), so a real restore is not the first one.

What a restore cannot bring back: a run that finished after the last backup (its owner reruns it), and the ACC cache (rebuilt with `recet-web
warm-cache --acc <year>`, or on the first run).
