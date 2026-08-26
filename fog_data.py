"""只見川.xlsxを読み込み、霧予測モデル用のDataFrameに変換する。

common.pyの固定列レイアウトを前提とする（列自動検出は行わない）。
"""
import csv
from datetime import datetime

import openpyxl
import pandas as pd

from common import (
    CAMERAS_CSV,
    COL_DATETIME,
    COL_DEWPOINT,
    COL_HUMIDITY,
    COL_PHENOM_START,
    COL_PRECIP,
    COL_SOLAR,
    COL_SUNSHINE,
    COL_TEMP,
    COL_VISIBILITY,
    COL_WIND_DIR,
    COL_WIND_SPEED,
    DATA_XLSX,
)
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
    encode_wind_direction_degrees,
)


def load_station_names(cameras_csv=CAMERAS_CSV):
    with open(cameras_csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [row["地点名"] for row in reader]


def load_river_fog_data(xlsx_path=DATA_XLSX, cameras_csv=CAMERAS_CSV):
    """(main_df, phenom_df, station_names) を返す。
    main_df  : datetime + 気象特徴量（fog_features.py の列名）
    phenom_df: datetime + 地点名ごとの現象コード生値（"/" / 数値 / None）

    openpyxlのread_onlyモードでは ws.cell(row, col) によるセル単位のランダムアクセスは
    行ごとに内部XMLの再走査が発生し、行数に対してほぼ二乗オーダーで遅くなる
    （データ件数がまだ少ない段階でも数分かかることを確認済み）。
    必ず iter_rows(values_only=True) で行単位にまとめて取得すること。
    """
    station_names = load_station_names(cameras_csv)
    n_cols = COL_PHENOM_START - 1 + len(station_names)

    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    ws = wb.active

    main_rows = []
    phenom_rows = []
    for values in ws.iter_rows(min_row=1, max_col=n_cols, values_only=True):
        dt = values[COL_DATETIME - 1]
        if not isinstance(dt, datetime):
            continue  # 見出し行・小見出し行をスキップ

        main_rows.append({
            "datetime": dt,
            F_TEMP: values[COL_TEMP - 1],
            F_PRECIP: values[COL_PRECIP - 1],
            F_WIND_SPEED: values[COL_WIND_SPEED - 1],
            F_WIND_DIR_DEG: encode_wind_direction_degrees(values[COL_WIND_DIR - 1]),
            F_SOLAR: values[COL_SOLAR - 1],
            F_SUNSHINE: values[COL_SUNSHINE - 1],
            F_DEWPOINT: values[COL_DEWPOINT - 1],
            F_HUMIDITY: values[COL_HUMIDITY - 1],
            F_VISIBILITY: values[COL_VISIBILITY - 1],
        })

        phenom_row = {"datetime": dt}
        for i, name in enumerate(station_names):
            phenom_row[name] = values[COL_PHENOM_START - 1 + i]
        phenom_rows.append(phenom_row)
    wb.close()

    main_df = pd.DataFrame(main_rows)
    for col in (F_TEMP, F_PRECIP, F_WIND_SPEED, F_SOLAR, F_SUNSHINE, F_DEWPOINT, F_HUMIDITY, F_VISIBILITY):
        main_df[col] = pd.to_numeric(main_df[col], errors="coerce")
    phenom_df = pd.DataFrame(phenom_rows)
    return main_df, phenom_df, station_names
