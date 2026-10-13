"""毎朝のシステムの健康チェック(2026-10-11、crontab 毎日06:40)。

前の晩から今朝までに、予測・毎時の収集・Geminiの判定・ディスク・バックアップ・夜間の巡回テスト(smoke_test.py)・
ウェブのサービスに問題が無かったかを確かめる。問題があれば、通知(Web Push)を受け取っている管理者に送る
(設定画面の「システムのお知らせ」がオンの人。ALERT_WEBHOOK_URLがあればそこにも)。問題が無ければ送らない。
結果は毎回 data/health/latest.json に書く(管理者ページの「システムの健康」で表示)。

実行: .venv/bin/python health_report.py [--dry-run]
"""
import argparse
import json
import logging
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta

from common import BASE_DIR, JST, send_alert

LOG_DIR = BASE_DIR / "logs"
OUT = BASE_DIR / "data" / "health" / "latest.json"
SMOKE = BASE_DIR / "data" / "health" / "smoke.json"
DISK_WARN_PCT = 85
GEMINI_QUOTA_WARN = 3          # 1日にこれ以上のクォータ超過があれば知らせる
COLLECT_MIN_RUNS = 14          # 4〜19時の毎時 = 16回。これ未満なら収集が抜けている
DNS_FAIL_WARN = 10             # 24時間で名前解決の失敗した「分」がこれ以上あれば知らせる(2026-10-13)
log = logging.getLogger("health_report")


def _log_lines_since(path, since):
    """ログの行のうち、先頭の時刻がsince以降のもの。"""
    if not path.exists():
        return []
    stamp = since.strftime("%Y-%m-%d %H:%M:%S")
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f.readlines()[-80000:]:
                if ln[:19] >= stamp and re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", ln):
                    out.append(ln)
    except OSError:
        pass
    return out


def check(now):
    issues, ok = [], []
    since = now - timedelta(hours=24)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # 予測(毎晩19:30)
    try:
        c = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'predictions.db'}?mode=ro", uri=True)
        run_at = c.execute("SELECT MAX(run_at) FROM predictions WHERE model_type NOT LIKE '%[再予測]'").fetchone()[0]
        c.close()
    except Exception as e:  # noqa: BLE001
        run_at = None
        issues.append(f"予測DBを読めません: {e}")
    if run_at and run_at[:10] >= yesterday:
        ok.append(f"予測: {run_at[:16]}に更新")
    else:
        issues.append(f"昨晩の予測が出ていません(最新 {run_at[:16] if run_at else 'なし'})")
    plines = _log_lines_since(LOG_DIR / "predict.log", since)
    if any("[ERROR]" in ln or "Traceback" in ln for ln in plines):
        issues.append("予測の処理でエラーが記録されています(logs/predict.log)")

    # 毎時の収集
    clines = _log_lines_since(LOG_DIR / "collect.log", since)
    runs = sum("=== Collection finished ===" in ln for ln in clines)
    if runs < COLLECT_MIN_RUNS:
        issues.append(f"毎時の収集が{runs}回しか終わっていません(通常16回)")
    else:
        ok.append(f"収集: {runs}回")
    quota = sum("[QUOTA]" in ln for ln in clines)
    if quota >= GEMINI_QUOTA_WARN:
        issues.append(f"Geminiのクォータ超過が{quota}回ありました(判定が抜けた可能性)")
    failed = sum("Gemini API call failed with all configured keys" in ln for ln in clines)
    if failed:
        issues.append(f"Geminiの判定が全キーで失敗した回が{failed}回ありました")

    # ディスク
    total, used, _ = shutil.disk_usage(BASE_DIR)
    pct = round(100 * used / total, 1)
    (issues if pct >= DISK_WARN_PCT else ok).append(f"ディスク使用率 {pct}%")

    # バックアップ(毎日03:00、logs/cron.logに結果が出る)
    blines = [ln for ln in _log_lines_since(LOG_DIR / "cron.log", since) if "backup" in ln.lower()]
    if any("error" in ln.lower() or "failed" in ln.lower() for ln in blines):
        issues.append("バックアップでエラーが記録されています(logs/cron.log)")

    # 夜間の巡回テスト
    try:
        sm = json.loads(SMOKE.read_text(encoding="utf-8"))
        if sm["checked_at"][:10] < yesterday:
            issues.append(f"夜間の巡回テストが動いていません(最後 {sm['checked_at'][:16]})")
        elif sm["failed"]:
            paths = ", ".join(f"{r['path']}({r['status']})" for r in sm["failed"][:5])
            issues.append(f"巡回テストで{len(sm['failed'])}ページが開けませんでした: {paths}")
        else:
            ok.append(f"巡回テスト: {sm['n']}ページ問題なし(最も遅い{sm['max_sec']}秒)")
        if sm.get("slow"):
            issues.append(f"表示に{sm['slow'][0]['sec']}秒以上かかったページが{len(sm['slow'])}件あります")
    except FileNotFoundError:
        issues.append("夜間の巡回テストの結果がありません")
    except Exception as e:  # noqa: BLE001
        issues.append(f"巡回テストの結果を読めません: {e}")

    # 名前解決(DNS)の失敗(2026-10-13): サーバーのDNSが時々数十秒失敗し、予測の天気予報の取得やアメダスの取得が抜けることがあった。
    # 各処理はやり直すが、多い日はWi-Fi・ルーター側を疑うきっかけにする。どのログにも出るので、主なログを合わせて数える。
    # 1回の不調で何行も出る(アメダスは地点ごと)ので、失敗のあった「分」の数で数える
    minutes = set()
    for name in ("cron.log", "collect.log", "predict.log", "amedas.log"):
        minutes.update(ln[:16] for ln in _log_lines_since(LOG_DIR / name, since)
                       if "Temporary failure in name resolution" in ln or "NameResolutionError" in ln)
    dns = len(minutes)
    if dns >= DNS_FAIL_WARN:
        hours = sorted({m[11:13] for m in minutes})
        issues.append(f"名前解決(DNS)の失敗が{dns}分ありました({'・'.join(h + '時' for h in hours[:8])}"
                      f"{'ほか' if len(hours) > 8 else ''}。Wi-Fi・ルーターの不調の可能性。処理はやり直しています)")
    else:
        ok.append(f"名前解決の失敗: {dns}分")

    # サービス・タイマー
    for unit in ("river-fog-web.service", "river-fog-collect.timer"):
        st = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
        if st != "active":
            issues.append(f"{unit} が {st} です")
    return {"checked_at": now.strftime("%Y-%m-%d %H:%M:%S"), "issues": issues, "ok": ok}


def notify(report):
    if not report["issues"]:
        return 0
    text = " / ".join(report["issues"])
    send_alert("朝の健康チェック: " + text, source="river-fog health")
    import push_notify
    import users
    from common import now_jst_naive
    db = BASE_DIR / "data" / "users.db"
    push_notify.init(db, now_jst_naive)
    conn = users.connect(db)
    try:
        admins = [u["id"] for u in users.list_users(conn) if u["role"] == "admin"]
    finally:
        conn.close()
    sent = 0
    for uid in admins:
        body = text if len(text) <= 160 else text[:159] + "…"
        sent += push_notify.deliver(uid, "admin", f"システムの確認: {len(report['issues'])}件の問題", body, "/admin/health",
                                    "health", key=f"health:{report['checked_at'][:10]}", ttl=12 * 3600, urgency="high")
    return sent


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    report = check(datetime.now(JST).replace(tzinfo=None))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if not args.dry_run:
        log.info("pushed to %d device(s)", notify(report))
