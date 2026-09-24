"""Chrome 留下殘骸之後的**復原鏈**——兩個變體各一份、而且從來沒有被執行過。

判準不是「這段程式多大」，是「它什麼時候才會第一次執行」。這一批全部只在
「Chrome 已經崩了／檔案已經被鎖住／上一次跑留下孤兒行程」之後才跑，也就是說
**它們第一次執行的那天，正是最不能出錯的那天**。2026-09-07 量到的覆蓋率是
一行都沒有：

    _kill_orphan_chrome / _kill_chrome_holders_of / _diagnose_file_lock /
    _force_unlink / _snapshot_chrome_profile / _sync_chrome_profile_back

而且它們是兩個變體**各一份**。CLAUDE.md 要求兩邊同步，但兩份副本都沒被測過，
等於「它們有沒有分歧」也從來沒有人問過。

**2026-09-07 更新：上面六支裡有三支已經不在了。** 補上測試之後才看得清楚，
`_cleanup_chrome_locks` → `_force_unlink` → `_kill_chrome_holders_of` →
`_diagnose_file_lock` 這一整條**沒有任何呼叫端**，而且清的是 `.chrome_profile/`
——Chrome 開的是 `.chrome_profile_snap/`，所以它就算被接上去也等於沒接。已連同
測試一起移除，換成一支對著正確目錄、真的有呼叫端的 `_clear_snapshot_locks`。
「寫好、測好、卻沒人呼叫」比沒有這段程式碼更糟：讀的人會以為檔案鎖是有處理的。

## 這支測試絕對不能碰到真的行程

被測的函式會真的終止行程並執行 taskkill，而這台機器上通常有一個跑了好幾天的
正式批次、Chrome 正開著。所以本檔的 autouse 夾具把兩個危險介面都**預設換成
安全替身**：

* `subprocess.run` → 直接 `AssertionError`。要用的測試自己換成記錄用的替身，
  於是「不小心接到真的」是紅燈，不是災難。
* `sys.modules["psutil"]` → `process_iter` 回空清單的假模組。忘記自己裝假的
  測試會掃到零個行程（無害）然後斷言失敗，不會掃到真的 chrome。

失效方向是刻意選的：**寧可測試紅，不可以真的殺到東西。**
"""
from __future__ import annotations

import ast
import os
import subprocess  # nosec B404 — 只用來替換 .run，從不真的執行
import pathlib
import sys
import types
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

import webrunner_je_only  # noqa: E402
import webrunner_novelai  # noqa: E402

_VARIANTS = (webrunner_novelai, webrunner_je_only)
_VARIANT_IDS = ("novelai", "je_only")


# ---------- 安全替身 ---------------------------------------------------------

class _FakeClock:
    """只換掉變體模組裡的 `time` 這個**名字**，stdlib 完全不動（conftest 會在
    每一支測試之後比對身分，換掉沒還原當場現形）。"""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def time(self) -> float:
        return 1_700_000_000.0

    def monotonic(self) -> float:
        return 0.0


class _FakeProc:
    """`process_iter` 掃到的行程替身。

    刻意**不再**提供 `open_files()`：唯一用過它的是那條已移除的檔案鎖復原鏈
    （見下方「snapshot 目錄裡的 lock 檔」那一節）。留著一個沒有被測程式碼會呼叫
    的方法，只會讓下一個人以為那條路還在——而 `psutil.Process.open_files()` 在
    這台機器上本來就有 330 個行程裡 141 個丟 `RuntimeError` 的問題。
    """

    def __init__(self, pid: int, name: str, killer=None):
        self.info = {"pid": pid, "name": name}
        self._killer = killer

    def kill(self):
        if self._killer is not None:
            self._killer(self.info["pid"], self.info["name"])


def _fake_psutil(procs=(), *, process_iter_error=None, killed=None):
    module = types.ModuleType("psutil")

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    module.NoSuchProcess = NoSuchProcess
    module.AccessDenied = AccessDenied

    def _record(pid, name):
        if killed is not None:
            killed.append((pid, name))

    if killed is not None:
        for proc in procs:
            proc._killer = _record

    def process_iter(attrs=None):
        if process_iter_error is not None:
            raise process_iter_error
        return list(procs)

    module.process_iter = process_iter
    return module


class _NoTaskkill:
    """預設的 `subprocess.run` 替身。"""

    def __call__(self, *args, **kwargs):
        raise AssertionError(
            "測試不得真的執行 taskkill／任何子行程。這台機器上有正式批次在跑，"
            f"而這一次呼叫是 {args!r}。要驗 taskkill 的測試請自己裝記錄用替身。")


# 這個檔案被 import 的當下、還沒有人動過手腳時的**真貨**。安全網的自我檢查拿它
# 當對照組：測試進行中，`sys.modules["psutil"]` 絕不可以是這一個。
_REAL_PSUTIL = sys.modules.get("psutil", "__absent__")
_REAL_SUBPROCESS_RUN = subprocess.run

_ABSENT = "__absent__"


def _restore_module(name, saved):
    """把 `sys.modules[name]` 還原成 `saved`；`_ABSENT` 才代表「本來就不在」。

    分開寫是為了讓那個哨兵有辦法被單獨測到——邏輯留在夾具裡的話，「用 None 當
    哨兵」這個錯誤在本檔的執行順序下剛好碰不到，改壞了也不會有人紅。
    """
    if saved is _ABSENT:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = saved


@pytest.fixture(autouse=True)
def _no_real_processes():
    """每一支測試都在「摸不到真行程」的世界裡跑，而且**跑完會自己驗一次**。

    還原用 `_ABSENT` 這個哨兵，不能用 `sys.modules.get(...)` 的 None：
    `.get()` 對「psutil 根本不在 sys.modules 裡」和「`sys.modules["psutil"]`
    **就是** None（那是「讓 import 失敗」的標準寫法，本檔與
    `test_process_control._no_psutil` 都在用）」回同一個答案。分不出來的話，
    第二種情況會被還原成 `pop`——而 `pop` 清掉的只是**快取**，下一個
    `import psutil` 會從磁碟載入真的那一個。那正是 2026-09-07 弄掉一個跑了
    78.7 小時的正式批次的機制，只是換成由這個夾具自己重新裝上去。
    """
    real_run = subprocess.run
    real_psutil = sys.modules.get("psutil", _ABSENT)
    fake = _fake_psutil()
    subprocess.run = _NoTaskkill()
    sys.modules["psutil"] = fake
    # 進場自檢：安全網真的裝上去了嗎？
    assert sys.modules["psutil"] is fake
    assert subprocess.run is not _REAL_SUBPROCESS_RUN
    try:
        yield
    finally:
        # 離場自檢：這一支測試有沒有在中途把真貨換回來？（在還原之前檢查，
        # 否則永遠是綠的。）
        during = sys.modules.get("psutil", _ABSENT)
        subprocess.run = real_run
        _restore_module("psutil", real_psutil)
        assert during is not _REAL_PSUTIL, (
            "這一支測試在執行期間把**真的** psutil 放回 sys.modules 了。"
            "接下來任何 `proc.kill()` 殺的都是這台機器上真正在跑的行程——"
            "2026-09-07 就是這樣弄掉一個連續執行 78.7 小時的正式批次。"
            '要模擬「套件不存在」請寫 `sys.modules["psutil"] = None`。')


@pytest.fixture
def taskkill_calls():
    """換上記錄用的 `subprocess.run`，回收集到的呼叫清單。"""
    calls: list[tuple[tuple, dict]] = []

    def _run(*args, **kwargs):
        calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    subprocess.run = _run
    return calls


@pytest.fixture
def fake_clock():
    """把變體模組的 `time` 換成假的；測試結束還原。"""
    installed: list[tuple[types.ModuleType, object]] = []

    def _install(module):
        clock = _FakeClock()
        installed.append((module, module.time))
        module.time = clock
        return clock

    yield _install
    for module, original in installed:
        module.time = original


@pytest.fixture(params=_VARIANTS, ids=_VARIANT_IDS)
def variant(request):
    return request.param


# ---------- 誰會被殺：nuclear sweep 的安全邊界 -------------------------------

def _sweep(variant, fake_clock, procs=(), **kwargs):
    killed: list[tuple[int, str]] = []
    sys.modules["psutil"] = _fake_psutil(procs, killed=killed, **kwargs)
    fake_clock(variant)
    return variant._kill_orphan_chrome(), killed


def test_the_orphan_sweep_spares_msedge_and_every_other_program(
        variant, fake_clock, taskkill_calls):
    """擁有者的日常瀏覽器**必須**活下來。

    這條規則原本只寫在 docstring 裡（"msedge.exe is deliberately spared (the
    owner's daily browser)"）——**一個只存在於註解裡的安全條件等於沒有**。掃描
    是無差別終止，多殺一個就是把使用者正在用的東西關掉。
    """
    procs = [
        _FakeProc(101, "chrome.exe"),
        _FakeProc(102, "chromedriver.exe"),
        _FakeProc(103, "msedge.exe"),
        _FakeProc(104, "msedgewebview2.exe"),
        _FakeProc(105, "msedgedriver.exe"),
        _FakeProc(106, "python.exe"),
        _FakeProc(107, "Code.exe"),
        _FakeProc(108, "explorer.exe"),
    ]
    count, killed = _sweep(variant, fake_clock, procs)
    assert sorted(name for _pid, name in killed) == [
        "chrome.exe", "chromedriver.exe"], (
        f"{variant.__name__}._kill_orphan_chrome 殺了不該殺的東西：{killed}。"
        "這支掃描是無差別的，白名單只有 chrome.exe / chromedriver.exe。")
    assert count == 2
    # taskkill 後援也只能點名這兩個 image。
    images = [args[0][-1] for args, _kw in taskkill_calls]
    assert sorted(images) == ["chrome.exe", "chromedriver.exe"], (
        f"taskkill 點名了 {images}；/IM 是精確比對 image 名稱，多一個就是"
        "把使用者的瀏覽器整棵樹殺掉。")


def test_the_orphan_sweep_never_kills_the_process_it_runs_in(
        variant, fake_clock, taskkill_calls):
    """自己的 pid 一定要跳過。

    今天這道檢查在正式環境是**防禦性**的（我們是 python 行程，本來就過不了名稱
    白名單），但它排在名稱檢查**之前**，所以它守的是「將來有人放寬名稱白名單」
    那一天。用一個名字剛好是 chrome.exe、pid 剛好是自己的行程把它釘住。
    """
    procs = [
        _FakeProc(os.getpid(), "chrome.exe"),
        _FakeProc(os.getpid() + 1, "chrome.exe"),
    ]
    _count, killed = _sweep(variant, fake_clock, procs)
    assert [pid for pid, _name in killed] == [os.getpid() + 1], (
        f"{variant.__name__}._kill_orphan_chrome 把自己也殺了：{killed}")


def test_the_taskkill_fallback_keeps_its_force_and_tree_flags(
        variant, fake_clock, taskkill_calls):
    """整棵樹那個旗標掉了不會有任何症狀——直到下一個 Chrome 又 OOM。

    docstring 解釋得很清楚：沒有 /T，被重新掛養的 renderer 會活過父行程、繼續
    佔著記憶體，於是下一個 Chrome 照樣 "Aw, Snap! Out of Memory"。那正是這支
    掃描存在的理由，而旗標掉了原本一支測試都不會紅。
    """
    _sweep(variant, fake_clock, [_FakeProc(101, "chrome.exe")])
    assert taskkill_calls, "taskkill 後援沒有被呼叫"
    for args, _kwargs in taskkill_calls:
        argv = list(args[0])
        assert argv[0] == "taskkill"
        assert "/F" in argv, f"少了 /F：{argv}"
        assert "/T" in argv, (
            f"少了 /T：{argv}。沒有它，被重新掛養的 renderer 會活下來，"
            "下一個 Chrome 再度 OOM——這支掃描就白做了。")
        assert "/IM" in argv, f"少了 /IM：{argv}"


def test_the_taskkill_fallback_never_decodes_console_output(
        variant, fake_clock, taskkill_calls):
    """taskkill 是 Windows 主控台工具，輸出走 **OEM 代碼頁**（本機 cp950）。

    現在的呼叫沒有任何文字模式旗標，所以拿到的是位元組、根本不解碼——正確。
    危險的是**將來**有人為了讀錯誤訊息順手加 `text=True`：`test_text_encoding`
    會要求指名編碼，而最順手的 utf-8 在這裡是錯的，它不會報錯，只會安靜地產生
    替換字元。本專案這個月才因為同一件事踩過一次（`schtasks`），所以這裡直接把
    方向釘死：要嘛不解碼，要嘛用 oem。
    """
    _sweep(variant, fake_clock, [_FakeProc(101, "chrome.exe")])
    assert taskkill_calls
    for _args, kwargs in taskkill_calls:
        if kwargs.get("text") or kwargs.get("universal_newlines"):
            assert "oem" in (kwargs.get("encoding") or ""), (
                f"taskkill 開了文字模式卻不是 OEM 編碼：{kwargs}。"
                "Windows 主控台工具的輸出是 OEM 代碼頁，用 utf-8 解會安靜地"
                "產生替換字元（見 _process_control._force_kill_pid 旁邊那條）。")
        else:
            assert "encoding" not in kwargs, (
                f"指定了 encoding 卻沒開文字模式：{kwargs}——那個參數不會有作用。")


# ---------- 後援的觸發條件 ---------------------------------------------------

def test_a_clean_psutil_sweep_skips_the_taskkill_fallback(variant, fake_clock):
    """psutil 掃完真的一個都不剩 → 不必再花一次 taskkill。

    （autouse 夾具的 `subprocess.run` 會直接 raise，所以「有跑到」就是紅燈。）
    """
    count, killed = _sweep(variant, fake_clock, [_FakeProc(101, "msedge.exe")])
    assert count == 0 and killed == []


def test_psutil_missing_falls_through_to_taskkill(
        variant, fake_clock, taskkill_calls):
    """沒有 psutil 這個套件 → 完全靠 taskkill。

    **模擬「套件不存在」要用 `sys.modules["psutil"] = None`，不是 `pop()`。**
    `pop()` 只是把**快取**拿掉；`import psutil` 接著會從磁碟**重新匯入真的那一
    個**，於是這支測試會拿真的 `psutil.process_iter()` 去跑 `proc.kill()`——
    也就是把這台機器上正在跑的 Chrome 全部殺掉。

    2026-09-07 這件事真的發生了：跑一次完整套件之後，一個已經連續執行 **78.7
    小時**的正式批次在 03:02:27 以
    `[WinError 10061] 目標電腦拒絕連線` 崩潰（chromedriver 的 HTTP 端點沒了），
    監督者五秒後重啟。**而測試看起來只是「斷言失敗」**：真的 psutil 把 chrome
    殺光 → 重數存活數得到 0 → `remaining != 0` 為 False → taskkill 整段跳過 →
    `len(taskkill_calls) == 0`。一個「無害的紅燈」底下是一次真正的破壞。

    `sys.modules[name] = None` 會讓 `import name` 丟
    `ImportError: import of psutil halted; None in sys.modules`，那才是真的「套件
    不在」。本檔的 autouse 夾具就是靠寫入 `sys.modules` 來裝安全替身的，所以
    **任何 `pop`／`del` 都會把安全網一起拆掉**——下面有一支守門釘住這件事。
    """
    sys.modules["psutil"] = None       # ← 讓 `import psutil` 真的丟 ImportError
    fake_clock(variant)
    variant._kill_orphan_chrome()
    assert len(taskkill_calls) == 2, "psutil 不在就只剩 taskkill，兩個 image 都要點名"


def test_restoring_a_none_sentinel_never_uncaches_the_real_module():
    """`sys.modules[name] = None` 還原之後必須**還是** None，不能變成「不在」。

    `sys.modules.get(name)` 對兩種完全不同的狀態回同一個答案：「這個名字不在
    快取裡」與「它在，值是 None」——而 None 正是「讓 `import` 失敗」的標準寫法
    （本檔與 `test_process_control._no_psutil` 都在用）。分不出來就會把第二種
    還原成 `pop`，於是下一個 `import psutil` 從磁碟載入**真的**那一個，安全網
    無聲地被自己的夾具拆掉。這就是 2026-09-07 那兩次意外的機制。

    用一個假名字驗，全程不碰 psutil。
    """
    name = "_axiomatic_probe_not_a_real_module"
    assert name not in sys.modules
    try:
        sys.modules[name] = None
        _restore_module(name, None)
        assert name in sys.modules and sys.modules[name] is None, (
            "None 哨兵被還原成「拿掉」了——那會讓下一個 import 拿到真的套件。")
        _restore_module(name, _ABSENT)
        assert name not in sys.modules, "本來就不在的，還原之後應該還是不在。"
    finally:
        # 走 helper，不寫 `sys.modules.pop` 字面——那個寫法本身是被守門禁掉的。
        _restore_module(name, _ABSENT)


def test_the_safety_net_is_actually_armed():
    """點名式的隔離自檢——**先確認安全網在，再談測到了什麼。**

    上面那些行為守門全都建立在「`import psutil` 拿到的是假的、`subprocess.run`
    是會 raise 的替身」這個前提上。前提要是悄悄失效，它們不會變紅，它們會**照樣
    綠**，只是順便把這台機器上正在跑的 Chrome 殺掉（2026-09-07 兩次，其中一次
    弄掉一個連續執行 78.7 小時的批次）。所以前提本身要有一支測試。

    刻意用身分比對而不是「真的跑一次 taskkill 看它會不會 raise」——後者在安全網
    失效的時候**就會真的執行那個命令**，等於把驗證本身變成災難。
    """
    assert isinstance(subprocess.run, _NoTaskkill), (
        "`subprocess.run` 不是安全替身——taskkill 會真的執行。")
    installed = sys.modules.get("psutil", _ABSENT)
    assert installed is not _REAL_PSUTIL, (
        "`sys.modules['psutil']` 是真的那一個——`proc.kill()` 會殺到真行程。")
    assert not hasattr(installed, "pid_exists"), (
        f"裝上去的 psutil 看起來是真貨（有 pid_exists）：{installed!r}")


def test_no_test_here_can_dismantle_the_process_safety_net():
    """**沒有任何 `test_*` 可以把 psutil 的安全替身從 `sys.modules` 拿掉。**

    這支守的不是被測程式，是**這個檔案自己**。autouse 夾具靠「往
    `sys.modules['psutil']` 塞一個假模組」來保證測試摸不到真行程；而
    `sys.modules.pop("psutil")` / `del sys.modules["psutil"]` 會把那層保護整個
    拆掉，接下來的 `import psutil` 就是真的那一個，`proc.kill()` 也是真的。

    這不是假設：2026-09-07 就是這樣殺掉一個跑了 78.7 小時的正式批次的
    Chrome。失效方向必須是「測試紅」，不能是「機器被動到」——所以在原始碼層
    直接禁掉這個寫法。要模擬套件不存在，寫 `sys.modules[...] = None`。

    夾具自己在 `finally` 裡還原時的 `pop` 是合法的，所以只掃 `test_*` 函式。
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("test_"):
            continue
        for node in ast.walk(fn):
            # sys.modules.pop(...)
            if (isinstance(node, ast.Call)
                    and ast.unparse(node.func).endswith("sys.modules.pop")):
                offenders.append(f"{fn.name}:{node.lineno} sys.modules.pop")
            # del sys.modules[...]
            if isinstance(node, ast.Delete):
                for target in node.targets:
                    if (isinstance(target, ast.Subscript)
                            and ast.unparse(target.value).endswith(
                                "sys.modules")):
                        offenders.append(
                            f"{fn.name}:{node.lineno} del sys.modules[...]")
    assert not offenders, (
        f"這些測試把行程安全網拆掉了：{offenders}。`pop`／`del` 只是清掉**快取**，"
        "下一次 `import psutil` 會從磁碟載入真的那一個，於是 `proc.kill()` 殺的是"
        "這台機器上真正在跑的 Chrome（2026-09-07 實際發生過，弄掉一個跑了 78.7 "
        '小時的批次）。要模擬「套件不存在」請寫 `sys.modules["psutil"] = None`，'
        "那會讓 `import` 丟 ImportError。")


def test_a_psutil_failure_still_reaches_the_taskkill_fallback(
        variant, fake_clock, taskkill_calls):
    """psutil 掃描**自己炸掉**的時候，taskkill 後援一定要跑。

    這是真缺陷，2026-09-07 實測重現：`except Exception` 只印一行就讓 `remaining`
    停在 0，於是 `remaining != 0` 是 False、整段 taskkill 被跳過，而摘要照樣印

        orphan-chrome sweep: killed 0 via psutil

    跟「這台機器是乾淨的」一字不差。孤兒行程原封不動留著，下一個 Chrome 照樣
    OOM——正是這支掃描存在的理由。**psutil 出錯是「psutil 掃不到東西」最強的
    證據，不是跳過後援的理由。**
    """
    sys.modules["psutil"] = _fake_psutil(
        process_iter_error=RuntimeError("psutil 內部炸了"))
    fake_clock(variant)
    variant._kill_orphan_chrome()
    images = sorted(args[0][-1] for args, _kw in taskkill_calls)
    assert images == ["chrome.exe", "chromedriver.exe"], (
        f"psutil 掃描失敗之後 taskkill 後援沒有跑（收到 {images}）。"
        "掃描等於整個沒做，而 log 會說 killed 0——看起來就像機器很乾淨。")


def test_the_sweep_reports_that_psutil_was_unusable(
        variant, fake_clock, taskkill_calls, capsys):
    """摘要要報**結果**不是報嘗試——psutil 沒跑成就要講出來。

    本專案同一個毛病已經記錄到第七例。這裡的版本特別壞：那行摘要是掃描留下的
    唯一痕跡，而它在「psutil 整個炸掉」與「機器很乾淨」兩種情況下**一模一樣**。
    """
    sys.modules["psutil"] = _fake_psutil(
        process_iter_error=RuntimeError("psutil 內部炸了"))
    fake_clock(variant)
    variant._kill_orphan_chrome()
    out = capsys.readouterr().out
    summary = [line for line in out.splitlines()
               if line.startswith("orphan-chrome sweep: killed")]
    assert summary, f"完全沒有印摘要：{out!r}"
    assert "taskkill only" in summary[-1], (
        f"摘要沒有講 psutil 這一輪不能用：{summary[-1]!r}。"
        "跟乾淨機器的輸出長得一樣，讀 log 的人會得到相反的結論。")


# ---------- snapshot 目錄裡的 lock 檔 ----------------------------------------
#
# 這裡以前是「檔案鎖復原鏈」的測試（`_kill_chrome_holders_of` /
# `_diagnose_file_lock` / `_force_unlink` 的四階梯，兩個變體各一份）。整條鏈在
# 2026-09-07 連同測試一起移除——它清的是 `.chrome_profile/`，而 Chrome 開的是
# `.chrome_profile_snap/`，所以它**即使被接上去也等於沒接**；而且在 Windows 上
# 「殘留的 lock 檔擋住啟動」根本不是可到達的狀態。完整證據見
# 見 `_clear_snapshot_locks` 的 docstring。
#
# 留下來的是唯一一個對著正確目錄、而且真的有呼叫端的動作。


def test_the_snapshot_lock_sweep_only_touches_the_lock_names(
        variant, tmp_path):
    """只准刪 `_CHROME_LOCK_FILES` 列的那幾個名字。

    它跑在 Chrome 即將開啟的那份 profile 上，隔壁就是 `Cookies` /
    `Login Data`——多刪一個就是把這一輪的登入態丟掉，而症狀只會是「又要重新
    登入」，沒有人會把它連到這支函式。
    """
    snap = tmp_path / "snap"
    snap.mkdir()
    for name in variant._CHROME_LOCK_FILES:
        (snap / name).write_text("x", encoding="utf-8")
    keep = {"Cookies": "c", "Local State": "s", "lockfile.bak": "b"}
    for name, body in keep.items():
        (snap / name).write_text(body, encoding="utf-8")

    variant._clear_snapshot_locks(snap)

    survivors = sorted(p.name for p in snap.iterdir())
    assert survivors == sorted(keep), (
        f"剩下 {survivors}——只該剩下不是 lock 名字的那些。")


def test_the_snapshot_lock_sweep_reports_a_file_it_could_not_delete(
        variant, tmp_path, monkeypatch, capsys):
    """刪不掉要**印出來**，而且不准往上炸。

    刪不掉代表有行程還握著那個檔——那正是「上一輪沒收乾淨」的訊號，而它同時是
    這支函式唯一可能有話要說的時刻。舊的內嵌版本是 `except OSError: pass`，
    整件事無聲無息（本專案「log 要報結果不是報嘗試」的同一族）。而拋出去更糟：
    呼叫端是 `build_stealth_driver()` 的開頭，一個 no-op 等級的清理不該擋住開機。
    """
    snap = tmp_path / "snap"
    snap.mkdir()
    victim = snap / "lockfile"
    victim.write_text("x", encoding="utf-8")

    real_unlink = pathlib.Path.unlink

    def _refuse(self, *args, **kwargs):
        if self.name == "lockfile":
            raise PermissionError(13, "held by another process")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", _refuse)
    variant._clear_snapshot_locks(snap)          # 不得 raise

    err = capsys.readouterr().err
    assert "lockfile" in err, (
        f"刪不掉卻什麼都沒印：{err!r}。這是這支函式唯一有話要說的時刻。")


def test_the_snapshot_lock_sweep_survives_a_missing_directory(
        variant, tmp_path):
    """目錄不存在（snapshot 建立失敗）也不能炸——它是開機路徑上的第一步。"""
    variant._clear_snapshot_locks(tmp_path / "not-there")


def test_every_driver_boot_clears_the_locks_in_the_snapshot_it_will_open(
        variant):
    """**清的必須是 Chrome 真正要開的那個目錄**，而且每一條 spawn 路徑都要清。

    這一支盯的是上一版真正的缺陷：那條被移除的鏈清的是 `.chrome_profile/`，
    Chrome 開的卻是 `--user-data-dir=<snapshot>`。所以判準不是「有沒有呼叫」，
    是「拿到的參數是不是剛剛那次 `_snapshot_chrome_profile()` 的回傳值」。

    順帶盯住兩個變體的重試路徑一致：je 那一邊原本在重試時只重建 snapshot、沒有
    再清一次，跟 selenium 變體不對稱。
    """
    boot = "build_stealth_driver" if hasattr(
        variant, "build_stealth_driver") else "start_driver"
    tree = ast.parse((PKG_ROOT / f"{variant.__name__}.py").read_text(
        encoding="utf-8"))
    func = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == boot)

    # `snapshot = _snapshot_chrome_profile()` 之後緊接著要有
    # `_clear_snapshot_locks(snapshot)`，同一個名字。
    assigns = [n for n in ast.walk(func)
               if isinstance(n, ast.Assign)
               and isinstance(n.value, ast.Call)
               and isinstance(n.value.func, ast.Name)
               and n.value.func.id == "_snapshot_chrome_profile"]
    assert len(assigns) == 2, (
        f"{boot} 裡的 _snapshot_chrome_profile() 賦值有 {len(assigns)} 個，"
        "預期 2（初次 ＋ 重試）。改了 spawn 流程就要一起改這支測試。")

    cleared = [n for n in ast.walk(func)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name)
               and n.func.id == "_clear_snapshot_locks"]
    assert len(cleared) == len(assigns), (
        f"{boot} 取了 {len(assigns)} 次 snapshot，卻只清了 {len(cleared)} 次 "
        "lock。每一條 spawn 路徑都要清它自己那一份。")
    for call in cleared:
        assert (len(call.args) == 1
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == "snapshot"), (
            f"{boot} 把 {ast.dump(call.args[0])} 交給 _clear_snapshot_locks——"
            "必須是剛剛那次 snapshot 的回傳值，不是 CHROME_PROFILE_DIR 之類的"
            "別的目錄。清錯目錄等於沒清（2026-09-07 移除的那條鏈就是這樣）。")


# ---------- 登入 profile：snapshot 與寫回 ------------------------------------

@pytest.fixture
def profile_dirs(variant, monkeypatch, tmp_path):
    """把兩個 profile 全域指向 tmp_path。**絕對不能**讓這些測試碰到真的
    `.chrome_profile/`——那是活的登入態。"""
    source = tmp_path / "profile"
    snap = tmp_path / "snap"
    (source / "Default").mkdir(parents=True)
    monkeypatch.setattr(variant, "CHROME_PROFILE_DIR", source)
    monkeypatch.setattr(variant, "CHROME_PROFILE_SNAPSHOT", snap)
    return source, snap


def test_the_snapshot_carries_the_login_and_leaves_the_locks_behind(
        variant, profile_dirs):
    """snapshot 的用途是「Chrome 開的是副本、跟原 profile 的 lockfile 無關」。
    所以 lock 檔與快取目錄要留下、登入相關的要帶走。"""
    source, snap = profile_dirs
    (source / "Default" / "Network").mkdir()
    (source / "Default" / "Network" / "Cookies").write_text(
        "cookie", encoding="utf-8")
    (source / "Default" / "Login Data").write_text("login", encoding="utf-8")
    (source / "Local State").write_text("state", encoding="utf-8")
    (source / "SingletonLock").write_text("lock", encoding="utf-8")
    (source / "lockfile").write_text("lock", encoding="utf-8")
    (source / "Default" / "Cache").mkdir()
    (source / "Default" / "Cache" / "big.bin").write_bytes(b"0" * 100)

    result = variant._snapshot_chrome_profile()

    assert result == snap
    assert (snap / "Default" / "Network" / "Cookies").read_text(
        encoding="utf-8") == "cookie"
    assert (snap / "Default" / "Login Data").exists()
    assert (snap / "Local State").exists()
    assert not (snap / "SingletonLock").exists(), (
        "lock 檔被複製進 snapshot——Chrome 看到它就會認為還有別的 instance 在用，"
        "直接 exit（SessionNotCreatedException）。")
    assert not (snap / "lockfile").exists()
    assert not (snap / "Default" / "Cache").exists(), "快取目錄不該複製"


def test_a_snapshot_that_carried_nothing_still_says_so(
        variant, profile_dirs, capsys):
    """一個檔案都沒複製到是最糟的結果——Chrome 會用一份**空的** profile 開機，
    也就是這一輪必然要重新登入。舊版在這個情況下**一個字都不印**（摘要包在
    `if copied:` 裡），於是最嚴重的失敗剛好是唯一沒有記錄的那個。"""
    source, _snap = profile_dirs
    (source / "Default" / "Cache").mkdir()
    (source / "SingletonLock").write_text("lock", encoding="utf-8")

    variant._snapshot_chrome_profile()

    out = capsys.readouterr()
    assert "snapshot profile:" in (out.out + out.err), (
        "一個檔案都沒複製到，卻什麼都沒印——log 上跟一切正常長得一樣。")


def test_the_snapshot_names_the_login_files_it_could_not_carry_over(
        variant, profile_dirs, monkeypatch, capsys):
    """來源有、snapshot 沒有的 session-critical 檔案，是「這一輪會被登出」唯一
    可行動的訊號。原本只印一句 skipped N (locked / inaccessible)，看不出來掉的
    是快取還是登入權杖。

    順便釘住 2026-09-09 加上去的那個分野：**「來源有但複製失敗」與「來源也沒有」
    要看得出差別**。前者是鎖住／權限，重跑一次可能就好；後者是路徑寫錯或根本
    還沒登入過，重跑一百次也一樣。`_SESSION_CRITICAL` 那兩筆 cookie 路徑寫錯了
    四個月都沒被抓到，正是因為當時的判準要求來源存在，於是後者根本不會出聲。
    """
    source, _snap = profile_dirs
    (source / "Default" / "Network").mkdir()
    (source / "Default" / "Network" / "Cookies").write_text(
        "cookie", encoding="utf-8")
    (source / "Default" / "Web Data").write_text("web", encoding="utf-8")
    (source / "Default" / "other.bin").write_text("junk", encoding="utf-8")

    import shutil as _shutil
    real_copy2 = _shutil.copy2

    def _copy2(src, dst, **kwargs):
        if Path(src).name == "Cookies":
            raise PermissionError(32, "被鎖住")
        return real_copy2(src, dst, **kwargs)

    monkeypatch.setattr(_shutil, "copy2", _copy2)
    variant._snapshot_chrome_profile()

    err = capsys.readouterr().err
    assert "Default/Network/Cookies" in err, (
        f"沒有點名帶不過去的登入檔案：{err!r}。"
        "少了它就只知道 skipped 1，不知道那一個剛好是登入權杖。")
    assert "Default/Network/Cookies（來源也沒有）" not in err, (
        f"把「來源有、複製失敗」講成「來源也沒有」：{err!r}。"
        "兩者要做的事完全不同（重試 vs 修路徑），標錯等於沒標。")
    assert "Default/Login Data（來源也沒有）" in err, (
        f"來源根本沒有的那幾筆沒有被標出來：{err!r}。這個標記就是「路徑寫錯」"
        "唯一看得見的形狀。")


def _complete_profile(variant, root: Path) -> None:
    """每一筆登入相關的檔案都在、每一個 LevelDB 都自洽的 profile。"""
    for rel in variant._SESSION_CRITICAL:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x", encoding="utf-8")
    for rel in variant._SESSION_CRITICAL_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
        (root / rel / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
        (root / rel / "MANIFEST-000001").write_bytes(b"\x00")


def test_a_complete_snapshot_raises_no_logout_warning(variant, profile_dirs, capsys):
    """「這一輪很可能要重新登入」只有在真的少了東西時才能出現。整套測試裡那個警告從來沒有
    **不**出現過——每一支都是缺東西的情境——所以「永遠印」這種壞法不會有任何東西變紅，
    而一個每輪都叫的警告很快就沒人看了。"""
    assert variant._SESSION_CRITICAL and variant._SESSION_CRITICAL_DIRS
    source, snap = profile_dirs
    _complete_profile(variant, source)
    assert variant._snapshot_chrome_profile() == snap
    err = capsys.readouterr().err
    assert "重新登入" not in err, err
    for rel in variant._SESSION_CRITICAL:
        assert (snap / rel).is_file(), rel


def test_a_missing_source_profile_yields_an_empty_snapshot_and_says_so(
        variant, profile_dirs, capsys):
    """第一次執行（或 profile 被搬走）：snapshot 開成空的，並講一聲這一輪要登入——不是崩潰。"""
    source, snap = profile_dirs
    import shutil
    shutil.rmtree(source)
    assert variant._snapshot_chrome_profile() == snap
    assert snap.is_dir() and not any(snap.iterdir())
    assert "重新登入" in capsys.readouterr().out
    assert not source.exists(), "快照不該反過來建出來源目錄"


def test_sync_back_without_a_snapshot_leaves_the_login_profile_alone(
        variant, profile_dirs, capsys):
    """snapshot 不見了（被清掉、上一步失敗）時，寫回什麼都不做——尤其不能把「沒有」當成
    「空的」寫回去，那會蓋掉唯一撐著登入態的那份。並講明這一輪什麼都沒寫回。"""
    source, snap = profile_dirs
    _complete_profile(variant, source)
    before = sorted((p.relative_to(source).as_posix(), p.read_bytes())
                    for p in source.rglob("*") if p.is_file())
    assert not snap.exists()
    variant._sync_chrome_profile_back(snap)
    after = sorted((p.relative_to(source).as_posix(), p.read_bytes())
                   for p in source.rglob("*") if p.is_file())
    assert after == before
    err = capsys.readouterr().err
    assert f"0/{len(variant._SESSION_CRITICAL)}" in err and "重新登入" in err, err


def test_sync_back_only_carries_the_session_critical_files(
        variant, profile_dirs):
    """寫回的範圍要窄。整棵樹寫回去等於把 snapshot 的快取／崩潰傾印也倒進登入
    profile，而那份 profile 是唯一撐著「不用重新登入」的東西。"""
    source, snap = profile_dirs
    (snap / "Default" / "Network").mkdir(parents=True)
    (snap / "Default" / "Network" / "Cookies").write_text(
        "new-cookie", encoding="utf-8")
    (snap / "Local State").write_text("new-state", encoding="utf-8")
    (snap / "Default" / "History").write_text("history", encoding="utf-8")
    (snap / "Default" / "Cache").mkdir()
    (snap / "Default" / "Cache" / "junk.bin").write_bytes(b"junk")

    variant._sync_chrome_profile_back(snap)

    assert (source / "Default" / "Network" / "Cookies").read_text(
        encoding="utf-8") == "new-cookie"
    assert (source / "Local State").read_text(encoding="utf-8") == "new-state"
    assert not (source / "Default" / "History").exists()
    assert not (source / "Default" / "Cache").exists()


def test_sync_back_never_leaves_a_half_written_login_file(
        variant, profile_dirs, monkeypatch):
    """寫回登入檔要是原子的。

    `shutil.copy2` 是「開目標為 wb 立刻截斷」，所以中途被砍（`/stop`、
    `taskkill /F`、當機、機器睡著）留下的是**半個 Cookies**。下一輪 Chrome 不會
    報錯，它會安靜地以未登入狀態開機——CLAUDE.md 那條跨行程原子寫入硬規則講的
    「不是崩潰而是安靜的錯誤結果」就是這個。

    這裡用一個「寫了一半才炸」的 copy2 模擬那一刀，然後檢查原本的登入檔還在。

    **`attempts` 那個正面對照組是必要的，不是裝飾。** 這支的主斷言是「原檔沒被
    動到」——而「一次都沒有嘗試複製」也完全滿足它。2026-09-09 把 cookie 路徑改
    成 `Default/Network/` 時，這支（與 `..._survives_dying_at_the_swap`）用的還
    是舊路徑，於是 sync-back 什麼都沒碰、測試**照樣綠**：空的選擇看起來跟乾淨的
    結果一模一樣。
    """
    source, snap = profile_dirs
    (snap / "Default" / "Network").mkdir(parents=True)
    (snap / "Default" / "Network" / "Cookies").write_text(
        "new-cookie", encoding="utf-8")
    live = source / "Default" / "Network" / "Cookies"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("ORIGINAL-COOKIE", encoding="utf-8")

    import shutil as _shutil
    attempts = []

    def _copy2_dies_midway(src, dst, **_kwargs):
        attempts.append(Path(src))
        Path(dst).write_text("HALF", encoding="utf-8")   # 截斷已經發生
        raise OSError(5, "行程在複製到一半被砍掉")

    monkeypatch.setattr(_shutil, "copy2", _copy2_dies_midway)
    variant._sync_chrome_profile_back(snap)

    assert attempts, (
        "sync-back 一個檔案都沒有嘗試複製——那樣下面那條「原檔沒被動到」是"
        "空的。多半是 _SESSION_CRITICAL 的路徑跟這裡造的檔案對不起來。")
    assert live.read_text(encoding="utf-8") == "ORIGINAL-COOKIE", (
        "複製中途失敗把活的登入檔寫壞了。要走同目錄 temp 再 os.replace，"
        "失敗時原檔必須原封不動。")
    leftovers = [p.name for p in (source / "Default" / "Network").iterdir()
                 if p.name != "Cookies"]
    assert leftovers == [], (
        f"留下了 {leftovers}——那是一份登入 profile 的殘骸躺在磁碟上。"
        "本專案有前科（隔離驗證用的 profile 副本曾經沒被 gitignore 蓋到）。")


def test_sync_back_survives_dying_at_the_swap(variant, profile_dirs, monkeypatch):
    """另外半邊：死在**交換那一刻**（複製已經完成、`os.replace` 還沒生效）。

    跟上一支互補，而且刻意用**真的** `shutil.copy2`——唯一被替換的是失敗的觸發
    點。所以這支證明的是「複製與交換之間那個窗口也是安全的」：原檔完好，而且
    那份寫到一半的登入資料不會留在磁碟上。

    舊版（直接 copy2 覆蓋）在這裡也會紅，但紅的理由不一樣：它根本沒有交換那一
    步，複製當下就已經把活的登入檔改掉了。
    """
    source, snap = profile_dirs
    (snap / "Default" / "Network").mkdir(parents=True)
    (snap / "Default" / "Network" / "Cookies").write_text(
        "new-cookie", encoding="utf-8")
    live = source / "Default" / "Network" / "Cookies"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("ORIGINAL-COOKIE", encoding="utf-8")

    swaps = []

    def _replace_dies(*args, **_kwargs):
        swaps.append(args)
        raise OSError(5, "換過去的時候被砍")

    monkeypatch.setattr(variant.os, "replace", _replace_dies)
    variant._sync_chrome_profile_back(snap)

    assert swaps, (
        "一次交換都沒發生——那樣下面兩條都是空的（見上一支的 attempts 註解）。")
    assert live.read_text(encoding="utf-8") == "ORIGINAL-COOKIE", (
        "交換失敗卻已經動到活的登入檔——複製的目標必須是 temp，不是目標本身。")
    assert sorted(p.name for p in (source / "Default" / "Network").iterdir()) \
        == ["Cookies"], "半途而廢的登入資料留在磁碟上了。"


def test_sync_back_writes_its_temp_next_to_the_target(
        variant, profile_dirs, monkeypatch):
    """temp 必須跟目標**同一個目錄**，否則 `os.replace` 會跨磁碟區失敗——而那個
    失敗只會在別人的機器上發生。"""
    source, snap = profile_dirs
    (snap / "Default" / "Network").mkdir(parents=True)
    (snap / "Default" / "Network" / "Cookies").write_text(
        "new-cookie", encoding="utf-8")
    seen = []

    import shutil as _shutil
    real_copy2 = _shutil.copy2

    def _copy2(src, dst, **kwargs):
        seen.append((Path(src), Path(dst)))
        return real_copy2(src, dst, **kwargs)

    monkeypatch.setattr(_shutil, "copy2", _copy2)
    variant._sync_chrome_profile_back(snap)

    assert seen, "沒有複製任何東西"
    for src, dst in seen:
        assert dst.parent == (source / src.relative_to(snap)).parent, (
            f"temp 落在 {dst.parent}，不是目標所在的目錄——`os.replace` 不保證"
            "跨磁碟區可用。")
    assert (source / "Default" / "Network" / "Cookies").read_text(
        encoding="utf-8") == "new-cookie"


def test_sync_back_reports_a_denominator_not_just_a_count(
        variant, profile_dirs, capsys):
    """摘要要報 `N/M`，不是只報 N。

    這是 2026-09-09 那個缺陷的**形狀**，不只是它的症狀：`_SESSION_CRITICAL` 有
    兩筆 cookie 路徑指向 Chrome 96 之後就不存在的位置，於是 sync-back 每一輪都
    只寫回 6 筆——而 log 一字不差地印了 **29 次 `synced 6 session files back`**。
    只有分子的話，「少了兩個」跟「一切正常」在 log 上長得一模一樣，沒有任何人
    看得出來。分母是唯一能讓短少自己現形的東西。

    分母刻意取 `len(_SESSION_CRITICAL)` 而不是寫死數字：清單增減時這支會跟著
    走，不會變成另一個要人手動同步的常數。
    """
    _source, snap = profile_dirs
    total = len(variant._SESSION_CRITICAL)
    assert total >= 6, "清單短到不像真的——正面對照組先擋一下"
    carried = variant._SESSION_CRITICAL[:2]
    for rel in carried:
        target = snap / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")

    variant._sync_chrome_profile_back(snap)

    out = capsys.readouterr()
    text = out.out + out.err
    assert f"{len(carried)}/{total}" in text, (
        f"摘要沒有帶分母：{text!r}。只報分子的話，路徑寫錯造成的短少會跟正常"
        "結果長得一模一樣（實測：29 次 `synced 6`，正確答案是 8）。")


def test_a_sync_back_that_wrote_nothing_still_says_so(
        variant, profile_dirs, capsys):
    """一個都沒寫回去 ＝ 下一輪必定重新登入，也就是最糟的結果。

    舊版把摘要包在 `if synced:` 裡，於是那個最糟的結果剛好是唯一什麼都不印的。
    `_snapshot_chrome_profile` 的摘要 2026-09-07 才因為同一個毛病改成無條件印，
    這一支漏掉了——同一個檔案裡的同一個形狀，隔了兩天才補上。
    """
    _source, snap = profile_dirs
    snap.mkdir(parents=True, exist_ok=True)

    variant._sync_chrome_profile_back(snap)

    out = capsys.readouterr()
    text = out.out + out.err
    assert f"0/{len(variant._SESSION_CRITICAL)}" in text, (
        f"一個檔案都沒寫回去，log 上卻看不出來：{text!r}")


def test_sync_back_names_a_session_file_missing_from_the_snapshot(
        variant, profile_dirs, capsys):
    """跳過一筆一定要留下可觀察的訊號。

    迴圈的 `if not src.exists(): continue` 本身是**完全靜默**的，而那就是 cookie
    路徑寫錯活了四個月的機制：路徑指向一個不存在的檔案 → 每一輪安靜跳過 →
    `.chrome_profile/` 的 cookie jar 從 2026-05-20 之後再也沒被更新過。三層守門
    （這個迴圈、snapshot 那側的 `missing` 診斷、測試）各自因為不同理由失效。

    這支釘的是第一層：被跳過的那一筆要被**點名**，因為只有名字才看得出是「路徑
    寫錯」還是「Chrome 這一輪沒建出那個檔」。
    """
    _source, snap = profile_dirs
    snap.mkdir(parents=True, exist_ok=True)
    present, absent = variant._SESSION_CRITICAL[0], variant._SESSION_CRITICAL[-1]
    assert present != absent, "清單只有一筆，這支證明不了東西"
    target = snap / present
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")

    variant._sync_chrome_profile_back(snap)

    err = capsys.readouterr().err
    # 逗號分隔的清單要**拆開**再比，不要用子字串——`Default/Network/Cookies` 是
    # `Default/Network/Cookies-journal` 的前綴，子字串比對會兩邊都命中。
    named = {part.strip() for line in err.splitlines() if ": " in line
             for part in line.split(": ", 1)[1].split(",")}
    assert absent in named, (
        f"snapshot 裡沒有 {absent}，卻安靜跳過了：{err!r}。"
        "沒有名字就沒有辦法分辨路徑寫錯與檔案沒建出來。")
    assert present not in named, (
        f"寫回成功的 {present} 也被當成缺漏報出來了：{err!r}")


# ---------- 登入態真正的所在：localStorage 的 LevelDB 目錄 -------------------
#
# 2026-09-09：修好 cookie 路徑之後仍然每一輪重新登入，因為 cookie 從來就不是那個
# 站放權杖的地方。實測——那個站只有三筆 cookie（同意橫幅 ＋ Google 小工具狀態，
# 全都沒到期、全都不是驗證用的），而 `Default/Local Storage/leveldb/` 裡有 key
# 叫 `session`。`_snapshot_chrome_profile` 是整份 `os.walk` 所以帶得過去，漏的是
# 回程：`_sync_chrome_profile_back` 只走單檔清單。
#
# 這一組測的是**目錄**那條路徑，跟上面那組單檔的互補。


def _leveldb_rel(variant) -> str:
    """要同步的 LevelDB 相對路徑，順便當正面對照組。

    `_SESSION_CRITICAL_DIRS` 是空 tuple 的話，程式碼會安靜地退回「只同步單檔」
    ——也就是 2026-09-09 之前那個從來沒有生效過的狀態——而下面每一支測試都會變成
    在測一個不存在的東西然後通過。空的選擇看起來跟乾淨的結果一模一樣。
    """
    dirs = variant._SESSION_CRITICAL_DIRS
    assert dirs, (
        "_SESSION_CRITICAL_DIRS 是空的。目錄同步整段等於被關掉，而所有單檔測試"
        "照樣綠——症狀是「每一輪都重新登入」，跟這個常數看不出關係。")
    assert any("Local Storage" in d for d in dirs), (
        f"清單裡沒有 localStorage 的 LevelDB：{dirs}。登入態就在那裡；只同步"
        "cookie 是這個缺陷四個月沒被發現的原因。")
    return dirs[0]


def _make_leveldb(root: Path, manifest: str, tables) -> None:
    """造一份最小但**自洽**的 LevelDB：`CURRENT` → `MANIFEST-*` → 一組表檔，
    外加 leveldb 自己那三個不屬於 DB 狀態的檔案（`LOCK` / `LOG` / `LOG.old`）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "CURRENT").write_text(manifest + "\n", encoding="utf-8")
    (root / manifest).write_bytes(b"manifest-bytes")
    for name in tables:
        (root / name).write_bytes(name.encode("ascii"))
    (root / "LOCK").write_bytes(b"")
    (root / "LOG").write_text("leveldb debug", encoding="utf-8")
    (root / "LOG.old").write_text("leveldb debug", encoding="utf-8")


def test_sync_back_carries_the_whole_localstorage_leveldb(
        variant, profile_dirs):
    """核心修正：登入態（localStorage）要被寫回去。

    在此之前 `_sync_chrome_profile_back` 只走單檔清單，於是來源 profile 的
    `session` 永遠停在 2026-05-20——每一輪都拿一份四個月前的權杖去試，然後被導去
    `/login`。實測 `session restored` 對 36 次 Chrome 啟動 ＝ **0**。
    """
    source, snap = profile_dirs
    rel = _leveldb_rel(variant)
    _make_leveldb(snap / rel, "MANIFEST-000001", ("000019.ldb", "000022.log"))

    variant._sync_chrome_profile_back(snap)

    landed = source / rel
    assert (landed / "CURRENT").read_text(encoding="utf-8").strip() \
        == "MANIFEST-000001"
    assert (landed / "000019.ldb").read_bytes() == b"000019.ldb"
    assert (landed / "000022.log").read_bytes() == b"000022.log"


def test_sync_back_replaces_the_leveldb_instead_of_merging_two_generations(
        variant, profile_dirs):
    """**先清乾淨再整組寫入**，不是逐檔覆蓋。

    LevelDB 是一組互相參照的檔案：`CURRENT` → `MANIFEST-*` → manifest 列出的
    `*.ldb` / `*.log`。實測（2026-09-09）來源那份是 `000005/000008/000010` ＋
    `000011.log`，同一份 profile 跑幾小時之後 snapshot 變成
    `000005/000019/000021/000023` ＋ `000022.log`——leveldb 的 LOG 裡有整串
    `Delete type=2 #8 #10 …` 的壓實紀錄。

    逐檔覆蓋會讓目的地同時擁有新 manifest 與一堆它不認得的舊檔案；而**缺一個
    manifest 指到的檔案，Chrome 不會報錯，它會安靜地把整個 DB 丟掉**，症狀就是
    又要重新登入——跟現在一模一樣，也就是最難查的那一種。

    所以這裡釘的是**結果的檔案集合**：舊世代一個都不許留下。
    """
    source, snap = profile_dirs
    rel = _leveldb_rel(variant)
    _make_leveldb(source / rel, "MANIFEST-000001",
                  ("000005.ldb", "000008.ldb", "000010.ldb", "000011.log"))
    _make_leveldb(snap / rel, "MANIFEST-000007",
                  ("000005.ldb", "000019.ldb", "000022.log"))

    variant._sync_chrome_profile_back(snap)

    landed = sorted(p.name for p in (source / rel).iterdir())
    assert landed == ["000005.ldb", "000019.ldb", "000022.log", "CURRENT",
                      "MANIFEST-000007"], (
        f"目的地是 {landed}。舊世代的檔案還在（或新的沒到齊）＝ 兩個世代混在"
        "一起，而 Chrome 對那個狀態的處置是安靜地丟掉整個 DB。")
    assert not (source / rel).with_name(
        (source / rel).name + ".sync.tmp").exists()
    assert not (source / rel).with_name(
        (source / rel).name + ".sync.old").exists()


def test_sync_back_leaves_the_leveldb_lock_and_debug_log_behind(
        variant, profile_dirs):
    """`LOCK` / `LOG` / `LOG.old` 不帶走。

    `LOCK` 是 leveldb 的 advisory lock，DB 開著的時候在 Windows 上是獨佔開啟的
    （實測連唯讀開啟都吃 `PermissionError`）——跳過它就等於把這個目錄裡唯一會被
    鎖住的東西排除掉。`LOG` / `LOG.old` 不被 MANIFEST 參照，不是 DB 一致狀態的
    一部分；帶過去只會留下一份**描述著另一個目錄**的記錄（實測 `.chrome_profile`
    那份 LOG 裡印的還是專案改名前的路徑），害下一個人查錯方向。
    """
    source, snap = profile_dirs
    rel = _leveldb_rel(variant)
    _make_leveldb(snap / rel, "MANIFEST-000001", ("000019.ldb",))

    variant._sync_chrome_profile_back(snap)

    landed = {p.name for p in (source / rel).iterdir()}
    assert landed == {"CURRENT", "MANIFEST-000001", "000019.ldb"}, (
        f"帶了不該帶的：{sorted(landed)}")
    assert set(variant._LEVELDB_SKIP_NAMES) & {
        p.name for p in (snap / rel).iterdir()}, (
        "來源根本沒有那幾個要跳過的檔名——上面那條斷言是空的。")


def test_sync_back_refuses_to_install_a_broken_leveldb(
        variant, profile_dirs, capsys):
    """組出來的 DB 不完整就**不換**，保留舊的。

    `CURRENT` 指到一份不存在的 MANIFEST ＝ Chrome 會丟掉整個 DB。這種時候裝上去
    比不裝更糟：不裝只是拿一份過期的登入態去試（多登入一次），裝上去是連同其他
    origin 的 localStorage 一起清空。所以失敗的方向刻意選「保留舊的」。
    """
    source, snap = profile_dirs
    rel = _leveldb_rel(variant)
    _make_leveldb(source / rel, "MANIFEST-000001", ("000010.ldb",))
    (snap / rel).mkdir(parents=True)
    (snap / rel / "CURRENT").write_text("MANIFEST-000009\n", encoding="utf-8")
    (snap / rel / "000019.ldb").write_bytes(b"orphan")   # manifest 不在

    swapped, copied = variant._sync_profile_dir_back(snap, rel)

    assert swapped is False, "把一份 CURRENT 指不到 MANIFEST 的 DB 裝上去了"
    assert copied == 2, f"copied={copied}——正面對照組：它應該真的複製過東西"
    assert (source / rel / "000010.ldb").exists(), "舊的那份被弄掉了"
    assert (source / rel / "CURRENT").read_text(encoding="utf-8").strip() \
        == "MANIFEST-000001"
    err = capsys.readouterr().err
    assert "不完整" in err and "重新登入" in err, (
        f"拒絕安裝卻沒有出聲：{err!r}。「保留舊的」在磁碟上跟「換上去了」長得"
        "一模一樣，只有 log 分得出來。")


def test_sync_back_survives_dying_between_the_two_renames(
        variant, profile_dirs):
    """目錄交換是兩步 rename，中間有一個「目的地短暫不存在」的窗口。

    單檔可以一步 `os.replace` 到位；目錄不行——Windows 的 `MoveFileEx` 對已存在
    的目錄不吃 `REPLACE_EXISTING`。所以這裡的原子性是**目錄級的取捨**，不是忘了
    照 CLAUDE.md 那條硬規則做。取捨的代價由 `_reclaim_dir_sync_residue` 補起來：
    死在窗口裡的話完好的舊資料躺在 `.sync.old`，把它搬回去。

    這一支模擬第二步被砍，然後要求**目的地仍然是可用的**（救回舊的那份）。
    """
    source, snap = profile_dirs
    rel = _leveldb_rel(variant)
    _make_leveldb(source / rel, "MANIFEST-000001", ("000010.ldb",))
    _make_leveldb(snap / rel, "MANIFEST-000007", ("000019.ldb",))

    real_replace = os.replace
    calls = []

    def _replace(src, dst, **kwargs):
        calls.append((Path(src).name, Path(dst).name))
        if len(calls) == 2:                    # tmp → dst 那一步
            raise OSError(5, "行程被砍在交換的正中間")
        return real_replace(src, dst, **kwargs)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(variant.os, "replace", _replace)
        swapped, _copied = variant._sync_profile_dir_back(snap, rel)
    finally:
        monkey.undo()

    assert len(calls) >= 2, f"沒有走到第二步 rename：{calls}"
    assert swapped is False
    assert (source / rel / "000010.ldb").exists(), (
        "死在窗口裡之後目的地是空的——下一輪 snapshot 會複製一份沒有 "
        "localStorage 的 profile 過去，登入態整個消失。")
    assert not (source / rel).with_name(
        (source / rel).name + ".sync.old").exists(), "殘骸留在磁碟上了"


def test_the_snapshot_reclaims_a_half_swapped_leveldb_before_copying(
        variant, profile_dirs, capsys):
    """真正會救到的時機是**下一次開機**，不是交換那一支自己的 except。

    行程被硬砍（`taskkill /F`、當機、機器睡著）的話 except 分支根本不會執行，
    磁碟上留下的就是「`leveldb/` 不見了、資料在 `leveldb.sync.old/`」。所以
    `_snapshot_chrome_profile` 在 `os.walk` **之前**先救一次——不先救的話，這一趟
    會把一份沒有 localStorage 的 profile 複製過去，而且 `.sync.old/` 還會被當成
    一般目錄一起複製，看起來一切正常。
    """
    source, snap = profile_dirs
    rel = _leveldb_rel(variant)
    residue = (source / rel).with_name(Path(rel).name + ".sync.old")
    _make_leveldb(residue, "MANIFEST-000001", ("000010.ldb",))
    assert not (source / rel).exists(), "前提沒造對：目的地應該是不存在的"

    variant._snapshot_chrome_profile()

    assert (source / rel / "000010.ldb").exists(), (
        "開機時沒有把半途而廢的交換救回來——這一輪的 localStorage 是空的。")
    assert not residue.exists()
    assert (snap / rel / "000010.ldb").exists(), (
        "救回來了卻沒有被複製進 snapshot——順序錯了（救援必須在 walk 之前）。")
    assert "救回" in capsys.readouterr().err, "救援是靜默的，log 上看不出來"


def test_sync_back_reports_the_directory_denominator_and_the_file_count(
        variant, profile_dirs, capsys):
    """摘要要能讓「目錄那一半短少了」自己現形。

    單檔那半的分母（`N/M`）是 2026-09-09 早上才補的，理由是只報分子的話「少了
    兩個」跟「一切正常」在 log 上長得一模一樣。目錄這半有**兩種**短少，所以要
    兩個數字：換過去了沒有（`D/E`），以及裡面**實際帶了幾個檔案**。少了後者，
    「換上去一份空的 LevelDB」會讓 `1/1` 看起來完全正常，而它的後果是登入態全沒。

    兩個分母刻意不合併：把一個 LevelDB 目錄跟一個 cookie jar 算成同一種「一筆」，
    分母就再也不能回答「短少了嗎」。
    """
    _source, snap = profile_dirs
    rel = _leveldb_rel(variant)
    files = len(variant._SESSION_CRITICAL)
    dirs = len(variant._SESSION_CRITICAL_DIRS)
    _make_leveldb(snap / rel, "MANIFEST-000001", ("000019.ldb", "000022.log"))

    variant._sync_chrome_profile_back(snap)

    text = "".join(capsys.readouterr())
    assert f"0/{files}" in text, f"單檔那半的分母不見了：{text!r}"
    assert f"{dirs}/{dirs} session dirs" in text, (
        f"目錄那半沒有分母：{text!r}")
    assert f"{rel}: 4 files" in text, (
        f"沒有報出目錄裡實際帶了幾個檔案：{text!r}。"
        "少了它，一份空的 LevelDB 會顯示成完全正常的 1/1。")


def test_a_directory_sync_that_never_happened_still_says_so(
        variant, profile_dirs, capsys):
    """snapshot 裡根本沒有那個目錄 ＝ 下一輪必定重新登入，也就是最糟的結果。
    這個專案已經被同一個形狀騙過兩次（摘要包在 `if copied:` / `if synced:` 裡，
    於是最嚴重的失敗剛好是唯一不出聲的那個），這裡不要再犯第三次。"""
    _source, snap = profile_dirs
    snap.mkdir(parents=True, exist_ok=True)
    dirs = len(variant._SESSION_CRITICAL_DIRS)

    variant._sync_chrome_profile_back(snap)

    text = "".join(capsys.readouterr())
    assert f"0/{dirs} session dirs" in text, (
        f"一個登入目錄都沒寫回去，log 上卻看不出來：{text!r}")
    assert "NOT swapped" in text


def test_the_snapshot_names_a_leveldb_that_did_not_make_it(
        variant, profile_dirs, capsys):
    """snapshot 之後的診斷也要看目錄，而且判準是**完整**不是**存在**。

    一個 `CURRENT` 指不到 MANIFEST 的目錄「存在」但沒有用——Chrome 會丟掉整個
    DB。只問 `exists()` 的話，最常見的壞法（複製到一半、少一個表檔）會被判成
    正常。這跟同一支函式 2026-09-09 學到的那一課是同一個：**判準下錯地方的檢查
    永遠不會說話。**
    """
    source, _snap = profile_dirs
    rel = _leveldb_rel(variant)
    (source / rel).mkdir(parents=True)
    (source / rel / "CURRENT").write_text("MANIFEST-000009\n", encoding="utf-8")

    variant._snapshot_chrome_profile()

    err = capsys.readouterr().err
    assert rel in err, (
        f"複製過去的是一份壞掉的 LevelDB，診斷卻沒點名：{err!r}")


# ---------- 兩個變體不得漂移 -------------------------------------------------

# **要比哪些函式是算出來的，不是列出來的。**
#
# 2026-09-10 之前這裡是一份 `_MIRRORED` 白名單，而白名單在這裡是 **fail-open**：
# 兩邊都加了一支新 helper、忘了登記，它就從此不被比對，而外觀上完全看不出來
# 。那份名單在
# 2026-09-09 才因為目錄級 sync-back 從 4 筆長到 8 筆——也就是說「有人記得回來更新
# 它」這件事已經是靠運氣了。
#
# 現在的判準：**兩個變體都有的模組層函式，預設就得逐一 AST 相同**；刻意的分岔列在
# 下面、每一筆寫理由。這個方向 fail-closed——新加一支兩邊都有的 helper 如果沒同步
# 會當場紅，本來就該分岔的話代價只是加一行理由。
#
# 實測 2026-09-10：兩個變體共有 15 支模組層函式，**8 支逐字相同、7 支宣告分岔**
# （下面這份就是那 7 筆），而舊白名單剛好等於「共有 − 這 7 筆」。也就是說今天兩種
# 寫法答案相同——正因為相同，才需要
# `test_the_mirror_list_is_derived_not_a_hardcoded_set` 那根釘子。
# （這段原本把 8 和 7 寫反了，2026-09-10 覆核時量出來更正。）
_DECLARED_PER_VARIANT = {
    "_setup_session":
        "登入／設定流程本身就分岔：selenium 變體走 WebDriverWait ＋ EC ＋ CSS，"
        "je 變體走 wait_until ＋ xpath 輪詢。共用層是靠 `setup_fn` callback 呼叫"
        "它的，所以兩份實作是刻意的。",
    "login_if_needed":
        "同上，等待機制不同（`WebDriverWait` vs 輪詢迴圈）。",
    "ensure_two_characters":
        "唯一呼叫端是各自的 `_setup_session`，跟著它一起分岔。",
    "click_add_character":
        "同上：一邊 `WebDriverWait`／`EC`，一邊自己寫的輪詢迴圈。",
    "hide_chrome_window":
        "讀各自延後初始化的 Chrome profile 全域變數；真正碰 OS 的那一半已經共用。",
    "_restart_chrome_session":
        "selenium 變體回傳新的 driver 給 `port.set_driver` 並重新指向 wrapper 的"
        "`current_webdriver`；je 變體驅動的是模組全域的 `wr` singleton，所以不回傳。",
    "main":
        "薄殼：`finally` 裡要 quit 的對象不同（`port.driver` vs `wr`），boot 進入點"
        "也不同。",
}

# 下限。真實資料乾淨時，「比 15 支」與「比 0 支」在輸出上一模一樣，所以要有下限。
#
# ⚠️ **下限要留餘裕，而且不能兼任「範圍有沒有縮掉」的偵測器。** 2026-09-10 覆核量
# 到這兩個數字原本是 15／7，而實際值是 15／8——**餘裕 0 與 1**。也就是說任何一次
# 合法的改名、或把一支 helper 搬進共用層，都會讓它們變紅，而訊息寫的是「抽取器
# 壞了」，那句話會把下一個人送去找一個不存在的 bug（MEMORY：a positive control can
# cry wolf；而最便宜的修法「把數字調低」會把控制組變成裝飾）。
#
# 現在分工清楚：**下限只負責抓「崩到 0」那一類**（抽取器壞掉、豁免清單倒過來變成
# 白名單），所以放得夠低；**「範圍有沒有縮掉」交給下面的具名 canary**——那是數字
# 回答不了的問題，因為 15→12 跟 15→18 在一個 `len()` 上長得一樣。
_COMMON_FUNC_FLOOR = 10          # 實測 15
_MIRRORED_FLOOR = 4              # 實測 8（2026-09-09 之前真的是 4，所以這是有據的）

# 具名 canary：這幾支不見了就當場點名，不要等某個數字掉破門檻。
#
# `_COMMON_CANARY` 刻意**同時**放了「逐一相同」與「宣告分歧」兩類——共有集合的工作
# 是把兩邊都有的函式全撈出來，只放前者的話，豁免那一半整個消失也不會有人說話。
_COMMON_CANARY = frozenset({
    "_kill_orphan_chrome",        # 逐一相同
    "_snapshot_chrome_profile",   # 逐一相同
    "main",                       # 宣告分歧
    "_setup_session",             # 宣告分歧
})
# `_MIRRORED_CANARY` 是「必須真的被逐字比對」的那些。少了任何一支，代表它被搬進了
# `_DECLARED_PER_VARIANT`——那可能是對的，但必須是有人明確決定的，不是靜靜發生的。
#
# ⚠️ **2026-09-11：它從「取樣」升級成「完整名冊」，因為取樣答不出它自己要答的問題。**
# 上面那段註解已經說對了一半——下限只能抓「崩到 0」，「範圍有沒有縮掉」要靠具名
# canary，因為 `15→12` 跟 `15→18` 在一個 `len()` 上長得一樣。但**同一句話對沒被點名
# 的那幾支同樣成立**：實測（2026-09-11）mirrored 有 8 支而這裡只點了 5 支，把剩下
# 3 支（`_leveldb_manifest_ok`、`_reclaim_dir_sync_residue`、`_session_entry_present`）
# 搬進 `_DECLARED_PER_VARIANT` 之後：mirrored 8→5、下限 `5 >= 4` **照樣通過**、
# `canary ⊆ mirrored` **照樣通過**——兩道都綠，而那三支從此可以任意漂移。
#
# 成因是**只對帳一個方向**：原本只問 `canary - mirrored`（點名的有沒有消失），
# 沒問 `mirrored - canary`（有沒有 mirrored 的沒被點名）。CLAUDE.md 對
# `_OWNER_ONLY_SLASH` 記的是同一個形狀：單向對帳的另一個方向永遠是綠的。
#
# 所以現在的規則是**歸類即涵蓋**：凡是 mirrored 的就必須列在這裡。新增一支兩變體共有
# 的 helper 時要加一行——那一行的價值就是逼人回答「這支該逐字相同，還是該宣告分歧」。
# 要豁免請走既有的機制（`_DECLARED_PER_VARIANT`，它本來就要求寫理由），不要在這裡
# 另開一個沒有理由的例外集合。
_MIRRORED_CANARY = frozenset({
    "_kill_orphan_chrome", "_snapshot_chrome_profile",
    "_sync_chrome_profile_back", "_clear_snapshot_locks",
    "_sync_profile_dir_back",
    # 2026-09-11 補上的三支。它們一直都在被逐字比對，只是沒有被點名——也就是沒有
    # 任何東西擋著它們「靜靜地不再被比對」。
    "_leveldb_manifest_ok",       # profile 快照的完整性判讀
    "_reclaim_dir_sync_residue",  # 同步回寫失敗後的殘留回收
    "_session_entry_present",     # session 還在不在的判讀
})


@pytest.mark.parametrize("variant", _VARIANTS, ids=lambda m: m.__name__)
def test_a_leveldb_current_file_is_trusted_only_when_it_names_a_manifest_beside_it(
        tmp_path, variant):
    """`CURRENT` 是 Chrome 寫的一行檔名，這裡拿它接在 profile 目錄後面問「那份 MANIFEST 在不在」。

    兩道檢查都沒被真的資料擋過：(1) 指的不是 MANIFEST（例如 `LOG`）——那份 DB 打不開，
    不能算完整；(2) 以 `MANIFEST-` 開頭卻帶著 `../`——前綴檢查不是包含性檢查，
    `MANIFEST-x/../../../secret` 會接到 profile **外面**的檔案，而那個檔案存在就讀成
    「完整」。兩種都要回 False（便宜的方向：多登入一次）。最後三格是讀不到的 `CURRENT`。"""
    db = tmp_path / "a" / "b" / "db"
    db.mkdir(parents=True)
    (tmp_path / "a" / "secret").write_text("x", encoding="utf-8")
    (db / "LOG").write_text("x", encoding="utf-8")
    (db / "MANIFEST-000007").write_bytes(b"\x00")
    current = db / "CURRENT"

    def verdict(content):
        if content is None:
            current.unlink(missing_ok=True)
        elif isinstance(content, bytes):
            current.write_bytes(content)
        else:
            current.write_text(content, encoding="utf-8")
        return variant._leveldb_manifest_ok(db)

    assert verdict("MANIFEST-000007\n") is True
    assert verdict("MANIFEST-000008\n") is False, "指到的 MANIFEST 不在"
    assert verdict("LOG\n") is False, "指的不是 MANIFEST"
    escape = "MANIFEST-x/../../../secret"
    assert (db / escape).is_file(), "前提：這個接合真的接得到 profile 外面的檔案"
    assert verdict(escape + "\n") is False
    assert verdict(escape.replace("/", "\\") + "\n") is False
    assert verdict(None) is False
    assert verdict(b"\xff\xfe\x00MANIFEST") is False
    assert verdict("") is False


def _canary_drift(mirrored, canary) -> tuple:
    """`(點名了卻不再比對的, 在比對卻沒被點名的)`。

    抽成純函式是因為**真實資料是乾淨的**：兩個方向都回空集合，所以主測試裡那兩句
    斷言整條刪掉也不會紅，牙齒得長在一個合成語料問得到的地方（本輪在
    `_channel_drift` / `_api_shortfall` / `_misfiled_subsections` 上是同一個作法）。

    ⚠️ **刻意不回傳「受檢總數」，雖然上面那三支都有回傳。** 那三支需要它，是因為它們
    的名冊是被檢集合的**子集**，所以「被檢集合空了」會讓兩個方向同時真空通過。這一支
    不一樣：名冊要求與 `mirrored` **相等**，所以 `mirrored` 空掉的話
    `unwatched` 會一次列出全部 8 個名字、大聲地紅，而且 `_MIRRORED_FLOOR` 在它之前
    就先擋掉了。在這裡加一個下限斷言會是**死儀式**——一個永遠成立、刪掉也沒有變異殺
    得掉的斷言，正是本輪一路在拆的東西。
    """
    unwatched = sorted(n for n in canary if n not in set(mirrored))
    unrostered = sorted(n for n in mirrored if n not in set(canary))
    return unwatched, unrostered


def test_the_canary_drift_comparison_actually_bites():
    """對照組：乾淨資料讓上面兩句斷言都證明不了自己在咬。

    ⚠️ **「在比對卻沒被點名」那一格是這一支存在的真正理由。** 2026-09-11 實測：把三支
    沒被點名的 mirrored 搬進 `_DECLARED_PER_VARIANT`，下限（`5 >= 4`）與單向 canary
    （`canary ⊆ mirrored`）**兩道都通過**。所以「`mirrored - canary` 非空」這件事必須
    有一個語料問得出來，否則主測試裡那句新斷言在真實資料上刪掉也不會紅。
    """
    # 乾淨：兩邊一致。
    assert _canary_drift({"a", "b"}, {"a", "b"}) == ([], []), (
        f"乾淨語料就該是空的：{_canary_drift({'a', 'b'}, {'a', 'b'})}")

    # 點名了卻不再比對（原本就有守住的那個方向）。
    assert _canary_drift({"a"}, {"a", "b"}) == (["b"], [])

    # **在比對卻沒被點名**——2026-09-11 之前完全沒有東西問這個方向。
    assert _canary_drift({"a", "b"}, {"a"}) == ([], ["b"])

    # 兩邊各有一個，訊息要分得開（一句話含兩種病因會讓人只修一半）。
    assert _canary_drift({"a", "x"}, {"a", "y"}) == (["y"], ["x"])

    # mirrored 整個空掉時，`unwatched` 會一次列出全部名字——所以這一支不需要
    # 另外一個「受檢總數」的下限斷言（見 `_canary_drift` 的 docstring）。
    unwatched, unrostered = _canary_drift(set(), {"a", "b"})
    assert (unwatched, unrostered) == (["a", "b"], []), (unwatched, unrostered)


def _module_level_functions(path: Path) -> set:
    return {n.name for n in ast.parse(
        path.read_text(encoding="utf-8"), str(path)).body
        if isinstance(n, ast.FunctionDef)}


def _mirrored_names(novelai: Path, je: Path) -> tuple:
    """`(共有的全部, 應該逐一相同的)`。

    兩個路徑是參數，就是為了讓範圍釘樁餵得進 `tmp_path`——一份寫死的函式名清單
    不可能含有 `_brand_new_recovery_helper` 這種名字（§8.8(A7)）。
    """
    common = _module_level_functions(novelai) & _module_level_functions(je)
    return common, tuple(sorted(common - set(_DECLARED_PER_VARIANT)))


_NOVELAI_ONLY_PREFIX = {"_kill_orphan_chrome": "_SUPPRESS_ORPHAN_SWEEP"}


def _mirrored_bodies(path: Path, mirrored) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in mirrored:
            continue
        body = list(node.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]                      # docstring 不比
        guard = _NOVELAI_ONLY_PREFIX.get(node.name)
        if (guard and body and isinstance(body[0], ast.If)
                and isinstance(body[0].test, ast.Name)
                and body[0].test.id == guard):
            body = body[1:]                      # 已宣告的例外
        # **簽章也要比，不能只比 body。** 第一版只 dump body，於是
        # `_force_unlink(p, *, retries=8)` 在一邊被改成 `retries=9` 完全看不
        # 出來——變異測試當場證明那個版本是綠的。而那不是裝飾性的差異：呼叫端
        # 正是用預設值呼叫的，所以預設值本身就是行為。（`_force_unlink` 已於
        # 2026-09-07 移除，但這條規則對留下來的幾支一樣成立。）
        out[node.name] = ast.dump(node.args, indent=1) + "\n" + ast.dump(
            ast.Module(body=body, type_ignores=[]), indent=1)
    return out


def test_the_recovery_chain_is_identical_in_both_variants():
    """兩份副本必須**逐一 AST 相同**（docstring 除外）。

    這是本檔最強的一支：上面每一條行為守門都跑在兩個變體上，但只有這一支會擋住
    「只改了一邊」。這條鏈一行都沒被執行過，所以漂移原本要等到某天真的切到另一
    個變體、在無人值守的批次裡才會現形。

    用 AST 不用字串比對是刻意的——`getsource()` 連 docstring 一起拿，而這幾支的
    docstring 正好都在解釋要檢查的規則（msedge 要放過、整棵樹那個旗標是刻意
    的），程式碼改壞了字串比對照樣綠。本專案這個月踩過兩次。
    """
    novelai_path = PKG_ROOT / "webrunner_novelai.py"
    je_path = PKG_ROOT / "webrunner_je_only.py"
    common, mirrored = _mirrored_names(novelai_path, je_path)
    # 下限一：共有函式抽不到就等於根本沒在比。
    assert len(common) >= _COMMON_FUNC_FLOOR, (
        f"兩個變體只抽到 {len(common)} 支共有的模組層函式（下限 "
        f"{_COMMON_FUNC_FLOOR}）——抽取器壞了，**或者**有人合法地把一批 helper 搬進"
        "共用層／改了名。先確認是哪一種再動這個數字：如果是後者，連同下面的"
        "`_COMMON_CANARY` 一起更新，不要只把門檻調低（那會把控制組變成裝飾）。")
    # 下限二：**只違反這一道**的語料是「共有夠多、但豁免清單把它們吃光了」，
    # 所以它必須是獨立的一句（§8.8(A4)）。
    assert len(mirrored) >= _MIRRORED_FLOOR, (
        f"扣掉宣告分歧之後只剩 {len(mirrored)} 支要比（下限 {_MIRRORED_FLOOR}）"
        "——`_DECLARED_PER_VARIANT` 是不是變成了倒過來的白名單？"
        "（也可能是有人合法地把幾支搬進共用層，那就連 `_MIRRORED_CANARY` 一起更新。）")
    # 具名 canary：數字答不了「範圍有沒有縮掉」。
    gone = sorted(_COMMON_CANARY - common)
    assert not gone, (
        f"這幾支不再是兩個變體共有的模組層函式了：{gone}。搬走了、改名了、還是"
        "只剩一個變體有？三種都要有人明確決定——改對之後把 `_COMMON_CANARY` "
        f"一起更新。（現在共有的是 {sorted(common)}）")
    unwatched, unrostered = _canary_drift(mirrored, _MIRRORED_CANARY)
    assert not unwatched, (
        f"{unwatched} 不再被逐字比對了——被加進 `_DECLARED_PER_VARIANT` 了嗎？"
        "那可能是對的，但這幾支是這條復原鏈的骨幹，靜靜地不再比對就等於兩個變體"
        "從此可以任意漂移。要放行的話請同時把它從 `_MIRRORED_CANARY` 拿掉並寫理由。")
    # 反方向（2026-09-11 補）。少了這一句，沒被點名的 mirrored 可以被搬進
    # `_DECLARED_PER_VARIANT` 而**兩道都照樣通過**——實測 8→5、下限 `5 >= 4`、
    # `canary ⊆ mirrored` 全綠。單向對帳的另一個方向永遠是綠的。
    assert not unrostered, (
        f"{unrostered} 正在被逐字比對，但沒有列在 `_MIRRORED_CANARY` 裡。"
        "請加進去——**歸類即涵蓋**：沒被點名的那幾支，哪天被搬進 "
        "`_DECLARED_PER_VARIANT` 就會靜靜地不再比對，而下限答不出範圍有沒有縮掉"
        "（`8->5` 仍然大於 4）。如果它**應該**宣告分歧，那就搬進 "
        "`_DECLARED_PER_VARIANT` 並寫下理由，那是既有的豁免機制。")
    a = _mirrored_bodies(novelai_path, mirrored)
    b = _mirrored_bodies(je_path, mirrored)
    missing = [name for name in mirrored if name not in a or name not in b]
    assert not missing, f"有變體少了 {missing}——改名的話這支測試要跟著改。"
    drifted = [name for name in mirrored if a[name] != b[name]]
    assert not drifted, (
        f"兩個變體的 {drifted} 已經漂移。要嘛把改動同步到另一邊，要嘛（如果是"
        f"刻意的）在 _NOVELAI_ONLY_PREFIX 寫明理由。")


def _names_in_code(path: Path) -> set[str]:
    """這個模組的**程式碼**裡出現的識別字（`ast.Name` / `ast.Attribute`）。

    ⚠️ **刻意不用子字串比對。** 2026-09-10 踩到：je 變體的
    `_kill_orphan_chrome` docstring 補了一段「novelai 那份 docstring 的最後一段講的
    是 `_SUPPRESS_ORPHAN_SWEEP`，而**本變體沒有那個旗標**」——一句完全正確、而且正
    是這支測試想確保的話。原本的 `"_SUPPRESS_ORPHAN_SWEEP" not in je` 是對整份原始碼
    文字做的，於是**被那句解釋自己絆倒**，把一個正確的文件更正報成程式漂移。

    這是 `CLAUDE.md` 反覆記過的同一條：說明規則的那句話會命中規則自己的子字串掃描
    （`test_language` 對反引號、`test_doc_symbols` 對自己都開了同樣的例外）。這裡不必
    開例外——問 AST 就好，註解與 docstring 天生不在裡面。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def test_the_declared_variant_exemption_is_still_real():
    """例外清單本身也會過期：`_SUPPRESS_ORPHAN_SWEEP` 哪天不在了，上面那支會
    安靜地退化成「什麼都沒扣掉」的普通比對。"""
    novelai = _names_in_code(PKG_ROOT / "webrunner_novelai.py")
    je = _names_in_code(PKG_ROOT / "webrunner_je_only.py")
    # 正面對照：抽取器真的看得見東西（回空集合時下面兩句都會「通過」）。
    assert len(novelai) >= 200 and len(je) >= 200, (
        f"抽到的識別字太少（novelai {len(novelai)}、je {len(je)}）——抽取器壞了")
    assert "_SUPPRESS_ORPHAN_SWEEP" in novelai, (
        "novelai 變體的**程式碼**裡已經沒有 `_SUPPRESS_ORPHAN_SWEEP` 了——"
        "`_NOVELAI_ONLY_PREFIX` 那筆例外正在扣掉一個不存在的差異。")
    assert "_SUPPRESS_ORPHAN_SWEEP" not in je, (
        "je 變體的程式碼出現了 _SUPPRESS_ORPHAN_SWEEP——如果它現在也有驗證模式，"
        "請把 _NOVELAI_ONLY_PREFIX 那筆例外刪掉，讓兩邊回到完全相同。"
        "（注意：docstring 裡**提到**這個名字不算，這支測試問的是 AST。）")


def test_the_recovery_chain_never_probes_liveness_with_signal_zero():
    """CLAUDE.md 的跨領域硬規則：Windows 上 `os.kill(pid, 0)` 兩個方向都會答錯
    （`signal.CTRL_C_EVENT == 0`，走的是 console group 那條分支）。這條鏈整個是
    在處理「上一輪留下什麼」，最容易長出探活。"""
    for name in ("webrunner_novelai.py", "webrunner_je_only.py"):
        tree = ast.parse((PKG_ROOT / name).read_text(encoding="utf-8"), name)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "kill"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                    and len(node.args) == 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == 0):
                raise AssertionError(
                    f"{name}:{node.lineno} 用 os.kill(pid, 0) 探活。"
                    "Windows 上那是 console group 事件，兩個方向都會答錯——"
                    "改用 psutil.pid_exists（見 CLAUDE.md）。")


def test_the_declared_divergence_list_has_no_stale_entries():
    """豁免清單自己也會過期。

    某支被搬進共用層、或改了名之後，那一筆就變成永遠對不到任何函式的字串——而守門
    照跑、清單還在、全綠。跟 `CLAUDE.md` 記載的 `_OWNER_ONLY_SLASH` 同一個失效
    方式，只是這裡的方向是「少比了一支」而不是「少守了一個指令」。
    """
    common, _ = _mirrored_names(PKG_ROOT / "webrunner_novelai.py",
                                PKG_ROOT / "webrunner_je_only.py")
    stale = sorted(set(_DECLARED_PER_VARIANT) - common)
    assert not stale, (
        f"`_DECLARED_PER_VARIANT` 有過期項目：{stale}。它們已經不是兩個變體共有的"
        "模組層函式了（搬走了？改名了？），請刪掉——留著等於替未來的回歸先開一張"
        "免死金牌。")
    for name, reason in _DECLARED_PER_VARIANT.items():
        assert len(reason) > 20, f"{name} 的分歧理由太短，寫清楚為什麼必須不一樣"


def test_the_divergence_reconciliation_actually_compares(monkeypatch):
    """對照組：清單乾淨時上面那支兩個方向都是空的，把比較刪掉照樣綠。

    ⚠️ 假理由要**夠長**（那支後面還有一句 `len(reason) > 20`），錨點要挑**只有
    對帳那一句才有**的字——否則長度那道會先炸，控制測試被它餵飽而放走真正要抓的
    變異。同一個坑今天已經踩過一次（`test_config_numbers` 的豁免對帳）。
    """
    monkeypatch.setattr(
        sys.modules[__name__], "_DECLARED_PER_VARIANT",
        {"_a_function_that_no_longer_exists":
         "這一筆刻意指向一個不存在的函式，用來證明對帳真的會叫；理由本身寫得夠長，"
         "才不會讓長度那一道先炸掉。"})
    with pytest.raises(AssertionError) as excinfo:
        test_the_declared_divergence_list_has_no_stale_entries()
    assert "有過期項目" in str(excinfo.value), (
        f"紅的不是對帳那一句，而是：{excinfo.value}")
    assert "_a_function_that_no_longer_exists" in str(excinfo.value)


def test_the_mirror_list_is_derived_not_a_hardcoded_set(tmp_path):
    """釘住「要比哪些函式是算出來的」。

    今天寫死的 8 支剛好就是「共有 − 宣告分歧」的答案，所以真實資料上兩種寫法分不
    出來（§8.8(A3)）。唯一分辨得出來的辦法是餵它一個**任何手寫清單都不可能含有**
    的名字——而那正是這道守門要防的情境：有人在兩個變體都加了一支新 helper。
    """
    a = tmp_path / "variant_a.py"
    b = tmp_path / "variant_b.py"
    a.write_text("def _brand_new_recovery_helper():\n    return 1\n"
                 "def _setup_session():\n    return 'a'\n", encoding="utf-8")
    b.write_text("def _brand_new_recovery_helper():\n    return 2\n"
                 "def _setup_session():\n    return 'b'\n", encoding="utf-8")
    common, mirrored = _mirrored_names(a, b)
    assert "_brand_new_recovery_helper" in mirrored, (
        "兩邊都新加的 helper 沒有被納入比對——列舉退回寫死的清單了，而那個方向是"
        "fail-open：忘記登記就永遠不被檢查。")
    assert "_setup_session" not in mirrored, "宣告過的分歧不該被要求相同"
    assert common == {"_brand_new_recovery_helper", "_setup_session"}, common
    # 而且它真的抓得到那個分岔（偵測器不能被 `return {}` 取代）。
    assert _mirrored_bodies(a, mirrored) != _mirrored_bodies(b, mirrored), (
        "兩份不同的實作被判成相同——比對器壞了")


@pytest.mark.parametrize("label, common, declared, fragment", [
    ("共有函式抽不到", {"_only_one"}, {}, "共有的模組層函式"),
    ("豁免清單把共有的吃光了",
     {f"_f{i}" for i in range(20)}, {f"_f{i}": "理由夠長，只是為了控制組" * 2
                                     for i in range(20)},
     "扣掉宣告分歧之後"),
])
def test_each_mirror_floor_fires_on_its_own(monkeypatch, label, common,
                                            declared, fragment):
    """兩道下限**依序**排列，所以各給一份剛好只違反它的語料（§8.8(A4)）。

    只餵「共有的很少」只會讓第一道炸，第二道一次都沒被執行過——把它放寬成 0
    照樣全綠。而且要斷言**是哪一句在叫**，不然第一道的 AssertionError 會把控制
    測試餵飽。
    """
    monkeypatch.setattr(sys.modules[__name__], "_DECLARED_PER_VARIANT", declared)
    monkeypatch.setattr(sys.modules[__name__], "_mirrored_names",
                        lambda a, b: (common,
                                      tuple(sorted(set(common) - set(declared)))))
    with pytest.raises(AssertionError) as excinfo:
        test_the_recovery_chain_is_identical_in_both_variants()
    assert fragment in str(excinfo.value), (
        f"「{label}」紅的不是那一句，而是：{excinfo.value}")


def test_the_body_comparison_still_notices_a_default_value_change(tmp_path):
    """canary：簽章也要比，不能只比 body。

    這是 2026-09-07 實測逃掉過的變異（`_force_unlink(p, *, retries=8)` 的預設值
    在一邊被改成 9），而呼叫端正是用預設值呼叫的，所以預設值本身就是行為。
    """
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("def _f(p, *, retries=8):\n    return p\n", encoding="utf-8")
    b.write_text("def _f(p, *, retries=9):\n    return p\n", encoding="utf-8")
    assert _mirrored_bodies(a, ("_f",)) != _mirrored_bodies(b, ("_f",)), (
        "只比 body 不比簽章——預設值的分岔會完全看不出來")
    # 反方向：一模一樣的兩份必須判成相同，否則守門會亂叫。
    b.write_text("def _f(p, *, retries=8):\n    return p\n", encoding="utf-8")
    assert _mirrored_bodies(a, ("_f",)) == _mirrored_bodies(b, ("_f",))
