"""weather_store.py(SQLite)を読み込み、霧予測モデル用のDataFrameに変換する。

以前は只見川.xlsxをopenpyxlで直接読んでいたが、read_onlyモードでも1回の読み込みが
行数に比例して重く、19,000行を超えた現状では10秒以上かかっていた(mtimeキャッシュで
場当たり的に緩和していたが、collect.py等が書き込むたびに結局再読込が必要だった)。
実データはweather_store.py(SQLite)に移し、ここはそれをfog_features.py用の列名に
変換するだけの薄い読み込み層にした。只見川.xlsx自体はエクスポート専用として残る
(weather_store.export_to_xlsx、管理画面からの手動書き出し)。

common.pyの固定列レイアウトは、SQLiteへの初回移行(weather_store.import_from_xlsx)と
xlsxエクスポート時にのみ引き続き前提とする。
"""
import csv
import threading

import pandas as pd

from common import CAMERAS_CSV, WEATHER_DB_PATH
import weather_store
from fog_features import (
    COL_DEWPOINT as F_DEWPOINT,
    COL_HUMIDITY as F_HUMIDITY,
    COL_PRECIP as F_PRECIP,
    COL_SOLAR as F_SOLAR,
    COL_SUNSHINE as F_SUNSHINE,
    COL_TEMP as F_TEMP,
    COL_VISIBILITY as F_VISIBILITY,
    COL_WIND_DIR_DEG as F_WIND_DIR_DEG,
    COL_WIND_SPEED as F_WIND_SPEED,
)


def load_station_names(cameras_csv=CAMERAS_CSV):
    with open(cameras_csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [row["地点名"] for row in reader]


# webapp.py側のほぼ全ページがリクエストのたびにload_river_fog_data()を呼ぶため、
# SQLiteとはいえ毎回クエリ・DataFrame組み立てをするのは無駄が大きい。DBファイルの
# 更新時刻(mtime)をキーにキャッシュし、collect.py等が実際に書き込むまでは再読込しない
# (旧xlsx版と同じ考え方をそのまま踏襲)。
_cache_lock = threading.Lock()
_cache = {"db_path": None, "mtime": None, "data": None}


def load_river_fog_data(db_path=WEATHER_DB_PATH, cameras_csv=CAMERAS_CSV):
    """(main_df, phenom_df, station_names) を返す。呼び出し元ごとに独立した
    DataFrameのコピーを返すので、呼び出し側で自由に加工してよい(キャッシュ本体は
    汚染されない)。

    main_df  : datetime + 気象特徴量（fog_features.py の列名）
    phenom_df: datetime + 地点名ごとの現象コード生値（"/" / 数値 / None）
    """
    try:
        mtime = db_path.stat().st_mtime
    except OSError:
        mtime = None
    with _cache_lock:
        if _cache["db_path"] == db_path and _cache["mtime"] == mtime and _cache["data"] is not None:
            main_df, phenom_df, station_names = _cache["data"]
            return main_df.copy(), phenom_df.copy(), list(station_names)

    main_df, phenom_df, station_names = _load_river_fog_data_uncached(db_path, cameras_csv)

    with _cache_lock:
        _cache["db_path"] = db_path
        _cache["mtime"] = mtime
        _cache["data"] = (main_df, phenom_df, station_names)
    return main_df.copy(), phenom_df.copy(), list(station_names)


def _load_river_fog_data_uncached(db_path, cameras_csv):
    """load_river_fog_data()の実処理(キャッシュ未ヒット時のみ呼ばれる)。"""
    station_names = load_station_names(cameras_csv)

    conn = weather_store.connect(db_path)
    try:
        raw = weather_store.load_weather_df(conn)
        phenom_df = weather_store.load_phenomena_wide_df(conn, station_names)
    finally:
        conn.close()

    main_df = pd.DataFrame({
        "datetime": raw["datetime"],
        F_TEMP: raw["temperature"],
        F_PRECIP: raw["precipitation"],
        F_WIND_SPEED: raw["wind_speed"],
        F_WIND_DIR_DEG: raw["wind_direction_deg"],
        F_SOLAR: raw["solar_radiation_mj"],
        F_SUNSHINE: raw["sunshine_hours"],
        F_DEWPOINT: raw["dew_point"],
        F_HUMIDITY: raw["humidity"],
        F_VISIBILITY: raw["visibility_km"],
    })
    for col in (F_TEMP, F_PRECIP, F_WIND_SPEED, F_SOLAR, F_SUNSHINE, F_DEWPOINT, F_HUMIDITY, F_VISIBILITY):
        main_df[col] = pd.to_numeric(main_df[col], errors="coerce")
    return main_df, phenom_df, station_names
