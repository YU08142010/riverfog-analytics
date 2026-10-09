"""AIの二重判定(label_verify.py)が、人の確認結果とどれだけ合うかを測る(読み取り専用の実験)。

対象: Geminiが霧(川霧1〜3・全体霧4〜6)と判定し、人が確認した画像
  - 人が訂正したもの(phenomena_correctionsのsource=human、original_codeが霧)
  - 人が「問題なし」としたもの(observation_reportsのdismissed、reported_codeが霧)
各画像を「基準画像あり」と「基準画像なし」の2通りで判定し、人の結論(川霧か・霧か)と比べる。
途中で止まっても再実行で続きから(data/experiments/label_verify_check.progress.json)。
結果: data/experiments/label_verify_check.json

実行: nohup .venv/bin/python label_verify_check.py &
"""
import json
import logging
import os
import sqlite3
import sys
import time

import pandas as pd

import label_verify as lv
from common import BASE_DIR

EXP = BASE_DIR / "data" / "experiments"
PROGRESS = EXP / "label_verify_check.progress.json"
OUT = EXP / "label_verify_check.json"
MODEL = os.environ.get("VERIFY_MODEL", "gemini-3.5-flash-lite")
CALL_INTERVAL_SEC = 4.5
log = logging.getLogger("label_verify_check")


def prim(v):
    v = float(v)
    return int(v // 100) if v > 10 else int(v)


def grp(c):
    return 0 if c == 0 else (1 if c in (1, 2, 3) else (2 if c in (4, 5, 6) else (3 if c in (7, 8) else 4)))


def test_set():
    u = sqlite3.connect(f"file:{lv.USERS_DB}?mode=ro", uri=True)
    corr = pd.read_sql("SELECT station_name, target_datetime, original_code, corrected_code FROM phenomena_corrections "
                       "WHERE source = 'human' AND original_code IS NOT NULL", u)
    rep = pd.read_sql("SELECT station_name, target_datetime, reported_code FROM observation_reports WHERE status = 'dismissed'", u)
    u.close()
    items = {}
    for r in corr.itertuples():
        og, ng = grp(prim(r.original_code)), grp(prim(r.corrected_code))
        if og in (1, 2):
            items[(r.station_name, r.target_datetime)] = {"first": og, "human": ng, "kind": "corrected"}
    for r in rep.itertuples():
        og = grp(prim(r.reported_code))
        if og in (1, 2) and (r.station_name, r.target_datetime) not in items:
            items[(r.station_name, r.target_datetime)] = {"first": og, "human": og, "kind": "confirmed"}
    return items


def _load_all_progress():
    """並列に走らせた分(progress.<n>.json)も含めて、判定済みの結果をまとめる。"""
    done = {}
    for p in sorted(EXP.glob("label_verify_check.progress*.json")):
        try:
            done.update(json.loads(p.read_text()))
        except ValueError:
            pass
    return done


def main(shard=None, n_shards=1):
    """shard/n_shards: 対象を分けて複数プロセスで並列に判定する(結果は別のprogressファイルに書く)。"""
    global PROGRESS
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if shard is not None:
        PROGRESS = EXP / f"label_verify_check.progress.{shard}.json"
    key = lv.experiment_api_key()
    items = test_set()
    refs = lv.load_reference_candidates()
    already = _load_all_progress()
    done = json.loads(PROGRESS.read_text()) if PROGRESS.exists() else {}
    log.info("test items: %d (done %d), model %s, shard %s/%s", len(items), len(already), MODEL, shard, n_shards)
    try:
        for i, ((st, dt), it) in enumerate(sorted(items.items(), key=lambda kv: kv[0][1])):
            k = f"{st}|{dt}"
            if shard is not None and i % n_shards != shard:
                continue
            if k in already and "ref" in already[k] and "noref" in already[k]:
                continue
            if not lv.image_path(st, dt).exists():
                continue
            ref = lv.pick_reference(refs, st, dt)
            rec = dict(already.get(k) or done.get(k) or {**it, "ref_dt": ref})
            for mode in ("ref", "noref"):
                if mode in rec or (mode == "ref" and not ref):
                    continue
                v = lv.verify_one(st, dt, ref, MODEL, key, with_reference=(mode == "ref"))
                time.sleep(CALL_INTERVAL_SEC)
                if v is not None:
                    rec[mode] = {"verdict": v.verdict, "confidence": v.confidence, "reasoning": v.reasoning[:200]}
            done[k] = rec
            PROGRESS.write_text(json.dumps(done, ensure_ascii=False))
    except lv.QuotaExhausted as e:
        log.warning("Quota exhausted, stopping (re-run later to continue): %s", e)
    summarize(_load_all_progress())


def _daylight_keys(keys):
    """日の出〜日の入りの画像だけ(学習・評価に使うのはこの時間帯だけなので、集計もそろえる)。"""
    import csv
    from fog_features import is_daylight
    from common import CAMERAS_CSV
    coords = {}
    with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("緯度") and row.get("経度"):
                coords[row["地点名"]] = (float(row["緯度"]), float(row["経度"]))
    out = set()
    for k in keys:
        st, dt = k.split("|")
        lat, lon = coords.get(st, (37.4, 139.6))
        if is_daylight(dt, lat, lon):
            out.add(k)
    return out


def summarize(done):
    day = _daylight_keys(done.keys())
    done = {k: v for k, v in done.items() if k in day}
    rows = []
    for k, r in done.items():
        for mode in ("ref", "noref"):
            if mode in r:
                rows.append({"key": k, "mode": mode, "first": r["first"], "human": r["human"], "kind": r["kind"],
                             "verdict": r[mode]["verdict"], "conf": r[mode]["confidence"]})
    df = pd.DataFrame(rows)
    out = {"model": MODEL, "n_items": len(done), "modes": {}}
    if df.empty:
        OUT.write_text(json.dumps(out, ensure_ascii=False)); return
    # 両方の判定がある項目だけで比べる(公平のため)
    both = df.groupby("key")["mode"].nunique()
    df = df[df["key"].isin(both[both == 2].index)]
    for mode, d in df.groupby("mode"):
        river_truth = d["human"] == 1
        fog_truth = d["human"].isin([1, 2])
        keep_river = d["verdict"] == "river"
        keep_fog = d["verdict"].isin(["river", "wide"])
        m = {
            "n": int(len(d)), "human_river": int(river_truth.sum()), "human_fog_any": int(fog_truth.sum()),
            "crosstab": pd.crosstab(d["human"], d["verdict"]).to_dict(),
            # 「人の結論が川霧ではない」もののうち、二重判定もriverと言わなかった割合(誤りを見抜けた割合)
            "river_false_caught": float((~keep_river[~river_truth]).mean()) if (~river_truth).any() else None,
            # 「人の結論が川霧」のうち、二重判定もriverと言った割合(正しい川霧を残せた割合)
            "river_true_kept": float(keep_river[river_truth].mean()) if river_truth.any() else None,
            "fog_false_caught": float((~keep_fog[~fog_truth]).mean()) if (~fog_truth).any() else None,
            "fog_true_kept": float(keep_fog[fog_truth].mean()) if fog_truth.any() else None,
            "unsure_rate": float((d["verdict"] == "unsure").mean()),
            # 二重判定が「霧なし(none)」と言ったもののうち、人も川霧ではないとしていた割合(外す判断の正しさ)
            "none_says_not_river": float((~river_truth[d["verdict"] == "none"]).mean()) if (d["verdict"] == "none").any() else None,
            "n_none": int((d["verdict"] == "none").sum()),
        }
        out["modes"][mode] = m
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    log.info("summary: %s", json.dumps(out["modes"], ensure_ascii=False))


if __name__ == "__main__":
    try:
        os.nice(10)
    except OSError:
        pass
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        summarize(_load_all_progress())
    elif len(sys.argv) > 2:
        main(int(sys.argv[1]), int(sys.argv[2]))
    else:
        main()
