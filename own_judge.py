"""自前の画像判定(2026-10-12)。Gemini(本番のラベル)とは別の「もう一つの判定」で、サーバー上の画像処理と
LightGBMだけで動く(APIを使わない)。

- 特徴量(features): 同じカメラの「見通しの良い時」の画像(label_verify.pick_referenceと同じ選び方)と比べた、
  画面を3×3に分けた区画ごとの「輪郭の比」「明るさの差」「白っぽい画素の割合の差」、画面全体の彩度の比・ダークチャネルの差、
  撮影時刻、地点の過去の霧の割合。川面だけが霞む川霧と、画面全体が霞む全体霧を見分けるため区画に分ける。
- 学習(--train、毎晩20:20): ラベルはGeminiの判定(人の訂正があればそちら、訂正は重み5倍)を5つのまとまり
  (現象なし・川霧・全体霧・層雲・雨)にしたもの。直近2週で評価し、Geminiのラベルとの一致と、人が訂正・確認した画像での
  当たり方(川霧の適合率・再現率。同じ画像のGeminiの元の判定とも比べる)を data/experiments/own_judge_check.json に書く。
- 判定(--run、毎時): 新しい画像の特徴量を計算して data/own_judge.db に保存し、判定(各まとまりの確率)を付ける。
  過去分の特徴量も少しずつ埋める(--limit)。地点ページの「もう一つの判定」に出す。
"""
import argparse
import csv
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from functools import lru_cache

import numpy as np
from PIL import Image

import label_verify as lv
from common import BASE_DIR, CAMERAS_CSV, JST

DB = BASE_DIR / "data" / "own_judge.db"
MODEL_PATH = BASE_DIR / "data" / "models" / "own_judge.joblib"
CHECK_PATH = BASE_DIR / "data" / "experiments" / "own_judge_check.json"
FROM = "2026-09-01"
GROUPS = ["現象なし", "川霧", "全体霧", "層雲", "雨"]
WIDTH = 240
GRID = 3
SCHEMA = """
CREATE TABLE IF NOT EXISTS feats (
    datetime TEXT NOT NULL, station TEXT NOT NULL, ref_datetime TEXT, vec TEXT NOT NULL,
    PRIMARY KEY (datetime, station)
);
CREATE TABLE IF NOT EXISTS judge (
    datetime TEXT NOT NULL, station TEXT NOT NULL, pred INTEGER NOT NULL, probs TEXT NOT NULL, model_at TEXT NOT NULL,
    PRIMARY KEY (datetime, station)
);
"""
log = logging.getLogger("own_judge")


def connect(ro=False):
    if ro:
        return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode = WAL")
    c.executescript(SCHEMA)
    return c


@lru_cache(maxsize=256)
def _image_stats(path):
    with Image.open(path) as im:
        im = im.convert("RGB")
        h = max(1, round(im.height * WIDTH / im.width))
        im = im.resize((WIDTH, h), Image.BILINEAR).crop((0, int(h * 0.08), WIDTH, int(h * 0.95)))
        rgb = np.asarray(im, dtype=np.float32) / 255.0
    lum = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    mx, mn = rgb.max(axis=2), rgb.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    lap = np.zeros_like(lum)
    lap[1:-1, 1:-1] = np.abs(4 * lum[1:-1, 1:-1] - lum[:-2, 1:-1] - lum[2:, 1:-1] - lum[1:-1, :-2] - lum[1:-1, 2:])
    white = (lum > 0.6) & (sat < 0.12)
    H, W = lum.shape
    cells = []
    for i in range(GRID):
        for j in range(GRID):
            sl = (slice(i * H // GRID, (i + 1) * H // GRID), slice(j * W // GRID, (j + 1) * W // GRID))
            cells.append((float(lap[sl].mean()), float(lum[sl].mean()), float(white[sl].mean())))
    return {"cells": cells, "sat": float(sat.mean()), "dark": float(mn.mean()), "lum_std": float(lum.std())}


def features(station, dt, ref):
    a, b = _image_stats(str(lv.image_path(station, dt))), _image_stats(str(lv.image_path(station, ref)))
    eps = 1e-6
    vec = []
    for (ea, la, wa), (eb, lb, wb) in zip(a["cells"], b["cells"]):
        vec += [ea / (eb + eps), la - lb, wa - wb]
    vec += [a["sat"] / (b["sat"] + eps), a["dark"] - b["dark"], a["lum_std"] / (b["lum_std"] + eps), a["sat"], a["dark"]]
    return [round(v, 5) for v in vec]


def _group(code):
    if code is None or code < 0:
        return None
    c = code // 100 if code > 10 else code
    return 0 if c == 0 else (1 if c in (1, 2, 3) else (2 if c in (4, 5, 6) else (3 if c in (7, 8) else 4)))


def compute_features(limit):
    """まだ特徴量の無い日中の画像を、新しい順にlimit枚まで。"""
    from fog_features import is_daylight
    coords = {}
    with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("緯度"):
                coords[r["地点名"]] = (float(r["緯度"]), float(r["経度"]))
    conn = connect()
    done = {(s, d) for s, d in conn.execute("SELECT station, datetime FROM feats")}
    todo = []
    for date in sorted(os.listdir(lv.IMAGE_DIR), reverse=True):
        if date < FROM or len(date) != 10:
            continue
        for hhmm in sorted(os.listdir(lv.IMAGE_DIR / date), reverse=True):
            dt = f"{date} {hhmm[:2]}:{hhmm[2:]}:00"
            for fn in os.listdir(lv.IMAGE_DIR / date / hhmm):
                st = fn[:-4]
                if not fn.endswith(".jpg") or fn.endswith("_unavailable.jpg") or st not in coords or (st, dt) in done:
                    continue
                if is_daylight(dt, *coords[st]):
                    todo.append((st, dt))
    refs = lv.load_reference_candidates()
    out, n = [], 0
    for st, dt in todo[:limit]:
        ref = lv.pick_reference(refs, st, dt)
        if not ref:
            continue
        try:
            out.append((dt, st, ref, json.dumps(features(st, dt, ref))))
        except Exception as e:  # noqa: BLE001
            log.warning("features %s %s: %s", st, dt, e)
        if len(out) >= 500:
            with conn:
                conn.executemany("INSERT OR REPLACE INTO feats VALUES (?, ?, ?, ?)", out)
            n += len(out); out = []
    with conn:
        conn.executemany("INSERT OR REPLACE INTO feats VALUES (?, ?, ?, ?)", out)
    n += len(out)
    conn.close()
    log.info("features computed for %d of %d pending image(s)", n, len(todo))


def _dataset():
    import pandas as pd
    c = connect(ro=True)
    f = pd.read_sql("SELECT datetime, station, vec FROM feats", c)
    c.close()
    w = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'weather.db'}?mode=ro", uri=True)
    ph = pd.read_sql("SELECT datetime, station_name AS station, code FROM phenomena WHERE datetime >= ?", w, params=(FROM,))
    w.close()
    u = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'users.db'}?mode=ro", uri=True)
    corr = pd.read_sql("SELECT target_datetime AS datetime, station_name AS station, original_code, corrected_code, source "
                       "FROM phenomena_corrections", u)
    conf = pd.read_sql("SELECT target_datetime AS datetime, station_name AS station, reported_code FROM observation_reports "
                       "WHERE status = 'dismissed'", u)
    u.close()
    d = f.merge(ph, on=["datetime", "station"], how="left").merge(corr, on=["datetime", "station"], how="left")
    d["human"] = d["source"] == "human"
    d["label_code"] = d["corrected_code"].where(d["human"], d["code"])
    d["y"] = d["label_code"].map(lambda v: _group(int(v)) if v == v and v is not None else None)
    d = d[d["y"].notna()].copy()
    d["y"] = d["y"].astype(int)
    X = np.array([json.loads(v) for v in d["vec"]], dtype=np.float32)
    hours = d["datetime"].str[11:13].astype(int).to_numpy()
    X = np.hstack([X, np.sin(2 * np.pi * hours / 24)[:, None], np.cos(2 * np.pi * hours / 24)[:, None]])
    d["confirmed"] = list(zip(d["datetime"], d["station"]))
    confirmed = set(zip(conf["datetime"], conf["station"]))
    d["confirmed"] = d["confirmed"].map(lambda k: k in confirmed)
    return d.reset_index(drop=True), X


def _rates(d, mask):
    r = d[mask].groupby("station")["y"].apply(lambda s: (s == 1).mean())
    return r.to_dict(), float((d.loc[mask, "y"] == 1).mean())


def _with_rate(X, d, rates, g):
    return np.hstack([X, d["station"].map(rates).fillna(g).to_numpy()[:, None]])


def _fit(X, y, w):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(objective="multiclass", n_estimators=400, learning_rate=0.05, num_leaves=31, min_child_samples=30,
                           subsample=0.8, subsample_freq=1, colsample_bytree=0.8, class_weight="balanced", verbose=-1)
    m.fit(X, y, sample_weight=w)
    return m


def train():
    import joblib
    import pandas as pd
    d, X = _dataset()
    end = pd.to_datetime(d["datetime"]).max()
    test_mask = (pd.to_datetime(d["datetime"]) > end - timedelta(days=14)).to_numpy()
    tr = ~test_mask
    w = np.where(d["human"], 5.0, 1.0)
    rates, g = _rates(d, tr)
    m = _fit(_with_rate(X[tr], d[tr], rates, g), d["y"][tr], w[tr])
    proba = m.predict_proba(_with_rate(X, d, rates, g))
    pred = proba.argmax(axis=1)
    res = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "n_images": int(len(d)),
           "n_train": int(tr.sum()), "n_test": int(test_mask.sum())}
    yt, pt = d["y"].to_numpy()[test_mask], pred[test_mask]
    res["test_vs_gemini_labels"] = {"agreement": round(float((yt == pt).mean()), 3),
                                    "river": _prf(pt == 1, yt == 1), "any_fog": _prf(np.isin(pt, [1, 2]), np.isin(yt, [1, 2]))}
    # 人が訂正した画像(学習に使っていない直近2週の分だけでなく、全期間を別のモデルで: 各訂正画像を除いて学習するのは重いので、
    # 直近2週より前で学習したモデルで直近2週の人の訂正を、という形は件数が少ない。ここでは学習に使った訂正も含めた参考値として、
    # Geminiの元の判定と同じ画像で比べる)
    h = d["human"].to_numpy() & test_mask
    if h.sum():
        orig = d["original_code"].map(lambda v: _group(int(v)) if v == v and v is not None else -1).to_numpy()
        res["test_human_corrected"] = {"n": int(h.sum()),
                                       "own_model_river": _prf(pred[h] == 1, d["y"].to_numpy()[h] == 1),
                                       "gemini_original_river": _prf(orig[h] == 1, d["y"].to_numpy()[h] == 1)}
    CHECK_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    rates2, g2 = _rates(d, np.ones(len(d), dtype=bool))
    final = _fit(_with_rate(X, d, rates2, g2), d["y"], w)
    joblib.dump({"model": final, "rates": rates2, "global": g2, "trained_at": res["generated_at"]}, MODEL_PATH)
    log.info("own judge: %s", json.dumps(res, ensure_ascii=False))
    return res


def _prf(pred, truth):
    tp, fp, fn = int((pred & truth).sum()), int((pred & ~truth).sum()), int((~pred & truth).sum())
    return {"precision": round(tp / (tp + fp), 3) if tp + fp else None, "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "n_true": int(truth.sum())}


def judge_recent(hours=6):
    """特徴量のある直近の画像に判定を付ける。"""
    import joblib
    import pandas as pd
    if not MODEL_PATH.exists():
        return
    st = joblib.load(MODEL_PATH)
    since = (datetime.now(JST) - pd.Timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    c = connect()
    rows = c.execute("SELECT datetime, station, vec FROM feats WHERE datetime >= ?", (since,)).fetchall()
    if not rows:
        c.close(); return
    d = pd.DataFrame(rows, columns=["datetime", "station", "vec"])
    X = np.array([json.loads(v) for v in d["vec"]], dtype=np.float32)
    hrs = d["datetime"].str[11:13].astype(int).to_numpy()
    X = np.hstack([X, np.sin(2 * np.pi * hrs / 24)[:, None], np.cos(2 * np.pi * hrs / 24)[:, None]])
    p = st["model"].predict_proba(_with_rate(X, d, st["rates"], st["global"]))
    with c:
        c.executemany("INSERT OR REPLACE INTO judge VALUES (?, ?, ?, ?, ?)",
                      [(dt, s, int(pr.argmax()), json.dumps([round(float(x), 3) for x in pr]), st["trained_at"])
                       for dt, s, pr in zip(d["datetime"], d["station"], p)])
    c.close()
    log.info("judged %d image(s)", len(d))


def latest_for(station):
    """地点ページ用: 直近3時間以内の判定 {"datetime", "label", "prob"}。"""
    if not DB.exists():
        return None
    c = connect(ro=True)
    try:
        r = c.execute("SELECT datetime, pred, probs FROM judge WHERE station = ? ORDER BY datetime DESC LIMIT 1", (station,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        c.close()
    if not r:
        return None
    if datetime.now(JST).replace(tzinfo=None) - datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") > timedelta(hours=3):
        return None
    probs = json.loads(r[2])
    return {"datetime": r[0], "label": GROUPS[r[1]], "prob": probs[r[1]], "probs": dict(zip(GROUPS, probs))}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--limit", type=int, default=300)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.FileHandler(BASE_DIR / "logs" / "own_judge.log", encoding="utf-8"), logging.StreamHandler()])
    try:
        os.nice(10)
    except OSError:
        pass
    if args.run:
        compute_features(args.limit)
        judge_recent()
    if args.train:
        train()
