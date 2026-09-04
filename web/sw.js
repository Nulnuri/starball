// 홈화면에서 열었을 때 껍데기가 즉시 뜨게 하고, 데이터는 항상 새로 받는다.
// 예측값을 캐시에서 주면 어제 값을 보여주게 되므로 절대 캐시하지 않는다.
const SHELL = "starball-shell-v3";
const FILES = ["./", "./manifest.webmanifest",
               "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/")) return;   // 구독 API 는 캐시 대상이 아니다

  // 화면 이동 요청은 언제나 앱 껍데기를 준다.
  //
  // Cloudflare Pages 는 /index.html 을 / 로 308 리다이렉트한다. 그런데
  // 리다이렉트를 거쳐 캐시된 응답을 내비게이션에 그대로 돌려주면 브라우저가
  // 거부하고 "사이트에 연결할 수 없음" 을 띄운다. 알림을 눌러도 앱이 열리지
  // 않던 원인이 이것이다. 그래서 경로가 뭐든 './' 로 맞춰 응답한다.
  //
  // 문서(/docs/…)는 별개의 페이지라 가로채면 안 된다 — 앱 껍데기가 대신
  // 떠서 문서를 볼 수 없게 된다.
  if (e.request.mode === "navigate") {
    const root = new URL("./", location).pathname;
    const here = url.pathname.replace(/index\.html$/, "");
    if (here !== root) return;                    // 문서 등은 그냥 네트워크
    e.respondWith((async () => {
      const shell = await caches.match(new URL("./", location).href);
      if (shell && !shell.redirected) return shell;
      try {
        return await fetch(e.request);
      } catch (err) {
        return shell || Response.error();
      }
    })());
    return;
  }

  // 데이터는 네트워크 우선. 오프라인이면 마지막으로 성공한 응답을 준다.
  if (url.pathname.endsWith("today.json") || url.pathname.endsWith("history.json")) {
    e.respondWith(
      fetch(e.request).then(r => {
        const copy = r.clone();
        caches.open(SHELL).then(c => c.put(e.request, copy));
        return r;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});


// ── 웹 푸시 ─────────────────────────────────────────────────────────────
// 깃헙 액션이 보낸 알림을 받아 잠금화면에 띄운다.
// 본문은 JSON 이지만, 형식이 깨져 왔더라도 알림은 반드시 띄워야 한다.
// showNotification 을 호출하지 않으면 브라우저가 "이 사이트가 백그라운드에서
// 실행됐습니다" 같은 기본 알림을 대신 띄운다.
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data && e.data.text() }; }

  const title = d.title || "스타볼 예측";
  e.waitUntil(self.registration.showNotification(title, {
    body: d.body || "오늘 추천값이 준비됐습니다.",
    icon: "./icon-192.png",
    badge: "./icon-192.png",
    // 같은 tag 를 쓰면 아침 알림이 마감 알림으로 조용히 교체된다.
    // 알림이 두 개 쌓여 어느 게 최신인지 헷갈리는 것을 막는다.
    // 서버가 날짜 붙은 tag 를 준다. 못 받았을 때도 날짜로 갈라둔다 —
    // "starball" 로 고정하면 새 알림이 어제 것을 조용히 덮어쓴다.
    tag: d.tag || ("starball-" + new Date().toISOString().slice(0, 10)),
    renotify: true,
    data: { url: d.url || "./index.html" },
  }));
});

// 알림을 누르면 이미 열린 창이 있으면 그것을 살리고, 없으면 새로 연다.
// 새 창을 무조건 열면 홈화면 웹앱이 여러 개 겹친다.
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || "./";
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (w.url.includes(location.origin)) {
        await w.focus();
        if ("navigate" in w) { try { await w.navigate(target); } catch {} }
        return;
      }
    }
    await self.clients.openWindow(target);
  })());
});

// 브라우저가 구독을 갱신했을 때(엔드포인트가 바뀐다) 서버에 다시 알려준다.
// 이걸 빼면 어느 날 조용히 알림이 끊기고 이유를 알 수 없다.
self.addEventListener("pushsubscriptionchange", e => {
  e.waitUntil((async () => {
    const sub = e.newSubscription || await self.registration.pushManager.subscribe(
      { userVisibleOnly: true,
        applicationServerKey: e.oldSubscription?.options?.applicationServerKey });
    if (!sub) return;
    await fetch("./api/subscribe", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(sub.toJSON()),
    }).catch(() => {});
  })());
});
