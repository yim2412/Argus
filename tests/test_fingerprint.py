"""프로세스 지문(Phase 6-B) 검증.

**지문은 조용히 틀리는 쪽이다.** 분위수 계산이 잘못돼도 예외가 나지 않고 숫자만
그럴듯하게 달라진다. 그래서 여기서는 **알려진 분포를 넣어 기대값과 대조한다** —
"돌아간다"가 아니라 "맞다"를 확인해야 한다.

억제 쪽에서 특히 조심할 것은 **방향**이다. 지문이 없을 때 막아 버리면 신규 프로세스의
누수를 통째로 놓친다(6-A 에서 추적 상한 때문에 실제로 겪었다). 그래서 "지문이 없으면
막지 않는다"를 별도로 고정한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.detection import fingerprint as fpmod  # noqa: E402
from argus.detection.base import Observation, ProcessView, run_detector  # noqa: E402
from argus.detection.fingerprint import Fingerprint, quantile  # noqa: E402
from argus.detection.procleak import ProcessLeakDetector  # noqa: E402


def fp(name: str, stat: str, p99: float) -> Fingerprint:
    return Fingerprint(name=name, stat=stat, p50=p99 / 4, p95=p99 / 2, p99=p99,
                       maximum=p99 * 1.5, samples=200, days=3)


def leak_stream(values, *, pid=42, name="leaky", attr="handles"):
    return [
        Observation(ts=1000.0 + i, processes=[ProcessView(pid=pid, name=name, **{attr: v})])
        for i, v in enumerate(values)
    ]


# --------------------------------------------------------------- 분위수

def test_quantile_matches_known_distribution():
    """1..100 에서 p50=50, p99=99. 보간이 아니라 **실제 관측된 값**이어야 한다."""
    values = [float(i) for i in range(1, 101)]
    assert quantile(values, 0.50) == 50.0
    assert quantile(values, 0.95) == 95.0
    assert quantile(values, 0.99) == 99.0


def test_quantile_returns_observed_values_only():
    """지문의 기준은 "실제로 도달한 수준"이다. 없는 값을 만들어 내면 안 된다."""
    values = [10.0, 20.0, 1000.0]
    for p in (0.0, 0.25, 0.5, 0.75, 0.99, 1.0):
        assert quantile(values, p) in values


def test_quantile_is_monotonic():
    """p50 ≤ p95 ≤ p99 가 깨지면 계산이 틀린 것이다 — 예외 없이 조용히 틀린다."""
    import random

    random.seed(3)
    values = [random.random() * 1000 for _ in range(500)]
    assert quantile(values, 0.5) <= quantile(values, 0.95) <= quantile(values, 0.99)


def test_quantile_rejects_empty():
    with pytest.raises(ValueError):
        quantile([], 0.5)


# --------------------------------------------------------------- 억제 방향

def test_suppresses_when_within_normal():
    """평소 범위 안이면 막는다 — medal 핸들 건(도달 1,395 / 평소 p99 12,466)."""
    detector = ProcessLeakDetector()
    detector.fingerprints = {("leaky", "handles_max"): fp("leaky", "handles_max", 12466)}
    fired = run_detector(detector, leak_stream([400 + i * 10 for i in range(600)]))
    assert not fired, "평소 범위인데 발화했다"
    assert detector.suppressed > 0, "억제 카운터가 오르지 않았다"


def test_does_not_suppress_when_above_normal():
    """평소를 넘으면 막지 않는다 — 주입 건(도달 8,523 / 평소 p99 2,768)."""
    detector = ProcessLeakDetector()
    detector.fingerprints = {("leaky", "handles_max"): fp("leaky", "handles_max", 2768)}
    fired = run_detector(detector, leak_stream([400 + i * 10 for i in range(600)]))
    assert fired, "평소를 넘었는데 억제됐다 — 진짜 누수를 놓친다"


# --------------------------------------------------------------- 등급 (위험 축)

def test_severity_rises_with_position_against_own_p99():
    """**등급이 누수 규모를 따라간다.**

    지금까지 `procleak` 은 warning 고정이라, 평소 상한의 3.5배로 자라는 주입과
    평소의 절반도 안 되는 정상 동작이 같은 등급이었다. 배수(`ratio`)로는 가를 수
    없다 — 실측에서 가장 높은 배수(27.8)가 정상 프로세스였다.

    같은 시계열에 지문만 바꿔 등급이 갈리는지 본다. 시계열이 같으므로 **등급을
    가르는 것이 지문 대비 위치뿐**임이 확정된다.

    기준값은 **발화 시점의 값(3,030)** 이지 시계열의 끝(6,390)이 아니다. 지속 조건을
    채우는 5분 시점에 발화하기 때문이다 — 끝값으로 기대를 적으면 테스트가 실제와
    다른 것을 재게 된다(처음 이 테스트를 쓸 때 실제로 그랬다).
    """
    stream = leak_stream([400 + i * 10 for i in range(600)])

    # 평소 상한의 세 배 (3,030 / 1,000)
    hot = ProcessLeakDetector()
    hot.fingerprints = {("leaky", "handles_max"): fp("leaky", "handles_max", 1000)}
    fired = run_detector(hot, stream)
    assert fired and fired[0].severity == "critical", (
        f"평소 상한의 3배인데 {fired and fired[0].severity} 다"
    )

    # 평소 상한을 넘기는 하지만 두 배는 아니다 (3,030 / 2,768 = 1.09배)
    warm = ProcessLeakDetector()
    warm.fingerprints = {("leaky", "handles_max"): fp("leaky", "handles_max", 2768)}
    fired = run_detector(warm, stream)
    assert fired and fired[0].severity == "warning", (
        f"평소 상한의 1.09배인데 {fired and fired[0].severity} 다"
    )


def test_severity_falls_back_to_warning_without_a_fingerprint():
    """**지문이 없으면 등급을 낮추지도 올리지도 않는다.**

    억제와 같은 방향이다 — 모르는 것을 조용히 info 로 내리면 진짜 누수가 묻히고,
    critical 로 올리면 신규 프로세스마다 운다. 지금까지의 고정값이 warning 이었으므로
    거기 머무는 것이 변화 없는 선택이다.
    """
    detector = ProcessLeakDetector()
    detector.fingerprints = {}
    fired = run_detector(detector, leak_stream([400 + i * 10 for i in range(600)]))
    assert fired and fired[0].severity == "warning"
    assert "지문 없음" in fired[0].features["severity_reason"]


def test_severity_threshold_comes_from_config():
    """문턱이 **config 에서** 온다. 값을 모듈에 박으면 튜닝이 코드 수정이 된다.

    `test_config_thresholds.py` 가 같은 것을 다른 자리에서 본다 — 거기는 판정 함수,
    여기는 탐지기까지 흘러가는 경로다. 2026-08-03 mutation 에서 `procleak` 의 단조성이
    로직만 검증되고 배선이 비어 있던 것이 정확히 이 차이였다.
    """
    from argus.config.loader import SeveritySettings

    stream = leak_stream([400 + i * 10 for i in range(600)])
    prints = {("leaky", "handles_max"): fp("leaky", "handles_max", 1000)}

    strict = ProcessLeakDetector(severity=SeveritySettings(risk_critical_ratio=10.0))
    strict.fingerprints = dict(prints)
    fired = run_detector(strict, stream)
    assert fired and fired[0].severity == "warning", (
        f"critical 문턱을 10배로 올렸는데 {fired and fired[0].severity} 다 — 설정이 닿지 않았다"
    )


def test_no_fingerprint_means_no_suppression():
    """**지문이 없으면 막지 않는다.** 모르는 것을 막는 방향으로는 틀지 않는다.

    6-A 에서 추적 상한 때문에 신규 프로세스를 통째로 놓친 적이 있다. 누수는 대개
    새로 뜬 프로세스에서 생기므로, 여기서 같은 실수를 반복하면 안 된다.
    """
    detector = ProcessLeakDetector()
    detector.fingerprints = {}
    fired = run_detector(detector, leak_stream([400 + i * 10 for i in range(600)]))
    assert fired, "지문이 없는데 억제됐다"
    assert detector.suppressed == 0


def test_fingerprint_of_another_process_is_not_applied():
    """이름이 다르면 남의 지문을 쓰면 안 된다."""
    detector = ProcessLeakDetector()
    detector.fingerprints = {("other", "handles_max"): fp("other", "handles_max", 999999)}
    fired = run_detector(detector, leak_stream([400 + i * 10 for i in range(600)]))
    assert fired, "다른 프로세스의 지문으로 억제했다"


def test_fingerprint_of_another_metric_is_not_applied():
    """핸들 지문으로 메모리를 억제하면 안 된다 — 단위가 다르다."""
    detector = ProcessLeakDetector()
    detector.fingerprints = {("leaky", "rss_p95"): fp("leaky", "rss_p95", 999999)}
    fired = run_detector(detector, leak_stream([400 + i * 10 for i in range(600)]))
    assert fired, "다른 지표의 지문으로 억제했다"


# --------------------------------------------------------------- 자격 조건


def _fake_source(monkeypatch, *, days: dict[str, int], buckets: int, name: str = "app.exe"):
    """`build()` 에 데이터를 공급하는 두 지점을 갈아끼운다.

    자격 조건은 지금까지 테스트가 닿지 않았다 — `build()` 가 실제 DB(핫+웜)를 직접
    읽어서, 조건을 0 으로 만들어도 10개 전부 통과했다. 여기서 데이터를 손으로 준다.
    `days` 는 {날짜: 그 날의 5분 버킷 수}, `buckets` 는 통계량 표본 수다.
    """
    monkeypatch.setattr(fpmod, "_series", lambda stat, exclude=None: {name: [100.0] * buckets})
    monkeypatch.setattr(
        fpmod.history, "process_day_index", lambda exclude=None: {name: days}
    )


def _build_one(monkeypatch, **kwargs):
    _fake_source(monkeypatch, **kwargs)
    return fpmod.build(stats=("handles_max",))


def test_build_rejects_too_few_days(monkeypatch):
    """이틀만 보인 프로그램은 지문이 되지 않는다 — 요일 차이를 아직 못 봤다."""
    assert _build_one(monkeypatch, days={"2026-07-28": 100, "2026-07-29": 100}, buckets=200) == []


def test_build_accepts_at_minimum_days(monkeypatch):
    prints = _build_one(
        monkeypatch,
        days={"2026-07-27": 100, "2026-07-28": 100, "2026-07-29": 100},
        buckets=300,
    )
    assert [p.days for p in prints] == [3]


def test_build_rejects_too_few_buckets(monkeypatch):
    """날짜 수를 채웠어도 표본이 적으면 p99 가 성립하지 않는다."""
    days = {f"2026-07-2{i}": 100 for i in range(1, 8)}
    assert _build_one(monkeypatch, days=days, buckets=99) == []
    assert len(_build_one(monkeypatch, days=days, buckets=100)) == 1


@pytest.mark.xfail(
    strict=True,
    reason="MIN_DAY_HOURS 가 build() 에서 읽히지 않는다 — 2026-07-30 확인, 수정은 "
           "리플레이 평가와 함께 올린다",
)
def test_build_does_not_count_a_five_minute_day_as_a_day(monkeypatch):
    """5분만 켜 둔 날을 하루로 세면 안 된다 (`MIN_DAY_HOURS` = 6시간).

    "2시간 켜 둔 날과 12시간 켜 둔 날은 같은 하루가 아니다"가 상수의 근거인데,
    그 조건이 실제로 걸리는지 여기서 고정한다.

    `strict=True` 로 둔 이유: 수정이 들어가 통과하기 시작하면 이 표시가 **실패로
    바뀌어** 지워야 한다는 것을 알려 준다. 조용히 통과하면 표시가 남아 다음 회귀를 덮는다.
    """
    prints = _build_one(
        monkeypatch,
        days={"2026-07-27": 100, "2026-07-28": 100, "2026-07-29": 1},
        buckets=201,
    )
    assert prints == [], "5분짜리 날이 하루로 세어져 자격을 넘겼다"


def test_reset_keeps_fingerprints():
    """지문은 상태가 아니라 학습 결과다. reset 으로 버리면 리플레이 재현성이 깨진다."""
    detector = ProcessLeakDetector()
    detector.fingerprints = {("leaky", "handles_max"): fp("leaky", "handles_max", 100)}
    detector.reset()
    assert detector.fingerprints, "reset 이 지문까지 버렸다"


# --------------------------------------------------------- 결함 주입 제외 (13번)


def test_exclusion_clause_binds_values_and_covers_every_window():
    from argus.storage.history import exclusion_clause

    clause, params = exclusion_clause([(10.0, 20.0), (30.0, 40.0)], bucket_s=5.0)
    assert clause.count("AND NOT") == 2
    # 겹침 판정이라 (상한, 하한 - 버킷) 순으로 바인딩된다.
    assert params == [20.0, 5.0, 40.0, 25.0]
    assert "10" not in clause, "값이 SQL 에 박혔다 — 바인딩해야 한다"

    empty, no_params = exclusion_clause([])
    assert empty == "" and no_params == []


def _excluded(ts: float, windows, bucket_s: float) -> bool:
    """`exclusion_clause` 가 만든 조건을 그대로 평가한다. SQL 을 파이썬으로 옮겨
    적으면 두 규칙이 갈리므로, 파라미터를 받아 같은 비교를 한다."""
    from argus.storage.history import exclusion_clause

    _clause, params = exclusion_clause(windows, bucket_s=bucket_s)
    for i in range(0, len(params), 2):
        upper, lower = params[i], params[i + 1]
        if ts < upper and ts > lower:  # AND NOT (col < ? AND col > ?)
            return True
    return False


def test_exclusion_drops_buckets_that_merely_overlap_the_window():
    """**버킷이 구간과 겹치기만 해도 빠져야 한다.**

    버킷 값은 `[ts, ts + 300)` 을 대표하므로, 시작점이 구간 밖이어도 그 5분 안에서
    주입이 시작됐으면 결함이 값에 들어 있다. 2026-08-03 에 이것이 실제로 지문을
    오염시켰다 — 제외를 켜 두고도 `python` 의 p99 가 정상 상한(2,768) 대신 4,533
    이었고, 남은 두 버킷이 정확히 주입 시작을 담은 경계 버킷이었다.
    """
    # 버킷 경계가 …000 · …300 · …600 일 때, 주입은 버킷 한가운데(+100)에서 시작한다.
    # 이것이 08-02 #53 의 모양이다 — 14:30 버킷이 도는 중에 14:31 주입이 시작됐다.
    window = [(1_000_100.0, 1_000_820.0)]

    assert _excluded(1_000_000.0, window, 300.0), (
        "주입 시작을 담은 경계 버킷이 남는다 — 지문이 결함을 평소로 배운다"
    )
    # 구간 안쪽 버킷은 당연히 빠진다.
    assert _excluded(1_000_300.0, window, 300.0)
    # 완전히 벗어난 버킷은 남아야 한다. 여유까지 빼면 정상 표본이 줄어든다.
    # 버킷 [999,700, 1,000,000) 은 주입 시작(1,000,100)을 담지 않는다.
    assert not _excluded(999_700.0, window, 300.0), "겹치지 않는 앞 버킷까지 뺐다"
    assert not _excluded(1_000_900.0, window, 300.0), "구간이 끝난 뒤 버킷까지 뺐다"


def test_build_excludes_fault_windows_from_the_distribution(monkeypatch):
    """**주입 구간을 학습하면 지문이 결함을 평소로 배운다.**

    2026-07-30 실측: 07-29 주입(상한 5,000)이 `python` 의 handles_max p99 를 4,755 로
    올려, 07-30 주입(최대 4,194)이 그 안에 들어가 억제됐다. 같은 데이터를 리플레이해도
    발화하지 않아 회귀 판정이 성립하지 않았다.
    """
    # 평소 400, 주입 구간에만 5,000. 제외하면 p99 가 400 대에 머물러야 한다.
    normal = [(float(1000 + i * 300), 400.0) for i in range(200)]
    spike = [(float(900_000 + i * 300), 5000.0) for i in range(60)]

    def fake_series(stat, exclude=None):
        from argus.storage.history import exclusion_clause  # 형식만 확인

        exclusion_clause(exclude or [])
        rows = normal + spike
        if exclude:
            rows = [
                (ts, v) for ts, v in rows
                if not any(lo <= ts < hi for lo, hi in exclude)
            ]
        return {"app.exe": [v for _, v in rows]}

    days = {f"2026-07-{d:02d}": 100 for d in range(1, 8)}
    monkeypatch.setattr(fpmod, "_series", fake_series)
    monkeypatch.setattr(fpmod.history, "process_day_index", lambda exclude=None: {"app.exe": days})

    without = fpmod.build(stats=("handles_max",))
    assert without and without[0].maximum == 5000.0, "제외 없이는 주입값이 들어와야 한다"

    window = [(900_000.0, 900_000.0 + 60 * 300)]
    with_exclusion = fpmod.build(stats=("handles_max",), exclude=window)
    assert with_exclusion, "제외 후 지문이 사라졌다"
    assert with_exclusion[0].maximum == 400.0, (
        f"주입 구간이 분포에 남았다: max {with_exclusion[0].maximum}"
    )


def test_fault_windows_survives_a_db_without_the_table():
    """주입 테이블이 없는 DB(구버전)에서도 지문 생성이 죽지 않는다."""

    class Broken:
        def query(self, *a, **k):
            raise RuntimeError("no such table")

    assert fpmod.fault_windows(Broken()) == []


def test_medal_case_stays_suppressed():
    """**(나)의 회귀 방지선.** 지문의 원래 목적을 깨뜨리지 않는지 본다.

    실측 사례: medal(게임 녹화)이 핸들 383 → 1,395 로 늘어 발화했는데 medal 의 평소
    handles_max p99 는 12,466 이었다. 이건 억제되는 것이 맞다 — 자기 평소 범위 안에서
    움직인 것이고, 알리면 오탐이다.

    억제 축을 손볼 때 이 케이스가 깨지면 그 변경은 틀린 것이다. 오늘 주입(400 → 4,194,
    p99 4,755)과 **구조가 같아** 수준만으로는 둘을 가를 수 없다 — 차이는 medal 의 p99 가
    정당하게 높고 주입의 p99 는 과거 누수가 부풀린 값이라는 데 있다. 그래서 (가)가
    본질이고 (나)는 이 선을 넘지 않는 범위에서만 가능하다.
    """
    detector = ProcessLeakDetector()
    detector.fingerprints = {("medal", "handles_max"): fp("medal", "handles_max", 12466)}
    fired = run_detector(detector, leak_stream([383 + i * 2 for i in range(600)], name="medal"))
    assert not fired, "medal 오탐이 돌아왔다 — 지문 억제의 존재 이유가 깨졌다"


def test_save_removes_fingerprints_that_no_longer_qualify(tmp_path):
    """자격을 잃은 지문이 남아 있으면 계속 억제한다.

    2026-07-30: 주입 구간을 학습에서 빼자 `python` 이 자격 미달로 사라졌는데,
    `replace=True` 는 없어진 행을 지우지 않아 오염된 p99 4,755 가 DB 에 남았다.
    """
    from argus.detection.fingerprint import load, save
    from argus.storage.hot import Database

    with Database(tmp_path / "t.db") as db:
        assert save(db, [fp("python", "handles_max", 4755), fp("medal", "handles_max", 12466)]) == 2
        assert set(load(db)) == {("python", "handles_max"), ("medal", "handles_max")}

        # 다음 빌드에서 python 이 자격을 잃었다
        assert save(db, [fp("medal", "handles_max", 12466)]) == 1
        assert set(load(db)) == {("medal", "handles_max")}, "사라진 지문이 남아 있다"


def test_save_keeps_existing_fingerprints_when_build_is_empty(tmp_path):
    """빈 결과로 테이블을 비우면 억제가 통째로 사라져 오탐이 쏟아진다.

    롤업이 아직 안 돌았거나 웜 조회가 실패해 결과가 빌 수 있고, 그것과 "지문이 정말
    하나도 없다"를 구분할 방법이 없다. 안전한 쪽을 택한다.
    """
    from argus.detection.fingerprint import load, save
    from argus.storage.hot import Database

    with Database(tmp_path / "t.db") as db:
        save(db, [fp("medal", "handles_max", 12466)])
        assert save(db, []) == 0
        assert set(load(db)) == {("medal", "handles_max")}, "빈 빌드가 기존 지문을 지웠다"
