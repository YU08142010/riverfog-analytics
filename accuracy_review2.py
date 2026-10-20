"""精度の改善候補の比較 その2(2026-10-20、本番は変えない)。

その1(accuracy_review_20261020.json)で、本番の作り方では予報を入力にすると確率が実際より低く出る(10月の評価期間で
平均1.0%に対し実際1.9%)ことが分かった。学習は実測、本番の入力は予報、という食い違いが原因と考えられる。
その1と同じ評価(直近6週を2週×3、検証・評価は過去の予報が入力)で、次を比べる。どれも地点別発生率は自分の週を除いて計算(P1)。
  Q0: P1(気候値と混ぜて補正。本番と同じ)
  Q1: Q0 + 学習データに「同じ時刻・地点の過去の予報を入力にした行」を加える(実測の行はそのまま)
  Q2: Q0の確率を、検証期間の予報入力でロジスティック補正(Platt)する(気候値との混合の代わり)
しきい値は T_cur(F1最大)と T_half(F1最大値の半分)。結果: data/experiments/accuracy_review2_20261020.json(どれも採らなかった)
実行: .venv/bin/python accuracy_review2.py
"""
import csv
import json
import logging
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, f1_score, log_loss

import bl_recheck as bl
import fog_data
import fog_model as fm
import station_weather
from common import BASE_DIR, CAMERAS_CSV, JST, STATION_WEATHER_DB_PATH, representative_coords

OUT = BASE_DIR / "data" / "experiments" / "accuracy_review2_20261020.json"
FOLDS, FOLD_DAYS, VAL_DAYS, HIST_DAYS = 3, 14, 14, 130
log = logging.getLogger("accuracy_review2")
T0 = time.time()


def f1_at(y, p, t):
    pred = (p >= t).astype(int)
    tp = int(((pred == 1) & (y == 1)).sum()); fp = int(((pred == 1) & (y == 0)).sum()); fn = int(((pred == 0) & (y == 1)).sum())
    return {"t": round(float(t), 3), "f1": round(2 * tp / (2 * tp + fp + fn), 4) if tp + fp + fn else 0.0,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None, "recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "tp": tp, "fp": fp, "fn": fn}


def thresholds(y, p):
    grid = np.array(fm.FOG_THRESHOLD_GRID)
    f1s = np.array([f1_score(y, (p >= t).astype(int), zero_division=0) for t in grid])
    i = int(np.argmax(f1s))
    return {"T_cur": float(grid[i]), "T_half": max(0.02, round(float(f1s[i]) / 2, 2)), "val_f1_max": round(float(f1s[i]), 4)}


def oof_station_rates(rows, block_days=7):
    blk = ((rows["__datetime"] - rows["__datetime"].min()).dt.days // block_days).to_numpy()
    st = rows["__station"].to_numpy(); y = rows["__y"].to_numpy()
    df = pd.DataFrame({"b": blk, "s": st, "y": y})
    tot = df.groupby("s")["y"].agg(["sum", "count"]); by = df.groupby(["s", "b"])["y"].agg(["sum", "count"])
    gb = df.groupby("b")["y"].agg(["sum", "count"])
    keys = pd.MultiIndex.from_arrays([st, blk])
    s_sum, s_cnt = tot["sum"].reindex(st).to_numpy(), tot["count"].reindex(st).to_numpy()
    b_sum, b_cnt = by["sum"].reindex(keys).to_numpy(), by["count"].reindex(keys).to_numpy()
    gb_sum, gb_cnt = gb["sum"].reindex(blk).to_numpy(), gb["count"].reindex(blk).to_numpy()
    glob = (y.sum() - gb_sum) / np.maximum(len(y) - gb_cnt, 1)
    m = fm.STATION_RATE_SMOOTHING
    return ((s_sum - b_sum) + m * glob) / ((s_cnt - b_cnt) + m)


def X_oof(rows, wf, static):
    X = fm._features_with_rate(rows, wf, {}, 0.0, static=static)
    X[fm.STATION_FOG_RATE_FEATURE] = oof_station_rates(rows)
    return X


def platt(p_val, y_val):
    lr = LogisticRegression(C=100.0)
    z = np.log(np.clip(p_val, 1e-6, 1 - 1e-6) / (1 - np.clip(p_val, 1e-6, 1 - 1e-6)))
    lr.fit(z.reshape(-1, 1), y_val)
    return lambda p: lr.predict_proba(np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6))).reshape(-1, 1))[:, 1]


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
    first = end - pd.Timedelta(days=HIST_DAYS)
    hist = bl.fetch_history(station_coords, first.date(), (end - pd.Timedelta(days=1)).date())
    fc_all = fm._forecast_input_rows(frame[frame["__datetime"] >= first], hist, station_coords, wf, medians)
    log.info("frame %d rows; forecast rows %d (%d fog) since %s [%.0fs]", len(frame), len(fc_all), int(fc_all["__y"].sum()),
             first.date(), time.time() - T0)
    params = dict(num_leaves=31, min_child_samples=60, **fm.LGBM_SAMPLING)
    folds, pooled = [], {}
    for k in range(FOLDS, 0, -1):
        fs, fe = end - pd.Timedelta(days=FOLD_DAYS * k), end - pd.Timedelta(days=FOLD_DAYS * (k - 1))
        vs = fs - pd.Timedelta(days=VAL_DAYS)
        inner = frame[frame["__datetime"] < vs]
        train = frame[frame["__datetime"] < fs]
        val_fc = fc_all[(fc_all["__datetime"] >= vs) & (fc_all["__datetime"] < fs)].reset_index(drop=True)
        test_fc = fc_all[(fc_all["__datetime"] >= fs) & (fc_all["__datetime"] < fe)].reset_index(drop=True)
        yv, yt = val_fc["__y"].to_numpy(), test_fc["__y"].to_numpy()
        fold = {"test": [str(fs.date()), str((fe - pd.Timedelta(days=1)).date())], "n_test": int(len(yt)), "fog_test": int(yt.sum()),
                "rate_test": round(float(yt.mean()), 4), "variants": {}}
        clim_v, clim_t = fm._climatology_probs(inner, val_fc), fm._climatology_probs(train, test_fc)
        ir, ig = fm._station_rates(inner); tr, tg = fm._station_rates(train)
        for name in ("Q0", "Q1"):
            if name == "Q1":
                inner_fit = pd.concat([inner, fc_all[fc_all["__datetime"] < vs]], ignore_index=True)
                train_fit = pd.concat([train, fc_all[fc_all["__datetime"] < fs]], ignore_index=True)
            else:
                inner_fit, train_fit = inner, train
            m = fm._fit_lgbm(X_oof(inner_fit, wf, static), inner_fit["__y"].to_numpy(), params, fm.MAX_ESTIMATORS,
                             eval_set=(fm._features_with_rate(val_fc, wf, ir, ig, static=static), yv),
                             sample_weight=fm._recency_weights(inner_fit, vs))
            n_est = max(50, int(round((m.best_iteration_ or fm.MAX_ESTIMATORS) * fm.REFIT_ESTIMATOR_SCALE)))
            pv_raw = m.predict_proba(fm._features_with_rate(val_fc, wf, ir, ig, static=static))[:, 1]
            final = fm._fit_lgbm(X_oof(train_fit, wf, static), train_fit["__y"].to_numpy(), params, n_est,
                                 sample_weight=fm._recency_weights(train_fit, fs))
            pt_raw = final.predict_proba(fm._features_with_rate(test_fc, wf, tr, tg, static=static))[:, 1]
            w = fm._best_climatology_weight(yv, pv_raw, clim_v)
            outs = {name: ((1 - w) * pv_raw + w * clim_v, (1 - w) * pt_raw + w * clim_t)}
            if name == "Q0":
                cal = platt(pv_raw, yv)
                outs["Q2"] = (cal(pv_raw), cal(pt_raw))
            for vname, (pv, pt) in outs.items():
                th = thresholds(yv, pv)
                v = {"trees": n_est, "clim_weight": w, "thresholds": th,
                     "ap": round(float(average_precision_score(yt, pt)), 4),
                     "logloss": round(float(log_loss(yt, np.clip(pt, 1e-6, 1 - 1e-6), labels=[0, 1])), 5),
                     "brier": round(float(np.mean((pt - yt) ** 2)), 5), "mean_p": round(float(pt.mean()), 4),
                     "val_mean_p": round(float(pv.mean()), 4), "val_rate": round(float(yv.mean()), 4),
                     "rules": {r: f1_at(yt, pt, th[r]) for r in ("T_cur", "T_half")}}
                fold["variants"][vname] = v
                d = pooled.setdefault(vname, {"y": [], "p": [], "d": [], "pred": {}})
                d["y"].append(yt); d["p"].append(pt); d["d"].append(test_fc["__datetime"].dt.strftime("%Y-%m-%d").to_numpy())
                for r in v["rules"]:
                    d["pred"].setdefault(r, []).append((pt >= th[r]).astype(int))
                log.info("fold %s %s: AP=%.4f ll=%.5f mean_p=%.4f rate=%.4f rules=%s [%.0fs]", fs.date(), vname, v["ap"], v["logloss"],
                         v["mean_p"], fold["rate_test"], {r: (x["t"], x["f1"]) for r, x in v["rules"].items()}, time.time() - T0)
        folds.append(fold)

    summary = {}
    for name, d in pooled.items():
        y, p = np.concatenate(d["y"]), np.concatenate(d["p"])
        s = {"n": int(len(y)), "fog": int(y.sum()), "ap": round(float(average_precision_score(y, p)), 4),
             "brier": round(float(np.mean((p - y) ** 2)), 5), "logloss": round(float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6), labels=[0, 1])), 5),
             "mean_p": round(float(p.mean()), 4), "rate": round(float(y.mean()), 4)}
        for r, parts in d["pred"].items():
            pr = np.concatenate(parts)
            s[r] = f1_at(y, pr.astype(float), 0.5)
        summary[name] = s
    rng = np.random.default_rng(0)
    y = np.concatenate(pooled["Q0"]["y"]); dd = np.concatenate(pooled["Q0"]["d"])
    uniq = np.unique(dd); idx = [np.where(dd == u)[0] for u in uniq]
    probs = {n: np.concatenate(pooled[n]["p"]) for n in pooled}
    preds = {f"{n}/{r}": np.concatenate(pooled[n]["pred"][r]) for n in pooled for r in pooled[n]["pred"]}

    def f1v(yy, pr):
        tp = ((pr == 1) & (yy == 1)).sum(); fp = ((pr == 1) & (yy == 0)).sum(); fn = ((pr == 0) & (yy == 1)).sum()
        return 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 0.0
    comps = {"AP Q1-Q0": ("ap", "Q1", "Q0"), "logloss Q1-Q0": ("ll", "Q1", "Q0"), "brier Q1-Q0": ("br", "Q1", "Q0"),
             "AP Q2-Q0": ("ap", "Q2", "Q0"), "logloss Q2-Q0": ("ll", "Q2", "Q0"), "brier Q2-Q0": ("br", "Q2", "Q0"),
             "F1 Q1/T_half-Q0/T_half": ("f1", "Q1/T_half", "Q0/T_half"), "F1 Q2/T_half-Q0/T_half": ("f1", "Q2/T_half", "Q0/T_half"),
             "F1 Q0/T_half-Q0/T_cur": ("f1", "Q0/T_half", "Q0/T_cur")}
    boots = {k: [] for k in comps}
    for _ in range(400):
        pick = np.concatenate([idx[i] for i in rng.integers(0, len(uniq), len(uniq))])
        yy = y[pick]
        if yy.sum() == 0:
            continue
        for k, (kind, a, b) in comps.items():
            if kind == "f1":
                boots[k].append(f1v(yy, preds[a][pick]) - f1v(yy, preds[b][pick]))
            elif kind == "ap":
                boots[k].append(average_precision_score(yy, probs[a][pick]) - average_precision_score(yy, probs[b][pick]))
            elif kind == "br":
                boots[k].append(np.mean((probs[a][pick] - yy) ** 2) - np.mean((probs[b][pick] - yy) ** 2))
            else:
                boots[k].append(log_loss(yy, np.clip(probs[a][pick], 1e-6, 1 - 1e-6), labels=[0, 1])
                                - log_loss(yy, np.clip(probs[b][pick], 1e-6, 1 - 1e-6), labels=[0, 1]))
    ci = {k: [round(float(np.percentile(v, 2.5)), 5), round(float(np.percentile(v, 97.5)), 5)] for k, v in boots.items()}
    out = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "folds": folds, "summary": summary, "ci95": ci,
           "elapsed_sec": round(time.time() - T0)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("summary %s", json.dumps(summary, ensure_ascii=False))
    log.info("ci %s", ci)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    logging.getLogger("fog_model").setLevel(logging.WARNING)
    run()
