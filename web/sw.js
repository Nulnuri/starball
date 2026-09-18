// 홈화면에서 열었을 때 껍데기가 즉시 뜨게 하고, 데이터는 항상 새로 받는다.
// 예측값을 캐시에서 주면 어제 값을 보여주게 되므로 절대 캐시하지 않는다.
const SHELL = "starball-shell-v5";
const FILES = ["./", "./manifest.webmanifest",
               "./icon-192.png", "./icon-512.png"];

// 껍데기도 없고 네트워크도 안 될 때 내놓을 최소 화면.
//
// 예전에는 이 자리에서 `Response.error()` 를 돌려줬다. 그게 브라우저의
// "페이지에 연결할 수 없음" 화면 그 자체다 — 알림을 눌렀는데 오류만 뜨고
// 넘어가지 않는다는 신고의 정체였다. 오류 화면을 브라우저에 맡기면 사용자는
// 앱이 죽은 줄 안다. 최소한 무슨 일인지 말하고 다시 시도할 길을 준다.
const OFFLINE = `<!doctype html><html lang=ko><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>스타볼 예측기</title>
<style>body{margin:0;display:grid;place-items:center;min-height:100svh;
background:#131011;color:#f5f3f2;font:16px/1.6 system-ui,sans-serif;text-align:center}
div{padding:24px;max-width:22rem}a{display:inline-block;margin-top:18px;padding:12px 22px;
border-radius:999px;background:#c8102e;color:#fff;text-decoration:none;font-weight:700}
p{color:#a29c9a;font-size:14px}</style>
<div><h1>잠깐 연결이 안 됩니다</h1>
<p>네트워크가 끊겼거나 앱 데이터가 아직 준비되지 않았습니다.<br>
잠시 뒤 다시 눌러 주세요.</p>
<a href="./">다시 열기</a></div>`;

function offlinePage() {
  return new Response(OFFLINE, {
    status: 200,
    headers: { "content-type": "text/html; charset=utf-8",
               "cache-control": "no-store" },
  });
}

// 리다이렉트를 거친 응답은 내비게이션에 그대로 돌려줄 수 없다 — 브라우저가
// 거부하고 오류 화면을 띄운다. 몸통만 꺼내 새 응답으로 다시 포장한다.
async function unredirect(r) {
  if (!r || !r.redirected) return r;
  const body = await r.blob();
  return new Response(body, {
    status: 200, statusText: "OK", headers: r.headers,
  });
}

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
      const home = new URL("./", location).href;

      // **네트워크 우선이어야 한다.** 예전에는 캐시를 먼저 돌려줬는데,
      // 그러면 껍데기를 고쳐 배포해도 폰에 영영 가지 않는다. sw.js 가
      // 바뀔 때만 다시 받으니까. 실제로 'LG 앱으로 가기' 링크가 틀려서
      // 고쳤는데, 그 수정이 이미 설치된 폰에는 닿지 않았다.
      //
      // 그렇다고 네트워크만 기다리면 지하철에서 앱이 안 열린다. 그래서
      // 짧게 기다려 보고(2.5초), 늦으면 캐시를 주되 받아온 것은 캐시에
      // 반영해 다음 번에 최신이 되게 한다.
      //
      // **`e.request` 를 그대로 다시 던지면 안 된다.** 주소가
      // `/index.html` 이면 308 이 돌아오고, 리다이렉트를 거친 응답을
      // 내비게이션에 돌려주면 브라우저가 거부해 "페이지에 연결할 수 없음"
      // 이 뜬다. 항상 './' 를 새로 받아 리다이렉트 자체를 없앤다.
      const net = (async () => {
        const r = await fetch(home, { cache: "no-store" });
        if (!r || !r.ok) throw new Error("bad response");
        caches.open(SHELL).then(c => c.put(home, r.clone())).catch(() => {});
        return r;
      })();

      const slow = new Promise(res => setTimeout(() => res(null), 2500));
      try {
        const won = await Promise.race([net.catch(() => null), slow]);
        if (won) return await unredirect(won);
      } catch (err) { /* 아래에서 처리 */ }

      // 네트워크가 늦거나 끊겼다. 캐시로 화면을 띄운다. 위의 net 은 계속
      // 돌면서 캐시를 갱신하므로 다음 번 열 때는 최신이 된다.
      const shell = await caches.match(home);
      if (shell) return await unredirect(shell);

      // 캐시도 없다 — 첫 실행인데 오프라인인 경우다.
      try {
        const last = await net;
        if (last) return await unredirect(last);
      } catch (err) { /* 아래 */ }
      return offlinePage();     // 오류 화면을 브라우저에 맡기지 않는다
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
    // 폴백을 `./index.html` 로 두면 안 된다 — Cloudflare 가 그 주소만
    // 308 로 되돌려서, 알림을 눌렀을 때 리다이렉트 응답이 내비게이션에
    // 돌아가고 브라우저가 "페이지에 연결할 수 없음" 을 띄운다.
    data: { url: d.url || "./" },
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
        // navigate 가 실패해도 조용히 넘어가면 사용자는 낡은 화면을 본다.
        // 실패하면 새 창으로라도 연다.
        if ("navigate" in w) {
          try { await w.navigate(target); return; } catch (err) { /* 아래로 */ }
        } else {
          return;
        }
        break;
      }
    }
    try { await self.clients.openWindow(target); }
    catch (err) { await self.clients.openWindow(new URL("./", location).href); }
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
