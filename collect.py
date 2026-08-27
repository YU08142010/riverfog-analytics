"""只見川流域32地点の川霧観測・自動収集（毎時実行）。

画像取得 -> Gemini分類（現象コード + 画像自体の有効性判定） -> Open-Meteo気象取得 -> xlsx追記 を1回分行う。
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
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Literal

import openpyxl
import requests
from dotenv import load_dotenv
from PIL import Image
from pydantic import BaseModel

from common import (
    BASE_DIR,
    CAMERAS_CSV,
    COL_DATETIME,
    COL_PHENOM_START,
    DATA_XLSX,
    JST,
    TEMPLATE_XLSX,
    set_weather_measures,
)

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

# cam.river.go.jp側の混雑等で取得自体に失敗した地点用のプレースホルダー（無地グレー画像）。
# アーカイブに何も残さないと「その時刻だけ地点が丸ごと消える」ように見えてしまうため、
# Geminiが無効画像と判定した場合(_unavailable)と同じ命名・扱いにして残す。
FETCH_FAILED_PLACEHOLDER_SIZE = (320, 240)
FETCH_FAILED_PLACEHOLDER_COLOR = (128, 128, 128)

LOG_FILE = BASE_DIR / "logs" / "collect.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# 夜間は画像が見えず分類結果が不正確になるため、観測は日中のみ行う
OBSERVATION_START_HOUR = 4
OBSERVATION_END_HOUR = 19

# この割合以上の地点で使える写真が無かった場合、単発の失敗ではなくカメラサイト
# (cam.river.go.jp)側の障害等の可能性が高いとみなしERRORで警告する（/admin/errorsに出る）。
IMAGE_FAILURE_ALERT_RATIO = 0.3

RESIZE_MAX_SIDE = 768
JPEG_QUALITY = 80

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# カメラ画像が現象分類の対象にならない（＝「取得できませんでした」等のプレースホルダー、
# 真っ黒/真っ白、テキストのみ等の非写真、またはレンズの水滴・泥汚れで背景の風景がほとんど
# 視認できない状態）とGeminiが判定したことを表すコード。
CODE_INVALID_IMAGE = -1

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
    confidence: Literal["high", "medium", "low"]


def load_cameras():
    with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [(row["地点名"], row["カメラID"]) for row in reader]


def fetch_one_image(name, cam_id):
    """画像を取得する。中身が本物の写真かプレースホルダーかはここでは判定しない（Gemini側で行う）。"""
    url = CAM_IMAGE_URL.format(cam_id=cam_id)
    attempts = FETCH_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers=CAM_HEADERS, timeout=FETCH_TIMEOUT)
            if resp.status_code == 200 and resp.content:
                return name, cam_id, resp.content
            log.warning(
                "Image fetch failed (HTTP %s, attempt %d/%d): %s (%s)",
                resp.status_code, attempt, attempts, name, cam_id,
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


def _fetch_failed_placeholder_bytes():
    img = Image.new("RGB", FETCH_FAILED_PLACEHOLDER_SIZE, color=FETCH_FAILED_PLACEHOLDER_COLOR)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def save_images(cameras, images_by_name, invalid_stations, run_time):
    """取得画像を後日の検証・再分類用にアーカイブ保存する（縮小版、直近IMAGE_RETENTION_DAYS日分のみ保持）。
    date/HHMM/地点名.jpg という時刻ごとのフォルダに分けて保存する（1フォルダに全時刻の画像が
    混在すると見づらいため）。Geminiが「カメラ画像が取得できませんでした」等のプレースホルダーと
    判定した地点、およびカメラサイト側の混雑等で取得自体に失敗した地点は、ファイル名に
    _unavailable を付けたグレーのプレースホルダー画像として保存する
    （何も残さないと、その時刻だけ地点が丸ごと消えたように見えてしまうため）。
    """
    hour_dir = IMAGE_ARCHIVE_DIR / run_time.strftime("%Y-%m-%d") / run_time.strftime("%H%M")
    hour_dir.mkdir(parents=True, exist_ok=True)
    for name, _cam_id in cameras:
        raw_bytes = images_by_name.get(name)
        if not raw_bytes:
            (hour_dir / f"{name}_unavailable.jpg").write_bytes(_fetch_failed_placeholder_bytes())
            continue
        try:
            resized = resize_image(raw_bytes)
        except Exception as e:
            log.warning("Image archive resize failed: %s: %s", name, e)
            (hour_dir / f"{name}_unavailable.jpg").write_bytes(_fetch_failed_placeholder_bytes())
            continue
        suffix = "_unavailable" if name in invalid_stations else ""
        (hour_dir / f"{name}{suffix}.jpg").write_bytes(resized)


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


SYSTEM_INSTRUCTION = (
    "あなたは河川監視カメラの画像から気象現象を分類する専門家です。\n"
    "各画像を、以下の現象コード表に従って-1〜10の整数のいずれか1つに分類してください。\n"
    "-1: この画像は実際のカメラ写真ではない（例：「カメラ画像が取得できませんでした」等の"
    "エラーメッセージ画面、単色の塗りつぶし、システムのロゴやUI画面など、河川や周辺の風景が"
    "写っていないもの）\n"
    "0: 実際のカメラ写真だが、該当する現象なし（霧・層雲・霧雨・雨のいずれも見られない）\n"
    "1: 薄い川霧\n2: 川霧\n3: 濃い川霧\n"
    "4: 薄い全体霧\n5: 全体霧\n6: 全体濃い霧\n"
    "7: 薄い層雲\n8: 濃い層雲\n9: 霧雨\n10: 雨\n"
    "\n"
    "注意（レンズ付着物との区別）: 屋外設置のカメラのため、レンズに雨滴・水滴や泥・砂埃が"
    "付着していることがあります。これらは画面の一部がにじむ・水玉状に歪む・茶色く汚れた斑点や"
    "曇りとして写り、風景全体が均一に白く煙る自然の霧・層雲とは見え方が異なります。"
    "判定の際は次のように扱ってください。\n"
    "- レンズに水滴や泥汚れが付着していても、背景の川面・対岸・空などの風景がある程度視認できる"
    "場合は、その視認できる範囲の実際の風景の様子から現象を判定してください"
    "（水滴・泥汚れ自体は霧・層雲・雨として数えない）。\n"
    "- レンズの水滴・泥汚れで背景の風景がほとんど視認できない場合は、-1（実際のカメラ写真ではない）"
    "としてください。\n"
    "- レンズに水滴が付着しているだけでは、その時点で実際に降雨中かどうかは判断できません。"
    "水滴の付着だけを根拠に9（霧雨）や10（雨）と判定せず、背景に見える降雨の様子"
    "（路面や川面の波紋、視界不良の均一な広がり方など）から総合的に判断してください。\n"
    "\n"
    "注意（特定カメラの恒常的な曇り）: 「三島大橋」「西谷橋」「鹿島橋」「麻生大橋」の4地点は"
    "レンズ自体が常時わずかに曇っており、天候に関わらずいつも少し霞んで見えます。"
    "この4地点については、その常時の軽い霞みだけを根拠に霧・層雲と判定せず、"
    "普段のその地点の見え方と比べて明らかに視界が悪化している場合のみ現象ありと判定してください。\n"
    "各画像の直前に「地点N: 地点名」というラベルがテキストで付与されています。"
    "station_index には、このラベルのNの数字をそのまま入れてください。"
    "画像とラベルの対応を取り違えないよう、慎重に対応させてください。\n"
    "\n"
    "confidenceには、その判定の確信度をhigh/medium/lowのいずれかで入れてください。\n"
    "- high: 画像が明瞭で、該当コードであることに疑いがない\n"
    "- medium: おおむね妥当だが、画質・逆光・時間帯（薄暮など）により多少の不確実性がある\n"
    "- low: 不鮮明・逆光・薄暮・レンズ汚れの影響が大きい等の理由で、この判定に自信が持てない"
)


def _build_contents(name_bytes_pairs):
    """[(地点名, 縮小済み画像bytes), ...] からGeminiに渡すcontentsとindex->地点名の対応を作る。"""
    from google.genai import types

    contents = []
    index_to_name = {}
    for i, (name, resized) in enumerate(name_bytes_pairs, start=1):
        contents.append(f"地点{i}: {name}")
        contents.append(types.Part.from_bytes(data=resized, mime_type="image/jpeg"))
        index_to_name[i] = name
    return contents, index_to_name


def _classify_with_model(contents, model, api_keys):
    """指定モデルで1回分類を試みる（APIキーのフォールバック・複数パスのリトライ込み）。
    失敗時はNoneを返す。
    """
    from google import genai
    from google.genai import types

    def try_all_keys():
        for key_idx, api_key in enumerate(api_keys, start=1):
            try:
                client = genai.Client(api_key=api_key)
                resp = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        response_mime_type="application/json",
                        response_schema=list[StationClassification],
                        temperature=0,
                        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    ),
                )
            except Exception as e:
                log.warning("Gemini API call failed (model=%s, key %d/%d): %s", model, key_idx, len(api_keys), e)
                continue
            if key_idx > 1:
                log.info("Main API key unavailable, fell back to sub key %d (model=%s)", key_idx, model)
            return resp
        return None

    response = None
    for pass_idx in range(1, GEMINI_RETRY_PASSES + 1):
        response = try_all_keys()
        if response is not None:
            break
        if pass_idx < GEMINI_RETRY_PASSES:
            log.warning(
                "All Gemini API keys failed (model=%s, pass %d/%d), retrying in %ds",
                model, pass_idx, GEMINI_RETRY_PASSES, GEMINI_RETRY_DELAY_SEC,
            )
            time.sleep(GEMINI_RETRY_DELAY_SEC)

    if response is None:
        log.error("Gemini API call failed with all configured keys (model=%s)", model)
        return None

    try:
        parsed: list[StationClassification] = response.parsed
        if parsed is None:
            raise ValueError("response.parsed is None")
    except Exception as e:
        log.error("Failed to parse Gemini response (model=%s): %s / raw=%r", model, e, getattr(response, "text", None))
        return None
    return parsed


def _parse_classifications(parsed, index_to_name):
    """Geminiのレスポンスを (地点名->code の辞書, 無効画像と判定された地点名のset,
    confidence:lowだった地点名のlist) に変換する。
    """
    result = {}
    invalid_stations = set()
    low_confidence = []
    for item in parsed:
        name = index_to_name.get(item.station_index)
        if name is None:
            log.warning("Unknown station_index in Gemini response: %s", item.station_index)
            continue
        if item.code == CODE_INVALID_IMAGE:
            invalid_stations.add(name)
            log.info("Gemini judged image as non-photo/placeholder: %s", name)
            continue
        if not (0 <= item.code <= 10):
            log.warning("Phenomenon code out of range in Gemini response: %s (%s)", item.code, name)
            continue
        result[name] = item.code
        if item.confidence == "low":
            low_confidence.append(name)
    return result, invalid_stations, low_confidence


def classify_images(cameras, images_by_name):
    """Gemini APIで一括分類する。confidence:low（判定に自信が持てない）と申告された地点は、
    より上位のモデル(GEMINI_ESCALATION_MODEL)で自動的に再判定する（自動修正）。
    再判定でも低confidenceのままだった地点はログに残した上で、その再判定結果
    （1回目より確度が高いはず）を採用する。
    戻り値は (地点名 -> code(0-10) の辞書（欠測・無効画像は含めない）, 無効画像と判定された地点名のset)。
    """
    valid = [(name, images_by_name[name]) for name, _ in cameras if images_by_name.get(name)]
    if not valid:
        return {}, set()

    api_keys = get_api_keys()
    if not api_keys:
        log.warning("GEMINI_API_KEY is not set, skipping Gemini classification (all stations missing)")
        return {}, set()

    resized_by_name = {}
    for name, raw_bytes in valid:
        try:
            resized_by_name[name] = resize_image(raw_bytes)
        except Exception as e:
            log.warning("Image resize failed: %s: %s", name, e)

    if not resized_by_name:
        return {}, set()

    contents, index_to_name = _build_contents(list(resized_by_name.items()))
    primary_model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    parsed = _classify_with_model(contents, primary_model, api_keys)
    if parsed is None:
        return {}, set()

    result, invalid_stations, low_confidence = _parse_classifications(parsed, index_to_name)

    if low_confidence:
        log.info(
            "Low-confidence classification: %d station(s), retrying with escalation model: %s",
            len(low_confidence), ", ".join(low_confidence),
        )
        escalation_model = os.environ.get("GEMINI_ESCALATION_MODEL", "gemini-2.5-pro")
        sub_contents, sub_index_to_name = _build_contents(
            [(name, resized_by_name[name]) for name in low_confidence]
        )
        sub_parsed = _classify_with_model(sub_contents, escalation_model, api_keys)
        if sub_parsed is None:
            log.warning(
                "Escalation classification failed; keeping first-pass result for: %s",
                ", ".join(low_confidence),
            )
        else:
            sub_result, sub_invalid, sub_low = _parse_classifications(sub_parsed, sub_index_to_name)
            for name in low_confidence:
                if name in sub_invalid:
                    invalid_stations.add(name)
                    result.pop(name, None)
                elif name in sub_result:
                    result[name] = sub_result[name]
            if sub_low:
                log.warning(
                    "Still low-confidence after escalation (kept escalation result anyway): %s",
                    ", ".join(sub_low),
                )

    return result, invalid_stations


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


def find_existing_row(ws, run_time_naive):
    """同じ日時の行が既に存在すればその行番号を返す（直近数行のみ確認、無ければNone）。"""
    max_row = ws.max_row
    for row in range(max_row, max(max_row - 5, 1), -1):
        if ws.cell(row=row, column=COL_DATETIME).value == run_time_naive:
            return row
    return None


def append_row(cameras, phenom_by_name, weather, run_time):
    ensure_workbook()
    wb = openpyxl.load_workbook(DATA_XLSX)
    ws = wb.active
    # Excelはtzinfoつきのdatetimeを書き込めないため、ローカル時刻の値のまま外す
    run_time_naive = run_time.replace(tzinfo=None)

    existing_row = find_existing_row(ws, run_time_naive)
    if existing_row is not None:
        row = existing_row
        log.warning("Row for %s already exists (row %d); overwriting instead of appending", run_time_naive, row)
    else:
        row = ws.max_row + 1

    ws.cell(row=row, column=COL_DATETIME).value = run_time_naive
    set_weather_measures(ws, row, weather)

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
    action = "Overwrote" if existing_row is not None else "Appended"
    log.info("%s row in xlsx: row %d (%s)", action, row, run_time_naive)


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

    phenom_by_name, invalid_stations = classify_images(cameras, images_by_name)
    log.info(
        "Gemini classification succeeded: %d / %d (invalid/placeholder: %d)",
        len(phenom_by_name), len(cameras), len(invalid_stations),
    )

    usable_count = ok_count - len(invalid_stations)
    problem_ratio = (1.0 - usable_count / len(cameras)) if cameras else 0.0
    if problem_ratio >= IMAGE_FAILURE_ALERT_RATIO:
        log.error(
            "Many stations have no usable photo this run: %d/%d (%.0f%%) -- possible outage on "
            "the camera source site (cam.river.go.jp) or a transient network issue",
            len(cameras) - usable_count, len(cameras), problem_ratio * 100,
        )

    save_images(cameras, images_by_name, invalid_stations, run_time)
    prune_old_images()

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
