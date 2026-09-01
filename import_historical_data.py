"""data/historical_incoming/ に置かれた過去観測xlsxを、本番の只見川.xlsxへ合流させる
一回限りの移行スクリプト。

過去に撮り貯めていたxlsxは気象庁のダウンロード形式そのもの（見出し6行＋データ行）で、
気象項目のブロック(2〜28列目: 気温・降水量・風速・日射量・日照時間・露点温度・相対湿度・
視程・水温、それぞれ品質情報/均質番号付き)は本番の只見川.xlsxと列位置が完全に一致する。
一方、地点(現象コード)の列は本番の33地点の中の一部だけを含み、開始列や並び順もファイルに
よって異なりうるため、3行目の地点名見出しを都度スキャンして cameras.csv 上の地点名と
突き合わせる(位置決め打ちはしない。一致しない地点名は取り込まずログに警告するだけで、
推測でマッピングしたりデータを捏造したりしない)。

安全のため:
- 本番xlsxに既に存在する日時は絶対に上書きしない(スキップしてログに件数だけ残す)。
- 書き込み前に data/backups/ へ丸ごとバックアップを取る(build_data_backup_zip)。
- collect.py 等との競合を避けるため xlsx_write_lock を取得してから書き込む。
- --dry-run では実際の書き込み・バックアップ・ファイル移動を一切行わず、
  取り込み件数・日時範囲・地点マッチング結果のサマリーだけを表示する。
- 取り込みに成功したファイルは data/historical_incoming/imported/ へ移動し、
  誤って二重取り込みしないようにする。
"""
import logging
import logging.handlers
import sys
from datetime import datetime

import openpyxl

from common import (
    BASE_DIR,
    COL_DATETIME,
    COL_PHENOM_START,
    DATA_XLSX,
    JST,
    XlsxLockTimeout,
    build_data_backup_zip,
    now_jst_naive,
    xlsx_write_lock,
)
from fog_data import load_station_names

INCOMING_DIR = BASE_DIR / "data" / "historical_incoming"
IMPORTED_DIR = INCOMING_DIR / "imported"
BACKUP_DIR = BASE_DIR / "backups"

# 気象ブロックは2〜28列目(気温3+降水量3+風速5+日射量3+日照時間3+露点温度3+相対湿度3+
# 視程3+水温1=27列)。本番の只見川.xlsxと列位置が一致することを、代表的な見出し文字列で
# 都度確認してから使う(一致しなければ想定外のフォーマットとして中断する)。
WEATHER_BLOCK_FIRST_COL = 2
WEATHER_BLOCK_LAST_COL = 28
EXPECTED_HEADER_ROW = 4
EXPECTED_HEADERS = {
    1: "年月日時",
    2: "気温(℃)",
    5: "降水量(mm)",
    8: "風速(m/s)",
    28: "水温（℃）",
}
STATION_HEADER_ROW = 3
DATA_START_ROW = 7

LOG_FILE = BASE_DIR / "logs" / "import_historical_data.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3

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
log = logging.getLogger("import_historical_data")


class UnexpectedLayout(Exception):
    """想定していた気象庁ダウンロード形式と違う場合に送出する(推測で読み進めない)。"""


def _normalize_station_name(raw):
    return str(raw).replace("\n", "").strip()


def _detect_station_columns(ws, current_station_names):
    """3行目を走査し、「只見」でも空でもないセルを地点名見出しとみなす。
    現在の33地点名(cameras.csv)と一致するものだけを {列番号: 地点名} で返す。
    一致しない見出しはunmatchedに集めて呼び出し側でログに出す(黙って捨てない)。"""
    header_row = next(ws.iter_rows(min_row=STATION_HEADER_ROW, max_row=STATION_HEADER_ROW, values_only=True))
    matched = {}
    unmatched = []
    current_set = set(current_station_names)
    for col_idx, raw in enumerate(header_row, start=1):
        if raw is None or raw == "只見":
            continue
        name = _normalize_station_name(raw)
        if name in current_set:
            matched[col_idx] = name
        else:
            unmatched.append((col_idx, name))
    return matched, unmatched


def _validate_weather_layout(ws, path):
    header_row = next(ws.iter_rows(min_row=EXPECTED_HEADER_ROW, max_row=EXPECTED_HEADER_ROW, values_only=True))
    for col_idx, expected in EXPECTED_HEADERS.items():
        actual = header_row[col_idx - 1] if col_idx - 1 < len(header_row) else None
        if actual != expected:
            raise UnexpectedLayout(
                f"{path.name}: {EXPECTED_HEADER_ROW}行目{col_idx}列目が想定と違います "
                f"(期待: {expected!r}, 実際: {actual!r})。想定外のフォーマットのため中断します。"
            )


def read_historical_file(path, current_station_names):
    """(dt, weather_values, phenom_by_name) のリストを返す。
    weather_valuesは2〜28列目(27個)をそのままの並びで持つタプル。
    phenom_by_nameは{地点名: 生の現象コード値}(一致した地点だけ)。"""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        ws = wb.active
        _validate_weather_layout(ws, path)
        station_cols, unmatched = _detect_station_columns(ws, current_station_names)
        if unmatched:
            for col_idx, name in unmatched:
                log.warning(
                    "%s: %d列目の地点名 %r は現在の地点一覧(cameras.csv)に無いため取り込みません",
                    path.name, col_idx, name,
                )
        if not station_cols:
            log.warning("%s: 現在の地点名と一致する列が1つもありませんでした", path.name)

        rows = []
        for values in ws.iter_rows(min_row=DATA_START_ROW, values_only=True):
            dt = values[COL_DATETIME - 1]
            if not isinstance(dt, datetime):
                continue
            weather_values = tuple(
                values[c - 1] if c - 1 < len(values) else None
                for c in range(WEATHER_BLOCK_FIRST_COL, WEATHER_BLOCK_LAST_COL + 1)
            )
            phenom_by_name = {}
            for col_idx, name in station_cols.items():
                v = values[col_idx - 1] if col_idx - 1 < len(values) else None
                if v is not None:
                    phenom_by_name[name] = v
            rows.append((dt, weather_values, phenom_by_name))
        return rows
    finally:
        wb.close()


def _read_existing_rows(ws, n_cols):
    """本番xlsxの既存データ行を {dt: 全列の値タプル} で返す(小規模なので全読み込みでよい)。"""
    existing = {}
    for values in ws.iter_rows(min_row=2, max_col=n_cols, values_only=True):
        dt = values[COL_DATETIME - 1]
        if isinstance(dt, datetime):
            existing[dt] = values
    return existing


def build_merged_rows(existing_by_dt, new_rows, name_to_col, n_cols):
    """既存行 + 新規の過去データ行 を日時順にマージした「全列値タプルのリスト」を返す。
    (insert_at, skipped_dt件数, 取り込んだ日時範囲) も合わせて返す。"""
    merged = dict(existing_by_dt)
    skipped = 0
    inserted_dts = []
    for dt, weather_values, phenom_by_name in new_rows:
        if dt in merged:
            skipped += 1
            continue
        row = [None] * n_cols
        row[COL_DATETIME - 1] = dt
        for offset, v in enumerate(weather_values):
            col = WEATHER_BLOCK_FIRST_COL + offset
            if col - 1 < n_cols:
                row[col - 1] = v
        for name, v in phenom_by_name.items():
            col = name_to_col[name]
            if col - 1 < n_cols:
                row[col - 1] = v
        merged[dt] = tuple(row)
        inserted_dts.append(dt)
    ordered = [merged[dt] for dt in sorted(merged.keys())]
    return ordered, skipped, inserted_dts


def main():
    dry_run = "--dry-run" in sys.argv[1:]

    if not DATA_XLSX.exists():
        log.error("本番xlsxが見つかりません: %s", DATA_XLSX)
        return

    incoming_files = sorted(p for p in INCOMING_DIR.glob("*.xlsx") if p.is_file())
    if not incoming_files:
        log.info("取り込み対象のファイルがありません: %s", INCOMING_DIR)
        return

    current_station_names = load_station_names()
    name_to_col = {name: COL_PHENOM_START + i for i, name in enumerate(current_station_names)}
    n_cols = COL_PHENOM_START + len(current_station_names) - 1

    log.info("=== 過去データ取り込み開始%s ===", " (dry-run)" if dry_run else "")
    log.info("対象ファイル: %s", ", ".join(p.name for p in incoming_files))

    all_new_rows = []
    for path in incoming_files:
        try:
            rows = read_historical_file(path, current_station_names)
        except UnexpectedLayout as e:
            log.error("%s", e)
            log.error("このファイルはスキップします: %s", path.name)
            continue
        log.info("%s: %d行読み込み", path.name, len(rows))
        all_new_rows.extend(rows)

    if not all_new_rows:
        log.info("有効な行が1件も読み込めませんでした。処理を終了します。")
        return

    # 同じ日時が複数ファイルに跨って存在する場合は、ファイルを読み込んだ順(ファイル名順)で
    # 後勝ちにする(黙って混ぜるのではなく件数をログに残す)。
    dedup_across_files = {}
    dup_across_files = 0
    for dt, weather_values, phenom_by_name in all_new_rows:
        if dt in dedup_across_files:
            dup_across_files += 1
        dedup_across_files[dt] = (dt, weather_values, phenom_by_name)
    if dup_across_files:
        log.warning("複数の取り込みファイルで同じ日時が%d件重複していました(後から読んだ方を採用)", dup_across_files)
    new_rows = list(dedup_across_files.values())

    wb = openpyxl.load_workbook(DATA_XLSX, read_only=True, data_only=True)
    try:
        existing_by_dt = _read_existing_rows(wb.active, n_cols)
    finally:
        wb.close()

    merged_rows, skipped, inserted_dts = build_merged_rows(existing_by_dt, new_rows, name_to_col, n_cols)

    if not inserted_dts:
        log.info("新規に取り込む行がありませんでした(全%d件が既存データと日時重複)", skipped)
        return

    log.info(
        "取り込み予定: %d件(既存と重複のためスキップ: %d件)、期間 %s 〜 %s",
        len(inserted_dts), skipped, min(inserted_dts), max(inserted_dts),
    )
    matched_names = sorted({n for _, _, p in new_rows for n in p})
    log.info("マッチした地点(%d件): %s", len(matched_names), ", ".join(matched_names))

    if dry_run:
        log.info("=== dry-runのため書き込みは行いません ===")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_jst_naive().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"river-fog-backup_before_historical_import_{stamp}.zip"
    build_data_backup_zip(backup_path)
    log.info("書き込み前バックアップを作成: %s", backup_path.name)

    try:
        with xlsx_write_lock():
            wb = openpyxl.load_workbook(DATA_XLSX)
            ws = wb.active
            ws.delete_rows(2, max(ws.max_row - 1, 0))
            for row in merged_rows:
                ws.append(row)
            wb.save(DATA_XLSX)
    except XlsxLockTimeout as e:
        log.error("%s; 書き込みを中断しました(collect.py等が実行中の可能性)。時間を置いて再実行してください。", e)
        return

    IMPORTED_DIR.mkdir(parents=True, exist_ok=True)
    for path in incoming_files:
        path.rename(IMPORTED_DIR / path.name)
    log.info("取り込み済みファイルを移動: %s", IMPORTED_DIR)

    log.info("=== 過去データ取り込み完了: %d件挿入、%d件重複スキップ ===", len(inserted_dts), skipped)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("import_historical_data.py が異常終了しました")
        sys.exit(1)
