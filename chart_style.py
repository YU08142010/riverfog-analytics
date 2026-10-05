"""グラフ(Plotly)の見た目をサイト全体でそろえる共通の仕上げ(2026-10-05)。

webapp.py・experiment_view.pyのグラフは、JSONにする直前に style_figure() を通す
(webapp._chart_json)。fog_graphs.pyの予測グラフ(HTMLファイル)も書き出す前に通す。
ダークモードの色の切り替えはブラウザ側(base.htmlのpolishChart、予報HTMLはDARK_POST_SCRIPT)で
SERIES_LIGHT → SERIES_DARK のように同じ位置の色に置き換える。

方針(データ可視化の指針):
- 系列の色は検証済みの8色を決まった順に使う(色覚の多様性に配慮した並び。ライト/ダークで
  それぞれ別の明るさの段)。グラフ内の系列には上の順で割り当て、9系列目は作らない。
- 量の濃淡(ヒートマップ)は1色(青)の明→暗。虹色や赤黄の段階は使わない。
- 線は2px、点は8px以上で周りに背景色の縁取り、面は線の色の約12%の薄い塗り。
- グリッド・軸は細い実線で控えめに、タイトルは左寄せ、凡例は上に横並び。
- 予報(破線)・しきい値(破線)など、破線は「予測・基準」の意味でだけ使う。
"""

SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = SERIES_LIGHT
NEUTRAL = "#9a9893"        # 比較の基準(気候値など)の控えめな灰色
INK_SECONDARY = "#52514e"  # しきい値などの注記線
SURFACE_LIGHT = "#ffffff"
SURFACE_DARK = "#1e2027"
FONT = "Inter, 'Zen Kaku Gothic New', -apple-system, 'Hiragino Sans', 'Yu Gothic UI', 'Noto Sans JP', sans-serif"
GRID = "rgba(20,30,50,0.09)"
BASELINE = "rgba(20,30,50,0.28)"
TICK = "#5c6370"
TITLE_INK = "#1d1d1f"

# 1色の濃淡(量の大小)。ライトは薄い→濃い。ダークはブラウザ側でSEQ_DARKに置き換える。
SEQ_LIGHT = [[0.0, "#eef4fc"], [0.2, "#cde2fb"], [0.4, "#86b6ef"], [0.6, "#3987e5"], [0.8, "#1c5cab"], [1.0, "#0d366b"]]
SEQ_DARK = [[0.0, "#1f2633"], [0.2, "#1c3a60"], [0.4, "#1c5cab"], [0.6, "#3987e5"], [0.8, "#86b6ef"], [1.0, "#cde2fb"]]

# 以前の各グラフの直書きの色を、上の決まった色に読み替える(意味のある対応だけ)。
_REMAP = {
    "#1a5276": BLUE, "#2980b9": BLUE, "#3498db": BLUE, "#2a78d6": BLUE,
    "#c0392b": ORANGE, "#e74c3c": ORANGE, "#eb6834": ORANGE,
    "#16a085": AQUA, "#8e44ad": VIOLET,
    "#7f8c8d": NEUTRAL, "#7f2020": INK_SECONDARY,
}


def _remap(color):
    # 1系列1色(文字列)だけを読み替える。点ごとの色のリスト(現象コードの色分け等)は意味のある色なので触らない。
    if isinstance(color, str):
        return _REMAP.get(color.lower(), color)
    return color


def _rgba(hex_color, alpha):
    if not (isinstance(hex_color, str) and hex_color.startswith("#") and len(hex_color) == 7):
        return None
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{alpha})"


def style_figure(fig, remap=True):
    """figの見た目をそろえて返す(その場で書き換える)。データ・軸の範囲・注記の内容は変えない。
    remap=Falseなら系列の色は読み替えない(呼び出し側で決まった色を指定済みの場合)。"""
    _remap_color = _remap if remap else (lambda c: c)
    only_lines = True
    has_scatter = False
    for t in fig.data:
        kind = t.type
        if kind in ("scatter", "scattergl"):
            has_scatter = True
            mode = t.mode or "lines+markers"
            if "lines" not in mode:
                only_lines = False
            if t.line is not None:
                if t.line.color is not None:
                    t.line.color = _remap_color(t.line.color)
                # 1px以上の線(データの線)は2pxに。0.7px等の補助線(決定木の枝など)はそのまま。
                if t.line.width is None or 1.0 <= t.line.width < 2.0:
                    t.line.width = 2
            # 点が多い折れ線は、全部の点に印を付けると混み合うので線だけにする(値はホバーで見られる)。
            n_points = len(t.x) if t.x is not None else 0
            if "lines" in mode and "markers" in mode and n_points > 24:
                mode = mode.replace("+markers", "").replace("markers+", "")
                t.mode = mode
            if "markers" in mode:
                if t.marker.color is not None:
                    t.marker.color = _remap_color(t.marker.color)
                if t.marker.size is None or (isinstance(t.marker.size, (int, float)) and t.marker.size < 8):
                    t.marker.size = 8
                t.marker.line = {"color": SURFACE_LIGHT, "width": 2}
            if t.fill not in (None, "none"):
                base = t.line.color if t.line is not None else None
                wash = _rgba(base, 0.12)
                if wash:
                    t.fillcolor = wash
        elif kind == "bar":
            only_lines = False
            if t.marker.color is not None:
                t.marker.color = _remap_color(t.marker.color)
            t.marker.line = {"width": 0}
        elif kind == "box":
            only_lines = False
            if t.line is not None and t.line.color is not None:
                t.line.color = _remap_color(t.line.color)
            if t.marker is not None and t.marker.color is not None:
                t.marker.color = _remap_color(t.marker.color)
            base = (t.line.color if t.line is not None else None) or (t.marker.color if t.marker is not None else None)
            wash = _rgba(base, 0.14)
            if wash:
                t.fillcolor = wash
            t.line.width = 1.5
        elif kind == "heatmap":
            only_lines = False
            t.colorscale = SEQ_LIGHT
            if t.colorbar is not None:
                t.colorbar.outlinewidth = 0
                t.colorbar.thickness = 12
        else:
            only_lines = False

    layout = fig.layout
    title_text = layout.title.text if layout.title is not None else None
    fig.update_layout(
        font={"family": FONT, "size": 12},
        hoverlabel={"font": {"family": FONT, "size": 12}, "bordercolor": "rgba(0,0,0,0)"},
        bargap=0.35 if layout.bargap is None else layout.bargap,
        barcornerradius=4,
    )
    # 凡例が出るか(名前の付いた系列が2つ以上)。出るなら、タイトルの下・グラフの上に1行分の場所を空ける。
    named = [t for t in fig.data if t.name and t.showlegend is not False]
    legend_shown = layout.showlegend is not False and len(named) >= 2 and layout.legend.orientation is None
    if title_text:
        fig.update_layout(title={"x": 0.0, "xanchor": "left", "xref": "paper",
                                 "y": 1.0, "yref": "container", "yanchor": "top", "pad": {"t": 14},
                                 "font": {"size": 15, "color": TITLE_INK, "weight": 600}})
        current_t = layout.margin.t if layout.margin.t is not None else 100
        need = (100 if "<br>" in title_text else 88) if legend_shown else (70 if "<br>" in title_text else 52)
        fig.update_layout(margin={"t": max(current_t, need)})
    if layout.legend.orientation is None:
        fig.update_layout(legend={"orientation": "h", "x": 0, "xanchor": "left", "y": 1.02, "yanchor": "bottom",
                                  "bgcolor": "rgba(0,0,0,0)", "font": {"size": 11}})
    else:
        fig.update_layout(legend={"bgcolor": "rgba(0,0,0,0)"})
    if layout.hovermode is None and has_scatter and only_lines:
        fig.update_layout(hovermode="x unified")
    axis_style = {
        "gridcolor": GRID, "gridwidth": 1, "griddash": "solid",
        "zerolinecolor": BASELINE, "zerolinewidth": 1,
        "showline": False, "ticks": "",
        "tickfont": {"size": 11, "color": TICK},
        "title": {"font": {"size": 12, "color": INK_SECONDARY}},
    }
    fig.update_xaxes(**axis_style)
    fig.update_yaxes(**axis_style)
    return fig


# 予報グラフ(fog_graphs.pyが書き出す単独のHTML)を、サイトのダーク/ライト設定に合わせる。
# write_htmlのpost_scriptに渡す({plot_id}はPlotlyが置き換える)。サイトと同じオリジンで
# iframe表示されるため、サイトの設定(localStorageのtheme)をそのまま読める。
DARK_POST_SCRIPT = """
(function() {
  var el = document.getElementById('{plot_id}');
  var theme = null;
  try { theme = localStorage.getItem('theme'); } catch (e) {}
  if (theme !== 'dark' && theme !== 'light') {
    theme = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.body.style.margin = '0';
  document.body.style.fontFamily = "%(font)s";
  if (theme !== 'dark' || !window.Plotly || !el) { return; }
  var L = %(light)s, D = %(dark)s;
  var patch = {'font.color': '#e6e8ee', 'title.font.color': '#f2f2f5', 'legend.font.color': '#e6e8ee'};
  Object.keys(el.layout).forEach(function(k) {
    if (!/^[xy]axis\\d*$/.test(k)) { return; }
    patch[k + '.gridcolor'] = 'rgba(255,255,255,0.08)';
    patch[k + '.zerolinecolor'] = 'rgba(255,255,255,0.25)';
    patch[k + '.tickfont.color'] = '#a1a6b2';
    patch[k + '.title.font.color'] = '#c3c7d0';
  });
  Plotly.relayout(el, patch);
  el.data.forEach(function(t, i) {
    var u = {};
    var lc = t.line && t.line.color, mc = t.marker && t.marker.color;
    if (typeof lc === 'string' && L.indexOf(lc) >= 0) { u['line.color'] = D[L.indexOf(lc)]; }
    if (typeof mc === 'string' && L.indexOf(mc) >= 0) { u['marker.color'] = D[L.indexOf(mc)]; }
    if (t.marker && t.marker.line && t.marker.line.color === '%(surface)s') { u['marker.line.color'] = '%(surface_dark)s'; }
    if (t.type === 'heatmap') { u['colorscale'] = [%(seq_dark)s]; }
    if (Object.keys(u).length) { Plotly.restyle(el, u, [i]); }
  });
})();
""" % {
    "font": FONT.replace('"', "'"),
    "light": str(SERIES_LIGHT).replace("'", '"'),
    "dark": str(SERIES_DARK).replace("'", '"'),
    "surface": SURFACE_LIGHT, "surface_dark": SURFACE_DARK,
    "seq_dark": str(SEQ_DARK).replace("'", '"'),
}
