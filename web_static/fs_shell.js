/* 全画面表示のボタンと、全画面のままページを移る仕組み(2026-10-07)。base.html(研究用)とpublic_base.html(公開)で共通。
   ブラウザの全画面表示は「いま開いている文書」にかかるため、リンクで別のページを読み込むと必ず解除され、
   操作なしに戻すこともできない。そこで全画面の間は、移動先のページを全画面の中の枠(iframe)に読み込む:
   - 全画面で、同じサイトへのリンク・フォーム送信・スクリプトでの移動(Navigation API対応のブラウザ)を枠の中で開く。
     以後の移動は枠の中で起きるので、全画面は解除されない。ブラウザの「戻る」は枠の中の履歴をたどる。
   - 枠の中のページの全画面ボタンは「全画面を終了」として働く。全画面を終えると、枠で見ていたページを普通に開き直す。
   - それでも解除された場合(ブラウザのタブの切り替え等)は、「全画面に戻す」ボタンを出す(押すだけで戻る)。
     Escや終了ボタンで自分で終えたときは出さない。
   ボタンは id="fs-toggle-btn"(研究用、親の#fs-wrapを表示) または id="pf-fs-btn"(公開)。 */
(function() {
    "use strict";
    var WANT_KEY = "fs-wanted";
    var btn = document.getElementById("fs-toggle-btn") || document.getElementById("pf-fs-btn");
    var wrap = document.getElementById("fs-wrap");
    var root = document.documentElement;

    function store(v) { try { if (v) { sessionStorage.setItem(WANT_KEY, "1"); } else { sessionStorage.removeItem(WANT_KEY); } } catch (e) {} }
    function wanted() { try { return sessionStorage.getItem(WANT_KEY) === "1"; } catch (e) { return false; } }
    function showButton() { if (wrap) { wrap.hidden = false; } else if (btn) { btn.hidden = false; } }
    function setPressed(on) {
        if (!btn) { return; }
        var label = on ? "全画面表示を終了する" : "全画面で表示する";
        btn.setAttribute("aria-pressed", on ? "true" : "false");
        btn.setAttribute("aria-label", label);
        btn.title = label;
    }

    /* ---- 枠の中のページ: ボタンは親の全画面を終える ---- */
    var parentShell = null;
    try { if (window.top !== window && window.top.RFShell && window.top.RFShell.active()) { parentShell = window.top.RFShell; } } catch (e) {}
    if (parentShell) {
        root.classList.add("fs-in-shell");
        if (btn) {
            showButton();
            setPressed(true);
            btn.addEventListener("click", function(e) { e.preventDefault(); parentShell.exit(); });
        }
        return;
    }

    var supported = !!(document.fullscreenEnabled || document.webkitFullscreenEnabled);
    if (!btn || !supported) { return; }
    showButton();
    function current() { return document.fullscreenElement || document.webkitFullscreenElement; }
    function enter() {
        try {
            var p = (root.requestFullscreen || root.webkitRequestFullscreen).call(root);
            if (p && p.catch) { p.catch(function() {}); }
        } catch (e) {}
    }
    var exitingByUser = false;
    function exit() {
        exitingByUser = true;
        store(false);
        try { (document.exitFullscreen || document.webkitExitFullscreen).call(document); } catch (e) {}
    }

    /* ---- 全画面の中の枠 ---- */
    var frame = null;
    function ensureFrame() {
        if (frame) { return frame; }
        frame = document.createElement("iframe");
        frame.name = "rf-fs-shell";
        frame.className = "fs-shell-frame";
        frame.title = "ページ";
        frame.setAttribute("allow", "clipboard-write");
        frame.addEventListener("load", function() {
            try {
                var w = frame.contentWindow;
                if (w.location.href === "about:blank") { return; }
                document.title = w.document.title;
                // 再読み込み・全画面の終了時に同じページが開くよう、上のURLも合わせておく
                history.replaceState(history.state, "", w.location.href);
                w.focus();
            } catch (e) {}
        });
        document.body.appendChild(frame);
        root.classList.add("fs-shell-on");
        return frame;
    }
    function active() { return !!current(); }
    function navigate(url) {
        if (!active()) { return false; }
        ensureFrame().src = url;
        return true;
    }
    window.RFShell = { active: active, navigate: navigate, exit: exit };

    function sameSite(url) {
        try { var u = new URL(url, location.href); return u.origin === location.origin; } catch (e) { return false; }
    }
    function samePageHash(url) {
        var u = new URL(url, location.href);
        return u.pathname === location.pathname && u.search === location.search && !!u.hash;
    }

    // Navigation API(Chrome/Edge): リンク・location.href・フォーム(GET)など、上の文書の移動をまとめて枠へ回す
    if (window.navigation && window.navigation.addEventListener) {
        window.navigation.addEventListener("navigate", function(e) {
            if (!active()) { return; }
            if (!e.cancelable || e.hashChange || e.downloadRequest || e.formData) { return; }
            if (e.destination && e.destination.sameDocument) { return; }
            if (e.navigationType === "reload" || e.navigationType === "traverse") { return; }
            var url = e.destination && e.destination.url;
            if (!url || !sameSite(url) || samePageHash(url)) { return; }
            e.preventDefault();
            navigate(url);
        });
    } else {
        // 対応しないブラウザ: 同じサイトへのリンクのクリックだけ枠へ回す
        document.addEventListener("click", function(e) {
            if (!active() || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) { return; }
            var a = e.target.closest && e.target.closest("a[href]");
            if (!a || (a.target && a.target !== "_self") || a.hasAttribute("download")) { return; }
            if (!sameSite(a.href) || samePageHash(a.href)) { return; }
            e.preventDefault();
            navigate(a.href);
        });
    }
    // フォーム送信(POSTも)は、送信先を枠にする
    document.addEventListener("submit", function(e) {
        if (!active() || e.defaultPrevented) { return; }
        var form = e.target;
        if (!form || (form.target && form.target !== "_self")) { return; }
        if (!sameSite(form.action || location.href)) { return; }
        ensureFrame();
        form.target = "rf-fs-shell";
    });

    /* ---- ボタン・解除されたとき ---- */
    btn.addEventListener("click", function() {
        if (current()) { exit(); } else { store(true); hidePill(); enter(); }
    });
    var pill = null;
    function hidePill() { if (pill) { pill.remove(); pill = null; } }
    function showPill() {
        if (pill || current()) { return; }
        pill = document.createElement("div");
        pill.className = "fs-restore";
        pill.setAttribute("role", "status");
        pill.innerHTML = '<button type="button" class="fs-restore-go"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>全画面に戻す</button>' +
                         '<button type="button" class="fs-restore-close" aria-label="全画面に戻さない">×</button>';
        pill.querySelector(".fs-restore-go").addEventListener("click", function() { hidePill(); enter(); });
        pill.querySelector(".fs-restore-close").addEventListener("click", function() { store(false); hidePill(); });
        document.body.appendChild(pill);
    }
    function onChange() {
        var on = !!current();
        setPressed(on);
        if (on) { hidePill(); return; }
        var byUser = exitingByUser;
        exitingByUser = false;
        // 画面が見えたままで外れた = Escやボタンで自分で終えた(タブの切り替えなら画面が隠れる)
        var intentional = byUser || (document.visibilityState === "visible" && !hiddenRecently);
        if (frame) {
            if (intentional) { store(false); }
            // 枠で見ていたページを、普通のページとして開き直す
            var url = null;
            try { url = frame.contentWindow.location.href; } catch (e) {}
            frame.remove();
            frame = null;
            root.classList.remove("fs-shell-on");
            if (url && url !== "about:blank") { location.replace(url); return; }
        }
        // 自分で終えたのか(Esc・ボタン)、タブの切り替え等で外れたのかを見分ける
        // (visibilitychangeが少し後に届くことがあるので、少し待ってから判断する)
        setTimeout(function() {
            if (document.visibilityState === "hidden" || hiddenRecently) { return; }  // タブを離れた: 戻ったら出す
            store(false);  // Esc・ボタン: 自分で終えた
        }, 150);
    }
    var hiddenRecently = false;
    document.addEventListener("visibilitychange", function() {
        if (document.visibilityState === "hidden") {
            hiddenRecently = true;
        } else {
            if (wanted() && !current()) { showPill(); }
            setTimeout(function() { hiddenRecently = false; }, 1500);
        }
    });
    document.addEventListener("fullscreenchange", onChange);
    document.addEventListener("webkitfullscreenchange", onChange);
    // 全画面のままページが移ってしまった(枠で受けられなかった移動)後は、戻すボタンを出す
    if (wanted() && !current()) { showPill(); }
})();
