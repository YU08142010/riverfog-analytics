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


__all__ = [
    "connect", "create_user", "get_user_by_email", "get_user_by_id", "verify_login",
    "list_users", "set_role", "delete_user", "count_admins", "email_taken",
    "create_signup_request", "list_signup_requests", "get_signup_request",
    "approve_signup_request", "reject_signup_request", "generate_password_hash",
]
