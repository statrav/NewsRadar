# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10"]
# ///
"""NewsRadar 아이콘(app/icon.ico)을 만든다. 바로가기·시작 메뉴·알림에 쓴다.
모양: 파란 둥근 사각형 + 흰 레이더 원 2개 + 오른쪽 위 신호 점 (정리 페이지 favicon과 같음).
아이콘을 바꿀 때만 실행: uv run app/tools/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

BLUE = (49, 130, 246, 255)
WHITE = (255, 255, 255, 255)
OUT = Path(__file__).resolve().parent.parent / "icon.ico"


def draw(size: int) -> Image.Image:
    s = 4  # 크게 그린 뒤 줄여서 가장자리를 매끄럽게
    n = size * s
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = n / 64
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=14 * u, fill=BLUE)
    w = max(round(4 * u), s)
    for r in (20, 10):
        d.ellipse((32 * u - r * u, 32 * u - r * u, 32 * u + r * u, 32 * u + r * u), outline=WHITE, width=w)
    d.ellipse((46 * u - 5 * u, 18 * u - 5 * u, 46 * u + 5 * u, 18 * u + 5 * u), fill=WHITE)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    sizes = [16, 24, 32, 48, 64, 128, 256]
    draw(256).save(OUT, format="ICO", sizes=[(x, x) for x in sizes])
    print(f"{OUT} ({OUT.stat().st_size:,}바이트)")
