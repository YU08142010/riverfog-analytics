"""SMB共有(data/以下)と同じ内容をブラウザから見られるようにするダッシュボード。

LAN/Tailscale内からのみアクセスされる想定で、認証は行わない
（既存のSamba共有・Cockpitと同じセキュリティモデル）。外部公開はしないこと。
"""
import logging
import logging.handlers
import re
import sqlite3
from datetime import datetime

from flask import Flask, abort, render_template, send_from_directory

from common import BASE_DIR, DATA_XLSX, JST
import fog_data
import prediction_log
from fog_features import PHENOM_COLORS, PHENOM_LABELS, SLASH_COLOR, encode_phenomena_cell

IMAGE_DIR = BASE_DIR / "data" / "images"
PRED_DIR = BASE_DIR / "data" / "predictions"
DB_PATH = BASE_DIR / "data" / "predictions.db"

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


def _sorted_subdirs(base_dir, reverse=True):
    if not base_dir.is_dir():
        return []
    return sorted((p.name for p in base_dir.iterdir() if p.is_dir()), reverse=reverse)


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


if __name__ == "__main__":
    from waitress import serve
    log.info("Starting river-fog webapp on 0.0.0.0:8080")
    serve(app, host="0.0.0.0", port=8080)
