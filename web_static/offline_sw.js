/* 電波の弱い場所でも見られるようにするService Worker(2026-10-12)。/offline-sw.js として配信し、範囲は "/"。
   通知用のService Worker(/web_static/sw.js、範囲 /web_static/)とは別で、通知の登録には関係しない。
   - ホーム・地点一覧・地点のページ・見頃・今朝の川霧・みんなの写真: まずネットから取り、取れたらこの端末に保存する。
     ネットにつながらない・遅い(PAGE_TIMEOUT_MS)ときは、最後に保存したものを出す。
   - サイトのCSS・JS・アイコン(地図の部品Leafletも /web_static/vendor/leaflet/ にある): 保存したものをすぐ出し、裏で新しくする。
   - カメラ画像はその地点のページで最後に見た分だけ(新しい順にIMAGE_MAX枚まで)。地図の背景の画像は保存しない。
   - ログアウトすると、保存したページは消す(ログイン画面が rf-pages を消す)。 */
var PAGES = "rf-pages-v1", ASSETS = "rf-assets-v1", IMAGES = "rf-images-v1";
var PAGE_TIMEOUT_MS = 6000, IMAGE_MAX = 120, PAGE_MAX = 80;
var PAGE_RE = /^\/(stations(\/[^/]+)?|flow|gallery|photos|map)?\/?$/;

self.addEventListener("install", function() { self.skipWaiting(); });
self.addEventListener("activate", function(e) {
    e.waitUntil(caches.keys().then(function(keys) {
        return Promise.all(keys.filter(function(k) { return /^rf-(pages|assets|images)-/.test(k) && [PAGES, ASSETS, IMAGES].indexOf(k) < 0; })
            .map(function(k) { return caches.delete(k); }));
    }).then(function() { return self.clients.claim(); }));
});

function trim(name, max) {
    return caches.open(name).then(function(c) {
        return c.keys().then(function(keys) {
            if (keys.length <= max) { return; }
            return Promise.all(keys.slice(0, keys.length - max).map(function(k) { return c.delete(k); }));
        });
    });
}

function offlinePage() {
    var html = '<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
        '<title>オフライン - 川霧観測・予測</title><style>body{font-family:system-ui,sans-serif;margin:0;padding:2rem 1.2rem;background:#eef3f9;color:#1d1d1f;line-height:1.7}' +
        'main{max-width:34rem;margin:auto}a{color:#0a64d2}@media(prefers-color-scheme:dark){body{background:#101218;color:#f2f2f5}a{color:#4aa3ff}}</style></head>' +
        '<body><main><h1 style="font-size:1.3rem">いまはネットにつながっていません</h1><p>このページはまだこの端末に保存されていないため、表示できません。' +
        'ホーム・地点のページ・見頃・今朝の川霧・みんなの写真は、一度開くと、つながらないときも最後に見た内容で開けます。</p>' +
        '<p><a href="/">ホーム</a> ・ <a href="/stations">地点一覧</a> ・ <a href="/flow?mode=fc">見頃</a> ・ <a href="/photos">みんなの写真</a></p>' +
        '<p><button onclick="location.reload()" style="font:inherit;padding:.5rem 1rem;border-radius:999px;border:0;background:#0a64d2;color:#fff">もう一度試す</button></p></main></body></html>';
    return new Response(html, {status: 503, headers: {"Content-Type": "text/html; charset=utf-8"}});
}

function networkFirst(req) {
    return new Promise(function(resolve) {
        var done = false;
        function fallback() {
            if (done) { return; }
            done = true;
            caches.open(PAGES).then(function(c) { return c.match(req, {ignoreVary: true}); }).then(function(hit) {
                resolve(hit || offlinePage());
            });
        }
        var timer = setTimeout(fallback, PAGE_TIMEOUT_MS);
        fetch(req).then(function(res) {
            clearTimeout(timer);
            // ログイン画面への転送や失敗は保存しない
            if (res.ok && !res.redirected && res.type === "basic") {
                var copy = res.clone();
                caches.open(PAGES).then(function(c) { return c.put(req, copy); }).then(function() { return trim(PAGES, PAGE_MAX); });
            }
            if (!done) { done = true; resolve(res); }
        }).catch(function() { clearTimeout(timer); fallback(); });
    });
}

function staleWhileRevalidate(req, name) {
    return caches.open(name).then(function(c) {
        return c.match(req).then(function(hit) {
            var net = fetch(req).then(function(res) {
                if (res.ok || res.type === "opaque") { c.put(req, res.clone()); }
                return res;
            }).catch(function() { return hit; });
            return hit || net;
        });
    });
}

self.addEventListener("fetch", function(e) {
    var req = e.request;
    if (req.method !== "GET") { return; }
    var url = new URL(req.url);
    if (url.origin === self.location.origin) {
        if (req.mode === "navigate") {
            if (PAGE_RE.test(url.pathname)) { e.respondWith(networkFirst(req)); }
            else { e.respondWith(fetch(req).catch(offlinePage)); }
            return;
        }
        if (url.pathname.indexOf("/web_static/") === 0) { e.respondWith(staleWhileRevalidate(req, ASSETS)); return; }
        if (/^\/images\/\d{4}-\d{2}-\d{2}\/\d{4}\//.test(url.pathname) || /^\/photos\/\d+(_t)?\.jpg$/.test(url.pathname)) {
            e.respondWith(caches.open(IMAGES).then(function(c) {
                return fetch(req).then(function(res) {
                    if (res.ok) { c.put(req, res.clone()).then(function() { return trim(IMAGES, IMAGE_MAX); }); }
                    return res;
                }).catch(function() { return c.match(req).then(function(hit) { return hit || Response.error(); }); });
            }));
        }
        return;
    }
    // 他のサイト(地図の背景の画像など)は扱わない(このサイトのCSPでService Workerからは取りに行けないため。
    // 地図の部品のLeafletは /web_static/vendor/leaflet/ に置いてあり、上の/web_static/の扱いで保存される)
});

self.addEventListener("message", function(e) {
    if (e.data === "rf-clear-pages") {
        e.waitUntil(Promise.all([caches.delete(PAGES), caches.delete(IMAGES)]));
    }
});
