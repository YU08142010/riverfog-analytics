"""しきい値の決め方と地点別発生率の作り方の比較(2026-10-20、本番は変えない。結果を見て本番のしきい値の規則を変えた)。

本番(fog_model.train_shared_model)と同じ作り方を、直近を2週ずつずらした期間(FOLDS個、データの無い期間は飛ばす)で再現する:
  学習は実測(地点の気象)、検証(各期間の直前14日)と評価は過去に実際に出されていた予報(JMA)を入力。
比べるもの
  確率: P0 本番どおり / P1 学習行の地点別発生率を「自分の週を除いて」計算(目的変数の漏れを防ぐ案)
  しきい値: T_cur 検証期間でF1が最大になる確率(2026-10-19までの本番) / T_half 検証期間の最大のF1の半分(2026-10-20からの本番)
            / T_smooth F1曲線を±0.03で平滑化してから最大 / T_rate 検証期間の「霧あり」の数が実際の霧の数と同じになる値
差は評価期間の日を単位にしたブートストラップの95%区間。結果: data/experiments/accuracy_review_latest.json
(2026-10-20の結果は accuracy_review_20261020.json(3期間)・accuracy_review3_20261020.json(4期間)に残してある)
実行: .venv/bin/python accuracy_review.py
"""
import csv
import json
import logging
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, f1_score, log_loss

import bl_recheck as bl
import fog_data
import fog_model as fm
import station_weather
from common import BASE_DIR, CAMERAS_CSV, JST, STATION_WEATHER_DB_PATH, representative_coords

OUT = BASE_DIR / "data" / "experiments" / "accuracy_review_latest.json"
FOLDS, FOLD_DAYS, VAL_DAYS = 6, 14, 14  # 遡ってデータの無い期間は飛ばす
log = logging.getLogger("accuracy_review")
T0 = time.time()


def f1_at(y, p, t):
    pred = (p >= t).astype(int)
    tp = int(((pred == 1) & (y == 1)).sum()); fp = int(((pred == 1) & (y == 0)).sum()); fn = int(((pred == 0) & (y == 1)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {"t": round(float(t), 3), "f1": round(2 * tp / (2 * tp + fp + fn), 4) if tp + fp + fn else 0.0,
            "precision": round(prec, 4), "recall": round(rec, 4), "tp": tp, "fp": fp, "fn": fn}


def thresholds(y, p):
    grid = np.array(fm.FOG_THRESHOLD_GRID)
    f1s = np.array([f1_score(y, (p >= t).astype(int), zero_division=0) for t in grid])
    i = int(np.argmax(f1s))
    sm = np.convolve(f1s, np.ones(7) / 7, mode="same")
    j = int(np.argmax(sm))
    n_pos = int(y.sum())
    order = np.sort(p)[::-1]
    t_rate = float(order[n_pos - 1]) if 0 < n_pos <= len(order) else float(grid[i])
    return {"T_cur": float(grid[i]), "T_half": max(0.02, round(float(f1s[i]) / 2, 2)), "T_smooth": float(grid[j]),
            "T_rate": round(t_rate, 3), "val_f1_max": round(float(f1s[i]), 4)}


def oof_station_rates(rows, block_days=7):
    """各行の地点別発生率を、その行と同じ週(block)を除いた行から計算する(平滑化は本番と同じ)。"""
    blk = ((rows["__datetime"] - rows["__datetime"].min()).dt.days // block_days).to_numpy()
    st = rows["__station"].to_numpy(); y = rows["__y"].to_numpy()
    df = pd.DataFrame({"b": blk, "s": st, "y": y})
    tot = df.groupby("s")["y"].agg(["sum", "count"])
    by = df.groupby(["s", "b"])["y"].agg(["sum", "count"])
    g_sum, g_cnt = y.sum(), len(y)
    gb = df.groupby("b")["y"].agg(["sum", "count"])
    m = fm.STATION_RATE_SMOOTHING
    keys = list(zip(st, blk))
    s_sum = tot["sum"].reindex(st).to_numpy(); s_cnt = tot["count"].reindex(st).to_numpy()
    b_sum = by["sum"].reindex(keys).to_numpy(); b_cnt = by["count"].reindex(keys).to_numpy()
    gb_sum = gb["sum"].reindex(blk).to_numpy(); gb_cnt = gb["count"].reindex(blk).to_numpy()
    glob = (g_sum - gb_sum) / np.maximum(g_cnt - gb_cnt, 1)
    out = ((s_sum - b_sum) + m * glob) / ((s_cnt - b_cnt) + m)
    return out


def feats_oof(rows, wf, static):
    X = fm._features_with_rate(rows, wf, {}, 0.0, static=static)
    X[fm.STATION_FOG_RATE_FEATURE] = oof_station_rates(rows)
    return X


def run():
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
    static = fm.load_station_terrain({s: c for s, c in coords.items() if s in station_coords})
    end = frame["__datetime"].max().normalize() + pd.Timedelta(days=1)
    first = end - pd.Timedelta(days=FOLD_DAYS * FOLDS + VAL_DAYS + 2)
    log.info("frame %d rows (%d fog); fetching forecasts %s - %s [%.0fs]", len(frame), int(frame["__y"].sum()),
             first.date(), end.date(), time.time() - T0)
    hist = bl.fetch_history(station_coords, first.date(), (end - pd.Timedelta(days=1)).date())
    log.info("forecasts for %d stations [%.0fs]", len(hist), time.time() - T0)
    params = dict(num_leaves=31, min_child_samples=60, **fm.LGBM_SAMPLING)

    folds, pooled = [], {}
    for k in range(FOLDS, 0, -1):
        fs, fe = end - pd.Timedelta(days=FOLD_DAYS * k), end - pd.Timedelta(days=FOLD_DAYS * (k - 1))
        vs = fs - pd.Timedelta(days=VAL_DAYS)
        inner = frame[frame["__datetime"] < vs]; val = frame[(frame["__datetime"] >= vs) & (frame["__datetime"] < fs)]
        train = frame[frame["__datetime"] < fs]; test = frame[(frame["__datetime"] >= fs) & (frame["__datetime"] < fe)]
        val_fc = fm._forecast_input_rows(val, hist, station_coords, wf, medians)
        test_fc = fm._forecast_input_rows(test, hist, station_coords, wf, medians)
        if len(val_fc) == 0 or len(test_fc) == 0 or val_fc["__y"].sum() < 10 or test_fc["__y"].sum() == 0 or len(inner) < 2000:
            log.info("fold %s skipped (val %d rows, test %d rows, inner %d)", fs.date(), len(val_fc), len(test_fc), len(inner))
            continue
        yv, yt = val_fc["__y"].to_numpy(), test_fc["__y"].to_numpy()
        fold = {"test": [str(fs.date()), str((fe - pd.Timedelta(days=1)).date())], "n_test": int(len(yt)), "fog_test": int(yt.sum()),
                "n_val": int(len(yv)), "fog_val": int(yv.sum()), "variants": {}}
        clim_v, clim_t = fm._climatology_probs(inner, val_fc), fm._climatology_probs(train, test_fc)
        for name in ("P0", "P1"):
            ir, ig = fm._station_rates(inner)
            if name == "P0":
                Xi = fm._features_with_rate(inner, wf, ir, ig, static=static)
            else:
                Xi = feats_oof(inner, wf, static)
            Xv = fm._features_with_rate(val, wf, ir, ig, static=static)
            m = fm._fit_lgbm(Xi, inner["__y"].to_numpy(), params, fm.MAX_ESTIMATORS, eval_set=(Xv, val["__y"].to_numpy()),
                             sample_weight=fm._recency_weights(inner, vs))
            n_est = max(50, int(round((m.best_iteration_ or fm.MAX_ESTIMATORS) * fm.REFIT_ESTIMATOR_SCALE)))
            pv_raw = m.predict_proba(fm._features_with_rate(val_fc, wf, ir, ig, static=static))[:, 1]
            w = fm._best_climatology_weight(yv, pv_raw, clim_v)
            pv = (1 - w) * pv_raw + w * clim_v
            th = thresholds(yv, pv)
            tr, tg = fm._station_rates(train)
            Xtr = fm._features_with_rate(train, wf, tr, tg, static=static) if name == "P0" else feats_oof(train, wf, static)
            final = fm._fit_lgbm(Xtr, train["__y"].to_numpy(), params, n_est, sample_weight=fm._recency_weights(train, fs))
            pt = (1 - w) * final.predict_proba(fm._features_with_rate(test_fc, wf, tr, tg, static=static))[:, 1] + w * clim_t
            v = {"trees": n_est, "clim_weight": w, "thresholds": th,
                 "ap": round(float(average_precision_score(yt, pt)), 4), "logloss": round(float(log_loss(yt, pt, labels=[0, 1])), 5),
                 "brier": round(float(np.mean((pt - yt) ** 2)), 5), "mean_p": round(float(pt.mean()), 4),
                 "rules": {r: f1_at(yt, pt, th[r]) for r in ("T_cur", "T_half", "T_smooth", "T_rate")},
                 "oracle_best_f1": max(f1_at(yt, pt, t)["f1"] for t in fm.FOG_THRESHOLD_GRID)}
            fold["variants"][name] = v
            pooled.setdefault(name, {"y": [], "p": [], "d": [], "pred": {}})
            pooled[name]["y"].append(yt); pooled[name]["p"].append(pt)
            pooled[name]["d"].append(test_fc["__datetime"].dt.strftime("%Y-%m-%d").to_numpy())
            for r in v["rules"]:
                pooled[name]["pred"].setdefault(r, []).append((pt >= th[r]).astype(int))
            log.info("fold %s %s: AP=%.4f ll=%.5f rules=%s [%.0fs]", fs.date(), name, v["ap"], v["logloss"],
                     {r: (x["t"], x["f1"]) for r, x in v["rules"].items()}, time.time() - T0)
        # 気候値(本番と同じくF1最大のしきい値)
        tc = thresholds(yv, clim_v)
        fold["climatology"] = {"ap": round(float(average_precision_score(yt, clim_t)), 4),
                               "brier": round(float(np.mean((clim_t - yt) ** 2)), 5),
                               "rules": {r: f1_at(yt, clim_t, tc[r]) for r in ("T_cur", "T_half")}}
        pooled.setdefault("CLIM", {"y": [], "p": [], "d": [], "pred": {}})
        pooled["CLIM"]["y"].append(yt); pooled["CLIM"]["p"].append(clim_t)
        pooled["CLIM"]["d"].append(test_fc["__datetime"].dt.strftime("%Y-%m-%d").to_numpy())
        for r in ("T_cur", "T_half"):
            pooled["CLIM"]["pred"].setdefault(r, []).append((clim_t >= tc[r]).astype(int))
        # 時刻別(P0)
        hrs = test_fc["__datetime"].dt.hour.to_numpy()
        fold["by_hour_P0"] = {int(h): {"n": int((hrs == h).sum()), "fog": int(yt[hrs == h].sum()),
                                       "mean_p": round(float(pt[hrs == h].mean()), 4) if (hrs == h).any() else None}
                              for h in sorted(set(hrs))}
        folds.append(fold)

    summary = {}
    for name, d in pooled.items():
        y, p, dd = np.concatenate(d["y"]), np.concatenate(d["p"]), np.concatenate(d["d"])
        s = {"n": int(len(y)), "fog": int(y.sum()), "ap": round(float(average_precision_score(y, p)), 4),
             "brier": round(float(np.mean((p - y) ** 2)), 5), "logloss": round(float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6), labels=[0, 1])), 5)}
        for r, parts in d["pred"].items():
            pr = np.concatenate(parts)
            tp = int(((pr == 1) & (y == 1)).sum()); fp = int(((pr == 1) & (y == 0)).sum()); fn = int(((pr == 0) & (y == 1)).sum())
            s[r] = {"f1": round(2 * tp / (2 * tp + fp + fn), 4), "precision": round(tp / (tp + fp), 4) if tp + fp else None,
                    "recall": round(tp / (tp + fn), 4), "tp": tp, "fp": fp, "fn": fn}
        summary[name] = s
    # 差の日単位ブートストラップ
    rng = np.random.default_rng(0)
    y = np.concatenate(pooled["P0"]["y"]); dd = np.concatenate(pooled["P0"]["d"])
    uniq = np.unique(dd); idx = [np.where(dd == u)[0] for u in uniq]
    def f1v(yy, pr):
        tp = ((pr == 1) & (yy == 1)).sum(); fp = ((pr == 1) & (yy == 0)).sum(); fn = ((pr == 0) & (yy == 1)).sum()
        return 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 0.0
    preds = {f"{n}/{r}": np.concatenate(pooled[n]["pred"][r]) for n in ("P0", "P1") for r in pooled[n]["pred"]}
    probs = {n: np.concatenate(pooled[n]["p"]) for n in ("P0", "P1", "CLIM")}
    comps = {"F1 P0/T_half - P0/T_cur": ("f1", "P0/T_half", "P0/T_cur"), "F1 P0/T_smooth - P0/T_cur": ("f1", "P0/T_smooth", "P0/T_cur"),
             "F1 P0/T_rate - P0/T_cur": ("f1", "P0/T_rate", "P0/T_cur"), "F1 P1/T_half - P0/T_half": ("f1", "P1/T_half", "P0/T_half"),
             "AP P1 - P0": ("ap", "P1", "P0"), "logloss P1 - P0": ("ll", "P1", "P0"), "AP P0 - CLIM": ("ap", "P0", "CLIM")}
    boots = {k: [] for k in comps}
    for _ in range(1000):
        pick = np.concatenate([idx[i] for i in rng.integers(0, len(uniq), len(uniq))])
        yy = y[pick]
        if yy.sum() == 0:
            continue
        for k, (kind, a, b) in comps.items():
            if kind == "f1":
                boots[k].append(f1v(yy, preds[a][pick]) - f1v(yy, preds[b][pick]))
            elif kind == "ap":
                boots[k].append(average_precision_score(yy, probs[a][pick]) - average_precision_score(yy, probs[b][pick]))
            else:
                boots[k].append(log_loss(yy, probs[a][pick], labels=[0, 1]) - log_loss(yy, probs[b][pick], labels=[0, 1]))
    ci = {k: [round(float(np.percentile(v, 2.5)), 4), round(float(np.percentile(v, 97.5)), 4)] for k, v in boots.items()}
    out = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "folds": folds, "summary": summary, "ci95": ci,
           "elapsed_sec": round(time.time() - T0)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("summary %s", json.dumps(summary, ensure_ascii=False))
    log.info("ci %s", ci)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    logging.getLogger("fog_model").setLevel(logging.WARNING)
    run()
