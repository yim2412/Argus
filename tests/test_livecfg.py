"""실행 중 설정 — 재시작 없이 알림을 켜고 끄는 경로.

2026-08-06 사용자 요청 ①. 지금까지 알림을 끄려면 `settings.yaml` 을 편집하고 상주를
재시작해야 했는데, 배포 대상 사용자에게 요구할 수 없는 절차다.

**여기서 잡으려는 것은 조용히 깨지는 쪽이다.**

- 껐을 때 **판정(`notified`)까지 멈추는 것** — 그러면 "켜면 몇 건이 올 것인가"라는
  측정이 죽는데, 알림이 원래 안 오는 상태라 **아무 신호도 없다.** 이 분리가
  알림을 켤 근거를 모은 방법 자체였다.
- 값을 **기동 시 한 번만 읽는 것** — 토글은 되는데 다음 알림이 옛 값으로 나간다.
  사용자는 눌렀다고 믿고 있으므로 이게 가장 나쁘다.
- 두 화면(트레이·창)이 **서로 다른 값**을 보여 주는 것 — 무엇이 참인지 알 수 없다.
"""

from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.runtime.livecfg import LIVE_KEYS, LiveConfig, LiveConfigWatcher  # noqa: E402


@pytest.fixture()
def cfg(tmp_path) -> LiveConfig:
    """실사용 파일을 절대 건드리지 않는다."""
    return LiveConfig(path=tmp_path / "runtime.yaml", defaults={"notify": True})


# ----------------------------------------------------------------- 값의 출처


def test_falls_back_to_settings_when_no_file(cfg: LiveConfig) -> None:
    """UI 에서 아무것도 바꾼 적이 없으면 `settings.yaml` 값이다."""
    assert cfg.notify_enabled is True
    assert cfg.overridden("notify") is False


def test_ui_value_beats_settings(cfg: LiveConfig) -> None:
    """**UI 가 이긴다** — 방금 누른 것이 가장 최근 의사표시다."""
    cfg.set("notify", False)

    assert cfg.notify_enabled is False
    assert cfg.overridden("notify") is True
    # 어느 쪽이 이겼는지 드러나야 한다(규칙 4). 설정 파일을 고쳤는데 안 먹는
    # 이유가 여기 보이지 않으면 사용자는 설정이 고장 났다고 생각한다.
    assert cfg.describe()["source"] == "UI"


def test_value_survives_a_restart(tmp_path) -> None:
    """**끈 것은 다음 기동에도 꺼져 있어야 한다.**

    파일에 남지 않으면 재부팅마다 알림이 되살아나고, 사용자는 그것을 고장으로 읽는다.
    """
    path = tmp_path / "runtime.yaml"
    LiveConfig(path=path, defaults={"notify": True}).set("notify", False)

    # 새 프로세스가 뜬 것과 같은 상황: 설정 파일 기본값은 여전히 True 다.
    again = LiveConfig(path=path, defaults={"notify": True})
    assert again.notify_enabled is False, "껐는데 재기동하니 다시 켜졌다"


def test_toggle_returns_the_new_value(cfg: LiveConfig) -> None:
    assert cfg.toggle("notify") is False
    assert cfg.toggle("notify") is True


# ----------------------------------------------------------------- 파일 다루기


def test_unknown_keys_are_ignored(cfg: LiveConfig) -> None:
    """**실행 중에 바꿔도 안전한 값만** 받는다.

    수집 주기나 큐 크기가 실행 중에 바뀌면 컴포넌트를 재구성해야 하는데, 그건
    재시작보다 위험하다. 파일에 아무거나 적어 넣어도 설정이 오염되지 않아야 한다.
    """
    cfg.path.write_text("notify: false\ninterval_s: 0.001\n", encoding="utf-8")
    cfg.reload(force=True)

    assert cfg.notify_enabled is False, "허용된 키까지 무시했다"
    assert cfg.get("interval_s") is None, "허용 목록 밖의 키가 통과했다"

    with pytest.raises(ValueError):
        cfg.set("interval_s", 0.001)


def test_broken_file_falls_back_instead_of_dying(cfg: LiveConfig) -> None:
    """깨진 파일 하나가 알림을 통째로 멈추면 안 된다."""
    cfg.path.write_text("notify: [불완전", encoding="utf-8")
    cfg.reload(force=True)

    assert cfg.notify_enabled is True, "깨진 파일 때문에 기본값으로도 못 갔다"


def test_write_is_atomic(cfg: LiveConfig) -> None:
    """**반쯤 쓰인 파일을 상대 프로세스가 읽으면 안 된다.**

    상주와 창이 같은 파일을 쓴다. 임시 파일이 남으면 교체가 아니라 덧씌우기로
    돌아간 것이다.
    """
    cfg.set("notify", False)

    leftovers = list(cfg.path.parent.glob("*.tmp"))
    assert not leftovers, f"임시 파일이 남았다: {leftovers}"
    assert cfg.path.exists()


def test_reload_skips_unchanged_files(cfg: LiveConfig) -> None:
    """**매 tick 파싱하지 않는다** — 관측자는 가벼워야 한다(설계 규칙 1)."""
    cfg.set("notify", False)
    assert cfg.reload() is False, "안 바뀐 파일을 다시 읽었다"


def test_reload_picks_up_another_process_write(cfg: LiveConfig) -> None:
    """**창에서 바꾼 것이 상주에 닿아야 한다.** 두 프로세스의 유일한 통로다."""
    cfg.set("notify", True)

    # 다른 프로세스가 쓴 상황. 같은 초 안에 일어나도 잡혀야 한다 — 초 단위 mtime
    # 으로는 안 잡히는 종류다(`.pyc` 캐시 사고와 같은 함정).
    other = LiveConfig(path=cfg.path, defaults={"notify": True})
    other.set("notify", False)

    assert cfg.reload() is True, "다른 프로세스가 쓴 변경을 못 봤다"
    assert cfg.notify_enabled is False


def test_watcher_reloads(cfg: LiveConfig) -> None:
    """감시 컴포넌트가 실제로 다시 읽는가."""
    watcher = LiveConfigWatcher(config=cfg)
    LiveConfig(path=cfg.path, defaults={"notify": True}).set("notify", False)

    watcher.tick()

    assert cfg.notify_enabled is False, "감시자가 변경을 집어 오지 않았다"


def test_live_keys_are_declared() -> None:
    """허용 목록이 코드 한 곳에 남아 있어야 한다(규칙 3)."""
    assert "notify" in LIVE_KEYS


# ----------------------------------------------------------------- Fusion 배선


def test_fusion_reads_live_value_at_send_time(tmp_path) -> None:
    """**발송 시점마다 다시 본다.**

    기동 시 받은 값을 들고 있으면 사용자가 껐는데도 다음 알림이 나간다 —
    누른 것이 안 먹는 상태이고, 그게 이 기능이 없는 것과 같다.
    """
    from argus.decide.fusion import Fusion, FusionSettings

    live = LiveConfig(path=tmp_path / "runtime.yaml", defaults={"notify": True})
    # settings 쪽은 **꺼짐**으로 둔다. 두 값이 같으면 배선이 끊겨도 통과한다.
    fusion = Fusion.__new__(Fusion)
    fusion.settings = FusionSettings(notify_enabled=False)
    fusion.live = live

    assert fusion.notify_enabled is True, "실행 중 값이 아니라 기동 시 값을 보고 있다"

    live.set("notify", False)
    assert fusion.notify_enabled is False

    live.set("notify", True)
    assert fusion.notify_enabled is True, "값을 한 번 읽고 캐시했다"


def test_fusion_without_live_uses_settings(tmp_path) -> None:
    """리플레이·재분석 경로에는 이 창구가 없다. 그때는 기존 값을 쓴다."""
    from argus.decide.fusion import Fusion, FusionSettings

    fusion = Fusion.__new__(Fusion)
    fusion.settings = FusionSettings(notify_enabled=True)
    fusion.live = None

    assert fusion.notify_enabled is True


def test_disabling_does_not_stop_the_judgement(tmp_path) -> None:
    """**끄는 것은 발송뿐이다.** 판정(`notified`)은 계속 돈다.

    이 분리가 죽으면 "켜면 몇 건이 올 것인가"를 잴 수 없게 되는데, 알림이 원래
    안 오는 상태라 **아무 신호도 없다.** 알림을 켤 근거를 모은 방법이 정확히 이것이었다.
    """
    import time as _time

    from argus.decide.fusion import Fusion, FusionSettings
    from argus.storage.hot import Database

    db = Database(path=tmp_path / "t.db").open()
    try:
        now = _time.time()
        with db._lock:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO incidents (ts_start, ts_end, severity, title, detectors,"
                " signal_count, peak_score) VALUES (?, ?, 'warning', '분석 중', '[\"rules\"]', 1, 0.9)",
                (now - 600, None),
            )
            db.conn.commit()

        sent: list = []
        notifier = type("N", (), {"notify": lambda self, t, m, s: sent.append((t, m, s)) or True})()
        live = LiveConfig(path=tmp_path / "runtime.yaml", defaults={"notify": True})
        live.set("notify", False)

        fusion = Fusion(db, FusionSettings(notify_enabled=True), notifier=notifier, live=live)
        fusion._set_watermark(now - 1200)
        fusion.run_once(now=now)

        assert sent == [], "껐는데 알림을 보냈다"
        notified = db.query("SELECT notified FROM incidents")[0]["notified"]
        assert notified == 1, "발송을 껐다고 판정까지 멈췄다 — 알림량 측정이 죽는다"
    finally:
        db.close()


# ----------------------------------------------------------------- 트레이 창구


class _MenuRecorder:
    """`win32gui` 대역. 메뉴에 무엇이 어떤 플래그로 붙는지만 본다."""

    MF_STRING = 0x0000
    MF_CHECKED = 0x0008
    MF_SEPARATOR = 0x0800

    def __init__(self) -> None:
        self.items: list[tuple[int, int, str]] = []

    def CreatePopupMenu(self):  # noqa: N802
        return 1

    def AppendMenu(self, menu, flags, ident, text):  # noqa: N802
        self.items.append((flags, ident, text))

    def GetCursorPos(self):  # noqa: N802
        return (0, 0)

    def SetForegroundWindow(self, hwnd):  # noqa: N802
        pass

    def TrackPopupMenu(self, *a):  # noqa: N802
        pass

    def PostMessage(self, *a):  # noqa: N802
        pass

    def DestroyMenu(self, menu):  # noqa: N802
        pass


def _patch_menu(monkeypatch, recorder: _MenuRecorder) -> None:
    import types

    monkeypatch.setitem(sys.modules, "win32gui", recorder)
    monkeypatch.setitem(
        sys.modules,
        "win32con",
        types.SimpleNamespace(
            MF_STRING=recorder.MF_STRING,
            MF_CHECKED=recorder.MF_CHECKED,
            MF_SEPARATOR=recorder.MF_SEPARATOR,
            TPM_LEFTALIGN=0,
            TPM_RIGHTBUTTON=0,
        ),
    )


def test_tray_menu_shows_the_current_state(monkeypatch, cfg: LiveConfig) -> None:
    """**메뉴를 열 때마다 현재 값을 읽는다.**

    창에서 바꿨을 수도 있으므로, 마지막으로 트레이가 쓴 값을 기억해 두면 체크 표시가
    거짓말을 한다.
    """
    from argus.ui.tray import TrayIcon

    recorder = _MenuRecorder()
    _patch_menu(monkeypatch, recorder)
    tray = TrayIcon(live=cfg)

    tray._show_menu(0)
    checked = [it for it in recorder.items if it[2] == "알림 받기"]
    assert checked, "알림 항목이 메뉴에 없다"
    assert checked[0][0] & recorder.MF_CHECKED, "켜져 있는데 체크가 없다"

    # 다른 프로세스(창)가 껐다.
    LiveConfig(path=cfg.path, defaults={"notify": True}).set("notify", False)
    cfg.reload()

    recorder.items.clear()
    tray._show_menu(0)
    checked = [it for it in recorder.items if it[2] == "알림 받기"]
    assert not (checked[0][0] & recorder.MF_CHECKED), "꺼졌는데 체크가 남아 있다"


def test_tray_toggle_writes_and_tells(monkeypatch, cfg: LiveConfig) -> None:
    """누르면 값이 바뀌고, **바뀌었다는 것을 알린다.**

    껐는데 아무 반응이 없으면 사용자는 눌렸는지조차 모른다.
    """
    from argus.ui.tray import TrayIcon

    tray = TrayIcon(live=cfg)
    told: list = []
    monkeypatch.setattr(tray, "notify", lambda t, m, s="info": told.append((t, m, s)) or True)

    tray._toggle_notify()

    assert cfg.notify_enabled is False, "메뉴를 눌렀는데 값이 안 바뀌었다"
    assert told, "껐다는 사실을 사용자에게 알리지 않았다"
    assert "탐지" in told[0][1], "탐지는 계속된다는 사실을 알리지 않았다"


def test_tray_tooltip_reveals_disabled_state(cfg: LiveConfig) -> None:
    """**꺼져 있으면 툴팁에 보여야 한다.**

    꺼 두고 잊으면 "조용한 것"과 "꺼진 것"을 구분할 수 없다 — 모니터링 도구가 낼
    수 있는 최악의 침묵이다(규칙 4).
    """
    from argus.ui.tray import TrayIcon

    tray = TrayIcon(live=cfg)
    assert "꺼짐" not in tray.current_tooltip()

    cfg.set("notify", False)
    assert "꺼짐" in tray.current_tooltip(), "알림이 꺼진 사실이 툴팁에 없다"


def test_daemon_passes_live_to_fusion_and_tray() -> None:
    """진입점이 실제로 묶어 주는가.

    **`__main__.run()` 은 테스트에서 부를 수 없다** — DB 를 열고 캘리브레이션을 돌린다.
    그래서 호출 인자를 AST 로 고정한다. 이 배선이 빠지면 토글은 파일에만 남고
    아무 동작도 바뀌지 않는다.
    """
    tree = ast.parse((ROOT / "argus" / "__main__.py").read_text(encoding="utf-8"))
    wired: dict[str, set[str]] = {"Fusion": set(), "TrayIcon": set()}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in wired:
            wired[name] = {kw.arg for kw in node.keywords if kw.arg}

    assert "live" in wired["Fusion"], "Fusion 이 실행 중 설정을 받지 않는다 — 토글이 발송에 안 닿는다"
    assert "live" in wired["TrayIcon"], "트레이가 실행 중 설정을 받지 않는다 — 메뉴가 안 뜬다"
