"""등급 역전의 착수 조건은 **미탐 라벨**만 센다 (2026-08-25).

**왜 이 파일이 생겼나.** `readiness.py` 의 주석은 처음부터 *"등급 역전은 안 나간 것이
나갔어야 했다는 실패라 증거가 미탐에만 있다"* 라고 적혀 있었는데, **쿼리에는 그 조건이
없었다.** 알림이 나간 사건의 라벨까지 같이 세고 있었다.

**여태 안 보인 이유가 중요하다.** 08-24 까지 THERMAL 라벨 3건이 전부 `notified=0`
이라 필터가 있든 없든 결과가 같았다. 08-23 에 처음으로 알림이 나간 THERMAL 사건이
생기면서 비로소 갈렸다 — 두 값이 우연히 같아 배선이 끊겨도 참이던 경우와 같은 구조다.
그대로 뒀으면 그 2건에 답하는 순간 6건이 차서 `[착수 가능]` 이 떴을 것이고, 착수했을
때 손에 쥔 근거는 4건이었을 것이다.

**`tools/` 는 테스트 밖이라 무력화 스윕이 못 잡는 자리다.** 그래서 여기에 둔다.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from argus.storage.hot import Database  # noqa: E402

_AXIS = "THERMAL"


def _seed(db_dir: Path, unnotified: int, notified: int) -> None:
    """THERMAL 축 라벨을 미탐/발송으로 나눠 심는다."""
    db = Database(db_dir / "argus.db")
    db.open()
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(incidents)")]
    now = time.time()
    rows = []
    for i in range(unnotified + notified):
        is_notified = 1 if i >= unnotified else 0
        values = {
            "id": 900 + i,
            "ts_start": now - 86400 * (i + 1),
            "ts_end": now - 86400 * (i + 1) + 30,
            "severity": "warning" if is_notified else "info",
            "bottleneck": _AXIS,
            "title": "발열",
            "explanation_md": "",
            "contributors": "[]",
            "evidence": '["GPU 90C"]',
            "detectors": '["메모리 이상 증가"]',
            "signal_count": 1,
            "peak_score": 0.5,
            "notified": is_notified,
            "user_label": "real",
            "labeled_at": now,
        }
        rows.append(tuple(values.get(c) for c in cols))
    db.conn.executemany(
        f"INSERT INTO incidents ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        rows,
    )
    db.conn.commit()
    db.close()


def _counted(tmp_path: Path, monkeypatch, unnotified: int, notified: int):
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    _seed(tmp_path, unnotified, notified)
    import readiness

    result = readiness.check_severity_inversion(readiness._days())
    return result.checks[0]


def test_notified_labels_do_not_count(tmp_path, monkeypatch):
    """알림이 나간 라벨은 세지 않는다 — 등급 역전에 대해 아무 말도 하지 않는다."""
    check = _counted(tmp_path, monkeypatch, unnotified=3, notified=3)
    assert not check.ok, "라벨 6건이지만 미탐은 3건뿐이라 착수 조건은 아직 아니다"
    assert "현재 3건" in check.detail


def test_unnotified_labels_do_count(tmp_path, monkeypatch):
    """미탐만 6건이면 조건이 찬다 — 위 테스트의 대조 짝.

    이것이 없으면 `total` 을 통째로 0 으로 만들어도 위 테스트가 통과한다.
    """
    check = _counted(tmp_path, monkeypatch, unnotified=6, notified=0)
    assert check.ok, "미탐 6건이면 착수 조건이 충족돼야 한다"
    assert "현재 6건" in check.detail


def test_the_two_cases_are_actually_different(tmp_path, monkeypatch):
    """같은 라벨 수(6)인데 미탐 구성만 다르면 판정이 갈려야 한다.

    **이 단언이 이 파일의 핵심이다.** 앞의 두 테스트는 조건을 따로 재지만, 필터가
    실제로 무엇을 가르는지는 *같은 총량에서 구성만 바꿔* 봐야 드러난다.
    """
    mixed = _counted(tmp_path, monkeypatch, unnotified=3, notified=3)
    assert not mixed.ok


def test_skipped_count_is_shown(tmp_path, monkeypatch):
    """세지 않은 것을 숫자로 보여 준다.

    조용히 빼면 "답했는데 왜 안 늘지"가 되고, 그때 사람은 조건이 아니라 도구를
    의심하게 된다.
    """
    check = _counted(tmp_path, monkeypatch, unnotified=3, notified=2)
    assert "2건은 세지 않았다" in check.detail
