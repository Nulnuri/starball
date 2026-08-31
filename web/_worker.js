// Cloudflare Pages 고급 모드 워커.
//
// 이 파일이 web/ 안에 있으면 Pages 는 모든 요청을 여기로 먼저 보낸다.
// 정적 파일(index.html, today.json …)은 env.ASSETS 로 그대로 넘긴다.
//
// 하는 일은 딱 하나다: 웹 푸시 구독자 명단을 KV 에 보관한다.
// 실제 발송은 깃헙 액션이 /api/subs 로 명단을 받아 직접 한다 — 발송 로직을
// 워커에 두면 VAPID 서명을 워커에서 구현해야 해서 부품이 늘어난다.
//
// 필요한 바인딩
//   SUBS               KV 네임스페이스 (구독 저장)
//   PUSH_SEND_SECRET   환경변수. /api/subs, /api/prune 을 여는 열쇠
//
// 바인딩이 없어도 사이트 자체는 정상 동작해야 한다. 알림 기능만 꺼진다.

const JSON_H = { "content-type": "application/json; charset=utf-8" };

function json(obj, status = 200, extra = {}) {
  return new Response(JSON.stringify(obj), { status, headers: { ...JSON_H, ...extra } });
}

// 같은 오리진에서만 부르므로 CORS 는 필요 없다. 다만 프리플라이트가 오면
// 조용히 허용해준다(홈 화면 웹앱이 다른 스킴으로 뜨는 경우가 있다).
function cors(req) {
  const o = req.headers.get("Origin");
  return o ? { "access-control-allow-origin": o,
              "access-control-allow-headers": "content-type",
              "access-control-allow-methods": "POST, OPTIONS" } : {};
}

// 엔드포인트 URL 을 KV 키로 쓴다. URL 이 길고 특수문자가 많아 해시로 줄인다.
async function keyOf(endpoint) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
  return "sub:" + [...new Uint8Array(buf)].slice(0, 16)
    .map(b => b.toString(16).padStart(2, "0")).join("");
}

// 브라우저가 준 구독 객체가 우리가 쓸 수 있는 모양인지 확인한다.
// 여기서 걸러내지 않으면 발송 때 매번 터진다.
function validate(sub) {
  if (!sub || typeof sub.endpoint !== "string") return "endpoint 없음";
  let u;
  try { u = new URL(sub.endpoint); } catch { return "endpoint 가 URL 이 아님"; }
  if (u.protocol !== "https:") return "https 아님";
  if (sub.endpoint.length > 1024) return "endpoint 가 너무 김";
  const k = sub.keys || {};
  if (typeof k.p256dh !== "string" || typeof k.auth !== "string") return "keys 없음";
  if (k.p256dh.length > 256 || k.auth.length > 64) return "keys 가 너무 김";
  return null;
}

async function subscribe(req, env) {
  let body;
  try { body = await req.json(); } catch { return json({ error: "본문이 JSON 이 아님" }, 400); }
  const bad = validate(body);
  if (bad) return json({ error: bad }, 400);

  const key = await keyOf(body.endpoint);
  const prev = await env.SUBS.get(key, "json");
  await env.SUBS.put(key, JSON.stringify({
    endpoint: body.endpoint,
    keys: { p256dh: body.keys.p256dh, auth: body.keys.auth },
    // 어떤 기기인지 대충 남긴다. 죽은 구독을 지울 때 참고용이다.
    ua: (req.headers.get("User-Agent") || "").slice(0, 120),
    since: prev?.since || new Date().toISOString(),
    seen: new Date().toISOString(),
  }));
  return json({ ok: true, renewed: !!prev }, 200, cors(req));
}

async function unsubscribe(req, env) {
  let body;
  try { body = await req.json(); } catch { return json({ error: "본문이 JSON 이 아님" }, 400); }
  if (typeof body.endpoint !== "string") return json({ error: "endpoint 없음" }, 400);
  await env.SUBS.delete(await keyOf(body.endpoint));
  return json({ ok: true }, 200, cors(req));
}

function authed(req, env) {
  const want = env.PUSH_SEND_SECRET;
  if (!want) return false;
  const got = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  // 길이가 같을 때만 비교해 타이밍 차이를 줄인다.
  if (got.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= got.charCodeAt(i) ^ want.charCodeAt(i);
  return diff === 0;
}

async function listSubs(env) {
  const out = [];
  let cursor;
  do {
    const page = await env.SUBS.list({ prefix: "sub:", cursor, limit: 1000 });
    for (const k of page.keys) {
      const v = await env.SUBS.get(k.name, "json");
      if (v) out.push({ key: k.name, ...v });
    }
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  return out;
}

// 발송에서 404/410 이 난 구독을 지운다. 지우지 않으면 매번 같은 실패가 쌓인다.
async function prune(req, env) {
  let body;
  try { body = await req.json(); } catch { return json({ error: "본문이 JSON 이 아님" }, 400); }
  const keys = Array.isArray(body.keys) ? body.keys.slice(0, 500) : [];
  let n = 0;
  for (const k of keys) {
    if (typeof k === "string" && k.startsWith("sub:")) { await env.SUBS.delete(k); n++; }
  }
  return json({ ok: true, deleted: n });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname;

    if (!p.startsWith("/api/")) return env.ASSETS.fetch(req);

    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(req) });

    // 알림 기능을 켤 수 있는 상태인지 웹앱이 물어보는 곳.
    // KV 바인딩이 없으면 버튼을 아예 숨기게 만든다.
    if (p === "/api/health") {
      return json({ kv: !!env.SUBS, secret: !!env.PUSH_SEND_SECRET }, 200, cors(req));
    }

    if (!env.SUBS) return json({ error: "구독 저장소가 연결되지 않았습니다" }, 503, cors(req));

    if (p === "/api/subscribe"   && req.method === "POST") return subscribe(req, env);
    if (p === "/api/unsubscribe" && req.method === "POST") return unsubscribe(req, env);

    if (p === "/api/subs"  && req.method === "GET") {
      if (!authed(req, env)) return json({ error: "권한 없음" }, 401);
      const subs = await listSubs(env);
      return json({ count: subs.length, subs });
    }
    if (p === "/api/prune" && req.method === "POST") {
      if (!authed(req, env)) return json({ error: "권한 없음" }, 401);
      return prune(req, env);
    }

    return json({ error: "없는 경로" }, 404);
  },
};
