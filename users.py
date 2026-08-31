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
CREATE TABLE IF NOT EXISTS point_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    amount INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, source)
);
CREATE INDEX IF NOT EXISTS idx_point_events_user_id ON point_events(user_id);
CREATE TABLE IF NOT EXISTS dm_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_b_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    UNIQUE(user_a_id, user_b_id)
);
CREATE TABLE IF NOT EXISTS dm_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES dm_conversations(id) ON DELETE CASCADE,
    sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dm_messages_conversation_id ON dm_messages(conversation_id);
CREATE TABLE IF NOT EXISTS dm_read_state (
    conversation_id INTEGER NOT NULL REFERENCES dm_conversations(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_read_message_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, user_id)
);
CREATE TABLE IF NOT EXISTS dm_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES dm_messages(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shared_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    uploaded_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    uploaded_at TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_shared_files_uploaded_at ON shared_files(uploaded_at);
CREATE TABLE IF NOT EXISTS shared_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES shared_folders(id) ON DELETE CASCADE,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shared_folders_parent_id ON shared_folders(parent_id);
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


def _ensure_shared_files_columns(conn):
    """folder_idはフォルダ機能を後から追加した列。無ければ足す。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(shared_files)").fetchall()}
    if "folder_id" not in cols:
        conn.execute(
            "ALTER TABLE shared_files ADD COLUMN folder_id INTEGER REFERENCES shared_folders(id)"
        )


def connect(db_path):
    conn = sqlite3.connect(db_path)
    # テーブル作り直し(移行)中はFK違反チェックを一時的に無効にする必要があるため、
    # スキーマ確定後にONへ切り替える。
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(SCHEMA)
    _ensure_profile_columns(conn)
    _ensure_shared_files_columns(conn)
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
    """挿入した行のidを返す(コメント投稿ポイントの付与に、地点名+時刻より確実な
    一意キーとして使うため)。"""
    with conn:
        cur = conn.execute(
            "INSERT INTO station_comments (station_name, user_id, body, created_at) VALUES (?, ?, ?, ?)",
            (station_name, user_id, body, created_at),
        )
        return cur.lastrowid


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


def grant_points(conn, user_id, source, amount, created_at):
    """実績以外のポイント源(ログインボーナス・コメント投稿・AIチャット利用等)用の汎用付与。
    UNIQUE(user_id, source)制約により、同じsource文字列への二重付与を防ぐ。sourceは
    呼び出し元で一意になるよう組み立てる(例: "daily_login:2026-08-31", "comment:123")。
    新規付与ならTrue、既に付与済みならFalseを返す。"""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO point_events (user_id, source, amount, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, source, amount, created_at),
        )
        return cur.rowcount > 0


def total_earned_points(conn, user_id):
    """実績ポイント(badge_awards)とそれ以外のポイント(point_events)の合計。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(points), 0) FROM badge_awards WHERE user_id = ?", (user_id,)
    ).fetchone()
    row2 = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM point_events WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] + row2[0]


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


def _dm_pair(user_a_id, user_b_id):
    """2人のuser_idを常に同じ順序に正規化する(dm_conversationsのUNIQUE制約が
    「AさんとBさん」と「BさんとAさん」を別会話として二重作成しないようにするため)。"""
    return (user_a_id, user_b_id) if user_a_id < user_b_id else (user_b_id, user_a_id)


def get_or_create_dm_conversation(conn, user_a_id, user_b_id, created_at):
    a, b = _dm_pair(user_a_id, user_b_id)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO dm_conversations (user_a_id, user_b_id, created_at, last_message_at) "
            "VALUES (?, ?, ?, ?)",
            (a, b, created_at, created_at),
        )
        row = conn.execute(
            "SELECT id FROM dm_conversations WHERE user_a_id = ? AND user_b_id = ?", (a, b)
        ).fetchone()
    return row[0]


def get_dm_conversation(conn, conversation_id):
    row = conn.execute(
        "SELECT id, user_a_id, user_b_id, last_message_at FROM dm_conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "user_a_id": row[1], "user_b_id": row[2], "last_message_at": row[3]}


def send_dm_message(conn, conversation_id, sender_id, body, created_at):
    with conn:
        cur = conn.execute(
            "INSERT INTO dm_messages (conversation_id, sender_id, body, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, sender_id, body, created_at),
        )
        conn.execute(
            "UPDATE dm_conversations SET last_message_at = ? WHERE id = ?", (created_at, conversation_id),
        )
        return cur.lastrowid


def list_dm_messages(conn, conversation_id, after_id=0):
    """after_idより新しいメッセージだけを返す(0なら全件。スレッド表示の初回取得と
    ポーリングでの差分取得の両方に使う)。"""
    rows = conn.execute(
        "SELECT id, sender_id, body, created_at FROM dm_messages "
        "WHERE conversation_id = ? AND id > ? ORDER BY id",
        (conversation_id, after_id),
    ).fetchall()
    return [{"id": r[0], "sender_id": r[1], "body": r[2], "created_at": r[3]} for r in rows]


def list_dm_conversations(conn, user_id):
    """user_idが参加している会話一覧を、最新メッセージが新しい順に返す
    (相手のuser_id・最新メッセージ日時のみ。相手の名前やアバターは呼び出し側で解決する)。"""
    rows = conn.execute(
        "SELECT id, user_a_id, user_b_id, last_message_at FROM dm_conversations "
        "WHERE user_a_id = ? OR user_b_id = ? ORDER BY last_message_at DESC",
        (user_id, user_id),
    ).fetchall()
    result = []
    for r in rows:
        other_id = r[2] if r[1] == user_id else r[1]
        result.append({"id": r[0], "other_id": other_id, "last_message_at": r[3]})
    return result


def mark_dm_read(conn, conversation_id, user_id, last_read_message_id):
    """last_read_message_idは既読にする最新メッセージid。既存の既読位置より
    古い値では絶対に後退させない(ポーリングの返答順序が前後しても安全なように)。"""
    with conn:
        conn.execute(
            "INSERT INTO dm_read_state (conversation_id, user_id, last_read_message_id) VALUES (?, ?, ?) "
            "ON CONFLICT(conversation_id, user_id) DO UPDATE SET last_read_message_id = "
            "MAX(last_read_message_id, excluded.last_read_message_id)",
            (conversation_id, user_id, last_read_message_id),
        )


def dm_unread_counts(conn, user_id):
    """{conversation_id: 未読数} を返す(0件の会話は含めない)。"""
    rows = conn.execute(
        """
        SELECT dc.id,
               (SELECT COUNT(*) FROM dm_messages dm
                WHERE dm.conversation_id = dc.id
                  AND dm.sender_id != ?
                  AND dm.id > COALESCE(
                      (SELECT last_read_message_id FROM dm_read_state
                       WHERE conversation_id = dc.id AND user_id = ?), 0))
        FROM dm_conversations dc
        WHERE dc.user_a_id = ? OR dc.user_b_id = ?
        """,
        (user_id, user_id, user_id, user_id),
    ).fetchall()
    return {r[0]: r[1] for r in rows if r[1] > 0}


def total_dm_unread(conn, user_id):
    return sum(dm_unread_counts(conn, user_id).values())


def add_dm_attachment(conn, message_id, filename, stored_name, content_type, size_bytes, uploaded_at):
    with conn:
        cur = conn.execute(
            "INSERT INTO dm_attachments (message_id, filename, stored_name, content_type, size_bytes, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, filename, stored_name, content_type, size_bytes, uploaded_at),
        )
        return cur.lastrowid


def get_dm_attachment(conn, attachment_id):
    """添付ファイルと、それが属する会話のuser_a_id/user_b_idも一緒に返す
    (ダウンロード時に会話の参加者本人かどうかをその場で確認できるようにするため)。"""
    row = conn.execute(
        """
        SELECT a.id, a.filename, a.stored_name, a.content_type, a.size_bytes,
               m.conversation_id, c.user_a_id, c.user_b_id
        FROM dm_attachments a
        JOIN dm_messages m ON m.id = a.message_id
        JOIN dm_conversations c ON c.id = m.conversation_id
        WHERE a.id = ?
        """,
        (attachment_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "filename": row[1], "stored_name": row[2], "content_type": row[3],
        "size_bytes": row[4], "conversation_id": row[5], "user_a_id": row[6], "user_b_id": row[7],
    }


def get_dm_attachments_for_messages(conn, message_ids):
    """{message_id: [添付情報, ...]} を返す(スレッド表示でメッセージ一覧と一緒にまとめて
    引くため。1件ずつ引くとメッセージ数だけクエリが発生してしまう)。"""
    ids = sorted(set(message_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, message_id, filename, size_bytes FROM dm_attachments "
        f"WHERE message_id IN ({placeholders})",
        ids,
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r[1], []).append({"id": r[0], "filename": r[2], "size_bytes": r[3]})
    return result


def get_dm_message(conn, message_id):
    """メッセージ本体と、それが属する会話のuser_a_id/user_b_idを一緒に返す
    (削除時に本人か会話の当事者かをその場で判定できるようにするため)。"""
    row = conn.execute(
        """
        SELECT m.id, m.conversation_id, m.sender_id, c.user_a_id, c.user_b_id
        FROM dm_messages m
        JOIN dm_conversations c ON c.id = m.conversation_id
        WHERE m.id = ?
        """,
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "conversation_id": row[1], "sender_id": row[2],
        "user_a_id": row[3], "user_b_id": row[4],
    }


def get_dm_message_attachment_stored_names(conn, message_id):
    """メッセージ削除の前に、ディスク上から一緒に消す添付ファイルの保存名一覧を返す
    (dm_attachmentsの行自体はON DELETE CASCADEで自動的に消えるが、ファイル本体は
    別途消す必要があるため)。"""
    rows = conn.execute(
        "SELECT stored_name FROM dm_attachments WHERE message_id = ?", (message_id,)
    ).fetchall()
    return [r[0] for r in rows]


def delete_dm_message(conn, message_id):
    with conn:
        conn.execute("DELETE FROM dm_messages WHERE id = ?", (message_id,))


def add_shared_file(
    conn, filename, stored_name, content_type, size_bytes, uploaded_by, uploaded_at,
    description="", folder_id=None,
):
    with conn:
        cur = conn.execute(
            "INSERT INTO shared_files "
            "(filename, stored_name, content_type, size_bytes, uploaded_by, uploaded_at, description, folder_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (filename, stored_name, content_type, size_bytes, uploaded_by, uploaded_at, description, folder_id),
        )
        return cur.lastrowid


def list_shared_files(conn, folder_id=None):
    """folder_id=Noneは最上位(フォルダに属さない)ファイルだけを返す。"""
    if folder_id is None:
        rows = conn.execute(
            "SELECT id, filename, content_type, size_bytes, uploaded_by, uploaded_at, description, folder_id "
            "FROM shared_files WHERE folder_id IS NULL ORDER BY uploaded_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, filename, content_type, size_bytes, uploaded_by, uploaded_at, description, folder_id "
            "FROM shared_files WHERE folder_id = ? ORDER BY uploaded_at DESC",
            (folder_id,),
        ).fetchall()
    return [
        {
            "id": r[0], "filename": r[1], "content_type": r[2], "size_bytes": r[3],
            "uploaded_by": r[4], "uploaded_at": r[5], "description": r[6], "folder_id": r[7],
        }
        for r in rows
    ]


def get_shared_file(conn, file_id):
    row = conn.execute(
        "SELECT id, filename, stored_name, content_type, size_bytes, uploaded_by, uploaded_at, "
        "description, folder_id FROM shared_files WHERE id = ?",
        (file_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "filename": row[1], "stored_name": row[2], "content_type": row[3],
        "size_bytes": row[4], "uploaded_by": row[5], "uploaded_at": row[6], "description": row[7],
        "folder_id": row[8],
    }


def delete_shared_file(conn, file_id):
    with conn:
        conn.execute("DELETE FROM shared_files WHERE id = ?", (file_id,))


def create_shared_folder(conn, name, parent_id, created_by, created_at):
    with conn:
        cur = conn.execute(
            "INSERT INTO shared_folders (name, parent_id, created_by, created_at) VALUES (?, ?, ?, ?)",
            (name, parent_id, created_by, created_at),
        )
        return cur.lastrowid


def get_shared_folder(conn, folder_id):
    row = conn.execute(
        "SELECT id, name, parent_id, created_by, created_at FROM shared_folders WHERE id = ?",
        (folder_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "parent_id": row[2], "created_by": row[3], "created_at": row[4]}


def list_shared_folders(conn, parent_id=None):
    if parent_id is None:
        rows = conn.execute(
            "SELECT id, name, parent_id, created_by, created_at FROM shared_folders "
            "WHERE parent_id IS NULL ORDER BY name"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, parent_id, created_by, created_at FROM shared_folders "
            "WHERE parent_id = ? ORDER BY name",
            (parent_id,),
        ).fetchall()
    return [
        {"id": r[0], "name": r[1], "parent_id": r[2], "created_by": r[3], "created_at": r[4]}
        for r in rows
    ]


def shared_folder_breadcrumbs(conn, folder_id):
    """ルートから対象フォルダまでの祖先を [{id, name}, ...] の順で返す(パンくずリスト用)。"""
    trail = []
    seen = set()
    current = folder_id
    while current is not None and current not in seen:
        seen.add(current)
        folder = get_shared_folder(conn, current)
        if folder is None:
            break
        trail.append({"id": folder["id"], "name": folder["name"]})
        current = folder["parent_id"]
    trail.reverse()
    return trail


def shared_folder_is_empty(conn, folder_id):
    has_subfolder = conn.execute(
        "SELECT 1 FROM shared_folders WHERE parent_id = ? LIMIT 1", (folder_id,)
    ).fetchone()
    has_file = conn.execute(
        "SELECT 1 FROM shared_files WHERE folder_id = ? LIMIT 1", (folder_id,)
    ).fetchone()
    return has_subfolder is None and has_file is None


def delete_shared_folder(conn, folder_id):
    with conn:
        conn.execute("DELETE FROM shared_folders WHERE id = ?", (folder_id,))


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
    "award_badge", "grant_points", "total_earned_points", "total_spent_points", "available_points",
    "owned_frame_ids", "purchase_frame", "set_equipped_frame",
    "get_or_create_dm_conversation", "get_dm_conversation", "send_dm_message",
    "list_dm_messages", "list_dm_conversations", "mark_dm_read", "dm_unread_counts", "total_dm_unread",
    "add_dm_attachment", "get_dm_attachment", "get_dm_attachments_for_messages",
    "get_dm_message", "get_dm_message_attachment_stored_names", "delete_dm_message",
    "add_shared_file", "list_shared_files", "get_shared_file", "delete_shared_file",
    "create_shared_folder", "get_shared_folder", "list_shared_folders",
    "shared_folder_breadcrumbs", "shared_folder_is_empty", "delete_shared_folder",
]
