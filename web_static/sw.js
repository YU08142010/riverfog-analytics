/* 通知用の最小限のService Worker(base.htmlが登録する)。キャッシュやオフライン動作はしない。
   - チャットの通知(タブを開いている間): ページからshowNotificationを呼ぶ。
   - Web Push(2026-10-09、push_notify.py): ブラウザを閉じていてもサーバーからの通知を受けて表示する。
     届く内容は {title, body, url, tag, kind}。
   通知を押すと、その画面を開く(開いているタブがあればそれを使う)。 */
self.addEventListener("install", function() { self.skipWaiting(); });
self.addEventListener("activate", function(e) { e.waitUntil(self.clients.claim()); });

var BASE = self.registration.scope.replace(/\/web_static\/?$/, "/").replace(/\/$/, "");

self.addEventListener("push", function(e) {
    var d = {};
    try { d = e.data ? e.data.json() : {}; } catch (err) { d = {body: e.data ? e.data.text() : ""}; }
    var url = typeof d.url === "string" && /^\/[^/]/.test(d.url) ? d.url : "/";
    e.waitUntil(self.registration.showNotification(d.title || "川霧観測・予測", {
        body: d.body || "",
        icon: BASE + "/web_static/icons/icon-192.png",
        badge: BASE + "/web_static/icons/icon-192.png",
        tag: d.tag || undefined,
        renotify: !!d.tag,
        data: {url: url},
        lang: "ja"
    }));
});

self.addEventListener("notificationclick", function(e) {
    e.notification.close();
    var url = (e.notification.data && e.notification.data.url) || "/";
    if (!/^\/[^/]/.test(url) && url !== "/") { url = "/"; }
    e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(function(list) {
        for (var i = 0; i < list.length; i++) {
            var c = list[i];
            if (new URL(c.url).origin === self.location.origin && "focus" in c) {
                // このSWの範囲(/web_static/)外のページのタブはnavigateできないことがあるので、そのときは新しく開く
                return c.focus().then(function(w) {
                    if (!w || !w.navigate) { return self.clients.openWindow(url); }
                    return w.navigate(url).then(function(r) { return r || self.clients.openWindow(url); },
                                                function() { return self.clients.openWindow(url); });
                });
            }
        }
        return self.clients.openWindow(url);
    }));
});
