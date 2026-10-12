/* 圏外で撮った写真の後送り(2026-10-12)。投稿の画面で、ネットにつながっていないときは写真と入力をこの端末
   (IndexedDB)に入れておき、つながったとき(このサイトのどのページを開いていても)に順に送る。
   base.htmlが <body data-photo-submit="(投稿のURL)" data-csrf="(トークン)"> を付けた、ログイン中のページだけで動く。 */
(function() {
    var DB = "rf-offline", STORE = "photos";
    var submitUrl = document.body.getAttribute("data-photo-submit");
    var csrf = document.body.getAttribute("data-csrf");
    if (!submitUrl || !("indexedDB" in window)) { return; }

    function open() {
        return new Promise(function(resolve, reject) {
            var r = indexedDB.open(DB, 1);
            r.onupgradeneeded = function() { r.result.createObjectStore(STORE, {keyPath: "id", autoIncrement: true}); };
            r.onsuccess = function() { resolve(r.result); };
            r.onerror = function() { reject(r.error); };
        });
    }
    function tx(mode, fn) {
        return open().then(function(db) {
            return new Promise(function(resolve, reject) {
                var t = db.transaction(STORE, mode), out = fn(t.objectStore(STORE));
                t.oncomplete = function() { resolve(out && out.result !== undefined ? out.result : out); };
                t.onerror = function() { reject(t.error); };
            });
        });
    }
    function all() { return tx("readonly", function(s) { return s.getAll(); }); }
    function remove(id) { return tx("readwrite", function(s) { s.delete(id); }); }
    function toast(msg, err) { if (window.showToast) { window.showToast(msg, err); } }

    // 投稿のフォームから: 撮った時刻が空なら、写真のファイルの時刻(無ければ今)を入れておく(後で送ると送った時刻になってしまうため)
    window.rfQueuePhoto = function(form) {
        var fd = new FormData(form), fields = {}, file = fd.get("photo");
        fd.forEach(function(v, k) { if (k !== "photo" && k !== "csrf_token" && typeof v === "string") { fields[k] = v; } });
        if (!fields.taken_at) {
            var d = new Date(file && file.lastModified ? file.lastModified : Date.now());
            var pad = function(n) { return (n < 10 ? "0" : "") + n; };
            fields.taken_at = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
        }
        return tx("readwrite", function(s) {
            s.add({fields: fields, file: file, name: file ? file.name : "photo.jpg", queued_at: Date.now()});
        }).then(function() { updateBadge(); });
    };

    var sending = false;
    function flush() {
        if (sending || !navigator.onLine) { return; }
        sending = true;
        all().then(function(items) {
            var chain = Promise.resolve(), sent = 0, failed = 0;
            items.forEach(function(it) {
                chain = chain.then(function() {
                    var fd = new FormData();
                    Object.keys(it.fields).forEach(function(k) { fd.append(k, it.fields[k]); });
                    fd.append("csrf_token", csrf);
                    fd.append("photo", it.file, it.name);
                    return fetch(submitUrl, {method: "POST", body: fd, headers: {"X-Requested-With": "XMLHttpRequest"}, credentials: "same-origin"})
                        .then(function(r) { return r.json().catch(function() { return {success: false, message: "送れませんでした(" + r.status + ")"}; }); })
                        .then(function(j) {
                            if (j.success) { sent++; return remove(it.id); }
                            failed++;
                            // 内容の問題(画像でない・場所が分からない等)は何度送っても同じなので、知らせて消す
                            toast("保存しておいた写真を送れませんでした: " + j.message, true);
                            return remove(it.id);
                        });
                });
            });
            return chain.then(function() { if (sent) { toast("つながったので、保存しておいた写真を" + sent + "枚送りました。"); } });
        }).catch(function() {}).then(function() { sending = false; updateBadge(); });
    }
    function updateBadge() {
        var el = document.getElementById("photo-queue-note");
        if (!el) { return; }
        all().then(function(items) {
            el.hidden = !items.length;
            el.textContent = items.length ? "この端末に、まだ送っていない写真が" + items.length + "枚あります。つながったら自動で送ります。" : "";
        }).catch(function() {});
    }
    window.addEventListener("online", flush);
    if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", function() { updateBadge(); flush(); }); }
    else { updateBadge(); flush(); }
})();
