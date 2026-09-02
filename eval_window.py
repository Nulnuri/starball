#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""몇 시즌까지 학습에 넣는 게 실제로 나은지, 어떤 방식이 나은지 고른다.

데이터가 많으면 항상 좋다고 생각하기 쉽지만 야구는 그렇지 않다. 규칙과 공이
바뀌고(2024 ABS 도입, 베이스 확대), 구장이 바뀌고(2025 대전 새 구장), 리그
득점 환경이 해마다 움직인다. 오래된 시즌은 표본을 늘리는 대신 '지금과 다른
리그'를 가르친다.

두 가지를 잰다.

  개막전 방식   과거 시즌만으로 학습해 검증 시즌 전체를 맞힌다.
                시즌 초에 실제로 놓이는 상황이다. 어려운 쪽이다.

  실전 방식     과거 시즌 + 검증 시즌의 앞부분까지 학습해 뒷부분을 맞힌다.
                시즌이 진행되면 실제로 이 상태가 된다. 운영 중 성적에
                해당하므로, 계수를 고를 때는 이 값을 봐야 한다.

  (2026-09-02 실측: 2025 만으로 2026 을 맞히면 52% 인데, 2026 안에서
   앞→뒤로 맞히면 56% 다. 팀 전력이 시즌 사이에 크게 바뀌어 과거 시즌
   계수가 잘 옮겨가지 않는다. 그래서 두 값을 갈라서 봐야 한다.)

    python eval_window.py                  # 있는 시즌으로 전부 비교
    python eval_window.py --test 2026      # 검증 시즌 지정
    python eval_window.py --floor 2022     # 학습 하한 임시 변경
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from collections import Counter

import train_outcome as T


def say(msg: str = "") -> None:
    """모든 출력을 한 스트림으로 보낸다.

    표는 stdout, 설명은 stderr 로 내보내던 탓에 파이프를 타면 순서가 뒤엉켜
    결론이 표보다 먼저 찍혔다.
    """
    print(msg, file=sys.stderr, flush=True)


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
    개막전 팀이 작년 성적을 들고 나온다. 실제 예측 상황과 다르다.
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


def weights(train: list, decay: float) -> "object":
    """오래된 시즌에 낮은 가중치를 준다.

    과거 시즌을 통째로 버리거나 똑같이 믿는 것 사이의 중간이다. decay=1.0 이면
    가중치를 안 쓴다(전부 동일). 0.5 면 한 시즌 멀어질 때마다 절반이 된다.
    """
    import numpy as np
    if decay >= 1.0:
        return None
    newest = max(r.get("season", 0) for r in train)
    return np.array([decay ** (newest - r.get("season", newest)) for r in train])


def score(train: list, test: list, C: float, kind: str,
          decay: float = 1.0) -> float:
    """train_outcome 과 같은 특징·전처리를 쓴다.

    한때 여기만 21개 특징을 쓰고 train_outcome 은 7개를 써서, 두 스크립트의
    숫자를 나란히 놓고 비교할 수 없었다. 창을 고르는 실험과 실제 학습이
    다른 설정이면 실험 결과가 쓸모없어진다.
    """
    import numpy as np
    from sklearn.preprocessing import StandardScaler

    ytr = np.array([r["y"] for r in train])
    yte = np.array([r["y"] for r in test])
    if kind == "base":
        return float((yte == Counter(ytr).most_common(1)[0][0]).mean())

    Xtr = T.core_matrix(train)
    Xte = T.core_matrix(test)
    sc = StandardScaler().fit(Xtr)
    Xtr, Xte = sc.transform(Xtr), sc.transform(Xte)
    w = weights(train, decay)
    if kind == "logistic":
        from sklearn.linear_model import LogisticRegression
        m = LogisticRegression(max_iter=5000, C=C).fit(Xtr, ytr, sample_weight=w)
    else:
        from sklearn.ensemble import HistGradientBoostingClassifier
        m = HistGradientBoostingClassifier(max_iter=400, learning_rate=0.06,
                                           max_depth=3, l2_regularization=1.0,
                                           random_state=0)
        m.fit(Xtr, ytr, sample_weight=w)
    return float((m.predict(Xte) == yte).mean())


def table(rows_of: list, C: float, decays: list) -> list:
    """학습 창별로 성적을 찍고, (설명, 성적) 목록을 돌려준다."""
    head = f"{'학습 창':<26}{'표본':>7}  {'그냥찍기':>9}"
    for d in decays:
        head += f"{('로지스틱' if d >= 1.0 else f'로지×{d}'):>10}"
    for d in decays:
        head += f"{('부스팅' if d >= 1.0 else f'부스×{d}'):>9}"
    say(head)

    out = []
    for label, tr, te in rows_of:
        if len(tr) < 200 or len(te) < 60:
            continue
        b = score(tr, te, C, "base") * 100
        line = f"  {label:<24}{len(tr):>7}  {b:>8.1f}%"
        # 과거 시즌이 섞이지 않은 창에는 가중치가 의미가 없다.
        multi = len({r.get("season") for r in tr}) > 1
        for kind, width in (("logistic", 10), ("gbm", 9)):
            for d in decays:
                if d < 1.0 and not multi:
                    line += f"{'—':>{width}}"
                    continue
                v = score(tr, te, C, kind, d) * 100
                line += f"{v:>{width - 1}.1f}%"
                name = ("로지스틱" if kind == "logistic" else "부스팅")
                if d < 1.0:
                    name += f"×{d}"
                out.append((f"{label} · {name}", v))
        say(line)
    return out


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="학습 창·방식 비교")
    ap.add_argument("--test", type=int, default=None, help="검증에 쓸 시즌")
    ap.add_argument("--C", type=float, default=0.3)
    ap.add_argument("--floor", type=int, default=None,
                    help=f"학습에 넣을 첫 시즌 (기본 {T.TRAIN_FROM_SEASON})")
    ap.add_argument("--decay", type=float, default=0.5,
                    help="한 시즌 멀어질 때마다 곱할 가중치 (1.0=가중치 안 씀)")
    ap.add_argument("--hold", type=float, default=0.3,
                    help="실전 방식에서 뒤쪽 몇 할을 검증에 쓸지 (기본 0.3)")
    args = ap.parse_args()

    have = seasons_available()
    if not have:
        raise SystemExit("경기 로그가 없습니다. build_gamelog.py 를 먼저 돌리세요.")
    test_year = args.test or have[-1]
    floor = args.floor if args.floor is not None else T.TRAIN_FROM_SEASON

    skipped = [y for y in have if y < floor]
    if skipped:
        say(f"학습에서 제외: {skipped} "
            f"(리그 환경이 달라 상대전적 참고용으로만 쓴다)")
    past = [y for y in have if floor <= y < test_year]

    cur = rows_for([test_year])
    if not cur:
        raise SystemExit(f"{test_year} 시즌 표본이 없습니다.")
    say(f"검증 시즌: {test_year} · 표본 {len(cur)}건")
    say(f"학습에 쓸 수 있는 과거 시즌: {past or '없음'}")
    say()

    decays = [1.0] + ([args.decay] if args.decay < 1.0 else [])

    if past:
        say("■ 개막전 방식 — 과거 시즌만으로 검증 시즌 전체를 맞힌다")
        got = table([(f"{ys[0]}~{ys[-1]}" if len(ys) > 1 else str(ys[0]),
                      rows_for(ys), cur)
                     for ys in (past[-k:] for k in range(1, len(past) + 1))],
                    args.C, decays)
        if got:
            got.sort(key=lambda kv: -kv[1])
            say(f"  → 이 방식 최고: {got[0][0]}  {got[0][1]:.1f}%")
        say()

    say(f"■ 실전 방식 — 과거 시즌 + {test_year} 앞부분으로 뒤 {args.hold:.0%} 를 맞힌다")
    cut = int(len(cur) * (1 - args.hold))
    head, tail = cur[:cut], cur[cut:]
    cases = [(f"{test_year} 앞부분만", head, tail)]
    for k in range(1, len(past) + 1):
        ys = past[-k:]
        label = f"{ys[0]}~{ys[-1]}+{test_year}" if len(ys) > 1 else f"{ys[0]}+{test_year}"
        cases.append((label, rows_for(ys) + head, tail))
    got = table(cases, args.C, decays)
    if got:
        got.sort(key=lambda kv: -kv[1])
        say()
        say(f"운영 기준 최고: {got[0][0]}  {got[0][1]:.1f}%")
    say()
    say("주의: 검증 시즌 하나로 고른 값이다. 리그 환경이 해마다 달라서,")
    say("      내년에 이 스크립트를 다시 돌려 창과 방식을 재확인해야 한다.")
    say("      계수를 고를 때는 '실전 방식' 쪽을 봐라 — 운영 중 상황이다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
