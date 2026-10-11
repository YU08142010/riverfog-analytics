"""判定例(few-shot、collect.py の GEMINI_FEWSHOT)の効果を、正解セットで確かめる(2026-10-11)。

正解セットで人の判定がそろった画像を、毎時の判定と同じ指示・同じモデルで「手本なし」「手本あり」の2通りで判定し直し、
人の判定とのまとまり(現象なし/川霧/全体霧/層雲/雨)の一致率と、川霧の適合率・再現率を比べる。
手本には判定する画像そのものは入れない。APIキーは毎時の収集と別(label_verify.experiment_api_key)。
結果: data/experiments/fewshot_check.json。正解セットが50枚ほど集まってから使う。

実行: .venv/bin/python fewshot_check.py [--max 120]
"""
import argparse
import json
import sqlite3
import time
from datetime import datetime

import collect
import label_verify as lv
from common import BASE_DIR, JST

BATCH = 10


def group(c):
    c = c // 100 if c > 10 else c
    return 0 if c == 0 else (1 if c in (1, 2, 3) else (2 if c in (4, 5, 6) else (3 if c in (7, 8) else 4)))


def gold_items(limit):
    conn = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'users.db'}?mode=ro", uri=True)
    rows = conn.execute("SELECT s.station_name, s.target_datetime, GROUP_CONCAT(l.code) FROM gold_samples s "
                        "JOIN gold_labels l ON l.sample_id = s.id WHERE l.code >= 0 GROUP BY s.id").fetchall()
    conn.close()
    out = []
    for st, dt, codes in rows:
        gs = {group(int(c)) for c in codes.split(",")}
        if len(gs) == 1 and lv.image_path(st, dt).exists():
            out.append((st, dt, gs.pop()))
    return out[:limit]


def classify(items, key, fewshot):
    """[(地点, 日時)] -> {(地点, 日時): まとまり}。地点名の重複を避けるため、1回の呼び出しには地点ごとに1枚まで。"""
    res = {}
    pending = list(items)
    while pending:
        batch, seen, rest = [], set(), []
        for st, dt in pending:
            if st in seen or len(batch) >= BATCH:
                rest.append((st, dt))
            else:
                batch.append((st, dt)); seen.add(st)
        pending = rest
        contents, idx = collect._build_contents([(st, collect.resize_image(lv.image_path(st, dt).read_bytes())) for st, dt in batch])
        if fewshot:
            contents = collect._fewshot_contents(collect.fewshot_examples(exclude=set(items))) + contents
        parsed = collect._classify_with_model(contents, collect.os.environ.get("GEMINI_MODEL", collect.DEFAULT_GEMINI_MODEL),
                                              [key], label="fewshot_check")
        if parsed is None:
            continue
        codes, invalid, *_ = collect._parse_classifications(parsed, idx)
        by_name = {st: dt for st, dt in batch}
        for st, code in codes.items():
            res[(st, by_name[st])] = group(int(code))
        time.sleep(5)
    return res


def metrics(pred, truth):
    keys = [k for k in truth if k in pred]
    if not keys:
        return {"n": 0}
    agree = sum(pred[k] == truth[k] for k in keys) / len(keys)
    tp = sum(pred[k] == 1 and truth[k] == 1 for k in keys)
    fp = sum(pred[k] == 1 and truth[k] != 1 for k in keys)
    fn = sum(pred[k] != 1 and truth[k] == 1 for k in keys)
    return {"n": len(keys), "group_agreement": round(agree, 3),
            "river_precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "river_recall": round(tp / (tp + fn), 3) if tp + fn else None}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=120)
    args = ap.parse_args()
    items = gold_items(args.max)
    truth = {(st, dt): g for st, dt, g in items}
    key = lv.experiment_api_key()
    pairs = [(st, dt) for st, dt, _ in items]
    out = {"generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "n_gold": len(items),
           "without_examples": metrics(classify(pairs, key, False), truth),
           "with_examples": metrics(classify(pairs, key, True), truth)}
    (BASE_DIR / "data" / "experiments" / "fewshot_check.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                                            encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
