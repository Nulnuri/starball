import sys, io, json
import numpy as np
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

PARK = {"창원":1.421,"대구":1.324,"문학":1.283,"광주":1.078,
        "대전":0.861,"고척":0.837,"사직":0.781,"수원":0.749,"잠실":0.665}
g = json.load(io.open("gamelog_2026.json", encoding="utf-8"))
games = sorted((g if isinstance(g,list) else g.get("games",[])), key=lambda x: x["date"])

def mb(m): return 9 if m>=9 else m          # 0..9 (9=9점 이상)
def hb(h): return 5 if h>=5 else h          # 0..5 (5=5개 이상)

# ── 팀-경기 표를 만들고, 그 시점까지의 누적 성적(미래 정보 없음)을 붙인다
run = defaultdict(lambda: {"g":0,"rs":0,"ra":0,"hr":0,"hra":0,"w":0})
rows=[]
for x in games:
    hs, as_ = x.get("home_score"), x.get("away_score")
    if hs is None or as_ is None: continue
    b = x.get("box") or {}
    snap = {t: dict(v) for t,v in run.items()}
    for me, foe, is_home in (("home","away",True),("away","home",False)):
        tm, op = x[me], x[foe]
        my, oy = (hs,as_) if is_home else (as_,hs)
        hr = (b.get(foe) or {}).get("hr_allowed")
        if hr is None: continue
        a, o = snap.get(tm), snap.get(op)
        if not a or not o or a["g"]<15 or o["g"]<15:   # 표본이 쌓인 뒤부터
            feat=None
        else:
            feat = [
                1.0 if is_home else 0.0,
                PARK.get(x.get("stadium",""),1.0),
                a["rs"]/a["g"], a["ra"]/a["g"], a["hr"]/a["g"], a["hra"]/a["g"], a["w"]/a["g"],
                o["rs"]/o["g"], o["ra"]/o["g"], o["hr"]/o["g"], o["hra"]/o["g"], o["w"]/o["g"],
                a["rs"]/a["g"] - o["ra"]/o["g"],      # 우리 타선 vs 상대 실점
                o["rs"]/o["g"] - a["ra"]/a["g"],
            ]
        rows.append({"date":x["date"],"team":tm,"opp":op,"home":is_home,
                     "feat":feat,
                     "y_out": 0 if my>oy else (2 if my<oy else 1),
                     "y_mar": mb(abs(my-oy)), "y_hr": hb(int(hr))})
    # 누적 갱신 (경기가 끝난 뒤)
    for me, foe, is_home in (("home","away",True),("away","home",False)):
        tm=x[me]; my,oy=(hs,as_) if is_home else (as_,hs)
        hr=(b.get(foe) or {}).get("hr_allowed") or 0
        hra=(b.get(me) or {}).get("hr_allowed") or 0
        r=run[tm]; r["g"]+=1; r["rs"]+=my; r["ra"]+=oy
        r["hr"]+=int(hr); r["hra"]+=int(hra); r["w"]+= 1 if my>oy else 0

data=[r for r in rows if r["feat"]]
print(f"학습 가능 표본 {len(data)}건 (전체 {len(rows)}건 중, 양 팀 15경기 이상 소화분)\n")

OUT=["승","무","패"]
MAR=[f"{k}점" for k in range(9)]+["9점 이상"]
HR=[f"{k}개" for k in range(5)]+["5개 이상"]

# ── 시간 분할: 앞 70% 학습, 뒤 30% 검증 (미래로 학습하는 일이 없게)
cut=int(len(data)*0.7)
tr, te = data[:cut], data[cut:]
Xtr=np.array([r["feat"] for r in tr]); Xte=np.array([r["feat"] for r in te])
print(f"학습 {len(tr)}건 ({tr[0]['date']}~{tr[-1]['date']}) / "
      f"검증 {len(te)}건 ({te[0]['date']}~{te[-1]['date']})\n")

def joint_rate(pick_fn, sample, label):
    hit=sum(1 for i,r in enumerate(sample)
            if pick_fn(i,r)==(r["y_out"],r["y_mar"],r["y_hr"]))
    print(f"  {label:<40} {hit}/{len(sample)} = {hit/len(sample)*100:.2f}%")
    return hit/len(sample)

print("■ 검증 구간 성적 (학습에 쓰지 않은 경기)")
# 기준선 1: 학습 구간의 최적 고정조합
c=Counter((r["y_out"],r["y_mar"],r["y_hr"]) for r in tr)
fixed=c.most_common(1)[0][0]
print(f"   (학습 구간 최적 고정조합: {OUT[fixed[0]]} / {MAR[fixed[1]]} / {HR[fixed[2]]})")
joint_rate(lambda i,r: fixed, te, "고정조합만 찍기")

# 기준선 2: 문항별 최빈값 조합
mo=(Counter(r["y_out"] for r in tr).most_common(1)[0][0],
    Counter(r["y_mar"] for r in tr).most_common(1)[0][0],
    Counter(r["y_hr"] for r in tr).most_common(1)[0][0])
joint_rate(lambda i,r: mo, te, "문항별 최빈값 조합")

# ML 1: 문항별로 따로 학습해 각각 argmax
preds={}
for name,key,n in (("out","y_out",3),("mar","y_mar",10),("hr","y_hr",6)):
    y=np.array([r[key] for r in tr])
    m=HistGradientBoostingClassifier(max_iter=300,learning_rate=0.06,
        max_depth=3,l2_regularization=1.0,random_state=0).fit(Xtr,y)
    P=m.predict_proba(Xte)
    cls=m.classes_
    preds[name]=(P,cls)
def ml_marg(i,r):
    return tuple(preds[k][1][int(np.argmax(preds[k][0][i]))] for k in ("out","mar","hr"))
joint_rate(ml_marg, te, "머신러닝 · 문항별 argmax (GBM)")

# ML 2: 결합 확률 최대 조합 (독립 가정)
def ml_joint(i,r):
    best=None;bp=-1
    for a,pa in zip(preds["out"][1],preds["out"][0][i]):
        for b,pb in zip(preds["mar"][1],preds["mar"][0][i]):
            for c2,pc in zip(preds["hr"][1],preds["hr"][0][i]):
                p=pa*pb*pc
                if p>bp: bp,best=p,(a,b,c2)
    return best
joint_rate(ml_joint, te, "머신러닝 · 결합확률 최대 (독립가정)")

# ML 3: 180개 조합을 하나의 다중분류로 (상관까지 학습)
lab_tr=np.array([r["y_out"]*100+r["y_mar"]*10+r["y_hr"] for r in tr])
m=HistGradientBoostingClassifier(max_iter=400,learning_rate=0.06,max_depth=3,
    l2_regularization=1.0,random_state=0).fit(Xtr,lab_tr)
P=m.predict_proba(Xte); cls=m.classes_
def ml_combo(i,r):
    v=int(cls[int(np.argmax(P[i]))])
    return (v//100, (v//10)%10, v%10)
joint_rate(ml_combo, te, "머신러닝 · 조합 자체를 분류 (상관 학습)")

# 로지스틱도 (표본이 적으면 단순한 모델이 나을 수 있다)
lp={}
for name,key in (("out","y_out"),("mar","y_mar"),("hr","y_hr")):
    y=np.array([r[key] for r in tr])
    m=LogisticRegression(max_iter=3000,C=0.3).fit(Xtr,y)
    lp[name]=(m.predict_proba(Xte),m.classes_)
joint_rate(lambda i,r: tuple(lp[k][1][int(np.argmax(lp[k][0][i]))]
                             for k in ("out","mar","hr")),
           te, "로지스틱 회귀 · 문항별 argmax")
