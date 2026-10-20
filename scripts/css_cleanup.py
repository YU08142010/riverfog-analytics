"""CSSの整理(上書きされて効かない宣言・どこにも使われていない規則を消す)。結果は /tmp/cssclean/ に書く(本番は変えない)。
2026-10-20に使った(style.css 84→77KB、glass.css 144→134KB)。tinycss2が要るので、プロジェクトのvenvとは別に入れて動かす:
  python3 -m venv /tmp/csstool && /tmp/csstool/bin/pip install tinycss2 && /tmp/csstool/bin/python scripts/css_cleanup.py
消した後は、全ページ・ライト/ダーク・PC/スマホ幅で全要素の計算済みスタイルが変わらないことを確かめてから置き換えること
(2026-10-20は31ページ×4通りで確認。違いは天気の背景のJSが動きに合わせて入れる --wx-mx/--wx-my だけだった)。
消す条件
  A) 宣言: 同じ文脈(@media等)・同じセレクタ・同じプロパティの宣言が後の別の規則にあり、そちらが必ず勝つ
     (後の方が!important、または前の方が!importantでない)。後の規則が新しい書き方(:has()等)なら、対応していない
     ブラウザでは後の規則ごと無視されて前が効くので、数えない。後の値が新しい関数(color-mix等)の場合も同じ理由で数えない。
     上書きする側は、同じファイルか、必ず後に読まれるファイル(style→glass→theme)。weather_scene.cssは自分の中だけ。
  B) 規則: セレクタのクラス名・idが、テンプレート・JS・Pythonのどこにも出てこない(動的に作る接頭辞・ライブラリのものは除く)。
"""
import glob, json, os, re, sys
import tinycss2

ROOT = "/opt/river-fog"
OUT = "/tmp/cssclean"
FILES = ["web_static/style.css", "web_static/glass.css", "web_static/theme.css", "web_static/weather_scene.css"]
LATER_OK = {"web_static/style.css": {"web_static/style.css", "web_static/glass.css", "web_static/theme.css"},
            "web_static/glass.css": {"web_static/glass.css", "web_static/theme.css"},
            "web_static/theme.css": {"web_static/theme.css"},
            "web_static/weather_scene.css": {"web_static/weather_scene.css"}}
SAFE_FUNCS = {"rgba", "rgb", "hsl", "hsla", "var", "url", "calc", "linear-gradient", "radial-gradient", "blur", "saturate", "translate",
              "translatey", "translatex", "scale", "rotate", "cubic-bezier", "repeat", "minmax", "format", "attr", "counter", "brightness",
              "contrast", "drop-shadow", "steps", "inset", "translate3d", "matrix"}
RISKY_SEL = re.compile(r":(has|is|where|focus-visible|user-invalid|user-valid|popover-open|modal)\b|::?-(webkit|moz|ms)-|:not\([^)]*[ ,>+~]")
DYNAMIC_PREFIXES = ("wx-", "lv", "leaflet", "js-plotly", "plotly", "modebar", "main-svg", "rf-", "is-", "has-")

corpus = "\n".join(open(p, encoding="utf-8", errors="ignore").read()
                   for pat in ["web_templates/**/*.html", "web_static/*.js", "*.py"]
                   for p in glob.glob(os.path.join(ROOT, pat), recursive=True) if "/vendor/" not in p)
_used = {}


def token_used(tok):
    if tok not in _used:
        u = re.search(r"(?<![\w-])" + re.escape(tok) + r"(?![\w-])", corpus) is not None
        if not u:
            parts = tok.split("-")
            for i in range(len(parts) - 1, 0, -1):
                pre = "-".join(parts[:i]) + "-"
                if len(pre) >= 3 and pre in corpus:
                    u = True; break
        _used[tok] = u
    return _used[tok]


def sel_tokens(sel):
    s = re.sub(r"::?[\w-]+(\([^)]*\))?", " ", sel)
    s = re.sub(r"\[[^\]]*\]", "", s)
    return re.findall(r"[.#]([A-Za-z_][\w-]*)", s)


def selector_dead(sel):
    toks = sel_tokens(sel)
    if not toks or any(t.startswith(DYNAMIC_PREFIXES) for t in toks):
        return False
    return not all(token_used(t) for t in toks)


def split_selectors(sel):
    parts, depth, cur = [], 0, ""
    for ch in sel:
        depth += ch in "(["; depth -= ch in ")]"
        if ch == "," and depth == 0:
            parts.append(cur.strip()); cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return parts


def line_offsets(text):
    offs, pos = [0], 0
    for line in text.split("\n"):
        pos += len(line) + 1; offs.append(pos)
    return offs


def flatten(nodes, ctx, out, fname):
    for r in nodes:
        if r.type == "qualified-rule":
            sel = re.sub(r"\s+", " ", tinycss2.serialize(r.prelude).strip())
            decls = [d for d in tinycss2.parse_declaration_list(r.content, skip_whitespace=True, skip_comments=True) if d.type == "declaration"]
            out.append({"file": fname, "ctx": ctx, "sel": sel, "sels": split_selectors(sel), "node": r, "decls": decls})
        elif r.type == "at-rule" and r.lower_at_keyword in ("media", "supports", "container", "layer") and r.content is not None:
            c = ctx + ("@" + r.lower_at_keyword + " " + re.sub(r"\s+", " ", tinycss2.serialize(r.prelude).strip()),)
            flatten(tinycss2.parse_rule_list(r.content, skip_whitespace=True, skip_comments=True), c, out, fname)


def safe_value(decl):
    v = tinycss2.serialize(decl.value)
    return set(f.lower() for f in re.findall(r"([A-Za-z-]+)\(", v)) <= SAFE_FUNCS


texts = {f: open(os.path.join(ROOT, f), encoding="utf-8").read() for f in FILES}
rules = []
for f in FILES:
    flatten(tinycss2.parse_stylesheet(texts[f], skip_whitespace=True, skip_comments=True), (), rules, f)

remove_decls, remove_rules = [], []
for i, r in enumerate(rules):
    if all(selector_dead(s) for s in r["sels"]):
        remove_rules.append(r); continue
    for d in r["decls"]:
        ok_all = True
        for s in r["sels"]:
            hit = False
            for r2 in rules[i + 1:]:
                if r2["file"] not in LATER_OK[r["file"]] or r2["ctx"] != r["ctx"] or s not in r2["sels"] or RISKY_SEL.search(r2["sel"]):
                    continue
                if any(d2.lower_name == d.lower_name and (d2.important or not d.important) and safe_value(d2) for d2 in r2["decls"]):
                    hit = True; break
            if not hit:
                ok_all = False; break
        if ok_all:
            remove_decls.append((r, d))

# 規則のすべての宣言が消えるなら、規則ごと消す
by_rule = {}
for r, d in remove_decls:
    by_rule.setdefault(id(r), (r, []))[1].append(d)
edits = {f: [] for f in FILES}  # (start, end) を消す
for r in remove_rules:
    edits[r["file"]].append(("rule", r))
for rid, (r, ds) in by_rule.items():
    if len(ds) == len(r["decls"]) and r not in remove_rules:
        edits[r["file"]].append(("rule", r))
    elif r not in remove_rules:
        for d in ds:
            edits[r["file"]].append(("decl", r, d))


def block_end(text, start):
    """startからの最初の { に対応する } の直後の位置。"""
    depth, i, in_str, q = 0, start, False, ""
    while i < len(text):
        ch = text[i]
        if text.startswith("/*", i):
            i = text.index("*/", i) + 2; continue
        if in_str:
            if ch == "\\":
                i += 2; continue
            if ch == q:
                in_str = False
        elif ch in "\"'":
            in_str, q = True, ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced")


def decl_end(text, start):
    depth, i, in_str, q = 0, start, False, ""
    while i < len(text):
        ch = text[i]
        if text.startswith("/*", i) and not in_str:
            i = text.index("*/", i) + 2; continue
        if in_str:
            if ch == "\\":
                i += 2; continue
            if ch == q:
                in_str = False
        elif ch in "\"'":
            in_str, q = True, ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == ";" and depth == 0:
            return i + 1
        elif ch == "}" and depth == 0:
            return i
        i += 1
    raise ValueError("no end")


stats = {}
os.makedirs(OUT, exist_ok=True)
for f in FILES:
    text = texts[f]
    offs = line_offsets(text)
    spans = []
    for e in edits[f]:
        if e[0] == "rule":
            r = e[1]["node"]
            s = offs[r.source_line - 1] + r.source_column - 1
            spans.append((s, block_end(text, s), "rule", e[1]["sel"]))
        else:
            d = e[2]
            s = offs[d.source_line - 1] + d.source_column - 1
            spans.append((s, decl_end(text, s), "decl", e[1]["sel"] + " { " + d.lower_name))
    spans.sort()
    # 重なり(規則の中の宣言)を除く
    merged = []
    for sp in spans:
        if merged and sp[0] < merged[-1][1]:
            continue
        merged.append(sp)
    new = text
    for s, e, kind, label in reversed(merged):
        # 消した後、その行が空白だけになるなら行ごと消す
        ls = new.rfind("\n", 0, s) + 1
        le = new.find("\n", e); le = len(new) if le == -1 else le
        if new[ls:s].strip() == "" and new[e:le].strip() == "":
            new = new[:ls] + new[le + 1:]
        else:
            # 宣言の前の空白も詰める
            while s > 0 and new[s - 1] in " \t" and kind == "decl":
                s -= 1
            new = new[:s] + new[e:]
    # 中身が空になった規則 { } を消す(宣言だけ消して空になったもの)
    new = re.sub(r"\n[^\n{}]*\{\s*\}[ \t]*(?=\n)", "", new)
    out_path = os.path.join(OUT, os.path.basename(f))
    open(out_path, "w", encoding="utf-8").write(new)
    # 消した後も構文として読めるか
    tinycss2.parse_stylesheet(new)
    stats[f] = {"before": len(text.encode()), "after": len(new.encode()),
                "rules_removed": sum(1 for x in merged if x[2] == "rule"), "decls_removed": sum(1 for x in merged if x[2] == "decl"),
                "removed_rules": [x[3][:90] for x in merged if x[2] == "rule"]}
print(json.dumps(stats, ensure_ascii=False, indent=1))
