"""月ごとの自動レポート(2026-10-11)。毎月1日の朝(crontab)に、前の月の川霧の観測と予測の成績をPDFにまとめる。

  .venv/bin/python monthly_report.py [YYYY-MM] [--no-push]

内容: 川霧を観測した日数(地点別・地域別)、日ごとの観測地点数、時刻別の出やすさ、前日夜の予測の成績(地域別)、
その月のいちばん霧が出ていた写真。data/reports/<YYYY-MM>.pdf と同名の.json(画面の一覧用の要約)に書き、
通知(Web Push)で知らせる(設定画面の「月ごとのレポート」がオンの人)。
観測はカメラ画像のAI判定(人の訂正があればそちら)、日の出〜日の入りの画像だけ。
"""
import argparse
import csv
import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from PIL import Image  # noqa: E402

import gallery  # noqa: E402
from common import BASE_DIR, CAMERAS_CSV, JST  # noqa: E402

OUT_DIR = BASE_DIR / "data" / "reports"
FONT_FILE = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
INK, MUTED, GRID = "#1d2733", "#5c6370", "#e3e7ec"
BLUE, ORANGE, GRAY = "#2a78d6", "#e8693a", "#9a9a94"
REGION_COLORS = {"只見川": BLUE, "会津": ORANGE}   # どの図でも同じ色にする
REGION_ORDER = {"只見川": 0, "会津": 1}
log = logging.getLogger("monthly_report")


def _setup_font():
    try:
        font_manager.fontManager.addfont(FONT_FILE)
        name = font_manager.FontProperties(fname=FONT_FILE).get_name()
        plt.rcParams["font.family"] = [name, "DejaVu Sans"]
    except Exception:
        pass
    plt.rcParams.update({"axes.edgecolor": GRID, "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                         "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID,
                         "axes.axisbelow": True, "font.size": 9})


def _prim(v):
    v = int(v)
    return v // 100 if v > 10 else v


def collect(month):
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    s, e = start.strftime("%Y-%m-%d 00:00:00"), (end - timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
    from fog_features import daylight_mask
    regions, coords = {}, {}
    with open(CAMERAS_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            regions[row["地点名"]] = row.get("地域") or "その他"
            if row.get("緯度") and row.get("経度"):
                coords[row["地点名"]] = (float(row["緯度"]), float(row["経度"]))
    w = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'weather.db'}?mode=ro", uri=True)
    ph = pd.read_sql("SELECT datetime, station_name AS station, code FROM phenomena WHERE datetime BETWEEN ? AND ? "
                     "AND code IS NOT NULL AND code >= 0", w, params=(s, e))
    w.close()
    u = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'users.db'}?mode=ro", uri=True)
    corr = {(st, d): c for st, d, c in u.execute("SELECT station_name, target_datetime, corrected_code FROM phenomena_corrections "
                                                  "WHERE target_datetime BETWEEN ? AND ?", (s, e))}
    u.close()
    ph = ph[ph["station"].isin(regions)]
    ph["code"] = [corr.get((st, d), c) for st, d, c in zip(ph["station"], ph["datetime"], ph["code"])]
    keep = np.zeros(len(ph), dtype=bool)
    for st, idx in ph.groupby("station").groups.items():
        lat, lon = coords.get(st, (37.4, 139.6))
        keep[ph.index.get_indexer(idx)] = np.asarray(daylight_mask(ph.loc[idx, "datetime"], lat, lon), dtype=bool)
    ph = ph[keep].copy()
    ph["fog"] = ph["code"].map(_prim).isin([1, 2, 3])
    ph["day"] = ph["datetime"].str[:10]
    ph["hour"] = ph["datetime"].str[11:13].astype(int)
    ph["region"] = ph["station"].map(regions)

    fog = ph[ph["fog"]]
    by_station = (fog.groupby("station")["day"].nunique().reindex(list(regions), fill_value=0)).sort_values(ascending=False)
    days = pd.date_range(start, end - timedelta(days=1)).strftime("%Y-%m-%d")
    per_day = fog.groupby(["day", "region"])["station"].nunique().unstack(fill_value=0).reindex(days, fill_value=0)
    by_hour = (fog.groupby("hour").size() / ph.groupby("hour").size()).fillna(0) * 100

    # 前日夜の予測の成績(その日0時より前で最新の本番の予測。公開ページの照合と同じ選び方)
    import prediction_log
    p = sqlite3.connect(f"file:{BASE_DIR / 'data' / 'predictions.db'}?mode=ro", uri=True)
    perf = {}
    try:
        for d in days:
            d0 = d + " 00:00:00"
            run_at = p.execute("SELECT MAX(run_at) FROM predictions WHERE model_type LIKE ? AND model_type NOT LIKE '%[再予測]' "
                               "AND run_at < ? AND target_datetime BETWEEN ? AND ?",
                               (prediction_log.CURRENT_MODEL_LIKE, d0, d0, d + " 23:59:59")).fetchone()[0]
            if not run_at:
                continue
            for st, tdt, pred in p.execute("SELECT station, target_datetime, predicted_code FROM predictions WHERE run_at = ? "
                                           "AND target_datetime BETWEEN ? AND ? AND COALESCE(daylight, 1) = 1",
                                           (run_at, d0, d + " 23:59:59")):
                perf[(st, tdt)] = int(pred or 0) == 1
    finally:
        p.close()
    ph["pred"] = [perf.get((st, d)) for st, d in zip(ph["station"], ph["datetime"])]
    scored = ph[ph["pred"].notna()]
    perf_by_region = {}
    for reg, d in list(scored.groupby("region")) + [("全体", scored)]:
        pred, act = d["pred"].astype(bool), d["fog"]
        tp, fp, fn = int((pred & act).sum()), int((pred & ~act).sum()), int((~pred & act).sum())
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        f1 = 2 * prec * rec / (prec + rec) if prec and rec else None
        perf_by_region[reg] = {"n": int(len(d)), "fog": int(act.sum()), "hit": tp, "false_alarm": fp, "miss": fn,
                               "precision": prec, "recall": rec, "f1": f1}

    # いちばん霧が出ていた写真(各日の朝のギャラリーの1位から、かすみ度の高い順に6枚)
    best = []
    for d in days:
        items = gallery.morning_fog(d, limit=1)
        if items:
            best.append(items[0])
    best.sort(key=lambda it: ((it["score"] if it["score"] is not None else -1), it["code"]), reverse=True)
    seen, distinct = set(), []
    for it in best:  # 同じ地点が並ばないよう、地点ごとに1枚
        if it["station"] not in seen:
            seen.add(it["station"]); distinct.append(it)
    best = distinct

    return {
        "month": month, "label": f"{y}年{m}月", "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "n_images": int(len(ph)), "n_fog_images": int(len(fog)),
        "fog_days_any": int(fog["day"].nunique()), "n_days": len(days),
        "by_region_days": {r: int(fog[fog["region"] == r]["day"].nunique()) for r in sorted(set(regions.values()))},
        "top_stations": [{"station": s, "days": int(n), "region": regions[s]} for s, n in by_station.head(10).items() if n > 0],
        "per_day": {"days": list(days), "regions": {r: [int(x) for x in per_day[r]] for r in per_day.columns}},
        "by_hour": {int(h): round(float(v), 2) for h, v in by_hour.items()},
        "performance": perf_by_region,
        "best_images": best[:6],
    }


def render_pdf(data, path):
    _setup_font()
    with PdfPages(path) as pdf:
        # 1ページ目: 要約と日ごとの観測地点数・上位の地点
        fig = plt.figure(figsize=(8.27, 11.69))
        fig.text(0.07, 0.955, f"川霧の観測と予測 {data['label']}", fontsize=18, color=INK, weight="bold")
        fig.text(0.07, 0.935, f"作成 {data['generated_at']} ・ カメラ画像のAI判定(人の訂正を反映)・日の出〜日の入りの画像",
                 fontsize=8, color=MUTED)
        perf = data["performance"].get("全体", {})
        pct = lambda v: "-" if v is None else f"{100 * v:.0f}%"  # noqa: E731
        stats = [("川霧を観測した日", f"{data['fog_days_any']} / {data['n_days']}日"),
                 ("川霧の画像", f"{data['n_fog_images']:,}枚"),
                 ("予測の適合率", pct(perf.get("precision"))), ("予測の再現率", pct(perf.get("recall")))]
        for i, (k, v) in enumerate(stats):
            x = 0.07 + i * 0.225
            fig.text(x, 0.895, k, fontsize=8, color=MUTED)
            fig.text(x, 0.868, v, fontsize=15, color=INK, weight="bold")
        ax = fig.add_axes([0.09, 0.58, 0.86, 0.24])
        pdays = data["per_day"]
        x = np.arange(len(pdays["days"]))
        bottom = np.zeros(len(x))
        for reg in sorted(pdays["regions"], key=lambda r: REGION_ORDER.get(r, 9)):
            color = REGION_COLORS.get(reg, GRAY)
            vals = np.array(pdays["regions"][reg])
            ax.bar(x, vals, bottom=bottom, color=color, width=0.75, label=reg)
            bottom += vals
        ax.set_xticks(x[::3]); ax.set_xticklabels([d[8:].lstrip("0") for d in pdays["days"]][::3])
        ax.set_title("日ごとの、川霧を観測した地点の数", loc="left", fontsize=11, color=INK)
        ax.set_xlabel("日"); ax.set_ylabel("地点数"); ax.grid(axis="x", visible=False)
        ax.legend(frameon=False, loc="upper right", ncol=3)
        ax2 = fig.add_axes([0.25, 0.08, 0.68, 0.4])
        top = data["top_stations"][::-1]
        ax2.barh([t["station"] for t in top], [t["days"] for t in top],
                 color=[REGION_COLORS.get(t["region"], GRAY) for t in top], height=0.65)
        for i, t in enumerate(top):
            ax2.text(t["days"] + 0.2, i, f"{t['days']}日", va="center", fontsize=8, color=INK)
        ax2.set_title("川霧を観測した日数が多かった地点", loc="left", fontsize=11, color=INK)
        ax2.set_xlabel("日数"); ax2.grid(axis="y", visible=False)
        pdf.savefig(fig); plt.close(fig)

        # 2ページ目: 時刻別・予測の成績
        fig = plt.figure(figsize=(8.27, 11.69))
        ax = fig.add_axes([0.09, 0.6, 0.86, 0.3])
        by_hour = {int(h): v for h, v in data["by_hour"].items()}  # JSONから読むとキーが文字列になる
        hours = sorted(by_hour)
        ax.bar(hours, [by_hour[h] for h in hours], color=ORANGE, width=0.7)
        ax.set_title("時刻ごとの、川霧が写っていた画像の割合", loc="left", fontsize=11, color=INK)
        ax.set_xlabel("時"); ax.set_ylabel("割合(%)"); ax.set_xticks(hours); ax.grid(axis="x", visible=False)
        fig.text(0.07, 0.5, "前日夜の予測の成績(「出やすい」と予測した時刻と、実際に川霧が写っていた時刻の照合)",
                 fontsize=11, color=INK)
        rows = [["地域", "照合した時刻", "川霧", "的中", "空振り", "見逃し", "適合率", "再現率", "F1"]]
        for reg, v in data["performance"].items():
            rows.append([reg, f"{v['n']:,}", v["fog"], v["hit"], v["false_alarm"], v["miss"], pct(v["precision"]),
                         pct(v["recall"]), pct(v["f1"])])
        tab = fig.add_axes([0.07, 0.3, 0.86, 0.18]); tab.axis("off")
        t = tab.table(cellText=rows, loc="upper left", cellLoc="center")
        t.auto_set_font_size(False); t.set_fontsize(8.5); t.scale(1, 1.6)
        for (r, c), cell in t.get_celld().items():
            cell.set_edgecolor(GRID)
            if r == 0:
                cell.set_text_props(weight="bold", color=INK); cell.set_facecolor("#f3f5f8")
        if not data["performance"].get("全体", {}).get("n"):
            fig.text(0.07, 0.24, "この月は、いまの予測モデル(2026-10-04〜)が前日夜に出した予測が無いため、成績は出していません。\n"
                     "後からやり直した予測は、当時の予測ではないので数えていません。", fontsize=8, color=INK, linespacing=1.6)
        fig.text(0.07, 0.27, "適合率: 出やすいと予測した時刻のうち、実際に出た割合。再現率: 実際に出た時刻のうち、予測できていた割合。",
                 fontsize=7.5, color=MUTED)
        pdf.savefig(fig); plt.close(fig)

        # 3ページ目: いちばん霧が出ていた写真
        best = data["best_images"]
        if best:
            fig = plt.figure(figsize=(8.27, 11.69))
            fig.text(0.07, 0.955, "この月のいちばん霧が出ていた朝", fontsize=13, color=INK, weight="bold")
            for i, it in enumerate(best[:6]):
                r, c = divmod(i, 2)
                ax = fig.add_axes([0.06 + c * 0.46, 0.66 - r * 0.3, 0.42, 0.25])
                p = gallery.IMAGE_DIR / it["datetime"][:10] / it["hhmm"] / f"{it['station']}.jpg"
                with Image.open(p) as im:
                    ax.imshow(im.convert("RGB"))
                ax.axis("off")
                ax.set_title(f"{it['station']}  {it['datetime'][5:10].replace('-', '/')} {it['hhmm'][:2]}:{it['hhmm'][2:]}",
                             fontsize=9, color=INK, loc="left")
            pdf.savefig(fig); plt.close(fig)


def notify(data):
    import push_notify
    import users
    from common import now_jst_naive
    db = BASE_DIR / "data" / "users.db"
    push_notify.init(db, now_jst_naive)
    conn = users.connect(db)
    try:
        ids = users.push_subscriber_ids(conn)
    finally:
        conn.close()
    sent = 0
    perf = data["performance"].get("全体", {})
    body = f"川霧を観測した日 {data['fog_days_any']}日。多かった地点: " + "・".join(t["station"] for t in data["top_stations"][:3])
    if perf.get("recall") is not None:
        body += f"。予測の再現率 {100 * perf['recall']:.0f}%"
    for uid in ids:
        sent += push_notify.deliver(uid, "report", f"{data['label']}の川霧レポート", body, "/reports", "monthly-report",
                                    key=f"report:{data['month']}", ttl=3 * 86400)
    return sent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("month", nargs="?")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()
    if args.month:
        month = args.month
    else:
        first = datetime.now(JST).date().replace(day=1)
        month = (first - timedelta(days=1)).strftime("%Y-%m")
    data = collect(month)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = OUT_DIR / f"{month}.pdf"
    render_pdf(data, pdf_path.with_suffix(".part.pdf"))
    pdf_path.with_suffix(".part.pdf").replace(pdf_path)
    (OUT_DIR / f"{month}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("Wrote %s (%d fog days)", pdf_path, data["fog_days_any"])
    if not args.no_push:
        log.info("Notified %d device(s)", notify(data))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    main()
