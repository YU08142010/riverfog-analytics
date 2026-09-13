"""風向・風速と地点間の実測座標(cameras.csv)から、川霧の移流(どちらからどちらへ
流れているか)を近似する特徴量。

これまでのCROSS_STATION_FOG_FEATURE(広域川霧指数)は「自地点を除く他地点全ての
1時間前の川霧発生率」を単純平均するだけで、地理的な位置関係も風向も一切見ていなかった。
この移流川霧指数は、地点ごとの実測緯度・経度(cameras.csv、捏造ではなく既存データ)と
実測の風向・風速から、「今の風なら他地点のどの実況が何時間後に自地点へ届くはずか」を
物理的に(大まかに)逆算し、その到達予定時刻ちょうどの実況を重み付き平均する。

川の合流・分流構造までは座標だけからは正しく再構成できない(誤った河川ネットワークを
捏造するリスクがある)ため、そこには踏み込まず、任意の2地点間の直線距離・方位という
純粋に幾何学的な事実だけを使う。風向と正しく整合するペアだけが実質的な重みを持つため、
結果的に川筋に沿った地点同士が自然に強く効くはずだが、それは学習結果からそうなる
だけであり、あらかじめ「どれが上流か」を決め打ちしているわけではない。
"""
import logging
import math

import numpy as np
import pandas as pd

from fog_features import SUMMARY_FOG_CODES

log = logging.getLogger("fog_advection")

EARTH_RADIUS_KM = 6371.0088
ADVECTION_FOG_FEATURE = "移流川霧指数"

# 距離/風速から逆算した到達予定時刻が、これを超える先(または風がほぼ無風)なら
# 「その風でその距離を近い将来に運ばれてくる」とは見なさず、寄与させない。
ADVECTION_MAX_LAG_HOURS = 6
ADVECTION_MIN_WIND_SPEED = 0.3  # m/s。無風に近いと風向自体の信頼性が低いため除外する。
# 風向のズレ(度)に対する重みの落ち方の鋭さ。cos(ズレ)の指数。大きいほど
# 「風向がほぼ正対したペアだけ」を重視するようになる。
ADVECTION_ALIGNMENT_POWER = 2.0


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _bearing_deg(lat1, lon1, lat2, lon2):
    """地点1から見た地点2への真方位角(北0°、東90°、時計回り)。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def compute_advection_fog_index(merged, phenom_cols, camera_coords, wind_dir_col, wind_speed_col,
                                 station_wind=None):
    """自地点Tについて、風上側にある他地点Sの「今の風速でならちょうど今頃自地点に
    届いているはずの時刻」の実況川霧有無を、風向の一致度で重み付けして平均する。

    merged: datetime, phenom_cols(現象コード生値), wind_dir_col, wind_speed_col を含むDataFrame。
    camera_coords: {地点名: (緯度, 経度)}(cameras.csvの実測値。座標の無い地点はスキップ)。
    station_wind: {地点名: (wind_dir配列, wind_speed配列)}(mergedと同じ行数・順序)を渡すと、
    その地点が移流の「届き先」(T)になる計算では、共通の代表地点の風(wind_dir_col/
    wind_speed_col)の代わりにその地点自身の実測風向・風速を使う(この特徴量は風向・風速が
    本質のため、代表地点1つの風より地点固有の実測風の方が物理的に正確なはず、と
    2026-09-13の監査で指摘された)。station_windに無い地点、station_wind自体が未指定の
    場合は従来通り共通の風にフォールバックする(プーリング救済モデルは全地点で共通の
    気象値を前提に設計されているため、そちらの経路では意図的に渡さない)。
    戻り値: {地点名: pd.Series}(mergedと同じindexのSeries。寄与するペアが無い行はNaN)。
    """
    station_wind = station_wind or {}
    stations_with_coords = [c for c in phenom_cols if c in camera_coords]
    if len(stations_with_coords) < 2:
        log.info("Advection fog index: not enough stations with coordinates (%d)", len(stations_with_coords))
        return {col: pd.Series(np.nan, index=merged.index) for col in phenom_cols}

    codes = merged[phenom_cols]
    is_fog = codes.isin(SUMMARY_FOG_CODES)
    valid = codes.notna()

    wind_dir = pd.to_numeric(merged[wind_dir_col], errors="coerce").to_numpy(dtype="float64")
    wind_speed = pd.to_numeric(merged[wind_speed_col], errors="coerce").to_numpy(dtype="float64")
    dt_values = pd.DatetimeIndex(merged["datetime"])

    # ペアごとの距離・方位は時刻に依存しないので先に1回だけ計算する。
    geo = {}
    for t in stations_with_coords:
        t_lat, t_lon = camera_coords[t]
        for s in stations_with_coords:
            if s == t:
                continue
            s_lat, s_lon = camera_coords[s]
            geo[(t, s)] = (
                _haversine_km(t_lat, t_lon, s_lat, s_lon),
                _bearing_deg(t_lat, t_lon, s_lat, s_lon),  # Tから見たSの方位＝Sから風が吹いてくる向き
            )

    with np.errstate(divide="ignore", invalid="ignore"):
        result = {}
        for t in phenom_cols:
            if t not in stations_with_coords:
                result[t] = pd.Series(np.nan, index=merged.index)
                continue

            t_wind_dir, t_wind_speed = station_wind.get(t, (wind_dir, wind_speed))

            weighted_sum = np.zeros(len(merged), dtype="float64")
            weight_total = np.zeros(len(merged), dtype="float64")
            for s in stations_with_coords:
                if s == t:
                    continue
                distance_km, bearing_from_t = geo[(t, s)]
                angle_diff = np.abs(((t_wind_dir - bearing_from_t + 180.0) % 360.0) - 180.0)
                alignment = np.clip(np.cos(np.radians(angle_diff)), 0.0, None) ** ADVECTION_ALIGNMENT_POWER

                travel_hours = distance_km / (t_wind_speed * 3.6)
                lag_hours = np.round(travel_hours)
                valid_lag = (
                    np.isfinite(t_wind_dir)
                    & (t_wind_speed >= ADVECTION_MIN_WIND_SPEED)
                    & np.isfinite(lag_hours)
                    & (lag_hours >= 0)
                    & (lag_hours <= ADVECTION_MAX_LAG_HOURS)
                )
                weight = np.nan_to_num(np.where(valid_lag, alignment, 0.0), nan=0.0)
                if not np.any(weight > 0):
                    continue

                source = pd.DataFrame({
                    "datetime": dt_values,
                    "_is_fog": is_fog[s].astype(float).to_numpy(),
                    "_valid": valid[s].astype(float).to_numpy(),
                }).drop_duplicates(subset="datetime").set_index("datetime")
                lookup = dt_values - pd.to_timedelta(np.where(np.isfinite(lag_hours), lag_hours, 0.0), unit="h")
                fog_at_lag = source["_is_fog"].reindex(lookup).to_numpy()
                valid_at_lag = source["_valid"].reindex(lookup).to_numpy()

                row_weight = np.where(np.nan_to_num(valid_at_lag, nan=0.0) >= 0.5, weight, 0.0)
                weighted_sum += np.nan_to_num(fog_at_lag, nan=0.0) * row_weight
                weight_total += row_weight

            idx = np.where(weight_total > 0, weighted_sum / np.where(weight_total > 0, weight_total, 1.0), np.nan)
            result[t] = pd.Series(idx, index=merged.index)
    return result
