#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""홈런 수 문항을 학습한다.

**왜 필요했나**
지금까지 홈런 추천은 96경기 전부 `0개` 였다. "0개가 최빈이니 최적" 이라고
설명했는데, 실제 기록을 보니 틀렸다. 2024~2026 팀-경기 3,460건을 기대
홈런으로 나눠보면:

    기대 0.12~0.46  →  0개 51.9% / 1개 33.5%   1위 0개
    기대 0.59~0.72  →  0개 43.8% / 1개 34.1%   1위 0개
    기대 0.92~1.20  →  0개 34.1% / 1개 35.9%   1위 1개  ←
    기대 1.20~2.16  →  0개 29.1% / 1개 35.2%   1위 1개  ←

**전체 경기의 3분의 1에서 정답은 1개다.** 매번 0개를 내던 것은 야구의
성질이 아니라 모델이 매치업에 반응하지 못한 결함이었다.

득실 차는 다르다. 같은 방식으로 재보면 어떤 구간에서도 1점이 최빈이라,
그쪽은 고정이 실제로 최적이다.

    python train_hr.py            # 학습 + 검증
    python train_hr.py --emit     # 계수 출력 (hr_model.json 으로 저장)

고르기는 2024~2025, 최종 검증은 2026 으로 한 번만 — 승패 모델과 같은
프로토콜이다.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

import train_outcome as T

# 홈런은 '우리 타선 × 상대 배터리 × 구장' 이 전부다. 우리 투수는 무관하다.
HR_FEATURES = ["park", "my_hr", "op_hra", "op_sp_hr9", "op_sp_era",
               "my_rs", "op_ra"]

# 실제 앱 선택지는 0~4 와 '5개 이상' 이다.
TOP_BUCKET = 5
LABELS = [f"{k}개" for k in range(TOP_BUCKET)] + [f"{TOP_BUCKET}개 이상"]


def hr_rows(games: list, prior=None, parks=None) -> list:
    """홈런 학습용 표본. 특징은 승패 모델과 같은 featurize 를 쓴다.

    사전값(작년 최종 성적)을 반드시 함께 넘긴다. 안 넘기면 개막 3주가
    표본에서 빠지고, 승패 모델과 특징 정의가 갈린다 — 실제로 그런 상태였다.
    """
    st = T.new_state()
    if parks is None:
        parks = T.park_hr_factors(games, prior=(prior or {}).get("parks"))
    out = []
    for x in sorted(games, key=lambda g: g.get("date", "")):
        hs, as_ = x.get("home_score"), x.get("away_score")
        if hs is None or as_ is None:
            continue
        box = x.get("box") or {}
        pitchers = x.get("pitchers") or {}
        date = x.get("date", "")

        def starter(side):
            for p in (pitchers.get(side) or []):
                if p.get("started"):
                    return p
            return None

        for me, foe, is_home in (("home", "away", True), ("away", "home", False)):
            ms, os_ = starter(me), starter(foe)
            if not ms or not os_:
                continue
            hr = (box.get(foe) or {}).get("hr_allowed")   # 상대가 허용 = 우리 홈런
            if hr is None:
                continue
            f = T.featurize(st, x.get(me), x.get(foe), is_home,
                            x.get("stadium", ""), ms.get("pcode"),
                            os_.get("pcode"), date, strict=True, prior=prior,
                            parks=parks)
            if f is None:
                continue
            out.append({"date": date, "team": x.get(me),
                        "season": int(str(date)[:4] or 0),
                        "f": f,
                        "y": min(int(hr), TOP_BUCKET)})
        T.feed(st, x)
    return out


def fit_hr(rows: list, C: float, names: list):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    import numpy as np
    X = np.array([[r["f"][n] for n in names] for r in rows])
    y = np.array([r["y"] for r in rows])
    sc = StandardScaler().fit(X)
    m = LogisticRegression(max_iter=5000, C=C).fit(sc.transform(X), y)
    return sc, m


def evaluate(train: list, test: list, C: float, names: list) -> dict:
    import numpy as np
    sc, m = fit_hr(train, C, names)
    X = sc.transform(np.array([[r["f"][n] for n in names] for r in test]))
    P = m.predict_proba(X)
    pred = m.classes_[P.argmax(1)]
    y = np.array([r["y"] for r in test])
    fixed = Counter([r["y"] for r in train]).most_common(1)[0][0]
    return {
        "model": float((pred == y).mean()) * 100,
        "fixed": float((y == fixed).mean()) * 100,
        "fixed_label": LABELS[fixed],
        "picks": Counter(LABELS[p] for p in pred),
        "n": len(test),
    }


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="홈런 수 문항 학습")
    ap.add_argument("--C", type=float, default=0.3)
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--out", default="hr_model.json")
    args = ap.parse_args()

    rows = []
    for y in (2024, 2025, 2026):
        try:
            prior = None
            try:
                prior = T.state_through(T.load(f"gamelog_{y - 1}.json"))
            except FileNotFoundError:
                pass
            rows += hr_rows(T.load(f"gamelog_{y}.json"), prior=prior)
        except FileNotFoundError:
            print(f"gamelog_{y}.json 없음", file=sys.stderr)
    rows.sort(key=lambda r: (r["season"], r["date"]))
    if not rows:
        raise SystemExit("표본이 없습니다. build_gamelog.py 를 먼저 돌리세요.")

    pick = [r for r in rows if r["season"] < 2026]
    cur = [r for r in rows if r["season"] == 2026]
    print(f"표본 {len(rows)}건 (고르기 {len(pick)} / 2026 {len(cur)})", file=sys.stderr)

    # 고르기 구간에서 특징 조합을 비교한다
    print("\n■ 고르기 구간(2024~2025) 안에서 특징 조합 비교", file=sys.stderr)
    cut = int(len(pick) * 0.7)
    best = (None, -1)
    sets = {
        "구장만": ["park"],
        "구장+타선": ["park", "my_hr"],
        "구장+타선+상대피홈런": ["park", "my_hr", "op_hra"],
        "+상대선발": ["park", "my_hr", "op_hra", "op_sp_hr9"],
        "전체 7개": HR_FEATURES,
    }
    for name, ns in sets.items():
        r = evaluate(pick[:cut], pick[cut:], args.C, ns)
        print(f"  {name:<22}{r['model']:>6.1f}%   "
              f"(고정 {r['fixed_label']} {r['fixed']:.1f}%)  "
              f"고른 값 {dict(r['picks'])}", file=sys.stderr)
        if r["model"] > best[1]:
            best = (ns, r["model"], name)

    names = best[0]
    print(f"\n고른 특징: {best[2]} — {', '.join(names)}", file=sys.stderr)

    print("\n■ 최종 검증 — 2026 으로 한 번 (되돌아가지 말 것)", file=sys.stderr)
    r = evaluate(pick + cur[:int(len(cur) * 0.7)], cur[int(len(cur) * 0.7):],
                 args.C, names)
    print(f"  학습 모델    {r['model']:>6.1f}%   {r['n']}경기", file=sys.stderr)
    print(f"  항상 {r['fixed_label']}   {r['fixed']:>6.1f}%", file=sys.stderr)
    print(f"  고른 값 분포 {dict(r['picks'])}", file=sys.stderr)

    sc, m = fit_hr(rows, args.C, names)
    payload = {
        "target": "lg_hr",
        "labels": LABELS,
        "classes": [LABELS[c] for c in m.classes_.tolist()],
        "features": names,
        "mean": [round(v, 6) for v in sc.mean_.tolist()],
        "scale": [round(v, 6) for v in sc.scale_.tolist()],
        "coef": [[round(c, 6) for c in row] for row in m.coef_.tolist()],
        "intercept": [round(c, 6) for c in m.intercept_.tolist()],
        "trained_on": {"rows": len(rows), "from": rows[0]["date"],
                       "to": rows[-1]["date"]},
        "validation": {"model": round(r["model"], 1), "fixed": round(r["fixed"], 1),
                       "fixed_label": r["fixed_label"], "n": r["n"]},
        "C": args.C,
    }
    if args.emit:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print(f"\n{args.out} 저장", file=sys.stderr)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
