"""AIの二重判定(2026-10-09): Geminiが霧(川霧・全体霧)と判定した画像を、同じカメラの「見通しの良い時の
基準画像」と並べて、もう一度だけ確かめる。

人が確認したラベルでは「川霧→実は現象なし」「全体霧→実は現象なし」の訂正が多く、確信度highの判定も
訂正されていた(label_audit.json)。1枚だけを見て「霞んでいるか」を決めるのは、カメラごとの普段の見え方
(レンズの曇り・遠景の霞み・画角)が分からないと難しいため、同じカメラの晴れた時の画像を基準として見せ、
「普段より明らかに霞んでいるか」を聞く。

- 基準画像: 判定対象より前(なければ後)の、その地点で現象なし(0)かつ確信度highの、日中(9〜14時)の画像。
  人が訂正した時刻は使わない。
- 結果は data/models/label_verify.csv に追記する(datetime, station, verdict, confidence, reasoning, model)。
  verdictは river(川霧)/ wide(全体霧)/ none(霧なし)/ unsure。
- 学習での使い方は fog_model.load_disputed_set() を参照(1回目の判定と食い違ったラベルを学習から外す)。
"""
import csv
import io
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel

from common import BASE_DIR

log = logging.getLogger("label_verify")

IMAGE_DIR = BASE_DIR / "data" / "images"
WEATHER_DB = BASE_DIR / "data" / "weather.db"
USERS_DB = BASE_DIR / "data" / "users.db"
CONF_LOG = BASE_DIR / "data" / "models" / "confidence_log.csv"
VERIFY_LOG = BASE_DIR / "data" / "models" / "label_verify.csv"
VERIFY_FIELDS = ["datetime", "station", "first_code", "verdict", "confidence", "reasoning", "model", "ref_datetime", "checked_at"]
REF_HOURS = range(9, 15)
REF_LOOKBACK_DAYS = 30
RESIZE_MAX_SIDE = 768
TIMEOUT_MS = 40_000

SYSTEM_INSTRUCTION = (
    "あなたは河川監視カメラの画像から霧を見分ける専門家です。同じカメラの画像が2枚あります。\n"
    "1枚目は、そのカメラで見通しが良い(霧も雨もない)時に撮った『基準画像』です。"
    "2枚目が判定する『対象画像』です。\n"
    "対象画像に霧が出ているかを、基準画像と見比べて判断してください。基準画像にも写っている"
    "カメラ固有の見え方(レンズのわずかな曇り・遠くの山の霞み・画角・構図)は霧として数えません。"
    "時刻や季節による明るさ・色合い・影の違い、朝夕の逆光、画面が暗いことも霧ではありません。\n"
    "verdictは次のいずれか:\n"
    "- river: 川面付近が白く霞む・霧の帯が見えるなど、基準画像より明らかに川の上に霧が出ている"
    "(対岸や空はある程度見える)\n"
    "- wide: 川面だけでなく対岸・山・空まで、基準画像より明らかに全体が白く霞んで見通しが悪い\n"
    "- none: 基準画像と比べて、見通しがはっきり悪くなってはいない(霧ではない)\n"
    "- unsure: 画像が不鮮明・レンズの水滴や汚れ・暗さのため、どちらとも判断できない\n"
    "迷う場合、基準画像との差がわずかなときは none としてください。"
    "reasoningには、基準画像と比べて何が違う(または同じ)かを1文で書き、それに基づいてverdictと"
    "confidence(high/medium/low)を決めてください。"
)


class Verdict(BaseModel):
    reasoning: str
    verdict: Literal["river", "wide", "none", "unsure"]
    confidence: Literal["high", "medium", "low"]


def image_path(station, dt_str):
    return IMAGE_DIR / dt_str[:10] / (dt_str[11:13] + dt_str[14:16]) / f"{station}.jpg"


def _resize(path):
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((RESIZE_MAX_SIDE, RESIZE_MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=82)
        return buf.getvalue()


def load_reference_candidates():
    """{地点名: [基準画像に使えるdatetime文字列(昇順)]}。現象なし(0)・確信度high・日中・人の訂正なし。"""
    import pandas as pd
    w = sqlite3.connect(f"file:{WEATHER_DB}?mode=ro", uri=True)
    try:
        ph = pd.read_sql("SELECT datetime, station_name, code FROM phenomena WHERE code = 0 AND datetime >= '2026-01-01'", w)
    finally:
        w.close()
    cl = pd.read_csv(CONF_LOG, usecols=["datetime", "station", "confidence"])
    cl = cl[cl["confidence"] == "high"]
    j = ph.merge(cl, left_on=["datetime", "station_name"], right_on=["datetime", "station"])
    j = j[pd.to_datetime(j["datetime"]).dt.hour.isin(list(REF_HOURS))]
    u = sqlite3.connect(f"file:{USERS_DB}?mode=ro", uri=True)
    try:
        touched = {(s, d) for s, d in u.execute("SELECT station_name, target_datetime FROM phenomena_corrections")}
    finally:
        u.close()
    out = {}
    for st, dt in zip(j["station_name"], j["datetime"]):
        if (st, dt) in touched:
            continue
        out.setdefault(st, []).append(dt)
    for st in out:
        out[st].sort()
    return out


def pick_reference(candidates, station, dt_str):
    """対象より前で一番近い基準画像(REF_LOOKBACK_DAYS日以内、同じ日は使わない)。無ければ後で一番近いもの。"""
    lst = [d for d in candidates.get(station, []) if d[:10] != dt_str[:10] and image_path(station, d).exists()]
    if not lst:
        return None
    lo = (datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S") - timedelta(days=REF_LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    before = [d for d in lst if lo <= d < dt_str]
    if before:
        return before[-1]
    after = [d for d in lst if d > dt_str]
    return after[0] if after else None


class QuotaExhausted(Exception):
    pass


def verify_one(station, dt_str, ref_dt, model, api_key, with_reference=True):
    """1件を判定してVerdictを返す。クォータ超過ならQuotaExhausted、その他の失敗はNone。"""
    from google import genai
    from google.genai import types
    contents = []
    if with_reference:
        contents += ["1枚目(基準画像、見通しの良い時):", types.Part.from_bytes(data=_resize(image_path(station, ref_dt)), mime_type="image/jpeg")]
        contents += ["2枚目(対象画像):"]
    else:
        contents += ["対象画像(基準画像はありません。一般的な見通しの良い河川の風景と比べて判断してください):"]
    contents += [types.Part.from_bytes(data=_resize(image_path(station, dt_str)), mime_type="image/jpeg")]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION, response_mime_type="application/json", response_schema=Verdict,
        temperature=0, http_options=types.HttpOptions(timeout=TIMEOUT_MS),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    client = genai.Client(api_key=api_key)  # 変数に持っておく(使い捨てにすると呼び出し中に閉じられる)
    for attempt in range(3):
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            return resp.parsed
        except Exception as e:  # noqa: BLE001
            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                if "PerDay" in str(e) or "per day" in str(e).lower() or attempt == 2:
                    raise QuotaExhausted(str(e)[:300])
                time.sleep(30)  # 分あたりの上限なら少し待てば戻る
                continue
            log.warning("Verify call failed (%s %s): %s", station, dt_str, str(e)[:200])
            time.sleep(3)
    return None


def experiment_api_key():
    """二重判定・検証用のキー。毎時の収集が主に使う1本目を避け、GEMINI_API_KEYSの最後のキーを使う。"""
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
    keys = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]
    if not keys and os.environ.get("GEMINI_API_KEY"):
        keys = [os.environ["GEMINI_API_KEY"]]
    return keys[-1] if keys else None


def append_results(rows):
    new = not VERIFY_LOG.exists()
    with open(VERIFY_LOG, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=VERIFY_FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in VERIFY_FIELDS})
