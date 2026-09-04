#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""학습된 승패 모델을 운영에서 쓴다 (scikit-learn 없이).

`train_outcome.py` 가 뽑아 `outcome_model.json` 에 넣어둔 계수만 읽어
확률을 계산한다. 매일 도는 파이프라인에 무거운 학습 라이브러리를 넣지
않는다 — 느려지고 고장 지점만 늘어난다.

특징은 `train_outcome.featurize()` 를 그대로 부른다. 여기서 특징을 다시
구현하면 학습과 운영이 갈려서, 에러 없이 예측만 틀린다.

    from outcome_infer import load_model, predict_outcome
    m = load_model()
    p = predict_outcome(m, gamelog_games, "LG", "OB", is_home=False,
                        stadium="잠실", my_sp="12345", op_sp="67890",
                        date="2026-09-02")
    # → {"승": 0.51, "무": 0.08, "패": 0.41, "confidence": "높음", ...}
"""
from __future__ import annotations

import json
import math
import os
from typing import Optional

import train_outcome as T

MODEL_FILE = "outcome_model.json"
HR_MODEL_FILE = "hr_model.json"

_PRIOR_CACHE: dict = {}

# 모델 확률을 '팀 승률' 쪽으로 얼마나 당길지. 1.0 이면 모델만.
#
# **이 도구는 LG 스타볼용이다.** 전 구단 평균으로 최적화하면 LG 에 손해다.
# 실측(2024~2025 학습 → 2026 검증):
#
#     섞는 비율      전체     LG     강팀    중위    약팀
#     모델만        55.1%  47.3%  51.7%  57.6%  55.1%
#     모델 0.3      55.8%  59.5%  57.8%  53.4%  56.6%
#     승률만        55.3%  58.1%  58.3%  52.1%  56.2%
#
# 모델은 중위권에서만 이긴다. 강팀은 그냥 자주 이겨서, 경기별 예측 기술보다
# '이 팀이 세다' 는 사실이 더 세다. LG 는 강팀이므로 승률 쪽에 무게를 둔다.
# LG 가 약팀이 되면 이 값을 다시 재야 한다 — 중위권이면 모델만 쓰는 게 낫다.
OUTCOME_BLEND = 0.3
HOME_EDGE = 0.035        # 홈 어드밴티지(승률 환산). 실측 홈 승률 약 53.5%
DRAW_SHARE = 0.02        # KBO 무승부 비율


def season_prior(date: str) -> Optional[dict]:
    """작년 최종 성적. 학습이 쓰는 것과 **똑같이** 운영에서도 넘겨야 한다.

    이걸 빼먹으면 시즌 초 투수의 ERA 가 축소되지 않는다. 실제로 1/3이닝에
    자책 2점을 준 투수가 ERA 54.00 으로 들어가서, 학습(7.86)과 운영(54.00)이
    46%p 어긋났다. 에러는 안 나고 예측만 틀린다 — 가장 잡기 어려운 사고다.
    """
    try:
        year = int(str(date)[:4]) - 1
    except (TypeError, ValueError):
        return None
    if year in _PRIOR_CACHE:
        return _PRIOR_CACHE[year]
    try:
        _PRIOR_CACHE[year] = T.state_through(T.load(f"gamelog_{year}.json"))
    except (FileNotFoundError, ValueError):
        _PRIOR_CACHE[year] = None
    return _PRIOR_CACHE[year]


def load_model(path: str = MODEL_FILE) -> Optional[dict]:
    """계수를 읽는다. 없거나 형식이 다르면 None — 부르는 쪽이 옛 모델로 돌아간다.

    조용히 틀린 값을 내는 것보다 '이 기능은 꺼졌다' 가 낫다.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (ValueError, OSError):
        return None

    need = ("classes", "features", "coef", "intercept", "mean", "scale")
    if any(k not in m for k in need):
        return None
    n_f = len(m["features"])
    n_c = len(m["classes"])
    # 계수 모양이 안 맞으면 특징 목록이 바뀐 뒤 재학습을 안 한 것이다.
    if len(m["coef"]) != n_c or any(len(row) != n_f for row in m["coef"]):
        return None
    if len(m["intercept"]) != n_c or len(m["mean"]) != n_f or len(m["scale"]) != n_f:
        return None
    if any(n not in T.FEATURES for n in m["features"]):
        return None
    return m


def load_hr_model(path: str = HR_MODEL_FILE) -> Optional[dict]:
    """홈런 모델 계수. 승패와 같은 검사를 거친다."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (ValueError, OSError):
        return None
    need = ("classes", "features", "coef", "intercept", "mean", "scale")
    if any(k not in m for k in need):
        return None
    n_f, n_c = len(m["features"]), len(m["classes"])
    if len(m["coef"]) != n_c or any(len(r) != n_f for r in m["coef"]):
        return None
    if len(m["intercept"]) != n_c or len(m["mean"]) != n_f or len(m["scale"]) != n_f:
        return None
    if any(n not in T.FEATURES for n in m["features"]):
        return None
    return m


def predict_hr(model: dict, games: list, tm: str, op: str, is_home: bool,
               stadium: str, my_sp: str, op_sp: str,
               date: str) -> Optional[dict]:
    """홈런 수 구간별 확률.

    매 경기 0개만 내던 문제를 고치기 위한 것이다. 실제 기록에서 기대 홈런이
    0.9 를 넘는 구간(전체의 3분의 1)에서는 1개가 최빈이다.
    """
    if not model:
        return None
    state = T.state_through(games, before=date)
    # 구장 팩터는 그 시즌 기록에서 계산한다. 하드코딩 값을 쓰면 신규 구장이
    # 생긴 해에 시즌 내내 틀린다(2025 신규 대전에서 실제로 24% 어긋났다).
    # 사전값(작년 팩터)까지 학습과 똑같이 넘겨야 한다. 이걸 빼먹어서
    # 홈런 확률이 11%p 어긋나 있었다.
    _pr = season_prior(date)
    parks = T.park_hr_factors([g for g in games if g.get("date", "") < date],
                              prior=(_pr or {}).get("parks"))
    f = T.featurize(state, tm, op, is_home, stadium, my_sp, op_sp, date,
                    strict=False, parks=parks,
                    prior=season_prior(date))
    if f is None:
        return None
    x = [(f[n] - model["mean"][i]) / (model["scale"][i] or 1.0)
         for i, n in enumerate(model["features"])]
    zs = [sum(c * v for c, v in zip(row, x)) + b
          for row, b in zip(model["coef"], model["intercept"])]
    probs = _softmax(zs)
    return {c: round(p, 4) for c, p in zip(model["classes"], probs)}


def _softmax(zs: list) -> list:
    hi = max(zs)
    es = [math.exp(z - hi) for z in zs]
    tot = sum(es) or 1.0
    return [e / tot for e in es]


def tier_of(model: dict, top: float) -> str:
    """확신도가 어느 등급인지. 문턱은 학습 때 실측으로 잰 값이다."""
    tiers = ((model.get("confidence") or {}).get("tiers") or [])
    best = None
    for t in sorted(tiers, key=lambda t: t.get("share", 1.0)):
        if top >= t.get("threshold", 1.0):
            best = t
            break
    if not best:
        return "낮음"
    if best["share"] <= 0.05:
        return "매우 높음"
    if best["share"] <= 0.10:
        return "높음"
    if best["share"] <= 0.20:
        return "보통"
    return "낮음"


def tier_accuracy(model: dict, top: float) -> Optional[float]:
    """그 등급에서 과거에 실제로 몇 % 맞았는지. 화면에 그대로 보여줄 값."""
    for t in sorted((model.get("confidence") or {}).get("tiers") or [],
                    key=lambda t: t.get("share", 1.0)):
        if top >= t.get("threshold", 1.0):
            return t.get("accuracy")
    return (model.get("confidence") or {}).get("overall")


def target_band(model: dict, top: float) -> Optional[dict]:
    """오늘이 어느 '목표 적중률' 구간인지. 아니면 None.

    "무조건 70% 이상" 은 매 경기로는 불가능하다(반칙 오라클도 59.2%).
    대신 문턱을 넘는 날에만 그렇게 표시하면 그 약속은 지켜진다.
    문턱과 실측치는 학습 때 잰 값이라, 재학습하면 함께 갱신된다.
    """
    best = None
    for t in (model.get("confidence") or {}).get("targets") or []:
        if top >= t.get("threshold", 1.0):
            if not best or t["target"] > best["target"]:
                best = t
    return best


def predict_outcome(model: dict, games: list, tm: str, op: str, is_home: bool,
                    stadium: str, my_sp: str, op_sp: str,
                    date: str) -> Optional[dict]:
    """오늘 경기의 승/무/패 확률. 특징을 못 만들면 None.

    games 는 gamelog 의 경기 목록이다. date **이전** 경기만 누적에 넣으므로
    미래 정보가 새지 않는다.
    """
    if not model:
        return None
    state = T.state_through(games, before=date)
    # 사전값(작년 팩터)까지 학습과 똑같이 넘겨야 한다. 이걸 빼먹어서
    # 홈런 확률이 11%p 어긋나 있었다.
    _pr = season_prior(date)
    parks = T.park_hr_factors([g for g in games if g.get("date", "") < date],
                              prior=(_pr or {}).get("parks"))
    f = T.featurize(state, tm, op, is_home, stadium, my_sp, op_sp, date,
                    strict=False, parks=parks,
                    prior=season_prior(date))
    if f is None:
        return None

    names = model["features"]
    x = []
    for i, n in enumerate(names):
        sc = model["scale"][i] or 1.0
        x.append((f[n] - model["mean"][i]) / sc)

    zs = [sum(c * v for c, v in zip(row, x)) + b
          for row, b in zip(model["coef"], model["intercept"])]
    temp = model.get("temperature") or 1.0
    probs = _softmax([z / temp for z in zs])

    # 팀 승률 쪽으로 당긴다. 강팀(LG)에서 모델만 쓰면 '항상 승' 보다 나쁘다.
    raw = dict(zip(model["classes"], probs))
    pw = min(0.95, max(0.05,
                       f.get("my_win", 0.5)
                       + (HOME_EDGE if f.get("home") else -HOME_EDGE)))
    prior = {"승": pw * (1 - DRAW_SHARE), "무": DRAW_SHARE,
             "패": (1 - pw) * (1 - DRAW_SHARE)}
    a = OUTCOME_BLEND
    mixed = {k: a * raw.get(k, 0.0) + (1 - a) * prior.get(k, 0.0) for k in raw}
    tot = sum(mixed.values()) or 1.0
    mixed = {k: v / tot for k, v in mixed.items()}

    out = {c: round(p, 4) for c, p in mixed.items()}
    out["modelOnly"] = {c: round(p, 4) for c, p in raw.items()}
    out["teamPrior"] = {c: round(p, 4) for c, p in prior.items()}
    probs = list(mixed.values())
    top = max(probs)
    out["confidence"] = tier_of(model, top)
    out["tierAccuracy"] = tier_accuracy(model, top)
    band = target_band(model, top)
    if band:
        out["band"] = band["target"]
        out["bandAccuracy"] = band["accuracy"]
        out["bandShare"] = band["share"]
    out["source"] = "학습 모델"
    return out
