# 다른 컴퓨터에서 이어서 작업하기

이 파일은 **새 Claude Code 세션이 처음 읽는 문서**다. 대화 기록은 기계마다
로컬에 남고 따라오지 않으므로, 여기에 "코드만 봐서는 알 수 없는 것"을 적는다.
코드 구조는 `web/docs/architecture.html`, 설치는 `web/docs/setup.html` 을 본다.

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

```bash
git config --global --add safe.directory "$(pwd)"   # 외장하드(exFAT)일 때만
pip install -r requirements.txt
gh auth login          # 반드시 개인 계정으로. 회사 계정이면 push 가 403 이다
python test_starball.py && python test_push.py
```

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

- **득실 차 드롭다운 상한 확인.** 지금은 0~10 으로 가정한다. 경기일에 앱에서
  목록을 끝까지 내려 확인하고, 다르면 `starball_questions.json` 을 고친 뒤
  `build_base_rates.py` → `backtest.py` 를 다시 돌린다.
- **2027 시즌 3월 주의.** 깃헙은 저장소가 60일간 조용하면 크론을 끈다.
  비시즌(11~2월)이 지나면 Actions 탭에서 다시 켜야 한다.
- 2026 잔여 시즌은 이대로 운영하고, 디벨롭은 2027 시즌에.
