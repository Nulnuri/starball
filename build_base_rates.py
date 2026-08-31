#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""스타볼 미션의 리그 기저 분포를 만든다.

    python build_gamelog.py        # 먼저 경기 로그
    python build_base_rates.py     # → base_rates.json

모델이 근거 없이 기저에서 벗어나면 오히려 손해라, 예측을 이 분포 쪽으로
당겨서 쓴다(starball_predictor.BASE_RATE_BLEND). 문항 정의를 바꾸면
반드시 다시 만들어야 한다 — 라벨이 안 맞으면 혼합이 무력화된다.
"""

from __future__ import annotations

import io
import json
import sys
from collections import Counter, defaultdict

import backtest as B
import starball_predictor as S


def main(argv: list) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    path = argv[1] if len(argv) > 1 else f"gamelog_{S.today_kst().year}.json"
    try:
        with io.open(path, encoding="utf-8") as f:
            games = json.load(f)["games"]
    except FileNotFoundError:
        print(f"{path} 가 없습니다. 먼저: python build_gamelog.py", file=sys.stderr)
        return 1

    keys = [q["key"] for q in S.STARBALL_QUESTIONS]
    per, joint, n = defaultdict(Counter), Counter(), 0
    for g in games:
        # 홈팀 관점으로 모은다. 팀별로 나누면 표본이 1/10 로 줄어드는데,
        # 미션 정의가 팀 무관이라 전 구단을 합치는 편이 추정이 안정적이다.
        t = B.actual_answers(g, g["home"])
        if any(t.get(k) is None for k in keys):
            continue
        n += 1
        for k in keys:
            per[k][t[k]] += 1
        joint[tuple(t[k] for k in keys)] += 1

    if n < 100:
        print(f"표본이 {n}건뿐입니다. 문항 라벨이 경기 로그와 맞는지 확인하세요.",
              file=sys.stderr)
        return 1

    out = {
        "_설명": "스타볼 미션의 리그 기저 분포. 모델을 이 쪽으로 당겨서 쓴다.",
        "_출처": f"{S.today_kst().year}시즌 정규경기 {n}건 (홈팀 관점)",
        "_갱신": "python build_base_rates.py",
        "_주의": "STARBALL_QUESTIONS 를 바꾸면 반드시 다시 만들 것",
        "questions": keys,
        "per_question": {k: {lbl: round(c / n, 5) for lbl, c in v.most_common()}
                         for k, v in per.items()},
        "joint": {"|".join(c): round(cnt / n, 5) for c, cnt in joint.most_common()},
    }
    with io.open("base_rates.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"base_rates.json · 표본 {n}경기")
    for k in keys:
        print(f"  {k:<9}", " · ".join(f"{l}={c/n*100:.0f}%"
                                      for l, c in per[k].most_common()))
    top = joint.most_common(1)[0]
    print(f"  최빈 조합: {'|'.join(top[0])} = {top[1]/n*100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
