"""アカウント(ユーザー)・新規登録申請のSQLiteベースの台帳。

ログインはメールアドレス+パスワードで行う。本人が直接アカウントを作れるわけではなく、
まず申請(signup_requests)として保存され、管理者が承認して初めてusersに実体ができる。
パスワードは常にハッシュ化して保存し、平文は一切保持しない。
"""
import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'general',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signup_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS login_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    logged_in_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_events_user_id ON login_events(user_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER,
    actor_name TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def _norm_email(email):
    return email.strip().lower()


def _user_row_to_dict(row):
    return {
        "id": row[0], "name": row[1], "email": row[2],
        "password_hash": row[3], "role": row[4], "created_at": row[5],
    }


def create_user(conn, name, email, password_hash, role, created_at):
    with conn:
        conn.execute(
            "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, _norm_email(email), password_hash, role, created_at),
        )


def get_user_by_email(conn, email):
    row = conn.execute(
        "SELECT id, name, email, password_hash, role, created_at FROM users WHERE email = ?",
        (_norm_email(email),),
    ).fetchone()
    return _user_row_to_dict(row) if row else None


def get_user_by_id(conn, user_id):
    row = conn.execute(
        "SELECT id, name, email, password_hash, role, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return _user_row_to_dict(row) if row else None


def verify_login(conn, email, password):
    user = get_user_by_email(conn, email)
    if user is None or not check_password_hash(user["password_hash"], password):
        return None
    return user


def list_users(conn):
    rows = conn.execute(
        "SELECT id, name, email, password_hash, role, created_at FROM users ORDER BY id"
    ).fetchall()
    return [_user_row_to_dict(r) for r in rows]


def set_role(conn, user_id, role):
    with conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def update_user(conn, user_id, name=None, email=None, password_hash=None):
    """name/email/password_hashのうち指定されたものだけを更新する（Noneは変更しない）。"""
    fields, values = [], []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
    if email is not None:
        fields.append("email = ?")
        values.append(_norm_email(email))
    if password_hash is not None:
        fields.append("password_hash = ?")
        values.append(password_hash)
    if not fields:
        return
    values.append(user_id)
    with conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)


def delete_user(conn, user_id):
    with conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def count_admins(conn):
    row = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()
    return row[0] if row else 0


def email_taken(conn, email):
    """既存アカウント、または未処理(pending)の申請のいずれかで既に使われているか。"""
    email = _norm_email(email)
    if get_user_by_email(conn, email):
        return True
    row = conn.execute(
        "SELECT 1 FROM signup_requests WHERE email = ? AND status = 'pending'", (email,)
    ).fetchone()
    return row is not None


def create_signup_request(conn, name, email, password_hash, requested_at):
    with conn:
        conn.execute(
            """INSERT INTO signup_requests (name, email, password_hash, requested_at, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (name, _norm_email(email), password_hash, requested_at),
        )


def _request_row_to_dict(row):
    return {
        "id": row[0], "name": row[1], "email": row[2],
        "password_hash": row[3], "requested_at": row[4], "status": row[5],
    }


def list_signup_requests(conn, status="pending"):
    rows = conn.execute(
        "SELECT id, name, email, password_hash, requested_at, status FROM signup_requests WHERE status = ? ORDER BY id",
        (status,),
    ).fetchall()
    return [_request_row_to_dict(r) for r in rows]


def get_signup_request(conn, request_id):
    row = conn.execute(
        "SELECT id, name, email, password_hash, requested_at, status FROM signup_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    return _request_row_to_dict(row) if row else None


def approve_signup_request(conn, request_id, created_at):
    """申請を承認し、一般(general)ロールでアカウントを作成する。
    パスワードは申請時に既にハッシュ化済みのものをそのまま引き継ぐ(平文は扱わない)。
    """
    req = get_signup_request(conn, request_id)
    if req is None or req["status"] != "pending":
        return False
    with conn:
        conn.execute(
            "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, 'general', ?)",
            (req["name"], req["email"], req["password_hash"], created_at),
        )
        conn.execute("UPDATE signup_requests SET status = 'approved' WHERE id = ?", (request_id,))
    return True


def reject_signup_request(conn, request_id):
    with conn:
        conn.execute(
            "UPDATE signup_requests SET status = 'rejected' WHERE id = ? AND status = 'pending'",
            (request_id,),
        )


def record_login(conn, user_id, logged_in_at):
    with conn:
        conn.execute(
            "INSERT INTO login_events (user_id, logged_in_at) VALUES (?, ?)",
            (user_id, logged_in_at),
        )


def login_history(conn, user_id):
    """指定ユーザーの全ログイン履歴を新しい順で返す（メンバーページの一覧表示用）。"""
    rows = conn.execute(
        "SELECT logged_in_at FROM login_events WHERE user_id = ? ORDER BY logged_in_at DESC",
        (user_id,),
    ).fetchall()
    return [r[0] for r in rows]


def login_counts_by_day(conn, user_id):
    """日付(YYYY-MM-DD)ごとのログイン回数を返す（ヒートマップ用、ログインがあった日のみ含む）。"""
    rows = conn.execute(
        "SELECT substr(logged_in_at, 1, 10) AS d, COUNT(*) FROM login_events WHERE user_id = ? GROUP BY d",
        (user_id,),
    ).fetchall()
    return {d: c for d, c in rows}


def login_count(conn, user_id):
    row = conn.execute("SELECT COUNT(*) FROM login_events WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else 0


def record_audit(conn, actor_id, actor_name, action, detail, created_at):
    """管理操作(ロール変更・削除・承認・クリーンアップ等)の監査ログを1件追加する。
    actor_nameは実行者のアカウントが後で削除されても表示できるよう、idと別に保存しておく。
    """
    with conn:
        conn.execute(
            "INSERT INTO audit_log (actor_id, actor_name, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (actor_id, actor_name, action, detail, created_at),
        )


def list_audit_log(conn, limit=500):
    """新しい順で監査ログを返す（管理画面の一覧表示用）。"""
    rows = conn.execute(
        "SELECT id, actor_id, actor_name, action, detail, created_at FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"id": r[0], "actor_id": r[1], "actor_name": r[2], "action": r[3], "detail": r[4], "created_at": r[5]}
        for r in rows
    ]


__all__ = [
    "connect", "create_user", "get_user_by_email", "get_user_by_id", "verify_login",
    "list_users", "set_role", "update_user", "delete_user", "count_admins", "email_taken",
    "create_signup_request", "list_signup_requests", "get_signup_request",
    "approve_signup_request", "reject_signup_request", "generate_password_hash",
    "record_login", "login_history", "login_counts_by_day", "login_count",
    "record_audit", "list_audit_log",
]
