/* 日付を選ぶカレンダー(2026-10-07)。すりガラスの月表示で、データのある日だけ選べる。
   - 日付一覧のページ: <div data-calendar-inline data-dates='["2026-10-06",...]' data-href="/x/0000-00-00">
     にその場で描く(新しい月から表示)。
   - 日付ごとのページ: <button data-calendar-button data-kind="images" data-current="2026-10-06"
     data-href="/images/0000-00-00"> を押すと、ボタンの下にカレンダーが開く(日付は/api/calendar-datesから)。
   - Ctrl+Kの検索(spotlight.js)からも RFCalendar.create を使う。
   - 普通の日付入力欄(<input type="date">)は、欄のどこを押してもブラウザのカレンダーが開くようにする。
   キーボード: ←→↑↓で日、PageUp/PageDownで月、Home/Endで週の初め/終わり、Enterで決定、Escで閉じる。 */
(function() {
    "use strict";
    var WEEK = ["日", "月", "火", "水", "木", "金", "土"];
    var KINDS = {
        observations: { label: "観測", cls: "obs" },
        images: { label: "画像", cls: "img" },
        predictions: { label: "予測", cls: "pred" }
    };
    var datesPromise = null;

    function pad(n) { return (n < 10 ? "0" : "") + n; }
    function fmt(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
    function parse(s) {
        var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
        return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
    }
    function addDays(d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); }
    function monthStart(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }
    function addMonths(d, n) { return new Date(d.getFullYear(), d.getMonth() + n, 1); }
    function weekdayLabel(s) { var d = parse(s); return d ? WEEK[d.getDay()] : ""; }

    function datesUrl() {
        var el = document.getElementById("spotlight-data");
        try { return JSON.parse(el.textContent).dates_url; } catch (e) { return "/api/calendar-dates"; }
    }
    /* 種類ごとのデータがある日付。1ページで1回だけ取得する。 */
    function loadDates() {
        if (!datesPromise) {
            datesPromise = fetch(datesUrl(), { credentials: "same-origin" })
                .then(function(r) { if (!r.ok) { throw new Error(r.status); } return r.json(); })
                .catch(function(e) { datesPromise = null; throw e; });
        }
        return datesPromise;
    }
    /* {日付: [種類,...]} を作る */
    function marksFrom(data, kinds) {
        var marks = {};
        (kinds || Object.keys(KINDS)).forEach(function(k) {
            (data[k] || []).forEach(function(d) { (marks[d] = marks[d] || []).push(k); });
        });
        return marks;
    }

    /* カレンダー本体。opts: marks({日付: [種類]}), selected, today, month("YYYY-MM-DD"のどれか),
       onPick(日付, イベント), href(日付→URL。あればリンクにする), showDots, label */
    function create(container, opts) {
        opts = opts || {};
        var marks = opts.marks || {};
        var keys = Object.keys(marks).sort();
        var minMonth = keys.length ? monthStart(parse(keys[0])) : null;
        var maxMonth = keys.length ? monthStart(parse(keys[keys.length - 1])) : null;
        var today = opts.today || fmt(new Date());
        var focusDate = opts.selected || keys[keys.length - 1] || today;
        var month = monthStart(parse(opts.month || focusDate) || new Date());

        var root = document.createElement("div");
        root.className = "rfcal" + (opts.compact ? " rfcal-compact" : "");
        root.setAttribute("role", "group");
        root.setAttribute("aria-label", opts.label || "カレンダー");
        root.innerHTML =
            '<div class="rfcal-head">' +
            '  <button type="button" class="rfcal-nav" data-step="-1" aria-label="前の月">‹</button>' +
            '  <div class="rfcal-title" aria-live="polite"></div>' +
            '  <button type="button" class="rfcal-nav" data-step="1" aria-label="次の月">›</button>' +
            '  <button type="button" class="rfcal-today">最新</button>' +
            '</div>' +
            '<div class="rfcal-week" aria-hidden="true">' +
            WEEK.map(function(w, i) { return '<span class="' + (i === 0 ? "sun" : i === 6 ? "sat" : "") + '">' + w + "</span>"; }).join("") +
            '</div><div class="rfcal-grid" role="grid"></div>';
        if (opts.showDots) {
            var legend = document.createElement("div");
            legend.className = "rfcal-legend";
            Object.keys(KINDS).forEach(function(k) {
                legend.insertAdjacentHTML("beforeend", '<span><i class="rfcal-dot ' + KINDS[k].cls + '"></i>' + KINDS[k].label + "</span>");
            });
            root.appendChild(legend);
        }
        container.appendChild(root);
        var title = root.querySelector(".rfcal-title");
        var grid = root.querySelector(".rfcal-grid");
        var prevBtn = root.querySelector('[data-step="-1"]');
        var nextBtn = root.querySelector('[data-step="1"]');

        function render(direction) {
            title.textContent = month.getFullYear() + "年" + (month.getMonth() + 1) + "月";
            prevBtn.disabled = !!(minMonth && month <= minMonth);
            nextBtn.disabled = !!(maxMonth && month >= maxMonth);
            grid.textContent = "";
            var start = addDays(month, -month.getDay());
            var daysInMonth = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
            var cells = Math.ceil((month.getDay() + daysInMonth) / 7) * 7;  // その月に要る週だけ描く
            var row;
            for (var i = 0; i < cells; i++) {
                if (i % 7 === 0) {
                    row = document.createElement("div");
                    row.className = "rfcal-row";
                    row.setAttribute("role", "row");
                    grid.appendChild(row);
                }
                var d = addDays(start, i), s = fmt(d), kinds = marks[s];
                var inMonth = d.getMonth() === month.getMonth();
                var el;
                if (kinds && inMonth) {
                    el = document.createElement(opts.href ? "a" : "button");
                    if (opts.href) { el.href = opts.href(s); } else { el.type = "button"; }
                    el.className = "rfcal-day has";
                    el.setAttribute("aria-label", (d.getMonth() + 1) + "月" + d.getDate() + "日(" + WEEK[d.getDay()] + ")" +
                        (opts.showDots ? " " + kinds.map(function(k) { return KINDS[k].label; }).join("・") : ""));
                } else {
                    el = document.createElement("span");
                    el.className = "rfcal-day" + (inMonth ? " none" : " out");
                }
                el.setAttribute("role", "gridcell");
                el.dataset.date = s;
                if (d.getDay() === 0) { el.classList.add("sun"); }
                if (d.getDay() === 6) { el.classList.add("sat"); }
                if (s === today) { el.classList.add("today"); }
                if (s === opts.selected) { el.classList.add("selected"); el.setAttribute("aria-current", "date"); }
                el.innerHTML = "<b>" + d.getDate() + "</b>";
                if (opts.showDots && kinds && inMonth) {
                    var dots = document.createElement("span");
                    dots.className = "rfcal-dots";
                    kinds.forEach(function(k) { dots.insertAdjacentHTML("beforeend", '<i class="rfcal-dot ' + KINDS[k].cls + '"></i>'); });
                    el.appendChild(dots);
                }
                if (el.tagName !== "SPAN") { el.tabIndex = s === focusDate ? 0 : -1; }
                row.appendChild(el);
            }
            // 表示中の月に「選べる日」が無ければ、その月で一番近い日にフォーカスを移す
            if (!grid.querySelector('[tabindex="0"]')) {
                var first = grid.querySelector(".rfcal-day.has");
                if (first) { first.tabIndex = 0; focusDate = first.dataset.date; }
            }
            if (direction) {
                grid.classList.remove("slide-l", "slide-r");
                void grid.offsetWidth;
                grid.classList.add(direction > 0 ? "slide-l" : "slide-r");
            }
        }
        function go(n) {
            var target = addMonths(month, n);
            if ((minMonth && target < minMonth) || (maxMonth && target > maxMonth)) { return false; }
            month = target;
            render(n);
            return true;
        }
        function focusOn(s) {
            var d = parse(s);
            if (!d) { return; }
            if (d.getMonth() !== month.getMonth() || d.getFullYear() !== month.getFullYear()) {
                month = monthStart(d);
                render(0);
            }
            var el = grid.querySelector('[data-date="' + s + '"]:not(span)');
            if (el) {
                Array.prototype.forEach.call(grid.querySelectorAll("[tabindex]"), function(x) { x.tabIndex = -1; });
                el.tabIndex = 0;
                focusDate = s;
                el.focus();
            }
        }
        /* 矢印キーでは、データのある日だけを渡り歩く(無い日は飛ばす) */
        function step(from, delta) {
            var d = parse(from);
            for (var i = 0; i < 400; i++) {
                d = addDays(d, delta);
                var s = fmt(d);
                if (marks[s]) { return s; }
                if ((minMonth && d < minMonth) || (maxMonth && d > addMonths(maxMonth, 1))) { return null; }
            }
            return null;
        }

        root.addEventListener("click", function(e) {
            var nav = e.target.closest(".rfcal-nav");
            if (nav) { go(+nav.dataset.step); return; }
            if (e.target.closest(".rfcal-today")) {
                var latest = keys[keys.length - 1];
                if (latest) { focusOn(marks[today] ? today : latest); }
                return;
            }
            var day = e.target.closest(".rfcal-day.has");
            if (day && opts.onPick) {
                if (!opts.href) { e.preventDefault(); }
                opts.onPick(day.dataset.date, e);
            }
        });
        grid.addEventListener("keydown", function(e) {
            var cur = e.target.closest && e.target.closest(".rfcal-day.has");
            if (!cur) { return; }
            var s = cur.dataset.date, next = null;
            switch (e.key) {
                case "ArrowLeft": next = step(s, -1); break;
                case "ArrowRight": next = step(s, 1); break;
                case "ArrowUp": next = step(s, -7) || step(s, -1); break;
                case "ArrowDown": next = step(s, 7) || step(s, 1); break;
                case "Home": next = step(fmt(addDays(parse(s), -parse(s).getDay() - 1)), 1); break;
                case "End": next = step(fmt(addDays(parse(s), 7 - parse(s).getDay())), -1); break;
                case "PageUp": if (go(-1)) { var p = grid.querySelector('[tabindex="0"]'); if (p) { p.focus(); } } e.preventDefault(); return;
                case "PageDown": if (go(1)) { var q = grid.querySelector('[tabindex="0"]'); if (q) { q.focus(); } } e.preventDefault(); return;
                default: return;
            }
            e.preventDefault();
            if (next) { focusOn(next); }
        });
        render(0);
        return {
            el: root,
            focus: function() { var f = grid.querySelector('[tabindex="0"]'); if (f) { f.focus(); } },
            focusOn: focusOn,
            showMonth: function(s) { var d = parse(s); if (d) { month = monthStart(d); render(0); } },
            destroy: function() { root.remove(); }
        };
    }

    /* ボタンの下に浮かぶカレンダー */
    var openPop = null;
    function closePopover() {
        if (!openPop) { return; }
        var p = openPop;
        openPop = null;
        p.el.classList.remove("open");
        p.anchor.setAttribute("aria-expanded", "false");
        document.removeEventListener("mousedown", p.outside, true);
        document.removeEventListener("keydown", p.esc, true);
        window.removeEventListener("resize", p.place);
        setTimeout(function() { p.el.remove(); }, 160);
        if (p.restoreFocus) { p.anchor.focus(); }
    }
    function popover(anchor, opts) {
        closePopover();
        var el = document.createElement("div");
        el.className = "rfcal-pop";
        el.setAttribute("role", "dialog");
        el.setAttribute("aria-label", opts.label || "日付を選ぶ");
        document.body.appendChild(el);
        var cal = create(el, opts);
        function place() {
            var r = anchor.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight;
            var left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
            var top = r.bottom + 8;
            if (top + h > window.innerHeight - 8 && r.top - h - 8 > 8) { top = r.top - h - 8; }
            el.style.left = left + "px";
            el.style.top = Math.max(8, top) + "px";
        }
        var p = {
            el: el, anchor: anchor, place: place, restoreFocus: false,
            outside: function(e) { if (!el.contains(e.target) && !anchor.contains(e.target)) { closePopover(); } },
            esc: function(e) { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); p.restoreFocus = true; closePopover(); } }
        };
        openPop = p;
        place();
        anchor.setAttribute("aria-expanded", "true");
        document.addEventListener("mousedown", p.outside, true);
        document.addEventListener("keydown", p.esc, true);
        window.addEventListener("resize", place);
        requestAnimationFrame(function() { el.classList.add("open"); cal.focus(); });
        return cal;
    }

    function hrefFrom(template) { return function(s) { return template.replace("0000-00-00", s); }; }

    function initInline(el) {
        var dates;
        try { dates = JSON.parse(el.dataset.dates || "[]"); } catch (e) { dates = []; }
        if (!dates.length) { return; }
        var marks = {}, kind = el.dataset.kind || "observations";
        dates.forEach(function(d) { marks[d] = [kind]; });
        create(el, { marks: marks, href: hrefFrom(el.dataset.href), label: el.dataset.label || "日付を選ぶ" });
    }
    function initButton(btn) {
        btn.setAttribute("aria-haspopup", "dialog");
        btn.setAttribute("aria-expanded", "false");
        btn.addEventListener("click", function() {
            if (openPop && openPop.anchor === btn) { closePopover(); return; }
            var kind = btn.dataset.kind;
            btn.classList.add("is-loading");
            loadDates().then(function(data) {
                btn.classList.remove("is-loading");
                var marks = marksFrom(data, [kind]);
                popover(btn, {
                    marks: marks, selected: btn.dataset.current, today: data.today,
                    href: hrefFrom(btn.dataset.href), label: btn.dataset.label || "日付を選ぶ"
                });
            }).catch(function() {
                btn.classList.remove("is-loading");
                if (window.showToast) { window.showToast("日付の一覧を読み込めませんでした", true); }
            });
        });
    }

    /* 普通の日付入力欄: 欄のどこを押してもブラウザのカレンダーを開く(対応ブラウザのみ) */
    document.addEventListener("click", function(e) {
        var input = e.target.closest && e.target.closest('input[type="date"], input[type="month"], input[type="datetime-local"]');
        if (input && !input.disabled && !input.readOnly && typeof input.showPicker === "function") {
            try { input.showPicker(); } catch (err) {}
        }
    });

    function init() {
        Array.prototype.forEach.call(document.querySelectorAll("[data-calendar-inline]"), initInline);
        Array.prototype.forEach.call(document.querySelectorAll("[data-calendar-button]"), initButton);
    }
    if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", init); } else { init(); }

    window.RFCalendar = {
        create: create, popover: popover, close: closePopover, loadDates: loadDates,
        marksFrom: marksFrom, kinds: KINDS, weekday: weekdayLabel, format: fmt, parse: parse, addDays: addDays
    };
})();
