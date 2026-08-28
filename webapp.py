"""SMB共有(data/以下)と同じ内容をブラウザから見られるようにするダッシュボード。

LAN/Tailscale内からのみアクセスされる想定で、認証は行わない
（既存のSamba共有・Cockpitと同じセキュリティモデル）。外部公開はしないこと。
"""
import csv
import io
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from PIL import Image
from flask import (
    Flask, Response, abort, flash, jsonify, redirect, render_template,
    request, send_file, send_from_directory, session, url_for,
)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager

from common import BASE_DIR, CAMERAS_CSV, DATA_XLSX, JST, NETWORK_FAILURE_LOG_PATH
import announcements
import users
import fog_data
import fog_model
import prediction_log
from fog_features import PHENOM_COLORS, PHENOM_LABELS, SLASH_COLOR, SUMMARY_FOG_CODES, encode_phenomena_cell

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

IMAGE_DIR = BASE_DIR / "data" / "images"
PRED_DIR = BASE_DIR / "data" / "predictions"
DB_PATH = BASE_DIR / "data" / "predictions.db"

AVATAR_DIR = BASE_DIR / "data" / "avatars"
AVATAR_SIZE = 256
AVATAR_MAX_UPLOAD_BYTES = 8 * 1024 * 1024
LOGIN_HEATMAP_DAYS = 371  # GitHubの草(コントリビューショングラフ)と同じく約53週間分

# ttydは全インターフェースにバインドし、Basic認証をかけた別プロセスとしてsystemdで動かしている
# (river-fog-terminal.service)。Tailscale/LANどちらからアクセスしても繋がるよう、iframeのURLは
# 固定せずリクエストのホスト名(request.host)に合わせて組み立てる（admin_terminal()参照）。
TTYD_PORT = 7681

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
    ("観測データ (只見川.xlsx)", DATA_XLSX),
    ("モデル (models/)", BASE_DIR / "data" / "models"),
]
BACKUP_ZIP_ROOT = BASE_DIR / "data"
RESTORE_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2GB
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

app = Flask(__name__, template_folder="web_templates", static_folder="web_static")
# 再起動のたびに変わってよい（管理者セッションが全員ログアウトされるだけ）ため、.envには
# 保存せずプロセス起動時に毎回生成する。
app.secret_key = secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = ADMIN_SESSION_LIFETIME
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True

try:
    STATION_NAMES = fog_data.load_station_names()
except Exception:
    STATION_NAMES = []

# メンテナンスモード中でも管理者(/admin配下)だけは常にアクセスできるようにする
# （でないと自分でオンにしたら誰もオフに戻せなくなる）。
MAINTENANCE_EXEMPT_PREFIXES = ("/admin", "/web_static", "/login", "/logout")
# サイト全体をログイン必須にする。ログイン・登録申請そのものと静的ファイルだけは例外。
LOGIN_EXEMPT_PREFIXES = ("/login", "/signup", "/logout", "/web_static")


@app.before_request
def _check_maintenance():
    if request.path.startswith(MAINTENANCE_EXEMPT_PREFIXES):
        return None
    state = _maintenance_state()
    if not state["enabled"]:
        return None
    return render_template("maintenance.html", message=state["message"]), 503


@app.before_request
def _require_login():
    """サイト全体をログイン必須にする（ログイン・登録申請そのものと静的ファイルは除く）。
    メンテナンスモードのチェックが先に走るため、メンテナンス中はここに到達しない。"""
    if request.path.startswith(LOGIN_EXEMPT_PREFIXES):
        return None
    if not session.get("user_id"):
        return redirect(url_for("login", next=request.path))
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


def _finish(endpoint, message, success=True, **url_kwargs):
    """通常のフォーム送信ならflash+redirect、fetch()経由のAjax送信ならJSONを返す。
    どちらで来たかはX-Requested-Withヘッダの有無で判定する（base.htmlのdata-ajaxフォーム用）。
    """
    if _wants_json():
        return jsonify({"message": message, "success": success}), (200 if success else 400)
    flash(message)
    return redirect(url_for(endpoint, **url_kwargs))


def _record_audit(action, detail=""):
    """アカウント管理・データ削除など、取り消しにくい管理操作を誰がいつ行ったか記録する
    （管理者タブの「操作ログ」ページで確認できる）。実行者は現在のセッションから取る。
    """
    conn = _users_conn()
    try:
        users.record_audit(
            conn, session.get("user_id"), session.get("name"), action, detail,
            datetime.now(JST).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
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
        try:
            conn = _users_conn()
            try:
                return len(users.list_signup_requests(conn, status="pending"))
            finally:
                conn.close()
        except Exception as e:
            log.warning("Failed to count pending signup requests: %s", e)
            return 0
    return {"pending_signup_count": pending_signup_count}


def _current_notifications():
    """通知ベル(base.html)の一覧。一般利用者には霧アラートのみ、管理者にはシステム状態の
    通知も加える。通知はDBに記録された履歴ではなく、その時点の状態から毎回計算するだけ
    （霧アラートは_upcoming_fog_alerts_safe()、システム状態は_admin_overview()を再利用）。
    既読管理はサーバー側に持たず、base.htmlのJSがlocalStorageで行う。
    """
    notifications = []
    nearest_by_station = {}
    for a in _upcoming_fog_alerts_safe():
        station = a["station"]
        if station not in nearest_by_station or a["target_datetime"] < nearest_by_station[station]["target_datetime"]:
            nearest_by_station[station] = a
    for station, a in sorted(nearest_by_station.items(), key=lambda kv: kv[1]["target_datetime"]):
        prob_pct = round((a["predicted_fog_prob"] or 0) * 100)
        notifications.append({
            "id": f"fog:{station}:{a['target_datetime']}",
            "level": "warning",
            "message": f"{station}: {a['target_datetime']}頃に霧の予測です（確率{prob_pct}%）",
            "url": url_for("station_detail", name=station),
        })

    if session.get("role") == "admin":
        overview = _admin_overview()
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
    return notifications


@app.context_processor
def _inject_notifications():
    def notifications_data():
        try:
            return _current_notifications()
        except Exception as e:
            log.warning("Failed to build notifications: %s", e)
            return []
    return {"notifications_data": notifications_data}


def requires_login(view):
    """ログイン必須（ロールは問わない）。未ログイン/期限切れなら/loginへリダイレクトする。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def requires_admin(view):
    """管理者ロールのアカウントでのログインを必須にする。
    HTTP Basic認証にすると、ブラウザがパスワードを覚えて自動送信し続けてしまい
    ブラウザを閉じない限り再入力を求められないため、あえてセッションCookie方式にしている
    （ADMIN_SESSION_LIFETIME分だけ無操作が続くと自動的にログアウトされる）。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            flash("この操作には管理者権限が必要です。")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


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


def _render_weather_timeseries_png(day_main, date_label):
    """1日分の気温・視程・湿度・降水量の推移をPNGにして返す。"""
    fig, ax1 = plt.subplots(figsize=(9, 3.6))
    if day_main is None or day_main.empty:
        ax1.text(0.5, 0.5, "データがありません", ha="center", va="center", transform=ax1.transAxes)
    else:
        x = day_main["datetime"]
        ax1.plot(x, day_main["気温(℃)"], color="#e74c3c", linewidth=1.4, marker="o", markersize=3, label="気温(℃)")
        ax1.plot(x, day_main["視程(km)"], color="#16a085", linewidth=1.4, marker="o", markersize=3, label="視程(km)")
        ax1.set_ylabel("気温(℃) / 視程(km)")
        ax1.grid(True, alpha=0.3)

        ax2 = ax1.twinx()
        ax2.bar(x, day_main["降水量(mm)"], width=1 / 48, color="#2980b9", alpha=0.35, label="降水量(mm)")
        ax2.plot(x, day_main["相対湿度(％)"], color="#8e44ad", linewidth=1.2, linestyle="--", alpha=0.8, label="相対湿度(％)")
        ax2.set_ylabel("相対湿度(％) / 降水量(mm)")
        ax2.set_ylim(bottom=0)

        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        fig.autofmt_xdate()

        h1, l1 = ax1.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax1.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8, ncol=2)

    ax1.set_title(f"{date_label} の観測値推移", fontsize=11)
    fig.tight_layout()
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
    """RandomForest単体・XGBoost単体・LightGBM単体・RF+LightGBM混合・採用中のhybridの
    霧F1平均を棒グラフで比較する。LightGBM単体とRF+LightGBM混合はどちらも診断目的の
    比較用モデルで、採用中のhybrid(RF+XGBブレンド)には含めていない
    (XGBoostとLightGBMはどちらもブースティング系で傾向が近いため、3モデル混合は
    追加せず、RFと組み合わせる相手をXGBかLightGBMかで比較できるようにしている)。
    """
    labels = ["RandomForest単体", "XGBoost単体", "LightGBM単体", "RF+LightGBM混合", "採用中(hybrid)"]
    keys = ["rf_only", "xgb_only", "lgbm_only", "rf_lgbm_only", None]
    colors = ["#7f8c8d", "#8e44ad", "#27ae60", "#d35400", "#1a5276"]
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

    fig, ax = plt.subplots(figsize=(7.6, 3.6))
    x = list(range(len(labels)))
    plotted = [v if v is not None else 0 for v in values]
    ax.bar(x, plotted, color=colors, width=0.55)
    for i, (v, n) in enumerate(zip(values, counts)):
        label = f"{v:.1f}%\n(n={n}地点)" if v is not None else "データなし"
        ax.text(i, (v or 0) + 2, label, ha="center", fontsize=8.5)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8.5)
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
    now_naive = datetime.now(JST).replace(tzinfo=None)
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

    now = datetime.now(JST).replace(tzinfo=None)
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
    """
    updates = {k: v for k, v in updates.items() if k in EDITABLE_ENV_KEYS}
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


_GEMINI_LINE_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_GEMINI_FALLBACK_RE = re.compile(r"fell back to sub key")
_GEMINI_QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|429")
_GEMINI_CONFIDENCE_RE = re.compile(r"Confidence distribution: high=(\d+), medium=(\d+), low=(\d+)")


def _gemini_log_stats(days=GEMINI_LOG_DAYS):
    """collect.logからGeminiまわりの状況(APIキーのフォールバック回数・クォータ超過回数・
    confidence分布)を日付ごとに集計する。"""
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
            now_str = datetime.now(JST).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
            return announcements.get_active_announcements(conn, now_str)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to load active announcements: %s", e)
        return []


def _accuracy_summary_safe():
    """予測精度サマリーを読み取り専用で取得する。DBが無い/読めない場合はNoneを返す。"""
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            return prediction_log.accuracy_summary(conn)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to read accuracy summary: %s", e)
        return None


def _latest_observations():
    """最新時刻の観測(気象値+地点ごとの現象コード)を返す。データが無ければNoneを返す。"""
    if not DATA_XLSX.exists():
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
        stations.append({"name": name, "label": label, "color": color, "text_color": text_color})

    return {
        "datetime": last_dt,
        "temp": main_row.get("気温(℃)"),
        "humidity": main_row.get("相対湿度(％)"),
        "precip": main_row.get("降水量(mm)"),
        "visibility": main_row.get("視程(km)"),
        "stations": stations,
    }


@app.route("/")
def dashboard():
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
    return render_template(
        "dashboard.html",
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
    if not DATA_XLSX.exists():
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
    if not DATA_XLSX.exists():
        abort(404)
    return send_file(DATA_XLSX, as_attachment=True, download_name="只見川.xlsx")


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
    return render_template("images_hour.html", date=date, hhmm=hhmm, files=files)


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


@app.route("/stations/<name>")
def station_detail(name):
    name = _validate_station(name)

    observations = _latest_observations()
    current = None
    current_by_name = {}
    if observations:
        current_by_name = {s["name"]: s for s in observations["stations"]}
        current = current_by_name.get(name)

    latest_pred_date, forecast_file = _station_latest_forecast_file(name)
    timelapse = _station_timelapse_images(name)
    image_dates = _sorted_subdirs(IMAGE_DIR)

    conn = _users_conn()
    try:
        raw_comments = users.list_station_comments(conn, name)
        author_names = {u["id"]: u["name"] for u in users.list_users(conn)}
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

    return render_template(
        "station_detail.html",
        name=name,
        current=current,
        observations_datetime=observations["datetime"] if observations else None,
        latest_pred_date=latest_pred_date,
        forecast_file=forecast_file,
        timelapse=timelapse,
        date_groups=_group_dates_by_month(image_dates),
        station_names=STATION_NAMES,
        current_by_name=current_by_name,
        active_names=[name],
        comments=comments,
        ai_station_summary=ai_station_summary,
        ai_summary_generated_at=ai_summary.get("generated_at") if ai_summary else None,
    )


@app.route("/stations/<name>/comments", methods=["POST"])
def station_comment_add(name):
    name = _validate_station(name)
    body = request.form.get("body", "").strip()[:1000]
    if not body:
        return _finish("station_detail", "コメントを入力してください。", success=False, name=name)
    conn = _users_conn()
    try:
        users.add_station_comment(
            conn, name, session["user_id"], body,
            datetime.now(JST).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
        )
    finally:
        conn.close()
    return _finish("station_detail", "コメントを投稿しました。", name=name)


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
    trend = _accuracy_trend_safe(station=name)
    dates = [r["date"] for r in trend if r["n"] > 0]
    rates = [100.0 * r["correct"] / r["n"] for r in trend if r["n"] > 0]
    png = _render_line_chart_png(dates, rates, f"{name} の的中率推移")
    return Response(png, mimetype="image/png")


@app.route("/accuracy")
def accuracy_page():
    summary = _accuracy_summary_safe()
    return render_template("accuracy.html", summary=summary)


@app.route("/accuracy/chart.png")
def accuracy_chart():
    trend = _accuracy_trend_safe()
    dates = [r["date"] for r in trend if r["n"] > 0]
    rates = [100.0 * r["correct"] / r["n"] for r in trend if r["n"] > 0]
    png = _render_line_chart_png(dates, rates, "全地点の的中率推移")
    return Response(png, mimetype="image/png")


@app.route("/login", methods=["GET", "POST"])
def login():
    next_path = request.values.get("next") or url_for("dashboard")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = url_for("dashboard")  # オープンリダイレクト対策

    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        conn = _users_conn()
        try:
            user = users.verify_login(conn, email, password)
        finally:
            conn.close()
        if user:
            session.clear()
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            session["name"] = user["name"]
            session.permanent = True
            login_at = datetime.now(JST).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
            conn = _users_conn()
            try:
                users.record_login(conn, user["id"], login_at)
            finally:
                conn.close()
            return redirect(next_path)
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


@app.route("/members")
def members_list():
    """ログイン中の全ユーザーが見られるメンバー一覧。個々のログイン履歴・
    ヒートマップはメンバー詳細ページ(member_detail)で確認する。"""
    today = datetime.now(JST).date()
    conn = _users_conn()
    try:
        all_users = users.list_users(conn)
        login_counts = {u["id"]: users.login_count(conn, u["id"]) for u in all_users}
        last_logins = {u["id"]: users.last_login(conn, u["id"]) for u in all_users}
        streaks = {u["id"]: users.login_streak(conn, u["id"], today) for u in all_users}
    finally:
        conn.close()
    members = [
        {
            "id": u["id"], "name": u["name"], "role": u["role"],
            "has_avatar": _avatar_path(u["id"]).exists(),
            "login_count": login_counts.get(u["id"], 0),
            "last_login": last_logins.get(u["id"]),
            "streak": streaks.get(u["id"], 0),
            "favorite_stations": [s for s in u["favorite_stations"].split(",") if s],
        }
        for u in all_users
    ]
    ranking = sorted(
        [m for m in members if m["login_count"] > 0], key=lambda m: -m["login_count"]
    )[:5]
    return render_template("members_list.html", members=members, ranking=ranking)


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
    finally:
        conn.close()
    favorite_stations = [s for s in target["favorite_stations"].split(",") if s]
    return render_template(
        "member_detail.html",
        member=target,
        history=history,
        counts_by_day=counts_by_day,
        total_logins=total_logins,
        streak=streak,
        favorite_stations=favorite_stations,
        station_names=STATION_NAMES,
        first_login=history[-1] if history else None,
        last_login=history[0] if history else None,
        has_avatar=_avatar_path(user_id).exists(),
        is_self=(session.get("user_id") == user_id),
        heatmap_days=LOGIN_HEATMAP_DAYS,
        avatar_size=AVATAR_SIZE,
    )


@app.route("/members/<int:user_id>/profile", methods=["POST"])
def member_profile_update(user_id):
    """自己紹介文とお気に入り地点は本人のみが変更できる(アバターと同じ制限)。"""
    if session.get("user_id") != user_id:
        abort(403)
    bio = request.form.get("bio", "").strip()[:300]
    favorite_stations = [s for s in request.form.getlist("favorite_stations") if s in STATION_NAMES]
    conn = _users_conn()
    try:
        users.update_profile(conn, user_id, bio=bio, favorite_stations=",".join(favorite_stations))
    finally:
        conn.close()
    return _finish("member_detail", "プロフィールを更新しました。", user_id=user_id)


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
                    requested_at = datetime.now(JST).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
                    users.create_signup_request(
                        conn, name, email, users.generate_password_hash(password), requested_at,
                    )
            finally:
                conn.close()
            if not error:
                return render_template("signup.html", submitted=True)

    return render_template("signup.html", error=error, submitted=False)


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
    network_failures = _network_failure_summary()
    model_state = fog_model.load_model_state()
    last_trained_at = None
    if model_state:
        trained_ats = [info.get("trained_at") for info in model_state.values() if info.get("trained_at")]
        if trained_ats:
            last_trained_at = max(trained_ats)
    try:
        conn = _users_conn()
        try:
            pending_signups = len(users.list_signup_requests(conn, status="pending"))
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to count pending signup requests: %s", e)
        pending_signups = 0
    return {
        "web_active": web_state == "active",
        "collect_timer_active": collect_timer_state == "active",
        "disk_used_pct": disk_used_pct,
        "pending_signups": pending_signups,
        "network_failures_24h": network_failures["count_24h"],
        "last_trained_at": last_trained_at,
        "model_count": len(model_state),
    }


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
    finally:
        conn.close()
    return render_template("admin_accounts.html", accounts=accounts, pending=pending)


@app.route("/admin/accounts/<int:user_id>/role", methods=["POST"])
@requires_admin
def admin_accounts_role(user_id):
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
        users.set_role(conn, user_id, role)
        _record_audit("ロール変更", f"{target['name']}({target['email']})を{target['role']}→{role}に変更")
        return _finish("admin_accounts", f"{target['name']}さんのロールを変更しました。")
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
        flash(message)
        return redirect(url_for("login"))
    return _finish("admin_accounts", message)


@app.route("/admin/accounts/requests/<int:request_id>/approve", methods=["POST"])
@requires_admin
def admin_accounts_approve(request_id):
    conn = _users_conn()
    try:
        req = users.get_signup_request(conn, request_id)
        created_at = datetime.now(JST).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
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
        created_at = datetime.now(JST).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
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


@app.route("/admin/run/<key>", methods=["POST"])
@requires_admin
def admin_run(key):
    script = RUNNABLE_SCRIPTS.get(key)
    if script is None:
        abort(404)
    if _is_script_running(script):
        flash(f"{script} は既に実行中のため、開始しませんでした。")
    else:
        _run_script_async(script)
        flash(f"{script} の実行を開始しました。数分後にログを確認してください。")
    return redirect(url_for("admin_status"))


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
        flash("webappサービスを再起動します(数秒後にページを再読み込みしてください)。")
    except Exception as e:
        log.exception("Failed to trigger webapp restart")
        flash(f"再起動の開始に失敗しました: {e}")
    return redirect(url_for("admin_status"))


@app.route("/admin/models")
@requires_admin
def admin_models():
    state = fog_model.load_model_state()
    rows = []
    for col in sorted(state.keys()):
        info = state[col]
        rows.append({
            "station": col,
            "model_type": info.get("model_type"),
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
        flash(f"{cutoff.strftime('%Y-%m-%d')} より前のカメラ画像アーカイブを{deleted}日分削除しました(解放: {_human_size(freed_bytes)})。")
        _record_audit(
            "カメラ画像アーカイブ削除",
            f"{cutoff.strftime('%Y-%m-%d')}より前を{deleted}日分削除(解放: {_human_size(freed_bytes)})",
        )
    else:
        flash("削除対象の画像アーカイブはありませんでした。")
    return redirect(url_for("admin_disk"))


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
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in BACKUP_ZIP_ROOT.rglob("*"):
                if f.is_file():
                    zf.write(f, arcname=str(f.relative_to(BACKUP_ZIP_ROOT)))
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


def _safe_extract_zip(zf, dest_dir):
    """zip-slip対策: 各エントリの展開先がdest_dir配下に収まることを、実際に展開する前に
    全件確認する（絶対パスや".."を含むエントリで想定外の場所に書き込まれるのを防ぐ）。
    """
    dest_dir = dest_dir.resolve()
    for member in zf.infolist():
        member_path = (dest_dir / member.filename).resolve()
        if member_path != dest_dir and dest_dir not in member_path.parents:
            raise ValueError(f"Unsafe path in zip: {member.filename}")
    zf.extractall(dest_dir)


@app.route("/admin/backup/restore", methods=["POST"])
@requires_admin
def admin_backup_restore():
    """アップロードされたバックアップZIPでdata/一式を丸ごと復元する。
    アカウント情報を含む全データを置き換える取り消しにくい操作のため、確認フレーズの
    一致を必須にし、復元前のdata/は削除せず日時付きディレクトリに退避しておく
    （何か問題があれば手動で戻せるように）。
    """
    confirm = request.form.get("confirm", "").strip().lower()
    if confirm != RESTORE_CONFIRM_PHRASE:
        flash(f'確認のため、入力欄に半角で "{RESTORE_CONFIRM_PHRASE}" と入力してください。')
        return redirect(url_for("admin_disk"))

    file = request.files.get("backup_zip")
    if not file or not file.filename:
        flash("ZIPファイルを選択してください。")
        return redirect(url_for("admin_disk"))
    if not file.filename.lower().endswith(".zip"):
        flash("ZIPファイル(.zip)を選択してください。")
        return redirect(url_for("admin_disk"))

    tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp_zip_path = Path(tmp_zip.name)
    tmp_zip.close()
    staging_dir = None
    try:
        file.save(tmp_zip_path)
        if tmp_zip_path.stat().st_size > RESTORE_MAX_UPLOAD_BYTES:
            flash(f"ファイルが大きすぎます({RESTORE_MAX_UPLOAD_BYTES // (1024 ** 3)}GB以下にしてください)。")
            return redirect(url_for("admin_disk"))

        try:
            zf = zipfile.ZipFile(tmp_zip_path)
        except zipfile.BadZipFile:
            flash("有効なZIPファイルではありません。")
            return redirect(url_for("admin_disk"))

        staging_dir = Path(tempfile.mkdtemp(prefix="river-fog-restore-", dir=str(BASE_DIR)))
        with zf:
            _safe_extract_zip(zf, staging_dir)

        stamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
        pre_restore_dir = BASE_DIR / f"data_before_restore_{stamp}"
        data_dir = BASE_DIR / "data"
        if data_dir.exists():
            data_dir.rename(pre_restore_dir)
        staging_dir.rename(data_dir)
        staging_dir = None

        _record_audit(
            "バックアップ復元",
            f"アップロードされたZIPからdata/を復元(復元前を{pre_restore_dir.name}に退避)",
        )
        flash(
            f"data/一式を復元しました。復元前のデータは {pre_restore_dir.name} "
            "に退避してあります(問題なければ後で手動削除してください)。サービスを再起動します。"
        )
        subprocess.Popen(
            ["/bin/sh", "-c", "sleep 1 && sudo -n systemctl restart river-fog-web.service"],
            start_new_session=True,
        )
    except Exception as e:
        log.exception("Backup restore failed")
        flash(f"復元に失敗しました: {e}")
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
    before_dt = (datetime.now(JST).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            deleted = prediction_log.delete_verified_before(conn, before_dt)
        finally:
            conn.close()
        flash(f"{before_dt} より前の答え合わせ済みレコードを{deleted}件削除しました。")
        _record_audit("予測DBクリーンアップ", f"{before_dt}より前の検証済みレコードを{deleted}件削除")
    except Exception as e:
        log.exception("DB cleanup failed")
        flash(f"クリーンアップに失敗しました: {e}")
    return redirect(url_for("admin_db"))


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
            flash("設定を保存しました。次回のcollect.py/predict.py実行から反映されます。")
        except Exception as e:
            log.exception("Failed to write .env")
            flash(f"設定の保存に失敗しました: {e}")
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


@app.route("/admin/gemini")
@requires_admin
def admin_gemini():
    return render_template("admin_gemini.html", rows=_gemini_log_stats())


@app.route("/admin/maintenance", methods=["GET", "POST"])
@requires_admin
def admin_maintenance():
    if request.method == "POST":
        enabled = request.form.get("enabled") == "on"
        message = request.form.get("message", "").strip()
        _set_maintenance_state(enabled, message)
        flash("メンテナンスモードを有効にしました。" if enabled else "メンテナンスモードを解除しました。")
        return redirect(url_for("admin_maintenance"))
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
            flash("選択式アンケートには選択肢を2つ以上入力してください。")
            return redirect(url_for("admin_announcements"))
    if not message:
        flash("本文/質問文を入力してください。")
        return redirect(url_for("admin_announcements"))

    created_at = datetime.now(JST).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    conn = _announcements_conn()
    try:
        announcements.create_announcement(conn, kind, message, choices, start_at, end_at, created_at)
    finally:
        conn.close()
    flash("お知らせ/アンケートを作成しました。")
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
    flash("削除しました。")
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
        submitted_at = datetime.now(JST).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        announcements.record_response(
            conn, announcement_id, visitor_id,
            int(choice_index) if choice_index not in (None, "") else None,
            answer_text, submitted_at,
        )
    finally:
        conn.close()
    flash("回答ありがとうございました。")
    response = redirect(request.referrer or url_for("dashboard"))
    if is_new:
        response.set_cookie(VISITOR_COOKIE_NAME, visitor_id, max_age=VISITOR_COOKIE_MAX_AGE)
    return response


if __name__ == "__main__":
    from waitress import serve
    log.info("Starting river-fog webapp on 0.0.0.0:8080")
    serve(app, host="0.0.0.0", port=8080)
