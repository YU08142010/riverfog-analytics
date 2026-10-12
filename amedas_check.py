"""アメダスの実測を本番の予報(predict.py)の特徴量に足すと当たりやすくなるかの比較(2026-10-12、本番は変えない)。

本番の予報は前日の夜(19:30)に出すので、使えるのはその夜までの実測だけ。対象の日の前日の、地点の最寄りのアメダスの
  18時の気温・湿度・露点との差(気温−露点)・風速、前日1日の降水量・日照時間、前日の最高気温−18時の気温(冷え方)
を足す(B)。いまの特徴量(A)と、bl_recheck.pyと同じ方法(過去に実際に出されていた予報を入力に、期間をずらして、
各期間より前のデータだけで学習)で比べる。アメダスのデータは2026-08-15からなので、A・Bとも2026-08-16以降の行だけを使い、
直近をFOLD_DAYS日×FOLDSに分ける。データが少ないうちは差が出にくいので、毎月3日01:00に比べ直して管理者に知らせる。
結果は data/experiments/amedas_check.json。「採用を検討」が出ても自動では本番に入れない。

  .venv/bin/python amedas_check.py           (通知する)
  .venv/bin/python amedas_check.py --quiet   (通知しない)
"""
import argparse
import csv
import json
import logging
import sqlite3
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, log_loss

import amedas
import bl_recheck as bl
from common import BASE_DIR, CAMERAS_CSV, JST

OUT = BASE_DIR / "data" / "experiments" / "amedas_check.json"
DATA_FROM = "2026-08-16"
FOLDS, FOLD_DAYS, VAL_DAYS = 3, 10, 10
AM_FEATURES = ["前日18時の気温(実測)", "前日18時の湿度(実測)", "前日18時の気温露点差(実測)", "前日18時の風速(実測)",
               "前日の降水量(実測)", "前日の日照時間(実測)", "前日の冷え方(実測)"]
log = logging.getLogger("amedas_check")


def daily_features():
    """{(アメダス地点, 日付): [特徴量...]}(その日の値。対象の日の前日として使う)。"""
    c = sqlite3.connect(f"file:{amedas.DB}?mode=ro", uri=True)
    try:
        d = pd.read_sql("SELECT station, datetime, temp, humidity, dew_point, wind, precip, sun FROM obs", c)
    finally:
        c.close()
    d["dt"] = pd.to_datetime(d["datetime"])
    # 気象庁の「1〜24時」の24時は翌日0時として入っているので、1時間戻してその日に入れる
    d["day"] = (d["dt"] - pd.Timedelta(hours=1)).dt.strftime("%Y-%m-%d")
    out = {}
    for (st, day), g in d.groupby(["station", "day"]):
        at18 = g[g["dt"].dt.hour == 18]
        if at18.empty or len(g) < 20:
            continue
        r = at18.iloc[0]
        dd = r["temp"] - r["dew_point"] if pd.notna(r["temp"]) and pd.notna(r["dew_point"]) else np.nan
        out[(st, day)] = [r["temp"], r["humidity"], dd, r["wind"], g["precip"].fillna(0).sum(), g["sun"].fillna(0).sum(),
                          g["temp"].max() - r["temp"] if pd.notna(r["temp"]) else np.nan]
    return out


def add_amedas(data):
    coords = {}
    with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("緯度") and row.get("経度"):
                coords[row["地点名"]] = (float(row["緯度"]), float(row["経度"]))
    near = {s: amedas.nearest(*c)[0] for s, c in coords.items()}
    feats = daily_features()
    prev = (data["__datetime"].dt.normalize() - pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    vals = np.array([feats.get((near.get(s), p), [np.nan] * len(AM_FEATURES)) for s, p in zip(data["__station"], prev)],
                    dtype=float)
    for i, name in enumerate(AM_FEATURES):
        data[name] = vals[:, i]
    return data


def main(notify=True):
    bl.HISTORY_DAYS = (pd.Timestamp.now() - pd.Timestamp(DATA_FROM)).days + 30  # 予報の取得は必要な分だけ
    bl.VAL_DAYS = VAL_DAYS
    data, wf, static = bl.build_dataset()
    data = data[data["__datetime"] >= DATA_FROM].reset_index(drop=True)
    data = add_amedas(data)
    coverage = float(data[AM_FEATURES[0]].notna().mean())
    log.info("Rows %d (%d fog), AMeDAS coverage %.1f%%", len(data), int(data["__y"].sum()), coverage * 100)
    bl.EXTRA_FEATURES = AM_FEATURES  # run_foldの「B」に足す列
    end = data["__datetime"].max().normalize() + pd.Timedelta(days=1)
    folds, ys, pas, pbs, days = [], [], [], [], []
    for k in range(FOLDS, 0, -1):
        fs, fe = end - pd.Timedelta(days=FOLD_DAYS * k), end - pd.Timedelta(days=FOLD_DAYS * (k - 1))
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
        log.error("No fold had enough data; nothing to compare")
        return None
    y, pa, pb, dd = np.concatenate(ys), np.concatenate(pas), np.concatenate(pbs), np.concatenate(days)
    out = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "data_from": DATA_FROM, "features_added": AM_FEATURES,
           "amedas_coverage": round(coverage, 3), "folds": folds,
           "overall": {"n": int(len(y)), "fog": int(y.sum()),
                       "A": {"ap": round(float(average_precision_score(y, pa)), 4), "logloss": round(float(log_loss(y, pa, labels=[0, 1])), 5)},
                       "B": {"ap": round(float(average_precision_score(y, pb)), 4), "logloss": round(float(log_loss(y, pb, labels=[0, 1])), 5)}},
           "bootstrap_by_day": bl.bootstrap_diff(dd, y, pa, pb)}
    better_ap = out["bootstrap_by_day"]["ap_diff_95"][0] > 0
    better_ll = out["bootstrap_by_day"]["logloss_diff_95"][1] < 0
    done = [f for f in folds if "A" in f]
    all_folds = len(done) == FOLDS and all(f["B"]["ap"] is not None and f["A"]["ap"] is not None and f["B"]["ap"] >= f["A"]["ap"] for f in done)
    out["verdict"] = ("採用を検討(APとloglossが有意に改善し、すべての期間でAPが下がらない)" if better_ap and better_ll and all_folds
                      else "見送り(有意な改善が無い、またはAPが下がる期間がある)")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Result: %s %s", out["overall"], out["verdict"])
    if notify:
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
            for uid in admins:
                push_notify.deliver(uid, "admin", "アメダスの実測を予報に足す比較が終わりました",
                                    f"AP {o['A']['ap']}→{o['B']['ap']}、{out['verdict']}", "/admin", "amedas-check",
                                    key=f"amedas_check:{out['generated_at'][:10]}", ttl=3 * 86400)
        except Exception as e:  # noqa: BLE001
            log.warning("Notification failed: %s", e)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    main(notify=not args.quiet)
