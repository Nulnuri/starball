import sys, io, json
import numpy as np
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

PARK={"창원":1.421,"대구":1.324,"문학":1.283,"광주":1.078,"대전":0.861,
      "고척":0.837,"사직":0.781,"수원":0.749,"잠실":0.665}
g=json.load(io.open("gamelog_2026.json",encoding="utf-8"))
games=sorted((g if isinstance(g,list) else g.get("games",[])),key=lambda x:x["date"])
def mb(m): return 9 if m>=9 else m
def hb(h): return 5 if h>=5 else h

team=defaultdict(lambda:{"g":0,"rs":0,"ra":0,"hr":0,"hra":0,"w":0})
pit=defaultdict(lambda:{"ip":0.0,"er":0,"hr":0,"k":0,"n":0})
rows=[]
for x in games:
    hs,as_=x.get("home_score"),x.get("away_score")
    if hs is None or as_ is None: continue
    b=x.get("box") or {}; P=x.get("pitchers") or {}
    def starter(side):
        for p in (P.get(side) or []):
            if p.get("started"): return p
        return None
    tsnap={t:dict(v) for t,v in team.items()}
    psnap={k:dict(v) for k,v in pit.items()}
    for me,foe,is_home in (("home","away",True),("away","home",False)):
        tm,op=x[me],x[foe]
        my,oy=(hs,as_) if is_home else (as_,hs)
        hr=(b.get(foe) or {}).get("hr_allowed")
        if hr is None: continue
        a,o=tsnap.get(tm),tsnap.get(op)
        ms,os_=starter(me),starter(foe)
        f=None
        if a and o and a["g"]>=15 and o["g"]>=15 and ms and os_:
            pm=psnap.get(ms["pcode"]); po=psnap.get(os_["pcode"])
            if pm and po and pm["ip"]>=20 and po["ip"]>=20:
                f=[1.0 if is_home else 0.0, PARK.get(x.get("stadium",""),1.0),
                   a["rs"]/a["g"],a["ra"]/a["g"],a["hr"]/a["g"],a["hra"]/a["g"],a["w"]/a["g"],
                   o["rs"]/o["g"],o["ra"]/o["g"],o["hr"]/o["g"],o["hra"]/o["g"],o["w"]/o["g"],
                   # 우리 선발 / 상대 선발 (그 시점까지 누적)
                   pm["er"]*9/pm["ip"], pm["hr"]*9/pm["ip"], pm["ip"]/max(pm["n"],1),
                   po["er"]*9/po["ip"], po["hr"]*9/po["ip"], po["ip"]/max(po["n"],1),
                   a["rs"]/a["g"]-o["ra"]/o["g"], o["rs"]/o["g"]-a["ra"]/a["g"],
                   po["er"]*9/po["ip"]-pm["er"]*9/pm["ip"]]
        rows.append({"date":x["date"],"feat":f,
                     "y_out":0 if my>oy else (2 if my<oy else 1),
                     "y_mar":mb(abs(my-oy)),"y_hr":hb(int(hr))})
    for me,foe,is_home in (("home","away",True),("away","home",False)):
        tm=x[me]; my,oy=(hs,as_) if is_home else (as_,hs)
        r=team[tm]; r["g"]+=1; r["rs"]+=my; r["ra"]+=oy
        r["hr"]+=int((b.get(foe) or {}).get("hr_allowed") or 0)
        r["hra"]+=int((b.get(me) or {}).get("hr_allowed") or 0)
        r["w"]+=1 if my>oy else 0
        for p in (P.get(me) or []):
            q=pit[p["pcode"]]; q["ip"]+=p.get("ip",0) or 0; q["er"]+=p.get("er",0) or 0
            q["hr"]+=p.get("hr",0) or 0
            if p.get("started"): q["n"]+=1

data=[r for r in rows if r["feat"]]
cut=int(len(data)*0.7); tr,te=data[:cut],data[cut:]
Xtr=np.array([r["feat"] for r in tr]); Xte=np.array([r["feat"] for r in te])
print(f"선발 정보까지 넣은 표본 {len(data)}건 · 특징 {Xtr.shape[1]}개")
print(f"학습 {len(tr)}건 ({tr[0]['date']}~{tr[-1]['date']}) / 검증 {len(te)}건 ({te[0]['date']}~{te[-1]['date']})\n")

def ev(fn,label):
    hit=sum(1 for i,r in enumerate(te) if fn(i,r)==(r["y_out"],r["y_mar"],r["y_hr"]))
    per=[sum(1 for i,r in enumerate(te) if fn(i,r)[j]==r[k])/len(te)*100
         for j,k in ((0,"y_out"),(1,"y_mar"),(2,"y_hr"))]
    print(f"  {label:<34} 전부 {hit/len(te)*100:>5.2f}%   "
          f"승패 {per[0]:>5.1f}%  득실 {per[1]:>5.1f}%  홈런 {per[2]:>5.1f}%")
    return hit/len(te)

c=Counter((r["y_out"],r["y_mar"],r["y_hr"]) for r in tr)
fixed=c.most_common(1)[0][0]
print("■ 검증 구간 (학습에 안 쓴 경기)")
ev(lambda i,r: fixed, "고정조합만 찍기 (기준선)")
pr={}
for n,k in (("out","y_out"),("mar","y_mar"),("hr","y_hr")):
    y=np.array([r[k] for r in tr])
    m=HistGradientBoostingClassifier(max_iter=300,learning_rate=0.06,max_depth=3,
        l2_regularization=1.0,random_state=0).fit(Xtr,y)
    pr[n]=(m.predict_proba(Xte),m.classes_)
ev(lambda i,r: tuple(pr[n][1][int(np.argmax(pr[n][0][i]))] for n in ("out","mar","hr")),
   "GBM (선발 포함) 문항별 argmax")
lp={}
for n,k in (("out","y_out"),("mar","y_mar"),("hr","y_hr")):
    y=np.array([r[k] for r in tr])
    m=LogisticRegression(max_iter=4000,C=0.2).fit(Xtr,y)
    lp[n]=(m.predict_proba(Xte),m.classes_)
ev(lambda i,r: tuple(lp[n][1][int(np.argmax(lp[n][0][i]))] for n in ("out","mar","hr")),
   "로지스틱 (선발 포함)")

# ── 여러 시점으로 잘라 반복 검증 (한 번의 운을 배제)
print("\n■ 분할 지점을 옮겨가며 반복 검증 (승패 문항)")
res={"기준선":[], "지금 모델방식":[], "로지스틱":[], "GBM":[]}
for frac in (0.5,0.55,0.6,0.65,0.7,0.75,0.8):
    cut=int(len(data)*frac); tr,te=data[:cut],data[cut:]
    if len(te)<60: continue
    Xtr=np.array([r["feat"] for r in tr]); Xte=np.array([r["feat"] for r in te])
    ytr=np.array([r["y_out"] for r in tr]); yte=np.array([r["y_out"] for r in te])
    base=Counter(ytr).most_common(1)[0][0]
    res["기준선"].append((yte==base).mean()*100)
    m=LogisticRegression(max_iter=4000,C=0.2).fit(Xtr,ytr)
    res["로지스틱"].append((m.predict(Xte)==yte).mean()*100)
    m=HistGradientBoostingClassifier(max_iter=300,learning_rate=0.06,max_depth=3,
        l2_regularization=1.0,random_state=0).fit(Xtr,ytr)
    res["GBM"].append((m.predict(Xte)==yte).mean()*100)
for k in ("기준선","로지스틱","GBM"):
    v=res[k]
    if v: print(f"  {k:<10} 평균 {sum(v)/len(v):>5.1f}%  "
                f"(범위 {min(v):.1f}~{max(v):.1f}, 분할 {len(v)}회)  "
                + " ".join(f"{x:.0f}" for x in v))

print("\n■ 스타볼 3.5개가 가능한 수인가 (잔여 24경기 기준)")
need=3.5/24*100
print(f"  필요한 경기당 전부적중률: {need:.2f}%")
print(f"  득실차의 물리적 상한 23.1% (576경기 중 1점이 최다), 홈런 상한 39.7% 라면")
for w in (54.2, 59.4, 70.0, 100.0):
    j=w/100*0.231*0.397*100
    print(f"    승패 {w:>5.1f}% 일 때 → 전부적중 {j:.2f}%  → 시즌 {j/100*24:.2f}개")
print(f"\n  승패를 100% 맞혀도 {0.231*0.397*24:.2f}개다. 3.5개는 이 구조에서 나올 수 없다.")
