"""창의 각 페이지를 그림 파일로 뽑는다 — **레이아웃은 눈으로만 판정된다.**

`--seconds` 스모크는 "갱신이 도는가"를 숫자로 답하지만, **겹침·잘림·여백은 그 숫자가
전부 통과한 상태에서도 일어난다.** 실제로 2026-08-06 에 그랬다 — 표본 수는 정상인데
화면은 읽을 수 없었다.

**마우스를 움직이지 않는다**(CLAUDE.md). Qt 가 자기 위젯을 스스로 그려 넘겨주는
`QWidget.grab()` 을 쓴다. 사람이 클릭하는 것을 흉내 내는 자동화가 아니다.

    python tools/ui_snapshot.py                  # 기본 크기(1240x820)
    python tools/ui_snapshot.py --size 1920x1080 # 최대화 상태
    python tools/ui_snapshot.py --out <디렉터리>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 페이지마다 데이터 로딩이 비동기라 잠깐 기다렸다 찍는다. 너무 짧으면 "연결 중…"만
# 찍히고, 그건 레이아웃 판정에 쓸 수 없다.
_SETTLE_MS = 2600


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", default="1240x820", help="창 크기 (기본 1240x820)")
    parser.add_argument("--out", default=None, help="저장 디렉터리")
    parser.add_argument("--settle-ms", type=int, default=_SETTLE_MS)
    args = parser.parse_args(argv)

    width, height = (int(v) for v in args.size.lower().split("x"))
    out = Path(args.out) if args.out else ROOT / "build" / "ui_snapshots"
    out.mkdir(parents=True, exist_ok=True)

    from PySide6 import QtCore, QtWidgets

    from argus.desktop.app import MainWindow, apply_theme, place_on_configured_screen

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    # **실제 앱과 같은 스타일을 입힌다.** 안 그러면 캡처가 다른 화면을 찍고,
    # 그 그림을 근거로 없는 문제를 고치게 된다.
    apply_theme(app)
    window = MainWindow()
    window.resize(width, height)
    placement = place_on_configured_screen(window)
    window.show()
    print(f"  창 위치: {placement} · 크기 {width}x{height}")

    saved: list[str] = []
    # 왼쪽 목록의 순서 그대로. 이름은 목록에서 읽어 와 페이지가 늘어도 따라온다.
    names = [window._nav.item(i).text() for i in range(window._nav.count())]

    def shoot(index: int) -> None:
        if index >= len(names):
            app.quit()
            return
        window._nav.setCurrentRow(index)

        def capture() -> None:
            path = out / f"{index}_{names[index].replace(' ', '')}_{width}x{height}.png"
            window.grab().save(str(path))
            saved.append(str(path))
            print(f"  [{index + 1}/{len(names)}] {names[index]} → {path.name}")
            shoot(index + 1)

        QtCore.QTimer.singleShot(args.settle_ms, capture)

    QtCore.QTimer.singleShot(600, lambda: shoot(0))
    app.exec()

    print(f"[OK] ui_snapshot — {len(saved)}장" if saved else "[FAIL] 아무것도 못 찍었다")
    return 0 if saved else 1


if __name__ == "__main__":
    os.environ.setdefault("ARGUS_UI_SCREEN", "1")  # 보조 모니터 (CLAUDE.md)
    raise SystemExit(main())
