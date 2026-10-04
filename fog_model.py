"""川霧予測モデル: 全地点共通のLightGBM二値分類(川霧あり/なし)。

2026-10-04、それまでの「地点ごとにRandomForest・XGBoost・LightGBMとそのブレンドを学習し、
検証データの正答率で地点ごとに方式を選ぶ多クラス分類(現象コード0〜10)」から全面的に
置き換えた。置き換えた理由(管理者タブ「仮検証」の結果、data/experiments/参照):
  - 地点ごとのモデルは、検証データに川霧の実例が0〜2件しかない地点が多く、学習も評価も
    成り立っていなかった(川霧F1が0のまま、比較の仕方の不具合で更新も止まっていた)。
  - 正答率で方式を選ぶと、川霧(全体の3〜4%)を一度も出さないモデルが選ばれやすかった。
  - 本番の川霧F1(0.15)・確率の誤差(Brier 0.038)は、「地点×時刻ごとの過去の発生率を
    そのまま答える」だけの基準(気候値: F1 0.20 / Brier 0.030)より悪かった。全地点共通の
    二値LightGBMは同じ評価でF1 0.21 / Brier 0.029と、どちらも上回った。

予測は二値化して出す: 川霧である確率(fog_probability)がしきい値(fog_threshold、全地点共通)
以上なら「川霧あり」(1)、未満なら「川霧なし」(0)。

【学習の入力は実測、しきい値と評価の入力は予報】
モデルは気象の実測値(地点ごとのstation_weather.db、無ければ代表地点)で学習する。一方、本番の
予測は気象の予報値を入力にする。予報値は実測より湿度等の変化がなだらかで、同じモデルでも
確率が低めに出るため、実測を入力にして選んだしきい値(0.2前後)をそのまま予報に使うと、
ほとんど「川霧あり」にならなかった(2026-10-04の検証で16日分・65地点の予測のうち2件)。
そこで、検証期間・評価期間について過去に実際に出されていた予報(Open-Meteoの
historical-forecast-api)を取得し、しきい値の選択と精度の評価は本番と同じ「予報値を入力に
した場合」で行う。過去の予報が取得できなかった場合は実測を入力にした値で代用する
(その旨をglobal_metrics["input"]に記録する)。

【特徴量】気象(気温・湿度・露点・風・降水・日照・日射とその時間変化、時刻、日の出までの時間)と
地点別川霧発生率(学習期間の実測から計算し、少数サンプルは全体平均へ平滑化)。
「月」と、他地点の1時間前の実況から作る広域川霧指数・移流川霧指数(fog_advection.py)は使わない。
後者は本番の予報(最大16日先)の時点では存在しない値で、学習と予報で入力の意味が変わってしまう
ため(2026-10-04の比較で、予報と同じ条件にすると使わない場合と精度が変わらなかった)。
「月」は学習データに無い月(10月以降など)で不安定になるため使わない(仮検証と同じ構成)。

【期間の分け方】(すべて時系列順。未来のデータで過去を学習しない)
  [ ........ 学習(inner) ........ | 検証 VALIDATION_WINDOW_DAYS日 | 評価 TEST_WINDOW_DAYS日 ]
  - 検証期間: ハイパーパラメータ・木の本数(early stopping)・しきい値の選択
  - 評価期間: 「学習+検証」で学習したモデルの成績(気候値と比較)と、劣化チェック
  - 本番用のモデル: 選んだ設定で全期間(評価期間も含む)を使って学習し直したもの
"""
import csv
import logging
import shutil
import time
import warnings
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
import requests
from sklearn.metrics import average_precision_score, confusion_matrix, f1_score, log_loss

from common import BASE_DIR, JST, request_with_dns_fallback
import station_weather
import fog_tree_viz

try:
    import lightgbm as lgb
    from lightgbm import LGBMClassifier
    _HAS_LIGHTGBM = True
except Exception:
    lgb = None
    LGBMClassifier = None
    _HAS_LIGHTGBM = False

from fog_features import (
    COL_DEWPOINT,
    COL_HUMIDITY,
    COL_PRECIP,
    COL_SOLAR,
    COL_SUNSHINE,
    COL_TEMP,
    COL_VISIBILITY,
    COL_WIND_DIR_DEG,
    COL_WIND_SPEED,
    SUMMARY_FOG_CODES,
    build_location_class_targets,
    prepare_features,
)

log = logging.getLogger("fog_model")

MODEL_TYPE_LABEL = "LightGBM(二値・全地点共通)"
BINARY_LABELS = ("川霧なし", "川霧あり")

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# 過去に実際に出されていた予報(各時刻の直近のモデル出力をつないだもの)。しきい値の選択と
# 評価を本番と同じ「予報値を入力にした場合」で行うために使う。
OPEN_METEO_HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
# 予報取得は1回失敗しただけでその日の予測・グラフ・記録が丸ごとスキップされてしまうため、
# collect.pyの実況取得と同様に短いリトライを入れる。
FORECAST_FETCH_RETRIES = 2
FORECAST_FETCH_RETRY_DELAY_SEC = 3

# collect.pyが実況値取得に使っているOpen-Meteoの変数名 -> 学習データと同じ列名・単位への変換
_FORECAST_VARS = {
    "temperature_2m": (COL_TEMP, None),
    "precipitation": (COL_PRECIP, None),
    "relative_humidity_2m": (COL_HUMIDITY, None),
    "dew_point_2m": (COL_DEWPOINT, None),
    "wind_speed_10m": (COL_WIND_SPEED, None),
    "wind_direction_10m": (COL_WIND_DIR_DEG, None),
    "sunshine_duration": (COL_SUNSHINE, lambda v: v / 3600.0),
    "shortwave_radiation": (COL_SOLAR, lambda v: v * 0.0036),
    "visibility": (COL_VISIBILITY, lambda v: v / 1000.0),
}

# --- 期間の分け方 -------------------------------------------------------------------
# 評価期間は7日だと川霧の実例が100件前後しかなく、週ごとの成績のぶれが大きかったため21日にした。
TEST_WINDOW_DAYS = 21
VALIDATION_WINDOW_DAYS = 14

# 学習を行うための最低条件。満たさなければモデルを作らない(前回のモデルを使い続ける)。
MIN_TRAIN_ROWS = 500
MIN_TRAIN_POSITIVES = 30
# 予測の対象にする地点の最低行数(地点別川霧発生率は少数サンプルなら全体平均へ平滑化される
# ため、追加されたばかりの地点でも予測できる)。
MIN_ROWS_PER_STATION = 3

# 使わない特徴量(モジュールのdocstring参照)。
EXCLUDED_FEATURES = ("月",)

# --- LightGBM ------------------------------------------------------------------------
# 基本値は仮検証(experiment_model.py)で検証済みの設定。class_weightは使わない
# (使うと確率が実際の発生率より大きく出るようになり、確率としての意味が崩れるため。
# 川霧が少ないことへの対応は、しきい値の選択で行う)。
LGBM_BASE_PARAMS = dict(
    objective="binary", learning_rate=0.04, subsample=0.8, subsample_freq=1,
    colsample_bytree=0.8, reg_lambda=5.0, random_state=0, n_jobs=2, verbosity=-1,
)
LGBM_PARAM_GRID = (
    dict(num_leaves=15, min_child_samples=60),
    dict(num_leaves=31, min_child_samples=60),
    dict(num_leaves=15, min_child_samples=120),
    dict(num_leaves=31, min_child_samples=30),
)
MAX_ESTIMATORS = 800
EARLY_STOPPING_ROUNDS = 50
# 検証期間で決めた木の本数を、データの増えた期間で学習し直す際に少し増やす係数。
REFIT_ESTIMATOR_SCALE = 1.2

# --- しきい値 --------------------------------------------------------------------------
FOG_THRESHOLD_GRID = tuple(round(t, 2) for t in np.arange(0.02, 0.91, 0.01))
# 検証期間に川霧がほとんど無い(冬季など)場合は、しきい値を選べないため前回の値を使い、
# 前回の値も無ければこの既定値を使う(仮検証で、予報を入力にした場合にF1が最大になった値)。
DEFAULT_FOG_THRESHOLD = 0.05
MIN_POSITIVES_FOR_THRESHOLD = 10

# 地点ごとの過去の川霧発生率(学習期間の実測から計算)。少数サンプルは全体平均へ平滑化する。
STATION_FOG_RATE_FEATURE = "地点別川霧発生率"
STATION_RATE_SMOOTHING = 20

# 短い欠測(視程・降水量が単発で欠けている等)は行ごと捨てず時間方向に補間して活かす。
WEATHER_INTERPOLATE_LIMIT = 3

# Geminiの分類confidenceのログ。confidence:lowだったラベルは学習から除外する。
CONFIDENCE_LOG_PATH = BASE_DIR / "data" / "models" / "confidence_log.csv"
# backfill_weather.pyが後から推定で埋めた(実測ではない)日時のログ。その日時は学習から除外する。
BACKFILL_LOG_PATH = BASE_DIR / "data" / "models" / "backfill_log.csv"

# --- 保存先 ----------------------------------------------------------------------------
MODEL_STATE_PATH = BASE_DIR / "data" / "models" / "model_state.joblib"
# 旧方式(地点別モデル)の確率較正モデル。新方式では使わないため、predict.pyが削除する。
FOG_CALIBRATOR_PATH = BASE_DIR / "data" / "models" / "fog_calibrator.joblib"
METRICS_HISTORY_PATH = BASE_DIR / "data" / "models" / "model_metrics_history.csv"
METRICS_HISTORY_FIELDS = ["trained_at", "station", "model_type", "pooled", "n", "accuracy", "f1", "fog_f1"]

# --- 劣化チェック(apply_regression_guard) ------------------------------------------------
# 新しいモデルの評価期間での確率の誤差(Brier)が、同じ行での気候値より統計的に有意に悪ければ
# (=気象から何も学べていない、学習データの異常等で壊れている)、前回のモデルを使い続ける。
# 前回のモデルと直接比べないのは、比べる行が「前回のモデルが学習していない最近の数日」に
# 限られて少なく、また前回に戻し続けると比較が毎日同じになり更新が止まる(2026-10-03に
# 地点別モデルで実際に起きた)ため。気候値との比較は毎回、新しいモデル自身が学習していない
# 評価期間の行だけで行えるので、どちらの問題も起きない。
REGRESSION_CONFIDENCE = 0.95
REGRESSION_N_BOOTSTRAP = 500
MIN_ROWS_FOR_REGRESSION_CHECK = 200


class BinaryFogModel:
    """全地点共通の二値(川霧あり/なし)LightGBMモデル。地点別川霧発生率は学習時の値を
    モデル自身が持ち、予測時に地点名から引いて特徴量に加える(新しい地点は全体平均)。"""

    model_type = MODEL_TYPE_LABEL
    classes_ = np.array([0, 1])

    def __init__(self, model, base_features, station_rates, global_rate, threshold, n_estimators, params):
        self.model = model
        self.base_features = list(base_features)
        self.station_rates = dict(station_rates)
        self.global_rate = float(global_rate)
        self.threshold = float(threshold)
        self.n_estimators = int(n_estimators)
        self.params = dict(params)

    @property
    def features(self):
        return self.base_features + [STATION_FOG_RATE_FEATURE]

    def station_rate(self, station):
        return float(self.station_rates.get(station, self.global_rate))

    def _with_rate(self, X, stations):
        X = X[self.base_features].copy()
        if isinstance(stations, str):
            X[STATION_FOG_RATE_FEATURE] = self.station_rate(stations)
        else:
            X[STATION_FOG_RATE_FEATURE] = [self.station_rate(s) for s in stations]
        return X

    def fog_probability(self, X, stations):
        """川霧である確率(0〜1)。stationsは地点名1つ、または行ごとの地点名の配列。"""
        if len(X) == 0:
            return np.zeros(0)
        return np.asarray(self.model.predict_proba(self._with_rate(X, stations))[:, 1], dtype="float64")

    def predict(self, X, stations):
        """二値化した予測(1=川霧あり、0=川霧なし)。"""
        return (self.fog_probability(X, stations) >= self.threshold).astype(int)


def normalize_model_type_label(model_type):
    """保存データに残っている旧方式の名称を表示用に読み替える(旧称"hybrid"はRF+XGBoostの旧名)。"""
    if not model_type:
        return model_type
    return model_type.replace("hybrid", "RF+XGBoost")


def is_fog_code(value):
    """現象コード(2コード同時観測のパック値を含む)が川霧(SUMMARY_FOG_CODES)かどうか。
    川霧は家族グループの並び上、2コード同時でも必ず主コード(数値の小さい方)側に入る。
    二値の予測値(1=川霧あり、0=川霧なし)にもそのまま使える(1は川霧コードに含まれる)。"""
    if value is None:
        return False
    try:
        if value != value:  # NaN
            return False
        code = int(value)
    except (TypeError, ValueError):
        return False
    primary = code // 100 if code > 10 else code
    return primary in SUMMARY_FOG_CODES


# --- 評価指標 -------------------------------------------------------------------------

def binary_metrics(y_true, y_pred, probs=None):
    """二値の評価指標。川霧の実例が無い場合、F1・recall・APはNone(定義できないため)。"""
    y_true = np.asarray(y_true, dtype=int)
    y_pred = np.asarray(y_pred, dtype=int)
    n = len(y_true)
    tp = int(((y_true == 1) & (y_pred == 1)).sum())
    fp = int(((y_true == 0) & (y_pred == 1)).sum())
    fn = int(((y_true == 1) & (y_pred == 0)).sum())
    tn = int(((y_true == 0) & (y_pred == 0)).sum())
    out = {
        "n": n, "n_fog": tp + fn, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "accuracy": (tp + tn) / n if n else None,
        "precision": tp / (tp + fp) if (tp + fp) else None,
        "recall": tp / (tp + fn) if (tp + fn) else None,
        "f1": float(f1_score(y_true, y_pred, zero_division=0)) if (tp + fn) else None,
    }
    if probs is not None and n:
        probs = np.asarray(probs, dtype="float64")
        out["brier"] = float(np.mean((probs - y_true) ** 2))
        out["ap"] = float(average_precision_score(y_true, probs)) if (tp + fn) else None
    return out


def _best_threshold(y_true, probs):
    """F1が最大になるしきい値と、そのF1。"""
    best_t, best_f1 = None, -1.0
    for t in FOG_THRESHOLD_GRID:
        f1 = f1_score(y_true, (probs >= t).astype(int), zero_division=0)
        if f1 > best_f1:
            best_t, best_f1 = float(t), float(f1)
    return best_t, best_f1


# --- 学習データの準備 -------------------------------------------------------------------

def load_low_confidence_set(path=CONFIDENCE_LOG_PATH):
    """collect.pyが記録した確信度ログから、confidence=lowだった(datetime文字列, 地点名)の
    組をsetで返す。学習時にそれらのラベルを欠測扱いして除外するために使う。"""
    if not path.exists():
        return set()
    try:
        df = pd.read_csv(path)
    except Exception as e:
        log.warning("Failed to read confidence log (%s): %s", path, e)
        return set()
    if "confidence" not in df.columns:
        return set()
    low = df[df["confidence"] == "low"]
    return set(zip(low["datetime"].astype(str), low["station"].astype(str)))


def load_backfilled_datetime_set(path=BACKFILL_LOG_PATH):
    """backfill_weather.pyが実測ではなく後から推定で埋めた日時のset。"""
    if not path.exists():
        return set()
    try:
        df = pd.read_csv(path)
    except Exception as e:
        log.warning("Failed to read backfill log (%s): %s", path, e)
        return set()
    if "datetime" not in df.columns:
        return set()
    return set(df["datetime"].astype(str))


# station_weather.py（Open-Meteoのキー名）-> fog_features.py列名（学習・予報のDataFrameで使う名前）
_STATION_WEATHER_COL_MAP = {
    "temperature": COL_TEMP, "precipitation": COL_PRECIP, "wind_speed": COL_WIND_SPEED,
    "wind_direction_deg": COL_WIND_DIR_DEG, "solar_radiation_mj": COL_SOLAR,
    "sunshine_hours": COL_SUNSHINE, "dew_point": COL_DEWPOINT, "humidity": COL_HUMIDITY,
    "visibility_km": COL_VISIBILITY,
}


def _apply_station_weather(main_df, station_name, weather_conn):
    """station_weather.dbに地点固有の気象実測があれば、main_dfの該当日時だけその値で
    上書きしたコピーを返す(値が無い日時・欠測項目は共通の代表値のまま)。
    記録が1件も無ければNone(呼び出し側は共通のmain_dfをそのまま使う)。"""
    station_df = station_weather.load_station_weather_df(weather_conn, station_name)
    if station_df.empty:
        return None
    station_df = station_df.rename(columns=_STATION_WEATHER_COL_MAP)
    out = main_df.set_index("datetime")
    patch = station_df.set_index("datetime")
    out.update(patch)
    return out.reset_index()


def _build_training_frame(main_df, phenom_df, phenom_cols, lat, lon, camera_coords=None, weather_conn=None):
    """全地点の学習データを縦持ち(1行=地点×時刻)にまとめる。
    戻り値: (frame, weather_features, shared_medians, station_coords)
      frame: weather_features + __y(川霧なら1) + __datetime + __station
      station_coords: {地点名: (緯度, 経度)}(座標が無い地点は代表地点)
    """
    targets = build_location_class_targets(phenom_df, phenom_cols)

    low_confidence = load_low_confidence_set()
    if low_confidence:
        dt_strs = targets["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
        excluded_total = 0
        for col in phenom_cols:
            mask = dt_strs.apply(lambda s, c=col: (s, c) in low_confidence)
            n = int(mask.sum())
            if n:
                targets.loc[mask, col] = np.nan
                excluded_total += n
        if excluded_total:
            log.info("Excluded %d low-confidence label(s) from training", excluded_total)

    backfilled = load_backfilled_datetime_set()
    if backfilled:
        dt_strs = targets["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
        mask = dt_strs.isin(backfilled)
        if int(mask.sum()):
            targets.loc[mask, phenom_cols] = np.nan
            log.info("Excluded %d backfilled (estimated, not observed) datetime row(s) from training",
                     int(mask.sum()))

    # 特徴量の名前と任意項目(視程・日照・日射)の穴埋め用の中央値は、全地点共通の代表値の
    # データから1回だけ決める(1つのモデルなので、地点によって特徴量の構成が変わってはいけない)。
    shared = pd.merge(main_df, targets[["datetime"]], on="datetime", how="inner")
    _, all_features, shared_medians = prepare_features(shared.sort_values("datetime"), lat, lon)
    weather_features = [f for f in all_features if f not in EXCLUDED_FEATURES]

    frames = []
    station_coords = {}
    for col in phenom_cols:
        coords = (camera_coords or {}).get(col)
        s_lat, s_lon = coords if coords else (lat, lon)
        station_coords[col] = (s_lat, s_lon)
        base = None
        if weather_conn is not None and coords:
            base = _apply_station_weather(main_df, col, weather_conn)
        if base is None:
            base = main_df
        df = pd.merge(base, targets[["datetime", col]], on="datetime", how="inner")
        df = df.sort_values("datetime").reset_index(drop=True)
        df, _, _ = prepare_features(df, s_lat, s_lon, weather_features, medians=shared_medians)
        df[weather_features] = df[weather_features].interpolate(
            method="linear", limit=WEATHER_INTERPOLATE_LIMIT, limit_direction="both",
        )
        df = df.dropna(subset=[col, *weather_features])
        if df.empty:
            continue
        part = df[weather_features].copy()
        part["__y"] = df[col].isin(SUMMARY_FOG_CODES).astype(int).to_numpy()
        part["__datetime"] = df["datetime"].to_numpy()
        part["__station"] = col
        frames.append(part)

    if not frames:
        return pd.DataFrame(), weather_features, shared_medians, station_coords
    frame = pd.concat(frames, ignore_index=True).sort_values(["__datetime", "__station"]).reset_index(drop=True)
    return frame, weather_features, shared_medians, station_coords


def _station_rates(frame):
    """地点別川霧発生率(全体平均へ平滑化)と全体の発生率。"""
    global_rate = float(frame["__y"].mean()) if len(frame) else 0.0
    g = frame.groupby("__station")["__y"].agg(["sum", "count"])
    m = STATION_RATE_SMOOTHING
    rates = ((g["sum"] + m * global_rate) / (g["count"] + m)).to_dict()
    return rates, global_rate


def _climatology_probs(fit_frame, eval_frame):
    """基準(気候値): 学習期間の「地点×時刻」ごとの川霧発生率をそのまま確率とみなす。
    件数の少ない組み合わせはその地点の発生率へ平滑化する。"""
    rates, global_rate = _station_rates(fit_frame)
    g = (fit_frame.assign(__hour=fit_frame["__datetime"].dt.hour)
         .groupby(["__station", "__hour"])["__y"].agg(["sum", "count"]))
    m = STATION_RATE_SMOOTHING
    out = np.empty(len(eval_frame))
    for i, key in enumerate(zip(eval_frame["__station"], eval_frame["__datetime"].dt.hour)):
        base = rates.get(key[0], global_rate)
        if key in g.index:
            out[i] = (g.loc[key, "sum"] + m * base) / (g.loc[key, "count"] + m)
        else:
            out[i] = base
    return out


def _fit_lgbm(X, y, params, n_estimators, eval_set=None):
    model = LGBMClassifier(**LGBM_BASE_PARAMS, **params, n_estimators=n_estimators)
    if eval_set is None:
        model.fit(X, y)
        return model
    with warnings.catch_warnings():
        # LightGBM 4.7でeval_setが非推奨になった(動作は同じ)。後継のeval_X/eval_yは
        # 4.6以前に無いため、互換性を優先してeval_setのまま警告だけ抑える。
        warnings.filterwarnings("ignore", message=".*eval_set.*deprecated.*")
        model.fit(X, y, eval_set=[eval_set], eval_metric="binary_logloss",
                  callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)])
    return model


def _features_with_rate(frame, base_features, rates, global_rate):
    X = frame[base_features].copy()
    X[STATION_FOG_RATE_FEATURE] = frame["__station"].map(rates).fillna(global_rate).to_numpy()
    return X


def _forecast_input_rows(rows, hist_by_station, station_coords, weather_features, medians):
    """rows(学習データの縦持ちの一部)の各行について、その時刻に実際に出されていた予報を
    入力にした特徴量に置き換えたDataFrameを返す(予報が無い行は除く)。本番の予測と同じく、
    時間変化などの派生特徴量は予報の時系列そのものから計算する。"""
    parts = []
    for station, sub in rows.groupby("__station"):
        fc = hist_by_station.get(station)
        if fc is None or fc.empty:
            continue
        s_lat, s_lon = station_coords[station]
        df, _, _ = prepare_features(fc, s_lat, s_lon, weather_features, medians=medians)
        df = df[["datetime", *weather_features]].drop_duplicates(subset="datetime")
        merged = sub[["__datetime", "__station", "__y"]].merge(
            df, left_on="__datetime", right_on="datetime", how="inner",
        ).drop(columns="datetime")
        parts.append(merged.dropna(subset=weather_features))
    if not parts:
        return pd.DataFrame(columns=["__datetime", "__station", "__y", *weather_features])
    return pd.concat(parts, ignore_index=True).sort_values(["__datetime", "__station"]).reset_index(drop=True)


def train_shared_model(main_df, phenom_df, phenom_cols, lat, lon,
                       camera_coords=None, weather_conn=None, prev_state=None):
    """全地点共通の二値LightGBMを学習し、地点ごとの情報を付けた辞書を返す。
    戻り値: {地点名: info}。全地点のinfo["pipe"]は同じBinaryFogModelを指す。
    学習の条件を満たさない場合は空辞書(predict.pyは予測を行わず、前回のモデルが残る)。
    """
    if not _HAS_LIGHTGBM:
        log.error("lightgbm is not installed; cannot train the fog model")
        return {}
    if prev_state is None:
        prev_state = load_model_state()

    frame, weather_features, shared_medians, station_coords = _build_training_frame(
        main_df, phenom_df, phenom_cols, lat, lon, camera_coords=camera_coords, weather_conn=weather_conn,
    )
    if frame.empty:
        log.warning("No labeled rows available for training")
        return {}

    data_end = frame["__datetime"].max()
    test_start = data_end - pd.Timedelta(days=TEST_WINDOW_DAYS)
    val_start = test_start - pd.Timedelta(days=VALIDATION_WINDOW_DAYS)
    is_test = frame["__datetime"] > test_start
    is_val = (frame["__datetime"] > val_start) & ~is_test
    train, inner, val, test = frame[~is_test], frame[~is_test & ~is_val], frame[is_val], frame[is_test]
    log.info(
        "Training shared binary LightGBM: %d rows (%d fog), inner=%d / validation=%d (%d fog) / "
        "test=%d (%d fog), %d station(s), features=%s",
        len(frame), int(frame["__y"].sum()), len(inner), len(val), int(val["__y"].sum()),
        len(test), int(test["__y"].sum()), frame["__station"].nunique(), weather_features,
    )
    if len(train) < MIN_TRAIN_ROWS or int(train["__y"].sum()) < MIN_TRAIN_POSITIVES:
        log.warning("Not enough training data (%d rows, %d fog); keeping the previous model",
                    len(train), int(train["__y"].sum()))
        return {}
    if len(inner) == 0 or inner["__y"].nunique() < 2 or len(val) == 0:
        log.warning("Not enough data before the validation window; keeping the previous model")
        return {}

    # 検証・評価期間に実際に出されていた予報(本番と同じ入力条件)。取得できなければ実測で代用。
    hist_by_station = {}
    try:
        hist_by_station = fetch_historical_forecast_multi(
            station_coords, (val_start - pd.Timedelta(days=2)).date(), data_end.date(),
        )
        log.info("Fetched historical forecasts for %d/%d station(s)", len(hist_by_station), len(station_coords))
    except Exception as e:
        log.warning("Historical forecast fetch failed; thresholds/evaluation fall back to observed weather: %s", e)

    # 1) 検証期間で設定と木の本数を選ぶ(実測を入力に、early stoppingで)
    inner_rates, inner_global = _station_rates(inner)
    X_inner = _features_with_rate(inner, weather_features, inner_rates, inner_global)
    X_val = _features_with_rate(val, weather_features, inner_rates, inner_global)
    y_inner, y_val = inner["__y"].to_numpy(), val["__y"].to_numpy()
    val_pos = int(y_val.sum())
    candidates = []
    for params in LGBM_PARAM_GRID:
        model = _fit_lgbm(X_inner, y_inner, params, MAX_ESTIMATORS, eval_set=(X_val, y_val))
        p_val = model.predict_proba(X_val)[:, 1]
        ap = float(average_precision_score(y_val, p_val)) if val_pos else None
        ll = float(log_loss(y_val, p_val, labels=[0, 1]))
        candidates.append({"params": params, "ap": ap, "logloss": ll, "model": model,
                           "best_iteration": int(model.best_iteration_ or MAX_ESTIMATORS)})
        log.info("  candidate %s: validation AP=%s logloss=%.4f trees=%d",
                 params, _fmt(ap), ll, candidates[-1]["best_iteration"])
    if val_pos >= MIN_POSITIVES_FOR_THRESHOLD:
        chosen = max(candidates, key=lambda c: (c["ap"], -c["logloss"]))
    else:
        chosen = min(candidates, key=lambda c: c["logloss"])
    params, inner_model = chosen["params"], chosen["model"]

    # 2) しきい値: 検証期間で、予報を入力にした場合にF1が最大になる値
    #    (川霧が少なすぎれば前回の値→既定値)
    val_fc = _forecast_input_rows(val, hist_by_station, station_coords, weather_features, shared_medians)
    use_forecast = len(val_fc) > 0 and int(val_fc["__y"].sum()) >= MIN_POSITIVES_FOR_THRESHOLD
    thr_rows = val_fc if use_forecast else val
    thr_y = thr_rows["__y"].to_numpy()
    thr_p = inner_model.predict_proba(
        _features_with_rate(thr_rows, weather_features, inner_rates, inner_global))[:, 1]
    prev_threshold = _previous_shared_threshold(prev_state)
    if int(thr_y.sum()) >= MIN_POSITIVES_FOR_THRESHOLD:
        threshold, thr_f1 = _best_threshold(thr_y, thr_p)
        threshold_source = (f"validation F1 max on {'forecast' if use_forecast else 'observed'} inputs "
                            f"({thr_f1:.3f}, {int(thr_y.sum())} fog rows)")
    elif prev_threshold is not None:
        threshold, threshold_source = prev_threshold, f"previous model (only {int(thr_y.sum())} fog rows in validation)"
    else:
        threshold, threshold_source = DEFAULT_FOG_THRESHOLD, f"default (only {int(thr_y.sum())} fog rows in validation)"
    log.info("Chosen params %s, trees=%d, threshold=%.2f [%s]",
             params, chosen["best_iteration"], threshold, threshold_source)

    # 3) 評価: 「学習+検証」で学習したモデルを、評価期間で採点する(予報を入力に。無ければ実測)
    rates, global_rate = _station_rates(train)
    n_est = max(50, int(round(chosen["best_iteration"] * REFIT_ESTIMATOR_SCALE)))
    eval_model = _fit_lgbm(_features_with_rate(train, weather_features, rates, global_rate),
                           train["__y"].to_numpy(), params, n_est)
    test_fc = _forecast_input_rows(test, hist_by_station, station_coords, weather_features, shared_medians)
    eval_input = "forecast" if len(test_fc) > 0 else "observed"
    eval_rows = test_fc if eval_input == "forecast" else test.reset_index(drop=True)
    y_test = eval_rows["__y"].to_numpy()
    p_test = (eval_model.predict_proba(_features_with_rate(eval_rows, weather_features, rates, global_rate))[:, 1]
              if len(eval_rows) else np.zeros(0))
    pred_test = (p_test >= threshold).astype(int)
    overall = binary_metrics(y_test, pred_test, p_test)
    # 基準(気候値)も同じ行・同じ決め方(検証期間でF1最大)のしきい値で採点する
    clim_val = _climatology_probs(inner, thr_rows)
    clim_threshold = (_best_threshold(thr_y, clim_val)[0]
                      if int(thr_y.sum()) >= MIN_POSITIVES_FOR_THRESHOLD else threshold)
    clim_test = _climatology_probs(train, eval_rows) if len(eval_rows) else np.zeros(0)
    climatology = binary_metrics(y_test, (clim_test >= clim_threshold).astype(int), clim_test)
    guard = _guard_against_climatology(y_test, p_test, clim_test)
    log.info(
        "Test window (%s - %s, %s inputs, %d rows / %d fog): model AP=%s Brier=%s F1=%s recall=%s precision=%s"
        " / climatology AP=%s Brier=%s F1=%s",
        test_start, data_end, eval_input, len(y_test), int(y_test.sum()) if len(y_test) else 0,
        _fmt(overall.get("ap")), _fmt(overall.get("brier"), 4), _fmt(overall.get("f1")),
        _fmt(overall.get("recall")), _fmt(overall.get("precision")),
        _fmt(climatology.get("ap")), _fmt(climatology.get("brier"), 4), _fmt(climatology.get("f1")),
    )

    # 4) 本番用: 同じ設定で全期間(評価期間も含む)を使って学習し直す
    all_rates, all_global = _station_rates(frame)
    n_final = max(50, int(round(chosen["best_iteration"] * REFIT_ESTIMATOR_SCALE)))
    final = _fit_lgbm(_features_with_rate(frame, weather_features, all_rates, all_global),
                      frame["__y"].to_numpy(), params, n_final)
    model = BinaryFogModel(final, weather_features, all_rates, all_global, threshold, n_final, params)

    global_metrics = {
        "model": overall, "climatology": {**climatology, "threshold": clim_threshold},
        "input": eval_input, "threshold": threshold, "threshold_source": threshold_source,
        "params": params, "n_estimators": n_final, "guard": guard,
        "n_train": len(frame), "n_train_fog": int(frame["__y"].sum()),
        "train_start": str(frame["__datetime"].min()), "train_end": str(data_end),
        "validation_start": str(val_start), "test_start": str(test_start), "test_end": str(data_end),
    }

    models = {}
    eval_stations = eval_rows["__station"].to_numpy() if len(eval_rows) else np.array([])
    counts = frame.groupby("__station").size().to_dict()
    for col in phenom_cols:
        n = int(counts.get(col, 0))
        if n < MIN_ROWS_PER_STATION:
            continue
        mask = eval_stations == col
        y_s, p_s, pred_s = y_test[mask], p_test[mask], pred_test[mask]
        m = binary_metrics(y_s, pred_s, p_s) if len(y_s) else {}
        s_lat, s_lon = station_coords[col]
        models[col] = {
            "pipe": model, "model_type": MODEL_TYPE_LABEL, "shared": True, "pooled": False,
            "features": model.features, "weather_features": list(weather_features),
            "measure_medians": shared_medians, "station_fog_rate": model.station_rate(col),
            "fog_threshold": threshold, "n": n,
            "accuracy": m.get("accuracy"), "fog_f1": m.get("f1"), "f1": m.get("f1"),
            "precision": m.get("precision"), "recall": m.get("recall"),
            "n_test": len(y_s), "n_test_fog": int(y_s.sum()) if len(y_s) else 0,
            "y_test": y_s if len(y_s) else None, "y_pred": pred_s if len(y_s) else None,
            "p_test": p_s if len(y_s) else None,
            "train_end": pd.Timestamp(data_end), "lat": s_lat, "lon": s_lon,
            "global_metrics": global_metrics,
        }
    log.info("Shared model deployed to %d/%d station(s)", len(models), len(phenom_cols))
    return models


def _fmt(v, digits=3):
    return "n/a" if v is None else f"{v:.{digits}f}"


def _guard_against_climatology(y, p_model, p_clim):
    """評価期間の同じ行で、モデルと気候値の確率の誤差(Brier)を比べるブートストラップ。
    戻り値: {"n", "brier_model", "brier_climatology", "prob_not_worse", "passed"}"""
    n = len(y)
    if n < MIN_ROWS_FOR_REGRESSION_CHECK:
        return {"n": n, "passed": True, "reason": "too few rows for the check"}
    y = np.asarray(y, dtype="float64")
    err_m, err_c = (np.asarray(p_model) - y) ** 2, (np.asarray(p_clim) - y) ** 2
    idx = np.random.default_rng(1).integers(0, n, size=(REGRESSION_N_BOOTSTRAP, n))
    prob_not_worse = float(np.mean(err_m[idx].mean(axis=1) <= err_c[idx].mean(axis=1)))
    return {
        "n": n, "brier_model": float(err_m.mean()), "brier_climatology": float(err_c.mean()),
        "prob_not_worse": prob_not_worse, "passed": prob_not_worse >= 1.0 - REGRESSION_CONFIDENCE,
    }


def _shared_info(state):
    """保存済み状態のうち、全地点共通の二値モデルの情報(1地点分)。旧方式の状態ならNone。"""
    for info in (state or {}).values():
        if isinstance(info.get("pipe"), BinaryFogModel):
            return info
    return None


def _previous_shared_threshold(prev_state):
    info = _shared_info(prev_state)
    return float(info["fog_threshold"]) if info and info.get("fog_threshold") is not None else None


def apply_regression_guard(models, prev_state=None, path=MODEL_STATE_PATH):
    """新しいモデルが評価期間で気候値より統計的に有意に悪い(=壊れている)場合、前回の
    モデルに戻す(modelsを直接書き換える。全地点で1つのモデルなので全地点まとめて戻す)。
    前回が旧方式(地点別モデル)で戻せない場合は、警告だけ出して新しいモデルを使う。
    戻り値: 前回のモデルに戻した地点名のリスト(戻さなければ空)。"""
    if not models:
        return []
    guard = (next(iter(models.values())).get("global_metrics") or {}).get("guard") or {}
    if guard.get("passed", True):
        if "prob_not_worse" in guard:
            log.info("Regression check passed: Brier model=%.4f climatology=%.4f (%d rows), prob(model<=clim)=%.3f",
                     guard["brier_model"], guard["brier_climatology"], guard["n"], guard["prob_not_worse"])
        return []
    log.warning("Regression detected: Brier model=%.4f is significantly worse than climatology=%.4f (%d rows)",
                guard["brier_model"], guard["brier_climatology"], guard["n"])
    if prev_state is None:
        prev_state = load_model_state(path)
    if _shared_info(prev_state) is None or any(col not in prev_state for col in models):
        log.warning("No usable previous shared model to roll back to; keeping the new model")
        return []
    rolled_back = []
    for col in list(models):
        models[col] = dict(prev_state[col])
        rolled_back.append(col)
    return rolled_back


# --- 予報 ---------------------------------------------------------------------------------

def _forecast_payload_to_df(hourly):
    data = {"datetime": pd.to_datetime(hourly["time"])}
    for var, (label, convert) in _FORECAST_VARS.items():
        if var not in hourly:
            continue
        values = pd.to_numeric(pd.Series(hourly[var]), errors="coerce")
        data[label] = convert(values) if convert else values
    df = pd.DataFrame(data)
    if COL_PRECIP in df.columns:
        df[COL_PRECIP] = df[COL_PRECIP].fillna(0.0)
    numeric_cols = [c for c in df.columns if c != "datetime"]
    # 学習側のWEATHER_INTERPOLATE_LIMITと揃え、長時間の欠落を直線補間で埋めないようにする。
    df[numeric_cols] = df[numeric_cols].interpolate(
        method="linear", limit=WEATHER_INTERPOLATE_LIMIT, limit_direction="both",
    )
    df = df.dropna(subset=[COL_TEMP, COL_HUMIDITY, COL_DEWPOINT, COL_WIND_SPEED]).reset_index(drop=True)
    if df.empty:
        raise ValueError("Open-Meteoから有効な予報値を取得できませんでした。")
    return df


def _get_forecast_with_retry(params, timeout, url=OPEN_METEO_FORECAST_URL):
    """Open-Meteoの予報APIをリトライ付きで呼ぶ(DNSの一時的な失敗にもフォールバックする)。"""
    last_exc = None
    for attempt in range(1, FORECAST_FETCH_RETRIES + 2):
        try:
            resp = request_with_dns_fallback("GET", url, params=params, timeout=timeout, log=log)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            log.warning("Open-Meteo fetch failed (attempt %d/%d): %s", attempt, FORECAST_FETCH_RETRIES + 1, e)
            if attempt < FORECAST_FETCH_RETRIES + 1:
                time.sleep(FORECAST_FETCH_RETRY_DELAY_SEC)
    raise last_exc


def _common_params():
    return {
        "hourly": ",".join(_FORECAST_VARS.keys()), "timezone": "Asia/Tokyo",
        "wind_speed_unit": "ms", "precipitation_unit": "mm", "temperature_unit": "celsius",
    }


def fetch_forecast_range(lat, lon, forecast_days):
    """Open-Meteoから今日以降の時間別予報を取得する(collect.pyの実況取得と同じ単位設定)。"""
    params = {**_common_params(), "latitude": lat, "longitude": lon, "forecast_days": forecast_days}
    payload = _get_forecast_with_retry(params, timeout=20).json()
    if "hourly" not in payload:
        raise ValueError(f"Open-Meteoの応答にhourlyが含まれていません: {payload}")
    return _forecast_payload_to_df(payload["hourly"])


# 複数地点の応答の座標は、要求した座標そのものではなく予報モデルの格子点(約0.05度間隔。
# 標高等を考慮して隣の格子点が選ばれることもある)に寄せられて返ってくる。以前の許容差
# 0.05度では、格子点が少し遠い前川橋が毎回はじかれていたため、格子1つ半程度まで許容する。
MULTI_LOCATION_COORD_TOLERANCE_DEG = 0.1


def _parse_multi_payload(camera_coords, names, payload):
    """複数地点の応答を {地点名: DataFrame} にする。応答は要求した順に並んでいる前提だが、
    万一ずれていたら誤った地点同士を対応付けないよう、応答が返した座標と要求した座標の差を
    確認し、MULTI_LOCATION_COORD_TOLERANCE_DEGを超える地点はスキップする。"""
    if isinstance(payload, dict):
        payload = [payload]  # 地点が1件だけの時は単一オブジェクトで返ってくるため揃える
    result = {}
    for name, item in zip(names, payload):
        if not item or "hourly" not in item:
            continue
        expected_lat, expected_lon = camera_coords[name]
        echoed_lat, echoed_lon = item.get("latitude"), item.get("longitude")
        if echoed_lat is not None and echoed_lon is not None:
            if (abs(echoed_lat - expected_lat) > MULTI_LOCATION_COORD_TOLERANCE_DEG
                    or abs(echoed_lon - expected_lon) > MULTI_LOCATION_COORD_TOLERANCE_DEG):
                log.error(
                    "Open-Meteo multi-location response order mismatch for %s: expected (%.4f, %.4f), "
                    "got (%.4f, %.4f); skipping to avoid misaligned data",
                    name, expected_lat, expected_lon, echoed_lat, echoed_lon,
                )
                continue
        try:
            result[name] = _forecast_payload_to_df(item["hourly"])
        except ValueError:
            continue
    return result


def fetch_forecast_range_multi(camera_coords, forecast_days):
    """全地点分の予報を1回のAPI呼び出しでまとめて取得する。戻り値: {地点名: forecast_df}
    (取得・変換できた地点だけ)。"""
    if not camera_coords:
        return {}
    names = list(camera_coords.keys())
    params = {
        **_common_params(), "forecast_days": forecast_days,
        "latitude": ",".join(str(camera_coords[n][0]) for n in names),
        "longitude": ",".join(str(camera_coords[n][1]) for n in names),
    }
    return _parse_multi_payload(camera_coords, names, _get_forecast_with_retry(params, timeout=30).json())


def fetch_historical_forecast_multi(camera_coords, start_date, end_date):
    """指定期間に実際に出されていた予報(historical-forecast-api)を全地点分まとめて取得する。
    戻り値: {地点名: DataFrame}(本番の予報と同じ列名・単位)。"""
    if not camera_coords:
        return {}
    names = list(camera_coords.keys())
    params = {
        **_common_params(), "start_date": str(start_date), "end_date": str(end_date),
        "latitude": ",".join(str(camera_coords[n][0]) for n in names),
        "longitude": ",".join(str(camera_coords[n][1]) for n in names),
    }
    resp = _get_forecast_with_retry(params, timeout=90, url=OPEN_METEO_HISTORICAL_FORECAST_URL)
    return _parse_multi_payload(camera_coords, names, resp.json())


def _station_feature_frame(info, raw, lat, lon):
    """予報・シミュレーター用: 生の気象値のDataFrameから、学習時と同じ特徴量を組み立てる
    (任意項目の穴埋めは学習時の中央値を使う)。"""
    weather_features = info["weather_features"]
    df, _, _ = prepare_features(raw, lat, lon, weather_features, medians=info.get("measure_medians"))
    return df, df[weather_features].copy()


def build_location_forecast(models, forecast_raw, lat, lon, forecast_by_station=None, camera_coords=None):
    """予報データに対して地点ごとの川霧の二値予測と確率を計算する。
    地点別の予報があればそれを使い、欠けている時間だけ代表地点の予報で埋める。
    戻り値: (pred_df, prob_df, feature_snapshots)
      pred_df: datetime + 地点ごとの列(1=川霧あり、0=川霧なし)
      prob_df: datetime + 地点ごとの列(川霧である確率)
      feature_snapshots: {地点名: DataFrame(datetime, 各特徴量, predicted_fog, predicted_fog_prob)}
    """
    pred_df = pd.DataFrame({"datetime": forecast_raw["datetime"].values})
    prob_df = pd.DataFrame({"datetime": forecast_raw["datetime"].values})
    feature_snapshots = {}
    for col, info in models.items():
        station_forecast = (forecast_by_station or {}).get(col)
        if station_forecast is not None and len(station_forecast) > 0:
            f_raw = (
                station_forecast.set_index("datetime")
                .reindex(forecast_raw["datetime"])
                .combine_first(forecast_raw.set_index("datetime"))
                .reset_index()
            )
            f_lat, f_lon = (camera_coords or {}).get(col, (lat, lon))
        else:
            f_raw, f_lat, f_lon = forecast_raw, lat, lon
        df, X = _station_feature_frame(info, f_raw, f_lat, f_lon)
        model = info["pipe"]
        probs = model.fog_probability(X, col)
        preds = (probs >= model.threshold).astype(int)
        pred_df[col] = preds
        prob_df[col] = probs
        snapshot = X.copy()
        snapshot[STATION_FOG_RATE_FEATURE] = model.station_rate(col)
        snapshot.insert(0, "datetime", df["datetime"].values)
        snapshot["predicted_fog"] = preds
        snapshot["predicted_fog_prob"] = probs
        feature_snapshots[col] = snapshot
    return pred_df, prob_df, feature_snapshots


def simulate_station(info, station, lat, lon, weather, dt):
    """手動入力した1時点分の気象条件で、その地点の川霧を予測する(What-ifシミュレーター用)。
    weather: fog_features.COL_*をキーとする値のdict(欠けている項目は学習時の中央値で埋まる)。
    戻り値: (is_fog(1/0), fog_prob)
    """
    raw = pd.DataFrame([{**weather, "datetime": pd.Timestamp(dt)}])
    _, X = _station_feature_frame(info, raw, lat, lon)
    model = info["pipe"]
    prob = float(model.fog_probability(X, station)[0])
    return int(prob >= model.threshold), prob


# --- 保存・読み込み ---------------------------------------------------------------------------

_MODEL_STATE_CACHE = {"path": None, "mtime": None, "data": None}


def load_model_state(path=MODEL_STATE_PATH):
    """保存済みのモデル一式を読み込む。無い/壊れている/旧方式で読めない場合は空辞書。
    webapp.pyが頻繁に呼ぶため、ファイルの更新日時が変わらない間はプロセス内キャッシュを返す。"""
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    cache = _MODEL_STATE_CACHE
    if cache["data"] is not None and cache["path"] == path and cache["mtime"] == mtime:
        return cache["data"]
    try:
        data = joblib.load(path)
    except Exception as e:
        log.warning("Failed to load model state (%s): %s", path, e)
        return {}
    cache["path"], cache["mtime"], cache["data"] = path, mtime, data
    return data


def append_metrics_history(models, trained_at, path=METRICS_HISTORY_PATH):
    """地点ごとの評価期間の成績を履歴CSVに1行ずつ追記する(無ければヘッダーから作成)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=METRICS_HISTORY_FIELDS)
        if is_new:
            writer.writeheader()
        for col, info in models.items():
            def _v(key):
                v = info.get(key)
                return "" if v is None or (isinstance(v, float) and np.isnan(v)) else v
            writer.writerow({
                "trained_at": trained_at, "station": col,
                "model_type": info.get("model_type", ""), "pooled": info.get("pooled", False),
                "n": _v("n"), "accuracy": _v("accuracy"), "f1": _v("f1"), "fog_f1": _v("fog_f1"),
            })


def save_model_state(models, path=MODEL_STATE_PATH):
    """採用したモデル一式(劣化チェック後)を保存し、成績を履歴CSVに追記する。前回のモデルに
    戻した場合は、そのモデルを実際に学習した日時(trained_at)をそのまま残す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    trained_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    state = {col: {**info, "trained_at": info.get("trained_at") or trained_at} for col, info in models.items()}
    if path.exists():
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except Exception as e:
            log.warning("Failed to back up previous model state: %s", e)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    joblib.dump(state, tmp_path)
    tmp_path.replace(path)
    append_metrics_history(models, trained_at)


def remove_legacy_calibrator(path=FOG_CALIBRATOR_PATH):
    """旧方式の確率較正モデルを削除する(新方式では使わない)。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Failed to remove legacy calibrator %s: %s", path, e)


# --- 分析ページ用 --------------------------------------------------------------------------

def shared_global_metrics(state):
    """評価期間での全体の成績(モデル・気候値)。旧方式の状態ならNone。"""
    info = _shared_info(state)
    return info.get("global_metrics") if info else None


def compute_feature_importance(state):
    """採用中のモデルの特徴量重要度(分岐による損失の改善量=gainの割合)。
    戻り値: [(特徴量名, 割合), ...] 降順。計算できなければ空リスト。"""
    info = _shared_info(state)
    if info is None:
        return []
    model = info["pipe"]
    try:
        gains = model.model.booster_.feature_importance(importance_type="gain")
    except Exception as e:
        log.warning("Failed to compute feature importance: %s", e)
        return []
    total = float(np.sum(gains)) or 1.0
    result = [(name, float(g) / total) for name, g in zip(model.features, gains)]
    result.sort(key=lambda x: x[1], reverse=True)
    return result


def compute_confusion_matrix(state):
    """評価期間の全地点を合算した二値の混同行列。
    戻り値: (labels, matrix)。labelsは["川霧なし", "川霧あり"]、matrixは行=実際・列=予測。"""
    all_true, all_pred = [], []
    for info in state.values():
        if info.get("y_test") is None or info.get("y_pred") is None:
            continue
        all_true.extend(np.asarray(info["y_test"]).tolist())
        all_pred.extend(np.asarray(info["y_pred"]).tolist())
    if not all_true:
        return [], np.zeros((0, 0))
    return list(BINARY_LABELS), confusion_matrix(all_true, all_pred, labels=[0, 1])


def extract_representative_tree(pipe, feature_names):
    """採用中のLightGBMの1本目の木を、可視化用の共通ノード形式(fog_tree_viz参照)で取り出す。
    LightGBMは数百本の木の合計で判定するため、1本だけでは全体を表しきれない(木の深さ・
    複雑さの雰囲気をつかむための図)。戻り値: {"LightGBM": {...}}(取り出せなければ空dict)。"""
    if not isinstance(pipe, BinaryFogModel):
        return {}
    try:
        model = pipe.model
        dump = model.booster_.dump_model()
        root = fog_tree_viz.lgbm_tree_to_nodes(dump["tree_info"][0]["tree_structure"], feature_names)
        return {"LightGBM": {
            "root": root,
            "actual_max_depth": fog_tree_viz.tree_actual_max_depth(root),
            "configured_max_depth": model.max_depth if model.max_depth != -1 else None,
            "n_estimators": len(dump["tree_info"]),
        }}
    except Exception as e:
        log.warning("Failed to extract LightGBM tree for visualization: %s", e)
        return {}
