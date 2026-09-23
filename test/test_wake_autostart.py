"""`wake_autostart.py`：直接叫醒工作排程器裡的監督者。

這支腳本存在的理由是 2026-09-22 那一次：bot 是直接啟動的、沒有監督者，擁有者下了
`/sys restart`，bot 就只是關掉了，當下只能手動去工作排程器把 `\\Axiomatic\\Bot`
叫起來。所以要守的是三件事：已經在跑的不要再叫（不多開）、沒註冊或被拒絕要講清楚
（不假裝成功）、預設兩支都叫醒（擁有者 2026-09-22 裁定），`--bot-only`／`--batch-only` 只叫一支。

測試不碰真的工作排程器，也不看真的行程表：排程器呼叫、啟動器探測、睡眠與時鐘
全部是注入的替身。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import install_autostart as ia  # noqa: E402
import wake_autostart as wa  # noqa: E402

BOT_TASK, BOT_SCRIPT = ia.TASKS["bot"]


class _Scheduler:
    """記下每一次排程器呼叫；`registered`／`accept` 決定 `/Query`／`/Run` 的回應。"""

    def __init__(self, *, registered: bool = True, accept: bool = True):
        self.calls: list[tuple[str, ...]] = []
        self.registered = registered
        self.accept = accept

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        ok = self.registered if args[0] == "/Query" else self.accept
        return subprocess.CompletedProcess(
            ["schtasks", *args], 0 if ok else 1, stdout="",
            stderr="" if ok else "ERROR: access denied")

    def runs(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] == "/Run"]


class _Launchers:
    """`running(script)` 的替身：依序回傳排好的 pid 清單，用完之後一直回最後一個。"""

    def __init__(self, *answers: list[int]):
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, script: str) -> list[int]:
        self.asked.append(script)
        if len(self.answers) > 1:
            return self.answers.pop(0)
        return self.answers[0]


class _Clock:
    """假時鐘：只有 `sleep` 會讓它前進，所以等待迴圈永遠不會真的睡。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


def _wake(scheduler, launchers, clock=None, timeout=wa.APPEAR_TIMEOUT_SEC):
    clock = clock or _Clock()
    return wa.wake("bot", run=scheduler, running=launchers,
                   sleep=clock.sleep, clock=clock.clock, timeout=timeout)


def test_a_launcher_that_is_already_running_is_left_alone(capsys):
    scheduler = _Scheduler()
    assert _wake(scheduler, _Launchers([4321])) == 0
    assert scheduler.calls == [], "已經在跑還去叫排程器，就是多開的第一步"
    assert "4321" in capsys.readouterr().out


def test_an_unregistered_task_is_reported_and_never_run(capsys):
    scheduler = _Scheduler(registered=False)
    assert _wake(scheduler, _Launchers([])) == 1
    assert scheduler.runs() == []
    assert "install_autostart.py --install" in capsys.readouterr().err


def test_a_refused_run_is_a_failure_not_a_success(capsys):
    scheduler = _Scheduler(accept=False)
    assert _wake(scheduler, _Launchers([])) == 1
    assert "access denied" in capsys.readouterr().err


def test_it_waits_until_the_launcher_shows_up(capsys):
    scheduler = _Scheduler()
    launchers = _Launchers([], [], [], [987])
    assert _wake(scheduler, launchers) == 0
    assert scheduler.runs() == [("/Run", "/TN", BOT_TASK)]
    assert set(launchers.asked) == {BOT_SCRIPT}
    assert "987" in capsys.readouterr().out


def test_waiting_in_vain_asks_the_scheduler_only_once_and_points_at_status(capsys):
    """等不到啟動器不重試：`IgnoreNew` 之下第二次 `/Run` 什麼都不會做。"""
    scheduler = _Scheduler()
    clock = _Clock()
    assert _wake(scheduler, _Launchers([]), clock=clock, timeout=3.0) == 0
    assert len(scheduler.runs()) == 1
    assert clock.sleeps == int(3.0 / wa.APPEAR_POLL_SEC), "等待有上限，也真的等到上限"
    assert "install_autostart.py --status" in capsys.readouterr().out


@pytest.mark.parametrize("argv, expected", [
    ([], ["bot", "batch"]),
    (None, ["bot", "batch"]),
    (["--bot-only"], ["bot"]),
    (["--batch-only"], ["batch"]),
])
def test_both_are_woken_unless_one_is_asked_for(monkeypatch, argv, expected):
    """預設和登入時自動啟動的那一組一樣：bot 與批次都叫（擁有者 2026-09-22 裁定）。"""
    woken: list[str] = []
    monkeypatch.setattr(wa, "wake", lambda key: woken.append(key) or 0)
    assert wa.main(argv) == 0
    assert woken == expected


def test_one_failure_makes_the_whole_run_fail(monkeypatch):
    monkeypatch.setattr(wa, "wake", lambda key: 1 if key == "batch" else 0)
    assert wa.main([]) == 1


def test_the_two_only_flags_cannot_be_combined():
    with pytest.raises(SystemExit):
        wa.main(["--bot-only", "--batch-only"])


def test_it_wakes_the_very_tasks_the_installer_registers():
    """工作名稱只有一份（`install_autostart.TASKS`）；抄一份就會有一天對不上。"""
    assert wa.TASKS is ia.TASKS
    assert wa._schtasks is ia._schtasks
