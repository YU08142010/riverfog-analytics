"""AIチャット機能(GeminiのAPIキーを使う)のやり取りを記録するSQLiteベースの台帳。

無料枠のクォータを守るため、1日あたりの質問回数を「利用者ごと」「サイト全体」の
両方で数えられるようにするのが目的で、会話内容そのものは主にレート制限の計算用の
副産物として保存している(将来的に「よくある質問」の分析等にも使えるが、現時点では
使っていない)。
"""
import sqlite3
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    asked_at TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_user_asked ON chat_messages(user_id, asked_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_asked ON chat_messages(asked_at);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def record_message(conn, user_id, asked_at, question, answer=None, error=None):
    with conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id, asked_at, question, answer, error) VALUES (?, ?, ?, ?, ?)",
            (user_id, asked_at, question, answer, error),
        )


def count_user_messages_since(conn, user_id, since_str):
    """指定日時以降に、そのユーザーが送った質問数を数える(失敗した質問も含める。
    レート制限はAPI呼び出しの試行回数を抑えるためのものなので、失敗しても消費したとみなす)。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE user_id = ? AND asked_at >= ?",
        (user_id, since_str),
    ).fetchone()
    return row[0] if row else 0


def count_all_messages_since(conn, since_str):
    row = conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE asked_at >= ?",
        (since_str,),
    ).fetchone()
    return row[0] if row else 0


def get_recent_messages(conn, user_id, limit=30):
    """そのユーザーの直近のやり取りを古い順で返す(チャットページ再訪問時の履歴表示用)。"""
    with closing(conn.execute(
        """SELECT question, answer, error FROM chat_messages
           WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
        (user_id, limit),
    )) as cur:
        rows = cur.fetchall()
    rows.reverse()
    return [{"question": q, "answer": a, "error": e} for q, a, e in rows]


def daily_message_counts(conn, days=14):
    """直近days日分の、日付ごとの質問数(全体)を返す(管理画面のGemini状況表示用)。"""
    with closing(conn.execute(
        """SELECT substr(asked_at, 1, 10) AS d, COUNT(*) FROM chat_messages
           GROUP BY d ORDER BY d DESC LIMIT ?""",
        (days,),
    )) as cur:
        rows = cur.fetchall()
    return [{"date": d, "count": c} for d, c in rows]
