#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""승패 적중률을 끝까지 짜낸다 — 후보 특징 탐색.

특징 정의가 바뀌면(사전값 축소 같은) 이 선택도 다시 해야 한다. 시즌 최종 순위를 미리 알고 강팀을 찍는 반칙
오라클이 59.2% 이므로 남은 여유는 1%p 안쪽이다. 그 안에서 실제로 얻을 게
있는지 본다.

**프로토콜을 나누는 이유**
특징을 여러 개 시험하면서 같은 집합으로 고르고 재면, 그 집합에만 맞는 조합을
고르게 된다(선택 과적합). 그래서

    고르기 : 2024~2025 안에서만 (전진 선택)
    재기   : 2026 은 마지막에 딱 한 번

2026 을 보고 되돌아가 고르기를 다시 하면 이 구분이 무의미해진다. 하지 말 것.

    python explore_features.py                # 전체 탐색
    python explore_features.py --quick        # 후보별 단독 효과만
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict

import train_outcome as T

# 후보 특징. 이름 → 설명. 기존 7개에 얹어 하나씩 시험한다.
CANDIDATES = {
    "my_bp_era": "우리 불펜 ERA (선발 제외)",
    "op_bp_era": "상대 불펜 ERA",
    "my_form10": "우리 최근 10경기 승률",
    "op_form10": "상대 최근 10경기 승률",
    "my_pyth": "우리 득실점 기반 기대승률",
    "op_pyth": "상대 기대승률",
    "my_rest": "우리 휴식일",
    "op_rest": "상대 휴식일",
    "h2h_win": "올 시즌 이 상대와의 승률",
    "my_sp_recent": "우리 선발 최근 3등판 ERA",
    "op_sp_recent": "상대 선발 최근 3등판 ERA",
    "sp_ip_edge": "선발 소화이닝 차 (불펜 부담)",
}


def build(games: list, prior=None) -> list:
    """train_outcome 의 특징 생성을 그대로 쓴다.

    한때 이 파일에 같은 로직을 복사해 뒀는데, 그 뒤 train_outcome 쪽에
    사전값 축소가 들어가면서 정의가 갈렸다. 그러면 여기서 고른 특징이
    실제 학습과 다른 값 위에서 고른 것이 된다 — 실험 결과가 쓸모없어진다.
    """
    rows = T.build_rows(games, prior=prior)   # 팩터는 build_rows 가 계산한다
    for r in rows:
        r["f"] = dict(zip(T.FEATURES, r["feat"]))
    return rows


def rows_for(year: int) -> list:
    """한 시즌. 작년을 사전값으로 넘긴다 (train_outcome 과 같은 방식)."""
    prior = None
    try:
        prior = T.state_through(T.load(f"gamelog_{year - 1}.json"))
    except FileNotFoundError:
        pass
    rows = build(T.load(f"gamelog_{year}.json"), prior=prior)
    for r in rows:
        r["season"] = year
    return rows


def fit_score(train, test, names, C=0.3, kind="logistic"):
    import numpy as np
    from sklearn.preprocessing import StandardScaler
    Xtr = np.array([[r["f"][n] for n in names] for r in train])
    Xte = np.array([[r["f"][n] for n in names] for r in test])
    ytr = np.array([r["y"] for r in train])
    yte = np.array([r["y"] for r in test])
    sc = StandardScaler().fit(Xtr)
    if kind == "logistic":
        from sklearn.linear_model import LogisticRegression
        m = LogisticRegression(max_iter=5000, C=C)
    else:
        from sklearn.ensemble import HistGradientBoostingClassifier
        m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.06,
                                           max_depth=3, l2_regularization=1.0,
                                           random_state=0)
    m.fit(sc.transform(Xtr), ytr)
    P = m.predict_proba(sc.transform(Xte))
    pred = m.classes_[P.argmax(1)]
    ok = (pred == yte)
    return float(ok.mean()), float(abs(P.max(1).mean() - ok.mean()))


def cv(rows, names, C=0.3, kind="logistic"):
    """가장 최근 시즌 안에서 여러 번 잘라 평균낸다."""
    newest = max(r["season"] for r in rows)
    past = [r for r in rows if r["season"] != newest]
    cur = [r for r in rows if r["season"] == newest]
    accs = []
    for frac in (0.5, 0.6, 0.7, 0.8):
        cut = int(len(cur) * frac)
        te = cur[cut:]
        if len(te) < 60:
            continue
        a, _ = fit_score(past + cur[:cut], te, names, C, kind)
        accs.append(a)
    return sum(accs) / len(accs) * 100 if accs else 0.0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="후보 특징 탐색")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--C", type=float, default=0.3)
    args = ap.parse_args()

    rows = []
    for y in (2024, 2025, 2026):
        try:
            rows += rows_for(y)
        except FileNotFoundError:
            print(f"gamelog_{y}.json 없음", file=sys.stderr)
    rows.sort(key=lambda r: (r["season"], r["date"]))
    pick_rows = [r for r in rows if r["season"] < 2026]      # 고르기용
    hold_rows = rows                                        # 마지막 재기용

    print(f"표본 {len(rows)}건 (고르기 {len(pick_rows)} / 최종검증 포함 {len(hold_rows)})")
    print(f"기준 특징 {len(T.CORE_FEATURES)}개: {', '.join(T.CORE_FEATURES)}\n")

    base = cv(pick_rows, T.CORE_FEATURES, args.C)
    print(f"■ 고르기 구간(2024~2025) 기준선  {base:.2f}%\n")

    print("■ 후보를 하나씩 얹어본 효과")
    gains = []
    for name, desc in CANDIDATES.items():
        v = cv(pick_rows, T.CORE_FEATURES + [name], args.C)
        gains.append((v - base, name, desc))
        print(f"  {name:<14}{v:>7.2f}%  {v - base:+6.2f}%p   {desc}")

    if args.quick:
        return 0

    print("\n■ 전진 선택 (좋아지는 것만 순서대로 얹는다)")
    chosen = list(T.CORE_FEATURES)
    cur = base
    pool = [n for _, n, _ in sorted(gains, key=lambda g: -g[0])]
    while pool:
        best = None
        for n in pool:
            v = cv(pick_rows, chosen + [n], args.C)
            if best is None or v > best[0]:
                best = (v, n)
        if best[0] <= cur + 0.15:            # 0.15%p 미만은 잡음으로 본다
            print(f"  더 얹어도 나아지지 않음 (최선 {best[1]} {best[0]:.2f}%)")
            break
        cur = best[0]
        chosen.append(best[1])
        pool.remove(best[1])
        print(f"  + {best[1]:<14}→ {cur:.2f}%")

    print(f"\n고른 특징 {len(chosen)}개: {', '.join(chosen)}")

    print("\n■ 최종 검증 — 2026 으로 딱 한 번 (여기 보고 되돌아가지 말 것)")
    newest = 2026
    past = [r for r in hold_rows if r["season"] != newest]
    cur_rows = [r for r in hold_rows if r["season"] == newest]
    cut = int(len(cur_rows) * 0.7)
    for label, names in ((f"지금 {len(T.CORE_FEATURES)}개", T.CORE_FEATURES),
                        (f"고른 {len(chosen)}개", chosen)):
        a, ce = fit_score(past + cur_rows[:cut], cur_rows[cut:], names, args.C)
        print(f"  {label:<12}{a * 100:>7.2f}%   확신도 오차 {ce * 100:.1f}%p")
    yte = [r["y"] for r in cur_rows[cut:]]
    print(f"  {'그냥 찍기':<12}"
          f"{Counter(yte).most_common(1)[0][1] / len(yte) * 100:>7.2f}%")
    print("\n  참고: 시즌 최종 순위를 미리 아는 반칙 오라클이 59.2% 다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
