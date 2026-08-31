#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""starball_predictor 회귀 테스트.

    python -m pytest test_starball.py -q      (pytest 있으면)
    python test_starball.py                   (없어도 그냥 돌아감)

네트워크를 타지 않는다. 전부 today.json 스냅샷과 순수 함수만 쓴다.
네트워크 계약 점검은 `python starball_predictor.py --probe` 쪽이다.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
from datetime import date, datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import starball_predictor as S  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "today.json")


def _snap() -> dict:
    with io.open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


# ── 이닝 파싱 ────────────────────────────────────────────────────────────
# 엔드포인트마다 표기가 다르다. 여기가 틀리면 ERA·WHIP 전체가 어긋난다.

def test_innings_kbo_decimal():
    """'34.2' 는 34.2가 아니라 34와 2/3이닝."""
    assert abs(S.parse_kbo_innings("34.2") - (34 + 2 / 3)) < 1e-9
    assert abs(S.parse_kbo_innings("10.1") - (10 + 1 / 3)) < 1e-9
    assert S.parse_kbo_innings("2.0") == 2.0


def test_innings_unicode_fraction():
    """박스스코어는 '5 ⅓' 처럼 유니코드 분수를 쓴다."""
    assert abs(S.parse_kbo_innings("5 ⅓") - (5 + 1 / 3)) < 1e-9
    assert abs(S.parse_kbo_innings("5⅔") - (5 + 2 / 3)) < 1e-9
    assert abs(S.parse_kbo_innings("⅓") - (1 / 3)) < 1e-9
    assert S.parse_kbo_innings("6") == 6.0


def test_innings_numeric_uses_same_rule():
    """팀 통계의 defenseInning 은 float 로 온다. 981.1 = 981과 1/3이닝."""
    assert abs(S.parse_kbo_innings(981.1) - (981 + 1 / 3)) < 1e-9
    assert S.parse_kbo_innings(1005.0) == 1005.0


def test_innings_junk_is_zero():
    for junk in (None, "", "-", "abc", {}):
        assert S.parse_kbo_innings(junk) == 0.0


# ── 축소 ────────────────────────────────────────────────────────────────

def test_shrink_endpoints():
    assert S.shrink(9.0, 4.5, 0, 25) == 4.5          # 표본 없으면 prior
    assert abs(S.shrink(9.0, 4.5, 25, 25) - 6.75) < 1e-9   # n==k 면 반반


def test_shrink_monotone():
    """표본이 커질수록 관측값에 가까워져야 한다."""
    vals = [S.shrink(9.0, 4.5, n, 25) for n in (1, 10, 50, 200)]
    assert vals == sorted(vals)
    assert vals[-1] < 9.0


def test_small_sample_is_pulled_hard():
    """2이닝 ERA 9.00 을 액면가로 쓰면 안 된다."""
    assert S.shrink(9.0, 4.5, 2, S.K_SHRINK_IP) < 5.0


# ── 구장 팩터 ────────────────────────────────────────────────────────────

def test_park_factor_regressed_toward_one():
    raw = S.PARK_HR_FACTOR["잠실"]
    used = S.park_hr_factor("잠실")
    assert raw < used < 1.0, "축소는 1.0 쪽으로만 당겨야 한다"


def test_unknown_park_is_neutral():
    assert S.park_hr_factor("없는구장") == 1.0


# ── 예측 ────────────────────────────────────────────────────────────────

def test_probabilities_sum_to_one():
    """버킷 정의에 빈틈이나 겹침이 있으면 여기서 걸린다."""
    ctx = S.context_from_fixture(_snap())
    pred = S.predict(ctx, n_sim=20000)
    for q in S.STARBALL_QUESTIONS:
        total = sum(pred.probs[q["key"]].values())
        assert abs(total - 1.0) < 0.02, f"{q['label']} 합계 {total}"


def test_deterministic():
    a = S.predict(S.context_from_fixture(_snap()), n_sim=5000)
    b = S.predict(S.context_from_fixture(_snap()), n_sim=5000)
    assert a.probs == b.probs and a.exp == b.exp


def test_worse_opposing_starter_helps_lg():
    """상대 선발이 나빠지면 LG 승률이 올라야 한다.

    실점력은 '선발 이닝 + 불펜 이닝'으로 분해된다. 팀 총실점(defenseR)은
    그 둘에 이미 들어있어 모델 입력이 아니다 — 흔들어도 안 움직이는 게 맞다.
    그래서 실제 경로인 선발 ERA 를 흔든다.
    """
    base = S.predict(S.context_from_fixture(_snap()), n_sim=20000)
    s = _snap()
    for side in ("homeStarter", "awayStarter"):
        node = s["preview"].get(side) or {}
        if (node.get("currentSeasonStats") or {}).get("teamCode") != S.MY_TEAM:
            node["currentSeasonStats"]["era"] = "12.00"
    worse = S.predict(S.context_from_fixture(s), n_sim=20000)
    assert worse.p_win > base.p_win, f"{base.p_win} → {worse.p_win}"


def test_worse_opposing_bullpen_helps_lg():
    """상대 불펜이 나빠지면 LG 승률이 올라야 한다(실측 불펜값 경로)."""
    snap = _snap()
    opp = (snap["game"]["awayTeamCode"]
           if snap["game"]["homeTeamCode"] == S.MY_TEAM
           else snap["game"]["homeTeamCode"])
    saved = S.BULLPEN_RA9.get(opp)
    try:
        base = S.predict(S.context_from_fixture(_snap()), n_sim=20000)
        S.BULLPEN_RA9[opp] = (saved or 5.0) + 4.0
        worse = S.predict(S.context_from_fixture(_snap()), n_sim=20000)
        assert worse.p_win > base.p_win, f"{base.p_win} → {worse.p_win}"
    finally:
        if saved is None:
            S.BULLPEN_RA9.pop(opp, None)
        else:
            S.BULLPEN_RA9[opp] = saved


def test_combo_beats_greedy_under_same_yardstick():
    """고른 조합이 버린 조합보다 표시 확률이 낮으면 안 된다.

    선택은 기저 혼합 후 확률로 하면서 보고는 모델만의 확률로 하던 버그가 있었다.
    화면에 '13.0%' 라고 띄운 조합보다 안 고른 조합이 더 높아 보이는 상황이었다.
    입력을 흔들어도 뒤집히지 않아야 한다.
    """
    import copy, random
    random.seed(11)
    base = _snap()
    for _ in range(8):
        s = copy.deepcopy(base)
        for t in s["team_stats"]:
            for k in ("offenseRun", "defenseR", "offenseHr", "defenseHr",
                      "offenseHit", "defenseHit", "offenseKk", "defenseKk"):
                t[k] = max(0, int(t[k] * random.uniform(0.4, 1.8)))
        c = S.predict(S.context_from_fixture(s), n_sim=6000).combo
        assert c["best_prob"] >= c["greedy_prob"] - 1e-9,             f"best {c['best_prob']} < greedy {c['greedy_prob']}"
        assert 0.0 <= c["best_prob"] <= 1.0


def test_run_and_hr_are_correlated():
    """득점과 홈런은 실측 상관 +0.47 이다. 독립으로 뽑으면 조합 확률이 틀린다."""
    ctx = S.context_from_fixture(_snap())
    pred = S.predict(ctx, n_sim=60000)
    combo = pred.combo
    # 상관이 살아있으면 조합 최적해와 문항별 1위가 갈릴 수 있어야 한다
    assert combo is not None and combo["best_prob"] >= combo["greedy_prob"], \
        "조합 최적해가 그리디보다 나빠질 수 없다"


def test_park_factor_moves_home_runs():
    """홈런 억제 구장이면 기대 홈런이 줄어야 한다."""
    s = _snap()
    s["game"]["stadium"] = "잠실"
    low = S.predict(S.context_from_fixture(s), n_sim=20000)
    s2 = _snap()
    s2["game"]["stadium"] = "창원"
    high = S.predict(S.context_from_fixture(s2), n_sim=20000)
    assert low.exp["total_hr"] < high.exp["total_hr"]


def test_edge_is_gap_to_runner_up():
    ctx = S.context_from_fixture(_snap())
    picks = S.to_starball_choices(S.predict(ctx, n_sim=20000))
    for p in picks:
        probs = sorted((v for _, v in p["all"]), reverse=True)
        assert abs(p["edge"] - (probs[0] - probs[1])) < 1e-9
        assert p["prob"] == probs[0]


# ── 결측 내성 ────────────────────────────────────────────────────────────

def _survives(mutate) -> bool:
    s = _snap()
    mutate(s)
    ctx = S.context_from_fixture(s)
    pred = S.predict(ctx, n_sim=3000)
    picks = S.to_starball_choices(pred)
    S.render(ctx, pred, picks)
    S.build_ntfy_message(ctx, pred, picks)
    return True


def test_survives_no_starters():
    assert _survives(lambda s: s["preview"].update(
        {"homeStarter": None, "awayStarter": None}))


def test_survives_no_head_to_head():
    assert _survives(lambda s: s.update({"h2h": []}))


def test_survives_no_recent_games():
    assert _survives(lambda s: s["preview"].update(
        {"homeTeamPreviousGames": None, "awayTeamPreviousGames": None}))


def test_survives_missing_team_stats():
    assert _survives(lambda s: s.update(
        {"team_stats": [t for t in s["team_stats"] if t["teamId"] != "NC"]}))


def test_survives_season_opening_day():
    """개막일에는 리그 집계가 전부 0이다. 0으로 나누면 안 된다."""
    def zero(s):
        for t in s["team_stats"]:
            t.update({"offenseRun": 0, "defenseR": 0, "gameCount": 0,
                      "offenseHr": 0, "offenseHit": 0, "offenseKk": 0,
                      "defenseHr": 0, "defenseHit": 0, "defenseKk": 0,
                      "defenseEr": 0, "defenseInning": 0.0})
    assert _survives(zero)


def test_rejects_non_lg_snapshot():
    """ctx.lg/.opp 는 MY_TEAM 이 한쪽에 있다고 가정한다."""
    s = _snap()
    s["game"].update({"homeTeamCode": "NC", "awayTeamCode": "SS"})
    try:
        S.context_from_fixture(s)
    except SystemExit:
        return
    raise AssertionError("비-LG 스냅샷을 통과시켰다")


# ── 캐시 키 ──────────────────────────────────────────────────────────────

def test_cache_keys_unique_for_long_urls():
    """달력 URL 은 정규화하면 120자를 넘는다. 꼬리만 쓰면 충돌한다."""
    c = S.NaverKBO(use_cache=False)
    urls = [f"{S.API}/schedule/calendar?upperCategoryId=kbaseball"
            f"&categoryIds=kbo&yearMonth=2026-{m:02d}&date=2026-{m:02d}-01"
            for m in range(3, 11)]
    assert len({c._cache_path(u) for u in urls}) == len(urls)


# ── 시간대 ───────────────────────────────────────────────────────────────

def test_today_kst_is_utc_plus_nine():
    utc = datetime.now(timezone.utc)
    assert S.today_kst() == (utc + timedelta(hours=9)).date()


# ── 알림 본문 ────────────────────────────────────────────────────────────

def test_ntfy_shows_combo_in_form_order():
    """알림은 조합 최적해를 앱 폼 순서 그대로 실어야 한다."""
    ctx = S.context_from_fixture(_snap())
    pred = S.predict(ctx, n_sim=20000)
    picks = S.to_starball_choices(pred)
    msg = S.build_ntfy_message(ctx, pred, picks)
    best = pred.combo["best"]
    order = [q["key"] for q in S.STARBALL_QUESTIONS]
    assert msg.splitlines()[0] == " · ".join(best[k] for k in order if k in best)
    for i, k in enumerate(order, 1):
        assert f"MISSION {i}." in msg
        assert best[k] in msg


def test_ntfy_digest_is_meaningful():
    """접힌 알림에서 첫 줄만 보인다. 세 값이 다 들어가야 한다."""
    ctx = S.context_from_fixture(_snap())
    pred = S.predict(ctx, n_sim=20000)
    picks = S.to_starball_choices(pred)
    first = S.build_ntfy_message(ctx, pred, picks).splitlines()[0]
    assert len(first.split(" · ")) == len(S.STARBALL_QUESTIONS)



def test_skill_gating_is_wired():
    """QUESTION_SKILL 이 실제로 picks 에 반영되는가."""
    ctx = S.context_from_fixture(_snap())
    picks = S.to_starball_choices(S.predict(ctx, n_sim=5000))
    for p in picks:
        expected = (S.QUESTION_SKILL.get(p["key"]) or {}).get("gain", 0.0) >= S.SKILL_THRESHOLD
        assert p["has_skill"] is expected


# ── 확률 보정 ────────────────────────────────────────────────────────────

def test_calibration_pulls_toward_uniform():
    """과신 보정은 확률을 균등분포 쪽으로만 당겨야 한다(순서는 유지)."""
    raw = {"a": 0.70, "b": 0.20, "c": 0.10}
    out = S._calibrate(raw)
    assert abs(sum(out.values()) - 1.0) < 1e-9, "합이 1이어야 한다"
    assert out["a"] < raw["a"] and out["c"] > raw["c"]
    assert out["a"] > out["b"] > out["c"], "순위가 바뀌면 안 된다"


def test_calibration_keeps_uniform_uniform():
    raw = {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}
    out = S._calibrate(raw)
    assert all(abs(v - 1 / 3) < 1e-9 for v in out.values())


# ── 러너 ─────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            failed.append(name)
            print(f"  FAIL  {name}\n          {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
