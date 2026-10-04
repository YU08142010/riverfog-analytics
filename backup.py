"""data/の再作成できないデータを毎日ローカルに自動バックアップするスクリプト(cronで毎日3:00)。

    0 3 * * * cd /opt/river-fog && .venv/bin/python backup.py >> logs/cron.log 2>&1

対象と対象外(2026-10-04に見直し):
  - 対象: SQLiteのDB(観測・予測・利用者・チャット等)、xlsx、モデル・実験結果、添付ファイル等。
    DBは書き込み中でも整合した状態で保存されるよう、SQLiteのバックアップAPIで一時的な
    スナップショットを作ってからZIPに入れる(ファイルをそのままコピーすると、書き込み途中の
    壊れた状態や、WALに残った最新の書き込みが抜けた状態を保存してしまうことがある)。
  - 対象外: data/images(カメラ画像。約2GBあり毎日増える。圧縮がほぼ効かないJPEGのため、
    毎日ZIPにすると14世代で数十GBになりディスクがあふれる)、data/predictions(予測グラフ・
    CSV。毎日作り直されるもの。予測そのものはpredictions.dbに残る)、SQLiteのWAL/SHM
    (スナップショットに含まれる)、.env(APIキー等のシークレット)。
    画像は、このサーバーの外への定期コピー(rsync等)で守ること。

これはディスク故障には効かない(同じディスク上に置くため)。誤操作・DBの破損・不具合による
データの書き換えから戻すためのもの。BACKUP_DIRの中身も定期的にサーバーの外へコピーすること。

管理画面の「バックアップをダウンロード」(common.build_data_backup_zip)は、画像も.envも
含む全データを手動で持ち出す別の機能で、ここでは使わない。
"""
import logging
import logging.handlers
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from common import BASE_DIR, JST, send_alert

DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = BASE_DIR / "backups"
# 世代管理の対象は、このスクリプトが作る日時付きのZIPだけ(手動で残した別名のZIPは消さない)。
BACKUP_GLOB = "river-fog-backup_[0-9]*.zip"
KEEP_GENERATIONS = 14
EXCLUDED_DIRS = {"images", "predictions"}
EXCLUDED_SUFFIXES = (".db-wal", ".db-shm", ".tmp", ".lock")
# 空き容量がこれ未満なら、ディスクをあふれさせないようバックアップを作らずに通知する。
MIN_FREE_BYTES = 2 * 1024 ** 3

LOG_FILE = BASE_DIR / "logs" / "backup.log"
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
log = logging.getLogger("backup")


def _is_target(path):
    rel = path.relative_to(DATA_DIR)
    if rel.parts and rel.parts[0] in EXCLUDED_DIRS:
        return False
    return not path.name.endswith(EXCLUDED_SUFFIXES)


def _is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _sqlite_snapshot(src, dest):
    """書き込み中のDBでも整合した状態をdestに保存する(SQLiteのオンラインバックアップ)。"""
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60) as source, sqlite3.connect(dest) as target:
        source.backup(target)
    with sqlite3.connect(dest) as check:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"integrity check failed for snapshot of {src.name}: {result}")


def build_backup(dest):
    """対象ファイルをZIPにまとめる。戻り値: (ファイル数, うちDB数)。"""
    n_files = n_db = 0
    with tempfile.TemporaryDirectory(prefix="rf_backup_") as tmp, \
            zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DATA_DIR.rglob("*")):
            if not path.is_file() or not _is_target(path):
                continue
            arcname = str(path.relative_to(DATA_DIR))
            if _is_sqlite(path):
                snapshot = Path(tmp) / f"snapshot_{n_db}.db"
                _sqlite_snapshot(path, snapshot)
                zf.write(snapshot, arcname=arcname)
                snapshot.unlink()
                n_db += 1
            else:
                zf.write(path, arcname=arcname)
            n_files += 1
    with zipfile.ZipFile(dest) as zf:
        bad = zf.testzip()
    if bad is not None:
        raise RuntimeError(f"zip verification failed at {bad}")
    return n_files, n_db


def prune_old_backups(keep=KEEP_GENERATIONS):
    backups = sorted(BACKUP_DIR.glob(BACKUP_GLOB), reverse=True)
    for old in backups[keep:]:
        try:
            old.unlink()
            log.info("Pruned old backup: %s", old.name)
        except Exception as e:
            log.warning("Failed to prune old backup %s: %s", old.name, e)


def main():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(BACKUP_DIR).free
    if free < MIN_FREE_BYTES:
        msg = f"空き容量が{free / 1024 ** 3:.1f}GBしかないため、データのバックアップを作成しませんでした"
        log.error(msg)
        send_alert(msg, source="backup.py")
        return
    stamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"river-fog-backup_{stamp}.zip"
    partial = dest.with_suffix(".zip.partial")
    log.info("=== Backup started ===")
    try:
        n_files, n_db = build_backup(partial)
        partial.replace(dest)
    finally:
        partial.unlink(missing_ok=True)
    log.info("Backup written: %s (%.1f MB, %d file(s), %d SQLite snapshot(s))",
             dest.name, dest.stat().st_size / 1024 ** 2, n_files, n_db)
    prune_old_backups()
    log.info("=== Backup finished ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("backup.py terminated abnormally")
        send_alert(f"backup.py が異常終了しました: {e}", source="backup.py")
        sys.exit(1)
