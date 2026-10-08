/* チャットの通知用の最小限のService Worker(base.htmlが登録する)。
   Androidなど`new Notification()`が使えない環境でも通知を出せるようにするためだけのもので、
   キャッシュやオフライン動作はしない。通知を押すと、そのチャットを開く(開いているタブがあればそれを使う)。 */
self.addEventListener("install", function() { self.skipWaiting(); });
self.addEventListener("activate", function(e) { e.waitUntil(self.clients.claim()); });
self.addEventListener("notificationclick", function(e) {
    e.notification.close();
    var url = (e.notification.data && e.notification.data.url) || "/";
    if (!/^\/[^/]/.test(url)) { url = "/"; }
    e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(function(list) {
        for (var i = 0; i < list.length; i++) {
            var c = list[i];
            if (new URL(c.url).origin === self.location.origin && "focus" in c) {
                return c.focus().then(function(w) { return w && w.navigate ? w.navigate(url) : null; });
            }
        }
        return self.clients.openWindow(url);
    }));
});
