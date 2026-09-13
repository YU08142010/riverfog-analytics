"""AIチャット機能(GeminiのAPIキーを使う)の会話・やり取りを記録するSQLiteベースの台帳。

Gemini/Claude等と同じく、質問は「会話(chat_conversations)」ごとにまとめて記録し、
利用者は過去の会話を一覧から選んで再開できる。1日あたりの質問回数の制限(利用者ごと・
サイト全体)は会話をまたいでchat_messagesの行数を数えるだけなので、会話機能を
追加した後もそのまま成立する。
"""
import sqlite3
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_conversations_user ON chat_conversations(user_id, updated_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER,
    user_id INTEGER NOT NULL,
    asked_at TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_user_asked ON chat_messages(user_id, asked_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_asked ON chat_messages(asked_at);
"""

# 会話機能を追加する前のDBには chat_messages.conversation_id 列が無いため、上のSCHEMAで
# 参照するとexecutescript自体が失敗する。列が確実に存在するようになった後(_migrate_legacy
# _messages内)で作るインデックスとして分離している。
CONVERSATION_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation ON chat_messages(conversation_id, id)"
)


def connect(db_path):
    # webapp.pyの複数ワーカースレッドがAIチャット送信のたびに書き込むため、
    # busy_timeoutで書き込み中の衝突を即座にエラーにせず待たせる
    # (weather_store.pyと同じ対策)。
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate_legacy_messages(conn)
    with conn:
        conn.execute(CONVERSATION_INDEX_SQL)
    return conn


def _migrate_legacy_messages(conn):
    """会話機能を追加する前のchat_messagesにはconversation_id列が無かった。
    列を追加し、まだ会話に紐付いていない既存メッセージはユーザーごとに
    1つの「以前の会話」へまとめて移行する(データを失わないため)。
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()]
    if "conversation_id" not in cols:
        with conn:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN conversation_id INTEGER")
    orphan_users = conn.execute(
        "SELECT DISTINCT user_id FROM chat_messages WHERE conversation_id IS NULL"
    ).fetchall()
    for (user_id,) in orphan_users:
        created_at, updated_at = conn.execute(
            "SELECT MIN(asked_at), MAX(asked_at) FROM chat_messages "
            "WHERE user_id = ? AND conversation_id IS NULL",
            (user_id,),
        ).fetchone()
        with conn:
            cur = conn.execute(
                "INSERT INTO chat_conversations (user_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, "以前の会話", created_at, updated_at),
            )
            conn.execute(
                "UPDATE chat_messages SET conversation_id = ? "
                "WHERE user_id = ? AND conversation_id IS NULL",
                (cur.lastrowid, user_id),
            )


def create_conversation(conn, user_id, created_at):
    with conn:
        cur = conn.execute(
            "INSERT INTO chat_conversations (user_id, title, created_at, updated_at) "
            "VALUES (?, NULL, ?, ?)",
            (user_id, created_at, created_at),
        )
    return cur.lastrowid


def get_conversation(conn, conversation_id, user_id):
    """指定IDの会話を返す。存在しない、または他人の会話ならNone
    (会話IDを書き換えて他人の会話を覗く/書き込むことを防ぐ)。"""
    row = conn.execute(
        "SELECT id, title, created_at, updated_at FROM chat_conversations "
        "WHERE id = ? AND user_id = ?",
        (conversation_id, user_id),
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "title": row[1], "created_at": row[2], "updated_at": row[3]}


def list_conversations(conn, user_id, limit=100):
    rows = conn.execute(
        """SELECT id, title, updated_at FROM chat_conversations
           WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    return [{"id": r[0], "title": r[1], "updated_at": r[2]} for r in rows]


def delete_conversation(conn, conversation_id, user_id):
    """指定の会話とその中の全メッセージを削除する。他人の会話は
    user_id条件に一致しないため削除されない(0件削除=Falseを返す)。"""
    with conn:
        conn.execute(
            "DELETE FROM chat_messages WHERE conversation_id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
        cur = conn.execute(
            "DELETE FROM chat_conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
    return cur.rowcount > 0


def delete_user_data(conn, user_id):
    """指定ユーザーの会話・メッセージを全て削除する。chat.dbはusers.dbとは別ファイルの
    SQLiteのため外部キーによるON DELETE CASCADEが効かず、以前はユーザー削除時に
    呼ばれることが無かった(admin_accounts_deleteはusers.delete_user()しか呼んでおらず、
    退会後もAIチャット履歴が孤立したまま残り続けていた、2026-09-13の監査で指摘)。
    admin_accounts_deleteからusers.delete_user()と一緒に呼ぶこと。"""
    with conn:
        conn.execute("DELETE FROM chat_messages WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM chat_conversations WHERE user_id = ?", (user_id,))


def set_conversation_title(conn, conversation_id, title):
    with conn:
        conn.execute("UPDATE chat_conversations SET title = ? WHERE id = ?", (title, conversation_id))


def touch_conversation(conn, conversation_id, updated_at):
    with conn:
        conn.execute(
            "UPDATE chat_conversations SET updated_at = ? WHERE id = ?",
            (updated_at, conversation_id),
        )


def get_conversation_messages(conn, conversation_id):
    with closing(conn.execute(
        "SELECT question, answer, error FROM chat_messages WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    )) as cur:
        rows = cur.fetchall()
    return [{"question": q, "answer": a, "error": e} for q, a, e in rows]


def record_message(conn, conversation_id, user_id, asked_at, question, answer=None, error=None):
    """挿入した行のidを返す(チャット利用ポイント付与の一意キーに使うため)。"""
    with conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (conversation_id, user_id, asked_at, question, answer, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, user_id, asked_at, question, answer, error),
        )
        return cur.lastrowid


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


def daily_message_counts(conn, days=14):
    """直近days日分の、日付ごとの質問数(全体)を返す(管理画面のGemini状況表示用)。"""
    with closing(conn.execute(
        """SELECT substr(asked_at, 1, 10) AS d, COUNT(*) FROM chat_messages
           GROUP BY d ORDER BY d DESC LIMIT ?""",
        (days,),
    )) as cur:
        rows = cur.fetchall()
    return [{"date": d, "count": c} for d, c in rows]
