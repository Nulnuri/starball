#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""아티팩트용 HTML 조각을 단독 실행 가능한 HTML 파일로 변환한다.

아티팩트는 발행 시점에 <!doctype>/<head>/<body> 골격과 최소 CSS 리셋을
자동으로 씌워준다. 그래서 원본 조각에는 그 뼈대가 없다. 파일을 그냥 꺼내
브라우저로 열면 <title>·<link>가 <body> 안에 떠 있는 상태가 되어 렌더링이
브라우저마다 달라진다. 이 스크립트가 그 골격을 직접 채운다.

    python make_standalone.py 조각.html 결과.html "설명문"
"""

from __future__ import annotations

import re
import sys

# 아티팩트 런타임이 넣어주던 몫을 대신한다.
RESET = """
  /* 단독 실행용 최소 리셋 */
  *, *::before, *::after { box-sizing: border-box; }
  img, video, svg { max-width: 100%; }
  html { -webkit-text-size-adjust: 100%; }
"""


def convert(fragment: str, description: str = "") -> str:
    title_m = re.search(r"<title>(.*?)</title>", fragment, re.S)
    if not title_m:
        raise SystemExit("조각에 <title> 이 없습니다.")
    title = title_m.group(1).strip()

    links = re.findall(r"<link [^>]+>", fragment)

    body = re.sub(r"<title>.*?</title>\s*", "", fragment, flags=re.S)
    for link in links:
        body = body.replace(link, "")

    # 첫 <style> 뒤에 리셋을 끼운다
    if "<style>" in body:
        body = body.replace("<style>", "<style>" + RESET, 1)

    desc = (f'\n<meta name="description" content="{description}">'
            if description else "")

    return (
        "<!doctype html>\n"
        '<html lang="ko">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>{desc}\n"
        + "\n".join(links)
        + "\n</head>\n<body>\n"
        + body.strip()
        + "\n</body>\n</html>\n"
    )


def main(argv: list) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    src, dst = argv[1], argv[2]
    desc = argv[3] if len(argv) > 3 else ""
    with open(src, encoding="utf-8") as f:
        out = convert(f.read(), desc)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"{dst}  ({len(out.encode('utf-8')) / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
