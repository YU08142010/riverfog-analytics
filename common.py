"""collect.py と backfill_weather.py で共有する定数・ヘルパー。"""
import csv
import logging
import socket
import threading
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_XLSX = BASE_DIR / "data" / "只見川.xlsx"
TEMPLATE_XLSX = BASE_DIR / "template" / "header_template.xlsx"
CAMERAS_CSV = BASE_DIR / "cameras.csv"
STATION_WEATHER_DB_PATH = BASE_DIR / "data" / "station_weather.db"

JST = timezone(timedelta(hours=9))


def load_camera_coords(cameras_csv=CAMERAS_CSV):
    """cameras.csvから {地点名: (緯度, 経度)} を読む。緯度・経度の列が無い/空の行はスキップする
    （2026-08-28にriver.go.jpの公開GeoJSONから実測値を追加した列。それより前のcameras.csvには無い）。
    """
    coords = {}
    with open(cameras_csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lat, lon = row.get("緯度"), row.get("経度")
            if lat and lon:
                coords[row["地点名"]] = (float(lat), float(lon))
    return coords

QUALITY_INFO_VALUE = 8
HOMOGENEITY_NUMBER_VALUE = 1

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


def wind_degrees_to_direction_ja(degrees):
    if degrees is None:
        return None
    nearest = min(WIND_DIR_DEGREES.values(), key=lambda d: min(abs(d - degrees), 360 - abs(d - degrees)))
    return DEGREES_TO_WIND_DIR[nearest]


def set_measure(ws, row, col_value, col_qi, col_hn, value):
    """気象値セル1組（値・QI・均質番号）に値を設定する。値がNoneなら3セルとも空にする。"""
    if value is None:
        ws.cell(row=row, column=col_value).value = None
        ws.cell(row=row, column=col_qi).value = None
        ws.cell(row=row, column=col_hn).value = None
    else:
        ws.cell(row=row, column=col_value).value = value
        ws.cell(row=row, column=col_qi).value = QUALITY_INFO_VALUE
        ws.cell(row=row, column=col_hn).value = HOMOGENEITY_NUMBER_VALUE


def set_weather_measures(ws, row, weather):
    """気象値一式（気温・降水・風・日射・日照・露点・湿度・視程）を1行分書き込む。
    weatherがNone、または個々のキーが無い/Noneの場合、該当項目は空にする。
    collect.py（毎時収集）とbackfill_weather.py（欠測の後埋め）の両方から呼ばれる。
    """
    weather = weather or {}
    set_measure(ws, row, COL_TEMP, COL_TEMP_QI, COL_TEMP_HN, weather.get("temperature"))
    set_measure(ws, row, COL_PRECIP, COL_PRECIP_QI, COL_PRECIP_HN, weather.get("precipitation"))

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

    set_measure(ws, row, COL_SOLAR, COL_SOLAR_QI, COL_SOLAR_HN, weather.get("solar_radiation_mj"))
    set_measure(ws, row, COL_SUNSHINE, COL_SUNSHINE_QI, COL_SUNSHINE_HN, weather.get("sunshine_hours"))
    set_measure(ws, row, COL_DEWPOINT, COL_DEWPOINT_QI, COL_DEWPOINT_HN, weather.get("dew_point"))
    set_measure(ws, row, COL_HUMIDITY, COL_HUMIDITY_QI, COL_HUMIDITY_HN, weather.get("humidity"))
    set_measure(ws, row, COL_VISIBILITY, COL_VISIBILITY_QI, COL_VISIBILITY_HN, weather.get("visibility_km"))


# --- DNSフォールバック ---------------------------------------------------------------
# このサーバーの名前解決はTailscale MagicDNS(100.100.100.100)を経由しており、
# Tailscale側やホストのネットワークが一瞬不安定になるだけで、天気API・カメラサイトの
# 外部ドメインの名前解決が丸ごと失敗することが分かっている(2026-08-20頃から断続的に発生)。
# システムDNSでの解決に失敗した時だけ、パブリックDNSへ直接問い合わせて再試行する。
FALLBACK_DNS_SERVERS = ["8.8.8.8", "1.1.1.1"]
_DNS_FAILURE_MARKERS = (
    "Temporary failure in name resolution",
    "Name or service not known",
    "No address associated with hostname",
    "nodename nor servname provided",
)
_dns_fallback_lock = threading.Lock()


def _is_dns_failure(exc):
    return any(marker in str(exc) for marker in _DNS_FAILURE_MARKERS)


def _resolve_via_fallback_dns(hostname, timeout=5):
    """システムDNSが失敗した時に、パブリックDNSへ直接問い合わせて解決する。
    dnspython未インストールの環境でもcommon.py自体は読み込めるよう、遅延importにする。
    """
    import dns.resolver

    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = FALLBACK_DNS_SERVERS
    resolver.timeout = timeout
    resolver.lifetime = timeout
    answer = resolver.resolve(hostname, "A")
    return str(answer[0])


def request_with_dns_fallback(method, url, log=None, **kwargs):
    """requests.request()のラッパー。システムDNSでの名前解決失敗を検知した場合のみ、
    パブリックDNS(8.8.8.8等)で直接解決したIPに対して1回だけ再試行する。
    TLS証明書検証は元のホスト名のまま行われるよう、socket.getaddrinfoをそのホスト名だけ
    一時的に差し替える(requests/urllib3の他の挙動は変えない)。フォールバックの名前解決
    自体が失敗した場合や、DNS以外のエラーの場合は、元の例外をそのまま送出する。

    ThreadPoolExecutorから並列に呼ばれる状況(collect.pyの画像取得等)を考慮し、
    socket.getaddrinfoの差し替え中は_dns_fallback_lockで排他する
    (差し替え自体は他ホスト名の解決には影響しないが、複数スレッドが同時に
    差し替え/復元を行うと互いのパッチを消し合う競合が起こりうるため)。
    """
    try:
        return requests.request(method, url, **kwargs)
    except requests.exceptions.ConnectionError as e:
        if not _is_dns_failure(e):
            raise
        hostname = urlparse(url).hostname
        if not hostname:
            raise
        try:
            ip = _resolve_via_fallback_dns(hostname)
        except Exception:
            raise e from None

        if log:
            log.warning(
                "System DNS resolution failed for %s; retrying via fallback DNS (resolved to %s)",
                hostname, ip,
            )

        with _dns_fallback_lock:
            original_getaddrinfo = socket.getaddrinfo

            def _patched_getaddrinfo(host, *args, **kw):
                if host == hostname:
                    host = ip
                return original_getaddrinfo(host, *args, **kw)

            socket.getaddrinfo = _patched_getaddrinfo
            try:
                return requests.request(method, url, **kwargs)
            finally:
                socket.getaddrinfo = original_getaddrinfo


NETWORK_FAILURE_LOG_PATH = BASE_DIR / "data" / "network_failures.csv"


def record_network_failure(source, detail, when=None, station=None):
    """DNS/ネットワーク障害を、日次の発生回数を後で集計できるようCSVに追記する
    （管理者タブでの可視化・将来の通知機能のための記録用）。書き込み自体が失敗しても
    収集処理を止めないよう、例外は握りつぶしてログにだけ残す。
    stationは障害が特定の地点に紐づく場合(画像取得失敗等)だけ渡す。天気APIのように
    地点を問わない障害はNone(空文字)のままでよい。
    """
    import csv
    from datetime import datetime

    when = when or datetime.now(JST).replace(tzinfo=None)
    try:
        NETWORK_FAILURE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not NETWORK_FAILURE_LOG_PATH.exists()
        with open(NETWORK_FAILURE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(["datetime", "source", "station", "detail"])
            writer.writerow([when.strftime("%Y-%m-%d %H:%M:%S"), source, station or "", detail])
    except Exception as e:
        logging.getLogger("common").warning("Failed to record network failure: %s", e)
