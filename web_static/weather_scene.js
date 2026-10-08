/* 地点ページ等の背景の空を、Canvasで動かす(Appleの「天気」アプリ風、2026-10-05)。
   .wx-scene(_weather_scene.html)の data-kind / data-level / data-phase を読み、
   霧のかたまり・雲・雨・星を描いて流す。マウスとスクロールに合わせて層ごとに少しずつずらし、
   奥行きを出す(手前の霧ほど大きく動く)。
   - CSSだけの霧(.wx-fogbank等)はJSが動かない場合の代わりとして残し、JSが動いたら隠す。
   - 描画は画面の半分の解像度で行い(霧はぼけているので見た目は変わらない)、約15fpsに抑える
     (2026-10-08に30→15fps。上に重なるガラスの面のぼかしが描くたびに計算し直しになり重かった)。
     (スクロール中に描くのを止めると、スクロールに合わせて動く霧が止まった後にずれて見えたので、止めない)
   - タブが裏にある間は止め、「動きを減らす」設定の利用者には1コマだけ描いて止める。 */
(function() {
    "use strict";
    var scene = document.querySelector(".wx-scene");
    if (!scene || !window.requestAnimationFrame) { return; }
    var canvas = document.createElement("canvas");
    var ctx = canvas.getContext && canvas.getContext("2d");
    if (!ctx) { return; }
    canvas.className = "wx-canvas";
    scene.appendChild(canvas);
    scene.classList.add("wx-js");

    var kind = scene.getAttribute("data-kind") || "clear";
    var level = parseInt(scene.getAttribute("data-level"), 10) || 1;
    var phase = scene.getAttribute("data-phase") || "day";
    var style = getComputedStyle(scene);
    var fogRGB = (style.getPropertyValue("--wx-fog") || "255, 255, 255").trim();
    var fogA = parseFloat(style.getPropertyValue("--wx-fog-alpha")) || 0;
    var cloudA = parseFloat(style.getPropertyValue("--wx-cloud-alpha")) || 0;
    var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var SCALE = 0.5;
    var W = 0, H = 0, unit = 1;

    function rand(a, b) { return a + Math.random() * (b - a); }

    // 霧のかたまり1つ分の絵(白→透明の円)を先に作っておき、拡大縮小して何度も描く。
    var sprite = document.createElement("canvas");
    sprite.width = sprite.height = 256;
    (function() {
        var s = sprite.getContext("2d");
        var g = s.createRadialGradient(128, 128, 0, 128, 128, 128);
        g.addColorStop(0, "rgba(" + fogRGB + ", 1)");
        g.addColorStop(0.45, "rgba(" + fogRGB + ", 0.55)");
        g.addColorStop(1, "rgba(" + fogRGB + ", 0)");
        s.fillStyle = g;
        s.fillRect(0, 0, 256, 256);
    })();

    var puffs = [], clouds = [], drops = [], stars = [];

    function build() {
        puffs = []; clouds = []; drops = []; stars = [];
        var nFog = 0;
        if (kind === "riverfog") { nFog = [0, 16, 26, 36][level] || 16; }
        else if (kind === "fog") { nFog = [0, 30, 42, 56][level] || 30; }
        else if (kind === "rain" && level === 1) { nFog = 10; }
        for (var i = 0; i < nFog; i++) {
            var d = rand(0.35, 1);  // 奥行き(1が手前)
            var y;
            if (kind === "riverfog") {
                // 川霧は低いところ(画面の下の方)に溜まり、上の方は薄く
                y = H * (0.28 + 0.78 * Math.pow(Math.random(), 0.65));
            } else {
                y = H * rand(-0.1, 1.1);
            }
            puffs.push({
                x: rand(-0.2, 1.2) * W, y: y, d: d,
                r: rand(140, 340) * (0.55 + d * 0.65) * unit,
                vx: rand(10, 34) * (0.4 + d) * (kind === "fog" ? 0.8 : 1),
                a: Math.min(1, (fogA || 0.5) * rand(0.22, 0.5)),
                ph: rand(0, Math.PI * 2), bob: rand(4, 14)
            });
        }
        var nCloud = cloudA > 0 ? Math.round(5 + cloudA * 10) : (kind === "clear" ? 4 : 0);
        for (var c = 0; c < nCloud; c++) {
            var cd = rand(0.3, 0.8);
            clouds.push({
                x: rand(-0.2, 1.2) * W, y: H * rand(-0.05, 0.3), d: cd,
                r: rand(180, 360) * unit, sx: rand(1.6, 2.6),
                vx: rand(5, 14) * (0.5 + cd),
                a: Math.min(1, (cloudA || 0.18) * rand(0.35, 0.6)),
                ph: rand(0, Math.PI * 2)
            });
        }
        if (kind === "rain") {
            var nDrop = level === 1 ? 110 : 230;
            for (var k = 0; k < nDrop; k++) {
                drops.push({ x: rand(0, W), y: rand(0, H), len: rand(10, 26), v: rand(650, 1000), a: rand(0.18, 0.42) });
            }
        }
        if (phase === "night" && (kind === "clear" || kind === "riverfog")) {
            for (var s = 0; s < 160; s++) {
                stars.push({ x: rand(0, W), y: H * Math.pow(Math.random(), 1.4) * 0.7, r: rand(0.5, 1.5), ph: rand(0, Math.PI * 2), sp: rand(0.6, 2) });
            }
        }
    }

    function resize() {
        W = window.innerWidth; H = window.innerHeight;
        unit = Math.max(0.6, Math.min(1.4, W / 1280));
        canvas.width = Math.ceil(W * SCALE);
        canvas.height = Math.ceil(H * SCALE);
        build();
    }

    // マウス・スクロールによる視差(目標値に向けてなめらかに追従させる)
    var mx = 0, my = 0, tmx = 0, tmy = 0;
    window.addEventListener("mousemove", function(e) {
        tmx = (e.clientX / Math.max(1, W)) * 2 - 1;
        tmy = (e.clientY / Math.max(1, H)) * 2 - 1;
    }, { passive: true });

    function draw(t, dt) {
        mx += (tmx - mx) * Math.min(1, dt * 2.5);
        my += (tmy - my) * Math.min(1, dt * 2.5);
        var scroll = window.scrollY || 0;
        scene.style.setProperty("--wx-mx", mx.toFixed(3));
        scene.style.setProperty("--wx-my", my.toFixed(3));

        ctx.setTransform(SCALE, 0, 0, SCALE, 0, 0);
        ctx.clearRect(0, 0, W, H);

        // 星
        for (var s = 0; s < stars.length; s++) {
            var st = stars[s];
            ctx.globalAlpha = 0.35 + 0.65 * (0.5 + 0.5 * Math.sin(t * st.sp + st.ph));
            ctx.fillStyle = "#fff";
            ctx.fillRect(st.x - mx * 4, st.y - scroll * 0.02, st.r * 2, st.r * 2);
        }

        // 雲(横長に引き伸ばした霧の絵)
        for (var c = 0; c < clouds.length; c++) {
            var cl = clouds[c];
            cl.x += cl.vx * dt;
            if (cl.x - cl.r * cl.sx > W) { cl.x = -cl.r * cl.sx; }
            ctx.globalAlpha = cl.a * (0.85 + 0.15 * Math.sin(t * 0.25 + cl.ph));
            var cw = cl.r * 2 * cl.sx, ch = cl.r * 1.1;
            ctx.drawImage(sprite, cl.x - cw / 2 - mx * 12 * cl.d, cl.y - ch / 2 - my * 6 * cl.d - scroll * 0.05 * cl.d, cw, ch);
        }

        // 霧(奥から手前の順に描く)
        for (var i = 0; i < puffs.length; i++) {
            var p = puffs[i];
            p.x += p.vx * dt;
            if (p.x - p.r > W * 1.05) { p.x = -p.r * 1.2; }
            var y = p.y + Math.sin(t * 0.22 + p.ph) * p.bob - scroll * 0.12 * p.d;
            ctx.globalAlpha = p.a * (0.78 + 0.22 * Math.sin(t * 0.3 + p.ph * 1.7));
            var size = p.r * 2;
            ctx.drawImage(sprite, p.x - p.r - mx * 30 * p.d, y - p.r * 0.6 - my * 14 * p.d, size, size * 0.6);
        }

        // 雨
        if (drops.length) {
            ctx.globalAlpha = 1;
            ctx.lineWidth = 1.2;
            ctx.lineCap = "round";
            for (var k = 0; k < drops.length; k++) {
                var dr = drops[k];
                dr.y += dr.v * dt;
                dr.x -= dr.v * 0.18 * dt;
                if (dr.y > H + 30) { dr.y = -30; dr.x = rand(0, W * 1.2); }
                ctx.strokeStyle = "rgba(255, 255, 255, " + dr.a + ")";
                ctx.beginPath();
                ctx.moveTo(dr.x, dr.y);
                ctx.lineTo(dr.x + dr.len * 0.18, dr.y - dr.len);
                ctx.stroke();
            }
        }
        ctx.globalAlpha = 1;
    }

    var last = 0, running = true, acc = 0;
    function frame(now) {
        if (!running) { return; }
        var dt = last ? Math.min(0.1, (now - last) / 1000) : 0.016;
        last = now;
        acc += dt;
        if (acc >= 1 / 16) {  // 約15fps
            draw(now / 1000, acc);
            acc = 0;
        }
        window.requestAnimationFrame(frame);
    }

    resize();
    var resizeTimer = null;
    window.addEventListener("resize", function() {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function() { resize(); if (reduceMotion) { draw(0, 0); } }, 150);
    });
    if (reduceMotion) {
        draw(0, 0);
        return;
    }
    document.addEventListener("visibilitychange", function() {
        if (document.hidden) { running = false; }
        else if (!running) { running = true; last = 0; window.requestAnimationFrame(frame); }
    });
    window.requestAnimationFrame(frame);
})();
