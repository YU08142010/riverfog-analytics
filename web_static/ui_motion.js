/* ボタン等を押したときの反応(2026-10-05)。glass.cssの「動き」の節と組で使う。
   - 押した位置から広がる波紋(ripple)。ホストのoverflowを変えずに済むよう、
     ホスト全面に重ねた切り抜き用の<span>の中に波紋を描く。
   - ダーク/ライト切替のときだけ、色の変化をなめらかにする。
   「動きを減らす」設定の利用者には何もしない。 */
(function() {
    "use strict";
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) { return; }

    var SELECTOR = [
        "a.button", "button.button", "label.button",
        "header nav a", ".page-tabs a", ".sidebar-list a",
        ".theme-toggle", ".notif-bell", ".nav-toggle",
        ".pf-day", ".pf-nav a", ".pf-chip", ".wx-hero-stat",
        ".copy-btn", ".back-to-top"
    ].join(",");

    document.addEventListener("pointerdown", function(e) {
        if (e.button !== 0) { return; }
        var host = e.target.closest && e.target.closest(SELECTOR);
        if (!host || host.disabled || host.getAttribute("aria-disabled") === "true") { return; }
        if (getComputedStyle(host).position === "static") { host.style.position = "relative"; }
        var rect = host.getBoundingClientRect();
        var clip = document.createElement("span");
        clip.className = "ui-ripple-clip";
        clip.setAttribute("aria-hidden", "true");
        var ripple = document.createElement("span");
        ripple.className = "ui-ripple";
        var size = Math.max(rect.width, rect.height) * 2.2;
        ripple.style.width = ripple.style.height = size + "px";
        ripple.style.left = (e.clientX - rect.left - size / 2) + "px";
        ripple.style.top = (e.clientY - rect.top - size / 2) + "px";
        clip.appendChild(ripple);
        host.appendChild(clip);
        ripple.addEventListener("animationend", function() { clip.remove(); });
        setTimeout(function() { if (clip.parentNode) { clip.remove(); } }, 1200);
    }, { passive: true });

    // ダーク/ライト切替: 切り替えの瞬間だけ全体の色をなめらかに遷移させる
    document.addEventListener("click", function(e) {
        if (!e.target.closest) { return; }
        if (!e.target.closest("#theme-toggle-btn, #pf-theme-btn")) { return; }
        var root = document.documentElement;
        root.classList.add("ui-theme-anim");
        setTimeout(function() { root.classList.remove("ui-theme-anim"); }, 600);
    }, true);
})();

/* スクロールで浮かび上がるカード・数字のカウントアップ・タイムラプスの切り替え演出(2026-10-05)。 */
(function() {
    "use strict";
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) { return; }
    if (!("IntersectionObserver" in window)) { return; }

    function ready(fn) {
        if (document.readyState !== "loading") { fn(); } else { document.addEventListener("DOMContentLoaded", fn); }
    }

    // 数字を0から目標値まで増やす。要素の最初の文字列の先頭の数値だけを書き換える(単位の<span>等はそのまま)。
    function countUp(el) {
        var node = null;
        for (var i = 0; i < el.childNodes.length; i++) {
            if (el.childNodes[i].nodeType === 3 && /\d/.test(el.childNodes[i].nodeValue)) { node = el.childNodes[i]; break; }
        }
        if (!node) { return; }
        var text = node.nodeValue;
        var m = text.match(/^(\s*)(-?\d[\d,]*\.?\d*)(.*)$/s);
        if (!m) { return; }
        var target = parseFloat(m[2].replace(/,/g, ""));
        if (!isFinite(target) || target === 0) { return; }
        var decimals = (m[2].split(".")[1] || "").length;
        var comma = m[2].indexOf(",") >= 0;
        var start = null, dur = Math.min(1400, 700 + Math.abs(target) * 4);
        function fmt(v) {
            var s = v.toFixed(decimals);
            return comma ? Number(s).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) : s;
        }
        function step(ts) {
            if (start === null) { start = ts; }
            var p = Math.min(1, (ts - start) / dur);
            var e = 1 - Math.pow(1 - p, 3);
            node.nodeValue = m[1] + fmt(target * e) + m[3];
            if (p < 1) { requestAnimationFrame(step); } else { node.nodeValue = text; }
        }
        node.nodeValue = m[1] + fmt(0) + m[3];
        requestAnimationFrame(step);
    }

    ready(function() {
        var COUNT = ".wx-hero-temp, .wx-hero-stat b, .stat-value, .pf-stat .v, .wx-card-temp";
        var REVEAL = "main .card, main .pf-card, main .wx-card";
        var vh = window.innerHeight || 800;
        var io = new IntersectionObserver(function(entries) {
            entries.forEach(function(en) {
                if (!en.isIntersecting) { return; }
                var el = en.target;
                io.unobserve(el);
                if (el.classList.contains("ui-reveal")) {
                    el.style.transitionDelay = (el.__revealDelay || 0) + "ms";
                    el.classList.add("ui-in");
                    setTimeout(function() { el.style.transitionDelay = ""; }, 1200);
                }
                if (el.__countUp) { countUp(el); }
            });
        }, { rootMargin: "0px 0px -6% 0px", threshold: 0.08 });

        var batch = 0;
        Array.prototype.forEach.call(document.querySelectorAll(REVEAL), function(el) {
            // 最初から画面に見えているものはそのまま(読み込み直後にちらつかないように)
            if (el.getBoundingClientRect().top < vh * 0.92) { return; }
            el.classList.add("ui-reveal");
            el.__revealDelay = (batch++ % 4) * 70;
            io.observe(el);
        });
        Array.prototype.forEach.call(document.querySelectorAll(COUNT), function(el) {
            el.__countUp = true;
            io.observe(el);
        });

        // タイムラプス: 画像が切り替わるたびにふわっと表示
        var tl = document.getElementById("tl-img");
        if (tl) {
            tl.addEventListener("load", function() {
                tl.classList.remove("tl-fade");
                void tl.offsetWidth;
                tl.classList.add("tl-fade");
            });
        }
    });
})();