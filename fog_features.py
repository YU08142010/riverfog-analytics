"""川霧予測モデル用の特徴量エンジニアリング。

~/Desktop/fog-prediction/weather_visualizer.py（Colab向け・単発実行ツール）の
特徴量設計をそのまま踏襲しつつ、river-fogサーバー用に以下を簡略化している。
- 列レイアウトは common.py の固定列を前提とし、Excelの自動レイアウト検出は行わない
- 本番データに「テスト用ダミーブロック」は存在しないため、その検出処理は持たない
"""
import math

import numpy as np
import pandas as pd

from common import WIND_DIR_DEGREES

# --- 現象コード関連（weather_visualizer.pyと同一の定義） ---
PHENOM_LABELS = {
    1: "薄い川霧", 2: "川霧", 3: "濃い川霧", 4: "薄い全体霧", 5: "全体霧",
    6: "全体濃い霧", 7: "薄い層雲", 8: "濃い層雲", 9: "霧雨", 10: "雨",
}
PHENOM_COLORS = {
    1: "#aed6f1", 2: "#3498db", 3: "#1a5276", 4: "#dcdde1", 5: "#909497",
    6: "#2c3e50", 7: "#f8c471", 8: "#d35400", 9: "#a9dfbf", 10: "#196f3d",
}
SLASH_COLOR = "#f9e79f"
SUMMARY_FOG_CODES = {1, 2, 3}  # 「川霧」とみなすコード（川霧F1・川霧確率・サマリー集計・アラートで使用）
# 以前は全体霧(4〜6)も含めていたが、「川霧である確率」等の表示が川霧限定に見えて実態と
# ズレる問題があり、川霧(1〜3)のみに統一した。これによりアラート・ダッシュボード集計・
# モデル評価指標もすべて川霧基準になる(全体霧単独の日はアラート対象外になる)。

# --- 2コード同時観測(2026-09対応) -----------------------------------------------
# 1つの観測に現象コードを最大2つまで同時記録できる(例:雨が降りながら川霧も出ている等)。
# 順序が意味を持つ: 家族グループは{0} < {1,2,3} < {4,5,6} < {7,8} < {9,10}の順に
# 数値が大きくなるよう並んでいる。2コードを組み合わせても川霧(1〜3、SUMMARY_FOG_CODES)は
# 必ず数値的に小さい方=主コード側に入るため、「川霧が観測されたか」を問う既存のロジック
# (較正・アラート・クイズ・賭けゲーム等)は主コードだけ見れば従来通り正しく動く
# (primary_phenom_code参照)。将来コードを追加・採番し直す場合はこの前提を壊さないこと。
PHENOM_CODE_GROUPS = (
    frozenset({0}),
    frozenset({1, 2, 3}),
    frozenset({4, 5, 6}),
    frozenset({7, 8}),
    frozenset({9, 10}),
)


class PhenomCodeError(ValueError):
    """現象コードの組み合わせが不正な場合に投げる(Gemini分類・手動訂正の書き込み境界で使う)。"""


def _group_for_code(code):
    for group in PHENOM_CODE_GROUPS:
        if code in group:
            return group
    return None


def validate_phenom_codes(codes):
    """codes: 現象コードのリスト(1個または2個、各0〜10)。組み合わせが不正なら
    PhenomCodeErrorを投げる(同じグループ内の2つ、0と他の組み合わせ、範囲外、重複等)。
    有効なら何も返さない。"""
    codes = list(codes)
    if len(codes) not in (1, 2):
        raise PhenomCodeError(f"現象コードは1つか2つ指定してください: {codes}")
    for c in codes:
        if not isinstance(c, (int, np.integer)) or isinstance(c, bool) or not (0 <= c <= 10):
            raise PhenomCodeError(f"現象コードは0〜10の整数である必要があります: {c!r}")
    if len(codes) == 2:
        a, b = codes
        if a == b:
            raise PhenomCodeError(f"同じ現象コードを2つ指定することはできません: {codes}")
        if 0 in (a, b):
            raise PhenomCodeError("「現象なし」は他の現象コードと組み合わせられません")
        if _group_for_code(a) == _group_for_code(b):
            raise PhenomCodeError(f"同じグループ内の現象コードは組み合わせられません: {codes}")


def encode_phenom_codes(codes):
    """codes(1個または2個の現象コード)を検証したうえで、格納用の1つの整数値に変換する
    (2個の場合は「小さい方*100+大きい方」にパックする。例: {1,5}->105、{6,10}->610)。
    既存の単一コード用のINTEGER列(weather_store.phenomena.code等)にそのまま格納できる
    (0〜10は単一コードとしてそのまま、100を超える値が2コード同時観測を表す)。"""
    validate_phenom_codes(codes)
    codes = sorted(set(codes))
    return codes[0] if len(codes) == 1 else codes[0] * 100 + codes[1]


def decode_phenom_value(value):
    """encode_phenom_codes/encode_phenomena_cellが返す格納値を(primary, secondary)に
    分解する。secondaryは単一コードまたは欠測ならNone。欠測(None/NaN)は(None, None)。"""
    if value is None:
        return None, None
    try:
        if value != value:  # NaN
            return None, None
    except TypeError:
        pass
    code = int(value)
    if code <= 10:
        return code, None
    return code // 100, code % 100


def primary_phenom_code(value):
    """decode_phenom_value(value)の主コードをfloat(NaN可)で返す。「川霧が観測されたか」
    のように主コードだけで判定できる箇所(較正・アラート・クイズ・賭けゲーム等)で使う。"""
    primary, _secondary = decode_phenom_value(value)
    return float(primary) if primary is not None else np.nan


def phenom_codes_set(value):
    """観測されている現象コードの集合を返す({1}や{1, 5}、欠測は空集合)。
    特定のコードが主・副どちらでもいいので含まれるかを調べたい場合に使う。"""
    primary, secondary = decode_phenom_value(value)
    if primary is None:
        return set()
    return {primary} if secondary is None else {primary, secondary}


def phenom_contains_any(value, code_set):
    return bool(phenom_codes_set(value) & set(code_set))


def phenom_label(value):
    """主・副コードを結合したラベルを返す(例:「薄い川霧＋全体霧」)。
    現象なしは「現象なし」、欠測は「-」。"""
    primary, secondary = decode_phenom_value(value)
    if primary is None:
        return "-"
    if primary == 0:
        return "現象なし"
    label = PHENOM_LABELS.get(primary, f"コード{primary}")
    if secondary is not None:
        label += "＋" + PHENOM_LABELS.get(secondary, f"コード{secondary}")
    return label


def format_phenom_display(value):
    """人間が読むための表記に変換する(例:「1 5」、単一なら「1」、現象なしは「/」)。
    Excelエクスポート・監査ログ等、人が直接読む場面専用(内部処理ではdecode_phenom_value
    /primary_phenom_code等を使うこと)。"""
    primary, secondary = decode_phenom_value(value)
    if primary is None:
        return None
    if primary == 0:
        return "/"
    return str(primary) if secondary is None else f"{primary} {secondary}"

# --- 特徴量の列名（DataFrame上の日本語列名。weather_visualizer.pyと揃えている） ---
COL_TEMP = "気温(℃)"
COL_PRECIP = "降水量(mm)"
COL_WIND_SPEED = "風速(m/s)"
COL_WIND_DIR_DEG = "風向"
COL_HUMIDITY = "相対湿度(％)"
COL_DEWPOINT = "露点温度(℃)"
COL_SUNSHINE = "日照時間(時間)"
COL_SOLAR = "日射量(MJ/m2)"
COL_VISIBILITY = "視程(km)"

BASE_FEATURES = [
    COL_TEMP, COL_PRECIP, COL_WIND_SPEED, COL_HUMIDITY, COL_DEWPOINT,
    "気温露点差", "時_sin", "時_cos", "月",
]
WIND_DIR_FEATURES = ["風向_sin", "風向_cos"]

# ファイルにあれば特徴量として使う任意項目。有効値の割合がMIN_COVERAGE未満なら使わない
# （視程は欠測が多いため、実質的にはここで自動的に足切りされる）。
OPTIONAL_MEASURE_KEYWORDS = [COL_SUNSHINE, COL_SOLAR, COL_VISIBILITY]
OPTIONAL_MEASURE_MIN_COVERAGE = 0.05

LAG_FEATURE_NAMES = ["気温変化_1h", "気温変化_3h", "風速平均_3h", "降水量_24h合計"]
SUN_FEATURE_NAME = "日の出まで残り時間"


def encode_phenomena_cell(value):
    """セルの値を現象コードの格納値に変換する。'/' -> 0、単一コード(1〜10)はそのまま、
    2コード同時観測を表すパック値(encode_phenom_codes/decode_phenom_value参照)もそのまま
    通す。空欄/解釈不能/無効な組み合わせはNaN。"""
    if value is None:
        return np.nan
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
        if np.isnan(value):
            return np.nan
        code = int(value)
    else:
        s = str(value).strip()
        if not s:
            return np.nan
        if s == "/":
            return 0.0
        try:
            code = int(float(s))
        except ValueError:
            return np.nan
    primary, secondary = decode_phenom_value(code)
    if primary is None:
        return np.nan
    codes = [primary] if secondary is None else [primary, secondary]
    try:
        validate_phenom_codes(codes)
    except PhenomCodeError:
        return np.nan
    return float(code)


def encode_wind_direction_degrees(value):
    """common.py が書き込む16方位の日本語表記（例:「北北西」）を度に変換する。無風・不明はNaN。"""
    if value is None:
        return np.nan
    s = str(value).strip()
    return WIND_DIR_DEGREES.get(s, np.nan)


def _lag_series(df, col, hours):
    """`col` の `hours` 時間前の値。datetimeの値そのもので引き当てるため、
    欠測で行が飛んでいても位置ベースの shift() のように誤った差分にならない。
    """
    s = df.drop_duplicates(subset="datetime").set_index("datetime")[col]
    lookup = pd.DatetimeIndex(df["datetime"]) - pd.Timedelta(hours=hours)
    return pd.Series(s.reindex(lookup).to_numpy(), index=df.index)


def _add_lag_features(df):
    df = df.copy()
    df["気温変化_1h"] = df[COL_TEMP] - _lag_series(df, COL_TEMP, 1)
    df["気温変化_3h"] = df[COL_TEMP] - _lag_series(df, COL_TEMP, 3)

    dedup = df.drop_duplicates(subset="datetime").sort_values("datetime")
    dt_index = pd.DatetimeIndex(dedup["datetime"])
    target_index = pd.DatetimeIndex(df["datetime"])

    s = pd.Series(pd.to_numeric(dedup[COL_WIND_SPEED], errors="coerce").to_numpy(), index=dt_index)
    df["風速平均_3h"] = s.rolling("3h", min_periods=1).mean().reindex(target_index).to_numpy()

    precip = pd.to_numeric(dedup[COL_PRECIP], errors="coerce").fillna(0.0)
    s = pd.Series(precip.to_numpy(), index=dt_index)
    df["降水量_24h合計"] = s.rolling("24h", min_periods=1).sum().reindex(target_index).to_numpy()
    return df


def _sunrise_hour(ts, lat, lon):
    """NOAAの簡易式によるその日の日の出時刻の近似（JST、0時からの経過時間）。"""
    n = ts.timetuple().tm_yday
    lat_rad = math.radians(lat)
    decl = math.radians(23.44) * math.sin(math.radians(360.0 / 365.0 * (n - 81)))
    cos_hour_angle = max(-1.0, min(1.0, -math.tan(lat_rad) * math.tan(decl)))
    hour_angle_deg = math.degrees(math.acos(cos_hour_angle))
    solar_noon = 12.0 - lon / 15.0 + 9.0
    return solar_noon - hour_angle_deg / 15.0


def sun_times_hours(ts, lat, lon):
    """その日の日の出・日の入り時刻(JST、0時からの経過時間)。NOAAの計算式(均時差・大気差込み、
    誤差は数分程度)。_sunrise_hour(特徴量用の簡易式)とは別に、時間帯の判定に使う。"""
    import math
    n = pd.Timestamp(ts).timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat_rad = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(lat_rad) * math.cos(decl))
              - math.tan(lat_rad) * math.tan(decl))
    ha = math.degrees(math.acos(max(-1.0, min(1.0, cos_ha))))
    noon_jst_min = 720 - 4 * lon - eqtime + 540
    return (noon_jst_min - 4 * ha) / 60.0, (noon_jst_min + 4 * ha) / 60.0


def is_daylight(dt, lat, lon):
    """その時刻が日の出から日の入りまでの間か。カメラ画像から川霧を判定できるのはこの時間帯
    だけ(日の出前・日の入り後の画像は暗く、川霧があっても「現象なし」と判定されやすい。
    2026-09下旬の実測では4時台の川霧の割合が0.8%、6時台が4.6%だった)。"""
    ts = pd.Timestamp(dt)
    sunrise, sunset = sun_times_hours(ts, lat, lon)
    hour = ts.hour + ts.minute / 60.0
    return sunrise <= hour <= sunset


def daylight_mask(datetimes, lat, lon):
    """is_daylightの配列版(日ごとに1回だけ日の出・日の入りを計算する)。"""
    dt = pd.to_datetime(pd.Series(datetimes)).reset_index(drop=True)
    days = dt.dt.normalize()
    times = {d: sun_times_hours(d, lat, lon) for d in days.unique()}
    hours = (dt - days).dt.total_seconds().to_numpy() / 3600.0
    sunrise = days.map(lambda d: times[d][0]).to_numpy(dtype="float64")
    sunset = days.map(lambda d: times[d][1]).to_numpy(dtype="float64")
    return (hours >= sunrise) & (hours <= sunset)


def _add_sunrise_feature(df, lat, lon):
    """『日の出まで残り何時間か』（明け方以外は0）を特徴量として追加する。"""
    df = df.copy()
    dt = pd.to_datetime(df["datetime"])
    day_start = dt.dt.normalize()
    hour_of_day = (dt - day_start).dt.total_seconds().to_numpy() / 3600.0

    unique_days = day_start.unique()
    sunrise_by_day = {d: _sunrise_hour(pd.Timestamp(d), lat, lon) for d in unique_days}
    sunrise_hours = day_start.map(sunrise_by_day).to_numpy(dtype="float64")

    remaining = sunrise_hours - hour_of_day
    df[SUN_FEATURE_NAME] = np.where((remaining < 0) | (remaining > 12), 0.0, remaining)
    return df


def _add_time_features(df, lat, lon):
    df = df.copy()
    df["時"] = df["datetime"].dt.hour
    df["月"] = df["datetime"].dt.month
    df["気温露点差"] = df[COL_TEMP] - df[COL_DEWPOINT]
    df["時_sin"] = np.sin(2 * np.pi * df["時"] / 24)
    df["時_cos"] = np.cos(2 * np.pi * df["時"] / 24)
    df = _add_lag_features(df)
    df = _add_sunrise_feature(df, lat, lon)
    if COL_WIND_DIR_DEG in df.columns:
        rad = np.deg2rad(pd.to_numeric(df[COL_WIND_DIR_DEG], errors="coerce"))
        df["風向_sin"] = np.sin(rad).fillna(0.0)
        df["風向_cos"] = np.cos(rad).fillna(0.0)
    return df


def select_feature_names(df, exclude_optional=None):
    exclude_optional = exclude_optional or ()
    features = list(BASE_FEATURES)
    if all(f in df.columns for f in WIND_DIR_FEATURES):
        features += WIND_DIR_FEATURES
    for label in OPTIONAL_MEASURE_KEYWORDS:
        if label in exclude_optional:
            continue
        if label in df.columns and df[label].notna().any():
            features.append(label)
    features += [f for f in LAG_FEATURE_NAMES if f in df.columns]
    if SUN_FEATURE_NAME in df.columns:
        features.append(SUN_FEATURE_NAME)
    return features


def _fill_optional_measures(df, medians=None):
    """任意項目（視程・日照時間・日射量）の欠測を中央値で埋める。
    割合が低ければ特徴量から外す対象としてラベルを返す(下記参照)。

    medians（{列名: 中央値}）を渡すと、その値で埋める（学習時に確定した中央値を
    予報時にも一貫して使うため）。渡さなければこのdf自身から中央値を計算する
    （従来通りの学習時の挙動）。戻り値は(df, 実際に使った{列名: 中央値}, カバレッジ不足で
    特徴量から除外すべき列名の集合)。
    以前は予報時にもdf自身（16日分の予報データだけ）から中央値を計算していたため、
    学習時とは全く異なる母集団の中央値で欠測を埋めることになり、同じ特徴量が
    train/inference間で意味的にズレる問題があった。
    以前はカバレッジ不足の列を丸ごとNaNで上書きしてselect_feature_names()に「使うな」と
    伝えていたが、これは実測の疎らな値まで一緒に消してしまい、表示用途等で生の実測値を
    再利用したい将来の呼び出し元にとって破壊的だった(2026-09-13の監査で指摘)。列自体は
    書き換えず、除外すべき列名を別途返す方式に改めた。
    """
    df = df.copy()
    medians = dict(medians) if medians else {}
    used = {}
    excluded = set()
    for label in OPTIONAL_MEASURE_KEYWORDS:
        if label not in df.columns:
            continue
        col = pd.to_numeric(df[label], errors="coerce")
        if label in medians:
            df[label] = col.fillna(medians[label])
            used[label] = medians[label]
            continue
        coverage = col.notna().mean() if len(col) else 0.0
        if coverage >= OPTIONAL_MEASURE_MIN_COVERAGE:
            m = col.median()
            df[label] = col.fillna(m)
            used[label] = m
        else:
            excluded.add(label)
    return df, used, excluded


def prepare_features(df, lat, lon, feature_names=None, medians=None):
    """特徴量一式を組み立てる。降水量の欠測は0mmとして扱う（無降水時は空欄の記録が多いため）。
    戻り値: (df, feature_names, 任意項目の穴埋めに使った中央値のdict)。
    medians引数の意味は_fill_optional_measures参照（予報時に学習時の中央値を渡すために使う）。
    """
    out = _add_time_features(df, lat, lon)
    out, medians_used, excluded_optional = _fill_optional_measures(out, medians=medians)
    out[COL_PRECIP] = pd.to_numeric(out[COL_PRECIP], errors="coerce").fillna(0.0)

    for name in ("気温変化_1h", "気温変化_3h"):
        out[name] = out[name].fillna(0.0)
    out["風速平均_3h"] = out["風速平均_3h"].fillna(out[COL_WIND_SPEED])
    out["降水量_24h合計"] = out["降水量_24h合計"].fillna(0.0)

    if feature_names is None:
        feature_names = select_feature_names(out, exclude_optional=excluded_optional)
    for name in feature_names:
        if name not in out.columns:
            out[name] = np.nan
    return out, feature_names, medians_used


def build_location_class_targets(phenom_df, phenom_cols):
    """地点それぞれの現象コード(主コード側。0="/"、1〜10、欠測はNaN)を作る。

    以前は雨・霧雨(9・10)を学習ターゲットとして0(現象なし)に丸めていたが、これは
    「降水あり時の的中率が降水なし時より20pt以上低い」という精度分析(accuracy.html)の
    結果につながっていた: predicted_code(常に0〜8のいずれか)がactual_code(実況では
    9・10も記録される)と完全一致するかで的中率を測っているため、雨天時は原理的に
    的中しようがなかった。9・10も他のクラスと同様にそのまま学習ターゲットに残すことで、
    モデルが「雨・霧雨っぽい気象条件」と「本当に何も無い(0)」・「川霧(1〜3)」を区別する
    手がかりを学習データから奪わないようにする。川霧である確率(_fog_probability)は
    SUMMARY_FOG_CODESの列だけを合算するため、この変更の影響を受けない。

    2コード同時観測(2026-09対応)の場合はprimary_phenom_code()で主コード(数値的に
    小さい方)だけを取り出す。川霧(1〜3)は家族グループの並び上、2コード同時でも必ず
    主コード側に入るため、川霧かどうか(fog_model.pyの二値の学習ターゲット)は主コード
    だけで正しく判定できる。
    """
    codes = phenom_df[phenom_cols].apply(lambda col: col.map(encode_phenomena_cell))
    targets = codes.apply(lambda col: col.map(primary_phenom_code))
    targets.insert(0, "datetime", phenom_df["datetime"].values)
    return targets

