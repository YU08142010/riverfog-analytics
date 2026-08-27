"""SMB共有(data/以下)と同じ内容をブラウザから見られるようにするダッシュボード。

LAN/Tailscale内からのみアクセスされる想定で、認証は行わない
（既存のSamba共有・Cockpitと同じセキュリティモデル）。外部公開はしないこと。
"""
import io
import logging
import logging.handlers
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, Response, abort, render_template, request, send_from_directory

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager

from common import BASE_DIR, DATA_XLSX, JST
import fog_data
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
    """HTTP Basic認証。パスワードは.envのADMIN_PASSWORD（未設定なら常に拒否）。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        ok = bool(ADMIN_PASSWORD) and auth is not None and \
            secrets.compare_digest(auth.password or "", ADMIN_PASSWORD)
        if not ok:
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="river-fog admin"'},
            )
        return view(*args, **kwargs)
    return wrapped


def _render_line_chart_png(dates, rates, title):
    """日付ごとの的中率(%)を折れ線グラフのPNGバイト列にして返す。"""
    fig, ax = plt.subplots(figsize=(9, 3.2))
    if dates:
        x = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
        ax.plot(x, rates, marker="o", markersize=3, color="#1a5276", linewidth=1.4)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
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


if __name__ == "__main__":
    from waitress import serve
    log.info("Starting river-fog webapp on 0.0.0.0:8080")
    serve(app, host="0.0.0.0", port=8080)
