"""今朝の川霧ギャラリー(2026-10-11)。その日の朝(5〜11時)に川霧を観測した画像から、地点ごとに1枚、
霧がよく出ている順に選ぶ。

- 霧かどうか: カメラ画像のAI判定(人の訂正があればそちら)が川霧(1〜3)。二重判定(label_verify)で
  「霧なし」とされた画像は除く。
- 並び順: かすみ度(haze_index)が高い順。かすみ度がまだ無ければ、濃さ(濃い川霧3→川霧2→薄い川霧1)、早い時刻の順。
"""
import sqlite3

import pandas as pd

import haze_index as hz
from common import BASE_DIR

WEATHER_DB = BASE_DIR / "data" / "weather.db"
USERS_DB = BASE_DIR / "data" / "users.db"
VERIFY_LOG = BASE_DIR / "data" / "models" / "label_verify.csv"
IMAGE_DIR = BASE_DIR / "data" / "images"
MORNING = (5, 11)


def _prim(v):
    v = int(v)
    return v // 100 if v > 10 else v


def morning_fog(date, limit=12):
    """date(YYYY-MM-DD)の朝の川霧の画像 [{station, datetime, hhmm, code, score}](地点ごとに1枚)。"""
    start, end = f"{date} {MORNING[0]:02d}:00:00", f"{date} {MORNING[1]:02d}:00:00"
    w = sqlite3.connect(f"file:{WEATHER_DB}?mode=ro", uri=True)
    try:
        ph = pd.read_sql("SELECT datetime, station_name AS station, code FROM phenomena WHERE datetime BETWEEN ? AND ? "
                         "AND code IS NOT NULL AND code > 0", w, params=(start, end))
    finally:
        w.close()
    u = sqlite3.connect(f"file:{USERS_DB}?mode=ro", uri=True)
    try:
        corr = {(s, d): c for s, d, c in u.execute(
            "SELECT station_name, target_datetime, corrected_code FROM phenomena_corrections WHERE target_datetime BETWEEN ? AND ?",
            (start, end))}
    finally:
        u.close()
    disputed = set()
    if VERIFY_LOG.exists():
        v = pd.read_csv(VERIFY_LOG)
        v = v[(v["verdict"] == "none") & (v["confidence"] == "high") & v["datetime"].between(start, end)]
        disputed = set(zip(v["station"], v["datetime"]))
    scores = hz.scores_between(start, end)
    picks = {}
    for dt, st, code in zip(ph["datetime"], ph["station"], ph["code"]):
        code = corr.get((st, dt), code)
        if code is None or _prim(code) not in (1, 2, 3) or ((st, dt) in disputed and (st, dt) not in corr):
            continue
        hhmm = dt[11:13] + dt[14:16]
        if not (IMAGE_DIR / date / hhmm / f"{st}.jpg").is_file():
            continue
        item = {"station": st, "datetime": dt, "hhmm": hhmm, "code": _prim(code), "score": scores.get((st, dt))}
        key = ((item["score"] if item["score"] is not None else -1), item["code"], -int(hhmm))
        if st not in picks or key > picks[st][0]:
            picks[st] = (key, item)
    items = [p[1] for p in sorted(picks.values(), key=lambda p: p[0], reverse=True)]
    return items[:limit]
