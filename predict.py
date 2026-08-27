"""霧予測の日次バッチ処理（毎日1回、backfill_weather.pyの後にcronで実行）。

1. 答え合わせ: 過去に予測して記録した中で、対象時刻の実測がもう出ている分を検証する
   （data/predictions.db）
2. 再学習: 実測データ（新しく確定した分も含む）全体でモデルを学習し直す
   （『予測と実際を見比べて誤差を学習し、完璧に近づけていく』の実体はここ。
   RandomForest/XGBoostは1件ずつのオンライン学習に向かないため、
   蓄積データで日次に学習し直す方式にしている）
3. 予測: Open-Meteoの予報から地点ごとの現象コード・霧確率を予測し、グラフ・CSVを出力
4. 記録: 今回の予測を答え合わせ用に記録する（観測時間帯4〜19時分のみ、翌日以降に検証可能）
"""
import argparse
import logging
import logging.handlers
import os
import sys
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv

from common import BASE_DIR, DATA_XLSX, JST
import fog_data
import fog_graphs
import fog_model
import prediction_log
from fog_features import encode_phenomena_cell

# 観測時間帯（collect.pyの毎時収集と合わせている）。この時間帯以外は実測が存在しないため
# 予測しても答え合わせができない。
OBSERVATION_START_HOUR = 4
OBSERVATION_END_HOUR = 19

FORECAST_DAYS = 16  # Open-Meteo無料予報の最大日数（グラフ・記録とも同じ範囲を使う）
HISTORY_DAYS = 5    # 地点別グラフに表示する実測の日数

DB_PATH = BASE_DIR / "data" / "predictions.db"
OUTPUT_ROOT = BASE_DIR / "data" / "predictions"

LOG_FILE = BASE_DIR / "logs" / "predict.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("predict")


def verify_pending_predictions(conn, phenom_df, station_names, now_naive):
    """答え合わせ: 対象時刻の実測が確定している未検証の予測を検証する。"""
    now_str = now_naive.strftime("%Y-%m-%d %H:%M:%S")
    pending = prediction_log.fetch_pending(conn, now_str)
    if not pending:
        log.info("No pending predictions to verify")
        return

    # station -> {datetime_naive: actual_code} の逆引き用Series
    actual_by_station = {}
    for name in station_names:
        s = phenom_df.set_index("datetime")[name].map(encode_phenomena_cell)
        actual_by_station[name] = s

    results = []
    verified_at = datetime.now(JST).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    n_no_data = 0
    for pred_id, station, target_dt_str in pending:
        target_dt = datetime.strptime(target_dt_str, "%Y-%m-%d %H:%M:%S")
        series = actual_by_station.get(station)
        if series is None or target_dt not in series.index:
            n_no_data += 1
            continue
        actual = series.loc[target_dt]
        if pd.isna(actual):
            n_no_data += 1
            continue
        results.append((pred_id, int(actual)))

    if results:
        prediction_log.verify_predictions(conn, verified_at, results)
    log.info("Verified %d prediction(s); %d still have no actual data yet", len(results), n_no_data)

    summary = prediction_log.accuracy_summary(conn)
    overall = summary["overall"]
    if overall["n"]:
        log.info("Overall accuracy so far: %d/%d (%.1f%%)",
                 overall["correct"], overall["n"], 100.0 * overall["correct"] / overall["n"])
    for row in summary["by_lead_day"]:
        if row["n"]:
            log.info("  lead day %s: %d/%d (%.1f%%)",
                     row["lead_day"], row["correct"], row["n"], 100.0 * row["correct"] / row["n"])


def record_new_predictions(conn, run_at_str, pred_df, prob_df, models):
    """今回の予測を答え合わせ用に記録する（観測時間帯のみ）。"""
    run_at_dt = datetime.strptime(run_at_str, "%Y-%m-%d %H:%M:%S")
    rows = []
    obs_mask = pred_df["datetime"].dt.hour.between(OBSERVATION_START_HOUR, OBSERVATION_END_HOUR)
    sub_pred = pred_df[obs_mask]
    sub_prob = prob_df[obs_mask]
    for col in models.keys():
        if col not in sub_pred.columns:
            continue
        model_type = models[col].get("model_type", "")
        for dt, code, prob in zip(sub_pred["datetime"], sub_pred[col], sub_prob[col]):
            lead_hours = (dt.to_pydatetime() - run_at_dt).total_seconds() / 3600.0
            if lead_hours < 0:
                continue
            rows.append((col, dt.strftime("%Y-%m-%d %H:%M:%S"), lead_hours, int(code), float(prob), model_type))
    prediction_log.record_predictions(conn, run_at_str, rows)
    log.info("Recorded %d new prediction row(s) for future verification", len(rows))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                       help="学習・予測・答え合わせは実行するが、DB更新とファイル出力は行わない")
    parser.add_argument("--forecast-days", type=int, default=FORECAST_DAYS)
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    lat = float(os.environ.get("WEATHER_LAT", "37.34861"))
    lon = float(os.environ.get("WEATHER_LON", "139.31583"))

    now_naive = datetime.now(JST).replace(tzinfo=None)
    run_at_str = now_naive.strftime("%Y-%m-%d %H:%M:%S")
    log.info("=== Predict run started%s ===", " (dry-run)" if args.dry_run else "")

    if not DATA_XLSX.exists():
        log.info("xlsx not found, nothing to do: %s", DATA_XLSX)
        return

    main_df, phenom_df, station_names = fog_data.load_river_fog_data()
    log.info("Loaded %d observation row(s), %d station(s)", len(main_df), len(station_names))

    conn = prediction_log.connect(":memory:" if args.dry_run else str(DB_PATH))

    verify_pending_predictions(conn, phenom_df, station_names, now_naive)

    models = fog_model.train_location_models(main_df, phenom_df, station_names, lat, lon, model="hybrid")
    if not models:
        log.warning("No models could be trained (not enough data yet); skipping forecast/graphs")
        return

    rolled_back = fog_model.apply_regression_guard(models)
    if rolled_back:
        log.warning("Rolled back to previous model due to regression: %s", ", ".join(rolled_back))
    if not args.dry_run:
        fog_model.save_model_state(models)

    try:
        forecast_raw = fog_model.fetch_forecast_range(lat, lon, args.forecast_days)
    except Exception as e:
        log.error("Forecast fetch failed: %s", e)
        return
    log.info("Fetched forecast: %d hour(s), %s - %s",
             len(forecast_raw), forecast_raw["datetime"].min(), forecast_raw["datetime"].max())

    pred_df, prob_df = fog_model.build_location_forecast_codes(models, forecast_raw, lat, lon)

    if not args.dry_run:
        out_dir = OUTPUT_ROOT / now_naive.strftime("%Y-%m-%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        run_date_str = now_naive.strftime("%Y%m%d")

        for col in models.keys():
            fog_graphs.plot_single_location_forecast(
                main_df, phenom_df, col, forecast_raw, pred_df[col], str(out_dir),
                history_days=HISTORY_DAYS, prob_series=prob_df[col],
                fog_threshold=models[col].get("fog_threshold", fog_model.DEFAULT_FOG_THRESHOLD),
                run_date_str=run_date_str,
            )
        fog_graphs.plot_all_location_summary(pred_df, models, str(out_dir), prob_df=prob_df,
                                             run_date_str=run_date_str)
        fog_graphs.export_prediction_csv(pred_df, prob_df, models, str(out_dir), run_date_str=run_date_str)
        log.info("Saved graphs/CSV to %s", out_dir)

        record_new_predictions(conn, run_at_str, pred_df, prob_df, models)
        conn.commit()

    log.info("=== Predict run finished ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("predict.py terminated abnormally")
        sys.exit(1)
