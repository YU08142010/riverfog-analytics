"""会津だけ予測の対象を「霧(川霧+全体霧)」に広げた場合の比較(2026-10-11)。本番のモデルは書き換えない。

同じデータで、毎晩の学習(fog_model.train_shared_model)を2回動かす:
  A: いま(全地域、川霧1〜3が対象)
  B: 会津だけ全体霧(4〜6)も対象(fog_features.REGION_TARGET_ENABLED=True)
評価期間(直近3週、予報入力)の地域ごとの AP・F1・適合率・再現率と、対象の霧の割合(気候値のAPの目安)を
data/experiments/aizu_target_check.json に書く。只見川はどちらも同じ対象なので、只見川の成績が
悪くならないこと(共通のモデルなので、会津の正解が変わると影響し得る)も確かめる。
"""
import json
import logging
import sys
from datetime import datetime

import numpy as np
from sklearn.metrics import average_precision_score

import fog_data
import fog_features
import fog_model
import station_weather
from common import BASE_DIR, JST, STATION_WEATHER_DB_PATH, load_camera_coords, load_camera_regions, representative_coords

log = logging.getLogger("aizu_target_check")


def region_metrics(models, regions):
    out = {}
    by = {}
    thr = None
    for st, info in models.items():
        y, p = np.asarray(info.get("y_test") if info.get("y_test") is not None else []), np.asarray(info.get("p_test") if info.get("p_test") is not None else [])
        if not len(y):
            continue
        thr = info.get("fog_threshold", thr)
        r = regions.get(st, "その他")
        by.setdefault(r, [[], []])
        by[r][0].append(y); by[r][1].append(p)
    for r, (ys, ps) in by.items():
        y, p = np.concatenate(ys).astype(int), np.concatenate(ps)
        pred = p >= thr
        tp, fp, fn = int((pred & (y == 1)).sum()), int((pred & (y == 0)).sum()), int((~pred & (y == 1)).sum())
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        out[r] = {"n": int(len(y)), "positives": int(y.sum()), "base_rate": round(float(y.mean()), 4),
                  "ap": round(float(average_precision_score(y, p)), 4) if y.sum() else None,
                  "precision": round(prec, 3) if prec is not None else None, "recall": round(rec, 3) if rec is not None else None,
                  "f1": round(2 * prec * rec / (prec + rec), 3) if prec and rec else None}
    return out, thr


def region_metrics_split(models, regions):
    """地域ごとに別のモデル(しきい値も別)のとき。"""
    out, thrs = {}, {}
    for r in ("只見川", "会津"):
        sub = {s: i for s, i in models.items() if regions.get(s) == r}
        m, thr = region_metrics(sub, regions)
        out.update(m)
        thrs[r] = thr
    return out, thrs


def main():
    main_df, phenom_df, names = fog_data.load_river_fog_data()
    lat, lon = representative_coords()
    coords = load_camera_coords()
    regions = load_camera_regions()
    prev = fog_model.load_model_state()
    res = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")}
    variants = [("A_river_only", False, None), ("B_aizu_all_fog", True, None), ("C_separate_models", True, "region")]
    only = sys.argv[1:]  # 名前を渡すとその案だけ
    for name, enabled, split in variants:
        if only and name not in only:
            continue
        fog_features.REGION_TARGET_ENABLED = enabled
        models = {}
        groups = ([[n for n in names if regions.get(n) == r] for r in ("只見川", "会津")] if split else [names])
        vs_clim = {}
        for cols in groups:
            fog_model._FRAME_CACHE["inputs"] = None  # 正解・地点が変わるので、作り直させる
            wconn = station_weather.connect(STATION_WEATHER_DB_PATH)
            try:
                part = fog_model.train_shared_model(main_df, phenom_df, cols, lat, lon, camera_coords=coords,
                                                    weather_conn=wconn, prev_state=prev)
            finally:
                wconn.close()
            models.update(part)
            gm = fog_model.shared_global_metrics(part) or {}
            key = regions.get(cols[0], "全体") if split else "全体"
            vs_clim[key] = {"model_ap": (gm.get("model") or {}).get("ap"), "climatology_ap": (gm.get("climatology") or {}).get("ap"),
                            "model_brier": (gm.get("model") or {}).get("brier"),
                            "climatology_brier": (gm.get("climatology") or {}).get("brier")}
        by_region, thr = region_metrics_split(models, regions) if split else region_metrics(models, regions)
        gm = fog_model.shared_global_metrics(models) or {}
        res[name] = {"threshold": thr, "overall": {k: gm.get("model", {}).get(k) for k in ("ap", "f1", "precision", "recall", "brier")},
                     "by_region": by_region, "vs_climatology": vs_clim}
        log.info("%s: %s", name, json.dumps(res[name], ensure_ascii=False))
    fog_features.REGION_TARGET_ENABLED = False
    out = BASE_DIR / "data" / "experiments" / "aizu_target_check.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    # 毎月の比べ直し: 会津の別モデルが過去の発生率を上回ったら、管理者に知らせる(有効にするかは人が決める)
    aizu = ((res.get("C_separate_models") or {}).get("vs_climatology") or {}).get("会津")
    if aizu and aizu.get("model_ap") is not None and aizu.get("climatology_ap") is not None:
        ready = aizu["model_ap"] > aizu["climatology_ap"] and (aizu.get("model_brier") or 1) <= (aizu.get("climatology_brier") or 0)
        try:
            import push_notify
            import users
            from common import now_jst_naive
            db = BASE_DIR / "data" / "users.db"
            push_notify.init(db, now_jst_naive)
            conn = users.connect(db)
            admins = [u["id"] for u in users.list_users(conn) if u["role"] == "admin"]
            conn.close()
            body = (f"会津の霧のモデル AP {aizu['model_ap']:.3f} / 過去の発生率 AP {aizu['climatology_ap']:.3f}。"
                    + ("過去の発生率を上回りました。fog_features.REGION_TARGET_ENABLEDを有効にできます。" if ready
                       else "まだ過去の発生率に及ばないため、会津は川霧の予測のままです。"))
            for uid in admins:
                push_notify.deliver(uid, "admin", "会津の予測の対象の比べ直し", body, "/admin", "aizu-check",
                                    key=f"aizu_check:{res['generated_at'][:7]}", ttl=3 * 86400)
        except Exception as e:  # noqa: BLE001
            log.warning("Notification failed: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    main()
