"""管理者タブ「仮検証(二値モデル)」の表示側。

experiment_model.pyが書き出したdata/experiments/binary_forecast_v1.jsonを読み、グラフと表にする。
この検証は本番の予測モデルとは別系統で、ここから本番のデータ・モデルを変更することはない。
webapp.pyからはregister()を1回呼ぶだけ(重い依存を持たないよう、experiment_modelはimportしない)。
"""
import json
import logging
import threading
from pathlib import Path

import plotly.graph_objects as go
import plotly.io as pio
from flask import Response, render_template

BASE_DIR = Path(__file__).resolve().parent
EXP_DIR = BASE_DIR / "data" / "experiments"
RESULT_PATH = EXP_DIR / "binary_forecast_v1.json"
STATUS_PATH = EXP_DIR / "binary_forecast_v1.status.json"
LOG_PATH = BASE_DIR / "logs" / "experiment.log"
SCRIPT_NAME = "experiment_model.py"

log = logging.getLogger("experiment_view")

SHORT = {"CLIM": "気候値", "RULE": "条件指標のみ(ルール)", "V2": "二値 実況→実況(上限)", "V3": "二値 実況→予報値",
         "V4": "二値 予報値→予報値", "V5": "V4+雲量・気圧等", "V6": "V4+条件指標"}
COLORS = {"CLIM": "#7f8c8d", "RULE": "#f1c40f", "V2": "#8e44ad", "V3": "#d35400", "V4": "#1a5276", "V5": "#16a085", "V6": "#c0392b"}
ORDER = ["CLIM", "RULE", "V2", "V3", "V4", "V6", "V5"]
PRODUCTION_COLOR = "#c0392b"

_cache_lock = threading.Lock()
_cache = {"mtime": None, "data": None}


def load_result():
    try:
        mtime = RESULT_PATH.stat().st_mtime
    except OSError:
        return None
    with _cache_lock:
        if _cache["data"] is not None and _cache["mtime"] == mtime:
            return _cache["data"]
    try:
        data = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to read %s: %s", RESULT_PATH, e)
        return None
    with _cache_lock:
        _cache["mtime"], _cache["data"] = mtime, data
    return data


def load_status():
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _tail_log(n=25):
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
    except OSError:
        return []


def _fmt(v, nd=3):
    # 欠測(None)や、テンプレートで値が無いキーを参照したときのUndefinedは「-」にする
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "-"
    return f"{v:.{nd}f}"


def _mtime_str(path):
    try:
        from datetime import datetime, timedelta, timezone
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return None


def _layout(title, height=380, **kw):
    base = {
        "title": {"text": title, "font": {"size": 14}},
        "height": height,
        "margin": {"t": 48, "r": 20, "b": 50, "l": 55},
        "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
        "legend": {"font": {"size": 11}},
    }
    base.update(kw)
    return base


def _empty(title, msg="データがありません"):
    return pio.to_json(go.Figure(layout=_layout(title, annotations=[
        {"text": msg, "xref": "paper", "yref": "paper", "x": 0.5, "y": 0.5, "showarrow": False}])))


def chart_variants(res):
    """バリアントごとのPR-AUCと川霧F1(しきい値は学習データだけから決めた値)。"""
    fig = go.Figure()
    order = ORDER
    for scheme, label, dash in (("chrono", "時系列ホールドアウト", None), ("lomo", "月別leave-one-out", "/")):
        block = res.get(scheme)
        if not block:
            continue
        for metric, mname, opacity in (("ap", "PR-AUC", 1.0), ("f1", "川霧F1", 0.55)):
            ys = [(block["metrics"].get(v) or {}).get(metric) for v in order]
            fig.add_trace(go.Bar(
                x=[SHORT[v] for v in order], y=ys, name=f"{mname}({label})",
                marker={"color": [COLORS[v] for v in order], "opacity": opacity,
                        "pattern": {"shape": dash} if dash else {}},
                text=[_fmt(y, 2) for y in ys], textposition="outside",
            ))
    fig.update_layout(**_layout("方式ごとのPR-AUCと川霧F1(高いほど良い)", height=420, barmode="group",
                                yaxis={"title": {"text": "スコア"}, "rangemode": "tozero"},
                                legend={"orientation": "h", "y": -0.28, "font": {"size": 10}}))
    return pio.to_json(fig)


def chart_pr(res):
    """適合率-再現率曲線(時系列ホールドアウト)。本番の予測は同じ(地点,時刻)での点として重ねる。"""
    block = res.get("chrono")
    if not block:
        return _empty("適合率-再現率曲線")
    fig = go.Figure()
    for v in ("CLIM", "RULE", "V3", "V4", "V6", "V5"):
        pts = block["pr"].get(v)
        if not pts or not pts["precision"]:
            continue
        fig.add_trace(go.Scatter(x=pts["recall"], y=pts["precision"], mode="lines", name=SHORT[v],
                                 line={"color": COLORS[v], "width": 2 if v == "V4" else 1.4}))
    comp = block.get("production_comparison")
    if comp:
        p = comp["production"]
        fig.add_trace(go.Scatter(x=[p["recall"]], y=[p["precision"]], mode="markers", name="現行の本番(同じ地点・時刻)",
                                 marker={"color": PRODUCTION_COLOR, "size": 12, "symbol": "x"}))
        n = comp["new_v4"]
        fig.add_trace(go.Scatter(x=[n["recall"]], y=[n["precision"]], mode="markers", name="二値 予報値→予報値(採用しきい値)",
                                 marker={"color": COLORS["V4"], "size": 11, "symbol": "diamond"}))
    rate = (block["metrics"].get("V4") or {}).get("rate")
    if rate:
        fig.add_hline(y=rate, line={"dash": "dot", "width": 1}, annotation_text="発生率(無作為)", annotation_position="bottom right")
    fig.update_layout(**_layout(f"適合率-再現率曲線(評価期間 {block['window'][0]}〜{block['window'][1]})", height=400,
                                xaxis={"title": {"text": "再現率(Recall)"}, "range": [0, 1]},
                                yaxis={"title": {"text": "適合率(Precision)"}, "range": [0, 1]}))
    return pio.to_json(fig)


def chart_reliability(res):
    """予測確率と実際の発生率(較正)。対角線に近いほど確率として信頼できる。"""
    block = res.get("lomo") or res.get("chrono")
    if not block:
        return _empty("較正曲線")
    fig = go.Figure()
    mx = 0.05
    for v in ("V3", "V4", "V6"):
        rows = block["reliability"].get(v) or []
        if not rows:
            continue
        mx = max(mx, max(r["pred"] for r in rows), max(r["obs"] for r in rows))
        fig.add_trace(go.Scatter(x=[r["pred"] for r in rows], y=[r["obs"] for r in rows], mode="lines+markers",
                                 name=SHORT[v], line={"color": COLORS[v]},
                                 text=[f"n={r['n']}" for r in rows]))
    mx = min(1.0, mx * 1.1)
    fig.add_trace(go.Scatter(x=[0, mx], y=[0, mx], mode="lines", name="理想(予測=実際)",
                             line={"color": "#7f8c8d", "dash": "dash", "width": 1}))
    fig.update_layout(**_layout("較正曲線(予測した確率と、実際の川霧の発生率)", height=380,
                                xaxis={"title": {"text": "予測確率(各ビンの平均)"}, "range": [0, mx]},
                                yaxis={"title": {"text": "実際の発生率"}, "range": [0, mx]}))
    return pio.to_json(fig)


def chart_months(res):
    block = res.get("lomo")
    if not block or not block.get("by_month"):
        return _empty("月別の精度")
    v4 = block["by_month"].get("V4", {})
    cl = block["by_month"].get("CLIM", {})
    months = sorted(v4.keys())
    fig = go.Figure()
    fig.add_trace(go.Bar(x=months, y=[(cl.get(m) or {}).get("ap") for m in months], name="気候値 PR-AUC",
                         marker={"color": COLORS["CLIM"]}))
    fig.add_trace(go.Bar(x=months, y=[(v4.get(m) or {}).get("ap") for m in months], name="二値 予報値→予報値 PR-AUC",
                         marker={"color": COLORS["V4"]}))
    fig.add_trace(go.Scatter(x=months, y=[(v4.get(m) or {}).get("rate") for m in months], name="川霧の発生率",
                             mode="lines+markers", line={"color": PRODUCTION_COLOR, "dash": "dot"}))
    fig.update_layout(**_layout("月別のPR-AUC(その月を除いて学習)", height=360, barmode="group",
                                yaxis={"title": {"text": "PR-AUC / 発生率"}, "rangemode": "tozero"},
                                legend={"orientation": "h", "y": -0.25}))
    return pio.to_json(fig)


def chart_hours(res):
    block = res.get("lomo")
    if not block or not block.get("by_hour"):
        return _empty("時刻別の精度")
    v4 = block["by_hour"].get("V4", {})
    hours = sorted(int(h) for h in v4.keys())
    get = lambda h, k: v4[str(h)][k] if str(h) in v4 else v4[h][k]  # noqa: E731
    fig = make_subplots_hours(hours, get)
    return pio.to_json(fig)


def make_subplots_hours(hours, get):
    fig = go.Figure()
    fig.add_trace(go.Bar(x=hours, y=[get(h, "fog_n") / max(get(h, "n"), 1) for h in hours], name="川霧の発生率",
                         marker={"color": "#aed6f1"}, yaxis="y2", opacity=0.7))
    fig.add_trace(go.Scatter(x=hours, y=[get(h, "f1") for h in hours], name="川霧F1(二値 予報値→予報値)",
                             mode="lines+markers", line={"color": COLORS["V4"]}))
    fig.add_trace(go.Scatter(x=hours, y=[get(h, "recall") for h in hours], name="再現率", mode="lines",
                             line={"color": "#27ae60", "dash": "dot"}))
    fig.add_trace(go.Scatter(x=hours, y=[get(h, "precision") for h in hours], name="適合率", mode="lines",
                             line={"color": "#e67e22", "dash": "dot"}))
    fig.update_layout(**_layout("時刻別の川霧F1と発生率", height=360,
                                xaxis={"title": {"text": "時刻(JST)"}, "dtick": 1},
                                yaxis={"title": {"text": "F1 / 適合率 / 再現率"}, "range": [0, 1]},
                                yaxis2={"title": {"text": "発生率"}, "overlaying": "y", "side": "right", "showgrid": False,
                                        "rangemode": "tozero"},
                                legend={"orientation": "h", "y": -0.28}))
    return fig


def chart_importance(res):
    block = res.get("chrono")
    rows = (block or {}).get("importance") or []
    if not rows:
        return _empty("特徴量の重要度")
    rows = rows[:16][::-1]
    fig = go.Figure(go.Bar(x=[r["ap_drop"] for r in rows], y=[r["feature"] for r in rows], orientation="h",
                           marker={"color": COLORS["V4"]}))
    fig.update_layout(**_layout("特徴量の重要度(その特徴量をシャッフルしたときのPR-AUCの低下)", height=460,
                                xaxis={"title": {"text": "PR-AUCの低下(大きいほど重要)"}},
                                margin={"t": 48, "r": 20, "b": 50, "l": 130}))
    return pio.to_json(fig)


def chart_cond(res):
    """満たした条件の数(0〜4)ごとの川霧の発生率。"""
    ci = res.get("condition_index")
    if not ci:
        return _empty("条件指標")
    fig = go.Figure()
    for key, label, color in (("forecast", "過去予報値", COLORS["V4"]), ("reanalysis", "実況(再解析)", "#8e44ad")):
        rows = (ci.get(key) or {}).get("by_count") or []
        if not rows:
            continue
        fig.add_trace(go.Bar(x=[f"{r['count']}個" for r in rows], y=[(r["rate"] or 0) * 100 for r in rows], name=label,
                             marker={"color": color},
                             text=[f"{(r['rate'] or 0) * 100:.1f}%<br>n={r['n']:,}" for r in rows], textposition="outside"))
    base = ((ci.get("forecast") or {}).get("base_rate") or 0) * 100
    if base:
        fig.add_hline(y=base, line={"dash": "dot", "width": 1}, annotation_text=f"全体の発生率 {base:.1f}%", annotation_position="top left")
    fig.update_layout(**_layout("満たした条件の数ごとの川霧の発生率", height=380, barmode="group",
                                xaxis={"title": {"text": "満たした条件の数(気温・露点差・風速・時間帯)"}},
                                yaxis={"title": {"text": "川霧の発生率(%)"}, "rangemode": "tozero"},
                                legend={"orientation": "h", "y": -0.28}))
    return pio.to_json(fig)


def chart_importance_cond(res):
    rows = ((res.get("chrono") or {}).get("importance_v6")) or []
    if not rows:
        return _empty("特徴量の重要度(V4+条件指標)")
    rows = rows[:16][::-1]
    colors = ["#c0392b" if r["feature"].startswith("条件") else COLORS["V4"] for r in rows]
    fig = go.Figure(go.Bar(x=[r["ap_drop"] for r in rows], y=[r["feature"] for r in rows], orientation="h", marker={"color": colors}))
    fig.update_layout(**_layout("特徴量の重要度(V4+条件指標、赤が条件指標)", height=460,
                                xaxis={"title": {"text": "PR-AUCの低下(大きいほど重要)"}},
                                margin={"t": 48, "r": 20, "b": 50, "l": 130}))
    return pio.to_json(fig)


CHARTS = {
    "variants": chart_variants, "pr": chart_pr, "reliability": chart_reliability,
    "months": chart_months, "hours": chart_hours, "importance": chart_importance,
    "cond": chart_cond, "importance_cond": chart_importance_cond,
}


# ---------------------------------------------------------------------------
# ラベル品質ページ(label_audit.py / 一回限りの評価label_eval.json / 手書きの対応履歴label_actions.json)
# ---------------------------------------------------------------------------
AUDIT_PATH = EXP_DIR / "label_audit.json"
EVAL_PATH = EXP_DIR / "label_eval.json"
ACTIONS_PATH = EXP_DIR / "label_actions.json"
EVAL_MODELS_PATH = EXP_DIR / "label_eval_models.json"
CONF_COLORS = {"high": "#27ae60", "medium": "#e67e22", "low": "#c0392b"}
CONF_LABELS = {"high": "high(確信あり)", "medium": "medium", "low": "low"}


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def chart_label_conf(audit, _eval):
    rows = (audit or {}).get("by_confidence") or []
    if not rows:
        return _empty("確信度別の訂正率")
    cs = [r["confidence"] for r in rows]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=[CONF_LABELS[c] for c in cs], y=[(r["corrected_rate"] or 0) * 100 for r in rows],
                         name="人間が確認したラベルのうち訂正された割合(%)", marker={"color": [CONF_COLORS[c] for c in cs]},
                         text=[f"{r['corrected']}/{r['reviewed']}" for r in rows], textposition="outside"))
    fig.add_trace(go.Scatter(x=[CONF_LABELS[c] for c in cs], y=[r["share_of_all_labels"] * 100 for r in rows],
                             name="全ラベルに占める割合(%)", mode="lines+markers", line={"color": "#7f8c8d", "dash": "dot"}))
    fig.update_layout(**_layout("Geminiの確信度別: 人間に訂正された割合", height=360,
                                yaxis={"title": {"text": "%"}, "range": [0, 105]},
                                legend={"orientation": "h", "y": -0.25}))
    return pio.to_json(fig)


def chart_label_flow(audit, _eval):
    flow = (audit or {}).get("flow")
    if not flow or not flow.get("codes"):
        return _empty("訂正の流れ")
    codes = [str(c) for c in flow["codes"]]
    fig = go.Figure(go.Heatmap(z=flow["matrix"], x=codes, y=codes, colorscale="Blues", showscale=False,
                               text=flow["matrix"], texttemplate="%{text}", hovertemplate="Gemini %{y} → 人間 %{x}: %{z}件<extra></extra>"))
    fig.update_layout(**_layout("人間による訂正の内訳(行=Geminiの判定、列=人間の訂正後)", height=420,
                                xaxis={"title": {"text": "訂正後のコード(人間)"}, "type": "category"},
                                yaxis={"title": {"text": "元のコード(Gemini)"}, "type": "category", "autorange": "reversed"}))
    return pio.to_json(fig)


def chart_label_hours(audit, _eval):
    rows = (audit or {}).get("by_hour") or []
    if not rows:
        return _empty("時刻別の訂正件数")
    hours = [r["hour"] for r in rows]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=hours, y=[r["reviewed"] for r in rows], name="人間が確認した件数", marker={"color": "#aed6f1"}))
    fig.add_trace(go.Bar(x=hours, y=[r["corrected"] for r in rows], name="そのうち訂正された件数", marker={"color": "#c0392b"}))
    fig.update_layout(**_layout("時刻別: 人間が確認・訂正した件数", height=340, barmode="overlay",
                                xaxis={"title": {"text": "時刻(JST)"}, "dtick": 1}, yaxis={"title": {"text": "件数"}},
                                legend={"orientation": "h", "y": -0.28}))
    return pio.to_json(fig)


def chart_label_groups(audit, _eval):
    rows = (audit or {}).get("confidence_by_group") or []
    if not rows:
        return _empty("現象別の確信度")
    fig = go.Figure()
    for c in ("high", "medium", "low"):
        fig.add_trace(go.Bar(x=[r["group"] for r in rows], y=[r[c] for r in rows], name=CONF_LABELS[c], marker={"color": CONF_COLORS[c]}))
    fig.update_layout(**_layout("Geminiの判定した現象ごとの確信度(2026-08-25以降の全ラベル)", height=360, barmode="stack",
                                yaxis={"title": {"text": "件数"}}, legend={"orientation": "h", "y": -0.25}))
    return pio.to_json(fig)


def chart_label_models(_audit, ev):
    if not ev:
        return _empty("Gemini設定の比較", "まだ評価が実行されていません")
    names = [n for n, v in ev.items() if v.get("score")]
    if not names:
        return _empty("Gemini設定の比較")
    fig = go.Figure()
    for key, label, color in (("group", "現象グループの一致率", "#1a5276"), ("fog_f1", "川霧F1", "#16a085"),
                              ("fp_on_truth_clear", "現象なしを川霧と誤判定した率(低いほど良い)", "#c0392b")):
        ys = [(ev[n]["score"].get(key)) for n in names]
        fig.add_trace(go.Bar(x=names, y=ys, name=label, marker={"color": color}, text=[_fmt(y, 2) for y in ys], textposition="outside"))
    fig.update_layout(**_layout("人間が確認した画像でのGemini設定の比較", height=430, barmode="group",
                                yaxis={"range": [0, 1.1]}, legend={"orientation": "h", "y": -0.35}))
    return pio.to_json(fig)


LABEL_CHARTS = {
    "conf": chart_label_conf, "flow": chart_label_flow, "hours": chart_label_hours,
    "groups": chart_label_groups, "models": chart_label_models,
}


def _table_rows(block):
    if not block:
        return []
    rows = []
    for v in ORDER:
        m = block["metrics"].get(v)
        if not m:
            continue
        rows.append({"id": v, "label": SHORT[v], **m})
    return rows


def _conclusions(res):
    """数値から機械的に作る要点(定型の文言に実測値を差し込むだけで、解釈は書かない)。"""
    out = []
    for scheme, label in (("chrono", "時系列ホールドアウト"), ("lomo", "月別leave-one-out")):
        block = res.get(scheme)
        if not block:
            continue
        m = block["metrics"]
        v4, cl, v3, v2 = m.get("V4"), m.get("CLIM"), m.get("V3"), m.get("V2")
        if not v4 or v4.get("ap") is None or not cl or cl.get("ap") is None:
            continue
        s = f"[{label}] 二値・予報値学習のPR-AUCは{v4['ap']:.3f}(気候値は{cl['ap']:.3f}、約{v4['ap'] / cl['ap']:.1f}倍)、川霧F1は{v4['f1']:.3f}(気候値{cl['f1']:.3f})。"
        if v3 and v3.get("ap") is not None:
            s += f" 実況で学習して予報値で評価した場合(現行の本番と同じ構図)のPR-AUCは{v3['ap']:.3f}。"
        if v2 and v2.get("ap") is not None:
            s += f" 実況で評価した上限側は{v2['ap']:.3f}。"
        out.append(s)
    for scheme, label in (("chrono", "時系列ホールドアウト"), ("lomo", "月別leave-one-out")):
        m = (res.get(scheme) or {}).get("metrics") or {}
        v4, v6, rule, cl = m.get("V4"), m.get("V6"), m.get("RULE"), m.get("CLIM")
        if rule and rule.get("ap") is not None and cl and cl.get("ap") is not None:
            out.append(f"[{label}] 条件指標だけのルールは、PR-AUC {rule['ap']:.3f}、川霧F1 {rule['f1']:.3f}(気候値はPR-AUC {cl['ap']:.3f}、F1 {cl['f1']:.3f})。")
        if v4 and v6 and v4.get("ap") is not None and v6.get("ap") is not None:
            out.append(f"[{label}] V4に条件指標を足すと、PR-AUCは {v4['ap']:.3f} → {v6['ap']:.3f}、川霧F1は {v4['f1']:.3f} → {v6['f1']:.3f}。")
    comp = (res.get("chrono") or {}).get("production_comparison")
    if comp:
        p, n = comp["production"], comp["new_v4"]
        out.append(
            f"[本番との比較] 同じ{comp['n']:,}件(地点×時刻、川霧{comp['fog_n']}件)で、現行の本番の川霧F1は{p['f1']:.3f}、"
            f"二値・予報値学習は{n['f1']:.3f}。Brierスコアは本番{p['brier']:.4f}、新モデル{n['brier']:.4f}"
            f"(発生率だけを返す基準は{comp['climatology_brier']:.4f}、小さいほど良い)。")
    return out


def register(app, requires_admin, cached_chart_json, is_script_running):
    """webapp.pyから1回呼ぶ。admin_experiment(ページ)とadmin_experiment_chart(グラフJSON)を登録する。"""

    @app.context_processor
    def _inject_experiment_tab():
        return {"experiment_tab_available": True}

    @app.route("/admin/experiment")
    @requires_admin
    def admin_experiment():
        res = load_result()
        status = load_status()
        running = is_script_running(SCRIPT_NAME)
        view = None
        if res:
            view = {
                "meta": res["meta"], "variants": res["variants"],
                "chrono_rows": _table_rows(res.get("chrono")), "lomo_rows": _table_rows(res.get("lomo")),
                "chrono": res.get("chrono"), "lomo": res.get("lomo"),
                "comp": (res.get("chrono") or {}).get("production_comparison"),
                "gain": (res.get("gain_importance") or [])[:12],
                "spread": res.get("spread_table") or [],
                "cond": res.get("condition_index"),
                "conclusions": _conclusions(res),
                "stations": ((res.get("lomo") or {}).get("by_station") or [])[:25],
            }
        return render_template("admin_experiment.html", view=view, status=status, running=running,
                               log_lines=_tail_log(), fmt=_fmt, short=SHORT)

    @app.route("/admin/experiment/labels")
    @requires_admin
    def admin_experiment_labels():
        audit = _load_json(AUDIT_PATH)
        ev = _load_json(EVAL_PATH)
        actions = _load_json(ACTIONS_PATH) or []
        ev_rows = []
        for name, v in (ev or {}).items():
            s = v.get("score") or {}
            ev_rows.append({"name": name, "model": v.get("model"), "weather": v.get("weather"),
                            "stopped": v.get("stopped_early"), **s})
        mev = _load_json(EVAL_MODELS_PATH) or {}
        model_rows = [{"name": n, "model": v.get("model"), "failed": v.get("failed"), "n_done": v.get("n_done"),
                       **(v.get("score") or {})} for n, v in mev.items()]
        return render_template(
            "admin_experiment_labels.html", audit=audit, ev_rows=ev_rows, actions=actions, model_rows=model_rows,
            running=is_script_running("label_audit.py"), fmt=_fmt,
            eval_generated=(_mtime_str(EVAL_PATH)),
        )

    @app.route("/admin/experiment/labels/chart/<name>.json")
    @requires_admin
    def admin_experiment_label_chart(name):
        fn = LABEL_CHARTS.get(name)
        if fn is None:
            return Response("not found", status=404)

        def render():
            return fn(_load_json(AUDIT_PATH), _load_json(EVAL_PATH))

        return Response(cached_chart_json(f"experiment_label_{name}", [AUDIT_PATH, EVAL_PATH], render),
                        mimetype="application/json")

    @app.route("/admin/experiment/chart/<name>.json")
    @requires_admin
    def admin_experiment_chart(name):
        fn = CHARTS.get(name)
        if fn is None:
            return Response("not found", status=404)

        def render():
            res = load_result()
            return fn(res) if res else _empty("仮検証", "まだ実行されていません")

        return Response(cached_chart_json(f"experiment_{name}", [RESULT_PATH], render), mimetype="application/json")
