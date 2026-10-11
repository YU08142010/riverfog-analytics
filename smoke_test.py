"""夜間の巡回テスト(2026-10-11、crontab 毎日02:20)。自動テストが無いので、更新で壊れた画面に翌朝気づけるようにする。

使い捨ての管理者アカウント zz-smoke@example.invalid を作り(_require_loginがDBの利用者を確かめるため)、
アプリの秘密鍵で署名したセッションCookieで、主要なページ・全地点のページ・分析ページのグラフ画像を順に開いて
ステータスと応答時間を記録する。アカウントは最後に必ず消す。
結果は data/health/smoke.json(health_report.pyが朝の通知に使う)。

実行: .venv/bin/python smoke_test.py
"""
import json
import logging
import os
import secrets
import sys
import time
import urllib.parse
from datetime import datetime

import requests
import urllib3

from common import BASE_DIR, JST

urllib3.disable_warnings()
OUT = BASE_DIR / "data" / "health" / "smoke.json"
BASE = "https://127.0.0.1:8443"
EMAIL = "zz-smoke@example.invalid"
SLOW_SECONDS = 15
PAGES = [
    "/", "/stations", "/compare", "/map", "/simulator", "/observations", "/images", "/predictions",
    "/graphs/weather-fog", "/river-fog-histogram", "/accuracy", "/report", "/members", "/messages", "/group",
    "/game", "/chat", "/settings", "/observation_reports", "/gold", "/gold/results", "/changelog",
    "/gallery", "/reports", "/admin", "/admin/status", "/admin/errors",
    "/forecast", "/forecast/about",
]
log = logging.getLogger("smoke_test")


def _session_cookie(uid):
    sys.path.insert(0, str(BASE_DIR))
    import webapp  # noqa: E402  (アプリ本体の秘密鍵でセッションCookieに署名するため)
    ser = webapp.app.session_interface.get_signing_serializer(webapp.app)
    name = webapp.app.config.get("SESSION_COOKIE_NAME", "session")
    return name, ser.dumps({"user_id": uid, "role": "admin", "name": "巡回テスト", "_permanent": True}), webapp


def run():
    import users
    from werkzeug.security import generate_password_hash
    db = str(BASE_DIR / "data" / "users.db")
    conn = users.connect(db)
    try:
        old = users.get_user_by_email(conn, EMAIL)
        if old:
            users.delete_user(conn, old["id"])  # 前回が途中で落ちて残っていた分
        users.create_user(conn, "巡回テスト", EMAIL, generate_password_hash(secrets.token_urlsafe(16)), "admin",
                          datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"))
        uid = users.get_user_by_email(conn, EMAIL)["id"]
    finally:
        conn.close()
    results = []
    try:
        cname, cookie, webapp = _session_cookie(uid)
        s = requests.Session()
        s.verify = False
        s.cookies.set(cname, cookie)
        s.headers["Referer"] = BASE + "/"  # 「最初に開くページ」へ飛ばされないように

        def hit(path, kind="page"):
            t0 = time.monotonic()
            try:
                r = s.get(BASE + path, timeout=90, allow_redirects=False)
                code, size = r.status_code, len(r.content)
                ok = code == 200 and (kind != "png" or r.headers.get("Content-Type", "").startswith("image/png"))
            except Exception as e:  # noqa: BLE001
                code, size, ok = None, 0, False
                log.warning("%s failed: %s", path, e)
            sec = round(time.monotonic() - t0, 2)
            results.append({"path": path, "kind": kind, "status": code, "ok": ok, "sec": sec, "bytes": size})

        for p in PAGES:
            hit(p)
        for name in webapp.STATION_NAMES:
            hit("/stations/" + urllib.parse.quote(name))
            hit("/forecast/station/" + urllib.parse.quote(name))
        # 分析ページのグラフ(画像に描き直す経路まで通す)
        import re
        r = s.get(BASE + "/accuracy", timeout=90)
        for u in sorted(set(re.findall(r'data-chart-url="(/[^"]+)"', r.text)))[:20]:  # "/"で始まる本物のURLだけ
            u = u.replace("&amp;", "&")
            hit(u + ("&" if "?" in u else "?") + "format=png&theme=light&w=760&scale=2&notitle=1", kind="png")
    finally:
        conn = users.connect(db)
        try:
            u = users.get_user_by_email(conn, EMAIL)
            if u:
                users.delete_user(conn, u["id"])
        finally:
            conn.close()
    failed = [r for r in results if not r["ok"]]
    slow = [r for r in results if r["ok"] and r["sec"] >= SLOW_SECONDS]
    out = {"checked_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "n": len(results),
           "failed": failed, "slow": slow, "max_sec": max((r["sec"] for r in results), default=0)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OUT)
    log.info("smoke test: %d checked, %d failed, %d slow", len(results), len(failed), len(slow))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        os.nice(10)
    except OSError:
        pass
    run()
