"""自走迴圈本體（`discord_bot._dorossi_run_loop`）——58 行 docstring，0 行被執行過。

這支函式有一份寫得非常完整的規格：docstring 58 行，底下 `Trap / invariant` 那一節
逐條列著六個不變式，正文裡還嵌著三次真實事故的日期與症狀（2026-09-03 連兩次 529
就收工、2026-09-05 一次輸出靜默就停掉整夜任務、2026-09-19 等待期間補的話兩小時後
才生效）。而到 2026-09-20 為止，**250 行裡有 166 行從來沒有被任何測試執行過**。

它是 Dorossi 無人值守長任務的引擎：每一輪之間的每一個判斷——停不停、要不要等、
等完之後從哪裡接、標記留不留——都只發生在這裡，而失敗形態全部是安靜的：迴圈停在
那裡、或者接續到別的對話、或者標記沒清掉讓下次重啟又活過來。沒有一種會拋例外。

測試不連網、不起子行程、**不碰磁碟上的工作階段檔**（`_dorossi_state_rmw` 整支換成
對記憶體 dict 的操作），也不會真的睡——所有等待都走替身並記下秒數。

停止訊號 `_LoopHalt` 刻意繼承 `BaseException`：迴圈體內有一段 `except Exception`
專門吃掉「沒人預料到的錯」並**重試**，所以繼承 `Exception` 的哨兵會被當成一次偶發
失敗、重試、再用盡腳本、再被吃掉——測試不是變紅，是**掛住**。
"""
import ast
import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord_bot as b  # noqa: E402
import dorossi_backend as db  # noqa: E402

UID = "owner"
SID = "s1"
SENTINEL = db.DOROSSI_LOOP_SENTINEL


class _LoopHalt(BaseException):
    """腳本裡的回合用完時由假的 `_dorossi_loop_one_round` 丟出，把迴圈停住。"""


# --------------------------------------------------------------------------
# 夾具
# --------------------------------------------------------------------------

class _FakeLive:
    """`_DorossiLiveMessage` 的替身：只記下每次 finalize／reopen 說了什麼。"""

    def __init__(self, message, prime=None):
        self.message = message
        self.prime = prime
        self.log = None          # 由夾具指定的共用 list

    async def finalize(self, content):
        _FakeLive.sink.append(content)

    def set(self, content):
        _FakeLive.sink.append(content)

    def update(self, content):
        pass

    def reopen(self):
        _FakeLive.sink.append("<reopen>")


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def loop_env(monkeypatch):
    """把 `_dorossi_run_loop` 的每一個對外動作換成替身，回傳可腳本化的 env。

    `env.rounds` 是這次要餵給迴圈的腳本，一個元素一輪：

    * `(answer, new_sid, info)` —— 這一輪正常跑完；
    * 例外實例 —— 這一輪丟那個例外；
    * 可呼叫物 —— 收到 `st`，回傳上面兩種之一（用來在「某一輪進行中」動狀態，
      例如設 abort 或把 slot 刪掉）。

    腳本用完而迴圈還要下一輪 → 丟 `_LoopHalt`，`_run()` 把它記成 `env.halted`。
    所以「迴圈有沒有自己停下來」就是 `halted is False`，而且是可斷言的。
    """
    env = types.SimpleNamespace(
        state={}, rounds=[], played=[], live=[], replies=[], channel_sends=[],
        events=[], waits=[], images=0, halted=False, final_chunks=[],
        abort_during_wait=False, inject_during_wait=[],
    )
    _FakeLive.sink = env.live

    rec = b._dorossi_user_record(env.state, UID)
    rec["sessions"][SID] = {"cc_session_id": "cc-old", "label": "L"}
    rec["active"] = SID

    async def fake_rmw(mutate):
        return mutate(env.state)

    async def fake_safe_reply(message, content=None, **_kwargs):
        env.replies.append(content)
        return types.SimpleNamespace(id=900 + len(env.replies))

    async def fake_channel_send(content=None, **_kwargs):
        env.channel_sends.append(content)

    async def fake_one_round(prompt, snap, live, on_text, silence_limit, st):
        env.played.append((prompt, snap))
        if not env.rounds:
            raise _LoopHalt(f"腳本只有 {len(env.played) - 1} 輪，迴圈要第 "
                            f"{len(env.played)} 輪")
        step = env.rounds.pop(0)
        if callable(step) and not isinstance(step, BaseException):
            step = step(st)
        if isinstance(step, BaseException):
            raise step
        return step

    async def fake_send_images(_message, _paths):
        return env.images

    async def fake_wait(st, delay):
        """所有退避都走這裡，所以測試永遠不會真的睡。

        兩個旋鈕模擬「等待期間發生的事」：擁有者按了 abort，或者補了一句話。
        這兩件事只會在等待**當中**發生，沒有替身就完全測不到。
        """
        env.waits.append(delay)
        if env.inject_during_wait:
            st.injections.extend(env.inject_during_wait)
            env.inject_during_wait = []
        if env.abort_during_wait:
            st.abort = True
            return False
        return not st.abort

    async def fake_refresh(_key):
        return None

    async def fake_reply_final(_message, _live, chunks, **_kwargs):
        env.final_chunks.append(chunks)

    channel = types.SimpleNamespace(id=42, send=fake_channel_send,
                                    typing=lambda: _Typing())
    env.message = types.SimpleNamespace(id=7, channel=channel)
    env.placeholder = types.SimpleNamespace(id=1)

    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "_dorossi_state_rmw", fake_rmw)
    monkeypatch.setattr(b, "safe_reply", fake_safe_reply)
    monkeypatch.setattr(b, "_DorossiLiveMessage", _FakeLive)
    monkeypatch.setattr(b, "_dorossi_loop_one_round", fake_one_round)
    monkeypatch.setattr(b, "_dorossi_send_images", fake_send_images)
    monkeypatch.setattr(b, "_dorossi_wait_for_usage_reset", fake_wait)
    monkeypatch.setattr(b, "_dorossi_refresh_waiters", fake_refresh)
    monkeypatch.setattr(b, "_dorossi_reply_final", fake_reply_final)
    monkeypatch.setattr(b, "_dorossi_event",
                        lambda kind, **data: env.events.append((kind, data)))
    monkeypatch.setattr(b, "_dorossi_resolve_cc_workdir",
                        lambda _s, _u, _i: "workdir")
    monkeypatch.setattr(b, "DOROSSI_BACKEND", "claude_code")
    monkeypatch.setattr(b, "DOROSSI_LOOP_EXHAUSTION_ROUNDS", 2)
    monkeypatch.setattr(b, "DOROSSI_MAX_PARALLEL_LOOPS", 0)
    monkeypatch.setattr(b, "DOROSSI_SILENCE_RETRY_MAX", 1)
    monkeypatch.setattr(b, "DOROSSI_ERROR_RETRY_MAX", 1)
    monkeypatch.setattr(b, "DOROSSI_USAGE_WAIT_MAX_CONSECUTIVE", 0)
    monkeypatch.setattr(b, "DOROSSI_TRANSIENT_MAX_CONSECUTIVE", 1)
    return env


def _run(env, **kwargs):
    """跑到迴圈自己結束，或跑到腳本用完為止。"""
    env.halted = False
    try:
        asyncio.run(b._dorossi_run_loop(env.message, "整理測試",
                                        env.placeholder, UID, SID, **kwargs))
    except _LoopHalt:
        env.halted = True
    return env


def _slot(env):
    return env.state[UID]["sessions"].get(SID)


def _work(answer="做了一件事", new_sid="cc-new"):
    return (answer, new_sid, {})


def _finished(new_sid="cc-new"):
    """後端自報完成的那一輪：答案裡帶著完成哨符。"""
    return (f"這批做完了 {SENTINEL}", new_sid, {})


def _nothing(new_sid="cc-new"):
    """這一輪什麼都沒產出（空轉黑洞）。"""
    return ("", new_sid, {})


def _said(env):
    """擁有者實際看到的所有文字——**包含最終答案**。

    第一版漏了 `final_chunks`（走 `_dorossi_reply_final` 的那一條），於是
    「壓縮輪的輸出不可以貼出去」那支測試看的是一個永遠不含答案的字串，
    變異跑出來就活了一個。「有沒有送出去」的斷言必須涵蓋**每一條**送出的
    路徑，而這個專案的送出路徑不只一條。
    """
    posted = [chunk for chunks in env.final_chunks for chunk in chunks]
    return "\n".join(
        str(x) for x in
        env.live + env.replies + env.channel_sends + posted)


# --------------------------------------------------------------------------
# 進出場：registry 與 per-session 鎖
# --------------------------------------------------------------------------

def test_a_second_loop_on_the_same_session_is_refused_and_leaves_the_first_alone(
        loop_env):
    """同一個 session 至多一個迴圈，而拒絕的那一方**不可以動到既有的註冊**。

    這個提早 return 刻意在 `try` **外面**。搬進 `try` 之後看起來更整齊，實際後果是
    `finally` 會把**另一個**還在跑的迴圈從 registry 裡移除——那個迴圈之後就收不到
    中途注入、abort 也找不到它，而它自己完全無感地繼續跑。
    """
    key = b._dorossi_session_key(UID, SID)
    sentinel = object()
    b._dorossi_loops[key] = sentinel
    _run(loop_env)
    assert loop_env.played == [], "被擋下來了卻還是跑了一輪"
    assert "已有任務進行中" in _said(loop_env)
    assert b._dorossi_loops[key] is sentinel, "拒絕的一方動到了既有的註冊"


def test_the_parallel_cap_refuses_before_registering_anything(loop_env,
                                                              monkeypatch):
    """並行數操作閥擋下來時「不留任何殘留狀態」——registry 與 slot 都不能被碰。"""
    monkeypatch.setattr(b, "DOROSSI_MAX_PARALLEL_LOOPS", 1)
    b._dorossi_loops[("someone", "else")] = object()
    _run(loop_env)
    assert loop_env.played == []
    assert "上限" in _said(loop_env)
    assert b._dorossi_session_key(UID, SID) not in b._dorossi_loops
    assert len(b._dorossi_loops) == 1, "拒絕時留下了殘留的註冊"
    assert "loop_pending" not in _slot(loop_env), "拒絕時卻寫了未完成標記"


def _abort_mid_round(answer="被中止那一輪的輸出"):
    def _step(st):
        st.abort = True
        return (answer, "cc-new", {})
    return _step


def _abort_then_raise(make_error):
    """`/dorossi abort` 殺掉正在跑的後端行程；那一輪接著以**某種錯誤**的樣子結束——
    殺掉的行程沒有輸出（看起來像卡住）、非 0 結束（看起來像一般錯誤），視當下在做什麼
    也可能被分類成暫時性故障或斷網。"""
    def _step(st):
        st.abort = True
        return make_error()
    return _step


@pytest.mark.parametrize("make_error", [
    lambda: b._DorossiLoopSilence("killed"),
    lambda: b._DorossiUsageLimitError("limit", None, session_id="cc-mid"),
    lambda: b._DorossiTransientError("overloaded", status=529, session_id="cc-mid"),
    lambda: db._DorossiOfflineError("ENOTFOUND", backend="claude_code"),
    lambda: RuntimeError("exited 1"),
], ids=["silence", "usage", "transient", "offline", "other"])
def test_an_abort_that_ends_a_round_as_an_error_is_not_retried(loop_env, make_error):
    """每一條錯誤分支都有自己的重試（重生、等額度、退避、等網路、泛用重試），而每一條都
    **先**問一次 abort。少了那一句，擁有者按下中止之後會看到「這一輪沒有進展，稍後自動
    重試」之類的訊息，然後那個任務在等待結束時又自己跑起來——中止被當成了一次故障。"""
    loop_env.rounds = [_abort_then_raise(make_error)]
    _run(loop_env)
    assert not loop_env.halted, "中止之後迴圈又要了下一輪"
    assert len(loop_env.played) == 1
    assert loop_env.waits == [], "中止之後還進了等待"
    said = _said(loop_env)
    assert "重試" not in said and "已停止" not in said and "已暫停" not in said, said
    assert "中斷" not in said, said


def _record_slot_id(env, into, then=None):
    """下一輪開跑時記下 slot 當下的 `cc_session_id`。

    要問的是「**等待開始之前**有沒有把 id 存回去」，而不是整個迴圈跑完之後的值
    ——後面每一輪成功都會再覆蓋它，所以在結尾斷言等於什麼都沒驗到。
    """
    def _step(_st):
        into.append(_slot(env).get("cc_session_id"))
        return then if then is not None else _finished()
    return _step


def _delete_slot_mid_round(env):
    def _step(_st):
        env.state[UID]["sessions"].pop(SID, None)
        return _work()
    return _step


@pytest.mark.parametrize("script_name", ["exhausted", "aborted", "fatal",
                                         "slot-deleted"])
def test_every_exit_path_removes_itself_from_the_registry(loop_env,
                                                          script_name):
    """**所有**離開路徑都要讓 `finally` 跑到，否則 per-session 鎖永遠不釋放。

    漏掉的後果不是報錯，是那個對話從此打不通：後續每一個針對同一 session 的請求都
    排隊到一個永遠不會結束的迴圈後面，一直到 bot 重啟為止。
    """
    scripts = {
        "exhausted": [_finished(), _finished()],
        "aborted": [_abort_mid_round()],
        "fatal": [RuntimeError("一次"), RuntimeError("兩次")],
        "slot-deleted": [_delete_slot_mid_round(loop_env)],
    }
    loop_env.rounds = scripts[script_name]
    _run(loop_env)
    assert not loop_env.halted, "迴圈沒有自己停下來（腳本被用完了）"
    assert b._dorossi_loops == {}, f"{script_name}：離開時沒有把自己移除"


@pytest.mark.parametrize("script_name", ["exhausted", "aborted", "fatal",
                                         "slot-deleted"])
def test_every_exit_path_lets_go_of_the_power_request(loop_env, script_name,
                                                      monkeypatch):
    """自走迴圈跑的時候持有一份電源要求；每一條離開的路都要放掉，否則 bot 會一直醒著。"""
    import _power_request as pr
    during = []

    def peek(step):
        def _step(st):
            during.append(pr.status()["reasons"])
            return step(st) if callable(step) else step
        return _step

    monkeypatch.setattr(b, "KEEP_BOT_AWAKE", True)
    monkeypatch.setattr(b, "_dorossi_power_holds", {})
    scripts = {
        "exhausted": [_finished(), _finished()],
        "aborted": [_abort_mid_round()],
        "fatal": [RuntimeError("一次"), RuntimeError("兩次")],
        "slot-deleted": [_delete_slot_mid_round(loop_env)],
    }
    first, *rest = scripts[script_name]
    loop_env.rounds = [first if isinstance(first, BaseException) else peek(first)] + rest
    _run(loop_env)
    assert not loop_env.halted
    if during:
        assert during[0] == ["axiomatic bot: dorossi loop"]
    assert pr.status()["count"] == 0, f"{script_name}：離開時沒有放掉電源要求"
    assert b._dorossi_power_holds == {}


# --------------------------------------------------------------------------
# 註冊必須在任何 await 之前（靜態）
# --------------------------------------------------------------------------

def _loop_tree():
    source = Path(b.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_dorossi_run_loop"):
            return node
    raise AssertionError("找不到 _dorossi_run_loop")


def _registration_problem(func: ast.AsyncFunctionDef) -> str:
    """回傳「註冊那一行前面有 await」的說明，沒問題就回空字串。

    判準：在函式的**頂層語句串**裡找到寫進 `_dorossi_loops[...]` 的那一句，它前面
    緊鄰的那一句必須是建立 `_DorossiLoopState` 的賦值，而且兩句都不含 `await`。
    """
    body = func.body
    hits = [i for i, stmt in enumerate(body)
            if isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "_dorossi_loops" for t in stmt.targets)]
    if len(hits) != 1:
        return f"頂層找到 {len(hits)} 個寫進 _dorossi_loops 的賦值，預期剛好一個"
    index = hits[0]
    if index == 0:
        return "註冊前面沒有建立狀態物件的那一句"
    previous = body[index - 1]
    made_state = (isinstance(previous, ast.Assign)
                  and isinstance(previous.value, ast.Call)
                  and isinstance(previous.value.func, ast.Name)
                  and previous.value.func.id == "_DorossiLoopState")
    if not made_state:
        return f"註冊前面那一句不是建立狀態物件：{ast.unparse(previous)[:60]}"
    for stmt in (previous, body[index]):
        if any(isinstance(n, ast.Await) for n in ast.walk(stmt)):
            return f"建立狀態與註冊之間有 await：{ast.unparse(stmt)[:60]}"
    return ""


def test_the_loop_registers_itself_before_any_await():
    """註冊與建立狀態之間不得有 `await`——這一點只有靜態看得出來。

    自走一啟動，`mcmd_dorossi` 的注入路由就要能把「針對同一 session」的擁有者中途
    訊息收進**本迴圈的**緩衝。中間插一個 `await`（例如先貼一則 ack）就會開一個窗口，
    落在裡面的訊息被排成一般 waiter——它不會報錯，只會「那句話沒被帶進去」，而擁有者
    看到的是任務繼續跑、指示沒生效。行為測試看不到這種時序，因為替身不會在那個瞬間
    送訊息進來。
    """
    assert _registration_problem(_loop_tree()) == ""


@pytest.mark.parametrize("source, expect", [
    ("async def f():\n    st = _DorossiLoopState(u, s)\n"
     "    _dorossi_loops[key] = st\n", ""),
    ("async def f():\n    st = _DorossiLoopState(u, s)\n"
     "    await ack()\n    _dorossi_loops[key] = st\n", "不是建立狀態物件"),
    ("async def f():\n    st = await _make_state()\n"
     "    _dorossi_loops[key] = st\n", "不是建立狀態物件"),
    ("async def f():\n    _dorossi_loops[key] = _DorossiLoopState(u, s)\n",
     "沒有建立狀態物件"),
])
def test_the_registration_check_catches_the_near_misses(source, expect):
    """對照組：真實資料乾淨時，上面那支測不出自己還會不會動。"""
    func = ast.parse(source).body[0]
    problem = _registration_problem(func)
    if not expect:
        assert problem == ""
    else:
        assert expect in problem, problem


# --------------------------------------------------------------------------
# 「未完成任務」標記
# --------------------------------------------------------------------------

def test_the_pending_marker_records_the_anchor_message(loop_env):
    """標記裡要帶錨點訊息的 id，否則 bot 被砍掉重啟後接不回去。

    `_dorossi_autoresume_pending_loops` 是用這兩個 id 把原訊息抓回來當新迴圈的
    `message`（擁有者閘門用抓回來的**真實作者**重驗，不信任磁碟上的 id）。取不到就
    只是不能自動接續，人工接續照舊——所以漏寫沒有任何即時症狀。
    """
    loop_env.rounds = [_finished(), _finished()]
    marks = []
    original = b._dorossi_mark_loop_pending

    def _spy(sess, task, **kwargs):
        marks.append((task, kwargs))
        return original(sess, task, **kwargs)

    b._dorossi_mark_loop_pending = _spy
    try:
        _run(loop_env)
    finally:
        b._dorossi_mark_loop_pending = original
    assert marks, "整個迴圈沒有寫過未完成標記"
    task, kwargs = marks[0]
    assert task == "整理測試"
    assert kwargs["channel_id"] == 42 and kwargs["message_id"] == 7


@pytest.mark.parametrize("script_name, cleared", [
    ("exhausted", True),
    ("aborted", False),
    ("fatal", False),
])
def test_only_a_natural_finish_clears_the_pending_marker(loop_env, script_name,
                                                         cleared):
    """只有「連續沒有進展而自然收尾」才清掉標記，其餘一律留著讓擁有者接續。

    反過來也要成立：abort 留下的標記必須被 `finally` 標成「不是活的」，否則 bot
    下次重啟時會把一個**擁有者親手按停**的任務自動接回去跑。
    """
    scripts = {
        "exhausted": [_finished(), _finished()],
        "aborted": [_abort_mid_round()],
        "fatal": [RuntimeError("一次"), RuntimeError("兩次")],
    }
    loop_env.rounds = scripts[script_name]
    _run(loop_env)
    marker = _slot(loop_env).get("loop_pending")
    if cleared:
        assert marker is None, "自然收尾沒有清掉標記，下次重啟會接回一個做完的任務"
    else:
        assert isinstance(marker, dict), f"{script_name} 不該清掉標記"
        assert marker.get("live") is False, (
            "離開時沒有標成「自己停的」——重啟後會被當成被砍死而自動接回去")


# --------------------------------------------------------------------------
# 停不停：exhaustion、推回、注入
# --------------------------------------------------------------------------

def test_one_self_reported_finish_is_pushed_back_instead_of_obeyed(loop_env):
    """後端說「做完了」一次不算數——推回去再找一輪，連續沒有進展才停。

    這是這個迴圈存在的理由：「做幾項就收工」正是它要擋的東西。門檻設 2，所以
    完成→有產出→完成→完成 必須跑滿四輪才停，而中間那一輪的產出要把計數清零。
    """
    loop_env.rounds = [_finished(), _work(), _finished(), _finished()]
    _run(loop_env)
    assert not loop_env.halted, "迴圈沒有停在第四輪"
    assert len(loop_env.played) == 4, [p[0][:12] for p in loop_env.played]
    assert loop_env.played[1][0] == b.DOROSSI_LOOP_PUSHBACK_PROMPT, (
        "自報完成之後沒有推回去，而是照常續跑")
    assert loop_env.played[2][0] == b.DOROSSI_LOOP_CONTINUE_PROMPT, (
        "有產出的那一輪之後不該再推回")
    assert "都做完了" in _said(loop_env)


def test_a_round_with_no_output_at_all_counts_as_no_progress(loop_env):
    """空轉黑洞也算「沒有進展」：連續兩輪什麼都沒產出就停，不然會永遠空轉。"""
    loop_env.rounds = [_nothing(), _nothing()]
    _run(loop_env)
    assert not loop_env.halted
    assert len(loop_env.played) == 2


def test_an_injection_at_the_boundary_stops_the_loop_from_stopping(loop_env):
    """**排序很重要**：drain 注入必須在「要不要停」之前。

    擁有者在最後一輪補了一句話，而迴圈剛好在這一輪達到停止門檻——先判定再 drain
    的話，那句話會連同整個任務一起被收掉，擁有者看到的是「已停止」而他剛剛才下了
    新指示。有注入就重置計數、不停、把補充折進下一輪。
    """
    def _inject_then_finish(st):
        st.injections.append("順便把文件也更新一下")
        return _finished()

    loop_env.rounds = [_finished(), _inject_then_finish]
    _run(loop_env)
    assert loop_env.halted, "有注入卻還是停了"
    assert len(loop_env.played) == 3, "沒有用注入起下一輪"
    assert "都做完了" not in _said(loop_env)
    assert "順便把文件也更新一下" in loop_env.played[2][0], (
        "下一輪的 prompt 沒有帶上補充內容")
    assert "已帶入你補充的 1 則訊息" in _said(loop_env)


# --------------------------------------------------------------------------
# 等待：用量上限、輸出靜默
# --------------------------------------------------------------------------

def test_a_usage_limit_saves_the_round_id_before_waiting(loop_env):
    """撞到方案用量上限時，**先把後端當下的工作階段 id 寫回 slot 再去等**。

    用量上限幾乎都是「做到一半」才撞上。不寫回去的話，額度回來之後 resume 的是
    **上一輪**的舊 id，這一輪做完的事全部白做——而且不會有任何錯誤訊息，只是進度
    莫名其妙倒退。
    """
    limit = b._DorossiUsageLimitError("limit", None, session_id="cc-mid")
    seen = []
    loop_env.rounds = [limit, _record_slot_id(loop_env, seen), _finished()]
    _run(loop_env)
    assert seen == ["cc-mid"], (
        f"等額度回來之前沒有保住這一輪推進到的工作階段：{seen}")
    assert loop_env.waits, "沒有進入等待"
    assert any(kind == "usage_wait" for kind, _ in loop_env.events)


def test_waiting_for_quota_is_not_counted_as_a_round_without_progress(loop_env):
    """等待不是「沒有進展」，是「還不能動」——不可以推進 exhaustion 計數。

    門檻設 2。腳本是「撞上限 → 自報完成 → 自報完成」：若等待被算成一次沒有進展，
    迴圈會在第二個自報完成之前就停掉，於是一個只是在等額度的任務被當成做完了。
    """
    limit = b._DorossiUsageLimitError("limit", None, session_id="cc-mid")
    loop_env.rounds = [limit, _finished(), _finished()]
    _run(loop_env)
    assert len(loop_env.played) == 3, (
        f"等待被算進 exhaustion 了：只跑了 {len(loop_env.played)} 輪")


def test_output_silence_retries_before_giving_up(loop_env):
    """輸出靜默先重生一次再放棄——卡住的多半是那個後端行程，換一個常常就過了。

    2026-09-05 之前這裡直接結束整個無人值守任務，等於一次卡頓就要人工接續。
    上限設 1，所以第一次重試、第二次才停，而且停的時候要告訴擁有者怎麼接續。
    """
    loop_env.rounds = [b._DorossiLoopSilence("stuck"),
                       b._DorossiLoopSilence("stuck again")]
    _run(loop_env)
    assert not loop_env.halted
    assert len(loop_env.played) == 2, "第一次靜默就放棄了，沒有重試"
    assert len(loop_env.waits) == 1, "重試之前沒有退避等待"
    assert any(kind == "silence_retry" for kind, _ in loop_env.events)
    assert "已停止" in _said(loop_env) and f"continue {SID}" in _said(loop_env)
    assert isinstance(_slot(loop_env).get("loop_pending"), dict), (
        "靜默停止屬於可接續的中斷，標記不該被清掉")


def test_a_transient_backend_error_backs_off_and_retries_the_same_prompt(
        loop_env):
    """伺服器側暫時性故障（529／5xx）要退避重試，不是就地收工。

    2026-09-03 實測：21:31 與 21:36 連兩次 529，整個無人值守迴圈就停了——而錯誤訊息
    自己寫著「usually temporary」。重試用**同一個 prompt**，這一輪不算進展也不算
    沒進展。
    """
    boom = b._DorossiTransientError("overloaded", status=529,
                                    session_id="cc-mid")
    seen = []
    loop_env.rounds = [boom, _record_slot_id(loop_env, seen), _finished()]
    _run(loop_env)
    assert len(loop_env.played) == 3, "退避之後沒有續跑"
    assert loop_env.played[0][0] == loop_env.played[1][0], (
        "重試時換了 prompt——這一輪根本沒跑完，不該推進")
    assert seen == ["cc-mid"], f"退避之前沒有保住工作階段：{seen}"
    assert any(kind == "transient_wait" for kind, _ in loop_env.events)


# --------------------------------------------------------------------------
# 本迴圈的 slot 中途消失
# --------------------------------------------------------------------------

def test_a_deleted_slot_stops_the_loop_instead_of_resuming_another(loop_env):
    """`/dorossi session delete` 落在兩輪之間時，乾淨停掉，**不要續跑進別的 slot**。

    迴圈只握著自己的 per-session 鎖，不是狀態鎖，所以刪除真的會落在回合邊界。
    舊的「取用中的那一個」fallback 在這裡是最糟的行為：它會安靜地接到另一個對話
    上繼續跑，而那個對話的擁有者看到自己的工作階段冒出不相干的內容。
    """
    loop_env.rounds = [_delete_slot_mid_round(loop_env), _work()]
    _run(loop_env)
    assert not loop_env.halted
    assert len(loop_env.played) == 1, "slot 沒了還繼續跑下一輪"
    assert "已被重置或刪除" in _said(loop_env)
    assert b._dorossi_loops == {}


# --------------------------------------------------------------------------
# 壓縮維護輪
# --------------------------------------------------------------------------

def test_a_compaction_round_is_not_counted_as_a_round_without_progress(
        loop_env, monkeypatch):
    """壓縮維護輪是刻意插進去的維護，不是「沒有進展」，也不把輸出貼出去。

    門檻設 2。腳本是「有產出 → （壓縮輪）→ 自報完成 → 自報完成」：壓縮輪若被算成
    一次沒有進展，迴圈會提早一輪停掉；它的輸出若被貼出去，就等於把「在做壓縮」這件
    事洩漏出去，而且貼的是一段對擁有者毫無意義的摘要。
    """
    due = [True]

    def fake_due(*_args, **_kwargs):
        return due.pop() if due else False

    monkeypatch.setattr(b, "_dorossi_loop_compaction_due", fake_due)
    loop_env.rounds = [_work(), _work("壓縮完成"), _finished(), _finished()]
    _run(loop_env)
    assert not loop_env.halted
    assert len(loop_env.played) == 4, "壓縮輪被算進 exhaustion 了"
    assert loop_env.played[1][0] == b.DOROSSI_LOOP_COMPACT_PROMPT
    assert loop_env.played[2][0] == b.DOROSSI_LOOP_CONTINUE_PROMPT, (
        "壓縮輪之後沒有切回正常續跑")
    assert "壓縮完成" not in _said(loop_env), "壓縮輪的輸出被貼到對話平台了"
    assert len(loop_env.final_chunks) == 3, (
        "四輪只該貼出三則——壓縮輪不走一般的貼回覆路徑："
        f"{loop_env.final_chunks}")


# --------------------------------------------------------------------------
# 兩種進入方式
# --------------------------------------------------------------------------

def test_the_backend_decided_entry_starts_from_the_second_round(loop_env):
    """自判轉進（`already_ran_first=True`）的 turn-1 已經跑完並推進了 session，
    所以本迴圈要**從第二輪**用續跑提示起跑，不可以再把第一輪的起跑提示送一次
    ——那等於把同一個任務重新交代一遍，後端會從頭再做。

    這條路的 placeholder 已經被 turn-1 用掉（呼叫端傳 None），ack 另發一則。
    """
    loop_env.placeholder = None
    loop_env.rounds = [_finished(), _finished()]
    _run(loop_env, already_ran_first=True, ack_override="接著自己做下去。")
    assert loop_env.played[0][0] == b.DOROSSI_LOOP_CONTINUE_PROMPT
    assert "接著自己做下去。" in _said(loop_env)


def test_the_phrase_entry_starts_with_the_task_and_the_first_round_suffix(
        loop_env):
    """片語快速路徑：第一輪的 prompt ＝ 使用者的任務 ＋ 第一輪後綴。"""
    loop_env.rounds = [_finished(), _finished()]
    _run(loop_env)
    first = loop_env.played[0][0]
    assert first.startswith("整理測試")
    assert first.endswith(b.DOROSSI_LOOP_FIRST_SUFFIX)


# --------------------------------------------------------------------------
# 等待當中發生的事
# --------------------------------------------------------------------------

def test_an_abort_during_a_quota_wait_ends_the_loop_quietly(loop_env):
    """等額度的那幾個小時正是擁有者最可能按停的時候。

    abort 自己會貼確認訊息，所以這條路**不可以**再貼一次「已停止」；但標記要留著
    （擁有者仍可接續），而且 `finally` 一定要跑到。
    """
    loop_env.abort_during_wait = True
    loop_env.rounds = [b._DorossiUsageLimitError("limit", None,
                                                 session_id="cc-mid"),
                       _finished()]
    _run(loop_env)
    assert not loop_env.halted, "abort 之後還繼續跑下一輪"
    assert len(loop_env.played) == 1
    assert b._dorossi_loops == {}
    assert isinstance(_slot(loop_env).get("loop_pending"), dict)
    assert _slot(loop_env)["loop_pending"].get("live") is False


def test_messages_added_during_a_quota_wait_are_folded_into_the_next_round(
        loop_env):
    """一次數小時的等待是最可能有人插話的窗口，直接丟掉會讓那些話等到下一輪邊界。

    2026-09-19 實測：擁有者在等待期間補了一句，只收到「下一輪會帶進去」，而下一輪
    在兩小時後。等完要先 drain 再重跑，而且要說一聲帶進去了幾則。
    """
    loop_env.inject_during_wait = ["順便看一下設定檔"]
    loop_env.rounds = [b._DorossiUsageLimitError("limit", None), _finished(),
                       _finished()]
    _run(loop_env)
    assert "順便看一下設定檔" in loop_env.played[1][0], (
        "等待期間補的話沒有折進等完之後那一輪")
    assert "已帶入你補充的 1 則訊息" in _said(loop_env)


def test_the_owner_can_cap_how_many_times_it_waits_for_quota(loop_env,
                                                             monkeypatch):
    """預設 0 ＝不設限（擁有者裁決：不得有回合／花費類上限；等待本身不花錢）。

    設定檔開了保底上限才會走到這條路，而走到的時候要留著標記——這是「暫停」，
    不是「做完了」。
    """
    monkeypatch.setattr(b, "DOROSSI_USAGE_WAIT_MAX_CONSECUTIVE", 1)
    loop_env.rounds = [b._DorossiUsageLimitError("limit", None),
                       b._DorossiUsageLimitError("limit", None),
                       _finished()]
    _run(loop_env)
    assert not loop_env.halted
    assert len(loop_env.played) == 2, "超過上限之後還繼續等"
    assert isinstance(_slot(loop_env).get("loop_pending"), dict)


def test_transient_failures_give_up_after_the_configured_streak(loop_env):
    """連續失敗太多次就不像「等一下就好」了——暫停並留著標記讓擁有者接續。"""
    boom = b._DorossiTransientError("overloaded", status=529)
    loop_env.rounds = [boom, b._DorossiTransientError("still overloaded",
                                                      status=503),
                       _finished()]
    _run(loop_env)
    assert not loop_env.halted
    assert len(loop_env.played) == 2
    assert "已暫停" in _said(loop_env)
    assert isinstance(_slot(loop_env).get("loop_pending"), dict)


# --------------------------------------------------------------------------
# 收尾
# --------------------------------------------------------------------------

def test_an_error_outside_a_round_still_runs_the_cleanup(loop_env,
                                                         monkeypatch):
    """回合**之外**的例外（例如貼回覆時炸掉）也要走泛用回覆 ＋ 完整收尾。

    這一段是整個迴圈最後一道網：它跑不到的話，registry 會留著一個死掉的項目，
    那個 session 從此排不進任何請求，而且沒有任何錯誤訊息。
    """
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("貼回覆時炸了")

    monkeypatch.setattr(b, "_dorossi_reply_final", _boom)
    loop_env.rounds = [_work()]
    _run(loop_env)
    assert not loop_env.halted
    assert "自走模式發生問題" in _said(loop_env)
    assert b._dorossi_loops == {}
    assert _slot(loop_env)["loop_pending"].get("live") is False


def test_a_message_that_lands_in_the_closing_window_is_reported_not_swallowed(
        loop_env):
    """任務剛結束、補充正好落在收尾窗口——泛用提示一聲，不要默默吞掉。

    擁有者的認知是「我補了一句話」；不說的話他會以為那句話被帶進去了。訊息本身
    絕不外送（Layer 1：那是未經控制的內容，而且這裡沒有提問者可判定）。
    """
    def _abort_with_leftover(st):
        st.injections.append("最後一句")
        st.abort = True
        return _work()

    loop_env.rounds = [_abort_with_leftover]
    _run(loop_env)
    assert "最後的補充未帶入" in _said(loop_env)
    assert "最後一句" not in _said(loop_env), "把補充內容原文送出去了"


def test_a_refusal_without_a_placeholder_still_reaches_the_owner(loop_env):
    """自判轉進那條路的 placeholder 已被 turn-1 用掉（呼叫端傳 None）。

    兩條提早 return 都會用到這個分支，而它們正是「使用者按了但什麼都沒發生」最容易
    出現的地方——沒有 placeholder 可編輯時必須另發一則。
    """
    loop_env.placeholder = None
    b._dorossi_loops[b._dorossi_session_key(UID, SID)] = object()
    _run(loop_env)
    assert any("已有任務進行中" in str(x) for x in loop_env.replies), (
        f"沒有 placeholder 時沒有另發訊息：{loop_env.replies}")


# --------------------------------------------------------------------------
# `/dorossi running` 讀的那幾個欄位
# --------------------------------------------------------------------------

def _observe(seen, result):
    """一輪的替身：記下「這一輪進行中」時迴圈狀態上的輪數／壓縮旗標／後端。"""
    def _step(st):
        seen.append((st.round_no, st.compacting, st.backend))
        return result
    return _step


def test_the_round_number_is_visible_while_each_round_runs(loop_env, monkeypatch):
    """輪數、壓縮輪、後端都要在**那一輪進行中**就看得到——`/dorossi running` 是在
    回合中途被叫的，事後才更新的欄位對它沒有用。壓縮輪也算一輪（它真的呼叫了後端）。"""
    due = [True]
    monkeypatch.setattr(b, "_dorossi_loop_compaction_due",
                        lambda *_a, **_k: due.pop() if due else False)
    seen: list = []
    loop_env.rounds = [_observe(seen, _work()), _observe(seen, _work("壓縮完成")),
                       _observe(seen, _finished()), _observe(seen, _finished())]
    _run(loop_env)
    assert seen == [(1, False, "claude_code"), (2, True, "claude_code"),
                    (3, False, "claude_code"), (4, False, "claude_code")], seen


def test_the_backend_decided_entry_counts_from_the_round_already_run(loop_env):
    """自判轉進時第一輪已經在單輪路徑跑過，迴圈接手的是第二輪。"""
    loop_env.placeholder = None
    seen: list = []
    loop_env.rounds = [_observe(seen, _finished()), _observe(seen, _finished())]
    _run(loop_env, already_ran_first=True, ack_override="接著自己做下去。")
    assert [r for r, _c, _b in seen] == [2, 3], seen


def test_a_backoff_wait_is_visible_and_cleared_afterwards():
    """退避等待期間 `wait_deadline` 有值，結束（含被 abort）之後一定清掉——
    留著的話，之後正常在跑的每一輪都會被顯示成「等待自動重試」。"""
    st = b._DorossiLoopState("u", "s1")
    seen = []

    async def scenario():
        async def peek():
            await asyncio.sleep(0)
            seen.append(st.wait_deadline)
            st.abort = True
        task = asyncio.ensure_future(peek())
        original = b._DOROSSI_USAGE_WAIT_POLL_SEC
        b._DOROSSI_USAGE_WAIT_POLL_SEC = 0.01
        try:
            got = await asyncio.wait_for(b._dorossi_wait_for_usage_reset(st, 3600.0), 5)
        finally:
            b._DOROSSI_USAGE_WAIT_POLL_SEC = original
        await task
        return got

    assert asyncio.run(scenario()) is False
    assert seen and seen[0] is not None, "等待期間沒有掛上截止時刻"
    assert st.wait_deadline is None, "等待結束後截止時刻沒有清掉"


# --------------------------------------------------------------------------
# 斷網（2026-09-22）：迴圈停下來等，不死；停下來的原因寫進標記
# --------------------------------------------------------------------------

import aiohttp  # noqa: E402


@pytest.fixture
def waits(monkeypatch):
    """`_dorossi_wait_online` 的替身：記下在等什麼，預設「網路回來了」。"""
    seen: list = []

    async def fake_wait_online(st, target, *, attempt=1):
        seen.append((target, attempt))
        return not st.abort

    monkeypatch.setattr(b, "_dorossi_wait_online", fake_wait_online)
    return seen


def _stop_reason(env):
    marker = _slot(env).get("loop_pending")
    return None if marker is None else marker.get("stop")


def test_a_backend_that_cannot_connect_waits_and_resumes_the_same_session(
        loop_env, waits):
    """以前：判成工作階段過舊 → 丟掉脈絡 → 泛用重試三次就停。現在：等網路回來，用
    同一個 prompt、同一個工作階段重跑同一輪，而且不吃任何重試額度。"""
    offline = db._DorossiOfflineError("ENOTFOUND", backend="claude_code",
                                      session_id="cc-old")
    loop_env.rounds = [offline, offline, offline, offline, _finished(), _finished()]
    _run(loop_env)
    assert not loop_env.halted, "斷網四次就停了——等網路沒有上限"
    assert waits == [("claude_code", 1), ("claude_code", 2), ("claude_code", 3),
                     ("claude_code", 4)]
    prompts = {p for p, _snap in loop_env.played}
    assert len(prompts) == 2, "斷網重跑的必須是同一輪（同一個 prompt）"
    assert all(snap["cc_session_id"] == "cc-old" for _p, snap in loop_env.played[:5])
    assert "網路中斷" in _said(loop_env)


def test_an_abort_during_the_network_wait_ends_the_loop_as_aborted(loop_env, waits):
    def _offline_then_abort(st):
        st.abort = True
        return db._DorossiOfflineError("ENOTFOUND", backend="claude_code")

    loop_env.rounds = [_offline_then_abort]
    _run(loop_env)
    assert not loop_env.halted
    assert _stop_reason(loop_env) == "abort"


def test_the_round_start_message_survives_the_platform_being_down(
        loop_env, waits, monkeypatch):
    """2026-09-22 的死因：每一輪開頭那則「推進中…」送不出去，例外直達最外層。"""
    real = b.safe_reply
    failures = []

    async def flaky(message, content=None, **kwargs):
        if "推進中" in str(content) and len(failures) < 2:
            failures.append(content)
            raise aiohttp.ClientConnectionError(
                "Cannot connect to host discord.com:443 [getaddrinfo failed]")
        return await real(message, content, **kwargs)

    monkeypatch.setattr(b, "safe_reply", flaky)
    loop_env.rounds = [_finished(), _finished()]
    _run(loop_env)
    assert not loop_env.halted
    assert len(failures) == 2 and waits == [("platform", 1), ("platform", 2)]
    assert len(loop_env.played) == 2, "連線回來之後要照常跑下去"
    assert _slot(loop_env).get("loop_pending") is None, "自然收尾，標記清掉"


def test_the_answer_is_delivered_after_the_platform_comes_back(
        loop_env, waits, monkeypatch):
    delivered = []

    async def flaky_reply(_message, _live, chunks, **kwargs):
        assert kwargs.get("resend_unlanded") is True, (
            "迴圈送答案要要求「edit 沒落地就另發」，否則斷網那一輪的答案會被吞掉")
        if not delivered:
            delivered.append("fail")
            raise aiohttp.ServerDisconnectedError()
        delivered.append(list(chunks))

    monkeypatch.setattr(b, "_dorossi_reply_final", flaky_reply)
    loop_env.rounds = [_work("這一輪的成果"), _finished(), _finished()]
    _run(loop_env)
    assert delivered[:2] == ["fail", ["這一輪的成果"]]
    assert waits and waits[0][0] == "platform"


def test_a_platform_error_inside_a_round_is_not_counted_as_an_error(
        loop_env, waits, monkeypatch):
    """回合進行中對平台的動作（例如 typing）斷線，不是這一輪的錯：不吃錯誤重試額度。"""
    monkeypatch.setattr(b, "DOROSSI_ERROR_RETRY_MAX", 0)   # 吃一次就會停
    loop_env.rounds = [aiohttp.ClientConnectionError("down"), _finished(), _finished()]
    _run(loop_env)
    assert not loop_env.halted
    assert waits == [("platform", 1)]
    assert _slot(loop_env).get("loop_pending") is None


@pytest.mark.parametrize("setup, reason", [
    ("silence", "silence"),
    ("error", "error"),
    ("platform_outside_rounds", "network"),
])
def test_the_stop_reason_is_written_by_the_finally(loop_env, waits, monkeypatch,
                                                   setup, reason):
    if setup == "silence":
        loop_env.rounds = [db._DorossiLoopSilence("x"), db._DorossiLoopSilence("x")]
    elif setup == "error":
        loop_env.rounds = [db._DorossiCliOptionError("--x")]
    else:
        async def boom(_message, _paths):
            raise aiohttp.ClientConnectionError("down")   # 回合外、沒有包起來的平台動作
        monkeypatch.setattr(b, "_dorossi_send_images", boom)
        loop_env.rounds = [_work()]
    _run(loop_env)
    assert not loop_env.halted
    marker = _slot(loop_env)["loop_pending"]
    assert marker["live"] is False and marker["stop"] == reason


def test_a_cancelled_loop_is_marked_interrupted_and_resumable(loop_env, monkeypatch):
    """bot 關機／重連時 task 被取消：不是自己要停的，要能被自動接回去。"""
    async def hang(*_a, **_k):
        await asyncio.Event().wait()

    monkeypatch.setattr(b, "_dorossi_loop_one_round", hang)

    async def scenario():
        task = asyncio.ensure_future(b._dorossi_run_loop(
            loop_env.message, "整理測試", loop_env.placeholder, UID, SID))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    marker = _slot(loop_env)["loop_pending"]
    assert marker["stop"] == "interrupted"
    assert db._dorossi_loop_marker_wants_autoresume(marker)
    assert b._dorossi_loops == {}

# --------------------------------------------------------------------------
# `_dorossi_loop_one_round` itself (every test above replaces it with a fake, so its own
# wiring had never run)
# --------------------------------------------------------------------------

class _RoundLive:
    def __init__(self):
        self.log: list = []

    async def finalize(self, content):
        self.log.append(("finalize", content))

    def reopen(self):
        self.log.append(("reopen",))


def _round_env(monkeypatch, *, fail_first=False, abort=False):
    calls: list = []

    def _backend(name):
        async def _invoke(prompt, stored_id, **kwargs):
            calls.append((name, prompt, stored_id, kwargs))
            if fail_first and len(calls) == 1:
                raise b._DorossiResumeError("stale session")
            return f"answer-{name}", "new-sid", {"cost": 0.1}
        return _invoke

    monkeypatch.setattr(b, "_dorossi_via_claude_code", _backend("claude_code"))
    monkeypatch.setattr(b, "_dorossi_via_codex", _backend("codex"))
    state = types.SimpleNamespace(abort=abort, set_proc=lambda proc: None)
    return calls, _RoundLive(), state


def _one_round(snap, live, state):
    async def _on_text(_text):
        return None
    return asyncio.run(b._dorossi_loop_one_round("do it", snap, live, _on_text, 30.0, state))


def test_one_round_wires_the_first_backend_with_its_budget_and_tuning(monkeypatch):
    """Default backend: resume the stored session and pass effort, model, budget and the
    previous round's cumulative baseline."""
    calls, live, state = _round_env(monkeypatch)
    snap = {"cc_session_id": "s-1", "cc_cwd": "/w", "cc_extra_dir": "/x", "effort": "high",
            "model": "m1", "cc_usage_mark": {"cost": 1.0}, "codex_session_id": "c-9"}
    assert _one_round(snap, live, state) == ("answer-claude_code", "new-sid", {"cost": 0.1})
    [(name, prompt, stored, kwargs)] = calls
    assert (name, prompt, stored) == ("claude_code", "do it", "s-1")
    assert kwargs["effort"] == "high" and kwargs["model"] == "m1"
    assert kwargs["usage_baseline"] == {"cost": 1.0}
    assert kwargs["max_budget_usd"] == b.DOROSSI_MAX_BUDGET_USD
    assert (kwargs["workdir"], kwargs["extra_dir"]) == ("/w", "/x")
    assert kwargs["loop_system_guidance"] == b.DOROSSI_LOOP_SYSTEM_GUIDANCE
    assert kwargs["abort_check"]() is False
    assert live.log == []


def test_one_round_gives_the_other_backend_only_what_it_understands(monkeypatch):
    """The other backend takes its own session id and the model; it has no effort or budget
    flags, and passing them would make it refuse to run."""
    calls, live, state = _round_env(monkeypatch)
    snap = {"backend": "codex", "codex_session_id": "c-9", "cc_session_id": "s-1",
            "effort": "high", "model": "m2"}
    _one_round(snap, live, state)
    [(name, _prompt, stored, kwargs)] = calls
    assert (name, stored, kwargs["model"]) == ("codex", "c-9", "m2")
    assert "effort" not in kwargs and "max_budget_usd" not in kwargs and "usage_baseline" not in kwargs


def test_a_stale_session_is_retried_once_as_a_new_one(monkeypatch):
    """A stale stored session: tell the user it is refreshing, then rerun once as a new
    session (id=None)."""
    calls, live, state = _round_env(monkeypatch, fail_first=True)
    assert _one_round({"cc_session_id": "s-1"}, live, state)[0] == "answer-claude_code"
    assert [c[2] for c in calls] == ["s-1", None]
    assert live.log == [("finalize", "⏳ 重新整理對話中…"), ("reopen",)]


def test_an_aborted_round_is_not_retried(monkeypatch):
    """An abort that kills the process also looks like a stale session. Retrying then would
    spawn another process racing the abort."""
    calls, live, state = _round_env(monkeypatch, fail_first=True, abort=True)
    with pytest.raises(b._DorossiResumeError):
        _one_round({"cc_session_id": "s-1"}, live, state)
    assert len(calls) == 1 and live.log == []
