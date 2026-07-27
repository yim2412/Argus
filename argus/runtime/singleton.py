"""중복 실행 방지.

**왜 필요한가** — 자동 시작을 붙이면 스케줄러가 띄운 인스턴스와 개발 중 손으로 띄운
인스턴스가 같은 `argus.db` 를 동시에 쓴다. WAL 이라 즉시 깨지지는 않지만 수집이 두 배로
들어가고, 융합 워터마크·예산 가드·자기 계측이 서로 덮어쓴다. 터지지 않고 조용히
오염되는 쪽이라 며칠 뒤 데이터를 열어 보고서야 알게 된다.

**이름에 데이터 디렉터리를 섞는 이유** — `ARGUS_DATA_DIR` 로 분리해 띄우는 테스트
인스턴스는 상주 인스턴스와 DB 를 공유하지 않는다. 그것까지 막으면 개발이 막힌다.

**뮤텍스를 먼저 쓰는 이유** — PID 파일은 강제 종료되면 남는다. 커널 뮤텍스는 프로세스가
어떻게 죽든(크래시·작업 관리자 종료 포함) 핸들이 닫히며 사라지므로 유령 잠금이 생기지
않는다. pywin32 가 없는 환경에서만 PID 파일로 내려간다.

뮤텍스 이름은 `Local\\` 네임스페이스다. `Global\\` 은 SeCreateGlobalPrivilege 가 필요해
관리자 권한을 요구하게 된다(설계 규칙 4). 대신 빠른 사용자 전환으로 같은 계정이 두 세션에
로그온하면 각각 하나씩 뜬다 — 데이터 디렉터리가 같으므로 이론상 충돌이지만, 그 경우엔
PID 파일 폴백이 아니라 애초에 세션이 둘인 것이 비정상이라 여기서 막지 않는다.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..logging_setup import get_logger
from ..paths import data_dir

log = get_logger(__name__)

_ERROR_ALREADY_EXISTS = 183


class AlreadyRunning(RuntimeError):
    """다른 Argus 인스턴스가 같은 데이터 디렉터리를 이미 점유하고 있다."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _key() -> str:
    """데이터 디렉터리를 식별하는 짧은 해시.

    경로를 그대로 뮤텍스 이름에 넣을 수 없다 — 백슬래시가 네임스페이스 구분자다.
    """
    raw = str(data_dir().resolve()).lower()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


class _MutexLock:
    """커널 named mutex. 프로세스가 죽으면 자동 해제된다."""

    kind = "mutex"

    def __init__(self, key: str) -> None:
        self._name = f"Local\\Argus-{key}"
        self._handle = None

    def acquire(self) -> None:
        import win32api
        import win32event
        import winerror

        handle = win32event.CreateMutex(None, True, self._name)
        # GetLastError 는 CreateMutex 직후에 읽어야 한다. 사이에 다른 win32 호출이
        # 끼면 값이 덮인다.
        last = win32api.GetLastError()
        if last == winerror.ERROR_ALREADY_EXISTS:
            win32api.CloseHandle(handle)
            raise AlreadyRunning(f"뮤텍스 {self._name} 이(가) 이미 점유됨")
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        import win32api
        import win32event

        try:
            win32event.ReleaseMutex(self._handle)
        except Exception:  # 이미 해제됐거나 소유자가 아님 — 닫기만 하면 된다
            pass
        win32api.CloseHandle(self._handle)
        self._handle = None


class _PidFileLock:
    """pywin32 가 없을 때의 폴백.

    stale 판정을 PID 존재 여부만으로 하지 않는다 — PID 는 재사용된다. 생성 시각을 함께
    적어 두고 둘 다 맞을 때만 "살아 있다"로 본다.
    """

    kind = "pidfile"

    def __init__(self, key: str) -> None:
        self._path: Path = data_dir() / f"argus-{key}.pid"

    def _holder(self) -> str | None:
        """살아 있는 소유자가 있으면 설명 문자열, 없으면 None (파일은 지운다)."""
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            pid_s, created_s = raw.split()
            pid, created = int(pid_s), float(created_s)
        except ValueError:
            self._path.unlink(missing_ok=True)
            return None

        import psutil

        try:
            proc = psutil.Process(pid)
            # 부동소수 왕복 오차 — 초 단위 비교로 충분하다
            if abs(proc.create_time() - created) < 1.0:
                return f"PID {pid} ({proc.name()})"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        self._path.unlink(missing_ok=True)
        return None

    def acquire(self) -> None:
        holder = self._holder()
        if holder is not None:
            raise AlreadyRunning(f"{self._path.name} 을(를) {holder} 이(가) 점유 중")

        import psutil

        me = psutil.Process(os.getpid())
        self._path.write_text(f"{os.getpid()} {me.create_time()!r}", encoding="utf-8")

    def release(self) -> None:
        self._path.unlink(missing_ok=True)


def _make_lock() -> _MutexLock | _PidFileLock:
    key = _key()
    try:
        import win32event  # noqa: F401
    except ImportError:
        log.warning("pywin32 없음 — PID 파일로 중복 실행을 막는다 (강제 종료 시 잔여 가능)")
        return _PidFileLock(key)
    return _MutexLock(key)


class InstanceLock:
    """단일 인스턴스 잠금. 컨텍스트 매니저로 쓴다.

        with InstanceLock():
            ...

    이미 돌고 있으면 `AlreadyRunning` 을 던진다. 호출자가 이것을 **오류가 아니라 정상
    종료**로 처리해야 한다 — 부팅 시 자동 시작과 사용자의 수동 실행이 겹치는 것은
    실수가 아니라 흔한 일이다.
    """

    def __init__(self) -> None:
        self._lock = _make_lock()
        self.kind = self._lock.kind

    def acquire(self) -> InstanceLock:
        self._lock.acquire()
        log.debug("단일 인스턴스 잠금 획득", extra={"kind": self.kind})
        return self

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


if __name__ == "__main__":  # 스모크: python -m argus.runtime.singleton
    print(f"  데이터 디렉터리 : {data_dir()}")
    print(f"  잠금 키         : {_key()}")

    first = InstanceLock()
    try:
        first.acquire()
    except AlreadyRunning as e:
        print(f"[FAIL] 첫 획득이 막혔다 — Argus 가 이미 실행 중인가? ({e.detail})")
        raise SystemExit(1)
    print(f"  1차 획득        : OK ({first.kind})")

    # 같은 프로세스 안에서도 두 번째 획득은 막혀야 한다. 뮤텍스는 재진입 가능하지만
    # ERROR_ALREADY_EXISTS 로 판정하므로 소유자 자신도 걸린다 — 그게 의도다.
    try:
        InstanceLock().acquire()
    except AlreadyRunning as e:
        print(f"  2차 획득 차단   : OK ({e.detail})")
    else:
        print("[FAIL] 두 번째 획득이 통과했다 — 중복 실행을 못 막는다")
        raise SystemExit(1)

    first.release()
    print("  해제 후 재획득  : ", end="")
    again = InstanceLock()
    try:
        again.acquire()
    except AlreadyRunning as e:
        print(f"\n[FAIL] 해제했는데 여전히 잠겨 있다 ({e.detail})")
        raise SystemExit(1)
    again.release()
    print("OK")

    # 폴백 경로. 이 PC 에는 pywin32 가 있어 위 시나리오가 전부 뮤텍스로만 돌았다.
    # 검증하지 않으면 pywin32 없는 사용자에게서만 깨진 채로 배포된다.
    fallback = _PidFileLock(_key() + "-smoke")
    fallback.acquire()
    print(f"  폴백 1차 획득   : OK ({fallback._path.name})")
    try:
        _PidFileLock(_key() + "-smoke").acquire()
    except AlreadyRunning as e:
        print(f"  폴백 2차 차단   : OK ({e.detail})")
    else:
        fallback.release()
        print("[FAIL] PID 파일 폴백이 중복 실행을 막지 못한다")
        raise SystemExit(1)

    # stale 판정: 죽은 프로세스의 PID 가 남아 있으면 잠금을 넘겨받아야 한다.
    fallback._path.write_text("999999 1.0", encoding="utf-8")
    try:
        _PidFileLock(_key() + "-smoke").acquire()
    except AlreadyRunning as e:
        print(f"[FAIL] 죽은 소유자의 잠금을 회수하지 못한다 ({e.detail})")
        raise SystemExit(1)
    print("  폴백 stale 회수 : OK")
    fallback.release()

    print("[OK] runtime.singleton")
