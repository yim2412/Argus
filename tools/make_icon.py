"""Argus 아이콘(.ico) 생성 — **일회성 도구다.**

산출물 `argus/assets/argus.ico` 를 커밋하므로, 앱도 빌드도 이 스크립트를 부르지 않는다.
그래서 Pillow 가 런타임·빌드 의존이 되지 않는다(선언된 의존성이 아니라 개발 환경에
우연히 들어와 있는 것뿐이다 — 여기에 기대면 남의 PC 에서 빌드가 깨진다).

아이콘을 파일로 그리는 이유는 **Windows 가 크기별로 다른 비트맵을 고르기 때문**이다.
트레이는 16px, 작업표시줄은 32px, Alt+Tab 은 256px 를 쓴다. 한 장을 축소하면
16px 에서 형체가 뭉개진다. 그래서 크기마다 선 굵기를 따로 정해 그린다.

모티프는 눈이다 — 아르고스는 눈이 백 개인 파수꾼이고, 이 프로그램이 하는 일도
지켜보는 것이다.

    python tools/make_icon.py          # argus/assets/argus.ico 를 다시 만든다
"""

from __future__ import annotations

import sys
from pathlib import Path

# 트레이 배경(밝은 테마·어두운 테마 양쪽)에서 형체가 남아야 한다. 순수 검정은
# 다크 테마에서 사라지고 순수 흰색은 라이트 테마에서 사라지므로, 중간 채도의
# 파랑을 쓰고 홍채를 밝게 둔다.
_RING = (37, 99, 235, 255)  # 눈매 — 진한 파랑
_IRIS = (56, 189, 248, 255)  # 홍채 — 밝은 하늘색
_PUPIL = (15, 23, 42, 255)  # 동공 — 거의 검정

# Windows 가 실제로 고르는 크기들. 256 은 Alt+Tab·탐색기 큰 아이콘용.
_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _draw(size: int):
    """한 크기의 아이콘 한 장. 선 굵기를 크기에 비례시켜 16px 에서도 형체가 남게 한다."""
    from PIL import Image, ImageDraw

    # 4배로 그린 뒤 줄여 계단을 없앤다(Pillow 의 원 그리기에는 안티앨리어싱이 없다).
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    cx = cy = s / 2
    # 눈매는 위아래로 눌린 타원. 가로를 꽉 채우고 세로는 60%.
    half_w = s * 0.47
    half_h = s * 0.29
    width = max(scale, int(s * 0.085))

    d.ellipse(
        (cx - half_w, cy - half_h, cx + half_w, cy + half_h),
        outline=_RING,
        width=width,
    )

    iris_r = s * 0.20
    d.ellipse((cx - iris_r, cy - iris_r, cx + iris_r, cy + iris_r), fill=_IRIS)

    pupil_r = s * 0.095
    d.ellipse((cx - pupil_r, cy - pupil_r, cx + pupil_r, cy + pupil_r), fill=_PUPIL)

    return img.resize((size, size), Image.LANCZOS)


def build(out: Path) -> Path:
    from PIL import Image  # noqa: F401  — 없으면 여기서 알아채게 한다

    layers = [_draw(n) for n in _SIZES]
    out.parent.mkdir(parents=True, exist_ok=True)
    # Pillow 는 첫 이미지에 sizes 를 주면 나머지를 스스로 축소해 버린다. 크기별로
    # 따로 그린 것을 살리려면 append_images 로 넘겨야 한다.
    layers[-1].save(out, format="ICO", sizes=[(n, n) for n in _SIZES], append_images=layers[:-1])
    return out


if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent / "argus" / "assets" / "argus.ico"
    try:
        build(target)
    except ImportError:
        print("[FAIL] Pillow 가 필요하다:  .venv\\Scripts\\pip install pillow")
        sys.exit(1)
    print(f"  {target}  ({target.stat().st_size:,} bytes · {len(_SIZES)}개 크기)")
    print("[OK] make_icon")
