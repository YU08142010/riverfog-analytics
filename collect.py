"""只見川流域32地点の川霧観測・自動収集（毎時実行）。

画像取得 -> Gemini分類 -> Open-Meteo気象取得 -> xlsx追記 を1回分行う。
サーバー1単独運用。データは /opt/river-fog/data にローカル保存し、Samba共有で公開する。
"""
import csv
import logging
import logging.handlers
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import openpyxl
import requests
from dotenv import load_dotenv
from PIL import Image
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
CAMERAS_CSV = BASE_DIR / "cameras.csv"
TEMPLATE_XLSX = BASE_DIR / "template" / "header_template.xlsx"
DATA_XLSX = BASE_DIR / "data" / "只見川.xlsx"
LOG_FILE = BASE_DIR / "logs" / "collect.log"

CAM_IMAGE_URL = "https://cam.river.go.jp/cam/now/{cam_id}.jpg"
CAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
FETCH_TIMEOUT = 10
FETCH_WORKERS = 8
FETCH_RETRIES = 2
FETCH_RETRY_DELAY = 1.5

GEMINI_TIMEOUT_MS = 30_000
GEMINI_RETRY_PASSES = 2
GEMINI_RETRY_DELAY_SEC = 5

WEATHER_FETCH_RETRIES = 2
WEATHER_RETRY_DELAY_SEC = 2

IMAGE_ARCHIVE_DIR = BASE_DIR / "data" / "images"
IMAGE_RETENTION_DAYS = 7

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# 夜間は画像が見えず分類結果が不正確になるため、観測は日中のみ行う
OBSERVATION_START_HOUR = 4
OBSERVATION_END_HOUR = 19

RESIZE_MAX_SIDE = 768
JPEG_QUALITY = 80

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

QUALITY_INFO_VALUE = 8
HOMOGENEITY_NUMBER_VALUE = 1

PHENOM_LABELS = {
    1: "薄い川霧",
    2: "川霧",
    3: "濃い川霧",
    4: "薄い全体霧",
    5: "全体霧",
    6: "全体濃い霧",
    7: "薄い層雲",
    8: "濃い層雲",
    9: "霧雨",
    10: "雨",
}

WIND_DIR_DEGREES = {
    "北": 0.0, "北北東": 22.5, "北東": 45.0, "東北東": 67.5,
    "東": 90.0, "東南東": 112.5, "南東": 135.0, "南南東": 157.5,
    "南": 180.0, "南南西": 202.5, "南西": 225.0, "西南西": 247.5,
    "西": 270.0, "西北西": 292.5, "北西": 315.0, "北北西": 337.5,
}
DEGREES_TO_WIND_DIR = {v: k for k, v in WIND_DIR_DEGREES.items()}

# xlsx列レイアウト（1始まり）。test.xlsxの本番形式に合わせている。
COL_DATETIME = 1
COL_TEMP = 2
COL_TEMP_QI = 3
COL_TEMP_HN = 4
COL_PRECIP = 5
COL_PRECIP_QI = 6
COL_PRECIP_HN = 7
COL_WIND_SPEED = 8
COL_WIND_SPEED_QI = 9
COL_WIND_DIR = 10
COL_WIND_DIR_QI = 11
COL_WIND_SPEED_HN = 12
COL_SOLAR = 13
COL_SOLAR_QI = 14
COL_SOLAR_HN = 15
COL_SUNSHINE = 16
COL_SUNSHINE_QI = 17
COL_SUNSHINE_HN = 18
COL_DEWPOINT = 19
COL_DEWPOINT_QI = 20
COL_DEWPOINT_HN = 21
COL_HUMIDITY = 22
COL_HUMIDITY_QI = 23
COL_HUMIDITY_HN = 24
COL_VISIBILITY = 25
COL_VISIBILITY_QI = 26
COL_VISIBILITY_HN = 27
# 28列目はスペーサー（空列）
COL_PHENOM_START = 29

JST = timezone(timedelta(hours=9))

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
log = logging.getLogger("collect")
# google-genai SDKが毎回出す「AFC非推奨」の情報的な警告を抑制する（disable済みのため実害なし）
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


class StationClassification(BaseModel):
    station_index: int
    code: int


def load_cameras():
    with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [(row["地点名"], row["カメラID"]) for row in reader]


def fetch_one_image(name, cam_id):
    url = CAM_IMAGE_URL.format(cam_id=cam_id)
    attempts = FETCH_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers=CAM_HEADERS, timeout=FETCH_TIMEOUT)
            content_type = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and resp.content and content_type.startswith("image/jpeg"):
                return name, cam_id, resp.content
            log.warning(
                "Image fetch failed (HTTP %s, Content-Type %s, attempt %d/%d): %s (%s)",
                resp.status_code, content_type, attempt, attempts, name, cam_id,
            )
        except requests.RequestException as e:
            log.warning(
                "Image fetch failed (attempt %d/%d): %s (%s): %s",
                attempt, attempts, name, cam_id, e,
            )
        if attempt < attempts:
            time.sleep(FETCH_RETRY_DELAY)
    return name, cam_id, None


def fetch_all_images(cameras):
    """cameras の順序を保ったまま、地点名 -> 画像bytes(取得失敗はNone) の辞書を返す。"""
    results = {}
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
        futures = {
            executor.submit(fetch_one_image, name, cam_id): name for name, cam_id in cameras
        }
        for future in as_completed(futures):
            name, cam_id, content = future.result()
            results[name] = content
    return results


def resize_image(raw_bytes):
    img = Image.open(BytesIO(raw_bytes))
    img = img.convert("RGB")
    w, h = img.size
    scale = RESIZE_MAX_SIDE / max(w, h)
    if scale < 1:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def save_images(cameras, images_by_name, run_time):
    """取得画像を後日の検証・再分類用にアーカイブ保存する（縮小版、直近IMAGE_RETENTION_DAYS日分のみ保持）。"""
    day_dir = IMAGE_ARCHIVE_DIR / run_time.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_time.strftime("%H%M")
    for name, _cam_id in cameras:
        raw_bytes = images_by_name.get(name)
        if not raw_bytes:
            continue
        try:
            resized = resize_image(raw_bytes)
        except Exception as e:
            log.warning("Image archive resize failed: %s: %s", name, e)
            continue
        (day_dir / f"{stamp}_{name}.jpg").write_bytes(resized)


def prune_old_images():
    """保存期間を過ぎた画像アーカイブを削除する。"""
    if not IMAGE_ARCHIVE_DIR.exists():
        return
    cutoff = datetime.now(JST).date() - timedelta(days=IMAGE_RETENTION_DAYS)
    for day_dir in IMAGE_ARCHIVE_DIR.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(day_dir)
            log.info("Pruned old image archive: %s", day_dir.name)


def get_api_keys():
    """GEMINI_API_KEYS（カンマ区切り）を優先し、無ければ単一のGEMINI_API_KEYにフォールバックする。"""
    keys_str = os.environ.get("GEMINI_API_KEYS", "")
    keys = [k.strip() for k in keys_str.split(",") if k.strip()]
    if keys:
        return keys
    single = os.environ.get("GEMINI_API_KEY", "").strip()
    return [single] if single else []


def classify_images(cameras, images_by_name):
    """Gemini APIで一括分類。地点名 -> code(0-10) の辞書を返す（欠測は含めない）。"""
    from google import genai
    from google.genai import types

    valid = [(name, images_by_name[name]) for name, _ in cameras if images_by_name.get(name)]
    if not valid:
        return {}

    api_keys = get_api_keys()
    if not api_keys:
        log.warning("GEMINI_API_KEY is not set, skipping Gemini classification (all stations missing)")
        return {}

    contents = []
    index_to_name = {}
    for i, (name, raw_bytes) in enumerate(valid, start=1):
        try:
            resized = resize_image(raw_bytes)
        except Exception as e:
            log.warning("Image resize failed: %s: %s", name, e)
            continue
        contents.append(f"地点{i}: {name}")
        contents.append(types.Part.from_bytes(data=resized, mime_type="image/jpeg"))
        index_to_name[i] = name

    if not contents:
        return {}

    system_instruction = (
        "あなたは河川監視カメラの画像から気象現象を分類する専門家です。\n"
        "各画像を、以下の現象コード表に従って0〜10の整数のいずれか1つに分類してください。\n"
        "0: 該当する現象なし（霧・層雲・霧雨・雨のいずれも見られない）\n"
        "1: 薄い川霧\n2: 川霧\n3: 濃い川霧\n"
        "4: 薄い全体霧\n5: 全体霧\n6: 全体濃い霧\n"
        "7: 薄い層雲\n8: 濃い層雲\n9: 霧雨\n10: 雨\n"
        "各画像の直前に「地点N: 地点名」というラベルがテキストで付与されています。"
        "station_index には、このラベルのNの数字をそのまま入れてください。"
        "画像とラベルの対応を取り違えないよう、慎重に対応させてください。"
    )

    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

    def try_all_keys():
        for key_idx, api_key in enumerate(api_keys, start=1):
            try:
                client = genai.Client(api_key=api_key)
                resp = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                        response_schema=list[StationClassification],
                        temperature=0,
                        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    ),
                )
            except Exception as e:
                log.warning("Gemini API call failed (key %d/%d): %s", key_idx, len(api_keys), e)
                continue
            if key_idx > 1:
                log.info("Main API key unavailable, fell back to sub key %d", key_idx)
            return resp
        return None

    response = None
    for pass_idx in range(1, GEMINI_RETRY_PASSES + 1):
        response = try_all_keys()
        if response is not None:
            break
        if pass_idx < GEMINI_RETRY_PASSES:
            log.warning(
                "All Gemini API keys failed (pass %d/%d), retrying in %ds",
                pass_idx, GEMINI_RETRY_PASSES, GEMINI_RETRY_DELAY_SEC,
            )
            time.sleep(GEMINI_RETRY_DELAY_SEC)

    if response is None:
        log.error("Gemini API call failed with all configured keys")
        return {}

    try:
        parsed: list[StationClassification] = response.parsed
        if parsed is None:
            raise ValueError("response.parsed is None")
    except Exception as e:
        log.error("Failed to parse Gemini response: %s / raw=%r", e, getattr(response, "text", None))
        return {}

    result = {}
    for item in parsed:
        name = index_to_name.get(item.station_index)
        if name is None:
            log.warning("Unknown station_index in Gemini response: %s", item.station_index)
            continue
        if not (0 <= item.code <= 10):
            log.warning("Phenomenon code out of range in Gemini response: %s (%s)", item.code, name)
            continue
        result[name] = item.code
    return result


def wind_degrees_to_direction_ja(degrees):
    if degrees is None:
        return None
    nearest = min(WIND_DIR_DEGREES.values(), key=lambda d: min(abs(d - degrees), 360 - abs(d - degrees)))
    return DEGREES_TO_WIND_DIR[nearest]


def fetch_weather():
    """代表地点（只見町役場）のOpen-Meteo実況に近い直近時刻の値を取得する。取得失敗時はNoneを返す。"""
    lat = os.environ.get("WEATHER_LAT", "37.34861")
    lon = os.environ.get("WEATHER_LON", "139.31583")
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(
            [
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
        ),
        "wind_speed_unit": "ms",
        "timezone": "Asia/Tokyo",
        "past_days": 1,
        "forecast_days": 1,
    }
    attempts = WEATHER_FETCH_RETRIES + 1
    data = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(OPEN_METEO_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException as e:
            log.warning(
                "Weather fetch failed (attempt %d/%d): %s", attempt, attempts, e,
            )
            if attempt < attempts:
                time.sleep(WEATHER_RETRY_DELAY_SEC)
    if data is None:
        log.error("Open-Meteo fetch failed after %d attempts", attempts)
        return None

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        log.error("Open-Meteo response has no time field")
        return None

    now_jst = datetime.now(JST).replace(minute=0, second=0, microsecond=0)
    target = now_jst.strftime("%Y-%m-%dT%H:00")
    try:
        idx = times.index(target)
    except ValueError:
        # ぴったりの時刻が無ければ直近の過去時刻を使う
        past = [t for t in times if t <= target]
        if not past:
            log.error("No matching time in Open-Meteo data: %s", target)
            return None
        idx = times.index(past[-1])

    def at(key):
        vals = hourly.get(key, [])
        return vals[idx] if idx < len(vals) else None

    solar_wm2 = at("shortwave_radiation")
    sunshine_sec = at("sunshine_duration")
    visibility_m = at("visibility")

    return {
        "temperature": at("temperature_2m"),
        "precipitation": at("precipitation"),
        "wind_speed": at("wind_speed_10m"),
        "wind_direction_deg": at("wind_direction_10m"),
        "solar_radiation_mj": None if solar_wm2 is None else round(solar_wm2 * 0.0036, 3),
        "sunshine_hours": None if sunshine_sec is None else round(sunshine_sec / 3600, 3),
        "dew_point": at("dew_point_2m"),
        "humidity": at("relative_humidity_2m"),
        "visibility_km": None if visibility_m is None else round(visibility_m / 1000, 2),
    }


def ensure_workbook():
    DATA_XLSX.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_XLSX.exists():
        shutil.copy(TEMPLATE_XLSX, DATA_XLSX)
        log.info("Created new workbook from template: %s", DATA_XLSX)


def append_row(cameras, phenom_by_name, weather, run_time):
    ensure_workbook()
    wb = openpyxl.load_workbook(DATA_XLSX)
    ws = wb.active
    row = ws.max_row + 1
    # Excelはtzinfoつきのdatetimeを書き込めないため、ローカル時刻の値のまま外す
    run_time = run_time.replace(tzinfo=None)

    def set_measure(col_value, col_qi, col_hn, value):
        if value is None:
            ws.cell(row=row, column=col_value).value = None
            ws.cell(row=row, column=col_qi).value = None
            ws.cell(row=row, column=col_hn).value = None
        else:
            ws.cell(row=row, column=col_value).value = value
            ws.cell(row=row, column=col_qi).value = QUALITY_INFO_VALUE
            ws.cell(row=row, column=col_hn).value = HOMOGENEITY_NUMBER_VALUE

    ws.cell(row=row, column=COL_DATETIME).value = run_time

    weather = weather or {}
    set_measure(COL_TEMP, COL_TEMP_QI, COL_TEMP_HN, weather.get("temperature"))
    set_measure(COL_PRECIP, COL_PRECIP_QI, COL_PRECIP_HN, weather.get("precipitation"))

    wind_speed = weather.get("wind_speed")
    if wind_speed is None:
        ws.cell(row=row, column=COL_WIND_SPEED).value = None
        ws.cell(row=row, column=COL_WIND_SPEED_QI).value = None
        ws.cell(row=row, column=COL_WIND_SPEED_HN).value = None
    else:
        ws.cell(row=row, column=COL_WIND_SPEED).value = wind_speed
        ws.cell(row=row, column=COL_WIND_SPEED_QI).value = QUALITY_INFO_VALUE
        ws.cell(row=row, column=COL_WIND_SPEED_HN).value = HOMOGENEITY_NUMBER_VALUE

    wind_dir_ja = wind_degrees_to_direction_ja(weather.get("wind_direction_deg"))
    if wind_dir_ja is None:
        ws.cell(row=row, column=COL_WIND_DIR).value = None
        ws.cell(row=row, column=COL_WIND_DIR_QI).value = None
    else:
        ws.cell(row=row, column=COL_WIND_DIR).value = wind_dir_ja
        ws.cell(row=row, column=COL_WIND_DIR_QI).value = QUALITY_INFO_VALUE

    set_measure(COL_SOLAR, COL_SOLAR_QI, COL_SOLAR_HN, weather.get("solar_radiation_mj"))
    set_measure(COL_SUNSHINE, COL_SUNSHINE_QI, COL_SUNSHINE_HN, weather.get("sunshine_hours"))
    set_measure(COL_DEWPOINT, COL_DEWPOINT_QI, COL_DEWPOINT_HN, weather.get("dew_point"))
    set_measure(COL_HUMIDITY, COL_HUMIDITY_QI, COL_HUMIDITY_HN, weather.get("humidity"))
    set_measure(COL_VISIBILITY, COL_VISIBILITY_QI, COL_VISIBILITY_HN, weather.get("visibility_km"))

    for i, (name, _cam_id) in enumerate(cameras):
        col = COL_PHENOM_START + i
        code = phenom_by_name.get(name)
        if code is None:
            ws.cell(row=row, column=col).value = None
        elif code == 0:
            ws.cell(row=row, column=col).value = "/"
        else:
            ws.cell(row=row, column=col).value = code

    wb.save(DATA_XLSX)
    log.info("Appended row to xlsx: row %d (%s)", row, run_time)


def main():
    load_dotenv(BASE_DIR / ".env")
    run_time = datetime.now(JST).replace(minute=0, second=0, microsecond=0)

    if not (OBSERVATION_START_HOUR <= run_time.hour <= OBSERVATION_END_HOUR):
        log.info(
            "Outside observation window (%d:00-%d:00), skipping: %s",
            OBSERVATION_START_HOUR, OBSERVATION_END_HOUR, run_time,
        )
        return

    log.info("=== Collection started: %s ===", run_time)

    cameras = load_cameras()
    log.info("Target stations: %d", len(cameras))

    images_by_name = fetch_all_images(cameras)
    ok_count = sum(1 for v in images_by_name.values() if v)
    log.info("Image fetch succeeded: %d / %d", ok_count, len(cameras))

    save_images(cameras, images_by_name, run_time)
    prune_old_images()

    phenom_by_name = classify_images(cameras, images_by_name)
    log.info("Gemini classification succeeded: %d / %d", len(phenom_by_name), len(cameras))

    weather = fetch_weather()
    if weather is None:
        log.warning("Failed to fetch weather data (all fields treated as missing)")

    append_row(cameras, phenom_by_name, weather, run_time)
    log.info("=== Collection finished ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("collect.py terminated abnormally")
        sys.exit(1)
