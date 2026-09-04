#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""승패 문항을 로지스틱 회귀로 학습해 계수를 뽑는다.

왜 승패만인가
    세 미션 중 데이터가 통하는 건 승패뿐이다. 득실 차는 576경기에서 가장 흔한
    값(1점)이 23.1%이고 예측값과 실제값의 상관이 r=+0.017 — 사실상 0이다.
    홈런은 별개다 — 한때 "어떤 조건에서도 0개가 최빈" 이라고 여기 적어뒀는데
    틀렸다. 그건 실제 기록이 아니라 이 모델의 출력을 본 것이었다. 원본
    3,460 팀-경기로는 기대 홈런 0.9 이상 구간(전체의 3분의 1)에서 1개가
    최빈이다. 홈런은 train_hr.py 가 따로 학습한다.

    득실 차는 학습을 붙이면 나빠진다(실측: 전부적중 4.46% → 2.97%).

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
from typing import Optional

# 구장 홈런 팩터.
#
# 하드코딩하지 않는다. 매 시즌 기록에서 계산하고, 표본이 얕으면 작년 값
# (없으면 리그 평균 1.0) 쪽으로 당긴다.
#
# **2027 신규 잠실야구장이 이 경로를 탄다.** 이름이 그대로 '잠실' 로 오면
# 옛 구장의 0.665 를 그대로 쓰게 되고, 홈런 모델은 구장 팩터가 유일한
# 특징이라 시즌 내내 조용히 틀린다. 그래서 기록에서 다시 계산해야 한다.
# 새 구장은 1.0 에서 시작해 경기가 쌓이는 만큼 실제 값으로 옮겨간다.
#
# 아래 값은 파일이 없을 때만 쓰는 최후 수단이다(2026 실측).
# 구장 팩터의 최후 수단. **여기에 값을 적지 않는다.**
#
# 한때 이 파일과 starball_predictor 에 각각 상수 사본이 있었고, park_factors.json
# 과 park_hr_factors() 까지 합쳐 같은 사실이 네 곳에 있었다. 2025 신규 대전에서
# 하드코딩 0.861 과 실제 1.07 이 24% 어긋난 것도 그 탓이다.
#
# 평소에는 park_hr_factors() 가 기록에서 계산한다. 이 값은 기록조차 없을 때
# (테스트 픽스처 등) 쓰는 중립값이다.
PARK_FALLBACK: dict = {}
PARK = PARK_FALLBACK        # 하위 호환. park_hr_factors() 를 쓸 것

K_PARK = 40.0        # 구장 팩터 축소: 40경기쯤 치르면 그 시즌 값을 절반쯤 믿는다

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
    "my_hr10", "op_hra10",                           # 최근 10경기 홈런 흐름
    "my_hr_trend",                                   # 최근 10경기 - 시즌 평균
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
                 "h2h_win", "my_form10", "op_form10"]

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


def new_state() -> dict:
    """누적 상태. 학습과 운영이 같은 그릇을 쓴다."""
    return {
        "team": defaultdict(lambda: {"g": 0, "rs": 0, "ra": 0, "hr": 0,
                                     "hra": 0, "w": 0, "last": None,
                                     "recent": [], "recent_hr": [],
                                     "recent_hra": []}),
        "pit": defaultdict(lambda: {"ip": 0.0, "er": 0, "hr": 0, "n": 0,
                                    "recent": []}),
        "bp": defaultdict(lambda: {"ip": 0.0, "er": 0}),
        "h2h": defaultdict(lambda: [0, 0]),
    }


def feed(state: dict, game: dict) -> None:
    """끝난 경기 하나를 누적에 반영한다."""
    hs, as_ = game.get("home_score"), game.get("away_score")
    if hs is None or as_ is None:
        return
    box = game.get("box") or {}
    pitchers = game.get("pitchers") or {}
    date = game.get("date", "")
    for me, foe, is_home in (("home", "away", True), ("away", "home", False)):
        tm, op = game.get(me), game.get(foe)
        my, oy = (hs, as_) if is_home else (as_, hs)
        r = state["team"][tm]
        r["g"] += 1
        r["rs"] += my
        r["ra"] += oy
        r["hr"] += int((box.get(foe) or {}).get("hr_allowed") or 0)
        r["hra"] += int((box.get(me) or {}).get("hr_allowed") or 0)
        won = 1 if my > oy else 0
        r["w"] += won
        r["recent"].append(won)
        # 최근 홈런 흐름. 시즌 평균만 쓰면 타선이 달아오른 구간을 못 따라간다 —
        # 2026-09 두산 3연전에서 LG 가 매 경기 1홈런을 쳤는데 모델은 계속
        # 0개를 골랐다. 시즌 평균이 0개(39%)였기 때문이다.
        r["recent_hr"].append(int((box.get(foe) or {}).get("hr_allowed") or 0))
        r["recent_hra"].append(int((box.get(me) or {}).get("hr_allowed") or 0))
        r["last"] = date
        state["h2h"][(tm, op)][0] += won
        state["h2h"][(tm, op)][1] += 1
        for pp in (pitchers.get(me) or []):
            ip = pp.get("ip") or 0
            er = pp.get("er") or 0
            if pp.get("started"):
                q = state["pit"][pp.get("pcode")]
                q["ip"] += ip
                q["er"] += er
                q["hr"] += pp.get("hr") or 0
                q["n"] += 1
                q["recent"].append((ip, er))
            else:
                state["bp"][tm]["ip"] += ip
                state["bp"][tm]["er"] += er


# 개막 직후에는 올 시즌 표본이 거의 없다. 작년 최종 성적을 사전값으로 두고
# 경기가 쌓이는 만큼 그쪽으로 옮겨간다(축소 추정). 선착순 목표에서는 개막
# 3주가 가장 중요한데, 이게 없으면 그 구간에 모델이 아예 안 돌았다.
# 축소 강도. 값이 작을수록 올 시즌 성적을 빨리 믿는다.
#
# 같은 경기로 공정하게 비교한 결과(학습 2024~2025, 검증 2026):
#     20/30  공통 610경기 56.1% · 개막 174경기 53.4%
#      6/10  공통       55.1% · 개막       55.2%
#      3/6   공통       54.6% · 개막       56.3%
# 사전값을 쓰면 공통 경기에서도 +0.5~1.3%p 나아지고, 예측 자체가 불가능했던
# 개막 174경기가 커버된다.
#
# 차이가 ±1%p 로 표본 잡음 범위 안이라 확신할 수는 없다. 스타볼 7개는
# **선착순**이어서 개막 구간의 값어치가 크므로 균형점인 6/10 을 쓴다.
# 표본이 더 쌓이면 이 표를 다시 만들어 고를 것.
K_TEAM = 6.0
K_SP = 10.0


def _shrink(obs_sum: float, obs_n: float, prior_rate: Optional[float],
            k: float) -> float:
    """관측과 사전값을 표본 크기로 섞는다. 사전값이 없으면 관측만 쓴다."""
    if prior_rate is None:
        return obs_sum / obs_n if obs_n else 0.0
    return (obs_sum + prior_rate * k) / (obs_n + k)


def _prior_team(prior: Optional[dict], code: str) -> Optional[dict]:
    if not prior:
        return None
    t = (prior.get("team") or {}).get(code)
    if not t or not t["g"]:
        return None
    g = t["g"]
    return {"rs": t["rs"] / g, "ra": t["ra"] / g, "hr": t["hr"] / g,
            "hra": t["hra"] / g, "w": t["w"] / g}


def _prior_pit(prior: Optional[dict], code: str) -> Optional[dict]:
    if not prior or not code:
        return None
    q = (prior.get("pit") or {}).get(code)
    if not q or q["ip"] < 10:
        return None
    return {"era": q["er"] * 9 / q["ip"], "hr9": q["hr"] * 9 / q["ip"],
            "ip": q["ip"] / max(q["n"], 1)}


def park_hr_factors(games: list, prior: Optional[dict] = None,
                    k: float = K_PARK) -> dict:
    """구장별 홈런 팩터를 그 시즌 기록에서 계산한다.

    팩터 = (그 구장 경기당 홈런) / (리그 경기당 홈런). 표본이 얕으면 작년
    값(없으면 1.0) 쪽으로 당긴다 — 신규 구장은 1.0 에서 시작한다.

    반환에 없는 구장은 부르는 쪽에서 1.0 으로 다룬다. 이름이 처음 보이는
    구장이면 그게 신규 구장이라는 신호다.
    """
    hr, gm = defaultdict(float), defaultdict(float)
    for x in games:
        if x.get("home_score") is None:
            continue
        box = x.get("box") or {}
        st = x.get("stadium") or ""
        if not st:
            continue
        both = ((box.get("home") or {}).get("hr_allowed") or 0)             + ((box.get("away") or {}).get("hr_allowed") or 0)
        hr[st] += both
        gm[st] += 1
    total_hr, total_gm = sum(hr.values()), sum(gm.values())
    if not total_gm:
        return {}
    league = total_hr / total_gm

    out = {}
    for st in gm:
        base = (prior or {}).get(st, 1.0) * league
        shrunk = (hr[st] + base * k) / (gm[st] + k)
        out[st] = round(shrunk / league, 4) if league else 1.0
    return out


def _recent_rate(rec: dict, key: str, fallback: float, n: int = 10) -> float:
    """최근 n경기 평균. 표본이 얕으면 시즌 값을 쓴다."""
    v = (rec.get(key) or [])[-n:]
    return sum(v) / len(v) if len(v) >= 5 else fallback


def _prior_h2h(prior: Optional[dict], tm: str, op: str) -> float:
    """작년 상대전적 승률. 없으면 0.5."""
    if not prior:
        return 0.5
    h = (prior.get("h2h") or {}).get((tm, op))
    return h[0] / h[1] if h and h[1] else 0.5


def _era(rec: dict, floor: float = 1.0) -> float:
    return rec["er"] * 9 / rec["ip"] if rec["ip"] >= floor else 4.5


def _recent_era(rec: dict) -> float:
    r = rec.get("recent") or []
    ip = sum(v[0] for v in r[-3:])
    er = sum(v[1] for v in r[-3:])
    return er * 9 / ip if ip >= 5 else _era(rec)


def _days(prev, date: str) -> float:
    if not prev:
        return 1.0
    try:
        from datetime import date as D
        return float(min((D.fromisoformat(date) - D.fromisoformat(prev)).days, 7))
    except ValueError:
        return 1.0


def featurize(state: dict, tm: str, op: str, is_home: bool, stadium: str,
              my_sp: str, op_sp: str, date: str,
              strict: bool = True,
              prior: Optional[dict] = None,
              parks: Optional[dict] = None) -> dict | None:
    """특징 한 벌을 만든다. **학습과 운영이 반드시 이 함수를 함께 쓴다.**

    학습은 gamelog 로 누적을 쌓아 이 함수를 부르고, 운영도 같은 gamelog 로
    같은 함수를 부른다. 두 곳에 따로 계산을 두면 값이 미세하게 달라지고,
    그러면 에러 없이 예측만 틀린다 — 머신러닝에서 가장 잡기 어려운 사고다.

    strict=True 면 누적이 얕을 때 None 을 준다(학습용). 운영에서는 값이
    없더라도 답을 내야 하므로 strict=False 로 부르고, 얕으면 리그 평균으로
    채운다.
    """
    ZERO_T = {"g": 0, "rs": 0, "ra": 0, "hr": 0, "hra": 0, "w": 0,
              "last": None, "recent": []}
    a = state["team"].get(tm) or dict(ZERO_T)
    o = state["team"].get(op) or dict(ZERO_T)
    pa, po_ = _prior_team(prior, tm), _prior_team(prior, op)

    # 사전값이 있으면 표본이 없어도 답을 낼 수 있다. 없으면 종전대로 하한을 본다.
    if not prior and (not a["g"] or not o["g"]):
        return None
    if strict and not prior and (a["g"] < MIN_TEAM_GAMES
                                 or o["g"] < MIN_TEAM_GAMES):
        return None
    if strict and prior and (a["g"] + o["g"] == 0) and not (pa and po_):
        return None

    pm = state["pit"].get(my_sp)
    po = state["pit"].get(op_sp)
    ppm, ppo = _prior_pit(prior, my_sp), _prior_pit(prior, op_sp)
    if strict and not prior and (not pm or not po
                                 or pm["ip"] < MIN_SP_IP or po["ip"] < MIN_SP_IP):
        return None
    if strict and prior:
        # 사전값도 없고 올 시즌 이닝도 얕은 선발이면 쓸 수 없다(신인 첫 등판).
        if not (pm and pm["ip"] >= MIN_SP_IP) and not ppm:
            return None
        if not (po and po["ip"] >= MIN_SP_IP) and not ppo:
            return None
    pm = pm or {"ip": 0.0, "er": 0, "hr": 0, "n": 0, "recent": []}
    po = po or {"ip": 0.0, "er": 0, "hr": 0, "n": 0, "recent": []}

    ab = state["bp"].get(tm, {"ip": 0.0, "er": 0})
    ob = state["bp"].get(op, {"ip": 0.0, "er": 0})
    hh = state["h2h"].get((tm, op), [0, 0])

    def sp_ip(rec):
        return rec["ip"] / max(rec["n"], 1) if rec["n"] else 5.0

    def sp_hr9(rec):
        return rec["hr"] * 9 / rec["ip"] if rec["ip"] >= 1.0 else 0.94

    def tr(rec, pri, key):
        """팀 지표. 사전값이 있으면 표본 크기로 섞는다."""
        return _shrink(rec[key], rec["g"], (pri or {}).get(key), K_TEAM)

    def sp_era(rec, pri):
        if rec and rec["ip"] >= 1.0:
            return _shrink(rec["er"] * 9, rec["ip"], (pri or {}).get("era"), K_SP)
        return (pri or {}).get("era", 4.5)

    def sp_hr9_(rec, pri):
        if rec and rec["ip"] >= 1.0:
            return _shrink(rec["hr"] * 9, rec["ip"], (pri or {}).get("hr9"), K_SP)
        return (pri or {}).get("hr9", 0.94)

    def sp_ip_(rec, pri):
        if rec and rec["n"]:
            return rec["ip"] / rec["n"]
        return (pri or {}).get("ip", 5.0)

    my_rs, my_ra = tr(a, pa, "rs"), tr(a, pa, "ra")
    op_rs, op_ra = tr(o, po_, "rs"), tr(o, po_, "ra")
    my_era, op_era = sp_era(pm, ppm), sp_era(po, ppo)

    return {
        "home": 1.0 if is_home else 0.0,
        "park": (parks or PARK).get(stadium, 1.0),
        "my_rs": my_rs, "my_ra": my_ra,
        "my_hr": tr(a, pa, "hr"), "my_hra": tr(a, pa, "hra"),
        "my_win": tr(a, pa, "w"),
        "op_rs": op_rs, "op_ra": op_ra,
        "op_hr": tr(o, po_, "hr"), "op_hra": tr(o, po_, "hra"),
        "op_win": tr(o, po_, "w"),
        "my_sp_era": my_era, "my_sp_hr9": sp_hr9_(pm, ppm),
        "my_sp_ip": sp_ip_(pm, ppm),
        "op_sp_era": op_era, "op_sp_hr9": sp_hr9_(po, ppo),
        "op_sp_ip": sp_ip_(po, ppo),
        "off_edge": my_rs - op_ra,
        "def_edge": op_rs - my_ra,
        "sp_edge": op_era - my_era,
        "my_bp_era": _era(ab, floor=10.0), "op_bp_era": _era(ob, floor=10.0),
        # 최근 10경기가 없으면 작년 승률로 대신한다(개막 직후).
        "my_form10": (sum(a["recent"][-10:]) / len(a["recent"][-10:])
                      if a["recent"] else (pa or {}).get("w", 0.5)),
        "op_form10": (sum(o["recent"][-10:]) / len(o["recent"][-10:])
                      if o["recent"] else (po_ or {}).get("w", 0.5)),
        "my_pyth": pyth(my_rs, my_ra),
        "op_pyth": pyth(op_rs, op_ra),
        "my_rest": _days(a["last"], date), "op_rest": _days(o["last"], date),
        # 올 시즌 상대전적이 없으면 작년 것을 쓴다.
        "h2h_win": (hh[0] / hh[1] if hh[1]
                    else _prior_h2h(prior, tm, op)),
        "my_sp_recent": (_recent_era(pm) if pm and pm["ip"] >= 5
                         else my_era),
        "op_sp_recent": (_recent_era(po) if po and po["ip"] >= 5
                         else op_era),
        "sp_ip_edge": sp_ip_(pm, ppm) - sp_ip_(po, ppo),
        # 최근 10경기 홈런 흐름. 없으면 시즌(축소) 값으로 대신한다.
        "my_hr10": _recent_rate(a, "recent_hr", tr(a, pa, "hr")),
        "op_hra10": _recent_rate(o, "recent_hra", tr(o, po_, "hra")),
        "my_hr_trend": (_recent_rate(a, "recent_hr", tr(a, pa, "hr"))
                        - tr(a, pa, "hr")),
    }


def state_through(games: list, before: str | None = None) -> dict:
    """주어진 날짜 **이전** 경기까지만 누적한다. 운영에서 오늘 특징을 만들 때 쓴다."""
    st = new_state()
    used = []
    for g in sorted(games, key=lambda x: x.get("date", "")):
        if before and g.get("date", "") >= before:
            break
        feed(st, g)
        used.append(g)
    # 사전값으로 쓸 때 작년 구장 팩터도 함께 넘긴다.
    st["parks"] = park_hr_factors(used)
    return st


def build_rows(games: list, prior: Optional[dict] = None,
               parks: Optional[dict] = None) -> list:
    """팀-경기 표본을 만든다. 그 시점까지의 누적만 특징으로 쓴다.

    미래 정보가 새면 검증 성적만 좋아지고 실제 예측은 틀린다. 누적 갱신은
    반드시 특징을 다 만든 뒤에 한다(feed 를 나중에 부른다).
    """
    st = new_state()
    # 그 시즌 기록으로 구장 팩터를 계산한다. 하드코딩 값을 쓰면 신규 구장이
    # 생긴 해에 시즌 내내 틀린 값을 쓴다.
    # 구장 팩터는 **그 날짜 이전 경기로만** 계산한다.
    #
    # 시즌 전체로 계산하면 미래 경기의 홈런이 들어가고, 운영은 그 시점까지만
    # 쓰므로 학습과 운영이 갈린다. 실측으로 홈런 확률이 최대 11%p 어긋나고
    # 추천이 792건 중 143건에서 달랐다. park 은 홈런 모델의 주요 특징이라
    # 그대로 두면 계속 조용히 틀린다.
    fixed_parks = parks
    prior_parks = (prior or {}).get("parks")
    rows = []
    # **날짜 단위로 처리한다.** 같은 날 경기를 하나씩 반영하면, 그날 두 번째
    # 경기가 첫 경기 결과를 보게 된다. 운영에서는 그날 경기를 전부 빼고
    # 예측하므로, 그렇게 학습하면 학습과 운영의 특징이 달라진다 —
    # 실측으로 확률이 최대 49%p 까지 벌어졌다.
    by_date: dict = {}
    for g in games:
        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        by_date.setdefault(g.get("date", ""), []).append(g)

    seen: list = []
    for date in sorted(by_date):
        todays = by_date[date]
        # 그 날짜 이전 경기까지로 팩터를 만든다. 운영과 같은 조건이다.
        parks = fixed_parks if fixed_parks is not None else             park_hr_factors(seen, prior=prior_parks)
        for x in todays:
            hs, as_ = x["home_score"], x["away_score"]
            pitchers = x.get("pitchers") or {}

            def starter(side):
                for pp in (pitchers.get(side) or []):
                    if pp.get("started"):
                        return pp
                return None

            for me, foe, is_home in (("home", "away", True),
                                     ("away", "home", False)):
                ms, os_ = starter(me), starter(foe)
                if not ms or not os_:
                    continue
                my, oy = (hs, as_) if is_home else (as_, hs)
                f = featurize(st, x.get(me), x.get(foe), is_home,
                              x.get("stadium", ""), ms.get("pcode"),
                              os_.get("pcode"), date, strict=True, prior=prior,
                              parks=parks)
                if f is None:
                    continue
                rows.append({"date": date, "team": x.get(me),
                             "feat": [f[n] for n in FEATURES],
                             "season": int(str(date)[:4] or 0),
                             "y": 0 if my > oy else (2 if my < oy else 1)})
        # 그날 경기를 전부 featurize 한 뒤에 반영한다
        for x in todays:
            feed(st, x)
        seen += todays
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
    out = {"n": len(p), "overall": round(float(ok.mean()) * 100, 1), "tiers": [],
           "targets": []}

    # 목표 적중률을 실제로 넘는 확신도 문턱을 찾는다.
    #
    # "무조건 70% 이상" 은 매 경기로는 불가능하다 — 시즌 최종 순위를 미리
    # 아는 반칙 오라클도 59.2% 이고, 리그 1위가 최하위를 만나도 77.9% 다.
    # 대신 **어느 날이 70% 구간인지** 는 말할 수 있다. 문턱을 넘는 날에만
    # '70% 이상' 이라고 표시하면 그 약속은 실제로 지켜진다.
    order = np.argsort(-p)
    for target in (70.0, 75.0, 80.0):
        found = None
        # 확신도가 높은 순으로 늘려가며, 그 집합의 적중률이 목표를 넘는
        # 가장 넓은 구간을 찾는다. 표본이 40건은 되어야 믿는다.
        for m in range(len(order), 39, -1):
            idx = order[:m]
            if float(ok[idx].mean()) * 100 >= target:
                found = {"target": target,
                         "threshold": round(float(p[idx].min()), 4),
                         "games": m,
                         "share": round(m / len(p), 4),
                         "accuracy": round(float(ok[idx].mean()) * 100, 1)}
                break
        if found:
            out["targets"].append(found)
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


def deviation_rule(rows: list, C: float, team: str = "LG") -> dict:
    """이 팀에 대해 모델이 '팀 전력이 가리키는 답' 에서 벗어나도 되는가.

    강팀은 그냥 자주 이긴다. 경기별 예측 기술이 그보다 약하면, 모델이
    다른 답을 낼 때마다 손해다. 실측(LG 3시즌 419경기):

        모델 패 확률 50% 이상 80경기 → 실제 패 40.0%
        모델 패 확률 55% 이상 31경기 → 실제 패 38.7%

    가장 확신할 때조차 LG 가 61% 이겼다. 즉 벗어날 문턱이 없다.

    그래서 문턱을 상수로 박지 않고 여기서 잰다. 벗어나서 이득인 구간이
    없으면 threshold 를 None 으로 두고, 운영은 팀 전력 쪽 답을 따른다.
    LG 가 중위권이 되면 문턱이 생기고 모델이 다시 쓰인다.
    """
    import numpy as np

    seg = [r for r in rows if r.get("team") == team]
    if len(seg) < 120:
        return {"team": team, "threshold": None, "n": len(seg),
                "note": "표본 부족"}

    # 그 시점까지로 학습해 다음 경기를 맞히는 방식으로 확률을 모은다.
    newest = max(r.get("season", 0) for r in rows)
    past = [r for r in rows if r.get("season", newest) != newest]
    cur = [r for r in rows if r.get("season", newest) == newest]
    probs = []
    for frac in (0.5, 0.65, 0.8):
        cut = int(len(cur) * frac)
        tr, te = past + cur[:cut], [r for r in cur[cut:] if r.get("team") == team]
        if len(te) < 10:
            continue
        sc, m = fit(tr, C)
        P = m.predict_proba(sc.transform(core_matrix(te)))
        cls = list(m.classes_)
        for i, r in enumerate(te):
            probs.append((float(P[i][cls.index(2)]) if 2 in cls else 0.0,
                          r["y"]))
    if len(probs) < 40:
        return {"team": team, "threshold": None, "n": len(probs),
                "note": "표본 부족"}

    best = None
    for th in (0.50, 0.55, 0.60, 0.65, 0.70):
        sel = [y for p, y in probs if p >= th]
        if len(sel) < 15:
            continue
        lose = sum(1 for y in sel if y == 2) / len(sel)
        # 벗어나서 이득이려면, 그 구간에서 실제로 지는 비율이 절반을 넘어야 한다.
        if lose > 0.5 and (best is None or lose > best["accuracy"]):
            best = {"threshold": th, "games": len(sel),
                    "accuracy": round(lose, 4)}

    # 벗어나지 않을 때 무엇을 고를지도 잰다.
    #
    # 홈 보정을 넣으면 원정 경기에서 확률이 0.5 아래로 밀려 '패' 가 된다.
    # LG 처럼 강한 팀에서는 그게 손해였다(3시즌 모두 '항상 승' 이 나음).
    # 어느 쪽이 나은지 상수로 정하지 않고 여기서 잰다.
    hi = FEATURES.index("home")
    wi = FEATURES.index("my_win")
    ways = {}
    for label, edge in (("winrate", 0.0), ("winrate+home", 0.035)):
        for cut in (0.50, 0.45, 0.40, 0.35, 0.00):
            hit = 0
            for r in seg:
                pw = r["feat"][wi] + (edge if r["feat"][hi] else -edge)
                hit += (r["y"] == (0 if pw >= cut else 2))
            ways[f"{label}@{cut:.2f}"] = round(hit / len(seg) * 100, 1)
    fallback = max(ways, key=ways.get)

    return {"team": team, "n": len(probs),
            "threshold": best["threshold"] if best else None,
            "detail": best,
            "fallback": fallback.split("@")[0],
            "fallbackCut": float(fallback.split("@")[1]),
            "fallbackScores": dict(sorted(ways.items(),
                                          key=lambda kv: -kv[1])[:4]),
            "note": ("벗어날 값어치 있음" if best
                     else "어떤 문턱에서도 벗어나면 손해 — 팀 전력 쪽을 따른다")}


def rows_by_season(paths: list) -> list:
    """시즌마다 따로 펼쳐 합친다.

    여러 시즌을 이어붙여 한 번에 펼치면 팀 누적이 시즌 경계를 넘어가서,
    2025 개막전 팀이 2024 성적을 들고 나온다. 실제로 이 탓에 성적이
    59.8% 대신 55.3% 로 나왔다.
    """
    import re
    out = []
    for path in paths:
        m = re.search(r"gamelog_(\d{4})\.json$", path.replace("\\", "/"))
        prior = None
        if m:
            # 작년 최종 성적을 사전값으로 넘긴다. 개막 직후에도 예측이 나온다.
            try:
                prior = state_through(load(f"gamelog_{int(m.group(1)) - 1}.json"))
            except FileNotFoundError:
                prior = None
        try:
            out += build_rows(load(path), prior=prior)
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
    if ct.get("targets"):
        print("", file=sys.stderr)
        print("목표 적중률을 실제로 넘는 구간", file=sys.stderr)
        for t in ct["targets"]:
            print(f"  {t['target']:.0f}% 이상: 확신도 {t['threshold'] * 100:.0f}% 넘는 날 "
                  f"(전체의 {t['share'] * 100:.0f}%, {t['games']}경기) "
                  f"→ 실측 {t['accuracy']:.1f}%", file=sys.stderr)
    else:
        print("  목표 적중률을 넘는 구간이 없다", file=sys.stderr)
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
        "deviation": deviation_rule(rows, args.C),
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
