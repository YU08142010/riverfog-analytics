// base.html(ログイン後の全ページ)とbase_auth.html(ログイン/サインアップ画面)は別々の
// HTML文書のため、両方から使う小さな共通ロジックをここにまとめる(以前はコピペで重複していた)。

// パスワード強度の簡易判定。class="js-pw-strength" の入力欄に使う。
function _pwStrength(pw) {
    if (!pw) { return {score: 0, label: ""}; }
    var score = 0;
    if (pw.length >= 8) score++;
    if (pw.length >= 12) score++;
    if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score++;
    if (/[0-9]/.test(pw)) score++;
    if (/[^A-Za-z0-9]/.test(pw)) score++;
    var labels = ["非常に弱い", "弱い", "普通", "強い", "非常に強い"];
    var colors = ["#c0392b", "#c0392b", "#b9770e", "#27ae60", "#1e8449"];
    var idx = Math.min(score, 4);
    return {score: idx, label: labels[idx], color: colors[idx], pct: pw.length ? (idx + 1) * 20 : 0};
}
document.addEventListener("input", function(e) {
    var input = e.target;
    if (!(input instanceof HTMLInputElement) || !input.classList.contains("js-pw-strength")) { return; }
    var wrap = input.closest(".pw-strength-wrap") || input.parentElement;
    var bar = wrap.querySelector(".strength-meter-bar");
    var label = wrap.querySelector(".strength-label");
    var result = _pwStrength(input.value);
    if (bar) { bar.style.width = result.pct + "%"; bar.style.background = result.color || "transparent"; }
    if (label) { label.textContent = input.value ? "強度: " + result.label : ""; }
});

// サイドバー検索・各種一覧フィルタで共通して使う「入力文字列でリスト項目を絞り込む」ロジック。
// 以前は_admin_sidebar.html/_station_sidebar.html/stations_list.html/admin_log.htmlに
// それぞれ個別実装があり、手法(classList.toggle/style.display)もバラバラだった。
//
// input: フィルタ用のテキスト入力要素
// items: 絞り込み対象の要素の配列(NodeList可)
// textOf: 各itemから検索対象の文字列を取り出す関数(省略時はtextContent)
// onFiltered: (visibleCount) => void。0件時の空表示切り替え等に使う(省略可)
function wireListFilter(input, items, textOf, onFiltered) {
    if (!input) { return; }
    var list = Array.prototype.slice.call(items);
    textOf = textOf || function(el) { return el.textContent; };
    input.addEventListener("input", function() {
        var q = input.value.trim().toLowerCase();
        var visible = 0;
        list.forEach(function(el) {
            var match = !q || textOf(el).toLowerCase().indexOf(q) !== -1;
            el.hidden = !match;
            if (match) { visible++; }
        });
        if (onFiltered) { onFiltered(visible); }
    });
}
