"""地点・日ごとのタイムラプス動画(2026-10-11)。毎時のカメラ画像をつないで、なめらかに切り替わる短いMP4にする。

- 1枚あたり HOLD_SEC 表示し、次の画像へ FADE_SEC かけて重ねながら切り替える。左上に時刻を入れる。
- 作った動画は data/timelapse/<日付>/<地点>_<枚数>.mp4 に保存して使い回す(その日の画像が増えたら作り直す)。
- 書き出しはffmpeg(サーバーに無ければ、pipで入れたimageio-ffmpegに同梱のもの)。
"""
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common import BASE_DIR, NOTO_CJK_BOLD

IMAGE_DIR = BASE_DIR / "data" / "images"
OUT_DIR = BASE_DIR / "data" / "timelapse"
FONT = NOTO_CJK_BOLD
log = logging.getLogger("timelapse")
FPS = 12   # 古いCPUで作るので控えめに(切り替えは十分なめらか)
HOLD_SEC = 0.7
FADE_SEC = 0.5
WIDTH = 960
_lock = threading.Lock()   # 古いCPUなので同時に作るのは1本まで


def ffmpeg_path():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def frames_for(station, date):
    """その日のその地点の画像 [(hhmm, path)](取得できなかった時刻は除く)。"""
    day = IMAGE_DIR / date
    if not day.is_dir():
        return []
    out = []
    for hhmm in sorted(os.listdir(day)):
        p = day / hhmm / f"{station}.jpg"
        if p.is_file():
            out.append((hhmm, p))
    return out


def _prepare(path, hhmm, size, font):
    with Image.open(path) as im:
        im = im.convert("RGB").resize(size, Image.LANCZOS)
    d = ImageDraw.Draw(im, "RGBA")
    label = f"{int(hhmm[:2])}:{hhmm[2:]}"
    pad = 10
    box = d.textbbox((0, 0), label, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.rounded_rectangle((pad, size[1] - h - 3 * pad, pad + w + 2 * pad, size[1] - pad), radius=10, fill=(0, 0, 0, 120))
    d.text((2 * pad, size[1] - h - 2.4 * pad), label, font=font, fill=(255, 255, 255, 235))
    return im


def cached_path(station, date):
    """既に作成済みの動画があればそのパス、無ければNone(生成はしない)。
    新規生成のレート制限で「安いキャッシュ配信」と「重い新規生成」を見分けるために使う。"""
    import glob as _glob
    d = OUT_DIR / date
    if not d.is_dir():
        return None
    matches = sorted(d.glob(_glob.escape(station) + "_*.mp4"))
    return matches[0] if matches else None


def build(station, date):
    """動画のパス(作れなければNone)。同じ枚数で作ったものがあれば使い回す。"""
    frames = frames_for(station, date)
    if len(frames) < 2:
        return None
    out = OUT_DIR / date / f"{station}_{len(frames)}.mp4"
    if out.is_file():
        return out
    exe = ffmpeg_path()
    if not exe:
        return None
    with _lock:
        if out.is_file():
            return out
        with Image.open(frames[0][1]) as im0:
            ratio = im0.height / im0.width
        size = (WIDTH, int(round(WIDTH * ratio / 2)) * 2)  # H.264は縦横とも偶数
        try:
            font = ImageFont.truetype(FONT, 28)
        except OSError:
            # load_default()のビットマップフォントは日本語を描けず、時刻ラベルの日本語部分
            # (地点名)が文字化けする。以前は無言でこの経路に入っていた(2026-10-23に発覚)。
            log.warning("Failed to load the CJK font (%s); the timestamp label may not render Japanese text", FONT)
            font = ImageFont.load_default()
        hold, fade = int(HOLD_SEC * FPS), int(FADE_SEC * FPS)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="rf_tl_"))
        try:
            imgs = [_prepare(p, hhmm, size, font) for hhmm, p in frames]
            n = 0
            for i, im in enumerate(imgs):
                for _ in range(hold):
                    im.save(tmp / f"{n:05d}.jpg", quality=90); n += 1
                if i + 1 < len(imgs):
                    for k in range(1, fade + 1):
                        Image.blend(im, imgs[i + 1], k / (fade + 1)).save(tmp / f"{n:05d}.jpg", quality=90); n += 1
            part = out.with_suffix(".part.mp4")
            subprocess.run([exe, "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(tmp / "%05d.jpg"),
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "24", "-preset", "veryfast",
                            "-movflags", "+faststart", str(part)], check=True, timeout=300)
            part.replace(out)
            # 同じ日のこの地点の古い(枚数の少ない)動画は消す
            for old in out.parent.glob(f"{station}_*.mp4"):
                if old != out:
                    old.unlink(missing_ok=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out
