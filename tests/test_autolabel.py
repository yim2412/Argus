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


# ---------------------------------------------------------------- 판정 로직


def test_hardware_limit_is_real() -> None:
    """열 스로틀은 어떤 앱을 돌렸든 비정상이다. 실측 라벨 3/3 이 여기였다."""
    verdict = autolabel.judge(
        _incident("THERMAL", ("svchost", 0.19)), foreground=FOREGROUND, settings=DEFAULTS
    )
    assert verdict.label == "real"


def test_hardware_limit_ignores_contributor() -> None:
    """**기여 프로세스를 보지 않는다.** GPU 온도 사건의 1위는 CPU 로 분해한 결과라
    원인이 아니다 — 포어그라운드가 아닌 svchost 여도 판정이 흔들리면 안 된다."""
    a = autolabel.judge(_incident("THERMAL", ("svchost", 0.01)), foreground={}, settings=DEFAULTS)
    b = autolabel.judge(_incident("THERMAL", None), foreground={}, settings=DEFAULTS)
    assert a.label == b.label == "real"


def test_user_app_is_normal() -> None:
    verdict = autolabel.judge(
        _incident("CPU", ("overwatch", 0.25)), foreground=FOREGROUND, settings=DEFAULTS
    )
    assert verdict.label == "normal"
    assert "overwatch" in verdict.reason


def test_background_process_is_not_judged() -> None:
    """포어그라운드 이력이 없으면 판정하지 않는다. 백그라운드가 CPU 를 먹은 것은
    "내가 돌린 작업"이 아니고, 실측 라벨이 한 건도 없어 기준을 세울 근거가 없다."""
    verdict = autolabel.judge(
        _incident("CPU", ("svchost", 0.60)), foreground=FOREGROUND, settings=DEFAULTS
    )
    assert verdict.label is None
    assert verdict.reason


def test_unknown_bottleneck_is_not_judged() -> None:
    """메모리 압박은 실측 라벨이 0건이다. 근거 없이 채우지 않는다."""
    assert (
        autolabel.judge(
            _incident("MEMORY", ("chrome", 0.69)), foreground={"chrome": True}, settings=DEFAULTS
        ).label
        is None
    )


def test_weak_attribution_is_not_judged() -> None:
    """1위 기여가 낮으면 "그 앱 때문"이라고 말할 근거가 없다 (실측: op.gg 13%)."""
    verdict = autolabel.judge(
        _incident("CPU", ("overwatch", 0.05)), foreground=FOREGROUND, settings=DEFAULTS
    )
    assert verdict.label is None


def test_self_and_dev_tools_are_left_to_human() -> None:
    """Argus 자신이 CPU 를 먹은 것을 "정상"으로 덮으면 관측자가 병목이 된 상황을
    자동 라벨이 가린다(설계 규칙 1)."""
    verdict = autolabel.judge(
        _incident("CPU", ("python", 0.72)), foreground={"python": True}, settings=DEFAULTS
    )
    assert verdict.label is None


def test_disabled_judges_nothing() -> None:
    off = AutoLabelSettings(enabled=False)
    assert autolabel.judge(_incident("THERMAL"), foreground={}, settings=off).label is None


# ---------------------------------------------------------------- config 배선


def test_hardware_list_comes_from_config() -> None:
    """**기본값에 없는 값으로 잰다.** MEMORY 는 기본 목록에 없으므로, 넣어서 판정이
    바뀌면 목록이 실제로 읽히고 있다는 뜻이다."""
    assert autolabel.judge(_incident("MEMORY"), foreground={}, settings=DEFAULTS).label is None
    widened = AutoLabelSettings(hardware_limit_bottlenecks=["THERMAL", "MEMORY"])
    assert autolabel.judge(_incident("MEMORY"), foreground={}, settings=widened).label == "real"


def test_min_top_share_comes_from_config() -> None:
    incident = _incident("CPU", ("overwatch", 0.20))
    assert autolabel.judge(incident, foreground=FOREGROUND, settings=DEFAULTS).label == "normal"
    strict = AutoLabelSettings(min_top_share=0.5)
    assert autolabel.judge(incident, foreground=FOREGROUND, settings=strict).label is None


def test_exclude_comes_from_config() -> None:
    incident = _incident("CPU", ("overwatch", 0.40))
    assert autolabel.judge(incident, foreground=FOREGROUND, settings=DEFAULTS).label == "normal"
    excluded = AutoLabelSettings(exclude=["overwatch"])
    assert autolabel.judge(incident, foreground=FOREGROUND, settings=excluded).label is None


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
            settings=DEFAULTS,
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
            settings=DEFAULTS,
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
