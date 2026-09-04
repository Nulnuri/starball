#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""학습 경로 회귀 테스트.

    python -m pytest test_train.py -q      (pytest 있으면)
    python test_train.py                   (없어도 그냥 돌아감)

네트워크도 scikit-learn 도 타지 않는다. 특징을 만드는 부분만 검사한다.
여기가 틀리면 학습은 조용히 성공하고 예측만 틀린다 — 가장 잡기 어려운 종류다.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_h2h_history as H  # noqa: E402
import train_outcome as T  # noqa: E402


def _game(date, away, home, away_score, home_score, stadium="잠실",
          away_sp="A선발", home_sp="H선발", away_hr_allowed=1, home_hr_allowed=2):
    """경기 한 건. box 는 투수 기준이라 hr_allowed 가 '상대 타선이 친 홈런' 이다."""
    def pit(name, er=3, hr=1):
        return [{"pcode": name, "name": name, "started": True,
                 "ip": 6.0, "er": er, "r": er, "hit": 5, "bb": 2, "hr": hr},
                {"pcode": name + "_불펜", "name": "불펜", "started": False,
                 "ip": 3.0, "er": 1, "r": 1, "hit": 2, "bb": 1, "hr": 0}]
    return {
        "date": date, "gameId": f"{date}{away}{home}0",
        "stadium": stadium, "home": home, "away": away,
        "home_score": home_score, "away_score": away_score,
        "box": {"home": {"hr_allowed": home_hr_allowed, "hit_allowed": 8,
                         "k_thrown": 7, "r_allowed": away_score,
                         "er_allowed": away_score},
                "away": {"hr_allowed": away_hr_allowed, "hit_allowed": 9,
                         "k_thrown": 6, "r_allowed": home_score,
                         "er_allowed": home_score}},
        "pitchers": {"home": pit(home_sp), "away": pit(away_sp)},
    }


# 선발은 5명씩 돌린다. 매 경기 다른 이름을 주면 아무도 누적 하한(20이닝)을
# 못 넘겨 표본이 0건이 된다 — 실제로 이 함정에 걸렸다.
ROTATION = 5


def sp(team: str, i: int) -> str:
    return f"{team}선발{i % ROTATION}"


def _season(n_warmup=25):
    """앞쪽에 준비 경기를 깔아 누적 하한을 넘긴다.

    팀 누적 15경기, 선발 누적 20이닝을 둘 다 넘겨야 표본이 나온다.
    선발이 6이닝씩 던지므로 5명 로테이션이면 20경기째에야 1인당 24이닝이
    된다 — 20경기로는 표본이 0건이라 25경기를 깐다.
    """
    games = []
    for i in range(n_warmup):
        d = f"2026-04-{i % 28 + 1:02d}"
        games.append(_game(d, "LG", "OB", 5, 3,
                           away_sp=sp("LG", i), home_sp=sp("OB", i)))
        games.append(_game(d, "SS", "KT", 2, 4,
                           away_sp=sp("SS", i), home_sp=sp("KT", i)))
        # NC 도 누적을 쌓아둔다. 안 그러면 NC 가 끼는 검사에서 표본이 안 나온다.
        games.append(_game(d, "NC", "HH", 4, 4, stadium="창원",
                           away_sp=sp("NC", i), home_sp=sp("HH", i)))
    return games


# ── 미래 정보가 새지 않는가 (가장 중요) ─────────────────────────────────

def test_no_future_leakage():
    """마지막 경기의 특징은 그 경기 결과를 몰라야 한다.

    누적 갱신을 특징 생성보다 먼저 하면 그 경기 점수가 자기 특징에 들어간다.
    그러면 검증 성적은 훌륭하고 실제 예측은 형편없어진다.
    """
    games = _season()
    last = _game("2026-06-01", "LG", "OB", 99, 0, away_sp=sp("LG", 0), home_sp=sp("OB", 0))
    rows_a = T.build_rows(games + [last])

    # 같은 경기를 점수만 뒤집어 다시 만든다. 특징은 똑같아야 한다.
    flipped = _game("2026-06-01", "LG", "OB", 0, 99, away_sp=sp("LG", 0), home_sp=sp("OB", 0))
    rows_b = T.build_rows(games + [flipped])

    a = [r for r in rows_a if r["date"] == "2026-06-01"]
    b = [r for r in rows_b if r["date"] == "2026-06-01"]
    assert a and len(a) == len(b), (len(a), len(b))
    for x, y in zip(a, b):
        assert x["feat"] == y["feat"], "그 경기 결과가 자기 특징에 새어들었다"
    # 정답(y)은 당연히 달라야 한다 — 안 다르면 테스트가 무의미하다
    assert [x["y"] for x in a] != [y["y"] for y in b]


def test_feature_count_matches_names():
    rows = T.build_rows(_season())
    assert rows, "표본이 하나도 안 나왔다"
    for r in rows:
        assert len(r["feat"]) == len(T.FEATURES), len(r["feat"])


def test_warmup_games_are_excluded():
    """누적이 얕은 초반 경기는 학습에 넣지 않는다."""
    rows = T.build_rows(_season(n_warmup=5))
    assert rows == [], f"하한을 못 지켰다: {len(rows)}건"


def test_home_flag_and_labels():
    games = _season()
    games.append(_game("2026-06-02", "LG", "OB", 7, 1, away_sp=sp("LG", 0), home_sp=sp("OB", 0)))
    rows = [r for r in T.build_rows(games) if r["date"] == "2026-06-02"]
    by = {r["team"]: r for r in rows}
    assert by["LG"]["feat"][T.FEATURES.index("home")] == 0.0    # LG 가 원정
    assert by["OB"]["feat"][T.FEATURES.index("home")] == 1.0
    assert by["LG"]["y"] == 0, "7-1 로 이긴 원정팀이 승이 아니다"
    assert by["OB"]["y"] == 2


def test_park_factor_comes_from_records():
    """구장 팩터는 하드코딩이 아니라 그 시즌 기록에서 나온다.

    2025 에 대전이 새 구장으로 바뀌었을 때 하드코딩 값(0.861)과 실제
    기록값(1.07)이 24% 어긋나 있었다. 홈런 모델의 주요 특징이라 그대로
    두면 시즌 내내 조용히 틀린다.
    """
    games = []
    for i in range(40):
        d = f"2026-04-{i % 28 + 1:02d}"
        # 창원은 홈런이 많이 나오고, 잠실은 적게 나오는 기록을 만든다
        games.append(_game(d, "NC", "HH", 5, 5, stadium="창원",
                           away_sp=sp("NC", i), home_sp=sp("HH", i),
                           away_hr_allowed=4, home_hr_allowed=4))
        games.append(_game(d, "LG", "OB", 3, 3, stadium="잠실",
                           away_sp=sp("LG", i), home_sp=sp("OB", i),
                           away_hr_allowed=0, home_hr_allowed=0))
    f = T.park_hr_factors(games)
    assert f["창원"] > 1.2, f
    assert f["잠실"] < 0.8, f
    # 기록에 없는 구장은 아예 나오지 않는다 (부르는 쪽이 1.0 으로 다룬다)
    assert "대구" not in f, f


def test_new_stadium_starts_neutral():
    """처음 보는 구장은 1.0 에서 시작한다 — 2027 신규 잠실야구장 대비.

    옛 구장의 팩터를 물려받으면 안 된다. 이름이 같아도(잠실 → 잠실)
    기록에서 다시 계산하므로, 새 구장은 표본이 쌓일 때까지 중립이다.
    """
    games = []
    for i in range(6):                      # 표본이 얕은 신규 구장
        games.append(_game(f"2027-03-{i + 20:02d}", "LG", "OB", 4, 4,
                           stadium="잠실",
                           away_sp=sp("LG", i), home_sp=sp("OB", i),
                           away_hr_allowed=3, home_hr_allowed=3))
    for i in range(40):                     # 리그 평균을 만드는 다른 구장
        games.append(_game(f"2027-04-{i % 28 + 1:02d}", "SS", "KT", 4, 4,
                           stadium="대구",
                           away_sp=sp("SS", i), home_sp=sp("KT", i),
                           away_hr_allowed=1, home_hr_allowed=1))
    f = T.park_hr_factors(games)
    # 실제로는 홈런이 3배 나왔지만 표본이 6경기뿐이라 1.0 쪽에 붙어 있어야 한다
    assert 0.9 < f["잠실"] < 1.9, f
    assert f["잠실"] < 3.0, "표본이 얕은데 원시 비율을 그대로 쓰고 있다"


def test_park_prior_carries_last_season():
    """작년 팩터가 있으면 개막 직후에 그쪽에서 시작한다."""
    games = [_game(f"2027-03-{i + 20:02d}", "LG", "OB", 4, 4, stadium="잠실",
                   away_sp=sp("LG", i), home_sp=sp("OB", i),
                   away_hr_allowed=1, home_hr_allowed=1) for i in range(4)]
    games += [_game(f"2027-04-{i % 28 + 1:02d}", "SS", "KT", 4, 4,
                    stadium="대구", away_sp=sp("SS", i), home_sp=sp("KT", i),
                    away_hr_allowed=1, home_hr_allowed=1) for i in range(40)]
    low = T.park_hr_factors(games, prior={"잠실": 0.6})
    high = T.park_hr_factors(games, prior={"잠실": 1.4})
    assert low["잠실"] < high["잠실"], (low, high)


def test_draw_is_its_own_label():
    games = _season()
    games.append(_game("2026-06-04", "LG", "OB", 4, 4, away_sp=sp("LG", 0), home_sp=sp("OB", 0)))
    rows = [r for r in T.build_rows(games) if r["date"] == "2026-06-04"]
    assert rows and all(r["y"] == 1 for r in rows), [r["y"] for r in rows]


def test_training_logs_filters_by_season():
    """2024 이전 로그가 학습에 섞이면 안 된다."""
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        try:
            os.chdir(d)
            for y in (2021, 2023, 2024, 2026):
                io.open(f"gamelog_{y}.json", "w", encoding="utf-8").write('{"games":[]}')
            got = sorted(T.training_logs())
            assert got == ["gamelog_2024.json", "gamelog_2026.json"], got
            assert sorted(T.training_logs(from_season=2021))[0] == "gamelog_2021.json"
        finally:
            os.chdir(cwd)


# ── 시즌 가중치 ────────────────────────────────────────────────────────

def test_recency_weights():
    import eval_window as E
    rows = [{"season": 2024}, {"season": 2025}, {"season": 2026}]
    assert E.weights(rows, 1.0) is None            # 1.0 이면 가중치를 안 쓴다
    w = list(E.weights(rows, 0.5))
    assert w == [0.25, 0.5, 1.0], w                # 최신이 1, 한 시즌 멀어지면 절반


# ── 상대전적 집계 ──────────────────────────────────────────────────────

def test_h2h_takes_hr_from_opponent_side():
    """LG 의 홈런은 '상대가 허용한 홈런' 이다. 뒤집으면 조용히 틀린다."""
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        try:
            os.chdir(d)
            # LG 원정, LG 3홈런(=home 쪽 hr_allowed), 두산 1홈런
            g = _game("2026-05-01", "LG", "OB", 6, 2,
                      away_hr_allowed=1, home_hr_allowed=3)
            io.open("gamelog_2026.json", "w", encoding="utf-8").write(
                json.dumps({"games": [g]}, ensure_ascii=False))
            os.makedirs("web", exist_ok=True)
            sys.argv = ["build_h2h_history.py"]
            H.main()
            d2 = json.load(io.open("web/h2h_history.json", encoding="utf-8"))
            ob = d2["bySeason"]["2026"]["OB"]
            assert ob["hr"] == 3.0, ob            # LG 가 친 홈런
            assert ob["hra"] == 1.0, ob           # LG 가 맞은 홈런
            assert (ob["w"], ob["l"], ob["d"]) == (1, 0, 0), ob
            assert ob["rs"] == 6.0 and ob["ra"] == 2.0, ob
        finally:
            os.chdir(cwd)


# ── 학습↔운영 일치 (이 프로젝트에서 가장 비싼 버그) ──────────────────────

def test_serving_features_match_training():
    """학습과 운영이 **같은 특징 값**을 만들어야 한다.

    이 프로젝트에서 가장 비싼 버그였다. 운영이 사전값(작년 성적)을 넘기지
    않아 시즌 초 투수 ERA 가 학습 7.86 / 운영 54.00 으로 들어갔고, 확률이
    49%p 어긋났다. 구장 팩터에서도 같은 일이 났다(11%p). 에러는 나지 않고
    예측만 틀린다.

    확률이 아니라 특징을 비교한다. 확률로 비교하려면 저장된 계수와 똑같은
    학습 집합으로 다시 적합해야 하는데, 그걸 틀리면 테스트가 거짓으로
    실패한다(실제로 그렇게 만들었다가 4.8%p 오탐이 났다).

    경기 로그가 없으면 건너뛴다 — 네트워크는 타지 않는다.
    """
    import os
    if not (os.path.exists("gamelog_2026.json")
            and os.path.exists("gamelog_2025.json")):
        return
    import outcome_infer as OI

    games = T.load("gamelog_2026.json")
    prior = T.state_through(T.load("gamelog_2025.json"))
    rows = T.build_rows(games, prior=prior)
    if len(rows) < 60:
        return
    by = {(r["date"], r["team"]): r for r in rows}

    # 운영 쪽이 실제로 부르는 인자 그대로 다시 만든다
    checked = worst = 0
    worst_key = None
    for x in sorted(games, key=lambda g: g.get("date", ""))[-30:]:
        pitchers = x.get("pitchers") or {}

        def starter(side):
            for p in (pitchers.get(side) or []):
                if p.get("started"):
                    return p
            return None

        for me, foe, is_home in (("home", "away", True), ("away", "home", False)):
            r = by.get((x.get("date"), x.get(me)))
            ms, os_ = starter(me), starter(foe)
            if not r or not ms or not os_:
                continue
            date = x.get("date")
            pr = OI.season_prior(date)
            parks = T.park_hr_factors(
                [g for g in games if g.get("date", "") < date],
                prior=(pr or {}).get("parks"))
            f = T.featurize(T.state_through(games, before=date),
                            x.get(me), x.get(foe), is_home,
                            x.get("stadium", ""), ms.get("pcode"),
                            os_.get("pcode"), date, strict=False,
                            prior=pr, parks=parks)
            if f is None:
                continue
            for k in T.FEATURES:
                d = abs(f[k] - r["feat"][T.FEATURES.index(k)])
                if d > worst:
                    worst, worst_key = d, (date, x.get(me), k, f[k],
                                           r["feat"][T.FEATURES.index(k)])
            checked += 1

    assert checked >= 10, f"비교한 경기가 {checked}건뿐이다"
    assert worst < 1e-6, (
        f"학습과 운영의 특징이 다르다: {worst_key}. "
        f"featurize 에 넘기는 인자(prior, parks)가 양쪽에서 같은지 확인할 것")


def test_probability_sources():
    """문항별 확률이 선언한 출처와 실제로 같은지 본다.

    이 프로젝트의 버그 대부분이 '같은 사실을 두 경로로 만든' 탓이었다.
    시뮬레이션과 학습 모델이 나란히 돌면서 이음매마다 값이 갈렸다(49%p).
    PROB_SOURCE 표가 규칙이고, 이 테스트가 그 규칙을 강제한다.

    확률만 몰래 갈아끼우고 표를 안 고치면 여기서 걸린다.
    """
    import os
    if not os.path.exists("gamelog_2026.json"):
        return
    import starball_predictor as S

    # 선언된 문항이 실제 문항 목록과 맞는가
    keys = {q["key"] for q in S.STARBALL_QUESTIONS}
    assert set(S.PROB_SOURCE) == keys, (set(S.PROB_SOURCE), keys)

    # 득실 차는 실측 분포와 정확히 같아야 한다
    base = S.BASE_RATES.get("margin")
    assert base, "base_rates.json 의 margin 이 비었다"
    for k, v in S.PROB_SOURCE.items():
        assert v in ("learned", "empirical", "simulation"), (k, v)

    # 학습 출처로 선언한 문항에는 계수 파일이 있어야 한다
    import outcome_infer as OI
    if S.PROB_SOURCE["outcome"] == "learned":
        assert OI.load_model(), "승패를 학습 출처로 선언했는데 계수 파일이 없다"
    if S.PROB_SOURCE["lg_hr"] == "learned":
        assert OI.load_hr_model(), "홈런을 학습 출처로 선언했는데 계수 파일이 없다"


# ── 러너 ──────────────────────────────────────────────────────────────

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
            print(f"  FAIL  {name}{chr(10)}          {type(e).__name__}: {e}")
    print(f"{chr(10)}{len(tests) - len(failed)}/{len(tests)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
