# 다른 컴퓨터에서 이어서 작업하기

이 파일은 **새 Claude Code 세션이 처음 읽는 문서**다. 대화 기록은 기계마다
로컬에 남고 따라오지 않으므로, 여기에 "코드만 봐서는 알 수 없는 것"을 적는다.
코드 구조는 `web/docs/architecture.html`, 설치는 `web/docs/setup.html` 을 본다.

외장하드로 넘어온 경우, 하드 루트의 `맥북에서-스타볼-시작하기.txt` 에
준비물·가상환경·로그인까지 순서대로 적혀 있다.

## 지금 상태 (2026-08-31)

- 저장소 `github.com/Nulnuri/starball` (개인 계정, public, main)
- 운영 `https://starball-9oj.pages.dev/` · 거울 `https://nulnuri.github.io/starball/`
- 알림 세 갈래
  - **웹 푸시** — 지인용. 웹앱에서 켠다. 구독자는 Cloudflare KV 에 있다.
  - **캘린더(.ics)** — 아이폰에서 홈화면 추가를 안 하는 사람용
  - **ntfy** — 만든 사람 본인 채널. 토픽은 깃헙 Secret `NTFY_TOPIC`
- 크론 10:00 / 12:00 / 15:00 / 16:30 KST (워크플로에는 UTC 로 적혀 있다)
- 테스트 49개 (`python test_starball.py`, `python test_push.py`)

## 시작하기

**맥 — 외장하드(exFAT)에서 작업할 때**

```bash
cd /Volumes/T7/starball-lab

# 1) git 설정 두 개. 안 하면 시작부터 못 쓴다 (아래 "맥 함정" 참고)
git config core.autocrlf input
git config core.precomposeunicode true

# 2) 가상환경은 반드시 내장 디스크에 만든다. 외장하드에 만들면 깨진다.
python3 -m venv ~/.venvs/starball
source ~/.venvs/starball/bin/activate
pip install -r requirements.txt

# 3) 확인 — 30/30, 19/19 가 정상
python test_starball.py && python test_push.py
```

다음부터는 이것만:

```bash
cd /Volumes/T7/starball-lab && source ~/.venvs/starball/bin/activate
```

`gh` 는 push 할 때만 필요하다. 맥에 없으면 Homebrew 로 깐다
(`brew install gh` → `gh auth login`, **반드시 개인 계정 Nulnuri 로**.
회사 계정이면 push 가 403 이다). Homebrew 자체가 없으면 brew.sh 참고.

**윈도우**

```bash
pip install -r requirements.txt
python test_starball.py && python test_push.py
```

## 맥 + 외장하드(exFAT) 함정 — 2026-09-01 실제로 다 밟았다

**가상환경을 외장하드에 만들면 안 된다.** 맥은 exFAT 처럼 확장속성을 못
담는 파일시스템에 파일을 만들 때마다 `._이름` 짝꿍 파일(AppleDouble, 4KB
바이너리)을 같이 만든다. 그래서 `._distutils-precedence.pth` 가 생기는데,
파이썬의 `site.addsitedir()` 은 `*.pth` 를 전부 설정 파일로 읽으므로
바이너리를 UTF-8 로 디코드하려다 죽는다. venv 안의 python 이 아예 실행되지
않고 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xb0` 만 나온다.
원인이 전혀 드러나지 않는 에러다. **venv 는 `~/.venvs/starball` 에 둔다.**
코드는 하드에, 가상환경은 내장 디스크에 — 속도도 이쪽이 훨씬 빠르다.

**`core.autocrlf=input` 을 안 하면 28개 파일이 통째로 수정된 것으로 보인다.**
윈도우 git 이 작업본을 CRLF 로 받아놨는데 맥 git 은 LF 를 기대해서, 모든
줄이 바뀐 것으로 잡힌다(10,672 줄 추가 / 10,672 줄 삭제). 이 상태로
`git add -A` 를 하면 줄바꿈만 바꾼 거대한 커밋이 올라가고, 이후 diff 가
쓸모없어진다. 파일을 고칠 필요는 없다 — 설정만 바꾸면 깨끗해진다.

**`core.precomposeunicode=true` 를 안 하면 `.gitignore` 의 한글이 안 먹는다.**
맥은 파일명을 자모 분리형(NFD)으로 넘기는데 `.gitignore` 에 적힌 건 결합형
(NFC)이라 서로 다른 문자열이 된다. `스타볼-구조문서.html` 같은 무시 대상이
untracked 로 뜨고, `git add -A` 에 딸려 들어간다.

**`claude` 가 터미널 PATH 에 없을 수 있다.** VS Code 확장으로 쓰고 있으면
확장 안에만 있다. 터미널에서 쓰려면 따로 설치한다
(`npm install -g @anthropic-ai/claude-code`).

## 검증에만 쓰는 도구 (없어도 본체는 돌아간다)

`requirements.txt` 에는 넣지 않았다. 화면·규격을 눈으로 확인할 때만 쓴다.

```bash
pip install playwright && playwright install chromium   # 화면 렌더링·JS 오류 확인
pip install icalendar                                  # .ics 를 실제 파서로 검증
```

Playwright 로 확인할 때 주의: 헤드리스 브라우저는 **알림을 지원하지 않아**
`Notification.permission` 이 항상 `denied` 로 나온다. `channel="chromium"`
(새 헤드리스)을 쓰면 `granted` 가 되어 버튼까지 볼 수 있지만, 실제 푸시
구독은 크롬·엣지 모두 헤드리스에서 막혀 있다. 발송 확인은 실제 폰으로만
가능하다 — `push_send.py --who` 로 구독자를 보고 `--test` 로 보낸다.

## 저장소에 없는 것

`starball-secrets.txt` (VAPID 비밀키 · 발송 열쇠). 외장하드 루트에 있다.
없어도 자동 발송은 돌아간다 — 깃헙 Secret 을 쓴다. 손으로 테스트 발송을
할 때만 필요하다.

```bash
export PUSH_SEND_SECRET=...      # 파일의 PUSH_SEND_SECRET
export VAPID_PRIVATE_KEY="$(...)"  # 반드시 따옴표. 없으면 PEM 개행이 뭉개진다
python push_send.py --site https://starball-9oj.pages.dev --who    # 구독자 확인
python push_send.py --site https://starball-9oj.pages.dev --test   # 확인용 발송
```

## 밟았던 함정 — 다시 밟지 말 것

**네이버 일정 API 는 `yearMonth` 를 무시한다.** 실제로 월을 고르는 건 `date`
파라미터다. 둘 다 넘기고 응답을 월 접두사로 걸러야 한다. 이걸 몰라서 9월
데이터를 달라고 했는데 8월이 왔고, 상대전적이 통째로 틀렸다.

**시범경기·올스타전이 정규시즌과 섞여 온다.** 구분은 경기 상세의 `roundCode`
에만 있다(`kbo_e`=시범, `kbo_r`=정규, `kbo_as`=올스타). 일정만 보고는 못
걸른다. 안 걸르면 팀당 12경기가 통계에 섞인다.

**이닝 표기가 세 가지다.** `"34.2"`=34⅔, `"5 ⅓"`(박스스코어), `981.1`(팀
기록, float=981⅓). 하나라도 놓치면 ERA 가 두 배로 나온다.

**pywebpush 는 PEM 을 못 읽는다.** 내부에서 쓰는 `py_vapid.Vapid.from_string()`
이 개행만 지우고 base64 디코드를 시도해서 `-----BEGIN` 에서 깨진다.
`push_send.normalize_vapid_key()` 가 32바이트 원시 키로 바꿔 넘긴다.
이것 때문에 첫 발송이 실패했고, 에러는 `Could not deserialize key data`
한 줄뿐이라 원인을 알 수 없었다.

**Cloudflare Pages 는 `/index.html` 을 `/` 로 308 리다이렉트한다.**
서비스워커가 리다이렉트를 거친 응답을 화면 이동 요청에 돌려주면 브라우저가
거부하고 "사이트에 연결할 수 없음" 을 띄운다. 알림 링크 · manifest 의
`start_url` · 서비스워커 캐시 목록, 셋 다 `/` 로 두어야 한다.

**카톡 인앱 브라우저에서는 알림을 켤 수 없다.** 안드로이드 카톡은 WebView 라
푸시 등록이 안 되고, 아이폰 카톡에는 '홈 화면에 추가' 가 있는 공유 버튼이
없다. 공유 링크에 `?openExternalBrowser=1` 을 붙이면 카톡이 바깥 브라우저로
열어준다. 앱도 인앱 브라우저를 감지해 안내한다.

**아이폰 웹 푸시는 홈 화면 추가가 필수다.** 애플 정책이라 우회 방법이 없다.
사파리 탭에서는 `PushManager` 자체가 없다. 그래서 캘린더 구독을 같이 만들었다.

**`--home-only` 를 쓰면 안 된다.** 스타볼 미션은 원정 경기에도 열린다.

**마감 알림 크론은 하루 세 번 돈다.** 경기 시각이 14:00/17:00/18:30 세
종류라서다. 경기 1~3시간 전인지 확인하지 않으면 한 경기에 세 번 보낸다.

**캘린더(.ics) 값 안에 실제 개행을 넣으면 안 된다.** 그 지점에서 속성이 끊긴
것으로 해석돼 파일 전체가 거부될 수 있다. 줄은 75**옥텟**(글자 수가 아니다)
이하로 접어야 한다 — 한글은 한 자에 3바이트다.

## 방침

- **회사 계정·회사 노트북과 분리한다.** 개인 취미이고, 그렇게 하기로 했다.
- **예측기이지 정답기가 아니다.** 세 개 동시 적중은 5% 안팎이고 아무 값이나
  고정으로 찍으면 6.3% 다. 승패(+2.1%p)만 데이터가 먹힌다. 이 사실을 앱과
  README 에 그대로 적어둔다 — 숨기면 도구를 믿을 수 없게 된다.
- **화면 문구는 통계 용어를 쓰지 않는다.** '최빈' 을 '흔한 값' 으로 바꿨다.
  지인이 읽는 화면이다.
- **부가 기능이 본체를 망가뜨리지 않게 한다.** Cloudflare 배포 · 웹 푸시 ·
  캘린더 단계는 모두 `continue-on-error: true` 다. 알림과 데이터가 정상인데
  빨간 X 가 뜨면 고장으로 오해한다.

## 남은 일

- **[2026-09-01 반영 완료] 드롭다운 범위가 가정과 달랐다.**

  2026-08-31 이용자 확인 결과, 실제 앱의 선택지는 이렇다:

  | 문항 | 실제 | 코드의 가정(틀림) |
  |---|---|---|
  | 득실 차 | `0점` ~ `9점 이상` (10개) | 0~10점 (11개) |
  | 홈런 수 | `0개` ~ `5개 이상` (6개) | 0~10개 (11개) |

  **마지막 항목이 '이상' 이라는 게 핵심이다.** 정확히 그 값이 아니라 누적
  구간이라 P(홈런이 5) 가 아니라 P(홈런이 5 이상) 을 써야 한다. 다행히
  `starball_questions.json` 의 버킷은 [라벨, 하한, 상한] 이고 상한에
  null 을 주면 열린 구간이 된다 — 모델 코드는 건드릴 필요가 없다.

      margin  ... ["8점", 8, 8],  ["9점 이상", 9, null]
      lg_hr   ... ["4개", 4, 4],  ["5개 이상", 5, null]

  반영 순서 (앞에서부터, 건너뛰지 말 것):

  1. `starball_questions.json` 을 위 형태로 고친다. `_미확정` 줄도 지운다.
  2. `build_base_rates.py` 를 다시 돌린다. **반드시 해야 한다** — 라벨이
     바뀌면 `base_rates.json` 이 안 맞아 로드 때 검증에 걸리고 기저율
     블렌딩이 통째로 꺼진다(경고는 뜨지만 조용히 나빠진다).
  3. `backtest.py` 를 다시 돌린다. 선택지가 11개에서 6·10개로 줄었으니
     '흔한 값만 찍기' 기준선이 올라간다. `starball_predictor.py` 상단의
     `QUESTION_SKILL` 숫자를 새 결과로 갈아끼운다.
  4. README·web/docs·앱 화면의 수치(3개 동시 5% / 기준선 6.3% / 완벽예측
     상한 5.5%)를 새 값으로 바꾼다. **전부 달라진다** — 선택지가 줄면 다
     맞힐 확률이 올라간다.
  5. `test_starball.py` 와 `test_push.py` 를 돌려 49개 통과를 확인한다.

  **다섯 단계 모두 마쳤다(2026-09-01).** 결과:

  | | 전(0~10 가정) | 후(실제 구조) |
  |---|---|---|
  | 승패 | 54.2% (기준 52.1) | 53.3% (기준 50.9) |
  | 득실 차 | 25.0% (기준 26.1) | 22.5% (기준 22.5) |
  | 홈런 수 | 36.5% (기준 36.5) | 41.9% (기준 41.9) |
  | 세 개 모두 | 약 5% | 4.63% |
  | 고정 조합 기준선 | 6.3% | 6.32% (승·1점·0개) |

  홈런이 36.5% → 41.9% 로 오른 건 선택지가 11개에서 6개로 줄어 0·1개에
  확률이 몰렸기 때문이다. 모델이 좋아진 게 아니라 문제가 쉬워진 것이고,
  기준선도 똑같이 올라서 개선폭은 여전히 0.0%p 다.

  README 에 있던 **'완벽한 예측자의 상한 5.5%' 는 지웠다.** 최초 커밋부터
  있던 값인데 이걸 계산하는 코드가 저장소 어디에도 없어서 재현할 수 없었다.
  대신 backtest.py 가 실제로 뱉는 두 숫자(모델 4.63% / 고정 조합 6.32%)로
  바꿨다. 재현 안 되는 숫자를 화면에 두지 않는다.

  앱 화면의 문항별 수치는 `webdata.py` 가 `QUESTION_SKILL` 에서 읽어 만들기
  때문에 따로 고칠 게 없다 — 다음 크론이 돌면 반영된다.
- **2027 시즌 3월 주의.** 깃헙은 저장소가 60일간 조용하면 크론을 끈다.
  비시즌(11~2월)이 지나면 Actions 탭에서 다시 켜야 한다.
- 2026 잔여 시즌은 이대로 운영하고, 디벨롭은 2027 시즌에.
