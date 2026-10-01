"""`/dorossi abort` 連後端已經起的子孫行程一起砍（擁有者裁定 2026-10-01）。

在這之前 `request_abort()` 只 `kill()` 後端 CLI 那一個行程。Windows 上那是
`TerminateProcess`，不帶走子孫：完整工具模式下後端起的 shell 指令（跑到一半的測試、
提交、長時間的腳本）在「已中止」之後照樣跑完。現在兩支 `request_abort` 共用
`dorossi_backend._dorossi_kill_backend_tree`：先列出後端的子孫、砍後端、再砍子孫。

這個檔案分四段：

1. **真的行程**：起一棵假的後端樹（幾個只會睡覺的直譯器）與一棵不相干的鄰居樹，
   確認中止之後整棵樹都不在、鄰居一個都沒少。這一段在修正前是紅的。
2. **列舉的規則**（假的行程表）：只認這個行程自己起的後端、每一條父子邊都要求
   「子不比父早出生」、自己永遠不在名單上、讀不到出生時間的不碰。
3. **不得往外拋**：列不成、少了行程函式庫、行程在動手那一刻不見或拒絕。
4. **函式庫的前提**：父子對照表還問得到；pid 換人之後函式庫拒絕動手。

第 1 段只砍自己起的直譯器。夾具在收尾時依**指令列**認回自己起的行程才動手，不拿
存下來的 pid 直接砍。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import subprocess
import sys
import textwrap
import time

import psutil
import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord_bot as b  # noqa: E402
import dorossi_backend as db  # noqa: E402

ME = os.getpid()

# 假的後端樹：每一層記下自己的 pid、起下一層、然後睡覺。標準輸出入全部關掉，
# 免得活過測試的行程握著測試執行器的管線。
_TREE_SCRIPT = '''\
import os
import subprocess
import sys
import time

depth, out, width = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
with open(os.path.join(out, f"{depth}_{os.getpid()}.pid"), "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
for _ in range(width if depth > 0 else 0):
    subprocess.Popen([sys.executable, __file__, str(depth - 1), out, str(width)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
time.sleep(120)
'''

_UP_DEADLINE_SEC = 60.0
_GONE_DEADLINE_SEC = 20.0


# --------------------------------------------------------------------------
# 一、真的行程
# --------------------------------------------------------------------------

class _Trees:
    """起假的行程樹，並在收尾時把自己起的全部收掉。"""

    def __init__(self, tmp_path):
        self.script = tmp_path / "fake_backend_tree.py"
        self.script.write_text(_TREE_SCRIPT, encoding="utf-8")
        self.tmp_path = tmp_path
        self.dirs: list = []

    def argv(self, depth: int, width: int = 1) -> tuple:
        out = self.tmp_path / f"tree{len(self.dirs)}"
        out.mkdir()
        self.dirs.append(out)
        return [sys.executable, str(self.script), str(depth), str(out), str(width)], out

    @staticmethod
    def pids(out) -> list[int]:
        return sorted(int(p.read_text(encoding="utf-8")) for p in out.glob("*.pid")
                      if p.read_text(encoding="utf-8").strip())

    def wait_up(self, out, depth: int, width: int = 1) -> list[int]:
        want = sum(width ** level for level in range(depth + 1))
        deadline = time.monotonic() + _UP_DEADLINE_SEC
        while time.monotonic() < deadline:
            got = self.pids(out)
            if len(got) >= want:
                return got
            time.sleep(0.05)
        raise AssertionError(f"假的行程樹沒有起來：{len(self.pids(out))}/{want}")

    def reap(self) -> None:
        marker = str(self.script)
        for out in self.dirs:
            for pid in self.pids(out):
                try:
                    proc = psutil.Process(pid)
                    if marker in proc.cmdline():
                        proc.kill()
                except psutil.Error:
                    continue


@pytest.fixture
def trees(tmp_path):
    made = _Trees(tmp_path)
    try:
        yield made
    finally:
        made.reap()


def _family(pid: int) -> list:
    """`pid` 與它的子孫（函式庫公開的那一支列舉，當作獨立的對照）。"""
    root = psutil.Process(pid)
    return [root] + root.children(recursive=True)


def _still_running(procs) -> list[int]:
    _gone, alive = psutil.wait_procs(procs, timeout=_GONE_DEADLINE_SEC)
    return sorted(p.pid for p in alive)


def _quiet_popen(argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


@pytest.mark.parametrize("state_class", ["_DorossiLoopState", "_DorossiTurnState"])
def test_abort_stops_everything_the_backend_started_and_nothing_else(trees, state_class):
    """這一支在修正前是紅的：後端那一個行程沒了，它起的那幾個照樣活著。

    後端用的是正式路徑上同一種物件（事件迴圈起的子行程）。鄰居樹是同一個父行程
    底下的另一棵——形狀就是「別的對話的後端與它起的東西」——一個都不得少。
    """
    async def scenario():
        argv, out = trees.argv(depth=2)
        backend = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        neighbour_argv, neighbour_out = trees.argv(depth=1)
        neighbour = _quiet_popen(neighbour_argv)
        try:
            recorded = trees.wait_up(out, depth=2)
            trees.wait_up(neighbour_out, depth=1)
            family = _family(backend.pid)
            others = _family(neighbour.pid)
            assert len(family) >= len(recorded) >= 3, (family, recorded)
            assert len(others) >= 2, others

            state = getattr(b, state_class)("424242", "s1")
            state.set_proc(backend)
            state.request_abort()

            assert state.abort is True
            assert _still_running(family) == [], "後端起的行程在中止之後還活著"
            assert [p.pid for p in others if not p.is_running()] == [], (
                "不是那個後端起的行程被砍掉了")
            assert neighbour.poll() is None
            await asyncio.wait_for(backend.wait(), _GONE_DEADLINE_SEC)
        finally:
            neighbour.kill()

    asyncio.run(asyncio.wait_for(scenario(), 150))


def test_a_wide_tree_goes_down_whole(trees):
    """每一層兩個、共三層（七個行程以上）：一次快照就列得完，全部不在。"""
    argv, out = trees.argv(depth=2, width=2)
    backend = _quiet_popen(argv)
    trees.wait_up(out, depth=2, width=2)
    family = _family(backend.pid)
    assert len(family) >= 7, family

    db._dorossi_kill_backend_tree(backend)

    assert _still_running(family) == []


# --------------------------------------------------------------------------
# 二、列舉的規則（假的行程表）
# --------------------------------------------------------------------------

class _FakeProcess:
    def __init__(self, world, pid):
        if pid not in world.born:
            raise world.NoSuchProcess(pid)
        self.pid = pid
        self._world = world

    def create_time(self):
        if self.pid in self._world.unreadable:
            raise self._world.AccessDenied(self.pid)
        return self._world.born[self.pid]

    def kill(self):
        self._world.log.append(("kill", self.pid))
        error = self._world.kill_raises.get(self.pid)
        if error is not None:
            raise error


class _World:
    """行程函式庫的替身：一張 pid→父 pid 的表、每個 pid 的出生時間。

    `log` 記下三種事的先後：拍快照、砍後端、砍某個 pid。`asked` 記下被問過的 pid。
    """

    class Error(Exception):
        pass

    class NoSuchProcess(Error):
        pass

    class AccessDenied(Error):
        pass

    def __init__(self, parents: dict, born: dict):
        self.parents = dict(parents)
        self.born = dict(born)
        self.unreadable: set = set()
        self.kill_raises: dict = {}
        self.snapshot_raises = None
        self.log: list = []
        self.asked: list = []

    def _ppid_map(self):
        self.log.append(("snapshot",))
        if self.snapshot_raises is not None:
            raise self.snapshot_raises
        return dict(self.parents)

    def Process(self, pid):  # noqa: N802  函式庫的名字
        self.asked.append(pid)
        return _FakeProcess(self, pid)

    @property
    def killed(self) -> list[int]:
        return [entry[1] for entry in self.log if entry[0] == "kill"]


class _Backend:
    """後端子行程的替身。"""

    def __init__(self, world, pid=100, returncode=None, kill_raises=None):
        self.pid = pid
        self.returncode = returncode
        self._world = world
        self._kill_raises = kill_raises

    def kill(self):
        self._world.log.append(("backend",))
        if self._kill_raises is not None:
            raise self._kill_raises


@pytest.fixture
def world(monkeypatch):
    def make(parents, born):
        made = _World(parents, born)
        monkeypatch.setitem(sys.modules, "psutil", made)
        return made
    return make


def test_the_tree_is_listed_then_the_backend_is_stopped_then_what_it_started(world):
    """順序是規則：後端一死就再也問不到它的子孫，所以先列；先砍後端再砍子孫，
    免得後端看到指令「結束」就接著起下一個。"""
    w = world({100: ME, 101: 100, 102: 101}, {100: 10.0, 101: 11.0, 102: 12.0})
    db._dorossi_kill_backend_tree(_Backend(w))
    assert w.log == [("snapshot",), ("backend",), ("kill", 101), ("kill", 102)]


def test_a_parent_is_stopped_before_its_own_children(world):
    """順帶釘住邊界：與父行程同一刻出生的（時鐘解析度）算它的子行程。"""
    w = world({100: ME, 101: 100, 102: 101, 103: 100, 104: 103},
              {100: 10.0, 101: 11.0, 102: 12.0, 103: 10.0, 104: 13.0})
    db._dorossi_kill_backend_tree(_Backend(w))
    order = w.killed
    assert sorted(order) == [101, 102, 103, 104]
    assert order.index(101) < order.index(102) and order.index(103) < order.index(104)


def test_an_orphan_whose_dead_parents_pid_was_reused_is_not_a_descendant(world):
    """別的行程的孤兒（它的父行程死了）記著的父 pid，後來被後端的某個子孫拿去用。

    那個孤兒比「現在拿著那個 pid 的行程」早出生，所以不可能是它起的。只跟後端比
    出生時間的寫法會收它（30 ≥ 10）；它底下的行程也不得跟著被收。
    """
    w = world({100: ME, 101: 100, 200: 101, 201: 200},
              {100: 10.0, 101: 50.0, 200: 30.0, 201: 60.0})
    db._dorossi_kill_backend_tree(_Backend(w))
    assert w.killed == [101]


def test_a_process_born_after_the_listing_began_is_not_a_descendant(world):
    """快照拍完到替某個 pid 建立物件之間，那個行程結束、pid 被一個新行程拿走：新行程
    比快照晚出生，所以不收——它底下的也不跟。"""
    later = time.time() + 3600.0
    w = world({100: ME, 101: 100, 102: 100, 103: 101},
              {100: 10.0, 101: later, 102: 11.0, 103: later + 1.0})
    db._dorossi_kill_backend_tree(_Backend(w))
    assert w.killed == [102]


def test_this_process_is_never_listed_even_when_the_map_says_so(world):
    """對照表說「這個行程的父行程是後端的子孫」（這個行程原本的父行程死了、pid 被
    重用），而且出生時間擋不住它（同一刻）——這個行程照樣不在名單上。"""
    w = world({100: ME, 101: 100, ME: 101, 300: ME},
              {100: 10.0, 101: 20.0, ME: 20.0, 300: 25.0})
    db._dorossi_kill_backend_tree(_Backend(w))
    assert w.killed == [101]


def test_a_backend_this_process_did_not_start_is_stopped_alone(world, capsys):
    """pid 對得到一個行程、但那個行程不是這個行程起的：它底下的東西一個都不碰。"""
    w = world({100: 777, 101: 100}, {100: 10.0, 101: 11.0})
    db._dorossi_kill_backend_tree(_Backend(w))
    assert w.log == [("snapshot",), ("backend",)]
    assert "only the backend process itself was stopped" in capsys.readouterr().err


def test_a_backend_that_already_ended_is_left_alone_entirely(world):
    """結束碼已經出來了：那個 pid 可能已經換人，連問都不問。"""
    w = world({100: ME, 101: 100}, {100: 10.0, 101: 11.0})
    db._dorossi_kill_backend_tree(_Backend(w, returncode=1))
    assert w.log == []


def test_a_backend_that_is_already_gone_is_still_asked_to_stop(world, capsys):
    """行程表裡已經沒有它（剛好結束）：不拍快照、不留訊息，砍的那一下照做。"""
    w = world({101: 100}, {101: 11.0})
    db._dorossi_kill_backend_tree(_Backend(w, kill_raises=ProcessLookupError()))
    assert w.log == [("backend",)]
    assert capsys.readouterr().err == ""


def test_a_process_whose_age_cannot_be_read_is_not_touched(world):
    """證明不了它是後端起的就不動它，它底下的也不跟。"""
    w = world({100: ME, 101: 100, 102: 101, 103: 100},
              {100: 10.0, 101: 11.0, 102: 12.0, 103: 11.0})
    w.unreadable = {101}
    db._dorossi_kill_backend_tree(_Backend(w))
    assert w.killed == [103]


@pytest.mark.parametrize("pid", [None, True, 0, -5, "100", 100.0])
def test_a_stand_in_without_a_usable_pid_is_only_asked_to_stop(world, pid, capsys):
    """不是正整數的 pid 連問都不問（0 在這個平台上是一個真的系統行程）。"""
    w = world({100: ME, 101: 100}, {100: 10.0, 101: 11.0})
    db._dorossi_kill_backend_tree(_Backend(w, pid=pid))
    assert w.log == [("backend",)]
    assert w.asked == [], "拿一個不是 pid 的東西去問了行程表"
    assert capsys.readouterr().err == ""


def test_nothing_to_stop_is_a_no_op(world):
    w = world({}, {})
    db._dorossi_kill_backend_tree(None)
    assert w.log == []


# --------------------------------------------------------------------------
# 三、不得往外拋
# --------------------------------------------------------------------------

def test_processes_that_vanish_or_refuse_do_not_stop_the_rest(world, capsys):
    """砍的那一刻已經不在（或 pid 換了人，函式庫拒絕）不算失敗；拒絕存取與其他
    意外算「沒砍掉」，而且都不得讓後面的少砍。"""
    w = world({100: ME, 101: 100, 102: 100, 103: 100, 104: 100},
              {100: 10.0, 101: 11.0, 102: 11.0, 103: 11.0, 104: 11.0})
    w.kill_raises = {101: w.NoSuchProcess(101), 102: w.AccessDenied(102),
                     103: RuntimeError("boom")}
    db._dorossi_kill_backend_tree(_Backend(w))
    assert sorted(w.killed) == [101, 102, 103, 104]
    assert "stopped the backend and 2 of the 4 process(es)" in capsys.readouterr().err


def test_the_report_carries_counts_only(world, capsys):
    """這一行會進 log，而 log 有對話平台那個出口：只有個數，沒有 pid。"""
    w = world({100: ME, 4101: 100, 4102: 4101}, {100: 10.0, 4101: 11.0, 4102: 12.0})
    db._dorossi_kill_backend_tree(_Backend(w))
    err = capsys.readouterr().err
    assert "2 of the 2 process(es)" in err
    assert "4101" not in err and "4102" not in err and "100" not in err


def test_a_backend_with_nothing_under_it_says_nothing(world, capsys):
    w = world({100: ME}, {100: 10.0})
    db._dorossi_kill_backend_tree(_Backend(w))
    assert w.log == [("snapshot",), ("backend",)]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("failure", [RuntimeError("snapshot failed"), OSError(5, "denied"),
                                     AttributeError("_ppid_map")])
def test_a_listing_that_fails_still_stops_the_backend(world, capsys, failure):
    w = world({100: ME, 101: 100}, {100: 10.0, 101: 11.0})
    w.snapshot_raises = failure
    db._dorossi_kill_backend_tree(_Backend(w))
    assert w.log == [("snapshot",), ("backend",)]
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and type(failure).__name__ in err
    assert "only the backend process itself was stopped" in err


def test_without_the_process_library_only_the_backend_is_stopped(monkeypatch, capsys):
    """少了行程函式庫（`import` 失敗）：中止照做，只是砍不到子孫，留一行。"""
    monkeypatch.setitem(sys.modules, "psutil", None)
    log = []

    class _Proc:
        pid, returncode = 100, None

        def kill(self):
            log.append("backend")

    db._dorossi_kill_backend_tree(_Proc())
    assert log == ["backend"]
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "only the backend process itself was stopped" in err


def test_a_backend_that_cannot_be_stopped_does_not_spare_what_it_started(world, capsys):
    """砍後端那一下出了意料之外的錯：不往外拋，子孫照砍，而且留得下痕跡。"""
    w = world({100: ME, 101: 100}, {100: 10.0, 101: 11.0})
    db._dorossi_kill_backend_tree(_Backend(w, kill_raises=PermissionError(5, "denied")))
    assert w.log == [("snapshot",), ("backend",), ("kill", 101)]
    assert "PermissionError" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 四、兩支 `request_abort` 共用同一支
# --------------------------------------------------------------------------

def _request_abort_bodies() -> dict:
    """bot 模組裡每一個自己定義 `request_abort` 的類別 → 那支方法的語法樹。

    從載入的模組推（不剖析整個檔）：之後多一個有 `request_abort` 的狀態類別，它會
    自己出現在這裡。
    """
    found = {}
    for owner in vars(b).values():
        if isinstance(owner, type) and "request_abort" in vars(owner):
            source = textwrap.dedent(inspect.getsource(owner.request_abort))
            found[owner.__name__] = ast.parse(source).body[0]
    return found


def test_both_request_aborts_go_through_the_one_helper():
    """各自再寫一次 `kill()` 就是兩份，改了一邊另一邊不會跟。"""
    bodies = _request_abort_bodies()
    assert sorted(bodies) == ["_DorossiLoopState", "_DorossiTurnState"], sorted(bodies)
    for owner, func in bodies.items():
        calls = [n for n in ast.walk(func) if isinstance(n, ast.Call)]
        names = [n.func.id for n in calls if isinstance(n.func, ast.Name)]
        attrs = [n.func.attr for n in calls if isinstance(n.func, ast.Attribute)]
        assert names == ["_dorossi_kill_backend_tree"], (owner, names)
        assert "kill" not in attrs, f"`{owner}.request_abort` 自己又砍了一次"
    assert b._dorossi_kill_backend_tree is db._dorossi_kill_backend_tree


@pytest.mark.parametrize("state_class", ["_DorossiLoopState", "_DorossiTurnState"])
def test_the_flag_is_up_before_anything_is_stopped(monkeypatch, state_class):
    """旗標與砍後端都在呼叫當下同步完成；旗標先設——後端的讀取迴圈一看到行程沒了
    就會問「是不是被要求中止」。"""
    state = getattr(b, state_class)("424242", "s1")
    proc = object()
    state.set_proc(proc)
    seen = []
    monkeypatch.setattr(b, "_dorossi_kill_backend_tree",
                        lambda given: seen.append((given, state.abort)))
    assert state.request_abort() is None
    assert seen == [(proc, True)]


# --------------------------------------------------------------------------
# 五、函式庫的前提
# --------------------------------------------------------------------------

def test_the_parent_map_still_answers_for_this_process_and_its_child():
    """列舉靠的是函式庫一次拍下的 pid→父 pid 對照表（不是公開介面，所以釘在這裡）。"""
    child = _quiet_popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        parents = psutil._ppid_map()
        assert isinstance(parents, dict) and len(parents) > 10
        assert parents[ME] == os.getppid()
        assert parents[child.pid] == ME
    finally:
        child.kill()
        child.wait(_GONE_DEADLINE_SEC)


def test_the_library_refuses_to_kill_a_pid_that_changed_hands():
    """從列出來到動手之間，那個 pid 可能已經換了一個行程。函式庫的 `kill()` 動手前
    比對（pid, 出生時間），對不上就丟 `NoSuchProcess`——這裡靠的就是這一點。

    做法：對一個活著的行程建立物件，再把物件記的出生時間改成別的（等於「這個物件
    是替先前拿著同一個 pid 的行程建立的」）。
    """
    child = _quiet_popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        stale = psutil.Process(child.pid)
        pid, born = stale._ident
        assert pid == child.pid and isinstance(born, float), stale._ident
        stale._ident = (pid, born - 5.0)
        with pytest.raises(psutil.NoSuchProcess):
            stale.kill()
        assert child.poll() is None, "pid 換人之後函式庫還是動手了"
    finally:
        child.kill()
        child.wait(_GONE_DEADLINE_SEC)
