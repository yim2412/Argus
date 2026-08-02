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


# ---------------------------------------------------------------- 저량 자원


def test_stock_ignores_startup_holdings_of_new_processes(db: Database) -> None:
    """구간 중에 뜬 프로세스의 **기본 보유량**이 증가분으로 둔갑하면 안 된다.

    2026-07-29 채점에서 실제로 일어난 일이다. `compattelrunner` 13개가 주입 도중에
    뜨면서 각자 원래 갖고 있던 400 핸들이 전부 "늘어난 것"으로 계산돼 5,200 이 됐고,
    혼자 +8,313 을 낸 진짜 원인을 밀어내고 1위를 차지했다. 실제로 이 13개가 구간
    동안 늘린 핸들은 각 20~29개, 합쳐서 300 남짓이었다.

    여기서는 같은 메커니즘을 20개로 재현한다 — 옛 방식이면 10,000(20 × 500)이 잡혀
    주입 프로세스(+8,100)를 이기고, 자기 시계열로 보면 400(20 × 20)에 그친다.
    """
    base = 1_000_000.0
    rows = []
    for i in range(200):  # 비교 창: 주입 프로세스만 있고 핸들은 400 에서 평평하다
        rows.append((base - 200 + i, 1, "leaky", 0.0, 0.0, 0.0, 400))
    for i in range(300):  # 이상 구간: 주입 프로세스가 400 → 8,500
        ts = base + i
        rows.append((ts, 1, "leaky", 0.0, 0.0, 0.0, 400 + i * 27))
        for pid in range(10, 30):  # 여기서 새로 뜬다. 각자 500 을 들고 와 20 만 늘린다
            rows.append((ts, pid, "compattelrunner", 0.0, 0.0, 0.0, 500 + i // 15))
    _proc(db, rows)

    result = attribute(db, "handles", before=(base - 200, base - 170), after=(base, base + 300))

    assert result[0].name == "leaky", (
        f"새로 뜬 프로세스의 초기 보유량이 1위를 뺏았다: "
        f"{[(c.name, round(c.delta)) for c in result[:3]]}"
    )
    newcomer = next(c for c in result if c.name == "compattelrunner")
    assert newcomer.delta < 1000, (
        f"신규 프로세스 20개의 증가분이 {newcomer.delta:.0f} 로 잡혔다 — "
        "기본 보유량(각 500)이 증가분에 섞여 있다"
    )
    # 그러면서도 새로 떴다는 사실 자체는 근거로 남아야 한다.
    assert newcomer.is_new and not result[0].is_new


def test_stock_still_catches_a_process_that_grows_as_it_starts(db: Database) -> None:
    """**초기 보유량을 빼는 것과 새 프로세스를 놓치는 것은 다르다.**

    구간 중에 떠서 실제로 자원을 먹어치우는 프로세스는 여전히 1위여야 한다. 초기
    보유량을 일괄로 빼는 방식(설계 검토 때의 A안)은 여기서 원인을 통째로 놓친다 —
    그래서 그쪽을 택하지 않았다. 자기 시계열 안에서도 자라기 때문에 잡힌다.
    """
    base = 1_000_000.0
    rows = [(base - 200 + i, 1, "idle", 0.0, 0.0, 0.0, 300) for i in range(200)]
    for i in range(300):
        ts = base + i
        rows.append((ts, 1, "idle", 0.0, 0.0, 0.0, 300))
        # 구간 중에 뜨고, 뜬 뒤로 자기 시계열 안에서 100 → 8,000 으로 자란다
        rows.append((ts, 50, "hog", 0.0, 0.0, 0.0, 100 + i * 26))
    _proc(db, rows)

    result = attribute(db, "handles", before=(base - 200, base - 170), after=(base, base + 300))
    assert result[0].name == "hog"
    assert result[0].is_new


def test_stock_does_not_stitch_across_pid_reuse(db: Database) -> None:
    """PID 가 재사용되면 시계열을 끊어야 한다.

    사건 구간은 수십 분일 수 있다. 죽은 프로세스와 그 PID 를 물려받은 프로그램의
    값이 한 시계열로 이어 붙으면, 이미 반납한 양까지 증가분으로 세게 된다.

    **이름까지 같은 경우로 쓴다.** 시계열 키가 `(pid, name)` 이라 이름이 다르면
    급락 리셋이 없어도 저절로 갈린다 — 그렇게 쓰면 이 테스트는 리셋을 검증하지 않고
    통과한다. 같은 프로그램을 껐다 켜는 것(워커 재시작)은 실제로 흔하다.
    """
    base = 1_000_000.0
    rows = [(base - 200 + i, 7, "worker", 0.0, 0.0, 0.0, 5000) for i in range(200)]
    for i in range(200):  # 같은 PID·같은 이름으로 다시 뜬다: 100 에서 300 까지
        rows.append((base + i, 7, "worker", 0.0, 0.0, 0.0, 100 + i))
    _proc(db, rows)

    result = attribute(db, "handles", before=(base - 200, base - 170), after=(base, base + 200))
    hits = [c for c in result if c.name == "worker"]
    assert hits, (
        "재시작 뒤의 상승(약 200)이 잡혀야 한다 — 5,000 에서 이어 붙으면 "
        "델타가 음수가 되어 후보에서 통째로 빠진다"
    )
    assert hits[0].delta < 500, f"이어 붙어 {hits[0].delta:.0f} 로 부풀었다"


def test_flow_still_counts_a_new_process_in_full(db: Database) -> None:
    """유량은 바뀌면 안 된다 — 저량 분기가 유량까지 건드리지 않았는지 본다.

    새로 뜬 프로세스가 CPU 30% 를 먹으면 그 30% 전부가 진짜로 새로 생긴 부하다.
    저량과 달리 여기서 "자기 시계열 안의 상승분"만 세면 원인을 과소평가한다.
    """
    base = 1_000_000.0
    rows = [(base - 200 + i, 1, "idle", 1.0, 0.0, 0.0, 100) for i in range(200)]
    for i in range(100):
        rows.append((base + i, 1, "idle", 1.0, 0.0, 0.0, 100))
        rows.append((base + i, 50, "spinner", 30.0, 0.0, 0.0, 100))  # 처음부터 30% 고정
    _proc(db, rows)

    result = attribute(db, "cpu", before=(base - 200, base - 170), after=(base, base + 100))
    assert result[0].name == "spinner"
    assert result[0].delta == pytest.approx(30.0, abs=0.5), (
        "유량에서 새 프로세스의 사용량 전체가 증가분으로 잡혀야 한다"
    )


def test_is_new_means_unseen_not_idle(db: Database) -> None:
    """`is_new` 는 '관측되지 않았다'이지 '사용량이 0 이었다'가 아니다.

    예전 정의(`before == 0.0`)로는 비교 창 내내 가만히 있던 프로세스가 "이상 구간에
    새로 시작됨"으로 리포트에 찍혔다. 사용자가 엉뚱한 프로그램을 의심하게 된다.
    """
    base = 1_000_000.0
    rows = [(base - 200 + i, 1, "sleeper", 0.0, 0.0, 0.0, 100) for i in range(200)]
    for i in range(100):
        rows.append((base + i, 1, "sleeper", 20.0, 0.0, 0.0, 100))
    _proc(db, rows)

    result = attribute(db, "cpu", before=(base - 200, base - 170), after=(base, base + 100))
    assert result[0].name == "sleeper"
    assert not result[0].is_new, "비교 창에 있던 프로세스는 신규가 아니다"


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


# --------------------------------------------- 제품 경로 채점 (9번)


def test_product_scoring_uses_the_incident_not_the_label(db: Database) -> None:
    """제품 경로 채점은 **라벨에서 자원을 받지 않는다.**

    함수 경로는 `SCENARIO_RESOURCE` 에서 `handles` 를 입력으로 받아 시작한다. 제품에는
    그런 입력이 없어 병목 분류와 탐지기 주장으로 추론해야 한다. 2026-07-30 에 함수
    경로 100% 와 제품 경로 0% 가 공존했다 — 스코어보드가 제품이 하지 않는 일을 재고 있었다.
    """
    import json

    from argus.eval.attribution import score_fault_product

    now = time.time()
    start = now - 600
    end = start + 300

    db.insert_many(
        "metrics_raw",
        ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"),
        [(start - 1800 + i, 15.0 + (i % 3), 30.0, 30.0, 0.1) for i in range(1800)]
        + [(start + i, 16.0, 31.0, 30.0, 0.1) for i in range(300)],
    )
    rows = []
    for i in range(0, 200, 2):
        rows.append((start - 200 + i, 700, "leaker", 1.0, 50.0, 0, 0, 400, 4, 0))
    for i in range(0, 300, 2):
        rows.append((start + i, 700, "leaker", 1.0, 52.0, 0, 0, 400 + i * 10, 4, 0))
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "rss_mb", "io_read_bps", "io_write_bps",
         "handles", "threads", "foreground"),
        rows,
    )
    db.insert_many(
        "fault_injections",
        ("id", "scenario", "ts_start", "ts_end", "pid", "params", "ramp", "completed"),
        [(1, "handle_leak", start, end, 700, "{}", 0, 1)],
    )
    features = {
        "rule": "핸들 누수", "rules": ["핸들 누수"], "process": "leaker", "pid": 700,
        "metric": "handles", "duration_s": 280.0,
        "explain": "leaker (PID 700) 핸들 400 → 3,390개",
    }
    db.insert_many(
        "anomaly_signals", ("ts", "detector", "score", "severity", "features", "run_id"),
        [(start + 10 + i * 5, "procleak", 0.7, "warning",
          json.dumps(features, ensure_ascii=False), None) for i in range(10)],
    )
    from argus.decide.fusion import Fusion

    fusion = Fusion(db)
    fusion._set_watermark(start - 1)  # noqa: SLF001
    fusion.run_once(now=now)

    fault = dict(db.query("SELECT * FROM fault_injections WHERE id = 1")[0])
    verdict = score_fault_product(db, fault)
    assert verdict.skipped is None, f"채점이 건너뛰어졌다: {verdict.skipped}"
    assert verdict.incident_id is not None, "근거 사건이 비어 있다"
    assert verdict.resource == "handles", f"자원 추론이 틀렸다: {verdict.resource}"
    assert verdict.is_top1, f"1위가 정답이 아니다: {verdict.ranked[:2]}"
    assert "leaker" in verdict.title, f"제목: {verdict.title}"


def test_product_scoring_reports_missing_incident_separately(db: Database) -> None:
    """사건이 없으면 귀인 실패가 아니라 **탐지 실패**다. 둘을 한 수치에 섞지 않는다.

    2026-07-30 실측: 주입 7건 중 3건이 사건을 만들지 못했다. 그것을 귀인 오답으로
    세면 Phase 8 을 따로 판정할 수 없고, 조용히 빼면 사용자가 아무것도 못 받은 사실이
    사라진다. 그래서 별도 사유로 표시한다.
    """
    from argus.eval.attribution import score_fault_product

    now = time.time()
    start = now - 600
    end = start + 300
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "rss_mb", "io_read_bps", "io_write_bps",
         "handles", "threads", "foreground"),
        [(start + i, 700, "leaker", 1.0, 50.0, 0, 0, 400, 4, 0) for i in range(0, 300, 2)],
    )
    db.insert_many(
        "fault_injections",
        ("id", "scenario", "ts_start", "ts_end", "pid", "params", "ramp", "completed"),
        [(2, "handle_leak", start, end, 700, "{}", 0, 1)],
    )

    fault = dict(db.query("SELECT * FROM fault_injections WHERE id = 2")[0])
    verdict = score_fault_product(db, fault)
    assert verdict.skipped and "사건이 만들어지지 않음" in verdict.skipped
    assert not verdict.is_top1


def test_product_scoring_ignores_the_label_when_inference_differs(db: Database) -> None:
    """**라벨을 입력으로 쓰지 않는다**를 고정한다.

    라벨은 `handle_leak`(자원 `handles`)인데 그 구간에 CPU 가 실제로 치솟는다. 제품은
    구체적 병목을 우선하므로 `cpu` 로 추론해야 한다. 라벨을 그대로 쓰면 `handles` 가
    나오고, 그러면 채점이 제품이 아니라 라벨을 재는 것이다.

    두 값이 같아지는 시나리오만 테스트하면 이 구분을 검증할 수 없다 — 처음에 그렇게
    써서 라벨을 쓰도록 되돌려도 통과했다.
    """
    import json

    from argus.decide.fusion import Fusion
    from argus.eval.attribution import score_fault_product

    now = time.time()
    start = now - 600
    end = start + 300

    # CPU 가 실제로 막힌 구간 — 병목 분류가 CPU 를 낸다.
    db.insert_many(
        "metrics_raw",
        ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"),
        [(start - 1800 + i, 15.0 + (i % 3), 30.0, 30.0, 0.1) for i in range(1800)]
        + [(start + i, 96.0, 99.0, 30.0, 0.1) for i in range(300)],
    )
    rows = []
    for i in range(0, 200, 2):
        rows.append((start - 200 + i, 700, "leaker", 2.0, 50.0, 0, 0, 400, 4, 0))
    for i in range(0, 300, 2):
        rows.append((start + i, 700, "leaker", 85.0, 52.0, 0, 0, 400 + i * 10, 4, 0))
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "rss_mb", "io_read_bps", "io_write_bps",
         "handles", "threads", "foreground"),
        rows,
    )
    db.insert_many(
        "fault_injections",
        ("id", "scenario", "ts_start", "ts_end", "pid", "params", "ramp", "completed"),
        [(3, "handle_leak", start, end, 700, "{}", 0, 1)],
    )
    features = {
        "rule": "핸들 누수", "rules": ["핸들 누수"], "process": "leaker", "pid": 700,
        "metric": "handles", "duration_s": 280.0, "explain": "leaker 핸들 400 → 3,390개",
    }
    db.insert_many(
        "anomaly_signals", ("ts", "detector", "score", "severity", "features", "run_id"),
        [(start + 10 + i * 5, "procleak", 0.7, "warning",
          json.dumps(features, ensure_ascii=False), None) for i in range(10)],
    )

    fusion = Fusion(db)
    fusion._set_watermark(start - 1)  # noqa: SLF001
    fusion.run_once(now=now)

    fault = dict(db.query("SELECT * FROM fault_injections WHERE id = 3")[0])
    verdict = score_fault_product(db, fault)
    assert verdict.skipped is None, f"건너뛰어졌다: {verdict.skipped}"
    assert verdict.resource == "cpu", (
        f"라벨(handles)을 그대로 썼다 — 추론 결과는 cpu 여야 한다: {verdict.resource}"
    )


def _leak_fixture(db: Database, *, global_metrics: bool) -> dict:
    """핸들 누수 주입 하나. `global_metrics=False` 면 구간의 전역 지표만 없다.

    나머지(프로세스 메트릭·정답 PID·완료 표시)는 동일하게 둔다 — 그래야 판정 차이가
    전역 지표 하나에서만 나온다.
    """
    now = time.time()
    start = now - 600
    end = start + 300

    # 비교 창(주입 전)에는 항상 둔다. 없애는 것은 **구간 안**뿐이다 —
    # 보존 정리가 오래된 쪽부터 지우므로 실제로도 이 모양이 된다.
    global_rows = [(start - 1800 + i, 15.0, 30.0, 30.0, 0.1) for i in range(1500)]
    if global_metrics:
        global_rows += [(start + i, 16.0, 31.0, 30.0, 0.1) for i in range(300)]
    db.insert_many(
        "metrics_raw",
        ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"),
        global_rows,
    )

    rows = [(start - 200 + i, 700, "leaker", 1.0, 50.0, 0, 400) for i in range(0, 200, 2)]
    rows += [(start + i, 700, "leaker", 1.0, 52.0, 0, 400 + i * 10) for i in range(0, 300, 2)]
    _proc(db, rows)
    db.insert_many(
        "fault_injections",
        ("id", "scenario", "ts_start", "ts_end", "pid", "params", "ramp", "completed"),
        [(1, "handle_leak", start, end, 700, "{}", 0, 1)],
    )
    # 사건은 있어야 한다. 없으면 그쪽이 먼저 걸려("탐지 실패") 전역 지표 분류에
    # 닿지도 못한다 — 그 순서가 의도된 것이라 여기서 사건을 만들어 둔다.
    db.insert_many(
        "incidents",
        ("id", "ts_start", "ts_end", "severity", "bottleneck", "title"),
        [(1, start + 10, end, "warning", "NONE", "사건")],
    )
    return dict(db.query("SELECT * FROM fault_injections WHERE id = 1")[0])


def test_product_scoring_skips_windows_whose_global_metrics_were_purged(db: Database) -> None:
    """전역 지표가 지워진 구간은 **채점 불능**이지 귀인 실패가 아니다.

    `analyze_incident()` 의 병목 분류는 `metrics_raw` 를 읽는다. 그 구간이 보존 정리에
    지워졌으면 병목이 "관측 없음"이 되고 자원이 기본값으로 돌아가 결과가 무조건
    미지목이다. 그것을 0점으로 세면 탐지기가 아니라 보존 정책을 채점하는 셈이다 —
    `score_fault` 가 "프로세스 메트릭이 없음"을 빼는 것과 같은 이유다.

    2026-08-02 에 이것이 없어 07-30 배치 7건이 전부 0% 로 계산돼 제품 경로 지목률이
    0.0% 로 나왔다. 더 나쁜 것은 **그 7건이 표본에 남아 이후 실행을 영구히 끌어내린다**
    는 점이다 — 새 배치가 5/5 를 맞혀도 5/12 = 42% 라 DoD 85% 에 닿을 수 없다.
    """
    from argus.eval.attribution import score_fault_product

    verdict = score_fault_product(db, _leak_fixture(db, global_metrics=False))

    assert verdict.skipped is not None, "전역 지표가 없는데도 채점했다"
    assert "전역 지표" in verdict.skipped, f"사유가 다르다: {verdict.skipped}"


def test_product_scoring_still_runs_when_global_metrics_survive(db: Database) -> None:
    """대조 — 전역 지표가 있으면 이 사유로는 빠지지 않는다.

    이게 없으면 위 테스트는 "무조건 건너뛰는 채점"으로도 통과한다. 사건이 없어
    다른 사유로 빠지는 것은 여기서 따지지 않는다.
    """
    from argus.eval.attribution import score_fault_product

    verdict = score_fault_product(db, _leak_fixture(db, global_metrics=True))

    assert "전역 지표" not in (verdict.skipped or ""), (
        f"전역 지표가 있는데 없다고 판정했다: {verdict.skipped}"
    )
