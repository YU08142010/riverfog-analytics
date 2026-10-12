"""研究用の書き出し(2026-10-12、webappの /export)。期間と地点を選んで、観測・地点の気象・予測・かすみ度・画像を
1つのZIPにまとめる。列の意味と注意点を書いた README.txt を必ず同梱する。"""
import csv
import io
import sqlite3
import zipfile
from datetime import datetime

from common import BASE_DIR

WEATHER_DB = BASE_DIR / "data" / "weather.db"
STATION_WEATHER_DB = BASE_DIR / "data" / "station_weather.db"
PRED_DB = BASE_DIR / "data" / "predictions.db"
HAZE_DB = BASE_DIR / "data" / "haze.db"
USERS_DB = BASE_DIR / "data" / "users.db"
IMAGE_DIR = BASE_DIR / "data" / "images"
MAX_DAYS = 62
MAX_IMAGES = 3000

README = """川霧観測・予測 研究用データ
作成: {created}
期間: {start} 〜 {end}
地点: {n_stations}地点({stations})

■ observations.csv(カメラ画像の現象の判定、毎時)
  datetime, station, region, code, label, source, daylight
  code: 0=現象なし 1〜3=川霧(薄い・普通・濃い) 4〜6=全体霧 7〜8=層雲 9=霧雨 10=雨。
        2つの現象が同時のときは「小さい方×100+大きい方」(例: 105 = 薄い川霧+全体霧)。-1=画像が判定に使えない。
  source: ai = カメラ画像のAI判定(Gemini)、human = 人が訂正したもの。
  daylight: 1 = 日の出〜日の入り。0の時刻は画像が暗く、霧があっても判定できないことが多い。
  注意: AIの判定には誤りが含まれる(人が確認した範囲では、川霧の判定の誤りが少なくない)。
{weather}{pred}{haze}{images}
■ 出典・利用上の注意
  カメラ画像: 国土交通省「川の防災情報」の河川カメラ。画像の再配布・公開は提供元の利用条件を確認すること。
  気象値: Open-Meteo(既定は気象庁のモデル)。
"""
WEATHER_DOC = """
■ station_weather.csv(地点ごとの気象、毎時)
  Open-Meteoのモデル値(地点の座標の格子)。地上での実測ではない。
"""
PRED_DOC = """
■ predictions.csv(本番の予測の記録)
  run_at = 予測した時刻(毎晩19:30ごろ)、target_datetime = 対象の時刻、lead_hours = 何時間先か、
  predicted_fog_prob = 川霧の確率、predicted = 1なら「川霧あり」と予測(しきい値以上)、actual_code = 実測の現象コード(上と同じ)。
  model_typeに「[再予測]」が付く行は、過去の日時を後から現行モデルで予測し直したもの(当時の予測ではない)。
"""
HAZE_DOC = """
■ haze.csv(かすみ度、画像処理)
  同じカメラの「見通しの良い時」の画像(ref_datetime)と比べた、輝度の散らばりの比(contrast_ratio)・輪郭の比(edge_ratio)・
  ダークチャネルの差(dark_diff)・明るさの差(bright_diff)。score = 0〜100(輪郭の失われ方)。日中の画像だけ。
"""
IMAGES_DOC = """
■ images/(カメラ画像)
  images/<日付>/<時刻>/<地点>.jpg。{image_hours}の画像(最大{max_images}枚)。
"""


def _rows(db, sql, params):
    if not db.exists():
        return [], []
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = c.execute(sql, params)
        return [d[0] for d in cur.description], cur.fetchall()
    finally:
        c.close()


def _csv_bytes(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")  # Excelで文字化けしないようBOM付き


def build_zip(dest, start, end, stations, regions, parts, image_hours, daylight_fn, phenom_label_fn):
    """dest(ファイルのパス)にZIPを書く。戻り値: 中身の件数のdict。"""
    s, e = f"{start} 00:00:00", f"{end} 23:59:59"
    marks = ",".join("?" * len(stations))
    stats = {}
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        # 観測(人の訂正を反映)
        _, ph = _rows(WEATHER_DB, f"SELECT datetime, station_name, code FROM phenomena WHERE datetime BETWEEN ? AND ? "
                                   f"AND station_name IN ({marks}) ORDER BY datetime, station_name", (s, e, *stations))
        _, corr = _rows(USERS_DB, f"SELECT target_datetime, station_name, corrected_code FROM phenomena_corrections "
                                  f"WHERE target_datetime BETWEEN ? AND ? AND station_name IN ({marks})", (s, e, *stations))
        corr = {(d, st): c for d, st, c in corr}
        rows = []
        for dt, st, code in ph:
            src = "ai"
            if (dt, st) in corr:
                code, src = corr[(dt, st)], "human"
            rows.append((dt, st, regions.get(st, ""), "" if code is None else code,
                         phenom_label_fn(code) if code is not None and code >= 0 else "", src, 1 if daylight_fn(st, dt) else 0))
        zf.writestr("observations.csv", _csv_bytes(["datetime", "station", "region", "code", "label", "source", "daylight"], rows))
        stats["observations"] = len(rows)
        if "weather" in parts:
            h, rows = _rows(STATION_WEATHER_DB, f"SELECT * FROM station_weather WHERE datetime BETWEEN ? AND ? "
                                                f"AND station_name IN ({marks}) ORDER BY datetime, station_name", (s, e, *stations))
            zf.writestr("station_weather.csv", _csv_bytes(h, rows))
            stats["station_weather"] = len(rows)
        if "predictions" in parts:
            h, rows = _rows(PRED_DB, "SELECT run_at, station, target_datetime, lead_hours, predicted_fog_prob, "
                                     "predicted_code AS predicted, actual_code, model_type FROM predictions WHERE target_datetime "
                                     f"BETWEEN ? AND ? AND station IN ({marks}) ORDER BY target_datetime, station, run_at",
                            (s, e, *stations))
            zf.writestr("predictions.csv", _csv_bytes(h, rows))
            stats["predictions"] = len(rows)
        if "haze" in parts:
            import haze_index
            h, rows = _rows(HAZE_DB, "SELECT datetime, station, contrast_ratio, edge_ratio, dark_diff, bright_diff, ref_datetime "
                                     f"FROM haze WHERE datetime BETWEEN ? AND ? AND station IN ({marks}) ORDER BY datetime, station",
                            (s, e, *stations))
            rows = [(*r, haze_index.score(r[3], r[4])) for r in rows]
            zf.writestr("haze.csv", _csv_bytes([*h, "score"], rows))
            stats["haze"] = len(rows)
        n_img = 0
        if "images" in parts:
            for dt, st, _code in ph:
                if n_img >= MAX_IMAGES:
                    break
                if int(dt[11:13]) not in image_hours:
                    continue
                p = IMAGE_DIR / dt[:10] / (dt[11:13] + dt[14:16]) / f"{st}.jpg"
                if p.is_file():
                    zf.write(p, f"images/{dt[:10]}/{dt[11:13]}{dt[14:16]}/{st}.jpg", compress_type=zipfile.ZIP_STORED)
                    n_img += 1
            stats["images"] = n_img
        hours_text = f"{min(image_hours)}〜{max(image_hours)}時" if image_hours else "-"
        zf.writestr("README.txt", README.format(
            created=datetime.now().strftime("%Y-%m-%d %H:%M"), start=start, end=end, n_stations=len(stations),
            stations="・".join(stations[:20]) + (" ほか" if len(stations) > 20 else ""),
            weather=WEATHER_DOC if "weather" in parts else "", pred=PRED_DOC if "predictions" in parts else "",
            haze=HAZE_DOC if "haze" in parts else "",
            images=IMAGES_DOC.format(image_hours=hours_text, max_images=MAX_IMAGES) if "images" in parts else "",
        ).encode("utf-8"))
    return stats
