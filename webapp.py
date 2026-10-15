"""SMB共有(data/以下)と同じ内容をブラウザから見られるようにするダッシュボード。

LAN/Tailscale内からのみアクセスされる想定。全ページログイン必須(_require_login参照)で、
管理者専用の操作にはさらにロールチェック(requires_admin)がかかる。外部公開はしないこと。
"""
import configparser
import csv
import bisect
import hmac
import gzip
import io
import json
import logging
import math
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
from urllib.parse import quote, urlparse

from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from PIL import Image
from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template,
    request, send_file, send_from_directory, session, url_for,
)

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager
import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots

from common import (
    AIZU_EXPORT_XLSX_PATH, AIZU_TEMPLATE_XLSX, BASE_DIR, CAMERAS_CSV, DATA_XLSX,
    EXPORT_XLSX_PATH, JST, NETWORK_FAILURE_LOG_PATH, STATION_WEATHER_DB_PATH,
    TEMPLATE_XLSX, WEATHER_DB_PATH, build_data_backup_zip, load_camera_regions,
    now_jst_naive, representative_coords, send_alert,
)
import announcements
import users
import user_settings
import push_notify
import fog_data
import fog_model
import chart_style
import mpl_render
import fog_features
from fog_features import is_daylight as fog_features_is_daylight
import fog_tree_viz
import weather_store
import prediction_log
import station_weather
from fog_features import (
    COL_DEWPOINT, COL_HUMIDITY, COL_PRECIP, COL_SOLAR, COL_SUNSHINE, COL_TEMP,
    COL_VISIBILITY, COL_WIND_DIR_DEG, COL_WIND_SPEED, PHENOM_COLORS,
    PHENOM_LABELS, PhenomCodeError, SLASH_COLOR, SUMMARY_FOG_CODES, decode_phenom_value,
    encode_phenom_codes, encode_phenomena_cell, format_phenom_display, phenom_codes_set,
    phenom_label, primary_phenom_code,
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
# _login_attempts_by_ipは以前、呼び出しのたびに「そのIPの分だけ」しか期限切れ分を
# 整理していなかったため、一度アクセスして二度と来ないIP(攻撃者・organic トラフィック
# 問わず)のエントリが無期限に溜まり続けてメモリが緩やかに増え続けていた
# (2026-09-13の監査で指摘)。この間隔でIP全体を対象にした掃除を行う。
LOGIN_ATTEMPT_SWEEP_INTERVAL = timedelta(hours=1)
_login_attempts_last_sweep = datetime.now(JST)


def _client_ip():
    return request.remote_addr or "unknown"


def _sweep_login_attempts(now):
    """呼び出し頻度が高すぎない程度に間引きながら、_login_attempts_by_ip全体から
    期限切れ・空になったIPのエントリを削除する(呼び出し元で_login_attempts_lock保持中に呼ぶこと)。"""
    global _login_attempts_last_sweep
    if now - _login_attempts_last_sweep < LOGIN_ATTEMPT_SWEEP_INTERVAL:
        return
    _login_attempts_last_sweep = now
    for ip in list(_login_attempts_by_ip.keys()):
        attempts = [t for t in _login_attempts_by_ip[ip] if now - t < LOGIN_ATTEMPT_WINDOW]
        if attempts:
            _login_attempts_by_ip[ip] = attempts
        else:
            del _login_attempts_by_ip[ip]


REPORT_RATE_LIMIT_WINDOW = timedelta(minutes=10)
REPORT_RATE_LIMIT_MAX = 3
_report_generation_attempts = {}
_report_generation_lock = threading.Lock()


def _report_rate_limited(user_id):
    """PDFレポート生成(_generate_report_pdf)は一般ユーザーでも呼べる公開機能(/report)
    だが軽い処理ではないため、連打による負荷を防ぐレート制限を設ける(2026-09-13の
    監査で無制限だった点を指摘された)。ユーザーID単位のキーで、実在する登録ユーザー数
    しか増えないため_login_attempts_by_ipのような無期限増加の心配はない。"""
    now = datetime.now(JST)
    with _report_generation_lock:
        attempts = [
            t for t in _report_generation_attempts.get(user_id, []) if now - t < REPORT_RATE_LIMIT_WINDOW
        ]
        if len(attempts) >= REPORT_RATE_LIMIT_MAX:
            _report_generation_attempts[user_id] = attempts
            return True
        attempts.append(now)
        _report_generation_attempts[user_id] = attempts
        return False


def _login_rate_limited(ip):
    now = datetime.now(JST)
    with _login_attempts_lock:
        _sweep_login_attempts(now)
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
IMAGES_HOUR_ZIP_MAX_BYTES = 300 * 1024 * 1024
# デコンプレッションボム対策: 高圧縮率の画像(小さいファイルサイズで巨大な解像度)による
# メモリ枯渇を防ぐため、Pillowの展開後ピクセル数の上限を明示的に指定する
# (アバターとして必要なサイズよりかなり余裕を持たせた値)。
Image.MAX_IMAGE_PIXELS = 40_000_000
LOGIN_HEATMAP_DAYS = 371  # GitHubの草(コントリビューショングラフ)と同じく約53週間分

# --- ファイル共有(DM添付・班の共有ファイル置き場) -------------------------------------
# 拡張子による制限は設けていない(利用者からの要望で撤廃)。1ファイルあたりのサイズ上限も
# あえて設けていないが、ディスクを使い切ってサーバー全体(川霧の収集・予測パイプライン含む)を
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


CLAMAV_DB_DIR = Path("/var/lib/clamav")
CLAMAV_DB_FILES = ["daily.cvd", "daily.cld", "main.cvd", "main.cld", "bytecode.cvd", "bytecode.cld"]
CLAMAV_SIGNATURE_STALE_DAYS = 7
CLAMAV_SELFTEST_DIR = BASE_DIR / "data" / ".clamav_selftest"
# 本物のウイルスではなく、全アンチウイルス製品が検知するよう業界標準化されたテスト文字列
# (EICAR)。これを検知できるかどうかで、フェイルオープン設計の_scan_file_for_malware()が
# 実際にちゃんと機能しているか(ClamAVが落ちていて素通しになっていないか)を確認する。
EICAR_TEST_STRING = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
CLAMAV_RESCAN_DIRS = [SHARED_FILES_DIR, DM_ATTACHMENT_DIR, AVATAR_DIR]
CLAMAV_RESCAN_LOG_PATH = BASE_DIR / "logs" / "clamav_rescan.log"


def _clamav_db_info():
    """シグネチャDBのバージョン・最終更新日時をsudo無しで調べる(/var/lib/clamavの
    .cvd/.cldはworld-readableなためファイルのmtimeで判定できる)。"""
    newest_mtime = None
    for name in CLAMAV_DB_FILES:
        p = CLAMAV_DB_DIR / name
        if p.exists():
            mtime = datetime.fromtimestamp(p.stat().st_mtime)
            if newest_mtime is None or mtime > newest_mtime:
                newest_mtime = mtime
    version = None
    try:
        result = subprocess.run(["sigtool", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            version = result.stdout.strip()
    except Exception as e:
        log.warning("sigtool --version failed: %s", e)
    stale = (
        newest_mtime is not None
        and (datetime.now() - newest_mtime).days >= CLAMAV_SIGNATURE_STALE_DAYS
    )
    return {
        "version": version,
        "updated_at": newest_mtime.strftime("%Y-%m-%d %H:%M:%S") if newest_mtime else None,
        "stale": stale,
        "missing": newest_mtime is None,
    }


def _clamav_service_status():
    """clamd(スキャン本体)・freshclam(シグネチャ自動更新)の稼働状況を、書き込み系
    権限が不要なsystemctl show/is-enabledだけで調べる(sudo不要・admin_smb.htmlと同じ
    「閲覧専用」方針。有効化はサーバー上で直接操作してもらう)。"""
    daemon = _systemctl_show("clamav-daemon.service", ["ActiveState", "SubState", "ActiveEnterTimestamp"])
    freshclam = _systemctl_show(
        "clamav-freshclam.service", ["ActiveState", "SubState", "UnitFileState", "ActiveEnterTimestamp"],
    )
    return {"daemon": daemon, "freshclam": freshclam}


def _run_clamav_selftest():
    """EICARテスト文字列を実際にclamdscanへ通し、検知できるかをその場で確認する。
    戻り値のok=Trueはスキャン自体が正常に完了したこと(検知できたかどうかとは別)を示す。
    ok=True・detected=Falseは、ClamAVは動いているのに既知のテスト検体すら検知できない
    (シグネチャDB破損・設定不備等)という、フェイルオープンに気づきにくい深刻な状態を表す。
    """
    CLAMAV_SELFTEST_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = CLAMAV_SELFTEST_DIR / f"eicar_{uuid.uuid4().hex}.txt"
    try:
        tmp_path.write_bytes(EICAR_TEST_STRING)
        try:
            result = subprocess.run(
                ["clamdscan", "--fdpass", "--no-summary", str(tmp_path)],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as e:
            return {"ok": False, "detected": False, "error": str(e)}
        if result.returncode == 1:
            return {"ok": True, "detected": True, "error": None}
        if result.returncode == 0:
            return {"ok": True, "detected": False, "error": None}
        return {"ok": False, "detected": False, "error": (result.stdout + result.stderr)[:300]}
    finally:
        tmp_path.unlink(missing_ok=True)


def _run_clamav_rescan_async():
    """既にアップロード済みのファイル(共有ファイル・DM添付・アバター)を、アップロード時点
    より新しいシグネチャで再スキャンする(アップロード時はまだ未検知だった脅威が、その後の
    シグネチャ更新で検知可能になるケースに備えた事後監査)。件数によっては時間がかかるため、
    _run_script_asyncと同じ考え方でバックグラウンドプロセスとして切り離して実行する。"""
    existing_dirs = [str(d) for d in CLAMAV_RESCAN_DIRS if d.exists()]
    if not existing_dirs:
        return
    # このclamdscan(1.5.3)には-r/--recursiveオプション自体が無く(directoryを渡すと
    # デフォルトで中身がスキャンされる)、付けると警告が出るだけなので付けない。
    cmd = ["clamdscan", "--fdpass", "--no-summary", "--infected", *existing_dirs]
    try:
        with open(CLAMAV_RESCAN_LOG_PATH, "wb") as log_f:
            proc = subprocess.Popen(
                cmd, cwd=str(BASE_DIR), stdout=log_f, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        _pid_file("clamav_rescan").write_text(str(proc.pid))
        log.info("Launched clamav rescan as pid %d", proc.pid)
    except Exception:
        log.exception("Failed to launch clamav rescan")


def _tailscale_status():
    """`tailscale status --json`はデーモンが起動していれば一般ユーザーでもsudo無しで
    実行できる(このサーバーでも確認済み)。取得できなければNoneを返す。"""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log.warning("tailscale status failed: %s", result.stderr)
            return None
        return json.loads(result.stdout)
    except Exception as e:
        log.warning("tailscale status could not run: %s", e)
        return None


def _tailscale_version():
    try:
        result = subprocess.run(["tailscale", "version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip().splitlines()[0]
    except Exception as e:
        log.warning("tailscale version could not run: %s", e)
    return None


def _tailscale_autoupdate_prefs():
    try:
        result = subprocess.run(["tailscale", "debug", "prefs"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return json.loads(result.stdout).get("AutoUpdate")
    except Exception as e:
        log.warning("tailscale debug prefs could not run: %s", e)
    return None


# ttydはBasic認証をかけた別プロセスとしてsystemdで動かしている(river-fog-terminal.service)。
# 以前は全インターフェースにバインドしており、requires_admin・CSRF・Funnel判定を一切
# 経由せずBasic認証(以前はsshuser:sshuserという弱い組み合わせ)だけを突破すれば誰でも
# フルシェルを取れてしまっていた(2026-09-13の監査で最重要指摘事項)。ループバック
# (127.0.0.1)限定にバインドしなおし、nginxが/admin/terminal-ws/として同一オリジンで
# プロキシする(river-fog-nginx設定参照)ことで、このアプリの他の管理機能と同じ
# allow/deny・X-Rf-Internalヘッダーの保護下に置いた。admin_terminal()はrequires_admin
# 配下のページなので、そこに到達できる(=既に管理者と確認済みの)閲覧者だけがiframeの
# URLを受け取れる。ttyd自身のBasic認証(ランダム生成したパスワードに変更済み)は
# 最後の防御層として残す。
TTYD_BASE_PATH = "/admin/terminal-ws/"

# CockpitはOS標準でインストール済み(HTTPSのみ、自己署名証明書)。9090番は別オリジンかつ
# X-Frame-Options: sameoriginを送るため、他オリジンからのiframe埋め込みができない
# (自己署名証明書とは別の、ブラウザが強制するより根本的な制約)。nginx(8080番)がCockpitを
# /cockpit/配下に本アプリと同一オリジンでリバースプロキシし、admin_status.htmlはその
# 相対パスをiframeで埋め込んでいる(cockpit.conf側もUrlRoot=/cockpitに設定済み)。

ALERT_HOURS_AHEAD = 24
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
    "experiment": "experiment_model.py",  # 管理者タブ「仮検証」(二値モデル+過去予報)の再計算
    "label_audit": "label_audit.py",  # 管理者タブ「仮検証」のラベル品質ページの再集計
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

CAMERA_HEALTH_DAYS = 7
GEMINI_LOG_DAYS = 14

CHAT_DB_PATH = BASE_DIR / "data" / "chat.db"
# 無料枠のクォータを画像分類・AI要約・エスカレーションと共有しているため、チャット単体に
# 使いすぎないよう1日あたりの上限を設ける(.envで調整可能。詳しくはGemini状況ページ参照)。
CHAT_DAILY_LIMIT_PER_USER = int(os.environ.get("AI_CHAT_DAILY_LIMIT_PER_USER", "10"))
CHAT_DAILY_LIMIT_TOTAL = int(os.environ.get("AI_CHAT_DAILY_LIMIT_TOTAL", "50"))
CHAT_QUESTION_MAX_LENGTH = 300

FEEDBACK_BODY_MAX_LENGTH = 2000
FEEDBACK_SUMMARY_SYSTEM_INSTRUCTION = (
    "あなたは「川霧観測・予測」サイトのコミュニティ意見箱に届いた投稿を、管理者が読みやすいように"
    "整理するアシスタントです。以下の投稿本文を、内容を一切省略・削除せずに、誤字脱字の修正や"
    "文の整理程度にとどめて要約してください。要望・不具合報告・質問など複数の話題が含まれる場合は、"
    "箇条書きで分けて構いません。本文に無い情報を推測で付け加えないでください。"
)

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
# MAX_CONTENT_LENGTHが未設定だと、Werkzeugはリクエストボディ全体をアプリコード実行前に
# メモリへ読み込んでしまう(アップロード先のルート自身のサイズチェックはfile.save()の
# "後"にしか効かない)。全アップロード系エンドポイント(添付ファイル・アバター・
# バックアップ復元等)に共通の上限を設け、巨大POST 1本によるメモリ/ディスク枯渇DoSを
# 防ぐ。最大の正規アップロードは管理者のバックアップ復元(RESTORE_MAX_UPLOAD_BYTES=2GB)
# なので、それに余裕を持たせた値にする(2026-09-13の監査で指摘)。
app.config["MAX_CONTENT_LENGTH"] = 2560 * 1024 * 1024  # 2.5GB


# --- 軽量化(2026-10-14) -------------------------------------------------------------
# 公開URL(Tailscale Funnel)とMagicDNSで直接開いた場合はnginxを通らず、圧縮されないまま送られていた
# (公開ページのHTML 131KB、glass.css 142KB。nginx経由なら glass.css は31KB)。nginxを通らない応答だけ、ここでgzipにする
# (nginx経由はnginxが圧縮する)。CSSは配信のときにコメント・余分な空白を除いて縮める(どの経路でも)。
# Flaskの after_request は登録の逆順に動くので、ここ(他より先)で登録して最後に動かす = 他の処理で作り直した応答も圧縮される。
_COMPRESS_TYPES = ("text/html", "text/css", "text/plain", "text/csv", "application/javascript", "text/javascript",
                   "application/json", "image/svg+xml", "application/manifest+json")
_COMPRESS_MIN_BYTES = 1024
_static_cache = {}            # (パス, ETag, 種類) -> 縮めた/圧縮した中身。静的ファイルは中身が変わればETagが変わる
_static_cache_lock = threading.Lock()
_STATIC_CACHE_MAX = 400


def _minify_css(text):
    """CSSのコメントと余分な空白を除く(意味を変えない範囲だけ: 「:」の前の空白は `.a :hover` のように意味があるので残す)。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([{};,>])\s*", r"\1", text)
    text = re.sub(r":\s+", ":", text)
    return text.replace(";}", "}").strip()


def _static_transform(kind, key, data_fn, fn):
    with _static_cache_lock:
        hit = _static_cache.get((kind,) + key)
    if hit is not None:
        return hit
    out = fn(data_fn())
    with _static_cache_lock:
        if len(_static_cache) >= _STATIC_CACHE_MAX:
            _static_cache.clear()
        _static_cache[(kind,) + key] = out
    return out


@app.after_request
def _compress_response(response):
    if response.status_code != 200 or request.method == "HEAD" or "Content-Encoding" in response.headers:
        return response
    mimetype = response.mimetype or ""
    is_static = request.endpoint == "static"
    etag = response.get_etag()[0] if is_static else None
    # CSSを縮める(静的ファイルの .css。既に縮めてある .min.css はそのまま)
    if is_static and mimetype == "text/css" and not request.path.endswith(".min.css") and etag:
        response.direct_passthrough = False
        try:
            body = _static_transform("css", (request.path, etag), lambda: response.get_data(),
                                     lambda b: _minify_css(b.decode("utf-8")).encode("utf-8"))
            response.set_data(body)
        except Exception as e:  # noqa: BLE001
            log.warning("CSS minify failed for %s: %s", request.path, e)
    if request.headers.get("X-Rf-Internal") == _NGINX_INTERNAL_HEADER_SECRET:
        return response  # nginx経由: 圧縮はnginxがする
    if "gzip" not in request.headers.get("Accept-Encoding", "").lower() or request.headers.get("Range"):
        return response
    if not mimetype.startswith(_COMPRESS_TYPES) or mimetype.startswith("text/event-stream"):
        return response
    if response.is_streamed and not is_static:
        return response
    response.direct_passthrough = False
    try:
        if is_static and etag:
            body = _static_transform("gz", (request.path, etag), lambda: response.get_data(),
                                     lambda b: gzip.compress(b, compresslevel=6))
        else:
            data = response.get_data()
            if len(data) < _COMPRESS_MIN_BYTES:
                return response
            body = gzip.compress(data, compresslevel=5)
    except Exception as e:  # noqa: BLE001
        log.warning("gzip failed for %s: %s", request.path, e)
        return response
    response.set_data(body)
    response.headers["Content-Encoding"] = "gzip"
    response.vary.add("Accept-Encoding")
    if etag:
        response.set_etag(etag + "-gz")
    return response

# 外部から読み込んでいるのは地図ページのLeaflet(cdnjs.cloudflare.com)と
# OpenStreetMapのタイル画像(*.tile.openstreetmap.org)のみ(把握済み・確認済み)。
# 管理者ターミナル(admin_terminal.html)は以前http://<host>:TTYD_PORTを別オリジンの
# iframeとして埋め込んでいたが、ttydをループバック限定にしnginx配下の同一オリジン
# (/admin/terminal-ws/)でプロキシする方式に変更したため、frame-srcを別途許可する
# 必要が無くなった(2026-09-13の監査で指摘、admin_terminal()参照)。
def _chart_json(fig):
    """グラフをJSONにする。サイト共通の見た目(chart_style.style_figure)を通してから変換する。"""
    return pio.to_json(chart_style.style_figure(fig))


_SECURITY_HEADERS_CSP = (
    "default-src 'self'; "
    # 公開ページの地図(2026-10-06〜)は国土地理院の淡色地図のタイル(cyberjapandata.gsi.go.jp)を使う。
    "img-src 'self' data: https://*.tile.openstreetmap.org https://cyberjapandata.gsi.go.jp; "
    "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
    "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
    "font-src 'self' https://cdnjs.cloudflare.com; "
    "frame-src 'self'; "
    "frame-ancestors 'self'; "
    "object-src 'none'; "
    "base-uri 'self'"
)
_PERMISSIONS_POLICY = ("camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=(), "
                       "midi=(), magnetometer=(), gyroscope=(), accelerometer=(), browsing-topics=()")


@app.after_request
def _apply_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Content-Security-Policy", _SECURITY_HEADERS_CSP)
    # 2026-10-14: このサイトで使わない端末の機能(カメラ・マイク・位置など)は、万一埋め込まれたスクリプトからも使えないようにする。
    # 別のサイトの窓から window.opener でこのページを操作されないようにする(COOP)。
    # HSTSは付けない: 公開URLと同じホスト名で研究用サイト(:8443、自前の証明書)を開くことがあり、
    # HSTSがあると証明書の警告を越えられず研究用サイトが開けなくなるため。
    response.headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if request.path == "/game/shop":
        # ショップ画面は所持ポイント・装着中アイテムをJSでその場更新するため、ブラウザの
        # bfcache(戻る/進むでの復元)やHTTPキャッシュに古いDOM/HTMLのまま残ると、
        # 別タブや別セッションで装備を切り替えた後も「装着中」表示が古いままになりうる。
        # 必ず毎回サーバーから取り直させることでこのズレを防ぐ。
        response.headers["Cache-Control"] = "no-store"
    if request.endpoint == "static":
        # 静的ファイルはFlaskの既定(no-cache)だと、ページを開くたびにブラウザが全ファイルを
        # 問い合わせ直す。日本語フォントは細かく分割された数十個のwoff2なので、1回の表示で
        # 約90件の問い合わせが一度に来て、waitressの処理待ちが詰まっていた(「Task queue depth
        # is 94」)。中身がまず変わらないフォント・外部ライブラリ・アイコンは30日、それ以外
        # (自前のCSS/JS、manifest)は更新が反映されるまでの遅れを抑えて10分キャッシュさせる。
        filename = (request.view_args or {}).get("filename", "")
        long_lived = filename.startswith(STATIC_LONG_CACHE_DIRS)
        response.headers["Cache-Control"] = (f"public, max-age={STATIC_LONG_CACHE_SECONDS}" if long_lived
                                             else f"public, max-age={STATIC_SHORT_CACHE_SECONDS}")
    return response


STATIC_LONG_CACHE_DIRS = ("fonts/", "vendor/", "icons/")
STATIC_LONG_CACHE_SECONDS = 30 * 24 * 3600
STATIC_SHORT_CACHE_SECONDS = 600


# グラフは画像(matplotlib)で表示する(2026-10-07〜)。各グラフのURLはPlotlyのfigureのJSONを返すままにして、
# ?format=pngが付いたときだけ、ここでmpl_renderが画像に描き直して返す(全グラフ共通。テーマ・幅・倍率・
# タイトルの有無は引数で受け取る)。2026-09-14からのPlotly(拡大・移動できるグラフ)は、触ると拡大・移動して
# しまい使いづらかったため。この関数は_apply_security_headersより後に登録しているので先に動き、
# 作り直した画像の応答にもセキュリティヘッダーが付く。
@app.after_request
def _chart_as_png(response):
    if request.args.get("format") != "png" or response.status_code != 200 or response.direct_passthrough:
        return response
    try:
        body = response.get_data(as_text=True)
    except Exception:
        return response
    if not body.lstrip().startswith("{"):
        return response
    try:
        fig = json.loads(body)
    except ValueError:
        return response
    if not isinstance(fig, dict) or not ("data" in fig or "layout" in fig):
        return response
    try:
        width = int(request.args.get("w", 900))
    except ValueError:
        width = 900
    try:
        scale = float(request.args.get("scale", 1))
    except ValueError:
        scale = 1.0
    png = mpl_render.render_cached(fig, theme="dark" if request.args.get("theme") == "dark" else "light",
                                   width=width, scale=scale, notitle=request.args.get("notitle") == "1")
    out = Response(png, mimetype="image/png")
    out.headers["Cache-Control"] = "private, max-age=600"
    return out

try:
    STATION_NAMES = fog_data.load_station_names()
except Exception:
    STATION_NAMES = []

try:
    STATION_REGION = load_camera_regions()
except Exception:
    STATION_REGION = {}
# 只見川.xlsx/会津.xlsxのダウンロードや、地点一覧・観測アーカイブ等サイト全体での
# 地域ごとの表示分けに使う、地点名の部分リスト(2026-09の会津地方追加で導入)。
TADAMI_STATION_NAMES = [n for n in STATION_NAMES if STATION_REGION.get(n) == "只見川"]
AIZU_STATION_NAMES = [n for n in STATION_NAMES if STATION_REGION.get(n) == "会津"]
_STATION_NAMES_BY_REGION = {"只見川": TADAMI_STATION_NAMES, "会津": AIZU_STATION_NAMES}


@app.context_processor
def _inject_station_names_by_region():
    """全テンプレートで地点を只見川/会津に分けて表示できるよう、共通で注入する
    (個々のrender_templateで毎回渡さずに済むようにするため)。station_regionは
    逆引き(地点名→地域)、station_names_by_regionは正引き(地域→地点名リスト)。"""
    return {"station_names_by_region": _STATION_NAMES_BY_REGION, "station_region": STATION_REGION}


# --- ロール: 一般(general)/管理者(admin)に加え、観測報告専用のロールを持つ ---------
# 会津観測者/只見観測者は、観測報告の確認待ちキュー(管理者と共有、担当地域分のみ)を
# 見て訂正・却下でき、かつ地点別ページから承認待ちを介さずその場で観測結果を直接
# 訂正できる(一般ユーザーは「報告」のみ)。観測者(region無し)は同じ権限を
# 地域を問わず両方に対して持つ(=観測報告に関しては管理者と同等)。
# これら3ロールはいずれもそれ以外の管理機能(アカウント管理・DB操作等)には
# 一切関与しない、observation_reports専用の限定ロール(@requires_adminは通せない)。
OBSERVER_ROLE_REGIONS = {"aizu_observer": "会津", "tadami_observer": "只見川"}
OBSERVATION_ROLES = ("admin", "observer", *OBSERVER_ROLE_REGIONS)
ROLE_LABELS = {
    "admin": "管理者", "general": "一般", "observer": "観測者",
    "aizu_observer": "会津観測者", "tadami_observer": "只見観測者",
}


def _observer_region_for_role(role):
    """地域を絞らない(=全地域を見る)ロールならNoneを返す(管理者・観測者)。
    会津観測者/只見観測者なら担当地域、観測権限が無ければNoneを返す(呼び出し側で
    _can_manage_observation_reportsと組み合わせて使うこと)。"""
    return OBSERVER_ROLE_REGIONS.get(role)


def _can_manage_observation_reports(role):
    return role in OBSERVATION_ROLES


def _enforce_observer_region(station_name):
    """requires_observerと組み合わせて使う: 会津観測者/只見観測者は担当地域外の地点への
    操作を403で拒否する(管理者・観測者は地域を問わず常に許可)。地域チェックをここに
    集約することで、新しいrequires_observerルートを追加する際にチェックを書き忘れる、
    あるいは微妙に間違った条件を書いてしまうリスクを減らす(2026-09-13の監査で、
    requires_observer自体は地域を絞らない設計のため各ルートでの手動チェックに依存して
    いる点が将来的な権限昇格リスクとして指摘された)。"""
    region = _observer_region_for_role(session.get("role"))
    if region is not None and STATION_REGION.get(station_name) != region:
        abort(403)


def _can_correct_station(role, station_name):
    """その地点の観測結果をその場で直接訂正してよいか(管理者・観測者は常に可、
    会津観測者/只見観測者は担当地域の地点のみ、一般は不可)。"""
    if role in ("admin", "observer"):
        return True
    region = OBSERVER_ROLE_REGIONS.get(role)
    return region is not None and STATION_REGION.get(station_name) == region


@app.context_processor
def _inject_role_label():
    return {"role_label": lambda role: ROLE_LABELS.get(role, role)}


@app.context_processor
def _inject_can_correct_station():
    return {"can_correct_station": lambda station_name: _can_correct_station(session.get("role"), station_name)}

# メンテナンスモード中でも管理者(/admin配下)だけは常にアクセスできるようにする
# （でないと自分でオンにしたら誰もオフに戻せなくなる）。/loginを例外にしているのは、
# セッションが無い状態では「管理者かどうか」を判定できず、管理者自身がログインする
# 手段が無くなってしまうため。/signupはLOGIN_EXEMPT_PREFIXESと違いここには意図的に
# 含めていない: メンテナンス中の新規登録申請は、管理者が行っている作業(DBの移行等)と
# 競合しうるため止めておきたい(この非対称はバグではなく意図した挙動)。
MAINTENANCE_EXEMPT_PREFIXES = ("/admin", "/web_static", "/login", "/logout")
# サイト全体をログイン必須にする。ログイン・登録申請そのものと静的ファイルだけは例外。
# /forecastは一般向けの公開ページ(_build_public_forecast参照)。
PUBLIC_PREFIX = "/forecast"
LOGIN_EXEMPT_PREFIXES = ("/login", "/signup", "/logout", "/web_static", PUBLIC_PREFIX, "/api/backup-manifest",
                         "/offline-sw.js")  # 2026-10-14: やめた仕組みを、ログアウト中のブラウザからも解除できるように(中身は自分を解除するだけのJS)

# Tailscale Funnelで一般公開しているホスト名。nginx(8443)経由のTailscale内アクセスとは
# 別に、tailscaledがこのホスト名宛のリクエストだけを直接127.0.0.1:8081へ転送してくる。
# 以前はHostヘッダーの値(funnel経由は必ずこのホスト名になる)でfunnel判定していたが、
# MagicDNSのホスト名(この値と同じ)を使ってTailscale内から直接ブラウザでアクセスした
# 場合もHostが同じ値になってしまい、正規のTailscale内アクセスなのにfunnel扱いされて
# 管理者機能が消える誤判定があった(2026-09-11に発覚)。Hostヘッダーはリクエストする側が
# 自由に指定できる値なので、そもそも判定に使うべきではなかった。
# 代わりに、nginx側だけが付与できる秘密のヘッダー(X-Rf-Internal、nginx設定ファイル参照)
# の有無で判定する。Funnelはnginxを経由せずtailscaledから直接127.0.0.1:8081へ転送される
# ため、このヘッダーは絶対に付かない(公開のFunnel経由リクエストが自分でこのヘッダーを
# 偽装しても、値が一致しなければfunnel扱いのままになる)。
def _load_or_create_nginx_internal_secret():
    """nginx専用ヘッダーの合言葉を.envから読む。無ければ生成して.envに書き込み、以後は固定する。
    リポジトリを公開GitHubにpushする際にコードへの直接埋め込みが発覚したため
    (2026-09-11)、SECRET_KEYと同じ自己永続化パターンに切り替えた。nginx側の設定
    (/etc/nginx/sites-available/river-fog、Gitでは管理していない)にも同じ値を
    手動で設定する必要がある。"""
    existing = os.environ.get("NGINX_INTERNAL_HEADER_SECRET")
    if existing:
        return existing
    new_secret = secrets.token_hex(24)
    try:
        with open(ENV_PATH, "a", encoding="utf-8") as f:
            f.write(f"\nNGINX_INTERNAL_HEADER_SECRET={new_secret}\n")
        os.environ["NGINX_INTERNAL_HEADER_SECRET"] = new_secret
    except Exception:
        log.exception("Failed to persist NGINX_INTERNAL_HEADER_SECRET to .env; using an in-memory value for this run")
    return new_secret


_NGINX_INTERNAL_HEADER_SECRET = _load_or_create_nginx_internal_secret()


def _is_funnel_request():
    if request.headers.get("X-Rf-Internal") == _NGINX_INTERNAL_HEADER_SECRET:
        return False
    # tailscale funnelは同じホスト名(sshserver.taile43041.ts.net)のポート443を
    # 使い回すため、nginxを経由しない(MagicDNS等で直接443番にアクセスした)リクエストは
    # 上のX-Rf-Internalだけでは「正規のtailnet内アクセス」と「公開funnel経由」を区別
    # できない(2026-09-11、MagicDNS経由だと観測報告タブが消えるという形で発覚)。
    # tailscaledは認証済みのtailnetメンバーからのリクエストにのみTailscale-User-Login等の
    # 身元ヘッダーを付与し(クライアントが自分で付けても必ず上書き・削除されるため偽装
    # 不可能。仕様: https://tailscale.com/s/serve-headers)、身元を特定できない公開funnel
    # 経由の匿名リクエストにはこのヘッダーが付かない。これを内部アクセス判定に使う。
    if request.headers.get("Tailscale-User-Login"):
        return False
    return True


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
    # nginxのauth_request(admin_auth_check)はリダイレクトを扱えないので、未ログイン・
    # 削除済みアカウントは401で返す。
    is_auth_check = request.path == "/admin/auth-check"
    if not user_id:
        if is_auth_check:
            return "", 401
        # 未ログインでトップに来た人(外部の人)は、ログイン画面ではなく公開ページに案内する。
        if request.path == "/":
            return redirect(url_for("public_forecast"))
        return redirect(url_for("login", next=request.path))
    conn = _users_conn()
    try:
        user = users.get_user_by_id(conn, user_id)
        if user is not None:
            g.user_settings = user_settings.normalize(users.get_user_settings_raw(conn, user_id), STATION_NAMES)
            # 「オンライン状態を表示しない」設定(設定画面)の人は、最終アクセス時刻を記録しない
            if g.user_settings["show_online"]:
                _touch_presence_if_due(conn, user_id)
    finally:
        conn.close()
    if user is None:
        session.clear()
        if is_auth_check:
            return "", 401
        flash("アカウントが削除されたため、ログアウトしました。")
        return redirect(url_for("login", next=request.path))
    session["role"] = user["role"]
    # 通知の絞り込み(お気に入り地点)等、同じリクエスト内で再度ユーザー情報が必要になる
    # 場合に備えてgに載せておく(_current_user_favorite_stations参照、二重問い合わせ防止)。
    g.current_user = user
    return None


# 利用規約(2026-10-13、公開ページだけ)。文面は web_templates/_terms_body.html。文面を大きく変えたら版(日付)を上げると、
# 公開ページで改めて同意の画面が出る。研究用サイト(ログインする方)では同意を求めない(2026-10-13、利用者の判断)。
TERMS_VERSION = "2026-10-13"


@app.context_processor
def _inject_terms_version():
    return {"terms_version": TERMS_VERSION}

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
CSRF_EXEMPT_PREFIXES = ("/web_static", "/api/backup-manifest")  # backup-manifestはセッションではなくトークンで確かめる


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

# 2026-09-14、予測グラフをPlotly(.html)に移行したが、移行前の過去アーカイブは
# 今後もずっと.pngのまま残る。.htmlだけにマッチするよう単純に切り替えると、過去の
# 日付の予測グラフが一覧から消えてしまう(実質的なデータ消失に見える)ため、
# 両方にマッチするよう拡張しておくこと。
STATION_FORECAST_RE = re.compile(r"^(.+)_予測_\d{8}\.(png|html)$")
SUMMARY_RE = re.compile(r"^全地点サマリー_\d{8}\.(png|html)$")
SUMMARY_REGION_RE = {
    "只見川": re.compile(r"^全地点サマリー_只見_\d{8}\.(png|html)$"),
    "会津": re.compile(r"^全地点サマリー_会津_\d{8}\.(png|html)$"),
}
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


def _phenom_label_color(value):
    """現象コード(単一0〜10、2コード同時観測のパック済み値、またはNaN)をラベル・
    背景色・文字色に変換する。背景色は主コードの色のまま(2色分割はスコープ外、
    シンプルに保つ)、ラベルはphenom_label()で主・副コードを結合したものになる。"""
    primary, _secondary = decode_phenom_value(value)
    if primary is None:
        return "-", "#ffffff", "#1c2733"
    if primary == 0:
        return "現象なし", SLASH_COLOR, _contrasting_text_color(SLASH_COLOR)
    bg = PHENOM_COLORS.get(primary, "#ffffff")
    return phenom_label(value), bg, _contrasting_text_color(bg)


# 現象コード訂正UI(phenom-picker、station_detail.html/observation_reports.html)が
# ボタンの背景色・文字色を、ダッシュボード等で使っているのと同じ実際の色でそのまま
# 出せるように、コード0〜10分をまとめて渡す。
PHENOM_STYLE = {
    code: dict(zip(("label", "bg", "text"), _phenom_label_color(code)))
    for code in range(11)
}


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


def _load_lens_haze_stations():
    """cameras.csvの「レンズ曇り」列が付いた地点(レンズ自体が常にわずかに曇っているカメラ)。
    これらの地点は画像全体がかすみ、「全体霧」と判定されやすい(日中の全体霧の割合が他地点の約3%に
    対して約27%)。川霧のラベルは隣接地点並みで、学習から外すとかえって精度が下がる
    (data/experiments/sun_lens_wind_check.json)ため学習には使い、画面で注意書きを出す。"""
    try:
        with open(CAMERAS_CSV, encoding="utf-8") as f:
            return {row["地点名"] for row in csv.DictReader(f) if (row.get("レンズ曇り") or "").strip()}
    except Exception:
        return set()


LENS_HAZE_STATIONS = _load_lens_haze_stations()


@app.context_processor
def _inject_lens_haze():
    return {"lens_haze_stations": LENS_HAZE_STATIONS}


def _river_go_jp_map_url(cam_id):
    """川の防災情報(国交省 river.go.jp)の地点別地図URLを組み立てる。
    座標データが無いため独自の地図は描けないが、river.go.jpは各カメラIDに対応する
    地図ページを持っており、緯度経度なしでカメラIDのみから直接リンクできる。
    以下の導出方法は只見川側のカメラID(101793163, 101793203)と、2026-09に追加した
    会津地方側のカメラID(121527024, 221527001等)をあわせてriver.go.jp側の
    インデックス済みURLを目視確認して検証済み(推測ではない):
    カメラID(scamId)の先頭は種別を表す1〜2桁のマーカーで、
    - "10"で始まる場合: itmkndCd=200、sysCamIdはマーカー2桁を除いた残り
    - "1"で始まり2桁目が0でない場合: itmkndCd=200、sysCamIdはマーカー1桁を除いた残り
    - "2"で始まる場合: itmkndCd=100、sysCamIdはマーカー1桁を除いた残り
    ownCdは自治体コードで、sysCamIdの末尾3桁を除いた部分と一致する。
    """
    if cam_id.startswith("10"):
        itmknd_cd, sys_cam_id = "200", cam_id[2:]
    elif cam_id.startswith("1"):
        itmknd_cd, sys_cam_id = "200", cam_id[1:]
    else:
        itmknd_cd, sys_cam_id = "100", cam_id[1:]
    own_cd = sys_cam_id[:-3]
    return (
        "https://www.river.go.jp/kawabou/mb/tm"
        f"?zm=15&scamId={cam_id}&ownCd={own_cd}&itmkndCd={itmknd_cd}&sysCamId={sys_cam_id}"
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


# --- 日付の一覧(カレンダー・前後の日への移動・Ctrl+Kの日付表示用) -----------------------------------
# 観測データの日付は全期間のDataFrameを作らずにSQLiteから直接引き、DBの更新時刻が変わるまで使い回す。
_OBS_DATES_CACHE = {"mtime": None, "dates": []}
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _observation_dates():
    """観測データのある日付(昇順)。"""
    try:
        mtime = WEATHER_DB_PATH.stat().st_mtime
    except OSError:
        return []
    if _OBS_DATES_CACHE["mtime"] != mtime:
        conn = sqlite3.connect(f"file:{WEATHER_DB_PATH}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT DISTINCT substr(datetime, 1, 10) FROM weather_observations ORDER BY 1").fetchall()
        finally:
            conn.close()
        _OBS_DATES_CACHE.update(mtime=mtime, dates=[r[0] for r in rows if r[0] and _DATE_DIR_RE.match(r[0])])
    return list(_OBS_DATES_CACHE["dates"])


def _available_dates(kind):
    """kind("observations" / "images" / "predictions")のデータがある日付(昇順)。"""
    if kind == "observations":
        return _observation_dates()
    base = {"images": IMAGE_DIR, "predictions": PRED_DIR}.get(kind)
    if base is None:
        return []
    return [d for d in _sorted_subdirs(base, reverse=False) if _DATE_DIR_RE.match(d)]


@app.template_filter("weekday_index")
def _weekday_index(date_str):
    """"YYYY-MM-DD"の曜日(月曜=0)。日付として読めなければNone。"""
    try:
        return datetime.strptime(str(date_str), "%Y-%m-%d").weekday()
    except ValueError:
        return None


def _adjacent_dates(kind, date):
    """dateの前後で、データがある日付 (前, 次)。無ければNone。"""
    dates = _available_dates(kind)
    i = bisect.bisect_left(dates, date)
    prev_date = dates[i - 1] if i > 0 else None
    j = i + 1 if i < len(dates) and dates[i] == date else i
    next_date = dates[j] if j < len(dates) else None
    return prev_date, next_date


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
    """トップバーの「観測報告」に未対応件数のバッジを出すため注入する。管理者・観測者は
    全件、会津観測者/只見観測者は担当地域分だけをカウントする(それ以外のロールには
    出さない)。"""
    def pending_observation_report_count():
        role = session.get("role")
        if not _can_manage_observation_reports(role):
            return 0
        conn = _users_conn()
        try:
            if role in ("admin", "observer"):
                return users.pending_observation_report_count(conn)
            region = _observer_region_for_role(role)
            return users.pending_observation_report_count_for_stations(
                conn, _STATION_NAMES_BY_REGION.get(region, []),
            )
        finally:
            conn.close()
    return {"pending_observation_report_count": pending_observation_report_count}


@app.context_processor
def _inject_pending_feedback_report_count():
    """管理者サイドバーの「意見箱」に未対応件数のバッジを出すため注入する。"""
    def pending_feedback_report_count():
        if session.get("role") != "admin":
            return 0
        conn = _users_conn()
        try:
            return users.pending_feedback_report_count(conn)
        finally:
            conn.close()
    return {"pending_feedback_report_count": pending_feedback_report_count}


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
    """通知ベル(base.html)の一覧。一般利用者には川霧アラート+カメラ故障の兆候、管理者には
    それに加えてシステム状態の通知を加える。通知はDBに記録された履歴ではなく、その時点の
    状態から毎回計算するだけ（川霧アラートは_upcoming_fog_alerts_safe()、システム状態は
    _admin_overview()を再利用）。既読管理はサーバー側に持たず、base.htmlのJSがlocalStorageで行う。

    お気に入り地点を設定しているユーザーには、川霧アラート・カメラ故障通知の両方をお気に入り
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


# ============================================================================
# 個人設定(/settings)とWeb Push(2026-10-09)
# 設定項目の定義はuser_settings.py、通知の送信はpush_notify.py。表示の設定(テーマ・文字の大きさ・
# 動きを減らす等)はbase.htmlの<head>で描画前に当てる。
# ============================================================================
push_notify.init(USERS_DB_PATH, now_jst_naive)

# 購読先(endpoint)として受け付けるプッシュサービス。サーバーがこのURLへPOSTするため、任意のURLを
# 登録させると内部ネットワーク等へリクエストを送らせる踏み台(SSRF)にされてしまう。
PUSH_ENDPOINT_HOST_RE = re.compile(
    r"^(fcm\.googleapis\.com|android\.googleapis\.com|updates\.push\.services\.mozilla\.com"
    r"|[a-z0-9-]+\.push\.services\.mozilla\.com|web\.push\.apple\.com|[a-z0-9-]+\.push\.apple\.com"
    r"|[a-z0-9-]+\.notify\.windows\.com)$"
)
PUSH_KEY_RE = re.compile(r"^[A-Za-z0-9_\-=]{8,200}$")
# 正規の証明書のあるURL(Tailscale Funnel)。Service Worker・Web Pushは証明書が正しいHTTPSでしか動かず、
# :8443(自己署名の証明書)で開いている人にはこちらで開き直すよう案内する。
PUBLIC_SITE_URL = push_notify.VAPID_SUB
SETTINGS_SECTION_LABELS = {"display": "表示", "privacy": "プライバシー", "push": "通知", "menu": "メニュー"}


def _current_user_settings():
    """ログイン中の利用者の設定(未ログインなら既定値)。_require_loginがgに載せたものを使う。"""
    s = g.get("user_settings")
    if s is None:
        s = user_settings.defaults()
        user_id = session.get("user_id")
        if user_id:
            conn = _users_conn()
            try:
                s = user_settings.normalize(users.get_user_settings_raw(conn, user_id), STATION_NAMES)
            finally:
                conn.close()
        g.user_settings = s
    return s


def _save_user_settings(user_id, new_settings):
    conn = _users_conn()
    try:
        users.save_user_settings(conn, user_id, json.dumps(new_settings, ensure_ascii=False),
                                 now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"))
        if not new_settings["show_online"]:
            users.clear_presence(conn, user_id)
    finally:
        conn.close()
    g.user_settings = new_settings


def _start_page_url(settings, favorite_stations_csv):
    """設定の「最初に開くページ」のURL。ダッシュボードのままならNone。"""
    page = settings.get("start_page", "dashboard")
    if page == "stations":
        return url_for("stations_list")
    if page == "map":
        return url_for("map_page")
    if page == "public":
        return url_for("public_forecast")
    if page == "favorite":
        favorites = [s for s in (favorite_stations_csv or "").split(",") if s in STATION_NAMES]
        return url_for("station_detail", name=favorites[0]) if favorites else url_for("stations_list")
    return None


@app.context_processor
def _inject_user_prefs():
    def user_prefs():
        if not session.get("user_id"):
            return None
        try:
            return _current_user_settings()
        except Exception as e:
            log.warning("Failed to load user settings: %s", e)
            return None
    return {"user_prefs": user_prefs}


def _device_label(ua):
    """購読した端末の表示名(設定画面の「通知を受け取る端末」の一覧用)。User-Agentからおおまかに作る。"""
    ua = ua or ""
    os_name = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua
               else "Windows" if "Windows" in ua else "Mac" if "Macintosh" in ua else "Linux" if "Linux" in ua else "端末")
    browser = ("Edge" if "Edg/" in ua else "Firefox" if "Firefox/" in ua else "Chrome" if "Chrome/" in ua
               else "Safari" if "Safari/" in ua else "ブラウザ")
    return f"{os_name}・{browser}"


@app.route("/settings")
def settings_page():
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    conn = _users_conn()
    try:
        me = users.get_user_by_id(conn, my_id)
        devices = users.list_push_subscriptions(conn, my_id)
    finally:
        conn.close()
    try:
        vapid_key = push_notify.vapid_public_key()
    except Exception as e:
        log.warning("Failed to load the VAPID key: %s", e)
        vapid_key = None
    favorites = [s for s in me["favorite_stations"].split(",") if s]
    return render_template(
        "settings.html", s=_current_user_settings(), devices=devices, vapid_key=vapid_key,
        station_names=STATION_NAMES, station_region=STATION_REGION, favorite_stations=favorites,
        is_admin=me["role"] == "admin", hours=user_settings.HOUR_CHOICES,
        public_url=PUBLIC_SITE_URL,
    )


# --- サイト全体のメニュー(2026-10-13にbase.htmlから移した) ------------------------------------------------
# 項目は1か所(_app_nav_items)で決め、PCの左/上のメニュー、狭い画面の「メニュー」のシート、設定画面の「メニュー」で共通に使う。
# 利用者は設定画面で、項目の表示/非表示と並び(nav_hidden / nav_order)、PCでの場所(nav_position: 左/上)を選べる。
NAV_ALWAYS_SHOWN = {"settings"}  # 設定に戻れなくならないよう、これだけは隠せない


def _app_nav_items(obs_pending=0, community_unread=0):
    """(id, グループ名, 表示名, URL, アイコン, 開いているか, 数, 数の種類, シートだけ)の並び(最初の並び)。役割で見えないものは含めない。"""
    p = request.path
    role = session.get("role")
    logged_in = bool(session.get("user_id"))
    obs_role = _can_manage_observation_reports(role)
    analysis = (p.startswith("/graphs") or p.startswith("/river-fog-histogram") or p.startswith("/accuracy")
                or (p.startswith("/report") and not p.startswith("/reports")))
    # 2026-10-13: 項目が増えすぎたため、同じページのタブで行き来できるものを1つにまとめた(19→12)。
    #   地点 = 地点一覧・比較・地図・今朝の川霧・流れ図/見頃、分析 = 気象と川霧〜期間レポート・月ごとのレポート、
    #   データ = 観測データ・カメラ画像・予測結果・書き出し、観測の確認 = 観測報告・正解セット。
    #   更新履歴とログアウトはヘッダーにあるので、狭い画面の「メニュー」のシートにだけ出す。
    stations_paths = ("/stations", "/compare", "/map", "/gallery", "/flow")
    data_paths = ("/observations", "/images", "/predictions", "/export")
    items = [
        ("home", None, "ホーム", url_for("dashboard"), "dashboard", p == url_for("dashboard")),
        ("stations", "観測", "地点", url_for("stations_list"), "stations", p.startswith(stations_paths)),
    ]
    if obs_role:
        items.append(("review", "観測", "観測の確認", url_for("observation_reports"), "error",
                      p.startswith("/observation_reports") or p.startswith("/gold"), obs_pending))
    items += [
        ("public", "予報・分析", "予報(公開ページ)", url_for("public_forecast"), "forecast", False),
        ("simulator", "予報・分析", "シミュレーター", url_for("simulator_page"), "simulator", p.startswith(url_for("simulator_page"))),
        ("analysis", "予報・分析", "分析", url_for("weather_fog_graph_page"), "accuracy", analysis or p.startswith("/reports")),
        ("data", "予報・分析", "データ", url_for("observations_dates"), "data", p.startswith(data_paths)),
    ]
    if logged_in:
        items += [("members", "コミュニティ", "メンバー・メッセージ", url_for("members_list"), "members",
                   p.startswith("/members") or p.startswith("/messages") or p.startswith("/files") or p.startswith("/group"),
                   community_unread, "community"),
                  ("game", "コミュニティ", "ゲーム", url_for("game_home"), "game", p.startswith("/game")),
                  ("chat", "コミュニティ", "AIチャット", url_for("chat_page"), "chat", p.startswith(url_for("chat_page"))),
                  ("settings", "その他", "設定", url_for("settings_page"), "settings", p.startswith("/settings"))]
    if role == "admin" and not _is_funnel_request():
        items.append(("admin", "その他", "管理者", url_for("admin_home"), "admin", p.startswith("/admin")))
    items.append(("changelog", "その他", "更新履歴", url_for("changelog_page"), "log", p.startswith(url_for("changelog_page")), 0, None, True))
    if logged_in:
        items.append(("logout", "その他", "ログアウト", url_for("logout"), "close", False, 0, None, True))
    out = []
    for it in items:
        it = tuple(it) + (0, None, False)[len(it) - 6:]
        out.append({"id": it[0], "group": it[1], "label": it[2], "url": it[3], "icon": it[4], "active": it[5],
                    "badge": it[6], "badge_key": it[7], "sheet_only": it[8]})
    return out


def _app_nav_ordered(items, prefs):
    """利用者の並び(nav_order)に並べ替える。並びに無い項目(新しく増えた項目など)は、最初の並びの位置のまま後ろに付く。"""
    order = (prefs or {}).get("nav_order") or []
    rank = {k: i for i, k in enumerate(order)}
    base = {it["id"]: i for i, it in enumerate(items)}
    return sorted(items, key=lambda it: (rank.get(it["id"], len(order) + base[it["id"]]),))


# 狭い画面の下のタブ(2026-10-13): 左右の「ホーム」「メニュー」は固定で、真ん中の3つを設定で選べる。
# 候補はメニューの項目に、よく使うページへの近道(見頃・今朝の川霧・地図)を足したもの。
TABBAR_SHORTCUTS = [
    ("flow_fc", "見頃", "flow_page", {"mode": "fc"}, "calendar", ("/flow",)),
    ("gallery", "今朝の川霧", "gallery_page", {}, "sunrise", ("/gallery",)),
    ("map", "地図", "map_page", {}, "map", ("/map",)),
]
TABBAR_SHORT_LABELS = {"stations": "地点", "public": "予報", "review": "確認", "members": "メンバー", "chat": "AIチャット",
                       "simulator": "シミュ", "changelog": "更新履歴"}


def _tabbar_candidates(nav_items):
    """[{id, label(タブ用の短い名前), full(設定画面用), url, icon, active}]。"""
    p = request.path
    out = [{"id": it["id"], "label": TABBAR_SHORT_LABELS.get(it["id"], it["label"]), "full": it["label"], "url": it["url"],
            "icon": it["icon"], "active": it["active"]} for it in nav_items if not it["sheet_only"] and it["id"] != "home"]
    for sid, label, endpoint, kwargs, icon, prefixes in TABBAR_SHORTCUTS:
        out.append({"id": sid, "label": label, "full": f"{label}(近道)", "url": url_for(endpoint, **kwargs), "icon": icon,
                    "active": p.startswith(prefixes)})
    return out

@app.context_processor
def _inject_app_nav():
    def app_nav_menu(obs_pending=0, community_unread=0):
        """メニューに出すグループと項目。隠した項目を除き、利用者の並びにする。グループは、その中で最初に来る項目の順。"""
        prefs = _current_user_settings() if session.get("user_id") else {}
        hidden = set(prefs.get("nav_hidden") or []) - NAV_ALWAYS_SHOWN
        items = [it for it in _app_nav_ordered(_app_nav_items(obs_pending, community_unread), prefs) if it["id"] not in hidden]
        groups = []
        for it in items:
            g_ = next((g for g in groups if g["title"] == it["group"]), None)
            if g_ is None:
                g_ = {"title": it["group"], "items": []}
                groups.append(g_)
            g_["items"].append(it)
        return groups

    def app_nav_catalog():
        """設定画面用: いまの並びの全項目(シートだけの項目を除く)と、表示しているか。"""
        prefs = _current_user_settings() if session.get("user_id") else {}
        hidden = set(prefs.get("nav_hidden") or []) - NAV_ALWAYS_SHOWN
        items = _app_nav_items()
        default_index = {it["id"]: i for i, it in enumerate(items)}
        return [dict(it, shown=it["id"] not in hidden, locked=it["id"] in NAV_ALWAYS_SHOWN, default_index=default_index[it["id"]])
                for it in _app_nav_ordered(items, prefs) if not it["sheet_only"]]

    def tabbar_items():
        """下のタブの真ん中の3つ(設定の tabbar_items。見られない項目は飛ばし、足りなければ既定で埋める)。"""
        prefs = _current_user_settings() if session.get("user_id") else {}
        cands = {c["id"]: c for c in _tabbar_candidates(_app_nav_items())}
        chosen = [cands[i] for i in (prefs.get("tabbar_items") or []) if i in cands]
        for i in user_settings.SETTINGS["tabbar_items"]["default"]:
            if len(chosen) >= 3:
                break
            if i in cands and cands[i] not in chosen:
                chosen.append(cands[i])
        if chosen:
            # 同じページで2つ光らないよう、開いているページに当てはまる最初の1つだけを光らせる
            # 近道(見頃・今朝の川霧・地図)は「地点」より絞った場所なので、両方当てはまるときは近道のほうを光らせる
            shortcut_ids = {s[0] for s in TABBAR_SHORTCUTS}
            lit = (next((c["id"] for c in chosen if c["active"] and c["id"] in shortcut_ids), None)
                   or next((c["id"] for c in chosen if c["active"]), None))
            chosen = [dict(c, active=c["id"] == lit) for c in chosen]
        return chosen[:3]

    def tabbar_choices():
        return _tabbar_candidates(_app_nav_items())

    return {"app_nav_menu": app_nav_menu, "app_nav_catalog": app_nav_catalog, "tabbar_items": tabbar_items,
            "tabbar_choices": tabbar_choices}

@app.route("/settings/<section>", methods=["POST"])
def settings_save(section):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    if section not in user_settings.SECTIONS:
        abort(404)
    try:
        new_settings = user_settings.from_form(request.form, section, _current_user_settings(), STATION_NAMES)
    except ValueError:
        return _finish("settings_page", "設定の値が正しくありません。ページを再読み込みしてからもう一度お試しください。",
                       success=False)
    _save_user_settings(my_id, new_settings)
    return _finish("settings_page", f"{SETTINGS_SECTION_LABELS[section]}の設定を保存しました。")


@app.route("/settings/quick", methods=["POST"])
def settings_quick():
    """ヘッダーのボタン(テーマ・文字の大きさ)から1項目だけ保存する。ほかの端末にも同じ設定が反映される。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    key = request.form.get("key", "")
    if key not in user_settings.QUICK_KEYS:
        abort(400)
    try:
        value = user_settings.parse_value(key, request.form.get("value", ""))
    except ValueError:
        abort(400)
    new_settings = dict(_current_user_settings())
    new_settings[key] = value
    _save_user_settings(my_id, new_settings)
    return jsonify({"success": True})


@app.route("/push/subscribe", methods=["POST"])
def push_subscribe():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    data = request.get_json(silent=True) or {}
    endpoint = str(data.get("endpoint") or "")
    keys = data.get("keys") or {}
    p256dh, auth = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
    parsed = urlparse(endpoint)
    if (parsed.scheme != "https" or not PUSH_ENDPOINT_HOST_RE.match(parsed.hostname or "") or len(endpoint) > 1000
            or not PUSH_KEY_RE.match(p256dh) or not PUSH_KEY_RE.match(auth)):
        log.warning("Rejected a push subscription from user %s (host %s)", my_id, parsed.hostname)
        return jsonify({"success": False, "message": "このブラウザの通知の送り先に対応していません。"}), 400
    conn = _users_conn()
    try:
        users.save_push_subscription(conn, my_id, endpoint, p256dh, auth,
                                     _device_label(request.headers.get("User-Agent")),
                                     now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"))
        sub_id = next((d["id"] for d in users.list_push_subscriptions(conn, my_id) if d["endpoint"] == endpoint), None)
    finally:
        conn.close()
    return jsonify({"success": True, "id": sub_id, "message": "この端末で通知を受け取れるようにしました。"})


@app.route("/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    data = request.get_json(silent=True) or {}
    conn = _users_conn()
    try:
        users.delete_push_subscription_by_endpoint(conn, str(data.get("endpoint") or ""), my_id)
    finally:
        conn.close()
    return jsonify({"success": True, "message": "この端末への通知を止めました。"})


@app.route("/push/devices/<int:sub_id>/delete", methods=["POST"])
def push_device_delete(sub_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        users.delete_push_subscription(conn, sub_id, user_id=my_id)
    finally:
        conn.close()
    return _finish("settings_page", "その端末への通知を止めました。")


@app.route("/push/test", methods=["POST"])
def push_test():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    try:
        sent = push_notify.deliver(my_id, "test", "テスト通知", "通知はこのように届きます。押すと設定画面を開きます。",
                                   url_for("settings_page"), "test", ttl=600)
    except Exception as e:
        log.warning("Test push failed: %s", e)
        sent = 0
    if sent:
        return _finish("settings_page", f"テスト通知を送りました({sent}台)。数秒で届きます。")
    return _finish("settings_page", "テスト通知を送れませんでした。この端末で「通知を受け取る」をオンにしてからお試しください。",
                   success=False)


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


def requires_observer(view):
    """観測報告の確認・訂正権限(管理者・会津観測者・只見観測者のいずれか)を必須にする。
    requires_adminと違い、Funnelでは遮断しない(2026-09-12、別tailnetのTailscale
    アカウントを使うスマホから使えなくなるという形で発覚)。観測報告はユーザー管理や
    ポイント操作のような本来の管理者専用機能(/admin配下、requires_admin)とは異なり、
    ログイン+役割さえ確認できればよい観測業務の一部として扱う。担当地域の絞り込みは
    ここでは行わない(ログイン+権限の有無だけをチェックする)ので、各ルート側で
    _observer_region_for_role/_can_correct_stationを使って地域チェックを行うこと。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        if not _can_manage_observation_reports(session.get("role")):
            flash("この操作には観測者または管理者の権限が必要です。", "error")
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


def _chart_theme():
    """グラフ描画時に使う配色テーマ('light'/'dark')。ダーク/ライト切替ボタン(base.html)が
    localStorageと一緒にthemeクッキーにも同じ値を書き込むので、それをサーバー側の
    matplotlib描画にも反映する(2026-09-12、分析ページのグラフがダークモードでも
    白背景のまま浮いて見えるという指摘を受けて追加)。"""
    return "dark" if request.cookies.get("theme") == "dark" else "light"


# Plotly移行(2026-09-14)前からの唯一の配色定義。matplotlib版(_chart_rc_context、
# PDFレポート専用)とPlotly版(ブラウザ側でのrelayout用、window.CHART_THEME_PALETTE)の
# 両方がここを参照する。以前は同じ色をそれぞれ別々にハードコードしており、
# 片方だけ配色を変えるとPDFとWebでグラフの見た目がずれていく懸念があったため、
# 単一の定義に統一した(2026-09-14)。
_CHART_THEME_PALETTE = {
    # グリッドは控えめな細線(chart_style参照)。matplotlib(PDFレポート)でも使うため16進数で指定する。
    "light": {"fg": "#1d1d1f", "grid": "#e3e6ec"},
    "dark": {"fg": "#e6e8ee", "grid": "#2c3038"},
}


def _chart_rc_context():
    """グラフの文字色・グリッド色をテーマに合わせ、背景を透過にするrcParamsのコンテキスト。
    _cached_pngでキャッシュする場合は、cache_keyにテーマも含めること(サーバー全体で
    共有するキャッシュのため、含めないと片方のテーマで生成された画像がもう片方の
    利用者にも出てしまう)。matplotlibで描画するPDFレポート(_generate_report_pdf)専用。"""
    pal = _CHART_THEME_PALETTE[_chart_theme()]
    fg, grid = pal["fg"], pal["grid"]
    return matplotlib.rc_context({
        "text.color": fg, "axes.labelcolor": fg, "axes.edgecolor": fg,
        "xtick.color": fg, "ytick.color": fg, "grid.color": grid,
        "legend.labelcolor": fg, "figure.facecolor": "none",
        "axes.facecolor": "none", "savefig.facecolor": "none",
        "savefig.transparent": True,
    })


@app.context_processor
def _inject_chart_theme_palette():
    return {"chart_theme_palette": _CHART_THEME_PALETTE}


# Plotlyグラフ用のJSONキャッシュ。_png_cache/_cached_pngと同じ「監視対象ファイルの
# mtimeが変わるまで結果を使い回す」仕組みを、PNGバイト列ではなくplotly.io.to_json()の
# JSON文字列に対して行う版。移行が全ルートに行き渡るまでは_cached_png/_png_cacheと
# 並行して存在する(移行し終えたルートから順にこちらへ切り替える)。
_chart_json_cache_lock = threading.Lock()
_chart_json_cache = {}


def _cached_chart_json(cache_key, watch_paths, render_fn):
    mtimes = tuple(_safe_mtime(p) for p in watch_paths)
    with _chart_json_cache_lock:
        cached = _chart_json_cache.get(cache_key)
        if cached is not None and cached[0] == mtimes:
            return cached[1]
    data = render_fn()
    with _chart_json_cache_lock:
        _chart_json_cache[cache_key] = (mtimes, data)
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


def _render_line_chart_plotly(dates, rates, title):
    """_render_line_chart_pngのPlotly版(2026-09-14、matplotlibからの移行第1弾)。
    PNGバイト列ではなくFigureのJSON文字列を返す。文字色・グリッド色はテーマに
    依存させず(サーバー側はニュートラルに描画)、base.htmlのmountPlotlyChart/
    chartThemeLayoutPatchがブラウザ側でPlotly.relayoutして反映する。そのため
    以降の全てのaxis(xaxis/yaxis, サブプロットがあればxaxis2/yaxis2...)は、
    空でも必ずlayoutに明示しておくこと(chartThemeLayoutPatchがel.layoutの
    キーを見て対象軸を探すため)。"""
    if dates:
        x = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
        data = [go.Scatter(
            x=x, y=rates, mode="lines+markers",
            line={"color": "#1a5276", "width": 1.4},
            marker={"size": 6},
            hovertemplate="%{x|%Y-%m-%d}: %{y:.1f}%<extra></extra>",
        )]
        annotations = []
    else:
        data = []
        annotations = [{
            "text": "データがありません", "xref": "paper", "yref": "paper",
            "x": 0.5, "y": 0.5, "showarrow": False,
        }]
    layout = {
        "title": {"text": title, "font": {"size": 14}},
        "xaxis": {"type": "date"},
        "yaxis": {"title": {"text": "的中率(%)"}, "range": [0, 100]},
        "margin": {"t": 48, "r": 20, "b": 40, "l": 50},
        "showlegend": False,
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "annotations": annotations,
    }
    return _chart_json(go.Figure(data=data, layout=layout))


# 気象条件別の川霧発生グラフの2群比較色。dataviz skillの検証済みパレットの
# カテゴリ1番目(青)・2番目(オレンジ)を固定順で使う(CVD安全性が確認済みの組み合わせ)。
_WEATHER_FOG_NO_COLOR = "#2a78d6"
_WEATHER_FOG_YES_COLOR = "#eb6834"
_WEATHER_FOG_BOXPLOT_FEATURES = [
    (COL_TEMP, "気温(℃)"), (COL_HUMIDITY, "相対湿度(％)"),
    (COL_WIND_SPEED, "風速(m/s)"), (COL_PRECIP, "降水量(mm)"),
]


def _render_weather_fog_boxplot_plotly(main_df, phenom_df, station_name):
    """station_nameの川霧発生有無ごとに、気象条件(気温・湿度・風速・降水量)の分布を
    箱ひげ図で比較する。同じdatetimeの気象値と現象コードを突き合わせ、SUMMARY_FOG_CODES
    (川霧のコード)に該当するかどうかで「川霧あり」「川霧なし」の2群に分ける。
    2群の識別だけなのでカテゴリ色は2色のみ使用し、標本数(n=)を各箱に添えて
    比較の信頼性が一目で分かるようにする。matplotlib版から移行(2026-09-14)。
    matplotlib版は各箱の下にtick_labelsで「川霧なし/あり(n=...)」を表示していたため、
    凡例ではなくx軸カテゴリ名そのものにnを埋め込んで同じ見た目を再現する。"""
    has_data = (
        main_df is not None and phenom_df is not None
        and not main_df.empty and station_name in phenom_df.columns
    )
    merged = None
    if has_data:
        merged = main_df.merge(phenom_df[["datetime", station_name]], on="datetime", how="inner")
        codes = merged[station_name].apply(encode_phenomena_cell).apply(primary_phenom_code)
        merged = merged.loc[codes.notna()].copy()
        merged["is_fog"] = codes.loc[codes.notna()].isin(SUMMARY_FOG_CODES)
        has_data = not merged.empty

    title = f"{station_name}: 気象条件別の川霧発生状況"
    if not has_data:
        fig = go.Figure()
        fig.update_layout(
            title={"text": title, "font": {"size": 14}},
            xaxis={"visible": False}, yaxis={"visible": False},
            annotations=[{
                "text": "データがありません", "xref": "paper", "yref": "paper",
                "x": 0.5, "y": 0.5, "showarrow": False,
            }],
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        )
        return _chart_json(fig)

    subplot_titles = [label for _, label in _WEATHER_FOG_BOXPLOT_FEATURES]
    fig = make_subplots(rows=2, cols=2, subplot_titles=subplot_titles,
                         vertical_spacing=0.18, horizontal_spacing=0.1)
    positions = [(1, 1), (1, 2), (2, 1), (2, 2)]
    for (row, col), (col_name, label) in zip(positions, _WEATHER_FOG_BOXPLOT_FEATURES):
        values = merged[[col_name, "is_fog"]].dropna()
        no_fog_vals = values.loc[~values["is_fog"], col_name]
        fog_vals = values.loc[values["is_fog"], col_name]
        no_fog_label = f"川霧なし(n={len(no_fog_vals)})"
        fog_label = f"川霧あり(n={len(fog_vals)})"
        fig.add_trace(go.Box(
            y=no_fog_vals, x=[no_fog_label] * len(no_fog_vals), name=no_fog_label,
            marker_color=_WEATHER_FOG_NO_COLOR, showlegend=False,
        ), row=row, col=col)
        fig.add_trace(go.Box(
            y=fog_vals, x=[fog_label] * len(fog_vals), name=fog_label,
            marker_color=_WEATHER_FOG_YES_COLOR, showlegend=False,
        ), row=row, col=col)
    fig.update_layout(
        title={"text": title, "font": {"size": 14}},
        showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin={"t": 90, "r": 20, "b": 40, "l": 50},
        height=560,
    )
    return _chart_json(fig)


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


def _collect_river_fog_histogram_values(main_df, phenom_df, station_names):
    """全地点分の行を「川霧あり」「なし」に分け、気象項目ごとの値の一覧を返す: (あり{列: [値]}, なし{列: [値]})。
    画像の判定が付いている行だけを使う(判定の無い夜間などは含めない)。"""
    fog = {col: [] for col, _, _ in _RIVER_FOG_HISTOGRAM_FEATURES}
    nofog = {col: [] for col, _, _ in _RIVER_FOG_HISTOGRAM_FEATURES}
    if main_df is None or phenom_df is None or main_df.empty:
        return fog, nofog
    main_df = main_df.copy()
    main_df[_COL_DEWPOINT_DEPRESSION] = main_df[COL_TEMP] - main_df[COL_DEWPOINT]
    for station_name in station_names or []:
        if station_name not in phenom_df.columns:
            continue
        merged = main_df.merge(phenom_df[["datetime", station_name]], on="datetime", how="inner")
        codes = merged[station_name].apply(encode_phenomena_cell).apply(primary_phenom_code)
        merged = merged.loc[codes.notna()].copy()
        is_fog = codes.loc[codes.notna()].isin(SUMMARY_FOG_CODES)
        for col, _, _ in _RIVER_FOG_HISTOGRAM_FEATURES:
            fog[col].extend(merged.loc[is_fog, col].dropna().tolist())
            nofog[col].extend(merged.loc[~is_fog, col].dropna().tolist())
    return fog, nofog


_river_fog_hist_cache = {"key": None, "values": None}
_river_fog_hist_cache_lock = threading.Lock()


def _river_fog_histogram_values():
    """_collect_river_fog_histogram_values の結果を、気象DBが変わるまで使い回す(9項目×2種類の図とCSVで同じものを使うため)。"""
    try:
        key = WEATHER_DB_PATH.stat().st_mtime
    except OSError:
        key = None
    with _river_fog_hist_cache_lock:
        if _river_fog_hist_cache["key"] == key and _river_fog_hist_cache["values"] is not None:
            return _river_fog_hist_cache["values"]
    main_df = phenom_df = station_names = None
    if key is not None:
        try:
            main_df, phenom_df, station_names = fog_data.load_river_fog_data()
        except Exception as e:
            log.warning("Failed to load data for river-fog histogram: %s", e)
    values = _collect_river_fog_histogram_values(main_df, phenom_df, station_names or STATION_NAMES)
    with _river_fog_hist_cache_lock:
        _river_fog_hist_cache.update(key=key, values=values)
    return values


def _river_fog_histogram_bins(step, *value_lists):
    """値の一覧(1つ以上)を同じ区分で数える。戻り値: (区分のラベル, 区分の中央(数値の区分のみ、それ以外はNone),
    値の一覧ごとの件数のリスト)。step が list なら不均等な境界(最初は「未満」、最後は「以上」)、数値なら固定幅で、
    すべての一覧の最小〜最大を覆う。"""
    if isinstance(step, list):
        labels = [f"〜{step[0]}"] + [f"{step[i]}〜{step[i + 1]}" for i in range(len(step) - 1)] + [f"{step[-1]}〜"]
        counts_list = []
        for values in value_lists:
            counts = [0] * (len(step) + 1)
            for v in values:
                counts[next((i for i, e in enumerate(step) if v < e), len(step))] += 1
            counts_list.append(counts)
        return labels, None, counts_list
    all_values = [v for values in value_lists for v in values]
    lo = (min(all_values) // step) * step
    hi = (max(all_values) // step) * step + step
    n_bins = max(1, int(round((hi - lo) / step)))
    counts_list = []
    for values in value_lists:
        counts = [0] * n_bins
        for v in values:
            counts[min(int((v - lo) // step), n_bins - 1)] += 1
        counts_list.append(counts)
    centers = [round(lo + (i + 0.5) * step, 6) for i in range(n_bins)]
    labels = [f"{_fmt_bin(lo + i * step)}〜{_fmt_bin(lo + (i + 1) * step)}" for i in range(n_bins)]
    return labels, centers, counts_list


def _fmt_bin(x):
    return f"{x:.1f}".rstrip("0").rstrip(".") if abs(x - round(x)) > 1e-9 else str(int(round(x)))


_WEATHER_FOG_NO_HIST_COLOR = "#8a94a6"


def _render_river_fog_histogram_plotly(fog_values, nofog_values, item=None, compare=False):
    """気象条件(気温・湿度・風速・降水量・露点温度・湿数・日射量・日照時間・視程)ごとに、
    川霧が発生していた瞬間の値だけをヒストグラムにする。_render_weather_fog_boxplot_plotly
    とは違い地点ごとに分けず、全地点分の「川霧あり」行をまとめて1つの分布として扱う
    (同じ日時でも複数地点で同時に川霧が発生していればその分だけ重みが乗る)。項目ごとに
    固定の刻み幅でビンを揃える(気温なら2℃刻み)。matplotlib版から移行(2026-09-14)。
    ax.hist(bins=edges, weights=...)と同じ「割合(%)のヒストグラム」を、go.Histogramの
    自動ビニングに任せず自前でカウントして再現する(ビン幅を項目ごとに固定するため)。
    item(_RIVER_FOG_HISTOGRAM_FEATURES の番号)を渡すと、その項目だけの1枚の図にする(個別に保存するため、2026-10-14)。
    compare=True なら「川霧なし」の分布を灰色で並べる(どの値で霧が出やすいかを比べるため、2026-10-14)。
    割合はそれぞれの中での割合(あり・なしで件数が大きく違うため)。"""
    title = "全地点合算：川霧発生時の気象条件ヒストグラム"
    if not any(fog_values.values()):
        fig = go.Figure()
        fig.update_layout(
            title={"text": title, "font": {"size": 14}},
            xaxis={"visible": False}, yaxis={"visible": False},
            annotations=[{
                "text": "データがありません", "xref": "paper", "yref": "paper",
                "x": 0.5, "y": 0.5, "showarrow": False,
            }],
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        )
        return _chart_json(fig)

    if item is not None:
        col_name, label, step = _RIVER_FOG_HISTOGRAM_FEATURES[item]
        fig = go.Figure()
        _add_river_fog_histogram_trace(fig, fog_values[col_name], nofog_values[col_name] if compare else None, step)
        fig.update_xaxes(title_text=label)
        fig.update_yaxes(title_text="割合(%)")
        if compare:
            text = (f"{label}: 川霧あり(n={len(fog_values[col_name])})となし(n={len(nofog_values[col_name])})の分布(全地点合算)")
        else:
            text = f"川霧発生時の{label}(全地点合算、n={len(fog_values[col_name])})"
        fig.update_layout(
            title={"text": text, "font": {"size": 14}},
            showlegend=compare, barmode="group", bargap=0.12,
            legend={"orientation": "h", "x": 1, "xanchor": "right", "y": 1.02, "yanchor": "bottom"},
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            margin={"t": 70, "r": 20, "b": 50, "l": 55},
            height=380,
        )
        return _chart_json(fig)

    subplot_titles = [f"{label}(n={len(fog_values[col])})" for col, label, _ in _RIVER_FOG_HISTOGRAM_FEATURES]
    fig = make_subplots(rows=3, cols=3, subplot_titles=subplot_titles,
                         vertical_spacing=0.12, horizontal_spacing=0.08)
    positions = [(r, c) for r in range(1, 4) for c in range(1, 4)]
    for (row, col), (col_name, label, step) in zip(positions, _RIVER_FOG_HISTOGRAM_FEATURES):
        _add_river_fog_histogram_trace(fig, fog_values[col_name], None, step, row=row, col=col)
        fig.update_yaxes(title_text="割合(%)", row=row, col=col)
    fig.update_layout(
        title={"text": title, "font": {"size": 14}},
        showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin={"t": 90, "r": 20, "b": 20, "l": 50},
        height=780,
    )
    return _chart_json(fig)


def _add_river_fog_histogram_trace(fig, values, nofog_values, step, row=None, col=None):
    """ヒストグラム1項目分の棒を fig に足す(row/col が無ければ1枚の図として)。nofog_values があれば「川霧なし」も並べる。"""
    if not values:
        return
    pos = {"row": row, "col": col} if row else {}
    lists = [values] + ([nofog_values] if nofog_values else [])
    labels, centers, counts_list = _river_fog_histogram_bins(step, *lists)
    pcts = [[c * 100.0 / max(1, len(v)) for c in counts] for v, counts in zip(lists, counts_list)]
    names = ["川霧あり", "川霧なし"]
    colors = [_WEATHER_FOG_YES_COLOR, _WEATHER_FOG_NO_HIST_COLOR]
    for i, pct in enumerate(pcts):
        if centers is None:
            fig.add_trace(go.Bar(
                x=labels, y=pct, name=names[i], marker_color=colors[i],
                text=[f"{v:.1f}" for v in pct] if len(pcts) == 1 else None, textposition="outside",
            ), **pos)
        else:
            width = step * (0.92 if len(pcts) == 1 else 0.44)
            offset = None if len(pcts) == 1 else (-0.46 + 0.46 * i) * step
            fig.add_trace(go.Bar(
                x=centers, y=pct, name=names[i], marker_color=colors[i], width=width, offset=offset,
            ), **pos)
    if centers is None:
        fig.update_xaxes(tickangle=-45, **pos)


def _river_fog_histogram_csv(fog_values, nofog_values, items):
    """ヒストグラムの数値(区分ごとの件数と割合、川霧あり・なし)をCSVの文字列にする(Excelで開けるようBOM付き)。"""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["項目", "区分", "川霧あり_件数", "川霧あり_割合(%)", "川霧なし_件数", "川霧なし_割合(%)"])
    for i in items:
        col_name, label, step = _RIVER_FOG_HISTOGRAM_FEATURES[i]
        fog, nofog = fog_values[col_name], nofog_values[col_name]
        if not fog and not nofog:
            continue
        labels, _, (cf, cn) = _river_fog_histogram_bins(step, fog, nofog)
        for lab, a, b in zip(labels, cf, cn):
            w.writerow([label, lab, a, round(a * 100.0 / max(1, len(fog)), 2), b, round(b * 100.0 / max(1, len(nofog)), 2)])
    return "﻿" + out.getvalue()

# 1日分の観測値推移グラフの項目(列名, 表示ラベル, 色, プロット種別)。
# 以前は気温・視程を左軸、湿度・降水量を右軸(twinx)にまとめた2軸グラフだったが、
# 軸を読み違えやすいため、項目ごとに軸を分けた4分割の小さい図に変更した。
_WEATHER_TIMESERIES_ITEMS = [
    ("気温(℃)", "気温(℃)", "#e74c3c", "line"),
    ("視程(km)", "視程(km)", "#16a085", "line"),
    ("相対湿度(％)", "相対湿度(％)", "#8e44ad", "line"),
    ("降水量(mm)", "降水量(mm)", "#2980b9", "bar"),
]


def _render_weather_timeseries_plotly(day_main, date_label):
    """1日分の気温・視程・湿度・降水量の推移を返す。項目ごとに軸を分けた2x2の小さい図に
    する(2軸グラフだと目盛りを読み違えやすいため)。matplotlib版から移行(2026-09-14)。"""
    title = f"{date_label} の観測値推移"
    if day_main is None or day_main.empty:
        fig = go.Figure()
        fig.update_layout(
            title={"text": title, "font": {"size": 14}},
            xaxis={"visible": False}, yaxis={"visible": False},
            annotations=[{
                "text": "データがありません", "xref": "paper", "yref": "paper",
                "x": 0.5, "y": 0.5, "showarrow": False,
            }],
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        )
        return _chart_json(fig)

    x = day_main["datetime"]
    subplot_titles = [label for _, label, _, _ in _WEATHER_TIMESERIES_ITEMS]
    fig = make_subplots(rows=2, cols=2, subplot_titles=subplot_titles,
                         vertical_spacing=0.18, horizontal_spacing=0.1)
    positions = [(1, 1), (1, 2), (2, 1), (2, 2)]
    for (row, col), (col_name, label, color, kind) in zip(positions, _WEATHER_TIMESERIES_ITEMS):
        if kind == "bar":
            fig.add_trace(go.Bar(x=x, y=day_main[col_name], marker_color=color, opacity=0.7),
                           row=row, col=col)
            fig.update_yaxes(rangemode="tozero", row=row, col=col)
        else:
            fig.add_trace(go.Scatter(
                x=x, y=day_main[col_name], mode="lines+markers",
                line={"color": color, "width": 1.4}, marker={"size": 5},
            ), row=row, col=col)
        fig.update_xaxes(tickformat="%H:%M", row=row, col=col)
    fig.update_layout(
        title={"text": title, "font": {"size": 14}},
        showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin={"t": 90, "r": 20, "b": 20, "l": 50},
        height=560,
    )
    return _chart_json(fig)


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


def _load_metrics_history_trend(station=None):
    """fog_model.append_metrics_historyが書く履歴CSVから、学習日ごとの
    平均accuracy・平均川霧F1(%)を計算する（『精度が向上しているか』を見るための集計）。
    station指定時はその地点の行だけに絞る(CSVは元々station列を持つ、地点ごとの
    モデル精度推移を見せるための拡張。2026-09-12追加)。
    """
    if not fog_model.METRICS_HISTORY_PATH.exists():
        return []
    by_date = {}
    try:
        with open(fog_model.METRICS_HISTORY_PATH, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if station is not None and row.get("station") != station:
                    continue
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


def _render_metrics_trend_png(trend, title="学習時のモデル精度推移(全地点平均)"):
    """学習時のaccuracy・川霧F1の推移を折れ線グラフのPNGにして返す。station指定で絞り込んだ
    trend(_load_metrics_history_trend参照)を渡せば地点別の推移にもなる。"""
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
            ax.plot(xs, ys, marker="s", markersize=3, color="#c0392b", linewidth=1.4, label="川霧F1(平均,%)")
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
    ax.set_title(title, fontsize=11)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _render_metrics_trend_plotly(trend, title="学習時のモデル精度推移(全地点平均)"):
    """_render_metrics_trend_pngのPlotly版(2026-09-14)。"""
    all_dates = [datetime.strptime(t["date"], "%Y-%m-%d") for t in trend]
    acc_pts = [(d, t["accuracy"]) for d, t in zip(all_dates, trend) if t["accuracy"] is not None]
    fog_pts = [(d, t["fog_f1"]) for d, t in zip(all_dates, trend) if t["fog_f1"] is not None]
    data = []
    annotations = []
    if all_dates:
        if acc_pts:
            xs, ys = zip(*acc_pts)
            data.append(go.Scatter(x=xs, y=ys, mode="lines+markers", name="accuracy(平均,%)",
                                    line={"color": "#1a5276", "width": 1.4}, marker={"size": 6}))
        if fog_pts:
            xs, ys = zip(*fog_pts)
            data.append(go.Scatter(x=xs, y=ys, mode="lines+markers", name="川霧F1(平均,%)",
                                    line={"color": "#c0392b", "width": 1.4}, marker={"size": 6, "symbol": "square"}))
    else:
        annotations.append({
            "text": "データがありません", "xref": "paper", "yref": "paper",
            "x": 0.5, "y": 0.5, "showarrow": False,
        })
    layout = {
        "title": {"text": title, "font": {"size": 14}},
        "xaxis": {"type": "date"},
        "yaxis": {"title": {"text": "スコア(%)"}, "range": [0, 100]},
        "margin": {"t": 48, "r": 20, "b": 40, "l": 50},
        "legend": {"x": 0, "y": 1, "font": {"size": 11}},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "annotations": annotations,
    }
    return _chart_json(go.Figure(data=data, layout=layout))


def _is_valid_number(v):
    """NoneでもNaNでもない数値かを判定する（NaN != NaNを利用）。"""
    return v is not None and v == v


def _render_model_compare_plotly(state):
    """採用中のモデル(全地点共通の二値LightGBM)と基準(気候値: 地点×時刻ごとの過去の
    川霧発生率をそのまま確率とみなす)を、評価期間(学習に使っていない直近のデータ)で比較する。
    気候値を上回っていなければ、モデルは「いつ・どこで出やすいか」以上のことを気象から
    学べていないことになる。"""
    metrics = fog_model.shared_global_metrics(state) or {}
    model_m, clim_m = metrics.get("model") or {}, metrics.get("climatology") or {}
    names = [("f1", "川霧F1"), ("ap", "PR-AUC"), ("recall", "recall(見逃しの少なさ)"),
             ("precision", "precision(空振りの少なさ)")]
    annotations = []
    data = []
    if model_m:
        for label, m, color in (("採用中(LightGBM二値)", model_m, "#1a5276"),
                                ("基準: 気候値(地点×時刻の発生率)", clim_m, "#7f8c8d")):
            values = [m.get(k) for k, _ in names]
            data.append(go.Bar(
                x=[n for _, n in names], y=[v * 100 if v is not None else 0 for v in values], name=label,
                marker={"color": color}, textposition="outside",
                text=[f"{v * 100:.1f}" if v is not None else "データなし" for v in values],
            ))
    else:
        annotations.append({
            "text": "データがありません", "xref": "paper", "yref": "paper",
            "x": 0.5, "y": 0.5, "showarrow": False,
        })
    layout = {
        "title": {"text": "採用モデルと基準(気候値)の比較(評価期間)", "font": {"size": 14}},
        "yaxis": {"title": {"text": "スコア(%)"}, "range": [0, 100]},
        "barmode": "group",
        "margin": {"t": 48, "r": 20, "b": 60, "l": 50},
        "legend": {"x": 1, "y": 1, "xanchor": "right", "yanchor": "top", "font": {"size": 11}},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "annotations": annotations,
    }
    return _chart_json(go.Figure(data=data, layout=layout))


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


def _render_feature_importance_plotly(importance, top_n=15, title="特徴量の重要度(上位)", xlabel="重要度(割合)"):
    """_render_feature_importance_pngのPlotly版(2026-09-14)。matplotlib版は
    件数に応じてfigsizeの高さを変えていたが、Plotly版はlayout.heightをpx単位で
    同様に可変にする(件数が多いほど縦に伸ばし、ラベルが詰まらないようにする)。"""
    top = importance[:top_n]
    annotations = []
    if not top:
        data = []
        annotations.append({
            "text": "データがありません", "xref": "paper", "yref": "paper",
            "x": 0.5, "y": 0.5, "showarrow": False,
        })
    else:
        names = [n for n, _ in top][::-1]
        vals = [v for _, v in top][::-1]
        data = [go.Bar(
            x=vals, y=names, orientation="h", marker={"color": "#1a5276"},
            text=[f"{v:.3f}" for v in vals], textposition="outside",
        )]
    height = max(320, int(35 * max(len(top), 1)) + 90)
    layout = {
        "title": {"text": title, "font": {"size": 14}},
        "xaxis": {"title": {"text": xlabel}},
        "yaxis": {},
        "height": height,
        "margin": {"t": 48, "r": 40, "b": 40, "l": 160},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "annotations": annotations,
    }
    return _chart_json(go.Figure(data=data, layout=layout))


def _render_confusion_matrix_png(labels, matrix, title="混同行列(全地点合算)"):
    """全地点分を合算した混同行列をヒートマップで表示する。管理者向け「モデル管理」
    ページ(学習時の検証データが元)と、/accuracyページ(本番の予測ログが元、
    _confusion_matrix_from_pairs参照)の両方でこの関数を共用する。"""
    fig, ax = plt.subplots(figsize=(4.6, 4.0))
    if matrix is None or len(labels) == 0:
        ax.text(0.5, 0.5, "データがありません", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
    else:
        label_texts = [c if isinstance(c, str) else PHENOM_LABELS.get(c, f"コード{c}") for c in labels]
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
    ax.set_title(title, fontsize=11)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _render_confusion_matrix_plotly(labels, matrix, title="混同行列(全地点合算)"):
    """_render_confusion_matrix_pngのPlotly版(2026-09-14)。セルの数値は
    go.Heatmapの組み込みtextではなくlayout.annotationsで1セルずつ置く
    (matplotlib版と同じく、セルの値がvmaxの60%を超えるかどうかで文字色を
    白/濃色に固定するため。この色はセルの背景色に対する可読性の話であって
    テーマとは無関係なので、chartThemeLayoutPatchのfont.color一括変更では
    上書きされない)。カラーバーの目盛り色だけはtrace側(colorbar)にあるため、
    base.htmlのapplyChartThemeがdata-chart-kind="heatmap"を見てPlotly.restyle
    で別途対応する。"""
    annotations = []
    if matrix is None or len(labels) == 0:
        data = []
        annotations.append({
            "text": "データがありません", "xref": "paper", "yref": "paper",
            "x": 0.5, "y": 0.5, "showarrow": False,
        })
        layout = {
            "title": {"text": title, "font": {"size": 14}},
            "xaxis": {"visible": False}, "yaxis": {"visible": False},
            "margin": {"t": 48, "r": 20, "b": 20, "l": 20},
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "annotations": annotations,
        }
        return _chart_json(go.Figure(data=data, layout=layout))

    label_texts = [c if isinstance(c, str) else PHENOM_LABELS.get(c, f"コード{c}") for c in labels]
    vmax = matrix.max() if matrix.size else 0
    data = [go.Heatmap(
        z=matrix, x=label_texts, y=label_texts, colorscale="Blues",
        hovertemplate="実際: %{y}<br>予測: %{x}<br>件数: %{z}<extra></extra>",
        showscale=True,
    )]
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            v = matrix[i, j]
            color = "white" if vmax and v > vmax * 0.6 else "#1c2733"
            annotations.append({
                "x": label_texts[j], "y": label_texts[i], "text": str(int(v)),
                "showarrow": False, "font": {"color": color, "size": 11},
            })
    layout = {
        "title": {"text": title, "font": {"size": 14}},
        "xaxis": {"title": {"text": "予測"}, "tickangle": -30},
        "yaxis": {"title": {"text": "実際"}, "autorange": "reversed"},
        "margin": {"t": 48, "r": 20, "b": 90, "l": 90},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "annotations": annotations,
    }
    return _chart_json(go.Figure(data=data, layout=layout))


def _render_lead_time_chart_png(by_lead_day):
    """リードタイム(予測から何日後か)別の的中率を棒グラフにする。表だけだと
    『日数が経つほど精度が落ちる』傾向が伝わりにくいための補足グラフ。"""
    fig, ax = plt.subplots(figsize=(7, 3.2))
    rows = [r for r in by_lead_day if r["n"] > 0]
    if rows:
        labels = [f"{r['lead_day']}日後" for r in rows]
        rates = [100.0 * r["correct"] / r["n"] for r in rows]
        x = list(range(len(rows)))
        ax.bar(x, rates, color="#1a5276", width=0.6)
        for i, (rate, r) in enumerate(zip(rates, rows)):
            ax.text(i, rate + 2, f"{rate:.1f}%\n(n={r['n']})", ha="center", fontsize=8)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=9)
    else:
        ax.text(0.5, 0.5, "データがありません", ha="center", va="center", transform=ax.transAxes)
    ax.set_ylim(0, 100)
    ax.set_ylabel("的中率(%)")
    ax.set_title("リードタイム別の的中率", fontsize=11)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _render_lead_time_chart_plotly(by_lead_day):
    """_render_lead_time_chart_pngのPlotly版(2026-09-14)。"""
    rows = [r for r in by_lead_day if r["n"] > 0]
    annotations = []
    if rows:
        labels = [f"{r['lead_day']}日後" for r in rows]
        rates = [100.0 * r["correct"] / r["n"] for r in rows]
        text = [f"{rate:.1f}%<br>(n={r['n']})" for rate, r in zip(rates, rows)]
        data = [go.Bar(x=labels, y=rates, marker={"color": "#1a5276"}, text=text, textposition="outside")]
    else:
        data = []
        annotations.append({
            "text": "データがありません", "xref": "paper", "yref": "paper",
            "x": 0.5, "y": 0.5, "showarrow": False,
        })
    layout = {
        "title": {"text": "リードタイム別の的中率", "font": {"size": 14}},
        "xaxis": {},
        "yaxis": {"title": {"text": "的中率(%)"}, "range": [0, 100]},
        "margin": {"t": 48, "r": 20, "b": 40, "l": 50},
        "showlegend": False,
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "annotations": annotations,
    }
    return _chart_json(go.Figure(data=data, layout=layout))


def _render_region_compare_chart_png(region_summary):
    """地域(会津/只見川)ごとに的中率と川霧F1を並べた棒グラフ。会津は観測開始が新しく
    学習データが少ないため、只見川との差が一目でわかるようにする。"""
    fig, ax = plt.subplots(figsize=(7, 3.4))
    rows = [r for r in region_summary if r["n"] > 0]
    if rows:
        labels = [r["region"] for r in rows]
        acc = [100.0 * r["correct"] / r["n"] for r in rows]
        f1 = [100.0 * r["f1"] if r["f1"] is not None else 0 for r in rows]
        x = np.arange(len(rows))
        width = 0.32
        ax.bar(x - width / 2, acc, width, color="#1a5276", label="的中率(%)")
        ax.bar(x + width / 2, f1, width, color="#c0392b", label="川霧F1(%)")
        for i, r in enumerate(rows):
            ax.text(i - width / 2, acc[i] + 2, f"{acc[i]:.1f}", ha="center", fontsize=8)
            label = f"{f1[i]:.1f}" if r["f1"] is not None else "データなし"
            ax.text(i + width / 2, f1[i] + 2, label, ha="center", fontsize=8)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=9)
        ax.legend(loc="upper right", fontsize=8)
    else:
        ax.text(0.5, 0.5, "データがありません", ha="center", va="center", transform=ax.transAxes)
    ax.set_ylim(0, 100)
    ax.set_ylabel("スコア(%)")
    ax.set_title("地域別の的中率・川霧F1", fontsize=11)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


REGION_F1_HIDDEN = {"会津"}  # 分析ページの「地域別の的中率・川霧F1」で川霧F1を出さない地域


def _render_region_compare_chart_plotly(region_summary):
    """_render_region_compare_chart_pngのPlotly版(2026-09-14)。"""
    rows = [r for r in region_summary if r["n"] > 0]
    annotations = []
    if rows:
        labels = [r["region"] for r in rows]
        acc = [100.0 * r["correct"] / r["n"] for r in rows]
        # 川霧F1は REGION_F1_HIDDEN の地域(会津、2026-10-13に利用者の判断で外した)には出さない
        f1_rows = [r for r in rows if r["region"] not in REGION_F1_HIDDEN]
        f1_text = [f"{100.0 * r['f1']:.1f}" if r["f1"] is not None else "データなし" for r in f1_rows]
        f1_vals = [100.0 * r["f1"] if r["f1"] is not None else 0 for r in f1_rows]
        data = [
            go.Bar(x=labels, y=acc, name="的中率(%)", marker={"color": "#1a5276"},
                   text=[f"{v:.1f}" for v in acc], textposition="outside"),
            go.Bar(x=[r["region"] for r in f1_rows], y=f1_vals, name="川霧F1(%)", marker={"color": "#c0392b"},
                   text=f1_text, textposition="outside"),
        ]
    else:
        data = []
        annotations.append({
            "text": "データがありません", "xref": "paper", "yref": "paper",
            "x": 0.5, "y": 0.5, "showarrow": False,
        })
    layout = {
        "title": {"text": "地域別の的中率・川霧F1", "font": {"size": 14}},
        "xaxis": {},
        "yaxis": {"title": {"text": "スコア(%)"}, "range": [0, 100]},
        "barmode": "group",
        "margin": {"t": 48, "r": 20, "b": 40, "l": 50},
        "legend": {"x": 1, "y": 1, "xanchor": "right", "yanchor": "top", "font": {"size": 11}},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "annotations": annotations,
    }
    return _chart_json(go.Figure(data=data, layout=layout))


def _render_threshold_sweep_png(sweep, current_threshold=None, title="しきい値ごとのprecision/recall/F1"):
    """川霧と判定するしきい値(fog_probがこの値以上なら「川霧」)を変えた場合の
    precision(空振りの少なさ)・recall(見逃しの少なさ)・F1の変化を折れ線で示す。
    現在採用中のしきい値(学習時に検証データでF1が最大になるよう選んだ値)を縦線で示し、
    他のしきい値を選んだ場合とのトレードオフを比較できるようにする。"""
    fig, ax = plt.subplots(figsize=(8, 3.4))
    if sweep:
        ts = [s["threshold"] for s in sweep]
        precisions = [s["precision"] if s["precision"] is not None else float("nan") for s in sweep]
        recalls = [s["recall"] if s["recall"] is not None else float("nan") for s in sweep]
        f1s = [s["f1"] if s["f1"] is not None else float("nan") for s in sweep]
        ax.plot(ts, precisions, marker="o", markersize=3, color="#2a78d6", linewidth=1.4, label="precision(空振りの少なさ)")
        ax.plot(ts, recalls, marker="s", markersize=3, color="#eb6834", linewidth=1.4, label="recall(見逃しの少なさ)")
        ax.plot(ts, f1s, marker="^", markersize=3, color="#16a085", linewidth=1.6, label="F1")
        if current_threshold is not None:
            # 以前はax.text(y=1.02)で縦線の上にラベルを浮かせていたが、タイトルと重なって
            # 読みにくかったため、凡例の1エントリとして表示する方式に変更した。
            ax.axvline(current_threshold, color="#7f2020", linestyle="--", linewidth=1.2,
                       label=f"採用中({current_threshold:.2f})")
        ax.legend(loc="upper right", fontsize=7.5, ncol=1, framealpha=0.9)
        ax.set_xlim(0, 1)
    else:
        ax.text(0.5, 0.5, "データがありません", ha="center", va="center", transform=ax.transAxes)
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("しきい値(この確率以上を「川霧」と判定)")
    ax.set_ylabel("スコア")
    ax.set_title(title, fontsize=11)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _render_threshold_sweep_plotly(sweep, current_threshold=None, title="しきい値ごとのprecision/recall/F1"):
    """_render_threshold_sweep_pngのPlotly版(2026-09-14)。現在採用中のしきい値の
    縦線は、Plotlyのlayout.shapesだと凡例に出ない(matplotlibのaxvline(label=...)と
    違う仕様)ため、2点だけのScatter(破線)として凡例名付きで描く。"""
    data = []
    annotations = []
    if sweep:
        ts = [s["threshold"] for s in sweep]
        precisions = [s["precision"] for s in sweep]
        recalls = [s["recall"] for s in sweep]
        f1s = [s["f1"] for s in sweep]
        data.append(go.Scatter(
            x=ts, y=precisions, mode="lines+markers", name="precision(空振りの少なさ)",
            line={"color": "#2a78d6", "width": 1.4}, marker={"size": 6, "symbol": "circle"},
        ))
        data.append(go.Scatter(
            x=ts, y=recalls, mode="lines+markers", name="recall(見逃しの少なさ)",
            line={"color": "#eb6834", "width": 1.4}, marker={"size": 6, "symbol": "square"},
        ))
        data.append(go.Scatter(
            x=ts, y=f1s, mode="lines+markers", name="F1",
            line={"color": "#16a085", "width": 1.6}, marker={"size": 7, "symbol": "triangle-up"},
        ))
        if current_threshold is not None:
            data.append(go.Scatter(
                x=[current_threshold, current_threshold], y=[0, 1.05], mode="lines",
                name=f"採用中({current_threshold:.2f})",
                line={"color": "#7f2020", "width": 1.2, "dash": "dash"},
            ))
    else:
        annotations.append({
            "text": "データがありません", "xref": "paper", "yref": "paper",
            "x": 0.5, "y": 0.5, "showarrow": False,
        })
    layout = {
        "title": {"text": title, "font": {"size": 14}},
        "xaxis": {"title": {"text": "しきい値(この確率以上を「川霧」と判定)"}, "range": [0, 1]},
        "yaxis": {"title": {"text": "スコア"}, "range": [0, 1.05]},
        "margin": {"t": 48, "r": 20, "b": 50, "l": 50},
        "legend": {"x": 1, "y": 1, "xanchor": "right", "yanchor": "top", "font": {"size": 11}},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "annotations": annotations,
    }
    return _chart_json(go.Figure(data=data, layout=layout))


def _draw_tree_on_plotly(fig, col, root, max_display_depth, is_rf):
    """_draw_tree_on_axisのPlotly版(2026-09-14)。箱・線はfig.add_shape/add_trace、
    ラベルはfig.add_annotationで描く(matplotlib版と同じ固定色のFancyBboxPatch相当)。
    matplotlib版には無かった改善点として、透明なマーカーの重ね描きでホバー時に
    省略ノード(「…」)の実際の深さ・ノード数や、9文字超で省略された特徴量の
    フルネームを表示できるようにする(matplotlib版では完全に読み取れなかった情報)。"""
    positions = fog_tree_viz.assign_layout(root, max_display_depth)
    edge_x, edge_y = [], []
    hover_x, hover_y, hover_text = [], [], []

    def draw(node):
        if node is None:
            return
        x, y, truncated = positions[id(node)]
        if node["is_leaf"] or truncated:
            if truncated:
                label, color = "…", "#9aa5b1"
                sub_n = fog_tree_viz.tree_node_count(node)
                sub_depth = fog_tree_viz.tree_actual_max_depth(node)
                hover = f"以降は省略(実際はさらに深さ{sub_depth}まで、{sub_n}ノード続く)"
            elif is_rf:
                label = PHENOM_LABELS.get(node["value"], "現象なし" if node["value"] == 0 else str(node["value"]))
                color = "#7fb8a4"
                hover = f"判定: {label}"
            else:
                label = f"{node['value']:+.2f}"
                color = "#e6c789"
                hover = f"この木単体の葉の値: {label}"
            fig.add_shape(
                type="rect", x0=x - 0.40, x1=x + 0.40, y0=y - 0.34, y1=y + 0.34,
                line={"color": "#5a6570", "width": 0.8}, fillcolor=color, opacity=0.9,
                row=1, col=col,
            )
            fig.add_annotation(x=x, y=y, text=label, showarrow=False,
                                font={"size": 9, "color": "#1c2733"}, row=1, col=col)
            hover_x.append(x)
            hover_y.append(y)
            hover_text.append(hover)
            return
        feat_full = node["feature"]
        feat = feat_full if len(feat_full) <= 9 else feat_full[:8] + "…"
        label = f"{feat}<br>≤{node['threshold']:.2f}"
        fig.add_shape(
            type="rect", x0=x - 0.44, x1=x + 0.44, y0=y - 0.34, y1=y + 0.34,
            line={"color": "#5a6570", "width": 0.8}, fillcolor="#a9c6de", opacity=0.9,
            row=1, col=col,
        )
        fig.add_annotation(x=x, y=y, text=label, showarrow=False,
                            font={"size": 8.5, "color": "#1c2733"}, row=1, col=col)
        hover_x.append(x)
        hover_y.append(y)
        hover_text.append(f"{feat_full} ≤ {node['threshold']:.2f}")
        for child in (node.get("left"), node.get("right")):
            if child is None:
                continue
            cx, cy, _ = positions[id(child)]
            edge_x.extend([x, cx, None])
            edge_y.extend([y + 0.34, cy - 0.34, None])
        draw(node["left"])
        draw(node["right"])

    draw(root)
    fig.add_trace(go.Scatter(
        x=edge_x, y=edge_y, mode="lines", line={"color": "#8a97a3", "width": 0.7},
        hoverinfo="skip", showlegend=False,
    ), row=1, col=col)
    fig.add_trace(go.Scatter(
        x=hover_x, y=hover_y, mode="markers",
        marker={"size": 30, "color": "rgba(0,0,0,0)"},
        hovertext=hover_text, hoverinfo="text", showlegend=False,
    ), row=1, col=col)
    xs = [p[0] for p in positions.values()]
    return (min(xs), max(xs)) if xs else (0, 0)


def _render_tree_depth_diagram_plotly(extracted, station_name):
    """_render_tree_depth_diagram_pngのPlotly版(2026-09-14)。"""
    kinds = list(extracted.keys())
    max_display = fog_tree_viz.DEFAULT_MAX_DISPLAY_DEPTH
    if not kinds:
        fig = go.Figure()
        fig.update_layout(
            title={"text": f"{station_name}: 採用モデルの決定木(代表1本)", "font": {"size": 14}},
            xaxis={"visible": False}, yaxis={"visible": False},
            annotations=[{
                "text": "この地点はまだモデルがありません", "xref": "paper", "yref": "paper",
                "x": 0.5, "y": 0.5, "showarrow": False,
            }],
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            margin={"t": 60, "r": 20, "b": 20, "l": 20},
        )
        return _chart_json(fig)

    subplot_titles = []
    for kind in kinds:
        info = extracted[kind]
        configured = info["configured_max_depth"]
        configured_label = "無制限" if configured is None else str(configured)
        subplot_titles.append(
            f"{kind}: 実際の深さ{info['actual_max_depth']} / "
            f"設定上限{configured_label} / 本数{info['n_estimators']}"
        )
    fig = make_subplots(rows=1, cols=len(kinds), subplot_titles=subplot_titles, horizontal_spacing=0.06)
    for i, kind in enumerate(kinds, start=1):
        info = extracted[kind]
        x_min, x_max = _draw_tree_on_plotly(fig, i, info["root"], max_display, kind == "RF")
        fig.update_xaxes(range=[x_min - 0.6, x_max + 0.6], visible=False, row=1, col=i)
        fig.update_yaxes(range=[max_display + 0.6, -0.6], visible=False, row=1, col=i)
    fig.update_layout(
        title={
            "text": f"{station_name}: 採用モデルの決定木(代表1本、表示は上位{max_display}階層まで、"
                    "「…」にカーソルを合わせると続きの深さ・ノード数を表示)",
            "font": {"size": 13},
        },
        showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin={"t": 100, "r": 20, "b": 20, "l": 20},
        height=520,
    )
    return _chart_json(fig)


def _low_accuracy_stations(state, n=5):
    """accuracyが低い順にn地点の名前を返す(精度未算出の地点は除く)。"""
    scored = [
        (name, info.get("accuracy"))
        for name, info in state.items()
        if _is_valid_number(info.get("accuracy"))
    ]
    scored.sort(key=lambda t: t[1])
    return [name for name, _ in scored[:n]]


# 川霧アラート(今後ALERT_HOURS_AHEAD時間)は通知のベル・ダッシュボードで全ページから呼ばれ、1回約0.26秒かかる。
# 予測は1日1回しか変わらないので60秒だけ使い回す(2026-10-08)。
UPCOMING_ALERTS_CACHE_SECONDS = 60
_upcoming_alerts_cache = {"data": None, "at": 0.0}


def _upcoming_fog_alerts_safe():
    now = time.monotonic()
    c = _upcoming_alerts_cache
    if c["data"] is None or now - c["at"] >= UPCOMING_ALERTS_CACHE_SECONDS:
        c["data"], c["at"] = _upcoming_fog_alerts_uncached(), now
    return [dict(a) if isinstance(a, dict) else a for a in c["data"]]


def _upcoming_fog_alerts_uncached():
    if not DB_PATH.exists():
        return []
    now_naive = now_jst_naive()
    from_str = now_naive.strftime("%Y-%m-%d %H:%M:%S")
    to_str = (now_naive + timedelta(hours=ALERT_HOURS_AHEAD)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            # 予測は二値化済み(確率がしきい値以上なら川霧あり)なので、「川霧あり」と予測された
            # 行をそのままアラートにする。以前は多クラス分類の確率に対して別途95%以上という
            # 条件を付けていたが、二値LightGBM(2026-10-04〜)ではしきい値の選択が
            # その役割を担う(確率は実際の発生率に近い値で出るため、95%はほぼ到達しない)。
            return prediction_log.upcoming_fog_alerts(conn, from_str, to_str, SUMMARY_FOG_CODES)
        finally:
            conn.close()
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
    "あなたは只見川・会津地方の川霧観測・予測システム「川霧観測・予測」のAIアシスタントです。"
    "以下に渡す現在の観測状況・今後数日の予測・予測精度・モデルやサーバーの状態・"
    "システムの仕組みをもとに、"
    "利用者からの質問に日本語で簡潔に答えてください。\n"
    "\n"
    "【このシステムの仕組み(常に正しい前提知識として使ってよい)】\n"
    "- 川霧の発生予測には、全地点で共通の1つのLightGBMモデルを使い、「川霧あり/なし」の二値で"
    "予測している(2026-10-04に、地点ごとに複数方式から選ぶ多クラス分類から切り替えた)。"
    "地点ごとの違いは、地点ごとの気象値と、その地点の過去の川霧発生率などの特徴量で表している。\n"
    "- モデルは川霧である確率を出し、確率がしきい値(fog_threshold、全地点共通)以上なら「川霧あり」と"
    "判定する。しきい値は、直近の検証用データでF1が最良になる値を自動選択している。\n"
    "- しきい値の選択と精度の評価は、本番と同じく気象の予報値を入力にして行っている"
    "(過去に実際に出されていた予報を使う)。精度は学習に使っていない直近21日分のデータで評価し、"
    "「地点×時刻ごとの過去の発生率」をそのまま答えるだけの基準(気候値)とも比較している。\n"
    "- 特徴量には気温・湿度・降水量・視程などの気象観測値に加え、その地点の過去の川霧発生率や、"
    "他地点も含めた広域の川霧の出やすさを示す指数なども使っている。\n"
    "- 毎日の再学習のたびに、新しいモデルの確率の誤差が評価期間で気候値より統計的に有意に"
    "悪ければ(学習が壊れている等)、自動的に前のモデルを使い続ける安全機構がある。\n"
    "- 予測精度がなぜ変動するか(季節性、学習データの偏り、直近の異常な気象、モデルの再学習・"
    "自動ロールバック等)といった、システムの仕組みや機械学習全般に関する質問には、"
    "上記の前提知識・渡された数値データ・一般的な機械学習の知識をもとに答えて構わない。\n"
    "\n"
    "【研究・論文に関する質問への答え方(2026-10-13追加)】\n"
    "- 研究の評価の数字は、下の「研究の評価と実験の結果」にある値を使い、必ず『どの期間・何を採点した値か』を添える。"
    "渡されていない数字は作らない。\n"
    "- 用語: F1=適合率と再現率の調和平均(0〜1、川霧のような少ない現象の当たり方を見る)。適合率(precision)=『出る』と"
    "予測したうち当たった割合(低いと空振りが多い)。再現率(recall)=実際に出た霧のうち予測できていた割合(低いと見逃しが多い)。"
    "Brierスコア=確率の二乗誤差(小さいほど良い)。AP=霧の出やすい時刻を上位に並べる力。気候値=天気予報を使わず、地点×時刻ごとの"
    "過去の発生率をそのまま確率とみなす予測(予測の実力を測る基準)。スキルスコア=1−(モデルのBrier÷気候値のBrier)。"
    "SHAP値=各特徴量が予測をどれだけ動かしたかの寄与。湿数=気温と露点温度の差(=特徴量の『気温露点差』)。\n"
    "- 不均衡データ: 川霧は全体の数%しかないため、常に『なし』と答えるだけで正答率が非常に高くなる。正答率は性能の指標にならず、"
    "F1・Brier・気候値との比較で評価する、と説明する。\n"
    "- 数字が違う理由を聞かれたら: 『評価期間の値』は、学習に使っていない直近の期間を、過去に実際に出された予報を入力にして"
    "地点×時刻ごとに1回だけ採点したもの(論文・モデル比較向け)。分析ページの累計(リードタイム別・地域別の的中率など)は、"
    "毎晩出してきた予報をあとから照合した累計で、同じ時刻を何度も数え、過去の日を予測し直した『再予測』も含む。"
    "『前日夜の予報』の値は、対象の日の前の晩に出た予報だけを数えた実運用の当たり方。\n"
    "- 研究としての良い点と課題を聞かれたら、数字にもとづいて両方を正直に答える(例: 気候値より空振りが少ない一方、再現率が低い)。\n"
    "- 採点の仕方が違う値どうし(評価期間・前日夜の予報・分析ページの累計)を並べて『向上した』『より良い』と比べない。並べるときは、条件が違うことを添える。\n"
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
    "- 川霧やこのシステムの仕組みと全く関係のない質問(一般雑学等)には、"
    "「この機能は川霧観測・予測に関する質問向けです」と丁寧に答える。"
)


_research_ctx_cache = {"at": 0.0, "text": ""}
RESEARCH_CTX_SECONDS = 600


def _build_research_context():
    """AIチャット用(2026-10-13): 研究で聞かれそうな評価の数字と実験の結果を、出典(期間・採点の仕方)つきの文章にする。"""
    now = time.monotonic()
    if _research_ctx_cache["text"] and now - _research_ctx_cache["at"] < RESEARCH_CTX_SECONDS:
        return _research_ctx_cache["text"]
    lines = ["【研究の評価と実験の結果】(数字はこの一覧の値だけを使うこと)"]
    pct = lambda v: "-" if v is None else f"{100.0 * v:.1f}%"  # noqa: E731
    num = lambda v, d=3: "-" if v is None else f"{v:.{d}f}"  # noqa: E731
    try:
        state = fog_model.load_model_state() or {}
        gm = fog_model.shared_global_metrics(state) or {}
        m, c = gm.get("model") or {}, gm.get("climatology") or {}
        if m:
            n, nf = m.get("n") or 0, m.get("n_fog") or 0
            lines.append(f"- 評価期間 {str(gm.get('test_start'))[:10]}〜{str(gm.get('test_end'))[:10]}(学習に使っていない直近の期間、過去の予報を入力、"
                         f"全{len(state)}地点・地点×時刻ごとに1回採点): {n}件中 川霧{nf}件({pct(nf / n if n else None)})。"
                         f"常に『なし』と答えた場合の正答率 {pct(1 - nf / n if n else None)}。")
            lines.append(f"- LightGBM(本番): F1 {num(m.get('f1'))}、適合率 {num(m.get('precision'))}、再現率 {num(m.get('recall'))}、正答率 {pct(m.get('accuracy'))}、"
                         f"Brier {num(m.get('brier'), 4)}、AP {num(m.get('ap'))}、空振り{m.get('fp')}件・見逃し{m.get('fn')}件・的中{m.get('tp')}件。")
            lines.append(f"- 気候値: F1 {num(c.get('f1'))}、適合率 {num(c.get('precision'))}、再現率 {num(c.get('recall'))}、Brier {num(c.get('brier'), 4)}、"
                         f"AP {num(c.get('ap'))}、空振り{c.get('fp')}件・見逃し{c.get('fn')}件。"
                         + (f"スキルスコア(Brier) {1 - m['brier'] / c['brier']:.2f}。" if m.get("brier") and c.get("brier") else ""))
            cmp_ = gm.get("comparison") or {}
            rx = cmp_.get("rf_xgboost") or {}
            if rx:
                ci = cmp_.get("f1_diff_ci") or [None, None]
                lines.append(f"- 比較(Random Forest+XGBoostのアンサンブル、同じ評価期間): F1 {num(rx.get('f1'))}、適合率 {num(rx.get('precision'))}、"
                             f"再現率 {num(rx.get('recall'))}、Brier {num(rx.get('brier'), 4)}、AP {num(rx.get('ap'))}。"
                             f"LightGBMとのF1の差 {num(cmp_.get('f1_diff'))}(95%区間 {num(ci[0])}〜{num(ci[1])}"
                             f"{'、0を含むので有意な差なし' if ci[0] is not None and ci[0] < 0 < ci[1] else ''})。")
            lines.append(f"- 学習データ: {str(gm.get('train_start'))[:10]}〜{str(gm.get('train_end'))[:10]}、{gm.get('n_train')}件(うち川霧{gm.get('n_train_fog')}件)。"
                         f"しきい値 {gm.get('threshold')}。全地点で1つのモデル。")
            # 地域別(評価期間の地点ごとの結果を地域で合計)
            import numpy as np
            for region in ("只見川", "会津"):
                ys, ps = [], []
                for name, info in state.items():
                    if STATION_REGION.get(name) == region and info.get("y_test") is not None and info.get("y_pred") is not None:
                        ys.append(np.asarray(info["y_test"], int)); ps.append(np.asarray(info["y_pred"], int))
                if ys:
                    y, yp = np.concatenate(ys), np.concatenate(ps)
                    tp, fp, fn = int(((yp == 1) & (y == 1)).sum()), int(((yp == 1) & (y == 0)).sum()), int(((yp == 0) & (y == 1)).sum())
                    f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
                    lines.append(f"- 評価期間の{region}だけ({len(ys)}地点): {len(y)}件中 川霧{int(y.sum())}件、F1 {f1:.3f}"
                                 f"(適合率 {num(tp / (tp + fp) if tp + fp else None)}、再現率 {num(tp / (tp + fn) if tp + fn else None)})。")
            imp = gm.get("importance") or {}
            if imp.get("features"):
                lines.append("- 特徴量の寄与(SHAP、評価期間、全地点): " + "、".join(f"{k}({v * 100:.0f}%)" for k, v in imp["features"][:8]) + "。")
            if imp.get("groups"):
                lines.append("- まとまりごとの寄与: " + "、".join(f"{k}({v * 100:.0f}%)" for k, v in imp["groups"]) + "。")
    except Exception as e:
        log.warning("Research context (model) failed: %s", e)

    def read(rel):
        try:
            return json.loads((BASE_DIR / "data" / rel).read_text(encoding="utf-8"))
        except Exception:
            return None
    d = read("health/drift.json")
    if d and d.get("recent_2w"):
        r = d["recent_2w"]
        lines.append(f"- 実運用(前日夜に出した本番の予報だけ、直近2週間、{d['checked_at'][:10]}時点): {r['n']}件中 川霧{r['fog']}件、F1 {num(r['f1'])}"
                     f"(適合率 {num(r['precision'])}、再現率 {num(r['recall'])})。")
    cal = read("experiments/calibration_check.json")
    if cal:
        lines.append(f"- 確率の補正の比較({cal['generated_at'][:10]}、本番は変えていない): Brier 補正なし {cal['raw']['brier']}、Platt {cal['platt_scores']['brier']}、"
                     f"isotonic {cal['isotonic_scores']['brier']}。40%以上の予報が外れたのは主に10/12・10/13の朝で、霧が出ていても『全体霧』と判定された"
                     "(会津の対象は川霧だけ)例やレンズの水滴の例があり、確率の補正では直らないため採用しなかった。")
    az = read("experiments/aizu_target_check.json")
    if az:
        parts = []
        for k, label in (("A_river_only", "A いま(川霧だけ)"), ("B_aizu_all_fog", "B 会津だけ霧全体(共通モデル)"), ("C_separate_models", "C 地域別の別モデル")):
            v = az.get(k)
            if v:
                br = v.get("by_region") or {}
                parts.append(label + ": " + "、".join(f"{rg} AP {num((br.get(rg) or {}).get('ap'))}" for rg in ("只見川", "会津")))
        if parts:
            lines.append(f"- 会津の予測の対象の比較({az['generated_at'][:10]}): " + " / ".join(parts) + "。会津は川霧だけを対象にしたまま(REGION_TARGET_ENABLED=False)。")
    am = read("experiments/amedas_check.json")
    if am and am.get("overall"):
        o = am["overall"]
        lines.append(f"- アメダスの実測を特徴量に足す比較({am['generated_at'][:10]}): AP {o['A']['ap']}→{o['B']['ap']}、判定『{am.get('verdict')}』。")
    sn = read("experiments/short_nowcast_check.json")
    if sn and (sn.get("by_lead") or {}).get("1"):
        l1 = sn["by_lead"]["1"]
        lines.append(f"- この後3時間の別予測(画像の判定・かすみ度・アメダスから、{sn['generated_at'][:10]}): 1時間後のAP {l1['model']['ap']}"
                     f"(そのまま続くと仮定した予測 {l1['persistence']['ap']}、同じ行での本番の予報 {l1['production_forecast']['ap']})。2〜3時間後は本番の予報と同程度以下。")
    oj = read("experiments/own_judge_check.json")
    if oj and oj.get("test_vs_gemini_labels"):
        lines.append(f"- 自前の画像判定(もう一つの判定): Geminiの判定との一致率 {pct(oj['test_vs_gemini_labels'].get('agreement'))}。川霧の見分けはまだ弱い。")
    lines.append("- 未導入のデータ: 水温(気水温差)・凝結核。湿数(気温露点差)は特徴量に入っている。")
    text = "\n".join(lines)
    _research_ctx_cache.update(at=now, text=text)
    return text

def _build_chat_context():
    """現在の観測・予測状況・サーバー状態をテキストにまとめる(チャットのシステムプロンプトに含める)。"""
    lines = [f"【バージョン】v{_app_version()}"]
    lines.append(
        f"【観測地点一覧】只見川{len(TADAMI_STATION_NAMES)}地点: " + "、".join(TADAMI_STATION_NAMES)
        + f" / 会津{len(AIZU_STATION_NAMES)}地点: " + "、".join(AIZU_STATION_NAMES)
    )
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
            lines.append("現在、川霧・層雲等の現象が出ている地点はありません。")
    else:
        lines.append("現在の観測データは取得できていません。")

    ai_summary = _load_ai_summary()
    if ai_summary and ai_summary.get("overall"):
        lines.append(f"【AIによる状況要約】({ai_summary.get('generated_at')}生成) {ai_summary['overall']}")

    alerts = _upcoming_fog_alerts_safe()
    if alerts:
        lines.append("【今後24時間以内の川霧アラート(「川霧あり」と予測された地点・時刻)】")
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
    global_metrics = fog_model.shared_global_metrics(model_state) if model_state else None
    if global_metrics:
        any_info = next(iter(model_state.values()))
        m, c = global_metrics.get("model") or {}, global_metrics.get("climatology") or {}

        def _pct(v):
            return "-" if v is None else f"{round(100.0 * v, 1)}%"
        lines.append(
            f"【モデル状況】{any_info.get('model_type')}を{len(model_state)}地点で使用"
            f"(学習日時{any_info.get('trained_at')}、しきい値{_pct(global_metrics.get('threshold'))})"
        )
        lines.append(
            f"評価期間({global_metrics.get('test_start')}〜{global_metrics.get('test_end')}、"
            f"{'予報値' if global_metrics.get('input') == 'forecast' else '実測値'}を入力)の成績: "
            f"モデル 川霧F1 {_pct(m.get('f1'))}・recall {_pct(m.get('recall'))}・precision {_pct(m.get('precision'))}"
            f" / 基準(気候値) 川霧F1 {_pct(c.get('f1'))}"
        )
        low_acc = _low_accuracy_stations(model_state, n=3)
        if low_acc:
            lines.append("評価期間のaccuracyが低い地点(下位3件): " + "、".join(low_acc))

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
        system_instruction=CHAT_SYSTEM_INSTRUCTION + "\n\n" + _build_chat_context() + "\n\n" + _build_research_context(),
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
# (2026-10-08に60秒→15分。予測の答え合わせは1時間ごと・予測は1日1回なので、15分遅れても困らない。
# 天気別の集計は1回約10秒かかり、60秒で切れると分析ページを開くたびに待たされていた。
# 期限が切れる前に_warm_caches_foreverが裏で作り直す)
ACCURACY_SUMMARY_CACHE_SECONDS = 900
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
                "SELECT target_datetime, predicted_code, actual_code, station FROM predictions "
                "WHERE actual_code IS NOT NULL AND model_type LIKE ? AND COALESCE(daylight, 1) = 1",
                (prediction_log.CURRENT_MODEL_LIKE,),
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
        for target_dt, predicted_code, actual_code, station in rows:
            weather_row = weather_by_dt.get(target_dt)
            if weather_row is None:
                continue
            # 的中 = 「川霧かどうか」が一致(prediction_log.pyの説明参照)。
            correct = 1 if fog_model.is_fog_code(predicted_code) == fog_model.is_fog_code(actual_code, station) else 0
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


# 川霧クラスのprecision/recall/F1・混同行列・地域比較は、いずれも答え合わせ済みの
# (地点, 予測コード, 実際のコード)の組み合わせ件数(prediction_log.fog_pair_counts)
# という1つの集計結果から計算できるため、accuracy_summaryと同じTTLキャッシュで共有する。
_fog_pair_counts_cache = {"data": None, "at": 0.0}
_fog_pair_counts_cache_lock = threading.Lock()


def _fog_pair_counts_safe():
    now = time.monotonic()
    with _fog_pair_counts_cache_lock:
        cached = _fog_pair_counts_cache["data"]
        if cached is not None and now - _fog_pair_counts_cache["at"] < ACCURACY_SUMMARY_CACHE_SECONDS:
            return cached
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            result = prediction_log.fog_pair_counts(conn)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to read fog pair counts: %s", e)
        return []
    with _fog_pair_counts_cache_lock:
        _fog_pair_counts_cache["data"] = result
        _fog_pair_counts_cache["at"] = now
    return result


def _fog_confusion_stats(pairs, fog_codes=None):
    """(station, predicted_code, actual_code, n)のリストから、「川霧」を陽性クラスとみなした
    TP(的中)/FP(空振り: 川霧じゃないのに川霧と予測)/FN(見逃し: 川霧なのに川霧じゃないと予測)/TNと、
    precision/recall/F1を計算する。予測は二値(1=川霧あり)、旧方式の行は現象コードで、
    どちらもSUMMARY_FOG_CODES(二値の1を含む)で川霧かどうかを判定する。"""
    fog_codes = SUMMARY_FOG_CODES if fog_codes is None else fog_codes
    tp = fp = fn = tn = 0
    for row in pairs:
        n = row["n"]
        # predicted_code/actual_codeは2コード同時観測(2026-09対応)のパック済み値の
        # こともあるが、川霧(SUMMARY_FOG_CODES)は家族グループの並び上、2コード同時でも
        # 必ず主コード側に入るため、primary_phenom_codeで取り出すだけで正しく判定できる。
        pred_fog = primary_phenom_code(row["predicted_code"]) in fog_codes
        actual_fog = primary_phenom_code(row["actual_code"]) in fog_codes
        if pred_fog and actual_fog:
            tp += n
        elif pred_fog and not actual_fog:
            fp += n
        elif actual_fog:
            fn += n
        else:
            tn += n
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision, "recall": recall, "f1": f1}


def _accuracy_station_metrics_safe():
    """地点ごとに川霧クラスのprecision/recall/F1をまとめる(地点別的中率テーブルに
    列を足すためのもの)。戻り値: {地点名: _fog_confusion_statsの戻り値}"""
    by_station = {}
    for row in _fog_pair_counts_safe():
        by_station.setdefault(row["station"], []).append(row)
    return {station: _fog_confusion_stats(rows) for station, rows in by_station.items()}


def _accuracy_region_summary_safe():
    """地域(会津/只見川)ごとに、的中率と川霧クラスのprecision/recall/F1をまとめる
    (会津は観測を始めてまだ日が浅く学習データが少ないため、只見川との差を
    一目で見せられるようにする)。"""
    by_region = {}
    for row in _fog_pair_counts_safe():
        region = STATION_REGION.get(row["station"], "不明")
        by_region.setdefault(region, []).append(row)
    result = []
    for region, rows in sorted(by_region.items()):
        n = sum(r["n"] for r in rows)
        correct = sum(
            r["n"] for r in rows
            if fog_model.is_fog_code(r["predicted_code"]) == fog_model.is_fog_code(r["actual_code"], r.get("station"))
        )
        result.append({"region": region, "n": n, "correct": correct, **_fog_confusion_stats(rows)})
    return result


def _confusion_matrix_from_pairs(pairs):
    """prediction_log.fog_pair_countsの集計結果から、「川霧かどうか」の二値の混同行列
    (labels, matrix)を組み立てる(行=実際、列=予測)。予測は二値(1=川霧あり)、旧方式の
    行は現象コードで、どちらもfog_model.is_fog_codeで川霧かどうかを判定する。"""
    matrix = np.zeros((2, 2), dtype=int)
    for row in pairs:
        if row["predicted_code"] is None or row["actual_code"] is None:
            continue
        a = int(fog_model.is_fog_code(row["actual_code"], row.get("station")))
        p = int(fog_model.is_fog_code(row["predicted_code"]))
        matrix[a, p] += row["n"]
    if not matrix.sum():
        return [], np.zeros((0, 0))
    return list(fog_model.BINARY_LABELS), matrix


_fog_prob_buckets_cache = {"data": None, "at": 0.0}
_fog_prob_buckets_cache_lock = threading.Lock()


def _fog_prob_buckets_safe(station=None):
    """predicted_fog_prob(0.05刻み)ごとに、実際に川霧だったか否かの件数を集計する。
    しきい値を変えた場合のprecision/recall/F1を後から計算するための元データ。
    station未指定(全地点合算、/accuracyページ用)の場合のみ短時間キャッシュする
    (地点別は station_detail 表示のたびに変わるstation引数が多様で、キャッシュの
    ヒット率が低いため素通しにする)。"""
    if station is None:
        now = time.monotonic()
        with _fog_prob_buckets_cache_lock:
            cached = _fog_prob_buckets_cache["data"]
            if cached is not None and now - _fog_prob_buckets_cache["at"] < ACCURACY_SUMMARY_CACHE_SECONDS:
                return cached
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            result = prediction_log.fog_prob_bucket_counts(conn, station=station)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to read fog probability buckets: %s", e)
        return []
    if station is None:
        with _fog_prob_buckets_cache_lock:
            _fog_prob_buckets_cache["data"] = result
            _fog_prob_buckets_cache["at"] = now
    return result


# しきい値ごとの集計で、そのしきい値以上と判定された件数がこれ未満の場合はprecision/F1を
# 計算しない(Noneのままにする)。特に地点別のグラフはしきい値を上げるほど対象件数が
# 数件まで減り、precisionが0%/100%の間で激しくジグザグしてグラフが見苦しくなるため
# (2026-09-12、station_threshold_chartのグラフが「気持ち悪い」との指摘を受けて追加)。
MIN_SAMPLES_FOR_THRESHOLD_PRECISION = 10


def _threshold_sweep_stats(buckets, fog_codes=None):
    """しきい値ごとのprecision/recall/F1を計算する。fog_model.FOG_THRESHOLD_GRIDと
    同じ刻みを使うので、学習時に選ばれたしきい値と同じ基準で比較できる。戻り値: [{"threshold":, "precision":, "recall":, "f1":}, ...]
    """
    fog_codes = SUMMARY_FOG_CODES if fog_codes is None else fog_codes
    if not buckets:
        return []
    # actual_codeは2コード同時観測(2026-09対応)のパック済み値のこともあるため、
    # primary_phenom_codeで主コードを取り出してから判定する(川霧は家族グループの並び上、
    # 2コード同時でも必ず主コード側に入る)。
    total_fog = sum(b["n"] for b in buckets if primary_phenom_code(b["actual_code"]) in fog_codes)
    if total_fog == 0:
        return []
    results = []
    for t in fog_model.FOG_THRESHOLD_GRID:
        tp = fp = 0
        for b in buckets:
            if b["prob"] < t:
                continue
            if primary_phenom_code(b["actual_code"]) in fog_codes:
                tp += b["n"]
            else:
                fp += b["n"]
        n_predicted_positive = tp + fp
        if n_predicted_positive >= MIN_SAMPLES_FOR_THRESHOLD_PRECISION:
            precision = tp / n_predicted_positive
        else:
            precision = None
        recall = tp / total_fog
        # 「if precision and recall」だとprecision/recallが正当な0.0の時も偽扱いになり
        # F1が本来計算できるのに欠落してしまうバグがあったため、is not Noneで判定する。
        if precision is not None and recall is not None and (precision + recall) > 0:
            f1 = 2 * precision * recall / (precision + recall)
        else:
            f1 = None
        results.append({"threshold": float(t), "precision": precision, "recall": recall, "f1": f1})
    return results


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
        raw = encode_phenomena_cell(phenom_row.get(name))
        label, color, text_color = _phenom_label_color(raw)
        primary, secondary = decode_phenom_value(raw)
        stations.append({
            "name": name, "label": label, "color": color, "text_color": text_color,
            "code": primary, "code2": secondary,
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


def _home_map_data():
    """ホーム(地図が中心、2026-10-12)の地図と横のまとめ。地点ごとに いま(最新の観測)・1時間後(short_nowcast)・
    今日/明日の出やすさ(公開ページと同じ段階 0〜3)・24時間以内の予測アラートを持たせ、画面で切り替える。"""
    observations = _latest_observations()
    now_by_name = {s["name"]: s for s in (observations or {}).get("stations", [])}
    alerts = _upcoming_fog_alerts_safe()
    alert_first = {}
    for a in alerts:
        alert_first.setdefault(a["station"], a["target_datetime"])
    forecast = _public_forecast_safe() or {}
    fc_by_name = {s["name"]: s for s in forecast.get("stations") or []}
    try:
        import short_nowcast
        nc = short_nowcast.latest()
    except Exception as e:
        log.warning("Home: short nowcast unavailable: %s", e)
        nc = None
    nc_hour = None
    if nc:
        nc_hour = (datetime.strptime(nc["issued_at"], "%Y-%m-%d %H:%M:%S") + timedelta(hours=1)).hour
    points = []
    for name in STATION_NAMES:
        info = CAMERA_INFO.get(name, {})
        if info.get("lat") is None:
            continue
        cur = now_by_name.get(name)
        fc = fc_by_name.get(name) or {}
        days = fc.get("days") or []
        p1 = (nc or {}).get("stations", {}).get(name, {}).get(str(nc_hour)) if nc_hour is not None else None
        points.append({
            "name": name, "region": STATION_REGION.get(name) or "", "lat": info["lat"], "lon": info["lon"],
            "url": url_for("station_detail", name=name),
            "now": cur["label"] if cur else None, "color": cur["color"] if cur else "#bdc3c7",
            "fog": bool(cur and cur.get("code") in fog_features.target_codes(name)),
            "alert": alert_first.get(name),
            "p1": round(p1, 3) if p1 is not None else None,
            "lv": [d["max"] for d in days[:2]],
        })
    fog_now = [p for p in points if p["fog"]]
    soon = sorted((p for p in points if p["p1"] is not None and not p["fog"]), key=lambda p: -p["p1"])
    push_thr = (nc or {}).get("push_threshold")
    return {
        "points": points,
        "fog_now": fog_now,
        "observed_at": observations["datetime"].strftime("%H:%M") if observations else None,
        "soon": [p for p in soon if push_thr is not None and p["p1"] >= push_thr][:5],
        "soon_hour": nc_hour, "nc_threshold": (nc or {}).get("threshold"), "nc_push_threshold": push_thr,
        "alerts": alerts,
        "days": forecast.get("days") or [],
        "tomorrow_high": [p["name"] for p in points if len(p["lv"]) > 1 and p["lv"][1] >= 3],
    }


_home_cache = {"at": 0.0, "value": None}
_home_cache_lock = threading.Lock()
HOME_CACHE_SECONDS = 60


def _home_map_data_cached():
    """ホームの地図のデータは全員同じ(ログインの有無や役割で変わらない)なので、1分だけ使い回す
    (観測は毎時、1時間後の見込みも毎時なので、1分遅れても見え方は変わらない)。"""
    now = time.monotonic()
    with _home_cache_lock:
        if _home_cache["value"] is not None and now - _home_cache["at"] < HOME_CACHE_SECONDS:
            return _home_cache["value"]
    value = _home_map_data()
    with _home_cache_lock:
        _home_cache.update(at=time.monotonic(), value=value)
    return value

@app.route("/")
def dashboard():
    # 「最初に開くページ」(設定画面)。外から開いたとき(ホーム画面のアイコン・ブックマーク等で
    # 参照元が無いか別サイト)だけ移る。サイト内のメニューからダッシュボードを開いたときはそのまま。
    # ホスト名だけで比べる(:8443はnginxがポート無しのHostで渡してくるため、ポートまで比べると一致しない)
    referrer_host = urlparse(request.referrer or "").hostname or ""
    if referrer_host != request.host.split(":")[0] and not request.args:
        start_url = _start_page_url(_current_user_settings(), (g.get("current_user") or {}).get("favorite_stations", ""))
        if start_url:
            return redirect(start_url)
    try:
        _check_economy_anomalies()
    except Exception as e:
        log.warning("Economy anomaly check failed: %s", e)
    try:
        _charge_due_subscriptions()
    except Exception as e:
        log.warning("Subscription billing check failed: %s", e)
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
    summary_files_by_region = {}
    if latest_pred_date:
        pred_dir = PRED_DIR / latest_pred_date
        pred_files = list(pred_dir.iterdir())
        summary_file = next((p.name for p in pred_files if SUMMARY_RE.match(p.name)), None)
        for region_label, region_re in SUMMARY_REGION_RE.items():
            match = next((p.name for p in pred_files if region_re.match(p.name)), None)
            if match:
                summary_files_by_region[region_label] = match

    active_announcements = _active_announcements_safe()
    if active_announcements:
        conn = None
        if session.get("user_id"):
            try:
                conn = _announcements_conn()
            except Exception as e:
                log.warning("Failed to open announcements db: %s", e)
        for ann in active_announcements:
            ann["already_responded"] = bool(
                conn and announcements.has_responded(conn, ann["id"], session.get("user_id"))
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
    try:
        home = _home_map_data_cached()
    except Exception as e:
        log.warning("Home map data failed: %s", e)
        home = None
    # お気に入りの地点(2026-10-13): プロフィールで選んだ地点を、いまの状態と今日の見込みつきでホームの上に出す
    fav_names = [n for n in ((g.get("current_user") or {}).get("favorite_stations") or "").split(",") if n in STATION_NAMES]
    obs_now = {s["name"]: s for s in (_latest_observations() or {}).get("stations", [])}
    pts = {p["name"]: p for p in (home or {}).get("points", [])}
    favorites = [{"name": n, "now": obs_now.get(n), "lv": (pts.get(n) or {}).get("lv") or [], "alert": (pts.get(n) or {}).get("alert"),
                  "fog": bool((pts.get(n) or {}).get("fog"))} for n in fav_names]
    fog_names = {p["name"] for p in (home or {}).get("points", []) if p.get("fog")}
    return render_template(
        "dashboard.html",
        home=home,
        favorites=favorites,
        has_favorites_setting=bool(session.get("user_id")),
        fog_names=fog_names,
        dashboard_theme_class=dashboard_theme_class,
        observations=_latest_observations(),
        accuracy=_accuracy_summary_safe(),
        alerts=_upcoming_fog_alerts_safe(),
        latest_image_date=latest_image_date,
        latest_image_hour=latest_image_hour,
        latest_images=latest_images,
        latest_pred_date=latest_pred_date,
        summary_file=summary_file,
        summary_files_by_region=summary_files_by_region,
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


_DAILY_FOG_COUNTS_CACHE = {"at": 0.0, "value": {}}
DAILY_FOG_COUNTS_CACHE_SECONDS = 600


def _daily_fog_counts():
    """日付ごとの、川霧(現象コード1〜3)をその日に1回でも観測した地点の数({"YYYY-MM-DD": 地点数}、0の日は省く)。
    観測・画像・予測の日付カレンダーで、霧の多い日ほど色を濃くするのに使う(_date_month_groups.html)。
    読み込みが重いので10分だけ覚えておく。失敗したら空(カレンダーは色分けなしで出る)。"""
    now = time.time()
    if now - _DAILY_FOG_COUNTS_CACHE["at"] < DAILY_FOG_COUNTS_CACHE_SECONDS:
        return _DAILY_FOG_COUNTS_CACHE["value"]
    value = {}
    try:
        import pandas as pd
        _, phenom_df, station_names = fog_data.load_river_fog_data()
        cols = [c for c in phenom_df.columns if c in set(station_names)]
        codes = phenom_df[cols].apply(pd.to_numeric, errors="coerce")
        counts = codes.isin([1, 2, 3]).groupby(phenom_df["datetime"].dt.strftime("%Y-%m-%d")).any().sum(axis=1)
        value = {d: int(n) for d, n in counts.items() if n > 0}
    except Exception as e:
        log.warning("Failed to count fog stations per day: %s", e)
    _DAILY_FOG_COUNTS_CACHE.update(at=now, value=value)
    return value


app.jinja_env.globals["daily_fog_counts"] = _daily_fog_counts


def _num_or_none(value):
    """表示用: 数値ならfloat、欠測(None/NaN/NaT)ならNone。iterrows()の行は日時列と
    混在したobject型になり、欠測がNaNではなくNaTになることがある。テンプレートの
    "%.1f"|formatはNaTを受け付けず、観測データの日別ページがエラーになっていた
    (2026-08-28〜10-02に19回)ため、ここで正規化する。"""
    if value is None or value != value:  # NaNもNaTも自分自身と等しくならない
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
            "temp": _num_or_none(main_row.get("気温(℃)")),
            "humidity": _num_or_none(main_row.get("相対湿度(％)")),
            "precip": _num_or_none(main_row.get("降水量(mm)")),
            "visibility": _num_or_none(main_row.get("視程(km)")),
            "cells": cells,
        })

    prev_date, next_date = _adjacent_dates("observations", date)
    return render_template("observations_date.html", date=date, station_names=station_names, rows=rows,
                           prev_date=prev_date, next_date=next_date)


@app.route("/observations/<date>/chart.json")
def observations_date_chart(date):
    date = _validate_date(date)

    def render():
        try:
            main_df, _, _ = fog_data.load_river_fog_data()
        except Exception as e:
            log.warning("Failed to load observation data: %s", e)
            main_df = None
        day_main = None
        if main_df is not None:
            day_main = main_df[main_df["datetime"].dt.strftime("%Y-%m-%d") == date].sort_values("datetime")
        return _render_weather_timeseries_plotly(day_main, date)

    data = _cached_chart_json(f"observations_date_chart:{date}", [WEATHER_DB_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/download/xlsx")
def download_xlsx():
    if not WEATHER_DB_PATH.exists():
        abort(404)
    conn = weather_store.connect(WEATHER_DB_PATH)
    try:
        weather_store.export_to_xlsx(
            conn, EXPORT_XLSX_PATH, CAMERAS_CSV, TEMPLATE_XLSX, station_names=TADAMI_STATION_NAMES,
        )
    finally:
        conn.close()
    return send_file(EXPORT_XLSX_PATH, as_attachment=True, download_name="只見川.xlsx")


@app.route("/download/xlsx/aizu")
def download_xlsx_aizu():
    """会津地方(2026-09追加)分の観測データ。只見川.xlsxと同じ列レイアウトだが、
    地点名の列は会津の20地点だけになる(download_xlsx()の会津版)。"""
    if not WEATHER_DB_PATH.exists():
        abort(404)
    conn = weather_store.connect(WEATHER_DB_PATH)
    try:
        weather_store.export_to_xlsx(
            conn, AIZU_EXPORT_XLSX_PATH, CAMERAS_CSV, AIZU_TEMPLATE_XLSX, station_names=AIZU_STATION_NAMES,
        )
    finally:
        conn.close()
    return send_file(AIZU_EXPORT_XLSX_PATH, as_attachment=True, download_name="会津.xlsx")


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
    prev_date, next_date = _adjacent_dates("images", date)
    return render_template("images_date.html", date=date, hours=_sorted_subdirs(date_dir),
                           prev_date=prev_date, next_date=next_date)


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
    """観測者・管理者(担当地域のみ)が、過去のアーカイブ画像に対するGeminiの現象判定を
    その場で直接訂正する。station_correct_observationと同じ承認不要の即時訂正だが、
    こちらは「今」ではなく任意の過去の日時を対象にできる点が異なる。訂正内容は
    phenomena_correctionsに記録するのみで、xlsx上の値そのものは書き換えない
    (次回の再学習でfog_model.py側がこの記録を使って除外・重み付けする設計にしやすいよう、
    生データと訂正履歴を分けて持つ)。
    2026-09-12: 元々役割チェックが無く、ログインしていれば誰でも(一般ユーザーでも)
    任意の地点の判定を書き換えられてしまっていた(station_correct_observationは
    _can_correct_stationで守られているのに、こちらは抜けていた)ため、同じチェックを
    追加した。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    date = _validate_date(date)
    hhmm = _validate_hhmm(hhmm)
    station = request.form.get("station", "")
    if station not in STATION_NAMES:
        abort(404)
    if not _can_correct_station(session.get("role"), station):
        abort(403)
    corrected_code = request.form.get("corrected_code", type=int)
    corrected_code2 = request.form.get("corrected_code2", type=int)
    if corrected_code is None:
        return _finish(
            "images_hour", "訂正後の現象コードを選んでください。", success=False, date=date, hhmm=hhmm,
        )
    codes = [corrected_code] if corrected_code2 is None else [corrected_code, corrected_code2]
    try:
        packed_corrected = encode_phenom_codes(codes)
    except PhenomCodeError as e:
        return _finish(
            "images_hour", f"現象コードの組み合わせが不正です: {e}", success=False, date=date, hhmm=hhmm,
        )

    target_dt_str = f"{date} {hhmm[:2]}:{hhmm[2:]}:00"
    codes_by_station, _ = _phenom_codes_for_datetime(target_dt_str)
    original_code = codes_by_station.get(station)

    conn = _users_conn()
    try:
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        users.add_phenomena_correction(conn, station, target_dt_str, original_code, packed_corrected, my_id, now_str)
        gem_note = ""
        if packed_corrected != original_code:
            if users.grant_gems(
                conn, my_id, f"direct_correction:{station}:{target_dt_str}",
                GEM_REWARD_DIRECT_CORRECTION, now_str,
            ):
                gem_note = f"(+{GEM_REWARD_DIRECT_CORRECTION}ジェム)"
        _record_audit(
            "画像判定を訂正",
            f"{station} {target_dt_str}: {format_phenom_display(original_code) or '-'} → "
            f"{format_phenom_display(packed_corrected) or '-'}",
        )
    finally:
        conn.close()
    return _finish("images_hour", f"訂正を記録しました{gem_note}。", date=date, hhmm=hhmm)


@app.route("/images/<date>/<hhmm>/file/<path:filename>")
def images_file(date, hhmm, filename):
    date = _validate_date(date)
    hhmm = _validate_hhmm(hhmm)
    return send_from_directory(IMAGE_DIR / date / hhmm, filename)


THUMB_DIR = BASE_DIR / "data" / "thumbs"
THUMB_WIDTH = 400          # 一覧のカードは横300〜400px前後
THUMB_QUALITY = 62


@app.route("/images/<date>/<hhmm>/thumb/<path:filename>")
def images_thumb(date, hhmm, filename):
    """カメラ画像の縮小版(2026-10-14、軽量化)。一覧・比較・観測報告などの小さい表示用(原寸 約74KB → 約15KB)。
    初めて求められたときに作って data/thumbs/ に置き、以後はそれを返す(元の画像は変わらないので作り直さない)。
    元の画像が無ければ404。押して大きく見るときは images_file(原寸)を使う。"""
    from werkzeug.security import safe_join
    date = _validate_date(date)
    hhmm = _validate_hhmm(hhmm)
    src = safe_join(str(IMAGE_DIR / date / hhmm), filename)
    if src is None or not os.path.isfile(src) or not filename.lower().endswith(".jpg"):
        abort(404)
    out_dir = THUMB_DIR / f"w{THUMB_WIDTH}q{THUMB_QUALITY}" / date / hhmm  # 大きさを変えたら作り直されるよう、フォルダを分ける
    out = out_dir / filename
    if not out.is_file() or out.stat().st_mtime < os.path.getmtime(src):
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as im:
                im = im.convert("RGB")
                if im.width > THUMB_WIDTH:
                    im = im.resize((THUMB_WIDTH, round(im.height * THUMB_WIDTH / im.width)), Image.LANCZOS)
                tmp = out.with_suffix(f".tmp{os.getpid()}")
                im.save(tmp, "JPEG", quality=THUMB_QUALITY, optimize=True, progressive=True)
                os.replace(tmp, out)
        except Exception as e:  # noqa: BLE001
            log.warning("Thumbnail failed for %s: %s", src, e)
            return send_from_directory(IMAGE_DIR / date / hhmm, filename)
    resp = send_from_directory(out_dir, filename)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


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
    # 地点数が増えていくとメモリ上に丸ごと構築するZIPも際限なく大きくなるため、
    # 合計サイズに上限を設ける(2026-09-13の監査で指摘)。
    total_bytes = sum(f.stat().st_size for f in files)
    if total_bytes > IMAGES_HOUR_ZIP_MAX_BYTES:
        abort(413)

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

    prev_date, next_date = _adjacent_dates("predictions", date)
    return render_template(
        "predictions_date.html", date=date,
        summary_file=summary_file, csv_file=csv_file, stations=stations,
        prev_date=prev_date, next_date=next_date,
    )


@app.route("/predictions/<date>/figure/<path:filename>")
def predictions_figure(date, filename):
    """予報グラフ・全地点サマリー(fog_graphsが書き出したPlotlyのHTML)から図を取り出してJSONで返す。
    ページでは?format=pngを付けて画像で表示する(_chart_as_png)。過去の日付のファイルにもそのまま効く。"""
    date = _validate_date(date)
    if not filename.lower().endswith(".html"):
        abort(404)
    from werkzeug.security import safe_join
    path = safe_join(str(PRED_DIR / date), filename)
    if path is None or not os.path.isfile(path):
        abort(404)
    try:
        with open(path, encoding="utf-8") as f:
            fig = mpl_render.figure_from_plotly_html(f.read())
    except (OSError, ValueError) as e:
        log.warning("Failed to read the figure in %s: %s", path, e)
        abort(404)
    resp = Response(json.dumps(fig, ensure_ascii=False), mimetype="application/json")
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@app.route("/predictions/<date>/file/<path:filename>")
def predictions_file(date, filename):
    date = _validate_date(date)
    if filename.lower().endswith(".html"):
        # 予報グラフのHTMLは、サイトのダーク/ライト設定に合わせるスクリプトを差し込んで返す
        # (chart_style.IFRAME_THEME_SCRIPT。過去の日付のファイルにも効くよう配信時に行う)。
        from werkzeug.security import safe_join
        path = safe_join(str(PRED_DIR / date), filename)
        if path is None or not os.path.isfile(path):
            abort(404)
        with open(path, encoding="utf-8") as f:
            html = chart_style.inject_iframe_theme(f.read())
        resp = Response(html, mimetype="text/html")
        resp.headers["Cache-Control"] = "private, max-age=300"
        return resp
    return send_from_directory(PRED_DIR / date, filename)


# --- Spotlight風の検索(Ctrl+K / ⌘K、web_static/spotlight.js) -------------------------------
# 検索の候補(ページ・地点・管理画面・日付ページのURLの型)をbase.htmlにJSONで埋め込む。
# 存在しないendpointが混ざっても壊れないよう、url_forに失敗したものは黙って除く。
SPOTLIGHT_PAGES = (
    ("ホーム(地図・いまの川霧)", "dashboard", "ホーム トップ ダッシュボード 地図"),
    ("設定", "settings_page", "せってい 通知 プッシュ テーマ ダーク 文字 大きさ プライバシー settings"),
    ("今朝の川霧", "gallery_page", "ギャラリー 写真 けさ 朝 gallery"),
    ("川霧の流れ図・見頃カレンダー", "flow_page", "流れ 広がり 見頃 みごろ カレンダー 時刻 アニメーション flow"),
    ("月ごとのレポート", "reports_page", "レポート 報告 PDF 月報 report"),
    ("研究用の書き出し", "export_page", "書き出し エクスポート ダウンロード CSV ZIP データ 研究 export"),
    ("地点一覧", "stations_list", "地点 一覧 カメラ"),
    ("地点比較", "compare_stations", "比較"),
    ("地図", "map_page", "マップ map"),
    ("シミュレーター", "simulator_page", "シミュレーション 予測 試す"),
    ("観測アーカイブ", "observations_dates", "観測データ 過去"),
    ("カメラ画像", "images_dates", "画像 写真 カメラ"),
    ("予測結果", "predictions_dates", "予報 予測グラフ"),
    ("予測精度", "accuracy_page", "精度 的中率 F1 特徴量 重要度 分析"),
    ("気象と川霧のグラフ", "weather_fog_graph_page", "分析 グラフ 気象"),
    ("観測報告", "observation_reports", "報告 訂正"),
    ("コミュニティ", "members_list", "メンバー 仲間 チャット"),
    ("AIチャット", "chat_page", "AI チャット 質問 Gemini"),
    ("ゲーム", "game_home", "ゲーム ショップ ポイント"),
    ("デイリーボーナス", "game_daily", "ゲーム ログインボーナス 毎日"),
    ("ショップ", "game_shop", "ゲーム 買い物 アイテム テーマ"),
    ("川霧予想ゲーム", "game_gamble", "ゲーム 予想 賭け"),
    ("意見箱", "community_feedback", "フィードバック 要望 意見"),
    ("メッセージ", "dm_inbox", "DM ダイレクトメッセージ 受信箱"),
    ("グループ", "group_home", "グループ チャット 投票"),
    ("共有ファイル", "shared_files_page", "ファイル 共有 アップロード ダウンロード"),
    ("期間レポート", "period_report", "レポート 期間 集計 まとめ"),
    ("川霧の時刻分布", "river_fog_histogram_page", "ヒストグラム 分布 時刻 分析"),
    ("更新履歴", "changelog_page", "バージョン 変更履歴"),
    ("公開ページ(出やすさ)", "public_forecast", "公開 一般向け 出やすさ"),
    ("この情報について(公開)", "public_about", "公開 説明 仕組み"),
)
SPOTLIGHT_ADMIN_PAGES = (
    ("管理者トップ", "admin_home", "管理 ログ"),
    ("システムの健康", "admin_health", "健康 チェック 巡回 テスト health"),
    ("エラー/警告", "admin_errors", "エラー 警告"),
    ("サービス状態", "admin_status", "サービス 状態 稼働"),
    ("モデル状況", "admin_models", "モデル 学習"),
    ("カメラ稼働率", "admin_camera_health", "カメラ 稼働"),
    ("Gemini状況", "admin_gemini", "Gemini AI 無料枠"),
    ("精度レポート", "admin_accuracy_report", "精度 レポート"),
    ("ディスク使用量", "admin_disk", "ディスク 容量"),
    ("DB管理", "admin_db", "データベース DB"),
    ("アカウント管理", "admin_accounts", "アカウント ユーザー 承認"),
    ("設定", "admin_settings", "設定"),
    ("お知らせ管理", "admin_announcements", "お知らせ 告知 アナウンス"),
    ("監査ログ", "admin_audit_log", "監査 ログ 操作履歴"),
    ("コックピット", "admin_cockpit", "概要 一覧 状況"),
    ("仮検証", "admin_experiment", "実験 検証 モデル比較"),
    ("ラベルの検証", "admin_experiment_labels", "ラベル 判定 検証"),
    ("意見箱(管理)", "admin_feedback", "フィードバック 要望"),
    ("メンテナンス", "admin_maintenance", "メンテナンス 停止"),
    ("ポイント管理", "admin_points", "ポイント ゲーム"),
    ("ウイルス検査(ClamAV)", "admin_clamav", "ウイルス ClamAV 検査"),
    ("環境変数", "admin_env", "環境 変数 env"),
    ("SMB共有", "admin_smb", "SMB 共有 ネットワーク"),
    ("Tailscale", "admin_tailscale", "Tailscale ネットワーク 公開"),
    ("ターミナル", "admin_terminal", "ターミナル コマンド シェル"),
)
SPOTLIGHT_DATE_RULES = (
    ("観測データ", "/observations/<date>", "observations"),
    ("カメラ画像", "/images/<date>", "images"),
    ("予測結果", "/predictions/<date>", "predictions"),
)


def _spotlight_items():
    items = []

    def add(label, endpoint, kind, keywords="", sub="", **values):
        try:
            items.append({"label": label, "href": url_for(endpoint, **values), "kind": kind,
                          "keywords": keywords, "sub": sub})
        except Exception:
            pass

    for label, endpoint, keywords in SPOTLIGHT_PAGES:
        add(label, endpoint, "page", keywords)
    for name in STATION_NAMES:
        region = STATION_REGION.get(name) or ""
        add(name, "station_detail", "station", region, sub=region, name=name)
    if session.get("role") == "admin" and not _is_funnel_request():
        for label, endpoint, keywords in SPOTLIGHT_ADMIN_PAGES:
            add(label, endpoint, "admin", keywords)
    dates = []
    for label, rule_text, kind in SPOTLIGHT_DATE_RULES:
        rule = next((r for r in app.url_map.iter_rules() if r.rule == rule_text), None)
        if rule is not None:
            try:
                dates.append({"label": label, "kind": kind, "href": url_for(rule.endpoint, date="0000-00-00")})
            except Exception:
                pass
    links = {}
    # 地点の公開ページ・最新のカメラ画像は、ブラウザ側でこのURLの型から作る(全ページに埋め込む量を増やさない)
    for key, endpoint, values in (("logout", "logout", {}),
                                  ("images_hour", "images_hour", {"date": "0000-00-00", "hhmm": "0000"}),
                                  ("public_station", "public_station", {"name": "__NAME__"})):
        try:
            links[key] = url_for(endpoint, **values)
        except Exception:
            pass
    return {"items": items, "dates": dates, "links": links, "dates_url": url_for("calendar_dates")}


@app.route("/api/calendar-dates")
def calendar_dates():
    """カレンダー(web_static/calendar.js)とCtrl+Kの日付表示用: 種類ごとにデータがある日付(昇順)と、
    最新のカメラ画像の日付・時刻。ページを開くたびではなく、カレンダーを開いたときだけ読む。"""
    out = {kind: _available_dates(kind) for _, _, kind in SPOTLIGHT_DATE_RULES}
    latest_image = None
    if out["images"]:
        last = out["images"][-1]
        hours = _sorted_subdirs(IMAGE_DIR / last)
        if hours:
            latest_image = {"date": last, "hhmm": hours[0]}
    out["latest_image"] = latest_image
    out["today"] = datetime.now(JST).strftime("%Y-%m-%d")
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


@app.route("/api/spotlight-items")
def spotlight_items_api():
    """Ctrl+Kの検索の候補(2026-10-14、軽量化): 以前は全ページに約20KB埋め込んでいたが、検索を開いたときだけ読む
    (base.htmlの小さな読み込み係が、これと spotlight.js を取ってから開く)。管理者向けの候補が入るので人ごとに変わる。"""
    resp = jsonify(_spotlight_items())
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@app.context_processor
def _inject_spotlight():
    return {"spotlight_items": _spotlight_items}


def _station_card_data():
    """地点一覧(天気アプリの都市リスト風)のカード用データ。"""
    observations = None
    try:
        observations = _latest_observations()
    except Exception as e:
        log.warning("Station cards: failed to load observations: %s", e)
    current_by_name = {s["name"]: s for s in observations["stations"]} if observations else {}
    forecast = _public_forecast_safe() or {}
    today_by_name = {s["name"]: s["days"][0] for s in forecast.get("stations", []) if s.get("days")}
    temps = {}
    try:
        conn = station_weather.connect(STATION_WEATHER_DB_PATH)
        try:
            for name in STATION_NAMES:
                row = station_weather.load_latest_station_weather(conn, name)
                if row is not None and row["temperature"] is not None:
                    temps[name] = row["temperature"]
        finally:
            conn.close()
    except Exception as e:
        log.warning("Station cards: failed to load station temperatures: %s", e)
    cards = {}
    for name in STATION_NAMES:
        cur = current_by_name.get(name)
        today = today_by_name.get(name) or {"max": 0, "hours": {}}
        high_hours = sorted(h for h, lv in today["hours"].items() if lv == 3)
        cards[name] = {
            "wx": _weather_scene(name, cur["code"] if cur else None),
            "label": cur["label"] if cur else "観測なし",
            "temp": temps.get(name),
            "today_level": today["max"],
            "today_label": PUBLIC_LEVELS[today["max"]]["label"] if today["max"] else None,
            "high_hours": (f"{high_hours[0]}時" + (f"〜{high_hours[-1]}時" if len(high_hours) > 1 else "")) if high_hours else None,
        }
    observed_at = observations["datetime"].strftime("%m/%d %H:%M") if observations else None
    return cards, observed_at, current_by_name


@app.route("/stations")
def stations_list():
    cards, observed_at, current_by_name = _station_card_data()
    return render_template(
        "stations_list.html", station_names=STATION_NAMES, current_by_name=current_by_name,
        station_region=STATION_REGION, station_cards=cards, observed_at=observed_at,
        favorite_stations=_current_user_favorite_stations(),
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
        latest_images = _station_timelapse_images(name, max_items=1)
        entries.append({
            "name": name,
            "region": STATION_REGION.get(name),
            "current": current_by_name.get(name),
            "latest_pred_date": latest_pred_date,
            "forecast_file": forecast_file,
            "accuracy": accuracy_by_station.get(name),
            "days": _station_days(name),
            "image": latest_images[-1] if latest_images else None,
        })

    return render_template(
        "compare.html", station_names=STATION_NAMES, selected=selected, entries=entries, station_region=STATION_REGION,
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
    # 地点を押したときの小画面(2026-10-13): 最新の時刻のカメラ画像と、今日の出やすさ
    latest_img = {}
    latest_date = next(iter(_sorted_subdirs(IMAGE_DIR)), None)
    if latest_date:
        for hh in _sorted_subdirs(IMAGE_DIR / latest_date)[:3]:
            for f in (IMAGE_DIR / latest_date / hh).glob("*.jpg"):
                if f.stem not in latest_img:
                    latest_img[f.stem] = url_for("images_file", date=latest_date, hhmm=hh, filename=f.name)
    fc = {s["name"]: s for s in (_public_forecast_safe() or {}).get("stations") or []}

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
            "region": STATION_REGION.get(name),
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
            "region": STATION_REGION.get(name) or "",
            "image": latest_img.get(name),
            "today": ((fc.get(name) or {}).get("days") or [{}])[0].get("max", 0),
        })

    return render_template(
        "map.html", station_names=STATION_NAMES, entries=entries,
        current_by_name=current_by_name, map_points=map_points,
        has_alert=bool(alert_stations),
    )


@app.route("/simulator")
def simulator_page():
    """What-ifシミュレーター: 地点を選び、気象条件・時刻を手動で入力すると、その地点が
    現在採用しているモデル(fog_model.simulate_station)でその場で川霧予測を試算する。
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
        is_fog, fog_prob = fog_model.simulate_station(info, station, lat, lon, weather, dt)
        is_daylight = fog_features_is_daylight(dt, lat, lon)
        # 「川霧あり」は観測の「川霧」(コード2)と同じ色で表示する。
        _, color, text_color = _phenom_label_color(2 if is_fog else 0)
        result = {
            "label": fog_model.BINARY_LABELS[is_fog], "is_fog": bool(is_fog), "is_daylight": is_daylight,
            "color": color, "text_color": text_color,
            "fog_prob": fog_prob, "fog_threshold": info.get("fog_threshold"),
            "model_type": fog_model.normalize_model_type_label(info.get("model_type")),
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
    観測・判定ミスの疑いとして検知する。以前は即座に自動訂正していたが、誤検知の
    リスクがあるため、現在は observation_reports の確認待ちキュー(source="auto_detected")
    に積むだけにとどめ、実際の訂正は管理者・観測者が/observation_reportsで前後の画像を
    見て判断してから行う。地点別ページを開くたびにその地点だけ軽く走査する
    (全地点を毎回スキャンする専用バッチは持たない)。
    ここでの現象コードは2コード同時観測(2026-09対応)のパック済み値のこともあるが、
    完全一致による比較(prev_c/cur_c/next_c)はパック値同士でもそのまま正しく機能する
    (パック値が異なれば元の組み合わせも異なる)ため、特別な分解処理は不要。"""
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
            # 2026-10-13: 予測の対象(この地点の霧かどうか)が変わらないブレは積まない(現象なし⇔全体霧・層雲、全体霧の濃さだけの違い等)。
            # 学習のラベルが変わらないため、確認待ちを埋めるだけだった(当時の確認待ち211件のうち約8割)。
            if _is_target_code(name, cur_c) == _is_target_code(name, prev_c):
                continue
            target_dt = recent.iloc[i]["datetime"]
            target_dt_str = (
                target_dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(target_dt, "strftime") else str(target_dt)
            )
            if name in users.get_phenomena_corrections_for_datetime(conn, target_dt_str):
                continue  # 既に訂正済み(人力・過去の承認いずれか)
            now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
            created = users.create_observation_report(
                conn, name, target_dt_str, cur_c, None, now_str, source="auto_detected",
            )
            if created:
                _record_audit(
                    "観測異常を自動検知(承認待ち)",
                    f"{name} {target_dt_str}: 現在{cur_c}、前後の観測は{prev_c} — 管理者の確認待ち",
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
    importance = request.form.get("importance", "medium")
    if importance not in IMPORTANCE_LEVELS:
        importance = "medium"
    reason = request.form.get("reason", "").strip()[:OBSERVATION_REPORT_REASON_MAX_LENGTH]
    if not reason:
        return _finish(
            "station_detail", "なぜこの現象だと思うか、理由を入力してください。", success=False, name=name,
        )
    conn = _users_conn()
    try:
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        report_id = users.create_observation_report(
            conn, name, target_dt_str, current["code"], my_id, now_str,
            source="user", importance=importance, reason=reason,
        )
        created = report_id is not None
        gem_note = ""
        if created:
            submit_gems = GEM_REWARD_ON_SUBMIT[importance]
            if users.grant_gems(conn, my_id, f"observation_report_submit:{report_id}", submit_gems, now_str):
                gem_note = f"(+{submit_gems}ジェム)"
            _record_audit(
                "観測報告を受信",
                f"{name} {target_dt_str}: コード{current['code']} (重要度: {IMPORTANCE_LABELS[importance]})",
            )
    finally:
        conn.close()
    if created:
        message = f"報告を受け付けました{gem_note}。管理者が確認します。"
    else:
        message = "この観測は既に報告済みです。"
    return _finish("station_detail", message, name=name)


@app.route("/stations/<name>/correct", methods=["POST"])
@requires_observer
def station_correct_observation(name):
    """会津観測者/只見観測者(担当地域のみ)・観測者・管理者が、承認待ちキューを介さず
    その場で現在の観測結果を直接訂正する。一般ユーザーの「報告」と異なり即時反映する。"""
    name = _validate_station(name)
    role = session.get("role")
    if not _can_correct_station(role, name):
        abort(403)
    my_id = session.get("user_id")
    observations = _latest_observations()
    if not observations:
        return _finish("station_detail", "現在参照できる観測データがありません。", success=False, name=name)
    current = {s["name"]: s for s in observations["stations"]}.get(name)
    if current is None or current.get("code") is None:
        return _finish("station_detail", "この地点の観測データがありません。", success=False, name=name)
    corrected_code = request.form.get("corrected_code", type=int)
    corrected_code2 = request.form.get("corrected_code2", type=int)
    if corrected_code is None:
        return _finish("station_detail", "訂正後の現象コードを選んでください。", success=False, name=name)
    codes = [corrected_code] if corrected_code2 is None else [corrected_code, corrected_code2]
    try:
        packed_corrected = encode_phenom_codes(codes)
    except PhenomCodeError as e:
        return _finish("station_detail", f"現象コードの組み合わせが不正です: {e}", success=False, name=name)
    original_codes = [current["code"]] if current.get("code2") is None else [current["code"], current["code2"]]
    original_packed = encode_phenom_codes(original_codes)
    target_dt_str = observations["datetime"]
    if hasattr(target_dt_str, "strftime"):
        target_dt_str = target_dt_str.strftime("%Y-%m-%d %H:%M:%S")
    conn = _users_conn()
    try:
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        users.add_phenomena_correction(
            conn, name, target_dt_str, original_packed, packed_corrected, my_id, now_str, source="human",
        )
        gem_note = ""
        if packed_corrected != original_packed:
            if users.grant_gems(
                conn, my_id, f"direct_correction:{name}:{target_dt_str}",
                GEM_REWARD_DIRECT_CORRECTION, now_str,
            ):
                gem_note = f"(+{GEM_REWARD_DIRECT_CORRECTION}ジェム)"
        _record_audit(
            "観測をその場で訂正",
            f"{name} {target_dt_str}: {format_phenom_display(original_packed)} → "
            f"{format_phenom_display(packed_corrected)}",
        )
    finally:
        conn.close()
    return _finish("station_detail", f"観測結果を訂正しました{gem_note}。", name=name)


# 観測報告は管理者専用機能ではなく観測業務の一部(観測者ロールも使う。requires_observer参照)のため、
# 以前の/admin/observation_reports(管理者メニュー配下、左に管理者サイドバーが出る)から
# トップレベルの独立したページへ移した(2026-10-03)。旧URLはブックマーク用にリダイレクトだけ残す。
@app.route("/admin/observation_reports")
def legacy_admin_observation_reports():
    return redirect(url_for("observation_reports"), code=301)


@app.route("/observation_reports")
@requires_observer
def observation_reports():
    role = session.get("role")
    region = _observer_region_for_role(role)
    conn = _users_conn()
    try:
        reports = users.list_pending_observation_reports(conn)
    finally:
        conn.close()
    if region is not None:
        reports = [r for r in reports if STATION_REGION.get(r["station_name"]) == region]

    # 前後1時間の現象コードを引けるよう、現象データを1回だけ読み込んで日時をインデックスに
    # する(報告のたびに全件読み直すと件数が多い時に重くなるため)。読み込みに失敗しても
    # 画像の前後比較自体は(コード無しで)続けられるようにNoneのまま進める。
    phenom_by_dt = None
    if reports:
        try:
            _, phenom_df, _ = fog_data.load_river_fog_data()
            phenom_by_dt = phenom_df.set_index("datetime")
        except Exception as e:
            log.warning("Failed to load phenomena data for observation report review: %s", e)

    def _phenom_code_at(station, dt):
        if phenom_by_dt is None or station not in phenom_by_dt.columns:
            return None
        try:
            code = encode_phenomena_cell(phenom_by_dt.at[dt, station])
        except KeyError:
            return None
        return None if code != code else int(code)  # NaNはcode != codeがTrueになる

    for r in reports:
        image_date = r["target_datetime"][:10]
        image_hhmm = r["target_datetime"][11:13] + r["target_datetime"][14:16]
        image_path = IMAGE_DIR / image_date / image_hhmm / f"{r['station_name']}.jpg"
        r["image_date"] = image_date
        r["image_hhmm"] = image_hhmm
        r["has_image"] = image_path.is_file()
        # 「孤立した1時間だけのブレか」を管理者が前後と見比べて判断できるよう、対象時刻の
        # 前後1時間分の画像と現象コードを、報告の種類(利用者報告/自動検知)を問わず表示する。
        target_dt = datetime.strptime(r["target_datetime"], "%Y-%m-%d %H:%M:%S")
        for offset, key in ((-1, "prev_image"), (1, "next_image")):
            neighbor_dt = target_dt + timedelta(hours=offset)
            n_date = neighbor_dt.strftime("%Y-%m-%d")
            n_hhmm = neighbor_dt.strftime("%H%M")
            n_path = IMAGE_DIR / n_date / n_hhmm / f"{r['station_name']}.jpg"
            code = _phenom_code_at(r["station_name"], neighbor_dt)
            r[key] = {
                "date": n_date, "hhmm": n_hhmm, "has_image": n_path.is_file(),
                "code": code,
                "label": phenom_label(code),
            }
        # 該当時刻自体の判定は前後と同じ形式で並べて見比べやすくする(値自体は報告時点の
        # reported_codeそのまま。訂正前の判断材料なので現時点の現象データを再取得する
        # 意味は無い)。reported_code_primary/secondaryは、2コード同時観測(2026-09対応)を
        # 訂正ピッカーの初期状態(主・副コード)として展開するために分解しておく。
        r["current_label"] = phenom_label(r["reported_code"])
        r["reported_code_primary"], r["reported_code_secondary"] = decode_phenom_value(r["reported_code"])
        differs_from_both = (
            r["prev_image"]["code"] is not None and r["next_image"]["code"] is not None
            and r["prev_image"]["code"] == r["next_image"]["code"]
            and r["prev_image"]["code"] != r["reported_code"]
        )
        r["looks_isolated"] = differs_from_both
    _attach_own_judge(reports)
    # 自前の判定が今のラベルと違う(=ラベルの誤りの可能性が高い)報告を先に。それ以外は元の並びのまま(sortは安定)
    reports.sort(key=lambda r: 0 if (r.get("own_judge") or {}).get("disagrees") else 1)
    conn = _users_conn()
    try:
        stale_auto_count = len(_stale_auto_reports(conn))
    finally:
        conn.close()
    return render_template(
        "observation_reports.html", reports=reports, phenom_labels=PHENOM_LABELS,
        stale_auto_count=stale_auto_count, stale_auto_days=STALE_AUTO_REPORT_DAYS,
        phenom_style=PHENOM_STYLE, importance_labels=IMPORTANCE_LABELS,
        own_judge_disagree_count=sum(1 for r in reports if (r.get("own_judge") or {}).get("disagrees")),
    )


OWN_JUDGE_CONFIDENT = 0.6  # 自前の判定がこの確率以上で今のラベルと違うときだけ「違う」とする(迷っている判定で並びを変えない)


def _attach_own_judge(reports):
    """観測報告の下見(2026-10-14): 各報告の対象時刻・地点の、自前の画像判定(own_judge.py、Geminiとは別の判定)を付ける。
    r["own_judge"] = {"label", "prob", "disagrees"}。disagrees は、自前の判定が今のラベルと別のまとまり
    (現象なし・川霧・全体霧・層雲・雨)を OWN_JUDGE_CONFIDENT 以上の確率で出しているとき。判定が無ければ付けない。"""
    if not reports:
        return
    try:
        import own_judge
        if not own_judge.DB.exists():
            return
        c = own_judge.connect(ro=True)
        try:
            keys = {(r["target_datetime"], r["station_name"]) for r in reports}
            rows = {}
            dts = sorted({k[0] for k in keys})
            for i in range(0, len(dts), 500):
                chunk = dts[i:i + 500]
                for dt, st, pred, probs in c.execute(
                        f"SELECT datetime, station, pred, probs FROM judge WHERE datetime IN ({','.join('?' * len(chunk))})", chunk):
                    if (dt, st) in keys:
                        rows[(dt, st)] = (pred, json.loads(probs))
        finally:
            c.close()
    except Exception as e:  # noqa: BLE001
        log.warning("own_judge lookup for observation reports failed: %s", e)
        return
    for r in reports:
        hit = rows.get((r["target_datetime"], r["station_name"]))
        if not hit:
            continue
        pred, probs = hit
        current = own_judge._group(r["reported_code"])
        r["own_judge"] = {
            "label": own_judge.GROUPS[pred], "prob": probs[pred],
            "disagrees": current is not None and pred != current and probs[pred] >= OWN_JUDGE_CONFIDENT,
        }


@app.route("/observation_reports/<int:report_id>/resolve", methods=["POST"])
@requires_observer
def observation_report_resolve(report_id):
    my_id = session.get("user_id")
    conn = _users_conn()
    try:
        report = users.get_observation_report(conn, report_id)
        if report is None or report["status"] != "pending":
            abort(404)
        _enforce_observer_region(report["station_name"])
        corrected_code = request.form.get("corrected_code", type=int)
        corrected_code2 = request.form.get("corrected_code2", type=int)
        if corrected_code is None:
            return _finish("observation_reports", "訂正後の現象コードを選んでください。", success=False)
        codes = [corrected_code] if corrected_code2 is None else [corrected_code, corrected_code2]
        try:
            packed_corrected = encode_phenom_codes(codes)
        except PhenomCodeError as e:
            return _finish(
                "observation_reports", f"現象コードの組み合わせが不正です: {e}", success=False,
            )
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        # original_codeは報告作成時点のreported_codeではなく、今この瞬間の実際の判定
        # (既に別経路で訂正済みならその訂正後コード、無ければ生のAI判定)を使う。報告から
        # 承認までの間に別経路(その場での直接訂正・再収集等)でコードが変わっていた場合、
        # 古いreported_codeをそのままoriginal_codeに残すとfog_model.pyの学習除外/
        # 再重み付けロジックが事実と食い違うレコードを参照してしまうため(2026-09-13の
        # 監査で指摘)。
        codes_by_station, corrections_by_station = _phenom_codes_for_datetime(report["target_datetime"])
        current_code = corrections_by_station.get(
            report["station_name"], codes_by_station.get(report["station_name"], report["reported_code"]),
        )
        users.add_phenomena_correction(
            conn, report["station_name"], report["target_datetime"],
            current_code, packed_corrected, my_id, now_str, source="human",
        )
        users.resolve_observation_report(
            conn, report_id, "resolved", my_id, now_str, f"{format_phenom_display(packed_corrected)}に訂正",
        )
        reward_note = ""
        if report["source"] == "user" and report["reported_by"] and packed_corrected != report["reported_code"]:
            granted = users.grant_points(
                conn, report["reported_by"], f"observation_report_reward:{report_id}",
                OBSERVATION_REPORT_REWARD_POINTS, now_str,
            )
            if granted:
                reward_note = f"(報告者に{OBSERVATION_REPORT_REWARD_POINTS}pt付与)"
            approve_gems = GEM_REWARD_ON_APPROVE.get(report["importance"], 0)
            if approve_gems > 0:
                gem_granted = users.grant_gems(
                    conn, report["reported_by"], f"observation_report_approve:{report_id}",
                    approve_gems, now_str,
                )
                if gem_granted:
                    reward_note = f"{reward_note}(+{approve_gems}ジェム)"
        elif report["source"] == "auto_detected" and packed_corrected != report["reported_code"]:
            # 自動検知分には報告者がいないため、代わりに実際に確認・訂正した管理者へ
            # ジェムを付与する(報告者選択の重要度が無いため「中」相当の固定額)。
            gem_granted = users.grant_gems(
                conn, my_id, f"observation_report_auto_approve:{report_id}",
                GEM_REWARD_TOTAL["medium"], now_str,
            )
            if gem_granted:
                reward_note = f"(確認した管理者に+{GEM_REWARD_TOTAL['medium']}ジェム)"
        _record_audit(
            "観測報告を確認・訂正",
            f"{report['station_name']} {report['target_datetime']}: "
            f"{format_phenom_display(report['reported_code'])} → "
            f"{format_phenom_display(packed_corrected)} {reward_note}".strip(),
        )
    finally:
        conn.close()
    return _finish("observation_reports", "訂正しました。")


@app.route("/observation_reports/<int:report_id>/dismiss", methods=["POST"])
@requires_observer
def observation_report_dismiss(report_id):
    my_id = session.get("user_id")
    conn = _users_conn()
    try:
        report = users.get_observation_report(conn, report_id)
        if report is None or report["status"] != "pending":
            abort(404)
        _enforce_observer_region(report["station_name"])
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        users.resolve_observation_report(conn, report_id, "dismissed", my_id, now_str, "問題なしと判断")
        _record_audit("観測報告を却下", f"{report['station_name']} {report['target_datetime']}")
    finally:
        conn.close()
    return _finish("observation_reports", "却下しました。")


def _is_target_code(station, packed):
    """現象コード(2コード同時はパック値)が、この地点の予測の対象の霧(fog_features.target_codes)を含むか。"""
    tc = fog_features.target_codes(station)
    return any(c is not None and c in tc for c in decode_phenom_value(packed))


def _auto_report_changes_target(r, phenom_by_dt):
    """自動検知の報告が、予測の対象かどうかの違うブレか(前の時刻と比べる)。調べられなければTrue(残す側)。"""
    try:
        import pandas as pd
        prev = phenom_by_dt.at[pd.Timestamp(r["target_datetime"]) - pd.Timedelta(hours=1), r["station_name"]]
        prev_c = encode_phenomena_cell(prev)
        if prev_c != prev_c:
            return True
        return _is_target_code(r["station_name"], int(prev_c)) != _is_target_code(r["station_name"], int(r["reported_code"]))
    except Exception:
        return True


STALE_AUTO_REPORT_DAYS = 3


def _stale_auto_reports(conn):
    """まとめて閉じられる報告(2026-10-13): 人ではなく自動で検知したもの(前後と1時間だけ違う判定・自前の判定との食い違い)で、
    STALE_AUTO_REPORT_DAYS日より前の時刻のもの(確認待ちの大半がこれで、日がたつと見直す価値が下がるため)。
    会津観測者/只見観測者は担当地域の分だけ。"""
    # 2026-10-13: 3日より前のものに加え、予測の対象(霧かどうか)が変わらないブレ(新しい条件では積まないもの)も含める
    region = _observer_region_for_role(session.get("role"))
    cutoff = (now_jst_naive() - timedelta(days=STALE_AUTO_REPORT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    pending = [r for r in users.list_pending_observation_reports(conn)
               if r["source"] == "auto_detected" and (region is None or STATION_REGION.get(r["station_name"]) == region)]
    phenom_by_dt = None
    try:
        _, phenom_df, _ = fog_data.load_river_fog_data()
        phenom_by_dt = phenom_df.set_index("datetime")
    except Exception as e:
        log.warning("Phenomena data unavailable for stale report check: %s", e)
    out = []
    for r in pending:
        own_judge_report = (r.get("reason") or "").startswith("自前の画像判定")
        if r["target_datetime"] < cutoff or (phenom_by_dt is not None and not own_judge_report
                                              and not _auto_report_changes_target(r, phenom_by_dt)):
            out.append(r)
    return out


@app.route("/observation_reports/bulk_dismiss", methods=["POST"])
@requires_observer
def observation_reports_bulk_dismiss():
    my_id = session.get("user_id")
    now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    conn = _users_conn()
    try:
        stale = _stale_auto_reports(conn)
        for r in stale:
            users.resolve_observation_report(conn, r["id"], "dismissed", my_id, now_str,
                                             f"見直す必要の低い自動検知({STALE_AUTO_REPORT_DAYS}日より前、または霧かどうかの変わらないブレ)をまとめて閉じた")
    finally:
        conn.close()
    _record_audit("観測報告をまとめて閉じた", f"見直す必要の低い自動検知: {len(stale)}件")
    return _finish("observation_reports", f"{len(stale)}件の自動検知を閉じました。")


# 地点ページの背景演出(_weather_scene.html / web_static/weather_scene.css)。Appleの「天気」アプリの
# ように、その地点の最新の観測(現象コード)と時間帯に応じて、背景に空の色と流れる霧・雲・雨を描く。
WX_PHASE_LABELS = {"night": "夜", "dawn": "朝", "day": "日中", "dusk": "夕方"}


def _weather_scene(name, code):
    """背景演出の種類を決める。戻り値: {"phase", "kind", "level", "caption"}。
    phase: 地点の日の出・日の入り(fog_features.sun_times_hours)から現在の時間帯を決める。
    kind/level: 現象コードから(川霧1〜3→riverfog、全体霧4〜6→fog、層雲7〜8→cloud、霧雨・雨→rain)。"""
    now = now_jst_naive()
    hour = now.hour + now.minute / 60.0
    info = CAMERA_INFO.get(name, {})
    lat, lon = info.get("lat"), info.get("lon")
    sunrise, sunset = (5.5, 17.5)
    if lat is not None and lon is not None:
        try:
            sunrise, sunset = fog_features.sun_times_hours(now, lat, lon)
        except Exception:
            pass
    if hour < sunrise - 0.6 or hour > sunset + 0.6:
        phase = "night"
    elif hour < sunrise + 1.0:
        phase = "dawn"
    elif hour > sunset - 1.0:
        phase = "dusk"
    else:
        phase = "day"
    try:
        code = int(code) if code is not None and code == code else None
    except (TypeError, ValueError):
        code = None
    if code in (1, 2, 3):
        kind, level = "riverfog", code
    elif code in (4, 5, 6):
        kind, level = "fog", code - 3
    elif code in (7, 8):
        kind, level = "cloud", code - 6
    elif code in (9, 10):
        kind, level = "rain", code - 8
    else:
        kind, level = "clear", 1
    if code is None:
        label = "観測なし"
    elif code == 0:
        label = "現象なし"
    else:
        label = PHENOM_LABELS.get(code, "")
    return {"phase": phase, "kind": kind, "level": level,
            "caption": f"最新の観測「{label}」・{WX_PHASE_LABELS[phase]}"}


_overview_scene_cache = {"data": None, "at": 0.0}
OVERVIEW_SCENE_CACHE_SECONDS = 120


def _observed_in_daylight(dt):
    info = CAMERA_INFO.get(STATION_NAMES[0], {}) if STATION_NAMES else {}
    if info.get("lat") is None or info.get("lon") is None:
        return True
    try:
        return bool(fog_features_is_daylight(dt, info["lat"], info["lon"]))
    except Exception:
        return True


def _overview_weather_scene():
    """ダッシュボード・公開ページのトップの背景演出。全地点の最新の観測から代表の状況を決める
    (どこかで川霧→川霧(最も濃いもの)、なければ全体霧、雨・霧雨が2割以上の地点→雨、それ以外は晴れ)。
    戻り値: {"wx": _weather_sceneの結果, "n_riverfog", "n_fog", "n_total", "observed_at"}"""
    now = time.monotonic()
    cached = _overview_scene_cache["data"]
    if cached is not None and now - _overview_scene_cache["at"] < OVERVIEW_SCENE_CACHE_SECONDS:
        return cached
    try:
        obs = _latest_observations()
    except Exception as e:
        log.warning("Overview scene: failed to load observations: %s", e)
        obs = None
    codes = []
    if obs:
        for s in obs["stations"]:
            c = s.get("code")
            if c is not None and c == c:
                codes.append(int(c))
    river = [c for c in codes if c in (1, 2, 3)]
    fog = [c for c in codes if c in (4, 5, 6)]
    rain = [c for c in codes if c in (9, 10)]
    if river:
        code = max(river)
    elif fog:
        code = max(fog)
    elif codes and len(rain) >= 0.2 * len(codes):
        code = max(rain)
    else:
        code = 0 if codes else None
    data = {
        "wx": _weather_scene(STATION_NAMES[0] if STATION_NAMES else None, code),
        "n_riverfog": len(river), "n_fog": len(fog), "n_total": len(codes),
        "observed_at": obs["datetime"].strftime("%m/%d %H:%M") if obs else None,
        # 日の出前・日の入り後の画像は暗く、判定は参考程度(学習・評価でも対象外にしている)。
        "observed_in_dark": bool(obs) and not _observed_in_daylight(obs["datetime"]),
    }
    _overview_scene_cache["data"] = data
    _overview_scene_cache["at"] = now
    return data


@app.context_processor
def _inject_overview_weather_scene():
    return {"overview_weather_scene": _overview_weather_scene}


@app.context_processor
def _inject_weather_scene():
    return {"weather_scene": _weather_scene}


def _station_hourly(name):
    """地点ページの「時間ごとの出やすさ」の帯(天気アプリ風)。今日の残りと明日の日中の各時刻。
    戻り値: [{"day": "今日"/"明日" (日の変わり目のみ), "label": "今"/"6時", "level", "short", "now"}, ...]"""
    data = _public_forecast_safe() or {}
    station = next((s for s in data.get("stations", []) if s["name"] == name), None)
    if not station:
        return []
    now = now_jst_naive()
    out = []
    for di, day in enumerate(data.get("days", [])[:2]):
        hours = station["days"][di]["hours"]
        first = True
        for h in sorted(hours):
            if di == 0 and h < now.hour:
                continue
            lv = hours[h]
            is_now = di == 0 and h == now.hour
            out.append({"day": day["label"] if first else None, "label": "今" if is_now else f"{h}時",
                        "level": lv, "short": PUBLIC_LEVELS[lv]["short"], "now": is_now})
            first = False
    return out


def _station_days(name):
    """地点ページの「この先3日の見込み」のカード(公開ページの日付カードと同じ3段階)。
    戻り値: [{"label": "今日", "date": "10/8(木)", "level": 0〜3, "short": "高"等, "span": "6時〜7時"}, ...]
    (level 0は、その日の日中の予測が無い)。"""
    data = _public_forecast_safe() or {}
    station = next((s for s in data.get("stations", []) if s["name"] == name), None)
    if not station:
        return []
    out = []
    for day, d in zip(data.get("days", [])[:3], station["days"]):
        hours = d.get("hours") or {}
        top = int(d.get("max") or (max(hours.values()) if hours else 0) or 0)
        span = ""
        if top >= 2:
            hs = sorted(h for h, lv in hours.items() if lv == top)
            span = f"{hs[0]}時" + (f"〜{hs[-1]}時" if len(hs) > 1 else "")
        out.append({"label": day["label"], "date": day.get("date_label", ""), "level": top,
                    "short": PUBLIC_LEVELS[top]["short"] if top in PUBLIC_LEVELS else "-", "span": span})
    return out


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
    order = _station_flow_order()
    i = order.index(name) if name in order else -1
    nav_prev = order[i - 1] if i > 0 else None
    nav_next = order[i + 1] if 0 <= i < len(order) - 1 else None

    return render_template(
        "station_detail.html",
        name=name,
        nav_prev=nav_prev,
        nav_next=nav_next,
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
        can_correct=_can_correct_station(session.get("role"), name),
        phenom_labels=PHENOM_LABELS,
        phenom_style=PHENOM_STYLE,
        hourly=_station_hourly(name),
        station_days=_station_days(name),
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


@app.route("/graphs/weather-fog.json")
def weather_fog_graph_png():
    station = _validate_station(request.args.get("station", STATION_NAMES[0]))

    def render():
        main_df = phenom_df = None
        if WEATHER_DB_PATH.exists():
            try:
                main_df, phenom_df, _ = fog_data.load_river_fog_data()
            except Exception as e:
                log.warning("Failed to load data for weather-fog graph: %s", e)
        return _render_weather_fog_boxplot_plotly(main_df, phenom_df, station)

    data = _cached_chart_json(f"weather_fog_graph:{station}", [WEATHER_DB_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/river-fog-histogram")
def river_fog_histogram_page():
    """全地点合算・川霧発生時のみに絞った気象条件ヒストグラムの専用ページ。
    「気象と川霧」ページ(/graphs配下)とはナビの見た目上のアクティブ判定が
    衝突しないよう、あえて/graphs以下ではないパスにしている。
    ?compare=1 で、気象要素ごとの図を「川霧あり・なし」の比較にする(2026-10-14)。"""
    conn = _users_conn()
    try:
        correction_counts = users.phenomena_correction_counts_by_station(conn)
    finally:
        conn.close()
    compare = request.args.get("compare") == "1"
    hist_items = [{"index": i, "label": label, "file": re.sub(r"[(（].*", "", label)}
                  for i, (_, label, _) in enumerate(_RIVER_FOG_HISTOGRAM_FEATURES)]
    return render_template("river_fog_histogram.html", correction_counts=correction_counts, hist_items=hist_items,
                           compare=compare)


@app.route("/graphs/weather-fog-histogram.json")
def weather_fog_histogram_png():
    """全地点合算・川霧発生時のみに絞った気象条件ヒストグラム(地点選択は無く常に全地点)。
    ?item=<番号> で1項目だけの図(個別に保存する用)、&compare=1 で「川霧なし」と並べた図(2026-10-14)。"""
    item = request.args.get("item", type=int)
    if item is not None and not 0 <= item < len(_RIVER_FOG_HISTOGRAM_FEATURES):
        abort(404)
    compare = item is not None and request.args.get("compare") == "1"

    def render():
        fog_values, nofog_values = _river_fog_histogram_values()
        return _render_river_fog_histogram_plotly(fog_values, nofog_values, item=item, compare=compare)

    key = "weather_fog_histogram" if item is None else f"weather_fog_histogram_{item}{'_cmp' if compare else ''}"
    data = _cached_chart_json(key, [WEATHER_DB_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/graphs/weather-fog-histogram.csv")
def weather_fog_histogram_csv():
    """ヒストグラムの数値をCSVで(区分ごとの件数・割合、川霧あり・なし)。?item=<番号> でその項目だけ、無ければ全項目(2026-10-14)。"""
    item = request.args.get("item", type=int)
    if item is not None and not 0 <= item < len(_RIVER_FOG_HISTOGRAM_FEATURES):
        abort(404)
    items = [item] if item is not None else list(range(len(_RIVER_FOG_HISTOGRAM_FEATURES)))
    fog_values, nofog_values = _river_fog_histogram_values()
    name = ("川霧ヒストグラム_" + re.sub(r"[(（].*", "", _RIVER_FOG_HISTOGRAM_FEATURES[item][1])) if item is not None else "川霧ヒストグラム_全項目"
    resp = Response(_river_fog_histogram_csv(fog_values, nofog_values, items), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name + '.csv')}"
    return resp


# 論文・発表用の図をまとめてZIPで(2026-10-14)。(ファイル名, エンドポイント, 引数)。画面と同じグラフを白い背景・大きめの画像にする。
_PAPER_FIGURES = [
    ("01_特徴量の重要度_SHAP.png", "accuracy_feature_importance_chart", {}),
    ("02_特徴量の重要度_グループ別.png", "accuracy_feature_group_importance_chart", {}),
    ("03_モデルと気候値の比較.png", "accuracy_model_compare_chart", {}),
    ("04_確率の信頼性.png", "accuracy_reliability_chart", {}),
    ("05_しきい値ごとの成績.png", "accuracy_threshold_chart", {}),
    ("06_混同行列_評価期間.png", "accuracy_training_confusion_matrix", {}),
    ("07_学習時の指標.png", "accuracy_training_metrics_chart", {}),
    ("08_的中率の推移.png", "accuracy_chart", {}),
    ("09_予報の先までの日数ごとの成績.png", "accuracy_lead_time_chart", {}),
    ("10_地域別の成績.png", "accuracy_region_chart", {}),
    ("11_混同行列_運用.png", "accuracy_confusion_matrix", {}),
    ("12_気象条件ヒストグラム_全項目.png", "weather_fog_histogram_png", {}),
] + [
    (f"{13 + i:02d}_川霧あり・なしの比較_{re.sub(r'[(（].*', '', label)}.png", "weather_fog_histogram_png", {"item": i, "compare": 1})
    for i, (_, label, _) in enumerate(_RIVER_FOG_HISTOGRAM_FEATURES)
]


@app.route("/analysis/paper-figures.zip")
def paper_figures_zip():
    """_PAPER_FIGURES をそれぞれのグラフのJSONから画像(PNG、白い背景・幅1400px・2倍)にしてZIPで返す。
    作れなかった図は飛ばして、ZIPの中の「作れなかった図.txt」に書く。ヒストグラムのCSVも入れる。"""
    buf = io.BytesIO()
    failed = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, endpoint, kwargs in _PAPER_FIGURES:
            try:
                with app.test_request_context(url_for(endpoint, **kwargs)):
                    resp = app.make_response(app.view_functions[endpoint]())
                    fig = json.loads(resp.get_data(as_text=True))
                png = mpl_render.render_cached(fig, theme="light", width=1400, scale=2, notitle=False)
                zf.writestr(fname, png)
            except Exception as e:  # noqa: BLE001
                log.warning("paper figure %s failed: %s", fname, e)
                failed.append(f"{fname}: {type(e).__name__}")
        fog_values, nofog_values = _river_fog_histogram_values()
        zf.writestr("気象条件ヒストグラム_全項目.csv",
                    _river_fog_histogram_csv(fog_values, nofog_values, range(len(_RIVER_FOG_HISTOGRAM_FEATURES))).encode("utf-8"))
        if failed:
            zf.writestr("作れなかった図.txt", "\n".join(failed) + "\n")
    name = f"川霧_論文用の図_{datetime.now(JST).strftime('%Y%m%d')}.zip"
    resp = Response(buf.getvalue(), mimetype="application/zip")
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    return resp

@app.route("/stations/<name>/comments", methods=["POST"])
def station_comment_add(name):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    name = _validate_station(name)
    body = request.form.get("body", "").strip()[:STATION_COMMENT_MAX_LENGTH]
    if not body:
        return _finish("station_detail", "コメントを入力してください。", success=False, name=name)
    created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    conn = _users_conn()
    try:
        comment_id = users.add_station_comment(conn, name, my_id, body, created_at)
        users.grant_points(
            conn, my_id, f"comment:{comment_id}", COMMENT_POST_POINTS, created_at,
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


@app.route("/stations/<name>/chart.json")
def station_chart(name):
    name = _validate_station(name)

    def render():
        trend = _accuracy_trend_safe(station=name)
        dates = [r["date"] for r in trend if r["n"] > 0]
        rates = [100.0 * r["correct"] / r["n"] for r in trend if r["n"] > 0]
        return _render_line_chart_plotly(dates, rates, f"{name} の的中率推移")

    data = _cached_chart_json(f"station_chart:{name}", [DB_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/stations/<name>/model_metrics_chart.json")
def station_model_metrics_chart(name):
    """地点別のモデル学習時精度推移(item 7)。全地点平均のみだった従来の
    admin_models_chartと違い、この地点固有の学習履歴だけに絞る。"""
    name = _validate_station(name)

    def render():
        trend = _load_metrics_history_trend(station=name)
        return _render_metrics_trend_plotly(trend, title=f"{name} の学習時モデル精度推移")

    data = _cached_chart_json(f"station_model_metrics_chart:{name}",
                               [fog_model.METRICS_HISTORY_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/stations/<name>/threshold_chart.json")
def station_threshold_chart(name):
    """地点別のしきい値ごとのprecision/recall/F1(item 8)。実際の本番予測ログ
    (predicted_fog_prob)から算出するので、学習時の検証データに依存しない。"""
    name = _validate_station(name)

    def render():
        sweep = _threshold_sweep_stats(_fog_prob_buckets_safe(station=name))
        state = fog_model.load_model_state()
        current = (state.get(name) or {}).get("fog_threshold")
        return _render_threshold_sweep_plotly(
            sweep, current_threshold=current,
            title=f"{name} のしきい値ごとのprecision/recall/F1",
        )

    data = _cached_chart_json(f"station_threshold_chart:{name}", [DB_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/stations/<name>/tree_diagram.json")
def station_tree_diagram(name):
    """採用中のモデル(全地点共通の二値LightGBM)が実際にどれくらいの深さの木を
    使っているかを示す図。木の深さは分岐条件を重ねるほど
    複雑な判定ができる一方、過学習のリスクとの兼ね合いでもあるため、モデルの複雑さを
    直感的に把握できるよう追加した(2026-09-13、利用者からの要望)。"""
    name = _validate_station(name)

    def render():
        state = fog_model.load_model_state()
        info = state.get(name)
        extracted = {}
        if info is not None and info.get("pipe") is not None:
            feature_names = list(info.get("features", []))
            try:
                extracted = fog_model.extract_representative_tree(info["pipe"], feature_names)
            except Exception as e:
                log.warning("Failed to extract tree diagram for %s: %s", name, e)
        return _render_tree_depth_diagram_plotly(extracted, name)

    data = _cached_chart_json(
        f"station_tree_diagram:{name}", [fog_model.MODEL_STATE_PATH], render,
    )
    return Response(data, mimetype="application/json")


@app.route("/accuracy")
def accuracy_page():
    summary = _accuracy_summary_safe()
    by_weather = _accuracy_by_weather_safe()
    if summary:
        # _accuracy_summary_safeはサーバー全体で共有するキャッシュを返すため、
        # そのdictを直接書き換えず、川霧クラス指標を足した新しい行リストを別途作る。
        station_metrics = _accuracy_station_metrics_safe()
        by_station = [
            {**row, **station_metrics.get(row["station"], {})}
            for row in summary["by_station"]
        ]
    else:
        by_station = []
    region_summary = _accuracy_region_summary_safe()
    overall_fog_stats = _fog_confusion_stats(_fog_pair_counts_safe())

    # 以前は管理者専用の「モデル管理」ページにしかなかった学習時(検証データ)の指標。
    # 2026-09-12、正答率・F1のような分析情報は管理者専用でなくてよいとの判断で
    # 分析ページに統合した(admin_models_*のチャート・地点別モデル状況の表を参照)。
    model_state = fog_model.load_model_state()
    model_rows = []
    for col in sorted(model_state.keys()):
        info = model_state[col]
        model_rows.append({
            "station": col,
            "n": info.get("n"),
            "station_fog_rate": info.get("station_fog_rate"),
            "n_test": info.get("n_test"),
            "n_test_fog": info.get("n_test_fog"),
            "accuracy": info.get("accuracy"),
            "fog_f1": info.get("fog_f1"),
            "recall": info.get("recall"),
            "precision": info.get("precision"),
        })
    any_info = next(iter(model_state.values()), {}) if model_state else {}

    return render_template(
        "accuracy.html", summary=summary, by_weather=by_weather, by_station=by_station,
        region_summary=region_summary, overall_fog_stats=overall_fog_stats,
        model_rows=model_rows, global_metrics=fog_model.shared_global_metrics(model_state),
        model_type=any_info.get("model_type"), model_trained_at=any_info.get("trained_at"),
        fog_threshold=any_info.get("fog_threshold"),
        recall_views=_recall_view_stats(any_info.get("fog_threshold")),
    )


def _recall_view_stats(threshold):
    """見逃しを減らす見方(2026-10-14、本番の予報は変えない「追加」の表示): 本番の予測(答え合わせ済み)を、
    (1) 高だけを霧ありとみる(いまのしきい値)、(2) 中以上を霧ありとみる(しきい値×PUBLIC_MID_RATIO)、
    (3) 見逃しを空振りより重く見るF2が最大になる確率(この期間で最も良かった値なので参考)、の3つで採点する。
    戻り値: [{"key", "label", "threshold", "precision", "recall", "f1", "hit", "false_alarm", "miss"}, ...] / データが無ければ[]。"""
    if threshold is None:
        return []
    buckets = _fog_prob_buckets_safe()
    if not buckets:
        return []
    fog = [(b["prob"], b["n"], primary_phenom_code(b["actual_code"]) in SUMMARY_FOG_CODES) for b in buckets]
    total_fog = sum(n for _, n, f in fog if f)
    if not total_fog:
        return []

    def score(t):
        hit = sum(n for p, n, f in fog if f and p >= t - 1e-9)
        fa = sum(n for p, n, f in fog if not f and p >= t - 1e-9)
        prec = hit / (hit + fa) if hit + fa else 0.0
        rec = hit / total_fog
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        f2 = 5 * prec * rec / (4 * prec + rec) if prec + rec else 0.0
        return {"threshold": round(t, 3), "precision": prec, "recall": rec, "f1": f1, "f2": f2,
                "hit": hit, "false_alarm": fa, "miss": total_fog - hit}

    best_f2 = max((score(float(t)) for t in fog_model.FOG_THRESHOLD_GRID), key=lambda s: (s["f2"], s["threshold"]))
    return [
        {"key": "high", "label": "「高」だけを霧ありとみる(いまの予報)", **score(float(threshold))},
        {"key": "mid", "label": "「中」以上を霧ありとみる(見逃しを減らす)", **score(float(threshold) * PUBLIC_MID_RATIO)},
        {"key": "f2", "label": "見逃し重視(F2)で最も良かった確率(参考)", **best_f2},
    ]


@app.route("/accuracy/chart.json")
def accuracy_chart():
    def render():
        trend = _accuracy_trend_safe()
        dates = [r["date"] for r in trend if r["n"] > 0]
        rates = [100.0 * r["correct"] / r["n"] for r in trend if r["n"] > 0]
        return _render_line_chart_plotly(dates, rates, "全地点の的中率推移")

    data = _cached_chart_json("accuracy_chart", [DB_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/accuracy/lead_time_chart.json")
def accuracy_lead_time_chart():
    def render():
        summary = _accuracy_summary_safe()
        by_lead_day = summary["by_lead_day"] if summary else []
        return _render_lead_time_chart_plotly(by_lead_day)

    data = _cached_chart_json("accuracy_lead_time_chart", [DB_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/accuracy/region_chart.json")
def accuracy_region_chart():
    def render():
        return _render_region_compare_chart_plotly(_accuracy_region_summary_safe())

    data = _cached_chart_json("accuracy_region_chart", [DB_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/accuracy/confusion_matrix.json")
def accuracy_confusion_matrix():
    def render():
        labels, matrix = _confusion_matrix_from_pairs(_fog_pair_counts_safe())
        return _render_confusion_matrix_plotly(labels, matrix, title="混同行列(全地点合算・本番予測)")

    data = _cached_chart_json("accuracy_confusion_matrix", [DB_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/accuracy/threshold_chart.json")
def accuracy_threshold_chart():
    def render():
        sweep = _threshold_sweep_stats(_fog_prob_buckets_safe())
        return _render_threshold_sweep_plotly(sweep, title="しきい値ごとのprecision/recall/F1(全地点合算)")

    data = _cached_chart_json("accuracy_threshold_chart", [DB_PATH], render)
    return Response(data, mimetype="application/json")


def _render_reliability_plotly(bins):
    """「確率の正直さ」(2026-10-12): 横に予測の確率、縦に実際に霧だった割合。点が斜めの線に近いほど、
    「30%」と言ったときに本当に3割ほど出ている(確率をそのまま信じてよい)。点の大きさは件数。"""
    data, annotations = [], []
    if bins:
        top = max(0.3, max(max(b["mean_prob"], b["observed"]) for b in bins) * 1.15)
        top = min(1.0, top)
        data.append(go.Scatter(x=[0, top], y=[0, top], mode="lines", name="ぴったり(予測の確率=実際の割合)",
                               line={"color": "#8a8f98", "width": 1.2, "dash": "dash"}, hoverinfo="skip"))
        sizes = [8 + 22 * (math.log10(b["n"]) / math.log10(max(x["n"] for x in bins) or 10) if b["n"] > 1 else 0) for b in bins]
        data.append(go.Scatter(
            x=[b["mean_prob"] for b in bins], y=[b["observed"] for b in bins], mode="lines+markers", name="本番の予測(前日夜)",
            line={"color": "#2a78d6", "width": 1.6}, marker={"size": sizes, "color": "#2a78d6", "line": {"color": "#fff", "width": 1}},
            text=[f"予測{b['lo'] * 100:.0f}〜{b['hi'] * 100:.0f}%: {b['n']:,}件、平均{b['mean_prob'] * 100:.1f}% → 実際{b['observed'] * 100:.1f}%"
                  for b in bins],
            hovertemplate="%{text}<extra></extra>",
        ))
        xr, yr = [0, top], [0, top]
    else:
        xr, yr = [0, 1], [0, 1]
        annotations.append({"text": "データがありません", "xref": "paper", "yref": "paper", "x": 0.5, "y": 0.5, "showarrow": False})
    layout = {
        "xaxis": {"title": {"text": "予測の確率"}, "range": xr, "tickformat": ".0%"},
        "yaxis": {"title": {"text": "実際に霧だった割合"}, "range": yr, "tickformat": ".0%", "scaleanchor": "x", "scaleratio": 1},
        "margin": {"t": 24, "r": 20, "b": 50, "l": 60},
        "legend": {"x": 0, "y": 1, "xanchor": "left", "yanchor": "top", "font": {"size": 11}},
        "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)", "annotations": annotations,
    }
    return _chart_json(go.Figure(data=data, layout=layout))


def _reliability_bins_safe():
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            return prediction_log.reliability_bins(conn)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to compute reliability bins: %s", e)
        return []


@app.route("/accuracy/reliability_chart.json")
def accuracy_reliability_chart():
    data = _cached_chart_json("accuracy_reliability_chart", [DB_PATH], lambda: _render_reliability_plotly(_reliability_bins_safe()))
    return Response(data, mimetype="application/json")

# 以下5つは元々/admin/models配下(管理者専用)にあった学習時(検証データ)のチャート。
# 本番予測ログ(prediction_log)ベースの上記チャートと違い、fog_model.load_model_state()
# が持つ最新の学習結果(検証データでの評価)が元になっている。


@app.route("/accuracy/training_metrics_chart.json")
def accuracy_training_metrics_chart():
    def render():
        return _render_metrics_trend_plotly(_load_metrics_history_trend())

    data = _cached_chart_json("accuracy_training_metrics_chart",
                               [fog_model.METRICS_HISTORY_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/accuracy/model_compare_chart.json")
def accuracy_model_compare_chart():
    def render():
        state = fog_model.load_model_state()
        return _render_model_compare_plotly(state)

    data = _cached_chart_json("accuracy_model_compare_chart", [fog_model.MODEL_STATE_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/accuracy/training_confusion_matrix.json")
def accuracy_training_confusion_matrix():
    def render():
        state = fog_model.load_model_state()
        labels, matrix = fog_model.compute_confusion_matrix(state)
        return _render_confusion_matrix_plotly(labels, matrix, title="混同行列(全地点合算・学習時検証データ)")

    data = _cached_chart_json("accuracy_training_confusion_matrix", [fog_model.MODEL_STATE_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/accuracy/feature_importance_chart.json")
def accuracy_feature_importance_chart():
    def render():
        state = fog_model.load_model_state()
        importance = fog_model.compute_feature_importance(state)
        return _render_feature_importance_plotly(importance, xlabel=_importance_xlabel(state))

    data = _cached_chart_json("accuracy_feature_importance_chart", [fog_model.MODEL_STATE_PATH], render)
    return Response(data, mimetype="application/json")


def _importance_xlabel(state):
    if fog_model.feature_importance_method(state) == fog_model.IMPORTANCE_METHOD_SHAP:
        return "予測への寄与の割合(SHAP・評価期間の予報入力)"
    return "重要度(gainの割合・学習データ)"


@app.route("/accuracy/feature_group_importance_chart.json")
def accuracy_feature_group_importance_chart():
    def render():
        state = fog_model.load_model_state()
        groups = fog_model.compute_feature_group_importance(state)
        return _render_feature_importance_plotly(groups, title="まとまりごとの重要度", xlabel=_importance_xlabel(state))

    data = _cached_chart_json("accuracy_feature_group_importance_chart", [fog_model.MODEL_STATE_PATH], render)
    return Response(data, mimetype="application/json")


@app.route("/changelog")
def changelog_page():
    return render_template("changelog.html", entries=_changelog_entries())


# ============================================================================
# 認証・会員ページ(ログイン・サインアップ・メンバー一覧・プロフィール)
# ============================================================================


# ---------------------------------------------------------------------------
# 一般向けの公開ページ(/forecast、ログイン不要)。研究外部の人(企業・地域の方など)に
# 「今日・明日どこで川霧が出やすいか」を見せるための入口で、未ログインで"/"に来た人は
# ここに案内する。ログインした人だけがその先の研究用サイト(ダッシュボード以降)に入れる。
# 載せるのは地点ごとの「出やすさ」の段階と最新のAI判定の結果だけで、カメラ画像・確率の
# 細かい値・利用者の情報などは出さない(画像は提供元の権利を確認できていないため)。
# 気象業務法の予報業務許可との関係があるため、「予報」ではなく研究用の試験情報として扱い、
# 検索エンジンにも載せない(noindex)。
# ---------------------------------------------------------------------------
PUBLIC_DAYS_AHEAD = 3              # 今日・明日・明後日
PUBLIC_MID_RATIO = 0.5             # しきい値のこの割合以上なら「やや出やすい」
PUBLIC_STALE_HOURS = 36            # 最新の予測がこれより古ければ「更新が止まっている」と表示
PUBLIC_CACHE_SECONDS = 300
PUBLIC_LEVELS = {
    3: {"label": "発生可能性 高", "short": "高"},
    2: {"label": "発生可能性 中", "short": "中"},
    1: {"label": "発生可能性 低", "short": "低"},
}
_public_forecast_cache = {"data": None, "at": 0.0}
_public_forecast_cache_lock = threading.Lock()


def _public_level(prob, threshold):
    if prob is None or prob != prob or threshold is None:
        return 0
    if prob >= threshold:
        return 3
    if prob >= threshold * PUBLIC_MID_RATIO:
        return 2
    return 1


PUBLIC_NOW_FALLBACK_HOURS = 3      # 「現況」で最新時刻が欠けた地点を、この時間以内の観測で補う


def _public_now_by_station(observations):
    """公開ページの「現況」用に、地点ごとの最新の観測を返す。最新時刻だけ画像を取得できなかった地点
    (カメラ配信元への一時的な通信障害で、その時刻の数十地点が空欄になることがある)は、直近
    PUBLIC_NOW_FALLBACK_HOURS時間以内のその地点の最新の観測で補い、その時刻(at、時)を付ける。"""
    if not observations:
        return {}
    out, missing = {}, []
    for s in observations["stations"]:
        if s.get("code") is None:
            missing.append(s["name"])
        else:
            out[s["name"]] = dict(s, at=None)
    if not missing:
        return out
    try:
        _main_df, phenom_df, _names = fog_data.load_river_fog_data()
    except Exception as e:
        log.warning("Public page: failed to load observations for fallback: %s", e)
        return out
    last = observations["datetime"]
    recent = phenom_df[(phenom_df["datetime"] < last)
                       & (phenom_df["datetime"] >= last - timedelta(hours=PUBLIC_NOW_FALLBACK_HOURS))]
    recent = recent.sort_values("datetime", ascending=False)
    for name in missing:
        if name not in recent.columns:
            continue
        for dt, value in zip(recent["datetime"], recent[name]):
            raw = encode_phenomena_cell(value)
            primary, secondary = decode_phenom_value(raw)
            if primary is None:
                continue
            label, _bg, _fg = _phenom_label_color(raw)
            out[name] = {"name": name, "label": label, "code": primary, "code2": secondary, "at": int(dt.hour)}
            break
    return out


def _build_public_forecast():
    """公開ページ用のデータ(地点ごと・日ごと・時刻ごとの出やすさの段階)を組み立てる。"""
    state = fog_model.load_model_state() or {}
    today = now_jst_naive().date()
    days = [today + timedelta(days=i) for i in range(PUBLIC_DAYS_AHEAD)]
    start = days[0].strftime("%Y-%m-%d 00:00:00")
    end = days[-1].strftime("%Y-%m-%d 23:59:59")

    rows, run_at, verification = [], None, []
    if DB_PATH.exists():
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            # 再予測(過去分のやり直し)を除いた、本番の最新の予測だけを使う。
            run_at = conn.execute(
                "SELECT MAX(run_at) FROM predictions WHERE model_type LIKE ? AND model_type NOT LIKE '%[再予測]'",
                (prediction_log.CURRENT_MODEL_LIKE,),
            ).fetchone()[0]
            if run_at:
                rows = conn.execute(
                    """SELECT station, target_datetime, predicted_fog_prob FROM predictions
                       WHERE run_at = ? AND target_datetime BETWEEN ? AND ? AND COALESCE(daylight, 1) = 1""",
                    (run_at, start, end),
                ).fetchall()
            try:
                verification = _build_public_verification(conn, today)
            except Exception as e:
                log.warning("Public page: failed to build verification: %s", e)
        finally:
            conn.close()

    by_station = {}
    for station, target_dt, prob in rows:
        info = state.get(station) or {}
        level = _public_level(prob, info.get("fog_threshold"))
        if level:
            by_station.setdefault(station, {})[target_dt[:13]] = level

    # 本日の残りの時間は、毎時の観測を入れて予測し直したナウキャスト(nowcast.py)があればそちらを使う
    # (直前の観測で数時間先の当たり方が上がることを確かめてある。nowcast.pyのdocstring参照)。
    nowcast_info = None
    try:
        import nowcast
        nc = nowcast.load_latest(today)
    except Exception as e:
        log.warning("Public page: failed to load nowcast: %s", e)
        nc = None
    if nc and nc.get("threshold"):
        prefix, n_hours = today.strftime("%Y-%m-%d"), 0
        for station, hours in (nc.get("stations") or {}).items():
            for hour, prob in hours.items():
                level = _public_level(prob, nc["threshold"])
                if level:
                    by_station.setdefault(station, {})[f"{prefix} {int(hour):02d}"] = level
                    n_hours += 1
        nowcast_info = {"issued_at": nc["issued_at"][11:16], "n": n_hours}

    observations = None
    try:
        observations = _latest_observations()
    except Exception as e:
        log.warning("Public page: failed to load observations: %s", e)
    obs_by_name = _public_now_by_station(observations)

    stations = []
    for name in STATION_NAMES:
        info = CAMERA_INFO.get(name, {})
        levels = by_station.get(name, {})
        day_levels = []
        for d in days:
            prefix = d.strftime("%Y-%m-%d")
            hours = {int(k[11:13]): v for k, v in levels.items() if k.startswith(prefix)}
            day_levels.append({"max": max(hours.values()) if hours else 0, "hours": hours})
        obs = obs_by_name.get(name)
        stations.append({
            "name": name,
            "region": STATION_REGION.get(name) or "その他",
            "lat": info.get("lat"),
            "lon": info.get("lon"),
            "days": day_levels,
            "now": {"label": obs["label"], "fog": obs.get("code") in fog_features.target_codes(name), "code": obs.get("code"),
                    "at": obs.get("at")} if obs else None,
        })

    stale = True
    if run_at:
        try:
            stale = now_jst_naive() - datetime.strptime(run_at[:19], "%Y-%m-%d %H:%M:%S") > timedelta(hours=PUBLIC_STALE_HOURS)
        except ValueError:
            pass
    any_info = next(iter(state.values()), {}) if state else {}
    metrics = (any_info.get("global_metrics") or {})
    model_m, clim_m = metrics.get("model") or {}, metrics.get("climatology") or {}
    weekdays = "月火水木金土日"
    return {
        "days": [{"date": d.strftime("%Y-%m-%d"), "label": ["今日", "明日", "明後日"][i] if i < 3 else d.strftime("%m/%d"),
                  "date_label": f"{d.month}/{d.day}({weekdays[d.weekday()]})"} for i, d in enumerate(days)],
        "stations": stations,
        "run_at": run_at,
        "verification": verification,
        "nowcast": nowcast_info,
        "stale": stale,
        "observed_at": observations["datetime"].strftime("%Y/%m/%d %H:%M") if observations else None,
        "metrics": {
            "precision": model_m.get("precision"), "recall": model_m.get("recall"),
            "f1": model_m.get("f1"), "f1_climatology": clim_m.get("f1"),
            "test_start": (metrics.get("test_start") or "")[:10], "test_end": (metrics.get("test_end") or "")[:10],
            "n_train": metrics.get("n_train"),
        },
    }


PUBLIC_VERIFY_DAYS = 7             # 予測と観測の照合を出す直近の日数


def _code_is_fog(code, station=None):
    """実測の現象コード(2コード同時観測はパック値)が川霧かどうか。prediction_log._fog_sqlと同じ判定。"""
    if code is None:
        return None
    code = int(code)
    return (code // 100 if code > 10 else code) in fog_features.target_codes(station)


def _build_public_verification(conn, today):
    """直近の日ごとに、前日夜の本番の予測(その日0時より前で最新のrun)と、カメラ画像のAI判定(観測)を
    照合する。「高」(=しきい値以上、predicted_code=1)を予測あり、川霧コードを観測ありとし、
    地点×時刻ごとに 的中(hit)・空振り(fa)・見逃し(miss) を数える。照合できる行が無い日は出さない。"""
    out = []
    for back in range(PUBLIC_VERIFY_DAYS, 0, -1):
        d = today - timedelta(days=back)
        day_start = d.strftime("%Y-%m-%d 00:00:00")
        run_at = conn.execute(
            """SELECT MAX(run_at) FROM predictions WHERE model_type LIKE ? AND model_type NOT LIKE '%[再予測]'
               AND run_at < ? AND target_datetime BETWEEN ? AND ?""",
            (prediction_log.CURRENT_MODEL_LIKE, day_start, day_start, d.strftime("%Y-%m-%d 23:59:59")),
        ).fetchone()[0]
        if not run_at:
            continue
        rows = conn.execute(
            """SELECT station, target_datetime, predicted_code, actual_code FROM predictions
               WHERE run_at = ? AND target_datetime BETWEEN ? AND ? AND COALESCE(daylight, 1) = 1
               AND actual_code IS NOT NULL""",
            (run_at, day_start, d.strftime("%Y-%m-%d 23:59:59")),
        ).fetchall()
        if not rows:
            continue
        counts = {"hit": 0, "fa": 0, "miss": 0, "cn": 0}
        cells = {}
        for station, target_dt, pred, actual in rows:
            p_fog, a_fog = int(pred or 0) == 1, _code_is_fog(actual, station)
            kind = "hit" if p_fog and a_fog else "fa" if p_fog else "miss" if a_fog else "cn"
            counts[kind] += 1
            if kind != "cn":
                cells.setdefault(station, {})[int(target_dt[11:13])] = kind
        stations = [{"name": n, "region": STATION_REGION.get(n) or "その他", "hours": cells[n]}
                    for n in STATION_NAMES if n in cells]
        weekdays = "月火水木金土日"
        out.append({
            "date": d.strftime("%Y-%m-%d"), "date_label": f"{d.month}/{d.day}({weekdays[d.weekday()]})",
            "run_at": run_at[:16], "n": len(rows), "counts": counts, "stations": stations,
        })
    return out


def _public_forecast_safe():
    now = time.monotonic()
    with _public_forecast_cache_lock:
        cached = _public_forecast_cache["data"]
        if cached is not None and now - _public_forecast_cache["at"] < PUBLIC_CACHE_SECONDS:
            return cached
    try:
        data = _build_public_forecast()
    except Exception as e:
        log.warning("Failed to build public forecast: %s", e)
        return cached
    with _public_forecast_cache_lock:
        _public_forecast_cache["data"] = data
        _public_forecast_cache["at"] = now
    return data


PUBLIC_STATS_CACHE_SECONDS = 6 * 3600
PUBLIC_STATS_MIN_N = 30            # 月別・時刻別の割合を出すのに必要な判定件数(少ないと割合がぶれるため)
_public_stats_cache = {"data": None, "at": 0.0}
_public_stats_cache_lock = threading.Lock()


def _build_public_station_stats():
    """地点ごとの過去の川霧の出やすさ(月別・時刻別、日中のみ・人による訂正込み)と、研究の規模の集計。"""
    _main_df, phenom_df, station_names = fog_data.load_river_fog_data()
    phenom_df, _n = fog_data.apply_phenomena_corrections(phenom_df, fog_data.load_phenomena_corrections())
    dts = phenom_df["datetime"].reset_index(drop=True)
    months = dts.dt.month.to_numpy()
    hours = dts.dt.hour.to_numpy()
    fog_codes = set(SUMMARY_FOG_CODES)
    stations, total_n, total_fog = {}, 0, 0
    for name in station_names:
        info = CAMERA_INFO.get(name, {})
        lat, lon = info.get("lat"), info.get("lon")
        if lat is None or lon is None:
            continue
        raw = phenom_df[name].reset_index(drop=True)
        mapping = {v: fog_features.primary_phenom_code(encode_phenomena_cell(v)) for v in raw.dropna().unique()}
        primary = raw.map(mapping).to_numpy(dtype="float64")
        valid = ~np.isnan(primary) & fog_features.daylight_mask(dts, lat, lon)
        is_fog = np.isin(primary, list(fog_codes)) & valid
        n, n_fog = int(valid.sum()), int(is_fog.sum())
        total_n += n
        total_fog += n_fog

        def rates(keys, key_values):
            out = []
            for k in key_values:
                sel = valid & (keys == k)
                cnt = int(sel.sum())
                out.append({"key": k, "n": cnt,
                            "rate": round(float(is_fog[sel].sum()) / cnt, 4) if cnt >= PUBLIC_STATS_MIN_N else None})
            return out

        stations[name] = {
            "n": n, "n_fog": n_fog, "rate": round(n_fog / n, 4) if n else None,
            "by_month": rates(months, list(range(1, 13))),
            "by_hour": rates(hours, list(range(5, 19))),
        }
    return {
        "stations": stations,
        "total_n": total_n,
        "total_fog": total_fog,
        "period_start": dts.min().strftime("%Y/%m/%d") if len(dts) else None,
        "period_end": dts.max().strftime("%Y/%m/%d") if len(dts) else None,
    }


def _refresh_public_station_stats():
    try:
        data = _build_public_station_stats()
    except Exception as e:
        log.warning("Failed to build public station stats: %s", e)
        data = None
    with _public_stats_cache_lock:
        if data is not None:
            _public_stats_cache["data"] = data
            _public_stats_cache["at"] = time.monotonic()
        _public_stats_cache["building"] = False


def _public_station_stats_safe():
    """集計には20秒ほどかかるため、閲覧者を待たせないよう裏で作り直し、それまでは前回の
    集計(初回はNone=「準備中」表示)を返す。"""
    now = time.monotonic()
    with _public_stats_cache_lock:
        cached = _public_stats_cache["data"]
        fresh = cached is not None and now - _public_stats_cache["at"] < PUBLIC_STATS_CACHE_SECONDS
        if fresh or _public_stats_cache.get("building"):
            return cached
        _public_stats_cache["building"] = True
    threading.Thread(target=_refresh_public_station_stats, daemon=True).start()
    return cached


def _public_station_weather(name):
    """公開の地点ページの見出しに出す、その地点の最新の気温・湿度(地点固有の値が無ければNone)。"""
    try:
        return _latest_station_weather(name, None)
    except Exception as e:
        log.warning("Public page: failed to load station weather for %s: %s", name, e)
        return None


def _public_station_outlook(station, data):
    """地点ページの見出しの下に出す、日ごとの見込みの1行(「本日 6〜7時に「高」」など)。"""
    out = []
    if not station or not data:
        return out
    for day, d in zip(data["days"], station["days"]):
        top = d.get("max") or 0
        if top < 2:
            continue
        hours = sorted(h for h, lv in d["hours"].items() if lv == top)
        span = f"{hours[0]}時" + (f"〜{hours[-1]}時" if len(hours) > 1 else "")
        out.append({"day": day["label"], "date": day["date_label"], "level": top,
                    "short": PUBLIC_LEVELS[top]["short"], "span": span})
    return out


@app.route("/forecast/station/<name>")
def public_station(name):
    if name not in STATION_NAMES:
        abort(404)
    data = _public_forecast_safe()
    station = next((s for s in (data or {}).get("stations", []) if s["name"] == name), None)
    stats = _public_station_stats_safe()
    cam_id = CAMERA_INFO.get(name, {}).get("cam_id")
    return render_template(
        "public_station.html", data=data, station=station, levels=PUBLIC_LEVELS, name=name,
        region=STATION_REGION.get(name),
        stats=(stats or {}).get("stations", {}).get(name),
        official_url=_river_go_jp_map_url(cam_id) if cam_id else None, min_n=PUBLIC_STATS_MIN_N,
        weather=_public_station_weather(name),
        outlook=_public_station_outlook(station, data),
        now_hour=now_jst_naive().hour, now_month=now_jst_naive().month,
        lat=CAMERA_INFO.get(name, {}).get("lat"), lon=CAMERA_INFO.get(name, {}).get("lon"),
    )


@app.after_request
def _public_noindex(response):
    if request.path.startswith(PUBLIC_PREFIX):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.route("/forecast")
def public_forecast():
    data = _public_forecast_safe()
    return render_template("public_forecast.html", data=data, levels=PUBLIC_LEVELS)


@app.route("/forecast/about")
def public_about():
    data = _public_forecast_safe()
    return render_template("public_about.html", data=data, levels=PUBLIC_LEVELS, mid_ratio=PUBLIC_MID_RATIO,
                           stats=_public_station_stats_safe())


@app.route("/login", methods=["GET", "POST"])
def login():
    next_path = request.values.get("next") or url_for("dashboard")
    # オープンリダイレクト対策。ブラウザは"\"を"/"とみなし、タブ・改行は読み飛ばすため、
    # "/\evil.com"や"/<TAB>/evil.com"も外部サイト("//evil.com")への移動になってしまう
    # (2026-10-08の監査で指摘)。
    if (not next_path.startswith("/") or next_path.startswith("//") or "\\" in next_path
            or any(ord(c) < 0x20 or ord(c) == 0x7f for c in next_path)):
        next_path = url_for("dashboard")

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
            # 管理者アカウントは、公開URLから誰でも5回間違えるだけでロックでき、管理者自身も
            # 入れなくなってしまう(解除できる人がいなくなる)。Tailscale内からのログインに限り、
            # ロック中でも正しいパスワードなら入れるようにする(ロック自体は残すので、
            # 公開URLからは引き続き入れない。解除はアカウント管理画面で行う)。
            admin_internal = (existing is not None and existing["role"] == "admin"
                              and not _is_funnel_request())
            if existing and existing["locked_at"] and not admin_internal:
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
                start_settings = user_settings.normalize(users.get_user_settings_raw(conn, user["id"]), STATION_NAMES)
            finally:
                conn.close()
            # 行き先の指定が無い(ダッシュボードへ行く)ときは、設定画面で選んだ「最初に開くページ」へ
            if next_path == url_for("dashboard"):
                next_path = _start_page_url(start_settings, user["favorite_stations"]) or next_path
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
    user_id = session.get("user_id")
    if user_id:
        conn = _users_conn()
        try:
            users.record_logout(conn, user_id, now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"))
        finally:
            conn.close()
    session.clear()
    return redirect(url_for("public_forecast"))


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
            # 単純な部分文字列置換だと、対象ユーザー名が別の(メンション対象ではない)
            # 名前の接頭辞になっている場合(例: 対象が"Yu"で本文中に無関係な"@Yuki"がある)、
            # 誤って"@Yu"だけをハイライトし"ki"が分離されてしまっていた。直後が単語構成
            # 文字(\w、日本語含む)で続いていないマッチだけを対象にする
            # (2026-09-13の監査で指摘)。
            pattern = re.compile(re.escape(str(token)) + r"(?!\w)")
            escaped = pattern.sub(f'<span class="mention-tag">{token}</span>', escaped)
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
    # 川霧予測ゲームの的中実績
    {"id": "gamble_first_win", "label": "初勝利", "desc": "川霧予測ゲームで初めて賭けを的中させた", "icon": "star", "points": 200,
     "check": lambda s: s["gamble_won_count"] >= 1},
    {"id": "gamble_won_20", "label": "ギャンブラー", "desc": "川霧予測ゲームで20回的中させた", "icon": "star", "points": 600,
     "check": lambda s: s["gamble_won_count"] >= 20},
    {"id": "gamble_won_50", "label": "目利き", "desc": "川霧予測ゲームで50回的中させた", "icon": "star", "points": 1500,
     "check": lambda s: s["gamble_won_count"] >= 50},
    {"id": "gamble_won_100", "label": "川霧予知能力者", "desc": "川霧予測ゲームで100回的中させた", "icon": "crown", "points": 3000,
     "check": lambda s: s["gamble_won_count"] >= 100},
    {"id": "gamble_won_500", "label": "川霧の神様", "desc": "川霧予測ゲームで500回的中させた", "icon": "crown", "points": 10000,
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
    {"id": "fog_quiz_correct_10", "label": "予報士見習い", "desc": "川霧予報クイズに10回正解した", "icon": "forecast", "points": 300,
     "check": lambda s: s["fog_quiz_correct"] >= 10},
    {"id": "fog_quiz_correct_50", "label": "予報士", "desc": "川霧予報クイズに50回正解した", "icon": "forecast", "points": 1200,
     "check": lambda s: s["fog_quiz_correct"] >= 50},
    {"id": "fog_quiz_correct_200", "label": "名予報士", "desc": "川霧予報クイズに200回正解した", "icon": "crown", "points": 4000,
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

# --- ショップ経済インフレ対策(2026-09-07) -----------------------------------------
# 川霧予測ゲームで数千万〜億単位のポイントを稼ぐユーザーが出てきた結果、既存のショップ価格
# (数百〜数万pt)がほぼ無価値になったため、高額品ほど強く底上げする形で全カタログの
# costを一括値上げする。安価な品(BASE以下)はそのまま据え置き、高額品ほど指数的に
# 値上がりする(単純な定数倍だと安い品まで一律で上がってしまうため)。
_SHOP_INFLATION_BASE = 1500
_SHOP_INFLATION_EXPONENT = 0.85


def _inflated_cost(cost):
    if cost <= _SHOP_INFLATION_BASE:
        return cost
    multiplier = (cost / _SHOP_INFLATION_BASE) ** _SHOP_INFLATION_EXPONENT
    return int(round(cost * multiplier / 10.0)) * 10


# 買い切りをやめ、毎月この割合分を使用料として引き落とす(装備中のアイテムのみ課金、
# 未装備なら請求しない)。costに対する割合なので安い品は安く・高い品は高くなり、
# 月額が元の購入価格を超えることは割合が1未満である限り起こらない。
MONTHLY_FEE_RATIO = 0.12


def _monthly_fee(cost):
    return max(1, round(cost * MONTHLY_FEE_RATIO))


def _reprice_for_inflation(definitions):
    """定義リストの各itemのcostを_inflated_costで置き換え、月額使用料(monthly_fee)も
    合わせて計算してitemに埋め込む(破壊的変更)。ショップ画面はitem.monthly_feeを
    そのまま表示するだけでよい。"""
    for item in definitions:
        item["cost"] = _inflated_cost(item["cost"])
        item["monthly_fee"] = _monthly_fee(item["cost"])
    return definitions


# --- ポイント交換アバターフレーム -------------------------------------------------
# 実績ポイントで交換できるアバター装飾枠。「段階(グレード)」×「テーマ」の組み合わせで
# 4x5=20種類になる。CSS側は段階クラス(リング太さ・発光・アニメーション)とテーマクラス
# (配色)を掛け合わせて表現するため、フレーム自体を20個ハードコードする必要はない
# (style.cssの.avatar-frame関連ルール参照)。
FRAME_GRADES = _reprice_for_inflation([
    {"id": "bronze", "label": "銅", "cost": 1000},
    {"id": "silver", "label": "銀", "cost": 2500},
    {"id": "gold", "label": "金", "cost": 5000},
    {"id": "platinum", "label": "虹", "cost": 10000},
    {"id": "legend", "label": "伝説", "cost": 20000},
])
FRAME_THEMES = [
    {"id": "kiri", "label": "川霧"},
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
        "monthly_fee": grade["monthly_fee"],
        "theme": theme["id"], "theme_label": theme["label"],
    }
    for grade in FRAME_GRADES for theme in FRAME_THEMES
]
FRAME_BY_ID = {f["id"]: f for f in FRAME_DEFINITIONS}

# --- 称号・名前の色・ダッシュボード配色・演出 ---------------------------------------
# フレーム以外のショップ品。装備の仕組みはusers.py側で共通化しているが(user_cosmetics、
# category列で区別)、カタログ(何が買えるか)はここに種類ごとに定義する。
TITLE_DEFINITIONS = _reprice_for_inflation([
    {"id": "regular", "label": "常連さん", "cost": 500},
    {"id": "early_riser", "label": "早起き名人", "cost": 1200},
    {"id": "fog_walker", "label": "川霧使い", "cost": 3000},
    {"id": "tadami_master", "label": "只見川マスター", "cost": 8000},
    {"id": "legendary_observer", "label": "伝説の観測者", "cost": 15000},
    {"id": "dopagaki", "label": "ドパガキ", "cost": 50000},
    {"id": "legendary_gambler", "label": "伝説のギャンブラー", "cost": 1000000},
])
# 経済インフレ対策と同時に追加した最高レア度の称号(2026-09-07)。値上げ後の最高額品
# よりさらに一段上に置くため、_reprice_for_inflationは通さず直接「狂った」価格を付ける。
TITLE_DEFINITIONS.append(
    {"id": "kiri_no_kamigami", "label": "川霧の神々に愛されし者", "cost": 1000000000,
     "monthly_fee": _monthly_fee(1000000000), "added": "2026-09-07"}
)
TITLE_DEFINITIONS.append(
    {"id": "gem_title_legend", "label": "川霧の伝承者", "currency": "gems", "cost_gems": 90000, "premium": True}
)
TITLE_BY_ID = {t["id"]: t for t in TITLE_DEFINITIONS}

NAME_COLOR_DEFINITIONS = _reprice_for_inflation([
    {"id": "red", "label": "赤", "cost": 800, "css_class": "name-color-red"},
    {"id": "blue", "label": "青", "cost": 800, "css_class": "name-color-blue"},
    {"id": "green", "label": "緑", "cost": 800, "css_class": "name-color-green"},
    {"id": "gold", "label": "金", "cost": 2000, "css_class": "name-color-gold"},
    {"id": "rainbow", "label": "虹色", "cost": 5000, "css_class": "name-color-rainbow"},
])
NAME_COLOR_DEFINITIONS.append(
    {"id": "gem_name_color_aurora", "label": "極光の名前", "css_class": "shop-gem-premium-text",
     "currency": "gems", "cost_gems": 30000}
)
NAME_COLOR_BY_ID = {c["id"]: c for c in NAME_COLOR_DEFINITIONS}

DASHBOARD_THEME_DEFINITIONS = _reprice_for_inflation([
    {"id": "yuuyake", "label": "夕焼け", "cost": 1500, "css_class": "dash-theme-yuuyake"},
    {"id": "hoshizora", "label": "星空", "cost": 2500, "css_class": "dash-theme-hoshizora"},
    {"id": "shinryoku", "label": "新緑", "cost": 1500, "css_class": "dash-theme-shinryoku"},
    {"id": "yukigesho", "label": "雪景色", "cost": 1500, "css_class": "dash-theme-yukigesho"},
])
DASHBOARD_THEME_DEFINITIONS.append(
    {"id": "gem_dashboard_aurora", "label": "黄金の川霧配色", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 25000}
)
DASHBOARD_THEME_BY_ID = {d["id"]: d for d in DASHBOARD_THEME_DEFINITIONS}

EFFECT_DEFINITIONS = _reprice_for_inflation([
    {"id": "fog_drift", "label": "川霧の揺らめき", "cost": 4000, "css_class": "fx-fog-drift"},
    {"id": "sparkle", "label": "きらめき", "cost": 4000, "css_class": "fx-sparkle"},
    {"id": "aurora_glow", "label": "オーロラの光", "cost": 6000, "css_class": "fx-aurora-glow"},
])
EFFECT_DEFINITIONS.append(
    {"id": "gem_effect_aurora", "label": "黄金の煌めき", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 35000}
)
EFFECT_BY_ID = {e["id"]: e for e in EFFECT_DEFINITIONS}

BANNER_DEFINITIONS = _reprice_for_inflation([
    {"id": "kiri", "label": "川霧景色", "cost": 2000, "css_class": "banner-kiri"},
    {"id": "yuuyake", "label": "夕焼け", "cost": 2000, "css_class": "banner-yuuyake"},
    {"id": "yukigesho", "label": "雪景色", "cost": 2000, "css_class": "banner-yukigesho"},
    {"id": "hoshizora", "label": "星空", "cost": 3000, "css_class": "banner-hoshizora"},
])
BANNER_DEFINITIONS.append(
    {"id": "gem_banner_aurora", "label": "黄金の川面", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 50000}
)
BANNER_BY_ID = {b["id"]: b for b in BANNER_DEFINITIONS}

DM_THEME_DEFINITIONS = _reprice_for_inflation([
    {"id": "kiri", "label": "川霧", "cost": 1500, "css_class": "dm-theme-kiri"},
    {"id": "yuuyake", "label": "夕焼け", "cost": 1500, "css_class": "dm-theme-yuuyake"},
    {"id": "mint", "label": "ミント", "cost": 1500, "css_class": "dm-theme-mint"},
])
DM_THEME_DEFINITIONS.append(
    {"id": "gem_dm_theme_aurora", "label": "黄金の便り", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 15000}
)
DM_THEME_BY_ID = {d["id"]: d for d in DM_THEME_DEFINITIONS}

# 名前の色と同じCSSクラス(name-color-*)をそのまま流用する(色の見た目自体は
# 用途によらず共通なので、称号用に別のクラスを増やす必要が無い)。
TITLE_COLOR_DEFINITIONS = _reprice_for_inflation([
    {"id": "red", "label": "赤", "cost": 600, "css_class": "name-color-red"},
    {"id": "blue", "label": "青", "cost": 600, "css_class": "name-color-blue"},
    {"id": "green", "label": "緑", "cost": 600, "css_class": "name-color-green"},
    {"id": "gold", "label": "金", "cost": 1500, "css_class": "name-color-gold"},
    {"id": "rainbow", "label": "虹色", "cost": 4000, "css_class": "name-color-rainbow"},
])
TITLE_COLOR_DEFINITIONS.append(
    {"id": "gem_title_color_aurora", "label": "極上の称号色", "css_class": "shop-gem-premium-text",
     "currency": "gems", "cost_gems": 20000}
)
TITLE_COLOR_BY_ID = {c["id"]: c for c in TITLE_COLOR_DEFINITIONS}

MARKER_DEFINITIONS = _reprice_for_inflation([
    {"id": "hall_of_fame", "label": "殿堂入り章", "cost": 25000, "icon": "★"},
])
# 経済インフレ対策と同時に追加した最高レア度のマーカー(2026-09-07)。
MARKER_DEFINITIONS.append(
    # 殿堂入り章(★)と見た目が被らないよう冠アイコンにし、買い切りではなく毎月
    # 使用料がかかる(=払い続けられなくなれば自動で外れる)仕様に合わせて「永久」を
    # 含まない名前にしている。
    {"id": "aurora_crown", "label": "暁光の冠", "cost": 500000000, "icon": "👑", "premium": True,
     "monthly_fee": _monthly_fee(500000000), "added": "2026-09-07"}
)
MARKER_BY_ID = {m["id"]: m for m in MARKER_DEFINITIONS}

# --- ジェム専用アイテム(2026-09-11、2026-09-11改訂) --------------------------------
# 観測報告の報酬として貯まるジェムでしか買えない最高級アイテム。ポイント経済とは
# 完全に別会計(user_gem_items)で、月額使用料も課さない買い切り品にしている
# (ジェムは観測報告という単一の獲得経路しかなく、ポイントほど大量に稼げないため)。
# 独立したショップタブは持たず、殿堂入り章タブに他の装飾品と並べて表示する
# (ポイント品と別画面に分けない・ジェムは最高級品にしか使わない、という方針のため)。
GEM_SHOP_DEFINITIONS = [
    {"id": "observer_glasses", "label": "観測者の眼鏡", "cost_gems": 2000, "icon": "🔍",
     "desc": "遠くの川霧もくっきり見通せるという、観測者の間で語られる縁起物。"},
    {"id": "forecaster_compass", "label": "川霧予報士の羅針盤", "cost_gems": 10000, "icon": "🧭",
     "desc": "明日の川霧の気配を示すとされる、予報士憧れの逸品。"},
    {"id": "river_fog_crystal", "label": "川霧の結晶", "cost_gems": 40000, "icon": "💎", "premium": True,
     "desc": "濃い川霧の日にしか現れないという伝説の結晶。"},
    {"id": "legendary_notebook", "label": "伝説の観測手帳", "cost_gems": 120000, "icon": "📔", "premium": True,
     "desc": "数々の重要報告を成し遂げた観測者だけが手にできる、史上最高級の手帳。"},
]
GEM_SHOP_BY_ID = {g["id"]: g for g in GEM_SHOP_DEFINITIONS}

# --- 2026-09-03追加のショップ拡張分 ------------------------------------------------
# NEW_ITEM_ADDED_ATはこのバッチで追加したカタログ品に付けた"added"日付。ショップ側で
# 一定期間(SHOP_NEW_BADGE_DAYS)だけ「NEW」バッジを出すのに使う。
NEW_ITEM_ADDED_AT = "2026-09-03"
SHOP_NEW_BADGE_DAYS = 21

TITLE_BADGE_DEFINITIONS = _reprice_for_inflation([
    {"id": "sparkle", "label": "きらきら", "cost": 600, "icon": "✨", "added": NEW_ITEM_ADDED_AT},
    {"id": "fire", "label": "めらめら", "cost": 600, "icon": "🔥", "added": NEW_ITEM_ADDED_AT},
    {"id": "drop", "label": "しずく", "cost": 800, "icon": "💧", "added": NEW_ITEM_ADDED_AT},
    {"id": "gem", "label": "きらめく宝石", "cost": 2500, "icon": "💎", "added": NEW_ITEM_ADDED_AT},
])
TITLE_BADGE_DEFINITIONS.append(
    {"id": "gem_badge_star", "label": "極上の輝き", "icon": "🌟", "currency": "gems", "cost_gems": 15000}
)
TITLE_BADGE_BY_ID = {t["id"]: t for t in TITLE_BADGE_DEFINITIONS}

BUBBLE_STYLE_DEFINITIONS = _reprice_for_inflation([
    {"id": "round", "label": "まる型", "cost": 1200, "css_class": "bubble-style-round", "added": NEW_ITEM_ADDED_AT},
    {"id": "square", "label": "かく型", "cost": 1200, "css_class": "bubble-style-square", "added": NEW_ITEM_ADDED_AT},
    {"id": "handwritten", "label": "手書き風", "cost": 2500, "css_class": "bubble-style-handwritten", "added": NEW_ITEM_ADDED_AT},
])
BUBBLE_STYLE_DEFINITIONS.append(
    {"id": "gem_bubble_aurora", "label": "黄金の吹き出し", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 15000}
)
BUBBLE_STYLE_BY_ID = {b["id"]: b for b in BUBBLE_STYLE_DEFINITIONS}

AVATAR_BG_DEFINITIONS = _reprice_for_inflation([
    {"id": "glow_blue", "label": "青の光暈", "cost": 1800, "css_class": "avatar-bg-glow-blue", "added": NEW_ITEM_ADDED_AT},
    {"id": "glow_gold", "label": "金の光暈", "cost": 3500, "css_class": "avatar-bg-glow-gold", "added": NEW_ITEM_ADDED_AT},
    {"id": "mist", "label": "川霧の背景", "cost": 2200, "css_class": "avatar-bg-mist", "added": NEW_ITEM_ADDED_AT},
])
AVATAR_BG_DEFINITIONS.append(
    {"id": "gem_avatar_bg_aurora", "label": "後光の輝き", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 25000}
)
AVATAR_BG_BY_ID = {a["id"]: a for a in AVATAR_BG_DEFINITIONS}

RANK_STYLE_DEFINITIONS = _reprice_for_inflation([
    {"id": "gold_number", "label": "金の数字", "cost": 1500, "css_class": "rank-style-gold", "added": NEW_ITEM_ADDED_AT},
    {"id": "neon_number", "label": "ネオンの数字", "cost": 1500, "css_class": "rank-style-neon", "added": NEW_ITEM_ADDED_AT},
    {"id": "serif_number", "label": "書道体の数字", "cost": 1500, "css_class": "rank-style-serif", "added": NEW_ITEM_ADDED_AT},
])
RANK_STYLE_DEFINITIONS.append(
    {"id": "gem_rank_aurora", "label": "至高の数字", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 20000}
)
RANK_STYLE_BY_ID = {r["id"]: r for r in RANK_STYLE_DEFINITIONS}

# soundは効果音の種類ID。実際の音はWeb Audio APIでmember_detail.html側が合成するので、
# 音声ファイルは持たない(依存を増やさないため)。
PROFILE_SOUND_DEFINITIONS = _reprice_for_inflation([
    {"id": "chime", "label": "チャイム", "cost": 1000, "sound": "chime", "added": NEW_ITEM_ADDED_AT},
    {"id": "bell", "label": "鈴の音", "cost": 1000, "sound": "bell", "added": NEW_ITEM_ADDED_AT},
    {"id": "kirakira", "label": "きらめき音", "cost": 1800, "sound": "kirakira", "added": NEW_ITEM_ADDED_AT},
])
PROFILE_SOUND_DEFINITIONS.append(
    # 音自体はkirakiraを流用(専用音を追加するとJS側の対応も必要になるため)。
    {"id": "gem_sound_aurora", "label": "黄金の調べ", "sound": "kirakira", "currency": "gems", "cost_gems": 10000}
)
PROFILE_SOUND_BY_ID = {p["id"]: p for p in PROFILE_SOUND_DEFINITIONS}

# 季節限定コレクション。available_from/available_untilの期間外は新規購入できないが、
# 既に持っている分はそのまま装備・表示し続けられる(買い方だけを期間で絞る)。
SEASONAL_DEFINITIONS = _reprice_for_inflation([
    {
        "id": "summer_2026", "label": "夏祭りの提灯", "cost": 1800, "css_class": "seasonal-summer",
        "available_from": "2026-07-01", "available_until": "2026-08-31", "added": NEW_ITEM_ADDED_AT,
    },
    {
        "id": "autumn_2026", "label": "紅葉の川面", "cost": 1800, "css_class": "seasonal-autumn",
        "available_from": "2026-10-01", "available_until": "2026-11-30", "added": NEW_ITEM_ADDED_AT,
    },
    {
        "id": "newyear_2027", "label": "迎春の川霧", "cost": 2200, "css_class": "seasonal-newyear",
        "available_from": "2026-12-15", "available_until": "2027-01-15", "added": NEW_ITEM_ADDED_AT,
    },
])
SEASONAL_DEFINITIONS.append(
    # 期間限定ではなく通年購入できる(available_from/untilを付けない)、ジェム限定の一点。
    {"id": "gem_seasonal_aurora", "label": "永遠の黄金川霧", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 45000}
)
SEASONAL_BY_ID = {s["id"]: s for s in SEASONAL_DEFINITIONS}

GACHA_COST = 5000
GACHA_BIG_REFUND_MULTIPLIER = 2.5
GACHA_BIG_REFUND_WEIGHT = 5
GACHA_CONSOLATION_POINTS = 300

# --- 川霧予測ゲーム・デイリーゲーム共通の消費アイテム ---------------------------------
CONSUMABLE_DEFINITIONS = [
    {
        "id": "insurance_ticket", "label": "保険チケット", "cost": 300,
        "desc": "川霧予測ゲームの賭けがハズレても、賭けたポイントの半額が払い戻されます。",
    },
    {
        "id": "boost_ticket", "label": "倍率ブーストチケット", "cost": 250,
        "desc": "川霧予測ゲームの賭けの倍率が10%上がります(的中時の払い戻しが増えます)。",
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
        "desc": "川霧予測ゲームの賭けの倍率が25%上がります(倍率ブーストチケットの上位版。同時使用時はこちらが優先されます)。",
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

# --- 川霧予測ゲーム ---------------------------------------------------------------
# 「この地点でこの時刻に川霧が出るか」にポイントを賭けるゲーム。
# 倍率 = 地点の難易度(過去の川霧発生率が低いほど高倍率) × 先の長さ(遠い予測ほど高倍率)
#      × 時間精度(ざっくり日付だけ<時間帯<正確な1時間、細かく当てるほど高倍率)
# 賭けた時点でポイントは引かれ(fog_bets.bet_amountがtotal_spent_pointsに含まれる)、
# 的中すれば bet_amount * multiplier をgrant_pointsで払い戻す。外れれば戻ってこない
# (名前どおりの「川霧予測ゲーム」として、当てずっぽうの丁半博打にならないよう
# 難易度に応じた倍率を付ける)。
class _ConsumableUnavailable(Exception):
    """賭け金の残高チェックと同一トランザクション内でチケットを消費する際、
    在庫チェック直後に別リクエストが先に使い切っていた場合に投げる(その場合
    spend_points_if_available側でロールバックし、賭けは成立させない)。"""


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

# --- デイリー川霧予報クイズ(無料・スキル型、川霧予測ゲームと違いポイントを賭けない) -------
# 毎日同じ地点を全員に出題する(日付から決定論的に選ぶ)ことで、その日1回だけの
# 気軽な予想ゲームにする。的中報酬に連続的中日数のボーナスを付け、継続を後押しする。
FOG_QUIZ_BASE_POINTS = 500
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
PHENOM_QUIZ_POINTS = 500
OBSERVATION_REPORT_REWARD_POINTS = 2000

# --- ジェム(観測報告専用の第二通貨) ------------------------------------------------
# 重要度は報告者が選ぶ(低・中・高)。「高」だけは緊急性を考慮し、管理者の承認を待たず
# 報告した時点で満額を即時付与する。低・中は誤報乱発によるファーミングを防ぐため、
# 報告時点では少額の参加ボーナスだけを出し、管理者が実際に訂正コードを変えて承認した
# 時にまとめて満額になるよう差額を追加付与する(既存のポイント報酬と同じ承認条件、
# corrected_code != reported_codeを使う)。
IMPORTANCE_LEVELS = ("low", "medium", "high")
IMPORTANCE_LABELS = {"low": "低", "medium": "中", "high": "高"}
OBSERVATION_REPORT_REASON_MAX_LENGTH = 300
GEM_REWARD_TOTAL = {"low": 10, "medium": 20, "high": 40}
GEM_REWARD_ON_SUBMIT = {"low": 1, "medium": 1, "high": GEM_REWARD_TOTAL["high"]}
GEM_REWARD_ON_APPROVE = {
    level: GEM_REWARD_TOTAL[level] - GEM_REWARD_ON_SUBMIT[level] for level in IMPORTANCE_LEVELS
}
# 観測者・管理者がstation_correct_observation(承認待ちを介さずその場で直接訂正する方)を
# 使った時の即時ジェム報酬。一般ユーザーの「報告」と違い、担当権限を持つ本人の判断で
# 確定させる操作のため承認は挟まず、最高重要度の報告(GEM_REWARD_TOTAL["high"]=40)より
# 高い50ジェムを即時付与する(2026-09-12追加)。
GEM_REWARD_DIRECT_CORRECTION = 50
PHENOM_QUIZ_CANDIDATES_CACHE_SECONDS = 600


def _phenom_category_id_for_code(code):
    # codeは2コード同時観測(2026-09対応)のパック済み値のこともあるため、主コードだけで
    # 判定する(クイズは主コードのみで採点する、というシンプルな方針)。
    primary = primary_phenom_code(code)
    if primary != primary:  # NaN
        return None
    primary = int(primary)
    for cat in PHENOM_QUIZ_CATEGORIES:
        if primary in cat["codes"]:
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
    """観測日が終わっている(今日より前の)未解決のデイリー川霧予報クイズを答え合わせする。
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
            codes = window[station].apply(encode_phenomena_cell).apply(primary_phenom_code)
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
            codes = merged[name].apply(encode_phenomena_cell).apply(primary_phenom_code)
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
# 終わっていないデータを「川霧は観測されなかった」と誤認して外れ扱いにしてしまう
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
                # (猶予期間内は「まだ観測されていない」を「川霧が出なかった」と混同しない)。
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
            codes = merged[station].apply(encode_phenomena_cell).apply(primary_phenom_code)
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
        month_comment_counts = users.comment_counts_since(conn, month_start)
        month_message_counts = users.message_counts_since(conn, month_start)
        month_gamble_win_counts = users.won_bet_counts_since(conn, month_start)
        month_points_earned = users.points_earned_since(conn, month_start)
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
            "month_comment_count": month_comment_counts.get(u["id"], 0),
            "month_message_count": month_message_counts.get(u["id"], 0),
            "month_gamble_win_count": month_gamble_win_counts.get(u["id"], 0),
            "month_points_earned": month_points_earned.get(u["id"], 0),
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
    # ログイン以外のランキング種別は報酬が絡まないため、シンプルに今月分のみ用意する。
    extra_rankings = [
        {
            "id": "comment", "label": "コメント投稿数", "unit": "件",
            "count_key": "month_comment_count",
            "entries": sorted(
                [m for m in members if m["month_comment_count"] > 0],
                key=lambda m: -m["month_comment_count"],
            )[:5],
        },
        {
            "id": "message", "label": "メッセージ送信数", "unit": "件",
            "count_key": "month_message_count",
            "entries": sorted(
                [m for m in members if m["month_message_count"] > 0],
                key=lambda m: -m["month_message_count"],
            )[:5],
        },
        {
            "id": "gamble_win", "label": "川霧予測ゲーム勝利数", "unit": "回",
            "count_key": "month_gamble_win_count",
            "entries": sorted(
                [m for m in members if m["month_gamble_win_count"] > 0],
                key=lambda m: -m["month_gamble_win_count"],
            )[:5],
        },
        {
            "id": "points", "label": "獲得ポイント", "unit": "pt",
            "count_key": "month_points_earned",
            "entries": sorted(
                [m for m in members if m["month_points_earned"] > 0],
                key=lambda m: -m["month_points_earned"],
            )[:5],
        },
    ]
    return render_template(
        "members_list.html", members=members, ranking=ranking, month_ranking=month_ranking,
        extra_rankings=extra_rankings,
    )


def _summarize_feedback_with_gemini(body):
    """意見箱の投稿本文をGeminiで整理する。失敗時はNoneを返す(投稿自体は原文のまま保存する)。"""
    api_keys = collect.get_api_keys()
    if not api_keys:
        return None
    try:
        from google.genai import types
        model = os.environ.get("GEMINI_MODEL", collect.DEFAULT_GEMINI_MODEL)
        config = types.GenerateContentConfig(
            system_instruction=FEEDBACK_SUMMARY_SYSTEM_INSTRUCTION, temperature=0.2,
        )
        response = collect._gemini_generate_with_fallback(model, body, config, api_keys, label="feedback_summary")
        if response is None or not getattr(response, "text", None):
            return None
        return response.text.strip()
    except Exception as e:
        log.warning("Feedback summary generation failed: %s", e)
        return None


@app.route("/feedback")
def community_feedback():
    """コミュニティ意見箱: 不具合報告・要望・質問を自由記述で管理者に送れるページ。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        my_reports = users.list_feedback_reports_by_user(conn, my_id)
    finally:
        conn.close()
    return render_template("community_feedback.html", my_reports=my_reports)


@app.route("/feedback/submit", methods=["POST"])
def community_feedback_submit():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    body = request.form.get("body", "").strip()[:FEEDBACK_BODY_MAX_LENGTH]
    if not body:
        return _finish("community_feedback", "内容を入力してください。", success=False)
    summary = _summarize_feedback_with_gemini(body)
    now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    conn = _users_conn()
    try:
        users.create_feedback_report(conn, my_id, body, summary, now_str)
        _record_audit("意見箱に投稿", body[:100])
    finally:
        conn.close()
    return _finish("community_feedback", "送信しました。ご協力ありがとうございます。")


@app.route("/admin/feedback")
@requires_admin
def admin_feedback():
    conn = _users_conn()
    try:
        reports = users.list_pending_feedback_reports(conn)
    finally:
        conn.close()
    return render_template("admin_feedback.html", reports=reports)


@app.route("/admin/feedback/<int:report_id>/resolve", methods=["POST"])
@requires_admin
def admin_feedback_resolve(report_id):
    my_id = session.get("user_id")
    conn = _users_conn()
    try:
        report = users.get_feedback_report(conn, report_id)
        if report is None or report["status"] != "pending":
            abort(404)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        users.resolve_feedback_report(conn, report_id, my_id, now_str)
        _record_audit("意見箱の投稿を対応済みにした", f"report_id={report_id}")
    finally:
        conn.close()
    return _finish("admin_feedback", "対応済みにしました。")


@app.route("/admin/clamav")
@requires_admin
def admin_clamav():
    conn = _users_conn()
    try:
        recent_events = [
            e for e in users.list_audit_log(conn, limit=500)
            if "マルウェア" in e["action"] or "ウイルス" in e["action"]
        ][:20]
    finally:
        conn.close()
    rescan_running = _is_script_running("clamav_rescan")
    rescan_log = None
    rescan_log_at = None
    if CLAMAV_RESCAN_LOG_PATH.exists():
        rescan_log = CLAMAV_RESCAN_LOG_PATH.read_text(errors="replace")[-8000:]
        rescan_log_at = datetime.fromtimestamp(CLAMAV_RESCAN_LOG_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return render_template(
        "admin_clamav.html",
        db_info=_clamav_db_info(), service_status=_clamav_service_status(),
        stale_days_threshold=CLAMAV_SIGNATURE_STALE_DAYS, recent_events=recent_events,
        rescan_running=rescan_running, rescan_log=rescan_log, rescan_log_at=rescan_log_at,
        rescan_dirs=[str(d) for d in CLAMAV_RESCAN_DIRS],
    )


@app.route("/admin/clamav/selftest", methods=["POST"])
@requires_admin
def admin_clamav_selftest():
    result = _run_clamav_selftest()
    _record_audit(
        "ウイルス対策セルフテスト",
        f"ok={result['ok']} detected={result['detected']} error={result['error']}",
    )
    if result["ok"] and result["detected"]:
        return _finish("admin_clamav", "EICARテスト検体を正しく検知しました。ウイルス対策は正常に機能しています。")
    if result["ok"] and not result["detected"]:
        return _finish(
            "admin_clamav",
            "ClamAVは応答していますが、既知のテスト検体(EICAR)を検知できませんでした。"
            "シグネチャDBや設定を確認してください。",
            success=False,
        )
    return _finish(
        "admin_clamav", f"スキャンを実行できませんでした: {result['error']}", success=False,
    )


@app.route("/admin/clamav/rescan", methods=["POST"])
@requires_admin
def admin_clamav_rescan():
    if _is_script_running("clamav_rescan"):
        return _finish("admin_clamav", "再スキャンは既に実行中です。", success=False)
    _run_clamav_rescan_async()
    _record_audit("ウイルス対策の一括再スキャンを開始", f"対象: {', '.join(str(d) for d in CLAMAV_RESCAN_DIRS)}")
    return _finish("admin_clamav", "再スキャンを開始しました。件数によっては数分かかります。")


@app.route("/members/<int:user_id>")
def member_detail(user_id):
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        history = users.login_history(conn, user_id)
        first_login_at = users.first_login(conn, user_id)
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
    night_owl = any(len(h["logged_in_at"]) >= 13 and int(h["logged_in_at"][11:13]) < 4 for h in history)
    early_bird = any(len(h["logged_in_at"]) >= 13 and 5 <= int(h["logged_in_at"][11:13]) < 7 for h in history)
    newyear_login = any(len(h["logged_in_at"]) >= 10 and h["logged_in_at"][5:10] == "01-01" for h in history)
    newyearseve_login = any(len(h["logged_in_at"]) >= 10 and h["logged_in_at"][5:10] == "12-31" for h in history)
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
    if _current_user_settings()["mute_profile_sounds"]:
        profile_sound = ""  # 設定画面の「ほかの人のプロフィールの効果音を鳴らさない」
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
        station_region=STATION_REGION,
        first_login=first_login_at,
        last_login=history[0]["logged_in_at"] if history else None,
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
    """ポイント・ショップ・川霧予測ゲームをまとめたゲームのトップページ。"""
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
    川霧予測ゲーム用消費アイテムと交換する画面。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    conn = _users_conn()
    try:
        member = users.get_user_by_id(conn, my_id)
        points = users.available_points(conn, my_id)
        gems = users.available_gems(conn, my_id)
        owned_gem_items = users.owned_gem_item_ids(conn, my_id)
        owned = users.owned_frame_ids(conn, my_id)
        # 各カテゴリのジェム限定アイテム(gem_shop_definitions等とは別に、各カテゴリの
        # 定義リストへcurrency:"gems"付きで直接追加してある)はuser_cosmeticsではなく
        # user_gem_itemsに所持記録があるため、owned_gem_items(IDはカタログ全体で一意)を
        # そのまま和集合して渡す。これでcosmetic_itemマクロのowned判定がそのまま使える。
        owned_titles = users.owned_cosmetic_ids(conn, my_id, "title") | owned_gem_items
        owned_name_colors = users.owned_cosmetic_ids(conn, my_id, "name_color") | owned_gem_items
        owned_dashboard_themes = users.owned_cosmetic_ids(conn, my_id, "dashboard_theme") | owned_gem_items
        owned_effects = users.owned_cosmetic_ids(conn, my_id, "effect") | owned_gem_items
        owned_banners = users.owned_cosmetic_ids(conn, my_id, "banner") | owned_gem_items
        owned_dm_themes = users.owned_cosmetic_ids(conn, my_id, "dm_theme") | owned_gem_items
        owned_title_colors = users.owned_cosmetic_ids(conn, my_id, "title_color") | owned_gem_items
        owned_markers = users.owned_cosmetic_ids(conn, my_id, "marker")
        owned_title_badges = users.owned_cosmetic_ids(conn, my_id, "title_badge") | owned_gem_items
        owned_bubble_styles = users.owned_cosmetic_ids(conn, my_id, "bubble_style") | owned_gem_items
        owned_avatar_bgs = users.owned_cosmetic_ids(conn, my_id, "avatar_bg") | owned_gem_items
        owned_rank_styles = users.owned_cosmetic_ids(conn, my_id, "rank_style") | owned_gem_items
        owned_profile_sounds = users.owned_cosmetic_ids(conn, my_id, "profile_sound") | owned_gem_items
        owned_seasonal = users.owned_cosmetic_ids(conn, my_id, "seasonal") | owned_gem_items
        consumables = users.consumable_quantities(conn, my_id)

        # 装着中のアイテム一覧と、今月の使用料合計(装着中のみ課金されるため)。
        equipped_items = []
        if member["equipped_frame"]:
            frame_def = FRAME_BY_ID.get(member["equipped_frame"])
            if frame_def:
                info = users.frame_billing_info(conn, my_id, member["equipped_frame"])
                equipped_items.append({
                    "category": "frame", "category_label": "フレーム", "label": frame_def["label"],
                    "monthly_fee": frame_def["monthly_fee"],
                    "next_billing_at": info["next_billing_at"] if info else None,
                })
        for category, (catalog, cosmetic_category, cat_label) in COSMETIC_CATALOGS.items():
            item_id = member.get(f"equipped_{category}")
            if not item_id:
                continue
            item_def = catalog.get(item_id)
            if not item_def or item_def.get("currency") == "gems":
                continue  # ジェム品は買い切りで月額請求が無いため、この一覧には出さない
            info = users.cosmetic_billing_info(conn, my_id, cosmetic_category, item_id)
            equipped_items.append({
                "category": category, "category_label": cat_label, "label": item_def["label"],
                "monthly_fee": item_def["monthly_fee"],
                "next_billing_at": info["next_billing_at"] if info else None,
            })
        total_monthly_fee = sum(i["monthly_fee"] for i in equipped_items)
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
        gems=gems,
        gem_shop_definitions=GEM_SHOP_DEFINITIONS,
        owned_gem_items=owned_gem_items,
        equipped_items=equipped_items,
        total_monthly_fee=total_monthly_fee,
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


def _check_slot_change_allowed(current_item_id, current_billing_info, target_item_id):
    """月額使用料アイテムは「装着中のものの次回請求日が来た(=月初の更新タイミング)時だけ
    選び直せて、それ以外は固定」という仕様にするための事前チェック。次回請求日より前に
    外してしまうと請求されずに済んでしまう抜け道を塞ぐため、変更(別アイテムへの
    切り替え・解除の両方を含む)は次回請求日到達後にしか許可しない。target_item_idが
    現在と同じ(実質変更なし)場合は常に許可する。
    戻り値: 変更してよければNone、拒否するなら理由メッセージ。"""
    if not current_item_id or current_item_id == target_item_id:
        return None
    next_billing_at = current_billing_info.get("next_billing_at") if current_billing_info else None
    if not next_billing_at:
        return None
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    if next_billing_at > now_str:
        return f"次回の更新日({next_billing_at[:10]})まで、装着中のアイテムは変更(解除も含む)できません。"
    return None


def _next_billing_first_of_month(base_dt=None):
    """次回請求日として「翌月1日 00:00:00 JST」の文字列を返す。
    base_dt が None の場合は現在時刻(JST)を使う。
    月額使用料の請求タイミングを月初に統一するために使う。"""
    if base_dt is None:
        base_dt = datetime.now(JST)
    if base_dt.month == 12:
        first = base_dt.replace(year=base_dt.year + 1, month=1, day=1,
                                hour=0, minute=0, second=0, microsecond=0)
    else:
        first = base_dt.replace(month=base_dt.month + 1, day=1,
                                hour=0, minute=0, second=0, microsecond=0)
    return first.strftime("%Y-%m-%d %H:%M:%S")


def _charge_billing_if_due(conn, user_id, kind, item_id, next_billing_at, fee, advance_billing_fn):
    """装備しようとしているアイテムの月額使用料の次回請求日が過ぎていれば、装着を許可する
    前にその場で1ヶ月分を請求する(装備中のものだけを課金する使用料モデルのため、装着操作
    そのものが「今月分を払って使い始める」タイミングになる)。feeは呼び出し側がカタログ
    (FRAME_BY_ID/COSMETIC_CATALOGS)から引いた現在のmonthly_feeを渡す(user_frames/
    user_cosmetics.costは取得時点の記録用でしかなく、福袋等でcost=0のまま所持している
    場合もあるため、使用料は必ずカタログの現在価格から計算する)。advance_billing_fnは
    請求成功時に次回請求日を1ヶ月先へ更新する関数(新しいnext_billing_at文字列を1つ受け取る)。
    戻り値: (装着してよいか, 失敗時にユーザーへ見せるメッセージ)。"""
    if not next_billing_at:
        return True, ""
    now = datetime.now(JST)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    if next_billing_at > now_str:
        return True, ""
    balance = users.available_points(conn, user_id)
    if balance < fee:
        return False, f"月額使用料{fee}ptが不足しているため装着できません(現在の残高: {balance}pt)。"
    source = f"subscription_fee:{kind}:{item_id}:{next_billing_at}"
    charged = users.grant_points(conn, user_id, source, -fee, now_str)
    if charged:
        next_billing_str = _next_billing_first_of_month(now)
        advance_billing_fn(next_billing_str)
    return True, ""


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
        now = datetime.now(JST)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        next_billing_str = _next_billing_first_of_month(now)
        ok = users.spend_points_if_available(
            conn, my_id, frame["cost"],
            lambda c: users.purchase_frame(c, my_id, frame["id"], frame["cost"], now_str, next_billing_str),
        )
        if not ok:
            return _finish("game_shop", "ポイントが足りません。", success=False)
    finally:
        conn.close()
    return _finish("game_shop", f"「{frame['label']}」と交換しました(初月分は購入価格に含まれています。翌月以降は毎月{_monthly_fee(frame['cost'])}ptの使用料がかかります)。")


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
        current_frame_id = users.get_user_by_id(conn, my_id)["equipped_frame"]
        current_info = users.frame_billing_info(conn, my_id, current_frame_id) if current_frame_id else None
        lock_message = _check_slot_change_allowed(current_frame_id, current_info, frame_id)
        if lock_message:
            return _finish("game_shop", lock_message, success=False)
        if frame_id:
            billing_info = users.frame_billing_info(conn, my_id, frame_id)
            frame_def = FRAME_BY_ID.get(frame_id)
            billed_ok, fee_message = _charge_billing_if_due(
                conn, my_id, "frame", frame_id,
                billing_info["next_billing_at"] if billing_info else None,
                frame_def["monthly_fee"] if frame_def else 0,
                lambda next_at: users.set_frame_next_billing(conn, my_id, frame_id, next_at),
            )
            if not billed_ok:
                return _finish("game_shop", fee_message, success=False)
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
    is_gem = item.get("currency") == "gems"
    conn = _users_conn()
    try:
        already_owned = (
            item["id"] in users.owned_gem_item_ids(conn, my_id) if is_gem
            else item["id"] in users.owned_cosmetic_ids(conn, my_id, cosmetic_category)
        )
        if already_owned:
            return _finish("game_shop", f"その{label}は既に持っています。", success=False)
        now = datetime.now(JST)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        if is_gem:
            ok = users.spend_gems_if_available(
                conn, my_id, item["cost_gems"],
                lambda c: users.purchase_gem_item(c, my_id, item["id"], item["cost_gems"], now_str),
            )
            if not ok:
                return _finish("game_shop", "ジェムが足りません。", success=False)
            gems = users.available_gems(conn, my_id)
        else:
            next_billing_str = _next_billing_first_of_month(now)
            ok = users.spend_points_if_available(
                conn, my_id, item["cost"],
                lambda c: users.purchase_cosmetic(
                    c, my_id, cosmetic_category, item["id"], item["cost"], now_str, next_billing_str
                ),
            )
            if not ok:
                return _finish("game_shop", "ポイントが足りません。", success=False)
            points = users.available_points(conn, my_id)
    finally:
        conn.close()
    if is_gem:
        return _finish(
            "game_shop", f"「{item['label']}」と交換しました。",
            extra={"action": "buy", "category": category, "item_id": item["id"], "available_gems": gems},
        )
    return _finish(
        "game_shop",
        f"「{item['label']}」と交換しました(初月分は購入価格に含まれています。"
        f"翌月以降は毎月{_monthly_fee(item['cost'])}ptの使用料がかかります)。",
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
    target_def = catalog.get(item_id) if item_id else None
    target_is_gem = bool(target_def) and target_def.get("currency") == "gems"
    conn = _users_conn()
    try:
        if item_id:
            owned = (
                item_id in users.owned_gem_item_ids(conn, my_id) if target_is_gem
                else item_id in users.owned_cosmetic_ids(conn, my_id, cosmetic_category)
            )
            if not owned:
                abort(400)
        current_item_id = users.get_user_by_id(conn, my_id).get(f"equipped_{category}")
        # 現在装着中のものがジェム品(買い切り)の場合はcosmetic_billing_infoが必ずNoneを
        # 返す(user_cosmeticsに存在しないため)ので、_check_slot_change_allowedは自動的に
        # 「ロックなし」と判定する(ジェム品には月額サイクルという概念自体が無いため)。
        current_info = (
            users.cosmetic_billing_info(conn, my_id, cosmetic_category, current_item_id)
            if current_item_id else None
        )
        lock_message = _check_slot_change_allowed(current_item_id, current_info, item_id)
        if lock_message:
            return _finish("game_shop", lock_message, success=False)
        if item_id and not target_is_gem:
            billing_info = users.cosmetic_billing_info(conn, my_id, cosmetic_category, item_id)
            billed_ok, fee_message = _charge_billing_if_due(
                conn, my_id, "cosmetic", f"{cosmetic_category}:{item_id}",
                billing_info["next_billing_at"] if billing_info else None,
                target_def["monthly_fee"] if target_def else 0,
                lambda next_at: users.set_cosmetic_next_billing(conn, my_id, cosmetic_category, item_id, next_at),
            )
            if not billed_ok:
                return _finish("game_shop", fee_message, success=False)
        users.set_equipped_cosmetic(conn, my_id, cosmetic_category, item_id)
    finally:
        conn.close()
    message = f"{label}を装着しました。" if item_id else f"{label}を外しました。"
    return _finish(
        "game_shop", message,
        extra={"action": "equip" if item_id else "unequip", "category": category, "item_id": item_id},
    )


@app.route("/game/shop/gems/buy", methods=["POST"])
def game_shop_buy_gem_item():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    item = GEM_SHOP_BY_ID.get(request.form.get("item_id", ""))
    if item is None:
        abort(404)
    conn = _users_conn()
    try:
        if item["id"] in users.owned_gem_item_ids(conn, my_id):
            return _finish("game_shop", f"「{item['label']}」は既に持っています。", success=False)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        ok = users.spend_gems_if_available(
            conn, my_id, item["cost_gems"],
            lambda c: users.purchase_gem_item(c, my_id, item["id"], item["cost_gems"], now_str),
        )
        if not ok:
            return _finish("game_shop", "ジェムが足りません。", success=False)
        gems = users.available_gems(conn, my_id)
    finally:
        conn.close()
    return _finish(
        "game_shop", f"「{item['label']}」と交換しました。",
        extra={"action": "buy_gem_item", "item_id": item["id"], "available_gems": gems},
    )


@app.route("/game/shop/gems/equip", methods=["POST"])
def game_shop_equip_gem_item():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    item_id = request.form.get("item_id", "").strip()
    conn = _users_conn()
    try:
        if item_id and item_id not in users.owned_gem_item_ids(conn, my_id):
            abort(400)
        users.set_equipped_gem_item(conn, my_id, item_id)
    finally:
        conn.close()
    message = "装着しました。" if item_id else "外しました。"
    return _finish("game_shop", message)


def _gacha_is_jackpot(weighted):
    """福袋の当たり判定。確率は常に1/GACHA_BIG_REFUND_WEIGHTに固定する(アイテム側の
    重みをそのまま分母に混ぜると、アイテムの価格帯によって当たり確率が事実上100%近くまで
    ぶれてしまうバグが過去にあったため、確率計算とアイテム抽選の重み計算を分離してある)。
    test_economy_sanity.pyがモンテカルロで確率を検証するため、判定だけを取り出した
    独立関数にしている(この関数の中身を変えるならそのテストも見直すこと)。"""
    return not weighted or random.random() < (1.0 / GACHA_BIG_REFUND_WEIGHT)


SUBSCRIPTION_BILLING_CHECK_INTERVAL_SECONDS = 1800
_subscription_billing_check_state = {"at": 0.0}
_subscription_billing_check_lock = threading.Lock()


def _charge_due_subscriptions():
    """装備したまま放置されているフレーム・コスメについて、月額使用料の支払期限
    (next_billing_at)が来ているものを一括で請求する。_charge_billing_if_dueが装着操作
    そのもののタイミングで課金するのに対し、こちらは既に装着済みのまま何もしなくても
    毎月ちゃんと引き落とされるようにするための見回り役(fog_bets清算・経済異常検知と同じく
    専用cronは持たず、よく踏まれるページから間引いて呼ぶ)。残高が足りなければ所持は
    残したまま自動で未装着に戻す(装備解除のみ。再装着時に_charge_billing_if_dueが
    改めて請求を試みる)。"""
    now = time.monotonic()
    with _subscription_billing_check_lock:
        if now - _subscription_billing_check_state["at"] < SUBSCRIPTION_BILLING_CHECK_INTERVAL_SECONDS:
            return
        _subscription_billing_check_state["at"] = now
    conn = _users_conn()
    try:
        wall_now = datetime.now(JST)
        now_str = wall_now.strftime("%Y-%m-%d %H:%M:%S")
        next_billing_str = _next_billing_first_of_month(wall_now)

        for user_id, frame_id, _cost, next_billing_at in users.list_equipped_frames_due(conn, now_str):
            frame_def = FRAME_BY_ID.get(frame_id)
            fee = frame_def["monthly_fee"] if frame_def else 0
            if users.available_points(conn, user_id) >= fee:
                source = f"subscription_fee:frame:{frame_id}:{next_billing_at}"
                if users.grant_points(conn, user_id, source, -fee, now_str):
                    users.set_frame_next_billing(conn, user_id, frame_id, next_billing_str)
            else:
                users.set_equipped_frame(conn, user_id, "")

        for user_id, category, item_id, _cost, next_billing_at in users.list_equipped_cosmetics_due(conn, now_str):
            entry = COSMETIC_CATALOGS.get(category)
            item_def = entry[0].get(item_id) if entry else None
            fee = item_def["monthly_fee"] if item_def else 0
            if users.available_points(conn, user_id) >= fee:
                source = f"subscription_fee:cosmetic:{category}:{item_id}:{next_billing_at}"
                if users.grant_points(conn, user_id, source, -fee, now_str):
                    users.set_cosmetic_next_billing(conn, user_id, category, item_id, next_billing_str)
            else:
                users.set_equipped_cosmetic(conn, user_id, category, "")
    finally:
        conn.close()


ECONOMY_ANOMALY_CHECK_INTERVAL_SECONDS = 1800
ECONOMY_ANOMALY_WINDOW_MINUTES = 60
ECONOMY_ANOMALY_POINT_THRESHOLD = 50000
# 重要度「高」の観測報告は承認なしで即座に40ジェムが確定するため、短時間に大量の
# 「高」報告を連発するファーミングを検知する(1時間に10件超のペース)。
ECONOMY_ANOMALY_GEM_THRESHOLD = 400
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
        gem_rows = conn.execute(
            "SELECT user_id, SUM(amount) FROM gem_events WHERE created_at >= ? AND amount > 0 "
            "GROUP BY user_id HAVING SUM(amount) >= ?",
            (cutoff, ECONOMY_ANOMALY_GEM_THRESHOLD),
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
    for user_id, total in gem_rows:
        message = (
            f"user_id={user_id}が直近{ECONOMY_ANOMALY_WINDOW_MINUTES}分で{total}ジェム獲得しています"
            f"(閾値{ECONOMY_ANOMALY_GEM_THRESHOLD}ジェム)。重要度「高」の観測報告乱発による"
            f"ファーミングの疑いがあるため確認してください。"
        )
        _record_audit("経済異常検知(ジェム)", message)
        _send_alert_throttled(f"economy_anomaly_gem_user_{user_id}", message)


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
        now_dt = datetime.now(JST)
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        next_billing_str = _next_billing_first_of_month(now_dt)
        candidates = []
        for category, (catalog, cosmetic_category, label) in COSMETIC_CATALOGS.items():
            owned = users.owned_cosmetic_ids(conn, my_id, cosmetic_category)
            today_str = now_str[:10]
            for item in catalog.values():
                if item.get("currency") == "gems":
                    continue  # ジェム限定品は福袋(ポイント抽選)の対象外
                if item["id"] in owned:
                    continue
                if item.get("available_from") and today_str < item["available_from"]:
                    continue
                if item.get("available_until") and today_str > item["available_until"]:
                    continue
                candidates.append((category, cosmetic_category, label, item))
        weighted = [(c, cc, lb, it, 1.0 / max(it["cost"], 1)) for c, cc, lb, it in candidates]
        is_jackpot = _gacha_is_jackpot(weighted)
        if not is_jackpot:
            roll = random.random() * sum(w for *_, w in weighted)
            chosen = weighted[-1]
            for entry in weighted:
                roll -= entry[4]
                if roll <= 0:
                    chosen = entry
                    break

        result = {}

        def _spend(c):
            # 参加費(GACHA_COST)はconsumable_purchases経由で記録し(total_spent_pointsの
            # 集計対象)、当たり分だけを別途grant_pointsで加算する(二重計上にならない)。
            users.buy_consumable(c, my_id, "gacha_draw", GACHA_COST, now_str)
            if is_jackpot:
                if weighted:
                    refund = int(round(GACHA_COST * GACHA_BIG_REFUND_MULTIPLIER))
                    users.grant_points(c, my_id, f"gacha_refund:{uuid.uuid4()}", refund, now_str)
                    result["message"] = f"福袋の結果: {refund}pt の大当たり!"
                else:
                    users.grant_points(c, my_id, f"gacha_consolation:{uuid.uuid4()}", GACHA_CONSOLATION_POINTS, now_str)
                    result["message"] = f"すべて所持済みだったため、参加賞として{GACHA_CONSOLATION_POINTS}ptを獲得しました。"
            else:
                _, cosmetic_category, label, item, _ = chosen
                users.purchase_cosmetic(c, my_id, cosmetic_category, item["id"], 0, now_str, next_billing_str)
                result["message"] = f"福袋の結果: 「{item['label']}」({label})を獲得しました!"

        ok = users.spend_points_if_available(conn, my_id, GACHA_COST, _spend)
        if not ok:
            return _finish("game_shop", "ポイントが足りません。", success=False)
        points = users.available_points(conn, my_id)
    finally:
        conn.close()
    return _finish("game_shop", result["message"], extra={"available_points": points})


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
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        ok = users.spend_points_if_available(
            conn, my_id, item["cost"],
            lambda c: users.buy_consumable(c, my_id, item["id"], item["cost"], now_str),
        )
        if not ok:
            return _finish("game_shop", "ポイントが足りません。", success=False)
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
    """川霧予測ゲーム: 地点・日付・時間精度を選び、倍率のプレビューを見ながら賭ける画面。"""
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
        owned = users.consumable_quantities(conn, my_id)
        if use_insurance and owned.get("insurance_ticket", 0) < 1:
            return _finish("game_gamble", "保険チケットを持っていません。", success=False)
        if use_super_boost and owned.get("super_boost_ticket", 0) < 1:
            return _finish("game_gamble", "上級倍率ブーストチケットを持っていません。", success=False)
        # 下のuse_boost実消費(_spend)は上級版が指定されていれば通常版を無視する(効果を
        # 重複させないため)。ここの事前チェックもそれに合わせないと、上級版のみ所持する
        # 利用者が両方のフラグを立てて送信した場合に、実際には使わない通常版の在庫不足で
        # 誤って拒否されてしまう(2026-09-13の監査で指摘)。
        if use_boost and not use_super_boost and owned.get("boost_ticket", 0) < 1:
            return _finish("game_gamble", "倍率ブーストチケットを持っていません。", success=False)

        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        applied_multiplier = {"value": multiplier}

        def _spend(c):
            # 上級版と通常版が同時に指定された場合は上級版だけを使う(効果は重複させない)。
            # 在庫チェックは上で済ませているが、直後に別リクエストが使い切る競合が
            # ありうるため、use_consumableの戻り値も必ず確認する(確認しないと、
            # チケットを消費できなかったのに倍率だけ適用されてしまう)。
            m = applied_multiplier["value"]
            if use_super_boost:
                if not users.use_consumable(c, my_id, "super_boost_ticket"):
                    raise _ConsumableUnavailable()
                m = round(m * CONSUMABLE_SUPER_BOOST_MULTIPLIER, 2)
            elif use_boost:
                if not users.use_consumable(c, my_id, "boost_ticket"):
                    raise _ConsumableUnavailable()
                m = round(m * CONSUMABLE_BOOST_MULTIPLIER, 2)
            if use_insurance:
                if not users.use_consumable(c, my_id, "insurance_ticket"):
                    raise _ConsumableUnavailable()
            applied_multiplier["value"] = m
            users.place_fog_bet(
                c, my_id, station, precision_id, window_start, window_end, bet_amount, m, now_str,
                insurance=use_insurance,
            )

        try:
            ok = users.spend_points_if_available(conn, my_id, bet_amount, _spend)
        except _ConsumableUnavailable:
            return _finish(
                "game_gamble", "チケットの在庫が変わったため賭けられませんでした。もう一度お試しください。",
                success=False,
            )
        if not ok:
            return _finish("game_gamble", "ポイントが足りません。", success=False)
        multiplier = applied_multiplier["value"]
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
    """無料のデイリーゲーム(川霧予報クイズ・現象当てクイズ・くじ)をまとめたページ。
    川霧予測ゲームと違いポイントを賭けないため、ポイントが無い新規ユーザーでも遊べる。"""
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
        push_ids = [uid for uid in users.push_subscriber_ids(conn) if uid != my_id]
        push_settings_raw = users.all_user_settings_raw(conn) if push_ids else {}
    finally:
        conn.close()
    try:
        my_name = session.get("name", "")
        for uid in push_ids:
            mentioned = uid in mention_ids
            push_notify.notify_async(
                [uid], "mention" if mentioned else "group",
                f"{my_name}さんがグループであなたをメンションしました" if mentioned else f"グループ: {my_name}さん",
                push_notify.message_preview(push_settings_raw.get(uid, "{}"), body, bool(attachment_info)),
                url_for("group_home"), "group", ttl=86400, urgency="high" if mentioned else "normal",
            )
    except Exception as e:
        log.warning("Failed to queue group push notifications: %s", e)
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
        recipient_settings_raw = users.get_user_settings_raw(conn, user_id)
    finally:
        conn.close()
    try:
        push_notify.notify_async(
            [user_id], "dm", f"{session.get('name', '')}さんからのメッセージ",
            push_notify.message_preview(recipient_settings_raw, body, bool(attachment_info)),
            url_for("dm_thread", user_id=my_id), f"dm-{my_id}", ttl=86400, urgency="high",
        )
    except Exception as e:
        log.warning("Failed to queue the DM push notification: %s", e)
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

# /files/<id>/raw はブラウザにその場で描画させる生配信のため、content_typeが
# text/html等になりうる場合はここで弾かないと格納型XSSが成立してしまう。
# アップロード自体には拡張子制限を設けていない(利用者の要望で撤廃済み)ため、
# 「その場で描画してよい」ものだけをここで安全側に絞り、それ以外は
# ダウンロード(添付ヘッダー付き)にフォールバックする。プレビュー画面が
# <img>/<embed>で読み込むのは画像とPDFのみ(shared_file_preview.html参照)。
# その場表示(添付ヘッダーなし)を許す種類。以前は"image/"で始まれば何でも通していたため、
# SVG(image/svg+xml、中にスクリプトを書ける)をアップロードして/files/<id>/rawを開かせると
# このサイトの権限でスクリプトが動いてしまった(2026-10-08の監査で指摘)。
SAFE_INLINE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf"}


def _is_safe_inline_content_type(content_type):
    return (content_type or "").split(";")[0].strip().lower() in SAFE_INLINE_CONTENT_TYPES


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
        # 空チェックと削除はusers.delete_shared_folder内で同じトランザクションとして行う
        # (以前はここで別々に呼んでおり、チェックと削除の間に別リクエストがファイルを
        # 追加するレースで未処理のIntegrityErrorになりうった、2026-09-13の監査で指摘)。
        if not users.delete_shared_folder(conn, folder_id):
            return _finish(
                "shared_files_page", "空でないフォルダは削除できません。",
                success=False, folder=folder["parent_id"],
            )
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
    if not _is_safe_inline_content_type(f["content_type"]):
        # 画像・PDF以外をその場描画させるとcontent_type偽装によるXSSが成立するため、
        # 添付ダウンロードにフォールバックする(アップロード自体は拒否しない)。
        return send_from_directory(
            SHARED_FILES_DIR, f["stored_name"], as_attachment=True, download_name=f["filename"],
        )
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


@app.route("/admin/points")
@requires_admin
def admin_points():
    conn = _users_conn()
    try:
        accounts = users.list_users(conn)
        balances = users.list_all_user_point_balances(conn)
        gem_balances = users.list_all_user_gem_balances(conn)
    finally:
        conn.close()
    return render_template(
        "admin_points.html", accounts=accounts, balances=balances, gem_balances=gem_balances,
    )


@app.route("/admin/points/<int:user_id>/adjust", methods=["POST"])
@requires_admin
def admin_points_adjust(user_id):
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        raw = (request.form.get("amount") or "").strip()
        try:
            amount = int(raw)
        except ValueError:
            return _finish("admin_points", "ポイント数は整数で入力してください。", success=False)
        if amount == 0:
            return _finish("admin_points", "0ポイントは指定できません。", success=False)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        source = f"admin_adjust:{session.get('user_id')}:{now_str}:{secrets.token_hex(4)}"
        users.grant_points(conn, user_id, source, amount, now_str)
        balance = users.available_points(conn, user_id)
    finally:
        conn.close()
    _record_audit(
        "ポイント手動調整",
        f"{target['name']}({target['email']})に{amount:+d}pt付与(調整後残高: {balance}pt)",
    )
    return _finish(
        "admin_points", f"{target['name']}さんに{amount:+d}ptを反映しました(残高: {balance}pt)。",
        extra={"user_id": user_id, "balance": balance},
    )


@app.route("/admin/points/<int:user_id>/adjust_gems", methods=["POST"])
@requires_admin
def admin_points_adjust_gems(user_id):
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        raw = (request.form.get("amount") or "").strip()
        try:
            amount = int(raw)
        except ValueError:
            return _finish("admin_points", "ジェム数は整数で入力してください。", success=False)
        if amount == 0:
            return _finish("admin_points", "0ジェムは指定できません。", success=False)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        source = f"admin_adjust_gems:{session.get('user_id')}:{now_str}:{secrets.token_hex(4)}"
        users.grant_gems(conn, user_id, source, amount, now_str)
        balance = users.available_gems(conn, user_id)
    finally:
        conn.close()
    _record_audit(
        "ジェム手動調整",
        f"{target['name']}({target['email']})に{amount:+d}ジェム付与(調整後残高: {balance}ジェム)",
    )
    return _finish(
        "admin_points", f"{target['name']}さんに{amount:+d}ジェムを反映しました(残高: {balance}ジェム)。",
        extra={"user_id": user_id, "gem_balance": balance},
    )


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
    (admin→general)は安全側の変更なので従来どおり即時反映する。観測者/会津観測者/
    只見観測者は観測報告の確認・訂正権限に限られ、他の管理機能には一切関与しないため、
    管理者昇格ほどの慎重さは不要と判断し、即時反映にしている。"""
    role = request.form.get("role")
    if role not in ("general", "admin", "observer", *OBSERVER_ROLE_REGIONS):
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
    # chat.dbはusers.dbと別ファイルのためON DELETE CASCADEが効かない。ここで明示的に
    # 削除しないとAIチャット履歴が孤立したまま残り続ける(2026-09-13の監査で指摘)。
    chat_conn = chat_log.connect(CHAT_DB_PATH)
    try:
        chat_log.delete_user_data(chat_conn, user_id)
    finally:
        chat_conn.close()
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


@app.route("/admin/auth-check")
def admin_auth_check():
    """nginxのauth_request用。/admin/terminal-ws/(ttyd)と/cockpit-ui/(Cockpit)は
    Flaskを通らずnginxから直接プロキシされるため、以前はtailnet内からなら誰でも
    ttyd/CockpitのログインやBasic認証の総当たりを試せた(2026-10-08の監査で指摘)。
    nginxがこのURLに副リクエストを送り、管理者としてログイン中(requires_adminと同じ条件)
    の時だけ204を返して通す。リダイレクトはauth_requestでは使えないため401を返す。"""
    if session.get("user_id") and session.get("role") == "admin" and not _is_funnel_request():
        return "", 204
    return "", 401


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


@app.route("/admin/tailscale")
@requires_admin
def admin_tailscale():
    """Tailscaleの接続状態を閲覧専用で表示する。状態を変更する操作(up/down/logout等)は
    一切用意しない: このサーバーへのSSH・管理画面アクセスの経路そのものがTailscale
    経由であり、誤操作で無効化すると物理アクセスかLAN側からの復旧作業が必要になる
    (admin_smb.htmlと同じ「sudoersを増やすリスクとの見合い」の考え方だが、
    ここは影響がリモートアクセス経路そのものに及ぶためより厳格に閲覧専用とした)。
    """
    status = _tailscale_status()
    self_info = (status or {}).get("Self") or {}
    peers = sorted(
        (status or {}).get("Peer", {}).values(),
        key=lambda p: (not p.get("Online"), (p.get("HostName") or "").lower()),
    )
    return render_template(
        "admin_tailscale.html",
        backend_state=(status or {}).get("BackendState"),
        self_info=self_info, peers=peers,
        version=_tailscale_version(), autoupdate=_tailscale_autoupdate_prefs(),
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
    """モデルの精度・F1・混同行列・特徴量重要度・地点別状況は、2026-09-12に
    /accuracy(分析ページ、管理者専用ではない)へ統合した。ここにはpredict.logの
    ロールバック履歴(モデルの自動ロールバックという運用上の出来事のログ)のみ残す。"""
    rollback_lines = []
    predict_log_path = ALLOWED_LOGS.get("predict")
    if predict_log_path and predict_log_path.exists():
        try:
            with open(predict_log_path, encoding="utf-8", errors="replace") as f:
                rollback_lines = [ln.rstrip("\n") for ln in f if "Rolled back to previous model" in ln]
        except Exception as e:
            log.warning("Failed to scan predict.log for rollbacks: %s", e)

    return render_template("admin_models.html", rollback_lines=rollback_lines[-50:])


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
    ttydはループバック限定で待ち受けており、nginxがTTYD_BASE_PATHを同一オリジンで
    プロキシする(TTYD_BASE_PATHの定義部のコメント参照)。このページ自体がrequires_admin
    配下なので、iframeのURLを受け取れるのは既に管理者と確認済みの閲覧者だけ。
    認証自体はttyd側のBasic認証が別途行う(最後の防御層として残している)。"""
    return render_template("admin_terminal.html", terminal_url=TTYD_BASE_PATH)


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
    """指定期間の川霧発生状況・気象概況・予測精度をまとめたPDFレポートを生成し、bytesで返す。
    matplotlibのPdfPages機能を使う(新規ライブラリの追加は不要)。
    """
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.ticker import MaxNLocator

    main_df, phenom_df, station_names = fog_data.load_river_fog_data()
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())
    mask = (phenom_df["datetime"] >= start_dt) & (phenom_df["datetime"] <= end_dt)
    period_df = phenom_df[mask]
    period_weather_df = main_df[(main_df["datetime"] >= start_dt) & (main_df["datetime"] <= end_dt)]

    # 地点ごとのコード系列(0=現象なし、1〜10=PHENOM_LABELS、2コード同時観測=2026-09対応は
    # パック済み値)をまとめて計算しておき、川霧発生回数の集計・現象内訳の集計の両方で使い回す。
    codes_by_station = {}
    for name in station_names:
        if name in period_df.columns:
            codes_by_station[name] = period_df[name].map(encode_phenomena_cell)

    # 川霧発生回数は主コードだけで判定できる(家族グループの並び上、2コード同時でも
    # 川霧は必ず主コード側に入る)。
    fog_counts = {
        name: int(codes.apply(primary_phenom_code).isin(SUMMARY_FOG_CODES).sum())
        for name, codes in codes_by_station.items()
    }
    for name in station_names:
        fog_counts.setdefault(name, 0)
    total_slots = len(period_df)

    tadami_stations = [n for n in station_names if STATION_REGION.get(n) == "只見川"]
    aizu_stations = [n for n in station_names if STATION_REGION.get(n) == "会津"]

    # 現象内訳は主・副どちらのコードもそれぞれ1件としてカウントする(2コード同時観測の
    # 場合、value_counts()だとパック済み値が独立した1つの値として扱われ、内訳の
    # どちらのコードにも計上されなくなってしまうため、phenom_codes_setで展開してから数える)。
    phenom_code_totals = {code: 0 for code in PHENOM_LABELS}
    for codes in codes_by_station.values():
        for value in codes:
            for code in phenom_codes_set(value):
                if code in phenom_code_totals:
                    phenom_code_totals[code] += 1

    accuracy = _accuracy_summary_safe()
    trend = _accuracy_trend_safe()
    trend_in_range = [r for r in trend if start_date.isoformat() <= r["date"] <= end_date.isoformat() and r["n"] > 0]
    # レポートのタイトルには「その期間の」的中率を出したいため、全期間集計のoverallではなく
    # 期間内の日別集計(trend_in_range)を合算する(以前はaccuracy["overall"]=全期間累計の値を
    # 誤って「この期間の」として表示してしまっていた)。
    period_correct = sum(r["correct"] for r in trend_in_range)
    period_n = sum(r["n"] for r in trend_in_range)

    def _weather_avg(col):
        if col not in period_weather_df.columns:
            return None
        series = period_weather_df[col].dropna()
        return float(series.mean()) if len(series) else None

    def _weather_sum(col):
        if col not in period_weather_df.columns:
            return None
        series = period_weather_df[col].dropna()
        return float(series.sum()) if len(series) else None

    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        fig = plt.figure(figsize=(8.27, 11.69))
        fig.text(0.5, 0.62, "川霧観測・予測レポート", ha="center", fontsize=24, fontweight="bold")
        fig.text(0.5, 0.55, f"{start_date} 〜 {end_date}", ha="center", fontsize=14)
        if period_n:
            fig.text(
                0.5, 0.48,
                f"この期間の予測的中率: {100.0 * period_correct / period_n:.1f}% "
                f"({period_correct}/{period_n}件)",
                ha="center", fontsize=11,
            )
        fig.text(0.5, 0.08, f"生成日時: {now_jst_naive().strftime('%Y-%m-%d %H:%M')}", ha="center", fontsize=9, color="gray")
        pdf.savefig(fig)
        plt.close(fig)

        # 地域別サマリー(観測コマ数・川霧発生回数・発生率を只見川/会津それぞれで比較できるように)。
        fig = plt.figure(figsize=(8.27, 6))
        fig.text(0.5, 0.92, "地域別サマリー", ha="center", fontsize=16, fontweight="bold")
        y = 0.75
        for label, names in (("只見川", tadami_stations), ("会津", aizu_stations)):
            region_slots = total_slots * len(names)
            region_fog = sum(fog_counts.get(n, 0) for n in names)
            rate = (100.0 * region_fog / region_slots) if region_slots else 0.0
            fig.text(
                0.5, y,
                f"【{label}】地点数 {len(names)} / 川霧発生 {region_fog}コマ"
                f"(全{region_slots}コマ中、発生率 {rate:.1f}%)",
                ha="center", fontsize=12,
            )
            y -= 0.12
        pdf.savefig(fig)
        plt.close(fig)

        def _station_fog_chart(title, names):
            if not total_slots or not names:
                fig = plt.figure(figsize=(8.27, 4))
                fig.text(0.5, 0.5, "この期間の観測データがありません。", ha="center", fontsize=12)
                pdf.savefig(fig)
                plt.close(fig)
                return
            sorted_counts = sorted(((n, fog_counts.get(n, 0)) for n in names), key=lambda kv: kv[1])
            names_sorted = [n for n, _ in sorted_counts]
            counts = [c for _, c in sorted_counts]
            fig, ax = plt.subplots(figsize=(8.27, 11.69))
            ax.barh(names_sorted, counts, color="#1a5276")
            ax.set_xlabel(f"川霧発生回数(観測コマ数、全{total_slots}コマ中)")
            ax.set_title(title)
            ax.tick_params(axis="y", labelsize=7)
            ax.xaxis.set_major_locator(MaxNLocator(integer=True))
            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)

        _station_fog_chart("只見川 地点別 川霧発生回数", tadami_stations)
        _station_fog_chart("会津 地点別 川霧発生回数", aizu_stations)

        # 気象概況(代表地点の実況値の期間平均・合計)。
        fig = plt.figure(figsize=(8.27, 6))
        fig.text(0.5, 0.92, "期間の気象概況(代表地点)", ha="center", fontsize=16, fontweight="bold")
        avg_temp = _weather_avg(COL_TEMP)
        total_precip = _weather_sum(COL_PRECIP)
        avg_humidity = _weather_avg(COL_HUMIDITY)
        avg_wind = _weather_avg(COL_WIND_SPEED)
        total_sunshine = _weather_sum(COL_SUNSHINE)
        if avg_temp is None:
            fig.text(0.5, 0.6, "この期間の気象データがありません。", ha="center", fontsize=12)
        else:
            rows = [
                f"平均気温: {avg_temp:.1f}℃",
                f"降水量合計: {total_precip:.1f}mm" if total_precip is not None else "降水量合計: -",
                f"平均相対湿度: {avg_humidity:.0f}%" if avg_humidity is not None else "平均相対湿度: -",
                f"平均風速: {avg_wind:.1f}m/s" if avg_wind is not None else "平均風速: -",
                f"日照時間合計: {total_sunshine:.1f}時間" if total_sunshine is not None else "日照時間合計: -",
            ]
            y = 0.75
            for row in rows:
                fig.text(0.5, y, row, ha="center", fontsize=12)
                y -= 0.1
        pdf.savefig(fig)
        plt.close(fig)

        # 現象内訳(全地点合算、川霧に限らず層雲・霧雨・雨まで含めた内訳)。
        if total_slots and any(phenom_code_totals.values()):
            sorted_items = sorted(phenom_code_totals.items(), key=lambda kv: kv[1])
            labels = [PHENOM_LABELS[c] for c, _ in sorted_items]
            counts = [c for _, c in sorted_items]
            colors = [PHENOM_COLORS.get(code, "#1a5276") for code, _ in sorted_items]
            fig, ax = plt.subplots(figsize=(8.27, 6))
            ax.barh(labels, counts, color=colors)
            ax.set_xlabel("発生回数(全地点合算・観測コマ数)")
            ax.set_title("現象別 発生内訳(全地点合算)")
            fig.tight_layout()
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


@app.route("/report", methods=["GET", "POST"])
def period_report():
    """期間レポート(PDF)。管理者専用ではなく、地点の分析ページ群と並ぶ一般公開の機能
    (元々/admin/reportだったが、内容が観測・予測の分析であり管理機能ではないため移動した)。"""
    today = now_jst_naive().date()
    default_start = today - timedelta(days=30)
    if request.method == "POST":
        try:
            start_date = datetime.strptime(request.form.get("start_date", ""), "%Y-%m-%d").date()
            end_date = datetime.strptime(request.form.get("end_date", ""), "%Y-%m-%d").date()
        except ValueError:
            flash("開始日・終了日を正しく指定してください。", "error")
            return redirect(url_for("period_report"))
        if start_date > end_date:
            flash("開始日は終了日より前の日付にしてください。", "error")
            return redirect(url_for("period_report"))
        if _report_rate_limited(session.get("user_id")):
            flash(f"短時間に生成しすぎです。{REPORT_RATE_LIMIT_WINDOW.seconds // 60}分ほど待ってからお試しください。", "error")
            return redirect(url_for("period_report"))
        try:
            pdf_bytes = _generate_report_pdf(start_date, end_date)
        except Exception:
            log.exception("Report generation failed")
            flash("レポートの生成に失敗しました。詳細はログを確認してください。", "error")
            return redirect(url_for("period_report"))
        filename = f"river-fog-report_{start_date}_{end_date}.pdf"
        return send_file(
            io.BytesIO(pdf_bytes), mimetype="application/pdf",
            as_attachment=True, download_name=filename,
        )
    return render_template("report.html", default_start=default_start, default_end=today)


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
        responses = announcements.list_responses(conn, announcement_id)
    finally:
        conn.close()
    users_conn = _users_conn()
    try:
        names_by_id = users.get_users_by_ids(users_conn, {r["user_id"] for r in responses if r["user_id"]})
    finally:
        users_conn.close()
    for r in responses:
        u = names_by_id.get(r["user_id"])
        r["respondent_name"] = u["name"] if u else "(退会済みユーザー)" if r["user_id"] else "(不明)"
        r["respondent_exists"] = u is not None
        if ann["kind"] == "choice" and r["choice_index"] is not None:
            choices = ann["choices"] or []
            r["choice_label"] = choices[r["choice_index"]] if r["choice_index"] < len(choices) else f"選択肢{r['choice_index']}"
    return render_template(
        "admin_announcement_results.html", ann=ann, summary=summary, responses=responses,
    )


@app.route("/announcements/<int:announcement_id>/respond", methods=["POST"])
def respond_announcement(announcement_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _announcements_conn()
    try:
        ann = announcements.get_announcement(conn, announcement_id)
        if ann is None or not ann["active"]:
            abort(404)
        choice_index = request.form.get("choice_index")
        answer_text = request.form.get("answer_text", "").strip() or None
        submitted_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        announcements.record_response(
            conn, announcement_id, my_id,
            int(choice_index) if choice_index not in (None, "") else None,
            answer_text, submitted_at,
        )
    finally:
        conn.close()
    message = "回答ありがとうございました。"
    if _wants_json():
        return jsonify({"message": message, "success": True})
    flash(message, "success")
    return redirect(request.referrer or url_for("dashboard"))


# 管理者タブ「仮検証(二値モデル)」。本番の予測モデルとは別系統の検証結果を表示する
# (experiment_model.py / experiment_view.py)。読み込みに失敗しても他の画面に影響しないよう、
# 失敗時はログに残してこのタブだけ無効にする。
try:
    import experiment_view
    experiment_view.register(app, requires_admin, _cached_chart_json, _is_script_running)
except Exception:
    log.exception("Failed to register the experiment (仮検証) admin tab")


# --- 重い集計を裏で先に作っておく(2026-10-08) ---------------------------------------------------
# 再起動直後(毎日03:40)や、キャッシュの期限切れ・毎時の観測の書き込みの後に最初に開いた人が、
# ダッシュボード約10秒・分析ページ約12秒・地点一覧約4秒待たされていた。期限が近いものから裏で作り直す。
WARM_INTERVAL_SECONDS = 120


def _warm_caches_once():
    def refresh(cache, ttl, fn):
        age = time.monotonic() - cache["at"]
        if cache["data"] is None or age > ttl - WARM_INTERVAL_SECONDS - 30:
            cache["at"] = -1e12  # 期限切れ扱いにして作り直させる(古い値は作り直すまで残る)
            fn()
    steps = (
        ("weather data", lambda: fog_data.load_river_fog_data()),  # 観測DBの更新時刻で自動的に読み直す
        ("public forecast", lambda: refresh(_public_forecast_cache, PUBLIC_CACHE_SECONDS, _public_forecast_safe)),
        ("accuracy summary", lambda: refresh(_accuracy_summary_cache, ACCURACY_SUMMARY_CACHE_SECONDS, _accuracy_summary_safe)),
        ("accuracy by weather", lambda: refresh(_accuracy_by_weather_cache, ACCURACY_SUMMARY_CACHE_SECONDS,
                                               _accuracy_by_weather_safe)),
        ("fog pair counts", lambda: refresh(_fog_pair_counts_cache, ACCURACY_SUMMARY_CACHE_SECONDS, _fog_pair_counts_safe)),
        ("fog alerts", lambda: refresh(_upcoming_alerts_cache, UPCOMING_ALERTS_CACHE_SECONDS + WARM_INTERVAL_SECONDS,
                                       _upcoming_fog_alerts_safe)),
    )
    for name, step in steps:
        try:
            step()
        except Exception as e:
            log.warning("Cache warm-up (%s) failed: %s", name, e)


def _warm_caches_forever():
    time.sleep(3)
    while True:
        t0 = time.monotonic()
        _warm_caches_once()
        log.debug("Cache warm-up took %.1fs", time.monotonic() - t0)
        time.sleep(WARM_INTERVAL_SECONDS)


# --- 正解セット(/gold、2026-10-09、gold_set.py) ------------------------------------------------------
# 毎日無作為に選んだ画像を、AIの判定を見せずに観測者が判定する。AIのラベルと予測の「本当の精度」を測るため。
import gold_set
import label_verify

_gold_ref_cache = {"data": None, "at": 0.0}


def _gold_reference(station, dt_str):
    """見比べ用の「見通しの良い時」の画像(label_verifyと同じ選び方、候補は1時間使い回す)。"""
    now = time.monotonic()
    if _gold_ref_cache["data"] is None or now - _gold_ref_cache["at"] > 3600:
        _gold_ref_cache["data"], _gold_ref_cache["at"] = label_verify.load_reference_candidates(), now
    return label_verify.pick_reference(_gold_ref_cache["data"], station, dt_str)


def _gold_daylight(station, dt_str):
    info = CAMERA_INFO.get(station, {})
    lat, lon = info.get("lat"), info.get("lon")
    if lat is None or lon is None:
        return 6 <= int(dt_str[11:13]) <= 16
    return fog_features_is_daylight(dt_str, lat, lon)


def _gold_allowed_stations():
    region = _observer_region_for_role(session.get("role"))
    return None if region is None else {n for n in STATION_NAMES if STATION_REGION.get(n) == region}


def _image_ref(station, dt):
    """テンプレート用: 画像があれば{date, hhmm, label}。"""
    date, hhmm = dt.strftime("%Y-%m-%d"), dt.strftime("%H%M")
    return {"date": date, "hhmm": hhmm, "time": dt.strftime("%H:%M"),
            "exists": (IMAGE_DIR / date / hhmm / f"{station}.jpg").is_file()}


@app.route("/gold")
@requires_observer
def gold_page():
    my_id = session.get("user_id")
    allowed = _gold_allowed_stations()
    conn = _users_conn()
    try:
        try:
            gold_set.ensure_samples(conn, now_jst_naive().date(), _gold_daylight,
                                    now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            log.warning("Gold set sampling failed: %s", e)
        sample = gold_set.next_sample(conn, my_id, allowed)
        remaining = gold_set.remaining_count(conn, my_id, allowed)
        mine = conn.execute("SELECT COUNT(*) FROM gold_labels WHERE user_id = ?", (my_id,)).fetchone()[0]
    finally:
        conn.close()
    ctx = {"sample": sample, "remaining": remaining, "mine": mine, "phenom_style": PHENOM_STYLE,
           "unsure_code": gold_set.UNSURE_CODE}
    if sample:
        dt = datetime.strptime(sample["target_datetime"], "%Y-%m-%d %H:%M:%S")
        ctx["target"] = _image_ref(sample["station_name"], dt)
        ctx["prev"] = _image_ref(sample["station_name"], dt - timedelta(hours=1))
        ctx["next"] = _image_ref(sample["station_name"], dt + timedelta(hours=1))
        try:
            ref = _gold_reference(sample["station_name"], sample["target_datetime"])
        except Exception as e:
            log.warning("Gold reference lookup failed: %s", e)
            ref = None
        ctx["ref"] = _image_ref(sample["station_name"], datetime.strptime(ref, "%Y-%m-%d %H:%M:%S")) if ref else None
        if ctx["ref"]:
            ctx["ref"]["date_label"] = ref[5:10].replace("-", "/")
    return render_template("gold.html", **ctx)


@app.route("/gold/<int:sample_id>/label", methods=["POST"])
@requires_observer
def gold_label(sample_id):
    my_id = session.get("user_id")
    code = request.form.get("code", type=int)
    if code is None or not (code == gold_set.UNSURE_CODE or 0 <= code <= 10):
        return _finish("gold_page", "現象を選んでください。", success=False)
    conn = _users_conn()
    try:
        sample = gold_set.get_sample(conn, sample_id)
        if sample is None:
            abort(404)
        _enforce_observer_region(sample["station_name"])
        codes_by_station, corrections_by_station = _phenom_codes_for_datetime(sample["target_datetime"])
        current = corrections_by_station.get(sample["station_name"], codes_by_station.get(sample["station_name"]))
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")

        def add_correction(station, dt, original, corrected, user_id, at):
            users.add_phenomena_correction(conn, station, dt, original, corrected, user_id, at, source="human")
            _record_audit("正解セットの判定で観測を訂正",
                          f"{station} {dt}: {format_phenom_display(original)} → {format_phenom_display(corrected)}")
        corrected = gold_set.save_label(conn, sample, my_id, code, now_str, current, add_correction)
    finally:
        conn.close()
    return _finish("gold_page", "判定を保存しました" + ("(AIの判定と違ったため、観測も訂正しました)。" if corrected else "。"))


@app.route("/gold/results")
@requires_observer
def gold_results():
    conn = _users_conn()
    try:
        res = gold_set.results(conn)
    finally:
        conn.close()
    return render_template("gold_results.html", r=res, group_names=GOLD_GROUP_NAMES)


GOLD_GROUP_NAMES = {0: "現象なし", 1: "川霧", 2: "全体霧", 3: "層雲", 4: "霧雨・雨"}


# --- 今朝の川霧ギャラリー・タイムラプス動画・月ごとのレポート・システムの健康(2026-10-11) -------------------------
import gallery
import haze_index
import timelapse

REPORTS_DIR = BASE_DIR / "data" / "reports"
HEALTH_DIR = BASE_DIR / "data" / "health"
GALLERY_CACHE_SECONDS = 300
_gallery_cache = {}


def _gallery_items(date_str):
    now = time.monotonic()
    hit = _gallery_cache.get(date_str)
    if hit and now - hit[0] < GALLERY_CACHE_SECONDS:
        return hit[1]
    try:
        items = gallery.morning_fog(date_str)
    except Exception as e:
        log.warning("Gallery failed for %s: %s", date_str, e)
        items = []
    for it in items:
        it["label"] = PHENOM_LABELS.get(it["code"], "川霧")
        it["region"] = STATION_REGION.get(it["station"], "")
    _gallery_cache[date_str] = (now, items)
    return items


@app.context_processor
def _inject_gallery():
    def morning_gallery(limit=3):
        today = now_jst_naive()
        # 11時までは今朝の分がまだ揃っていないので、出ている分だけ。0件なら前日の分
        items = _gallery_items(today.strftime("%Y-%m-%d"))
        if not items:
            return {"date": (today - timedelta(days=1)).strftime("%Y-%m-%d"), "items": _gallery_items(
                (today - timedelta(days=1)).strftime("%Y-%m-%d"))[:limit], "today": False}
        return {"date": today.strftime("%Y-%m-%d"), "items": items[:limit], "today": True}

    def haze_latest(name):
        try:
            rows = haze_index.latest_for_station(name)
        except Exception:
            return None
        # 直近3時間以内の画像の値だけ出す(古い値を「いま」の値に見せない)
        if rows and rows[0]["datetime"] >= (now_jst_naive() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"):
            return rows[0]
        return None
    def amedas_for(name):
        """地点ページ用: 最寄りのアメダスと、その直近3時間以内の実測値(無ければNone)。"""
        info = CAMERA_INFO.get(name, {})
        if info.get("lat") is None:
            return None
        try:
            import amedas
            st, km, alt = amedas.nearest(info["lat"], info["lon"])
            obs = amedas.latest_for(st)
        except Exception as e:
            log.warning("AMeDAS lookup failed for %s: %s", name, e)
            return None
        if obs and obs["datetime"] < (now_jst_naive() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"):
            obs = None
        return {"station": st, "km": km, "alt": alt, "obs": obs}

    return {"morning_gallery": morning_gallery, "haze_latest": haze_latest, "amedas_for": amedas_for,
            "fog_word": fog_features.target_word}  # 会津など全体霧も対象の地点は「霧」、ほかは「川霧」


@app.route("/gallery")
def gallery_page():
    date_str = request.args.get("date") or now_jst_naive().strftime("%Y-%m-%d")
    date_str = _validate_date(date_str)
    d = datetime.strptime(date_str, "%Y-%m-%d")
    today = now_jst_naive().date()
    items = _gallery_items(date_str)
    return render_template(
        "gallery.html", date=date_str, items=items, date_label=f"{d.month}月{d.day}日({'月火水木金土日'[d.weekday()]})",
        prev_date=(d - timedelta(days=1)).strftime("%Y-%m-%d"),
        next_date=(d + timedelta(days=1)).strftime("%Y-%m-%d") if d.date() < today else None,
        has_video=timelapse.ffmpeg_path() is not None,
    )


@app.route("/stations/<name>/timelapse/<date>.mp4")
def station_timelapse_video(name, date):
    name = _validate_station(name)
    date = _validate_date(date)
    try:
        path = timelapse.build(name, date)
    except Exception as e:
        log.warning("Timelapse video failed for %s %s: %s", name, date, e)
        path = None
    if path is None:
        abort(404)
    resp = send_file(path, mimetype="video/mp4", conditional=True,
                     download_name=f"{name}_{date}.mp4", as_attachment=request.args.get("download") == "1")
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


def _reports_list():
    out = []
    for p in sorted(REPORTS_DIR.glob("*.json"), reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (REPORTS_DIR / f"{d['month']}.pdf").is_file():
            out.append(d)
    return out


# --- 川霧の流れ図・見頃カレンダー(/flow、2026-10-12) ----------------------------------------------------
# 地点を川沿いに並べ、時刻ごとの霧を色で塗った表と、時間のスライダーで動く地図で、霧の広がり方を見せる。
# 観測(過去の日のカメラ画像の判定)と予報(今日〜明後日の出やすさ = 見頃カレンダー)の2つのモード。
# 地点の並び: 川の流路のデータは無いため、只見川は「伊南川と支流(南→北、伊南川合流部で只見川に合流)」の後に
# 「只見川の本流(上流→下流、FLOW_MAIN_STEMの順)」、会津は南→北に並べる(画面にもそう書く)。
FLOW_MAIN_STEM = ["伊南川合流部", "柴倉橋", "堅盤橋", "蒲生水道橋", "田沢橋", "二本木橋", "丹過橋", "越川側道橋", "湯倉橋",
                  "西谷橋", "金山ふれあい広場", "早戸大橋", "三島大橋", "麻生大橋", "大野橋"]
FLOW_HOURS = list(range(4, 20))


def _flow_sections(region):
    names = [n for n in STATION_NAMES if STATION_REGION.get(n) == region]
    lat = lambda n: CAMERA_INFO.get(n, {}).get("lat", 0)  # noqa: E731
    if region == "只見川":
        main = [n for n in FLOW_MAIN_STEM if n in names]
        others = sorted((n for n in names if n not in main), key=lat)
        return [("伊南川と支流(南→北)", others), ("只見川の本流(上流→下流)", main)]
    return [("会津(南→北)", sorted(names, key=lat))]


def _station_flow_order():
    """地点ページの「前の地点/次の地点」の順(2026-10-13): 流れ図と同じ並び(只見川は伊南川と支流→本流の上流→下流、会津は南→北)。"""
    out = []
    for region in ("只見川", "会津"):
        for _title, names in _flow_sections(region):
            out.extend(n for n in names if n not in out)
    out.extend(n for n in STATION_NAMES if n not in out)
    return out


def _flow_observed(date_str, names):
    """{地点: {時: {"code": 主コード, "label", "img": 画像があるか, "day": 日中か}}}。人の訂正を反映する。"""
    codes_by = {}
    w = sqlite3.connect(f"file:{WEATHER_DB_PATH}?mode=ro", uri=True)
    try:
        for dt, st, code in w.execute("SELECT datetime, station_name, code FROM phenomena WHERE datetime BETWEEN ? AND ?",
                                      (f"{date_str} 00:00:00", f"{date_str} 23:59:59")):
            codes_by[(st, int(dt[11:13]))] = code
    finally:
        w.close()
    conn = _users_conn()
    try:
        for st, dt, code in conn.execute("SELECT station_name, target_datetime, corrected_code FROM phenomena_corrections "
                                         "WHERE target_datetime BETWEEN ? AND ?", (f"{date_str} 00:00:00", f"{date_str} 23:59:59")):
            codes_by[(st, int(dt[11:13]))] = code
    finally:
        conn.close()
    out = {}
    for n in names:
        info = CAMERA_INFO.get(n, {})
        row = {}
        for h in FLOW_HOURS:
            code = codes_by.get((n, h))
            prim = None if code is None or code < 0 else (code // 100 if code > 10 else code)
            hhmm = f"{h:02d}00"
            day = (fog_features_is_daylight(f"{date_str} {h:02d}:00:00", info["lat"], info["lon"])
                   if info.get("lat") is not None else 6 <= h <= 16)
            row[h] = {"c": prim, "l": phenom_label(code) if prim is not None else "-", "d": bool(day),
                      "i": (IMAGE_DIR / date_str / hhmm / f"{n}.jpg").is_file()}
        out[n] = row
    return out


def _flow_forecast(day_index, names):
    data = _public_forecast_safe() or {}
    by = {s["name"]: s for s in data.get("stations", [])}
    out = {}
    for n in names:
        st = by.get(n)
        hours = (st["days"][day_index].get("hours") or {}) if st and day_index < len(st["days"]) else {}
        out[n] = {h: {"lv": int(hours.get(h, 0) or 0)} for h in FLOW_HOURS}
    days = data.get("days") or []
    return out, (days[day_index] if day_index < len(days) else None)


@app.route("/flow")
def flow_page():
    region = request.args.get("region") or "只見川"
    if region not in ("只見川", "会津"):
        abort(404)
    mode = request.args.get("mode") or "obs"
    if mode not in ("obs", "fc"):
        abort(404)
    sections = _flow_sections(region)
    names = [n for _, ns in sections for n in ns]
    ctx = {"region": region, "mode": mode, "sections": sections, "hours": FLOW_HOURS,
           "coords": {n: [CAMERA_INFO.get(n, {}).get("lat"), CAMERA_INFO.get(n, {}).get("lon")] for n in names}}
    if mode == "obs":
        today = now_jst_naive().date()
        date_str = _validate_date(request.args.get("date") or today.strftime("%Y-%m-%d"))
        d = datetime.strptime(date_str, "%Y-%m-%d")
        ctx.update(date=date_str, date_label=f"{d.month}月{d.day}日({'月火水木金土日'[d.weekday()]})",
                   prev_date=(d - timedelta(days=1)).strftime("%Y-%m-%d"),
                   next_date=(d + timedelta(days=1)).strftime("%Y-%m-%d") if d.date() < today else None,
                   cells=_flow_observed(date_str, names))
    else:
        day_index = request.args.get("day", type=int) or 0
        day_index = max(0, min(2, day_index))
        cells, day = _flow_forecast(day_index, names)
        # 見頃: 「出やすい」(3)の時刻がある地点を、早い時刻・長く続く順に
        best = []
        for n in names:
            hs = [h for h in FLOW_HOURS if cells[n][h]["lv"] >= 3]
            if hs:
                best.append({"name": n, "first": hs[0], "last": hs[-1], "n": len(hs)})
        best.sort(key=lambda b: (b["first"], -b["n"]))
        ctx.update(day_index=day_index, day=day, cells=cells, best=best[:12])
    return render_template("flow.html", **ctx, phenom_colors=PHENOM_COLORS)


# --- 研究用の書き出し(/export、2026-10-12、export_data.py) -------------------------------------------------
import export_data

EXPORT_PARTS = [("weather", "地点の気象(毎時)"), ("predictions", "予測の記録"), ("haze", "かすみ度"), ("images", "カメラ画像")]


@app.route("/export", methods=["GET", "POST"])
def export_page():
    today = now_jst_naive().date()
    if request.method == "GET":
        return render_template("export.html", station_names=STATION_NAMES, station_region=STATION_REGION,
                               parts=EXPORT_PARTS, start=(today - timedelta(days=7)).strftime("%Y-%m-%d"),
                               end=(today - timedelta(days=1)).strftime("%Y-%m-%d"), max_days=export_data.MAX_DAYS,
                               max_images=export_data.MAX_IMAGES)
    start, end = request.form.get("start", ""), request.form.get("end", "")
    try:
        d0, d1 = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return _finish("export_page", "期間を正しく入れてください。", success=False)
    if d1 < d0 or (d1 - d0).days + 1 > export_data.MAX_DAYS:
        return _finish("export_page", f"期間は{export_data.MAX_DAYS}日以内にしてください。", success=False)
    stations = [n for n in STATION_NAMES if n in set(request.form.getlist("stations"))]
    if not stations:
        return _finish("export_page", "地点を1つ以上選んでください。", success=False)
    parts = {p for p, _ in EXPORT_PARTS if request.form.get("part_" + p)}
    image_hours = set(range(request.form.get("img_from", 5, type=int), request.form.get("img_to", 10, type=int) + 1))
    sun_cache = {}

    def daylight(station, dt):
        info = CAMERA_INFO.get(station, {})
        key = (station, dt[:10])
        if key not in sun_cache:
            if info.get("lat") is None:
                sun_cache[key] = (6.0, 17.0)
            else:
                sun_cache[key] = fog_features.sun_times_hours(dt[:10], info["lat"], info["lon"])
        rise, set_ = sun_cache[key]
        h = int(dt[11:13]) + int(dt[14:16]) / 60
        return rise <= h <= set_

    tmp = tempfile.NamedTemporaryFile(prefix="rf_export_", suffix=".zip", delete=False)
    tmp.close()
    try:
        stats = export_data.build_zip(tmp.name, start, end, stations, STATION_REGION, parts, image_hours, daylight, phenom_label)
    except Exception:
        os.unlink(tmp.name)
        raise
    _record_audit("研究用データの書き出し", f"{start}〜{end}、{len(stations)}地点、{','.join(sorted(parts)) or '観測のみ'}: {stats}")
    resp = send_file(tmp.name, mimetype="application/zip", as_attachment=True,
                     download_name=f"river-fog_{start}_{end}.zip")
    resp.call_on_close(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
    return resp


@app.route("/reports")
def reports_page():
    return render_template("reports.html", reports=_reports_list())


@app.route("/reports/<month>.pdf")
def report_pdf(month):
    if not re.match(r"^\d{4}-\d{2}$", month):
        abort(404)
    path = REPORTS_DIR / f"{month}.pdf"
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="application/pdf", download_name=f"川霧レポート_{month}.pdf",
                     as_attachment=request.args.get("download") == "1")


# --- 地点ページの「この後3時間」「もう一つの判定」(2026-10-12) -------------------------------------------

def station_short_nowcast(name):
    """地点ページ用: 「この後3時間」の別予測(short_nowcast.py)。無い・古い場合はNone。
    [{"hour": 7, "prob": 0.42, "likely": True}, ...]と、発表時刻・判定の基準。"""
    try:
        import short_nowcast
        d = short_nowcast.latest()
    except Exception as e:
        log.warning("Short nowcast lookup failed: %s", e)
        return None
    if not d or name not in d.get("stations", {}):
        return None
    thr = d.get("threshold") or 0.5
    hours = sorted(d["stations"][name].items(), key=lambda kv: (int(kv[0]) - int(d["issued_at"][11:13])) % 24)
    return {"issued_at": d["issued_at"], "threshold": thr,
            "hours": [{"hour": int(h), "prob": p, "likely": p >= thr, "lead": i + 1} for i, (h, p) in enumerate(hours)]}


def station_own_judge(name):
    """地点ページ用: 自前の画像判定(own_judge.py、Geminiとは別の「もう一つの判定」)。"""
    try:
        import own_judge
        d = own_judge.latest_for(name)
        if d:
            try:
                d["agreement"] = json.loads(own_judge.CHECK_PATH.read_text(encoding="utf-8"))["test_vs_gemini_labels"]["agreement"]
            except Exception:
                d["agreement"] = None
        return d
    except Exception as e:
        log.warning("Own judge lookup failed: %s", e)
        return None


@app.context_processor
def _inject_station_extras():
    return {"station_short_nowcast": station_short_nowcast, "station_own_judge": station_own_judge}


@app.route("/api/backup-manifest", methods=["POST"])
def backup_manifest():
    """このPC(Windows)へのバックアップの報告(2026-10-12、RiverFogBackup/pull.ps1 が取り終えたあとに送る)。
    PCが持っている毎日のZIPの名前と、カメラ画像の日付ごとの枚数・大きさ。storage_plan.pyが、PCに確実にある古い日の画像だけを
    サーバーから間引く判断と、「PCへのバックアップが止まっていないか」の確認に使う。
    ログインではなく .env の RF_BACKUP_TOKEN で確かめ、公開URL(Funnel)からは受け付けない。トークンが無ければ404。"""
    token = os.environ.get("RF_BACKUP_TOKEN") or ""
    if not token or _is_funnel_request():
        abort(404)
    if not hmac.compare_digest(request.headers.get("X-RF-Backup-Token", ""), token):
        abort(403)
    data = request.get_json(silent=True) or {}
    days = data.get("image_days") or {}
    zips = data.get("zips") or []
    if not isinstance(days, dict) or not isinstance(zips, list) or len(days) > 5000 or len(zips) > 5000:
        abort(400)
    clean_days = {}
    for d, v in days.items():
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d)) and isinstance(v, dict):
            clean_days[d] = {"files": int(v.get("files") or 0), "bytes": int(v.get("bytes") or 0)}
    clean_zips = [z for z in zips if isinstance(z, str) and re.fullmatch(r"river-fog-backup_\d{8}_\d{6}\.zip", z)]
    out = {"received_at": now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"), "host": str(data.get("host") or "")[:60],
           "image_days": clean_days, "zips": sorted(clean_zips)}
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HEALTH_DIR / "offsite_manifest.json.tmp"
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(HEALTH_DIR / "offsite_manifest.json")
    return jsonify({"ok": True, "days": len(clean_days), "zips": len(clean_zips)})


@app.route("/forecast/terms")
def public_terms():
    return render_template("public_terms.html")


@app.route("/offline-sw.js")
def offline_service_worker():
    """2026-10-14にやめた「電波の弱い場所でも見られる」仕組みの、自分を解除して消える版(web_static/offline_sw.js)を配る。範囲を "/" にするため、
    /web_static/ ではなくサイトの直下から配信する(Service Workerは置いた場所より上の範囲を扱えない)。"""
    resp = send_from_directory(BASE_DIR / "web_static", "offline_sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/admin/health")
@requires_admin
def admin_health():
    def read(name):
        try:
            return json.loads((HEALTH_DIR / name).read_text(encoding="utf-8"))
        except Exception:
            return None
    return render_template("admin_health.html", report=read("latest.json"), smoke=read("smoke.json"), camera=read("camera_check.json"), drift=read("drift.json"), storage=read("storage.json"))


# --- Web Pushの定期判定(2026-10-09、push_notify.run_scheduled) ----------------------------------------
# 川霧の朝夕のまとめ・「まもなく出やすい」・「観測した」・カメラ故障・管理者向けのお知らせを5分ごとに判定する。
# url_forを使うため、ダミーのリクエストの文脈の中で動かす(セッションは空)。
PUSH_SCHEDULE_INTERVAL_SECONDS = 300


def _push_admin_alerts():
    overview = _admin_overview_cached()
    alerts = []
    if overview["disk_used_pct"] is not None and overview["disk_used_pct"] >= 85:
        alerts.append(("disk", f"ディスク使用率が{overview['disk_used_pct']}%です", url_for("admin_disk")))
    if not overview["collect_timer_active"]:
        alerts.append(("collect_timer", "毎時収集タイマーが停止しています", url_for("admin_status")))
    if overview["recent_restart_count"] >= RESTART_ALERT_THRESHOLD:
        alerts.append(("restarts", f"直近{RESTART_ALERT_WINDOW_HOURS}時間でwebappが{overview['recent_restart_count']}回"
                                   "起動しています", url_for("admin_log", key="webapp")))
    if overview["pending_signups"]:
        alerts.append(("signups", f"保留中の登録申請が{overview['pending_signups']}件あります", url_for("admin_accounts")))
    # 保存の計画(storage_plan.py、毎日06:30): ディスクがいっぱいになる見込み・PCへのバックアップの止まり
    try:
        sp = json.loads((HEALTH_DIR / "storage.json").read_text(encoding="utf-8"))
        if sp.get("checked_at", "")[:10] == now_jst_naive().strftime("%Y-%m-%d"):
            for a in sp.get("alerts") or []:
                alerts.append((f"storage-{a['kind']}", a["message"], url_for("admin_health")))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Storage plan result unreadable: %s", e)    # 予測の当たり方の見張り(drift_watch.py、毎週月曜)。その週の結果だけ。
    try:
        dw = json.loads((HEALTH_DIR / "drift.json").read_text(encoding="utf-8"))
        if dw.get("checked_at", "")[:10] >= (now_jst_naive() - timedelta(days=6)).strftime("%Y-%m-%d"):
            for a in dw.get("alerts") or []:
                alerts.append((f"drift-{a['kind']}", a["message"], url_for("admin_health")))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Drift watch result unreadable: %s", e)    # カメラの異常の自動検知(camera_check.py、毎日15:30)。その日の結果だけ(1件ごとに1日1回)。
    try:
        cc = json.loads((HEALTH_DIR / "camera_check.json").read_text(encoding="utf-8"))
        if cc.get("date") == now_jst_naive().strftime("%Y-%m-%d"):
            for a in cc.get("alerts") or []:
                alerts.append((f"camcheck-{a['kind']}-{a['station']}", a["message"], url_for("admin_health")))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Camera check result unreadable: %s", e)
    return alerts


def _short_nowcast_latest():
    import short_nowcast
    return short_nowcast.latest()


def _push_schedule_once():
    conn = _users_conn()
    try:
        if not users.push_subscriber_ids(conn):
            return  # 通知を受け取る端末が1台も無ければ何も読まない
    finally:
        conn.close()
    with app.test_request_context("/"):
        ctx = {"station_names": STATION_NAMES, "fog_codes": set(SUMMARY_FOG_CODES),
               "fog_codes_by_station": {n: set(fog_features.target_codes(n)) for n in STATION_NAMES}}
        for key, fn in (("forecast", _public_forecast_safe), ("observations", _latest_observations),
                        ("camera_failures", _stations_with_recent_camera_failures_cached),
                        ("admin_alerts", _push_admin_alerts), ("nowcast", _short_nowcast_latest)):
            try:
                ctx[key] = fn()
            except Exception as e:
                log.warning("Push schedule: failed to load %s: %s", key, e)
                ctx[key] = None
        push_notify.run_scheduled(ctx)


def _push_schedule_forever():
    time.sleep(60)
    while True:
        try:
            _push_schedule_once()
        except Exception:
            log.exception("Push schedule failed")
        time.sleep(PUSH_SCHEDULE_INTERVAL_SECONDS)


if __name__ == "__main__":
    from waitress import serve
    threading.Thread(target=_warm_caches_forever, name="cache-warmer", daemon=True).start()
    threading.Thread(target=_push_schedule_forever, name="push-scheduler", daemon=True).start()
    # 8080番は外部公開用にnginxが受け、Cockpit(9090番、別オリジン)と同一オリジンで
    # リバースプロキシするために127.0.0.1:8081をローカル専用で使う(直接の外部公開はしない)。
    # trusted_proxyを設定しないと、waitressはX-Forwarded-For等のヘッダーを黙って
    # 破棄してしまい、request.remote_addrが常に127.0.0.1(nginx/tailscaledからの接続元)
    # になってしまう。nginxとTailscale Funnelはどちらも127.0.0.1経由で接続してくる
    # ローカルの正規プロキシなので、これを信頼してX-Forwarded-Forを実際のクライアント
    # 情報として使う。
    log.info("Starting river-fog webapp on 127.0.0.1:8081 (behind nginx on :8080)")
    serve(
        # threads: waitressの既定は4。公開URL(Tailscale Funnel)はnginxを通らず直接ここに来るため、
        # 静的ファイルも含めた全リクエストを4本で捌くことになり、ページ表示の瞬間に処理待ちが
        # 数十件たまっていた。処理の大半はファイル読み出し・DB読み出しの待ちなので、CPU数(4)より
        # 多めにする。
        app, host="127.0.0.1", port=8081, threads=12,
        trusted_proxy="127.0.0.1", trusted_proxy_count=1,
        trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
        clear_untrusted_proxy_headers=True,
        ident="",  # 2026-10-14: 応答に「Server: waitress」を出さない(公開URLで使っているソフトを知らせない)
    )
