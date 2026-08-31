#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""남은 LG 경기 일정을 캘린더 구독 파일(web/starball.ics)로 만든다.

아이폰은 애플 정책상 '홈 화면에 추가된 웹앱' 에만 웹 푸시를 허용한다.
홈 화면 추가를 안 하는 사람에게는 알림을 보낼 방법이 없다 — 그 사람들을 위해
기본 캘린더로 우회한다. 링크 한 번 누르면 남은 경기가 모두 들어가고,
경기 2시간 전에 기기 자체 알림이 뜬다. 설치할 것이 없다.

일정에 예측값을 박지 않는 이유:
아이폰은 구독 캘린더를 몇 시간에 한 번 제 맘대로 갱신한다. 값을 박으면
갱신이 늦은 기기에서 '틀린 값' 을 확신에 차서 보여주게 된다. 그래서 일정은
'입력할 시간이다 + 링크' 만 알려주고, 값은 링크를 눌러 보게 한다.

    python make_ics.py --site https://starball-9oj.pages.dev
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

import starball_predictor as S

KST = timezone(timedelta(hours=9))
OUT = "web/starball.ics"

# 경기 시작 몇 시간 전에 알릴지. 예측기의 마감 임박 알림과 같은 시점으로 둔다.
ALARM_BEFORE = "-PT2H"


def esc(text: str) -> str:
    """RFC 5545 TEXT 이스케이프.

    순서가 중요하다 — 역슬래시를 먼저 처리해야 뒤에 넣은 역슬래시가
    다시 이스케이프되지 않는다. 개행은 반드시 두 글자 표기로 바꿔야 한다.
    값 안에 실제 개행이 남으면 그 지점에서 속성이 끊긴 것으로 해석돼
    파일 전체가 거부될 수 있다.
    """
    b = chr(92)
    return (str(text)
            .replace(b, b + b)
            .replace(";", b + ";")
            .replace(",", b + ",")
            .replace(chr(13) + chr(10), b + "n")
            .replace(chr(10), b + "n")
            .replace(chr(13), b + "n"))


def fold(line: str) -> str:
    """한 줄을 75옥텟 이하로 접는다.

    글자 수가 아니라 바이트 수 기준이다. 한글은 한 자에 3바이트라
    글자 수로 자르면 규격을 넘고, 멀티바이트 중간에서 자르면 깨진다.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, buf, size = [], [], 0
    limit = 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append("".join(buf))
            buf, size, limit = [ch], n, 74      # 이어지는 줄은 앞에 공백 한 칸
        else:
            buf.append(ch)
            size += n
    out.append("".join(buf))
    return "\r\n ".join(out)


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def collect(client: S.NaverKBO, today: date) -> list[dict]:
    """오늘 이후의 LG 경기. 취소된 경기는 뺀다."""
    games = []
    seen = set()
    for month in range(today.month, 12):
        try:
            entries = client.calendar(today.year, month)
        except Exception as e:
            print(f"  {today.year}-{month:02d} 일정 조회 실패: {e}", file=sys.stderr)
            continue
        for entry in entries:
            ymd = entry.get("ymd") or ""
            try:
                day = date.fromisoformat(ymd)
            except ValueError:
                continue
            if day < today:
                continue
            for gi in entry.get("gameInfos") or []:
                if not client.is_team_game(gi):
                    continue
                if S.MY_TEAM not in (gi.get("homeTeamCode"), gi.get("awayTeamCode")):
                    continue
                gid = gi.get("gameId")
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                try:
                    g = client.game(gid)
                except Exception as e:
                    print(f"  {gid} 상세 조회 실패: {e}", file=sys.stderr)
                    continue
                if g.get("cancel") or not str(g.get("roundCode", "")).endswith("_r"):
                    continue
                try:
                    start = datetime.fromisoformat(g["gameDateTime"]).replace(tzinfo=KST)
                except (KeyError, TypeError, ValueError):
                    continue
                games.append({
                    "id": gid, "start": start,
                    "stadium": g.get("stadium") or "",
                    "away": g.get("awayTeamName") or g.get("awayTeamCode") or "",
                    "home": g.get("homeTeamName") or g.get("homeTeamCode") or "",
                    "tbd": bool(g.get("timeTbd")),
                })
        if not games and month > today.month + 2:
            break
    games.sort(key=lambda x: x["start"])
    return games


def build(games: list[dict], site: str, stamp: datetime) -> str:
    site = site.rstrip("/")
    host = site.split("//", 1)[-1]
    L = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//starball//LG Twins Starball//KO",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:LG 스타볼",
        "X-WR-CALDESC:경기 2시간 전에 스타볼 입력을 알려드립니다.",
        "X-WR-TIMEZONE:Asia/Seoul",
        # 구독 캘린더를 얼마나 자주 새로 받을지 알려주는 힌트다.
        # 기기가 반드시 따르지는 않는다(아이폰은 자기 판단으로 미룬다).
        "REFRESH-INTERVAL;VALUE=DURATION:PT4H",
        "X-PUBLISHED-TTL:PT4H",
    ]
    for g in games:
        label = f"{g['away']}@{g['home']}"
        title = f"스타볼 · {label}" + ("  (시간 미정)" if g["tbd"] else "")
        # 실제 개행으로 만들고 esc() 가 규격 표기로 바꾼다.
        desc = esc(chr(10).join([
            f"오늘 추천값 보기: {site}/",
            "",
            f"{label} · {g['stadium']} · {g['start']:%H:%M} 시작",
            "값은 링크에서 확인하세요 — 경기 당일 아침에 계산됩니다.",
        ]))
        L += [
            "BEGIN:VEVENT",
            f"UID:{g['id']}@{host}",
            f"DTSTAMP:{utc(stamp)}",
            f"DTSTART:{utc(g['start'])}",
            f"DTEND:{utc(g['start'] + timedelta(hours=3, minutes=30))}",
            f"SUMMARY:{esc(title)}",
            f"LOCATION:{esc(g['stadium'])}",
            f"DESCRIPTION:{desc}",
            f"URL:{site}/",
            "TRANSP:TRANSPARENT",          # 바쁨으로 잡히지 않게
            "BEGIN:VALARM",
            f"TRIGGER:{ALARM_BEFORE}",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{esc('스타볼 입력 시간 · ' + label)}",
            "END:VALARM",
            "END:VEVENT",
        ]
    L.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in L) + "\r\n"


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="스타볼 캘린더 구독 파일 생성")
    ap.add_argument("--site", required=True)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    now = datetime.now(KST)
    games = collect(S.NaverKBO(), now.date())
    if not games:
        print("남은 경기가 없어 캘린더를 만들지 않습니다", file=sys.stderr)
        return 0

    text = build(games, args.site, now)
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print(f"{args.out} · {len(games)}경기 "
          f"({games[0]['start']:%m/%d} ~ {games[-1]['start']:%m/%d}) "
          f"· {len(text.encode('utf-8'))}바이트", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
