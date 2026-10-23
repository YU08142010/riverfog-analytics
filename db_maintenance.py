"""SQLite各DBの定期VACUUM/ANALYZE(cronで月1回、他の夜間ジョブと被らない時間に実行)。

    30 3 1 * * cd /opt/river-fog && .venv/bin/python db_maintenance.py >> logs/cron.log 2>&1

毎時〜毎日書き込みが続くDB(predictions.db, weather.db等)は、削除・更新で空いた領域が
断片化したまま残りクエリ計画(ANALYZE統計)も古びていく。VACUUMで断片化を解消し、
ANALYZEで統計を更新する。対象はdata/直下の*.dbすべて(backup.pyと違い、画像を含まない
DBファイルだけなので除外リストは不要)。VACUUMは対象DBへの排他ロックを短時間取るため、
他プロセスが書き込み中だと待たされる・待たせることがある — 深夜の空いている時間帯に実行する。
"""
import logging
import logging.handlers
import sqlite3
import time
from pathlib import Path

from common import BASE_DIR, send_alert

DATA_DIR = BASE_DIR / "data"
LOG_FILE = BASE_DIR / "logs" / "db_maintenance.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("db_maintenance")


def _vacuum_analyze(path):
    before = path.stat().st_size
    started = time.monotonic()
    conn = sqlite3.connect(str(path), timeout=120)
    try:
        conn.execute("PRAGMA busy_timeout = 120000")
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
    finally:
        conn.close()
    after = path.stat().st_size
    log.info(
        "%s: %.1fMB -> %.1fMB (%.1fs)",
        path.name, before / 1024 ** 2, after / 1024 ** 2, time.monotonic() - started,
    )


def main():
    failures = []
    for path in sorted(DATA_DIR.glob("*.db")):
        try:
            _vacuum_analyze(path)
        except Exception as e:
            log.exception("VACUUM/ANALYZE failed for %s", path.name)
            failures.append(f"{path.name}: {e}")
    if failures:
        send_alert(
            "db_maintenance.pyでVACUUM/ANALYZEに失敗したDBがあります: " + ", ".join(failures),
            source="db_maintenance.py",
        )


if __name__ == "__main__":
    main()
