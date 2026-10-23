"""雨の判定(2026-10-23)。

川霧と違い雨は一般の天気予報で十分予測できるため、このアプリ独自のモデルでは学習・予測の
対象にしない(fog_model.py・fog_features.target_codesは元々コード9・10=霧雨・雨を含まない)。
公開ページ等の表示では、代わりにここで「いま」はアメダスの実測、「これから」はOpen-Meteoの
予報時刻ごとの降水量を使って、地点ごとに雨かどうかを判定する。
"""
import logging
import time
from datetime import timedelta

import amedas
import collect
import common

log = logging.getLogger("rain_status")

RAIN_PRECIP_THRESHOLD_MM = 0.5     # 1時間降水量がこれ以上なら「雨」とみなす(微量記録のノイズを除く)
AMEDAS_CURRENT_MAX_AGE_HOURS = 3   # 「いま」判定に使うアメダス実測の鮮度基準(amedas_forと同じ)
FORECAST_DAYS_MAX = 3              # PUBLIC_DAYS_AHEAD(webapp.py)と同じ日数まで取得すれば足りる


def current_rain(station_name, camera_info):
    """アメダス実測から、いま雨が降っているか。直近{AMEDAS_CURRENT_MAX_AGE_HOURS}時間以内の
    実測が無ければ判定不可としてNoneを返す(「雨ではない」と「わからない」を区別する)。"""
    info = camera_info.get(station_name, {})
    if info.get("lat") is None:
        return None
    try:
        st, _km, _alt = amedas.nearest(info["lat"], info["lon"])
        obs = amedas.latest_for(st)
    except Exception as e:
        log.warning("AMeDAS lookup failed for %s: %s", station_name, e)
        return None
    if not obs:
        return None
    if obs["datetime"] < (common.now_jst_naive() - timedelta(hours=AMEDAS_CURRENT_MAX_AGE_HOURS)).strftime(
            "%Y-%m-%d %H:%M:%S"):
        return None
    precip = obs.get("precip")
    if precip is None:
        return None
    return precip >= RAIN_PRECIP_THRESHOLD_MM


def forecast_rain_hours(coords_by_name, days_ahead=FORECAST_DAYS_MAX):
    """Open-Meteoの地点ごとの予報降水量(1回のAPI呼び出しでまとめて取得)から、
    {地点名: {"YYYY-MM-DD HH": 雨かどうか}}を返す。取得失敗時は空dict
    (呼び出し側は「予報の雨情報なし」として、雨の表示を省くだけで予測自体は止めない)。"""
    if not coords_by_name:
        return {}
    names = list(coords_by_name.keys())
    params = {
        "latitude": ",".join(str(coords_by_name[n][0]) for n in names),
        "longitude": ",".join(str(coords_by_name[n][1]) for n in names),
        "hourly": "precipitation",
        "timezone": "Asia/Tokyo",
        "past_days": 0,
        "forecast_days": days_ahead,
    }
    attempts = collect.WEATHER_FETCH_RETRIES + 1
    payload, last_exc = None, None
    for attempt in range(1, attempts + 1):
        try:
            resp = common.request_with_dns_fallback("GET", collect.OPEN_METEO_URL, params=params, timeout=20, log=log)
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception as e:
            last_exc = e
            log.warning("Rain forecast fetch failed (attempt %d/%d): %s", attempt, attempts, e)
            if attempt < attempts:
                time.sleep(collect.WEATHER_RETRY_DELAY_SEC)
    if payload is None:
        log.error("Rain forecast fetch failed after %d attempts", attempts)
        if last_exc is not None:
            common.record_network_failure("rain_forecast_fetch", str(last_exc))
        return {}
    if isinstance(payload, dict):
        payload = [payload]  # 地点が1件だけの時は単一オブジェクトで返ってくるため揃える(fetch_weather_multiと同じ)

    result = {}
    for name, item in zip(names, payload):
        hourly = (item or {}).get("hourly") or {}
        times, precs = hourly.get("time", []), hourly.get("precipitation", [])
        hours = {}
        for t, p in zip(times, precs):
            if p is None:
                continue
            hours[t.replace("T", " ")[:13]] = p >= RAIN_PRECIP_THRESHOLD_MM
        if hours:
            result[name] = hours
    return result


__all__ = ["current_rain", "forecast_rain_hours", "RAIN_PRECIP_THRESHOLD_MM"]
