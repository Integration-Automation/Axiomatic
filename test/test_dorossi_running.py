"""同時跑好幾個工作階段時的兩件事：看得到誰在跑，也送得進指定的那一個。

**一、`/dorossi running`。** 引擎早就能同時跑好幾輪（per-session 鎖 ＋
`_dorossi_backend_sem` 的並行上限 ＋ `_dorossi_loops`），但單輪回合在跑的時候只存在於
那把鎖與號誌的內部狀態裡：外面看不出「哪個對話、跑多久、是在等空位還是真的在跑」。
所以 `_dorossi_process_turn` 在整輪期間把自己登記進 `_dorossi_turns`，指令再把它和既有的
`_dorossi_loops`／`_dorossi_waiters` 合起來印。這份登記最怕的是**殘影**：少清一次，
指令就會永遠說有一輪在跑，而且沒有任何別的症狀——所以正常結束、丟例外、被取消三條路
各有一支測試。

**二、`/dorossi ask` 的 `session` 選項。** 語意與提問開頭的 `/session <id>` 完全相同
（本輪級、不動 active、存在與否在狀態鎖內驗、格式錯走同一句泛用錯誤），差別只在它是
**分開**傳進來的：接成前綴的舊寫法分不出兩者誰說了什麼，擋不住互相矛盾；而且選項值裡
空白後面那一段會變成提問的一部分——`s3 /new` 會讓本輪變成「開新對話」。

測試不連網、不起子行程；會落地的檔案全部導到 `tmp_path`。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord_bot as b  # noqa: E402
import dorossi_backend as db  # noqa: E402

UID = str(b.DOROSSI_USER_ID)
STRANGER = max(b.OWNER_USER_ID, b.DOROSSI_USER_ID) + 1


# ---------------------------------------------------------------------------
# 夾具
# ---------------------------------------------------------------------------

class _Placeholder:
    """`safe_reply` 回傳的訊息替身：記下每一次 edit。"""

    def __init__(self, content):
        self.initial = content
        self.content = content
        self.edits: list = []

    async def edit(self, content=None, **_kw):
        self.edits.append(content)
        self.content = content


def _msg(uid: int | None = None):
    return types.SimpleNamespace(
        id=11, author=types.SimpleNamespace(id=b.DOROSSI_USER_ID if uid is None else uid),
        channel=types.SimpleNamespace(id=0))


@pytest.fixture
def env(monkeypatch, tmp_path):
    """`mcmd_dorossi` 一路走到 `_dorossi_process_turn` 為止，那一步換成記錄器。

    存放檔是真的（在 tmp），所以「active 有沒有被動到」讀的是磁碟上那一份。
    每一支測試都換新的鎖與登記表：`asyncio.Lock` 第一次被爭用時才綁事件迴圈，
    共用會撞上「綁在別的迴圈上」。
    """
    rec = types.SimpleNamespace(replies=[], turns=[], placeholders=[], block=None)

    async def fake_reply(_message, content=None, **_kw):
        rec.replies.append(content)
        placeholder = _Placeholder(content)
        rec.placeholders.append(placeholder)
        return placeholder

    async def fake_turn(message, prompt, placeholder, uid, sid, *,
                        effort=None, model_tier=None):
        rec.turns.append({"prompt": prompt, "sid": sid, "uid": uid,
                          "placeholder": placeholder, "effort": effort})
        if rec.block is not None and len(rec.turns) == 1:
            await rec.block.wait()

    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", tmp_path / "dorossi_session.json")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "dorossi_events.ndjson")
    monkeypatch.setattr(b, "DOROSSI_QUEUE_FILE", tmp_path / "dorossi_queue.ndjson")
    monkeypatch.setattr(b, "DOROSSI_FAILED_QUEUE_FILE",
                        tmp_path / "dorossi_queue_failed.ndjson")
    monkeypatch.setattr(b, "safe_reply", fake_reply)
    monkeypatch.setattr(b, "_dorossi_process_turn", fake_turn)
    monkeypatch.setattr(b, "_dorossi_state_lock", asyncio.Lock())
    monkeypatch.setattr(b, "_dorossi_session_locks", {})
    monkeypatch.setattr(b, "_dorossi_session_lock_refs", {})
    monkeypatch.setattr(b, "_dorossi_waiters", {})
    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "_dorossi_turns", [])

    state: dict = {}
    record = db._dorossi_user_record(state, UID)
    for label in ("主專案", "副專案", "舊的"):
        db._dorossi_new_session(record, label=label)
    record["sessions"]["s3"]["archived"] = True
    record["active"] = "s1"
    db._dorossi_save_state(state)
    return rec


def _disk_record() -> dict:
    return db._dorossi_load_state().get(UID) or {}


def _ask(env, prompt, session=""):
    asyncio.run(b.mcmd_dorossi(_msg(), prompt, session))
    return env


def _generic_session_error() -> str:
    """提問開頭打錯 `/session` 時的那一句——選項打錯必須是**同一句**。"""
    return ("`/session` 只接受工作階段代號（如 `s3`）。")


# ---------------------------------------------------------------------------
# 一、`/dorossi ask` 的 `session` 選項
# ---------------------------------------------------------------------------

def test_the_option_sends_the_turn_to_that_slot_and_leaves_active_alone(env):
    _ask(env, "幫我看這個", "s2")
    assert [(t["sid"], t["prompt"]) for t in env.turns] == [("s2", "幫我看這個")]
    assert _disk_record()["active"] == "s1", "`session` 選項不該切換目前的工作階段"


def test_the_option_is_read_like_the_directive(env):
    """大小寫與前後空白照 `/session` 那條的規矩（值會先 lower）。"""
    _ask(env, "問題", "  S2 ")
    assert [t["sid"] for t in env.turns] == ["s2"]


def test_an_unknown_slot_is_refused_instead_of_falling_back_to_active(env):
    """指到不存在的 slot 絕不退回 active——那等於把問題送進另一個專案。"""
    _ask(env, "問題", "s9")
    assert env.turns == []
    assert env.replies and "找不到那個工作階段" in env.replies[0], env.replies
    assert _disk_record()["active"] == "s1"


@pytest.mark.parametrize("bad", ["abc", "new", "s2 /new", "s2 /effort max", "s", "2"])
def test_a_malformed_option_takes_the_same_path_as_a_malformed_directive(env, bad):
    """格式錯的值必須被**整個**拒絕，而不是把空白後面那段接進提問。

    `s2 /new` 是這條的重點：接成前綴的舊寫法會把它讀成「送到 s2，提問是
    `/new 問題`」，於是本輪變成開新對話、active 被移走、還多出一個 slot。
    """
    before = _disk_record()
    _ask(env, "問題", bad)
    assert env.turns == [], f"{bad!r} 不該送出任何一輪"
    assert env.replies == [_generic_session_error()], env.replies
    after = _disk_record()
    assert after["active"] == before["active"]
    assert set(after["sessions"]) == set(before["sessions"]), "不該多出或少掉任何 slot"
    assert all("tune_effort" not in s for s in after["sessions"].values())


def test_the_same_bad_value_inline_gets_the_same_reply(env):
    """對照組：上一支比對的那一句，確實就是提問開頭打錯時的回覆。"""
    _ask(env, "/session abc 問題")
    assert env.replies == [_generic_session_error()]


def test_an_option_that_disagrees_with_the_directive_is_refused(env):
    _ask(env, "/session s1 這題要給誰", "s2")
    assert env.turns == [], "兩個目標互相矛盾時不能猜"
    assert len(env.replies) == 1
    reply = env.replies[0]
    assert "`s2`" in reply and "`s1`" in reply, reply
    assert "沒有送出" in reply, reply
    assert _disk_record()["active"] == "s1"


def test_an_option_that_agrees_with_the_directive_is_fine(env):
    _ask(env, "/session s2 同一個", "s2")
    assert [(t["sid"], t["prompt"]) for t in env.turns] == [("s2", "同一個")]


def test_a_settings_only_prompt_lands_in_the_optioned_slot(env):
    """只打微調指令、不提問：寫進選項指的那個 slot，不是 active，也不送出一輪。"""
    _ask(env, "/effort high", "s2")
    sessions = _disk_record()["sessions"]
    assert sessions["s2"].get("tune_effort") == "high"
    assert "tune_effort" not in sessions["s1"]
    assert env.turns == []


def test_the_option_reaches_the_loop_that_owns_that_slot(env):
    """那個 slot 有自走任務在跑：照「一個對話一個任務」的既有規則進它的注入緩衝。"""
    loop_s2 = b._DorossiLoopState(UID, "s2")
    loop_s1 = b._DorossiLoopState(UID, "s1")
    b._dorossi_loops[(UID, "s2")] = loop_s2
    b._dorossi_loops[(UID, "s1")] = loop_s1
    _ask(env, "順便補這個", "s2")
    assert loop_s2.injections == ["順便補這個"]
    assert loop_s1.injections == [], "active 那個任務不該收到別人的補充"
    assert env.turns == []


def test_the_slash_command_hands_the_option_over_separately(monkeypatch):
    """斜線包裝不再把選項接成提問前綴——要不然上面那些分辨都做不到。"""
    seen = []

    async def fake_slash_run(interaction, handler, *args, detach_ack=None):
        seen.append((handler, args, detach_ack))

    monkeypatch.setattr(b, "_slash_run", fake_slash_run)
    asyncio.run(b.slash_dorossi_ask.callback(object(), "問題", "s2"))
    handler, args, ack = seen[0]
    assert handler is b.mcmd_dorossi
    assert args == ("問題", "s2")
    assert ack, "長回合要帶 detach_ack，互動 token 才不會在 15 分鐘後失效"


# ---------------------------------------------------------------------------
# 二、`session` 選項的自動補全
# ---------------------------------------------------------------------------

def _state_with(labels, active="s1", archived=()):
    state: dict = {}
    record = db._dorossi_user_record(state, UID)
    for label in labels:
        db._dorossi_new_session(record, label=label)
    for sid in archived:
        record["sessions"][sid]["archived"] = True
    record["active"] = active
    return state


def test_the_choices_list_the_callers_sessions_with_their_labels():
    choices = b._dorossi_session_choices(_state_with(["主專案", "副專案"]), UID)
    assert [value for _name, value in choices] == ["s1", "s2"]
    names = [name for name, _value in choices]
    assert "主專案" in names[0] and "s1" in names[0]
    assert "目前" in names[0] and "目前" not in names[1], "active 那個要標出來"
    assert "副專案" in names[1]


def test_the_choices_narrow_by_what_has_been_typed():
    state = _state_with(["前端", "後端", "文件"])
    assert [v for _n, v in b._dorossi_session_choices(state, UID, "後")] == ["s2"]
    assert [v for _n, v in b._dorossi_session_choices(state, UID, "S3")] == ["s3"]
    assert b._dorossi_session_choices(state, UID, "沒有這個") == []


def test_archived_sessions_are_not_offered():
    state = _state_with(["a", "b"], archived=("s2",))
    assert [v for _n, v in b._dorossi_session_choices(state, UID)] == ["s1"]


def test_the_choices_stay_within_the_platform_limits():
    """平台一次最多 25 個選項、名稱最多 100 字；超過的話整個補全回應會被拒。"""
    state = _state_with(["很長的標籤" * 30] * 40)
    choices = b._dorossi_session_choices(state, UID)
    assert len(choices) == 25
    assert all(1 <= len(name) <= 100 for name, _v in choices)


def test_a_broken_record_offers_nothing_instead_of_raising():
    assert b._dorossi_session_choices({}, UID) == []
    assert b._dorossi_session_choices({UID: "壞掉的"}, UID) == []
    assert b._dorossi_session_choices({UID: {"sessions": "壞掉的"}}, UID) == []


def test_only_the_dorossi_user_gets_any_choices(monkeypatch):
    """全域閘對補全一律放行（每個按鍵都會觸發），所以這裡自己擋。

    工作階段的標籤是擁有者自己打的字，不該在別人的補全欄位裡出現。
    """
    reads = []

    def load():
        reads.append(1)
        state = _state_with(["私人專案"])
        # 陌生人自己也有一筆紀錄：少了閘門時他拿到的會是**非空**清單，這支才殺得掉
        # 「把閘門拿掉」的變異（只有擁有者有紀錄的話，閘門在不在答案都是空的）。
        other = db._dorossi_user_record(state, str(STRANGER))
        db._dorossi_new_session(other, label="別人的")
        return state

    monkeypatch.setattr(b, "_dorossi_load_state", load)
    stranger = types.SimpleNamespace(user=types.SimpleNamespace(id=STRANGER))
    owner = types.SimpleNamespace(user=types.SimpleNamespace(id=b.DOROSSI_USER_ID))
    assert asyncio.run(b._dorossi_ask_session_autocomplete(stranger, "")) == []
    assert reads == [], "陌生人的每個按鍵都不該去讀存放檔"
    got = asyncio.run(b._dorossi_ask_session_autocomplete(owner, ""))
    assert [c.value for c in got] == ["s1"]
    assert "私人專案" in got[0].name


def test_the_autocomplete_is_wired_to_the_option():
    assert b.slash_dorossi_ask.get_parameter("session").autocomplete


# ---------------------------------------------------------------------------
# 三、進行中回合的登記：三條離開的路都要清掉
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(b, "_dorossi_turns", [])
    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "_dorossi_waiters", {})
    return b._dorossi_turns


def test_a_turn_is_registered_while_it_runs_and_removed_after(registry, monkeypatch):
    seen = []

    async def body(message, prompt, placeholder, uid, sid, turn, **_kw):
        seen.append([(t.uid, t.sid, t.kind, t.phase) for t in b._dorossi_turns])
        assert turn in b._dorossi_turns

    monkeypatch.setattr(b, "_dorossi_run_turn", body)
    asyncio.run(b._dorossi_process_turn(_msg(), "問題", None, UID, "s2"))
    assert seen == [[(UID, "s2", "ask", "prep")]]
    assert b._dorossi_turns == [], "回合結束後不能留下殘影"


def test_a_manual_compaction_is_its_own_kind(registry, monkeypatch):
    kinds = []

    async def body(message, prompt, placeholder, uid, sid, turn, **_kw):
        kinds.append(turn.kind)

    monkeypatch.setattr(b, "_dorossi_run_turn", body)
    asyncio.run(b._dorossi_process_turn(_msg(), "/compact", None, UID, "s1"))
    assert kinds == ["compact"]


def test_a_turn_that_raises_is_still_removed(registry, monkeypatch):
    async def body(*_a, **_kw):
        raise RuntimeError("回合中途炸掉")

    monkeypatch.setattr(b, "_dorossi_run_turn", body)
    with pytest.raises(RuntimeError):
        asyncio.run(b._dorossi_process_turn(_msg(), "問題", None, UID, "s1"))
    assert b._dorossi_turns == []


def test_a_cancelled_turn_is_still_removed(registry, monkeypatch):
    """`CancelledError` 是 BaseException，`except Exception` 接不到——只有 `finally` 清得掉。"""
    async def body(*_a, **_kw):
        await asyncio.Event().wait()

    monkeypatch.setattr(b, "_dorossi_run_turn", body)

    async def scenario():
        task = asyncio.ensure_future(
            b._dorossi_process_turn(_msg(), "問題", None, UID, "s1"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(b._dorossi_turns) == 1, "取消之前它應該已經登記了"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert b._dorossi_turns == []


# ---------------------------------------------------------------------------
# 四、`/dorossi running` 的內容
# ---------------------------------------------------------------------------

def _report_state():
    return _state_with(["主專案", "副專案", "夜間任務"])


def _turn(sid, phase, *, started, backend="claude_code", kind="ask", proc=None):
    turn = b._dorossi_turn_register(UID, sid, "/compact" if kind == "compact" else "問題")
    turn.phase = phase
    turn.started_ts = started
    turn.backend = backend
    turn.proc = proc
    return turn


def _waiter(canceled=False):
    waiter = b._DorossiWaiter(None, None)
    waiter.canceled = canceled
    return waiter


def test_idle_says_so_and_still_shows_the_caps(registry, monkeypatch):
    monkeypatch.setattr(b, "DOROSSI_CC_MAX_PARALLEL", 3)
    monkeypatch.setattr(b, "DOROSSI_MAX_PARALLEL_LOOPS", 6)
    text = b._dorossi_running_report(_report_state(), _msg())
    assert "目前沒有進行中的工作" in text, text
    assert "執行中 0／上限 3" in text and "等候空位 0" in text, text
    assert "自走任務：0／上限 6" in text, text
    assert "`s1`" not in text


def test_a_running_and_a_waiting_turn_are_told_apart(registry, monkeypatch):
    monkeypatch.setattr(b, "DOROSSI_CC_MAX_PARALLEL", 1)
    now = time.time()
    _turn("s1", "run", started=now - 312,
          proc=types.SimpleNamespace(pid=4321, returncode=None))
    _turn("s2", "wait", started=now - 45)
    b._dorossi_waiters[(UID, "s1")] = [_waiter(), _waiter(), _waiter(canceled=True)]
    text = b._dorossi_running_report(_report_state(), _msg(), now=now)
    lines = text.splitlines()
    s1 = next(i for i, line in enumerate(lines) if "`s1`" in line)
    s2 = next(i for i, line in enumerate(lines) if "`s2`" in line)
    assert "主專案" in lines[s1] and "單輪提問" in lines[s1], lines[s1]
    assert "後端 A" in lines[s1] and "執行中" in lines[s1], lines[s1]
    assert "等候空位" not in lines[s1]
    assert "PID 4321" in lines[s1], "擁有者看得到後端行程的 PID"
    assert "5m 12s" in lines[s1 + 1] and "後面排隊 2 筆" in lines[s1 + 1], lines[s1 + 1]
    assert "等候空位" in lines[s2] and "副專案" in lines[s2], lines[s2]
    assert "45s" in lines[s2 + 1] and "後面排隊 0 筆" in lines[s2 + 1], lines[s2 + 1]
    assert "執行中 1／上限 1，等候空位 1" in text, text
    assert s1 < s2, "先開始的排在前面"


def test_pids_follow_the_owner_decision_point(registry, monkeypatch):
    """PID 屬於 Layer 1 只准給擁有者看的東西；判準走 `_owner_detail`，不在這裡另寫。"""
    _turn("s1", "run", started=time.time(),
          proc=types.SimpleNamespace(pid=4321, returncode=None))
    text = b._dorossi_running_report(_report_state(), _msg(STRANGER))
    assert "4321" not in text and "PID" not in text, text
    ended = types.SimpleNamespace(pid=999, returncode=0)
    b._dorossi_turns[0].proc = ended
    text = b._dorossi_running_report(_report_state(), _msg())
    assert "999" not in text, "已經結束的行程不該再報 PID"


def test_a_loop_shows_its_round_and_its_own_cap(registry, monkeypatch):
    monkeypatch.setattr(b, "DOROSSI_MAX_PARALLEL_LOOPS", 0)
    now = time.time()
    st = b._DorossiLoopState(UID, "s3")
    st.started_ts = now - 3 * 3600
    st.round_no = 12
    st.backend = "claude_code"
    st.proc = types.SimpleNamespace(pid=5678, returncode=None)
    st.injections.append("補一句")
    b._dorossi_loops[(UID, "s3")] = st
    text = b._dorossi_running_report(_report_state(), _msg(), now=now)
    line = next(line for line in text.splitlines() if "`s3`" in line)
    assert "自走任務第 12 輪" in line and "夜間任務" in line, line
    assert "執行中" in line and "PID 5678" in line, line
    assert "3h" in text and "待帶入的補充 1 則" in text, text
    assert "自走任務：1（不設限" in text, text


@pytest.mark.parametrize("setup, expect", [
    (lambda st: setattr(st, "wait_deadline", time.monotonic() + 90), "等待自動重試"),
    (lambda st: setattr(st, "compacting", True), "壓縮"),
    (lambda st: setattr(st, "abort", True), "中止中"),
])
def test_a_loop_that_is_not_simply_running_says_why(registry, setup, expect):
    st = b._DorossiLoopState(UID, "s1")
    st.round_no = 3
    setup(st)
    b._dorossi_loops[(UID, "s1")] = st
    line = next(line for line in
                b._dorossi_running_report(_report_state(), _msg()).splitlines()
                if "`s1`" in line)
    assert expect in line, line


def test_a_turn_handed_to_its_loop_is_listed_once(registry):
    """轉進自走之後由迴圈那一筆代表；兩筆都列就像同一個對話跑了兩件事。"""
    _turn("s1", "loop", started=time.time())
    st = b._DorossiLoopState(UID, "s1")
    st.round_no = 1
    b._dorossi_loops[(UID, "s1")] = st
    text = b._dorossi_running_report(_report_state(), _msg())
    assert sum("`s1`" in line for line in text.splitlines()) == 1, text
    assert "自走任務第 1 輪" in text


def test_background_compaction_holds_a_slot_and_says_what_it_is(registry):
    _turn("s2", "compact", started=time.time())
    text = b._dorossi_running_report(_report_state(), _msg())
    line = next(line for line in text.splitlines() if "`s2`" in line)
    assert "背景壓縮" in line and "執行中" in line, line
    assert "執行中 1／" in text, "背景壓縮也佔一個後端空位"


def test_the_real_background_compaction_moves_the_turn_through_its_phases(
        registry, monkeypatch):
    """走真的 `_dorossi_maybe_compact_single_turn`：壓縮期間登記是「背景壓縮／執行中」、
    行程交給登記（PID），結束後回到「收尾中」。"""
    seen = {}

    async def fake_cc(prompt, session_id, **kwargs):
        kwargs["on_proc"](types.SimpleNamespace(pid=2468, returncode=None))
        seen["phase"] = turn.phase
        seen["report"] = b._dorossi_running_report(_report_state(), _msg())
        return ("", session_id, {})

    async def fake_rmw(mutate):
        return None

    monkeypatch.setattr(b, "_dorossi_context_compaction_due", lambda _t: True)
    monkeypatch.setattr(b, "_dorossi_via_claude_code", fake_cc)
    monkeypatch.setattr(b, "_dorossi_state_rmw", fake_rmw)
    monkeypatch.setattr(b, "_dorossi_backend_sem", asyncio.Semaphore(1))
    turn = b._dorossi_turn_register(UID, "s2", "問題")
    turn.phase = "finish"
    asyncio.run(b._dorossi_maybe_compact_single_turn(
        UID, "s2", "cc-1", {}, {"ctx": 10**9}, turn=turn))
    assert seen["phase"] == "compact"
    line = next(line for line in seen["report"].splitlines() if "`s2`" in line)
    assert "背景壓縮" in line and "執行中" in line and "PID 2468" in line, line
    assert turn.phase == "finish", "壓縮結束、空位放掉之後要回到「收尾中」"


def test_a_turn_that_was_told_to_abort_says_so_until_it_is_gone(registry):
    """被要求中止、還沒收尾的單輪回合（例如排在空位上，輪到它才會結束）標成「中止中」，
    跟自走任務同一個說法——否則擁有者看到的是一輪照常「等候空位」的回合。"""
    turn = _turn("s2", "wait", started=time.time())
    before = b._dorossi_running_report(_report_state(), _msg())
    turn.request_abort()
    after = b._dorossi_running_report(_report_state(), _msg())
    line = next(line for line in after.splitlines() if "`s2`" in line)
    assert "中止中" in line and "等候空位" not in line, line
    assert "中止中" not in before, "還沒被要求中止就標成中止中"


def test_a_manual_compaction_is_labelled_as_one(registry):
    _turn("s2", "wait", started=time.time(), kind="compact")
    line = next(line for line in
                b._dorossi_running_report(_report_state(), _msg()).splitlines()
                if "`s2`" in line)
    assert "壓縮脈絡" in line and "等候空位" in line, line


# ---------------------------------------------------------------------------
# 五、指令本身：擁有者閘與送出
# ---------------------------------------------------------------------------

def test_a_stranger_is_refused_before_anything_is_read(registry, monkeypatch):
    replies = []

    async def fake_reply(_message, content=None, **_kw):
        replies.append(content)

    def boom(*_a, **_kw):
        raise AssertionError("非擁有者不該讓報告被組出來")

    monkeypatch.setattr(b, "safe_reply", fake_reply)
    monkeypatch.setattr(b, "_dorossi_running_report", boom)
    monkeypatch.setattr(b, "_dorossi_load_state", boom)
    asyncio.run(b.mcmd_dorossi_running(_msg(STRANGER)))
    assert replies == [b.OWNER_ONLY_DENIED]


def test_the_owner_gets_the_report(registry, monkeypatch, tmp_path):
    replies = []

    async def fake_reply(_message, content=None, **kw):
        replies.append((content, kw))

    monkeypatch.setattr(b, "safe_reply", fake_reply)
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "events.ndjson")
    monkeypatch.setattr(b, "_dorossi_load_state", _report_state)
    _turn("s1", "run", started=time.time())
    asyncio.run(b.mcmd_dorossi_running(_msg()))
    assert len(replies) == 1
    content, kw = replies[0]
    # 2026-10-01 起送的是卡片；沒有卡片的平台看到的是 `flatten_embed` 攤平的文字，這裡讀的就是那一份。
    from _chat_platform import outbound_text  # noqa: PLC0415
    text = outbound_text(content, kw.get("embed"))
    assert "`s1`" in text and "主專案" in text, text
    assert "allowed_mentions" in kw, "標籤是使用者打的字，送出時不能帶 ping"


def test_the_card_says_what_the_text_report_says(registry):
    """卡片與文字版由同一支 `_dorossi_running_parts` 組：每件工作一個欄位，摘要在描述，狀態決定顏色。"""
    from _chat_platform import flatten_embed  # noqa: PLC0415
    import _reply_card  # noqa: PLC0415
    now = time.time()
    _turn("s1", "run", started=now - 30)
    text = b._dorossi_running_report(_report_state(), _msg(), now=now)
    card = b._dorossi_running_card(_report_state(), _msg(), now=now)
    assert len(card.fields) == 1 and "`s1`" in card.fields[0].name
    assert card.color.value == _reply_card.STATUS_COLORS["ok"]
    flat = flatten_embed(card)
    for line in text.splitlines()[1:]:
        assert line.strip().removeprefix("• ") in flat, (line, flat)


def test_an_idle_card_is_grey(registry):
    import _reply_card  # noqa: PLC0415
    card = b._dorossi_running_card({}, _msg(), now=time.time())
    assert not card.fields and card.color.value == _reply_card.STATUS_COLORS["idle"]


def test_the_command_is_on_the_tree_next_to_status():
    names = {c.name for c in b.dorossi_group.commands}
    assert {"running", "status"} <= names


# ---------------------------------------------------------------------------
# 六、排到之後的那一句：沒有空位時不能說「正在處理」
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("busy", [True, False])
def test_the_dequeued_placeholder_tells_the_truth_about_the_slot(env, monkeypatch, busy):
    """同一個對話排第二的那一輪輪到了：空位滿的時候要說在等空位，不是「正在處理」。"""
    monkeypatch.setattr(b, "DOROSSI_CC_MAX_PARALLEL", 2)
    monkeypatch.setattr(b, "_dorossi_backend_sem",
                        types.SimpleNamespace(locked=lambda: busy))

    async def scenario():
        env.block = asyncio.Event()
        first = asyncio.ensure_future(b.mcmd_dorossi(_msg(), "第一題"))
        for _ in range(20):
            await asyncio.sleep(0)
            if env.turns:
                break
        second = asyncio.ensure_future(b.mcmd_dorossi(_msg(), "第二題"))
        for _ in range(20):
            await asyncio.sleep(0)
            if env.placeholders:
                break
        assert env.placeholders, "第二題應該排進佇列並送出佔位訊息"
        env.block.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)

    asyncio.run(scenario())
    queued = env.placeholders[0]
    assert "已排入佇列" in queued.initial, queued.initial
    assert [t["prompt"] for t in env.turns] == ["第一題", "第二題"]
    assert env.turns[1]["placeholder"] is queued, "排隊的那則訊息要交給那一輪沿用"
    turned = queued.edits[0]
    if busy:
        assert turned == "⏳ 換你了，等候空位（同時執行上限 2）…", turned
    else:
        assert turned == "⏳ 換你了，正在處理…", turned


# ---------------------------------------------------------------------------
# 電源：進行中的回合與自走迴圈各持有一份，離開時放掉
# ---------------------------------------------------------------------------

class _FakePowerBackend:
    """記下向作業系統要／放的次數；計數器只在第一份與最後一份時才叫它。"""

    def __init__(self):
        self.acquires: list = []
        self.releases = 0

    def acquire(self, reason):
        self.acquires.append(reason)
        return "power-request"

    def release(self):
        self.releases += 1


@pytest.fixture
def power(monkeypatch):
    import _power_request as pr
    backend = _FakePowerBackend()
    monkeypatch.setattr(pr, "_MANAGER",
                        pr._Manager(backend_factory=lambda: backend))
    monkeypatch.setattr(b, "KEEP_BOT_AWAKE", True)
    monkeypatch.setattr(b, "_dorossi_power_holds", {})
    return pr, backend


def test_a_running_turn_holds_power_and_lets_go_after(registry, power, monkeypatch):
    pr, backend = power
    seen = []

    async def body(*_a, **_kw):
        seen.append(pr.status())

    monkeypatch.setattr(b, "_dorossi_run_turn", body)
    asyncio.run(b._dorossi_process_turn(_msg(), "問題", None, UID, "s2"))
    assert seen == [{"count": 1, "active": "power-request",
                     "reasons": ["axiomatic bot: dorossi turn"]}]
    assert pr.status()["count"] == 0 and backend.releases == 1
    assert b._dorossi_power_holds == {}


def test_a_turn_that_raises_lets_go_of_power(registry, power, monkeypatch):
    pr, backend = power

    async def body(*_a, **_kw):
        raise RuntimeError("回合中途炸掉")

    monkeypatch.setattr(b, "_dorossi_run_turn", body)
    with pytest.raises(RuntimeError):
        asyncio.run(b._dorossi_process_turn(_msg(), "問題", None, UID, "s1"))
    assert pr.status()["count"] == 0 and backend.releases == 1


def test_a_cancelled_turn_lets_go_of_power(registry, power, monkeypatch):
    """`/dorossi abort` 取消回合時，那一份要跟著放掉，不能讓 bot 一直醒著。"""
    pr, backend = power

    async def body(*_a, **_kw):
        await asyncio.Event().wait()

    monkeypatch.setattr(b, "_dorossi_run_turn", body)

    async def scenario():
        task = asyncio.ensure_future(
            b._dorossi_process_turn(_msg(), "問題", None, UID, "s1"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert pr.status()["count"] == 1, "取消之前它應該已經拿到了"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert pr.status()["count"] == 0 and backend.releases == 1


def test_two_turns_on_one_session_hold_two_shares(power):
    """第二個回合在 per-session 鎖前面等的時候就登記了：第一個結束不能把它的份放掉。"""
    pr, backend = power
    b._dorossi_work_started("turn", (UID, "s1"))
    b._dorossi_work_started("turn", (UID, "s1"))
    assert pr.status()["count"] == 2
    b._dorossi_work_finished("turn", (UID, "s1"))
    assert pr.status()["count"] == 1 and backend.releases == 0
    b._dorossi_work_finished("turn", (UID, "s1"))
    assert pr.status()["count"] == 0 and backend.releases == 1
    assert b._dorossi_power_holds == {}


def test_a_loop_and_a_turn_share_one_request_to_the_os(power):
    pr, backend = power
    b._dorossi_work_started("loop", (UID, "s3"))
    b._dorossi_work_started("turn", (UID, "s1"))
    assert len(backend.acquires) == 1
    assert pr.status()["reasons"] == ["axiomatic bot: dorossi loop",
                                      "axiomatic bot: dorossi turn"]
    b._dorossi_work_finished("loop", (UID, "s3"))
    assert backend.releases == 0, "還有一個回合在跑"
    b._dorossi_work_finished("turn", (UID, "s1"))
    assert backend.releases == 1


def test_finishing_what_never_started_is_harmless(power):
    pr, backend = power
    b._dorossi_work_finished("turn", (UID, "s9"))
    assert pr.status()["count"] == 0 and backend.releases == 0


def test_with_the_switch_off_nothing_is_held(power, monkeypatch):
    pr, backend = power
    monkeypatch.setattr(b, "KEEP_BOT_AWAKE", False)
    b._dorossi_work_started("loop", (UID, "s1"))
    assert pr.status()["count"] == 0 and backend.acquires == []
    b._dorossi_work_finished("loop", (UID, "s1"))
    assert b._dorossi_power_holds == {}


def test_a_power_failure_never_stops_the_work(power, monkeypatch):
    def boom(_reason):
        raise OSError("no power api")

    monkeypatch.setattr(b, "bot_power_hold", boom)
    b._dorossi_work_started("turn", (UID, "s1"))
    b._dorossi_work_finished("turn", (UID, "s1"))
    assert b._dorossi_power_holds == {}


def test_a_task_saved_for_the_quota_is_listed_with_its_resume_time(registry):
    """撞到方案用量上限而存檔停下的自走任務不在跑，但到點會自己接回來——跟停放的提問同一個
    性質，所以要列出來（哪個對話、幾點），否則 `/dorossi running` 看起來是空的，幾小時後
    卻憑空冒出一個自走任務。別種停下來的任務不列。"""
    now = time.time()
    state = _report_state()
    sessions = state[UID]["sessions"]
    sessions["s7"] = {"loop_pending": {"ts": now, "task": "t", "live": True}}
    db._dorossi_mark_loop_usage_wait(sessions["s7"], resume_at=now + 3600, usage_waits=1,
                                     resume_prompt="p", injections=[])
    sessions["s8"] = {"loop_pending": {"ts": now, "task": "t", "live": False,
                                       "stop": "network"}}
    text = b._dorossi_running_report(state, _msg(), now=now)
    line = next(line for line in text.splitlines() if "等方案用量重設" in line)
    assert "自走任務" in line and "1 件" in line and "`s7`" in line, line
    assert b._dorossi_reset_clock(now + 3600, now=now) in line, line
    assert "`s8`" not in text, text


# ---------------------------------------------------------------------------
# 七、標記不再是提問入口；那些事現在只經 `/dorossi ask` 到得了（2026-10-01）
# ---------------------------------------------------------------------------
# 這一段跑的是**真的** `mcmd_dorossi`（上面那個 `env` 夾具，只有 `_dorossi_process_turn`
# 是記錄器），所以「沒有開一輪」不是替身說的，是磁碟上的事件檔、佇列檔、存放檔與記憶體
# 裡的鎖一起說的。派發層那一半（回的是哪一張圖、控制字照舊）在 `test_slash_gate.py`。
BOT_ID = 777_000_777
SELF_DRIVE = "持續推進直到做完，不要問我"


@pytest.fixture
def surfaces(env, monkeypatch):
    """`env` 之上再加：bot 自己的身分、回圖那支換成記錄器、完整工具模式。"""
    env.pictures = []
    env.messages = []

    async def fake_picture(_message, tags):
        env.pictures.append(tags)

    inner = b._dorossi_process_turn

    async def turn_keeping_the_message(message, *args, **kwargs):
        env.messages.append(message)
        await inner(message, *args, **kwargs)

    monkeypatch.setattr(b, "client", types.SimpleNamespace(
        user=types.SimpleNamespace(id=BOT_ID)))
    monkeypatch.setattr(b, "_send_danbooru_image", fake_picture)
    monkeypatch.setattr(b, "_dorossi_process_turn", turn_keeping_the_message)
    monkeypatch.setattr(b, "DOROSSI_CC_TOOLS", "full")
    return env


def _mentioned(text: str):
    """既有平台上一則標記了 bot 的訊息（擁有者發的）。"""
    return types.SimpleNamespace(
        id=21, content=f"<@{BOT_ID}> {text}".strip(), guild=None, attachments=[],
        mentions=[types.SimpleNamespace(id=BOT_ID)],
        author=types.SimpleNamespace(id=b.DOROSSI_USER_ID),
        channel=types.SimpleNamespace(id=0))


def _from_another_platform(text: str):
    return types.SimpleNamespace(
        id=22, content=text, guild=None, attachments=[], mentions=[],
        author=types.SimpleNamespace(id=b.DOROSSI_USER_ID, is_owner=True),
        channel=types.SimpleNamespace(id=0, is_direct=True, is_command_chat=True))


def _event_types() -> list:
    import json
    path = b.DOROSSI_EVENTS_FILE
    if not path.exists():
        return []
    return [json.loads(line)["type"] for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _SlashInteraction:
    """`_slash_run` ＋ `_InteractionMessageProxy` 真正碰得到的那幾個屬性。"""

    def __init__(self, uid=None):
        self.extras: dict = {}
        self.id = 31
        self.guild = None
        self.user = types.SimpleNamespace(
            id=b.DOROSSI_USER_ID if uid is None else uid)
        self.sent: list = []
        outer = self

        class _Response:
            def is_done(self):
                return False

            async def defer(self, **_kwargs):
                return None

        class _Followup:
            async def send(self, content=None, **_kwargs):
                outer.sent.append(content)
                return _Placeholder(content)

        class _Channel:
            id = 0

            async def send(self, content=None, **_kwargs):
                outer.sent.append(content)
                return _Placeholder(content)

        self.response = _Response()
        self.followup = _Followup()
        self.channel = _Channel()


def _slash_ask(prompt: str, session: str = "", uid=None) -> _SlashInteraction:
    interaction = _SlashInteraction(uid)
    asyncio.run(b.slash_dorossi_ask.callback(interaction, prompt, session))
    return interaction


@pytest.mark.parametrize("text", [
    "幫我看這個", "dorossi 幫我看這個", "/new", "/effort high 幫我看這個",
    "/session s2 幫我看這個", SELF_DRIVE,
], ids=["plain", "keyword", "reset-word", "tuning-token", "session-token",
        "self-drive-phrase"])
def test_a_mention_from_the_owner_opens_no_turn_at_all(surfaces, text):
    """擁有者標記 bot 打一段字：不開一輪、不記 `prompt_received`、不佔那個對話的鎖、
    不排隊、不動存放檔（`/new` 沒有多開 slot、微調沒有被寫進去）——只回那張預設圖。"""
    before = _disk_record()
    asyncio.run(b.on_message(_mentioned(text)))
    assert surfaces.turns == [], f"標記 bot 還是開了一輪：{surfaces.turns}"
    assert "prompt_received" not in _event_types()
    assert b._dorossi_session_lock_refs == {} and b._dorossi_waiters == {}
    assert b._dorossi_queue_read() == []
    assert _disk_record() == before, "標記 bot 動到了存放檔"
    assert surfaces.pictures == ["rossi_(arknights)"], surfaces.pictures


def test_a_mention_no_longer_steers_a_running_task(surfaces):
    """自走任務進行中：標記 bot 打的字不再被收成中途補充。"""
    loop = b._DorossiLoopState(UID, "s1")
    b._dorossi_loops[(UID, "s1")] = loop
    asyncio.run(b.on_message(_mentioned("順便補這個")))
    assert loop.injections == []
    assert surfaces.replies == [], surfaces.replies
    assert surfaces.pictures == ["rossi_(arknights)"]


@pytest.mark.parametrize("text, prompt", [
    ("幫我看這個", "幫我看這個"), ("dorossi 幫我看這個", "幫我看這個"),
], ids=["plain", "keyword"])
def test_the_same_text_from_a_platform_without_a_slash_menu_still_opens_a_turn(
        surfaces, text, prompt):
    """對照組：同一段字從沒有斜線選單的平台進來，照舊是提問。少了這一支，上面那兩支
    在「提問那條路整個壞掉」時也會是綠的。"""
    asyncio.run(b.dispatch_external_message(_from_another_platform(text)))
    assert [(t["sid"], t["prompt"]) for t in surfaces.turns] == [("s1", prompt)]
    assert "prompt_received" in _event_types()
    assert surfaces.pictures == []


def test_the_slash_command_opens_a_turn(surfaces):
    interaction = _slash_ask("幫我看這個")
    assert [(t["sid"], t["prompt"]) for t in surfaces.turns] == [("s1", "幫我看這個")]
    assert interaction.sent and "已收下" in interaction.sent[0], interaction.sent


def test_the_slash_command_steers_a_running_task(surfaces):
    """中途補充：自走任務在跑時，`/dorossi ask` 的提問進它的注入緩衝，不另開一輪。"""
    loop = b._DorossiLoopState(UID, "s1")
    b._dorossi_loops[(UID, "s1")] = loop
    _slash_ask("順便補這個")
    assert loop.injections == ["順便補這個"]
    assert surfaces.turns == []
    assert surfaces.replies == ["📨 已加入目前任務，下一輪會帶進去。"], surfaces.replies


def test_the_slash_command_takes_the_reset_word(surfaces):
    """`/new` 打在 `prompt` 開頭：開一個新的工作階段並切過去，這一輪送進新的那一個。"""
    before = set(_disk_record()["sessions"])
    _slash_ask("/new")
    after = _disk_record()
    opened = sorted(set(after["sessions"]) - before)
    assert len(opened) == 1, opened
    assert after["active"] == opened[0]
    assert [(t["sid"], t["prompt"]) for t in surfaces.turns] == [(opened[0], "/new")]


def test_the_slash_command_takes_the_tuning_tokens(surfaces):
    """`/effort`／`/model` 打在 `prompt` 開頭：從送出去的提問剝掉，帶進這一輪。"""
    _slash_ask("/effort high 幫我看這個")
    assert [(t["prompt"], t["effort"]) for t in surfaces.turns] == [("幫我看這個", "high")]


def test_the_slash_command_can_start_a_self_driving_task(surfaces):
    """自走的入口在提問那一輪裡面（片語 ＋ 擁有者 ＋ 完整工具模式）。斜線那條路交進去的
    是代理物件，所以要確認閘門認得它的發問者——而且提問原封不動交到那一輪手上。"""
    _slash_ask(SELF_DRIVE)
    assert [t["prompt"] for t in surfaces.turns] == [SELF_DRIVE]
    proxy = surfaces.messages[0]
    assert b._dorossi_should_loop(proxy, "claude_code", SELF_DRIVE) is True
    # 對照：同一句話、不是擁有者 → 閘門不開（否則上面那一句只是在測片語比對）。
    stranger = _SlashInteraction(uid=STRANGER)
    assert b._dorossi_should_loop(
        b._InteractionMessageProxy(stranger), "claude_code", SELF_DRIVE) is False
