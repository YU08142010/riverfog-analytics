"""境界層の高さ・逆転の強さを特徴量に足すと当たりやすくなるかの確かめ直し(2026-10-11作成、10/22に1回だけ実行)。

2026-10-08の実験(README「試して採らなかった改良」)では、この2つだけを足すとAP +0.009・loglossも有意に改善したが、
3期間のうち最後の期間はAPが下がり、全部入れた結果を見てから選んだ組み合わせだった。データが増えた新しい期間で確かめ直す。

方法(前回と同じ考え方): 過去に実際に出されていた予報(Open-Meteo historical-forecast-api、既定=JMA)を入力に、
直近6週を2週×3にずらし、各期間より前のデータだけで学習(その直前2週でearly stopping)して、その期間を採点する。
  A: いまの特徴量 + 地点別発生率 + 地形
  B: A + 境界層高度(boundary_layer_height) + 逆転強度(850hPaの気温 − 地上の気温)
AP・loglossと、その差の日単位ブートストラップ95%区間を data/experiments/bl_recheck.json に書き、管理者に通知する。

  .venv/bin/python bl_recheck.py          (crontab: 10/22 23:30。実行済みの印があれば何もしない)
  .venv/bin/python bl_recheck.py --force  (いつでも実行)
"""
import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, log_loss

import fog_data
import fog_model as fm
import station_weather
from common import BASE_DIR, CAMERAS_CSV, JST, STATION_WEATHER_DB_PATH, representative_coords
from fog_features import prepare_features

OUT = BASE_DIR / "data" / "experiments" / "bl_recheck.json"
DONE = BASE_DIR / "data" / "experiments" / "bl_recheck.done"
RUN_ON_OR_AFTER = "2026-10-22"
FOLDS, FOLD_DAYS, VAL_DAYS = 3, 14, 14
EXTRA_VARS = ["boundary_layer_height", "temperature_850hPa"]
EXTRA_FEATURES = ["境界層高度", "逆転強度"]
PARAMS = dict(num_leaves=31, min_child_samples=30, **fm.LGBM_SAMPLING)
CHUNK_DAYS, CHUNK_STATIONS = 60, 16
HISTORY_DAYS = 120
MIN_INNER_ROWS = 2000
log = logging.getLogger("bl_recheck")


def fetch_history(coords, start, end):
    """{地点: DataFrame(本番と同じ列名 + 境界層高度・逆転強度)}。期間と地点を分けて取得する。"""
    out = {}
    names = list(coords)
    for i in range(0, len(names), CHUNK_STATIONS):
        group = names[i:i + CHUNK_STATIONS]
        d = start
        while d <= end:
            e = min(end, d + timedelta(days=CHUNK_DAYS - 1))
            params = {**fm._common_params(), "start_date": str(d), "end_date": str(e),
                      "latitude": ",".join(str(coords[n][0]) for n in group),
                      "longitude": ",".join(str(coords[n][1]) for n in group)}
            params["hourly"] += "," + ",".join(EXTRA_VARS)
            payload = None
            for attempt in range(6):
                try:
                    payload = fm._get_forecast_with_retry(params, timeout=120, url=fm.OPEN_METEO_HISTORICAL_FORECAST_URL).json()
                    break
                except Exception as e:  # noqa: BLE001  利用上限(429)なら1分、空の応答などの一時的な失敗なら15秒待ってやり直す
                    if attempt == 5:
                        raise
                    wait = 60 if "429" in str(e) else 15
                    log.warning("Open-Meteo fetch failed (%s); waiting %ds (%d/5)", str(e)[:80], wait, attempt + 1)
                    time.sleep(wait)
            time.sleep(2)
            if isinstance(payload, dict):
                payload = [payload]
            for name, item in zip(group, payload):
                if not item or "hourly" not in item:
                    continue
                try:
                    df = fm._forecast_payload_to_df(item["hourly"])
                except ValueError:
                    continue
                h = item["hourly"]
                extra = pd.DataFrame({"datetime": pd.to_datetime(h["time"]),
                                      "境界層高度": pd.to_numeric(pd.Series(h.get("boundary_layer_height")), errors="coerce"),
                                      "_t850": pd.to_numeric(pd.Series(h.get("temperature_850hPa")), errors="coerce")})
                df = df.merge(extra, on="datetime", how="left")
                df["逆転強度"] = df["_t850"] - df[fm.COL_TEMP]
                out.setdefault(name, []).append(df.drop(columns="_t850"))
            d = e + timedelta(days=1)
    return {n: pd.concat(parts, ignore_index=True).drop_duplicates("datetime").sort_values("datetime") for n, parts in out.items()}


def build_dataset():
    main_df, phenom_df, cols = fog_data.load_river_fog_data()
    lat, lon = representative_coords()
    coords = {}
    with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("緯度") and row.get("経度"):
                coords[row["地点名"]] = (float(row["緯度"]), float(row["経度"]))
    wconn = station_weather.connect(STATION_WEATHER_DB_PATH)
    try:
        frame, wf, medians, station_coords = fm._build_training_frame(main_df, phenom_df, cols, lat, lon,
                                                                     camera_coords=coords, weather_conn=wconn)
    finally:
        wconn.close()
    # 比べるのは直近6週。学習にはその前のHISTORY_DAYS日まで使う(全期間の予報を取るとOpen-Meteoの利用上限に当たる)
    frame = frame[frame["__datetime"] >= frame["__datetime"].max() - pd.Timedelta(days=HISTORY_DAYS)]
    start = (frame["__datetime"].min() - pd.Timedelta(days=2)).date()
    end = frame["__datetime"].max().date()
    log.info("Labels: %d rows (%d fog), %s - %s; fetching historical forecasts", len(frame), int(frame["__y"].sum()), start, end)
    hist = fetch_history(station_coords, start, end)
    parts = []
    for st, sub in frame.groupby("__station"):
        fc = hist.get(st)
        if fc is None or fc.empty:
            continue
        s_lat, s_lon = station_coords[st]
        feats, _, _ = prepare_features(fc, s_lat, s_lon, wf, medians=medians)
        feats = feats[["datetime", *wf]].merge(fc[["datetime", *EXTRA_FEATURES]], on="datetime", how="left")
        m = sub[["__datetime", "__station", "__y"]].merge(feats, left_on="__datetime", right_on="datetime").drop(columns="datetime")
        parts.append(m.dropna(subset=wf))
    data = pd.concat(parts, ignore_index=True).sort_values(["__datetime", "__station"]).reset_index(drop=True)
    static = fm.load_station_terrain({s: c for s, c in coords.items() if s in station_coords})
    return data, wf, static


def run_fold(data, wf, static, fold_start, fold_end):
    train = data[data["__datetime"] < fold_start]
    val_start = fold_start - pd.Timedelta(days=VAL_DAYS)
    inner, val = train[train["__datetime"] < val_start], train[train["__datetime"] >= val_start]
    test = data[(data["__datetime"] >= fold_start) & (data["__datetime"] < fold_end)]
    if len(inner) < MIN_INNER_ROWS or inner["__y"].nunique() < 2 or val["__y"].nunique() < 2 or test["__y"].sum() == 0:
        log.warning("Fold %s skipped: too little data before it (inner %d rows)", fold_start.date(), len(inner))
        return test, None
    res = {}
    for name, extra in (("A", []), ("B", EXTRA_FEATURES)):
        feats = list(wf) + extra
        rates, g = fm._station_rates(inner)
        Xi, Xv = fm._features_with_rate(inner, feats, rates, g, static=static), fm._features_with_rate(val, feats, rates, g, static=static)
        m = fm._fit_lgbm(Xi, inner["__y"].to_numpy(), PARAMS, fm.MAX_ESTIMATORS, eval_set=(Xv, val["__y"].to_numpy()),
                         sample_weight=fm._recency_weights(inner, val_start))
        n = max(50, int(round((m.best_iteration_ or fm.MAX_ESTIMATORS) * fm.REFIT_ESTIMATOR_SCALE)))
        rates, g = fm._station_rates(train)
        final = fm._fit_lgbm(fm._features_with_rate(train, feats, rates, g, static=static), train["__y"].to_numpy(), PARAMS, n,
                             sample_weight=fm._recency_weights(train, fold_start))
        res[name] = final.predict_proba(fm._features_with_rate(test, feats, rates, g, static=static))[:, 1]
    return test, res


def bootstrap_diff(days, y, pa, pb, n=1000, seed=0):
    rng = np.random.default_rng(seed)
    uniq = np.unique(days)
    idx_by_day = {d: np.where(days == d)[0] for d in uniq}
    ap_d, ll_d = [], []
    for _ in range(n):
        pick = np.concatenate([idx_by_day[d] for d in rng.choice(uniq, len(uniq))])
        if y[pick].sum() == 0:
            continue
        ap_d.append(average_precision_score(y[pick], pb[pick]) - average_precision_score(y[pick], pa[pick]))
        ll_d.append(log_loss(y[pick], pb[pick], labels=[0, 1]) - log_loss(y[pick], pa[pick], labels=[0, 1]))
    return {"ap_diff_95": [round(float(np.percentile(ap_d, 2.5)), 4), round(float(np.percentile(ap_d, 97.5)), 4)],
            "logloss_diff_95": [round(float(np.percentile(ll_d, 2.5)), 5), round(float(np.percentile(ll_d, 97.5)), 5)]}


def main(force=False):
    today = datetime.now(JST).strftime("%Y-%m-%d")
    if not force and (DONE.exists() or today < RUN_ON_OR_AFTER):
        log.info("Not scheduled to run (done=%s, today=%s)", DONE.exists(), today)
        return
    data, wf, static = build_dataset()
    end = data["__datetime"].max().normalize() + pd.Timedelta(days=1)
    folds, ys, pas, pbs, days = [], [], [], [], []
    for k in range(FOLDS, 0, -1):
        fs, fe = end - pd.Timedelta(days=FOLD_DAYS * k), end - pd.Timedelta(days=FOLD_DAYS * (k - 1))
        test, res = run_fold(data, wf, static, fs, fe)
        if res is None:
            folds.append({"start": str(fs.date()), "skipped": "学習に使えるデータが足りない"})
            continue
        y = test["__y"].to_numpy()
        f = {"start": str(fs.date()), "end": str((fe - pd.Timedelta(days=1)).date()), "n": int(len(y)), "fog": int(y.sum())}
        for name in ("A", "B"):
            f[name] = {"ap": round(float(average_precision_score(y, res[name])), 4) if y.sum() else None,
                       "logloss": round(float(log_loss(y, res[name], labels=[0, 1])), 5)}
        folds.append(f)
        ys.append(y); pas.append(res["A"]); pbs.append(res["B"]); days.append(test["__datetime"].dt.strftime("%Y-%m-%d").to_numpy())
        log.info("Fold %s: %s", f["start"], f)
    if not ys:
        log.error("No fold had enough data; nothing to compare")
        return
    y, pa, pb, dd = np.concatenate(ys), np.concatenate(pas), np.concatenate(pbs), np.concatenate(days)
    out = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "features_added": EXTRA_FEATURES,
           "folds": folds, "overall": {"n": int(len(y)), "fog": int(y.sum()),
                                       "A": {"ap": round(float(average_precision_score(y, pa)), 4),
                                             "logloss": round(float(log_loss(y, pa, labels=[0, 1])), 5)},
                                       "B": {"ap": round(float(average_precision_score(y, pb)), 4),
                                             "logloss": round(float(log_loss(y, pb, labels=[0, 1])), 5)}},
           "bootstrap_by_day": bootstrap_diff(dd, y, pa, pb)}
    better_ap = out["bootstrap_by_day"]["ap_diff_95"][0] > 0
    better_ll = out["bootstrap_by_day"]["logloss_diff_95"][1] < 0
    done = [f for f in folds if "A" in f]
    all_folds = len(done) == FOLDS and all(f["B"]["ap"] is not None and f["A"]["ap"] is not None and f["B"]["ap"] >= f["A"]["ap"] for f in done)
    out["verdict"] = ("採用を検討(APとloglossが有意に改善し、3期間すべてでAPが下がらない)" if better_ap and better_ll and all_folds
                      else "見送り(有意な改善が無い、またはAPが下がる期間がある)")
    (OUT.with_name("bl_recheck_trial.json") if force else OUT).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                                           encoding="utf-8")
    if force:
        log.info("Trial run (--force): not marking as done, not notifying. Verdict: %s", out["verdict"])
        return
    DONE.write_text(out["generated_at"], encoding="utf-8")
    log.info("Result: %s", out["verdict"])
    try:
        import push_notify
        import users
        from common import now_jst_naive
        db = BASE_DIR / "data" / "users.db"
        push_notify.init(db, now_jst_naive)
        conn = users.connect(db)
        admins = [u["id"] for u in users.list_users(conn) if u["role"] == "admin"]
        conn.close()
        o = out["overall"]
        body = f"AP {o['A']['ap']}→{o['B']['ap']}、{out['verdict']}"
        for uid in admins:
            push_notify.deliver(uid, "admin", "境界層・逆転の強さの再検証が終わりました", body, "/admin", "bl-recheck",
                                key="bl_recheck", ttl=3 * 86400)
    except Exception as e:  # noqa: BLE001
        log.warning("Notification failed: %s", e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    main(args.force)
