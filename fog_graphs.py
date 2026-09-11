"""地点ごとの予測グラフ・全地点サマリー・CSV出力。

~/Desktop/fog-prediction/weather_visualizer.py のグラフ設計を踏襲。Colab依存
（日本語フォント自動導入・ノートブック内表示）は持たない。日本語フォント自体は
サーバー側で事前導入済み（`sudo apt-get install fonts-noto-cjk`）であることを
前提にするが、matplotlibは起動時に構築した独自のフォントキャッシュしか見ないため、
インストール後にフォントキャッシュを更新するかmatplotlib側にも明示的に教える
必要がある。ここでは後者（フォントファイルを直接addfontする）を行う。
"""
import logging
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from matplotlib import font_manager
from matplotlib.patches import Patch

log = logging.getLogger("fog_graphs")

# Ubuntu/Debianでfonts-noto-cjkをapt導入した場合の標準パス。無ければ日本語が
# 文字化け（豆腐）した状態でグラフが生成されるが、処理自体は止めない。
_NOTO_CJK_JP_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
if os.path.exists(_NOTO_CJK_JP_PATH):
    font_manager.fontManager.addfont(_NOTO_CJK_JP_PATH)
    matplotlib.rcParams["font.family"] = "Noto Sans CJK JP"
else:
    log.warning(
        "Japanese font not found at %s; graphs will show tofu boxes for Japanese text. "
        "Run: sudo apt-get install -y fonts-noto-cjk", _NOTO_CJK_JP_PATH,
    )

from fog_features import (
    COL_DEWPOINT,
    COL_HUMIDITY,
    COL_TEMP,
    PHENOM_COLORS,
    PHENOM_LABELS,
    SLASH_COLOR,
    SUMMARY_FOG_CODES,
    encode_phenomena_cell,
)
from fog_model import DEFAULT_FOG_THRESHOLD

SAVE_DPI = 150
FORECAST_MIN_FIG_WIDTH = 16
FORECAST_MAX_FIG_WIDTH = 34
FORECAST_INCH_PER_DAY = 1.2
AXIS_BREAK_GAP_DAYS = 2.0


def _phenom_name(code):
    return "現象なし" if code == 0 else PHENOM_LABELS.get(code, f"コード{code}")


def _phenom_legend_handles():
    handles = [Patch(facecolor=SLASH_COLOR, label="「/」現象なし")]
    for code in range(1, 11):
        handles.append(Patch(facecolor=PHENOM_COLORS[code], label=f"{code}: {PHENOM_LABELS[code]}"))
    return handles


def _apply_date_axis(ax, span_days):
    day_interval = 1 if span_days <= 30 else max(1, int(span_days // 30))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=day_interval))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y/%m/%d"))
    ax.grid(True, axis="x", which="major", color="gray", alpha=0.25, linewidth=0.6, zorder=0)
    ax.tick_params(axis="x", labelsize=8, labelrotation=45)


def _draw_axis_break_marks(ax_left, ax_right):
    kw = dict(marker=[(-1, -0.6), (1, 0.6)], markersize=8, linestyle="none",
              color="dimgray", mec="dimgray", mew=1.2, clip_on=False)
    ax_left.plot([1, 1], [0, 1], transform=ax_left.transAxes, **kw)
    ax_right.plot([0, 0], [0, 1], transform=ax_right.transAxes, **kw)


def get_last_observed_datetime(phenom_df, col):
    codes = phenom_df[col].map(encode_phenomena_cell)
    valid_dates = phenom_df.loc[codes.notna(), "datetime"]
    if valid_dates.empty:
        return phenom_df["datetime"].max()
    return valid_dates.max()


def plot_single_location_forecast(main_df, phenom_df, col, forecast_raw, pred_series, out_dir,
                                  history_days=5, prob_series=None,
                                  fog_threshold=DEFAULT_FOG_THRESHOLD, run_date_str=""):
    """1地点の「実測(直近history_days日) + 今後の予測」グラフ。
    上段=気温/露点/湿度、中段=霧の予測確率、下段=現象コード(実測+予測)。
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
    hist_start = hist_main["datetime"].min() if len(hist_main) else hist_cutoff
    hist_end = hist_main["datetime"].max() if len(hist_main) else last_observed

    gap_days = (fc_start - hist_end).total_seconds() / 86400.0
    if len(hist_main) and gap_days > AXIS_BREAK_GAP_DAYS:
        segments = [(hist_start, hist_end), (fc_start, fc_end)]
    elif len(hist_main):
        segments = [(min(hist_start, fc_start), max(hist_end, fc_end))]
    else:
        segments = [(fc_start, fc_end)]

    seg_days = [max(0.5, (e - s).total_seconds() / 86400.0) for s, e in segments]
    fig_width = float(np.clip(sum(seg_days) * FORECAST_INCH_PER_DAY,
                              FORECAST_MIN_FIG_WIDTH, FORECAST_MAX_FIG_WIDTH))

    fig, axes = plt.subplots(
        3, len(segments), figsize=(fig_width, 8.2), squeeze=False, sharey="row",
        gridspec_kw={"height_ratios": [5, 1.6, 1.2], "width_ratios": seg_days,
                     "hspace": 0.12, "wspace": 0.05},
    )

    probs = None if prob_series is None else np.asarray(prob_series, dtype="float64") * 100.0
    pred_codes = np.asarray(pred_series, dtype="int64")
    x_hist = mdates.date2num(hist_phenom["datetime"].to_numpy()) if len(hist_phenom) else np.array([])
    x_fore = mdates.date2num(forecast_raw["datetime"].to_numpy())
    n_days = max(1, round(len(forecast_raw) / 24))

    humid_axes = []
    for j, (seg_start, seg_end) in enumerate(segments):
        ax1, axp, ax2 = axes[0][j], axes[1][j], axes[2][j]
        is_forecast_panel = seg_end >= fc_start
        is_last = (j == len(segments) - 1)

        if is_forecast_panel:
            for ax in (ax1, axp, ax2):
                ax.axvspan(fc_start, fc_end, color="gray", alpha=0.08, zorder=0)
                ax.axvline(fc_start, color="dimgray", linewidth=1.5, linestyle=":")

        ax1.plot(hist_main["datetime"], hist_main[COL_TEMP], color="#e74c3c", linewidth=1.2,
                 label="気温(℃) [実測]")
        ax1.plot(hist_main["datetime"], hist_main[COL_DEWPOINT], color="#16a085", linewidth=1.2,
                 label="露点温度(℃) [実測]")
        ax1.plot(forecast_raw["datetime"], forecast_raw[COL_TEMP], color="#e74c3c", linewidth=1.5,
                 linestyle="--", label="気温(℃) [予報]")
        ax1.plot(forecast_raw["datetime"], forecast_raw[COL_DEWPOINT], color="#16a085", linewidth=1.5,
                 linestyle="--", label="露点温度(℃) [予報]")
        ax1.grid(True, axis="y", alpha=0.25)

        ax1r = ax1.twinx()
        humid_axes.append(ax1r)
        ax1r.plot(hist_main["datetime"], hist_main[COL_HUMIDITY], color="#8e44ad", linewidth=0.9,
                  alpha=0.6, label="相対湿度(％) [実測]")
        ax1r.plot(forecast_raw["datetime"], forecast_raw[COL_HUMIDITY], color="#8e44ad", linewidth=1.2,
                  alpha=0.9, linestyle="--", label="相対湿度(％) [予報]")
        ax1r.set_ylim(0, 115)

        threshold_pct = fog_threshold * 100.0
        if probs is not None:
            axp.fill_between(forecast_raw["datetime"], 0, probs, color="#2980b9", alpha=0.30, zorder=2)
            axp.plot(forecast_raw["datetime"], probs, color="#1a5276", linewidth=1.4, zorder=3,
                     label="霧(1〜6)の予測確率")
            axp.axhline(threshold_pct, color="#c0392b", linewidth=0.9, linestyle="--", alpha=0.8, zorder=1,
                       label=f"霧警戒ライン（{threshold_pct:.0f}%）")
        axp.set_ylim(0, 100)
        axp.set_yticks([0, 50, 100])
        axp.grid(True, axis="y", alpha=0.25)

        if len(x_hist):
            slash_mask = hist_codes == 0
            if slash_mask.any():
                ax2.vlines(x_hist[slash_mask], -0.4, 0.4, color=SLASH_COLOR, linewidth=2.2, zorder=2)
            for code in range(1, 11):
                code_mask = hist_codes == code
                if code_mask.any():
                    ax2.vlines(x_hist[code_mask], -0.45, 0.45, color=PHENOM_COLORS[code],
                               linewidth=3.0, zorder=3)
        for code in range(0, 11):
            mask = pred_codes == code
            if mask.any():
                color = SLASH_COLOR if code == 0 else PHENOM_COLORS[code]
                ax2.vlines(x_fore[mask], -0.45, 0.45, color=color, linewidth=3.0, zorder=3)
        ax2.set_yticks([])
        ax2.set_ylim(-0.6, 0.6)
        ax2.axhline(0, color="#ececec", linewidth=0.5, zorder=1)

        span = (seg_end - seg_start).total_seconds() / 86400.0
        for ax in (ax1, axp, ax2):
            ax.set_xlim(seg_start, seg_end)
        _apply_date_axis(ax1, span)
        _apply_date_axis(axp, span)
        _apply_date_axis(ax2, span)

        if j == 0:
            ax1.set_ylabel("気温・露点温度（℃）", fontsize=10)
            axp.set_ylabel("霧の確率\n（％）", fontsize=9)
            ax2.set_ylabel(col, fontsize=9)
            ax2.set_xlabel("実測（年/月/日）" if len(segments) > 1 else "日時（年/月/日）")
        else:
            ax2.set_xlabel("予報（年/月/日）")
        if is_last:
            ax1r.set_ylabel("相対湿度（％）", fontsize=10)
        else:
            ax1r.set_yticklabels([])

        if is_forecast_panel:
            note = "ここから先は予測（背景グレー・点線）" if len(segments) == 1 else "予測ゾーン（背景グレー・点線）"
            ax1.set_title(note, loc="left", fontsize=9, color="dimgray")
        elif len(segments) > 1:
            ax1.set_title("実測", loc="left", fontsize=9, color="dimgray")

    h1, lbl1 = axes[0][0].get_legend_handles_labels()
    h2, lbl2 = humid_axes[0].get_legend_handles_labels()
    axes[0][0].legend(h1 + h2, lbl1 + lbl2, bbox_to_anchor=(0, 1.02, 1, 0.12), loc="lower left",
                      ncol=3, mode="expand", borderaxespad=0., fontsize=7.5)
    if probs is not None:
        axes[1][-1].legend(loc="upper left", fontsize=7.5)
    axes[2][-1].legend(handles=_phenom_legend_handles(), bbox_to_anchor=(1.08, 2.6), loc="upper left",
                       fontsize=7, borderaxespad=0., title="現象コード", title_fontsize=8)

    if len(segments) > 1:
        for row in range(3):
            left, right = axes[row][0], axes[row][1]
            left.spines["right"].set_visible(False)
            right.spines["left"].set_visible(False)
            right.tick_params(axis="y", left=False)
            _draw_axis_break_marks(left, right)
        gap_note = f"（実測の最終記録から予報開始まで{gap_days:.0f}日の空白があるため、横軸を分割しています）"
    else:
        gap_note = ""

    fig.suptitle(
        f"【{col}】直近{history_days}日間の実測 ＋ 今後{n_days}日間の霧予測{gap_note}",
        fontsize=12, y=0.98,
    )
    fig.autofmt_xdate()

    out_path = os.path.join(out_dir, f"{col}_予測_{run_date_str}.png")
    fig.savefig(out_path, dpi=SAVE_DPI, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_all_location_summary(pred_df, models, out_dir, prob_df=None, run_date_str="",
                               station_names=None, file_suffix="", label_suffix=""):
    """全地点サマリー。上段=日ごとの霧予測地点数、下段=地点×日の霧確率ヒートマップ。
    station_namesを指定すると、その地点だけに絞ったサマリーを生成する（2026-09に会津
    地方を追加した際、只見川・会津で別々のダッシュボードカードに分けられるようにするため）。
    対象地点にモデルが1つも無い場合は描画せずNoneを返す。
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
        counts.append(sum(1 for col in fog_cols if group[col].astype(int).isin(SUMMARY_FOG_CODES).any()))
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

    x = np.arange(n_days)
    fig_w = max(10, n_days * 0.8)
    bar_ratio, heat_ratio = 3.0, max(2.0, n_total * 0.22)
    fig, (ax_bar, ax_heat) = plt.subplots(
        2, 1, figsize=(fig_w, bar_ratio + heat_ratio), sharex=True,
        gridspec_kw={"height_ratios": [bar_ratio, heat_ratio], "hspace": 0.08},
    )

    ax_bar.bar(x, counts, color="#3498db", width=0.6, label="霧が予測された地点数")
    for bx, c in zip(x, counts):
        ax_bar.text(bx, c + 0.3, str(c), ha="center", va="bottom", fontsize=9)
    ax_bar.set_ylim(0, n_total + 1)
    ax_bar.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax_bar.set_ylabel(f"霧が予測された\n地点数（全{n_total}中）", fontsize=9)
    ax_bar.set_title(f"日ごとの霧予測サマリー{label_suffix}（今後{n_days}日間）", fontsize=13)
    ax_bar.grid(True, axis="y", alpha=0.3)

    handles, labels = ax_bar.get_legend_handles_labels()
    if prob_df is not None and not all(np.isnan(mean_probs)):
        axr = ax_bar.twinx()
        line, = axr.plot(x, mean_probs, color="#c0392b", marker="o", markersize=4, linewidth=1.4,
                         label="霧確率の平均（全地点・全時刻）")
        axr.set_ylim(0, 100)
        axr.set_ylabel("霧確率\nの平均(%)", fontsize=9)
        handles.append(line)
        labels.append(line.get_label())
    ax_bar.legend(handles, labels, loc="upper left", fontsize=8)
    ax_bar.grid(True, axis="x", alpha=0.25, linewidth=0.6)

    im = ax_heat.imshow(heat, aspect="auto", cmap="YlOrRd", vmin=0, vmax=100, interpolation="nearest")
    ax_heat.set_yticks(range(n_total))
    ax_heat.set_yticklabels(fog_cols, fontsize=8)
    ax_heat.set_xticks(x)
    ax_heat.set_xticklabels([d.strftime("%m/%d") for d in dates], fontsize=8, rotation=45, ha="right")
    ax_heat.set_xlabel("日付")
    ax_heat.set_ylabel("地点", fontsize=9)
    for i in range(n_total):
        for j in range(n_days):
            v = heat[i, j]
            if not np.isnan(v) and v >= 50:
                ax_heat.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=6,
                             color="white" if v >= 70 else "black")
    cbar = fig.colorbar(im, ax=[ax_bar, ax_heat], fraction=0.02, pad=0.01,
                        shrink=heat_ratio / (bar_ratio + heat_ratio), anchor=(0.0, 0.0))
    cbar.set_label("霧確率の最大（％）", fontsize=8)

    out_path = os.path.join(out_dir, f"全地点サマリー{file_suffix}_{run_date_str}.png")
    fig.savefig(out_path, dpi=SAVE_DPI, bbox_inches="tight")
    plt.close(fig)
    return out_path


def export_prediction_csv(pred_df, prob_df, models, out_dir, run_date_str=""):
    """予測結果CSV（縦持ち: 日時・地点・予測コード・現象名・霧確率・判定）。"""
    long_rows = []
    for col in models.keys():
        if col not in pred_df.columns:
            continue
        threshold = models[col].get("fog_threshold", DEFAULT_FOG_THRESHOLD)
        codes = pred_df[col].astype(int)
        probs = prob_df[col] if prob_df is not None and col in prob_df.columns else pd.Series(np.nan, index=pred_df.index)
        long_rows.append(pd.DataFrame({
            "日時": pred_df["datetime"].dt.strftime("%Y/%m/%d %H:%M"),
            "地点": col,
            "予測コード": codes,
            "現象名": codes.map(_phenom_name),
            "霧確率(%)": (probs * 100).round(1),
            "判定": np.where(probs >= threshold, "霧", "-"),
        }))
    long_out = pd.concat(long_rows, ignore_index=True) if long_rows else pd.DataFrame()
    out_path = os.path.join(out_dir, f"予測結果_{run_date_str}.csv")
    long_out.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path
