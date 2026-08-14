"""웜 내보내기는 자식 프로세스에서 돈다.

**`import pyarrow` 하나가 private 366MB 를 프로세스 수명 내내 붙든다**(실측
2026-08-12). 파이썬은 모듈을 프로세스가 죽을 때까지 놓지 않으므로 함수 안 임포트도
일회성이 아니라 상주 비용이 된다 — 하루 한 번 쓰는 라이브러리 때문에 관측자가
366MB 를 이고 다니는 것은 설계 규칙 1 과 정면으로 어긋난다.

**이 규칙은 되돌아가기 쉽고, 되돌아가도 조용하다.** `WarmExporter.tick()` 이
`export_pending()` 을 직접 부르게 바꾸면 결과는 똑같이 나온다. 무거워질 뿐이다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.config.loader import WarmSettings  # noqa: E402
from argus.storage import warm as warm_mod  # noqa: E402
from argus.storage.hot import Database  # noqa: E402


@pytest.fixture()
def exporter(tmp_path, monkeypatch):
    monkeypatch.setattr(warm_mod, "warm_dir", lambda: tmp_path / "warm")
    db = Database(tmp_path / "t.db").open()
    yield warm_mod.WarmExporter(db, WarmSettings()), db
    db.close()


def test_resident_process_never_imports_pyarrow(tmp_path) -> None:
    """**상주 경로에 pyarrow 가 들어오면 안 된다.**

    다른 테스트가 이미 pyarrow 를 올려 뒀을 수 있으므로 **깨끗한 프로세스**에서
    확인한다. 여기서 상주가 하는 일 중 웜과 관련된 것을 전부 한다 — 모듈 임포트,
    조회 계층(duckdb), 그리고 내보내기 컴포넌트를 만들기까지.
    """
    script = """
import sys, json
from argus.storage import warm
from argus.storage.hot import Database
from argus.config.loader import WarmSettings
import tempfile, pathlib

tmp = pathlib.Path(tempfile.mkdtemp())
db = Database(tmp / "t.db").open()
warm.WarmExporter(db, WarmSettings())
db.close()
print(json.dumps({"pyarrow": "pyarrow" in sys.modules}))
"""
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout.strip().splitlines()[-1])
    assert payload["pyarrow"] is False, "상주 경로가 pyarrow 를 끌어들였다 (+366MB)"


def test_tick_spawns_a_child_instead_of_exporting_inline(exporter, monkeypatch) -> None:
    """**직접 부르지 않고 자식을 띄운다.**

    인라인으로 되돌아가면 결과는 똑같고 메모리만 는다 — 이 테스트 말고는 신호가 없다.
    """
    component, _db = exporter
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = '{"exported": {"metrics": 3}}'
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        return _Done()

    monkeypatch.setattr(warm_mod.subprocess, "run", fake_run)
    # 인라인으로 돌아갔는지 확인하려면 그 경로에 지뢰를 놓는다.
    monkeypatch.setattr(
        warm_mod.WarmStore,
        "export_pending",
        lambda *a, **k: pytest.fail("자식이 아니라 상주 안에서 내보냈다"),
    )

    component.tick()

    assert len(calls) == 1, f"자식을 정확히 한 번 띄워야 한다: {calls}"
    assert "--export-warm" in calls[0], calls[0]


def test_child_failure_does_not_raise(exporter, monkeypatch) -> None:
    """**자식이 실패해도 상주는 계속 돈다.**

    웜 내보내기가 밀리면 SQLite 가 며칠 더 들고 있을 뿐이다. 여기서 예외가 나가면
    수퍼바이저 스레드가 죽고, 그러면 **다시는 내보내지 않는다.**
    """
    component, _db = exporter

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "무언가 터졌다"

    monkeypatch.setattr(warm_mod.subprocess, "run", lambda *a, **k: _Failed())
    component.tick()  # 예외가 없어야 한다

    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="argus", timeout=1)

    monkeypatch.setattr(warm_mod.subprocess, "run", timeout)
    component.tick()

    def cannot_spawn(*_a, **_k):
        raise OSError("실행 파일을 못 찾았다")

    monkeypatch.setattr(warm_mod.subprocess, "run", cannot_spawn)
    component.tick()


def test_child_actually_runs_under_the_resident_interpreter(tmp_path) -> None:
    """**자식이 진짜로 뜨는지 본다.** 나머지 테스트는 `subprocess.run` 을 모킹한다.

    2026-08-12 첫 배선이 여기서 걸렸다. 상주는 base `pythonw.exe` 로 돌고 venv
    경로를 `site.addsitedir()` 로 세우는데(`tools/soak_entry.py` — venv 트램폴린이
    콘솔 창을 띄우기 때문), 자식은 그 경로를 물려받지 못해
    `ModuleNotFoundError: psutil` 로 죽었다. **모킹한 테스트는 전부 통과했다** —
    자식이 뜨긴 뜨는데 아무것도 못 하고 매시간 실패 로그만 남겼다.

    상주의 조건(venv 를 모르는 인터프리터)을 그대로 재현한다: `-E` 로 부모의
    PYTHONPATH 를 끄고 `sys.path` 만 넘겨 준다.
    """
    from argus.storage.warm import export_command, export_env

    command = export_command()
    env = export_env()
    env["ARGUS_DATA_DIR"] = str(tmp_path)

    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),  # **프로젝트 밖에서 돈다** — cwd 덕에 되는 것이면 안 된다
        timeout=180,
    )

    assert done.returncode == 0, (
        f"자식이 실제로는 못 돈다 (returncode {done.returncode})\n{done.stderr[-800:]}"
    )
    payload = json.loads(done.stdout.strip().splitlines()[-1])
    assert "exported" in payload, done.stdout


def test_child_output_is_decoded_as_utf8_not_locale(exporter, monkeypatch) -> None:
    """**자식의 출력 인코딩을 로캘에 맡기지 않는다.**

    자식은 우리 자신이고 `logging_setup.setup()` 이 stdout/stderr 를 UTF-8 로
    reconfigure 한다. 부모가 `text=True` 만 쓰면 **실행 PC 의 ACP** 로 디코딩하므로
    CP949 PC 에서 어긋난다. 개발 PC 는 2026-08-15 부터 UTF-8 로캘이라
    **실행만으로는 이 회귀가 절대 드러나지 않는다** — 그래서 인자를 직접 본다.
    """
    component, _db = exporter
    seen: dict = {}

    class _Done:
        returncode = 0
        stdout = '{"exported": {"metrics": 3}}'
        stderr = ""

    def fake_run(command, **kwargs):
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(warm_mod.subprocess, "run", fake_run)
    component.tick()

    assert seen.get("encoding") == "utf-8", (
        "자식 출력 디코딩을 로캘에 맡겼다. CP949 PC 에서 stderr 가 None 이 되고 "
        f"실패 원인이 통째로 사라진다. 넘긴 인자={sorted(seen)}"
    )
    # 로그 한 줄 때문에 내보내기 결과를 잃으면 안 된다.
    assert seen.get("errors") == "replace", f"errors 를 지정하지 않았다: {sorted(seen)}"


def test_locale_decoding_really_loses_stderr() -> None:
    """위 테스트가 막는 것이 **실재하는 피해**임을 보인다.

    인자를 세는 테스트만 있으면 "왜 그 인자가 필요한가"가 남지 않는다. 여기서는
    진짜 자식을 띄워, 로캘이 어긋났을 때 `subprocess` 가 **예외를 던지지 않고
    스트림을 `None` 으로** 돌려준다는 것을 확인한다 — 이게 조용해서 위험한 이유다.
    """
    child = (
        "import sys;"
        "sys.stderr.reconfigure(encoding='utf-8', errors='replace');"
        "sys.stderr.write('디스크 병목\\n')"
    )
    argv = [sys.executable, "-c", child]

    # CP949 PC 재현. 예외가 나지 않는다는 것 자체가 요점이다.
    broken = subprocess.run(argv, capture_output=True, text=True, encoding="cp949", timeout=30)
    assert broken.stderr is None, (
        "이 테스트의 전제가 깨졌다 — 로캘 불일치가 더 이상 stderr 를 삼키지 않는다면 "
        "warm.py 의 방어 근거를 다시 확인해야 한다"
    )

    # warm.py 가 쓰는 인자. 실행 PC 로캘과 무관하게 한글이 온전히 온다.
    fixed = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
    )
    assert "디스크 병목" in fixed.stderr, repr(fixed.stderr)


def test_frozen_and_source_take_different_commands(monkeypatch) -> None:
    """**exe 와 소스 실행이 다른 명령이다.**

    exe 에서는 `sys.executable` 이 argus.exe 자신이라 `-m argus` 를 붙이면 안 된다.
    하나로 쓰면 배포판에서만 조용히 안 도는 상태가 된다(`_MEIPASS` 와 같은 자리).
    """
    monkeypatch.setattr(warm_mod, "is_frozen", lambda: False)
    source = warm_mod.export_command()
    assert source[1:] == ["-m", "argus", "--export-warm"], source

    monkeypatch.setattr(warm_mod, "is_frozen", lambda: True)
    frozen = warm_mod.export_command()
    assert "-m" not in frozen, f"exe 에 -m 을 붙였다: {frozen}"
    assert frozen[-1] == "--export-warm"
