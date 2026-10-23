"""アカウント(ユーザー)・新規登録申請のSQLiteベースの台帳。

ログインはメールアドレス+パスワードで行う。本人が直接アカウントを作れるわけではなく、
まず申請(signup_requests)として保存され、管理者が承認して初めてusersに実体ができる。
パスワードは常にハッシュ化して保存し、平文は一切保持しない。
"""
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger("users")
_foreign_keys_verified = False

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
CREATE TABLE IF NOT EXISTS login_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT,
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_failures_email ON login_failures(email);
CREATE TABLE IF NOT EXISTS user_presence (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    last_active_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS role_change_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_role TEXT NOT NULL,
    requested_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
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
CREATE TABLE IF NOT EXISTS user_cosmetics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    item_id TEXT NOT NULL,
    cost INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    UNIQUE(user_id, category, item_id)
);
CREATE INDEX IF NOT EXISTS idx_user_cosmetics_user_id ON user_cosmetics(user_id);
CREATE TABLE IF NOT EXISTS user_consumables (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, item_id)
);
CREATE TABLE IF NOT EXISTS consumable_purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    cost INTEGER NOT NULL,
    purchased_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consumable_purchases_user_id ON consumable_purchases(user_id);
CREATE TABLE IF NOT EXISTS point_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    amount INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, source)
);
CREATE INDEX IF NOT EXISTS idx_point_events_user_id ON point_events(user_id);
CREATE TABLE IF NOT EXISTS gem_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    amount INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, source)
);
CREATE INDEX IF NOT EXISTS idx_gem_events_user_id ON gem_events(user_id);
CREATE TABLE IF NOT EXISTS user_gem_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    cost_gems INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    UNIQUE(user_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_user_gem_items_user_id ON user_gem_items(user_id);
CREATE TABLE IF NOT EXISTS fog_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    station_name TEXT NOT NULL,
    precision TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    bet_amount INTEGER NOT NULL,
    multiplier REAL NOT NULL,
    placed_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_at TEXT,
    payout INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fog_bets_user_id ON fog_bets(user_id);
CREATE INDEX IF NOT EXISTS idx_fog_bets_status ON fog_bets(status);
CREATE TABLE IF NOT EXISTS daily_fog_quiz (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    quiz_date TEXT NOT NULL,
    station_name TEXT NOT NULL,
    guess INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_at TEXT,
    payout INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, quiz_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_fog_quiz_status ON daily_fog_quiz(status, quiz_date);
CREATE TABLE IF NOT EXISTS daily_fog_quiz_stats (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    current_streak INTEGER NOT NULL DEFAULT 0,
    best_streak INTEGER NOT NULL DEFAULT 0,
    total_correct INTEGER NOT NULL DEFAULT 0,
    total_played INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily_phenom_quiz (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    quiz_date TEXT NOT NULL,
    category_guess TEXT NOT NULL,
    correct INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, quiz_date)
);
CREATE TABLE IF NOT EXISTS phenomena_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_name TEXT NOT NULL,
    target_datetime TEXT NOT NULL,
    original_code INTEGER,
    corrected_code INTEGER NOT NULL,
    corrected_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    corrected_at TEXT NOT NULL,
    UNIQUE(station_name, target_datetime)
);
CREATE TABLE IF NOT EXISTS observation_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_name TEXT NOT NULL,
    target_datetime TEXT NOT NULL,
    reported_code INTEGER,
    reported_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reported_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    resolved_at TEXT,
    resolution_note TEXT,
    importance TEXT NOT NULL DEFAULT 'medium',
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_observation_reports_status ON observation_reports(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_observation_reports_pending_unique
    ON observation_reports(station_name, target_datetime) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS feedback_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    gemini_summary TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_reports_status ON feedback_reports(status);
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
CREATE TABLE IF NOT EXISTS dm_pinned_conversations (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES dm_conversations(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, conversation_id)
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
CREATE TABLE IF NOT EXISTS dm_message_reactions (
    message_id INTEGER NOT NULL REFERENCES dm_messages(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id, emoji)
);
CREATE TABLE IF NOT EXISTS dm_message_mentions (
    message_id INTEGER NOT NULL REFERENCES dm_messages(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (message_id, user_id)
);
-- 全員参加のグループチャット(単一チャンネル、参加者管理なし)。dm_messages/dm_attachments/
-- dm_read_stateと対になる構造だが、宛先が固定(全員)なので会話ID(dm_conversations相当)を持たない。
CREATE TABLE IF NOT EXISTS group_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    edited_at TEXT,
    reply_to_id INTEGER REFERENCES group_messages(id) ON DELETE SET NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    card_type TEXT,
    card_data TEXT
);
CREATE TABLE IF NOT EXISTS group_message_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES group_messages(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_read_state (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    last_read_message_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS group_message_reactions (
    message_id INTEGER NOT NULL REFERENCES group_messages(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id, emoji)
);
CREATE TABLE IF NOT EXISTS group_message_mentions (
    message_id INTEGER NOT NULL REFERENCES group_messages(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (message_id, user_id)
);
CREATE TABLE IF NOT EXISTS shared_file_stars (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES shared_files(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, file_id)
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
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    device_label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_success_at TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user_id ON push_subscriptions(user_id);
CREATE TABLE IF NOT EXISTS gold_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_name TEXT NOT NULL,
    target_datetime TEXT NOT NULL,
    stratum TEXT NOT NULL,
    weight REAL NOT NULL,
    ai_code INTEGER,
    ai_confidence TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (station_name, target_datetime)
);
CREATE TABLE IF NOT EXISTS gold_labels (
    sample_id INTEGER NOT NULL REFERENCES gold_samples(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code INTEGER NOT NULL,
    labeled_at TEXT NOT NULL,
    PRIMARY KEY (sample_id, user_id)
);
CREATE TABLE IF NOT EXISTS push_sent (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
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
    if "locked_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN locked_at TEXT")
    for col in (
        "equipped_title", "equipped_name_color", "equipped_dashboard_theme", "equipped_effect",
        "equipped_banner", "equipped_dm_theme", "equipped_title_color", "equipped_marker",
        "equipped_title_badge", "equipped_bubble_style", "equipped_avatar_bg",
        "equipped_rank_style", "equipped_profile_sound", "equipped_seasonal", "equipped_gem_item",
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _ensure_shared_files_columns(conn):
    """folder_idはフォルダ機能を後から追加した列。tags(自動タグ付け)・content_text
    (検索用に読み込んだテキスト系ファイルの中身)も後から追加した列。無ければ足す。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(shared_files)").fetchall()}
    if "folder_id" not in cols:
        conn.execute(
            "ALTER TABLE shared_files ADD COLUMN folder_id INTEGER REFERENCES shared_folders(id)"
        )
    if "tags" not in cols:
        conn.execute("ALTER TABLE shared_files ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
    if "content_text" not in cols:
        conn.execute("ALTER TABLE shared_files ADD COLUMN content_text TEXT NOT NULL DEFAULT ''")


def _ensure_dm_messages_columns(conn):
    """返信・編集・ピン留め・地点共有カードは後から追加した列。無ければ足す。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(dm_messages)").fetchall()}
    if "reply_to_id" not in cols:
        conn.execute("ALTER TABLE dm_messages ADD COLUMN reply_to_id INTEGER REFERENCES dm_messages(id)")
    if "edited_at" not in cols:
        conn.execute("ALTER TABLE dm_messages ADD COLUMN edited_at TEXT")
    if "pinned" not in cols:
        conn.execute("ALTER TABLE dm_messages ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    if "card_type" not in cols:
        conn.execute("ALTER TABLE dm_messages ADD COLUMN card_type TEXT")
    if "card_data" not in cols:
        conn.execute("ALTER TABLE dm_messages ADD COLUMN card_data TEXT")


def _ensure_fog_bets_columns(conn):
    """insuranceは保険チケット(ハズレても賭け金の半額を払い戻す消費アイテム)を使った
    賭けかどうかのフラグ。後から追加した列なので無ければ足す。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(fog_bets)").fetchall()}
    if "insurance" not in cols:
        conn.execute("ALTER TABLE fog_bets ADD COLUMN insurance INTEGER NOT NULL DEFAULT 0")


def _migrate_phenomena_corrections_nullable_source(conn):
    """自動異常検知がcorrected_by=NULL(source='auto')で記録できるよう、既存の
    corrected_by NOT NULL制約を緩和し、source列(human/auto)を追加する。
    SQLiteはALTER TABLEでNOT NULLを外せないため、テーブルを作り直す。"""
    cols = conn.execute("PRAGMA table_info(phenomena_corrections)").fetchall()
    corrected_by_col = next((c for c in cols if c[1] == "corrected_by"), None)
    if corrected_by_col is None or corrected_by_col[3] == 0:
        return  # 列が無い、または既にNOT NULLが外れている(移行済み)
    with conn:
        conn.execute("ALTER TABLE phenomena_corrections RENAME TO phenomena_corrections_old_mig")
        conn.execute("CREATE TABLE phenomena_corrections (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    station_name TEXT NOT NULL,\n    target_datetime TEXT NOT NULL,\n    original_code INTEGER,\n    corrected_code INTEGER NOT NULL,\n    corrected_by INTEGER REFERENCES users(id) ON DELETE CASCADE,\n    corrected_at TEXT NOT NULL,\n    source TEXT NOT NULL DEFAULT 'human',\n    UNIQUE(station_name, target_datetime)\n)")
        conn.execute(
            "INSERT INTO phenomena_corrections "
            "(id, station_name, target_datetime, original_code, corrected_code, corrected_by, corrected_at) "
            "SELECT id, station_name, target_datetime, original_code, corrected_code, corrected_by, corrected_at "
            "FROM phenomena_corrections_old_mig"
        )
        conn.execute("DROP TABLE phenomena_corrections_old_mig")


def _ensure_subscription_billing_columns(conn):
    """フレーム・その他装備品を買い切りから月額使用料制に変えた際に追加した列。
    next_billing_atがNULLでない かつ 現在時刻以下になった装備中のアイテムから
    毎月使用料を引き落とす(未装着のものは請求しない)。既存の所持者がいきなり
    請求されて驚かないよう、列を新規追加した直後だけ「今から30日後」を初期値として
    バックフィルする(1回きりの移行猶予期間)。"""
    frame_cols = {row[1] for row in conn.execute("PRAGMA table_info(user_frames)").fetchall()}
    frame_is_new = "next_billing_at" not in frame_cols
    if frame_is_new:
        conn.execute("ALTER TABLE user_frames ADD COLUMN next_billing_at TEXT")
    cosmetic_cols = {row[1] for row in conn.execute("PRAGMA table_info(user_cosmetics)").fetchall()}
    cosmetic_is_new = "next_billing_at" not in cosmetic_cols
    if cosmetic_is_new:
        conn.execute("ALTER TABLE user_cosmetics ADD COLUMN next_billing_at TEXT")
    if frame_is_new or cosmetic_is_new:
        grace = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        with conn:
            if frame_is_new:
                conn.execute("UPDATE user_frames SET next_billing_at = ? WHERE next_billing_at IS NULL", (grace,))
            if cosmetic_is_new:
                conn.execute("UPDATE user_cosmetics SET next_billing_at = ? WHERE next_billing_at IS NULL", (grace,))


def _ensure_observation_report_importance_column(conn):
    """observation_reportsに後から追加したimportance列(low/medium/high)。ジェム報酬の
    金額とキューの並び順に使う。既存行はデフォルトの'medium'扱いにする。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(observation_reports)").fetchall()}
    if "importance" not in cols:
        conn.execute("ALTER TABLE observation_reports ADD COLUMN importance TEXT NOT NULL DEFAULT 'medium'")


def _ensure_observation_report_reason_column(conn):
    """observation_reportsに後から追加したreason列。報告者が「なぜこの現象だと思ったか」を
    自由記述で残せるようにするための列(管理者が承認キューで画像と一緒に確認する)。
    自動検知(source="auto_detected")分には人の理由が無いためNULLのままになる。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(observation_reports)").fetchall()}
    if "reason" not in cols:
        conn.execute("ALTER TABLE observation_reports ADD COLUMN reason TEXT")


def _ensure_login_events_columns(conn):
    """ipは異常ログイン検知(未知IPからの管理者ログイン等)のために後から追加した列。
    logged_out_atはログイン履歴に終了時刻も表示するために後から追加した列
    (/logoutを踏んだ場合のみ記録される。タイムアウトやタブを閉じただけの場合はNULLのまま)。
    login_eventsはFK移行(_migrate_add_foreign_keys)でテーブルごと作り直されることが
    あるため、移行の後に呼ぶこと。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(login_events)").fetchall()}
    if "ip" not in cols:
        conn.execute("ALTER TABLE login_events ADD COLUMN ip TEXT")
    if "logged_out_at" not in cols:
        conn.execute("ALTER TABLE login_events ADD COLUMN logged_out_at TEXT")


def _verify_foreign_keys(conn):
    """SCHEMA文字列でREFERENCESを書いているテーブルが、実際にこのDBファイル上でも
    外部キーとして有効になっているかを確認し、ずれていればログに警告する。

    CREATE TABLE IF NOT EXISTSは既存テーブルに対して無視されるため、あるテーブルが
    「REFERENCESが書き足される前」に一度作られたまま残っていると、SCHEMA文字列の
    見た目上はFKがあるように見えても実際には効いていない、という状態になりうる
    (login_events/audit_log/station_commentsで過去に実際に発生し、_migrate_add_foreign_keys
    で個別に直した)。他のテーブルで同じ問題が今後(または他の環境で)静かに発生しても
    気づけるよう、起動のたびに全テーブルを機械的に突き合わせる(2026-09-13の監査で
    「3テーブルしか個別対応していない」点を指摘された)。"""
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", SCHEMA, re.S)
    for table, body in tables:
        if "REFERENCES" not in body:
            continue
        actual_fks = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        if not actual_fks:
            log.warning(
                "Table %s declares REFERENCES in SCHEMA but has no foreign key enforced on this "
                "database file (likely created before the REFERENCES clause was added); "
                "ON DELETE CASCADE will not apply to it. Consider adding a migration like "
                "_migrate_add_foreign_keys for this table.",
                table,
            )


def connect(db_path):
    # webapp.pyの複数ワーカースレッドが同時に読み書きするDBのため、busy_timeoutを
    # 設定して他スレッドの書き込み中でも即座にエラーにせず一定時間待たせる
    # (weather_store.py等の他DBモジュールと同じ対策。WALモードにすると読み取りが
    # 書き込みをブロックしなくなり並行性がさらに上がる)。
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    # テーブル作り直し(移行)中はFK違反チェックを一時的に無効にする必要があるため、
    # スキーマ確定後にONへ切り替える。
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(SCHEMA)
    _ensure_profile_columns(conn)
    _ensure_shared_files_columns(conn)
    _ensure_fog_bets_columns(conn)
    _ensure_subscription_billing_columns(conn)
    _ensure_dm_messages_columns(conn)
    _migrate_add_foreign_keys(conn)
    _migrate_phenomena_corrections_nullable_source(conn)
    _ensure_login_events_columns(conn)
    _ensure_observation_report_importance_column(conn)
    _ensure_observation_report_reason_column(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    global _foreign_keys_verified
    if not _foreign_keys_verified:
        # connect()はリクエストのたびに呼ばれるため、この検証はプロセスごとに1回だけ
        # 行う(毎回全34テーブル分PRAGMAを打つのは無駄なオーバーヘッドになるため)。
        _verify_foreign_keys(conn)
        _foreign_keys_verified = True
    return conn


def _escape_like(keyword):
    """SQLのLIKE検索でキーワード中の%・_をワイルドカードではなく文字そのものとして
    扱わせるためにエスケープする(呼び出し側でLIKE ? ESCAPE '\\'と組み合わせて使う)。
    パラメータ化されているためSQLインジェクションのリスクは無いが、例えば"50%"で検索
    すると"%"がワイルドカード扱いされ「50に続く任意の文字列」にマッチしてしまい、
    利用者の意図と異なる検索結果になっていた(2026-09-13の監査で指摘)。"""
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _norm_email(email):
    return email.strip().lower()


_USER_COLUMNS = (
    "id, name, email, password_hash, role, created_at, bio, favorite_stations, equipped_frame, locked_at, "
    "equipped_title, equipped_name_color, equipped_dashboard_theme, equipped_effect, "
    "equipped_banner, equipped_dm_theme, equipped_title_color, equipped_marker, "
    "equipped_title_badge, equipped_bubble_style, equipped_avatar_bg, equipped_rank_style, "
    "equipped_profile_sound, equipped_seasonal, equipped_gem_item"
)


def _user_row_to_dict(row):
    return {
        "id": row[0], "name": row[1], "email": row[2],
        "password_hash": row[3], "role": row[4], "created_at": row[5],
        "bio": row[6], "favorite_stations": row[7], "equipped_frame": row[8],
        "locked_at": row[9], "equipped_title": row[10], "equipped_name_color": row[11],
        "equipped_dashboard_theme": row[12], "equipped_effect": row[13],
        "equipped_banner": row[14], "equipped_dm_theme": row[15],
        "equipped_title_color": row[16], "equipped_marker": row[17],
        "equipped_title_badge": row[18], "equipped_bubble_style": row[19],
        "equipped_avatar_bg": row[20], "equipped_rank_style": row[21],
        "equipped_profile_sound": row[22], "equipped_seasonal": row[23],
        "equipped_gem_item": row[24],
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


def record_login_failure(conn, email, ip, user_agent, attempted_at):
    with conn:
        conn.execute(
            "INSERT INTO login_failures (email, ip, user_agent, attempted_at) VALUES (?, ?, ?, ?)",
            (_norm_email(email), ip, user_agent, attempted_at),
        )


def count_login_failures(conn, email):
    """直近のリセット(ログイン成功またはロック解除)以降に溜まっている失敗回数。
    成功・解除のたびにclear_login_failures()で該当分を消しているので、単純に
    残っている行数を数えるだけでよい。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM login_failures WHERE email = ?", (_norm_email(email),)
    ).fetchone()
    return row[0] if row else 0


def recent_login_failures(conn, email, limit=20):
    """ロック解除画面で「誰が・どのIPから・いつ」失敗したかを見せるための一覧
    (新しい順)。"""
    rows = conn.execute(
        "SELECT ip, user_agent, attempted_at FROM login_failures "
        "WHERE email = ? ORDER BY attempted_at DESC LIMIT ?",
        (_norm_email(email), limit),
    ).fetchall()
    return [{"ip": r[0], "user_agent": r[1], "attempted_at": r[2]} for r in rows]


def clear_login_failures(conn, email):
    with conn:
        conn.execute("DELETE FROM login_failures WHERE email = ?", (_norm_email(email),))


def lock_account(conn, user_id, locked_at):
    with conn:
        conn.execute("UPDATE users SET locked_at = ? WHERE id = ?", (locked_at, user_id))


def unlock_account(conn, user_id):
    """ロック解除。以後の再ロックが即座に起きないよう、溜まっていた失敗履歴も
    合わせて消す。"""
    with conn:
        row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.execute("UPDATE users SET locked_at = NULL WHERE id = ?", (user_id,))
        if row:
            conn.execute("DELETE FROM login_failures WHERE email = ?", (row[0],))


def list_locked_users(conn):
    rows = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE locked_at IS NOT NULL ORDER BY locked_at DESC"
    ).fetchall()
    return [_user_row_to_dict(r) for r in rows]


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


def create_role_change_request(conn, target_user_id, requested_role, requested_by, requested_at):
    with conn:
        cur = conn.execute(
            "INSERT INTO role_change_requests "
            "(target_user_id, requested_role, requested_by, requested_at) VALUES (?, ?, ?, ?)",
            (target_user_id, requested_role, requested_by, requested_at),
        )
        return cur.lastrowid


def get_role_change_request(conn, request_id):
    row = conn.execute(
        "SELECT id, target_user_id, requested_role, requested_by, requested_at, status "
        "FROM role_change_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "target_user_id": row[1], "requested_role": row[2],
        "requested_by": row[3], "requested_at": row[4], "status": row[5],
    }


def list_pending_role_change_requests(conn):
    rows = conn.execute(
        "SELECT id, target_user_id, requested_role, requested_by, requested_at, status "
        "FROM role_change_requests WHERE status = 'pending' ORDER BY requested_at"
    ).fetchall()
    return [
        {
            "id": r[0], "target_user_id": r[1], "requested_role": r[2],
            "requested_by": r[3], "requested_at": r[4], "status": r[5],
        }
        for r in rows
    ]


def set_role_change_request_status(conn, request_id, status):
    with conn:
        conn.execute(
            "UPDATE role_change_requests SET status = ? WHERE id = ?", (status, request_id)
        )


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


def count_signup_requests(conn, status="pending"):
    """指定ステータスの申請数。/signupの受付上限(承認待ちキューの膨張・DoS対策)の判定に使う。"""
    return conn.execute(
        "SELECT COUNT(*) FROM signup_requests WHERE status = ?", (status,),
    ).fetchone()[0]


def get_signup_request(conn, request_id):
    row = conn.execute(
        "SELECT id, name, email, password_hash, requested_at, status FROM signup_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    return _request_row_to_dict(row) if row else None


class DuplicateEmailError(Exception):
    """承認しようとした申請のメールアドレスが既に別アカウントで使われている時に送出する。"""


def approve_signup_request(conn, request_id, created_at):
    """申請を承認し、一般(general)ロールでアカウントを作成する。
    パスワードは申請時に既にハッシュ化済みのものをそのまま引き継ぐ(平文は扱わない)。

    メールアドレスが既に別アカウントで使われている場合(同じメールで複数回申請された、
    承認待ちの間に別経路で同じメールのアカウントができた等)はDuplicateEmailErrorを送出
    する。以前はusers.emailのUNIQUE制約に任せきりで、この場合に生のsqlite3.IntegrityError
    がそのまま呼び出し元(管理画面)に漏れ、エラー画面になっていた(2026-10-23に発覚)。
    """
    req = get_signup_request(conn, request_id)
    if req is None or req["status"] != "pending":
        return False
    if get_user_by_email(conn, req["email"]) is not None:
        raise DuplicateEmailError(req["email"])
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


def record_login(conn, user_id, logged_in_at, ip=None):
    """新しいログインを記録する前に、前回のログインがlogged_out_at未記録のまま
    (タブを閉じた・セッションタイムアウトなどで/logoutを踏まずに終わった)残っていれば、
    そのユーザーの最終アクセス時刻(user_presence.last_active_at、閲覧のたびに更新される)
    を使って締める。この時点ではまだ今回のログイン分のpresenceは記録されていないため、
    ここで読める値は前回セッション中に最後に観測された時刻そのものになる。"""
    with conn:
        last_active_row = conn.execute(
            "SELECT last_active_at FROM user_presence WHERE user_id = ?", (user_id,)
        ).fetchone()
        last_active_at = last_active_row[0] if last_active_row else None
        if last_active_at:
            conn.execute(
                "UPDATE login_events SET logged_out_at = ? WHERE user_id = ? "
                "AND logged_out_at IS NULL AND logged_in_at < ?",
                (last_active_at, user_id, logged_in_at),
            )
        conn.execute(
            "INSERT INTO login_events (user_id, logged_in_at, ip) VALUES (?, ?, ?)",
            (user_id, logged_in_at, ip),
        )


def record_logout(conn, user_id, logged_out_at):
    """直近の(終了時刻未記録の)ログインに終了時刻を記録する。/logoutを踏んだ場合のみ
    呼ばれる(セッションタイムアウトやタブを閉じただけの場合は呼ばれず、終了時刻はNULLのまま)。"""
    with conn:
        conn.execute(
            "UPDATE login_events SET logged_out_at = ? WHERE id = ("
            "SELECT id FROM login_events WHERE user_id = ? AND logged_out_at IS NULL "
            "ORDER BY logged_in_at DESC LIMIT 1)",
            (logged_out_at, user_id),
        )


def has_logged_in_from_ip(conn, user_id, ip):
    """このユーザーが過去にこのIPからログインしたことがあるか(異常検知用)。
    ipがNoneの場合は判定不能として「既知扱い」にする(誤検知でアラートを出さないため)。"""
    if not ip:
        return True
    row = conn.execute(
        "SELECT 1 FROM login_events WHERE user_id = ? AND ip = ? LIMIT 1", (user_id, ip)
    ).fetchone()
    return row is not None


def login_history(conn, user_id, limit=50):
    """指定ユーザーの直近ログイン履歴を新しい順で返す（メンバーページの一覧表示用）。
    件数は際限なく増え続けるため、直近limit件だけに絞る。"""
    rows = conn.execute(
        "SELECT logged_in_at, logged_out_at FROM login_events WHERE user_id = ? "
        "ORDER BY logged_in_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [{"logged_in_at": r[0], "logged_out_at": r[1]} for r in rows]


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


def comment_counts_since(conn, since_str):
    """指定日時以降の地点コメント投稿数を1クエリでまとめて返す(月間ランキング用)。"""
    rows = conn.execute(
        "SELECT user_id, COUNT(*) FROM station_comments WHERE created_at >= ? GROUP BY user_id",
        (since_str,),
    ).fetchall()
    return {uid: c for uid, c in rows}


def message_counts_since(conn, since_str):
    """指定日時以降のDM・グループチャット送信数の合計を1クエリでまとめて返す
    (月間ランキング用)。"""
    rows = conn.execute(
        """
        SELECT sender_id, COUNT(*) FROM (
            SELECT sender_id, created_at FROM dm_messages
            UNION ALL
            SELECT sender_id, created_at FROM group_messages
        ) WHERE created_at >= ?
        GROUP BY sender_id
        """,
        (since_str,),
    ).fetchall()
    return {uid: c for uid, c in rows}


def won_bet_counts_since(conn, since_str):
    """指定日時以降に決着した川霧ギャンブルの勝利数を1クエリでまとめて返す
    (月間ランキング用。的中したタイミングであるresolved_atで絞る)。"""
    rows = conn.execute(
        "SELECT user_id, COUNT(*) FROM fog_bets WHERE status = 'won' AND resolved_at >= ? GROUP BY user_id",
        (since_str,),
    ).fetchall()
    return {uid: c for uid, c in rows}


def points_earned_since(conn, since_str):
    """指定日時以降にpoint_events経由で獲得したポイント合計を1クエリでまとめて返す
    (月間ランキング用。実績バッジ分は含まない)。"""
    rows = conn.execute(
        "SELECT user_id, SUM(amount) FROM point_events WHERE amount > 0 AND created_at >= ? GROUP BY user_id",
        (since_str,),
    ).fetchall()
    return {uid: c for uid, c in rows}


def login_counts_between(conn, since_str, until_str):
    """[since_str, until_str)の半開区間でのログイン回数を1クエリでまとめて返す
    ({user_id: count})。月間ランキング報酬のように「確定した1か月分」を
    集計する場合に使う(login_counts_sinceは期間の終わりを区切れないため)。"""
    rows = conn.execute(
        "SELECT user_id, COUNT(*) FROM login_events WHERE logged_in_at >= ? AND logged_in_at < ? "
        "GROUP BY user_id",
        (since_str, until_str),
    ).fetchall()
    return {uid: c for uid, c in rows}


def last_login(conn, user_id):
    row = conn.execute(
        "SELECT MAX(logged_in_at) FROM login_events WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row and row[0] else None


def first_login(conn, user_id):
    """初回ログイン日時。login_history()は表示件数を直近50件に絞っているため、
    そちらのhistory[-1]をそのまま「初回」として使うと、50回以上ログインしている
    ユーザーで実際の初回日時とズレる。MIN()で全期間から直接求める。"""
    row = conn.execute(
        "SELECT MIN(logged_in_at) FROM login_events WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row and row[0] else None


def last_logins_all(conn):
    """全ユーザーの最終ログイン日時を1クエリでまとめて返す({user_id: last_login})。"""
    rows = conn.execute("SELECT user_id, MAX(logged_in_at) FROM login_events GROUP BY user_id").fetchall()
    return {uid: v for uid, v in rows}


def touch_presence(conn, user_id, at):
    """オンライン判定用の最終アクセス時刻を記録する(呼び出し側で更新頻度を
    間引いていること前提。webapp.py の _touch_presence_if_due 参照)。"""
    with conn:
        conn.execute(
            "INSERT INTO user_presence (user_id, last_active_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_active_at = excluded.last_active_at",
            (user_id, at),
        )


def last_active(conn, user_id):
    row = conn.execute(
        "SELECT last_active_at FROM user_presence WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row else None


def last_active_all(conn):
    """全ユーザーの最終アクセス日時を1クエリでまとめて返す({user_id: last_active_at})。"""
    rows = conn.execute("SELECT user_id, last_active_at FROM user_presence").fetchall()
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


def list_audit_log_since(conn, since_str):
    """指定日時以降の監査ログを古い順で返す(週次ダイジェスト通知用)。"""
    rows = conn.execute(
        "SELECT id, actor_id, actor_name, action, detail, created_at FROM audit_log "
        "WHERE created_at >= ? ORDER BY created_at",
        (since_str,),
    ).fetchall()
    return [
        {"id": r[0], "actor_id": r[1], "actor_name": r[2], "action": r[3], "detail": r[4], "created_at": r[5]}
        for r in rows
    ]


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
    新規付与ならTrue、既に付与済みならFalseを返す。

    注意: 同じ(user_id, source)には生涯で1回しか付与できない(スキーマレベルでの保証)。
    呼び出し元がsourceに日付やレコードidのような一意な値を含めずに固定文字列を使うと、
    本来は繰り返し発生してよいはずのイベントでも2回目以降は静かに付与されなくなる
    (エラーにはならず、cur.rowcount == 0でFalseが返るだけ)。新しい付与経路を追加する際は
    sourceの一意性を必ず確認すること(2026-09-13の監査で指摘)。"""
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
    """フレーム交換(user_frames)・称号や名前の色等の装備品(user_cosmetics)・
    ギャンブルの消費アイテム購入(consumable_purchases)で使った分と、川霧ギャンブルで
    賭けた分(fog_bets、勝敗に関わらず賭けた時点で「使った」扱いにする。勝った場合の
    払い戻しはgrant_points経由でtotal_earned_points側に加算されるので二重計上には
    ならない)の合計。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost), 0) FROM user_frames WHERE user_id = ?", (user_id,)
    ).fetchone()
    row2 = conn.execute(
        "SELECT COALESCE(SUM(bet_amount), 0) FROM fog_bets WHERE user_id = ?", (user_id,)
    ).fetchone()
    row3 = conn.execute(
        "SELECT COALESCE(SUM(cost), 0) FROM user_cosmetics WHERE user_id = ?", (user_id,)
    ).fetchone()
    row4 = conn.execute(
        "SELECT COALESCE(SUM(cost), 0) FROM consumable_purchases WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] + row2[0] + row3[0] + row4[0]


def messages_sent_count(conn, user_id):
    """DM・グループチャット両方の送信件数の合計(実績「発言量」系で使う)。"""
    row1 = conn.execute("SELECT COUNT(*) FROM dm_messages WHERE sender_id = ?", (user_id,)).fetchone()
    row2 = conn.execute("SELECT COUNT(*) FROM group_messages WHERE sender_id = ?", (user_id,)).fetchone()
    return row1[0] + row2[0]


def reactions_given_count(conn, user_id):
    row1 = conn.execute(
        "SELECT COUNT(*) FROM dm_message_reactions WHERE user_id = ?", (user_id,)
    ).fetchone()
    row2 = conn.execute(
        "SELECT COUNT(*) FROM group_message_reactions WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row1[0] + row2[0]


def reactions_received_count(conn, user_id):
    row1 = conn.execute(
        "SELECT COUNT(*) FROM dm_message_reactions r JOIN dm_messages m ON r.message_id = m.id "
        "WHERE m.sender_id = ?", (user_id,),
    ).fetchone()
    row2 = conn.execute(
        "SELECT COUNT(*) FROM group_message_reactions r JOIN group_messages m ON r.message_id = m.id "
        "WHERE m.sender_id = ?", (user_id,),
    ).fetchone()
    return row1[0] + row2[0]


def mentions_received_count(conn, user_id):
    row1 = conn.execute(
        "SELECT COUNT(*) FROM dm_message_mentions WHERE user_id = ?", (user_id,)
    ).fetchone()
    row2 = conn.execute(
        "SELECT COUNT(*) FROM group_message_mentions WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row1[0] + row2[0]


def station_share_count(conn, user_id):
    """station_share()がcard_type='station'で送るメッセージの件数(DM・グループ合算)。"""
    row1 = conn.execute(
        "SELECT COUNT(*) FROM dm_messages WHERE sender_id = ? AND card_type = 'station'", (user_id,)
    ).fetchone()
    row2 = conn.execute(
        "SELECT COUNT(*) FROM group_messages WHERE sender_id = ? AND card_type = 'station'", (user_id,)
    ).fetchone()
    return row1[0] + row2[0]


def shared_file_upload_count(conn, user_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM shared_files WHERE uploaded_by = ?", (user_id,)
    ).fetchone()
    return row[0]


def shared_file_star_count(conn, user_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM shared_file_stars WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0]


def phenom_quiz_correct_count(conn, user_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM daily_phenom_quiz WHERE user_id = ? AND correct = 1", (user_id,)
    ).fetchone()
    return row[0]


def lottery_draw_count(conn, user_id):
    """デイリーくじはpoint_events(source='daily_lottery:YYYY-MM-DD')のUNIQUE制約で
    1日1回しか記録されないため、件数がそのまま「くじを引いた日数」になる。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM point_events WHERE user_id = ? AND source LIKE 'daily_lottery:%'",
        (user_id,),
    ).fetchone()
    return row[0]


def monthly_ranking_first_count(conn, user_id):
    """月間ログインランキングで1位を取った回数(_award_monthly_ranking_rewardsが
    source='monthly_login_ranking_<YYYY-MM>_1'でgrant_pointsするのを利用する)。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM point_events WHERE user_id = ? AND source LIKE 'monthly_login_ranking_%\\_1' ESCAPE '\\'",
        (user_id,),
    ).fetchone()
    return row[0]


def place_fog_bet(conn, user_id, station_name, precision, window_start, window_end, bet_amount, multiplier,
                   placed_at, insurance=False):
    with conn:
        cur = conn.execute(
            "INSERT INTO fog_bets "
            "(user_id, station_name, precision, window_start, window_end, bet_amount, multiplier, placed_at, "
            "insurance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, station_name, precision, window_start, window_end, bet_amount, multiplier, placed_at,
             1 if insurance else 0),
        )
        return cur.lastrowid


def list_pending_fog_bets(conn, up_to):
    """window_endが過ぎた(=答え合わせに必要な観測データが出揃っているはずの)未決済の
    賭けを返す。"""
    rows = conn.execute(
        "SELECT id, user_id, station_name, window_start, window_end, bet_amount, multiplier, insurance "
        "FROM fog_bets WHERE status = 'pending' AND window_end <= ?",
        (up_to,),
    ).fetchall()
    return [
        {
            "id": r[0], "user_id": r[1], "station_name": r[2], "window_start": r[3],
            "window_end": r[4], "bet_amount": r[5], "multiplier": r[6], "insurance": bool(r[7]),
        }
        for r in rows
    ]


def resolve_fog_bet(conn, bet_id, status, resolved_at, payout):
    with conn:
        conn.execute(
            "UPDATE fog_bets SET status = ?, resolved_at = ?, payout = ? WHERE id = ?",
            (status, resolved_at, payout, bet_id),
        )


def get_fog_bet(conn, bet_id):
    row = conn.execute(
        "SELECT id, user_id, station_name, window_start, status, bet_amount FROM fog_bets WHERE id = ?", (bet_id,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "user_id": row[1], "station_name": row[2], "window_start": row[3], "status": row[4],
            "bet_amount": row[5]}


def cancel_fog_bet(conn, bet_id, cancelled_at, refund):
    """見送り券で賭けを取り消す時に使う。resolve_fog_betと同じUPDATEだが、statusに
    'cancelled'を使う点だけが違う(_resolve_pending_fog_betsはstatus='pending'しか
    見ないため、cancelled行は以後の答え合わせ対象から自然に外れる)。"""
    with conn:
        conn.execute(
            "UPDATE fog_bets SET status = 'cancelled', resolved_at = ?, payout = ? WHERE id = ?",
            (cancelled_at, refund, bet_id),
        )


def won_fog_bet_count(conn, user_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM fog_bets WHERE user_id = ? AND status = 'won'", (user_id,)
    ).fetchone()
    return row[0]


def list_fog_bets_for_user(conn, user_id, limit=30):
    rows = conn.execute(
        "SELECT id, station_name, precision, window_start, window_end, bet_amount, multiplier, "
        "status, resolved_at, payout, placed_at, insurance FROM fog_bets WHERE user_id = ? "
        "ORDER BY placed_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [
        {
            "id": r[0], "station_name": r[1], "precision": r[2], "window_start": r[3],
            "window_end": r[4], "bet_amount": r[5], "multiplier": r[6], "status": r[7],
            "resolved_at": r[8], "payout": r[9], "placed_at": r[10], "insurance": bool(r[11]),
        }
        for r in rows
    ]


def available_points(conn, user_id):
    """実績で獲得した合計ポイントから、フレーム交換で使った分を差し引いた残高。"""
    return total_earned_points(conn, user_id) - total_spent_points(conn, user_id)


def list_all_user_point_balances(conn):
    """全ユーザーの所持ポイント残高({user_id: balance})を1クエリでまとめて返す。
    available_points()を全ユーザー分N回呼ぶと管理者ポイント一覧ページが重くなるため、
    total_earned_points/total_spent_pointsと同じ集計をSQL側で一括して行う。"""
    rows = conn.execute(
        """
        SELECT u.id,
               COALESCE(ba.total, 0) + COALESCE(pe.total, 0)
               - COALESCE(uf.total, 0) - COALESCE(fb.total, 0)
               - COALESCE(uc.total, 0) - COALESCE(cp.total, 0) AS balance
        FROM users u
        LEFT JOIN (SELECT user_id, SUM(points) AS total FROM badge_awards GROUP BY user_id) ba
            ON ba.user_id = u.id
        LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM point_events GROUP BY user_id) pe
            ON pe.user_id = u.id
        LEFT JOIN (SELECT user_id, SUM(cost) AS total FROM user_frames GROUP BY user_id) uf
            ON uf.user_id = u.id
        LEFT JOIN (SELECT user_id, SUM(bet_amount) AS total FROM fog_bets GROUP BY user_id) fb
            ON fb.user_id = u.id
        LEFT JOIN (SELECT user_id, SUM(cost) AS total FROM user_cosmetics GROUP BY user_id) uc
            ON uc.user_id = u.id
        LEFT JOIN (SELECT user_id, SUM(cost) AS total FROM consumable_purchases GROUP BY user_id) cp
            ON cp.user_id = u.id
        """
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def list_all_user_gem_balances(conn):
    """全ユーザーの所持ジェム残高({user_id: balance})を1クエリでまとめて返す。
    ジェムはgem_events.amountの合計だけが残高になる(list_all_user_point_balances参照、
    ポイントと違い別テーブル合算が不要なぶんシンプル)。"""
    rows = conn.execute(
        """
        SELECT u.id, COALESCE(ge.total, 0) AS balance
        FROM users u
        LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM gem_events GROUP BY user_id) ge
            ON ge.user_id = u.id
        """
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def spend_points_if_available(conn, user_id, cost, do_spend):
    """残高チェックと消費(do_spendが行うINSERT/UPDATE)をBEGIN IMMEDIATEで1つの
    トランザクションに収めてアトミックに行う。残高は都度SUMで再計算する設計のため、
    「チェック時点では足りていたが、ほぼ同時に来た別のリクエストが先に使ってしまい
    実際は残高不足だった」という競合(ポイントのマイナス化・想定外の二重購入)を、
    SQLiteの書き込みロックで直列化することで防ぐ。残高が足りなければdo_spendを
    呼ばずにFalseを返す。do_spend内で使う個別の関数(purchase_frame等)が自前で
    `with conn:`を使っていても、既にトランザクションが開始済みのためそのまま
    正しく収まる(with conn:の終了時にこの外側のトランザクションがcommitされる)。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        balance = total_earned_points(conn, user_id) - total_spent_points(conn, user_id)
        if balance < cost:
            conn.execute("ROLLBACK")
            return False
        do_spend(conn)
        if conn.in_transaction:
            conn.commit()
        return True
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


# --- ジェム(観測報告専用の第二通貨) ------------------------------------------------
# ポイントと完全に別会計にする(交換不可)。ポイントのuser_frames/user_cosmetics.costの
# ような別テーブル合算方式は使わず、gem_events.amountの符号(付与は正・消費は負)だけで
# 残高を表す、よりシンプルな1テーブル方式にしている(ジェムは観測報告の報酬という
# 単一の獲得経路しか持たないため、ポイントほど複雑な集計は不要)。
def grant_gems(conn, user_id, source, amount, created_at):
    """UNIQUE(user_id, source)制約により、同じsource文字列への二重付与(・二重消費)を
    防ぐ。新規付与/消費ならTrue、既に同じsourceで記録済みならFalseを返す。

    注意: grant_pointsと同じ契約。同じ(user_id, source)には生涯で1回しか付与できない。
    呼び出し元がsourceに日付やレコードidのような一意な値を含めずに固定文字列を使うと、
    本来は繰り返し発生してよいはずのイベントでも2回目以降は静かに付与されなくなる
    (エラーにはならず、cur.rowcount == 0でFalseが返るだけ)。新しい付与経路を追加する際は
    sourceの一意性を必ず確認すること(2026-09-13の監査で指摘)。"""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO gem_events (user_id, source, amount, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, source, amount, created_at),
        )
        return cur.rowcount > 0


def available_gems(conn, user_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM gem_events WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0]


def spend_gems_if_available(conn, user_id, cost, do_spend):
    """spend_points_if_availableのジェム版。BEGIN IMMEDIATEで残高チェックと消費を
    アトミックに行う。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if available_gems(conn, user_id) < cost:
            conn.execute("ROLLBACK")
            return False
        do_spend(conn)
        if conn.in_transaction:
            conn.commit()
        return True
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def owned_gem_item_ids(conn, user_id):
    rows = conn.execute(
        "SELECT item_id FROM user_gem_items WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r[0] for r in rows}


def purchase_gem_item(conn, user_id, item_id, cost_gems, acquired_at):
    """ジェム残高が足りているかは呼び出し側(spend_gems_if_available)で確認済みの
    前提。既に所持しているアイテムの場合はUNIQUE制約により何もしない。available_gems()
    はgem_events.amountの合計だけを見るため、所持記録(user_gem_items)を作るだけでは
    残高が減らない。gem_events側にも同額のマイナスを記録して初めて消費が反映される。"""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO user_gem_items (user_id, item_id, cost_gems, acquired_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, item_id, cost_gems, acquired_at),
        )
        if cur.rowcount > 0:
            conn.execute(
                "INSERT OR IGNORE INTO gem_events (user_id, source, amount, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, f"gem_item_purchase:{item_id}", -cost_gems, acquired_at),
            )


def set_equipped_gem_item(conn, user_id, item_id):
    """item_idに空文字を渡すと「未装備」の状態に戻せる。"""
    with conn:
        conn.execute("UPDATE users SET equipped_gem_item = ? WHERE id = ?", (item_id or "", user_id))


def add_phenomena_correction(conn, station_name, target_datetime, original_code, corrected_code, user_id, at, source="human"):
    """カメラ画像の現象判定(Gemini)を訂正した記録を残す。同じ(地点, 日時)を
    再度訂正した場合は上書きする(訂正の訂正はよくあることなので、履歴は持たず
    最新の1件だけを残す)。user_idはNone(自動異常検知による訂正)を許容する。
    original_code/corrected_codeは、2コード同時観測(2026-09対応)の場合は
    fog_features.encode_phenom_codesでパックした値(INTEGER列のままでスキーマ変更不要)。"""
    with conn:
        conn.execute(
            "INSERT INTO phenomena_corrections "
            "(station_name, target_datetime, original_code, corrected_code, corrected_by, corrected_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(station_name, target_datetime) DO UPDATE SET "
            "corrected_code = excluded.corrected_code, corrected_by = excluded.corrected_by, "
            "corrected_at = excluded.corrected_at, source = excluded.source",
            (station_name, target_datetime, original_code, corrected_code, user_id, at, source),
        )


def get_phenomena_corrections_for_datetime(conn, target_datetime):
    """指定日時の全地点分の訂正済みコードを返す({地点名: 訂正後コード})。"""
    rows = conn.execute(
        "SELECT station_name, corrected_code FROM phenomena_corrections WHERE target_datetime = ?",
        (target_datetime,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def phenomena_correction_counts_by_station(conn):
    """地点別の訂正件数を多い順で返す(画像判定の誤りやすい地点を把握するため)。"""
    rows = conn.execute(
        "SELECT station_name, COUNT(*) FROM phenomena_corrections GROUP BY station_name "
        "ORDER BY COUNT(*) DESC"
    ).fetchall()
    return [{"station": s, "count": c} for s, c in rows]


def create_observation_report(conn, station_name, target_datetime, reported_code, reported_by, reported_at,
                               source="user", importance="medium", reason=None):
    """観測が間違っていると思う場合の報告を1件記録する。同じ(地点, 日時)への保留中の
    報告が既にあれば新規作成しない(idx_observation_reports_pending_uniqueにより
    INSERT OR IGNOREが安全に効く)。挿入できたら新しいreport_idを、既に保留中の報告が
    あればNoneを返す(呼び出し側はNoneかどうかで成否を判定する)。
    reason: 報告者が「なぜこの現象だと思ったか」を書いた自由記述(自動検知分はNone)。
    reported_codeは、2コード同時観測(2026-09対応)の場合はfog_features.encode_phenom_codes
    でパックした値(INTEGER列のままでスキーマ変更不要)。"""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO observation_reports "
            "(station_name, target_datetime, reported_code, reported_by, reported_at, source, status, importance, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (station_name, target_datetime, reported_code, reported_by, reported_at, source, importance, reason),
        )
        return cur.lastrowid if cur.rowcount > 0 else None


def list_pending_observation_reports(conn):
    """保留中の観測報告を、報告者名を添えて重要度が高い順(同じ重要度なら新しい順)で返す
    (管理者の確認キュー用)。"""
    rows = conn.execute(
        """
        SELECT r.id, r.station_name, r.target_datetime, r.reported_code, r.reported_by, r.reported_at,
               r.source, u.name AS reporter_name, r.importance, r.reason
        FROM observation_reports r
        LEFT JOIN users u ON u.id = r.reported_by
        WHERE r.status = 'pending'
        ORDER BY CASE r.importance WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, r.reported_at DESC
        """
    ).fetchall()
    return [
        {
            "id": r[0], "station_name": r[1], "target_datetime": r[2], "reported_code": r[3],
            "reported_by": r[4], "reported_at": r[5], "source": r[6],
            "reporter_name": (
                "(自動検知)" if r[6] == "auto_detected" else (r[7] or "(退会済みユーザー)")
            ),
            "importance": r[8],
            "reason": r[9],
        }
        for r in rows
    ]


def pending_observation_report_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM observation_reports WHERE status = 'pending'"
    ).fetchone()[0]


def pending_observation_report_count_for_stations(conn, station_names):
    """会津観測者/只見観測者が担当地域分だけのバッジ件数を見られるようにするための、
    地点名リストで絞り込んだ版。station_namesが空なら0を返す。"""
    if not station_names:
        return 0
    placeholders = ",".join("?" for _ in station_names)
    return conn.execute(
        f"SELECT COUNT(*) FROM observation_reports WHERE status = 'pending' "
        f"AND station_name IN ({placeholders})",
        station_names,
    ).fetchone()[0]


def get_observation_report(conn, report_id):
    row = conn.execute(
        "SELECT id, station_name, target_datetime, reported_code, status, reported_by, source, importance "
        "FROM observation_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "station_name": row[1], "target_datetime": row[2],
        "reported_code": row[3], "status": row[4], "reported_by": row[5], "source": row[6],
        "importance": row[7],
    }


def resolve_observation_report(conn, report_id, status, resolved_by, resolved_at, note):
    with conn:
        conn.execute(
            "UPDATE observation_reports SET status = ?, resolved_by = ?, resolved_at = ?, resolution_note = ? "
            "WHERE id = ?",
            (status, resolved_by, resolved_at, note, report_id),
        )


def create_feedback_report(conn, user_id, body, gemini_summary, created_at):
    """コミュニティの意見箱への投稿を1件記録する。gemini_summaryはNoneでもよい
    (要約生成に失敗した場合、管理画面では原文だけを表示する)。"""
    with conn:
        cur = conn.execute(
            "INSERT INTO feedback_reports (user_id, body, gemini_summary, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, body, gemini_summary, created_at),
        )
        return cur.lastrowid


def list_feedback_reports_by_user(conn, user_id, limit=50):
    """自分が過去に投稿した意見箱の内容を新しい順で返す(投稿ページの一覧表示用)。"""
    rows = conn.execute(
        "SELECT id, body, gemini_summary, created_at, status FROM feedback_reports "
        "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [
        {"id": r[0], "body": r[1], "gemini_summary": r[2], "created_at": r[3], "status": r[4]}
        for r in rows
    ]


def list_pending_feedback_reports(conn):
    """未対応の意見箱投稿を、投稿者名を添えて新しい順で返す(管理者の確認キュー用)。"""
    rows = conn.execute(
        """
        SELECT f.id, f.body, f.gemini_summary, f.created_at, u.name
        FROM feedback_reports f
        LEFT JOIN users u ON u.id = f.user_id
        WHERE f.status = 'pending'
        ORDER BY f.created_at DESC
        """
    ).fetchall()
    return [
        {
            "id": r[0], "body": r[1], "gemini_summary": r[2], "created_at": r[3],
            "reporter_name": r[4] or "(退会済みユーザー)",
        }
        for r in rows
    ]


def pending_feedback_report_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM feedback_reports WHERE status = 'pending'"
    ).fetchone()[0]


def get_feedback_report(conn, report_id):
    row = conn.execute(
        "SELECT id, status FROM feedback_reports WHERE id = ?", (report_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "status": row[1]}


def resolve_feedback_report(conn, report_id, resolved_by, resolved_at):
    with conn:
        conn.execute(
            "UPDATE feedback_reports SET status = 'reviewed', resolved_by = ?, resolved_at = ? WHERE id = ?",
            (resolved_by, resolved_at, report_id),
        )


def owned_frame_ids(conn, user_id):
    rows = conn.execute(
        "SELECT frame_id FROM user_frames WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r[0] for r in rows}


def purchase_frame(conn, user_id, frame_id, cost, acquired_at, next_billing_at):
    """ポイント残高が足りているかは呼び出し側で確認してから使うこと。既に所持している
    フレームの場合はUNIQUE制約により何もしない(二重購入・二重課金の防止)。next_billing_at
    は購入時点で最初の1ヶ月分を含めた上での次回請求日(購入日+1ヶ月)を呼び出し側で計算して渡す。"""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_frames (user_id, frame_id, cost, acquired_at, next_billing_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, frame_id, cost, acquired_at, next_billing_at),
        )


def set_equipped_frame(conn, user_id, frame_id):
    """frame_idに空文字を渡すと「フレームなし」の状態に戻せる。"""
    with conn:
        conn.execute("UPDATE users SET equipped_frame = ? WHERE id = ?", (frame_id or "", user_id))


def frame_billing_info(conn, user_id, frame_id):
    row = conn.execute(
        "SELECT cost, next_billing_at FROM user_frames WHERE user_id = ? AND frame_id = ?",
        (user_id, frame_id),
    ).fetchone()
    return {"cost": row[0], "next_billing_at": row[1]} if row else None


def set_frame_next_billing(conn, user_id, frame_id, next_billing_at):
    with conn:
        conn.execute(
            "UPDATE user_frames SET next_billing_at = ? WHERE user_id = ? AND frame_id = ?",
            (next_billing_at, user_id, frame_id),
        )


def list_equipped_frames_due(conn, as_of):
    """装備中(users.equipped_frame一致)かつ次回請求日を過ぎているフレームを全ユーザー分返す。
    月額使用料の定期徴収バッチ(未装着のものは請求しない)で使う。"""
    return conn.execute(
        "SELECT uf.user_id, uf.frame_id, uf.cost, uf.next_billing_at "
        "FROM user_frames uf JOIN users u ON u.id = uf.user_id AND u.equipped_frame = uf.frame_id "
        "WHERE uf.next_billing_at IS NOT NULL AND uf.next_billing_at <= ?",
        (as_of,),
    ).fetchall()


# --- 称号・名前の色・ダッシュボード配色・演出(まとめてuser_cosmeticsで管理) ------------
# フレームと違い、これらは装備先(usersテーブルの列)が4通りあるだけで購入・所持の
# 仕組み自体は共通なので、種類ごとにuser_frames相当のテーブルを4つ作らず、
# category列で区別する1つのテーブルにまとめている。
_COSMETIC_EQUIP_COLUMNS = {
    "title": "equipped_title",
    "name_color": "equipped_name_color",
    "dashboard_theme": "equipped_dashboard_theme",
    "effect": "equipped_effect",
    "banner": "equipped_banner",
    "dm_theme": "equipped_dm_theme",
    "title_color": "equipped_title_color",
    "marker": "equipped_marker",
    "title_badge": "equipped_title_badge",
    "bubble_style": "equipped_bubble_style",
    "avatar_bg": "equipped_avatar_bg",
    "rank_style": "equipped_rank_style",
    "profile_sound": "equipped_profile_sound",
    "seasonal": "equipped_seasonal",
}


def owned_cosmetic_ids(conn, user_id, category):
    rows = conn.execute(
        "SELECT item_id FROM user_cosmetics WHERE user_id = ? AND category = ?", (user_id, category)
    ).fetchall()
    return {r[0] for r in rows}


def purchase_cosmetic(conn, user_id, category, item_id, cost, acquired_at, next_billing_at):
    """ポイント残高が足りているかは呼び出し側で確認してから使うこと。既に所持している
    アイテムの場合はUNIQUE制約により何もしない(二重購入・二重課金の防止)。next_billing_at
    は購入時点で最初の1ヶ月分を含めた上での次回請求日(購入日+1ヶ月)を呼び出し側で計算して渡す。

    このため、呼び出しは必ずspend_points_if_available()のdo_spendコールバック経由にし、
    既に開始済みのトランザクション(BEGIN IMMEDIATE)の中で呼ぶこと。それ以外の経路で
    直接呼ぶと、残高チェックと消費の間に競合が起き、ポイントがマイナスになったり二重に
    購入できてしまう(2026-10-23の監査で、将来の呼び出し元が踏み抜きやすい点として指摘)。
    踏み抜きに早く気付けるよう、トランザクションの外からの呼び出しはここで弾く。"""
    if not conn.in_transaction:
        raise RuntimeError(
            "purchase_cosmetic() must be called inside an existing transaction "
            "(route it through spend_points_if_available()'s do_spend callback)"
        )
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_cosmetics (user_id, category, item_id, cost, acquired_at, next_billing_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, category, item_id, cost, acquired_at, next_billing_at),
        )


def set_equipped_cosmetic(conn, user_id, category, item_id):
    """item_idに空文字を渡すと「未装備」の状態に戻せる。categoryは_COSMETIC_EQUIP_COLUMNSの
    キーである必要がある(SQL列名を組み立てる前に検証し、任意の列名注入を防ぐ)。"""
    column = _COSMETIC_EQUIP_COLUMNS.get(category)
    if column is None:
        raise ValueError(f"unknown cosmetic category: {category}")
    with conn:
        conn.execute(f"UPDATE users SET {column} = ? WHERE id = ?", (item_id or "", user_id))


def cosmetic_billing_info(conn, user_id, category, item_id):
    row = conn.execute(
        "SELECT cost, next_billing_at FROM user_cosmetics WHERE user_id = ? AND category = ? AND item_id = ?",
        (user_id, category, item_id),
    ).fetchone()
    return {"cost": row[0], "next_billing_at": row[1]} if row else None


def set_cosmetic_next_billing(conn, user_id, category, item_id, next_billing_at):
    with conn:
        conn.execute(
            "UPDATE user_cosmetics SET next_billing_at = ? WHERE user_id = ? AND category = ? AND item_id = ?",
            (next_billing_at, user_id, category, item_id),
        )


def list_equipped_cosmetics_due(conn, as_of):
    """装備中(users.equipped_*一致)かつ次回請求日を過ぎているコスメを、全カテゴリ・
    全ユーザー分まとめて返す。各要素は(user_id, category, item_id, cost, next_billing_at)。"""
    due = []
    for category, column in _COSMETIC_EQUIP_COLUMNS.items():
        rows = conn.execute(
            f"SELECT uc.user_id, uc.item_id, uc.cost, uc.next_billing_at "
            f"FROM user_cosmetics uc JOIN users u ON u.id = uc.user_id AND u.{column} = uc.item_id "
            f"WHERE uc.category = ? AND uc.next_billing_at IS NOT NULL AND uc.next_billing_at <= ?",
            (category, as_of),
        ).fetchall()
        due.extend((user_id, category, item_id, cost, next_billing_at)
                   for user_id, item_id, cost, next_billing_at in rows)
    return due


# --- 川霧ギャンブルの消費アイテム(保険チケット・倍率ブーストチケット) ------------------
# フレーム・称号等の装備品と違い、買った分だけ在庫が増え、使うたびに1個減る消費型アイテム。
def buy_consumable(conn, user_id, item_id, cost, purchased_at):
    with conn:
        conn.execute(
            "INSERT INTO user_consumables (user_id, item_id, quantity) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, item_id) DO UPDATE SET quantity = quantity + 1",
            (user_id, item_id),
        )
        conn.execute(
            "INSERT INTO consumable_purchases (user_id, item_id, cost, purchased_at) VALUES (?, ?, ?, ?)",
            (user_id, item_id, cost, purchased_at),
        )


def consumable_quantities(conn, user_id):
    rows = conn.execute(
        "SELECT item_id, quantity FROM user_consumables WHERE user_id = ? AND quantity > 0", (user_id,)
    ).fetchall()
    return {item_id: q for item_id, q in rows}


def use_consumable(conn, user_id, item_id):
    """在庫が1個以上あれば1個消費してTrueを返す。在庫が無ければ何もせずFalseを返す。"""
    with conn:
        cur = conn.execute(
            "UPDATE user_consumables SET quantity = quantity - 1 "
            "WHERE user_id = ? AND item_id = ? AND quantity > 0",
            (user_id, item_id),
        )
        return cur.rowcount > 0


# --- デイリー川霧予報クイズ(無料・スキル型) -----------------------------------------
def place_daily_fog_quiz_guess(conn, user_id, quiz_date, station_name, guess, created_at):
    """同じ(user_id, quiz_date)への2回目以降の投稿はPRIMARY KEY制約により無視される
    (1日1回のみ)。新規に記録できればTrue、既に今日の分を出していればFalseを返す。"""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO daily_fog_quiz (user_id, quiz_date, station_name, guess, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, quiz_date, station_name, 1 if guess else 0, created_at),
        )
        return cur.rowcount > 0


def get_daily_fog_quiz(conn, user_id, quiz_date):
    row = conn.execute(
        "SELECT station_name, guess, status, payout FROM daily_fog_quiz WHERE user_id = ? AND quiz_date = ?",
        (user_id, quiz_date),
    ).fetchone()
    if row is None:
        return None
    return {"station_name": row[0], "guess": bool(row[1]), "status": row[2], "payout": row[3]}


def list_pending_daily_fog_quiz(conn, before_date):
    """quiz_date(観測当日)が既に終わっている(before_dateより前)未解決分を返す。"""
    rows = conn.execute(
        "SELECT user_id, quiz_date, station_name, guess FROM daily_fog_quiz "
        "WHERE status = 'pending' AND quiz_date < ?",
        (before_date,),
    ).fetchall()
    return [{"user_id": r[0], "quiz_date": r[1], "station_name": r[2], "guess": bool(r[3])} for r in rows]


def resolve_daily_fog_quiz(conn, user_id, quiz_date, status, resolved_at, payout):
    with conn:
        conn.execute(
            "UPDATE daily_fog_quiz SET status = ?, resolved_at = ?, payout = ? "
            "WHERE user_id = ? AND quiz_date = ?",
            (status, resolved_at, payout, user_id, quiz_date),
        )


def get_daily_fog_quiz_stats(conn, user_id):
    row = conn.execute(
        "SELECT current_streak, best_streak, total_correct, total_played "
        "FROM daily_fog_quiz_stats WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return {"current_streak": 0, "best_streak": 0, "total_correct": 0, "total_played": 0}
    return {"current_streak": row[0], "best_streak": row[1], "total_correct": row[2], "total_played": row[3]}


def update_daily_fog_quiz_stats(conn, user_id, correct):
    """的中ならcurrent_streakを+1、外れなら0に戻す。更新後のcurrent_streakを返す
    (呼び出し側でストリークボーナスの計算に使う)。"""
    stats = get_daily_fog_quiz_stats(conn, user_id)
    new_streak = stats["current_streak"] + 1 if correct else 0
    best = max(stats["best_streak"], new_streak)
    total_correct = stats["total_correct"] + (1 if correct else 0)
    total_played = stats["total_played"] + 1
    with conn:
        conn.execute(
            "INSERT INTO daily_fog_quiz_stats "
            "(user_id, current_streak, best_streak, total_correct, total_played) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "current_streak = excluded.current_streak, best_streak = excluded.best_streak, "
            "total_correct = excluded.total_correct, total_played = excluded.total_played",
            (user_id, new_streak, best, total_correct, total_played),
        )
    return new_streak


# --- デイリー現象当てクイズ(無料、即時採点) ---------------------------------------
def place_daily_phenom_quiz_answer(conn, user_id, quiz_date, category_guess, correct, created_at):
    """1日1回のみ(PRIMARY KEY制約)。新規に記録できればTrue、既に回答済みならFalse。"""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO daily_phenom_quiz (user_id, quiz_date, category_guess, correct, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, quiz_date, category_guess, 1 if correct else 0, created_at),
        )
        return cur.rowcount > 0


def get_daily_phenom_quiz(conn, user_id, quiz_date):
    row = conn.execute(
        "SELECT category_guess, correct FROM daily_phenom_quiz WHERE user_id = ? AND quiz_date = ?",
        (user_id, quiz_date),
    ).fetchone()
    if row is None:
        return None
    return {"category_guess": row[0], "correct": bool(row[1])}


# --- デイリーくじ ------------------------------------------------------------------
# 専用テーブルは持たず、grant_points()のpoint_events(UNIQUE(user_id, source))を
# そのまま「1日1回」の判定に使う(source="daily_lottery:YYYY-MM-DD")。
def get_point_event_amount(conn, user_id, source):
    """指定sourceで既に付与済みなら、その時の金額を返す(無ければNone)。
    デイリーくじの「今日はもう引いた」表示に、実際に出た金額を再表示するために使う。"""
    row = conn.execute(
        "SELECT amount FROM point_events WHERE user_id = ? AND source = ?", (user_id, source)
    ).fetchone()
    return row[0] if row else None


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


def send_dm_message(conn, conversation_id, sender_id, body, created_at, reply_to_id=None,
                     card_type=None, card_data=None):
    with conn:
        cur = conn.execute(
            "INSERT INTO dm_messages (conversation_id, sender_id, body, created_at, reply_to_id, "
            "card_type, card_data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, sender_id, body, created_at, reply_to_id, card_type, card_data),
        )
        conn.execute(
            "UPDATE dm_conversations SET last_message_at = ? WHERE id = ?", (created_at, conversation_id),
        )
        return cur.lastrowid


def list_dm_messages(conn, conversation_id, after_id=0):
    """after_idより新しいメッセージだけを返す(0なら全件。スレッド表示の初回取得と
    ポーリングでの差分取得の両方に使う)。"""
    rows = conn.execute(
        "SELECT id, sender_id, body, created_at, reply_to_id, edited_at, pinned, card_type, card_data "
        "FROM dm_messages WHERE conversation_id = ? AND id > ? ORDER BY id",
        (conversation_id, after_id),
    ).fetchall()
    return [
        {
            "id": r[0], "sender_id": r[1], "body": r[2], "created_at": r[3], "reply_to_id": r[4],
            "edited_at": r[5], "pinned": bool(r[6]), "card_type": r[7], "card_data": r[8],
        }
        for r in rows
    ]


def edit_dm_message(conn, message_id, body, edited_at):
    with conn:
        conn.execute(
            "UPDATE dm_messages SET body = ?, edited_at = ? WHERE id = ?", (body, edited_at, message_id),
        )


def pin_dm_message(conn, message_id, pinned):
    with conn:
        conn.execute("UPDATE dm_messages SET pinned = ? WHERE id = ?", (1 if pinned else 0, message_id))


def list_pinned_dm_messages(conn, conversation_id):
    rows = conn.execute(
        "SELECT id, sender_id, body, created_at FROM dm_messages "
        "WHERE conversation_id = ? AND pinned = 1 ORDER BY id",
        (conversation_id,),
    ).fetchall()
    return [{"id": r[0], "sender_id": r[1], "body": r[2], "created_at": r[3]} for r in rows]


def search_dm_messages(conn, conversation_id, keyword, limit=50):
    rows = conn.execute(
        "SELECT id, sender_id, body, created_at FROM dm_messages "
        "WHERE conversation_id = ? AND body LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
        (conversation_id, f"%{_escape_like(keyword)}%", limit),
    ).fetchall()
    return [{"id": r[0], "sender_id": r[1], "body": r[2], "created_at": r[3]} for r in rows]


def get_dm_read_state(conn, conversation_id, user_id):
    row = conn.execute(
        "SELECT last_read_message_id FROM dm_read_state WHERE conversation_id = ? AND user_id = ?",
        (conversation_id, user_id),
    ).fetchone()
    return row[0] if row else 0


def add_dm_reaction(conn, message_id, user_id, emoji, created_at):
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO dm_message_reactions (message_id, user_id, emoji, created_at) "
            "VALUES (?, ?, ?, ?)",
            (message_id, user_id, emoji, created_at),
        )


def remove_dm_reaction(conn, message_id, user_id, emoji):
    with conn:
        conn.execute(
            "DELETE FROM dm_message_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
            (message_id, user_id, emoji),
        )


def get_dm_reactions_for_messages(conn, message_ids):
    """{message_id: [{"emoji":, "user_id":}, ...]} を返す(集計・自分の反応判定は
    呼び出し側で行う。get_dm_attachments_for_messagesと同じ、まとめ引きパターン)。"""
    ids = sorted(set(message_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT message_id, emoji, user_id FROM dm_message_reactions WHERE message_id IN ({placeholders})",
        ids,
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r[0], []).append({"emoji": r[1], "user_id": r[2]})
    return result


def record_dm_mentions(conn, message_id, user_ids):
    ids = sorted(set(user_ids))
    if not ids:
        return
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO dm_message_mentions (message_id, user_id) VALUES (?, ?)",
            [(message_id, uid) for uid in ids],
        )


def get_dm_mentions_for_messages(conn, message_ids):
    ids = sorted(set(message_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT message_id, user_id FROM dm_message_mentions WHERE message_id IN ({placeholders})",
        ids,
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r[0], []).append(r[1])
    return result


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


def mark_dm_unread(conn, conversation_id, user_id):
    """受信箱のクイック操作「未読にする」用。既読位置を0に戻す(会話全体が
    未読扱いになる。1件だけを未読に戻す繊細な制御はせず、単純な状態に倒す)。"""
    with conn:
        conn.execute(
            "INSERT INTO dm_read_state (conversation_id, user_id, last_read_message_id) VALUES (?, ?, 0) "
            "ON CONFLICT(conversation_id, user_id) DO UPDATE SET last_read_message_id = 0",
            (conversation_id, user_id),
        )


def toggle_pinned_conversation(conn, user_id, conversation_id):
    """会話(受信箱の1行)のピン留めをトグルする。ピン留め後の状態(True/False)を返す。"""
    with conn:
        cur = conn.execute(
            "DELETE FROM dm_pinned_conversations WHERE user_id = ? AND conversation_id = ?",
            (user_id, conversation_id),
        )
        if cur.rowcount > 0:
            return False
        conn.execute(
            "INSERT INTO dm_pinned_conversations (user_id, conversation_id) VALUES (?, ?)",
            (user_id, conversation_id),
        )
        return True


def pinned_conversation_ids(conn, user_id):
    rows = conn.execute(
        "SELECT conversation_id FROM dm_pinned_conversations WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r[0] for r in rows}


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
    (削除時に本人か会話の当事者かをその場で判定できるようにするため。
    bodyも合わせて返し、返信プレビュー表示に再利用できるようにしている)。"""
    row = conn.execute(
        """
        SELECT m.id, m.conversation_id, m.sender_id, c.user_a_id, c.user_b_id, m.body
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
        "user_a_id": row[3], "user_b_id": row[4], "body": row[5],
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


def count_users(conn):
    row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    return row[0] if row else 0


# --- 全員参加グループチャット ------------------------------------------------------
# dm_messages系の関数と対になる構造(会話IDが無い点だけが違う)。単一チャンネルのため
# 参加者管理・招待の概念自体を持たない(全登録ユーザーが暗黙のメンバー)。
def send_group_message(conn, sender_id, body, created_at, reply_to_id=None, card_type=None, card_data=None):
    with conn:
        cur = conn.execute(
            "INSERT INTO group_messages (sender_id, body, created_at, reply_to_id, card_type, card_data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sender_id, body, created_at, reply_to_id, card_type, card_data),
        )
        return cur.lastrowid


def list_group_messages(conn, after_id=0):
    rows = conn.execute(
        "SELECT id, sender_id, body, created_at, reply_to_id, edited_at, pinned, card_type, card_data "
        "FROM group_messages WHERE id > ? ORDER BY id",
        (after_id,),
    ).fetchall()
    return [
        {
            "id": r[0], "sender_id": r[1], "body": r[2], "created_at": r[3], "reply_to_id": r[4],
            "edited_at": r[5], "pinned": bool(r[6]), "card_type": r[7], "card_data": r[8],
        }
        for r in rows
    ]


def get_group_message(conn, message_id):
    row = conn.execute(
        "SELECT id, sender_id, body FROM group_messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "sender_id": row[1], "body": row[2]}


def edit_group_message(conn, message_id, body, edited_at):
    with conn:
        conn.execute(
            "UPDATE group_messages SET body = ?, edited_at = ? WHERE id = ?", (body, edited_at, message_id),
        )


def pin_group_message(conn, message_id, pinned):
    with conn:
        conn.execute("UPDATE group_messages SET pinned = ? WHERE id = ?", (1 if pinned else 0, message_id))


def list_pinned_group_messages(conn):
    rows = conn.execute(
        "SELECT id, sender_id, body, created_at FROM group_messages WHERE pinned = 1 ORDER BY id"
    ).fetchall()
    return [{"id": r[0], "sender_id": r[1], "body": r[2], "created_at": r[3]} for r in rows]


def search_group_messages(conn, keyword, limit=50):
    rows = conn.execute(
        "SELECT id, sender_id, body, created_at FROM group_messages "
        "WHERE body LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
        (f"%{_escape_like(keyword)}%", limit),
    ).fetchall()
    return [{"id": r[0], "sender_id": r[1], "body": r[2], "created_at": r[3]} for r in rows]


def get_group_message_attachment_stored_names(conn, message_id):
    rows = conn.execute(
        "SELECT stored_name FROM group_message_attachments WHERE message_id = ?", (message_id,)
    ).fetchall()
    return [r[0] for r in rows]


def delete_group_message(conn, message_id):
    with conn:
        conn.execute("DELETE FROM group_messages WHERE id = ?", (message_id,))


def add_group_attachment(conn, message_id, filename, stored_name, content_type, size_bytes, uploaded_at):
    with conn:
        cur = conn.execute(
            "INSERT INTO group_message_attachments "
            "(message_id, filename, stored_name, content_type, size_bytes, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, filename, stored_name, content_type, size_bytes, uploaded_at),
        )
        return cur.lastrowid


def get_group_attachment(conn, attachment_id):
    row = conn.execute(
        "SELECT id, filename, stored_name, content_type, size_bytes FROM group_message_attachments WHERE id = ?",
        (attachment_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "filename": row[1], "stored_name": row[2], "content_type": row[3], "size_bytes": row[4],
    }


def get_group_attachments_for_messages(conn, message_ids):
    ids = sorted(set(message_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, message_id, filename, size_bytes FROM group_message_attachments "
        f"WHERE message_id IN ({placeholders})",
        ids,
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r[1], []).append({"id": r[0], "filename": r[2], "size_bytes": r[3]})
    return result


def mark_group_read(conn, user_id, last_read_message_id):
    with conn:
        conn.execute(
            "INSERT INTO group_read_state (user_id, last_read_message_id) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_read_message_id = "
            "MAX(last_read_message_id, excluded.last_read_message_id)",
            (user_id, last_read_message_id),
        )


def get_group_read_state(conn, user_id):
    row = conn.execute(
        "SELECT last_read_message_id FROM group_read_state WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row else 0


def group_unread_count(conn, user_id):
    last_read = get_group_read_state(conn, user_id)
    row = conn.execute(
        "SELECT COUNT(*) FROM group_messages WHERE id > ? AND sender_id != ?", (last_read, user_id)
    ).fetchone()
    return row[0] if row else 0


def all_group_read_states(conn):
    """{user_id: last_read_message_id}(読んだことのあるユーザーのみ、無い場合は暗黙で0)。
    グループチャットの「既読 n人」表示で、メッセージごとにクエリを発行しないための
    まとめ取得(会話数が少ないグループチャットでは全件を1回読んでPython側で集計する
    方が、メッセージ数だけクエリを発行するより安い)。"""
    rows = conn.execute("SELECT user_id, last_read_message_id FROM group_read_state").fetchall()
    return {r[0]: r[1] for r in rows}


def add_group_reaction(conn, message_id, user_id, emoji, created_at):
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_message_reactions (message_id, user_id, emoji, created_at) "
            "VALUES (?, ?, ?, ?)",
            (message_id, user_id, emoji, created_at),
        )


def remove_group_reaction(conn, message_id, user_id, emoji):
    with conn:
        conn.execute(
            "DELETE FROM group_message_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
            (message_id, user_id, emoji),
        )


def get_group_reactions_for_messages(conn, message_ids):
    ids = sorted(set(message_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT message_id, emoji, user_id FROM group_message_reactions WHERE message_id IN ({placeholders})",
        ids,
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r[0], []).append({"emoji": r[1], "user_id": r[2]})
    return result


def record_group_mentions(conn, message_id, user_ids):
    ids = sorted(set(user_ids))
    if not ids:
        return
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO group_message_mentions (message_id, user_id) VALUES (?, ?)",
            [(message_id, uid) for uid in ids],
        )


def get_group_mentions_for_messages(conn, message_ids):
    ids = sorted(set(message_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT message_id, user_id FROM group_message_mentions WHERE message_id IN ({placeholders})",
        ids,
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r[0], []).append(r[1])
    return result


def add_shared_file(
    conn, filename, stored_name, content_type, size_bytes, uploaded_by, uploaded_at,
    description="", folder_id=None, tags="", content_text="",
):
    with conn:
        cur = conn.execute(
            "INSERT INTO shared_files "
            "(filename, stored_name, content_type, size_bytes, uploaded_by, uploaded_at, description, "
            "folder_id, tags, content_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (filename, stored_name, content_type, size_bytes, uploaded_by, uploaded_at, description,
             folder_id, tags, content_text),
        )
        return cur.lastrowid


def list_shared_files(conn, folder_id=None):
    """folder_id=Noneは最上位(フォルダに属さない)ファイルだけを返す。"""
    if folder_id is None:
        rows = conn.execute(
            "SELECT id, filename, content_type, size_bytes, uploaded_by, uploaded_at, description, "
            "folder_id, tags FROM shared_files WHERE folder_id IS NULL ORDER BY uploaded_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, filename, content_type, size_bytes, uploaded_by, uploaded_at, description, "
            "folder_id, tags FROM shared_files WHERE folder_id = ? ORDER BY uploaded_at DESC",
            (folder_id,),
        ).fetchall()
    return [
        {
            "id": r[0], "filename": r[1], "content_type": r[2], "size_bytes": r[3],
            "uploaded_by": r[4], "uploaded_at": r[5], "description": r[6], "folder_id": r[7], "tags": r[8],
        }
        for r in rows
    ]


def search_shared_files(conn, keyword):
    """filename・description・tags・content_text(全文検索対象、_save_uploaded_file時に
    テキスト系ファイルのみ格納済み)を横断検索する。フォルダをまたいで全件を対象にする。"""
    like = f"%{_escape_like(keyword)}%"
    rows = conn.execute(
        "SELECT id, filename, content_type, size_bytes, uploaded_by, uploaded_at, description, "
        "folder_id, tags FROM shared_files "
        "WHERE filename LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' "
        "OR tags LIKE ? ESCAPE '\\' OR content_text LIKE ? ESCAPE '\\' "
        "ORDER BY uploaded_at DESC",
        (like, like, like, like),
    ).fetchall()
    return [
        {
            "id": r[0], "filename": r[1], "content_type": r[2], "size_bytes": r[3],
            "uploaded_by": r[4], "uploaded_at": r[5], "description": r[6], "folder_id": r[7], "tags": r[8],
        }
        for r in rows
    ]


def get_shared_file(conn, file_id):
    row = conn.execute(
        "SELECT id, filename, stored_name, content_type, size_bytes, uploaded_by, uploaded_at, "
        "description, folder_id, tags FROM shared_files WHERE id = ?",
        (file_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "filename": row[1], "stored_name": row[2], "content_type": row[3],
        "size_bytes": row[4], "uploaded_by": row[5], "uploaded_at": row[6], "description": row[7],
        "folder_id": row[8], "tags": row[9],
    }


def delete_shared_file(conn, file_id):
    with conn:
        conn.execute("DELETE FROM shared_files WHERE id = ?", (file_id,))


def move_shared_file(conn, file_id, folder_id):
    with conn:
        conn.execute("UPDATE shared_files SET folder_id = ? WHERE id = ?", (folder_id, file_id))


def toggle_file_star(conn, user_id, file_id, created_at):
    """既にお気に入りならお気に入り解除、そうでなければ登録する。登録後の状態(True/False)を返す。"""
    with conn:
        cur = conn.execute(
            "DELETE FROM shared_file_stars WHERE user_id = ? AND file_id = ?", (user_id, file_id),
        )
        if cur.rowcount > 0:
            return False
        conn.execute(
            "INSERT INTO shared_file_stars (user_id, file_id, created_at) VALUES (?, ?, ?)",
            (user_id, file_id, created_at),
        )
        return True


def starred_file_ids(conn, user_id):
    rows = conn.execute("SELECT file_id FROM shared_file_stars WHERE user_id = ?", (user_id,)).fetchall()
    return {r[0] for r in rows}


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
    """空のフォルダだけを削除する。空チェックと削除を同じトランザクション内で行うことで、
    「空チェック」と「削除」の間に別リクエストがファイルを追加するレース(TOCTOU)を防ぐ
    (以前はwebapp.py側でshared_folder_is_empty()を先に呼び、その結果を見てから
    delete_shared_folder()を呼ぶ2ステップだったため、間隔中に追加されたファイルが
    親フォルダ削除のON DELETE CASCADEで一緒に消えてしまう、あるいはIntegrityErrorに
    なる可能性があった、2026-09-13の監査で指摘)。戻り値: 実際に削除できたらTrue、
    空でなかった(削除しなかった)場合はFalse。"""
    with conn:
        if not shared_folder_is_empty(conn, folder_id):
            return False
        conn.execute("DELETE FROM shared_folders WHERE id = ?", (folder_id,))
        return True


# --- 個人設定とWeb Push(2026-10-09、user_settings.py・push_notify.py) ---------------------------

def get_user_settings_raw(conn, user_id):
    """保存済みの設定JSON(文字列)。未保存なら"{}"。検証・既定値の補完はuser_settings.normalize。"""
    row = conn.execute("SELECT data FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else "{}"


def all_user_settings_raw(conn):
    return {uid: data for uid, data in conn.execute("SELECT user_id, data FROM user_settings").fetchall()}


def save_user_settings(conn, user_id, data_json, updated_at):
    with conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (user_id, data_json, updated_at),
        )


def clear_presence(conn, user_id):
    """「オンライン状態を表示しない」にした利用者の最終アクセス時刻を消す(以後は記録しない)。"""
    with conn:
        conn.execute("DELETE FROM user_presence WHERE user_id = ?", (user_id,))


_PUSH_COLUMNS = "id, user_id, endpoint, p256dh, auth, device_label, created_at, last_success_at, failure_count"


def _push_row(r):
    return {"id": r[0], "user_id": r[1], "endpoint": r[2], "p256dh": r[3], "auth": r[4],
            "device_label": r[5], "created_at": r[6], "last_success_at": r[7], "failure_count": r[8]}


def save_push_subscription(conn, user_id, endpoint, p256dh, auth, device_label, created_at):
    """同じendpoint(=同じブラウザ)を別の利用者が登録し直した場合は、新しい利用者のものに付け替える
    (共用の端末でログインし直したとき、前の人宛の通知が届き続けないように)。"""
    with conn:
        conn.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, device_label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, "
            "p256dh = excluded.p256dh, auth = excluded.auth, device_label = excluded.device_label, "
            "failure_count = 0",
            (user_id, endpoint, p256dh, auth, device_label, created_at),
        )


def list_push_subscriptions(conn, user_id):
    rows = conn.execute(
        f"SELECT {_PUSH_COLUMNS} FROM push_subscriptions WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    return [_push_row(r) for r in rows]


def push_subscriber_ids(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM push_subscriptions").fetchall()]


def delete_push_subscription(conn, sub_id, user_id=None):
    with conn:
        if user_id is None:
            conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))
        else:
            conn.execute("DELETE FROM push_subscriptions WHERE id = ? AND user_id = ?", (sub_id, user_id))


def delete_push_subscription_by_endpoint(conn, endpoint, user_id):
    with conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?", (endpoint, user_id))


def record_push_result(conn, sub_id, ok, at):
    with conn:
        if ok:
            conn.execute("UPDATE push_subscriptions SET last_success_at = ?, failure_count = 0 WHERE id = ?",
                         (at, sub_id))
        else:
            conn.execute("UPDATE push_subscriptions SET failure_count = failure_count + 1 WHERE id = ?", (sub_id,))


def claim_push_key(conn, user_id, key, at):
    """同じ通知(key)を同じ利用者に二度送らないための記録。初めてならTrue(記録した)、既に送っていればFalse。"""
    with conn:
        cur = conn.execute("INSERT OR IGNORE INTO push_sent (user_id, key, sent_at) VALUES (?, ?, ?)",
                           (user_id, key, at))
        return cur.rowcount == 1


def prune_push_sent(conn, before):
    with conn:
        conn.execute("DELETE FROM push_sent WHERE sent_at < ?", (before,))


__all__ = [
    "get_user_settings_raw", "all_user_settings_raw", "save_user_settings", "clear_presence",
    "save_push_subscription", "list_push_subscriptions", "push_subscriber_ids", "delete_push_subscription",
    "delete_push_subscription_by_endpoint", "record_push_result", "claim_push_key", "prune_push_sent",
    "connect", "create_user", "get_user_by_email", "get_user_by_id", "verify_login",
    "list_users", "get_users_by_ids", "set_role", "update_user", "update_profile", "delete_user",
    "count_admins", "email_taken",
    "create_signup_request", "list_signup_requests", "count_signup_requests", "get_signup_request",
    "approve_signup_request", "reject_signup_request", "generate_password_hash",
    "record_login", "record_logout", "login_history", "login_counts_by_day", "login_count",
    "login_counts_all", "login_counts_since", "last_login", "first_login", "last_logins_all", "login_streak",
    "login_dates_all", "login_streaks_all",
    "record_audit", "list_audit_log",
    "add_station_comment", "list_station_comments", "get_station_comment",
    "delete_station_comment", "comment_count",
    "award_badge", "grant_points", "total_earned_points", "total_spent_points", "available_points",
    "list_all_user_point_balances", "list_all_user_gem_balances",
    "owned_frame_ids", "purchase_frame", "set_equipped_frame",
    "frame_billing_info", "set_frame_next_billing", "list_equipped_frames_due",
    "owned_cosmetic_ids", "purchase_cosmetic", "set_equipped_cosmetic",
    "cosmetic_billing_info", "set_cosmetic_next_billing", "list_equipped_cosmetics_due",
    "grant_gems", "available_gems", "spend_gems_if_available",
    "owned_gem_item_ids", "purchase_gem_item", "set_equipped_gem_item",
    "buy_consumable", "consumable_quantities", "use_consumable",
    "login_counts_between",
    "get_fog_bet", "cancel_fog_bet", "won_fog_bet_count",
    "messages_sent_count", "reactions_given_count", "reactions_received_count",
    "mentions_received_count", "station_share_count",
    "shared_file_upload_count", "shared_file_star_count",
    "phenom_quiz_correct_count", "lottery_draw_count", "monthly_ranking_first_count",
    "place_daily_fog_quiz_guess", "get_daily_fog_quiz", "list_pending_daily_fog_quiz",
    "resolve_daily_fog_quiz", "get_daily_fog_quiz_stats", "update_daily_fog_quiz_stats",
    "place_daily_phenom_quiz_answer", "get_daily_phenom_quiz", "get_point_event_amount",
    "get_or_create_dm_conversation", "get_dm_conversation", "send_dm_message",
    "list_dm_messages", "list_dm_conversations", "mark_dm_read", "mark_dm_unread",
    "dm_unread_counts", "total_dm_unread",
    "toggle_pinned_conversation", "pinned_conversation_ids",
    "add_dm_attachment", "get_dm_attachment", "get_dm_attachments_for_messages",
    "get_dm_message", "get_dm_message_attachment_stored_names", "delete_dm_message",
    "edit_dm_message", "pin_dm_message", "list_pinned_dm_messages", "search_dm_messages",
    "get_dm_read_state", "add_dm_reaction", "remove_dm_reaction", "get_dm_reactions_for_messages",
    "record_dm_mentions", "get_dm_mentions_for_messages",
    "count_users",
    "send_group_message", "list_group_messages", "get_group_message", "edit_group_message",
    "pin_group_message", "list_pinned_group_messages", "search_group_messages",
    "get_group_message_attachment_stored_names", "delete_group_message",
    "add_group_attachment", "get_group_attachment", "get_group_attachments_for_messages",
    "mark_group_read", "get_group_read_state", "group_unread_count", "all_group_read_states",
    "add_group_reaction", "remove_group_reaction", "get_group_reactions_for_messages",
    "record_group_mentions", "get_group_mentions_for_messages",
    "add_shared_file", "list_shared_files", "search_shared_files", "get_shared_file", "delete_shared_file",
    "move_shared_file", "toggle_file_star", "starred_file_ids",
    "create_shared_folder", "get_shared_folder", "list_shared_folders",
    "shared_folder_breadcrumbs", "shared_folder_is_empty", "delete_shared_folder",
]
