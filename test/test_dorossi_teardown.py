"""後端叫用的**收尾路徑**：離開時不得留下活著的行程、孤兒任務，或無上限的等待。

為什麼要有這一份（2026-09-09）：`_dorossi_via_claude_code` ——也就是**預設後端**——
的讀取迴圈外面原本是兩行裸的 await：

    rc = await proc.wait()
    err = (await err_task).decode(...)

兩行**各自**都是無限的，而且整支函式連 try/finally 都沒有。實測（本機 CPython
3.14 / Windows）把兩件事釘死：

1. `asyncio` 的 `BaseSubprocessTransport._wait()` 掛在 `_exit_waiters` 上，那批
   waiter 只有 `_call_connection_lost` 會叫醒，而 `_try_finish` 要求
   `all(p.disconnected)`——**stdout 與 stderr 都要先 EOF**。孫行程繼承著管線寫端時
   `await proc.wait()` 就永遠不返回。量到的數字：kill 之後 pending 的 `wait()` 三秒
   後仍未完成；把孫行程殺掉的那一瞬間立刻返回。
2. 同一時刻 `proc.returncode` **已經是 1**（0.25 秒就設好了）——`_process_exited`
   與管線無關。這就是修法的支點：有上限地 `wait()`，逾時退回 `returncode`。

孫行程留得下來是因為 Windows 的 `proc.kill()` 是 `TerminateProcess`，只帶走直接子
行程；而 `dorossi_cc_tools="full"` 正是會在主機上起 shell 的模式。

嚴重度來自呼叫端：`_dorossi_process_turn` 一律在 `async with lock`（per-session 鎖）
裡叫用，所以卡在這裡＝那個 session 的鎖永久掛死，之後每一輪都只能排隊等一個永遠不會
結束的回合（排到 `DOROSSI_MAX_WAITING` 才開始回「忙線中」），`/dorossi session continue`
直接被 `lock.locked()` 擋掉。

**這一份的假物件模擬的是「平台」，不是「被測程式」。** `_FakeProc.wait()` 的語意
（要等管線 EOF、`returncode` 卻早就設好）整條來自上面那兩個實測結果，不是從
`_dorossi_reap_proc` 抄回來的——抄本永遠是綠的。而且每一支測試都自帶一個
**反面對照組**：先用同一個假物件跑一次**舊寫法**那兩行，證明它真的會卡住，再跑新的。
只證明新寫法會過，證不出防護到底有沒有在做事。

⚠️ **反例就在隔壁，值得記住是怎麼漏的。** `test_bot_helpers` 的
`test_the_codex_usage_query_cannot_hang_on_a_pipe_nobody_closes` 早就在測同一支
函式的同一段收尾，卻抓不到那一段裡的 `await proc.wait()`——因為它的假 `wait()`
寫的是 `return self.returncode`，也就是照著**被測程式當時的假設**（「行程死了
wait() 就會回來」）做的，而不是照著平台真正的行為。假物件抄實作，就只會確認實作
的假設，不會挑戰它。

**這份檔案下半部的結構守門是一條專案級規則**（「起了子行程、接了管線，就不准有
沒有上限的 `await`」），不只管 dorossi。它為什麼住在一個以 dorossi 命名的檔案裡、
以及範圍怎麼被釘住，寫在那一段的區塊註解裡。
"""
from __future__ import annotations

import ast
import asyncio
import json
import math
import os
import sys

from time import monotonic as _now

import pytest

from pathlib import Path

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import dorossi_backend as db  # noqa: E402

PKG_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "axiomatic"

# 測試用的收尾上限。**從上線的原始碼抽**，不要在這裡寫死第二份——寫死的話有人把
# 正式值改掉時這裡照樣綠。這只是把它縮小以免測試真的等十秒。
SHIPPED_REAP_TIMEOUT = db._DOROSSI_REAP_TIMEOUT_SEC
FAST_REAP_TIMEOUT = 0.25
# 每支測試的牆鐘上限。**卡住的測試比紅的測試更糟**，所以每一個 `asyncio.run` 都包
# `wait_for`。倍率取得寬鬆（收尾最多兩輪 reap ＋ 一次 drain）但仍遠小於「無限」。
TEST_DEADLINE = FAST_REAP_TIMEOUT * 20


# ---------------------------------------------------------------------------
# 平台替身
# ---------------------------------------------------------------------------
class _NeverStream:
    """`read()` 永遠不返回（EOF 不會來），`readline()` 依序吐出預先排好的行。

    這就是「孫行程握著寫端」在 `asyncio` 這一側看起來的樣子：資料讀得到，EOF 讀不到。
    """

    def __init__(self, lines=(), *, readline_raises=None):
        self._lines = list(lines)
        self._readline_raises = readline_raises
        self.read_started = 0

    async def read(self):
        self.read_started += 1
        await asyncio.get_running_loop().create_future()   # 永遠不完成
        raise AssertionError("unreachable")

    async def readline(self):
        if self._readline_raises is not None and not self._lines:
            raise self._readline_raises
        if self._lines:
            return self._lines.pop(0)
        return b""          # stdout 這一側的 EOF：後端自己把答案吐完離開了


class _FakeStdin:
    def write(self, _data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class _FakeProc:
    """行為抄自實測，不是抄自 `_dorossi_reap_proc`。

    * `returncode` 由 `exit_with()` 設定——模擬 `_process_exited`：作業系統層的離開一被
      觀察到就設好，**與管線無關**。
    * `wait()` 等的是 `pipes_closed` 這個 Event，**不是** `returncode`——模擬
      `_try_finish` 要求 `all(p.disconnected)`。所以「行程死了但管線被握著」時
      `wait()` 不返回，而 `returncode` 讀得到。
    * `kill()` 讓 `returncode` 在**下一個 tick** 出現（實測 0.25 秒），但**不會**讓
      管線 EOF——`TerminateProcess` 帶不走繼承了管線的孫行程。`unkillable=True` 模擬
      「連 kill 都收不掉」的異常主機狀態。
    """

    def __init__(self, *, stdout_lines=(), readline_raises=None,
                 exited_rc=None, unkillable=False, kill_rc=1):
        self.stdin = _FakeStdin()
        self.stdout = _NeverStream(stdout_lines, readline_raises=readline_raises)
        self.stderr = _NeverStream()
        self.returncode = exited_rc
        self.kills = 0
        self.pid = 4242
        self._unkillable = unkillable
        self._kill_rc = kill_rc
        self._pipes_closed = asyncio.Event()   # 永遠不 set：管線被孫行程握著

    def kill(self):
        self.kills += 1
        if self._unkillable or self.returncode is not None:
            return

        def _observed_exit():
            self.returncode = self._kill_rc

        asyncio.get_running_loop().call_soon(_observed_exit)

    async def wait(self):
        # 等的是管線，不是 returncode——這正是 `_try_finish` 的
        # `all(p.disconnected)` 條件在測試裡的樣子。
        await self._pipes_closed.wait()
        return self.returncode


_RESULT_LINE = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "session_id": "sess-abc", "result": "答案", "total_cost_usd": 0.5,
    "usage": {"input_tokens": 1, "output_tokens": 2,
              "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4},
}).encode("utf-8") + b"\n"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """把會落地的東西全部導去 tmp，並把收尾上限縮小。

    `_dorossi_round_info_and_record` 會 append 到 repo root 的用量帳本——那是**線上
    使用者資料**。同 `test_bot_helpers` 對 `AUDIT_FILE` 的處置：用 autouse fixture 導開，
    不要逐測試處理。
    """
    monkeypatch.setattr(db, "DOROSSI_USAGE_FILE", tmp_path / "usage.ndjson")
    monkeypatch.setattr(db, "_DOROSSI_REAP_TIMEOUT_SEC", FAST_REAP_TIMEOUT)
    monkeypatch.setattr(db, "DOROSSI_CC_WORKDIR", tmp_path / "work")
    monkeypatch.setattr(db._shutil, "which", lambda _n: r"C:\fake\claude.exe")


def _install(monkeypatch, proc):
    async def _fake_exec(*_a, **_k):
        return proc

    monkeypatch.setattr(db.asyncio, "create_subprocess_exec", _fake_exec)


def _run(coro, deadline=TEST_DEADLINE, leftovers=None):
    """跑一段 coroutine，**一定**帶牆鐘上限。

    逾時訊息要指名這是回歸而不是環境慢：這一族測的就是「會不會永遠不返回」。

    `leftovers` 給一個 list 的話，會在被測程式**返回／拋出的那一瞬間**把「還沒完成
    的殘留任務」記進去。時機是重點：**`asyncio.run` 收工時自己會取消殘留任務**，所以
    事後再看永遠是乾淨的。這正是原本的 `_no_orphan_readers()` 量不到孤兒任務的原因
    ——實測把 `_dorossi_reap_proc` 與 `_dorossi_drain_stderr` 的 `finally`（`cancel()`
    ＋ `gather()`）整段換成 `pass`，14 支測試照樣全綠。「加了上限」與「收得乾淨」是
    兩件事，兩個 docstring 都寫了後者是規則，卻沒有任何東西在執行它。
    """
    async def _bounded():
        mine = asyncio.current_task()
        try:
            return await asyncio.wait_for(coro, timeout=deadline)
        except (asyncio.TimeoutError, TimeoutError):
            raise AssertionError(
                f"收尾在 {deadline:.1f}s 內沒有返回——收尾路徑上又出現無上限的等待了"
            ) from None
        finally:
            if leftovers is not None:
                leftovers.extend(_describe(t) for t in asyncio.all_tasks()
                                 if t is not mine and not t.done())

    return asyncio.run(_bounded())


def _describe(task) -> str:
    """把一個殘留任務講成人看得懂的字串（斷言訊息要能指出是哪一個 await 沒收）。"""
    coro = task.get_coro()
    return getattr(coro, "__qualname__", None) or repr(coro)


# ---------------------------------------------------------------------------
# 反面對照組：舊寫法真的會卡住
# ---------------------------------------------------------------------------
def test_the_old_teardown_really_does_hang_on_this_fake():
    """先證明假物件重現得出缺陷，否則下面每一支都只是在測一件本來就成立的事。

    這裡跑的是**舊寫法那兩行**（`await proc.wait()` / `await err_task`），對象是同一個
    假物件。兩個都必須逾時；任何一個返回就代表假物件沒有重現「孫行程握著管線」。
    """
    async def _old_shape():
        proc = _FakeProc(exited_rc=0)          # 行程已離開，管線仍被握著
        err_task = asyncio.ensure_future(db._read_stream_all(proc.stderr))
        hung = []
        for name, awaitable in (("proc.wait()", proc.wait()),
                                ("err_task", asyncio.shield(err_task))):
            try:
                await asyncio.wait_for(awaitable, timeout=FAST_REAP_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError):
                hung.append(name)
        err_task.cancel()
        await asyncio.gather(err_task, return_exceptions=True)
        return hung

    hung = _run(_old_shape())
    assert hung == ["proc.wait()", "err_task"], (
        f"舊寫法居然沒卡住：{hung}。假物件沒重現缺陷，下面的測試就沒有意義。")


def test_the_shipped_timeout_is_a_real_finite_bound():
    """正式值本身要是有限正數。

    `nan` 會讓每一道用比較寫成的防線同時失效（比較一律回 False），`inf` 與 0／負數
    則分別等於「沒有上限」與「立刻放棄」。這條在本 repo 已經踩過兩次。
    """
    assert isinstance(SHIPPED_REAP_TIMEOUT, (int, float)), SHIPPED_REAP_TIMEOUT
    assert not isinstance(SHIPPED_REAP_TIMEOUT, bool)
    assert math.isfinite(SHIPPED_REAP_TIMEOUT), SHIPPED_REAP_TIMEOUT
    assert SHIPPED_REAP_TIMEOUT > 0, SHIPPED_REAP_TIMEOUT


# ---------------------------------------------------------------------------
# 正常路徑：管線被握著，但答案已經拿到了
# ---------------------------------------------------------------------------
def test_a_finished_round_still_returns_when_the_pipes_are_held(monkeypatch):
    """後端答完、離開，但孫行程還握著 stderr → 舊寫法在這裡永遠卡住。

    這是**最重要**的一格：答案已經在 `state` 裡，rc 也問得到（`returncode` 與管線
    無關），所以正確的結果是「照常回答」，不是逾時、也不是把成功的一輪判成失敗。
    """
    proc = _FakeProc(stdout_lines=[_RESULT_LINE], exited_rc=0)
    _install(monkeypatch, proc)

    answer, sid, info = _run(db._dorossi_via_claude_code("問題", None))

    assert answer == "答案", answer
    assert sid == "sess-abc", sid
    assert info["cost_usd"] == 0.5, info
    assert proc.stderr.read_started == 1, "stderr 沒有被抽水"


def test_the_answer_survives_even_though_stderr_is_unreachable(monkeypatch):
    """stderr 拿不到只能降級成空字串，**不可以**因此把整輪打掉。

    stderr 只是診斷的第三順位來源（前面還有 result 事件與 stdout 尾巴）。
    """
    seen = {}
    real = db._claude_stream_verdict

    def _spy(state, rc, err, session_id, **kw):
        seen["rc"] = rc
        seen["err"] = err
        return real(state, rc, err, session_id, **kw)

    monkeypatch.setattr(db, "_claude_stream_verdict", _spy)
    proc = _FakeProc(stdout_lines=[_RESULT_LINE], exited_rc=0)
    _install(monkeypatch, proc)

    _run(db._dorossi_via_claude_code("問題", None))
    assert seen["rc"] == 0, seen
    assert seen["err"] == "", seen


def test_a_process_that_will_not_die_is_reported_as_a_failure(monkeypatch):
    """連 rc 都問不出來（kill 過了還是沒有 returncode）→ 走既有的非零離開分類。

    不可以當成 rc==0：那會把一個什麼都沒產出的回合當成「成功但答案是空的」送出去。
    """
    proc = _FakeProc(stdout_lines=[], unkillable=True)   # EOF 但行程「不死」
    _install(monkeypatch, proc)

    with pytest.raises(RuntimeError) as excinfo:
        _run(db._dorossi_via_claude_code("問題", None))
    assert str(db._DOROSSI_UNREAPED_RC) in str(excinfo.value), excinfo.value
    assert proc.kills >= 1, "收不掉的行程沒有被 kill"


# ---------------------------------------------------------------------------
# 讀取迴圈的閒置看門狗：答完之後 CLI 還掛著（2026-09-19 事故）
#
# 模型在回合結尾留下一個還沒觸發的監看工作 → CLI 先送出 `result`，然後**完全靜默**
# 地等它觸發。背景工作不是 pending 的 tool_use，所以舊版閒置監看把這段等待砍掉，
# 判定又把手上的答案丟掉。兩道修法各有一支：背景工作壓住閒置監看（但壓不住硬上限），
# 以及閒置砍掉時若已收到成功的 result 就收下答案。
#
# 這一段放在這個檔案，是因為讀取迴圈的假行程只有這裡有；`test_dorossi_stream.py`
# 的範圍刻意只到「純折疊 ＋ 純判定」。
# ---------------------------------------------------------------------------
FAST_IDLE = 0.05
FAST_HARD = 0.6


class _LingeringStdout:
    """吐完預先排好的行之後**不 EOF、也不再輸出**，直到行程被 kill——就是 CLI 答完
    還掛著等背景工作時，`asyncio` 這一側看到的樣子。"""

    def __init__(self, lines):
        self._lines = list(lines)
        self.cut = asyncio.Event()

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        await self.cut.wait()
        return b""


class _LingeringProc(_FakeProc):
    def __init__(self, lines):
        super().__init__()
        self.stdout = _LingeringStdout(lines)

    def kill(self):
        super().kill()
        self.stdout.cut.set()


def _bg_line(*task_ids):
    return json.dumps({
        "type": "system", "subtype": "background_tasks_changed",
        "session_id": "sess-abc",
        "tasks": [{"task_id": t, "task_type": "local_bash", "description": "probe"}
                  for t in task_ids]}).encode("utf-8") + b"\n"


def _fast_watchdog(monkeypatch):
    monkeypatch.setattr(db, "DOROSSI_CC_IDLE_LIMIT_SEC", FAST_IDLE)
    monkeypatch.setattr(db, "_dorossi_cc_hard_limit_sec", lambda: FAST_HARD)


def test_a_pending_background_task_holds_off_the_idle_kill(monkeypatch):
    """背景工作還在 → 閒置監看不開火，一路等到**硬上限**才砍；沒有答案時硬上限丟例外。

    判準是「死在哪一層」：少了背景工作那個抑制條件，這裡會在 `FAST_IDLE` 就被閒置
    監看砍掉（例外訊息是 idle 不是 hard，時間也早得多）。

    2026-09-19 之前這支餵的是「背景工作 ＋ 成功的 result」，靠「硬上限丟例外、閒置
    出口不丟」分辨兩層。同一天硬上限也有了保留答案的出口，那個訊號就沒了，所以改成
    **沒有 result** 的串流（同時釘住「硬上限在沒有答案時照舊丟例外」），有答案的那一格
    交給下一支。
    """
    _fast_watchdog(monkeypatch)
    proc = _LingeringProc([_bg_line("b2ydaq80d")])
    _install(monkeypatch, proc)

    async def _call():
        # 在 coroutine **裡面**接 TimeoutError：`_run` 把逸出的 TimeoutError 一律當成
        # 「測試本身逾時」改寫成 AssertionError，會把被測程式丟的那一個吃掉。
        try:
            await db._dorossi_via_claude_code("問題", None)
        except TimeoutError as error:
            return error
        return None

    started = _now()
    got = _run(_call())
    assert isinstance(got, TimeoutError), f"應該被硬上限砍掉，卻回傳了：{got!r}"
    assert "hard" in str(got), got
    assert _now() - started >= FAST_HARD * 0.9, "沒等到硬上限就結束了"
    assert proc.kills >= 1


def test_a_hard_kill_after_the_answer_returns_the_answer(monkeypatch, capsys):
    """CLI 送出成功的 result 之後被背景工作撐著、一路撐到硬上限才被砍 → **答案照常
    回傳**（2026-09-19 擁有者：「任務一直被殺掉」）。舊行為是在這裡丟 TimeoutError、
    把 3 小時前就出來的答案丟掉。

    時間下限釘住「真的是硬上限砍的」（不是閒置監看提早收下答案），log 那一行釘住
    判定端走的是硬上限的出口。rc 非零、session_id 有值——落到 rc != 0 會變成 resume
    重試，這支同時釘住它沒有落下去。"""
    _fast_watchdog(monkeypatch)
    proc = _LingeringProc([_bg_line("b1"), _RESULT_LINE])
    _install(monkeypatch, proc)

    started = _now()
    answer, sid, info = _run(db._dorossi_via_claude_code("問題", "sess-abc"))
    elapsed = _now() - started
    assert answer == "答案", answer
    assert sid == "sess-abc", sid
    assert info["cost_usd"] == 0.5, info
    assert elapsed >= FAST_HARD * 0.9, f"{elapsed:.2f}s——不是硬上限砍的"
    assert proc.kills >= 1
    err = capsys.readouterr().err
    assert "hard-killed" in err and "answer kept" in err, err


def test_a_loop_silence_kill_after_the_answer_returns_the_answer(monkeypatch, capsys):
    """自走模式：答完之後 CLI 不離開、沉默到 `silence_limit` 被砍 → 答案照常回傳，
    不丟 `_DorossiLoopSilence`（那會讓迴圈把做完的一輪整個重跑一次）。"""
    monkeypatch.setattr(db, "_dorossi_cc_hard_limit_sec", lambda: FAST_HARD)
    proc = _LingeringProc([_RESULT_LINE])
    _install(monkeypatch, proc)

    async def _call():
        try:
            return await db._dorossi_via_claude_code(
                "問題", "sess-abc", silence_limit=FAST_SILENCE)
        except db._DorossiLoopSilence as error:
            return error

    got = _run(_call())
    assert isinstance(got, tuple), f"沉默砍掉時丟掉了已經出來的答案：{got!r}"
    assert got[0] == "答案", got
    assert proc.kills >= 1
    assert "answer kept" in capsys.readouterr().err


def test_an_idle_kill_after_the_answer_returns_the_answer(monkeypatch, capsys):
    """沒有背景工作、答案已經出來、CLI 卻還掛著 → 閒置監看砍掉它，**答案照常回傳**。

    這是 2026-09-19 使用者實際看到的那條路。被 kill 的行程 rc 非零，所以這支也釘住
    「閒置出口不會落到 rc != 0 那一段」在真的讀取迴圈裡成立。

    「是閒置監看砍的、不是硬上限」看的是**哪一道監看留下的那一行**，不是耗時。原本斷言
    耗時 `< FAST_HARD`（0.6 秒），2026-09-22 在一輪跑了 10 分 42 秒的重負載整套裡誤紅過一次
    ——單獨跑三次都過。兩道監看的 stderr 措辭不同，而那是結果本身，不是它的代理。
    """
    _fast_watchdog(monkeypatch)
    proc = _LingeringProc([_RESULT_LINE])
    _install(monkeypatch, proc)

    answer, sid, _info = _run(db._dorossi_via_claude_code("問題", "sess-abc"))
    assert answer == "答案", answer
    assert sid == "sess-abc", sid
    err = capsys.readouterr().err
    assert "idle-killed" in err, f"應該是閒置監看砍的：{err[-400:]!r}"
    assert "hard-killed" not in err, f"是硬上限砍的，不是閒置監看：{err[-400:]!r}"
    assert proc.kills >= 1


def _cumulative_round(cost, cr, *, version="2.1.277"):
    """一次 2.1.277 的 resume：init 帶版本，result 的金額／modelUsage 是工作階段累計、
    頂層 usage 與 iterations 是這次叫用（形狀照 2026-09-19 實跑）。"""
    init = json.dumps({"type": "system", "subtype": "init", "session_id": "sess-abc",
                       "claude_code_version": version}).encode("utf-8") + b"\n"
    result = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": "sess-abc", "result": "答案", "total_cost_usd": cost,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 6500,
                  "cache_creation_input_tokens": 80, "output_tokens": 30,
                  "iterations": [{"type": "message", "input_tokens": 10,
                                  "cache_read_input_tokens": 6500,
                                  "cache_creation_input_tokens": 80,
                                  "output_tokens": 30}]},
        "modelUsage": {"claude-haiku": {
            "inputTokens": 30, "cacheReadInputTokens": cr,
            "cacheCreationInputTokens": 6800, "outputTokens": 110,
            "costUSD": cost}},
    }).encode("utf-8") + b"\n"
    return [init, result]


def test_the_read_loop_hands_the_version_and_baseline_to_the_accounting(monkeypatch):
    """接線測試：`_dorossi_via_claude_code` 要把**這次 resume 的 id、後端回報的 id、
    init 的版本、呼叫端給的基準**四樣都交給換算。純函式那一層（`test_dorossi_usage_record`）
    測得到規則，測不到「讀取迴圈有沒有把東西傳下去」——任何一樣掉了，回傳的就是
    工作階段累計，自走迴圈的花費觸發又會每一輪都過門檻。"""
    baseline = {"sid": "sess-abc", "mode": "cumulative", "cost": 0.0144,
                "tok": "model", "in": 20, "cr": 6500, "cc": 6720, "out": 80}
    proc = _FakeProc(stdout_lines=_cumulative_round(0.0154, 13000), exited_rc=0)
    _install(monkeypatch, proc)

    answer, sid, info = _run(db._dorossi_via_claude_code(
        "問題", "sess-abc", usage_baseline=baseline))
    assert answer == "答案" and sid == "sess-abc"
    assert info["acct"] == "delta", info
    assert abs(info["cost_usd"] - 0.001) < 1e-9, info
    assert (info["in"], info["cr"], info["cc"], info["out"]) == (10, 6500, 80, 30), info
    assert info["ctx"] == 6590, info
    # 下一輪的基準是**原始**累計值，而且屬於後端回報的那個 id。
    assert info["usage_mark"]["cost"] == 0.0154
    assert info["usage_mark"]["sid"] == "sess-abc"
    assert info["usage_mark"]["mode"] == "cumulative"


def test_the_read_loop_takes_the_version_from_the_stream(monkeypatch):
    """同一個串流、沒有基準：2.1.277 → 分不出這一次佔多少，金額記 0（不膨脹）；
    2.1.276 → 回報值本身。兩個結果不同，**只**因為 init 事件的版本——讀取迴圈沒把
    版本交下去的話，兩格會一樣（上一支帶著累計基準，版本掉了會被「沿用上一次模式」
    蓋過去，所以要這一支）。"""
    for version, acct, cost in (("2.1.277", "base", 0.0), ("2.1.276", "call", 0.0154)):
        proc = _FakeProc(stdout_lines=_cumulative_round(0.0154, 13000, version=version),
                         exited_rc=0)
        _install(monkeypatch, proc)
        _answer, _sid, info = _run(db._dorossi_via_claude_code("問題", "sess-abc"))
        assert (info["acct"], info["cost_usd"]) == (acct, cost), (version, info)
        assert info["v"] == version


# ---------------------------------------------------------------------------
# 後端 CLI 沒有可用的登入：讀取迴圈那一半（2026-09-19）
# ---------------------------------------------------------------------------
# 判定規則在 `test_dorossi_stream` 用假串流測；這裡測的是**接線**：讀取迴圈要把 init 的
# `memory_paths` 與 result 交給判定，啟動警告要在判定之前印（判定會 raise），而且整輪要
# 以 `_DorossiAuthError` 結束、**不是** `_DorossiResumeError`（後者會讓呼叫端丟掉對話
# 重開一次，以一模一樣的方式再失敗）。事件照 CLI 2.1.276 實測的 bare 模式形狀寫。

def _event_line(ev: dict) -> bytes:
    return json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n"


_BARE_INIT = {"type": "system", "subtype": "init", "session_id": "sess-abc",
              "apiKeySource": "none", "tools": [], "claude_code_version": "2.1.276"}
_NORMAL_INIT = dict(_BARE_INIT, memory_paths={"auto": "C:/x/memory/"})
_NOT_LOGGED_IN = {"type": "result", "subtype": "success", "is_error": True,
                  "api_error_status": None, "terminal_reason": "api_error",
                  "result": "Not logged in \u00b7 Please run /login",
                  "session_id": "sess-abc", "total_cost_usd": 0}


async def _catch_auth(coro):
    try:
        await coro
    except db._DorossiAuthError as error:
        return error
    return None


def test_a_sign_in_failure_ends_the_round_without_a_fresh_session_retry(
        monkeypatch, capsys):
    """兩輪都是 bare 模式的「Not logged in」：每一輪都以 `_DorossiAuthError` 結束，而
    「像 bare 模式」那一行啟動警告在這個行程裡只出現一次（每一輪都重判，不去重的話
    會洗掉整份 log）。"""
    for _ in range(2):
        proc = _FakeProc(stdout_lines=[_event_line(_BARE_INIT),
                                       _event_line(_NOT_LOGGED_IN)], exited_rc=1)
        _install(monkeypatch, proc)
        got = _run(_catch_auth(db._dorossi_via_claude_code("問題", "sess-abc")))
        assert isinstance(got, db._DorossiAuthError), got
        assert got.bare_suspect is True
    err = capsys.readouterr().err
    # 「claude -p started」只出現在啟動警告那一句；未登入那一行的 bare 提示寫的是
    # 「The CLI also started」，不能拿兩者共用的片語來數。
    assert err.count("claude -p started without instruction-file discovery") == 1, err
    assert err.count("no usable sign-in (result text)") == 2, err


def test_a_normal_start_prints_no_startup_warning(monkeypatch, capsys):
    """必須放行：一般模式（有 `memory_paths`、`apiKeySource` 是 "none"）一個字都不多印。"""
    proc = _FakeProc(stdout_lines=[_event_line(_NORMAL_INIT), _RESULT_LINE], exited_rc=0)
    _install(monkeypatch, proc)
    answer, _sid, _info = _run(db._dorossi_via_claude_code("問題", "sess-abc"))
    assert answer == "答案"
    err = capsys.readouterr().err
    assert "instruction-file discovery" not in err, err
    assert "authenticating with" not in err, err


def test_a_fresh_session_ignores_a_stale_baseline_in_the_read_loop(monkeypatch):
    """新工作階段（沒有 `--resume`）的總額就是這次叫用，不管呼叫端塞了什麼基準。
    自走迴圈的 resume 重試正是這樣呼叫的（`invoke(prompt, None, **common)`，common 裡
    帶著舊基準）。"""
    stale = {"sid": "sess-abc", "mode": "cumulative", "cost": 0.0144,
             "tok": "model", "in": 20, "cr": 6500, "cc": 6800, "out": 80}
    proc = _FakeProc(stdout_lines=_cumulative_round(0.0154, 13000), exited_rc=0)
    _install(monkeypatch, proc)

    _answer, _sid, info = _run(db._dorossi_via_claude_code(
        "問題", None, usage_baseline=stale))
    assert info["acct"] == "call", info
    assert info["cost_usd"] == 0.0154, info


def test_a_cleared_task_snapshot_lets_the_idle_kill_fire_again(monkeypatch, capsys):
    """快照變成空清單之後，閒置監看要恢復——不然一次背景工作就讓整輪只剩硬上限。

    判斷看哪一道監看留下的那一行，不看耗時（理由見
    `test_an_idle_kill_after_the_answer_returns_the_answer`）。"""
    _fast_watchdog(monkeypatch)
    proc = _LingeringProc([_bg_line("b1"), _RESULT_LINE, _bg_line()])
    _install(monkeypatch, proc)

    answer, _sid, _info = _run(db._dorossi_via_claude_code("問題", None))
    assert answer == "答案", answer
    err = capsys.readouterr().err
    assert "idle-killed" in err and "hard-killed" not in err, (
        f"空快照沒有把閒置監看放回來：{err[-400:]!r}")


def test_the_idle_kill_uses_the_configured_idle_limit(monkeypatch, capsys):
    """單輪的閒置上限是**設定值**，不是寫死的數字（2026-09-19 前寫死 300s）。

    讀取迴圈要在呼叫時讀 `DOROSSI_CC_IDLE_LIMIT_SEC`，判定端報出來的也要是同一個
    值——否則 log 上的秒數會跟實際砍掉的時刻對不上。"""
    seen = {}
    real = db._claude_stream_verdict

    def _spy(state, rc, err, session_id, **kw):
        seen.update(kw)
        return real(state, rc, err, session_id, **kw)

    configured = 0.07
    monkeypatch.setattr(db, "DOROSSI_CC_IDLE_LIMIT_SEC", configured)
    monkeypatch.setattr(db, "_dorossi_cc_hard_limit_sec", lambda: FAST_HARD)
    monkeypatch.setattr(db, "_claude_stream_verdict", _spy)
    proc = _LingeringProc([_RESULT_LINE])
    _install(monkeypatch, proc)

    started = _now()
    answer, _sid, _info = _run(db._dorossi_via_claude_code("問題", None))
    elapsed = _now() - started
    assert answer == "答案", answer
    assert seen.get("idle_limit") == configured, seen
    # 下限照量（負載只會讓它更慢，不會誤紅）；「是閒置監看、不是硬上限」看留下的那一行。
    assert configured * 0.8 <= elapsed, (
        f"{elapsed:.2f}s——比設定的閒置上限（{configured}s）還早就砍了")
    err = capsys.readouterr().err
    assert "idle-killed" in err and "hard-killed" not in err, (
        f"閒置監看沒有照設定值（{configured}s）開火：{err[-400:]!r}")


# ---------------------------------------------------------------------------
# 自走模式的沉默看門狗：背景工作壓得住，但壓不過一輪的牆鐘上限（2026-09-19）
#
# 擁有者：「現在等待太短了，任務一直被殺掉」。自走模式原本只有一條規則——
# `silence_limit` 秒沒有任何輸出就砍，**連工具還在跑也砍**（那是刻意的：卡死的前景
# 工具不能讓無人值守的迴圈永遠卡住）。可是等一個背景 subagent、背景 shell 的回合，
# 那段沉默是正當的等待。現在：CLI 回報背景工作時沉默不砍，但只撐到這一輪開始後
# `_dorossi_cc_hard_limit_sec()` 秒；前景工具照舊不算。
# ---------------------------------------------------------------------------
FAST_SILENCE = 0.05


def _silence_kill_time(monkeypatch, lines, *, silence=FAST_SILENCE, hard=FAST_HARD,
                       stdout=None):
    """跑一輪自走模式的讀取迴圈，回傳 (丟出的例外, 第一次 kill 的時刻, 假行程)。

    量的是**砍下去的那一刻**，不是整支函式的牆鐘：kill 之後還有收尾（假 stderr 永遠
    不 EOF，`_dorossi_drain_stderr` 會等滿收尾上限），把那段算進來會讓「沒有被壓住」
    與「被壓到上限」兩種結果的時間差縮到分不出來。

    另外記下 `proc.timeouts`：讀取迴圈等滿一次沉默上限、什麼都沒讀到的次數。「第一次沉默
    就砍」與「被背景工作一路壓到上限」用這個數字分，**不看牆鐘的上限**——負載重的時候每次
    等待只會變長、次數只會變少，所以次數的上限不會誤紅；耗時的上限會（2026-09-22 在一輪
    跑了 10 分鐘的整套裡，同一族的另一支就這樣紅過）。"""
    monkeypatch.setattr(db, "_dorossi_cc_hard_limit_sec", lambda: hard)
    proc = _LingeringProc(lines)
    if stdout is not None:
        proc.stdout = stdout
    proc.timeouts = 0
    real_readline = db._dorossi_readline_watched

    async def _counted_readline(stream, timeout, clock):
        try:
            return await real_readline(stream, timeout, clock)
        except asyncio.TimeoutError:
            proc.timeouts += 1
            raise

    monkeypatch.setattr(db, "_dorossi_readline_watched", _counted_readline)
    kill_times = []
    real_kill = proc.kill

    def _timed_kill():
        kill_times.append(_now())
        real_kill()

    proc.kill = _timed_kill
    _install(monkeypatch, proc)

    async def _call():
        try:
            await db._dorossi_via_claude_code("問題", "sess-abc", silence_limit=silence)
        except db._DorossiLoopSilence as error:
            return error
        return None

    started = _now()
    got = _run(_call())
    assert kill_times, "行程從頭到尾沒有被 kill"
    return got, kill_times[0] - started, proc


def test_a_background_task_holds_off_the_loop_silence_kill_until_the_ceiling(
        monkeypatch):
    """背景工作在 → 沉默逾時不砍，一路撐到這一輪的牆鐘上限才砍。

    兩個方向都在這一支：少了壓制，這裡在 `FAST_SILENCE` 就被砍（太早）；少了上限，
    這裡永遠不會結束（`_run` 的牆鐘把它改寫成 AssertionError）。"""
    got, elapsed, proc = _silence_kill_time(monkeypatch, [_bg_line("b1")])
    assert isinstance(got, db._DorossiLoopSilence), got
    assert elapsed >= FAST_HARD * 0.9, f"{elapsed:.2f}s——沒等到上限就砍了"
    # 上限之後不再拖：到上限為止最多等 ⌈上限／沉默⌉ 次，再多一次是碰到上限的那一次。
    most = math.ceil(FAST_HARD / FAST_SILENCE) + 1
    assert 2 <= proc.timeouts <= most, f"逾時 {proc.timeouts} 次（最多 {most}）——上限之後又拖了"


def test_a_foreground_tool_does_not_hold_off_the_loop_silence_kill(monkeypatch):
    """前景工具（pending tool_use）**不**算：卡死的前景工具正是這層 backstop 要擋的
    東西。把壓制條件寫成 `pending_tools or background_tasks` 的話這支會紅。"""
    tool_use = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash",
         "input": {"command": "sleep 99999"}}]}}).encode("utf-8") + b"\n"
    got, _elapsed, proc = _silence_kill_time(monkeypatch, [tool_use])
    assert isinstance(got, db._DorossiLoopSilence), got
    assert proc.timeouts == 1, f"逾時 {proc.timeouts} 次才砍——前景工具不該壓住沉默看門狗"


def test_a_cleared_task_snapshot_restores_the_loop_silence_kill(monkeypatch):
    got, _elapsed, proc = _silence_kill_time(
        monkeypatch, [_bg_line("b1"), _bg_line()])
    assert isinstance(got, db._DorossiLoopSilence), got
    assert proc.timeouts == 1, f"逾時 {proc.timeouts} 次才砍——空快照沒有把沉默看門狗放回來"


class _TimedStdout:
    """照預定時刻吐出每一行（相對於第一次 readline），之後不再輸出、直到被 kill。

    被 `wait_for` 取消時那一行**留在排程裡**——跟真的 `StreamReader.readline`
    一樣，逾時不會吃掉還沒到的資料。"""

    def __init__(self, schedule):
        self._schedule = list(schedule)
        self._t0 = None
        self.cut = asyncio.Event()

    async def readline(self):
        loop = asyncio.get_running_loop()
        if self._t0 is None:
            self._t0 = loop.time()
        if self._schedule:
            at, raw = self._schedule[0]
            delay = self._t0 + at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._schedule.pop(0)
            return raw
        await self.cut.wait()
        return b""


def test_the_loop_silence_kill_never_lands_earlier_than_it_used_to(monkeypatch):
    """放寬必須是**嚴格**放寬：任何一輪被砍的時刻，都不能早於「最後一行輸出之後
    再等滿 silence_limit」——那是舊行為的砍法。

    這一支專門擋一個看起來更精確的寫法：壓制期間把等待縮成
    `min(silence_limit, 離上限剩餘)`。那會讓一個**剛剛才有輸出**的回合在上限那一刻
    被砍——比舊行為還早。這裡讓第二行輸出落在上限前夕，正確實作要再等滿一個
    silence_limit 才砍。"""
    silence, hard, last_line_at = 0.3, 0.45, 0.4
    stdout = _TimedStdout([(0.0, _bg_line("b1")), (last_line_at, _bg_line("b1"))])
    got, elapsed, _proc = _silence_kill_time(
        monkeypatch, [], silence=silence, hard=hard, stdout=stdout)
    assert isinstance(got, db._DorossiLoopSilence), got
    assert elapsed >= last_line_at + silence * 0.85, (
        f"{elapsed:.2f}s——最後一行在 {last_line_at}s，砍的時刻比舊行為"
        f"（{last_line_at + silence}s）還早")


def test_the_watchdog_defaults_are_the_lengthened_ones():
    """2026-09-19 擁有者：「現在等待太短了，任務一直被殺掉」。這幾個預設就是那一次
    決定的數字；改它們要是一個看得見的決定，不是順手。純聊天的硬上限刻意沒動。"""
    import _bot_config as bc
    d = bc._DEFAULT_BOT_CONFIG
    assert d["dorossi_cc_hard_limit_full_sec"] == 10800.0
    assert d["dorossi_cc_hard_limit_off_sec"] == 900.0
    assert d["dorossi_cc_idle_limit_sec"] == 600.0
    assert d["dorossi_loop_silence_limit_sec"] == 1800.0
    # 關係比數字更重要：閒置要比兩個硬上限都短（否則那一層永遠沒機會開火），
    # 自走沉默要比 full 的上限短（否則「背景工作壓得住，但只撐到上限」這條規則
    # 形同虛設——上限到的時候沉默看門狗本來就還沒開火）。
    assert d["dorossi_cc_idle_limit_sec"] < d["dorossi_cc_hard_limit_off_sec"]
    assert d["dorossi_cc_idle_limit_sec"] < d["dorossi_cc_hard_limit_full_sec"]
    assert d["dorossi_loop_silence_limit_sec"] < d["dorossi_cc_hard_limit_full_sec"]


def test_the_idle_limit_is_wired_from_the_config():
    """模組常數必須來自載入的設定，不是另一份寫死的數字。"""
    assert db.DOROSSI_CC_IDLE_LIMIT_SEC == db.BOT_CONFIG["dorossi_cc_idle_limit_sec"]
    assert db.DOROSSI_LOOP_SILENCE_LIMIT_SEC == \
        db.BOT_CONFIG["dorossi_loop_silence_limit_sec"]
    assert db.DOROSSI_CC_HARD_LIMIT_FULL_SEC == \
        db.BOT_CONFIG["dorossi_cc_hard_limit_full_sec"]


def _load_with(tmp_path, monkeypatch, overrides):
    import _bot_config as bc
    cfg = tmp_path / "bot_config.json"
    cfg.write_text(json.dumps(overrides), encoding="utf-8")
    monkeypatch.setattr(bc, "BOT_CONFIG_FILE", cfg)
    return bc.load_bot_config()


@pytest.mark.parametrize("bad", [0, -5, 10, 59.9])
def test_a_too_small_idle_limit_is_clamped_up(bad, tmp_path, monkeypatch):
    """太小的閒置上限會把每一個需要先想一下的回答砍掉——夾到下限，不能被設定關掉。"""
    import _bot_config as bc
    loaded = _load_with(tmp_path, monkeypatch, {"dorossi_cc_idle_limit_sec": bad})
    assert loaded["dorossi_cc_idle_limit_sec"] == bc.DOROSSI_CC_HARD_LIMIT_FLOOR_SEC


@pytest.mark.parametrize("bad", [None, "600", True, float("inf"), float("nan")])
def test_an_unusable_idle_limit_falls_back_to_the_default(bad, tmp_path, monkeypatch):
    """`inf` 照收等於把閒置那一層關掉；型別錯的值退回預設。"""
    loaded = _load_with(tmp_path, monkeypatch, {"dorossi_cc_idle_limit_sec": bad})
    assert loaded["dorossi_cc_idle_limit_sec"] == 600.0


def test_a_reasonable_idle_limit_gets_through(tmp_path, monkeypatch):
    """反面對照組：上面兩支若只是在測「永遠回預設／永遠回下限」，這支會紅。"""
    loaded = _load_with(tmp_path, monkeypatch, {"dorossi_cc_idle_limit_sec": 1200})
    assert loaded["dorossi_cc_idle_limit_sec"] == 1200.0


# ---------------------------------------------------------------------------
# 非預期離開路徑
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("exc", [
    # 單行 NDJSON 超過 16MB 緩衝上限。`StreamReader.readline` 把 `readuntil` 的
    # `LimitOverrunError` 轉成 `ValueError`（CPython 3.14 原始碼實測），而迴圈裡
    # 只接 `asyncio.TimeoutError`。`full` 模式下工具讀一個大檔就做得到。
    ValueError("Separator is not found, and chunk exceed the limit"),
    # 管線斷掉：transport 把例外 set 進 reader，下一次 readline 就丟出來。
    ConnectionResetError("pipe closed"),
])
def test_an_unexpected_readline_error_kills_the_process(monkeypatch, exc):
    """`readline()` 丟出非逾時的例外時，行程要被砍掉、抽水任務要被收掉。

    **判準是機制不是型別**：迴圈只接 `asyncio.TimeoutError`，所以「任何其他例外」
    都會直接逸出整支函式。舊寫法在這條路上留下一個還在跑的後端行程（`full` 模式下
    它握著主機的 shell）＋一個沒人收的 stderr 抽水任務，而呼叫端此時握著 session 鎖。
    """
    proc = _FakeProc(readline_raises=exc)
    _install(monkeypatch, proc)

    leftovers = []
    with pytest.raises(type(exc)):
        _run(db._dorossi_via_claude_code("問題", None), leftovers=leftovers)

    assert proc.kills >= 1, "例外逸出時行程沒有被 kill"
    assert leftovers == [], f"stderr 抽水任務變成孤兒了：{leftovers}"


def test_an_unexpected_exit_kills_at_once_not_after_the_reap_timeout(monkeypatch):
    """非預期離開時的 kill 必須是**同步**的，不是等收尾那個 timeout 到期才動手。

    這一支測的是「多快」而不只是「有沒有」，因為兩件事都會讓 `kills >= 1` 成立：
    `except` 裡那個同步的 kill，以及 `_dorossi_reap_proc` 在第一階段逾時後補的 kill。
    只斷言 `kills >= 1` 的話，把同步那個拿掉照樣全綠——而拿掉的代價是實際的：一個
    `full` 模式的後端行程會在主機上多跑一整個 timeout（正式值 10 秒），而在取消／
    關機路徑上更可能根本輪不到那個 await 執行。

    量法：**收尾開始的那一刻，行程已經被 kill 過**。有同步 kill → 進 `_dorossi_reap_proc`
    時 `kills >= 1`；沒有 → 進去時還是 0，要等它第一階段逾時才補。原本量的是整趟的牆鐘
    時間（`< FAST_REAP_TIMEOUT`，0.25 秒），那在重負載的整套裡會誤紅；順序不會。

    這裡刻意讓 stderr **立刻 EOF**，把 drain 的那一份時間從量測裡拿掉——否則兩種情況
    都各多一個 timeout，訊號被稀釋成一個很窄的邊界。要隔離的是 kill 的延遲。
    """
    proc = _FakeProc(readline_raises=ValueError("limit"))

    async def _eof_read():
        return b""

    proc.stderr.read = _eof_read
    _install(monkeypatch, proc)
    real_reap = db._dorossi_reap_proc
    kills_when_reaping: list = []

    async def _reap(target, *args, **kwargs):
        kills_when_reaping.append(target.kills)
        return await real_reap(target, *args, **kwargs)

    monkeypatch.setattr(db, "_dorossi_reap_proc", _reap)

    with pytest.raises(ValueError):
        _run(db._dorossi_via_claude_code("問題", None))

    assert proc.kills >= 1, "例外逸出時行程沒有被 kill"
    assert kills_when_reaping, "收尾沒有經過 `_dorossi_reap_proc`——這支的前提變了"
    assert kills_when_reaping[0] >= 1, (
        "收尾開始時行程還沒被 kill——是等到 reap 逾時才補的，非預期離開路徑上那個"
        "同步的 kill 不見了")


def test_a_raising_progress_callback_is_absorbed_rather_than_escaping(monkeypatch):
    """`on_text` 丟例外**不會**逸出——這一條是複驗，結論跟直覺相反。

    原本以為回呼是第三條非預期離開路徑，實測不是：`_ClaudeStreamState.feed` 自己把
    `on_text` 包在 try/except 裡（「進度更新失敗絕不影響主串流」），那是它「永遠不
    raise」承諾的一部分。這支測試把那個依賴寫下來——收尾的保護**不依賴**它，但如果
    哪天有人把 `feed` 裡那個 except 拿掉，這裡會提醒他那是一條新的逸出路徑。
    """
    calls = []

    def _boom(text):
        calls.append(text)
        raise RuntimeError("callback exploded")

    proc = _FakeProc(stdout_lines=[
        json.dumps({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text"}}}).encode() + b"\n",
        json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "嗨"}}}).encode() + b"\n",
        _RESULT_LINE,
    ], exited_rc=0)
    _install(monkeypatch, proc)

    answer, _sid, _info = _run(
        db._dorossi_via_claude_code("問題", None, on_text=_boom))
    assert calls, "回呼根本沒被呼叫到，這支測試什麼都沒證明"
    assert answer == "答案", "回呼炸掉卻把整輪打掉了"


def test_cancellation_kills_the_process_and_collects_the_reader(monkeypatch):
    """abort／關機把 `CancelledError` 打進 readline 的等待點。

    `CancelledError` 是 `BaseException`，所以任何 `except Exception` 都接不到它——
    舊寫法在這裡直接展開，行程沒被殺、抽水任務變孤兒。**kill 必須是同步的**：取消
    路徑上不保證還有機會跑完任何 await。
    """
    proc = _FakeProc()
    proc.stdout = _NeverStream()          # readline 也不返回 → 停在等待點

    async def _readline_forever():
        await asyncio.get_running_loop().create_future()

    proc.stdout.readline = _readline_forever
    _install(monkeypatch, proc)

    async def _scenario():
        task = asyncio.ensure_future(db._dorossi_via_claude_code("問題", None))
        await asyncio.sleep(0.05)         # 讓它跑到 readline 的等待點
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task

    leftovers = []
    _run(_scenario(), leftovers=leftovers)
    assert proc.kills >= 1, "被取消時行程沒有被 kill"
    assert leftovers == [], f"取消路徑上留下了孤兒任務：{leftovers}"


# ---------------------------------------------------------------------------
# 「有上限」與「收得乾淨」是兩件事
# ---------------------------------------------------------------------------
def test_the_capture_helper_can_actually_see_an_orphan_task():
    """正面對照組：先證明 `leftovers` 真的抓得到孤兒，否則下面那支是空的斷言。

    這一族的每一支都以 `leftovers == []` 收尾，而一個永遠回空 list 的量測方式會讓
    它們全部無條件為真——`asyncio.all_tasks()` 少了正確的呼叫時機就正是那樣（見
    `_run` 的 docstring）。所以這裡刻意漏一個任務出去，它必須被看見。

    這支的第一版當場就紅了，理由值得留著：漏出去的是 `ensure_future(<Future>)`，而
    **`ensure_future` 拿到 Future 是原樣回傳、不會包成 Task**，`all_tasks()` 只列
    Task，所以那個「孤兒」根本不在名單裡。要漏就要漏一個真的 coroutine——被測的兩個
    `ensure_future(proc.wait())` / `ensure_future(_read_stream_all(...))` 收的都是
    coroutine，確實是 Task。
    """
    async def _forever():
        await asyncio.get_running_loop().create_future()

    async def _leaks():
        asyncio.ensure_future(_forever())
        await asyncio.sleep(0)

    leftovers = []
    _run(_leaks(), leftovers=leftovers)
    assert leftovers, "量測方式看不到孤兒任務，這一族的 `leftovers == []` 全是空話"


def test_the_bounded_waits_collect_their_tasks_instead_of_orphaning_them(
        monkeypatch):
    """收尾的兩個上限都逾時（行程收不掉、stderr 也拿不到）→ 兩個任務都要被收乾淨。

    這是**唯一**同時走到 `_dorossi_reap_proc` 與 `_dorossi_drain_stderr` 兩個
    `finally` 的情境：行程 kill 不死所以 reap 兩階段都逾時，stderr 的 EOF 永遠不來
    所以 drain 也逾時。兩支的 docstring 都寫著「只加上限不收任務，等於把『永遠卡住』
    換成『孤兒任務』」——這一支就是那句話的執行者。

    為什麼要獨立一支而不是併進
    `test_a_process_that_will_not_die_is_reported_as_a_failure`：那一支問的是「判定對
    不對」（rc 拿不到要走非零離開分類），這一支問的是「資源收乾淨了沒」。兩個規則
    共用一個情境，但改壞其中一個不會讓另一個轉紅，所以要兩支。
    """
    proc = _FakeProc(stdout_lines=[], unkillable=True)
    _install(monkeypatch, proc)

    leftovers = []
    with pytest.raises(RuntimeError):
        _run(db._dorossi_via_claude_code("問題", None), leftovers=leftovers)

    assert leftovers == [], (
        f"收尾逾時之後把任務丟著不管：{leftovers}。"
        "`_dorossi_reap_proc` / `_dorossi_drain_stderr` 的 `finally` 要 `cancel()` "
        "＋ `await asyncio.gather(t, return_exceptions=True)`，否則只是把"
        "「永遠卡住」換成「孤兒任務」。")


# ---------------------------------------------------------------------------
# codex 那一側：同一個缺陷，同一個修法
# ---------------------------------------------------------------------------
def test_codex_abnormal_teardown_is_bounded_too(monkeypatch, tmp_path):
    """codex 的**逾時分支**與 `finally` 原本各有一個裸的 `await proc.wait()`。

    它的 `finally` 註解已經寫了「行程還活著就直接 await 會永遠卡住」，但那個守衛是
    `if proc.returncode is None` ——而實測顯示**行程死掉之後 `wait()` 一樣會卡**（它
    等的是管線 EOF，不是 returncode），所以守衛擋不住這條路；逾時分支那一行更是連
    守衛都沒有。

    這裡走的是輸出靜默那條：readline 一直不返回 → `silence_limit` 逾時 → kill →
    收尾。舊寫法會停在 kill 後面那個裸的 `await proc.wait()`，而**自走迴圈**正是靠
    這條路把卡住的一輪停掉的，所以它掛掉等於那個 backstop 整個失效。
    """
    monkeypatch.setattr(db, "find_codex_executable", lambda: r"C:\fake\codex.exe")
    monkeypatch.setattr(db, "_collect_codex_images", lambda *_a, **_k: [])

    async def _readline_forever():
        await asyncio.get_running_loop().create_future()

    proc = _FakeProc()
    proc.stdout.readline = _readline_forever
    _install(monkeypatch, proc)

    leftovers = []
    with pytest.raises(db._DorossiLoopSilence):
        _run(db._dorossi_via_codex("問題", None, workdir=str(tmp_path),
                                   silence_limit=FAST_REAP_TIMEOUT),
             leftovers=leftovers)
    assert proc.kills >= 1, "逾時之後行程沒有被 kill"
    assert leftovers == [], f"codex 這一側留下了孤兒任務：{leftovers}"


# ---------------------------------------------------------------------------
# 結構守門：「等待條件不在我們手上」的 await 一律要有上限（**全專案**）
#
# 這一段守的是一條**跟模組無關**的規則，卻住在一個以 dorossi 命名的檔案裡，理由
# 有二，兩個都不是「懶得搬」：
#
# 1. 規則的**行為**那一半需要這裡的平台替身（`_FakeProc` / `_NeverStream` /
#    `_run(leftovers=)`）。`_FakeProc.wait()` 的語意（等管線 EOF、`returncode`
#    卻早就設好）是這條規則存在的唯一理由，抄一份到別的檔案就等著兩份分岔——本
#    repo 已經為「抄一份上限到假 port」付過學費（§8.10）。
# 2. 規則指向的兩支有上限 helper（`_dorossi_reap_proc` /
#    `_dorossi_drain_stderr`）就住在 `dorossi_backend.py`。
#
# 檔名記的是**它是在哪裡被發現的**，不是它的範圍。範圍由
# `test_the_unbounded_wait_rule_is_enforced_outside_dorossi_backend` 另外釘住，
# 所以檔名不可能安靜地把範圍縮回去。
#
# 這一段原本寫死 `_TEARDOWN_FUNCS = ("_dorossi_via_claude_code",
# "_dorossi_via_codex")`，於是同一個缺陷的另外三個現場（`discord_bot` 的兩個用量
# 查詢、`presence_probe` 的 SMTC 探測）從它底下大搖大擺走過去——
# ---------------------------------------------------------------------------
_MODULE_FLOOR = 25          # 抽不到檔案時「零筆違規」跟「全部乾淨」長得一模一樣

# 把等待包起來的構造。名字出現在**呼叫的 func 位置**就算，它引數裡的節點視為
# 「已被包住」。`gather` / `shield` 在這裡算包住，是因為本 repo 規定的收法就是
# `cancel()` ＋ `await asyncio.gather(t, return_exceptions=True)`。
_BOUNDING_CALLS = frozenset({
    "wait_for", "gather", "shield", "timeout", "timeout_at",
    "_dorossi_reap_proc", "_dorossi_drain_stderr",
    "_reap_usage_query_proc", "_reap_timed_out_probe",
})
# asyncio 子行程的管線屬性。這是**機制**不是命名習慣：`create_subprocess_exec`
# 只會把管線掛在這三個名字上。
_PIPE_ATTRS = frozenset({"stdout", "stderr", "stdin"})
_SPAWNERS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})

# 刻意的豁免。**列管而不是放寬判準**：放寬判準會連下一個真的缺陷一起放掉。
# 每一筆都要寫「為什麼它不是缺陷」，而且兩個方向都對帳（見
# `test_the_unbounded_wait_roster_has_no_stale_entries`）。
_UNBOUNDED_WAIT_EXEMPT = {
    ("discord_bot.py", "_reap_oneshot_webrunner"):
        "這支的**工作**就是等 one-shot 背景程式結束（等完才 reset globals ＋ 清 "
        "PID 檔），有上限反而是缺陷。而且它等的是 `subprocess.Popen.wait`（同步、"
        "丟進 executor），Windows 上那是對**行程 handle** 的 WaitForSingleObject，"
        "與管線 EOF 無關——跟這條規則的機制不同。`/stop` 與 `/run` 都會 cancel 它。",
    ("dorossi_backend.py", "_read_stream_all"):
        "刻意沒有自己的上限：它就是「把管線抽乾」那一半，上限由**呼叫端**的 "
        "`_dorossi_drain_stderr` 提供（它的 docstring 明寫「不要在任何地方直接 "
        "`await` 它」）。把上限塞進來會讓兩層都在猜同一個逾時。",
}


def _project_module_asts() -> list:
    """專案自己的**非測試**模組 → `[(檔名, AST)]`。

    用 glob 而不是列舉：列舉是 fail-open 的（下一個新模組不會自動被蓋到）。
    呼叫端要自己斷言數量下限。`legacy/` 不含在內（CLAUDE.md：唯讀參考）。
    """
    root = PKG_ROOT.parent
    # `test/`（本檔所在的目錄）照同一個判準過濾：`conftest.py` 與手動 e2e 腳本在
    # 2026-09-22 之前住在套件裡、在範圍內，搬家之後照舊。
    tests = Path(__file__).resolve().parent
    out = []
    for path in (sorted(PKG_ROOT.glob("*.py")) + sorted(tests.glob("*.py"))
                 + sorted(root.glob("*.py"))):
        if path.name.startswith("test_") or path.name == "__init__.py":
            continue
        try:
            out.append((path.name,
                        ast.parse(path.read_text(encoding="utf-8"), path.name)))
        except (SyntaxError, UnicodeDecodeError):     # pragma: no cover
            continue
    return out


def _callee_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def _pipe_parameters(trees) -> dict:
    """哪些函式的哪個**參數**收得到 `X.stdout` / `X.stderr`（由呼叫端推導）。

    為什麼要這一步：只看名字的掃描只認得 `await proc.stderr.read()`，看不到
    `_read_stream_all(proc.stderr)` 裡面的 `await stream.read()`——呼叫端寫的是
    常數、寫入端寫的是參數，**兩行單獨看都不像有問題**。`test_atomic_writes` 為
    了完全一樣的形狀付過一次學費（`events.ndjson` 的輪替因此漏檢），這裡直接照它
    的結論做：守門要跟著參數走。

    `trees` 是 `[(名字, AST)]`；推導跨檔案，所以呼叫端與定義端不必同一個模組。
    """
    passed = {}
    for _name, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee_name(node)
            for index, arg in enumerate(node.args):
                if isinstance(arg, ast.Attribute) and arg.attr in _PIPE_ATTRS:
                    passed.setdefault(callee, set()).add(index)
    out = {}
    for _name, tree in trees:
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            indexes = passed.get(fn.name)
            if not indexes:
                continue
            params = fn.args.posonlyargs + fn.args.args
            for index in indexes:
                if index < len(params):
                    out.setdefault(fn.name, set()).add(params[index].arg)
    return out


def _has_timeout_kwarg(call: ast.Call) -> bool:
    return any(kw.arg == "timeout" for kw in call.keywords)


def _unbounded_process_awaits(tree: ast.AST, pipe_params=None) -> list:
    """列出「完成條件不在我們手上」而又沒有上限的 `await` → `[(函式, 形狀, 行號)]`。

    判準是**機制**：這些 await 要等的是子行程離開或管線 EOF，而**兩者都由別人
    決定**（實測見 `_dorossi_reap_proc` 的 docstring：孫行程握著寫端時
    `await proc.wait()` 永遠不返回，`kill()` 也救不了）。認的形狀：

    * `await X.wait()` / `await X.communicate()`——沒有 `timeout=` 的那些。帶
      `timeout=` 的（例如 `asyncio.wait({t}, timeout=...)`）依機制就是有上限的。
    * `await <管線>.read()`——`X.stdout` / `X.stderr`，或由 `_pipe_parameters`
      推導出來、收得到管線的參數。**HTTP 的 `.read()` 不算**（`attachment.read()`
      / `content.read(n)`）：那一族的上限在 session 層的 `ClientTimeout`，把它們
      掃進來只會製造 5 筆假陽性，而會叫狼來了的守門遲早被關掉。
    * `await <task>` / `await <x_task>`——包著上面任一個的任務。**先 `cancel()`
      過的不算**：`cancel()` ＋ `await` 正是本 repo 規定的收法，它的完成條件握在
      我們自己手上。
    * `await loop.run_in_executor(..., X.wait)` / `await asyncio.to_thread(X.wait)`
      ——同一件事的阻塞版（更糟：連執行緒池的位子都被永久佔住）。
    """
    pipe_params = pipe_params or {}
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        wrapped, cancelled = set(), set()
        for sub in ast.walk(fn):
            if not isinstance(sub, ast.Call):
                continue
            callee = _callee_name(sub)
            if callee in _BOUNDING_CALLS:
                for arg in list(sub.args) + [kw.value for kw in sub.keywords]:
                    for inner in ast.walk(arg):
                        wrapped.add(id(inner))
            if (callee == "cancel" and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name)):
                cancelled.add(sub.func.value.id)
        for sub in ast.walk(fn):
            if not isinstance(sub, ast.Await) or id(sub.value) in wrapped:
                continue
            value = sub.value
            shape = None
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                attr = value.func.attr
                if attr in ("wait", "communicate") and not _has_timeout_kwarg(value):
                    shape = f"{attr}()"
                elif attr == "read" and _is_pipe_expr(value.func.value, fn.name,
                                                      pipe_params):
                    shape = "pipe.read()"
                elif attr in ("run_in_executor", "to_thread"):
                    for index, arg in enumerate(value.args):
                        if not (isinstance(arg, ast.Attribute)
                                and arg.attr == "wait"):
                            continue
                        # `to_thread(fn, 10)` / `run_in_executor(None, fn, 10)`
                        # 把 10 交給 `fn` ——對 `subprocess.Popen.wait` 來說那
                        # **就是** timeout（它收位置引數，不是關鍵字），所以有
                        # 東西接在後面就是有上限。`_process_control` 的
                        # `to_thread(tracked_proc.wait, 10)` 正是這一種，判準沒
                        # 有這一半的話它會是假陽性。
                        rest = value.args[index + 1:]
                        if rest or _has_timeout_kwarg(value):
                            break
                        shape = f"{attr}(.wait)"
                        break
            elif isinstance(value, ast.Name):
                if ((value.id == "task" or value.id.endswith("_task"))
                        and value.id not in cancelled):
                    shape = f"bare task `{value.id}`"
            if shape:
                hits.append((fn.name, shape, sub.lineno))
    return hits


def _is_pipe_expr(node, fn_name: str, pipe_params: dict) -> bool:
    if isinstance(node, ast.Attribute) and node.attr in _PIPE_ATTRS:
        return True
    return (isinstance(node, ast.Name)
            and node.id in pipe_params.get(fn_name, frozenset()))


def _scan_project() -> tuple:
    """-> (模組數, {(模組, 函式): [(形狀, 行號)]})。"""
    trees = _project_module_asts()
    pipe_params = _pipe_parameters(trees)
    found = {}
    for name, tree in trees:
        for fn, shape, line in _unbounded_process_awaits(tree, pipe_params):
            found.setdefault((name, fn), []).append((shape, line))
    return len(trees), found


def test_no_unbounded_process_wait_survives_anywhere():
    """全專案：起了子行程／接了管線之後，不得有沒有上限的 `await`。

    這條規則跟模組無關，所以掃**所有**非測試模組。失敗形態是最難查的那一種——
    不是報錯，是那一則回覆永遠不來、而外面的 `except Exception` 完全接不到（掛住
    不是例外）。
    """
    module_count, found = _scan_project()
    # 正面對照組：抽不到檔案時「零筆違規」跟「全部乾淨」在輸出上一模一樣。
    assert module_count >= _MODULE_FLOOR, (
        f"只抽到 {module_count} 個模組，`_project_module_asts()` 壞了——"
        "這支守門會退化成永遠會過。")
    offenders = {k: v for k, v in found.items() if k not in _UNBOUNDED_WAIT_EXEMPT}
    assert not offenders, (
        f"收尾路徑上出現沒有上限的等待：{sorted(offenders)}。\n"
        "`await proc.wait()` 等的是**管線 EOF** 不是行程結束——孫行程握著寫端時它"
        "永遠不返回，而 `if proc.returncode is None: kill()` 這種守衛擋的是別的"
        "東西。改走 `_dorossi_reap_proc` / `_dorossi_drain_stderr`（或該模組自己"
        "的有上限收尾），逾時之後一律 `cancel()` ＋ "
        "`await asyncio.gather(t, return_exceptions=True)`。\n"
        "真的是刻意的，就寫進 `_UNBOUNDED_WAIT_EXEMPT` 並說明為什麼。")


def test_the_unbounded_wait_roster_has_no_stale_entries():
    """豁免名單兩個方向都要對帳，而且它同時是**函式範圍**的釘子。

    正向：一筆豁免對應的現場如果被修好（或函式改名／搬走），這筆就變成一張留給
    未來回歸的免死金牌——`CLAUDE.md` 對 `_OWNER_ONLY_SLASH` 寫的同一條，具名例外
    fail-open。

    反向（這一支真正的牙齒）：名單裡兩筆都**不在**舊的
    `("_dorossi_via_claude_code", "_dorossi_via_codex")` 裡，所以只要有人把掃描
    範圍縮回那兩個函式，兩筆豁免會同時變成孤兒 → 這裡轉紅。模組層級的縮窄由
    下一支負責，函式層級的縮窄由這一支負責。
    """
    module_count, found = _scan_project()
    assert module_count >= _MODULE_FLOOR, module_count
    assert _UNBOUNDED_WAIT_EXEMPT, "豁免名單空了，這支就變成空斷言"
    stale = sorted(set(_UNBOUNDED_WAIT_EXEMPT) - set(found))
    assert not stale, (
        f"這些豁免已經沒有對應的現場了：{stale}。要嘛它被修好了（刪掉這筆），"
        "要嘛函式改名／搬家了（改掉這筆），要嘛掃描範圍被縮窄了（那才是問題）。")


# ---------------------------------------------------------------------------
# 上面那三支各自有一道「只在別的東西壞掉時才會說話」的下限／對帳。**在乾淨的真實
# 資料上，那幾行是量不出來的**：變異測試實測把三者分別放寬（模組下限改 0、掃描器
# 輸出下限改 0、反向對帳改成永遠空）時，六支測試照樣全綠。理由跟 §8.8 同一條——
# 真實資料乾淨時，一道正確的防線和一道被拆掉的防線在輸出上一模一樣。
#
# 所以每一道都給它一個把前提打壞的對照組。做法是替換模組層的取樣函式而不是動磁碟
# （`monkeypatch` 進 `sys.modules[__name__]`），所以不會跟正在跑的別的測試打架。
# ---------------------------------------------------------------------------
def _patch(monkeypatch, name, value):
    monkeypatch.setattr(sys.modules[__name__], name, value)


def test_the_module_floor_fires_when_the_enumerator_comes_back_empty(monkeypatch):
    """抽不到模組時「零筆違規」＝「全部乾淨」，所以下限必須自己會叫。"""
    _patch(monkeypatch, "_scan_project", lambda: (0, {}))
    with pytest.raises(AssertionError) as excinfo:
        test_no_unbounded_process_wait_survives_anywhere()
    assert "只抽到 0 個模組" in str(excinfo.value), excinfo.value


def test_the_scanner_output_floor_fires_when_the_shapes_stop_matching(monkeypatch):
    """掃描器認不出任何形狀時，也要能跟「專案變乾淨了」分開。

    模組數量正常（30）但一筆都沒認出來——這正是形狀比對壞掉的樣子。
    """
    _patch(monkeypatch, "_scan_project", lambda: (30, {}))
    with pytest.raises(AssertionError) as excinfo:
        test_the_unbounded_wait_rule_is_enforced_outside_dorossi_backend()
    assert "一筆都沒認出來" in str(excinfo.value), excinfo.value


def test_the_roster_reconciliation_fires_when_an_exemption_goes_stale(monkeypatch):
    """豁免的反向對帳：現場不見了，那筆豁免就必須被指出來。

    具名例外是 fail-open 的（`CLAUDE.md` 對 `_OWNER_ONLY_SLASH` 寫的同一條），
    所以「沒有過期條目」這件事本身要有東西在執行，不能只寫在註解裡。
    """
    _patch(monkeypatch, "_scan_project", lambda: (30, {}))
    with pytest.raises(AssertionError) as excinfo:
        test_the_unbounded_wait_roster_has_no_stale_entries()
    assert "已經沒有對應的現場了" in str(excinfo.value), excinfo.value


def test_the_unbounded_wait_rule_is_enforced_outside_dorossi_backend():
    """釘的是**範圍**不是規則：掃描一定要蓋到 `dorossi_backend.py` 以外。

    上一支在真實資料上只剩兩筆豁免，所以把掃描縮回只讀 `dorossi_backend.py`
    它照樣全綠——一支範圍正確的守門和一支永遠會過的守門，在輸出上看不出差別
    。

    這裡改成量**這條規則保護的族群**：掃描範圍裡有幾個模組真的在起 asyncio 子
    行程。族群一縮就會紅。
    """
    trees = _project_module_asts()
    assert len(trees) >= _MODULE_FLOOR, len(trees)
    spawners = set()
    for name, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee_name(node) in _SPAWNERS:
                spawners.add(name)
                break
    assert "dorossi_backend.py" in spawners, (
        f"抽取器壞了：後端一定在起子行程，實際抽到 {sorted(spawners)}。")
    assert spawners - {"dorossi_backend.py"}, (
        "掃描範圍裡除了 `dorossi_backend.py` 之外沒有任何模組在起子行程。"
        "要嘛 `_project_module_asts()` 被縮窄了，要嘛起子行程的模組被搬走了；"
        "兩種都要人看一眼，因為上面那兩支守門會因此退化成永遠會過。")
    # 掃描器在真實資料上**確實有輸出**（現在剛好全是豁免）。少了這句，掃描器整個
    # 壞掉（例如形狀比對再也認不出東西）會表現成「全部乾淨」。
    _count, found = _scan_project()
    assert len(found) >= 2, (
        f"掃描器在真實原始碼上一筆都沒認出來（{found}）——它壞了，不是專案變乾淨了。")


def test_the_scanner_catches_every_shape_of_the_unbounded_teardown():
    """正面對照組：五種舊寫法各餵一次，全部都要被抓到。

    乾淨的原始碼會讓上面那三支永遠是綠的——包括掃描器自己壞掉的時候。這一支才是
    它們的牙齒，所以**真實原始碼裡出現過的每一種寫法都要有一個樣本**（本 repo 的
    monotonic 守門就是因為樣本少了一種條件式賦值，清乾淨之後永遠綠）。
    """
    old = (
        "async def a(x):\n"
        "    rc = await proc.wait()\n"                       # discord_bot ×2
        "async def b(x):\n"
        "    out, err = await proc.communicate()\n"
        "async def c(x):\n"
        "    raw = await proc.stderr.read()\n"
        "async def d(stream):\n"
        "    raw = await stream.read()\n"                    # 參數推導那一條
        "async def e(x):\n"
        "    err = (await err_task).decode('utf-8')\n"
        "async def f(x):\n"
        "    rc = await loop.run_in_executor(None, proc.wait)\n"
        "async def g(x):\n"
        "    rc = await asyncio.to_thread(proc.wait)\n"
    )
    hits = _unbounded_process_awaits(ast.parse(old), {"d": {"stream"}})
    shapes = {(fn, shape) for fn, shape, _line in hits}
    for expected in (("a", "wait()"), ("b", "communicate()"),
                     ("c", "pipe.read()"), ("d", "pipe.read()"),
                     ("e", "bare task `err_task`"),
                     ("f", "run_in_executor(.wait)"),
                     ("g", "to_thread(.wait)")):
        assert expected in shapes, f"漏掉 {expected}：{sorted(shapes)}"


def test_the_scanner_accepts_the_bounded_shapes():
    """反方向：包好的寫法一筆都不可以被誤報。

    會叫狼來了的守門是會被關掉的守門（`test_language` / `test_text_encoding` 的
    同一條結論）。這裡列的每一種都在正式原始碼裡真的出現過。
    """
    good = (
        "async def a(x):\n"
        "    rc = await _dorossi_reap_proc(proc)\n"
        "    err = await _dorossi_drain_stderr(err_task)\n"
        "    y = await asyncio.wait_for(proc.communicate(), timeout=45)\n"
        "    z = await asyncio.wait({wait_task}, timeout=0.05)\n"
        "async def b(x):\n"
        "    task.cancel()\n"
        "    await task\n"                                  # cancel 過的
        "async def c(x):\n"
        "    stderr_task.cancel()\n"
        "    await asyncio.gather(stderr_task, return_exceptions=True)\n"
        "async def d(x):\n"
        "    body = await attachment.read()\n"              # HTTP，不是管線
        "    chunk = await content.read(65536)\n"
        "async def e(x):\n"
        # `Popen.wait` 的 timeout 是**位置**引數，所以「有東西接在 callable 後面」
        # 就是有上限。`_process_control._terminate_all_webrunner_instances` 真的
        # 這樣寫，少了這一格它會是假陽性。
        "    rc = await asyncio.to_thread(tracked_proc.wait, 10)\n"
        "    rc2 = await loop.run_in_executor(None, proc.wait, 10)\n"
    )
    hits = _unbounded_process_awaits(ast.parse(good), {})
    assert not hits, f"包好的寫法被誤報了：{hits}"


def test_the_pipe_parameter_derivation_follows_the_call_site():
    """`_pipe_parameters` 自己的對照組：跨檔案、看的是位置不是名字。

    這一半沒有自己的測試的話，把它整個換成 `return {}` 會讓上面的
    `pipe.read()` 那一格安靜地只剩屬性形式——而參數形式正是真實原始碼裡的那一個
    （`_read_stream_all(proc.stderr)`）。
    """
    caller = ast.parse("async def x(p):\n    t = _drain(p.stderr)\n")
    callee = ast.parse("async def _drain(stream):\n    return await stream.read()\n")
    derived = _pipe_parameters([("caller.py", caller), ("callee.py", callee)])
    assert derived.get("_drain") == {"stream"}, derived
    # 位置要對得上：管線傳在第 0 格，就不該把第 1 格的參數當成管線。
    other = ast.parse("async def y(p):\n    t = _two(0, p.stdout)\n")
    two = ast.parse("async def _two(n, pipe):\n    return n\n")
    assert _pipe_parameters([("a.py", other), ("b.py", two)]).get("_two") == {"pipe"}
    # 沒有人傳管線給它 → 不推導（否則每個叫 `stream` 的參數都會變成管線）。
    lone = ast.parse("async def _drain(stream):\n    return await stream.read()\n")
    assert not _pipe_parameters([("c.py", lone)])
