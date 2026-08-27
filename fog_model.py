"""地点ごとの霧予測モデル（RandomForest + XGBoostハイブリッド）の学習・予測。

~/Desktop/fog-prediction/weather_visualizer.py のモデル設計をそのまま踏襲している
（アルゴリズム自体はそちらで検証済みのものを流用し、Colab専用の周辺機能だけを外した）。
"""
import csv
import logging
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
import requests
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, make_scorer
from sklearn.model_selection import RandomizedSearchCV, TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.utils.class_weight import compute_sample_weight

from common import BASE_DIR, JST

try:
    from xgboost import XGBClassifier
    _HAS_XGBOOST = True
except Exception:
    XGBClassifier = None
    _HAS_XGBOOST = False

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
    _lag_series,
    build_location_class_targets,
    prepare_features,
    select_feature_names,
)

log = logging.getLogger("fog_model")

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

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

# 地点ごとにモデルを学習するための最低条件（満たさない地点は共通モデルで補う）
MIN_ROWS_PER_LOCATION = 200
MIN_CLASSES_PER_LOCATION = 2
MIN_TOTAL_ROWS_FOR_POOLING = 500
MIN_ROWS_FOR_POOLED_RESCUE = 30

FOG_THRESHOLD_GRID = tuple(round(t, 2) for t in np.arange(0.05, 1.0, 0.05))
DEFAULT_FOG_THRESHOLD = 0.5

PARAM_DIST = {
    "model__n_estimators": [200, 300, 500],
    "model__max_depth": [6, 8, 10, 12, 14, None],
    "model__min_samples_leaf": [1, 2, 3, 5],
    "model__max_features": ["sqrt", "log2", None],
}
XGB_PARAM_DIST = {
    "model__n_estimators": [200, 400],
    "model__max_depth": [3, 4, 6, 8],
    "model__learning_rate": [0.05, 0.1, 0.2],
    "model__subsample": [0.8, 1.0],
    "model__colsample_bytree": [0.8, 1.0],
    "model__min_child_weight": [1, 3, 5],
}
RF_FALLBACK_PARAMS = dict(model__n_estimators=500, model__max_depth=14, model__min_samples_leaf=2)
XGB_FALLBACK_PARAMS = dict(model__n_estimators=300, model__max_depth=4, model__learning_rate=0.1)
BLEND_WEIGHTS = [0.0, 0.25, 0.5, 0.75, 1.0]

# プーリングモデル専用の追加特徴量：地点ごとの過去の霧発生率（他の特徴量は全地点で同一の
# 気象データのため、これが無いと同時刻の予測が全地点で完全に一致してしまう）。
# サンプル数が少ない地点で経験値がそのまま乗ると不安定なので、全地点平均へ向けてベイズ平滑化する。
STATION_FOG_RATE_FEATURE = "地点別霧発生率"
STATION_RATE_SMOOTHING = 20

# 地点間の空間相関を表す特徴量：自地点を除く他地点の(CROSS_STATION_FOG_LAG_HOURS)時間前の
# 霧発生率。川霧は隣接区間で連動しやすいという想定に基づく。1時間前の値を使うのは、
# 同時刻の値をそのまま使うと「今まさに霧かどうか」という答えの情報が特徴量に漏れてしまう
# ため（他地点の現在値は、自地点の現在の判定と強く相関するが、それは推論時には使えない
# 未来情報に等しい）。
# 予報（未来）には他地点の実測が存在しないため、学習時のみ時間変化する値を使い、予報時は
# 直近の実測時点での値を一定値として引き継ぐ近似とする（視程等と同様、予報期間が長くなる
# ほど厳密ではなくなるが、無いよりは直近の広域的な傾向を反映できる）。
CROSS_STATION_FOG_LAG_HOURS = 1
CROSS_STATION_FOG_FEATURE = "広域霧指数"

# 短い欠測（視程・降水量が単発で欠けている等）は行ごと捨てず時間方向に補間して活かす。
# これを超える欠測はこれまで通り除外する。
WEATHER_INTERPOLATE_LIMIT = 3

# Geminiの分類confidenceのログ。collect.pyが分類のたびに追記し、ここでは
# confidence:lowだったラベルを学習から除外するために読む。
CONFIDENCE_LOG_PATH = BASE_DIR / "data" / "models" / "confidence_log.csv"

# モデル退行防止：前回学習時点よりスコア（霧F1、無ければ全体F1）が悪化した地点を自動検出する。
# 固定の許容誤差ではなく、各回のテストデータをブートストラップ再抽出して「今回のスコアが
# 前回以上になる確率」を推定し、それがREGRESSION_CONFIDENCE分の1未満（＝ノイズでは説明が
# つかないほど明確に悪化）と判断できた地点だけ、自動的に前回のモデルへロールバックする。
# サンプル数が少なすぎる、または前回の生データ(y_test/y_pred)が保存されていない（形式が古い等）
# 場合は、判定できないため比較自体を行わない。
MODEL_STATE_PATH = BASE_DIR / "data" / "models" / "model_state.joblib"
# model_state.joblibは常に最新状態で上書きされ過去の履歴が残らないため、精度が実際に
# 改善しているかを後で振り返れるよう、学習のたびに地点ごとのaccuracy/F1をここに追記する。
METRICS_HISTORY_PATH = BASE_DIR / "data" / "models" / "model_metrics_history.csv"
METRICS_HISTORY_FIELDS = ["trained_at", "station", "model_type", "pooled", "n", "accuracy", "f1", "fog_f1"]
REGRESSION_CONFIDENCE = 0.95
REGRESSION_N_BOOTSTRAP = 500
MIN_N_FOR_REGRESSION_CHECK = 30


class _XGBLabelSafeClassifier(ClassifierMixin, BaseEstimator):
    """歯抜けの現象コード（例: {0,1,2,7,9}）でもそのまま学習できるXGBoostのラッパ。"""

    def __init__(self, n_estimators=300, max_depth=4, learning_rate=0.1,
                 subsample=1.0, colsample_bytree=1.0, min_child_weight=1,
                 random_state=42):
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.subsample = subsample
        self.colsample_bytree = colsample_bytree
        self.min_child_weight = min_child_weight
        self.random_state = random_state

    def fit(self, X, y, sample_weight=None):
        y = np.asarray(y)
        self.classes_, encoded = np.unique(y, return_inverse=True)
        if sample_weight is None:
            sample_weight = compute_sample_weight("balanced", y)
        self.model_ = XGBClassifier(
            n_estimators=self.n_estimators, max_depth=self.max_depth,
            learning_rate=self.learning_rate, subsample=self.subsample,
            colsample_bytree=self.colsample_bytree,
            min_child_weight=self.min_child_weight,
            random_state=self.random_state, tree_method="hist", n_jobs=1,
            verbosity=0,
        )
        self.model_.fit(X, encoded, sample_weight=sample_weight)
        return self

    def predict_proba(self, X):
        return np.asarray(self.model_.predict_proba(X), dtype="float64")

    def predict(self, X):
        return self.classes_[np.argmax(self.predict_proba(X), axis=1)]


class HybridFogClassifier:
    """確率 = weight * RandomForest + (1 - weight) * XGBoost。weightは地点ごとに自動決定する。"""

    def __init__(self, rf=None, xgb=None, weight=1.0):
        if rf is None and xgb is None:
            raise ValueError("rf と xgb の少なくとも一方が必要です")
        if rf is None:
            weight = 0.0
        elif xgb is None:
            weight = 1.0
        self.rf = rf
        self.xgb = xgb
        self.weight = float(weight)
        self.classes_ = np.asarray((rf if rf is not None else xgb).classes_)

    @property
    def model_type(self):
        if self.rf is None:
            return "xgb"
        if self.xgb is None:
            return "rf"
        return "hybrid"

    def _aligned_proba(self, model, X):
        proba = np.asarray(model.predict_proba(X), dtype="float64")
        model_classes = list(np.asarray(model.classes_))
        if model_classes == list(self.classes_):
            return proba
        index = {c: i for i, c in enumerate(self.classes_)}
        out = np.zeros((proba.shape[0], len(self.classes_)), dtype="float64")
        for j, c in enumerate(model_classes):
            if c in index:
                out[:, index[c]] = proba[:, j]
        return out

    def predict_proba(self, X):
        if self.xgb is None:
            return self._aligned_proba(self.rf, X)
        if self.rf is None:
            return self._aligned_proba(self.xgb, X)
        return (self.weight * self._aligned_proba(self.rf, X)
                + (1.0 - self.weight) * self._aligned_proba(self.xgb, X))

    def predict(self, X):
        return self.classes_[np.argmax(self.predict_proba(X), axis=1)]


def compute_fog_probability(pipe, X):
    """霧（コード1〜6）である確率（predict_probaの該当クラス列の合計）。"""
    proba = pipe.predict_proba(X)
    fog_idx = [i for i, c in enumerate(pipe.classes_) if c in SUMMARY_FOG_CODES]
    if not fog_idx:
        return np.zeros(len(X))
    return proba[:, fog_idx].sum(axis=1)


def compute_feature_importance(state, only_stations=None):
    """学習済みモデル一式(model_state.joblibをload_model_stateしたもの)から、
    特徴量ごとの重要度を計算する。RandomForest/XGBoostのfeature_importances_を
    hybridの混合比(blend_weight)で重み付けし、複数地点で同一のpipeを共有している
    場合（プーリングモデル）は1回だけ数える。
    only_stationsを指定すると、その地点名の集合に含まれる地点のモデルだけを対象にする
    （精度が低い地点だけに絞って「何が効いていないか」を見る用途）。
    戻り値: [(特徴量名, 重要度), ...] 重要度の降順。計算できなければ空リスト。
    """
    seen_ids = set()
    totals, counts = {}, {}
    for station_name, info in state.items():
        if only_stations is not None and station_name not in only_stations:
            continue
        pipe = info.get("pipe")
        if pipe is None or id(pipe) in seen_ids:
            continue
        seen_ids.add(id(pipe))

        features = list(info.get("features", []))
        if info.get("pooled") and "station_fog_rate" in info:
            features = features + [STATION_FOG_RATE_FEATURE]

        rf_imp = xgb_imp = None
        try:
            if pipe.rf is not None:
                rf_imp = pipe.rf.named_steps["model"].feature_importances_
        except Exception:
            rf_imp = None
        try:
            if pipe.xgb is not None:
                xgb_imp = pipe.xgb.named_steps["model"].model_.feature_importances_
        except Exception:
            xgb_imp = None

        if rf_imp is not None and xgb_imp is not None \
                and len(rf_imp) == len(features) and len(xgb_imp) == len(features):
            importances = pipe.weight * rf_imp + (1.0 - pipe.weight) * xgb_imp
        elif rf_imp is not None and len(rf_imp) == len(features):
            importances = rf_imp
        elif xgb_imp is not None and len(xgb_imp) == len(features):
            importances = xgb_imp
        else:
            continue

        for name, val in zip(features, importances):
            totals[name] = totals.get(name, 0.0) + float(val)
            counts[name] = counts.get(name, 0) + 1

    result = [(name, totals[name] / counts[name]) for name in totals]
    result.sort(key=lambda x: x[1], reverse=True)
    return result


def compute_confusion_matrix(state):
    """全地点のテストデータ(y_test/y_pred)を合算した混同行列を計算する
    （どの現象コード同士をよく取り違えているかを把握するための診断用）。
    戻り値: (labels, matrix)。labelsは行・列に対応する現象コードの昇順リスト、
    matrixはlen(labels) x len(labels)のnumpy配列（行=実測、列=予測）。
    データが無ければ (空リスト, 空配列)。
    """
    all_true, all_pred = [], []
    for info in state.values():
        y_test = info.get("y_test")
        y_pred = info.get("y_pred")
        if y_test is None or y_pred is None:
            continue
        all_true.extend(np.asarray(y_test).tolist())
        all_pred.extend(np.asarray(y_pred).tolist())
    if not all_true:
        return [], np.zeros((0, 0))
    labels = sorted(set(all_true) | set(all_pred))
    matrix = confusion_matrix(all_true, all_pred, labels=labels)
    return labels, matrix


def _fog_class_f1(y_true, y_pred):
    fog_labels = [c for c in sorted(SUMMARY_FOG_CODES) if c in set(pd.Series(y_true).unique())]
    if not fog_labels:
        return np.nan
    return f1_score(y_true, y_pred, labels=fog_labels, average="macro", zero_division=0)


def _blend_score(y_true, y_pred):
    score = _fog_class_f1(y_true, y_pred)
    if np.isnan(score):
        score = f1_score(y_true, y_pred, average="macro", zero_division=0)
    return score


def _single_component_metrics(component_pipe, is_rf, X_test, y_test):
    """RandomForest単体・XGBoost単体それぞれで予測した場合のaccuracy・霧F1を返す
    （採用しているhybridとの性能比較を管理画面で見せるための診断用。モデル選択自体には
    使わない）。コンポーネントが学習されていない場合はNoneを返す。
    """
    if component_pipe is None or len(X_test) == 0:
        return None
    solo = HybridFogClassifier(component_pipe, None, weight=1.0) if is_rf \
        else HybridFogClassifier(None, component_pipe, weight=0.0)
    y_pred = solo.predict(X_test)
    fog_f1 = _fog_class_f1(y_test, y_pred)
    return {
        "accuracy": float(accuracy_score(y_test, y_pred)),
        "fog_f1": None if np.isnan(fog_f1) else float(fog_f1),
    }


FOG_F1_SCORER = make_scorer(_blend_score)


def _temporal_train_test_split(X, y, test_ratio=0.2):
    """時系列順を保ったまま、後ろtest_ratioをテスト（検証）データにする。"""
    split_idx = max(1, int(len(X) * (1 - test_ratio)))
    if split_idx >= len(X):
        split_idx = len(X) - 1
    return X.iloc[:split_idx], X.iloc[split_idx:], y.iloc[:split_idx], y.iloc[split_idx:]


def _make_base_pipe(kind):
    if kind == "rf":
        return Pipeline([("model", RandomForestClassifier(class_weight="balanced_subsample", random_state=42))])
    return Pipeline([("model", _XGBLabelSafeClassifier())])


def _fit_component(kind, X_train, y_train):
    """RandomForestまたはXGBoostを1つ学習する。TimeSeriesSplitで未来データの混入を防ぐ。"""
    param_dist = PARAM_DIST if kind == "rf" else XGB_PARAM_DIST
    fallback_params = RF_FALLBACK_PARAMS if kind == "rf" else XGB_FALLBACK_PARAMS
    n_iter = 10 if kind == "rf" else 8

    cv_splits = min(3, max(2, len(X_train) // 100))
    if cv_splits >= 2 and len(X_train) >= 30:
        try:
            search = RandomizedSearchCV(
                _make_base_pipe(kind), param_dist, n_iter=n_iter,
                cv=TimeSeriesSplit(n_splits=cv_splits),
                # n_jobs=-1（loky/マルチプロセス）はこのサーバー環境でBroken pipeを起こすことを
                # 確認したため、単一プロセスで確実性を優先する（日次バッチなので数秒〜数十秒の
                # 差は問題にならない）。
                scoring=FOG_F1_SCORER, random_state=42, n_jobs=1, error_score="raise",
            )
            search.fit(X_train, y_train)
            return search.best_estimator_, search.best_params_, True
        except Exception as e:
            log.warning("Hyperparameter search failed (%s), using fallback params: %s", kind, e)
    pipe = _make_base_pipe(kind).set_params(**fallback_params)
    pipe.fit(X_train, y_train)
    return pipe, dict(fallback_params), False


def _refit_component(kind, params, X_train, y_train):
    pipe = _make_base_pipe(kind).set_params(**params)
    pipe.fit(X_train, y_train)
    return pipe


def _choose_blend_weight(rf, xgb, X_val, y_val):
    best_w, best_score = 1.0, -1.0
    for w in sorted(BLEND_WEIGHTS, reverse=True):
        score = _blend_score(y_val, HybridFogClassifier(rf, xgb, weight=w).predict(X_val))
        if score > best_score:
            best_w, best_score = w, score
    return best_w


def _choose_fog_threshold(pipe, X_val, y_val):
    if len(X_val) == 0:
        return DEFAULT_FOG_THRESHOLD
    y_bin = pd.Series(y_val).isin(SUMMARY_FOG_CODES).astype(int).to_numpy()
    if y_bin.sum() == 0 or y_bin.sum() == len(y_bin):
        return DEFAULT_FOG_THRESHOLD
    probs = compute_fog_probability(pipe, X_val)
    best_t, best_f1 = DEFAULT_FOG_THRESHOLD, -1.0
    for t in FOG_THRESHOLD_GRID:
        f1 = f1_score(y_bin, (probs >= t).astype(int), zero_division=0)
        if f1 > best_f1:
            best_t, best_f1 = float(t), f1
    return best_t


def _fit_and_select(kind, X_train, y_train):
    """学習し、検証期間が確保できれば混合比・霧判定しきい値も選ぶ。
    戻り値: (HybridFogClassifier, tuned:bool, threshold:float, rf_pipe, xgb_pipe)
    """
    X_inner, X_val, y_inner, y_val = _temporal_train_test_split(X_train, y_train)
    have_val = y_inner.nunique() >= 2 and len(X_val) >= 10

    if kind == "hybrid":
        if have_val:
            rf_tmp, rf_params, rf_tuned = _fit_component("rf", X_inner, y_inner)
            xgb_tmp, xgb_params, xgb_tuned = _fit_component("xgb", X_inner, y_inner)
            weight = _choose_blend_weight(rf_tmp, xgb_tmp, X_val, y_val)
            threshold = _choose_fog_threshold(HybridFogClassifier(rf_tmp, xgb_tmp, weight), X_val, y_val)
            rf_pipe = _refit_component("rf", rf_params, X_train, y_train)
            xgb_pipe = _refit_component("xgb", xgb_params, X_train, y_train)
            tuned = rf_tuned and xgb_tuned
        else:
            rf_pipe, _, rf_tuned = _fit_component("rf", X_train, y_train)
            xgb_pipe, _, xgb_tuned = _fit_component("xgb", X_train, y_train)
            weight, tuned, threshold = 0.5, rf_tuned and xgb_tuned, DEFAULT_FOG_THRESHOLD
    else:
        single_kind = kind
        weight = 1.0 if single_kind == "rf" else 0.0
        rf_pipe = xgb_pipe = None
        if have_val:
            tmp, params, s_tuned = _fit_component(single_kind, X_inner, y_inner)
            tmp_pipe = (HybridFogClassifier(tmp, None, weight=1.0) if single_kind == "rf"
                       else HybridFogClassifier(None, tmp, weight=0.0))
            threshold = _choose_fog_threshold(tmp_pipe, X_val, y_val)
            final_pipe = _refit_component(single_kind, params, X_train, y_train)
            tuned = s_tuned
        else:
            final_pipe, _, tuned = _fit_component(single_kind, X_train, y_train)
            threshold = DEFAULT_FOG_THRESHOLD
        if single_kind == "rf":
            rf_pipe = final_pipe
        else:
            xgb_pipe = final_pipe

    pipe = HybridFogClassifier(rf_pipe, xgb_pipe, weight=weight)
    return pipe, tuned, threshold, rf_pipe, xgb_pipe


def _resolve_model_kind(kind):
    if kind in ("hybrid", "xgb") and not _HAS_XGBOOST:
        log.warning("xgboost not available, falling back to RandomForest only")
        return "rf"
    return kind


def _collect_location_xy(merged, col, feature_names):
    """[col]の欠測に加え、feature_names（地点ごとに値を入れ替えるCROSS_STATION_FOG_FEATURE等を
    含みうる）の欠測行もここで除く。他の共通特徴量は呼び出し側で既に一括除去済みのため、
    ここでの追加除去は主にCROSS_STATION_FOG_FEATURE用。"""
    sub = merged.dropna(subset=[col, *feature_names]).sort_values("datetime").reset_index(drop=True)
    return sub, sub[feature_names], sub[col].astype(int)


def load_low_confidence_set(path=CONFIDENCE_LOG_PATH):
    """collect.pyが記録した確信度ログから、confidence=lowだった(datetime文字列, 地点名)の
    組をsetで返す。学習時にそれらのラベルを欠測扱いして除外するために使う。
    ファイルが無ければ空set（除外なしで従来通り学習する）。
    """
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


def train_location_models(main_df, phenom_df, phenom_cols, lat, lon, model="hybrid"):
    """地点それぞれについて独立した多クラス分類モデルを学習する。データ不足の地点は
    全地点共通の「プーリングモデル」で補う。

    戻り値: {地点名: {"pipe":, "features":, "fog_threshold":, "model_type":, "n":, ...}}
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
            log.info(
                "Excluded %d low-confidence label(s) from training across %d station(s)",
                excluded_total, len(phenom_cols),
            )

    merged = pd.merge(main_df, targets, on="datetime", how="inner")
    merged = merged.sort_values("datetime").reset_index(drop=True)
    cross_rates = _compute_cross_station_fog_rates(merged, phenom_cols)

    merged, feature_names = prepare_features(merged, lat, lon)
    # 短い欠測は時間方向に補間して活かす（視程・降水量が単発で欠けているだけで行ごと
    # 丸ごと捨てるのは勿体ないため）。それでも埋まらない大きな欠測は従来通り除外する。
    merged[feature_names] = merged[feature_names].interpolate(
        method="linear", limit=WEATHER_INTERPOLATE_LIMIT, limit_direction="both",
    )
    merged = merged.dropna(subset=feature_names)
    feature_names = feature_names + [CROSS_STATION_FOG_FEATURE]

    kind = _resolve_model_kind(model)
    log.info("Training location models: kind=%s, features=%s", kind, feature_names)

    models = {}
    for col in phenom_cols:
        merged[CROSS_STATION_FOG_FEATURE] = cross_rates[col].reindex(merged.index).to_numpy()
        sub, X, y = _collect_location_xy(merged, col, feature_names)
        if len(sub) < MIN_ROWS_PER_LOCATION:
            continue
        if y.nunique() < MIN_CLASSES_PER_LOCATION:
            continue

        X_train, X_test, y_train, y_test = _temporal_train_test_split(X, y)
        if y_train.nunique() < 2:
            continue

        pipe, tuned, threshold, rf_pipe, xgb_pipe = _fit_and_select(kind, X_train, y_train)
        y_pred = pipe.predict(X_test)
        acc = accuracy_score(y_test, y_pred)
        f1 = f1_score(y_test, y_pred, average="weighted", zero_division=0)
        fog_f1 = _fog_class_f1(y_test, y_pred)
        rf_only = _single_component_metrics(rf_pipe, True, X_test, y_test)
        xgb_only = _single_component_metrics(xgb_pipe, False, X_test, y_test)
        log.info(
            "%s: n=%d, model=%s, accuracy=%.3f, f1=%.3f, fog_f1=%s, threshold=%.0f%%, tuned=%s",
            col, len(sub), pipe.model_type,
            acc, f1, "n/a" if np.isnan(fog_f1) else f"{fog_f1:.3f}", threshold * 100, tuned,
        )
        models[col] = {
            "pipe": pipe, "accuracy": acc, "f1": f1, "fog_f1": fog_f1,
            "n": len(sub), "features": feature_names,
            "model_type": pipe.model_type, "blend_weight": pipe.weight,
            "fog_threshold": threshold, "pooled": False,
            "y_test": y_test.to_numpy(), "y_pred": np.asarray(y_pred),
            "rf_only": rf_only, "xgb_only": xgb_only,
            "cross_fog_index_last": _last_valid_value(cross_rates[col]),
        }

    n_individual = len(models)
    _rescue_with_pooled_model(models, merged, phenom_cols, feature_names, kind, cross_rates)

    n_pooled = len(models) - n_individual
    n_unmodeled = len(phenom_cols) - len(models)
    log.info(
        "Training summary: %d/%d individual, %d/%d pooled, %d/%d could not be modeled at all",
        n_individual, len(phenom_cols), n_pooled, len(phenom_cols), n_unmodeled, len(phenom_cols),
    )
    return models


def _station_fog_rate(y, global_rate):
    """地点ごとの霧発生率を、サンプル数が少ないほど全地点平均に寄せて平滑化する。"""
    n = len(y)
    if n == 0:
        return global_rate
    k = float(y.isin(SUMMARY_FOG_CODES).sum())
    m = STATION_RATE_SMOOTHING
    return (k + m * global_rate) / (n + m)


def _compute_cross_station_fog_rates(merged, phenom_cols):
    """各地点について、自分を除く他地点のCROSS_STATION_FOG_LAG_HOURS時間前の霧発生率を
    計算する。戻り値: {地点名: pd.Series}（mergedと同じindexのSeries）。
    他地点の実測が無い（欠測ばかり）時刻はNaNになる。
    """
    codes = merged[phenom_cols]
    is_fog = codes.isin(SUMMARY_FOG_CODES)
    valid = codes.notna()
    fog_total = is_fog.sum(axis=1)
    valid_total = valid.sum(axis=1)

    rates = {}
    for col in phenom_cols:
        other_fog = fog_total - is_fog[col].astype(int)
        other_valid = valid_total - valid[col].astype(int)
        same_time_rate = (other_fog / other_valid.replace(0, np.nan)).astype(float)
        tmp = pd.DataFrame({
            "datetime": merged["datetime"].to_numpy(),
            "_rate": same_time_rate.to_numpy(),
        })
        lagged = _lag_series(tmp, "_rate", CROSS_STATION_FOG_LAG_HOURS)
        rates[col] = pd.Series(lagged.to_numpy(), index=merged.index)
    return rates


def _last_valid_value(series, default=0.0):
    s = series.dropna()
    return float(s.iloc[-1]) if len(s) else default


def _rescue_with_pooled_model(models, merged, phenom_cols, feature_names, kind, cross_rates):
    """個別条件を満たさなかった地点を、全地点共通データで学習した「プーリングモデル」で補う。

    気象特徴量は全地点で同一の値（代表地点1つの実況/予報）のため、地点ごとの過去の霧発生率
    (STATION_FOG_RATE_FEATURE)を追加特徴量として与える。これが無いと、同時刻の予測が
    全地点で完全に同一になってしまう（実際に2026-08-26の初回本番実行で発生した）。
    """
    skipped_cols = [c for c in phenom_cols if c not in models]
    if not skipped_cols:
        return

    per_station_xy = {}
    total_k, total_n = 0.0, 0
    for col in phenom_cols:
        merged[CROSS_STATION_FOG_FEATURE] = cross_rates[col].reindex(merged.index).to_numpy()
        sub, X, y = _collect_location_xy(merged, col, feature_names)
        if sub.empty:
            continue
        per_station_xy[col] = (sub, X, y)
        total_k += float(y.isin(SUMMARY_FOG_CODES).sum())
        total_n += len(y)
    if not per_station_xy:
        return
    global_rate = (total_k / total_n) if total_n else 0.0

    pool_frames = []
    station_rates = {}
    for col, (sub, X, y) in per_station_xy.items():
        rate = _station_fog_rate(y, global_rate)
        station_rates[col] = rate
        part = X.copy()
        part[STATION_FOG_RATE_FEATURE] = rate
        part["__target"] = y.to_numpy()
        part["__datetime"] = sub["datetime"].to_numpy()
        pool_frames.append(part)

    pooled_feature_names = feature_names + [STATION_FOG_RATE_FEATURE]
    pooled = pd.concat(pool_frames, ignore_index=True).sort_values("__datetime").reset_index(drop=True)
    if len(pooled) < MIN_TOTAL_ROWS_FOR_POOLING or pooled["__target"].nunique() < 2:
        log.info("Not enough pooled data (%d rows) to rescue %d location(s)", len(pooled), len(skipped_cols))
        return

    Xp, yp = pooled[pooled_feature_names], pooled["__target"]
    Xp_train, Xp_test, yp_train, yp_test = _temporal_train_test_split(Xp, yp)
    if yp_train.nunique() < 2:
        return

    log.info("Training pooled model to rescue %d location(s) (%d total rows)", len(skipped_cols), len(pooled))
    pooled_pipe, pooled_tuned, pooled_threshold, pooled_rf, pooled_xgb = _fit_and_select(kind, Xp_train, yp_train)

    rescued = 0
    for col in skipped_cols:
        if col not in per_station_xy:
            continue
        sub, X, y = per_station_xy[col]
        if len(sub) < MIN_ROWS_FOR_POOLED_RESCUE or y.nunique() < 2:
            continue
        X2 = X.copy()
        X2[STATION_FOG_RATE_FEATURE] = station_rates[col]
        X_train2, X_test2, y_train2, y_test2 = _temporal_train_test_split(X2, y)
        if len(X_test2) == 0:
            continue
        y_pred2 = pooled_pipe.predict(X_test2)
        acc2 = accuracy_score(y_test2, y_pred2)
        f12 = f1_score(y_test2, y_pred2, average="weighted", zero_division=0)
        fog_f12 = _fog_class_f1(y_test2, y_pred2)
        rf_only2 = _single_component_metrics(pooled_rf, True, X_test2, y_test2)
        xgb_only2 = _single_component_metrics(pooled_xgb, False, X_test2, y_test2)
        models[col] = {
            "pipe": pooled_pipe, "accuracy": acc2, "f1": f12, "fog_f1": fog_f12,
            "n": len(sub), "features": feature_names, "station_fog_rate": station_rates[col],
            "model_type": f"pooled({pooled_pipe.model_type})", "blend_weight": pooled_pipe.weight,
            "fog_threshold": pooled_threshold, "pooled": True,
            "y_test": y_test2.to_numpy(), "y_pred": np.asarray(y_pred2),
            "rf_only": rf_only2, "xgb_only": xgb_only2,
            "cross_fog_index_last": _last_valid_value(cross_rates[col]),
        }
        rescued += 1
    log.info("Pooled model rescued %d/%d location(s)", rescued, len(skipped_cols))


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
    df[numeric_cols] = df[numeric_cols].interpolate(limit_direction="both")
    df = df.dropna(subset=[COL_TEMP, COL_HUMIDITY, COL_DEWPOINT, COL_WIND_SPEED]).reset_index(drop=True)
    if df.empty:
        raise ValueError("Open-Meteoから有効な予報値を取得できませんでした。")
    return df


def fetch_forecast_range(lat, lon, forecast_days):
    """Open-Meteoから今日以降の時間別予報を取得する（collect.pyの実況取得と同じ地点・単位設定）。"""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(_FORECAST_VARS.keys()),
        "timezone": "Asia/Tokyo",
        "forecast_days": forecast_days,
        "wind_speed_unit": "ms",
        "precipitation_unit": "mm",
        "temperature_unit": "celsius",
    }
    resp = requests.get(OPEN_METEO_FORECAST_URL, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if "hourly" not in payload:
        raise ValueError(f"Open-Meteoの応答にhourlyが含まれていません: {payload}")
    return _forecast_payload_to_df(payload["hourly"])


def build_location_forecast_codes(models, forecast_raw, lat, lon):
    """予報データに対して地点ごとの現象コードと霧確率を予測する。
    戻り値: (pred_df, prob_df)。どちらもdatetime列 + 地点ごとの列。
    """
    pred_df = pd.DataFrame({"datetime": forecast_raw["datetime"].values})
    prob_df = pd.DataFrame({"datetime": forecast_raw["datetime"].values})
    for col, info in models.items():
        feature_names = info["features"]
        df, _ = prepare_features(forecast_raw, lat, lon, feature_names)
        X = df[feature_names].copy()
        if info.get("pooled") and "station_fog_rate" in info:
            X[STATION_FOG_RATE_FEATURE] = info["station_fog_rate"]
        if CROSS_STATION_FOG_FEATURE in feature_names:
            # 予報期間中は他地点の実測が存在しないため、直近の実測時点での値を
            # 一定値として引き継ぐ近似値を使う（train_location_modelsのコメント参照）。
            X[CROSS_STATION_FOG_FEATURE] = info.get("cross_fog_index_last", 0.0)
        pred_df[col] = info["pipe"].predict(X).astype(int)
        prob_df[col] = compute_fog_probability(info["pipe"], X)
    return pred_df, prob_df


def _effective_score(info):
    """モデル比較用のスコア。霧クラスのF1を優先し、無ければ全体F1を使う
    （ハイパーパラメータ選択時の_blend_scoreと同じ考え方）。"""
    fog_f1 = info.get("fog_f1")
    if fog_f1 is not None and not (isinstance(fog_f1, float) and np.isnan(fog_f1)):
        return float(fog_f1)
    return float(info.get("f1", 0.0))


def load_model_state(path=MODEL_STATE_PATH):
    """前回保存したモデル一式を読み込む。無い/壊れている場合は空辞書（比較なしで続行）。"""
    if not path.exists():
        return {}
    try:
        return joblib.load(path)
    except Exception as e:
        log.warning("Failed to load previous model state (%s), skipping regression check: %s", path, e)
        return {}


def append_metrics_history(models, trained_at, path=METRICS_HISTORY_PATH):
    """地点ごとのaccuracy/F1を履歴CSVに1行ずつ追記する（無ければヘッダーから作成）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=METRICS_HISTORY_FIELDS)
        if is_new:
            writer.writeheader()
        for col, info in models.items():
            fog_f1 = info.get("fog_f1")
            if isinstance(fog_f1, float) and np.isnan(fog_f1):
                fog_f1 = ""
            writer.writerow({
                "trained_at": trained_at,
                "station": col,
                "model_type": info.get("model_type", ""),
                "pooled": info.get("pooled", False),
                "n": info.get("n", ""),
                "accuracy": info.get("accuracy", ""),
                "f1": info.get("f1", ""),
                "fog_f1": fog_f1,
            })


def save_model_state(models, path=MODEL_STATE_PATH):
    """今回採用した（＝ロールバック判定後の）モデル一式を、次回比較用に保存する。
    あわせて地点ごとのaccuracy/F1を履歴CSV（METRICS_HISTORY_PATH）に追記し、
    精度が実際に改善しているかを後から追えるようにする。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    trained_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    state = {col: {**info, "trained_at": trained_at} for col, info in models.items()}
    joblib.dump(state, path)
    append_metrics_history(models, trained_at)


def _score_from_predictions(y_true, y_pred):
    """_effective_scoreと同じ考え方（霧クラスF1優先、無ければ全体F1）を、任意の
    (y_true, y_pred)配列に対して計算する（ブートストラップ再抽出のたびに呼び出す）。"""
    fog_f1 = _fog_class_f1(y_true, y_pred)
    if not (isinstance(fog_f1, float) and np.isnan(fog_f1)):
        return float(fog_f1)
    return float(f1_score(y_true, y_pred, average="weighted", zero_division=0))


def _bootstrap_scores(y_true, y_pred, n_boot, seed):
    """テストデータをn_boot回、復元抽出でリサンプリングし、そのたびのスコアを配列で返す。"""
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    n = len(y_true)
    rng = np.random.default_rng(seed)
    return np.array([
        _score_from_predictions(y_true[idx], y_pred[idx])
        for idx in rng.integers(0, n, size=(n_boot, n))
    ])


def _bootstrap_prob_not_worse(prev_info, new_info, n_boot=REGRESSION_N_BOOTSTRAP):
    """前回・今回それぞれのテストデータを独立にブートストラップし、「今回のスコアが
    前回以上になる」割合を推定して返す（0〜1）。前回の生データ(y_test/y_pred)が
    保存されていない（形式が古い等）場合はNoneを返し、判定不能を表す。
    """
    prev_y, prev_pred = prev_info.get("y_test"), prev_info.get("y_pred")
    new_y, new_pred = new_info.get("y_test"), new_info.get("y_pred")
    if prev_y is None or prev_pred is None or new_y is None or new_pred is None:
        return None
    if len(prev_y) == 0 or len(new_y) == 0:
        return None
    prev_scores = _bootstrap_scores(prev_y, prev_pred, n_boot, seed=1)
    new_scores = _bootstrap_scores(new_y, new_pred, n_boot, seed=2)
    return float(np.mean(new_scores >= prev_scores))


def apply_regression_guard(models, prev_state=None, path=MODEL_STATE_PATH):
    """新しく学習したモデルが前回より統計的に有意に劣化していると判断できる地点があれば、
    その地点だけ前回のモデルへ自動的に差し戻す（models を直接書き換える）。
    戻り値：ロールバックした地点名のリスト。
    """
    if prev_state is None:
        prev_state = load_model_state(path)
    rolled_back = []
    for col, info in models.items():
        prev = prev_state.get(col)
        if prev is None:
            continue
        if info.get("n", 0) < MIN_N_FOR_REGRESSION_CHECK or prev.get("n", 0) < MIN_N_FOR_REGRESSION_CHECK:
            continue
        prob_not_worse = _bootstrap_prob_not_worse(prev, info)
        if prob_not_worse is None:
            continue
        if prob_not_worse < (1.0 - REGRESSION_CONFIDENCE):
            log.warning(
                "Regression detected for %s (new score=%.3f, previous score=%.3f, "
                "bootstrap prob(new>=prev)=%.3f < %.2f); rolling back to previous model",
                col, _effective_score(info), _effective_score(prev),
                prob_not_worse, 1.0 - REGRESSION_CONFIDENCE,
            )
            models[col] = prev
            rolled_back.append(col)
    return rolled_back
