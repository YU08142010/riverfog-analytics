"""カメラ画像の「かすみ度」(2026-10-11)。Geminiを使わず、サーバー上の画像処理だけで求める。

同じカメラの「見通しの良い時」の画像(label_verify.pick_referenceと同じ選び方)と比べて、
  - contrast_ratio: 明暗の散らばり(輝度の標準偏差)の比。霧で白っぽく平たくなると下がる
  - edge_ratio    : 輪郭のくっきり度(輝度のラプラシアンの絶対値の平均)の比。霞むと下がる
  - dark_diff     : ダークチャネル(RGBの最小値を小さな範囲で最小化したもの)の平均の差。霧で白く持ち上がると上がる
  - bright_diff   : 明るさの平均の差(逆光・時間帯の違いの目安)
を求め、data/haze.db に保存する。かすみ度(score、0〜100)は、霧のラベルとの照合(haze_check)で選んだ
組み合わせで計算する(score())。
使い道: ラベルが怪しい画像の目安・地点ページの表示・「今朝の川霧」ギャラリーの並び順。
"""
import sqlite3
from datetime import datetime

import numpy as np
from PIL import Image, ImageFilter

import label_verify as lv
from common import BASE_DIR

HAZE_DB = BASE_DIR / "data" / "haze.db"
WIDTH = 320

SCHEMA = """
CREATE TABLE IF NOT EXISTS haze (
    datetime TEXT NOT NULL,
    station TEXT NOT NULL,
    contrast_ratio REAL,
    edge_ratio REAL,
    dark_diff REAL,
    bright_diff REAL,
    ref_datetime TEXT,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (datetime, station)
);
CREATE INDEX IF NOT EXISTS idx_haze_station ON haze(station, datetime);
"""


def connect(read_only=False):
    if read_only:
        return sqlite3.connect(f"file:{HAZE_DB}?mode=ro", uri=True)
    conn = sqlite3.connect(HAZE_DB, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def _features(path):
    with Image.open(path) as im:
        im = im.convert("RGB")
        h = max(1, round(im.height * WIDTH / im.width))
        im = im.resize((WIDTH, h), Image.BILINEAR)
        # 画面の上端・下端の時刻表示などの文字を避けるため、上下を少し切る
        im = im.crop((0, int(h * 0.08), WIDTH, int(h * 0.95)))
        rgb = np.asarray(im, dtype=np.float32) / 255.0
        lum = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        lap = np.abs(4 * lum[1:-1, 1:-1] - lum[:-2, 1:-1] - lum[2:, 1:-1] - lum[1:-1, :-2] - lum[1:-1, 2:])
        mins = Image.fromarray((rgb.min(axis=2) * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(7))
        dark = np.asarray(mins, dtype=np.float32) / 255.0
    return {"std": float(lum.std()), "edge": float(lap.mean()), "dark": float(dark.mean()), "bright": float(lum.mean())}


def measure(station, dt_str, ref_dt):
    """(contrast_ratio, edge_ratio, dark_diff, bright_diff)。画像が無ければNone。"""
    p, r = lv.image_path(station, dt_str), lv.image_path(station, ref_dt)
    if not p.exists() or not r.exists():
        return None
    a, b = _features(p), _features(r)
    eps = 1e-6
    return (a["std"] / (b["std"] + eps), a["edge"] / (b["edge"] + eps), a["dark"] - b["dark"], a["bright"] - b["bright"])


# かすみ度(0〜100)。2026-10-11の照合(haze_run.py --check、28,579枚)で、霧のラベルとの一致(AUC)は輪郭の比だけが
# いちばん高く(霧全体0.77・川霧0.69)、ダークチャネルを混ぜると少し下がった(0.76・0.67)ため、輪郭の比だけで計算する。
SCORE_WEIGHTS = {"edge": 1.0, "dark": 0.0}


def score(edge_ratio, dark_diff):
    if edge_ratio is None or dark_diff is None:
        return None
    e = min(1.0, max(0.0, 1.0 - edge_ratio))          # 輪郭が基準の何割失われたか
    d = min(1.0, max(0.0, dark_diff / 0.35))         # ダークチャネルの持ち上がり(0.35で最大)
    return round(100 * (SCORE_WEIGHTS["edge"] * e + SCORE_WEIGHTS["dark"] * d))


def save(conn, rows):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO haze (datetime, station, contrast_ratio, edge_ratio, dark_diff, bright_diff, ref_datetime, "
            "computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(dt, st, *vals, ref, now) for st, dt, vals, ref in rows])


def latest_for_station(station, limit=1):
    """地点の最新のかすみ度 [{datetime, score, ...}]。DBが無ければ空。"""
    if not HAZE_DB.exists():
        return []
    conn = connect(read_only=True)
    try:
        rows = conn.execute("SELECT datetime, contrast_ratio, edge_ratio, dark_diff FROM haze WHERE station = ? "
                            "ORDER BY datetime DESC LIMIT ?", (station, limit)).fetchall()
    finally:
        conn.close()
    return [{"datetime": d, "contrast_ratio": c, "edge_ratio": e, "dark_diff": k, "score": score(e, k)} for d, c, e, k in rows]


def scores_between(start, end):
    """{(地点, datetime): score}。ギャラリー・報告書用。"""
    if not HAZE_DB.exists():
        return {}
    conn = connect(read_only=True)
    try:
        rows = conn.execute("SELECT station, datetime, edge_ratio, dark_diff FROM haze WHERE datetime BETWEEN ? AND ?",
                            (start, end)).fetchall()
    finally:
        conn.close()
    return {(s, d): score(e, k) for s, d, e, k in rows}
