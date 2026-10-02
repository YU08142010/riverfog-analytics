"""ラベル品質の調査(管理者タブ「仮検証」のラベル品質ページ用)。

人間による訂正・確認の記録(users.db)とGeminiの確信度ログから、ラベルの誤りの傾向を集計する。
すべて読み取り専用で、結果はdata/experiments/label_audit.jsonに書き出す(本番のDB・モデルは変更しない)。
判定者は匿名化し(A, B, ...)、個人を特定できる情報は出力しない。

実行: .venv/bin/python label_audit.py
"""
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime

import numpy as np
import pandas as pd

from common import BASE_DIR, JST

EXP_DIR = BASE_DIR / "data" / "experiments"
OUT_PATH = EXP_DIR / "label_audit.json"
USERS_DB = BASE_DIR / "data" / "users.db"
WEATHER_DB = BASE_DIR / "data" / "weather.db"
CONF_LOG = BASE_DIR / "data" / "models" / "confidence_log.csv"
ESC_STATE = BASE_DIR / "data" / "models" / ".escalation_state.json"
COLLECT_LOG = BASE_DIR / "logs" / "collect.log"

log = logging.getLogger("label_audit")


def prim(v):
    v = float(v)
    return int(v // 100) if v > 10 else int(v)


def group(c):
    return 0 if c == 0 else (1 if c in (1, 2, 3) else (2 if c in (4, 5, 6) else (3 if c in (7, 8) else 4)))


GROUP_NAMES = {0: "現象なし", 1: "川霧(1〜3)", 2: "全体霧(4〜6)", 3: "層雲(7・8)", 4: "霧雨・雨(9・10)"}


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if o != o else float(o)
    return o


def _anon(series):
    """判定者IDを、出現順にA, B, C...へ置き換える(個人を特定しない)。"""
    order = {v: chr(ord("A") + i) for i, v in enumerate(pd.Series(series).dropna().value_counts().index)}
    return series.map(order).fillna("自動")


def run():
    u = sqlite3.connect(f"file:{USERS_DB}?mode=ro", uri=True)
    corr = pd.read_sql("SELECT * FROM phenomena_corrections", u)
    reports = pd.read_sql("SELECT * FROM observation_reports", u)
    u.close()
    cl = pd.read_csv(CONF_LOG)

    human = corr[corr["source"] == "human"].copy()
    human["o"] = human["original_code"].map(prim)
    human["n"] = human["corrected_code"].map(prim)
    human["who"] = _anon(human["corrected_by"])
    human["hour"] = pd.to_datetime(human["target_datetime"]).dt.hour
    human = human.merge(cl, left_on=["target_datetime", "station_name"], right_on=["datetime", "station"], how="left")

    confirmed = reports[reports["status"] == "dismissed"].copy()  # 人間が「問題なし」と確認
    confirmed["o"] = confirmed["reported_code"].map(prim)
    confirmed["n"] = confirmed["o"]
    confirmed["hour"] = pd.to_datetime(confirmed["target_datetime"]).dt.hour
    confirmed = confirmed.merge(cl, left_on=["target_datetime", "station_name"], right_on=["datetime", "station"], how="left")

    reviewed = pd.concat([
        human[["station_name", "target_datetime", "o", "n", "hour", "confidence"]].assign(kind="corrected"),
        confirmed[["station_name", "target_datetime", "o", "n", "hour", "confidence"]].assign(kind="confirmed"),
    ], ignore_index=True).drop_duplicates(["station_name", "target_datetime"], keep="first")
    reviewed["changed"] = reviewed["kind"] == "corrected"

    out = {"meta": {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "period": [str(corr["target_datetime"].min()), str(corr["target_datetime"].max())],
        "n_corrections_total": int(len(corr)), "n_human_corrections": int(len(human)),
        "n_auto_corrections": int((corr["source"] == "auto").sum()),
        "n_confirmed": int(len(confirmed)), "n_reviewed": int(len(reviewed)),
    }}

    # --- 判定者 -----------------------------------------------------------------
    per = human["who"].value_counts()
    keys = corr.groupby(["station_name", "target_datetime"]).agg(n=("id", "size"), who=("corrected_by", "nunique"))
    out["reviewers"] = {
        "counts": [{"reviewer": k, "n": int(v)} for k, v in per.items()],
        "distinct_targets": int(len(keys)),
        "targets_reviewed_by_2plus_people": int((keys["who"] >= 2).sum()),
        "top2_share": float(per.iloc[:2].sum() / per.sum()) if len(per) else None,
    }

    # --- 訂正の流れ(Gemini → 人間) -------------------------------------------------
    codes = sorted(set(human["o"]) | set(human["n"]))
    mat = pd.crosstab(human["o"], human["n"]).reindex(index=codes, columns=codes, fill_value=0)
    og, ng = human["o"].map(group), human["n"].map(group)
    out["flow"] = {
        "codes": codes, "matrix": mat.values.tolist(),
        "fog_to_clear": int(((og == 1) & (ng == 0)).sum()), "fog_to_other": int(((og == 1) & (ng != 0) & (ng != 1)).sum()),
        "clear_to_fog": int(((og == 0) & (ng == 1)).sum()), "both_fog": int(((og == 1) & (ng == 1)).sum()),
        "wide_to_clear": int(((og == 2) & (ng == 0)).sum()), "stratus_to_clear": int(((og == 3) & (ng == 0)).sum()),
        "n_gemini_fog_labels_reviewed_as_corrected": int((og == 1).sum()),
        "unchanged": int((human["o"] == human["n"]).sum()),
    }

    # --- 確信度別(人間が見た範囲で、訂正された割合) ----------------------------------
    allc = cl["confidence"].value_counts(normalize=True)
    rows = []
    for c in ("high", "medium", "low"):
        sub = reviewed[reviewed["confidence"] == c]
        rows.append({"confidence": c, "reviewed": int(len(sub)), "corrected": int(sub["changed"].sum()),
                     "corrected_rate": float(sub["changed"].mean()) if len(sub) else None,
                     "share_of_all_labels": float(allc.get(c, 0.0))})
    out["by_confidence"] = rows
    out["by_confidence_note"] = "人間が確認した(訂正または問題なしと判断した)ラベルのうち、実際に訂正された割合。自動検出で疑わしいと判定されたラベルが中心のため、全ラベルの誤り率ではない。"

    # --- 時刻・地点別 -------------------------------------------------------------
    by_hour = reviewed.groupby("hour")["changed"].agg(["size", "sum"])
    out["by_hour"] = [{"hour": int(h), "reviewed": int(r["size"]), "corrected": int(r["sum"])} for h, r in by_hour.iterrows()]
    by_st = reviewed.groupby("station_name")["changed"].agg(["size", "sum"]).sort_values("sum", ascending=False).head(12)
    out["by_station"] = [{"station": s, "reviewed": int(r["size"]), "corrected": int(r["sum"])} for s, r in by_st.iterrows()]

    # --- 具体例(川霧→現象なし) ----------------------------------------------------
    fp = human[(og == 1) & (ng == 0)].sort_values("target_datetime")
    out["examples"] = [{
        "datetime": r.target_datetime, "station": r.station_name, "gemini": int(r.o), "human": int(r.n),
        "confidence": r.confidence if isinstance(r.confidence, str) else None,
        "reasoning": (r.reasoning[:120] if isinstance(r.reasoning, str) else None),
    } for r in fp.head(40).itertuples()]

    # --- 全ラベルでの確信度×現象グループ(学習データの構成) ------------------------------
    w = sqlite3.connect(f"file:{WEATHER_DB}?mode=ro", uri=True)
    ph = pd.read_sql("SELECT datetime, station_name, code FROM phenomena WHERE code IS NOT NULL AND datetime >= '2026-08-25'", w)
    w.close()
    ph["g"] = ph["code"].map(prim).map(group)
    j = ph.merge(cl, left_on=["datetime", "station_name"], right_on=["datetime", "station"], how="inner")
    tab = pd.crosstab(j["g"], j["confidence"]).reindex(columns=["high", "medium", "low"], fill_value=0)
    out["confidence_by_group"] = [{"group": GROUP_NAMES.get(int(g), str(g)), **{c: int(tab.loc[g, c]) for c in tab.columns}} for g in tab.index]

    # --- 上位モデルによる再判定(エスカレーション)の状況 -----------------------------------
    esc = {}
    try:
        esc.update(json.loads(ESC_STATE.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        pass
    try:
        env = {}
        for ln in open(BASE_DIR / ".env", encoding="utf-8"):
            if ln.startswith("GEMINI_MODEL=") or ln.startswith("GEMINI_ESCALATION_MODEL="):  # 秘密情報ではない2項目のみ読む
                k, _, v = ln.strip().partition("=")
                env[k] = v
        esc["configured_models"] = env
    except Exception:  # noqa: BLE001
        pass
    try:
        with open(COLLECT_LOG, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-60000:]
        today = datetime.now(JST).strftime("%Y-%m-%d")
        esc["today_escalation_failed"] = sum(1 for ln in tail if ln.startswith(today) and "Escalation classification failed" in ln)
        esc["today_escalation_reasoning"] = sum(1 for ln in tail if ln.startswith(today) and "Escalation reasoning:" in ln)
        esc["today_quota_errors"] = sum(1 for ln in tail if ln.startswith(today) and "[QUOTA] (escalation" in ln)
    except Exception:  # noqa: BLE001
        pass
    out["escalation"] = esc

    EXP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_jsonable(out), ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT_PATH)
    log.info("label audit written: %s", OUT_PATH)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        os.nice(10)
    except OSError:
        pass
    run()
