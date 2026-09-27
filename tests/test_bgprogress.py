"""상태줄 진행 줄 헬퍼 (tools/audit/bgprogress.py).

남은 시간이 지어낸 값이 되지 않는지(끝난 게 없으면 "모름"), 끄는 조건이 실제로 끄는지,
예외로 끝나도 줄이 남지 않는지를 본다 — 끝난 작업이 상태줄에 계속 떠 있으면 그 자체가 거짓말이다.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "audit"))

import bgprogress  # noqa: E402


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    monkeypatch.setattr(bgprogress, "PROGRESS_DIR", tmp_path)
    monkeypatch.delenv(bgprogress.DISABLE_ENV, raising=False)
    monkeypatch.delenv("POOL_SLOT_DIR", raising=False)
    return tmp_path


def _files(d: pathlib.Path) -> list[pathlib.Path]:
    return sorted(d.glob("*.txt"))


def test_eta_is_unknown_until_something_finished(pdir, monkeypatch) -> None:
    p = bgprogress.Progress("t", "작업", 10)
    assert "남음 모름" in p.line(0)
    assert p.line(0).startswith("작업 1/10")


def test_eta_comes_from_measured_average(pdir, monkeypatch) -> None:
    p = bgprogress.Progress("t", "작업", 10)
    monkeypatch.setattr(bgprogress.time, "monotonic", lambda: p.t0 + 600)  # 2건에 10분
    line = p.line(2, "abc")
    assert line == "작업 3/10 (abc) · 경과 10분 · 남음 약 40분"


def test_eta_can_be_turned_off_for_uneven_units(pdir, monkeypatch) -> None:
    p = bgprogress.Progress("t", "작업", 10, eta=False)
    monkeypatch.setattr(bgprogress.time, "monotonic", lambda: p.t0 + 600)
    assert p.line(2) == "작업 3/10 · 경과 10분"  # 대조는 위 테스트(기본값이면 남음이 붙는다)


def test_step_writes_and_close_removes(pdir) -> None:
    with bgprogress.Progress("t", "작업", 3) as p:
        p.step(0, "a")
        (f,) = _files(pdir)
        assert f.read_text(encoding="utf-8").startswith("작업 1/3 (a)")
    assert _files(pdir) == []


def test_exception_still_removes_the_line(pdir) -> None:
    with pytest.raises(RuntimeError):
        with bgprogress.Progress("t", "작업", 3) as p:
            p.step(0)
            assert _files(pdir)  # 대조: 지우기 전에는 있었다
            raise RuntimeError
    assert _files(pdir) == []


@pytest.mark.parametrize("env", [(bgprogress.DISABLE_ENV, "0"), ("POOL_SLOT_DIR", "x")])
def test_disabled_writes_nothing(pdir, monkeypatch, env) -> None:
    with bgprogress.Progress("t", "작업", 3) as p:  # 대조: 끄지 않으면 쓴다
        p.step(1)
        assert _files(pdir)
    monkeypatch.setenv(*env)
    with bgprogress.Progress("t", "작업", 3) as p:
        p.step(1)
        assert _files(pdir) == []  # close 가 지우기 전에 봐야 끈 것을 잰다


def test_parallel_runs_do_not_share_a_file(pdir) -> None:
    a = bgprogress.Progress("t", "작업", 3)
    assert a.path.name == f"t-{os.getpid()}.txt"
    b = bgprogress.Progress("t", "작업", 3)
    b.path = b.path.with_name("t-other.txt")  # 다른 프로세스를 흉내 — 이름에 PID 가 들어간다
    a.step(0)
    b.step(0)
    a.close()
    assert [f.name for f in _files(pdir)] == ["t-other.txt"]  # 먼저 끝난 쪽이 남의 줄을 안 지운다
