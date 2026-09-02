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


def test_park_factor_enters_features():
    games = _season()
    games.append(_game("2026-06-03", "LG", "NC", 3, 3, stadium="창원",
                       away_sp=sp("LG", 0), home_sp=sp("NC", 0)))
    games.append(_game("2026-06-03", "SS", "KT", 3, 3, stadium="잠실",
                       away_sp=sp("SS", 0), home_sp=sp("KT", 0)))
    rows = T.build_rows(games)
    i = T.FEATURES.index("park")
    parks = {r["team"]: r["feat"][i] for r in rows if r["date"] == "2026-06-03"}
    assert "LG" in parks and "SS" in parks, parks
    assert parks["LG"] > 1.3, parks                  # 창원 1.421
    assert parks["SS"] < 0.7, parks                  # 잠실 0.665


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
