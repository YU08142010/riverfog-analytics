"""かすみ度(haze_index.py)の計算と照合(2026-10-11)。

  .venv/bin/python haze_run.py [--limit N]   新しい画像から順に、まだ計算していない日中の画像を計算する
                                            (crontab: 毎時40分に少し、毎晩23:10に多めに。2026-09-01以降が対象)
  .venv/bin/python haze_run.py --check      各指標・かすみ度が霧のラベルとどれだけ合うか
                                            (Geminiのラベル・人の訂正/確認・正解セット)を
                                            data/experiments/haze_check.json に書く
"""
import argparse
import csv
import json
import logging
import os
import sqlite3
from datetime import datetime
from functools import lru_cache

import haze_index as hz
import label_verify as lv
from common import BASE_DIR, CAMERAS_CSV, JST

FROM = "2026-09-01"
LOG_FILE = BASE_DIR / "logs" / "haze.log"
log = logging.getLogger("haze_run")


def _coords():
    out = {}
    with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("緯度") and row.get("経度"):
                out[row["地点名"]] = (float(row["緯度"]), float(row["経度"]))
    return out


def candidates():
    """計算していない日中の画像 [(地点, datetime)] を新しい順に。"""
    from fog_features import is_daylight
    coords = _coords()
    done = set()
    if hz.HAZE_DB.exists():
        c = hz.connect(read_only=True)
        done = {(s, d) for s, d in c.execute("SELECT station, datetime FROM haze")}
        c.close()
    out = []
    for date in sorted(os.listdir(lv.IMAGE_DIR), reverse=True):
        if date < FROM or len(date) != 10:
            continue
        for hhmm in sorted(os.listdir(lv.IMAGE_DIR / date), reverse=True):
            dt = f"{date} {hhmm[:2]}:{hhmm[2:]}:00"
            for fn in os.listdir(lv.IMAGE_DIR / date / hhmm):
                if not fn.endswith(".jpg") or fn.endswith("_unavailable.jpg"):
                    continue
                st = fn[:-4]
                if st not in coords or (st, dt) in done:
                    continue
                if is_daylight(dt, *coords[st]):
                    out.append((st, dt))
    return out


def run(limit):
    rows = candidates()
    if not rows:
        log.info("Nothing to compute")
        return
    refs = lv.load_reference_candidates()

    @lru_cache(maxsize=512)
    def feats(path):
        return hz._features(path)

    out, saved = [], 0
    conn = hz.connect()

    def flush():
        nonlocal out, saved
        if out:
            hz.save(conn, out)
            saved += len(out)
            out = []

    for st, dt in rows[:limit]:
        if len(out) >= 500:
            flush()  # 途中で止まっても、それまでの分は残るように少しずつ保存する
        ref = lv.pick_reference(refs, st, dt)
        if not ref:
            continue
        try:
            a, b = feats(str(lv.image_path(st, dt))), feats(str(lv.image_path(st, ref)))
        except Exception as e:  # noqa: BLE001  壊れた画像など
            log.warning("haze: %s %s: %s", st, dt, e)
            continue
        eps = 1e-6
        out.append((st, dt, (a["std"] / (b["std"] + eps), a["edge"] / (b["edge"] + eps), a["dark"] - b["dark"],
                             a["bright"] - b["bright"]), ref))
    try:
        flush()
    finally:
        conn.close()
    log.info("Computed %d of %d pending image(s)", saved, len(rows))


def check():
    """各指標とかすみ度が、霧のラベルとどれだけ合うか(AUC・AP)。"""
    import pandas as pd
    from sklearn.metrics import average_precision_score, roc_auc_score
    c = hz.connect(read_only=True)
    h = pd.read_sql("SELECT * FROM haze", c)
    c.close()
    h["score"] = [hz.score(e, d) for e, d in zip(h["edge_ratio"], h["dark_diff"])]
    w = sqlite3.connect(f"file:{lv.WEATHER_DB}?mode=ro", uri=True)
    ph = pd.read_sql("SELECT datetime, station_name AS station, code FROM phenomena WHERE code IS NOT NULL AND code >= 0 "
                     "AND datetime >= ?", w, params=(FROM,))
    w.close()
    u = sqlite3.connect(f"file:{lv.USERS_DB}?mode=ro", uri=True)
    corr = pd.read_sql("SELECT station_name AS station, target_datetime AS datetime, corrected_code AS hcode FROM "
                       "phenomena_corrections WHERE source = 'human'", u)
    gold = pd.read_sql("SELECT s.station_name AS station, s.target_datetime AS datetime, l.code AS gcode FROM gold_samples s "
                       "JOIN gold_labels l ON l.sample_id = s.id WHERE l.code >= 0", u)
    u.close()
    prim = lambda v: int(v) // 100 if int(v) > 10 else int(v)  # noqa: E731
    out = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "n_images": int(len(h)), "against": {}}
    metrics = {"contrast_ratio": -1, "edge_ratio": -1, "dark_diff": 1, "bright_diff": 1, "score": 1}

    def evaluate(df, label_col, name):
        res = {"n": int(len(df)), "positives": int(df[label_col].sum())}
        if df[label_col].nunique() < 2:
            out["against"][name] = res
            return
        for m, sign in metrics.items():
            x = df[m].astype(float) * sign
            ok = x.notna()
            res[m] = {"auc": round(float(roc_auc_score(df.loc[ok, label_col], x[ok])), 3),
                      "ap": round(float(average_precision_score(df.loc[ok, label_col], x[ok])), 3)}
        out["against"][name] = res

    j = h.merge(ph, on=["datetime", "station"])
    j["river"] = j["code"].map(prim).isin([1, 2, 3]).astype(int)
    j["anyfog"] = j["code"].map(prim).isin([1, 2, 3, 4, 5, 6]).astype(int)
    evaluate(j, "river", "Geminiのラベル: 川霧")
    evaluate(j, "anyfog", "Geminiのラベル: 霧(川霧+全体霧)")
    jc = h.merge(corr, on=["datetime", "station"])
    jc["river"] = jc["hcode"].map(prim).isin([1, 2, 3]).astype(int)
    evaluate(jc, "river", "人が訂正したラベル: 川霧")
    jg = h.merge(gold.drop_duplicates(["station", "datetime"]), on=["datetime", "station"])
    if len(jg):
        jg["river"] = jg["gcode"].map(prim).isin([1, 2, 3]).astype(int)
        evaluate(jg, "river", "正解セット: 川霧")
    (BASE_DIR / "data" / "experiments").mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "data" / "experiments" / "haze_check.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                                         encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()])
    try:
        os.nice(10)
    except OSError:
        pass
    if args.check:
        check()
    else:
        run(args.limit)
