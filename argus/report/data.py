"""일일 리포트 조회 계층.

**UI 를 모른다.** `dashboard/data.py` 와 같은 규약을 따른다 — 읽기 전용 연결, TTL 캐시,
프레임워크 의존 없음. 창이 그대로 쓰고, 나중에 다른 표현 계층이 붙어도 같다.

**저장된 JSON 을 풀어서 돌려준다.** 화면마다 `json.loads` 를 반복하면 파싱 실패
처리가 여러 곳으로 흩어진다 — 여기서 한 번 풀고, 깨진 행은 빈 값으로 만든다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from ..dashboard.data import query, ttl_cache

# 하루에 한 번 바뀌는 값이라 캐시를 길게 잡는다. 창을 열 때마다 다시 물을 이유가 없다.
_TTL = 300.0


def _loads(raw: Any, fallback: Any) -> Any:
    """저장된 JSON 을 푼다. **깨져 있어도 화면이 죽지 않는다.**

    이 값들은 우리가 쓴 것이라 깨질 일이 없어야 하지만, 스키마가 바뀌는 중이거나
    손으로 고친 DB 에서는 깨질 수 있다. 그때 리포트 한 칸이 비는 것과 창 전체가
    예외로 닫히는 것 중에서는 전자가 낫다.
    """
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _shape(row: dict) -> dict:
    return {
        "day": row["day"],
        "total_s": float(row["total_s"] or 0.0),
        "observed_s": float(row["observed_s"] or 0.0),
        "by_category": _loads(row["by_category"], {}),
        "top_apps": _loads(row["top_apps"], []),
        "by_slot": _loads(row["by_slot"], {}),
        "built_at": float(row["built_at"] or 0.0),
    }


@ttl_cache(_TTL)
def report(day: str) -> dict | None:
    """그날의 요약. 없으면 `None` — 호출 쪽이 "기록 없음"으로 표시한다.

    행이 없는 날은 정상이다: Argus 가 꺼져 있었거나, 원본이 이미 잘려 나가 요약을
    만들지 않기로 한 날이다(`DailyReportRollup._coverage`).
    """
    rows = query("SELECT * FROM daily_report WHERE day = ?", (day,))
    return _shape(rows[0]) if rows else None


@ttl_cache(_TTL)
def available_days(limit: int = 90) -> list[str]:
    """리포트가 있는 날짜들 (최신 우선). 날짜 선택기가 이걸로 채워진다.

    **달력을 통째로 보여주고 빈 날을 고르게 하지 않는다** — 기록이 있는 날만 고를 수
    있으면 "왜 비어 있지"라는 질문 자체가 생기지 않는다.
    """
    return [r["day"] for r in query(
        "SELECT day FROM daily_report ORDER BY day DESC LIMIT ?", (limit,)
    )]


@ttl_cache(_TTL)
def recent_reports(days: int = 7) -> list[dict]:
    """최근 `days` 일의 요약 (오래된 것부터). 추이 비교용이다."""
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    return [_shape(r) for r in query(
        "SELECT * FROM daily_report WHERE day >= ? ORDER BY day", (cutoff,)
    )]


def compare(day: str) -> dict | None:
    """그날과 **직전 기록일**의 차이.

    **전날이 아니라 직전 기록일이다.** 어제 PC 를 안 켰으면 어제와의 비교는 "전부
    감소"가 되는데 그건 사실이 아니다 — 비교 대상이 없는 것이다.

    **저장하지 않고 매번 계산한다.** 이웃 두 행의 뺄셈이라 저장하면 같은 값이 두 곳에
    생기고, 어제 리포트가 나중에 다시 만들어지면 둘이 갈린다.
    """
    today = report(day)
    if today is None:
        return None

    rows = query(
        "SELECT * FROM daily_report WHERE day < ? ORDER BY day DESC LIMIT 1", (day,)
    )
    if not rows:
        return {"previous": None, "total_delta_s": None, "by_category_delta": {}}

    previous = _shape(rows[0])
    deltas = {}
    for key in set(today["by_category"]) | set(previous["by_category"]):
        deltas[key] = today["by_category"].get(key, 0.0) - previous["by_category"].get(key, 0.0)
    return {
        "previous": previous,
        "total_delta_s": today["total_s"] - previous["total_s"],
        "by_category_delta": deltas,
    }


def clear_cache() -> None:
    """캐시를 비운다. 창이 새로고침할 때와 테스트가 쓴다."""
    report.cache_clear()
    available_days.cache_clear()
    recent_reports.cache_clear()
