#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""승패 문항을 로지스틱 회귀로 학습해 계수를 뽑는다.

왜 승패만인가
    세 미션 중 데이터가 통하는 건 승패뿐이다. 득실 차는 576경기에서 가장 흔한
    값(1점)이 23.1%이고 예측값과 실제값의 상관이 r=+0.017 — 사실상 0이다.
    홈런도 어떤 조건에서도 0개가 최빈이다. 이 두 문항에 학습을 붙이면 오히려
    나빠진다(실측: 전부적중 4.46% → 2.97%).

왜 로지스틱인가
    부스팅(GBM)도 해봤지만 표본이 수백 건이라 과적합했다. 7개 시점으로 나눠
    검증한 결과 로지스틱 57.3%, GBM 51.4%, 그냥 찍기 48.6%. 데이터가 적을
    때는 단순한 모델이 이긴다. 표본이 2~3시즌으로 늘면 부스팅을 다시 시도할
    만하다 — 그때 이 스크립트로 두 방식을 다시 비교한다.

쓰는 법
    python train_outcome.py                         학습 + 검증 + 계수 출력
    python train_outcome.py --emit                  코드에 붙일 형태로 출력
    python train_outcome.py --log gamelog_2026.json gamelog_2027.json

시즌이 끝나면 이걸 돌려 나온 계수를 starball_predictor.py 에 붙여넣는다.
학습에는 scikit-learn 이 필요하지만 매일 도는 파이프라인에는 계수(숫자)만
들어가므로, 운영 의존성은 늘지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

PARK = {"창원": 1.421, "대구": 1.324, "문학": 1.283, "광주": 1.078,
        "대전": 0.861, "고척": 0.837, "사직": 0.781, "수원": 0.749,
        "잠실": 0.665}

# 특징 이름. 순서가 계수 순서와 같아야 한다 — 바꾸면 예측이 조용히 틀린다.
FEATURES = [
    "home",                                          # 홈 경기인가
    "park",                                          # 구장 홈런 팩터
    "my_rs", "my_ra", "my_hr", "my_hra", "my_win",   # 우리 팀 시점 누적
    "op_rs", "op_ra", "op_hr", "op_hra", "op_win",   # 상대 팀
    "my_sp_era", "my_sp_hr9", "my_sp_ip",            # 우리 선발
    "op_sp_era", "op_sp_hr9", "op_sp_ip",            # 상대 선발
    "off_edge",                                      # 우리 타선 - 상대 실점
    "def_edge",                                      # 상대 타선 - 우리 실점
    "sp_edge",                                       # 상대 선발 ERA - 우리 선발 ERA
]

# 실제로 학습에 넣는 특징. 위 21개를 다 쓰면 표본 474건에 과적합해서
# 적중률이 55.3% 로 떨어지고, 확신도가 거짓이 된다(70% 라고 말한 경기의
# 실제 적중률이 37.5% 였다). 7개로 줄이고 표준화하면 59.4% / 확신오차
# 2.6%p 가 된다. 나머지 14개는 앞으로 표본이 늘면 다시 시험해볼 후보로
# 남겨둔다 — build_rows 는 계속 21개를 다 만든다.
#
# 참고: 시즌 최종 순위를 미리 알고 강팀을 찍는 반칙 오라클이 59.2% 다.
# 즉 59.4% 는 이 종목에서 사실상 상한이다. 더 올리려면 경기 전에 존재하지
# 않는 정보(당일 부상·심판·날씨 변화)가 필요하다.
CORE_FEATURES = ["home", "my_win", "op_win",
                 "my_sp_era", "op_sp_era", "off_edge", "def_edge"]

MIN_TEAM_GAMES = 15      # 팀 누적이 이만큼 쌓인 뒤부터 학습에 쓴다
MIN_SP_IP = 20.0         # 선발 누적 이닝 하한
LABELS = ["승", "무", "패"]

# 학습에 넣는 첫 시즌.
#
# 2024 에 ABS(자동 볼판정)와 확대 베이스가 도입되면서 득점 환경이 갈렸다.
# 그 이전 시즌은 표본을 늘려주지만 '지금과 다른 리그'를 가르친다. 2025 에는
# 대전 새 구장(한화생명볼파크)이 열려 구장 팩터도 그 전과 이어지지 않는다.
#
# 그래서 학습은 2024 이후만 쓴다. 그 이전 기록은 받아두되 상대전적 참고로만
# 쓴다(h2h_history.json). 이 값을 내리려면 eval_window.py 로 최근 시즌
# 검증 성적이 실제로 나아지는지 먼저 확인할 것 — 표본이 늘어도 성적이
# 나빠지는 구간이 있다.
TRAIN_FROM_SEASON = 2024


def training_logs(from_season: int = TRAIN_FROM_SEASON) -> list:
    """학습에 쓸 경기 로그 파일들. 기준 시즌 이후만 고른다."""
    import glob
    import re
    out = []
    for path in sorted(glob.glob("gamelog_*.json")):
        m = re.search(r"gamelog_(\d{4})\.json$", path.replace("\\", "/"))
        if m and int(m.group(1)) >= from_season:
            out.append(path)
    return out


def load(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    games = d.get("games", d) if isinstance(d, dict) else d
    return sorted(games, key=lambda g: g.get("date", ""))


def build_rows(games: list) -> list:
    """경기를 팀-경기 표본으로 펼치고, 그 시점까지의 누적만 특징으로 쓴다.

    미래 정보가 새어들면 검증 성적이 부풀어 실제로는 안 맞는다. 그래서 누적
    갱신은 반드시 특징을 만든 뒤에 한다.
    """
    team = defaultdict(lambda: {"g": 0, "rs": 0, "ra": 0, "hr": 0, "hra": 0, "w": 0})
    pit = defaultdict(lambda: {"ip": 0.0, "er": 0, "hr": 0, "n": 0})
    rows = []

    for x in games:
        hs, as_ = x.get("home_score"), x.get("away_score")
        if hs is None or as_ is None:
            continue
        box = x.get("box") or {}
        pitchers = x.get("pitchers") or {}

        def starter(side):
            for p in (pitchers.get(side) or []):
                if p.get("started"):
                    return p
            return None

        tsnap = {t: dict(v) for t, v in team.items()}
        psnap = {k: dict(v) for k, v in pit.items()}

        for me, foe, is_home in (("home", "away", True), ("away", "home", False)):
            tm, op = x.get(me), x.get(foe)
            my, oy = (hs, as_) if is_home else (as_, hs)
            a, o = tsnap.get(tm), tsnap.get(op)
            ms, os_ = starter(me), starter(foe)
            if not (a and o and ms and os_):
                continue
            if a["g"] < MIN_TEAM_GAMES or o["g"] < MIN_TEAM_GAMES:
                continue
            pm, po = psnap.get(ms.get("pcode")), psnap.get(os_.get("pcode"))
            if not (pm and po) or pm["ip"] < MIN_SP_IP or po["ip"] < MIN_SP_IP:
                continue

            f = [
                1.0 if is_home else 0.0,
                PARK.get(x.get("stadium", ""), 1.0),
                a["rs"] / a["g"], a["ra"] / a["g"], a["hr"] / a["g"],
                a["hra"] / a["g"], a["w"] / a["g"],
                o["rs"] / o["g"], o["ra"] / o["g"], o["hr"] / o["g"],
                o["hra"] / o["g"], o["w"] / o["g"],
                pm["er"] * 9 / pm["ip"], pm["hr"] * 9 / pm["ip"],
                pm["ip"] / max(pm["n"], 1),
                po["er"] * 9 / po["ip"], po["hr"] * 9 / po["ip"],
                po["ip"] / max(po["n"], 1),
                a["rs"] / a["g"] - o["ra"] / o["g"],
                o["rs"] / o["g"] - a["ra"] / a["g"],
                po["er"] * 9 / po["ip"] - pm["er"] * 9 / pm["ip"],
            ]
            if len(f) != len(FEATURES):
                raise SystemExit(f"특징 개수 불일치 {len(f)} != {len(FEATURES)}")
            rows.append({"date": x.get("date", ""), "team": tm, "feat": f,
                         "season": int(str(x.get("date", "0"))[:4] or 0),
                         "y": 0 if my > oy else (2 if my < oy else 1)})

        # 특징을 다 만든 뒤에 누적을 갱신한다 (미래 정보 차단)
        for me, foe, is_home in (("home", "away", True), ("away", "home", False)):
            tm = x.get(me)
            my, oy = (hs, as_) if is_home else (as_, hs)
            r = team[tm]
            r["g"] += 1
            r["rs"] += my
            r["ra"] += oy
            r["hr"] += int((box.get(foe) or {}).get("hr_allowed") or 0)
            r["hra"] += int((box.get(me) or {}).get("hr_allowed") or 0)
            r["w"] += 1 if my > oy else 0
            for p in (pitchers.get(me) or []):
                q = pit[p.get("pcode")]
                q["ip"] += p.get("ip") or 0
                q["er"] += p.get("er") or 0
                q["hr"] += p.get("hr") or 0
                if p.get("started"):
                    q["n"] += 1
    return rows


def core_matrix(rows: list):
    """핵심 특징만 뽑아낸다. 순서는 CORE_FEATURES 를 따른다."""
    import numpy as np
    ii = [FEATURES.index(n) for n in CORE_FEATURES]
    return np.array([[r["feat"][k] for k in ii] for r in rows])


def fit(rows: list, C: float):
    """표준화 + 로지스틱. 표준화를 빼면 정규화가 특징마다 다르게 걸린다."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    import numpy as np
    X = core_matrix(rows)
    y = np.array([r["y"] for r in rows])
    sc = StandardScaler().fit(X)
    m = LogisticRegression(max_iter=5000, C=C).fit(sc.transform(X), y)
    return sc, m


def rows_by_season(paths: list) -> list:
    """시즌마다 따로 펼쳐 합친다.

    여러 시즌을 이어붙여 한 번에 펼치면 팀 누적이 시즌 경계를 넘어가서,
    2025 개막전 팀이 2024 성적을 들고 나온다. 실제로 이 탓에 성적이
    59.8% 대신 55.3% 로 나왔다.
    """
    out = []
    for path in paths:
        try:
            out += build_rows(load(path))
        except FileNotFoundError:
            print(f"{path} 없음 — 건너뜀", file=sys.stderr)
    out.sort(key=lambda r: (r.get("season", 0), r["date"]))
    return out


def validate(rows: list, C: float) -> dict:
    """분할 지점을 옮겨가며 반복 검증한다.

    한 번만 자르면 그 구간의 운이 성적으로 잡힌다. 시간순이므로 항상 과거로
    학습해 미래를 맞힌다.

    여러 시즌을 넣을 때는 **가장 최근 시즌 안에서만** 자른다. 전체를 비율로
    자르면 검증 집합에 과거 시즌이 섞여, 실제로는 이미 지난 경기를 맞히는
    성적이 섞여 들어간다(그래서 한때 55.3% 로 낮게 나왔다). 운영 중 상황은
    '과거 시즌 전부 + 올 시즌 지금까지 → 다음 경기' 다.
    """
    import numpy as np

    newest = max(r.get("season", 0) for r in rows)
    past = [r for r in rows if r.get("season", newest) != newest]
    cur = [r for r in rows if r.get("season", newest) == newest]
    if len(cur) < 120:                      # 시즌 초라 아직 자를 게 없다
        past, cur = [], rows

    out = {"base": [], "model": [], "calib": []}
    for frac in (0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8):
        cut = int(len(cur) * frac)
        tr, te = past + cur[:cut], cur[cut:]
        if len(te) < 60:
            continue
        ytr = np.array([r["y"] for r in tr])
        yte = np.array([r["y"] for r in te])
        base = Counter(ytr).most_common(1)[0][0]
        out["base"].append(float((yte == base).mean()))
        sc, m = fit(tr, C)
        P = m.predict_proba(sc.transform(core_matrix(te)))
        pred = m.classes_[P.argmax(1)]
        ok = (pred == yte)
        out["model"].append(float(ok.mean()))
        # 말한 확신도와 실제 적중률의 차이. 이게 크면 화면의 % 를 믿을 수 없다.
        out["calib"].append(float(abs(P.max(1).mean() - ok.mean())))
    return out


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="승패 문항 학습")
    ap.add_argument("--log", nargs="*", default=None,
                    help=f"경기 로그. 기본값은 {TRAIN_FROM_SEASON} 시즌 이후 전부")
    ap.add_argument("--C", type=float, default=0.3,
                    help="정규화 세기(작을수록 강하게 억제)")
    ap.add_argument("--emit", action="store_true", help="코드에 붙일 형태로 출력")
    args = ap.parse_args()

    paths = args.log or training_logs()
    if not paths:
        raise SystemExit(f"{TRAIN_FROM_SEASON} 이후 경기 로그가 없습니다. "
                         f"build_gamelog.py 로 먼저 받으세요.")
    print(f"학습에 쓰는 로그: {', '.join(paths)}", file=sys.stderr)

    rows = rows_by_season(paths)
    n_games = sum(len(load(p)) for p in paths if os.path.exists(p))
    if not rows:
        raise SystemExit("학습 표본이 없습니다.")
    print(f"경기 {n_games}건 → 학습 표본 {len(rows)}건 "
          f"({rows[0]['date']} ~ {rows[-1]['date']})", file=sys.stderr)
    if len(rows) < 300:
        print("표본이 300건 미만이다. 계수를 갈아끼우기엔 이르다.", file=sys.stderr)

    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        raise SystemExit("scikit-learn 이 필요합니다:  pip install scikit-learn")

    v = validate(rows, args.C)
    n = len(v["base"])
    if n:
        b = sum(v["base"]) / n * 100
        m = sum(v["model"]) / n * 100
        print(f"\n반복 검증 {n}회 (과거로 학습 → 미래 맞히기)", file=sys.stderr)
        print(f"  그냥 찍기   {b:.1f}%", file=sys.stderr)
        print(f"  이 모델     {m:.1f}%   개선 {m - b:+.1f}%p "
              f"(범위 {min(v['model']) * 100:.0f}~{max(v['model']) * 100:.0f}%)",
              file=sys.stderr)
        ce = sum(v["calib"]) / n * 100
        print(f"  확신도 오차  {ce:.1f}%p   "
              f"(말한 확률과 실제 적중률의 차이. 화면의 % 를 믿을 수 있는지)",
              file=sys.stderr)
        if m - b < 3.0:
            print("  개선이 3%p 미만이다. 갈아끼울 값어치가 있는지 다시 보라.",
                  file=sys.stderr)

    scaler, model = fit(rows, args.C)

    payload = {
        "classes": [LABELS[i] for i in model.classes_.tolist()],
        "features": CORE_FEATURES,
        # 운영 쪽에서 (x - mean) / scale 을 먼저 적용해야 한다. 빼먹으면
        # 계수는 맞는데 확률만 엉뚱하게 나온다.
        "mean": [round(v, 6) for v in scaler.mean_.tolist()],
        "scale": [round(v, 6) for v in scaler.scale_.tolist()],
        "coef": [[round(c, 6) for c in row] for row in model.coef_.tolist()],
        "intercept": [round(c, 6) for c in model.intercept_.tolist()],
        "trained_on": {"games": n_games, "rows": len(rows),
                       "from": rows[0]["date"], "to": rows[-1]["date"]},
        "C": args.C,
        "validation": {
            "splits": n,
            "base": round(sum(v["base"]) / n * 100, 1) if n else None,
            "model": round(sum(v["model"]) / n * 100, 1) if n else None,
            "calib_error": round(sum(v["calib"]) / n * 100, 1) if n else None,
        },
    }

    if args.emit:
        print("OUTCOME_MODEL = " + json.dumps(payload, ensure_ascii=False, indent=4))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
