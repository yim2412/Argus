"""`python -m argus.dashboard` — streamlit 을 직접 부르지 않아도 되게 하는 래퍼.

`streamlit run <경로>` 를 사용자가 외우게 하지 않는다. 경로는 패키지 안에서
`_MEIPASS` 를 거쳐 풀리므로 exe 로 묶여도 그대로 동작한다.

환경변수:
  ARGUS_DASHBOARD_PORT      포트 (기본 8501)
  ARGUS_DASHBOARD_HEADLESS  1 이면 브라우저를 자동으로 열지 않는다 (검증용)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    app = Path(__file__).resolve().parent / "app.py"
    port = os.environ.get("ARGUS_DASHBOARD_PORT", "8501")
    headless = os.environ.get("ARGUS_DASHBOARD_HEADLESS", "0") == "1"

    argv = [
        "streamlit",
        "run",
        str(app),
        "--server.port",
        port,
        "--server.headless",
        "true" if headless else "false",
        # 사용량 통계 전송을 끈다. 개인정보는 로컬을 벗어나지 않는다는 규칙이
        # 우리 데이터뿐 아니라 우리가 쓰는 도구에도 적용된다.
        "--browser.gatherUsageStats",
        "false",
    ]

    try:
        from streamlit.web.cli import main as streamlit_main
    except ImportError:
        print(
            "[FAIL] streamlit 이 없습니다.  uv pip install streamlit plotly",
            file=sys.stderr,
        )
        return 1

    sys.argv = argv
    return int(streamlit_main(standalone_mode=False) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
