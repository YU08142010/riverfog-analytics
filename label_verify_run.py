"""AIの二重判定(label_verify.py)を毎時まとめて行う(crontab、収集の後の毎時35分)。

Geminiの1回目の判定が川霧(1〜3)だった画像を、同じカメラの見通しの良い時の画像と見比べてもう一度判定し、
data/models/label_verify.csv に記録する。人が訂正済みの画像・判定済みの画像は飛ばす。
新しいものを先に、残りの枠で 2026-09-01 以降の過去分を少しずつ埋める。
クォータ超過になったらその回はやめる(次の回に続きから)。毎時の収集とは別のAPIキー
(label_verify.experiment_api_key)を使う。

実行: .venv/bin/python label_verify_run.py [--limit N]
"""
import argparse
import logging
import os
import sqlite3
import time
from datetime import datetime

import pandas as pd

import label_verify as lv
from common import BASE_DIR, JST

MODEL = os.environ.get("VERIFY_MODEL", "gemini-3.5-flash-lite")
BACKFILL_FROM = "2026-09-01 00:00:00"
DEFAULT_LIMIT = 60
CALL_INTERVAL_SEC = 4.5
LOG_FILE = BASE_DIR / "logs" / "label_verify.log"
log = logging.getLogger("label_verify_run")


def prim(v):
    v = int(v)
    return v // 100 if v > 10 else v


def candidates():
    w = sqlite3.connect(f"file:{lv.WEATHER_DB}?mode=ro", uri=True)
    try:
        ph = pd.read_sql("SELECT datetime, station_name, code FROM phenomena WHERE code IS NOT NULL AND code > 0 "
                         "AND datetime >= ?", w, params=(BACKFILL_FROM,))
    finally:
        w.close()
    ph = ph[ph["code"].map(prim).isin([1, 2, 3])]
    u = sqlite3.connect(f"file:{lv.USERS_DB}?mode=ro", uri=True)
    try:
        touched = {(s, d) for s, d in u.execute("SELECT station_name, target_datetime FROM phenomena_corrections")}
    finally:
        u.close()
    done = set()
    if lv.VERIFY_LOG.exists():
        v = pd.read_csv(lv.VERIFY_LOG, usecols=["datetime", "station"])
        done = set(zip(v["station"], v["datetime"]))
    rows = [(s, d, int(c)) for d, s, c in zip(ph["datetime"], ph["station_name"], ph["code"])
            if (s, d) not in touched and (s, d) not in done and lv.image_path(s, d).exists()]
    rows.sort(key=lambda r: r[1], reverse=True)  # 新しいものから
    return rows


def run(limit):
    key = lv.experiment_api_key()
    if not key:
        log.warning("No Gemini API key; skipping")
        return
    rows = candidates()
    if not rows:
        log.info("Nothing to verify")
        return
    refs = lv.load_reference_candidates()
    out, n_quota = [], False
    try:
        for station, dt, code in rows[:limit]:
            ref = lv.pick_reference(refs, station, dt)
            if not ref:
                continue
            v = lv.verify_one(station, dt, ref, MODEL, key)
            time.sleep(CALL_INTERVAL_SEC)
            if v is None:
                continue
            out.append({"datetime": dt, "station": station, "first_code": code, "verdict": v.verdict,
                        "confidence": v.confidence, "reasoning": v.reasoning.replace("\n", " ")[:200], "model": MODEL,
                        "ref_datetime": ref, "checked_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")})
    except lv.QuotaExhausted as e:
        n_quota = True
        log.warning("Quota exhausted; stopping this run: %s", e)
    finally:
        if out:
            lv.append_results(out)
    counts = pd.Series([r["verdict"] for r in out]).value_counts().to_dict() if out else {}
    log.info("Verified %d of %d candidate label(s)%s: %s", len(out), len(rows), " (quota)" if n_quota else "", counts)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        os.nice(10)
    except OSError:
        pass
    run(args.limit)
