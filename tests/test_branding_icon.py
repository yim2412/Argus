"""아이콘과 앱 정체 — 파이썬이 아니라 Argus 로 보이는가.

2026-08-06 사용자 요청 ②. 지금까지 트레이는 시스템 느낌표 아이콘(`IDI_INFORMATION`)을
썼고, 작업표시줄·알림에는 파이썬이 발신자로 붙었다.

**여기서 검증하는 것은 배선이지 그림이 아니다.** 아이콘이 예쁜지·풍선이 실제로 뜨는지는
사람이 봐야 하고 자동화하지 않는다(CLAUDE.md). 이 파일이 잡는 것은 조용히 깨지는 쪽이다 —
자원이 빌드에서 빠지거나, `set_app_id()` 호출이 사라지거나, 호출 순서가 뒤집히는 것.
셋 다 **예외를 내지 않고 그냥 파이썬 아이콘으로 돌아간다.**

특히 순서를 따로 재는 이유: Windows 는 창이 처음 생길 때 AppUserModelID 를 읽는다.
나중에 불러도 API 는 성공(True)을 돌려주므로, **반환값만 보는 검사는 순서가 뒤집혀도
통과한다.**
"""

from __future__ import annotations

import ast
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.branding import APP_ID, set_app_id  # noqa: E402
from argus.paths import icon_path, resource_path  # noqa: E402
from argus.ui.tray import TrayIcon  # noqa: E402

SPEC_DIR = ROOT / "packaging"


# ----------------------------------------------------------------- 아이콘 자원


def _ico_sizes(path: Path) -> set[int]:
    """.ico 안에 든 이미지들의 한 변 길이. Pillow 없이 헤더만 읽는다.

    테스트가 개발 환경의 우연한 Pillow 설치에 기대면 안 된다 — Pillow 는 선언된
    의존성이 아니다(아이콘은 `tools/make_icon.py` 로 미리 만들어 커밋한다).
    """
    raw = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    assert reserved == 0 and kind == 1, f"ICO 파일이 아니다: {path}"
    sizes = set()
    for i in range(count):
        width = raw[6 + i * 16]
        # ICO 는 256 을 0 으로 적는다(한 바이트에 안 들어간다).
        sizes.add(width or 256)
    return sizes


def test_icon_resource_exists_and_has_a_tray_sized_layer() -> None:
    """**16px 판이 실제로 들어 있어야 한다.**

    한 장짜리 큰 아이콘을 넣으면 Windows 가 트레이에서 축소하는데, 눈 모양이 그
    크기에서 뭉개진다. 크기별로 따로 그린 것이 그대로 있는지 본다.
    """
    path = icon_path()
    assert path.exists(), f"아이콘 자원이 없다: {path} — tools/make_icon.py 로 만든다"

    sizes = _ico_sizes(path)
    assert 16 in sizes, f"트레이 크기(16px) 판이 없다: 들어 있는 크기 {sorted(sizes)}"
    assert 256 in sizes, f"큰 아이콘(256px) 판이 없다: {sorted(sizes)}"


def test_icon_path_goes_through_the_resource_root() -> None:
    """**`_MEIPASS` 를 거치는가.** 상대경로는 소스에서만 되고 exe 에서 조용히 깨진다.

    `resource_path` 와 같은 뿌리에서 나오는지로 확인한다 — `icon_path()` 가 직접
    `Path(__file__)` 을 쓰도록 바뀌면 여기서 갈라진다.
    """
    assert icon_path() == resource_path("assets/argus.ico")


# ----------------------------------------------------------------- 트레이 폴백


class _FakeWin32Gui:
    """`win32gui` 대역. 어느 API 로 아이콘을 얻었는지 기록한다."""

    IMAGE_ICON = 1

    def __init__(self, *, load_image_result: int = 0) -> None:
        self.load_image_args: tuple | None = None
        self.load_icon_called = False
        self._result = load_image_result

    def LoadImage(self, *args):  # noqa: N802 - win32 이름을 따른다
        self.load_image_args = args
        if self._result == 0:
            return 0
        return self._result

    def LoadIcon(self, *args):  # noqa: N802
        self.load_icon_called = True
        return 999


def _patch_win32(monkeypatch, fake: _FakeWin32Gui) -> None:
    import types

    win32con = types.SimpleNamespace(IMAGE_ICON=1, LR_LOADFROMFILE=0x0010, IDI_INFORMATION=32516)
    monkeypatch.setitem(sys.modules, "win32gui", fake)
    monkeypatch.setitem(sys.modules, "win32con", win32con)


def test_tray_loads_the_bundled_icon_at_tray_size(monkeypatch) -> None:
    """전용 아이콘을 **`LoadImage` 로, 트레이 크기를 지정해** 읽는다.

    `LoadIcon` 은 파일을 읽지 못하고 크기도 못 고른다 — 그쪽으로 돌아가면 아이콘이
    다시 시스템 것이 된다.
    """
    fake = _FakeWin32Gui(load_image_result=4242)
    _patch_win32(monkeypatch, fake)
    monkeypatch.setattr("argus.ui.tray.win32api_small_icon_size", lambda: 20)

    handle, own = TrayIcon()._load_icon()

    assert own is True and handle == 4242, "전용 아이콘을 쓰지 않았다"
    assert not fake.load_icon_called, "LoadIcon 폴백으로 갔다"
    assert fake.load_image_args is not None
    path_arg, _kind, cx, cy = (
        fake.load_image_args[1],
        fake.load_image_args[2],
        fake.load_image_args[3],
        fake.load_image_args[4],
    )
    assert str(icon_path()) == path_arg, f"엉뚱한 파일을 읽는다: {path_arg}"
    # 시스템이 알려 준 크기를 그대로 써야 한다. 16 을 박아 두면 고DPI 에서 흐려진다.
    assert (cx, cy) == (20, 20), f"트레이 크기를 시스템에서 받지 않았다: {(cx, cy)}"


def test_tray_falls_back_when_the_icon_is_missing(monkeypatch, tmp_path) -> None:
    """자원이 빠진 빌드에서도 트레이는 뜬다.

    아이콘이 없다고 트레이를 포기하면 **상주 중인지 알 방법이 사라진다** — 그게 트레이의
    첫 목적이다. 대신 폴백했다는 사실이 `describe()` 에 드러나야 한다(규칙 4).
    """
    fake = _FakeWin32Gui(load_image_result=4242)
    _patch_win32(monkeypatch, fake)
    monkeypatch.setattr("argus.paths.resource_path", lambda rel: tmp_path / "없음.ico")

    tray = TrayIcon()
    handle, own = tray._load_icon()

    assert own is False, "없는 파일을 읽고도 전용 아이콘이라고 했다"
    assert handle == 999 and fake.load_icon_called, "시스템 아이콘으로 떨어지지 않았다"
    assert fake.load_image_args is None, "존재하지 않는 파일을 읽으려 했다"

    tray._own_icon = own
    assert tray.describe()["icon"] == "시스템(폴백)", "폴백 상태를 드러내지 않는다"


def test_tray_falls_back_when_loading_fails(monkeypatch) -> None:
    """파일은 있는데 `LoadImage` 가 0 을 돌려주는 경우(손상·권한).

    **핸들 0 은 예외가 아니라 실패다.** 그대로 등록하면 아이콘 없는 트레이가 된다.
    """
    fake = _FakeWin32Gui(load_image_result=0)
    _patch_win32(monkeypatch, fake)

    handle, own = TrayIcon()._load_icon()

    assert own is False and handle == 999, "실패한 핸들을 그대로 썼다"
    assert fake.load_icon_called


def test_describe_reports_the_icon_kind() -> None:
    """상태 창구에 아이콘 종류가 있어야 한다 — 폴백은 조용하면 안 된다.

    **키 집합 전체를 여기서 고정하지 않는다.** 이 테스트가 보는 것은 아이콘 하나인데
    전체를 비교하면 무관한 키가 늘 때마다 같이 깨진다(실제로 두 번 그랬다).
    창구에 무엇이 들어 있는지는 `test_tray.py` 가 한 곳에서 고정한다.
    """
    state = TrayIcon().describe()
    assert state["icon"] in ("전용", "시스템(폴백)")


# ----------------------------------------------------------------- AppUserModelID


def test_set_app_id_calls_shell32(monkeypatch) -> None:
    """실제로 Windows 에 말하는가. **True 를 돌려주는 것만으로는 모른다.**"""
    import ctypes

    called: list[str] = []

    class _Shell32:
        def SetCurrentProcessExplicitAppUserModelID(self, app_id):  # noqa: N802
            called.append(app_id)
            return 0

    monkeypatch.setattr(
        ctypes, "windll", type("W", (), {"shell32": _Shell32()})(), raising=False
    )

    assert set_app_id() is True
    assert called == [APP_ID], f"AppUserModelID 를 넘기지 않았다: {called}"


def test_set_app_id_survives_failure(monkeypatch) -> None:
    """실패해도 앱은 계속 간다 — 아이콘 문제는 불편이지 고장이 아니다."""
    import ctypes

    class _Broken:
        @property
        def shell32(self):
            raise OSError("shell32 없음")

    monkeypatch.setattr(ctypes, "windll", _Broken(), raising=False)
    assert set_app_id() is False


# ------------------------------------------------------- 진입점 배선 (호출과 순서)


def _call_lines(path: Path, func_name: str, callee: str) -> list[int]:
    """`func_name` 안에서 `callee(...)` 를 부르는 줄 번호들.

    소스를 읽는 이유는 **부르는 쪽을 실제로 실행할 수 없기 때문**이다 — 상주 진입점은
    DB 와 캘리브레이션을 건드리고, 창 진입점은 `QApplication` 을 띄운다. 그래서
    "호출이 있는가"와 "어디에 있는가"를 AST 로 고정한다. 문자열 검색과 달리 주석과
    문서 문자열에 속지 않는다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    target = inner.func
                    name = getattr(target, "id", None) or getattr(target, "attr", None)
                    if name == callee:
                        lines.append(inner.lineno)
    return lines


def test_daemon_sets_the_app_id_before_the_tray_window() -> None:
    """상주는 **트레이 창을 만들기 전에** 정체를 밝힌다.

    뒤에 부르면 이미 만들어진 창의 그룹이 바뀌지 않아 알림 발신자가 파이썬으로 남는다.
    """
    src = ROOT / "argus" / "__main__.py"
    app_id = _call_lines(src, "run", "set_app_id")
    tray = _call_lines(src, "run", "TrayIcon")

    assert app_id, "상주가 set_app_id() 를 부르지 않는다"
    assert tray, "TrayIcon 생성이 run() 에서 사라졌다 — 이 테스트의 기준점이 없어졌다"
    assert min(app_id) < min(tray), (
        f"set_app_id() 가 트레이 생성보다 뒤에 있다 (줄 {min(app_id)} vs {min(tray)})"
    )


def test_window_sets_the_app_id_and_icon_before_qapplication() -> None:
    """창은 **`QApplication` 생성 전에** 정체를 밝히고, 아이콘을 물린다."""
    src = ROOT / "argus" / "desktop" / "app.py"
    app_id = _call_lines(src, "main", "set_app_id")
    qapp = _call_lines(src, "main", "QApplication")
    set_icon = _call_lines(src, "main", "setWindowIcon")

    assert app_id, "창이 set_app_id() 를 부르지 않는다"
    assert qapp, "QApplication 생성이 사라졌다 — 이 테스트의 기준점이 없어졌다"
    assert min(app_id) < min(qapp), (
        f"set_app_id() 가 QApplication 보다 뒤에 있다 (줄 {min(app_id)} vs {min(qapp)})"
    )
    assert set_icon, "창 아이콘을 물리지 않는다 — 작업표시줄이 파이썬으로 보인다"


# ----------------------------------------------------------------- 패키징 배선


def test_specs_ship_the_icon_both_ways() -> None:
    """**exe 자원과 동봉 파일 둘 다 필요하다.**

    `icon=` 만 넣으면 탐색기에서는 Argus 로 보이지만 트레이가 런타임에 파일을 못 찾아
    시스템 아이콘으로 떨어진다. 반대로 datas 만 넣으면 exe 파일 자체가 기본 아이콘이다.
    **빌드해 봐야 아는 종류라 여기서 고정한다.**
    """
    for name in ("argus.spec", "argus_ui.spec"):
        text = (SPEC_DIR / name).read_text(encoding="utf-8")
        assert "icon=ICON" in text, f"{name}: EXE 에 아이콘 자원을 넣지 않는다"
        assert '(ICON, "assets")' in text, f"{name}: 아이콘을 동봉하지 않는다 — 트레이가 못 읽는다"
        assert "argus.ico" in text, f"{name}: ICON 경로가 정의되어 있지 않다"


def test_package_data_includes_the_icon() -> None:
    """설치본(`pip install .`)에서도 자원이 따라가야 한다."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "assets/*.ico" in text, "package-data 에 아이콘이 빠져 설치본에서 사라진다"
