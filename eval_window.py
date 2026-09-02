#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""몇 시즌까지 학습에 넣는 게 실제로 나은지 고른다.

데이터가 많으면 항상 좋다고 생각하기 쉽지만 야구는 그렇지 않다. 규칙과 공이
바뀌고(2024 ABS 도입, 베이스 확대), 구장이 바뀌고(대전 새 구장 2025),
리그 득점 환경 자체가 해마다 움직인다. 오래된 시즌을 넣으면 표본은 늘지만
지금과 다른 리그를 배우게 된다.

그래서 '넣을수록 좋다' 고 가정하지 않고, **최근 시즌으로 검증**해서 실제로
나아지는 구간까지만 쓴다.

    python eval_window.py                    # 학습 창 비교
    python eval_window.py --test 2026        # 검증에 쓸 시즌

같은 검증 집합(가장 최근 시즌)에 대고 학습 창만 바꿔 비교하므로,
숫자를 그대로 견줄 수 있다.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter

import train_outcome as T


def seasons_available() -> list:
    out = []
    for p in glob.glob("gamelog_*.json"):
        m = re.search(r"gamelog_(\d{4})\.json$", p.replace("\\", "/"))
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def rows_for(years: list) -> list:
    """여러 시즌을 시즌 단위로 각각 펼친다.

    시즌을 이어붙여 한 번에 펼치면 누적 성적이 시즌 경계를 넘어 이어져
    개막전 팀이 작년 성적을 들고 나오게 된다. 그건 실제 예측 상황과 다르다.
    """
    rows = []
    for y in years:
        path = f"gamelog_{y}.json"
        if not os.path.exists(path):
            continue
        r = T.build_rows(T.load(path))
        for x in r:
            x["season"] = y
        rows += r
    rows.sort(key=lambda r: (r["season"], r["date"]))
    return rows


def score(train: list, test: list, C: float, kind: str) -> float:
    import numpy as np
    Xtr = np.array([r["feat"] for r in train])
    ytr = np.array([r["y"] for r in train])
    Xte = np.array([r["feat"] for r in test])
    yte = np.array([r["y"] for r in test])
    if kind == "base":
        return float((yte == Counter(ytr).most_common(1)[0][0]).mean())
    if kind == "logistic":
        from sklearn.linear_model import LogisticRegression
        m = LogisticRegression(max_iter=5000, C=C).fit(Xtr, ytr)
    else:
        from sklearn.ensemble import HistGradientBoostingClassifier
        m = HistGradientBoostingClassifier(max_iter=400, learning_rate=0.06,
                                           max_depth=3, l2_regularization=1.0,
                                           random_state=0).fit(Xtr, ytr)
    return float((m.predict(Xte) == yte).mean())


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="학습 창 비교")
    ap.add_argument("--test", type=int, default=None, help="검증에 쓸 시즌")
    ap.add_argument("--C", type=float, default=0.2)
    ap.add_argument("--floor", type=int, default=None,
                    help=f"학습에 넣을 첫 시즌 (기본 {T.TRAIN_FROM_SEASON})")
    args = ap.parse_args()

    have = seasons_available()
    if len(have) < 2:
        raise SystemExit(f"시즌이 2개 이상 필요합니다. 지금: {have}")
    test_year = args.test or have[-1]
    floor = args.floor if args.floor is not None else T.TRAIN_FROM_SEASON
    skipped = [y for y in have if y < floor]
    if skipped:
        print(f"학습에서 제외: {skipped} "
              f"(리그 환경이 달라 상대전적 참고용으로만 쓴다)", file=sys.stderr)
    past = [y for y in have if floor <= y < test_year]
    if not past:
        raise SystemExit(f"{test_year} 이전 시즌이 없습니다.")

    te = rows_for([test_year])
    print(f"검증: {test_year} 시즌 {len(te)}건", file=sys.stderr)
    print(f"쓸 수 있는 과거 시즌: {past}\n", file=sys.stderr)

    print(f"{'학습 창':<26}{'표본':>7}   {'그냥찍기':>8}{'로지스틱':>9}{'부스팅':>8}")
    best = (None, -1)
    for k in range(1, len(past) + 1):
        years = past[-k:]
        tr = rows_for(years)
        if len(tr) < 200:
            continue
        b = score(tr, te, args.C, "base") * 100
        l = score(tr, te, args.C, "logistic") * 100
        g = score(tr, te, args.C, "gbm") * 100
        label = f"{years[0]}~{years[-1]}" if len(years) > 1 else str(years[0])
        print(f"  {label:<24}{len(tr):>7}   {b:>7.1f}%{l:>8.1f}%{g:>7.1f}%")
        for name, v in (("로지스틱", l), ("부스팅", g)):
            if v > best[1]:
                best = (f"{label} · {name}", v)

    print(f"\n최고: {best[0]}  {best[1]:.1f}%", file=sys.stderr)
    print("주의: 검증 시즌 하나로 고른 값이다. 시즌마다 리그 환경이 달라서,",
          file=sys.stderr)
    print("      내년에 이 스크립트를 다시 돌려 창을 재확인해야 한다.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
