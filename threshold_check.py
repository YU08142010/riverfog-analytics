"""見逃しを減らすしきい値の比較(2026-10-13、本番は変えない)。

いまのしきい値は「検証期間でF1が最大」になる値で、空振りを減らす代わりに見逃し(再現率 約0.15)が多い。
用途によっては見逃しを重く見たいので、次の2つを data/experiments/threshold_check.json に書く。
1) 評価期間(fog_modelの学習で取っておいた期間、各地点の y_test / p_test)で、しきい値ごとの適合率・再現率・F1・F2・空振り・見逃し。
   どのしきい値でどうなるかの一覧(ここで選ぶと評価期間を見て選ぶことになるので、成績の主張には使わない)。
2) 公平な比べ方: 前日夜の本番の予報(再予測は除く)の、前の期間でしきい値を決め(F1最大 / F2最大)、直近 TEST_DAYS 日で採点する。
F2 = 再現率を適合率の2倍重く見る指標(見逃しを空振りより重く見る)。

実行: .venv/bin/python threshold_check.py
"""
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta

import numpy as np

import fog_model
import prediction_log
from common import BASE_DIR, JST

OUT = BASE_DIR / "data" / "experiments" / "threshold_check.json"
TEST_DAYS = 7
GRID = [round(x, 2) for x in np.arange(0.03, 0.61, 0.01)]
log = logging.getLogger("threshold_check")


def score(y, p, thr):
    pred = p >= thr
    tp, fp, fn = int((pred & (y == 1)).sum()), int((pred & (y == 0)).sum()), int((~pred & (y == 1)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    f2 = 5 * prec * rec / (4 * prec + rec) if prec + rec else 0.0
    return {"threshold": thr, "precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3), "f2": round(f2, 3),
            "hit": tp, "false_alarm": fp, "miss": fn}


def best(y, p, key):
    return max((score(y, p, t) for t in GRID), key=lambda s: (s[key], -s["threshold"]))


def main():
    st = fog_model.load_model_state()
    ys, ps = [], []
    for info in st.values():
        if info.get("y_test") is not None and info.get("p_test") is not None and len(info["y_test"]) == len(info["p_test"]):
            ys.append(np.asarray(info["y_test"], int)); ps.append(np.asarray(info["p_test"], float))
    y, p = np.concatenate(ys), np.concatenate(ps)
    gm = fog_model.shared_global_metrics(st) or {}
    cur = gm.get("threshold")
    table = [score(y, p, t) for t in (0.05, 0.08, 0.1, 0.12, 0.15, 0.19, 0.25, 0.3, 0.4)]
    res = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
           "eval_window": {"from": str(gm.get("test_start"))[:10], "to": str(gm.get("test_end"))[:10], "n": int(len(y)), "fog": int(y.sum()),
                           "current_threshold": cur, "current": score(y, p, cur) if cur else None, "table": table}}

    # 2) 前日夜の本番の予報で、前の期間で決めて直近で採点
    c = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'predictions.db'}?mode=ro", uri=True)
    try:
        rows = c.execute(
            f"""WITH r AS (SELECT target_datetime, predicted_fog_prob AS p, {prediction_log._actual_fog_sql()} AS y,
                           ROW_NUMBER() OVER (PARTITION BY station, target_datetime ORDER BY run_at DESC) AS k
                    FROM predictions WHERE actual_code IS NOT NULL AND predicted_fog_prob IS NOT NULL AND COALESCE(daylight, 1) = 1
                      AND model_type LIKE ? AND model_type NOT LIKE '%[再予測]'
                      AND run_at < substr(target_datetime, 1, 10) || ' 00:00:00')
                SELECT target_datetime, p, y FROM r WHERE k = 1""", (prediction_log.CURRENT_MODEL_LIKE,)).fetchall()
    finally:
        c.close()
    if rows:
        end = max(r[0] for r in rows)[:10]
        cut = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=TEST_DAYS - 1)).strftime("%Y-%m-%d")
        tr = [(pp, yy) for d, pp, yy in rows if d[:10] < cut]
        te = [(pp, yy) for d, pp, yy in rows if d[:10] >= cut]
        if tr and te and sum(yy for _, yy in tr) >= 10:
            ptr, ytr = np.array([a for a, _ in tr]), np.array([b for _, b in tr], int)
            pte, yte = np.array([a for a, _ in te]), np.array([b for _, b in te], int)
            t_f1, t_f2 = best(ytr, ptr, "f1")["threshold"], best(ytr, ptr, "f2")["threshold"]
            res["production_holdout"] = {"choose_until": cut, "test_from": cut, "test_to": end, "test_n": int(len(yte)), "test_fog": int(yte.sum()),
                                         "f1_rule": score(yte, pte, t_f1), "f2_rule": score(yte, pte, t_f2)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    r = main()
    print(json.dumps(r, ensure_ascii=False, indent=1))
