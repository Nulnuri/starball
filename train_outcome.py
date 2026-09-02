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
    "my_sp_era", "my_sp_hr9", "my_sp_ip",            # 우리 선발 (시즌 누적)
    "op_sp_era", "op_sp_hr9", "op_sp_ip",            # 상대 선발
    "off_edge",                                      # 우리 타선 - 상대 실점
    "def_edge",                                      # 상대 타선 - 우리 실점
    "sp_edge",                                       # 상대 선발 ERA - 우리 선발 ERA
    "my_bp_era", "op_bp_era",                        # 불펜 ERA (선발 제외)
    "my_form10", "op_form10",                        # 최근 10경기 승률
    "my_pyth", "op_pyth",                            # 득실점 기반 기대승률
    "my_rest", "op_rest",                            # 휴식일
    "h2h_win",                                       # 올 시즌 이 상대와의 승률
    "my_sp_recent", "op_sp_recent",                  # 선발 최근 3등판 ERA
    "sp_ip_edge",                                    # 선발 소화이닝 차
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
                 "my_sp_era", "op_sp_era", "off_edge", "def_edge",
                 "my_sp_recent", "op_sp_recent",
                 "h2h_win", "my_form10"]

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


def pyth(rs: float, ra: float) -> float:
    """득실점으로 낸 기대승률. 승률보다 표본 잡음이 적다."""
    if rs <= 0 and ra <= 0:
        return 0.5
    a, b = rs ** 1.83, ra ** 1.83
    return a / (a + b) if a + b else 0.5


def build_rows(games: list) -> list:
    """팀-경기 표본을 만든다. 그 시점까지의 누적만 특징으로 쓴다.

    미래 정보가 새면 검증 성적만 좋아지고 실제 예측은 틀린다. 누적 갱신은
    반드시 특징을 다 만든 뒤에 한다.

    특징 생성은 이 함수 하나뿐이다. 두 곳에 두면 반드시 갈린다 —
    실제로 eval_window 가 21개, 여기가 7개를 쓰던 시기가 있었다.
    """
    team = defaultdict(lambda: {"g": 0, "rs": 0, "ra": 0, "hr": 0, "hra": 0,
                                "w": 0, "last": None, "recent": []})
    pit = defaultdict(lambda: {"ip": 0.0, "er": 0, "hr": 0, "n": 0, "recent": []})
    bp = defaultdict(lambda: {"ip": 0.0, "er": 0})
    h2h = defaultdict(lambda: [0, 0])          # (팀, 상대) → [승, 경기]
    rows = []

    for x in games:
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

        tsnap = {t: dict(v) for t, v in team.items()}
        psnap = {k: dict(v) for k, v in pit.items()}
        bsnap = {k: dict(v) for k, v in bp.items()}
        hsnap = {k: list(v) for k, v in h2h.items()}

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

            ab, ob = bsnap.get(tm, {"ip": 0.0, "er": 0}), bsnap.get(op, {"ip": 0.0, "er": 0})

            def days(prev):
                if not prev:
                    return 1.0
                try:
                    from datetime import date as D
                    d1 = D.fromisoformat(prev)
                    d2 = D.fromisoformat(date)
                    return float(min((d2 - d1).days, 7))
                except ValueError:
                    return 1.0

            def era(rec, ip_key="ip", er_key="er", floor=1.0):
                return rec[er_key] * 9 / rec[ip_key] if rec[ip_key] >= floor else 4.5

            def recent_era(rec):
                r = rec.get("recent") or []
                ip = sum(v[0] for v in r[-3:])
                er = sum(v[1] for v in r[-3:])
                return er * 9 / ip if ip >= 5 else era(rec)

            base = {
                "home": 1.0 if is_home else 0.0,
                "park": PARK.get(x.get("stadium", ""), 1.0),
                "my_rs": a["rs"] / a["g"], "my_ra": a["ra"] / a["g"],
                "my_hr": a["hr"] / a["g"], "my_hra": a["hra"] / a["g"],
                "my_win": a["w"] / a["g"],
                "op_rs": o["rs"] / o["g"], "op_ra": o["ra"] / o["g"],
                "op_hr": o["hr"] / o["g"], "op_hra": o["hra"] / o["g"],
                "op_win": o["w"] / o["g"],
                "my_sp_era": era(pm), "my_sp_hr9": pm["hr"] * 9 / pm["ip"],
                "my_sp_ip": pm["ip"] / max(pm["n"], 1),
                "op_sp_era": era(po), "op_sp_hr9": po["hr"] * 9 / po["ip"],
                "op_sp_ip": po["ip"] / max(po["n"], 1),
                "off_edge": a["rs"] / a["g"] - o["ra"] / o["g"],
                "def_edge": o["rs"] / o["g"] - a["ra"] / a["g"],
                "sp_edge": era(po) - era(pm),
                # ── 후보 ──
                "my_bp_era": era(ab, floor=10.0), "op_bp_era": era(ob, floor=10.0),
                "my_form10": (sum(a["recent"][-10:]) / len(a["recent"][-10:])
                              if a["recent"] else 0.5),
                "op_form10": (sum(o["recent"][-10:]) / len(o["recent"][-10:])
                              if o["recent"] else 0.5),
                "my_pyth": pyth(a["rs"] / a["g"], a["ra"] / a["g"]),
                "op_pyth": pyth(o["rs"] / o["g"], o["ra"] / o["g"]),
                "my_rest": days(a["last"]), "op_rest": days(o["last"]),
                "h2h_win": (hsnap.get((tm, op), [0, 0])[0]
                            / hsnap[(tm, op)][1] if hsnap.get((tm, op), [0, 0])[1]
                            else 0.5),
                "my_sp_recent": recent_era(pm), "op_sp_recent": recent_era(po),
                "sp_ip_edge": pm["ip"] / max(pm["n"], 1) - po["ip"] / max(po["n"], 1),
            }
            feat = [base[n] for n in FEATURES]
            rows.append({"date": date, "team": tm, "feat": feat,
                         "season": int(str(date)[:4] or 0),
                         "y": 0 if my > oy else (2 if my < oy else 1)})

        # 누적 갱신 (특징을 다 만든 뒤에)
        for me, foe, is_home in (("home", "away", True), ("away", "home", False)):
            tm = x.get(me)
            my, oy = (hs, as_) if is_home else (as_, hs)
            r = team[tm]
            r["g"] += 1
            r["rs"] += my
            r["ra"] += oy
            r["hr"] += int((box.get(foe) or {}).get("hr_allowed") or 0)
            r["hra"] += int((box.get(me) or {}).get("hr_allowed") or 0)
            won = 1 if my > oy else 0
            r["w"] += won
            r["recent"].append(won)
            r["last"] = date
            h2h[(tm, x.get(foe))][0] += won
            h2h[(tm, x.get(foe))][1] += 1
            for p in (pitchers.get(me) or []):
                ip = p.get("ip") or 0
                er = p.get("er") or 0
                if p.get("started"):
                    q = pit[p.get("pcode")]
                    q["ip"] += ip
                    q["er"] += er
                    q["hr"] += p.get("hr") or 0
                    q["n"] += 1
                    q["recent"].append((ip, er))
                else:
                    bp[tm]["ip"] += ip
                    bp[tm]["er"] += er
    return rows


def core_matrix(rows: list):
    """학습에 쓰는 특징만 뽑아낸다. 순서는 CORE_FEATURES 를 따른다."""
    import numpy as np
    ii = [FEATURES.index(n) for n in CORE_FEATURES]
    return np.array([[r["feat"][k] for k in ii] for r in rows])


def fit(rows: list, C: float):
    """표준화 + 로지스틱.

    표준화를 빼면 정규화가 특징마다 다른 세기로 걸린다(승률은 0~1,
    ERA 는 0~10 이라 스케일이 열 배 다르다).
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    import numpy as np
    X = core_matrix(rows)
    y = np.array([r["y"] for r in rows])
    sc = StandardScaler().fit(X)
    m = LogisticRegression(max_iter=5000, C=C).fit(sc.transform(X), y)
    return sc, m


def softmax(z):
    import numpy as np
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def fit_temperature(sc, m, rows_val: list) -> float:
    """확신도를 실제 적중률에 맞춘다 (온도 보정).

    로짓을 T 로 나눈다. T > 1 이면 자신감을 낮추고 < 1 이면 높인다.
    소프트맥스의 순서는 T 로 나눠도 바뀌지 않으므로 **적중률은 그대로**이고
    말하는 확률만 정직해진다. 파라미터가 하나뿐이라 표본이 적어도 안전하다.

    이걸 안 하면 '70% 확신' 이라고 말한 경기의 실제 적중률이 37.5% 인
    상태가 된다 — 그러면 화면의 숫자가 거짓말이 된다.
    """
    import numpy as np
    if len(rows_val) < 60:
        return 1.0
    X = sc.transform(core_matrix(rows_val))
    y = np.array([r["y"] for r in rows_val])
    logit = m.decision_function(X)
    if logit.ndim == 1:                      # 2클래스면 (n,) 로 온다
        logit = np.column_stack([-logit, logit])
    cols = {c: i for i, c in enumerate(m.classes_)}
    idx = np.array([cols[v] for v in y])

    best_t, best_nll = 1.0, float("inf")
    for t in np.arange(0.4, 5.01, 0.05):
        P = softmax(logit / t)
        nll = -np.log(np.clip(P[np.arange(len(y)), idx], 1e-12, 1)).mean()
        if nll < best_nll:
            best_nll, best_t = nll, float(t)
    return round(best_t, 3)


def confidence_tiers(rows: list, C: float) -> dict:
    """확신도가 얼마 이상일 때 실제로 몇 % 맞는지 잰다.

    전체 적중률은 59% 에서 포화됐다(팀 전력을 완벽히 아는 이론 상한이
    57.9~60.5%). 더 올릴 데가 없으므로, 남은 값어치는 **어느 날을 믿어야
    하는지** 를 말해주는 데 있다.

    실측(2024~2026, 7개 분할 합산 1,496경기):
        확신도 상위  5% → 81.1%
        확신도 상위 10% → 72.5%
        확신도 상위 15% → 66.1%
        전체            → 59.2%

    여기서 낸 문턱을 앱이 써서 '오늘은 믿을 만한 날' 을 표시한다.
    문턱을 코드에 손으로 박지 않는 이유는, 시즌마다 전력 분포가 달라지면
    같은 확신도가 다른 적중률을 뜻하기 때문이다.
    """
    import numpy as np

    newest = max(r.get("season", 0) for r in rows)
    past = [r for r in rows if r.get("season", newest) != newest]
    cur = [r for r in rows if r.get("season", newest) == newest]
    if len(cur) < 120:
        past, cur = [], rows

    probs, hits = [], []
    for frac in (0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8):
        cut = int(len(cur) * frac)
        tr, te = past + cur[:cut], cur[cut:]
        if len(te) < 60:
            continue
        hold = max(60, int(len(tr) * 0.2))
        sc_h, m_h = fit(tr[:-hold], C)
        temp = fit_temperature(sc_h, m_h, tr[-hold:])
        sc, m = fit(tr, C)
        lg = m.decision_function(sc.transform(core_matrix(te)))
        if lg.ndim == 1:
            lg = np.column_stack([-lg, lg])
        P = softmax(lg / temp)
        y = np.array([r["y"] for r in te])
        probs += P.max(1).tolist()
        hits += (m.classes_[P.argmax(1)] == y).tolist()

    if len(probs) < 200:
        return {}
    p = np.array(probs)
    ok = np.array(hits)
    out = {"n": len(p), "overall": round(float(ok.mean()) * 100, 1), "tiers": []}
    for share in (0.05, 0.10, 0.15, 0.20, 0.30):
        n = max(20, int(len(p) * share))
        idx = np.argsort(-p)[:n]
        out["tiers"].append({
            "share": share,
            "threshold": round(float(p[idx].min()), 4),
            "games": n,
            "accuracy": round(float(ok[idx].mean()) * 100, 1),
        })
    return out


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

    out = {"base": [], "model": [], "calib": [], "calib_raw": [], "temp": []}
    for frac in (0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8):
        cut = int(len(cur) * frac)
        tr, te = past + cur[:cut], cur[cut:]
        if len(te) < 60:
            continue
        ytr = np.array([r["y"] for r in tr])
        yte = np.array([r["y"] for r in te])
        base = Counter(ytr).most_common(1)[0][0]
        out["base"].append(float((yte == base).mean()))
        # 온도는 학습 구간의 뒤 20% 로 맞춘다. 검증 집합을 쓰면 반칙이다.
        # 온도를 맞춘 뒤에는 계수를 학습 구간 전체로 다시 학습한다 —
        # 배포할 때 그렇게 하므로, 검증도 같은 방식이어야 숫자를 믿을 수 있다.
        hold = max(60, int(len(tr) * 0.2))
        sc_h, m_h = fit(tr[:-hold], C)
        temp = fit_temperature(sc_h, m_h, tr[-hold:])
        sc, m = fit(tr, C)

        X = sc.transform(core_matrix(te))
        logit = m.decision_function(X)
        if logit.ndim == 1:
            logit = np.column_stack([-logit, logit])
        raw = softmax(logit)
        cal = softmax(logit / temp)
        pred = m.classes_[cal.argmax(1)]
        ok = (pred == yte)
        out["model"].append(float(ok.mean()))
        # 말한 확신도와 실제 적중률의 차이. 크면 화면의 % 를 믿을 수 없다.
        out["calib"].append(float(abs(cal.max(1).mean() - ok.mean())))
        out["calib_raw"].append(float(abs(raw.max(1).mean() - ok.mean())))
        out["temp"].append(temp)
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
        cr = sum(v["calib_raw"]) / n * 100
        tt = sum(v["temp"]) / n
        print(f"  확신도 오차  {cr:.1f}%p → {ce:.1f}%p (온도 보정 후, 평균 T={tt:.2f})",
              file=sys.stderr)
        print(f"               말한 확률과 실제 적중률의 차이다. "
              f"화면의 % 를 믿을 수 있는지를 뜻한다.", file=sys.stderr)

    ct = confidence_tiers(rows, args.C)
    if ct.get("tiers"):
        print("", file=sys.stderr)
        n_ct = ct["n"]
        print(f"확신도가 높은 날만 골랐을 때 (표본 {n_ct}건)", file=sys.stderr)
        for t in ct["tiers"]:
            print(f"  상위 {t['share'] * 100:>3.0f}% (확신도 {t['threshold'] * 100:.0f}% 이상, "
                  f"{t['games']:>3}경기)  적중 {t['accuracy']:>5.1f}%", file=sys.stderr)
        print(f"  전체                                        "
              f"적중 {ct['overall']:>5.1f}%", file=sys.stderr)
        if m - b < 3.0:
            print("  개선이 3%p 미만이다. 갈아끼울 값어치가 있는지 다시 보라.",
                  file=sys.stderr)

    # 온도용으로 마지막 20% 를 떼어 맞춘 뒤, 계수는 전체로 다시 학습한다.
    hold = max(60, int(len(rows) * 0.2))
    sc_h, m_h = fit(rows[:-hold], args.C)
    temperature = fit_temperature(sc_h, m_h, rows[-hold:])
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
        # 운영 쪽에서 로짓을 이 값으로 나눈 뒤 소프트맥스를 취한다.
        # 순서는 안 바뀌므로 고르는 값은 같고, 말하는 확률만 정직해진다.
        "temperature": temperature,
        "confidence": confidence_tiers(rows, args.C),
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
