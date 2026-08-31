#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""예측 모델을 과거 경기로 검증한다.

    python build_gamelog.py            # 먼저 경기 로그를 만든다
    python backtest.py                 # 전 구단
    python backtest.py --team LG       # LG 경기만
    python backtest.py --from 2026-06-01

핵심은 시점 고정(point-in-time)이다. D일 경기를 예측할 때 D일 이전 경기만
누적해서 팀·투수 성적을 만든다. 시즌 최종 성적을 쓰면 미래를 훔쳐보는
꼴이라 적중률이 부풀려진다.

측정 항목:
  적중률   1위 선택지가 실제로 맞았는가. 기준선(홈팀 항상 선택)과 비교한다.
  브라이어  확률의 품질. 낮을수록 좋다. 0.25 = 아무 정보 없이 반반 찍은 수준.
  캘리브레이션  "60%" 라고 말한 경기들이 실제로 60% 일어났는가.
           적중률이 높아도 캘리브레이션이 깨지면 확률값은 못 믿는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date

import starball_predictor as S

WARMUP_GAMES = 20      # 팀당 이만큼 쌓이기 전에는 예측하지 않는다


# ─────────────────────────────────────────────────────────────────────────
# 시점별 상태
# ─────────────────────────────────────────────────────────────────────────

class SeasonState:
    """경기를 시간순으로 먹여가며 그 시점까지의 누적 성적을 들고 있는다."""

    def __init__(self):
        self.team = defaultdict(lambda: defaultdict(float))
        self.pitcher = defaultdict(lambda: defaultdict(float))
        self.pitcher_log = defaultdict(list)     # pcode → 최신순 등판
        self.h2h = defaultdict(list)             # (a,b) 정렬 튜플 → 경기들
        self.team_games = defaultdict(list)      # 팀 → 최근 경기 (득/실점)

    def feed(self, g: dict) -> None:
        h, a = g["home"], g["away"]
        hs, as_ = g["home_score"], g["away_score"]
        box = g["box"]

        for me, foe, my_s, foe_s, my_box, foe_box in (
                (h, a, hs, as_, box["home"], box["away"]),
                (a, h, as_, hs, box["away"], box["home"])):
            t = self.team[me]
            t["g"] += 1
            t["rs"] += my_s
            t["ra"] += foe_s
            # 투수 기준 기록이므로 내 타선의 산출은 상대 배터리 기록에서 온다
            t["hr"] += foe_box["hr_allowed"]
            t["hit"] += foe_box["hit_allowed"]
            t["kk"] += foe_box["k_thrown"]        # 내 타자가 당한 삼진
            t["hr_allowed"] += my_box["hr_allowed"]
            t["hit_allowed"] += my_box["hit_allowed"]
            t["k_thrown"] += my_box["k_thrown"]
            t["er_allowed"] += my_box["er_allowed"]
            self.team_games[me].append({"rs": my_s, "ra": foe_s})

        self.h2h[tuple(sorted((h, a)))].append(g)

        for key in ("home", "away"):
            for p in g["pitchers"][key]:
                pc = p["pcode"]
                if not pc:
                    continue
                d = self.pitcher[pc]
                d["ip"] += p["ip"]
                d["er"] += p["er"]
                d["hit"] += p["hit"]
                d["bb"] += p["bb"]
                d["hr"] += p["hr"]
                d["g"] += 1
                if p["started"]:
                    d["gs"] += 1
                else:
                    # 불펜만 따로 누적한다. 팀 평균을 쓰면 선발 성적이 섞인다.
                    b = self.team[g["home"] if key == "home" else g["away"]]
                    b["pen_ip"] += p["ip"]
                    b["pen_r"] += p.get("r", p["er"])
                self.pitcher_log[pc].insert(0, dict(p, date=g["date"]))

    # ── 조회 ──────────────────────────────────────────────────────────
    def ready(self, *teams) -> bool:
        return all(self.team[t]["g"] >= WARMUP_GAMES for t in teams)

    def league(self) -> dict:
        g = sum(t["g"] for t in self.team.values())
        if not g:
            return {}
        agg = {k: sum(t[k] for t in self.team.values()) / g
               for k in ("rs", "hr", "hit", "kk")}
        # 팀당 경기당 자책점. 한 팀이 경기당 약 9이닝을 던지므로 ERA 근사가 된다
        # (연장, 홈팀 8회 종료는 무시 — 백테스트 기준선으로는 충분하다).
        er = sum(t["er_allowed"] for t in self.team.values())
        agg["era"] = er / g
        return agg


# ─────────────────────────────────────────────────────────────────────────
# 시점 상태 → GameContext
# ─────────────────────────────────────────────────────────────────────────

def make_starter(state: SeasonState, p: dict, league_era: float) -> S.Starter:
    """그 경기 실제 선발의, 그 경기 직전까지의 성적으로 Starter 를 만든다."""
    pc = p["pcode"]
    d = state.pitcher.get(pc)
    sp = S.Starter(name=p["name"], pcode=pc, announced=True)
    if not d or d["ip"] <= 0:
        sp.season_era = league_era
        return sp
    sp.season_ip = d["ip"]
    sp.season_games = int(d["g"])
    sp.season_hr = int(d["hr"])
    sp.season_era = d["er"] * 9.0 / d["ip"]
    sp.season_whip = (d["hit"] + d["bb"]) / d["ip"]

    for log in state.pitcher_log[pc][:5]:
        sp.recent_ip += log["ip"]
        sp.recent_er += log["er"]
        sp.recent_hit += log["hit"]
        sp.recent_bb += log["bb"]
        sp.recent_starts += 1
        sp.recent_log.append({"date": log["date"], "ip": log["ip"],
                              "er": log["er"], "started": log["started"]})
    return sp


def make_context(state: SeasonState, g: dict) -> S.GameContext:
    lg = state.league()
    league_rpg = lg.get("rs") or S.LEAGUE_RPG_FALLBACK
    league_era = lg.get("era") or S.LEAGUE_ERA_FALLBACK
    league_counts = {"hr": lg.get("hr") or S.COUNT_STATS["hr"]["fallback"],
                     "hit": lg.get("hit") or S.COUNT_STATS["hit"]["fallback"],
                     "k": lg.get("kk") or S.COUNT_STATS["k"]["fallback"]}

    def side(code: str, is_home: bool) -> S.TeamSide:
        t = state.team[code]
        n = t["g"] or 1
        s = S.TeamSide(code=code, name=S.TEAM_NAMES.get(code, code),
                       is_home=is_home)
        s.rs_per_game = t["rs"] / n
        s.ra_per_game = t["ra"] / n
        s.bullpen_ra9 = (t["pen_r"] * 9.0 / t["pen_ip"]
                         if t["pen_ip"] >= 50 else s.ra_per_game)
        s.off_rate = {"hr": t["hr"] / n, "hit": t["hit"] / n, "k": t["kk"] / n}
        s.def_rate = {"hr": t["hr_allowed"] / n, "hit": t["hit_allowed"] / n,
                      "k": t["k_thrown"] / n}
        recent = state.team_games[code][-5:]
        if recent:
            s.recent_rs = sum(r["rs"] for r in recent) / len(recent)
            s.recent_ra = sum(r["ra"] for r in recent) / len(recent)
            s.recent_games = len(recent)

        past = state.h2h.get(tuple(sorted((g["home"], g["away"])))) or []
        if past:
            mine = [(x["home_score"] if x["home"] == code else x["away_score"])
                    for x in past]
            foe = [(x["away_score"] if x["home"] == code else x["home_score"])
                   for x in past]
            s.h2h_rs = sum(mine) / len(mine)
            s.h2h_ra = sum(foe) / len(foe)
            s.h2h_games = len(past)
            s.h2h_w = sum(1 for m, f in zip(mine, foe) if m > f)
            s.h2h_l = sum(1 for m, f in zip(mine, foe) if m < f)
            s.h2h_d = sum(1 for m, f in zip(mine, foe) if m == f)

        starters = [p for p in g["pitchers"][
            "home" if is_home else "away"] if p["started"]]
        if starters:
            s.starter = make_starter(state, starters[0], league_era)
        return s

    return S.GameContext(
        game_id=g["gameId"], game_date=g["date"], start_time="",
        stadium=g["stadium"],
        home=side(g["home"], True), away=side(g["away"], False),
        league_rpg=league_rpg, league_era=league_era,
        league_counts=league_counts, warnings=[])


# ─────────────────────────────────────────────────────────────────────────
# 실제 결과 → 문항별 정답
# ─────────────────────────────────────────────────────────────────────────

def actual_answers(g: dict, my_team: str) -> dict:
    """MY_TEAM 관점의 실제 결과를 문항 라벨로 환산한다."""
    is_home = g["home"] == my_team
    me = g["home_score"] if is_home else g["away_score"]
    foe = g["away_score"] if is_home else g["home_score"]
    box, pit = g["box"], g["pitchers"]

    my_key = "home" if is_home else "away"
    foe_key = "away" if is_home else "home"

    total_hr = box["home"]["hr_allowed"] + box["away"]["hr_allowed"]
    my_hr = box[foe_key]["hr_allowed"]          # 내 타선이 친 홈런
    total_hit = box["home"]["hit_allowed"] + box["away"]["hit_allowed"]
    total_k = box["home"]["k_thrown"] + box["away"]["k_thrown"]

    def starter_er(key: str):
        st = [p for p in pit[key] if p["started"]]
        return st[0]["er"] if st else None

    def bucket(value, buckets):
        if value is None:
            return None
        for label, lo, hi in buckets:
            if (lo is None or value >= lo) and (hi is None or value <= hi):
                return label
        return None

    qs = {q["key"]: q for q in S.STARBALL_QUESTIONS}
    value = {
        "outcome": None,
        "margin_abs": abs(me - foe),
        "total_hr": total_hr,
        "lg_hr": my_hr,
        "lg_sp_er": starter_er(my_key),
        "opp_sp_er": starter_er(foe_key),
        "total_runs": me + foe,
        "total_hit": total_hit,
        "total_k": total_k,
    }
    out = {}
    for q in qs.values():
        if q.get("categorical"):
            out[q["key"]] = "승" if me > foe else ("패" if me < foe else "무")
        else:
            out[q["key"]] = bucket(value.get(q["source"]), q["buckets"])
    return out


# ─────────────────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────────────────

def run(games: list, team: str = None, start: date = None,
        n_sim: int = 8000) -> dict:
    state = SeasonState()
    hits = defaultdict(lambda: [0, 0])        # key → [맞음, 전체]
    brier = defaultdict(list)
    calib = defaultdict(lambda: [0, 0])       # 확률 구간 → [실제 발생, 전체]
    home_base = [0, 0]
    truth_counts = defaultdict(lambda: defaultdict(int))   # 가장 흔한 값 기준선용
    combo_hits = {"best": [0, 0], "greedy": [0, 0]}
    truth_combo = defaultdict(int)
    seen = defaultdict(lambda: defaultdict(int))   # 시점별 문항 기저
    seen_combo = defaultdict(int)                  # 시점별 조합 기저
    raw = []                                               # 보정 적합용
    n_pred = 0

    for g in games:
        gd = date.fromisoformat(g["date"])
        target = (team in (g["home"], g["away"])) if team else True
        if target and state.ready(g["home"], g["away"]) and \
                (start is None or gd >= start):
            my_team = team or g["home"]
            old = S.MY_TEAM
            try:
                S.MY_TEAM = my_team
                ctx = make_context(state, g)
                # 그 시점까지 관측된 결과 분포. 미래를 안 쓴다.
                base = {k: {lbl: c / sum(v.values()) for lbl, c in v.items()}
                        for k, v in seen.items() if sum(v.values()) >= 30}
                nc = sum(seen_combo.values())
                bc = ({k: v / nc for k, v in seen_combo.items()}
                      if nc >= 30 else None)
                pred = S.predict(ctx, n_sim=n_sim, base_rates=base,
                                 base_combo=bc)
                picks = S.to_starball_choices(pred)
            finally:
                S.MY_TEAM = old

            truth = actual_answers(g, my_team)
            n_pred += 1

            # 스타볼은 미션을 '모두' 맞혀야 지급된다. 그게 진짜 지표다.
            if pred.combo:
                keys = pred.combo["keys"]
                for mode in ("best", "greedy"):
                    ok = all(pred.combo[mode].get(k) == truth.get(k)
                             for k in keys)
                    combo_hits[mode][0] += ok
                    combo_hits[mode][1] += 1
                truth_combo[tuple(truth.get(k) for k in keys)] += 1

            for p in picks:
                real = truth.get(p["key"])
                if real is None:
                    continue
                hit = (p["pick"] == real)
                hits[p["key"]][0] += hit
                hits[p["key"]][1] += 1
                # 브라이어: 모든 선택지에 대해 (확률 - 실제)^2 합
                probs = dict(p["all"])
                brier[p["key"]].append(
                    sum((v - (1.0 if lbl == real else 0.0)) ** 2
                        for lbl, v in probs.items()))
                b = min(int(p["prob"] * 10), 9)
                calib[b][0] += hit
                calib[b][1] += 1
                truth_counts[p["key"]][real] += 1
                raw.append((probs, real))

            # 기준선: 항상 홈팀 승리
            if truth["outcome"] != "무":
                home_base[0] += (truth["outcome"] == "승") == (g["home"] == my_team)
                home_base[1] += 1

        _t = actual_answers(g, g["home"])
        for k, v in _t.items():
            if v is not None:
                seen[k][v] += 1
        _keys = [q["key"] for q in S.STARBALL_QUESTIONS]
        if all(_t.get(k) is not None for k in _keys):
            seen_combo[tuple(_t[k] for k in _keys)] += 1
        state.feed(g)

    return {"n": n_pred, "hits": dict(hits),
            "brier": {k: sum(v) / len(v) for k, v in brier.items() if v},
            "calib": dict(calib), "home_base": home_base,
            "truth_counts": {k: dict(v) for k, v in truth_counts.items()},
            "combo_hits": combo_hits, "truth_combo": dict(truth_combo),
            "raw": raw}


def fit_calibration(raw: list) -> tuple:
    """확률을 균등분포 쪽으로 얼마나 당겨야 하는지 적합한다.

    모델이 과신하면(60%라 했는데 실제 52%) 확률값 자체를 못 믿는다.
    p' = u + (p - u) · λ  로 눌러서 브라이어를 최소화하는 λ 를 찾는다.
    u 는 그 문항의 균등확률(선택지 수의 역수)이다.
    """
    best = (1.0, None)
    for i in range(0, 61):
        lam = 0.40 + i * 0.01
        total = 0.0
        for probs, real in raw:
            u = 1.0 / len(probs)
            total += sum((u + (v - u) * lam - (1.0 if lbl == real else 0.0)) ** 2
                         for lbl, v in probs.items())
        score = total / len(raw)
        if best[1] is None or score < best[1]:
            best = (round(lam, 2), score)
    base = sum(sum((v - (1.0 if lbl == real else 0.0)) ** 2
                   for lbl, v in probs.items())
               for probs, real in raw) / len(raw)
    return best[0], best[1], base


def report(res: dict) -> None:
    labels = {q["key"]: q["label"] for q in S.STARBALL_QUESTIONS}
    nopt = {q["key"]: len(q.get("buckets") or q.get("categorical") or [])
            for q in S.STARBALL_QUESTIONS}

    print(f"\n예측한 경기: {res['n']}건")
    if not res["n"]:
        print("표본이 없습니다. --from 을 앞당기거나 경기 로그를 확인하세요.")
        return

    print("\n기준선은 '항상 가장 흔한 선택지 찍기'다. 1/n 은 실제 분포가 고르지 않아")
    print("모델을 과대평가한다(합계 홈런의 1~2개는 원래 절반 가까이 나온다).")
    print("─" * 76)
    print(f"{'문항':<16}{'적중률':>9}{'흔한값':>10}{'개선':>9}"
          f"{'브라이어':>11}{'표본':>7}")
    print("─" * 76)
    useless = []
    for key, (hit, tot) in res["hits"].items():
        if not tot:
            continue
        acc = hit / tot
        counts = res["truth_counts"].get(key, {})
        base = max(counts.values()) / tot if counts else 1.0 / nopt[key]
        gain = acc - base
        mark = ""
        if gain < 0.02:
            mark = "  ← 정보 없음"
            useless.append(labels[key])
        print(f"{labels[key]:<16}{acc*100:>8.1f}%{base*100:>9.1f}%"
              f"{gain*100:>+8.1f}p{res['brier'].get(key, 0):>11.4f}"
              f"{tot:>7}{mark}")
    print("─" * 76)
    if useless:
        print(f"\n가장 흔한 선택지를 찍는 것보다 나을 게 없는 문항: "
              f"{', '.join(useless)}")

    ch = res.get("combo_hits") or {}
    if ch.get("best", [0, 0])[1]:
        print("\n" + "═" * 74)
        print("스타볼 지급 조건 — 미션을 '모두' 맞힌 비율 (이게 진짜 지표다)")
        print("═" * 74)
        tot = ch["best"][1]
        tc = res.get("truth_combo") or {}
        best_fixed = max(tc.values()) / tot if tc else 0.0
        for mode, label in (("best", "조합 최적화 (P(전부 적중) 최대)"),
                            ("greedy", "문항별 1위 따로 고르기")):
            hit, n = ch[mode]
            print(f"  {label:<34}{hit / n * 100:>7.2f}%   ({hit}/{n})")
        print(f"  {'항상 같은 조합 (가장 흔한 값, 사후적)':<34}{best_fixed * 100:>7.2f}%")
        import math
        se = math.sqrt(0.1 * 0.9 / tot) * 100
        print(f"\n  표본 {tot}건 · 표준오차 ≈ ±{se:.2f}%p")
        exp_games = 7 / (ch["best"][0] / tot) if ch["best"][0] else float("inf")
        print(f"  스타볼 7개(경품 응모선)를 모으려면 약 {exp_games:.0f}경기 필요")

    hb, hbn = res["home_base"]
    if hbn:
        print(f"\n승패 기준선 — 항상 홈팀 승리 선택: {hb / hbn * 100:.1f}% ({hbn}건)")

    if res.get("raw"):
        lam, better, base_brier = fit_calibration(res["raw"])
        print(f"\n확률 보정 계수 λ = {lam}"
              f"   (브라이어 {base_brier:.4f} → {better:.4f})")
        if lam < 0.95:
            print(f"  λ<1 은 모델이 과신한다는 뜻이다. 확률을 균등분포 쪽으로")
            print(f"  {(1 - lam) * 100:.0f}% 눌러야 실제 발생률과 맞는다.")
            print(f"  starball_predictor.PROB_CALIBRATION = {lam} 로 반영한다.")

    print("\n캘리브레이션 (모델이 말한 확률 vs 실제 발생률)")
    print(f"  {'구간':<12}{'예측':>8}{'실제':>8}{'차이':>9}{'표본':>7}")
    for b in sorted(res["calib"]):
        hit, tot = res["calib"][b]
        if tot < 20:
            continue
        mid = b * 10 + 5
        act = hit / tot * 100
        print(f"  {b*10}~{b*10+9}%{'':<5}{mid:>7}%{act:>7.1f}%"
              f"{act-mid:>+8.1f}p{tot:>7}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="예측 모델 과거 검증")
    ap.add_argument("--log", default=None, help="경기 로그 파일")
    ap.add_argument("--team", default=None, help="이 팀 경기만 (예: LG)")
    ap.add_argument("--from", dest="start", default=None, help="시작일")
    ap.add_argument("--sims", type=int, default=8000)
    args = ap.parse_args(argv)

    path = args.log or f"gamelog_{S.today_kst().year}.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"{path} 가 없습니다. 먼저 실행하세요:\n"
              f"  python build_gamelog.py", file=sys.stderr)
        return 1

    games = sorted(data["games"], key=lambda g: (g["date"], g["gameId"]))
    print(f"경기 로그: {path} · {len(games)}건 "
          f"({games[0]['date']} ~ {games[-1]['date']})")
    start = date.fromisoformat(args.start) if args.start else None
    report(run(games, team=args.team, start=start, n_sim=args.sims))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
