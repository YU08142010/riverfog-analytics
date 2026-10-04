"""旧モデル(地点別の多クラス分類)が予測していた過去の日時を、現行の全地点共通の二値LightGBMで
「その当時に予測していたら」の条件で予測し直し、predictions.dbに追加する(2026-10-05)。

予測DBは「その時点で実際に何と予測したか」の記録なので、旧モデルの行は書き換えずに残し、
再予測の行はmodel_typeに"[再予測]"を付けて追加する(HINDCAST_MODEL_TYPE)。分析ページ等の
精度の集計は、現行モデル(本番の予測+この再予測)の行だけを対象にする(prediction_log参照)。

後から分かった結果で過去を予測しないよう、対象日Dごとに次の条件をそろえる:
  - 発表時刻: 前日D-1の19:30(本番の日次予測の実行時刻)。lead_hoursはここから数える。
  - 学習: 発表時刻より前の実測(人による訂正を反映したもの)だけで学習し直す。
  - しきい値: 発表時刻の直前VALIDATION_WINDOW_DAYS日で、予報を入力にした場合にF1が最大の値
    (その期間の川霧が少なすぎれば前日の値)。
  - 入力: 対象日時に実際に出されていた予報(Open-Meteoのhistorical-forecast-api)。本番の
    前日夜の予報(8〜24時間先)より少し新しい予報になるため、実際よりわずかに有利な条件になる。
  - 木の設定(葉の数等)と本数は現行モデルの値を使う(日ごとの探索は行わない)。

同じ(地点, 日時, 発表時刻)の行は追加しないので、何度実行しても重複しない。
実行: .venv/bin/python hindcast_lightgbm.py [--dry-run]
"""
import argparse
import logging
import sys

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from common import BASE_DIR, STATION_WEATHER_DB_PATH, load_camera_coords, now_jst_naive, representative_coords
import fog_data
import fog_model as fm
import prediction_log
import station_weather
from fog_features import prepare_features

DB_PATH = BASE_DIR / "data" / "predictions.db"
HINDCAST_MODEL_TYPE = fm.MODEL_TYPE_LABEL + "[再予測]"
ISSUE_HOUR, ISSUE_MINUTE = 19, 30
OBSERVATION_START_HOUR, OBSERVATION_END_HOUR = 4, 19

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger("hindcast")


def _old_model_targets(conn):
    """旧モデルが予測していて、答え合わせ済みの(地点, 日時) -> 実測コード。"""
    rows = conn.execute(
        """SELECT station, target_datetime, MAX(actual_code) FROM predictions
           WHERE actual_code IS NOT NULL AND model_type NOT LIKE ?
           GROUP BY station, target_datetime""",
        (fm.MODEL_TYPE_LABEL + "%",),
    ).fetchall()
    return {(s, t): a for s, t, a in rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="予測するがDBには書き込まない")
    args = parser.parse_args()
    load_dotenv(BASE_DIR / ".env")

    state = fm.load_model_state()
    current = fm._shared_info(state)
    if current is None:
        log.error("The current model is not the shared binary LightGBM; run predict.py first")
        return 1
    params, n_estimators = current["pipe"].params, current["pipe"].n_estimators
    log.info("Using params %s, %d trees (from the current model)", params, n_estimators)

    conn = prediction_log.connect(str(DB_PATH))
    targets = _old_model_targets(conn)
    if not targets:
        log.info("No old-model predictions to re-predict")
        return 0
    target_df = pd.DataFrame([(s, pd.Timestamp(t), a) for (s, t), a in targets.items()],
                             columns=["station", "datetime", "actual_code"])
    days = sorted(target_df["datetime"].dt.normalize().unique())
    log.info("%d old-model (station, datetime) target(s) over %d day(s): %s - %s",
             len(target_df), len(days), days[0].date(), days[-1].date())

    lat, lon = representative_coords()
    coords = load_camera_coords()
    main_df, phenom_df, names = fog_data.load_river_fog_data()
    frame, wf, medians, station_coords = fm._build_training_frame(
        main_df, phenom_df, names, lat, lon, camera_coords=coords,
        weather_conn=station_weather.connect(STATION_WEATHER_DB_PATH),
    )

    first_val = days[0] - pd.Timedelta(days=fm.VALIDATION_WINDOW_DAYS + 3)
    hist = fm.fetch_historical_forecast_multi(station_coords, first_val.date(), days[-1].date())
    log.info("Fetched historical forecasts for %d/%d station(s)", len(hist), len(station_coords))
    # 予報を入力にした特徴量: しきい値選択用(実測ラベル付きの行)と、予測対象の全行
    fc_labeled = fm._forecast_input_rows(frame[frame["__datetime"] >= first_val], hist, station_coords, wf, medians)
    fc_by_station = {}
    for st, df in hist.items():
        feats, _, _ = prepare_features(df, *station_coords[st], wf, medians=medians)
        fc_by_station[st] = feats[["datetime", *wf]].drop_duplicates("datetime").set_index("datetime")

    prev_threshold = None
    records = []
    for day in days:
        issue = day - pd.Timedelta(days=1) + pd.Timedelta(hours=ISSUE_HOUR, minutes=ISSUE_MINUTE)
        train = frame[frame["__datetime"] < issue]
        val_start = issue - pd.Timedelta(days=fm.VALIDATION_WINDOW_DAYS)
        inner = train[train["__datetime"] <= val_start]
        if len(train) < fm.MIN_TRAIN_ROWS or int(train["__y"].sum()) < fm.MIN_TRAIN_POSITIVES or inner["__y"].nunique() < 2:
            log.warning("%s: not enough training data before %s; skipped", day.date(), issue)
            continue

        # しきい値(発表時刻の直前の検証期間、予報を入力に)
        val_fc = fc_labeled[(fc_labeled["__datetime"] > val_start) & (fc_labeled["__datetime"] < issue)]
        if int(val_fc["__y"].sum()) >= fm.MIN_POSITIVES_FOR_THRESHOLD:
            r_in, g_in = fm._station_rates(inner)
            inner_model = fm._fit_lgbm(fm._features_with_rate(inner, wf, r_in, g_in), inner["__y"].to_numpy(),
                                       params, n_estimators)
            p_val = inner_model.predict_proba(fm._features_with_rate(val_fc, wf, r_in, g_in))[:, 1]
            threshold, _ = fm._best_threshold(val_fc["__y"].to_numpy(), p_val)
            source = "validation"
        else:
            threshold = prev_threshold if prev_threshold is not None else fm.DEFAULT_FOG_THRESHOLD
            source = "previous/default"
        prev_threshold = threshold

        rates, global_rate = fm._station_rates(train)
        model = fm._fit_lgbm(fm._features_with_rate(train, wf, rates, global_rate), train["__y"].to_numpy(),
                             params, n_estimators)

        day_targets = target_df[target_df["datetime"].dt.normalize() == day]
        n_day = n_fog = 0
        for st, group in day_targets.groupby("station"):
            feats = fc_by_station.get(st)
            if feats is None:
                continue
            X = feats.reindex(group["datetime"])
            ok = X.notna().all(axis=1).to_numpy()
            if not ok.any():
                continue
            X = X[ok].copy()
            X[fm.STATION_FOG_RATE_FEATURE] = rates.get(st, global_rate)
            probs = model.predict_proba(X[[*wf, fm.STATION_FOG_RATE_FEATURE]])[:, 1]
            preds = (probs >= threshold).astype(int)
            for (dt, actual), p, pred in zip(group[ok][["datetime", "actual_code"]].itertuples(index=False, name=None),
                                             probs, preds):
                lead = (dt - issue).total_seconds() / 3600.0
                records.append((issue.strftime("%Y-%m-%d %H:%M:%S"), st, dt.strftime("%Y-%m-%d %H:%M:%S"),
                                lead, int(pred), float(p), HINDCAST_MODEL_TYPE, int(actual)))
                n_day += 1
                n_fog += int(pred)
        log.info("%s: issued %s, trained on %d rows, threshold=%.2f (%s), predicted %d row(s), %d as fog",
                 day.date(), issue, len(train), threshold, source, n_day, n_fog)

    log.info("Re-predicted %d of %d target(s)", len(records), len(target_df))
    if records:
        y = np.array([fm.is_fog_code(r[7]) for r in records], dtype=int)
        pred = np.array([r[4] for r in records]); prob = np.array([r[5] for r in records])
        m = fm.binary_metrics(y, pred, prob)
        log.info("Re-prediction scores: F1=%s recall=%s precision=%s AP=%s Brier=%s",
                 fm._fmt(m["f1"]), fm._fmt(m["recall"]), fm._fmt(m["precision"]), fm._fmt(m.get("ap")),
                 fm._fmt(m.get("brier"), 4))
    if args.dry_run or not records:
        log.info("Dry run: nothing written" if args.dry_run else "Nothing to write")
        return 0
    verified_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        before = conn.total_changes
        conn.executemany(
            """INSERT OR IGNORE INTO predictions
               (run_at, station, target_datetime, lead_hours, predicted_code, predicted_fog_prob, model_type,
                actual_code, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [r + (verified_at,) for r in records],
        )
        log.info("Inserted %d row(s) into %s", conn.total_changes - before, DB_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
