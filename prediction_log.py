"""予測結果の記録・答え合わせ（検証）・精度集計を行うSQLiteベースの台帳。

『予測していたデータを実際のデータと見比べて誤差を学習し、完璧に近づけていく』
というループの核となる部分。日々の予測をすべて記録しておき、実測が確定した後で
answer合わせをし、その結果を含めて翌日また全期間で再学習する（fog_model.train_location_models
は毎回、最新の実測データも含めて学習し直すため、この記録自体は学習の入力ではなく
「答え合わせと精度の推移を追跡するための台帳」という位置づけ）。
"""
import sqlite3
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    station TEXT NOT NULL,
    target_datetime TEXT NOT NULL,
    lead_hours REAL NOT NULL,
    predicted_code INTEGER NOT NULL,
    predicted_fog_prob REAL,
    model_type TEXT,
    actual_code INTEGER,
    verified_at TEXT,
    UNIQUE(station, target_datetime, run_at)
);
CREATE INDEX IF NOT EXISTS idx_predictions_pending
    ON predictions(target_datetime) WHERE actual_code IS NULL;
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def record_predictions(conn, run_at, rows):
    """rows: [(station, target_datetime, lead_hours, predicted_code, predicted_fog_prob, model_type), ...]
    同じ(station, target_datetime, run_at)の組み合わせは1回しか記録されない（UNIQUE制約）。
    """
    with conn:
        conn.executemany(
            """INSERT OR IGNORE INTO predictions
               (run_at, station, target_datetime, lead_hours, predicted_code, predicted_fog_prob, model_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(run_at, station, target_dt, lead_hours, predicted_code, predicted_fog_prob, model_type)
             for station, target_dt, lead_hours, predicted_code, predicted_fog_prob, model_type in rows],
        )


def fetch_pending(conn, up_to_datetime):
    """target_datetime <= up_to_datetime で、まだ答え合わせしていない予測を返す。"""
    with closing(conn.execute(
        """SELECT id, station, target_datetime FROM predictions
           WHERE actual_code IS NULL AND target_datetime <= ?""",
        (up_to_datetime,),
    )) as cur:
        return cur.fetchall()


def verify_predictions(conn, verified_at, results):
    """results: [(id, actual_code), ...]"""
    with conn:
        conn.executemany(
            "UPDATE predictions SET actual_code = ?, verified_at = ? WHERE id = ?",
            [(actual_code, verified_at, pred_id) for pred_id, actual_code in results],
        )


def accuracy_summary(conn, since=None):
    """答え合わせ済みの予測について、全体・地点別・リードタイム別の的中率を返す。"""
    where = "WHERE actual_code IS NOT NULL"
    params = ()
    if since:
        where += " AND verified_at >= ?"
        params = (since,)

    overall = conn.execute(
        f"""SELECT COUNT(*), SUM(CASE WHEN predicted_code = actual_code THEN 1 ELSE 0 END)
            FROM predictions {where}""", params,
    ).fetchone()

    by_lead_bucket = conn.execute(
        f"""SELECT CAST(lead_hours / 24 AS INTEGER) AS lead_day,
                   COUNT(*), SUM(CASE WHEN predicted_code = actual_code THEN 1 ELSE 0 END)
            FROM predictions {where}
            GROUP BY lead_day ORDER BY lead_day""", params,
    ).fetchall()

    by_station = conn.execute(
        f"""SELECT station, COUNT(*), SUM(CASE WHEN predicted_code = actual_code THEN 1 ELSE 0 END)
            FROM predictions {where}
            GROUP BY station ORDER BY station""", params,
    ).fetchall()

    return {
        "overall": {"n": overall[0] or 0, "correct": overall[1] or 0},
        "by_lead_day": [{"lead_day": d, "n": n, "correct": c} for d, n, c in by_lead_bucket],
        "by_station": [{"station": s, "n": n, "correct": c} for s, n, c in by_station],
    }
