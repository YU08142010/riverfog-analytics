"""collect.py と backfill_weather.py で共有する定数・ヘルパー。"""
from datetime import timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_XLSX = BASE_DIR / "data" / "只見川.xlsx"
TEMPLATE_XLSX = BASE_DIR / "template" / "header_template.xlsx"
CAMERAS_CSV = BASE_DIR / "cameras.csv"

JST = timezone(timedelta(hours=9))

QUALITY_INFO_VALUE = 8
HOMOGENEITY_NUMBER_VALUE = 1

WIND_DIR_DEGREES = {
    "北": 0.0, "北北東": 22.5, "北東": 45.0, "東北東": 67.5,
    "東": 90.0, "東南東": 112.5, "南東": 135.0, "南南東": 157.5,
    "南": 180.0, "南南西": 202.5, "南西": 225.0, "西南西": 247.5,
    "西": 270.0, "西北西": 292.5, "北西": 315.0, "北北西": 337.5,
}
DEGREES_TO_WIND_DIR = {v: k for k, v in WIND_DIR_DEGREES.items()}

# xlsx列レイアウト（1始まり）。test.xlsxの本番形式に合わせている。
COL_DATETIME = 1
COL_TEMP = 2
COL_TEMP_QI = 3
COL_TEMP_HN = 4
COL_PRECIP = 5
COL_PRECIP_QI = 6
COL_PRECIP_HN = 7
COL_WIND_SPEED = 8
COL_WIND_SPEED_QI = 9
COL_WIND_DIR = 10
COL_WIND_DIR_QI = 11
COL_WIND_SPEED_HN = 12
COL_SOLAR = 13
COL_SOLAR_QI = 14
COL_SOLAR_HN = 15
COL_SUNSHINE = 16
COL_SUNSHINE_QI = 17
COL_SUNSHINE_HN = 18
COL_DEWPOINT = 19
COL_DEWPOINT_QI = 20
COL_DEWPOINT_HN = 21
COL_HUMIDITY = 22
COL_HUMIDITY_QI = 23
COL_HUMIDITY_HN = 24
COL_VISIBILITY = 25
COL_VISIBILITY_QI = 26
COL_VISIBILITY_HN = 27
# 28列目はスペーサー（空列）
COL_PHENOM_START = 29


def wind_degrees_to_direction_ja(degrees):
    if degrees is None:
        return None
    nearest = min(WIND_DIR_DEGREES.values(), key=lambda d: min(abs(d - degrees), 360 - abs(d - degrees)))
    return DEGREES_TO_WIND_DIR[nearest]


def set_measure(ws, row, col_value, col_qi, col_hn, value):
    """気象値セル1組（値・QI・均質番号）に値を設定する。値がNoneなら3セルとも空にする。"""
    if value is None:
        ws.cell(row=row, column=col_value).value = None
        ws.cell(row=row, column=col_qi).value = None
        ws.cell(row=row, column=col_hn).value = None
    else:
        ws.cell(row=row, column=col_value).value = value
        ws.cell(row=row, column=col_qi).value = QUALITY_INFO_VALUE
        ws.cell(row=row, column=col_hn).value = HOMOGENEITY_NUMBER_VALUE
