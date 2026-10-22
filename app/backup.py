"""Nightly database backup. Runs inside the container:

    docker exec spend python /app/backup.py

Takes an online SQLite backup (safe while the API is running, WAL included),
verifies it with integrity_check, saves it as BACKUP_DIR/spend-YYYY-MM-DD.db and
prunes local copies older than KEEP_DAYS. scripts/backup.sh then copies them
off the NVMe to the storage pool.
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("DB_PATH", "/data/spend.db")
RULES_PATH = os.environ.get("RULES_PATH", "/data/categories.json")
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/data/backups"))
KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "30"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Vancouver"))  # name files by local date, not UTC


def main() -> int:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(TZ).date().isoformat()
    tmp = BACKUP_DIR / f".spend-{today}.db.tmp"
    out = BACKUP_DIR / f"spend-{today}.db"

    src = sqlite3.connect(DB_PATH, timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")  # self-contained single file
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        n = dst.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        print(f"backup FAILED integrity_check: {ok}", file=sys.stderr)
        return 1
    tmp.replace(out)
    os.chmod(out, 0o644)
    if os.path.exists(RULES_PATH):
        shutil.copy2(RULES_PATH, BACKUP_DIR / "categories.json")

    cutoff = (datetime.now(TZ).date() - timedelta(days=KEEP_DAYS)).isoformat()
    pruned = 0
    for f in BACKUP_DIR.glob("spend-*.db"):
        if f.stem.removeprefix("spend-") < cutoff:
            f.unlink()
            pruned += 1
    print(f"backup ok: {out.name} ({n} transactions, {out.stat().st_size // 1024} KiB), pruned {pruned}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
