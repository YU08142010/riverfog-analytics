"""保存の計画(2026-10-12、crontab 毎日06:30、健康チェック06:40の前)。

- ディスクの見込み: カメラ画像の1日あたりの増え方(直近14日の中央値)から、ディスクの使用率がFULL_PCTに届くまでの日数を出す。
  WARN_DAYS日を切ったら管理者に知らせる。
- PCへのバックアップの確認: PC(Windows)の RiverFogBackup/pull.ps1 が取り終えるたびに送る報告
  (data/health/offsite_manifest.json、webappの /api/backup-manifest)が、STALE_DAYS日より古ければ知らせる。
- 古い画像の間引き(既定はしない): KEEP_DAYS日より古い日付のフォルダのうち、PCの報告で枚数・大きさがサーバーと完全に同じ日だけを
  サーバーから消す。.env の RF_IMAGE_THIN=1 のときだけ実際に消し、それ以外は「消せる日」を数えて書くだけ。
  画像は学習(own_judgeの特徴量・かすみ度は計算済みでDBにある)・ギャラリー・タイムラプス・書き出しで使うので、KEEP_DAYSは長めにしてある。
結果は data/health/storage.json(/admin/health に出す)。

実行: .venv/bin/python storage_plan.py
"""
import json
import logging
import os
import shutil
import statistics
import sys
from datetime import datetime, timedelta

from common import BASE_DIR, JST

IMAGE_DIR = BASE_DIR / "data" / "images"
HEALTH = BASE_DIR / "data" / "health"
OUT = HEALTH / "storage.json"
MANIFEST = HEALTH / "offsite_manifest.json"
FULL_PCT = 90
WARN_DAYS = 120
STALE_DAYS = 3
KEEP_DAYS = 180
log = logging.getLogger("storage_plan")


def day_stats(d):
    files, size = 0, 0
    for root, _dirs, names in os.walk(d):
        for n in names:
            try:
                size += os.path.getsize(os.path.join(root, n))
                files += 1
            except OSError:
                pass
    return {"files": files, "bytes": size}


def main():
    load_env()
    now = datetime.now(JST).replace(tzinfo=None)
    today = now.date()
    days = sorted(p.name for p in IMAGE_DIR.iterdir() if p.is_dir() and len(p.name) == 10)
    stats = {d: day_stats(IMAGE_DIR / d) for d in days}
    full_days = [d for d in days if d < str(today)][-14:]  # 今日は途中なので除く
    per_day = statistics.median(stats[d]["bytes"] for d in full_days) if full_days else 0
    du = shutil.disk_usage(BASE_DIR)
    used_pct = du.used / du.total * 100
    room = du.total * FULL_PCT / 100 - du.used
    days_left = int(room / per_day) if per_day > 0 and room > 0 else (0 if room <= 0 else None)
    alerts = []
    if days_left is not None and days_left < WARN_DAYS:
        alerts.append({"kind": "disk", "message": f"このままだと約{days_left}日でディスクの使用率が{FULL_PCT}%に届きます"
                                                  f"(カメラ画像が1日約{per_day / 1e6:.0f}MB増加)。古い画像の間引きを検討してください。"})

    manifest = None
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("offsite manifest unreadable: %s", e)
    offsite = None
    if manifest:
        received = datetime.strptime(manifest["received_at"], "%Y-%m-%d %H:%M:%S")
        pc_days = manifest.get("image_days") or {}
        same = [d for d in days if d in pc_days and pc_days[d] == stats[d]]
        offsite = {"received_at": manifest["received_at"], "host": manifest.get("host"), "zips": len(manifest.get("zips") or []),
                   "latest_zip": (manifest.get("zips") or [None])[-1], "image_days_on_pc": len(pc_days),
                   "image_days_identical": len(same), "image_days_on_server": len(days)}
        if now - received > timedelta(days=STALE_DAYS):
            alerts.append({"kind": "offsite", "message": f"PCへのバックアップの報告が{(now - received).days}日来ていません"
                                                         f"(最後は{manifest['received_at'][:16]})。PCの電源・Tailscaleを確認してください。"})

    # 古い画像の間引き
    cutoff = str(today - timedelta(days=KEEP_DAYS))
    old = [d for d in days if d < cutoff]
    pc_days = (manifest or {}).get("image_days") or {}
    removable = [d for d in old if d in pc_days and pc_days[d] == stats[d]]
    enabled = os.environ.get("RF_IMAGE_THIN") == "1"
    removed = []
    if enabled and manifest and now - datetime.strptime(manifest["received_at"], "%Y-%m-%d %H:%M:%S") <= timedelta(days=STALE_DAYS):
        for d in removable:
            shutil.rmtree(IMAGE_DIR / d)
            removed.append(d)
            log.info("thinned images of %s (%d files, identical copy on the PC)", d, stats[d]["files"])
    res = {"checked_at": now.strftime("%Y-%m-%d %H:%M:%S"), "disk": {"total_gb": round(du.total / 1e9, 1), "used_pct": round(used_pct, 1),
           "free_gb": round(du.free / 1e9, 1)}, "images": {"days": len(days), "gb": round(sum(s["bytes"] for s in stats.values()) / 1e9, 2),
           "per_day_mb": round(per_day / 1e6, 1)}, "days_to_full": days_left, "full_pct": FULL_PCT, "offsite": offsite,
           "thin": {"enabled": enabled, "keep_days": KEEP_DAYS, "older_days": len(old), "removable": len(removable), "removed": removed},
           "alerts": alerts}
    HEALTH.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("storage plan: %s", {k: res[k] for k in ("disk", "images", "days_to_full", "thin")})
    return res


def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    main()
