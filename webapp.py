"""SMB共有(data/以下)と同じ内容をブラウザから見られるようにするダッシュボード。

LAN/Tailscale内からのみアクセスされる想定で、認証は行わない
（既存のSamba共有・Cockpitと同じセキュリティモデル）。外部公開はしないこと。
"""
import csv
import io
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask, Response, abort, flash, redirect, render_template,
    request, send_from_directory, session, url_for,
)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager

from common import BASE_DIR, DATA_XLSX, JST
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
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

ADMIN_SESSION_LIFETIME = timedelta(minutes=15)

IMAGE_DIR = BASE_DIR / "data" / "images"
PRED_DIR = BASE_DIR / "data" / "predictions"
DB_PATH = BASE_DIR / "data" / "predictions.db"

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
RUNNABLE_SCRIPTS = {"collect": "collect.py", "predict": "predict.py"}

SYSTEMD_UNITS = ["river-fog-collect.timer", "river-fog-collect.service", "river-fog-web.service"]
DISK_USAGE_PARTS = [
    ("カメラ画像 (images/)", IMAGE_DIR),
    ("予測結果 (predictions/)", PRED_DIR),
    ("予測DB (predictions.db)", DB_PATH),
    ("観測データ (只見川.xlsx)", DATA_XLSX),
    ("モデル (models/)", BASE_DIR / "data" / "models"),
]

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

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HHMM_RE = re.compile(r"^\d{4}$")

STATION_FORECAST_RE = re.compile(r"^(.+)_予測_\d{8}\.png$")
SUMMARY_RE = re.compile(r"^全地点サマリー_\d{8}\.png$")
CSV_RE = re.compile(r"^予測結果_\d{8}\.csv$")


def _phenom_label_color(code):
    """現象コード(0〜10、またはNaN)をラベルと表示色に変換する。"""
    if code is None:
        return "-", "#ffffff"
    try:
        if code != code:  # NaN
            return "-", "#ffffff"
    except TypeError:
        pass
    code = int(code)
    if code == 0:
        return "現象なし", SLASH_COLOR
    return PHENOM_LABELS.get(code, f"コード{code}"), PHENOM_COLORS.get(code, "#ffffff")


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


def _sorted_subdirs(base_dir, reverse=True):
    if not base_dir.is_dir():
        return []
    return sorted((p.name for p in base_dir.iterdir() if p.is_dir()), reverse=reverse)


def requires_admin(view):
    """セッションログイン必須。未ログイン/期限切れなら/admin/loginへリダイレクトする。
    HTTP Basic認証にすると、ブラウザがパスワードを覚えて自動送信し続けてしまい
    ブラウザを閉じない限り再入力を求められないため、あえてセッションCookie方式にしている
    （ADMIN_SESSION_LIFETIME分だけ無操作が続くと自動的にログアウトされる）。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
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


def _upcoming_fog_alerts_safe():
    if not DB_PATH.exists():
        return []
    now_naive = datetime.now(JST).replace(tzinfo=None)
    from_str = now_naive.strftime("%Y-%m-%d %H:%M:%S")
    to_str = (now_naive + timedelta(hours=ALERT_HOURS_AHEAD)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
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
        label, color = _phenom_label_color(code)
        stations.append({"name": name, "label": label, "color": color})

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
    return render_template("observations_dates.html", dates=dates)


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
                label, color = _phenom_label_color(code)
                cells.append({"label": label, "color": color})
        rows.append({
            "time": dt.strftime("%H:%M"),
            "temp": main_row.get("気温(℃)"),
            "humidity": main_row.get("相対湿度(％)"),
            "precip": main_row.get("降水量(mm)"),
            "visibility": main_row.get("視程(km)"),
            "cells": cells,
        })

    return render_template("observations_date.html", date=date, station_names=station_names, rows=rows)


@app.route("/images")
def images_dates():
    return render_template("images_dates.html", dates=_sorted_subdirs(IMAGE_DIR))


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


@app.route("/predictions")
def predictions_dates():
    return render_template("predictions_dates.html", dates=_sorted_subdirs(PRED_DIR))


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
    return render_template("stations_list.html", station_names=STATION_NAMES)


@app.route("/stations/<name>")
def station_detail(name):
    name = _validate_station(name)

    observations = _latest_observations()
    current = None
    if observations:
        current = next((s for s in observations["stations"] if s["name"] == name), None)

    latest_pred_date, forecast_file = _station_latest_forecast_file(name)
    timelapse = _station_timelapse_images(name)

    return render_template(
        "station_detail.html",
        name=name,
        current=current,
        observations_datetime=observations["datetime"] if observations else None,
        latest_pred_date=latest_pred_date,
        forecast_file=forecast_file,
        timelapse=timelapse,
    )


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


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    next_path = request.values.get("next") or url_for("admin_home")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = url_for("admin_home")  # オープンリダイレクト対策

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if ADMIN_PASSWORD and secrets.compare_digest(password, ADMIN_PASSWORD):
            session.clear()
            session["is_admin"] = True
            session.permanent = True
            return redirect(next_path)
        error = "パスワードが違います。"

    return render_template("admin_login.html", error=error, next=next_path)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@requires_admin
def admin_home():
    logs = [{"key": k, "path": str(p), "exists": p.exists(),
             "size_kb": round(p.stat().st_size / 1024, 1) if p.exists() else 0}
            for k, p in ALLOWED_LOGS.items()]
    return render_template("admin.html", logs=logs)


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
    return render_template("admin_errors.html", issues=_recent_log_issues())


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

    return render_template("admin_models.html", rows=rows, rollback_lines=rollback_lines[-50:])


@app.route("/admin/models/chart.png")
@requires_admin
def admin_models_chart():
    png = _render_metrics_trend_png(_load_metrics_history_trend())
    return Response(png, mimetype="image/png")


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
    return render_template("admin_disk.html", rows=rows, disk=disk)


if __name__ == "__main__":
    from waitress import serve
    log.info("Starting river-fog webapp on 0.0.0.0:8080")
    serve(app, host="0.0.0.0", port=8080)
