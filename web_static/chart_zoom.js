/* グラフの拡大表示(2026-10-08)。サイト内のグラフ画像(base.htmlのmountPlotlyChartが作る img.chart-img、
   分析ページ等の .chart-wrap img・img.full-graph)を押すと、画面いっぱいに大きく表示し、保存もできる。
   data-chart-url を持つグラフは、拡大用に大きいサイズ・タイトル付きで描き直したものを表示する。
   狭い画面では横にスクロールでき、ピンチでの拡大もそのまま使える。 */
(function() {
    var SELECTOR = "img.chart-img, .chart-wrap img, img.full-graph";
    var overlay, body, img, title, save, closeBtn, lastFocus = null;

    function build() {
        overlay = document.createElement("div");
        overlay.className = "rf-zoom";
        overlay.hidden = true;
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.setAttribute("aria-label", "グラフの拡大表示");
        overlay.innerHTML =
            '<div class="rf-zoom-bar">' +
            '  <span class="rf-zoom-title"></span>' +
            '  <a class="rf-zoom-btn rf-zoom-save" download="graph.png">保存</a>' +
            '  <button type="button" class="rf-zoom-btn rf-zoom-close" aria-label="閉じる">×</button>' +
            '</div>' +
            '<div class="rf-zoom-body"><img alt=""></div>';
        document.body.appendChild(overlay);
        body = overlay.querySelector(".rf-zoom-body");
        img = overlay.querySelector(".rf-zoom-body img");
        title = overlay.querySelector(".rf-zoom-title");
        save = overlay.querySelector(".rf-zoom-save");
        closeBtn = overlay.querySelector(".rf-zoom-close");
        closeBtn.addEventListener("click", close);
        overlay.addEventListener("click", function(e) {
            if (e.target === overlay || e.target === body) { close(); }
        });
        document.addEventListener("keydown", function(e) {
            if (overlay.hidden) { return; }
            if (e.key === "Escape") { e.preventDefault(); close(); }
            if (e.key === "Tab") {  // フォーカスを拡大表示の中に留める
                var f = [save, closeBtn];
                var i = f.indexOf(document.activeElement);
                e.preventDefault();
                f[(i + (e.shiftKey ? f.length - 1 : 1)) % f.length].focus();
            }
        });
    }

    function theme() {
        var t = document.documentElement.getAttribute("data-theme");
        if (t === "dark" || t === "light") { return t; }
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }

    function bigSrc(source) {
        var holder = source.closest(".chart-themed[data-chart-url]");
        var narrow = window.innerWidth < 700;
        var w = narrow ? 760 : Math.max(800, Math.min(1600, Math.round((window.innerWidth - 80) / 40) * 40));
        if (holder) {
            var url = holder.dataset.chartUrl;
            return {
                src: url + (url.indexOf("?") >= 0 ? "&" : "?") + "format=png&theme=" + theme() + "&w=" + w + "&scale=2&notitle=0",
                width: w, narrow: narrow
            };
        }
        return { src: source.currentSrc || source.src, width: null, narrow: narrow };
    }

    function captionFor(source) {
        var card = source.closest(".card, .pf-card");
        var h = card && card.querySelector("h2, h3");
        return (h && h.textContent.trim()) || source.getAttribute("alt") || "グラフ";
    }

    function open(source) {
        if (!overlay) { build(); }
        var big = bigSrc(source);
        lastFocus = document.activeElement;
        title.textContent = captionFor(source);
        img.alt = source.getAttribute("alt") || "グラフ";
        img.src = big.src;
        img.style.width = big.narrow && big.width ? big.width + "px" : "";
        overlay.classList.toggle("is-narrow", big.narrow);
        save.href = big.src;
        // 保存する名前: グラフの周りに data-download-name があればそれ(例: 気象要素ごとのヒストグラム)
        var named = source.closest("[data-download-name]");
        save.setAttribute("download", named ? named.getAttribute("data-download-name") : "graph.png");
        overlay.hidden = false;
        document.documentElement.classList.add("rf-zoom-open");
        closeBtn.focus();
    }

    function close() {
        overlay.hidden = true;
        img.removeAttribute("src");
        document.documentElement.classList.remove("rf-zoom-open");
        if (lastFocus && lastFocus.focus) { lastFocus.focus(); }
    }

    document.addEventListener("click", function(e) {
        var t = e.target.closest ? e.target.closest(SELECTOR) : null;
        if (!t || t.closest("a, .rf-zoom") || !t.complete || !t.naturalWidth) { return; }
        e.preventDefault();
        open(t);
    });
    // キーボードでも開けるように(グラフにフォーカスしてEnter)
    function makeFocusable(root) {
        (root || document).querySelectorAll(SELECTOR).forEach(function(el) {
            if (el.closest("a") || el.hasAttribute("tabindex")) { return; }
            el.setAttribute("tabindex", "0");
            el.setAttribute("role", "button");
            el.setAttribute("aria-label", (el.getAttribute("alt") || "グラフ") + "を拡大表示");
        });
    }
    document.addEventListener("keydown", function(e) {
        if (e.key !== "Enter" && e.key !== " ") { return; }
        var t = document.activeElement;
        if (t && t.matches && t.matches(SELECTOR) && !t.closest("a, .rf-zoom")) { e.preventDefault(); open(t); }
    });
    document.addEventListener("DOMContentLoaded", function() { makeFocusable(); });
    var pending = false;
    new MutationObserver(function() {
        if (pending) { return; }
        pending = true;
        setTimeout(function() { pending = false; makeFocusable(); }, 300);
    }).observe(document.documentElement, { childList: true, subtree: true });
})();
