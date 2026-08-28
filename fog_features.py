"""霧予測モデル用の特徴量エンジニアリング。

~/Desktop/fog-prediction/weather_visualizer.py（Colab向け・単発実行ツール）の
特徴量設計をそのまま踏襲しつつ、river-fogサーバー用に以下を簡略化している。
- 列レイアウトは common.py の固定列を前提とし、Excelの自動レイアウト検出は行わない
- 本番データに「テスト用ダミーブロック」は存在しないため、その検出処理は持たない
"""
import math

import numpy as np
import pandas as pd

from common import WIND_DIR_DEGREES

# --- 現象コード関連（weather_visualizer.pyと同一の定義） ---
PHENOM_LABELS = {
    1: "薄い川霧", 2: "川霧", 3: "濃い川霧", 4: "薄い全体霧", 5: "全体霧",
    6: "全体濃い霧", 7: "薄い層雲", 8: "濃い層雲", 9: "霧雨", 10: "雨",
}
PHENOM_COLORS = {
    1: "#aed6f1", 2: "#3498db", 3: "#1a5276", 4: "#dcdde1", 5: "#909497",
    6: "#2c3e50", 7: "#f8c471", 8: "#d35400", 9: "#a9dfbf", 10: "#196f3d",
}
SLASH_COLOR = "#f9e79f"
SUMMARY_FOG_CODES = {1, 2, 3, 4, 5, 6}  # 「霧」とみなすコード（霧F1・霧確率・サマリー集計で使用）

# --- 特徴量の列名（DataFrame上の日本語列名。weather_visualizer.pyと揃えている） ---
COL_TEMP = "気温(℃)"
COL_PRECIP = "降水量(mm)"
COL_WIND_SPEED = "風速(m/s)"
COL_WIND_DIR_DEG = "風向"
COL_HUMIDITY = "相対湿度(％)"
COL_DEWPOINT = "露点温度(℃)"
COL_SUNSHINE = "日照時間(時間)"
COL_SOLAR = "日射量(MJ/m2)"
COL_VISIBILITY = "視程(km)"

BASE_FEATURES = [
    COL_TEMP, COL_PRECIP, COL_WIND_SPEED, COL_HUMIDITY, COL_DEWPOINT,
    "気温露点差", "時_sin", "時_cos", "月",
]
WIND_DIR_FEATURES = ["風向_sin", "風向_cos"]

# ファイルにあれば特徴量として使う任意項目。有効値の割合がMIN_COVERAGE未満なら使わない
# （視程は欠測が多いため、実質的にはここで自動的に足切りされる）。
OPTIONAL_MEASURE_KEYWORDS = [COL_SUNSHINE, COL_SOLAR, COL_VISIBILITY]
OPTIONAL_MEASURE_MIN_COVERAGE = 0.05

LAG_FEATURE_NAMES = ["気温変化_1h", "気温変化_3h", "風速平均_3h", "降水量_24h合計"]
SUN_FEATURE_NAME = "日の出まで残り時間"


def encode_phenomena_cell(value):
    """セルの値を現象コードに変換する。'/' -> 0、1〜10はそのまま、空欄/解釈不能はNaN。"""
    if value is None:
        return np.nan
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
        if np.isnan(value):
            return np.nan
        code = int(value)
        return float(code) if 0 <= code <= 10 else np.nan
    s = str(value).strip()
    if not s:
        return np.nan
    if s == "/":
        return 0.0
    try:
        code = int(float(s))
    except ValueError:
        return np.nan
    return float(code) if 0 <= code <= 10 else np.nan


def encode_wind_direction_degrees(value):
    """common.py が書き込む16方位の日本語表記（例:「北北西」）を度に変換する。無風・不明はNaN。"""
    if value is None:
        return np.nan
    s = str(value).strip()
    return WIND_DIR_DEGREES.get(s, np.nan)


def _lag_series(df, col, hours):
    """`col` の `hours` 時間前の値。datetimeの値そのもので引き当てるため、
    欠測で行が飛んでいても位置ベースの shift() のように誤った差分にならない。
    """
    s = df.drop_duplicates(subset="datetime").set_index("datetime")[col]
    lookup = pd.DatetimeIndex(df["datetime"]) - pd.Timedelta(hours=hours)
    return pd.Series(s.reindex(lookup).to_numpy(), index=df.index)


def _add_lag_features(df):
    df = df.copy()
    df["気温変化_1h"] = df[COL_TEMP] - _lag_series(df, COL_TEMP, 1)
    df["気温変化_3h"] = df[COL_TEMP] - _lag_series(df, COL_TEMP, 3)

    dedup = df.drop_duplicates(subset="datetime").sort_values("datetime")
    dt_index = pd.DatetimeIndex(dedup["datetime"])
    target_index = pd.DatetimeIndex(df["datetime"])

    s = pd.Series(pd.to_numeric(dedup[COL_WIND_SPEED], errors="coerce").to_numpy(), index=dt_index)
    df["風速平均_3h"] = s.rolling("3h", min_periods=1).mean().reindex(target_index).to_numpy()

    precip = pd.to_numeric(dedup[COL_PRECIP], errors="coerce").fillna(0.0)
    s = pd.Series(precip.to_numpy(), index=dt_index)
    df["降水量_24h合計"] = s.rolling("24h", min_periods=1).sum().reindex(target_index).to_numpy()
    return df


def _sunrise_hour(ts, lat, lon):
    """NOAAの簡易式によるその日の日の出時刻の近似（JST、0時からの経過時間）。"""
    n = ts.timetuple().tm_yday
    lat_rad = math.radians(lat)
    decl = math.radians(23.44) * math.sin(math.radians(360.0 / 365.0 * (n - 81)))
    cos_hour_angle = max(-1.0, min(1.0, -math.tan(lat_rad) * math.tan(decl)))
    hour_angle_deg = math.degrees(math.acos(cos_hour_angle))
    solar_noon = 12.0 - lon / 15.0 + 9.0
    return solar_noon - hour_angle_deg / 15.0


def _add_sunrise_feature(df, lat, lon):
    """『日の出まで残り何時間か』（明け方以外は0）を特徴量として追加する。"""
    df = df.copy()
    dt = pd.to_datetime(df["datetime"])
    day_start = dt.dt.normalize()
    hour_of_day = (dt - day_start).dt.total_seconds().to_numpy() / 3600.0

    unique_days = day_start.unique()
    sunrise_by_day = {d: _sunrise_hour(pd.Timestamp(d), lat, lon) for d in unique_days}
    sunrise_hours = day_start.map(sunrise_by_day).to_numpy(dtype="float64")

    remaining = sunrise_hours - hour_of_day
    df[SUN_FEATURE_NAME] = np.where((remaining < 0) | (remaining > 12), 0.0, remaining)
    return df


def _add_time_features(df, lat, lon):
    df = df.copy()
    df["時"] = df["datetime"].dt.hour
    df["月"] = df["datetime"].dt.month
    df["気温露点差"] = df[COL_TEMP] - df[COL_DEWPOINT]
    df["時_sin"] = np.sin(2 * np.pi * df["時"] / 24)
    df["時_cos"] = np.cos(2 * np.pi * df["時"] / 24)
    df = _add_lag_features(df)
    df = _add_sunrise_feature(df, lat, lon)
    if COL_WIND_DIR_DEG in df.columns:
        rad = np.deg2rad(pd.to_numeric(df[COL_WIND_DIR_DEG], errors="coerce"))
        df["風向_sin"] = np.sin(rad).fillna(0.0)
        df["風向_cos"] = np.cos(rad).fillna(0.0)
    return df


def select_feature_names(df):
    features = list(BASE_FEATURES)
    if all(f in df.columns for f in WIND_DIR_FEATURES):
        features += WIND_DIR_FEATURES
    for label in OPTIONAL_MEASURE_KEYWORDS:
        if label in df.columns and df[label].notna().any():
            features.append(label)
    features += [f for f in LAG_FEATURE_NAMES if f in df.columns]
    if SUN_FEATURE_NAME in df.columns:
        features.append(SUN_FEATURE_NAME)
    return features


def _fill_optional_measures(df):
    """任意項目（視程・日照時間・日射量）の欠測を、有効値割合が十分あれば中央値で埋める。
    割合が低ければNaNのままにし、select_feature_names()に特徴量から外させる。
    """
    df = df.copy()
    for label in OPTIONAL_MEASURE_KEYWORDS:
        if label not in df.columns:
            continue
        col = pd.to_numeric(df[label], errors="coerce")
        coverage = col.notna().mean() if len(col) else 0.0
        df[label] = col.fillna(col.median()) if coverage >= OPTIONAL_MEASURE_MIN_COVERAGE else np.nan
    return df


def prepare_features(df, lat, lon, feature_names=None):
    """特徴量一式を組み立てる。降水量の欠測は0mmとして扱う（無降水時は空欄の記録が多いため）。"""
    out = _add_time_features(df, lat, lon)
    out = _fill_optional_measures(out)
    out[COL_PRECIP] = pd.to_numeric(out[COL_PRECIP], errors="coerce").fillna(0.0)

    for name in ("気温変化_1h", "気温変化_3h"):
        out[name] = out[name].fillna(0.0)
    out["風速平均_3h"] = out["風速平均_3h"].fillna(out[COL_WIND_SPEED])
    out["降水量_24h合計"] = out["降水量_24h合計"].fillna(0.0)

    if feature_names is None:
        feature_names = select_feature_names(out)
    for name in feature_names:
        if name not in out.columns:
            out[name] = np.nan
    return out, feature_names


def build_location_class_targets(phenom_df, phenom_cols):
    """地点それぞれの現象コード（0="/"、1〜10、欠測はNaN）を作る。

    雨・霧雨(9・10)は学習ターゲットとしては0(現象なし)に丸める。雨予報は
    別途一般の天気予報で足りるため、この霧予測モデルでは雨そのものを予測対象に
    しない（学習データに9・10が出てこなくなるため、scikit-learnの分類器も
    自然にそれらのクラスを一切予測しなくなる）。実況分類(collect.py)側は
    引き続き9・10を判定・記録し、霧との誤認別を防ぐ。
    """
    codes = phenom_df[phenom_cols].apply(lambda col: col.map(encode_phenomena_cell))
    codes = codes.replace({9: 0, 10: 0})
    targets = codes.copy()
    targets.insert(0, "datetime", phenom_df["datetime"].values)
    return targets
