// 研究用サイトの全ページ共通の画面の動き(2026-10-20、base.htmlの中に書いていた約30KBのスクリプトを、ブラウザが
// キャッシュできるようにファイルに分けた)。サーバー側の値(CSRFトークン・通知など)は、base.htmlが直前に
// window.RF_BASE に入れて渡す。読み込む位置は以前と同じなので、実行の順番は変わらない。
// ヘッダーの実際の高さをCSS変数として公開する。ヘッダーは折り返し等で高さが変わり得るため
// (AIチャットボタン追加時など)、サイドバーやテーブル見出しのsticky位置を固定値ではなく
// これに追従させることで、ヘッダーが高くなった際に見出しがずれる問題を防ぐ。
(function() {
    var header = document.querySelector("header");
    if (!header) { return; }
    function updateHeaderHeight() {
        document.documentElement.style.setProperty("--header-h", header.offsetHeight + "px");
    }
    updateHeaderHeight();
    window.addEventListener("resize", updateHeaderHeight);
    if (window.ResizeObserver) {
        new ResizeObserver(updateHeaderHeight).observe(header);
    }
})();

// スマホのスワイプ「戻る」等でページがbfcacheから復元されると、装備中バッジや
// 未読バッジのようにJSがその場でDOMを書き換えた状態がそのまま(サーバーの最新状態と
// 無関係に)復元されてしまい、見た目が古いままになることがある。event.persistedが
// trueの時だけ強制的に再読み込みし、常にサーバーの最新状態を表示するようにする。
window.addEventListener("pageshow", function(e) {
    if (e.persisted) { window.location.reload(); }
});

// 長い表・一覧の「もっと見る」の開閉(2026-10-23): ページごとに別々のJSを書くと操作感がページで
// ばらつくため、どのページでも使える共通の仕組みにした。
// <button class="expand-toggle" data-expand-target="対象のid" data-more-label="…" data-less-label="…">
// + hiddenを付けた対象の要素、の組み合わせだけで動く(新しいJSを書く必要はない)。
// data-more-label・data-less-labelはtextContentで差し込む(HTMLとしては解釈しない)。
// 今のところ呼び出し側は固定文字列しか渡していないが、将来どこかがここに利用者由来の
// 文字列(メッセージ本文等)を渡した場合に備え、innerHTMLではなくtextContentにして
// XSSの経路にならないようにしてある(2026-10-23の監査で指摘)。三角の記号は文字実体参照
// (&#9660;等)ではなくUnicode文字そのものを渡すこと。
document.addEventListener("click", function(e) {
    var btn = e.target.closest(".expand-toggle");
    if (!btn) { return; }
    var target = document.getElementById(btn.dataset.expandTarget);
    if (!target) { return; }
    var wasHidden = target.hidden;
    target.hidden = !wasHidden;
    btn.textContent = wasHidden ? btn.dataset.lessLabel : btn.dataset.moreLabel;
    btn.setAttribute("aria-expanded", String(wasHidden));
});

// ホーム画面のカードの折りたたみ(2026-10-23): 項目が多すぎて分かりにくいとの指摘を受け、
// カードの見出し(h2)にボタンを付けて中身を折りたためるようにした。開閉はこの端末のブラウザに
// 覚える(カードごとのid、dashboard.htmlのdata-card)。表示/非表示・並びは別の仕組み(設定画面の
// 「ホーム画面」、サーバー側)で扱うので、ここはあくまで「一時的にしまう」だけ。
(function() {
    var slots = document.querySelectorAll(".dash-card-slot[data-card]");
    if (!slots.length) { return; }
    var KEY = "rf-dash-collapsed", state = {};
    try { state = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) {}
    slots.forEach(function(slot) {
        var card = slot.querySelector(":scope > .card");
        var h2 = card && card.querySelector(":scope > h2");
        if (!card || !h2) { return; }
        var id = slot.dataset.card;
        var body = document.createElement("div");
        body.className = "dash-card-body";
        var node = h2.nextSibling;
        while (node) { var next = node.nextSibling; body.appendChild(node); node = next; }
        card.appendChild(body);
        var btn = document.createElement("button");
        btn.type = "button"; btn.className = "dash-card-collapse";
        btn.setAttribute("aria-label", "折りたたむ/展開する");
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
        h2.appendChild(btn);
        function apply(collapsed) {
            body.hidden = collapsed;
            btn.setAttribute("aria-expanded", String(!collapsed));
            card.classList.toggle("is-collapsed", collapsed);
        }
        apply(!!state[id]);
        btn.addEventListener("click", function() {
            state[id] = !state[id];
            apply(state[id]);
            try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
        });
    });
})();

// ヘッダーのボタンで変えたテーマ・文字の大きさを、ログイン中ならサーバーにも保存する(他の端末にも反映)。
window.rfSaveQuickSetting = function(key, value) {
    if (!window.RF_BASE.loggedIn) { return; }
    var fd = new FormData();
    fd.append("key", key); fd.append("value", value); fd.append("csrf_token", window.RF_BASE.csrfToken);
    fetch(window.RF_BASE.quickSettingUrl, {method: "POST", body: fd, headers: {"X-Requested-With": "XMLHttpRequest"}})
        .catch(function() {});
};

(function() {
    var btn = document.getElementById("text-size-btn");
    if (!btn) { return; }
    var root = document.documentElement;
    function sync() {
        var on = root.classList.contains("rf-text-large") || root.classList.contains("rf-text-xlarge");
        btn.setAttribute("aria-pressed", on ? "true" : "false");
        btn.setAttribute("aria-label", on ? "文字を標準の大きさに戻す" : "文字を大きくする");
    }
    btn.addEventListener("click", function() {
        var on = !(root.classList.contains("rf-text-large") || root.classList.contains("rf-text-xlarge"));
        root.classList.remove("rf-text-xlarge");
        root.classList.toggle("rf-text-large", on);
        try { localStorage.setItem("rf-text", on ? "large" : "normal"); } catch (e) {}
        window.rfSaveQuickSetting("text_size", on ? "large" : "normal");
        sync();
        window.dispatchEvent(new Event("resize"));  // ヘッダーのメニューの折り返し(その他▾)を測り直させる
        if (window.showToast) { window.showToast(on ? "文字を大きめにしました(特大は設定画面で選べます)" : "文字を標準の大きさに戻しました"); }
    });
    sync();
})();

document.getElementById("theme-toggle-btn").addEventListener("click", function() {
    var current = document.documentElement.getAttribute("data-theme");
    if (!current) {
        current = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    var next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("theme", next); } catch (e) {}
    window.rfSaveQuickSetting("theme", next);
    // サーバー側で描画する分析ページ等のグラフPNGもテーマに合わせられるように、
    // クッキーにも書いておく(反映されるのは次にそのページを開いた時から)。
    document.cookie = "theme=" + next + "; path=/; max-age=31536000; samesite=lax";
    // Plotlyグラフ(.chart-themed)は上のクッキーと違い、サーバー往復なしでその場で
    // 配色を変える。画面上に既にマウント済みの全グラフ(window.PLOTLY_CHARTS)に対し、
    // applyChartThemeでrelayout/restyleする。
    (window.PLOTLY_CHARTS || []).forEach(function(el) {
        if (document.body.contains(el)) { applyChartTheme(el, next); }
    });
});

// 全画面表示のボタンと、全画面のままページを移る仕組みは web_static/fs_shell.js(公開ページと共通)。

// メニュー(2026-10-12)。PC: 左のサイドバーを細くする/広げる(この端末に覚える)。狭い画面: 下の「メニュー」でシートを開く。
(function() {
    var root = document.documentElement;
    var collapse = document.getElementById("app-side-collapse");
    function syncCollapse() {
        var on = root.classList.contains("rf-side-collapsed");
        collapse.setAttribute("aria-label", on ? "メニューを広げる" : "メニューを細くする");
        collapse.querySelector(".app-nav-label").textContent = on ? "メニューを広げる" : "メニューを細くする";
    }
    if (collapse) {
        collapse.addEventListener("click", function() {
            var on = root.classList.toggle("rf-side-collapsed");
            try { localStorage.setItem("rf-side-collapsed", on ? "1" : "0"); } catch (e) {}
            syncCollapse();
            window.dispatchEvent(new Event("resize"));  // 地図・グラフの幅を測り直させる
        });
        syncCollapse();
    }
    // メニューを閉じる/開く(PC、2026-10-12)。閉じるとサイドバーを画面の外に出し、本文を広く使う。この端末に覚える。
    function toggler(btn, cls, key, labels, after) {
        if (!btn) { return; }
        function sync() {
            var hidden = root.classList.contains(cls);
            btn.setAttribute("aria-expanded", hidden ? "false" : "true");
            if (labels.aria) { btn.setAttribute("aria-label", hidden ? labels.show : labels.hide); }
            else { btn.textContent = hidden ? labels.show : labels.hide; }
        }
        btn.addEventListener("click", function() {
            var hidden = root.classList.toggle(cls);
            try { localStorage.setItem(key, hidden ? "1" : "0"); } catch (e) {}
            sync();
            setTimeout(function() { window.dispatchEvent(new Event("resize")); }, 250);
            if (after) { after(hidden); }
        });
        sync();
    }
    toggler(document.getElementById("app-side-toggle"), "rf-side-hidden", "rf-side-hidden", {aria: true, show: "メニューを開く", hide: "メニューを閉じる"});
    toggler(document.getElementById("sidebar-toggle"), "rf-stlist-hidden", "rf-stlist-hidden", (function(b) { var n = (b && b.getAttribute("data-list-name")) || "地点の一覧"; return {show: n + "を出す", hide: n + "を隠す"}; })(document.getElementById("sidebar-toggle")));
    var sheet = document.getElementById("app-sheet"), openBtn = document.getElementById("app-sheet-open");
    if (!sheet || !openBtn) { return; }
    var panel = sheet.querySelector(".app-sheet-panel");
    function setOpen(open) {
        if (open) {
            sheet.hidden = false;
            requestAnimationFrame(function() { sheet.classList.add("open"); });
            root.classList.add("rf-sheet-open");
            var cur = panel.querySelector(".app-nav-link.active") || panel;
            panel.focus();
            if (cur !== panel) { cur.scrollIntoView({block: "center"}); }
        } else {
            sheet.classList.remove("open");
            root.classList.remove("rf-sheet-open");
            setTimeout(function() { if (!sheet.classList.contains("open")) { sheet.hidden = true; } }, 220);
        }
        openBtn.setAttribute("aria-expanded", open ? "true" : "false");
    }
    openBtn.addEventListener("click", function() { setOpen(sheet.hidden); });
    panel.addEventListener("keydown", function(e) {
        if (e.key !== "Tab") { return; }
        var f = panel.querySelectorAll("a[href], button:not([disabled])");
        if (!f.length) { return; }
        var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && (document.activeElement === first || document.activeElement === panel)) { last.focus(); e.preventDefault(); }
        else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
    });
    sheet.addEventListener("click", function(e) { if (e.target.closest("[data-sheet-close]")) { setOpen(false); openBtn.focus(); } });
    document.addEventListener("keydown", function(e) { if (e.key === "Escape" && !sheet.hidden) { setOpen(false); openBtn.focus(); } });
    // 下に引いて閉じる
    var startY = null;
    panel.addEventListener("touchstart", function(e) { if (panel.querySelector(".app-sheet-body").scrollTop <= 0) { startY = e.touches[0].clientY; } }, {passive: true});
    panel.addEventListener("touchmove", function(e) {
        if (startY === null) { return; }
        var dy = Math.max(0, e.touches[0].clientY - startY);
        panel.style.transform = dy ? "translateY(" + dy + "px)" : "";
    }, {passive: true});
    panel.addEventListener("touchend", function(e) {
        if (startY === null) { return; }
        var dy = e.changedTouches[0].clientY - startY;
        startY = null; panel.style.transform = "";
        if (dy > 90) { setOpen(false); }
    });
})();
// 最近見たページ(2026-10-13): この端末に新しい順で12件まで覚える。検索(Ctrl+K)と地点ページの「最近見た地点」が使う。
(function() {
    if (/^\/(terms|login|logout)/.test(location.pathname)) { return; }
    try {
        var label = (document.title || "").split(" - ")[0].trim();
        if (!label) { return; }
        var href = location.pathname + location.search;
        var list = JSON.parse(localStorage.getItem("rf-visited") || "[]").filter(function(v) { return v.href !== href; });
        list.unshift({label: label, href: href, at: Date.now()});
        localStorage.setItem("rf-visited", JSON.stringify(list.slice(0, 12)));
    } catch (e) {}
})();
// 電波の弱い場所でも見られるようにする仕組み(/offline-sw.js)は2026-10-14にやめた。前に入ったブラウザに残っている登録を解除し、
// 保存したページ・画像を消す(通知用の /web_static/ の登録は残す)。
(function() {
    if (!("serviceWorker" in navigator)) { return; }
    navigator.serviceWorker.getRegistrations().then(function(regs) {
        regs.forEach(function(r) {
            var url = (r.active || r.waiting || r.installing || {}).scriptURL || "";
            if (url.indexOf("/offline-sw.js") >= 0) { r.unregister(); }
        });
    }).catch(function() {});
    try { if (window.caches) { ["rf-pages-v1", "rf-images-v1", "rf-assets-v1", "rf-assets-v2"].forEach(function(k) { caches.delete(k); }); } } catch (e) {}
})();
// トップに戻るボタン(一定量スクロールしたら表示)。
(function() {
    var btn = document.getElementById("back-to-top-btn");
    window.addEventListener("scroll", function() {
        btn.classList.toggle("show", window.scrollY > 600);
    }, {passive: true});
    btn.addEventListener("click", function() {
        window.scrollTo({top: 0, behavior: "smooth"});
    });
})();

// トースト。閉じるボタン付き、指定秒数後に自動で消える。
function showToast(message, isError) {
    var container = document.getElementById("toast-container");
    var toast = document.createElement("div");
    toast.className = "toast" + (isError ? " toast-error" : "");
    var msg = document.createElement("span");
    msg.className = "toast-message";
    msg.textContent = message;
    var closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "toast-close";
    closeBtn.setAttribute("aria-label", "閉じる");
    closeBtn.textContent = "×";
    var dismissTimer = null;
    function dismiss() {
        if (dismissTimer) { clearTimeout(dismissTimer); }
        toast.classList.remove("show");
        setTimeout(function() { toast.remove(); }, 300);
    }
    closeBtn.addEventListener("click", dismiss);
    toast.appendChild(msg);
    toast.appendChild(closeBtn);
    container.appendChild(toast);
    requestAnimationFrame(function() { toast.classList.add("show"); });
    dismissTimer = setTimeout(dismiss, 5000);
}

// confirm()の代わりのモーダル。confirmModal(message).then(function(ok) { ... })のように使う。
function confirmModal(message) {
    return new Promise(function(resolve) {
        var overlay = document.getElementById("modal-overlay");
        var msgEl = document.getElementById("modal-message");
        var okBtn = document.getElementById("modal-confirm-btn");
        var cancelBtn = document.getElementById("modal-cancel-btn");
        msgEl.textContent = message;
        overlay.hidden = false;
        function cleanup(result) {
            overlay.hidden = true;
            okBtn.removeEventListener("click", onOk);
            cancelBtn.removeEventListener("click", onCancel);
            overlay.removeEventListener("click", onOverlay);
            document.removeEventListener("keydown", onKey);
            resolve(result);
        }
        function onOk() { cleanup(true); }
        function onCancel() { cleanup(false); }
        function onOverlay(e) { if (e.target === overlay) { cleanup(false); } }
        function onKey(e) {
            if (e.key === "Escape") { cleanup(false); return; }
            if (e.key !== "Tab") { return; }
            // 2つのボタンだけの簡易フォーカストラップ(Tabでモーダルの外に出さない)。
            e.preventDefault();
            if (document.activeElement === okBtn) { cancelBtn.focus(); } else { okBtn.focus(); }
        }
        okBtn.addEventListener("click", onOk);
        cancelBtn.addEventListener("click", onCancel);
        overlay.addEventListener("click", onOverlay);
        document.addEventListener("keydown", onKey);
        okBtn.focus();
    });
}

// window.prompt()の代わりのモーダル。1行テキスト入力が必要な操作(フォルダ名の入力等)に使う。
// promptModal(message, defaultValue).then(function(value) { ... }) のように使い、
// キャンセル時はvalueがnullになる。confirmModalと同じオーバーレイ・ボタンを共有し、
// 入力欄だけ表示を切り替える。
function promptModal(message, defaultValue) {
    return new Promise(function(resolve) {
        var overlay = document.getElementById("modal-overlay");
        var msgEl = document.getElementById("modal-message");
        var input = document.getElementById("modal-prompt-input");
        var okBtn = document.getElementById("modal-confirm-btn");
        var cancelBtn = document.getElementById("modal-cancel-btn");
        msgEl.textContent = message;
        input.hidden = false;
        input.value = defaultValue || "";
        overlay.hidden = false;
        function cleanup(result) {
            overlay.hidden = true;
            input.hidden = true;
            okBtn.removeEventListener("click", onOk);
            cancelBtn.removeEventListener("click", onCancel);
            overlay.removeEventListener("click", onOverlay);
            input.removeEventListener("keydown", onInputKey);
            document.removeEventListener("keydown", onKey);
            resolve(result);
        }
        function onOk() { cleanup(input.value); }
        function onCancel() { cleanup(null); }
        function onOverlay(e) { if (e.target === overlay) { cleanup(null); } }
        function onInputKey(e) {
            if (e.key === "Enter") { e.preventDefault(); cleanup(input.value); }
        }
        function onKey(e) {
            if (e.key === "Escape") { cleanup(null); return; }
            if (e.key !== "Tab") { return; }
            e.preventDefault();
            if (document.activeElement === okBtn) { cancelBtn.focus(); } else { okBtn.focus(); }
        }
        okBtn.addEventListener("click", onOk);
        cancelBtn.addEventListener("click", onCancel);
        overlay.addEventListener("click", onOverlay);
        input.addEventListener("keydown", onInputKey);
        document.addEventListener("keydown", onKey);
        input.focus();
    });
}

// 汎用の右クリックメニュー。data-context-menu="menu要素id"を持つ要素上で右クリックすると、
// そのidのメニュー(hiddenな<div class="context-menu">)をクリック位置に表示する。
// メニュー内のbutton要素にはdata-context-actionを付け、"contextmenuaction"カスタム
// イベント(detail: {action, target})をdocumentに投げるので、各ページはそれを購読するだけでよい
// (共有ファイルページに限らず、右クリックメニューを使いたいページはこの仕組みに乗せられる)。
// 一部のトラックパッド(特にmacOSの2本指タップによる右クリック)では、contextmenu
// イベントの直後に余分な"click"イベントが合成発火することがあり、それが下の
// 「メニュー外クリックで閉じる」処理に拾われて、開いた直後のメニューが一瞬で
// 閉じてしまう不具合があった。メニューを開いた直後の短い猶予時間は、その
// "click"を外側クリックとして扱わないようにして回避する。
var _lastMenuShownAt = 0;
document.addEventListener("contextmenu", function(e) {
    var host = e.target.closest("[data-context-menu]");
    if (!host) { return; }
    var menu = document.getElementById(host.getAttribute("data-context-menu"));
    if (!menu) { return; }
    e.preventDefault();
    document.querySelectorAll(".context-menu").forEach(function(m) { m.hidden = true; });
    menu.dataset.targetId = host.dataset.contextTargetId || "";
    menu.style.left = e.pageX + "px";
    menu.style.top = e.pageY + "px";
    menu.hidden = false;
    _lastMenuShownAt = Date.now();
});
document.addEventListener("click", function(e) {
    if (e.target.closest(".context-menu")) { return; }
    if (Date.now() - _lastMenuShownAt < 300) { return; }
    document.querySelectorAll(".context-menu").forEach(function(m) { m.hidden = true; });
});
document.addEventListener("click", function(e) {
    var btn = e.target.closest(".context-menu [data-context-action]");
    if (!btn) { return; }
    var menu = btn.closest(".context-menu");
    menu.hidden = true;
    document.dispatchEvent(new CustomEvent("contextmenuaction", {
        detail: {action: btn.getAttribute("data-context-action"), targetId: menu.dataset.targetId},
    }));
});

// data-confirm="メッセージ" を持つform/buttonは、確認モーダルを通してから送信する
// (これまでonsubmit/onclickでbrowser標準confirm()を呼んでいた箇所の置き換え用)。
document.addEventListener("submit", function(e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-confirm")) { return; }
    if (form.dataset.confirmed === "1") { return; }
    e.preventDefault();
    confirmModal(form.getAttribute("data-confirm")).then(function(ok) {
        if (ok) {
            form.dataset.confirmed = "1";
            if (typeof form.requestSubmit === "function") { form.requestSubmit(e.submitter || undefined); }
            else { form.submit(); }
        }
    });
});
document.addEventListener("click", function(e) {
    var btn = e.target.closest("[data-confirm-click]");
    if (!btn) { return; }
    e.preventDefault();
    confirmModal(btn.getAttribute("data-confirm-click")).then(function(ok) {
        if (ok && btn.form) {
            btn.form.dataset.confirmed = "1";
            if (typeof btn.form.requestSubmit === "function") { btn.form.requestSubmit(btn); }
            else { btn.form.submit(); }
        } else if (ok) {
            btn.click();
        }
    });
});

// コピー用ボタン(data-copy="コピーする文字列")。
document.addEventListener("click", function(e) {
    var btn = e.target.closest(".copy-btn");
    if (!btn) { return; }
    var text = btn.getAttribute("data-copy") || "";
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function() {
            showToast("コピーしました。");
        }).catch(function() {
            showToast("コピーできませんでした。", true);
        });
    }
});

// テーブルをCSVとしてダウンロードするボタン(data-csv-export="対象tableのid"、
// data-csv-name="ファイル名(拡張子抜き)")。サーバー側にエクスポート用ルートを
// 個別に用意しなくても、画面に表示済みの表をそのままCSV化できる。
document.addEventListener("click", function(e) {
    var btn = e.target.closest("[data-csv-export]");
    if (!btn) { return; }
    var table = document.getElementById(btn.getAttribute("data-csv-export"));
    if (!table) { return; }
    var rows = Array.prototype.map.call(table.querySelectorAll("tr"), function(tr) {
        return Array.prototype.map.call(tr.querySelectorAll("th,td"), function(cell) {
            return '"' + cell.textContent.trim().replace(/"/g, '""') + '"';
        }).join(",");
    });
    var csv = "\uFEFF" + rows.join("\r\n");
    var blob = new Blob([csv], {type: "text/csv;charset=utf-8;"});
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = (btn.getAttribute("data-csv-name") || "export") + ".csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
});

// グラフ(2026-10-07〜): <div class="chart-themed" data-chart-url="..."> に、サーバーがmatplotlibで描いた画像
// (webapp._chart_as_png・mpl_render)を表示する。2026-09-14からのPlotly(拡大・移動できるグラフ)は、触ると
// 拡大・移動してしまい使いづらかったため戻した。グラフのURLは今までどおりfigureのJSONを返し、
// ?format=png&theme=&w=&scale=&notitle= を付けると画像になる。テーマを切り替えたとき・幅が大きく
// 変わったときは描き直す。カードの見出しと重なるグラフ自身のタイトルは出さない(notitle)。
window.PLOTLY_CHARTS = [];  // (名前は以前のまま)表示中のグラフ

function currentChartTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t === "dark" || t === "light") { return t; }
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function chartImageWidth(el) {
    var w = el.clientWidth || (el.parentNode && el.parentNode.clientWidth) || 800;
    return Math.max(320, Math.min(1800, Math.round(w / 40) * 40));
}

function chartImageSrc(el, theme) {
    var url = el.dataset.chartUrl;
    var card = el.closest(".card");
    var notitle = el.dataset.notitle === "1" ||
        (card && card.querySelector("h2, h3") && card.querySelectorAll(".chart-themed").length === 1);
    // いつも2倍の解像度で描く(表示倍率125%のPCなどで、等倍の画像を引き伸ばしてぼやけていたため、2026-10-09)
    var scale = 2;
    el.__rfW = chartImageWidth(el);
    return url + (url.indexOf("?") >= 0 ? "&" : "?") + "format=png&theme=" + theme + "&w=" + el.__rfW +
        "&scale=" + scale + "&notitle=" + (notitle ? 1 : 0);
}

function applyChartTheme(el, theme) {
    if (el.__rfImg) { el.__rfImg.src = chartImageSrc(el, theme); }
}

function mountPlotlyChart(el) {
    var url = el.dataset.chartUrl;
    if (!url) { return; }
    var wrap = el.closest(".chart-wrap");
    el.classList.add("loading");
    if (wrap) { wrap.classList.add("is-loading"); }
    var stop = function() {
        el.classList.remove("loading");
        if (wrap) { wrap.classList.remove("is-loading"); }
    };
    var img = document.createElement("img");
    img.className = "chart-img";
    img.alt = el.getAttribute("aria-label") || "グラフ";
    img.decoding = "async";
    img.addEventListener("load", function() { stop(); el.classList.add("is-ready"); });
    // 画像化(mpl_render)は同時に描ける枚数を絞っているため(サーバー負荷対策)、1ページに
    // グラフが複数あると一部が一時的なタイムアウト等で失敗することがある。1回だけ自動で
    // 再試行し、それでも失敗したときだけメッセージを出す(2026-10-26)。
    var retried = false;
    img.addEventListener("error", function() {
        if (!retried) {
            retried = true;
            setTimeout(function() { img.src = chartImageSrc(el, currentChartTheme()); }, 1500);
            return;
        }
        el.textContent = "グラフの読み込みに失敗しました";
        stop();
    });
    el.textContent = "";
    el.appendChild(img);
    el.__rfImg = img;
    img.src = chartImageSrc(el, currentChartTheme());
    window.PLOTLY_CHARTS.push(el);
}

// 画面の幅が大きく変わったら(回転・ウィンドウの大きさの変更)、その幅で描き直す
(function() {
    var timer = null;
    window.addEventListener("resize", function() {
        clearTimeout(timer);
        timer = setTimeout(function() {
            (window.PLOTLY_CHARTS || []).forEach(function(el) {
                if (!el.__rfImg || !document.body.contains(el)) { return; }
                var w = chartImageWidth(el);
                if (el.__rfW && Math.abs(w - el.__rfW) / el.__rfW > 0.15) { applyChartTheme(el, currentChartTheme()); }
            });
        }, 400);
    });
})();

document.addEventListener("DOMContentLoaded", function() {
    document.querySelectorAll(".chart-wrap img").forEach(function(img) {
        var wrap = img.closest(".chart-wrap");
        if (img.complete && img.naturalWidth > 0) { return; }
        img.classList.add("loading");
        if (wrap) { wrap.classList.add("is-loading"); }
        var stop = function() {
            img.classList.remove("loading");
            if (wrap) { wrap.classList.remove("is-loading"); }
        };
        img.addEventListener("load", stop);
        img.addEventListener("error", stop);
    });

    document.querySelectorAll(".chart-themed[data-chart-url]").forEach(function(el) {
        mountPlotlyChart(el);
    });

    // table.obs-tableのth文字列を各tdのdata-labelに自動反映する
    // (狭い画面でのカード表示用。style.cssの@media (max-width: 640px)参照)。
    document.querySelectorAll("table.obs-table").forEach(function(table) {
        var headers = Array.prototype.map.call(
            table.querySelectorAll("thead tr:last-child th"),
            function(th) { return th.textContent.trim(); }
        );
        if (!headers.length) { return; }
        table.querySelectorAll("tbody tr").forEach(function(tr) {
            Array.prototype.forEach.call(tr.children, function(td, i) {
                if (headers[i] && !td.hasAttribute("data-label")) { td.setAttribute("data-label", headers[i]); }
            });
        });
    });
});

// data-ajax属性を持つフォームは、ページ遷移せずfetchで送信しトーストで結果を表示する。
// サーバー側はX-Requested-Withヘッダの有無でJSON({message, success})かredirectかを切り替える。
document.addEventListener("submit", function(e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-ajax")) { return; }
    e.preventDefault();
    var submitter = e.submitter;
    var action = (submitter && submitter.getAttribute("formaction")) || form.action;
    var formData = new FormData(form);
    if (submitter && submitter.name) { formData.append(submitter.name, submitter.value); }
    // 送信中は二重送信防止のためボタン類を無効化する(完了/失敗どちらでも必ず戻す)。
    var controls = form.querySelectorAll("button, select, input");
    controls.forEach(function(el) { el.disabled = true; });
    fetch(action, {
        method: (form.method || "POST").toUpperCase(),
        body: formData,
        headers: {"X-Requested-With": "XMLHttpRequest"},
    })
    .then(function(resp) {
        return resp.json().then(function(data) { return {ok: resp.ok, data: data}; });
    })
    .then(function(result) {
        showToast(result.data.message || (result.ok ? "完了しました。" : "エラーが発生しました。"), !result.ok);
        if (result.ok) {
            document.dispatchEvent(new CustomEvent("ajaxformsuccess", {detail: {form: form, data: result.data}}));
            // data-ajax-reload: 表示中のページ自体の状態(サービス状態・メンテナンス設定等)が
            // 変わる操作は、トーストを一瞬見せてからページを再読み込みして最新状態を映す。
            if (form.hasAttribute("data-ajax-reload")) {
                var delay = parseInt(form.getAttribute("data-ajax-reload"), 10) || 900;
                setTimeout(function() { window.location.reload(); }, delay);
                return;
            }
        }
        controls.forEach(function(el) { el.disabled = false; });
    })
    .catch(function() {
        showToast("通信エラーが発生しました。", true);
        controls.forEach(function(el) { el.disabled = false; });
    });
});

// パスワード強度メーター(_pwStrength)はweb_static/shared.jsに共通化した。

// 保留中の登録申請があれば、管理者にはfaviconにも赤いバッジを出す(気づきやすくするため)。
(function() {
    var pending = window.RF_BASE.pendingSignups;
    if (!pending) { return; }
    var link = document.getElementById("favicon-link");
    var img = new Image();
    img.onload = function() {
        var canvas = document.createElement("canvas");
        canvas.width = 32; canvas.height = 32;
        var ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, 32, 32);
        ctx.beginPath();
        ctx.arc(25, 8, 6, 0, Math.PI * 2);
        ctx.fillStyle = "#c0392b";
        ctx.fill();
        link.href = canvas.toDataURL("image/png");
    };
    img.src = link.href;
})();

// 通知ベル。既読管理はサーバーに状態を持たず、ブラウザのlocalStorageで通知idごとに行う
// (通知の内容自体は川霧アラート・システム状態から毎回計算しているだけで、DBに保存された
// 通知レコードではないため)。
(function() {
    var notifications = window.RF_BASE.notifications || [];
    var STORAGE_KEY = "read-notification-ids";
    var bellBtn = document.getElementById("notif-bell-btn");
    var panel = document.getElementById("notif-panel");
    var badge = document.getElementById("notif-badge");
    var list = document.getElementById("notif-list");
    if (!bellBtn || !panel || !badge || !list) { return; }
    // 「お気に入り絞り込み」「既読の保存先」の説明は、通知が少ないと一覧より目立って
    // しまっていた(2026-10-24)。×を押したら以後この端末では出さない。
    var INFO_DISMISSED_KEY = "notif-info-dismissed";
    var info = document.getElementById("notif-info");
    var infoClose = document.getElementById("notif-info-close");
    if (info && infoClose) {
        var infoDismissed = false;
        try { infoDismissed = localStorage.getItem(INFO_DISMISSED_KEY) === "1"; } catch (e) {}
        info.hidden = infoDismissed;
        infoClose.addEventListener("click", function() {
            info.hidden = true;
            try { localStorage.setItem(INFO_DISMISSED_KEY, "1"); } catch (e) {}
        });
    }

    function loadRead() {
        try {
            var parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
            return Array.isArray(parsed) ? parsed : [];
        } catch (e) { return []; }
    }
    function saveRead(ids) {
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(ids)); } catch (e) {}
    }
    function updateBadge() {
        var read = loadRead();
        var unread = notifications.some(function(n) { return read.indexOf(n.id) === -1; });
        badge.hidden = !unread;
    }
    function renderList() {
        list.innerHTML = "";
        if (!notifications.length) {
            var empty = document.createElement("p");
            empty.className = "muted notif-empty";
            empty.textContent = "通知はありません。";
            list.appendChild(empty);
            return;
        }
        notifications.forEach(function(n) {
            var a = document.createElement("a");
            a.className = "notif-item notif-level-" + n.level;
            // サーバー生成データとはいえ、javascript:等の危険なスキームは多層防御として拒否する。
            a.href = /^\/[^/]/.test(n.url) ? n.url : "#";
            a.textContent = n.message;
            list.appendChild(a);
        });
    }
    function panelFocusable() {
        return Array.prototype.slice.call(panel.querySelectorAll("a, button"));
    }
    function trapFocus(e) {
        if (e.key !== "Tab") { return; }
        var focusable = panelFocusable();
        if (!focusable.length) { e.preventDefault(); return; }
        var first = focusable[0], last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
        }
    }
    function openPanel() {
        panel.hidden = false;
        bellBtn.setAttribute("aria-expanded", "true");
        saveRead(notifications.map(function(n) { return n.id; }));
        updateBadge();
        // 開いた直後にパネル内へフォーカスを移し、Tabキーがパネルの外に出ないようにする
        // (キーボード操作でベルを開いた場合、次のTabでどこに飛ぶか分からない問題への対応)。
        var focusable = panelFocusable();
        (focusable[0] || panel).focus();
        document.addEventListener("keydown", trapFocus);
    }
    function closePanel() {
        panel.hidden = true;
        bellBtn.setAttribute("aria-expanded", "false");
        document.removeEventListener("keydown", trapFocus);
    }
    bellBtn.addEventListener("click", function(e) {
        e.stopPropagation();
        if (panel.hidden) { openPanel(); } else { closePanel(); }
    });
    document.addEventListener("click", function(e) {
        if (!panel.hidden && !panel.contains(e.target) && e.target !== bellBtn) { closePanel(); }
    });
    document.addEventListener("keydown", function(e) {
        if (e.key === "Escape" && !panel.hidden) { closePanel(); }
    });
    renderList();
    updateBadge();
    // base.htmlの共通ポーラー(/api/nav_status、15秒おき)から、ページを開いたままでも
    // 川霧アラート等を反映できるようにする(2026-10-24まではページ読み込み時の内容のまま固定だった)。
    window.RF_BASE.updateNotifications = function(fresh) {
        notifications = fresh || [];
        renderList();
        updateBadge();
    };
})();

