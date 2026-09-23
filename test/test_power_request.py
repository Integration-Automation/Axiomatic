"""`_power_request`：參考計數的電源要求。

這一檔驗四件事，每一件都有一個「安靜壞掉」的形狀：

* **計數**：第一個 acquire 才向作業系統要、最後一個 release 才放。算錯的症狀是
  「以為還拿著、其實已經放了」（bot 在監督期間被暫停）或「放不掉」——後者無害，
  前者正是這個模組要防的事，而兩者從外面都看不出來。
* **例外與取消**：`with` 區塊裡丟例外、asyncio 取消（`/stop` 就是取消監督者）都要
  放得掉，而且 release 冪等——同一份放兩次不能把別人的份一起放掉。
* **非 Windows 是 no-op**、後端炸掉**永不 raise**：這是加分項，不能讓監督或批次
  起不來。
* **Windows 那條路**：用假的 kernel32 走完整條，不碰真實電源狀態；另外靜態釘住
  `argtypes`／`restype`（64 位元 HANDLE 被截斷之後仍然非零，行為測試看不出來）。
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _power_request as pr  # noqa: E402

SOURCE = Path(pr.__file__)


class _FakeBackend:
    """記錄向「作業系統」要／放了幾次。"""

    def __init__(self, result="power-request", raise_on_acquire=None):
        self.result = result
        self.raise_on_acquire = raise_on_acquire
        self.acquires: list[str] = []
        self.releases = 0

    def acquire(self, reason):
        self.acquires.append(reason)
        if self.raise_on_acquire is not None:
            raise self.raise_on_acquire
        return self.result

    def release(self):
        self.releases += 1


def _manager(backend):
    return pr._Manager(backend_factory=lambda: backend)


# ---------------------------------------------------------------------------
# 計數
# ---------------------------------------------------------------------------

def test_only_the_first_acquire_and_the_last_release_reach_the_os():
    backend = _FakeBackend()
    manager = _manager(backend)
    first = manager.acquire("batch supervision")
    second = manager.acquire("dorossi turn")
    assert backend.acquires == ["batch supervision"], backend.acquires
    assert manager.count == 2 and manager.active == "power-request"
    assert first.active == second.active == "power-request"
    first.release()
    assert backend.releases == 0, "還有一份拿著就放掉了——另一個理由會在待命時被暫停"
    assert manager.active == "power-request"
    second.release()
    assert backend.releases == 1
    assert manager.count == 0 and manager.active is None


def test_releasing_the_same_hold_twice_does_not_release_someone_elses():
    """`finally` 裡多叫一次 release 是常態；它不能吃掉另一份。"""
    backend = _FakeBackend()
    manager = _manager(backend)
    mine = manager.acquire("a")
    theirs = manager.acquire("b")
    mine.release()
    mine.release()
    mine.release()
    assert manager.count == 1 and backend.releases == 0
    assert manager.reasons() == ["b"]
    theirs.release()
    assert backend.releases == 1


def test_releasing_a_later_hold_removes_that_one_not_the_first():
    """放掉的不是第一筆時，走訪要走過前面那些——這條路在整個套件裡從來沒跑過
    （2026-09-22 分支覆蓋率：上面幾支放的都是第一筆）。刪錯一筆的後果是實打實的：
    批次還在跑，防待命的請求卻被提早放掉，主機就睡了。"""
    backend = _FakeBackend()
    manager = _manager(backend)
    first = manager.acquire("first")
    second = manager.acquire("second")
    third = manager.acquire("third")

    second.release()
    assert manager.reasons() == ["first", "third"], manager.reasons()
    assert backend.releases == 0

    third.release()
    first.release()
    assert manager.count == 0 and backend.releases == 1


def test_two_holds_with_the_same_reason_are_counted_separately():
    backend = _FakeBackend()
    manager = _manager(backend)
    one = manager.acquire("same")
    two = manager.acquire("same")
    one.release()
    assert manager.count == 1 and backend.releases == 0
    two.release()
    assert backend.releases == 1


def test_it_asks_again_after_everything_was_released():
    backend = _FakeBackend()
    manager = _manager(backend)
    manager.acquire("x").release()
    manager.acquire("y").release()
    assert backend.acquires == ["x", "y"] and backend.releases == 2


def test_a_failed_request_is_retried_by_the_next_acquire_and_still_balances():
    """拿不到（回 None）也要記帳；下一個 acquire 再試一次。"""
    backend = _FakeBackend(result=None)
    manager = _manager(backend)
    first = manager.acquire("a")
    assert first.active is None and manager.count == 1
    backend.result = "execution-state"
    second = manager.acquire("b")
    assert second.active == "execution-state"
    assert backend.acquires == ["a", "b"]
    first.release()
    second.release()
    assert manager.count == 0 and backend.releases == 1


def test_a_backend_that_raises_never_propagates():
    backend = _FakeBackend(raise_on_acquire=OSError("boom"))
    manager = _manager(backend)
    hold = manager.acquire("a")
    assert hold.active is None and manager.count == 1
    hold.release()
    assert manager.count == 0


def test_a_backend_whose_release_raises_never_propagates():
    backend = _FakeBackend()

    def _boom():
        raise OSError("release failed")

    backend.release = _boom
    manager = _manager(backend)
    manager.acquire("a").release()          # 不得往外丟
    assert manager.count == 0 and manager.active is None


def test_concurrent_acquire_and_release_from_threads_balance():
    backend = _FakeBackend()
    manager = _manager(backend)
    barrier = threading.Barrier(8)

    def _worker():
        barrier.wait()
        for _ in range(200):
            manager.acquire("t").release()

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert manager.count == 0 and manager.active is None
    assert len(backend.acquires) == backend.releases, (
        backend.acquires[:3], backend.releases)


# ---------------------------------------------------------------------------
# with／例外／取消
# ---------------------------------------------------------------------------

def test_the_hold_releases_when_the_block_raises(monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(pr, "_MANAGER", _manager(backend))
    with pytest.raises(RuntimeError):
        with pr.hold("x"):
            assert pr.status()["count"] == 1
            raise RuntimeError("boom")
    assert pr.status() == {"count": 0, "active": None, "reasons": []}
    assert backend.releases == 1


def test_a_power_hold_object_is_itself_a_context_manager(monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(pr, "_MANAGER", _manager(backend))
    with pytest.raises(ValueError):
        with pr.acquire("x") as held:
            assert held.active == "power-request"
            raise ValueError
    assert pr.status()["count"] == 0 and backend.releases == 1


def test_the_hold_releases_when_the_task_is_cancelled(monkeypatch):
    """`/stop` 就是取消監督者 task——那一刻的 `CancelledError` 要把電源要求放掉。"""
    backend = _FakeBackend()
    monkeypatch.setattr(pr, "_MANAGER", _manager(backend))
    entered = asyncio.Event()

    async def _supervise():
        with pr.hold("batch supervision"):
            entered.set()
            await asyncio.sleep(3600)

    async def _body():
        task = asyncio.create_task(_supervise())
        await entered.wait()
        assert pr.status()["count"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_body())
    assert pr.status()["count"] == 0 and backend.releases == 1


def test_a_disabled_hold_touches_nothing(monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(pr, "_MANAGER", _manager(backend))
    with pr.hold("x", enabled=False) as held:
        assert held.active is None
        assert pr.status()["count"] == 0
    inert = pr.inert("y")
    inert.release()
    inert.release()
    assert backend.acquires == [] and backend.releases == 0


# ---------------------------------------------------------------------------
# 平台
# ---------------------------------------------------------------------------

def test_off_windows_it_is_a_no_op_and_never_loads_the_os_library():
    loaded = []
    with mock.patch.object(pr.os, "name", "posix"), \
            mock.patch.object(pr, "_load_kernel32",
                              lambda: loaded.append(1)):
        manager = pr._Manager()
        hold = manager.acquire("x")
        assert hold.active is None and manager.active is None
        hold.release()
    assert loaded == [], "非 Windows 不該去載入 kernel32"


class _FakeFunc:
    def __init__(self, result, calls, name):
        self.result = result
        self.calls = calls
        self.name = name
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append((self.name, args))
        return self.result() if callable(self.result) else self.result


class _FakeKernel32:
    """夠用的假 kernel32：每一支函式記下被怎麼呼叫。"""

    def __init__(self, *, create=0x1234, set_ok=True, es_ok=0x80000000):
        self.calls: list = []
        self.PowerCreateRequest = _FakeFunc(create, self.calls, "create")
        self.PowerSetRequest = _FakeFunc(set_ok, self.calls, "set")
        self.PowerClearRequest = _FakeFunc(True, self.calls, "clear")
        self.CloseHandle = _FakeFunc(True, self.calls, "close")
        self.SetThreadExecutionState = _FakeFunc(es_ok, self.calls, "es")


def _windows_backend_with(kernel32, monkeypatch):
    monkeypatch.setattr(pr.os, "name", "nt")
    monkeypatch.setattr(pr, "_load_kernel32", lambda: kernel32)
    return pr._WindowsBackend()


@pytest.mark.skipif(os.name != "nt", reason="ctypes.wintypes 只在 Windows 上完整")
def test_the_windows_path_uses_an_execution_required_power_request(monkeypatch):
    kernel32 = _FakeKernel32()
    backend = _windows_backend_with(kernel32, monkeypatch)
    assert backend.acquire("axiomatic bot: batch supervision") == "power-request"
    names = [name for name, _ in kernel32.calls]
    assert names == ["create", "set"], names
    _, (handle, request_type) = kernel32.calls[1]
    assert handle == 0x1234
    assert request_type == pr._POWER_REQUEST_EXECUTION_REQUIRED == 3
    backend.release()
    assert [name for name, _ in kernel32.calls][2:] == ["clear", "close"]
    backend.release()                        # 冪等
    assert len(kernel32.calls) == 4


@pytest.mark.skipif(os.name != "nt", reason="ctypes.wintypes 只在 Windows 上完整")
def test_a_refused_request_falls_back_to_the_execution_state_flag(monkeypatch):
    kernel32 = _FakeKernel32(set_ok=False)
    backend = _windows_backend_with(kernel32, monkeypatch)
    assert backend.acquire("x") == "execution-state"
    names = [name for name, _ in kernel32.calls]
    assert names == ["create", "set", "close", "es"], names
    assert kernel32.calls[-1][1] == (pr._ES_CONTINUOUS | pr._ES_SYSTEM_REQUIRED,)
    backend.release()
    assert kernel32.calls[-1] == ("es", (pr._ES_CONTINUOUS,))


@pytest.mark.skipif(os.name != "nt", reason="ctypes.wintypes 只在 Windows 上完整")
def test_when_both_mechanisms_refuse_nothing_is_reported_as_held(monkeypatch):
    """備援的 `SetThreadExecutionState` 失敗時回 0。不看回傳值的話，`acquire` 會回報
    拿到了、呼叫端就以為主機不會睡，而 `release` 還會去清一個從來沒設上的旗標。"""
    kernel32 = _FakeKernel32(set_ok=False, es_ok=0)
    backend = _windows_backend_with(kernel32, monkeypatch)
    assert backend.acquire("x") is None
    assert backend.active is None
    backend.release()
    assert [name for name, _ in kernel32.calls] == ["create", "set", "close", "es"]


@pytest.mark.skipif(os.name != "nt", reason="ctypes.wintypes 只在 Windows 上完整")
def test_an_invalid_handle_is_not_mistaken_for_a_request(monkeypatch):
    """INVALID_HANDLE_VALUE 是 -1（非零）——只檢查 falsy 會把它當成成功。"""
    from ctypes import wintypes
    kernel32 = _FakeKernel32(create=wintypes.HANDLE(-1).value)
    backend = _windows_backend_with(kernel32, monkeypatch)
    assert backend.acquire("x") == "execution-state"
    assert "set" not in [name for name, _ in kernel32.calls]


def test_a_loader_that_raises_leaves_nothing_held(monkeypatch):
    monkeypatch.setattr(pr.os, "name", "nt")

    def _boom():
        raise OSError("no kernel32 here")

    monkeypatch.setattr(pr, "_load_kernel32", _boom)
    backend = pr._WindowsBackend()
    assert backend.acquire("x") is None
    backend.release()


# ---------------------------------------------------------------------------
# 靜態：型別要釘、螢幕不要點亮
# ---------------------------------------------------------------------------

def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "_WindowsBackend":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError(f"找不到 `_WindowsBackend.{name}`")


def test_the_power_request_handle_is_not_truncated():
    """`argtypes`／`restype` 一定要寫：64 位元 HANDLE 被預設的 c_int 截斷後仍然非零。

    行為測試看不到這一格——這台機器的 handle 值小到截斷後數值不變。
    """
    assigned = {ast.unparse(n.targets[0]) for n in ast.walk(_function("acquire"))
                if isinstance(n, ast.Assign) and len(n.targets) == 1}
    for required in ("kernel32.PowerCreateRequest.argtypes",
                     "kernel32.PowerCreateRequest.restype",
                     "kernel32.PowerSetRequest.argtypes",
                     "kernel32.CloseHandle.argtypes",
                     "kernel32.SetThreadExecutionState.argtypes",
                     "kernel32.SetThreadExecutionState.restype"):
        assert required in assigned, f"`acquire` 少了 `{required}`"
    released = {ast.unparse(n.targets[0]) for n in ast.walk(_function("release"))
                if isinstance(n, ast.Assign) and len(n.targets) == 1}
    for required in ("kernel32.PowerClearRequest.argtypes",
                     "kernel32.CloseHandle.argtypes"):
        assert required in released, f"`release` 少了 `{required}`"


def test_it_never_asks_to_keep_the_display_on():
    names = {n.targets[0].id for n in ast.walk(ast.parse(
        SOURCE.read_text(encoding="utf-8")))
        if isinstance(n, ast.Assign) and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)}
    for banned in ("_ES_DISPLAY_REQUIRED", "_POWER_REQUEST_DISPLAY_REQUIRED"):
        assert banned not in names


def test_the_module_imports_nothing_from_the_project():
    """被動共用模組：純 stdlib。多一條專案 import 就可能把 bot 或批次的閉包拖大。"""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "contextlib", "ctypes", "os", "sys",
                        "threading"}, imported


def test_the_notes_keep_the_two_caveats_that_are_easy_to_lose():
    """保的是**行程**（不是系統不待命）、DC 電源下 5 分鐘會被撤銷——重寫說明時最容易
    被順手刪掉的兩句，少了任何一句，讀的人會退回「它擋得住待命」的錯誤理解。"""
    doc = ast.get_docstring(ast.parse(SOURCE.read_text(encoding="utf-8"))) or ""
    for needed in ("PLM", "DC", "5 分鐘", "子行程"):
        assert needed in doc, f"模組說明少了「{needed}」"
    assert "擋得住 Modern Standby" not in doc
