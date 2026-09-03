"""SMB共有(data/以下)と同じ内容をブラウザから見られるようにするダッシュボード。

LAN/Tailscale内からのみアクセスされる想定。全ページログイン必須(_require_login参照)で、
管理者専用の操作にはさらにロールチェック(requires_admin)がかかる。外部公開はしないこと。
"""
import configparser
import csv
import hmac
import io
import json
import logging
import logging.handlers
import os
import random
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from PIL import Image
from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template,
    request, send_file, send_from_directory, session, url_for,
)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager

from common import (
    BASE_DIR, CAMERAS_CSV, DATA_XLSX, EXPORT_XLSX_PATH, JST, NETWORK_FAILURE_LOG_PATH,
    STATION_WEATHER_DB_PATH, TEMPLATE_XLSX, WEATHER_DB_PATH, build_data_backup_zip,
    now_jst_naive, representative_coords, send_alert,
)
import announcements
import users
import fog_data
import fog_model
import weather_store
import prediction_log
import station_weather
from fog_features import (
    COL_DEWPOINT, COL_HUMIDITY, COL_PRECIP, COL_SOLAR, COL_SUNSHINE, COL_TEMP,
    COL_VISIBILITY, COL_WIND_DIR_DEG, COL_WIND_SPEED, PHENOM_COLORS, PHENOM_LABELS,
    SLASH_COLOR, SUMMARY_FOG_CODES, encode_phenomena_cell,
)

# collect.py・fog_graphs.py と同じ日本語フォント設定（無ければグラフの日本語が文字化けするだけで、
# 処理は止めない）。
_NOTO_CJK_JP_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
if os.path.exists(_NOTO_CJK_JP_PATH):
    font_manager.fontManager.addfont(_NOTO_CJK_JP_PATH)
    matplotlib.rcParams["font.family"] = "Noto Sans CJK JP"

load_dotenv(BASE_DIR / ".env")

ADMIN_SESSION_LIFETIME = timedelta(hours=2)

USERS_DB_PATH = BASE_DIR / "data" / "users.db"
MIN_PASSWORD_LENGTH = 8
STATION_COMMENT_MAX_LENGTH = 1000
DM_MESSAGE_MAX_LENGTH = 1000

# オンライン/オフライン表示用の閾値(この時間内にリクエストがあればオンライン扱い)。
# リアルタイム配信基盤が無いため、直近アクセスの有無で近似する簡易な方式。
ONLINE_THRESHOLD = timedelta(minutes=3)
# 毎リクエストDBに書き込むと負荷が大きいため、この間隔以上空いた時だけ更新する。
PRESENCE_TOUCH_INTERVAL = timedelta(seconds=60)

# ログイン試行のレート制限(総当たり・アカウント列挙対策)。プロセス内メモリで管理する
# シンプルな実装(waitressは単一プロセスで動かしているため、これで十分機能する)。
LOGIN_ATTEMPT_WINDOW = timedelta(minutes=15)
LOGIN_ATTEMPT_MAX = 10

# アカウント単位のロックアウト。IPベースのレート制限(上記)とは別に、特定アカウントへの
# パスワード総当たりを検知したらそのアカウント自体をロックし、正しいパスワードが来ても
# 管理者が解除するまでログインさせない(IPを変えられても意味が無いようにする)。
ACCOUNT_LOCKOUT_THRESHOLD = 5
_login_attempts_lock = threading.Lock()
_login_attempts_by_ip = {}


def _client_ip():
    return request.remote_addr or "unknown"


def _login_rate_limited(ip):
    now = datetime.now(JST)
    with _login_attempts_lock:
        attempts = [t for t in _login_attempts_by_ip.get(ip, []) if now - t < LOGIN_ATTEMPT_WINDOW]
        _login_attempts_by_ip[ip] = attempts
        return len(attempts) >= LOGIN_ATTEMPT_MAX


def _record_login_failure(ip):
    with _login_attempts_lock:
        _login_attempts_by_ip.setdefault(ip, []).append(datetime.now(JST))

IMAGE_DIR = BASE_DIR / "data" / "images"
PRED_DIR = BASE_DIR / "data" / "predictions"
DB_PATH = BASE_DIR / "data" / "predictions.db"

AVATAR_DIR = BASE_DIR / "data" / "avatars"
AVATAR_SIZE = 256
AVATAR_MAX_UPLOAD_BYTES = 8 * 1024 * 1024
# デコンプレッションボム対策: 高圧縮率の画像(小さいファイルサイズで巨大な解像度)による
# メモリ枯渇を防ぐため、Pillowの展開後ピクセル数の上限を明示的に指定する
# (アバターとして必要なサイズよりかなり余裕を持たせた値)。
Image.MAX_IMAGE_PIXELS = 40_000_000
LOGIN_HEATMAP_DAYS = 371  # GitHubの草(コントリビューショングラフ)と同じく約53週間分

# --- ファイル共有(DM添付・班の共有ファイル置き場) -------------------------------------
# 拡張子による制限は設けていない(利用者からの要望で撤廃)。1ファイルあたりのサイズ上限も
# あえて設けていないが、ディスクを使い切ってサーバー全体(霧の収集・予測パイプライン含む)を
# 巻き添えにしないよう、書き込み前に空き容量だけ確認する。
DM_ATTACHMENT_DIR = BASE_DIR / "data" / "dm_attachments"
SHARED_FILES_DIR = BASE_DIR / "data" / "shared_files"
UPLOAD_MIN_FREE_BYTES = 2 * 1024 ** 3  # このバイト数を切り込む書き込みは拒否する
# 既知の拡張子はcontent_typeの表示に使うだけで、アップロードの可否には使わない。
KNOWN_UPLOAD_CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _save_uploaded_file(file_storage, dest_dir):
    """アップロードされたファイルを保存する(拡張子による制限なし)。戻り値は
    (result, error)のタプルで、成功時はresult=(元のファイル名, 保存用ファイル名,
    content_type, サイズ)・error=None、拒否した場合(空き容量不足等)はresult=None・
    errorにエラーメッセージが入る。保存名はUUID+拡張子にし、元のファイル名は一切
    パスに使わない(拡張子を絞らない以上、パストラバーサル・同名ファイル衝突だけは
    避ける必要があるため)。
    """
    # secure_filename()は非ASCII文字を全て"_"に潰してしまい、日本語ファイル名が
    # ダウンロード時に別物になってしまう。実ディスク上のパスは下でUUIDから作る
    # stored_nameだけを使うので、表示・ダウンロード名側はパス区切り文字を除去する
    # だけでよく、文字種を制限する必要はない。
    raw_name = (file_storage.filename or "").strip().replace("\\", "/")
    original_name = raw_name.rsplit("/", 1)[-1]
    if not original_name:
        return None, "ファイル名を確認できませんでした。"
    ext = Path(original_name).suffix.lower()
    content_type = KNOWN_UPLOAD_CONTENT_TYPES.get(ext) or file_storage.content_type or "application/octet-stream"

    dest_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest_path = dest_dir / stored_name
    size = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = file_storage.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                free = shutil.disk_usage(BASE_DIR).free
                if free - len(chunk) < UPLOAD_MIN_FREE_BYTES:
                    raise IOError("disk space")
                out.write(chunk)
    except IOError:
        dest_path.unlink(missing_ok=True)
        return None, "サーバーの空き容量が不足しているため、アップロードできませんでした。"

    if _scan_file_for_malware(dest_path):
        dest_path.unlink(missing_ok=True)
        log.warning("Malware detected in upload, rejected: %s (original name: %s)", stored_name, original_name)
        _record_audit("マルウェア検知", f"アップロードされたファイル「{original_name}」からマルウェアを検知し拒否")
        return None, "このファイルはウイルス対策スキャンで問題が検出されたため、アップロードできませんでした。"
    return (original_name, stored_name, content_type, size), None


def _scan_file_for_malware(path):
    """ClamAV(clamd常駐プロセス)にclamdscan経由でスキャンさせる。デーモンが落ちている・
    タイムアウトする等スキャンできなかった場合は「検知なし」として扱う(可用性を優先し、
    スキャン基盤の一時的な不調でアップロード機能自体を止めない。ただし継続的に失敗する
    場合は管理者に気づけるようアラートする)。ウイルスを検出した場合のみTrueを返す。"""
    try:
        result = subprocess.run(
            ["clamdscan", "--fdpass", "--no-summary", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        log.warning("Malware scan could not run for %s: %s", path, e)
        _send_alert_throttled("malware_scan_unavailable", f"ウイルススキャン(clamdscan)が実行できません: {e}")
        return False
    # clamdscanの終了コード: 0=クリーン, 1=感染検出, 2=エラー(引数不正・デーモン未応答等)。
    if result.returncode == 2:
        log.warning("Malware scan returned an error for %s: %s", path, result.stdout + result.stderr)
        _send_alert_throttled(
            "malware_scan_error", f"ウイルススキャン(clamdscan)がエラーを返しました: {(result.stdout + result.stderr)[:300]}",
        )
        return False
    return result.returncode == 1


# ttydは全インターフェースにバインドし、Basic認証をかけた別プロセスとしてsystemdで動かしている
# (river-fog-terminal.service)。Tailscale/LANどちらからアクセスしても繋がるよう、iframeのURLは
# 固定せずリクエストのホスト名(request.host)に合わせて組み立てる（admin_terminal()参照）。
TTYD_PORT = 7681

# CockpitはOS標準でインストール済み(HTTPSのみ、自己署名証明書)。9090番は別オリジンかつ
# X-Frame-Options: sameoriginを送るため、他オリジンからのiframe埋め込みができない
# (自己署名証明書とは別の、ブラウザが強制するより根本的な制約)。nginx(8080番)がCockpitを
# /cockpit/配下に本アプリと同一オリジンでリバースプロキシし、admin_status.htmlはその
# 相対パスをiframeで埋め込んでいる(cockpit.conf側もUrlRoot=/cockpitに設定済み)。

ALERT_HOURS_AHEAD = 24
ALERT_MIN_FOG_PROB = 0.95
TIMELAPSE_MAX_ITEMS = 48

ALLOWED_LOGS = {
    "collect": BASE_DIR / "logs" / "collect.log",
    "predict": BASE_DIR / "logs" / "predict.log",
    "backfill_weather": BASE_DIR / "logs" / "backfill_weather.log",
    "cron": BASE_DIR / "logs" / "cron.log",
    "webapp": BASE_DIR / "logs" / "webapp.log",
}
LOG_TAIL_LINES = 400
LOG_ISSUE_RE = re.compile(r"\[(WARNING|ERROR)\]")
LOG_ISSUE_SCAN_LINES = 3000
LOG_ISSUE_MAX_TOTAL = 300

VENV_PYTHON = BASE_DIR / ".venv" / "bin" / "python"
# キー: 管理画面の「今すぐ実行」ボタンから起動できるバッチスクリプト。
RUNNABLE_SCRIPTS = {
    "collect": "collect.py",
    "predict": "predict.py",
    "backfill_weather": "backfill_weather.py",
}

SYSTEMD_UNITS = ["river-fog-collect.timer", "river-fog-collect.service", "river-fog-web.service"]
DISK_USAGE_PARTS = [
    ("カメラ画像 (images/)", IMAGE_DIR),
    ("予測結果 (predictions/)", PRED_DIR),
    ("予測DB (predictions.db)", DB_PATH),
    ("観測データ (weather.db)", WEATHER_DB_PATH),
    ("観測データ旧形式 (只見川.xlsx、移行後は更新されない)", DATA_XLSX),
    ("モデル (models/)", BASE_DIR / "data" / "models"),
]
RESTORE_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2GB
# 復元のたびにdata_before_restore_*/.env_before_restore_*が増え続けディスクを圧迫しないよう、
# 直近何世代分だけ残すか(古いものは復元成功時に自動削除する)。
RESTORE_BACKUP_KEEP_GENERATIONS = 3
# 展開後の合計サイズの上限(zip爆弾対策)。カメラ画像は既に圧縮済みで圧縮率が低いため、
# アップロード上限(2GB)よりかなり大きめに取っても正常なバックアップなら十分収まる。
RESTORE_MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024 * 1024  # 50GB
RESTORE_CONFIRM_PHRASE = "restore"

# 検証済み(答え合わせ済み)の予測レコードのうち、これより古いものは管理画面から削除できる。
DB_CLEANUP_DEFAULT_DAYS = 180

# カメラ画像アーカイブは無期限保存(collect.pyのIMAGE_RETENTION_DAYS=None)のため、
# 容量が圧迫してきた場合はこの日数をデフォルト値として管理画面から手動で古い画像を削除する。
IMAGE_CLEANUP_DEFAULT_DAYS = 30

ENV_PATH = BASE_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"
# 管理画面から直接編集してよい設定（APIキー・パスワード類は対象外。SSH側の.envで管理する）。
EDITABLE_ENV_KEYS = ["GEMINI_MODEL", "GEMINI_ESCALATION_MODEL"]

MAINTENANCE_FLAG_PATH = BASE_DIR / "data" / ".maintenance.json"

ANNOUNCEMENTS_DB_PATH = BASE_DIR / "data" / "announcements.db"
VISITOR_COOKIE_NAME = "visitor_id"
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 2  # 2年

CAMERA_HEALTH_DAYS = 7
GEMINI_LOG_DAYS = 14

CHAT_DB_PATH = BASE_DIR / "data" / "chat.db"
# 無料枠のクォータを画像分類・AI要約・エスカレーションと共有しているため、チャット単体に
# 使いすぎないよう1日あたりの上限を設ける(.envで調整可能。詳しくはGemini状況ページ参照)。
CHAT_DAILY_LIMIT_PER_USER = int(os.environ.get("AI_CHAT_DAILY_LIMIT_PER_USER", "10"))
CHAT_DAILY_LIMIT_TOTAL = int(os.environ.get("AI_CHAT_DAILY_LIMIT_TOTAL", "50"))
CHAT_QUESTION_MAX_LENGTH = 300

LOG_FILE = BASE_DIR / "logs" / "webapp.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

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
log = logging.getLogger("webapp")

# collect.pyはimportされるとモジュール読み込み時にlogging.basicConfig()を自前で呼ぶため、
# webapp.py側のbasicConfig()より先にimportしてしまうと、そちら(collect.log向け設定)が
# 先に適用されてしまいwebapp.log側の設定が無視される(basicConfigは最初の1回しか効かない)。
# 必ずここ(webapp.py自身のbasicConfig呼び出しの後)でimportする。
import chat_log
import collect

def _load_or_create_secret_key():
    """SECRET_KEYを.envから読む。無ければ生成して.envに書き込み、以後は固定する。
    以前は毎回ランダム生成していたが、webappの再起動(手動再起動・クラッシュ双方)のたびに
    全ユーザーが強制ログアウトされてしまっていたため、永続化する。"""
    existing = os.environ.get("SECRET_KEY")
    if existing:
        return existing
    new_key = secrets.token_hex(32)
    try:
        with open(ENV_PATH, "a", encoding="utf-8") as f:
            f.write(f"\nSECRET_KEY={new_key}\n")
        os.environ["SECRET_KEY"] = new_key
    except Exception:
        log.exception("Failed to persist SECRET_KEY to .env; using an in-memory key for this run")
    return new_key


app = Flask(__name__, template_folder="web_templates", static_folder="web_static")
app.secret_key = _load_or_create_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = ADMIN_SESSION_LIFETIME
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# TailscaleのIP直打ちでHTTP配信されており、Secureを付けるとブラウザがCookie自体を
# 送らなくなりログインできなくなる。HTTPS化した場合はFORCE_HTTPS_COOKIES=1を.envに
# 設定すること。
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FORCE_HTTPS_COOKIES") == "1"

try:
    STATION_NAMES = fog_data.load_station_names()
except Exception:
    STATION_NAMES = []

# メンテナンスモード中でも管理者(/admin配下)だけは常にアクセスできるようにする
# （でないと自分でオンにしたら誰もオフに戻せなくなる）。
MAINTENANCE_EXEMPT_PREFIXES = ("/admin", "/web_static", "/login", "/logout")
# サイト全体をログイン必須にする。ログイン・登録申請そのものと静的ファイルだけは例外。
LOGIN_EXEMPT_PREFIXES = ("/login", "/signup", "/logout", "/web_static")

# Tailscale Funnelで一般公開しているホスト名。nginx(8443)経由のTailscale内アクセスとは
# 別に、tailscaledがこのホスト名宛のリクエストだけを直接127.0.0.1:8081へ転送してくる
# (実機のHostヘッダーで検証済み: funnel経由はHostが必ずこの値になり、nginx経由では
# ならない)。管理者機能は、たとえ管理者ロールでログインしていてもこの経路では常に
# 使わせない(公開範囲を広げすぎないための多層防御)。
FUNNEL_PUBLIC_HOSTNAME = "sshserver.taile43041.ts.net"


def _is_funnel_request():
    return request.host.split(":")[0] == FUNNEL_PUBLIC_HOSTNAME


@app.context_processor
def _inject_is_funnel_request():
    return {"is_funnel_request": _is_funnel_request}


@app.before_request
def _check_maintenance():
    if request.path.startswith(MAINTENANCE_EXEMPT_PREFIXES):
        return None
    state = _maintenance_state()
    if not state["enabled"]:
        return None
    if session.get("role") == "admin":
        return None
    return render_template("maintenance.html", message=state["message"]), 503


@app.before_request
def _require_login():
    """サイト全体をログイン必須にする（ログイン・登録申請そのものと静的ファイルは除く）。
    メンテナンスモードのチェックが先に走るため、メンテナンス中はここに到達しない。

    ロール(session["role"])はDBの最新状態と毎回照合する。session.get("role")だけを
    信用すると、管理者がロールを剥奪・アカウント削除しても、対象者は既存セッションが
    切れるまで(最大ADMIN_SESSION_LIFETIME)そのまま操作を続けられてしまうため。
    """
    if request.path.startswith(LOGIN_EXEMPT_PREFIXES):
        return None
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login", next=request.path))
    conn = _users_conn()
    try:
        user = users.get_user_by_id(conn, user_id)
        if user is not None:
            _touch_presence_if_due(conn, user_id)
    finally:
        conn.close()
    if user is None:
        session.clear()
        flash("アカウントが削除されたため、ログアウトしました。")
        return redirect(url_for("login", next=request.path))
    session["role"] = user["role"]
    # 通知の絞り込み(お気に入り地点)等、同じリクエスト内で再度ユーザー情報が必要になる
    # 場合に備えてgに載せておく(_current_user_favorite_stations参照、二重問い合わせ防止)。
    g.current_user = user
    return None


def _touch_presence_if_due(conn, user_id):
    """オンライン判定用の最終アクセス時刻を更新する。毎リクエストDBに書き込むと
    負荷が大きいので、セッションに前回更新時刻を持たせてPRESENCE_TOUCH_INTERVAL
    以上経過している時だけ実際に書き込む。"""
    now = now_jst_naive()
    last_touch_str = session.get("_presence_touched_at")
    if last_touch_str:
        try:
            last_touch = datetime.strptime(last_touch_str, "%Y-%m-%d %H:%M:%S")
            if now - last_touch < PRESENCE_TOUCH_INTERVAL:
                return
        except ValueError:
            pass
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    users.touch_presence(conn, user_id, now_str)
    session["_presence_touched_at"] = now_str


def _is_online(last_active_str):
    if not last_active_str:
        return False
    try:
        last_active = datetime.strptime(last_active_str[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False
    return now_jst_naive() - last_active <= ONLINE_THRESHOLD


CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# CSRFチェックの対象外にするパス。/web_staticは静的ファイルの配信のみでフォーム送信はない。
CSRF_EXEMPT_PREFIXES = ("/web_static",)


def _get_or_create_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["csrf_token"] = token
    return token


@app.context_processor
def _inject_csrf_token():
    return {"csrf_token": _get_or_create_csrf_token}


@app.before_request
def _check_csrf():
    """状態変更を伴うリクエスト(GET/HEAD/OPTIONS以外)には、直前にレンダリングされた
    ページ由来のCSRFトークン(セッションに保存)と一致するトークンの送信を必須にする。
    ログイン中の利用者に別サイトから意図しないリクエストを踏ませるCSRF攻撃を防ぐ。
    フォームには{{ csrf_token() }}を隠しフィールドとして埋め込む(data-ajaxフォームは
    FormData(form)経由で自動的に含まれる)。"""
    if request.method in CSRF_SAFE_METHODS:
        return None
    if request.path.startswith(CSRF_EXEMPT_PREFIXES):
        return None
    token = session.get("csrf_token")
    submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not token or not submitted or not hmac.compare_digest(token, submitted):
        log.warning("CSRF token mismatch for %s %s", request.method, request.path)
        message = "画面の情報が古くなっています。ページを再読み込みしてからもう一度お試しください。"
        if _wants_json():
            return jsonify({"message": message, "success": False}), 400
        flash(message, "error")
        return redirect(request.referrer or url_for("dashboard"))
    return None


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HHMM_RE = re.compile(r"^\d{4}$")

STATION_FORECAST_RE = re.compile(r"^(.+)_予測_\d{8}\.png$")
SUMMARY_RE = re.compile(r"^全地点サマリー_\d{8}\.png$")
CSV_RE = re.compile(r"^予測結果_\d{8}\.csv$")


def _contrasting_text_color(hex_color):
    """背景色(#rrggbb)に対して読みやすい文字色(濃紺 or 白)を、知覚輝度から選ぶ。
    現象コードの色には#1a5276や#2c3e50のような暗い色も含まれており、
    文字色を常に濃紺で固定すると暗い背景色の上で判読できなくなる
    （ダークモード導入時に実際にこれが原因で文字が見えなくなった）。
    """
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    return "#1c2733" if brightness > 140 else "#ffffff"


def _phenom_label_color(code):
    """現象コード(0〜10、またはNaN)をラベル・背景色・文字色に変換する。"""
    if code is None:
        return "-", "#ffffff", "#1c2733"
    try:
        if code != code:  # NaN
            return "-", "#ffffff", "#1c2733"
    except TypeError:
        pass
    code = int(code)
    if code == 0:
        return "現象なし", SLASH_COLOR, _contrasting_text_color(SLASH_COLOR)
    bg = PHENOM_COLORS.get(code, "#ffffff")
    return PHENOM_LABELS.get(code, f"コード{code}"), bg, _contrasting_text_color(bg)


def _validate_date(date_str):
    if not DATE_RE.match(date_str):
        abort(404)
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        abort(404)
    return date_str


def _validate_hhmm(hhmm):
    if not HHMM_RE.match(hhmm):
        abort(404)
    return hhmm


def _validate_station(name):
    """既知の地点名(cameras.csv由来)以外は404にする。任意文字列からファイルパスを
    組み立てる箇所(タイムラプス等)があるため、ここで厳格にホワイトリスト検証する。"""
    if name not in STATION_NAMES:
        abort(404)
    return name


def _load_camera_info():
    """cameras.csvから {地点名: {カメラID, 緯度, 経度}} を読む。読めない場合は空dict。
    緯度経度は国交省「川の防災情報」(river.go.jp)が地図描画に使っている公開GeoJSON
    (/kawabou/file/gjson/scam/{市町村コード}.json)から取得した実測値であり、
    地点名からの逆ジオコーディング等による推測値ではない。
    """
    try:
        with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            info = {}
            for row in reader:
                entry = {"cam_id": row["カメラID"]}
                lat, lon = row.get("緯度"), row.get("経度")
                if lat and lon:
                    entry["lat"] = float(lat)
                    entry["lon"] = float(lon)
                info[row["地点名"]] = entry
            return info
    except Exception:
        return {}


CAMERA_INFO = _load_camera_info()


def _river_go_jp_map_url(cam_id):
    """川の防災情報(国交省 river.go.jp)の地点別地図URLを組み立てる。
    座標データが無いため独自の地図は描けないが、river.go.jpは各カメラIDに対応する
    地図ページを持っており、緯度経度なしでカメラIDのみから直接リンクできる。
    以下の導出方法は実カメラID2件(101793163, 101793203)についてriver.go.jp側の
    インデックス済みURLを目視確認して検証済み(推測ではない):
    ownCdは自治体コードで、当システムの全33カメラID(先頭"101793"で始まる)に
    共通の固定値"1793"。sysCamIdはカメラIDから先頭"10"を除いたもの。
    """
    sys_cam_id = cam_id[2:]
    return (
        "https://www.river.go.jp/kawabou/mb/tm"
        f"?zm=15&scamId={cam_id}&ownCd=1793&itmkndCd=200&sysCamId={sys_cam_id}"
    )


def _google_maps_search_url(lat, lon):
    """緯度経度でのGoogleマップ検索URL。ジオコーディング不要でその座標に直接ピンが立つ。
    新しいタブで開く用（埋め込みはできない見た目のフルGoogleマップ画面）。
    """
    return "https://www.google.com/maps/search/?api=1&query=" + quote(f"{lat},{lon}")


def _sorted_subdirs(base_dir, reverse=True):
    if not base_dir.is_dir():
        return []
    return sorted((p.name for p in base_dir.iterdir() if p.is_dir()), reverse=reverse)


def _group_dates_by_month(dates):
    """"YYYY-MM-DD"形式の日付文字列のリストを年月ごとにグループ化する。
    画像アーカイブを無期限保存にしたことで日付一覧が長期化するため、日付ボタンを
    バラバラに並べるのではなく月単位でまとめて見やすくする。元のリストの並び順
    (通常は新しい日付順)は維持したまま、連続する同じ年月をまとめるだけなので、
    降順・昇順どちらのリストを渡してもそのまま使える。
    """
    groups = []
    current_key = None
    for d in dates:
        key = d[:7]
        if key != current_key:
            year, month = d[:4], d[5:7]
            groups.append({"label": f"{year}年{int(month)}月", "days": []})
            current_key = key
        groups[-1]["days"].append({"date": d, "day": d[8:10]})
    return groups


def _users_conn():
    return users.connect(USERS_DB_PATH)


def _wants_json():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _finish(endpoint, message, success=True, extra=None, **url_kwargs):
    """通常のフォーム送信ならflash+redirect、fetch()経由のAjax送信ならJSONを返す。
    どちらで来たかはX-Requested-Withヘッダの有無で判定する（base.htmlのdata-ajaxフォーム用）。
    extraはJSON応答にだけ追加で乗せたいフィールド(ショップのその場更新用のitem_id等)。
    リダイレクト先のURLパラメータには影響しない。
    """
    if _wants_json():
        payload = {"message": message, "success": success}
        if extra:
            payload.update(extra)
        return jsonify(payload), (200 if success else 400)
    flash(message, "success" if success else "error")
    return redirect(url_for(endpoint, **url_kwargs))


def _record_audit(action, detail=""):
    """アカウント管理・データ削除など、取り消しにくい管理操作を誰がいつ行ったか記録する
    （管理者タブの「操作ログ」ページで確認できる）。実行者は現在のセッションから取る。
    """
    conn = _users_conn()
    try:
        users.record_audit(
            conn, session.get("user_id"), session.get("name"), action, detail,
            now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"),
        )
    finally:
        conn.close()


VERSION_PATH = BASE_DIR / "VERSION"


def _app_version():
    """VERSIONファイル(gitのpre-commitフックがコミットごとに更新)を読む。
    無ければ"?"を返す（バージョン表示自体が壊れても他の機能に影響させないため）。
    """
    try:
        return VERSION_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return "?"


@app.context_processor
def _inject_app_version():
    return {"app_version": _app_version()}


def _count_pending_signups():
    """保留中の登録申請件数。1リクエスト内でpending_signup_count()(サイドバー等)と
    _admin_overview()(管理者トップ)の両方から呼ばれることがあるため、flask.gで
    リクエスト単位にキャッシュし、同じ内容のDB問い合わせが重複しないようにする。
    """
    if "pending_signup_count" in g:
        return g.pending_signup_count
    try:
        conn = _users_conn()
        try:
            count = len(users.list_signup_requests(conn, status="pending"))
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to count pending signup requests: %s", e)
        count = 0
    g.pending_signup_count = count
    return count


@app.context_processor
def _inject_pending_signup_count():
    """管理者タブの「アカウント管理」に未処理件数のバッジを出すため、admin_nav.htmlから
    使えるようテンプレート全体にpending_signup_count()を注入する。管理者以外には
    バッジは出さない（admin_nav.html自体が管理者ページにしか出ないため実害はないが、
    念のためロールも見ておく）。
    """
    def pending_signup_count():
        if session.get("role") != "admin":
            return 0
        return _count_pending_signups()
    return {"pending_signup_count": pending_signup_count}


@app.context_processor
def _inject_pending_observation_report_count():
    """管理者サイドバーの「観測報告」に未対応件数のバッジを出すため注入する。"""
    def pending_observation_report_count():
        if session.get("role") != "admin":
            return 0
        conn = _users_conn()
        try:
            return users.pending_observation_report_count(conn)
        finally:
            conn.close()
    return {"pending_observation_report_count": pending_observation_report_count}


def _current_user_favorite_stations():
    """ログイン中ユーザーのお気に入り地点名のsetを返す(未ログイン/未設定なら空set)。
    空setは「絞り込み対象なし(=全地点を通知対象にする)」という意味で扱う
    (お気に入りを1つも設定していない利用者の通知を空にしてしまうと不便なため)。
    _require_loginがgに載せたユーザー情報を再利用し、二重にDBへ問い合わせない。
    """
    user = g.get("current_user")
    if user is None:
        return set()
    return {s for s in user["favorite_stations"].split(",") if s}


# カメラが直近この回数連続で使える写真を撮れていない場合、故障・レンズ曇り等の兆候として
# 通知する。観測は毎時(4-19時)なので6回はおよそ半日分に相当する。
CAMERA_FAILURE_ALERT_CONSECUTIVE = 6
CAMERA_FAILURE_CHECK_SCAN_DAYS = 3


def _stations_with_recent_camera_failures(consecutive=CAMERA_FAILURE_ALERT_CONSECUTIVE):
    """直近の観測スロットを新しい順にたどり、consecutive回連続で「使える写真」が
    無かった地点名のリストを返す。ファイル存在チェックのみでsubprocessは使わない。
    """
    slots = []
    for date in _sorted_subdirs(IMAGE_DIR)[:CAMERA_FAILURE_CHECK_SCAN_DAYS]:
        for hhmm in _sorted_subdirs(IMAGE_DIR / date):
            slots.append(IMAGE_DIR / date / hhmm)
    failing = []
    for name in STATION_NAMES:
        streak = 0
        for hour_dir in slots:
            if (hour_dir / f"{name}.jpg").exists():
                break
            if (hour_dir / f"{name}_unavailable.jpg").exists():
                streak += 1
                if streak >= consecutive:
                    break
            else:
                break
        if streak >= consecutive:
            failing.append(name)
    return failing


CAMERA_HEALTH_CACHE_SECONDS = 300
_camera_health_cache = {"data": None, "at": 0.0}
_camera_health_cache_lock = threading.Lock()


def _stations_with_recent_camera_failures_cached():
    now = time.monotonic()
    with _camera_health_cache_lock:
        cached = _camera_health_cache["data"]
        if cached is not None and now - _camera_health_cache["at"] < CAMERA_HEALTH_CACHE_SECONDS:
            return cached
    data = _stations_with_recent_camera_failures()
    with _camera_health_cache_lock:
        _camera_health_cache["data"] = data
        _camera_health_cache["at"] = now
    return data


def _current_notifications():
    """通知ベル(base.html)の一覧。一般利用者には霧アラート+カメラ故障の兆候、管理者には
    それに加えてシステム状態の通知を加える。通知はDBに記録された履歴ではなく、その時点の
    状態から毎回計算するだけ（霧アラートは_upcoming_fog_alerts_safe()、システム状態は
    _admin_overview()を再利用）。既読管理はサーバー側に持たず、base.htmlのJSがlocalStorageで行う。

    お気に入り地点を設定しているユーザーには、霧アラート・カメラ故障通知の両方をお気に入り
    地点だけに絞る(1件も設定していない場合は絞り込まず全地点分を出す)。
    """
    favorites = _current_user_favorite_stations()
    notifications = []
    nearest_by_station = {}
    for a in _upcoming_fog_alerts_safe():
        station = a["station"]
        if favorites and station not in favorites:
            continue
        if station not in nearest_by_station or a["target_datetime"] < nearest_by_station[station]["target_datetime"]:
            nearest_by_station[station] = a
    for station, a in sorted(nearest_by_station.items(), key=lambda kv: kv[1]["target_datetime"]):
        prob_pct = round((a["predicted_fog_prob"] or 0) * 100)
        notifications.append({
            "id": f"fog:{station}:{a['target_datetime']}",
            "level": "warning",
            "message": f"{station}: {a['target_datetime']}頃に川霧の予測です（確率{prob_pct}%）",
            "url": url_for("station_detail", name=station),
        })

    for station in _stations_with_recent_camera_failures_cached():
        if favorites and station not in favorites:
            continue
        notifications.append({
            "id": f"camera:{station}",
            "level": "warning",
            "message": f"{station}: カメラ画像が直近{CAMERA_FAILURE_ALERT_CONSECUTIVE}回連続で取得できていません",
            "url": url_for("station_detail", name=station),
        })

    my_id = session.get("user_id")
    if my_id:
        conn = _users_conn()
        try:
            unread = users.dm_unread_counts(conn, my_id)
            conversations = {cid: users.get_dm_conversation(conn, cid) for cid in unread}
            other_ids = [
                (c["user_b_id"] if c["user_a_id"] == my_id else c["user_a_id"])
                for c in conversations.values() if c is not None
            ]
            senders = users.get_users_by_ids(conn, other_ids)
        finally:
            conn.close()
        for cid, count in unread.items():
            conv = conversations.get(cid)
            if conv is None:
                continue
            other_id = conv["user_b_id"] if conv["user_a_id"] == my_id else conv["user_a_id"]
            other = senders.get(other_id)
            if other is None:
                continue
            notifications.append({
                "id": f"dm:{cid}",
                "level": "info",
                "message": f"{other['name']}さんから未読メッセージが{count}件あります",
                "url": url_for("dm_thread", user_id=other_id),
            })

    if session.get("role") == "admin":
        overview = _admin_overview_cached()
        if overview["disk_used_pct"] is not None and overview["disk_used_pct"] >= 85:
            notifications.append({
                "id": "sys:disk",
                "level": "danger",
                "message": f"ディスク使用率が{overview['disk_used_pct']}%です",
                "url": url_for("admin_disk"),
            })
        if not overview["collect_timer_active"]:
            notifications.append({
                "id": "sys:collect_timer",
                "level": "danger",
                "message": "毎時収集タイマーが停止しています",
                "url": url_for("admin_status"),
            })
        if overview["network_failures_24h"]:
            notifications.append({
                "id": "sys:network",
                "level": "warning",
                "message": f"直近24時間でDNS/ネットワーク障害が{overview['network_failures_24h']}件発生しています",
                "url": url_for("admin_errors"),
            })
        if overview["pending_signups"]:
            notifications.append({
                "id": "sys:pending_signups",
                "level": "info",
                "message": f"保留中の登録申請が{overview['pending_signups']}件あります",
                "url": url_for("admin_accounts"),
            })
        if overview["recent_restart_count"] >= RESTART_ALERT_THRESHOLD:
            notifications.append({
                "id": "sys:frequent_restarts",
                "level": "danger",
                "message": (
                    f"直近{RESTART_ALERT_WINDOW_HOURS}時間でwebappが{overview['recent_restart_count']}回"
                    "起動しています(クラッシュループの可能性があります)"
                ),
                "url": url_for("admin_log", key="webapp"),
            })
    return notifications


@app.context_processor
def _inject_notifications():
    def notifications_data():
        try:
            return _current_notifications()
        except Exception as e:
            log.warning("Failed to build notifications: %s", e)
            return []

    def notifications_favorites_active():
        return bool(_current_user_favorite_stations())
    return {"notifications_data": notifications_data, "notifications_favorites_active": notifications_favorites_active}


def requires_admin(view):
    """管理者ロールのアカウントでのログインを必須にする。
    HTTP Basic認証にすると、ブラウザがパスワードを覚えて自動送信し続けてしまい
    ブラウザを閉じない限り再入力を求められないため、あえてセッションCookie方式にしている
    （ADMIN_SESSION_LIFETIME分だけ無操作が続くと自動的にログアウトされる）。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        if _is_funnel_request():
            # 管理者ロールであっても、公開URL(Funnel)経由では常にブロックする。
            flash("管理者機能は公開URLからは利用できません。Tailscale経由でアクセスしてください。", "error")
            return redirect(url_for("dashboard"))
        if session.get("role") != "admin":
            flash("この操作には管理者権限が必要です。", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


# matplotlibでのグラフ描画はこの古いCPUではそれなりに重く、元データ(weather.db・
# predictions.db)はcollect.py/predict.pyが書き込む時しか変わらないのに、以前は
# アクセスのたびに毎回ゼロから再描画していた。fog_data.load_river_fog_data()の
# mtimeキャッシュと同じ考え方で、監視対象ファイルのmtimeが変わるまでPNGバイト列自体を
# キャッシュする(サーバー全体で共有、ユーザーごとではない)。
_png_cache_lock = threading.Lock()
_png_cache = {}


def _safe_mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _cached_png(cache_key, watch_paths, render_fn):
    mtimes = tuple(_safe_mtime(p) for p in watch_paths)
    with _png_cache_lock:
        cached = _png_cache.get(cache_key)
        if cached is not None and cached[0] == mtimes:
            return cached[1]
    data = render_fn()
    with _png_cache_lock:
        _png_cache[cache_key] = (mtimes, data)
    return data


def _render_line_chart_png(dates, rates, title):
    """日付ごとの的中率(%)を折れ線グラフのPNGバイト列にして返す。"""
    fig, ax = plt.subplots(figsize=(9, 3.2))
    if dates:
        x = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
        ax.plot(x, rates, marker="o", markersize=3, color="#1a5276", linewidth=1.4)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=2, maxticks=10))
        # データが1点、または期間が非常に短いと、軸の範囲をmatplotlibが自動決定できず
        # 数年単位の既定レンジにフォールバックしてしまう（目盛りが7/1・1/1ばかりになる）。
        # 明示的にデータの範囲＋余白でxlimを指定して防ぐ。
        span = x[-1] - x[0]
        pad = max(span * 0.1, timedelta(hours=12))
        ax.set_xlim(x[0] - pad, x[-1] + pad)
        fig.autofmt_xdate()
    else:
        ax.text(0.5, 0.5, "データがありません", ha="center", va="center", transform=ax.transAxes)
    ax.set_ylim(0, 100)
    ax.set_ylabel("的中率(%)")
    ax.set_title(title, fontsize=11)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# 気象条件別の川霧発生グラフの2群比較色。dataviz skillの検証済みパレットの
# カテゴリ1番目(青)・2番目(オレンジ)を固定順で使う(CVD安全性が確認済みの組み合わせ)。
_WEATHER_FOG_NO_COLOR = "#2a78d6"
_WEATHER_FOG_YES_COLOR = "#eb6834"
_WEATHER_FOG_BOXPLOT_FEATURES = [
    (COL_TEMP, "気温(℃)"), (COL_HUMIDITY, "相対湿度(％)"),
    (COL_WIND_SPEED, "風速(m/s)"), (COL_PRECIP, "降水量(mm)"),
]


def _render_weather_fog_boxplot_png(main_df, phenom_df, station_name):
    """station_nameの川霧発生有無ごとに、気象条件(気温・湿度・風速・降水量)の分布を
    箱ひげ図で比較する。同じdatetimeの気象値と現象コードを突き合わせ、SUMMARY_FOG_CODES
    (川霧のコード)に該当するかどうかで「川霧あり」「川霧なし」の2群に分ける。
    2群の識別だけなのでカテゴリ色は2色のみ使用し、標本数(n=)を各箱に添えて
    比較の信頼性が一目で分かるようにする。"""
    fig, axes = plt.subplots(2, 2, figsize=(9, 7))

    has_data = (
        main_df is not None and phenom_df is not None
        and not main_df.empty and station_name in phenom_df.columns
    )
    merged = None
    if has_data:
        merged = main_df.merge(phenom_df[["datetime", station_name]], on="datetime", how="inner")
        codes = merged[station_name].apply(encode_phenomena_cell)
        merged = merged.loc[codes.notna()].copy()
        merged["is_fog"] = codes.loc[codes.notna()].isin(SUMMARY_FOG_CODES)
        has_data = not merged.empty

    if not has_data:
        for ax in axes.flat:
            ax.axis("off")
        fig.text(0.5, 0.5, "データがありません", ha="center", va="center")
    else:
        for ax, (col, label) in zip(axes.flat, _WEATHER_FOG_BOXPLOT_FEATURES):
            values = merged[[col, "is_fog"]].dropna()
            no_fog_vals = values.loc[~values["is_fog"], col]
            fog_vals = values.loc[values["is_fog"], col]
            if no_fog_vals.empty and fog_vals.empty:
                ax.text(0.5, 0.5, "データがありません", ha="center", va="center", transform=ax.transAxes)
                ax.set_title(label, fontsize=10)
                continue
            bp = ax.boxplot(
                [no_fog_vals, fog_vals],
                tick_labels=[f"川霧なし\n(n={len(no_fog_vals)})", f"川霧あり\n(n={len(fog_vals)})"],
                patch_artist=True, widths=0.5, medianprops={"color": "#0b0b0b"},
            )
            for patch, color in zip(bp["boxes"], [_WEATHER_FOG_NO_COLOR, _WEATHER_FOG_YES_COLOR]):
                patch.set_facecolor(color)
                patch.set_alpha(0.75)
            ax.set_title(label, fontsize=10)
            ax.grid(True, axis="y", alpha=0.25)
            ax.tick_params(axis="x", labelsize=8)
        fig.suptitle(f"{station_name}: 気象条件別の川霧発生状況", fontsize=12)

    fig.tight_layout(rect=(0, 0, 1, 0.95) if has_data else (0, 0, 1, 1))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# 気象条件ごとの川霧発生時ヒストグラムのビン幅(気温は2℃刻みなど、項目ごとに固定)。
# 日射量・視程はアメダスでも観測している地点が少なく、川霧発生時に限るとさらに
# サンプル数が少なくなるが、値を捏造せずそのまま少ないn件のヒストグラムとして出す。
# 風速だけは値がほぼ弱風側に偏るため、固定刻み幅ではなく低速側を細かくした不均等な
# 区分(リストで指定)を使い、各区分を等幅の棒として並べる(区分の実際の幅は棒の幅に
# 反映しない。気象庁の観測資料等でよく使われる並べ方に合わせた)。
_COL_DEWPOINT_DEPRESSION = "気温露点差"  # 湿数(気温-露点温度)。fog_features.pyの予測用特徴量と同じ列名・定義。

_RIVER_FOG_HISTOGRAM_FEATURES = [
    (COL_TEMP, "気温(℃)", 2), (COL_HUMIDITY, "相対湿度(％)", 5),
    (COL_WIND_SPEED, "風速(m/s)", [0.2, 0.5, 1, 2, 3]), (COL_PRECIP, "降水量(mm)", 1),
    (COL_DEWPOINT, "露点温度(℃)", 2), (_COL_DEWPOINT_DEPRESSION, "湿数(℃)", 1),
    (COL_SOLAR, "日射量(MJ/m2)", 0.5),
    (COL_SUNSHINE, "日照時間(時間)", 0.1), (COL_VISIBILITY, "視程(km)", 2),
]


def _bucket_by_edges(values, edges):
    """不均等な境界値edges(例: [0, 0.2, 0.5, 1, 2, 3])で値を区分けし、
    (ラベル一覧, 区分ごとの割合(%)一覧)を返す。最初の区分は「edges[0]未満」、
    最後の区分は「edges[-1]以上」として全ての値を必ずどこかの区分に入れる。"""
    n = len(values)
    counts = [0] * (len(edges) + 1)
    for v in values:
        placed = False
        for i, e in enumerate(edges):
            if v < e:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    labels = [f"〜{edges[0]}"]
    labels += [f"{edges[i]}〜{edges[i + 1]}" for i in range(len(edges) - 1)]
    labels.append(f"{edges[-1]}〜")
    pct = [c * 100.0 / n for c in counts]
    return labels, pct


def _render_river_fog_histogram_png(main_df, phenom_df, station_names):
    """気象条件(気温・湿度・風速・降水量・露点温度・湿数・日射量・日照時間・視程)ごとに、
    川霧が発生していた瞬間の値だけをヒストグラムにする。_render_weather_fog_boxplot_png
    とは違い地点ごとに分けず、全地点分の「川霧あり」行をまとめて1つの分布として扱う
    (同じ日時でも複数地点で同時に川霧が発生していればその分だけ重みが乗る)。項目ごとに
    固定の刻み幅でビンを揃える(気温なら2℃刻み)。"""
    fig, axes = plt.subplots(3, 3, figsize=(12, 11))

    values_by_col = {col: [] for col, _, _ in _RIVER_FOG_HISTOGRAM_FEATURES}
    has_data = main_df is not None and phenom_df is not None and not main_df.empty
    if has_data:
        main_df = main_df.copy()
        main_df[_COL_DEWPOINT_DEPRESSION] = main_df[COL_TEMP] - main_df[COL_DEWPOINT]
        for station_name in station_names or []:
            if station_name not in phenom_df.columns:
                continue
            merged = main_df.merge(phenom_df[["datetime", station_name]], on="datetime", how="inner")
            codes = merged[station_name].apply(encode_phenomena_cell)
            merged = merged.loc[codes.notna()].copy()
            merged["is_fog"] = codes.loc[codes.notna()].isin(SUMMARY_FOG_CODES)
            fog_rows = merged.loc[merged["is_fog"]]
            if fog_rows.empty:
                continue
            for col, _, _ in _RIVER_FOG_HISTOGRAM_FEATURES:
                values_by_col[col].extend(fog_rows[col].dropna().tolist())
        has_data = any(values_by_col.values())

    if not has_data:
        for ax in axes.flat:
            ax.axis("off")
        fig.text(0.5, 0.5, "データがありません", ha="center", va="center")
    else:
        for ax, (col, label, step) in zip(axes.flat, _RIVER_FOG_HISTOGRAM_FEATURES):
            values = values_by_col[col]
            if not values:
                ax.text(0.5, 0.5, "データがありません", ha="center", va="center", transform=ax.transAxes)
                ax.set_title(label, fontsize=10)
                continue
            if isinstance(step, list):
                bin_labels, pct = _bucket_by_edges(values, step)
                bars = ax.bar(
                    range(len(bin_labels)), pct, color=_WEATHER_FOG_YES_COLOR, alpha=0.85,
                    edgecolor="white", linewidth=0.5, width=0.85,
                )
                ax.set_xticks(range(len(bin_labels)))
                ax.set_xticklabels(bin_labels, rotation=45, ha="right", fontsize=8)
                ax.bar_label(bars, fmt="%.1f", fontsize=7, padding=2)
            else:
                lo = (min(values) // step) * step
                hi = (max(values) // step) * step + step
                n_bins = int(round((hi - lo) / step))
                edges = [lo + i * step for i in range(n_bins + 1)]
                ax.hist(
                    values, bins=edges, color=_WEATHER_FOG_YES_COLOR, alpha=0.85,
                    edgecolor="white", linewidth=0.5, rwidth=0.92,
                    weights=[100.0 / len(values)] * len(values),
                )
                ax.tick_params(axis="x", labelsize=8)
            ax.set_title(f"{label}(n={len(values)})", fontsize=10)
            ax.set_ylabel("割合(%)")
            ax.grid(True, axis="y", alpha=0.25)
        fig.suptitle("全地点合算：川霧発生時の気象条件ヒストグラム", fontsize=12)

    fig.tight_layout(rect=(0, 0, 1, 0.95) if has_data else (0, 0, 1, 1))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# 1日分の観測値推移グラフの項目(列名, 表示ラベル, 色, プロット種別)。
# 以前は気温・視程を左軸、湿度・降水量を右軸(twinx)にまとめた2軸グラフだったが、
# 軸を読み違えやすいため、項目ごとに軸を分けた4分割の小さい図に変更した。
_WEATHER_TIMESERIES_ITEMS = [
    ("気温(℃)", "気温(℃)", "#e74c3c", "line"),
    ("視程(km)", "視程(km)", "#16a085", "line"),
    ("相対湿度(％)", "相対湿度(％)", "#8e44ad", "line"),
    ("降水量(mm)", "降水量(mm)", "#2980b9", "bar"),
]


def _render_weather_timeseries_png(day_main, date_label):
    """1日分の気温・視程・湿度・降水量の推移をPNGにして返す。項目ごとに軸を分けた
    2x2の小さい図にする(2軸グラフだと目盛りを読み違えやすいため)。"""
    fig, axes = plt.subplots(2, 2, figsize=(9, 6), sharex=True)
    if day_main is None or day_main.empty:
        for ax in axes.flat:
            ax.axis("off")
        fig.text(0.5, 0.5, "データがありません", ha="center", va="center")
    else:
        x = day_main["datetime"]
        for ax, (col, label, color, kind) in zip(axes.flat, _WEATHER_TIMESERIES_ITEMS):
            if kind == "bar":
                ax.bar(x, day_main[col], width=1 / 48, color=color, alpha=0.7)
                ax.set_ylim(bottom=0)
            else:
                ax.plot(x, day_main[col], color=color, linewidth=1.4, marker="o", markersize=3)
            ax.set_title(label, fontsize=10)
            ax.grid(True, alpha=0.3)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        fig.autofmt_xdate()
        fig.suptitle(f"{date_label} の観測値推移", fontsize=12)

    fig.tight_layout(rect=(0, 0, 1, 0.95) if day_main is not None and not day_main.empty else (0, 0, 1, 1))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _accuracy_trend_safe(station=None):
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            return prediction_log.accuracy_trend(conn, station=station)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to read accuracy trend: %s", e)
        return []


def _load_metrics_history_trend():
    """fog_model.append_metrics_historyが書く履歴CSVから、学習日ごとの
    平均accuracy・平均霧F1(%)を計算する（『精度が向上しているか』を見るための集計）。
    """
    if not fog_model.METRICS_HISTORY_PATH.exists():
        return []
    by_date = {}
    try:
        with open(fog_model.METRICS_HISTORY_PATH, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                date = (row.get("trained_at") or "")[:10]
                if not date:
                    continue
                entry = by_date.setdefault(date, {"acc_sum": 0.0, "acc_n": 0, "fog_sum": 0.0, "fog_n": 0})
                acc = row.get("accuracy")
                if acc:
                    entry["acc_sum"] += float(acc)
                    entry["acc_n"] += 1
                fog_f1 = row.get("fog_f1")
                if fog_f1:
                    entry["fog_sum"] += float(fog_f1)
                    entry["fog_n"] += 1
    except Exception as e:
        log.warning("Failed to read metrics history: %s", e)
        return []

    trend = []
    for date in sorted(by_date.keys()):
        e = by_date[date]
        trend.append({
            "date": date,
            "accuracy": (e["acc_sum"] / e["acc_n"] * 100.0) if e["acc_n"] else None,
            "fog_f1": (e["fog_sum"] / e["fog_n"] * 100.0) if e["fog_n"] else None,
        })
    return trend


def _render_metrics_trend_png(trend):
    """学習時のaccuracy・霧F1（全地点平均、%）の推移を折れ線グラフのPNGにして返す。"""
    fig, ax = plt.subplots(figsize=(9, 3.2))
    all_dates = [datetime.strptime(t["date"], "%Y-%m-%d") for t in trend]
    acc_pts = [(d, t["accuracy"]) for d, t in zip(all_dates, trend) if t["accuracy"] is not None]
    fog_pts = [(d, t["fog_f1"]) for d, t in zip(all_dates, trend) if t["fog_f1"] is not None]

    if all_dates:
        if acc_pts:
            xs, ys = zip(*acc_pts)
            ax.plot(xs, ys, marker="o", markersize=3, color="#1a5276", linewidth=1.4, label="accuracy(平均,%)")
        if fog_pts:
            xs, ys = zip(*fog_pts)
            ax.plot(xs, ys, marker="s", markersize=3, color="#c0392b", linewidth=1.4, label="霧F1(平均,%)")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=2, maxticks=10))
        span = max(all_dates) - min(all_dates)
        pad = max(span * 0.1, timedelta(hours=12))
        ax.set_xlim(min(all_dates) - pad, max(all_dates) + pad)
        fig.autofmt_xdate()
        ax.legend(loc="upper left", fontsize=8)
    else:
        ax.text(0.5, 0.5, "データがありません", ha="center", va="center", transform=ax.transAxes)

    ax.set_ylim(0, 100)
    ax.set_ylabel("スコア(%)")
    ax.set_title("学習時のモデル精度推移(全地点平均)", fontsize=11)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _is_valid_number(v):
    """NoneでもNaNでもない数値かを判定する（NaN != NaNを利用、numpyを追加importしない）。"""
    return v is not None and v == v


def _render_model_compare_png(state):
    """RandomForest単体・XGBoost単体・LightGBM単体・RF+XGBoost・RF+LightGBM・
    RF+XGBoost+LightGBMの霧F1平均を棒グラフで比較する。地点ごとに実際に採用される
    モデル方式はこのうち検証データでの正答率が最も高かったものが自動選択されるため
    (fog_model._fit_best_model参照)、「採用中」バーは方式が地点によって異なる
    ものの平均を示す。
    """
    labels = [
        "RandomForest単体", "XGBoost単体", "LightGBM単体",
        "RF+XGBoost", "RF+LightGBM", "RF+XGBoost+LightGBM", "採用中(自動選択)",
    ]
    keys = ["rf_only", "xgb_only", "lgbm_only", "hybrid_only", "rf_lgbm_only", "triple_only", None]
    colors = ["#7f8c8d", "#8e44ad", "#27ae60", "#c0392b", "#d35400", "#16a085", "#1a5276"]
    values, counts = [], []
    for key in keys:
        vals = []
        for info in state.values():
            if key is None:
                v = info.get("fog_f1")
            else:
                sub = info.get(key)
                v = sub.get("fog_f1") if sub else None
            if _is_valid_number(v):
                vals.append(float(v) * 100.0)
        values.append(sum(vals) / len(vals) if vals else None)
        counts.append(len(vals))

    fig, ax = plt.subplots(figsize=(10.5, 4.2))
    x = list(range(len(labels)))
    plotted = [v if v is not None else 0 for v in values]
    ax.bar(x, plotted, color=colors, width=0.55)
    for i, (v, n) in enumerate(zip(values, counts)):
        label = f"{v:.1f}%\n(n={n}地点)" if v is not None else "データなし"
        ax.text(i, (v or 0) + 2, label, ha="center", fontsize=8.5)
    ax.set_xticks(x)
    # 7方式分のラベル(特に"RF+XGBoost+LightGBM"のような長いもの)が横並びだと重なるため、
    # 斜めにして間隔を確保する。
    ax.set_xticklabels(labels, fontsize=8.5, rotation=20, ha="right")
    ax.set_ylim(0, 100)
    ax.set_ylabel("霧F1(平均, %)")
    ax.set_title("モデル方式ごとの性能比較(全地点平均)", fontsize=11)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _render_feature_importance_png(importance, top_n=15):
    """特徴量重要度の上位top_n件を横棒グラフにする。"""
    top = importance[:top_n]
    fig_h = max(3.0, 0.35 * max(len(top), 1))
    fig, ax = plt.subplots(figsize=(7.2, fig_h))
    if not top:
        ax.text(0.5, 0.5, "データがありません", ha="center", va="center", transform=ax.transAxes)
    else:
        names = [n for n, _ in top][::-1]
        vals = [v for _, v in top][::-1]
        ax.barh(names, vals, color="#1a5276")
        ax.set_xlabel("重要度(平均)")
        for i, v in enumerate(vals):
            ax.text(v, i, f" {v:.3f}", va="center", fontsize=8)
    ax.set_title("特徴量の重要度(上位)", fontsize=11)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _render_confusion_matrix_png(labels, matrix):
    """全地点のテストデータを合算した混同行列をヒートマップで表示する。"""
    fig, ax = plt.subplots(figsize=(4.6, 4.0))
    if matrix is None or len(labels) == 0:
        ax.text(0.5, 0.5, "データがありません", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
    else:
        label_texts = [PHENOM_LABELS.get(c, f"コード{c}") for c in labels]
        im = ax.imshow(matrix, cmap="Blues")
        ax.set_xticks(range(len(labels)))
        ax.set_yticks(range(len(labels)))
        ax.set_xticklabels(label_texts, fontsize=9, rotation=30, ha="right")
        ax.set_yticklabels(label_texts, fontsize=9)
        ax.set_xlabel("予測")
        ax.set_ylabel("実際")
        vmax = matrix.max() if matrix.size else 0
        for i in range(matrix.shape[0]):
            for j in range(matrix.shape[1]):
                v = matrix[i, j]
                color = "white" if vmax and v > vmax * 0.6 else "#1c2733"
                ax.text(j, i, str(int(v)), ha="center", va="center", color=color, fontsize=10)
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    ax.set_title("混同行列(全地点合算)", fontsize=11)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _low_accuracy_stations(state, n=5):
    """accuracyが低い順にn地点の名前を返す(精度未算出の地点は除く)。"""
    scored = [
        (name, info.get("accuracy"))
        for name, info in state.items()
        if _is_valid_number(info.get("accuracy"))
    ]
    scored.sort(key=lambda t: t[1])
    return [name for name, _ in scored[:n]]


def _upcoming_fog_alerts_safe():
    if not DB_PATH.exists():
        return []
    now_naive = now_jst_naive()
    from_str = now_naive.strftime("%Y-%m-%d %H:%M:%S")
    to_str = (now_naive + timedelta(hours=ALERT_HOURS_AHEAD)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            alerts = prediction_log.upcoming_fog_alerts(conn, from_str, to_str, SUMMARY_FOG_CODES)
        finally:
            conn.close()
        return [
            a for a in alerts
            if a["predicted_fog_prob"] is not None and a["predicted_fog_prob"] >= ALERT_MIN_FOG_PROB
        ]
    except Exception as e:
        log.warning("Failed to read upcoming fog alerts: %s", e)
        return []


def _station_timelapse_images(station, max_items=TIMELAPSE_MAX_ITEMS):
    """指定地点の直近の画像を古い→新しい順で返す（スライドショー用）。"""
    items = []
    for date in _sorted_subdirs(IMAGE_DIR):
        date_dir = IMAGE_DIR / date
        for hhmm in _sorted_subdirs(date_dir):
            hour_dir = date_dir / hhmm
            for suffix in ("", "_unavailable"):
                f = hour_dir / f"{station}{suffix}.jpg"
                if f.exists():
                    items.append({"date": date, "hhmm": hhmm, "file": f.name,
                                  "unavailable": bool(suffix)})
                    break
            if len(items) >= max_items:
                break
        if len(items) >= max_items:
            break
    items.reverse()
    return items


def _station_latest_forecast_file(station):
    """最新の予測日付における、この地点の予測グラフファイル名を返す(無ければNone)。"""
    latest_pred_date = next(iter(_sorted_subdirs(PRED_DIR)), None)
    if not latest_pred_date:
        return None, None
    pred_dir = PRED_DIR / latest_pred_date
    for p in pred_dir.iterdir():
        m = STATION_FORECAST_RE.match(p.name)
        if m and m.group(1) == station:
            return latest_pred_date, p.name
    return latest_pred_date, None


def _recent_log_issues(max_per_log=LOG_ISSUE_SCAN_LINES, max_total=LOG_ISSUE_MAX_TOTAL):
    """各ログファイルの末尾からWARNING/ERROR行だけを抜き出し、新しい順にまとめて返す。"""
    issues = []
    for key, path in ALLOWED_LOGS.items():
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-max_per_log:]
        except Exception as e:
            log.warning("Failed to read log for issue scan: %s: %s", key, e)
            continue
        for line in lines:
            m = LOG_ISSUE_RE.search(line)
            if m:
                issues.append({"log": key, "level": m.group(1), "text": line.rstrip("\n")})
    # 各行の先頭が "YYYY-MM-DD HH:MM:SS,mmm" 形式であることを前提に、文字列としてソートすれば
    # そのまま時系列になる。
    issues.sort(key=lambda x: x["text"][:23], reverse=True)
    return issues[:max_total]


# このログメッセージが短時間に何度も出る場合、クラッシュループか頻繁な手動再起動が
# 起きている兆候(以前、実際に約1.5日で65回起動していたことがあった)。
RESTART_LOG_MARKER = "Starting river-fog webapp"
RESTART_ALERT_WINDOW_HOURS = 24
RESTART_ALERT_THRESHOLD = 10


def _recent_restart_count(hours=RESTART_ALERT_WINDOW_HOURS):
    """webapp.logの起動ログを直近hours時間分だけ数える(頻繁な再起動・クラッシュループの検知用)。"""
    if not LOG_FILE.exists():
        return 0
    cutoff = now_jst_naive() - timedelta(hours=hours)
    count = 0
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-LOG_ISSUE_SCAN_LINES:]
    except Exception as e:
        log.warning("Failed to read webapp log for restart count: %s", e)
        return 0
    for line in lines:
        if RESTART_LOG_MARKER not in line:
            continue
        try:
            ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts >= cutoff:
            count += 1
    return count


AI_SUMMARY_PATH = BASE_DIR / "data" / "ai_summary.json"


def _load_ai_summary():
    """collect.py(generate_ai_summaries)が毎時保存するAI要約を読む。
    無い/壊れている場合はNoneを返す(テンプレート側は要約カードごと出さない)。
    """
    if not AI_SUMMARY_PATH.exists():
        return None
    try:
        with open(AI_SUMMARY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("Failed to read AI summary: %s", e)
        return None


FEATURE_SNAPSHOT_PATH = BASE_DIR / "data" / "models" / "feature_snapshot.json"


def _load_station_feature_snapshot(name):
    """predict.py(_save_feature_snapshot)が保存した、直近の予測に使われた特徴量の値を
    指定地点分だけ読む。無い/壊れている/該当地点が無い場合はNoneを返す
    (station_detail.htmlは「予測の内訳」セクションごと出さない)。
    """
    if not FEATURE_SNAPSHOT_PATH.exists():
        return None
    try:
        with open(FEATURE_SNAPSHOT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("Failed to read feature snapshot: %s", e)
        return None
    station_data = data.get("stations", {}).get(name)
    if not station_data:
        return None
    station_data = {**station_data, "model_type": fog_model.normalize_model_type_label(station_data.get("model_type"))}
    return {"generated_at": data.get("generated_at"), **station_data}


# ============================================================================
# AIチャット(Gemini)。無料枠のクォータを画像分類・AI要約と共有しているため、
# CHAT_DAILY_LIMIT_PER_USER/CHAT_DAILY_LIMIT_TOTALで1日あたりの質問数を制限する。
# 会話履歴はサーバー側に保持せず(トークン消費を抑えるため)、直近の状況を含む
# システムプロンプトのみを毎回組み立てて単発の質問として送る。
# ============================================================================

CHAT_SYSTEM_INSTRUCTION = (
    "あなたは只見川流域の川霧観測・予測システム「只見川霧観測」のAIアシスタントです。"
    "以下に渡す現在の観測状況・今後数日の予測・予測精度・モデルやサーバーの状態・"
    "システムの仕組みをもとに、"
    "利用者からの質問に日本語で簡潔に答えてください。\n"
    "\n"
    "【このシステムの仕組み(常に正しい前提知識として使ってよい)】\n"
    "- 霧の発生予測には、RandomForest単体・XGBoost単体・LightGBM単体、およびRF+XGBoost・"
    "RF+LightGBM・RF+XGBoost+LightGBMのブレンドという複数のモデル方式を候補として地点ごとに"
    "学習し、検証データでの正答率が最も高かった方式を地点ごとに自動選択して使っている"
    "(地点によって採用される方式は異なる)。ブレンド方式が採用された場合の混合比"
    "(ブレンド重み)も地点ごとに検証データから自動選択される。\n"
    "- 観測データが少ない地点は、単独では十分な精度のモデルを学習できないため、複数地点をまとめた"
    "「pooled」モデル(地点別霧発生率などの特徴量を加えた共通モデル)で代替している。\n"
    "- 霧と判定するしきい値(fog_threshold)は0.5固定ではなく、地点ごとに検証データでF1が"
    "最良になる値を自動選択している。\n"
    "- 特徴量には気温・湿度・降水量・視程などの気象観測値に加え、その地点の過去の霧発生率や、"
    "他地点も含めた広域の霧の出やすさを示す指数なども使っている。\n"
    "- 定期的な再学習のたびに、新モデルが統計的に有意に前のモデルより良いと確認できない場合は、"
    "自動的に前のモデルへロールバックする安全機構がある(精度低下を防ぐため)。\n"
    "- 予測精度がなぜ変動するか(季節性、学習データの偏り、直近の異常な気象、モデルの再学習・"
    "自動ロールバック等)といった、システムの仕組みや機械学習全般に関する質問には、"
    "上記の前提知識・渡された数値データ・一般的な機械学習の知識をもとに答えて構わない。\n"
    "\n"
    "【このサーバーについて(常に正しい前提知識として使ってよい)】\n"
    "- 本システムは、学校に設置されている2012年モデルのMacBook Pro(Apple MacBookPro9,2、"
    "Intel Core i7-3520M @2.90GHz、メモリ約7GB、ストレージ54GB)上でUbuntu Linuxを動かして"
    "個人で運用している。\n"
    "- サーバーへのアクセスはTailscale(VPN)経由に限定しており、インターネットには一般公開して"
    "いない。負荷・メモリ使用率・稼働状況等の実際の数値は、都度渡される「サーバーの状態」"
    "「主要サービスの稼働状況」を参照する。\n"
    "\n"
    "【回答の際の注意】\n"
    "- 渡されていない具体的な情報(渡された時刻より先の詳細な天気予報、渡されていない地点の状況、"
    "ソースコードの正確な行数等)は推測で答えず、「その情報は分かりません」と正直に伝える。\n"
    "- 観測・予測はいずれもAIやセンサーによる自動判定であり、100%正確とは限らないことを"
    "必要に応じて伝える。\n"
    "- 安全に関わる最終判断(通行止め・避難等)を促す必要がある場合は、必ず自治体や河川管理者の"
    "公式情報も確認するよう案内する。\n"
    "- 只見川の霧やこのシステムの仕組みと全く関係のない質問(一般雑学等)には、"
    "「この機能は只見川の霧観測に関する質問向けです」と丁寧に答える。"
)


def _build_chat_context():
    """現在の観測・予測状況・サーバー状態をテキストにまとめる(チャットのシステムプロンプトに含める)。"""
    lines = [f"【バージョン】v{_app_version()}"]
    observations = _latest_observations()
    if observations:
        lines.append(f"【現在の観測】{observations['datetime']} 時点")
        if observations.get("temp") is not None:
            lines.append(
                f"代表地点の気象: 気温{observations.get('temp')}℃ "
                f"湿度{observations.get('humidity')}% 降水量{observations.get('precip')}mm "
                f"視程{observations.get('visibility')}km"
            )
        notable = [s for s in observations["stations"] if s["label"] not in ("現象なし", "-")]
        if notable:
            lines.append("現在なんらかの現象が出ている地点: " + "、".join(
                f"{s['name']}({s['label']})" for s in notable
            ))
        else:
            lines.append("現在、霧・層雲等の現象が出ている地点はありません。")
    else:
        lines.append("現在の観測データは取得できていません。")

    ai_summary = _load_ai_summary()
    if ai_summary and ai_summary.get("overall"):
        lines.append(f"【AIによる状況要約】({ai_summary.get('generated_at')}生成) {ai_summary['overall']}")

    alerts = _upcoming_fog_alerts_safe()
    if alerts:
        lines.append("【今後24時間以内の川霧アラート(予測確率95%以上)】")
        for a in alerts[:10]:
            prob_pct = round((a["predicted_fog_prob"] or 0) * 100)
            lines.append(f"- {a['station']}: {a['target_datetime']}頃(確率{prob_pct}%)")
    else:
        lines.append("現在、今後24時間以内の川霧アラートは出ていません。")

    if DB_PATH.exists():
        try:
            now_naive = now_jst_naive()
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            try:
                wider_alerts = prediction_log.upcoming_fog_alerts(
                    conn,
                    now_naive.strftime("%Y-%m-%d %H:%M:%S"),
                    (now_naive + timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S"),
                    SUMMARY_FOG_CODES,
                )
            finally:
                conn.close()
            wider_alerts = sorted(
                (a for a in wider_alerts if a["predicted_fog_prob"] is not None),
                key=lambda a: a["predicted_fog_prob"], reverse=True,
            )
            if wider_alerts:
                lines.append("【今後72時間以内の川霧予測(確率が高い順、上位10件)】")
                for a in wider_alerts[:10]:
                    prob_pct = round((a["predicted_fog_prob"] or 0) * 100)
                    lines.append(f"- {a['station']}: {a['target_datetime']}頃(確率{prob_pct}%)")
        except Exception as e:
            log.warning("Failed to read wider forecast for chat context: %s", e)

    accuracy = _accuracy_summary_safe()
    overall = accuracy.get("overall") if accuracy else None
    if overall and overall.get("n"):
        overall_pct = round(100.0 * overall["correct"] / overall["n"], 1)
        lines.append(f"【予測精度(全期間累計)】{overall['correct']}/{overall['n']}件 正解率{overall_pct}%")
        by_lead = [r for r in (accuracy.get("by_lead_day") or []) if r.get("n")]
        if by_lead:
            lead_parts = [
                f"{r['lead_day']}日後:{round(100.0 * r['correct'] / r['n'], 1)}%"
                for r in by_lead[:5]
            ]
            lines.append("リードタイム別の正解率: " + "、".join(lead_parts))
        by_station = [r for r in (accuracy.get("by_station") or []) if r.get("n")]
        if by_station:
            worst = sorted(by_station, key=lambda r: r["correct"] / r["n"])[:3]
            worst_parts = [
                f"{r['station']}({round(100.0 * r['correct'] / r['n'], 1)}%)" for r in worst
            ]
            lines.append("正解率が低い地点(下位3件): " + "、".join(worst_parts))
        trend = [r for r in _accuracy_trend_safe() if r.get("n")]
        if len(trend) >= 7:
            recent = trend[-7:]
            recent_n = sum(r["n"] for r in recent)
            recent_correct = sum(r["correct"] for r in recent)
            if recent_n:
                recent_pct = round(100.0 * recent_correct / recent_n, 1)
                lines.append(
                    f"直近7日間の正解率: {recent_pct}%(全期間累計{overall_pct}%との比較用)"
                )
    else:
        lines.append("予測精度の集計データはまだありません。")

    try:
        model_state = fog_model.load_model_state()
    except Exception:
        model_state = {}
    if model_state:
        n_total = len(model_state)
        n_pooled = sum(1 for info in model_state.values() if info.get("pooled"))
        accs = [info["accuracy"] for info in model_state.values() if _is_valid_number(info.get("accuracy"))]
        avg_acc_part = ""
        if accs:
            avg_acc_part = f"、検証accuracy平均{round(100.0 * sum(accs) / len(accs), 1)}%"
        lines.append(f"【モデル状況】学習済み{n_total}地点(うちpooledモデル{n_pooled}地点){avg_acc_part}")
        low_acc = _low_accuracy_stations(model_state, n=3)
        if low_acc:
            lines.append("検証accuracyが低い地点(下位3件): " + "、".join(low_acc))
        model_type_parts = [
            f"{name}: {fog_model.normalize_model_type_label(info.get('model_type'))}"
            for name, info in model_state.items()
        ]
        if model_type_parts:
            lines.append("【地点ごとの採用モデル方式(全地点)】" + "、".join(model_type_parts))

    predict_log_path = ALLOWED_LOGS.get("predict")
    if predict_log_path and predict_log_path.exists():
        try:
            with open(predict_log_path, encoding="utf-8", errors="replace") as f:
                rollback_lines = [ln.rstrip("\n") for ln in f if "Rolled back to previous model" in ln]
            if rollback_lines:
                lines.append(
                    "【直近のモデル自動ロールバック】(新モデルが有意に改善しなかったため元に戻した記録)"
                )
                for ln in rollback_lines[-5:]:
                    lines.append("- " + ln)
        except Exception:
            pass

    resources = _read_server_resources()
    if resources.get("load1") is not None or resources.get("mem_used_pct") is not None:
        parts = []
        if resources.get("load1") is not None:
            parts.append(
                f"CPU負荷(1/5/15分平均) {resources['load1']:.2f}/{resources['load5']:.2f}/"
                f"{resources['load15']:.2f}"
            )
        if resources.get("mem_used_pct") is not None:
            parts.append(f"メモリ使用率{resources['mem_used_pct']}%")
        if resources.get("temp_c") is not None:
            parts.append(f"CPU温度{resources['temp_c']:.1f}℃")
        lines.append("【サーバーの状態】" + "、".join(parts))

    unit_states = []
    for unit in SYSTEMD_UNITS:
        info = _systemctl_show(unit, ["ActiveState"])
        unit_states.append(f"{unit}:{info.get('ActiveState', '不明')}")
    if unit_states:
        lines.append("【主要サービスの稼働状況】" + "、".join(unit_states))

    camera_rows = [r for r in _camera_uptime_stats_cached() if r.get("total")]
    if camera_rows:
        worst_cameras = camera_rows[:3]
        camera_parts = [
            f"{r['station']}({round(r['rate'], 1)}%)" for r in worst_cameras if r.get("rate") is not None
        ]
        if camera_parts:
            lines.append(
                f"【カメラ画像取得の稼働率(直近{CAMERA_HEALTH_DAYS}日間、低い順)】"
                + "、".join(camera_parts)
            )

    net = _network_failure_summary()
    if net["count_24h"] or net["count_7d"]:
        lines.append(
            f"【DNS/ネットワーク障害】過去24時間{net['count_24h']}件、過去7日間{net['count_7d']}件"
            "(このサーバーの名前解決はTailscale経由のため、断続的に外部サイトへのアクセスが"
            "一時的に失敗することがある既知の症状)"
        )

    return "\n".join(lines)


def _generate_chat_title(question, answer, api_keys):
    """新しい会話の最初のやり取りから、一覧に出す短い題名を生成する(Gemini/Claude等の
    チャットUIと同じ挙動)。失敗しても会話自体は使えるべきなので、生成できなければ
    質問文の先頭を代わりに使う。この呼び出しは利用者の1日あたりの質問回数には数えない
    (レート制限はchat_messagesの行数で数えており、この呼び出しはそこに記録しないため)。"""
    fallback = question.strip()[:20] or "新しいチャット"
    if not api_keys:
        return fallback
    try:
        from google.genai import types
        model = os.environ.get("GEMINI_MODEL", collect.DEFAULT_GEMINI_MODEL)
        config = types.GenerateContentConfig(
            system_instruction=(
                "次の質問と回答の内容を表す、15文字以内の簡潔な日本語の題名だけを"
                "出力してください。句読点・記号・引用符・「題名:」等の前置きは付けないでください。"
            ),
            temperature=0.2,
            max_output_tokens=30,
        )
        content = f"質問: {question}\n回答: {(answer or '')[:300]}"
        response = collect._gemini_generate_with_fallback(
            model, content, config, api_keys, label="chat_title"
        )
        if response and getattr(response, "text", None):
            title = response.text.strip().strip('「」"\'').splitlines()[0].strip()[:20]
            if title:
                return title
    except Exception as e:
        log.warning("Chat title generation failed: %s", e)
    return fallback


def _chat_rate_limit_status(user_id):
    """(利用者ごとの残り回数, サイト全体の残り回数)を返す。0以下なら制限に達している。"""
    today_start = now_jst_naive().strftime("%Y-%m-%d 00:00:00")
    conn = chat_log.connect(CHAT_DB_PATH)
    try:
        used_by_user = chat_log.count_user_messages_since(conn, user_id, today_start)
        used_total = chat_log.count_all_messages_since(conn, today_start)
    finally:
        conn.close()
    return CHAT_DAILY_LIMIT_PER_USER - used_by_user, CHAT_DAILY_LIMIT_TOTAL - used_total


@app.route("/chat")
def chat_page():
    user_id = session.get("user_id")
    conn = chat_log.connect(CHAT_DB_PATH)
    try:
        conversations = chat_log.list_conversations(conn, user_id)
        conversation = None
        history = []
        if request.args.get("new") != "1":
            conv_id = request.args.get("c", type=int)
            if conv_id is None and conversations:
                conv_id = conversations[0]["id"]
            if conv_id is not None:
                conversation = chat_log.get_conversation(conn, conv_id, user_id)
                if conversation:
                    history = chat_log.get_conversation_messages(conn, conversation["id"])
    finally:
        conn.close()
    return render_template(
        "chat.html", conversations=conversations, conversation=conversation, history=history,
    )


@app.route("/chat/<int:conversation_id>/delete", methods=["POST"])
def chat_delete(conversation_id):
    conn = chat_log.connect(CHAT_DB_PATH)
    try:
        deleted = chat_log.delete_conversation(conn, conversation_id, session.get("user_id"))
    finally:
        conn.close()
    if _wants_json():
        return jsonify({"success": deleted})
    flash("会話を削除しました。" if deleted else "会話が見つかりませんでした。", "success" if deleted else "error")
    return redirect(url_for("chat_page"))


@app.route("/chat/status")
def chat_status():
    """チャットパネルを開いた時にだけ残り質問数を取得する(全ページで毎回DBを
    引かずに済むよう、必要になった時だけ読む)。"""
    remaining_user, remaining_total = _chat_rate_limit_status(session.get("user_id"))
    return jsonify({
        "remaining_today": max(0, min(remaining_user, remaining_total)),
        "daily_limit": CHAT_DAILY_LIMIT_PER_USER,
    })


@app.route("/chat/ask", methods=["POST"])
def chat_ask():
    user_id = session.get("user_id")
    question = request.form.get("question", "").strip()[:CHAT_QUESTION_MAX_LENGTH]
    if not question:
        return jsonify({"message": "質問を入力してください。", "success": False}), 400

    remaining_user, remaining_total = _chat_rate_limit_status(user_id)
    if remaining_total <= 0:
        return jsonify({
            "message": "本日のサイト全体での質問回数の上限に達しました。日付が変わってからお試しください。",
            "success": False,
        }), 429
    if remaining_user <= 0:
        return jsonify({
            "message": f"本日の質問回数の上限({CHAT_DAILY_LIMIT_PER_USER}回)に達しました。日付が変わってからお試しください。",
            "success": False,
        }), 429

    api_keys = collect.get_api_keys()
    asked_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    if not api_keys:
        return jsonify({"message": "AIチャットは現在利用できません(APIキー未設定)。", "success": False}), 503

    from google.genai import types

    model = os.environ.get("GEMINI_MODEL", collect.DEFAULT_GEMINI_MODEL)
    config = types.GenerateContentConfig(
        system_instruction=CHAT_SYSTEM_INSTRUCTION + "\n\n" + _build_chat_context(),
        temperature=0.3,
    )
    conn = chat_log.connect(CHAT_DB_PATH)
    try:
        conversation_id = request.form.get("conversation_id", type=int)
        conversation = None
        if conversation_id is not None:
            conversation = chat_log.get_conversation(conn, conversation_id, user_id)
        is_new_conversation = conversation is None
        if is_new_conversation:
            conversation_id = chat_log.create_conversation(conn, user_id, asked_at)

        response = collect._gemini_generate_with_fallback(model, question, config, api_keys, label="chat")
        if response is None or not getattr(response, "text", None):
            chat_log.record_message(conn, conversation_id, user_id, asked_at, question, error="no response")
            chat_log.touch_conversation(conn, conversation_id, asked_at)
            return jsonify({"message": "回答の生成に失敗しました。しばらくしてから再度お試しください。", "success": False}), 502
        answer = response.text.strip()
        message_id = chat_log.record_message(conn, conversation_id, user_id, asked_at, question, answer=answer)
        chat_log.touch_conversation(conn, conversation_id, asked_at)

        title = None
        if is_new_conversation:
            title = _generate_chat_title(question, answer, api_keys)
            chat_log.set_conversation_title(conn, conversation_id, title)
    finally:
        conn.close()
    users_conn = _users_conn()
    try:
        users.grant_points(users_conn, user_id, f"chat:{message_id}", CHAT_MESSAGE_POINTS, asked_at)
    finally:
        users_conn.close()
    remaining_user, remaining_total = _chat_rate_limit_status(user_id)
    return jsonify({
        "success": True, "answer": answer,
        "remaining_today": max(0, min(remaining_user, remaining_total)),
        "conversation_id": conversation_id,
        "title": title,
    })


def _network_failure_summary(recent_n=30):
    """common.record_network_failure()が記録したDNS/ネットワーク障害を集計する。
    このサーバーの名前解決はTailscale MagicDNS経由で、断続的に外部ドメインの解決が
    失敗することが分かっているため、頻度を可視化して気づきやすくする。
    """
    empty = {"count_24h": 0, "count_7d": 0, "recent": []}
    if not NETWORK_FAILURE_LOG_PATH.exists():
        return empty
    try:
        rows = []
        with open(NETWORK_FAILURE_LOG_PATH, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
                except (KeyError, ValueError):
                    continue
                rows.append({
                    "datetime": dt, "source": row.get("source", ""),
                    "station": row.get("station") or "", "detail": row.get("detail", ""),
                })
    except Exception as e:
        log.warning("Failed to read network failure log: %s", e)
        return empty

    now = now_jst_naive()
    count_24h = sum(1 for r in rows if now - r["datetime"] <= timedelta(hours=24))
    count_7d = sum(1 for r in rows if now - r["datetime"] <= timedelta(days=7))
    rows.sort(key=lambda r: r["datetime"], reverse=True)
    recent = [
        {
            "datetime": r["datetime"].strftime("%Y-%m-%d %H:%M:%S"), "source": r["source"],
            "station": r["station"], "detail": r["detail"],
        }
        for r in rows[:recent_n]
    ]
    return {"count_24h": count_24h, "count_7d": count_7d, "recent": recent}


def _systemctl_show(unit, properties):
    """systemctl show の出力（Key=Value の羅列）を辞書にして返す。取得できなければ空辞書。"""
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "-p", ",".join(properties), "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        log.warning("systemctl show failed for %s: %s", unit, e)
        return {}
    info = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v
    return info


SMB_CONF_PATH = Path("/etc/samba/smb.conf")
SMB_SHARE_NAME = "river-fog"


def _read_smb_share_config(share_name=SMB_SHARE_NAME, path=SMB_CONF_PATH):
    """smb.confから指定共有(既定: river-fog)の設定を読み取り専用で読む。
    Sambaの設定ファイルはINI形式に近いためconfigparserで解析する
    (`;`/`#`によるコメント記法がconfigparserの既定と一致する)。
    値に含まれるSamba独自のマクロ(%h, %m, %u等)をconfigparserの補間機能が
    誤って解釈しないよう、interpolation=Noneで無効化している。
    ファイルが無い/読めない/該当セクションが無い場合はNoneを返す
    (書き込みは行わない。設定変更はサーバー上で直接smb.confを編集する運用のまま)。
    """
    if not path.exists():
        return None
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except Exception as e:
        log.warning("Failed to parse smb.conf: %s", e)
        return None
    if share_name not in parser:
        return None
    section = parser[share_name]

    def _yn(key, default=False):
        return section.get(key, "yes" if default else "no").strip().lower() in ("yes", "true", "1")

    return {
        "path": section.get("path", "-"),
        "read_only": _yn("read only", default=True),
        "guest_ok": _yn("guest ok", default=False),
        "browseable": _yn("browseable", default=True),
        "valid_users": section.get("valid users", "").strip(),
        "comment": section.get("comment", "").strip(),
    }


CHANGELOG_MAX_ENTRIES = 30


def _changelog_entries(limit=CHANGELOG_MAX_ENTRIES):
    """gitタグ(v<VERSION>、post-commit-version-tag.shが各コミットで自動付与)から
    更新履歴を作る。コミットメッセージは英語(リポジトリの規約)のため、この一覧も
    英語の要約のまま表示する(タグは記録用のpost-commit-version-tag.sh導入以降にしか
    無いため、それより前のバージョンは一覧に出ない)。
    """
    try:
        result = subprocess.run(
            [
                "git", "for-each-ref", "refs/tags", "--sort=-creatordate",
                "--format=%(refname:short)|%(creatordate:short)|%(subject)",
            ],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        log.warning("Failed to read git tags for changelog: %s", e)
        return []
    entries = []
    for line in result.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        version, date, summary = parts
        entries.append({"version": version, "date": date, "summary": summary})
    return entries[:limit]


def _crontab_safe():
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        return result.stdout
    except Exception as e:
        log.warning("crontab -l failed: %s", e)
        return ""


def _pid_file(script_name):
    return BASE_DIR / "data" / f".{script_name}.pid"


def _is_script_running(script_name):
    """PIDファイルに記録したプロセスがまだ生きているかで判定する。
    以前は`pgrep -f script_name`（コマンドライン全体の部分一致）を使っていたが、
    "predict.py"という文字列を含む無関係なコマンド（ログファイル名やSSHでの調査コマンド
    そのものなど）にまで誤ってマッチしてしまい、実際には動いていないのに「実行中」と
    誤判定するバグがあったため、PIDの生死を直接確認する方式に変更した。
    """
    pid_file = _pid_file(script_name)
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        try:
            pid_file.unlink()
        except OSError:
            pass
        return False
    except PermissionError:
        return True
    return True


def _run_script_async(script_name):
    """バッチスクリプトをwebappから切り離したプロセスとしてバックグラウンド起動する
    （webapp自体の再起動・停止で巻き添えにならないようにstart_new_session=Trueにする）。
    ログはスクリプト自身がlogs/配下に書くため、標準出力は捨てる。起動時の予期しない失敗
    （import例外等）だけは拾えるよう、標準エラーだけは小さなデバッグログに残す。
    PIDはファイルに記録し、_is_script_runningの二重起動チェックに使う。
    """
    debug_log = BASE_DIR / "logs" / f"{script_name}.launch_debug.log"
    try:
        with open(debug_log, "ab") as stderr_f:
            proc = subprocess.Popen(
                [str(VENV_PYTHON), str(BASE_DIR / script_name)],
                cwd=str(BASE_DIR), stdout=subprocess.DEVNULL, stderr=stderr_f,
                start_new_session=True,
            )
        _pid_file(script_name).write_text(str(proc.pid))
        log.info("Launched %s as pid %d", script_name, proc.pid)
    except Exception:
        log.exception("Failed to launch %s", script_name)


def _dir_size_bytes(path):
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _read_server_resources():
    """CPU負荷・メモリ・温度をベストエフォートで読む。取得できない項目はNoneのまま返す。"""
    info = {"load1": None, "load5": None, "load15": None,
            "mem_used_pct": None, "mem_total": None, "mem_available": None,
            "temp_c": None}
    try:
        with open("/proc/loadavg", encoding="utf-8") as f:
            parts = f.read().split()
        info["load1"], info["load5"], info["load15"] = float(parts[0]), float(parts[1]), float(parts[2])
    except Exception as e:
        log.warning("Failed to read /proc/loadavg: %s", e)

    try:
        meminfo = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                meminfo[key.strip()] = int(rest.strip().split()[0])  # kB
        total_kb = meminfo.get("MemTotal")
        avail_kb = meminfo.get("MemAvailable")
        if total_kb:
            info["mem_total"] = total_kb * 1024
            if avail_kb is not None:
                info["mem_available"] = avail_kb * 1024
                info["mem_used_pct"] = round(100.0 * (total_kb - avail_kb) / total_kb, 1)
    except Exception as e:
        log.warning("Failed to read /proc/meminfo: %s", e)

    try:
        thermal_root = Path("/sys/class/thermal")
        best = None
        if thermal_root.is_dir():
            for zone in thermal_root.glob("thermal_zone*/temp"):
                try:
                    milli = int(zone.read_text().strip())
                    celsius = milli / 1000.0
                    if best is None or celsius > best:
                        best = celsius
                except Exception:
                    continue
        info["temp_c"] = best
    except Exception as e:
        log.warning("Failed to read thermal zone temperature: %s", e)

    return info


def _maintenance_state():
    """メンテナンスモードの状態(enabled, message)を読む。無ければ無効扱い。"""
    if not MAINTENANCE_FLAG_PATH.exists():
        return {"enabled": False, "message": ""}
    try:
        data = json.loads(MAINTENANCE_FLAG_PATH.read_text(encoding="utf-8"))
        return {"enabled": bool(data.get("enabled")), "message": data.get("message", "")}
    except Exception as e:
        log.warning("Failed to read maintenance flag: %s", e)
        return {"enabled": False, "message": ""}


def _set_maintenance_state(enabled, message):
    MAINTENANCE_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAINTENANCE_FLAG_PATH.write_text(
        json.dumps({"enabled": bool(enabled), "message": message}, ensure_ascii=False),
        encoding="utf-8",
    )


def _env_status():
    """.env.exampleに列挙されている設定項目それぞれが、実際に(値を見ずに)設定済みかを返す。"""
    if not ENV_EXAMPLE_PATH.exists():
        return []
    keys = []
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        keys.append(key)
    return [{"key": k, "is_set": bool(os.environ.get(k))} for k in keys]


def _read_env_file_lines():
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def _read_editable_env_values():
    lines = _read_env_file_lines()
    values = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        values[key.strip()] = val
    return {k: values.get(k, "") for k in EDITABLE_ENV_KEYS}


def _write_editable_env_values(updates):
    """updates: {key: value}。EDITABLE_ENV_KEYSに含まれるものだけを.envに書き戻す。
    既存の行は値だけ置き換え、無ければ末尾に追記する。他の行(APIキー等)には触れない。
    値に改行が含まれていると、意図しない環境変数行が追記されてしまうため拒否する。
    """
    updates = {k: v for k, v in updates.items() if k in EDITABLE_ENV_KEYS}
    for key, val in updates.items():
        if "\n" in val or "\r" in val:
            raise ValueError(f"{key} の値に改行を含めることはできません")
    lines = _read_env_file_lines()
    seen = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        new_lines.append(line)
    for key, val in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={val}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _camera_uptime_stats(days=CAMERA_HEALTH_DAYS):
    """直近days日間、各地点で「使える写真」が撮れた割合を計算する
    (_unavailableでない.jpgがある = 成功、フォルダはあるがファイルが無い/_unavailable = 失敗)。
    """
    date_dirs = _sorted_subdirs(IMAGE_DIR)[:days]
    ok_counts = {name: 0 for name in STATION_NAMES}
    total_counts = {name: 0 for name in STATION_NAMES}
    for date in date_dirs:
        for hhmm in _sorted_subdirs(IMAGE_DIR / date):
            hour_dir = IMAGE_DIR / date / hhmm
            for name in STATION_NAMES:
                ok_path = hour_dir / f"{name}.jpg"
                bad_path = hour_dir / f"{name}_unavailable.jpg"
                if ok_path.exists():
                    ok_counts[name] += 1
                    total_counts[name] += 1
                elif bad_path.exists():
                    total_counts[name] += 1
    rows = []
    for name in STATION_NAMES:
        total = total_counts[name]
        rate = (100.0 * ok_counts[name] / total) if total else None
        rows.append({"station": name, "ok": ok_counts[name], "total": total, "rate": rate})
    rows.sort(key=lambda r: (r["rate"] is None, r["rate"]))
    return rows


_camera_uptime_cache = {"data": None, "at": 0.0}
_camera_uptime_cache_lock = threading.Lock()


def _camera_uptime_stats_cached():
    """_camera_uptime_stats()はファイルシステムを日数分走査するため、AIチャット等
    高頻度に呼ばれる場所からはキャッシュ経由で使う(CAMERA_HEALTH_CACHE_SECONDSと同じ間隔)。"""
    now = time.monotonic()
    with _camera_uptime_cache_lock:
        cached = _camera_uptime_cache["data"]
        if cached is not None and now - _camera_uptime_cache["at"] < CAMERA_HEALTH_CACHE_SECONDS:
            return cached
    data = _camera_uptime_stats()
    with _camera_uptime_cache_lock:
        _camera_uptime_cache["data"] = data
        _camera_uptime_cache["at"] = now
    return data


_GEMINI_LINE_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_GEMINI_FALLBACK_RE = re.compile(r"fell back to sub key")
_GEMINI_QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|429")
_GEMINI_CONFIDENCE_RE = re.compile(r"Confidence distribution: high=(\d+), medium=(\d+), low=(\d+)")
# 分類・エスカレーション・AI要約それぞれの呼び出し回数を数えるためのパターン。
# 「本当に2キーで足りているか」を実際の呼び出し量から判断できるようにする。
_GEMINI_CLASSIFICATION_CALL_RE = _GEMINI_CONFIDENCE_RE  # 分類成功のたびに必ず1回出るログ
_GEMINI_ESCALATION_CALL_RE = re.compile(r"retrying with escalation model")
_GEMINI_ESCALATION_SKIPPED_RE = re.compile(r"Skipping escalation for \d+ station")
_GEMINI_SUMMARY_CALL_RE = re.compile(r"Saved AI summary")


def _gemini_log_stats(days=GEMINI_LOG_DAYS):
    """collect.logからGeminiまわりの状況(APIキーのフォールバック回数・クォータ超過回数・
    confidence分布・呼び出し回数)を日付ごとに集計する。"""
    path = ALLOWED_LOGS.get("collect")
    by_date = {}
    if not path or not path.exists():
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _GEMINI_LINE_DATE_RE.match(line)
                if not m:
                    continue
                date = m.group(1)
                entry = by_date.setdefault(date, {
                    "fallback": 0, "quota_error": 0, "high": 0, "medium": 0, "low": 0,
                    "classification_calls": 0, "escalation_calls": 0,
                    "escalation_skipped": 0, "summary_calls": 0,
                })
                if _GEMINI_FALLBACK_RE.search(line):
                    entry["fallback"] += 1
                if _GEMINI_QUOTA_RE.search(line):
                    entry["quota_error"] += 1
                cm = _GEMINI_CONFIDENCE_RE.search(line)
                if cm:
                    entry["high"] += int(cm.group(1))
                    entry["medium"] += int(cm.group(2))
                    entry["low"] += int(cm.group(3))
                    entry["classification_calls"] += 1
                if _GEMINI_ESCALATION_CALL_RE.search(line):
                    entry["escalation_calls"] += 1
                if _GEMINI_ESCALATION_SKIPPED_RE.search(line):
                    entry["escalation_skipped"] += 1
                if _GEMINI_SUMMARY_CALL_RE.search(line):
                    entry["summary_calls"] += 1
    except Exception as e:
        log.warning("Failed to scan collect.log for Gemini stats: %s", e)
        return []

    dates = sorted(by_date.keys())[-days:]
    return [{"date": d, **by_date[d]} for d in dates]


def _get_visitor_id():
    """アンケートの二重回答防止用に、ブラウザごとのIDをCookieで管理する。"""
    vid = request.cookies.get(VISITOR_COOKIE_NAME)
    if vid:
        return vid, False
    return str(uuid.uuid4()), True


def _announcements_conn():
    return announcements.connect(ANNOUNCEMENTS_DB_PATH)


def _active_announcements_safe():
    try:
        conn = _announcements_conn()
        try:
            now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
            return announcements.get_active_announcements(conn, now_str)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to load active announcements: %s", e)
        return []


# ダッシュボード等、複数のページから毎リクエスト呼ばれる集計(predictionsテーブル全体を
# GROUP BYで走査する)。データが更新されるのはpredict.py実行時(1日1回程度)だけなので、
# 短いTTLでキャッシュして無駄な全表走査を減らす。
ACCURACY_SUMMARY_CACHE_SECONDS = 60
_accuracy_summary_cache = {"data": None, "at": 0.0}
_accuracy_summary_cache_lock = threading.Lock()


def _accuracy_summary_safe():
    """予測精度サマリーを読み取り専用で取得する。DBが無い/読めない場合はNoneを返す。"""
    now = time.monotonic()
    with _accuracy_summary_cache_lock:
        cached = _accuracy_summary_cache["data"]
        if cached is not None and now - _accuracy_summary_cache["at"] < ACCURACY_SUMMARY_CACHE_SECONDS:
            return cached
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            result = prediction_log.accuracy_summary(conn)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to read accuracy summary: %s", e)
        return None
    with _accuracy_summary_cache_lock:
        _accuracy_summary_cache["data"] = result
        _accuracy_summary_cache["at"] = now
    return result


# 気象条件別の的中率集計(降水の有無・気温帯)もキャッシュする。predictionsテーブルの
# 全件をxlsxの気象データとdatetimeで突き合わせる処理はやや重いため。
_accuracy_by_weather_cache = {"data": None, "at": 0.0}
_accuracy_by_weather_cache_lock = threading.Lock()


def _accuracy_by_weather_safe():
    """答え合わせ済みの予測を、対象時刻の気象条件(降水の有無・気温帯)ごとに束ねて
    的中率を返す。地点別・リードタイム別の集計(prediction_log.accuracy_summary)はあるが、
    気象条件別の集計が無かったため追加。DB/xlsxが無い、または読めない場合はNoneを返す。
    """
    now = time.monotonic()
    with _accuracy_by_weather_cache_lock:
        cached = _accuracy_by_weather_cache["data"]
        if cached is not None and now - _accuracy_by_weather_cache["at"] < ACCURACY_SUMMARY_CACHE_SECONDS:
            return cached
    if not DB_PATH.exists() or not WEATHER_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT target_datetime, predicted_code, actual_code FROM predictions "
                "WHERE actual_code IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        main_df, _, _ = fog_data.load_river_fog_data()
        weather_by_dt = {
            row["datetime"].strftime("%Y-%m-%d %H:%M:%S"): row
            for _, row in main_df.iterrows()
        }
        precip_buckets = {}
        temp_buckets = {}
        for target_dt, predicted_code, actual_code in rows:
            weather_row = weather_by_dt.get(target_dt)
            if weather_row is None:
                continue
            correct = 1 if predicted_code == actual_code else 0
            precip = weather_row[COL_PRECIP]
            precip_key = "降水あり" if precip is not None and precip > 0 else "降水なし"
            precip_buckets.setdefault(precip_key, {"n": 0, "correct": 0})
            precip_buckets[precip_key]["n"] += 1
            precip_buckets[precip_key]["correct"] += correct
            temp = weather_row[COL_TEMP]
            if temp is not None:
                band_lo = int(temp // 5) * 5
                temp_key = f"{band_lo}〜{band_lo + 5}℃"
                temp_buckets.setdefault(temp_key, {"n": 0, "correct": 0})
                temp_buckets[temp_key]["n"] += 1
                temp_buckets[temp_key]["correct"] += correct
    except Exception as e:
        log.warning("Failed to compute accuracy by weather condition: %s", e)
        return None
    result = {
        "by_precip": [
            {"label": k, "n": v["n"], "correct": v["correct"]}
            for k, v in sorted(precip_buckets.items())
        ],
        "by_temp": [
            {"label": k, "n": v["n"], "correct": v["correct"]}
            for k, v in sorted(temp_buckets.items(), key=lambda kv: int(kv[0].split("〜")[0]))
        ],
    }
    with _accuracy_by_weather_cache_lock:
        _accuracy_by_weather_cache["data"] = result
        _accuracy_by_weather_cache["at"] = now
    return result


def _latest_observations():
    """最新時刻の観測(気象値+地点ごとの現象コード)を返す。データが無ければNoneを返す。"""
    if not WEATHER_DB_PATH.exists():
        return None
    try:
        main_df, phenom_df, station_names = fog_data.load_river_fog_data()
    except Exception as e:
        log.warning("Failed to load observation data: %s", e)
        return None
    if main_df.empty:
        return None
    last_dt = main_df["datetime"].max()
    main_row = main_df[main_df["datetime"] == last_dt].iloc[0]
    phenom_row = phenom_df[phenom_df["datetime"] == last_dt].iloc[0]

    stations = []
    for name in station_names:
        code = encode_phenomena_cell(phenom_row.get(name))
        label, color, text_color = _phenom_label_color(code)
        clean_code = int(code) if code == code else None  # NaNはNoneにする
        stations.append({
            "name": name, "label": label, "color": color, "text_color": text_color, "code": clean_code,
        })

    return {
        "datetime": last_dt,
        "temp": main_row.get("気温(℃)"),
        "humidity": main_row.get("相対湿度(％)"),
        "precip": main_row.get("降水量(mm)"),
        "visibility": main_row.get("視程(km)"),
        "stations": stations,
    }


_WIND_COMPASS_POINTS = ["北", "北東", "東", "南東", "南", "南西", "西", "北西"]


def _wind_compass_label(deg):
    """風向(度)を8方位の日本語ラベルに変換する。"""
    if deg is None or deg != deg:  # None/NaN
        return None
    idx = int((float(deg) % 360) / 45 + 0.5) % 8
    return _WIND_COMPASS_POINTS[idx]


def _latest_station_weather(name, observations):
    """地点別ページ(station_detail.html)用の、その地点固有の最新気象情報。
    station_weather.db(地点固有の実測、2026-08-28以降蓄積分)に記録があればそれを使い、
    無ければ全地点共通の代表値(_latest_observations()、只見川.xlsx由来)にフォールバックする。
    観測データが全く無ければNoneを返す。
    """
    conn = station_weather.connect(STATION_WEATHER_DB_PATH)
    try:
        row = station_weather.load_latest_station_weather(conn, name)
    finally:
        conn.close()
    if row is not None:
        try:
            dt_label = datetime.strptime(row["datetime"][:19], "%Y-%m-%d %H:%M:%S").strftime("%Y/%m/%d %H:%M")
        except Exception:
            dt_label = row["datetime"]
        return {
            "datetime_label": dt_label,
            "temp": row["temperature"], "humidity": row["humidity"],
            "precip": row["precipitation"], "visibility": row["visibility_km"],
            "wind_speed": row["wind_speed"], "wind_dir_label": _wind_compass_label(row["wind_direction_deg"]),
            "solar": row["solar_radiation_mj"], "sunshine": row["sunshine_hours"], "dew_point": row["dew_point"],
            "is_station_specific": True,
        }
    if observations is None:
        return None
    return {
        "datetime_label": observations["datetime"].strftime("%Y/%m/%d %H:%M"),
        "temp": observations.get("temp"), "humidity": observations.get("humidity"),
        "precip": observations.get("precip"), "visibility": observations.get("visibility"),
        "wind_speed": None, "wind_dir_label": None, "solar": None, "sunshine": None, "dew_point": None,
        "is_station_specific": False,
    }


# シミュレーター(What-ifページ)で調整可能にする気象項目。key: フォームの入力名、
# col: fog_features.pyの学習時と同じ列名(simulate_stationに渡すweather dictのキーになる)。
# fallbackは、その地点の実測値がまだ無い(station_weather.dbに記録が無い)場合の初期値。
SIM_WEATHER_FIELDS = [
    {"key": "temp", "col": COL_TEMP, "label": "気温", "unit": "℃", "min": -20, "max": 40, "fallback": 10.0},
    {"key": "precip", "col": COL_PRECIP, "label": "降水量", "unit": "mm", "min": 0, "max": 30, "fallback": 0.0},
    {"key": "wind_speed", "col": COL_WIND_SPEED, "label": "風速", "unit": "m/s", "min": 0, "max": 15, "fallback": 1.0},
    {"key": "wind_dir", "col": COL_WIND_DIR_DEG, "label": "風向", "unit": "°", "min": 0, "max": 360, "fallback": 0.0},
    {"key": "humidity", "col": COL_HUMIDITY, "label": "相対湿度", "unit": "%", "min": 0, "max": 100, "fallback": 80.0},
    {"key": "dew_point", "col": COL_DEWPOINT, "label": "露点温度", "unit": "℃", "min": -20, "max": 30, "fallback": 8.0},
    {"key": "sunshine", "col": COL_SUNSHINE, "label": "日照時間", "unit": "h", "min": 0, "max": 1, "fallback": 0.0},
    {"key": "solar", "col": COL_SOLAR, "label": "日射量", "unit": "MJ/m2", "min": 0, "max": 4, "fallback": 0.0},
    {"key": "visibility", "col": COL_VISIBILITY, "label": "視程", "unit": "km", "min": 0, "max": 20, "fallback": 5.0},
]


def _simulator_defaults(name):
    """シミュレーター画面の初期値。その地点固有の最新気象実測(station_weather.db、
    2026-08-28以降蓄積分)があればそれを使い、無ければ各項目のfallback値を使う。"""
    if not name:
        return {}
    conn = station_weather.connect(STATION_WEATHER_DB_PATH)
    try:
        row = station_weather.load_latest_station_weather(conn, name)
    finally:
        conn.close()
    if row is None:
        return {}
    return {
        "temp": row["temperature"], "precip": row["precipitation"],
        "wind_speed": row["wind_speed"], "wind_dir": row["wind_direction_deg"],
        "humidity": row["humidity"], "dew_point": row["dew_point"],
        "sunshine": row["sunshine_hours"], "solar": row["solar_radiation_mj"],
        "visibility": row["visibility_km"],
    }


# ============================================================================
# 公開ページ(観測データ・カメラ画像・予測・地点別ページ)
# 2668行の単一ファイルで見通しが悪いという指摘への対応として、まずは大まかな区分けの
# コメントを入れている(Blueprint分割は全url_for呼び出し・テンプレートへの影響が大きく、
# ステージング環境の無いこの構成で一括に行うのはリスクが高いため見送った)。
# ============================================================================


@app.route("/")
def dashboard():
    try:
        _check_economy_anomalies()
    except Exception as e:
        log.warning("Economy anomaly check failed: %s", e)
    latest_image_date = next(iter(_sorted_subdirs(IMAGE_DIR)), None)
    latest_image_hour = None
    latest_images = []
    if latest_image_date:
        latest_image_hour = next(iter(_sorted_subdirs(IMAGE_DIR / latest_image_date)), None)
        if latest_image_hour:
            hour_dir = IMAGE_DIR / latest_image_date / latest_image_hour
            latest_images = sorted(p.name for p in hour_dir.iterdir() if p.suffix.lower() == ".jpg")

    latest_pred_date = next(iter(_sorted_subdirs(PRED_DIR)), None)
    summary_file = None
    if latest_pred_date:
        pred_dir = PRED_DIR / latest_pred_date
        summary_file = next((p.name for p in pred_dir.iterdir() if SUMMARY_RE.match(p.name)), None)

    active_announcements = _active_announcements_safe()
    existing_visitor_id = request.cookies.get(VISITOR_COOKIE_NAME)
    if active_announcements:
        conn = None
        if existing_visitor_id:
            try:
                conn = _announcements_conn()
            except Exception as e:
                log.warning("Failed to open announcements db: %s", e)
        for ann in active_announcements:
            ann["already_responded"] = bool(
                conn and announcements.has_responded(conn, ann["id"], existing_visitor_id)
            )
        if conn:
            conn.close()

    ai_summary = _load_ai_summary()
    dashboard_theme_class = ""
    my_id = session.get("user_id")
    if my_id:
        conn = _users_conn()
        try:
            me = users.get_user_by_id(conn, my_id)
        finally:
            conn.close()
        if me:
            theme = DASHBOARD_THEME_BY_ID.get(me.get("equipped_dashboard_theme", ""))
            if theme:
                dashboard_theme_class = theme["css_class"]
    return render_template(
        "dashboard.html",
        dashboard_theme_class=dashboard_theme_class,
        observations=_latest_observations(),
        accuracy=_accuracy_summary_safe(),
        alerts=_upcoming_fog_alerts_safe(),
        latest_image_date=latest_image_date,
        latest_image_hour=latest_image_hour,
        latest_images=latest_images,
        latest_pred_date=latest_pred_date,
        summary_file=summary_file,
        station_names=STATION_NAMES,
        announcements_list=active_announcements,
        ai_overall_summary=ai_summary.get("overall") if ai_summary else None,
        ai_summary_generated_at=ai_summary.get("generated_at") if ai_summary else None,
    )


@app.route("/observations")
def observations_dates():
    if not WEATHER_DB_PATH.exists():
        return render_template("observations_dates.html", dates=[])
    try:
        main_df, _, _ = fog_data.load_river_fog_data()
    except Exception as e:
        log.warning("Failed to load observation data: %s", e)
        return render_template("observations_dates.html", dates=[])
    dates = sorted({dt.strftime("%Y-%m-%d") for dt in main_df["datetime"]}, reverse=True)
    return render_template(
        "observations_dates.html", dates=dates,
        date_groups=_group_dates_by_month(dates), endpoint="observations_date",
    )


@app.route("/observations/<date>")
def observations_date(date):
    date = _validate_date(date)
    try:
        main_df, phenom_df, station_names = fog_data.load_river_fog_data()
    except Exception as e:
        log.warning("Failed to load observation data: %s", e)
        abort(404)

    day_main = main_df[main_df["datetime"].dt.strftime("%Y-%m-%d") == date].sort_values("datetime")
    day_phenom = phenom_df[phenom_df["datetime"].dt.strftime("%Y-%m-%d") == date].sort_values("datetime")
    if day_main.empty:
        abort(404)

    rows = []
    for _, main_row in day_main.iterrows():
        dt = main_row["datetime"]
        phenom_row = day_phenom[day_phenom["datetime"] == dt]
        cells = []
        if not phenom_row.empty:
            phenom_row = phenom_row.iloc[0]
            for name in station_names:
                code = encode_phenomena_cell(phenom_row.get(name))
                label, color, text_color = _phenom_label_color(code)
                cells.append({"label": label, "color": color, "text_color": text_color})
        rows.append({
            "time": dt.strftime("%H:%M"),
            "temp": main_row.get("気温(℃)"),
            "humidity": main_row.get("相対湿度(％)"),
            "precip": main_row.get("降水量(mm)"),
            "visibility": main_row.get("視程(km)"),
            "cells": cells,
        })

    return render_template("observations_date.html", date=date, station_names=station_names, rows=rows)


@app.route("/observations/<date>/chart.png")
def observations_date_chart(date):
    date = _validate_date(date)
    try:
        main_df, _, _ = fog_data.load_river_fog_data()
    except Exception as e:
        log.warning("Failed to load observation data: %s", e)
        main_df = None
    day_main = None
    if main_df is not None:
        day_main = main_df[main_df["datetime"].dt.strftime("%Y-%m-%d") == date].sort_values("datetime")
    png = _render_weather_timeseries_png(day_main, date)
    return Response(png, mimetype="image/png")


@app.route("/download/xlsx")
def download_xlsx():
    if not WEATHER_DB_PATH.exists():
        abort(404)
    conn = weather_store.connect(WEATHER_DB_PATH)
    try:
        weather_store.export_to_xlsx(conn, EXPORT_XLSX_PATH, CAMERAS_CSV, TEMPLATE_XLSX)
    finally:
        conn.close()
    return send_file(EXPORT_XLSX_PATH, as_attachment=True, download_name="只見川.xlsx")


@app.route("/images")
def images_dates():
    dates = _sorted_subdirs(IMAGE_DIR)
    return render_template(
        "images_dates.html", dates=dates,
        date_groups=_group_dates_by_month(dates), endpoint="images_date",
    )


@app.route("/images/<date>")
def images_date(date):
    date = _validate_date(date)
    date_dir = IMAGE_DIR / date
    if not date_dir.is_dir():
        abort(404)
    return render_template("images_date.html", date=date, hours=_sorted_subdirs(date_dir))


@app.route("/images/<date>/<hhmm>")
def images_hour(date, hhmm):
    date = _validate_date(date)
    hhmm = _validate_hhmm(hhmm)
    hour_dir = IMAGE_DIR / date / hhmm
    if not hour_dir.is_dir():
        abort(404)
    files = sorted(p.name for p in hour_dir.iterdir() if p.suffix.lower() == ".jpg")

    target_dt_str = f"{date} {hhmm[:2]}:{hhmm[2:]}:00"
    codes_by_station, corrections_by_station = _phenom_codes_for_datetime(target_dt_str)

    return render_template(
        "images_hour.html", date=date, hhmm=hhmm, files=files,
        codes_by_station=codes_by_station, corrections_by_station=corrections_by_station,
        phenom_labels=PHENOM_LABELS,
    )


def _phenom_codes_for_datetime(target_dt_str):
    """指定日時(YYYY-MM-DD HH:MM:SS)の、地点ごとのGemini判定コードと人間による訂正済み
    コードを返す((codes_by_station, corrections_by_station)、いずれも{地点名: コード})。
    images_hour()と後述のimages_hour_correct()の両方から使う共通処理。"""
    codes_by_station = {}
    if WEATHER_DB_PATH.exists():
        try:
            _, phenom_df, station_names = fog_data.load_river_fog_data()
            row = phenom_df[phenom_df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S") == target_dt_str]
            if not row.empty:
                for name in station_names:
                    if name not in row.columns:
                        continue
                    code = encode_phenomena_cell(row.iloc[0][name])
                    if code == code:  # NaNではない
                        codes_by_station[name] = int(code)
        except Exception as e:
            log.warning("Failed to load phenom codes for %s: %s", target_dt_str, e)
    conn = _users_conn()
    try:
        corrections_by_station = users.get_phenomena_corrections_for_datetime(conn, target_dt_str)
    finally:
        conn.close()
    return codes_by_station, corrections_by_station


@app.route("/images/<date>/<hhmm>/correct", methods=["POST"])
def images_hour_correct(date, hhmm):
    """人間がGeminiの現象判定を訂正する。訂正内容はphenomena_correctionsに記録するのみで、
    xlsx上の値そのものは書き換えない(次回の再学習でfog_model.py側がこの記録を使って
    除外・重み付けする設計にしやすいよう、生データと訂正履歴を分けて持つ)。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    date = _validate_date(date)
    hhmm = _validate_hhmm(hhmm)
    station = request.form.get("station", "")
    if station not in STATION_NAMES:
        abort(404)
    corrected_code = request.form.get("corrected_code", type=int)
    if corrected_code is None or not (0 <= corrected_code <= 10):
        return _finish(
            "images_hour", "訂正後の現象コードを選んでください。", success=False, date=date, hhmm=hhmm,
        )

    target_dt_str = f"{date} {hhmm[:2]}:{hhmm[2:]}:00"
    codes_by_station, _ = _phenom_codes_for_datetime(target_dt_str)
    original_code = codes_by_station.get(station)

    conn = _users_conn()
    try:
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        users.add_phenomena_correction(conn, station, target_dt_str, original_code, corrected_code, my_id, now_str)
        _record_audit(
            "画像判定を訂正",
            f"{station} {target_dt_str}: {original_code} → {corrected_code}",
        )
    finally:
        conn.close()
    return _finish("images_hour", "訂正を記録しました。", date=date, hhmm=hhmm)


@app.route("/images/<date>/<hhmm>/file/<path:filename>")
def images_file(date, hhmm, filename):
    date = _validate_date(date)
    hhmm = _validate_hhmm(hhmm)
    return send_from_directory(IMAGE_DIR / date / hhmm, filename)


@app.route("/images/<date>/<hhmm>/download.zip")
def images_hour_zip(date, hhmm):
    date = _validate_date(date)
    hhmm = _validate_hhmm(hhmm)
    hour_dir = IMAGE_DIR / date / hhmm
    if not hour_dir.is_dir():
        abort(404)
    files = sorted(p for p in hour_dir.iterdir() if p.suffix.lower() == ".jpg")
    if not files:
        abort(404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    buf.seek(0)
    return send_file(
        buf, mimetype="application/zip", as_attachment=True,
        download_name=f"{date}_{hhmm}.zip",
    )


@app.route("/predictions")
def predictions_dates():
    dates = _sorted_subdirs(PRED_DIR)
    return render_template(
        "predictions_dates.html", dates=dates,
        date_groups=_group_dates_by_month(dates), endpoint="predictions_date",
    )


@app.route("/predictions/<date>")
def predictions_date(date):
    date = _validate_date(date)
    date_dir = PRED_DIR / date
    if not date_dir.is_dir():
        abort(404)
    files = sorted(p.name for p in date_dir.iterdir() if p.is_file())

    summary_file = next((f for f in files if SUMMARY_RE.match(f)), None)
    csv_file = next((f for f in files if CSV_RE.match(f)), None)
    stations = []
    for f in files:
        m = STATION_FORECAST_RE.match(f)
        if m:
            stations.append({"name": m.group(1), "file": f})
    stations.sort(key=lambda s: s["name"])

    return render_template(
        "predictions_date.html", date=date,
        summary_file=summary_file, csv_file=csv_file, stations=stations,
    )


@app.route("/predictions/<date>/file/<path:filename>")
def predictions_file(date, filename):
    date = _validate_date(date)
    return send_from_directory(PRED_DIR / date, filename)


@app.route("/stations")
def stations_list():
    observations = _latest_observations()
    current_by_name = {}
    if observations:
        current_by_name = {s["name"]: s for s in observations["stations"]}
    return render_template(
        "stations_list.html", station_names=STATION_NAMES, current_by_name=current_by_name,
    )


@app.route("/compare")
def compare_stations():
    selected = [s for s in request.args.get("stations", "").split(",") if s]
    selected = [s for s in selected if s in STATION_NAMES]

    observations = _latest_observations()
    current_by_name = {}
    if observations:
        current_by_name = {s["name"]: s for s in observations["stations"]}

    accuracy_by_station = {}
    summary = _accuracy_summary_safe()
    if summary:
        for row in summary["by_station"]:
            accuracy_by_station[row["station"]] = row

    entries = []
    for name in selected:
        latest_pred_date, forecast_file = _station_latest_forecast_file(name)
        entries.append({
            "name": name,
            "current": current_by_name.get(name),
            "latest_pred_date": latest_pred_date,
            "forecast_file": forecast_file,
            "accuracy": accuracy_by_station.get(name),
        })

    return render_template(
        "compare.html", station_names=STATION_NAMES, selected=selected, entries=entries,
        observations_datetime=observations["datetime"] if observations else None,
        current_by_name=current_by_name, active_names=selected,
    )


@app.route("/map")
def map_page():
    observations = _latest_observations()
    current_by_name = {}
    if observations:
        current_by_name = {s["name"]: s for s in observations["stations"]}

    alert_stations = {a["station"] for a in _upcoming_fog_alerts_safe()}

    entries = []
    map_points = []
    for name in STATION_NAMES:
        info = CAMERA_INFO.get(name, {})
        cam_id = info.get("cam_id")
        lat, lon = info.get("lat"), info.get("lon")
        current = current_by_name.get(name)
        google_url = _google_maps_search_url(lat, lon) if lat is not None and lon is not None else None
        entries.append({
            "name": name,
            "current": current,
            "river_gov_url": _river_go_jp_map_url(cam_id) if cam_id else None,
            "google_url": google_url,
            "lat": lat,
            "lon": lon,
        })
        map_points.append({
            "name": name,
            "lat": lat,
            "lon": lon,
            "label": current["label"] if current else "データなし",
            "color": current["color"] if current else "#bdc3c7",
            "alert": name in alert_stations,
            "detail_url": url_for("station_detail", name=name),
        })

    return render_template(
        "map.html", station_names=STATION_NAMES, entries=entries,
        current_by_name=current_by_name, map_points=map_points,
        has_alert=bool(alert_stations),
    )


@app.route("/simulator")
def simulator_page():
    """What-ifシミュレーター: 地点を選び、気象条件・時刻を手動で入力すると、その地点が
    現在採用しているモデル(fog_model.simulate_station)でその場で霧予測を試算する。
    学習・本番の予測パイプラインには一切書き込まない、閲覧専用の推論だけの機能。
    """
    station = request.args.get("station") or (STATION_NAMES[0] if STATION_NAMES else None)
    if station and station not in STATION_NAMES:
        abort(404)

    state = fog_model.load_model_state()
    info = state.get(station) if station else None

    defaults = _simulator_defaults(station)
    now = now_jst_naive()
    date_str = request.args.get("date") or now.strftime("%Y-%m-%d")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        date_str = now.strftime("%Y-%m-%d")
    hour = request.args.get("hour", type=int)
    if hour is None or not (0 <= hour <= 23):
        hour = now.hour

    values = {}
    explicit_fields = set()
    for field in SIM_WEATHER_FIELDS:
        raw = request.args.get(field["key"])
        if raw not in (None, ""):
            try:
                # URL経由の値は公開時に外部から自由に渡せるため、想定レンジにクランプする
                # (学習データのレンジを大きく外れた値をモデルにそのまま与えないようにするため)。
                values[field["key"]] = min(max(float(raw), field["min"]), field["max"])
                explicit_fields.add(field["key"])
                continue
            except ValueError:
                pass
        default = defaults.get(field["key"])
        values[field["key"]] = float(default) if default is not None else field["fallback"]

    defaults_are_real = bool(defaults)
    wind_dir_label = _wind_compass_label(values.get("wind_dir"))
    warnings = []
    if values["dew_point"] > values["temp"]:
        warnings.append(
            "露点温度が気温を上回っています(通常は起こらない組み合わせです)。"
            "この入力では予測結果の信頼性が下がる可能性があります。"
        )

    result = None
    if info is not None:
        cam = CAMERA_INFO.get(station, {})
        lat, lon = cam.get("lat"), cam.get("lon")
        if lat is None or lon is None:
            lat, lon = representative_coords()
        dt = datetime.strptime(f"{date_str} {hour:02d}:00", "%Y-%m-%d %H:%M")
        weather = {field["col"]: values[field["key"]] for field in SIM_WEATHER_FIELDS}
        predicted_code, fog_prob, class_proba = fog_model.simulate_station(info, lat, lon, weather, dt)
        label, color, text_color = _phenom_label_color(predicted_code)
        threshold = info.get("fog_threshold") or fog_model.DEFAULT_FOG_THRESHOLD
        result = {
            "label": label, "color": color, "text_color": text_color,
            "fog_prob": fog_prob, "fog_threshold": threshold, "is_fog_alert": fog_prob >= threshold,
            "model_type": fog_model.normalize_model_type_label(info.get("model_type")),
            "class_proba": [
                {"code": c, "label": _phenom_label_color(c)[0], "prob": p}
                for c, p in class_proba.items() if p >= 0.01
            ],
        }

    return render_template(
        "simulator.html",
        station_names=STATION_NAMES, station=station, active_names=[station] if station else [],
        fields=SIM_WEATHER_FIELDS, values=values, hour=hour, date=date_str,
        no_model=(station is not None and info is None), result=result,
        defaults_are_real=defaults_are_real, wind_dir_label=wind_dir_label, warnings=warnings,
        explicit_fields=explicit_fields,
    )


_anomaly_scan_cache = {}
_anomaly_scan_lock = threading.Lock()
ANOMALY_SCAN_INTERVAL_SECONDS = 900
ANOMALY_SCAN_LOOKBACK_HOURS = 48


def _scan_and_autocorrect_station_anomalies(name):
    """孤立した1時間だけのブレ(前後の観測が一致し、その1時間だけ現象コードが違う)を
    観測・判定ミスの疑いとして扱い、周囲の値に自動で訂正する。地点別ページを開くたびに
    その地点だけ軽く走査する(全地点を毎回スキャンする専用バッチは持たない)。
    ギャンブルの払い戻し計算はこの訂正テーブルを一切参照しないため、賭けの結果には
    影響しない。"""
    now = time.monotonic()
    with _anomaly_scan_lock:
        last = _anomaly_scan_cache.get(name)
        if last is not None and now - last < ANOMALY_SCAN_INTERVAL_SECONDS:
            return
        _anomaly_scan_cache[name] = now
    if not WEATHER_DB_PATH.exists():
        return
    try:
        _, phenom_df, station_names = fog_data.load_river_fog_data()
    except Exception as e:
        log.warning("Anomaly scan failed to load data for %s: %s", name, e)
        return
    if name not in phenom_df.columns:
        return
    recent = phenom_df[["datetime", name]].dropna(subset=["datetime"]).tail(ANOMALY_SCAN_LOOKBACK_HOURS)
    recent = recent.reset_index(drop=True)
    raw_codes = [encode_phenomena_cell(v) for v in recent[name]]
    conn = _users_conn()
    try:
        for i in range(1, len(raw_codes) - 1):
            prev_raw, cur_raw, next_raw = raw_codes[i - 1], raw_codes[i], raw_codes[i + 1]
            if any(c != c for c in (prev_raw, cur_raw, next_raw)):  # NaNを含む区間はスキップ
                continue
            prev_c, cur_c, next_c = int(prev_raw), int(cur_raw), int(next_raw)
            if prev_c != next_c or cur_c == prev_c:
                continue
            target_dt = recent.iloc[i]["datetime"]
            target_dt_str = (
                target_dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(target_dt, "strftime") else str(target_dt)
            )
            if name in users.get_phenomena_corrections_for_datetime(conn, target_dt_str):
                continue  # 既に訂正済み(自動・人力いずれか)
            now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
            note = f"前後の観測({prev_c})に基づき自動訂正"
            users.add_phenomena_correction(
                conn, name, target_dt_str, cur_c, prev_c, None, now_str, source="auto",
            )
            users.log_auto_resolved_observation_report(conn, name, target_dt_str, cur_c, prev_c, now_str, note)
            _record_audit(
                "観測異常の自動検知・訂正",
                f"{name} {target_dt_str}: {cur_c} → {prev_c}({note})",
            )
    finally:
        conn.close()


@app.route("/stations/<name>/report", methods=["POST"])
def station_report_observation(name):
    """現在表示中の観測(現象判定)が間違っていると思う場合、ログイン中のユーザーなら
    誰でも管理者へ報告できる。データは直接書き換えず、確認待ちキューに積むだけ。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    name = _validate_station(name)
    observations = _latest_observations()
    if not observations:
        return _finish("station_detail", "現在参照できる観測データがありません。", success=False, name=name)
    current = {s["name"]: s for s in observations["stations"]}.get(name)
    if current is None or current.get("code") is None:
        return _finish("station_detail", "この地点の観測データがありません。", success=False, name=name)
    target_dt_str = observations["datetime"]
    if hasattr(target_dt_str, "strftime"):
        target_dt_str = target_dt_str.strftime("%Y-%m-%d %H:%M:%S")
    conn = _users_conn()
    try:
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        created = users.create_observation_report(
            conn, name, target_dt_str, current["code"], my_id, now_str, source="user",
        )
        if created:
            _record_audit("観測報告を受信", f"{name} {target_dt_str}: コード{current['code']}")
    finally:
        conn.close()
    message = "報告を受け付けました。管理者が確認します。" if created else "この観測は既に報告済みです。"
    return _finish("station_detail", message, name=name)


@app.route("/admin/observation_reports")
@requires_admin
def admin_observation_reports():
    conn = _users_conn()
    try:
        reports = users.list_pending_observation_reports(conn)
    finally:
        conn.close()
    for r in reports:
        image_date = r["target_datetime"][:10]
        image_hhmm = r["target_datetime"][11:13] + r["target_datetime"][14:16]
        image_path = IMAGE_DIR / image_date / image_hhmm / f"{r['station_name']}.jpg"
        r["image_date"] = image_date
        r["image_hhmm"] = image_hhmm
        r["has_image"] = image_path.is_file()
    return render_template(
        "admin_observation_reports.html", reports=reports, phenom_labels=PHENOM_LABELS,
    )


@app.route("/admin/observation_reports/<int:report_id>/resolve", methods=["POST"])
@requires_admin
def admin_observation_report_resolve(report_id):
    my_id = session.get("user_id")
    conn = _users_conn()
    try:
        report = users.get_observation_report(conn, report_id)
        if report is None or report["status"] != "pending":
            abort(404)
        corrected_code = request.form.get("corrected_code", type=int)
        if corrected_code is None or not (0 <= corrected_code <= 10):
            return _finish("admin_observation_reports", "訂正後の現象コードを選んでください。", success=False)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        users.add_phenomena_correction(
            conn, report["station_name"], report["target_datetime"],
            report["reported_code"], corrected_code, my_id, now_str, source="human",
        )
        users.resolve_observation_report(
            conn, report_id, "resolved", my_id, now_str, f"{corrected_code}に訂正",
        )
        reward_note = ""
        if report["source"] == "user" and report["reported_by"] and corrected_code != report["reported_code"]:
            granted = users.grant_points(
                conn, report["reported_by"], f"observation_report_reward:{report_id}",
                OBSERVATION_REPORT_REWARD_POINTS, now_str,
            )
            if granted:
                reward_note = f"(報告者に{OBSERVATION_REPORT_REWARD_POINTS}pt付与)"
        _record_audit(
            "観測報告を確認・訂正",
            f"{report['station_name']} {report['target_datetime']}: "
            f"{report['reported_code']} → {corrected_code} {reward_note}".strip(),
        )
    finally:
        conn.close()
    return _finish("admin_observation_reports", "訂正しました。")


@app.route("/admin/observation_reports/<int:report_id>/dismiss", methods=["POST"])
@requires_admin
def admin_observation_report_dismiss(report_id):
    my_id = session.get("user_id")
    conn = _users_conn()
    try:
        report = users.get_observation_report(conn, report_id)
        if report is None or report["status"] != "pending":
            abort(404)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        users.resolve_observation_report(conn, report_id, "dismissed", my_id, now_str, "問題なしと判断")
        _record_audit("観測報告を却下", f"{report['station_name']} {report['target_datetime']}")
    finally:
        conn.close()
    return _finish("admin_observation_reports", "却下しました。")


@app.route("/stations/<name>")
def station_detail(name):
    name = _validate_station(name)
    try:
        _scan_and_autocorrect_station_anomalies(name)
    except Exception as e:
        log.warning("Anomaly scan error for %s: %s", name, e)

    observations = _latest_observations()
    current = None
    current_by_name = {}
    if observations:
        current_by_name = {s["name"]: s for s in observations["stations"]}
        current = current_by_name.get(name)

    latest_pred_date, forecast_file = _station_latest_forecast_file(name)
    timelapse = _station_timelapse_images(name)
    image_dates = _sorted_subdirs(IMAGE_DIR)

    my_id = session.get("user_id")
    conn = _users_conn()
    try:
        raw_comments = users.list_station_comments(conn, name)
        commenter_ids = {c["user_id"] for c in raw_comments}
        author_names = {uid: u["name"] for uid, u in users.get_users_by_ids(conn, commenter_ids).items()}
        share_targets = [u for u in users.list_users(conn) if u["id"] != my_id] if my_id else []
    finally:
        conn.close()
    comments = [
        {**c, "author_name": author_names.get(c["user_id"], "(退会済みユーザー)")}
        for c in raw_comments
    ]

    ai_summary = _load_ai_summary()
    ai_station_summary = None
    if ai_summary:
        ai_station_summary = ai_summary.get("stations", {}).get(name)

    station_weather_latest = _latest_station_weather(name, observations)

    return render_template(
        "station_detail.html",
        name=name,
        current=current,
        station_weather_latest=station_weather_latest,
        observations_datetime=observations["datetime"] if observations else None,
        latest_pred_date=latest_pred_date,
        forecast_file=forecast_file,
        timelapse=timelapse,
        share_targets=share_targets,
        date_groups=_group_dates_by_month(image_dates),
        station_names=STATION_NAMES,
        current_by_name=current_by_name,
        active_names=[name],
        comments=comments,
        comment_max_length=STATION_COMMENT_MAX_LENGTH,
        ai_station_summary=ai_station_summary,
        ai_summary_generated_at=ai_summary.get("generated_at") if ai_summary else None,
        feature_snapshot=_load_station_feature_snapshot(name),
    )


@app.route("/graphs/weather-fog")
def weather_fog_graph_page():
    """気象条件(気温・湿度・風速・降水量)ごとに、川霧が発生した/しなかった日でどう
    分布が違うかを箱ひげ図で比較するページ。地点はクエリパラメータで切り替える。"""
    station = _validate_station(request.args.get("station", STATION_NAMES[0]))
    return render_template(
        "weather_fog_graph.html", station=station, station_names=STATION_NAMES,
        active_names=[station],
    )


@app.route("/graphs/weather-fog.png")
def weather_fog_graph_png():
    station = _validate_station(request.args.get("station", STATION_NAMES[0]))

    def render():
        main_df = phenom_df = None
        if WEATHER_DB_PATH.exists():
            try:
                main_df, phenom_df, _ = fog_data.load_river_fog_data()
            except Exception as e:
                log.warning("Failed to load data for weather-fog graph: %s", e)
        return _render_weather_fog_boxplot_png(main_df, phenom_df, station)

    png = _cached_png(f"weather_fog_graph:{station}", [WEATHER_DB_PATH], render)
    return Response(png, mimetype="image/png")


@app.route("/river-fog-histogram")
def river_fog_histogram_page():
    """全地点合算・川霧発生時のみに絞った気象条件ヒストグラムの専用ページ。
    「気象と川霧」ページ(/graphs配下)とはナビの見た目上のアクティブ判定が
    衝突しないよう、あえて/graphs以下ではないパスにしている。"""
    conn = _users_conn()
    try:
        correction_counts = users.phenomena_correction_counts_by_station(conn)
    finally:
        conn.close()
    return render_template("river_fog_histogram.html", correction_counts=correction_counts)


@app.route("/graphs/weather-fog-histogram.png")
def weather_fog_histogram_png():
    """全地点合算・川霧発生時のみに絞った気象条件ヒストグラム(地点選択は無く常に全地点)。"""
    def render():
        main_df = phenom_df = station_names = None
        if WEATHER_DB_PATH.exists():
            try:
                main_df, phenom_df, station_names = fog_data.load_river_fog_data()
            except Exception as e:
                log.warning("Failed to load data for river-fog histogram: %s", e)
        return _render_river_fog_histogram_png(main_df, phenom_df, station_names or STATION_NAMES)

    png = _cached_png("weather_fog_histogram", [WEATHER_DB_PATH], render)
    return Response(png, mimetype="image/png")


@app.route("/stations/<name>/comments", methods=["POST"])
def station_comment_add(name):
    name = _validate_station(name)
    body = request.form.get("body", "").strip()[:STATION_COMMENT_MAX_LENGTH]
    if not body:
        return _finish("station_detail", "コメントを入力してください。", success=False, name=name)
    created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    conn = _users_conn()
    try:
        comment_id = users.add_station_comment(conn, name, session["user_id"], body, created_at)
        users.grant_points(
            conn, session["user_id"], f"comment:{comment_id}", COMMENT_POST_POINTS, created_at,
        )
    finally:
        conn.close()
    if _wants_json():
        # data-ajaxのフォームがページ遷移無しで一覧に追加できるよう、投稿内容自体も返す
        # (通常のflash+redirectでは表示側の情報が無いのでここだけ_finish()を使わない)。
        return jsonify({
            "message": "コメントを投稿しました。", "success": True,
            "author_name": session.get("name", ""), "body": body, "created_at": created_at,
        })
    flash("コメントを投稿しました。", "success")
    return redirect(url_for("station_detail", name=name))


@app.route("/stations/<name>/comments/<int:comment_id>/delete", methods=["POST"])
def station_comment_delete(name, comment_id):
    name = _validate_station(name)
    conn = _users_conn()
    try:
        comment = users.get_station_comment(conn, comment_id)
        if comment is None or comment["station_name"] != name:
            abort(404)
        if comment["user_id"] != session.get("user_id") and session.get("role") != "admin":
            abort(403)
        users.delete_station_comment(conn, comment_id)
    finally:
        conn.close()
    return _finish("station_detail", "コメントを削除しました。", name=name)


def _latest_station_thumbnail_url(name):
    """地点共有カード用の、直近取得済みのカメラ画像URL(無ければNone)。
    dashboard()と同じ「最新日付→最新時刻フォルダ」の辿り方をそのまま使う。"""
    latest_image_date = next(iter(_sorted_subdirs(IMAGE_DIR)), None)
    if not latest_image_date:
        return None
    latest_image_hour = next(iter(_sorted_subdirs(IMAGE_DIR / latest_image_date)), None)
    if not latest_image_hour:
        return None
    if not (IMAGE_DIR / latest_image_date / latest_image_hour / f"{name}.jpg").exists():
        return None
    return url_for("images_file", date=latest_image_date, hhmm=latest_image_hour, filename=f"{name}.jpg")


@app.route("/stations/<name>/share", methods=["POST"])
def station_share(name):
    """地点の現在の様子をリッチカード(地点名・現象色・サムネイル)として、選んだメンバー
    へのDM、またはグループチャットへワンタップ共有する。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    name = _validate_station(name)
    target_raw = request.form.get("user_id", "")
    to_group = target_raw == "group"
    user_id = None if to_group else (int(target_raw) if target_raw.isdigit() else None)
    if not to_group and (not user_id or user_id == my_id):
        return _finish("station_detail", "共有先を選んでください。", success=False, name=name)

    observations = _latest_observations()
    current = None
    if observations:
        current = {s["name"]: s for s in observations["stations"]}.get(name)
    card_data = json.dumps({
        "station": name,
        "label": current["label"] if current else None,
        "color": current["color"] if current else None,
        "text_color": current["text_color"] if current else None,
        "datetime": observations["datetime"].strftime("%Y/%m/%d %H:%M") if observations else None,
        "thumbnail_url": _latest_station_thumbnail_url(name),
        "url": url_for("station_detail", name=name),
    })
    body = f"{name}、{current['label']}です！" if current else f"{name}の様子を見てください。"
    created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")

    conn = _users_conn()
    try:
        if to_group:
            message_id = users.send_group_message(
                conn, my_id, body, created_at, card_type="station", card_data=card_data,
            )
            users.mark_group_read(conn, my_id, message_id)
        else:
            target = users.get_user_by_id(conn, user_id)
            if target is None:
                return _finish("station_detail", "共有先のメンバーが見つかりません。", success=False, name=name)
            conversation_id = users.get_or_create_dm_conversation(conn, my_id, user_id, created_at)
            message_id = users.send_dm_message(
                conn, conversation_id, my_id, body, created_at, card_type="station", card_data=card_data,
            )
            users.mark_dm_read(conn, conversation_id, my_id, message_id)
    finally:
        conn.close()
    if to_group:
        return _finish("group_home", "グループチャットに共有しました。")
    return _finish("dm_thread", f"{target['name']}さんに共有しました。", user_id=user_id)


@app.route("/stations/<name>/timelapse.json")
def station_timelapse_json(name):
    """タイムラプスで特定の日を選んだ際に、その日1日分の画像一覧を返す(Ajax用)。
    画像が無期限保存になり件数が増え続けるため、タイムラプス自体には直近分のみを
    埋め込み、日付を選んだ時だけその日の分を取りに行くようにしている。
    """
    name = _validate_station(name)
    date = request.args.get("date", "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        abort(400)
    items = []
    date_dir = IMAGE_DIR / date
    if date_dir.is_dir():
        for hhmm in _sorted_subdirs(date_dir, reverse=False):
            hour_dir = date_dir / hhmm
            for suffix in ("", "_unavailable"):
                f = hour_dir / f"{name}{suffix}.jpg"
                if f.exists():
                    items.append({
                        "url": url_for("images_file", date=date, hhmm=hhmm, filename=f.name),
                        "caption": f"{date} {hhmm[:2]}:{hhmm[2:]}" + ("(取得失敗)" if suffix else ""),
                    })
                    break
    return jsonify({"items": items})


@app.route("/stations/<name>/chart.png")
def station_chart(name):
    name = _validate_station(name)

    def render():
        trend = _accuracy_trend_safe(station=name)
        dates = [r["date"] for r in trend if r["n"] > 0]
        rates = [100.0 * r["correct"] / r["n"] for r in trend if r["n"] > 0]
        return _render_line_chart_png(dates, rates, f"{name} の的中率推移")

    png = _cached_png(f"station_chart:{name}", [DB_PATH], render)
    return Response(png, mimetype="image/png")


@app.route("/accuracy")
def accuracy_page():
    summary = _accuracy_summary_safe()
    by_weather = _accuracy_by_weather_safe()
    return render_template("accuracy.html", summary=summary, by_weather=by_weather)


@app.route("/accuracy/chart.png")
def accuracy_chart():
    def render():
        trend = _accuracy_trend_safe()
        dates = [r["date"] for r in trend if r["n"] > 0]
        rates = [100.0 * r["correct"] / r["n"] for r in trend if r["n"] > 0]
        return _render_line_chart_png(dates, rates, "全地点の的中率推移")

    png = _cached_png("accuracy_chart", [DB_PATH], render)
    return Response(png, mimetype="image/png")


@app.route("/changelog")
def changelog_page():
    return render_template("changelog.html", entries=_changelog_entries())


# ============================================================================
# 認証・会員ページ(ログイン・サインアップ・メンバー一覧・プロフィール)
# ============================================================================


@app.route("/login", methods=["GET", "POST"])
def login():
    next_path = request.values.get("next") or url_for("dashboard")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = url_for("dashboard")  # オープンリダイレクト対策

    error = None
    if request.method == "POST":
        ip = _client_ip()
        if _login_rate_limited(ip):
            error = "ログイン試行の回数が多すぎます。しばらくしてから再度お試しください。"
            return render_template("login.html", error=error, next=next_path)
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user_agent = request.headers.get("User-Agent", "")[:255]
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        conn = _users_conn()
        try:
            existing = users.get_user_by_email(conn, email)
            if existing and existing["locked_at"]:
                # 正しいパスワードでもロック中は一切ログインさせない(IPを変えても無意味にする)。
                users.record_login_failure(conn, email, ip, user_agent, now_str)
                error = "このアカウントはロックされています。管理者に解除を依頼してください。"
                return render_template("login.html", error=error, next=next_path)
            user = users.verify_login(conn, email, password)
        finally:
            conn.close()
        if user:
            session.clear()
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            session["name"] = user["name"]
            session.permanent = True
            login_at = now_str
            conn = _users_conn()
            try:
                if user["role"] == "admin" and not users.has_logged_in_from_ip(conn, user["id"], ip):
                    _send_alert_throttled(
                        f"admin_new_ip:{user['id']}:{ip}",
                        f"管理者「{user['name']}」({user['email']})が未知のIP({ip})からログインしました。"
                        f"心当たりが無い場合はパスワード変更・アカウントロックを検討してください。",
                    )
                users.record_login(conn, user["id"], login_at, ip)
                users.clear_login_failures(conn, user["email"])
                # 1日1回だけのログインボーナス。source文字列に日付を含めているので、
                # 同じ日に何度ログインしても2回目以降は付与されない。
                users.grant_points(
                    conn, user["id"], f"daily_login:{login_at[:10]}",
                    DAILY_LOGIN_BONUS_POINTS, login_at,
                )
            finally:
                conn.close()
            return redirect(next_path)
        _record_login_failure(ip)
        if existing:
            conn = _users_conn()
            try:
                users.record_login_failure(conn, email, ip, user_agent, now_str)
                fail_count = users.count_login_failures(conn, existing["email"])
                if fail_count >= ACCOUNT_LOCKOUT_THRESHOLD:
                    users.lock_account(conn, existing["id"], now_str)
            finally:
                conn.close()
            if fail_count >= ACCOUNT_LOCKOUT_THRESHOLD:
                _send_alert_throttled(
                    f"account_lock:{existing['id']}",
                    f"アカウント「{existing['name']}」({existing['email']})が連続ログイン失敗により"
                    f"ロックされました。直近アクセス元IP: {ip}",
                )
                error = "ログイン失敗が続いたため、このアカウントはロックされました。管理者に解除を依頼してください。"
            else:
                error = "メールアドレスまたはパスワードが違います。"
        else:
            error = "メールアドレスまたはパスワードが違います。"

    return render_template("login.html", error=error, next=next_path)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _avatar_path(user_id):
    return AVATAR_DIR / f"{user_id}.jpg"


@app.context_processor
def _inject_avatar_url():
    """テンプレートからmember_avatar_url(user_id)でアバター画像のURLを取れるようにする。
    画像が未設定ならNoneを返す(テンプレート側でイニシャル円などにフォールバックする)。
    ファイルのmtimeをクエリに付けて、差し替え後に古い画像がキャッシュされ続けるのを防ぐ。
    """
    def member_avatar_url(user_id):
        if user_id is None:
            return None
        path = _avatar_path(user_id)
        if not path.exists():
            return None
        return url_for("member_avatar_file", user_id=user_id) + f"?t={int(path.stat().st_mtime)}"
    return {"member_avatar_url": member_avatar_url}


_URL_RE = re.compile(r"(https?://\S+)")


@app.template_filter("linkify")
def linkify(text):
    """メッセージ本文中のURLをクリックできるリンクにする(地点共有ボタンが添える
    URLなど)。本文は自前でHTMLエスケープしてから組み立てるので、テンプレート側は
    {{ m.body|linkify }} とだけ書けば安全に埋め込める。"""
    from markupsafe import Markup, escape
    parts = _URL_RE.split(text or "")
    html = "".join(
        f'<a href="{escape(p)}" target="_blank" rel="noopener">{escape(p)}</a>'
        if _URL_RE.fullmatch(p) else str(escape(p))
        for p in parts
    )
    return Markup(html)


@app.template_filter("richbody")
def richbody(text, mentioned_names=None):
    """linkifyと同じ「エスケープしてから安全に組み立てる」パターンで、URLのリンク化と
    @メンション対象の表示名の太字ハイライトを1回の処理でまとめて行う(別々に適用すると
    二重エスケープになるため)。"""
    from markupsafe import Markup, escape
    mentioned_names = sorted(mentioned_names or [], key=len, reverse=True)
    parts = _URL_RE.split(text or "")
    out = []
    for p in parts:
        if _URL_RE.fullmatch(p):
            out.append(f'<a href="{escape(p)}" target="_blank" rel="noopener">{escape(p)}</a>')
            continue
        escaped = str(escape(p))
        for name in mentioned_names:
            token = f"@{escape(name)}"
            escaped = escaped.replace(str(token), f'<span class="mention-tag">{token}</span>')
        out.append(escaped)
    return Markup("".join(out))


@app.context_processor
def _inject_dm_unread_count():
    """base.htmlのナビに「メッセージ」の未読バッジを出すため。通知ベルと同じく
    ページ読み込み時に1回計算するだけで、スレッド表示中の反映はポーリング側で行う。"""
    def dm_unread_count():
        user_id = session.get("user_id")
        if not user_id:
            return 0
        conn = _users_conn()
        try:
            return users.total_dm_unread(conn, user_id)
        finally:
            conn.close()
    return {"dm_unread_count": dm_unread_count}


@app.context_processor
def _inject_group_unread_count():
    """base.htmlのナビに「グループ」の未読バッジを出すため(dm_unread_countと同じ形)。"""
    def group_unread_count():
        user_id = session.get("user_id")
        if not user_id:
            return 0
        conn = _users_conn()
        try:
            return users.group_unread_count(conn, user_id)
        finally:
            conn.close()
    return {"group_unread_count": group_unread_count}


@app.route("/api/nav_status")
def api_nav_status():
    """base.htmlのグローバルポーラーが15秒おきに叩く軽量API。ナビの未読バッジと、
    現在のページに映っているメンバー(data-user-id属性を持つ要素)のオンライン状況を、
    スレッドを開いていなくても・タブを切り替えなくても更新できるようにする。"""
    my_id = session.get("user_id")
    if not my_id:
        return jsonify({"dm_unread": 0, "group_unread": 0, "online_user_ids": []})
    ids_param = request.args.get("ids", "")
    watch_ids = [int(i) for i in ids_param.split(",") if i.strip().isdigit()][:100]
    conn = _users_conn()
    try:
        dm_unread = users.total_dm_unread(conn, my_id)
        group_unread = users.group_unread_count(conn, my_id)
        last_active = users.last_active_all(conn) if watch_ids else {}
    finally:
        conn.close()
    online_ids = [uid for uid in watch_ids if _is_online(last_active.get(uid))]
    return jsonify({"dm_unread": dm_unread, "group_unread": group_unread, "online_user_ids": online_ids})


# メンバーページの実績バッジ。未達成のものもロック表示するため、達成条件を関数として
# 持たせ、達成/未達成の両方をテンプレート側で判定できるようにしている。
BADGE_DEFINITIONS = [
    # ログイン回数(易→難、上位はcrownで統一しどのカテゴリでも「頂点」と分かるようにする)
    # pointsは達成の難易度に応じたポイント付与量(フレーム交換の原資)。初回付与時のみ加算され、
    # 何度ページを見ても再付与されない(_award_new_badges参照)。
    {"id": "first_login", "label": "はじめの一歩", "desc": "ログインした", "icon": "medal", "points": 100,
     "check": lambda s: s["login_count"] >= 1},
    {"id": "login_10", "label": "常連", "desc": "ログイン10回達成", "icon": "medal", "points": 300,
     "check": lambda s: s["login_count"] >= 10},
    {"id": "login_100", "label": "ヘビーユーザー", "desc": "ログイン100回達成", "icon": "medal", "points": 1500,
     "check": lambda s: s["login_count"] >= 100},
    {"id": "login_500", "label": "レジェンド", "desc": "ログイン500回達成", "icon": "crown", "points": 5000,
     "check": lambda s: s["login_count"] >= 500},
    # 連続ログイン日数
    {"id": "streak_3", "label": "三日坊主卒業", "desc": "3日連続ログイン", "icon": "flame", "points": 200,
     "check": lambda s: s["streak"] >= 3},
    {"id": "streak_7", "label": "一週間皆勤", "desc": "7日連続ログイン", "icon": "flame", "points": 500,
     "check": lambda s: s["streak"] >= 7},
    {"id": "streak_14", "label": "二週間皆勤", "desc": "14日連続ログイン", "icon": "flame", "points": 1000,
     "check": lambda s: s["streak"] >= 14},
    {"id": "streak_30", "label": "常習化", "desc": "30日連続ログイン", "icon": "flame", "points": 2000,
     "check": lambda s: s["streak"] >= 30},
    {"id": "streak_100", "label": "百日修行", "desc": "100日連続ログイン", "icon": "crown", "points": 5000,
     "check": lambda s: s["streak"] >= 100},
    # コメント投稿数
    {"id": "first_comment", "label": "発言デビュー", "desc": "地点にコメントを投稿した", "icon": "comment", "points": 100,
     "check": lambda s: s["comment_count"] >= 1},
    {"id": "comment_5", "label": "常連コメンター", "desc": "コメントを5件投稿した", "icon": "comment", "points": 400,
     "check": lambda s: s["comment_count"] >= 5},
    {"id": "comment_10", "label": "地点通", "desc": "コメントを10件投稿した", "icon": "comment", "points": 800,
     "check": lambda s: s["comment_count"] >= 10},
    {"id": "comment_30", "label": "コメント職人", "desc": "コメントを30件投稿した", "icon": "comment", "points": 2000,
     "check": lambda s: s["comment_count"] >= 30},
    {"id": "comment_100", "label": "コメントマスター", "desc": "コメントを100件投稿した", "icon": "crown", "points": 5000,
     "check": lambda s: s["comment_count"] >= 100},
    # AIチャット利用数
    {"id": "first_chat", "label": "AIとの対話", "desc": "AIチャットを利用した", "icon": "chat", "points": 100,
     "check": lambda s: s["chat_count"] >= 1},
    {"id": "chat_10", "label": "質問好き", "desc": "AIチャットで10回質問した", "icon": "chat", "points": 400,
     "check": lambda s: s["chat_count"] >= 10},
    {"id": "chat_50", "label": "探究者", "desc": "AIチャットで50回質問した", "icon": "crown", "points": 2000,
     "check": lambda s: s["chat_count"] >= 50},
    # お気に入り地点
    {"id": "favorite_3", "label": "お気に入り登録", "desc": "お気に入り地点を3件以上登録した", "icon": "star", "points": 200,
     "check": lambda s: s["favorite_count"] >= 3},
    {"id": "favorite_10", "label": "お気に入り愛好家", "desc": "お気に入り地点を10件以上登録した", "icon": "star", "points": 600,
     "check": lambda s: s["favorite_count"] >= 10},
    {"id": "favorite_all", "label": "全地点制覇", "desc": "全地点をお気に入り登録した", "icon": "crown", "points": 3000,
     "check": lambda s: s["favorite_count"] >= s["station_total"]},
    # 在籍日数
    {"id": "veteran_30", "label": "新米卒業", "desc": "登録から30日経過", "icon": "calendar", "points": 300,
     "check": lambda s: s["account_age_days"] >= 30},
    {"id": "veteran_180", "label": "半年選手", "desc": "登録から180日経過", "icon": "calendar", "points": 1200,
     "check": lambda s: s["account_age_days"] >= 180},
    {"id": "veteran_365", "label": "一周年", "desc": "登録から365日経過", "icon": "crown", "points": 3000,
     "check": lambda s: s["account_age_days"] >= 365},
    # プロフィール設定
    {"id": "avatar_set", "label": "プロフィール画像設定", "desc": "プロフィール画像を設定した", "icon": "camera", "points": 150,
     "check": lambda s: s["has_avatar"]},
    {"id": "bio_set", "label": "自己紹介デビュー", "desc": "自己紹介を入力した", "icon": "account", "points": 150,
     "check": lambda s: s["bio_set"]},
    # 権限
    {"id": "role_admin", "label": "運営メンバー", "desc": "管理者権限を持っている", "icon": "admin", "points": 1000,
     "check": lambda s: s["role"] == "admin"},
    # ログイン時間帯
    {"id": "night_owl", "label": "深夜の常連", "desc": "深夜0時〜4時台にログインしたことがある", "icon": "moon", "points": 300,
     "check": lambda s: s["night_owl"]},
    {"id": "early_bird", "label": "早起きさん", "desc": "早朝5時〜6時台にログインしたことがある", "icon": "sunrise", "points": 300,
     "check": lambda s: s["early_bird"]},
    # 週末利用
    {"id": "weekend_5", "label": "週末も欠かさず", "desc": "土曜日または日曜日にログインした日が5日以上ある", "icon": "calendar", "points": 600,
     "check": lambda s: s["weekend_days"] >= 5},
    # 上位ラダーの追加(既存カテゴリの、より遠い到達点)
    {"id": "login_1000", "label": "生き字引", "desc": "ログイン1000回達成", "icon": "crown", "points": 8000,
     "check": lambda s: s["login_count"] >= 1000},
    {"id": "comment_300", "label": "座談の主", "desc": "コメントを300件投稿した", "icon": "crown", "points": 6000,
     "check": lambda s: s["comment_count"] >= 300},
    {"id": "chat_200", "label": "知恵袋", "desc": "AIチャットで200回質問した", "icon": "crown", "points": 5000,
     "check": lambda s: s["chat_count"] >= 200},
    # 季節ログイン
    {"id": "newyear_login", "label": "元旦詣で", "desc": "1月1日にログインしたことがある", "icon": "calendar", "points": 400,
     "check": lambda s: s["newyear_login"]},
    {"id": "newyearseve_login", "label": "大晦日の見届け人", "desc": "12月31日にログインしたことがある", "icon": "calendar", "points": 400,
     "check": lambda s: s["newyearseve_login"]},
    # 累計ポイント(実績以外のポイント源も含めたこれまでの総獲得量。使い切っても下がらない)
    {"id": "points_1000", "label": "小金持ち", "desc": "累計獲得ポイントが1000ptに到達した", "icon": "medal", "points": 100,
     "check": lambda s: s["lifetime_points"] >= 1000},
    {"id": "points_10000", "label": "ポイント長者", "desc": "累計獲得ポイントが10000ptに到達した", "icon": "crown", "points": 1000,
     "check": lambda s: s["lifetime_points"] >= 10000},
    {"id": "points_25000", "label": "資産家", "desc": "累計獲得ポイントが25000ptに到達した", "icon": "medal", "points": 600,
     "check": lambda s: s["lifetime_points"] >= 25000},
    {"id": "points_50000", "label": "富豪", "desc": "累計獲得ポイントが50000ptに到達した", "icon": "medal", "points": 1500,
     "check": lambda s: s["lifetime_points"] >= 50000},
    {"id": "points_100000", "label": "億万長者", "desc": "累計獲得ポイントが100000ptに到達した", "icon": "crown", "points": 3000,
     "check": lambda s: s["lifetime_points"] >= 100000},
    {"id": "points_300000", "label": "只見川の大富豪", "desc": "累計獲得ポイントが300000ptに到達した", "icon": "crown", "points": 8000,
     "check": lambda s: s["lifetime_points"] >= 300000},
    {"id": "points_1000000", "label": "伝説の資産家", "desc": "累計獲得ポイントが100万ptに到達した", "icon": "crown", "points": 20000,
     "check": lambda s: s["lifetime_points"] >= 1000000},
    # フレームコレクション(交換ショップ自体を使ってもらうための実績)
    {"id": "frame_first", "label": "はじめての着せ替え", "desc": "アバターフレームを1つ交換した", "icon": "star", "points": 100,
     "check": lambda s: s["owned_frame_count"] >= 1},
    {"id": "frame_collector", "label": "フレームコレクター", "desc": "アバターフレームを5つ以上交換した", "icon": "star", "points": 500,
     "check": lambda s: s["owned_frame_count"] >= 5},
    {"id": "frame_master", "label": "フレームマスター", "desc": "全種類のアバターフレームを交換した", "icon": "crown", "points": 3000,
     # FRAME_DEFINITIONSはこの後で定義されるが、lambda本体はaward/表示のたびに評価される
     # ため問題ない(定義時ではなく呼び出し時にグローバル名を解決するPythonの通常の挙動)。
     "check": lambda s: s["owned_frame_count"] >= len(FRAME_DEFINITIONS)},
    # 川霧ギャンブルの的中実績
    {"id": "gamble_first_win", "label": "初勝利", "desc": "川霧ギャンブルで初めて賭けを的中させた", "icon": "star", "points": 200,
     "check": lambda s: s["gamble_won_count"] >= 1},
    {"id": "gamble_won_20", "label": "ギャンブラー", "desc": "川霧ギャンブルで20回的中させた", "icon": "star", "points": 600,
     "check": lambda s: s["gamble_won_count"] >= 20},
    {"id": "gamble_won_50", "label": "目利き", "desc": "川霧ギャンブルで50回的中させた", "icon": "star", "points": 1500,
     "check": lambda s: s["gamble_won_count"] >= 50},
    {"id": "gamble_won_100", "label": "霧予知能力者", "desc": "川霧ギャンブルで100回的中させた", "icon": "crown", "points": 3000,
     "check": lambda s: s["gamble_won_count"] >= 100},
    {"id": "gamble_won_500", "label": "川霧の神様", "desc": "川霧ギャンブルで500回的中させた", "icon": "crown", "points": 10000,
     "check": lambda s: s["gamble_won_count"] >= 500},
    # メッセージ機能の利用(DM・グループチャット合算)
    {"id": "msg_10", "label": "おしゃべりデビュー", "desc": "DM・グループチャットで合計10件送信した", "icon": "chat", "points": 200,
     "check": lambda s: s["msg_count"] >= 10},
    {"id": "msg_100", "label": "おしゃべり好き", "desc": "DM・グループチャットで合計100件送信した", "icon": "chat", "points": 800,
     "check": lambda s: s["msg_count"] >= 100},
    {"id": "msg_500", "label": "コミュ力おばけ", "desc": "DM・グループチャットで合計500件送信した", "icon": "crown", "points": 3000,
     "check": lambda s: s["msg_count"] >= 500},
    {"id": "reaction_given_50", "label": "気配り上手", "desc": "リアクションを50回送った", "icon": "star", "points": 400,
     "check": lambda s: s["reaction_given_count"] >= 50},
    {"id": "reaction_received_50", "label": "人気者", "desc": "リアクションを50回もらった", "icon": "star", "points": 400,
     "check": lambda s: s["reaction_received_count"] >= 50},
    {"id": "mention_received_20", "label": "頼られる存在", "desc": "@メンションを20回もらった", "icon": "comment", "points": 400,
     "check": lambda s: s["mention_received_count"] >= 20},
    # 無料デイリーゲーム
    {"id": "fog_quiz_correct_10", "label": "予報士見習い", "desc": "霧予報クイズに10回正解した", "icon": "forecast", "points": 300,
     "check": lambda s: s["fog_quiz_correct"] >= 10},
    {"id": "fog_quiz_correct_50", "label": "予報士", "desc": "霧予報クイズに50回正解した", "icon": "forecast", "points": 1200,
     "check": lambda s: s["fog_quiz_correct"] >= 50},
    {"id": "fog_quiz_correct_200", "label": "名予報士", "desc": "霧予報クイズに200回正解した", "icon": "crown", "points": 4000,
     "check": lambda s: s["fog_quiz_correct"] >= 200},
    {"id": "phenom_quiz_correct_20", "label": "現象マニア", "desc": "現象当てクイズに20回正解した", "icon": "accuracy", "points": 400,
     "check": lambda s: s["phenom_quiz_correct"] >= 20},
    {"id": "phenom_quiz_correct_100", "label": "現象博士", "desc": "現象当てクイズに100回正解した", "icon": "crown", "points": 2500,
     "check": lambda s: s["phenom_quiz_correct"] >= 100},
    {"id": "lottery_30", "label": "くじ引き常連", "desc": "デイリーくじを30日分引いた", "icon": "star", "points": 300,
     "check": lambda s: s["lottery_draws"] >= 30},
    {"id": "lottery_180", "label": "くじの達人", "desc": "デイリーくじを180日分引いた", "icon": "crown", "points": 2000,
     "check": lambda s: s["lottery_draws"] >= 180},
    # ファイル共有
    {"id": "file_upload_5", "label": "共有デビュー", "desc": "ファイルを5件アップロードした", "icon": "attach", "points": 200,
     "check": lambda s: s["file_upload_count"] >= 5},
    {"id": "file_upload_30", "label": "資料係", "desc": "ファイルを30件アップロードした", "icon": "attach", "points": 1000,
     "check": lambda s: s["file_upload_count"] >= 30},
    {"id": "file_star_10", "label": "整理整頓", "desc": "ファイルを10件お気に入り登録した", "icon": "star", "points": 300,
     "check": lambda s: s["file_star_count"] >= 10},
    # ショップコレクション
    {"id": "title_complete", "label": "称号コンプリート", "desc": "全ての称号を交換した", "icon": "crown", "points": 2000,
     "check": lambda s: s["title_complete"]},
    {"id": "name_color_complete", "label": "名前の色コンプリート", "desc": "全ての名前の色を交換した", "icon": "crown", "points": 1500,
     "check": lambda s: s["name_color_complete"]},
    {"id": "points_spent_10000", "label": "太っ腹", "desc": "ショップで累計10000pt使った", "icon": "medal", "points": 500,
     "check": lambda s: s["points_spent"] >= 10000},
    {"id": "points_spent_50000", "label": "浪費家", "desc": "ショップで累計50000pt使った", "icon": "crown", "points": 2000,
     "check": lambda s: s["points_spent"] >= 50000},
    # 月間ランキング
    {"id": "ranking_first_1", "label": "月間王者", "desc": "月間ログインランキングで1位を取った", "icon": "crown", "points": 500,
     "check": lambda s: s["ranking_first_count"] >= 1},
    {"id": "ranking_first_3", "label": "常勝の風格", "desc": "月間ログインランキングで1位を3回取った", "icon": "crown", "points": 2000,
     "check": lambda s: s["ranking_first_count"] >= 3},
    {"id": "ranking_first_12", "label": "只見川の覇者", "desc": "月間ログインランキングで1位を12回取った", "icon": "crown", "points": 6000,
     "check": lambda s: s["ranking_first_count"] >= 12},
    # 地点共有カード
    {"id": "station_share_10", "label": "情報通", "desc": "地点情報を10回共有した", "icon": "share", "points": 250,
     "check": lambda s: s["station_share_count"] >= 10},
    {"id": "station_share_50", "label": "地点ソムリエ", "desc": "地点情報を50回共有した", "icon": "crown", "points": 1500,
     "check": lambda s: s["station_share_count"] >= 50},
]

# --- 実績以外のポイント源 ---------------------------------------------------------
# 一度きりの実績達成だけでなく、日々の利用そのものからも少しずつポイントが貯まるように
# する。users.grant_points()のsource文字列で1回ごとの重複付与を防ぐ
# (例: 同じ日の2回目のログインでは付与しない、同じコメントに2回付与しない)。
DAILY_LOGIN_BONUS_POINTS = 20
COMMENT_POST_POINTS = 15
CHAT_MESSAGE_POINTS = 10

# --- ポイント交換アバターフレーム -------------------------------------------------
# 実績ポイントで交換できるアバター装飾枠。「段階(グレード)」×「テーマ」の組み合わせで
# 4x5=20種類になる。CSS側は段階クラス(リング太さ・発光・アニメーション)とテーマクラス
# (配色)を掛け合わせて表現するため、フレーム自体を20個ハードコードする必要はない
# (style.cssの.avatar-frame関連ルール参照)。
FRAME_GRADES = [
    {"id": "bronze", "label": "銅", "cost": 1000},
    {"id": "silver", "label": "銀", "cost": 2500},
    {"id": "gold", "label": "金", "cost": 5000},
    {"id": "platinum", "label": "虹", "cost": 10000},
    {"id": "legend", "label": "伝説", "cost": 20000},
]
FRAME_THEMES = [
    {"id": "kiri", "label": "霧"},
    {"id": "hare", "label": "晴天"},
    {"id": "yuuyake", "label": "夕焼け"},
    {"id": "yuki", "label": "雪"},
    {"id": "hoshizora", "label": "星空"},
    {"id": "momiji", "label": "紅葉"},
    {"id": "sakura", "label": "桜"},
    {"id": "hanabi", "label": "花火"},
    {"id": "mangetsu", "label": "満月"},
    {"id": "aurora", "label": "オーロラ"},
]
FRAME_DEFINITIONS = [
    {
        "id": f"{grade['id']}_{theme['id']}",
        "label": f"{grade['label']}の{theme['label']}",
        "grade": grade["id"], "grade_label": grade["label"], "cost": grade["cost"],
        "theme": theme["id"], "theme_label": theme["label"],
    }
    for grade in FRAME_GRADES for theme in FRAME_THEMES
]
FRAME_BY_ID = {f["id"]: f for f in FRAME_DEFINITIONS}

# --- 称号・名前の色・ダッシュボード配色・演出 ---------------------------------------
# フレーム以外のショップ品。装備の仕組みはusers.py側で共通化しているが(user_cosmetics、
# category列で区別)、カタログ(何が買えるか)はここに種類ごとに定義する。
TITLE_DEFINITIONS = [
    {"id": "regular", "label": "常連さん", "cost": 500},
    {"id": "early_riser", "label": "早起き名人", "cost": 1200},
    {"id": "fog_walker", "label": "霧使い", "cost": 3000},
    {"id": "tadami_master", "label": "只見川マスター", "cost": 8000},
    {"id": "legendary_observer", "label": "伝説の観測者", "cost": 15000},
    {"id": "dopagaki", "label": "ドパガキ", "cost": 50000},
    {"id": "legendary_gambler", "label": "伝説のギャンブラー", "cost": 1000000},
    {"id": "admin_title", "label": "管理人", "cost": 0, "admin_only": True},
]
TITLE_BY_ID = {t["id"]: t for t in TITLE_DEFINITIONS}

NAME_COLOR_DEFINITIONS = [
    {"id": "red", "label": "赤", "cost": 800, "css_class": "name-color-red"},
    {"id": "blue", "label": "青", "cost": 800, "css_class": "name-color-blue"},
    {"id": "green", "label": "緑", "cost": 800, "css_class": "name-color-green"},
    {"id": "gold", "label": "金", "cost": 2000, "css_class": "name-color-gold"},
    {"id": "rainbow", "label": "虹色", "cost": 5000, "css_class": "name-color-rainbow"},
]
NAME_COLOR_BY_ID = {c["id"]: c for c in NAME_COLOR_DEFINITIONS}

DASHBOARD_THEME_DEFINITIONS = [
    {"id": "yuuyake", "label": "夕焼け", "cost": 1500, "css_class": "dash-theme-yuuyake"},
    {"id": "hoshizora", "label": "星空", "cost": 2500, "css_class": "dash-theme-hoshizora"},
    {"id": "shinryoku", "label": "新緑", "cost": 1500, "css_class": "dash-theme-shinryoku"},
    {"id": "yukigesho", "label": "雪景色", "cost": 1500, "css_class": "dash-theme-yukigesho"},
]
DASHBOARD_THEME_BY_ID = {d["id"]: d for d in DASHBOARD_THEME_DEFINITIONS}

EFFECT_DEFINITIONS = [
    {"id": "fog_drift", "label": "霧の揺らめき", "cost": 4000, "css_class": "fx-fog-drift"},
    {"id": "sparkle", "label": "きらめき", "cost": 4000, "css_class": "fx-sparkle"},
    {"id": "aurora_glow", "label": "オーロラの光", "cost": 6000, "css_class": "fx-aurora-glow"},
]
EFFECT_BY_ID = {e["id"]: e for e in EFFECT_DEFINITIONS}

BANNER_DEFINITIONS = [
    {"id": "kiri", "label": "霧景色", "cost": 2000, "css_class": "banner-kiri"},
    {"id": "yuuyake", "label": "夕焼け", "cost": 2000, "css_class": "banner-yuuyake"},
    {"id": "yukigesho", "label": "雪景色", "cost": 2000, "css_class": "banner-yukigesho"},
    {"id": "hoshizora", "label": "星空", "cost": 3000, "css_class": "banner-hoshizora"},
]
BANNER_BY_ID = {b["id"]: b for b in BANNER_DEFINITIONS}

DM_THEME_DEFINITIONS = [
    {"id": "kiri", "label": "霧", "cost": 1500, "css_class": "dm-theme-kiri"},
    {"id": "yuuyake", "label": "夕焼け", "cost": 1500, "css_class": "dm-theme-yuuyake"},
    {"id": "mint", "label": "ミント", "cost": 1500, "css_class": "dm-theme-mint"},
]
DM_THEME_BY_ID = {d["id"]: d for d in DM_THEME_DEFINITIONS}

# 名前の色と同じCSSクラス(name-color-*)をそのまま流用する(色の見た目自体は
# 用途によらず共通なので、称号用に別のクラスを増やす必要が無い)。
TITLE_COLOR_DEFINITIONS = [
    {"id": "red", "label": "赤", "cost": 600, "css_class": "name-color-red"},
    {"id": "blue", "label": "青", "cost": 600, "css_class": "name-color-blue"},
    {"id": "green", "label": "緑", "cost": 600, "css_class": "name-color-green"},
    {"id": "gold", "label": "金", "cost": 1500, "css_class": "name-color-gold"},
    {"id": "rainbow", "label": "虹色", "cost": 4000, "css_class": "name-color-rainbow"},
]
TITLE_COLOR_BY_ID = {c["id"]: c for c in TITLE_COLOR_DEFINITIONS}

MARKER_DEFINITIONS = [
    {"id": "hall_of_fame", "label": "殿堂入り章", "cost": 25000, "icon": "★"},
]
MARKER_BY_ID = {m["id"]: m for m in MARKER_DEFINITIONS}

# --- 2026-09-03追加のショップ拡張分 ------------------------------------------------
# NEW_ITEM_ADDED_ATはこのバッチで追加したカタログ品に付けた"added"日付。ショップ側で
# 一定期間(SHOP_NEW_BADGE_DAYS)だけ「NEW」バッジを出すのに使う。
NEW_ITEM_ADDED_AT = "2026-09-03"
SHOP_NEW_BADGE_DAYS = 21

TITLE_BADGE_DEFINITIONS = [
    {"id": "sparkle", "label": "きらきら", "cost": 600, "icon": "✨", "added": NEW_ITEM_ADDED_AT},
    {"id": "fire", "label": "めらめら", "cost": 600, "icon": "🔥", "added": NEW_ITEM_ADDED_AT},
    {"id": "drop", "label": "しずく", "cost": 800, "icon": "💧", "added": NEW_ITEM_ADDED_AT},
    {"id": "gem", "label": "きらめく宝石", "cost": 2500, "icon": "💎", "added": NEW_ITEM_ADDED_AT},
]
TITLE_BADGE_BY_ID = {t["id"]: t for t in TITLE_BADGE_DEFINITIONS}

BUBBLE_STYLE_DEFINITIONS = [
    {"id": "round", "label": "まる型", "cost": 1200, "css_class": "bubble-style-round", "added": NEW_ITEM_ADDED_AT},
    {"id": "square", "label": "かく型", "cost": 1200, "css_class": "bubble-style-square", "added": NEW_ITEM_ADDED_AT},
    {"id": "handwritten", "label": "手書き風", "cost": 2500, "css_class": "bubble-style-handwritten", "added": NEW_ITEM_ADDED_AT},
]
BUBBLE_STYLE_BY_ID = {b["id"]: b for b in BUBBLE_STYLE_DEFINITIONS}

AVATAR_BG_DEFINITIONS = [
    {"id": "glow_blue", "label": "青の光暈", "cost": 1800, "css_class": "avatar-bg-glow-blue", "added": NEW_ITEM_ADDED_AT},
    {"id": "glow_gold", "label": "金の光暈", "cost": 3500, "css_class": "avatar-bg-glow-gold", "added": NEW_ITEM_ADDED_AT},
    {"id": "mist", "label": "霧の背景", "cost": 2200, "css_class": "avatar-bg-mist", "added": NEW_ITEM_ADDED_AT},
]
AVATAR_BG_BY_ID = {a["id"]: a for a in AVATAR_BG_DEFINITIONS}

RANK_STYLE_DEFINITIONS = [
    {"id": "gold_number", "label": "金の数字", "cost": 1500, "css_class": "rank-style-gold", "added": NEW_ITEM_ADDED_AT},
    {"id": "neon_number", "label": "ネオンの数字", "cost": 1500, "css_class": "rank-style-neon", "added": NEW_ITEM_ADDED_AT},
    {"id": "serif_number", "label": "書道体の数字", "cost": 1500, "css_class": "rank-style-serif", "added": NEW_ITEM_ADDED_AT},
]
RANK_STYLE_BY_ID = {r["id"]: r for r in RANK_STYLE_DEFINITIONS}

# soundは効果音の種類ID。実際の音はWeb Audio APIでmember_detail.html側が合成するので、
# 音声ファイルは持たない(依存を増やさないため)。
PROFILE_SOUND_DEFINITIONS = [
    {"id": "chime", "label": "チャイム", "cost": 1000, "sound": "chime", "added": NEW_ITEM_ADDED_AT},
    {"id": "bell", "label": "鈴の音", "cost": 1000, "sound": "bell", "added": NEW_ITEM_ADDED_AT},
    {"id": "kirakira", "label": "きらめき音", "cost": 1800, "sound": "kirakira", "added": NEW_ITEM_ADDED_AT},
]
PROFILE_SOUND_BY_ID = {p["id"]: p for p in PROFILE_SOUND_DEFINITIONS}

# 季節限定コレクション。available_from/available_untilの期間外は新規購入できないが、
# 既に持っている分はそのまま装備・表示し続けられる(買い方だけを期間で絞る)。
SEASONAL_DEFINITIONS = [
    {
        "id": "summer_2026", "label": "夏祭りの提灯", "cost": 1800, "css_class": "seasonal-summer",
        "available_from": "2026-07-01", "available_until": "2026-08-31", "added": NEW_ITEM_ADDED_AT,
    },
    {
        "id": "autumn_2026", "label": "紅葉の川面", "cost": 1800, "css_class": "seasonal-autumn",
        "available_from": "2026-10-01", "available_until": "2026-11-30", "added": NEW_ITEM_ADDED_AT,
    },
    {
        "id": "newyear_2027", "label": "迎春の霧", "cost": 2200, "css_class": "seasonal-newyear",
        "available_from": "2026-12-15", "available_until": "2027-01-15", "added": NEW_ITEM_ADDED_AT,
    },
]
SEASONAL_BY_ID = {s["id"]: s for s in SEASONAL_DEFINITIONS}

GACHA_COST = 5000
GACHA_BIG_REFUND_MULTIPLIER = 2.5
GACHA_BIG_REFUND_WEIGHT = 5
GACHA_CONSOLATION_POINTS = 300

# --- 川霧ギャンブル・デイリーゲーム共通の消費アイテム ---------------------------------
CONSUMABLE_DEFINITIONS = [
    {
        "id": "insurance_ticket", "label": "保険チケット", "cost": 300,
        "desc": "川霧ギャンブルの賭けがハズレても、賭けたポイントの半額が払い戻されます。",
    },
    {
        "id": "boost_ticket", "label": "倍率ブーストチケット", "cost": 250,
        "desc": "川霧ギャンブルの賭けの倍率が10%上がります(的中時の払い戻しが増えます)。",
    },
    {
        "id": "lottery_luck_ticket", "label": "ラッキーチケット", "cost": 200,
        "desc": "デイリーくじを引く時に使うと、高額ポイントの出やすさが上がります。",
    },
    {
        "id": "quiz_hint_ticket", "label": "クイズヒントチケット", "cost": 150,
        "desc": "現象当てクイズで、誤った選択肢を1つ消せます。",
    },
    {
        "id": "super_boost_ticket", "label": "上級倍率ブーストチケット", "cost": 600,
        "desc": "川霧ギャンブルの賭けの倍率が25%上がります(倍率ブーストチケットの上位版。同時使用時はこちらが優先されます)。",
        "added": NEW_ITEM_ADDED_AT,
    },
    {
        "id": "withdraw_ticket", "label": "見送り券", "cost": 200,
        "desc": "観測時間帯が始まる前なら、賭けた後でもこのチケットで取り消し、全額払い戻せます。",
        "added": NEW_ITEM_ADDED_AT,
    },
]
CONSUMABLE_BY_ID = {c["id"]: c for c in CONSUMABLE_DEFINITIONS}
CONSUMABLE_BOOST_MULTIPLIER = 1.1
CONSUMABLE_SUPER_BOOST_MULTIPLIER = 1.25
CONSUMABLE_INSURANCE_REFUND_RATIO = 0.5
# ラッキーチケット使用時、100pt以上の枠の重みをこの倍率で底上げする(重みの再配分)。
LOTTERY_LUCK_WEIGHT_MULTIPLIER = 3

# --- 川霧ギャンブル ---------------------------------------------------------------
# 「この地点でこの時刻に川霧が出るか」にポイントを賭けるゲーム。
# 倍率 = 地点の難易度(過去の川霧発生率が低いほど高倍率) × 先の長さ(遠い予測ほど高倍率)
#      × 時間精度(ざっくり日付だけ<時間帯<正確な1時間、細かく当てるほど高倍率)
# 賭けた時点でポイントは引かれ(fog_bets.bet_amountがtotal_spent_pointsに含まれる)、
# 的中すれば bet_amount * multiplier をgrant_pointsで払い戻す。外れれば戻ってこない
# (名前どおりの「ギャンブル」として、当てずっぽうの丁半博打にならないよう
# 難易度に応じた倍率を付ける)。
GAMBLE_MIN_BET = 10
GAMBLE_MAX_LEAD_HOURS = 24 * 7  # 1週間先まで賭けられる
GAMBLE_MULTIPLIER_CAP = 30.0
GAMBLE_STATION_RARITY_FLOOR = 0.02  # 観測史上ほぼ川霧が出ない地点でも倍率を無限大にしない下限
GAMBLE_STATION_RARITY_CAP = 15.0
# 観測時間帯(04-19 JST)を3つの時間帯(block精度)に区切る。
GAMBLE_BLOCKS = [(4, 9, "早朝〜午前(4-9時)"), (9, 14, "昼(9-14時)"), (14, 19, "夕方(14-19時)")]
GAMBLE_PRECISIONS = [
    {"id": "day", "label": "日(その日いつでも)", "multiplier": 1.0},
    {"id": "block", "label": "時間帯(5時間区切り)", "multiplier": 1.8},
    {"id": "hour", "label": "正確な1時間", "multiplier": 3.0},
]
GAMBLE_PRECISION_BY_ID = {p["id"]: p for p in GAMBLE_PRECISIONS}

# --- デイリー霧予報クイズ(無料・スキル型、川霧ギャンブルと違いポイントを賭けない) -------
# 毎日同じ地点を全員に出題する(日付から決定論的に選ぶ)ことで、その日1回だけの
# 気軽な予想ゲームにする。的中報酬に連続的中日数のボーナスを付け、継続を後押しする。
FOG_QUIZ_BASE_POINTS = 10
FOG_QUIZ_STREAK_BONUS_PER_DAY = 2
FOG_QUIZ_STREAK_BONUS_CAP_DAYS = 20  # ボーナス上限 = 20日 x 2pt = 40pt

# --- デイリーくじ -------------------------------------------------------------------
# 専用テーブルは持たず、grant_points()のUNIQUE(user_id, source)制約をそのまま
# 「1日1回」の判定に使う(users.get_point_event_amount参照)。
LOTTERY_OUTCOMES = [(50, 10), (30, 30), (15, 100), (4, 300), (1, 1000)]  # (重み, ポイント)

# --- デイリー現象当てクイズ(無料、即時採点) -----------------------------------------
# 1〜10の細かいコードそのままでは難しすぎるため、フォームUI上は5カテゴリに丸めて出題する
# (画像判定・訂正機能そのものはこれまで通り0〜10の全コードを扱う。丸めるのはクイズの
# 表示上だけ)。
PHENOM_QUIZ_CATEGORIES = [
    {"id": "kawagiri", "label": "川霧", "codes": {1, 2, 3}},
    {"id": "zentaigiri", "label": "全体霧", "codes": {4, 5, 6}},
    {"id": "souun", "label": "層雲", "codes": {7, 8}},
    {"id": "ame", "label": "霧雨・雨", "codes": {9, 10}},
    {"id": "nashi", "label": "現象なし", "codes": {0}},
]
PHENOM_QUIZ_POINTS = 15
OBSERVATION_REPORT_REWARD_POINTS = 2000
PHENOM_QUIZ_CANDIDATES_CACHE_SECONDS = 600


def _phenom_category_id_for_code(code):
    for cat in PHENOM_QUIZ_CATEGORIES:
        if code in cat["codes"]:
            return cat["id"]
    return None


def _daily_fog_quiz_station(date_str):
    """その日の出題地点を日付から決定論的に選ぶ(全員同じ地点、日付が変われば変わる)。"""
    return random.Random(f"fog_quiz:{date_str}").choice(STATION_NAMES)


_phenom_quiz_candidates_cache = {"data": None, "at": 0.0}
_phenom_quiz_candidates_lock = threading.Lock()


def _phenom_quiz_candidates():
    """(日付, HHMM, 地点名, 現象コード)のうち、実際に画像ファイルが存在し、かつ
    現象コードが判明している(未分類でない)組み合わせのリストを返す。画像アーカイブは
    無期限保存だが実運用開始以降の分しか無いため、対象はその期間に限られる
    (データが増えるほど出題の幅が自然に広がる)。"""
    now = time.monotonic()
    with _phenom_quiz_candidates_lock:
        cached = _phenom_quiz_candidates_cache["data"]
        if cached is not None and now - _phenom_quiz_candidates_cache["at"] < PHENOM_QUIZ_CANDIDATES_CACHE_SECONDS:
            return cached
    candidates = []
    if IMAGE_DIR.exists() and WEATHER_DB_PATH.exists():
        try:
            image_dates = list(_sorted_subdirs(IMAGE_DIR))
            if image_dates:
                _, phenom_df, station_names = fog_data.load_river_fog_data()
                date_set = set(image_dates)
                phenom_df = phenom_df[phenom_df["datetime"].dt.strftime("%Y-%m-%d").isin(date_set)]
                code_lookup = {}
                for _, row in phenom_df.iterrows():
                    dt_str = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
                    for name in station_names:
                        code = encode_phenomena_cell(row[name])
                        if code == code and code >= 0:  # NaN(未分類)・-1(無効画像)を除く
                            code_lookup[(dt_str, name)] = int(code)
                for date_dir in image_dates:
                    for hhmm in _sorted_subdirs(IMAGE_DIR / date_dir):
                        dt_str = f"{date_dir} {hhmm[:2]}:{hhmm[2:]}:00"
                        for name in station_names:
                            code = code_lookup.get((dt_str, name))
                            if code is None:
                                continue
                            if (IMAGE_DIR / date_dir / hhmm / f"{name}.jpg").exists():
                                candidates.append((date_dir, hhmm, name, code))
        except Exception as e:
            log.warning("Failed to build phenom quiz candidates: %s", e)
    with _phenom_quiz_candidates_lock:
        _phenom_quiz_candidates_cache["data"] = candidates
        _phenom_quiz_candidates_cache["at"] = now
    return candidates


def _todays_phenom_quiz_item(date_str):
    """その日の出題(date_dir, hhmm, station_name, code)を日付から決定論的に選ぶ
    (候補が無ければNone)。"""
    candidates = _phenom_quiz_candidates()
    if not candidates:
        return None
    return random.Random(f"phenom_quiz:{date_str}").choice(candidates)


def _resolve_pending_daily_fog_quiz():
    """観測日が終わっている(今日より前の)未解決のデイリー霧予報クイズを答え合わせする。
    賭けの答え合わせ(_resolve_pending_fog_bets)・バッジ付与と同じく、ゲーム関連ページを
    開いたタイミングで遅延実行する(専用のcronは持たない)。"""
    conn = _users_conn()
    try:
        today_str = now_jst_naive().strftime("%Y-%m-%d")
        pending = users.list_pending_daily_fog_quiz(conn, today_str)
        if not pending:
            return
        if not WEATHER_DB_PATH.exists():
            return
        _, phenom_df, _ = fog_data.load_river_fog_data()
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        for entry in pending:
            station = entry["station_name"]
            quiz_date = entry["quiz_date"]
            if station not in phenom_df.columns:
                users.resolve_daily_fog_quiz(conn, entry["user_id"], quiz_date, "incorrect", now_str, 0)
                continue
            day_start, day_end = f"{quiz_date} 00:00:00", f"{quiz_date} 23:59:59"
            window = phenom_df[(phenom_df["datetime"] >= day_start) & (phenom_df["datetime"] <= day_end)]
            codes = window[station].apply(encode_phenomena_cell)
            occurred = bool(codes.dropna().isin(SUMMARY_FOG_CODES).any())
            correct = occurred == entry["guess"]
            new_streak = users.update_daily_fog_quiz_stats(conn, entry["user_id"], correct)
            if correct:
                bonus = min(new_streak, FOG_QUIZ_STREAK_BONUS_CAP_DAYS) * FOG_QUIZ_STREAK_BONUS_PER_DAY
                payout = FOG_QUIZ_BASE_POINTS + bonus
                users.grant_points(conn, entry["user_id"], f"fog_quiz:{quiz_date}", payout, now_str)
                users.resolve_daily_fog_quiz(conn, entry["user_id"], quiz_date, "correct", now_str, payout)
            else:
                users.resolve_daily_fog_quiz(conn, entry["user_id"], quiz_date, "incorrect", now_str, 0)
    except Exception as e:
        log.warning("Failed to resolve daily fog quiz: %s", e)
    finally:
        conn.close()


_station_fog_rate_cache = {"data": None, "at": 0.0}
_station_fog_rate_cache_lock = threading.Lock()


def _station_fog_rates():
    """地点ごとの、観測時間帯(04-19 JST)における川霧発生率(0〜1)を返す({地点名: 発生率})。
    データが無い/読めない場合は空dict(倍率計算側でフロア値にフォールバックする)。"""
    now = time.monotonic()
    with _station_fog_rate_cache_lock:
        cached = _station_fog_rate_cache["data"]
        if cached is not None and now - _station_fog_rate_cache["at"] < ACCURACY_SUMMARY_CACHE_SECONDS:
            return cached
    if not WEATHER_DB_PATH.exists():
        return {}
    try:
        main_df, phenom_df, station_names = fog_data.load_river_fog_data()
        rates = {}
        for name in station_names or []:
            if name not in phenom_df.columns:
                continue
            merged = main_df.merge(phenom_df[["datetime", name]], on="datetime", how="inner")
            codes = merged[name].apply(encode_phenomena_cell)
            valid = codes.notna()
            n = int(valid.sum())
            if n == 0:
                continue
            fog_n = int(codes.loc[valid].isin(SUMMARY_FOG_CODES).sum())
            rates[name] = fog_n / n
    except Exception as e:
        log.warning("Failed to compute station fog rates: %s", e)
        return {}
    with _station_fog_rate_cache_lock:
        _station_fog_rate_cache["data"] = rates
        _station_fog_rate_cache["at"] = now
    return rates


def _gamble_multiplier(fog_rate, lead_hours, precision_id):
    """賭けの倍率を計算する。fog_rateがNone(データ不足)の場合は最も保守的な
    (rarity_mult=1.0の)側に倒す。"""
    if fog_rate is None:
        rarity_mult = 1.0
    else:
        rarity_mult = min(max(1.0 / max(fog_rate, GAMBLE_STATION_RARITY_FLOOR), 1.0), GAMBLE_STATION_RARITY_CAP)
    lead_mult = 1.0 + min(max(lead_hours, 0), GAMBLE_MAX_LEAD_HOURS) / GAMBLE_MAX_LEAD_HOURS * 2.0
    precision_mult = GAMBLE_PRECISION_BY_ID.get(precision_id, {"multiplier": 1.0})["multiplier"]
    total = rarity_mult * lead_mult * precision_mult
    return round(min(total, GAMBLE_MULTIPLIER_CAP), 2)


def _gamble_window_for_bet(target_date, precision_id, hour=None, block_index=None):
    """precisionに応じて(window_start, window_end)のdatetime文字列ペアを返す。
    観測時間帯(04-19 JST)の外を指定された場合はNoneを返す(呼び出し側でエラー扱い)。"""
    if precision_id == "day":
        start_h, end_h = 4, 19
    elif precision_id == "block":
        if block_index is None or not (0 <= block_index < len(GAMBLE_BLOCKS)):
            return None
        start_h, end_h = GAMBLE_BLOCKS[block_index][0], GAMBLE_BLOCKS[block_index][1]
    elif precision_id == "hour":
        if hour is None or not (4 <= hour <= 19):
            return None
        start_h, end_h = hour, hour
    else:
        return None
    window_start = f"{target_date} {start_h:02d}:00:00"
    window_end = f"{target_date} {end_h:02d}:00:00"
    return window_start, window_end


# collect.pyの毎時収集は画像取得+Gemini分類を伴うため、その時刻ちょうどに終わるとは限らず、
# 実測では数十秒〜(まれに)数分かかる(logs/collect.logで確認済み: ある回はwindow_end+26秒、
# 別の回は+2分46秒で完了していた)。window_endを過ぎた瞬間に答え合わせすると、まだ収集が
# 終わっていないデータを「霧は観測されなかった」と誤認して外れ扱いにしてしまう
# (2026-09-03に実際に発生し、window_endのわずか14秒後に3件が誤って「はずれ」判定されていた:
# 本来は川霧が観測されていたのに、その時点ではまだ該当時刻のデータがweather.dbに
# 書き込まれておらず、他地点も含め「その時刻の行が1つも無い」状態で判定してしまっていた)。
# そのため、対象時刻(window_end)のデータがまだ存在しない場合は「まだ判定できない」として
# 今回は見送り、次にゲーム関連ページが開かれた時に再試行する。収集が恒久的に失敗している
# (画像取得不能等)ケースに備え、それでも一定時間(GAMBLE_RESOLUTION_GRACE_PERIOD)経っても
# データが来なければ、やむを得ずその時点のデータだけで判定を確定する。
GAMBLE_RESOLUTION_GRACE_PERIOD = timedelta(hours=2)


def _resolve_pending_fog_bets():
    """window_endが過ぎた賭けの答え合わせをする。バッジ付与(_award_new_badges)と同じく、
    ゲーム関連ページを開いたタイミングで遅延実行する(専用のcronは持たない)。"""
    conn = _users_conn()
    try:
        now = now_jst_naive()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        pending = users.list_pending_fog_bets(conn, now_str)
        if not pending:
            return
        if not WEATHER_DB_PATH.exists():
            return
        main_df, phenom_df, _ = fog_data.load_river_fog_data()
        collected_datetimes = set(main_df["datetime"])
        for bet in pending:
            station = bet["station_name"]
            window_end_dt = datetime.strptime(bet["window_end"], "%Y-%m-%d %H:%M:%S")
            data_ready = window_end_dt in collected_datetimes
            if not data_ready and (now - window_end_dt) < GAMBLE_RESOLUTION_GRACE_PERIOD:
                # 対象時刻の収集がまだ終わっていない可能性が高いため、今回は判定を見送る
                # (猶予期間内は「まだ観測されていない」を「霧が出なかった」と混同しない)。
                continue
            if station not in phenom_df.columns:
                # 地点が無くなった等、答え合わせ不能。賭け金は返さずlostとして締める
                # (無期限pendingのまま残り続けるのを防ぐ)。
                users.resolve_fog_bet(conn, bet["id"], "lost", now_str, 0)
                continue
            window = main_df[
                (main_df["datetime"] >= bet["window_start"]) & (main_df["datetime"] <= bet["window_end"])
            ]
            merged = window.merge(phenom_df[["datetime", station]], on="datetime", how="inner")
            codes = merged[station].apply(encode_phenomena_cell)
            occurred = bool(codes.dropna().isin(SUMMARY_FOG_CODES).any())
            if occurred:
                payout = int(round(bet["bet_amount"] * bet["multiplier"]))
                users.resolve_fog_bet(conn, bet["id"], "won", now_str, payout)
                users.grant_points(conn, bet["user_id"], f"gamble_win:{bet['id']}", payout, now_str)
            else:
                refund = 0
                if bet.get("insurance"):
                    refund = int(round(bet["bet_amount"] * CONSUMABLE_INSURANCE_REFUND_RATIO))
                    users.grant_points(conn, bet["user_id"], f"gamble_insurance:{bet['id']}", refund, now_str)
                users.resolve_fog_bet(conn, bet["id"], "lost", now_str, refund)
    except Exception as e:
        log.warning("Failed to resolve pending fog bets: %s", e)
    finally:
        conn.close()


def _compute_badges(stats):
    """statsは{login_count, streak, comment_count, chat_count, favorite_count,
    station_total, account_age_days, has_avatar, bio_set, role, night_owl, early_bird,
    weekend_days, newyear_login, newyearseve_login, lifetime_points, owned_frame_count}。
    定義済みの実績すべてについて、達成済みかどうかを付けたリストを返す
    (未達成もロック表示するため、達成したものだけに絞り込まない)。"""
    return [
        {"id": b["id"], "label": b["label"], "desc": b["desc"], "icon": b["icon"],
         "points": b["points"], "achieved": b["check"](stats)}
        for b in BADGE_DEFINITIONS
    ]


def _award_new_badges(conn, user_id, badges):
    """達成済みなのにまだポイントを付与していない実績があれば、その分だけ付与する。
    users.award_badge()がUNIQUE制約で二重付与を防ぐため、member_detail()を見るたびに
    呼び出しても達成済みの実績に再度ポイントが入ることはない(新規達成分だけ加算される)。"""
    awarded_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    for b in badges:
        if b["achieved"]:
            users.award_badge(conn, user_id, b["id"], b["points"], awarded_at)


# 月間(先月分)ログインランキング上位3名への報酬。members_list()を開くたびに
# _award_monthly_ranking_rewards()から呼ばれるが、grant_points()のUNIQUE(user_id, source)
# 制約により実際の付与は月initial回だけ(以後は何もしない、バッジ付与と同じ遅延実行方式)。
MONTHLY_RANKING_REWARDS = [(1, 5000), (2, 2000), (3, 500)]


def _award_monthly_ranking_rewards(conn):
    """先月分の月間ログイン回数ランキング上位3名に、未付与ならポイントを付与する。"""
    today = datetime.now(JST).date()
    this_month_start = today.replace(day=1)
    last_month_end_day = this_month_start - timedelta(days=1)
    last_month_start = last_month_end_day.replace(day=1)
    since_str = last_month_start.strftime("%Y-%m-%d 00:00:00")
    until_str = this_month_start.strftime("%Y-%m-%d 00:00:00")

    counts = users.login_counts_between(conn, since_str, until_str)
    if not counts:
        return
    # 集計後にアカウントが削除されている場合(login_eventsは残るがusersからは消える)、
    # grant_pointsのFOREIGN KEY制約で例外になってしまうため、現存するユーザーだけに絞る。
    existing_ids = set(users.get_users_by_ids(conn, list(counts.keys())).keys())
    ranked = sorted(
        ((uid, c) for uid, c in counts.items() if uid in existing_ids),
        key=lambda kv: (-kv[1], kv[0]),
    )
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    month_label = last_month_start.strftime("%Y-%m")
    for rank, points in MONTHLY_RANKING_REWARDS:
        if rank > len(ranked):
            break
        user_id, count = ranked[rank - 1]
        if count <= 0:
            continue
        source = f"monthly_login_ranking_{month_label}_{rank}"
        if users.grant_points(conn, user_id, source, points, now_str):
            log.info(
                "Granted %d points to user %d for monthly login ranking #%d (%s, %d logins)",
                points, user_id, rank, month_label, count,
            )


@app.route("/members")
def members_list():
    """ログイン中の全ユーザーが見られるメンバー一覧。個々のログイン履歴・
    ヒートマップはメンバー詳細ページ(member_detail)で確認する。"""
    today = datetime.now(JST).date()
    month_start = today.replace(day=1).strftime("%Y-%m-%d 00:00:00")
    conn = _users_conn()
    try:
        _award_monthly_ranking_rewards(conn)
        all_users = users.list_users(conn)
        login_counts = users.login_counts_all(conn)
        month_login_counts = users.login_counts_since(conn, month_start)
        last_logins = users.last_logins_all(conn)
        streaks = users.login_streaks_all(conn, today)
        last_active = users.last_active_all(conn)
    finally:
        conn.close()
    members = [
        {
            "id": u["id"], "name": u["name"], "role": u["role"],
            "has_avatar": _avatar_path(u["id"]).exists(),
            "login_count": login_counts.get(u["id"], 0),
            "month_login_count": month_login_counts.get(u["id"], 0),
            "last_login": last_logins.get(u["id"]),
            "streak": streaks.get(u["id"], 0),
            "favorite_stations": [s for s in u["favorite_stations"].split(",") if s],
            "equipped_frame": u["equipped_frame"],
            "online": _is_online(last_active.get(u["id"])),
            "title_label": TITLE_BY_ID.get(u["equipped_title"], {}).get("label", ""),
            "name_color_class": NAME_COLOR_BY_ID.get(u["equipped_name_color"], {}).get("css_class", ""),
            "title_color_class": TITLE_COLOR_BY_ID.get(u["equipped_title_color"], {}).get("css_class", ""),
            "marker_icon": MARKER_BY_ID.get(u["equipped_marker"], {}).get("icon", ""),
            "title_badge_icon": TITLE_BADGE_BY_ID.get(u["equipped_title_badge"], {}).get("icon", ""),
            "rank_style_class": RANK_STYLE_BY_ID.get(u["equipped_rank_style"], {}).get("css_class", ""),
        }
        for u in all_users
    ]
    ranking = sorted(
        [m for m in members if m["login_count"] > 0], key=lambda m: -m["login_count"]
    )[:5]
    month_ranking = sorted(
        [m for m in members if m["month_login_count"] > 0], key=lambda m: -m["month_login_count"]
    )[:5]
    return render_template(
        "members_list.html", members=members, ranking=ranking, month_ranking=month_ranking,
    )


@app.route("/members/<int:user_id>")
def member_detail(user_id):
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        history = users.login_history(conn, user_id)
        counts_by_day = users.login_counts_by_day(conn, user_id)
        total_logins = users.login_count(conn, user_id)
        streak = users.login_streak(conn, user_id, datetime.now(JST).date())
        comment_count = users.comment_count(conn, user_id)
        lifetime_points = users.total_earned_points(conn, user_id)
        owned_frame_count = len(users.owned_frame_ids(conn, user_id))
        last_active_at = users.last_active(conn, user_id)
        gamble_won_count = users.won_fog_bet_count(conn, user_id)
        msg_count = users.messages_sent_count(conn, user_id)
        reaction_given_count = users.reactions_given_count(conn, user_id)
        reaction_received_count = users.reactions_received_count(conn, user_id)
        mention_received_count = users.mentions_received_count(conn, user_id)
        fog_quiz_stats_for_badges = users.get_daily_fog_quiz_stats(conn, user_id)
        phenom_quiz_correct = users.phenom_quiz_correct_count(conn, user_id)
        lottery_draws = users.lottery_draw_count(conn, user_id)
        file_upload_count = users.shared_file_upload_count(conn, user_id)
        file_star_count = users.shared_file_star_count(conn, user_id)
        title_complete = len(users.owned_cosmetic_ids(conn, user_id, "title")) >= len(TITLE_DEFINITIONS)
        name_color_complete = (
            len(users.owned_cosmetic_ids(conn, user_id, "name_color")) >= len(NAME_COLOR_DEFINITIONS)
        )
        points_spent = users.total_spent_points(conn, user_id)
        ranking_first_count = users.monthly_ranking_first_count(conn, user_id)
        station_share_count = users.station_share_count(conn, user_id)
    finally:
        conn.close()
    favorite_stations = [s for s in target["favorite_stations"].split(",") if s]
    chat_conn = chat_log.connect(CHAT_DB_PATH)
    try:
        chat_count = chat_log.count_user_messages_since(chat_conn, user_id, "0000-01-01 00:00:00")
    finally:
        chat_conn.close()
    try:
        created_at = datetime.strptime(target["created_at"][:19], "%Y-%m-%d %H:%M:%S")
        account_age_days = (now_jst_naive() - created_at).days
    except (ValueError, TypeError):
        account_age_days = 0
    has_avatar = _avatar_path(user_id).exists()
    night_owl = any(len(h) >= 13 and int(h[11:13]) < 4 for h in history)
    early_bird = any(len(h) >= 13 and 5 <= int(h[11:13]) < 7 for h in history)
    newyear_login = any(len(h) >= 10 and h[5:10] == "01-01" for h in history)
    newyearseve_login = any(len(h) >= 10 and h[5:10] == "12-31" for h in history)
    weekend_days = sum(
        1 for d in counts_by_day if datetime.strptime(d, "%Y-%m-%d").weekday() >= 5
    )
    badges = _compute_badges({
        "login_count": total_logins, "streak": streak, "comment_count": comment_count,
        "chat_count": chat_count, "favorite_count": len(favorite_stations),
        "station_total": len(STATION_NAMES), "account_age_days": account_age_days,
        "has_avatar": has_avatar, "bio_set": bool(target["bio"]), "role": target["role"],
        "night_owl": night_owl, "early_bird": early_bird, "weekend_days": weekend_days,
        "newyear_login": newyear_login, "newyearseve_login": newyearseve_login,
        "lifetime_points": lifetime_points, "owned_frame_count": owned_frame_count,
        "gamble_won_count": gamble_won_count, "msg_count": msg_count,
        "reaction_given_count": reaction_given_count, "reaction_received_count": reaction_received_count,
        "mention_received_count": mention_received_count,
        "fog_quiz_correct": fog_quiz_stats_for_badges["total_correct"],
        "phenom_quiz_correct": phenom_quiz_correct, "lottery_draws": lottery_draws,
        "file_upload_count": file_upload_count, "file_star_count": file_star_count,
        "title_complete": title_complete, "name_color_complete": name_color_complete,
        "points_spent": points_spent, "ranking_first_count": ranking_first_count,
        "station_share_count": station_share_count,
    })
    # 実績の達成状況を見る/見られるたびに、新規達成分のポイントを付与する
    # (award_badgeが既付与分を無視するので、何度呼んでも二重加算にはならない)。
    points_conn = _users_conn()
    try:
        _award_new_badges(points_conn, user_id, badges)
        points = users.available_points(points_conn, user_id)
    finally:
        points_conn.close()
    title_label = TITLE_BY_ID.get(target["equipped_title"], {}).get("label", "")
    name_color_class = NAME_COLOR_BY_ID.get(target["equipped_name_color"], {}).get("css_class", "")
    effect_class = EFFECT_BY_ID.get(target["equipped_effect"], {}).get("css_class", "")
    banner_class = BANNER_BY_ID.get(target["equipped_banner"], {}).get("css_class", "")
    title_color_class = TITLE_COLOR_BY_ID.get(target["equipped_title_color"], {}).get("css_class", "")
    marker_icon = MARKER_BY_ID.get(target["equipped_marker"], {}).get("icon", "")
    title_badge_icon = TITLE_BADGE_BY_ID.get(target["equipped_title_badge"], {}).get("icon", "")
    avatar_bg_class = AVATAR_BG_BY_ID.get(target["equipped_avatar_bg"], {}).get("css_class", "")
    seasonal_class = SEASONAL_BY_ID.get(target["equipped_seasonal"], {}).get("css_class", "")
    profile_sound = PROFILE_SOUND_BY_ID.get(target["equipped_profile_sound"], {}).get("sound", "")
    return render_template(
        "member_detail.html",
        member=target,
        title_label=title_label,
        name_color_class=name_color_class,
        effect_class=effect_class,
        banner_class=banner_class,
        title_color_class=title_color_class,
        marker_icon=marker_icon,
        title_badge_icon=title_badge_icon,
        avatar_bg_class=avatar_bg_class,
        seasonal_class=seasonal_class,
        profile_sound=profile_sound,
        history=history,
        counts_by_day=counts_by_day,
        total_logins=total_logins,
        streak=streak,
        favorite_stations=favorite_stations,
        station_names=STATION_NAMES,
        first_login=history[-1] if history else None,
        last_login=history[0] if history else None,
        has_avatar=has_avatar,
        is_self=(session.get("user_id") == user_id),
        heatmap_days=LOGIN_HEATMAP_DAYS,
        avatar_size=AVATAR_SIZE,
        badges=badges,
        points=points,
        online=_is_online(last_active_at),
        last_active_at=last_active_at,
    )


@app.route("/members/<int:user_id>/profile", methods=["POST"])
def member_profile_update(user_id):
    """自己紹介文とお気に入り地点は本人のみが変更できる(アバターと同じ制限)。"""
    if session.get("user_id") != user_id:
        abort(403)
    bio = request.form.get("bio", "").strip()
    favorite_stations = [s for s in request.form.getlist("favorite_stations") if s in STATION_NAMES]
    conn = _users_conn()
    try:
        users.update_profile(conn, user_id, bio=bio, favorite_stations=",".join(favorite_stations))
    finally:
        conn.close()
    return _finish("member_detail", "プロフィールを更新しました。", user_id=user_id)


@app.route("/game")
def game_home():
    """ポイント・ショップ・川霧ギャンブルをまとめたゲームのトップページ。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    _resolve_pending_fog_bets()
    conn = _users_conn()
    try:
        points = users.available_points(conn, my_id)
        recent_bets = users.list_fog_bets_for_user(conn, my_id, limit=5)
    finally:
        conn.close()
    return render_template("game_home.html", points=points, recent_bets=recent_bets)


@app.route("/game/shop")
def game_shop():
    """ポイントでアバターフレーム・称号・名前の色・ダッシュボード配色・演出・
    ギャンブル用消費アイテムと交換する画面。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    conn = _users_conn()
    try:
        member = users.get_user_by_id(conn, my_id)
        points = users.available_points(conn, my_id)
        owned = users.owned_frame_ids(conn, my_id)
        owned_titles = users.owned_cosmetic_ids(conn, my_id, "title")
        owned_name_colors = users.owned_cosmetic_ids(conn, my_id, "name_color")
        owned_dashboard_themes = users.owned_cosmetic_ids(conn, my_id, "dashboard_theme")
        owned_effects = users.owned_cosmetic_ids(conn, my_id, "effect")
        owned_banners = users.owned_cosmetic_ids(conn, my_id, "banner")
        owned_dm_themes = users.owned_cosmetic_ids(conn, my_id, "dm_theme")
        owned_title_colors = users.owned_cosmetic_ids(conn, my_id, "title_color")
        owned_markers = users.owned_cosmetic_ids(conn, my_id, "marker")
        owned_title_badges = users.owned_cosmetic_ids(conn, my_id, "title_badge")
        owned_bubble_styles = users.owned_cosmetic_ids(conn, my_id, "bubble_style")
        owned_avatar_bgs = users.owned_cosmetic_ids(conn, my_id, "avatar_bg")
        owned_rank_styles = users.owned_cosmetic_ids(conn, my_id, "rank_style")
        owned_profile_sounds = users.owned_cosmetic_ids(conn, my_id, "profile_sound")
        owned_seasonal = users.owned_cosmetic_ids(conn, my_id, "seasonal")
        consumables = users.consumable_quantities(conn, my_id)
    finally:
        conn.close()
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    new_cutoff = (now_jst_naive() - timedelta(days=SHOP_NEW_BADGE_DAYS)).strftime("%Y-%m-%d")

    def _is_new(item):
        return bool(item.get("added")) and item["added"] >= new_cutoff

    return render_template(
        "game_shop.html",
        member=member,
        points=points,
        owned=owned,
        frame_grades=FRAME_GRADES,
        frame_themes=FRAME_THEMES,
        frame_definitions=FRAME_DEFINITIONS,
        title_definitions=TITLE_DEFINITIONS,
        owned_titles=owned_titles,
        name_color_definitions=NAME_COLOR_DEFINITIONS,
        owned_name_colors=owned_name_colors,
        dashboard_theme_definitions=DASHBOARD_THEME_DEFINITIONS,
        owned_dashboard_themes=owned_dashboard_themes,
        effect_definitions=EFFECT_DEFINITIONS,
        owned_effects=owned_effects,
        banner_definitions=BANNER_DEFINITIONS,
        owned_banners=owned_banners,
        dm_theme_definitions=DM_THEME_DEFINITIONS,
        owned_dm_themes=owned_dm_themes,
        title_color_definitions=TITLE_COLOR_DEFINITIONS,
        owned_title_colors=owned_title_colors,
        marker_definitions=MARKER_DEFINITIONS,
        owned_markers=owned_markers,
        title_badge_definitions=TITLE_BADGE_DEFINITIONS,
        owned_title_badges=owned_title_badges,
        bubble_style_definitions=BUBBLE_STYLE_DEFINITIONS,
        owned_bubble_styles=owned_bubble_styles,
        avatar_bg_definitions=AVATAR_BG_DEFINITIONS,
        owned_avatar_bgs=owned_avatar_bgs,
        rank_style_definitions=RANK_STYLE_DEFINITIONS,
        owned_rank_styles=owned_rank_styles,
        profile_sound_definitions=PROFILE_SOUND_DEFINITIONS,
        owned_profile_sounds=owned_profile_sounds,
        seasonal_definitions=SEASONAL_DEFINITIONS,
        owned_seasonal=owned_seasonal,
        today_str=today_str,
        is_new=_is_new,
        gacha_cost=GACHA_COST,
        consumable_definitions=CONSUMABLE_DEFINITIONS,
        consumables=consumables,
    )


@app.route("/game/shop/buy", methods=["POST"])
def game_shop_buy():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    frame = FRAME_BY_ID.get(request.form.get("frame_id", ""))
    if frame is None:
        abort(404)
    conn = _users_conn()
    try:
        if frame["id"] in users.owned_frame_ids(conn, my_id):
            return _finish("game_shop", "そのフレームは既に持っています。", success=False)
        if users.available_points(conn, my_id) < frame["cost"]:
            return _finish("game_shop", "ポイントが足りません。", success=False)
        users.purchase_frame(
            conn, my_id, frame["id"], frame["cost"], datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        )
    finally:
        conn.close()
    return _finish("game_shop", f"「{frame['label']}」と交換しました。")


@app.route("/game/shop/equip", methods=["POST"])
def game_shop_equip():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    frame_id = request.form.get("frame_id", "").strip()
    conn = _users_conn()
    try:
        if frame_id and frame_id not in users.owned_frame_ids(conn, my_id):
            abort(400)
        users.set_equipped_frame(conn, my_id, frame_id)
    finally:
        conn.close()
    message = "フレームを装着しました。" if frame_id else "フレームを外しました。"
    return _finish("game_shop", message)


# category(URLの<category>) -> (カタログのdict, users.pyに渡すcategory名, 表示用ラベル)
COSMETIC_CATALOGS = {
    "title": (TITLE_BY_ID, "title", "称号"),
    "name_color": (NAME_COLOR_BY_ID, "name_color", "名前の色"),
    "dashboard_theme": (DASHBOARD_THEME_BY_ID, "dashboard_theme", "ダッシュボード配色"),
    "effect": (EFFECT_BY_ID, "effect", "演出"),
    "banner": (BANNER_BY_ID, "banner", "プロフィールバナー"),
    "dm_theme": (DM_THEME_BY_ID, "dm_theme", "メッセージ配色"),
    "title_color": (TITLE_COLOR_BY_ID, "title_color", "称号の色"),
    "marker": (MARKER_BY_ID, "marker", "殿堂入り章"),
    "title_badge": (TITLE_BADGE_BY_ID, "title_badge", "称号バッジ"),
    "bubble_style": (BUBBLE_STYLE_BY_ID, "bubble_style", "チャット吹き出し形状"),
    "avatar_bg": (AVATAR_BG_BY_ID, "avatar_bg", "アバター背景エフェクト"),
    "rank_style": (RANK_STYLE_BY_ID, "rank_style", "ランキング数字の書体"),
    "profile_sound": (PROFILE_SOUND_BY_ID, "profile_sound", "プロフィール効果音"),
    "seasonal": (SEASONAL_BY_ID, "seasonal", "季節限定コレクション"),
}


@app.route("/game/shop/buy/<category>", methods=["POST"])
def game_shop_buy_cosmetic(category):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    entry = COSMETIC_CATALOGS.get(category)
    if entry is None:
        abort(404)
    catalog, cosmetic_category, label = entry
    item = catalog.get(request.form.get("item_id", ""))
    if item is None:
        abort(404)
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    if item.get("available_from") and today_str < item["available_from"]:
        return _finish("game_shop", "まだ販売期間ではありません。", success=False)
    if item.get("available_until") and today_str > item["available_until"]:
        return _finish("game_shop", "販売期間が終了しました。", success=False)
    if item.get("admin_only") and session.get("role") != "admin":
        return _finish("game_shop", "この称号は管理者のみ入手できます。", success=False)
    conn = _users_conn()
    try:
        if item["id"] in users.owned_cosmetic_ids(conn, my_id, cosmetic_category):
            return _finish("game_shop", f"その{label}は既に持っています。", success=False)
        if users.available_points(conn, my_id) < item["cost"]:
            return _finish("game_shop", "ポイントが足りません。", success=False)
        users.purchase_cosmetic(
            conn, my_id, cosmetic_category, item["id"], item["cost"],
            datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        )
        points = users.available_points(conn, my_id)
    finally:
        conn.close()
    return _finish(
        "game_shop", f"「{item['label']}」と交換しました。",
        extra={"action": "buy", "category": category, "item_id": item["id"], "available_points": points},
    )


@app.route("/game/shop/equip/<category>", methods=["POST"])
def game_shop_equip_cosmetic(category):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    entry = COSMETIC_CATALOGS.get(category)
    if entry is None:
        abort(404)
    catalog, cosmetic_category, label = entry
    item_id = request.form.get("item_id", "").strip()
    conn = _users_conn()
    try:
        if item_id and item_id not in users.owned_cosmetic_ids(conn, my_id, cosmetic_category):
            abort(400)
        users.set_equipped_cosmetic(conn, my_id, cosmetic_category, item_id)
    finally:
        conn.close()
    message = f"{label}を装着しました。" if item_id else f"{label}を外しました。"
    return _finish(
        "game_shop", message,
        extra={"action": "equip" if item_id else "unequip", "category": category, "item_id": item_id},
    )


def _gacha_is_jackpot(weighted):
    """福袋の当たり判定。確率は常に1/GACHA_BIG_REFUND_WEIGHTに固定する(アイテム側の
    重みをそのまま分母に混ぜると、アイテムの価格帯によって当たり確率が事実上100%近くまで
    ぶれてしまうバグが過去にあったため、確率計算とアイテム抽選の重み計算を分離してある)。
    test_economy_sanity.pyがモンテカルロで確率を検証するため、判定だけを取り出した
    独立関数にしている(この関数の中身を変えるならそのテストも見直すこと)。"""
    return not weighted or random.random() < (1.0 / GACHA_BIG_REFUND_WEIGHT)


ECONOMY_ANOMALY_CHECK_INTERVAL_SECONDS = 1800
ECONOMY_ANOMALY_WINDOW_MINUTES = 60
ECONOMY_ANOMALY_POINT_THRESHOLD = 50000
_economy_anomaly_check_state = {"at": 0.0}
_economy_anomaly_check_lock = threading.Lock()


def _check_economy_anomalies():
    """直近ECONOMY_ANOMALY_WINDOW_MINUTES分でECONOMY_ANOMALY_POINT_THRESHOLDpt以上を
    獲得したユーザーがいないかを軽く見張る(福袋バグのような無限増殖系の不正を、誰かが
    気づく前に自動で検知するため)。よく踏まれるページから間引いて呼ぶだけの簡易版で、
    専用のバッチ処理は用意していない。検知したら操作ログとアラートWebhookの両方に残す
    (Webhook未設定でも操作ログには必ず残るようにしている)。"""
    now = time.monotonic()
    with _economy_anomaly_check_lock:
        if now - _economy_anomaly_check_state["at"] < ECONOMY_ANOMALY_CHECK_INTERVAL_SECONDS:
            return
        _economy_anomaly_check_state["at"] = now
    conn = _users_conn()
    try:
        cutoff = (now_jst_naive() - timedelta(minutes=ECONOMY_ANOMALY_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            "SELECT user_id, SUM(amount) FROM point_events WHERE created_at >= ? AND amount > 0 "
            "GROUP BY user_id HAVING SUM(amount) >= ?",
            (cutoff, ECONOMY_ANOMALY_POINT_THRESHOLD),
        ).fetchall()
    finally:
        conn.close()
    for user_id, total in rows:
        message = (
            f"user_id={user_id}が直近{ECONOMY_ANOMALY_WINDOW_MINUTES}分で{total}pt獲得しています"
            f"(閾値{ECONOMY_ANOMALY_POINT_THRESHOLD}pt)。ポイント無限増殖バグの疑いがあるため確認してください。"
        )
        _record_audit("経済異常検知", message)
        _send_alert_throttled(f"economy_anomaly_user_{user_id}", message)


@app.route("/game/shop/gacha", methods=["POST"])
def game_shop_gacha():
    """福袋: 固定ポイントで、未所持コスメの中からランダムに1つ交換するか、ポイントを
    払い戻す。GACHA_BIG_REFUND_WEIGHT分の1の確率で大きめの払い戻し、残りは安いアイテム
    ほど当たりやすい重み付き抽選(値段の逆数を重みにする)。全部所持済みなら参加賞として
    少額ポイントを返す。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        if users.available_points(conn, my_id) < GACHA_COST:
            return _finish("game_shop", "ポイントが足りません。", success=False)
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        candidates = []
        for category, (catalog, cosmetic_category, label) in COSMETIC_CATALOGS.items():
            owned = users.owned_cosmetic_ids(conn, my_id, cosmetic_category)
            today_str = now_str[:10]
            for item in catalog.values():
                if item["id"] in owned:
                    continue
                if item.get("available_from") and today_str < item["available_from"]:
                    continue
                if item.get("available_until") and today_str > item["available_until"]:
                    continue
                candidates.append((category, cosmetic_category, label, item))
        weighted = [(c, cc, lb, it, 1.0 / max(it["cost"], 1)) for c, cc, lb, it in candidates]
        if _gacha_is_jackpot(weighted):
            # 参加費(GACHA_COST)はconsumable_purchases経由で記録し(total_spent_pointsの
            # 集計対象)、当たり分だけを別途grant_pointsで加算する(二重計上にならない)。
            users.buy_consumable(conn, my_id, "gacha_draw", GACHA_COST, now_str)
            if weighted:
                refund = int(round(GACHA_COST * GACHA_BIG_REFUND_MULTIPLIER))
                users.grant_points(conn, my_id, f"gacha_refund:{uuid.uuid4()}", refund, now_str)
                message = f"福袋の結果: {refund}pt の大当たり!"
            else:
                users.grant_points(conn, my_id, f"gacha_consolation:{uuid.uuid4()}", GACHA_CONSOLATION_POINTS, now_str)
                message = f"すべて所持済みだったため、参加賞として{GACHA_CONSOLATION_POINTS}ptを獲得しました。"
        else:
            roll = random.random() * sum(w for *_, w in weighted)
            chosen = weighted[-1]
            for entry in weighted:
                roll -= entry[4]
                if roll <= 0:
                    chosen = entry
                    break
            _, cosmetic_category, label, item, _ = chosen
            users.buy_consumable(conn, my_id, "gacha_draw", GACHA_COST, now_str)
            users.purchase_cosmetic(conn, my_id, cosmetic_category, item["id"], 0, now_str)
            message = f"福袋の結果: 「{item['label']}」({label})を獲得しました!"
        points = users.available_points(conn, my_id)
    finally:
        conn.close()
    return _finish("game_shop", message, extra={"available_points": points})


@app.route("/game/shop/buy_consumable", methods=["POST"])
def game_shop_buy_consumable():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    item = CONSUMABLE_BY_ID.get(request.form.get("item_id", ""))
    if item is None:
        abort(404)
    conn = _users_conn()
    try:
        if users.available_points(conn, my_id) < item["cost"]:
            return _finish("game_shop", "ポイントが足りません。", success=False)
        users.buy_consumable(
            conn, my_id, item["id"], item["cost"], datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        )
        points = users.available_points(conn, my_id)
        quantity = users.consumable_quantities(conn, my_id).get(item["id"], 0)
    finally:
        conn.close()
    return _finish(
        "game_shop", f"「{item['label']}」を購入しました。",
        extra={"action": "buy_consumable", "item_id": item["id"], "available_points": points, "quantity": quantity},
    )


@app.route("/game/gamble")
def game_gamble():
    """川霧ギャンブル: 地点・日付・時間精度を選び、倍率のプレビューを見ながら賭ける画面。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    _resolve_pending_fog_bets()
    conn = _users_conn()
    try:
        points = users.available_points(conn, my_id)
        bets = users.list_fog_bets_for_user(conn, my_id)
        consumables = users.consumable_quantities(conn, my_id)
    finally:
        conn.close()
    fog_rates = _station_fog_rates()
    station_rows = sorted(
        (
            {
                "name": name,
                "fog_rate": fog_rates.get(name),
                "rarity_multiplier": _gamble_multiplier(fog_rates.get(name), 0, "day"),
            }
            for name in STATION_NAMES
        ),
        key=lambda r: -r["rarity_multiplier"],
    )
    return render_template(
        "game_gamble.html",
        points=points, bets=bets, station_rows=station_rows,
        station_names=STATION_NAMES, precisions=GAMBLE_PRECISIONS, blocks=GAMBLE_BLOCKS,
        min_bet=GAMBLE_MIN_BET,
        today=now_jst_naive().strftime("%Y-%m-%d"),
        consumables=consumables, consumable_definitions=CONSUMABLE_DEFINITIONS,
        consumable_boost_multiplier=CONSUMABLE_BOOST_MULTIPLIER,
        consumable_super_boost_multiplier=CONSUMABLE_SUPER_BOOST_MULTIPLIER,
        now_str=now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"),
    )


@app.route("/game/gamble/preview")
def game_gamble_preview():
    """賭けフォームが入力を変えるたびに叩く、倍率だけを返す軽量プレビューAPI。
    入力が不正/不完全な場合は倍率算出をせずmultiplier: nullを返す(フォーム側でエラー表示は
    別途行うため、ここではエラーメッセージまでは作らない)。"""
    station = request.args.get("station", "")
    target_date = request.args.get("target_date", "")
    precision_id = request.args.get("precision", "")
    hour = request.args.get("hour", type=int)
    block_index = request.args.get("block_index", type=int)

    if station not in STATION_NAMES or precision_id not in GAMBLE_PRECISION_BY_ID or not DATE_RE.match(target_date or ""):
        return jsonify({"multiplier": None})
    window = _gamble_window_for_bet(target_date, precision_id, hour=hour, block_index=block_index)
    if window is None:
        return jsonify({"multiplier": None})
    window_start, _ = window
    try:
        lead_hours = (
            datetime.strptime(window_start, "%Y-%m-%d %H:%M:%S") - now_jst_naive()
        ).total_seconds() / 3600.0
    except ValueError:
        return jsonify({"multiplier": None})
    if lead_hours < 0 or lead_hours > GAMBLE_MAX_LEAD_HOURS:
        return jsonify({"multiplier": None})
    fog_rate = _station_fog_rates().get(station)
    multiplier = _gamble_multiplier(fog_rate, lead_hours, precision_id)
    return jsonify({"multiplier": multiplier})


@app.route("/game/gamble/place", methods=["POST"])
def game_gamble_place():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    station = request.form.get("station", "")
    target_date = request.form.get("target_date", "")
    precision_id = request.form.get("precision", "")
    hour = request.form.get("hour", type=int)
    block_index = request.form.get("block_index", type=int)
    bet_amount = request.form.get("bet_amount", type=int)
    use_insurance = request.form.get("use_insurance") == "1"
    use_boost = request.form.get("use_boost") == "1"
    use_super_boost = request.form.get("use_super_boost") == "1"

    if station not in STATION_NAMES:
        return _finish("game_gamble", "地点を選んでください。", success=False)
    if precision_id not in GAMBLE_PRECISION_BY_ID:
        return _finish("game_gamble", "時間精度を選んでください。", success=False)
    if not DATE_RE.match(target_date or ""):
        return _finish("game_gamble", "日付が正しくありません。", success=False)
    if bet_amount is None or bet_amount < GAMBLE_MIN_BET:
        return _finish(
            "game_gamble", f"賭けポイントは{GAMBLE_MIN_BET}pt以上で指定してください。",
            success=False,
        )

    window = _gamble_window_for_bet(target_date, precision_id, hour=hour, block_index=block_index)
    if window is None:
        return _finish("game_gamble", "指定した時刻が観測時間帯(4-19時)の範囲外です。", success=False)
    window_start, window_end = window

    now = now_jst_naive()
    lead_hours = (datetime.strptime(window_start, "%Y-%m-%d %H:%M:%S") - now).total_seconds() / 3600.0
    if lead_hours < 0:
        return _finish("game_gamble", "過去の時刻には賭けられません。", success=False)
    if lead_hours > GAMBLE_MAX_LEAD_HOURS:
        return _finish("game_gamble", "1週間より先の時刻には賭けられません。", success=False)

    fog_rate = _station_fog_rates().get(station)
    multiplier = _gamble_multiplier(fog_rate, lead_hours, precision_id)

    conn = _users_conn()
    try:
        if users.available_points(conn, my_id) < bet_amount:
            return _finish("game_gamble", "ポイントが足りません。", success=False)
        owned = users.consumable_quantities(conn, my_id)
        if use_insurance and owned.get("insurance_ticket", 0) < 1:
            return _finish("game_gamble", "保険チケットを持っていません。", success=False)
        if use_boost and owned.get("boost_ticket", 0) < 1:
            return _finish("game_gamble", "倍率ブーストチケットを持っていません。", success=False)
        if use_super_boost and owned.get("super_boost_ticket", 0) < 1:
            return _finish("game_gamble", "上級倍率ブーストチケットを持っていません。", success=False)

        # 上級版と通常版が同時に指定された場合は上級版だけを使う(効果は重複させない)。
        if use_super_boost:
            users.use_consumable(conn, my_id, "super_boost_ticket")
            multiplier = round(multiplier * CONSUMABLE_SUPER_BOOST_MULTIPLIER, 2)
        elif use_boost:
            users.use_consumable(conn, my_id, "boost_ticket")
            multiplier = round(multiplier * CONSUMABLE_BOOST_MULTIPLIER, 2)
        if use_insurance:
            users.use_consumable(conn, my_id, "insurance_ticket")

        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        users.place_fog_bet(
            conn, my_id, station, precision_id, window_start, window_end, bet_amount, multiplier, now_str,
            insurance=use_insurance,
        )
    finally:
        conn.close()
    return _finish(
        "game_gamble",
        f"{station}に{bet_amount}pt賭けました(倍率{multiplier}倍)。観測が終わり次第、答え合わせされます。",
    )


@app.route("/game/gamble/cancel/<int:bet_id>", methods=["POST"])
def game_gamble_cancel(bet_id):
    """見送り券を使い、観測時間帯が始まる前ならpending中の賭けを全額払い戻して取り消す。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        bet = users.get_fog_bet(conn, bet_id)
        if bet is None or bet["user_id"] != my_id:
            abort(404)
        if bet["status"] != "pending":
            return _finish("game_gamble", "この賭けは既に答え合わせ済みです。", success=False)
        window_start_dt = datetime.strptime(bet["window_start"], "%Y-%m-%d %H:%M:%S")
        if now_jst_naive() >= window_start_dt:
            return _finish("game_gamble", "観測時間帯が始まっているため取り消せません。", success=False)
        if not users.use_consumable(conn, my_id, "withdraw_ticket"):
            return _finish("game_gamble", "見送り券を持っていません。", success=False)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        users.cancel_fog_bet(conn, bet_id, now_str, 0)
        users.grant_points(conn, my_id, f"gamble_withdraw:{bet_id}", 0, now_str)
    finally:
        conn.close()
    return _finish("game_gamble", "賭けを取り消し、全額払い戻しました。")


@app.route("/game/daily")
def game_daily():
    """無料のデイリーゲーム(霧予報クイズ・現象当てクイズ・くじ)をまとめたページ。
    川霧ギャンブルと違いポイントを賭けないため、ポイントが無い新規ユーザーでも遊べる。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    _resolve_pending_daily_fog_quiz()
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    fog_quiz_station = _daily_fog_quiz_station(today_str)
    phenom_item = _todays_phenom_quiz_item(today_str)
    phenom_image_url = None
    if phenom_item:
        date_dir, hhmm, station, _code = phenom_item
        phenom_image_url = url_for("images_file", date=date_dir, hhmm=hhmm, filename=f"{station}.jpg")

    conn = _users_conn()
    try:
        points = users.available_points(conn, my_id)
        fog_quiz_today = users.get_daily_fog_quiz(conn, my_id, today_str)
        fog_quiz_stats = users.get_daily_fog_quiz_stats(conn, my_id)
        phenom_quiz_today = users.get_daily_phenom_quiz(conn, my_id, today_str)
        lottery_result = users.get_point_event_amount(conn, my_id, f"daily_lottery:{today_str}")
        consumables = users.consumable_quantities(conn, my_id)
    finally:
        conn.close()
    return render_template(
        "game_daily.html",
        points=points,
        fog_quiz_station=fog_quiz_station, fog_quiz_today=fog_quiz_today, fog_quiz_stats=fog_quiz_stats,
        phenom_item=phenom_item, phenom_image_url=phenom_image_url, phenom_quiz_today=phenom_quiz_today,
        phenom_categories=PHENOM_QUIZ_CATEGORIES, phenom_quiz_points=PHENOM_QUIZ_POINTS,
        lottery_result=lottery_result, lottery_outcomes=LOTTERY_OUTCOMES,
        fog_quiz_base_points=FOG_QUIZ_BASE_POINTS, fog_quiz_streak_bonus=FOG_QUIZ_STREAK_BONUS_PER_DAY,
        consumables=consumables,
    )


@app.route("/game/daily/fog_quiz/guess", methods=["POST"])
def game_daily_fog_quiz_guess():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    guess = request.form.get("guess")
    if guess not in ("yes", "no"):
        return _finish("game_daily", "予想を選んでください。", success=False)
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    station = _daily_fog_quiz_station(today_str)
    conn = _users_conn()
    try:
        created = users.place_daily_fog_quiz_guess(
            conn, my_id, today_str, station, guess == "yes",
            now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"),
        )
    finally:
        conn.close()
    if not created:
        return _finish("game_daily", "今日はもう回答済みです。", success=False)
    return _finish("game_daily", f"「{station}」の予想を受け付けました。観測が終わり次第、答え合わせされます。")


@app.route("/game/daily/phenom_quiz/hint", methods=["POST"])
def game_daily_phenom_quiz_hint():
    """クイズヒントチケットを1個消費し、誤った選択肢を1つランダムに選んで返す
    (どれが正解かは明かさない)。フォーム送信ではなくボタンクリックからfetchで
    直接呼ぶため、_finish()は使わずJSONを返す。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    item = _todays_phenom_quiz_item(today_str)
    if item is None:
        return jsonify({"success": False, "message": "今日は出題できる画像がありません。"})
    _date_dir, _hhmm, _station, code = item
    correct_category = _phenom_category_id_for_code(code)
    conn = _users_conn()
    try:
        if users.get_daily_phenom_quiz(conn, my_id, today_str) is not None:
            return jsonify({"success": False, "message": "今日はもう回答済みです。"})
        if not users.use_consumable(conn, my_id, "quiz_hint_ticket"):
            return jsonify({"success": False, "message": "クイズヒントチケットを持っていません。"})
        wrong_ids = [c["id"] for c in PHENOM_QUIZ_CATEGORIES if c["id"] != correct_category]
        eliminate = random.choice(wrong_ids)
    finally:
        conn.close()
    return jsonify({"success": True, "eliminate": eliminate})


@app.route("/game/daily/phenom_quiz/answer", methods=["POST"])
def game_daily_phenom_quiz_answer():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    item = _todays_phenom_quiz_item(today_str)
    if item is None:
        return _finish("game_daily", "今日は出題できる画像がありません。", success=False)
    _date_dir, _hhmm, _station, code = item
    category_guess = request.form.get("category", "")
    if category_guess not in {c["id"] for c in PHENOM_QUIZ_CATEGORIES}:
        return _finish("game_daily", "選択肢を選んでください。", success=False)
    correct_category = _phenom_category_id_for_code(code)
    correct = category_guess == correct_category
    now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    conn = _users_conn()
    try:
        created = users.place_daily_phenom_quiz_answer(conn, my_id, today_str, category_guess, correct, now_str)
        if created and correct:
            users.grant_points(conn, my_id, f"phenom_quiz:{today_str}", PHENOM_QUIZ_POINTS, now_str)
    finally:
        conn.close()
    if not created:
        return _finish("game_daily", "今日はもう回答済みです。", success=False)
    correct_label = next((c["label"] for c in PHENOM_QUIZ_CATEGORIES if c["id"] == correct_category), "不明")
    if correct:
        return _finish("game_daily", f"正解！「{correct_label}」でした。(+{PHENOM_QUIZ_POINTS}pt)")
    return _finish("game_daily", f"不正解。正解は「{correct_label}」でした。")


@app.route("/game/daily/lottery/draw", methods=["POST"])
def game_daily_lottery_draw():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    source = f"daily_lottery:{today_str}"
    use_luck = request.form.get("use_luck") == "1"
    conn = _users_conn()
    try:
        existing = users.get_point_event_amount(conn, my_id, source)
        if existing is not None:
            return _finish("game_daily", f"今日はもう引いています(結果: {existing}pt)。", success=False)
        used_luck = False
        if use_luck:
            if users.consumable_quantities(conn, my_id).get("lottery_luck_ticket", 0) < 1:
                return _finish("game_daily", "ラッキーチケットを持っていません。", success=False)
            used_luck = users.use_consumable(conn, my_id, "lottery_luck_ticket")
        weights = [
            w * LOTTERY_LUCK_WEIGHT_MULTIPLIER if used_luck and a >= 100 else w
            for w, a in LOTTERY_OUTCOMES
        ]
        amount = random.choices([a for _, a in LOTTERY_OUTCOMES], weights=weights, k=1)[0]
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        if not users.grant_points(conn, my_id, source, amount, now_str):
            # 直前にほぼ同時の二重送信があった場合、実際に記録された方の金額を表示する
            amount = users.get_point_event_amount(conn, my_id, source)
    finally:
        conn.close()
    return _finish("game_daily", f"デイリーくじ: {amount}pt獲得！")


# --- メッセージ機能共通(DM・グループチャット両方で使う) ------------------------------
REACTION_EMOJIS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]
MESSAGE_SEARCH_LIMIT = 50

# 入力中表示: 専用テーブルは持たず、_png_cache等と同じ「モジュールレベルのdict+Lock」で
# 一時的な状態として保持する(プロセス再起動で消えても実害が無いため永続化不要)。
# キーは(channel_key, user_id)。channel_keyはDMなら"dm:<conversation_id>"、
# グループチャットなら固定文字列"group"。
_typing_state = {}
_typing_state_lock = threading.Lock()
TYPING_INDICATOR_TTL_SECONDS = 6


def _mark_typing(channel_key, user_id):
    with _typing_state_lock:
        _typing_state[(channel_key, user_id)] = time.monotonic()


def _typing_user_ids(channel_key, exclude_user_id=None):
    now = time.monotonic()
    with _typing_state_lock:
        stale = [k for k, t in _typing_state.items() if now - t > TYPING_INDICATOR_TTL_SECONDS]
        for k in stale:
            del _typing_state[k]
        return [uid for (key, uid) in _typing_state if key == channel_key and uid != exclude_user_id]


_MENTION_TOKEN_RE = re.compile(r"@([^\s@]+)")


def _parse_mentions(body, name_to_id):
    """本文中の`@表示名`を、既存ユーザー名(name_to_id: {name: user_id})と照合する。
    表示名に空白は使えない前提のため、@の後ろの非空白トークンに対し、長い名前から
    順に前方一致を試す(短い名前が長い名前の一部になっているケースの誤爆を避けるため)。"""
    matched = set()
    names_by_length = sorted(name_to_id.keys(), key=len, reverse=True)
    for m in _MENTION_TOKEN_RE.finditer(body):
        token = m.group(1)
        for name in names_by_length:
            if token.startswith(name):
                matched.add(name_to_id[name])
                break
    return matched


def _reaction_summary(reactions, my_id):
    """[{"emoji":, "user_id":}, ...] を [{"emoji":, "count":, "mine": bool}, ...] に集計する
    (絵文字が最初に現れた順を保つ)。"""
    counts = {}
    order = []
    mine = set()
    for r in reactions:
        emoji = r["emoji"]
        if emoji not in counts:
            counts[emoji] = 0
            order.append(emoji)
        counts[emoji] += 1
        if r["user_id"] == my_id:
            mine.add(emoji)
    return [{"emoji": e, "count": counts[e], "mine": e in mine} for e in order]


def _reply_preview_dict(reply_msg, name_by_id):
    """返信先メッセージの要約を、送信直後のJSONレスポンスやポーリング応答にも
    その場で載せられる形にする(ページ再読み込みを待たずに表示するため)。"""
    if reply_msg is None:
        return None
    body = reply_msg["body"]
    snippet = body[:60] + ("…" if len(body) > 60 else "")
    return {
        "id": reply_msg["id"],
        "sender_name": name_by_id.get(reply_msg["sender_id"], "(退会済みユーザー)"),
        "snippet": snippet,
    }


def _attach_message_extras(messages, reactions_by_id, mentions_by_id, name_by_id, reply_by_id, my_id):
    """メッセージ辞書のリストに、リアクション集計・メンション表示名・返信先の要約を
    その場で追加する(DM・グループ両方の描画で共通に使う)。"""
    for m in messages:
        m["reactions"] = _reaction_summary(reactions_by_id.get(m["id"], []), my_id)
        mention_ids = mentions_by_id.get(m["id"], [])
        m["mentioned_names"] = [name_by_id[uid] for uid in mention_ids if uid in name_by_id]
        try:
            m["card"] = json.loads(m["card_data"]) if m.get("card_data") else None
        except (TypeError, ValueError):
            m["card"] = None
        reply = reply_by_id.get(m.get("reply_to_id"))
        if reply:
            snippet = reply["body"][:60] + ("…" if len(reply["body"]) > 60 else "")
            m["reply_preview"] = {
                "id": reply["id"],
                "sender_name": name_by_id.get(reply["sender_id"], "(退会済みユーザー)"),
                "snippet": snippet,
            }
        else:
            m["reply_preview"] = None
    return messages


@app.route("/group")
def group_home():
    """全員参加のグループチャット。参加者管理は無く、全登録ユーザーが常にメンバー。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    conn = _users_conn()
    try:
        messages = users.list_group_messages(conn)
        message_ids = [m["id"] for m in messages]
        attachments_by_id = users.get_group_attachments_for_messages(conn, message_ids)
        reactions_by_id = users.get_group_reactions_for_messages(conn, message_ids)
        mentions_by_id = users.get_group_mentions_for_messages(conn, message_ids)
        all_users = users.list_users(conn)
        pinned = users.list_pinned_group_messages(conn)
        if messages:
            users.mark_group_read(conn, my_id, messages[-1]["id"])
        read_states = users.all_group_read_states(conn)
        me = users.get_user_by_id(conn, my_id)
    finally:
        conn.close()
    name_by_id = {u["id"]: u["name"] for u in all_users}
    reply_by_id = {m["id"]: m for m in messages}
    for m in messages:
        m["attachments"] = attachments_by_id.get(m["id"], [])
        m["sender_name"] = name_by_id.get(m["sender_id"], "(退会済みユーザー)")
        m["read_count"] = sum(
            1 for uid, last_read in read_states.items() if uid != m["sender_id"] and last_read >= m["id"]
        )
    _attach_message_extras(messages, reactions_by_id, mentions_by_id, name_by_id, reply_by_id, my_id)
    for p in pinned:
        p["sender_name"] = name_by_id.get(p["sender_id"], "(退会済みユーザー)")
    my_dm_theme_class = DM_THEME_BY_ID.get(me["equipped_dm_theme"], {}).get("css_class", "") if me else ""
    my_bubble_style_class = BUBBLE_STYLE_BY_ID.get(me["equipped_bubble_style"], {}).get("css_class", "") if me else ""
    return render_template(
        "group_chat.html", messages=messages, pinned=pinned, my_id=my_id,
        dm_message_max_length=DM_MESSAGE_MAX_LENGTH, reaction_emojis=REACTION_EMOJIS,
        member_names=sorted(name_by_id.values()), my_dm_theme_class=my_dm_theme_class,
        my_bubble_style_class=my_bubble_style_class,
    )


def _serializable_read_states(read_states):
    """dictのキーがintのままだとjsonify後にJS側でstringキーになるため、
    グループチャットの既読数リアルタイム更新ではキーを文字列化して明示的に渡す。"""
    return {str(uid): last_read for uid, last_read in read_states.items()}


@app.route("/group/send", methods=["POST"])
def group_send():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    body = request.form.get("body", "").strip()[:DM_MESSAGE_MAX_LENGTH]
    reply_to_id = request.form.get("reply_to_id", type=int)
    file = request.files.get("attachment")
    has_file = bool(file and file.filename)
    if not body and not has_file:
        return _finish("group_home", "メッセージまたは添付ファイルを指定してください。", success=False)
    attachment_info = None
    if has_file:
        result, error = _save_uploaded_file(file, DM_ATTACHMENT_DIR)
        if error:
            return _finish("group_home", error, success=False)
        attachment_info = result
    created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    conn = _users_conn()
    try:
        if reply_to_id is not None and users.get_group_message(conn, reply_to_id) is None:
            reply_to_id = None
        message_id = users.send_group_message(conn, my_id, body, created_at, reply_to_id=reply_to_id)
        attachment_id = None
        if attachment_info:
            original_name, stored_name, content_type, size = attachment_info
            attachment_id = users.add_group_attachment(
                conn, message_id, original_name, stored_name, content_type, size, created_at,
            )
        all_users = users.list_users(conn)
        name_to_id = {u["name"]: u["id"] for u in all_users if u["id"] != my_id}
        mention_ids = _parse_mentions(body, name_to_id)
        users.record_group_mentions(conn, message_id, mention_ids)
        users.mark_group_read(conn, my_id, message_id)
        reply_preview = None
        if reply_to_id is not None:
            reply_name_by_id = {u["id"]: u["name"] for u in all_users}
            reply_preview = _reply_preview_dict(users.get_group_message(conn, reply_to_id), reply_name_by_id)
    finally:
        conn.close()
    with _typing_state_lock:
        _typing_state.pop(("group", my_id), None)
    if _wants_json():
        return jsonify({
            "message": "送信しました。", "success": True,
            "id": message_id, "body": body, "created_at": created_at,
            "attachment": (
                {"id": attachment_id, "filename": attachment_info[0]} if attachment_info else None
            ),
            "reply_preview": reply_preview,
        })
    return redirect(url_for("group_home"))


@app.route("/group/poll")
def group_poll():
    """dm_pollと同じ4秒ポーリング方式。新着メッセージに加え、入力中の相手・既読状況も返す。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    after_id = request.args.get("after", 0, type=int)
    conn = _users_conn()
    try:
        messages = users.list_group_messages(conn, after_id)
        message_ids = [m["id"] for m in messages]
        attachments_by_id = users.get_group_attachments_for_messages(conn, message_ids)
        reactions_by_id = users.get_group_reactions_for_messages(conn, message_ids)
        mentions_by_id = users.get_group_mentions_for_messages(conn, message_ids)
        all_users = users.list_users(conn)
        if messages:
            users.mark_group_read(conn, my_id, messages[-1]["id"])
        read_states = users.all_group_read_states(conn)
        reply_ids = {m["reply_to_id"] for m in messages if m.get("reply_to_id")}
        reply_msgs_by_id = {rid: users.get_group_message(conn, rid) for rid in reply_ids}
    finally:
        conn.close()
    name_by_id = {u["id"]: u["name"] for u in all_users}
    for m in messages:
        m["attachments"] = attachments_by_id.get(m["id"], [])
        m["sender_name"] = name_by_id.get(m["sender_id"], "(退会済みユーザー)")
        m["reactions"] = _reaction_summary(reactions_by_id.get(m["id"], []), my_id)
        mention_ids = mentions_by_id.get(m["id"], [])
        m["mentioned_names"] = [name_by_id[uid] for uid in mention_ids if uid in name_by_id]
        try:
            m["card"] = json.loads(m["card_data"]) if m.get("card_data") else None
        except (TypeError, ValueError):
            m["card"] = None
        m["read_count"] = sum(
            1 for uid, last_read in read_states.items() if uid != m["sender_id"] and last_read >= m["id"]
        )
        m["reply_preview"] = _reply_preview_dict(reply_msgs_by_id.get(m.get("reply_to_id")), name_by_id)
    typing_names = [
        name_by_id[uid] for uid in _typing_user_ids("group", exclude_user_id=my_id) if uid in name_by_id
    ]
    return jsonify({
        "messages": messages, "typing_names": typing_names,
        "read_states": _serializable_read_states(read_states),
    })


@app.route("/group/typing", methods=["POST"])
def group_typing():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    _mark_typing("group", my_id)
    return jsonify({"success": True})


@app.route("/group/search")
def group_search():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"results": []})
    conn = _users_conn()
    try:
        matches = users.search_group_messages(conn, keyword, limit=MESSAGE_SEARCH_LIMIT)
        name_by_id = {u["id"]: u["name"] for u in users.list_users(conn)}
    finally:
        conn.close()
    for m in matches:
        m["sender_name"] = name_by_id.get(m["sender_id"], "(退会済みユーザー)")
    return jsonify({"results": matches})


@app.route("/group/attachments/<int:attachment_id>")
def group_attachment_file(attachment_id):
    if not session.get("user_id"):
        abort(403)
    conn = _users_conn()
    try:
        att = users.get_group_attachment(conn, attachment_id)
    finally:
        conn.close()
    if att is None:
        abort(404)
    return send_from_directory(
        DM_ATTACHMENT_DIR, att["stored_name"], as_attachment=True, download_name=att["filename"],
    )


@app.route("/group/<int:message_id>/delete", methods=["POST"])
def group_message_delete(message_id):
    """自分が送信したメッセージ、または管理者は誰のメッセージでも削除できる
    (荒らし・誤爆投稿を全員が見えるチャンネルから消せるようにするため)。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        msg = users.get_group_message(conn, message_id)
        if msg is None:
            abort(404)
        if msg["sender_id"] != my_id and session.get("role") != "admin":
            return jsonify({"success": False, "message": "自分が送信したメッセージのみ削除できます。"}), 403
        stored_names = users.get_group_message_attachment_stored_names(conn, message_id)
        users.delete_group_message(conn, message_id)
    finally:
        conn.close()
    for stored_name in stored_names:
        (DM_ATTACHMENT_DIR / stored_name).unlink(missing_ok=True)
    return jsonify({"success": True, "message_id": message_id})


@app.route("/group/<int:message_id>/edit", methods=["POST"])
def group_message_edit(message_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    body = request.form.get("body", "").strip()[:DM_MESSAGE_MAX_LENGTH]
    if not body:
        return jsonify({"success": False, "message": "本文を入力してください。"}), 400
    conn = _users_conn()
    try:
        msg = users.get_group_message(conn, message_id)
        if msg is None:
            abort(404)
        if msg["sender_id"] != my_id:
            return jsonify({"success": False, "message": "自分が送信したメッセージのみ編集できます。"}), 403
        edited_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        users.edit_group_message(conn, message_id, body, edited_at)
    finally:
        conn.close()
    return jsonify({"success": True, "message_id": message_id, "body": body, "edited_at": edited_at})


@app.route("/group/<int:message_id>/pin", methods=["POST"])
def group_message_pin(message_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    pinned = request.form.get("pinned") == "1"
    conn = _users_conn()
    try:
        msg = users.get_group_message(conn, message_id)
        if msg is None:
            abort(404)
        if msg["sender_id"] != my_id and session.get("role") != "admin":
            return jsonify({"success": False, "message": "投稿者本人か管理者のみピン留めできます。"}), 403
        users.pin_group_message(conn, message_id, pinned)
    finally:
        conn.close()
    return jsonify({"success": True, "message_id": message_id, "pinned": pinned})


@app.route("/group/<int:message_id>/react", methods=["POST"])
def group_message_react(message_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    emoji = request.form.get("emoji", "")
    if emoji not in REACTION_EMOJIS:
        abort(400)
    conn = _users_conn()
    try:
        if users.get_group_message(conn, message_id) is None:
            abort(404)
        existing = users.get_group_reactions_for_messages(conn, [message_id]).get(message_id, [])
        already = any(r["user_id"] == my_id and r["emoji"] == emoji for r in existing)
        if already:
            users.remove_group_reaction(conn, message_id, my_id, emoji)
        else:
            users.add_group_reaction(
                conn, message_id, my_id, emoji, now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"),
            )
        reactions = users.get_group_reactions_for_messages(conn, [message_id]).get(message_id, [])
    finally:
        conn.close()
    return jsonify({"success": True, "reactions": _reaction_summary(reactions, my_id)})


@app.route("/messages")
def dm_inbox():
    """自分が参加している会話(直近やり取りが新しい順、未読件数付き)を先頭に、
    まだ会話していない他の全メンバーもそのまま同じ並び方で続ける(わざわざ相手の
    プロフィールページを経由しなくても、ここから新しい会話を始められるように
    するため)。話したことがある/ないで見た目やセクションを分けたりはしない。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    conn = _users_conn()
    try:
        conversations = users.list_dm_conversations(conn, my_id)
        unread = users.dm_unread_counts(conn, my_id)
        pinned_ids = users.pinned_conversation_ids(conn, my_id)
        others = users.get_users_by_ids(conn, [c["other_id"] for c in conversations])
        all_users = users.list_users(conn)
        last_active = users.last_active_all(conn)
    finally:
        conn.close()
    entries = [
        {
            "other": others.get(c["other_id"]),
            "conversation_id": c["id"],
            "last_message_at": c["last_message_at"],
            "unread": unread.get(c["id"], 0),
            "pinned": c["id"] in pinned_ids,
            "online": _is_online(last_active.get(c["other_id"])),
        }
        for c in conversations
        if c["other_id"] in others  # 退会済みの相手は一覧に出さない
    ]
    started_ids = {c["other_id"] for c in conversations}
    entries += [
        {
            "other": u, "conversation_id": None, "last_message_at": "", "unread": 0, "pinned": False,
            "online": _is_online(last_active.get(u["id"])),
        }
        for u in all_users
        if u["id"] != my_id and u["id"] not in started_ids
    ]
    return render_template("dm_inbox.html", entries=entries)


@app.route("/messages/<int:user_id>/mark_unread", methods=["POST"])
def dm_mark_unread(user_id):
    """受信箱のクイック操作「未読にする」。まだ会話が無い相手には無意味なので、
    会話が存在する場合のみ処理する。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        a, b = sorted((my_id, user_id))
        row = conn.execute(
            "SELECT id FROM dm_conversations WHERE user_a_id = ? AND user_b_id = ?", (a, b),
        ).fetchone()
        if row is not None:
            users.mark_dm_unread(conn, row[0], my_id)
    finally:
        conn.close()
    return jsonify({"success": True})


@app.route("/messages/<int:user_id>/pin_conversation", methods=["POST"])
def dm_pin_conversation(user_id):
    """受信箱のクイック操作「ピン留め」。会話がまだ無い相手はピン留めできない
    (ピン留めは会話単位の概念のため)。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        a, b = sorted((my_id, user_id))
        row = conn.execute(
            "SELECT id FROM dm_conversations WHERE user_a_id = ? AND user_b_id = ?", (a, b),
        ).fetchone()
        if row is None:
            return jsonify({"success": False, "message": "まだ会話がありません。"}), 400
        pinned = users.toggle_pinned_conversation(conn, my_id, row[0])
    finally:
        conn.close()
    return jsonify({"success": True, "pinned": pinned})


@app.route("/messages/<int:user_id>")
def dm_thread(user_id):
    """user_idとの1対1会話を開く(存在しなければ最初のメッセージ送信時に作成されるので、
    ここでは会話が無い＝メッセージ0件として空の状態を表示するだけでよい)。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    if user_id == my_id:
        abort(404)
    conn = _users_conn()
    try:
        other = users.get_user_by_id(conn, user_id)
        if other is None:
            abort(404)
        a, b = sorted((my_id, user_id))
        row = conn.execute(
            "SELECT id FROM dm_conversations WHERE user_a_id = ? AND user_b_id = ?", (a, b),
        ).fetchone()
        messages = []
        pinned = []
        other_read = 0
        if row is not None:
            conversation_id = row[0]
            messages = users.list_dm_messages(conn, conversation_id)
            message_ids = [m["id"] for m in messages]
            attachments_by_message = users.get_dm_attachments_for_messages(conn, message_ids)
            reactions_by_id = users.get_dm_reactions_for_messages(conn, message_ids)
            mentions_by_id = users.get_dm_mentions_for_messages(conn, message_ids)
            pinned = users.list_pinned_dm_messages(conn, conversation_id)
            name_by_id = {my_id: session.get("name", ""), user_id: other["name"]}
            reply_by_id = {m["id"]: m for m in messages}
            for m in messages:
                m["attachments"] = attachments_by_message.get(m["id"], [])
                m["sender_name"] = name_by_id.get(m["sender_id"], "")
            _attach_message_extras(messages, reactions_by_id, mentions_by_id, name_by_id, reply_by_id, my_id)
            for p in pinned:
                p["sender_name"] = name_by_id.get(p["sender_id"], "")
            if messages:
                users.mark_dm_read(conn, conversation_id, my_id, messages[-1]["id"])
            other_read = users.get_dm_read_state(conn, conversation_id, user_id)
        else:
            conversation_id = None
        other_online = _is_online(users.last_active(conn, user_id))
        me = users.get_user_by_id(conn, my_id)
    finally:
        conn.close()
    my_dm_theme_class = DM_THEME_BY_ID.get(me["equipped_dm_theme"], {}).get("css_class", "") if me else ""
    other_dm_theme_class = DM_THEME_BY_ID.get(other["equipped_dm_theme"], {}).get("css_class", "")
    my_bubble_style_class = BUBBLE_STYLE_BY_ID.get(me["equipped_bubble_style"], {}).get("css_class", "") if me else ""
    other_bubble_style_class = BUBBLE_STYLE_BY_ID.get(other["equipped_bubble_style"], {}).get("css_class", "")
    return render_template(
        "dm_thread.html", other=other, messages=messages, conversation_id=conversation_id,
        my_id=my_id, dm_message_max_length=DM_MESSAGE_MAX_LENGTH, other_online=other_online,
        my_dm_theme_class=my_dm_theme_class, other_dm_theme_class=other_dm_theme_class,
        my_bubble_style_class=my_bubble_style_class, other_bubble_style_class=other_bubble_style_class,
        pinned=pinned, other_read=other_read, reaction_emojis=REACTION_EMOJIS,
    )


@app.route("/messages/<int:user_id>/send", methods=["POST"])
def dm_send(user_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    if user_id == my_id:
        abort(404)
    body = request.form.get("body", "").strip()[:DM_MESSAGE_MAX_LENGTH]
    reply_to_id = request.form.get("reply_to_id", type=int)
    file = request.files.get("attachment")
    has_file = bool(file and file.filename)
    if not body and not has_file:
        return _finish(
            "dm_thread", "メッセージまたは添付ファイルを指定してください。", success=False, user_id=user_id,
        )
    attachment_info = None
    if has_file:
        result, error = _save_uploaded_file(file, DM_ATTACHMENT_DIR)
        if error:
            return _finish("dm_thread", error, success=False, user_id=user_id)
        attachment_info = result
    conn = _users_conn()
    try:
        other = users.get_user_by_id(conn, user_id)
        if other is None:
            abort(404)
        created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        conversation_id = users.get_or_create_dm_conversation(conn, my_id, user_id, created_at)
        if reply_to_id is not None:
            reply_msg = users.get_dm_message(conn, reply_to_id)
            if reply_msg is None or reply_msg["conversation_id"] != conversation_id:
                reply_msg = None
                reply_to_id = None
        else:
            reply_msg = None
        message_id = users.send_dm_message(conn, conversation_id, my_id, body, created_at, reply_to_id=reply_to_id)
        attachment_id = None
        if attachment_info:
            original_name, stored_name, content_type, size = attachment_info
            attachment_id = users.add_dm_attachment(
                conn, message_id, original_name, stored_name, content_type, size, created_at,
            )
        name_to_id = {other["name"]: user_id}
        mention_ids = _parse_mentions(body, name_to_id)
        users.record_dm_mentions(conn, message_id, mention_ids)
        users.mark_dm_read(conn, conversation_id, my_id, message_id)
        reply_preview = _reply_preview_dict(
            reply_msg, {my_id: session.get("name", ""), user_id: other["name"]},
        )
    finally:
        conn.close()
    with _typing_state_lock:
        _typing_state.pop((f"dm:{conversation_id}", my_id), None)
    if _wants_json():
        return jsonify({
            "message": "送信しました。", "success": True,
            "id": message_id, "body": body, "created_at": created_at,
            "attachment": (
                {"id": attachment_id, "filename": attachment_info[0]} if attachment_info else None
            ),
            "reply_preview": reply_preview,
        })
    return redirect(url_for("dm_thread", user_id=user_id))


@app.route("/messages/<int:user_id>/poll")
def dm_poll(user_id):
    """開いているスレッドで新着メッセージをJSで取りに行くためのエンドポイント
    (このアプリには汎用のリアルタイム配信基盤が無いため、スレッド表示中だけ
    数秒おきにポーリングする軽量な方式にしている)。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    after_id = request.args.get("after", 0, type=int)
    conn = _users_conn()
    try:
        other = users.get_user_by_id(conn, user_id)
        a, b = sorted((my_id, user_id))
        row = conn.execute(
            "SELECT id FROM dm_conversations WHERE user_a_id = ? AND user_b_id = ?", (a, b),
        ).fetchone()
        messages = []
        other_read = 0
        conversation_id = row[0] if row is not None else None
        if row is not None:
            messages = users.list_dm_messages(conn, conversation_id, after_id)
            message_ids = [m["id"] for m in messages]
            attachments_by_message = users.get_dm_attachments_for_messages(conn, message_ids)
            reactions_by_id = users.get_dm_reactions_for_messages(conn, message_ids)
            mentions_by_id = users.get_dm_mentions_for_messages(conn, message_ids)
            name_by_id = {my_id: session.get("name", ""), user_id: other["name"] if other else ""}
            reply_ids = {m["reply_to_id"] for m in messages if m.get("reply_to_id")}
            reply_msgs_by_id = {rid: users.get_dm_message(conn, rid) for rid in reply_ids}
            for m in messages:
                m["attachments"] = attachments_by_message.get(m["id"], [])
                m["sender_name"] = name_by_id.get(m["sender_id"], "")
                m["reactions"] = _reaction_summary(reactions_by_id.get(m["id"], []), my_id)
                mention_ids = mentions_by_id.get(m["id"], [])
                m["mentioned_names"] = [name_by_id[uid] for uid in mention_ids if uid in name_by_id]
                try:
                    m["card"] = json.loads(m["card_data"]) if m.get("card_data") else None
                except (TypeError, ValueError):
                    m["card"] = None
                m["reply_preview"] = _reply_preview_dict(reply_msgs_by_id.get(m.get("reply_to_id")), name_by_id)
            if messages:
                users.mark_dm_read(conn, conversation_id, my_id, messages[-1]["id"])
            other_read = users.get_dm_read_state(conn, conversation_id, user_id)
        other_online = _is_online(users.last_active(conn, user_id))
    finally:
        conn.close()
    typing_names = []
    if conversation_id is not None and other is not None:
        typing_ids = _typing_user_ids(f"dm:{conversation_id}", exclude_user_id=my_id)
        if user_id in typing_ids:
            typing_names = [other["name"]]
    return jsonify({
        "messages": messages, "other_online": other_online, "other_read": other_read,
        "typing_names": typing_names,
    })


@app.route("/messages/attachments/<int:attachment_id>")
def dm_attachment_file(attachment_id):
    """DM添付ファイルのダウンロード。その会話の当事者(送信者・受信者どちらか)だけが
    取得できる(会話に無関係な人がidを推測してもファイルを見られないようにするため)。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        att = users.get_dm_attachment(conn, attachment_id)
    finally:
        conn.close()
    if att is None:
        abort(404)
    if my_id not in (att["user_a_id"], att["user_b_id"]):
        abort(403)
    return send_from_directory(
        DM_ATTACHMENT_DIR, att["stored_name"], as_attachment=True, download_name=att["filename"],
    )


@app.route("/messages/<int:user_id>/<int:message_id>/delete", methods=["POST"])
def dm_message_delete(user_id, message_id):
    """自分が送信したメッセージを削除する(相手のメッセージは消せない。右クリック
    メニューからJS経由で叩かれる想定なので、成功/失敗をJSONで返すだけでよい)。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        msg = users.get_dm_message(conn, message_id)
        if msg is None or my_id not in (msg["user_a_id"], msg["user_b_id"]):
            abort(404)
        if msg["sender_id"] != my_id:
            return jsonify({"success": False, "message": "自分が送信したメッセージのみ削除できます。"}), 403
        stored_names = users.get_dm_message_attachment_stored_names(conn, message_id)
        users.delete_dm_message(conn, message_id)
    finally:
        conn.close()
    for stored_name in stored_names:
        (DM_ATTACHMENT_DIR / stored_name).unlink(missing_ok=True)
    return jsonify({"success": True, "message_id": message_id})


@app.route("/messages/<int:user_id>/<int:message_id>/edit", methods=["POST"])
def dm_message_edit(user_id, message_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    body = request.form.get("body", "").strip()[:DM_MESSAGE_MAX_LENGTH]
    if not body:
        return jsonify({"success": False, "message": "本文を入力してください。"}), 400
    conn = _users_conn()
    try:
        msg = users.get_dm_message(conn, message_id)
        if msg is None or my_id not in (msg["user_a_id"], msg["user_b_id"]):
            abort(404)
        if msg["sender_id"] != my_id:
            return jsonify({"success": False, "message": "自分が送信したメッセージのみ編集できます。"}), 403
        edited_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        users.edit_dm_message(conn, message_id, body, edited_at)
    finally:
        conn.close()
    return jsonify({"success": True, "message_id": message_id, "body": body, "edited_at": edited_at})


@app.route("/messages/<int:user_id>/<int:message_id>/pin", methods=["POST"])
def dm_message_pin(user_id, message_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    pinned = request.form.get("pinned") == "1"
    conn = _users_conn()
    try:
        msg = users.get_dm_message(conn, message_id)
        if msg is None or my_id not in (msg["user_a_id"], msg["user_b_id"]):
            abort(404)
        users.pin_dm_message(conn, message_id, pinned)
    finally:
        conn.close()
    return jsonify({"success": True, "message_id": message_id, "pinned": pinned})


@app.route("/messages/<int:user_id>/<int:message_id>/react", methods=["POST"])
def dm_message_react(user_id, message_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    emoji = request.form.get("emoji", "")
    if emoji not in REACTION_EMOJIS:
        abort(400)
    conn = _users_conn()
    try:
        msg = users.get_dm_message(conn, message_id)
        if msg is None or my_id not in (msg["user_a_id"], msg["user_b_id"]):
            abort(404)
        existing = users.get_dm_reactions_for_messages(conn, [message_id]).get(message_id, [])
        already = any(r["user_id"] == my_id and r["emoji"] == emoji for r in existing)
        if already:
            users.remove_dm_reaction(conn, message_id, my_id, emoji)
        else:
            users.add_dm_reaction(conn, message_id, my_id, emoji, now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"))
        reactions = users.get_dm_reactions_for_messages(conn, [message_id]).get(message_id, [])
    finally:
        conn.close()
    return jsonify({"success": True, "reactions": _reaction_summary(reactions, my_id)})


@app.route("/messages/<int:user_id>/typing", methods=["POST"])
def dm_typing(user_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    a, b = sorted((my_id, user_id))
    conn = _users_conn()
    try:
        row = conn.execute(
            "SELECT id FROM dm_conversations WHERE user_a_id = ? AND user_b_id = ?", (a, b),
        ).fetchone()
        if row is None:
            created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
            conversation_id = users.get_or_create_dm_conversation(conn, my_id, user_id, created_at)
        else:
            conversation_id = row[0]
    finally:
        conn.close()
    _mark_typing(f"dm:{conversation_id}", my_id)
    return jsonify({"success": True})


@app.route("/messages/<int:user_id>/search")
def dm_search(user_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"results": []})
    a, b = sorted((my_id, user_id))
    conn = _users_conn()
    try:
        row = conn.execute(
            "SELECT id FROM dm_conversations WHERE user_a_id = ? AND user_b_id = ?", (a, b),
        ).fetchone()
        if row is None:
            return jsonify({"results": []})
        matches = users.search_dm_messages(conn, row[0], keyword, limit=MESSAGE_SEARCH_LIMIT)
    finally:
        conn.close()
    return jsonify({"results": matches})


_PREVIEWABLE_XLSX_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_PREVIEW_MAX_ROWS = 200
_PREVIEW_MAX_COLS = 30


@app.route("/files")
def shared_files_page():
    """班の共有ファイル置き場(誰でもアップロード・ダウンロードできる、Teamsの「ファイル」
    タブに相当)。ログイン済みなら誰でも見られる。?folder=<id>でフォルダの中を表示する
    (省略時は最上位)。?q=キーワードを指定すると、フォルダをまたいで全文検索する
    (ファイル名・説明・自動タグ・テキスト系ファイルの中身が対象)。?starred=1で
    お気に入りのみに絞り込める(検索と併用可能)。"""
    if not session.get("user_id"):
        return redirect(url_for("login", next=request.path))
    my_id = session["user_id"]
    folder_id = request.args.get("folder", type=int)
    query = request.args.get("q", "").strip()
    starred_only = request.args.get("starred") == "1"
    conn = _users_conn()
    try:
        starred_ids = users.starred_file_ids(conn, my_id)
        if query:
            folders = []
            files = users.search_shared_files(conn, query)
            breadcrumbs = []
        else:
            if folder_id is not None and users.get_shared_folder(conn, folder_id) is None:
                abort(404)
            folders = users.list_shared_folders(conn, folder_id)
            files = users.list_shared_files(conn, folder_id)
            breadcrumbs = users.shared_folder_breadcrumbs(conn, folder_id) if folder_id else []
        if starred_only:
            files = [f for f in files if f["id"] in starred_ids]
        creators = users.get_users_by_ids(
            conn, [f["uploaded_by"] for f in files] + [fo["created_by"] for fo in folders]
        )
    finally:
        conn.close()
    for f in files:
        f["uploader_name"] = creators.get(f["uploaded_by"], {}).get("name", "(退会済みユーザー)")
        f["previewable"] = (
            f["content_type"].startswith("image/") or f["content_type"] == "application/pdf"
            or f["content_type"] in _PREVIEWABLE_XLSX_CONTENT_TYPES
        )
        f["tag_list"] = [t for t in f["tags"].split(",") if t]
        f["starred"] = f["id"] in starred_ids
    for fo in folders:
        fo["created_by_name"] = creators.get(fo["created_by"], {}).get("name", "(退会済みユーザー)")
    return render_template(
        "shared_files.html", files=files, folders=folders, breadcrumbs=breadcrumbs,
        current_folder_id=folder_id, query=query, starred_only=starred_only,
    )


_TEXT_INDEXABLE_EXTENSIONS = {
    ".txt": "text/plain", ".log": "text/plain", ".csv": "text/csv",
    ".md": "text/markdown", ".json": "application/json",
}
CONTENT_TEXT_MAX_BYTES = 300 * 1024
_DATE_TAG_RE = re.compile(r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})")


def _auto_tags_for_filename(filename):
    """アップロードされたファイル名から、地点名(STATION_NAMESとの部分一致)・日付らしき
    文字列を自動で拾い、"地点:丹過橋,日付:2026-09-03"のようなタグ文字列を組み立てる。
    捏造は避け、あくまでファイル名自体に既に含まれている情報を拾うだけにとどめる。"""
    tags = [f"地点:{name}" for name in STATION_NAMES if name and name in filename]
    m = _DATE_TAG_RE.search(filename)
    if m:
        tags.append(f"日付:{m.group(1)}-{m.group(2)}-{m.group(3)}")
    return ",".join(tags)


def _extract_content_text(path, ext):
    """検索(全文検索)用に、テキスト系ファイルの中身を読み込む。対象拡張子以外は
    空文字を返す(PDF/画像/xlsx等の中身までは読まない。新規の重い依存ライブラリを
    増やさない方針のため)。"""
    if ext not in _TEXT_INDEXABLE_EXTENSIONS:
        return ""
    try:
        with open(path, "rb") as f:
            raw = f.read(CONTENT_TEXT_MAX_BYTES)
        return raw.decode("utf-8", errors="ignore")
    except OSError:
        return ""


@app.route("/files/upload", methods=["POST"])
def shared_file_upload():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    folder_id = request.form.get("folder_id", type=int)
    file = request.files.get("file")
    if not file or not file.filename:
        return _finish("shared_files_page", "ファイルを選択してください。", success=False, folder=folder_id)
    result, error = _save_uploaded_file(file, SHARED_FILES_DIR)
    if error:
        return _finish("shared_files_page", error, success=False, folder=folder_id)
    original_name, stored_name, content_type, size = result
    description = request.form.get("description", "").strip()[:200]
    tags = _auto_tags_for_filename(original_name)
    content_text = _extract_content_text(SHARED_FILES_DIR / stored_name, Path(original_name).suffix.lower())
    conn = _users_conn()
    try:
        if folder_id is not None and users.get_shared_folder(conn, folder_id) is None:
            abort(404)
        users.add_shared_file(
            conn, original_name, stored_name, content_type, size, my_id,
            now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"), description, folder_id,
            tags=tags, content_text=content_text,
        )
    finally:
        conn.close()
    return _finish("shared_files_page", "アップロードしました。", folder=folder_id)


@app.route("/files/folders", methods=["POST"])
def shared_folder_create():
    """右クリックメニューの「新しいフォルダ」から呼ばれる。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    parent_id = request.form.get("parent_id", type=int)
    name = request.form.get("name", "").strip()[:100]
    if not name:
        return _finish("shared_files_page", "フォルダ名を入力してください。", success=False, folder=parent_id)
    conn = _users_conn()
    try:
        if parent_id is not None and users.get_shared_folder(conn, parent_id) is None:
            abort(404)
        users.create_shared_folder(conn, name, parent_id, my_id, now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        conn.close()
    return _finish("shared_files_page", "フォルダを作成しました。", folder=parent_id)


@app.route("/files/folders/<int:folder_id>/delete", methods=["POST"])
def shared_folder_delete(folder_id):
    """空のフォルダのみ削除できる(中身を巻き添えで消さないため)。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        folder = users.get_shared_folder(conn, folder_id)
        if folder is None:
            abort(404)
        if folder["created_by"] != my_id and session.get("role") != "admin":
            abort(403)
        if not users.shared_folder_is_empty(conn, folder_id):
            return _finish(
                "shared_files_page", "空でないフォルダは削除できません。",
                success=False, folder=folder["parent_id"],
            )
        users.delete_shared_folder(conn, folder_id)
        parent_id = folder["parent_id"]
    finally:
        conn.close()
    return _finish("shared_files_page", "フォルダを削除しました。", folder=parent_id)


@app.route("/files/<int:file_id>")
def shared_file_download(file_id):
    if not session.get("user_id"):
        abort(403)
    conn = _users_conn()
    try:
        f = users.get_shared_file(conn, file_id)
    finally:
        conn.close()
    if f is None:
        abort(404)
    return send_from_directory(
        SHARED_FILES_DIR, f["stored_name"], as_attachment=True, download_name=f["filename"],
    )


@app.route("/files/<int:file_id>/raw")
def shared_file_raw(file_id):
    """プレビュー画面の<img>/<embed>から読み込ませる、添付ヘッダーを付けない生配信。"""
    if not session.get("user_id"):
        abort(403)
    conn = _users_conn()
    try:
        f = users.get_shared_file(conn, file_id)
    finally:
        conn.close()
    if f is None:
        abort(404)
    return send_from_directory(SHARED_FILES_DIR, f["stored_name"], mimetype=f["content_type"])


@app.route("/files/<int:file_id>/preview")
def shared_file_preview(file_id):
    """画像・PDF・xlsxをブラウザ上でその場に見られるプレビュー画面。
    xlsxはopenpyxlで読み込み、先頭シートを表としてサーバー側でHTML化する
    (行・列数が多いシートは重くなるので上限で打ち切る)。"""
    if not session.get("user_id"):
        abort(403)
    conn = _users_conn()
    try:
        f = users.get_shared_file(conn, file_id)
    finally:
        conn.close()
    if f is None:
        abort(404)

    kind = None
    xlsx_rows = None
    xlsx_truncated = False
    if f["content_type"].startswith("image/"):
        kind = "image"
    elif f["content_type"] == "application/pdf":
        kind = "pdf"
    elif f["content_type"] in _PREVIEWABLE_XLSX_CONTENT_TYPES:
        kind = "xlsx"
        import openpyxl
        try:
            wb = openpyxl.load_workbook(
                SHARED_FILES_DIR / f["stored_name"], data_only=True, read_only=True,
            )
            ws = wb.active
            xlsx_rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= _PREVIEW_MAX_ROWS:
                    xlsx_truncated = True
                    break
                xlsx_rows.append(row[:_PREVIEW_MAX_COLS])
            if ws.max_column and ws.max_column > _PREVIEW_MAX_COLS:
                xlsx_truncated = True
        except Exception as e:
            log.warning("xlsx preview failed for shared_files id=%d: %s", file_id, e)
            kind = None
    return render_template(
        "shared_file_preview.html", f=f, kind=kind, xlsx_rows=xlsx_rows, xlsx_truncated=xlsx_truncated,
    )


@app.route("/files/<int:file_id>/delete", methods=["POST"])
def shared_file_delete(file_id):
    """アップロード本人か管理者だけが削除できる。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        f = users.get_shared_file(conn, file_id)
        if f is None:
            abort(404)
        if f["uploaded_by"] != my_id and session.get("role") != "admin":
            abort(403)
        users.delete_shared_file(conn, file_id)
        folder_id = f["folder_id"]
    finally:
        conn.close()
    stored_path = SHARED_FILES_DIR / f["stored_name"]
    stored_path.unlink(missing_ok=True)
    return _finish("shared_files_page", "削除しました。", folder=folder_id)


@app.route("/files/<int:file_id>/star", methods=["POST"])
def shared_file_star(file_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        f = users.get_shared_file(conn, file_id)
        if f is None:
            abort(404)
        starred = users.toggle_file_star(conn, my_id, file_id, now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        conn.close()
    if _wants_json():
        return jsonify({"success": True, "starred": starred})
    return _finish("shared_files_page", "お気に入りに追加しました。" if starred else "お気に入りを解除しました。")


def _bulk_file_ids_from_form():
    ids = request.form.getlist("file_ids", type=int)
    return sorted(set(i for i in ids if i))


@app.route("/files/bulk/move", methods=["POST"])
def shared_file_bulk_move():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    file_ids = _bulk_file_ids_from_form()
    target_folder_id = request.form.get("target_folder_id", type=int)
    if not file_ids:
        return jsonify({"success": False, "message": "移動するファイルを選択してください。"}), 400
    conn = _users_conn()
    try:
        if target_folder_id is not None and users.get_shared_folder(conn, target_folder_id) is None:
            return jsonify({"success": False, "message": "移動先フォルダが見つかりません。"}), 404
        moved = 0
        for file_id in file_ids:
            f = users.get_shared_file(conn, file_id)
            if f is None:
                continue
            if f["uploaded_by"] != my_id and session.get("role") != "admin":
                continue
            users.move_shared_file(conn, file_id, target_folder_id)
            moved += 1
    finally:
        conn.close()
    skipped = len(file_ids) - moved
    message = f"{moved}件移動しました。" + (f"({skipped}件は権限がなくスキップしました)" if skipped else "")
    return jsonify({"success": True, "message": message})


@app.route("/files/bulk/delete", methods=["POST"])
def shared_file_bulk_delete():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    file_ids = _bulk_file_ids_from_form()
    if not file_ids:
        return jsonify({"success": False, "message": "削除するファイルを選択してください。"}), 400
    conn = _users_conn()
    try:
        deleted = 0
        stored_paths = []
        for file_id in file_ids:
            f = users.get_shared_file(conn, file_id)
            if f is None:
                continue
            if f["uploaded_by"] != my_id and session.get("role") != "admin":
                continue
            users.delete_shared_file(conn, file_id)
            stored_paths.append(SHARED_FILES_DIR / f["stored_name"])
            deleted += 1
    finally:
        conn.close()
    for p in stored_paths:
        p.unlink(missing_ok=True)
    skipped = len(file_ids) - deleted
    message = f"{deleted}件削除しました。" + (f"({skipped}件は権限がなくスキップしました)" if skipped else "")
    return jsonify({"success": True, "message": message})


@app.route("/files/bulk/download")
def shared_file_bulk_download():
    """複数選択したファイルをまとめてZIPでダウンロードする。build_data_backup_zip
    (バックアップ機能)と同じzipfile標準ライブラリを使う。"""
    if not session.get("user_id"):
        abort(403)
    file_ids = sorted(set(request.args.getlist("file_ids", type=int)))
    if not file_ids:
        abort(400)
    conn = _users_conn()
    try:
        files = [users.get_shared_file(conn, fid) for fid in file_ids]
    finally:
        conn.close()
    files = [f for f in files if f is not None]
    if not files:
        abort(404)
    buf = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            path = SHARED_FILES_DIR / f["stored_name"]
            if not path.exists():
                continue
            arcname = f["filename"]
            # 同名ファイルが複数選択された場合、ZIP内で上書きされないように連番を振る。
            n = 1
            base_arcname = arcname
            while arcname in used_names:
                stem, dot, ext = base_arcname.rpartition(".")
                arcname = f"{stem or base_arcname}_{n}{dot}{ext}" if dot else f"{base_arcname}_{n}"
                n += 1
            used_names.add(arcname)
            zf.write(path, arcname=arcname)
    buf.seek(0)
    return Response(
        buf.getvalue(), mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=shared_files.zip"},
    )


@app.route("/members/<int:user_id>/avatar", methods=["POST"])
def member_avatar_upload(user_id):
    """プロフィール画像は本人のみが設定できる(他人になりすませないようにするため)。
    member_detail.htmlのクロップUIが既に正方形に切り出した画像をfetch()で送ってくるが、
    JS無効時やクロップUIを経由しない直接POSTにも備えて、ここでも中央正方形切り出し+
    リサイズをかける(既に正方形ならほぼ無処理で通る)。
    """
    if session.get("user_id") != user_id:
        abort(403)
    file = request.files.get("avatar")
    if not file or not file.filename:
        return _finish("member_detail", "画像ファイルを選択してください。", success=False, user_id=user_id)
    raw = file.read(AVATAR_MAX_UPLOAD_BYTES + 1)
    if len(raw) > AVATAR_MAX_UPLOAD_BYTES:
        return _finish(
            "member_detail",
            f"画像ファイルが大きすぎます({AVATAR_MAX_UPLOAD_BYTES // (1024 * 1024)}MB以下にしてください)。",
            success=False, user_id=user_id,
        )
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        w, h = img.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        img = img.crop((left, top, left + side, top + side)).resize(
            (AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS
        )
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        img.save(_avatar_path(user_id), format="JPEG", quality=88)
    except Exception as e:
        log.warning("Avatar upload failed for user %d: %s", user_id, e)
        return _finish(
            "member_detail", "画像を処理できませんでした。JPEGまたはPNG形式の画像を選んでください。",
            success=False, user_id=user_id,
        )
    return _finish("member_detail", "プロフィール画像を更新しました。", user_id=user_id)


@app.route("/members/<int:user_id>/avatar.jpg")
def member_avatar_file(user_id):
    path = _avatar_path(user_id)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="image/jpeg")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        if not name or not email or not password:
            error = "全ての項目を入力してください。"
        elif "@" not in email:
            error = "メールアドレスの形式が正しくありません。"
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f"パスワードは{MIN_PASSWORD_LENGTH}文字以上にしてください。"
        else:
            conn = _users_conn()
            try:
                if users.email_taken(conn, email):
                    error = "このメールアドレスは既に登録済み、または申請中です。"
                else:
                    requested_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
                    users.create_signup_request(
                        conn, name, email, users.generate_password_hash(password), requested_at,
                    )
            finally:
                conn.close()
            if not error:
                return render_template("signup.html", submitted=True)

    return render_template("signup.html", error=error, submitted=False)


# 管理画面を開かなくてもディスク逼迫・頻繁な再起動等に気付けるよう、ALERT_WEBHOOK_URLが
# 設定されていればプッシュ通知する。_admin_overview()は短いTTLキャッシュ越しとはいえ
# 頻繁に呼ばれるため、同じ内容を送り続けないよう種類ごとにクールダウンを設ける。
_ALERT_COOLDOWN_SEC = 3600
_last_alert_sent_at = {}
_last_alert_lock = threading.Lock()


def _send_alert_throttled(key, message):
    now = time.monotonic()
    with _last_alert_lock:
        last = _last_alert_sent_at.get(key, 0.0)
        if now - last < _ALERT_COOLDOWN_SEC:
            return
        _last_alert_sent_at[key] = now
    send_alert(message, source="webapp")


def _admin_overview():
    """管理者トップページ用の要約(サービス状態・ディスク・保留申請・DNS障害・最終学習日時)。
    個々の詳細ページ(サービス状態/ディスク使用量等)のロジックは変えず、表示用に軽くまとめるだけ。
    取得できない項目はNoneのままにし、テンプレート側で「-」表示にする。
    """
    web_state = _systemctl_show("river-fog-web.service", ["ActiveState"]).get("ActiveState")
    collect_timer_state = _systemctl_show("river-fog-collect.timer", ["ActiveState"]).get("ActiveState")
    try:
        total, used, _free = shutil.disk_usage(BASE_DIR)
        disk_used_pct = round(100.0 * used / total, 1) if total else None
    except Exception:
        disk_used_pct = None
    if disk_used_pct is not None and disk_used_pct >= 85:
        _send_alert_throttled("disk", f"ディスク使用率が{disk_used_pct}%です")
    if collect_timer_state != "active":
        _send_alert_throttled("collect_timer", "毎時収集タイマーが停止しています")
    network_failures = _network_failure_summary()
    model_state = fog_model.load_model_state()
    last_trained_at = None
    if model_state:
        trained_ats = [info.get("trained_at") for info in model_state.values() if info.get("trained_at")]
        if trained_ats:
            last_trained_at = max(trained_ats)
    restart_count = _recent_restart_count()
    if restart_count >= RESTART_ALERT_THRESHOLD:
        _send_alert_throttled(
            "frequent_restarts",
            f"直近{RESTART_ALERT_WINDOW_HOURS}時間でwebappが{restart_count}回起動しています",
        )
    conn = _users_conn()
    try:
        since_24h = (now_jst_naive() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        economy_anomalies_24h = sum(
            1 for e in users.list_audit_log_since(conn, since_24h) if e["action"] == "経済異常検知"
        )
    finally:
        conn.close()
    return {
        "web_active": web_state == "active",
        "collect_timer_active": collect_timer_state == "active",
        "disk_used_pct": disk_used_pct,
        "pending_signups": _count_pending_signups(),
        "network_failures_24h": network_failures["count_24h"],
        "last_trained_at": last_trained_at,
        "model_count": len(model_state),
        "recent_restart_count": restart_count,
        "economy_anomalies_24h": economy_anomalies_24h,
    }


# 通知ベル(base.html)は全ページのヘッダーに出るため、_admin_overview()(systemctl showの
# subprocess呼び出し2回・ディスク使用量計算・DBクエリを含む)が管理者のページ表示のたびに
# 毎回走ってしまう。短いTTLでキャッシュし、無駄な外部プロセス起動を減らす
# (トレードオフとして、管理者トップ画面の表示が最大ADMIN_OVERVIEW_CACHE_SECONDS秒古くなりうる)。
ADMIN_OVERVIEW_CACHE_SECONDS = 30
_admin_overview_cache = {"data": None, "at": 0.0}
_admin_overview_cache_lock = threading.Lock()


def _admin_overview_cached():
    now = time.monotonic()
    with _admin_overview_cache_lock:
        cached = _admin_overview_cache["data"]
        if cached is not None and now - _admin_overview_cache["at"] < ADMIN_OVERVIEW_CACHE_SECONDS:
            return cached
    data = _admin_overview()
    with _admin_overview_cache_lock:
        _admin_overview_cache["data"] = data
        _admin_overview_cache["at"] = now
    return data


# ============================================================================
# 管理者ページ(アカウント管理・ログ・ステータス・モデル・ディスク・バックアップ・設定)
# ============================================================================


@app.route("/admin")
@requires_admin
def admin_home():
    logs = [{"key": k, "path": str(p), "exists": p.exists(),
             "size_kb": round(p.stat().st_size / 1024, 1) if p.exists() else 0}
            for k, p in ALLOWED_LOGS.items()]
    return render_template("admin.html", logs=logs, overview=_admin_overview())


@app.route("/admin/audit_log")
@requires_admin
def admin_audit_log():
    conn = _users_conn()
    try:
        entries = users.list_audit_log(conn, limit=500)
    finally:
        conn.close()
    return render_template("admin_audit_log.html", entries=entries)


@app.route("/admin/accounts")
@requires_admin
def admin_accounts():
    conn = _users_conn()
    try:
        accounts = users.list_users(conn)
        pending = users.list_signup_requests(conn, status="pending")
        locked_failures = {
            a["id"]: users.recent_login_failures(conn, a["email"])
            for a in accounts if a["locked_at"]
        }
        role_requests = users.list_pending_role_change_requests(conn)
        role_request_users = users.get_users_by_ids(
            conn, [r["target_user_id"] for r in role_requests] + [r["requested_by"] for r in role_requests]
        )
    finally:
        conn.close()
    return render_template(
        "admin_accounts.html", accounts=accounts, pending=pending, locked_failures=locked_failures,
        role_requests=role_requests, role_request_users=role_request_users,
    )


@app.route("/admin/accounts/<int:user_id>/unlock", methods=["POST"])
@requires_admin
def admin_accounts_unlock(user_id):
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        users.unlock_account(conn, user_id)
        _record_audit("ロック解除", f"{target['name']}({target['email']})のアカウントロックを解除")
        return _finish("admin_accounts", f"{target['name']}さんのロックを解除しました。")
    finally:
        conn.close()


@app.route("/admin/accounts/<int:user_id>/role", methods=["POST"])
@requires_admin
def admin_accounts_role(user_id):
    """管理者への昇格(general→admin)は、権限昇格の悪用・誤操作を防ぐため即時反映せず、
    別の管理者による承認を必須にする(admin_accounts_role_approve参照)。降格
    (admin→general)は安全側の変更なので従来どおり即時反映する。"""
    role = request.form.get("role")
    if role not in ("general", "admin"):
        abort(400)
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        if target["role"] == "admin" and role != "admin" and users.count_admins(conn) <= 1:
            return _finish("admin_accounts", "最後の管理者アカウントの権限は変更できません。", success=False)
        if role == "admin" and target["role"] != "admin":
            now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
            users.create_role_change_request(conn, user_id, role, session["user_id"], now_str)
            _record_audit("管理者昇格を申請", f"{target['name']}({target['email']})を管理者に昇格申請")
            _send_alert_throttled(
                f"role_request:{user_id}",
                f"{session.get('name')}さんが{target['name']}さんの管理者昇格を申請しました。"
                f"他の管理者の承認が必要です。",
            )
            return _finish(
                "admin_accounts",
                f"{target['name']}さんの管理者昇格を申請しました。他の管理者の承認後に反映されます。",
            )
        users.set_role(conn, user_id, role)
        _record_audit("ロール変更", f"{target['name']}({target['email']})を{target['role']}→{role}に変更")
        return _finish("admin_accounts", f"{target['name']}さんのロールを変更しました。")
    finally:
        conn.close()


@app.route("/admin/accounts/role-requests/<int:request_id>/approve", methods=["POST"])
@requires_admin
def admin_accounts_role_approve(request_id):
    conn = _users_conn()
    try:
        req = users.get_role_change_request(conn, request_id)
        if req is None or req["status"] != "pending":
            abort(404)
        if req["requested_by"] == session["user_id"]:
            return _finish("admin_accounts", "自分自身が申請した昇格は承認できません。別の管理者に依頼してください。", success=False)
        target = users.get_user_by_id(conn, req["target_user_id"])
        if target is None:
            users.set_role_change_request_status(conn, request_id, "canceled")
            return _finish("admin_accounts", "対象のアカウントが見つかりませんでした。", success=False)
        users.set_role(conn, target["id"], req["requested_role"])
        users.set_role_change_request_status(conn, request_id, "approved")
        _record_audit("管理者昇格を承認", f"{target['name']}({target['email']})の管理者昇格を承認")
        return _finish("admin_accounts", f"{target['name']}さんを管理者に昇格しました。")
    finally:
        conn.close()


@app.route("/admin/accounts/role-requests/<int:request_id>/reject", methods=["POST"])
@requires_admin
def admin_accounts_role_reject(request_id):
    conn = _users_conn()
    try:
        req = users.get_role_change_request(conn, request_id)
        if req is None or req["status"] != "pending":
            abort(404)
        target = users.get_user_by_id(conn, req["target_user_id"])
        users.set_role_change_request_status(conn, request_id, "rejected")
        _record_audit(
            "管理者昇格を却下",
            f"{target['name'] if target else req['target_user_id']}の管理者昇格申請を却下",
        )
        return _finish("admin_accounts", "昇格申請を却下しました。")
    finally:
        conn.close()


@app.route("/admin/accounts/<int:user_id>/edit", methods=["POST"])
@requires_admin
def admin_accounts_edit(user_id):
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)

        if not name or not email:
            return _finish("admin_accounts", "お名前とメールアドレスは必須です。", success=False)
        if "@" not in email:
            return _finish("admin_accounts", "メールアドレスの形式が正しくありません。", success=False)
        existing = users.get_user_by_email(conn, email)
        if existing and existing["id"] != user_id:
            return _finish("admin_accounts", "このメールアドレスは既に別のアカウントで使われています。", success=False)
        if password and len(password) < MIN_PASSWORD_LENGTH:
            return _finish(
                "admin_accounts", f"パスワードを変更する場合は{MIN_PASSWORD_LENGTH}文字以上にしてください。",
                success=False,
            )

        password_hash = users.generate_password_hash(password) if password else None
        users.update_user(conn, user_id, name=name, email=email, password_hash=password_hash)
        if session.get("user_id") == user_id:
            session["name"] = name
        changes = []
        if target["name"] != name:
            changes.append(f"名前: {target['name']}→{name}")
        if target["email"] != email:
            changes.append(f"メール: {target['email']}→{email}")
        if password:
            changes.append("パスワード変更")
        _record_audit("アカウント編集", f"{name}({email}): " + ("、".join(changes) if changes else "変更なし"))
        return _finish("admin_accounts", f"{name}さんの情報を更新しました。")
    finally:
        conn.close()


@app.route("/admin/accounts/<int:user_id>/delete", methods=["POST"])
@requires_admin
def admin_accounts_delete(user_id):
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        if target["role"] == "admin" and users.count_admins(conn) <= 1:
            return _finish("admin_accounts", "最後の管理者アカウントは削除できません。", success=False)
        users.delete_user(conn, user_id)
        message = f"{target['name']}さんのアカウントを削除しました。"
    finally:
        conn.close()
    _record_audit("アカウント削除", f"{target['name']}({target['email']})を削除")
    if session.get("user_id") == user_id:
        session.clear()
        if _wants_json():
            return jsonify({"message": message, "success": True, "redirect": url_for("login")})
        flash(message, "success")
        return redirect(url_for("login"))
    return _finish("admin_accounts", message)


@app.route("/admin/accounts/requests/<int:request_id>/approve", methods=["POST"])
@requires_admin
def admin_accounts_approve(request_id):
    conn = _users_conn()
    try:
        req = users.get_signup_request(conn, request_id)
        created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        ok = users.approve_signup_request(conn, request_id, created_at)
    finally:
        conn.close()
    if ok and req:
        _record_audit("登録申請承認", f"{req['name']}({req['email']})")
    return _finish(
        "admin_accounts", "承認してアカウントを作成しました。" if ok else "この申請は既に処理済みです。",
        success=ok,
    )


@app.route("/admin/accounts/requests/<int:request_id>/reject", methods=["POST"])
@requires_admin
def admin_accounts_reject(request_id):
    conn = _users_conn()
    try:
        req = users.get_signup_request(conn, request_id)
        users.reject_signup_request(conn, request_id)
    finally:
        conn.close()
    if req:
        _record_audit("登録申請拒否", f"{req['name']}({req['email']})")
    return _finish("admin_accounts", "申請を拒否しました。")


@app.route("/admin/accounts/requests/bulk", methods=["POST"])
@requires_admin
def admin_accounts_requests_bulk():
    """選択した複数の登録申請を、まとめて承認/拒否する。"""
    action = request.form.get("bulk_action")
    request_ids = request.form.getlist("request_ids")
    if action not in ("approve", "reject") or not request_ids:
        return _finish("admin_accounts", "対象の申請を選択してください。", success=False)

    conn = _users_conn()
    processed = []
    try:
        created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        n = 0
        for rid in request_ids:
            try:
                rid = int(rid)
            except ValueError:
                continue
            req = users.get_signup_request(conn, rid)
            if action == "approve":
                if users.approve_signup_request(conn, rid, created_at):
                    n += 1
                    if req:
                        processed.append(req["email"])
            else:
                users.reject_signup_request(conn, rid)
                n += 1
                if req:
                    processed.append(req["email"])
    finally:
        conn.close()
    verb = "承認" if action == "approve" else "拒否"
    if processed:
        _record_audit(f"登録申請一括{verb}", f"{n}件: " + "、".join(processed))
    return _finish("admin_accounts", f"{n}件の申請を{verb}しました。")


@app.route("/admin/logs/<key>")
@requires_admin
def admin_log(key):
    path = ALLOWED_LOGS.get(key)
    if path is None:
        abort(404)
    if not path.exists():
        return render_template("admin_log.html", key=key, lines=["(ログファイルがまだありません)"])
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()[-LOG_TAIL_LINES:]
    return render_template("admin_log.html", key=key, lines=lines)


@app.route("/admin/errors")
@requires_admin
def admin_errors():
    return render_template(
        "admin_errors.html", issues=_recent_log_issues(),
        network_failures=_network_failure_summary(),
    )


@app.route("/admin/status")
@requires_admin
def admin_status():
    props = ["ActiveState", "SubState", "Result", "ActiveEnterTimestamp",
              "ExecMainStartTimestamp", "ExecMainExitTimestamp"]
    units = [{"unit": unit, **_systemctl_show(unit, props)} for unit in SYSTEMD_UNITS]
    timer_info = _systemctl_show("river-fog-collect.timer", ["NextElapseUSecRealtime", "LastTriggerUSec"])
    running = {key: _is_script_running(script) for key, script in RUNNABLE_SCRIPTS.items()}
    return render_template(
        "admin_status.html", units=units, timer_info=timer_info,
        crontab=_crontab_safe(), running=running,
        resources=_read_server_resources(),
    )


@app.route("/admin/cockpit")
@requires_admin
def admin_cockpit():
    return render_template("admin_cockpit.html")


@app.route("/admin/status/resources.json")
@requires_admin
def admin_status_resources_json():
    """サーバーリソース(CPU負荷・メモリ・温度)をJSONで返す。admin_status.htmlが
    数秒おきにポーリングしてCockpit風にリアルタイム更新するために使う。
    /proc・/sysの読み取りのみでsubprocessを呼ばないため、頻繁に呼んでも軽い。
    """
    return jsonify(_read_server_resources())


@app.route("/admin/smb")
@requires_admin
def admin_smb():
    """Samba([river-fog]共有)の設定・サービス状態を閲覧専用で表示する。
    設定の変更やsmbd/nmbdの再起動はここでは行わない(sudoersの追加が必要になり、
    ネットワーク共有の設定に管理画面から書き込めるようにするリスクが見合わないと
    判断し、閲覧のみに留めた)。設定を変更したい場合はサーバー上で直接
    /etc/samba/smb.confを編集し、`sudo systemctl reload smbd`すること。
    """
    share = _read_smb_share_config()
    smbd_state = _systemctl_show("smbd", ["ActiveState", "SubState"])
    nmbd_state = _systemctl_show("nmbd", ["ActiveState", "SubState"])
    return render_template(
        "admin_smb.html",
        share=share, share_name=SMB_SHARE_NAME,
        smbd_state=smbd_state, nmbd_state=nmbd_state,
        smb_conf_path=str(SMB_CONF_PATH),
    )


@app.route("/admin/run/<key>", methods=["POST"])
@requires_admin
def admin_run(key):
    script = RUNNABLE_SCRIPTS.get(key)
    if script is None:
        abort(404)
    if _is_script_running(script):
        return _finish("admin_status", f"{script} は既に実行中のため、開始しませんでした。", success=False)
    _run_script_async(script)
    return _finish("admin_status", f"{script} の実行を開始しました。数分後にログを確認してください。")


@app.route("/admin/restart", methods=["POST"])
@requires_admin
def admin_restart():
    """webappサービス自体の再起動。今応答しているプロセスがまさに落ちるため、応答を
    返し切ってから少し遅れて再起動するようにする（sudoers側でこのコマンド1つだけ
    パスワード無しの実行を許可している）。"""
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", "sleep 1 && sudo -n systemctl restart river-fog-web.service"],
            start_new_session=True,
        )
    except Exception:
        log.exception("Failed to trigger webapp restart")
        return _finish("admin_status", "再起動の開始に失敗しました。詳細はログを確認してください。", success=False)
    return _finish("admin_status", "webappサービスを再起動します(数秒後にページを再読み込みしてください)。")


@app.route("/admin/models")
@requires_admin
def admin_models():
    state = fog_model.load_model_state()
    rows = []
    for col in sorted(state.keys()):
        info = state[col]
        rows.append({
            "station": col,
            "model_type": fog_model.normalize_model_type_label(info.get("model_type")),
            "n": info.get("n"),
            "accuracy": info.get("accuracy"),
            "fog_f1": info.get("fog_f1"),
            "rf_only": info.get("rf_only"),
            "xgb_only": info.get("xgb_only"),
            "pooled": info.get("pooled"),
            "trained_at": info.get("trained_at"),
        })

    rollback_lines = []
    predict_log_path = ALLOWED_LOGS.get("predict")
    if predict_log_path and predict_log_path.exists():
        try:
            with open(predict_log_path, encoding="utf-8", errors="replace") as f:
                rollback_lines = [ln.rstrip("\n") for ln in f if "Rolled back to previous model" in ln]
        except Exception as e:
            log.warning("Failed to scan predict.log for rollbacks: %s", e)

    low_accuracy_stations = _low_accuracy_stations(state)
    return render_template(
        "admin_models.html", rows=rows, rollback_lines=rollback_lines[-50:],
        low_accuracy_stations=low_accuracy_stations,
    )


@app.route("/admin/models/chart.png")
@requires_admin
def admin_models_chart():
    png = _render_metrics_trend_png(_load_metrics_history_trend())
    return Response(png, mimetype="image/png")


@app.route("/admin/models/compare_chart.png")
@requires_admin
def admin_models_compare_chart():
    state = fog_model.load_model_state()
    png = _render_model_compare_png(state)
    return Response(png, mimetype="image/png")


@app.route("/admin/models/importance_chart.png")
@requires_admin
def admin_models_importance_chart():
    state = fog_model.load_model_state()
    importance = fog_model.compute_feature_importance(state)
    png = _render_feature_importance_png(importance)
    return Response(png, mimetype="image/png")


@app.route("/admin/models/confusion_matrix.png")
@requires_admin
def admin_models_confusion_matrix():
    state = fog_model.load_model_state()
    labels, matrix = fog_model.compute_confusion_matrix(state)
    png = _render_confusion_matrix_png(labels, matrix)
    return Response(png, mimetype="image/png")


@app.route("/admin/models/importance_low_accuracy_chart.png")
@requires_admin
def admin_models_importance_low_accuracy_chart():
    state = fog_model.load_model_state()
    only_stations = set(_low_accuracy_stations(state))
    importance = fog_model.compute_feature_importance(state, only_stations=only_stations)
    png = _render_feature_importance_png(importance)
    return Response(png, mimetype="image/png")


def _image_archive_day_dirs():
    """images/配下の日付フォルダを(date, Path)のリストで日付昇順に返す(不正な名前は除外)。"""
    if not IMAGE_DIR.exists():
        return []
    dirs = []
    for d in IMAGE_DIR.iterdir():
        if not d.is_dir():
            continue
        try:
            day = datetime.strptime(d.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        dirs.append((day, d))
    dirs.sort(key=lambda t: t[0])
    return dirs


@app.route("/admin/disk")
@requires_admin
def admin_disk():
    rows = []
    for label, path in DISK_USAGE_PARTS:
        size = _dir_size_bytes(path)
        rows.append({"label": label, "human": _human_size(size)})
    total, used, free = shutil.disk_usage(BASE_DIR)
    disk = {
        "total": _human_size(total), "used": _human_size(used), "free": _human_size(free),
        "used_pct": round(100.0 * used / total, 1) if total else 0,
    }
    image_days = _image_archive_day_dirs()
    image_archive = {
        "day_count": len(image_days),
        "oldest": image_days[0][0].strftime("%Y-%m-%d") if image_days else None,
        "newest": image_days[-1][0].strftime("%Y-%m-%d") if image_days else None,
        "human": _human_size(_dir_size_bytes(IMAGE_DIR)),
    }
    return render_template(
        "admin_disk.html", rows=rows, disk=disk, image_archive=image_archive,
        image_cleanup_default_days=IMAGE_CLEANUP_DEFAULT_DAYS,
    )


@app.route("/admin/disk/images_cleanup", methods=["POST"])
@requires_admin
def admin_disk_images_cleanup():
    """容量が圧迫してきた場合に、指定日数より古いカメラ画像アーカイブを手動で削除する
    (通常の自動削除は行わず無期限保存としているため、この手動操作が唯一の削除手段)。"""
    try:
        days = int(request.form.get("days", IMAGE_CLEANUP_DEFAULT_DAYS))
    except ValueError:
        days = IMAGE_CLEANUP_DEFAULT_DAYS
    days = max(1, days)
    cutoff = datetime.now(JST).date() - timedelta(days=days)
    deleted = 0
    freed_bytes = 0
    for day, day_dir in _image_archive_day_dirs():
        if day < cutoff:
            freed_bytes += _dir_size_bytes(day_dir)
            shutil.rmtree(day_dir)
            deleted += 1
    if deleted:
        _record_audit(
            "カメラ画像アーカイブ削除",
            f"{cutoff.strftime('%Y-%m-%d')}より前を{deleted}日分削除(解放: {_human_size(freed_bytes)})",
        )
        return _finish(
            "admin_disk",
            f"{cutoff.strftime('%Y-%m-%d')} より前のカメラ画像アーカイブを{deleted}日分削除しました(解放: {_human_size(freed_bytes)})。",
        )
    return _finish("admin_disk", "削除対象の画像アーカイブはありませんでした。")


@app.route("/admin/backup.zip")
@requires_admin
def admin_backup():
    """data/一式をZIPにまとめてダウンロードする（現状バックアップが無いことの補い）。
    容量が大きくなりうるため、メモリ上ではなく一時ファイルに書き出してから送る。
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    tmp_path = tmp.name
    try:
        # .envの内容(APIキー等)を含むが、このエンドポイント自体が管理者限定
        # (@requires_admin)のため許容する。backup.py(定期自動バックアップ)とも
        # 共通のロジック(common.build_data_backup_zip)を使う。
        build_data_backup_zip(tmp_path)
        stamp = datetime.now(JST).strftime("%Y%m%d_%H%M")
        response = send_file(
            tmp_path, mimetype="application/zip", as_attachment=True,
            download_name=f"river-fog-backup_{stamp}.zip",
        )
        response.call_on_close(lambda: os.path.exists(tmp_path) and os.remove(tmp_path))
        return response
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _prune_restore_backups(pattern, keep=RESTORE_BACKUP_KEEP_GENERATIONS):
    """BASE_DIR直下のdata_before_restore_*/.env_before_restore_*のうち、直近keep件を
    残して古いものを削除する(復元を繰り返すたびに無期限に溜まっていくのを防ぐ)。
    """
    paths = sorted(BASE_DIR.glob(pattern), key=lambda p: p.name, reverse=True)
    for old in paths[keep:]:
        try:
            if old.is_dir():
                shutil.rmtree(old)
            else:
                old.unlink()
            log.info("Pruned old restore backup: %s", old.name)
        except Exception as e:
            log.warning("Failed to prune old restore backup %s: %s", old.name, e)


def _safe_extract_zip(zf, dest_dir):
    """zip-slip対策: 各エントリの展開先がdest_dir配下に収まることを、実際に展開する前に
    全件確認する（絶対パスや".."を含むエントリで想定外の場所に書き込まれるのを防ぐ）。
    また、展開後の合計サイズがRESTORE_MAX_UNCOMPRESSED_BYTESを超えないことも確認する
    (圧縮率の高いZIPでディスクを枯渇させるzip爆弾対策)。
    """
    dest_dir = dest_dir.resolve()
    total_size = 0
    for member in zf.infolist():
        member_path = (dest_dir / member.filename).resolve()
        if member_path != dest_dir and dest_dir not in member_path.parents:
            raise ValueError(f"Unsafe path in zip: {member.filename}")
        total_size += member.file_size
        if total_size > RESTORE_MAX_UNCOMPRESSED_BYTES:
            raise ValueError("展開後の合計サイズが上限を超えています")
    zf.extractall(dest_dir)


@app.route("/admin/backup/restore", methods=["POST"])
@requires_admin
def admin_backup_restore():
    """アップロードされたバックアップZIPでdata/一式(および含まれていれば.env)を丸ごと復元する。
    アカウント情報を含む全データを置き換える取り消しにくい操作のため、確認フレーズの
    一致を必須にし、復元前のdata/・.envは削除せず日時付きファイル/ディレクトリに退避しておく
    （何か問題があれば手動で戻せるように）。
    """
    confirm = request.form.get("confirm", "").strip().lower()
    if confirm != RESTORE_CONFIRM_PHRASE:
        flash(f'確認のため、入力欄に半角で "{RESTORE_CONFIRM_PHRASE}" と入力してください。', "error")
        return redirect(url_for("admin_disk"))

    file = request.files.get("backup_zip")
    if not file or not file.filename:
        flash("ZIPファイルを選択してください。", "error")
        return redirect(url_for("admin_disk"))
    if not file.filename.lower().endswith(".zip"):
        flash("ZIPファイル(.zip)を選択してください。", "error")
        return redirect(url_for("admin_disk"))

    tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp_zip_path = Path(tmp_zip.name)
    tmp_zip.close()
    staging_dir = None
    try:
        file.save(tmp_zip_path)
        if tmp_zip_path.stat().st_size > RESTORE_MAX_UPLOAD_BYTES:
            flash(f"ファイルが大きすぎます({RESTORE_MAX_UPLOAD_BYTES // (1024 ** 3)}GB以下にしてください)。", "error")
            return redirect(url_for("admin_disk"))

        try:
            zf = zipfile.ZipFile(tmp_zip_path)
        except zipfile.BadZipFile:
            flash("有効なZIPファイルではありません。", "error")
            return redirect(url_for("admin_disk"))

        staging_dir = Path(tempfile.mkdtemp(prefix="river-fog-restore-", dir=str(BASE_DIR)))
        with zf:
            _safe_extract_zip(zf, staging_dir)

        stamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")

        # .envはadmin_backup()がdata/の外(BASE_DIR直下)に別途含めたものなので、
        # staging_dir(このままdata/になる)の外へ退避してから個別に反映する。
        # 元の.envもdata/と同様に日時付きでバックアップしておく(何か問題があれば手動で戻せるように)。
        staged_env = staging_dir / ".env"
        if staged_env.exists():
            if ENV_PATH.exists():
                shutil.copy2(ENV_PATH, BASE_DIR / f".env_before_restore_{stamp}")
            shutil.move(str(staged_env), str(ENV_PATH))

        pre_restore_dir = BASE_DIR / f"data_before_restore_{stamp}"
        data_dir = BASE_DIR / "data"
        if data_dir.exists():
            data_dir.rename(pre_restore_dir)
        staging_dir.rename(data_dir)
        staging_dir = None
        _prune_restore_backups("data_before_restore_*")
        _prune_restore_backups(".env_before_restore_*")

        _record_audit(
            "バックアップ復元",
            f"アップロードされたZIPからdata/を復元(復元前を{pre_restore_dir.name}に退避)",
        )
        flash(
            f"data/一式を復元しました。復元前のデータは {pre_restore_dir.name} "
            "に退避してあります(問題なければ後で手動削除してください)。サービスを再起動します。",
            "success",
        )
        subprocess.Popen(
            ["/bin/sh", "-c", "sleep 1 && sudo -n systemctl restart river-fog-web.service"],
            start_new_session=True,
        )
    except Exception as e:
        log.exception("Backup restore failed")
        flash("復元に失敗しました。詳細はログを確認してください。", "error")
    finally:
        if tmp_zip_path.exists():
            tmp_zip_path.unlink()
        if staging_dir is not None and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
    return redirect(url_for("admin_disk"))


@app.route("/admin/terminal")
@requires_admin
def admin_terminal():
    """サーバーのシェルにブラウザからアクセスするためのページ(ttydをiframeで埋め込む)。
    ttydは全インターフェースでBasic認証をかけて待ち受けているため、ここでは単に
    そのURLをiframeに埋め込むだけで、認証自体はttyd側が別途行う。Tailscale経由・LAN経由の
    どちらでこのダッシュボードを開いても繋がるよう、ホスト名は固定せずrequest.hostから取る。"""
    host = request.host.split(":")[0]
    return render_template("admin_terminal.html", terminal_url=f"http://{host}:{TTYD_PORT}")


@app.route("/admin/db")
@requires_admin
def admin_db():
    stats = None
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            try:
                stats = prediction_log.db_stats(conn)
            finally:
                conn.close()
        except Exception as e:
            log.warning("Failed to read predictions.db stats: %s", e)
    return render_template("admin_db.html", stats=stats, default_days=DB_CLEANUP_DEFAULT_DAYS)


@app.route("/admin/db/cleanup", methods=["POST"])
@requires_admin
def admin_db_cleanup():
    try:
        days = int(request.form.get("days", DB_CLEANUP_DEFAULT_DAYS))
    except ValueError:
        days = DB_CLEANUP_DEFAULT_DAYS
    days = max(1, days)
    before_dt = (now_jst_naive() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            deleted = prediction_log.delete_verified_before(conn, before_dt)
        finally:
            conn.close()
        _record_audit("予測DBクリーンアップ", f"{before_dt}より前の検証済みレコードを{deleted}件削除")
        return _finish("admin_db", f"{before_dt} より前の答え合わせ済みレコードを{deleted}件削除しました。")
    except Exception:
        log.exception("DB cleanup failed")
        return _finish("admin_db", "クリーンアップに失敗しました。詳細はログを確認してください。", success=False)


@app.route("/admin/camera_health")
@requires_admin
def admin_camera_health():
    rows = _camera_uptime_stats()
    return render_template("admin_camera_health.html", rows=rows, days=CAMERA_HEALTH_DAYS)


@app.route("/admin/env")
@requires_admin
def admin_env():
    return render_template("admin_env.html", env_status=_env_status())


@app.route("/admin/settings", methods=["GET", "POST"])
@requires_admin
def admin_settings():
    if request.method == "POST":
        updates = {key: request.form.get(key, "").strip() for key in EDITABLE_ENV_KEYS}
        try:
            _write_editable_env_values(updates)
            flash("設定を保存しました。次回のcollect.py/predict.py実行から反映されます。", "success")
        except Exception as e:
            log.exception("Failed to write .env")
            flash("設定の保存に失敗しました。詳細はログを確認してください。", "error")
        return redirect(url_for("admin_settings"))
    return render_template("admin_settings.html", values=_read_editable_env_values())


@app.route("/admin/accuracy_report")
@requires_admin
def admin_accuracy_report():
    summary = _accuracy_summary_safe()
    by_station = []
    if summary:
        by_station = sorted(
            summary["by_station"],
            key=lambda r: (r["n"] == 0, r["correct"] / r["n"] if r["n"] else 1.0),
        )
    return render_template("admin_accuracy_report.html", by_station=by_station, overall=summary["overall"] if summary else None)


def _generate_report_pdf(start_date, end_date):
    """指定期間の霧発生状況・予測精度をまとめたPDFレポートを生成し、bytesで返す。
    matplotlibのPdfPages機能を使う(新規ライブラリの追加は不要)。
    """
    from matplotlib.backends.backend_pdf import PdfPages

    main_df, phenom_df, station_names = fog_data.load_river_fog_data()
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())
    mask = (phenom_df["datetime"] >= start_dt) & (phenom_df["datetime"] <= end_dt)
    period_df = phenom_df[mask]

    fog_counts = {}
    for name in station_names:
        if name not in period_df.columns:
            fog_counts[name] = 0
            continue
        codes = period_df[name].map(encode_phenomena_cell)
        fog_counts[name] = int(codes.isin(SUMMARY_FOG_CODES).sum())
    total_slots = len(period_df)

    accuracy = _accuracy_summary_safe()
    trend = _accuracy_trend_safe()
    trend_in_range = [r for r in trend if start_date.isoformat() <= r["date"] <= end_date.isoformat() and r["n"] > 0]

    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        fig = plt.figure(figsize=(8.27, 11.69))
        fig.text(0.5, 0.62, "只見川霧観測レポート", ha="center", fontsize=24, fontweight="bold")
        fig.text(0.5, 0.55, f"{start_date} 〜 {end_date}", ha="center", fontsize=14)
        overall = accuracy["overall"] if accuracy else None
        if overall and overall["n"]:
            fig.text(
                0.5, 0.48,
                f"この期間の全体的中率(集計期間全体): {100.0 * overall['correct'] / overall['n']:.1f}% "
                f"({overall['correct']}/{overall['n']}件)",
                ha="center", fontsize=11,
            )
        fig.text(0.5, 0.08, f"生成日時: {now_jst_naive().strftime('%Y-%m-%d %H:%M')}", ha="center", fontsize=9, color="gray")
        pdf.savefig(fig)
        plt.close(fig)

        if total_slots:
            sorted_counts = sorted(fog_counts.items(), key=lambda kv: kv[1])
            names = [n for n, _ in sorted_counts]
            counts = [c for _, c in sorted_counts]
            fig, ax = plt.subplots(figsize=(8.27, 11.69))
            ax.barh(names, counts, color="#1a5276")
            ax.set_xlabel(f"霧発生回数(観測コマ数、全{total_slots}コマ中)")
            ax.set_title("地点別 霧発生回数")
            ax.tick_params(axis="y", labelsize=7)
            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)
        else:
            fig = plt.figure(figsize=(8.27, 4))
            fig.text(0.5, 0.5, "この期間の観測データがありません。", ha="center", fontsize=12)
            pdf.savefig(fig)
            plt.close(fig)

        if trend_in_range:
            dates = [datetime.strptime(r["date"], "%Y-%m-%d") for r in trend_in_range]
            rates = [100.0 * r["correct"] / r["n"] for r in trend_in_range]
            fig, ax = plt.subplots(figsize=(8.27, 5.5))
            ax.plot(dates, rates, marker="o", markersize=3, color="#1a5276")
            ax.set_title("予測的中率の推移")
            ax.set_ylabel("的中率(%)")
            ax.set_ylim(0, 100)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
            fig.autofmt_xdate()
            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)

    return buf.getvalue()


@app.route("/admin/report", methods=["GET", "POST"])
@requires_admin
def admin_report():
    today = now_jst_naive().date()
    default_start = today - timedelta(days=30)
    if request.method == "POST":
        try:
            start_date = datetime.strptime(request.form.get("start_date", ""), "%Y-%m-%d").date()
            end_date = datetime.strptime(request.form.get("end_date", ""), "%Y-%m-%d").date()
        except ValueError:
            flash("開始日・終了日を正しく指定してください。", "error")
            return redirect(url_for("admin_report"))
        if start_date > end_date:
            flash("開始日は終了日より前の日付にしてください。", "error")
            return redirect(url_for("admin_report"))
        try:
            pdf_bytes = _generate_report_pdf(start_date, end_date)
        except Exception:
            log.exception("Report generation failed")
            flash("レポートの生成に失敗しました。詳細はログを確認してください。", "error")
            return redirect(url_for("admin_report"))
        filename = f"river-fog-report_{start_date}_{end_date}.pdf"
        return send_file(
            io.BytesIO(pdf_bytes), mimetype="application/pdf",
            as_attachment=True, download_name=filename,
        )
    return render_template("admin_report.html", default_start=default_start, default_end=today)


@app.route("/admin/gemini")
@requires_admin
def admin_gemini():
    conn = chat_log.connect(CHAT_DB_PATH)
    try:
        chat_rows = {r["date"]: r["count"] for r in chat_log.daily_message_counts(conn, days=GEMINI_LOG_DAYS)}
    finally:
        conn.close()
    rows = _gemini_log_stats()
    for r in rows:
        r["chat_calls"] = chat_rows.get(r["date"], 0)
    return render_template(
        "admin_gemini.html", rows=rows,
        chat_daily_limit_per_user=CHAT_DAILY_LIMIT_PER_USER, chat_daily_limit_total=CHAT_DAILY_LIMIT_TOTAL,
    )


@app.route("/admin/maintenance", methods=["GET", "POST"])
@requires_admin
def admin_maintenance():
    if request.method == "POST":
        enabled = request.form.get("enabled") == "on"
        message = request.form.get("message", "").strip()
        _set_maintenance_state(enabled, message)
        return _finish(
            "admin_maintenance",
            "メンテナンスモードを有効にしました。" if enabled else "メンテナンスモードを解除しました。",
        )
    return render_template("admin_maintenance.html", state=_maintenance_state())


@app.route("/admin/announcements")
@requires_admin
def admin_announcements():
    conn = _announcements_conn()
    try:
        items = announcements.list_announcements(conn)
        for item in items:
            item["response_count"] = announcements.response_count(conn, item["id"])
    finally:
        conn.close()
    return render_template("admin_announcements.html", items=items)


@app.route("/admin/announcements/new", methods=["POST"])
@requires_admin
def admin_announcements_new():
    kind = request.form.get("kind", "message")
    message = request.form.get("message", "").strip()
    start_at = request.form.get("start_at", "").strip() or None
    end_at = request.form.get("end_at", "").strip() or None
    choices = None
    if kind == "choice":
        choices = [c.strip() for c in request.form.get("choices", "").splitlines() if c.strip()]
        if len(choices) < 2:
            flash("選択式アンケートには選択肢を2つ以上入力してください。", "error")
            return redirect(url_for("admin_announcements"))
    if not message:
        flash("本文/質問文を入力してください。", "error")
        return redirect(url_for("admin_announcements"))

    created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    conn = _announcements_conn()
    try:
        announcements.create_announcement(conn, kind, message, choices, start_at, end_at, created_at)
    finally:
        conn.close()
    flash("お知らせ/アンケートを作成しました。", "success")
    return redirect(url_for("admin_announcements"))


@app.route("/admin/announcements/<int:announcement_id>/toggle", methods=["POST"])
@requires_admin
def admin_announcements_toggle(announcement_id):
    conn = _announcements_conn()
    try:
        ann = announcements.get_announcement(conn, announcement_id)
        if ann is None:
            abort(404)
        announcements.set_active(conn, announcement_id, not ann["active"])
    finally:
        conn.close()
    return redirect(url_for("admin_announcements"))


@app.route("/admin/announcements/<int:announcement_id>/delete", methods=["POST"])
@requires_admin
def admin_announcements_delete(announcement_id):
    conn = _announcements_conn()
    try:
        announcements.delete_announcement(conn, announcement_id)
    finally:
        conn.close()
    flash("削除しました。", "success")
    return redirect(url_for("admin_announcements"))


@app.route("/admin/announcements/<int:announcement_id>/results")
@requires_admin
def admin_announcements_results(announcement_id):
    conn = _announcements_conn()
    try:
        ann = announcements.get_announcement(conn, announcement_id)
        if ann is None:
            abort(404)
        summary = announcements.response_summary(conn, announcement_id)
    finally:
        conn.close()
    return render_template("admin_announcement_results.html", ann=ann, summary=summary)


@app.route("/announcements/<int:announcement_id>/respond", methods=["POST"])
def respond_announcement(announcement_id):
    conn = _announcements_conn()
    try:
        ann = announcements.get_announcement(conn, announcement_id)
        if ann is None or not ann["active"]:
            abort(404)
        visitor_id, is_new = _get_visitor_id()
        choice_index = request.form.get("choice_index")
        answer_text = request.form.get("answer_text", "").strip() or None
        submitted_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        announcements.record_response(
            conn, announcement_id, visitor_id,
            int(choice_index) if choice_index not in (None, "") else None,
            answer_text, submitted_at,
        )
    finally:
        conn.close()
    message = "回答ありがとうございました。"
    if _wants_json():
        response = jsonify({"message": message, "success": True})
    else:
        flash(message, "success")
        response = redirect(request.referrer or url_for("dashboard"))
    if is_new:
        response.set_cookie(VISITOR_COOKIE_NAME, visitor_id, max_age=VISITOR_COOKIE_MAX_AGE)
    return response


if __name__ == "__main__":
    from waitress import serve
    # 8080番は外部公開用にnginxが受け、Cockpit(9090番、別オリジン)と同一オリジンで
    # リバースプロキシするために127.0.0.1:8081をローカル専用で使う(直接の外部公開はしない)。
    # trusted_proxyを設定しないと、waitressはX-Forwarded-For等のヘッダーを黙って
    # 破棄してしまい、request.remote_addrが常に127.0.0.1(nginx/tailscaledからの接続元)
    # になってしまう。nginxとTailscale Funnelはどちらも127.0.0.1経由で接続してくる
    # ローカルの正規プロキシなので、これを信頼してX-Forwarded-Forを実際のクライアント
    # 情報として使う。
    log.info("Starting river-fog webapp on 127.0.0.1:8081 (behind nginx on :8080)")
    serve(
        app, host="127.0.0.1", port=8081,
        trusted_proxy="127.0.0.1", trusted_proxy_count=1,
        trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
        clear_untrusted_proxy_headers=True,
    )
