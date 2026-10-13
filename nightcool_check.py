"""夜間の冷え込みの特徴量を足すと当たりやすくなるかの比較(2026-10-13、本番は変えない)。

川霧(放射霧)は、晴れて風の弱い夜に地面や川面付近が冷え込み、空気が飽和に近づくと出やすい。いまの特徴量には
直前1〜3時間の気温変化はあるが、夜を通した冷え込みはない。そこで、過去に実際に出された予報の毎時の値から、
各時刻について次の3つを作って足す(B)。
  - 夜間冷え込み_12h: 直前12時間(その時刻を含む)の最高気温 − その時刻の気温
  - 静穏時間_12h: 直前12時間のうち、風速が CALM_WIND m/s 未満だった時間の数
  - 湿数最小_12h: 直前12時間の、気温と露点温度の差(湿数)の最小値
いまの特徴量(A)と、bl_recheck.py と同じ方法(過去の予報を入力に、直近を2週×3期間、各期間より前だけで学習)で比べ、
data/experiments/nightcool_check.json に書く。「採用を検討」が出ても自動では本番に入れない。

実行: .venv/bin/python nightcool_check.py
"""
import csv
import json
import logging
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, log_loss

import bl_recheck as bl
import fog_data
import fog_model as fm
import station_weather
from common import BASE_DIR, CAMERAS_CSV, JST, STATION_WEATHER_DB_PATH, representative_coords
from fog_features import COL_DEWPOINT, COL_TEMP, COL_WIND_SPEED, prepare_features

OUT = BASE_DIR / "data" / "experiments" / "nightcool_check.json"
NIGHT_FEATURES = ["夜間冷え込み_12h", "静穏時間_12h", "湿数最小_12h"]
CALM_WIND = 1.5
WINDOW = "12h"
log = logging.getLogger("nightcool_check")


def night_features(fc):
    """予報の毎時の表(datetime列つき)に、夜間の冷え込みの3つの列を足す。"""
    s = fc.sort_values("datetime").set_index("datetime")
    temp, wind = s[COL_TEMP].astype(float), s[COL_WIND_SPEED].astype(float)
    dd = temp - s[COL_DEWPOINT].astype(float)
    out = pd.DataFrame({
        "datetime": s.index,
        "夜間冷え込み_12h": (temp.rolling(WINDOW, min_periods=6).max() - temp).to_numpy(),
        "静穏時間_12h": (wind < CALM_WIND).astype(float).rolling(WINDOW, min_periods=6).sum().to_numpy(),
        "湿数最小_12h": dd.rolling(WINDOW, min_periods=6).min().to_numpy(),
    })
    return out


def build_dataset():
    """bl_recheck.build_dataset と同じ作り方で、境界層の列の代わりに夜間の冷え込みの列を付ける。"""
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
    frame = frame[frame["__datetime"] >= frame["__datetime"].max() - pd.Timedelta(days=bl.HISTORY_DAYS)]
    start = (frame["__datetime"].min() - pd.Timedelta(days=2)).date()
    end = frame["__datetime"].max().date()
    log.info("Labels: %d rows (%d fog), %s - %s; fetching historical forecasts", len(frame), int(frame["__y"].sum()), start, end)
    hist = bl.fetch_history(station_coords, start, end)
    parts = []
    for st, sub in frame.groupby("__station"):
        fc = hist.get(st)
        if fc is None or fc.empty:
            continue
        s_lat, s_lon = station_coords[st]
        feats, _, _ = prepare_features(fc, s_lat, s_lon, wf, medians=medians)
        feats = feats[["datetime", *wf]].merge(night_features(fc), on="datetime", how="left")
        m = sub[["__datetime", "__station", "__y"]].merge(feats, left_on="__datetime", right_on="datetime").drop(columns="datetime")
        parts.append(m.dropna(subset=wf))
    data = pd.concat(parts, ignore_index=True).sort_values(["__datetime", "__station"]).reset_index(drop=True)
    static = fm.load_station_terrain({s: c for s, c in coords.items() if s in station_coords})
    return data, wf, static


def main():
    data, wf, static = build_dataset()
    bl.EXTRA_FEATURES = NIGHT_FEATURES  # bl.run_fold の「B」に足す列
    end = data["__datetime"].max().normalize() + pd.Timedelta(days=1)
    folds, ys, pas, pbs, days = [], [], [], [], []
    for k in range(bl.FOLDS, 0, -1):
        fs, fe = end - pd.Timedelta(days=bl.FOLD_DAYS * k), end - pd.Timedelta(days=bl.FOLD_DAYS * (k - 1))
        test, res = bl.run_fold(data, wf, static, fs, fe)
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
        log.error("No fold had enough data")
        return None
    y, pa, pb, dd = np.concatenate(ys), np.concatenate(pas), np.concatenate(pbs), np.concatenate(days)
    out = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "features_added": NIGHT_FEATURES, "folds": folds,
           "overall": {"n": int(len(y)), "fog": int(y.sum()),
                       "A": {"ap": round(float(average_precision_score(y, pa)), 4), "logloss": round(float(log_loss(y, pa, labels=[0, 1])), 5)},
                       "B": {"ap": round(float(average_precision_score(y, pb)), 4), "logloss": round(float(log_loss(y, pb, labels=[0, 1])), 5)}},
           "bootstrap_by_day": bl.bootstrap_diff(dd, y, pa, pb)}
    better_ap = out["bootstrap_by_day"]["ap_diff_95"][0] > 0
    better_ll = out["bootstrap_by_day"]["logloss_diff_95"][1] < 0
    done = [f for f in folds if "A" in f]
    all_folds = len(done) == bl.FOLDS and all(f["B"]["ap"] >= f["A"]["ap"] for f in done if f["A"]["ap"] is not None and f["B"]["ap"] is not None)
    out["verdict"] = ("採用を検討(APとloglossが有意に改善し、すべての期間でAPが下がらない)" if better_ap and better_ll and all_folds
                      else "見送り(有意な改善が無い、またはAPが下がる期間がある)")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Result: %s %s %s", out["overall"], out["bootstrap_by_day"], out["verdict"])
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    main()
