"""川霧予測の日次バッチ処理（毎日1回、backfill_weather.pyの後にcronで実行）。

1. 答え合わせ: 過去に予測して記録した中で、対象時刻の実測がもう出ている分を検証する
   （data/predictions.db）
2. 再学習: 実測データ全体で、全地点共通の二値LightGBM(川霧あり/なし)を学習し直す
   (fog_model.train_shared_model)。前回のモデルより確率の誤差が有意に悪化していれば
   前回のモデルに戻す(fog_model.apply_regression_guard)。
3. 予測: Open-Meteoの予報から地点ごとの川霧の確率を計算し、しきい値で二値化した予測
   (1=川霧あり、0=川霧なし)とあわせてグラフ・CSVを出力する
4. 記録: 今回の予測を答え合わせ用に記録する（観測時間帯4〜19時分のみ、翌日以降に検証可能）
"""
import argparse
import json
import logging
import logging.handlers
import sys
from datetime import datetime, timedelta

import pandas as pd
from dotenv import load_dotenv

from common import (
    BASE_DIR, DATA_XLSX, STATION_WEATHER_DB_PATH,
    load_camera_coords, load_camera_regions, now_jst_naive, representative_coords, send_alert,
)
import fog_data
import fog_features
import fog_graphs
import fog_model
import prediction_log
import station_weather
from fog_features import encode_phenomena_cell

# 観測時間帯（collect.pyの毎時収集と合わせている）。この時間帯以外は実測が存在しないため
# 予測しても答え合わせができない。
OBSERVATION_START_HOUR = 4
OBSERVATION_END_HOUR = 19

FORECAST_DAYS = 16  # Open-Meteo無料予報の最大日数（グラフ・記録とも同じ範囲を使う）
HISTORY_DAYS = 5    # 地点別グラフに表示する実測の日数

DB_PATH = BASE_DIR / "data" / "predictions.db"
OUTPUT_ROOT = BASE_DIR / "data" / "predictions"
# 各地点の直近の予測に使われた特徴量の値(webapp.pyのstation_detail.htmlで
# 「なぜこの予測になったか」を数値で示すために読む)。
FEATURE_SNAPSHOT_PATH = BASE_DIR / "data" / "models" / "feature_snapshot.json"

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
    """答え合わせ: 対象時刻の実測が確定している未検証の予測を検証する。
    実測は現象コード(0〜10、2コード同時観測はパック値)のまま記録し、的中の判定は
    集計時に「川霧かどうか」で行う(prediction_log参照)。"""
    now_str = now_naive.strftime("%Y-%m-%d %H:%M:%S")
    pending = prediction_log.fetch_pending(conn, now_str)
    if not pending:
        log.info("No pending predictions to verify")
        return

    # 実測は人による訂正(users.dbのphenomena_corrections)を反映した値で答え合わせする。
    # phenom_dfに同一datetimeの行が重複しているとSeriesのlocがスカラーを返さなくなるため
    # drop_duplicatesしてから引く。
    phenom_df, _ = fog_data.apply_phenomena_corrections(phenom_df, fog_data.load_phenomena_corrections())
    deduped = phenom_df.drop_duplicates(subset="datetime").set_index("datetime")
    actual_by_station = {name: deduped[name].map(encode_phenomena_cell) for name in station_names}

    results = []
    verified_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    n_no_data = 0
    for p in pending:
        target_dt = datetime.strptime(p["target_datetime"], "%Y-%m-%d %H:%M:%S")
        series = actual_by_station.get(p["station"])
        if series is None or target_dt not in series.index:
            n_no_data += 1
            continue
        actual = series.loc[target_dt]
        if pd.isna(actual):
            n_no_data += 1
            continue
        results.append((p["id"], int(actual)))

    if results:
        prediction_log.verify_predictions(conn, verified_at, results)
    log.info("Verified %d prediction(s); %d still have no actual data yet", len(results), n_no_data)
    # 答え合わせの後に人が訂正した実測も、記録済みの答え合わせに反映する。
    n_fixed = prediction_log.apply_actual_corrections(conn, fog_data.load_phenomena_corrections())
    if n_fixed:
        log.info("Updated %d verified prediction(s) with corrected actual codes", n_fixed)

    summary = prediction_log.accuracy_summary(conn)
    overall = summary["overall"]
    if overall["n"]:
        log.info("Overall accuracy (fog / no fog) so far: %d/%d (%.1f%%)",
                 overall["correct"], overall["n"], 100.0 * overall["correct"] / overall["n"])
    for row in summary["by_lead_day"]:
        if row["n"]:
            log.info("  lead day %s: %d/%d (%.1f%%)",
                     row["lead_day"], row["correct"], row["n"], 100.0 * row["correct"] / row["n"])


def _save_feature_snapshot(feature_snapshots, models, now_naive, path=FEATURE_SNAPSHOT_PATH):
    """各地点について、now以降で最も近い時刻の予測に使われた特徴量の値をJSONで保存する
    (station_detail.htmlで「この予測にどの数値が使われたか」を表示するため)。"""
    payload = {}
    for station, df in feature_snapshots.items():
        future = df[(df["datetime"] >= now_naive) & (df["predicted_fog"] >= 0)]
        if future.empty:
            continue
        row = future.iloc[0]
        info = models.get(station, {})
        features = {
            col: (None if pd.isna(row[col]) else round(float(row[col]), 3))
            for col in df.columns
            if col not in ("datetime", "predicted_fog", "predicted_fog_prob")
        }
        payload[station] = {
            "target_datetime": row["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
            "features": features,
            "predicted_fog": int(row["predicted_fog"]),
            "predicted_fog_prob": round(float(row["predicted_fog_prob"]), 4),
            "fog_threshold": info.get("fog_threshold"),
            "model_type": info.get("model_type", ""),
        }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                {"generated_at": now_naive.strftime("%Y-%m-%d %H:%M:%S"), "stations": payload},
                f, ensure_ascii=False, indent=2,
            )
        tmp_path.replace(path)
    except Exception as e:
        log.warning("Failed to save feature snapshot: %s", e)


def record_new_predictions(conn, run_at_str, pred_df, prob_df, models):
    """今回の予測を答え合わせ用に記録する（観測時間帯のみ）。predicted_codeには二値の
    予測(1=川霧あり、0=川霧なし)を入れる(prediction_log.pyのスキーマ説明参照)。"""
    run_at_dt = datetime.strptime(run_at_str, "%Y-%m-%d %H:%M:%S")
    rows = []
    n_past = 0
    n_total = 0
    obs_mask = pred_df["datetime"].dt.hour.between(OBSERVATION_START_HOUR, OBSERVATION_END_HOUR)
    sub_pred = pred_df[obs_mask]
    sub_prob = prob_df[obs_mask]
    for col in models.keys():
        if col not in sub_pred.columns:
            continue
        model_type = models[col].get("model_type", "")
        for dt, pred, prob in zip(sub_pred["datetime"], sub_pred[col], sub_prob[col]):
            if int(pred) == fog_model.NOT_PREDICTED:
                continue  # 日の出前・日の入り後は予測しない(答え合わせもしない)
            n_total += 1
            lead_hours = (dt.to_pydatetime() - run_at_dt).total_seconds() / 3600.0
            if lead_hours < 0:
                n_past += 1
                continue
            rows.append((col, dt.strftime("%Y-%m-%d %H:%M:%S"), lead_hours, int(pred), float(prob), model_type))
    if n_total and n_past / n_total > 0.5:
        # 予報時刻がJSTのnaive値である前提が崩れる(API仕様変更でタイムゾーンがズレる等)と、
        # ほぼ全行がlead_hours<0で捨てられ「0件記録」という分かりにくい結果になるため警告する。
        log.warning(
            "%d/%d forecast row(s) were treated as being in the past (lead_hours < 0); "
            "the forecast timestamps may not be JST as expected",
            n_past, n_total,
        )
    prediction_log.record_predictions(conn, run_at_str, rows)
    log.info("Recorded %d new prediction row(s) for future verification", len(rows))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="学習・予測・答え合わせは実行するが、DB更新とファイル出力は行わない")
    parser.add_argument("--forecast-days", type=int, default=FORECAST_DAYS)
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    lat, lon = representative_coords()

    now_naive = now_jst_naive()
    run_at_str = now_naive.strftime("%Y-%m-%d %H:%M:%S")
    log.info("=== Predict run started%s ===", " (dry-run)" if args.dry_run else "")

    if not DATA_XLSX.exists():
        log.info("xlsx not found, nothing to do: %s", DATA_XLSX)
        return

    main_df, phenom_df, station_names = fog_data.load_river_fog_data()
    log.info("Loaded %d observation row(s), %d station(s)", len(main_df), len(station_names))

    camera_coords = load_camera_coords()
    weather_conn = station_weather.connect(STATION_WEATHER_DB_PATH)

    conn = prediction_log.connect(":memory:" if args.dry_run else str(DB_PATH))

    verify_pending_predictions(conn, phenom_df, station_names, now_naive)
    n_marked = prediction_log.fill_daylight(conn, camera_coords)
    if n_marked:
        log.info("Marked %d prediction row(s) as daytime/night-time", n_marked)

    prev_state = fog_model.load_model_state()
    # 全地点共通のモデルは、これまでどおり全地域を川霧(1〜3)で学習する(只見川の予測にはこれを使う)。
    target_enabled = fog_features.REGION_TARGET_ENABLED
    fog_features.REGION_TARGET_ENABLED = False
    try:
        models = fog_model.train_shared_model(
            main_df, phenom_df, station_names, lat, lon,
            camera_coords=camera_coords, weather_conn=weather_conn, prev_state=prev_state,
        )
    finally:
        fog_features.REGION_TARGET_ENABLED = target_enabled
    if not models:
        log.warning("No model could be trained (not enough data yet); skipping forecast/graphs")
        return
    # 地域ごとの予測の対象(2026-10-11〜、fog_features.REGION_TARGET_CODES): 会津は全体霧も含む「霧」を、
    # 会津の地点だけで学習した別のモデルで予測する。同じ評価期間での比較(aizu_target_check.py)で、
    # 会津は共通モデルに混ぜるより別モデルの方が良く(AP 0.173→0.204)、只見川は共通モデルのままが良かった。
    if target_enabled:
        regions_now = load_camera_regions()
        for region in fog_features.REGION_TARGET_CODES:
            cols = [n for n in station_names if regions_now.get(n) == region]
            if not cols:
                continue
            fog_model._FRAME_CACHE["inputs"] = None  # 正解が変わるので組み立て直す
            try:
                region_models = fog_model.train_shared_model(
                    main_df, phenom_df, cols, lat, lon,
                    camera_coords=camera_coords, weather_conn=weather_conn, prev_state=prev_state,
                )
            except Exception:
                log.exception("Region model (%s) failed; keeping the shared model for it", region)
                region_models = {}
            if region_models:
                for st in region_models:
                    region_models[st]["target_region"] = region
                models.update(region_models)
                log.info("Region model for %s (all fog incl. wide fog): %d station(s)", region, len(region_models))

    rolled_back = fog_model.apply_regression_guard(models, prev_state=prev_state)
    if rolled_back:
        log.warning("Rolled back to previous model due to regression: %s", ", ".join(rolled_back))
    if not args.dry_run:
        fog_model.save_model_state(models)
        fog_model.remove_legacy_calibrator()

    try:
        forecast_raw = fog_model.fetch_forecast_range(lat, lon, args.forecast_days)
    except Exception as e:
        log.error("Forecast fetch failed: %s", e)
        return
    log.info("Fetched forecast: %d hour(s), %s - %s",
             len(forecast_raw), forecast_raw["datetime"].min(), forecast_raw["datetime"].max())

    # Open-Meteoは常に「今日のローカル日付の00:00」から返す仕様のため、日付だけを見て
    # タイムゾーンがAsia/Tokyoのままか確認する(UTCに戻ると9時間ズレたまま記録されてしまう)。
    first_forecast_date = forecast_raw["datetime"].min().to_pydatetime().date()
    if first_forecast_date not in (now_naive.date(), now_naive.date() - timedelta(days=1)):
        log.warning(
            "Forecast start date looks suspicious (now=%s, forecast_start_date=%s); "
            "check whether Open-Meteo is still returning Asia/Tokyo local time",
            now_naive, first_forecast_date,
        )

    try:
        forecast_by_station = fog_model.fetch_forecast_range_multi(camera_coords, args.forecast_days)
        log.info("Fetched per-station forecast: %d/%d station(s)", len(forecast_by_station), len(camera_coords))
    except Exception as e:
        log.warning("Per-station forecast fetch failed, falling back to the shared forecast: %s", e)
        forecast_by_station = {}

    # 気象モデルの平均(fog_model.ENSEMBLE_MODELS)。取得できなければ既定の予報だけで予測する。
    alt_forecasts = {}
    for nwp in fog_model.ENSEMBLE_MODELS:
        try:
            alt_forecasts[nwp] = fog_model.fetch_forecast_range_multi(
                camera_coords, min(args.forecast_days, fog_model.ENSEMBLE_MAX_FORECAST_DAYS), model=nwp)
            log.info("Fetched %s forecast: %d/%d station(s)", nwp, len(alt_forecasts[nwp]), len(camera_coords))
        except Exception as e:
            log.warning("%s forecast fetch failed; using the default forecast only: %s", nwp, e)

    pred_df, prob_df, feature_snapshots = fog_model.build_location_forecast(
        models, forecast_raw, lat, lon,
        forecast_by_station=forecast_by_station, camera_coords=camera_coords,
        alt_forecasts=alt_forecasts,
    )
    # 「川霧あり」と予測された件数を日ごとに残す(しきい値・予報の入力が想定通りかの確認用)。
    obs_hours = pred_df["datetime"].dt.hour.between(OBSERVATION_START_HOUR, OBSERVATION_END_HOUR)
    station_cols = [c for c in pred_df.columns if c != "datetime"]
    obs = pred_df[obs_hours]
    per_day = (obs[station_cols] == 1).groupby(obs["datetime"].dt.strftime("%m/%d")).sum().sum(axis=1)
    log.info("Forecast: %d station-hour(s) in observation hours, %d predicted as river fog (by day: %s)",
             len(obs) * len(station_cols), int(per_day.sum()),
             ", ".join(f"{d}:{int(n)}" for d, n in per_day.items()))

    if not args.dry_run:
        _save_feature_snapshot(feature_snapshots, models, now_naive)
        out_dir = OUTPUT_ROOT / now_naive.strftime("%Y-%m-%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        run_date_str = now_naive.strftime("%Y%m%d")

        sample_chart_path = None
        for col in models.keys():
            chart_path = fog_graphs.plot_single_location_forecast(
                main_df, phenom_df, col, forecast_raw, pred_df[col], str(out_dir),
                history_days=HISTORY_DAYS, prob_series=prob_df[col],
                fog_threshold=models[col].get("fog_threshold", fog_model.DEFAULT_FOG_THRESHOLD),
                run_date_str=run_date_str,
            )
            if sample_chart_path is None:
                sample_chart_path = chart_path
        fog_graphs.plot_all_location_summary(pred_df, models, str(out_dir), prob_df=prob_df,
                                             run_date_str=run_date_str)
        regions = load_camera_regions()
        for region_label, file_suffix in (("只見川", "_只見"), ("会津", "_会津")):
            region_stations = {name for name, r in regions.items() if r == region_label}
            fog_graphs.plot_all_location_summary(
                pred_df, models, str(out_dir), prob_df=prob_df, run_date_str=run_date_str,
                station_names=region_stations, file_suffix=file_suffix, label_suffix=f"（{region_label}）",
            )
        fog_graphs.export_prediction_csv(pred_df, prob_df, models, str(out_dir), run_date_str=run_date_str)
        # グラフの出力形式(.html=Plotly / .png=旧matplotlib)をログに残しておく
        # (デプロイのタイミングによって新旧どちらで動いたか見分けられるようにするため)。
        chart_format = sample_chart_path.rsplit(".", 1)[-1] if sample_chart_path else "?"
        log.info("Saved graphs/CSV to %s (chart format: .%s)", out_dir, chart_format)

        record_new_predictions(conn, run_at_str, pred_df, prob_df, models)
        conn.commit()

        # 当日の短時間予測(ナウキャスト、nowcast.py)のモデルを学習し直す。失敗しても本体の予測は済んでいる。
        try:
            import nowcast
            nowcast.train_and_save(main_df, phenom_df, station_names, lat, lon,
                                   camera_coords=camera_coords, weather_conn=weather_conn)
        except Exception:
            log.exception("Nowcast training failed (the daily forecast itself succeeded)")

    weather_conn.close()
    log.info("=== Predict run finished ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("predict.py terminated abnormally")
        # 日次の再学習・予報生成が丸ごと失敗すると、翌日の予測が更新されないまま
        # 古い状態で表示され続けてしまう。ログだけでは気づきにくいため即座にプッシュ通知する。
        send_alert(f"predict.py が異常終了しました: {e}", source="predict.py")
        sys.exit(1)
