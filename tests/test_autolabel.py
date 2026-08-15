"""자동 라벨 — 기계가 매기는 "이 알림이 쓸모 있었나".

여기서 지키는 것 둘이다.

1. **판정 로직**: 셋으로 갈리는가. 특히 셋째("모른다")가 실제로 비어 있는가 —
   근거 없는 칸을 채우면 다음 사람이 그것을 데이터로 읽는다.
2. **배선**: `config` 를 고치면 판정이 실제로 바뀌는가. 로직만 재면 08-03 의
   `procleak` 과 같은 상태가 된다(YAML 을 고쳐도 판정이 안 바뀌는데 전부 통과).

배선은 **기본값이 아닌 값**으로 잰다. `assert engine.x == cfg.x` 는 코드 기본값과
YAML 기본값이 같으면 배선이 끊겨도 참이다(08-04 에 같은 유형이 네 번 나왔다).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from argus.config.loader import AutoLabelSettings
from argus.decide import autolabel
from argus.storage.hot import Database


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


def _incident(bottleneck: str, top: tuple[str, float] | None = None) -> dict:
    contributors = None
    if top is not None:
        contributors = json.dumps([{"name": top[0], "share": top[1]}])
    return {"bottleneck": bottleneck, "contributors": contributors}


def _store(db: Database, **kwargs) -> int:
    """저장된 사건 하나. **판정이 실제로 `normal` 이 나오는 조건으로 만든다.**

    처음에는 `program_info` 를 비워 뒀는데, 그러면 판정이 어차피 "직접 띄운 적이
    있는지 모른다"로 끝나 `auto_label` 이 None 이 된다 — 사람 답 보호도 미발송
    차단도 **뜯어내나 안 뜯어내나 결과가 같아** 두 무력화가 전부 통과했다(측정 3/5).
    그래서 여기서 포어그라운드 이력을 먼저 넣는다.
    """
    db.insert_many(
        "program_info",
        ("name", "checked_at", "foreground_seen"),
        [("overwatch", time.time(), 1)],
        replace=True,
    )
    row = {
        "ts_start": time.time() - 600,
        "ts_end": time.time() - 300,
        "severity": "warning",
        "title": "t",
        "notified": 1,
        "bottleneck": "CPU",
        "contributors": json.dumps([{"name": "overwatch", "share": 0.4}]),
    }
    row.update(kwargs)
    columns = tuple(row)
    db.insert_many("incidents", columns, [tuple(row[c] for c in columns)])
    db.flush() if hasattr(db, "flush") else None
    return int(db.query("SELECT MAX(id) AS id FROM incidents")[0]["id"])


DEFAULTS = AutoLabelSettings()
FOREGROUND = {"overwatch": True, "svchost": False}

# 관측자 상태. **`CLEAN` 을 기본으로 쓰는 이유**는 대부분의 테스트가 관측자와 무관한
# 판정을 재기 때문이다 — 여기서 `None` 을 쓰면 관측자 이름이 아닌 사건까지 조용히
# 다른 경로를 타게 되어, 무엇을 재고 있는지 흐려진다.
CLEAN = autolabel.ObserverWindow(samples=12, cpu_max=0.7, throttle_max=0, dropped=0)
THROTTLED = autolabel.ObserverWindow(samples=12, cpu_max=9.4, throttle_max=2, dropped=0)
DROPPING = autolabel.ObserverWindow(samples=12, cpu_max=1.1, throttle_max=0, dropped=37)


# ---------------------------------------------------------------- 판정 로직


def test_hardware_limit_is_real() -> None:
    """열 스로틀은 어떤 앱을 돌렸든 비정상이다. 실측 라벨 3/3 이 여기였다."""
    verdict = autolabel.judge(
        _incident("THERMAL", ("svchost", 0.19)), foreground=FOREGROUND, observer=CLEAN, settings=DEFAULTS
    )
    assert verdict.label == "real"


def test_hardware_limit_ignores_contributor() -> None:
    """**기여 프로세스를 보지 않는다.** GPU 온도 사건의 1위는 CPU 로 분해한 결과라
    원인이 아니다 — 포어그라운드가 아닌 svchost 여도 판정이 흔들리면 안 된다."""
    a = autolabel.judge(_incident("THERMAL", ("svchost", 0.01)), foreground={}, observer=CLEAN, settings=DEFAULTS)
    b = autolabel.judge(_incident("THERMAL", None), foreground={}, observer=CLEAN, settings=DEFAULTS)
    assert a.label == b.label == "real"


def test_user_app_is_normal() -> None:
    verdict = autolabel.judge(
        _incident("CPU", ("overwatch", 0.25)), foreground=FOREGROUND, observer=CLEAN, settings=DEFAULTS
    )
    assert verdict.label == "normal"
    assert "overwatch" in verdict.reason


def test_background_process_is_not_judged() -> None:
    """포어그라운드 이력이 없으면 판정하지 않는다. 백그라운드가 CPU 를 먹은 것은
    "내가 돌린 작업"이 아니고, 실측 라벨이 한 건도 없어 기준을 세울 근거가 없다."""
    verdict = autolabel.judge(
        _incident("CPU", ("svchost", 0.60)), foreground=FOREGROUND, observer=CLEAN, settings=DEFAULTS
    )
    assert verdict.label is None
    assert verdict.reason


def test_unknown_bottleneck_is_not_judged() -> None:
    """메모리 압박은 실측 라벨이 0건이다. 근거 없이 채우지 않는다."""
    assert (
        autolabel.judge(
            _incident("MEMORY", ("chrome", 0.69)), foreground={"chrome": True}, observer=CLEAN, settings=DEFAULTS
        ).label
        is None
    )


def test_weak_attribution_is_not_judged() -> None:
    """1위 기여가 낮으면 "그 앱 때문"이라고 말할 근거가 없다 (실측: op.gg 13%)."""
    verdict = autolabel.judge(
        _incident("CPU", ("overwatch", 0.05)), foreground=FOREGROUND, observer=CLEAN, settings=DEFAULTS
    )
    assert verdict.label is None


def test_dev_tools_without_basis_are_left_to_human() -> None:
    """`exclude` 는 **관측자일 수 없는** 개발 도구다. 판정 근거가 없어 사람에게 남긴다."""
    verdict = autolabel.judge(
        _incident("CPU", ("claude", 0.72)),
        foreground={"claude": True},
        observer=CLEAN,
        settings=DEFAULTS,
    )
    assert verdict.label is None


# ------------------------------------------------- 관측자 (설계 규칙 1)
#
# **이름으로 가르지 않는다.** 사건 #179 의 `python` 기여자 PID 25개 안에 테스트가
# 띄운 `-m argus` 자식이 섞여 있었다(2026-08-15 실측). 상주와 개발 도구가 같은
# 이름으로 잡히므로, 가르는 것은 관측자 자신의 실측이어야 한다.


def test_observer_over_budget_is_left_to_human() -> None:
    """★ **관측자가 병목이 됐으면 절대 normal 로 덮지 않는다.**

    여기를 덮으면 설계 규칙 1 이 말하는 제품 실패(모니터가 병목)가 자동 라벨 뒤에 묻힌다.

    **막지 않았으면 무엇이 일어났을 것인가를 먼저 단언한다** — 관측자가 깨끗할 때는
    같은 사건이 `normal` 로 나온다. 그래야 이 테스트가 보호 장치를 재는 것이 된다.
    """
    incident = _incident("CPU", ("python", 0.72))
    fg = {"python": True}

    assert (
        autolabel.judge(incident, foreground=fg, observer=CLEAN, settings=DEFAULTS).label
        == "normal"
    ), "막지 않았어도 판정이 안 나오는 조건이면 이 테스트는 아무것도 재지 않는다"

    for dirty in (THROTTLED, DROPPING):
        verdict = autolabel.judge(incident, foreground=fg, observer=dirty, settings=DEFAULTS)
        assert verdict.label is None, f"관측자가 더러운데 판정했다: {verdict}"
        assert "관측자" in verdict.reason


def test_observer_without_telemetry_is_left_to_human() -> None:
    """`self_telemetry` 는 7일만 보존되고 웜으로 나가지 않는다. 오래된 사건은
    결백을 증명할 방법이 없다 — **확인할 수 없는 것을 확인했다고 적지 않는다.**"""
    incident = _incident("CPU", ("python", 0.72))
    fg = {"python": True}
    for missing in (None, autolabel.ObserverWindow(0, 0.0, 0, 0)):
        verdict = autolabel.judge(incident, foreground=fg, observer=missing, settings=DEFAULTS)
        assert verdict.label is None
        assert "확인할 수 없다" in verdict.reason


def test_clean_observer_lets_dev_tool_be_judged() -> None:
    """**관측자가 결백하면 개발 도구도 ② 로 판정한다.**

    예전에는 이름만 보고 무조건 거부해서 관측자가 결백한 경우까지 답 대기에 쌓였다
    (2026-08-15: 답 대기 6건 중 4건이 pytest·mutation_sweep 이었다).
    """
    verdict = autolabel.judge(
        _incident("CPU", ("python", 0.40)),
        foreground={"python": True},
        observer=CLEAN,
        settings=DEFAULTS,
    )
    assert verdict.label == "normal"


def test_observer_names_come_from_config() -> None:
    """**기본값에 없는 이름으로 잰다.** `overwatch` 는 기본 목록에 없으므로, 넣었을 때
    관측자 검사를 타면 목록이 실제로 읽히고 있다는 뜻이다."""
    incident = _incident("CPU", ("overwatch", 0.40))
    assert (
        autolabel.judge(incident, foreground=FOREGROUND, observer=None, settings=DEFAULTS).label
        == "normal"
    ), "기본값에서는 관측자와 무관하게 판정돼야 한다"

    watched = AutoLabelSettings(observer_names=["overwatch"])
    verdict = autolabel.judge(incident, foreground=FOREGROUND, observer=None, settings=watched)
    assert verdict.label is None
    assert "확인할 수 없다" in verdict.reason


def test_disabled_judges_nothing() -> None:
    off = AutoLabelSettings(enabled=False)
    assert autolabel.judge(_incident("THERMAL"), foreground={}, observer=CLEAN, settings=off).label is None


# ---------------------------------------------------------------- config 배선


def test_hardware_list_comes_from_config() -> None:
    """**기본값에 없는 값으로 잰다.** MEMORY 는 기본 목록에 없으므로, 넣어서 판정이
    바뀌면 목록이 실제로 읽히고 있다는 뜻이다."""
    assert autolabel.judge(_incident("MEMORY"), foreground={}, observer=CLEAN, settings=DEFAULTS).label is None
    widened = AutoLabelSettings(hardware_limit_bottlenecks=["THERMAL", "MEMORY"])
    assert autolabel.judge(_incident("MEMORY"), foreground={}, observer=CLEAN, settings=widened).label == "real"


def test_min_top_share_comes_from_config() -> None:
    incident = _incident("CPU", ("overwatch", 0.20))
    assert autolabel.judge(incident, foreground=FOREGROUND, observer=CLEAN, settings=DEFAULTS).label == "normal"
    strict = AutoLabelSettings(min_top_share=0.5)
    assert autolabel.judge(incident, foreground=FOREGROUND, observer=CLEAN, settings=strict).label is None


def test_exclude_comes_from_config() -> None:
    incident = _incident("CPU", ("overwatch", 0.40))
    assert autolabel.judge(incident, foreground=FOREGROUND, observer=CLEAN, settings=DEFAULTS).label == "normal"
    excluded = AutoLabelSettings(exclude=["overwatch"])
    assert autolabel.judge(incident, foreground=FOREGROUND, observer=CLEAN, settings=excluded).label is None


def test_fusion_carries_config_to_autolabel() -> None:
    """상주 경로가 실제로 그 설정을 들고 가는가. `FusionSettings` 가 중간에서
    떨어뜨리면 YAML 을 고쳐도 판정이 안 바뀐다 — 08-04 레지스트리 구멍과 같은 자리다."""
    from argus.decide.fusion import FusionSettings

    settings = FusionSettings(autolabel=AutoLabelSettings(min_top_share=0.77))
    assert settings.autolabel.min_top_share == 0.77


# ---------------------------------------------------------------- 저장 (apply)


def test_apply_never_overwrites_human(db: Database) -> None:
    """**사람 답이 이긴다.** 섞이면 기계가 매긴 것으로 기계를 고치게 된다."""
    incident_id = _store(db, user_label="real")
    # **막지 않았으면 normal 이 나왔을 조건이다.** 이 줄이 없으면 판정이 어차피
    # None 인 사건을 두고 "안 덮었다"고 말하게 된다 — 그 상태로 무력화 측정에서
    # 통과했다(2026-08-14).
    assert (
        autolabel.judge(
            {"bottleneck": "CPU", "contributors": json.dumps([{"name": "overwatch", "share": 0.4}])},
            foreground=autolabel.foreground_map(db),
            observer=CLEAN, settings=DEFAULTS,
        ).label
        == "normal"
    )
    autolabel.apply(db, incident_id, DEFAULTS)
    row = db.query("SELECT user_label, auto_label FROM incidents WHERE id = ?", (incident_id,))[0]
    assert row["user_label"] == "real"
    assert row["auto_label"] is None


def test_apply_skips_unnotified(db: Database) -> None:
    """안 나간 알림에는 줄일 것이 없다. 사건이 알림보다 세 배 많아 여기를 열면
    타일이 기계 답으로 뒤덮인다."""
    incident_id = _store(db, notified=0)
    assert (
        autolabel.judge(
            {"bottleneck": "CPU", "contributors": json.dumps([{"name": "overwatch", "share": 0.4}])},
            foreground=autolabel.foreground_map(db),
            observer=CLEAN, settings=DEFAULTS,
        ).label
        == "normal"
    ), "막지 않았어도 판정이 안 나오는 조건이면 이 테스트는 아무것도 재지 않는다"
    assert autolabel.apply(db, incident_id, DEFAULTS).label is None
    row = db.query("SELECT auto_label FROM incidents WHERE id = ?", (incident_id,))[0]
    assert row["auto_label"] is None


def test_apply_skips_injection_window(db: Database) -> None:
    """사람에게 안 묻는 것과 같은 이유로 기계도 판정하지 않는다."""
    incident_id = _store(db)
    row = db.query("SELECT ts_start, ts_end FROM incidents WHERE id = ?", (incident_id,))[0]
    db.insert_many(
        "fault_injections",
        ("ts_start", "ts_end", "scenario"),
        [(row["ts_start"] - 60, row["ts_end"] + 60, "cpu_hog")],
    )
    assert autolabel.apply(db, incident_id, DEFAULTS).label is None
    assert db.query("SELECT auto_label FROM incidents WHERE id = ?", (incident_id,))[0][
        "auto_label"
    ] is None


def test_apply_leaves_neighbour_of_injection_alone(db: Database) -> None:
    """겹치는 것만 빠진다. 인접한 사건까지 삼키면 그 뒤 알림이 전부 판정에서 빠진다."""
    incident_id = _store(db)
    row = db.query("SELECT ts_start FROM incidents WHERE id = ?", (incident_id,))[0]
    db.insert_many(
        "fault_injections",
        ("ts_start", "ts_end", "scenario"),
        [(row["ts_start"] - 7200, row["ts_start"] - 3600, "cpu_hog")],
    )
    assert autolabel.apply(db, incident_id, DEFAULTS).label == "normal"


def test_apply_stores_reason_even_without_verdict(db: Database) -> None:
    """판정을 못 해도 근거는 남는다 — "무엇이 왜 안 걸렸나"를 세어야 기준을 넓힌다."""
    incident_id = _store(db, bottleneck="MEMORY")
    autolabel.apply(db, incident_id, DEFAULTS)
    row = db.query(
        "SELECT auto_label, auto_label_reason FROM incidents WHERE id = ?", (incident_id,)
    )[0]
    assert row["auto_label"] is None
    assert row["auto_label_reason"]


# ------------------------------------------- 관측자 배선 (apply → DB → judge)


def _selftel(db: Database, ts_start: float, ts_end: float, *, throttle: int, drops: int) -> None:
    """구간을 덮는 자기계측 표본. `drop_count` 는 누적값이라 증가분으로 넣는다."""
    rows = []
    span = max(ts_end - ts_start, 1.0)
    for i in range(5):
        rows.append((ts_start + span * i / 4.0, 0.8, 60.0, 9, 120, 0, 100 + drops * i, 1.2, throttle))
    db.insert_many(
        "self_telemetry",
        ("ts", "cpu_percent", "rss_mb", "threads", "handles", "queue_depth",
         "drop_count", "write_latency_ms", "throttle_level"),
        rows,
        replace=True,
    )


def test_apply_reads_observer_telemetry(db: Database) -> None:
    """**`apply` 가 실제로 자기계측을 읽는가.**

    `judge` 만 재면 08-03 `procleak` 과 같은 상태가 된다 — 로직은 맞는데 배선이
    끊겨 있어도 전부 통과한다. 여기서는 **DB 에 넣은 값이 판정을 뒤집는지**를 본다.

    `python` 은 `observer_names` 라 관측자 검사를 탄다. 자기계측이 깨끗하면 `normal`,
    스로틀이 올라가 있으면 판정 없음이어야 한다. **둘이 같은 값이면 배선이 끊긴 것이다.**
    """
    db.insert_many(
        "program_info", ("name", "checked_at", "foreground_seen"),
        [("python", time.time(), 1)], replace=True,
    )
    contributors = json.dumps([{"name": "python", "share": 0.72}])

    clean_id = _store(db, contributors=contributors)
    row = db.query("SELECT ts_start, ts_end FROM incidents WHERE id = ?", (clean_id,))[0]
    _selftel(db, row["ts_start"], row["ts_end"], throttle=0, drops=0)
    assert autolabel.apply(db, clean_id, DEFAULTS).label == "normal", (
        "관측자가 깨끗한데 판정하지 않았다 — 그러면 아래 단언이 아무것도 재지 않는다"
    )

    db.conn.execute("DELETE FROM self_telemetry")
    db.conn.commit()
    dirty_id = _store(db, contributors=contributors)
    row = db.query("SELECT ts_start, ts_end FROM incidents WHERE id = ?", (dirty_id,))[0]
    _selftel(db, row["ts_start"], row["ts_end"], throttle=2, drops=0)
    verdict = autolabel.apply(db, dirty_id, DEFAULTS)
    assert verdict.label is None, f"관측자가 스로틀 중인데 판정했다: {verdict}"
    assert "관측자가 예산을 넘었다" in verdict.reason


def test_observer_window_uses_drop_delta(db: Database) -> None:
    """**`drop_count` 는 누적값이라 차이를 본다.** 최대값만 보면 예전에 한 번 버린 적이
    있는 프로세스가 영원히 "더러운" 상태가 되어, 그 뒤 사건이 전부 판정에서 빠진다."""
    now = time.time()
    # 구간 내내 누적 드롭이 500 으로 **일정하다** — 이 구간에서 버린 것은 없다.
    db.insert_many(
        "self_telemetry",
        ("ts", "cpu_percent", "rss_mb", "threads", "handles", "queue_depth",
         "drop_count", "write_latency_ms", "throttle_level"),
        [(now - 60 + i * 10, 0.5, 55.0, 9, 120, 0, 500, 1.0, 0) for i in range(6)],
        replace=True,
    )
    window = autolabel.observer_window(db, now - 60, now)
    assert window is not None and window.samples == 6
    assert window.dropped == 0, f"누적값을 그대로 읽었다: {window}"
    assert window.clean is True


def test_observer_window_is_none_without_samples(db: Database) -> None:
    """표본이 없으면 `None`. 보존이 지난 사건을 "깨끗하다"로 읽으면 안 된다."""
    assert autolabel.observer_window(db, time.time() - 600, time.time() - 300) is None


# ------------------------------------------------ 백필 도구 (미리보기 = 저장)


def _backfill_module():
    """`tools/autolabel_backfill.py` 를 모듈로 연다. 패키지가 아니라 경로로 로드한다."""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "tools" / "autolabel_backfill.py"
    spec = importlib.util.spec_from_file_location("autolabel_backfill", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_preview_matches_apply(db: Database, monkeypatch, capsys) -> None:
    """**미리보기와 저장이 같은 답을 낸다.** 이 도구에는 테스트가 없어서, 08-15 에
    `judge` 가 `observer` 를 필수로 받게 되자 미리보기만 `TypeError` 로 죽었는데도
    전체 531개가 통과했다. 도구를 실제로 돌리지 않으면 이 유형은 안 잡힌다.

    막지 않았으면 무엇이 일어났을 것인가: 미리보기가 판정 경로를 따로 갖고 있으면
    **저장 결과와 다른 답을 화면에 찍는다** — 미리보기를 볼 이유가 사라진다.
    """
    module = _backfill_module()
    incident_id = _store(db)
    monkeypatch.setattr(module, "Database", lambda *a, **k: db)
    monkeypatch.setattr(db, "open", lambda: db, raising=False)
    monkeypatch.setattr(db, "close", lambda: None, raising=False)

    assert module.main([]) == 0
    preview = capsys.readouterr().out
    assert "normal" in preview, f"미리보기가 판정을 못 냈다:\n{preview}"
    assert db.query("SELECT auto_label FROM incidents WHERE id = ?", (incident_id,))[0][
        "auto_label"
    ] is None, "미리보기가 저장했다"

    assert module.main(["--apply"]) == 0
    stored = db.query("SELECT auto_label FROM incidents WHERE id = ?", (incident_id,))[0]
    assert stored["auto_label"] == "normal"


def test_evaluate_does_not_store(db: Database) -> None:
    """`evaluate` 는 판정만 한다. `apply` 와 같은 답이어야 한다 — 갈리면 `_decide`
    를 지나지 않는 경로가 생긴 것이다."""
    incident_id = _store(db)
    preview = autolabel.evaluate(db, incident_id, DEFAULTS)
    assert preview.label == "normal"
    assert db.query("SELECT auto_label FROM incidents WHERE id = ?", (incident_id,))[0][
        "auto_label"
    ] is None
    assert autolabel.apply(db, incident_id, DEFAULTS).label == preview.label


def test_apply_does_not_stamp_reason_on_human_answer(db: Database) -> None:
    """게이트에 걸린 사건은 **칸을 건드리지 않는다.** 사람이 답한 사건에
    "사람이 이미 답했다"를 써 넣으면, 사람 답과 기계 답이 같은 화면에서 서로를
    설명하게 된다. `_decide` 의 둘째 반환값이 막는 것이 이것이다."""
    incident_id = _store(db, user_label="real")
    autolabel.apply(db, incident_id, DEFAULTS)
    row = db.query(
        "SELECT auto_label, auto_label_reason FROM incidents WHERE id = ?", (incident_id,)
    )[0]
    assert row["auto_label"] is None
    assert row["auto_label_reason"] is None, f"사람 답 사건에 기계 근거가 찍혔다: {row['auto_label_reason']}"
