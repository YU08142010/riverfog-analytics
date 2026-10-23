"""只見川流域33地点の川霧観測・自動収集（毎時実行）。

画像取得 -> Gemini分類（現象コード + 画像自体の有効性判定） -> Open-Meteo気象取得 -> weather.db追記 を1回分行う。
サーバー1単独運用。データは /opt/river-fog/data にローカル保存し、Samba共有で公開する。

スケジューリングはsystemdタイマー(river-fog-collect.timer/.service)で行っており、
crontabではない(predict.py/backfill_weather.py/backfill_images.pyはcrontab管理)。
"""
import csv
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from io import BytesIO
from typing import Literal

import requests
from dotenv import load_dotenv
from PIL import Image, ImageChops
from pydantic import BaseModel

from common import (
    BASE_DIR,
    CAMERAS_CSV,
    DEFAULT_GEMINI_MODEL,
    JST,
    STATION_WEATHER_DB_PATH,
    WEATHER_DB_PATH,
    load_camera_coords,
    load_camera_regions,
    record_network_failure,
    representative_coords,
    request_with_dns_fallback,
    send_alert,
)
import fog_features
import station_weather
import weather_store

CAM_IMAGE_URL = "https://cam.river.go.jp/cam/now/{cam_id}.jpg"
CAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
FETCH_TIMEOUT = 10
FETCH_WORKERS = 8
FETCH_RETRIES = 2
FETCH_RETRY_DELAY = 1.5
# 取りこぼした地点の取り直し。カメラ配信元(cam.river.go.jp)への名前解決・接続が数十秒〜数分
# だけ失敗し、その時刻の大半の地点が欠けることがある(2026-10-06 08:00は42/65地点)。地点ごとの
# 即時リトライ(FETCH_RETRIES)は数秒おきなのでこうした障害を越えられず、欠けた時刻は後から
# 取り直せない(配信元は最新画像しか出さない)。そこで1回目の取得の後、失敗した地点だけを
# 間隔を空けて取り直す。数地点だけの失敗は個別のカメラの停止であることが多く、待っても
# 戻らないため、REFETCH_MIN_FAILED地点以上失敗した時だけ行う(毎時の収集を無駄に遅らせない)。
REFETCH_ROUND_DELAYS = (90, 240)   # 秒。1回目の取得の後、この間隔を空けて最大2回取り直す
REFETCH_MIN_FAILED = 5

GEMINI_TIMEOUT_MS = 30_000
# Tailscale経由の名前解決が断続的に失敗することがあり(DNS一時障害)、クォータ切れとは
# 別の一過性エラーとして数秒〜十数秒後には回復することが多いため、パス数を3に増やして
# 回復を待てるようにしている(AIチャット機能で「たまに失敗する」という報告があったため)。
GEMINI_RETRY_PASSES = 3
GEMINI_RETRY_DELAY_SEC = 5

WEATHER_FETCH_RETRIES = 2
WEATHER_RETRY_DELAY_SEC = 2

IMAGE_ARCHIVE_DIR = BASE_DIR / "data" / "images"
# Noneにすると自動削除を行わず無期限に保存する。容量が圧迫してきた場合は
# 管理者タブの「ディスク使用量」ページから手動で古い画像を削除できる。
IMAGE_RETENTION_DAYS = None

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

RESIZE_MAX_SIDE = 1024  # 遠景の対岸のわずかな霞み等、微妙な階調を判定しやすくするため768から引き上げ
JPEG_QUALITY = 80

# 自己申告のconfidenceだけに頼らず、既知の判定困難な条件下ではmedium(自信ありと申告されても)を
# low相当としてエスカレーション対象に含める(機械的な補助基準)。LLMの自己申告確信度は必ずしも
# 較正されておらず「自信満々に間違える」ことがあるため。
DAWN_DUSK_HOURS = {4, 5, 6, 17, 18, 19}  # 観測時間帯(4-19時)の両端＝逆光になりやすい時間帯の目安

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
    6: "濃い全体霧",
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
    # 先にreasoningを言語化させてからcode/confidenceを決めさせることで、いきなり
    # 結論だけを出すより主観的な視覚判定の精度が上がりやすい（フィールド順が生成順に
    # 影響するため、reasoningをcodeより前に置いている）。reasoning自体は保存・表示せず
    # ログ出力のみに使う診断用の項目。
    reasoning: str
    code: int
    # 2コード同時観測(2026-09対応、例:雨が降りながら川霧も出ている)。はっきり異なる
    # 系統の現象が同時に写っている場合のみ埋める。無ければNone(SYSTEM_INSTRUCTION参照)。
    # 文字列("1 5"のような人間向け表記)ではなく整数フィールドにしているのは、
    # 構造化出力のJSON schema上で「2つ目があるかどうか」をnull/数値のはっきりした
    # 二択にするため（自由形式の文字列だとフォーマットのブレでパースに失敗しやすい）。
    code2: int | None = None
    confidence: Literal["high", "medium", "low"]


def load_cameras():
    """cameras.csvから (地点名, 画像取得用ID) の一覧を返す。
    通常はカメラID列がそのままcam.river.go.jp/cam/now/{カメラID}.jpgで実画像を返すが、
    river.go.jp側の一部地点(itmkndCd=100、2026-09に追加した会津地方の馬取川・馬越・山科・
    南大橋)はこの規則に従わず、カメラID(scamId)とは無関係な別形式の画像ID
    (例: cctv_070009_41C01420)でしか実画像を取得できない。カメラIDをそのまま使うと
    毎回プレースホルダー画像(同一サイズのPNG)が返り、地点ごとに区別なく同じ画像を
    延々分類し続けてしまう。「画像ID」列にその実際の画像IDを入れておき、あればそちらを
    優先する(river.go.jp側の観測所検索→該当地点クリック→
    file/files/master/obs/scam/{カメラID}.jsonのcurrProvUrlから確認したもの)。
    カメラID自体はriver.go.jp側の地図リンク生成(_river_go_jp_map_url)に必要なため、
    そのまま残す。
    """
    with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [(row["地点名"], row.get("画像ID") or row["カメラID"]) for row in reader]


def _lens_fog_station_names():
    """レンズ自体が常時わずかに曇っているカメラの地点名(cameras.csvの「レンズ曇り」列が
    truthyな行)を返す。以前は該当4地点をSYSTEM_INSTRUCTION内に直接書いていたが、
    cameras.csv側で地点が増減・変更されても追従しないため、CSVの列から動的に組み立てる。
    """
    with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [row["地点名"] for row in reader if row.get("レンズ曇り")]


def fetch_one_image(name, cam_id):
    """画像を取得する。中身が本物の写真かプレースホルダーかはここでは判定しない（Gemini側で行う）。"""
    url = CAM_IMAGE_URL.format(cam_id=cam_id)
    attempts = FETCH_RETRIES + 1
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            resp = request_with_dns_fallback("GET", url, headers=CAM_HEADERS, timeout=FETCH_TIMEOUT, log=log)
            if resp.status_code == 200 and resp.content:
                return name, cam_id, resp.content
            log.warning(
                "Image fetch failed (HTTP %s, attempt %d/%d): %s (%s)",
                resp.status_code, attempt, attempts, name, cam_id,
            )
        except requests.RequestException as e:
            last_exc = e
            log.warning(
                "Image fetch failed (attempt %d/%d): %s (%s): %s",
                attempt, attempts, name, cam_id, e,
            )
        if attempt < attempts:
            time.sleep(FETCH_RETRY_DELAY)
    if last_exc is not None:
        record_network_failure("image_fetch", str(last_exc), station=name)
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


def refetch_failed_images(cameras, images_by_name, delays=REFETCH_ROUND_DELAYS, sleep=time.sleep):
    """取得に失敗した地点(None)だけを、delaysの間隔を空けて取り直し、images_by_nameを更新する。
    失敗がREFETCH_MIN_FAILED地点未満になった時点で打ち切る。取り直せた地点数を返す。"""
    recovered_total = 0
    for delay in delays:
        failed = [(name, cam_id) for name, cam_id in cameras if not images_by_name.get(name)]
        if len(failed) < REFETCH_MIN_FAILED:
            break
        log.warning("Image fetch failed for %d station(s); retrying them in %ds", len(failed), delay)
        sleep(delay)
        retried = fetch_all_images(failed)
        recovered = 0
        for name, content in retried.items():
            if content:
                images_by_name[name] = content
                recovered += 1
        recovered_total += recovered
        log.info("Image refetch recovered %d / %d", recovered, len(failed))
    return recovered_total


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


# 前後フレーム比較によるエスカレーション(自己申告confidenceだけに頼らない追加の機械的
# シグナル)。直前の同地点の画像と比べて見た目が大きく変わっている場合、Geminiが
# confidence:highと自己申告していても念のため上位モデルで再確認する(急な霧の発生・解消、
# あるいは誤判定を疑う根拠になるため)。閾値は経験的な値で、単なる明るさの日内変動では
# 超えない程度を想定している(運用しながら調整すること)。
FRAME_DIFF_THUMBNAIL_SIZE = (64, 64)
FRAME_DIFF_ESCALATION_THRESHOLD = 40.0  # 0-255スケールでの平均輝度差


def _frame_diff_score(bytes_a, bytes_b):
    """2枚の画像のグレースケール平均輝度差(0-255)を返す。小さいサムネイルに縮小してから
    比較するため、圧縮ノイズ等の微差は吸収される。"""
    img_a = Image.open(BytesIO(bytes_a)).convert("L").resize(FRAME_DIFF_THUMBNAIL_SIZE)
    img_b = Image.open(BytesIO(bytes_b)).convert("L").resize(FRAME_DIFF_THUMBNAIL_SIZE)
    diff = ImageChops.difference(img_a, img_b)
    hist = diff.histogram()
    total_pixels = sum(hist)
    if not total_pixels:
        return 0.0
    return sum(i * count for i, count in enumerate(hist)) / total_pixels


def _previous_hour_image_bytes(name, run_time):
    """直前の観測時刻(1時間前)にこの地点で実際に保存された(取得失敗プレースホルダー
    ではない)画像のbytesを返す。無ければNone。"""
    if run_time is None:
        return None
    prev_time = run_time - timedelta(hours=1)
    path = IMAGE_ARCHIVE_DIR / prev_time.strftime("%Y-%m-%d") / prev_time.strftime("%H%M") / f"{name}.jpg"
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _fetch_failed_placeholder_bytes():
    img = Image.new("RGB", FETCH_FAILED_PLACEHOLDER_SIZE, color=FETCH_FAILED_PLACEHOLDER_COLOR)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def save_images(cameras, images_by_name, invalid_stations, run_time):
    """取得画像を後日の検証・再分類用にアーカイブ保存する（縮小版、保存期間は無期限）。
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
    """保存期間を過ぎた画像アーカイブを削除する。IMAGE_RETENTION_DAYSがNoneの場合は
    無期限保存とし、何もしない(古い画像は管理者タブから手動削除する運用)。"""
    if IMAGE_RETENTION_DAYS is None or not IMAGE_ARCHIVE_DIR.exists():
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


_LENS_FOG_STATION_NAMES = _lens_fog_station_names()
_LENS_FOG_NOTE = (
    "注意（特定カメラの恒常的な曇り）: {names}の{count}地点は"
    "レンズ自体が常時わずかに曇っており、天候に関わらずいつも少し霞んで見えます。"
    "この{count}地点については、その常時の軽い霞みだけを根拠に霧・層雲と判定せず、"
    "普段のその地点の見え方と比べて明らかに視界が悪化している場合のみ現象ありと判定してください。\n"
).format(
    names="".join(f"「{n}」" for n in _LENS_FOG_STATION_NAMES),
    count=len(_LENS_FOG_STATION_NAMES),
) if _LENS_FOG_STATION_NAMES else ""


def _should_treat_medium_as_low(name, run_time):
    """confidence:mediumだった地点を、low相当としてエスカレーション対象に含めるべきかを返す。
    薄暮・夜明け前後(逆光になりやすい)か、レンズが常時わずかに曇っている地点は、
    自己申告のconfidenceが実態より高く出やすいと考えられるため、機械的に上乗せする。
    """
    if run_time is not None and run_time.hour in DAWN_DUSK_HOURS:
        return True
    return name in _LENS_FOG_STATION_NAMES


SYSTEM_INSTRUCTION = (
    "あなたは河川監視カメラの画像から気象現象を分類する専門家です。\n"
    "各画像を、以下の現象コード表に従って-1〜10の整数のいずれか1つに分類してください。\n"
    "-1: この画像は実際のカメラ写真ではない（例：「カメラ画像が取得できませんでした」等の"
    "エラーメッセージ画面、単色の塗りつぶし、システムのロゴやUI画面など、河川や周辺の風景が"
    "写っていないもの）\n"
    "0: 実際のカメラ写真だが、該当する現象なし（霧・層雲・霧雨・雨のいずれも見られない）\n"
    "1: 薄い川霧\n2: 川霧\n3: 濃い川霧\n"
    "4: 薄い全体霧\n5: 全体霧\n6: 濃い全体霧\n"
    "7: 薄い層雲\n8: 濃い層雲\n9: 霧雨\n10: 雨\n"
    "\n"
    "1つの画像に、はっきり異なる系統の現象が同時に写っている場合は、code2にもう1つの"
    "コードを入れてください（無ければcode2は入れないでください）。\n"
    "現象コードは次の5つのグループに分かれています: "
    "(A)現象なし{0} (B)川霧{1,2,3} (C)全体霧{4,5,6} (D)層雲{7,8} (E)霧雨・雨{9,10}。\n"
    "- 同じグループ内の2つのコードを両方選んではいけません"
    "（例: 1と3を両方選ばない。「薄いか濃いか」はどちらか1つのコードの強度で表現し、"
    "2つのコードには分けない）。\n"
    "- 0（現象なし）は他のどのコードとも組み合わせてはいけません。\n"
    "- 現象が1つしか見当たらない場合はcode2を入れず、無理に2つ目を探さないでください"
    "（例: 川面付近は川霧で霞み、かつ空は層雲に覆われている、のように実際に異なる系統の"
    "現象が同時にはっきり見える場合のみcode2を使う）。\n"
    "\n"
    "判定基準の目安（カテゴリ名だけでは境界が曖昧なため、具体的な見え方で判断してください）:\n"
    "- 現象なし(0)と薄い現象(1・4・7)の境界: 対岸や空の輪郭がわずかでも明確に見分けられる場合は"
    "現象なし(0)とし、肉眼で視程の低下がはっきり分かる場合のみ薄い現象とする。\n"
    "- 薄い/普通/濃いの目安: 対岸の輪郭がはっきり見える→薄い、輪郭はぼやけるが判別できる→普通、"
    "対岸がほとんど見えない→濃い。\n"
    "- 川霧(1〜3) vs 全体霧(4〜6): 川面付近だけが白く霞み対岸・空はある程度見える場合は川霧、"
    "川面だけでなく対岸・空まで含めて画面全体が一様に白く霞んでいる場合は全体霧とする。\n"
    "- 全体霧(4〜6) vs 層雲(7・8): 地表・川面付近の視界も悪化している場合は霧(1〜6)、"
    "地表付近の視界は保たれていて空だけが厚い雲に覆われている場合は層雲(7・8)とする。\n"
    "- 薄暮・夜間の暗さと霧を混同しない: 画面全体が暗いだけでは霧・層雲と判定しない。"
    "暗さの中でも光がにじむ・輪郭が霞む等、視程の低下がはっきり認められる場合のみ現象ありとする。\n"
    "\n"
    "注意（レンズ付着物との区別）: 屋外設置のカメラのため、レンズに雨滴・水滴や泥・砂埃が"
    "付着していることがあります。これらは画面の一部がにじむ・水玉状に歪む・茶色く汚れた斑点や"
    "曇りとして写り、風景全体が均一に白く煙る自然の霧・層雲とは見え方が異なります。"
    "判定の際は次のように扱ってください。\n"
    "- レンズに水滴や泥汚れが付着していても、背景の川面・対岸・空などの風景がある程度視認できる"
    "場合は、その視認できる範囲の実際の風景の様子から現象を判定してください"
    "（水滴・泥汚れ自体は霧・層雲・雨として数えない）。\n"
    "- レンズの水滴・泥汚れで背景の風景がほとんど視認できない場合は、-1（実際のカメラ写真ではない）"
    "としてください。ただし橋・道路・川面・対岸の一部など、風景の手がかりが少しでも判別できる"
    "場合は-1にせず、その手がかりから読み取れる現象で判定してください"
    "（-1は、風景の手がかりが本当に何も読み取れない場合に限定してください）。\n"
    "- レンズに水滴が付着しているだけでは、その時点で実際に降雨中かどうかは判断できません。"
    "水滴の付着だけを根拠に9（霧雨）や10（雨）と判定せず、背景に見える降雨の様子"
    "（路面や川面の波紋、視界不良の均一な広がり方など）から総合的に判断してください。\n"
    "\n"
    + _LENS_FOG_NOTE +
    "各画像の直前に「地点N: 地点名」というラベルがテキストで付与されています。"
    "station_index には、このラベルのNの数字をそのまま入れてください。"
    "画像とラベルの対応を取り違えないよう、慎重に対応させてください。\n"
    "\n"
    "reasoningには、判定の決め手になった視覚的な根拠を1文程度で簡潔に記述してください"
    "（例:「対岸の輪郭が薄く霞んで見えるため」「川面付近にのみ白い帯状の霞みがあるため」）。"
    "必ずreasoningで見えている根拠を整理してから、それに基づいてcode（code2があれば併せて）と"
    "confidenceを決めてください。\n"
    "\n"
    "confidenceには、その判定の確信度をhigh/medium/lowのいずれかで入れてください"
    "（code2がある場合は、2つのコードそれぞれの確信度も踏まえたうえで判断してください）。\n"
    "- high: 画像が明瞭で、該当コードであることに疑いがない\n"
    "- medium: おおむね妥当だが、画質・逆光・時間帯（薄暮など）により多少の不確実性がある\n"
    "- low: 不鮮明・逆光・薄暮・レンズ汚れの影響が大きい等の理由で、この判定に自信が持てない"
)


# --- 判定例(few-shot、2026-10-11) ---------------------------------------------------------------
# 正解セット(gold_set.py)で人が判定した画像を、判定の手本としてプロンプトの先頭に添える。
# .envのGEMINI_FEWSHOT=1で有効(既定はオフ)。効果は fewshot_check.py で正解セットの画像を判定し直して確かめる。
FEWSHOT_PLAN = ((1, 2), (2, 1), (3, 1), (5, 1), (0, 2), (7, 1))   # (まとまりの代表コード, 枚数): 川霧を厚めに
FEWSHOT_GROUPS = {0: {0}, 1: {1}, 2: {2}, 3: {3}, 5: {4, 5, 6}, 7: {7, 8}}


def fewshot_examples(exclude=None, max_total=8):
    """人の判定が一致した(または1人だけが判定した)正解セットの画像から、判定例 [(コード, ラベル文, 画像bytes)]。
    excludeの(地点, 日時)は使わない(評価のときに、判定する画像そのものを手本に入れないため)。"""
    import sqlite3
    db = BASE_DIR / "data" / "users.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT s.station_name, s.target_datetime, GROUP_CONCAT(l.code) FROM gold_samples s "
            "JOIN gold_labels l ON l.sample_id = s.id WHERE l.code >= 0 GROUP BY s.id ORDER BY s.target_datetime DESC"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    pool = {}
    for station, dt, codes in rows:
        if exclude and (station, dt) in exclude:
            continue
        cs = {int(c) for c in codes.split(",")}
        prim = {c // 100 if c > 10 else c for c in cs}
        if len(prim) != 1:
            continue  # 人の間で分かれた画像は手本にしない
        p = prim.pop()
        path = IMAGE_ARCHIVE_DIR / dt[:10] / (dt[11:13] + dt[14:16]) / f"{station}.jpg"
        if path.is_file():
            pool.setdefault(p, []).append((station, dt, path, p))
    out, used_stations = [], set()
    for rep, n in FEWSHOT_PLAN:
        cands = [x for c in FEWSHOT_GROUPS[rep] for x in pool.get(c, [])]
        # まだ使っていない地点を先に、新しい日付から
        cands.sort(key=lambda x: x[1], reverse=True)
        cands.sort(key=lambda x: x[0] in used_stations)
        for station, dt, path, code in cands[:n]:
            try:
                out.append((code, PHENOM_LABELS.get(code, "現象なし") if code else "現象なし", resize_image(path.read_bytes())))
                used_stations.add(station)
            except Exception as e:  # noqa: BLE001
                log.warning("Few-shot example skipped (%s %s): %s", station, dt, e)
        if len(out) >= max_total:
            break
    return out[:max_total]


def _fewshot_contents(examples):
    from google.genai import types
    if not examples:
        return []
    parts = ["以下は、人が同じ基準で判定した手本です(判定する対象ではありません)。見え方の基準の参考にしてください。"]
    for i, (code, label, data) in enumerate(examples, start=1):
        parts.append(f"手本{i}: 人の判定はコード{code}「{label}」")
        parts.append(types.Part.from_bytes(data=data, mime_type="image/jpeg"))
    parts.append("ここからが判定する画像です。")
    return parts


def _phenom_label_for_prompt(code):
    # codeは2コード同時観測(2026-09対応)のパック済み値のこともあるため、
    # fog_features.phenom_labelで主・副コードを結合したラベルにする。
    return fog_features.phenom_label(code)


def _build_contents(name_bytes_pairs, weather_context=None, first_pass_by_name=None, first_pass_invalid=None):
    """[(地点名, 縮小済み画像bytes), ...] からGeminiに渡すcontentsとindex->地点名の対応を作る。

    weather_context: 冒頭に添える気象状況の説明文（Noneまたは空文字なら付けない）。
    first_pass_by_name: {地点名: 1回目の判定コード}。エスカレーション(再判定)呼び出し時に
    渡すと、各地点のラベルに1回目の結果を添えて「特に隣接コードとの境界でないか」の
    再確認を促す（画像だけを渡してゼロから判定させるより、着目点を絞り込める）。
    first_pass_invalid: 1回目に-1（無効画像）と判定され、かつconfidenceが低かった地点名のset。
    レンズの水滴・泥汚れを実際の霧・無効画像のどちらと見るかは軽量モデルが誤りやすい境界の
    ひとつなので、上位モデルには「本当に無効画像かどうか」自体を再確認させる。
    """
    from google.genai import types

    contents = []
    if weather_context:
        contents.append(weather_context)
    index_to_name = {}
    for i, (name, resized) in enumerate(name_bytes_pairs, start=1):
        label = f"地点{i}: {name}"
        if first_pass_invalid and name in first_pass_invalid:
            label += (
                "（1回目の判定: -1「実際のカメラ写真ではない」・確信度low。"
                "レンズの水滴・泥汚れによる歪みと、背景に実際に写っている川面・対岸・空の様子を"
                "区別し、背景がある程度でも視認できるなら-1にはせず、その視認できる範囲の"
                "実際の現象で判定し直してください）"
            )
        elif first_pass_by_name and name in first_pass_by_name:
            prev_code = first_pass_by_name[name]
            label += (
                f"（1回目の判定: コード{prev_code}「{_phenom_label_for_prompt(prev_code)}」・"
                "確信度low。特に隣接するコードとの境界でないか、画像を慎重に見直してください）"
            )
        contents.append(label)
        contents.append(types.Part.from_bytes(data=resized, mime_type="image/jpeg"))
        index_to_name[i] = name
    return contents, index_to_name


def _weather_context_text(weather):
    """撮影時刻の周辺気象を、判定の補助材料としてGeminiに渡すためのテキストを作る。
    放射霧は高湿度・低風速・無降水の条件で発生しやすいという傾向を判定のヒントにする
    （あくまで補助情報で、実際に画像に写っている見た目を優先させる）。
    データが無ければ空文字を返す(呼び出し側はcontentsに追加しない)。
    """
    if not weather:
        return ""
    parts = []
    if weather.get("temperature") is not None:
        parts.append(f"気温{weather['temperature']:.1f}℃")
    if weather.get("humidity") is not None:
        parts.append(f"湿度{weather['humidity']:.0f}%")
    if weather.get("wind_speed") is not None:
        parts.append(f"風速{weather['wind_speed']:.1f}m/s")
    if weather.get("precipitation") is not None:
        parts.append(f"降水量{weather['precipitation']:.1f}mm")
    if not parts:
        return ""
    return (
        "参考情報（この時刻の周辺気象）: " + "・".join(parts) + "。"
        "高湿度・低風速・無降水の条件は霧が発生しやすく、逆に低湿度の条件で白っぽく写っている"
        "場合は霧ではなく逆光・もや・排気等の可能性も考慮してください"
        "（あくまで補助的な参考情報であり、実際に画像に写っている見た目を優先して判定してください）。"
    )


def _gemini_generate_with_fallback(model, contents, config, api_keys, label):
    """指定モデル・contents・configでGemini APIを呼ぶ共通ロジック。api_keysを順に試し
    (フォールバック)、全滅した場合はGEMINI_RETRY_PASSES回までリトライする。
    ただし全キーがクォータ超過(429/RESOURCE_EXHAUSTED)だった場合は、日次クォータが
    数秒待っても回復することはないため、それ以上のリトライはせず即座に諦める
    (以前は一時的なネットワーク障害と同じ扱いで無駄なリトライ・待機をしていた)。
    labelはログ用の呼び出し元名("classification"/"escalation"/"ai_summary"等)。
    失敗時はNoneを返す。
    """
    from google import genai

    def try_all_keys():
        quota_hits = 0
        for key_idx, api_key in enumerate(api_keys, start=1):
            try:
                client = genai.Client(api_key=api_key)
                resp = client.models.generate_content(model=model, contents=contents, config=config)
            except Exception as e:
                # クォータ超過(恒久的に失敗する)と一時的なネットワーク障害を、
                # ログ上で区別できるようにする(原因調査をしやすくするため)。
                is_quota = "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e)
                if is_quota:
                    quota_hits += 1
                log.warning(
                    "Gemini API call failed [%s] (%s, model=%s, key %d/%d, %s): %s",
                    "QUOTA" if is_quota else "ERROR", label, model, key_idx, len(api_keys), type(e).__name__, e,
                )
                continue
            if key_idx > 1:
                log.info("Main API key unavailable, fell back to sub key %d (%s, model=%s)", key_idx, label, model)
            return resp, False
        return None, quota_hits == len(api_keys)

    response = None
    for pass_idx in range(1, GEMINI_RETRY_PASSES + 1):
        response, all_quota_exhausted = try_all_keys()
        if response is not None:
            break
        if all_quota_exhausted:
            log.error(
                "All Gemini API keys report quota exhaustion (%s, model=%s); giving up without further "
                "retries this run (daily quota won't recover within seconds)",
                label, model,
            )
            break
        if pass_idx < GEMINI_RETRY_PASSES:
            log.warning(
                "All Gemini API keys failed (%s, model=%s, pass %d/%d), retrying in %ds",
                label, model, pass_idx, GEMINI_RETRY_PASSES, GEMINI_RETRY_DELAY_SEC,
            )
            time.sleep(GEMINI_RETRY_DELAY_SEC)

    if response is None:
        log.error("Gemini API call failed with all configured keys (%s, model=%s)", label, model)
    return response


def _classify_with_model(contents, model, api_keys, label="classification"):
    """指定モデルで1回分類を試みる（APIキーのフォールバック・クォータ考慮リトライ込み）。
    失敗時はNoneを返す。
    """
    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=list[StationClassification],
        temperature=0,
        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    response = _gemini_generate_with_fallback(model, contents, config, api_keys, label)
    if response is None:
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
    confidence:lowだった地点名のlist, 地点名->confidenceの辞書, 地点名->reasoningの辞書)
    に変換する。返すcodeは、code2があれば2コード同時観測(2026-09対応)をパックした値
    (fog_features.encode_phenom_codes参照)。

    confidenceは無効画像(-1)判定についても記録する。レンズの水滴・泥汚れと実際の霧を
    見分けるのは境界が曖昧な判定であり、この確信度が低い/中程度の場合は
    classify_images側で通常の現象コードと同様にエスカレーション(上位モデルでの再判定)の
    対象に含める必要があるため（以前は無効画像と判定された時点でconfidenceを捨てて
    確定させてしまい、誤って無効判定された画像を訂正する機会が無かった）。
    """
    result = {}
    invalid_stations = set()
    low_confidence = []
    confidence_by_name = {}
    reasoning_by_name = {}
    for item in parsed:
        name = index_to_name.get(item.station_index)
        if name is None:
            log.warning("Unknown station_index in Gemini response: %s", item.station_index)
            continue
        reasoning_by_name[name] = item.reasoning
        confidence_by_name[name] = item.confidence
        if item.confidence == "low":
            low_confidence.append(name)
        if item.code == CODE_INVALID_IMAGE:
            invalid_stations.add(name)
            log.info(
                "Gemini judged image as non-photo/placeholder: %s (confidence=%s)", name, item.confidence,
            )
            continue
        if not (0 <= item.code <= 10):
            log.warning("Phenomenon code out of range in Gemini response: %s (%s)", item.code, name)
            continue
        codes = [item.code] if item.code2 is None else [item.code, item.code2]
        try:
            result[name] = fog_features.encode_phenom_codes(codes)
        except fog_features.PhenomCodeError as e:
            # Geminiが誤って同じグループの2つ・0との組み合わせ等を返した場合、
            # code2ごと捨てて単一コードとして扱う(丸ごと欠測にして学習データを
            # 減らすよりはましなフォールバック)。
            log.warning("Invalid phenomenon code combination from Gemini: %s (%s): %s", codes, name, e)
            result[name] = item.code
    return result, invalid_stations, low_confidence, confidence_by_name, reasoning_by_name


def classify_images(cameras, images_by_name, weather=None, run_time=None):
    """Gemini APIで一括分類する。confidence:low（判定に自信が持てない）と申告された地点は、
    より上位のモデル(GEMINI_ESCALATION_MODEL)で自動的に再判定する（自動修正）。
    再判定でも低confidenceのままだった地点はログに残した上で、その再判定結果
    （1回目より確度が高いはず）を採用する。
    confidence:mediumの地点でも、薄暮・夜明け前後やレンズが常時わずかに曇っている地点
    （_should_treat_medium_as_low参照）は自己申告confidenceを信用しきらず、機械的に
    エスカレーション対象へ追加する(run_timeを渡した場合のみ時間帯判定が有効になる)。
    -1（無効画像）と判定された地点もconfidence:low/medium相当ならエスカレーション対象に
    含める。レンズの水滴・泥汚れで一部視界が悪いだけの画像を誤って「取得失敗」扱いに
    してしまうケースがあり、上位モデルによる再確認で背景の実際の現象を拾い直せるように
    するため（再判定で実際の現象コードが得られれば無効判定は取り消される）。
    weatherを渡すと、撮影時刻の周辺気象(気温・湿度・風速等)を判定の補助材料として
    プロンプトに含める（霧の発生しやすさの参考情報。Noneなら付けない）。
    戻り値は (地点名 -> code(0-10) の辞書（欠測・無効画像は含めない）, 無効画像と判定された地点名のset,
    地点名 -> 最終的に採用したconfidenceの辞書)。confidenceは学習時に低confidenceラベルを
    除外できるよう、collect.py側でCSVに記録するために返している。
    """
    valid = [(name, images_by_name[name]) for name, _ in cameras if images_by_name.get(name)]
    if not valid:
        return {}, set(), {}, {}

    api_keys = get_api_keys()
    if not api_keys:
        log.warning("GEMINI_API_KEY is not set, skipping Gemini classification (all stations missing)")
        return {}, set(), {}, {}

    resized_by_name = {}
    for name, raw_bytes in valid:
        try:
            resized_by_name[name] = resize_image(raw_bytes)
        except Exception as e:
            log.warning("Image resize failed: %s: %s", name, e)

    if not resized_by_name:
        return {}, set(), {}, {}

    # 気象のヒント(高湿度・低風速なら霧が出やすい等)をプロンプトに含めると、ラベル(目的変数)が
    # 予測モデルの説明変数(同じ気象値)の影響を受けてしまい、予測精度の評価が甘くなる。人間が確認した画像での
    # 比較(2026-10-02、管理者タブ「仮検証」のラベル品質ページ)でも、ヒントなしの方が良かったため、
    # 既定ではヒントを付けない。従来の挙動に戻す場合は.envにGEMINI_WEATHER_HINT=1を設定する。
    weather_context = _weather_context_text(weather) if os.environ.get("GEMINI_WEATHER_HINT", "0") == "1" else ""
    contents, index_to_name = _build_contents(list(resized_by_name.items()), weather_context=weather_context)
    if os.environ.get("GEMINI_FEWSHOT", "0") == "1":
        try:
            shots = _fewshot_contents(fewshot_examples())
            if shots:
                contents = shots + contents
                log.info("Added %d few-shot example image(s) from the gold set", sum(1 for p in shots if not isinstance(p, str)))
        except Exception as e:  # noqa: BLE001  手本が作れなくても判定は続ける
            log.warning("Few-shot examples skipped: %s", e)
    primary_model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    parsed = _classify_with_model(contents, primary_model, api_keys)
    if parsed is None:
        return {}, set(), {}, {}

    result, invalid_stations, low_confidence, confidence_by_name, reasoning_by_name = _parse_classifications(
        parsed, index_to_name,
    )

    confidence_counts = {"high": 0, "medium": 0, "low": 0}
    for item in parsed:
        confidence_counts[item.confidence] = confidence_counts.get(item.confidence, 0) + 1
    log.info(
        "Confidence distribution: high=%d, medium=%d, low=%d",
        confidence_counts["high"], confidence_counts["medium"], confidence_counts["low"],
    )

    extra_escalation = [
        name for name, conf in confidence_by_name.items()
        if conf == "medium" and name not in low_confidence and _should_treat_medium_as_low(name, run_time)
    ]
    if extra_escalation:
        log.info(
            "Escalating %d additional medium-confidence station(s) due to known-difficult conditions "
            "(dawn/dusk hour or persistently misty lens): %s",
            len(extra_escalation), ", ".join(extra_escalation),
        )
        low_confidence = low_confidence + extra_escalation

    # 薄暮・夜明け前後は光量変化そのものが大きく、フレーム間の輝度差が(実際の現象と無関係に)
    # 通常時より大幅に高く出ることを実データで確認済み(閾値40に対し中央値50〜65)。
    # その時間帯は既にDAWN_DUSK_HOURSの仕組みで別途エスカレーション対象になっているため、
    # フレーム比較はそれ以外の時間帯に限定する(でないと薄暮のたびにほぼ全地点が
    # 引っかかってしまい、シグナルとして機能しない)。
    frame_change_escalation = []
    if run_time is not None and run_time.hour not in DAWN_DUSK_HOURS:
        for name, raw_bytes in valid:
            if name in low_confidence:
                continue
            prev_bytes = _previous_hour_image_bytes(name, run_time)
            if prev_bytes is None:
                continue
            try:
                score = _frame_diff_score(prev_bytes, raw_bytes)
            except Exception as e:
                log.warning("Frame diff check failed for %s: %s", name, e)
                continue
            if score >= FRAME_DIFF_ESCALATION_THRESHOLD:
                frame_change_escalation.append(name)
    if frame_change_escalation:
        log.info(
            "Escalating %d additional station(s) due to large frame-to-frame change vs previous hour: %s",
            len(frame_change_escalation), ", ".join(frame_change_escalation),
        )
        low_confidence = low_confidence + frame_change_escalation

    if low_confidence:
        for name in low_confidence:
            log.info("Low-confidence reasoning: %s -> %s", name, reasoning_by_name.get(name, ""))

        escalation_state = _load_escalation_state()
        skip_until = escalation_state.get("skip_until")
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        if skip_until and now_str < skip_until:
            log.info(
                "Skipping escalation for %d station(s) (escalation model failed repeatedly; "
                "cooling down until %s): %s",
                len(low_confidence), skip_until, ", ".join(low_confidence),
            )
        else:
            log.info(
                "Low-confidence classification: %d station(s), retrying with escalation model: %s",
                len(low_confidence), ", ".join(low_confidence),
            )
            escalation_model = os.environ.get("GEMINI_ESCALATION_MODEL", "gemini-3.1-pro-preview")
            # 1回目の判定結果(コードまたは-1)を各地点のラベルに添えて、上位モデルには
            # 「ゼロから判定」ではなく「特にこの境界を再確認する」形で見直させる。
            first_pass_by_name = {name: result[name] for name in low_confidence if name in result}
            first_pass_invalid = {name for name in low_confidence if name in invalid_stations}
            sub_contents, sub_index_to_name = _build_contents(
                [(name, resized_by_name[name]) for name in low_confidence],
                weather_context=weather_context,
                first_pass_by_name=first_pass_by_name,
                first_pass_invalid=first_pass_invalid,
            )
            sub_parsed = _classify_with_model(sub_contents, escalation_model, api_keys, label="escalation")
            if sub_parsed is None:
                log.warning(
                    "Escalation classification failed; keeping first-pass result for: %s",
                    ", ".join(low_confidence),
                )
                escalation_state["consecutive_failures"] = escalation_state.get("consecutive_failures", 0) + 1
                if escalation_state["consecutive_failures"] >= ESCALATION_FAILURE_THRESHOLD:
                    cooldown_until = (datetime.now(JST) + timedelta(hours=ESCALATION_COOLDOWN_HOURS))
                    escalation_state["skip_until"] = cooldown_until.strftime("%Y-%m-%d %H:%M:%S")
                    log.warning(
                        "Escalation failed %d time(s) in a row; pausing escalation attempts until %s",
                        escalation_state["consecutive_failures"], escalation_state["skip_until"],
                    )
                _save_escalation_state(escalation_state)
            else:
                if escalation_state.get("consecutive_failures") or escalation_state.get("skip_until"):
                    _save_escalation_state({"consecutive_failures": 0, "skip_until": None})
                sub_result, sub_invalid, sub_low, sub_confidence, sub_reasoning = _parse_classifications(
                    sub_parsed, sub_index_to_name,
                )
                for name in low_confidence:
                    log.info("Escalation reasoning: %s -> %s", name, sub_reasoning.get(name, ""))
                    if name in sub_invalid:
                        invalid_stations.add(name)
                        result.pop(name, None)
                        confidence_by_name.pop(name, None)
                    elif name in sub_result:
                        result[name] = sub_result[name]
                        confidence_by_name[name] = sub_confidence.get(name, "low")
                        # 判定理由も再判定時のものに揃える(採用したコードと理由がズレたまま
                        # ログに残ってしまう不整合を防ぐ)。
                        reasoning_by_name[name] = sub_reasoning.get(name, reasoning_by_name.get(name, ""))
                        # 1回目は-1(無効画像)判定だったが、上位モデルの再判定で実際の現象
                        # コードが得られた場合は、無効判定を取り消して正しい現象を採用する。
                        invalid_stations.discard(name)
                if sub_low:
                    log.warning(
                        "Still low-confidence after escalation (kept escalation result anyway): %s",
                        ", ".join(sub_low),
                    )

    return result, invalid_stations, confidence_by_name, reasoning_by_name


# エスカレーション(上位モデルでの再判定)が連続で失敗する場合のサーキットブレーカー。
# pro系モデルは無料枠のクォータが厳しいことが多く、枯渇していると毎時律儀に呼び続けて
# 無駄になるため、連続失敗が閾値を超えたら一定時間エスカレーションそのものをスキップする。
ESCALATION_STATE_PATH = BASE_DIR / "data" / "models" / ".escalation_state.json"
ESCALATION_FAILURE_THRESHOLD = 3
ESCALATION_COOLDOWN_HOURS = 6


def _load_escalation_state():
    try:
        with open(ESCALATION_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"consecutive_failures": 0, "skip_until": None}


def _save_escalation_state(state):
    try:
        ESCALATION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(ESCALATION_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning("Failed to save escalation circuit-breaker state: %s", e)


CONFIDENCE_LOG_PATH = BASE_DIR / "data" / "models" / "confidence_log.csv"


def _migrate_confidence_log_add_reasoning_column():
    """旧フォーマット(datetime,station,confidenceの3列)のログファイルに、reasoning列を
    追加する1回限りの移行処理。既にreasoning列があれば何もしない(冪等)。"""
    if not CONFIDENCE_LOG_PATH.exists():
        return
    with open(CONFIDENCE_LOG_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows or (rows[0] and rows[0][-1] == "reasoning"):
        return
    rows[0].append("reasoning")
    for row in rows[1:]:
        row.append("")
    with open(CONFIDENCE_LOG_PATH, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    log.info("Migrated confidence_log.csv to add reasoning column (%d row(s))", len(rows) - 1)


def append_confidence_log(confidence_by_name, reasoning_by_name, run_time):
    """Geminiの分類confidenceと判定理由を記録する。fog_model.pyがこれを読み、学習時に
    confidence:lowだったラベルを除外するために使う。reasoningは元々ログファイルにのみ
    出力する診断用の項目だったが、後から人間が誤判定を見直す際の手がかりとして
    構造化して残すようにした。"""
    if not confidence_by_name:
        return
    try:
        CONFIDENCE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _migrate_confidence_log_add_reasoning_column()
        is_new = not CONFIDENCE_LOG_PATH.exists()
        dt_str = run_time.strftime("%Y-%m-%d %H:%M:%S")
        with open(CONFIDENCE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(["datetime", "station", "confidence", "reasoning"])
            for name, confidence in confidence_by_name.items():
                writer.writerow([dt_str, name, confidence, reasoning_by_name.get(name, "")])
    except Exception as e:
        log.warning("Failed to append confidence log: %s", e)


class StationSummaryItem(BaseModel):
    name: str
    summary: str


class AiSummaryResult(BaseModel):
    overall: str
    stations: list[StationSummaryItem]


AI_SUMMARY_PATH = BASE_DIR / "data" / "ai_summary.json"
# 画像分類(毎時、confidence:low時は再判定でさらに1回)に加えてのAPI呼び出しになるため、
# 無料枠を圧迫しないよう要約生成は間隔を空ける(観測時間帯4〜19時の毎時実行のうち、
# 実際にはこの間隔以上経過している回だけ生成する)。
AI_SUMMARY_MIN_INTERVAL_HOURS = 3

AI_SUMMARY_SYSTEM_INSTRUCTION = (
    "あなたは只見川・会津地方の川霧観測・予測ダッシュボードの解説者です。以下に渡す気象情報と、"
    "各観測地点の現在の現象を踏まえて、日本語で要約を書いてください。地点は【只見川】【会津】の"
    "見出しで地域ごとに分けて渡されます。\n"
    "- overallには、両地域それぞれの状況を踏まえて2〜3文程度で簡潔にまとめてください"
    "（霧が出ている地点が多いか少ないか、天候の傾向、注意すべき点など。片方の地域にしか"
    "言及すべき変化が無い場合は、その地域だけに触れれば構いません）。\n"
    "- stationsには、渡された各地点について1つずつ、1〜2文程度の短い説明を書いてください。"
    "現象がない地点についても「現在、目立った現象は見られません」のように簡潔に触れてください。\n"
    "- 数値を断定的に予測したり、渡されていない情報を推測で付け加えたりしないでください。"
)


def _build_summary_prompt(cameras, phenom_by_name, weather, run_time):
    lines = [f"観測時刻: {run_time.strftime('%Y-%m-%d %H:%M')}"]
    if weather:
        w = weather
        lines.append(
            "気象(只見川代表地点): 気温{temp}℃ 降水量{precip}mm 風速{wind}m/s 湿度{humidity}%".format(
                temp=w.get("temperature", "-"), precip=w.get("precipitation", "-"),
                wind=w.get("wind_speed", "-"), humidity=w.get("humidity", "-"),
            )
        )
    regions = load_camera_regions()

    def phenom_label(name):
        code = phenom_by_name.get(name)
        if code is None:
            return "不明(取得失敗)"
        # codeは2コード同時観測(2026-09対応)のパック済み値のこともあるため、
        # fog_features.phenom_labelで主・副コードを結合したラベルにする。
        return fog_features.phenom_label(code)

    for region_label in ("只見川", "会津"):
        names_in_region = [name for name, _cam_id in cameras if regions.get(name) == region_label]
        if not names_in_region:
            continue
        lines.append(f"【{region_label}】の現在の現象:")
        for name in names_in_region:
            lines.append(f"- {name}: {phenom_label(name)}")
    # 地域列が空/未設定の地点(通常は無いはずだが、cameras.csvの記載漏れ対策)も取りこぼさない。
    unclassified = [name for name, _cam_id in cameras if not regions.get(name)]
    if unclassified:
        lines.append("【地域未設定】の現在の現象:")
        for name in unclassified:
            lines.append(f"- {name}: {phenom_label(name)}")
    return "\n".join(lines)


def generate_ai_summaries(cameras, phenom_by_name, weather, run_time):
    """流域全体+地点ごとのAI要約をGeminiで生成し、AI_SUMMARY_PATHにJSONで保存する。
    画像分類と同じくGEMINI_API_KEYS(カンマ区切り)を順に試し、メインキーが枯渇/失敗した
    場合はサブキーにフォールバックする(get_api_keys()・GEMINI_RETRY_PASSESを流用)。
    失敗しても収集全体は止めない(前回分のJSONが残るだけで、表示側は古い要約のまま)。
    """
    try:
        if AI_SUMMARY_PATH.exists():
            try:
                with open(AI_SUMMARY_PATH, encoding="utf-8") as f:
                    prev_generated_at = datetime.strptime(
                        json.load(f)["generated_at"], "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=JST)
                elapsed_hours = (run_time - prev_generated_at).total_seconds() / 3600
                if elapsed_hours < AI_SUMMARY_MIN_INTERVAL_HOURS:
                    log.info(
                        "Skipping AI summary generation (last one was %.1fh ago, min interval is %dh)",
                        elapsed_hours, AI_SUMMARY_MIN_INTERVAL_HOURS,
                    )
                    return
            except Exception as e:
                log.warning("Failed to check previous AI summary timestamp, generating anyway: %s", e)

        from google.genai import types

        api_keys = get_api_keys()
        if not api_keys:
            log.warning("GEMINI_API_KEY is not set, skipping AI summary generation")
            return

        prompt = _build_summary_prompt(cameras, phenom_by_name, weather, run_time)
        model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        config = types.GenerateContentConfig(
            system_instruction=AI_SUMMARY_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=AiSummaryResult,
            temperature=0.3,
            http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = _gemini_generate_with_fallback(model, prompt, config, api_keys, label="ai_summary")
        if response is None:
            return

        parsed: AiSummaryResult = response.parsed
        if parsed is None:
            log.error("AI summary response could not be parsed")
            return

        payload = {
            "generated_at": run_time.strftime("%Y-%m-%d %H:%M:%S"),
            "overall": parsed.overall,
            "stations": {item.name: item.summary for item in parsed.stations},
        }
        AI_SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(AI_SUMMARY_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        log.info("Saved AI summary (%d station note(s))", len(payload["stations"]))
    except Exception:
        log.exception("Failed to generate AI summary")


WEATHER_HOURLY_VARS = [
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


def _extract_weather_at_target_hour(hourly):
    """hourly応答(1地点分)から直近時刻の値を取り出し、weather dictにして返す。
    該当時刻が無ければNone。fetch_weather()・fetch_weather_multi()の両方で使う。"""
    times = hourly.get("time", [])
    if not times:
        return None

    now_jst = datetime.now(JST).replace(minute=0, second=0, microsecond=0)
    target = now_jst.strftime("%Y-%m-%dT%H:00")
    try:
        idx = times.index(target)
    except ValueError:
        # ぴったりの時刻が無ければ直近の過去時刻を使う
        past = [t for t in times if t <= target]
        if not past:
            return None
        idx = times.index(past[-1])
        log.warning(
            "No exact weather data for %s; falling back to nearest past hour %s",
            target, past[-1],
        )

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


def fetch_weather():
    """代表地点（全33地点の座標の重心）のOpen-Meteo実況に近い直近時刻の値を取得する。
    取得失敗時はNoneを返す。2026-08-28以降はこれとは別に、fetch_weather_multi()で
    全地点分もあわせて取得している（只見川.xlsxとの互換性のため、この代表地点の値の
    書き込み自体は変えていない）。
    """
    lat, lon = representative_coords()
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(WEATHER_HOURLY_VARS),
        "wind_speed_unit": "ms",
        "timezone": "Asia/Tokyo",
        "past_days": 1,
        "forecast_days": 1,
    }
    attempts = WEATHER_FETCH_RETRIES + 1
    data = None
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            resp = request_with_dns_fallback("GET", OPEN_METEO_URL, params=params, timeout=15, log=log)
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException as e:
            last_exc = e
            log.warning(
                "Weather fetch failed (attempt %d/%d): %s", attempt, attempts, e,
            )
            if attempt < attempts:
                time.sleep(WEATHER_RETRY_DELAY_SEC)
    if data is None:
        log.error("Open-Meteo fetch failed after %d attempts", attempts)
        if last_exc is not None:
            record_network_failure("weather_fetch", str(last_exc))
        return None

    weather = _extract_weather_at_target_hour(data.get("hourly", {}))
    if weather is None:
        log.error("No matching time in Open-Meteo response")
    return weather


def fetch_weather_multi(coords_by_name):
    """全地点分の気象値を1回のAPI呼び出しでまとめて取得する(Open-Meteoは緯度経度を
    カンマ区切りで渡すと地点ごとの結果を配列で返す)。取得失敗/該当地点分の欠落は
    その地点をdictに含めないことで表す（部分的な失敗があっても他地点は活かす）。
    """
    if not coords_by_name:
        return {}
    names = list(coords_by_name.keys())
    params = {
        "latitude": ",".join(str(coords_by_name[n][0]) for n in names),
        "longitude": ",".join(str(coords_by_name[n][1]) for n in names),
        "hourly": ",".join(WEATHER_HOURLY_VARS),
        "wind_speed_unit": "ms",
        "timezone": "Asia/Tokyo",
        "past_days": 1,
        "forecast_days": 1,
    }
    attempts = WEATHER_FETCH_RETRIES + 1
    payload = None
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            resp = request_with_dns_fallback("GET", OPEN_METEO_URL, params=params, timeout=20, log=log)
            resp.raise_for_status()
            payload = resp.json()
            break
        except requests.RequestException as e:
            last_exc = e
            log.warning("Per-station weather fetch failed (attempt %d/%d): %s", attempt, attempts, e)
            if attempt < attempts:
                time.sleep(WEATHER_RETRY_DELAY_SEC)
    if payload is None:
        log.error("Per-station Open-Meteo fetch failed after %d attempts", attempts)
        if last_exc is not None:
            record_network_failure("weather_fetch_multi", str(last_exc))
        return {}

    if isinstance(payload, dict):
        payload = [payload]  # 地点が1件だけの時は単一オブジェクトで返ってくるため揃える

    result = {}
    for name, item in zip(names, payload):
        weather = _extract_weather_at_target_hour((item or {}).get("hourly", {}))
        if weather is not None:
            result[name] = weather
    if len(result) < len(names):
        log.warning("Per-station weather: got %d/%d station(s)", len(result), len(names))
    return result


def record_station_weather(run_time):
    """全地点分の気象値を取得し、station_weather.dbに記録する。失敗しても収集全体は止めない
    （只見川.xlsx側の代表地点の値は従来通り記録済みのため、地点別データが今回だけ
    欠けても致命的ではない）。"""
    try:
        coords = load_camera_coords(CAMERAS_CSV)
        if not coords:
            log.warning("No camera coordinates available; skipping per-station weather")
            return
        weather_by_name = fetch_weather_multi(coords)
        if not weather_by_name:
            return
        dt_str = run_time.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        conn = station_weather.connect(STATION_WEATHER_DB_PATH)
        try:
            for name, weather in weather_by_name.items():
                station_weather.record_weather(conn, name, dt_str, weather)
        finally:
            conn.close()
        log.info("Recorded per-station weather for %d/%d station(s)", len(weather_by_name), len(coords))
    except Exception:
        log.exception("Failed to record per-station weather")


def append_row(cameras, phenom_by_name, weather, run_time):
    """今回の観測1時間分をweather.db(weather_store.py)に記録する。同じdatetimeは
    上書き(UPSERT)されるため、以前のxlsx版のような「既存行を探す」処理は不要。"""
    # Excelとの互換用にtzinfoつきのdatetimeは保存しない(以前のxlsx版と同じ、ローカル時刻の値)。
    run_time_naive = run_time.replace(tzinfo=None)
    dt_str = run_time_naive.strftime("%Y-%m-%d %H:%M:%S")
    phenom_full = {name: phenom_by_name.get(name) for name, _cam_id in cameras}

    conn = weather_store.connect(WEATHER_DB_PATH)
    try:
        weather_store.record_weather(conn, dt_str, weather)
        weather_store.record_phenomena(conn, dt_str, phenom_full)
    finally:
        conn.close()
    log.info("Recorded observation in weather.db: %s", run_time_naive)


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
    refetch_failed_images(cameras, images_by_name)
    ok_count = sum(1 for v in images_by_name.values() if v)
    log.info("Image fetch succeeded: %d / %d", ok_count, len(cameras))

    # 気象データをGemini分類より先に取得する（分類プロンプトに補助情報として渡すため。
    # 以前はxlsx記録の直前、分類の後で取得していた）。
    weather = fetch_weather()
    if weather is None:
        log.warning("Failed to fetch weather data (all fields treated as missing)")

    phenom_by_name, invalid_stations, confidence_by_name, reasoning_by_name = classify_images(
        cameras, images_by_name, weather=weather, run_time=run_time,
    )
    log.info(
        "Gemini classification succeeded: %d / %d (invalid/placeholder: %d)",
        len(phenom_by_name), len(cameras), len(invalid_stations),
    )
    if ok_count > 0 and not phenom_by_name:
        # 画像取得自体は成功しているのにGemini分類が1件も返ってこない場合、
        # (usable_countはimages_by_nameベースで計算されるため)既存の
        # IMAGE_FAILURE_ALERT_RATIOのチェックでは検知できない、分類パイプライン全体の
        # 障害(モデル名の廃止等)を示している可能性が高いため、別途警告する。
        message = (
            "Gemini classification returned 0 result(s) despite successfully fetching "
            f"{ok_count} image(s); the classification pipeline itself may be broken "
            "(e.g. a deprecated model name)"
        )
        log.error(message)
        send_alert(message, source="collect.py")
    append_confidence_log(confidence_by_name, reasoning_by_name, run_time)

    usable_count = ok_count - len(invalid_stations)
    problem_ratio = (1.0 - usable_count / len(cameras)) if cameras else 0.0
    if problem_ratio >= IMAGE_FAILURE_ALERT_RATIO:
        message = (
            f"Many stations have no usable photo this run: "
            f"{len(cameras) - usable_count}/{len(cameras)} ({problem_ratio * 100:.0f}%) -- possible "
            "outage on the camera source site (cam.river.go.jp) or a transient network issue"
        )
        log.error(message)
        # ALERT_WEBHOOK_URLが設定されていれば通知する(未設定なら何もしない)。
        # 以前は管理画面のログを見に行かないと気付けなかった。
        send_alert(message, source="collect.py")

    save_images(cameras, images_by_name, invalid_stations, run_time)
    prune_old_images()

    # record_station_weather・generate_ai_summaries・nowcast.run_nowcast()はそれぞれ内部で
    # 自分の失敗を外に漏らさないようになっている(互いに独立であるべきという意図)が、
    # append_row自体は無防備で、ここで例外が起きるとmain()がここで止まり、後続の3つが
    # 今回ぶんだけ丸ごと実行されずに終わっていた(2026-10-23に発覚)。weather.dbへの
    # 記録はこの時間の観測そのものなので失敗したら気付けるよう通知もする。
    try:
        append_row(cameras, phenom_by_name, weather, run_time)
    except Exception as e:
        log.exception("Failed to record this hour's observation in weather.db")
        send_alert(f"weather.dbへの記録に失敗しました: {e}", source="collect.py")

    record_station_weather(run_time)
    generate_ai_summaries(cameras, phenom_by_name, weather, run_time)
    # 今の観測を入れて、その日の残りの時間の予測を更新する(nowcast.py)。失敗しても収集は成功扱い。
    try:
        import nowcast
        nowcast.run_nowcast()
    except Exception:
        log.exception("Nowcast update failed (the collection itself succeeded)")
    log.info("=== Collection finished ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("collect.py terminated abnormally")
        sys.exit(1)
