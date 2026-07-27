"""귀인 엔진 회귀 테스트.

여기 고정한 것들은 전부 **실측에서 실제로 틀렸던 것**이다. 각 테스트는 한 번씩
잘못된 답을 만들었던 경로를 막는다.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from argus.detection.baseline import BaselineSet
from argus.explain.attribution import (
    SHARED_HOSTS,
    attribute,
    descendants,
    group_key,
    lead_time,
    Contributor,
)
from argus.explain.bottleneck import classify
from argus.explain.changepoint import find_onset
from argus.explain.report import build_incident, render_plain
from argus.storage.hot import Database


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


def _proc(db, rows):
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "rss_mb", "io_write_bps", "handles"),
        rows,
    )


def _events(db, rows):
    db.insert_many("process_events", ("ts", "event", "pid", "ppid", "name"), rows)


# ---------------------------------------------------------------- 묶음 기준


def test_shared_hosts_are_not_merged_by_name() -> None:
    """svchost 91개를 한 덩어리로 묶으면 안 된다.

    이름이 같아도 서로 다른 서비스다. 실측에서 이 합산이 주입 프로세스를 제치고
    1위를 차지했고, "svchost 가 41%"라는 쓸모없는 답이 나왔다.
    """
    assert group_key("svchost", 100) != group_key("svchost", 200)
    assert group_key("svchost.exe", 100) != group_key("svchost.exe", 200)
    # 일반 프로그램은 합친다 — 크롬 탭 30개는 사용자에게 하나의 크롬이다.
    assert group_key("chrome", 100) == group_key("chrome", 200)
    assert "svchost" in SHARED_HOSTS


def test_same_program_is_merged_across_pids(db: Database) -> None:
    """한 프로그램의 프로세스 여럿을 합쳐야 순위가 바로 선다.

    실측: CPU 스핀의 자식 8개가 각 4.2% 라 개별로는 게임 하나(4.3%)에 밀렸다.
    합치면 33.6% 로 압도적 1위다.
    """
    base = 1_000_000.0
    rows = []
    for i in range(30):
        ts = base + i
        rows.append((ts, 999, "game", 4.3, 100.0, 0.0, 100))
        for pid in range(10, 18):  # 자식 8개
            rows.append((ts, pid, "python", 4.2, 50.0, 0.0, 50))
    # 이전 창: 게임만 돌고 있었다
    for i in range(30):
        ts = base - 200 + i
        rows.append((ts, 999, "game", 4.3, 100.0, 0.0, 100))
    _proc(db, rows)

    result = attribute(db, "cpu", before=(base - 200, base - 170), after=(base, base + 30))
    assert result[0].name == "python"
    assert len(result[0].pids) == 8
    assert result[0].delta == pytest.approx(33.6, abs=0.1)


def test_baseline_window_uses_median(db: Database) -> None:
    """'평소' 창은 중앙값으로 봐야 한다.

    실측: 비교 창에 우연한 스파이크가 하나 있으면 평균이 끌려가 델타가 왜곡되고,
    그것 때문에 순위가 뒤집혔다.
    """
    base = 1_000_000.0
    rows = []
    # A: 평소 10, 스파이크 한 번 300 (평균 19.7 / 중앙값 10)
    for i in range(30):
        rows.append((base - 200 + i, 1, "a", 300.0 if i == 5 else 10.0, 0.0, 0.0, 0))
    for i in range(30):
        rows.append((base + i, 1, "a", 20.0, 0.0, 0.0, 0))
    _proc(db, rows)

    result = attribute(db, "cpu", before=(base - 200, base - 170), after=(base, base + 30))
    assert result, "중앙값 기준이면 10 → 20 으로 상승이 잡혀야 한다"
    assert result[0].before == pytest.approx(10.0)
    assert result[0].delta == pytest.approx(10.0)


# ---------------------------------------------------------------- 트리


def test_descendants_expands_injection_answer(db: Database) -> None:
    """정답은 주입 PID 하나가 아니라 그 트리 전체다.

    주입기는 부모 하나만 기록하는데 CPU 스핀은 프로세스로 fork 한다. 부모는 CPU 를
    쓰지 않으므로, 부모만 정답으로 두면 어떤 엔진도 통과할 수 없다.
    """
    now = time.time()
    _events(
        db,
        [
            (now, "start", 100, 1, "python"),
            (now, "start", 101, 100, "python"),
            (now, "start", 102, 100, "python"),
            (now, "start", 103, 101, "python"),  # 손자
            (now, "start", 900, 1, "other"),
        ],
    )
    assert descendants(db, 100, now, now + 60) == {100, 101, 102, 103}


# ---------------------------------------------------------------- 선행성


def test_lead_time_is_none_when_already_high(db: Database) -> None:
    """조회 창 시작부터 이미 높으면 '언제 올랐는지'를 알 수 없다.

    실측: 이때 첫 표본을 답으로 내는 바람에 항상 lookback 에 가까운 값이 나와
    "255초 선행" 같은 가짜 선행이 만들어졌다. 실제 뜻은 "원래 그 수준이었다"이다.
    """
    onset = 1_000_000.0
    _proc(db, [(onset - 300 + i, 1, "a", 50.0, 0.0, 0.0, 0) for i in range(300)])

    contributor = Contributor(name="a", pids={1}, before=10.0, after=50.0)
    assert lead_time(db, contributor, "cpu", onset) is None


def test_lead_time_finds_rise(db: Database) -> None:
    onset = 1_000_000.0
    rows = []
    for i in range(300):
        ts = onset - 300 + i
        # 100초 전부터 오른다
        value = 50.0 if i >= 200 else 10.0
        rows.append((ts, 1, "a", value, 0.0, 0.0, 0))
    _proc(db, rows)

    contributor = Contributor(name="a", pids={1}, before=10.0, after=50.0)
    lead = lead_time(db, contributor, "cpu", onset)
    assert lead is not None and 95 <= lead <= 105


# ---------------------------------------------------------------- 변화점


def _stats(values: list[float]):
    baselines = BaselineSet(window_s=10_000.0, min_samples=10)
    for index, value in enumerate(values):
        baselines.observe(float(index), {"cpu_total": value})
    return baselines.stats("cpu_total")


def test_onset_survives_a_ramp() -> None:
    """서서히 오르는 부하는 임계를 들락날락한다.

    표본 하나가 임계 아래라고 끊으면 시작점이 한참 뒤로 잡히고, 그러면 "원인
    프로세스가 결과보다 늦게 올랐다"는 말이 안 되는 결론이 나온다.
    """
    stats = _stats([10.0] * 200)
    assert stats is not None

    samples: list[tuple[float, float | None]] = [(float(i), 10.0) for i in range(100)]
    # 100초부터 램프: 임계 위아래로 진동하며 오른다
    for i in range(100, 200):
        value = 10.0 + (i - 100) * 0.5
        if i % 7 == 0:
            value = 10.0  # 잠깐 내려감
        samples.append((float(i), value))

    onset = find_onset(samples, stats, signal_ts=199.0)
    assert onset is not None
    # 진동에 끊기지 않고 램프 초입 근처를 잡아야 한다
    assert onset.ts <= 140.0, f"시작점이 너무 늦다: {onset.ts}"


def test_onset_ignores_single_spike() -> None:
    stats = _stats([10.0] * 200)
    samples: list[tuple[float, float | None]] = [(float(i), 10.0) for i in range(200)]
    samples[50] = (50.0, 500.0)  # 한 번 튄 것
    assert find_onset(samples, stats, signal_ts=199.0) is None


# ---------------------------------------------------------------- 병목·리포트


def test_evidence_belongs_to_the_verdict() -> None:
    """판정과 무관한 근거가 섞이면 안 된다.

    실측: "CPU 병목 — 근거: CPU 96% · 스왑 220MB" 처럼 메모리 근거가 붙어 나왔다.
    읽는 사람이 판단을 검증할 수 없게 된다.
    """
    baselines = BaselineSet(window_s=10_000.0, min_samples=10)
    for index in range(120):
        baselines.observe(float(index), {"cpu_total": 20.0 + index % 3, "mem_percent": 30.0})

    result = classify({"cpu_total": 96.0, "mem_percent": 40.0, "swap_used_mb": 220.0}, baselines)
    assert result.kind == "CPU"
    assert not any("스왑" in e for e in result.evidence)
    assert any("CPU" in e for e in result.evidence)


def test_disk_needs_a_symptom_not_just_throughput() -> None:
    """처리량만 높은 것은 병목이 아니다.

    NVMe 에 600MB/s 를 퍼부어도 응답이 0.2ms 면 사용자는 아무것도 못 느낀다.
    """
    baselines = BaselineSet(window_s=10_000.0, min_samples=10)
    for index in range(120):
        baselines.observe(
            float(index),
            {"disk_write_bps": 1e6, "disk_resp_ms": 0.1, "disk_queue": 0.0, "cpu_total": 20.0},
        )

    quiet = classify(
        {"disk_write_bps": 6e8, "disk_resp_ms": 0.2, "disk_queue": 0.5, "cpu_total": 20.0},
        baselines,
    )
    assert quiet.kind != "IO"


def test_report_hides_lead_for_minor_contributors() -> None:
    from argus.explain.bottleneck import Bottleneck

    major = Contributor(name="python", pids={1, 2}, before=10.0, after=40.0, share=0.8, lead_s=120.0)
    minor = Contributor(name="discord", pids={3}, before=1.0, after=2.0, share=0.05, lead_s=255.0)
    incident = build_incident(
        1_000_000.0, 1_000_300.0, Bottleneck("CPU", 0.8, ["CPU 96%"], "cpu"), [major, minor]
    )
    text = render_plain(incident)
    assert "120초 선행" in text
    assert "255초 선행" not in text, "기여도가 낮은 후보의 선행성은 잡음이다"
