"""依存パッケージの既知の脆弱性をpip-auditで定期チェックするスクリプト(週次cron想定)。

脆弱性が見つかった場合だけALERT_WEBHOOK_URL(.env)へ通知する。何も見つからなければ
ログに記録するだけで静かに終わる(通知が来ないこと自体を「正常」として運用する)。
"""
import json
import logging
import logging.handlers
import subprocess

from common import BASE_DIR, send_alert

LOG_FILE = BASE_DIR / "logs" / "security_audit.log"
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
log = logging.getLogger("security_audit")

PIP_AUDIT_BIN = str(BASE_DIR / ".venv" / "bin" / "pip-audit")


def main():
    try:
        result = subprocess.run(
            [PIP_AUDIT_BIN, "--format", "json"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=300,
        )
    except Exception as e:
        log.error("pip-auditの実行に失敗しました: %s", e)
        send_alert(f"pip-auditの実行に失敗しました: {e}", source="security-audit")
        return

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error(
            "pip-auditの出力を解析できませんでした(returncode=%s): %s",
            result.returncode, (result.stdout or result.stderr)[:1000],
        )
        return

    dependencies = data.get("dependencies", [])
    vulnerable = [d for d in dependencies if d.get("vulns")]
    if not vulnerable:
        log.info("既知の脆弱性は見つかりませんでした(%d件のパッケージを確認)", len(dependencies))
        return

    lines = []
    for dep in vulnerable:
        ids = ", ".join(v.get("id", "?") for v in dep["vulns"])
        lines.append(f"- {dep['name']} {dep['version']}: {ids}")
    message = f"依存パッケージに既知の脆弱性が{len(vulnerable)}件見つかりました:\n" + "\n".join(lines)
    log.warning(message)
    send_alert(message, source="security-audit")


if __name__ == "__main__":
    main()
