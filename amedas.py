"""アメダス(気象庁の地上観測)の実測値(2026-10-12)。予測モデルの入力はこれまでどおりOpen-Meteoのモデル値のままで、
アメダスは「別の項目」として取り込み・表示する(地点ページの「最寄りのアメダス(実測)」、短時間予測 short_nowcast.py の入力)。

- 使う観測所: カメラの周り(70km以内)で気温と湿度を観測している10か所(STATIONS)。カメラごとに一番近い観測所を使う。
- 毎時15分(crontab): 気象庁の公開JSON(bosai/amedas/data/point、10分ごと・約9日分)から、正時の値を data/amedas.db に入れる。
- 過去分: 気象庁「過去の気象データ検索」の1時間ごとの値のページ(etrn hourly_a1/s1)から日ごとに読む(backfill)。
- 風向は16方位を度に、露点はJSONに無いので気温と湿度から求める(Magnusの式)。
出典: 気象庁ホームページ(アメダス)。
"""
import argparse
import json
import logging
import math
import re
import sqlite3
import time
from datetime import date, datetime, timedelta

import requests

from common import BASE_DIR, JST

DB = BASE_DIR / "data" / "amedas.db"
TABLE_CACHE = BASE_DIR / "data" / "amedas_stations.json"
HEADERS = {"User-Agent": "river-fog research (river fog observation, Fukushima)"}
# 名前: (JSONの観測所番号, 都府県番号, 過去データのblock番号, 種別 a=アメダス s=気象官署)
STATIONS = {
    "金山": ("36342", "36", "1044", "a"), "西会津": ("36251", "36", "1031", "a"), "若松": ("36361", "36", "47570", "s"),
    "南郷": ("36536", "36", "0301", "a"), "田島": ("36641", "36", "0303", "a"), "喜多方": ("36176", "36", "0286", "a"),
    "只見": ("36426", "36", "0297", "a"), "湯本": ("36562", "36", "1294", "a"), "猪苗代": ("36276", "36", "0290", "a"),
    "桧枝岐": ("36716", "36", "1295", "a"),
}
DIRS16 = ["北北東", "北東", "東北東", "東", "東南東", "南東", "南南東", "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西", "北"]
log = logging.getLogger("amedas")

SCHEMA = """
CREATE TABLE IF NOT EXISTS obs (
    station TEXT NOT NULL,
    datetime TEXT NOT NULL,
    temp REAL, dew_point REAL, humidity REAL, wind REAL, wind_dir_deg REAL, precip REAL, sun REAL,
    source TEXT NOT NULL,
    PRIMARY KEY (station, datetime)
);
"""


def connect():
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def station_table():
    """{観測所名: {"lat", "lon", "alt"}}。気象庁の観測所の一覧(amedastable.json)から作り、ファイルに置いて使い回す。"""
    if TABLE_CACHE.exists():
        return json.loads(TABLE_CACHE.read_text(encoding="utf-8"))
    t = requests.get("https://www.jma.go.jp/bosai/amedas/const/amedastable.json", headers=HEADERS, timeout=30).json()
    out = {}
    for name, (code, *_rest) in STATIONS.items():
        s = t.get(code)
        if s:
            out[name] = {"lat": s["lat"][0] + s["lat"][1] / 60, "lon": s["lon"][0] + s["lon"][1] / 60, "alt": s.get("alt")}
    TABLE_CACHE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def distance_km(lat1, lon1, lat2, lon2):
    return math.hypot((lat1 - lat2) * 111.0, (lon1 - lon2) * 111.0 * math.cos(math.radians((lat1 + lat2) / 2)))


def nearest(lat, lon):
    """(観測所名, 距離km, 標高m)。"""
    best = None
    for name, s in station_table().items():
        d = distance_km(lat, lon, s["lat"], s["lon"])
        if best is None or d < best[1]:
            best = (name, d, s.get("alt"))
    return best


def dew_point(temp, rh):
    if temp is None or rh is None or rh <= 0:
        return None
    a, b = 17.62, 243.12
    g = math.log(rh / 100.0) + a * temp / (b + temp)
    return round(b * g / (a - g), 1)


def _val(v):
    """JSONの [値, 品質] から値(品質が0=正常以外や欠測はNone)。"""
    if not v or v[0] is None or (len(v) > 1 and v[1] not in (0, None)):
        return None
    return v[0]


def collect_latest(conn):
    """最新の2ブロック(3時間ずつ)のJSONから、正時の値を入れる。戻り値: 入れた行数。"""
    latest = datetime.fromisoformat(requests.get("https://www.jma.go.jp/bosai/amedas/data/latest_time.txt",
                                                 headers=HEADERS, timeout=20).text.strip()).replace(tzinfo=None)
    rows = []
    for name, (code, *_rest) in STATIONS.items():
        for back in (0, 3, 6):
            t = latest - timedelta(hours=back)
            blk = t.replace(hour=(t.hour // 3) * 3)
            url = f"https://www.jma.go.jp/bosai/amedas/data/point/{code}/{blk.strftime('%Y%m%d')}_{blk.hour:02d}.json"
            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
                if r.status_code != 200:
                    continue
                data = r.json()
            except Exception as e:  # noqa: BLE001
                log.warning("AMeDAS %s %s: %s", name, url, e)
                continue
            for k, v in data.items():
                if not k.endswith("0000"):
                    continue  # 正時の値だけ
                dt = datetime.strptime(k, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:00:00")
                temp, rh = _val(v.get("temp")), _val(v.get("humidity"))
                wd = _val(v.get("windDirection"))
                rows.append((name, dt, temp, dew_point(temp, rh), rh, _val(v.get("wind")),
                             (wd % 16) * 22.5 if wd else (None if wd is None else 0.0),
                             _val(v.get("precipitation1h")), _val(v.get("sun1h")), "json"))
            time.sleep(0.3)
    with conn:
        conn.executemany("INSERT OR REPLACE INTO obs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def _num(s):
    s = (s or "").replace(")", "").replace("]", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def backfill_day(conn, name, day):
    """気象庁「過去の気象データ検索」の1時間ごとの値(その日の1〜24時)を入れる。戻り値: 入れた行数。"""
    code, prec, block, kind = STATIONS[name]
    url = (f"https://www.data.jma.go.jp/obd/stats/etrn/view/hourly_{kind}1.php?prec_no={prec}&block_no={block}"
           f"&year={day.year}&month={day.month}&day={day.day}&view=")
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.encoding = "utf-8"
    t = r.text
    i = t.find("tablefix1")
    if i < 0:
        return 0
    rows = []
    for row in re.findall(r'<tr class="mtx"[^>]*>(.*?)</tr>', t[i:], re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)]
        if not cells or not cells[0].isdigit():
            continue
        h = int(cells[0])
        dt = (datetime.combine(day, datetime.min.time()) + timedelta(hours=h)).strftime("%Y-%m-%d %H:00:00")
        if kind == "a":  # 時, 降水量, 気温, 露点, 蒸気圧, 湿度, 風速, 風向, 日照, 降雪, 積雪
            precip, temp, dew, rh, wind, wdir, sun = _num(cells[1]), _num(cells[2]), _num(cells[3]), _num(cells[5]), \
                _num(cells[6]), cells[7], _num(cells[8])
        else:            # 時, 気圧(現地), 気圧(海面), 降水量, 気温, 露点, 蒸気圧, 湿度, 風速, 風向, 日照, ...
            precip, temp, dew, rh, wind, wdir, sun = _num(cells[3]), _num(cells[4]), _num(cells[5]), _num(cells[7]), \
                _num(cells[8]), cells[9], _num(cells[10])
        deg = None
        if wdir in DIRS16:
            deg = ((DIRS16.index(wdir) + 1) % 16) * 22.5
        elif wdir == "静穏":
            deg = 0.0
        rows.append((name, dt, temp, dew, rh, wind, deg, precip if precip is not None else 0.0, sun, "etrn"))
    with conn:
        # 毎時のJSONで入れた値は上書きしない(同じ値のはず。JSONの方が早く入る)
        conn.executemany("INSERT OR IGNORE INTO obs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def backfill(conn, start, end):
    total = 0
    d = start
    while d <= end:
        for name in STATIONS:
            have = conn.execute("SELECT COUNT(*) FROM obs WHERE station = ? AND datetime BETWEEN ? AND ?",
                                (name, f"{d} 01:00:00", f"{d + timedelta(days=1)} 00:00:00")).fetchone()[0]
            if have >= 24:
                continue
            try:
                total += backfill_day(conn, name, d)
            except Exception as e:  # noqa: BLE001
                log.warning("AMeDAS backfill %s %s: %s", name, d, e)
            time.sleep(1.0)  # 気象庁のサイトに負担をかけないよう、ゆっくり
        d += timedelta(days=1)
    return total


def latest_for(name, at=None):
    """観測所の最新(またはat以前で最新)の毎時の値のdict。"""
    if not DB.exists():
        return None
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        q = "SELECT datetime, temp, dew_point, humidity, wind, wind_dir_deg, precip, sun FROM obs WHERE station = ?"
        params = [name]
        if at:
            q += " AND datetime <= ?"
            params.append(at)
        row = conn.execute(q + " AND temp IS NOT NULL ORDER BY datetime DESC LIMIT 1", params).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return dict(zip(("datetime", "temp", "dew_point", "humidity", "wind", "wind_dir_deg", "precip", "sun"), row))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-from")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.FileHandler(BASE_DIR / "logs" / "amedas.log", encoding="utf-8"), logging.StreamHandler()])
    conn = connect()
    try:
        station_table()
        if args.backfill_from:
            n = backfill(conn, date.fromisoformat(args.backfill_from), datetime.now(JST).date() - timedelta(days=1))
            log.info("Backfilled %d hourly row(s)", n)
        else:
            log.info("Collected %d hourly row(s)", collect_latest(conn))
    finally:
        conn.close()
