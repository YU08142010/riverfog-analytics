/* 電波の弱い場所でも見られるようにする仕組み(2026-10-12)は、2026-10-14に利用者の判断でやめた
   (Tailscaleの不調などで本当につながらないときに「ネットにつながっていません」の画面が出て、分かりにくかったため)。
   すでにこの仕組みが入っているブラウザのために、このファイルは「自分を解除して消える」版にしてある:
   読み込まれたら、保存したページ・画像を消し、自分の登録を解除し、開いているページを読み直す。
   ページの取得には一切関わらない(fetchを扱わない)。通知用の /web_static/sw.js とは別物で、通知には影響しない。 */
self.addEventListener("install", function() { self.skipWaiting(); });
self.addEventListener("activate", function(e) {
    e.waitUntil(
        caches.keys().then(function(keys) {
            return Promise.all(keys.filter(function(k) { return /^rf-(pages|assets|images)-/.test(k); })
                .map(function(k) { return caches.delete(k); }));
        }).then(function() {
            return self.registration.unregister();
        }).then(function() {
            return self.clients.matchAll({type: "window"});
        }).then(function(list) {
            list.forEach(function(c) { if (c.navigate) { c.navigate(c.url).catch(function() {}); } });
        })
    );
});
