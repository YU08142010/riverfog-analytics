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
    announcement_id INTEGER NOT NULL,
    visitor_id TEXT NOT NULL,
    choice_index INTEGER,
    answer_text TEXT,
    submitted_at TEXT NOT NULL,
    UNIQUE(announcement_id, visitor_id)
);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
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


def has_responded(conn, announcement_id, visitor_id):
    row = conn.execute(
        "SELECT 1 FROM announcement_responses WHERE announcement_id = ? AND visitor_id = ?",
        (announcement_id, visitor_id),
    ).fetchone()
    return row is not None


def record_response(conn, announcement_id, visitor_id, choice_index, answer_text, submitted_at):
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO announcement_responses
               (announcement_id, visitor_id, choice_index, answer_text, submitted_at)
               VALUES (?, ?, ?, ?, ?)""",
            (announcement_id, visitor_id, choice_index, answer_text, submitted_at),
        )


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
