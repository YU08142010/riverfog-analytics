"""季節の変わり目の見張り(2026-10-12、crontab 毎週月曜07:30)。

これまでのデータは2026年の夏〜秋(放射霧が中心)だけで、冬は「けあらし」(冷え込んだ朝に川面から立つ湯気のような霧)など
性質の違う霧になる。モデルの当たり方が落ちても毎晩の再学習では気づきにくいので、前日夜の本番の予測(再予測は除く、日中だけ)を
週ごとに採点し、学習時の評価期間の成績(fog_modelのglobal_metrics)と比べる。
- 精度の低下: 直近2週の川霧F1が、学習時の評価期間のF1のDROP_RATIO倍を下回った
- 確率のずれ: 直近2週の平均の予測確率と、実際に霧だった割合の比が CALIB_RATIO 倍以上 / 分の1以下
どちらも直近2週に霧がMIN_FOG件以上あるときだけ判定する(少ないと偶然で上下するため)。
結果は data/health/drift.json。当てはまれば管理者に通知し、/admin/health に出す。

実行: .venv/bin/python drift_watch.py [--quiet]
"""
import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta

from common import BASE_DIR, JST

OUT = BASE_DIR / "data" / "health" / "drift.json"
DB = BASE_DIR / "data" / "predictions.db"
WEEKS = 8
DROP_RATIO = 0.6
CALIB_RATIO = 2.0
MIN_FOG = 15
log = logging.getLogger("drift_watch")


def weekly(conn, start):
    import prediction_log
    rows = conn.execute(
        f"""WITH r AS (
                SELECT station, target_datetime, predicted_code AS pc, predicted_fog_prob AS p, {prediction_log._actual_fog_sql()} AS y,
                       ROW_NUMBER() OVER (PARTITION BY station, target_datetime ORDER BY run_at DESC) AS k
                FROM predictions
                WHERE actual_code IS NOT NULL AND COALESCE(daylight, 1) = 1 AND model_type LIKE ?
                  AND model_type NOT LIKE '%[再予測]' AND run_at < substr(target_datetime, 1, 10) || ' 00:00:00'
                  AND target_datetime >= ?)
            SELECT target_datetime, pc, p, y FROM r WHERE k = 1""",
        (prediction_log.CURRENT_MODEL_LIKE, start),
    ).fetchall()
    return rows


def score(rows):
    n = len(rows)
    tp = sum(1 for _, pc, _, y in rows if pc == 1 and y)
    fp = sum(1 for _, pc, _, y in rows if pc == 1 and not y)
    fn = sum(1 for _, pc, _, y in rows if pc != 1 and y)
    fog = tp + fn
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / fog if fog else None
    f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
    probs = [p for _, _, p, _ in rows if p is not None]
    return {"n": n, "fog": fog, "hit": tp, "false_alarm": fp, "miss": fn,
            "precision": round(prec, 3) if prec is not None else None, "recall": round(rec, 3) if rec is not None else None,
            "f1": round(f1, 3), "mean_prob": round(sum(probs) / len(probs), 4) if probs else None,
            "observed": round(fog / n, 4) if n else None}


def main(notify=True):
    import fog_model
    now = datetime.now(JST).replace(tzinfo=None)
    today = now.date()
    monday = today - timedelta(days=today.weekday())  # 今週の月曜(今週分は途中なので数えない)
    start = monday - timedelta(weeks=WEEKS)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = weekly(conn, start.strftime("%Y-%m-%d 00:00:00"))
    finally:
        conn.close()
    weeks = []
    for i in range(WEEKS):
        a, b = start + timedelta(weeks=i), start + timedelta(weeks=i + 1)
        sel = [r for r in rows if str(a) <= r[0][:10] < str(b)]
        if sel:
            weeks.append(dict(score(sel), start=str(a), end=str(b - timedelta(days=1))))
    recent = [r for r in rows if str(monday - timedelta(weeks=2)) <= r[0][:10] < str(monday)]
    rec = score(recent) if recent else None
    # 比べる基準: 同じ前日夜の予測の、直近2週より前(最大6週)の成績。霧が少なすぎるときだけ学習時の評価期間のF1を使う
    # (学習時の評価期間はしきい値の決め方が違い、本番より低めに出るため、まず本番どうしで比べる)。
    older = [r for r in rows if r[0][:10] < str(monday - timedelta(weeks=2))]
    base = score(older) if older else None
    if base and base["fog"] >= MIN_FOG:
        base_f1, base_src = base["f1"], f"本番の予測の{start}〜{monday - timedelta(weeks=2, days=1)}"
    else:
        gm = fog_model.shared_global_metrics(fog_model.load_model_state()) or {}
        base_f1, base_src = (gm.get("model") or {}).get("f1"), "学習時の評価期間"
    alerts = []
    if rec and rec["fog"] >= MIN_FOG:
        if base_f1 and rec["f1"] < DROP_RATIO * base_f1:
            alerts.append({"kind": "f1", "message": f"直近2週の前日夜の予測の川霧F1が{rec['f1']:.2f}で、それより前({base_f1:.2f})より"
                                                    f"大きく下がっています。季節が変わって霧の性質が変わった可能性があります。"})
        if rec["mean_prob"] and rec["observed"]:
            ratio = rec["mean_prob"] / rec["observed"]
            if ratio >= CALIB_RATIO or ratio <= 1 / CALIB_RATIO:
                alerts.append({"kind": "calibration", "message": f"直近2週の予測の確率の平均({rec['mean_prob'] * 100:.1f}%)と実際に霧だった割合"
                                                                 f"({rec['observed'] * 100:.1f}%)が{ratio:.1f}倍ずれています"
                                                                 f"({'出ると言いすぎ' if ratio > 1 else '出ないと言いすぎ'})。"})
    res = {"checked_at": now.strftime("%Y-%m-%d %H:%M:%S"), "weeks": weeks, "recent_2w": rec, "baseline_f1": base_f1, "baseline_source": base_src,
           "min_fog": MIN_FOG, "alerts": alerts}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("drift watch: recent=%s baseline_f1=%s alerts=%d", rec, base_f1, len(alerts))
    if notify and alerts:
        try:
            import push_notify
            import users
            from common import now_jst_naive
            db = BASE_DIR / "data" / "users.db"
            push_notify.init(db, now_jst_naive)
            c = users.connect(db)
            admins = [u["id"] for u in users.list_users(c) if u["role"] == "admin"]
            c.close()
            for uid in admins:
                push_notify.deliver(uid, "admin", "予測の当たり方が変わってきています", alerts[0]["message"], "/admin/health",
                                    "drift-watch", key=f"drift:{today}", ttl=3 * 86400)
        except Exception as e:  # noqa: BLE001
            log.warning("Notification failed: %s", e)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    main(notify=not args.quiet)
