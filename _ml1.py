import sys, io, json
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PARK = {"창원":1.421,"대구":1.324,"문학":1.283,"광주":1.078,
        "대전":0.861,"고척":0.837,"사직":0.781,"수원":0.749,"잠실":0.665}

g = json.load(io.open("gamelog_2026.json", encoding="utf-8"))
games = g if isinstance(g, list) else g.get("games", [])

def mbucket(m):                      # 실제 드롭다운: 0~8, 9점 이상
    return "9점 이상" if m >= 9 else f"{m}점"
def hbucket(h):                      # 실제 드롭다운: 0~4, 5개 이상
    return "5개 이상" if h >= 5 else f"{h}개"

rows = []
for x in games:
    hs, as_ = x.get("home_score"), x.get("away_score")
    if hs is None or as_ is None: continue
    b = x.get("box") or {}
    for me, foe, is_home in (("home","away",True), ("away","home",False)):
        my, oy = (hs, as_) if is_home else (as_, hs)
        hr = (b.get(foe) or {}).get("hr_allowed")     # 상대가 허용 = 내 홈런
        if hr is None: continue
        rows.append({
            "team": x[me], "opp": x[foe], "home": is_home,
            "stadium": x.get("stadium",""),
            "park": PARK.get(x.get("stadium",""), 1.0),
            "outcome": "승" if my > oy else ("패" if my < oy else "무"),
            "margin": mbucket(abs(my - oy)),
            "hr": hbucket(int(hr)),
        })

print(f"팀-경기 표본 {len(rows)}건 (경기 {len(rows)//2}건 × 양 팀)\n")

def best_fixed(sample, label):
    c = Counter((r["outcome"], r["margin"], r["hr"]) for r in sample)
    combo, n = c.most_common(1)[0]
    print(f"  {label:<34} 최적 고정조합 {' / '.join(combo):<22} "
          f"{n}/{len(sample)} = {n/len(sample)*100:.2f}%")
    return n/len(sample)

print("■ 1) 아무 조건 없이 '매번 같은 조합' 만 찍을 때의 최적값")
base_all = best_fixed(rows, "전체 10개 구단")
lg = [r for r in rows if r["team"] == "LG"]
base_lg = best_fixed(lg, f"LG 만 ({len(lg)}경기)")

print("\n■ 2) 상한: 조건을 알면 얼마나 올라가나")
print("   각 조건 칸마다 그 칸에서 가장 흔한 조합을 '미리 알고' 찍는다.")
print("   같은 데이터로 정답을 보고 고르는 것이므로, 어떤 모델도 이 값을 넘을 수 없다.\n")

def oracle(sample, keyfn, label):
    cells = defaultdict(Counter)
    for r in sample:
        cells[keyfn(r)][(r["outcome"], r["margin"], r["hr"])] += 1
    hit = sum(c.most_common(1)[0][1] for c in cells.values())
    print(f"  {label:<34} 칸 {len(cells):>3}개  {hit}/{len(sample)} = "
          f"{hit/len(sample)*100:.2f}%")
    return hit/len(sample)

oracle(rows, lambda r: (r["home"],), "홈/원정")
oracle(rows, lambda r: (r["stadium"],), "구장")
oracle(rows, lambda r: (r["home"], r["stadium"]), "홈/원정 + 구장")
oracle(rows, lambda r: (r["team"],), "팀")
oracle(rows, lambda r: (r["team"], r["home"]), "팀 + 홈/원정")
oracle(rows, lambda r: (r["team"], r["opp"]), "팀 + 상대 (90칸)")
oracle(rows, lambda r: (r["team"], r["opp"], r["home"]), "팀 + 상대 + 홈/원정 (180칸)")
