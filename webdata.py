#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PWA 가 읽을 JSON 을 만들고, 결과를 정산해 기록에 쌓는다.

    python webdata.py predict          # 오늘 예측 → web/today.json, 기록에 등재
    python webdata.py settle           # 끝난 경기 채점 → web/history.json
    python webdata.py both             # 위 둘을 순서대로 (깃헙 액션이 쓰는 것)
    python webdata.py backfill 12      # 지난 경기 시점 고정 재현 (시연·검증용)

파일 두 개만 만든다. 웹앱은 이 둘만 읽는다.
  web/today.json    오늘 무엇을 입력할지 + 그 근거
  web/history.json  지난 추천과 실제 결과, 누적 성적, 스타볼 개수

정산(settle)이 이 도구의 새로운 부분이다. 예측만 하고 결과를 안 보면
'실제로 맞았는지'를 영영 모른다 — 백테스트 수치는 과거 재현이지 실사용 성적이
아니다. 경기가 끝나면 박스스코어로 채점해서 기록에 남긴다.
"""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import date, datetime

import backtest as B
import starball_predictor as S

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
TODAY_FILE = os.path.join(WEB_DIR, "today.json")
HISTORY_FILE = os.path.join(WEB_DIR, "history.json")

STARBALL_GOAL = 7          # 경품 응모선


# ─────────────────────────────────────────────────────────────────────────
# 입출력
# ─────────────────────────────────────────────────────────────────────────

def _read(path: str, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _write(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _history() -> dict:
    return _read(HISTORY_FILE, {"games": []})


# ─────────────────────────────────────────────────────────────────────────
# 예측 → today.json
# ─────────────────────────────────────────────────────────────────────────

def apply_outcome_model(today: dict, ctx: "S.GameContext") -> None:
    """승패 문항을 학습 모델 값으로 갈아끼운다.

    세 문항 중 데이터가 통하는 건 승패뿐이다(득실 차·홈런은 어떤 조건에서도
    최빈값이 안 바뀐다). 그래서 승패만 학습 모델을 쓰고 나머지는 그대로 둔다.

    계수 파일이 없거나 특징을 못 만들면 아무것도 하지 않는다 — 옛 모델 값이
    그대로 남는다. 조용히 틀린 값을 내는 것보다 기능이 꺼지는 게 낫다.
    """
    try:
        import outcome_infer as OI
    except ImportError:
        return
    model = OI.load_model()
    if not model:
        print("outcome_model.json 없음 — 승패는 옛 모델 값을 쓴다", file=sys.stderr)
        return

    try:
        year = str(ctx.game_date)[:4]
        games = OI.T.load(f"gamelog_{year}.json")
    except FileNotFoundError:
        print("경기 로그가 없어 승패 학습 모델을 건너뛴다", file=sys.stderr)
        return

    lg, opp = ctx.lg, ctx.opp
    sp = lambda side: getattr(getattr(side, "starter", None), "pcode", None)
    out = OI.predict_outcome(
        model, games, lg.code, opp.code, bool(getattr(ctx.home, "code", "") == lg.code),
        ctx.stadium, sp(lg), sp(opp), str(ctx.game_date))
    if not out:
        print("승패 특징을 만들 수 없어 옛 모델 값을 쓴다", file=sys.stderr)
        return

    m = next((x for x in today.get("missions") or [] if x.get("key") == "outcome"),
             None)
    if not m:
        return

    old_pick, old_prob = m.get("pick"), m.get("prob") or 0.0
    labels = [o["label"] for o in m.get("options") or []]
    probs = {k: out[k] for k in labels if k in out}
    if len(probs) != len(labels) or not probs:
        return

    pick = max(probs, key=probs.get)
    m["options"] = [{"label": k, "prob": round(probs[k], 4)} for k in labels]
    m["pick"] = pick
    m["prob"] = round(probs[pick], 4)
    m["confidence"] = out.get("confidence")
    m["tierAccuracy"] = out.get("tierAccuracy")
    # 목표 구간(70/75/80%)에 드는 날만 표시한다. 없으면 키를 안 넣는다.
    if out.get("band"):
        m["band"] = out["band"]
        m["bandAccuracy"] = out["bandAccuracy"]
    m["model"] = "learned"

    # 결합 확률은 승패 확률이 바뀐 만큼만 비례로 고친다. 시뮬레이션을 다시
    # 돌리지 않으므로 정확한 재계산은 아니다 — 그래서 근사임을 표시한다.
    j = today.get("joint") or {}
    if old_prob > 0 and j.get("prob"):
        j["prob"] = round(j["prob"] * m["prob"] / old_prob, 4)
        j["outcomeModel"] = "learned"
    today["outcomeSkill"] = {
        "overall": (model.get("confidence") or {}).get("overall"),
        "validation": model.get("validation"),
    }
    if pick != old_pick:
        print(f"승패 추천이 바뀜: {old_pick} → {pick} "
              f"({old_prob * 100:.1f}% → {m['prob'] * 100:.1f}%)", file=sys.stderr)


def build_today(ctx: S.GameContext, pred: S.Prediction, picks: list) -> dict:
    """화면이 필요로 하는 것만 담는다. 모델 내부값은 내보내지 않는다."""
    lg, opp = ctx.lg, ctx.opp
    by_key = {p["key"]: p for p in picks}
    best = (pred.combo or {}).get("best") or {}

    missions = []
    for i, q in enumerate(S.STARBALL_QUESTIONS, 1):
        k = q["key"]
        p = by_key.get(k)
        if not p or k not in best:
            continue
        sk = S.QUESTION_SKILL.get(k) or {}
        missions.append({
            "n": i, "key": k, "label": q["label"],
            "pick": best[k],
            # 이 문항을 얼마나 믿을지. 화면이 그대로 보여준다.
            "skill": {"hit": sk.get("hit"), "base": sk.get("base"),
                      "gain": sk.get("gain")} if sk else None,
            # 조합 최적해의 확률을 보여준다. 문항별 1위와 다를 수 있고,
            # 그 경우 화면에 나가야 하는 값은 조합 쪽이다.
            "prob": round(pred.probs.get(k, {}).get(best[k], 0.0), 4),
            "options": [{"label": lbl, "prob": round(v, 4)}
                        for lbl, v in p["all"]],
        })

    sp_lg, sp_opp = lg.starter, opp.starter

    def starter(sp: S.Starter, foe_name: str) -> dict:
        d = {"name": sp.name, "era": round(sp.season_era, 2),
             "whip": round(sp.season_whip, 2),
             "ip": round(sp.season_ip, 1), "games": sp.season_games,
             "spot": sp.looks_like_reliever}
        if sp.vs_era is not None:
            d["vs"] = {"foe": foe_name, "era": round(sp.vs_era, 2),
                       "ip": round(sp.vs_ip, 1), "games": sp.vs_games}
        if sp.recent_era is not None:
            d["recent"] = {"era": round(sp.recent_era, 2),
                           "whip": round(sp.recent_whip, 2),
                           "ip": round(sp.recent_ip, 1),
                           "starts": sp.recent_starts,
                           "asStarter": sum(1 for g in sp.recent_log
                                            if g.get("started"))}
        return d

    park_raw = S.PARK_HR_FACTOR.get(ctx.stadium)
    # 학습 모델이 붙은 문항에 확신도·목표 구간을 실어 보낸다.
    # 이게 없으면 화면에서 '오늘은 믿을 만한 날인가' 를 알 수 없다.
    lr = getattr(pred, "learned", None) or {}
    meta = lr.get("meta") or {}
    for m in missions:
        mm = meta.get(m["key"]) or {}
        if m["key"] in (lr.get("applied") or []):
            m["model"] = "learned"
        if mm.get("reasons"):
            m["reasons"] = mm["reasons"]
        if mm.get("blend"):
            m["blend"] = mm["blend"]
        if m["key"] == "outcome":
            for k in ("confidence", "tierAccuracy", "band", "bandAccuracy"):
                if mm.get(k) is not None:
                    m[k] = mm[k]

    # 득실 차는 학습을 안 붙인다. 그 이유도 화면에 밝힌다 — 근거 없이
    # 매번 같은 값을 내면 사용자는 고장이라고 생각한다.
    for m in missions:
        if m["key"] == "margin" and not m.get("reasons"):
            m["fixedNote"] = ("어떤 매치업이든 1점차가 가장 흔합니다. "
                              "실제 기록 1,172경기에서 23%로 1위라, "
                              "이 문항은 매치업으로 바뀌지 않습니다.")

    combo = pred.combo or {}
    hist = _history()

    model_info = None
    try:
        import outcome_infer as OI
        om = OI.load_model()
        if om and (lr.get("applied")):
            model_info = {"overall": (om.get("confidence") or {}).get("overall"),
                          "validation": om.get("validation")}
    except Exception:
        model_info = None

    return {
        "generated": datetime.now(S.KST).isoformat(timespec="seconds"),
        "outcomeSkill": model_info,
        "game": {
            "date": ctx.game_date, "time": ctx.start_time,
            "stadium": ctx.stadium,
            "lg": lg.name, "opp": opp.name, "lgIsHome": lg.is_home,
        },
        "missions": missions,
        "joint": {
            "prob": round(combo.get("best_prob", 0.0), 4),
            # 리그에서 가장 흔한 조합의 확률. 모델을 이 값과 나란히 보여줘야
            # 사용자가 '모델이 더 나은가'를 스스로 판단할 수 있다.
            "baseline": round(max(S.BASE_COMBO.values()), 4)
            if S.BASE_COMBO else None,
            "confidence": pred.confidence,
        },
        "score": {"lg": round(pred.exp["lg_runs"], 1),
                  "opp": round(pred.exp["opp_runs"], 1),
                  "modal": list(pred.modal_score)},
        "reasoning": {
            "starters": {"lg": starter(sp_lg, opp.name),
                         "opp": starter(sp_opp, lg.name)},
            "park": {"name": ctx.stadium,
                     "raw": park_raw,
                     "applied": round(S.park_hr_factor(ctx.stadium), 3)}
            if park_raw is not None else None,
            "bullpen": {"lg": S.BULLPEN_RA9.get(lg.code),
                        "opp": S.BULLPEN_RA9.get(opp.code)},
            "form": {"lg": lg.recent_form, "opp": opp.recent_form},
            "h2h": {"w": lg.h2h_w, "l": lg.h2h_l, "d": lg.h2h_d,
                    "games": lg.h2h_games},
        },
        "warnings": ctx.warnings,
        "starballs": sum(1 for g in hist["games"] if g.get("earned")),
        "goal": STARBALL_GOAL,
    }


def record_prediction(today: dict, pred: "S.Prediction" = None) -> None:
    """오늘 추천을 기록에 등재한다(결과는 나중에 정산이 채운다).

    추천값만 남기면 나중에 '왜 틀렸는지'를 못 캔다. 모델이 각 선택지에
    부여한 확률까지 통째로 남겨서, 다음 시즌에 캘리브레이션을 다시 재고
    가중치를 조정할 재료로 쓴다. 한 경기당 1KB 남짓이라 부담이 없다.
    """
    hist = _history()
    gid = today["game"]["date"]
    if any(g["date"] == gid for g in hist["games"]):
        return                      # 같은 날 두 번 돌아도 중복 등재 안 함
    row = {
        "date": gid,
        "opp": today["game"]["opp"],
        "home": today["game"]["lgIsHome"],
        "picks": {m["key"]: m["pick"] for m in today["missions"]},
        "settled": False,
        # ── 내년 분석용 (화면은 안 씀) ──
        "belief": {m["key"]: {o["label"]: o["prob"] for o in m["options"]}
                   for m in today["missions"]},
        "jointProb": today["joint"]["prob"],
        "baseline": today["joint"]["baseline"],
        "expScore": today["score"],
        "starters": {k: {"name": v["name"], "era": v["era"],
                         "spot": v.get("spot", False)}
                     for k, v in today["reasoning"]["starters"].items()},
        "params": {"blend": S.BASE_RATE_BLEND,
                   "calib": S.PROB_CALIBRATION,
                   "version": today["generated"][:10]},
    }
    if pred is not None and pred.combo:
        row["greedy"] = pred.combo.get("greedy")
        row["greedyProb"] = round(pred.combo.get("greedy_prob", 0.0), 4)
    hist["games"].append(row)
    hist["games"].sort(key=lambda g: g["date"], reverse=True)
    _write(HISTORY_FILE, _summarize(hist))


# ─────────────────────────────────────────────────────────────────────────
# 정산 → history.json
# ─────────────────────────────────────────────────────────────────────────

def settle(client: S.NaverKBO, limit: int = 30) -> int:
    """미정산 경기를 박스스코어로 채점한다. 채점한 건수를 돌려준다."""
    hist = _history()
    pending = [g for g in hist["games"] if not g.get("settled")]
    if not pending:
        return 0

    today = S.today_kst()
    done = 0
    for g in pending[:limit]:
        day = date.fromisoformat(g["date"])
        if day >= today:
            continue                # 아직 안 끝난 경기
        gi = client.find_team_game(day, S.MY_TEAM)
        if not gi or gi.get("statusCode") != "RESULT":
            continue
        try:
            game = client.game(gi["gameId"])
            rd = client.record(gi["gameId"])
        except Exception:
            continue
        box = (rd or {}).get("teamPitchingBoxscore") or {}
        pit = (rd or {}).get("pitchersBoxscore") or {}
        if not box.get("home") or not box.get("away"):
            continue

        # backtest 의 채점 로직을 그대로 쓴다. 두 곳에 같은 규칙을 두면
        # 반드시 어긋나므로 한 곳만 유지한다.
        payload = {
            "date": g["date"], "gameId": gi["gameId"],
            "home": game["homeTeamCode"], "away": game["awayTeamCode"],
            "home_score": int(S.fnum(game.get("homeTeamScore"))),
            "away_score": int(S.fnum(game.get("awayTeamScore"))),
            "box": {side: {"hr_allowed": S.fnum((box.get(side) or {}).get("hr")),
                           "hit_allowed": S.fnum((box.get(side) or {}).get("hit")),
                           "k_thrown": S.fnum((box.get(side) or {}).get("kk")),
                           "r_allowed": S.fnum((box.get(side) or {}).get("r")),
                           "er_allowed": S.fnum((box.get(side) or {}).get("er"))}
                    for side in ("home", "away")},
            "pitchers": {side: [{"pcode": str(p.get("pcode") or ""),
                                 "started": i == 0,
                                 "ip": S.parse_kbo_innings(p.get("inn")),
                                 "er": int(S.fnum(p.get("er"))),
                                 "r": int(S.fnum(p.get("r"))),
                                 "hit": int(S.fnum(p.get("hit"))),
                                 "bb": int(S.fnum(p.get("bb"))),
                                 "hr": int(S.fnum(p.get("hr")))}
                                for i, p in enumerate(pit.get(side) or [])]
                         for side in ("home", "away")},
        }
        actual = B.actual_answers(payload, S.MY_TEAM)
        hits = {k: (actual.get(k) == v) for k, v in g["picks"].items()}

        lg_home = game["homeTeamCode"] == S.MY_TEAM
        g.update({
            "settled": True,
            "actual": {k: actual.get(k) for k in g["picks"]},
            "hits": hits,
            "earned": all(hits.values()),
            "score": {"lg": payload["home_score"] if lg_home else payload["away_score"],
                      "opp": payload["away_score"] if lg_home else payload["home_score"]},
        })
        done += 1

    _write(HISTORY_FILE, _summarize(hist))
    return done


def _summarize(hist: dict) -> dict:
    """누적 지표를 다시 계산한다. 화면이 계산하지 않도록 여기서 끝낸다."""
    games = hist["games"]
    settled = [g for g in games if g.get("settled")]
    earned = [g for g in settled if g.get("earned")]
    replayed = sum(1 for g in settled if g.get("replayed"))
    per = {}
    for q in S.STARBALL_QUESTIONS:
        k = q["key"]
        rows = [g for g in settled if k in (g.get("hits") or {})]
        if rows:
            per[k] = {"label": q["label"],
                      "hit": sum(1 for g in rows if g["hits"][k]),
                      "total": len(rows)}
    # 미정산 항목에도 키가 있어야 소비자(웹앱)가 분기 하나로 끝난다.
    # 예전에는 settled=False 인 항목에 hits/earned 가 아예 없어 KeyError 가 났다.
    for g in games:
        g.setdefault("settled", False)
        if not g["settled"]:
            g.setdefault("actual", None)
            g.setdefault("hits", None)
            g.setdefault("earned", None)
            g.setdefault("score", None)

    return {
        "updated": datetime.now(S.KST).isoformat(timespec="seconds"),
        "starballs": len(earned),
        "goal": STARBALL_GOAL,
        "settled": len(settled),
        "allHit": len(earned),
        "rate": round(len(earned) / len(settled), 4) if settled else None,
        # 재현분과 실사용분을 구분해서 내보낸다. 섞어서 자랑하면 안 된다.
        "replayed": replayed,
        "live": len(settled) - replayed,
        "perMission": per,
        # 시즌 전체를 남긴다. 잘라내면 내년 분석 재료가 사라진다.
        "games": games,
    }


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────

def cmd_backfill(count: int) -> int:
    """지난 경기를 시점 고정으로 재현해 기록을 채운다(시연·검증용).

    주의: 그냥 build_context 로 과거 날짜를 부르면 '오늘 시점' 팀 성적이
    쓰여서 미래를 훔쳐보게 되고, 모든 경기가 비슷하게 나온다. 백테스트와
    같은 누적 상태를 써야 그때 실제로 나왔을 추천이 된다.
    """
    path = f"gamelog_{S.today_kst().year}.json"
    try:
        with io.open(path, encoding="utf-8") as f:
            games = sorted(json.load(f)["games"],
                           key=lambda g: (g["date"], g["gameId"]))
    except FileNotFoundError:
        print(f"{path} 가 없습니다. 먼저: python build_gamelog.py", file=sys.stderr)
        return 1

    state = B.SeasonState()
    rows = []
    for g in games:
        if S.MY_TEAM in (g["home"], g["away"]) and state.ready(g["home"], g["away"]):
            ctx = B.make_context(state, g)
            pred = S.predict(ctx, n_sim=20000)
            best = (pred.combo or {}).get("best") or {}
            if best:
                rows.append({
                    "date": g["date"],
                    "opp": (S.TEAM_NAMES.get(g["away"] if g["home"] == S.MY_TEAM
                                             else g["home"])),
                    "home": g["home"] == S.MY_TEAM,
                    "picks": dict(best),
                    "settled": False,
                    "replayed": True,     # 실사용 기록이 아니라 재현이라는 표시
                })
        state.feed(g)

    rows = rows[-count:]
    rows.sort(key=lambda r: r["date"], reverse=True)
    _write(HISTORY_FILE, _summarize({"games": rows}))
    print(f"시점 고정 재현 {len(rows)}경기 등재", file=sys.stderr)
    return 0


def cmd_predict(day: date) -> int:
    client = S.NaverKBO()
    try:
        ctx = S.build_context(client, day)
    except S.NoGame as e:
        print(str(e), file=sys.stderr)
        return 0
    except S.NotReady as e:
        print(str(e), file=sys.stderr)
        return 75
    pred = S.predict(ctx)
    picks = S.to_starball_choices(pred)
    today = build_today(ctx, pred, picks)
    _write(TODAY_FILE, today)
    record_prediction(today, pred)
    print(f"web/today.json · {today['game']['lg']} vs {today['game']['opp']} · "
          f"{' / '.join(m['pick'] for m in today['missions'])} "
          f"({today['joint']['prob']*100:.1f}%)")
    return 0


def main(argv: list) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(argv) > 1 and argv[1] == "backfill":
        return cmd_backfill(int(argv[2]) if len(argv) > 2 else 12)

    cmd = argv[1] if len(argv) > 1 else "both"
    day = date.fromisoformat(argv[2]) if len(argv) > 2 else S.today_kst()

    if cmd in ("settle", "both"):
        n = settle(S.NaverKBO())
        print(f"정산 {n}건", file=sys.stderr)
        if cmd == "settle":
            return 0
    if cmd in ("predict", "both"):
        return cmd_predict(day)

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
