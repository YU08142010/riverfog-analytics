"""地点ごとの川霧予測モデルの学習・予測。

RandomForest・XGBoost・LightGBM単体、およびRF+XGBoost・RF+LightGBM・
RF+XGBoost+LightGBMのブレンドを候補として地点ごとに学習し、検証データでの正答率(accuracy)が最も高い方式を
採用する（地点によって気象条件やデータの蓄積量が異なり、最適なモデル方式も
一様ではないため。以前は全地点でRF+XGBoostブレンド固定だった）。

~/Desktop/fog-prediction/weather_visualizer.py のモデル設計をそのまま踏襲している
（アルゴリズム自体はそちらで検証済みのものを流用し、Colab専用の周辺機能だけを外した）。
"""
import csv
import json
import logging
import shutil
import time
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
import requests
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.ensemble import RandomForestClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, make_scorer
from sklearn.model_selection import RandomizedSearchCV, TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.utils.class_weight import compute_sample_weight

from common import BASE_DIR, JST, request_with_dns_fallback
import station_weather
from fog_advection import ADVECTION_FOG_FEATURE, compute_advection_fog_index
import fog_tree_viz

try:
    from xgboost import XGBClassifier
    _HAS_XGBOOST = True
except Exception:
    XGBClassifier = None
    _HAS_XGBOOST = False

try:
    from lightgbm import LGBMClassifier
    _HAS_LIGHTGBM = True
except Exception:
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
    PHENOM_CODE_GROUPS,
    SUMMARY_FOG_CODES,
    _lag_series,
    build_location_class_targets,
    build_location_secondary_targets,
    prepare_features,
    select_feature_names,
)

log = logging.getLogger("fog_model")

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
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

# 地点ごとにモデルを学習するための最低条件（満たさない地点は共通モデルで補う）。
# いずれも実験的に精密検証された値ではなく、初期運用時の経験則(サンプル数が少なすぎる
# 状態で個別学習すると明らかに不安定だった)から暫定的に決めた値。地点数・データ蓄積量が
# 増えてきたら、実際の学習ログ(精度履歴CSV)を見ながら見直すことを想定している。
MIN_ROWS_PER_LOCATION = 200
MIN_CLASSES_PER_LOCATION = 2
MIN_TOTAL_ROWS_FOR_POOLING = 500
MIN_ROWS_FOR_POOLED_RESCUE = 30
# 追加されたばかりの地点(会津地方など)は、まだMIN_ROWS_FOR_POOLED_RESCUEに満たない・
# 川霧の実例が1件も無い(1クラスのみ)ことがほとんどで、従来のゲートのままだとプーリング
# モデルにすら乗せられず「その地点だけモデルが存在しない」状態になってしまう。
# 地点別川霧発生率(STATION_FOG_RATE_FEATURE)は少数サンプルでも全体平均に平滑化される設計
# のため、ごく少数の実測さえあればプーリングモデルへの「配置」自体は問題ない。以下の値を
# 満たせば、ホールドアウト評価(精度算出)はできなくても実際の予測には使えるようにする。
MIN_ROWS_FOR_POOLED_DEPLOY = 3

FOG_THRESHOLD_GRID = tuple(round(t, 2) for t in np.arange(0.05, 1.0, 0.05))
DEFAULT_FOG_THRESHOLD = 0.5

# 川霧確率の較正(isotonic regression)に使う、個別学習した地点のホールドアウトを
# プールしたサンプル数の下限。RF/XGBoost/LightGBMのブレンド確率はSVMのような
# sigmoid型ではなく非単調に歪みやすく、しきい値ごとのprecision/recallのブレの
# 一因になっているため事後補正するが、isotonicはノンパラメトリックな分サンプルが
# 少ないと過学習しやすい。足りない場合は較正せず生の確率をそのまま使う。
MIN_SAMPLES_FOR_FOG_CALIBRATION = 300

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
LGBM_PARAM_DIST = {
    "model__n_estimators": [200, 400],
    "model__max_depth": [-1, 4, 6, 8],
    "model__learning_rate": [0.05, 0.1, 0.2],
    "model__num_leaves": [15, 31, 63],
    "model__subsample": [0.8, 1.0],
    "model__colsample_bytree": [0.8, 1.0],
}
RF_FALLBACK_PARAMS = dict(model__n_estimators=500, model__max_depth=14, model__min_samples_leaf=2)
XGB_FALLBACK_PARAMS = dict(model__n_estimators=300, model__max_depth=4, model__learning_rate=0.1)
LGBM_FALLBACK_PARAMS = dict(model__n_estimators=300, model__max_depth=-1, model__learning_rate=0.1)
BLEND_WEIGHTS = [0.0, 0.25, 0.5, 0.75, 1.0]

# プーリングモデル専用の追加特徴量：地点ごとの過去の川霧発生率（他の特徴量は全地点で同一の
# 気象データのため、これが無いと同時刻の予測が全地点で完全に一致してしまう）。
# サンプル数が少ない地点で経験値がそのまま乗ると不安定なので、全地点平均へ向けてベイズ平滑化する。
STATION_FOG_RATE_FEATURE = "地点別川霧発生率"
STATION_RATE_SMOOTHING = 20

# 地点間の空間相関を表す特徴量：自地点を除く他地点の(CROSS_STATION_FOG_LAG_HOURS)時間前の
# 川霧発生率。川霧は隣接区間で連動しやすいという想定に基づく。1時間前の値を使うのは、
# 同時刻の値をそのまま使うと「今まさに川霧かどうか」という答えの情報が特徴量に漏れてしまう
# ため（他地点の現在値は、自地点の現在の判定と強く相関するが、それは推論時には使えない
# 未来情報に等しい）。
# 予報（未来）には他地点の実測が存在しないため、学習時のみ時間変化する値を使い、予報時は
# 直近の実測時点での値を一定値として引き継ぐ近似とする（視程等と同様、予報期間が長くなる
# ほど厳密ではなくなるが、無いよりは直近の広域的な傾向を反映できる）。
CROSS_STATION_FOG_LAG_HOURS = 1
CROSS_STATION_FOG_FEATURE = "広域川霧指数"

# 上のCROSS_STATION_FOG_FEATUREは地理・風向を一切見ない単純平均のため、「どちらから
# どちらへ流れているか」を表せない。fog_advection.pyの移流川霧指数(ADVECTION_FOG_FEATURE)は
# 実測座標(cameras.csv)と実測風向風速から風上側の地点を推定し、その延長として追加した
# 特徴量。互いに排他ではなく両方を特徴量として与え、どちらがどれだけ効くかはモデルの
# 学習(および回帰検知の自動ロールバック)に委ねる。

# 短い欠測（視程・降水量が単発で欠けている等）は行ごと捨てず時間方向に補間して活かす。
# これを超える欠測はこれまで通り除外する。
WEATHER_INTERPOLATE_LIMIT = 3

# Geminiの分類confidenceのログ。collect.pyが分類のたびに追記し、ここでは
# confidence:lowだったラベルを学習から除外するために読む。
CONFIDENCE_LOG_PATH = BASE_DIR / "data" / "models" / "confidence_log.csv"

# backfill_weather.pyが後から推定で埋めた(実測ではない)日時のログ。ここでは
# その日時の気象特徴量を学習から除外するために読む(confidence_log.csvと同じ考え方)。
BACKFILL_LOG_PATH = BASE_DIR / "data" / "models" / "backfill_log.csv"

# モデル退行防止：前回学習時点よりスコア（川霧F1、無ければ全体F1）が悪化した地点を自動検出する。
# 固定の許容誤差ではなく、各回のテストデータをブートストラップ再抽出して「今回のスコアが
# 前回以上になる確率」を推定し、それがREGRESSION_CONFIDENCE分の1未満（＝ノイズでは説明が
# つかないほど明確に悪化）と判断できた地点だけ、自動的に前回のモデルへロールバックする。
# サンプル数が少なすぎる、または前回の生データ(y_test/y_pred)が保存されていない（形式が古い等）
# 場合は、判定できないため比較自体を行わない。
MODEL_STATE_PATH = BASE_DIR / "data" / "models" / "model_state.joblib"
# 川霧確率の較正モデル(IsotonicRegression)。modelsと形が異なる(地点ごとの辞書ではなく
# 単一のオブジェクト)ため、model_state.joblibとは別ファイルに保存する。
FOG_CALIBRATOR_PATH = BASE_DIR / "data" / "models" / "fog_calibrator.joblib"
# model_state.joblibは常に最新状態で上書きされ過去の履歴が残らないため、精度が実際に
# 改善しているかを後で振り返れるよう、学習のたびに地点ごとのaccuracy/F1をここに追記する。
METRICS_HISTORY_PATH = BASE_DIR / "data" / "models" / "model_metrics_history.csv"
METRICS_HISTORY_FIELDS = ["trained_at", "station", "model_type", "pooled", "n", "accuracy", "f1", "fog_f1"]
REGRESSION_CONFIDENCE = 0.95
REGRESSION_N_BOOTSTRAP = 500
MIN_N_FOR_REGRESSION_CHECK = 30
# ブートストラップによる統計的判定に足るサンプルが無い(新規地点・プーリング救済で
# 評価split自体が組めない場合を含む)地点は完全に回帰保護の対象外になっていた
# (2026-09-13の監査で指摘)。統計的な有意差検定はできなくても、スコアがこれだけ
# 絶対値で落ちていれば「明らかに壊れている」とみなしてロールバックする簡易ガードを
# フォールバックとして使う。
SPARSE_REGRESSION_ABS_DROP = 0.3

# --- 副コード(2コード同時観測)予測: Stage B(2026-09対応) ---------------------------
# 主コード分類器(Stage A、上のRF/XGBoost/LightGBMブレンド)はそのまま無改修で使い続け、
# 2つ目の現象コードだけを別建ての軽量なモデルで予測する。RandomizedSearchCVは使わず
# 固定ハイパーパラメータのRandomForestのみとし、日次の全地点再学習コストをほぼ増やさない
# ようにする(2026-09の監査で、11個の独立分類器を毎日フル学習する案は学習時間が
# 10倍以上に増える懸念があると指摘され、この軽量な代替構成を採用した)。
MIN_DUAL_CODE_POSITIVES = 20  # これ未満の陽性例しかない地点はStage B自体をスキップする
SECONDARY_RETRAIN_INTERVAL_DAYS = 7  # 直近の学習からこの日数未満なら再学習せず引き継ぐ
SECONDARY_MODEL_PARAMS = dict(
    model__n_estimators=200, model__max_depth=8, model__min_samples_leaf=2, model__max_features="sqrt",
)


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


_SECOND_MODEL_LABELS = {"xgb": "XGBoost", "lgbm": "LightGBM"}


def normalize_model_type_label(model_type):
    """モデル退行防止ロールバック(apply_regression_guard)で以前のモデル状態がそのまま
    温存された地点は、コード更新前の旧称"hybrid"(RF+XGBoostブレンドの旧名)がmodel_type
    (延いてはfeature_snapshot等の保存データ)に残ったままになる。表示・保存のたびに
    ここで新しい名称へ読み替える。"""
    if not model_type:
        return model_type
    return model_type.replace("hybrid", "RF+XGBoost")


def _align_proba(model, X, classes):
    """model.predict_proba(X)の列を目的のclasses順に並べ替える（学習データに無かった
    クラスがある場合は該当列を0で埋める）。ブレンド系分類器で共通して使う。"""
    proba = np.asarray(model.predict_proba(X), dtype="float64")
    model_classes = list(np.asarray(model.classes_))
    if model_classes == list(classes):
        return proba
    index = {c: i for i, c in enumerate(classes)}
    out = np.zeros((proba.shape[0], len(classes)), dtype="float64")
    for j, c in enumerate(model_classes):
        if c in index:
            out[:, index[c]] = proba[:, j]
    return out


class HybridFogClassifier:
    """確率 = weight * RandomForest + (1 - weight) * 第2モデル。weight・第2モデルの
    種類(XGBoostかLightGBMか)は地点ごとに、実測の正答率に基づいて自動決定する。"""

    def __init__(self, rf=None, xgb=None, weight=1.0, second_kind="xgb"):
        if rf is None and xgb is None:
            raise ValueError("rf と xgb の少なくとも一方が必要です")
        if rf is None:
            weight = 0.0
        elif xgb is None:
            weight = 1.0
        self.rf = rf
        self.xgb = xgb
        self.weight = float(weight)
        self.second_kind = second_kind
        self.classes_ = np.asarray((rf if rf is not None else xgb).classes_)

    @property
    def model_type(self):
        # second_kind属性を持たない旧pickle(joblib保存済みの過去のモデル状態)との
        # 互換のためgetattrで既定値"xgb"にフォールバックする。
        second_label = _SECOND_MODEL_LABELS.get(getattr(self, "second_kind", "xgb"), "XGBoost")
        if self.rf is None:
            return second_label
        if self.xgb is None:
            return "RF"
        return f"RF+{second_label}"

    def predict_proba(self, X):
        if self.xgb is None:
            return _align_proba(self.rf, X, self.classes_)
        if self.rf is None:
            return _align_proba(self.xgb, X, self.classes_)
        return (self.weight * _align_proba(self.rf, X, self.classes_)
                + (1.0 - self.weight) * _align_proba(self.xgb, X, self.classes_))

    def predict(self, X):
        return self.classes_[np.argmax(self.predict_proba(X), axis=1)]


class TripleBlendFogClassifier:
    """確率 = w_rf*RandomForest + w_xgb*XGBoost + w_lgbm*LightGBM（重みの合計は1）。
    3方式とも学習できた地点でのみ候補になる（fog_model._fit_best_model参照）。"""

    model_type = "RF+XGBoost+LightGBM"

    def __init__(self, rf, xgb, lgbm, w_rf, w_xgb, w_lgbm):
        self.rf, self.xgb, self.lgbm = rf, xgb, lgbm
        self.w_rf, self.w_xgb, self.w_lgbm = float(w_rf), float(w_xgb), float(w_lgbm)
        self.weight = self.w_rf  # 他方式のpipeと同様にpipe.weightで参照できるようにする(RF比率)
        self.classes_ = np.asarray(rf.classes_)

    def predict_proba(self, X):
        return (self.w_rf * _align_proba(self.rf, X, self.classes_)
                + self.w_xgb * _align_proba(self.xgb, X, self.classes_)
                + self.w_lgbm * _align_proba(self.lgbm, X, self.classes_))

    def predict(self, X):
        return self.classes_[np.argmax(self.predict_proba(X), axis=1)]


TRIPLE_BLEND_STEPS = 4  # 0, 0.25, 0.5, 0.75, 1.0刻みの重心座標グリッド(3方式合計15通り)


def _choose_triple_blend_weights(rf, xgb, lgbm, X_val, y_val):
    """RF・XGBoost・LightGBMの3方式ブレンドの重み(w_rf, w_xgb, w_lgbm)を、検証データでの
    _blend_score(川霧F1優先)が最も高くなる組み合わせから選ぶ。"""
    n = TRIPLE_BLEND_STEPS
    best_w, best_score = (1.0 / 3, 1.0 / 3, 1.0 / 3), -1.0
    for i in range(n + 1):
        for j in range(n + 1 - i):
            k = n - i - j
            w = (i / n, j / n, k / n)
            score = _blend_score(y_val, TripleBlendFogClassifier(rf, xgb, lgbm, *w).predict(X_val))
            if score > best_score:
                best_w, best_score = w, score
    return best_w


def compute_fog_probability(pipe, X, calibrator=None):
    """川霧（SUMMARY_FOG_CODES、コード1〜3）である確率（predict_probaの該当クラス列の合計）。
    calibratorに_fit_fog_calibratorで学習したIsotonicRegressionを渡すと、その較正を
    通した確率を返す(木ベースアンサンブルの生の確率は非単調に歪みやすいため)。
    Noneの場合(較正モデルが無い/学習を打ち切った場合)は生の確率をそのまま返す。"""
    proba = pipe.predict_proba(X)
    fog_idx = [i for i, c in enumerate(pipe.classes_) if c in SUMMARY_FOG_CODES]
    raw = np.zeros(len(X)) if not fog_idx else proba[:, fog_idx].sum(axis=1)
    return apply_fog_calibration(calibrator, raw)


def apply_fog_calibration(calibrator, probs):
    """calibratorがあれば較正後の確率を、無ければそのまま返す。"""
    probs = np.asarray(probs, dtype="float64")
    if calibrator is None:
        return probs
    return np.clip(calibrator.predict(probs), 0.0, 1.0)


def extract_representative_tree(pipe, feature_names):
    """採用中のモデル(HybridFogClassifier/TripleBlendFogClassifier、または単一方式)が
    実際に使っている決定木の構造を、可視化用の共通ノード形式(fog_tree_viz参照)で取り出す。
    ブレンドモデルは複数方式を組み合わせているため、実際に使われている(Noneでない)
    コンポーネントそれぞれについて、その中の代表として1本目の木を取り出す(RF/XGBoost/
    LightGBMいずれも多数の木の平均・合計で最終予測を出す方式のため、1本だけでは全体の
    性格を表しきれない点に注意。木の深さ・複雑さの雰囲気をつかむための図であり、
    予測ロジックそのものの正確な再現ではない)。

    戻り値: {"RF": {...}, "XGBoost": {...}, "LightGBM": {...}}(実際に使われている方式のみ)。
    各値は {"root": ノード, "actual_max_depth": int, "configured_max_depth": int/None,
    "n_estimators": int}。
    """
    result = {}
    rf_pipe = getattr(pipe, "rf", None)
    xgb_pipe = getattr(pipe, "xgb", None)
    lgbm_pipe = getattr(pipe, "lgbm", None)

    if rf_pipe is not None:
        rf_model = rf_pipe.named_steps["model"]
        tree0 = rf_model.estimators_[0].tree_
        root = fog_tree_viz.sklearn_tree_to_nodes(tree0, feature_names)
        result["RF"] = {
            "root": root,
            "actual_max_depth": fog_tree_viz.tree_actual_max_depth(root),
            "configured_max_depth": rf_model.max_depth,
            "n_estimators": rf_model.n_estimators,
        }
    if xgb_pipe is not None:
        try:
            xgb_model = xgb_pipe.named_steps["model"].model_
            booster = xgb_model.get_booster()
            dump = booster.get_dump(dump_format="json")
            tree0 = json.loads(dump[0])
            root = fog_tree_viz.xgb_tree_to_nodes(tree0, feature_names)
            result["XGBoost"] = {
                "root": root,
                "actual_max_depth": fog_tree_viz.tree_actual_max_depth(root),
                "configured_max_depth": xgb_model.max_depth,
                "n_estimators": len(dump),
            }
        except Exception as e:
            log.warning("Failed to extract XGBoost tree for visualization: %s", e)
    if lgbm_pipe is not None:
        try:
            lgbm_model = lgbm_pipe.named_steps["model"]
            model_dump = lgbm_model.booster_.dump_model()
            tree_structure = model_dump["tree_info"][0]["tree_structure"]
            root = fog_tree_viz.lgbm_tree_to_nodes(tree_structure, feature_names)
            result["LightGBM"] = {
                "root": root,
                "actual_max_depth": fog_tree_viz.tree_actual_max_depth(root),
                "configured_max_depth": lgbm_model.max_depth if lgbm_model.max_depth != -1 else None,
                "n_estimators": len(model_dump["tree_info"]),
            }
        except Exception as e:
            log.warning("Failed to extract LightGBM tree for visualization: %s", e)
    return result


def _fit_fog_calibrator(probs, labels):
    """個々の地点モデルのホールドアウト(検証用テストデータ)をプールし、川霧確率の較正モデル
    (isotonic regression)を学習する。プーリング救済した地点は評価split自体が信頼できない
    ことがある(can_evaluate=False)ため含めず、個別学習できた地点の分だけを使う。
    プールしたサンプル数がMIN_SAMPLES_FOR_FOG_CALIBRATION未満、またはラベルが1種類しか
    無い場合は較正を行わずNoneを返す(呼び出し側は生の確率をそのまま使う)。"""
    probs = np.asarray(probs, dtype="float64")
    labels = np.asarray(labels, dtype="int64")
    if len(probs) < MIN_SAMPLES_FOR_FOG_CALIBRATION or len(np.unique(labels)) < 2:
        log.info("Not enough pooled holdout data (%d rows) to fit a fog probability calibrator", len(probs))
        return None
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    calibrator.fit(probs, labels)
    log.info("Fitted fog probability calibrator on %d pooled holdout row(s)", len(probs))
    return calibrator


def save_fog_calibrator(calibrator, path=FOG_CALIBRATOR_PATH):
    """較正モデルを保存する。Noneの場合(サンプル不足等で較正を行わない場合)は既存の
    ファイルを削除し、次回load_fog_calibrator()がNoneを返す(=生の確率にフォールバック)
    ようにする。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if calibrator is None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    joblib.dump(calibrator, tmp_path)
    tmp_path.replace(path)


def load_fog_calibrator(path=FOG_CALIBRATOR_PATH):
    """保存済みの較正モデルを読み込む。無い/壊れている場合はNone(生の確率にフォールバック)。"""
    if not path.exists():
        return None
    try:
        return joblib.load(path)
    except Exception as e:
        log.warning("Failed to load fog probability calibrator (%s), using raw probabilities: %s", path, e)
        return None


def _estimator_feature_importances(estimator):
    """RandomForest/LightGBMはfeature_importances_をそのまま持つが、XGBoostは
    歯抜けラベル対応のため_XGBLabelSafeClassifierでラップしており、実体は
    .model_.feature_importances_にある。両対応にする。"""
    if estimator is None:
        return None
    inner = getattr(estimator, "model_", None)
    if inner is not None and hasattr(inner, "feature_importances_"):
        return inner.feature_importances_
    return getattr(estimator, "feature_importances_", None)


def compute_feature_importance(state, only_stations=None):
    """学習済みモデル一式(model_state.joblibをload_model_stateしたもの)から、
    特徴量ごとの重要度を計算する。RandomForestと第2モデル(XGBoostまたはLightGBM)の
    feature_importances_をブレンド比(blend_weight)で重み付けし、複数地点で同一のpipeを
    共有している場合（プーリングモデル）は1回だけ数える。
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

        is_triple = hasattr(pipe, "lgbm")  # TripleBlendFogClassifierだけがlgbm属性を持つ
        rf_imp = xgb_imp = lgbm_imp = None
        try:
            if pipe.rf is not None:
                rf_imp = _estimator_feature_importances(pipe.rf.named_steps["model"])
        except Exception:
            rf_imp = None
        try:
            if getattr(pipe, "xgb", None) is not None:
                xgb_imp = _estimator_feature_importances(pipe.xgb.named_steps["model"])
        except Exception:
            xgb_imp = None
        if is_triple:
            try:
                lgbm_imp = _estimator_feature_importances(pipe.lgbm.named_steps["model"])
            except Exception:
                lgbm_imp = None

        parts = []
        if is_triple:
            if rf_imp is not None and len(rf_imp) == len(features):
                parts.append(pipe.w_rf * rf_imp)
            if xgb_imp is not None and len(xgb_imp) == len(features):
                parts.append(pipe.w_xgb * xgb_imp)
            if lgbm_imp is not None and len(lgbm_imp) == len(features):
                parts.append(pipe.w_lgbm * lgbm_imp)
        elif rf_imp is not None and xgb_imp is not None \
                and len(rf_imp) == len(features) and len(xgb_imp) == len(features):
            parts = [pipe.weight * rf_imp, (1.0 - pipe.weight) * xgb_imp]
        elif rf_imp is not None and len(rf_imp) == len(features):
            parts = [rf_imp]
        elif xgb_imp is not None and len(xgb_imp) == len(features):
            parts = [xgb_imp]
        if not parts:
            continue
        importances = sum(parts)

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
    """RandomForest単体・XGBoost単体それぞれで予測した場合のaccuracy・川霧F1を返す
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


def _evaluate_solo_pipe(pipe, X_test, y_test):
    """任意の学習済みモデル単体(ここではLightGBM)で予測した場合のaccuracy・川霧F1を返す。
    RF/XGBの_single_component_metrics()はHybridFogClassifier経由で評価するが、
    LightGBMは採用中のhybridブレンド(RF+XGB)には含めていない独立した比較用モデルのため、
    直接.predict()を呼ぶだけで同じ指標を計算できる（単に横並び比較を管理画面で見せるための
    診断用で、モデル選択自体には使わない）。
    """
    if pipe is None or len(X_test) == 0:
        return None
    y_pred = pipe.predict(X_test)
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
    if kind == "lgbm":
        return Pipeline([("model", LGBMClassifier(class_weight="balanced", random_state=42, verbosity=-1, n_jobs=1))])
    return Pipeline([("model", _XGBLabelSafeClassifier())])


def _fit_component(kind, X_train, y_train):
    """RandomForest・XGBoost・LightGBMのいずれか1つを学習する。
    TimeSeriesSplitで未来データの混入を防ぐ。
    """
    param_dist = {"rf": PARAM_DIST, "xgb": XGB_PARAM_DIST, "lgbm": LGBM_PARAM_DIST}[kind]
    fallback_params = {"rf": RF_FALLBACK_PARAMS, "xgb": XGB_FALLBACK_PARAMS, "lgbm": LGBM_FALLBACK_PARAMS}[kind]
    n_iter = 10 if kind == "rf" else 8

    cv_splits = min(3, max(2, len(X_train) // 100))
    if cv_splits >= 2 and len(X_train) >= 30:
        try:
            search = RandomizedSearchCV(
                _make_base_pipe(kind), param_dist, n_iter=n_iter,
                cv=TimeSeriesSplit(n_splits=cv_splits),
                # n_jobs=-1をjoblibの既定のloky(マルチプロセス)バックエンドと組み合わせると
                # このサーバー環境でBroken pipeを起こすことを確認したため、単一プロセスに
                # していた。ただし日次バッチが実測1.5時間以上かかるようになっており
                # (2026-09-13の監査で「数秒〜数十秒の差」という前提が既に古いと指摘された)、
                # 並列化自体は諦めずthreadingバックエンドに切り替える。RF/XGBoost/LightGBMは
                # 学習の大部分がC/C++実装でGILを解放するため、プロセス間通信(壊れうるpipe)を
                # 使わないthreadingでも実効的に並列化でき、Broken pipeの再発リスクを避けられる。
                scoring=FOG_F1_SCORER, random_state=42, n_jobs=-1, error_score="raise",
            )
            with joblib.parallel_backend("threading"):
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


def _phenom_group_for_code(code):
    for group in PHENOM_CODE_GROUPS:
        if code in group:
            return group
    return None


def _stage_a_extra_features(X, primary_pipe):
    """Stage B(副コード分類器)に、Stage A(主コード分類器)自身の予測確率を追加特徴量として
    与える(最大確率・1位2位差)。Stage Aが不確実なときほど2つ目の現象が同時に写っている
    可能性が高いという想定の再利用で、Stage B専用に新たな重い学習をしなくて済む。"""
    proba = np.asarray(primary_pipe.predict_proba(X))
    sorted_proba = np.sort(proba, axis=1)
    top1 = sorted_proba[:, -1]
    margin = top1 - sorted_proba[:, -2] if proba.shape[1] >= 2 else np.zeros(len(X))
    extra = X.copy()
    extra["__stage_a_top1_prob"] = top1
    extra["__stage_a_margin"] = margin
    return extra


def _fit_or_reuse_secondary_model(X_train, y_train, y2_train, X_test, y2_test, primary_pipe, prev_secondary):
    """副コード(2コード同時観測の2つ目)を予測する軽量な2段構成を学習する。
    - presence_pipe: 「2つ目のコードがあるか」の二値分類器
    - identity_pipe: 「2つ目は具体的にどのコードか」の多クラス分類器(2コード同時だった
      行のみで学習)
    どちらもRandomizedSearchCVは使わずSECONDARY_MODEL_PARAMS固定のRandomForestのみ。
    直近の学習(prev_secondary)からSECONDARY_RETRAIN_INTERVAL_DAYS日未満ならそのまま
    引き継ぎ、陽性例がMIN_DUAL_CODE_POSITIVES未満の地点はスキップする。
    """
    now = datetime.now(JST)
    if prev_secondary and prev_secondary.get("trained_at"):
        try:
            prev_trained_at = datetime.fromisoformat(prev_secondary["trained_at"])
            if prev_trained_at.tzinfo is None:
                prev_trained_at = prev_trained_at.replace(tzinfo=JST)
            if (now - prev_trained_at).days < SECONDARY_RETRAIN_INTERVAL_DAYS:
                return prev_secondary
        except (TypeError, ValueError):
            pass

    skipped = {
        "presence_pipe": None, "identity_pipe": None, "identity_classes_": None,
        "presence_threshold": DEFAULT_FOG_THRESHOLD, "n_dual": int(y2_train.notna().sum()),
        "trained_at": now.isoformat(), "skipped_reason": "insufficient_dual_samples",
    }
    if skipped["n_dual"] < MIN_DUAL_CODE_POSITIVES:
        return skipped

    presence_y_train = y2_train.notna().astype(int)
    if presence_y_train.nunique() < 2:
        return skipped

    X_train_ext = _stage_a_extra_features(X_train, primary_pipe)
    presence_pipe = _make_base_pipe("rf").set_params(**SECONDARY_MODEL_PARAMS)
    presence_pipe.fit(X_train_ext, presence_y_train)

    dual_mask_train = y2_train.notna()
    identity_pipe = None
    identity_classes_ = None
    if dual_mask_train.sum() >= 2 and y2_train[dual_mask_train].nunique() >= 2:
        identity_pipe = _make_base_pipe("rf").set_params(**SECONDARY_MODEL_PARAMS)
        identity_pipe.fit(X_train_ext[dual_mask_train], y2_train[dual_mask_train].astype(int))
        identity_classes_ = identity_pipe.named_steps["model"].classes_.tolist()
    else:
        return skipped

    presence_threshold = DEFAULT_FOG_THRESHOLD
    if len(X_test) > 0:
        y2_test_bin = y2_test.notna().astype(int).to_numpy()
        if 0 < y2_test_bin.sum() < len(y2_test_bin):
            probs = presence_pipe.predict_proba(_stage_a_extra_features(X_test, primary_pipe))[:, 1]
            best_t, best_f1 = DEFAULT_FOG_THRESHOLD, -1.0
            for t in FOG_THRESHOLD_GRID:
                f1 = f1_score(y2_test_bin, (probs >= t).astype(int), zero_division=0)
                if f1 > best_f1:
                    best_t, best_f1 = float(t), f1
            presence_threshold = best_t

    return {
        "presence_pipe": presence_pipe, "identity_pipe": identity_pipe,
        "identity_classes_": identity_classes_, "presence_threshold": presence_threshold,
        "n_dual": skipped["n_dual"], "trained_at": now.isoformat(), "skipped_reason": None,
    }


def predict_secondary_codes(info, X, primary_pred):
    """副コード(存在すれば)とその出現確率を予測する。主コードと同じ家族グループ、
    および0(現象なし)は候補から除外してから最有力の候補を選ぶ(PHENOM_CODE_GROUPS参照)。
    Stage Bが未学習(スキップ状態)の地点は全行None/0.0を返す。
    戻り値: (secondary_codes: np.ndarray[object](Noneまたはint), secondary_probs: np.ndarray[float])
    """
    secondary = info.get("secondary") or {}
    presence_pipe = secondary.get("presence_pipe")
    identity_pipe = secondary.get("identity_pipe")
    n = len(X)
    if presence_pipe is None or identity_pipe is None:
        return np.array([None] * n, dtype=object), np.zeros(n)

    primary_pipe = info["pipe"]
    X_ext = _stage_a_extra_features(X, primary_pipe)
    presence_prob = np.asarray(presence_pipe.predict_proba(X_ext))[:, 1]
    threshold = secondary.get("presence_threshold", DEFAULT_FOG_THRESHOLD)
    has_secondary = presence_prob >= threshold

    identity_proba = np.asarray(identity_pipe.predict_proba(X_ext))
    identity_classes = np.asarray(secondary.get("identity_classes_") or identity_pipe.named_steps["model"].classes_)

    secondary_codes = np.array([None] * n, dtype=object)
    primary_arr = np.asarray(primary_pred)
    for i in range(n):
        if not has_secondary[i]:
            continue
        primary_group = _phenom_group_for_code(int(primary_arr[i]))
        for idx in np.argsort(-identity_proba[i]):
            cand = int(identity_classes[idx])
            if cand == 0 or _phenom_group_for_code(cand) == primary_group:
                continue
            secondary_codes[i] = cand
            break
    return secondary_codes, presence_prob


def _fit_best_model(X_train, y_train):
    """RandomForest単体・XGBoost単体・LightGBM単体・RF+XGBoost・RF+LightGBM・
    RF+XGBoost+LightGBMを候補として学習し、検証データでの正答率(accuracy)が
    最も高い方式を採用する。採用方式が決まったら、その構成要素をX_train全体で
    再学習して本番用パイプを作る。

    戻り値: dict
      pipe: 採用したモデル（HybridFogClassifierまたはTripleBlendFogClassifier。本番予測用）
      tuned: ハイパーパラメータ探索が実行できたか
      threshold: 川霧判定しきい値
      rf_pipe, xgb_pipe, lgbm_pipe: X_train全体で再学習した各コンポーネント単体
          （利用不可なライブラリの分はNone。診断用の性能比較に使う）
      hybrid_weight: RF+XGBoostの比較用ブレンド重み（xgboost無ければNone）
      hybrid_lgbm_weight: RF+LightGBMの比較用ブレンド重み（lightgbm無ければNone）
      triple_weights: RF+XGBoost+LightGBMの比較用ブレンド重み(w_rf,w_xgb,w_lgbm)
          （どちらか無ければNone）
    """
    X_inner, X_val, y_inner, y_val = _temporal_train_test_split(X_train, y_train)
    have_val = y_inner.nunique() >= 2 and len(X_val) >= 10

    if not have_val:
        # 検証データを確保できない極小地点は、方式間の比較ができないため
        # 従来通りRF+XGBoost(利用不可ならRF単体)を既定重み0.5で学習する。
        rf_pipe, _, rf_tuned = _fit_component("rf", X_train, y_train)
        xgb_pipe = lgbm_pipe = None
        if _HAS_XGBOOST:
            xgb_pipe, _, xgb_tuned = _fit_component("xgb", X_train, y_train)
            pipe = HybridFogClassifier(rf_pipe, xgb_pipe, weight=0.5, second_kind="xgb")
            tuned = rf_tuned and xgb_tuned
        else:
            pipe = HybridFogClassifier(rf_pipe, None, weight=1.0)
            tuned = rf_tuned
        return {
            "pipe": pipe, "tuned": tuned, "threshold": DEFAULT_FOG_THRESHOLD,
            "rf_pipe": rf_pipe, "xgb_pipe": xgb_pipe, "lgbm_pipe": lgbm_pipe,
            "hybrid_weight": None, "hybrid_lgbm_weight": None, "triple_weights": None,
        }

    rf_tmp, rf_params, rf_tuned = _fit_component("rf", X_inner, y_inner)
    candidates = [(accuracy_score(y_val, HybridFogClassifier(rf_tmp, None, weight=1.0).predict(X_val)),
                   "rf", rf_tuned)]

    xgb_tmp = xgb_params = None
    xgb_tuned = False
    hybrid_weight = None
    if _HAS_XGBOOST:
        xgb_tmp, xgb_params, xgb_tuned = _fit_component("xgb", X_inner, y_inner)
        candidates.append((
            accuracy_score(y_val, HybridFogClassifier(None, xgb_tmp, weight=0.0, second_kind="xgb").predict(X_val)),
            "xgb", xgb_tuned,
        ))
        hybrid_weight = _choose_blend_weight(rf_tmp, xgb_tmp, X_val, y_val)
        candidates.append((
            accuracy_score(y_val, HybridFogClassifier(rf_tmp, xgb_tmp, weight=hybrid_weight, second_kind="xgb").predict(X_val)),
            "hybrid", rf_tuned and xgb_tuned,
        ))

    lgbm_tmp = lgbm_params = None
    lgbm_tuned = False
    hybrid_lgbm_weight = None
    if _HAS_LIGHTGBM:
        lgbm_tmp, lgbm_params, lgbm_tuned = _fit_component("lgbm", X_inner, y_inner)
        candidates.append((
            accuracy_score(y_val, HybridFogClassifier(None, lgbm_tmp, weight=0.0, second_kind="lgbm").predict(X_val)),
            "lgbm", lgbm_tuned,
        ))
        hybrid_lgbm_weight = _choose_blend_weight(rf_tmp, lgbm_tmp, X_val, y_val)
        candidates.append((
            accuracy_score(y_val, HybridFogClassifier(rf_tmp, lgbm_tmp, weight=hybrid_lgbm_weight, second_kind="lgbm").predict(X_val)),
            "hybrid_lgbm", rf_tuned and lgbm_tuned,
        ))

    triple_weights = None
    if xgb_tmp is not None and lgbm_tmp is not None:
        # RF・XGBoost・LightGBMは既に検証用に学習済みなので、追加の学習コスト無しで
        # 3方式ブレンドの重みグリッドを試せる。
        triple_weights = _choose_triple_blend_weights(rf_tmp, xgb_tmp, lgbm_tmp, X_val, y_val)
        candidates.append((
            accuracy_score(y_val, TripleBlendFogClassifier(rf_tmp, xgb_tmp, lgbm_tmp, *triple_weights).predict(X_val)),
            "triple", rf_tuned and xgb_tuned and lgbm_tuned,
        ))

    best_acc, best_kind, best_tuned = max(candidates, key=lambda c: c[0])
    log.debug("Model candidates (val accuracy): %s -> chosen=%s(%.3f)",
              [(k, round(a, 3)) for a, k, _ in candidates], best_kind, best_acc)

    if best_kind == "triple":
        threshold_pipe = TripleBlendFogClassifier(rf_tmp, xgb_tmp, lgbm_tmp, *triple_weights)
    elif best_kind == "rf":
        threshold_pipe = HybridFogClassifier(rf_tmp, None, weight=1.0)
    elif best_kind == "xgb":
        threshold_pipe = HybridFogClassifier(None, xgb_tmp, weight=0.0, second_kind="xgb")
    elif best_kind == "lgbm":
        threshold_pipe = HybridFogClassifier(None, lgbm_tmp, weight=0.0, second_kind="lgbm")
    elif best_kind == "hybrid":
        threshold_pipe = HybridFogClassifier(rf_tmp, xgb_tmp, weight=hybrid_weight, second_kind="xgb")
    else:  # hybrid_lgbm
        threshold_pipe = HybridFogClassifier(rf_tmp, lgbm_tmp, weight=hybrid_lgbm_weight, second_kind="lgbm")
    threshold = _choose_fog_threshold(threshold_pipe, X_val, y_val)

    # 採用方式が決まったら、比較に使った各コンポーネントをX_train全体で再学習する
    # （検証用に分けていたX_innerだけでなく全データを使った方が本番用として有利なため）。
    rf_pipe = _refit_component("rf", rf_params, X_train, y_train)
    xgb_pipe = _refit_component("xgb", xgb_params, X_train, y_train) if xgb_tmp is not None else None
    lgbm_pipe = _refit_component("lgbm", lgbm_params, X_train, y_train) if lgbm_tmp is not None else None

    if best_kind == "triple":
        pipe = TripleBlendFogClassifier(rf_pipe, xgb_pipe, lgbm_pipe, *triple_weights)
    elif best_kind == "rf":
        pipe = HybridFogClassifier(rf_pipe, None, weight=1.0)
    elif best_kind == "xgb":
        pipe = HybridFogClassifier(None, xgb_pipe, weight=0.0, second_kind="xgb")
    elif best_kind == "lgbm":
        pipe = HybridFogClassifier(None, lgbm_pipe, weight=0.0, second_kind="lgbm")
    elif best_kind == "hybrid":
        pipe = HybridFogClassifier(rf_pipe, xgb_pipe, weight=hybrid_weight, second_kind="xgb")
    else:  # hybrid_lgbm
        pipe = HybridFogClassifier(rf_pipe, lgbm_pipe, weight=hybrid_lgbm_weight, second_kind="lgbm")

    return {
        "pipe": pipe, "tuned": best_tuned, "threshold": threshold,
        "rf_pipe": rf_pipe, "xgb_pipe": xgb_pipe, "lgbm_pipe": lgbm_pipe,
        "hybrid_weight": hybrid_weight, "hybrid_lgbm_weight": hybrid_lgbm_weight,
        "triple_weights": triple_weights,
    }


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


def load_backfilled_datetime_set(path=BACKFILL_LOG_PATH):
    """backfill_weather.pyが記録したログから、実測ではなく後から推定で埋めた日時の
    setを返す。気象特徴量は全地点共通(代表値)なので、地点を問わずその日時の行全体を
    学習から除外するために使う。ファイルが無ければ空set。"""
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
    上書きしたコピーを返す（値が無い日時・欠測項目は共通の代表値のまま）。
    記録が1件も無ければNoneを返す（呼び出し側は共通のmain_dfをそのまま使う）。
    """
    station_df = station_weather.load_station_weather_df(weather_conn, station_name)
    if station_df.empty:
        return None
    station_df = station_df.rename(columns=_STATION_WEATHER_COL_MAP)
    out = main_df.set_index("datetime")
    patch = station_df.set_index("datetime")
    out.update(patch)
    return out.reset_index()


def train_location_models(main_df, phenom_df, phenom_cols, lat, lon,
                           camera_coords=None, weather_conn=None, prev_state=None):
    """地点それぞれについて独立した多クラス分類モデルを学習する。データ不足の地点は
    全地点共通の「プーリングモデル」で補う。あわせて、2コード同時観測(2026-09対応)の
    副コードを予測する軽量なStage B(presence_pipe/identity_pipe)も地点ごとに学習・
    引き継ぐ(prev_state参照。Noneならload_model_state()で前回の保存内容を読む)。

    戻り値: {地点名: {"pipe":, "features":, "fog_threshold":, "model_type":, "n":, "secondary":, ...}}
    """
    if prev_state is None:
        prev_state = load_model_state()
    targets = build_location_class_targets(phenom_df, phenom_cols)
    secondary_targets = build_location_secondary_targets(phenom_df, phenom_cols)

    low_confidence = load_low_confidence_set()
    if low_confidence:
        dt_strs = targets["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
        excluded_total = 0
        for col in phenom_cols:
            mask = dt_strs.apply(lambda s, c=col: (s, c) in low_confidence)
            n = int(mask.sum())
            if n:
                targets.loc[mask, col] = np.nan
                secondary_targets.loc[mask, col] = np.nan
                excluded_total += n
        if excluded_total:
            log.info(
                "Excluded %d low-confidence label(s) from training across %d station(s)",
                excluded_total, len(phenom_cols),
            )

    backfilled = load_backfilled_datetime_set()
    if backfilled:
        dt_strs = targets["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
        mask = dt_strs.isin(backfilled)
        n = int(mask.sum())
        if n:
            targets.loc[mask, phenom_cols] = np.nan
            secondary_targets.loc[mask, phenom_cols] = np.nan
            log.info(
                "Excluded %d backfilled (estimated, not observed) datetime row(s) from training "
                "across %d station(s)", n, len(phenom_cols),
            )

    secondary_targets_renamed = secondary_targets.rename(columns={c: f"{c}__secondary" for c in phenom_cols})
    merged = pd.merge(main_df, targets, on="datetime", how="inner")
    merged = pd.merge(merged, secondary_targets_renamed, on="datetime", how="left")
    merged = merged.sort_values("datetime").reset_index(drop=True)
    cross_rates = _compute_cross_station_fog_rates(merged, phenom_cols)

    # 移流川霧指数(風向・風速が本質の特徴量)は、地点固有の実測気象(station_weather.db)が
    # あればそれを使う。無ければcompute_advection_fog_index側で共通の代表地点の風に
    # フォールバックする(2026-09-13の監査で、この特徴量こそ地点固有の精度の恩恵が
    # 大きいはずだと指摘された)。プーリング救済モデルは全地点で共通の気象値を前提に
    # 設計されているため、あえてここでは対象にしない(下のstation_framesループと同じ考え方)。
    station_wind = {}
    if weather_conn is not None and camera_coords:
        for col in phenom_cols:
            if col not in camera_coords:
                continue
            station_main_df = _apply_station_weather(main_df, col, weather_conn)
            if station_main_df is None:
                continue
            aligned = pd.merge(
                merged[["datetime"]],
                station_main_df[["datetime", COL_WIND_DIR_DEG, COL_WIND_SPEED]],
                on="datetime", how="left",
            )
            station_wind[col] = (
                pd.to_numeric(aligned[COL_WIND_DIR_DEG], errors="coerce").to_numpy(dtype="float64"),
                pd.to_numeric(aligned[COL_WIND_SPEED], errors="coerce").to_numpy(dtype="float64"),
            )

    advection_idx = compute_advection_fog_index(
        merged, phenom_cols, camera_coords or {}, COL_WIND_DIR_DEG, COL_WIND_SPEED,
        station_wind=station_wind,
    )

    merged, feature_names, shared_medians = prepare_features(merged, lat, lon)
    # 短い欠測は時間方向に補間して活かす（視程・降水量が単発で欠けているだけで行ごと
    # 丸ごと捨てるのは勿体ないため）。それでも埋まらない大きな欠測は従来通り除外する。
    merged[feature_names] = merged[feature_names].interpolate(
        method="linear", limit=WEATHER_INTERPOLATE_LIMIT, limit_direction="both",
    )
    merged = merged.dropna(subset=feature_names)
    feature_names = feature_names + [CROSS_STATION_FOG_FEATURE, ADVECTION_FOG_FEATURE]

    log.info("Training location models: per-station best-model auto-selection, features=%s", feature_names)

    # 地点固有の気象実測(station_weather.db、2026-08-28以降蓄積分)があれば、個別モデルの
    # 学習だけそれを使う。無い地点・記録がまだ無い日時は、これまで通り共通の代表値
    # (main_df)にフォールバックする。データ不足時の「プーリングモデル」(下のrescue)は
    # 元々「全地点で共通の気象値」を前提に設計されているため、意図的にここでは変えず、
    # 上のmerged/feature_names(共通の代表値ベース)をそのまま使い続ける。
    station_frames = {}
    if weather_conn is not None and camera_coords:
        for col in phenom_cols:
            coords = camera_coords.get(col)
            if not coords:
                continue
            station_main_df = _apply_station_weather(main_df, col, weather_conn)
            if station_main_df is None:
                continue
            s_merged = pd.merge(station_main_df, targets, on="datetime", how="inner")
            s_merged = pd.merge(s_merged, secondary_targets_renamed, on="datetime", how="left")
            s_merged = s_merged.sort_values("datetime").reset_index(drop=True)
            s_lat, s_lon = coords
            s_merged, s_features, s_medians = prepare_features(s_merged, s_lat, s_lon)
            s_merged[s_features] = s_merged[s_features].interpolate(
                method="linear", limit=WEATHER_INTERPOLATE_LIMIT, limit_direction="both",
            )
            s_merged = s_merged.dropna(subset=s_features)
            station_frames[col] = (s_merged, s_features + [CROSS_STATION_FOG_FEATURE], s_medians)
        if station_frames:
            log.info("Using per-station weather for %d/%d station(s)", len(station_frames), len(phenom_cols))

    models = {}
    # 川霧確率較正(isotonic)を学習するための材料。個別学習できた地点のホールドアウト
    # (X_test/y_test)だけをプールする(プーリング救済した地点は評価split自体が
    # 信頼できないことがあるため対象外)。地点ごとに分けて保持するのは、この後
    # apply_regression_guardでロールバックされた地点(今回の候補パイプが破棄され、
    # 前回のパイプがそのまま配信される)を較正モデルの学習データから除外できるように
    # するため(2026-09-13の監査で、ロールバックされる可能性のある「まだ採用が確定して
    # いない」候補の確率を較正学習に混ぜてしまうバグとして指摘された)。実際の較正モデル
    # 学習・しきい値の較正後空間への変換は、この関数の中ではなくfinalize_fog_calibration()
    # で、apply_regression_guardの後にpredict.py側から呼ぶ。
    calib_data_by_station = {}
    for col in phenom_cols:
        col_merged, col_features, col_medians = station_frames.get(col, (merged, feature_names, shared_medians))
        col_merged[CROSS_STATION_FOG_FEATURE] = cross_rates[col].reindex(col_merged.index).to_numpy()
        # 風が弱く移流方向の推定自体が信頼できない時間帯はNaNになるため(_compute_cross_station_fog_rates
        # と異なりCROSS_STATION_FOG_FEATUREよりずっと高頻度に発生する)、_collect_location_xyのdropnaで
        # 行ごと大量に失われないよう「追加の上流リスク信号なし」として0.0で埋める。
        col_merged[ADVECTION_FOG_FEATURE] = advection_idx[col].reindex(col_merged.index).fillna(0.0).to_numpy()
        sub, X, y = _collect_location_xy(col_merged, col, col_features)
        if len(sub) < MIN_ROWS_PER_LOCATION:
            continue
        if y.nunique() < MIN_CLASSES_PER_LOCATION:
            continue

        X_train, X_test, y_train, y_test = _temporal_train_test_split(X, y)
        if y_train.nunique() < 2:
            continue

        result = _fit_best_model(X_train, y_train)
        pipe, tuned, threshold = result["pipe"], result["tuned"], result["threshold"]
        rf_pipe, xgb_pipe, lgbm_pipe = result["rf_pipe"], result["xgb_pipe"], result["lgbm_pipe"]
        y_pred = pipe.predict(X_test)
        calib_data_by_station[col] = (
            compute_fog_probability(pipe, X_test),
            pd.Series(y_test).isin(SUMMARY_FOG_CODES).astype(int).to_numpy(),
        )
        acc = accuracy_score(y_test, y_pred)
        f1 = f1_score(y_test, y_pred, average="weighted", zero_division=0)
        fog_f1 = _fog_class_f1(y_test, y_pred)
        rf_only = _single_component_metrics(rf_pipe, True, X_test, y_test)
        xgb_only = _single_component_metrics(xgb_pipe, False, X_test, y_test) if xgb_pipe is not None else None
        lgbm_only = _evaluate_solo_pipe(lgbm_pipe, X_test, y_test) if lgbm_pipe is not None else None
        hybrid_only = None
        if xgb_pipe is not None and result["hybrid_weight"] is not None:
            hybrid_only = _evaluate_solo_pipe(
                HybridFogClassifier(rf_pipe, xgb_pipe, weight=result["hybrid_weight"], second_kind="xgb"),
                X_test, y_test,
            )
        rf_lgbm_only = None
        if lgbm_pipe is not None and result["hybrid_lgbm_weight"] is not None:
            rf_lgbm_only = _evaluate_solo_pipe(
                HybridFogClassifier(rf_pipe, lgbm_pipe, weight=result["hybrid_lgbm_weight"], second_kind="lgbm"),
                X_test, y_test,
            )
        triple_only = None
        if xgb_pipe is not None and lgbm_pipe is not None and result["triple_weights"] is not None:
            triple_only = _evaluate_solo_pipe(
                TripleBlendFogClassifier(rf_pipe, xgb_pipe, lgbm_pipe, *result["triple_weights"]),
                X_test, y_test,
            )
        log.info(
            "%s: n=%d, model=%s, accuracy=%.3f, f1=%.3f, fog_f1=%s, threshold=%.0f%%, tuned=%s",
            col, len(sub), pipe.model_type,
            acc, f1, "n/a" if np.isnan(fog_f1) else f"{fog_f1:.3f}", threshold * 100, tuned,
        )
        models[col] = {
            "pipe": pipe, "accuracy": acc, "f1": f1, "fog_f1": fog_f1,
            "n": len(sub), "features": col_features,
            "model_type": pipe.model_type, "blend_weight": pipe.weight,
            "fog_threshold": threshold, "pooled": False,
            "y_test": y_test.to_numpy(), "y_pred": np.asarray(y_pred),
            # apply_regression_guardで前回モデルにも同じテストデータを予測させるため(保存はしない。
            # save_model_state参照)。
            "X_test": X_test,
            "test_datetimes": sub["datetime"].iloc[len(X_train):].to_numpy(),
            # このモデルが学習に使った最後の日時。翌日以降の回帰判定で、このモデルが学習済みの
            # 行を比較から除くために保存する(_paired_test_predictions参照)。
            "train_end": pd.Timestamp(sub["datetime"].iloc[len(X_train) - 1]),
            "rf_only": rf_only, "xgb_only": xgb_only, "lgbm_only": lgbm_only,
            "hybrid_only": hybrid_only, "rf_lgbm_only": rf_lgbm_only, "triple_only": triple_only,
            "cross_fog_index_last": _last_valid_value(cross_rates[col]),
            "advection_index_last": _last_valid_value(advection_idx[col]),
            "measure_medians": col_medians,
        }

        y2_col = f"{col}__secondary"
        y2_all = sub[y2_col] if y2_col in sub.columns else pd.Series(np.nan, index=sub.index)
        _, _, y2_train, y2_test = _temporal_train_test_split(X, y2_all)
        prev_secondary = (prev_state.get(col) or {}).get("secondary")
        models[col]["secondary"] = _fit_or_reuse_secondary_model(
            X_train, y_train, y2_train, X_test, y2_test, pipe, prev_secondary,
        )

    n_individual = len(models)
    _rescue_with_pooled_model(
        models, merged, phenom_cols, feature_names, cross_rates, shared_medians, advection_idx,
    )

    n_pooled = len(models) - n_individual
    n_unmodeled = len(phenom_cols) - len(models)
    log.info(
        "Training summary: %d/%d individual, %d/%d pooled, %d/%d could not be modeled at all",
        n_individual, len(phenom_cols), n_pooled, len(phenom_cols), n_unmodeled, len(phenom_cols),
    )

    # 川霧確率較正モデルの学習・しきい値の較正後空間への変換は、ここでは行わない
    # (calib_data_by_stationをそのまま返す)。apply_regression_guardによる
    # ロールバック判定がこの後(predict.py側)で行われるため、まだ採用が確定していない
    # 今回の候補パイプの確率を較正学習に混ぜないよう、finalize_fog_calibration()を
    # ロールバック判定の後に呼ぶ設計にしている(2026-09-13の監査で指摘された、
    # 較正とロールバックの順序バグの修正)。
    return models, calib_data_by_station


def finalize_fog_calibration(models, calib_data_by_station, rolled_back=()):
    """train_location_modelsが集めたホールドアウト確率から川霧確率較正モデルを学習し、
    全地点(ロールバックされた地点も含む)のfog_thresholdを較正後の確率空間に変換する。
    apply_regression_guardの後に呼ぶこと。

    rolled_backに含まれる地点は、今回学習した候補パイプが不採用になり前回のパイプが
    そのまま配信される。前回のパイプの生確率はここでは再計算できない(前回のホールド
    アウトのX_testを保持していないため)ので、較正モデルの学習データからは単純に除外する
    (今回不採用になった候補の確率を混ぜるのは、実際に配信されないものを学習材料に
    使うことになり不適切なため)。しきい値の較正後空間への変換自体は、ロールバックされた
    地点も含め全地点に対して行う(配信時にはロールバック後のパイプも含め全地点の生確率に
    この較正モデルを適用するため、しきい値の表示・保存も同じ較正後空間に揃える必要がある)。
    """
    rolled_back = set(rolled_back)
    usable = {col: v for col, v in calib_data_by_station.items() if col not in rolled_back}
    probs = [p for p, _ in usable.values()]
    labels = [l for _, l in usable.values()]
    fog_calibrator = _fit_fog_calibrator(
        np.concatenate(probs) if probs else np.array([]),
        np.concatenate(labels) if labels else np.array([]),
    )
    if fog_calibrator is not None:
        # 較正はisotonic(単調非減少)なので、既に選んだしきい値(生の確率空間でF1最大の
        # 点)を較正後の値に写像するだけで、分類結果を変えずにしきい値の表示・保存だけを
        # 較正後の確率空間に合わせられる(グリッドを較正後の確率で再探索する必要はない)。
        for info in models.values():
            info["fog_threshold"] = float(apply_fog_calibration(fog_calibrator, [info["fog_threshold"]])[0])
    return fog_calibrator


def _station_fog_rate(y, global_rate):
    """地点ごとの川霧発生率を、サンプル数が少ないほど全地点平均に寄せて平滑化する。"""
    n = len(y)
    if n == 0:
        return global_rate
    k = float(y.isin(SUMMARY_FOG_CODES).sum())
    m = STATION_RATE_SMOOTHING
    return (k + m * global_rate) / (n + m)


def _compute_cross_station_fog_rates(merged, phenom_cols):
    """各地点について、自分を除く他地点のCROSS_STATION_FOG_LAG_HOURS時間前の川霧発生率を
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


def _rescue_with_pooled_model(models, merged, phenom_cols, feature_names, cross_rates, shared_medians, advection_idx):
    """個別条件を満たさなかった地点を、全地点共通データで学習した「プーリングモデル」で補う。

    気象特徴量は全地点で同一の値（代表地点1つの実況/予報）のため、地点ごとの過去の川霧発生率
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
        merged[ADVECTION_FOG_FEATURE] = advection_idx[col].reindex(merged.index).fillna(0.0).to_numpy()
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
        # rateは学習期間(train側)のyだけから計算する。以前は全期間(sub全体)のyから
        # 計算していたため、この後の_temporal_train_test_splitで作られるテスト期間の
        # ラベル情報がrate経由で特徴量に混入していた(リーク)。個々の地点を後で
        # 救済評価する際も同じ_temporal_train_test_split(既定test_ratio=0.2)を使うため、
        # ここで先に同じ分割をしてtrain側だけを使えば、評価時の分割と整合する。
        _, _, y_train_only, _ = _temporal_train_test_split(X, y)
        rate = _station_fog_rate(y_train_only, global_rate)
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
    pooled_result = _fit_best_model(Xp_train, yp_train)
    pooled_train_end = pd.Timestamp(pooled["__datetime"].iloc[len(Xp_train) - 1])
    pooled_pipe, pooled_tuned, pooled_threshold = (
        pooled_result["pipe"], pooled_result["tuned"], pooled_result["threshold"],
    )
    pooled_rf, pooled_xgb, pooled_lgbm = (
        pooled_result["rf_pipe"], pooled_result["xgb_pipe"], pooled_result["lgbm_pipe"],
    )

    rescued = 0
    deployed_without_eval = 0
    for col in skipped_cols:
        if col not in per_station_xy:
            continue
        sub, X, y = per_station_xy[col]
        if len(sub) < MIN_ROWS_FOR_POOLED_DEPLOY:
            continue
        X2 = X.copy()
        X2[STATION_FOG_RATE_FEATURE] = station_rates[col]

        # 十分な行数・2クラス以上が揃っていればホールドアウト評価付きで救済する。
        # 揃っていない(追加されたばかりで川霧の実例がまだ無い等)場合でも、プーリング
        # モデル自体は他地点のデータで既に学習済みのため、精度算出は諦めて予測にだけ使う
        # (accuracy/f1等はNoneのままにする。webapp.py側は既にNoneを「精度不明」として
        # 扱う設計になっている: 例えば_is_valid_number()での判定やis not Noneでのフィルタ)。
        can_evaluate = len(sub) >= MIN_ROWS_FOR_POOLED_RESCUE and y.nunique() >= 2
        acc2 = f12 = fog_f12 = None
        y_test2 = y_pred2 = None
        rf_only2 = xgb_only2 = lgbm_only2 = None
        hybrid_only2 = rf_lgbm_only2 = triple_only2 = None

        if can_evaluate:
            X_train2, X_test2, y_train2, y_test2 = _temporal_train_test_split(X2, y)
            if len(X_test2) == 0:
                can_evaluate = False
                y_test2 = None
            else:
                y_pred2 = pooled_pipe.predict(X_test2)
                acc2 = accuracy_score(y_test2, y_pred2)
                f12 = f1_score(y_test2, y_pred2, average="weighted", zero_division=0)
                fog_f12 = _fog_class_f1(y_test2, y_pred2)
                rf_only2 = _single_component_metrics(pooled_rf, True, X_test2, y_test2)
                xgb_only2 = _single_component_metrics(pooled_xgb, False, X_test2, y_test2) if pooled_xgb is not None else None
                lgbm_only2 = _evaluate_solo_pipe(pooled_lgbm, X_test2, y_test2) if pooled_lgbm is not None else None
                if pooled_xgb is not None and pooled_result["hybrid_weight"] is not None:
                    hybrid_only2 = _evaluate_solo_pipe(
                        HybridFogClassifier(pooled_rf, pooled_xgb, weight=pooled_result["hybrid_weight"], second_kind="xgb"),
                        X_test2, y_test2,
                    )
                if pooled_lgbm is not None and pooled_result["hybrid_lgbm_weight"] is not None:
                    rf_lgbm_only2 = _evaluate_solo_pipe(
                        HybridFogClassifier(pooled_rf, pooled_lgbm, weight=pooled_result["hybrid_lgbm_weight"], second_kind="lgbm"),
                        X_test2, y_test2,
                    )
                if pooled_xgb is not None and pooled_lgbm is not None and pooled_result["triple_weights"] is not None:
                    triple_only2 = _evaluate_solo_pipe(
                        TripleBlendFogClassifier(pooled_rf, pooled_xgb, pooled_lgbm, *pooled_result["triple_weights"]),
                        X_test2, y_test2,
                    )

        models[col] = {
            "pipe": pooled_pipe, "accuracy": acc2, "f1": f12, "fog_f1": fog_f12,
            "n": len(sub), "features": feature_names, "station_fog_rate": station_rates[col],
            "model_type": f"pooled({pooled_pipe.model_type})", "blend_weight": pooled_pipe.weight,
            "fog_threshold": pooled_threshold, "pooled": True,
            "y_test": (y_test2.to_numpy() if y_test2 is not None else None),
            "y_pred": (np.asarray(y_pred2) if y_pred2 is not None else None),
            "X_test": (X_test2 if can_evaluate else None),
            "test_datetimes": (sub["datetime"].iloc[len(X_train2):].to_numpy() if can_evaluate else None),
            "train_end": pooled_train_end,
            "rf_only": rf_only2, "xgb_only": xgb_only2, "lgbm_only": lgbm_only2,
            "hybrid_only": hybrid_only2, "rf_lgbm_only": rf_lgbm_only2, "triple_only": triple_only2,
            "cross_fog_index_last": _last_valid_value(cross_rates[col]),
            "advection_index_last": _last_valid_value(advection_idx[col]),
            "measure_medians": shared_medians,
            # プーリング救済モデルは元々データが薄い地点向けの救済措置のため、そこからさらに
            # 稀な「2コード同時」の副コードまで学習するのは無理があり、Stage B自体を対象外とする。
            "secondary": {
                "presence_pipe": None, "identity_pipe": None, "identity_classes_": None,
                "presence_threshold": DEFAULT_FOG_THRESHOLD, "n_dual": None,
                "trained_at": datetime.now(JST).isoformat(), "skipped_reason": "pooled_station",
            },
        }
        rescued += 1
        if not can_evaluate:
            deployed_without_eval += 1
    log.info(
        "Pooled model rescued %d/%d location(s) (%d deployed without a reliable evaluation split yet)",
        rescued, len(skipped_cols), deployed_without_eval,
    )


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
    # 学習側(_prepare_weather_for_training等)のWEATHER_INTERPOLATE_LIMITと揃える。
    # 以前はlimit指定が無く、Open-Meteoの応答が長時間欠落した場合に任意の長さの区間が
    # 直線補間で埋まってしまっていた。
    df[numeric_cols] = df[numeric_cols].interpolate(
        method="linear", limit=WEATHER_INTERPOLATE_LIMIT, limit_direction="both",
    )
    df = df.dropna(subset=[COL_TEMP, COL_HUMIDITY, COL_DEWPOINT, COL_WIND_SPEED]).reset_index(drop=True)
    if df.empty:
        raise ValueError("Open-Meteoから有効な予報値を取得できませんでした。")
    return df


def _get_forecast_with_retry(params, timeout):
    """Open-Meteo予報APIをリトライ付きで呼ぶ。1回の失敗でその日の予測が丸ごと
    スキップされていた問題への対応(collect.pyの実況取得と同様のリトライ)。
    DNS解決の一時的失敗にも common.request_with_dns_fallback でフォールバックする。
    """
    last_exc = None
    for attempt in range(1, FORECAST_FETCH_RETRIES + 2):
        try:
            resp = request_with_dns_fallback(
                "GET", OPEN_METEO_FORECAST_URL, params=params, timeout=timeout, log=log,
            )
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            log.warning(
                "Open-Meteo forecast fetch failed (attempt %d/%d): %s",
                attempt, FORECAST_FETCH_RETRIES + 1, e,
            )
            if attempt < FORECAST_FETCH_RETRIES + 1:
                time.sleep(FORECAST_FETCH_RETRY_DELAY_SEC)
    raise last_exc


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
    resp = _get_forecast_with_retry(params, timeout=20)
    payload = resp.json()
    if "hourly" not in payload:
        raise ValueError(f"Open-Meteoの応答にhourlyが含まれていません: {payload}")
    return _forecast_payload_to_df(payload["hourly"])


def fetch_forecast_range_multi(camera_coords, forecast_days):
    """全地点分の予報を1回のAPI呼び出しでまとめて取得する。戻り値: {地点名: forecast_df}。
    地点ごとに取得・変換できた分だけを含む（失敗した地点はキーごと含まれない）。
    """
    if not camera_coords:
        return {}
    names = list(camera_coords.keys())
    params = {
        "latitude": ",".join(str(camera_coords[n][0]) for n in names),
        "longitude": ",".join(str(camera_coords[n][1]) for n in names),
        "hourly": ",".join(_FORECAST_VARS.keys()),
        "timezone": "Asia/Tokyo",
        "forecast_days": forecast_days,
        "wind_speed_unit": "ms",
        "precipitation_unit": "mm",
        "temperature_unit": "celsius",
    }
    resp = _get_forecast_with_retry(params, timeout=30)
    payload = resp.json()
    if isinstance(payload, dict):
        payload = [payload]  # 地点が1件だけの時は単一オブジェクトで返ってくるため揃える

    result = {}
    for name, item in zip(names, payload):
        if not item or "hourly" not in item:
            continue
        # Open-Meteoの複数地点バッチ応答は位置(names[i] <-> payload[i])で対応付けている。
        # 応答がエコーバックするlatitude/longitudeと要求した座標を突き合わせ、万一順序が
        # ずれていたら(APIの仕様変更等)黙って誤った地点同士を対応付けるのではなく検知して
        # スキップする(2026-09-13の監査で指摘)。
        expected_lat, expected_lon = camera_coords[name]
        echoed_lat, echoed_lon = item.get("latitude"), item.get("longitude")
        if echoed_lat is not None and echoed_lon is not None:
            if abs(echoed_lat - expected_lat) > 0.05 or abs(echoed_lon - expected_lon) > 0.05:
                log.error(
                    "Open-Meteo multi-location response order mismatch for %s: "
                    "expected (%.4f, %.4f), got (%.4f, %.4f); skipping to avoid misaligned data",
                    name, expected_lat, expected_lon, echoed_lat, echoed_lon,
                )
                continue
        try:
            result[name] = _forecast_payload_to_df(item["hourly"])
        except ValueError:
            continue
    return result


def build_location_forecast_codes(models, forecast_raw, lat, lon, forecast_by_station=None,
                                   camera_coords=None, calibrator=None):
    """予報データに対して地点ごとの現象コードと川霧確率を予測する。
    forecast_by_station/camera_coordsが与えられ、かつその地点の予報が取得できていれば、
    共通の代表地点予報(forecast_raw/lat/lon)の代わりにその地点自身の予報を使う。
    calibratorを渡すとpredicted_fog_prob(prob_df/feature_snapshots両方)に較正を適用する。
    戻り値: (pred_df, prob_df, secondary_pred_df, secondary_prob_df, feature_snapshots)。
    pred_df/prob_df/secondary_pred_df/secondary_prob_dfはいずれもdatetime列 + 地点ごとの列。
    secondary_pred_dfは2コード同時観測(2026-09対応)の副コード(Stage B未学習の地点はNone)、
    secondary_prob_dfはその出現確率。
    feature_snapshotsは {地点名: DataFrame(datetime, 各特徴量, predicted_code, predicted_fog_prob,
    predicted_code2, predicted_code2_prob)}で、「この予測にどの特徴量の値が使われたか」を
    後から参照する(station_detail.htmlの表示用)ために予測結果と一緒に返す。
    """
    pred_df = pd.DataFrame({"datetime": forecast_raw["datetime"].values})
    prob_df = pd.DataFrame({"datetime": forecast_raw["datetime"].values})
    secondary_pred_df = pd.DataFrame({"datetime": forecast_raw["datetime"].values})
    secondary_prob_df = pd.DataFrame({"datetime": forecast_raw["datetime"].values})
    feature_snapshots = {}
    for col, info in models.items():
        feature_names = info["features"]
        station_forecast = (forecast_by_station or {}).get(col)
        if station_forecast is not None and len(station_forecast) > 0:
            # 以前は1時間でも代表地点予報と行数が食い違う(取得の一部失敗・欠測補間の
            # dropna等)と地点別予報を丸ごと捨てて代表地点予報にフォールバックしていた。
            # 代表地点の日時にreindexしたうえでcombine_firstを使い、地点別予報がある
            # 時間はそれを、欠けている時間だけ代表地点予報の値で埋める形に改めた
            # (2026-09-13の監査で「全か無か」の設計を指摘された)。
            f_raw = (
                station_forecast.set_index("datetime")
                .reindex(forecast_raw["datetime"])
                .combine_first(forecast_raw.set_index("datetime"))
                .reset_index()
            )
            f_lat, f_lon = (camera_coords or {}).get(col, (lat, lon))
        else:
            # 地点別の予報が全く取得できなかった場合は共通の代表地点予報にフォールバックする。
            f_raw, f_lat, f_lon = forecast_raw, lat, lon
        # 任意項目(視程・日照・日射)の中央値埋めは、学習時にfit済みの中央値(measure_medians)
        # をそのまま使う。以前は予報データ(16日分だけ)から都度中央値を計算し直しており、
        # 学習時と全く異なる母集団の中央値で埋めることになっていた(train/inference不整合)。
        df, _, _ = prepare_features(f_raw, f_lat, f_lon, feature_names, medians=info.get("measure_medians"))
        X = df[feature_names].copy()
        if info.get("pooled") and "station_fog_rate" in info:
            X[STATION_FOG_RATE_FEATURE] = info["station_fog_rate"]
        if CROSS_STATION_FOG_FEATURE in feature_names:
            # 予報期間中は他地点の実測が存在しないため、直近の実測時点での値を
            # 一定値として引き継ぐ近似値を使う（train_location_modelsのコメント参照）。
            X[CROSS_STATION_FOG_FEATURE] = info.get("cross_fog_index_last", 0.0)
        if ADVECTION_FOG_FEATURE in feature_names:
            X[ADVECTION_FOG_FEATURE] = info.get("advection_index_last", 0.0)
        pred_df[col] = info["pipe"].predict(X).astype(int)
        prob_df[col] = compute_fog_probability(info["pipe"], X, calibrator=calibrator)
        secondary_codes, secondary_probs = predict_secondary_codes(info, X, pred_df[col].to_numpy())
        secondary_pred_df[col] = secondary_codes
        secondary_prob_df[col] = secondary_probs
        snapshot = X.copy()
        snapshot.insert(0, "datetime", df["datetime"].values)
        snapshot["predicted_code"] = pred_df[col].values
        snapshot["predicted_fog_prob"] = prob_df[col].values
        snapshot["predicted_code2"] = secondary_pred_df[col].values
        snapshot["predicted_code2_prob"] = secondary_prob_df[col].values
        feature_snapshots[col] = snapshot
    return pred_df, prob_df, secondary_pred_df, secondary_prob_df, feature_snapshots


def simulate_station(info, lat, lon, weather, dt, calibrator=None):
    """手動入力した1時点分の気象条件から、その地点が採用中のモデルで川霧予測をシミュレートする
    (Whatーifシミュレーター用)。build_location_forecast_codesと同じ特徴量の組み立て方
    (学習時にfitした中央値・地点別川霧発生率・広域川霧指数の最終値を使う近似)を、1行分の
    データに対して行う。

    info: load_model_state()で読んだ、1地点分の辞書(models[地点名])。
    weather: fog_features.COL_*を主なキーとする値のdict(欠けている項目はNaNのまま扱われ、
    視程・日照・日射は学習時の中央値(measure_medians)で自動的に埋まる)。
    dt: この予測を行う日時(時刻依存の特徴量に使う)。
    calibrator: load_fog_calibrator()で読んだ較正モデル。渡すとfog_probが較正後の値になり、
    しきい値グラフ等(本番のpredicted_fog_prob)と同じ確率空間で比較できる。

    戻り値: (predicted_code, predicted_code2, fog_prob, class_proba, secondary_class_proba)。
    class_proba/secondary_class_probaは{現象コード: 確率}を確率降順に並べたdict
    (secondary_class_probaはStage B未学習の地点では空dict)。predicted_code2は
    2コード同時観測(2026-09対応)の副コード(無ければNone)。
    """
    raw = pd.DataFrame([{**weather, "datetime": pd.Timestamp(dt)}])
    feature_names = info["features"]
    df, _, _ = prepare_features(raw, lat, lon, feature_names, medians=info.get("measure_medians"))
    X = df[feature_names].copy()
    if info.get("pooled") and "station_fog_rate" in info:
        X[STATION_FOG_RATE_FEATURE] = info["station_fog_rate"]
    if CROSS_STATION_FOG_FEATURE in feature_names:
        # 予報時(build_location_forecast_codes)と同じく、他地点の実測が無い分は
        # 直近の実測時点での値を一定値として引き継ぐ近似を使う。
        X[CROSS_STATION_FOG_FEATURE] = info.get("cross_fog_index_last", 0.0)
    if ADVECTION_FOG_FEATURE in feature_names:
        X[ADVECTION_FOG_FEATURE] = info.get("advection_index_last", 0.0)

    pipe = info["pipe"]
    proba = np.asarray(pipe.predict_proba(X))[0]
    predicted_code = int(pipe.classes_[np.argmax(proba)])
    fog_prob = float(compute_fog_probability(pipe, X, calibrator=calibrator)[0])
    class_proba = dict(sorted(
        ((int(c), float(p)) for c, p in zip(pipe.classes_, proba)),
        key=lambda item: -item[1],
    ))

    secondary_codes, _secondary_probs = predict_secondary_codes(info, X, np.array([predicted_code]))
    predicted_code2 = secondary_codes[0]
    secondary_class_proba = {}
    secondary = info.get("secondary") or {}
    identity_pipe = secondary.get("identity_pipe")
    if identity_pipe is not None:
        X_ext = _stage_a_extra_features(X, pipe)
        id_proba = np.asarray(identity_pipe.predict_proba(X_ext))[0]
        identity_classes = secondary.get("identity_classes_") or identity_pipe.named_steps["model"].classes_.tolist()
        secondary_class_proba = dict(sorted(
            ((int(c), float(p)) for c, p in zip(identity_classes, id_proba)),
            key=lambda item: -item[1],
        ))
    return predicted_code, predicted_code2, fog_prob, class_proba, secondary_class_proba


def _effective_score(info):
    """モデル比較用のスコア。川霧クラスのF1を優先し、無ければ全体F1を使う
    （ハイパーパラメータ選択時の_blend_scoreと同じ考え方）。"""
    fog_f1 = info.get("fog_f1")
    if fog_f1 is not None and not (isinstance(fog_f1, float) and np.isnan(fog_f1)):
        return float(fog_f1)
    return float(info.get("f1", 0.0))


def _effective_score_or_none(info):
    """_effective_scoreと同じだが、fog_f1もf1も無い(プーリング救済でホールドアウト
    評価split自体が組めなかった地点、"deployed without eval")場合はNoneを返す
    (_effective_scoreのfloat(info.get("f1", 0.0))は、"f1"キー自体はあるが値がNoneの
    ときfloat(None)で例外になるため、そのまま流用できない)。"""
    fog_f1 = info.get("fog_f1")
    if fog_f1 is not None and not (isinstance(fog_f1, float) and np.isnan(fog_f1)):
        return float(fog_f1)
    f1 = info.get("f1")
    return float(f1) if f1 is not None else None


_MODEL_STATE_CACHE = {"path": None, "mtime": None, "data": None}


def load_model_state(path=MODEL_STATE_PATH):
    """前回保存したモデル一式を読み込む。無い/壊れている場合は空辞書（比較なしで続行）。

    webapp.py側の複数ルート(シミュレーター、管理画面など)がリクエストのたびにこの関数を
    呼ぶため、ファイルの更新日時が前回読み込み時と同じであればプロセス内キャッシュを返す
    (公開してアクセスが増えた場合に、リクエストごとに数十地点分のjoblibを毎回ディスクから
    読み直すのを避けるため)。predict.pyは別プロセスで日次実行されるため、再学習後の
    ファイル更新はmtimeの変化として自然に検知される。
    """
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
        log.warning("Failed to load previous model state (%s), skipping regression check: %s", path, e)
        return {}
    cache["path"], cache["mtime"], cache["data"] = path, mtime, data
    return data


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
    # X_testは回帰判定(apply_regression_guard)のためだけに持つ一時データなので保存しない。
    # trained_atは、ロールバックで前回モデルを使い続ける地点では前回の値(そのモデルを実際に
    # 学習した日時)を残す。以前は毎回上書きしていたため、何週間も更新が止まっている地点も
    # 「今日学習した」ように見えていた(2026-10-03の調査で判明)。
    state = {
        col: {
            **{k: v for k, v in info.items() if k not in ("X_test", "test_datetimes")},
            "trained_at": info.get("trained_at") or trained_at,
        }
        for col, info in models.items()
    }
    # 前世代を1つだけ.bakとして残す(このファイルが破損した場合のフォールバック用。
    # 歴代全部は保持しない。精度の推移自体はappend_metrics_historyの履歴CSVで追える)。
    if path.exists():
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except Exception as e:
            log.warning("Failed to back up previous model state: %s", e)
    # 書き込み中にプロセスが落ちても壊れたファイルが残らないよう、一時ファイルに書いてから
    # 置き換える(os.replaceは同一ファイルシステム内であれば原子的)。
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    joblib.dump(state, tmp_path)
    tmp_path.replace(path)
    append_metrics_history(models, trained_at)


def _score_from_predictions(y_true, y_pred, fog_labels=None):
    """_effective_scoreと同じ考え方（川霧クラスF1優先、無ければ全体F1）を、任意の
    (y_true, y_pred)配列に対して計算する（ブートストラップ再抽出のたびに呼び出す）。
    fog_labelsを渡すと、このリサンプルにたまたま川霧の実例が1件も無くても(全体F1に
    フォールバックせず)強制的にそのラベル集合でF1を計算する(下の_bootstrap_scores参照)。
    """
    if fog_labels is not None:
        return float(f1_score(y_true, y_pred, labels=fog_labels, average="macro", zero_division=0))
    fog_f1 = _fog_class_f1(y_true, y_pred)
    if not (isinstance(fog_f1, float) and np.isnan(fog_f1)):
        return float(fog_f1)
    return float(f1_score(y_true, y_pred, average="weighted", zero_division=0))


def _fog_labels_present(y_true):
    """y_trueに実際に含まれる川霧コード(SUMMARY_FOG_CODES)のリスト。無ければNone
    (その場合は川霧F1ではなく全体F1で評価する。_fog_class_f1と同じ考え方)。"""
    present = set(pd.Series(y_true).unique())
    labels = [c for c in sorted(SUMMARY_FOG_CODES) if c in present]
    return labels or None


def _paired_test_predictions(prev_info, new_info):
    """今回のテストデータに対する (y_true, 今回モデルの予測, 前回モデルの予測) を返す。
    比較できない場合(今回モデルに評価用データが無い、前回モデルが今回の特徴量で予測できない等)はNone。

    以前は「前回モデルを前回の学習時に採点したスコア」と「今回モデルを今回のテストデータで
    採点したスコア」を比べていた。テスト期間が違ううえ、川霧の実例が無い期間では全体F1、
    ある期間では川霧F1と指標まで変わるため、前回のテスト期間に川霧が無かった地点
    (全体F1で0.9前後)は、今回川霧を含む期間で採点された新モデル(川霧F1で0前後)に毎回
    「劣化」と判定されていた。しかもロールバック後は前回モデルと前回のテスト結果が
    そのまま保存されるため、同じ比較が毎日繰り返され、一度ロールバックされると二度と
    更新されなかった(2026-10-03の調査で、14地点が9月中旬〜下旬から固定されていた)。
    そこで、前回モデルにも今回のテストデータを予測させ、同じデータ・同じ指標で比べる。
    """
    X_test = new_info.get("X_test")
    y_test = new_info.get("y_test")
    new_pred = new_info.get("y_pred")
    test_datetimes = new_info.get("test_datetimes")
    prev_pipe = prev_info.get("pipe")
    prev_train_end = prev_info.get("train_end")
    if X_test is None or y_test is None or new_pred is None or prev_pipe is None or len(X_test) == 0:
        return None
    # 前回モデルが学習に使った期間と今回のテスト期間は重なる(テストは直近20%なので、前日の
    # モデルは今日のテスト行のほとんどを学習済み)。重なった行で比べると前回モデルが不当に
    # 有利になるため、前回モデルの学習最終日時より後の行だけで比べる。学習最終日時が記録されて
    # いない古い形式のモデル(2026-10-03より前に保存されたもの)は公平に比べられないので、
    # 比較せず今回のモデルを採用する。
    if prev_train_end is None or test_datetimes is None:
        log.info("%s: previous model has no recorded training cutoff; adopting the new model without comparison",
                 new_info.get("_station", "?"))
        return None
    unseen = pd.to_datetime(pd.Series(test_datetimes)).to_numpy() > np.datetime64(pd.Timestamp(prev_train_end))
    if not unseen.any():
        return None
    X_test = X_test[unseen]
    y_test = np.asarray(y_test)[unseen]
    new_pred = np.asarray(new_pred)[unseen]
    # 今回と前回で特徴量の構成が違うことがある(個別モデル⇔プーリング救済の切り替え等)ため、
    # そのまま予測できなければ前回モデルの特徴量だけを取り出して試す。
    candidates = [X_test]
    prev_features = prev_info.get("features")
    if prev_features and all(f in X_test.columns for f in prev_features):
        candidates.append(X_test[list(prev_features)])
    last_error = None
    for X in candidates:
        try:
            prev_pred = np.asarray(prev_pipe.predict(X))
            break
        except Exception as e:
            last_error = e
    else:
        log.warning("Could not evaluate the previous model on the current test data: %s", last_error)
        return None
    return np.asarray(y_test), np.asarray(new_pred), prev_pred


def _paired_bootstrap_prob_not_worse(y_true, new_pred, prev_pred, fog_labels, n_boot=REGRESSION_N_BOOTSTRAP, seed=1):
    """同じテストデータを復元抽出でn_boot回リサンプリングし、そのたび同じ行で今回・前回の
    スコアを計算して「今回 >= 前回」となった割合を返す(0〜1)。同じ行で比べる対応のある
    比較なので、テストデータ自体の難しさの違いが差に混ざらない。"""
    rng = np.random.default_rng(seed)
    n = len(y_true)
    not_worse = 0
    for idx in rng.integers(0, n, size=(n_boot, n)):
        yt = y_true[idx]
        new_score = _score_from_predictions(yt, new_pred[idx], fog_labels=fog_labels)
        prev_score = _score_from_predictions(yt, prev_pred[idx], fog_labels=fog_labels)
        if new_score >= prev_score:
            not_worse += 1
    return not_worse / n_boot


def apply_regression_guard(models, prev_state=None, path=MODEL_STATE_PATH):
    """新しく学習したモデルが前回より統計的に有意に劣化していると判断できる地点があれば、
    その地点だけ前回のモデルへ自動的に差し戻す（models を直接書き換える）。
    比較は、今回のテストデータで前回・今回の両モデルを採点する対応のある比較で行う
    (_paired_test_predictions参照)。比較できない地点は今回のモデルを採用する。
    戻り値：ロールバックした地点名のリスト。
    """
    if prev_state is None:
        prev_state = load_model_state(path)
    rolled_back = []
    for col, info in models.items():
        prev = prev_state.get(col)
        if prev is None:
            continue
        paired = _paired_test_predictions(prev, {**info, "_station": col})
        if paired is None:
            continue
        y_true, new_pred, prev_pred = paired
        fog_labels = _fog_labels_present(y_true)
        metric = "fog_f1" if fog_labels else "f1"
        new_score = _score_from_predictions(y_true, new_pred, fog_labels=fog_labels)
        prev_score = _score_from_predictions(y_true, prev_pred, fog_labels=fog_labels)
        if len(y_true) < MIN_N_FOR_REGRESSION_CHECK:
            # テストデータが少なすぎてブートストラップが当てにならない場合は、大きく下がった
            # ときだけ差し戻す(SPARSE_REGRESSION_ABS_DROP)。
            regressed = new_score < prev_score - SPARSE_REGRESSION_ABS_DROP
            how = f"absolute-drop guard, only {len(y_true)} test row(s)"
        else:
            prob_not_worse = _paired_bootstrap_prob_not_worse(y_true, new_pred, prev_pred, fog_labels)
            regressed = prob_not_worse < (1.0 - REGRESSION_CONFIDENCE)
            how = f"paired bootstrap prob(new>=prev)={prob_not_worse:.3f}"
        if not regressed:
            continue
        log.warning(
            "Regression detected for %s on the same %d test row(s) (%s: new=%.3f, previous=%.3f, %s); "
            "rolling back to previous model",
            col, len(y_true), metric, new_score, prev_score, how,
        )
        # 差し戻した前回モデルの精度は、今回のテストデータで測り直した値に置き換える
        # (前回学習時の古いテスト期間の値のままだと、管理画面や履歴CSVに実態と違う精度が残る)。
        models[col] = {
            **prev,
            "model_type": normalize_model_type_label(prev.get("model_type")),
            "y_test": y_true, "y_pred": prev_pred,
            "accuracy": float(accuracy_score(y_true, prev_pred)),
            "f1": float(f1_score(y_true, prev_pred, average="weighted", zero_division=0)),
            "fog_f1": _fog_class_f1(y_true, prev_pred),
        }
        rolled_back.append(col)
    return rolled_back
