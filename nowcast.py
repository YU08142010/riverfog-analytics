"""当日の短時間予測(ナウキャスト): 直前のカメラ観測を入れて、その日の残りの時間を毎時予測し直す。

前日夜(predict.py、19:30)の予測は、翌日の朝の霧が出たかどうかを知らないまま一日中使われる。
川霧は一度出ると数時間続きやすく(直近6週で、霧の1時間後も霧の割合は34%、霧でない時の1時間後は2%)、
「今どこで霧が見えているか」は数時間先の予測の強い手がかりになる。そこで、気象(予報)・地点別
川霧発生率に、発表時刻t0の観測(その地点の今・1時間前・2時間前の霧、全地点のうち霧の地点の割合)と
先行時間(t0から何時間先か)を加えた全地点共通のLightGBMを別に学習し、毎時の収集(collect.py)の後に
その日の残りの日中(最大MAX_LEAD_HOURS時間先)を予測し直す。

効果の確認(data/experiments/nowcast_and_source_check2.json): 直近6週を2週×3でずらし、評価期間は
過去に実際に出されていた予報を入力にした評価で、AP 0.202→0.260(差の95%区間 +0.011〜+0.104)、
loglossも有意に改善。先行1〜8時間のどれでも改善した(1時間先 0.211→0.296、8時間先 0.247→0.308)。
早期打ち切りで木の本数が下限(50本)に張り付くと改善が消えたため、木は最低MIN_TREES本にする。
基本モデルの確率を観測でロジスティック補正する方式は、自信過剰になり悪化したため採らない。

学習はpredict.py(毎晩)、予測はcollect.py(毎時)から呼ぶ。どちらも失敗しても本体の処理は止めない。
結果は data/models/nowcast_latest.json(公開ページの本日分の表示に使う)と、後で答え合わせできるよう
predictions.dbのnowcastsテーブルに残す(本番のpredictionsテーブル・精度の集計には混ぜない)。
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta

import joblib
import numpy as np
import pandas as pd

from common import BASE_DIR, JST, STATION_WEATHER_DB_PATH, load_camera_coords, representative_coords
import fog_data
import fog_model
import station_weather
from fog_features import SUMMARY_FOG_CODES, build_location_class_targets, daylight_mask, prepare_features

log = logging.getLogger("nowcast")

NOWCAST_STATE_PATH = BASE_DIR / "data" / "models" / "nowcast_state.joblib"
NOWCAST_LATEST_PATH = BASE_DIR / "data" / "models" / "nowcast_latest.json"
PREDICTIONS_DB_PATH = BASE_DIR / "data" / "predictions.db"
MODEL_TYPE_LABEL = "LightGBM(ナウキャスト・直前観測)"

TRAIN_LEADS = (1, 2, 3, 4, 6, 8)   # 学習に使う先行時間(全時間を使うと行数が増えるだけで差が出なかった)
MAX_LEAD_HOURS = 8                 # これより先は学習していないため、前日夜の予測のままにする
PARAMS = dict(num_leaves=15, min_child_samples=120)
MIN_TREES = 300
MAX_TREES = 1500
MAX_OBS_AGE_HOURS = 2              # 最新の観測がこれより古ければ(収集が止まっている等)更新しない
# 安全装置: 毎晩の学習時に評価期間(直近21日・予報入力)で、同じ行の基本モデル(観測なし)よりAPが
# この差以上悪ければ、ナウキャストを使わない(公開ページは前日夜の予測のままになる)。
# 2026-10-06の初回の学習では、直近21日は霧が少なくAP 0.169 対 0.172とほぼ同じだった
# (6週の実験では有意に改善。改善は霧の多かった時期に大きい)。
GUARD_AP_MARGIN = 0.02
LEAD_FEATURE = "先行時間"
OBS_FEATURES = ["観測_川霧_今", "観測_川霧_1h前", "観測_川霧_2h前", "観測_川霧_直近3h",
                "観測_広域川霧率_今", "観測_広域川霧率_1h前"]


class NowcastFogModel:
    """気象+地点別川霧発生率+発表時刻の観測+先行時間 → 川霧である確率。"""

    model_type = MODEL_TYPE_LABEL

    def __init__(self, model, weather_features, medians, station_rates, global_rate, threshold,
                 n_estimators, station_coords):
        self.model = model
        self.weather_features = list(weather_features)
        self.medians = dict(medians or {})
        self.station_rates = dict(station_rates)
        self.global_rate = float(global_rate)
        self.threshold = float(threshold)
        self.n_estimators = int(n_estimators)
        self.station_coords = dict(station_coords)

    @property
    def columns(self):
        return self.weather_features + OBS_FEATURES + [LEAD_FEATURE]

    def fog_probability(self, rows):
        """rows: self.columns と __station を持つDataFrame。"""
        if len(rows) == 0:
            return np.zeros(0)
        X = fog_model._features_with_rate(rows, self.columns, self.station_rates, self.global_rate)
        return np.asarray(self.model.predict_proba(X)[:, 1], dtype="float64")


# --- 観測の特徴量 ------------------------------------------------------------------------------

def _hourly(Y):
    return Y.reindex(pd.date_range(Y.index.min(), Y.index.max(), freq="h")) if len(Y) else Y


def observation_matrix_from_frame(frame):
    """学習データ(縦持ち)から、地点×時刻の川霧(1/0、無ければNaN)と、全地点のうち霧の地点の割合。"""
    Y = frame.pivot_table(index="__datetime", columns="__station", values="__y", aggfunc="max")
    Y = _hourly(Y)
    return Y, Y.mean(axis=1)


def observation_matrix_from_phenom(phenom_df, names, coords, since):
    """本番用: 最新の現象記録(人による訂正込み)から、学習時と同じ形の観測行列を作る。
    学習と同じく日中だけを使う(暗い時間帯の判定は信用できないためNaNにする)。"""
    phenom_df, _ = fog_data.apply_phenomena_corrections(phenom_df, fog_data.load_phenomena_corrections())
    recent = phenom_df[phenom_df["datetime"] >= since].drop_duplicates(subset="datetime")
    targets = build_location_class_targets(recent, names)
    lat0, lon0 = representative_coords()
    Y = pd.DataFrame(index=pd.DatetimeIndex(targets["datetime"]))
    for name in names:
        codes = targets[name].to_numpy(dtype="float64")
        fog = np.where(np.isnan(codes), np.nan, np.isin(codes, list(SUMMARY_FOG_CODES)).astype(float))
        s_lat, s_lon = coords.get(name, (lat0, lon0))
        fog[~daylight_mask(targets["datetime"], s_lat, s_lon)] = np.nan
        Y[name] = fog
    Y = _hourly(Y.sort_index())
    return Y, Y.mean(axis=1)


def obs_features(Y, region, t0, stations):
    """発表時刻t0(行ごと)時点の観測の特徴量。地点が観測行列に無い・時刻が範囲外ならNaN。"""
    t0 = pd.DatetimeIndex(t0)
    stations = np.asarray(stations)
    values = Y.to_numpy()
    cols = Y.columns.get_indexer(stations)

    def at(dt):
        idx = Y.index.get_indexer(dt)
        out = np.full(len(dt), np.nan)
        ok = (idx >= 0) & (cols >= 0)
        out[ok] = values[idx[ok], cols[ok]]
        return out

    now, prev1, prev2 = at(t0), at(t0 - pd.Timedelta(hours=1)), at(t0 - pd.Timedelta(hours=2))
    stacked = np.vstack([now, prev1, prev2])
    any3 = np.where(np.isnan(stacked).all(axis=0), np.nan, np.nanmax(np.nan_to_num(stacked, nan=-1), axis=0))
    return pd.DataFrame({
        "観測_川霧_今": now, "観測_川霧_1h前": prev1, "観測_川霧_2h前": prev2, "観測_川霧_直近3h": any3,
        "観測_広域川霧率_今": region.reindex(t0).to_numpy(),
        "観測_広域川霧率_1h前": region.reindex(t0 - pd.Timedelta(hours=1)).to_numpy(),
    })


def expand_rows(rows, Y, region, leads=TRAIN_LEADS):
    """対象行(t, 地点)を、同じ日の発表時刻 t0 = t - k ごとの行に増やし、t0時点の観測を付ける。
    t0にどの地点の観測も無い行(夜明け前など、本番でも予測し直さない時刻)は除く。"""
    parts = []
    for k in leads:
        t0 = rows["__datetime"] - pd.Timedelta(hours=k)
        same_day = (t0.dt.date == rows["__datetime"].dt.date).to_numpy()
        sub = rows[same_day].reset_index(drop=True)
        feats = obs_features(Y, region, t0[same_day], sub["__station"].to_numpy())
        sub = pd.concat([sub, feats], axis=1)
        sub[LEAD_FEATURE] = float(k)
        parts.append(sub[~np.isnan(sub["観測_広域川霧率_今"].to_numpy())])
    if not parts:
        return rows.iloc[0:0]
    return pd.concat(parts, ignore_index=True)


def _forecast_rows(rows, hist_by_station, station_coords, weather_features, medians):
    """rowsの気象特徴量を、その時刻に実際に出されていた予報の値に置き換える(予報の無い行は除く)。"""
    keys = rows[["__datetime", "__station", "__y"]].drop_duplicates(["__datetime", "__station"])
    fc = fog_model._forecast_input_rows(keys, hist_by_station, station_coords, weather_features, medians)
    keep = [c for c in rows.columns if c not in weather_features]
    return rows[keep].merge(fc[["__datetime", "__station", *weather_features]], on=["__datetime", "__station"], how="inner")


# --- 学習(predict.pyから毎晩) -------------------------------------------------------------------

def train_nowcast_model(main_df, phenom_df, names, lat, lon, camera_coords=None, weather_conn=None, prev=None):
    """ナウキャストのモデルを学習する。期間の分け方・しきい値の決め方は本体(fog_model)と同じ。
    戻り値: (NowcastFogModel, metrics) 。学習できなければ (None, None)。"""
    frame, wf, medians, scoords = fog_model._build_training_frame(
        main_df, phenom_df, names, lat, lon, camera_coords=camera_coords, weather_conn=weather_conn,
    )
    if frame.empty:
        return None, None
    data_end = frame["__datetime"].max()
    test_start = data_end - pd.Timedelta(days=fog_model.TEST_WINDOW_DAYS)
    val_start = test_start - pd.Timedelta(days=fog_model.VALIDATION_WINDOW_DAYS)
    inner = frame[frame["__datetime"] <= val_start]
    val = frame[(frame["__datetime"] > val_start) & (frame["__datetime"] <= test_start)]
    test = frame[frame["__datetime"] > test_start]
    Y, region = observation_matrix_from_frame(frame)
    ei, ev = expand_rows(inner, Y, region), expand_rows(val, Y, region)
    if len(ei) < fog_model.MIN_TRAIN_ROWS or int(ei["__y"].sum()) < fog_model.MIN_TRAIN_POSITIVES or len(ev) == 0:
        log.warning("Nowcast: not enough training data (%d rows)", len(ei))
        return None, None
    cols = wf + OBS_FEATURES + [LEAD_FEATURE]
    r_i, g_i = fog_model._station_rates(inner)
    X = lambda rows, r, g: fog_model._features_with_rate(rows, cols, r, g)
    m1 = fog_model._fit_lgbm(X(ei, r_i, g_i), ei["__y"].to_numpy(), PARAMS, MAX_TREES,
                             eval_set=(X(ev, r_i, g_i), ev["__y"].to_numpy()))
    n_trees = max(MIN_TREES, int(round((m1.best_iteration_ or MAX_TREES) * fog_model.REFIT_ESTIMATOR_SCALE)))

    hist = {}
    try:
        hist = fog_model.fetch_historical_forecast_multi(scoords, (val_start - pd.Timedelta(days=2)).date(), data_end.date())
    except Exception as e:
        log.warning("Nowcast: historical forecast fetch failed; using observed weather for threshold/eval: %s", e)
    ev_in = _forecast_rows(ev, hist, scoords, wf, medians) if hist else ev
    if len(ev_in) == 0 or int(ev_in["__y"].sum()) < fog_model.MIN_POSITIVES_FOR_THRESHOLD:
        ev_in = ev
    y_v = ev_in["__y"].to_numpy()
    if int(y_v.sum()) >= fog_model.MIN_POSITIVES_FOR_THRESHOLD:
        threshold, _f1 = fog_model._best_threshold(y_v, m1.predict_proba(X(ev_in, r_i, g_i))[:, 1])
        threshold_source = "validation F1 max"
    else:
        threshold = getattr(prev, "threshold", None) or fog_model.DEFAULT_FOG_THRESHOLD
        threshold_source = "previous/default (too few fog rows)"

    # 評価: 「学習+検証」で学習し、評価期間(予報入力)で採点。同じ行での基本モデル(観測なし)とも比べる。
    metrics = {"threshold": threshold, "threshold_source": threshold_source, "n_trees": n_trees,
               "test_start": str(test_start), "test_end": str(data_end)}
    try:
        train = pd.concat([inner, val])
        et = expand_rows(train, Y, region)
        r_t, g_t = fog_model._station_rates(train)
        m_eval = fog_model._fit_lgbm(X(et, r_t, g_t), et["__y"].to_numpy(), PARAMS, n_trees)
        base_eval = fog_model._fit_lgbm(fog_model._features_with_rate(train, wf, r_t, g_t), train["__y"].to_numpy(),
                                        PARAMS, max(50, int(round(n_trees / 2))))
        tt = expand_rows(test, Y, region)
        tt = _forecast_rows(tt, hist, scoords, wf, medians) if hist else tt
        if len(tt) and int(tt["__y"].sum()):
            y = tt["__y"].to_numpy()
            p_now = m_eval.predict_proba(X(tt, r_t, g_t))[:, 1]
            p_base = base_eval.predict_proba(fog_model._features_with_rate(tt, wf, r_t, g_t))[:, 1]
            metrics["nowcast"] = fog_model.binary_metrics(y, (p_now >= threshold).astype(int), p_now)
            metrics["base_same_rows"] = {k: v for k, v in fog_model.binary_metrics(y, (p_base >= threshold).astype(int), p_base).items()
                                         if k in ("n", "n_fog", "ap", "brier")}
            metrics["input"] = "forecast" if hist else "observed"
            log.info("Nowcast test window: AP %.3f (base on same rows %.3f), F1 %.3f, %d rows / %d fog",
                     metrics["nowcast"]["ap"] or 0, metrics["base_same_rows"]["ap"] or 0,
                     metrics["nowcast"]["f1"] or 0, len(y), int(y.sum()))
    except Exception as e:
        log.warning("Nowcast evaluation failed: %s", e)

    ea = expand_rows(frame, Y, region)
    r_a, g_a = fog_model._station_rates(frame)
    final = fog_model._fit_lgbm(X(ea, r_a, g_a), ea["__y"].to_numpy(), PARAMS, n_trees)
    model = NowcastFogModel(final, wf, medians, r_a, g_a, threshold, n_trees, scoords)
    metrics.update({"n_train_rows": int(len(ea)), "n_train_fog": int(ea["__y"].sum())})
    log.info("Nowcast model trained: %d rows, %d trees, threshold %.2f [%s]", len(ea), n_trees, threshold, threshold_source)
    return model, metrics


def save_state(model, metrics, path=NOWCAST_STATE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    joblib.dump({"model": model, "metrics": metrics,
                 "trained_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")}, tmp)
    tmp.replace(path)


def load_state(path=NOWCAST_STATE_PATH):
    if not path.exists():
        return None
    try:
        return joblib.load(path)
    except Exception as e:
        log.warning("Failed to load nowcast state: %s", e)
        return None


def train_and_save(main_df, phenom_df, names, lat, lon, camera_coords=None, weather_conn=None):
    prev = (load_state() or {}).get("model")
    model, metrics = train_nowcast_model(main_df, phenom_df, names, lat, lon,
                                         camera_coords=camera_coords, weather_conn=weather_conn, prev=prev)
    if model is not None:
        save_state(model, metrics)
    return model, metrics


# --- 予測(collect.pyから毎時) --------------------------------------------------------------------

def _ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS nowcasts (
        issued_at TEXT NOT NULL, station TEXT NOT NULL, target_datetime TEXT NOT NULL,
        lead_hours INTEGER NOT NULL, prob REAL NOT NULL, predicted INTEGER NOT NULL,
        PRIMARY KEY (issued_at, station, target_datetime))""")


def run_nowcast(now=None):
    """最新の観測を起点に、その日の残りの日中(最大MAX_LEAD_HOURS時間先)を予測し直して保存する。
    戻り値: 予測した(地点×時刻の)件数。モデルが無い・観測が古い等で更新しなければ0。"""
    state = load_state()
    if not state or state.get("model") is None:
        log.info("Nowcast: no trained model yet; skipping")
        return 0
    model = state["model"]
    m = state.get("metrics") or {}
    ap_now, ap_base = (m.get("nowcast") or {}).get("ap"), (m.get("base_same_rows") or {}).get("ap")
    if ap_now is not None and ap_base is not None and ap_now < ap_base - GUARD_AP_MARGIN:
        log.warning("Nowcast disabled: test AP %.3f is worse than the base model's %.3f on the same rows", ap_now, ap_base)
        NOWCAST_LATEST_PATH.unlink(missing_ok=True)
        return 0
    now = now or datetime.now(JST).replace(tzinfo=None)
    main_df, phenom_df, names = fog_data.load_river_fog_data()
    coords = {**model.station_coords, **load_camera_coords()}
    Y, region = observation_matrix_from_phenom(phenom_df, names, coords, since=now - timedelta(hours=6))
    observed = region.dropna()
    if observed.empty:
        log.info("Nowcast: no daytime observation in the last hours; skipping")
        return 0
    t0 = observed.index.max()
    if t0.date() != now.date() or (now - t0.to_pydatetime()) > timedelta(hours=MAX_OBS_AGE_HOURS):
        log.info("Nowcast: latest observation %s is too old or not today; skipping", t0)
        return 0

    stations = [n for n in names if n in coords]
    forecasts = fog_model.fetch_forecast_range_multi({n: coords[n] for n in stations}, 2)
    rows = []
    for name in stations:
        fc = forecasts.get(name)
        if fc is None or fc.empty:
            continue
        s_lat, s_lon = coords[name]
        df, _, _ = prepare_features(fc, s_lat, s_lon, model.weather_features, medians=model.medians)
        targets = df[(df["datetime"] > t0) & (df["datetime"] <= t0 + pd.Timedelta(hours=MAX_LEAD_HOURS))
                     & (df["datetime"].dt.date == t0.date())]
        targets = targets[daylight_mask(targets["datetime"], s_lat, s_lon)]
        if targets.empty:
            continue
        part = targets[["datetime", *model.weather_features]].rename(columns={"datetime": "__datetime"}).copy()
        part["__station"] = name
        rows.append(part)
    if not rows:
        log.info("Nowcast: no remaining daytime hours today after %s", t0)
        return 0
    rows = pd.concat(rows, ignore_index=True)
    feats = obs_features(Y, region, [t0] * len(rows), rows["__station"].to_numpy())
    rows = pd.concat([rows.reset_index(drop=True), feats], axis=1)
    rows[LEAD_FEATURE] = ((rows["__datetime"] - t0) / pd.Timedelta(hours=1)).astype(float)
    probs = model.fog_probability(rows)

    issued = t0.strftime("%Y-%m-%d %H:%M:%S")
    out = {"issued_at": issued, "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
           "threshold": model.threshold, "model_type": MODEL_TYPE_LABEL, "stations": {}}
    db_rows = []
    for (dt, st, lead), p in zip(rows[["__datetime", "__station", LEAD_FEATURE]].itertuples(index=False), probs):
        out["stations"].setdefault(st, {})[str(dt.hour)] = round(float(p), 4)
        db_rows.append((issued, st, dt.strftime("%Y-%m-%d %H:%M:%S"), int(lead), float(p), int(p >= model.threshold)))
    tmp = NOWCAST_LATEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(NOWCAST_LATEST_PATH)
    try:
        conn = sqlite3.connect(PREDICTIONS_DB_PATH, timeout=30)
        try:
            _ensure_table(conn)
            conn.executemany("INSERT OR REPLACE INTO nowcasts VALUES (?, ?, ?, ?, ?, ?)", db_rows)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("Nowcast: failed to record rows in predictions.db: %s", e)
    log.info("Nowcast issued at %s: %d station-hour(s), %d predicted as river fog", issued, len(db_rows),
             sum(r[5] for r in db_rows))
    return len(db_rows)


def load_latest(today):
    """公開ページ用: 本日分の最新のナウキャスト(無い・古い・壊れていればNone)。"""
    try:
        data = json.loads(NOWCAST_LATEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not str(data.get("issued_at", "")).startswith(today.strftime("%Y-%m-%d")):
        return None
    return data


if __name__ == "__main__":
    # 手動実行: python nowcast.py train  (学習して保存) / python nowcast.py run (今の観測で予測)
    # __main__のままクラスを保存すると「__main__.NowcastFogModel」として記録され、collect.py等から
    # 読めなくなるため、モジュールとして読み込み直したものを使う。
    import sys
    import nowcast as _module
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "train":
        lat, lon = representative_coords()
        m, d, n = fog_data.load_river_fog_data()
        conn = station_weather.connect(STATION_WEATHER_DB_PATH)
        model, metrics = _module.train_and_save(m, d, n, lat, lon, camera_coords=load_camera_coords(), weather_conn=conn)
        print(json.dumps(metrics, ensure_ascii=False, default=str, indent=1))
    else:
        print(_module.run_nowcast())
