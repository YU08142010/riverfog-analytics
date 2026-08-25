"""欠測している気象データ行を、後から Open-Meteo の実況値で埋め直すバックフィルスクリプト。

collect.py の毎時収集は、DNS障害等でその回の気象取得（Open-Meteo）だけ失敗することがある
（画像取得・Gemini分類は成功しているのに気象値だけ欠測、というケース）。
本スクリプトは xlsx を走査して気温列が空の行を「気象データ欠測」とみなし、
1. まず Archive API（ERA5実況値、信頼性が高いが直近数日分は未反映のことがある）
2. 反映されていなければ Forecast API の過去分（past_days、直近分もほぼ即時反映）
の順で該当時刻の値を取得し、埋められた行だけ更新して保存する。

collect.py と同じ時間帯に同時実行されると xlsx の書き込みが競合するため、
cron 等で定期実行する場合は毎時収集(04:00-19:00台の00分)を避けたタイミングにすること。
"""
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime

import openpyxl
import requests
from dotenv import load_dotenv

from common import (
    BASE_DIR,
    COL_DATETIME,
    COL_TEMP,
    DATA_XLSX,
    JST,
    set_weather_measures,
)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_FIELDS = [
    "temperature_2m",
    "precipitation",
    "wind_speed_10m",
    "wind_direction_10m",
    "shortwave_radiation",
    "sunshine_duration",
    "dew_point_2m",
    "relative_humidity_2m",
    "visibility",
]

REQUEST_TIMEOUT = 20
REQUEST_RETRIES = 2
REQUEST_RETRY_DELAY_SEC = 2
BETWEEN_ROW_DELAY_SEC = 0.5

LOG_FILE = BASE_DIR / "logs" / "backfill_weather.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("backfill_weather")


def query_open_meteo(url, params):
    attempts = REQUEST_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.warning("Open-Meteo request failed (%s, attempt %d/%d): %s", url, attempt, attempts, e)
            if attempt < attempts:
                time.sleep(REQUEST_RETRY_DELAY_SEC)
    return None


def extract_hourly(data, target_str):
    """data の hourly から target_str（"YYYY-MM-DDTHH:00"）時刻の値一式を取り出す。無ければNone。"""
    if not data:
        return None
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if target_str not in times:
        return None
    idx = times.index(target_str)

    def at(key):
        vals = hourly.get(key, [])
        return vals[idx] if idx < len(vals) else None

    return {key: at(key) for key in HOURLY_FIELDS}


def raw_to_weather(raw):
    solar_wm2 = raw.get("shortwave_radiation")
    sunshine_sec = raw.get("sunshine_duration")
    visibility_m = raw.get("visibility")
    return {
        "temperature": raw.get("temperature_2m"),
        "precipitation": raw.get("precipitation"),
        "wind_speed": raw.get("wind_speed_10m"),
        "wind_direction_deg": raw.get("wind_direction_10m"),
        "solar_radiation_mj": None if solar_wm2 is None else round(solar_wm2 * 0.0036, 3),
        "sunshine_hours": None if sunshine_sec is None else round(sunshine_sec / 3600, 3),
        "dew_point": raw.get("dew_point_2m"),
        "humidity": raw.get("relative_humidity_2m"),
        "visibility_km": None if visibility_m is None else round(visibility_m / 1000, 2),
    }


def fetch_weather_for(dt_naive, lat, lon):
    """指定時刻の気象値を取得する。取得できなければ (None, None) を返す。
    戻り値の2要素目はどのAPIで埋まったか("archive" / "forecast")のログ用ラベル。
    """
    target_str = dt_naive.strftime("%Y-%m-%dT%H:00")
    date_str = dt_naive.strftime("%Y-%m-%d")

    archive_params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_str,
        "end_date": date_str,
        "hourly": ",".join(HOURLY_FIELDS),
        "wind_speed_unit": "ms",
        "timezone": "Asia/Tokyo",
    }
    raw = extract_hourly(query_open_meteo(ARCHIVE_URL, archive_params), target_str)
    source = "archive"

    if raw is None or raw.get("temperature_2m") is None:
        days_ago = (datetime.now(JST).date() - dt_naive.date()).days
        forecast_params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(HOURLY_FIELDS),
            "wind_speed_unit": "ms",
            "timezone": "Asia/Tokyo",
            "past_days": max(1, min(days_ago + 1, 92)),
            "forecast_days": 1,
        }
        raw2 = extract_hourly(query_open_meteo(FORECAST_URL, forecast_params), target_str)
        if raw2 is not None and raw2.get("temperature_2m") is not None:
            raw = raw2
            source = "forecast(past_days)"

    if raw is None or raw.get("temperature_2m") is None:
        return None, None

    return raw_to_weather(raw), source


def find_missing_rows(ws):
    """気温列が空の行を「気象データ欠測」とみなして (行番号, 日時) のリストを返す。"""
    missing = []
    for row in range(2, ws.max_row + 1):
        dt = ws.cell(row=row, column=COL_DATETIME).value
        if dt is None:
            continue
        if ws.cell(row=row, column=COL_TEMP).value is None:
            missing.append((row, dt))
    return missing


def main():
    load_dotenv(BASE_DIR / ".env")
    dry_run = "--dry-run" in sys.argv[1:]

    lat = os.environ.get("WEATHER_LAT", "37.34861")
    lon = os.environ.get("WEATHER_LON", "139.31583")

    if not DATA_XLSX.exists():
        log.info("xlsx not found, nothing to backfill: %s", DATA_XLSX)
        return

    log.info("=== Backfill started%s ===", " (dry-run)" if dry_run else "")

    wb = openpyxl.load_workbook(DATA_XLSX)
    ws = wb.active

    missing = find_missing_rows(ws)
    if not missing:
        log.info("No missing weather rows found (%d data row(s) checked)", ws.max_row - 1)
        log.info("=== Backfill finished: 0/0 filled ===")
        return

    log.info("Found %d row(s) with missing weather data", len(missing))

    filled = 0
    for row, dt in missing:
        weather, source = fetch_weather_for(dt, lat, lon)
        if weather is None:
            log.info("Still unavailable, skipped: row %d (%s)", row, dt)
        else:
            if not dry_run:
                set_weather_measures(ws, row, weather)
            filled += 1
            log.info("Backfilled: row %d (%s) via %s%s", row, dt, source, " [dry-run]" if dry_run else "")
        time.sleep(BETWEEN_ROW_DELAY_SEC)

    if filled and not dry_run:
        wb.save(DATA_XLSX)
        log.info("Saved xlsx with %d row(s) backfilled", filled)

    log.info("=== Backfill finished: %d/%d filled ===", filled, len(missing))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("backfill_weather.py terminated abnormally")
        sys.exit(1)
