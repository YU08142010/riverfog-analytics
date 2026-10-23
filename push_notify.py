"""Web Push通知(2026-10-09)。ブラウザやアプリを閉じていても、スマホ・PCに通知が届く。

- 鍵: VAPIDの秘密鍵を data/push_vapid_private.pem に置く(無ければ初回に作る。data/はgit管理外)。
  ブラウザ側は公開鍵(vapid_public_key())で購読し、購読情報(endpoint等)をusers.dbの
  push_subscriptionsに保存する。送信はpywebpush(RFC 8291の暗号化とVAPID署名)。
- 何を送るかは利用者ごとの設定(user_settings.py、設定画面)で決める。静かな時間帯は送らない
  (送らなかった分は記録しないので、時間帯が明けてもまだ当てはまる通知はそのとき届く)。
- 同じ通知を二度送らないよう、通知ごとのキーをpush_sentに記録する(14日で消す)。
- DM・グループは送信した瞬間にwebapp.pyからnotify_async()で送る。川霧の予報・観測・カメラ・
  管理者向けは、webapp.pyの裏のスレッドが5分ごとにrun_scheduled()を呼んで判定する。
- 送信先が「もう無い」(404/410。ブラウザで通知を切った、アプリを消した等)なら購読を消す。
  それ以外の失敗が続いた(MAX_FAILURES回)購読も消す。
"""
import base64
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import users
import user_settings
from common import send_alert

log = logging.getLogger("push")

BASE_DIR = Path(__file__).resolve().parent
VAPID_PRIVATE_PATH = BASE_DIR / "data" / "push_vapid_private.pem"
# VAPIDの連絡先(プッシュサービス側が問題のある送信元に連絡するためのもの)。個人のメールアドレスは使わない。
VAPID_SUB = "https://sshserver.taile43041.ts.net"
MAX_FAILURES = 5
SENT_KEEP_DAYS = 14
DIGEST_WINDOW_HOURS = 3       # 朝夕のまとめは、選んだ時刻からこの時間内に1回だけ送る
SOON_LOOKAHEAD_HOURS = 3      # 「この後」=何時間先までの「発生可能性 高」を知らせるか
NOW_MAX_AGE_MINUTES = 100     # これより古い観測では「いま観測」の通知を出さない(収集が止まった時の誤報防止)
# 夜のまとめ(明日の見込み)が参照する予報の生成(run_at)が、これより古いまま止まっていたら
# 送らない。以前は「今日生成された予報か」だけを見ており、生成が何日止まっていても
# 1時間待てば無条件に古い予報のまま送り続けていた(「今日の見込み」のつもりで利用者に
# 届くのに中身は何日も前のものという食い違いに誰も気付けなかった、2026-10-23に発覚)。
STALE_FORECAST_DAYS = 2

_users_db_path = None
_now = None
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="push")
_lock = threading.Lock()
_vapid = None
_public_key = None
_last_stale_forecast_alert_date = None  # send_alertを同じ日に何度も送らないための記録


def init(users_db_path, now_fn):
    global _users_db_path, _now
    _users_db_path = users_db_path
    _now = now_fn


def _conn():
    return users.connect(_users_db_path)


def _load_keys():
    global _vapid, _public_key
    with _lock:
        if _vapid is not None:
            return _vapid, _public_key
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from py_vapid import Vapid
        if not VAPID_PRIVATE_PATH.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            VAPID_PRIVATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = VAPID_PRIVATE_PATH.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
            os.replace(tmp, VAPID_PRIVATE_PATH)
            log.info("Generated a new VAPID key at %s", VAPID_PRIVATE_PATH)
        vapid = Vapid.from_file(str(VAPID_PRIVATE_PATH))
        raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        _public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        _vapid = vapid
        return _vapid, _public_key


def vapid_public_key():
    return _load_keys()[1]


def _send_one(sub, payload, ttl, urgency):
    """1つの購読先に送る。戻り値: "ok" / "gone"(購読がもう無い) / "error"。"""
    from pywebpush import WebPushException, webpush
    vapid, _ = _load_keys()
    try:
        webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=vapid, vapid_claims={"sub": VAPID_SUB},
            ttl=ttl, headers={"Urgency": urgency}, timeout=15,
        )
        return "ok"
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            return "gone"
        log.warning("Web Push failed (status %s) for subscription %s: %s", status, sub["id"], str(e)[:300])
        return "error"
    except Exception as e:
        log.warning("Web Push failed for subscription %s: %s", sub["id"], e)
        return "error"


def kind_enabled(settings, kind, role):
    if kind == "test":
        return True
    if kind == "dm":
        return settings["push_dm"]
    if kind == "mention":
        return settings["push_group"] in ("all", "mention")
    if kind == "group":
        return settings["push_group"] == "all"
    if kind == "fog_soon":
        return settings["push_fog_soon"]
    if kind == "fog_now":
        return settings["push_fog_now"]
    if kind == "fog_nowcast":
        return settings["push_fog_nowcast"]
    if kind == "camera":
        return settings["push_camera"]
    if kind == "report":
        return settings["push_monthly_report"]
    if kind == "digest":
        return settings["push_digest_morning"] is not None or settings["push_digest_evening"] is not None
    if kind == "admin":
        return role == "admin" and settings["push_admin"]
    return False


def deliver(user_id, kind, title, body, url, tag, key=None, ttl=3600, urgency="normal", checked=False):
    """1人の利用者の全端末に送る(同期)。送れた端末の数を返す。checked=Trueなら設定・静かな時間帯・
    重複の確認は呼び出し側で済んでいるものとして飛ばす。"""
    now = _now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn()
    try:
        subs = users.list_push_subscriptions(conn, user_id)
        if not subs:
            return 0
        if not checked and kind != "test":
            user = users.get_user_by_id(conn, user_id)
            if user is None:
                return 0
            s = user_settings.normalize(users.get_user_settings_raw(conn, user_id))
            if not kind_enabled(s, kind, user["role"]) or user_settings.in_quiet_hours(s, now.hour):
                return 0
        if key and not checked and not users.claim_push_key(conn, user_id, key, now_str):
            return 0
    finally:
        conn.close()
    if not url or not url.startswith("/") or url.startswith("//"):
        url = "/"
    payload = {"title": title, "body": body, "url": url, "tag": tag, "kind": kind}
    sent = 0
    for sub in subs:
        result = _send_one(sub, payload, ttl, urgency)
        conn = _conn()
        try:
            if result == "gone" or (result == "error" and sub["failure_count"] + 1 >= MAX_FAILURES):
                users.delete_push_subscription(conn, sub["id"])
                log.info("Removed push subscription %s of user %s (%s)", sub["id"], user_id, result)
            else:
                users.record_push_result(conn, sub["id"], result == "ok", now_str)
        finally:
            conn.close()
        if result == "ok":
            sent += 1
    return sent


def notify_async(user_ids, kind, title, body, url, tag, key=None, ttl=3600, urgency="normal"):
    """リクエストを待たせないよう、裏のスレッドで送る(DM・グループ用)。"""
    for uid in set(user_ids):
        def job(uid=uid):
            try:
                deliver(uid, kind, title, body, url, tag, key=key, ttl=ttl, urgency=urgency)
            except Exception:
                log.exception("Web Push delivery to user %s failed", uid)
        _executor.submit(job)


def message_preview(settings_raw, text, has_attachment=False):
    """DM・グループの通知の本文。「内容を表示しない」設定なら決まった文だけにする。"""
    s = user_settings.normalize(settings_raw)
    if not s["push_preview"]:
        return "新しいメッセージが届きました"
    text = " ".join((text or "").split())
    if not text:
        return "添付ファイルが届きました" if has_attachment else "新しいメッセージが届きました"
    return text if len(text) <= 80 else text[:79] + "…"


# --- 予報・観測などの定期判定 ----------------------------------------------------------------------

def station_url(name):
    return "/stations/" + quote(name, safe="")


def target_stations(settings, favorites, station_names):
    """通知の対象にする地点。お気に入りを1つも設定していなければ全地点(通知ベルと同じ扱い)。"""
    if settings["push_stations"] == "all":
        return list(station_names)
    if settings["push_stations"] == "custom":
        return list(settings["push_custom_stations"])
    picked = [n for n in station_names if n in favorites]
    return picked or list(station_names)


def _join_names(names, limit=4):
    if len(names) <= limit:
        return "・".join(names)
    return "・".join(names[:limit]) + f" ほか{len(names) - limit}地点"


def build_digest(forecast, day_index, targets, min_level):
    """朝夕のまとめの(タイトル, 本文, 先頭の地点)。当てはまる地点が無ければNone。"""
    if not forecast or day_index >= len(forecast.get("days") or []):
        return None
    by_name = {s["name"]: s for s in forecast.get("stations") or []}
    high, mid = [], []
    for name in targets:
        st = by_name.get(name)
        if not st or day_index >= len(st["days"]):
            continue
        lv = st["days"][day_index]["max"]
        if lv >= 3:
            high.append(name)
        elif lv == 2 and min_level <= 2:
            mid.append(name)
    if not high and not mid:
        return None
    day = forecast["days"][day_index]
    title = f"{day['label']}{day.get('date_label', '')}の川霧の見込み"
    parts = []
    if high:
        parts.append("出やすい: " + _join_names(high))
    if mid:
        parts.append("やや出やすい: " + _join_names(mid))
    return title, " / ".join(parts), (high + mid)[0]


def _soon_stations(forecast, targets, now):
    """この後SOON_LOOKAHEAD_HOURS時間以内に「発生可能性 高」の時刻がある地点 → {地点: 最初の時刻}。
    日ごとのhoursは0〜23で別々に区切られているため、23時台など日をまたぐ時間帯では
    今日のhoursだけを見ても24時・25時が存在せず素通りしていた(2026-10-23に発覚、霧の
    リスクが高いことが多い深夜直前の時間帯でちょうど通知が抜け落ちていた)。h>=24は
    翌日(days[1])のhoursをh-24で引く。"""
    out = {}
    if not forecast or not forecast.get("days"):
        return out
    if forecast["days"][0]["date"] != now.strftime("%Y-%m-%d"):
        return out
    by_name = {s["name"]: s for s in forecast.get("stations") or []}
    for name in targets:
        st = by_name.get(name)
        if not st:
            continue
        for h in range(now.hour + 1, now.hour + 1 + SOON_LOOKAHEAD_HOURS):
            if h < 24:
                lv = (st["days"][0].get("hours") or {}).get(h, 0)
            elif len(st["days"]) > 1:
                lv = (st["days"][1].get("hours") or {}).get(h - 24, 0)
            else:
                lv = 0
            if lv >= 3:
                out[name] = h % 24
                break
    return out


def _nowcast_hits(nowcast, now):
    """short_nowcast.latest()の値から、1時間後の見込みが通知用のしきい値以上の地点 → (時, 確率)。
    その1時間後(画像の時刻+1時間)を過ぎた・しきい値が無い(検証で当たる割合が足りなかった)なら空。
    cronで毎時18分ごろに発表されるので、通知はその時刻の40分ほど前になる。"""
    if not nowcast or nowcast.get("push_threshold") is None:
        return {}
    try:
        issued = datetime.strptime(nowcast["issued_at"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError):
        return {}
    if now >= issued + timedelta(hours=1):
        return {}
    h1 = (issued + timedelta(hours=1)).hour
    out = {}
    for name, hours in (nowcast.get("stations") or {}).items():
        p = hours.get(str(h1))
        if p is not None and p >= nowcast["push_threshold"]:
            out[name] = (h1, p)
    return out


def run_scheduled(ctx):
    """裏のスレッドから5分ごとに呼ぶ。ctx(webapp.pyが組み立てる):
    forecast(_public_forecast_safe()の値), observations(_latest_observations()), fog_codes,
    camera_failures(地点名のリスト), admin_alerts([(id, 文, url)]), station_names。"""
    now = _now()
    today = now.strftime("%Y-%m-%d")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn()
    try:
        ids = users.push_subscriber_ids(conn)
        if not ids:
            return
        members = users.get_users_by_ids(conn, ids)
        settings_raw = users.all_user_settings_raw(conn)
        if now.hour == 3:
            users.prune_push_sent(conn, (now - timedelta(days=SENT_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        conn.close()

    station_names = ctx["station_names"]
    forecast = ctx.get("forecast")
    obs = ctx.get("observations")
    fog_now = []
    if obs and obs.get("datetime") is not None and now - obs["datetime"] <= timedelta(minutes=NOW_MAX_AGE_MINUTES):
        by_station = ctx.get("fog_codes_by_station") or {}
        fog_now = [s["name"] for s in obs["stations"] if s.get("code") in by_station.get(s["name"], ctx["fog_codes"])]
        obs_hour = obs["datetime"].hour
    run_at = (forecast or {}).get("run_at") or ""
    nowcast_hits = _nowcast_hits(ctx.get("nowcast"), now)

    forecast_too_stale = False
    if run_at[:10]:
        try:
            stale_days = (now.date() - datetime.strptime(run_at[:10], "%Y-%m-%d").date()).days
        except ValueError:
            stale_days = None
        if stale_days is not None and stale_days > STALE_FORECAST_DAYS:
            forecast_too_stale = True
            global _last_stale_forecast_alert_date
            if _last_stale_forecast_alert_date != today:
                _last_stale_forecast_alert_date = today
                log.warning("Forecast hasn't updated in %d day(s) (last run_at=%s); holding back the evening digest",
                            stale_days, run_at)
                send_alert(f"川霧予報の生成が{stale_days}日止まっています(最終更新 {run_at})。"
                           "夜のまとめ通知は古いデータのまま送らず見合わせています。", source="push_notify")

    for uid in ids:
        user = members.get(uid)
        if user is None:
            continue
        s = user_settings.normalize(settings_raw.get(uid, "{}"), station_names)
        if user_settings.in_quiet_hours(s, now.hour):
            continue
        favorites = {x for x in user["favorite_stations"].split(",") if x}
        targets = target_stations(s, favorites, station_names)
        jobs = []  # (kind, keys, title, body, url, tag, urgency)

        for slot, day_index, hour in (("morning", 0, s["push_digest_morning"]), ("evening", 1, s["push_digest_evening"])):
            if hour is None or not (hour <= now.hour < hour + DIGEST_WINDOW_HOURS):
                continue
            # 夕方は、その日の夜の予測(19:30〜)が出てから送る。予測が止まっていても1時間待ったら前日分で送る
            # (ただしSTALE_FORECAST_DAYSを超えて古いままなら、もはや「前日分」とは呼べないため送らない)。
            if slot == "evening" and run_at[:10] != today and now.hour < hour + 1:
                continue
            if slot == "evening" and forecast_too_stale:
                continue
            d = build_digest(forecast, day_index, targets, s["push_digest_level"])
            if d:
                title, body, first = d
                jobs.append(("digest", [f"digest:{today}:{slot}"], title, body, "/stations", f"digest-{slot}", "normal"))

        if s["push_fog_soon"] and 3 <= now.hour <= 17:
            soon = _soon_stations(forecast, targets, now)
            if soon:
                names = sorted(soon, key=lambda n: (soon[n], station_names.index(n)))
                jobs.append(("fog_soon", [f"soon:{today}:{n}" for n in names], "まもなく川霧が出やすくなります",
                             f"{_join_names(names)}({soon[names[0]]}時ごろ〜)", station_url(names[0]), "fog-soon", "high"))

        if s["push_fog_nowcast"]:
            names = [n for n in targets if n in nowcast_hits and n not in fog_now]
            if names:
                names.sort(key=lambda n: -nowcast_hits[n][1])
                h = nowcast_hits[names[0]][0]
                jobs.append(("fog_nowcast", [f"nowcast:{today}:{n}" for n in names], "1時間以内に川霧が出ていそうです",
                             f"{_join_names(names)}({h}時ごろ、画像と実測から・試験中)", station_url(names[0]),
                             "fog-nowcast", "high"))

        if s["push_fog_now"] and fog_now:
            names = [n for n in targets if n in fog_now]
            if names:
                wide = any(len((ctx.get("fog_codes_by_station") or {}).get(n, ())) > 3 for n in names)
                jobs.append(("fog_now", [f"now:{today}:{n}" for n in names], "霧を観測しました" if wide else "川霧を観測しました",
                             f"{_join_names(names)}({obs_hour}時のカメラ画像)", station_url(names[0]), "fog-now", "high"))

        if s["push_camera"]:
            names = [n for n in targets if n in (ctx.get("camera_failures") or [])]
            if names:
                jobs.append(("camera", [f"camera:{today}:{n}" for n in names], "カメラ画像が届いていません",
                             f"{_join_names(names)}: 直近の画像が続けて取得できていません", station_url(names[0]),
                             "camera", "low"))

        if user["role"] == "admin" and s["push_admin"]:
            for aid, message, url in ctx.get("admin_alerts") or []:
                jobs.append(("admin", [f"admin:{today}:{aid}"], "システムのお知らせ", message, url, f"admin-{aid}", "high"))

        for kind, keys, title, body, url, tag, urgency in jobs:
            conn = _conn()
            try:
                if not users.list_push_subscriptions(conn, uid):
                    break
                # 地点ごとのキーのうち、まだ送っていないものが1つでもあれば送る(新しく加わった地点を知らせる)
                fresh = [k for k in keys if users.claim_push_key(conn, uid, k, now_str)]
            finally:
                conn.close()
            if not fresh:
                continue
            try:
                deliver(uid, kind, title, body, url, tag, ttl=3 * 3600, urgency=urgency, checked=True)
            except Exception:
                log.exception("Scheduled Web Push (%s) to user %s failed", kind, uid)
