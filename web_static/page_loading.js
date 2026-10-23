/* フルページ遷移(リンク・フォーム送信)のあいだ、画面上部に薄い帯を表示する(loading.css)。
   このアプリはSPAではなく毎回サーバーに取りに行く作りのため、何も表示しないと応答待ちの間
   画面が固まった(または白い)ままに見える。遷移が終われば新しいページが読み込まれ、この
   帯も含めて丸ごと作り直されるので、「消す」処理は不要(bfcache復帰時だけ念のため消す)。 */
(function () {
    "use strict";

    var bar = null;

    function showBar() {
        if (!bar) {
            bar = document.createElement("div");
            bar.className = "rf-page-loading-bar";
            bar.setAttribute("aria-hidden", "true");
            document.body.appendChild(bar);
        }
        // 同期的にクラスを付けると幅のtransitionが発火しないブラウザがあるため1フレーム待つ
        window.requestAnimationFrame(function () {
            bar.classList.add("is-active");
        });
    }

    function isPlainClick(e) {
        return !(e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey);
    }

    document.addEventListener("click", function (e) {
        if (!isPlainClick(e)) { return; }
        var a = e.target.closest && e.target.closest("a[href]");
        if (!a) { return; }
        if (a.target && a.target !== "_self") { return; }
        if (a.hasAttribute("download") || a.getAttribute("rel") === "external") { return; }
        var href = a.getAttribute("href") || "";
        if (!href || href.charAt(0) === "#" || /^(javascript|mailto|tel):/i.test(href)) { return; }
        var url;
        try {
            url = new URL(a.href, window.location.href);
        } catch (err) {
            return;
        }
        if (url.origin !== window.location.origin) { return; }
        // 同一ページ内のハッシュ移動(サーバーに取りに行かない)は対象外
        if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash) { return; }
        if (e.defaultPrevented) { return; }
        showBar();
    });

    document.addEventListener("submit", function (e) {
        var form = e.target;
        if (!form || form.hasAttribute("data-no-loading")) { return; }
        // フォーム側でfetch送信などに切り替えてpreventDefault()していたら対象外
        // (このリスナーはバブリング段で動くため、要素自身のハンドラは先に実行済み)
        if (e.defaultPrevented) { return; }
        showBar();
    });

    window.addEventListener("pageshow", function () {
        if (bar) { bar.classList.remove("is-active"); }
    });
})();
