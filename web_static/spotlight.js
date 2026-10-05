/* Spotlight風の検索(2026-10-05)。Ctrl+K / ⌘K、ヘッダーの検索ボタン、または入力欄の外で「/」を
   押すと、画面中央にすりガラスの検索窓を開く。候補はbase.htmlの#spotlight-data(webapp._spotlight_items)。
   - 地点・ページ・管理画面を、先頭一致 > 部分一致 > 文字の飛び飛び一致の順で並べる。
   - 日付(2026-08-27 / 20260827 / 8/27)を入れると、その日の観測データ・カメラ画像・予測結果を出す。
   - ↑↓で選択、Enterで移動、Escで閉じる。最近開いた候補はこのブラウザに保存する(読めなくても動く)。 */
(function() {
    "use strict";
    var dataEl = document.getElementById("spotlight-data");
    if (!dataEl) { return; }
    var data;
    try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }
    var RECENT_KEY = "spotlight-recent";
    var KIND_LABEL = { station: "地点", page: "ページ", admin: "管理", date: "日付", recent: "最近" };
    var KIND_ICON = {
        station: '<path d="M12 21s-6-5.3-6-10a6 6 0 1 1 12 0c0 4.7-6 10-6 10z"/><circle cx="12" cy="11" r="2.2"/>',
        page: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 9h8M8 13h8M8 17h5"/>',
        admin: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
        date: '<rect x="3.5" y="5" width="17" height="15" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4"/>'
    };

    var overlay = document.createElement("div");
    overlay.className = "spotlight-overlay";
    overlay.hidden = true;
    overlay.innerHTML =
        '<div class="spotlight" role="dialog" aria-modal="true" aria-label="検索">' +
        '  <div class="spotlight-bar">' +
        '    <svg class="spotlight-glass" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>' +
        '    <input type="text" class="spotlight-input" placeholder="地点・ページ・日付を検索" aria-label="地点・ページ・日付を検索" autocomplete="off" spellcheck="false">' +
        '    <kbd class="spotlight-esc">esc</kbd>' +
        '  </div>' +
        '  <ul class="spotlight-results" role="listbox"></ul>' +
        '  <div class="spotlight-foot"><span><kbd>↑</kbd><kbd>↓</kbd> 選択</span><span><kbd>Enter</kbd> 開く</span><span><kbd>Ctrl</kbd>+<kbd>K</kbd> でいつでも検索</span></div>' +
        '</div>';
    document.body.appendChild(overlay);
    var input = overlay.querySelector(".spotlight-input");
    var list = overlay.querySelector(".spotlight-results");
    var current = [], active = 0, lastFocus = null;

    function norm(s) {
        // カタカナ→ひらがな、全角英数→半角、小文字化
        return (s || "").toLowerCase()
            .replace(/[ァ-ヶ]/g, function(c) { return String.fromCharCode(c.charCodeAt(0) - 0x60); })
            .replace(/[！-～]/g, function(c) { return String.fromCharCode(c.charCodeAt(0) - 0xfee0); })
            .replace(/\s+/g, "");
    }
    function score(item, q) {
        var label = norm(item.label), kw = norm(item.keywords);
        if (label === q) { return 100; }
        if (label.indexOf(q) === 0) { return 80; }
        if (label.indexOf(q) >= 0) { return 60; }
        if (kw.indexOf(q) >= 0) { return 40; }
        var i = 0;
        for (var k = 0; k < label.length && i < q.length; k++) { if (label[k] === q[i]) { i++; } }
        return i === q.length ? 20 : 0;
    }
    function parseDate(q) {
        var m = q.match(/^(\d{4})[-\/.]?(\d{1,2})[-\/.]?(\d{1,2})$/);
        var y, mo, d;
        if (m) { y = +m[1]; mo = +m[2]; d = +m[3]; }
        else {
            m = q.match(/^(\d{1,2})[\/.月-](\d{1,2})日?$/);
            if (!m) { return null; }
            y = new Date().getFullYear(); mo = +m[1]; d = +m[2];
        }
        if (mo < 1 || mo > 12 || d < 1 || d > 31) { return null; }
        return y + "-" + (mo < 10 ? "0" : "") + mo + "-" + (d < 10 ? "0" : "") + d;
    }
    function recent() {
        try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); } catch (e) { return []; }
    }
    function remember(item) {
        try {
            var r = recent().filter(function(x) { return x.href !== item.href; });
            r.unshift({ label: item.label, href: item.href, kind: item.kind === "recent" ? item.origKind : item.kind });
            localStorage.setItem(RECENT_KEY, JSON.stringify(r.slice(0, 6)));
        } catch (e) {}
    }

    function search(raw) {
        var q = norm(raw);
        if (!q) {
            var r = recent().map(function(x) { return { label: x.label, href: x.href, kind: "recent", origKind: x.kind }; });
            var defaults = data.items.filter(function(x) { return x.kind === "page"; }).slice(0, 8 - Math.min(r.length, 4));
            return r.slice(0, 4).concat(defaults);
        }
        var out = [];
        var date = parseDate(raw.trim());
        if (date) {
            data.dates.forEach(function(d) { out.push({ label: date + " の" + d.label, href: d.href.replace("0000-00-00", date), kind: "date", s: 200 }); });
        }
        data.items.forEach(function(item) {
            var s = score(item, q);
            if (s) { out.push({ label: item.label, href: item.href, kind: item.kind, s: s + (item.kind === "station" ? 3 : 0) }); }
        });
        out.sort(function(a, b) { return b.s - a.s; });
        return out.slice(0, 10);
    }

    function render() {
        current = search(input.value);
        active = Math.min(active, Math.max(0, current.length - 1));
        list.textContent = "";
        if (!current.length) {
            var empty = document.createElement("li");
            empty.className = "spotlight-empty";
            empty.textContent = "見つかりませんでした";
            list.appendChild(empty);
            return;
        }
        current.forEach(function(item, i) {
            var li = document.createElement("li");
            li.className = "spotlight-item" + (i === active ? " active" : "");
            li.setAttribute("role", "option");
            li.setAttribute("aria-selected", i === active ? "true" : "false");
            var kind = item.kind === "recent" ? (item.origKind || "page") : item.kind;
            var icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
            icon.setAttribute("viewBox", "0 0 24 24");
            icon.setAttribute("class", "spotlight-icon spotlight-icon-" + kind);
            icon.innerHTML = KIND_ICON[kind] || KIND_ICON.page;
            var label = document.createElement("span");
            label.className = "spotlight-label";
            label.textContent = item.label;
            var tag = document.createElement("span");
            tag.className = "spotlight-kind";
            tag.textContent = KIND_LABEL[item.kind] || "";
            li.appendChild(icon); li.appendChild(label); li.appendChild(tag);
            li.addEventListener("mousemove", function() { if (active !== i) { active = i; highlight(); } });
            li.addEventListener("click", function() { go(i); });
            list.appendChild(li);
        });
    }
    function highlight() {
        Array.prototype.forEach.call(list.children, function(li, i) {
            li.classList.toggle("active", i === active);
            li.setAttribute("aria-selected", i === active ? "true" : "false");
        });
        var el = list.children[active];
        if (el && el.scrollIntoView) { el.scrollIntoView({ block: "nearest" }); }
    }
    function go(i) {
        var item = current[i];
        if (!item) { return; }
        remember(item);
        window.location.href = item.href;
    }
    function open() {
        if (!overlay.hidden) { return; }
        lastFocus = document.activeElement;
        overlay.hidden = false;
        requestAnimationFrame(function() { overlay.classList.add("open"); });
        input.value = "";
        active = 0;
        render();
        input.focus();
    }
    function close() {
        if (overlay.hidden) { return; }
        overlay.classList.remove("open");
        setTimeout(function() { overlay.hidden = true; }, 180);
        if (lastFocus && lastFocus.focus) { lastFocus.focus(); }
    }
    window.openSpotlight = open;
    if (/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) {
        Array.prototype.forEach.call(document.querySelectorAll(".spotlight-trigger kbd"), function(k) { k.textContent = "⌘K"; });
        var hint = overlay.querySelector(".spotlight-foot span:last-child");
        if (hint) { hint.innerHTML = "<kbd>⌘</kbd>+<kbd>K</kbd> でいつでも検索"; }
    }

    input.addEventListener("input", function() { active = 0; render(); });
    input.addEventListener("keydown", function(e) {
        if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(current.length - 1, active + 1); highlight(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(0, active - 1); highlight(); }
        else if (e.key === "Enter") { e.preventDefault(); go(active); }
        else if (e.key === "Escape") { e.preventDefault(); close(); }
    });
    overlay.addEventListener("mousedown", function(e) { if (e.target === overlay) { close(); } });
    document.addEventListener("keydown", function(e) {
        var k = (e.key || "").toLowerCase();
        if ((e.ctrlKey || e.metaKey) && k === "k") { e.preventDefault(); if (overlay.hidden) { open(); } else { close(); } return; }
        if (k === "/" && overlay.hidden) {
            var t = e.target, tag = t && t.tagName;
            if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) { return; }
            e.preventDefault(); open();
        }
    });
    document.addEventListener("click", function(e) {
        if (e.target.closest && e.target.closest("[data-spotlight-open]")) { e.preventDefault(); open(); }
    });
})();
