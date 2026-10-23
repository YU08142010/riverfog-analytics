"""webapp.py から分けた部分(web_game)。webapp.py の最後で読み込まれ、ここで定義した名前は webapp にも戻される。
ルート・テンプレート・URLの名前(エンドポイント)は分ける前と同じ。"""
import os
import random
import threading
import time
import uuid
from datetime import datetime, timedelta
from flask import abort, jsonify, redirect, render_template, request, session, url_for
from common import JST, WEATHER_DB_PATH, now_jst_naive
import users
import fog_data
from fog_features import SUMMARY_FOG_CODES, encode_phenomena_cell, primary_phenom_code
import chat_log
import collect

from webapp import (ACCURACY_SUMMARY_CACHE_SECONDS, AVATAR_SIZE, CHAT_DB_PATH, CLAMAV_RESCAN_DIRS, CLAMAV_RESCAN_LOG_PATH, 
    CLAMAV_SIGNATURE_STALE_DAYS, DATE_RE, FEEDBACK_BODY_MAX_LENGTH, FEEDBACK_SUMMARY_SYSTEM_INSTRUCTION, IMAGE_DIR, 
    LOGIN_HEATMAP_DAYS, STATION_NAMES, STATION_REGION, _avatar_path, _clamav_db_info, _clamav_service_status, 
    _current_user_settings, _finish, _is_online, _is_script_running, _record_audit, _run_clamav_rescan_async, 
    _run_clamav_selftest, _send_alert_throttled, _sorted_subdirs, _users_conn, app, log, requires_admin)  # noqa: F401


# メンバーページの実績バッジ。未達成のものもロック表示するため、達成条件を関数として
# 持たせ、達成/未達成の両方をテンプレート側で判定できるようにしている。
BADGE_DEFINITIONS = [
    # ログイン回数(易→難、上位はcrownで統一しどのカテゴリでも「頂点」と分かるようにする)
    # pointsは達成の難易度に応じたポイント付与量(フレーム交換の原資)。初回付与時のみ加算され、
    # 何度ページを見ても再付与されない(_award_new_badges参照)。
    {"id": "first_login", "label": "はじめの一歩", "desc": "ログインした", "icon": "medal", "points": 100,
     "check": lambda s: s["login_count"] >= 1},
    {"id": "login_10", "label": "常連", "desc": "ログイン10回達成", "icon": "medal", "points": 300,
     "check": lambda s: s["login_count"] >= 10},
    {"id": "login_100", "label": "ヘビーユーザー", "desc": "ログイン100回達成", "icon": "medal", "points": 1500,
     "check": lambda s: s["login_count"] >= 100},
    {"id": "login_500", "label": "レジェンド", "desc": "ログイン500回達成", "icon": "crown", "points": 5000,
     "check": lambda s: s["login_count"] >= 500},
    # 連続ログイン日数
    {"id": "streak_3", "label": "三日坊主卒業", "desc": "3日連続ログイン", "icon": "flame", "points": 200,
     "check": lambda s: s["streak"] >= 3},
    {"id": "streak_7", "label": "一週間皆勤", "desc": "7日連続ログイン", "icon": "flame", "points": 500,
     "check": lambda s: s["streak"] >= 7},
    {"id": "streak_14", "label": "二週間皆勤", "desc": "14日連続ログイン", "icon": "flame", "points": 1000,
     "check": lambda s: s["streak"] >= 14},
    {"id": "streak_30", "label": "常習化", "desc": "30日連続ログイン", "icon": "flame", "points": 2000,
     "check": lambda s: s["streak"] >= 30},
    {"id": "streak_100", "label": "百日修行", "desc": "100日連続ログイン", "icon": "crown", "points": 5000,
     "check": lambda s: s["streak"] >= 100},
    # コメント投稿数
    {"id": "first_comment", "label": "発言デビュー", "desc": "地点にコメントを投稿した", "icon": "comment", "points": 100,
     "check": lambda s: s["comment_count"] >= 1},
    {"id": "comment_5", "label": "常連コメンター", "desc": "コメントを5件投稿した", "icon": "comment", "points": 400,
     "check": lambda s: s["comment_count"] >= 5},
    {"id": "comment_10", "label": "地点通", "desc": "コメントを10件投稿した", "icon": "comment", "points": 800,
     "check": lambda s: s["comment_count"] >= 10},
    {"id": "comment_30", "label": "コメント職人", "desc": "コメントを30件投稿した", "icon": "comment", "points": 2000,
     "check": lambda s: s["comment_count"] >= 30},
    {"id": "comment_100", "label": "コメントマスター", "desc": "コメントを100件投稿した", "icon": "crown", "points": 5000,
     "check": lambda s: s["comment_count"] >= 100},
    # AIチャット利用数
    {"id": "first_chat", "label": "AIとの対話", "desc": "AIチャットを利用した", "icon": "chat", "points": 100,
     "check": lambda s: s["chat_count"] >= 1},
    {"id": "chat_10", "label": "質問好き", "desc": "AIチャットで10回質問した", "icon": "chat", "points": 400,
     "check": lambda s: s["chat_count"] >= 10},
    {"id": "chat_50", "label": "探究者", "desc": "AIチャットで50回質問した", "icon": "crown", "points": 2000,
     "check": lambda s: s["chat_count"] >= 50},
    # お気に入り地点
    {"id": "favorite_3", "label": "お気に入り登録", "desc": "お気に入り地点を3件以上登録した", "icon": "star", "points": 200,
     "check": lambda s: s["favorite_count"] >= 3},
    {"id": "favorite_10", "label": "お気に入り愛好家", "desc": "お気に入り地点を10件以上登録した", "icon": "star", "points": 600,
     "check": lambda s: s["favorite_count"] >= 10},
    {"id": "favorite_all", "label": "全地点制覇", "desc": "全地点をお気に入り登録した", "icon": "crown", "points": 3000,
     "check": lambda s: s["favorite_count"] >= s["station_total"]},
    # 在籍日数
    {"id": "veteran_30", "label": "新米卒業", "desc": "登録から30日経過", "icon": "calendar", "points": 300,
     "check": lambda s: s["account_age_days"] >= 30},
    {"id": "veteran_180", "label": "半年選手", "desc": "登録から180日経過", "icon": "calendar", "points": 1200,
     "check": lambda s: s["account_age_days"] >= 180},
    {"id": "veteran_365", "label": "一周年", "desc": "登録から365日経過", "icon": "crown", "points": 3000,
     "check": lambda s: s["account_age_days"] >= 365},
    # プロフィール設定
    {"id": "avatar_set", "label": "プロフィール画像設定", "desc": "プロフィール画像を設定した", "icon": "camera", "points": 150,
     "check": lambda s: s["has_avatar"]},
    {"id": "bio_set", "label": "自己紹介デビュー", "desc": "自己紹介を入力した", "icon": "account", "points": 150,
     "check": lambda s: s["bio_set"]},
    # 権限
    {"id": "role_admin", "label": "運営メンバー", "desc": "管理者権限を持っている", "icon": "admin", "points": 1000,
     "check": lambda s: s["role"] == "admin"},
    # ログイン時間帯
    {"id": "night_owl", "label": "深夜の常連", "desc": "深夜0時〜4時台にログインしたことがある", "icon": "moon", "points": 300,
     "check": lambda s: s["night_owl"]},
    {"id": "early_bird", "label": "早起きさん", "desc": "早朝5時〜6時台にログインしたことがある", "icon": "sunrise", "points": 300,
     "check": lambda s: s["early_bird"]},
    # 週末利用
    {"id": "weekend_5", "label": "週末も欠かさず", "desc": "土曜日または日曜日にログインした日が5日以上ある", "icon": "calendar", "points": 600,
     "check": lambda s: s["weekend_days"] >= 5},
    # 上位ラダーの追加(既存カテゴリの、より遠い到達点)
    {"id": "login_1000", "label": "生き字引", "desc": "ログイン1000回達成", "icon": "crown", "points": 8000,
     "check": lambda s: s["login_count"] >= 1000},
    {"id": "comment_300", "label": "座談の主", "desc": "コメントを300件投稿した", "icon": "crown", "points": 6000,
     "check": lambda s: s["comment_count"] >= 300},
    {"id": "chat_200", "label": "知恵袋", "desc": "AIチャットで200回質問した", "icon": "crown", "points": 5000,
     "check": lambda s: s["chat_count"] >= 200},
    # 季節ログイン
    {"id": "newyear_login", "label": "元旦詣で", "desc": "1月1日にログインしたことがある", "icon": "calendar", "points": 400,
     "check": lambda s: s["newyear_login"]},
    {"id": "newyearseve_login", "label": "大晦日の見届け人", "desc": "12月31日にログインしたことがある", "icon": "calendar", "points": 400,
     "check": lambda s: s["newyearseve_login"]},
    # 累計ポイント(実績以外のポイント源も含めたこれまでの総獲得量。使い切っても下がらない)
    {"id": "points_1000", "label": "小金持ち", "desc": "累計獲得ポイントが1000ptに到達した", "icon": "medal", "points": 100,
     "check": lambda s: s["lifetime_points"] >= 1000},
    {"id": "points_10000", "label": "ポイント長者", "desc": "累計獲得ポイントが10000ptに到達した", "icon": "crown", "points": 1000,
     "check": lambda s: s["lifetime_points"] >= 10000},
    {"id": "points_25000", "label": "資産家", "desc": "累計獲得ポイントが25000ptに到達した", "icon": "medal", "points": 600,
     "check": lambda s: s["lifetime_points"] >= 25000},
    {"id": "points_50000", "label": "富豪", "desc": "累計獲得ポイントが50000ptに到達した", "icon": "medal", "points": 1500,
     "check": lambda s: s["lifetime_points"] >= 50000},
    {"id": "points_100000", "label": "億万長者", "desc": "累計獲得ポイントが100000ptに到達した", "icon": "crown", "points": 3000,
     "check": lambda s: s["lifetime_points"] >= 100000},
    {"id": "points_300000", "label": "只見川の大富豪", "desc": "累計獲得ポイントが300000ptに到達した", "icon": "crown", "points": 8000,
     "check": lambda s: s["lifetime_points"] >= 300000},
    {"id": "points_1000000", "label": "伝説の資産家", "desc": "累計獲得ポイントが100万ptに到達した", "icon": "crown", "points": 20000,
     "check": lambda s: s["lifetime_points"] >= 1000000},
    # フレームコレクション(交換ショップ自体を使ってもらうための実績)
    {"id": "frame_first", "label": "はじめての着せ替え", "desc": "アバターフレームを1つ交換した", "icon": "star", "points": 100,
     "check": lambda s: s["owned_frame_count"] >= 1},
    {"id": "frame_collector", "label": "フレームコレクター", "desc": "アバターフレームを5つ以上交換した", "icon": "star", "points": 500,
     "check": lambda s: s["owned_frame_count"] >= 5},
    {"id": "frame_master", "label": "フレームマスター", "desc": "全種類のアバターフレームを交換した", "icon": "crown", "points": 3000,
     # FRAME_DEFINITIONSはこの後で定義されるが、lambda本体はaward/表示のたびに評価される
     # ため問題ない(定義時ではなく呼び出し時にグローバル名を解決するPythonの通常の挙動)。
     "check": lambda s: s["owned_frame_count"] >= len(FRAME_DEFINITIONS)},
    # 川霧予測ゲームの的中実績
    {"id": "gamble_first_win", "label": "初勝利", "desc": "川霧予測ゲームで初めて賭けを的中させた", "icon": "star", "points": 200,
     "check": lambda s: s["gamble_won_count"] >= 1},
    {"id": "gamble_won_20", "label": "ギャンブラー", "desc": "川霧予測ゲームで20回的中させた", "icon": "star", "points": 600,
     "check": lambda s: s["gamble_won_count"] >= 20},
    {"id": "gamble_won_50", "label": "目利き", "desc": "川霧予測ゲームで50回的中させた", "icon": "star", "points": 1500,
     "check": lambda s: s["gamble_won_count"] >= 50},
    {"id": "gamble_won_100", "label": "川霧予知能力者", "desc": "川霧予測ゲームで100回的中させた", "icon": "crown", "points": 3000,
     "check": lambda s: s["gamble_won_count"] >= 100},
    {"id": "gamble_won_500", "label": "川霧の神様", "desc": "川霧予測ゲームで500回的中させた", "icon": "crown", "points": 10000,
     "check": lambda s: s["gamble_won_count"] >= 500},
    # メッセージ機能の利用(DM・グループチャット合算)
    {"id": "msg_10", "label": "おしゃべりデビュー", "desc": "DM・グループチャットで合計10件送信した", "icon": "chat", "points": 200,
     "check": lambda s: s["msg_count"] >= 10},
    {"id": "msg_100", "label": "おしゃべり好き", "desc": "DM・グループチャットで合計100件送信した", "icon": "chat", "points": 800,
     "check": lambda s: s["msg_count"] >= 100},
    {"id": "msg_500", "label": "コミュ力おばけ", "desc": "DM・グループチャットで合計500件送信した", "icon": "crown", "points": 3000,
     "check": lambda s: s["msg_count"] >= 500},
    {"id": "reaction_given_50", "label": "気配り上手", "desc": "リアクションを50回送った", "icon": "star", "points": 400,
     "check": lambda s: s["reaction_given_count"] >= 50},
    {"id": "reaction_received_50", "label": "人気者", "desc": "リアクションを50回もらった", "icon": "star", "points": 400,
     "check": lambda s: s["reaction_received_count"] >= 50},
    {"id": "mention_received_20", "label": "頼られる存在", "desc": "@メンションを20回もらった", "icon": "comment", "points": 400,
     "check": lambda s: s["mention_received_count"] >= 20},
    # 無料デイリーゲーム
    {"id": "fog_quiz_correct_10", "label": "予報士見習い", "desc": "川霧予報クイズに10回正解した", "icon": "forecast", "points": 300,
     "check": lambda s: s["fog_quiz_correct"] >= 10},
    {"id": "fog_quiz_correct_50", "label": "予報士", "desc": "川霧予報クイズに50回正解した", "icon": "forecast", "points": 1200,
     "check": lambda s: s["fog_quiz_correct"] >= 50},
    {"id": "fog_quiz_correct_200", "label": "名予報士", "desc": "川霧予報クイズに200回正解した", "icon": "crown", "points": 4000,
     "check": lambda s: s["fog_quiz_correct"] >= 200},
    {"id": "phenom_quiz_correct_20", "label": "現象マニア", "desc": "現象当てクイズに20回正解した", "icon": "accuracy", "points": 400,
     "check": lambda s: s["phenom_quiz_correct"] >= 20},
    {"id": "phenom_quiz_correct_100", "label": "現象博士", "desc": "現象当てクイズに100回正解した", "icon": "crown", "points": 2500,
     "check": lambda s: s["phenom_quiz_correct"] >= 100},
    {"id": "lottery_30", "label": "くじ引き常連", "desc": "デイリーくじを30日分引いた", "icon": "star", "points": 300,
     "check": lambda s: s["lottery_draws"] >= 30},
    {"id": "lottery_180", "label": "くじの達人", "desc": "デイリーくじを180日分引いた", "icon": "crown", "points": 2000,
     "check": lambda s: s["lottery_draws"] >= 180},
    # ファイル共有
    {"id": "file_upload_5", "label": "共有デビュー", "desc": "ファイルを5件アップロードした", "icon": "attach", "points": 200,
     "check": lambda s: s["file_upload_count"] >= 5},
    {"id": "file_upload_30", "label": "資料係", "desc": "ファイルを30件アップロードした", "icon": "attach", "points": 1000,
     "check": lambda s: s["file_upload_count"] >= 30},
    {"id": "file_star_10", "label": "整理整頓", "desc": "ファイルを10件お気に入り登録した", "icon": "star", "points": 300,
     "check": lambda s: s["file_star_count"] >= 10},
    # ショップコレクション
    {"id": "title_complete", "label": "称号コンプリート", "desc": "全ての称号を交換した", "icon": "crown", "points": 2000,
     "check": lambda s: s["title_complete"]},
    {"id": "name_color_complete", "label": "名前の色コンプリート", "desc": "全ての名前の色を交換した", "icon": "crown", "points": 1500,
     "check": lambda s: s["name_color_complete"]},
    {"id": "points_spent_10000", "label": "太っ腹", "desc": "ショップで累計10000pt使った", "icon": "medal", "points": 500,
     "check": lambda s: s["points_spent"] >= 10000},
    {"id": "points_spent_50000", "label": "浪費家", "desc": "ショップで累計50000pt使った", "icon": "crown", "points": 2000,
     "check": lambda s: s["points_spent"] >= 50000},
    # 月間ランキング
    {"id": "ranking_first_1", "label": "月間王者", "desc": "月間ログインランキングで1位を取った", "icon": "crown", "points": 500,
     "check": lambda s: s["ranking_first_count"] >= 1},
    {"id": "ranking_first_3", "label": "常勝の風格", "desc": "月間ログインランキングで1位を3回取った", "icon": "crown", "points": 2000,
     "check": lambda s: s["ranking_first_count"] >= 3},
    {"id": "ranking_first_12", "label": "只見川の覇者", "desc": "月間ログインランキングで1位を12回取った", "icon": "crown", "points": 6000,
     "check": lambda s: s["ranking_first_count"] >= 12},
    # 地点共有カード
    {"id": "station_share_10", "label": "情報通", "desc": "地点情報を10回共有した", "icon": "share", "points": 250,
     "check": lambda s: s["station_share_count"] >= 10},
    {"id": "station_share_50", "label": "地点ソムリエ", "desc": "地点情報を50回共有した", "icon": "crown", "points": 1500,
     "check": lambda s: s["station_share_count"] >= 50},
]

# --- 実績以外のポイント源 ---------------------------------------------------------
# 一度きりの実績達成だけでなく、日々の利用そのものからも少しずつポイントが貯まるように
# する。users.grant_points()のsource文字列で1回ごとの重複付与を防ぐ
# (例: 同じ日の2回目のログインでは付与しない、同じコメントに2回付与しない)。
DAILY_LOGIN_BONUS_POINTS = 20
COMMENT_POST_POINTS = 15
CHAT_MESSAGE_POINTS = 10

# --- ショップ経済インフレ対策(2026-09-07) -----------------------------------------
# 川霧予測ゲームで数千万〜億単位のポイントを稼ぐユーザーが出てきた結果、既存のショップ価格
# (数百〜数万pt)がほぼ無価値になったため、高額品ほど強く底上げする形で全カタログの
# costを一括値上げする。安価な品(BASE以下)はそのまま据え置き、高額品ほど指数的に
# 値上がりする(単純な定数倍だと安い品まで一律で上がってしまうため)。
_SHOP_INFLATION_BASE = 1500
_SHOP_INFLATION_EXPONENT = 0.85


def _inflated_cost(cost):
    if cost <= _SHOP_INFLATION_BASE:
        return cost
    multiplier = (cost / _SHOP_INFLATION_BASE) ** _SHOP_INFLATION_EXPONENT
    return int(round(cost * multiplier / 10.0)) * 10


# 買い切りをやめ、毎月この割合分を使用料として引き落とす(装備中のアイテムのみ課金、
# 未装備なら請求しない)。costに対する割合なので安い品は安く・高い品は高くなり、
# 月額が元の購入価格を超えることは割合が1未満である限り起こらない。
MONTHLY_FEE_RATIO = 0.12


def _monthly_fee(cost):
    return max(1, round(cost * MONTHLY_FEE_RATIO))


def _reprice_for_inflation(definitions):
    """定義リストの各itemのcostを_inflated_costで置き換え、月額使用料(monthly_fee)も
    合わせて計算してitemに埋め込む(破壊的変更)。ショップ画面はitem.monthly_feeを
    そのまま表示するだけでよい。"""
    for item in definitions:
        item["cost"] = _inflated_cost(item["cost"])
        item["monthly_fee"] = _monthly_fee(item["cost"])
    return definitions


# --- ポイント交換アバターフレーム -------------------------------------------------
# 実績ポイントで交換できるアバター装飾枠。「段階(グレード)」×「テーマ」の組み合わせで
# 4x5=20種類になる。CSS側は段階クラス(リング太さ・発光・アニメーション)とテーマクラス
# (配色)を掛け合わせて表現するため、フレーム自体を20個ハードコードする必要はない
# (style.cssの.avatar-frame関連ルール参照)。
FRAME_GRADES = _reprice_for_inflation([
    {"id": "bronze", "label": "銅", "cost": 1000},
    {"id": "silver", "label": "銀", "cost": 2500},
    {"id": "gold", "label": "金", "cost": 5000},
    {"id": "platinum", "label": "虹", "cost": 10000},
    {"id": "legend", "label": "伝説", "cost": 20000},
])
FRAME_THEMES = [
    {"id": "kiri", "label": "川霧"},
    {"id": "hare", "label": "晴天"},
    {"id": "yuuyake", "label": "夕焼け"},
    {"id": "yuki", "label": "雪"},
    {"id": "hoshizora", "label": "星空"},
    {"id": "momiji", "label": "紅葉"},
    {"id": "sakura", "label": "桜"},
    {"id": "hanabi", "label": "花火"},
    {"id": "mangetsu", "label": "満月"},
    {"id": "aurora", "label": "オーロラ"},
]
FRAME_DEFINITIONS = [
    {
        "id": f"{grade['id']}_{theme['id']}",
        "label": f"{grade['label']}の{theme['label']}",
        "grade": grade["id"], "grade_label": grade["label"], "cost": grade["cost"],
        "monthly_fee": grade["monthly_fee"],
        "theme": theme["id"], "theme_label": theme["label"],
    }
    for grade in FRAME_GRADES for theme in FRAME_THEMES
]
FRAME_BY_ID = {f["id"]: f for f in FRAME_DEFINITIONS}

# --- 称号・名前の色・ダッシュボード配色・演出 ---------------------------------------
# フレーム以外のショップ品。装備の仕組みはusers.py側で共通化しているが(user_cosmetics、
# category列で区別)、カタログ(何が買えるか)はここに種類ごとに定義する。
TITLE_DEFINITIONS = _reprice_for_inflation([
    {"id": "regular", "label": "常連さん", "cost": 500},
    {"id": "early_riser", "label": "早起き名人", "cost": 1200},
    {"id": "fog_walker", "label": "川霧使い", "cost": 3000},
    {"id": "tadami_master", "label": "只見川マスター", "cost": 8000},
    {"id": "legendary_observer", "label": "伝説の観測者", "cost": 15000},
    {"id": "dopagaki", "label": "ドパガキ", "cost": 50000},
    {"id": "legendary_gambler", "label": "伝説のギャンブラー", "cost": 1000000},
])
# 経済インフレ対策と同時に追加した最高レア度の称号(2026-09-07)。値上げ後の最高額品
# よりさらに一段上に置くため、_reprice_for_inflationは通さず直接「狂った」価格を付ける。
TITLE_DEFINITIONS.append(
    {"id": "kiri_no_kamigami", "label": "川霧の神々に愛されし者", "cost": 1000000000,
     "monthly_fee": _monthly_fee(1000000000), "added": "2026-09-07"}
)
TITLE_DEFINITIONS.append(
    {"id": "gem_title_legend", "label": "川霧の伝承者", "currency": "gems", "cost_gems": 90000, "premium": True}
)
TITLE_BY_ID = {t["id"]: t for t in TITLE_DEFINITIONS}

NAME_COLOR_DEFINITIONS = _reprice_for_inflation([
    {"id": "red", "label": "赤", "cost": 800, "css_class": "name-color-red"},
    {"id": "blue", "label": "青", "cost": 800, "css_class": "name-color-blue"},
    {"id": "green", "label": "緑", "cost": 800, "css_class": "name-color-green"},
    {"id": "gold", "label": "金", "cost": 2000, "css_class": "name-color-gold"},
    {"id": "rainbow", "label": "虹色", "cost": 5000, "css_class": "name-color-rainbow"},
])
NAME_COLOR_DEFINITIONS.append(
    {"id": "gem_name_color_aurora", "label": "極光の名前", "css_class": "shop-gem-premium-text",
     "currency": "gems", "cost_gems": 30000}
)
NAME_COLOR_BY_ID = {c["id"]: c for c in NAME_COLOR_DEFINITIONS}

DASHBOARD_THEME_DEFINITIONS = _reprice_for_inflation([
    {"id": "yuuyake", "label": "夕焼け", "cost": 1500, "css_class": "dash-theme-yuuyake"},
    {"id": "hoshizora", "label": "星空", "cost": 2500, "css_class": "dash-theme-hoshizora"},
    {"id": "shinryoku", "label": "新緑", "cost": 1500, "css_class": "dash-theme-shinryoku"},
    {"id": "yukigesho", "label": "雪景色", "cost": 1500, "css_class": "dash-theme-yukigesho"},
])
DASHBOARD_THEME_DEFINITIONS.append(
    {"id": "gem_dashboard_aurora", "label": "黄金の川霧配色", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 25000}
)
DASHBOARD_THEME_BY_ID = {d["id"]: d for d in DASHBOARD_THEME_DEFINITIONS}

EFFECT_DEFINITIONS = _reprice_for_inflation([
    {"id": "fog_drift", "label": "川霧の揺らめき", "cost": 4000, "css_class": "fx-fog-drift"},
    {"id": "sparkle", "label": "きらめき", "cost": 4000, "css_class": "fx-sparkle"},
    {"id": "aurora_glow", "label": "オーロラの光", "cost": 6000, "css_class": "fx-aurora-glow"},
])
EFFECT_DEFINITIONS.append(
    {"id": "gem_effect_aurora", "label": "黄金の煌めき", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 35000}
)
EFFECT_BY_ID = {e["id"]: e for e in EFFECT_DEFINITIONS}

BANNER_DEFINITIONS = _reprice_for_inflation([
    {"id": "kiri", "label": "川霧景色", "cost": 2000, "css_class": "banner-kiri"},
    {"id": "yuuyake", "label": "夕焼け", "cost": 2000, "css_class": "banner-yuuyake"},
    {"id": "yukigesho", "label": "雪景色", "cost": 2000, "css_class": "banner-yukigesho"},
    {"id": "hoshizora", "label": "星空", "cost": 3000, "css_class": "banner-hoshizora"},
])
BANNER_DEFINITIONS.append(
    {"id": "gem_banner_aurora", "label": "黄金の川面", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 50000}
)
BANNER_BY_ID = {b["id"]: b for b in BANNER_DEFINITIONS}

DM_THEME_DEFINITIONS = _reprice_for_inflation([
    {"id": "kiri", "label": "川霧", "cost": 1500, "css_class": "dm-theme-kiri"},
    {"id": "yuuyake", "label": "夕焼け", "cost": 1500, "css_class": "dm-theme-yuuyake"},
    {"id": "mint", "label": "ミント", "cost": 1500, "css_class": "dm-theme-mint"},
])
DM_THEME_DEFINITIONS.append(
    {"id": "gem_dm_theme_aurora", "label": "黄金の便り", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 15000}
)
DM_THEME_BY_ID = {d["id"]: d for d in DM_THEME_DEFINITIONS}

# 名前の色と同じCSSクラス(name-color-*)をそのまま流用する(色の見た目自体は
# 用途によらず共通なので、称号用に別のクラスを増やす必要が無い)。
TITLE_COLOR_DEFINITIONS = _reprice_for_inflation([
    {"id": "red", "label": "赤", "cost": 600, "css_class": "name-color-red"},
    {"id": "blue", "label": "青", "cost": 600, "css_class": "name-color-blue"},
    {"id": "green", "label": "緑", "cost": 600, "css_class": "name-color-green"},
    {"id": "gold", "label": "金", "cost": 1500, "css_class": "name-color-gold"},
    {"id": "rainbow", "label": "虹色", "cost": 4000, "css_class": "name-color-rainbow"},
])
TITLE_COLOR_DEFINITIONS.append(
    {"id": "gem_title_color_aurora", "label": "極上の称号色", "css_class": "shop-gem-premium-text",
     "currency": "gems", "cost_gems": 20000}
)
TITLE_COLOR_BY_ID = {c["id"]: c for c in TITLE_COLOR_DEFINITIONS}

MARKER_DEFINITIONS = _reprice_for_inflation([
    {"id": "hall_of_fame", "label": "殿堂入り章", "cost": 25000, "icon": "★"},
])
# 経済インフレ対策と同時に追加した最高レア度のマーカー(2026-09-07)。
MARKER_DEFINITIONS.append(
    # 殿堂入り章(★)と見た目が被らないよう冠アイコンにし、買い切りではなく毎月
    # 使用料がかかる(=払い続けられなくなれば自動で外れる)仕様に合わせて「永久」を
    # 含まない名前にしている。
    {"id": "aurora_crown", "label": "暁光の冠", "cost": 500000000, "icon": "👑", "premium": True,
     "monthly_fee": _monthly_fee(500000000), "added": "2026-09-07"}
)
MARKER_BY_ID = {m["id"]: m for m in MARKER_DEFINITIONS}

# --- ジェム専用アイテム(2026-09-11、2026-09-11改訂) --------------------------------
# 観測報告の報酬として貯まるジェムでしか買えない最高級アイテム。ポイント経済とは
# 完全に別会計(user_gem_items)で、月額使用料も課さない買い切り品にしている
# (ジェムは観測報告という単一の獲得経路しかなく、ポイントほど大量に稼げないため)。
# 独立したショップタブは持たず、殿堂入り章タブに他の装飾品と並べて表示する
# (ポイント品と別画面に分けない・ジェムは最高級品にしか使わない、という方針のため)。
GEM_SHOP_DEFINITIONS = [
    {"id": "observer_glasses", "label": "観測者の眼鏡", "cost_gems": 2000, "icon": "🔍",
     "desc": "遠くの川霧もくっきり見通せるという、観測者の間で語られる縁起物。"},
    {"id": "forecaster_compass", "label": "川霧予報士の羅針盤", "cost_gems": 10000, "icon": "🧭",
     "desc": "明日の川霧の気配を示すとされる、予報士憧れの逸品。"},
    {"id": "river_fog_crystal", "label": "川霧の結晶", "cost_gems": 40000, "icon": "💎", "premium": True,
     "desc": "濃い川霧の日にしか現れないという伝説の結晶。"},
    {"id": "legendary_notebook", "label": "伝説の観測手帳", "cost_gems": 120000, "icon": "📔", "premium": True,
     "desc": "数々の重要報告を成し遂げた観測者だけが手にできる、史上最高級の手帳。"},
]
GEM_SHOP_BY_ID = {g["id"]: g for g in GEM_SHOP_DEFINITIONS}

# --- 2026-09-03追加のショップ拡張分 ------------------------------------------------
# NEW_ITEM_ADDED_ATはこのバッチで追加したカタログ品に付けた"added"日付。ショップ側で
# 一定期間(SHOP_NEW_BADGE_DAYS)だけ「NEW」バッジを出すのに使う。
NEW_ITEM_ADDED_AT = "2026-09-03"
SHOP_NEW_BADGE_DAYS = 21

TITLE_BADGE_DEFINITIONS = _reprice_for_inflation([
    {"id": "sparkle", "label": "きらきら", "cost": 600, "icon": "✨", "added": NEW_ITEM_ADDED_AT},
    {"id": "fire", "label": "めらめら", "cost": 600, "icon": "🔥", "added": NEW_ITEM_ADDED_AT},
    {"id": "drop", "label": "しずく", "cost": 800, "icon": "💧", "added": NEW_ITEM_ADDED_AT},
    {"id": "gem", "label": "きらめく宝石", "cost": 2500, "icon": "💎", "added": NEW_ITEM_ADDED_AT},
])
TITLE_BADGE_DEFINITIONS.append(
    {"id": "gem_badge_star", "label": "極上の輝き", "icon": "🌟", "currency": "gems", "cost_gems": 15000}
)
TITLE_BADGE_BY_ID = {t["id"]: t for t in TITLE_BADGE_DEFINITIONS}

BUBBLE_STYLE_DEFINITIONS = _reprice_for_inflation([
    {"id": "round", "label": "まる型", "cost": 1200, "css_class": "bubble-style-round", "added": NEW_ITEM_ADDED_AT},
    {"id": "square", "label": "かく型", "cost": 1200, "css_class": "bubble-style-square", "added": NEW_ITEM_ADDED_AT},
    {"id": "handwritten", "label": "手書き風", "cost": 2500, "css_class": "bubble-style-handwritten", "added": NEW_ITEM_ADDED_AT},
])
BUBBLE_STYLE_DEFINITIONS.append(
    {"id": "gem_bubble_aurora", "label": "黄金の吹き出し", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 15000}
)
BUBBLE_STYLE_BY_ID = {b["id"]: b for b in BUBBLE_STYLE_DEFINITIONS}

AVATAR_BG_DEFINITIONS = _reprice_for_inflation([
    {"id": "glow_blue", "label": "青の光暈", "cost": 1800, "css_class": "avatar-bg-glow-blue", "added": NEW_ITEM_ADDED_AT},
    {"id": "glow_gold", "label": "金の光暈", "cost": 3500, "css_class": "avatar-bg-glow-gold", "added": NEW_ITEM_ADDED_AT},
    {"id": "mist", "label": "川霧の背景", "cost": 2200, "css_class": "avatar-bg-mist", "added": NEW_ITEM_ADDED_AT},
])
AVATAR_BG_DEFINITIONS.append(
    {"id": "gem_avatar_bg_aurora", "label": "後光の輝き", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 25000}
)
AVATAR_BG_BY_ID = {a["id"]: a for a in AVATAR_BG_DEFINITIONS}

RANK_STYLE_DEFINITIONS = _reprice_for_inflation([
    {"id": "gold_number", "label": "金の数字", "cost": 1500, "css_class": "rank-style-gold", "added": NEW_ITEM_ADDED_AT},
    {"id": "neon_number", "label": "ネオンの数字", "cost": 1500, "css_class": "rank-style-neon", "added": NEW_ITEM_ADDED_AT},
    {"id": "serif_number", "label": "書道体の数字", "cost": 1500, "css_class": "rank-style-serif", "added": NEW_ITEM_ADDED_AT},
])
RANK_STYLE_DEFINITIONS.append(
    {"id": "gem_rank_aurora", "label": "至高の数字", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 20000}
)
RANK_STYLE_BY_ID = {r["id"]: r for r in RANK_STYLE_DEFINITIONS}

# soundは効果音の種類ID。実際の音はWeb Audio APIでmember_detail.html側が合成するので、
# 音声ファイルは持たない(依存を増やさないため)。
PROFILE_SOUND_DEFINITIONS = _reprice_for_inflation([
    {"id": "chime", "label": "チャイム", "cost": 1000, "sound": "chime", "added": NEW_ITEM_ADDED_AT},
    {"id": "bell", "label": "鈴の音", "cost": 1000, "sound": "bell", "added": NEW_ITEM_ADDED_AT},
    {"id": "kirakira", "label": "きらめき音", "cost": 1800, "sound": "kirakira", "added": NEW_ITEM_ADDED_AT},
])
PROFILE_SOUND_DEFINITIONS.append(
    # 音自体はkirakiraを流用(専用音を追加するとJS側の対応も必要になるため)。
    {"id": "gem_sound_aurora", "label": "黄金の調べ", "sound": "kirakira", "currency": "gems", "cost_gems": 10000}
)
PROFILE_SOUND_BY_ID = {p["id"]: p for p in PROFILE_SOUND_DEFINITIONS}

# 季節限定コレクション。available_from/available_untilの期間外は新規購入できないが、
# 既に持っている分はそのまま装備・表示し続けられる(買い方だけを期間で絞る)。
SEASONAL_DEFINITIONS = _reprice_for_inflation([
    {
        "id": "summer_2026", "label": "夏祭りの提灯", "cost": 1800, "css_class": "seasonal-summer",
        "available_from": "2026-07-01", "available_until": "2026-08-31", "added": NEW_ITEM_ADDED_AT,
    },
    {
        "id": "autumn_2026", "label": "紅葉の川面", "cost": 1800, "css_class": "seasonal-autumn",
        "available_from": "2026-10-01", "available_until": "2026-11-30", "added": NEW_ITEM_ADDED_AT,
    },
    {
        "id": "newyear_2027", "label": "迎春の川霧", "cost": 2200, "css_class": "seasonal-newyear",
        "available_from": "2026-12-15", "available_until": "2027-01-15", "added": NEW_ITEM_ADDED_AT,
    },
])
SEASONAL_DEFINITIONS.append(
    # 期間限定ではなく通年購入できる(available_from/untilを付けない)、ジェム限定の一点。
    {"id": "gem_seasonal_aurora", "label": "永遠の黄金川霧", "css_class": "shop-gem-premium-box",
     "currency": "gems", "cost_gems": 45000}
)
SEASONAL_BY_ID = {s["id"]: s for s in SEASONAL_DEFINITIONS}

GACHA_COST = 5000
GACHA_BIG_REFUND_MULTIPLIER = 2.5
GACHA_BIG_REFUND_WEIGHT = 5
GACHA_CONSOLATION_POINTS = 300

# --- 川霧予測ゲーム・デイリーゲーム共通の消費アイテム ---------------------------------
CONSUMABLE_DEFINITIONS = [
    {
        "id": "insurance_ticket", "label": "保険チケット", "cost": 300,
        "desc": "川霧予測ゲームの賭けがハズレても、賭けたポイントの半額が払い戻されます。",
    },
    {
        "id": "boost_ticket", "label": "倍率ブーストチケット", "cost": 250,
        "desc": "川霧予測ゲームの賭けの倍率が10%上がります(的中時の払い戻しが増えます)。",
    },
    {
        "id": "lottery_luck_ticket", "label": "ラッキーチケット", "cost": 200,
        "desc": "デイリーくじを引く時に使うと、高額ポイントの出やすさが上がります。",
    },
    {
        "id": "quiz_hint_ticket", "label": "クイズヒントチケット", "cost": 150,
        "desc": "現象当てクイズで、誤った選択肢を1つ消せます。",
    },
    {
        "id": "super_boost_ticket", "label": "上級倍率ブーストチケット", "cost": 600,
        "desc": "川霧予測ゲームの賭けの倍率が25%上がります(倍率ブーストチケットの上位版。同時使用時はこちらが優先されます)。",
        "added": NEW_ITEM_ADDED_AT,
    },
    {
        "id": "withdraw_ticket", "label": "見送り券", "cost": 200,
        "desc": "観測時間帯が始まる前なら、賭けた後でもこのチケットで取り消し、全額払い戻せます。",
        "added": NEW_ITEM_ADDED_AT,
    },
]
CONSUMABLE_BY_ID = {c["id"]: c for c in CONSUMABLE_DEFINITIONS}
CONSUMABLE_BOOST_MULTIPLIER = 1.1
CONSUMABLE_SUPER_BOOST_MULTIPLIER = 1.25
CONSUMABLE_INSURANCE_REFUND_RATIO = 0.5
# ラッキーチケット使用時、100pt以上の枠の重みをこの倍率で底上げする(重みの再配分)。
LOTTERY_LUCK_WEIGHT_MULTIPLIER = 3

# --- 川霧予測ゲーム ---------------------------------------------------------------
# 「この地点でこの時刻に川霧が出るか」にポイントを賭けるゲーム。
# 倍率 = 地点の難易度(過去の川霧発生率が低いほど高倍率) × 先の長さ(遠い予測ほど高倍率)
#      × 時間精度(ざっくり日付だけ<時間帯<正確な1時間、細かく当てるほど高倍率)
# 賭けた時点でポイントは引かれ(fog_bets.bet_amountがtotal_spent_pointsに含まれる)、
# 的中すれば bet_amount * multiplier をgrant_pointsで払い戻す。外れれば戻ってこない
# (名前どおりの「川霧予測ゲーム」として、当てずっぽうの丁半博打にならないよう
# 難易度に応じた倍率を付ける)。
class _ConsumableUnavailable(Exception):
    """賭け金の残高チェックと同一トランザクション内でチケットを消費する際、
    在庫チェック直後に別リクエストが先に使い切っていた場合に投げる(その場合
    spend_points_if_available側でロールバックし、賭けは成立させない)。"""


GAMBLE_MIN_BET = 10
GAMBLE_MAX_LEAD_HOURS = 24 * 7  # 1週間先まで賭けられる
GAMBLE_MULTIPLIER_CAP = 30.0
GAMBLE_STATION_RARITY_FLOOR = 0.02  # 観測史上ほぼ川霧が出ない地点でも倍率を無限大にしない下限
GAMBLE_STATION_RARITY_CAP = 15.0
# 観測時間帯(04-19 JST)を3つの時間帯(block精度)に区切る。
GAMBLE_BLOCKS = [(4, 9, "早朝〜午前(4-9時)"), (9, 14, "昼(9-14時)"), (14, 19, "夕方(14-19時)")]
GAMBLE_PRECISIONS = [
    {"id": "day", "label": "日(その日いつでも)", "multiplier": 1.0},
    {"id": "block", "label": "時間帯(5時間区切り)", "multiplier": 1.8},
    {"id": "hour", "label": "正確な1時間", "multiplier": 3.0},
]
GAMBLE_PRECISION_BY_ID = {p["id"]: p for p in GAMBLE_PRECISIONS}

# --- デイリー川霧予報クイズ(無料・スキル型、川霧予測ゲームと違いポイントを賭けない) -------
# 毎日同じ地点を全員に出題する(日付から決定論的に選ぶ)ことで、その日1回だけの
# 気軽な予想ゲームにする。的中報酬に連続的中日数のボーナスを付け、継続を後押しする。
FOG_QUIZ_BASE_POINTS = 500
FOG_QUIZ_STREAK_BONUS_PER_DAY = 2
FOG_QUIZ_STREAK_BONUS_CAP_DAYS = 20  # ボーナス上限 = 20日 x 2pt = 40pt

# --- デイリーくじ -------------------------------------------------------------------
# 専用テーブルは持たず、grant_points()のUNIQUE(user_id, source)制約をそのまま
# 「1日1回」の判定に使う(users.get_point_event_amount参照)。
LOTTERY_OUTCOMES = [(50, 10), (30, 30), (15, 100), (4, 300), (1, 1000)]  # (重み, ポイント)

# --- デイリー現象当てクイズ(無料、即時採点) -----------------------------------------
# 1〜10の細かいコードそのままでは難しすぎるため、フォームUI上は5カテゴリに丸めて出題する
# (画像判定・訂正機能そのものはこれまで通り0〜10の全コードを扱う。丸めるのはクイズの
# 表示上だけ)。
PHENOM_QUIZ_CATEGORIES = [
    {"id": "kawagiri", "label": "川霧", "codes": {1, 2, 3}},
    {"id": "zentaigiri", "label": "全体霧", "codes": {4, 5, 6}},
    {"id": "souun", "label": "層雲", "codes": {7, 8}},
    {"id": "ame", "label": "霧雨・雨", "codes": {9, 10}},
    {"id": "nashi", "label": "現象なし", "codes": {0}},
]
PHENOM_QUIZ_POINTS = 500
OBSERVATION_REPORT_REWARD_POINTS = 2000

# --- ジェム(観測報告・正解セット専用の第二通貨) --------------------------------------
# 重要度は報告者が選ぶ(低・中・高)。「高」だけは緊急性を考慮し、管理者の承認を待たず
# 報告した時点で満額を即時付与する。低・中は誤報乱発によるファーミングを防ぐため、
# 報告時点では少額の参加ボーナスだけを出し、管理者が実際に訂正コードを変えて承認した
# 時にまとめて満額になるよう差額を追加付与する(既存のポイント報酬と同じ承認条件、
# corrected_code != reported_codeを使う)。
IMPORTANCE_LEVELS = ("low", "medium", "high")
IMPORTANCE_LABELS = {"low": "低", "medium": "中", "high": "高"}
OBSERVATION_REPORT_REASON_MAX_LENGTH = 300
GEM_REWARD_TOTAL = {"low": 10, "medium": 20, "high": 40}
GEM_REWARD_ON_SUBMIT = {"low": 1, "medium": 1, "high": GEM_REWARD_TOTAL["high"]}
GEM_REWARD_ON_APPROVE = {
    level: GEM_REWARD_TOTAL[level] - GEM_REWARD_ON_SUBMIT[level] for level in IMPORTANCE_LEVELS
}
# 観測者・管理者がstation_correct_observation(承認待ちを介さずその場で直接訂正する方)を
# 使った時の即時ジェム報酬。一般ユーザーの「報告」と違い、担当権限を持つ本人の判断で
# 確定させる操作のため承認は挟まず、最高重要度の報告(GEM_REWARD_TOTAL["high"]=40)より
# 高い50ジェムを即時付与する(2026-09-12追加)。
GEM_REWARD_DIRECT_CORRECTION = 50
# 正解セット(/gold)の判定1件ごとの即時ジェム報酬。サンプルはシステム側が無作為に割り当て、
# 残り枚数も有限なため(観測報告の低・中重要度のような)ファーミング対策の差額付与は不要で、
# 回答のたびに即時満額を付与する(2026-10-23追加)。
GEM_REWARD_GOLD_LABEL = 30
PHENOM_QUIZ_CANDIDATES_CACHE_SECONDS = 600


def _phenom_category_id_for_code(code):
    # codeは2コード同時観測(2026-09対応)のパック済み値のこともあるため、主コードだけで
    # 判定する(クイズは主コードのみで採点する、というシンプルな方針)。
    primary = primary_phenom_code(code)
    if primary != primary:  # NaN
        return None
    primary = int(primary)
    for cat in PHENOM_QUIZ_CATEGORIES:
        if primary in cat["codes"]:
            return cat["id"]
    return None


def _daily_fog_quiz_station(date_str):
    """その日の出題地点を日付から決定論的に選ぶ(全員同じ地点、日付が変われば変わる)。"""
    return random.Random(f"fog_quiz:{date_str}").choice(STATION_NAMES)


_phenom_quiz_candidates_cache = {"data": None, "at": 0.0}
_phenom_quiz_candidates_lock = threading.Lock()


def _phenom_quiz_candidates():
    """(日付, HHMM, 地点名, 現象コード)のうち、実際に画像ファイルが存在し、かつ
    現象コードが判明している(未分類でない)組み合わせのリストを返す。画像アーカイブは
    無期限保存だが実運用開始以降の分しか無いため、対象はその期間に限られる
    (データが増えるほど出題の幅が自然に広がる)。"""
    now = time.monotonic()
    with _phenom_quiz_candidates_lock:
        cached = _phenom_quiz_candidates_cache["data"]
        if cached is not None and now - _phenom_quiz_candidates_cache["at"] < PHENOM_QUIZ_CANDIDATES_CACHE_SECONDS:
            return cached
    candidates = []
    if IMAGE_DIR.exists() and WEATHER_DB_PATH.exists():
        try:
            image_dates = list(_sorted_subdirs(IMAGE_DIR))
            if image_dates:
                _, phenom_df, station_names = fog_data.load_river_fog_data()
                date_set = set(image_dates)
                phenom_df = phenom_df[phenom_df["datetime"].dt.strftime("%Y-%m-%d").isin(date_set)]
                code_lookup = {}
                for _, row in phenom_df.iterrows():
                    dt_str = row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
                    for name in station_names:
                        code = encode_phenomena_cell(row[name])
                        if code == code and code >= 0:  # NaN(未分類)・-1(無効画像)を除く
                            code_lookup[(dt_str, name)] = int(code)
                for date_dir in image_dates:
                    for hhmm in _sorted_subdirs(IMAGE_DIR / date_dir):
                        dt_str = f"{date_dir} {hhmm[:2]}:{hhmm[2:]}:00"
                        for name in station_names:
                            code = code_lookup.get((dt_str, name))
                            if code is None:
                                continue
                            if (IMAGE_DIR / date_dir / hhmm / f"{name}.jpg").exists():
                                candidates.append((date_dir, hhmm, name, code))
        except Exception as e:
            log.warning("Failed to build phenom quiz candidates: %s", e)
    with _phenom_quiz_candidates_lock:
        _phenom_quiz_candidates_cache["data"] = candidates
        _phenom_quiz_candidates_cache["at"] = now
    return candidates


def _todays_phenom_quiz_item(date_str):
    """その日の出題(date_dir, hhmm, station_name, code)を日付から決定論的に選ぶ
    (候補が無ければNone)。"""
    candidates = _phenom_quiz_candidates()
    if not candidates:
        return None
    return random.Random(f"phenom_quiz:{date_str}").choice(candidates)


def _resolve_pending_daily_fog_quiz():
    """観測日が終わっている(今日より前の)未解決のデイリー川霧予報クイズを答え合わせする。
    賭けの答え合わせ(_resolve_pending_fog_bets)・バッジ付与と同じく、ゲーム関連ページを
    開いたタイミングで遅延実行する(専用のcronは持たない)。"""
    conn = _users_conn()
    try:
        today_str = now_jst_naive().strftime("%Y-%m-%d")
        pending = users.list_pending_daily_fog_quiz(conn, today_str)
        if not pending:
            return
        if not WEATHER_DB_PATH.exists():
            return
        _, phenom_df, _ = fog_data.load_river_fog_data()
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        for entry in pending:
            station = entry["station_name"]
            quiz_date = entry["quiz_date"]
            if station not in phenom_df.columns:
                users.resolve_daily_fog_quiz(conn, entry["user_id"], quiz_date, "incorrect", now_str, 0)
                continue
            day_start, day_end = f"{quiz_date} 00:00:00", f"{quiz_date} 23:59:59"
            window = phenom_df[(phenom_df["datetime"] >= day_start) & (phenom_df["datetime"] <= day_end)]
            codes = window[station].apply(encode_phenomena_cell).apply(primary_phenom_code)
            occurred = bool(codes.dropna().isin(SUMMARY_FOG_CODES).any())
            correct = occurred == entry["guess"]
            new_streak = users.update_daily_fog_quiz_stats(conn, entry["user_id"], correct)
            if correct:
                bonus = min(new_streak, FOG_QUIZ_STREAK_BONUS_CAP_DAYS) * FOG_QUIZ_STREAK_BONUS_PER_DAY
                payout = FOG_QUIZ_BASE_POINTS + bonus
                users.grant_points(conn, entry["user_id"], f"fog_quiz:{quiz_date}", payout, now_str)
                users.resolve_daily_fog_quiz(conn, entry["user_id"], quiz_date, "correct", now_str, payout)
            else:
                users.resolve_daily_fog_quiz(conn, entry["user_id"], quiz_date, "incorrect", now_str, 0)
    except Exception as e:
        log.warning("Failed to resolve daily fog quiz: %s", e)
    finally:
        conn.close()


_station_fog_rate_cache = {"data": None, "at": 0.0}
_station_fog_rate_cache_lock = threading.Lock()


def _station_fog_rates():
    """地点ごとの、観測時間帯(04-19 JST)における川霧発生率(0〜1)を返す({地点名: 発生率})。
    データが無い/読めない場合は空dict(倍率計算側でフロア値にフォールバックする)。"""
    now = time.monotonic()
    with _station_fog_rate_cache_lock:
        cached = _station_fog_rate_cache["data"]
        if cached is not None and now - _station_fog_rate_cache["at"] < ACCURACY_SUMMARY_CACHE_SECONDS:
            return cached
    if not WEATHER_DB_PATH.exists():
        return {}
    try:
        main_df, phenom_df, station_names = fog_data.load_river_fog_data()
        rates = {}
        for name in station_names or []:
            if name not in phenom_df.columns:
                continue
            merged = main_df.merge(phenom_df[["datetime", name]], on="datetime", how="inner")
            codes = merged[name].apply(encode_phenomena_cell).apply(primary_phenom_code)
            valid = codes.notna()
            n = int(valid.sum())
            if n == 0:
                continue
            fog_n = int(codes.loc[valid].isin(SUMMARY_FOG_CODES).sum())
            rates[name] = fog_n / n
    except Exception as e:
        log.warning("Failed to compute station fog rates: %s", e)
        return {}
    with _station_fog_rate_cache_lock:
        _station_fog_rate_cache["data"] = rates
        _station_fog_rate_cache["at"] = now
    return rates


def _gamble_multiplier(fog_rate, lead_hours, precision_id):
    """賭けの倍率を計算する。fog_rateがNone(データ不足)の場合は最も保守的な
    (rarity_mult=1.0の)側に倒す。"""
    if fog_rate is None:
        rarity_mult = 1.0
    else:
        rarity_mult = min(max(1.0 / max(fog_rate, GAMBLE_STATION_RARITY_FLOOR), 1.0), GAMBLE_STATION_RARITY_CAP)
    lead_mult = 1.0 + min(max(lead_hours, 0), GAMBLE_MAX_LEAD_HOURS) / GAMBLE_MAX_LEAD_HOURS * 2.0
    precision_mult = GAMBLE_PRECISION_BY_ID.get(precision_id, {"multiplier": 1.0})["multiplier"]
    total = rarity_mult * lead_mult * precision_mult
    return round(min(total, GAMBLE_MULTIPLIER_CAP), 2)


def _gamble_window_for_bet(target_date, precision_id, hour=None, block_index=None):
    """precisionに応じて(window_start, window_end)のdatetime文字列ペアを返す。
    観測時間帯(04-19 JST)の外を指定された場合はNoneを返す(呼び出し側でエラー扱い)。"""
    if precision_id == "day":
        start_h, end_h = 4, 19
    elif precision_id == "block":
        if block_index is None or not (0 <= block_index < len(GAMBLE_BLOCKS)):
            return None
        start_h, end_h = GAMBLE_BLOCKS[block_index][0], GAMBLE_BLOCKS[block_index][1]
    elif precision_id == "hour":
        if hour is None or not (4 <= hour <= 19):
            return None
        start_h, end_h = hour, hour
    else:
        return None
    window_start = f"{target_date} {start_h:02d}:00:00"
    window_end = f"{target_date} {end_h:02d}:00:00"
    return window_start, window_end


# collect.pyの毎時収集は画像取得+Gemini分類を伴うため、その時刻ちょうどに終わるとは限らず、
# 実測では数十秒〜(まれに)数分かかる(logs/collect.logで確認済み: ある回はwindow_end+26秒、
# 別の回は+2分46秒で完了していた)。window_endを過ぎた瞬間に答え合わせすると、まだ収集が
# 終わっていないデータを「川霧は観測されなかった」と誤認して外れ扱いにしてしまう
# (2026-09-03に実際に発生し、window_endのわずか14秒後に3件が誤って「はずれ」判定されていた:
# 本来は川霧が観測されていたのに、その時点ではまだ該当時刻のデータがweather.dbに
# 書き込まれておらず、他地点も含め「その時刻の行が1つも無い」状態で判定してしまっていた)。
# そのため、対象時刻(window_end)のデータがまだ存在しない場合は「まだ判定できない」として
# 今回は見送り、次にゲーム関連ページが開かれた時に再試行する。収集が恒久的に失敗している
# (画像取得不能等)ケースに備え、それでも一定時間(GAMBLE_RESOLUTION_GRACE_PERIOD)経っても
# データが来なければ、やむを得ずその時点のデータだけで判定を確定する。
GAMBLE_RESOLUTION_GRACE_PERIOD = timedelta(hours=2)


def _resolve_pending_fog_bets():
    """window_endが過ぎた賭けの答え合わせをする。バッジ付与(_award_new_badges)と同じく、
    ゲーム関連ページを開いたタイミングで遅延実行する(専用のcronは持たない)。"""
    conn = _users_conn()
    try:
        now = now_jst_naive()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        pending = users.list_pending_fog_bets(conn, now_str)
        if not pending:
            return
        if not WEATHER_DB_PATH.exists():
            return
        main_df, phenom_df, _ = fog_data.load_river_fog_data()
        collected_datetimes = set(main_df["datetime"])
        for bet in pending:
            station = bet["station_name"]
            window_end_dt = datetime.strptime(bet["window_end"], "%Y-%m-%d %H:%M:%S")
            data_ready = window_end_dt in collected_datetimes
            if not data_ready and (now - window_end_dt) < GAMBLE_RESOLUTION_GRACE_PERIOD:
                # 対象時刻の収集がまだ終わっていない可能性が高いため、今回は判定を見送る
                # (猶予期間内は「まだ観測されていない」を「川霧が出なかった」と混同しない)。
                continue
            if station not in phenom_df.columns:
                # 地点が無くなった等、答え合わせ不能。賭け金は返さずlostとして締める
                # (無期限pendingのまま残り続けるのを防ぐ)。
                users.resolve_fog_bet(conn, bet["id"], "lost", now_str, 0)
                continue
            window = main_df[
                (main_df["datetime"] >= bet["window_start"]) & (main_df["datetime"] <= bet["window_end"])
            ]
            merged = window.merge(phenom_df[["datetime", station]], on="datetime", how="inner")
            codes = merged[station].apply(encode_phenomena_cell).apply(primary_phenom_code)
            occurred = bool(codes.dropna().isin(SUMMARY_FOG_CODES).any())
            if occurred:
                payout = int(round(bet["bet_amount"] * bet["multiplier"]))
                users.resolve_fog_bet(conn, bet["id"], "won", now_str, payout)
                users.grant_points(conn, bet["user_id"], f"gamble_win:{bet['id']}", payout, now_str)
            else:
                refund = 0
                if bet.get("insurance"):
                    refund = int(round(bet["bet_amount"] * CONSUMABLE_INSURANCE_REFUND_RATIO))
                    users.grant_points(conn, bet["user_id"], f"gamble_insurance:{bet['id']}", refund, now_str)
                users.resolve_fog_bet(conn, bet["id"], "lost", now_str, refund)
    except Exception as e:
        log.warning("Failed to resolve pending fog bets: %s", e)
    finally:
        conn.close()


def _compute_badges(stats):
    """statsは{login_count, streak, comment_count, chat_count, favorite_count,
    station_total, account_age_days, has_avatar, bio_set, role, night_owl, early_bird,
    weekend_days, newyear_login, newyearseve_login, lifetime_points, owned_frame_count}。
    定義済みの実績すべてについて、達成済みかどうかを付けたリストを返す
    (未達成もロック表示するため、達成したものだけに絞り込まない)。"""
    return [
        {"id": b["id"], "label": b["label"], "desc": b["desc"], "icon": b["icon"],
         "points": b["points"], "achieved": b["check"](stats)}
        for b in BADGE_DEFINITIONS
    ]


def _award_new_badges(conn, user_id, badges):
    """達成済みなのにまだポイントを付与していない実績があれば、その分だけ付与する。
    users.award_badge()がUNIQUE制約で二重付与を防ぐため、member_detail()を見るたびに
    呼び出しても達成済みの実績に再度ポイントが入ることはない(新規達成分だけ加算される)。"""
    awarded_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    for b in badges:
        if b["achieved"]:
            users.award_badge(conn, user_id, b["id"], b["points"], awarded_at)


# 月間(先月分)ログインランキング上位3名への報酬。members_list()を開くたびに
# _award_monthly_ranking_rewards()から呼ばれるが、grant_points()のUNIQUE(user_id, source)
# 制約により実際の付与は月initial回だけ(以後は何もしない、バッジ付与と同じ遅延実行方式)。
MONTHLY_RANKING_REWARDS = [(1, 5000), (2, 2000), (3, 500)]


def _award_monthly_ranking_rewards(conn):
    """先月分の月間ログイン回数ランキング上位3名に、未付与ならポイントを付与する。"""
    today = datetime.now(JST).date()
    this_month_start = today.replace(day=1)
    last_month_end_day = this_month_start - timedelta(days=1)
    last_month_start = last_month_end_day.replace(day=1)
    since_str = last_month_start.strftime("%Y-%m-%d 00:00:00")
    until_str = this_month_start.strftime("%Y-%m-%d 00:00:00")

    counts = users.login_counts_between(conn, since_str, until_str)
    if not counts:
        return
    # 集計後にアカウントが削除されている場合(login_eventsは残るがusersからは消える)、
    # grant_pointsのFOREIGN KEY制約で例外になってしまうため、現存するユーザーだけに絞る。
    existing_ids = set(users.get_users_by_ids(conn, list(counts.keys())).keys())
    ranked = sorted(
        ((uid, c) for uid, c in counts.items() if uid in existing_ids),
        key=lambda kv: (-kv[1], kv[0]),
    )
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    month_label = last_month_start.strftime("%Y-%m")
    for rank, points in MONTHLY_RANKING_REWARDS:
        if rank > len(ranked):
            break
        user_id, count = ranked[rank - 1]
        if count <= 0:
            continue
        source = f"monthly_login_ranking_{month_label}_{rank}"
        if users.grant_points(conn, user_id, source, points, now_str):
            log.info(
                "Granted %d points to user %d for monthly login ranking #%d (%s, %d logins)",
                points, user_id, rank, month_label, count,
            )


@app.route("/members")
def members_list():
    """ログイン中の全ユーザーが見られるメンバー一覧。個々のログイン履歴・
    ヒートマップはメンバー詳細ページ(member_detail)で確認する。"""
    today = datetime.now(JST).date()
    month_start = today.replace(day=1).strftime("%Y-%m-%d 00:00:00")
    conn = _users_conn()
    try:
        _award_monthly_ranking_rewards(conn)
        all_users = users.list_users(conn)
        login_counts = users.login_counts_all(conn)
        month_login_counts = users.login_counts_since(conn, month_start)
        month_comment_counts = users.comment_counts_since(conn, month_start)
        month_message_counts = users.message_counts_since(conn, month_start)
        month_gamble_win_counts = users.won_bet_counts_since(conn, month_start)
        month_points_earned = users.points_earned_since(conn, month_start)
        last_logins = users.last_logins_all(conn)
        streaks = users.login_streaks_all(conn, today)
        last_active = users.last_active_all(conn)
    finally:
        conn.close()
    members = [
        {
            "id": u["id"], "name": u["name"], "role": u["role"],
            "has_avatar": _avatar_path(u["id"]).exists(),
            "login_count": login_counts.get(u["id"], 0),
            "month_login_count": month_login_counts.get(u["id"], 0),
            "month_comment_count": month_comment_counts.get(u["id"], 0),
            "month_message_count": month_message_counts.get(u["id"], 0),
            "month_gamble_win_count": month_gamble_win_counts.get(u["id"], 0),
            "month_points_earned": month_points_earned.get(u["id"], 0),
            "last_login": last_logins.get(u["id"]),
            "streak": streaks.get(u["id"], 0),
            "favorite_stations": [s for s in u["favorite_stations"].split(",") if s],
            "equipped_frame": u["equipped_frame"],
            "online": _is_online(last_active.get(u["id"])),
            "title_label": TITLE_BY_ID.get(u["equipped_title"], {}).get("label", ""),
            "name_color_class": NAME_COLOR_BY_ID.get(u["equipped_name_color"], {}).get("css_class", ""),
            "title_color_class": TITLE_COLOR_BY_ID.get(u["equipped_title_color"], {}).get("css_class", ""),
            "marker_icon": MARKER_BY_ID.get(u["equipped_marker"], {}).get("icon", ""),
            "title_badge_icon": TITLE_BADGE_BY_ID.get(u["equipped_title_badge"], {}).get("icon", ""),
            "rank_style_class": RANK_STYLE_BY_ID.get(u["equipped_rank_style"], {}).get("css_class", ""),
        }
        for u in all_users
    ]
    ranking = sorted(
        [m for m in members if m["login_count"] > 0], key=lambda m: -m["login_count"]
    )[:5]
    month_ranking = sorted(
        [m for m in members if m["month_login_count"] > 0], key=lambda m: -m["month_login_count"]
    )[:5]
    # ログイン以外のランキング種別は報酬が絡まないため、シンプルに今月分のみ用意する。
    extra_rankings = [
        {
            "id": "comment", "label": "コメント投稿数", "unit": "件",
            "count_key": "month_comment_count",
            "entries": sorted(
                [m for m in members if m["month_comment_count"] > 0],
                key=lambda m: -m["month_comment_count"],
            )[:5],
        },
        {
            "id": "message", "label": "メッセージ送信数", "unit": "件",
            "count_key": "month_message_count",
            "entries": sorted(
                [m for m in members if m["month_message_count"] > 0],
                key=lambda m: -m["month_message_count"],
            )[:5],
        },
        {
            "id": "gamble_win", "label": "川霧予測ゲーム勝利数", "unit": "回",
            "count_key": "month_gamble_win_count",
            "entries": sorted(
                [m for m in members if m["month_gamble_win_count"] > 0],
                key=lambda m: -m["month_gamble_win_count"],
            )[:5],
        },
        {
            "id": "points", "label": "獲得ポイント", "unit": "pt",
            "count_key": "month_points_earned",
            "entries": sorted(
                [m for m in members if m["month_points_earned"] > 0],
                key=lambda m: -m["month_points_earned"],
            )[:5],
        },
    ]
    return render_template(
        "members_list.html", members=members, ranking=ranking, month_ranking=month_ranking,
        extra_rankings=extra_rankings,
    )


def _summarize_feedback_with_gemini(body):
    """意見箱の投稿本文をGeminiで整理する。失敗時はNoneを返す(投稿自体は原文のまま保存する)。"""
    api_keys = collect.get_api_keys()
    if not api_keys:
        return None
    try:
        from google.genai import types
        model = os.environ.get("GEMINI_MODEL", collect.DEFAULT_GEMINI_MODEL)
        config = types.GenerateContentConfig(
            system_instruction=FEEDBACK_SUMMARY_SYSTEM_INSTRUCTION, temperature=0.2,
        )
        response = collect._gemini_generate_with_fallback(model, body, config, api_keys, label="feedback_summary")
        if response is None or not getattr(response, "text", None):
            return None
        return response.text.strip()
    except Exception as e:
        log.warning("Feedback summary generation failed: %s", e)
        return None


@app.route("/feedback")
def community_feedback():
    """コミュニティ意見箱: 不具合報告・要望・質問を自由記述で管理者に送れるページ。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        my_reports = users.list_feedback_reports_by_user(conn, my_id)
    finally:
        conn.close()
    return render_template("community_feedback.html", my_reports=my_reports)


@app.route("/feedback/submit", methods=["POST"])
def community_feedback_submit():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    body = request.form.get("body", "").strip()[:FEEDBACK_BODY_MAX_LENGTH]
    if not body:
        return _finish("community_feedback", "内容を入力してください。", success=False)
    summary = _summarize_feedback_with_gemini(body)
    now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    conn = _users_conn()
    try:
        users.create_feedback_report(conn, my_id, body, summary, now_str)
        _record_audit("意見箱に投稿", body[:100])
    finally:
        conn.close()
    return _finish("community_feedback", "送信しました。ご協力ありがとうございます。")


@app.route("/admin/feedback")
@requires_admin
def admin_feedback():
    conn = _users_conn()
    try:
        reports = users.list_pending_feedback_reports(conn)
    finally:
        conn.close()
    return render_template("admin_feedback.html", reports=reports)


@app.route("/admin/feedback/<int:report_id>/resolve", methods=["POST"])
@requires_admin
def admin_feedback_resolve(report_id):
    my_id = session.get("user_id")
    conn = _users_conn()
    try:
        report = users.get_feedback_report(conn, report_id)
        if report is None or report["status"] != "pending":
            abort(404)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        users.resolve_feedback_report(conn, report_id, my_id, now_str)
        _record_audit("意見箱の投稿を対応済みにした", f"report_id={report_id}")
    finally:
        conn.close()
    return _finish("admin_feedback", "対応済みにしました。")


@app.route("/admin/clamav")
@requires_admin
def admin_clamav():
    conn = _users_conn()
    try:
        recent_events = [
            e for e in users.list_audit_log(conn, limit=500)
            if "マルウェア" in e["action"] or "ウイルス" in e["action"]
        ][:20]
    finally:
        conn.close()
    rescan_running = _is_script_running("clamav_rescan")
    rescan_log = None
    rescan_log_at = None
    if CLAMAV_RESCAN_LOG_PATH.exists():
        rescan_log = CLAMAV_RESCAN_LOG_PATH.read_text(errors="replace")[-8000:]
        rescan_log_at = datetime.fromtimestamp(CLAMAV_RESCAN_LOG_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return render_template(
        "admin_clamav.html",
        db_info=_clamav_db_info(), service_status=_clamav_service_status(),
        stale_days_threshold=CLAMAV_SIGNATURE_STALE_DAYS, recent_events=recent_events,
        rescan_running=rescan_running, rescan_log=rescan_log, rescan_log_at=rescan_log_at,
        rescan_dirs=[str(d) for d in CLAMAV_RESCAN_DIRS],
    )


@app.route("/admin/clamav/selftest", methods=["POST"])
@requires_admin
def admin_clamav_selftest():
    result = _run_clamav_selftest()
    _record_audit(
        "ウイルス対策セルフテスト",
        f"ok={result['ok']} detected={result['detected']} error={result['error']}",
    )
    if result["ok"] and result["detected"]:
        return _finish("admin_clamav", "EICARテスト検体を正しく検知しました。ウイルス対策は正常に機能しています。")
    if result["ok"] and not result["detected"]:
        return _finish(
            "admin_clamav",
            "ClamAVは応答していますが、既知のテスト検体(EICAR)を検知できませんでした。"
            "シグネチャDBや設定を確認してください。",
            success=False,
        )
    return _finish(
        "admin_clamav", f"スキャンを実行できませんでした: {result['error']}", success=False,
    )


@app.route("/admin/clamav/rescan", methods=["POST"])
@requires_admin
def admin_clamav_rescan():
    if _is_script_running("clamav_rescan"):
        return _finish("admin_clamav", "再スキャンは既に実行中です。", success=False)
    _run_clamav_rescan_async()
    _record_audit("ウイルス対策の一括再スキャンを開始", f"対象: {', '.join(str(d) for d in CLAMAV_RESCAN_DIRS)}")
    return _finish("admin_clamav", "再スキャンを開始しました。件数によっては数分かかります。")


@app.route("/members/<int:user_id>")
def member_detail(user_id):
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        history = users.login_history(conn, user_id)
        first_login_at = users.first_login(conn, user_id)
        counts_by_day = users.login_counts_by_day(conn, user_id)
        total_logins = users.login_count(conn, user_id)
        streak = users.login_streak(conn, user_id, datetime.now(JST).date())
        comment_count = users.comment_count(conn, user_id)
        lifetime_points = users.total_earned_points(conn, user_id)
        owned_frame_count = len(users.owned_frame_ids(conn, user_id))
        last_active_at = users.last_active(conn, user_id)
        gamble_won_count = users.won_fog_bet_count(conn, user_id)
        msg_count = users.messages_sent_count(conn, user_id)
        reaction_given_count = users.reactions_given_count(conn, user_id)
        reaction_received_count = users.reactions_received_count(conn, user_id)
        mention_received_count = users.mentions_received_count(conn, user_id)
        fog_quiz_stats_for_badges = users.get_daily_fog_quiz_stats(conn, user_id)
        phenom_quiz_correct = users.phenom_quiz_correct_count(conn, user_id)
        lottery_draws = users.lottery_draw_count(conn, user_id)
        file_upload_count = users.shared_file_upload_count(conn, user_id)
        file_star_count = users.shared_file_star_count(conn, user_id)
        title_complete = len(users.owned_cosmetic_ids(conn, user_id, "title")) >= len(TITLE_DEFINITIONS)
        name_color_complete = (
            len(users.owned_cosmetic_ids(conn, user_id, "name_color")) >= len(NAME_COLOR_DEFINITIONS)
        )
        points_spent = users.total_spent_points(conn, user_id)
        ranking_first_count = users.monthly_ranking_first_count(conn, user_id)
        station_share_count = users.station_share_count(conn, user_id)
    finally:
        conn.close()
    favorite_stations = [s for s in target["favorite_stations"].split(",") if s]
    chat_conn = chat_log.connect(CHAT_DB_PATH)
    try:
        chat_count = chat_log.count_user_messages_since(chat_conn, user_id, "0000-01-01 00:00:00")
    finally:
        chat_conn.close()
    try:
        created_at = datetime.strptime(target["created_at"][:19], "%Y-%m-%d %H:%M:%S")
        account_age_days = (now_jst_naive() - created_at).days
    except (ValueError, TypeError):
        account_age_days = 0
    has_avatar = _avatar_path(user_id).exists()
    night_owl = any(len(h["logged_in_at"]) >= 13 and int(h["logged_in_at"][11:13]) < 4 for h in history)
    early_bird = any(len(h["logged_in_at"]) >= 13 and 5 <= int(h["logged_in_at"][11:13]) < 7 for h in history)
    newyear_login = any(len(h["logged_in_at"]) >= 10 and h["logged_in_at"][5:10] == "01-01" for h in history)
    newyearseve_login = any(len(h["logged_in_at"]) >= 10 and h["logged_in_at"][5:10] == "12-31" for h in history)
    weekend_days = sum(
        1 for d in counts_by_day if datetime.strptime(d, "%Y-%m-%d").weekday() >= 5
    )
    badges = _compute_badges({
        "login_count": total_logins, "streak": streak, "comment_count": comment_count,
        "chat_count": chat_count, "favorite_count": len(favorite_stations),
        "station_total": len(STATION_NAMES), "account_age_days": account_age_days,
        "has_avatar": has_avatar, "bio_set": bool(target["bio"]), "role": target["role"],
        "night_owl": night_owl, "early_bird": early_bird, "weekend_days": weekend_days,
        "newyear_login": newyear_login, "newyearseve_login": newyearseve_login,
        "lifetime_points": lifetime_points, "owned_frame_count": owned_frame_count,
        "gamble_won_count": gamble_won_count, "msg_count": msg_count,
        "reaction_given_count": reaction_given_count, "reaction_received_count": reaction_received_count,
        "mention_received_count": mention_received_count,
        "fog_quiz_correct": fog_quiz_stats_for_badges["total_correct"],
        "phenom_quiz_correct": phenom_quiz_correct, "lottery_draws": lottery_draws,
        "file_upload_count": file_upload_count, "file_star_count": file_star_count,
        "title_complete": title_complete, "name_color_complete": name_color_complete,
        "points_spent": points_spent, "ranking_first_count": ranking_first_count,
        "station_share_count": station_share_count,
    })
    # 実績の達成状況を見る/見られるたびに、新規達成分のポイントを付与する
    # (award_badgeが既付与分を無視するので、何度呼んでも二重加算にはならない)。
    points_conn = _users_conn()
    try:
        _award_new_badges(points_conn, user_id, badges)
        points = users.available_points(points_conn, user_id)
    finally:
        points_conn.close()
    title_label = TITLE_BY_ID.get(target["equipped_title"], {}).get("label", "")
    name_color_class = NAME_COLOR_BY_ID.get(target["equipped_name_color"], {}).get("css_class", "")
    effect_class = EFFECT_BY_ID.get(target["equipped_effect"], {}).get("css_class", "")
    banner_class = BANNER_BY_ID.get(target["equipped_banner"], {}).get("css_class", "")
    title_color_class = TITLE_COLOR_BY_ID.get(target["equipped_title_color"], {}).get("css_class", "")
    marker_icon = MARKER_BY_ID.get(target["equipped_marker"], {}).get("icon", "")
    title_badge_icon = TITLE_BADGE_BY_ID.get(target["equipped_title_badge"], {}).get("icon", "")
    avatar_bg_class = AVATAR_BG_BY_ID.get(target["equipped_avatar_bg"], {}).get("css_class", "")
    seasonal_class = SEASONAL_BY_ID.get(target["equipped_seasonal"], {}).get("css_class", "")
    profile_sound = PROFILE_SOUND_BY_ID.get(target["equipped_profile_sound"], {}).get("sound", "")
    if _current_user_settings()["mute_profile_sounds"]:
        profile_sound = ""  # 設定画面の「ほかの人のプロフィールの効果音を鳴らさない」
    return render_template(
        "member_detail.html",
        member=target,
        title_label=title_label,
        name_color_class=name_color_class,
        effect_class=effect_class,
        banner_class=banner_class,
        title_color_class=title_color_class,
        marker_icon=marker_icon,
        title_badge_icon=title_badge_icon,
        avatar_bg_class=avatar_bg_class,
        seasonal_class=seasonal_class,
        profile_sound=profile_sound,
        history=history,
        counts_by_day=counts_by_day,
        total_logins=total_logins,
        streak=streak,
        favorite_stations=favorite_stations,
        station_names=STATION_NAMES,
        station_region=STATION_REGION,
        first_login=first_login_at,
        last_login=history[0]["logged_in_at"] if history else None,
        has_avatar=has_avatar,
        is_self=(session.get("user_id") == user_id),
        heatmap_days=LOGIN_HEATMAP_DAYS,
        avatar_size=AVATAR_SIZE,
        badges=badges,
        points=points,
        online=_is_online(last_active_at),
        last_active_at=last_active_at,
    )


@app.route("/members/<int:user_id>/profile", methods=["POST"])
def member_profile_update(user_id):
    """自己紹介文とお気に入り地点は本人のみが変更できる(アバターと同じ制限)。"""
    if session.get("user_id") != user_id:
        abort(403)
    bio = request.form.get("bio", "").strip()
    favorite_stations = [s for s in request.form.getlist("favorite_stations") if s in STATION_NAMES]
    conn = _users_conn()
    try:
        users.update_profile(conn, user_id, bio=bio, favorite_stations=",".join(favorite_stations))
    finally:
        conn.close()
    return _finish("member_detail", "プロフィールを更新しました。", user_id=user_id)


@app.route("/game")
def game_home():
    """ポイント・ショップ・川霧予測ゲームをまとめたゲームのトップページ。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    _resolve_pending_fog_bets()
    conn = _users_conn()
    try:
        points = users.available_points(conn, my_id)
        recent_bets = users.list_fog_bets_for_user(conn, my_id, limit=5)
    finally:
        conn.close()
    return render_template("game_home.html", points=points, recent_bets=recent_bets)


@app.route("/game/shop")
def game_shop():
    """ポイントでアバターフレーム・称号・名前の色・ダッシュボード配色・演出・
    川霧予測ゲーム用消費アイテムと交換する画面。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    conn = _users_conn()
    try:
        member = users.get_user_by_id(conn, my_id)
        points = users.available_points(conn, my_id)
        gems = users.available_gems(conn, my_id)
        owned_gem_items = users.owned_gem_item_ids(conn, my_id)
        owned = users.owned_frame_ids(conn, my_id)
        # 各カテゴリのジェム限定アイテム(gem_shop_definitions等とは別に、各カテゴリの
        # 定義リストへcurrency:"gems"付きで直接追加してある)はuser_cosmeticsではなく
        # user_gem_itemsに所持記録があるため、owned_gem_items(IDはカタログ全体で一意)を
        # そのまま和集合して渡す。これでcosmetic_itemマクロのowned判定がそのまま使える。
        owned_titles = users.owned_cosmetic_ids(conn, my_id, "title") | owned_gem_items
        owned_name_colors = users.owned_cosmetic_ids(conn, my_id, "name_color") | owned_gem_items
        owned_dashboard_themes = users.owned_cosmetic_ids(conn, my_id, "dashboard_theme") | owned_gem_items
        owned_effects = users.owned_cosmetic_ids(conn, my_id, "effect") | owned_gem_items
        owned_banners = users.owned_cosmetic_ids(conn, my_id, "banner") | owned_gem_items
        owned_dm_themes = users.owned_cosmetic_ids(conn, my_id, "dm_theme") | owned_gem_items
        owned_title_colors = users.owned_cosmetic_ids(conn, my_id, "title_color") | owned_gem_items
        owned_markers = users.owned_cosmetic_ids(conn, my_id, "marker")
        owned_title_badges = users.owned_cosmetic_ids(conn, my_id, "title_badge") | owned_gem_items
        owned_bubble_styles = users.owned_cosmetic_ids(conn, my_id, "bubble_style") | owned_gem_items
        owned_avatar_bgs = users.owned_cosmetic_ids(conn, my_id, "avatar_bg") | owned_gem_items
        owned_rank_styles = users.owned_cosmetic_ids(conn, my_id, "rank_style") | owned_gem_items
        owned_profile_sounds = users.owned_cosmetic_ids(conn, my_id, "profile_sound") | owned_gem_items
        owned_seasonal = users.owned_cosmetic_ids(conn, my_id, "seasonal") | owned_gem_items
        consumables = users.consumable_quantities(conn, my_id)

        # 装着中のアイテム一覧と、今月の使用料合計(装着中のみ課金されるため)。
        equipped_items = []
        if member["equipped_frame"]:
            frame_def = FRAME_BY_ID.get(member["equipped_frame"])
            if frame_def:
                info = users.frame_billing_info(conn, my_id, member["equipped_frame"])
                equipped_items.append({
                    "category": "frame", "category_label": "フレーム", "label": frame_def["label"],
                    "monthly_fee": frame_def["monthly_fee"],
                    "next_billing_at": info["next_billing_at"] if info else None,
                })
        for category, (catalog, cosmetic_category, cat_label) in COSMETIC_CATALOGS.items():
            item_id = member.get(f"equipped_{category}")
            if not item_id:
                continue
            item_def = catalog.get(item_id)
            if not item_def or item_def.get("currency") == "gems":
                continue  # ジェム品は買い切りで月額請求が無いため、この一覧には出さない
            info = users.cosmetic_billing_info(conn, my_id, cosmetic_category, item_id)
            equipped_items.append({
                "category": category, "category_label": cat_label, "label": item_def["label"],
                "monthly_fee": item_def["monthly_fee"],
                "next_billing_at": info["next_billing_at"] if info else None,
            })
        total_monthly_fee = sum(i["monthly_fee"] for i in equipped_items)
    finally:
        conn.close()
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    new_cutoff = (now_jst_naive() - timedelta(days=SHOP_NEW_BADGE_DAYS)).strftime("%Y-%m-%d")

    def _is_new(item):
        return bool(item.get("added")) and item["added"] >= new_cutoff

    return render_template(
        "game_shop.html",
        member=member,
        points=points,
        gems=gems,
        gem_shop_definitions=GEM_SHOP_DEFINITIONS,
        owned_gem_items=owned_gem_items,
        equipped_items=equipped_items,
        total_monthly_fee=total_monthly_fee,
        owned=owned,
        frame_grades=FRAME_GRADES,
        frame_themes=FRAME_THEMES,
        frame_definitions=FRAME_DEFINITIONS,
        title_definitions=TITLE_DEFINITIONS,
        owned_titles=owned_titles,
        name_color_definitions=NAME_COLOR_DEFINITIONS,
        owned_name_colors=owned_name_colors,
        dashboard_theme_definitions=DASHBOARD_THEME_DEFINITIONS,
        owned_dashboard_themes=owned_dashboard_themes,
        effect_definitions=EFFECT_DEFINITIONS,
        owned_effects=owned_effects,
        banner_definitions=BANNER_DEFINITIONS,
        owned_banners=owned_banners,
        dm_theme_definitions=DM_THEME_DEFINITIONS,
        owned_dm_themes=owned_dm_themes,
        title_color_definitions=TITLE_COLOR_DEFINITIONS,
        owned_title_colors=owned_title_colors,
        marker_definitions=MARKER_DEFINITIONS,
        owned_markers=owned_markers,
        title_badge_definitions=TITLE_BADGE_DEFINITIONS,
        owned_title_badges=owned_title_badges,
        bubble_style_definitions=BUBBLE_STYLE_DEFINITIONS,
        owned_bubble_styles=owned_bubble_styles,
        avatar_bg_definitions=AVATAR_BG_DEFINITIONS,
        owned_avatar_bgs=owned_avatar_bgs,
        rank_style_definitions=RANK_STYLE_DEFINITIONS,
        owned_rank_styles=owned_rank_styles,
        profile_sound_definitions=PROFILE_SOUND_DEFINITIONS,
        owned_profile_sounds=owned_profile_sounds,
        seasonal_definitions=SEASONAL_DEFINITIONS,
        owned_seasonal=owned_seasonal,
        today_str=today_str,
        is_new=_is_new,
        gacha_cost=GACHA_COST,
        consumable_definitions=CONSUMABLE_DEFINITIONS,
        consumables=consumables,
    )


def _check_slot_change_allowed(current_item_id, current_billing_info, target_item_id):
    """月額使用料アイテムは「装着中のものの次回請求日が来た(=月初の更新タイミング)時だけ
    選び直せて、それ以外は固定」という仕様にするための事前チェック。次回請求日より前に
    外してしまうと請求されずに済んでしまう抜け道を塞ぐため、変更(別アイテムへの
    切り替え・解除の両方を含む)は次回請求日到達後にしか許可しない。target_item_idが
    現在と同じ(実質変更なし)場合は常に許可する。
    戻り値: 変更してよければNone、拒否するなら理由メッセージ。"""
    if not current_item_id or current_item_id == target_item_id:
        return None
    next_billing_at = current_billing_info.get("next_billing_at") if current_billing_info else None
    if not next_billing_at:
        return None
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    if next_billing_at > now_str:
        return f"次回の更新日({next_billing_at[:10]})まで、装着中のアイテムは変更(解除も含む)できません。"
    return None


def _next_billing_first_of_month(base_dt=None):
    """次回請求日として「翌月1日 00:00:00 JST」の文字列を返す。
    base_dt が None の場合は現在時刻(JST)を使う。
    月額使用料の請求タイミングを月初に統一するために使う。"""
    if base_dt is None:
        base_dt = datetime.now(JST)
    if base_dt.month == 12:
        first = base_dt.replace(year=base_dt.year + 1, month=1, day=1,
                                hour=0, minute=0, second=0, microsecond=0)
    else:
        first = base_dt.replace(month=base_dt.month + 1, day=1,
                                hour=0, minute=0, second=0, microsecond=0)
    return first.strftime("%Y-%m-%d %H:%M:%S")


def _charge_billing_if_due(conn, user_id, kind, item_id, next_billing_at, fee, advance_billing_fn):
    """装備しようとしているアイテムの月額使用料の次回請求日が過ぎていれば、装着を許可する
    前にその場で1ヶ月分を請求する(装備中のものだけを課金する使用料モデルのため、装着操作
    そのものが「今月分を払って使い始める」タイミングになる)。feeは呼び出し側がカタログ
    (FRAME_BY_ID/COSMETIC_CATALOGS)から引いた現在のmonthly_feeを渡す(user_frames/
    user_cosmetics.costは取得時点の記録用でしかなく、福袋等でcost=0のまま所持している
    場合もあるため、使用料は必ずカタログの現在価格から計算する)。advance_billing_fnは
    請求成功時に次回請求日を1ヶ月先へ更新する関数(新しいnext_billing_at文字列を1つ受け取る)。
    戻り値: (装着してよいか, 失敗時にユーザーへ見せるメッセージ)。"""
    if not next_billing_at:
        return True, ""
    now = datetime.now(JST)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    if next_billing_at > now_str:
        return True, ""
    balance = users.available_points(conn, user_id)
    if balance < fee:
        return False, f"月額使用料{fee}ptが不足しているため装着できません(現在の残高: {balance}pt)。"
    source = f"subscription_fee:{kind}:{item_id}:{next_billing_at}"
    charged = users.grant_points(conn, user_id, source, -fee, now_str)
    if charged:
        next_billing_str = _next_billing_first_of_month(now)
        advance_billing_fn(next_billing_str)
    return True, ""


@app.route("/game/shop/buy", methods=["POST"])
def game_shop_buy():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    frame = FRAME_BY_ID.get(request.form.get("frame_id", ""))
    if frame is None:
        abort(404)
    conn = _users_conn()
    try:
        if frame["id"] in users.owned_frame_ids(conn, my_id):
            return _finish("game_shop", "そのフレームは既に持っています。", success=False)
        now = datetime.now(JST)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        next_billing_str = _next_billing_first_of_month(now)
        ok = users.spend_points_if_available(
            conn, my_id, frame["cost"],
            lambda c: users.purchase_frame(c, my_id, frame["id"], frame["cost"], now_str, next_billing_str),
        )
        if not ok:
            return _finish("game_shop", "ポイントが足りません。", success=False)
    finally:
        conn.close()
    return _finish("game_shop", f"「{frame['label']}」と交換しました(初月分は購入価格に含まれています。翌月以降は毎月{_monthly_fee(frame['cost'])}ptの使用料がかかります)。")


@app.route("/game/shop/equip", methods=["POST"])
def game_shop_equip():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    frame_id = request.form.get("frame_id", "").strip()
    conn = _users_conn()
    try:
        if frame_id and frame_id not in users.owned_frame_ids(conn, my_id):
            abort(400)
        current_frame_id = users.get_user_by_id(conn, my_id)["equipped_frame"]
        current_info = users.frame_billing_info(conn, my_id, current_frame_id) if current_frame_id else None
        lock_message = _check_slot_change_allowed(current_frame_id, current_info, frame_id)
        if lock_message:
            return _finish("game_shop", lock_message, success=False)
        if frame_id:
            billing_info = users.frame_billing_info(conn, my_id, frame_id)
            frame_def = FRAME_BY_ID.get(frame_id)
            billed_ok, fee_message = _charge_billing_if_due(
                conn, my_id, "frame", frame_id,
                billing_info["next_billing_at"] if billing_info else None,
                frame_def["monthly_fee"] if frame_def else 0,
                lambda next_at: users.set_frame_next_billing(conn, my_id, frame_id, next_at),
            )
            if not billed_ok:
                return _finish("game_shop", fee_message, success=False)
        users.set_equipped_frame(conn, my_id, frame_id)
    finally:
        conn.close()
    message = "フレームを装着しました。" if frame_id else "フレームを外しました。"
    return _finish("game_shop", message)


# category(URLの<category>) -> (カタログのdict, users.pyに渡すcategory名, 表示用ラベル)
COSMETIC_CATALOGS = {
    "title": (TITLE_BY_ID, "title", "称号"),
    "name_color": (NAME_COLOR_BY_ID, "name_color", "名前の色"),
    "dashboard_theme": (DASHBOARD_THEME_BY_ID, "dashboard_theme", "ダッシュボード配色"),
    "effect": (EFFECT_BY_ID, "effect", "演出"),
    "banner": (BANNER_BY_ID, "banner", "プロフィールバナー"),
    "dm_theme": (DM_THEME_BY_ID, "dm_theme", "メッセージ配色"),
    "title_color": (TITLE_COLOR_BY_ID, "title_color", "称号の色"),
    "marker": (MARKER_BY_ID, "marker", "殿堂入り章"),
    "title_badge": (TITLE_BADGE_BY_ID, "title_badge", "称号バッジ"),
    "bubble_style": (BUBBLE_STYLE_BY_ID, "bubble_style", "チャット吹き出し形状"),
    "avatar_bg": (AVATAR_BG_BY_ID, "avatar_bg", "アバター背景エフェクト"),
    "rank_style": (RANK_STYLE_BY_ID, "rank_style", "ランキング数字の書体"),
    "profile_sound": (PROFILE_SOUND_BY_ID, "profile_sound", "プロフィール効果音"),
    "seasonal": (SEASONAL_BY_ID, "seasonal", "季節限定コレクション"),
}


@app.route("/game/shop/buy/<category>", methods=["POST"])
def game_shop_buy_cosmetic(category):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    entry = COSMETIC_CATALOGS.get(category)
    if entry is None:
        abort(404)
    catalog, cosmetic_category, label = entry
    item = catalog.get(request.form.get("item_id", ""))
    if item is None:
        abort(404)
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    if item.get("available_from") and today_str < item["available_from"]:
        return _finish("game_shop", "まだ販売期間ではありません。", success=False)
    if item.get("available_until") and today_str > item["available_until"]:
        return _finish("game_shop", "販売期間が終了しました。", success=False)
    if item.get("admin_only") and session.get("role") != "admin":
        return _finish("game_shop", "この称号は管理者のみ入手できます。", success=False)
    is_gem = item.get("currency") == "gems"
    conn = _users_conn()
    try:
        already_owned = (
            item["id"] in users.owned_gem_item_ids(conn, my_id) if is_gem
            else item["id"] in users.owned_cosmetic_ids(conn, my_id, cosmetic_category)
        )
        if already_owned:
            return _finish("game_shop", f"その{label}は既に持っています。", success=False)
        now = datetime.now(JST)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        if is_gem:
            ok = users.spend_gems_if_available(
                conn, my_id, item["cost_gems"],
                lambda c: users.purchase_gem_item(c, my_id, item["id"], item["cost_gems"], now_str),
            )
            if not ok:
                return _finish("game_shop", "ジェムが足りません。", success=False)
            gems = users.available_gems(conn, my_id)
        else:
            next_billing_str = _next_billing_first_of_month(now)
            ok = users.spend_points_if_available(
                conn, my_id, item["cost"],
                lambda c: users.purchase_cosmetic(
                    c, my_id, cosmetic_category, item["id"], item["cost"], now_str, next_billing_str
                ),
            )
            if not ok:
                return _finish("game_shop", "ポイントが足りません。", success=False)
            points = users.available_points(conn, my_id)
    finally:
        conn.close()
    if is_gem:
        return _finish(
            "game_shop", f"「{item['label']}」と交換しました。",
            extra={"action": "buy", "category": category, "item_id": item["id"], "available_gems": gems},
        )
    return _finish(
        "game_shop",
        f"「{item['label']}」と交換しました(初月分は購入価格に含まれています。"
        f"翌月以降は毎月{_monthly_fee(item['cost'])}ptの使用料がかかります)。",
        extra={"action": "buy", "category": category, "item_id": item["id"], "available_points": points},
    )


@app.route("/game/shop/equip/<category>", methods=["POST"])
def game_shop_equip_cosmetic(category):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    entry = COSMETIC_CATALOGS.get(category)
    if entry is None:
        abort(404)
    catalog, cosmetic_category, label = entry
    item_id = request.form.get("item_id", "").strip()
    target_def = catalog.get(item_id) if item_id else None
    target_is_gem = bool(target_def) and target_def.get("currency") == "gems"
    conn = _users_conn()
    try:
        if item_id:
            owned = (
                item_id in users.owned_gem_item_ids(conn, my_id) if target_is_gem
                else item_id in users.owned_cosmetic_ids(conn, my_id, cosmetic_category)
            )
            if not owned:
                abort(400)
        current_item_id = users.get_user_by_id(conn, my_id).get(f"equipped_{category}")
        # 現在装着中のものがジェム品(買い切り)の場合はcosmetic_billing_infoが必ずNoneを
        # 返す(user_cosmeticsに存在しないため)ので、_check_slot_change_allowedは自動的に
        # 「ロックなし」と判定する(ジェム品には月額サイクルという概念自体が無いため)。
        current_info = (
            users.cosmetic_billing_info(conn, my_id, cosmetic_category, current_item_id)
            if current_item_id else None
        )
        lock_message = _check_slot_change_allowed(current_item_id, current_info, item_id)
        if lock_message:
            return _finish("game_shop", lock_message, success=False)
        if item_id and not target_is_gem:
            billing_info = users.cosmetic_billing_info(conn, my_id, cosmetic_category, item_id)
            billed_ok, fee_message = _charge_billing_if_due(
                conn, my_id, "cosmetic", f"{cosmetic_category}:{item_id}",
                billing_info["next_billing_at"] if billing_info else None,
                target_def["monthly_fee"] if target_def else 0,
                lambda next_at: users.set_cosmetic_next_billing(conn, my_id, cosmetic_category, item_id, next_at),
            )
            if not billed_ok:
                return _finish("game_shop", fee_message, success=False)
        users.set_equipped_cosmetic(conn, my_id, cosmetic_category, item_id)
    finally:
        conn.close()
    message = f"{label}を装着しました。" if item_id else f"{label}を外しました。"
    return _finish(
        "game_shop", message,
        extra={"action": "equip" if item_id else "unequip", "category": category, "item_id": item_id},
    )


@app.route("/game/shop/gems/buy", methods=["POST"])
def game_shop_buy_gem_item():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    item = GEM_SHOP_BY_ID.get(request.form.get("item_id", ""))
    if item is None:
        abort(404)
    conn = _users_conn()
    try:
        if item["id"] in users.owned_gem_item_ids(conn, my_id):
            return _finish("game_shop", f"「{item['label']}」は既に持っています。", success=False)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        ok = users.spend_gems_if_available(
            conn, my_id, item["cost_gems"],
            lambda c: users.purchase_gem_item(c, my_id, item["id"], item["cost_gems"], now_str),
        )
        if not ok:
            return _finish("game_shop", "ジェムが足りません。", success=False)
        gems = users.available_gems(conn, my_id)
    finally:
        conn.close()
    return _finish(
        "game_shop", f"「{item['label']}」と交換しました。",
        extra={"action": "buy_gem_item", "item_id": item["id"], "available_gems": gems},
    )


@app.route("/game/shop/gems/equip", methods=["POST"])
def game_shop_equip_gem_item():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    item_id = request.form.get("item_id", "").strip()
    conn = _users_conn()
    try:
        if item_id and item_id not in users.owned_gem_item_ids(conn, my_id):
            abort(400)
        users.set_equipped_gem_item(conn, my_id, item_id)
    finally:
        conn.close()
    message = "装着しました。" if item_id else "外しました。"
    return _finish("game_shop", message)


def _gacha_is_jackpot(weighted):
    """福袋の当たり判定。確率は常に1/GACHA_BIG_REFUND_WEIGHTに固定する(アイテム側の
    重みをそのまま分母に混ぜると、アイテムの価格帯によって当たり確率が事実上100%近くまで
    ぶれてしまうバグが過去にあったため、確率計算とアイテム抽選の重み計算を分離してある)。
    test_economy_sanity.pyがモンテカルロで確率を検証するため、判定だけを取り出した
    独立関数にしている(この関数の中身を変えるならそのテストも見直すこと)。"""
    return not weighted or random.random() < (1.0 / GACHA_BIG_REFUND_WEIGHT)


SUBSCRIPTION_BILLING_CHECK_INTERVAL_SECONDS = 1800
_subscription_billing_check_state = {"at": 0.0}
_subscription_billing_check_lock = threading.Lock()


def _charge_due_subscriptions():
    """装備したまま放置されているフレーム・コスメについて、月額使用料の支払期限
    (next_billing_at)が来ているものを一括で請求する。_charge_billing_if_dueが装着操作
    そのもののタイミングで課金するのに対し、こちらは既に装着済みのまま何もしなくても
    毎月ちゃんと引き落とされるようにするための見回り役(fog_bets清算・経済異常検知と同じく
    専用cronは持たず、よく踏まれるページから間引いて呼ぶ)。残高が足りなければ所持は
    残したまま自動で未装着に戻す(装備解除のみ。再装着時に_charge_billing_if_dueが
    改めて請求を試みる)。"""
    now = time.monotonic()
    with _subscription_billing_check_lock:
        if now - _subscription_billing_check_state["at"] < SUBSCRIPTION_BILLING_CHECK_INTERVAL_SECONDS:
            return
        _subscription_billing_check_state["at"] = now
    conn = _users_conn()
    try:
        wall_now = datetime.now(JST)
        now_str = wall_now.strftime("%Y-%m-%d %H:%M:%S")
        next_billing_str = _next_billing_first_of_month(wall_now)

        for user_id, frame_id, _cost, next_billing_at in users.list_equipped_frames_due(conn, now_str):
            frame_def = FRAME_BY_ID.get(frame_id)
            fee = frame_def["monthly_fee"] if frame_def else 0
            if users.available_points(conn, user_id) >= fee:
                source = f"subscription_fee:frame:{frame_id}:{next_billing_at}"
                if users.grant_points(conn, user_id, source, -fee, now_str):
                    users.set_frame_next_billing(conn, user_id, frame_id, next_billing_str)
            else:
                users.set_equipped_frame(conn, user_id, "")

        for user_id, category, item_id, _cost, next_billing_at in users.list_equipped_cosmetics_due(conn, now_str):
            entry = COSMETIC_CATALOGS.get(category)
            item_def = entry[0].get(item_id) if entry else None
            fee = item_def["monthly_fee"] if item_def else 0
            if users.available_points(conn, user_id) >= fee:
                source = f"subscription_fee:cosmetic:{category}:{item_id}:{next_billing_at}"
                if users.grant_points(conn, user_id, source, -fee, now_str):
                    users.set_cosmetic_next_billing(conn, user_id, category, item_id, next_billing_str)
            else:
                users.set_equipped_cosmetic(conn, user_id, category, "")
    finally:
        conn.close()


ECONOMY_ANOMALY_CHECK_INTERVAL_SECONDS = 1800
ECONOMY_ANOMALY_WINDOW_MINUTES = 60
ECONOMY_ANOMALY_POINT_THRESHOLD = 50000
# 重要度「高」の観測報告は承認なしで即座に40ジェムが確定するため、短時間に大量の
# 「高」報告を連発するファーミングを検知する(1時間に10件超のペース)。
ECONOMY_ANOMALY_GEM_THRESHOLD = 400
_economy_anomaly_check_state = {"at": 0.0}
_economy_anomaly_check_lock = threading.Lock()


def _check_economy_anomalies():
    """直近ECONOMY_ANOMALY_WINDOW_MINUTES分でECONOMY_ANOMALY_POINT_THRESHOLDpt以上を
    獲得したユーザーがいないかを軽く見張る(福袋バグのような無限増殖系の不正を、誰かが
    気づく前に自動で検知するため)。よく踏まれるページから間引いて呼ぶだけの簡易版で、
    専用のバッチ処理は用意していない。検知したら操作ログとアラートWebhookの両方に残す
    (Webhook未設定でも操作ログには必ず残るようにしている)。"""
    now = time.monotonic()
    with _economy_anomaly_check_lock:
        if now - _economy_anomaly_check_state["at"] < ECONOMY_ANOMALY_CHECK_INTERVAL_SECONDS:
            return
        _economy_anomaly_check_state["at"] = now
    conn = _users_conn()
    try:
        cutoff = (now_jst_naive() - timedelta(minutes=ECONOMY_ANOMALY_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            "SELECT user_id, SUM(amount) FROM point_events WHERE created_at >= ? AND amount > 0 "
            "GROUP BY user_id HAVING SUM(amount) >= ?",
            (cutoff, ECONOMY_ANOMALY_POINT_THRESHOLD),
        ).fetchall()
        gem_rows = conn.execute(
            "SELECT user_id, SUM(amount) FROM gem_events WHERE created_at >= ? AND amount > 0 "
            "GROUP BY user_id HAVING SUM(amount) >= ?",
            (cutoff, ECONOMY_ANOMALY_GEM_THRESHOLD),
        ).fetchall()
    finally:
        conn.close()
    for user_id, total in rows:
        message = (
            f"user_id={user_id}が直近{ECONOMY_ANOMALY_WINDOW_MINUTES}分で{total}pt獲得しています"
            f"(閾値{ECONOMY_ANOMALY_POINT_THRESHOLD}pt)。ポイント無限増殖バグの疑いがあるため確認してください。"
        )
        _record_audit("経済異常検知", message)
        _send_alert_throttled(f"economy_anomaly_user_{user_id}", message)
    for user_id, total in gem_rows:
        message = (
            f"user_id={user_id}が直近{ECONOMY_ANOMALY_WINDOW_MINUTES}分で{total}ジェム獲得しています"
            f"(閾値{ECONOMY_ANOMALY_GEM_THRESHOLD}ジェム)。重要度「高」の観測報告乱発による"
            f"ファーミングの疑いがあるため確認してください。"
        )
        _record_audit("経済異常検知(ジェム)", message)
        _send_alert_throttled(f"economy_anomaly_gem_user_{user_id}", message)


@app.route("/game/shop/gacha", methods=["POST"])
def game_shop_gacha():
    """福袋: 固定ポイントで、未所持コスメの中からランダムに1つ交換するか、ポイントを
    払い戻す。GACHA_BIG_REFUND_WEIGHT分の1の確率で大きめの払い戻し、残りは安いアイテム
    ほど当たりやすい重み付き抽選(値段の逆数を重みにする)。全部所持済みなら参加賞として
    少額ポイントを返す。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        now_dt = datetime.now(JST)
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        next_billing_str = _next_billing_first_of_month(now_dt)
        candidates = []
        for category, (catalog, cosmetic_category, label) in COSMETIC_CATALOGS.items():
            owned = users.owned_cosmetic_ids(conn, my_id, cosmetic_category)
            today_str = now_str[:10]
            for item in catalog.values():
                if item.get("currency") == "gems":
                    continue  # ジェム限定品は福袋(ポイント抽選)の対象外
                if item["id"] in owned:
                    continue
                if item.get("available_from") and today_str < item["available_from"]:
                    continue
                if item.get("available_until") and today_str > item["available_until"]:
                    continue
                candidates.append((category, cosmetic_category, label, item))
        weighted = [(c, cc, lb, it, 1.0 / max(it["cost"], 1)) for c, cc, lb, it in candidates]
        is_jackpot = _gacha_is_jackpot(weighted)
        if not is_jackpot:
            roll = random.random() * sum(w for *_, w in weighted)
            chosen = weighted[-1]
            for entry in weighted:
                roll -= entry[4]
                if roll <= 0:
                    chosen = entry
                    break

        result = {}

        def _spend(c):
            # 参加費(GACHA_COST)はconsumable_purchases経由で記録し(total_spent_pointsの
            # 集計対象)、当たり分だけを別途grant_pointsで加算する(二重計上にならない)。
            users.buy_consumable(c, my_id, "gacha_draw", GACHA_COST, now_str)
            if is_jackpot:
                if weighted:
                    refund = int(round(GACHA_COST * GACHA_BIG_REFUND_MULTIPLIER))
                    users.grant_points(c, my_id, f"gacha_refund:{uuid.uuid4()}", refund, now_str)
                    result["message"] = f"福袋の結果: {refund}pt の大当たり!"
                else:
                    users.grant_points(c, my_id, f"gacha_consolation:{uuid.uuid4()}", GACHA_CONSOLATION_POINTS, now_str)
                    result["message"] = f"すべて所持済みだったため、参加賞として{GACHA_CONSOLATION_POINTS}ptを獲得しました。"
            else:
                _, cosmetic_category, label, item, _ = chosen
                users.purchase_cosmetic(c, my_id, cosmetic_category, item["id"], 0, now_str, next_billing_str)
                result["message"] = f"福袋の結果: 「{item['label']}」({label})を獲得しました!"

        ok = users.spend_points_if_available(conn, my_id, GACHA_COST, _spend)
        if not ok:
            return _finish("game_shop", "ポイントが足りません。", success=False)
        points = users.available_points(conn, my_id)
    finally:
        conn.close()
    return _finish("game_shop", result["message"], extra={"available_points": points})


@app.route("/game/shop/buy_consumable", methods=["POST"])
def game_shop_buy_consumable():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    item = CONSUMABLE_BY_ID.get(request.form.get("item_id", ""))
    if item is None:
        abort(404)
    conn = _users_conn()
    try:
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        ok = users.spend_points_if_available(
            conn, my_id, item["cost"],
            lambda c: users.buy_consumable(c, my_id, item["id"], item["cost"], now_str),
        )
        if not ok:
            return _finish("game_shop", "ポイントが足りません。", success=False)
        points = users.available_points(conn, my_id)
        quantity = users.consumable_quantities(conn, my_id).get(item["id"], 0)
    finally:
        conn.close()
    return _finish(
        "game_shop", f"「{item['label']}」を購入しました。",
        extra={"action": "buy_consumable", "item_id": item["id"], "available_points": points, "quantity": quantity},
    )


@app.route("/game/gamble")
def game_gamble():
    """川霧予測ゲーム: 地点・日付・時間精度を選び、倍率のプレビューを見ながら賭ける画面。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    _resolve_pending_fog_bets()
    conn = _users_conn()
    try:
        points = users.available_points(conn, my_id)
        bets = users.list_fog_bets_for_user(conn, my_id)
        consumables = users.consumable_quantities(conn, my_id)
    finally:
        conn.close()
    fog_rates = _station_fog_rates()
    station_rows = sorted(
        (
            {
                "name": name,
                "fog_rate": fog_rates.get(name),
                "rarity_multiplier": _gamble_multiplier(fog_rates.get(name), 0, "day"),
            }
            for name in STATION_NAMES
        ),
        key=lambda r: -r["rarity_multiplier"],
    )
    return render_template(
        "game_gamble.html",
        points=points, bets=bets, station_rows=station_rows,
        station_names=STATION_NAMES, precisions=GAMBLE_PRECISIONS, blocks=GAMBLE_BLOCKS,
        min_bet=GAMBLE_MIN_BET,
        today=now_jst_naive().strftime("%Y-%m-%d"),
        consumables=consumables, consumable_definitions=CONSUMABLE_DEFINITIONS,
        consumable_boost_multiplier=CONSUMABLE_BOOST_MULTIPLIER,
        consumable_super_boost_multiplier=CONSUMABLE_SUPER_BOOST_MULTIPLIER,
        now_str=now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"),
    )


@app.route("/game/gamble/preview")
def game_gamble_preview():
    """賭けフォームが入力を変えるたびに叩く、倍率だけを返す軽量プレビューAPI。
    入力が不正/不完全な場合は倍率算出をせずmultiplier: nullを返す(フォーム側でエラー表示は
    別途行うため、ここではエラーメッセージまでは作らない)。"""
    station = request.args.get("station", "")
    target_date = request.args.get("target_date", "")
    precision_id = request.args.get("precision", "")
    hour = request.args.get("hour", type=int)
    block_index = request.args.get("block_index", type=int)

    if station not in STATION_NAMES or precision_id not in GAMBLE_PRECISION_BY_ID or not DATE_RE.match(target_date or ""):
        return jsonify({"multiplier": None})
    window = _gamble_window_for_bet(target_date, precision_id, hour=hour, block_index=block_index)
    if window is None:
        return jsonify({"multiplier": None})
    window_start, _ = window
    try:
        lead_hours = (
            datetime.strptime(window_start, "%Y-%m-%d %H:%M:%S") - now_jst_naive()
        ).total_seconds() / 3600.0
    except ValueError:
        return jsonify({"multiplier": None})
    if lead_hours < 0 or lead_hours > GAMBLE_MAX_LEAD_HOURS:
        return jsonify({"multiplier": None})
    fog_rate = _station_fog_rates().get(station)
    multiplier = _gamble_multiplier(fog_rate, lead_hours, precision_id)
    return jsonify({"multiplier": multiplier})


@app.route("/game/gamble/place", methods=["POST"])
def game_gamble_place():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    station = request.form.get("station", "")
    target_date = request.form.get("target_date", "")
    precision_id = request.form.get("precision", "")
    hour = request.form.get("hour", type=int)
    block_index = request.form.get("block_index", type=int)
    bet_amount = request.form.get("bet_amount", type=int)
    use_insurance = request.form.get("use_insurance") == "1"
    use_boost = request.form.get("use_boost") == "1"
    use_super_boost = request.form.get("use_super_boost") == "1"

    if station not in STATION_NAMES:
        return _finish("game_gamble", "地点を選んでください。", success=False)
    if precision_id not in GAMBLE_PRECISION_BY_ID:
        return _finish("game_gamble", "時間精度を選んでください。", success=False)
    if not DATE_RE.match(target_date or ""):
        return _finish("game_gamble", "日付が正しくありません。", success=False)
    if bet_amount is None or bet_amount < GAMBLE_MIN_BET:
        return _finish(
            "game_gamble", f"賭けポイントは{GAMBLE_MIN_BET}pt以上で指定してください。",
            success=False,
        )

    window = _gamble_window_for_bet(target_date, precision_id, hour=hour, block_index=block_index)
    if window is None:
        return _finish("game_gamble", "指定した時刻が観測時間帯(4-19時)の範囲外です。", success=False)
    window_start, window_end = window

    now = now_jst_naive()
    lead_hours = (datetime.strptime(window_start, "%Y-%m-%d %H:%M:%S") - now).total_seconds() / 3600.0
    if lead_hours < 0:
        return _finish("game_gamble", "過去の時刻には賭けられません。", success=False)
    if lead_hours > GAMBLE_MAX_LEAD_HOURS:
        return _finish("game_gamble", "1週間より先の時刻には賭けられません。", success=False)

    fog_rate = _station_fog_rates().get(station)
    multiplier = _gamble_multiplier(fog_rate, lead_hours, precision_id)

    conn = _users_conn()
    try:
        owned = users.consumable_quantities(conn, my_id)
        if use_insurance and owned.get("insurance_ticket", 0) < 1:
            return _finish("game_gamble", "保険チケットを持っていません。", success=False)
        if use_super_boost and owned.get("super_boost_ticket", 0) < 1:
            return _finish("game_gamble", "上級倍率ブーストチケットを持っていません。", success=False)
        # 下のuse_boost実消費(_spend)は上級版が指定されていれば通常版を無視する(効果を
        # 重複させないため)。ここの事前チェックもそれに合わせないと、上級版のみ所持する
        # 利用者が両方のフラグを立てて送信した場合に、実際には使わない通常版の在庫不足で
        # 誤って拒否されてしまう(2026-09-13の監査で指摘)。
        if use_boost and not use_super_boost and owned.get("boost_ticket", 0) < 1:
            return _finish("game_gamble", "倍率ブーストチケットを持っていません。", success=False)

        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        applied_multiplier = {"value": multiplier}

        def _spend(c):
            # 上級版と通常版が同時に指定された場合は上級版だけを使う(効果は重複させない)。
            # 在庫チェックは上で済ませているが、直後に別リクエストが使い切る競合が
            # ありうるため、use_consumableの戻り値も必ず確認する(確認しないと、
            # チケットを消費できなかったのに倍率だけ適用されてしまう)。
            m = applied_multiplier["value"]
            if use_super_boost:
                if not users.use_consumable(c, my_id, "super_boost_ticket"):
                    raise _ConsumableUnavailable()
                m = round(m * CONSUMABLE_SUPER_BOOST_MULTIPLIER, 2)
            elif use_boost:
                if not users.use_consumable(c, my_id, "boost_ticket"):
                    raise _ConsumableUnavailable()
                m = round(m * CONSUMABLE_BOOST_MULTIPLIER, 2)
            if use_insurance:
                if not users.use_consumable(c, my_id, "insurance_ticket"):
                    raise _ConsumableUnavailable()
            applied_multiplier["value"] = m
            users.place_fog_bet(
                c, my_id, station, precision_id, window_start, window_end, bet_amount, m, now_str,
                insurance=use_insurance,
            )

        try:
            ok = users.spend_points_if_available(conn, my_id, bet_amount, _spend)
        except _ConsumableUnavailable:
            return _finish(
                "game_gamble", "チケットの在庫が変わったため賭けられませんでした。もう一度お試しください。",
                success=False,
            )
        if not ok:
            return _finish("game_gamble", "ポイントが足りません。", success=False)
        multiplier = applied_multiplier["value"]
    finally:
        conn.close()
    return _finish(
        "game_gamble",
        f"{station}に{bet_amount}pt賭けました(倍率{multiplier}倍)。観測が終わり次第、答え合わせされます。",
    )


@app.route("/game/gamble/cancel/<int:bet_id>", methods=["POST"])
def game_gamble_cancel(bet_id):
    """見送り券を使い、観測時間帯が始まる前ならpending中の賭けを全額払い戻して取り消す。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _users_conn()
    try:
        bet = users.get_fog_bet(conn, bet_id)
        if bet is None or bet["user_id"] != my_id:
            abort(404)
        if bet["status"] != "pending":
            return _finish("game_gamble", "この賭けは既に答え合わせ済みです。", success=False)
        window_start_dt = datetime.strptime(bet["window_start"], "%Y-%m-%d %H:%M:%S")
        if now_jst_naive() >= window_start_dt:
            return _finish("game_gamble", "観測時間帯が始まっているため取り消せません。", success=False)
        if not users.use_consumable(conn, my_id, "withdraw_ticket"):
            return _finish("game_gamble", "見送り券を持っていません。", success=False)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        # 賭けた分(bet_amount)を払い戻す。以前はここに0を渡しており、total_spent_pointsが
        # 取り消し済みの賭けも「使った」扱いのまま数え続けるため、「全額払い戻しました」と
        # 表示しながら実際には賭けた分を没収していた(2026-10-23に発覚)。
        users.cancel_fog_bet(conn, bet_id, now_str, bet["bet_amount"])
        users.grant_points(conn, my_id, f"gamble_withdraw:{bet_id}", bet["bet_amount"], now_str)
    finally:
        conn.close()
    return _finish("game_gamble", "賭けを取り消し、全額払い戻しました。")


@app.route("/game/daily")
def game_daily():
    """無料のデイリーゲーム(川霧予報クイズ・現象当てクイズ・くじ)をまとめたページ。
    川霧予測ゲームと違いポイントを賭けないため、ポイントが無い新規ユーザーでも遊べる。"""
    my_id = session.get("user_id")
    if not my_id:
        return redirect(url_for("login", next=request.path))
    _resolve_pending_daily_fog_quiz()
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    fog_quiz_station = _daily_fog_quiz_station(today_str)
    phenom_item = _todays_phenom_quiz_item(today_str)
    phenom_image_url = None
    if phenom_item:
        date_dir, hhmm, station, _code = phenom_item
        phenom_image_url = url_for("images_file", date=date_dir, hhmm=hhmm, filename=f"{station}.jpg")

    conn = _users_conn()
    try:
        points = users.available_points(conn, my_id)
        fog_quiz_today = users.get_daily_fog_quiz(conn, my_id, today_str)
        fog_quiz_stats = users.get_daily_fog_quiz_stats(conn, my_id)
        phenom_quiz_today = users.get_daily_phenom_quiz(conn, my_id, today_str)
        lottery_result = users.get_point_event_amount(conn, my_id, f"daily_lottery:{today_str}")
        consumables = users.consumable_quantities(conn, my_id)
    finally:
        conn.close()
    return render_template(
        "game_daily.html",
        points=points,
        fog_quiz_station=fog_quiz_station, fog_quiz_today=fog_quiz_today, fog_quiz_stats=fog_quiz_stats,
        phenom_item=phenom_item, phenom_image_url=phenom_image_url, phenom_quiz_today=phenom_quiz_today,
        phenom_categories=PHENOM_QUIZ_CATEGORIES, phenom_quiz_points=PHENOM_QUIZ_POINTS,
        lottery_result=lottery_result, lottery_outcomes=LOTTERY_OUTCOMES,
        fog_quiz_base_points=FOG_QUIZ_BASE_POINTS, fog_quiz_streak_bonus=FOG_QUIZ_STREAK_BONUS_PER_DAY,
        consumables=consumables,
    )


@app.route("/game/daily/fog_quiz/guess", methods=["POST"])
def game_daily_fog_quiz_guess():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    guess = request.form.get("guess")
    if guess not in ("yes", "no"):
        return _finish("game_daily", "予想を選んでください。", success=False)
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    station = _daily_fog_quiz_station(today_str)
    conn = _users_conn()
    try:
        created = users.place_daily_fog_quiz_guess(
            conn, my_id, today_str, station, guess == "yes",
            now_jst_naive().strftime("%Y-%m-%d %H:%M:%S"),
        )
    finally:
        conn.close()
    if not created:
        return _finish("game_daily", "今日はもう回答済みです。", success=False)
    return _finish("game_daily", f"「{station}」の予想を受け付けました。観測が終わり次第、答え合わせされます。")


@app.route("/game/daily/phenom_quiz/hint", methods=["POST"])
def game_daily_phenom_quiz_hint():
    """クイズヒントチケットを1個消費し、誤った選択肢を1つランダムに選んで返す
    (どれが正解かは明かさない)。フォーム送信ではなくボタンクリックからfetchで
    直接呼ぶため、_finish()は使わずJSONを返す。"""
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    item = _todays_phenom_quiz_item(today_str)
    if item is None:
        return jsonify({"success": False, "message": "今日は出題できる画像がありません。"})
    _date_dir, _hhmm, _station, code = item
    correct_category = _phenom_category_id_for_code(code)
    conn = _users_conn()
    try:
        if users.get_daily_phenom_quiz(conn, my_id, today_str) is not None:
            return jsonify({"success": False, "message": "今日はもう回答済みです。"})
        if not users.use_consumable(conn, my_id, "quiz_hint_ticket"):
            return jsonify({"success": False, "message": "クイズヒントチケットを持っていません。"})
        wrong_ids = [c["id"] for c in PHENOM_QUIZ_CATEGORIES if c["id"] != correct_category]
        eliminate = random.choice(wrong_ids)
    finally:
        conn.close()
    return jsonify({"success": True, "eliminate": eliminate})


@app.route("/game/daily/phenom_quiz/answer", methods=["POST"])
def game_daily_phenom_quiz_answer():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    item = _todays_phenom_quiz_item(today_str)
    if item is None:
        return _finish("game_daily", "今日は出題できる画像がありません。", success=False)
    _date_dir, _hhmm, _station, code = item
    category_guess = request.form.get("category", "")
    if category_guess not in {c["id"] for c in PHENOM_QUIZ_CATEGORIES}:
        return _finish("game_daily", "選択肢を選んでください。", success=False)
    correct_category = _phenom_category_id_for_code(code)
    correct = category_guess == correct_category
    now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    conn = _users_conn()
    try:
        created = users.place_daily_phenom_quiz_answer(conn, my_id, today_str, category_guess, correct, now_str)
        if created and correct:
            users.grant_points(conn, my_id, f"phenom_quiz:{today_str}", PHENOM_QUIZ_POINTS, now_str)
    finally:
        conn.close()
    if not created:
        return _finish("game_daily", "今日はもう回答済みです。", success=False)
    correct_label = next((c["label"] for c in PHENOM_QUIZ_CATEGORIES if c["id"] == correct_category), "不明")
    if correct:
        return _finish("game_daily", f"正解！「{correct_label}」でした。(+{PHENOM_QUIZ_POINTS}pt)")
    return _finish("game_daily", f"不正解。正解は「{correct_label}」でした。")


@app.route("/game/daily/lottery/draw", methods=["POST"])
def game_daily_lottery_draw():
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    today_str = now_jst_naive().strftime("%Y-%m-%d")
    source = f"daily_lottery:{today_str}"
    use_luck = request.form.get("use_luck") == "1"
    conn = _users_conn()
    try:
        existing = users.get_point_event_amount(conn, my_id, source)
        if existing is not None:
            return _finish("game_daily", f"今日はもう引いています(結果: {existing}pt)。", success=False)
        used_luck = False
        if use_luck:
            if users.consumable_quantities(conn, my_id).get("lottery_luck_ticket", 0) < 1:
                return _finish("game_daily", "ラッキーチケットを持っていません。", success=False)
            used_luck = users.use_consumable(conn, my_id, "lottery_luck_ticket")
        weights = [
            w * LOTTERY_LUCK_WEIGHT_MULTIPLIER if used_luck and a >= 100 else w
            for w, a in LOTTERY_OUTCOMES
        ]
        amount = random.choices([a for _, a in LOTTERY_OUTCOMES], weights=weights, k=1)[0]
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        if not users.grant_points(conn, my_id, source, amount, now_str):
            # 直前にほぼ同時の二重送信があった場合、実際に記録された方の金額を表示する
            amount = users.get_point_event_amount(conn, my_id, source)
    finally:
        conn.close()
    return _finish("game_daily", f"デイリーくじ: {amount}pt獲得！")
