"""이 프로그램이 Windows 에 자신을 뭐라고 소개하는가.

**상주와 창은 별도 프로세스지만 사용자에게는 한 앱이다.** 그래서 여기 값을 둘 다 쓴다.
따로 두면 작업표시줄에 두 그룹으로 갈라지고, 알림 발신자도 서로 다른 앱이 된다.

`AppUserModelID` 를 지정하지 않으면 Windows 는 **실행 파일 기준으로 정체를 추론한다.**
소스로 띄우면 그 실행 파일이 `python.exe` 라, 작업표시줄과 알림에 파이썬이 발신자로
붙는다(2026-08-06 사용자 요청 ②). exe 로 묶으면 절반은 해결되지만, 나머지 절반 —
알림 그룹과 아이콘 — 은 이 ID 를 명시해야 닫힌다.

형식은 Microsoft 권고인 `회사.제품.하위제품.버전` 이다. 하위제품을 나누지 않는 이유는
위와 같다 — 상주와 창을 한 앱으로 묶는 것이 목적이다.
"""

from __future__ import annotations

from .logging_setup import get_logger

log = get_logger(__name__)

APP_ID = "Argus.PerformanceMonitor"
APP_TITLE = "Argus"


def set_app_id(app_id: str = APP_ID) -> bool:
    """이 프로세스의 AppUserModelID 를 정한다. 성공하면 True.

    **창을 만들기 전에 불러야 한다.** Windows 는 창이 처음 생길 때 이 값을 읽어
    작업표시줄 그룹을 정하고, 그 뒤 바꿔도 이미 생긴 창에는 반영되지 않는다.

    실패해도 계속 간다 — 아이콘이 파이썬으로 보이는 것은 불편이지 고장이 아니다.
    다만 조용히 넘어가지 않는다(설계 규칙 4).
    """
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception as exc:  # 비-Windows, 또는 shell32 가 없는 환경
        log.debug("AppUserModelID 를 설정하지 못했다", extra={"error": str(exc)})
        return False


if __name__ == "__main__":  # 스모크: python -m argus.branding
    ok = set_app_id()
    print(f"  app_id = {APP_ID}  설정 = {ok}")
    print("[OK] branding" if ok else "[FAIL] branding")
