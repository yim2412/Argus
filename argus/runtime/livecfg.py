"""실행 중에 바뀔 수 있는 설정.

**설정 전체를 다시 읽는 구조는 만들지 않는다.** 수집 주기나 큐 크기가 실행 중에
바뀌면 컴포넌트를 재구성해야 하는데, 그건 재시작보다 위험하다. 여기 담는 것은
**바꿔도 아무것도 재구성하지 않아도 되는 값**뿐이다.

파일을 `settings.yaml` 과 나눈 이유는 `paths.runtime_config_path()` 에 있다 —
한 줄로 요약하면 **주석을 지키기 위해서**다.

**우선순위는 이 파일이 가장 세다.** 사용자가 방금 UI 에서 누른 것이 가장 최근
의사표시이기 때문이다. 그래서 `settings.yaml` 을 손으로 고쳤는데 안 먹는 상황이
생길 수 있고, 그때 조용하면 안 된다(설계 규칙 4) — `describe()` 가 "어느 쪽이
이겼는지"를 돌려주고 트레이 툴팁과 창이 그것을 보여 준다.

두 프로세스가 같은 파일을 쓴다. 상주(트레이)와 창은 별도 프로세스라 메모리를 공유할
수 없고, 파일이 유일한 통로다. 그래서:

- **쓰기는 원자적으로 한다**(임시 파일 → `os.replace`). 반쯤 쓰인 YAML 을 상대가
  읽으면 파싱이 깨지는데, 그 순간이 하필 알림을 켜려던 순간이다.
- **읽기는 mtime 이 바뀌었을 때만.** 매 tick 파일을 파싱하면 관측자가 무거워진다
  (설계 규칙 1). `st_mtime_ns` 와 크기를 함께 보는 이유는 `.pyc` 캐시 사고와 같다 —
  같은 초 안에 일어난 변경은 초 단위 mtime 으로 안 잡힌다.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..logging_setup import get_logger
from ..paths import runtime_config_path
from .supervisor import Component

log = get_logger(__name__)

# 이 파일이 덮어쓸 수 있는 값. **여기 없는 키는 무시한다** — 사용자가 손으로 아무거나
# 적어 넣어도 설정 스키마가 오염되지 않게, 그리고 "실행 중에 바꿔도 안전한 것"의
# 목록이 코드 한 곳에 남게 한다.
LIVE_KEYS = ("notify",)


@dataclass
class LiveConfig:
    """UI 가 바꾼 값을 읽고 쓴다. **스레드 안전하다** — 트레이와 융합이 다른 스레드다."""

    path: Path = field(default_factory=runtime_config_path)
    # 파일에 값이 없을 때 쓸 값. 기동 시 `settings.detection.notify` 를 넣는다.
    defaults: dict[str, Any] = field(default_factory=dict)

    _values: dict[str, Any] = field(default_factory=dict)
    _stamp: tuple[int, int] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.reload()

    # ------------------------------------------------------------------ 읽기

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def reload(self, *, force: bool = False) -> bool:
        """파일이 바뀌었으면 다시 읽는다. 실제로 읽었으면 True.

        파일이 없는 것은 정상이다 — 아무도 UI 에서 바꾼 적이 없는 상태다.
        """
        stamp = self._file_stamp()
        if not force and stamp == self._stamp:
            return False

        values: dict[str, Any] = {}
        if stamp is not None:
            try:
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                # 깨진 파일 때문에 알림이 통째로 멈추면 안 된다. 기본값으로 돌고
                # **조용히 넘어가지 않는다**(규칙 4).
                log.warning("실행 중 설정을 읽지 못했다 — 기본값을 쓴다", extra={"error": str(exc)})
                self._stamp = stamp
                return False
            if isinstance(raw, dict):
                values = {k: v for k, v in raw.items() if k in LIVE_KEYS}

        with self._lock:
            self._values = values
            self._stamp = stamp
        return True

    def get(self, key: str) -> Any:
        """현재 값. 파일에 없으면 기동 시 받은 기본값."""
        with self._lock:
            if key in self._values:
                return self._values[key]
        return self.defaults.get(key)

    @property
    def notify_enabled(self) -> bool:
        return bool(self.get("notify"))

    def overridden(self, key: str) -> bool:
        """UI 에서 바꾼 값이 설정 파일을 이기고 있는가."""
        with self._lock:
            return key in self._values

    # ------------------------------------------------------------------ 쓰기

    def set(self, key: str, value: Any) -> None:
        """값을 바꾸고 파일에 남긴다. **메모리를 먼저 바꾼다.**

        파일 쓰기가 실패해도 이번 실행에서는 사용자가 누른 대로 동작해야 한다 —
        눌렀는데 아무 일도 안 일어나는 것이 가장 나쁘다.
        """
        if key not in LIVE_KEYS:
            raise ValueError(f"실행 중에 바꿀 수 있는 값이 아니다: {key}")

        with self._lock:
            self._values[key] = value
            snapshot = dict(self._values)

        self._write(snapshot)

    def toggle(self, key: str) -> bool:
        """현재 값을 뒤집고 새 값을 돌려준다."""
        new = not bool(self.get(key))
        self.set(key, new)
        return new

    def _write(self, values: dict[str, Any]) -> None:
        text = yaml.safe_dump(values, allow_unicode=True, sort_keys=True)
        header = (
            "# Argus 가 쓰는 파일이다. **손으로 고치지 않아도 된다** — 트레이 메뉴나\n"
            "# 창의 설정 페이지에서 바꾼 값이 여기 남는다.\n"
            "# 지우면 settings.yaml 의 값으로 돌아간다.\n"
        )
        tmp = self.path.with_suffix(".yaml.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(header + text, encoding="utf-8")
            # 원자적 교체. 반쯤 쓰인 파일을 상대 프로세스가 읽는 일을 막는다.
            os.replace(tmp, self.path)
            self._stamp = self._file_stamp()
        except OSError as exc:
            log.warning("실행 중 설정을 저장하지 못했다", extra={"error": str(exc)})

    # ------------------------------------------------------------------ 진단

    def describe(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "notify": str(self.notify_enabled),
            # 어느 쪽이 이겼는지. 설정 파일을 고쳤는데 안 먹는 이유가 여기 드러난다.
            "source": "UI" if self.overridden("notify") else "settings.yaml",
        }


@dataclass
class LiveConfigWatcher(Component):
    """파일이 바뀌었는지 주기적으로 본다.

    **이게 없으면 창에서 바꾼 값이 상주에 닿지 않는다.** 트레이에서 바꾼 것은 같은
    프로세스라 즉시 반영되지만, 창은 별도 프로세스다(창이 죽어도 수집이 계속되게
    나눠 둔 결과 — 설계 규칙 1).

    주기가 곧 반응 속도다. 2초면 사용자가 누르고 다음 사건까지 사이에 충분히 들어오고,
    하는 일은 `stat()` 한 번이라 비용이 사실상 없다.
    """

    config: LiveConfig | None = None
    name: str = "livecfg"
    interval_s: float = 2.0

    def tick(self) -> None:
        if self.config is None:
            return
        if self.config.reload():
            log.info("실행 중 설정이 바뀌었다", extra=self.config.describe())


if __name__ == "__main__":  # 스모크: python -m argus.runtime.livecfg
    import tempfile

    from ..logging_setup import setup

    setup(level="INFO")

    # **실사용 파일은 읽기만 한다.** 스모크가 상주의 설정을 건드리면, 알림이 꺼진
    # 이유가 "사용자가 껐다"인지 "내가 스모크를 돌렸다"인지 구분할 수 없게 된다.
    current = LiveConfig(defaults={"notify": True})
    print(f"  현재 notify = {current.notify_enabled}  (출처: {current.describe()['source']})")

    # 왕복 확인은 임시 파일로 한다 — 두 프로세스가 파일로 주고받는 것이 이 모듈의 요점이다.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runtime.yaml"
        writer = LiveConfig(path=path, defaults={"notify": True})
        writer.set("notify", False)

        reader = LiveConfig(path=path, defaults={"notify": True})
        ok = reader.notify_enabled is False
        print(f"  다른 프로세스가 읽은 값 = {reader.notify_enabled}  (기대 False)")

    print("[OK] runtime.livecfg" if ok else "[FAIL] runtime.livecfg")
