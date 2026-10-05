"""2026-08-30〜10-02の現象ラベルを、気象のヒントなしで判定し直す(2026-10-05)。

この期間は、Geminiに画像を判定させる際に撮影時刻の気象値(湿度・風速等と「高湿度・低風速なら
霧が出やすい」という説明)を一緒に渡していた(collect.pyのGEMINI_WEATHER_HINT、10/2に既定で
無効化)。予測モデルの説明変数と同じ情報でラベル(目的変数)が左右されるため、学習・評価の
正解として使うと精度が実際より良く見える。保存済みの画像を、現在の判定方法(ヒントなし・
現在の上位モデルでの再確認。10/3以降のラベルと同じ条件)で判定し直し、weather.dbを書き換える。

  - 判定は1時刻ごと(その時刻の全地点の画像をまとめて1回)。collect.classify_imagesをそのまま使う。
  - 取得失敗のプレースホルダー(_unavailable.jpg)しかない地点は判定せず、元のラベルのまま。
  - 「実際の写真ではない」と判定された地点は、collect.pyと同じく欠測(None)にする。
  - 人による訂正(users.dbのphenomena_corrections)は別に保持しているので影響を受けない
    (学習・答え合わせでは、これまで通り訂正が優先される)。
  - 判定の確信度はconfidence_log.csvに追記する(学習時は日時・地点ごとの最新の値を使う)。
  - 変更前後のラベルはdata/experiments/relabel_without_weather_hint.csvに残す。
  - 進捗はdata/experiments/relabel_without_weather_hint.progress.jsonに記録し、途中で
    止まっても(Geminiの無料枠の上限等)、再実行すれば続きから再開する。
  - 全時刻を終えたら、予測DBの答え合わせ(実測)を新しいラベルに揃え、再予測
    (hindcast_lightgbm.py --replace)をやり直す。

実行: nohup .venv/bin/python relabel_without_weather_hint.py >> logs/relabel.log 2>&1 &
"""
import argparse
import csv
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv

from common import BASE_DIR, JST, WEATHER_DB_PATH

START_DATE = "2026-08-30"
END_DATE = "2026-10-02"
OUT_DIR = BASE_DIR / "data" / "experiments"
CHANGES_PATH = OUT_DIR / "relabel_without_weather_hint.csv"
PROGRESS_PATH = OUT_DIR / "relabel_without_weather_hint.progress.json"
IMAGE_DIR = BASE_DIR / "data" / "images"
PAUSE_BETWEEN_HOURS_SEC = 20     # 無料枠の分あたりの上限を避けるための間隔
RETRY_WAIT_SEC = 30 * 60         # 判定が丸ごと失敗した(上限に達した等)ときの待ち時間
MAX_CONSECUTIVE_FAILURES = 16    # これだけ続けて失敗したら止める(約8時間)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger("relabel")


def _load_progress():
    try:
        return set(json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))["done"])
    except Exception:
        return set()


def _save_progress(done):
    tmp = PROGRESS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"done": sorted(done)}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PROGRESS_PATH)


def _slots(start, end):
    day = datetime.strptime(start, "%Y-%m-%d")
    last = datetime.strptime(end, "%Y-%m-%d")
    while day <= last:
        day_dir = IMAGE_DIR / day.strftime("%Y-%m-%d")
        if day_dir.is_dir():
            for hour_dir in sorted(p for p in day_dir.iterdir() if p.is_dir() and p.name.isdigit()):
                yield day.strftime("%Y-%m-%d"), hour_dir.name
        day += timedelta(days=1)


def _refresh_predictions():
    """予測DBの答え合わせ(実測)を新しいラベル+人による訂正に揃え、再予測をやり直す。"""
    import fog_data
    import prediction_log
    changes = {}
    with open(CHANGES_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            changes[(row["datetime"], row["station"])] = (None if row["new_code"] == "" else int(row["new_code"]))
    conn = prediction_log.connect(str(BASE_DIR / "data" / "predictions.db"))
    with conn:
        before = conn.total_changes
        # 新しいラベルが欠測(写真ではない)になった時刻は、答え合わせ自体を取り消す。
        conn.executemany(
            """UPDATE predictions SET actual_code = ?, verified_at = CASE WHEN ? IS NULL THEN NULL ELSE verified_at END
               WHERE station = ? AND target_datetime = ? AND actual_code IS NOT NULL""",
            [(code, code, st, dt) for (dt, st), code in changes.items()],
        )
        n_updated = conn.total_changes - before
    n_fixed = prediction_log.apply_actual_corrections(conn, fog_data.load_phenomena_corrections())
    log.info("Prediction DB: %d verified row(s) updated to the new labels, %d re-applied human correction(s)",
             n_updated, n_fixed)
    conn.close()
    result = subprocess.run([sys.executable, str(BASE_DIR / "hindcast_lightgbm.py"), "--replace"], cwd=BASE_DIR)
    log.info("Re-prediction finished with exit code %d", result.returncode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-only", action="store_true",
                        help="判定はせず、予測DBの更新と再予測だけを行う")
    args = parser.parse_args()
    load_dotenv(BASE_DIR / ".env")
    # この期間を判定し直す目的そのものなので、.envの設定に関わらず気象のヒントは付けない。
    os.environ["GEMINI_WEATHER_HINT"] = "0"
    if args.refresh_only:
        _refresh_predictions()
        return 0

    import collect
    import weather_store

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cameras = collect.load_cameras()
    names = [name for name, _ in cameras]
    done = _load_progress()
    slots = [s for s in _slots(START_DATE, END_DATE) if f"{s[0]} {s[1]}" not in done]
    log.info("=== Relabel started: %d hour slot(s) remaining (%s - %s) ===", len(slots), START_DATE, END_DATE)
    is_new_log = not CHANGES_PATH.exists()
    failures = 0
    i = 0
    while i < len(slots):
        date, hhmm = slots[i]
        run_time = datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=JST)
        dt_str = run_time.strftime("%Y-%m-%d %H:%M:%S")
        hour_dir = IMAGE_DIR / date / hhmm
        images = {}
        for name in names:
            path = hour_dir / f"{name}.jpg"
            if path.exists():
                images[name] = path.read_bytes()
        if not images:
            done.add(f"{date} {hhmm}")
            _save_progress(done)
            i += 1
            continue

        result, invalid, confidence, reasoning = collect.classify_images(cameras, images, weather=None, run_time=run_time)
        if not result and not invalid:
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("Classification failed %d time(s) in a row; stopping (re-run to resume)", failures)
                return 1
            log.warning("Classification failed for %s; waiting %d min before retrying (%d/%d)",
                        dt_str, RETRY_WAIT_SEC // 60, failures, MAX_CONSECUTIVE_FAILURES)
            time.sleep(RETRY_WAIT_SEC)
            continue
        failures = 0

        conn = weather_store.connect(WEATHER_DB_PATH)
        try:
            old = dict(conn.execute("SELECT station_name, code FROM phenomena WHERE datetime = ?", (dt_str,)).fetchall())
            new = {}
            for name in images:
                if name in result:
                    new[name] = int(result[name])
                elif name in invalid:
                    new[name] = None
            weather_store.record_phenomena(conn, dt_str, new)
        finally:
            conn.close()
        collect.append_confidence_log(confidence, reasoning, run_time)
        with open(CHANGES_PATH, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if is_new_log:
                writer.writerow(["datetime", "station", "old_code", "new_code", "confidence", "reasoning"])
                is_new_log = False
            for name, code in new.items():
                writer.writerow([dt_str, name, "" if old.get(name) is None else old[name],
                                 "" if code is None else code, confidence.get(name, ""), reasoning.get(name, "")])
        n_changed = sum(1 for name, code in new.items() if old.get(name) != code)
        log.info("%s: %d station(s) re-judged, %d label(s) changed", dt_str, len(new), n_changed)
        done.add(f"{date} {hhmm}")
        _save_progress(done)
        i += 1
        time.sleep(PAUSE_BETWEEN_HOURS_SEC)

    log.info("=== All hour slots re-judged; refreshing the prediction DB and re-predictions ===")
    _refresh_predictions()
    log.info("=== Relabel finished ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
