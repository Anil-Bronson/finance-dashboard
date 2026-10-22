#!/usr/bin/env bash
# Nightly backup (run from cron). 1) online backup inside the container,
# 2) copy to the storage pool so a dead NVMe doesn't take the history with it.
set -euo pipefail
cd "$(dirname "$0")/.."

DEST=/mnt/storage/backups/spend-tracker
KEEP_DAYS=90

echo "== $(date -Is)"
docker exec spend python /app/backup.py

# The pool is always a mountpoint (mergerfs), even when the disk enclosure is
# off, so check for real content before writing to it.
if mountpoint -q /mnt/storage && [ -d /mnt/storage/movies ]; then
  mkdir -p "$DEST"
  cp --preserve=timestamps --update data/backups/spend-*.db data/backups/categories.json "$DEST"/
  find "$DEST" -name 'spend-*.db' -mtime +"$KEEP_DAYS" -delete
  echo "copied to $DEST ($(ls "$DEST"/spend-*.db | wc -l) backups there)"
else
  echo "storage pool not mounted (enclosure off?); local backup only" >&2
  exit 1
fi
