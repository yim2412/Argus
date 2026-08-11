"""보존 정리의 청크 삭제 (2026-08-12).

`db._lock` 은 읽기·쓰기를 함께 덮는 **전역 락**이다. DELETE 하나가 끝날 때까지 놓지
않으면 그 시간이 그대로 수집 쓰기의 지연이 된다. 실측 17일에서 10초 넘는 쓰기 지연
17건 중 12건이 보존 정리·지문 갱신 근처였고 최대 109초였다.

**조용히 깨지는 쪽이다.** 청크가 무력화돼도 행은 똑같이 지워지고 예외도 안 난다 —
달라지는 것은 락을 얼마나 오래 잡는지뿐이라 결과만 보는 테스트는 전부 통과한다.
그래서 **락을 몇 번 잡았는지**와 **한 번에 몇 행을 지웠는지**를 직접 잰다.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.config.loader import RetentionSettings  # noqa: E402
from argus.storage.hot import Database  # noqa: E402
from argus.storage.retention import Retention  # noqa: E402


class _CountingLock:
    """`db._lock` 을 감싸 획득 횟수와 보유 시간을 센다."""

    def __init__(self, inner):
        self._inner = inner
        self.acquisitions = 0
        self.holds: list[float] = []
        self._t0 = 0.0

    def __enter__(self):
        self._inner.__enter__()
        self.acquisitions += 1
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.holds.append(time.perf_counter() - self._t0)
        return self._inner.__exit__(*exc)


def _db(tmp_path: Path, rows: int, *, hours_old: float = 48.0) -> Database:
    db = Database(path=tmp_path / "t.db").open()
    now = time.time()
    old = now - hours_old * 3600
    with db._lock:  # noqa: SLF001
        db.conn.executemany(
            "INSERT INTO self_telemetry (ts, cpu_percent, rss_mb) VALUES (?, ?, ?)",
            [(old + i * 0.001, 1.0, 1.0) for i in range(rows)],
        )
        db.conn.commit()
    return db


def _settings(**kw) -> RetentionSettings:
    opts = dict(self_telemetry_days=1, delete_chunk_rows=100, delete_max_rows_per_tick=0)
    opts.update(kw)
    return RetentionSettings(**opts)


def _count(db: Database, table: str = "self_telemetry") -> int:
    return db.query(f"SELECT COUNT(*) c FROM {table}")[0]["c"]


# ------------------------------------------------------------------ 청크 로직

def test_chunking_bounds_rows_per_transaction(tmp_path):
    """한 트랜잭션에 청크 크기보다 많이 지우지 않는다 — 이것이 락 보유의 상한이다."""
    db = _db(tmp_path, 850)
    lock = _CountingLock(db._lock)  # noqa: SLF001
    db._lock = lock  # noqa: SLF001
    try:
        removed = Retention(db, _settings(delete_chunk_rows=100))._delete_chunked(
            "self_telemetry", " WHERE ts < ?", [time.time() - 3600]
        )
        assert removed == 850, f"전부 지워야 한다: {removed}"
        assert _count(db) == 0
        # 850행 / 100 = 9회(마지막은 50행이라 거기서 멈춘다). 1회면 안 나뉜 것이다.
        assert lock.acquisitions >= 9, (
            f"청크가 적용되지 않았다 — 락을 {lock.acquisitions}회만 잡았다"
        )
    finally:
        db._lock = lock._inner  # noqa: SLF001
        db.close()


def test_chunk_zero_keeps_single_transaction(tmp_path):
    """0 은 '나누지 않는다'. 되돌릴 길을 남겨 둔 것이라 그대로 동작해야 한다."""
    db = _db(tmp_path, 500)
    lock = _CountingLock(db._lock)  # noqa: SLF001
    db._lock = lock  # noqa: SLF001
    try:
        removed = Retention(db, _settings(delete_chunk_rows=0))._delete_chunked(
            "self_telemetry", " WHERE ts < ?", [time.time() - 3600]
        )
        assert removed == 500
        assert lock.acquisitions == 1, f"나누지 않아야 한다: {lock.acquisitions}회"
    finally:
        db._lock = lock._inner  # noqa: SLF001
        db.close()


def test_max_rows_per_tick_defers_rest(tmp_path):
    """틱당 상한을 넘으면 멈추고 남긴다. 다음 틱이 가져간다 — 지우는 일은 급하지 않다."""
    db = _db(tmp_path, 1000)
    try:
        retention = Retention(
            db, _settings(delete_chunk_rows=100, delete_max_rows_per_tick=300)
        )
        first = retention._delete_chunked(
            "self_telemetry", " WHERE ts < ?", [time.time() - 3600]
        )
        assert first == 300, f"상한 300 에서 멈춰야 한다: {first}"
        assert _count(db) == 700, "남은 것이 있어야 한다"

        # 다음 틱이 이어서 가져간다
        second = retention._delete_chunked(
            "self_telemetry", " WHERE ts < ?", [time.time() - 3600]
        )
        assert second == 300
        assert _count(db) == 400
    finally:
        db.close()


def test_chunking_respects_the_where_clause(tmp_path):
    """조건에 맞지 않는 행은 남는다. 청크로 쪼개면서 조건을 흘리면 데이터가 사라진다."""
    db = Database(path=tmp_path / "t.db").open()
    now = time.time()
    try:
        with db._lock:  # noqa: SLF001
            db.conn.executemany(
                "INSERT INTO self_telemetry (ts, cpu_percent, rss_mb) VALUES (?, ?, ?)",
                [(now - 48 * 3600 + i, 1.0, 1.0) for i in range(300)]
                + [(now - i, 1.0, 1.0) for i in range(300)],   # 최근 것 — 남아야 한다
            )
            db.conn.commit()
        removed = Retention(db, _settings(delete_chunk_rows=50))._delete_chunked(
            "self_telemetry", " WHERE ts < ?", [now - 3600]
        )
        assert removed == 300, f"오래된 것만 지워야 한다: {removed}"
        assert _count(db) == 300, "최근 행이 사라졌다"
    finally:
        db.close()


def test_no_infinite_loop_when_nothing_matches(tmp_path):
    """지울 것이 없으면 즉시 끝난다. 루프 종료 조건이 틀리면 여기서 멈추지 않는다."""
    db = _db(tmp_path, 10, hours_old=0.1)      # 전부 최근
    try:
        removed = Retention(db, _settings(delete_chunk_rows=100))._delete_chunked(
            "self_telemetry", " WHERE ts < ?", [time.time() - 86400]
        )
        assert removed == 0
        assert _count(db) == 10
    finally:
        db.close()


def test_purge_once_uses_chunking(tmp_path):
    """**실제 경로**가 청크를 쓰는가. `_delete_chunked` 만 테스트하면 `purge_once` 가
    옛 코드를 그대로 쓰고 있어도 통과한다."""
    db = _db(tmp_path, 450)
    lock = _CountingLock(db._lock)  # noqa: SLF001
    db._lock = lock  # noqa: SLF001
    try:
        deleted = Retention(db, _settings(delete_chunk_rows=100)).purge_once()
        assert deleted.get("self_telemetry") == 450, f"지워지지 않았다: {deleted}"
        assert lock.acquisitions >= 5, (
            f"purge_once 가 청크를 쓰지 않는다 — 락 {lock.acquisitions}회"
        )
    finally:
        db._lock = lock._inner  # noqa: SLF001
        db.close()


# ------------------------------------------------------------------ config 배선

def test_config_wiring_uses_non_default_values(tmp_path, monkeypatch):
    """YAML 을 고치면 청크 크기가 실제로 바뀐다. **기본값이 아닌 값으로 재야 의미가 있다** —
    기본(2000)으로 재면 코드 기본값과 같아 배선이 끊겨도 참이 된다."""
    from argus.config import loader

    settings_yaml = tmp_path / "settings.yaml"
    settings_yaml.write_text(
        "retention:\n"
        "  delete_chunk_rows: 37\n"
        "  delete_max_rows_per_tick: 111\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "user_config_path", lambda: settings_yaml)

    cfg = loader.load_settings().retention
    assert cfg.delete_chunk_rows == 37, f"배선 끊김: {cfg.delete_chunk_rows}"
    assert cfg.delete_max_rows_per_tick == 111

    # 값만 옮겨졌는지 확인한다 — 그 값으로 실제 청크가 갈리는가.
    db = _db(tmp_path, 100)
    lock = _CountingLock(db._lock)  # noqa: SLF001
    db._lock = lock  # noqa: SLF001
    try:
        Retention(db, cfg)._delete_chunked(
            "self_telemetry", " WHERE ts < ?", [time.time() - 3600]
        )
        # 100행 / 37 = 3회(37+37+26). 청크가 안 쓰이면 1회다.
        assert lock.acquisitions == 3, (
            f"청크 크기 37 이 판정에 쓰이지 않았다 — 락 {lock.acquisitions}회"
        )
    finally:
        db._lock = lock._inner  # noqa: SLF001
        db.close()


def test_shipped_default_enables_chunking():
    """**배포 기본값이 청크를 켜 두는가.**

    위 배선 테스트는 설정을 직접 써서 재므로(규칙: 배선은 기본값 아닌 값으로) 기본값이
    0 으로 뒤집혀도 조용하다 — mutation 에서 실제로 그랬다. 그러면 남의 PC 에서는
    청크가 영영 돌지 않으면서 테스트는 전부 통과한다.

    이건 배선이 아니라 **기본값 자체가 옳은지**를 재는 것이고, 둘 다 필요하다.
    """
    import yaml

    from argus.config.loader import RetentionSettings, load_settings
    from argus.paths import resource_path

    cfg = load_settings().retention
    assert cfg.delete_chunk_rows > 0, (
        "배포 기본값이 청크를 끄고 있다 — 전역 락 보유가 다시 밀린 양에 비례한다"
    )
    assert cfg.delete_max_rows_per_tick > 0, "틱당 상한이 꺼져 있다"

    # 코드 기본값과 YAML 이 어긋나면 어느 쪽이 진짜인지 알 수 없다.
    with open(resource_path("config/defaults.yaml"), encoding="utf-8") as fh:
        on_disk = (yaml.safe_load(fh) or {}).get("retention") or {}
    code = RetentionSettings()
    assert on_disk.get("delete_chunk_rows") == code.delete_chunk_rows
    assert on_disk.get("delete_max_rows_per_tick") == code.delete_max_rows_per_tick


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
