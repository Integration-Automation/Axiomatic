"""`wake_autostart.py`：直接叫醒工作排程器裡的監督者。

這支腳本存在的理由是 2026-09-22 那一次：bot 是直接啟動的、沒有監督者，擁有者下了
`/sys restart`，bot 就只是關掉了，當下只能手動去工作排程器把 `\\Axiomatic\\Bot`
叫起來。所以要守的是三件事：已經在跑的不要再叫（不多開）、沒註冊或被拒絕要講清楚
（不假裝成功）、預設**排程器裡有的全部**叫醒，`--bot-only`／`--batch-only` 只叫一邊。

bot 的工作是一個平台一筆，而它們共用同一個腳本檔名，所以「它在跑嗎」要連平台一起
問——不然其中一個平台的那一支會替別的平台回答「在跑」，而那個平台就永遠叫不醒。

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

# 合成的工作名：這幾支不碰真的排程器，所以名字只要形狀對就好。
BOT_TASK = ia.bot_task("discord")[0]
BOT_SCRIPT = "start_discord_bot.py"
BATCH_TASK = ia.BATCH_TASK[0]


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
        self.filters: list = []

    def __call__(self, script: str, *, also_contains=None) -> list[int]:
        self.asked.append(script)
        self.filters.append(also_contains)
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
    return wa.wake(BOT_TASK, run=scheduler, running=launchers,
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


_REGISTERED = [ia.bot_task("discord")[0], ia.bot_task("telegram")[0],
               ia.BATCH_TASK[0]]


@pytest.mark.parametrize("argv, expected", [
    ([], _REGISTERED),
    (None, _REGISTERED),
    (["--bot-only"], _REGISTERED[:2]),
    (["--batch-only"], _REGISTERED[2:]),
])
def test_everything_registered_is_woken_unless_one_side_is_asked_for(
        monkeypatch, argv, expected):
    """預設和登入時自動啟動的那一組一樣：**排程器裡有的全部**。

    `--bot-only` 是「所有平台的那幾筆」，不是「某一個平台」——一個平台一個行程之後
    那已經不是一筆工作了。
    """
    monkeypatch.setattr(ia, "registered_tasks", lambda: list(_REGISTERED))
    woken: list[str] = []
    monkeypatch.setattr(wa, "wake", lambda name: woken.append(name) or 0)
    assert wa.main(argv) == 0
    assert woken == expected


def test_nothing_registered_is_a_failure_that_says_what_to_run(monkeypatch, capsys):
    """一筆工作都沒有時要出聲：「叫醒完成、但一支都沒叫」跟成功印起來一模一樣。"""
    monkeypatch.setattr(ia, "registered_tasks", lambda: [])
    assert wa.main([]) == 1
    assert "install_autostart.py --install" in capsys.readouterr().err


def test_one_failure_makes_the_whole_run_fail(monkeypatch):
    monkeypatch.setattr(ia, "registered_tasks", lambda: list(_REGISTERED))
    monkeypatch.setattr(
        wa, "wake", lambda name: 1 if name == ia.BATCH_TASK[0] else 0)
    assert wa.main([]) == 1


def test_the_two_only_flags_cannot_be_combined():
    with pytest.raises(SystemExit):
        wa.main(["--bot-only", "--batch-only"])


def test_it_wakes_the_very_tasks_the_installer_registers():
    """工作名稱與排程器呼叫只有一份（`install_autostart`）；抄一份就會有一天對不上。"""
    assert wa.BATCH_TASK is ia.BATCH_TASK
    assert wa._schtasks is ia._schtasks
    assert wa._wanted_task_names is ia._wanted_task_names


@pytest.mark.parametrize("task, script, platform", [
    (ia.bot_task("discord")[0], "start_discord_bot.py", "discord"),
    (ia.bot_task("telegram")[0], "start_discord_bot.py", "telegram"),
    (ia.BATCH_TASK[0], ia.BATCH_TASK[1], None),
])
def test_each_task_maps_back_to_its_script_and_platform(task, script, platform):
    """工作名 →（腳本, 平台）。平台那一半就是「它在跑嗎」問得準不準的關鍵。"""
    assert wa.task_script(task) == (script, platform)


def test_the_running_probe_is_asked_about_this_platform_only():
    """否則其中一個平台的監督者會替另一個平台回答「在跑」，那個平台就永遠叫不醒。"""
    launchers = _Launchers([1234])
    wa.wake(ia.bot_task("telegram")[0], run=_Scheduler(), running=launchers)
    assert launchers.filters == ["telegram"], launchers.filters
    batch = _Launchers([1234])
    wa.wake(ia.BATCH_TASK[0], run=_Scheduler(), running=batch)
    assert batch.filters == [None], batch.filters
