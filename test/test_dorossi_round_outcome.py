"""一輪的結果必須由**後端**決定，不得由我們為了照顧那一輪而做的動作決定。

`_dorossi_via_claude_code` 與 `_dorossi_via_codex` 在 spawn 前後做了一串**輔助
動作**：把行程交給呼叫端登記（`on_proc`）、問一次 abort 有沒有落地
（`abort_check`）、把 prompt 寫進 stdin、以及看門狗開火時 `proc.kill()`。這些動作
沒有一個是「這一輪的答案」的一部分——它們全都只是為了照顧那一輪。所以規則是：

    **這些動作自己出事，不得變成這一輪的結果。**

這一份就是那條規則的語料。2026-09-20 量到的起點是：那 29 行（兩支後端合計 36 行）
**一行都沒有被執行過**——所有 `try/except: pass` 的 except 半邊、四個看門狗
`proc.kill()` 的失敗分支、以及迴圈頂端那道硬上限預檢，全部是零。整套 7800 支測試
裡沒有任何一支傳過 `on_proc` 或 `abort_check`，而那兩個正是**自走迴圈每一輪**都會
傳的東西。

## 為什麼 `except ProcessLookupError` 在這裡**不是**死碼（2026-09-20 實測）

`CLAUDE.md` 的「Windows PID liveness」那一節寫著「`ProcessLookupError` is
essentially never raised on Windows, so any `except ProcessLookupError` branch is
dead code there」。**那句話的範圍是 `os.kill(pid, 0)`**——以訊號做存活探測的寫法。
`asyncio` 的子行程走的是完全不同的一條路：

    # asyncio/base_subprocess.py
    def _check_proc(self):
        if self._proc is None:
            raise ProcessLookupError()

`kill()` 一開頭就呼叫它，而 `_proc` 會在 `_call_connection_lost` 裡被設成 `None`。
所以「子行程已經結束、connection_lost 已經跑過」之後再 `kill()`，**在每一個平台
（含這台 Windows 主機）都會丟 `ProcessLookupError`**，而且與訊號無關。本機實測
（Windows 11 / CPython 3.14.4）：起一個 `asyncio` 子行程、等它結束、連續 `kill()`
兩次，兩次都拿到 `ProcessLookupError()`，`transport._proc is None` 為 True。
`test_an_asyncio_kill_really_raises_processlookuperror_after_the_child_exits`
把這個前提釘成一支會動的測試——下面所有「砍不到」的假物件都是照它做的，前提若在
某個 CPython 版本改掉，紅的是那一支而不是一堆看不懂的斷言。

而這正是看門狗的常態競態：逾時開火的那一瞬間子行程剛好自己走完。少了那個
`except`，一個良性的競態會變成一個**非型別化**的例外逸出整支函式，呼叫端拿到的就
不是 `TimeoutError`／`_DorossiLoopSilence`，於是 resume 重試、沉默重試、abort 收尾
全部分類不到。

## 兩個後端共用同一份語料

`_dorossi_via_codex` 的同一段有一模一樣的形狀，連註解都寫著「與
_dorossi_via_claude_code 對齊」。兩邊各自綠不等於兩邊一致——所以輔助動作那一族
**只有一份表**，跑在兩個後端上。
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import sys

from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import dorossi_backend as db  # noqa: E402

# 平台替身**只有一份**：`_FakeProc.wait()`「等管線 EOF、`returncode` 卻早就設好」的
# 語意來自 2026-09-09 的實測，抄一份到這裡就等著兩份分岔（同檔案 §8.10 的學費）。
# `_isolate` 也一起取用——它把用量帳本、收尾上限、工作目錄全部導去 tmp，是這兩支
# 後端函式能安全執行的前提，重寫一份只會多一個會漂的副本。
from test_dorossi_teardown import (  # noqa: E402
    FAST_HARD,
    FAST_SILENCE,
    _FakeProc,
    _LingeringStdout,
    _NeverStream,
    _RESULT_LINE,
    _bg_line,
    _fast_watchdog,
    _install,
    _isolate,  # noqa: F401  (autouse fixture，靠 import 進本模組命名空間生效)
    _run,
)

PKG_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "axiomatic"

# codex 那一側的兩行：thread id 與答案。格式抄自 `_CodexStreamState.feed` 認得的
# 事件型別，不是抄自某次真的輸出——這兩個欄位名就是它的契約。
_CODEX_THREAD = json.dumps(
    {"type": "thread.started", "thread_id": "thr-1"}).encode("utf-8") + b"\n"
_CODEX_ANSWER = json.dumps(
    {"type": "item.completed",
     "item": {"type": "agent_message", "text": "答案"}}).encode("utf-8") + b"\n"

# 自走迴圈永遠是 resume 的那一輪，所以 abort 那一族一律帶著 session id 叫用——
# 判定最後那條 `_DorossiResumeError` 只在 `session_id` 有值時才成立。
_RESUMED_ID = "sess-abc"

# 一行不是 result 的普通串流事件：拿來餵「一直在講話」的那一支。
_NOISE_LINE = json.dumps(
    {"type": "system", "subtype": "noise", "session_id": "sess-abc"}
).encode("utf-8") + b"\n"


# ---------------------------------------------------------------------------
# 替身：把 `_FakeProc` 補上這一份需要的兩件事
# ---------------------------------------------------------------------------
class _ClosedStderr:
    """stderr 立刻 EOF。

    `_FakeProc` 預設給的是「孫行程握著寫端」的 `_NeverStream`，那是收尾那一份要量的
    東西；這一份量的是別的事，每支測試多付一個 `_dorossi_drain_stderr` 的逾時只會
    讓紅訊息變得難讀。
    """

    def __init__(self) -> None:
        self.read_started = 0

    async def read(self) -> bytes:
        self.read_started += 1
        return b""


class _DeadOnKill(_FakeProc):
    """`kill()` → stdout 立刻 EOF、`returncode` 立刻可讀、管線也關上。

    也就是「行程真的被帶走了」的樣子。`kill_raises` 給值時**先把上述狀態做完再丟**
    ——`ProcessLookupError` 的意思正是「它早就不在了」，所以它不會讓 EOF 不來；把
    順序寫反的話，測到的會是一個真實世界裡不存在的行程。
    """

    def __init__(self, lines=(), *, kill_raises=None, exited_rc=None):
        super().__init__(exited_rc=exited_rc)
        self.stdout = _LingeringStdout(lines)
        self.stderr = _ClosedStderr()
        self.kill_raises = kill_raises

    def kill(self) -> None:
        self.kills += 1
        cut = getattr(self.stdout, "cut", None)
        if cut is not None:                 # 有些 stdout 替身不靠 kill 來 EOF
            cut.set()
        if self.returncode is None:
            self.returncode = 1
        self._pipes_closed.set()
        if self.kill_raises is not None:
            raise self.kill_raises


class _ChattyStdout:
    """每次 `readline` 都**同步**返回一行（整支 coroutine 一次 await 都沒有）。

    這是「子行程一直在噴輸出、資料永遠已經在緩衝區裡」在 `StreamReader` 這一側的
    樣子，而它同步這件事正是重點：`asyncio.wait_for` 的逾時回呼要等一次 event-loop
    迭代才有機會跑，一個不 await 就返回的 coroutine 根本不給它那個機會。本機實測
    （CPython 3.14.4）：`wait_for(同步返回的 coroutine, timeout=1e-9)` **返回值**，
    而同一個 timeout 餵一個讓出過一次的 coroutine 就是 `TimeoutError`。

    `budget_sec` 之後改吐 EOF，純粹是為了讓**壞掉的**實作快點結束——否則守門一被
    拿掉，這支就從「紅」變成「永遠不返回」，而卡住的測試比紅的測試更糟。
    """

    def __init__(self, line: bytes, *, budget_sec: float):
        self._line = line
        self._budget = budget_sec
        self._t0 = None
        self.served = 0
        self.cut = asyncio.Event()      # 被 kill 之後就 EOF（與 `_LingeringStdout` 同介面）

    async def readline(self) -> bytes:
        import time as _time
        if self.cut.is_set():
            return b""
        if self._t0 is None:
            self._t0 = _time.monotonic()
        if _time.monotonic() - self._t0 >= self._budget:
            return b""
        self.served += 1
        return self._line


def _count_waits(proc):
    """數 `proc.wait()` 被起過幾次。

    這是「收尾那一刀有沒有在**同步**那一步就砍掉行程」唯一乾淨的訊號：砍成功的話
    `returncode` 當場就有值，`_dorossi_reap_proc` 開頭那行 `if proc.returncode is
    not None: return` 會立刻返回、連 wait 都不會起；沒砍的話它得走「先等一個上限、
    再補刀、再等一個上限」那條路。用計數而不是碼錶，是因為時間差只有一個 reap 上限，
    寫成牆鐘比較就是一支等著壞掉的測試。
    """
    original = proc.wait
    proc.wait_started = 0

    async def _wait():
        proc.wait_started += 1
        return await original()

    proc.wait = _wait
    return proc


def _make_kill_fail(proc, exc):
    """讓一個假行程的 `kill()` 在**做完該做的事之後**丟例外。

    順序是重點：`ProcessLookupError` 的意思是「它早就不在了」，所以 returncode 照樣
    要出現；先丟再做的話，測到的是一個真實世界裡不存在的行程。
    """
    if exc is None:
        return proc
    original = proc.kill

    def _kill():
        original()
        raise exc

    proc.kill = _kill
    return proc


def _proc(lines=(), *, exited_rc=0) -> _FakeProc:
    """答完就 EOF、rc 問得到、stderr 立刻 EOF 的乖行程（正常路徑用）。"""
    proc = _FakeProc(stdout_lines=lines, exited_rc=exited_rc)
    proc.stderr = _ClosedStderr()
    proc._pipes_closed.set()
    return proc


# ---------------------------------------------------------------------------
# 兩個後端：同一份語料要能跑在兩邊
# ---------------------------------------------------------------------------
def _prepare_claude(monkeypatch, proc):
    _install(monkeypatch, proc)


def _prepare_codex(monkeypatch, proc):
    monkeypatch.setattr(db, "find_codex_executable", lambda: r"C:\fake\codex.exe")
    monkeypatch.setattr(db, "_collect_codex_images", lambda *_a, **_k: [])
    _install(monkeypatch, proc)


class _Backend:
    """一個後端在這份語料裡需要的四件事：怎麼準備、怎麼叫用、正常路徑餵什麼、
    正常路徑該回什麼。"""

    def __init__(self, name, prepare, invoke, lines, expected):
        self.name = name
        self.prepare = prepare
        self.invoke = invoke
        self.lines = lines
        self.expected = expected

    def __repr__(self) -> str:      # pytest 的 id
        return self.name


_BACKENDS = (
    _Backend("claude_code", _prepare_claude,
             lambda sid=None, **kw: db._dorossi_via_claude_code("問題", sid, **kw),
             [_RESULT_LINE], ("答案", "sess-abc")),
    _Backend("codex", _prepare_codex,
             lambda sid=None, **kw: db._dorossi_via_codex("問題", sid, **kw),
             [_CODEX_THREAD, _CODEX_ANSWER], ("答案", "thr-1")),
)


# ---------------------------------------------------------------------------
# 一、輔助動作自己出事 → 一輪的結果不變
# ---------------------------------------------------------------------------
def _fault_on_proc(proc, kwargs):
    fired = []

    def _cb(live_proc):
        fired.append(live_proc)
        raise RuntimeError("呼叫端的登記回呼壞了")

    kwargs["on_proc"] = _cb
    return fired


def _fault_abort_probe(proc, kwargs):
    fired = []

    def _probe():
        fired.append("abort_check")
        raise RuntimeError("abort 探針壞了")

    kwargs["abort_check"] = _probe
    return fired


def _fault_abort_probe_lookup(proc, kwargs):
    """探針丟 `ProcessLookupError`——它排在 `except Exception` **前面**，所以是一條
    獨立的路；兩條都要吞掉。"""
    fired = []

    def _probe():
        fired.append("abort_check")
        raise ProcessLookupError(3, "No such process")

    kwargs["abort_check"] = _probe
    return fired


def _fault_stdin_write(proc, kwargs):
    fired = []

    def _write(_data):
        fired.append("write")
        raise BrokenPipeError(32, "Broken pipe")

    proc.stdin.write = _write
    return fired


def _fault_stdin_drain(proc, kwargs):
    fired = []

    async def _drain():
        fired.append("drain")
        raise ConnectionResetError(104, "Connection reset by peer")

    proc.stdin.drain = _drain
    return fired


def _fault_stdin_close(proc, kwargs):
    fired = []

    def _close():
        fired.append("close")
        raise OSError(22, "Invalid argument")

    proc.stdin.close = _close
    return fired


# 每一筆都是「我們自己為了照顧這一輪而做的一個動作」。**沒有一筆是後端在講話**，
# 所以沒有一筆有資格改變這一輪的結果。
_AUX_FAULTS = (
    ("沒有注入（控制組）", None),
    ("on_proc 回呼丟例外", _fault_on_proc),
    ("abort 探針丟例外", _fault_abort_probe),
    ("abort 探針丟 ProcessLookupError", _fault_abort_probe_lookup),
    ("stdin.write 丟 BrokenPipeError", _fault_stdin_write),
    ("stdin.drain 丟 ConnectionResetError", _fault_stdin_drain),
    ("stdin.close 丟 OSError", _fault_stdin_close),
)


@pytest.mark.parametrize("backend", _BACKENDS, ids=lambda b: b.name)
@pytest.mark.parametrize("case_id,install", _AUX_FAULTS,
                         ids=[c for c, _ in _AUX_FAULTS])
def test_the_round_ends_the_same_way_whatever_the_helpers_did(
        monkeypatch, backend, case_id, install):
    """輔助動作丟例外 → 這一輪的答案與 session id **一字不差**跟沒出事時一樣。

    控制組（不注入）那一列同時是這張表的正面對照：它若失敗，代表下面每一列的
    「跟正常一樣」比的是一個錯的基準，整張表就只是在確認一件本來就成立的事。

    `fired` 那個斷言也不是裝飾：注入了卻從來沒被呼叫到，這一列會**綠得跟通過一樣**
    ——空的選擇看起來就像乾淨的結果。
    """
    proc = _proc(backend.lines)
    kwargs: dict = {}
    fired = install(proc, kwargs) if install is not None else None
    backend.prepare(monkeypatch, proc)

    answer, sid, _info = _run(backend.invoke(**kwargs))

    assert (answer, sid) == backend.expected, (
        f"{backend.name}／{case_id}：輔助動作出事改變了這一輪的結果 "
        f"{(answer, sid)!r}，應為 {backend.expected!r}")
    if install is not None:
        assert fired, f"{case_id} 根本沒被呼叫到——這一列什麼都沒測到"


# ---------------------------------------------------------------------------
# 二、abort 落在「呼叫端最後一次檢查」與「spawn」之間
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend", _BACKENDS, ids=lambda b: b.name)
def test_an_abort_that_lands_at_spawn_stops_the_round_before_it_runs(
        monkeypatch, backend):
    """abort 已經落地 → 行程當場砍掉，一輪無人值守的工作不會整輪跑完。

    釘住的不只是「有沒有 kill」，還有**呼叫端讀得到什麼**：被砍掉的行程 rc 非零，
    而自走迴圈一律帶著 session id resume，所以判定會走到最後那條
    `_DorossiResumeError`。`discord_bot._dorossi_loop_one_round` 正是靠這個型別
    ＋自己的 abort 旗標決定「不要重試、不要再 spawn 一個新行程跟 abort 競賽」；
    型別變了那段 except 就接不到，abort 之後反而會多跑一整輪。
    """
    proc = _DeadOnKill([])
    backend.prepare(monkeypatch, proc)

    async def _call():
        try:
            await backend.invoke(sid=_RESUMED_ID, abort_check=lambda: True)
        except db._DorossiResumeError as error:
            return error
        return None

    got = _run(_call())
    assert isinstance(got, db._DorossiResumeError), (
        f"{backend.name}：abort 落地之後這一輪居然正常跑完了：{got!r}")
    assert proc.kills >= 1, f"{backend.name}：abort 落地了卻沒有砍掉行程"


@pytest.mark.parametrize("backend", _BACKENDS, ids=lambda b: b.name)
def test_an_abort_that_has_not_landed_leaves_the_round_alone(monkeypatch, backend):
    """近似反例：探針回 False 就**不准**動這一輪。

    沒有這一列的話，把 `if abort_check():` 改成 `if True:` 會活下來——而那會讓每
    一輪自走工作在 spawn 之後立刻自殺。
    """
    proc = _proc(backend.lines)
    seen = []
    backend.prepare(monkeypatch, proc)

    answer, sid, _info = _run(
        backend.invoke(abort_check=lambda: seen.append("asked") or False))

    assert (answer, sid) == backend.expected, (answer, sid)
    assert seen == ["asked"], f"{backend.name}：abort 探針沒有被問到：{seen}"
    assert proc.kills == 0, f"{backend.name}：沒有 abort 卻砍了行程"


@pytest.mark.parametrize("backend", _BACKENDS, ids=lambda b: b.name)
def test_an_abort_kill_that_finds_the_process_already_gone_still_stops_it(
        monkeypatch, backend):
    """abort 命中、`kill()` 丟 `ProcessLookupError`（行程在我們動手前就走了）。

    這是 `asyncio` 上的常態競態（見本檔開頭），不是 Windows 上的死碼。少了那個
    `except`，一個良性的競態會讓 `ProcessLookupError` 逸出整支函式：呼叫端拿到的
    不是 `_DorossiResumeError`，abort 收尾那條 except 就接不到。
    """
    proc = _DeadOnKill([], kill_raises=ProcessLookupError(3, "No such process"))
    backend.prepare(monkeypatch, proc)

    async def _call():
        try:
            await backend.invoke(sid=_RESUMED_ID, abort_check=lambda: True)
        except db._DorossiResumeError as error:
            return error
        except ProcessLookupError as error:
            return error
        return None

    got = _run(_call())
    assert isinstance(got, db._DorossiResumeError), (
        f"{backend.name}：kill 失敗換掉了這一輪的結果：{got!r}")
    assert proc.kills >= 1


# ---------------------------------------------------------------------------
# 三、看門狗砍不到行程時，不得換一種死法
#
# 三種砍法（自走沉默／硬上限／閒置）各自只接 `ProcessLookupError`，接不到的例外會
# 逸出到外層的 `except BaseException`，在那裡再 kill 一次（同樣失敗）、吞掉、然後
# `raise` **原本那個**例外——於是呼叫端拿到的是一個非型別化的例外，三條分類路
# （沉默重試／硬上限逾時／閒置逾時）一條都走不到。
# ---------------------------------------------------------------------------
def _watchdog_case_silence(monkeypatch):
    monkeypatch.setattr(db, "_dorossi_cc_hard_limit_sec", lambda: FAST_HARD)
    return [], {"silence_limit": FAST_SILENCE}, db._DorossiLoopSilence, "no output"


def _watchdog_case_hard(monkeypatch):
    """背景工作把閒置那一層壓住，一路撐到硬上限才被砍。"""
    _fast_watchdog(monkeypatch)
    return [_bg_line("b1")], {}, TimeoutError, "hard wall-clock"


def _watchdog_case_idle(monkeypatch):
    """沒有輸出、沒有工具在跑 → 閒置監看開火。"""
    _fast_watchdog(monkeypatch)
    return [], {}, TimeoutError, "idle for"


_WATCHDOG_CASES = (
    ("自走沉默", _watchdog_case_silence),
    ("硬上限", _watchdog_case_hard),
    ("閒置", _watchdog_case_idle),
)


@pytest.mark.parametrize("kill_raises", [None, ProcessLookupError(3, "gone")],
                         ids=["kill 成功", "kill 丟 ProcessLookupError"])
@pytest.mark.parametrize("case_id,setup", _WATCHDOG_CASES,
                         ids=[c for c, _ in _WATCHDOG_CASES])
def test_a_watchdog_kill_that_finds_the_process_gone_keeps_its_own_verdict(
        monkeypatch, case_id, setup, kill_raises):
    """同一個情境跑兩次——kill 成功與 kill 丟 `ProcessLookupError`——**結論必須一樣**。

    「kill 成功」那一欄同時是每一格的正面對照：它若沒有走到預期的那條看門狗出口，
    右邊那一欄的「一樣」就毫無意義。
    """
    lines, kwargs, want_type, want_text = setup(monkeypatch)
    proc = _DeadOnKill(lines, kill_raises=kill_raises)
    _install(monkeypatch, proc)

    async def _call():
        try:
            await db._dorossi_via_claude_code("問題", None, **kwargs)
        # **不是** `except BaseException`：`_run` 逾時時會取消這個 coroutine，
        # 而 `CancelledError` 是 `BaseException`——接住它就會把「被測程式卡住」
        # 變成一個回傳值，紅訊息也就從「收尾沒有返回」換成看不懂的型別不合。
        # 要看的三個型別（`TimeoutError` / `_DorossiLoopSilence` /
        # `_DorossiResumeError`）全都是 `Exception` 的子類。
        except Exception as error:      # noqa: BLE001 — 就是要看到型別本身
            return error
        return None

    got = _run(_call())
    assert isinstance(got, want_type), (
        f"{case_id}／kill_raises={kill_raises!r}：拿到的是 {got!r}，"
        f"不是 {want_type.__name__}")
    assert want_text in str(got), (case_id, str(got))
    assert proc.kills >= 1, f"{case_id}：看門狗沒有砍行程"


def test_a_watchdog_kill_failure_that_is_not_a_lookup_error_is_not_swallowed(
        monkeypatch):
    """反面：`ProcessLookupError` **以外**的 kill 失敗，刻意**不**在迴圈裡吞。

    這不是漏寫。迴圈裡那三個 `except ProcessLookupError` 只赦免「行程早就不在了」
    這一種良性競態；`PermissionError` 之類代表這台主機上真的出了事，吞掉它等於把
    一個「砍不掉的後端行程還在跑」偽裝成一次普通逾時。例外會走外層的
    `except BaseException`（同步再 kill 一次、收尾照跑）再往上拋，所以**收尾仍然
    完整**——這一支同時釘住那一點。
    """
    _fast_watchdog(monkeypatch)
    proc = _DeadOnKill([], kill_raises=PermissionError(13, "Access is denied"))
    _install(monkeypatch, proc)

    async def _call():
        try:
            await db._dorossi_via_claude_code("問題", None)
        except Exception as error:      # noqa: BLE001
            return error
        return None

    got = _run(_call())
    assert isinstance(got, PermissionError), f"被吞掉了：{got!r}"
    assert proc.kills >= 2, (
        f"外層的收尾沒有再試一次 kill（kills={proc.kills}）")


# ---------------------------------------------------------------------------
# 四、一直在講話的一輪，仍然受硬上限拘束
# ---------------------------------------------------------------------------
_CHATTY_BACKENDS = (
    ("claude_code",
     lambda **kw: db._dorossi_via_claude_code("問題", None, **kw),
     "hard wall-clock"),
    ("codex",
     lambda **kw: db._dorossi_via_codex("問題", None, **kw),
     "codex invocation timed out"),
)


@pytest.mark.parametrize("kill_raises", [None, ProcessLookupError(3, "gone")],
                         ids=["kill 成功", "kill 丟 ProcessLookupError"])
@pytest.mark.parametrize("name,invoke,want_text", _CHATTY_BACKENDS,
                         ids=[n for n, _i, _t in _CHATTY_BACKENDS])
def test_a_round_that_never_stops_talking_is_still_killed_at_the_ceiling(
        monkeypatch, name, invoke, want_text, kill_raises):
    """硬上限對「一直有輸出」的一輪也必須成立。

    docstring 寫的是「(2) HARD — always kill ... regardless of pending tools」，而
    逾時那一層在這種輸入上**永遠不會開火**：每一行都已經在緩衝區裡，`readline()`
    一次 await 都沒有就返回，`wait_for` 的計時器連跑的機會都沒有（本機實測，見
    `_ChattyStdout`）。所以這一輪能不能停，只由迴圈頂端對 `deadline` 的比較決定
    ——兩個後端各有一道，這裡兩道都走一遍。

    在 `full` 模式下這是一個握著主機 shell 的後端行程，所以「停不下來」不是慢，是
    一個沒有上限的無人值守工作。
    """
    hard = 0.2
    monkeypatch.setattr(db, "DOROSSI_CC_IDLE_LIMIT_SEC", 30.0)  # 閒置那層絕不開火
    monkeypatch.setattr(db, "_dorossi_cc_hard_limit_sec", lambda: hard)
    monkeypatch.setattr(db, "find_codex_executable", lambda: r"C:\fake\codex.exe")
    monkeypatch.setattr(db, "_collect_codex_images", lambda *_a, **_k: [])
    proc = _DeadOnKill([], kill_raises=kill_raises)
    proc.stdout = _ChattyStdout(_NOISE_LINE, budget_sec=hard * 4)
    _install(monkeypatch, proc)

    async def _call():
        try:
            await invoke()
        except TimeoutError as error:
            return error
        return None

    got = _run(_call(), deadline=hard * 12)
    assert isinstance(got, TimeoutError), (
        f"{name}：一直有輸出的一輪沒有被硬上限停下來：{got!r}"
        f"（吐了 {proc.stdout.served} 行）")
    assert want_text in str(got), (name, str(got))
    assert proc.stdout.served > 10, (
        f"{name}：只吐了 {proc.stdout.served} 行——這一支要測的是「一直在講話」，"
        "串流沒跑起來就什麼都沒測到")
    assert proc.kills >= 1


# ---------------------------------------------------------------------------
# 五、取消從外面打進來，而收尾的 kill 也失敗
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kill_raises",
                         [ProcessLookupError(3, "gone"),
                          PermissionError(13, "Access is denied")],
                         ids=["ProcessLookupError", "PermissionError"])
def test_cancellation_still_propagates_when_the_teardown_kill_fails(
        monkeypatch, kill_raises):
    """`CancelledError` 打進來、同步收尾的 `proc.kill()` 也失敗 → **取消照樣傳上去**。

    這一段是 abort／關機唯一的出口。收尾的 kill 若把取消換成別的例外（或把它吞掉
    變成一個正常的返回），呼叫端會以為那一輪還在進行或已經答完，而它其實兩者都不是。
    `ProcessLookupError` 與其他 `OSError` 走的是不同的 except，兩條都要走得通。
    """
    proc = _DeadOnKill([], kill_raises=kill_raises)
    _install(monkeypatch, proc)

    async def _call():
        task = asyncio.ensure_future(db._dorossi_via_claude_code("問題", None))
        await asyncio.sleep(0)      # 讓它跑到 readline 那一步
        task.cancel()
        try:
            await task
            return None
        except asyncio.CancelledError:
            return "cancelled"
        except Exception as error:      # noqa: BLE001
            return error

    got = _run(_call())
    assert got == "cancelled", f"取消被換成了別的東西：{got!r}"
    assert proc.kills >= 1, "取消路徑上沒有同步把行程砍掉"


# ---------------------------------------------------------------------------
# 六、前提本身：`asyncio` 的 kill 真的會丟 ProcessLookupError
# ---------------------------------------------------------------------------
def test_an_asyncio_kill_really_raises_processlookuperror_after_the_child_exits():
    """上面所有「砍不到」的假物件都建立在這個前提上，所以前提要有自己的一支。

    `CLAUDE.md` 說 `ProcessLookupError` 在 Windows 上基本上不會發生——**那句話的
    範圍是 `os.kill(pid, 0)`**。`asyncio` 走的是 `BaseSubprocessTransport._check_proc`
    （`if self._proc is None: raise ProcessLookupError()`），與訊號、與平台都無關。
    假物件照著一個錯的前提做，測出來的就是一件真實世界裡不會發生的事；這一支用一個
    真的子行程把前提量一次，CPython 哪天改掉，紅的是這裡。
    """
    async def _probe():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "pass",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.stdout.read()
        await proc.stderr.read()
        await proc.wait()
        # `_proc` 是在 `_call_connection_lost` 裡被清掉的，那是一個 call_soon
        # 回呼——`wait()` 返回的當下它還沒跑，要讓出幾次才看得到。
        for _ in range(10):
            await asyncio.sleep(0)
        try:
            proc.kill()
        except ProcessLookupError:
            return "ProcessLookupError"
        except Exception as error:      # noqa: BLE001
            return error
        return None

    assert _run(_probe(), deadline=20.0) == "ProcessLookupError", (
        "asyncio 的 kill() 在子行程結束後不再丟 ProcessLookupError 了——"
        "本檔所有「砍不到」的假物件都要重新量一次")


# ---------------------------------------------------------------------------
# 七、結構守門：asyncio 子行程的 kill 一律要接得住 ProcessLookupError
#
# 這條規則跟模組無關，卻住在一個以 dorossi 命名的檔案裡，理由與隔壁
# `test_dorossi_teardown` 那段「無上限的 await」相同：它的**證據**（上面那支真的
# 子行程探測）與它的**現場**（十個推導得到的呼叫點裡有九個在 `dorossi_backend`）
# 都在這裡。範圍由下面兩支反查釘住，所以檔名不可能安靜地把它縮回 dorossi。
#
# 這條規則之所以需要被執行而不只是被相信：`CLAUDE.md` 目前那句「any
# `except ProcessLookupError` branch is dead code there」讀起來就是在邀請人把它們
# 刪掉，而刪掉之後**沒有任何症狀**——那個 except 本來就只在競態上跑到。
# ---------------------------------------------------------------------------
_CAN_CATCH_LOOKUP = {"ProcessLookupError", "OSError", "Exception", "BaseException"}

# 推導看不到的那一半：被殺的行程是**參數**或屬性傳進來的，靜態上無從得知它是
# `asyncio` 的子行程還是 `subprocess.Popen`。具名列管，與 `CLAUDE.md` 對
# `stream_child` / `run_full` 的處置同一個形狀。每一筆都要反查得到，否則改名之後
# 這份豁免會安靜地不再指向任何東西。
_INDIRECT_KILL_SITES = (
    ("dorossi_backend.py", "_dorossi_reap_proc"),
    ("discord_bot.py", "request_abort"),
    ("discord_bot.py", "_reap_usage_query_proc"),
    ("verify_dorossi_cli.py", "_kill"),
)


def _project_modules() -> list:
    # `test/`（本檔所在的目錄）照同一個判準過濾：`conftest.py` 與手動 e2e 腳本在
    # 2026-09-22 之前住在套件裡、在範圍內，搬家之後照舊。
    tests = Path(__file__).resolve().parent
    return sorted(
        [p for p in PKG_ROOT.glob("*.py") if not p.name.startswith("test_")]
        + [p for p in tests.glob("*.py") if not p.name.startswith("test_")]
        + [p for p in PKG_ROOT.parent.glob("*.py") if not p.name.startswith("test_")])


def _parents_of(func) -> dict:
    parents = {}
    for node in ast.walk(func):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _handler_names(try_node) -> set:
    names = set()
    for handler in try_node.handlers:
        node = handler.type
        if node is None:
            names.add("BaseException")      # 裸 except 什麼都接
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Tuple):
            names.update(e.id for e in node.elts if isinstance(e, ast.Name))
    return names


def _tolerates_a_vanished_process(call, func) -> bool:
    """這個 `.kill()` 有沒有被一個接得住 `ProcessLookupError` 的 `try` 包住。"""
    parents = _parents_of(func)
    node = call
    while node in parents:
        parent = parents[node]
        if isinstance(parent, ast.Try) and node in parent.body:
            if _handler_names(parent) & _CAN_CATCH_LOOKUP:
                return True
        node = parent
    return False


def _spawned_subprocess_names(func) -> set:
    """同一個函式裡由 `asyncio.create_subprocess_*` 綁出來的名字。"""
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            value, targets = node.value, node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value, targets = node.value, [node.target]
        else:
            continue
        if isinstance(value, ast.Await):
            value = value.value
        if not (isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr.startswith("create_subprocess_")):
            continue
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _async_kill_sites(tree) -> list:
    """回傳 [(函式名, 行號, 有沒有接住)]——只收推導得到的那些。

    `os.kill(pid, 0)` 這種以訊號做存活探測的寫法**不在範圍內**：它有兩個引數，而
    這裡只收零引數的 `X.kill()`，也就是子行程物件那一種。兩者的規則相反（訊號那條
    在 Windows 上本來就永遠不會丟 `ProcessLookupError`），混在一起會讓守門對著
    `_pid_alive` 亂叫。
    """
    sites = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = _spawned_subprocess_names(func)
        if not names:
            continue
        for node in ast.walk(func):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "kill"
                    and not node.args and not node.keywords
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in names):
                sites.append((func.name, node.lineno,
                              _tolerates_a_vanished_process(node, func)))
    return sites


def test_every_asyncio_subprocess_kill_can_tolerate_a_vanished_process():
    """子行程在我們動手之前自己走完 → `kill()` 丟 `ProcessLookupError`。每一個
    這種呼叫點都必須接得住，否則一個良性的競態會變成一個沒人分類得到的例外。"""
    bare, total = [], 0
    for path in _project_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func_name, lineno, ok in _async_kill_sites(tree):
            total += 1
            if not ok:
                bare.append(f"{path.name}:{lineno} {func_name}")
    assert total >= 8, (
        f"只推導出 {total} 個 asyncio 子行程的 kill——掃描壞了，"
        "空的選擇看起來就像乾淨的結果")
    assert not bare, (
        "這些 `proc.kill()` 接不住 `ProcessLookupError`：\n  "
        + "\n  ".join(bare)
        + "\n（asyncio 的 kill 在行程已結束後由 `_check_proc` 丟它，與平台無關——"
          "`CLAUDE.md` 那句「Windows 上是死碼」講的是 `os.kill(pid, 0)`。）")


def test_the_named_indirect_kill_sites_are_all_still_there_and_guarded():
    """推導看不到的那幾個（行程是參數／屬性傳進來的）具名列管，並且兩個方向都查。

    只查「有沒有接住」而不查「還在不在」的話，改個函式名就會讓這份豁免變成一串
    再也對不上任何東西的字串，而守門照跑、整套照綠。
    """
    missing, bare = [], []
    for module, func_name in _INDIRECT_KILL_SITES:
        path = PKG_ROOT / module
        if not path.exists():
            path = PKG_ROOT.parent / module
        if not path.exists():
            missing.append(f"{module}（檔案不在）")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = [f for f in ast.walk(tree)
                 if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and f.name == func_name]
        if not found:
            missing.append(f"{module}:{func_name}")
            continue
        for func in found:
            kills = [n for n in ast.walk(func)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "kill" and not n.args]
            if not kills:
                missing.append(f"{module}:{func_name}（裡面已經沒有 kill 了）")
                continue
            for call in kills:
                if not _tolerates_a_vanished_process(call, func):
                    bare.append(f"{module}:{call.lineno} {func_name}")
    assert not missing, f"這份豁免已經對不上程式碼了：{missing}"
    assert not bare, f"具名列管的呼叫點接不住 ProcessLookupError：{bare}"


_SCANNER_CONTROL = '''
import asyncio


async def guarded():
    proc = await asyncio.create_subprocess_exec("x")
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def bare():
    proc = await asyncio.create_subprocess_exec("x")
    proc.kill()


async def caught_too_late():
    """`finally` 不是 `body`——包在那裡的 kill 一樣會逸出。"""
    proc = await asyncio.create_subprocess_exec("x")
    try:
        pass
    except ProcessLookupError:
        pass
    finally:
        proc.kill()


async def not_a_subprocess(handle):
    handle.kill()


def signal_probe(pid):
    import os
    os.kill(pid, 0)
'''


def test_the_kill_scanner_tells_the_four_shapes_apart():
    """合成對照組：守門必須抓到該抓的、放過不該抓的。

    `caught_too_late` 是那個會讓人看走眼的形狀——`try` 有，而且 except 寫的正是
    `ProcessLookupError`，只是 kill 在 `finally` 裡，所以那個 except 接不到它。
    `signal_probe` 則是相反的近似例：`os.kill(pid, 0)` 在 Windows 上本來就不會丟
    `ProcessLookupError`，掃進來就會對著 `_pid_alive` 那一族亂叫。
    """
    sites = dict((name, ok) for name, _lineno, ok
                 in _async_kill_sites(ast.parse(_SCANNER_CONTROL)))
    assert sites == {"guarded": True, "bare": False, "caught_too_late": False}, sites


# ---------------------------------------------------------------------------
# 八、codex 那一側自己的收尾路徑
#
# 上面那張輔助動作的表兩個後端共用，但**看門狗的形狀不一樣**：codex 沒有 idle 那一
# 層，逾時只有一個出口，而且它的 `finally` 是靠 `proc.returncode is None` 決定要不
# 要補一刀。形狀不同的部分就不能靠共用的表帶過去。
# ---------------------------------------------------------------------------
def test_a_codex_timeout_kill_that_finds_the_process_gone_keeps_its_verdict(
        monkeypatch):
    """codex 逾時 → kill 丟 `ProcessLookupError` → 結論仍然是逾時。

    這一條的 `except` 只寫了 `ProcessLookupError`（沒有 claude 那側的
    `except Exception` 第二層），所以它是這裡唯一的赦免；少了它，一個良性的競態會
    讓 `ProcessLookupError` 取代 `TimeoutError` 逸出，呼叫端的逾時分類就接不到。
    """
    monkeypatch.setattr(db, "find_codex_executable", lambda: r"C:\fake\codex.exe")
    monkeypatch.setattr(db, "_collect_codex_images", lambda *_a, **_k: [])
    monkeypatch.setattr(db, "_dorossi_cc_hard_limit_sec", lambda: 0.15)
    proc = _DeadOnKill([], kill_raises=ProcessLookupError(3, "gone"))
    _install(monkeypatch, proc)

    async def _call():
        try:
            await db._dorossi_via_codex("問題", None)
        except Exception as error:      # noqa: BLE001
            return error
        return None

    got = _run(_call(), deadline=3.0)
    assert isinstance(got, TimeoutError), f"kill 失敗換掉了結論：{got!r}"
    assert "codex invocation timed out" in str(got), got
    assert proc.kills >= 1


@pytest.mark.parametrize("kill_raises",
                         [None, ProcessLookupError(3, "gone"),
                          PermissionError(13, "Access is denied")],
                         ids=["kill 成功", "ProcessLookupError", "PermissionError"])
def test_a_codex_round_that_dies_unexpectedly_is_still_killed_in_the_teardown(
        monkeypatch, kill_raises):
    """`readline` 丟出非逾時的例外 → 例外照樣往上傳，但行程**不准**留在主機上。

    這條路（單行 NDJSON 超過緩衝上限時 `readline` 會丟 `ValueError`）既不是逾時、
    也不是正常 EOF，所以兩個 `except` 都接不到；把行程收掉的是 `finally` 裡那道
    `if proc.returncode is None` 補刀。`full` 模式下留下來的是一個握著主機 shell
    的後端行程。
    """
    monkeypatch.setattr(db, "find_codex_executable", lambda: r"C:\fake\codex.exe")
    monkeypatch.setattr(db, "_collect_codex_images", lambda *_a, **_k: [])
    proc = _DeadOnKill([])
    proc.stdout = _NeverStream([], readline_raises=ValueError("Separator is not found"))
    # 收尾那一刀自己失敗時，**離開這支函式的仍然必須是原本那個例外**：`finally` 裡
    # 的 kill 只是盡力而為，它的失敗沒有資格取代呼叫端要分類的那個錯誤。
    _count_waits(proc)
    _make_kill_fail(proc, kill_raises)
    _install(monkeypatch, proc)

    async def _call():
        try:
            await db._dorossi_via_codex("問題", None)
        except Exception as error:      # noqa: BLE001
            return error
        return None

    got = _run(_call(), deadline=5.0)
    assert isinstance(got, ValueError), f"例外被換掉了：{got!r}"
    assert proc.kills >= 1, "非預期離開路徑上把後端行程留在主機上了"
    assert proc.wait_started == 0, (
        "收尾那一刀不是同步砍的——行程被留著又多活了一整個 reap 上限，"
        "而 `full` 模式下它握著主機的 shell")


def test_the_loop_guidance_reaches_the_codex_child_on_every_round(monkeypatch):
    """自走守則是**每一輪**都要真的到達後端的東西，所以它要進 wire prompt。

    claude 那一側走 `--append-system-prompt`（`_dorossi_cc_argv` 組，已有測試）；
    codex 沒有那個旗標，改成接在這一輪的 prompt 後面。兩邊的保證相同，而這一行是
    codex 唯一的實作——漏掉不會有任何症狀，只是守則從此不再到達後端。
    """
    monkeypatch.setattr(db, "find_codex_executable", lambda: r"C:\fake\codex.exe")
    monkeypatch.setattr(db, "_collect_codex_images", lambda *_a, **_k: [])
    written = []
    proc = _proc([_CODEX_THREAD, _CODEX_ANSWER])
    proc.stdin.write = written.append
    _install(monkeypatch, proc)

    answer, _sid, _info = _run(db._dorossi_via_codex(
        "問題", "thr-1", loop_system_guidance="每一輪都要照這條做"))

    assert answer == "答案", answer
    blob = b"".join(written).decode("utf-8")
    assert "每一輪都要照這條做" in blob, (
        f"自走守則沒有到達後端：{blob!r}")
    assert blob.index("問題") < blob.index("每一輪都要照這條做"), (
        "守則被排到問題前面了——它是附加在這一輪之後的守則，不是新的問題")
