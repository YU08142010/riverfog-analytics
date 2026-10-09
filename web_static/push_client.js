/* Web Push(2026-10-09)のブラウザ側。設定画面(settings.html)の「この端末で通知を受け取る」と、
   全ページでの購読情報の同期に使う。設定値は base.html の window.RF_PUSH。
   - 購読(subscribe)はsw.js(範囲は /web_static/)の登録に対して行う。ページはその範囲の外なので
     navigator.serviceWorker.ready は使えず、登録したワーカーが有効になるのを自分で待つ。
   - この端末で受け取っているかはlocalStorageの rf-push に覚える(タブを開いている間のチャット通知と
     二重に出さないため。base.html)。ブラウザ側で購読が変わっていたら、次に開いたときサーバーに送り直す。 */
(function() {
    "use strict";
    var C = window.RF_PUSH;
    if (!C) { return; }

    function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
    function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

    function supported() {
        return !!(window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window);
    }
    function isIos() { return /iphone|ipad|ipod/i.test(navigator.userAgent); }
    function isStandalone() {
        return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
            (window.matchMedia && window.matchMedia("(display-mode: fullscreen)").matches) || !!window.navigator.standalone;
    }

    function keyBytes(b64) {
        var pad = "=".repeat((4 - b64.length % 4) % 4);
        var raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
        var out = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i++) { out[i] = raw.charCodeAt(i); }
        return out;
    }

    function activeRegistration() {
        return navigator.serviceWorker.register(C.sw, {scope: C.scope}).then(function(r) {
            if (r.active) { return r; }
            var w = r.installing || r.waiting;
            return new Promise(function(resolve, reject) {
                if (!w) { reject(new Error("no worker")); return; }
                w.addEventListener("statechange", function() {
                    if (w.state === "activated") { resolve(r); }
                    if (w.state === "redundant") { reject(new Error("worker failed")); }
                });
            });
        });
    }

    function currentSubscription() {
        if (!supported()) { return Promise.resolve(null); }
        return navigator.serviceWorker.getRegistration(C.scope).then(function(r) {
            return r ? r.pushManager.getSubscription() : null;
        }).catch(function() { return null; });
    }

    function post(url, data) {
        return fetch(url, {
            method: "POST", body: JSON.stringify(data), credentials: "same-origin",
            headers: {"Content-Type": "application/json", "X-CSRF-Token": C.csrf, "X-Requested-With": "XMLHttpRequest"}
        }).then(function(res) {
            return res.json().catch(function() { return {}; }).then(function(j) {
                if (!res.ok || j.success === false) { throw new Error(j.message || ("HTTP " + res.status)); }
                return j;
            });
        });
    }

    function sendSubscription(sub) {
        return post(C.subscribe, sub.toJSON()).then(function(j) {
            lsSet("rf-push", "1");
            lsSet("rf-push-endpoint", sub.endpoint);
            if (j.id) { lsSet("rf-push-id", String(j.id)); }
            return j;
        });
    }

    function enable(vapidKey) {
        if (!supported()) { return Promise.reject(new Error("unsupported")); }
        return Notification.requestPermission().then(function(p) {
            if (p !== "granted") { throw new Error(p === "denied" ? "denied" : "dismissed"); }
            return activeRegistration();
        }).then(function(r) {
            return r.pushManager.getSubscription().then(function(sub) {
                return sub || r.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: keyBytes(vapidKey)});
            });
        }).then(sendSubscription);
    }

    function disable() {
        return currentSubscription().then(function(sub) {
            lsSet("rf-push", "0");
            if (!sub) { return null; }
            var endpoint = sub.endpoint;
            return sub.unsubscribe().catch(function() {}).then(function() {
                return post(C.unsubscribe, {endpoint: endpoint});
            });
        });
    }

    function sync() {
        if (lsGet("rf-push") !== "1") { return; }
        currentSubscription().then(function(sub) {
            if (!sub) { lsSet("rf-push", "0"); return; }
            if (lsGet("rf-push-endpoint") !== sub.endpoint) { sendSubscription(sub).catch(function() {}); }
        });
    }

    window.RFPush = {
        supported: supported, isIos: isIos, isStandalone: isStandalone,
        currentSubscription: currentSubscription, enable: enable, disable: disable
    };
    sync();
})();
