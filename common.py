"""collect.py と backfill_weather.py で共有する定数・ヘルパー。"""
import csv
import fcntl
import logging
import os
import socket
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_XLSX = BASE_DIR / "data" / "只見川.xlsx"
DATA_XLSX_LOCK_PATH = BASE_DIR / "data" / ".只見川.xlsx.lock"
TEMPLATE_XLSX = BASE_DIR / "template" / "header_template.xlsx"
CAMERAS_CSV = BASE_DIR / "cameras.csv"
STATION_WEATHER_DB_PATH = BASE_DIR / "data" / "station_weather.db"
# 只見川.xlsxに代わる代表地点気象・現象コードの実データストア(weather_store.py参照)。
# 只見川.xlsx自体は削除せず、人がExcelで見たい時のエクスポート出力先として残す。
WEATHER_DB_PATH = BASE_DIR / "data" / "weather.db"
# ダウンロード時にweather_store.export_to_xlsx()でその場に書き出すエクスポート専用ファイル。
# 移行前の只見川.xlsx(DATA_XLSX)は移行時点のスナップショットとしてそのまま残し、
# 以後はこちらだけを更新する。
EXPORT_XLSX_PATH = BASE_DIR / "data" / "只見川_エクスポート.xlsx"

JST = timezone(timedelta(hours=9))

# GEMINI_MODEL環境変数が未設定の場合のフォールバック値。collect.py・backfill_images.pyの
# 3箇所に別々にハードコードされていたのを1箇所にまとめたもの。
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


def now_jst_naive():
    """現在時刻をJSTのnaive datetime(tzinfo無し)で返す。
    predict.py/collect.py/backfill_weather.py等、xlsxやDBにJSTのnaive値として保存する
    箇所で共通利用する(各ファイルで`datetime.now(JST).replace(tzinfo=None)`が
    個別に重複していたのを1箇所にまとめたもの)。
    """
    return datetime.now(JST).replace(tzinfo=None)


# 日本国内に収まる大まかな範囲(桁間違い・緯度経度の入れ替わり等の異常値を検知する用)。
# 実際の33地点は北海道・沖縄を含まないもっと狭い範囲だが、将来別地域が増えても
# 誤検知しないよう日本全体の範囲に留めている。
_JAPAN_LAT_RANGE = (20.0, 46.0)
_JAPAN_LON_RANGE = (122.0, 154.0)


def load_camera_coords(cameras_csv=CAMERAS_CSV):
    """cameras.csvから {地点名: (緯度, 経度)} を読む。緯度・経度の列が無い/空の行はスキップする
    （2026-08-28にriver.go.jpの公開GeoJSONから実測値を追加した列。それより前のcameras.csvには無い）。
    日本国内の大まかな範囲から外れる値は、桁間違い等の異常値とみなし警告して除外する
    （representative_coordsの重心計算等が1地点の異常値で大きく狂うのを防ぐため）。
    """
    coords = {}
    with open(cameras_csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lat, lon = row.get("緯度"), row.get("経度")
            if not (lat and lon):
                continue
            lat, lon = float(lat), float(lon)
            if not (_JAPAN_LAT_RANGE[0] <= lat <= _JAPAN_LAT_RANGE[1]
                    and _JAPAN_LON_RANGE[0] <= lon <= _JAPAN_LON_RANGE[1]):
                logging.getLogger("common").warning(
                    "Ignoring out-of-range coordinates for %s: (%s, %s)",
                    row["地点名"], lat, lon,
                )
                continue
            coords[row["地点名"]] = (lat, lon)
    return coords


def representative_coords(cameras_csv=CAMERAS_CSV):
    """全33地点の座標の重心を返す。cross_rates計算やプールド救済モデルが使う
    共有気象データ("代表地点"の気象)の取得地点として使う。
    2026-08-28以前はWEATHER_LAT/WEATHER_LON環境変数で固定の1地点(只見町役場)を
    指定していたが、全地点の実測座標が揃ったため、その重心に置き換えた。
    """
    coords = load_camera_coords(cameras_csv)
    if not coords:
        return 37.34861, 139.31583
    lats = [lat for lat, _ in coords.values()]
    lons = [lon for _, lon in coords.values()]
    return sum(lats) / len(lats), sum(lons) / len(lons)


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


class XlsxLockTimeout(Exception):
    """xlsx書き込みロックを制限時間内に取得できなかった場合に送出する。"""


@contextmanager
def xlsx_write_lock(timeout=120, poll_interval=1.0, lock_path=DATA_XLSX_LOCK_PATH):
    """只見川.xlsxへの書き込み(load_workbook〜save)を排他制御する。

    collect.py・backfill_weather.py・backfill_images.pyの3スクリプトが同じxlsxを
    読み書きするが、以前はcrontabの実行時刻をずらすことだけで衝突を避けていた
    (いずれかの処理が遅延・長時間化すると書き込み競合・データ破損の恐れがあった)。
    flock()はプロセスが異常終了(kill・OOM等)しても自動的に解放されるため、
    ロックを取ったまま永久に固まる心配はない。取得自体をtimeout秒までポーリングし、
    それでも取れなければXlsxLockTimeoutを送出して呼び出し側に委ねる(無限待機はしない)。
    読み取り専用の箇所(predict.py・webapp.py等)には適用していない
    (openpyxlの保存は通常ごく短時間で完了し、Webサーバーのリクエスト処理にまで
    ロック待ちを持ち込むとハングのリスクの方が大きいと判断したため)。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = open(lock_path, "w")
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise XlsxLockTimeout(f"Could not acquire xlsx write lock within {timeout}s")
                time.sleep(poll_interval)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


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

        with _patched_dns_for_hostname(hostname, ip):
            return requests.request(method, url, **kwargs)


@contextmanager
def _patched_dns_for_hostname(hostname, ip):
    """socket.getaddrinfoを一時的に差し替え、指定したhostnameだけをipに解決させる。
    他のホスト名の解決には影響しない(host == hostnameの時だけ書き換える)。
    _dns_fallback_lockで排他しているため、複数スレッドが同時に差し替え/復元を行い
    互いのパッチを消し合う競合は起きない。例外時も必ず元に戻す。"""
    with _dns_fallback_lock:
        original_getaddrinfo = socket.getaddrinfo

        def _patched_getaddrinfo(host, *args, **kw):
            if host == hostname:
                host = ip
            return original_getaddrinfo(host, *args, **kw)

        socket.getaddrinfo = _patched_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


def build_data_backup_zip(dest_path, base_dir=BASE_DIR):
    """data/一式 + .env をZIPにまとめてdest_pathに書き出す。
    webapp.pyのadmin_backup()(手動ダウンロード)とbackup.py(定期自動バックアップ)の
    両方から使う共通ロジック(以前はwebapp.py側にだけ書かれていた)。
    """
    data_dir = base_dir / "data"
    env_path = base_dir / ".env"
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if data_dir.exists():
            for f in data_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, arcname=str(f.relative_to(data_dir)))
        if env_path.exists():
            zf.write(env_path, arcname=".env")


NETWORK_FAILURE_LOG_PATH = BASE_DIR / "data" / "network_failures.csv"


def rotate_csv_if_large(path, max_bytes=5 * 1024 * 1024, keep=3):
    """CSVが肥大化しすぎないよう、max_bytesを超えたらlogrotate同様に世代交代する
    (path.1, path.2, ... path.keepまで保持し、古いものから削除する)。
    network_failures.csv・confidence_log.csvのように追記され続けるだけで
    ローテーションの仕組みが無かったCSVに使う。
    """
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        for i in range(keep - 1, 0, -1):
            src = Path(f"{path}.{i}")
            dst = Path(f"{path}.{i + 1}")
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        rotated = Path(f"{path}.1")
        if rotated.exists():
            rotated.unlink()
        path.rename(rotated)
    except Exception:
        logging.getLogger("common").warning("Failed to rotate %s", path, exc_info=True)


def send_alert(message, source="river-fog"):
    """重大な障害(ディスク逼迫・頻繁な再起動・大量の画像取得失敗等)をプッシュ通知する。

    これまでは管理画面を開かないと気付けなかった。ALERT_WEBHOOK_URL(.env)に
    Slack等のIncoming Webhook URLを設定すると、そこにテキストをPOSTする。
    未設定なら何もしない(既定では従来通り管理画面で確認する運用のまま)。
    通知の送信自体が失敗しても、呼び出し元のバッチ処理を止めないよう例外は握りつぶす。
    """
    webhook_url = os.environ.get("ALERT_WEBHOOK_URL")
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json={"text": f"[{source}] {message}"}, timeout=10)
    except Exception as e:
        logging.getLogger("common").warning("Failed to send alert webhook: %s", e)


def record_network_failure(source, detail, when=None, station=None):
    """DNS/ネットワーク障害を、日次の発生回数を後で集計できるようCSVに追記する
    （管理者タブでの可視化・将来の通知機能のための記録用）。書き込み自体が失敗しても
    収集処理を止めないよう、例外は握りつぶしてログにだけ残す。
    stationは障害が特定の地点に紐づく場合(画像取得失敗等)だけ渡す。天気APIのように
    地点を問わない障害はNone(空文字)のままでよい。
    """
    import csv

    when = when or now_jst_naive()
    try:
        NETWORK_FAILURE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rotate_csv_if_large(NETWORK_FAILURE_LOG_PATH)
        is_new = not NETWORK_FAILURE_LOG_PATH.exists()
        with open(NETWORK_FAILURE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(["datetime", "source", "station", "detail"])
            writer.writerow([when.strftime("%Y-%m-%d %H:%M:%S"), source, station or "", detail])
    except Exception as e:
        logging.getLogger("common").warning("Failed to record network failure: %s", e)
