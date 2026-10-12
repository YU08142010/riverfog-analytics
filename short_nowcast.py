"""この後3時間の川霧(画像と実測から、2026-10-12)。本番の予測(predict.py・fog_model)とは別の項目。

いま(時刻t)の情報だけから、1〜3時間後(t+1〜t+3)に川霧(1〜3)が写っているかを当てる、全地点共通のLightGBM。
入力: カメラ画像の判定(t・t-1・t-2のまとまり)、かすみ度(haze.db、tとt-1)、最寄りのアメダスの実測(気温・湿度・
露点差・風速と、その1・3時間の変化)、地域でいま川霧の地点の割合、対象の時刻(sin/cos)、先行時間、地点の過去の発生率。
人の訂正はラベルにも入力にも反映する。日の出〜日の入りの時刻だけ。

  .venv/bin/python short_nowcast.py --train     毎晩(20:10)。直近2週を評価、その前の1週でしきい値を決め、
                                                評価を data/experiments/short_nowcast_check.json に、
                                                全期間で学習し直したモデルを data/models/short_nowcast.joblib に保存
  .venv/bin/python short_nowcast.py --predict   毎時(収集・かすみ度・アメダスの後)。各地点のt+1〜t+3の確率を
                                                data/models/short_nowcast_latest.json に書く(地点ページの「この後3時間」)
評価では、「いまの状態がそのまま続く」(持続)と、前日夜の本番の予測の確率とも同じ行で比べる。
"""
import argparse
import csv
import json
import logging
import math
import sqlite3
from datetime import datetime, timedelta

import joblib
import numpy as np
import pandas as pd

from common import BASE_DIR, CAMERAS_CSV, JST

FROM = "2026-08-18 00:00:00"
LEADS = (1, 2, 3)
TEST_DAYS, VAL_DAYS = 14, 7
MODEL_PATH = BASE_DIR / "data" / "models" / "short_nowcast.joblib"
LATEST_PATH = BASE_DIR / "data" / "models" / "short_nowcast_latest.json"
CHECK_PATH = BASE_DIR / "data" / "experiments" / "short_nowcast_check.json"
PARAMS = dict(n_estimators=400, learning_rate=0.05, num_leaves=31, min_child_samples=40, subsample=0.8, subsample_freq=1,
              colsample_bytree=0.8, verbose=-1)
FEATURES = ["g_t", "g_t1", "g_t2", "river_t", "wide_t", "edge_t", "dark_t", "edge_t1", "frac_river_region",
            "am_temp", "am_rh", "am_dd", "am_wind", "am_dtemp1", "am_drh1", "am_dtemp3", "hour_sin", "hour_cos", "lead",
            "station_rate", "minutes_since_sunrise"]
log = logging.getLogger("short_nowcast")


def _group(code):
    if code is None or (isinstance(code, float) and math.isnan(code)) or code < 0:
        return -1
    c = int(code)
    c = c // 100 if c > 10 else c
    return 0 if c == 0 else (1 if c in (1, 2, 3) else (2 if c in (4, 5, 6) else (3 if c in (7, 8) else 4)))


def _cameras():
    out = {}
    with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("緯度") and r.get("経度"):
                out[r["地点名"]] = {"lat": float(r["緯度"]), "lon": float(r["経度"]), "region": r.get("地域") or ""}
    return out


def load_frame(since=FROM):
    """地点×毎時の表(画像の判定・かすみ度・アメダス・日の出からの時間)。"""
    import amedas
    from fog_features import sun_times_hours
    cams = _cameras()
    w = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'weather.db'}?mode=ro", uri=True)
    ph = pd.read_sql("SELECT datetime, station_name AS station, code FROM phenomena WHERE datetime >= ?", w, params=(since,))
    w.close()
    u = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'users.db'}?mode=ro", uri=True)
    corr = pd.read_sql("SELECT target_datetime AS datetime, station_name AS station, corrected_code FROM phenomena_corrections "
                       "WHERE target_datetime >= ?", u, params=(since,))
    u.close()
    ph = ph.merge(corr, on=["datetime", "station"], how="left")
    ph["code"] = ph["corrected_code"].where(ph["corrected_code"].notna(), ph["code"])
    ph = ph[ph["station"].isin(cams)]
    ph["g"] = ph["code"].map(_group)
    ph["dt"] = pd.to_datetime(ph["datetime"])
    try:
        hc = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'haze.db'}?mode=ro", uri=True)
        hz = pd.read_sql("SELECT datetime, station, edge_ratio AS edge, dark_diff AS dark FROM haze WHERE datetime >= ?", hc, params=(since,))
        hc.close()
    except Exception:
        hz = pd.DataFrame(columns=["datetime", "station", "edge", "dark"])
    ph = ph.merge(hz, on=["datetime", "station"], how="left")
    near = {s: amedas.nearest(c["lat"], c["lon"])[0] for s, c in cams.items()}
    try:
        ac = sqlite3.connect(f"file:{amedas.DB}?mode=ro", uri=True)
        am = pd.read_sql("SELECT station AS am, datetime, temp, dew_point, humidity, wind FROM obs WHERE datetime >= ?", ac,
                         params=(since,))
        ac.close()
    except Exception:
        am = pd.DataFrame(columns=["am", "datetime", "temp", "dew_point", "humidity", "wind"])
    am["dt"] = pd.to_datetime(am["datetime"])
    am = am.sort_values(["am", "dt"])
    for col, lag, name in (("temp", 1, "am_dtemp1"), ("humidity", 1, "am_drh1"), ("temp", 3, "am_dtemp3")):
        prev = am[["am", "dt", col]].copy()
        prev["dt"] = prev["dt"] + pd.Timedelta(hours=lag)
        am = am.merge(prev.rename(columns={col: "_p"}), on=["am", "dt"], how="left")
        am[name] = am[col] - am["_p"]
        am = am.drop(columns="_p")
    am = am.rename(columns={"temp": "am_temp", "humidity": "am_rh", "wind": "am_wind"})
    am["am_dd"] = am["am_temp"] - am["dew_point"]
    ph["am"] = ph["station"].map(near)
    ph = ph.merge(am[["am", "dt", "am_temp", "am_rh", "am_dd", "am_wind", "am_dtemp1", "am_drh1", "am_dtemp3"]],
                  on=["am", "dt"], how="left")
    ph["region"] = ph["station"].map(lambda s: cams[s]["region"])
    sun = {}
    def since_sunrise(st, dt):
        key = (st, dt.date())
        if key not in sun:
            sun[key] = sun_times_hours(pd.Timestamp(dt.date()), cams[st]["lat"], cams[st]["lon"])
        rise, set_ = sun[key]
        h = dt.hour + dt.minute / 60
        return (h - rise) * 60 if rise <= h <= set_ else np.nan
    ph["minutes_since_sunrise"] = [since_sunrise(s, d) for s, d in zip(ph["station"], ph["dt"])]
    return ph.sort_values(["station", "dt"]).reset_index(drop=True)


def build_rows(ph, for_predict_at=None):
    """学習・評価用の行(t, lead) → ラベル(t+leadの川霧)。for_predict_atを渡すと、その時刻tの行だけ(ラベル無し)。"""
    base = ph[["station", "dt", "g", "edge", "dark", "region", "am_temp", "am_rh", "am_dd", "am_wind",
               "am_dtemp1", "am_drh1", "am_dtemp3", "minutes_since_sunrise"]].copy()
    def shifted(col, hours):
        """hours時間前の値(同じ地点)。"""
        d = {(s, t): v for s, t, v in zip(base["station"], base["dt"], base[col])}
        lag = pd.Timedelta(hours=hours)
        return [d.get((s, t - lag), np.nan) for s, t in zip(base["station"], base["dt"])]
    base["g_t"] = base["g"]
    base["g_t1"] = list(shifted("g", 1))
    base["g_t2"] = list(shifted("g", 2))
    base["edge_t1"] = list(shifted("edge", 1))
    base["edge_t"], base["dark_t"] = base["edge"], base["dark"]
    base["river_t"] = (base["g"] == 1).astype(int)
    base["wide_t"] = (base["g"] == 2).astype(int)
    valid = base[base["g"] >= 0]
    frac = valid.groupby(["region", "dt"])["river_t"].mean().rename("frac_river_region")
    base = base.join(frac, on=["region", "dt"])
    base = base[base["minutes_since_sunrise"].notna()]  # tが日中の行だけ
    if for_predict_at is not None:
        base = base[base["dt"] == for_predict_at]
    rows = []
    label_map = ({(s, d): g for s, d, g in zip(ph["station"], ph["dt"], ph["g"])} if for_predict_at is None else None)
    for lead in LEADS:
        part = base.copy()
        part["lead"] = lead
        target = part["dt"] + pd.Timedelta(hours=lead)
        part["target_dt"] = target
        part["hour_sin"] = np.sin(2 * np.pi * target.dt.hour / 24)
        part["hour_cos"] = np.cos(2 * np.pi * target.dt.hour / 24)
        if label_map is not None:
            g = [label_map.get((s, t), np.nan) for s, t in zip(part["station"], target)]
            part["y_g"] = g
        rows.append(part)
    out = pd.concat(rows, ignore_index=True)
    if label_map is not None:
        out = out[out["y_g"].notna() & (out["y_g"] >= 0)]
        out["y"] = (out["y_g"] == 1).astype(int)
    return out


def _station_rates(train):
    r = train.groupby("station")["y"].agg(["sum", "count"])
    g = train["y"].mean()
    return ((r["sum"] + 20 * g) / (r["count"] + 20)).to_dict(), g


def _fit(df, rates, g):
    import lightgbm as lgb
    X = df.assign(station_rate=df["station"].map(rates).fillna(g))[FEATURES]
    m = lgb.LGBMClassifier(**PARAMS)
    m.fit(X, df["y"])
    return m


def _proba(m, df, rates, g):
    return m.predict_proba(df.assign(station_rate=df["station"].map(rates).fillna(g))[FEATURES])[:, 1]


def _best_threshold(y, p):
    best = (0.0, 0.5)
    for t in np.arange(0.05, 0.9, 0.01):
        pred = p >= t
        tp, fp, fn = int((pred & (y == 1)).sum()), int((pred & (y == 0)).sum()), int((~pred & (y == 1)).sum())
        f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
        if f1 > best[0]:
            best = (f1, round(float(t), 2))
    return best[1]


PUSH_MIN_PRECISION = 0.35
PUSH_MIN_TP = 5


def _push_threshold(y, p):
    """通知用(1時間後だけ): 検証期間で「当たる割合」がPUSH_MIN_PRECISION以上になる、いちばん低いしきい値。
    当たりがPUSH_MIN_TP件未満しか残らないなら、通知はしない(None)。"""
    for t in np.arange(0.05, 0.95, 0.01):
        pred = p >= t
        tp, fp = int((pred & (y == 1)).sum()), int((pred & (y == 0)).sum())
        if tp < PUSH_MIN_TP:
            return None
        if tp / (tp + fp) >= PUSH_MIN_PRECISION:
            return round(float(t), 2)
    return None


def _metrics(y, p, thr):
    from sklearn.metrics import average_precision_score
    pred = p >= thr
    tp, fp, fn = int((pred & (y == 1)).sum()), int((pred & (y == 0)).sum()), int((~pred & (y == 1)).sum())
    return {"n": int(len(y)), "fog": int(y.sum()), "ap": round(float(average_precision_score(y, p)), 4) if y.sum() else None,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None, "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "f1": round(2 * tp / (2 * tp + fp + fn), 3) if tp else 0.0}


def _production_probs(rows):
    """各行の対象時刻に対する、前日夜(その日の0時より前)の本番の予測の確率。"""
    import prediction_log
    c = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'predictions.db'}?mode=ro", uri=True)
    try:
        q = pd.read_sql("SELECT station, target_datetime, predicted_fog_prob, run_at FROM predictions WHERE model_type LIKE ? "
                        "AND model_type NOT LIKE '%[再予測]' AND target_datetime >= ?", c,
                        params=(prediction_log.CURRENT_MODEL_LIKE, rows["target_dt"].min().strftime("%Y-%m-%d %H:%M:%S")))
    finally:
        c.close()
    q = q[q["run_at"] < q["target_datetime"].str[:10] + " 00:00:00"]
    q = q.sort_values("run_at").drop_duplicates(["station", "target_datetime"], keep="last")
    m = {(s, t): p for s, t, p in zip(q["station"], q["target_datetime"], q["predicted_fog_prob"])}
    return np.array([m.get((s, t.strftime("%Y-%m-%d %H:%M:%S")), np.nan) for s, t in zip(rows["station"], rows["target_dt"])])


def train():
    ph = load_frame()
    rows = build_rows(ph)
    end = rows["dt"].max()
    test_start = end - pd.Timedelta(days=TEST_DAYS)
    val_start = test_start - pd.Timedelta(days=VAL_DAYS)
    inner, val, test = rows[rows["dt"] < val_start], rows[(rows["dt"] >= val_start) & (rows["dt"] < test_start)], rows[rows["dt"] >= test_start]
    log.info("rows: inner %d (%d fog) / val %d (%d fog) / test %d (%d fog)", len(inner), inner["y"].sum(), len(val),
             val["y"].sum(), len(test), test["y"].sum())
    rates, g = _station_rates(inner)
    m = _fit(inner, rates, g)
    p_val = _proba(m, val, rates, g)
    thr = _best_threshold(val["y"].to_numpy(), p_val)
    v1 = (val["lead"] == 1).to_numpy()
    push_thr = _push_threshold(val["y"].to_numpy()[v1], p_val[v1])
    train_df = rows[rows["dt"] < test_start]
    rates2, g2 = _station_rates(train_df)
    m2 = _fit(train_df, rates2, g2)
    p = _proba(m2, test, rates2, g2)
    y = test["y"].to_numpy()
    persist = (test["g_t"] == 1).astype(float).to_numpy()
    prod = _production_probs(test)
    try:
        import fog_model
        prod_threshold = float((fog_model.shared_global_metrics(fog_model.load_model_state()) or {}).get("threshold") or 0.19)
    except Exception:
        prod_threshold = 0.19
    res = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "test_start": str(test_start), "test_end": str(end),
           "threshold": thr, "by_lead": {}}
    for lead in LEADS:
        k = (test["lead"] == lead).to_numpy()
        ok = k & ~np.isnan(prod)
        prod_thr = prod_threshold
        res["by_lead"][str(lead)] = {
            "model": _metrics(y[k], p[k], thr),
            "persistence": _metrics(y[k], persist[k], 0.5),
            "model_same_rows_as_production": _metrics(y[ok], p[ok], thr),
            "production_forecast": _metrics(y[ok], prod[ok], prod_thr),
        }
    k1 = (test["lead"] == 1).to_numpy()
    res["push_threshold"] = push_thr
    res["push_test"] = _metrics(y[k1], p[k1], push_thr) if push_thr is not None else None
    imp = dict(zip(FEATURES, (m2.booster_.feature_importance("gain") / max(1, m2.booster_.feature_importance("gain").sum())).round(4)))
    res["importance"] = dict(sorted(imp.items(), key=lambda kv: -kv[1])[:10])
    CHECK_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    # 本番用: 全期間で学習し直す(しきい値は検証期間で決めたもの)
    rates3, g3 = _station_rates(rows)
    final = _fit(rows, rates3, g3)
    joblib.dump({"model": final, "rates": rates3, "global": g3, "threshold": thr, "push_threshold": push_thr,
                 "trained_at": res["generated_at"],
                 "check": res["by_lead"]}, MODEL_PATH)
    log.info("short nowcast: %s", json.dumps(res["by_lead"], ensure_ascii=False))
    return res


def predict():
    if not MODEL_PATH.exists():
        log.info("No short nowcast model yet")
        return
    st = joblib.load(MODEL_PATH)
    since = (datetime.now(JST) - timedelta(hours=8)).strftime("%Y-%m-%d %H:00:00")
    ph = load_frame(since)
    if ph.empty:
        return
    t = ph[ph["g"] >= 0]["dt"].max()
    rows = build_rows(ph, for_predict_at=t)
    if rows.empty:
        LATEST_PATH.write_text(json.dumps({"issued_at": str(t), "stations": {}}, ensure_ascii=False), encoding="utf-8")
        return
    p = _proba(st["model"], rows, st["rates"], st["global"])
    out = {}
    for (s, lead, target), prob in zip(zip(rows["station"], rows["lead"], rows["target_dt"]), p):
        out.setdefault(s, {})[str(int(target.hour))] = round(float(prob), 3)
    LATEST_PATH.write_text(json.dumps({"issued_at": t.strftime("%Y-%m-%d %H:%M:%S"), "threshold": st["threshold"],
                                       "push_threshold": st.get("push_threshold"), "stations": out},
                                      ensure_ascii=False), encoding="utf-8")
    log.info("short nowcast issued at %s for %d station(s)", t, len(out))


def latest():
    """地点ページ用(3時間より古い発表は使わない)。"""
    if not LATEST_PATH.exists():
        return None
    d = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    try:
        issued = datetime.strptime(d["issued_at"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError):
        return None
    if datetime.now(JST).replace(tzinfo=None) - issued > timedelta(hours=3):
        return None
    return d


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--predict", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.FileHandler(BASE_DIR / "logs" / "short_nowcast.log", encoding="utf-8"), logging.StreamHandler()])
    if args.train:
        train()
    if args.predict:
        predict()
