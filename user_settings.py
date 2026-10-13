"""ログイン利用者ごとの個人設定(2026-10-09、設定画面 /settings)。

users.dbのuser_settingsテーブルに、利用者ごとにJSON1行で保存する。ここでは項目の定義(既定値と
取りうる値)と、保存値・フォーム入力の検証だけを行う(Flaskに依存しない)。項目を増やすときは
SETTINGSに足すだけでよい。保存済みのJSONに無い項目は既定値で補い、定義から外した項目や
不正な値は読み込み時に捨てる(古い値が残っていても壊れない)。

表示まわりの項目(theme/text_size)は既定値をNone(=未設定)にしてある。未設定の間は、これまでどおり
その端末のブラウザの保存値(localStorage)に従う(この機能より前に端末ごとに選んでいた人の設定を
勝手に上書きしないため)。設定画面かヘッダーのボタンで一度選ぶと、全端末で同じになる。
"""
import json
import re

HOUR_CHOICES = list(range(24))

SETTINGS = {
    # --- 表示 ---
    "theme": {"default": None, "choices": [None, "device", "light", "dark"]},
    "text_size": {"default": None, "choices": [None, "normal", "large", "xlarge"]},
    "reduce_motion": {"default": False, "type": bool},
    "calm_background": {"default": False, "type": bool},
    "start_page": {"default": "dashboard", "choices": ["dashboard", "stations", "map", "favorite", "public"]},
    # --- メニュー(2026-10-13) ---
    "nav_position": {"default": "side", "choices": ["side", "top"]},  # PCでのメニューの場所(左/上)
    "nav_order": {"default": [], "type": "ids"},   # 項目の並び(空なら最初の並び)
    "nav_hidden": {"default": [], "type": "ids"},  # 出さない項目
    # --- プライバシー ---
    "show_online": {"default": True, "type": bool},
    "mute_profile_sounds": {"default": False, "type": bool},
    # --- 通知(Web Push) ---
    "push_stations": {"default": "favorites", "choices": ["favorites", "all", "custom"]},
    "push_custom_stations": {"default": [], "type": "stations"},
    "push_digest_morning": {"default": 6, "choices": [None, 5, 6, 7, 8]},
    "push_digest_evening": {"default": 20, "choices": [None, 20, 21, 22]},
    "push_digest_level": {"default": 3, "choices": [2, 3]},
    "push_fog_soon": {"default": True, "type": bool},
    "push_fog_now": {"default": True, "type": bool},
    "push_fog_nowcast": {"default": False, "type": bool},  # この後1時間の別予測(short_nowcast.py、2026-10-12)
    "push_camera": {"default": False, "type": bool},
    "push_monthly_report": {"default": True, "type": bool},
    "push_dm": {"default": True, "type": bool},
    "push_group": {"default": "mention", "choices": ["all", "mention", "off"]},
    "push_preview": {"default": True, "type": bool},
    "push_admin": {"default": True, "type": bool},
    "quiet_enabled": {"default": False, "type": bool},
    "quiet_start": {"default": 22, "choices": HOUR_CHOICES},
    "quiet_end": {"default": 6, "choices": HOUR_CHOICES},
}


def defaults():
    return {k: (list(v["default"]) if isinstance(v["default"], list) else v["default"]) for k, v in SETTINGS.items()}


def _valid(key, value, station_names=None):
    spec = SETTINGS[key]
    if "choices" in spec:
        return value in spec["choices"]
    if spec.get("type") is bool:
        return isinstance(value, bool)
    if spec.get("type") == "stations":
        return isinstance(value, list) and all(isinstance(s, str) for s in value) and (
            station_names is None or all(s in station_names for s in value))
    if spec.get("type") == "ids":
        return isinstance(value, list) and len(value) <= 60 and all(isinstance(s, str) and _ID_RE.fullmatch(s) for s in value)
    return False


_ID_RE = re.compile(r"[a-z_]{1,30}")


def normalize(raw, station_names=None):
    """保存済みの値(dictまたはJSON文字列)を既定値で補い、不正な値を捨てたdictを返す。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    out = defaults()
    for key in SETTINGS:
        if key in raw and _valid(key, raw[key], station_names):
            out[key] = raw[key]
    if station_names is not None:
        out["push_custom_stations"] = [s for s in station_names if s in set(out["push_custom_stations"])]
    return out


def parse_value(key, text, station_names=None):
    """フォーム・ヘッダーのボタンから来た文字列1つを、その項目の値に変換する。不正ならValueError。"""
    if key not in SETTINGS:
        raise ValueError(key)
    spec = SETTINGS[key]
    if spec.get("type") is bool:
        if text in ("1", "true", "on"):
            return True
        if text in ("0", "false", "off", ""):
            return False
        raise ValueError(text)
    if "choices" in spec:
        for c in spec["choices"]:
            if (c is None and text in ("", "none")) or (c is not None and str(c) == text):
                return c
        raise ValueError(text)
    raise ValueError(key)


def from_form(form, section, current, station_names):
    """設定画面の1つの節(section)のフォーム送信から、新しい設定dictを作る。
    チェックボックスは送られてこない=オフなので、その節に属する真偽値の項目はすべて読み直す。"""
    out = dict(current)
    for key in SECTIONS[section]:
        spec = SETTINGS[key]
        if spec.get("type") is bool:
            out[key] = form.get(key) in ("1", "on", "true")
        elif spec.get("type") == "stations":
            out[key] = [s for s in station_names if s in set(form.getlist(key))]
        elif key == "nav_order":
            out[key] = [s for s in form.getlist("nav_order") if _ID_RE.fullmatch(s)][:60]
        elif key == "nav_hidden":
            # 画面の一覧(nav_order)のうち、チェック(nav_show)の外れているもの
            shown = set(form.getlist("nav_show"))
            out[key] = [s for s in form.getlist("nav_order") if _ID_RE.fullmatch(s) and s not in shown][:60]
        elif key in form:
            out[key] = parse_value(key, form.get(key), station_names)
    return out


# 設定画面の節ごとに、どの項目を扱うか(from_formで真偽値の読み直しに使う)
SECTIONS = {
    "display": ["theme", "text_size", "reduce_motion", "calm_background", "start_page"],
    "menu": ["nav_position", "nav_order", "nav_hidden"],
    "privacy": ["show_online", "mute_profile_sounds"],
    "push": ["push_stations", "push_custom_stations", "push_digest_morning", "push_digest_evening",
             "push_digest_level", "push_fog_soon", "push_fog_now", "push_fog_nowcast", "push_camera", "push_monthly_report", "push_dm", "push_group",
             "push_preview", "push_admin", "quiet_enabled", "quiet_start", "quiet_end"],
}

# ヘッダーのボタン(テーマ・文字の大きさ)から、ページを移らずに1項目だけ保存してよい項目
QUICK_KEYS = {"theme", "text_size"}


def in_quiet_hours(settings, hour):
    """hour(0〜23)が通知を止める時間帯に入っているか。開始>終了なら日をまたぐ(22時〜6時など)。"""
    if not settings.get("quiet_enabled"):
        return False
    start, end = settings["quiet_start"], settings["quiet_end"]
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end
