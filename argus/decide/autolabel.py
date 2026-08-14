"""사건에 "이 알림이 쓸모 있었나"를 기계가 먼저 매긴다.

**왜 필요했나.** 라벨 경로는 08-09 에 뚫렸는데 닷새 뒤에도 0건이었다. 08-14 에 화면을
고쳐 7건이 붙었지만, 남은 21건 앞에서 사용자가 막힌 지점은 화면이 아니라 **판단 기준**
이었다 — "이게 정상인지 아닌지를 무엇을 보고 정하냐". 그 기준을 사람 머릿속에 두는 한
라벨은 사건마다 다시 어려워진다.

**축은 사실이 아니라 쓸모다.** "롤이 CPU 26% 를 썼나"는 매번 참이라 사실로 모으면 전부
`real` 이 되고 아무것도 못 거른다. 그래서 여기가 답하는 질문은 하나뿐이다 —
*이 알림을 안 보냈으면 사용자가 손해였나.*

**기준은 셋이고, 셋째는 "모른다"다.**

    ① 하드웨어가 실제로 성능을 깎았다 (열 스로틀)        → real
    ② 원인이 내가 띄운 앱이다 (포어그라운드 이력 있음)   → normal
    ③ 그 외                                              → 판정하지 않는다

셋째를 비워 두는 것이 이 모듈에서 가장 중요한 부분이다. 메모리 압박·병목 없음·백그라운드
프로세스가 원인인 사건은 실측 라벨이 **한 건도 없어** 기준을 세울 근거가 없다. 근거 없이
채우면 다음 사람이 그 값을 데이터로 읽는다.

**실측 근거** (2026-08-14, 라벨 7건):

    발열 스로틀링   3/3 real     GPU 87~90°C. 알림은 등급이 낮아 나가지도 않았다(미탐)
    CPU 병목·경합   4/4 normal   overwatch 25%·48% · gunfire reborn · fczf 25%

**①에서 기여 프로세스를 보지 않는 이유**는 그것이 원인이 아니기 때문이다. GPU 온도
사건에 CPU 기여도가 붙어 1위가 svchost·audiodg·wmiprvse 로 잡힌다 — 열을 낸 것은
GPU 를 쓴 게임이지 저것들이 아니다.

**②에 지문(p99) 축이 없는 이유**는 지문이 `handles_max` 와 `rss_p95` 뿐이라
CPU 에는 잴 자가 없어서다. 못 재는 것을 잰다고 적지 않는다 — 메모리 쪽에 붙일 때
그 축을 함께 넣는다.

**이 판정은 `user_label` 을 덮지 않는다.** 칸이 따로 있고(`auto_label`), 사람이 답한
사건은 그 답이 이긴다. 섞으면 기계가 매긴 것으로 기계를 고치게 된다.

문턱은 전부 `config` 의 `autolabel` 절에 있다.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from ..config.loader import AutoLabelSettings

LABEL_NORMAL = "normal"
LABEL_REAL = "real"


@dataclass(frozen=True)
class Verdict:
    """판정과 그 근거.

    `label` 이 None 이면 **판정하지 않았다는 뜻**이고, 그때도 `reason` 은 채운다 —
    "왜 안 물어봤지"와 "왜 판정을 못 했지"는 다른 질문이고, 후자에 답할 수 없으면
    기준을 넓힐 근거도 못 모은다.
    """

    label: str | None
    reason: str


def _top_contributor(raw: str | None) -> dict | None:
    """기여도 1위. 저장 형식은 `fusion._finish` 가 쓰는 JSON 배열이다."""
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(items, list) or not items:
        return None
    top = items[0]
    return top if isinstance(top, dict) else None


def judge(
    incident: dict,
    *,
    foreground: dict[str, bool],
    settings: AutoLabelSettings,
) -> Verdict:
    """사건 하나를 판정한다. **순수 함수다** — DB 를 읽지 않는다.

    `foreground` 는 `program_info.foreground_seen` 을 이름으로 찾을 수 있게 만든 표다.
    조회를 밖으로 뺀 이유는 리플레이·백필·실시간이 같은 판정을 쓰게 하기 위해서다.
    """
    if not settings.enabled:
        return Verdict(None, "자동 라벨이 꺼져 있다")

    # `sqlite3.Row` 도 그대로 받는다 — 부르는 쪽(백필·실시간·테스트)이 셋 다 다른
    # 형태로 행을 들고 온다.
    incident = dict(incident)
    bottleneck = (incident.get("bottleneck") or "").upper()

    if bottleneck in {b.upper() for b in settings.hardware_limit_bottlenecks}:
        return Verdict(LABEL_REAL, f"하드웨어가 실제로 성능을 깎았다 ({bottleneck})")

    if bottleneck not in {b.upper() for b in settings.user_workload_bottlenecks}:
        return Verdict(None, f"판정 기준이 없는 종류다 ({bottleneck or '병목 미상'})")

    top = _top_contributor(incident.get("contributors"))
    if top is None:
        return Verdict(None, "원인 프로세스가 기록되지 않았다")

    name = str(top.get("name") or "")
    share = float(top.get("share") or 0.0)

    if share < settings.min_top_share:
        return Verdict(None, f"1위 기여가 {share:.0%} 뿐이라 원인을 지목할 수 없다")

    if name.lower() in {x.lower() for x in settings.exclude}:
        # Argus 자신·개발 도구. 이걸 "내가 띄운 앱"으로 덮으면 관측자가 병목이 된
        # 상황(설계 규칙 1)을 자동 라벨이 가린다. 사람에게 남긴다.
        return Verdict(None, f"{name} 은 판정에서 빼는 이름이다 (자기 자신·개발 도구)")

    if not foreground.get(name.lower()):
        return Verdict(None, f"{name} 을 직접 띄운 적이 있는지 모른다")

    return Verdict(LABEL_NORMAL, f"원인이 직접 띄운 앱이다 — {name} {share:.0%}")


# ------------------------------------------------------------------ DB 경유

def foreground_map(db) -> dict[str, bool]:
    """`program_info` 를 이름→포어그라운드 이력 표로. 이름은 소문자로 맞춘다."""
    rows = db.query("SELECT name, foreground_seen FROM program_info")
    return {str(r["name"]).lower(): bool(r["foreground_seen"]) for r in rows}


def during_injection(db, incident_id: int) -> bool:
    """결함 주입 구간과 겹치는가.

    **닫히지 않은 주입(`ts_end IS NULL`)은 시작 시각만으로 본다.** 전원이 끊겨
    `finally` 가 못 돈 경우인데(07-30 실측), 무한한 구간으로 읽으면 그 뒤 사건이
    전부 판정에서 빠지고 그 사실이 아무 데도 보이지 않는다. 조회 계층의
    `_DURING_INJECTION` 과 같은 규칙이다.
    """
    rows = db.query(
        "SELECT 1 FROM incidents i JOIN fault_injections f"
        " ON f.ts_start <= COALESCE(i.ts_end, i.ts_start)"
        " AND COALESCE(f.ts_end, f.ts_start) >= i.ts_start"
        " WHERE i.id = ? LIMIT 1",
        (incident_id,),
    )
    return bool(rows)


def apply(db, incident_id: int, settings: AutoLabelSettings) -> Verdict:
    """사건 하나를 판정해 저장한다. **사람 답이 있으면 건드리지 않는다.**

    판정이 없어도(`label is None`) 근거는 남긴다 — 다음에 기준을 넓힐 때
    "무엇이 왜 안 걸렸나"를 세어 볼 수 있어야 한다.
    """
    rows = db.query(
        "SELECT id, bottleneck, contributors, user_label, notified FROM incidents WHERE id = ?",
        (incident_id,),
    )
    if not rows:
        return Verdict(None, "사건이 없다")
    row = rows[0]
    if row["user_label"]:
        return Verdict(None, "사람이 이미 답했다")
    if not row["notified"]:
        # **안 나간 알림은 판정하지 않는다.** 라벨의 쓰임이 "알림을 줄일지"인데,
        # 아무도 성가시게 하지 않은 사건에는 줄일 것이 없다. 사건 173건 대 알림
        # 49건이라, 여기를 열면 타일이 기계 답으로 뒤덮인다.
        return Verdict(None, "알림이 나가지 않았다")
    if during_injection(db, incident_id):
        # 결함 주입 구간. 사람에게 안 묻는 것과 같은 이유로 기계도 판정하지 않는다 —
        # 내가 일부러 만든 부하에 대한 판단은 실사용 문턱을 고칠 근거가 아니다.
        return Verdict(None, "결함 주입 구간이다")

    verdict = judge(row, foreground=foreground_map(db), settings=settings)
    with db._lock:  # noqa: SLF001
        db.conn.execute(
            "UPDATE incidents SET auto_label = ?, auto_label_reason = ?, auto_labeled_at = ?"
            " WHERE id = ?",
            (verdict.label, verdict.reason, time.time(), incident_id),
        )
        db.conn.commit()
    return verdict
