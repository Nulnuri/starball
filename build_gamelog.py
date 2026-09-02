#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""백테스트용 경기 로그를 만든다.

시즌의 완료된 경기를 전부 훑어 팀 성적·투수 등판 기록을 한 파일로 뽑는다.
backtest.py 가 이 파일만 읽고 시점별(point-in-time) 상태를 재구성한다.

    python build_gamelog.py [연도]      → gamelog_2026.json
    python build_gamelog.py --full      → 처음부터 다시 받는다

이미 있는 파일은 읽어서 새 경기만 덧붙인다. 매일 돌려도 요청이 몇 건뿐이라
워크플로에서 자동으로 돌린다. 이 파일이 내년 시즌 학습의 재료다.
"""

from __future__ import annotations

import json
import sys
from datetime import date

import starball_predictor as S

GAMES = "games"
ADDED = "added"
GAME_ID = "gameId"


def load_existing(path: str) -> list:
    """이미 받아둔 경기 목록. 파일이 없거나 깨졌으면 빈 목록."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, ValueError):
        return []
    return d.get(GAMES, []) if isinstance(d, dict) else list(d)


def build(year: int, full: bool = False) -> dict:
    client = S.NaverKBO()
    today = S.today_kst()

    prev = [] if full else load_existing(f"gamelog_{year}.json")
    have = {g.get("gameId") for g in prev if g.get("gameId")}
    if have:
        print(f"이미 있는 경기 {len(have)}건 — 새 경기만 받는다", file=sys.stderr)

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
                if gi.get("statusCode") != "RESULT" or not client.is_team_game(gi):
                    continue
                if gi["gameId"] in have:
                    continue                      # 이미 받아둔 경기
                ids.append((ymd, gi["gameId"]))
    ids.sort()
    print(f"새로 받을 경기 {len(ids)}건", file=sys.stderr)

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

    # 날짜순으로 합친다. 같은 gameId 는 새로 받은 쪽을 쓴다.
    merged = {g[GAME_ID]: g for g in prev if g.get(GAME_ID)}
    merged.update({g[GAME_ID]: g for g in games})
    out = sorted(merged.values(), key=lambda g: (g.get("date", ""), g.get(GAME_ID, "")))
    return {"season": year, "built": today.isoformat(),
            ADDED: len(games), GAMES: out}


def main(argv: list) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    full = "--full" in argv
    year = int(args[0]) if args else S.today_kst().year

    data = build(year, full=full)
    path = f"gamelog_{year}.json"
    total = len(data[GAMES])
    added = data[ADDED]

    # 새로 받은 게 없으면 파일을 다시 쓰지 않는다. 매일 돌리는 자리라,
    # 내용이 같은데도 커밋이 생기면 기록이 지저분해진다.
    if added == 0 and not full:
        print(f"{path} · 새 경기 없음 (총 {total}건, 그대로 둠)", file=sys.stderr)
        return 0

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"{path} · 총 {total}건 (새로 {added}건)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main(sys.argv))
