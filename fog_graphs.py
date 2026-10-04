"""地点ごとの予測グラフ・全地点サマリー・CSV出力。

~/Desktop/fog-prediction/weather_visualizer.py のグラフ設計を踏襲。Colab依存
（日本語フォント自動導入・ノートブック内表示）は持たない。

2026-09-14、matplotlib静的PNGからPlotly(インタラクティブHTML)へ移行した。Plotly.jsは
CDNではなく自己ホストのpinned版(web_static/vendor/)を使う(Tailscale限定公開のアプリで
外部到達性を前提にできないため)。日本語フォントはブラウザ側で描画されるため、
旧版がここで行っていたNoto Sans CJK JPのmatplotlibへの明示addfont登録は不要になった。
"""
import logging
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

log = logging.getLogger("fog_graphs")

# Flaskのstatic_url_path(webapp.py、static_folder="web_static"かつstatic_url_path未指定
# のため既定で"/web_static"になる。app.static_url_pathで要確認)に合わせた絶対URL。
# write_htmlのinclude_plotlyjsに絶対URL文字列を渡すことで、出力先ディレクトリ(地点数×
# 日数分)ごとに1MB近いJSファイルを複製せずに済む('directory'指定は使わない)。
PLOTLY_JS_URL = "/web_static/vendor/plotly-cartesian-2.35.2.min.js"
_HOUR_MS = 3600 * 1000

from fog_features import (
    COL_DEWPOINT,
    COL_HUMIDITY,
    COL_TEMP,
    PHENOM_COLORS,
    PHENOM_LABELS,
    SLASH_COLOR,
    encode_phenomena_cell,
)
from fog_model import BINARY_LABELS, DEFAULT_FOG_THRESHOLD

# 予報の帯(二値の予測)の色。「川霧あり」は実測の帯の「川霧」(コード2)と同じ色にそろえる。
BINARY_FOG_COLOR = PHENOM_COLORS[2]
BINARY_CLEAR_COLOR = "#d5d8dc"


def _phenom_name(code):
    return "現象なし" if code == 0 else PHENOM_LABELS.get(code, f"コード{code}")


def get_last_observed_datetime(phenom_df, col):
    codes = phenom_df[col].map(encode_phenomena_cell)
    valid_dates = phenom_df.loc[codes.notna(), "datetime"]
    if valid_dates.empty:
        return phenom_df["datetime"].max()
    return valid_dates.max()


def _phenom_code_bar(x, codes, hover_prefix):
    """現象コード列(vlinesの列)をgo.Barで再現する。各時刻を幅0.9時間・高さ1の縦棒として
    並べ、色はPHENOM_COLORS(0=SLASH_COLOR)で塗る。凡例は常設せず、ホバーで現象名を
    表示する(matplotlib版の右側の現象コード凡例の代わり。インタラクティブな図では
    常設の凡例より場所を取らないホバーの方が適していると判断した)。"""
    colors, labels = [], []
    for c in codes:
        code = 0 if c != c else int(c)  # NaN(c != c)は「現象なし」扱い
        colors.append(SLASH_COLOR if code == 0 else PHENOM_COLORS.get(code, "#999999"))
        labels.append(f"{hover_prefix}: {_phenom_name(code)}")
    return go.Bar(
        x=x, y=[1] * len(x), base=-0.5, width=_HOUR_MS * 0.9,
        marker={"color": colors, "line": {"width": 0}},
        hovertext=labels, hoverinfo="text", showlegend=False,
    )


def _binary_forecast_bar(x, preds):
    """予報の二値予測(1=川霧あり、0=川霧なし)を、実測の現象コードの帯と同じ形の縦棒で表す。"""
    preds = [int(p) for p in preds]
    return go.Bar(
        x=x, y=[1] * len(x), base=-0.5, width=_HOUR_MS * 0.9,
        marker={"color": [BINARY_FOG_COLOR if p == 1 else BINARY_CLEAR_COLOR for p in preds],
                "line": {"width": 0}},
        hovertext=[f"予報: {BINARY_LABELS[p]}" for p in preds], hoverinfo="text", showlegend=False,
    )


def plot_single_location_forecast(main_df, phenom_df, col, forecast_raw, pred_series, out_dir,
                                  history_days=5, prob_series=None,
                                  fog_threshold=DEFAULT_FOG_THRESHOLD, run_date_str=""):
    """1地点の「実測(直近history_days日) + 今後の予測」グラフ。4段構成
    (気温/露点温度、相対湿度、川霧の予測確率、現象の帯)のインタラクティブHTML。
    現象の帯は、実測の部分は現象コード、予報の部分は二値の予測(川霧あり/なし。
    pred_seriesは1/0)を表す。

    matplotlib版からの変更点(2026-09-14): (1) 気温/露点温度と相対湿度を同じ軸に
    重ねるtwinx()をやめ、湿度を専用の2段目に分けた(2軸グラフは読み違えやすいため
    避ける方針)。(2) 実測の最終記録から予報開始までに空白期間がある場合に横軸を
    分割していた処理を廃止し、常に1本の連続した時間軸にした(インタラクティブな
    パン/ズームで空白期間の圧縮を体感的に補えるため)。
    """
    last_observed = get_last_observed_datetime(phenom_df, col)
    hist_cutoff = last_observed - pd.Timedelta(days=history_days)
    hist_main = main_df[(main_df["datetime"] >= hist_cutoff) &
                        (main_df["datetime"] <= last_observed)].reset_index(drop=True)
    hist_phenom = phenom_df[(phenom_df["datetime"] >= hist_cutoff) &
                            (phenom_df["datetime"] <= last_observed)].reset_index(drop=True)
    hist_codes = hist_phenom[col].map(encode_phenomena_cell).to_numpy(dtype="float64")

    fc_start = forecast_raw["datetime"].min()
    fc_end = forecast_raw["datetime"].max()
    n_days = max(1, round(len(forecast_raw) / 24))
    probs = None if prob_series is None else np.asarray(prob_series, dtype="float64") * 100.0
    pred_codes = np.asarray(pred_series, dtype="int64")
    threshold_pct = fog_threshold * 100.0

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True,
        row_heights=[0.40, 0.14, 0.26, 0.14], vertical_spacing=0.035,
    )

    fig.add_trace(go.Scatter(x=hist_main["datetime"], y=hist_main[COL_TEMP], mode="lines",
                              line={"color": "#e74c3c", "width": 1.4}, name="気温(℃)[実測]"), row=1, col=1)
    fig.add_trace(go.Scatter(x=hist_main["datetime"], y=hist_main[COL_DEWPOINT], mode="lines",
                              line={"color": "#16a085", "width": 1.4}, name="露点温度(℃)[実測]"), row=1, col=1)
    fig.add_trace(go.Scatter(x=forecast_raw["datetime"], y=forecast_raw[COL_TEMP], mode="lines",
                              line={"color": "#e74c3c", "width": 1.6, "dash": "dash"},
                              name="気温(℃)[予報]"), row=1, col=1)
    fig.add_trace(go.Scatter(x=forecast_raw["datetime"], y=forecast_raw[COL_DEWPOINT], mode="lines",
                              line={"color": "#16a085", "width": 1.6, "dash": "dash"},
                              name="露点温度(℃)[予報]"), row=1, col=1)

    fig.add_trace(go.Scatter(x=hist_main["datetime"], y=hist_main[COL_HUMIDITY], mode="lines",
                              line={"color": "#8e44ad", "width": 1.0}, opacity=0.7,
                              name="相対湿度(％)[実測]"), row=2, col=1)
    fig.add_trace(go.Scatter(x=forecast_raw["datetime"], y=forecast_raw[COL_HUMIDITY], mode="lines",
                              line={"color": "#8e44ad", "width": 1.3, "dash": "dash"},
                              name="相対湿度(％)[予報]"), row=2, col=1)

    if probs is not None:
        fig.add_trace(go.Scatter(
            x=forecast_raw["datetime"], y=probs, mode="lines", fill="tozeroy",
            line={"color": "#1a5276", "width": 1.6}, fillcolor="rgba(41,128,185,0.3)",
            name="川霧(1〜3)の予測確率",
        ), row=3, col=1)
        fig.add_trace(go.Scatter(
            x=[forecast_raw["datetime"].min(), forecast_raw["datetime"].max()],
            y=[threshold_pct, threshold_pct], mode="lines",
            line={"color": "#c0392b", "width": 1.2, "dash": "dash"},
            name=f"川霧と判定するしきい値({threshold_pct:.0f}%)",
        ), row=3, col=1)

    if len(hist_phenom):
        fig.add_trace(_phenom_code_bar(hist_phenom["datetime"], hist_codes, "実測"), row=4, col=1)
    fig.add_trace(_binary_forecast_bar(forecast_raw["datetime"], pred_codes), row=4, col=1)

    fig.add_vrect(x0=fc_start, x1=fc_end, fillcolor="gray", opacity=0.08, line_width=0,
                  row="all", col="all")
    fig.add_vline(x=fc_start, line_dash="dot", line_color="dimgray", line_width=1.5,
                  row="all", col="all")

    fig.update_yaxes(title_text="気温・露点温度(℃)", row=1, col=1)
    fig.update_yaxes(title_text="相対湿度(％)", range=[0, 115], row=2, col=1)
    fig.update_yaxes(title_text="川霧の確率(％)", range=[0, 100], row=3, col=1)
    fig.update_yaxes(title_text=col, visible=False, range=[-0.6, 0.6], row=4, col=1)
    fig.update_xaxes(title_text="日時(年/月/日)", row=4, col=1)

    fig.update_layout(
        title={
            "text": f"【{col}】直近{history_days}日間の実測 ＋ 今後{n_days}日間の川霧予測"
                    "<br><span style=\"font-size:0.75em;\">"
                    "(グレーの帯＝予測ゾーン。一番下の帯は実測が現象コード、予報が川霧あり/なし。"
                    "ホバーで名前を表示)</span>",
            "font": {"size": 14},
        },
        # 横並びの凡例は8系列あり、埋め込み先(station_detail.htmlのiframe等)の幅が
        # 狭いと折り返してタイトルと重なるため、幅に依存しない縦並び・右側配置にする。
        legend={"orientation": "v", "x": 1.01, "y": 1, "xanchor": "left", "yanchor": "top",
                "font": {"size": 9}},
        margin={"t": 70, "r": 190, "b": 60, "l": 70},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        height=760,
    )

    out_path = os.path.join(out_dir, f"{col}_予測_{run_date_str}.html")
    fig.write_html(
        out_path, include_plotlyjs=PLOTLY_JS_URL, full_html=True,
        config={"responsive": True, "displaylogo": False},
    )
    return out_path


def plot_all_location_summary(pred_df, models, out_dir, prob_df=None, run_date_str="",
                               station_names=None, file_suffix="", label_suffix=""):
    """全地点サマリー。上段=日ごとの川霧予測地点数、下段=地点×日の川霧確率ヒートマップ。
    station_namesを指定すると、その地点だけに絞ったサマリーを生成する（2026-09に会津
    地方を追加した際、只見川・会津で別々のダッシュボードカードに分けられるようにするため）。
    対象地点にモデルが1つも無い場合は描画せずNoneを返す。

    matplotlib版からの変更点(2026-09-14): 上段が地点数(実数、左軸)と川霧確率の平均(%、
    右軸)のtwinx()だったが、2軸グラフは読み違えやすいため避ける方針に合わせ、地点数を
    「全地点に対する割合(%)」に換算して確率と同じ0〜100%の1軸に統一した。実数の地点数は
    失わず、ホバーテキストに残す。
    """
    df = pred_df.copy()
    df["date"] = df["datetime"].dt.date
    fog_cols = [c for c in models.keys() if c in df.columns]
    if station_names is not None:
        fog_cols = [c for c in fog_cols if c in station_names]
    n_total = len(fog_cols)
    if n_total == 0:
        return None

    dates = sorted(df["date"].unique())
    n_days = len(dates)
    counts, mean_probs = [], []
    prob_dates = prob_df["datetime"].dt.date if prob_df is not None else None
    for d in dates:
        group = df[df["date"] == d]
        counts.append(sum(1 for col in fog_cols if (group[col].astype(int) == 1).any()))
        if prob_df is not None and fog_cols:
            mask = prob_dates == d
            mean_probs.append(float(prob_df.loc[mask, fog_cols].to_numpy().mean() * 100.0) if mask.any()
                              else np.nan)
        else:
            mean_probs.append(np.nan)

    heat = np.full((n_total, n_days), np.nan)
    if prob_df is not None:
        for i, col in enumerate(fog_cols):
            for j, d in enumerate(dates):
                mask = prob_dates == d
                if mask.any():
                    heat[i, j] = float(prob_df.loc[mask, col].max() * 100.0)

    date_labels = [d.strftime("%m/%d") for d in dates]
    count_pct = [100.0 * c / n_total for c in counts]

    bar_ratio, heat_ratio = 3.0, max(2.0, n_total * 0.22)
    total_ratio = bar_ratio + heat_ratio
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[bar_ratio / total_ratio, heat_ratio / total_ratio], vertical_spacing=0.05,
    )

    fig.add_trace(go.Bar(
        x=date_labels, y=count_pct, marker_color="#3498db", name="川霧が予測された地点数",
        customdata=counts, hovertemplate=f"%{{x}}: %{{customdata}}地点(全{n_total}中)<extra></extra>",
    ), row=1, col=1)
    if prob_df is not None and not all(np.isnan(mean_probs)):
        fig.add_trace(go.Scatter(
            x=date_labels, y=mean_probs, mode="lines+markers", line={"color": "#c0392b", "width": 1.4},
            marker={"size": 6}, name="川霧確率の平均(全地点・全時刻)",
        ), row=1, col=1)

    fig.add_trace(go.Heatmap(
        z=heat, x=date_labels, y=fog_cols, colorscale="YlOrRd", zmin=0, zmax=100,
        hovertemplate="地点: %{y}<br>日付: %{x}<br>確率(最大): %{z:.0f}%<extra></extra>",
        colorbar={"title": {"text": "川霧確率<br>の最大(％)"}},
    ), row=2, col=1)

    fig.update_yaxes(title_text=f"川霧予測地点の割合<br>(全{n_total}中、％)", range=[0, 100], row=1, col=1)
    fig.update_yaxes(title_text="地点", autorange="reversed", row=2, col=1)
    fig.update_xaxes(title_text="日付", row=2, col=1)

    fig.update_layout(
        title={"text": f"日ごとの川霧予測サマリー{label_suffix}(今後{n_days}日間)", "font": {"size": 14}, "y": 0.99},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.03, "x": 0, "font": {"size": 10}},
        margin={"t": 130, "r": 30, "b": 60, "l": 90},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        height=max(460, int(90 * bar_ratio + 26 * n_total) + 40),
    )

    out_path = os.path.join(out_dir, f"全地点サマリー{file_suffix}_{run_date_str}.html")
    fig.write_html(
        out_path, include_plotlyjs=PLOTLY_JS_URL, full_html=True,
        config={"responsive": True, "displaylogo": False},
    )
    return out_path


def export_prediction_csv(pred_df, prob_df, models, out_dir, run_date_str=""):
    """予測結果CSV（縦持ち: 日時・地点・川霧の予測(あり/なし)・川霧確率・しきい値）。"""
    long_rows = []
    for col in models.keys():
        if col not in pred_df.columns:
            continue
        threshold = models[col].get("fog_threshold", DEFAULT_FOG_THRESHOLD)
        preds = pred_df[col].astype(int)
        probs = prob_df[col] if prob_df is not None and col in prob_df.columns else pd.Series(np.nan, index=pred_df.index)
        long_rows.append(pd.DataFrame({
            "日時": pred_df["datetime"].dt.strftime("%Y/%m/%d %H:%M"),
            "地点": col,
            "川霧の予測": preds.map(lambda p: BINARY_LABELS[p]),
            "川霧確率(%)": (probs * 100).round(1),
            "しきい値(%)": round(threshold * 100, 1),
        }))
    long_out = pd.concat(long_rows, ignore_index=True) if long_rows else pd.DataFrame()
    out_path = os.path.join(out_dir, f"予測結果_{run_date_str}.csv")
    long_out.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path
