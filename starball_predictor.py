#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
starball_predictor.py — LG 트윈스 '스타볼' 예측 자동화 파이프라인

데이터 수집 → 예측 모델 → CLI 리포트 → (선택) ntfy 푸시 알림.

사용 예:
    python starball_predictor.py                         # 오늘 LG 경기 예측
    python starball_predictor.py --date 2026-08-28
    python starball_predictor.py --probe                 # 엔드포인트 생존 점검
    python starball_predictor.py --save-fixture t.json   # 응답 스냅샷 저장
    python starball_predictor.py --fixture t.json        # 오프라인 재현
    python starball_predictor.py --json                  # 기계 판독용 출력
    python starball_predictor.py --ntfy                  # 예측 후 푸시 발송
    python starball_predictor.py --ntfy --ntfy-topic my_topic

푸시 토픽 우선순위: --ntfy-topic > $NTFY_TOPIC > 'lg_starball_predict_2026'.
ntfy 토픽은 누구나 구독할 수 있는 공개 채널이므로, 남이 봐서 곤란한 내용은
넣지 않는다(현재 본문은 공개 경기 기록에서 나온 예측뿐이다).

데이터 출처: 네이버 스포츠 비공식 API (api-gw.sports.naver.com).
공개 문서가 없는 내부 엔드포인트라 언제든 스키마가 바뀔 수 있다. 그래서
수집(NaverKBO)과 모델(predict)을 완전히 분리했고, 수집이 깨져도 --fixture 로
모델과 출력은 항상 돌아간다. 어디가 깨졌는지는 --probe 가 알려준다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
import requests

# ─────────────────────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────────────────────

API = "https://api-gw.sports.naver.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MY_TEAM = "LG"

# 한국은 1988년 이후 서머타임이 없으므로 고정 +9 오프셋이 항상 정확하다.
# zoneinfo 를 쓰면 윈도우에서 tzdata 패키지가 필요해지므로 일부러 피했다.
KST = timezone(timedelta(hours=9))


def today_kst() -> date:
    """KBO 경기일 기준 '오늘'.

    date.today() 를 쓰면 실행 환경의 로컬 날짜가 나온다. 깃헙 액션 러너는
    UTC라서, 한국 시간 오전 9시 이전에 돌면 어제 날짜로 경기를 찾게 된다.
    클라우드에서 돌릴 거라 반드시 KST 로 고정해야 한다.
    """
    return datetime.now(KST).date()

TEAM_NAMES = {
    "LG": "LG", "OB": "두산", "SS": "삼성", "HH": "한화", "KT": "KT",
    "SK": "SSG", "WO": "키움", "HT": "KIA", "LT": "롯데", "NC": "NC",
}

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
CACHE_TTL = 60 * 30          # 30분. 경기 전 정보는 이 정도면 충분히 신선하다.
POLITE_DELAY = 0.4           # 연속 요청 사이 최소 간격(초)

# ── 모델 하이퍼파라미터 ───────────────────────────────────────────────────────
# 튜닝은 이 블록만 만지면 된다.

W_OFF_SEASON = 0.55     # 팀 득점력: 시즌 전체
W_OFF_VS = 0.20         #            상대팀 한정
W_OFF_RECENT = 0.25     #            최근 경기

W_SP_SEASON = 0.55      # 선발 실점력: 시즌 전체
W_SP_VS = 0.25          #              상대팀 한정
W_SP_RECENT = 0.20      #              최근 등판(박스스코어에서 복원)

# 축소(shrinkage) 상수. 표본이 작을수록 리그 평균 쪽으로 끌어당긴다.
# "상대전적 2경기 ERA 9.00"을 액면가로 믿으면 모델이 망가지므로 필수다.
K_SHRINK_IP = 25.0      # 투수 상대 ERA: 25이닝만큼의 가상 리그평균을 섞는다
K_SHRINK_GAMES = 20.0   # 팀 상대 득점: 20경기만큼
K_SHRINK_RECENT_IP = 20.0  # 투수 최근 등판 ERA. 5등판이면 25이닝쯤이라 절반 넘게 남는다
K_SHRINK_RECENT = 8.0   # 최근 경기 페이스. 5경기면 리그평균 쪽에 약간 더 붙는다
K_SHRINK_HR_IP = 50.0   # (COUNT_STATS 의 k_ip 로 이관됨, 하위호환용)

# 시즌 개막 직후에는 집계가 전부 0이라 리그 평균을 낼 수 없다. 그때 쓰는 값.
# COUNT_STATS 의 fallback 과 함께 2026시즌 실측 수준으로 맞춰뒀다.
LEAGUE_RPG_FALLBACK = 4.90
LEAGUE_ERA_FALLBACK = 4.40

ER_TO_R = 1.08          # 자책점 → 총실점 환산(비자책 포함)
HOME_FIELD_RUNS = 0.15  # 홈 어드밴티지(득점 환산). KBO 홈 승률 ≈ .535
SP_IP_MIN, SP_IP_MAX = 3.0, 6.5   # 선발 예상 소화 이닝 클램프

NB_PHI_TEAM = 4.0       # 팀 득점 분포의 과산포. var = mu + mu^2/phi
NB_PHI_SP = 3.0         # 선발 실점 분포의 과산포
DRAW_SHARE = 0.35       # 9회 동점 중 무승부로 끝나는 비율(나머지는 연장 승부)
# ── 백테스트로 측정한 값 (2026시즌 529경기, backtest.py) ─────────────────────
# 모델은 과신한다. 65%라고 말한 예측이 실제로는 52%만 맞았다.
# 확률을 균등분포 쪽으로 눌러 실제 발생률과 맞춘다.
#   p' = u + (p - u) · PROB_CALIBRATION       (u = 1/선택지수)
PROB_CALIBRATION = 0.76

# 모델 확률을 리그 기저 분포와 섞는 비율. 1.0 이면 모델만, 0.0 이면 기저만.
# 모델이 근거 없이 기저에서 벗어나면 오히려 손해라, 백테스트로 적합한다.
# 정확도와 다양성의 맞바꿈. 468경기 실측:
#   α    서로 다른 조합  흔한값 비중  3미션 동시
#   1.0        5개       66%     12.21%
#   0.7        4개       80%     12.21%   ← 채택
#   0.5        4개       92%     12.42%
#   0.3        2개      100%     12.63%
#   0.0        1개      100%     12.63%
#
# 적중률 차이는 전부 표준오차(±1.55%p) 안이다 — 468경기에서 2경기 차이라
# 통계적으로 구별되지 않는다. 그래서 이 값은 정확도가 아니라 제품 판단으로
# 골랐다: α 가 낮으면 출력이 사실상 상수가 되어(92~100% 동일) 도구를 열
# 이유가 없어진다. 0.7 이면 5경기 중 1번은 다른 답이 나오면서 측정 가능한
# 손해는 없다.
BASE_RATE_BLEND = 0.7

BASE_RATES_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "base_rates.json")
BASE_RATES: dict = {}
BASE_COMBO: dict = {}


def _load_base_rates() -> None:
    """리그 기저 분포를 불러온다.

    이 파일이 없으면 혼합이 통째로 무력화되어 모델이 사실상 α=1.0 으로 돈다.
    백테스트 기준 12.18% vs 13.03% 차이인데 아무 표시 없이 나빠지므로,
    없으면 반드시 경고한다. 배포 시 함께 올려야 하는 파일이다.
    """
    if not os.path.exists(BASE_RATES_FILE):
        print(f"경고: {os.path.basename(BASE_RATES_FILE)} 가 없습니다. "
              f"기저 분포 혼합이 꺼진 채로 동작합니다(정확도 하락). "
              f"python build_base_rates.py 로 만드세요.", file=sys.stderr)
        return
    try:
        with open(BASE_RATES_FILE, encoding="utf-8") as f:
            d = json.load(f)
        rates = d.get("per_question") or {}
        combo = {tuple(k.split("|")): v
                 for k, v in (d.get("joint") or {}).items()}

        # 라벨 정합성 검사. 문항을 바꾸고 기저를 다시 안 만들면 라벨이 어긋나
        # 기저 확률이 전부 0 으로 섞이고, 정규화 탓에 혼합이 '조용히' 무력화된다.
        # 결과는 α=1.0 과 같아지는데 화면에는 아무 표시도 안 난다.
        stale = []
        for q in STARBALL_QUESTIONS:
            want = {lbl for lbl, *_ in (q.get("categorical") or q.get("buckets") or [])}
            have = set(rates.get(q["key"]) or {})
            if not have:
                stale.append(f"{q['label']}: 기저 없음")
            elif not (want & have):
                stale.append(f"{q['label']}: 라벨 불일치 "
                             f"(문항 {sorted(want)} vs 기저 {sorted(have)})")
        if stale:
            print("경고: base_rates.json 이 현재 문항과 맞지 않습니다 —",
                  file=sys.stderr)
            for line in stale:
                print(f"  · {line}", file=sys.stderr)
            print("  기저 혼합을 끕니다. python build_base_rates.py 로 다시 만드세요.",
                  file=sys.stderr)
            return

        BASE_RATES.update(rates)
        BASE_COMBO.update(combo)
    except Exception as e:
        print(f"base_rates.json 을 읽지 못했습니다({e})", file=sys.stderr)

# 문항별 실측 성적: 가장 흔한 선택지를 계속 찍는 것 대비 개선폭(%p).
# 이 값이 SKILL_THRESHOLD 미만이면 모델이 정보를 못 주는 문항이므로
# 추천으로 내세우지 않는다. 재측정: python backtest.py
#
# 주의: 아래는 전 구단 529경기 기준이다. LG 경기만 따로 재면(107경기)
# 승패가 -1.9%p 로 떨어진다. 표준오차 ±4.8%p 안이라 '나쁘다'기보다
# '구별 불가'지만, LG 는 강팀이라 "항상 LG 승" 기준선(55%)이 높다는 뜻이다.
# 모델은 팀별로 다르지 않으므로 표본이 큰 전 구단 값을 게이팅에 쓴다.
#   python backtest.py --team LG   로 직접 확인할 수 있다.
#
# 더 중요한 경고: 표본 468경기의 표준오차가 약 ±2.3%p다. 유일하게 임계값을
# 넘는 승패(+2.4%p)조차 그 범위 안이라, 통계적으로 '입증됐다'고 말할 수 없다.
# 절제 실험(ablation.py) 결과 전체 모델 53.2% vs '항상 홈팀' 51.7% = +1.5%p,
# 이것도 표준오차 ±2.1%p 안이다.
QUESTION_SKILL = {
    # 실측 적중률과 '가장 흔한 값만 찍기' 대비 개선폭(%p).
    # 2026-09-01 backtest.py, 표본 475경기. 앱의 실제 드롭다운 구조
    # (득실 차 0~9점이상 10개 · 홈런 0~5개이상 6개) 기준.
    # 화면에 그대로 띄운다 — 사용자가 문항별로 얼마나 믿을지 스스로 정하게.
    "outcome": {"hit": 53.3, "base": 50.9, "gain": +2.3},
    "margin":  {"hit": 22.5, "base": 22.5, "gain": +0.0},
    "lg_hr":   {"hit": 41.9, "base": 41.9, "gain": +0.0},
}
SKILL_THRESHOLD = 2.0

N_SIM = 50_000
SEED = 20260827

# ── 카운팅 스탯 모델 ─────────────────────────────────────────────────────────
# 득점 외의 문항(홈런/안타/삼진)은 전부 같은 구조로 처리한다.
#   off  = 그 팀 타선의 경기당 산출, dfn = 상대 팀 배터리의 경기당 허용
#   phi  = 음이항 과산포. var = mu + mu^2/phi (작을수록 분산이 크다)
# phi 는 KBO 경기당 분산에 대충 맞춘 값이라 튜닝 여지가 있다.
# 볼넷(offenseBb/defenseBb)은 전 구단 null 로 내려와 쓸 수 없다. 필요하면
# offenseBbhp(볼넷+사구)로 대체해야 한다.
COUNT_STATS = {
    # sp: 선발의 해당 스탯 누적 필드. 이닝 지분만큼 팀 평균 대신 선발 값을 쓴다.
    # k_ip: 그 스탯의 축소 상수(이닝). 안정화 속도가 다르다 —
    #       탈삼진은 빨리, 피안타는 BABIP 탓에 느리게, 피홈런은 가장 느리게.
    # beta: 그 팀의 득점과 묶는 계수. 홈런·안타는 많이 뽑는 날 같이 나온다.
    #       독립으로 뽑으면 세 미션 동시 적중 확률이 어긋난다.
    # phi:  득점을 조건으로 건 뒤의 잔여 과산포. 1138 팀-경기로 수치 보정했다.
    #       홈런은 조건부로 보면 거의 포아송이다 — 종전 3.5 는 사실 득점 상관을
    #       과산포로 잘못 흡수한 값이었다.
    "hr":  {"off": "offenseHr",  "dfn": "defenseHr",  "phi": 400.0, "beta": 0.75,
            "label": "홈런", "fallback": 0.94, "sp": "season_hr", "k_ip": 50.0},
    "hit": {"off": "offenseHit", "dfn": "defenseHit", "phi": 2000.0, "beta": 0.35,
            "label": "안타", "fallback": 9.26, "sp": "season_hit", "k_ip": 40.0},
    "k":   {"off": "offenseKk",  "dfn": "defenseKk",  "phi": 2000.0, "beta": 0.0,
            "label": "삼진", "fallback": 7.61, "sp": "season_kk", "k_ip": 20.0},
}

# 구장별 홈런 팩터(실측 원값). 1.0 이 중립, 낮을수록 홈런이 덜 나오는 구장이다.
# 2026시즌 626경기 박스스코어에서 홈/원정 대비법으로 계산했다(2026-08-27 기준).
# 다시 계산하려면:  python starball_predictor.py --compute-park-factors
# park_factors.json 이 있으면 그 값이 아래를 덮어쓴다.
PARK_HR_FACTOR: dict = {
    "창원": 1.421, "대구": 1.324, "문학": 1.283, "광주": 1.078,
    "대전": 0.861, "고척": 0.837, "사직": 0.781, "수원": 0.749,
    "잠실": 0.665,
}

# 위 원값을 그대로 쓰지 않고 1.0 쪽으로 당겨서 쓴다. 두 가지 이유가 있다.
#  1) 한 시즌 홈경기는 구장당 60여 경기뿐이라 표본 노이즈가 크다.
#  2) 홈/원정 대비법에는 구조적 편향이 있다. 어떤 팀의 '원정'에는 자기 홈구장이
#     빠지므로, 비교 기준이 그 구장을 제외한 평균이 된다. 결과적으로 모든
#     구장의 팩터가 1.0에서 실제보다 멀어지는 방향으로 부풀려진다.
# 0.5 는 한 시즌 표본에 통용되는 보수적인 값이다. 여러 시즌을 모으면 올려도 된다.
PARK_FACTOR_REGRESSION = 0.5


def park_hr_factor(stadium: str) -> float:
    """실제 모델에 쓰는 구장 홈런 팩터(축소 적용 후)."""
    raw = PARK_HR_FACTOR.get(stadium, 1.0)
    return 1.0 + (raw - 1.0) * PARK_FACTOR_REGRESSION

BULLPEN_RA9: dict = {}     # 팀별 불펜 RA9 (선발 제외). park_factors.json 에서 로드

PARK_FACTOR_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "park_factors.json")


def _load_park_factors() -> None:
    """park_factors.json 이 있으면 내장값 위에 덮어쓴다."""
    if not os.path.exists(PARK_FACTOR_FILE):
        return
    try:
        with open(PARK_FACTOR_FILE, encoding="utf-8") as f:
            data = json.load(f)
        PARK_HR_FACTOR.update({k: float(v)
                               for k, v in (data.get("factors") or data).items()})
        BULLPEN_RA9.update({k: float(v)
                            for k, v in (data.get("bullpen_ra9") or {}).items()})
    except Exception as e:
        print(f"park_factors.json 을 읽지 못했습니다({e}) — 중립값 사용",
              file=sys.stderr)

# ── 스타볼 문항 정의 ─────────────────────────────────────────────────────────
# 스타볼 문항은 경기마다 바뀐다. 실제 폼과 다르면 여기만 고치면 된다.
#
#   source  : predict() 가 만드는 시뮬레이션 분포 이름
#   buckets : (표시 라벨, 하한, 상한) — None 은 열린 구간. 경계 포함.
#
# 문항 추가는 dict 한 줄이면 된다. 모델 코드는 건드릴 필요 없다.
# 쓸 수 있는 source: outcome, margin_abs, total_runs, lg_runs, opp_runs,
#   lg_sp_er, opp_sp_er, total_hr, lg_hr, opp_hr, total_hit, lg_hit,
#   opp_hit, total_k
# 2026-08-28 앱 화면 실측. 미션은 3개이고 전부 LG 기준이다.
# 스타볼은 "매치데이 미션을 모두 달성"해야 지급되므로, 문항별 1위가 아니라
# 세 개가 동시에 맞을 확률을 최대화해야 한다(predict() 의 combo).
#
# ※ 미션 2·3 은 드롭다운이라 화면에서 선택지를 못 봤다. 아래는 추정이며
#    실제와 다르면 starball_questions.json 으로 덮어쓰면 된다.
STARBALL_QUESTIONS = [
    {"key": "outcome", "label": "승패 맞히기", "source": "outcome",
     "categorical": [("승", "win"), ("무", "draw"), ("패", "lose")]},
    {"key": "margin", "label": "득실 차 맞히기", "source": "margin_abs",
     "buckets": [("1점", 1, 1), ("2점", 2, 2), ("3점", 3, 3),
                 ("4점 이상", 4, None)]},
    {"key": "lg_hr", "label": "홈런 수 맞히기", "source": "lg_hr",
     "buckets": [("0개", 0, 0), ("1개", 1, 1), ("2개", 2, 2),
                 ("3개 이상", 3, None)]},
]


QUESTIONS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "starball_questions.json")


def _load_questions() -> None:
    """starball_questions.json 이 있으면 내장 문항을 통째로 대체한다.

    스타볼 실제 문항은 이벤트마다 바뀐다. 파이썬을 못 만지는 사람도 문항을
    갱신할 수 있도록 JSON 으로 뺐다. buckets 는 [라벨, 하한, 상한] 형태이고
    null 은 열린 구간이다.
    """
    if not os.path.exists(QUESTIONS_FILE):
        return
    try:
        with open(QUESTIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        qs = data.get("questions") if isinstance(data, dict) else data
        if not qs:
            return
        parsed = []
        for q in qs:
            item = {"key": q["key"], "label": q["label"],
                    "source": q["source"]}
            if q.get("note"):
                item["note"] = q["note"]
            if q.get("categorical"):
                item["categorical"] = [tuple(x) for x in q["categorical"]]
            else:
                item["buckets"] = [tuple(b) for b in q["buckets"]]
            parsed.append(item)
        STARBALL_QUESTIONS[:] = parsed
        print(f"문항 {len(parsed)}개를 {os.path.basename(QUESTIONS_FILE)} 에서"
              f" 불러왔습니다", file=sys.stderr)
    except Exception as e:
        print(f"starball_questions.json 을 읽지 못했습니다({e}) — 내장 문항 사용",
              file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────────────────────

def parse_kbo_innings(s: Any) -> float:
    """KBO 이닝 표기를 실수로. '34.2'는 34.2가 아니라 34와 2/3이닝이다.

    이걸 틀리면 ERA·WHIP 계산이 통째로 어긋나므로 별도 함수로 뺐다.
    """
    if s in (None, "", "-"):
        return 0.0
    if isinstance(s, bool):
        return 0.0
    if isinstance(s, (int, float)):
        # 숫자로 와도 규칙은 같다. 팀 통계의 defenseInning 은 float 로 내려오는데
        # 981.1 은 981.1이닝이 아니라 981과 1/3이닝이다. 문자열과 같은 경로로 보낸다.
        s = f"{s:.1f}"
    text = str(s).strip()

    # 표기가 엔드포인트마다 다르다. 실측으로 확인된 형태만 나열한다.
    #   팀/선수 시즌 스탯  : "34.2", "108 1/3", 981.1(float)
    #   경기 박스스코어    : "5 ⅓", "5⅔", "6"      ← 유니코드 분수
    for glyph, frac in (("⅓", 1 / 3.0), ("⅔", 2 / 3.0)):
        if glyph in text:
            whole = text.replace(glyph, "").strip()
            return (float(whole) if whole else 0.0) + frac

    m = re.match(r"^(\d+)\s+(\d)/3$", text)
    if m:
        return float(m.group(1)) + int(m.group(2)) / 3.0
    m = re.match(r"^(\d+)(?:\.(\d))?$", text)
    if not m:
        return 0.0
    whole = float(m.group(1))
    outs = int(m.group(2)) if m.group(2) else 0
    return whole + (outs / 3.0 if outs in (1, 2) else 0.0)


def fnum(v: Any, default: float = 0.0) -> float:
    try:
        if v in (None, "", "-"):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def shrink(observed: float, prior: float, n: float, k: float) -> float:
    """표본 n의 관측값을 prior 쪽으로 축소. n == k 이면 정확히 반반."""
    if n <= 0:
        return prior
    return (observed * n + prior * k) / (n + k)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 데이터 수집
# ─────────────────────────────────────────────────────────────────────────────

class NaverKBO:
    """네이버 스포츠 KBO 비공식 API 클라이언트.

    2026-08-27 실측으로 확인한 엔드포인트:
      GET /schedule/calendar?upperCategoryId=kbaseball&categoryIds=kbo&yearMonth=YYYY-MM
      GET /schedule/games/{gameId}
      GET /schedule/games/{gameId}/preview
      GET /statistics/categories/kbo/seasons/{year}/teams

    /schedule/games?fromDate=..&toDate=.. 는 200을 주지만 항상 빈 배열이라
    쓰지 않는다. 일정은 calendar 로 받는다.
    gameId 포맷: YYYYMMDD + AWAY(2) + HOME(2) + '0' + YEAR
    """

    def __init__(self, use_cache: bool = True, verbose: bool = False):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": "https://m.sports.naver.com/kbaseball/schedule/index",
            "Accept": "application/json, text/plain, */*",
        })
        self.use_cache = use_cache
        self.verbose = verbose
        self.calls: list[str] = []
        self._last_call = 0.0
        self._season_start: dict = {}
        if use_cache:
            os.makedirs(CACHE_DIR, exist_ok=True)

    def _cache_path(self, url: str) -> str:
        # 읽을 수 있게 URL 꼬리를 남기되, 구분은 전체 URL 해시로 한다.
        # 꼬리만 쓰면 앞부분만 다른 URL 이 같은 파일을 덮어쓴다(달력 URL 이
        # 이미 123자라 절단 구간에 걸려 있었다).
        tail = re.sub(r"[^A-Za-z0-9]+", "_", url)[-90:]
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        return os.path.join(CACHE_DIR, f"{tail}_{digest}.json")

    def get(self, path: str, **params) -> dict:
        url = f"{API}{path}"
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        cp = self._cache_path(url)

        if self.use_cache and os.path.exists(cp):
            if time.time() - os.path.getmtime(cp) < CACHE_TTL:
                with open(cp, encoding="utf-8") as f:
                    return json.load(f)

        gap = time.time() - self._last_call
        if gap < POLITE_DELAY:
            time.sleep(POLITE_DELAY - gap)
        self._last_call = time.time()

        if self.verbose:
            print(f"  -> GET {url}", file=sys.stderr)
        self.calls.append(url)

        r = self.s.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not data.get("success", False):
            raise RuntimeError(f"API가 success=false 응답: {url}")

        if self.use_cache:
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        return data

    # ── 공개 메서드 ──────────────────────────────────────────────────────
    def calendar(self, year: int, month: int) -> list[dict]:
        """해당 월의 일정.

        주의: yearMonth 만 넘기면 서버가 그걸 무시하고 '현재 월'을 돌려준다.
        실제로 월을 고르는 건 date 파라미터다. 둘 다 넘겨야 맞게 나온다.
        (2026-08 시점 실측: yearMonth=2026-09 단독 → 8월 데이터 반환)
        """
        ym = f"{year:04d}-{month:02d}"
        d = self.get("/schedule/calendar", upperCategoryId="kbaseball",
                     categoryIds="kbo", yearMonth=ym, date=f"{ym}-01")
        dates = d["result"].get("dates", [])
        # 그래도 다른 달이 오면 조용히 틀린 답을 내느니 비워서 알린다.
        return [x for x in dates if str(x.get("ymd", "")).startswith(ym)]

    def find_team_game(self, day: date, team: str = MY_TEAM) -> Optional[dict]:
        """해당 날짜의 team 경기 1건. 없으면 None."""
        for entry in self.calendar(day.year, day.month):
            if entry.get("ymd") != day.isoformat():
                continue
            for gi in entry.get("gameInfos") or []:
                if team in (gi.get("homeTeamCode"), gi.get("awayTeamCode")):
                    return gi
        return None

    def game(self, game_id: str) -> dict:
        return self.get(f"/schedule/games/{game_id}")["result"]["game"]

    def preview(self, game_id: str) -> Optional[dict]:
        res = self.get(f"/schedule/games/{game_id}/preview")["result"]
        return res.get("previewData")

    def record(self, game_id: str) -> Optional[dict]:
        """끝난 경기의 박스스코어. 경기 전이면 None."""
        res = self.get(f"/schedule/games/{game_id}/record")["result"]
        return res.get("recordData")

    def regular_season_start(self, year: int) -> date:
        """정규시즌 개막일. 시범경기를 걸러내기 위해 필요하다.

        네이버 일정에는 시범경기가 정규시즌과 같은 statusCode=RESULT 로 섞여
        온다. 구분은 경기 상세의 roundCode 에만 있다(kbo_e=시범, kbo_r=정규).
        일정만 보고 걸러낼 수 없어서, 3월 날짜를 앞에서부터 훑으며 처음으로
        kbo_r 이 나오는 날을 찾는다. 결과는 인스턴스에 캐시한다.

        이걸 안 걸르면 시범경기 성적이 상대전적·투수 기록·구장 팩터에 전부
        섞인다. 시범경기는 라인업이 달라 통계로 쓰면 안 된다.
        """
        cached = self._season_start.get(year)
        if cached:
            return cached

        fallback = date(year, 3, 1)
        for month in (3, 4):
            try:
                entries = self.calendar(year, month)
            except Exception:
                continue
            for entry in sorted(entries, key=lambda e: e.get("ymd") or ""):
                gis = entry.get("gameInfos") or []
                if not gis:
                    continue
                try:
                    g = self.game(gis[0]["gameId"])
                except Exception:
                    continue
                if str(g.get("roundCode", "")).endswith("_r"):
                    found = date.fromisoformat(entry["ymd"])
                    self._season_start[year] = found
                    return found
        self._season_start[year] = fallback
        return fallback

    @staticmethod
    def is_team_game(gi: dict) -> bool:
        """정규 구단끼리의 경기인가.

        올스타전은 roundCode 가 kbo_as 이고 팀 코드가 EA/WE(드림/나눔)로
        내려온다. 날짜로는 못 거르므로(시즌 중간) 코드로 막는다.
        """
        return (gi.get("homeTeamCode") in TEAM_NAMES
                and gi.get("awayTeamCode") in TEAM_NAMES)

    def is_regular(self, year: int, ymd: str) -> bool:
        """그 날짜가 정규시즌인가."""
        try:
            return date.fromisoformat(ymd) >= self.regular_season_start(year)
        except Exception:
            return True

    def team_games_before(self, year: int, team: str, before: date,
                          limit: int = 30) -> list:
        """team 의 완료된 경기를 최신순으로. (ymd, gameId, 홈경기여부) 튜플."""
        out = []
        month = before.month
        while month >= 3 and len(out) < limit * 2:
            try:
                entries = self.calendar(year, month)
            except Exception:
                break
            for entry in entries:
                ymd = entry.get("ymd")
                if not ymd or date.fromisoformat(ymd) >= before:
                    continue
                if not self.is_regular(year, ymd):
                    continue          # 시범경기는 통계에 넣지 않는다
                for gi in entry.get("gameInfos") or []:
                    if gi.get("statusCode") != "RESULT" or not self.is_team_game(gi):
                        continue
                    if team in (gi.get("homeTeamCode"), gi.get("awayTeamCode")):
                        out.append((ymd, gi["gameId"],
                                    gi.get("homeTeamCode") == team))
            month -= 1
        return sorted(out, reverse=True)[:limit]

    def team_stats(self, year: int) -> pd.DataFrame:
        d = self.get(f"/statistics/categories/kbo/seasons/{year}/teams")
        return pd.DataFrame(d["result"]["seasonTeamStats"]).set_index("teamId")

    def head_to_head_games(self, year: int, a: str, b: str,
                           before: date) -> pd.DataFrame:
        """올 시즌 a-b 맞대결 결과(득실점 포함).

        승패는 preview 의 seasonVsResult 가 바로 주지만 득실점은 주지 않는다.
        일정에서 맞대결만 골라 경기별로 스코어를 받아온다. 캐시 덕에 첫 실행
        이후에는 비용이 거의 없다.
        """
        rows = []
        for month in range(3, before.month + 1):
            try:
                entries = self.calendar(year, month)
            except Exception:
                continue
            for entry in entries:
                ymd = entry.get("ymd")
                if not ymd or date.fromisoformat(ymd) >= before:
                    continue
                if not self.is_regular(year, ymd):
                    continue          # 시범경기는 상대전적에서 뺀다
                for gi in entry.get("gameInfos") or []:
                    codes = {gi.get("homeTeamCode"), gi.get("awayTeamCode")}
                    if codes != {a, b} or gi.get("statusCode") != "RESULT":
                        continue
                    try:
                        g = self.game(gi["gameId"])
                    except Exception:
                        continue
                    rows.append({
                        "date": ymd,
                        "home": g["homeTeamCode"], "away": g["awayTeamCode"],
                        "home_score": g.get("homeTeamScore", 0),
                        "away_score": g.get("awayTeamScore", 0),
                    })
        return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 2. 도메인 모델
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Starter:
    name: str = "미정"
    pcode: str = ""
    season_era: float = 4.50
    season_whip: float = 1.40
    season_ip: float = 0.0
    season_games: int = 0
    season_hr: int = 0
    season_hit: int = 0
    season_kk: int = 0
    vs_era: Optional[float] = None
    vs_ip: float = 0.0
    vs_games: int = 0
    # 최근 등판 누적 — 경기별 박스스코어에서 역순으로 모은다.
    recent_ip: float = 0.0
    recent_er: int = 0
    recent_hit: int = 0
    recent_bb: int = 0
    recent_starts: int = 0
    recent_log: list = field(default_factory=list)
    pitches: list = field(default_factory=list)
    announced: bool = False

    @property
    def ip_per_game(self) -> float:
        return self.season_ip / self.season_games if self.season_games else 0.0

    @property
    def expected_ip(self) -> float:
        return min(SP_IP_MAX, max(SP_IP_MIN, self.ip_per_game or 5.0))

    @property
    def looks_like_reliever(self) -> bool:
        """등판당 이닝이 얕은 불펜/임시 선발인지. 예측 신뢰도에 직결된다."""
        return self.season_games > 0 and self.ip_per_game < 3.0

    @property
    def recent_era(self) -> Optional[float]:
        """최근 등판 ERA. 표본이 너무 얕으면 None."""
        if self.recent_ip < 3:
            return None
        return self.recent_er * 9.0 / self.recent_ip

    @property
    def recent_whip(self) -> Optional[float]:
        if self.recent_ip < 3:
            return None
        return (self.recent_hit + self.recent_bb) / self.recent_ip

    def per9(self, field: str) -> Optional[float]:
        """해당 누적 스탯의 9이닝당 값. 이닝이 너무 적으면 None."""
        if self.season_ip < 10:
            return None
        return getattr(self, field, 0) * 9.0 / self.season_ip


@dataclass
class TeamSide:
    code: str
    name: str
    is_home: bool
    rs_per_game: float = 4.6
    ra_per_game: float = 4.6
    team_era: float = 4.50
    bullpen_ra9: float = 4.80
    wins: int = 0
    losses: int = 0
    draws: int = 0
    rank: int = 0
    recent_rs: Optional[float] = None
    recent_ra: Optional[float] = None
    recent_form: str = ""
    recent_games: int = 0
    h2h_w: int = 0
    h2h_l: int = 0
    h2h_d: int = 0
    h2h_rs: Optional[float] = None
    h2h_ra: Optional[float] = None
    h2h_games: int = 0
    # COUNT_STATS 키별 경기당 산출/허용. 예: off_rate["hr"] = 팀 경기당 홈런
    off_rate: dict = field(default_factory=dict)
    def_rate: dict = field(default_factory=dict)
    starter: Starter = field(default_factory=Starter)


@dataclass
class GameContext:
    game_id: str
    game_date: str
    start_time: str
    stadium: str
    home: TeamSide
    away: TeamSide
    league_rpg: float = 4.60
    league_era: float = 4.40
    # COUNT_STATS 키별 리그 경기당 평균(팀 하나 기준). log5 계산의 기준값.
    league_counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    @property
    def lg(self) -> TeamSide:
        return self.home if self.home.code == MY_TEAM else self.away

    @property
    def opp(self) -> TeamSide:
        return self.away if self.home.code == MY_TEAM else self.home


@dataclass
class Prediction:
    # 기대값 모음. 키는 predict() 의 시뮬레이션 이름과 같다.
    #   lg_runs, opp_runs, total_runs, lg_sp_er, opp_sp_er,
    #   lg_hr, opp_hr, total_hr, total_hit, total_k ...
    exp: dict
    # {문항 key: {선택지 라벨: 확률}}
    probs: dict
    exp_margin: float
    p_win: float
    p_draw: float
    p_lose: float
    modal_score: tuple
    drivers: list
    confidence: str
    # 세 미션 동시 적중을 최대화하는 조합 (스타볼 지급 조건)
    combo: Optional[dict] = None


# ─────────────────────────────────────────────────────────────────────────────
# 3. 수집 → GameContext 조립
# ─────────────────────────────────────────────────────────────────────────────

def _starter_from_preview(node: Optional[dict]) -> Starter:
    if not node:
        return Starter()
    info = node.get("playerInfo") or {}
    cs = node.get("currentSeasonStats") or {}
    vs = node.get("currentSeasonStatsOnOpponents") or {}
    st = Starter(
        name=info.get("name") or "미정",
        pcode=str(info.get("pCode") or ""),
        season_era=fnum(cs.get("era"), 4.50),
        season_whip=fnum(cs.get("whip"), 1.40),
        season_ip=parse_kbo_innings(cs.get("inn")),
        season_games=int(fnum(cs.get("gameCount"), 0)),
        season_hr=int(fnum(cs.get("hr"), 0)),
        season_hit=int(fnum(cs.get("hit"), 0)),
        season_kk=int(fnum(cs.get("kk"), 0)),
        pitches=node.get("currentPitKindStats") or [],
        announced=bool(info.get("name")),
    )
    if vs.get("gameCount"):
        st.vs_era = fnum(vs.get("era"), None) if vs.get("era") else None
        st.vs_ip = parse_kbo_innings(vs.get("inn"))
        st.vs_games = int(fnum(vs.get("gameCount"), 0))
    return st


def _recent_from_preview(games: Optional[list], team_code: str):
    """preview 의 최근 경기에서 팀 관점 득/실점·전적 문자열·실제 경기 수를 뽑는다.

    경기 수를 함께 돌려주는 이유는 축소 가중치 때문이다. 5경기가 다 있는지
    2경기뿐인지에 따라 최근 페이스를 얼마나 믿을지가 달라진다.
    """
    if not games:
        return None, None, "", 0
    rs, ra, form = [], [], []
    for g in games[:5]:
        if g.get("hCode") == team_code:
            mine, theirs = g.get("hScore"), g.get("aScore")
        elif g.get("aCode") == team_code:
            mine, theirs = g.get("aScore"), g.get("hScore")
        else:
            continue
        if mine is None or theirs is None:
            continue
        rs.append(mine)
        ra.append(theirs)
        form.append("승" if mine > theirs else ("패" if mine < theirs else "무"))
    if not rs:
        return None, None, "", 0
    return sum(rs) / len(rs), sum(ra) / len(ra), "".join(form), len(rs)


def attach_pitcher_recent(client: NaverKBO, sp: Starter, team_code: str,
                          day: date, want: int = 5,
                          max_scan: int = 30) -> None:
    """투수의 최근 등판 기록을 채운다(Starter 를 제자리에서 수정).

    네이버 선수 기록 엔드포인트는 전부 404다. 대신 팀 경기를 최신순으로 훑으며
    각 경기 박스스코어의 pitchersBoxscore 에서 pcode 로 본인을 찾는다.
    선발은 5경기에 한 번 나오므로 5등판을 채우려면 팀 경기 25경기쯤 봐야 한다.
    캐시가 있으면 다음 날부터는 새 경기 1건만 추가로 받는다.

    실패해도 예외를 던지지 않는다. 최근 기록은 없으면 없는 대로 모델이 돈다.
    """
    if not sp.pcode:
        return
    side_key = None
    for ymd, gid, is_home in client.team_games_before(
            day.year, team_code, day, limit=max_scan):
        if sp.recent_starts >= want:
            break
        try:
            rd = client.record(gid)
        except Exception:
            continue
        pb = (rd or {}).get("pitchersBoxscore") or {}
        side_key = "home" if is_home else "away"
        for order, row in enumerate(pb.get(side_key) or []):
            if str(row.get("pcode")) != sp.pcode:
                continue
            ip = parse_kbo_innings(row.get("inn"))
            if ip <= 0:
                continue
            sp.recent_ip += ip
            sp.recent_er += int(fnum(row.get("er")))
            sp.recent_hit += int(fnum(row.get("hit")))
            sp.recent_bb += int(fnum(row.get("bb")))
            sp.recent_starts += 1
            sp.recent_log.append({
                "date": ymd, "ip": round(ip, 2),
                "er": int(fnum(row.get("er"))),
                "started": order == 0,
            })
            break


def compute_park_factors(client: NaverKBO, year: int,
                         progress: bool = True) -> dict:
    """끝난 경기 박스스코어에서 구장별 홈런 팩터를 실측 계산한다.

    방식은 표준적인 홈/원정 대비법이다. 팀 T에 대해
        PF = (T의 홈경기 경기당 총홈런) / (T의 원정경기 경기당 총홈런)
    같은 팀이 분자·분모 양쪽에 들어가므로 팀 타력 차이가 상쇄된다.
    구장을 공유하는 팀(잠실: LG·두산)은 경기 수로 가중 평균한다.
    마지막에 전체 평균이 1.0이 되도록 정규화한다.

    시즌 전 경기를 훑으므로 첫 실행은 몇 분 걸린다. 캐시가 남아 이후는 빠르다.
    """
    rows = []
    for month in range(3, 12):
        try:
            entries = client.calendar(year, month)
        except Exception:
            continue
        for entry in entries:
            if not client.is_regular(year, entry.get("ymd") or ""):
                continue              # 시범경기는 구장 팩터에서 뺀다
            for gi in entry.get("gameInfos") or []:
                if gi.get("statusCode") != "RESULT" or not client.is_team_game(gi):
                    continue
                rows.append((gi["gameId"], gi.get("homeTeamCode"),
                             gi.get("awayTeamCode")))

    if progress:
        print(f"완료된 경기 {len(rows)}건의 박스스코어를 읽습니다"
              f" (첫 실행은 수 분 소요)...", file=sys.stderr)

    games = []
    bullpen: dict = {}
    for i, (gid, home, away) in enumerate(rows, 1):
        try:
            rd = client.record(gid)
            box = (rd or {}).get("teamPitchingBoxscore") or {}
            if not box.get("home") and not box.get("away"):
                continue
            # 투수 피홈런의 합 = 그 경기에 나온 총 홈런
            total_hr = fnum(box.get("away", {}).get("hr")) + \
                fnum(box.get("home", {}).get("hr"))
            stadium = ((rd.get("gameInfo") or {}).get("stadium") or "").strip()
            if not stadium:
                continue
            games.append({"home": home, "away": away,
                          "stadium": stadium, "hr": total_hr})

            # 불펜 실점률: 팀 총실점에서 선발 몫을 덜어낸 나머지.
            # 팀 평균을 그대로 쓰면 강한 선발을 가진 팀의 불펜이 과대평가된다.
            pit = (rd or {}).get("pitchersBoxscore") or {}
            for side, team in (("home", home), ("away", away)):
                for order, row in enumerate(pit.get(side) or []):
                    if order == 0:          # 선발은 제외
                        continue
                    d = bullpen.setdefault(team, {"ip": 0.0, "r": 0.0})
                    d["ip"] += parse_kbo_innings(row.get("inn"))
                    d["r"] += fnum(row.get("r"))
        except Exception:
            continue
        if progress and i % 100 == 0:
            print(f"  {i}/{len(rows)}", file=sys.stderr)

    if not games:
        return {}, {}

    df = pd.DataFrame(games)
    league_hr = df["hr"].mean()

    # 팀별 홈/원정 홈런율 → 그 팀 홈구장의 팩터
    raw = []
    for team in sorted(set(df["home"]) | set(df["away"])):
        h = df[df["home"] == team]
        a = df[df["away"] == team]
        if len(h) < 10 or len(a) < 10 or a["hr"].mean() <= 0:
            continue
        park = h["stadium"].mode()
        if park.empty:
            continue
        raw.append({"team": team, "stadium": park.iloc[0],
                    "pf": h["hr"].mean() / a["hr"].mean(),
                    "games": len(h) + len(a)})
    if not raw:
        return {}

    rdf = pd.DataFrame(raw)
    # 구장 공유 시 경기 수 가중 평균
    agg = (rdf.assign(w=lambda d: d["pf"] * d["games"])
              .groupby("stadium")
              .apply(lambda d: d["w"].sum() / d["games"].sum(),
                     include_groups=False))
    factors = (agg / agg.mean()).round(3).to_dict()   # 평균 1.0 정규화

    if progress:
        print(f"\n구장별 홈런 팩터 ({len(games)}경기, 리그 경기당 "
              f"{league_hr:.2f}홈런 기준)", file=sys.stderr)
        for st, f in sorted(factors.items(), key=lambda kv: -kv[1]):
            at = df[df["stadium"] == st]
            print(f"  {st:<8} {f:>5.3f}   ({len(at)}경기, 경기당 "
                  f"{at['hr'].mean():.2f}홈런)", file=sys.stderr)

    bull = {t: round(d["r"] * 9.0 / d["ip"], 3)
            for t, d in bullpen.items() if d["ip"] >= 50}
    if progress and bull:
        print("\n불펜 RA9 (선발 제외)", file=sys.stderr)
        for t, v in sorted(bull.items(), key=lambda kv: kv[1]):
            print(f"  {TEAM_NAMES.get(t, t):<6} {v:>5.2f}"
                  f"  ({bullpen[t]['ip']:.0f}이닝)", file=sys.stderr)
    return factors, bull


class NoGame(Exception):
    """오늘 해당 경기가 없다. 스케줄 실행에서는 정상 종료(0)로 취급한다."""


class NotReady(Exception):
    """경기는 있는데 선발 예고 전이라 아직 예측할 수 없다. 재시도 대상(exit 75)."""


def build_context(client: NaverKBO, day: date,
                  snapshot: Optional[dict] = None,
                  home_only: bool = False,
                  with_pitcher_recent: bool = True) -> GameContext:
    """오늘 LG 경기의 모든 입력을 모아 GameContext 하나로 만든다."""
    warnings: list = []
    snap: dict = snapshot if snapshot is not None else {}

    gi = client.find_team_game(day, MY_TEAM)
    if not gi:
        raise NoGame(f"{day.isoformat()} 에 {MY_TEAM} 경기가 없습니다.")
    if home_only and gi.get("homeTeamCode") != MY_TEAM:
        raise NoGame(f"{day.isoformat()} 는 {MY_TEAM} 원정 경기입니다"
                     f"(--home-only).")
    game_id = gi["gameId"]

    game = client.game(game_id)
    preview = client.preview(game_id)
    tstats = client.team_stats(day.year)
    snap.update({"game": game, "preview": preview,
                 "team_stats": tstats.reset_index().to_dict("records")})

    if not preview:
        raise NotReady("프리뷰 데이터가 아직 없습니다(선발 예고 전일 수 있음).")

    home_code, away_code = game["homeTeamCode"], game["awayTeamCode"]

    # 리그 평균은 하드코딩하지 않고 당일 집계에서 직접 계산한다.
    # 리그 평균은 하드코딩하지 않고 당일 집계에서 계산한다. 다만 개막 직후에는
    # 집계가 전부 0이라 그대로 쓰면 뒤에서 0으로 나누게 된다(오즈비 계산).
    total_games = fnum(tstats["gameCount"].sum(), 1) or 1
    league_rpg = float(tstats["offenseRun"].sum()) / total_games
    total_inn = tstats["defenseInning"].map(parse_kbo_innings).sum()
    league_era = (float(tstats["defenseEr"].sum()) * 9.0 / total_inn
                  if total_inn else LEAGUE_ERA_FALLBACK)
    league_counts = {k: float(tstats[c["off"]].sum()) / total_games
                     for k, c in COUNT_STATS.items()}

    if league_rpg <= 0 or league_era <= 0:
        warnings.append("리그 집계가 비어 있습니다(개막 직후로 보임) — "
                        "리그 평균을 기본값으로 대체합니다")
        league_rpg = league_rpg if league_rpg > 0 else LEAGUE_RPG_FALLBACK
        league_era = league_era if league_era > 0 else LEAGUE_ERA_FALLBACK
    for k, c in COUNT_STATS.items():
        if league_counts.get(k, 0) <= 0:
            league_counts[k] = c["fallback"]

    def make_side(code: str, is_home: bool) -> TeamSide:
        s = TeamSide(code=code, name=TEAM_NAMES.get(code, code), is_home=is_home)
        if code in tstats.index:
            row = tstats.loc[code]
            g = fnum(row["gameCount"], 1) or 1
            s.rs_per_game = fnum(row["offenseRun"]) / g
            # 표시용. 실점력 모델은 '선발 ERA + 실측 불펜 RA9' 로
            # 분해되므로 팀 총실점은 입력이 아니다(중복 방지).
            s.ra_per_game = fnum(row["defenseR"]) / g
            s.team_era = fnum(row["defenseEra"], 4.50)
            s.wins = int(fnum(row["winGameCount"]))
            s.losses = int(fnum(row["loseGameCount"]))
            s.draws = int(fnum(row["drawnGameCount"]))
            s.rank = int(fnum(row["ranking"]))
            s.recent_form = str(row.get("lastFiveGames") or "")
            for k, c in COUNT_STATS.items():
                s.off_rate[k] = fnum(row[c["off"]]) / g
                s.def_rate[k] = fnum(row[c["dfn"]]) / g
        else:
            warnings.append(f"{code} 팀 시즌 스탯 없음 — 리그 평균으로 대체")
            for k in COUNT_STATS:
                s.off_rate[k] = s.def_rate[k] = league_counts[k]

        key = "homeTeamPreviousGames" if is_home else "awayTeamPreviousGames"
        s.recent_rs, s.recent_ra, form, s.recent_games = _recent_from_preview(
            preview.get(key), code)
        if form:
            s.recent_form = form

        s.starter = _starter_from_preview(
            preview.get("homeStarter" if is_home else "awayStarter"))
        # 불펜 실점률. 박스스코어에서 선발을 제외하고 실측한 값이 있으면 그걸
        # 쓴다. 팀 경기당 실점을 그대로 쓰면 선발 성적이 섞여, 강한 선발을
        # 가진 팀의 불펜을 실제보다 좋게 평가하게 된다.
        s.bullpen_ra9 = BULLPEN_RA9.get(code) or s.ra_per_game
        return s

    home = make_side(home_code, True)
    away = make_side(away_code, False)

    # 투수 최근 등판. 박스스코어를 역순으로 훑어야 해서 요청이 늘어난다.
    # 실패해도 예측은 계속된다 — 없으면 시즌·상대전적만으로 간다.
    if with_pitcher_recent:
        for side in (home, away):
            try:
                attach_pitcher_recent(client, side.starter, side.code, day)
            except Exception as e:
                warnings.append(
                    f"{side.name} 선발 최근 등판 수집 실패({type(e).__name__})")
        for side, foe in ((home, away), (away, home)):
            sp = side.starter
            if sp.announced and sp.recent_era is None:
                warnings.append(
                    f"{sp.name}의 최근 등판 기록을 찾지 못했습니다 — 시즌 성적만 사용")

    # 상대 전적 승패는 preview 가 바로 준다.
    svr = preview.get("seasonVsResult") or {}
    if svr:
        home.h2h_w = int(fnum(svr.get("hw")))
        home.h2h_l = int(fnum(svr.get("hl")))
        home.h2h_d = int(fnum(svr.get("hd")))
        away.h2h_w = int(fnum(svr.get("aw")))
        away.h2h_l = int(fnum(svr.get("al")))
        away.h2h_d = int(fnum(svr.get("ad")))

    # 상대 전적 득실점은 경기별 조회가 필요하다. 실패해도 예측은 계속된다.
    try:
        h2h = client.head_to_head_games(day.year, home_code, away_code, day)
        snap["h2h"] = h2h.to_dict("records")
        if not h2h.empty:
            for s in (home, away):
                mine = h2h.apply(lambda r, c=s.code:
                                 r["home_score"] if r["home"] == c else r["away_score"],
                                 axis=1)
                foe = h2h.apply(lambda r, c=s.code:
                                r["away_score"] if r["home"] == c else r["home_score"],
                                axis=1)
                s.h2h_rs, s.h2h_ra = float(mine.mean()), float(foe.mean())
                s.h2h_games = len(h2h)
                # 승패도 같은 표본에서 다시 센다. preview 의 seasonVsResult 는
                # 3월 경기를 빼고 세는 등 집계 범위가 달라서, 그대로 두면
                # 화면의 '상대전적'과 득실점 평균이 서로 다른 경기 묶음이 된다.
                s.h2h_w = int((mine > foe).sum())
                s.h2h_l = int((mine < foe).sum())
                s.h2h_d = int((mine == foe).sum())
    except Exception as e:
        warnings.append(
            f"상대전적 득실점 수집 실패({type(e).__name__}) — 시즌 평균만 사용")

    for s, foe in ((home, away), (away, home)):
        sp = s.starter
        if not sp.announced:
            warnings.append(f"{s.name} 선발 미예고 — 리그 평균 투수로 가정")
        elif sp.looks_like_reliever:
            warnings.append(
                f"{s.name} 선발 {sp.name}은 등판당 {sp.ip_per_game:.1f}이닝"
                f"({sp.season_games}경기)로 불펜/임시 선발 패턴 — 소화 이닝 불확실")
        if sp.vs_games and sp.vs_ip < 10:
            warnings.append(
                f"{sp.name}의 {foe.name}전 표본은 {sp.vs_ip:.1f}이닝"
                f"({sp.vs_games}경기)뿐 — 상대 ERA를 크게 축소 적용")

    snap["pitcher_recent"] = {
        key: {"ip": s.starter.recent_ip, "er": s.starter.recent_er,
              "hit": s.starter.recent_hit, "bb": s.starter.recent_bb,
              "starts": s.starter.recent_starts, "log": s.starter.recent_log}
        for key, s in (("home", home), ("away", away))
    }

    return GameContext(
        game_id=game_id,
        game_date=game.get("gameDate", day.isoformat()),
        start_time=(game.get("gameDateTime") or "")[11:16],
        stadium=game.get("stadium", ""),
        home=home, away=away,
        league_rpg=league_rpg, league_era=league_era,
        league_counts=league_counts,
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. 예측 모델
# ─────────────────────────────────────────────────────────────────────────────

def _offense_rating(s: TeamSide):
    """팀 경기당 득점력. 시즌/상대전적/최근을 가중 평균한다."""
    parts = [(W_OFF_SEASON, s.rs_per_game, "시즌")]
    if s.h2h_rs is not None:
        vs = shrink(s.h2h_rs, s.rs_per_game, s.h2h_games, K_SHRINK_GAMES)
        parts.append((W_OFF_VS, vs, "상대전적"))
    if s.recent_rs is not None and s.recent_games > 0:
        # 표본 수를 하드코딩하면(이전에는 항상 5) 2경기뿐일 때도 5경기처럼
        # 취급해 최근 페이스를 과대반영한다.
        rec = shrink(s.recent_rs, s.rs_per_game, s.recent_games, K_SHRINK_RECENT)
        parts.append((W_OFF_RECENT, rec, f"최근{s.recent_games}"))
    wsum = sum(w for w, _, _ in parts)
    if wsum <= 0:                      # 가중치를 전부 0으로 둔 경우
        return s.rs_per_game, parts
    return sum(w * v for w, v, _ in parts) / wsum, parts


def _starter_ra9(sp: Starter, league_era: float, foe_name: str):
    """선발의 9이닝당 예상 실점(비자책 포함)과 근거 문자열."""
    if not sp.announced:
        return league_era * ER_TO_R, f"선발 미예고 → 리그 평균 {league_era:.2f} 가정"

    season = shrink(sp.season_era, league_era, sp.season_ip, 20.0)
    note = f"시즌 ERA {sp.season_era:.2f}({sp.season_ip:.1f}이닝)"

    # 시즌 / 상대팀 한정 / 최근 등판 — 있는 것만 가중 평균한다.
    parts = [(W_SP_SEASON, season)]

    if sp.vs_era is not None and sp.vs_ip > 0:
        vs = shrink(sp.vs_era, league_era, sp.vs_ip, K_SHRINK_IP)
        parts.append((W_SP_VS, vs))
        note += (f" + {foe_name}전 {sp.vs_era:.2f}({sp.vs_ip:.1f}이닝)"
                 f" → 축소 후 {vs:.2f}")
    else:
        note += f" / {foe_name}전 등판 이력 없음"

    if sp.recent_era is not None:
        rec = shrink(sp.recent_era, season, sp.recent_ip, K_SHRINK_RECENT_IP)
        parts.append((W_SP_RECENT, rec))
        note += (f" + 최근{sp.recent_starts}등판 {sp.recent_era:.2f}"
                 f"({sp.recent_ip:.1f}이닝, WHIP {sp.recent_whip:.2f})"
                 f" → 축소 후 {rec:.2f}")

    # 가중치를 전부 0으로 튜닝하면 여기서 0으로 나눈다. 그 경우 선발이
    # 아무 정보도 주지 않는다는 뜻이므로 리그 평균으로 되돌린다.
    wsum = sum(w for w, _ in parts)
    era = sum(w * v for w, v in parts) / wsum if wsum > 0 else league_era

    return era * ER_TO_R, note


def _blend_base(probs: dict, base: Optional[dict]) -> dict:
    """모델 확률을 그 문항의 리그 기저 분포 쪽으로 당긴다.

    모델이 정보를 못 주는 문항에서는 기저 분포를 그대로 찍는 게 최선이다.
    BASE_RATE_BLEND 가 그 정도를 정하고, 백테스트로 적합한다.
    """
    if not base or BASE_RATE_BLEND >= 1.0:
        return probs
    a = BASE_RATE_BLEND
    out = {k: a * v + (1 - a) * base.get(k, 0.0) for k, v in probs.items()}
    tot = sum(out.values())
    return {k: v / tot for k, v in out.items()} if tot > 0 else probs


def _calibrate(probs: dict) -> dict:
    """과신을 눌러 실제 발생률에 맞춘다. 백테스트로 적합한 계수를 쓴다.

    시뮬레이션은 입력 추정치를 확실한 값으로 취급하기 때문에, 추정 자체의
    불확실성이 반영되지 않아 확률이 과하게 뾰족해진다.
    """
    if not probs or PROB_CALIBRATION >= 1.0:
        return probs
    u = 1.0 / len(probs)
    return {k: u + (v - u) * PROB_CALIBRATION for k, v in probs.items()}


def _combo_lift(keys: list, base_combo: Optional[dict]) -> dict:
    """실측 조합 분포에서 '독립가정 대비 몇 배인지'(lift)를 뽑는다.

    lift = P(조합) / (P(문항1) × P(문항2) × P(문항3))

    1.0 이면 세 문항이 서로 무관하다는 뜻이고, 1보다 크면 함께 잘 나온다는
    뜻이다. 주변확률은 모델에서, 상관은 여기서 가져와 곱한다.

    표본이 얕은 조합에서 lift 가 튀지 않게 0.4~2.5 로 묶는다.
    """
    if not base_combo:
        return {}
    marg: dict = {}
    for cand, p in base_combo.items():
        for i, v in enumerate(cand):
            marg.setdefault(i, {})
            marg[i][v] = marg[i].get(v, 0.0) + p
    out = {}
    for cand, p in base_combo.items():
        ind = 1.0
        for i, v in enumerate(cand):
            ind *= marg.get(i, {}).get(v, 0.0)
        if ind > 1e-9:
            out[cand] = min(2.5, max(0.4, p / ind))
    return out


def _learned_probs(ctx: GameContext) -> dict:
    """학습 모델이 있는 문항의 확률. 없거나 못 만들면 그 문항은 빠진다.

    승패와 홈런에만 붙인다. 득실 차는 실제 기록을 어느 축으로 나눠 봐도
    1점이 최빈이라 고정이 최적이고, 학습을 붙이면 나빠진다(실측).
    """
    try:
        import outcome_infer as OI
    except ImportError:
        return {}
    try:
        games = OI.T.load(f"gamelog_{str(ctx.game_date)[:4]}.json")
    except (FileNotFoundError, ValueError):
        return {}

    lg, opp = ctx.lg, ctx.opp
    pcode = lambda side: getattr(getattr(side, "starter", None), "pcode", None)
    kw = dict(tm=lg.code, op=opp.code,
              is_home=getattr(ctx.home, "code", "") == lg.code,
              stadium=ctx.stadium, my_sp=pcode(lg), op_sp=pcode(opp),
              date=str(ctx.game_date))

    out = {}
    m = OI.load_model()
    if m:
        r = OI.predict_outcome(m, games, **kw)
        if r:
            out["outcome"] = {k: r[k] for k in ("승", "무", "패") if k in r}
            out["_outcome_meta"] = {k: r.get(k) for k in
                                    ("confidence", "tierAccuracy", "band",
                                     "bandAccuracy", "bandShare")}
    hm = OI.load_hr_model()
    if hm:
        r = OI.predict_hr(hm, games, **kw)
        if r:
            out["lg_hr"] = r
    return {k: v for k, v in out.items() if not k.startswith("_")} | (
        {"_meta": out.get("_outcome_meta")} if out.get("_outcome_meta") else {})


def predict(ctx: GameContext, n_sim: int = N_SIM, seed: int = SEED,
            base_rates: Optional[dict] = None,
            base_combo: Optional[dict] = None,
            use_learned: bool = True) -> Prediction:
    """기대 득점을 산출한 뒤 몬테카를로로 각 문항의 확률을 뽑는다.

    점수차·실점 구간은 점추정을 눈대중으로 나누면 틀린다. 음이항 분포로
    시뮬레이션해서 구간 확률을 직접 계산한다.
    """
    import numpy as np
    rng = np.random.default_rng(seed)

    # 호출부가 안 주면 파일에서 읽은 리그 기저를 쓴다(백테스트는 시점별로 준다).
    if base_rates is None:
        base_rates = BASE_RATES or None
    if base_combo is None:
        base_combo = BASE_COMBO or None

    lg, opp = ctx.lg, ctx.opp
    drivers: list = []

    def expected_runs(off: TeamSide, dfn: TeamSide):
        off_rate, parts = _offense_rating(off)
        sp_ra9, sp_note = _starter_ra9(dfn.starter, ctx.league_era, off.name)

        ip = dfn.starter.expected_ip
        share = ip / 9.0
        def_rate = share * sp_ra9 + (1 - share) * dfn.bullpen_ra9

        # 오즈비(log5) 방식: 리그 평균 대비 공격력 × 상대 실점력.
        # 단순 산술평균보다 극단값에서 안정적이다.
        exp = ctx.league_rpg * (off_rate / ctx.league_rpg) * (def_rate / ctx.league_rpg)
        # 홈 이점을 홈팀에만 더하면 양 팀 합계 득점이 통째로 부풀려진다.
        # 절반씩 나눠 홈에 더하고 원정에서 빼면, 승패·득실차 확률은 그대로면서
        # 합계 득점이 중립이 된다.
        exp += HOME_FIELD_RUNS / 2 if off.is_home else -HOME_FIELD_RUNS / 2

        breakdown = " / ".join(f"{n} {v:.2f}" for _, v, n in parts)
        drivers.append(
            f"{off.name} 득점력 {off_rate:.2f} ({breakdown})  vs  "
            f"{dfn.name} 실점력 {def_rate:.2f} "
            f"[선발 {ip:.1f}이닝 RA9 {sp_ra9:.2f} + 불펜 {dfn.bullpen_ra9:.2f}]"
            f"  ⇒ 기대 {max(0.8, exp):.2f}점")
        drivers.append(f"  └ {dfn.starter.name}: {sp_note}")
        return max(0.8, exp), sp_ra9, ip

    e_lg, opp_sp_ra9, opp_sp_ip = expected_runs(lg, opp)
    e_opp, lg_sp_ra9, lg_sp_ip = expected_runs(opp, lg)

    def nb(mu: float, phi: float, size: int):
        mu = max(0.05, mu)
        return rng.negative_binomial(phi, phi / (phi + mu), size)

    r_lg = nb(e_lg, NB_PHI_TEAM, n_sim)
    r_opp = nb(e_opp, NB_PHI_TEAM, n_sim)

    tie = r_lg == r_opp
    n_tie = int(tie.sum())
    # 9회 동점 → 일부는 연장에서 갈리고 일부는 무승부로 끝난다.
    tie_draw = rng.random(n_tie) < DRAW_SHARE
    tie_lgwin = (~tie_draw) & (rng.random(n_tie) < 0.5)

    win = int((r_lg > r_opp).sum() + tie_lgwin.sum())
    draw = int(tie_draw.sum())
    lose = n_sim - win - draw

    e_lg_sp_er = lg_sp_ra9 * lg_sp_ip / 9.0 / ER_TO_R
    e_opp_sp_er = opp_sp_ra9 * opp_sp_ip / 9.0 / ER_TO_R

    # ── 카운팅 스탯(홈런/안타/삼진) ───────────────────────────────────────
    # 득점과 같은 오즈비 방식. 팀 타선 산출 × 상대 배터리 허용 / 리그 평균.
    def expected_count(stat: str, off: TeamSide, dfn: TeamSide) -> float:
        base = ctx.league_counts.get(stat)
        if not base:
            return 0.0
        off_r = off.off_rate.get(stat, base)
        dfn_r = dfn.def_rate.get(stat, base)

        # 선발 투수 성향을 이닝 지분만큼 반영한다. 표본이 얕으면 팀 평균
        # 쪽으로 축소한다(스탯마다 안정화 속도가 달라 k_ip 도 다르다).
        cfg = COUNT_STATS[stat]
        rate9 = dfn.starter.per9(cfg["sp"])
        if rate9 is not None:
            rate9 = shrink(rate9, dfn_r, dfn.starter.season_ip, cfg["k_ip"])
            share = dfn.starter.expected_ip / 9.0
            dfn_r = share * rate9 + (1 - share) * dfn_r

        exp = base * (off_r / base) * (dfn_r / base)
        if stat == "hr":
            exp *= park_hr_factor(ctx.stadium)
        return max(0.02, exp)

    sims: dict = {
        "lg_runs": r_lg,
        "opp_runs": r_opp,
        "total_runs": r_lg + r_opp,
        # 무승부면 0. 실제 폼의 '득실 차' 드롭다운에 0 항목이 없다면 어떤
        # 선택지와도 안 맞으므로, 조합 최적화가 알아서 무승부를 피하게 된다.
        "margin_abs": abs(r_lg - r_opp),
        "lg_sp_er": nb(e_lg_sp_er, NB_PHI_SP, n_sim),
        "opp_sp_er": nb(e_opp_sp_er, NB_PHI_SP, n_sim),
    }
    exp: dict = {
        "lg_runs": e_lg, "opp_runs": e_opp, "total_runs": e_lg + e_opp,
        "lg_sp_er": e_lg_sp_er, "opp_sp_er": e_opp_sp_er,
    }

    def nb_coupled(mu: float, phi: float, beta: float,
                   runs, mu_runs: float):
        """그 팀의 득점 시뮬레이션에 묶어서 뽑는다.

        많이 뽑은 날 홈런도 같이 나온다(실측 상관 +0.47). 독립으로 뽑으면
        '승 + 4점차 + 2홈런' 같은 조합의 확률을 크게 틀린다. 스타볼은
        조합으로 판정되므로 이 상관이 결과를 좌우한다.
        """
        mu = max(0.02, mu)
        if beta <= 0:
            return nb(mu, phi, n_sim)
        scale = np.clip(1.0 + beta * (runs / max(mu_runs, 0.1) - 1.0), 0.05, None)
        m = np.maximum(0.02, mu * scale)
        return rng.negative_binomial(phi, phi / (phi + m))

    for stat, cfg in COUNT_STATS.items():
        mu_lg = expected_count(stat, lg, opp)
        mu_opp = expected_count(stat, opp, lg)
        s_lg = nb_coupled(mu_lg, cfg["phi"], cfg["beta"], r_lg, e_lg)
        s_opp = nb_coupled(mu_opp, cfg["phi"], cfg["beta"], r_opp, e_opp)
        sims[f"lg_{stat}"], sims[f"opp_{stat}"] = s_lg, s_opp
        sims[f"total_{stat}"] = s_lg + s_opp
        exp[f"lg_{stat}"], exp[f"opp_{stat}"] = mu_lg, mu_opp
        exp[f"total_{stat}"] = mu_lg + mu_opp
        note = ""
        if stat == "hr":
            raw = PARK_HR_FACTOR.get(ctx.stadium)
            if raw is not None:
                note = (f", {ctx.stadium} 구장 팩터 {park_hr_factor(ctx.stadium):.2f}"
                        f" [실측 {raw:.2f}를 1.0쪽으로 축소]")
        drivers.append(
            f"{cfg['label']} 기대 — {lg.name} {mu_lg:.2f} / {opp.name} {mu_opp:.2f}"
            f"  (합계 {mu_lg + mu_opp:.2f}, 리그 평균 {ctx.league_counts[stat]:.2f}"
            f"{note})")

    p_win, p_draw, p_lose = win / n_sim, draw / n_sim, lose / n_sim

    # ── 문항 정의 → 확률 ─────────────────────────────────────────────────
    probs: dict = {}
    for q in STARBALL_QUESTIONS:
        key, src = q["key"], q.get("source")
        if q.get("categorical"):
            pool = {"win": p_win, "draw": p_draw, "lose": p_lose}
            probs[key] = _blend_base(
                _calibrate({lbl: pool.get(code, 0.0)
                            for lbl, code in q["categorical"]}),
                (base_rates or {}).get(key))
            continue

        x = sims.get(src)
        if x is None or len(x) == 0:
            probs[key] = {lbl: 0.0 for lbl, _, _ in q["buckets"]}
            continue
        out = {}
        for lbl, lo, hi in q["buckets"]:
            m = np.ones(len(x), dtype=bool)
            if lo is not None:
                m &= x >= lo
            if hi is not None:
                m &= x <= hi
            out[lbl] = float(m.mean())
        probs[key] = _blend_base(_calibrate(out),
                                 (base_rates or {}).get(key))

    # ── 조합 최적화 ──────────────────────────────────────────────────────
    # 스타볼은 "매치데이 미션을 모두 달성"해야 지급된다. 문항별로 1위를
    # 따로 고르면 각각은 최선이어도 조합으로는 최선이 아닐 수 있다.
    # 득실 차와 홈런 수는 서로 상관되기 때문이다(대승이면 홈런도 잘 나온다).
    # 그래서 시뮬레이션마다 라벨을 붙이고, 세 개가 동시에 맞는 비율을
    # 조합별로 세어 최대인 조합을 고른다.
    outcome_sim = np.where(r_lg > r_opp, "승", "패").astype(object)
    if n_tie:
        tie_idx = np.flatnonzero(tie)
        outcome_sim[tie_idx[tie_draw]] = "무"
        outcome_sim[tie_idx[tie_lgwin]] = "승"
        outcome_sim[tie_idx[~tie_draw & ~tie_lgwin]] = "패"

    sim_labels: dict = {}
    for q in STARBALL_QUESTIONS:
        if q.get("categorical"):
            code_to_label = {c: l for l, c in q["categorical"]}
            sim_labels[q["key"]] = np.array(
                [code_to_label.get({"승": "win", "무": "draw", "패": "lose"}[v], v)
                 for v in outcome_sim], dtype=object)
            continue
        x = sims.get(q["source"])
        if x is None or len(x) != n_sim:
            continue
        arr = np.full(n_sim, None, dtype=object)
        for lbl, lo, hi in q["buckets"]:
            m = np.ones(n_sim, dtype=bool)
            if lo is not None:
                m &= x >= lo
            if hi is not None:
                m &= x <= hi
            arr[m & (arr == None)] = lbl        # noqa: E711 — 객체배열 비교
        sim_labels[q["key"]] = arr

    # ── 학습 모델 반영 ──────────────────────────────────────────────────
    # 반드시 여기서 갈아끼운다. predict() 밖에서 바꾸면 미션 확률만 학습
    # 값이 되고 결합 확률은 옛 시뮬레이션 값이 남아, 화면의 두 숫자가
    # 서로 다른 모델을 말하게 된다(실제로 그런 상태였다).
    #
    # 시뮬레이션 표본에 가중치를 줘서 주변확률만 학습 값에 맞춘다.
    # 이렇게 하면 문항 사이의 상관(크게 이기면 홈런도 많다 같은)은
    # 시뮬레이션 것을 그대로 쓰면서, 각 문항의 확률은 학습 값이 된다.
    sim_w = np.ones(n_sim)
    learned_note = []
    if use_learned:
        _lp = _learned_probs(ctx)
        learned_meta = _lp.pop("_meta", None)
        for key, fresh in _lp.items():
            old = probs.get(key) or {}
            arr = sim_labels.get(key)
            if arr is None or set(fresh) != set(old):
                continue
            for lbl, p_new in fresh.items():
                p_old = old.get(lbl, 0.0)
                if p_old > 1e-9:
                    sim_w[arr == lbl] *= p_new / p_old
            before = max(old, key=old.get) if old else None
            probs[key] = dict(fresh)
            after = max(fresh, key=fresh.get)
            if before and before != after:
                learned_note.append(f"{key}: {before} → {after}")
        if sim_w.sum() <= 0:
            sim_w = np.ones(n_sim)
        if learned_meta and learned_meta.get("band"):
            drivers.append(
                f"오늘은 확신도 {learned_meta['band']:.0f}% 구간이다 — 과거 이 "
                f"구간의 실제 적중률 {learned_meta['bandAccuracy']}% "
                f"(전체 경기의 {learned_meta['bandShare'] * 100:.0f}%만 해당)")
        if learned_note:
            drivers.append("학습 모델이 바꾼 값: " + ", ".join(learned_note))
        p_win = probs.get("outcome", {}).get("승", p_win)
        p_draw = probs.get("outcome", {}).get("무", p_draw)
        p_lose = probs.get("outcome", {}).get("패", p_lose)

    combo = None
    keys = [q["key"] for q in STARBALL_QUESTIONS if q["key"] in sim_labels]
    if keys:
        import itertools
        options = [sorted(set(sim_labels[k]) - {None}) for k in keys]
        # 조합 확률도 기저 분포와 섞는다. 모델이 근거 없이 기저에서 벗어나면
        # 손해라, 섞는 비율(BASE_RATE_BLEND)을 백테스트로 정한다.
        # 문항별 확률만 섞으면 조합 선택에는 아무 영향이 없다 — 조합은 원시
        # 시뮬레이션에서 뽑히기 때문이다. 그래서 여기서 다시 섞는다.
        best_p, best_c = -1.0, None
        model_p: dict = {}

        # 조합 확률을 실측 상관으로 계산한다.
        #
        # 예전에는 시뮬레이션에서 직접 셌는데, 그 상관 구조가 실제와 반대였다.
        # 실측 4,090 팀-경기에서 '이겼을 때 0홈런' 은 31.1%, '졌을 때' 는
        # 53.2% 다 — 홈런을 못 치면 지니까 당연하다. 그런데 시뮬레이션은
        # 이길 때 0홈런이 더 흔하다고 봤다. 그래서 학습 모델이 '패' 라고
        # 해도 조합은 '승' 을 고르는 일이 생겼다.
        #
        # 이제 독립가정에서 출발해 실측 조합 분포의 '들뜸(lift)' 만 얹는다.
        # 주변확률은 모델(학습 포함) 값, 상관은 실제 기록 — 각자 잘하는 쪽을
        # 쓴다. 실측에 없는 조합은 lift 1.0(독립)으로 둔다.
        lift = _combo_lift(keys, base_combo)
        for cand in itertools.product(*options):
            ind = 1.0
            for k, v in zip(keys, cand):
                ind *= probs.get(k, {}).get(v, 0.0)
            model_p[cand] = ind * lift.get(cand, 1.0)
        tot = sum(model_p.values())
        if tot > 0:
            model_p = {k: v / tot for k, v in model_p.items()}
        else:
            # 실측이 없으면 예전처럼 시뮬레이션에서 센다.
            for cand in itertools.product(*options):
                m = np.ones(n_sim, dtype=bool)
                for k, v in zip(keys, cand):
                    m &= (sim_labels[k] == v)
                model_p[cand] = float(sim_w[m].sum() / sim_w.sum())

        a = BASE_RATE_BLEND

        def believed(cand: tuple) -> float:
            """이 조합이 맞을 것이라고 우리가 실제로 믿는 확률.

            선택도 보고도 이 값으로 한다. 예전에는 선택은 혼합값으로 하고
            보고는 모델값으로 해서, 고른 조합보다 버린 조합의 표시 확률이 더
            높게 나오는 경우가 있었다(화면에 그대로 나갔다).
            """
            pm = model_p.get(cand, 0.0)
            if a >= 1.0 or not base_combo:
                return pm
            return a * pm + (1 - a) * base_combo.get(cand, 0.0)

        for cand in model_p:
            p = believed(cand)
            if p > best_p:
                best_p, best_c = p, cand
        # 문항별 1위를 따로 고른 조합의 동시 적중 확률(비교용)
        greedy = tuple(max(probs[k], key=probs[k].get) for k in keys)
        combo = {
            "keys": keys,
            "best": dict(zip(keys, best_c)),
            "best_prob": best_p,                       # 믿는 확률(혼합 후)
            "best_model_prob": model_p.get(best_c, 0.0),   # 모델만의 값(진단용)
            "greedy": dict(zip(keys, greedy)),
            "greedy_prob": believed(greedy),           # 같은 잣대로 비교해야 한다
        }

    modal = (pd.Series(list(zip(r_lg.tolist(), r_opp.tolist())))
             .value_counts().index[0])

    edge = max(p_win, p_lose)
    confidence = "높음" if edge >= 0.60 else ("보통" if edge >= 0.545 else "낮음")
    if any("미예고" in w for w in ctx.warnings):
        confidence = "낮음"
    elif any("불펜/임시 선발" in w for w in ctx.warnings) and confidence == "높음":
        # 선발 소화 이닝이 불확실하면 승률 숫자만 믿을 수 없다.
        confidence = "보통"

    return Prediction(
        exp=exp, probs=probs, exp_margin=e_lg - e_opp,
        p_win=p_win, p_draw=p_draw, p_lose=p_lose,
        modal_score=(int(modal[0]), int(modal[1])),
        drivers=drivers, confidence=confidence, combo=combo,
    )


def use_learned_models(ctx: GameContext, pred: Prediction) -> None:
    """학습 모델이 있는 문항을 그 값으로 갈아끼운다.

    승패와 홈런에만 붙인다. 득실 차는 실제 기록을 어느 축으로 나눠 봐도
    1점이 최빈이라(기대 총득점·전력 격차 모두) 고정이 최적이고, 학습을
    붙이면 오히려 나빠진다.
    """
    use_learned_outcome(ctx, pred)
    use_learned_hr(ctx, pred)


def _live_context(ctx: GameContext):
    """운영에서 특징을 만들 재료. 없으면 None."""
    try:
        import outcome_infer as OI
    except ImportError:
        return None
    try:
        games = OI.T.load(f"gamelog_{str(ctx.game_date)[:4]}.json")
    except (FileNotFoundError, ValueError):
        return None
    lg, opp = ctx.lg, ctx.opp
    pcode = lambda side: getattr(getattr(side, "starter", None), "pcode", None)
    return (OI, games, dict(tm=lg.code, op=opp.code,
                            is_home=getattr(ctx.home, "code", "") == lg.code,
                            stadium=ctx.stadium, my_sp=pcode(lg),
                            op_sp=pcode(opp), date=str(ctx.game_date)))


def use_learned_hr(ctx: GameContext, pred: Prediction) -> bool:
    """홈런 확률을 학습 모델 값으로 갈아끼운다.

    이걸 넣기 전에는 96경기 전부 0개를 골랐다. 실제 기록에서는 기대 홈런이
    0.9 를 넘는 구간(전체의 3분의 1)에서 1개가 최빈이라, 매치업에 따라
    답이 달라져야 한다.
    """
    got = _live_context(ctx)
    if not got:
        return False
    OI, games, kw = got
    model = OI.load_hr_model()
    if not model:
        return False
    out = OI.predict_hr(model, games, **kw)
    if not out:
        return False

    cur = pred.probs.get("lg_hr") or {}
    fresh = {k: out[k] for k in cur if k in out}
    # 앱 선택지와 모델 구간이 다르면 손대지 않는다. 억지로 맞추면
    # '5개 이상' 확률이 '5개' 자리에 들어가 조용히 틀린다.
    if set(fresh) != set(cur) or not fresh:
        print(f"홈런 선택지가 모델과 다릅니다({sorted(cur)} vs {sorted(out)}) "
              f"— 옛 모델 값을 씁니다", file=sys.stderr)
        return False

    before = max(cur, key=cur.get)
    pred.probs["lg_hr"] = fresh
    after = max(fresh, key=fresh.get)
    pred.drivers.append(
        f"홈런은 학습 모델 값이다 (실측 {model['validation']['model']}% vs "
        f"항상 {model['validation']['fixed_label']} {model['validation']['fixed']}%)")
    if before != after:
        print(f"홈런 추천이 바뀜: {before} → {after} "
              f"({cur[before] * 100:.1f}% → {fresh[after] * 100:.1f}%)",
              file=sys.stderr)
    return True


def use_learned_outcome(ctx: GameContext, pred: Prediction) -> bool:
    """승패 확률을 학습 모델 값으로 갈아끼운다. 성공하면 True.

    세 문항 중 데이터가 통하는 건 승패뿐이다 — 득실 차·홈런은 어떤 조건에서도
    최빈값이 바뀌지 않는다. 단서: 이 '96경기 전부 1점/0개' 라는 관찰은 이
    모델의 출력이지 실제 기록이 아니다. 홈런은 원본 기록으로 다시 재보니
    3분의 1 구간에서 1개가 최빈이어서 학습 모델을 붙였다(use_learned_hr).
    득실 차는 원본으로도 모든 구간에서 1점이 최빈이라 그대로 둔다.

    계수 파일이 없거나 특징을 못 만들면 아무것도 하지 않는다. 옛 모델 값이
    그대로 남는 쪽이, 조용히 틀린 값을 내는 것보다 낫다.
    """
    try:
        import outcome_infer as OI
    except ImportError:
        return False
    model = OI.load_model()
    if not model:
        return False
    try:
        games = OI.T.load(f"gamelog_{str(ctx.game_date)[:4]}.json")
    except (FileNotFoundError, ValueError):
        print("경기 로그가 없어 승패 학습 모델을 건너뜁니다", file=sys.stderr)
        return False

    lg, opp = ctx.lg, ctx.opp
    pcode = lambda side: getattr(getattr(side, "starter", None), "pcode", None)
    out = OI.predict_outcome(model, games, lg.code, opp.code,
                             getattr(ctx.home, "code", "") == lg.code,
                             ctx.stadium, pcode(lg), pcode(opp),
                             str(ctx.game_date))
    if not out:
        print("승패 특징을 만들 수 없어 옛 모델 값을 씁니다", file=sys.stderr)
        return False

    cur = pred.probs.get("outcome") or {}
    fresh = {k: out[k] for k in cur if k in out}
    if len(fresh) != len(cur) or not fresh:
        return False

    before = max(cur, key=cur.get) if cur else None
    pred.probs["outcome"] = fresh
    pred.p_win, pred.p_draw, pred.p_lose = (fresh.get("승", 0.0),
                                            fresh.get("무", 0.0),
                                            fresh.get("패", 0.0))
    after = max(fresh, key=fresh.get)
    tier = out.get("confidence")
    acc = out.get("tierAccuracy")
    if out.get("band"):
        pred.drivers.append(
            f"오늘은 확신도 {out['band']:.0f}% 구간이다 — 과거 이 구간의 "
            f"실제 적중률 {out['bandAccuracy']}% (전체 경기의 "
            f"{out['bandShare'] * 100:.0f}%만 해당)")
    pred.drivers.append(
        f"승패는 학습 모델 값이다 (2024~2026 {model['trained_on']['rows']}표본, "
        f"실측 {model['validation']['model']}%). 확신도 {tier}"
        + (f", 이 등급의 과거 적중률 {acc}%" if acc else ""))
    if before and before != after:
        print(f"승패 추천이 바뀜: {before} → {after} "
              f"({cur[before] * 100:.1f}% → {fresh[after] * 100:.1f}%)",
              file=sys.stderr)
    return True


def to_starball_choices(pred: Prediction) -> list:
    """문항 정의 + 예측 확률 → 추천 선택지.

    edge(1위와 2위의 확률 격차)를 같이 낸다. 격차가 몇 %p 안 되면 모델이
    사실상 정보를 못 주는 문항이라는 뜻이라 그대로 따를 이유가 없다.
    """
    out = []
    for q in STARBALL_QUESTIONS:
        probs = pred.probs.get(q["key"], {})
        if not probs:
            continue
        ranked = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
        best_label, best_p = ranked[0]
        runner_p = ranked[1][1] if len(ranked) > 1 else 0.0
        out.append({
            "key": q["key"], "label": q["label"],
            "pick": best_label, "prob": best_p, "edge": best_p - runner_p,
            "note": q.get("note", ""),
            "skill": QUESTION_SKILL.get(q["key"]),
            "has_skill": ((QUESTION_SKILL.get(q["key"]) or {}).get("gain", 0.0)
                          >= SKILL_THRESHOLD),
            "exp": pred.exp.get(q.get("source")),
            "all": list(probs.items()),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 5. 출력
# ─────────────────────────────────────────────────────────────────────────────

def bar(p: float, width: int = 18) -> str:
    n = int(round(p * width))
    return "█" * n + "·" * (width - n)


def render(ctx: GameContext, pred: Prediction, picks: list) -> str:
    lg, opp = ctx.lg, ctx.opp
    L: list = []
    W = 76
    L.append("═" * W)
    L.append(f" 스타볼 예측 | {ctx.game_date} {ctx.start_time} {ctx.stadium}"
             f" | {ctx.away.name} @ {ctx.home.name}")
    L.append("═" * W)

    L.append("\n[ 팀 현황 ]")
    L.append(f"   {'팀':<8}{'순위':>4} {'시즌전적':>12} {'득점/G':>7} {'실점/G':>7}"
             f" {'최근5':>7} {'상대전적':>8}")
    for s in (lg, opp):
        tag = " ★ " if s.code == MY_TEAM else "   "
        L.append(f"{tag}{s.name:<8}{s.rank:>3}위 {s.wins:>3}승{s.losses:>3}패{s.draws:>2}무"
                 f" {s.rs_per_game:>7.2f} {s.ra_per_game:>7.2f} {s.recent_form:>7}"
                 f" {s.h2h_w:>3}승{s.h2h_l:>2}패{s.h2h_d:>2}무")

    L.append("\n[ 선발 매치업 ]")
    for s, foe in ((lg, opp), (opp, lg)):
        sp = s.starter
        L.append(f"   {s.name} · {sp.name:<8} 시즌 ERA {sp.season_era:>5.2f}"
                 f"  WHIP {sp.season_whip:>4.2f}  {sp.season_ip:>5.1f}이닝"
                 f"  {sp.season_games}경기")
        if sp.vs_era is not None:
            L.append(f"   {'':<11} {foe.name}전 ERA {sp.vs_era:.2f}"
                     f" ({sp.vs_ip:.1f}이닝 / {sp.vs_games}경기)")
        else:
            L.append(f"   {'':<11} {foe.name}전 등판 이력 없음")
        if sp.recent_era is not None:
            starts = sum(1 for g in sp.recent_log if g.get("started"))
            role = (f"선발 {starts}" if starts == sp.recent_starts
                    else f"선발 {starts}·구원 {sp.recent_starts - starts}")
            L.append(f"   {'':<11} 최근 {sp.recent_starts}등판({role}) "
                     f"ERA {sp.recent_era:.2f} · WHIP {sp.recent_whip:.2f}"
                     f" ({sp.recent_ip:.1f}이닝)")
            L.append(f"   {'':<11}   " + " · ".join(
                f"{g['date'][5:]} {g['ip']}이닝 {g['er']}자책"
                for g in sp.recent_log))
        elif sp.announced:
            L.append(f"   {'':<11} 최근 등판 기록 없음")
        if sp.pitches:
            pk = ", ".join(f"{p.get('type')} {fnum(p.get('pit_rt')):.0f}%"
                           f"/{fnum(p.get('speed')):.0f}km"
                           for p in sp.pitches[:3])
            L.append(f"   {'':<11} 주무기: {pk}")

    L.append("\n[ 분석 근거 ]")
    for d in pred.drivers:
        L.append(f"   {d}" if d.startswith("  ") else f"   • {d}")

    e = pred.exp
    L.append(f"\n[ 예상 스코어 ]  {lg.name} {e['lg_runs']:.2f}"
             f" : {e['opp_runs']:.2f} {opp.name}    "
             f"가장 흔한 스코어 {pred.modal_score[0]}-{pred.modal_score[1]},"
             f" 기대 마진 {pred.exp_margin:+.2f}")
    L.append(f"   선발 예상 자책점 — {lg.name} {e['lg_sp_er']:.2f}점,"
             f" {opp.name} {e['opp_sp_er']:.2f}점")
    for stat, cfg in COUNT_STATS.items():
        L.append(f"   예상 {cfg['label']} — {lg.name} {e[f'lg_{stat}']:.2f} /"
                 f" {opp.name} {e[f'opp_{stat}']:.2f}"
                 f"  (합계 {e[f'total_{stat}']:.2f})")

    L.append(f"\n[ 스타볼 추천 ]  종합 신뢰도: {pred.confidence}")
    for p in picks:
        bits = [f"{p['prob']*100:.1f}%", f"2위와 {p['edge']*100:+.1f}%p"]
        if p.get("note"):
            bits.append(p["note"])
        if p.get("skill"):
            bits.append(f"실측 {p['skill']['hit']:.0f}%")
        if not p["has_skill"]:
            flag = "  ← 과거 검증 실패, 참고만"
        elif p["edge"] < NTFY_COINFLIP_EDGE:
            flag = "  ← 이번 경기는 판단 불가"
        else:
            flag = ""
        L.append(f"\n   ▸ {p['label']}  →  {p['pick']}"
                 f"   ({', '.join(bits)}){flag}")
        for lbl, prob in p["all"]:
            mark = "✔" if lbl == p["pick"] else " "
            L.append(f"       {mark} {lbl:<12} {bar(prob)} {prob*100:5.1f}%")

    if ctx.warnings:
        L.append("\n[ 주의 ]")
        for w in ctx.warnings:
            L.append(f"   ! {w}")

    L.append("\n" + "─" * W)
    L.append(" 확률 모델의 산출물이며 결과를 보장하지 않습니다.")
    L.append("─" * W)
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# 6. ntfy.sh 푸시 알림
# ─────────────────────────────────────────────────────────────────────────────
#
# 자동 제출(Playwright) 모듈은 제거했다. 스타볼은 경품이 걸린 이벤트라 약관이
# 매크로 참여를 금지하는 경우가 많다. 예측 결과를 휴대폰으로 받아보고 입력은
# 직접 하는 쪽이 안전하고, 그 역할을 ntfy 가 맡는다.

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC_DEFAULT = "lg_starball_predict_2026"
NTFY_TITLE = "⚾ 오늘의 LG 스타볼 예측 추천"
NTFY_CLICK = "https://www.lgtwins.com"
NTFY_TAGS = "baseball,chart_with_upwards_trend"
NTFY_STARBALL_URL = "https://www.lgtwins.com/starball"   # 액션 버튼 이동지
# 1위와 2위 확률 격차가 이 미만이면 '반반'으로 따로 묶는다.
NTFY_COINFLIP_EDGE = 0.05

# 마감 임박 알림을 보낼 구간(경기 시작까지 남은 시간).
# 워크플로가 12:00 / 15:00 / 16:30 (KST) 에 돌면서, 각 경기가 이 구간에
# 들어올 때만 딱 한 번 걸리도록 맞춰져 있다. 구간을 넓히면 한 경기에
# 두 번 갈 수 있으니 크론 시각과 같이 봐야 한다.
REMINDER_WINDOW = (1.0, 3.0)   # 시간




def resolve_ntfy_topic(cli_topic: Optional[str] = None) -> str:
    """토픽 우선순위: --ntfy-topic > NTFY_TOPIC 환경변수 > 기본값."""
    return cli_topic or os.environ.get("NTFY_TOPIC") or NTFY_TOPIC_DEFAULT



def hours_until_start(ctx: GameContext) -> Optional[float]:
    """경기 시작까지 남은 시간. 시각을 모르면 None."""
    if not ctx.start_time:
        return None
    try:
        h, m = (int(x) for x in ctx.start_time.split(":"))
        start = datetime.combine(date.fromisoformat(ctx.game_date),
                                 datetime.min.time(), tzinfo=KST)
        start = start.replace(hour=h, minute=m)
        return (start - datetime.now(KST)).total_seconds() / 3600.0
    except Exception:
        return None


def build_reminder_message(ctx: GameContext, pred: Prediction,
                           picks: list, hours: float) -> str:
    """마감 임박 알림. 아침 알림과 달리 짧게 — 값 세 개만 다시 보여준다."""
    best = (pred.combo or {}).get("best") or {}
    order = [q["key"] for q in STARBALL_QUESTIONS]
    return "\n".join([
        " · ".join(best[k] for k in order if k in best) or "예측 불가",
        f"{ctx.away.name}@{ctx.home.name} {ctx.start_time} 시작"
        f" · {hours:.1f}시간 남음",
        "",
        "아직 안 넣었으면 지금 넣으세요.",
    ])


def build_ntfy_message(ctx: GameContext, pred: Prediction, picks: list) -> str:
    """푸시 본문.

    폰 알림은 접힌 상태에서 두세 줄만 보인다. 그래서 첫 줄에 '그대로 입력할
    세 값'을 몰아넣는다. 스타볼은 미션을 모두 맞혀야 지급되므로, 문항별 1위가
    아니라 조합 최적해(pred.combo)를 싣는다. 순서는 앱 폼 순서 그대로다.
    """
    by_key = {p["key"]: p for p in picks}
    combo = pred.combo or {}
    best = combo.get("best") or {}
    order = [q["key"] for q in STARBALL_QUESTIONS]

    digest = " · ".join(best[k] for k in order if k in best) or "예측 불가"
    lines = [digest]

    lines.append(f"{ctx.away.name}@{ctx.home.name} {ctx.stadium} {ctx.start_time}"
                 f" · 예상 {pred.exp['lg_runs']:.1f}-{pred.exp['opp_runs']:.1f}"
                 f" · 신뢰도 {pred.confidence}")

    if best:
        lines.append("")
        lines.append("■ 이대로 입력하세요")
        for i, k in enumerate(order, 1):
            if k not in best:
                continue
            p = by_key.get(k)
            label = p["label"] if p else k
            mark = f"  ({p['prob'] * 100:.0f}%)" if p else ""
            lines.append(f"MISSION {i}. {label} → {best[k]}{mark}")
        lines.append("")
        lines.append(f"3개 모두 적중 확률 {combo.get('best_prob', 0) * 100:.1f}%"
                     f" · 스타볼 1개")

    # 신뢰도를 깎은 이유가 있으면 하나만. 전부 넣으면 알림이 길어진다.
    if ctx.warnings and pred.confidence != "높음":
        lines += ["", f"※ {ctx.warnings[0]}"]

    return "\n".join(lines)


def send_ntfy_notification(ctx: GameContext, pred: Prediction, picks: list,
                           topic: Optional[str] = None,
                           server: str = NTFY_SERVER,
                           timeout: int = 15,
                           reminder: bool = False) -> dict:
    """예측 요약을 ntfy 토픽으로 푸시한다.

    푸시 실패가 예측 결과까지 날리면 안 되므로 예외를 삼키고 dict 로 알린다.
    호출부가 ok 를 보고 안내 문구를 찍는다.
    """
    topic = resolve_ntfy_topic(topic)
    url = f"{server.rstrip('/')}/{topic}"
    if reminder:
        hours = hours_until_start(ctx) or 0.0
        message = build_reminder_message(ctx, pred, picks, hours)
    else:
        message = build_ntfy_message(ctx, pred, picks)

    # 헤더가 아니라 JSON 본문으로 발행한다. HTTP 헤더는 latin-1 이라 한글을
    # RFC 2047 로 인코딩해야 하는데, 값이 길면 인코딩 결과가 줄바꿈되면서
    # requests 가 거부한다(액션 라벨에서 실제로 터졌다). JSON 은 그 문제가 없다.
    payload = {
        "topic": topic,
        "title": "⏰ 스타볼 마감 전 알림" if reminder else NTFY_TITLE,
        "message": message,
        "click": NTFY_CLICK,
        "tags": [t.strip() for t in NTFY_TAGS.split(",") if t.strip()],
        "actions": [{"action": "view", "label": "스타볼 입력하러",
                     "url": NTFY_STARBALL_URL, "clear": True}],
    }

    try:
        r = requests.post(server.rstrip("/") + "/", json=payload, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        return {"ok": False, "topic": topic, "url": url,
                "error": f"{type(e).__name__}: {e}", "message": message}

    return {"ok": True, "topic": topic, "url": url,
            "status": r.status_code, "message": message}



def context_from_fixture(snap: dict) -> GameContext:
    """--save-fixture 로 남긴 원본 응답을 되감아 GameContext 를 재구성한다.

    네트워크 없이 모델/출력만 테스트하거나 과거 경기로 백테스트할 때 쓴다.
    """
    game, preview = snap["game"], snap["preview"]
    # GameContext.lg / .opp 는 MY_TEAM 이 반드시 한쪽에 있다고 가정한다.
    # 아니면 조용히 엉뚱한 팀을 가리키므로 여기서 막는다.
    if MY_TEAM not in (game.get("homeTeamCode"), game.get("awayTeamCode")):
        raise SystemExit(
            f"이 스냅샷은 {MY_TEAM} 경기가 아닙니다 "
            f"({game.get('awayTeamCode')} @ {game.get('homeTeamCode')}).")
    tstats = pd.DataFrame(snap["team_stats"]).set_index("teamId")
    h2h = pd.DataFrame(snap.get("h2h") or [])

    class _Replay(NaverKBO):
        def __init__(self):
            self.calls = []

        def find_team_game(self, day, team=MY_TEAM):
            return {"gameId": game["gameId"]}

        def game(self, game_id):
            return game

        def preview(self, game_id):
            return preview

        def team_stats(self, year):
            return tstats

        def head_to_head_games(self, year, a, b, before):
            return h2h

    ctx = build_context(_Replay(), date.fromisoformat(game["gameDate"]),
                        with_pitcher_recent=False)
    # 스냅샷에 최근 등판이 남아 있으면 되감는다(네트워크 재조회 없이).
    for key, side in (("home", ctx.home), ("away", ctx.away)):
        saved = (snap.get("pitcher_recent") or {}).get(key)
        if not saved:
            continue
        sp = side.starter
        sp.recent_ip = saved.get("ip", 0.0)
        sp.recent_er = saved.get("er", 0)
        sp.recent_hit = saved.get("hit", 0)
        sp.recent_bb = saved.get("bb", 0)
        sp.recent_starts = saved.get("starts", 0)
        sp.recent_log = saved.get("log", [])
    return ctx


def cmd_probe(client: NaverKBO, day: date) -> int:
    """각 엔드포인트가 살아있는지, 필요한 필드가 있는지 점검한다."""
    print(f"엔드포인트 점검 ({day.isoformat()})")
    print("─" * 66)
    ok = True

    def check(name, fn, validate):
        nonlocal ok
        try:
            v = fn()
            good, detail = validate(v)
            print(f"  {'OK  ' if good else 'WARN'}  {name:<30} {detail}")
            ok = ok and good
            return v
        except Exception as e:
            print(f"  FAIL  {name:<30} {type(e).__name__}: {e}")
            ok = False
            return None

    # 경기 없는 날에도 엔드포인트 자체는 점검해야 한다. 예전에는 '오늘 경기
    # 없음'을 실패로 보고하고 나머지 검사를 건너뛰어, 휴식일마다 거짓 경보가
    # 났다. 오늘 경기가 없으면 가장 최근 경기로 나머지를 점검한다.
    gi = check("schedule/calendar",
               lambda: client.find_team_game(day),
               lambda v: (True, f"{MY_TEAM} 경기 {v['gameId']}" if v
                          else f"오늘은 {MY_TEAM} 경기 없음 (엔드포인트는 정상)"))

    if gi is None:
        recent = client.team_games_before(day.year, MY_TEAM, day, limit=1)
        if recent:
            gi = {"gameId": recent[0][1]}
            print(f"  ..    오늘 경기가 없어 최근 경기({recent[0][0]})로 점검합니다",
                  file=sys.stderr)

    if gi:
        check("schedule/games/{id}", lambda: client.game(gi["gameId"]),
              lambda v: ("homeTeamCode" in v,
                         f"{v.get('stadium')} {v.get('gameDateTime')}"))

        def _preview_ok(v):
            if not v:
                return False, "previewData 없음(선발 예고 전)"
            hs = (v.get("homeStarter") or {}).get("playerInfo") or {}
            has_vs = bool((v.get("homeStarter") or {})
                          .get("currentSeasonStatsOnOpponents"))
            return (bool(hs.get("name")),
                    f"홈 선발 {hs.get('name', '미예고')}"
                    f", 상대전적 스탯 {'있음' if has_vs else '없음'}")

        check("schedule/games/{id}/preview",
              lambda: client.preview(gi["gameId"]), _preview_ok)

    check("statistics/.../teams", lambda: client.team_stats(day.year),
          lambda v: (len(v) == 10 and "offenseRun" in v.columns,
                     f"{len(v)}개 팀, 컬럼 {len(v.columns)}개"))
    print("─" * 66)
    print("전체 정상" if ok else "일부 실패 — --fixture 로 오프라인 실행 가능")
    return 0 if ok else 1


_load_park_factors()
_load_questions()
_load_base_rates()


def _positive_int(text: str) -> int:
    """--sims 검증. 0을 넘기면 predict() 안에서 0으로 나누게 된다."""
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"정수가 아닙니다: {text}")
    if n < 1000:
        raise argparse.ArgumentTypeError(
            f"시뮬레이션 횟수는 1000 이상이어야 합니다 (받은 값: {n})")
    return n


def main(argv=None) -> int:
    # parse_args() 는 --help 를 찍고 바로 종료하므로 인코딩 설정이 먼저다.
    # cp949 콘솔에서는 이 순서를 놓치면 도움말이 통째로 깨진다.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="LG 트윈스 스타볼 예측 파이프라인")
    ap.add_argument("--date", help="경기일 YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--probe", action="store_true", help="엔드포인트 생존 점검")
    ap.add_argument("--fixture", help="저장된 스냅샷으로 오프라인 실행")
    ap.add_argument("--save-fixture", metavar="PATH", help="응답 스냅샷 저장")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    ap.add_argument("--no-cache", action="store_true", help="캐시 무시")
    ap.add_argument("--sims", type=_positive_int, default=N_SIM,
                    help=f"시뮬레이션 횟수 (기본 {N_SIM}, 최소 1000)")
    ap.add_argument("--no-pitcher-recent", action="store_true",
                    help="투수 최근 등판 수집을 건너뛴다(요청 수를 줄임)")
    ap.add_argument("--home-only", action="store_true",
                    help="LG 홈 경기일 때만 실행(원정이면 조용히 종료)")
    ap.add_argument("--compute-park-factors", action="store_true",
                    help="구장별 홈런 팩터를 실측 재계산해 park_factors.json 저장")
    ap.add_argument("--ntfy", action="store_true",
                    help="예측 완료 후 ntfy 로 푸시 알림 발송")
    ap.add_argument("--reminder", action="store_true",
                    help="마감 임박 알림. 경기 시작이 REMINDER_WINDOW 안일 "
                         "때만 짧은 알림을 보내고, 아니면 조용히 종료한다")
    ap.add_argument("--ntfy-topic", metavar="TOPIC",
                    help=f"ntfy 토픽 (미지정 시 $NTFY_TOPIC, "
                         f"그다음 '{NTFY_TOPIC_DEFAULT}')")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    day = date.fromisoformat(args.date) if args.date else today_kst()

    client = NaverKBO(use_cache=not args.no_cache, verbose=args.verbose)

    if args.probe:
        return cmd_probe(client, day)

    if args.compute_park_factors:
        factors, bullpen = compute_park_factors(client, day.year)
        if not factors:
            print("계산할 경기 데이터를 얻지 못했습니다.", file=sys.stderr)
            return 1
        with open(PARK_FACTOR_FILE, "w", encoding="utf-8") as f:
            json.dump({"computed": day.isoformat(), "season": day.year,
                       "factors": factors, "bullpen_ra9": bullpen},
                      f, ensure_ascii=False, indent=1)
        print(f"\n저장 완료 → {PARK_FACTOR_FILE}", file=sys.stderr)
        print(json.dumps(factors, ensure_ascii=False, indent=2))
        return 0

    if args.fixture:
        with open(args.fixture, encoding="utf-8") as f:
            ctx = context_from_fixture(json.load(f))
    else:
        snap: dict = {}
        try:
            ctx = build_context(client, day, snapshot=snap,
                                home_only=args.home_only,
                                with_pitcher_recent=not args.no_pitcher_recent)
        except NoGame as e:
            # 스케줄러가 매일 도는 걸 전제로 한다. 경기 없는 날은 실패가 아니다.
            print(str(e), file=sys.stderr)
            return 0
        except NotReady as e:
            # 선발 예고 전. 나중에 다시 돌리라는 뜻으로 별도 코드를 준다.
            print(str(e), file=sys.stderr)
            return 75
        if args.save_fixture:
            snap["_meta"] = {"date": day.isoformat(),
                             "saved": datetime.now().isoformat()}
            with open(args.save_fixture, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=1, default=str)
            print(f"스냅샷 저장 → {args.save_fixture}", file=sys.stderr)

    pred = predict(ctx, n_sim=args.sims)
    picks = to_starball_choices(pred)

    if args.json:
        print(json.dumps({"context": asdict(ctx), "prediction": asdict(pred),
                          "picks": picks},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(render(ctx, pred, picks))

    if args.reminder:
        # 크론은 고정 시각인데 경기 시각은 14:00/17:00/18:30 으로 다르다.
        # 그래서 '남은 시간'으로 걸러, 각 경기가 딱 한 번만 걸리게 한다.
        h = hours_until_start(ctx)
        lo, hi = REMINDER_WINDOW
        if h is None or not (lo <= h < hi):
            print(f"마감 임박 구간이 아닙니다 "
                  f"(남은 시간 {h if h is None else round(h, 1)}시간) — 건너뜁니다.",
                  file=sys.stderr)
            return 0
        res = send_ntfy_notification(ctx, pred, picks, topic=args.ntfy_topic,
                                     reminder=True)
        if not res["ok"]:
            print(f"\nntfy 발송 실패: {res['error']}", file=sys.stderr)
            return 1
        print(f"\n마감 임박 알림 발송 ({h:.1f}시간 전)", file=sys.stderr)
        return 0

    if args.ntfy:
        res = send_ntfy_notification(ctx, pred, picks, topic=args.ntfy_topic)
        if res["ok"]:
            print(f"\nntfy 발송 완료 → {res['url']}", file=sys.stderr)
        else:
            # 푸시가 실패해도 예측 결과는 이미 출력됐으므로 종료 코드만 올린다.
            print(f"\nntfy 발송 실패: {res['error']}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
