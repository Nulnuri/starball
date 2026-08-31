// 홈화면에서 열었을 때 껍데기가 즉시 뜨게 하고, 데이터는 항상 새로 받는다.
// 예측값을 캐시에서 주면 어제 값을 보여주게 되므로 절대 캐시하지 않는다.
const SHELL = "starball-shell-v1";
const FILES = ["./index.html", "./manifest.webmanifest",
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
