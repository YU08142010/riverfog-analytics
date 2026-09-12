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
    # predict.py(日次)とWebの両方が書き込みうるため、busy_timeoutで書き込み中の
    # 衝突を即座にエラーにせず待たせる(weather_store.pyと同じ対策)。
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
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
    """target_datetime <= up_to_datetime で、まだ答え合わせしていない予測を返す。
    戻り値: [{"id":, "station":, "target_datetime":}, ...]（他の集計関数と同じくdictのリスト）。
    """
    with closing(conn.execute(
        """SELECT id, station, target_datetime FROM predictions
           WHERE actual_code IS NULL AND target_datetime <= ?""",
        (up_to_datetime,),
    )) as cur:
        rows = cur.fetchall()
    return [{"id": r[0], "station": r[1], "target_datetime": r[2]} for r in rows]


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


def accuracy_trend(conn, station=None):
    """target_datetimeの日付ごとの的中率推移を返す。stationを指定すればその地点のみ。
    戻り値: [{"date": "YYYY-MM-DD", "n": int, "correct": int}, ...]（日付昇順）
    """
    where = "WHERE actual_code IS NOT NULL"
    params = ()
    if station:
        where += " AND station = ?"
        params = (station,)

    rows = conn.execute(
        f"""SELECT substr(target_datetime, 1, 10) AS d, COUNT(*),
                   SUM(CASE WHEN predicted_code = actual_code THEN 1 ELSE 0 END)
            FROM predictions {where}
            GROUP BY d ORDER BY d""", params,
    ).fetchall()
    return [{"date": d, "n": n, "correct": c} for d, n, c in rows]


def fog_pair_counts(conn, since=None, station=None):
    """答え合わせ済みの(station, 予測コード, 実際のコード)の組み合わせごとの件数を返す。
    混同行列・地点/地域別のprecision/recall/F1・霧クラスの見逃し/空振り集計など、
    分析ページの各種指標をこの1つの集計結果から使い回す（個別に似たクエリを何度も
    発行しないため）。戻り値: [{"station":, "predicted_code":, "actual_code":, "n":}, ...]
    """
    where = ["actual_code IS NOT NULL"]
    params = []
    if since:
        where.append("verified_at >= ?")
        params.append(since)
    if station:
        where.append("station = ?")
        params.append(station)
    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"""SELECT station, predicted_code, actual_code, COUNT(*)
            FROM predictions WHERE {where_sql}
            GROUP BY station, predicted_code, actual_code""", params,
    ).fetchall()
    return [{"station": s, "predicted_code": p, "actual_code": a, "n": n} for s, p, a, n in rows]


def fog_prob_bucket_counts(conn, since=None, station=None):
    """答え合わせ済みかつpredicted_fog_probが記録されている予測を、確率を0.05刻みに
    丸めた上で件数を集計する。しきい値を変えた場合のprecision/recall/F1を後から
    計算するための元データ（生の行を全部返すとレコード数が多い場合に重いため、
    しきい値の刻み幅と同じ粒度に丸めて集計してから返す）。
    戻り値: [{"prob": 0.05刻みの確率, "actual_code":, "n":}, ...]
    """
    where = ["actual_code IS NOT NULL", "predicted_fog_prob IS NOT NULL"]
    params = []
    if since:
        where.append("verified_at >= ?")
        params.append(since)
    if station:
        where.append("station = ?")
        params.append(station)
    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"""SELECT ROUND(predicted_fog_prob, 2) AS bucket, actual_code, COUNT(*)
            FROM predictions WHERE {where_sql}
            GROUP BY bucket, actual_code""", params,
    ).fetchall()
    return [{"prob": b, "actual_code": a, "n": n} for b, a, n in rows]


def upcoming_fog_alerts(conn, from_dt_str, to_dt_str, fog_codes):
    """from_dt_str〜to_dt_strの間で「霧」と予測されている地点・時刻を返す。
    同じ(station, target_datetime)に複数回の予測記録がある場合、最新のrun_atのものを使う
    （予報は毎日更新されるため、直近の実行結果の方が精度が高い）。
    戻り値: [{"station":, "target_datetime":, "predicted_code":, "predicted_fog_prob":}, ...]
    （target_datetime昇順）
    """
    rows = conn.execute(
        """SELECT station, target_datetime, predicted_code, predicted_fog_prob, run_at
           FROM predictions
           WHERE target_datetime BETWEEN ? AND ?
           ORDER BY run_at DESC""",
        (from_dt_str, to_dt_str),
    ).fetchall()
    seen = set()
    alerts = []
    for station, target_dt, code, prob, _run_at in rows:
        key = (station, target_dt)
        if key in seen:
            continue
        seen.add(key)
        if code in fog_codes:
            alerts.append({
                "station": station, "target_datetime": target_dt,
                "predicted_code": code, "predicted_fog_prob": prob,
            })
    alerts.sort(key=lambda a: (a["target_datetime"], a["station"]))
    return alerts


def db_stats(conn):
    """predictions.dbの規模を把握するための統計（管理画面のディスク使用量表示用）。"""
    total = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    verified = conn.execute("SELECT COUNT(*) FROM predictions WHERE actual_code IS NOT NULL").fetchone()[0]
    pending = total - verified
    oldest = conn.execute("SELECT MIN(target_datetime) FROM predictions").fetchone()[0]
    newest = conn.execute("SELECT MAX(target_datetime) FROM predictions").fetchone()[0]
    return {
        "total": total, "verified": verified, "pending": pending,
        "oldest": oldest, "newest": newest,
    }


def delete_verified_before(conn, before_dt_str):
    """target_datetimeがbefore_dt_strより前で、既に答え合わせ済みのレコードを削除する。
    未検証(actual_code IS NULL)のレコードは、まだ答え合わせに使うため対象外。
    戻り値: 削除した件数。
    """
    with conn:
        cur = conn.execute(
            "DELETE FROM predictions WHERE actual_code IS NOT NULL AND target_datetime < ?",
            (before_dt_str,),
        )
        return cur.rowcount
