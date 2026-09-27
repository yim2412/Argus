"""상태줄 진행 줄 — 오래 걸리는 개발 도구가 "N/M · 경과 · 남음"을 띄운다.

전역 상태줄(`~/.claude/statusline.sh`)이 `~/.claude/bg-progress.d/*.txt` 의 첫 줄을 모아 띄운다.
백그라운드로 던진 도구는 터미널에 아무것도 안 띄워 멈춘 것처럼 보인다 — 그 간극을 메운다.
**개발 도구 전용이다.** 제품(`argus/`)은 이걸 import 하지 않는다(배포물이 `~/.claude` 를 알 이유가 없다).

`tools/audit/` 에 두는 이유: `backtest.py` 가 이 폴더만 옛 커밋 worktree 에 복사해 돌린다.
`tools/` 바로 아래 두면 옛 트리에서 import 가 깨진다.

남은 시간은 끝난 단위의 **실제 평균**에서 낸다 — 끝난 게 없으면 "모름". 상수를 박으면 곧 거짓말이 된다.
"""

from __future__ import annotations

import os
import pathlib
import time

PROGRESS_DIR = pathlib.Path.home() / ".claude" / "bg-progress.d"
# 이 변수가 "0" 이면 쓰지 않는다 — 다른 도구의 자식으로 돌 때(백테스트 안의 게이트) 줄이 겹치지 않게
DISABLE_ENV = "BG_PROGRESS"


def _enabled() -> bool:
    # 풀(pool.js) 슬롯 안에서는 풀이 전체 진행을 이미 쓴다 — 슬롯마다 한 줄씩이면 7줄이 된다
    return os.environ.get(DISABLE_ENV) != "0" and not os.environ.get("POOL_SLOT_DIR")


class Progress:
    """`with Progress("mutation_sweep", "변이 스윕", total) as p: p.step(i, "키")` — 끝나면(예외 포함) 지운다."""

    def __init__(self, name: str, label: str, total: int, *, eta: bool = True) -> None:
        # eta=False: 단위 길이가 제각각이면(게이트 0초~90초) 평균으로 낸 남은 시간은 거짓말이다
        self.label, self.total, self.eta = label, total, eta
        self.path = PROGRESS_DIR / f"{name}-{os.getpid()}.txt"
        self.t0 = time.monotonic()
        self.on = _enabled()

    def line(self, done: int, detail: str = "") -> str:
        el = time.monotonic() - self.t0
        eta = f"남음 약 {el * (self.total - done) / done / 60:.0f}분" if done else "남음 모름"
        what = f" ({detail})" if detail else ""
        line = f"{self.label} {min(done + 1, self.total)}/{self.total}{what} · 경과 {el / 60:.0f}분"
        return f"{line} · {eta}" if self.eta else line

    def step(self, done: int, detail: str = "") -> None:
        """`done` = 이미 끝난 단위 수. 지금 도는 것은 done+1 번째로 표시한다."""
        if not self.on:
            return
        try:
            PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self.line(done, detail) + "\n", encoding="utf-8")
        except OSError:
            pass  # 진행 표시 실패가 도구를 죽이면 안 된다

    def close(self) -> None:
        if self.on:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> Progress:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
