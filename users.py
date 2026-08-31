"""アカウント(ユーザー)・新規登録申請のSQLiteベースの台帳。

ログインはメールアドレス+パスワードで行う。本人が直接アカウントを作れるわけではなく、
まず申請(signup_requests)として保存され、管理者が承認して初めてusersに実体ができる。
パスワードは常にハッシュ化して保存し、平文は一切保持しない。
"""
import sqlite3
from datetime import date

from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'general',
    created_at TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT '',
    favorite_stations TEXT NOT NULL DEFAULT ''
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
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    logged_in_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_events_user_id ON login_events(user_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor_name TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
CREATE TABLE IF NOT EXISTS station_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_name TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_station_comments_station_name ON station_comments(station_name);
CREATE TABLE IF NOT EXISTS badge_awards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    badge_id TEXT NOT NULL,
    points INTEGER NOT NULL,
    awarded_at TEXT NOT NULL,
    UNIQUE(user_id, badge_id)
);
CREATE INDEX IF NOT EXISTS idx_badge_awards_user_id ON badge_awards(user_id);
CREATE TABLE IF NOT EXISTS user_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    frame_id TEXT NOT NULL,
    cost INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    UNIQUE(user_id, frame_id)
);
CREATE INDEX IF NOT EXISTS idx_user_frames_user_id ON user_frames(user_id);
"""

# 2026-08-29より前に作られたDBにはFOREIGN KEY制約が無いテーブルがある。
# SQLiteはALTER TABLEでFK制約を後付けできないため、_migrate_add_foreign_keysで
# テーブルを作り直して移行する（対象: login_events, audit_log, station_comments）。
_FK_MIGRATIONS = {
    "login_events": (
        """CREATE TABLE login_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            logged_in_at TEXT NOT NULL
        )""",
        "id, user_id, logged_in_at",
        "CREATE INDEX IF NOT EXISTS idx_login_events_user_id ON login_events(user_id)",
    ),
    "audit_log": (
        """CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_name TEXT,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        )""",
        "id, actor_id, actor_name, action, detail, created_at",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at)",
    ),
    "station_comments": (
        """CREATE TABLE station_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_name TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        "id, station_name, user_id, body, created_at",
        "CREATE INDEX IF NOT EXISTS idx_station_comments_station_name ON station_comments(station_name)",
    ),
}


def _migrate_add_foreign_keys(conn):
    for table, (create_sql, columns, index_sql) in _FK_MIGRATIONS.items():
        fk_rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        if fk_rows:
            continue  # 既に移行済み
        with conn:
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old_fkmig")
            conn.execute(create_sql)
            conn.execute(
                f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table}_old_fkmig"
            )
            conn.execute(f"DROP TABLE {table}_old_fkmig")
            conn.execute(index_sql)


def _ensure_profile_columns(conn):
    """bio/favorite_stationsは後から追加した列。既存のusers.dbには無いことがあるため、
    無ければALTER TABLEで追加する(重複追加はsqlite3.OperationalErrorとして無視する)。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "bio" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN bio TEXT NOT NULL DEFAULT ''")
    if "favorite_stations" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN favorite_stations TEXT NOT NULL DEFAULT ''")
    if "equipped_frame" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN equipped_frame TEXT NOT NULL DEFAULT ''")


def connect(db_path):
    conn = sqlite3.connect(db_path)
    # テーブル作り直し(移行)中はFK違反チェックを一時的に無効にする必要があるため、
    # スキーマ確定後にONへ切り替える。
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(SCHEMA)
    _ensure_profile_columns(conn)
    _migrate_add_foreign_keys(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _norm_email(email):
    return email.strip().lower()


_USER_COLUMNS = (
    "id, name, email, password_hash, role, created_at, bio, favorite_stations, equipped_frame"
)


def _user_row_to_dict(row):
    return {
        "id": row[0], "name": row[1], "email": row[2],
        "password_hash": row[3], "role": row[4], "created_at": row[5],
        "bio": row[6], "favorite_stations": row[7], "equipped_frame": row[8],
    }


def create_user(conn, name, email, password_hash, role, created_at):
    with conn:
        conn.execute(
            "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, _norm_email(email), password_hash, role, created_at),
        )


def get_user_by_email(conn, email):
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE email = ?",
        (_norm_email(email),),
    ).fetchone()
    return _user_row_to_dict(row) if row else None


def get_user_by_id(conn, user_id):
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return _user_row_to_dict(row) if row else None


# メールアドレスが存在しない場合にハッシュ比較そのものを省略すると、応答時間の差から
# アカウントの有無を推測できてしまう(タイミング攻撃)。存在しない場合でも同程度の時間の
# ハッシュ比較を行うためのダミーハッシュ(起動時に1回だけ生成する)。
_DUMMY_PASSWORD_HASH = generate_password_hash("dummy-password-for-timing-safety")


def verify_login(conn, email, password):
    user = get_user_by_email(conn, email)
    if user is None:
        check_password_hash(_DUMMY_PASSWORD_HASH, password)
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    return user


def list_users(conn):
    rows = conn.execute(f"SELECT {_USER_COLUMNS} FROM users ORDER BY id").fetchall()
    return [_user_row_to_dict(r) for r in rows]


def get_users_by_ids(conn, user_ids):
    """指定したidのユーザーだけをまとめて取得する({id: user_dict})。
    station_detail()のコメント投稿者名解決のように、全ユーザーの一部だけが必要な場面で
    list_users(conn)の全件取得を避けるために使う。"""
    ids = sorted(set(user_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {row[0]: _user_row_to_dict(row) for row in rows}


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


def update_profile(conn, user_id, bio=None, favorite_stations=None):
    """bio/favorite_stationsのうち指定されたものだけを更新する(Noneは変更しない)。
    favorite_stationsはカンマ区切りの地点名文字列として保存する。"""
    fields, values = [], []
    if bio is not None:
        fields.append("bio = ?")
        values.append(bio)
    if favorite_stations is not None:
        fields.append("favorite_stations = ?")
        values.append(favorite_stations)
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


def login_counts_all(conn):
    """全ユーザーのログイン回数を1クエリでまとめて返す({user_id: count})。
    members_list()のようにユーザー数分login_count()を呼ぶとN+1になる場面で使う。"""
    rows = conn.execute("SELECT user_id, COUNT(*) FROM login_events GROUP BY user_id").fetchall()
    return {uid: c for uid, c in rows}


def login_counts_since(conn, since_str):
    """指定日時以降のログイン回数を1クエリでまとめて返す({user_id: count})。
    「今月のランキング」等、期間を区切ったランキング表示に使う。"""
    rows = conn.execute(
        "SELECT user_id, COUNT(*) FROM login_events WHERE logged_in_at >= ? GROUP BY user_id",
        (since_str,),
    ).fetchall()
    return {uid: c for uid, c in rows}


def last_login(conn, user_id):
    row = conn.execute(
        "SELECT MAX(logged_in_at) FROM login_events WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row and row[0] else None


def last_logins_all(conn):
    """全ユーザーの最終ログイン日時を1クエリでまとめて返す({user_id: last_login})。"""
    rows = conn.execute("SELECT user_id, MAX(logged_in_at) FROM login_events GROUP BY user_id").fetchall()
    return {uid: v for uid, v in rows}


def _streak_from_dates(date_strings, today):
    if not date_strings:
        return 0
    days = sorted((date.fromisoformat(d) for d in date_strings), reverse=True)
    if (today - days[0]).days > 1:
        return 0
    streak = 1
    cur = days[0]
    for d in days[1:]:
        if (cur - d).days == 1:
            streak += 1
            cur = d
        else:
            break
    return streak


def login_streak(conn, user_id, today=None):
    """直近の連続ログイン日数を返す。今日または昨日のログインを起点に、日付が
    1日ずつ途切れず遡れる限り数える(2日以上ログインが無ければ0)。
    todayは基準日(date、省略時はサーバーのローカル日付)。ログイン記録はJSTで
    保存されているため、呼び出し側はJSTの日付を渡すこと。"""
    if today is None:
        today = date.today()
    rows = conn.execute(
        "SELECT DISTINCT substr(logged_in_at, 1, 10) AS d FROM login_events WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return _streak_from_dates([r[0] for r in rows], today)


def login_dates_all(conn):
    """全ユーザーの、ログインがあった日付(YYYY-MM-DD)集合を1クエリでまとめて返す
    ({user_id: set_of_date_strings})。login_streaks_all()で使う。"""
    rows = conn.execute(
        "SELECT DISTINCT user_id, substr(logged_in_at, 1, 10) AS d FROM login_events"
    ).fetchall()
    result = {}
    for uid, d in rows:
        result.setdefault(uid, set()).add(d)
    return result


def login_streaks_all(conn, today=None):
    """全ユーザーの連続ログイン日数を1クエリでまとめて返す({user_id: streak})。
    members_list()のようにユーザー数分login_streak()を呼ぶとN+1になる場面で使う。"""
    if today is None:
        today = date.today()
    dates_by_user = login_dates_all(conn)
    return {uid: _streak_from_dates(dates, today) for uid, dates in dates_by_user.items()}


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


def add_station_comment(conn, station_name, user_id, body, created_at):
    with conn:
        conn.execute(
            "INSERT INTO station_comments (station_name, user_id, body, created_at) VALUES (?, ?, ?, ?)",
            (station_name, user_id, body, created_at),
        )


def _comment_row_to_dict(row):
    return {
        "id": row[0], "station_name": row[1], "user_id": row[2],
        "body": row[3], "created_at": row[4],
    }


def comment_count(conn, user_id):
    """そのユーザーが投稿したコメントの総数(実績バッジの判定に使う)。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM station_comments WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row else 0


def list_station_comments(conn, station_name):
    """指定地点のコメントを古い順で返す(掲示板表示用)。"""
    rows = conn.execute(
        "SELECT id, station_name, user_id, body, created_at FROM station_comments "
        "WHERE station_name = ? ORDER BY id",
        (station_name,),
    ).fetchall()
    return [_comment_row_to_dict(r) for r in rows]


def get_station_comment(conn, comment_id):
    row = conn.execute(
        "SELECT id, station_name, user_id, body, created_at FROM station_comments WHERE id = ?",
        (comment_id,),
    ).fetchone()
    return _comment_row_to_dict(row) if row else None


def delete_station_comment(conn, comment_id):
    with conn:
        conn.execute("DELETE FROM station_comments WHERE id = ?", (comment_id,))


def award_badge(conn, user_id, badge_id, points, awarded_at):
    """指定した実績のポイントを付与する。UNIQUE(user_id, badge_id)制約により、
    同じ実績への二重付与は自動的に無視される(INSERT OR IGNORE)。
    新規に付与した場合はTrue、既に付与済みで何もしなかった場合はFalseを返す。"""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO badge_awards (user_id, badge_id, points, awarded_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, badge_id, points, awarded_at),
        )
        return cur.rowcount > 0


def total_earned_points(conn, user_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(points), 0) FROM badge_awards WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0]


def total_spent_points(conn, user_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(cost), 0) FROM user_frames WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0]


def available_points(conn, user_id):
    """実績で獲得した合計ポイントから、フレーム交換で使った分を差し引いた残高。"""
    return total_earned_points(conn, user_id) - total_spent_points(conn, user_id)


def owned_frame_ids(conn, user_id):
    rows = conn.execute(
        "SELECT frame_id FROM user_frames WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r[0] for r in rows}


def purchase_frame(conn, user_id, frame_id, cost, acquired_at):
    """ポイント残高が足りているかは呼び出し側で確認してから使うこと。既に所持している
    フレームの場合はUNIQUE制約により何もしない(二重購入・二重課金の防止)。"""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_frames (user_id, frame_id, cost, acquired_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, frame_id, cost, acquired_at),
        )


def set_equipped_frame(conn, user_id, frame_id):
    """frame_idに空文字を渡すと「フレームなし」の状態に戻せる。"""
    with conn:
        conn.execute("UPDATE users SET equipped_frame = ? WHERE id = ?", (frame_id or "", user_id))


__all__ = [
    "connect", "create_user", "get_user_by_email", "get_user_by_id", "verify_login",
    "list_users", "get_users_by_ids", "set_role", "update_user", "update_profile", "delete_user",
    "count_admins", "email_taken",
    "create_signup_request", "list_signup_requests", "get_signup_request",
    "approve_signup_request", "reject_signup_request", "generate_password_hash",
    "record_login", "login_history", "login_counts_by_day", "login_count",
    "login_counts_all", "login_counts_since", "last_login", "last_logins_all", "login_streak",
    "login_dates_all", "login_streaks_all",
    "record_audit", "list_audit_log",
    "add_station_comment", "list_station_comments", "get_station_comment",
    "delete_station_comment", "comment_count",
    "award_badge", "total_earned_points", "total_spent_points", "available_points",
    "owned_frame_ids", "purchase_frame", "set_equipped_frame",
]
