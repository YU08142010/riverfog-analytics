"""Plotlyの図(dictのfigure)を、matplotlibで画像(PNG)に描く(2026-10-07)。

サイトのグラフは2026-09-14にPlotly(ブラウザ上で拡大・移動できるグラフ)へ移行したが、触ると拡大・移動して
しまい使いづらいという声があり、画像のグラフ(matplotlib)に戻した。約60種類あるグラフの作り(データ・色・
軸・注記)はPlotlyのfigureとして各所に定義済みなので、それを作り直さずにこのモジュールが共通に描き直す。
- 使える要素: scatter(線・点・文字・面の塗り)、bar(縦・横、group/stack/overlay、文字)、heatmap(セルの文字・
  カラーバー)、box。軸のdomainによる複数段、date/category/linear/log軸、range・reversed・tickvals・
  tickformat、shapes(line/rect、data・paper・domain座標)、annotations、凡例(上に横並び)。
- 見た目はchart_styleの方針(細い線・控えめなグリッド・上の凡例)にそろえ、背景は透明(すりガラスのカードの上に
  置くため)。テーマ(light/dark)ごとに文字色・系列色(chart_style.SERIES_LIGHT→SERIES_DARK)を変える。
- 決定木の節の四角(fog_tree_viz)は角を丸めた半透明の面、予報グラフの一番下の帯の夜間・川霧なしの灰色は
  半透明にする(以前ブラウザ側で行っていた調整と同じ)。
pyplotは使わず(スレッド安全でないため)、Figureを直接作る。同じ図・同じ条件の画像はメモリに保持して使い回す。
"""
import base64
import hashlib
import html as _html
import io
import json
import logging
import math
import re
import threading
import warnings
from collections import OrderedDict

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import LinearSegmentedColormap, to_rgba
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch, Patch, Rectangle
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import matplotlib.transforms as mtransforms

import chart_style as cs

log = logging.getLogger("mpl_render")

_NOTO = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
_NOTO_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
for _p in (_NOTO, _NOTO_BOLD):
    try:
        font_manager.fontManager.addfont(_p)
    except Exception:
        pass
FONT = ["Noto Sans CJK JP", "DejaVu Sans"]
matplotlib.rcParams["font.family"] = FONT
matplotlib.rcParams["axes.unicode_minus"] = False
PX = 0.72  # Plotlyのpx → matplotlibのpt(dpi=100で1px=0.72pt)

THEMES = {
    "light": {"fg": "#1d1d1f", "muted": "#5c6370", "grid": (20 / 255, 30 / 255, 50 / 255, 0.10),
              "zero": (20 / 255, 30 / 255, 50 / 255, 0.28), "surface": "#ffffff"},
    "dark": {"fg": "#e6e8ee", "muted": "#a1a6b2", "grid": (1, 1, 1, 0.09),
             "zero": (1, 1, 1, 0.25), "surface": "#1e2027"},
}
_DARK_INKS = {"#2a3f5f", "#1d1d1f", "#1c2733", "#444", "#444444", "#000", "#000000", "black", "#333", "#333333",
              "#5c6370", "#52514e"}
_SERIES = {c.lower(): i for i, c in enumerate(cs.SERIES_LIGHT)}
# 決定木の節(fog_tree_viz)と、予報グラフの帯の灰色(fog_graphs)
_TREE_KIND = {"#a9c6de": "split", "#e6c789": "leaf", "#9aa5b1": "more"}
_TREE_COLORS = {
    "light": {"split": ((42, 120, 214, 0.14), (42, 120, 214, 0.45)), "leaf": ((235, 104, 52, 0.14), (235, 104, 52, 0.5)),
              "more": ((110, 120, 140, 0.10), (110, 120, 140, 0.35))},
    "dark": {"split": ((57, 135, 229, 0.22), (125, 189, 255, 0.5)), "leaf": ((217, 89, 38, 0.22), (255, 160, 120, 0.5)),
             "more": ((255, 255, 255, 0.06), (255, 255, 255, 0.25))},
}
_SOFT_BAND = {"light": {"#566573": (86, 101, 115, 0.30), "#d5d8dc": (120, 130, 150, 0.22)},
              "dark": {"#566573": (255, 255, 255, 0.08), "#d5d8dc": (255, 255, 255, 0.20)}}
_DASH = {"dash": (0, (4.5, 3)), "dot": (0, (1.2, 2.2)), "dashdot": (0, (4.5, 2, 1.2, 2)), "longdash": (0, (8, 3)),
         "longdashdot": (0, (8, 2, 1.2, 2)), "solid": "-"}
_SYMBOL = {"circle": "o", "square": "s", "diamond": "D", "triangle-up": "^", "triangle-down": "v", "triangle-left": "<",
           "triangle-right": ">", "x": "x", "cross": "P", "star": "*", "hexagon": "h", "pentagon": "p", "line-ns": "|"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


# --- 色 ---------------------------------------------------------------------------------------
def _rgba255(t):
    r, g, b, a = t
    return (r / 255, g / 255, b / 255, a)


def color(c, theme, default=None, kind="line"):
    """Plotlyの色指定 → matplotlibのRGBA。darkでは系列色を同じ位置のダーク用の色へ、濃い文字色を明るい色へ。"""
    if c is None or c == "":
        return default
    if isinstance(c, (list, tuple)):
        return default
    s = str(c).strip()
    low = s.lower().replace(" ", "")
    if theme == "dark":
        if low in _SERIES:
            s = cs.SERIES_DARK[_SERIES[low]]
            low = s.lower()
        elif kind == "text" and low in _DARK_INKS:
            return to_rgba(THEMES["dark"]["fg"])
    if low in _SOFT_BAND[theme] and kind == "band":
        return _rgba255(_SOFT_BAND[theme][low])
    m = re.match(r"^rgba?\(([^)]*)\)$", low)
    if m:
        parts = [float(p) for p in m.group(1).split(",")]
        a = parts[3] if len(parts) > 3 else 1.0
        return (parts[0] / 255, parts[1] / 255, parts[2] / 255, a)
    try:
        return to_rgba(s)
    except ValueError:
        return default


def _with_alpha(c, a):
    if c is None:
        return None
    return (c[0], c[1], c[2], a)


def _strip(text):
    """Plotlyの文字(HTML少々)を普通の文字にする。<br>は改行。"""
    if text is None:
        return ""
    t = str(text)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    return _html.unescape(t)


def _title_text(t):
    if isinstance(t, dict):
        return _strip(t.get("text"))
    return _strip(t)


# --- 軸の値 --------------------------------------------------------------------------------------
class AxisInfo:
    def __init__(self, name, spec):
        self.name = name            # "xaxis", "yaxis2" ...
        self.spec = spec or {}
        self.type = self.spec.get("type")
        self.categories = []
        arr = self.spec.get("categoryarray")
        if arr:
            self.categories = [str(v) for v in arr]

    def infer(self, values):
        if self.type in ("date", "category", "linear", "log"):
            return
        for v in values:
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            if isinstance(v, str):
                self.type = "date" if _DATE_RE.match(v) else "category"
            else:
                self.type = "linear"
            return

    def add_categories(self, values):
        for v in values:
            if v is None:
                continue
            s = str(v)
            if s not in self.categories:
                self.categories.append(s)

    def conv(self, values):
        """値の列 → 数値の配列(dateはmatplotlibの日付数値、categoryは位置)。"""
        if values is None:
            return np.array([])
        vals = list(values) if not isinstance(values, (str, bytes)) else [values]
        if self.type == "date":
            ts = pd.to_datetime(pd.Series(vals, dtype="object"), errors="coerce")
            out = np.full(len(vals), np.nan)
            ok = ts.notna().to_numpy()
            if ok.any():
                out[ok] = mdates.date2num(ts[ok].dt.to_pydatetime())
            return out
        if self.type == "category":
            idx = {c: i for i, c in enumerate(self.categories)}
            return np.array([idx.get(str(v), np.nan) if v is not None else np.nan for v in vals], dtype=float)
        return np.array([np.nan if v is None else (float(v) if not isinstance(v, str) else _num(v)) for v in vals], dtype=float)

    def conv1(self, v):
        r = self.conv([v])
        return float(r[0]) if len(r) else np.nan


def _num(s):
    try:
        return float(s)
    except ValueError:
        return np.nan


def _axis_key(ref, letter):
    """"x" → "xaxis", "x2" → "xaxis2"。"""
    ref = ref or letter
    return letter + "axis" + ref[1:]


def decode_typed_arrays(obj):
    """Plotly 6以降のJSONは数値の配列を {"dtype": "f8", "bdata": "<base64>", "shape": "3, 4"} の形で書くので、
    普通のリストに戻す(入れ子もすべて)。"""
    if isinstance(obj, dict):
        if "bdata" in obj and "dtype" in obj:
            arr = np.frombuffer(base64.b64decode(obj["bdata"]), dtype=np.dtype(obj["dtype"]))
            shape = obj.get("shape")
            if shape:
                dims = [int(s) for s in str(shape).replace(" ", "").split(",") if s]
                arr = arr.reshape(dims)
            return arr.tolist()
        return {k: decode_typed_arrays(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decode_typed_arrays(v) for v in obj]
    return obj


# --- 本体 --------------------------------------------------------------------------------------------
def render_png(fig_dict, theme="light", width=900, scale=1.0, notitle=False):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return _render_png(decode_typed_arrays(fig_dict), theme, width, scale, notitle)


def _render_png(fig_dict, theme, width, scale, notitle):
    theme = "dark" if theme == "dark" else "light"
    pal = THEMES[theme]
    layout = dict(fig_dict.get("layout") or {})
    traces = [t for t in (fig_dict.get("data") or []) if t.get("visible", True) is not False]
    W = int(max(240, min(2400, width)))
    H = int(layout.get("height") or 420)
    if W < 520:
        H = int(H * 0.92)

    # 軸の情報を集める
    axes_spec = {k: v for k, v in layout.items() if re.match(r"^[xy]axis\d*$", k)}
    axinfo = {k: AxisInfo(k, v) for k, v in axes_spec.items()}

    def info(key):
        if key not in axinfo:
            axinfo[key] = AxisInfo(key, {})
        return axinfo[key]

    pairs = []
    for t in traces:
        p = (_axis_key(t.get("xaxis"), "x"), _axis_key(t.get("yaxis"), "y"))
        if p not in pairs:
            pairs.append(p)
    for k, v in axes_spec.items():
        if k.startswith("xaxis") and isinstance(v, dict) and v.get("anchor", "").startswith("y"):
            p = (k, _axis_key(v["anchor"], "y"))
            if p not in pairs and not any(q[0] == k for q in pairs):
                pairs.append(p)
    if not pairs:
        pairs = [("xaxis", "yaxis")]

    # 値の型の推定とカテゴリーの並び
    for t in traces:
        xk, yk = _axis_key(t.get("xaxis"), "x"), _axis_key(t.get("yaxis"), "y")
        typ = t.get("type", "scatter")
        xs, ys = t.get("x"), t.get("y")
        if typ == "box":
            if xs is None:
                xs = [t.get("name") or ""] * max(1, len(ys or []))
        if typ == "heatmap":
            info(xk).infer(xs or [0]); info(yk).infer(ys or [0])
        else:
            if xs is not None:
                info(xk).infer(xs)
            if ys is not None:
                info(yk).infer(ys)
        for k, vals in ((xk, xs), (yk, ys)):
            if vals is not None and info(k).type == "category":
                info(k).add_categories(vals)

    # 余白・凡例の行数
    margin = layout.get("margin") or {}
    ml, mr = float(margin.get("l", 70)), float(margin.get("r", 30))
    mt, mb = float(margin.get("t", 60)), float(margin.get("b", 60))
    mr = min(mr, 40)  # 右の縦の凡例は上に移すので、右の余白は詰める
    if any(t.get("type") == "heatmap" and t.get("showscale", True) is not False for t in traces):
        mr = max(mr, 78)  # カラーバーの目盛りの数字が切れないように
    title = "" if notitle else _title_text(layout.get("title"))
    legend_items = []
    if layout.get("showlegend", True) is not False:
        seen = set()
        for t in traces:
            name = _strip(t.get("name"))
            if not name or t.get("showlegend") is False or name in seen:
                continue
            if t.get("type") in ("heatmap",):
                continue
            legend_items.append(t)
            seen.add(name)
        if len(legend_items) < 2 and layout.get("showlegend") is not True:
            legend_items = []
    plot_w = W - ml - mr
    ncol = max(1, min(len(legend_items), int(plot_w // 150))) if legend_items else 1
    legend_rows = math.ceil(len(legend_items) / ncol) if legend_items else 0
    need_t = 10 + (22 if title else 0) + 19 * legend_rows + (6 if legend_rows else 0)
    has_paper_annot_top = any((a.get("yref") == "paper" and float(a.get("y", 0) or 0) >= 1.0)
                              for a in (layout.get("annotations") or []))
    mt = max(need_t + (20 if has_paper_annot_top else 0), 14 if (notitle and not legend_rows) else min(mt, 46))
    # 棒の上に数字を出すグラフは、軸の範囲が固定(0〜100等)だと数字が描画範囲の上にはみ出すので、上の余白を確保する
    if any(t.get("type") == "bar" and t.get("text") is not None and t.get("orientation") != "h"
           and (t.get("textposition") or "auto") in ("outside", "auto") for t in traces):
        mt = max(mt, (need_t if legend_rows or title else 0) + 30)
    mb = max(36, min(mb, 90))
    ml = max(44, min(ml, 140))

    fig = Figure(figsize=(W / 100, H / 100), dpi=100)
    FigureCanvasAgg(fig)
    fig.patch.set_alpha(0)
    left, right = ml / W, 1 - mr / W
    bottom, top = mb / H, 1 - mt / H
    pw, ph = right - left, top - bottom

    def dom(spec, default=(0.0, 1.0)):
        d = (spec or {}).get("domain") or default
        return float(d[0]), float(d[1])

    mpl_axes = {}
    for xk, yk in pairs:
        x0, x1 = dom(axes_spec.get(xk))
        y0, y1 = dom(axes_spec.get(yk))
        ax = fig.add_axes([left + x0 * pw, bottom + y0 * ph, max(0.01, (x1 - x0) * pw), max(0.01, (y1 - y0) * ph)])
        ax.patch.set_alpha(0)
        mpl_axes[(xk, yk)] = ax

    ctx = {"theme": theme, "pal": pal, "info": info, "fig": fig, "axes": mpl_axes, "W": W, "H": H,
           "left": left, "bottom": bottom, "pw": pw, "ph": ph, "layout": layout}

    # トレースを描く
    bars_by_axes = {}
    for t in traces:
        xk, yk = _axis_key(t.get("xaxis"), "x"), _axis_key(t.get("yaxis"), "y")
        if t.get("type") == "bar":
            bars_by_axes.setdefault((xk, yk), []).append(t)
    legend_handles = {}
    prev_y = {}
    for t in traces:
        xk, yk = _axis_key(t.get("xaxis"), "x"), _axis_key(t.get("yaxis"), "y")
        ax = mpl_axes.get((xk, yk))
        if ax is None:
            continue
        typ = t.get("type", "scatter")
        try:
            if typ == "scatter" or typ == "scattergl":
                h = _scatter(ax, t, info(xk), info(yk), ctx, prev_y.get((xk, yk)))
                ys = info(yk).conv(t.get("y") or [])
                prev_y[(xk, yk)] = (info(xk).conv(t.get("x") or []), ys)
            elif typ == "bar":
                h = _bar(ax, t, info(xk), info(yk), ctx, bars_by_axes[(xk, yk)])
            elif typ == "heatmap":
                h = _heatmap(ax, t, info(xk), info(yk), ctx)
            elif typ == "box":
                h = _box(ax, t, info(xk), info(yk), ctx)
            else:
                h = None
        except Exception as e:
            log.warning("mpl_render: failed to draw a %s trace: %s", typ, e)
            h = None
        name = _strip(t.get("name"))
        if h is not None and name and name not in legend_handles:
            legend_handles[name] = h

    # 軸の見た目
    for (xk, yk), ax in mpl_axes.items():
        _style_axis(ax, info(xk), info(yk), ctx)
    # 共有のx軸(matches): 範囲をそろえ、一番下の段以外は目盛りの文字を消す
    groups = {}
    for (xk, yk), ax in mpl_axes.items():
        root = (axes_spec.get(xk) or {}).get("matches")
        key = _axis_key(root, "x") if root else xk
        groups.setdefault(key, []).append(ax)
    for key, axs in groups.items():
        if len(axs) < 2:
            continue
        lims = [a.get_xlim() for a in axs]
        lo, hi = min(l[0] for l in lims), max(l[1] for l in lims)
        lowest = min(axs, key=lambda a: a.get_position().y0)
        for a in axs:
            a.set_xlim(lo, hi)
            if a is not lowest:
                a.tick_params(axis="x", labelbottom=False)
                a.set_xlabel("")

    # 図形・注記
    for sh in layout.get("shapes") or []:
        try:
            _shape(sh, ctx)
        except Exception as e:
            log.debug("shape skipped: %s", e)
    for an in layout.get("annotations") or []:
        try:
            _annotation(an, ctx)
        except Exception as e:
            log.debug("annotation skipped: %s", e)

    # タイトル・凡例(上に横並び)
    y_cursor = 1 - 8 / H
    if title:
        fig.text(left, y_cursor, title, ha="left", va="top", fontsize=14 * PX * 1.05, color=pal["fg"],
                 fontweight="bold", family=FONT)
        y_cursor -= 24 / H
    if legend_items:
        handles, labels = [], []
        for t in legend_items:
            name = _strip(t.get("name"))
            h = legend_handles.get(name) or _proxy(t, ctx)
            if h is None:
                continue
            handles.append(h)
            labels.append(name)
        if handles:
            fig.legend(handles, labels, loc="upper left", bbox_to_anchor=(left - 0.005, y_cursor), ncol=ncol,
                       frameon=False, fontsize=11 * PX, labelcolor=pal["fg"], handlelength=1.8, columnspacing=1.4,
                       handletextpad=0.5, borderaxespad=0, prop={"family": FONT, "size": 11 * PX})

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100 * scale, transparent=True)
    return buf.getvalue()


# --- トレース -------------------------------------------------------------------------------------
def _scatter(ax, t, xi, yi, ctx, prev):
    theme = ctx["theme"]
    xs = xi.conv(t.get("x") if t.get("x") is not None else list(range(len(t.get("y") or []))))
    ys = yi.conv(t.get("y") or [])
    n = min(len(xs), len(ys))
    xs, ys = xs[:n], ys[:n]
    mode = t.get("mode") or ("lines+markers" if n < 20 else "lines")
    line = t.get("line") or {}
    marker = t.get("marker") or {}
    lc = color(line.get("color") or marker.get("color") if not isinstance(marker.get("color"), list) else line.get("color"),
               theme, default=color(cs.SERIES_LIGHT[0], theme))
    lw = float(line.get("width", 2)) * PX * 1.1
    dash = _DASH.get(line.get("dash") or "solid", "-")
    handle = None
    if "lines" in mode and n:
        if t.get("connectgaps"):
            ok = ~(np.isnan(xs) | np.isnan(ys))
            handle, = ax.plot(xs[ok], ys[ok], color=lc, lw=lw, linestyle=dash, solid_capstyle="round")
        else:
            handle, = ax.plot(xs, ys, color=lc, lw=lw, linestyle=dash, solid_capstyle="round")
        if line.get("shape") == "hv":
            handle.set_drawstyle("steps-post")
    fill = t.get("fill")
    if fill in ("tozeroy", "tonexty") and n:
        fc = color(t.get("fillcolor"), theme) or _with_alpha(lc, 0.12)
        if fill == "tonexty" and prev is not None and len(prev[1]) == n:
            ax.fill_between(xs, prev[1], ys, color=fc, linewidth=0)
        else:
            ax.fill_between(xs, 0, ys, color=fc, linewidth=0)
    if "markers" in mode and n:
        mcol = marker.get("color")
        size = marker.get("size", 6)
        sizes = np.array(size, dtype=float) if isinstance(size, list) else float(size)
        sym = str(marker.get("symbol") or "circle")
        open_ = sym.endswith("-open")
        mk = _SYMBOL.get(sym.replace("-open", ""), "o")
        mline = marker.get("line") or {}
        if isinstance(mcol, list):
            fcs = [color(c, theme, default=lc) for c in mcol]
        else:
            fc1 = color(mcol, theme, default=lc)
            if fc1 is not None and fc1[3] == 0:
                return handle  # 透明な点(決定木のホバー用の印など)は描かない
            fcs = [fc1] * n
        ring = color(mline.get("color"), theme)
        if ring is not None and mline.get("color") in ("#ffffff", "white", "#fff", cs.SURFACE_LIGHT):
            ring = to_rgba(ctx["pal"]["surface"])
        s = (sizes * 0.85) ** 2 if isinstance(sizes, np.ndarray) else (sizes * 0.85) ** 2
        sc = ax.scatter(xs, ys, s=s, marker=mk, c=None if open_ else fcs[:n], facecolors="none" if open_ else None,
                        edgecolors=ring if ring is not None and not open_ else (fcs[:n] if open_ else "none"),
                        linewidths=float(mline.get("width", 1)) * PX if ring is not None or open_ else 0, zorder=3)
        handle = handle or sc
    if "text" in mode and t.get("text") is not None and n:
        texts = t["text"] if isinstance(t["text"], list) else [t["text"]] * n
        tf = t.get("textfont") or {}
        tc = color(tf.get("color"), theme, default=to_rgba(ctx["pal"]["fg"]), kind="text")
        pos = str(t.get("textposition") or "middle center")
        va = "bottom" if "top" in pos else ("top" if "bottom" in pos else "center")
        ha = "left" if "right" in pos else ("right" if "left" in pos else "center")
        for x, y, s_ in zip(xs, ys, texts):
            if np.isnan(x) or np.isnan(y) or s_ in (None, ""):
                continue
            ax.annotate(_strip(s_), (x, y), xytext=(0, 4 if va == "bottom" else (-4 if va == "top" else 0)),
                        textcoords="offset points", ha=ha, va=va, fontsize=float(tf.get("size", 11)) * PX, color=tc,
                        family=FONT)
    return handle


def _bar(ax, t, xi, yi, ctx, group):
    theme = ctx["theme"]
    layout = ctx["layout"]
    horiz = t.get("orientation") == "h"
    cat_i, val_i = (yi, xi) if horiz else (xi, yi)
    cats = cat_i.conv(t.get("y") if horiz else t.get("x") or list(range(len(t.get("x" if horiz else "y") or []))))
    vals = val_i.conv(t.get("x") if horiz else t.get("y") or [])
    n = min(len(cats), len(vals))
    cats, vals = cats[:n], vals[:n]
    marker = t.get("marker") or {}
    mcol = marker.get("color")
    if isinstance(mcol, list):
        cols = [color(c, theme, default=color(cs.SERIES_LIGHT[0], theme), kind="band") for c in mcol][:n]
    else:
        cols = [color(mcol, theme, default=color(cs.SERIES_LIGHT[0], theme), kind="band")] * n
    op = t.get("opacity")
    if op is not None:
        cols = [_with_alpha(c, c[3] * float(op)) for c in cols]
    mop = marker.get("opacity")
    if isinstance(mop, (int, float)):
        cols = [_with_alpha(c, c[3] * float(mop)) for c in cols]
    # 斜線などの模様(marker.pattern.shape)。同じ色の棒を指標ごとに見分けるのに使われている
    shape = ((marker.get("pattern") or {}).get("shape") or "")
    hatch = {"/": "///", "\\": "\\\\\\", "x": "xxx", "-": "---", "|": "|||", "+": "+++", ".": "..."}.get(shape)
    barmode = layout.get("barmode") or "group"
    visible_group = [g for g in group if g.get("visible", True) is not False]
    k = len(visible_group) if barmode == "group" else 1
    idx = visible_group.index(t) if (barmode == "group" and t in visible_group) else 0
    gap = float(layout.get("bargap", 0.2))
    width = t.get("width")
    if cat_i.type == "date":
        if width is not None:
            w = float(width if not isinstance(width, list) else width[0]) / 86400000.0
        else:
            d = np.nanmin(np.diff(np.unique(cats[~np.isnan(cats)]))) if np.sum(~np.isnan(cats)) > 1 else 1.0
            w = d * (1 - gap)
        offs = 0.0
        w_each = w / k
        offs = (idx - (k - 1) / 2) * w_each if barmode == "group" else 0.0
    else:
        slot = 1.0 if cat_i.type == "category" else (
            np.nanmin(np.diff(np.unique(cats[~np.isnan(cats)]))) if np.sum(~np.isnan(cats)) > 1 else 1.0)
        if width is not None and not isinstance(width, list):
            w_each = float(width)
        else:
            w_each = slot * (1 - gap) / k
        offs = (idx - (k - 1) / 2) * w_each if barmode == "group" else 0.0
    base = t.get("base")
    if barmode in ("stack", "relative"):
        stack = ctx.setdefault("_stack", {}).setdefault(id(ax), {})
        bottoms = np.array([stack.get(round(c, 9), 0.0) for c in cats])
        for c, v in zip(cats, vals):
            if not np.isnan(v):
                stack[round(c, 9)] = stack.get(round(c, 9), 0.0) + v
    elif base is not None:
        bottoms = np.array(base if isinstance(base, list) else [base] * n, dtype=float)[:n]
    else:
        bottoms = np.zeros(n)
    edge = _with_alpha(to_rgba(ctx["pal"]["fg"]), 0.45) if hatch else "none"
    if horiz:
        bars = ax.barh(cats + offs, vals, height=w_each, left=bottoms, color=cols, edgecolor=edge, linewidth=0,
                       hatch=hatch, zorder=2)
    else:
        bars = ax.bar(cats + offs, vals, width=w_each, bottom=bottoms, color=cols, edgecolor=edge, linewidth=0,
                      hatch=hatch, zorder=2)
    # 棒の外に数字を出すときは、上端(横棒は右端)で切れないよう余白を足す
    if t.get("text") is not None and (t.get("textposition") or "auto") in ("outside", "auto") and n:
        ends = bottoms + np.nan_to_num(vals)
        hi = float(np.nanmax(ends)) if len(ends) else 0.0
        if hi > 0:
            ctx.setdefault("_headroom", {})[(id(ax), "x" if horiz else "y")] = max(
                ctx.get("_headroom", {}).get((id(ax), "x" if horiz else "y"), 0.0), hi)
    text = t.get("text")
    if text is not None:
        texts = text if isinstance(text, list) else [text] * n
        tf = t.get("textfont") or {}
        tc = color(tf.get("color"), theme, default=to_rgba(ctx["pal"]["fg"]), kind="text")
        pos = t.get("textposition") or "auto"
        for c, v, b, s_ in zip(cats + offs, vals, bottoms, texts):
            if np.isnan(v) or s_ in (None, ""):
                continue
            end = b + v
            inside = pos == "inside"
            if horiz:
                ax.annotate(_strip(s_), (end if not inside else b + v / 2, c), xytext=(3 if not inside else 0, 0),
                            textcoords="offset points", ha="left" if not inside else "center", va="center",
                            fontsize=float(tf.get("size", 10)) * PX, color=tc, family=FONT, annotation_clip=False)
            else:
                ax.annotate(_strip(s_), (c, end if not inside else b + v / 2), xytext=(0, 2 if not inside else 0),
                            textcoords="offset points", ha="center", va="bottom" if not inside else "center",
                            fontsize=float(tf.get("size", 10)) * PX, color=tc, family=FONT, annotation_clip=False)
    return Patch(facecolor=cols[0] if cols else None, hatch=hatch, edgecolor=edge if hatch else None) if n else None


def _colorscale(cs_spec, theme):
    if isinstance(cs_spec, str):
        try:
            return matplotlib.colormaps[cs_spec.lower()]
        except KeyError:
            cs_spec = cs.SEQ_LIGHT
    if not cs_spec:
        cs_spec = cs.SEQ_LIGHT
    spec = [[float(p), c] for p, c in cs_spec]
    if theme == "dark" and json.dumps(spec) == json.dumps([[float(p), c] for p, c in cs.SEQ_LIGHT]):
        spec = [[float(p), c] for p, c in cs.SEQ_DARK]
    return LinearSegmentedColormap.from_list("plotly", [(p, color(c, "light")) for p, c in spec])


def _heatmap(ax, t, xi, yi, ctx):
    theme = ctx["theme"]
    z = np.array([[np.nan if v is None else float(v) for v in row] for row in (t.get("z") or [])], dtype=float)
    if z.size == 0:
        return None
    ny, nx = z.shape
    xs = t.get("x") if t.get("x") is not None else list(range(nx))
    ys = t.get("y") if t.get("y") is not None else list(range(ny))
    if xi.type != "category":
        xi.type = "category"; xi.add_categories(xs)
    if yi.type != "category":
        yi.type = "category"; yi.add_categories(ys)
    xp, yp = xi.conv(xs), yi.conv(ys)
    cmap = _colorscale(t.get("colorscale"), theme)
    vmin = t.get("zmin", np.nanmin(z) if np.isfinite(np.nanmin(z)) else 0)
    vmax = t.get("zmax", np.nanmax(z) if np.isfinite(np.nanmax(z)) else 1)
    xe = np.concatenate([xp - 0.5, [xp[-1] + 0.5]]) if len(xp) else np.array([0, 1])
    ye = np.concatenate([yp - 0.5, [yp[-1] + 0.5]]) if len(yp) else np.array([0, 1])
    order_x, order_y = np.argsort(xp), np.argsort(yp)
    zz = z[np.ix_(order_y, order_x)]
    xe = np.concatenate([np.sort(xp) - 0.5, [np.sort(xp)[-1] + 0.5]])
    ye = np.concatenate([np.sort(yp) - 0.5, [np.sort(yp)[-1] + 0.5]])
    mesh = ax.pcolormesh(xe, ye, np.ma.masked_invalid(zz), cmap=cmap, vmin=vmin, vmax=vmax, shading="flat",
                         edgecolors=to_rgba(ctx["pal"]["surface"], 0.0), linewidth=0)
    ax.set_xlim(xe[0], xe[-1]); ax.set_ylim(ye[0], ye[-1])
    ax.grid(False)
    ctx.setdefault("_nogrid", set()).add(id(ax))
    if t.get("showscale", True) is not False:
        cb = ctx["fig"].colorbar(mesh, ax=ax, fraction=0.035, pad=0.015)
        cb.outline.set_visible(False)
        cb.ax.tick_params(colors=ctx["pal"]["muted"], labelsize=10 * PX, length=0)
        ttl = _title_text(((t.get("colorbar") or {}).get("title")))
        if ttl:
            cb.ax.set_title(ttl, fontsize=10 * PX, color=ctx["pal"]["muted"], family=FONT, loc="left")
    tt = t.get("texttemplate")
    text = t.get("text")
    if tt or text is not None:
        tf = t.get("textfont") or {}
        for i in range(ny):
            for j in range(nx):
                v = z[i, j]
                if tt:
                    s_ = _fmt_template(tt, v, text[i][j] if isinstance(text, list) and isinstance(text[i], list) else None)
                elif isinstance(text, list) and isinstance(text[i], list):
                    s_ = text[i][j]
                else:
                    continue
                if s_ in (None, "") or (isinstance(v, float) and np.isnan(v) and not tt):
                    continue
                frac = 0.0 if vmax == vmin or np.isnan(v) else (v - vmin) / (vmax - vmin)
                bright = frac > 0.55 if theme == "light" else frac < 0.45
                tc = (1, 1, 1, 0.95) if bright else to_rgba(ctx["pal"]["fg"])
                if theme == "dark":
                    tc = (0.07, 0.08, 0.1, 0.95) if frac > 0.55 else to_rgba(ctx["pal"]["fg"])
                ax.text(xp[j], yp[i], _strip(s_), ha="center", va="center", fontsize=float(tf.get("size", 11)) * PX,
                        color=tc, family=FONT)
    return None


def _fmt_template(tt, z, text):
    def rep(m):
        key, fmt = m.group(1), m.group(2)
        val = text if key == "text" else z
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return ""
        if fmt and key != "text":
            try:
                spec = fmt.lstrip(":")
                spec = spec.replace("%", "%") if not spec.endswith("%") else spec
                return format(float(val), spec)
            except (ValueError, TypeError):
                return str(val)
        if isinstance(val, float) and val.is_integer():
            return str(int(val))
        return str(val)
    return re.sub(r"%\{(\w+)(:[^}]*)?\}", rep, tt)


def _box(ax, t, xi, yi, ctx):
    theme = ctx["theme"]
    ys = yi.conv(t.get("y") or [])
    ys = ys[~np.isnan(ys)]
    if not len(ys):
        return None
    xv = t.get("x")
    pos = xi.conv([xv[0]])[0] if xv else xi.conv([t.get("name") or ""])[0]
    if np.isnan(pos):
        pos = 0.0
    line = t.get("line") or {}
    marker = t.get("marker") or {}
    lc = color(line.get("color") or marker.get("color"), theme, default=color(cs.SERIES_LIGHT[0], theme))
    fc = color(t.get("fillcolor"), theme) or _with_alpha(lc, 0.14)
    bp = ax.boxplot([ys], positions=[pos], widths=0.5, patch_artist=True, showfliers=True, manage_ticks=False,
                    medianprops={"color": lc, "linewidth": 1.6}, whiskerprops={"color": lc, "linewidth": 1.1},
                    capprops={"color": lc, "linewidth": 1.1},
                    flierprops={"marker": "o", "markersize": 2.6, "markerfacecolor": lc, "markeredgecolor": "none", "alpha": 0.6})
    for b in bp["boxes"]:
        b.set_facecolor(fc)
        b.set_edgecolor(lc)
        b.set_linewidth(1.1)
        if line.get("dash"):
            b.set_linestyle(_DASH.get(line["dash"], "-"))
    return Patch(facecolor=fc, edgecolor=lc)


def _proxy(t, ctx):
    theme = ctx["theme"]
    typ = t.get("type", "scatter")
    if typ == "scatter":
        line = t.get("line") or {}
        c = color(line.get("color") or (t.get("marker") or {}).get("color"), theme, default=color(cs.SERIES_LIGHT[0], theme))
        return Line2D([0], [0], color=c, lw=2 * PX * 1.1, linestyle=_DASH.get(line.get("dash") or "solid", "-"))
    c = color((t.get("marker") or {}).get("color"), theme, default=color(cs.SERIES_LIGHT[0], theme))
    return Patch(facecolor=c)


# --- 軸の見た目 -------------------------------------------------------------------------------------
def _style_axis(ax, xi, yi, ctx):
    pal = ctx["pal"]
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(axis="both", length=0, colors=pal["muted"], labelsize=11 * PX, pad=4)
    if id(ax) not in ctx.get("_nogrid", set()):
        ax.grid(True, color=pal["grid"], linewidth=0.8)
        ax.set_axisbelow(True)
    def apply_range(ai, which):
        """範囲(range)・逆向き(autorange: reversed)・棒の数字の余白。軸を隠していても効かせる
        (決定木の図は縦軸を隠したうえで逆向きにして、根を上に描いている)。"""
        spec = ai.spec
        rng = spec.get("range")
        if rng and len(rng) == 2:
            lo, hi = ai.conv1(rng[0]), ai.conv1(rng[1])
            if not (np.isnan(lo) or np.isnan(hi)):
                (ax.set_xlim if which == "x" else ax.set_ylim)(lo, hi)
        else:
            head = ctx.get("_headroom", {}).get((id(ax), which))
            if head:
                lo, hi = (ax.get_xlim() if which == "x" else ax.get_ylim())
                (ax.set_xlim if which == "x" else ax.set_ylim)(lo, max(hi, head * (1.16 if which == "x" else 1.12)))
        if spec.get("autorange") == "reversed":
            (ax.invert_xaxis if which == "x" else ax.invert_yaxis)()

    for ai, which in ((xi, "x"), (yi, "y")):
        spec = ai.spec
        axis = ax.xaxis if which == "x" else ax.yaxis
        if spec.get("visible") is False:
            axis.set_visible(False)
            ax.grid(False, axis=which)
            apply_range(ai, which)
            continue
        if spec.get("showgrid") is False:
            ax.grid(False, axis=which)
        if ai.type == "date":
            loc = mdates.AutoDateLocator(minticks=3, maxticks=8)
            fmt = mdates.ConciseDateFormatter(loc, formats=["%Y年", "%-m月", "%-m/%-d", "%H時", "%H:%M", "%S秒"],
                                             zero_formats=["", "%Y年", "%-m月", "%-m/%-d", "%H:%M", "%H:%M"],
                                             offset_formats=["", "%Y年", "%Y年%-m月", "%Y/%-m/%-d", "%Y/%-m/%-d", "%Y/%-m/%-d %H:%M"],
                                             show_offset=False)
            tf = spec.get("tickformat")
            if tf and "%" in tf and not tf.startswith("."):
                fmt = mdates.DateFormatter(tf)
            axis.set_major_locator(loc)
            axis.set_major_formatter(fmt)
        elif ai.type == "category":
            n = len(ai.categories)
            axis.set_major_locator(mticker.FixedLocator(range(n)))
            axis.set_major_formatter(mticker.FixedFormatter([_strip(c) for c in ai.categories]))
            if which == "x":
                ax.set_xlim(-0.5, n - 0.5) if n else None
                ax.grid(False, axis="x")
            else:
                ax.set_ylim(-0.5, n - 0.5) if n else None
                ax.grid(False, axis="y")
        elif ai.type == "log":
            (ax.set_xscale if which == "x" else ax.set_yscale)("log")
        tickvals, ticktext = spec.get("tickvals"), spec.get("ticktext")
        if tickvals is not None:
            pos = ai.conv(tickvals)
            axis.set_major_locator(mticker.FixedLocator(pos))
            if ticktext is not None:
                axis.set_major_formatter(mticker.FixedFormatter([_strip(s) for s in ticktext]))
        elif ai.type in (None, "linear"):
            tf = spec.get("tickformat")
            if tf and tf.endswith("%"):
                digits = int(re.sub(r"\D", "", tf) or 0)
                axis.set_major_formatter(mticker.PercentFormatter(xmax=1.0, decimals=digits))
            dtick = spec.get("dtick")
            if isinstance(dtick, (int, float)) and dtick > 0:
                axis.set_major_locator(mticker.MultipleLocator(dtick))
        apply_range(ai, which)
        ang = spec.get("tickangle")
        if ang not in (None, "auto"):
            for lbl in (ax.get_xticklabels() if which == "x" else ax.get_yticklabels()):
                lbl.set_rotation(-float(ang))
                lbl.set_ha("right" if float(ang) < 0 and which == "x" else "center")
        if spec.get("showticklabels") is False:
            ax.tick_params(axis=which, labelbottom=False, labelleft=False)
        ttl = _title_text(spec.get("title"))
        if ttl:
            if which == "x":
                ax.set_xlabel(ttl, color=pal["fg"], fontsize=11.5 * PX, family=FONT, labelpad=4)
            else:
                ax.set_ylabel(ttl, color=pal["fg"], fontsize=11.5 * PX, family=FONT, labelpad=4)
    if xi.type == "date":
        ax.xaxis.set_tick_params(rotation=0)


# --- 図形・注記 ----------------------------------------------------------------------------------------
def _ref_transform(ref, letter, ctx):
    """Plotlyの座標の種類(x / x2 / paper / x domain)→ (変換, 値の変換関数)。"""
    ref = ref or letter
    fig = ctx["fig"]
    if ref == "paper":
        if letter == "x":
            tr = mtransforms.Affine2D().scale(ctx["pw"], 1).translate(ctx["left"], 0) + fig.transFigure
        else:
            tr = mtransforms.Affine2D().scale(1, ctx["ph"]).translate(0, ctx["bottom"]) + fig.transFigure
        return tr, float, None
    domain = ref.endswith(" domain")
    key = _axis_key(ref.replace(" domain", ""), letter)
    ax = None
    for (xk, yk), a in ctx["axes"].items():
        if (letter == "x" and xk == key) or (letter == "y" and yk == key):
            ax = a
            break
    if ax is None:
        ax = next(iter(ctx["axes"].values()))
    if domain:
        return ax.transAxes, float, ax
    ai = ctx["info"](key)
    return ax.transData, ai.conv1, ax


def _shape(sh, ctx):
    theme = ctx["theme"]
    typ = sh.get("type", "rect")
    xtr, xconv, xax = _ref_transform(sh.get("xref"), "x", ctx)
    ytr, yconv, yax = _ref_transform(sh.get("yref"), "y", ctx)
    tr = mtransforms.blended_transform_factory(xtr, ytr)
    x0, x1 = xconv(sh.get("x0")), xconv(sh.get("x1"))
    y0, y1 = yconv(sh.get("y0")), yconv(sh.get("y1"))
    line = sh.get("line") or {}
    lc = color(line.get("color"), theme, default=ctx["pal"]["zero"], kind="text")
    if lc is not None and theme == "dark" and str(line.get("color", "")).lower() in _DARK_INKS:
        lc = to_rgba(ctx["pal"]["muted"])
    lw = float(line.get("width", 1)) * PX * 1.1
    op = float(sh.get("opacity", 1))
    fig = ctx["fig"]
    clip_ax = xax or yax
    if typ == "line":
        ln = Line2D([x0, x1], [y0, y1], transform=tr, color=_with_alpha(lc, lc[3] * op) if lc else None, lw=lw,
                    linestyle=_DASH.get(line.get("dash") or "solid", "-"), zorder=2.5)
        paper = "paper" in (sh.get("xref"), sh.get("yref"))
        if clip_ax is None or paper:
            ln.set_clip_on(False)
            fig.add_artist(ln)
        else:
            clip_ax.add_line(ln)
        return
    if typ == "rect":
        fill_raw = str(sh.get("fillcolor") or "").lower()
        kind = _TREE_KIND.get(fill_raw)
        if kind:
            fc, ec = _TREE_COLORS[theme][kind]
            box = FancyBboxPatch((min(x0, x1), min(y0, y1)), abs(x1 - x0), abs(y1 - y0),
                                 boxstyle="round,pad=0,rounding_size=0.12", transform=tr, facecolor=_rgba255(fc),
                                 edgecolor=_rgba255(ec), linewidth=1.0, zorder=2, mutation_aspect=abs(y1 - y0) / max(1e-9, abs(x1 - x0)))
            (clip_ax or fig).add_patch(box) if clip_ax is not None else fig.add_artist(box)
            return
        fc = color(sh.get("fillcolor"), theme)
        if fc is not None:
            fc = _with_alpha(fc, fc[3] * op)
        if line.get("width", 1) == 0 or line.get("color") is None:
            ec, lw2 = "none", 0
        else:
            ec, lw2 = lc, lw
        rect = Rectangle((min(x0, x1), min(y0, y1)), abs(x1 - x0), abs(y1 - y0), transform=tr,
                         facecolor=fc if fc is not None else "none", edgecolor=ec, linewidth=lw2,
                         zorder=0.5 if sh.get("layer") == "below" or fc is not None else 2.5)
        if clip_ax is not None and "paper" not in (sh.get("xref"), sh.get("yref")):
            clip_ax.add_patch(rect)
        else:
            rect.set_clip_on(False)
            fig.add_artist(rect)


def _annotation(an, ctx):
    theme = ctx["theme"]
    text = _strip(an.get("text"))
    if not text:
        return
    xtr, xconv, xax = _ref_transform(an.get("xref"), "x", ctx)
    ytr, yconv, yax = _ref_transform(an.get("yref"), "y", ctx)
    tr = mtransforms.blended_transform_factory(xtr, ytr)
    x, y = xconv(an.get("x", 0.5)), yconv(an.get("y", 0.5))
    font = an.get("font") or {}
    tc = color(font.get("color"), theme, default=to_rgba(ctx["pal"]["fg"]), kind="text")
    # 決定木の節の文字は、半透明になった面の上で読めるようテーマの文字色にする
    if an.get("xref", "x") == "x" and an.get("yref", "y") == "y" and theme == "dark":
        tc = to_rgba(ctx["pal"]["fg"])
    xa = an.get("xanchor", "auto")
    ya = an.get("yanchor", "auto")
    ha = {"left": "left", "right": "right", "center": "center"}.get(xa, "center")
    va = {"top": "top", "bottom": "bottom", "middle": "center"}.get(ya, "center")
    if ya == "auto" and an.get("yref") == "paper" and float(an.get("y", 0.5)) >= 1.0:
        va = "bottom"
    size = float(font.get("size", 12)) * PX
    bbox = None
    if an.get("bgcolor"):
        bg = color(an["bgcolor"], theme)
        if bg is not None and theme == "dark" and bg[0] > 0.9 and bg[1] > 0.9:
            bg = (0.12, 0.13, 0.16, 0.85)
        bbox = {"boxstyle": "round,pad=0.25", "facecolor": bg, "edgecolor": "none"}
    xs, ys = float(an.get("xshift", 0)), float(an.get("yshift", 0))
    artist_parent = xax or yax
    kw = dict(ha=ha, va=va, fontsize=size, color=tc, family=FONT, bbox=bbox, zorder=4)
    if xs or ys:
        offset = mtransforms.ScaledTranslation(xs / 100, ys / 100, ctx["fig"].dpi_scale_trans)
        tr = tr + offset
    if artist_parent is not None:
        artist_parent.text(x, y, text, transform=tr, clip_on=False, **kw)
    else:
        ctx["fig"].text(x, y, text, transform=tr, **kw)


# --- 予報グラフのHTMLから図を取り出す --------------------------------------------------------------------
def figure_from_plotly_html(html_text):
    """Plotly.write_htmlで書いたHTML(fog_graphs)から、最初の図の {data, layout} を取り出す。"""
    i = html_text.find("Plotly.newPlot(")
    if i < 0:
        raise ValueError("Plotly figure not found")
    dec = json.JSONDecoder()
    j = i + len("Plotly.newPlot(")
    # 1つ目の引数(図の要素のid)を読み飛ばす
    while html_text[j] in " \t\r\n":
        j += 1
    _, j = dec.raw_decode(html_text, j)
    j = html_text.index(",", j) + 1
    while html_text[j] in " \t\r\n":
        j += 1
    data, j = dec.raw_decode(html_text, j)
    j = html_text.index(",", j) + 1
    while html_text[j] in " \t\r\n":
        j += 1
    layout, j = dec.raw_decode(html_text, j)
    return {"data": data, "layout": layout}


# --- キャッシュ付きの入口 --------------------------------------------------------------------------------
_CACHE = OrderedDict()
_CACHE_MAX = 300
_CACHE_LOCK = threading.Lock()
_RENDER_SLOTS = threading.Semaphore(2)  # 古いCPUなので同時に描くのは2つまで


def render_cached(fig_dict, theme="light", width=900, scale=1.0, notitle=False):
    """同じ図・同じ条件なら前に描いた画像を返す。描けなかったときはメッセージだけの画像を返す。"""
    width = int(round(max(240, min(2400, width)) / 40.0) * 40)
    scale = 2.0 if scale >= 1.5 else 1.0
    key = hashlib.sha1((json.dumps(fig_dict, sort_keys=True, ensure_ascii=False, default=str) +
                        f"|{theme}|{width}|{scale}|{int(bool(notitle))}").encode("utf-8")).hexdigest()
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    with _RENDER_SLOTS:
        try:
            png = render_png(fig_dict, theme=theme, width=width, scale=scale, notitle=notitle)
        except Exception:
            log.exception("mpl_render: failed to render a chart")
            png = _message_png("グラフを表示できませんでした", theme, width, scale)
    with _CACHE_LOCK:
        _CACHE[key] = png
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return png


def _message_png(msg, theme, width, scale):
    fig = Figure(figsize=(width / 100, 1.2), dpi=100)
    FigureCanvasAgg(fig)
    fig.text(0.5, 0.5, msg, ha="center", va="center", color=THEMES[theme]["muted"], fontsize=12 * PX, family=FONT)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100 * scale, transparent=True)
    return buf.getvalue()
