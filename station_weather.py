"""地点ごとの気象実測(Open-Meteo)のSQLiteベースの記録。

2026-08-28以前は、システム全体で代表1地点(WEATHER_LAT/WEATHER_LON)の気象値を
只見川.xlsxに1時刻1行で記録し、全33地点で共通の値として使っていた。cameras.csvに
各地点の実測緯度経度を追加したのを機に、collect.pyの毎時収集はこれ以降、全33地点分の
気象値もここにあわせて記録する。

只見川.xlsx側の代表地点の値は今まで通り書き込みを続ける(過去の学習データとの
互換性のため)。ここに記録されるのはこの変更以降の分のみで、過去にはさかのぼらない。
fog_model.pyの学習・予報は、地点ごとの記録があればそれを優先し、無い日時は
只見川.xlsx側の代表値にフォールバックする。
"""
import sqlite3

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS station_weather (
    station_name TEXT NOT NULL,
    datetime TEXT NOT NULL,
    temperature REAL,
    precipitation REAL,
    wind_speed REAL,
    wind_direction_deg REAL,
    solar_radiation_mj REAL,
    sunshine_hours REAL,
    dew_point REAL,
    humidity REAL,
    visibility_km REAL,
    PRIMARY KEY (station_name, datetime)
);
"""


def connect(db_path):
    # collect.py(毎時)・backfill_weather.py・Webの複数プロセスが同じDBファイルに
    # 書き込みうるため、busy_timeoutで他プロセスの書き込み中も即座にエラーにせず
    # 待たせる(weather_store.pyと同じ対策)。
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def record_weather(conn, station_name, dt_str, weather):
    """weather: collect.pyのfetch_weather()と同じ形のdict(値が無ければNoneでもよい)。
    同じ(station_name, datetime)は上書き(UPSERT)する。"""
    weather = weather or {}
    with conn:
        conn.execute(
            """INSERT INTO station_weather
               (station_name, datetime, temperature, precipitation, wind_speed,
                wind_direction_deg, solar_radiation_mj, sunshine_hours, dew_point,
                humidity, visibility_km)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(station_name, datetime) DO UPDATE SET
                 temperature=excluded.temperature,
                 precipitation=excluded.precipitation,
                 wind_speed=excluded.wind_speed,
                 wind_direction_deg=excluded.wind_direction_deg,
                 solar_radiation_mj=excluded.solar_radiation_mj,
                 sunshine_hours=excluded.sunshine_hours,
                 dew_point=excluded.dew_point,
                 humidity=excluded.humidity,
                 visibility_km=excluded.visibility_km""",
            (
                station_name, dt_str,
                weather.get("temperature"), weather.get("precipitation"),
                weather.get("wind_speed"), weather.get("wind_direction_deg"),
                weather.get("solar_radiation_mj"), weather.get("sunshine_hours"),
                weather.get("dew_point"), weather.get("humidity"),
                weather.get("visibility_km"),
            ),
        )


def load_latest_station_weather(conn, station_name):
    """指定地点の最新1件の気象実測を辞書で返す(記録が無ければNone)。
    station_detail.htmlの「最新の気象情報」表示に使う。"""
    row = conn.execute(
        "SELECT datetime, temperature, precipitation, wind_speed, wind_direction_deg, "
        "solar_radiation_mj, sunshine_hours, dew_point, humidity, visibility_km "
        "FROM station_weather WHERE station_name = ? ORDER BY datetime DESC LIMIT 1",
        (station_name,),
    ).fetchone()
    if row is None:
        return None
    cols = ["datetime", "temperature", "precipitation", "wind_speed", "wind_direction_deg",
            "solar_radiation_mj", "sunshine_hours", "dew_point", "humidity", "visibility_km"]
    return dict(zip(cols, row))


def load_station_weather_df(conn, station_name):
    """指定地点の気象実測をdatetime昇順のDataFrameで返す(記録が無ければ空のDataFrame)。
    列名はOpen-Meteo由来のキー名のまま(fog_features.py列名への変換は呼び出し側で行う)。
    """
    df = pd.read_sql_query(
        "SELECT datetime, temperature, precipitation, wind_speed, wind_direction_deg, "
        "solar_radiation_mj, sunshine_hours, dew_point, humidity, visibility_km "
        "FROM station_weather WHERE station_name = ? ORDER BY datetime",
        conn, params=(station_name,),
    )
    if not df.empty:
        df["datetime"] = pd.to_datetime(df["datetime"])
    return df


__all__ = ["connect", "record_weather", "load_station_weather_df", "load_latest_station_weather"]
