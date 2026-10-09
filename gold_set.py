"""正解セット(2026-10-09): 毎日ランダムに選んだ画像を、AIの判定を見せずに人が判定する。

これまで人が見たのは「怪しい」と自動で拾われたラベルや利用者の報告ばかりで、AIのラベル全体の正しさも、
予測の本当の精度(人の判定に対する的中)も測れていなかった(label_audit.jsonの注意書き)。
そこで、毎日の日中の画像から次の2つの層に分けて無作為に選ぶ:
  - ai_fog: AIが霧(川霧1〜3・全体霧4〜6)と判定した画像から AI_FOG_PER_DAY 枚
  - other : それ以外(現象なし・層雲・雨など)から OTHER_PER_DAY 枚
霧は全体の数%しかないため、霧の層を多めに選ぶ。全体の割合を出すときは、層ごとの「選ばれた確率」の
逆数(weight = その層の枚数 / 選んだ枚数)で重み付けする。

- 判定は1枚につき2人まで(別々に、互いの判定も見せない)。2人の一致率も出す。
- 人の判定がAIと違い、かつそれまでの人の判定と食い違わなければ、観測の訂正
  (phenomena_corrections、source=human)としても記録する(学習のラベルが良くなる)。
- テーブルはusers.pyのSCHEMA(gold_samples・gold_labels)。画面はwebappの /gold。
"""
import random
import sqlite3
from datetime import datetime, timedelta

import pandas as pd

from common import BASE_DIR

WEATHER_DB = BASE_DIR / "data" / "weather.db"
PRED_DB = BASE_DIR / "data" / "predictions.db"
CONF_LOG = BASE_DIR / "data" / "models" / "confidence_log.csv"
IMAGE_DIR = BASE_DIR / "data" / "images"
AI_FOG_PER_DAY = 10
OTHER_PER_DAY = 20
BACKFILL_DAYS = 7          # 初回は過去この日数分を用意する
MAX_LABELS_PER_SAMPLE = 2
UNSURE_CODE = -1           # 「画像からは判定できない」
START_DATE = "2026-10-02"  # これより前の日は作らない(気象のヒント付きの判定だった期間を避ける)


def prim(code):
    code = int(code)
    return code // 100 if code > 10 else code


def group(code):
    """0:現象なし 1:川霧 2:全体霧 3:層雲 4:霧雨・雨 / None:判定できない"""
    if code is None or code == UNSURE_CODE:
        return None
    c = prim(code)
    return 0 if c == 0 else (1 if c in (1, 2, 3) else (2 if c in (4, 5, 6) else (3 if c in (7, 8) else 4)))


def _image_exists(station, dt):
    return (IMAGE_DIR / dt[:10] / (dt[11:13] + dt[14:16]) / f"{station}.jpg").is_file()


def ensure_samples(conn, today, daylight_fn, now_str):
    """昨日までの各日について、まだ選んでいなければ選ぶ。daylight_fn(地点名, datetime文字列) -> bool。"""
    have = {r[0] for r in conn.execute("SELECT DISTINCT substr(target_datetime, 1, 10) FROM gold_samples")}
    days = []
    for back in range(1, BACKFILL_DAYS + 1):
        d = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        if d >= START_DATE and d not in have:
            days.append(d)
    if not days:
        return 0
    w = sqlite3.connect(f"file:{WEATHER_DB}?mode=ro", uri=True)
    try:
        ph = pd.read_sql(
            "SELECT datetime, station_name, code FROM phenomena WHERE code IS NOT NULL AND code >= 0 "
            "AND datetime >= ? AND datetime < ?",
            w, params=(min(days) + " 00:00:00", (datetime.strptime(max(days), "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")),
        )
    finally:
        w.close()
    try:
        cl = pd.read_csv(CONF_LOG, usecols=["datetime", "station", "confidence"])
        conf = {(s, d): c for s, d, c in zip(cl["station"], cl["datetime"], cl["confidence"])}
    except Exception:
        conf = {}
    added = 0
    for d in days:
        sub = ph[ph["datetime"].str.startswith(d)]
        rows = [(s, dt, int(c)) for dt, s, c in zip(sub["datetime"], sub["station_name"], sub["code"])
                if daylight_fn(s, dt) and _image_exists(s, dt)]
        if not rows:
            continue
        rng = random.Random(f"gold:{d}")
        strata = {"ai_fog": [r for r in rows if group(r[2]) in (1, 2)],
                  "other": [r for r in rows if group(r[2]) not in (1, 2)]}
        with conn:
            for name, pool, n in (("ai_fog", strata["ai_fog"], AI_FOG_PER_DAY), ("other", strata["other"], OTHER_PER_DAY)):
                if not pool:
                    continue
                pick = rng.sample(pool, min(n, len(pool)))
                weight = len(pool) / len(pick)
                for s, dt, c in pick:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO gold_samples (station_name, target_datetime, stratum, weight, ai_code, "
                        "ai_confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (s, dt, name, weight, c, conf.get((s, dt)), now_str))
                    added += cur.rowcount
    return added


def next_sample(conn, user_id, allowed_stations=None):
    """この人がまだ判定しておらず、判定が2人に満たない画像を1枚。1人目の判定が済んだもの(2人目待ち)を先に、
    次に新しい日から。allowed_stationsを渡すとその地点だけ(地域担当の観測者)。"""
    rows = conn.execute(
        "SELECT s.id, s.station_name, s.target_datetime, COUNT(l.user_id) AS n, "
        "SUM(CASE WHEN l.user_id = ? THEN 1 ELSE 0 END) AS mine FROM gold_samples s "
        "LEFT JOIN gold_labels l ON l.sample_id = s.id GROUP BY s.id "
        "HAVING mine = 0 AND n < ? ORDER BY n DESC, substr(s.target_datetime, 1, 10) DESC, s.id",
        (user_id, MAX_LABELS_PER_SAMPLE),
    ).fetchall()
    for sid, st, dt, n, _ in rows:
        if allowed_stations is None or st in allowed_stations:
            return {"id": sid, "station_name": st, "target_datetime": dt, "n_labels": n}
    return None


def remaining_count(conn, user_id, allowed_stations=None):
    rows = conn.execute(
        "SELECT s.station_name, COUNT(l.user_id) AS n, SUM(CASE WHEN l.user_id = ? THEN 1 ELSE 0 END) AS mine "
        "FROM gold_samples s LEFT JOIN gold_labels l ON l.sample_id = s.id GROUP BY s.id "
        "HAVING mine = 0 AND n < ?", (user_id, MAX_LABELS_PER_SAMPLE)).fetchall()
    return sum(1 for st, _, _ in rows if allowed_stations is None or st in allowed_stations)


def get_sample(conn, sample_id):
    r = conn.execute("SELECT id, station_name, target_datetime, stratum, weight, ai_code, ai_confidence FROM gold_samples "
                     "WHERE id = ?", (sample_id,)).fetchone()
    if not r:
        return None
    return dict(zip(("id", "station_name", "target_datetime", "stratum", "weight", "ai_code", "ai_confidence"), r))


def save_label(conn, sample, user_id, code, now_str, current_code, add_correction):
    """判定を保存する。AIと違い、ほかの人の判定と食い違わなければ観測の訂正にもする。
    add_correction(station, dt, original_code, corrected_code, user_id, at)。戻り値: 訂正したか。"""
    with conn:
        conn.execute("INSERT OR REPLACE INTO gold_labels (sample_id, user_id, code, labeled_at) VALUES (?, ?, ?, ?)",
                     (sample["id"], user_id, code, now_str))
    if code == UNSURE_CODE:
        return False
    others = [c for (c,) in conn.execute("SELECT code FROM gold_labels WHERE sample_id = ? AND user_id != ?",
                                         (sample["id"], user_id))]
    if any(group(c) is not None and group(c) != group(code) for c in others):
        return False  # 人の間で分かれた: どちらにも直さない(結果の画面で「意見が分かれた」として出す)
    if current_code is not None and group(current_code) == group(code):
        return False
    add_correction(sample["station_name"], sample["target_datetime"], current_code, code, user_id, now_str)
    return True


def _consensus(codes):
    gs = [group(c) for c in codes if group(c) is not None]
    if not gs:
        return None
    return gs[0] if all(g == gs[0] for g in gs) else "split"


def _prf(pred, truth, weight):
    tp = float(((pred) & (truth)) @ weight)
    fp = float(((pred) & (~truth)) @ weight)
    fn = float(((~pred) & (truth)) @ weight)
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return {"precision": p, "recall": r, "f1": f}


def results(conn):
    """結果の画面用の集計。"""
    s = pd.read_sql("SELECT id, station_name, target_datetime, stratum, weight, ai_code, ai_confidence FROM gold_samples", conn)
    l = pd.read_sql("SELECT sample_id, user_id, code FROM gold_labels", conn)
    out = {"n_samples": int(len(s)), "n_labels": int(len(l)), "n_labelers": int(l["user_id"].nunique()) if len(l) else 0}
    if s.empty or l.empty:
        return out
    by = l.groupby("sample_id")["code"].apply(list)
    s["codes"] = s["id"].map(by)
    s = s[s["codes"].notna()].copy()
    s["n"] = s["codes"].map(len)
    s["human"] = s["codes"].map(_consensus)
    out["n_labeled_samples"] = int(len(s))
    # 2人の一致(川霧かどうか・5つのまとまり)
    two = s[s["n"] >= 2]
    pairs = [(group(c[0]), group(c[1])) for c in two["codes"] if group(c[0]) is not None and group(c[1]) is not None]
    if pairs:
        out["agreement"] = {"n": len(pairs), "group": sum(a == b for a, b in pairs) / len(pairs),
                            "river_fog": sum((a == 1) == (b == 1) for a, b in pairs) / len(pairs)}
    ok = s[s["human"].isin([0, 1, 2, 3, 4])].copy()
    out["n_split"] = int((s["human"] == "split").sum())
    out["n_unsure"] = int(s["human"].isna().sum())
    if ok.empty:
        return out
    ok["ai_g"] = ok["ai_code"].map(group)
    ok["h_g"] = ok["human"].astype(int)
    wt = ok["weight"].astype(float).values
    # AIのラベルの正しさ(全体の割合は層の重みで補正)
    out["ai_label"] = {
        "n": int(len(ok)),
        "group_accuracy_weighted": float(((ok["ai_g"] == ok["h_g"]).astype(float).values @ wt) / wt.sum()),
        "river": _prf((ok["ai_g"] == 1).values, (ok["h_g"] == 1).values, wt),
        "any_fog": _prf(ok["ai_g"].isin([1, 2]).values, ok["h_g"].isin([1, 2]).values, wt),
        "crosstab": pd.crosstab(ok["ai_g"], ok["h_g"]).to_dict(),
        "by_stratum": {k: {"n": int(len(d)), "agree": float((d["ai_g"] == d["h_g"]).mean())} for k, d in ok.groupby("stratum")},
        "by_confidence": {k: {"n": int(len(d)), "agree": float((d["ai_g"] == d["h_g"]).mean())}
                          for k, d in ok.groupby(ok["ai_confidence"].fillna("-"))},
    }
    # 予測の本当の精度: その日の0時より前で最新の本番の予測(公開ページの照合と同じ選び方)
    try:
        import prediction_log
        p = sqlite3.connect(f"file:{PRED_DB}?mode=ro", uri=True)
        preds = []
        try:
            for r in ok.itertuples():
                day0 = r.target_datetime[:10] + " 00:00:00"
                row = p.execute(
                    "SELECT predicted_code, predicted_fog_prob FROM predictions WHERE station = ? AND target_datetime = ? "
                    "AND model_type LIKE ? AND model_type NOT LIKE '%[再予測]' AND run_at < ? ORDER BY run_at DESC LIMIT 1",
                    (r.station_name, r.target_datetime, prediction_log.CURRENT_MODEL_LIKE, day0)).fetchone()
                preds.append(row[0] if row else None)
        finally:
            p.close()
        ok["pred"] = preds
        m = ok[ok["pred"].notna()]
        if len(m):
            w2 = m["weight"].astype(float).values
            pred = (m["pred"].astype(int) == 1).values
            out["model"] = {"n": int(len(m)),
                            "vs_human": _prf(pred, (m["h_g"] == 1).values, w2),
                            "vs_ai_label": _prf(pred, (m["ai_g"] == 1).values, w2)}
    except Exception as e:  # noqa: BLE001
        out["model_error"] = str(e)[:200]
    return out
