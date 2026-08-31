#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""지인용 알림(웹 푸시 · 캘린더) 회귀 테스트.

    python -m pytest test_push.py -q      (pytest 있으면)
    python test_push.py                   (없어도 그냥 돌아감)

네트워크를 타지 않는다. 순수 함수만 검사한다.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_ics as I  # noqa: E402
import push_send as P  # noqa: E402

KST = timezone(timedelta(hours=9))
BS = chr(92)
NL = chr(10)


def _today(lg_is_home: bool = False) -> dict:
    return {
        "game": {"date": "2026-09-01", "time": "18:30", "stadium": "잠실",
                 "lg": "LG", "opp": "두산", "lgIsHome": lg_is_home},
        "missions": [
            {"n": 1, "label": "승패 맞히기", "pick": "승", "prob": 0.4691},
            {"n": 2, "label": "득실 차 맞히기", "pick": "1점", "prob": 0.1917},
            {"n": 3, "label": "홈런 수 맞히기", "pick": "0개", "prob": 0.4226},
        ],
        "joint": {"prob": 0.056, "confidence": "낮음"},
    }


def _game(hour: int = 18, minute: int = 30) -> dict:
    return {"id": "20260901LGOB02026",
            "start": datetime(2026, 9, 1, hour, minute, tzinfo=KST),
            "stadium": "잠실", "away": "LG", "home": "두산", "tbd": False}


# ── 알림 본문 ────────────────────────────────────────────────────────────

def test_away_game_reads_lg_at_opponent():
    """lgIsHome=False 는 LG 원정이다. 한때 이걸 뒤집어 써서 '두산@LG' 가 나왔다.
    웹앱(index.html)과 같은 규칙이어야 한다."""
    body = P.build_payload(_today(lg_is_home=False), False, "https://x")["body"]
    assert "LG@두산" in body, body
    assert "두산@LG" not in body, body


def test_home_game_reads_opponent_at_lg():
    body = P.build_payload(_today(lg_is_home=True), False, "https://x")["body"]
    assert "두산@LG" in body, body


def test_morning_payload_carries_all_three_picks():
    """알림 첫 줄만 보고 그대로 입력할 수 있어야 한다."""
    p = P.build_payload(_today(), False, "https://x")
    for pick in ("승", "1점", "0개"):
        assert pick in p["title"], p["title"]
    for label in ("승패 맞히기", "득실 차 맞히기", "홈런 수 맞히기"):
        assert label in p["body"], p["body"]
    assert p["url"].startswith("https://x")
    assert p["tag"] == "starball"


def test_reminder_payload_is_short():
    """마감 임박 알림은 잠금화면에서 한눈에 읽혀야 한다. 확률표를 넣지 않는다."""
    r = P.build_payload(_today(), True, "https://x")
    assert "마감" in r["title"]
    assert len(r["body"].splitlines()) == 1, r["body"]
    assert "%" not in r["body"], r["body"]


def test_no_game_returns_none():
    assert P.build_payload({}, False, "https://x") is None
    assert P.build_payload({"game": None, "missions": []}, False, "https://x") is None


def test_partial_missions_returns_none():
    """미션이 3개가 아니면 보내지 않는다. 두 개만 보내면 오히려 헷갈린다."""
    d = _today()
    d["missions"] = d["missions"][:2]
    assert P.build_payload(d, False, "https://x") is None


def test_payload_fits_push_limit():
    """푸시 서비스는 본문 4096바이트를 넘기면 거부한다."""
    raw = json.dumps(P.build_payload(_today(), False, "https://starball-9oj.pages.dev"),
                     ensure_ascii=False).encode("utf-8")
    assert len(raw) < 3000, len(raw)


def test_vapid_pem_is_converted_for_pywebpush():
    """pywebpush 는 PEM 을 못 읽는다 — 32바이트 원시 키로 바꿔 넘겨야 한다.

    이걸 빼면 발송이 'Could not deserialize key data' 한 줄만 남기고 조용히
    실패한다. 실제로 첫 발송이 이것 때문에 실패했다.
    """
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import py_vapid

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    want = base64.urlsafe_b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)).decode().rstrip("=")

    for label, text in (("PEM", pem),
                        ("CR 섞인 PEM", pem.replace(NL, chr(13) + NL)),
                        ("앞뒤 공백", "  " + pem + "  ")):
        raw = P.normalize_vapid_key(text)
        assert len(raw) == 43, (label, len(raw))
        # pywebpush 가 실제로 쓰는 경로를 통과해야 한다
        v = py_vapid.Vapid.from_string(private_key=raw)
        got = base64.urlsafe_b64encode(
            v.public_key.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint)).decode().rstrip("=")
        assert got == want, label


def test_vapid_raw_key_passes_through():
    """이미 원시 키면 손대지 않는다."""
    raw = "aF3vYF5kuJoRliwXt8D1abcdefghijklmnopqrstuvw"
    assert P.normalize_vapid_key(raw) == raw
    assert P.normalize_vapid_key("  " + raw + NL) == raw


# ── 캘린더 ───────────────────────────────────────────────────────────────

def test_ics_escapes_special_characters():
    assert I.esc("a;b") == "a" + BS + ";b"
    assert I.esc("a,b") == "a" + BS + ",b"
    assert I.esc(BS) == BS + BS
    # 개행은 반드시 두 글자 표기로. 실제 개행이 남으면 그 지점에서 속성이
    # 끊긴 것으로 해석돼 파일 전체가 거부될 수 있다.
    assert I.esc("a" + NL + "b") == "a" + BS + "nb"
    assert I.esc("a" + chr(13) + chr(10) + "b") == "a" + BS + "nb"
    assert NL not in I.esc("a" + NL + "b")


def test_ics_escape_order():
    """역슬래시를 먼저 처리해야 나중에 넣은 역슬래시가 다시 안 먹힌다."""
    assert I.esc(BS + ";") == BS + BS + BS + ";"


def test_ics_folds_by_octets_not_characters():
    """한글은 한 자에 3바이트다. 글자 수로 자르면 규격(75옥텟)을 넘는다."""
    folded = I.fold("SUMMARY:" + "가" * 60)
    for line in folded.split(chr(13) + chr(10)):
        assert len(line.encode("utf-8")) <= 75, len(line.encode("utf-8"))
    # 접힘을 풀면 원문이 그대로 나와야 한다
    assert folded.replace(chr(13) + chr(10) + " ", "") == "SUMMARY:" + "가" * 60


def test_ics_short_line_untouched():
    assert I.fold("BEGIN:VEVENT") == "BEGIN:VEVENT"


def test_ics_has_alarm_two_hours_before():
    text = I.build([_game()], "https://x", datetime(2026, 8, 31, 10, 0, tzinfo=KST))
    assert "BEGIN:VALARM" in text
    assert "TRIGGER:-PT2H" in text


def test_ics_times_are_utc():
    """18:30 KST = 09:30 UTC. 시간대를 안 바꾸면 9시간 어긋난 알람이 뜬다."""
    text = I.build([_game(18, 30)], "https://x", datetime(2026, 8, 31, tzinfo=KST))
    assert "DTSTART:20260901T093000Z" in text, text


def test_ics_structure_is_balanced():
    text = I.build([_game(), _game(14, 0)], "https://x", datetime(2026, 8, 31, tzinfo=KST))
    for tag in ("VCALENDAR", "VEVENT", "VALARM"):
        assert text.count("BEGIN:" + tag) == text.count("END:" + tag)
    assert text.count("BEGIN:VEVENT") == 2


def test_ics_every_line_is_crlf_and_within_limit():
    text = I.build([_game()], "https://x", datetime(2026, 8, 31, tzinfo=KST))
    raw = text.encode("utf-8")
    lone = sum(1 for i, b in enumerate(raw) if b == 10 and (i == 0 or raw[i - 1] != 13))
    assert lone == 0, f"단독 LF {lone}개"
    for line in text.split(chr(13) + chr(10)):
        assert len(line.encode("utf-8")) <= 75, line[:40]


def test_ics_description_has_no_real_newline():
    """접힘을 풀었을 때도 DESCRIPTION 안에 실제 개행이 없어야 한다."""
    text = I.build([_game()], "https://x", datetime(2026, 8, 31, tzinfo=KST))
    un, cur = [], ""
    for line in text.split(chr(13) + chr(10)):
        if line.startswith(" "):
            cur += line[1:]
        else:
            if cur:
                un.append(cur)
            cur = line
    if cur:
        un.append(cur)
    descs = [x for x in un if x.startswith("DESCRIPTION:")]
    # DESCRIPTION 은 두 개다 — 일정 본문과 알람 문구. 알람 쪽은 한 줄이라
    # 줄바꿈이 없는 것이 정상이므로, 여기서는 '실제 개행이 없다' 만 공통으로
    # 검사하고 규격 표기는 여러 줄인 본문 쪽에서 확인한다.
    assert len(descs) == 2, len(descs)
    for d in descs:
        assert NL not in d and chr(13) not in d, repr(d[:60])
    body = [d for d in descs if "https://" in d]
    assert body, "링크가 담긴 일정 본문이 없다"
    assert BS + "n" in body[0], "줄바꿈이 규격 표기로 들어가야 한다"


def test_ics_uid_is_stable_per_game():
    """UID 가 바뀌면 갱신이 아니라 중복 일정으로 쌓인다."""
    a = I.build([_game()], "https://x", datetime(2026, 8, 31, 10, tzinfo=KST))
    b = I.build([_game()], "https://x", datetime(2026, 9, 1, 10, tzinfo=KST))
    uid = lambda t: [l for l in t.split(chr(13) + chr(10)) if l.startswith("UID:")][0]
    assert uid(a) == uid(b), (uid(a), uid(b))


# ── 러너 ─────────────────────────────────────────────────────────────────

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
