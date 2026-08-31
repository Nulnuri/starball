#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""백테스트용 경기 로그를 만든다.

시즌의 완료된 경기를 전부 훑어 팀 성적·투수 등판 기록을 한 파일로 뽑는다.
backtest.py 가 이 파일만 읽고 시점별(point-in-time) 상태를 재구성한다.

    python build_gamelog.py [연도]      → gamelog_2026.json

한 번 받으면 캐시가 남아 재실행은 빠르다.
"""

from __future__ import annotations

import json
import sys
from datetime import date

import starball_predictor as S


def build(year: int) -> dict:
    client = S.NaverKBO()
    today = S.today_kst()

    ids = []
    for month in range(3, 12):
        try:
            entries = client.calendar(year, month)
        except Exception:
            continue
        for entry in entries:
            ymd = entry.get("ymd")
            if not ymd or date.fromisoformat(ymd) >= today:
                continue
            if not client.is_regular(year, ymd):
                continue              # 시범경기 제외 (roundCode kbo_e)
            for gi in entry.get("gameInfos") or []:
                # 올스타전(EA/WE) 제외 — 구단 코드가 아닌 경기는 통계에 안 넣는다
                if gi.get("statusCode") == "RESULT" and client.is_team_game(gi):
                    ids.append((ymd, gi["gameId"]))
    ids.sort()
    print(f"완료 경기 {len(ids)}건 수집 시작 (첫 실행은 수 분)", file=sys.stderr)

    games = []
    for i, (ymd, gid) in enumerate(ids, 1):
        try:
            g = client.game(gid)
            rd = client.record(gid)
        except Exception as e:
            print(f"  건너뜀 {gid}: {type(e).__name__}", file=sys.stderr)
            continue
        box = (rd or {}).get("teamPitchingBoxscore") or {}
        pit = (rd or {}).get("pitchersBoxscore") or {}
        if not box.get("home") or not box.get("away"):
            continue

        def side(key: str) -> dict:
            b = box.get(key) or {}
            # 투수 피안타/삼진은 '상대 타선이 친 안타 / 당한 삼진'이다.
            return {"hr_allowed": S.fnum(b.get("hr")),
                    "hit_allowed": S.fnum(b.get("hit")),
                    "k_thrown": S.fnum(b.get("kk")),
                    "r_allowed": S.fnum(b.get("r")),
                    "er_allowed": S.fnum(b.get("er"))}

        def pitchers(key: str) -> list:
            out = []
            for order, p in enumerate(pit.get(key) or []):
                ip = S.parse_kbo_innings(p.get("inn"))
                if ip <= 0 and order > 0:
                    continue
                out.append({"pcode": str(p.get("pcode") or ""),
                            "name": p.get("name") or "",
                            "started": order == 0,
                            "ip": round(ip, 4),
                            "er": int(S.fnum(p.get("er"))),
                            "r": int(S.fnum(p.get("r"))),
                            "hit": int(S.fnum(p.get("hit"))),
                            "bb": int(S.fnum(p.get("bb"))),
                            "hr": int(S.fnum(p.get("hr")))})
            return out

        games.append({
            "date": ymd, "gameId": gid,
            "stadium": g.get("stadium", ""),
            "home": g["homeTeamCode"], "away": g["awayTeamCode"],
            "home_score": int(S.fnum(g.get("homeTeamScore"))),
            "away_score": int(S.fnum(g.get("awayTeamScore"))),
            # 투수 기준 기록이라, home 의 hr_allowed = away 타선이 친 홈런
            "box": {"home": side("home"), "away": side("away")},
            "pitchers": {"home": pitchers("home"), "away": pitchers("away")},
        })
        if i % 100 == 0:
            print(f"  {i}/{len(ids)}", file=sys.stderr)

    return {"season": year, "built": today.isoformat(), "games": games}


def main(argv: list) -> int:
    year = int(argv[1]) if len(argv) > 1 else S.today_kst().year
    data = build(year)
    path = f"gamelog_{year}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"\n{path} · 경기 {len(data['games'])}건", file=sys.stderr)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main(sys.argv))
