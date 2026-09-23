"""`_process_control.py` 的行為測試——這個模組決定**什麼東西會被殺掉**。

在此之前這支模組完全沒有專屬測試（只有 `test_bot_helpers.py` 裡 5 筆
`collapse_interpreter_stub_pairs` 的案例），而它是全專案裡少數幾個「判斷錯了就有
人的東西不見」的地方：輸出直接餵給 `taskkill /F`。

**這支測試是為了一個實際發生過的誤判而寫的。** 舊版 `_find_all_webrunner_pids`
用子字串比對 cmdline（`if target in norm`），於是任何**提到**檔名的命令列都會被
算成「漏網的背景產圖程式」。2026-08-30 在這台機器上實測，掃出 7 筆而其中只有 2 筆
是真的；另外 5 筆是命令列裡剛好出現檔名的 shell 與 `python -c`。後果分兩層：

- `/sys health`、`/sys doctor`、`_failure_diagnostic_summary` 報出幽靈孤兒
  （doctor 還會因此整份判定失敗）；
- `/gen stop` 與 `/gen run` 會**真的把它們 `taskkill /F` 掉**——包含編輯器裡
  未存檔的內容。

所以下面的 fixture 直接就是當時掃到的那幾筆。
"""
from __future__ import annotations

import asyncio
import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import _process_control as pc  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"


# ---------------------------------------------------------------- fixtures --

class _FakeProc:
    """夠像 `psutil.Process` 的替身：`info` 是 `process_iter(attrs=…)` 填的，
    `cmdline()` / `ppid()` 是本模組現在改成逐筆索取的兩個。"""

    def __init__(self, pid, name, cmdline, ppid=0, raises=None):
        self.info = {"pid": pid, "name": name}
        self._cmdline = cmdline
        self._ppid = ppid
        self._raises = raises

    def cmdline(self):
        if self._raises is not None:
            raise self._raises
        return self._cmdline

    def ppid(self):
        if self._raises is not None:
            raise self._raises
        return self._ppid


class _FakePsutil:
    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    def __init__(self, procs):
        self._procs = procs
        self.attrs_seen = []

    def process_iter(self, attrs=None):
        self.attrs_seen.append(tuple(attrs or ()))
        return list(self._procs)

    @staticmethod
    def pid_exists(pid):
        return False


def _install(monkeypatch, procs):
    fake = _FakePsutil(procs)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_real_chrome_release_wait(monkeypatch):
    """把「殺完 chrome 之後等作業系統放掉握柄」那段等待設成 0。

    測試餵的是假行程，沒有任何握柄要放，但同一行 `sleep` 照樣睡滿。做成 autouse
    是因為付錢的不只一支：`_terminate_all_webrunner_instances` 那條路上有 11 支測試
    會走到它，合計 22 秒，佔這個檔案三十幾秒裡的大半，而且對這個模組跑變異時每個
    變異都再付一次。**正式值不受影響**——它由
    `test_the_chrome_release_wait_is_still_a_real_wait_in_production` 直接讀原始碼
    的字面值釘住，這支夾具改的是匯入後的模組屬性，碰不到那個數字。
    """
    monkeypatch.setattr(pc, "_CHROME_RELEASE_WAIT_SEC", 0)


# 2026-08-30 實測掃到的那 7 筆，原樣搬過來當 fixture。
REAL_WORLD_SCAN = [
    _FakeProc(37584, "python.exe",
              [r"D:\Work\Example\.venv\Scripts\python.exe", "-u",
               r"D:\Work\Example\Axiomatic\webrunner_novelai.py"],
              ppid=43512),
    _FakeProc(43512, "python.exe",
              [r"D:\Work\Example\.venv\Scripts\python.exe", "-u",
               r"D:\Work\Example\Axiomatic\webrunner_novelai.py"],
              ppid=43860),
    _FakeProc(58184, "bash.exe",
              ["bash.exe", "-c",
               "cd /d/Work/Axiomatic && grep -n webrunner_novelai.py"],
              ppid=1),
    _FakeProc(69568, "bash.exe",
              ["bash.exe", "-c", "sed -n '1,40p' axiomatic/webrunner_novelai.py"],
              ppid=1),
    _FakeProc(70772, "python.exe",
              ["python.exe", "-c",
               "targets=('webrunner_novelai.py','webrunner_je_only.py')"],
              ppid=1),
    _FakeProc(75132, "python.exe",
              ["python.exe", "-c",
               "targets=('webrunner_novelai.py','webrunner_je_only.py')"],
              ppid=1),
    _FakeProc(75592, "bash.exe",
              ["bash.exe", "-c", "echo webrunner_novelai.py"], ppid=1),
]


# ------------------------------------------- looks_like_python_process ------

@pytest.mark.parametrize("name", [
    "python.exe", "Python.EXE", "pythonw.exe", "py.exe",
    "python", "python3", "python3.14", "python313.exe", "python3.14.exe",
    " python.exe ",
])
def test_a_python_interpreter_is_recognised(name):
    assert pc.looks_like_python_process(name), name


@pytest.mark.parametrize("name", [
    "bash.exe",         # 我自己的 shell
    "code.exe",         # 編輯器——殺掉＝未存檔的內容沒了
    "Code.exe",
    "git.exe",
    "pwsh.exe",
    "chrome.exe",
    "mypython.exe",     # 名字裡有 python，但不是直譯器
    "pythonic.exe",
    "python.exe.bak",
    "notepad++.exe",
    "", "   ", None, 123, ["python.exe"],
])
def test_everything_else_is_not_a_python_interpreter(name):
    assert not pc.looks_like_python_process(name), name


# --------------------------------------------- python_script_argument ------

TARGETS = ("webrunner_novelai.py", "webrunner_je_only.py")

@pytest.mark.parametrize("cmdline,expect", [
    # 這台機器上實測到的兩種真實形狀（2026-08-30 `psutil.Process.cmdline()`）
    ([r"D:\a\.venv\Scripts\python.exe", "-u",
      r"D:\a\Axiomatic\webrunner_novelai.py"],
     r"D:\a\Axiomatic\webrunner_novelai.py"),
    ([r"D:\a\.venv\Scripts\python.exe", r"D:\a\start_webrunner.py"],
     r"D:\a\start_webrunner.py"),
    # `py -3 -u script.py`——啟動器在沒有 .venv 時會走這條
    (["py", "-3", "-u", "s.py"], "s.py"),
    (["python", "s.py", "-u", "arg"], "s.py"),         # 腳本自己的參數不影響
    (["python", "-uB", "s.py"], "s.py"),               # 合併寫法
    (["python", "-Xdev", "s.py"], "s.py"),
    (["python", "-X", "dev", "s.py"], "s.py"),         # 分開寫，flag 吃掉一格
    (["python", "-W", "ignore", "s.py"], "s.py"),
    (["python", "-Wignore", "s.py"], "s.py"),
    (["python", "--check-hash-based-pycs", "always", "s.py"], "s.py"),
])
def test_the_script_python_is_actually_running_is_identified(cmdline, expect):
    assert pc.python_script_argument(cmdline) == expect


@pytest.mark.parametrize("cmdline", [
    ["python", "-c", "print('hi')"],
    ["python", "-uc", "print('hi')"],          # 合併寫法的 -c
    ["python", "-m", "black", "s.py"],
    ["python", "-um", "pytest", "s.py"],
    ["python", "-"],                           # 從 stdin 讀程式
    ["python"],                                # 互動模式
    ["python", "-u"],                          # 只有 flag，沒有腳本
    [], None, ["python", None, "s.py"],
])
def test_no_script_argument_is_reported_when_there_is_none(cmdline):
    assert pc.python_script_argument(cmdline) is None


# ------------------------------------------------- cmdline_runs_script ------

@pytest.mark.parametrize("cmdline,expect", [
    (["python", "-u", r"D:\a\Axiomatic\webrunner_novelai.py"],
     "webrunner_novelai.py"),
    (["python", "/home/u/axiomatic/webrunner_novelai.py"], "webrunner_novelai.py"),
    (["python", "webrunner_novelai.py"], "webrunner_novelai.py"),   # cd 進去再跑
    (["python", "-u", "axiomatic/webrunner_je_only.py"], "webrunner_je_only.py"),
    (["python", r"D:\A\WEBRUNNER_NOVELAI.PY"], "webrunner_novelai.py"),
    (["python", '"D:/a/webrunner_novelai.py"'], "webrunner_novelai.py"),
])
def test_running_the_script_is_a_match(cmdline, expect):
    assert pc.cmdline_runs_script(cmdline, TARGETS) == expect


@pytest.mark.parametrize("cmdline", [
    # 只是**提到**檔名——舊的子字串比對會全部命中
    # `grep -n webrunner_novelai.py` 不在這份清單裡——它的參數**就是**裸檔名，
    # 路徑比對本來就擋不住；擋住它的是行程名那一關，見
    # `test_a_grep_is_rejected_by_the_process_name_gate`。
    ["git", "commit", "-m", "webrunner_novelai.py: 修好對話框"],
    ["code", "--goto", "webrunner_novelai.py:120"],
    ["bash", "-c", "sed -n 1,40p axiomatic/webrunner_novelai.py.orig"],
    ["tail", "-f", "webrunner_novelai.py.log"],
    # 這三個是 Python 行程、參數也真的指向那支檔案，但它在**處理**而不是執行它
    ["python", "-c", "print('webrunner_novelai.py')"],
    ["python", "-m", "black", "axiomatic/webrunner_novelai.py"],
    ["python", "-m", "pyflakes", "webrunner_novelai.py"],
    # 檔名只對了一部分
    ["python", "my_webrunner_novelai.py"],
    ["python", "webrunner_novelai.pyc"],
    [], None, [None, 3, object()],
])
def test_merely_naming_the_file_is_not_a_match(cmdline):
    assert pc.cmdline_runs_script(cmdline, TARGETS) is None


# --------------------------------------------- _find_all_webrunner_pids -----

def test_the_real_world_scan_finds_two_not_seven(monkeypatch):
    """回歸測試：2026-08-30 那次實際掃描。"""
    _install(monkeypatch, REAL_WORLD_SCAN)
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    found, scan_ok = pc._find_all_webrunner_pids()
    # 37584 的父行程 43512 也在命中清單裡（virtualenv 轉接殼），所以併成一筆，
    # 留下最外層的 43512。
    assert found == [(43512, "webrunner_novelai.py")], found
    assert scan_ok is True, "掃描順利跑完就該回報完整"


def test_the_five_false_positives_are_each_rejected(monkeypatch):
    """逐筆點名——整體斷言通過但原因不對的話，這支會說出是哪一筆漏了。"""
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    for proc in REAL_WORLD_SCAN[2:]:
        _install(monkeypatch, [proc])
        assert pc._find_all_webrunner_pids() == ([], True), (
            f"pid {proc.info['pid']} ({proc.info['name']}) 不該被當成背景產圖程式："
            f"{proc._cmdline}")


def test_an_editor_holding_the_file_open_is_not_swept(monkeypatch):
    """最貴的那個誤判：編輯器被殺＝未存檔的內容沒了。"""
    _install(monkeypatch, [
        _FakeProc(4242, "Code.exe",
                  ["Code.exe", r"D:\Work\Example\Axiomatic\webrunner_novelai.py"]),
    ])
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    assert pc._find_all_webrunner_pids() == ([], True)


def test_both_variants_are_still_found(monkeypatch):
    _install(monkeypatch, [
        _FakeProc(10, "python.exe", ["python", "-u", "/a/webrunner_novelai.py"]),
        _FakeProc(11, "python.exe", ["python", "-u", "/a/webrunner_je_only.py"]),
    ])
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    found, scan_ok = pc._find_all_webrunner_pids()
    assert sorted(found) == [
        (10, "webrunner_novelai.py"), (11, "webrunner_je_only.py")]
    assert scan_ok is True


def test_the_py_launcher_stub_pair_collapses(monkeypatch):
    """`py -3 webrunner_novelai.py`：`py.exe` 也算直譯器，而它與本尊是一對。"""
    _install(monkeypatch, [
        _FakeProc(20, "py.exe", ["py", "-3", "/a/webrunner_novelai.py"], ppid=1),
        _FakeProc(21, "python.exe",
                  ["python.exe", "/a/webrunner_novelai.py"], ppid=20),
    ])
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    assert pc._find_all_webrunner_pids() == (
        [(20, "webrunner_novelai.py")], True)


def test_the_scanner_never_asks_psutil_for_ppid_in_bulk(monkeypatch):
    """成本守門：`attrs=[..., "ppid"]` 在 Windows 是 O(N²)（每筆重建全系統對照表）。

    本機實測 361 個行程：`attrs=["pid","ppid"]` 要 3.7 秒，而 `attrs=["pid","name"]`
    只要 118 ms。這條路徑會被 `/sys health`／`/sys doctor` 直接呼叫。
    """
    fake = _install(monkeypatch, REAL_WORLD_SCAN)
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    pc._find_all_webrunner_pids()
    assert fake.attrs_seen, "根本沒呼叫 process_iter"
    for attrs in fake.attrs_seen:
        assert "ppid" not in attrs, (
            f"process_iter(attrs={attrs}) 又把 ppid 拉進批次取值了——"
            "psutil 在 Windows 上每取一次 ppid 就重建整台機器的對照表。"
            "改成只對命中的那幾筆呼叫 `proc.ppid()`。")
        assert "cmdline" not in attrs, (
            f"process_iter(attrs={attrs})：cmdline 也不必對全機取，"
            "先用 name 篩掉非 Python 行程再逐筆讀。")


def test_own_pid_is_never_returned(monkeypatch):
    _install(monkeypatch, [
        _FakeProc(777, "python.exe", ["python", "/a/webrunner_novelai.py"]),
    ])
    monkeypatch.setattr(os, "getpid", lambda: 777)
    assert pc._find_all_webrunner_pids() == ([], True)


def test_a_process_that_dies_mid_scan_is_skipped_not_fatal(monkeypatch):
    fake = _FakePsutil([])
    procs = [
        _FakeProc(30, "python.exe", None, raises=_FakePsutil.NoSuchProcess()),
        _FakeProc(31, "python.exe", ["python", "/a/webrunner_novelai.py"]),
        _FakeProc(32, "python.exe", None, raises=_FakePsutil.AccessDenied()),
    ]
    fake._procs = procs
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    assert pc._find_all_webrunner_pids() == (
        [(31, "webrunner_novelai.py")], True), (
        "單一行程在列舉途中消失是常態，不算掃描失敗")


def test_no_psutil_yields_an_empty_list_not_a_crash(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def fake_import(name, *a, **kw):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", fake_import)
    # 沒有 psutil 就掃不了，所以名單是空的**而且**要誠實說掃描不完整——
    # 空名單本身無法區分「掃不了」與「機器很乾淨」。兩個掃描器同一條規則。
    assert pc._find_all_webrunner_pids() == ([], False)
    assert pc._find_all_chrome_processes() == ([], False)


def test_a_grep_is_rejected_by_the_process_name_gate(monkeypatch):
    """`grep -n webrunner_novelai.py -r .` 的參數**就是**裸檔名，路徑比對擋不住它
    ——擋住它的是「行程名必須是 Python 直譯器」那一關。兩道關卡缺一不可。"""
    assert pc.cmdline_runs_script(
        ["grep", "webrunner_novelai.py"], TARGETS) == "webrunner_novelai.py", (
        "前提變了：路徑比對現在自己就擋得住裸檔名，這支測試要重寫")
    _install(monkeypatch, [
        _FakeProc(50, "grep.exe", ["grep", "webrunner_novelai.py"]),
    ])
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    assert pc._find_all_webrunner_pids() == ([], True)


def test_a_python_linter_on_the_file_is_rejected_by_the_path_gate(monkeypatch):
    """反過來：`python -m black <那支檔案>` 通過了行程名那一關，
    擋住它的是「必須是**正在執行**的那一格」。"""
    _install(monkeypatch, [
        _FakeProc(51, "python.exe",
                  ["python", "-m", "black", "axiomatic/webrunner_novelai.py"]),
    ])
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    assert pc._find_all_webrunner_pids() == ([], True)


# ------------------------------------------------------ chrome discovery ----

def test_chrome_discovery_is_name_based_and_nuclear(monkeypatch):
    """Chrome 那一輪**刻意**只看行程名、不看 cmdline（見該函式 docstring）。
    這裡把現況釘住，免得有人「順手」把它也改成路徑比對而漏掉 renderer。"""
    _install(monkeypatch, [
        _FakeProc(40, "chrome.exe", []),
        _FakeProc(41, "chromedriver.exe", []),
        _FakeProc(42, "msedge.exe", []),        # 使用者平常用的瀏覽器，不動
        _FakeProc(43, "firefox.exe", []),
    ])
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    procs, scan_ok = pc._find_all_chrome_processes()
    assert sorted(procs) == [(40, "chrome.exe"), (41, "chromedriver.exe")]
    assert scan_ok is True, "掃描順利跑完就該回報完整"


# ------------------------------------------------------- pid liveness -------

def test_nonpositive_pids_are_dead_without_touching_the_os(monkeypatch):
    def boom(*_a, **_kw):
        raise AssertionError("不該對 pid<=0 做任何系統呼叫")
    monkeypatch.setattr(os, "kill", boom)
    for pid in (0, -1, -99999, None):
        assert pc._pid_alive(pid) is False, pid


def test_windows_liveness_never_reaches_os_kill(monkeypatch):
    """CLAUDE.md 的跨領域硬規則：Windows 上 `os.kill(pid, 0)` 是 Ctrl+C，不是探測。"""
    monkeypatch.setattr(os, "name", "nt")

    def boom(*_a, **_kw):
        raise AssertionError("Windows 路徑碰到了 os.kill")
    monkeypatch.setattr(os, "kill", boom)
    monkeypatch.setattr(pc, "_nt_pid_alive", lambda pid: "sentinel")
    assert pc._pid_alive(1234) == "sentinel"


def test_posix_undecidable_reads_as_dead(monkeypatch):
    """三份 `_pid_alive` 對「判不出來」的答案**刻意不一致**（見 CLAUDE.md）。
    這一份是 False——它回答的是「要不要當作已經停了」。

    ⚠️ **不要加 `@pytest.mark.skipif(os.name == "nt", ...)` 回來。** 這支自己就把
    平台假掉了（下一行的 `setattr(os, "name", "posix")`），連 `os.kill` 也是假的，
    所以它**不需要**真的 POSIX。加上那個標記的效果是讓它在唯一會跑測試的這台機器
    上永遠 skip——而本專案沒有 CI，等於這支從來沒執行過。2026-09-12 移除，移除當下
    實測兩支都在 Windows 上通過。`test_suite_safety` 現在會擋住這個形狀。"""
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", lambda *_a: (_ for _ in ()).throw(OSError()))
    assert pc._pid_alive(1234) is False


def test_permission_error_reads_as_alive(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(
        os, "kill", lambda *_a: (_ for _ in ()).throw(PermissionError()))
    assert pc._pid_alive(1234) is True


def test_nt_liveness_prefers_psutil(monkeypatch):
    class P:
        @staticmethod
        def pid_exists(pid):
            return pid == 4242
    monkeypatch.setitem(sys.modules, "psutil", P)
    assert pc._nt_pid_alive(4242) is True
    assert pc._nt_pid_alive(4243) is False


def test_nt_liveness_says_alive_when_psutil_itself_breaks(monkeypatch):
    """判不出來就保守回「還活著」——寧可多等，也不要對一個還在跑的東西宣告死亡。"""
    class P:
        @staticmethod
        def pid_exists(pid):
            raise RuntimeError("psutil 壞了")
    monkeypatch.setitem(sys.modules, "psutil", P)
    assert pc._nt_pid_alive(1) is True


# ------------------------------------------------------------ kill paths ----

def test_kill_by_pid_reports_gone_without_signalling(monkeypatch):
    monkeypatch.setattr(pc, "_pid_alive", lambda pid: False)

    def boom(*_a, **_kw):
        raise AssertionError("已經死了還送訊號")
    monkeypatch.setattr(os, "kill", boom)
    assert pc._kill_by_pid(123) is True


def test_kill_by_pid_swallows_a_failed_signal(monkeypatch, capsys):
    monkeypatch.setattr(pc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(os, "kill", lambda *_a: (_ for _ in ()).throw(OSError("nope")))
    assert pc._kill_by_pid(123) is False
    assert "SIGTERM" in capsys.readouterr().err


def test_force_kill_on_windows_shells_out_to_taskkill(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    seen = []
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kw: seen.append((args, kw)))
    pc._force_kill_pid(4321)
    args, kw = seen[0]
    assert args == ["taskkill", "/F", "/PID", "4321"]
    assert kw.get("check") is False, "check=True 會讓「行程已經死了」變成例外"


def test_force_kill_survivors_only_touches_the_living(monkeypatch):
    alive = {2}
    monkeypatch.setattr(pc, "_pid_alive", lambda pid: pid in alive)
    killed = []
    monkeypatch.setattr(pc, "_force_kill_pid", killed.append)
    out = pc._force_kill_survivors([(1, "a.py"), (2, "b.py"), (3, "c.py")])
    assert killed == [2]
    assert out == [(2, "b.py")]


def test_signalling_a_vanished_pid_does_not_abort_the_rest(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    sent = []

    def kill(pid, sig):
        if pid == 2:
            raise OSError("gone")
        sent.append(pid)
    monkeypatch.setattr(os, "kill", kill)
    out = pc._signal_webrunner_pids([(1, "a"), (2, "b"), (3, "c")])
    assert sent == [1, 3]
    assert out == [(1, "a"), (3, "c")], "掛掉的那一筆不該出現在『已送出』清單裡"


def test_chrome_kill_asks_for_the_whole_tree_and_forces_it(monkeypatch):
    """Chrome 那一輪用 `/T /F`（整棵樹＋強制），與背景程式那輪的 `/T`（先禮）不同。"""
    monkeypatch.setattr(os, "name", "nt")
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: seen.append(args))
    pc._kill_chrome_pids([(1, "chrome.exe"), (2, "chromedriver.exe")])
    assert seen == [["taskkill", "/PID", "1", "/T", "/F"],
                    ["taskkill", "/PID", "2", "/T", "/F"]]


def test_chrome_kill_never_raises(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    pc._kill_chrome_pids([(1, "chrome.exe")])


def test_the_polite_signal_does_not_force(monkeypatch):
    """`_signal_webrunner_pids` 刻意**不帶** `/F`——先給機會自己收尾，
    5 秒後才由 `_force_kill_survivors` 強制。"""
    monkeypatch.setattr(os, "name", "nt")
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: seen.append(args))
    pc._signal_webrunner_pids([(9, "webrunner_novelai.py")])
    assert seen == [["taskkill", "/PID", "9", "/T"]]
    assert "/F" not in seen[0]


# -------------------------------------------------- collapse edge cases -----

def test_collapse_keeps_an_orphaned_inner_process():
    """外層轉接殼已經死了（不在命中清單裡），內層還活著 → 內層要留下來。"""
    raw = [(101, 100, "webrunner_novelai.py")]      # 100 不在 raw 裡
    assert pc.collapse_interpreter_stub_pairs(raw) == [(101, "webrunner_novelai.py")]


def test_collapse_handles_two_independent_stub_pairs():
    raw = [(100, 1, "a.py"), (101, 100, "a.py"),
           (200, 1, "b.py"), (201, 200, "b.py")]
    assert sorted(pc.collapse_interpreter_stub_pairs(raw)) == [
        (100, "a.py"), (200, "b.py")]


def test_collapse_is_empty_for_empty_input():
    assert pc.collapse_interpreter_stub_pairs([]) == []
    assert pc.collapse_interpreter_stub_pairs([], exclude_pid=5) == []


# ------------------------------------------------------- the sweep itself ---

class _FakePopen:
    def __init__(self, running=True, wait_raises=None):
        self._running = running
        self._wait_raises = wait_raises
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._running else 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if self._wait_raises is not None:
            raise self._wait_raises
        return 0


def _sweep(monkeypatch, tracked, pid, survivors=(), chrome=(), scan_ok=True,
           rescan_ok=None, rescan=(), wr_scan_ok=True):
    """`rescan_ok` / `rescan` 只影響**第二次**掃描（殺完之後的複查）。

    兩次掃描要能分開設定，否則「複查自己掛了」那條分支根本測不到——它的前提正是
    第一次成功、第二次失敗。預設 `None` ＝ 跟第一次一樣，既有呼叫端不受影響。
    """
    calls = {"n": 0}

    def _scan():
        calls["n"] += 1
        if calls["n"] == 1:
            return list(chrome), scan_ok
        return (list(rescan),
                scan_ok if rescan_ok is None else rescan_ok)

    monkeypatch.setattr(pc, "_find_all_webrunner_pids",
                        lambda *_a, **_k: (list(survivors), wr_scan_ok))
    monkeypatch.setattr(pc, "_find_all_chrome_processes", _scan)
    monkeypatch.setattr(pc, "_signal_webrunner_pids", lambda s: list(s))
    monkeypatch.setattr(pc, "_force_kill_survivors", lambda s: [])
    monkeypatch.setattr(pc, "_kill_chrome_pids", lambda c: None)
    monkeypatch.setattr(pc, "_pid_alive", lambda pid: False)
    return asyncio.run(pc._terminate_all_webrunner_instances(tracked, pid))


def test_the_sweep_reports_a_clean_stop(monkeypatch):
    proc = _FakePopen()
    lines = _sweep(monkeypatch, proc, 500)
    assert proc.terminated and not proc.killed
    assert lines == ["已停止背景程式"]


def test_the_sweep_force_kills_a_process_that_will_not_exit(monkeypatch):
    proc = _FakePopen(wait_raises=subprocess.TimeoutExpired("x", 10))
    lines = _sweep(monkeypatch, proc, 500)
    assert proc.killed
    assert lines == ["已強制結束背景程式"]


def test_the_sweep_says_nothing_when_there_is_nothing_to_do(monkeypatch):
    assert _sweep(monkeypatch, None, None) == []


@pytest.mark.parametrize("scan_ok,rescan_ok,wr_scan_ok", [
    (True, True, True),     # 一切順利
    (False, False, True),   # 瀏覽器掃描不完整
    (True, False, True),    # 複查掛掉
    (True, True, False),    # 背景程式掃描不完整
    (False, False, False),  # 兩邊都掃不成
])
def test_the_sweep_never_leaks_a_pid_or_a_path_into_its_reply(
        monkeypatch, scan_ok, rescan_ok, wr_scan_ok):
    """洩漏規則第 1 層：回傳值會被拼進 Discord 訊息，PID／路徑／服務名一律不得出現。

    **失敗路徑也要掃。** 原本只跑「順利」那一條，於是後來新增的錯誤訊息
    完全不在這支守門的視線內——而錯誤訊息正是最容易夾帶原始例外文字、
    主機路徑與 PID 的地方（本專案的洩漏幾乎都出在這一類字串上）。
    """
    lines = _sweep(monkeypatch, _FakePopen(), 31337,
                   survivors=[(4242, "webrunner_novelai.py")],
                   chrome=[(1, "chrome.exe"), (2, "chromedriver.exe")],
                   scan_ok=scan_ok, rescan_ok=rescan_ok,
                   wr_scan_ok=wr_scan_ok)
    blob = " ".join(lines)
    for banned in ("31337", "4242", "webrunner", "novelai", "je_only",
                   ".py", "D:\\", "/d/"):
        assert banned not in blob, f"回傳的 status line 洩漏了 {banned!r}：{blob!r}"


def test_a_failed_background_scan_does_not_look_like_a_clean_machine(monkeypatch):
    """掃不到漏網的背景程式，跟「真的沒有漏網的」，**必須說得不一樣**。

    這一條比 chrome 那條重。漏掉的是**背景產圖程式本體**——它會自己再開一個
    瀏覽器，於是兩個實例同時寫同一份佇列、搶同一個瀏覽器設定檔，登入態與佇列
    都會壞掉。而修之前兩者送出的訊息一字不差（都是什麼都不說）。
    """
    clean = _sweep(monkeypatch, None, None, survivors=(), wr_scan_ok=True)
    broken = _sweep(monkeypatch, None, None, survivors=(), wr_scan_ok=False)
    assert clean == [], f"乾淨的機器不該多話：{clean!r}"
    assert broken, "掃描失敗卻什麼都不說——這正是要修掉的缺陷"
    assert broken != clean, "掃描失敗與乾淨機器送出同一組訊息，使用者無從分辨"


def test_a_partial_background_scan_still_admits_it(monkeypatch):
    """掃到一半才炸：有東西可殺，但「就這些了」這個保證已經不成立。

    跟空名單是**兩個不同的洞**。只補一個的話，使用者會收到一句聽起來很完整的
    「sweep 清掉 1 個漏網的」，而機器上其實還有第二個沒被掃到。
    """
    ok = _sweep(monkeypatch, None, None,
                survivors=[(4242, "webrunner_novelai.py")], wr_scan_ok=True)
    partial = _sweep(monkeypatch, None, None,
                     survivors=[(4242, "webrunner_novelai.py")],
                     wr_scan_ok=False)
    assert len(partial) > len(ok), f"掃描不完整時應該多一句提醒：{partial!r}"


def test_a_working_background_scan_stays_quiet(monkeypatch):
    """反方向：一切正常時不得多印警告。

    沒有這一支，把 `wr_scan_ok` 寫死成 False 也能讓上面兩支通過——那樣守門就
    退化成「永遠在抱怨」，跟永遠沉默一樣沒有資訊。
    """
    lines = _sweep(monkeypatch, None, None,
                   survivors=[(4242, "webrunner_novelai.py")], wr_scan_ok=True)
    assert not any("沒掃完整" in ln for ln in lines), lines


def test_a_background_scan_that_dies_midway_returns_what_it_got(monkeypatch):
    """函式層的對照：列舉途中炸掉要留住已經掃到的，但**必須**回報不完整。

    形狀取自真實成因：Windows 上列舉全系統行程會遇到權限不足或 WMI 卡住，
    `process_iter` 在**產出幾筆之後**才拋錯，所以這裡用產生器而不是 list。
    """
    fake = _install(monkeypatch, [])

    def dying_iter(attrs=None):
        yield _FakeProc(60, "python.exe", ["python", "/a/webrunner_novelai.py"])
        raise OSError(5, "Access is denied")

    fake.process_iter = dying_iter
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    found, scan_ok = pc._find_all_webrunner_pids()
    assert found == [(60, "webrunner_novelai.py")], (
        f"已經掃到的應該留住——能殺一個是一個：{found!r}")
    assert scan_ok is False, "掃描中途死掉卻回報完整，呼叫端就會宣稱查過了"


def test_a_failed_chrome_scan_does_not_look_like_a_clean_machine(monkeypatch):
    """掃描整個炸掉，跟「機器上真的沒有 chrome」，**必須說得不一樣**。

    這是這支守門的全部理由。修之前兩者送給使用者的訊息一字不差（都是什麼都不
    說），所以使用者以為清乾淨了，實際上一個都沒殺，而下一次啟動瀏覽器才會撞上
    殘留的 singleton lock——症狀出現在別的地方、別的時間，幾乎不可能回推。

    注意警告必須發生在名單為空的時候。修之前那整段掃描結果的處理都關在
    `if chrome_procs:` 裡面，而掃描一開始就炸的時候名單正好是空的——**最需要
    說話的情況，剛好是唯一不會說話的情況**。
    """
    clean = _sweep(monkeypatch, None, None, chrome=(), scan_ok=True)
    broken = _sweep(monkeypatch, None, None, chrome=(), scan_ok=False)
    assert clean == [], f"乾淨的機器不該多話：{clean!r}"
    assert broken, "掃描失敗卻什麼都不說——這正是修掉的那個缺陷"
    assert broken != clean, (
        "掃描失敗與乾淨機器送出同一組訊息，使用者無從分辨")


def test_an_incomplete_scan_does_not_claim_it_killed_them_all(monkeypatch):
    """掃到一半才炸：有東西可殺，但「殺光所有」這個保證已經不成立。

    上面那支測的是「空名單」，這支測的是「部分名單」——**兩個不同的洞**，
    只補一個的話另一個照樣是靜默的錯誤結果：使用者收到一句聽起來很完整的
    「殺光所有 chrome：2 個」，而機器上其實還有第三個沒被掃到。
    """
    ok = _sweep(monkeypatch, None, None,
                chrome=[(1, "chrome.exe"), (2, "chromedriver.exe")],
                scan_ok=True)
    partial = _sweep(monkeypatch, None, None,
                     chrome=[(1, "chrome.exe"), (2, "chromedriver.exe")],
                     scan_ok=False)
    assert any("殺光所有" in ln for ln in ok), ok
    assert not any("殺光所有" in ln for ln in partial), (
        f"掃描不完整卻仍宣稱殺光所有：{partial!r}")
    assert len(partial) > len(ok), "掃描不完整時應該多一句提醒"


def test_a_scan_that_dies_midway_returns_what_it_got_and_admits_it(monkeypatch):
    """列舉途中炸掉：名單留住已經掃到的，但**必須**回報不完整。

    這是函式層的對照；上面兩支測的是呼叫端怎麼講。兩層都要釘，因為部分名單本身
    是合理的行為（能殺一個是一個），錯的是把部分名單當成全部。

    形狀取自真實成因：Windows 上列舉全系統行程會遇到權限不足或 WMI 卡住，
    `process_iter` 在**產出幾筆之後**才拋錯。所以這裡用產生器而不是 list。
    """
    fake = _install(monkeypatch, [])

    def dying_iter(attrs=None):
        yield _FakeProc(40, "chrome.exe", [])
        yield _FakeProc(41, "chromedriver.exe", [])
        raise OSError(5, "Access is denied")

    fake.process_iter = dying_iter
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    procs, scan_ok = pc._find_all_chrome_processes()
    assert sorted(procs) == [(40, "chrome.exe"), (41, "chromedriver.exe")], (
        "已經掃到的應該留住——能殺一個是一個")
    assert scan_ok is False, "掃描中途死掉卻回報完整，呼叫端就會宣稱殺光了"


def test_the_sweep_still_says_nothing_extra_when_everything_worked(monkeypatch):
    """反方向：一切正常時不得多印警告。

    沒有這一支，把 `scan_ok` 直接寫死成 False 也能讓上面兩支通過——
    那樣守門就退化成「永遠在抱怨」，跟永遠沉默一樣沒有資訊。
    """
    lines = _sweep(monkeypatch, None, None,
                   chrome=[(1, "chrome.exe")], scan_ok=True)
    assert not any("沒有掃完整" in ln or "不確定" in ln for ln in lines), lines


def test_a_failed_recheck_does_not_pass_for_a_clean_result(monkeypatch):
    """第一次掃描成功、殺完之後的**複查**掛掉：不得沉默地當成「都關掉了」。

    「沒有倖存者」這個結論完全來自複查。複查沒跑完的時候那個結論沒有根據，而
    沉默剛好等於在宣告它成立——跟真的清乾淨無法區分。這是同一個缺陷的第三種
    形狀（前兩種是空名單與部分名單），三個都要各自釘住。
    """
    good = _sweep(monkeypatch, None, None, chrome=[(1, "chrome.exe")],
                  scan_ok=True, rescan_ok=True, rescan=())
    bad = _sweep(monkeypatch, None, None, chrome=[(1, "chrome.exe")],
                 scan_ok=True, rescan_ok=False, rescan=())
    assert not any("不確定" in ln for ln in good), good
    assert any("不確定" in ln for ln in bad), (
        f"複查掛掉卻報得像清乾淨了：{bad!r}")


def test_the_sweep_counts_chrome_and_driver_separately(monkeypatch):
    lines = _sweep(monkeypatch, None, None,
                   chrome=[(1, "chrome.exe"), (2, "chrome.exe"),
                           (3, "chromedriver.exe")])
    assert any("2" in ln and "1" in ln and "3" in ln for ln in lines), lines


# --------------------------------------------------------- static guards ----

def _fn(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"找不到 {name}")


def test_the_scanner_still_routes_through_the_path_predicate():
    """AST 而不是字串比對——這支測試自己的說明文字裡就有那兩個函式名。"""
    tree = ast.parse((PKG_ROOT / "_process_control.py").read_text(encoding="utf-8"))
    fn = _fn(tree, "_find_all_webrunner_pids")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for required in ("looks_like_python_process", "cmdline_runs_script"):
        assert required in called, (
            f"`_find_all_webrunner_pids` 不再呼叫 `{required}`。"
            "回到子字串比對的話，任何提到檔名的命令列都會被 taskkill /F 掃掉。")


def test_the_launcher_uses_the_same_predicate():
    """啟動器的「已經有另一個實例」掃描是同一個坑的第二個現場。

    2026-09-03 實作從 `start_discord_bot._other_launcher_pids` **上移**到
    `_supervisor.other_launcher_pids`，好讓 `start_webrunner.py` 用同一份。守門
    跟著搬家，檢查的東西一個都沒少——搬家的時候把規則弄丟，正是這支測試要防的。
    順帶把涵蓋面從一支啟動器擴大到「共用的那一份」，所以兩支同時受保護。
    """
    src = (PKG_ROOT / "_supervisor.py").read_text(encoding="utf-8")
    fn = _fn(ast.parse(src), "other_launcher_pids")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "cmdline_runs_script" in called, (
        "啟動器又用回子字串比對了——那會把任何提到啟動器檔名的命令列"
        "（shell、`python -c`）算成「已經有一個實例在跑」。")
    assert "looks_like_python_process" in called, (
        "少了 Python 行程的前置過濾——非 Python 的行程也會被算進來。")
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "process_iter"):
            for kw in node.keywords:
                names = ast.literal_eval(kw.value) if kw.arg == "attrs" else ()
                assert "ppid" not in (names or ()), (
                    "啟動器又把 ppid 拉進批次取值了（Windows 上 O(N²)，"
                    "而這是每次啟動都會跑的路徑）")


def test_both_launchers_route_through_the_shared_scanner():
    """兩支啟動器都不得自己再寫一份掃描——那正是會各自漂移的配對。"""
    for launcher in ("start_discord_bot.py", "start_webrunner.py"):
        src = (PKG_ROOT.parent / launcher).read_text(encoding="utf-8")
        called = {n.func.id for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "other_launcher_pids" in called, (
            f"{launcher} 沒有用共用的 `other_launcher_pids`。")
        assert "process_iter" not in called, (
            f"{launcher} 自己又寫了一份行程掃描，沒有走共用實作。")


def _scope_of_each_node(tree):
    """`{id(node): 所屬函式節點}`。**先建一次**，不要在逐節點的迴圈裡再
    `ast.walk`——`discord_bot.py` 有一萬多行，那是 O(n²)。"""
    scope = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            scope.setdefault(id(node), fn)
    return scope


def _names_in_branch_tests(fn):
    """這個函式裡，出現在**分支條件**（if / while / 三元）中的名字。"""
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.If, ast.IfExp, ast.While)):
            for sub in ast.walk(node.test):
                if isinstance(sub, ast.Name):
                    out.add(sub.id)
    return out


def _scan_result_is_dropped(src, scanner):
    """回「把 `scanner` 的第二個回傳值丟掉」的呼叫點清單 `[(函式名, 行號, 原因), …]`。

    兩個判準，都是變異測試逼出來的：

    1. **看識別字，不是看呼叫節點。** 四個呼叫點裡有三個是
       `await asyncio.to_thread(_find_all_webrunner_pids, pid)`——掃描器在那裡是
       引數（`ast.Name`），只認 `ast.Call` 的話那三個完全在視線外。
    2. **那個旗標要出現在分支條件裡，不是「有被讀到」就算。**
       `print(f"… scan_ok={flag}")` 也算讀到，可是 stderr 擋不住任何事；要讓呼叫端
       做對事，訊號就必須真的改變它的行為。
    """
    tree = ast.parse(src)
    scope = _scope_of_each_node(tree)
    branch_names = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            branch_names[id(fn)] = _names_in_branch_tests(fn)
    # 每個 Assign 的 value 底下的節點 -> 那個 Assign，先建一次（O(n)）。
    assign_of = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for sub in ast.walk(node.value):
                assign_of[id(sub)] = node
    bad = []
    for ref in ast.walk(tree):
        if not (isinstance(ref, ast.Name) and ref.id == scanner
                and isinstance(ref.ctx, ast.Load)):
            continue
        fn = scope.get(id(ref))
        where = fn.name if fn else "<module>"
        assign = assign_of.get(id(ref))
        if assign is None or len(assign.targets) != 1:
            bad.append((where, ref.lineno, "回傳值沒有被解開"))
            continue
        target = assign.targets[0]
        if not (isinstance(target, ast.Tuple) and len(target.elts) == 2
                and all(isinstance(e, ast.Name) for e in target.elts)):
            bad.append((where, ref.lineno, "沒有解成 (名單, 掃描是否完整)"))
            continue
        flag = target.elts[1].id
        if fn is None or flag not in branch_names.get(id(fn), set()):
            bad.append((where, ref.lineno, f"`{flag}` 沒有影響任何判斷"))
    return bad


def test_every_caller_actually_uses_the_scan_completeness_flag():
    """四個呼叫點都要**用到**「掃描是否完整」，不是解開來就丟掉。

    改成回兩個值本身不會讓任何呼叫端變對——`orphans, _ = …` 一樣編得過、一樣
    靜默。真正要守的是那個訊號有沒有走到做決定的地方：`/sys health` 說「⏸️ not
    running」、`/sys doctor` 說「no obvious blocker」，兩句話在掃不成的時候都
    是沒有根據的結論，而它們的後果是使用者直接下 `/gen run`，於是 sweep 什麼
    都沒殺，兩個背景產圖程式同時搶同一個瀏覽器設定檔。

    `_find_all_chrome_processes` 一起守——同一個模組、同一個形狀，兩個掃描器
    的呼叫端規則一致，才不會下一個人只照著其中一半抄。
    """
    for name in ("discord_bot.py", "_process_control.py"):
        src = (PKG_ROOT / name).read_text(encoding="utf-8")
        for scanner in ("_find_all_webrunner_pids", "_find_all_chrome_processes"):
            bad = _scan_result_is_dropped(src, scanner)
            assert not bad, (
                f"{name} 這些地方把 `{scanner}` 的「掃描是否完整」丟掉了："
                + "、".join(f"{w}（第 {ln} 行）：{why}" for w, ln, why in bad)
                + "。掃不成跟「機器很乾淨」回的都是空名單，"
                  "呼叫端不處理的話這個修正等於沒做。")


def test_the_dropped_flag_scanner_actually_catches_things():
    """守門自己的 canary——掃描器壞掉的話上面那支會永遠是綠的。

    每一種壞形狀都各放一個樣本，而且**真實原始碼裡每一種寫法也要各放一個好樣本**：
    好樣本少一種，那一種在原始碼清乾淨之後就永遠測不到了（本專案已經踩過三次）。
    """
    good = {
        "直接呼叫": (
            "def f():\n"
            "    rows, ok = _find_all_webrunner_pids()\n"
            "    if not ok:\n"
            "        return 'unknown'\n"
            "    return rows\n"),
        "丟給 to_thread": (
            "async def f():\n"
            "    rows, ok = await asyncio.to_thread(_find_all_webrunner_pids, 1)\n"
            "    return rows if ok else 'unknown'\n"),
    }
    for label, src in good.items():
        assert _scan_result_is_dropped(src, "_find_all_webrunner_pids") == [], (
            f"好樣本「{label}」被誤判成丟掉旗標")
    bad_shapes = {
        "解開但沒人讀": (
            "def f():\n"
            "    rows, ok = _find_all_webrunner_pids()\n"
            "    return rows\n"),
        "只印到 stderr 不影響判斷": (
            "def f():\n"
            "    rows, ok = _find_all_webrunner_pids()\n"
            "    print(f'scan_ok={ok}')\n"
            "    return rows\n"),
        "只取第一個": (
            "def f():\n"
            "    rows = _find_all_webrunner_pids()[0]\n"
            "    return rows\n"),
        "整包當名單用": (
            "def f():\n"
            "    rows = _find_all_webrunner_pids()\n"
            "    return len(rows)\n"),
        "to_thread 但丟掉旗標": (
            "async def f():\n"
            "    rows, ok = await asyncio.to_thread(_find_all_webrunner_pids, 1)\n"
            "    return rows\n"),
    }
    for label, bad_src in bad_shapes.items():
        assert _scan_result_is_dropped(bad_src, "_find_all_webrunner_pids"), (
            f"掃描器看不到「{label}」這個形狀")


def test_the_bot_does_not_scan_on_the_event_loop():
    """模組 docstring 的規則：阻塞工作一律 `asyncio.to_thread`。

    `cmd_health` / `cmd_doctor` 是 coroutine，跟 gateway heartbeat 共用 event loop。
    """
    src = (PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders = []
    for fname in ("cmd_health", "cmd_doctor"):
        fn = _fn(tree, fname)
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_find_all_webrunner_pids"):
                offenders.append(f"{fname}:{node.lineno}")
    assert not offenders, (
        f"這些地方直接呼叫了掃描函式而沒有丟到執行緒：{offenders}。"
        "改成 `await asyncio.to_thread(_find_all_webrunner_pids, ...)`。")


# ------------------------------------- 全專案：process_iter 的 attrs 規則 ----

def _project_py_files():
    # repo root 那一半是 `*.py`：這條規則沒有指名任何模組，收窄成 `start_*.py`
    # 會讓 root 上的其他正式腳本落在視線外（2026-09-20 連同另外五處一起收）。
    # 測試 2026-09-22 起住在 repo 根目錄的 `test/`（本檔所在的目錄）；搬家前它們在
    # 套件的 glob 裡，所以另外列，範圍照舊。
    root = PKG_ROOT.parent
    out = (list(PKG_ROOT.glob("*.py")) + list(Path(__file__).resolve().parent.glob("*.py"))
           + list(root.glob("*.py")))
    return [p for p in out if "legacy" not in p.parts]


def _process_iter_attrs(path):
    """回 [(lineno, [attr, …]), …]——這個檔案裡每一次 `process_iter(attrs=[…])`。"""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "process_iter"):
            continue
        for kw in node.keywords:
            if kw.arg != "attrs":
                continue
            try:
                found.append((node.lineno, list(ast.literal_eval(kw.value))))
            except (ValueError, TypeError):
                pass            # 非字面值，交給人看
    return found


def test_the_scan_inventory_is_not_empty():
    """先證明掃描器真的找得到東西——空清單通過是最沒用的綠燈。"""
    total = sum(len(_process_iter_attrs(p)) for p in _project_py_files())
    assert total >= 8, f"只找到 {total} 個 process_iter(attrs=…)，掃描條件可能壞了"


def test_no_scan_asks_psutil_for_ppid_in_bulk():
    """`attrs=[…, "ppid"]` 在 Windows 上永遠是 O(N²)，沒有例外。

    psutil 的 `Process.ppid()` 實作是 `ppid_map()[self.pid]`，而 `ppid_map()` 每次
    呼叫都重建**整台機器**的父行程對照表。放進 `attrs=` 就是 N 個行程各掃一次全系統
    快照。本機實測（361 個行程）：帶 ppid 3,721 ms，不帶 118 ms。
    上游自己也認這件事——psutil 8.0.0 的變更記錄寫著 Windows 的 `Process.ppid` 快了
    約 **58 倍**（ARM64 上 99 倍）。在那之前（本機裝的是 7.2.2），先用便宜的 `name`
    篩掉九成，再對命中的那幾筆單獨呼叫 `proc.ppid()`。
    """
    offenders = [f"{p.name}:{lineno} attrs={attrs}"
                 for p in _project_py_files()
                 for lineno, attrs in _process_iter_attrs(p)
                 if "ppid" in attrs]
    assert not offenders, (
        f"這些掃描把 ppid 放進了 attrs：{offenders}。"
        "改成先用 name 篩，再對命中的那幾筆呼叫 `proc.ppid()`。")


def test_a_scan_that_filters_on_name_does_not_prefetch_cmdline():
    """既然要用 `name` 篩掉九成，就不該替全機每個行程都讀一次 PEB。

    實測（361 個行程）：`attrs=["pid","name","cmdline"]` 334 ms，
    改成先篩再逐筆 `proc.cmdline()` 121 ms。`find_browser_pids_for_profile`
    每次重啟瀏覽器都會跑一次，而預設是每個角色都重啟。
    """
    offenders = [f"{p.name}:{lineno} attrs={attrs}"
                 for p in _project_py_files()
                 for lineno, attrs in _process_iter_attrs(p)
                 if "cmdline" in attrs and "name" in attrs]
    assert not offenders, (
        f"這些掃描同時批次取了 name 與 cmdline：{offenders}。"
        "既然 name 那一關會篩掉絕大多數行程，cmdline 就留給通過的那幾筆逐一索取。")


def test_no_scan_uses_the_deprecated_empty_attrs():
    """psutil 8.0.0 起，`attrs=[]`（＝取回全部屬性）已棄用。"""
    offenders = [f"{p.name}:{lineno}"
                 for p in _project_py_files()
                 for lineno, attrs in _process_iter_attrs(p)
                 if attrs == []]
    assert not offenders, f"`process_iter(attrs=[])` 已被 psutil 棄用：{offenders}"


# ---------------------------------------------------------------------------
# 「跑著的是不是磁碟上的那一份」
#
# 2026-09-01 補。擁有者問「為什麼還是沒有用量達到的自動重試」，答案是 bot 行程比
# 那段程式碼舊四天——Python 在 import 期綁模組，編輯磁碟對活著的行程毫無影響，而
# **症狀是「功能像是沒寫」**，最容易讓人回頭重讀原始碼卻永遠讀不出問題。同一次盤點
# 還發現 supervisor 落後自己的啟動器四天，後果是 `discord_bot.log` 根本不存在，
# 而 bot 的錯誤回覆一直叫人「查看 log」。
#
# 判定邏輯把「行程來源」與「檔案時間」都做成可注入的，所以整段測得起來而不必造真的
# 行程或真的檔案——這是這支模組一貫的做法（見上面那組 fixture）。
# ---------------------------------------------------------------------------

_COMPONENTS = {
    "bot": ("discord_bot.py", ("a.py", "b.py")),
    "supervisor": ("start_discord_bot.py", ("c.py",)),
}
_MTIMES = {"a.py": 1000.0, "b.py": 2000.0, "c.py": 500.0}


def _procs(*rows):
    """(pid, name, cmdline, create_time) 的清單；cmdline 用完整路徑，跟真的一樣。"""
    return list(rows)


def _bot(pid, started, script="discord_bot.py", name="python.exe"):
    return (pid, name,
            ["C:/py/python.exe", f"D:/Work/Example/axiomatic/{script}"],
            started)


def _find(procs, **kw):
    return pc.find_stale_components(
        Path("D:/Work/Example"), _COMPONENTS,
        procs=procs, mtime=_MTIMES.get, **kw)


def test_a_process_older_than_its_code_is_reported():
    """這就是那個實際發生過的狀況：功能寫好了、測試全綠、線上跑的是舊的。"""
    got = _find(_procs(_bot(1, 1500.0)))
    assert [(row[0], row[1], row[4]) for row in got] == [("bot", 1, "b.py")], got


def test_a_process_newer_than_its_code_is_not_reported():
    assert _find(_procs(_bot(1, 3000.0))) == []


def test_a_process_started_exactly_at_the_mtime_is_not_stale():
    """邊界：同一秒啟動就當它讀到了新版。誤判成 STALE 會讓 doctor 在每次剛重啟時
    都亮紅燈——會亂叫的守門，最後會被人關掉（`test_language` 記過同一個教訓）。"""
    assert _find(_procs(_bot(1, 2000.0))) == []


def test_a_component_whose_files_are_all_missing_is_never_stale():
    """讀不到檔案時間 ⇒ 判斷不出來 ⇒ 不吵。診斷缺席比診斷說謊好。"""
    got = pc.find_stale_components(
        Path("D:/x"), _COMPONENTS, procs=_procs(_bot(1, 0.0)),
        mtime=lambda rel: None)
    assert got == []


def test_a_mtime_probe_that_raises_is_treated_as_missing():
    def _boom(rel):
        raise OSError("permission denied")
    got = pc.find_stale_components(
        Path("D:/x"), _COMPONENTS, procs=_procs(_bot(1, 0.0)), mtime=_boom)
    assert got == []


def test_a_non_python_process_is_ignored():
    rows = _procs((1, "chrome.exe",
                   ["chrome.exe", "D:/Work/Example/axiomatic/discord_bot.py"],
                   0.0))
    assert _find(rows) == []


def test_a_process_that_merely_mentions_the_script_is_ignored():
    """跟這支模組其餘部分同一條規則：子字串比對會把「命令列裡剛好提到檔名」的
    shell 與 `python -c` 算進來。2026-08-30 實測 7 筆裡有 5 筆是這種。"""
    rows = _procs((1, "python.exe",
                   ["python.exe", "-c", "print('discord_bot.py')"], 0.0))
    assert _find(rows) == []


def test_the_worst_offender_comes_first():
    """依落後多久排序：先看最舊的那一個。"""
    rows = _procs(_bot(1, 1999.0),                       # 落後 1 秒
                  _bot(2, 0.0, "start_discord_bot.py"))  # 落後 500 秒
    assert [row[1] for row in _find(rows)] == [2, 1]


@pytest.mark.parametrize("started", [None, "1500", True, [], {}])
def test_a_junk_start_time_never_raises(started):
    assert _find(_procs(_bot(1, started))) == []


def test_the_shim_and_the_real_interpreter_are_both_listed():
    """`.venv\\Scripts\\python.exe` 是轉接殼，會 spawn 真的直譯器；兩個跑同一個
    腳本、啟動時間相同，所以兩個都舊。這裡刻意**不**去重——要顯示給人看的呼叫端
    自己收斂（doctor 就是用 label 去重的）。"""
    got = _find(_procs(_bot(1, 1500.0), _bot(2, 1500.0)))
    assert sorted(row[1] for row in got) == [1, 2]


def test_the_newest_dependency_is_the_one_reported():
    mtime, who = pc.newest_dependency_mtime(
        Path("D:/x"), ("a.py", "b.py", "missing.py"), mtime=_MTIMES.get)
    assert (mtime, who) == (2000.0, "b.py")


def test_no_dependencies_at_all_yields_zero():
    assert pc.newest_dependency_mtime(Path("D:/x"), (), mtime=_MTIMES.get) == (0.0, "")


def test_the_real_component_map_names_files_that_exist():
    """對照表列的每一個檔案都要真的在 repo 裡。打錯字不會讓任何東西紅，只會讓那個
    元件的 staleness 判定安靜地少看一個檔案——正好是這個功能要防的失效模式。"""
    root = Path(__file__).resolve().parent.parent
    missing = [rel for _script, deps in pc.STALE_COMPONENTS.values()
               for rel in deps if not (root / rel).exists()]
    assert not missing, f"對照表指到不存在的檔案：{missing}"


def _project_modules(root: Path) -> dict:
    """`模組名 -> 相對路徑`（`axiomatic/*.py` ＋ repo 根目錄的 `*.py`，不含測試）。

    `test/` 照同一個判準過濾：手動 e2e 腳本在 2026-09-22 之前住在套件裡、在範圍內，
    搬家之後照舊。"""
    found = {}
    for path in (sorted(root.glob("*.py")) + sorted((root / "axiomatic").glob("*.py"))
                 + sorted((root / "test").glob("*.py"))):
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        found[path.stem] = path.relative_to(root).as_posix()
    return found


def _imported_project_modules(path: Path, known: dict) -> set:
    """`path` 會 import 到的本專案模組名。

    **`ast.walk` 而不是只看 `tree.body`**：函式裡的延遲 import 一樣進 `sys.modules`、
    一樣綁在第一次載入的那份原始碼上，所以對「改了要不要重啟」而言完全等價。

    三種寫法都要認得，少認一種就會安靜地漏掉一整條分支：
    `import x` / `from x import a` / **`from axiomatic import x`**（最後這種的
    模組名在 `names` 裡，不在 `module` 裡——第一版就是漏了它，於是
    `start_webrunner.py` 的 `_chrome_slot` 整個看不到）。
    """
    def resolve(dotted: str):
        parts = dotted.split(".")
        if parts and parts[0] == "axiomatic":
            parts = parts[1:]
        return parts[0] if parts and parts[0] in known else None

    out = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (name := resolve(alias.name)):
                    out.add(name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if (name := resolve(node.module)):
                out.add(name)
            elif node.module.split(".")[0] == "axiomatic":
                out |= {a.name for a in node.names if a.name in known}
    return out


def _import_closure(root: Path, entry: str, known: dict) -> set:
    seen, stack = set(), [entry]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(
            _imported_project_modules(root / known[current], known) - seen)
    return seen


def test_the_component_map_matches_the_real_import_closure():
    """對照表就是各進入點的**傳遞 import 閉包**，兩個方向都要對得起來。

    這張表 2026-09-09 之前是手寫的，四筆有三筆不對，而兩個方向的錯法都會誤導：

    * **少列** → `find_stale_components` 安靜地漏報。`bot` 少了七個模組，其中
      `_webrunner_shared.py` 與 `discord_rpc.py` 是動得最勤的兩個，於是「改了一整天
      的東西還沒生效」正好看不出來。
    * **多列／掛錯元件** → 它回傳的 `dep_rel` 是要顯示給人看的「就是這個檔案讓你
      陳舊」，指錯就是主動誤導。`batch` 當時列著 `_chrome_slot.py`，而
      `webrunner_novelai.py` 根本不 import 它——import 它的是 `start_webrunner.py`，
      也就是那一筆同時做到了誤報批次與漏報批次監督者。
    """
    root = Path(__file__).resolve().parent.parent
    known = _project_modules(root)
    # 正面對照組：解析器壞掉時每個閉包都只剩進入點自己，而那會讓「多列」的斷言
    # 炸成一堆雜訊、「少列」則永遠通過。先確認它真的走得動。
    bot_closure = _import_closure(root, "discord_bot", known)
    assert len(bot_closure) > 10, (
        f"bot 的 import 閉包只算出 {sorted(bot_closure)}，解析器多半壞了。")

    for label, (script, deps) in pc.STALE_COMPONENTS.items():
        entry = Path(script).stem
        expected = {known[m] for m in _import_closure(root, entry, known)}
        listed = set(deps)
        assert not (expected - listed), (
            f"`{label}` 會載入這些檔案但對照表沒列："
            f"{sorted(expected - listed)}。改了它們必須重啟才生效，"
            "漏列等於安靜地漏報陳舊。")
        assert not (listed - expected), (
            f"`{label}` 的對照表列了它其實不會載入的檔案："
            f"{sorted(listed - expected)}。這比漏列更糟——"
            "`find_stale_components` 會指著這個檔名說「就是它讓你陳舊」。")


def test_every_entry_point_in_the_map_is_a_real_script():
    root = Path(__file__).resolve().parent.parent
    for label, (script, _deps) in pc.STALE_COMPONENTS.items():
        hits = list(root.rglob(script))
        assert hits, f"{label} 的進入點 {script} 在 repo 裡找不到"


def _doctor_func():
    tree = ast.parse((Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py")
                     .read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "cmd_doctor")


def _names_reached_by(func) -> set[str]:
    """`func` 裡「被呼叫」或「被當成引數交出去」的名字。

    第二條規則 2026-09-20 從 `args[0]` 放寬到**每一個**引數（含關鍵字引數）。原本
    只看第一個引數，而 `cmd_doctor` 裡每一個 `_doctor_probe(label, probe)` 都把探測
    放在**第二個**——於是把一個檢查改寫成走 `_doctor_probe`（那正是這支函式的正規
    寫法）就會讓守門變紅，而訊息寫的是「`/sys doctor` 沒有回報……」，也就是對著正確
    的程式碼指控一件沒發生的事。這種守門最後會被人放寬或關掉，不會被修對
    ——`CLAUDE.md` 自己的判語是「a guard that cries wolf is a guard someone switches
    off」。

    用 AST 而不是字串比對：這兩支測試上面的註解就提到那些函式名。
    """
    names = set()
    for call in ast.walk(func):
        if not isinstance(call, ast.Call):
            continue
        if isinstance(call.func, ast.Name):
            names.add(call.func.id)
        for arg in (*call.args, *(kw.value for kw in call.keywords)):
            if isinstance(arg, ast.Name):
                names.add(arg.id)
    return names


@pytest.mark.parametrize("source, reached", [
    ("def f():\n    probe(thing)", {"probe", "thing"}),
    # 這一筆是 2026-09-20 的盲點：探測排在第二個引數。
    ("def f():\n    probe('label', thing)", {"probe", "thing"}),
    ("def f():\n    probe('label', probe=thing)", {"probe", "thing"}),
    # 包一層 lambda 仍然看得到，因為裡面那個是真的呼叫。
    ("def f():\n    probe('label', lambda: thing())", {"probe", "thing"}),
    # 屬性取用不算——`_gui.job_list` 不是這個檔在追蹤的頂層名字。
    ("def f():\n    probe('label', gui.thing)", {"probe"}),
])
def test_the_reached_names_extractor_sees_an_argument_in_any_position(
        source, reached):
    """抽取器自己的對照。

    沒有這一支的話，放寬與不放寬在**目前這棵樹**上結果一樣（兩個名字剛好都各有
    一個直接呼叫看得到），於是改對改錯都全綠——空的選集跟乾淨的結果長得一模一樣。
    最後一筆是必須**排除**的那一面：只有必須擋的語料，放寬那一步會活下來。
    """
    func = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.FunctionDef))
    assert _names_reached_by(func) == reached


def test_the_doctor_actually_runs_the_check():
    """功能寫好卻沒有人呼叫，就只是一段沒人跑的程式碼——而這一支功能的全部意義
    正是「有人會看到」。"""
    assert "find_stale_components" in _names_reached_by(_doctor_func()), (
        "`/sys doctor` 沒有跑「跑著的是不是磁碟上那一份」的檢查")


def test_the_doctor_hides_file_names_from_non_owners():
    """檔名要走 `_owner_detail` 的 raw 分支。泛用分支只准講數量。

    Layer 1 硬規則：專案相對路徑（`axiomatic/...`）不得送給非擁有者。
    """
    tree = ast.parse((Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py")
                     .read_text(encoding="utf-8"))
    doctor = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "cmd_doctor")
    owner_calls = [c for c in ast.walk(doctor) if isinstance(c, ast.Call)
                   and isinstance(c.func, ast.Name)
                   and c.func.id == "_owner_detail"]
    assert owner_calls, "doctor 沒有任何 `_owner_detail`——細節沒有被分流"
    for call in owner_calls:
        generic = call.args[2] if len(call.args) > 2 else None
        for node in ast.walk(generic) if generic is not None else []:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "axiomatic/" not in node.value and ".py" not in node.value, (
                    f"泛用分支帶了檔名：{node.value!r}")


# ---------------------------------------------------------------------------
# 「主機當掉之後，這一套自己回得來嗎」
#
# 2026-09-05 補。`install_autostart.py` 把兩支監督者註冊成**登入時**觸發，所以
# 「重開之後沒人拉起來」那一半解決了——但只在有人登入的前提下。這台機器每 2～5 天
# 一次自動重開；量過的那一次重開後停在鎖定畫面，整套
# 停了約 23 小時。沒有自動登入的話，這條鏈就是缺一角，而且完全看不出來。
#
# 不能改成開機觸發：批次要開一個有桌面的瀏覽器，session 0 起不來。真正補齊要在
# 系統層開自動登入，那會把帳號憑證放進主機的認證存放區——屬於主機安全設定，
# 不該由程式偷偷改掉。所以這裡只做**偵測**。
# ---------------------------------------------------------------------------


def test_a_complete_chain_reports_no_gap():
    # `expiry`／`auto_end_tasks` 一起注入：自動登入開著時兩支探測都會被問，不注入就是
    # 在讀這台機器真的登錄檔，這支的綠燈會變成「這台主機剛好設好了」。
    got = pc.autostart_recovery_status(
        tasks=("A", "B"), query=lambda n: True, autologon=True,
        expiry=None, auto_end_tasks=True)
    assert got["gap"] is None
    assert got["registered"] == ["A", "B"] and not got["missing"]


def test_an_unregistered_task_is_the_worst_gap():
    """沒註冊比「要人登入」嚴重：後者至少登入之後會恢復。"""
    got = pc.autostart_recovery_status(
        tasks=("A", "B"), query=lambda n: n == "A", autologon=False)
    assert got["gap"] == "not_registered"
    assert got["missing"] == ["B"]


def test_registered_but_no_autologon_is_reported():
    got = pc.autostart_recovery_status(
        tasks=("A",), query=lambda n: True, autologon=False)
    assert got["gap"] == "needs_logon", (
        "沒開自動登入時必須講出來——當機重開會停在鎖定畫面，"
        "而登入觸發的工作永遠不會發生。")


def test_an_undecidable_autologon_does_not_cry_wolf():
    """判斷不出來時保持安靜。會亂叫的診斷最後會被人忽略——`test_language` 的
    字表、`find_stale_components` 的 `<` 邊界都記過同一個教訓。"""
    got = pc.autostart_recovery_status(
        tasks=("A",), query=lambda n: True, autologon=None)
    assert got["gap"] is None


def test_none_means_undecidable_not_go_look_it_up():
    """**這一條是回歸測試**：`autologon` 原本用 `None` 兼作「沒指定」，於是測試
    想注入「判斷不出來」時會被當成「請你自己去查登錄檔」，注入形同無效
    （2026-09-05 實測踩到）。現在「沒指定」用獨立哨符。
    """
    import inspect
    sig = inspect.signature(pc.autostart_recovery_status)
    default = sig.parameters["autologon"].default
    assert default is not None, (
        "`autologon` 的預設值又變回 None 了——那會跟「判斷不出來」這個合法值撞在"
        "一起，讓注入失效。用獨立的哨符物件。")


def test_the_probe_never_raises_when_the_query_blows_up():
    def _boom(_name):
        raise OSError("schtasks missing")
    got = pc.autostart_recovery_status(
        tasks=("A",), query=_boom, autologon=True,
        expiry=None, auto_end_tasks=True)
    assert got["gap"] is None and got["unknown"] == ["A"]


def test_the_doctor_surfaces_the_recovery_gap():
    """偵測寫好卻沒人顯示，就只是一段沒人跑的程式碼。"""
    assert "autostart_recovery_status" in _names_reached_by(_doctor_func()), (
        "`/sys doctor` 沒有回報「主機當掉之後回不回得來」。")


def _fake_winreg(monkeypatch, outcome, **by_name):
    """讓 `_autologon_enabled` 讀到指定的結果，完全不碰真的登錄檔。

    `outcome` 是例外就從 `QueryValueEx` 丟出來，否則當成值回傳（`int` 走
    REG_DWORD，`str` 走 REG_SZ）。`by_name` 針對個別值名給不同結果
    （`AutoLogonCount=FileNotFoundError()`），沒列到的值名用 `outcome`——
    `_autologon_expiry` 一次讀兩個值，只給一個結果的話兩個值會讀到同一個東西，
    組不出「計數不存在＋原則已設」這種真實組合。函式裡是 `import winreg` 再取它的屬性，所以
    換掉模組屬性就夠；還原交給 `monkeypatch`——這裡動的是 **stdlib 模組本身**，
    自己寫 `try/finally` 漏掉一項就會污染後面每一支測試。
    """
    winreg = pytest.importorskip("winreg")      # 非 Windows 直接跳過
    monkeypatch.setattr(winreg, "OpenKey", lambda *a, **k: object())
    monkeypatch.setattr(winreg, "CloseKey", lambda handle: None)

    def _query(_handle, name):
        got = by_name.get(name, outcome)
        if isinstance(got, BaseException):
            raise got
        return (got,
                winreg.REG_DWORD if isinstance(got, int) else winreg.REG_SZ)

    monkeypatch.setattr(winreg, "QueryValueEx", _query)
    return winreg


@pytest.mark.parametrize("outcome, expected", [
    ("1", True),
    (1, True),                                  # REG_DWORD：`str()` 就是為了這一格
    ("0", False),
    (0, False),
    ("1 ", False),                              # 帶空白：刻意**不**容忍，見 docstring
    (" 1", False),
    ("true", False),                            # 文件載的值是 `"1"`，不是布林字面
    (FileNotFoundError("value missing"), False),  # 值不存在 ＝ 確定沒開
    (PermissionError("access denied"), None),     # 讀不到 ＝ 判斷不出來
    (OSError("registry unavailable"), None),
])
def test_the_autologon_probe_keeps_its_three_answers(monkeypatch, outcome, expected):
    """True／False／None 三個答案都要分得開，而且 `except` 的**順序**是承重的。

    `FileNotFoundError` 是 `OSError` 的子類別，所以那兩個 `except` 一對調，
    `return False` 立刻變成死碼——「值不存在」會被讀成「判斷不出來」，於是
    `/sys doctor` 那句「這台主機沒有開自動登入」**安靜地消失**。而那正是這台機器
    現在的狀態（實測 `AutoAdminLogon` 根本不存在），也就是說唯一一個警告正確的
    情形剛好是會被弄壞的那一個。同一個形狀在 `start_webrunner._live_webrunner_pid`
    記過一次。

    `(1, True)` 那一格釘的是 `str()`：REG_DWORD 的值拿到的是 `int`，少了 `str()`
    就是 `raw.strip()` → `AttributeError` → 被最外層的 broad except 吃掉 → 回
    `None`，一個「開著」的主機被讀成「不知道」。

    **`"1 "` / `" 1"` / `"true"` 三格釘的是一個決定，不是一個猜測（2026-09-12 改）。**
    在此之前那一行是 `str(raw).strip() in ("1", "true", "True")`，而這份 docstring
    寫著刻意不釘 `"1 "`，理由是「Winlogon 吃不吃帶空白的值不重開機驗不出來，釘一個
    猜測比沒有覆蓋更糟」。那個理由對，結論不對——**要決定的從來不是 Winlogon 怎麼
    讀，而是這支探測該往哪一側犯錯。** 寬鬆的那一側讓 `/sys doctor` 那句警告安靜
    消失，而那句警告是這支函式存在的唯一理由；犯錯成本差三個數量級（多印一行 NOTE
    vs. 一台停在鎖定畫面、2026-09-02 實測停了約 23 小時的主機）。所以改成精確比對，
    而這三格釘的是**我們自己的**取捨，不是對 Winlogon 的斷言——真有人拿到反向證據
    （例如帶空白的值實際上登得進去），改回去時要連這段一起改。

    順帶收掉 `"true"` / `"True"`：它們跟 `.strip()` 同一個形狀（沒有依據的寬鬆），
    而且從來沒有任何一格測試碰過。
    """
    _fake_winreg(monkeypatch, outcome)
    assert pc._autologon_enabled() is expected, (
        f"{outcome!r} 的判定變了。三個答案分不開的話，`/sys doctor` 要嘛在該講話"
        "的時候沉默，要嘛在判斷不出來的時候亂叫。")


@pytest.mark.parametrize("outcome, gap", [
    ("1", None),
    (FileNotFoundError("value missing"), "needs_logon"),
    (PermissionError("access denied"), None),
])
def test_only_a_definite_no_reaches_the_doctor(monkeypatch, outcome, gap):
    """把真的探測接到 `gap` 上跑一次——注入以外的那條路也要對。

    既有那幾支都用 `autologon=` 注入，所以 `_autologon_enabled` 在此之前**一行
    都沒被跑過**，連「有沒有被接起來」都沒人問。這支刻意**不注入**，走預設值那條
    路，所以探測與判定之間的接線也在覆蓋範圍內。

    `gap` 的語意是「要不要對人講話」：只有**確定沒開**才講，判斷不出來一律沉默
    （會亂叫的診斷最後會被人忽略）。
    """
    # 另外三個值固定成「沒問題」，這支只量 `AutoAdminLogon` 那一軸；少了前兩個，
    # `"1"` 那一格會讀到 `AutoLogonCount="1"` 而被報成 `autologon_expires`。
    # `AutoEndTasks` 寫明而不是靠 `outcome` 碰巧是 `"1"`：那個巧合一改就會把
    # `"1"` 那一格翻成 `restart_vetoable`。
    _fake_winreg(monkeypatch, outcome,
                 AutoLogonCount=FileNotFoundError("absent"),
                 DisableAutomaticRestartSignOn=1,
                 AutoEndTasks="1")
    got = pc.autostart_recovery_status(tasks=("A",), query=lambda _n: True)
    assert got["gap"] == gap


# ---------------------------------------------------------------------------
# 自動登入「開著」≠「下一次重開還開著」（2026-09-19）
#
# Windows Update 重開時的 ARSO 會自己設 `AutoLogonCount`；歸零時 Winlogon 把
# `AutoAdminLogon` 改成 0、刪掉 `DefaultPassword`，連擁有者自己設的一起清。這台
# 主機的事件記錄有 8 筆 Id 5013，全部落在更新重開的第二次開機。只看
# `AutoAdminLogon` 的探測會一路回報「鏈路完整」，直到那一次更新之後。
# ---------------------------------------------------------------------------

_ABSENT = FileNotFoundError("absent")


@pytest.mark.parametrize("count, policy, expected", [
    (_ABSENT, 1, None),                          # 這台主機 2026-09-19 之後的狀態
    (_ABSENT, _ABSENT, "update_restart"),        # 原則沒設 ＝ ARSO 預設開
    (_ABSENT, 0, "update_restart"),
    (_ABSENT, "1", "update_restart"),            # 字串型不算：文件載明是 DWORD
    (3, 1, "count_limited"),
    (0, 1, "count_limited"),                     # 0 也是：下一次開機就清
    (0, _ABSENT, "count_limited"),               # 兩個都壞時報較近的那個
    (PermissionError("denied"), 1, None),        # 讀不到 ＝ 判斷不出來
    (_ABSENT, PermissionError("denied"), None),
    (OSError("registry unavailable"), _ABSENT, None),
])
def test_the_expiry_probe_keeps_its_answers(monkeypatch, count, policy, expected):
    """三種答案要分得開，而且 `except` 的順序與「值不存在」的意思都是承重的。

    `FileNotFoundError` 在兩個值上的意思**相反**：`AutoLogonCount` 不存在是好消息
    （沒有次數上限），原則值不存在是壞消息（ARSO 預設開）。把兩個 `except` 寫成
    同一個形狀，其中一邊就會反過來。`(_ABSENT, _ABSENT)` 那一格正是這台主機
    2026-09-19 之前的真實狀態，也是唯一一個警告正確的情形——與
    `test_the_autologon_probe_keeps_its_three_answers` 記過的是同一個形狀。
    """
    _fake_winreg(monkeypatch, "1", AutoLogonCount=count,
                 DisableAutomaticRestartSignOn=policy)
    assert pc._autologon_expiry() == expected, (
        f"(AutoLogonCount={count!r}, DisableAutomaticRestartSignOn={policy!r}) "
        "的判定變了。")


def test_an_expiring_autologon_is_reported():
    got = pc.autostart_recovery_status(
        tasks=("A",), query=lambda _n: True, autologon=True,
        expiry="update_restart", auto_end_tasks=True)
    assert got["gap"] == "autologon_expires", (
        "自動登入開著、但下一次更新重開就會被清掉——這要講出來，否則 doctor 會一路"
        "回報鏈路完整，直到主機又停在鎖定畫面。")
    assert got["expiry"] == "update_restart"


@pytest.mark.parametrize("registered, autologon, gap", [
    (False, True, "not_registered"),
    (True, False, "needs_logon"),
])
def test_a_worse_gap_outranks_an_expiring_autologon(registered, autologon, gap):
    got = pc.autostart_recovery_status(
        tasks=("A",), query=lambda _n: registered, autologon=autologon,
        expiry="update_restart")
    assert got["gap"] == gap


@pytest.mark.parametrize("autologon", [False, None])
def test_expiry_is_only_asked_when_autologon_is_on(monkeypatch, autologon):
    """沒開或判斷不出來時 `expiry` 必須是 `None`。否則 doctor 會在根本沒開的主機上
    說「開著但會被清掉」，而判斷不出來時本來就該沉默。

    這裡刻意讓探測**會回壞消息**：它要是被問了，這支就紅。
    """
    monkeypatch.setattr(pc, "_autologon_expiry", lambda: "update_restart")
    got = pc.autostart_recovery_status(
        tasks=("A",), query=lambda _n: True, autologon=autologon)
    assert got["expiry"] is None and got["gap"] != "autologon_expires"


@pytest.mark.parametrize("policy, gap", [
    (1, None),
    (_ABSENT, "autologon_expires"),
])
def test_the_expiry_probe_is_wired_to_the_gap(monkeypatch, policy, gap):
    """不注入 `autologon`／`expiry`，走預設那條路——探測與判定之間的接線也要在
    覆蓋範圍內（`_autologon_enabled` 曾經一行都沒被跑過，見上面那支）。"""
    _fake_winreg(monkeypatch, "1", AutoLogonCount=_ABSENT,
                 DisableAutomaticRestartSignOn=policy, AutoEndTasks="1")
    got = pc.autostart_recovery_status(tasks=("A",), query=lambda _n: True)
    assert got["gap"] == gap


# ---------------------------------------------------------------------------
# 第三個缺口：擁有者自己按的重開被常駐程式否決（2026-09-19）
#
# 從開始功能表按重新啟動，兩個常駐程式擋下關機，60 秒後「重新啟動失敗」、回到鎖定
# 畫面——主機根本沒重開、自動登入沒機會跑，從外面看卻像「自動登入壞了」。已設目前
# 使用者的 `AutoEndTasks`（REG_SZ `"1"`）；這一段讓偵測端看得到那個值。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcome, expected", [
    ("1", True),
    (1, False),                                 # REG_DWORD：刻意**不**算，見 docstring
    ("0", False),
    ("1 ", False),                              # 帶空白：同 `_autologon_enabled` 的取捨
    ("", False),
    (FileNotFoundError("value missing"), False),  # 值不存在 ＝ 預設 0 ＝ 會被擋
    (PermissionError("access denied"), None),     # 讀不到 ＝ 判斷不出來
    (OSError("registry unavailable"), None),
])
def test_the_end_tasks_probe_keeps_its_three_answers(monkeypatch, outcome, expected):
    """True／False／None 三個答案要分得開；`except` 的順序與型別檢查都是承重的。

    * `FileNotFoundError` 是 `OSError` 的子類別：兩個 `except` 對調，「值不存在」就
      被讀成「判斷不出來」，於是 `/sys doctor` 那句提醒在**唯一該講的情形**安靜消失
      ——與 `test_the_autologon_probe_keeps_its_three_answers` 記過的是同一個形狀。
    * `(1, False)` 釘的是型別檢查：文件載明的型別是字串，REG_DWORD 的 1 關機時認不
      認不重開驗不出來，所以往「多印一行 NOTE」那側犯錯。拿掉 `kind == REG_SZ` 或改成
      `str(raw) == "1"`，這一格就翻過去。
    """
    _fake_winreg(monkeypatch, outcome)
    assert pc._auto_end_tasks_enabled() is expected, (
        f"{outcome!r} 的判定變了。")


def test_the_end_tasks_probe_reads_the_current_user_hive(monkeypatch):
    """`AutoEndTasks` 是**逐使用者**的值。其他幾支讀 HKLM，照抄的話會讀到一個根本
    不存在的位置，於是永遠回 False、永遠多印一行——一個永遠亂叫的提醒最後會被人忽略。
    """
    winreg = _fake_winreg(monkeypatch, "1")
    opened = []

    def _open(hive, path, *_a, **_k):
        opened.append((hive, path))
        return object()

    monkeypatch.setattr(winreg, "OpenKey", _open)
    assert pc._auto_end_tasks_enabled() is True
    assert opened == [(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop")], opened


def test_a_vetoable_restart_is_reported():
    """正面對照：鏈路其餘完整、自動登入開著、`AutoEndTasks` 確定沒開 ⇒ 要講出來。"""
    got = pc.autostart_recovery_status(
        tasks=("A",), query=lambda _n: True, autologon=True,
        expiry=None, auto_end_tasks=False)
    assert got["gap"] == "restart_vetoable", (
        "手動重開會被常駐程式擋下卻沒講——主機根本沒重開，症狀會長得像「自動登入壞了」。")
    assert got["auto_end_tasks"] is False


def test_an_undecidable_end_tasks_does_not_cry_wolf():
    got = pc.autostart_recovery_status(
        tasks=("A",), query=lambda _n: True, autologon=True,
        expiry=None, auto_end_tasks=None)
    assert got["gap"] is None


@pytest.mark.parametrize("registered, autologon, expiry, gap", [
    (False, True, None, "not_registered"),
    (True, False, None, "needs_logon"),
    (True, True, "update_restart", "autologon_expires"),
])
def test_every_heavier_gap_outranks_a_vetoable_restart(registered, autologon,
                                                       expiry, gap):
    """它是最輕的一角：其他任何一角缺了都先講那一角（doctor 一次只印一個 gap）。"""
    got = pc.autostart_recovery_status(
        tasks=("A",), query=lambda _n: registered, autologon=autologon,
        expiry=expiry, auto_end_tasks=False)
    assert got["gap"] == gap


@pytest.mark.parametrize("autologon", [False, None])
def test_end_tasks_is_only_asked_when_autologon_is_on(monkeypatch, autologon):
    """沒開自動登入時，手動重開成不成功都回不來，講這個只是雜訊；判斷不出來時本來
    就該沉默。探測刻意**會回壞消息**：它要是被問了，這支就紅。"""
    monkeypatch.setattr(pc, "_auto_end_tasks_enabled", lambda: False)
    got = pc.autostart_recovery_status(
        tasks=("A",), query=lambda _n: True, autologon=autologon, expiry=None)
    assert got["auto_end_tasks"] is None and got["gap"] != "restart_vetoable"


@pytest.mark.parametrize("value, gap", [
    ("1", None),
    (_ABSENT, "restart_vetoable"),
    (PermissionError("denied"), None),
])
def test_the_end_tasks_probe_is_wired_to_the_gap(monkeypatch, value, gap):
    """不注入，走預設那條路：探測與判定之間的接線也要在覆蓋範圍內。其餘三個值固定
    成「沒問題」，這支只量 `AutoEndTasks` 那一軸。"""
    _fake_winreg(monkeypatch, "1", AutoLogonCount=_ABSENT,
                 DisableAutomaticRestartSignOn=1, AutoEndTasks=value)
    got = pc.autostart_recovery_status(tasks=("A",), query=lambda _n: True)
    assert got["gap"] == gap


def _gap_keys_returned(fn) -> set[str]:
    """函式裡被指派給 `gap` 的每一個字串字面值。"""
    return {n.value.value for n in ast.walk(fn)
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "gap"
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)}


def _gap_keys_handled(fn) -> set[str]:
    """函式裡拿來跟 `<x>.get("gap")` 做 `==` 比對的每一個字串字面值。"""
    keys = set()
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Compare) and len(n.ops) == 1
                and isinstance(n.ops[0], ast.Eq)):
            continue
        left, right = n.left, n.comparators[0]
        if (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute)
                and left.func.attr == "get" and left.args
                and isinstance(left.args[0], ast.Constant)
                and left.args[0].value == "gap"
                and isinstance(right, ast.Constant)
                and isinstance(right.value, str)):
            keys.add(right.value)
    return keys


def test_every_recovery_gap_has_a_doctor_message():
    """探測會回的每一個 `gap` 鍵，`/sys doctor` 都要有對應的訊息，反之亦然。

    `gap` 是開放的字串集合，而 doctor 用 `if／elif` 一個一個比——新增一個鍵卻沒
    在那邊加分支，它就**安靜地什麼都不印**，從外面看跟「鏈路完整」一模一樣。
    2026-09-19 加 `autologon_expires` 時正是這個形狀：探測改完、它自己的測試全綠，
    而 doctor 一個字都不會多講。反方向是改名留下的死分支，與 `_OWNER_ONLY_SLASH`
    的過期條目同一個形狀。
    """
    here = Path(__file__).resolve().parent.parent / "axiomatic"
    probe = next(
        n for n in ast.walk(ast.parse(
            (here / "_process_control.py").read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef)
        and n.name == "autostart_recovery_status")
    doctor = next(
        n for n in ast.walk(ast.parse(
            (here / "discord_bot.py").read_text(encoding="utf-8")))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "cmd_doctor")
    returned, handled = _gap_keys_returned(probe), _gap_keys_handled(doctor)
    # 正控制：抽取本身壞掉時兩邊都是空集合，而空集合對空集合讀起來是「一致」。
    floor = {"not_registered", "needs_logon", "autologon_expires",
             "restart_vetoable"}
    assert floor <= returned, f"從探測抽到的鍵少了：{sorted(floor - returned)}"
    assert returned == handled, (
        f"探測會回、doctor 卻沒有訊息：{sorted(returned - handled)}；"
        f"doctor 有分支、探測卻不會回：{sorted(handled - returned)}。")


def test_the_gap_reconciliation_extractors_bite():
    """上面那支在真實語料上是綠的，所以兩個抽取器要另外有會咬人的語料：
    只認 `gap` 這個名字、只認字串、只認 `.get("gap")` 左邊的 `==`。"""
    probe = ast.parse(
        'def f():\n'
        '    gap = None\n'
        '    gap = "a"\n'
        '    other = "x"\n'
        '    gap = "b"\n').body[0]
    doctor = ast.parse(
        'async def cmd_doctor():\n'
        '    if r.get("gap") == "a":\n        pass\n'
        '    elif r.get("gap") == "c":\n        pass\n'
        '    elif r.get("other") == "b":\n        pass\n'
        '    elif r.get("gap") != "d":\n        pass\n').body[0]
    assert _gap_keys_returned(probe) == {"a", "b"}
    assert _gap_keys_handled(doctor) == {"a", "c"}


# ===========================================================================
# ctypes 退路：只有 psutil 真的不在時才會跑到，所以平常一行都沒被執行
#
# `requirements.txt` 把 psutil 列為必要相依，所以 `_nt_pid_alive` 幾乎永遠走第一
# 條路。既有的測試釘住了「優先用 psutil」與「psutil 自己爆掉時保守回 True」，但
# **退路本身**——28 行裡的 22 行——從來沒有被執行過。裡面有 `OpenProcess` 的旗標、
# `GetExitCodeProcess`、`STILL_ACTIVE`、`ERROR_ACCESS_DENIED` 的特判與 handle 的
# 釋放，任何一處打錯字都不會有人發現，直到某天 fresh clone 少裝了 psutil。
#
# 而那一天它壞掉的方式是**安靜的**：`except Exception: return True` 會把任何
# 失誤變成「一律活著」，於是 `_load_pid` 永遠不清 PID 檔、`_webrunner_alive()`
# 永遠說有在跑、supervisor 永遠不重啟。跟沒有這條退路的差別看不出來。
#
# 這裡用「拿 psutil 當對照組」的方式驗：同一個 pid 兩條路的答案必須一致。
# ===========================================================================

class _no_psutil:
    """讓函式內的 `import psutil` 丟 ImportError。

    `sys.modules[name] = None` 是 CPython 的既定行為：import 機制看到 None 就丟
    `ImportError`，不會再去找檔案。比裝一個 meta path finder 簡單，而且只影響
    這一個名字。
    """

    def __enter__(self):
        self._saved = sys.modules.get("psutil", "__absent__")
        sys.modules["psutil"] = None
        return self

    def __exit__(self, *exc):
        if self._saved == "__absent__":
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = self._saved
        return False


def _a_definitely_dead_pid() -> int:
    """起一個立刻結束的子行程，回它的 pid（已確認收屍完畢）。"""
    import subprocess
    # `subprocess.run` 回的是 `CompletedProcess`，**沒有 `.pid`**——要 pid 就得
    # 自己拿 `Popen`。`wait()` 之後子行程已經被收屍，pid 不再對應任何行程。
    proc = subprocess.Popen([sys.executable, "-c", "pass"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    proc.wait(timeout=30)
    return proc.pid


def test_the_import_block_really_blocks():
    """守門的自我檢查：擋不住的話下面每一支都會安靜地走回 psutil 那條路。"""
    with _no_psutil():
        with pytest.raises(ImportError):
            import psutil  # noqa: F401
    import psutil  # noqa: F401  # 出了 with 要恢復


@pytest.mark.skipif(os.name != "nt", reason="ctypes 退路是 Windows 專用的")
def test_the_ctypes_fallback_sees_a_live_process():
    with _no_psutil():
        assert pc._nt_pid_alive(os.getpid()) is True


@pytest.mark.skipif(os.name != "nt", reason="ctypes 退路是 Windows 專用的")
def test_the_ctypes_fallback_sees_a_dead_process():
    """這是**唯一**會回 False 的那條路。

    退路裡每一個 `except` 都回 True（保守），所以「回 True」證明不了它有在運作
    ——只有一個真正的 False 能證明 `OpenProcess` 開失敗、`GetLastError` 也不是
    ACCESS_DENIED 這條完整走通了。
    """
    import psutil
    dead = _a_definitely_dead_pid()
    if psutil.pid_exists(dead):
        pytest.skip("pid 被回收了，這次測不到")
    with _no_psutil():
        assert pc._nt_pid_alive(dead) is False


@pytest.mark.skipif(os.name != "nt", reason="ctypes 退路是 Windows 專用的")
def test_the_two_paths_agree():
    """退路的正確性拿 psutil 當對照組——兩條路對同一個 pid 必須給同一個答案。"""
    import psutil
    dead = _a_definitely_dead_pid()
    if psutil.pid_exists(dead):
        pytest.skip("pid 被回收了，這次測不到")
    for pid in (os.getpid(), dead):
        with _no_psutil():
            fallback = pc._nt_pid_alive(pid)
        assert fallback is pc._nt_pid_alive(pid) is psutil.pid_exists(pid), (
            f"pid={pid}：ctypes 退路與 psutil 不同調")


@pytest.mark.skipif(os.name != "nt", reason="ctypes 退路是 Windows 專用的")
def test_the_fallback_still_never_signals_anything(monkeypatch):
    """退路是**查詢**，不是操作。`os.kill` 一次都不准被叫到。"""
    called = []
    monkeypatch.setattr(os, "kill",
                        lambda *a, **k: called.append(a))
    with _no_psutil():
        pc._nt_pid_alive(os.getpid())
        pc._nt_pid_alive(_a_definitely_dead_pid())
    assert called == [], f"退路對 pid 送了訊號：{called}"


@pytest.mark.skipif(os.name != "nt", reason="ctypes 退路是 Windows 專用的")
def test_a_broken_ctypes_reads_as_alive(monkeypatch):
    """兩條路都不通時要保守回 True。

    這裡是「應該重啟嗎／應該讓位嗎」那一類問題的答案來源，猜錯的方向要選在
    「以為還活著」——那只是這次不動作，反過來會開出第二套 Chrome。
    """
    import ctypes
    monkeypatch.setattr(
        ctypes, "WinDLL",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no kernel32")))
    with _no_psutil():
        assert pc._nt_pid_alive(_a_definitely_dead_pid()) is True


# ---------------------------------------------------------------------------
# `taskkill` 的等待上限
#
# 三個呼叫端原本各自寫一次 `subprocess.run([...], capture_output=True,
# check=False)`，**都沒有 `timeout=`**，而 `_force_kill_pid` 的 Windows 分支連
# `try` 都沒有（它的 docstring 卻寫著「錯誤 swallow」——`check=False` 只吞非零結束
# 碼，吞不掉 spawn 階段的 `OSError`）。
#
# `taskkill` 不是永遠會回來的：目標行程卡在核心的不可中斷等待（典型是顯示驅動的
# I/O）時，`/F` 也得排隊。而長時間跑 Chrome 的機器本來就會吃 GPU。
# 呼叫端已經 `asyncio.to_thread(...)` 過，所以 heartbeat 不會死；死的是那條執行緒，
# 而 `_terminate_all_webrunner_instances` 在 await 它——`!stop` / `!restart` 永遠
# 不回覆，「重生前先清乾淨」也走不完。**安靜地卡住**，跟「現在沒事發生」分不出來。
#
# ⚠️ 最容易寫錯的一點：`subprocess.TimeoutExpired` **不是** `OSError`
# （`TimeoutExpired → SubprocessError → Exception`），所以只加 `timeout=` 而不擴大
# except，等於把「安靜地卡住」換成「例外往外炸」。下面兩個方向都釘。
# ---------------------------------------------------------------------------

class _RunSpy:
    """假的 `subprocess.run`：記下收到什麼，並照 `raises` 決定要不要拋。"""

    def __init__(self, raises=None):
        self.calls: list = []
        self._raises = raises

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(args, 0, b"", b"")


def test_taskkill_is_given_a_bounded_wait(monkeypatch):
    """實際傳出去的呼叫必須帶 `timeout=`，而且值要是有限的正數。

    正面對照組：先確認 spy 真的被呼叫到了。沒有這一句，`_run_taskkill` 變成
    no-op 也會讓下面的斷言空轉通過。
    """
    spy = _RunSpy()
    monkeypatch.setattr(subprocess, "run", spy)
    assert pc._run_taskkill(["taskkill", "/F", "/PID", "1234"]) is True
    assert len(spy.calls) == 1, f"根本沒跑到 subprocess.run：{spy.calls}"
    _args, kwargs = spy.calls[0]
    assert "timeout" in kwargs, "沒有給 taskkill 等待上限"
    assert 0 < kwargs["timeout"] < 600, kwargs["timeout"]
    assert kwargs["timeout"] == pc._TASKKILL_TIMEOUT_SEC


def test_a_wedged_taskkill_is_reported_not_raised(monkeypatch, capsys):
    """逾時要**回 False 並寫 stderr**，不得往外拋。

    `TimeoutExpired` 不是 `OSError`，所以呼叫端既有的 `except OSError` 接不到它。
    """
    spy = _RunSpy(raises=subprocess.TimeoutExpired(["taskkill"], 15))
    monkeypatch.setattr(subprocess, "run", spy)
    assert pc._run_taskkill(["taskkill", "/F", "/PID", "1234"]) is False
    assert "timed out" in capsys.readouterr().err


def test_a_missing_taskkill_is_reported_not_raised(monkeypatch, capsys):
    """spawn 階段的 `OSError`（PATH 上沒有 `taskkill`）同樣不得往外拋。

    這是 `_force_kill_pid` 原本真正的缺口：Windows 分支沒有 `try`，而 `check=False`
    對 spawn 失敗完全沒有作用。
    """
    spy = _RunSpy(raises=FileNotFoundError(2, "not found", "taskkill"))
    monkeypatch.setattr(subprocess, "run", spy)
    assert pc._run_taskkill(["taskkill", "/F", "/PID", "1234"]) is False
    err = capsys.readouterr().err
    assert "taskkill failed" in err
    # 洩漏規則：用 `!r` 而不是 `{error}`，所以不會把檔案路徑攤在字面上。
    assert "FileNotFoundError" in err


def test_timeout_expired_is_not_an_oserror():
    """把上面那條「為什麼要另外接」的前提釘住。

    這不是在測 stdlib 好玩：整個 `_run_taskkill` 的 except 形狀就是靠這個事實。
    哪天它變成 `OSError` 的子類，多接一個就只是多餘而不是必要——但**現在**少接它
    就是缺陷，而那個缺陷長得跟「已經處理好了」一模一樣。
    """
    assert not issubclass(subprocess.TimeoutExpired, OSError)
    assert issubclass(subprocess.TimeoutExpired, Exception)


@pytest.mark.skipif(os.name != "nt", reason="taskkill 是 Windows 專用的")
def test_one_wedged_process_does_not_abort_the_whole_chrome_sweep(monkeypatch):
    """一個卡住的行程不得讓整輪 sweep 停在那裡。

    這是真正的判準。`_kill_chrome_pids` 是掃到 40 個 chrome 行程就跑 40 次的迴圈；
    第 2 個卡住而讓例外逃出去的話，後面 38 個**一個都不會被殺**——而呼叫端接著就要
    開新的 Chrome，於是舊的那批繼續握著 profile。
    """
    seen: list[int] = []

    def _fake_run(args, **kwargs):
        pid = int(args[args.index("/PID") + 1])
        seen.append(pid)
        if pid == 222:
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 0))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    pc._kill_chrome_pids([(111, "chrome.exe"), (222, "chrome.exe"),
                          (333, "chrome.exe"), (444, "chromedriver.exe")])
    assert seen == [111, 222, 333, 444], (
        f"卡住的那一個把 sweep 帶走了，只跑到 {seen}")


class _TaskkillSite(NamedTuple):
    """一個 `taskkill` 的 argv 字面值被交出去的地方。

    `bounded` ＝ 這一行自己帶了 `timeout=`；`handlers` ＝ 包住它的每一個 `try`
    的 handler 型別名（聯集，裸 `except:` 記成 `Exception`）。兩個欄位缺一不可：
    只有 `timeout=` 而 handler 是 `except OSError` 的話，「安靜地卡住」只是換成
    「例外往外炸」——這條的理由是
    `subprocess.TimeoutExpired` 的 MRO 不經過 `OSError`。
    """
    module: str
    callee: str
    lineno: int
    bounded: bool
    handlers: frozenset


# 接得住 `subprocess.TimeoutExpired` 的 handler 名字。`OSError` **刻意不在**裡面：
# 那正是這條規則存在的理由。
_CATCHES_TIMEOUT = frozenset({
    "Exception", "BaseException",
    "TimeoutExpired", "subprocess.TimeoutExpired",
    "SubprocessError", "subprocess.SubprocessError",
})

# 掃描範圍：全專案的**非測試**模組，含 repo root。
#
# 2026-09-10 之前只掃 `_process_control.py` 一個檔案。而這條規則的文字
# （「`taskkill` 不保證會回來 … `TimeoutExpired` 不是 `OSError`」）跟模組完全無關
# ——§8.9 開頭那句「這一整套（bot ＋ 產圖批次）一次跑好幾天」講的就是兩邊。實測：
# 全專案 8 個 taskkill 呼叫點，**5 個在原本的範圍外**（兩個 webrunner 變體各自的
# `_kill_orphan_chrome`、`verify_browser` 的兩處）。五個今天都帶 `timeout=` 也都在
# `except Exception` 裡，所以性質成立——但沒有任何東西在檢查它，拿掉
# `webrunner_novelai.py` 那個 `timeout=20`，四千八百支測試不會有一支變紅。
#
# 測試檔排除：本檔與 `test_suite_safety.py` 裡的 taskkill argv 是刻意的替身／探針，
# 套真規則只會讓人把整支守門關掉。
_TASKKILL_SITE_FLOOR = 8        # 實測 8 筆（3 個受管 ＋ 5 個自帶上限）
_TASKKILL_MODULE_FLOOR = 3      # _process_control / webrunner_* / verify_browser


def _taskkill_scanned_sources(pkg_root=None, repo_root=None) -> tuple:
    """兩個目錄可以換掉，是為了讓範圍釘樁測得出「這是算出來的，不是今天剛好對」。"""
    package = PKG_ROOT if pkg_root is None else pkg_root
    root = package.parent if repo_root is None else repo_root
    out = []
    for path in sorted(package.glob("*.py")) + sorted(root.glob("*.py")):
        if path.name.startswith(("test_", "_test_")):
            continue
        if path.name in ("conftest.py", "__init__.py"):
            continue
        out.append(path)
    return tuple(out)


def _taskkill_sites_in_tree(tree, module: str) -> list[_TaskkillSite]:
    """純函式（只吃 AST ＋ 模組名），所以合成資料問得到它。"""
    def callee(node) -> str:
        f = node.func
        if isinstance(f, ast.Attribute):
            base = f.value.id if isinstance(f.value, ast.Name) else "?"
            return f"{base}.{f.attr}"
        return f.id if isinstance(f, ast.Name) else "?"

    def is_taskkill(node) -> bool:
        for arg in node.args:
            if not isinstance(arg, (ast.List, ast.Tuple)) or not arg.elts:
                continue
            head = arg.elts[0]
            if (isinstance(head, ast.Constant) and isinstance(head.value, str)
                    and head.value.lower().startswith("taskkill")):
                return True
        return False

    out: list[_TaskkillSite] = []

    def walk(node, chain):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and is_taskkill(child):
                out.append(_TaskkillSite(
                    module, callee(child), child.lineno,
                    any(kw.arg == "timeout" for kw in child.keywords),
                    frozenset().union(*chain) if chain else frozenset()))
            if isinstance(child, ast.Try):
                names = set()
                for handler in child.handlers:
                    if handler.type is None:
                        names.add("Exception")
                    elif isinstance(handler.type, ast.Tuple):
                        names |= {ast.unparse(e) for e in handler.type.elts}
                    else:
                        names.add(ast.unparse(handler.type))
                for stmt in child.body:
                    walk(stmt, chain + [names])
                for stmt in child.orelse + child.finalbody:
                    walk(stmt, chain)
                for handler in child.handlers:
                    for stmt in handler.body:
                        walk(stmt, chain)
            else:
                walk(child, chain)

    walk(tree, [])
    return sorted(out, key=lambda s: (s.module, s.lineno))


def _taskkill_offenders(sites) -> list[str]:
    """哪些呼叫點不合規。

    **抽成純函式是因為真實資料是乾淨的**：判準寫在測試函式裡的時候，把任何一邊
    的分支整個拿掉（`if False:`）都不會有測試變紅——變異實測，兩個分支各存活過
    一次。抽出來之後合成語料就餵得進去，兩條路各有自己的牙齒。
    """
    offenders: list[str] = []
    for site in sites:
        if site.module == "_process_control.py":
            # **手段是模組專屬的**：這裡有那扇唯一的門，就必須走它。
            if site.callee != "_run_taskkill":
                offenders.append(f"{site.module}:{site.lineno} "
                                 f"繞過 _run_taskkill（{site.callee}）")
            continue
        # 其餘模組進不了那扇門（`verify_browser` 的 docstring 明文要它自包含、
        # 不 import 任何 webrunner；兩個變體也不該去用別人的私有 helper），所以
        # **判準**照樣要成立：自己帶上限，而且外層接得住 `TimeoutExpired`。
        if not site.bounded:
            offenders.append(f"{site.module}:{site.lineno} 沒有 `timeout=`")
        elif not (site.handlers & _CATCHES_TIMEOUT):
            offenders.append(
                f"{site.module}:{site.lineno} 帶了 `timeout=` 但外層 handler "
                f"是 {sorted(site.handlers) or '（完全沒有 try）'}")
    return offenders


def _all_taskkill_sites(sources=None) -> list[_TaskkillSite]:
    out: list[_TaskkillSite] = []
    for path in (_taskkill_scanned_sources() if sources is None else sources):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except (SyntaxError, UnicodeDecodeError):   # pragma: no cover
            continue
        out.extend(_taskkill_sites_in_tree(tree, path.name))
    return out


def _taskkill_argv_sites(tree) -> list[tuple[str, int]]:
    """每一個「開頭是 `"taskkill"` 的 argv 字面值」被交給了誰 → `(被呼叫者, 行號)`。

    純函式（只吃 AST），所以合成資料問得到它。

    **判準刻意是「argv 字面值交給誰」，不是「`subprocess.run` 的引數提到 taskkill」。**
    後者是我第一版寫的，而它抽到**零筆**——因為 `_run_taskkill` 內部那一行傳的是
    參數 `args`，不是字面值。零筆會讓對帳空轉通過，是正面對照組當場抓到的
    （這正是它存在的理由）。改成現在這個判準之後，被授權的那一個呼叫端
    （`_run_taskkill(["taskkill", …])`）與任何繞過它的寫法都在同一個視野裡。
    """
    return sorted((s.callee, s.lineno)
                  for s in _taskkill_sites_in_tree(tree, "?"))


def test_every_taskkill_goes_through_the_one_bounded_runner():
    """靜態對帳：`taskkill` 的 argv 只能交給 `_run_taskkill`。

    這條是**結構性**的，不是列舉——下一個複製貼上的呼叫端會自動被抓到，不需要有人
    記得回來更新一份名單。本專案的既定教訓：同一條規則多份實作，修好的永遠只有其中
    一份（原本就是三個呼叫端各寫一次，其中 `_force_kill_pid` 連 `try` 都沒有）。
    """
    sites = _all_taskkill_sites()
    # 下限一：抽取器回空時，「沒有違規」與「全部合規」一模一樣。
    assert len(sites) >= _TASKKILL_SITE_FLOOR, (
        f"只抽到 {len(sites)} 個 taskkill 呼叫點（下限 "
        f"{_TASKKILL_SITE_FLOOR}）——抽取器壞了，或範圍被縮回去了")
    # 下限二：**只違反這一道**的語料是「筆數夠、但全擠在同一個模組」，所以它必須是
    # 獨立的一句（§8.8(A4)：一支控制測試只證得了它真的執行到的那一行）。
    modules = {s.module for s in sites}
    assert len(modules) >= _TASKKILL_MODULE_FLOOR, (
        f"taskkill 只出現在這 {len(modules)} 個模組：{sorted(modules)}（下限 "
        f"{_TASKKILL_MODULE_FLOOR}）——掃描範圍縮回單一檔案了")
    offenders = _taskkill_offenders(sites)
    assert not offenders, (
        f"這些 taskkill 呼叫沒有等待上限，或有上限卻接不住逾時：{offenders}。"
        "`taskkill` 不保證會回來（目標卡在核心的不可中斷等待時 `/F` 也要排隊），"
        "而 `subprocess.TimeoutExpired` 的 MRO 是 `SubprocessError → Exception`"
        "——**不經過 `OSError`**，所以只加 `timeout=` 而 handler 還是 "
        "`except OSError` 只是把「安靜地卡住」換成「例外往外炸」。"
        "`_process_control` 內部走 `_run_taskkill`；其他模組自己帶 `timeout=` "
        "＋ `except Exception`。")


def test_the_one_bounded_runner_really_passes_a_timeout():
    """`_run_taskkill` 自己那一行必須帶 `timeout=`。

    上面那支只保證「大家都走同一扇門」，保證不了「那扇門是鎖著的」。兩支合起來才
    是完整的性質。
    """
    tree = ast.parse((PKG_ROOT / "_process_control.py").read_text(
        encoding="utf-8"))
    runs = [n for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef) and fn.name == "_run_taskkill"
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run"]
    assert len(runs) == 1, f"`_run_taskkill` 裡有 {len(runs)} 個 run(...)"
    assert any(kw.arg == "timeout" for kw in runs[0].keywords), (
        "`_run_taskkill` 沒有把 `timeout=` 傳下去——那整支就只是一層包裝")


def test_the_taskkill_site_extractor_can_still_see_a_violation():
    """對照組：現況乾淨，所以上面那支把斷言刪掉也不會紅。牙齒在這裡。"""
    tree = ast.parse(
        "def bad(pid):\n"
        "    subprocess.run(['taskkill', '/F', '/PID', str(pid)])\n"
        "def sanctioned(pid):\n"
        "    _run_taskkill(['taskkill', '/F', '/PID', str(pid)])\n"
        "def unrelated():\n"
        "    subprocess.run(['schtasks', '/Query'])\n"
        "def indirect(args):\n"
        "    subprocess.run(args)\n")
    got = _taskkill_argv_sites(tree)
    assert got == [("_run_taskkill", 4), ("subprocess.run", 2)], got
    # 別的命令不得被誤抓，傳參數的那種也不算（那正是被授權的內層寫法）。
    assert all(ln in (2, 4) for _fn, ln in got), got


def test_the_taskkill_scan_is_a_glob_not_a_hardcoded_list(tmp_path):
    """釘住「範圍是算出來的」。

    那 5 個受管之外的呼叫點今天全都合規，所以加寬與不加寬在真實資料上分不出來
    （§8.8(A3)）。唯一分辨得出來的辦法是餵它一個**清單裡不可能有的名字**。
    """
    pkg = tmp_path / "pkg"
    root = tmp_path / "root"
    pkg.mkdir()
    root.mkdir()
    (pkg / "some_module.py").write_text("X = 1\n", encoding="utf-8")
    (root / "brand_new_reaper.py").write_text("X = 1\n", encoding="utf-8")
    (root / "test_not_a_product_module.py").write_text("", encoding="utf-8")

    names = {p.name for p in _taskkill_scanned_sources(pkg_root=pkg,
                                                      repo_root=root)}
    assert "brand_new_reaper.py" in names, (
        "repo root 新出現的腳本沒被算進來——那一半是寫死的清單，不是 glob。"
        "`run_batch.py` 與兩支監督者都住在那裡。")
    assert "some_module.py" in names, "套件那一半也要跟著換得掉"
    assert "test_not_a_product_module.py" not in names, (
        "測試檔進了掃描——本檔與 `test_suite_safety` 都有刻意的 taskkill 探針")


@pytest.mark.parametrize("label, fake, fragment", [
    # 只違反「筆數下限」：一個模組、一筆。
    ("sites",
     [_TaskkillSite("a.py", "_run_taskkill", 1, True, frozenset({"Exception"}))],
     "個 taskkill 呼叫點"),
    # 只違反「模組數下限」：筆數夠，但全擠在同一個模組。
    ("modules",
     [_TaskkillSite("a.py", "_run_taskkill", n, True, frozenset({"Exception"}))
      for n in range(1, _TASKKILL_SITE_FLOOR + 1)],
     "個模組"),
])
def test_each_taskkill_floor_fires_on_its_own(monkeypatch, label, fake,
                                              fragment):
    """§8.8(A4)：兩道下限**依序**排列，把語料換成空的只會讓第一道炸。

    所以每一道各給一份剛好只違反它的語料，而且斷言**是哪一句在叫**——不然把後面
    那道放寬成 0，控制測試照樣會被前面那道的 AssertionError 餵飽。
    """
    monkeypatch.setattr(sys.modules[__name__], "_all_taskkill_sites",
                        lambda sources=None: list(fake))
    with pytest.raises(AssertionError) as excinfo:
        test_every_taskkill_goes_through_the_one_bounded_runner()
    assert fragment in str(excinfo.value), (
        f"紅的不是 {label} 那一句，而是：{excinfo.value}")


def test_the_taskkill_site_extractor_records_boundedness_and_handlers():
    """新增的兩個欄位也要有牙齒。

    真實原始碼上完全看不出來：那 5 個範圍外的呼叫點今天都帶 `timeout=`、也都在
    `except Exception` 裡，所以把 `bounded` 寫死成 True、或把 handler 蒐集整段
    拿掉，主守門照樣全綠。
    """
    rich = ast.parse(
        "def unbounded():\n"
        "    try:\n"
        "        subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'])\n"
        "    except Exception:\n"
        "        pass\n"
        "def wrong_handler():\n"
        "    try:\n"
        "        subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'],\n"
        "                       timeout=20)\n"
        "    except OSError:\n"
        "        pass\n"
        "def naked(pid):\n"
        "    subprocess.run(['taskkill', '/F', '/PID', str(pid)], timeout=10)\n"
        "def compliant(pid):\n"
        "    try:\n"
        "        subprocess.run(['taskkill', '/F', '/PID', str(pid)],\n"
        "                       timeout=10)\n"
        "    except Exception:\n"
        "        pass\n")
    by_line = {s.lineno: s for s in _taskkill_sites_in_tree(rich, "probe.py")}
    assert set(by_line) == {3, 8, 13, 16}, sorted(by_line)
    assert by_line[3].bounded is False, "沒帶 timeout= 卻被記成有界"
    assert by_line[8].bounded is True and by_line[8].handlers == {"OSError"}, (
        f"handler 型別沒被記下來：{by_line[8]}")
    assert not (by_line[8].handlers & _CATCHES_TIMEOUT), (
        "`except OSError` 被算成接得住 TimeoutExpired——§8.9 的整個重點")
    assert by_line[13].handlers == frozenset(), "完全沒有 try 卻記到了 handler"
    assert by_line[16].handlers & _CATCHES_TIMEOUT, "合規的寫法被誤判"


def test_the_real_sweep_would_be_caught_if_it_lost_its_timeout(tmp_path):
    """把**磁碟上真的那一支**改壞一行，掃描必須紅。

    合成語料證明的是偵測邏輯；這一支證明的是「偵測邏輯真的對得上磁碟上那一行」。
    `webrunner_novelai._kill_orphan_chrome` 每個角色邊界都會跑一次，而它在
    2026-09-10 之前完全在這道守門的視野外。

    改的是**沙盒裡的副本**——那個檔案正被一個 56 小時的正式批次執行著。
    """
    real = PKG_ROOT / "webrunner_novelai.py"
    source = real.read_text(encoding="utf-8")
    anchor = "capture_output=True, timeout=20, check=False,"
    assert anchor in source, (
        f"`{anchor}` 不在 `webrunner_novelai.py` 裡了。寫法換了就把錨點一起換掉"
        "——別把這支測試刪掉，它是這道加寬唯一的端對端證明。")
    broken = tmp_path / "webrunner_novelai.py"
    broken.write_text(
        source.replace(anchor, "capture_output=True, check=False,"),
        encoding="utf-8")
    sites = _all_taskkill_sites(sources=(broken,))
    unbounded = [s for s in sites if not s.bounded]
    assert unbounded, (
        "把 `_kill_orphan_chrome` 的 `timeout=20` 拿掉，掃描竟然沒發現——"
        "範圍或判準已經對不上磁碟上那一行了。")


@pytest.mark.parametrize("label, site, offends", [
    ("受管模組繞過那扇門",
     _TaskkillSite("_process_control.py", "subprocess.run", 9, True,
                   frozenset({"Exception"})), True),
    ("受管模組走那扇門",
     _TaskkillSite("_process_control.py", "_run_taskkill", 9, False,
                   frozenset()), False),
    ("非受管模組沒有上限",
     _TaskkillSite("webrunner_novelai.py", "subprocess.run", 9, False,
                   frozenset({"Exception"})), True),
    ("非受管模組有上限但 handler 接不住逾時",
     _TaskkillSite("webrunner_novelai.py", "subprocess.run", 9, True,
                   frozenset({"OSError"})), True),
    ("非受管模組有上限但完全沒有 try",
     _TaskkillSite("verify_browser.py", "subprocess.run", 9, True,
                   frozenset()), True),
    ("非受管模組完全合規",
     _TaskkillSite("verify_browser.py", "subprocess.run", 9, True,
                   frozenset({"Exception"})), False),
    ("非受管模組用具名的逾時型別接",
     _TaskkillSite("webrunner_je_only.py", "subprocess.run", 9, True,
                   frozenset({"subprocess.TimeoutExpired"})), False),
])
def test_the_taskkill_classifier_sees_both_kinds_of_violation(label, site,
                                                              offends):
    """判準自己的 canary。

    真實 repo 上八個呼叫點全部合規，所以把 `if not site.bounded:` 或
    `if site.callee != "_run_taskkill":` 整個換成 `if False:`，主守門照樣全綠
    ——變異實測，兩個分支各存活過一次。這裡把**兩條路**都餵一次，正反都釘。
    """
    got = _taskkill_offenders([site])
    assert bool(got) is offends, f"「{label}」判斷錯了（回傳 {got}）"

# ------------------------------------------- 「(名單, 掃描完整嗎)」的宣告面 --
# 2026-09-07 把兩支掃描函式從「回名單」改成「回 `(名單, 掃描是否完整)`」，而且
# 已經有一支守門要求**呼叫端真的用到第二個值**。漏掉的是**宣告**那一面：
# `_find_all_chrome_processes` 的回傳型別註解從那天起一直寫著 `list[tuple[int, str]]`
# ——它的姊妹函式 `_find_all_webrunner_pids` 是對的，兩支在同一次改動裡分岔了。
# 註解不影響執行，所以這件事**完全沒有症狀**；它是讀者與型別檢查器唯一看得到的
# 合約，而那次改動的全部重點就是「第二個值不是裝飾品」。2026-09-20 由 `ty` 掃出來
# （`invalid-return-type`），同時也是它在這個檔案報的六筆假警報的來源。
#
# 這裡**不比對字串**，而是拿註解跟函式自己的 `return` 對帳：宣告要是二元組，而且
# 每一個帶值的 `return` 都要真的回二元組。這樣加一條「只回名單」的早退路徑也會紅。
_TWO_VALUE_SCANS = ("_find_all_chrome_processes", "_find_all_webrunner_pids")


@pytest.mark.parametrize("name", _TWO_VALUE_SCANS)
def test_the_scan_functions_declare_the_pair_they_actually_return(name):
    """宣告的回傳型別要跟每一個 `return` 的形狀對得起來（都是二元組）。"""
    tree = ast.parse((PKG_ROOT / "_process_control.py").read_text(encoding="utf-8"),
                     "_process_control.py")
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, f"找不到 {name}——這支的前提變了"

    ann = fn.returns
    assert isinstance(ann, ast.Subscript) and ast.unparse(ann.value) == "tuple", (
        f"{name} 宣告回傳 {ast.unparse(ann) if ann else None}，"
        "但它回的是 `(名單, 掃描是否完整)`")
    declared = ann.slice.elts if isinstance(ann.slice, ast.Tuple) else [ann.slice]
    assert len(declared) == 2, f"{name} 宣告了 {len(declared)} 個值，合約是 2 個"

    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value]
    assert len(returns) >= 2, (
        f"{name} 只找到 {len(returns)} 個帶值的 return——抽取器壞了，"
        "不是這支函式變乾淨了")
    for node in returns:
        assert isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2, (
            f"{name} 第 {node.lineno} 行回的是 `{ast.unparse(node.value)}`，"
            "不是 `(名單, 掃描是否完整)`——呼叫端拆包會當場爆掉，"
            "或更糟：剛好拆成兩個元素而靜靜地拿到錯的值")


def test_the_chrome_release_wait_is_still_a_real_wait_in_production():
    """正式的等待值必須 >= 1 秒，而且要從**原始碼**讀而不是從模組讀。

    上面那支 autouse 夾具把模組屬性設成 0，所以「從 `pc` 讀」的版本會量到夾具、
    永遠通過——那正是「為了讓測試快一點，順手把正式值也改成 0」不會被抓到的走法。
    所以這裡解析檔案裡的字面值，並且順便斷言此刻模組上的值**確實**是夾具設的 0，
    證明這支真的沒有在讀模組。

    第三個斷言管的是**範圍**：那一行 `sleep` 要真的引用這個常數。把它改回
    `sleep(2)` 的話，常數會變成沒人用的裝飾品、夾具安靜地失效，而測試只是慢 22 秒
    ——沒有任何東西會紅。
    """
    source = (PKG_ROOT / "_process_control.py").read_text(encoding="utf-8")
    tree = ast.parse(source, "_process_control.py")
    assigned = [node.value for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name)
                        and t.id == "_CHROME_RELEASE_WAIT_SEC"
                        for t in node.targets)]
    assert len(assigned) == 1, (
        f"`_CHROME_RELEASE_WAIT_SEC` 在模組層被賦值 {len(assigned)} 次——"
        "抽取器壞了，或者有人加了第二個定義")
    literal = assigned[0]
    assert isinstance(literal, ast.Constant) and isinstance(
        literal.value, (int, float)) and not isinstance(literal.value, bool), (
        f"正式值寫成 `{ast.unparse(literal)}`，這支只認得數字字面值")
    assert literal.value >= 1, (
        f"正式的 chrome 釋放等待是 {literal.value} 秒。這段等待是給作業系統真的放掉\n"
        "profile 的檔案握柄用的，太短的話下一次 selenium 鎖不到 profile，而症狀是\n"
        "「瀏覽器起不來」這種很難回推的東西。測試要快就改夾具，不要改這裡。")

    used = [node for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and node.id == "_CHROME_RELEASE_WAIT_SEC"
            and isinstance(node.ctx, ast.Load)]
    assert used, (
        "沒有任何地方**讀**這個常數——那一行 `sleep` 大概被改回寫死的秒數了，"
        "於是夾具安靜地失效、每支測試又開始真的睡。")

    assert pc._CHROME_RELEASE_WAIT_SEC == 0, (
        "夾具沒有生效（模組上的值不是 0），那表示這支測試量到的可能是模組而不是"
        "原始碼——上面那三個斷言的意義就沒了。")
