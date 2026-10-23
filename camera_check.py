"""カメラの異常の自動検知(2026-10-12、crontab 毎日15:30)。

AIの判定が「現象なし」だった昼(10〜14時)の画像だけを使い、その地点の過去(BASE_FROM_DAYS〜BASE_TO_DAYS日前)の
同じ条件の画像と比べる。霧や雨の画像は使わないので、天気で画像が変わっても異常とは見なさない。
- 向きのずれ: 画像を小さくした輪郭の配置(どこに建物や木や橋の線があるか)の、過去の画像のうちいちばん似たものとの相関。
  過去の画像どうしの相関より大きく下がった日が続いたら「向きが変わった・何かが写り込んだ」可能性。
- レンズの汚れ・結露・ピンぼけ: 輪郭の強さ(ラプラシアンの平均)が、過去の中央値の半分未満の日が続いたら。
- 画像の固まり: 直近の毎時の画像が、ファイルの中身ごと同じものが3回以上続いたら(カメラ側の停止)。
  画像が届かない場合は既存の「カメラ画像が届いていません」(webappのcamera_failures)が知らせる。
結果は data/health/camera_check.json。管理者への通知はwebappの_push_admin_alerts、画面は /admin/health。
画像ごとの値は data/camera_check.db に保存して使い回す(初回だけ過去分の計算に数分かかる)。

実行: .venv/bin/python camera_check.py [--date YYYY-MM-DD]
"""
import argparse
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta

import numpy as np
from PIL import Image

import label_verify as lv
from common import BASE_DIR, JST

DB = BASE_DIR / "data" / "camera_check.db"
OUT = BASE_DIR / "data" / "health" / "camera_check.json"
HOURS = range(10, 15)
GRID_W, GRID_H = 48, 27
BASE_FROM_DAYS, BASE_TO_DAYS = 35, 7
RECENT_DAYS = 2
MIN_BASE, MIN_RECENT = 8, 3
ALIGN_DROP_MIN = 0.15
SHARP_RATIO = 0.5
FROZEN_RUN = 3
log = logging.getLogger("camera_check")


def connect():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA busy_timeout = 30000")
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("CREATE TABLE IF NOT EXISTS stats (station TEXT NOT NULL, datetime TEXT NOT NULL, edges BLOB NOT NULL, "
              "sharp REAL NOT NULL, lum REAL NOT NULL, PRIMARY KEY (station, datetime))")
    return c


def image_stats(path):
    with Image.open(path) as im:
        g = im.convert("L")
        w = 320
        g = g.resize((w, max(1, round(g.height * w / g.width))), Image.BILINEAR)
        a = np.asarray(g, dtype=np.float32) / 255.0
    a = a[int(a.shape[0] * 0.08):]  # 上端の日時の文字を外す
    lap = np.zeros_like(a)
    lap[1:-1, 1:-1] = np.abs(4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:])
    gx, gy = np.zeros_like(a), np.zeros_like(a)
    gx[:, 1:-1], gy[1:-1, :] = a[:, 2:] - a[:, :-2], a[2:, :] - a[:-2, :]
    mag = Image.fromarray(np.hypot(gx, gy))
    edges = np.asarray(mag.resize((GRID_W, GRID_H), Image.BOX), dtype=np.float32)
    return edges, float(lap.mean()), float(a.mean())


def _corr(a, b):
    a, b = a.ravel() - a.mean(), b.ravel() - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 0 else 0.0


def clear_images(since, until):
    """AIの判定が「現象なし」(人の訂正があればそちら)だった昼の画像の(地点, 日時)。"""
    w = sqlite3.connect(f"file:{lv.WEATHER_DB}?mode=ro", uri=True)
    try:
        rows = w.execute("SELECT station_name, datetime, code FROM phenomena WHERE datetime >= ? AND datetime < ?",
                         (since, until)).fetchall()
    finally:
        w.close()
    u = sqlite3.connect(f"file:{lv.USERS_DB}?mode=ro", uri=True)
    try:
        corr = {(s, d): c for s, d, c in u.execute(
            "SELECT station_name, target_datetime, corrected_code FROM phenomena_corrections WHERE target_datetime >= ?", (since,))}
    finally:
        u.close()
    out = []
    for s, d, c in rows:
        c = corr.get((s, d), c)
        if c == 0 and int(d[11:13]) in HOURS:
            out.append((s, d))
    return out


def load_stats(conn, pairs):
    have = {}
    for s, d, e, sh, lum in conn.execute("SELECT station, datetime, edges, sharp, lum FROM stats WHERE datetime >= ?",
                                         (min(p[1] for p in pairs),) if pairs else ("9999",)):
        have[(s, d)] = (np.frombuffer(e, dtype=np.float16).astype(np.float32).reshape(GRID_H, GRID_W), sh, lum)
    new = []
    for s, d in pairs:
        if (s, d) in have:
            continue
        p = lv.image_path(s, d)
        if not p.exists():
            continue
        try:
            e, sh, lum = image_stats(p)
        except Exception as ex:  # noqa: BLE001
            log.warning("stats failed for %s %s: %s", s, d, ex)
            continue
        have[(s, d)] = (e, sh, lum)
        new.append((s, d, e.astype(np.float16).tobytes(), sh, lum))
    if new:
        with conn:
            conn.executemany("INSERT OR REPLACE INTO stats VALUES (?, ?, ?, ?, ?)", new)
    log.info("image stats: %d cached, %d new", len(have) - len(new), len(new))
    return have


def frozen_stations(day):
    """その日の直近の画像が、中身ごと同じものがFROZEN_RUN回以上続いている地点 → 続いた回数。"""
    d = lv.IMAGE_DIR / day
    if not d.exists():
        return {}
    hours = sorted(p.name for p in d.iterdir() if p.is_dir())[-8:]
    last, run = {}, {}
    for hh in hours:
        for f in (d / hh).glob("*.jpg"):
            h = hashlib.md5(f.read_bytes()).hexdigest()
            s = f.stem
            if s.endswith("_unavailable"):  # 取得できなかった時の代わりの画像(camera_failuresが知らせる)
                continue
            run[s] = run.get(s, 1) + 1 if last.get(s) == h else 1
            last[s] = h
    return {s: n for s, n in run.items() if n >= FROZEN_RUN}


def check(day):
    end = datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)
    base_from, base_to = end - timedelta(days=BASE_FROM_DAYS), end - timedelta(days=BASE_TO_DAYS)
    recent_from = end - timedelta(days=RECENT_DAYS)
    fmt = "%Y-%m-%d %H:%M:%S"
    pairs = clear_images(base_from.strftime(fmt), end.strftime(fmt))
    conn = connect()
    try:
        stats = load_stats(conn, pairs)
    finally:
        conn.close()
    by_station = {}
    for (s, d), v in stats.items():
        by_station.setdefault(s, []).append((d, *v))
    stations, alerts = {}, []
    for s, items in sorted(by_station.items()):
        base = [x for x in items if base_from.strftime(fmt) <= x[0] < base_to.strftime(fmt)]
        recent = [x for x in items if x[0] >= recent_from.strftime(fmt)]
        info = {"n_base": len(base), "n_recent": len(recent)}
        stations[s] = info
        if len(base) < MIN_BASE or len(recent) < MIN_RECENT:
            continue
        # 過去の画像のうちいちばん似たものとの相関(日なた・日かげで輪郭が変わっても、過去の同じような天気の日と比べられる)。
        # 過去の画像どうしは自分自身を除いて比べる。
        B = np.stack([(x[1] - x[1].mean()).ravel() / (np.linalg.norm(x[1] - x[1].mean()) or 1) for x in base])
        sim = B @ B.T
        np.fill_diagonal(sim, -1)
        base_corr = sim.max(axis=1)
        R = np.stack([(x[1] - x[1].mean()).ravel() / (np.linalg.norm(x[1] - x[1].mean()) or 1) for x in recent])
        rec_all = R @ B.T
        rec_corr = rec_all.max(axis=1)
        mad = float(np.median(np.abs(base_corr - np.median(base_corr))))
        limit = float(np.median(base_corr)) - max(ALIGN_DROP_MIN, 3 * 1.4826 * mad)
        base_sharp = float(np.median([x[2] for x in base]))
        rec_sharp = float(np.median([x[2] for x in recent]))
        info.update({"align_base": round(float(np.median(base_corr)), 3), "align_recent": round(float(np.median(rec_corr)), 3),
                     "align_limit": round(limit, 3), "sharp_ratio": round(rec_sharp / base_sharp, 3) if base_sharp > 0 else None,
                     "latest": max(x[0] for x in recent)})
        # 直近2日のそれぞれで下回っていること(1日だけの一時的な写り込みでは知らせない)
        days = sorted({x[0][:10] for x in recent})
        if len(days) >= RECENT_DAYS:
            per_day_align = [np.median([c for x, c in zip(recent, rec_corr) if x[0][:10] == dd]) for dd in days]
            per_day_sharp = [np.median([x[2] for x in recent if x[0][:10] == dd]) for dd in days]
            if all(v < limit for v in per_day_align):
                alerts.append({"station": s, "kind": "align", "message": f"{s}: カメラの向きが変わったか、何かが写り込んでいる可能性があります"
                               f"(構図の一致 {np.median(rec_corr):.2f}、ふだん {np.median(base_corr):.2f})"})
            if base_sharp > 0 and all(v < SHARP_RATIO * base_sharp for v in per_day_sharp):
                alerts.append({"station": s, "kind": "sharp", "message": f"{s}: 晴れていても画像がぼやけています。レンズの汚れ・結露・ピントの"
                               f"可能性があります(くっきり度がふだんの{rec_sharp / base_sharp * 100:.0f}%)"})
    for s, n in sorted(frozen_stations(day).items()):
        alerts.append({"station": s, "kind": "frozen", "message": f"{s}: 同じ画像が{n}回続いています。カメラ側で画像の更新が止まっている可能性があります"})
        stations.setdefault(s, {})["frozen_run"] = n
    res = {"checked_at": datetime.now(JST).strftime(fmt), "date": day, "alerts": alerts, "stations": stations}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("camera check %s: %d alert(s) over %d station(s)", day, len(alerts), len(stations))
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(JST).strftime("%Y-%m-%d"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.FileHandler(BASE_DIR / "logs" / "camera_check.log", encoding="utf-8"), logging.StreamHandler()])
    try:
        os.nice(10)
    except OSError:
        pass
    check(args.date)
