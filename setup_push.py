#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare Pages 프로젝트에 웹 푸시용 부품을 붙인다. 한 번만 돌리면 된다.

붙이는 것 두 가지
  SUBS               구독자를 담는 KV 네임스페이스
  PUSH_SEND_SECRET   /api/subs 를 여는 열쇠 (비밀 환경변수)

여러 번 돌려도 같은 상태가 된다. 이미 있으면 만들지 않고 그대로 쓴다.

토큰에 Workers KV 권한이 없으면 KV 생성 단계에서 막힌다. 그때는 대시보드에서
네임스페이스만 손으로 만들고 --kv-id 로 알려주면 나머지는 여기서 처리한다.

    CLOUDFLARE_API_TOKEN=... python setup_push.py --account <ID>
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests

API = "https://api.cloudflare.com/client/v4"


def out(msg: str = "") -> None:
    print(msg, flush=True)


class Cf:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def call(self, method: str, path: str, **kw) -> dict:
        r = self.s.request(method, API + path, timeout=30, **kw)
        try:
            d = r.json()
        except ValueError:
            raise SystemExit(f"응답이 JSON 이 아닙니다 ({r.status_code}): {r.text[:200]}")
        if not d.get("success"):
            for e in d.get("errors", []):
                out(f"   오류 {e.get('code')}: {e.get('message')}")
        return d


def find_or_create_kv(cf: Cf, account: str, title: str) -> str | None:
    base = f"/accounts/{account}/storage/kv/namespaces"
    d = cf.call("GET", base, params={"per_page": 100})
    if not d.get("success"):
        out("   → 토큰에 Workers KV Storage 권한이 없습니다.")
        return None
    for n in d["result"]:
        if n["title"] == title:
            out(f"   이미 있습니다: {title} ({n['id']})")
            return n["id"]
    out(f"   새로 만듭니다: {title}")
    d = cf.call("POST", base, json={"title": title})
    if not d.get("success"):
        return None
    return d["result"]["id"]


def bind(cf: Cf, account: str, project: str, kv_id: str, secret: str) -> bool:
    # production 과 preview 양쪽에 넣는다. preview 를 빼면 미리보기 배포에서
    # 알림 기능만 조용히 죽어서 원인을 찾기 어렵다.
    cfg = {
        "kv_namespaces": {"SUBS": {"namespace_id": kv_id}},
        "env_vars": {"PUSH_SEND_SECRET": {"type": "secret_text", "value": secret}},
    }
    d = cf.call("PATCH", f"/accounts/{account}/pages/projects/{project}",
                json={"deployment_configs": {"production": cfg, "preview": cfg}})
    if not d.get("success"):
        return False
    prod = d["result"]["deployment_configs"]["production"]
    out(f"   KV 바인딩 : {list((prod.get('kv_namespaces') or {}).keys())}")
    out(f"   환경변수  : {list((prod.get('env_vars') or {}).keys())}")
    return True


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="웹 푸시용 Cloudflare 설정")
    ap.add_argument("--account", default=os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""))
    ap.add_argument("--project", default="starball")
    ap.add_argument("--kv-title", default="starball-subs")
    ap.add_argument("--kv-id", default="", help="대시보드에서 만든 네임스페이스 ID")
    args = ap.parse_args()

    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    secret = os.environ.get("PUSH_SEND_SECRET", "").strip()
    if not token:
        raise SystemExit("CLOUDFLARE_API_TOKEN 이 없습니다")
    if not args.account:
        raise SystemExit("계정 ID 가 없습니다 (--account 또는 CLOUDFLARE_ACCOUNT_ID)")
    if not secret:
        raise SystemExit("PUSH_SEND_SECRET 이 없습니다")

    cf = Cf(token)

    out("1) 구독 저장소(KV) 확보")
    kv_id = args.kv_id or find_or_create_kv(cf, args.account, args.kv_title)
    if not kv_id:
        out()
        out("KV 를 만들 수 없습니다. 대시보드에서 직접 만들어 주세요:")
        out("   Storage & Databases → KV → Create instance")
        out(f"   이름은 정확히 '{args.kv_title}' 로 하시고, 다시 이 작업을 실행하세요.")
        return 20
    out(f"   ID: {kv_id}")

    out()
    out("2) Pages 프로젝트에 붙이기")
    if not bind(cf, args.account, args.project, kv_id, secret):
        out()
        out("붙이지 못했습니다. 프로젝트 이름이 맞는지 확인하세요"
            f" (지금 값: {args.project}).")
        return 21

    out()
    out("완료. 다음 배포부터 알림 기능이 켜집니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
