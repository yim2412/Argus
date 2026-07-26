"""컴포넌트 스레드 생명주기 관리.

원칙 두 가지.

- **에러 격리**: 컴포넌트 하나가 예외를 던져도 나머지는 계속 돈다. 실패한 컴포넌트는
  지수 백오프로 재시도하되 프로세스를 죽이지 않는다. GPU 가 없어 NVML 수집기가
  죽었다고 CPU 수집까지 멈추면 안 된다.
- **깨끗한 종료**: Ctrl+C 하나로 모든 스레드가 정리되고 DB 가 닫혀야 한다.
  `time.sleep` 대신 `Event.wait` 로 대기해서 종료 신호에 즉시 반응한다.

스로틀은 여기서 적용한다. 각 컴포넌트의 주기에 `BudgetGuard.multiplier` 를 곱한다.
"""

from __future__ import annotations

import signal
import threading
import time
from typing import Callable

from ..logging_setup import get_logger, write_crash

log = get_logger(__name__)

# 연속 실패 시 대기 시간을 늘린다. 같은 예외로 로그를 도배하지 않기 위함.
_BACKOFF_MAX_S = 60.0


class Component:
    """주기적으로 `tick()` 이 불리는 작업 단위.

    상속해서 `tick()` 을 구현한다. `setup()`/`teardown()` 은 선택.
    `throttleable=False` 면 스로틀 배수를 적용하지 않는다(예산 가드 자신처럼
    항상 같은 주기로 돌아야 하는 것들).
    """

    name: str = "component"
    interval_s: float = 1.0
    throttleable: bool = True

    def setup(self) -> None:  # noqa: B027 - 선택적 훅
        pass

    def tick(self) -> None:
        raise NotImplementedError

    def teardown(self) -> None:  # noqa: B027 - 선택적 훅
        pass


class Supervisor:
    """컴포넌트들을 각자의 스레드에서 돌린다."""

    def __init__(self, *, multiplier_fn: Callable[[], float] | None = None) -> None:
        self._components: list[Component] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        # "종료를 요청했다"(_stop)와 "정리를 끝냈다"(_joined)는 별개다. 시그널 핸들러는
        # _stop 만 세우므로, 이 둘을 하나로 묶으면 stop() 이 조기 반환해 스레드 join 과
        # teardown 이 통째로 건너뛰어진다.
        self._joined = False
        self._join_lock = threading.Lock()
        self._multiplier_fn = multiplier_fn or (lambda: 1.0)
        self._error_counts: dict[str, int] = {}

    def add(self, component: Component) -> "Supervisor":
        self._components.append(component)
        return self

    # ------------------------------------------------------------------ 실행

    def _run_component(self, component: Component) -> None:
        name = component.name
        try:
            component.setup()
        except Exception as e:
            log.exception("컴포넌트 setup 실패 — 이 컴포넌트만 중단한다", extra={"component": name})
            write_crash(e, context=f"setup:{name}")
            return

        backoff = 0.0
        log.debug("컴포넌트 시작", extra={"component": name, "interval_s": component.interval_s})

        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                component.tick()
            except Exception:
                self._error_counts[name] = self._error_counts.get(name, 0) + 1
                count = self._error_counts[name]
                # 처음 몇 번만 전체 트레이스백을 남긴다. 이후엔 요약만.
                if count <= 3:
                    log.exception("컴포넌트 tick 실패", extra={"component": name, "errors": count})
                else:
                    log.warning(
                        "컴포넌트 tick 실패 (반복)",
                        extra={"component": name, "errors": count},
                    )
                backoff = min(_BACKOFF_MAX_S, max(component.interval_s, backoff * 2 or 1.0))
            else:
                if backoff:
                    log.info("컴포넌트 회복", extra={"component": name})
                backoff = 0.0

            interval = component.interval_s
            if component.throttleable:
                interval *= self._multiplier_fn()
            elapsed = time.perf_counter() - started
            wait = backoff if backoff else max(0.0, interval - elapsed)
            self._stop.wait(wait)

        try:
            component.teardown()
        except Exception:
            log.exception("컴포넌트 teardown 실패", extra={"component": name})
        log.debug("컴포넌트 종료", extra={"component": name})

    def start(self) -> None:
        for component in self._components:
            thread = threading.Thread(
                target=self._run_component,
                args=(component,),
                name=f"argus-{component.name}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        log.info("수퍼바이저 시작", extra={"components": [c.name for c in self._components]})

    def stop(self, timeout: float = 10.0) -> None:
        """종료를 요청하고 모든 스레드가 정리될 때까지 기다린다. 여러 번 불러도 안전하다."""
        with self._join_lock:
            if self._joined:
                return
            self._joined = True

        log.info("종료 신호 — 컴포넌트를 정리한다")
        self._stop.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
        alive = [t.name for t in self._threads if t.is_alive()]
        if alive:
            # 데몬 스레드라 프로세스 종료는 막지 않지만, 조용히 넘어가지는 않는다.
            log.warning("시간 내 종료하지 못한 스레드", extra={"threads": alive})
        else:
            log.info("모든 컴포넌트 종료 완료")

    def broadcast_time_gap(self, gap_s: float) -> list[str]:
        """모든 컴포넌트에 시간 공백을 알린다. 처리한 컴포넌트 이름을 돌려준다.

        각 컴포넌트의 처리 실패를 격리한다 — 하나가 복구에 실패해도 나머지는 복구되어야
        한다. 복구 자체를 못 한 컴포넌트는 다음 틱부터 어차피 스스로 회복하거나
        에러 격리 경로를 탄다.
        """
        handled: list[str] = []
        for component in self._components:
            handler = getattr(component, "on_time_gap", None)
            if handler is None:
                continue
            try:
                handler(gap_s)
                handled.append(component.name)
            except Exception:
                log.exception("공백 복구 실패", extra={"component": component.name})
        return handled

    def install_signal_handlers(self) -> None:
        """Ctrl+C / 종료 요청을 정상 종료로 바꾼다."""

        def handler(signum, _frame):
            log.info("시그널 수신", extra={"signal": signum})
            self._stop.set()

        signals = [signal.SIGINT, signal.SIGTERM]
        # Windows 의 Ctrl+Break. 콘솔 창을 닫거나 Ctrl+Break 를 눌러도 DB 를 정리하고
        # 나가야 한다. (SIGINT 와 달리 다른 프로세스에서 보낼 수 있어 종료 테스트에도 쓴다)
        if hasattr(signal, "SIGBREAK"):
            signals.append(signal.SIGBREAK)

        for sig in signals:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, AttributeError):
                # 메인 스레드가 아니거나 플랫폼이 지원하지 않는 경우. 치명적이지 않다.
                log.debug("시그널 핸들러 등록 실패", extra={"signal": str(sig)})

    def wait(self) -> None:
        """종료 신호가 올 때까지 블록한다."""
        try:
            while not self._stop.is_set():
                # 타임아웃을 두어야 Windows 에서 KeyboardInterrupt 가 전달된다.
                self._stop.wait(0.5)
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt")
            self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()


class CallableComponent(Component):
    """함수 하나를 컴포넌트로 감싸는 어댑터. 테스트·간단한 작업용."""

    def __init__(
        self,
        name: str,
        fn: Callable[[], None],
        interval_s: float = 1.0,
        throttleable: bool = True,
    ) -> None:
        self.name = name
        self._fn = fn
        self.interval_s = interval_s
        self.throttleable = throttleable

    def tick(self) -> None:
        self._fn()


if __name__ == "__main__":  # 스모크: python -m argus.runtime.supervisor
    from ..logging_setup import setup

    setup(level="INFO")

    counter = {"ok": 0, "bad": 0}

    def healthy() -> None:
        counter["ok"] += 1

    def failing() -> None:
        counter["bad"] += 1
        raise RuntimeError("의도된 실패 — 다른 컴포넌트는 계속 돌아야 한다")

    sup = Supervisor()
    sup.add(CallableComponent("healthy", healthy, interval_s=0.1))
    sup.add(CallableComponent("failing", failing, interval_s=0.1))
    sup.start()
    time.sleep(1.2)
    sup.stop()

    print(f"  정상 컴포넌트 tick: {counter['ok']}회")
    print(f"  실패 컴포넌트 tick: {counter['bad']}회 (백오프로 횟수가 적은 것이 정상)")
    if counter["ok"] < 5:
        print("[FAIL] 정상 컴포넌트가 충분히 돌지 않았다 — 에러 격리가 동작하지 않는다")
        raise SystemExit(1)
    print("[OK] runtime.supervisor")
