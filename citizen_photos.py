"""市民観測(写真投稿、2026-10-12)。ログインした利用者がスマホで撮った霧の写真を、位置・撮影時刻・現象つきで投稿する。
観測者・管理者が確認して承認したものだけを一覧(/photos)と近くの地点ページに出す(承認すると投稿者にポイント)。

- 保存: アップロードはwebappの_save_uploaded_file(ウイルス検査つき)で受け、Pillowで開けるか確かめてから、
  長辺 MAX_SIDE px のJPEGに描き直して保存する(EXIF=撮影機種や位置情報などは消える)。一覧用の小さい画像も作る。
  元のファイルは残さない。
- 撮影時刻・位置: 投稿の画面で入れた値を優先し、無ければ写真のEXIF(撮影日時・GPS)から読む。位置は小数3桁
  (約100m)に丸めて保存し、画面には最寄りの地点と距離だけを出す。
- テーブルはusers.pyのcitizen_photos。
"""
import io
import math
from datetime import datetime

from PIL import ExifTags, Image, ImageOps

from common import BASE_DIR

PHOTO_DIR = BASE_DIR / "data" / "citizen_photos"
MAX_SIDE = 2000
THUMB_SIDE = 480
NEAR_KM = 5.0
REWARD_POINTS = 500
CODES = [(2, "川霧"), (5, "全体霧(あたり一面の霧)"), (7, "層雲・低い雲"), (0, "霧は無い"), (10, "雨")]


def _exif(im):
    try:
        raw = im.getexif()
    except Exception:
        return {}, {}
    tags = {ExifTags.TAGS.get(k, k): v for k, v in raw.items()}
    try:
        exif_ifd = raw.get_ifd(0x8769)
        tags.update({ExifTags.TAGS.get(k, k): v for k, v in exif_ifd.items()})
        gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in raw.get_ifd(0x8825).items()}
    except Exception:
        gps = {}
    return tags, gps


def _gps_deg(vals, ref):
    try:
        d, m, s = (float(x) for x in vals)
        v = d + m / 60 + s / 3600
        return -v if ref in ("S", "W") else v
    except Exception:
        return None


def process(src_path, photo_id):
    """元の画像から、保存用のJPEGと小さい画像を作る。戻り値: (幅, 高さ, EXIFの撮影日時 or None, EXIFの(緯度, 経度) or None)。
    開けない・画像ではない場合はValueError。"""
    try:
        with Image.open(src_path) as im:
            im.verify()
    except Exception as e:
        raise ValueError("画像として開けませんでした") from e
    with Image.open(src_path) as im:
        tags, gps = _exif(im)
        taken = None
        for key in ("DateTimeOriginal", "DateTime"):
            if tags.get(key):
                try:
                    taken = datetime.strptime(str(tags[key]).strip(), "%Y:%m:%d %H:%M:%S")
                    break
                except ValueError:
                    pass
        latlon = None
        if gps.get("GPSLatitude") and gps.get("GPSLongitude"):
            la, lo = _gps_deg(gps["GPSLatitude"], gps.get("GPSLatitudeRef")), _gps_deg(gps["GPSLongitude"], gps.get("GPSLongitudeRef"))
            if la is not None and lo is not None:
                latlon = (la, lo)
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        im.save(PHOTO_DIR / f"{photo_id}.jpg", "JPEG", quality=85, optimize=True)  # EXIFは付けない
        w, h = im.size
        im.thumbnail((THUMB_SIDE, THUMB_SIDE))
        im.save(PHOTO_DIR / f"{photo_id}_t.jpg", "JPEG", quality=80)
    return w, h, taken, latlon


def nearest_station(lat, lon, camera_info, names):
    best = None
    for n in names:
        c = camera_info.get(n, {})
        if c.get("lat") is None:
            continue
        d = math.hypot((lat - c["lat"]) * 111.0, (lon - c["lon"]) * 111.0 * math.cos(math.radians(lat)))
        if best is None or d < best[1]:
            best = (n, d)
    return best


def forecast_checks(photos):
    """写真での予報の答え合わせ(2026-10-12)。承認済みで地点の近く(NEAR_KM以内、または地点を選んで投稿)の写真ごとに、
    撮った時刻(時)のその地点の前日夜の本番の予測(対象の日の0時より前で最新、再予測は除く)と照合する。
    カメラの判定とは別の、人の目での答え合わせ。写真の「霧」は地点ごとの予測の対象(fog_features.target_codes)。
    戻り値: (各写真に kind=hit/fa/miss/cn・prob を付けたリスト(予測が無いものは除く), {kind: 件数})"""
    import sqlite3
    import fog_features
    import prediction_log
    from common import BASE_DIR as _B
    db = _B / "data" / "predictions.db"
    out, counts = [], {"hit": 0, "fa": 0, "miss": 0, "cn": 0}
    if not db.exists():
        return out, counts
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for p in photos:
            if p.get("status") != "approved" or not p.get("station_name"):
                continue
            if p.get("distance_km") is not None and p["distance_km"] > NEAR_KM:
                continue
            target = p["taken_at"][:13] + ":00:00"
            r = conn.execute(
                "SELECT predicted_code, predicted_fog_prob FROM predictions WHERE station = ? AND target_datetime = ? "
                "AND model_type LIKE ? AND model_type NOT LIKE '%[再予測]' AND run_at < ? ORDER BY run_at DESC LIMIT 1",
                (p["station_name"], target, prediction_log.CURRENT_MODEL_LIKE, target[:10] + " 00:00:00"),
            ).fetchone()
            if r is None:
                continue
            pred = int(r[0] or 0) == 1
            fog = p["code"] in fog_features.target_codes(p["station_name"])
            kind = "hit" if pred and fog else "fa" if pred else "miss" if fog else "cn"
            counts[kind] += 1
            out.append(dict(p, kind=kind, prob=r[1], predicted=pred, photo_fog=fog))
    finally:
        conn.close()
    return out, counts


def delete_files(photo_id):
    for suffix in ("", "_t"):
        (PHOTO_DIR / f"{photo_id}{suffix}.jpg").unlink(missing_ok=True)
