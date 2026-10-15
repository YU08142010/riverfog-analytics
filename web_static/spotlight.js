/* Spotlight風の検索(2026-10-05、2026-10-07に拡張)。Ctrl+K / ⌘K、ヘッダーの検索ボタン、または入力欄の外で
   「/」を押すと、画面中央にすりガラスの検索窓を開く。候補はbase.htmlの#spotlight-data(webapp._spotlight_items)。
   - 地点・地点の公開ページ・ページ・管理画面・操作(テーマ切替・全画面・URLのコピー等)を、
     先頭一致 > 部分一致 > キーワード一致 > 文字の飛び飛び一致の順で並べ、一致した部分を強調する。
   - 先頭に「>」で操作だけ、「@」で地点だけ、「?」でショートカット一覧。
   - 日付(2026-08-27 / 20260827 / 8/27 / 10月6日 / 今日 / 昨日 / 3日前)で、その日の観測データ・カメラ画像・
     予測結果(データのある分)を出す。「9月」「2026-09」でその月のカレンダーを開く。
   - 「カレンダーから日付を選ぶ」で、データのある日に印の付いたカレンダーを出す(web_static/calendar.js)。
   - ↑↓/Tabで選択、Enterで開く、Ctrl+Enter(⌘+Enter)で新しいタブ、Escで戻る/閉じる。
   - 最近開いた候補はこのブラウザに保存する(読めなくても動く)。 */
(function() {
    "use strict";
    var dataEl = document.getElementById("spotlight-data");
    if (!dataEl) { return; }
    var data;
    try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }
    data.links = data.links || {};
    var RECENT_KEY = "spotlight-recent";
    var IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
    var MOD = IS_MAC ? "⌘" : "Ctrl";
    var KIND_LABEL = { station: "地点", public: "公開", page: "ページ", admin: "管理", date: "日付", recent: "最近",
                       action: "操作", quick: "すぐ開く", month: "カレンダー" };
    var KIND_ICON = {
        station: '<path d="M12 21s-6-5.3-6-10a6 6 0 1 1 12 0c0 4.7-6 10-6 10z"/><circle cx="12" cy="11" r="2.2"/>',
        public: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.6 3.6 5.4 3.6 8.5s-1.1 5.9-3.6 8.5c-2.5-2.6-3.6-5.4-3.6-8.5s1.1-5.9 3.6-8.5z"/>',
        page: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 9h8M8 13h8M8 17h5"/>',
        admin: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
        date: '<rect x="3.5" y="5" width="17" height="15" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
        month: '<rect x="3.5" y="5" width="17" height="15" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4M8 14h2M12 14h2M16 14h0M8 17h2M12 17h2"/>',
        action: '<path d="M13 3 5 14h6l-1 7 8-11h-6z"/>',
        quick: '<path d="M5 12h12M13 6l6 6-6 6"/>'
    };

    /* ---- 操作(Enterでその場で実行する候補) ---- */
    function clickEl(id) { var el = document.getElementById(id); if (el) { el.click(); } }
    function toast(msg, err) { if (window.showToast) { window.showToast(msg, err); } }
    var ACTIONS = [
        { id: "calendar", label: "カレンダーから日付を選ぶ", keywords: "日付 カレンダー 過去 アーカイブ かれんだー", run: function() { showCalendar(); }, keep: true },
        { id: "theme", label: "ダーク/ライトを切り替える", keywords: "テーマ ダークモード ライトモード 色 夜", run: function() { clickEl("theme-toggle-btn"); } },
        { id: "fullscreen", label: "全画面表示を切り替える", keywords: "フルスクリーン 全画面 大きく", when: function() { var w = document.getElementById("fs-wrap"); return w && !w.hidden; },
          run: function() { clickEl("fs-toggle-btn"); } },
        { id: "notif", label: "通知を開く", keywords: "お知らせ ベル 通知", when: function() { return !!document.getElementById("notif-bell-btn"); },
          run: function() { setTimeout(function() { clickEl("notif-bell-btn"); }, 60); } },
        { id: "copy", label: "このページのURLをコピー", keywords: "リンク 共有 コピー url", run: function() {
            var url = location.href;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(url).then(function() { toast("URLをコピーしました"); }, function() { toast("コピーできませんでした", true); });
            } else { toast("このブラウザではコピーできません", true); }
        } },
        { id: "top", label: "ページの先頭へ", keywords: "トップ 上 スクロール 先頭", run: function() { window.scrollTo({ top: 0, behavior: "smooth" }); } },
        { id: "bottom", label: "ページの末尾へ", keywords: "下 スクロール 最後 末尾", run: function() { window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" }); } },
        { id: "back", label: "前のページに戻る", keywords: "戻る バック 履歴", when: function() { return history.length > 1; }, run: function() { history.back(); } },
        { id: "reload", label: "ページを再読み込み", keywords: "更新 リロード 再読込", run: function() { location.reload(); } },
        { id: "print", label: "このページを印刷", keywords: "印刷 プリント pdf", run: function() { setTimeout(function() { window.print(); }, 200); } },
        { id: "help", label: "キーボードショートカット一覧", keywords: "ヘルプ キー 使い方 ショートカット ?", run: function() { showHelp(); }, keep: true },
        { id: "logout", label: "ログアウト", keywords: "サインアウト 終了", when: function() { return !!data.links.logout; }, href: function() { return data.links.logout; } }
    ];
    var ACTION_BY_ID = {};
    ACTIONS.forEach(function(a) { ACTION_BY_ID[a.id] = a; });

    /* 地点の公開ページは、地点の候補とURLの型からここで作る(全ページに埋め込む量を増やさないため) */
    if (data.links.public_station) {
        data.items.filter(function(x) { return x.kind === "station"; }).forEach(function(st) {
            data.items.push({ label: st.label, sub: "公開ページ", kind: "public", keywords: "公開 一般向け " + (st.keywords || ""),
                              href: data.links.public_station.replace("__NAME__", encodeURIComponent(st.label)) });
        });
    }

    /* ---- 日付の一覧(開いたときに1回だけ読む) ---- */
    var dates = null;
    function ensureDates() {
        if (dates || !window.RFCalendar) { return; }
        window.RFCalendar.loadDates().then(function(d) {
            dates = d;
            if (!overlay.hidden && mode === "search") { render(); }
        }, function() {});
    }
    function hasData(kind, date) { return !dates || (dates[kind] || []).indexOf(date) >= 0; }
    function latestOf(kind) { var a = dates && dates[kind]; return a && a.length ? a[a.length - 1] : null; }
    function dateHref(kind, date) {
        var d = data.dates.filter(function(x) { return x.kind === kind; })[0];
        return d ? d.href.replace("0000-00-00", date) : null;
    }
    function quickItems() {
        if (!dates) { return []; }
        var out = [];
        if (dates.latest_image && data.links.images_hour) {
            var li = dates.latest_image;
            out.push({ label: "最新のカメラ画像", sub: li.date.slice(5).replace("-", "/") + " " + li.hhmm.slice(0, 2) + ":" + li.hhmm.slice(2),
                       href: data.links.images_hour.replace("0000-00-00", li.date).replace("0000", li.hhmm), kind: "quick",
                       keywords: "画像 写真 カメラ 最新 いま 今" });
        }
        var obs = hasData("observations", dates.today) ? dates.today : latestOf("observations");
        if (obs && dateHref("observations", obs)) {
            out.push({ label: obs === dates.today ? "今日の観測データ" : "最新の観測データ", sub: obs, href: dateHref("observations", obs),
                       kind: "quick", keywords: "観測 今日 きょう 最新" });
        }
        var pred = latestOf("predictions");
        if (pred && dateHref("predictions", pred)) {
            out.push({ label: "最新の予測結果", sub: pred, href: dateHref("predictions", pred), kind: "quick", keywords: "予測 予報 最新 グラフ" });
        }
        return out;
    }

    /* ---- 画面 ---- */
    var overlay = document.createElement("div");
    overlay.className = "spotlight-overlay";
    overlay.hidden = true;
    overlay.innerHTML =
        '<div class="spotlight" role="dialog" aria-modal="true" aria-label="検索">' +
        '  <div class="spotlight-bar">' +
        '    <button type="button" class="spotlight-back" aria-label="検索に戻る" hidden>‹</button>' +
        '    <svg class="spotlight-glass" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>' +
        '    <input type="text" class="spotlight-input" placeholder="地点・ページ・日付・操作を検索" aria-label="地点・ページ・日付・操作を検索" autocomplete="off" spellcheck="false" role="combobox" aria-expanded="true" aria-autocomplete="list">' +
        '    <kbd class="spotlight-esc">esc</kbd>' +
        '  </div>' +
        '  <div class="spotlight-chips" role="toolbar" aria-label="絞り込み">' +
        '    <button type="button" data-prefix="">すべて</button><button type="button" data-prefix="@">地点</button>' +
        '    <button type="button" data-prefix=">">操作</button><button type="button" data-view="calendar">カレンダー</button>' +
        '  </div>' +
        '  <ul class="spotlight-results" role="listbox" id="spotlight-listbox"></ul>' +
        '  <div class="spotlight-panel" hidden></div>' +
        '  <div class="spotlight-foot"><span><kbd>↑</kbd><kbd>↓</kbd> 選択</span><span><kbd>Enter</kbd> 開く</span>' +
        '<span><kbd>' + MOD + '</kbd>+<kbd>Enter</kbd> 新しいタブ</span><span><kbd>&gt;</kbd> 操作 <kbd>@</kbd> 地点 <kbd>?</kbd> ヘルプ</span></div>' +
        '</div>';
    document.body.appendChild(overlay);
    var box = overlay.querySelector(".spotlight");
    var input = overlay.querySelector(".spotlight-input");
    var list = overlay.querySelector(".spotlight-results");
    var panel = overlay.querySelector(".spotlight-panel");
    var backBtn = overlay.querySelector(".spotlight-back");
    var chips = overlay.querySelector(".spotlight-chips");
    input.setAttribute("aria-controls", "spotlight-listbox");
    var current = [], active = 0, lastFocus = null, mode = "search", calendarCtl = null;

    /* ---- 検索 ---- */
    function normChar(s) {
        // カタカナ→ひらがな、全角英数→半角、小文字化(1文字ずつ。位置がずれないので強調表示に使える)
        return (s || "").toLowerCase()
            .replace(/[ァ-ヶ]/g, function(c) { return String.fromCharCode(c.charCodeAt(0) - 0x60); })
            .replace(/[！-～]/g, function(c) { return String.fromCharCode(c.charCodeAt(0) - 0xfee0); });
    }
    function norm(s) { return normChar(s).replace(/\s+/g, ""); }
    function score(item, q) {
        var label = norm(item.label), kw = norm((item.keywords || "") + " " + (item.sub || ""));
        if (label === q) { return 100; }
        if (label.indexOf(q) === 0) { return 80; }
        if (label.indexOf(q) >= 0) { return 60; }
        if (kw.indexOf(q) >= 0) { return 40; }
        var i = 0;
        for (var k = 0; k < label.length && i < q.length; k++) { if (label[k] === q[i]) { i++; } }
        return i === q.length ? 20 : 0;
    }
    /* 強調する文字の位置: 連続一致があればそこ、無ければ飛び飛び一致の各文字 */
    function matchPositions(label, q) {
        if (!q) { return []; }
        var nl = normChar(label), pos = [], at = nl.indexOf(q);
        if (at >= 0) { for (var i = 0; i < q.length; i++) { pos.push(at + i); } return pos; }
        var j = 0;
        for (var k = 0; k < nl.length && j < q.length; k++) { if (nl[k] === q[j]) { pos.push(k); j++; } }
        return j === q.length ? pos : [];
    }

    function pad(n) { return (n < 10 ? "0" : "") + n; }
    function ymd(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
    function todayDate() { return dates && dates.today ? new Date(dates.today + "T00:00:00") : new Date(); }
    function parseDate(raw) {
        var q = normChar(raw).replace(/\s+/g, "");
        var base = todayDate(), m, y, mo, d;
        if (/^(今日|きょう|today|本日|ほんじつ)$/.test(q)) { return ymd(base); }
        if (/^(昨日|きのう|yesterday|さくじつ)$/.test(q)) { return ymd(new Date(base.getFullYear(), base.getMonth(), base.getDate() - 1)); }
        if (/^(一昨日|おととい|おとつい)$/.test(q)) { return ymd(new Date(base.getFullYear(), base.getMonth(), base.getDate() - 2)); }
        if ((m = q.match(/^(\d{1,3})日前$/))) { return ymd(new Date(base.getFullYear(), base.getMonth(), base.getDate() - (+m[1]))); }
        if ((m = q.match(/^(\d{4})[-\/.年]?(\d{1,2})[-\/.月]?(\d{1,2})日?$/))) { y = +m[1]; mo = +m[2]; d = +m[3]; }
        else if ((m = q.match(/^(\d{1,2})[\/.月-](\d{1,2})日?$/))) { y = base.getFullYear(); mo = +m[1]; d = +m[2]; }
        else { return null; }
        if (mo < 1 || mo > 12 || d < 1 || d > 31) { return null; }
        var dt = new Date(y, mo - 1, d);
        if (dt.getMonth() !== mo - 1) { return null; }
        return ymd(dt);
    }
    function parseMonth(raw) {
        var q = normChar(raw).replace(/\s+/g, ""), m;
        if ((m = q.match(/^(\d{4})[-\/.年](\d{1,2})月?$/))) { return { y: +m[1], m: +m[2] }; }
        if ((m = q.match(/^(\d{1,2})月$/))) { return { y: todayDate().getFullYear(), m: +m[1] }; }
        if (/^(今月|こんげつ)$/.test(q)) { return { y: todayDate().getFullYear(), m: todayDate().getMonth() + 1 }; }
        if (/^(先月|せんげつ)$/.test(q)) { var b = todayDate(); var p = new Date(b.getFullYear(), b.getMonth() - 1, 1); return { y: p.getFullYear(), m: p.getMonth() + 1 }; }
        return null;
    }

    function recent() {
        try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); } catch (e) { return []; }
    }
    function remember(item) {
        try {
            var key = item.action || item.href;
            var r = recent().filter(function(x) { return (x.action || x.href) !== key; });
            r.unshift({ label: item.label, href: item.href || null, action: item.action || null, sub: item.sub || "",
                        kind: item.kind === "recent" ? item.origKind : item.kind });
            localStorage.setItem(RECENT_KEY, JSON.stringify(r.slice(0, 8)));
        } catch (e) {}
    }
    function actionItems() {
        return ACTIONS.filter(function(a) { return !a.when || a.when(); }).map(function(a) {
            return { label: a.label, kind: "action", action: a.id, keywords: a.keywords, href: a.href ? a.href() : null };
        });
    }
    function dateItems(date) {
        var out = [], wd = window.RFCalendar ? "(" + window.RFCalendar.weekday(date) + ")" : "";
        data.dates.forEach(function(d) {
            var ok = hasData(d.kind, date);
            out.push({ label: date + wd + " の" + d.label, href: d.href.replace("0000-00-00", date), kind: "date",
                       sub: ok ? "" : "データなし", disabled: !ok, s: ok ? 200 : 150 });
        });
        return out;
    }

    function search(raw) {
        var text = raw.trim(), prefix = "";
        if (/^[>@]/.test(text)) { prefix = text[0]; text = text.slice(1).trim(); }
        var q = norm(text);
        var pool;
        if (prefix === ">") { pool = actionItems(); }
        else if (prefix === "@") { pool = data.items.filter(function(x) { return x.kind === "station" || x.kind === "public"; }); }
        else { pool = data.items.concat(actionItems(), quickItems()); }

        if (!q) {
            if (prefix) { return pool.slice(0, 40).map(function(x) { return Object.assign({}, x, { q: "" }); }); }
            var r = recent().map(function(x) {
                return { label: x.label, href: x.href, action: x.action, sub: x.sub, kind: "recent", origKind: x.kind };
            }).filter(function(x) { return x.href || ACTION_BY_ID[x.action]; }).slice(0, 4);
            var out = [];
            if (r.length) { out.push({ header: "最近" }); out = out.concat(r); }
            // 最近見たページ(2026-10-13、base.htmlが覚える rf-visited)。検索から開いたものと、いま開いているページは除く
            var seenHref = {}; r.forEach(function(x) { if (x.href) { seenHref[x.href] = 1; } });
            var here = location.pathname + location.search, visited = [];
            try { visited = JSON.parse(localStorage.getItem("rf-visited") || "[]"); } catch (e) {}
            visited = visited.filter(function(v) { return v.href && v.href !== here && !seenHref[v.href]; }).slice(0, 5).map(function(v) {
                return { label: v.label, href: v.href, kind: "recent", origKind: /^\/stations\/[^/]+$/.test(v.href) ? "station" : "page", sub: "最近見た" };
            });
            if (visited.length) { out.push({ header: "最近見たページ" }); out = out.concat(visited); }
            var qi = quickItems();
            if (qi.length) { out.push({ header: "すぐ開く" }); out = out.concat(qi); }
            out.push({ header: "操作" });
            out = out.concat(actionItems().filter(function(a) { return ["calendar", "theme", "copy", "help"].indexOf(a.action) >= 0; }));
            out.push({ header: "ページ" });
            out = out.concat(data.items.filter(function(x) { return x.kind === "page"; }).slice(0, 6));
            return out;
        }
        var res = [];
        if (!prefix) {
            var date = parseDate(text);
            if (date) { res = res.concat(dateItems(date)); }
            var mon = parseMonth(text);
            if (mon && mon.m >= 1 && mon.m <= 12) {
                res.push({ label: mon.y + "年" + mon.m + "月のカレンダー", kind: "month", s: 190, month: mon.y + "-" + pad(mon.m) + "-01" });
            }
        }
        pool.forEach(function(item) {
            var s = score(item, q);
            if (s) { res.push(Object.assign({}, item, { s: s + (item.kind === "station" ? 3 : item.kind === "public" ? 1 : 0) })); }
        });
        res.sort(function(a, b) { return b.s - a.s; });
        return res.slice(0, 14).map(function(x) { return Object.assign(x, { q: q }); });
    }

    function selectable() { return current.filter(function(x) { return !x.header; }); }
    function render() {
        current = search(input.value);
        var sel = selectable();
        active = Math.min(active, Math.max(0, sel.length - 1));
        list.textContent = "";
        if (!sel.length) {
            var empty = document.createElement("li");
            empty.className = "spotlight-empty";
            empty.innerHTML = "見つかりませんでした<small>日付(10/6・昨日)、「&gt;」で操作、「@」で地点を探せます</small>";
            list.appendChild(empty);
            input.removeAttribute("aria-activedescendant");
            return;
        }
        var n = 0;
        current.forEach(function(item) {
            var li = document.createElement("li");
            if (item.header) {
                li.className = "spotlight-header";
                li.setAttribute("role", "presentation");
                li.textContent = item.header;
                list.appendChild(li);
                return;
            }
            var i = n++;
            li.className = "spotlight-item" + (i === active ? " active" : "") + (item.disabled ? " is-disabled" : "");
            li.id = "spotlight-opt-" + i;
            li.setAttribute("role", "option");
            li.setAttribute("aria-selected", i === active ? "true" : "false");
            if (item.disabled) { li.setAttribute("aria-disabled", "true"); }
            var kind = item.kind === "recent" ? (item.origKind || "page") : item.kind;
            var icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
            icon.setAttribute("viewBox", "0 0 24 24");
            icon.setAttribute("class", "spotlight-icon spotlight-icon-" + kind);
            icon.setAttribute("aria-hidden", "true");
            icon.innerHTML = KIND_ICON[kind] || KIND_ICON.page;
            var label = document.createElement("span");
            label.className = "spotlight-label";
            var pos = matchPositions(item.label, item.q || "");
            if (pos.length) {
                for (var c = 0; c < item.label.length; c++) {
                    if (pos.indexOf(c) >= 0) {
                        var mk = document.createElement("mark");
                        mk.textContent = item.label[c];
                        label.appendChild(mk);
                    } else { label.appendChild(document.createTextNode(item.label[c])); }
                }
            } else { label.textContent = item.label; }
            if (item.sub) {
                var sub = document.createElement("small");
                sub.className = "spotlight-sub";
                sub.textContent = item.sub;
                label.appendChild(sub);
            }
            var tag = document.createElement("span");
            tag.className = "spotlight-kind";
            tag.textContent = KIND_LABEL[item.kind] || "";
            li.appendChild(icon); li.appendChild(label); li.appendChild(tag);
            li.addEventListener("mousemove", function() { if (active !== i) { active = i; highlight(); } });
            li.addEventListener("click", function(e) { go(i, e.ctrlKey || e.metaKey); });
            li.addEventListener("auxclick", function(e) { if (e.button === 1) { e.preventDefault(); go(i, true); } });
            list.appendChild(li);
        });
        highlight();
    }
    function highlight() {
        var opts = list.querySelectorAll(".spotlight-item");
        Array.prototype.forEach.call(opts, function(li, i) {
            li.classList.toggle("active", i === active);
            li.setAttribute("aria-selected", i === active ? "true" : "false");
        });
        var el = opts[active];
        if (el) {
            input.setAttribute("aria-activedescendant", el.id);
            if (el.scrollIntoView) { el.scrollIntoView({ block: "nearest" }); }
        }
    }
    function go(i, newTab) {
        var item = selectable()[i];
        if (!item || item.disabled) { return; }
        if (item.kind === "month") { showCalendar(item.month); return; }
        var act = item.action ? ACTION_BY_ID[item.action] : null;
        if (act && act.run) {
            remember(item);
            if (!act.keep) { close(); }
            act.run();
            return;
        }
        if (!item.href) { return; }
        remember(item);
        if (newTab) { window.open(item.href, "_blank", "noopener"); close(); return; }
        openHref(item.href);
    }
    /* 全画面のときは全画面を保ったまま開く(web_static/fs_shell.js) */
    function openHref(href) {
        if (window.RFShell && window.RFShell.navigate(href)) { close(); return; }
        window.location.href = href;
    }

    /* ---- カレンダー・ヘルプの表示 ---- */
    function setMode(m) {
        mode = m;
        var inSearch = m === "search";
        list.hidden = !inSearch;
        panel.hidden = inSearch;
        backBtn.hidden = inSearch;
        box.classList.toggle("is-panel", !inSearch);
        Array.prototype.forEach.call(chips.querySelectorAll("button"), function(b) {
            var on = b.dataset.view ? b.dataset.view === m : (inSearch && (input.value.trim()[0] || "") === b.dataset.prefix ||
                     inSearch && b.dataset.prefix === "" && !/^[>@]/.test(input.value.trim()));
            b.classList.toggle("on", !!on);
        });
        if (inSearch && calendarCtl) { calendarCtl.destroy(); calendarCtl = null; }
    }
    function showCalendar(month) {
        if (!window.RFCalendar) { return; }
        setMode("calendar");
        panel.innerHTML = '<p class="spotlight-panel-note">データのある日を選ぶと、その日の観測データ・カメラ画像・予測結果を開けます。</p><div class="spotlight-cal"><p class="muted">読み込み中…</p></div>';
        var holder = panel.querySelector(".spotlight-cal");
        window.RFCalendar.loadDates().then(function(d) {
            dates = d;
            if (mode !== "calendar") { return; }
            holder.textContent = "";
            calendarCtl = window.RFCalendar.create(holder, {
                marks: window.RFCalendar.marksFrom(d), today: d.today, month: month, showDots: true,
                selected: null, label: "データのある日のカレンダー",
                onPick: function(date) { showDay(date); }
            });
            if (month) { calendarCtl.showMonth(month); }
            calendarCtl.focus();
        }, function() { holder.innerHTML = '<p class="muted">日付の一覧を読み込めませんでした。</p>'; });
    }
    function showDay(date) {
        var items = dateItems(date).filter(function(x) { return !x.disabled; });
        if (items.length === 1) { remember(items[0]); openHref(items[0].href); return; }
        setMode("day");
        var wd = window.RFCalendar ? "(" + window.RFCalendar.weekday(date) + ")" : "";
        panel.innerHTML = '<p class="spotlight-panel-title">' + date + wd + '</p><ul class="spotlight-daylist"></ul>' +
                          '<p class="spotlight-panel-note"><kbd>esc</kbd> でカレンダーに戻る</p>';
        var ul = panel.querySelector(".spotlight-daylist");
        items.forEach(function(item) {
            var li = document.createElement("li");
            var a = document.createElement("a");
            a.href = item.href;
            a.className = "spotlight-dayitem";
            a.textContent = item.label.replace(date + wd + " の", "");
            a.addEventListener("click", function() { remember(item); });
            li.appendChild(a);
            ul.appendChild(li);
        });
        panel.dataset.date = date;
        var first = ul.querySelector("a");
        if (first) { first.focus(); }
    }
    function showHelp() {
        setMode("help");
        var rows = [
            [MOD + " + K", "検索を開く・閉じる(入力欄の外では「/」でも開く)"],
            ["↑ ↓ / Tab", "候補を選ぶ"],
            ["Enter", "開く・実行する"],
            [MOD + " + Enter", "新しいタブで開く(中ボタンのクリックでも)"],
            ["Esc", "前の表示に戻る・閉じる"],
            ["> テーマ", "「>」で始めると操作だけを探す"],
            ["@ 赤岩", "「@」で始めると地点(と公開ページ)だけを探す"],
            ["10/6・昨日・3日前", "その日の観測データ・カメラ画像・予測結果"],
            ["9月・2026-09", "その月のカレンダーを開く"],
            ["カレンダー内", "←→↑↓で日、PageUp/PageDownで月を移動、Enterで決定"]
        ];
        panel.innerHTML = '<p class="spotlight-panel-title">キーボードショートカット</p><dl class="spotlight-help">' +
            rows.map(function(r) { return "<dt><kbd>" + r[0].replace(/ \+ /g, "</kbd>+<kbd>") + "</kbd></dt><dd>" + r[1] + "</dd>"; }).join("") + "</dl>";
    }
    function back() {
        if (mode === "day") { showCalendar(panel.dataset.date); return; }
        setMode("search");
        render();
        input.focus();
    }

    function open(prefill) {
        if (!overlay.hidden) { return; }
        lastFocus = document.activeElement;
        overlay.hidden = false;
        requestAnimationFrame(function() { overlay.classList.add("open"); });
        input.value = prefill || "";
        active = 0;
        setMode("search");
        render();
        ensureDates();
        input.focus();
        if (prefill === "?") { input.value = ""; showHelp(); }  // 「?」で開いたとき(読み込み前に押された場合も)
    }
    function close() {
        if (overlay.hidden) { return; }
        overlay.classList.remove("open");
        setTimeout(function() { overlay.hidden = true; setMode("search"); }, 180);
        if (lastFocus && lastFocus.focus) { lastFocus.focus(); }
    }
    window.openSpotlight = open;
    if (IS_MAC) {
        Array.prototype.forEach.call(document.querySelectorAll(".spotlight-trigger kbd"), function(k) { k.textContent = "⌘K"; });
    }

    input.addEventListener("input", function() {
        if (input.value.trim() === "?") { input.value = ""; showHelp(); return; }
        active = 0;
        setMode("search");
        render();
    });
    input.addEventListener("keydown", function(e) {
        var n = selectable().length;
        if (mode !== "search") {
            if (e.key === "Escape") { e.preventDefault(); back(); }
            return;
        }
        if (e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey)) { e.preventDefault(); active = n ? (active + 1) % n : 0; highlight(); }
        else if (e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey)) { e.preventDefault(); active = n ? (active - 1 + n) % n : 0; highlight(); }
        else if (e.key === "Enter") { e.preventDefault(); go(active, e.ctrlKey || e.metaKey); }
        else if (e.key === "Escape") { e.preventDefault(); if (input.value) { input.value = ""; active = 0; render(); } else { close(); } }
    });
    panel.addEventListener("keydown", function(e) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); back(); }
    });
    backBtn.addEventListener("click", back);
    chips.addEventListener("click", function(e) {
        var b = e.target.closest("button");
        if (!b) { return; }
        if (b.dataset.view === "calendar") { showCalendar(); return; }
        input.value = b.dataset.prefix ? b.dataset.prefix + " " : "";
        active = 0;
        setMode("search");
        render();
        input.focus();
    });
    overlay.addEventListener("mousedown", function(e) { if (e.target === overlay) { close(); } });
    document.addEventListener("keydown", function(e) {
        var k = (e.key || "").toLowerCase();
        if ((e.ctrlKey || e.metaKey) && k === "k") { e.preventDefault(); if (overlay.hidden) { open(); } else { close(); } return; }
        if ((k === "/" || k === "?") && overlay.hidden && !e.ctrlKey && !e.metaKey && !e.altKey) {
            var t = e.target, tag = t && t.tagName;
            if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) { return; }
            e.preventDefault();
            open();
            if (k === "?") { showHelp(); }
        }
    });
    document.addEventListener("click", function(e) {
        if (e.target.closest && e.target.closest("[data-spotlight-open]")) { e.preventDefault(); open(); }
    });
})();
