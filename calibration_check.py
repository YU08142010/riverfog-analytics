"""予測の確率の補正を試す(2026-10-13、本番は変えない)。

分析ページの「確率の正直さ」で、40%以上と出した予報が実際にはほとんど当たっていない(言いすぎ)ことが分かったため、
確率を実際の割合に合わせる補正を、過去のデータで補正あり/なしで比べる。
- 補正の学習: 評価期間より前の、いまのモデルの予測(再予測=過去の日をいまのモデルで予測し直したものも含める)と、実際の霧。
- 評価: 直近 TEST_DAYS 日の「前日夜の本番の予測」(再予測は含めない)。
- 補正の方法: Platt(確率のlogitに対するロジスティック回帰、2つの数だけ)と、isotonic(段々の単調な対応表)。
  補正は確率の大小の順を変えないので、AP(霧を上位に並べる力)はほぼ変わらない。見るのは Brier・logloss と、高い確率の区切りの当たり方。
結果は data/experiments/calibration_check.json。

実行: .venv/bin/python calibration_check.py
"""
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss

import prediction_log
from common import BASE_DIR, JST

DB = BASE_DIR / "data" / "predictions.db"
OUT = BASE_DIR / "data" / "experiments" / "calibration_check.json"
TEST_DAYS = 7
EDGES = (0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 1.01)
log = logging.getLogger("calibration_check")


def load():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = c.execute(
            f"""WITH r AS (
                    SELECT station, target_datetime, predicted_fog_prob AS p, {prediction_log._actual_fog_sql()} AS y,
                           model_type LIKE '%[再予測]' AS hind, run_at < substr(target_datetime, 1, 10) || ' 00:00:00' AS prev_eve,
                           ROW_NUMBER() OVER (PARTITION BY station, target_datetime,
                               (model_type LIKE '%[再予測]') ORDER BY run_at DESC) AS k
                    FROM predictions
                    WHERE actual_code IS NOT NULL AND predicted_fog_prob IS NOT NULL AND COALESCE(daylight, 1) = 1
                      AND model_type LIKE ?)
                SELECT target_datetime, p, y, hind, prev_eve FROM r WHERE k = 1""",
            (prediction_log.CURRENT_MODEL_LIKE,)).fetchall()
    finally:
        c.close()
    return rows


def bins(p, y):
    out = []
    for lo, hi in zip(EDGES, EDGES[1:]):
        m = (p >= lo) & (p < hi)
        if m.sum():
            out.append({"range": f"{lo:.2f}-{min(hi, 1):.2f}", "n": int(m.sum()), "mean_prob": round(float(p[m].mean()), 4),
                        "observed": round(float(y[m].mean()), 4)})
    return out


def scores(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return {"brier": round(float(brier_score_loss(y, p)), 5), "logloss": round(float(log_loss(y, p, labels=[0, 1])), 5),
            "ap": round(float(average_precision_score(y, p)), 4) if y.sum() else None, "mean_prob": round(float(p.mean()), 4),
            "observed": round(float(y.mean()), 4), "reliability": bins(p, y)}


def main():
    rows = load()
    end = max(r[0] for r in rows)[:10]
    test_start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=TEST_DAYS - 1)).strftime("%Y-%m-%d")
    train = [r for r in rows if r[0][:10] < test_start]
    test = [r for r in rows if r[0][:10] >= test_start and r[4] and not r[3]]
    if not train or not test:
        log.error("Not enough data (train %d, test %d)", len(train), len(test))
        return None
    pt, yt = np.array([r[1] for r in train], float), np.array([r[2] for r in train], int)
    pe, ye = np.array([r[1] for r in test], float), np.array([r[2] for r in test], int)
    logit = lambda p: np.log(np.clip(p, 1e-4, 1 - 1e-4) / (1 - np.clip(p, 1e-4, 1 - 1e-4)))  # noqa: E731
    platt = LogisticRegression(C=1e6).fit(logit(pt)[:, None], yt)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(pt, yt)
    res = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "train_rows": len(train), "train_fog": int(yt.sum()),
           "train_until": test_start, "test_rows": len(test), "test_fog": int(ye.sum()), "test_from": test_start, "test_to": end,
           "platt": {"a": round(float(platt.coef_[0][0]), 4), "b": round(float(platt.intercept_[0]), 4)},
           "raw": scores(pe, ye), "platt_scores": scores(platt.predict_proba(logit(pe)[:, None])[:, 1], ye),
           "isotonic_scores": scores(iso.predict(pe), ye)}
    best = min(("raw", "platt_scores", "isotonic_scores"), key=lambda k: res[k]["brier"])
    res["best_by_brier"] = best
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("calibration: raw %s / platt %s / isotonic %s -> best %s", res["raw"]["brier"], res["platt_scores"]["brier"],
             res["isotonic_scores"]["brier"], best)
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    r = main()
    if r:
        for k in ("raw", "platt_scores", "isotonic_scores"):
            s = r[k]
            print(k, {x: s[x] for x in ("brier", "logloss", "ap", "mean_prob", "observed")})
            for b in s["reliability"]:
                print("   ", b)
        print("train", r["train_rows"], r["train_fog"], "test", r["test_rows"], r["test_fog"], r["test_from"], r["test_to"], "platt", r["platt"])
