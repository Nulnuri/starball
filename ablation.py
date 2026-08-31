#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""모델 구성요소가 실제로 기여하는지 하나씩 꺼가며 잰다.

    python ablation.py

"모델이 안 맞는다"까지는 백테스트로 알 수 있지만, 어느 부분이 문제인지는
알 수 없다. 구성요소를 제거했을 때 성능이 떨어지지 않는다면 그 구성요소는
정보를 주는 게 아니라 잡음을 넣고 있는 것이다.

승패 문항만 본다. 나머지는 백테스트에서 이미 기준선을 못 넘었다.
"""

from __future__ import annotations

import math
import sys
from datetime import date

import backtest as B
import starball_predictor as S


def accuracy(games: list, patch: dict, n_sim: int = 4000) -> tuple:
    """patch 로 모듈 상수를 바꾼 뒤 승패 적중률을 잰다."""
    saved = {k: getattr(S, k) for k in patch}
    for k, v in patch.items():
        setattr(S, k, v)
    try:
        state = B.SeasonState()
        hit = tot = 0
        for g in games:
            if state.ready(g["home"], g["away"]):
                old = S.MY_TEAM
                try:
                    S.MY_TEAM = g["home"]
                    ctx = B.make_context(state, g)
                    pred = S.predict(ctx, n_sim=n_sim)
                    picks = S.to_starball_choices(pred)
                finally:
                    S.MY_TEAM = old
                real = B.actual_answers(g, g["home"])["outcome"]
                pick = next(p["pick"] for p in picks if p["key"] == "outcome")
                hit += (pick == real)
                tot += 1
            state.feed(g)
    finally:
        for k, v in saved.items():
            setattr(S, k, v)
    return hit, tot


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    import json
    with open(f"gamelog_{S.today_kst().year}.json", encoding="utf-8") as f:
        games = sorted(json.load(f)["games"],
                       key=lambda g: (g["date"], g["gameId"]))

    # 각 변형: 그 구성요소의 가중치를 0으로 만든다.
    VARIANTS = [
        ("전체 모델 (현재)", {}),
        ("선발 투수 제거", {"W_SP_SEASON": 0.0, "W_SP_VS": 0.0,
                        "W_SP_RECENT": 0.0}),
        ("상대전적 제거", {"W_OFF_VS": 0.0, "W_SP_VS": 0.0}),
        ("최근 페이스 제거", {"W_OFF_RECENT": 0.0, "W_SP_RECENT": 0.0}),
        ("홈 어드밴티지 제거", {"HOME_FIELD_RUNS": 0.0}),
        ("시즌 팀 성적만", {"W_OFF_VS": 0.0, "W_OFF_RECENT": 0.0,
                       "W_SP_VS": 0.0, "W_SP_RECENT": 0.0}),
    ]

    print("승패 적중률 — 구성요소 절제 실험")
    print("음수 개선폭은 그 구성요소를 '빼는 게 낫다'는 뜻이다.\n")
    print(f"{'변형':<20}{'적중률':>9}{'전체 대비':>11}{'표본':>7}")
    print("─" * 50)

    base_acc = None
    for label, patch in VARIANTS:
        hit, tot = accuracy(games, patch)
        acc = hit / tot if tot else 0.0
        if base_acc is None:
            base_acc = acc
            delta = "  기준"
        else:
            delta = f"{(acc - base_acc) * 100:>+9.1f}p"
        print(f"{label:<20}{acc * 100:>8.1f}%{delta:>11}{tot:>7}")

    # 기준선
    dec = [g for g in games if g["home_score"] != g["away_score"]]
    homew = sum(1 for g in dec if g["home_score"] > g["away_score"])
    n = len(dec)
    se = math.sqrt(0.5 * 0.5 / n) * 100
    print("─" * 50)
    print(f"{'항상 홈팀 (기준선)':<20}{homew / n * 100:>8.1f}%{'':>11}{n:>7}")
    print(f"\n표본 {n}건의 표준오차 ≈ ±{se:.1f}%p")
    print("차이가 이 범위 안이면 '더 낫다'고 말할 수 없다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
