#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""과거 시즌 상대전적을 뽑는다 (web/h2h_history.json).

2024 이전 시즌은 리그 환경이 달라 학습에 넣지 않는다(ABS 도입, 베이스 확대,
구장 교체). 그렇다고 버릴 이유는 없어서, **상대전적 참고**로만 쓴다.
화면에 "역대 두산전" 같은 맥락을 보여주는 용도이고, 모델 계산에는 들어가지
않는다 — 5년 전 상대전적은 선수단이 통째로 바뀌어 예측력이 없다.

    python build_h2h_history.py            # 있는 로그 전부로 만든다
    python build_h2h_history.py --team LG

산출물은 작다(팀 하나 기준 상대 9팀 × 시즌 수). 앱이 매번 받으므로
가볍게 유지한다.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict

MY_TEAM = "LG"
OUT = "web/h2h_history.json"

# 팀 코드 → 이름. 과거 시즌에는 지금 없는 코드가 나올 수 있어 원본도 남긴다.
NAMES = {"LG": "LG", "OB": "두산", "SS": "삼성", "KT": "KT", "WO": "키움",
         "SK": "SSG", "HH": "한화", "HT": "KIA", "NC": "NC", "LT": "롯데"}


def seasons() -> list:
    out = []
    for p in sorted(glob.glob("gamelog_*.json")):
        m = re.search(r"gamelog_(\d{4})\.json$", p.replace("\\", "/"))
        if m:
            out.append((int(m.group(1)), p))
    return out


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="과거 상대전적 집계")
    ap.add_argument("--team", default=MY_TEAM)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    found = seasons()
    if not found:
        raise SystemExit("경기 로그가 없습니다. build_gamelog.py 를 먼저 돌리세요.")

    # (시즌, 상대) → 성적
    tally = defaultdict(lambda: {"w": 0, "l": 0, "d": 0, "rs": 0, "ra": 0,
                                 "hr": 0, "hra": 0, "g": 0})
    for year, path in found:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        games = d.get("games", d) if isinstance(d, dict) else d
        for x in games:
            hs, as_ = x.get("home_score"), x.get("away_score")
            if hs is None or as_ is None:
                continue
            if args.team not in (x.get("home"), x.get("away")):
                continue
            is_home = x.get("home") == args.team
            me, foe = ("home", "away") if is_home else ("away", "home")
            my, oy = (hs, as_) if is_home else (as_, hs)
            box = x.get("box") or {}
            t = tally[(year, x.get(foe))]
            t["g"] += 1
            t["rs"] += my
            t["ra"] += oy
            # 투수 기준 기록이라 '상대가 허용한 홈런' 이 우리 홈런이다
            t["hr"] += int((box.get(foe) or {}).get("hr_allowed") or 0)
            t["hra"] += int((box.get(me) or {}).get("hr_allowed") or 0)
            if my > oy:
                t["w"] += 1
            elif my < oy:
                t["l"] += 1
            else:
                t["d"] += 1

    by_season = defaultdict(dict)
    for (year, opp), t in tally.items():
        if not opp or t["g"] == 0:
            continue
        by_season[str(year)][opp] = {
            "name": NAMES.get(opp, opp), "g": t["g"],
            "w": t["w"], "l": t["l"], "d": t["d"],
            "rs": round(t["rs"] / t["g"], 2), "ra": round(t["ra"] / t["g"], 2),
            "hr": round(t["hr"] / t["g"], 2), "hra": round(t["hra"] / t["g"], 2),
        }

    # 상대별 합계도 같이 낸다 (화면에서 '역대' 로 보여줄 값)
    total = defaultdict(lambda: {"g": 0, "w": 0, "l": 0, "d": 0,
                                 "rs": 0.0, "ra": 0.0})
    for year, opps in by_season.items():
        for opp, v in opps.items():
            t = total[opp]
            t["g"] += v["g"]
            t["w"] += v["w"]
            t["l"] += v["l"]
            t["d"] += v["d"]
            t["rs"] += v["rs"] * v["g"]
            t["ra"] += v["ra"] * v["g"]
    overall = {opp: {"name": NAMES.get(opp, opp), "g": t["g"], "w": t["w"],
                     "l": t["l"], "d": t["d"],
                     "rs": round(t["rs"] / t["g"], 2),
                     "ra": round(t["ra"] / t["g"], 2)}
               for opp, t in total.items() if t["g"]}

    years = sorted(by_season)
    data = {"team": args.team, "seasons": years,
            "note": "참고용 기록이다. 모델 계산에는 쓰지 않는다.",
            "bySeason": dict(sorted(by_season.items())),
            "overall": dict(sorted(overall.items(),
                                   key=lambda kv: -kv[1]["g"]))}

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    size = os.path.getsize(args.out)
    print(f"{args.out} · 시즌 {len(years)}개 ({years[0]}~{years[-1]}) · "
          f"{size / 1024:.1f} KB", file=sys.stderr)
    for opp, v in list(data["overall"].items())[:12]:
        print(f"   {v['name']:<5} {v['g']:>3}경기  {v['w']}승 {v['l']}패 "
              f"{v['d']}무  득 {v['rs']:.2f} 실 {v['ra']:.2f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
