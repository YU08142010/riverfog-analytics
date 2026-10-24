"""公開ページ(public_base.html系、/forecast以下)の日本語・英語対応(2026-10-24)。

対象は公開ページのみ(ログイン後の研究サイト本体・管理画面・コミュニティ機能は対象外)。
gettext/Flask-Babel等は使わず、文字列IDごとの辞書を素直に引く単純な方式にした
(対象ページが少数で、動的な値を埋め込む箇所も{}でシンプルに済むため)。

- STRINGS: 静的なUI文言(ja/en)。pf_t(key, **kwargs)で引く。
- JS_STRINGS: public_forecast.htmlのJS側に window.PF_I18N として渡す分。
- LABEL_EN: 観測・予測の現象ラベル(PHENOM_LABELS・PUBLIC_LEVELS・rain_label等、webapp.py側で
  既に日本語で組み立て済みの文字列)を英語化するための橋渡し辞書。pf_label(text)で引く
  (地点名などの固有名詞は対象外のため、ここに無い文字列はそのまま返す)。
"""

PUBLIC_LANG_COOKIE = "pf_lang"
PUBLIC_LANGS = ("ja", "en")
DEFAULT_LANG = "ja"


def resolve_lang(request):
    q = request.args.get("lang")
    if q in PUBLIC_LANGS:
        return q
    c = request.cookies.get(PUBLIC_LANG_COOKIE)
    if c in PUBLIC_LANGS:
        return c
    return DEFAULT_LANG


STRINGS = {
    "ja": {
        "skip_to_content": "本文へ移動",
        "site_title_suffix": "川霧発生予測(試験公開)",
        "meta_description": "只見川・会津地方の河川カメラ設置地点を対象に、3日先までの川霧の発生可能性を機械学習で予測する研究の試験公開ページ。",
        "og_title": "川霧発生予測(試験公開)",
        "og_description": "只見川・会津地方における川霧の発生時刻・地点を機械学習で予測する研究の試験公開。",
        "brand_name": "川霧発生予測",
        "brand_tagline": "只見川・会津地方 研究成果の試験公開",
        "nav_forecast": "発生予測",
        "nav_about": "研究の概要",
        "nav_to_site": "研究サイトへ",
        "nav_login": "関係者ログイン",
        "fullscreen_btn": "全画面で表示する",
        "theme_btn": "表示の明暗を切り替える",
        "footer_no_republish": "本サイトの内容(予測・カメラ画像・データ・画面のスクリーンショット)を、許可なくSNS・Webサイト等に公開・転載しないこと。",
        "footer_disclaimer": "本ページは研究段階の川霧予測モデルによる試験的な計算結果であり、気象庁の予報ではない。予測には誤りが含まれるため、行動の判断に用いる場合は利用者の責任において判断されたい。",
        "footer_sources": "気象データ: <a href=\"https://open-meteo.com/\" target=\"_blank\" rel=\"noopener\">Open-Meteo</a>"
                           "(CC BY 4.0)・気象庁<br>河川カメラ: 国土交通省「川の防災情報」<br>地図: "
                           "<a href=\"https://maps.gsi.go.jp/development/ichiran.html\" target=\"_blank\" "
                           "rel=\"noopener\">国土地理院</a>(淡色地図)",
        "footer_terms_link": "利用規約",
        "footer_meta": "只見川・会津地方 川霧発生予測(試験公開)",
        "terms_notice_title": "利用規約への同意",
        "terms_notice_lead": "<strong>このサイトの内容を、許可なくネット上に公開しないでください。</strong>"
                              "ご利用の前に、利用規約をお読みいただき、同意をお願いします。",
        "terms_notice_check": "利用規約を読み、同意します",
        "terms_notice_ok": "同意して見る",
        "map_load_failed": "地図を読み込めなかった。",

        "about_title": "研究の概要",
        "about_intro": "本研究は、河川上に発生する霧(川霧)の発生時刻・地点を、気象データと機械学習により予測することを目的とする。"
                        "本ページでは、只見川および会津地方の河川カメラ設置地点を対象に、日中の各時刻について3日先までの"
                        "発生可能性を試験的に公開している。",
        "about_intro_note": "本ページの内容は研究段階の予測モデルによる計算結果であり、気象庁の予報ではない。",
        "about_data_scale": "データの規模",
        "about_stat_stations": "対象地点数",
        "about_stat_total_n": "日中の判定件数(地点×時刻)",
        "about_stat_total_fog": "うち川霧ありと判定された件数",
        "about_stat_period": "記録期間(開始時期は地点により異なる)",
        "about_method": "手法",
        "about_step1_title": "観測",
        "about_step1_body": "国土交通省の河川カメラ画像を1時間ごとに収集する。",
        "about_step2_title": "判定",
        "about_step2_body": "画像をマルチモーダルAI(Google Gemini)で判定し、川霧の有無を記録する。目視で確認・修正した記録も併用する。",
        "about_step3_title": "学習",
        "about_step3_body": "川霧の有無と同時刻の気象要素(気温・湿度・風など)の関係を、勾配ブースティング決定木(LightGBM)により学習する。",
        "about_step4_title": "予測",
        "about_step4_body": "気象予報値を入力し、地点・時刻ごとの川霧発生確率を算出する。モデルは毎日、最新のデータで再学習する。"
                             "当日の残りの時間(最大8時間先)は、毎時のカメラ観測(各地点の直近の霧の有無と、全地点のうち"
                             "霧の地点の割合)を入力に加えた別のモデルで予測し直す。",
        "about_levels_title": "3段階の判定基準",
        "about_levels_high_desc": "予測確率が判定閾値以上の時間帯。閾値は原則として、過去の予報値を入力とした検証データで"
                                   "F1値が最大となるよう決定している{precision_note}。",
        "about_levels_high_precision": "(現行の閾値における適合率は約{pct}%)",
        "about_levels_mid_desc": "予測確率が閾値未満かつ閾値の{pct}%以上の時間帯。",
        "about_levels_low_desc": "上記以外の時間帯。",
        "about_levels_blank_label": "空欄",
        "about_levels_blank_desc": "日の出前・日の入り後の時間帯。カメラ画像による判定ができないため予測対象外とした。",
        "about_accuracy_title": "予測精度の検証",
        "about_accuracy_note": "学習に用いていない直近期間({start}〜{end})について、実際の予報値を入力として評価した結果(日中のみ)。",
        "about_metric_precision": "適合率(「高」と予測した時刻のうち実際に発生した割合)",
        "about_metric_recall": "再現率(実際の発生のうち「高」と予測できた割合)",
        "about_metric_f1": "F1値<br>参考: 気候値(地点・時刻別の過去の発生率)による予測 {clim}",
        "about_compare_model": "本モデル",
        "about_compare_climatology": "気候値",
        "about_compare_aria": "F1値の比較: 本モデル {model}、気候値 {clim}",
        "about_compare_caption": "F1値(大きいほど良い)。気候値は地点・時刻別の過去の発生率だけを用いた予測。",
        "about_accuracy_footer": "川霧は日中の発生率が約2%と稀な現象であり、現段階の予測精度は限定的である。"
                                  "気候値による予測を上回ることを基準に、モデルの改良を継続している。",
        "about_notes_title": "利用上の注意",
        "about_note1": "本ページは研究目的の試験公開であり、内容の変更や公開の停止を予告なく行うことがある。",
        "about_note2": "予測には誤りが含まれる。移動・撮影・観光等の判断に用いる場合は、気象庁の情報等と併せ、"
                        "利用者の責任において判断されたい。",
        "about_note3": "「現況」欄はカメラ画像のAI判定による観測値であり、誤判定を含む場合がある。",
        "about_sources_title": "データの出典",
        "about_source_forecast": "気象の予報値・過去値: <a href=\"https://open-meteo.com/\" target=\"_blank\" "
                                  "rel=\"noopener\">Open-Meteo</a>(CC BY 4.0)",
        "about_source_observed": "過去の気象観測: 気象庁",
        "about_source_camera": "河川カメラの画像: 国土交通省「川の防災情報」",
        "about_source_map": "地図: <a href=\"https://maps.gsi.go.jp/development/ichiran.html\" target=\"_blank\" "
                             "rel=\"noopener\">国土地理院</a>(地理院タイル・淡色地図)",
        "about_staff_note": "研究関係者は、右上の「関係者ログイン」から研究用サイトを利用できる。",

        "not_found_title": "ページが見つかりません",
        "not_found_body": "URLが間違っているか、ページが移動・削除された可能性があります。",
        "not_found_back": "発生予測のトップへ戻る",

        "terms_title": "利用規約",

        "station_back": "← 全地点の予測に戻る",
        "station_no_observation": "観測なし",
        "station_lens_haze_note": "※本地点のカメラはレンズが曇りやすく、現況の判定は参考値である",
        "station_official_link": "この地点の河川カメラ(国土交通省「川の防災情報」)",
        "station_outlook_calm": "3日間とも発生可能性「中」以上の時間帯はない",
        "station_outlook_aria": "3日間の見込み",
        "station_3day_title": "3日間の発生予測",
        "station_days_caption": "高・中 = 発生可能性の段階(色の薄いマスは「低」)、空欄 = 夜間(対象外)。",
        "station_days_asof": "{time} 時点の計算結果",
        "station_days_nowcast": "(本日の残りの時間は {time} の観測を加えて再計算)",
        "station_night_na": "夜間(対象外)",
        "station_rain_forecast": "雨の予報あり",
        "station_no_forecast_yet": "本地点の予測は現在算出されていない。",
        "station_now_title": "現況(観測)",
        "station_now_at": "({hour}時の観測)",
        "station_now_note": "{time} 時点のカメラ画像をAIで判定した結果(誤判定を含む場合がある)。",
        "station_location_title": "位置",
        "station_map_aria": "{name}の位置の地図",
        "station_coords": "{region} 緯度 {lat}、経度 {lon}",
        "station_history_title": "過去の発生傾向",
        "station_history_summary": "日中の判定 <b>{n}</b> 件のうち、{word}ありは <b>{n_fog}</b> 件(発生率 <b>{rate}%</b>)である。",
        "station_by_month": "月別発生率(%)",
        "station_by_hour": "時刻別発生率(%)",
        "station_history_footer": "数値は各月・各時刻の{word}の発生率(%)。判定件数が{min_n}件未満の区分は「-」とした。"
                                   "2026年8月以前は、別手法で収集し目視で確認した記録である。",
        "station_history_pending": "集計は準備中である。",
        "station_bar_aria": "{unit}ごとの{word}の割合",
        "station_bar_title_pct": "{key}{unit}: {pct}%(判定{n}件)",
        "station_bar_insufficient": "データ不足",
        "unit_month": "月",
        "unit_hour": "時",
    },
    "en": {
        # --- public_base.html(共通の枠) ---
        "skip_to_content": "Skip to content",
        "site_title_suffix": "River Fog Forecast (Pilot)",
        "meta_description": "A pilot research page forecasting the likelihood of river fog up to 3 days ahead, "
                             "using machine learning, for river-camera sites in the Tadami River / Aizu area.",
        "og_title": "River Fog Forecast (Pilot)",
        "og_description": "A research pilot using machine learning to forecast when and where river fog will occur "
                           "in the Tadami River / Aizu area.",
        "brand_name": "River Fog Forecast",
        "brand_tagline": "Tadami River / Aizu area — research pilot",
        "nav_forecast": "Forecast",
        "nav_about": "About this research",
        "nav_to_site": "Go to research site",
        "nav_login": "Staff login",
        "fullscreen_btn": "View fullscreen",
        "theme_btn": "Toggle light/dark",
        "footer_no_republish": "Do not publish or republish this site's content (forecasts, camera images, data, "
                                "or screenshots) on social media or other websites without permission.",
        "footer_disclaimer": "This page shows experimental output from a research-stage river fog forecast model. "
                              "It is not an official forecast from the Japan Meteorological Agency. Forecasts may "
                              "be inaccurate; use them at your own responsibility if acting on them.",
        "footer_sources": "Weather data: <a href=\"https://open-meteo.com/\" target=\"_blank\" rel=\"noopener\">"
                           "Open-Meteo</a> (CC BY 4.0) &middot; Japan Meteorological Agency<br>River cameras: "
                           "MLIT “River Disaster Prevention Information”<br>Map: "
                           "<a href=\"https://maps.gsi.go.jp/development/ichiran.html\" target=\"_blank\" "
                           "rel=\"noopener\">GSI Japan</a> (pale map)",
        "footer_terms_link": "Terms of use",
        "footer_meta": "Tadami River / Aizu area river fog forecast (pilot)",
        "terms_notice_title": "Agree to the terms of use",
        "terms_notice_lead": "Please do not share this site's content publicly online without permission. "
                              "Read the terms of use below and agree before continuing.",
        "terms_notice_check": "I have read and agree to the terms of use",
        "terms_notice_ok": "Agree and continue",
        "map_load_failed": "Could not load the map.",

        # --- public_about.html ---
        "about_title": "About this research",
        "about_intro": "This research forecasts when and where fog forms over rivers (“river fog”), using "
                        "weather data and machine learning. This page is a pilot publication of the forecast for "
                        "each daytime hour, up to 3 days ahead, at the river-camera sites in the Tadami River and "
                        "Aizu area.",
        "about_intro_note": "The content on this page is computed output from a research-stage forecast model; it "
                             "is not a forecast from the Japan Meteorological Agency.",
        "about_data_scale": "Scale of the data",
        "about_stat_stations": "Number of sites",
        "about_stat_total_n": "Daytime classifications (site × hour)",
        "about_stat_total_fog": "Classified as river fog",
        "about_stat_period": "Recorded period (start varies by site)",
        "about_method": "Method",
        "about_step1_title": "Observation",
        "about_step1_body": "River-camera images from the Ministry of Land, Infrastructure, Transport and Tourism "
                             "are collected hourly.",
        "about_step2_title": "Classification",
        "about_step2_body": "Images are classified by a multimodal AI (Google Gemini) for the presence of river "
                             "fog. Manually checked/corrected records are also used.",
        "about_step3_title": "Training",
        "about_step3_body": "A gradient-boosted decision tree (LightGBM) learns the relationship between river fog "
                             "and weather variables (temperature, humidity, wind, etc.) at the same time.",
        "about_step4_title": "Forecasting",
        "about_step4_body": "Weather forecast values are used to compute the probability of river fog for each "
                             "site and hour. The model is retrained daily on the latest data. For the rest of the "
                             "current day (up to 8 hours ahead), a separate model recomputes the forecast using the "
                             "hourly camera observations (each site's latest fog status and the fraction of foggy "
                             "sites overall) as additional input.",
        "about_levels_title": "The 3-level forecast scale",
        "about_levels_high_desc": "Hours where the forecast probability is at or above the decision threshold. The "
                                   "threshold is generally chosen to maximize the F1 score on validation data using "
                                   "past forecast values{precision_note}.",
        "about_levels_high_precision": " (the current threshold's precision is about {pct}%)",
        "about_levels_mid_desc": "Hours where the probability is below the threshold but at or above {pct}% of it.",
        "about_levels_low_desc": "All other hours.",
        "about_levels_blank_label": "(blank)",
        "about_levels_blank_desc": "Hours before sunrise or after sunset. Camera images cannot be classified then, "
                                    "so these hours are excluded from the forecast.",
        "about_accuracy_title": "Forecast accuracy check",
        "about_accuracy_note": "Evaluated on the most recent period not used for training ({start} – {end}), "
                                "using the actual forecast values as input (daytime only).",
        "about_metric_precision": "Precision (share of hours forecast as “high” that actually had fog)",
        "about_metric_recall": "Recall (share of actual fog occurrences forecast as “high”)",
        "about_metric_f1": "F1 score<br>Reference: forecast by climatology (past occurrence rate by site/hour) "
                            "{clim}",
        "about_compare_model": "This model",
        "about_compare_climatology": "Climatology",
        "about_compare_aria": "F1 score comparison: this model {model}, climatology {clim}",
        "about_compare_caption": "F1 score (higher is better). Climatology uses only the past occurrence rate by "
                                  "site and hour.",
        "about_accuracy_footer": "River fog is rare — it occurs in only about 2% of daytime hours — so "
                                  "current forecast accuracy is limited. The model is continually improved against "
                                  "the benchmark of beating the climatology forecast.",
        "about_notes_title": "Notes on use",
        "about_note1": "This page is a pilot publication for research purposes; its content or availability may "
                        "change or stop without notice.",
        "about_note2": "Forecasts may contain errors. If using them to decide on travel, photography, sightseeing, "
                        "etc., check official sources such as the Japan Meteorological Agency and use your own "
                        "judgment.",
        "about_note3": "The “current conditions” field is an AI-classified observation from camera "
                        "images and may contain misclassifications.",
        "about_sources_title": "Data sources",
        "about_source_forecast": "Weather forecast / past values: <a href=\"https://open-meteo.com/\" "
                                  "target=\"_blank\" rel=\"noopener\">Open-Meteo</a> (CC BY 4.0)",
        "about_source_observed": "Past weather observations: Japan Meteorological Agency",
        "about_source_camera": "River camera images: MLIT “River Disaster Prevention Information”",
        "about_source_map": "Map: <a href=\"https://maps.gsi.go.jp/development/ichiran.html\" target=\"_blank\" "
                             "rel=\"noopener\">GSI Japan</a> (GSI tiles, pale map)",
        "about_staff_note": "Research staff can use the research site via “Staff login” at the top right.",

        # --- public_not_found.html ---
        "not_found_title": "Page not found",
        "not_found_body": "The URL may be incorrect, or the page may have moved or been removed.",
        "not_found_back": "Back to the forecast top page",

        # --- public_terms.html ---
        "terms_title": "Terms of use",

        # --- public_station.html ---
        "station_back": "← Back to all sites",
        "station_no_observation": "No observation",
        "station_lens_haze_note": "※ This site's camera lens tends to fog up, so the current-conditions "
                                   "reading is a rough reference only.",
        "station_official_link": "This site's river camera (MLIT “River Disaster Prevention "
                                  "Information”)",
        "station_outlook_calm": "No hour in the next 3 days reaches “medium” likelihood or higher",
        "station_outlook_aria": "3-day outlook",
        "station_3day_title": "3-day forecast",
        "station_days_caption": "High/Medium = forecast likelihood level (lighter cells = “low”), blank = "
                                 "nighttime (not forecast).",
        "station_days_asof": "As of {time}",
        "station_days_nowcast": " (the rest of today is recomputed using the {time} observation)",
        "station_night_na": "Nighttime (not forecast)",
        "station_rain_forecast": "Rain forecast",
        "station_no_forecast_yet": "No forecast is currently available for this site.",
        "station_now_title": "Current conditions (observed)",
        "station_now_at": "(observed at {hour}:00)",
        "station_now_note": "AI classification of the camera image as of {time} (may include misclassifications).",
        "station_location_title": "Location",
        "station_map_aria": "Map of {name}'s location",
        "station_coords": "{region} Lat {lat}, Lon {lon}",
        "station_history_title": "Past occurrence trend",
        "station_history_summary": "Of <b>{n}</b> daytime classifications, <b>{n_fog}</b> were classified as "
                                    "{word} (occurrence rate <b>{rate}%</b>).",
        "station_by_month": "Occurrence rate by month (%)",
        "station_by_hour": "Occurrence rate by hour (%)",
        "station_history_footer": "Figures are the {word} occurrence rate (%) for each month/hour. Bins with fewer "
                                   "than {min_n} classifications are shown as “-”. Records before August "
                                   "2026 were collected by a different method and visually checked.",
        "station_history_pending": "Statistics are not yet available.",
        "station_bar_aria": "Share of {word} by {unit}",
        "station_bar_title_pct": "{key}{unit}: {pct}% ({n} classifications)",
        "station_bar_insufficient": "insufficient data",
        "unit_month": "month",
        "unit_hour": "hour",
    },
}


def t(lang, key, **kwargs):
    table = STRINGS.get(lang) or {}
    s = table.get(key)
    if s is None:
        s = STRINGS["ja"].get(key)
    if s is None:
        s = key
    return s.format(**kwargs) if kwargs else s


# --- public_forecast.html のJS側に渡す分(window.PF_I18N) -----------------------------------
JS_STRINGS = {
    "ja": None,  # 日本語はJS側の元の文言をそのまま使う(このキーが無ければ日本語フォールバック)。
    "en": {
        "headline_today": "Today",
        "headline_tomorrow": "Tomorrow",
        "headline_day_after": "Day after tomorrow",
        "headline_suffix": "’s river fog forecast",
        "summary_title": "{label} ({date}) forecast summary",
        "now_label_suffix": " ({hour}:00)",
        "map_load_failed": "Could not load the map. See the table below.",
        "map_aria": "Map of river fog likelihood by site (see the table below for details)",
        "map_aria_at": "Map of river fog likelihood by site ({when}. See the table below for details)",
        "answer_today_ended": "Today's daytime forecast window has ended. See the forecast for tomorrow onward.",
        "answer_not_yet": "This day's forecast has not been computed yet.",
        "answer_high": "{n} site(s) have a “high” likelihood",
        "answer_high_and_mid": ", and {n} site(s) have “medium”",
        "answer_high_tail": " (out of {total} sites).",
        "answer_mid_only": "No sites have “high” likelihood; {n} site(s) have “medium”",
        "answer_mid_tail": " (out of {total} sites).",
        "answer_all_low": "All sites have “low” likelihood.",
        "rank_more": "{n} more site(s) are shown in the table below.",
        "day_max": "Day max",
        "play_label": "▶ Play through the hours",
        "stop_label": "■ Stop",
        "play_aria": "Show each hour from 5:00 to 18:00 in order",
        "stop_aria": "Stop playback",
        "time_hint_max": "Showing each site's daily maximum likelihood. Select an hour to switch to that hour's "
                          "forecast.",
        "time_hint_hour": "Showing the forecast for {hour}:00. {desc}",
        "desc_night": "Outside the forecast window (nighttime).",
        "desc_all_low": "All sites have “low” likelihood.",
        "desc_high_n": "“High”: {n} site(s)",
        "desc_mid_n": "“Medium”: {n} site(s)",
        "desc_prefix": "Likelihood — ",
        "popup_now": "Current (observed): {label}",
        "popup_detail_link": "Site details",
        "popup_to_row_btn": "Go to this row in the table",
        "col_site": "Site",
        "col_now": "Current",
        "col_hour": "{h}:00",
        "filter_all": "All sites ({n})",
        "filter_mid": "“Medium” or higher only ({n})",
        "table_empty": "No site reaches “medium” or higher likelihood today. Select “All sites” "
                       "to see all {n}.",
        "table_cell_title": "{name} {hour}:00: {label}",
        "day_badge_ended": "Daytime forecast ended",
        "day_badge_none": "No forecast",
        "day_badge_all_low": "All sites “low”",
        "day_badge_high": "High {n}",
        "day_badge_mid": "Mid {n}",
        "day_aria": "{label} {date}, {n3} site(s) “high”, {n2} site(s) “medium”",
    },
}


def js_strings(lang):
    return JS_STRINGS.get(lang) or {}


# --- 動的に組み立てられた日本語ラベルの英語化(地点名など固有名詞は対象外) -----------------------
LABEL_EN = {
    # fog_features.PHENOM_LABELS
    "薄い川霧": "light river fog", "川霧": "river fog", "濃い川霧": "dense river fog",
    "薄い全体霧": "light wide fog", "全体霧": "wide fog", "濃い全体霧": "dense wide fog",
    "薄い層雲": "light stratus", "濃い層雲": "dense stratus",
    "霧雨": "drizzle", "雨": "rain", "現象なし": "no phenomenon",
    # PUBLIC_LEVELS
    "発生可能性 高": "Likelihood: High", "発生可能性 中": "Likelihood: Medium",
    "発生可能性 低": "Likelihood: Low",
    "高": "High", "中": "Medium", "低": "Low",
    # fog_features.target_word
    "霧": "fog",
    # 日付ラベル(public_station.htmlの3日間表・見込みチップ、webapp._public_station_outlook等)
    "今日": "Today", "明日": "Tomorrow", "明後日": "Day after tomorrow",
    # misc
    "随時更新": "updated hourly", "日次更新": "updated daily",
}


def label(lang, text):
    """webapp.py側で既に組み立て済みの日本語ラベル(rain_label・PUBLIC_LEVELS・PHENOM_LABELS等)を、
    langがenなら辞書を引いて英語化する(無ければそのまま返す。地点名等の固有名詞はそもそも
    この辞書に無いので影響しない)。"""
    if lang != "en" or not text:
        return text
    if text in LABEL_EN:
        return LABEL_EN[text]
    # 「雨 発生可能性 高」のように雨が先頭に付いた複合ラベルを分解する。
    if text.startswith("雨 "):
        rest = text[2:]
        return "rain, " + (LABEL_EN.get(rest, rest))
    return text
