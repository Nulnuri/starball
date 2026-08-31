#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""웹앱 구독자에게 웹 푸시를 보낸다.

ntfy 는 내가 쓰는 채널이고, 이쪽은 지인용이다. 지인은 앱을 깔지 않고
사이트를 열어 '알림 받기' 만 누르면 된다.

구독자 명단은 Cloudflare Pages 의 워커(web/_worker.js)가 KV 에 갖고 있다.
여기서는 명단을 받아 발송하고, 죽은 구독을 되돌려 지우게 한다.
죽은 구독을 방치하면 매 실행마다 같은 실패가 쌓여 로그를 못 믿게 된다.

    python push_send.py --site https://starball-9oj.pages.dev
    python push_send.py --site ... --reminder      # 마감 임박용 짧은 알림

환경변수
    VAPID_PRIVATE_KEY   PEM. 발송 서명용
    PUSH_SEND_SECRET    워커의 /api/subs 를 여는 열쇠
    VAPID_SUBJECT       선택. 기본 mailto:starball@example.invalid
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# 윈도우 콘솔은 기본이 cp949 라 이모지에서 터진다. 로그가 죽으면 발송 결과를
# 확인할 수 없으므로 출력 스트림을 먼저 고정한다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

KST = timezone(timedelta(hours=9))

# 푸시 서비스가 이 상태를 주면 그 구독은 영구히 끝난 것이다.
# 그 밖의 실패(429, 5xx)는 다음 실행에서 다시 시도한다.
DEAD = (404, 410)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fetch_subs(site: str, secret: str) -> list[dict]:
    r = requests.get(f"{site}/api/subs", timeout=30,
                     headers={"Authorization": f"Bearer {secret}"})
    if r.status_code == 401:
        raise SystemExit("구독 목록 권한이 없습니다 — PUSH_SEND_SECRET 이 워커와 다릅니다")
    if r.status_code == 503:
        raise SystemExit("워커에 KV 가 연결되지 않았습니다 — Pages 설정에서 SUBS 바인딩 확인")
    r.raise_for_status()
    return r.json().get("subs", [])


def prune(site: str, secret: str, keys: list[str]) -> None:
    if not keys:
        return
    r = requests.post(f"{site}/api/prune", timeout=30,
                      headers={"Authorization": f"Bearer {secret}"},
                      json={"keys": keys})
    if r.ok:
        log(f"   죽은 구독 {r.json().get('deleted', 0)}건 정리")
    else:
        log(f"   구독 정리 실패({r.status_code}) — 다음 실행에서 다시 시도됩니다")


def build_payload(today: dict, reminder: bool, site: str) -> dict | None:
    """today.json 을 알림 한 통으로 줄인다. 첫 줄만 보고 입력할 수 있어야 한다."""
    g = today.get("game")
    ms = today.get("missions") or []
    if not g or len(ms) < 3:
        return None

    picks = " · ".join(str(m.get("pick", "?")) for m in ms)
    # lgIsHome 이 False 면 LG 가 원정이다. 웹앱(index.html)과 같은 규칙을 쓴다.
    away, home = (g["opp"], g["lg"]) if g.get("lgIsHome") else (g["lg"], g["opp"])
    conf = (today.get("joint") or {}).get("confidence", "")

    if reminder:
        title = f"⏰ 스타볼 마감 임박 · {picks}"
        body = f"{away}@{home} {g.get('time','')} 시작. 아직 입력 안 하셨다면 지금."
    else:
        title = f"⚾ 오늘의 스타볼 · {picks}"
        lines = [f"{away}@{home} {g.get('stadium','')} {g.get('time','')}"
                 + (f" · 신뢰도 {conf}" if conf else "")]
        for m in ms:
            p = m.get("prob")
            lines.append(f"{m.get('label','')} → {m.get('pick','')}"
                         + (f"  {p*100:.0f}%" if isinstance(p, (int, float)) else ""))
        body = "\n".join(lines)

    return {"title": title, "body": body, "url": f"{site}/index.html",
            "tag": "starball"}


def main() -> int:
    ap = argparse.ArgumentParser(description="웹앱 구독자에게 웹 푸시 발송")
    ap.add_argument("--site", required=True, help="배포 주소 (예: https://starball-9oj.pages.dev)")
    ap.add_argument("--today", default="web/today.json", help="예측 데이터 경로")
    ap.add_argument("--reminder", action="store_true", help="마감 임박용 짧은 알림")
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않고 내용만 출력")
    args = ap.parse_args()

    site = args.site.rstrip("/")

    try:
        today = json.load(open(args.today, encoding="utf-8"))
    except FileNotFoundError:
        log(f"{args.today} 가 없습니다 — 예측을 먼저 실행하세요")
        return 0

    g = today.get("game") or {}
    now = datetime.now(KST)

    # 1) 오늘 것인지 확인.
    #    크론이 밀리거나 예측이 실패한 날에는 저장소에 어제 값이 남아 있다.
    #    그걸 보내면 지난 경기의 값을 오늘 것처럼 보내게 된다 — 조용히 틀리는
    #    것보다 안 보내는 쪽이 낫다.
    if g.get("date") != now.date().isoformat():
        log(f"today.json 이 오늘({now:%Y-%m-%d}) 것이 아닙니다"
            f"(={g.get('date')}) — 웹 푸시를 보내지 않습니다")
        return 0

    # 2) 마감 알림은 경기 직전에 딱 한 번만.
    #    마감 크론은 하루 세 번(12:00 / 15:00 / 16:30) 도는데, 그건 경기 시각이
    #    14:00 / 17:00 / 18:30 세 종류라서다. 구간을 안 따지면 한 경기에 세 번
    #    보내게 된다. 예측기와 같은 상수를 쓴다.
    if args.reminder:
        try:
            import starball_predictor as S
            lo, hi = S.REMINDER_WINDOW
        except Exception:
            lo, hi = 1.0, 3.0
        try:
            start = datetime.fromisoformat(f"{g['date']}T{g['time']}:00").replace(tzinfo=KST)
        except (KeyError, ValueError):
            log("경기 시각을 읽을 수 없어 마감 알림을 건너뜁니다")
            return 0
        hours = (start - now).total_seconds() / 3600
        if not (lo <= hours <= hi):
            log(f"경기까지 {hours:.1f}시간 — 마감 알림 구간({lo}~{hi}시간) 밖이라 건너뜁니다")
            return 0

    payload = build_payload(today, args.reminder, site)
    if payload is None:
        log("오늘은 경기가 없어 웹 푸시를 보내지 않습니다")
        return 0

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0

    secret = os.environ.get("PUSH_SEND_SECRET", "").strip()
    priv = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not secret or not priv:
        log("VAPID_PRIVATE_KEY / PUSH_SEND_SECRET 이 없어 웹 푸시를 건너뜁니다")
        return 0

    # 무거운 의존성이라 실제로 보낼 때만 불러온다.
    from pywebpush import WebPushException, webpush

    subs = fetch_subs(site, secret)
    if not subs:
        log("구독자가 없습니다 — 웹앱에서 '알림 받기' 를 누른 사람이 아직 없습니다")
        return 0

    data = json.dumps(payload, ensure_ascii=False)
    claims = {"sub": os.environ.get("VAPID_SUBJECT", "mailto:starball@example.invalid")}
    # 만료는 12시간 후로 둔다. 기기가 꺼져 있어도 다음 알림 전에는 도착한다.
    ttl = 12 * 3600

    sent = failed = 0
    dead: list[str] = []
    for s in subs:
        try:
            webpush(subscription_info={"endpoint": s["endpoint"], "keys": s["keys"]},
                    data=data, vapid_private_key=priv, vapid_claims=dict(claims),
                    ttl=ttl, timeout=20)
            sent += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in DEAD:
                dead.append(s["key"])
            else:
                failed += 1
                log(f"   실패({code}) {s['endpoint'][:48]}… {str(e)[:80]}")
        except Exception as e:                      # 네트워크 등
            failed += 1
            log(f"   실패 {s['endpoint'][:48]}… {str(e)[:80]}")

    log(f"웹 푸시 — 성공 {sent} / 실패 {failed} / 만료 {len(dead)}"
        f"  (구독자 {len(subs)}, {datetime.now(KST):%H:%M})")
    prune(site, secret, dead)

    # 한 명도 못 보냈고 만료도 아니면 설정 문제일 수 있다. 워크플로가 알아채게 한다.
    return 1 if sent == 0 and failed else 0


if __name__ == "__main__":
    sys.exit(main())
