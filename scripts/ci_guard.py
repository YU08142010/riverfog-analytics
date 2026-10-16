"""CIの見張り: 秘密の値や本番のデータがリポジトリに入っていないかを確かめる。

- 入れてはいけないファイル: .env、users.db などの *.db、data/ logs/ backups/ の中身、秘密鍵
- 書いてはいけない値: GitHub・Google・Slack のトークン、秘密鍵の本文、.env 形式の SECRET_KEY=値
見つかったら一覧を出して終了コード1で止める。実行: python scripts/ci_guard.py
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_FILES = [
    (re.compile(r"(^|/)\.env$"), ".env(秘密の設定)"),
    (re.compile(r"\.(db|sqlite3?)$"), "データベース"),
    (re.compile(r"^(data|logs|backups)/"), "本番のデータ・ログ"),
    (re.compile(r"\.(pem|key|p12)$"), "鍵ファイル"),
]
SECRET_PATTERNS = [
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "GitHub のトークン"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"), "GitHub のトークン"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "Google の API キー"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "Slack のトークン"),
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), "Slack の Webhook"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"), "秘密鍵"),
    (re.compile(r"^(SECRET_KEY|GEMINI_API_KEYS?|NGINX_INTERNAL_HEADER_SECRET|RF_BACKUP_TOKEN)=\S+", re.M), ".env の値"),
]
TEXT_SUFFIXES = {".py", ".html", ".js", ".css", ".md", ".txt", ".sh", ".yml", ".yaml", ".toml", ".json", ".csv",
                 ".conf", ".cfg", ".ini", ".example", ""}


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def main():
    problems = []
    for rel in tracked_files():
        for pattern, what in FORBIDDEN_FILES:
            if pattern.search(rel):
                problems.append(f"{rel}: {what}はリポジトリに入れない")
        path = ROOT / rel
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern, what in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{line}: {what}らしき値")
    for p in problems:
        print(f"::error::{p}")
    print(f"ci_guard: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
