"""仮検証: 二値・全地点共通LightGBMモデル + Open-Meteo過去予報データ(管理者タブ「仮検証」用)。

本番の予測モデル(fog_model.py / predict.py)には一切手を加えず、別系統で次の2点を検証する。
  1. 目的変数を「川霧の有無(コード1〜3)」の二値にし、全地点で1つのモデルを共有する
  2. 学習・評価に、再解析値(weather.db、実況)ではなく、Open-Meteoの過去の予報値
     (historical-forecast-api)を使う(本番は実況で学習し、予報値で予測しているため)

結果はdata/experiments/binary_forecast_v1.jsonに書き出し、experiment_view.pyが管理者タブで
グラフ・表にする。本番のDB・モデル・設定には書き込まない(読み取り専用)。

実行: .venv/bin/python experiment_model.py  (管理者タブの「今すぐ実行」からも起動できる)
"""
import json
import logging
import logging.handlers
import os
import sqlite3
import sys
import time
import traceback
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from common import BASE_DIR, JST, load_camera_regions, representative_coords, request_with_dns_fallback  # noqa: E402

EXP_DIR = BASE_DIR / "data" / "experiments"
RESULT_PATH = EXP_DIR / "binary_forecast_v1.json"
STATUS_PATH = EXP_DIR / "binary_forecast_v1.status.json"
HIST_FC_CACHE = EXP_DIR / "historical_forecast_rep.csv"
PREDICTIONS_DB = BASE_DIR / "data" / "predictions.db"
LOG_FILE = BASE_DIR / "logs" / "experiment.log"

HIST_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
HIST_FORECAST_CACHE_HOURS = 20
HIST_FORECAST_CHUNK_DAYS = 120

log = logging.getLogger("experiment_model")

MIN_FOG_HOURS_FOR_TEST_MONTH = 150   # 評価対象の月(その月に川霧が何時間あるか)の下限
MIN_FOG_HOURS_FOR_INNER_MONTH = 30   # しきい値決定の内側検証に使う月の下限
CHRONO_SPLIT = pd.Timestamp("2026-09-01")  # 時系列ホールドアウトの境目(これ以降が評価期間)
STATION_RATE_SMOOTHING = 20
THRESHOLD_GRID = np.round(np.arange(0.02, 0.9, 0.01), 2)

LGBM_PARAMS = dict(
    n_estimators=250, learning_rate=0.04, num_leaves=15, min_child_samples=60,
    subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=5.0,
    n_jobs=2, verbosity=-1, random_state=0,
)

# --- 川霧の出やすさの条件指標 ------------------------------------------------------------
# 先行研究で示された条件(気温20〜25℃、気温露点差0〜1℃、風速0.5〜1m/s)に、時間帯(朝4〜8時・夕16〜19時)を
# 加えた4条件のうち、いくつ満たしているか(0〜4)。数値の範囲は研究の値をそのまま使い、このデータで
# 調整していない。時間帯の枠だけは、データを見て決めたもの(楽観的になりうる)。
COND_TEMP = (20.0, 25.0)
COND_SPREAD = (0.0, 1.0)
COND_WIND = (0.5, 1.0)
COND_HOURS = ((4, 8), (16, 19))
COND_FEATURES = ["条件_気温", "条件_露点差", "条件_風速", "条件_時間帯", "条件の数"]


def add_condition_features(d):
    """気象特徴量とhour列を持つDataFrameに、条件指標(各条件の成否と満たした数)を追加して返す。"""
    d = d.copy()
    t, sp, ws, hr = d["気温(℃)"], d["気温露点差"], d["風速(m/s)"], d["hour"]
    d["条件_気温"] = ((t >= COND_TEMP[0]) & (t <= COND_TEMP[1])).astype(int)
    d["条件_露点差"] = ((sp >= COND_SPREAD[0]) & (sp <= COND_SPREAD[1])).astype(int)
    d["条件_風速"] = ((ws >= COND_WIND[0]) & (ws <= COND_WIND[1])).astype(int)
    d["条件_時間帯"] = sum(((hr >= a) & (hr <= b)).astype(int) for a, b in COND_HOURS).clip(upper=1)
    d["条件の数"] = d[["条件_気温", "条件_露点差", "条件_風速", "条件_時間帯"]].sum(axis=1)
    return d


def condition_table(d, y_col="y"):
    """満たした条件の数(0〜4)ごとの件数・川霧の件数・発生率。"""
    base = float(d[y_col].mean())
    rows = []
    for k in range(5):
        g = d[d["条件の数"] == k]
        rows.append({"count": k, "n": int(len(g)), "fog_n": int(g[y_col].sum()),
                     "rate": float(g[y_col].mean()) if len(g) else None,
                     "ratio": (float(g[y_col].mean()) / base) if len(g) and base > 0 else None})
    by_month = []
    for m, g in d.groupby("month"):
        hi, lo = g[g["条件の数"] >= 3], g[g["条件の数"] <= 1]
        if g[y_col].sum() >= 20 and len(hi) >= 20 and len(lo) >= 20:
            by_month.append({"month": m, "rate_3plus": float(hi[y_col].mean()), "rate_1minus": float(lo[y_col].mean()),
                             "n_3plus": int(len(hi)), "n_1minus": int(len(lo))})
    return {"base_rate": base, "by_count": rows, "by_month": by_month}


def rule_proba(train, test):
    """条件の数ごとの、学習データでの川霧発生率を、そのまま確率とする(指標単独のルール)。"""
    rates = train.groupby("条件の数")["y"].mean()
    fallback = float(train["y"].mean())
    return test["条件の数"].map(rates).fillna(fallback).to_numpy()


VARIANTS = {
    "CLIM": {"label": "気候値(地点×時刻)", "desc": "学習期間の地点×時刻ごとの川霧発生率をそのまま確率とみなす最も単純な基準。"},
    "RULE": {"label": "条件指標のみ(ルール)", "desc": "気温・露点差・風速・時間帯の4条件のうち満たした数だけから、学習期間の発生率を確率とする単純なルール。機械学習は使わない。"},
    "V2": {"label": "二値 / 実況→実況", "desc": "二値・全地点共通モデルを実況(再解析値)で学習し、実況で評価する。気象が完全に当たった場合の上限に近い。"},
    "V3": {"label": "二値 / 実況→予報値", "desc": "実況で学習し、過去の予報値で評価する。現行の本番と同じ構図(学習と予測で入力の性質が違う)。"},
    "V4": {"label": "二値 / 予報値→予報値(提案2)", "desc": "過去の予報値で学習し、過去の予報値で評価する。今回の提案(1+2)。"},
    "V5": {"label": "V4 + 雲量・気圧など", "desc": "V4に雲量、気圧、露点差の変化、前12時間の最低気温などを足した補足検証。"},
    "V6": {"label": "V4 + 条件指標", "desc": "V4に、4条件の成否と満たした数(条件指標)を特徴量として足したモデル。"},
}


# ---------------------------------------------------------------------------
# 進捗・ログ
# ---------------------------------------------------------------------------
def _setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


_STARTED_AT = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def _write_status(state, step="", **extra):
    EXP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"state": state, "step": step, "started_at": _STARTED_AT,
               "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), **extra}
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATUS_PATH)


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if (o != o or o in (float("inf"), float("-inf"))) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    return o


# ---------------------------------------------------------------------------
# データ読み込み
# ---------------------------------------------------------------------------
def load_label_frame(stations, phenom_df):
    """(datetime, station) ごとの川霧有無(1.0/0.0)の縦持ちDataFrameを返す。本番の学習と同様に、
    Geminiの確信度がlowのラベルとbackfill推定の日時は除外する(fog_modelの読み込み関数を再利用)。"""
    import fog_features as ff
    import fog_model as fm

    targets = ff.build_location_class_targets(phenom_df, stations)
    low = fm.load_low_confidence_set()
    backfilled = fm.load_backfilled_datetime_set()
    dts = targets["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    for s in stations:
        mask = dts.apply(lambda x, s=s: (x, s) in low)
        targets.loc[mask, s] = np.nan
    targets.loc[dts.isin(backfilled), stations] = np.nan
    targets = targets.drop_duplicates("datetime").set_index("datetime")
    fog = targets[stations].isin([1, 2, 3]).astype(float).where(targets[stations].notna())
    long = fog.stack().dropna().rename("y").reset_index()  # pandas 3ではstack()が欠測を落とさない
    long.columns = ["datetime", "station", "y"]
    long["y"] = long["y"].astype(int)
    long["hour"] = long["datetime"].dt.hour
    long["month"] = long["datetime"].dt.strftime("%Y-%m")
    return long


def fetch_historical_forecast(lat, lon, start_date, end_date):
    """Open-Meteoの過去予報(historical-forecast-api)を取得して、本番の学習データと同じ列名・単位の
    DataFrameで返す。同じモデルの直近の予報値(リードタイム約0〜1日)をつなげたもので、
    再解析・実況値ではない。取得結果はdata/experiments/にキャッシュする。"""
    import fog_model as fm

    if HIST_FC_CACHE.exists():
        age_h = (time.time() - HIST_FC_CACHE.stat().st_mtime) / 3600.0
        if age_h < HIST_FORECAST_CACHE_HOURS:
            cached = pd.read_csv(HIST_FC_CACHE, parse_dates=["datetime"])
            if cached["datetime"].min().date() <= start_date and cached["datetime"].max().date() >= end_date - timedelta(days=3):
                log.info("Using cached historical forecast (%d rows)", len(cached))
                return cached

    extra_vars = ["cloud_cover", "cloud_cover_low", "surface_pressure"]
    hourly_vars = list(fm._FORECAST_VARS.keys()) + extra_vars
    frames = []
    cur = start_date
    while cur <= end_date:
        chunk_end = min(cur + timedelta(days=HIST_FORECAST_CHUNK_DAYS - 1), end_date)
        params = {
            "latitude": lat, "longitude": lon, "hourly": ",".join(hourly_vars),
            "start_date": cur.isoformat(), "end_date": chunk_end.isoformat(),
            "timezone": "Asia/Tokyo", "wind_speed_unit": "ms", "precipitation_unit": "mm",
            "temperature_unit": "celsius",
        }
        resp = None
        for attempt in range(1, 4):
            try:
                resp = request_with_dns_fallback("GET", HIST_FORECAST_URL, params=params, timeout=90, log=log)
                resp.raise_for_status()
                break
            except Exception as e:  # noqa: BLE001
                log.warning("historical forecast fetch failed (%s..%s, attempt %d/3): %s", cur, chunk_end, attempt, e)
                resp = None
                time.sleep(5 * attempt)
        if resp is None:
            raise RuntimeError(f"Open-Meteo過去予報を取得できませんでした({cur}〜{chunk_end})")
        hourly = resp.json()["hourly"]
        # fog_model側の変換(列名・単位・短い欠測の補間)を再利用し、追加変数は別に取り込む。
        df = fm._forecast_payload_to_df({k: v for k, v in hourly.items() if k != "cloud_cover"
                                         and k not in ("cloud_cover_low", "surface_pressure")} | {"time": hourly["time"]})
        extra = pd.DataFrame({"datetime": pd.to_datetime(hourly["time"])})
        for k in extra_vars:
            extra[k] = pd.to_numeric(pd.Series(hourly[k]), errors="coerce")
        frames.append(df.merge(extra, on="datetime", how="left"))
        cur = chunk_end + timedelta(days=1)
        time.sleep(1.0)
    out = pd.concat(frames, ignore_index=True).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    EXP_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(HIST_FC_CACHE, index=False)
    log.info("Fetched historical forecast: %d rows, %s - %s", len(out), out["datetime"].min(), out["datetime"].max())
    return out


# ---------------------------------------------------------------------------
# 特徴量
# ---------------------------------------------------------------------------
def _feature_names():
    import fog_features as ff
    return [
        ff.COL_TEMP, ff.COL_PRECIP, ff.COL_WIND_SPEED, ff.COL_HUMIDITY, ff.COL_DEWPOINT, "気温露点差",
        "時_sin", "時_cos", "風向_sin", "風向_cos", ff.COL_SUNSHINE, ff.COL_SOLAR,
        *ff.LAG_FEATURE_NAMES, ff.SUN_FEATURE_NAME,
    ]


EXTRA_FEATURES = [
    "雲量", "下層雲量", "雲量平均_6h", "気圧", "気圧変化_3h", "露点差変化_3h", "露点差最小_6h",
    "前12h最低気温", "気温-前12h最低", "風速最大_6h", "降水_6h",
]
KEY_COLS = ["気温(℃)", "露点温度(℃)", "相対湿度(％)", "風速(m/s)"]


def build_weather_features(wdf, lat, lon, with_extra=False):
    """本番(fog_features.prepare_features)と同じ処理で気象特徴量を作る。
    月(季節)の特徴量は、学習期間に無い月へ外挿してしまうため、この検証では使わない。
    戻り値: datetimeをindexにしたDataFrame(特徴量列のみ)。"""
    import fog_features as ff

    w = wdf.drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    out, _names, _ = ff.prepare_features(w, lat, lon)
    out = out.set_index("datetime")
    feats = _feature_names()
    if with_extra:
        idx = out.index
        t = out[ff.COL_TEMP]
        spread = out["気温露点差"]
        out["雲量"] = w.set_index("datetime")["cloud_cover"].reindex(idx)
        out["下層雲量"] = w.set_index("datetime")["cloud_cover_low"].reindex(idx)
        out["雲量平均_6h"] = out["雲量"].rolling(6, min_periods=2).mean()
        out["気圧"] = w.set_index("datetime")["surface_pressure"].reindex(idx)
        out["気圧変化_3h"] = out["気圧"] - out["気圧"].shift(3)
        out["露点差変化_3h"] = spread - spread.shift(3)
        out["露点差最小_6h"] = spread.rolling(6, min_periods=2).min()
        out["前12h最低気温"] = t.rolling(12, min_periods=4).min()
        out["気温-前12h最低"] = t - out["前12h最低気温"]
        out["風速最大_6h"] = out[ff.COL_WIND_SPEED].rolling(6, min_periods=2).max()
        out["降水_6h"] = out[ff.COL_PRECIP].rolling(6, min_periods=2).sum()
        feats = feats + EXTRA_FEATURES
    missing = [c for c in feats if c not in out.columns]
    if missing:
        raise RuntimeError(f"特徴量を作れませんでした: {missing}")
    return out[feats]


# ---------------------------------------------------------------------------
# モデル・評価
# ---------------------------------------------------------------------------
def _station_rate(train):
    g = train.groupby("station")["y"].agg(["sum", "count"])
    gr = train["y"].mean()
    return (g["sum"] + STATION_RATE_SMOOTHING * gr) / (g["count"] + STATION_RATE_SMOOTHING)


def fit_predict(train, test, feats, return_model=False):
    """train/testはstation, y, hourと特徴量列を持つDataFrame(別々の気象ソースでもよい)。
    地点別川霧発生率(地点識別の代わり)はtrainだけから計算する(リーク防止)。"""
    import lightgbm as lgb

    rate = _station_rate(train)
    tr, te = train.copy(), test.copy()
    tr["地点別川霧発生率"] = tr["station"].map(rate)
    te["地点別川霧発生率"] = te["station"].map(rate).fillna(train["y"].mean())
    cols = list(feats) + ["地点別川霧発生率"]
    model = lgb.LGBMClassifier(**LGBM_PARAMS).fit(tr[cols], tr["y"])
    p = model.predict_proba(te[cols])[:, 1]
    if return_model:
        return p, model, cols, rate
    return p


def climatology_proba(train, test):
    cl = train.groupby(["station", "hour"])["y"].mean()
    fallback = train["y"].mean()
    keys = list(zip(test["station"], test["hour"]))
    return np.array([cl.get(k, fallback) for k in keys])


def _f1_at(y, p, thr):
    pred = p >= thr
    tp = float((pred & (y == 1)).sum()); fp = float((pred & (y == 0)).sum()); fn = float(((~pred) & (y == 1)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return f1, prec, rec


def pick_threshold(y, p):
    y = np.asarray(y); p = np.asarray(p)
    if y.sum() == 0:
        return 0.15
    best_t, best_f = 0.15, -1.0
    for t in THRESHOLD_GRID:
        f1, _, _ = _f1_at(y, p, t)
        if f1 > best_f:
            best_t, best_f = float(t), f1
    return best_t


def inner_threshold(train, feats, climatology=False):
    """しきい値は評価データを一切見ずに、学習データの内側で月ごとに分けた検証(leave-one-month-out)の
    予測から決める(F1が最大になる値)。"""
    if climatology:
        return pick_threshold(train["y"].to_numpy(), climatology_proba(train, train))
    months = sorted(train["month"].unique())
    ys, ps = [], []
    for m in months:
        te = train[train["month"] == m]
        if te["y"].sum() < MIN_FOG_HOURS_FOR_INNER_MONTH:
            continue
        tr = train[train["month"] != m]
        if tr["y"].sum() == 0:
            continue
        ps.append(fit_predict(tr, te, feats)); ys.append(te["y"].to_numpy())
    if not ps:
        return 0.15
    return pick_threshold(np.concatenate(ys), np.concatenate(ps))


def _metrics(y, p, thr, clim_p=None):
    from sklearn.metrics import average_precision_score, brier_score_loss, precision_recall_curve, roc_auc_score
    y = np.asarray(y); p = np.asarray(p)
    out = {"n": int(len(y)), "fog_n": int(y.sum()), "rate": float(y.mean()) if len(y) else None}
    if len(y) == 0 or y.sum() == 0 or y.sum() == len(y):
        out.update({"ap": None, "auc": None, "brier": None, "bss": None, "f1": None, "precision": None,
                    "recall": None, "best_f1": None, "thr": thr})
        return out
    f1, prec, rec = _f1_at(y, p, thr)
    pr, rc, _ = precision_recall_curve(y, p)
    best_f1 = float((2 * pr * rc / np.maximum(pr + rc, 1e-9)).max())
    brier = float(brier_score_loss(y, np.clip(p, 0, 1)))
    ref = clim_p if clim_p is not None else np.full(len(y), y.mean())
    ref_brier = float(brier_score_loss(y, np.clip(ref, 0, 1)))
    out.update({
        "ap": float(average_precision_score(y, p)), "auc": float(roc_auc_score(y, p)),
        "brier": brier, "bss": (1 - brier / ref_brier) if ref_brier > 0 else None,
        "f1": f1, "precision": prec, "recall": rec, "best_f1": best_f1, "thr": float(thr),
    })
    return out


def _pr_points(y, p, max_points=70):
    from sklearn.metrics import precision_recall_curve
    y = np.asarray(y)
    if y.sum() == 0:
        return {"precision": [], "recall": []}
    pr, rc, _ = precision_recall_curve(y, p)
    if len(pr) > max_points:
        sel = np.unique(np.linspace(0, len(pr) - 1, max_points).astype(int))
        pr, rc = pr[sel], rc[sel]
    return {"precision": [float(v) for v in pr], "recall": [float(v) for v in rc]}


def _reliability(y, p, n_bins=10):
    y = np.asarray(y); p = np.asarray(p)
    edges = np.unique(np.quantile(p, np.linspace(0, 1, n_bins + 1)))
    if len(edges) < 3:
        return []
    ids = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, len(edges) - 2)
    rows = []
    for b in range(len(edges) - 1):
        m = ids == b
        if m.sum() == 0:
            continue
        rows.append({"n": int(m.sum()), "pred": float(p[m].mean()), "obs": float(y[m].mean())})
    return rows


def evaluate_scheme(datasets, scheme_folds, variants_spec, label):
    """scheme_folds: [(train_mask, test_mask, name)]。各バリアントについて全foldの予測を集めて返す。
    戻り値: {variant: {"y","p","thr_by_row","keys"(DataFrame:datetime,station,hour,month)}}"""
    collected = {v: {"y": [], "p": [], "thr": [], "keys": []} for v in variants_spec}
    for fold_i, (tr_mask, te_mask, fold_name) in enumerate(scheme_folds, 1):
        log.info("[%s] fold %d/%d (%s): train=%d test=%d", label, fold_i, len(scheme_folds), fold_name,
                 int(tr_mask.sum()), int(te_mask.sum()))
        for vid, spec in variants_spec.items():
            tr_df = datasets[spec["train"]][tr_mask]
            te_df = datasets[spec["test"]][te_mask]
            if vid == "CLIM":
                thr = inner_threshold(tr_df, None, climatology=True)
                p = climatology_proba(tr_df, te_df)
            elif spec.get("rule"):
                thr = pick_threshold(tr_df["y"].to_numpy(), rule_proba(tr_df, tr_df))
                p = rule_proba(tr_df, te_df)
            else:
                thr = inner_threshold(tr_df, spec["feats"])
                p = fit_predict(tr_df, te_df, spec["feats"])
            c = collected[vid]
            c["y"].append(te_df["y"].to_numpy()); c["p"].append(p); c["thr"].append(np.full(len(te_df), thr))
            c["keys"].append(te_df[["datetime", "station", "hour", "month"]])
    for vid, c in collected.items():
        c["y"] = np.concatenate(c["y"]); c["p"] = np.concatenate(c["p"]); c["thr"] = np.concatenate(c["thr"])
        c["keys"] = pd.concat(c["keys"], ignore_index=True)
    return collected


def summarize_scheme(collected, extras=True):
    """collectedから、バリアントごとの指標・PR曲線・較正・月別・時刻別・地点別をまとめる。"""
    out = {"metrics": {}, "pr": {}, "reliability": {}, "by_month": {}, "by_hour": {}, "by_station": []}
    clim = collected.get("CLIM")
    for vid, c in collected.items():
        thr_mean = float(np.mean(c["thr"]))
        clim_p = None
        if clim is not None and len(clim["p"]) == len(c["p"]):
            clim_p = clim["p"]
        # しきい値は fold ごとに違うため、予測クラスはfoldごとのしきい値で判定してから指標を出す
        y, p, thr_row = c["y"], c["p"], c["thr"]
        m = _metrics(y, p, thr_mean, clim_p)
        if m["ap"] is not None:
            pred = p >= thr_row
            tp = float((pred & (y == 1)).sum()); fp = float((pred & (y == 0)).sum()); fn = float(((~pred) & (y == 1)).sum())
            prec = tp / (tp + fp) if tp + fp else 0.0
            rec = tp / (tp + fn) if tp + fn else 0.0
            m.update({"precision": prec, "recall": rec, "f1": (2 * prec * rec / (prec + rec)) if prec + rec else 0.0})
        out["metrics"][vid] = m
        out["pr"][vid] = _pr_points(y, p)
        if vid in ("V4", "V3", "V2", "V6", "RULE"):
            out["reliability"][vid] = _reliability(y, p)
        if not extras:
            continue
        keys = c["keys"]
        bm = {}
        for month, idx in keys.groupby("month").groups.items():
            idx = np.asarray(list(idx))
            mm = _metrics(y[idx], p[idx], thr_mean, clim_p[idx] if clim_p is not None else None)
            pred = p[idx] >= thr_row[idx]
            yy = y[idx]
            tp = float((pred & (yy == 1)).sum()); fp = float((pred & (yy == 0)).sum()); fn = float(((~pred) & (yy == 1)).sum())
            pr_ = tp / (tp + fp) if tp + fp else 0.0
            rc_ = tp / (tp + fn) if tp + fn else 0.0
            mm["f1"] = (2 * pr_ * rc_ / (pr_ + rc_)) if pr_ + rc_ else 0.0
            bm[month] = mm
        out["by_month"][vid] = bm
        bh = {}
        for hour, idx in keys.groupby("hour").groups.items():
            idx = np.asarray(list(idx))
            pred = p[idx] >= thr_row[idx]
            yy = y[idx]
            tp = float((pred & (yy == 1)).sum()); fp = float((pred & (yy == 0)).sum()); fn = float(((~pred) & (yy == 1)).sum())
            pr_ = tp / (tp + fp) if tp + fp else 0.0
            rc_ = tp / (tp + fn) if tp + fn else 0.0
            bh[int(hour)] = {"n": int(len(idx)), "fog_n": int(yy.sum()),
                             "f1": (2 * pr_ * rc_ / (pr_ + rc_)) if pr_ + rc_ else 0.0, "precision": pr_, "recall": rc_}
        out["by_hour"][vid] = bh
        if vid == "V4":
            rows = []
            for station, idx in keys.groupby("station").groups.items():
                idx = np.asarray(list(idx))
                yy = y[idx]
                if yy.sum() < 20:
                    continue
                mm = _metrics(yy, p[idx], thr_mean, clim_p[idx] if clim_p is not None else None)
                rows.append({"station": station, "n": mm["n"], "fog_n": mm["fog_n"], "ap": mm["ap"], "f1": mm["f1"],
                             "clim_ap": _metrics(yy, clim["p"][idx], thr_mean)["ap"] if clim is not None else None})
            rows.sort(key=lambda r: -(r["fog_n"] or 0))
            out["by_station"] = rows
    return out


def production_comparison(chrono_keys, v4_p, v4_thr, v2_p=None):
    """predictions.dbの本番予測(検証済み分)と、新モデルを同じ(地点, 時刻)で比べる。
    本番は1つの対象時刻に複数の発表時刻の予測があるため、リードタイムが最短(最も新しい予報)の
    1件を使う。新モデルの予測は同じ(地点, 時刻)の時系列ホールドアウト予測。"""
    from sklearn.metrics import average_precision_score, brier_score_loss
    if not PREDICTIONS_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{PREDICTIONS_DB}?mode=ro", uri=True)
    try:
        df = pd.read_sql(
            "SELECT station, target_datetime, lead_hours, predicted_code, predicted_fog_prob, actual_code "
            "FROM predictions WHERE actual_code IS NOT NULL AND lead_hours >= 0 AND target_datetime >= ?",
            conn, params=(CHRONO_SPLIT.strftime("%Y-%m-%d %H:%M:%S"),),
        )
    finally:
        conn.close()
    if df.empty:
        return None
    df["datetime"] = pd.to_datetime(df["target_datetime"])
    df = df.sort_values("lead_hours").drop_duplicates(["station", "datetime"], keep="first")
    prim = lambda v: np.where(v > 10, v // 100, v)  # noqa: E731
    df["pred_fog"] = np.isin(prim(df["predicted_code"].to_numpy()), [1, 2, 3])
    new = chrono_keys[["datetime", "station"]].copy()
    new["new_p"] = v4_p
    merged = df.merge(new, on=["station", "datetime"], how="inner")
    if merged.empty:
        return None
    y = np.isin(prim(merged["actual_code"].to_numpy()), [1, 2, 3]).astype(int)
    prod_pred = merged["pred_fog"].to_numpy()
    tp = float((prod_pred & (y == 1)).sum()); fp = float((prod_pred & (y == 0)).sum()); fn = float(((~prod_pred) & (y == 1)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    prod_f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    new_f1, new_prec, new_rec = _f1_at(y, merged["new_p"].to_numpy(), v4_thr)
    rate = float(y.mean())
    return {
        "n": int(len(y)), "fog_n": int(y.sum()), "rate": rate,
        "median_lead_hours": float(merged["lead_hours"].median()),
        "production": {
            "f1": prod_f1, "precision": prec, "recall": rec,
            "ap": float(average_precision_score(y, merged["predicted_fog_prob"])) if y.sum() else None,
            "brier": float(brier_score_loss(y, np.clip(merged["predicted_fog_prob"], 0, 1))),
        },
        "new_v4": {
            "f1": new_f1, "precision": new_prec, "recall": new_rec,
            "ap": float(average_precision_score(y, merged["new_p"])) if y.sum() else None,
            "brier": float(brier_score_loss(y, np.clip(merged["new_p"], 0, 1))),
        },
        "climatology_brier": float(brier_score_loss(y, np.full(len(y), rate))),
    }


def permutation_importance(model, cols, rate, test_df, base_p, repeats=3):
    from sklearn.metrics import average_precision_score
    te = test_df.copy()
    te["地点別川霧発生率"] = te["station"].map(rate).fillna(0.0)
    y = te["y"].to_numpy()
    if y.sum() == 0:
        return []
    base_ap = average_precision_score(y, base_p)
    rng = np.random.default_rng(0)
    rows = []
    for f in cols:
        drops = []
        for _ in range(repeats):
            t2 = te[cols].copy()
            t2[f] = rng.permutation(t2[f].to_numpy())
            drops.append(base_ap - average_precision_score(y, model.predict_proba(t2)[:, 1]))
        rows.append({"feature": f, "ap_drop": float(np.mean(drops))})
    rows.sort(key=lambda r: -r["ap_drop"])
    return rows


def spread_table(df_f):
    """気温露点差(予報値)ごとの川霧発生率。単独では川霧を識別しにくいことの確認用。"""
    bins = [(0, 0.5), (0.5, 1), (1, 2), (2, 4), (4, 99)]
    sp = df_f["気温露点差"]
    rows = []
    for lo, hi in bins:
        m = (sp >= lo) & (sp < hi)
        rows.append({"range": f"{lo}〜{hi if hi < 99 else ''}℃", "n": int(m.sum()),
                     "fog_rate": float(df_f.loc[m, "y"].mean()) if m.sum() else None})
    return rows


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def run():
    t0 = time.time()
    try:
        os.nice(10)  # 収集・予測バッチやWebを妨げないよう優先度を下げる
    except OSError:
        pass
    import fog_data

    _write_status("running", "データ読み込み")
    main_df, phenom_df, stations = fog_data.load_river_fog_data()
    lat, lon = representative_coords()
    labels = load_label_frame(stations, phenom_df)
    log.info("Labels: %d rows, fog rate %.4f", len(labels), labels["y"].mean())

    _write_status("running", "Open-Meteo過去予報の取得")
    start_date = labels["datetime"].min().date()
    end_date = (datetime.now(JST) - timedelta(days=2)).date()
    hist = fetch_historical_forecast(lat, lon, start_date, end_date)

    _write_status("running", "特徴量の作成")
    featA = build_weather_features(main_df, lat, lon, with_extra=False)
    featF = build_weather_features(hist, lat, lon, with_extra=False)
    featF5 = build_weather_features(hist, lat, lon, with_extra=True)
    # 両方のソースで主要な気象値がそろっている時刻だけを使い、比較を公平にする
    okA = featA[KEY_COLS].notna().all(axis=1)
    okF = featF[KEY_COLS].notna().all(axis=1)
    common = okA[okA].index.intersection(okF[okF].index)
    lab = labels[labels["datetime"].isin(common)].reset_index(drop=True)
    lab = lab[lab["datetime"] <= pd.Timestamp(end_date) + pd.Timedelta(hours=23)].reset_index(drop=True)
    log.info("Rows with both reanalysis and historical-forecast weather: %d (fog rate %.4f)", len(lab), lab["y"].mean())

    dA = add_condition_features(lab.join(featA, on="datetime"))
    dF = add_condition_features(lab.join(featF, on="datetime"))
    dF5 = lab.join(featF5, on="datetime")
    datasets = {"A": dA, "F": dF, "F5": dF5}
    base_feats = _feature_names()
    f5_feats = base_feats + EXTRA_FEATURES
    f6_feats = base_feats + COND_FEATURES

    variants_spec = {
        "CLIM": {"train": "A", "test": "A", "feats": None},
        "RULE": {"train": "F", "test": "F", "feats": None, "rule": True},
        "V6": {"train": "F", "test": "F", "feats": f6_feats},
        "V2": {"train": "A", "test": "A", "feats": base_feats},
        "V3": {"train": "A", "test": "F", "feats": base_feats},
        "V4": {"train": "F", "test": "F", "feats": base_feats},
        "V5": {"train": "F5", "test": "F5", "feats": f5_feats},
    }

    months_tbl = []
    for m, g in lab.groupby("month"):
        months_tbl.append({"month": m, "n": int(len(g)), "fog_n": int(g["y"].sum()), "stations": int(g["station"].nunique())})

    # --- 時系列ホールドアウト ------------------------------------------------
    _write_status("running", "時系列ホールドアウト評価")
    tr_mask = (lab["datetime"] < CHRONO_SPLIT).to_numpy()
    te_mask = (lab["datetime"] >= CHRONO_SPLIT).to_numpy()
    chrono = None
    if te_mask.sum() > 0 and lab.loc[te_mask, "y"].sum() > 0:
        coll_c = evaluate_scheme(datasets, [(tr_mask, te_mask, "train<2026-09 / test>=2026-09")], variants_spec, "chrono")
        chrono = summarize_scheme(coll_c)
        chrono["window"] = [CHRONO_SPLIT.strftime("%Y-%m-%d"), lab["datetime"].max().strftime("%Y-%m-%d")]
        chrono["n_train"] = int(tr_mask.sum()); chrono["n_test"] = int(te_mask.sum())
        chrono["production_comparison"] = production_comparison(
            coll_c["V4"]["keys"], coll_c["V4"]["p"], float(np.mean(coll_c["V4"]["thr"])))
        # 特徴量の重要度(permutation, 時系列ホールドアウトのV4)
        _write_status("running", "特徴量重要度")
        p, model, cols, rate = fit_predict(dF[tr_mask], dF[te_mask], base_feats, return_model=True)
        chrono["importance"] = permutation_importance(model, cols, rate, dF[te_mask], p)
        p6, model6, cols6, rate6 = fit_predict(dF[tr_mask], dF[te_mask], f6_feats, return_model=True)
        chrono["importance_v6"] = permutation_importance(model6, cols6, rate6, dF[te_mask], p6)

    # --- leave-one-month-out ------------------------------------------------
    _write_status("running", "月別leave-one-out評価")
    test_months = [m for m, g in lab.groupby("month") if g["y"].sum() >= MIN_FOG_HOURS_FOR_TEST_MONTH]
    folds = []
    for m in test_months:
        te = (lab["month"] == m).to_numpy()
        folds.append((~te, te, m))
    lomo = None
    if folds:
        coll_l = evaluate_scheme(datasets, folds, variants_spec, "lomo")
        lomo = summarize_scheme(coll_l)
        lomo["test_months"] = test_months

    # --- 参考: 全データで学習したV4の特徴量利得 --------------------------------
    gain = []
    try:
        _, model_all, cols_all, _ = fit_predict(dF, dF.iloc[:10], base_feats, return_model=True)
        imp = model_all.booster_.feature_importance(importance_type="gain")
        tot = float(imp.sum()) or 1.0
        gain = sorted(({"feature": c, "gain_share": float(v) / tot} for c, v in zip(cols_all, imp)),
                      key=lambda r: -r["gain_share"])
    except Exception as e:  # noqa: BLE001
        log.warning("gain importance failed: %s", e)

    regions = load_camera_regions()
    result = {
        "meta": {
            "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            "duration_sec": round(time.time() - t0, 1),
            "n_rows": int(len(lab)), "n_fog": int(lab["y"].sum()), "fog_rate": float(lab["y"].mean()),
            "n_stations": int(lab["station"].nunique()),
            "date_range": [lab["datetime"].min().strftime("%Y-%m-%d"), lab["datetime"].max().strftime("%Y-%m-%d")],
            "regions": {r: int(sum(1 for s in lab["station"].unique() if regions.get(s) == r)) for r in set(regions.values())},
            "months": months_tbl,
            "historical_forecast": {
                "rows": int(len(hist)), "range": [hist["datetime"].min().strftime("%Y-%m-%d"), hist["datetime"].max().strftime("%Y-%m-%d")],
                "point": [lat, lon],
                "note": "Open-Meteo historical-forecast-api(各予報時刻の直近のモデル出力をつないだもの。リードタイム約0〜1日)。",
            },
            "model": {"type": "LightGBM(二値)", "params": {k: v for k, v in LGBM_PARAMS.items() if k not in ('n_jobs', 'verbosity')},
                      "features": base_feats, "extra_features": EXTRA_FEATURES,
                      "station_feature": "地点別川霧発生率(学習データのみから計算、ベイズ平滑化)"},
        },
        "variants": VARIANTS,
        "chrono": chrono,
        "lomo": lomo,
        "gain_importance": gain,
        "spread_table": spread_table(dF),
        "condition_index": {
            "definition": {
                "気温": f"{COND_TEMP[0]:g}〜{COND_TEMP[1]:g}℃", "気温露点差": f"{COND_SPREAD[0]:g}〜{COND_SPREAD[1]:g}℃",
                "風速": f"{COND_WIND[0]:g}〜{COND_WIND[1]:g}m/s",
                "時間帯": "・".join(f"{a}〜{b}時" for a, b in COND_HOURS),
            },
            "forecast": condition_table(dF), "reanalysis": condition_table(dA),
        },
    }
    EXP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RESULT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_jsonable(result), ensure_ascii=False), encoding="utf-8")
    tmp.replace(RESULT_PATH)
    _write_status("done", "完了", finished_at=datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                  duration_sec=round(time.time() - t0, 1))
    log.info("Done in %.1fs -> %s", time.time() - t0, RESULT_PATH)


if __name__ == "__main__":
    _setup_logging()
    try:
        run()
    except Exception as e:  # noqa: BLE001
        log.error("experiment failed: %s\n%s", e, traceback.format_exc())
        _write_status("error", "失敗", error=str(e))
        sys.exit(1)
