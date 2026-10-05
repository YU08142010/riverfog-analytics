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
