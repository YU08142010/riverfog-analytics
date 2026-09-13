"""data/一式(.envは含まない)を定期的にローカルへ自動バックアップするスクリプト。
.envはAPIキー等のシークレットを含み、世代管理されたバックアップを外部同期する運用も
想定されるため、定期自動バックアップには含めない(手動ダウンロード版のみ.envを含む)。

以前は管理画面から手動でZIPをダウンロードするしか手段が無く、誰もダウンロードしなければ
バックアップが1件も存在しない状態だった。まずはその「自動バックアップが無い」問題を
解消するため、cron等で定期実行しBACKUP_DIR配下に世代管理して保存する。

オフサイト保管(このサーバー自体の故障に備えた、別ホスト/クラウドストレージへの転送)は
行わない。転送先・認証情報の選定は運用者が判断すべき事項のため、ここでは行わず、
BACKUP_DIRの中身を定期的に外部にコピーする運用を別途組むことを推奨する。

crontabの設定例(毎日3:00に実行、他のバッチと時間帯が被らないようにする):
    0 3 * * * cd /opt/river-fog && .venv/bin/python backup.py >> logs/backup.log 2>&1
"""
import logging
import logging.handlers
import sys
from datetime import datetime

from common import BASE_DIR, JST, build_data_backup_zip

BACKUP_DIR = BASE_DIR / "backups"
# 毎日1回の実行を想定し、直近2週間分を残す(それ以上は手動ダウンロードで別途保管する運用)。
KEEP_GENERATIONS = 14

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


def prune_old_backups(keep=KEEP_GENERATIONS):
    backups = sorted(BACKUP_DIR.glob("river-fog-backup_*.zip"), reverse=True)
    for old in backups[keep:]:
        try:
            old.unlink()
            log.info("Pruned old backup: %s", old.name)
        except Exception as e:
            log.warning("Failed to prune old backup %s: %s", old.name, e)


def main():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"river-fog-backup_{stamp}.zip"
    log.info("=== Backup started ===")
    build_data_backup_zip(dest, include_env=False)
    size_mb = dest.stat().st_size / (1024 * 1024)
    log.info("Backup written: %s (%.1f MB)", dest.name, size_mb)
    prune_old_backups()
    log.info("=== Backup finished ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("backup.py terminated abnormally")
        sys.exit(1)
