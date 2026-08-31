# 스타볼 예측

LG 트윈스 앱의 **스타볼 매치데이 미션** 세 개에 넣을 값을, 네이버 스포츠
공개 기록으로 계산해서 폰 알림과 웹앱으로 보내주는 도구입니다.

```
MISSION 1  승패 맞히기        승 / 무 / 패
MISSION 2  득실 차 맞히기      0 ~ 10
MISSION 3  홈런 수 맞히기      0 ~ 10  (LG 기준)
```

세 개를 **모두** 맞혀야 스타볼 1개, 7개를 모으면 경품 응모입니다.

## 쓰는 법

**https://starball-9oj.pages.dev/** ← 폰에서 열고 홈 화면에 추가하세요.

- **웹앱** — 위 주소. 오늘 추천 / 근거 / 지난 기록 세 화면입니다.
- **알림** — 위 주소를 열고 아래쪽 **알림 받기** 를 누르면 됩니다. 앱을 깔
  필요는 없습니다. 경기일 아침과 마감 2시간 전에 한 번씩 옵니다.
  - 안드로이드: 홈 화면 추가 없이 바로 됩니다.
  - 아이폰: 애플이 홈 화면에 추가된 웹앱에만 알림을 허용합니다. **공유 →
    홈 화면에 추가** 후 아이콘으로 열어야 버튼이 보입니다.
  - 홈 화면 추가가 번거로우면 **캘린더로 받기** — 남은 경기가 기본 캘린더에
    들어가고 경기 2시간 전에 기기가 알려줍니다. 설치할 것이 없습니다.
- **ntfy** — 만든 사람이 쓰는 채널입니다. 지인에게는 위쪽 방법을 권합니다.
- **거울 주소** — <https://nulnuri.github.io/starball/> (같은 내용. Cloudflare 가 죽었을 때의 예비)
- **직접 돌려보기** — `docs/` 안내서 (https://starball-9oj.pages.dev/docs/)

다른 컴퓨터에서 이어서 작업하려면 [HANDOFF.md](HANDOFF.md) 를 먼저 보세요.

설치는 [설치 안내](web/docs/setup.html), 내부 구조는
[구조 문서](web/docs/architecture.html)를 보세요.

## 얼마나 맞히나

솔직한 수치입니다. 2026시즌 475경기를 시점 고정으로 재현해 측정했습니다.

| 항목 | 적중률 |
|---|---|
| 승패 | 54% |
| 득실 차 | 25% |
| 홈런 수 | 37% |
| **세 개 모두** | **약 5%** (20경기에 한 번) |

세 개를 동시에 요구하니 곱해져서 낮아집니다. 참고로 **완벽한 예측자의 상한이
5.5%** 이고, 아무 데이터 없이 가장 흔한 값만 계속 찍어도 6.3%입니다. 즉 이 문제에서
예측의 여지 자체가 크지 않습니다. 경품을 노리는 도구라기보다
**근거를 갖춘 추천을 매번 자동으로 받는 도구**로 보시는 게 맞습니다.

자세한 근거와 한계는 구조 문서에 정리해뒀습니다.

## 구성

```
starball_predictor.py   예측 본체 (수집 · 모델 · ntfy 알림)
push_send.py            웹앱 구독자에게 웹 푸시 발송
make_ics.py             남은 경기 캘린더(.ics) 생성
setup_push.py           Cloudflare KV 연결 (1회)
web/_worker.js          구독자 명단 보관 API (Pages 에서 실행)
webdata.py              웹앱 데이터 생성 · 경기 후 자동 채점
backtest.py             시점 고정 백테스트
ablation.py             구성요소 절제 실험
build_gamelog.py        시즌 경기 로그 수집
build_base_rates.py     리그 기저 분포 생성
test_starball.py        회귀 테스트 30건
web/                    PWA (의존성 없는 정적 파일)
```

## 직접 돌려보기

```bash
pip install -r requirements.txt
python starball_predictor.py            # 오늘 예측
python starball_predictor.py --probe    # 엔드포인트 점검
python test_starball.py                 # 테스트
python backtest.py                      # 과거 검증
```

## 데이터 출처

네이버 스포츠 비공개 API (`api-gw.sports.naver.com`). 공개 문서가 없는
내부 엔드포인트라 언제든 바뀔 수 있습니다. `--probe` 가 어디가 깨졌는지
알려주고, 수집이 깨져도 `--fixture` 로 모델과 출력은 계속 검증됩니다.

예측은 확률 산출물이며 결과를 보장하지 않습니다.
