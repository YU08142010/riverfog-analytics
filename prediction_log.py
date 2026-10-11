"""予測結果の記録・答え合わせ（検証）・精度集計を行うSQLiteベースの台帳。

『予測していたデータを実際のデータと見比べて誤差を学習し、完璧に近づけていく』
というループの核となる部分。日々の予測をすべて記録しておき、実測が確定した後で
answer合わせをし、その結果を含めて翌日また全期間で再学習する（fog_model.train_shared_model
は毎回、最新の実測データも含めて学習し直すため、この記録自体は学習の入力ではなく
「答え合わせと精度の推移を追跡するための台帳」という位置づけ）。
"""
import sqlite3
from contextlib import closing

import fog_features

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
# predicted_code: 2026-10-04以降は二値の予測(1=川霧あり、0=川霧なし。全地点共通の二値
#   LightGBM、fog_model.py参照)。それ以前の行は旧方式(地点別の多クラス分類)の現象コード
#   (0〜10、2コード同時観測はパック値)のまま残っている。
# actual_code: 実測の現象コード(0〜10、2コード同時観測は「小さい方*100+大きい方」のパック値。
#   fog_features.encode_phenom_codes参照)。
# 的中の判定は、新旧どちらの行も「川霧かどうか」が一致したかで行う(_correct_sql())。
# 二値の1は川霧コード(SUMMARY_FOG_CODES)に含まれ、0は含まれないため、同じ判定式が
# 旧方式の現象コードにも二値の予測にもそのまま使える。
_FOG_CODES_SQL = ", ".join(str(c) for c in sorted(fog_features.SUMMARY_FOG_CODES))


def _fog_sql(col):
    """SQL式: 列の値(現象コード/パック値/二値の予測)が川霧かどうか(1/0)。
    川霧は家族グループの並び上、2コード同時観測でも必ず主コード(パック値の上2桁)側に入る。"""
    return f"((CASE WHEN {col} > 10 THEN {col} / 100 ELSE {col} END) IN ({_FOG_CODES_SQL}))"


def _actual_fog_sql(col="actual_code", station_col="station"):
    """実測が「予測の対象の霧」かどうか(1/0)。会津など全体霧も対象の地点(fog_features.target_codes)は4〜6も含める。"""
    wide = fog_features.wide_target_stations(_all_station_names())
    if not wide:
        return _fog_sql(col)
    names = ", ".join("'" + n.replace("'", "''") + "'" for n in wide)
    prim = f"(CASE WHEN {col} > 10 THEN {col} / 100 ELSE {col} END)"
    return f"(CASE WHEN {station_col} IN ({names}) THEN {prim} IN (1, 2, 3, 4, 5, 6) ELSE {prim} IN ({_FOG_CODES_SQL}) END)"


def _all_station_names():
    try:
        from fog_data import load_station_names
        return load_station_names()
    except Exception:
        return []


def _correct_sql():
    """的中(1/0)のSQL式。予測は二値(1=霧あり)なので、実測の側だけ地点ごとの対象で判定する。"""
    return f"CASE WHEN {_fog_sql('predicted_code')} = {_actual_fog_sql()} THEN 1 ELSE 0 END"

# 精度の集計の対象にするモデル(model_typeのLIKEパターン)。現行モデル(全地点共通の二値
# LightGBM)の本番の予測と、旧モデルが予測していた過去の日時を現行モデルで予測し直した行
# (model_typeに"[再予測]"が付く。hindcast_lightgbm.py)が該当する。旧モデルの行は「当時実際に
# どう予測したか」の記録としてDBに残すが、同じ日時に新旧の予測が並ぶため集計からは外す。
# model_like=Noneを渡すと全モデルの行を集計する。日の出前・日の入り後(daylight=0)の行は常に除く。
CURRENT_MODEL_LIKE = "LightGBM(二値%"


def connect(db_path):
    # predict.py(日次)とWebの両方が書き込みうるため、busy_timeoutで書き込み中の
    # 衝突を即座にエラーにせず待たせる(weather_store.pyと同じ対策)。
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    # daylight: 対象時刻が日の出〜日の入りの間なら1、それ以外は0(NULLは未判定、fill_daylightで埋める)。
    # 暗い時間帯の画像からは川霧を判定できないため、精度の集計からは外す(2026-10-05追加)。
    if "daylight" not in {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}:
        conn.execute("ALTER TABLE predictions ADD COLUMN daylight INTEGER")
        conn.commit()
    return conn


def fill_daylight(conn, coords_by_station):
    """daylightが未判定の行を埋める。coords_by_station: {地点名: (緯度, 経度)}
    (座標の無い地点は全地点の座標の平均で判定する)。戻り値: 埋めた(地点, 時刻)の数。"""
    from fog_features import is_daylight
    pairs = conn.execute(
        "SELECT DISTINCT station, target_datetime FROM predictions WHERE daylight IS NULL"
    ).fetchall()
    if not pairs:
        return 0
    if coords_by_station:
        fallback = (sum(c[0] for c in coords_by_station.values()) / len(coords_by_station),
                    sum(c[1] for c in coords_by_station.values()) / len(coords_by_station))
    else:
        fallback = (37.4, 139.6)
    updates = [
        (1 if is_daylight(dt, *coords_by_station.get(st, fallback)) else 0, st, dt)
        for st, dt in pairs
    ]
    with conn:
        conn.executemany(
            "UPDATE predictions SET daylight = ? WHERE station = ? AND target_datetime = ? AND daylight IS NULL",
            updates,
        )
    return len(updates)


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


def accuracy_summary(conn, since=None, model_like=CURRENT_MODEL_LIKE):
    """答え合わせ済みの予測について、全体・地点別・リードタイム別の的中率
    (「川霧かどうか」が一致した割合)を返す。"""
    where = "WHERE actual_code IS NOT NULL"
    params = ()
    if since:
        where += " AND verified_at >= ?"
        params = (since,)
    if model_like:
        where += " AND model_type LIKE ?"
        params = params + (model_like,)
    where += " AND COALESCE(daylight, 1) = 1"

    overall = conn.execute(
        f"""SELECT COUNT(*), SUM({_correct_sql()})
            FROM predictions {where}""", params,
    ).fetchone()

    # 見出しの的中率は「翌日の予測」(発表から24時間先まで)に限る。全リードタイムを混ぜると、
    # 先の日付ほど当たりにくい予測の割合(答え合わせが済んだ時期によって変わる)で値が動くため。
    # 再予測(過去分のやり直し、[再予測])の件数も分けて返し、本番の予測と区別できるようにする。
    next_day = conn.execute(
        f"""SELECT COUNT(*), SUM({_correct_sql()}), SUM(CASE WHEN model_type LIKE '%[再予測]' THEN 1 ELSE 0 END)
            FROM predictions {where} AND lead_hours < 24""", params,
    ).fetchone()
    n_hindcast = conn.execute(
        f"""SELECT COUNT(*) FROM predictions {where} AND model_type LIKE '%[再予測]'""", params,
    ).fetchone()[0]

    by_lead_bucket = conn.execute(
        f"""SELECT CAST(lead_hours / 24 AS INTEGER) AS lead_day,
                   COUNT(*), SUM({_correct_sql()})
            FROM predictions {where}
            GROUP BY lead_day ORDER BY lead_day""", params,
    ).fetchall()

    by_station = conn.execute(
        f"""SELECT station, COUNT(*), SUM({_correct_sql()})
            FROM predictions {where}
            GROUP BY station ORDER BY station""", params,
    ).fetchall()

    return {
        "overall": {"n": overall[0] or 0, "correct": overall[1] or 0, "n_hindcast": n_hindcast or 0},
        "next_day": {"n": next_day[0] or 0, "correct": next_day[1] or 0, "n_hindcast": next_day[2] or 0},
        "by_lead_day": [{"lead_day": d, "n": n, "correct": c} for d, n, c in by_lead_bucket],
        "by_station": [{"station": s, "n": n, "correct": c} for s, n, c in by_station],
    }


def accuracy_trend(conn, station=None, model_like=CURRENT_MODEL_LIKE):
    """target_datetimeの日付ごとの的中率(「川霧かどうか」の一致率)の推移を返す。
    stationを指定すればその地点のみ。
    戻り値: [{"date": "YYYY-MM-DD", "n": int, "correct": int}, ...]（日付昇順）
    """
    where = "WHERE actual_code IS NOT NULL"
    params = ()
    if station:
        where += " AND station = ?"
        params = (station,)
    if model_like:
        where += " AND model_type LIKE ?"
        params = params + (model_like,)
    where += " AND COALESCE(daylight, 1) = 1"

    rows = conn.execute(
        f"""SELECT substr(target_datetime, 1, 10) AS d, COUNT(*),
                   SUM({_correct_sql()})
            FROM predictions {where}
            GROUP BY d ORDER BY d""", params,
    ).fetchall()
    return [{"date": d, "n": n, "correct": c} for d, n, c in rows]


def fog_pair_counts(conn, since=None, station=None, model_like=CURRENT_MODEL_LIKE):
    """答え合わせ済みの(station, 予測コード, 実際のコード)の組み合わせごとの件数を返す。
    混同行列・地点/地域別のprecision/recall/F1・川霧クラスの見逃し/空振り集計など、
    分析ページの各種指標をこの1つの集計結果から使い回す（個別に似たクエリを何度も
    発行しないため）。戻り値: [{"station":, "predicted_code":, "actual_code":, "n":}, ...]
    """
    where = ["actual_code IS NOT NULL"]
    params = []
    if model_like:
        where.append("model_type LIKE ?")
        params.append(model_like)
    where.append("COALESCE(daylight, 1) = 1")
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


def fog_prob_bucket_counts(conn, since=None, station=None, model_like=CURRENT_MODEL_LIKE):
    """答え合わせ済みかつpredicted_fog_probが記録されている予測を、確率を0.01刻みに
    丸めた上で件数を集計する。しきい値を変えた場合のprecision/recall/F1を後から
    計算するための元データ（生の行を全部返すとレコード数が多い場合に重いため、
    しきい値の刻み幅(fog_model.FOG_THRESHOLD_GRID、0.01刻み)と同じ粒度に丸めて集計する。
    2026-10-04の二値LightGBMへの切り替えで、採用されるしきい値が0.1前後と小さくなり、
    以前の0.05刻みでは粗すぎるようになったため0.01刻みにした）。
    戻り値: [{"prob": 0.01刻みの確率, "actual_code":, "n":}, ...]
    """
    where = ["actual_code IS NOT NULL", "predicted_fog_prob IS NOT NULL"]
    params = []
    if model_like:
        where.append("model_type LIKE ?")
        params.append(model_like)
    where.append("COALESCE(daylight, 1) = 1")
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
    """from_dt_str〜to_dt_strの間で「川霧」と予測されている地点・時刻を返す
    (二値の予測では1=川霧あり。旧方式の現象コードの行は主コードが川霧かで判定する)。
    同じ(station, target_datetime)に複数回の予測記録がある場合、最新のrun_atのものを使う
    （予報は毎日更新されるため、直近の実行結果の方が精度が高い）。
    戻り値: [{"station":, "target_datetime":, "predicted_code":, "predicted_fog_prob":}, ...]
    （target_datetime昇順）
    """
    rows = conn.execute(
        """SELECT station, target_datetime, predicted_code, predicted_fog_prob, run_at
           FROM predictions
           WHERE target_datetime BETWEEN ? AND ? AND COALESCE(daylight, 1) = 1
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
        if fog_features.primary_phenom_code(code) in fog_codes:
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


def apply_actual_corrections(conn, corrections):
    """答え合わせ済みの予測の実測(actual_code)を、人による訂正後の値に揃える。
    corrections: {(日時文字列, 地点名): 訂正後のコード}(fog_data.load_phenomena_corrections)。
    戻り値: 書き換えた行数。"""
    if not corrections:
        return 0
    with conn:
        before = conn.total_changes
        conn.executemany(
            """UPDATE predictions SET actual_code = ?
               WHERE station = ? AND target_datetime = ? AND actual_code IS NOT NULL AND actual_code != ?""",
            [(code, station, dt, code) for (dt, station), code in corrections.items()],
        )
        return conn.total_changes - before
