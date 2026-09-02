"""管理者による操作(監査ログ)の週次ダイジェストを送るスクリプト(週次cron想定)。

管理画面の「操作ログ」ページはいつでも見られるが、能動的にチェックする習慣が無いと
異常な操作(見覚えのないロール変更・大量削除等)に気づけない。直近1週間分の件数を
種類別に集計し、ALERT_WEBHOOK_URL(.env)へ送ることで、定期レビューのきっかけを作る。
1件も無い週は通知しない(通知が来ないこと自体を「平常運転」として扱えるようにする)。
"""
import logging
import logging.handlers
from collections import Counter
from datetime import timedelta

from common import BASE_DIR, now_jst_naive, send_alert
import users

USERS_DB_PATH = BASE_DIR / "data" / "users.db"

LOG_FILE = BASE_DIR / "logs" / "audit_digest.log"
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
log = logging.getLogger("audit_digest")


def main():
    since = (now_jst_naive() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    conn = users.connect(USERS_DB_PATH)
    try:
        entries = users.list_audit_log_since(conn, since)
    finally:
        conn.close()

    if not entries:
        log.info("直近1週間の管理操作はありませんでした。")
        return

    counts = Counter(e["action"] for e in entries)
    lines = [f"- {action}: {count}件" for action, count in counts.most_common()]
    message = (
        f"直近1週間の管理操作サマリ({len(entries)}件、管理画面の「操作ログ」で詳細を確認できます):\n"
        + "\n".join(lines)
    )
    log.info(message)
    send_alert(message, source="audit-digest")


if __name__ == "__main__":
    main()
