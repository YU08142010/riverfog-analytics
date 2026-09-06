"""管理者がダッシュボード上部に出す、お知らせメッセージ・アンケート機能のSQLiteベースの台帳。

kind:
  "message" - 単なるお知らせ文（回答なし）
  "choice"  - 選択式アンケート（choices_jsonに選択肢一覧）
  "text"    - 自由記述アンケート
"""
import json
import sqlite3
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    choices_json TEXT,
    start_at TEXT,
    end_at TEXT,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS announcement_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    user_id INTEGER,
    choice_index INTEGER,
    answer_text TEXT,
    submitted_at TEXT NOT NULL,
    UNIQUE(announcement_id, user_id)
);
"""


def _migrate_add_foreign_keys(conn):
    """2026-08-29より前に作られたDBにはannouncement_responsesにFK制約が無い。
    SQLiteはALTER TABLEでFK制約を後付けできないため、テーブルを作り直して移行する。"""
    fk_rows = conn.execute("PRAGMA foreign_key_list(announcement_responses)").fetchall()
    if fk_rows:
        return  # 既に移行済み
    with conn:
        conn.execute("ALTER TABLE announcement_responses RENAME TO announcement_responses_old_fkmig")
        conn.execute("""
            CREATE TABLE announcement_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
                visitor_id TEXT NOT NULL,
                choice_index INTEGER,
                answer_text TEXT,
                submitted_at TEXT NOT NULL,
                UNIQUE(announcement_id, visitor_id)
            )
        """)
        conn.execute(
            "INSERT INTO announcement_responses "
            "(id, announcement_id, visitor_id, choice_index, answer_text, submitted_at) "
            "SELECT id, announcement_id, visitor_id, choice_index, answer_text, submitted_at "
            "FROM announcement_responses_old_fkmig"
        )
        conn.execute("DROP TABLE announcement_responses_old_fkmig")


def _migrate_visitor_id_to_user_id(conn):
    """以前はCookie(visitor_id)だけで回答者を区別していたため、誰が答えたか分からなかった。
    サイト全体がログイン必須になって久しく、回答時には必ずセッションのuser_idが取れるため、
    以後はuser_idで回答者を識別・重複防止する。過去にvisitor_idだけで記録された回答の
    投稿者は遡って特定できない(Cookie値から実際のアカウントへの対応表が無い)ため、
    それらはuser_id=NULLのまま「投稿者不明」として残す。usersテーブルは別DBファイル
    (users.db)にあるためFK制約は付けられない(webapp.py側でuser_idから名前を引く)。"""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(announcement_responses)").fetchall()]
    if "user_id" in cols:
        return  # 既に移行済み
    with conn:
        conn.execute("ALTER TABLE announcement_responses RENAME TO announcement_responses_old_uidmig")
        conn.execute("""
            CREATE TABLE announcement_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
                user_id INTEGER,
                choice_index INTEGER,
                answer_text TEXT,
                submitted_at TEXT NOT NULL,
                UNIQUE(announcement_id, user_id)
            )
        """)
        conn.execute(
            "INSERT INTO announcement_responses "
            "(id, announcement_id, user_id, choice_index, answer_text, submitted_at) "
            "SELECT id, announcement_id, NULL, choice_index, answer_text, submitted_at "
            "FROM announcement_responses_old_uidmig"
        )
        conn.execute("DROP TABLE announcement_responses_old_uidmig")


def connect(db_path):
    # webapp.pyの複数ワーカースレッドから読み書きされるため、busy_timeoutで
    # 書き込み中の衝突を即座にエラーにせず待たせる(weather_store.pyと同じ対策)。
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(SCHEMA)
    _migrate_add_foreign_keys(conn)
    _migrate_visitor_id_to_user_id(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_announcement(conn, kind, message, choices, start_at, end_at, created_at):
    """choices: kind='choice'のときの選択肢リスト（それ以外はNone）。
    start_at/end_atはNoneなら無期限（開始はすぐ、終了は設定しない）。
    """
    choices_json = json.dumps(choices, ensure_ascii=False) if choices else None
    with conn:
        cur = conn.execute(
            """INSERT INTO announcements (kind, message, choices_json, start_at, end_at, created_at, active)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (kind, message, choices_json, start_at, end_at, created_at),
        )
        return cur.lastrowid


def _row_to_dict(row):
    (aid, kind, message, choices_json, start_at, end_at, created_at, active) = row
    return {
        "id": aid, "kind": kind, "message": message,
        "choices": json.loads(choices_json) if choices_json else None,
        "start_at": start_at, "end_at": end_at,
        "created_at": created_at, "active": bool(active),
    }


def list_announcements(conn):
    with closing(conn.execute(
        """SELECT id, kind, message, choices_json, start_at, end_at, created_at, active
           FROM announcements ORDER BY id DESC"""
    )) as cur:
        return [_row_to_dict(row) for row in cur.fetchall()]


def get_announcement(conn, announcement_id):
    row = conn.execute(
        """SELECT id, kind, message, choices_json, start_at, end_at, created_at, active
           FROM announcements WHERE id = ?""",
        (announcement_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def get_active_announcements(conn, now_str):
    """現在の日時(now_str, "YYYY-MM-DD HH:MM:SS")の時点で表示すべきお知らせ・アンケートを、
    新しい順に全て返す（active=1、かつ start_at<=now<=end_atの範囲、どちらもNULL可）。
    """
    rows = conn.execute(
        """SELECT id, kind, message, choices_json, start_at, end_at, created_at, active
           FROM announcements
           WHERE active = 1
             AND (start_at IS NULL OR start_at <= ?)
             AND (end_at IS NULL OR end_at >= ?)
           ORDER BY id DESC""",
        (now_str, now_str),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def set_active(conn, announcement_id, active):
    with conn:
        conn.execute("UPDATE announcements SET active = ? WHERE id = ?", (1 if active else 0, announcement_id))


def delete_announcement(conn, announcement_id):
    with conn:
        conn.execute("DELETE FROM announcement_responses WHERE announcement_id = ?", (announcement_id,))
        conn.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,))


def has_responded(conn, announcement_id, user_id):
    row = conn.execute(
        "SELECT 1 FROM announcement_responses WHERE announcement_id = ? AND user_id = ?",
        (announcement_id, user_id),
    ).fetchone()
    return row is not None


def record_response(conn, announcement_id, user_id, choice_index, answer_text, submitted_at):
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO announcement_responses
               (announcement_id, user_id, choice_index, answer_text, submitted_at)
               VALUES (?, ?, ?, ?, ?)""",
            (announcement_id, user_id, choice_index, answer_text, submitted_at),
        )


def list_responses(conn, announcement_id):
    """回答者(user_id)ごとの回答一覧を新しい順で返す(管理画面の結果ページ用)。
    名前解決はusers.dbが別ファイルのためここでは行わない(webapp.py側で行う)。"""
    rows = conn.execute(
        """SELECT user_id, choice_index, answer_text, submitted_at FROM announcement_responses
           WHERE announcement_id = ? ORDER BY id DESC""",
        (announcement_id,),
    ).fetchall()
    return [
        {"user_id": uid, "choice_index": ci, "answer_text": text, "submitted_at": s}
        for uid, ci, text, s in rows
    ]


def response_summary(conn, announcement_id):
    """choice型: 選択肢ごとの回答数のリスト。text型: 回答テキストの新しい順リスト。"""
    ann = get_announcement(conn, announcement_id)
    if ann is None:
        return None
    if ann["kind"] == "choice":
        rows = conn.execute(
            """SELECT choice_index, COUNT(*) FROM announcement_responses
               WHERE announcement_id = ? GROUP BY choice_index""",
            (announcement_id,),
        ).fetchall()
        counts_by_index = {idx: n for idx, n in rows}
        return [
            {"choice": choice, "count": counts_by_index.get(i, 0)}
            for i, choice in enumerate(ann["choices"] or [])
        ]
    if ann["kind"] == "text":
        rows = conn.execute(
            """SELECT answer_text, submitted_at FROM announcement_responses
               WHERE announcement_id = ? ORDER BY id DESC""",
            (announcement_id,),
        ).fetchall()
        return [{"text": t, "submitted_at": s} for t, s in rows]
    return None


def response_count(conn, announcement_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM announcement_responses WHERE announcement_id = ?",
        (announcement_id,),
    ).fetchone()
    return row[0] if row else 0
