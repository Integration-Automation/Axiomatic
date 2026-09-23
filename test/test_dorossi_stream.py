"""後端串流的**折疊**與**判定**：行為測試（真的把假串流餵進去執行）。

為什麼要有這一份：2026-09-08 用 `coverage` 量過 `test_dorossi_*.py` ＋
`test_bot_helpers.py`，`_ClaudeStreamState.feed` 51 行只執行到 1 行（那一行是
`def` 本身，匯入時跑的），`_claude_stream_verdict` 30 行執行到 1 行。`grep` 看起來
有 7 處「提到」這些符號，但**全部是 AST 結構檢查**——它們讀原始碼、確認某個呼叫寫
在那裡，一行都沒有真的執行過。**「有測試提到它」不等於「它被執行過」**，這一份補
的就是後者。

範圍刻意只到「純資料轉換 ＋ 純判定」這一半：`_ClaudeStreamState.feed` /
`.failure_reason` / `_claude_stream_verdict` / `_CodexStreamState.feed` /
`_codex_stream_verdict`。叫用那一半（`_dorossi_via_claude_code` /
`_dorossi_via_codex`）綁死在 subprocess ＋ 兩段式看門狗 ＋ `proc.kill()` 上，要測就
得真的起一個後端 CLI，不在這裡。重構本來就是為了這個切分而做的，兩個 state 類別的
docstring 都寫明了理由。

四件事值得單獨點出來，因為它們是**規則**而不只是行為：

**一、`feed` 承諾「永遠不 raise」，而那個承諾是承重的。** claude 那側的讀取迴圈
**沒有** try/finally，例外會逸出整支函式，於是子行程沒人 kill、stderr 抽水任務變成
孤兒、而那個 session 的鎖永久卡住——一個無人值守的迴圈就這樣停在那裡，沒有任何訊號。
所以壞行（非 JSON 雜訊、空行、合法 JSON 但不是物件、以及**欄位型別不對**的物件）
一律要「跳過、繼續讀」。

**二、判定順序就是規則本身。** 2026-09-05 的事故是三個具名分支被排到泛用處理後面，
整段變成死碼而沒有任何訊號。順序是：
`輸出靜默 → 硬性上限 → 閒置 → 預算閘 → 用量上限 → 暫時性故障 → 沒有可用的登入 → CLI 拒絕旗標 → resume 重試`。
下面每一對**能夠同時成立**的相鄰關係都有一支測試釘住（三個看門狗理由共用同一個
`kill_reason` 欄位，彼此互斥、湊不出衝突輸入，改成各自驗自己那條路）。三個看門狗
理由共用一個出口（2026-09-19）：已經收到成功的 `result` 時收下答案、走 rc==0 收尾；
沒有的話照舊丟例外——兩個方向每一條都各有一支。

**三、預算閘走 `return "budget"` 而不是 raise。** `test_bot_helpers` 已經在 AST 層
釘過（raise 會**繞過** `_dorossi_round_info_and_record`，那一輪的花費不進帳本），
這裡在行為層再釘一次。

**四、進度預覽只吃「答案文字」。** 只有 `type == "text"` 的 content block 的
`text_delta` 才進 `stream_text`；工具呼叫的 `input_json_delta` 不進。這是安全性質不
是版面問題——`stream_text` 會被送到對話平台，而工具呼叫的參數裡裝的是主機路徑與指令。
"""
from __future__ import annotations

import asyncio
import ast
import copy
import json
import os
import sys

import pytest

from pathlib import Path

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import dorossi_backend as db  # noqa: E402

PKG_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "axiomatic"


# ---------------------------------------------------------------------------
# 事件形狀。照著真實串流長相寫，不要簡化成「只有測試需要的欄位」——判定讀的是
# subtype / is_error / api_error_status，少一個就測不到真正跑的那條路。
# ---------------------------------------------------------------------------

def line(obj) -> str:
    """把一個事件序列化成串流裡的一行（讀取端餵進 feed 之前已經 strip 過）。"""
    return json.dumps(obj, ensure_ascii=False)


def claude_system(sid="sess-abc"):
    return line({"type": "system", "subtype": "init", "session_id": sid,
                 "model": "opus", "tools": ["Bash", "Read"]})


def claude_tool_use(tool_id="toolu_01", name="Bash"):
    return line({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": name,
         "input": {"command": "ls"}}]}})


def claude_tool_result(tool_id="toolu_01"):
    return line({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}})


def claude_result(text="完成了。", sid="sess-abc", **extra):
    ev = {"type": "result", "subtype": "success", "is_error": False,
          "result": text, "session_id": sid, "total_cost_usd": 0.0123,
          "usage": {"input_tokens": 2, "output_tokens": 4,
                    "cache_read_input_tokens": 18110}}
    ev.update(extra)
    return line(ev)


def claude_delta_start(index=0, block_type="text"):
    blk = {"type": block_type}
    if block_type == "text":
        blk["text"] = ""
    else:
        blk.update({"id": "toolu_01", "name": "Bash", "input": {}})
    return line({"type": "stream_event", "event": {
        "type": "content_block_start", "index": index, "content_block": blk}})


def claude_text_delta(text, index=0):
    return line({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": index,
        "delta": {"type": "text_delta", "text": text}}})


def claude_json_delta(partial, index=1):
    return line({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": index,
        "delta": {"type": "input_json_delta", "partial_json": partial}}})


def codex_started(thread_id="th-0199"):
    return line({"type": "thread.started", "thread_id": thread_id})


def codex_message(text="完成了。"):
    return line({"type": "item.completed", "item": {
        "id": "item_0", "type": "agent_message", "text": text}})


def codex_turn_done(**usage):
    return line({"type": "turn.completed",
                 "usage": usage or {"input_tokens": 120, "output_tokens": 40}})


def feed_all(state, lines, on_text=None):
    for raw in lines:
        state.feed(raw, on_text)
    return state


# ===========================================================================
# 一、Claude 折疊：正常一輪
# ===========================================================================

def test_a_normal_claude_round_folds_into_answer_and_session_id():
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_system("sess-abc"), claude_tool_use(),
                     claude_tool_result(), claude_result("完成了。", "sess-abc")])
    assert state.answer == "完成了。"
    assert state.sid == "sess-abc"
    assert state.last_result_ev.get("subtype") == "success"
    assert db._claude_stream_verdict(state, 0, "", None) == "ok"


def test_a_finished_tool_call_leaves_no_pending_tool():
    """`pending_tools` 是閒置看門狗的抑制條件：有工具在跑就不算閒置。收到
    `tool_result` 卻沒銷帳的話，一個真的閒置住的行程會被永遠當成「還在工作」。"""
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_tool_use("toolu_A"), claude_tool_use("toolu_B")])
    assert state.pending_tools == {"toolu_A", "toolu_B"}
    state.feed(claude_tool_result("toolu_A"))
    assert state.pending_tools == {"toolu_B"}


def claude_bg_tasks(*task_ids, sid="sess-abc", tasks=None):
    """`system`/`background_tasks_changed`：CLI 回報的背景工作**完整快照**。

    形狀照 2026-09-19 實測的事件抄，**連 session_id 一起帶**——真實串流的 system
    事件都有它，漏掉的話「session_id 分支吃掉整個 system 事件」那種寫法會測不到。
    """
    if tasks is None:
        tasks = [{"task_id": t, "task_type": "local_bash", "description": "probe"}
                 for t in task_ids]
    return line({"type": "system", "subtype": "background_tasks_changed",
                 "session_id": sid, "uuid": "u-1", "tasks": tasks})


def test_a_background_task_snapshot_is_tracked_and_replaced():
    """背景工作是閒置看門狗的第二個抑制條件（2026-09-19 事故：回合結尾留下監看工作，
    CLI 送出 result 之後靜默等它觸發，被閒置監看當成閒置砍掉）。事件帶的是**完整
    快照**，所以後一則整個取代前一則；空清單＝都結束了。"""
    state = db._ClaudeStreamState(None)
    state.feed(claude_bg_tasks("b2ydaq80d", sid="sess-bg"))
    assert state.background_tasks == {"b2ydaq80d"}
    # 同一則 system 事件的 session_id 也要照收——兩件事不可以互相吃掉。
    assert state.sid == "sess-bg"
    state.feed(claude_bg_tasks("b2ydaq80d", "k9"))
    assert state.background_tasks == {"b2ydaq80d", "k9"}
    state.feed(claude_bg_tasks("k9"))
    assert state.background_tasks == {"k9"}
    state.feed(claude_bg_tasks())
    assert state.background_tasks == set()


def test_background_tasks_do_not_touch_pending_tools_or_the_answer():
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_tool_use("toolu_A"), claude_bg_tasks("b1"),
                     claude_result("DONE")])
    assert state.pending_tools == {"toolu_A"}
    assert state.background_tasks == {"b1"}
    assert state.answer == "DONE"


@pytest.mark.parametrize("tasks", [
    None, 5, "b1", {"task_id": "b1"}, True,
])
def test_an_unreadable_task_snapshot_clears_the_set_instead_of_keeping_it(tasks):
    """`tasks` 不是 list：**清空**，不是保留舊值。保留舊值會安靜地一直抑制閒置
    看門狗；清空的失敗方向是「可能早一點開火」，有界而且大聲（見 `_protocol_key`）。"""
    state = db._ClaudeStreamState(None)
    state.feed(claude_bg_tasks("b1"))
    state.feed(claude_bg_tasks(tasks=tasks))
    assert state.background_tasks == set()


def test_malformed_task_entries_are_dropped_and_good_ones_kept():
    state = db._ClaudeStreamState(None)
    state.feed(claude_bg_tasks(tasks=[
        None, 7, "b-bare-string", [], {"description": "沒有 task_id"},
        {"task_id": ["不可雜湊"]}, {"task_id": {"a": 1}}, {"task_id": True},
        {"task_id": ""}, {"task_id": None},
        {"task_id": "good", "task_type": "local_bash"}]))
    assert state.background_tasks == {"good"}


def test_a_task_snapshot_on_another_subtype_is_ignored():
    """只有 `background_tasks_changed` 帶快照；其他 system 事件（例如 init）若剛好
    有 `tasks` 欄位，不可以被誤當成快照。"""
    state = db._ClaudeStreamState(None)
    state.feed(claude_bg_tasks("b1"))
    state.feed(line({"type": "system", "subtype": "task_started", "session_id": "s",
                     "task_id": "zz", "tasks": []}))
    assert state.background_tasks == {"b1"}


def test_the_result_event_can_move_the_session_id():
    """後端可能在 result 事件才報出（推進後的）工作階段 id。漏掉它，等額度回來
    resume 的會是上一輪的舊 id，這一輪做完的事全部白做。"""
    state = db._ClaudeStreamState("old-sid")
    state.feed(claude_result("嗨", sid="new-sid"))
    assert state.sid == "new-sid"


def test_a_result_event_without_a_session_id_keeps_the_old_one():
    state = db._ClaudeStreamState("old-sid")
    state.feed(line({"type": "result", "subtype": "success", "result": "嗨"}))
    assert state.sid == "old-sid"


def claude_init(sid="sess-abc", version="2.1.277"):
    """`system`/`init`，形狀照 2026-09-19 實跑的事件（兩版都帶 `claude_code_version`
    與 `capabilities`）。"""
    ev = {"type": "system", "subtype": "init", "session_id": sid, "model": "opus",
          "tools": [], "capabilities": ["interrupt_receipt_v1"]}
    if version is not None:
        ev["claude_code_version"] = version
    return line(ev)


def test_the_cli_version_is_read_from_the_init_event():
    """2.1.277 起 `--resume` 的金額是工作階段累計，換算要知道**這一次**叫用的是哪一版
    （CLI 會自動更新，bot 每輪重新起它）。版本讀到之後 session_id 也要照收——兩件事
    在同一則 system 事件裡，不可以互相吃掉。"""
    state = db._ClaudeStreamState(None)
    state.feed(claude_init("sess-v", " 2.1.277 "))
    assert state.cli_version == "2.1.277"
    assert state.sid == "sess-v"


@pytest.mark.parametrize("version", [None, 2.1, "", "   ", ["2.1.277"], {"v": 1}])
def test_an_unreadable_cli_version_is_left_unset(version):
    state = db._ClaudeStreamState(None)
    state.feed(claude_init(version=version))
    assert state.cli_version is None


def test_a_version_field_on_another_system_subtype_is_ignored():
    """只信 init。別的 system 事件剛好帶了同名欄位，不可以把版本換掉。"""
    state = db._ClaudeStreamState(None)
    state.feed(claude_init(version="2.1.276"))
    state.feed(line({"type": "system", "subtype": "task_started", "session_id": "s",
                     "claude_code_version": "9.9.9"}))
    assert state.cli_version == "2.1.276"


# ===========================================================================
# 二、Claude 折疊：壞行「跳過、繼續讀」，**永遠不 raise**
# ===========================================================================

UNPARSEABLE = [
    "",                                  # 空行
    "   ",                               # 只有空白
    "Warning: something happened",       # CLI 的純文字警告
    '{"type": "result", "resu',          # 被截斷的半行
    "\x00\xff not utf-8 shaped",         # 解碼替代字元留下的雜訊
]

# 合法 JSON、但**不是物件**。舊版直接 `ev.get(...)` 會丟 AttributeError。
NOT_AN_OBJECT = ["null", "123", "-4.5", '"just a string"', "[1, 2, 3]",
                 "true", "[]", '{}']

# 合法物件、但**欄位型別不對**。這一組是 2026-09-08 補的：`isinstance(ev, dict)`
# 那道守門只擋住了「整個事件不是物件」，`message` 是 `null`／字串／陣列、
# `content` 是數字、`result` 是數字或物件時照樣會 raise，而那正是同一個 bug 類別
# 深一層。修法見 `_ClaudeStreamState._content_blocks`。
WRONG_FIELD_TYPES = [
    line({"type": "assistant", "message": None}),
    line({"type": "user", "message": None}),
    line({"type": "assistant", "message": "不是物件"}),
    line({"type": "user", "message": []}),
    line({"type": "assistant", "message": {"content": 5}}),
    line({"type": "assistant", "message": {"content": "不是陣列"}}),
    line({"type": "user", "message": {"content": None}}),
    line({"type": "result", "result": 123}),
    line({"type": "result", "result": {"a": 1}}),
    line({"type": "result", "result": ["a"]}),
    line({"type": "stream_event", "event": None}),
    line({"type": "stream_event", "event": {"type": "content_block_delta",
                                            "index": 0, "delta": None}}),
    line({"type": "rate_limit_event", "rate_limit_info": "不是物件"}),
    line({"type": "assistant", "message": {"content": [None, 7, "x"]}}),
]

# 這一組**確實會**改到狀態（所以不在下面那支「不擾動」的名單裡），但仍然屬於
# 「不可以 raise」那一組。刻意不硬化，理由見
# `test_a_numeric_session_id_is_taken_as_is`。
TOLERATED_ODD_SHAPES = [line({"type": "system", "session_id": 123})]


@pytest.mark.parametrize(
    "raw", UNPARSEABLE + NOT_AN_OBJECT + WRONG_FIELD_TYPES + TOLERATED_ODD_SHAPES)
def test_a_bad_line_is_skipped_and_never_raises(raw):
    """`feed` 的 docstring 承諾「**永遠不 raise**」，而那個承諾是承重的：claude 這
    側的讀取迴圈沒有 try/finally，例外會逸出整支函式 → 子行程沒人 kill、stderr 抽水
    任務變孤兒、那個 session 的鎖永久卡住。"""
    db._ClaudeStreamState("sid").feed(raw)


@pytest.mark.parametrize("raw", UNPARSEABLE + NOT_AN_OBJECT + WRONG_FIELD_TYPES)
def test_a_bad_line_does_not_disturb_the_state(raw):
    """不只是「不 raise」——壞行也不可以把已經折好的狀態改掉。"""
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_system("sess-abc"), claude_tool_use("toolu_A")])
    state.feed(raw)
    assert state.sid == "sess-abc"
    assert state.pending_tools == {"toolu_A"}
    assert state.answer == ""


def test_a_numeric_session_id_is_taken_as_is():
    """記錄**現況**，不是主張它是對的。

    `session_id` 不是字串時 `state.sid` 會照收，之後會被寫回 slot、下一輪當成
    `--resume` 的引數送進 `create_subprocess_exec`——那裡會丟 TypeError。刻意不在這裡
    硬化，兩個理由：(a) 要走到這條路，得是後端自己改掉輸出格式（這個欄位不是使用者
    控制的），(b) 失敗形態是**吵的**（spawn 當場炸），不是本檔在防的那種靜默錯值。
    真正承重的保證是「不 raise」，那條在上面測了。哪天真的要收緊，把這支改成斷言
    `state.sid is None` 即可。
    """
    state = db._ClaudeStreamState(None)
    state.feed(line({"type": "system", "session_id": 123}))
    assert state.sid == 123


def test_a_bad_line_in_the_middle_does_not_lose_the_round():
    """一行雜訊不該讓整輪已經跑完的工作作廢——真正的失敗訊號是 rc 與 result 事件。"""
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_system("sess-abc"),
                     "Warning: deprecated flag",
                     "null",
                     line({"type": "assistant", "message": None}),
                     claude_result("還是拿到答案了。", "sess-abc")])
    assert state.answer == "還是拿到答案了。"
    assert db._claude_stream_verdict(state, 0, "", None) == "ok"


@pytest.mark.parametrize("raw", UNPARSEABLE + NOT_AN_OBJECT)
def test_a_bad_line_never_reaches_the_progress_callback(raw):
    """壞行不可以觸發進度更新——那會把雜訊推到對話平台上。"""
    seen = []
    db._ClaudeStreamState("sid").feed(raw, seen.append)
    assert seen == []


# ===========================================================================
# 三、Claude 折疊：逐 token 進度只吃「答案文字」（安全性質）
# ===========================================================================

def test_only_text_blocks_accumulate_into_the_progress_preview():
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_delta_start(0, "text"),
                     claude_text_delta("你", 0),
                     claude_text_delta("好", 0)])
    assert state.stream_text == "你好"
    assert state.text_block_indices == {0}


SECRET_ARG = '{"command":"type D:\\\\Codes\\\\Axiomatic\\\\auth.md"}'


def test_a_tool_calls_arguments_never_reach_the_progress_preview():
    """安全性質，不是版面問題：`stream_text` 會被送到對話平台，而工具呼叫的參數裡
    裝的是主機路徑與指令。"""
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_delta_start(0, "text"),
                     claude_text_delta("查一下…", 0),
                     claude_delta_start(1, "tool_use"),
                     claude_json_delta(SECRET_ARG, 1)])
    assert state.stream_text == "查一下…"
    assert "auth.md" not in state.stream_text
    assert "D:\\" not in state.stream_text
    assert state.text_block_indices == {0}


def test_a_text_delta_on_a_block_that_was_never_opened_as_text_is_dropped():
    """刻意的合成輸入：目前的後端不會在 tool_use 的 block 上送 `text_delta`。

    但白名單的意義正是「不依賴上游的良好行為」——`stream_text` 的保證由**這道
    索引過濾**提供，不是由「上游剛好不那樣送」提供。拿掉過濾之後只有這一筆會紅。
    """
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_delta_start(1, "tool_use"),
                     claude_text_delta("D:\\Work\\secret", 1)])
    assert state.stream_text == ""


def test_a_non_text_delta_on_a_text_block_is_dropped():
    """另一半的合成輸入：同一個條件的另一個子句（delta 型別必須是 `text_delta`）。

    跟上面那筆分開寫是因為兩道子句會互相遮蔽——只餵一種形狀的話，拿掉任一道都還有
    另一道擋著，兩個 mutation 會雙雙存活。
    """
    state = db._ClaudeStreamState(None)
    state.feed(claude_delta_start(0, "text"))
    state.feed(line({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "thinking_delta", "text": "內部推理"}}}))
    assert state.stream_text == ""


def test_the_progress_callback_gets_the_accumulated_text_not_the_piece():
    """回呼拿到的是**累積到目前為止**的整段，因為對話平台那側是重寫同一則訊息。"""
    seen = []
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_delta_start(0, "text"),
                     claude_text_delta("你", 0),
                     claude_text_delta("好", 0),
                     claude_text_delta("嗎", 0)], seen.append)
    assert seen == ["你", "你好", "你好嗎"]


def test_an_empty_text_delta_does_not_fire_the_callback():
    seen = []
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_delta_start(0, "text"),
                     claude_text_delta("", 0)], seen.append)
    assert seen == []
    assert state.stream_text == ""


def test_a_raising_progress_callback_cannot_break_the_stream():
    """進度更新失敗（對話平台 429、訊息被刪）絕不可以打掉整輪。"""
    calls = []

    def boom(text):
        calls.append(text)
        raise RuntimeError("edit failed")

    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_delta_start(0, "text"),
                     claude_text_delta("你", 0),
                     claude_text_delta("好", 0),
                     claude_result("完成了。", "sess-abc")], boom)
    assert calls == ["你", "你好"]          # 第一次丟例外之後仍繼續呼叫
    assert state.stream_text == "你好"
    assert state.answer == "完成了。"


# ===========================================================================
# 四、Claude 折疊：`rate_limit_event` → 真正的重設時刻
# ===========================================================================

FUTURE_EPOCH = 1788199200.0        # 落在 _DOROSSI_EPOCH_MIN/MAX 之間


def rate_limit_event(status="allowed", resets_at=FUTURE_EPOCH, top_level=True):
    info = {"status": status, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": 0.98,
                                             "resetsAt": resets_at},
                               "seven_day": {"utilization": 0.4,
                                             "resetsAt": resets_at + 86400}}}
    if top_level:
        info["resetsAt"] = resets_at
    return line({"type": "rate_limit_event", "rate_limit_info": info})


def test_the_rate_limit_event_is_remembered():
    state = db._ClaudeStreamState(None)
    state.feed(rate_limit_event())
    assert state.rate_reset == pytest.approx(FUTURE_EPOCH)


def test_the_last_rate_limit_event_wins():
    """每一次呼叫都會來好幾則；要的是最後那一則。"""
    state = db._ClaudeStreamState(None)
    state.feed(rate_limit_event(resets_at=FUTURE_EPOCH))
    state.feed(rate_limit_event(resets_at=FUTURE_EPOCH + 3600))
    assert state.rate_reset == pytest.approx(FUTURE_EPOCH + 3600)


def test_an_unreadable_rate_limit_event_leaves_the_previous_value_alone():
    """讀不出時刻時**不可以**把已經拿到的好值覆蓋成 None——那會讓等待退回用猜的。"""
    state = db._ClaudeStreamState(None)
    state.feed(rate_limit_event(resets_at=FUTURE_EPOCH))
    state.feed(line({"type": "rate_limit_event", "rate_limit_info": {}}))
    state.feed(line({"type": "rate_limit_event"}))
    assert state.rate_reset == pytest.approx(FUTURE_EPOCH)


def test_the_reset_time_reaches_the_usage_limit_exception():
    """端到端的那條鏈：串流事件 → `rate_reset` → 例外的 `reset_at` → 迴圈睡到那一刻。

    斷掉的話不會有任何錯誤，只是每次撞上限都退回「15 分鐘起跳、每次加倍」的猜，
    最壞白等好幾小時。
    """
    state = db._ClaudeStreamState("sid")
    feed_all(state, [rate_limit_event(status="rejected"),
                     claude_result("Claude usage limit reached.", sid="sid",
                                   subtype="error", is_error=True,
                                   api_error_status=429)])
    with pytest.raises(db._DorossiUsageLimitError) as got:
        db._claude_stream_verdict(state, 1, "", None)
    assert got.value.reset_at == pytest.approx(FUTURE_EPOCH)


# ===========================================================================
# 五、Claude `failure_reason`：來源優先序（result → stdout 尾巴 → stderr）
# ===========================================================================

def test_the_result_event_is_the_first_source_of_a_failure_reason():
    state = db._ClaudeStreamState(None)
    state.feed(claude_result("", sid="sid", subtype="error_during_execution",
                             is_error=True, api_error_status=529))
    reason = state.failure_reason("stderr 也有東西")
    assert "subtype=error_during_execution" in reason
    assert "api_error_status=529" in reason
    assert "stderr 也有東西" not in reason


def test_a_stream_cut_short_falls_back_to_the_stdout_tail():
    """**這是重點**：串流在收到 `result` 之前就斷了（後端被砍、管線斷掉），
    診斷不可以變成空字串——rc != 0 時那是唯一能查的東西。"""
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_system("sess-abc"), claude_tool_use(),
                     "panic: runtime error in backend"])
    reason = state.failure_reason("")
    assert reason not in ("", "(no output)")
    assert "panic: runtime error in backend" in reason


def test_a_stream_with_no_stdout_at_all_falls_back_to_stderr():
    state = db._ClaudeStreamState(None)
    assert "ENOENT" in state.failure_reason("spawn failed: ENOENT")


def test_a_silent_failure_still_says_something():
    assert db._ClaudeStreamState(None).failure_reason("") == "(no output)"


def test_the_stdout_tail_is_bounded_and_keeps_the_last_lines():
    """有界是因為它會被 print 進 log；保留**尾巴**是因為失敗訊息在最後面。"""
    state = db._ClaudeStreamState(None)
    for i in range(40):
        state.feed(f"noise-{i}")
    assert len(state.stdout_tail) == db._ClaudeStreamState._STDOUT_TAIL_LINES
    assert state.stdout_tail[-1] == "noise-39"
    assert "noise-0" not in state.failure_reason("")


def test_an_empty_line_is_not_kept_in_the_tail():
    state = db._ClaudeStreamState(None)
    feed_all(state, ["", "", "real"])
    assert state.stdout_tail == ["real"]


# ===========================================================================
# 六、Claude 判定：三條具名錯誤路徑（2026-09-05 事故的現場）
# ===========================================================================

def killed(reason, **kw):
    """一個被看門狗砍掉的串流。`kill_reason` 由**讀取端**設定，折疊端看不到時間。"""
    state = db._ClaudeStreamState(kw.pop("sid", "sess-abc"))
    for raw in kw.pop("lines", []):
        state.feed(raw)
    state.kill_reason = reason
    return state


def test_output_silence_raises_its_own_exception():
    """自走迴圈的每輪 backstop。它**不是** TimeoutError——迴圈要能分辨「這一輪卡住
    了（重生幾次再說）」與「一般逾時」。"""
    with pytest.raises(db._DorossiLoopSilence):
        db._claude_stream_verdict(killed("silence"), 1, "", "sess-abc",
                                  silence_limit=1800.0)


def test_the_hard_ceiling_reports_the_limit_that_was_actually_used():
    """報告的必須是這一輪真正用的（依模式、依設定）上限，不是寫死的數字。"""
    with pytest.raises(TimeoutError) as got:
        db._claude_stream_verdict(killed("hard"), 1, "", None,
                                  idle_limit=600.0, hard_limit=10800.0)
    assert "10800" in str(got.value)


def test_the_idle_tier_reports_the_idle_limit():
    with pytest.raises(TimeoutError) as got:
        db._claude_stream_verdict(killed("idle"), 1, "", None,
                                  idle_limit=600.0, hard_limit=10800.0)
    assert "600" in str(got.value)


def test_an_idle_kill_after_a_successful_result_keeps_the_answer():
    """2026-09-19 事故：答案早在 300 秒前就出來了，CLI 之後還掛著（等一個回合結尾
    留下的背景工作），閒置監看把它砍掉，而判定把手上的答案整個丟掉、回了「暫時無法
    回應」。已經收到成功的 result → 收下答案。

    rc 給 1 而且帶著 session_id 是刻意的：被砍掉的行程 rc 一定非零，落到 rc != 0
    那一段會變成 resume 重試——這支同時釘住「閒置出口不會落下去」。"""
    state = killed("idle", lines=[claude_system("sess-new"),
                                  claude_result("答完了。", sid="sess-new")])
    assert db._claude_stream_verdict(state, 1, "", "sess-old",
                                     idle_limit=600.0, hard_limit=10800.0) == "ok"
    assert state.answer == "答完了。"
    assert state.sid == "sess-new"


def test_an_idle_kill_with_no_result_still_fails():
    state = killed("idle", lines=[claude_system("sess-abc"),
                                  claude_text_delta("寫到一半")])
    with pytest.raises(TimeoutError):
        db._claude_stream_verdict(state, 1, "", "sess-abc", idle_limit=600.0)


def test_an_idle_kill_after_an_error_result_still_fails():
    """`subtype` 寫著 success、`is_error` 卻為真：不算答完。只看 subtype 的話，一個
    錯誤回合會被當成答案送出去。"""
    state = killed("idle", lines=[claude_result("出錯了", is_error=True)])
    with pytest.raises(TimeoutError):
        db._claude_stream_verdict(state, 1, "", "sess-abc", idle_limit=600.0)


def test_an_idle_kill_after_a_non_success_result_still_fails():
    state = killed("idle", lines=[claude_result("", subtype="error_during_execution",
                                                is_error=False)])
    with pytest.raises(TimeoutError):
        db._claude_stream_verdict(state, 1, "", "sess-abc", idle_limit=600.0)


def test_a_hard_kill_after_a_successful_result_keeps_the_answer(capsys):
    """2026-09-19 反轉（擁有者：「現在等待太短了，任務一直被殺掉」）。這支原本叫
    `test_the_hard_ceiling_still_fails_even_after_a_successful_result`，釘的是相反的規則
    ——當時保留答案的出口只給閒置。上限拉長之後，背景工作會把閒置監看一路壓到硬上限
    （full 預設 3 小時），一個永遠不觸發的背景工作可以把一輪撐滿 3 小時再**丟掉早就
    出來的答案**。硬上限仍然是那個邊界（砍的時刻不變），只是砍完之後答案留下。

    rc=1 ＋ session_id 同閒置那支：落到 rc != 0 會變成 resume 重試。"""
    state = killed("hard", lines=[claude_bg_tasks("b1"),
                                  claude_result("答完了。", sid="sess-new")])
    assert db._claude_stream_verdict(state, 1, "", "sess-old",
                                     idle_limit=600.0, hard_limit=10800.0) == "ok"
    assert state.answer == "答完了。"
    assert state.sid == "sess-new"
    err = capsys.readouterr().err
    assert "answer kept" in err and "10800" in err, err


def test_a_hard_kill_with_no_successful_result_still_fails():
    """邊界沒有跟著放寬：沒有成功的 result（答到一半、或是錯誤的 result）照舊丟例外。"""
    for lines in ([claude_bg_tasks("b1"), claude_text_delta("寫到一半")],
                  [claude_result("出錯了", is_error=True)],
                  [claude_result("", subtype="error_during_execution")]):
        with pytest.raises(TimeoutError) as got:
            db._claude_stream_verdict(killed("hard", lines=lines), 1, "", "sess-abc",
                                      idle_limit=600.0, hard_limit=10800.0)
        assert "hard" in str(got.value)


def test_a_loop_silence_kill_after_a_successful_result_keeps_the_answer(capsys):
    """原本叫 `test_output_silence_still_fails_even_after_a_successful_result`，同一天
    反轉。自走模式的沉默砍在 CLI 答完之後還掛著時（背景工作壓住沉默到牆鐘上限、或
    答完不離開），丟例外的下場是**沉默重試把這一輪整個重跑一次**——已經做完的工作
    再做一遍。收下答案，迴圈把它當成完成的一輪。"""
    state = killed("silence", lines=[claude_result("答完了。")])
    assert db._claude_stream_verdict(state, 1, "", "sess-abc",
                                     silence_limit=1800.0) == "ok"
    assert state.answer == "答完了。"
    assert "answer kept" in capsys.readouterr().err


def test_a_loop_silence_kill_with_no_successful_result_still_fails():
    for lines in ([], [claude_text_delta("寫到一半")],
                  [claude_result("出錯了", is_error=True)]):
        with pytest.raises(db._DorossiLoopSilence):
            db._claude_stream_verdict(killed("silence", lines=lines), 1, "",
                                      "sess-abc", silence_limit=1800.0)


@pytest.mark.parametrize("reason", ["hard", "silence"])
def test_a_kept_answer_after_hard_or_silence_is_still_checked_for_a_limit(reason):
    """收下答案走的是 rc==0 那條收尾，所以「result 文字本身就是上限通知」照樣被攔
    ——三種砍法共用同一個出口，不能只有閒置那條有這一步。"""
    state = killed(reason, lines=[claude_result("You've hit your usage limit.")])
    with pytest.raises(db._DorossiUsageLimitError):
        db._claude_stream_verdict(state, 1, "", "sess-abc", silence_limit=1800.0,
                                  idle_limit=600.0, hard_limit=10800.0)


def test_an_idle_kept_answer_is_still_checked_for_a_usage_limit_notice():
    """閒置出口走的是 rc==0 那條收尾，所以「result 文字本身就是上限通知」照樣被攔。"""
    state = killed("idle", lines=[claude_result("You've hit your usage limit.")])
    with pytest.raises(db._DorossiUsageLimitError):
        db._claude_stream_verdict(state, 1, "", "sess-abc", idle_limit=600.0)


def test_a_plan_usage_limit_raises_the_usage_exception_not_a_resume_retry():
    """用量上限不是「工作階段過舊」。走 resume 重試只是再燒一次呼叫、又把可以續接
    的對話丟掉——2026-09-05 之前那正是唯一的路。"""
    state = db._ClaudeStreamState("sess-abc")
    state.feed(claude_result("Claude usage limit reached. Your limit will "
                             "reset at 1pm (Etc/GMT+5)", sid="sess-abc",
                             subtype="error", is_error=True))
    with pytest.raises(db._DorossiUsageLimitError):
        db._claude_stream_verdict(state, 1, "", "sess-abc")


@pytest.mark.parametrize("status", sorted(db.DOROSSI_TRANSIENT_STATUSES))
def test_a_server_side_transient_failure_raises_the_transient_exception(status):
    """529 Overloaded／5xx 會自己好，迴圈要退避後**用同一個工作階段**重跑。"""
    state = db._ClaudeStreamState("sess-abc")
    state.feed(claude_result("", sid="sess-abc", subtype="error",
                             is_error=True, api_error_status=status))
    with pytest.raises(db._DorossiTransientError) as got:
        db._claude_stream_verdict(state, 1, "", "sess-abc")
    assert got.value.status == status


def test_an_overloaded_message_without_a_status_code_still_counts_as_transient():
    """狀態碼不一定會被帶進 result 事件，有時只留下英文訊息。"""
    state = db._ClaudeStreamState("sess-abc")
    state.feed(claude_result("API Error: Overloaded", sid="sess-abc",
                             subtype="error", is_error=True))
    with pytest.raises(db._DorossiTransientError):
        db._claude_stream_verdict(state, 1, "", "sess-abc")


def test_a_failed_resume_run_asks_for_one_fresh_session_retry():
    state = db._ClaudeStreamState("sess-old")
    state.feed(claude_result("", sid="sess-old", subtype="error",
                             is_error=True))
    with pytest.raises(db._DorossiResumeError):
        db._claude_stream_verdict(state, 1, "", "sess-old")


def test_a_failed_fresh_run_is_a_plain_failure_not_a_resume_retry():
    """沒有工作階段可以丟掉重開，所以不該走那條路。"""
    state = db._ClaudeStreamState(None)
    state.feed(claude_result("", subtype="error", is_error=True))
    with pytest.raises(RuntimeError) as got:
        db._claude_stream_verdict(state, 1, "boom", None)
    assert not isinstance(got.value, db._DorossiResumeError)


def test_a_usage_limit_that_comes_back_with_rc_zero_is_still_caught():
    """少數情況下上限會以 rc==0 回來，而 result 文字**本身**就是上限通知。不攔的話
    會把「你的額度用完了」當成正常答覆送給使用者。"""
    state = db._ClaudeStreamState("sess-abc")
    state.feed(claude_result("You've hit your usage limit.", sid="sess-abc"))
    with pytest.raises(db._DorossiUsageLimitError):
        db._claude_stream_verdict(state, 0, "", "sess-abc")


def test_a_normal_answer_with_rc_zero_is_not_mistaken_for_a_limit():
    """反面：判定字樣比對得很寬，一般答案不可以被誤判。"""
    state = db._ClaudeStreamState("sess-abc")
    state.feed(claude_result("我把佇列整理好了，共 12 對。", sid="sess-abc"))
    assert db._claude_stream_verdict(state, 0, "", "sess-abc") == "ok"


def claude_rate_event(status="allowed", **info):
    """`rate_limit_event`，形狀照 2026-08-31 實跑收到的事件（見
    `test_dorossi_usage_limit.REAL_RATE_LIMIT_EVENT`）。每一次呼叫都會有一則。"""
    body = {"status": status, "resetsAt": 1788199200, "rateLimitType": "five_hour"}
    body.update(info)
    return line({"type": "rate_limit_event", "rate_limit_info": body,
                 "uuid": "u-r", "session_id": "sess-abc"})


# 短到像一則通知、又踩到比對表的**答案**：長度那道擋不住，只剩串流的配額狀態。
_SHORT_MENTION = "Yes, that SDK has a rate limit."


def test_the_stream_status_is_captured_and_vetoes_a_short_mention():
    """2026-09-19：串流說這次呼叫放行了，成功回合的答案就不可能是配額拒絕。

    走真的折疊 ＋ 真的判定，所以「status 有讀到卻沒交給偵測」這種斷線也會紅——
    純函式測試看不到那一段。對照組（同一句話、沒有事件）必須被攔下，否則證明不了
    翻轉它的是否決。"""
    control = db._ClaudeStreamState("sess-abc")
    control.feed(claude_result(_SHORT_MENTION, sid="sess-abc"))
    with pytest.raises(db._DorossiUsageLimitError):
        db._claude_stream_verdict(control, 0, "", "sess-abc")

    state = db._ClaudeStreamState("sess-abc")
    feed_all(state, [claude_rate_event("allowed"),
                     claude_result(_SHORT_MENTION, sid="sess-abc")])
    assert state.rate_status == "allowed"
    assert db._claude_stream_verdict(state, 0, "", "sess-abc") == "ok"
    assert state.answer == _SHORT_MENTION


def test_the_stream_status_reaches_the_nonzero_exit_classifier_too():
    """rc != 0 那一個偵測呼叫也要拿到 status。成功的 result ＋ 非零 rc（答完之後行程
    才出事）不是用量上限：放行訊號在，就該落到後面的 resume 重試，而不是去睡一場
    幾小時的額度等待。"""
    state = db._ClaudeStreamState("sess-abc")
    feed_all(state, [claude_rate_event("allowed"),
                     claude_result(_SHORT_MENTION, sid="sess-abc")])
    with pytest.raises(db._DorossiResumeError):
        db._claude_stream_verdict(state, 1, "", "sess-abc")


def test_a_later_unreadable_rate_event_clears_the_status():
    """status 每一則都覆寫：前一則 allowed、後一則讀不懂時，留著舊的 allowed 會否決
    一則真的通知。讀不到＝None＝不否決。（`rate_reset` 刻意相反，保留上一個好值。）"""
    state = db._ClaudeStreamState(None)
    feed_all(state, [claude_rate_event("allowed"), claude_rate_event("mystery")])
    assert state.rate_status is None
    assert state.rate_reset == 1788199200.0


def test_a_real_limit_is_caught_even_after_an_earlier_allowed_event():
    """一輪裡可能打了好幾次後端，前面放行、最後一次才被擋。真實通知帶著 429，結構化
    欄位不經過否決。"""
    state = db._ClaudeStreamState("sess-abc")
    feed_all(state, [claude_rate_event("allowed"),
                     claude_result("You've hit your session limit · resets 4:50pm "
                                   "(Asia/Taipei)", sid="sess-abc",
                                   is_error=True, api_error_status=429)])
    with pytest.raises(db._DorossiUsageLimitError):
        db._claude_stream_verdict(state, 1, "", "sess-abc")


def test_the_usage_exception_carries_the_session_the_backend_advanced_to():
    """**「等待後續跑不會丟掉工作」的關鍵。** 上限幾乎都是做到一半才撞上；帶回去的
    必須是後端當下推進到的 id，不是這次叫用傳進去的舊 id。"""
    state = db._ClaudeStreamState("sess-old")
    feed_all(state, [claude_system("sess-new"),
                     claude_result("usage limit reached", sid="sess-new",
                                   subtype="error", is_error=True)])
    with pytest.raises(db._DorossiUsageLimitError) as got:
        db._claude_stream_verdict(state, 1, "", "sess-old")
    assert got.value.session_id == "sess-new"


def test_the_transient_exception_also_carries_the_advanced_session():
    state = db._ClaudeStreamState("sess-old")
    feed_all(state, [claude_system("sess-new"),
                     claude_result("", sid="sess-new", subtype="error",
                                   is_error=True, api_error_status=529)])
    with pytest.raises(db._DorossiTransientError) as got:
        db._claude_stream_verdict(state, 1, "", "sess-old")
    assert got.value.session_id == "sess-new"


# ===========================================================================
# 七、Claude 判定：**順序就是規則**
#
# 每一支都餵一個**同時滿足兩條**的輸入，確認先觸發的是排前面那條。三個看門狗理由
# 共用同一個 `kill_reason` 欄位、彼此互斥，湊不出衝突輸入，所以那三條各自驗自己
# 那一條路（上一節），另外用「rc 正常但仍被砍」釘住它們排在 rc 判定之前。
# ===========================================================================

def limit_result(**extra):
    """一個看起來像用量上限的 result 事件。"""
    return claude_result("Claude AI usage limit reached", sid="sess-abc",
                         subtype="error", is_error=True, **extra)


def test_silence_beats_a_usage_limit():
    state = killed("silence", lines=[limit_result()])
    with pytest.raises(db._DorossiLoopSilence):
        db._claude_stream_verdict(state, 1, "", "sess-abc", silence_limit=1800.0)


def test_silence_beats_the_budget_gate():
    state = killed("silence", lines=[claude_result(
        "", sid="sess-abc", subtype="error_max_budget_usd", is_error=True)])
    with pytest.raises(db._DorossiLoopSilence):
        db._claude_stream_verdict(state, 1, "", "sess-abc", silence_limit=1800.0)


def test_the_hard_ceiling_beats_a_usage_limit():
    state = killed("hard", lines=[limit_result()])
    with pytest.raises(TimeoutError):
        db._claude_stream_verdict(state, 1, "", "sess-abc", hard_limit=10800.0)


def test_the_idle_tier_beats_a_usage_limit():
    state = killed("idle", lines=[limit_result()])
    with pytest.raises(TimeoutError):
        db._claude_stream_verdict(state, 1, "", "sess-abc", idle_limit=600.0)


def test_a_watchdog_kill_wins_even_when_the_process_exited_zero():
    """看門狗砍掉的判定排在 rc 之前，所以不受 rc 影響——砍掉的行程回報 0 也一樣。"""
    with pytest.raises(db._DorossiLoopSilence):
        db._claude_stream_verdict(killed("silence"), 0, "", "sess-abc",
                                  silence_limit=1800.0)


def test_the_budget_gate_beats_a_usage_limit():
    """預算閘是**我們自己**設的單次花費上限，跟方案額度是兩件事——它要 graceful
    收尾（下一輪照樣 resume），不是走「等額度回來」那條路。"""
    state = db._ClaudeStreamState("sess-abc")
    state.feed(claude_result("usage limit reached", sid="sess-abc",
                             subtype="error_max_budget_usd", is_error=True))
    assert db._claude_stream_verdict(state, 1, "", "sess-abc") == "budget"


def test_the_budget_gate_beats_the_resume_retry():
    """重試會**再燒一次**預算，所以絕不能落到 resume 那條路。"""
    state = db._ClaudeStreamState("sess-abc")
    state.feed(claude_result("", sid="sess-abc", subtype="error_max_budget_usd",
                             is_error=True))
    assert db._claude_stream_verdict(state, 1, "", "sess-abc") == "budget"


def test_a_usage_limit_beats_a_transient_failure():
    """兩者都成立時走用量上限：它有真正的重設時刻可以等，比指數退避精準得多。"""
    state = db._ClaudeStreamState("sess-abc")
    state.feed(claude_result("usage limit reached", sid="sess-abc",
                             subtype="error", is_error=True,
                             api_error_status=503))
    with pytest.raises(db._DorossiUsageLimitError):
        db._claude_stream_verdict(state, 1, "", "sess-abc")


def test_a_transient_failure_beats_the_resume_retry():
    """resume 重試是把工作階段丟掉重開，對「伺服器過載」毫無幫助。"""
    state = db._ClaudeStreamState("sess-abc")
    state.feed(claude_result("", sid="sess-abc", subtype="error",
                             is_error=True, api_error_status=529))
    with pytest.raises(db._DorossiTransientError):
        db._claude_stream_verdict(state, 1, "", "sess-abc")


def test_a_usage_limit_beats_the_resume_retry():
    state = db._ClaudeStreamState("sess-abc")
    state.feed(limit_result())
    with pytest.raises(db._DorossiUsageLimitError):
        db._claude_stream_verdict(state, 1, "", "sess-abc")


# ===========================================================================
# 八、Claude 判定：預算閘 `return`，不 raise
# ===========================================================================

@pytest.mark.parametrize("result_ev_extra", [
    {"subtype": "error_max_budget_usd"},
    {"subtype": "error", "errors": ["Reached maximum budget ($5.00)"]},
])
def test_the_budget_gate_returns_a_label_instead_of_raising(result_ev_extra):
    """`test_bot_helpers` 已經在 AST 層釘過這條：raise 會**繞過**
    `_dorossi_round_info_and_record`，那一輪的花費不進帳本。這裡在行為層再釘一次。
    """
    state = db._ClaudeStreamState("sess-abc")
    state.feed(claude_result("", sid="sess-abc", is_error=True,
                             **result_ev_extra))
    assert db._claude_stream_verdict(state, 1, "", "sess-abc") == "budget"


def test_the_budget_round_still_hands_back_the_session_id():
    """graceful 收尾的意義就在這裡：這一輪當作沒有進展，但**下一輪要能續跑**。"""
    state = db._ClaudeStreamState("sess-old")
    feed_all(state, [claude_system("sess-new"),
                     claude_result("", sid="sess-new",
                                   subtype="error_max_budget_usd",
                                   is_error=True)])
    assert db._claude_stream_verdict(state, 1, "", "sess-old") == "budget"
    assert state.sid == "sess-new"
    assert state.answer == ""      # 預算被切掉的那一輪多半沒有答案


# ===========================================================================
# 九、Codex 折疊
# ===========================================================================

def test_a_normal_codex_round_folds_into_answer_thread_and_usage():
    state = db._CodexStreamState(None)
    feed_all(state, [codex_started("th-0199"), codex_message("完成了。"),
                     codex_turn_done(input_tokens=120, output_tokens=40)])
    assert state.answer == "完成了。"
    assert state.thread_id == "th-0199"
    assert state.usage == {"input_tokens": 120, "output_tokens": 40}
    assert db._codex_stream_verdict(state, 0, "", None) == "ok"


def test_a_codex_resume_keeps_the_thread_id_when_none_is_reported():
    state = db._CodexStreamState("th-old")
    feed_all(state, [codex_message("嗨")])
    assert state.thread_id == "th-old"


CODEX_UNPARSEABLE = ["", "   ", "not json at all", '{"type": "item.comp',
                     "null", "42", "[1,2]", '"str"', "true"]

CODEX_WRONG_TYPES = [
    line({"type": "item.completed", "item": None}),
    line({"type": "item.completed", "item": "不是物件"}),
    line({"type": "item.completed", "item": []}),
    line({"type": "turn.completed", "usage": None}),
    line({"type": "thread.started", "thread_id": None}),
    line({"type": "error", "error": 123}),
    line({"type": "error", "message": None}),
    line({"type": "error", "error": {"nothing": "useful"}}),
]


@pytest.mark.parametrize("raw", CODEX_UNPARSEABLE + CODEX_WRONG_TYPES)
def test_a_bad_codex_line_is_skipped_and_never_raises(raw):
    db._CodexStreamState("th-1").feed(raw)


@pytest.mark.parametrize("raw", CODEX_UNPARSEABLE + CODEX_WRONG_TYPES)
def test_a_bad_codex_line_does_not_disturb_the_state(raw):
    state = db._CodexStreamState(None)
    feed_all(state, [codex_started("th-0199"), codex_message("完成了。")])
    state.feed(raw)
    assert state.thread_id == "th-0199"
    assert state.answer == "完成了。"


def test_a_raising_codex_progress_callback_cannot_break_the_stream():
    def boom(_text):
        raise RuntimeError("edit failed")

    state = db._CodexStreamState(None)
    feed_all(state, [codex_started("th-1"), codex_message("完成了。"),
                     codex_turn_done()], boom)
    assert state.answer == "完成了。"
    assert state.usage


def test_codex_failure_text_is_collected_from_json_events():
    """**為什麼要收**：codex 走 JSON 事件輸出時，上限／伺服器錯誤訊息常常只出現在
    事件裡，stderr 是空的。收不到就退回舊行為——白白重開一個新工作階段，然後把整個
    無人值守任務判死。"""
    state = db._CodexStreamState("th-1")
    state.feed(line({"type": "turn.failed",
                     "error": {"message": "You've hit your usage limit."}}))
    assert any("usage limit" in t for t in state.failure_texts)


@pytest.mark.parametrize("event,expected", [
    ({"type": "error", "message": "429 Too Many Requests"}, "429"),
    ({"type": "stream.error", "error": "stream disconnected"}, "disconnected"),
    ({"type": "turn.aborted", "reason": "interrupted"}, "interrupted"),
    ({"type": "notice", "text": "server overloaded"}, "overloaded"),
    # 型別名稱在不同 codex 版本間會變，所以判定**不寫死型別**——沒有 type 也要收。
    ({"error": {"text": "insufficient_quota"}}, "quota"),
])
def test_codex_failure_text_is_collected_regardless_of_the_event_type_name(
        event, expected):
    state = db._CodexStreamState("th-1")
    state.feed(line(event))
    assert any(expected in t for t in state.failure_texts)


def test_a_normal_codex_event_is_not_collected_as_a_failure():
    """反面：三個已知的正常事件型別都有自己的分支，不可以落進失敗文字。"""
    state = db._CodexStreamState(None)
    feed_all(state, [codex_started("th-1"), codex_message("完成了。"),
                     codex_turn_done()])
    assert state.failure_texts == []


def test_a_non_message_item_is_not_collected_as_a_failure():
    """`item.completed` 有自己的分支，所以工具執行的紀錄不會被當成失敗文字——
    否則每一次執行指令都會污染 rc!=0 的分類。"""
    state = db._CodexStreamState(None)
    state.feed(line({"type": "item.completed", "item": {
        "type": "command_execution", "command": "ls",
        "aggregated_output": "rate limit"}}))
    assert state.failure_texts == []
    assert state.answer == ""


def test_the_collected_failure_text_is_bounded():
    """會被拼進 blob 再 print 進 log，不能讓一則巨大的錯誤把 log 灌爆。"""
    state = db._CodexStreamState(None)
    state.feed(line({"type": "error", "message": "x" * 5000}))
    assert all(len(t) <= 500 for t in state.failure_texts)


# ===========================================================================
# 十、Codex 判定
# ===========================================================================

def codex_failed(text, thread_id="th-1"):
    state = db._CodexStreamState(thread_id)
    if text:
        state.feed(line({"type": "error", "message": text}))
    return state


def test_a_successful_codex_round_short_circuits_before_any_classification():
    """rc==0 直接回 "ok"，就算串流裡混進過像失敗的文字也一樣。"""
    state = codex_failed("rate limit warning")
    assert db._codex_stream_verdict(state, 0, "some stderr noise", "th-1") == "ok"


def test_a_codex_usage_limit_is_classified_from_the_json_event_alone():
    """**stderr 是空的**——這正是收集 JSON 失敗事件的理由。"""
    state = codex_failed("You've hit your usage limit. Try again later.")
    with pytest.raises(db._DorossiUsageLimitError) as got:
        db._codex_stream_verdict(state, 1, "", "th-1")
    assert got.value.session_id == "th-1"


def test_a_codex_usage_limit_is_also_read_from_stderr():
    """兩個來源都要吃：blob 是 stderr ＋ 事件文字接起來的。"""
    state = db._CodexStreamState("th-1")
    with pytest.raises(db._DorossiUsageLimitError):
        db._codex_stream_verdict(state, 1, "Error: 429 too many requests", "th-1")


@pytest.mark.parametrize("text", ["503 Service Unavailable", "server overloaded",
                                  "Bad Gateway", "internal server error"])
def test_a_codex_transient_failure_is_classified(text):
    state = codex_failed(text)
    with pytest.raises(db._DorossiTransientError):
        db._codex_stream_verdict(state, 1, "", "th-1")


def test_a_failed_codex_resume_asks_for_one_fresh_session_retry():
    state = codex_failed("session not found")
    with pytest.raises(db._DorossiResumeError):
        db._codex_stream_verdict(state, 1, "", "th-1")


def test_a_failed_fresh_codex_run_is_a_plain_failure():
    state = db._CodexStreamState(None)
    with pytest.raises(RuntimeError) as got:
        db._codex_stream_verdict(state, 1, "boom", None)
    assert not isinstance(got.value, db._DorossiResumeError)


def test_a_codex_usage_limit_beats_the_resume_retry():
    """2026-09-05 之前 codex 這側**只有** resume 重試那條路：撞到上限時會先白白重開
    一個新工作階段（一樣會撞上），然後把整個無人值守任務判死。"""
    state = codex_failed("You've hit your usage limit.")
    with pytest.raises(db._DorossiUsageLimitError):
        db._codex_stream_verdict(state, 1, "", "th-1")


def test_a_codex_transient_failure_beats_the_resume_retry():
    state = codex_failed("529 overloaded")
    with pytest.raises(db._DorossiTransientError):
        db._codex_stream_verdict(state, 1, "", "th-1")


def test_a_codex_usage_limit_wins_over_a_transient_marker():
    """兩種字樣同時出現時走用量上限（有重設時間可以等，比退避精準）。

    注意這一支**驗的是結果，不是 verdict 裡的順序**：`_dorossi_codex_transient`
    自己就會在看到用量字樣時回 None，所以把 verdict 裡那兩行對調，結果一樣是用量
    上限。也就是說 codex 這側的順序被偵測器的自我防護遮蔽了、湊不出區分輸入——
    下一支測那道防護本身，兩支合起來才把這個性質釘牢。
    """
    state = codex_failed("503 Service Unavailable: usage limit reached")
    with pytest.raises(db._DorossiUsageLimitError):
        db._codex_stream_verdict(state, 1, "", "th-1")


def test_the_codex_transient_detector_declines_when_it_sees_a_usage_limit():
    """上一支說的那道自我防護。它消失的話，`_codex_stream_verdict` 裡的排序就變成
    唯一的防線——所以兩支都要有。"""
    assert db._dorossi_codex_transient(
        "503 Service Unavailable: usage limit reached") is None
    assert db._dorossi_codex_transient("503 Service Unavailable") is not None


# ===========================================================================
# 十一、`feed` 的「永遠不 raise」：**掃整個類別，不是列舉個案**
#
# 2026-09-08 第二輪。第一輪只列舉了當時發現的那幾筆，於是修好之後**同一個 bug
# 類別在別的巢狀位置照樣活著**——系統性掃描（每個骨架 × 每個巢狀位置 × 每種 JSON
# 進得來的敵意值）在 Claude 側又找出 30 筆、歸成 6 類、分屬三種互相獨立的機制：
#
#   (A) 對非 dict 呼叫 `.get()`。`X or {}` 只擋 falsy，擋不掉 truthy 的非 dict。
#   (B) 把不可雜湊的值放進 set。JSON array → `list`，`add()` 與 `discard()` 都炸。
#   (C) 輸入本身不是 str。`json.loads(None)` 丟 **TypeError** 不是 ValueError。
#
# 所以這一節守的是**類別**：下一次有人在事件裡加欄位，只要把骨架補進語料，
# 所有位置就自動被涵蓋。個案列舉留在前面幾節當可讀的說明，這裡才是防線。
#
# 骨架與實作的同步由 `test_the_skeleton_corpus_covers_every_event_type_the_code_
# handles` 盯著——它從原始碼把 feed 實際比對的事件型別抽出來，語料少一個就紅。
# ===========================================================================

# JSON 進得來、而程式多半沒預期的值。`True`/`False` 在裡面是刻意的：
# `isinstance(True, int)` 為真，所以 bool 會溜過「只收 str/int」那種過濾。
HOSTILE_VALUES = [None, 5, "字串", [], [1], True, False, {}, {"a": 1}, 0.5, -1,
                  "", 10 ** 20]

# 每個骨架都照真實串流長相寫。**刻意包含解析路徑會真的走到的巢狀結構**——
# 例如 `rate_limit_event` 一定要用 `rate_limit_info` + `resetsAt` +
# `unifiedWindows`，寫成別的鍵名的話那個事件型別的解析路徑一次都不會被走到，
# 掃描看起來是綠的但其實什麼都沒掃。
CLAUDE_EVENT_SKELETONS = [
    {"type": "system", "subtype": "init", "session_id": "s1",
     "model": "opus", "tools": ["Bash"], "apiKeySource": "none",
     "memory_paths": {"auto": "C:/x/memory/"}, "claude_code_version": "2.1.276"},
    {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10,
     "retry_delay_ms": 600, "error_status": 401, "error": "authentication_failed",
     "session_id": "s1"},
    {"type": "system", "subtype": "background_tasks_changed", "session_id": "s1",
     "tasks": [{"task_id": "b2ydaq80d", "task_type": "local_bash",
                "description": "probe"}]},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "嗨"},
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "ls"}}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": "ok"}], "is_error": False}]}},
    {"type": "result", "subtype": "success", "is_error": False,
     "result": "答案", "session_id": "s1", "total_cost_usd": 0.1,
     "usage": {"input_tokens": 2, "cache_read_input_tokens": 1},
     "modelUsage": {"opus": {"inputTokens": 1}}},
    {"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
     "errors": ["Reached maximum budget ($5.00)"], "session_id": "s1"},
    {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1788199200,
        "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 0.9,
                                         "resetsAt": 1788199200},
                           "seven_day": {"resetsAt": 1788285600}}}},
    {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""}}},
    {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "id": "t1", "name": "Bash",
                          "input": {}}}},
    {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "hi"}}},
    {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": "{}"}}},
    {"type": "stream_event", "event": {"type": "message_stop"}},
]

CODEX_EVENT_SKELETONS = [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "item.completed", "item": {"id": "i1", "type": "agent_message",
                                        "text": "答案"}},
    {"type": "item.completed", "item": {"id": "i2", "type": "command_execution",
                                        "command": "ls",
                                        "aggregated_output": "x"}},
    {"type": "item.started", "item": {"id": "i3", "type": "reasoning"}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2,
                                         "cached_input_tokens": 3}},
    {"type": "turn.failed", "error": {"message": "boom", "code": 429}},
    {"type": "error", "message": "boom"},
    {"type": "notice", "text": "hi"},
    {"type": "whatever", "reason": "hi"},
    {"error": {"text": "沒有 type 欄位"}},
]


def _positions(obj, prefix=()):
    """走訪所有可替換／可刪除的巢狀位置。"""
    out = [prefix] if prefix else []
    if isinstance(obj, dict):
        for key, val in obj.items():
            out.extend(_positions(val, prefix + (key,)))
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            out.extend(_positions(val, prefix + (i,)))
    return out


def _set_at(obj, path, value):
    node = obj
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return obj


def _del_at(obj, path):
    node = obj
    for step in path[:-1]:
        node = node[step]
    del node[path[-1]]
    return obj


def _hostile_lines(skeleton):
    """(說明, 一行 JSON)：每個位置換成每種敵意值，再加上「把該位置刪掉」。"""
    for path in _positions(skeleton):
        label = ".".join(str(s) for s in path)
        for bad in HOSTILE_VALUES:
            yield (f"{label}={bad!r}",
                   line(_set_at(copy.deepcopy(skeleton), path, bad)))
        try:
            yield (f"刪掉 {label}", line(_del_at(copy.deepcopy(skeleton), path)))
        except (KeyError, IndexError, TypeError):
            pass


def _sweep(state_cls, skeleton):
    broke = []
    for why, raw in _hostile_lines(skeleton):
        try:
            state_cls().feed(raw)
        except Exception as error:                    # noqa: BLE001
            broke.append(f"  {why}\n    -> {type(error).__name__}: {error}"
                         f"\n    {raw[:110]}")
    return broke


@pytest.mark.parametrize(
    "skeleton", CLAUDE_EVENT_SKELETONS,
    ids=[s.get("type", "無type") + "/" + str(
        (s.get("event") or {}).get("type", "")
        if isinstance(s.get("event"), dict) else s.get("subtype", ""))
        for s in CLAUDE_EVENT_SKELETONS])
def test_no_hostile_shape_of_a_claude_event_can_make_feed_raise(skeleton):
    """每個巢狀位置 × 每種敵意值 × 再加刪鍵，一個都不准 raise。"""
    broke = _sweep(db._ClaudeStreamState, skeleton)
    assert not broke, (
        "`_ClaudeStreamState.feed` 的 docstring 承諾「永遠不 raise」，但這些形狀會：\n"
        + "\n".join(broke)
        + "\n\n讀取迴圈**沒有** try/finally，例外會逸出整支函式 → 子行程沒人 kill、"
        "stderr 抽水任務變孤兒、那個 session 的鎖永久卡住。\n"
        "修的時候請用「一次擋掉整個類別」的寫法（`_event_dict` / `_protocol_key`），"
        "不要逐點打補丁——那正是這個 bug 類別會原地復發的原因。")


@pytest.mark.parametrize(
    "skeleton", CODEX_EVENT_SKELETONS,
    ids=[s.get("type", "無type") for s in CODEX_EVENT_SKELETONS])
def test_no_hostile_shape_of_a_codex_event_can_make_feed_raise(skeleton):
    """codex 側目前是乾淨的，納入是為了**防止退化**——兩個 feed 是同一個契約。"""
    broke = _sweep(db._CodexStreamState, skeleton)
    assert not broke, (
        "`_CodexStreamState.feed` 一樣承諾「永遠不 raise」，但這些形狀會：\n"
        + "\n".join(broke))


@pytest.mark.parametrize("state_cls", [db._ClaudeStreamState, db._CodexStreamState],
                         ids=["claude", "codex"])
@pytest.mark.parametrize("raw", [None, 5, [], {}, 0.5, True, b'{"type":"x"}',
                                 bytearray(b"{}")])
def test_feed_survives_an_argument_that_is_not_even_a_string(state_cls, raw):
    """機制 (C)：`json.loads(None)` 丟的是 **TypeError**，不是 ValueError。

    目前的讀取端一定傳 str（`line.decode(...).strip()`），所以這條走不到——但
    「永遠不 raise」是**這個函式自己宣告的契約**，不是「目前這個呼叫端剛好不會踩」。
    契約要嘛守住，要嘛改掉 docstring 別再承諾；留在「宣稱不會但其實會」最糟。
    """
    state_cls().feed(raw)


def test_a_deeply_nested_or_exotic_json_line_does_not_raise():
    for raw in ('{"a":' * 5000 + "1" + "}" * 5000,          # 極深巢狀
                '{"type":"rate_limit_event","rate_limit_info":'
                '{"resetsAt":NaN,"unifiedWindows":{"five_hour":'
                '{"resetsAt":Infinity}}}}',                  # NaN / Infinity
                '{"type":"result","result":"\\ud800"}',      # 落單的代理字元
                json.dumps({"type": "result", "result": "x" * 100000})):
        db._ClaudeStreamState().feed(raw)
        db._CodexStreamState().feed(raw)


def test_the_skeleton_corpus_covers_every_event_type_the_code_handles():
    """語料要跟實作同步——**從原始碼推**，不要靠人記得補。

    掃描的力量完全來自語料涵蓋面：漏一個事件型別，那條解析路徑就一次都不會被走到，
    而掃描仍然是綠的（看起來像「已經很安全」）。所以這裡把兩個 `feed` 裡**實際拿來
    比對的字串常數**抽出來，要求每一個都在語料裡出現過。加新事件型別時這支會先紅。
    """
    src = (PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def compared_strings(cls_name):
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == cls_name)
        fn = next(n for n in ast.walk(cls)
                  if isinstance(n, ast.FunctionDef) and n.name == "feed")
        found = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Compare) and len(node.ops) == 1 \
                    and isinstance(node.ops[0], ast.Eq):
                for side in (node.left, node.comparators[0]):
                    if isinstance(side, ast.Constant) and isinstance(side.value, str):
                        found.add(side.value)
        return found

    for cls_name, corpus in (("_ClaudeStreamState", CLAUDE_EVENT_SKELETONS),
                             ("_CodexStreamState", CODEX_EVENT_SKELETONS)):
        blob = json.dumps(corpus, ensure_ascii=False)
        missing = sorted(s for s in compared_strings(cls_name) if s not in blob)
        assert not missing, (
            f"{cls_name}.feed 會比對這些字串，但掃描語料裡一個都沒有："
            f"{missing}。\n那條解析路徑因此**一次都沒被掃到**——掃描是綠的，"
            "但它什麼都沒證明。請把對應的事件骨架補進語料。")


# ---------------------------------------------------------------------------
# 十二、掃描**看不到**的兩個性質（自我檢查逼出來的盲點）
#
# 把每一道守門逐一還原、再跑一次掃描，證明掃描抓得到——有兩道還原之後掃描仍然是
# 綠的，因為它們根本不是「會不會 raise」的問題：
#   * `_protocol_key` 讓 `bool` 通過 → 是**別名**問題（`1 in {True}` 成立）。
#   * `failure_reason` 的 `join` → 掃描只呼叫 `feed`，碰不到那條路。
# 所以這兩個各自有專屬的行為測試。**掃描能證明的東西有邊界，要知道邊界在哪。**
# ---------------------------------------------------------------------------

def test_a_boolean_index_cannot_alias_a_real_block_index():
    """`isinstance(True, int)` 為真，所以「只收 str/int」擋不住 `bool`——而
    `1 in {True}` **成立**。少了那道 bool 排除，`index: true` 的文字區塊會讓
    index 為 1 的**工具**區塊看起來像已知的文字區塊，工具參數就洩進進度預覽。
    這是安全性質被別名繞過，不是型別潔癖。
    """
    state = db._ClaudeStreamState(None)
    state.feed(line({"type": "stream_event", "event": {
        "type": "content_block_start", "index": True,
        "content_block": {"type": "text"}}}))
    assert state.text_block_indices == set()
    state.feed(claude_json_delta(SECRET_ARG, 1))
    state.feed(line({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "text_delta", "text": "D:\\Work\\secret"}}}))
    assert state.stream_text == ""


def test_a_missing_index_cannot_alias_another_block_with_a_missing_index():
    """同一條別名的另一半：欄位**缺漏**時 `.get("index")` 回 `None`，若照收，
    「沒有 index 的文字區塊」與「沒有 index 的工具區塊」就會互相配對。"""
    state = db._ClaudeStreamState(None)
    state.feed(line({"type": "stream_event", "event": {
        "type": "content_block_start", "content_block": {"type": "text"}}}))
    assert state.text_block_indices == set()
    state.feed(line({"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "洩漏"}}}))
    assert state.stream_text == ""


def test_a_malformed_tool_id_is_dropped_symmetrically():
    """(B) 選的是**丟掉**而不是轉字串。

    轉換會發明一個永遠配不到的幽靈鍵，而 `pending_tools` 留著配不掉的項目會一直
    抑制閒置監看（硬上限雖然仍會兜底，但那是**安靜地**讓守門失效）；丟掉的失敗方向
    是「監看可能早一點開火」——有界，而且會帶著診斷大聲失敗。

    關鍵是 `add` 與 `discard` 用**同一道**濾網，所以兩邊永遠對稱：進不去的也不需要
    被移除。（`discard` 對不可雜湊的值同樣丟 TypeError，這點很容易漏。）
    """
    state = db._ClaudeStreamState(None)
    state.feed(line({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": ["壞掉的 id"]},
        {"type": "tool_use", "id": "good"}]}}))
    assert state.pending_tools == {"good"}
    state.feed(line({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": ["壞掉的 id"]},
        {"type": "tool_result", "tool_use_id": "good"}]}}))
    assert state.pending_tools == set()


def test_failure_reason_survives_a_tail_that_is_not_all_strings():
    """機制 (C) 修好之後，非 str 的行也進得了 `stdout_tail`，而 `join` 對非 str
    元素會丟 TypeError。診斷路徑同樣不該炸——它正是 rc != 0 時唯一的線索。"""
    state = db._ClaudeStreamState(None)
    state.feed(5)
    state.feed(None)
    state.feed("真的是一行文字")
    assert "真的是一行文字" in state.failure_reason("")


# ---------------------------------------------------------------------------
# 十三、防禦性取值的兩個 helper 本身
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, 5, "字串", [], [1], True, 0.5])
def test_event_dict_refuses_everything_that_is_not_a_dict(value):
    """`X or {}` 只擋得掉 falsy；`5` / `"字串"` / `[1]` 都是真值，會一路走到
    `.get()` 丟 AttributeError。這就是機制 (A)。"""
    assert db._event_dict({"k": value}, "k") == {}


def test_event_dict_passes_a_real_dict_through_unchanged():
    inner = {"type": "text"}
    assert db._event_dict({"k": inner}, "k") is inner
    assert db._event_dict({}, "missing") == {}


@pytest.mark.parametrize("value,expected", [
    ("t1", "t1"), (0, 0), (7, 7), (-1, -1),
    (None, None), ([], None), ([1], None), ({}, None), ({"a": 1}, None),
    (0.5, None), (True, None), (False, None),
])
def test_protocol_key_only_admits_the_types_the_protocol_uses(value, expected):
    assert db._protocol_key(value) is expected


@pytest.mark.parametrize("value", [[], {}, [1], {"a": 1}])
def test_protocol_key_output_is_always_safe_to_put_in_a_set(value):
    """真正要保證的是這件事：回傳值一定放得進 set。"""
    got = db._protocol_key(value)
    probe = set()
    probe.add(got)
    probe.discard(got)


# ---------------------------------------------------------------------------
# codex 後端的命令列組裝——**沙箱那一行是安全性的判定點**
# ---------------------------------------------------------------------------
# 2026-09-08 補：實測 `_dorossi_via_codex` 的函式本體**一行都沒有被執行過**，包含
# 決定要不要送 `--dangerously-bypass-approvals-and-sandbox` 的那個 if。而 codex CLI
# 是**真的裝在這台機器上**的（0.145.0），擁有者一個指令就能切過去，所以這不是死路。
#
# `CLAUDE.md` 把 `dorossi_cc_tools="full"` 描述成「移除核准關卡、可在主機無確認執行
# shell 與讀寫檔案」——比 `/host` 還重。組錯 argv 的兩個方向都是實害：多送那個旗標
# 等於在不該的時候拆掉沙箱；漏掉 `sandbox_mode="read-only"` 則是把保護交給主機上
# 那份使用者設定碰巧長什麼樣（原始碼的註解正是為此才每次重新宣告）。
#
# 這一族只驗 **argv**：假的 `create_subprocess_exec` 收下參數就丟一個哨符例外，
# 所以完全不會起任何行程，也不需要讓整條串流機制跑起來。


@pytest.fixture(autouse=True)
def _backend_workspace_in_tmp(tmp_path, monkeypatch):
    """後端的預設工作目錄導到暫存區。

    沒給 `workdir` 時，兩個叫用函式都會在 spawn 之前 `mkdir` 受管的
    `DOROSSI_CC_WORKDIR`——也就是 repo 裡 bot 正在用的那一個。這一族假的
    `create_subprocess_exec` 攔得住 spawn，攔不住它前面的 mkdir；2026-09-19 的 repo
    寫入守門第一次跑整套就在這個檔抓到 36 支。目錄剛好已經存在時 mkdir 什麼都不做，
    但在全新 clone 上它會真的把目錄建出來，所以不能靠「反正已經有了」。"""
    monkeypatch.setattr(db, "DOROSSI_CC_WORKDIR", tmp_path / "dorossi_workspace")


class _ArgvCaptured(Exception):
    """哨符：argv 已經收到，不要再往下跑（避免真的起行程）。"""

    def __init__(self, args, kwargs):
        super().__init__("argv captured")
        self.args_seen = list(args)
        self.kwargs_seen = kwargs


def _codex_argv(monkeypatch, *, tools="off", session_id=None,
                extra_dir=None, workdir=None):
    """跑一次 `_dorossi_via_codex`，回傳它**準備要執行**的 argv。"""
    monkeypatch.setattr(db, "find_codex_executable", lambda: r"C:\fake\codex.exe")
    monkeypatch.setattr(db, "DOROSSI_CC_TOOLS", tools)

    async def _fake_exec(*args, **kwargs):
        raise _ArgvCaptured(args, kwargs)

    monkeypatch.setattr(db.asyncio, "create_subprocess_exec", _fake_exec)
    try:
        asyncio.run(db._dorossi_via_codex(
            "問題", session_id, extra_dir=extra_dir, workdir=workdir))
    except _ArgvCaptured as captured:
        return captured.args_seen, captured.kwargs_seen
    raise AssertionError(
        "假的 create_subprocess_exec 沒有被呼叫到——argv 根本沒組起來，"
        "下面每一條斷言都會是空轉。")


def test_full_tools_mode_passes_the_sandbox_bypass_flag(monkeypatch):
    """`dorossi_cc_tools="full"` → 送出繞過核准與沙箱的旗標。"""
    argv, _ = _codex_argv(monkeypatch, tools="full")
    assert "--dangerously-bypass-approvals-and-sandbox" in argv, argv


def test_without_full_tools_the_sandbox_is_reasserted_read_only(monkeypatch):
    """反方向，而且**這一條才是真正要守的**。

    只驗「full 會送旗標」的話，一個**永遠**送旗標的實作也會全綠——而那正好是最糟
    的方向：在沒有開 full 的機器上把沙箱拆掉。所以這裡兩件事一起斷言：旗標**不在**，
    而且 `sandbox_mode="read-only"` **有**被重新宣告（原始碼註解說明了為什麼要每次
    重新宣告，而不是相信主機上那份使用者設定）。
    """
    argv, _ = _codex_argv(monkeypatch, tools="off")
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv, argv
    assert "-c" in argv and 'sandbox_mode="read-only"' in argv, argv


@pytest.mark.parametrize("tools", ["", "OFF", "Full", "readonly", None])
def test_only_the_exact_string_full_unlocks_the_sandbox(monkeypatch, tools):
    """只有**剛好等於** `"full"` 才解鎖——大小寫不同、拼錯、空值都要走唯讀。

    fail-closed：這個判定的預設方向必須是「保護還在」。
    """
    argv, _ = _codex_argv(monkeypatch, tools=tools)
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv, (
        f"tools={tools!r} 竟然解鎖了沙箱")
    assert 'sandbox_mode="read-only"' in argv, argv


def test_a_new_session_gets_the_git_check_skipped_and_a_working_directory(
        monkeypatch, tmp_path):
    """沒有 session id → 新的一輪：`--json --skip-git-repo-check -C <cwd>`。"""
    argv, kwargs = _codex_argv(monkeypatch, workdir=str(tmp_path))
    assert argv[1] == "exec", argv
    assert "resume" not in argv, "沒有 session id 卻走了 resume"
    assert "--skip-git-repo-check" in argv, argv
    assert "-C" in argv and str(tmp_path) in argv, argv
    assert kwargs.get("cwd") == str(tmp_path), kwargs
    assert argv[-1] == "-", "prompt 要從 stdin 進去（結尾的 `-`）"


def test_resuming_a_session_switches_to_resume_and_passes_the_id(
        monkeypatch):
    """有 session id → `exec resume --json … <id> -`。

    順序有意義：id 要在旗標之後、`-` 之前。
    """
    argv, _ = _codex_argv(monkeypatch, session_id="sess-123")
    assert argv[1:3] == ["exec", "resume"], argv
    assert argv[-2:] == ["sess-123", "-"], argv
    # resume 那條路**不該**再指定工作目錄或跳過 git 檢查（那些是開新局才有的）
    assert "--skip-git-repo-check" not in argv, argv


def test_an_extra_directory_is_only_added_on_a_new_session(monkeypatch,
                                                           tmp_path):
    """`--add-dir` 只掛在開新局那條路上；resume 不吃這個參數。"""
    argv_new, _ = _codex_argv(monkeypatch, extra_dir=str(tmp_path))
    assert "--add-dir" in argv_new and str(tmp_path) in argv_new, argv_new

    argv_resume, _ = _codex_argv(monkeypatch, session_id="s", extra_dir=str(tmp_path))
    assert "--add-dir" not in argv_resume, (
        f"resume 那條路不吃 --add-dir，送過去會讓 codex 直接拒絕：{argv_resume}")


def test_a_missing_codex_executable_raises_instead_of_running_something_else(
        monkeypatch):
    """找不到 codex → `FileNotFoundError`，不可以退回去執行別的東西。"""
    monkeypatch.setattr(db, "find_codex_executable", lambda: None)

    async def _boom(*_a, **_k):
        raise AssertionError("找不到執行檔的時候不該起任何行程")

    monkeypatch.setattr(db.asyncio, "create_subprocess_exec", _boom)
    with pytest.raises(FileNotFoundError):
        asyncio.run(db._dorossi_via_codex("問題", None))


# ---------------------------------------------------------------------------
# 預設後端（claude_code）的命令列組裝——純聊天的工具封鎖
# ---------------------------------------------------------------------------
# 跟上面 codex 那一族同一個理由，但這一條更重要：**這是實際在用的預設後端**。
#
# 2026-09-19 之前，純聊天的「關起來」只靠**一張手寫的工具名清單**：
#
#     --disallowedTools Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit
#
# 列舉式的保護只擋得住它列出來的東西，而那一天實測（CLI 2.1.276）它已經漏了：只帶這
# 一層時 init 事件仍列出 18～22 個工具，其中 ListAgents（列出本機其他互動式工作階段）與
# SendMessage **不經核准**就執行得到。這正是 `CLAUDE.md` 對 `_OWNER_ONLY_GROUPS` 說過的
# 同一件事：「群組式所以 fail-closed；**不要**換成一張逐條列舉的清單」。
#
# 現在純聊天帶**空的白名單** `--tools ""`（init 的工具列表是 0 個），列舉那一層留著當第二
# 道。下面的測試因此是**白名單形式優先**：主斷言是「`--tools` 後面緊跟一個空字串」；
# 列舉清單仍然釘住（刪一項會紅），但它已經不是唯一撐著的東西。
#
# **空字串要是 argv 裡一個真的空元素，這件事單元測試證明不了**——假的
# `create_subprocess_exec` 收到的是 Python 串列，看不到 Windows 的 `list2cmdline` 與 CLI
# 自己的剖析器怎麼處理它。所以同日用**正式程式碼**對真的 CLI 跑過一次
# （`<repo 外的暫存目錄>\purechat_real_call_probe.py`：呼叫 `_dorossi_via_claude_code`
# 本身、只在旁邊記下串流裡的 init 事件），init 的工具列表是 `[]`、答案照常回來。


def _claude_argv(monkeypatch, *, tools="off", session_id=None, effort=None,
                 max_budget_usd=None, model=None, extra_dir=None,
                 workdir=None, loop_system_guidance=None):
    """跑一次 `_dorossi_via_claude_code`，回傳它**準備要執行**的 argv。"""
    monkeypatch.setattr(db._shutil, "which", lambda _n: r"C:\fake\claude.exe")
    monkeypatch.setattr(db, "DOROSSI_CC_TOOLS", tools)

    async def _fake_exec(*args, **kwargs):
        raise _ArgvCaptured(args, kwargs)

    monkeypatch.setattr(db.asyncio, "create_subprocess_exec", _fake_exec)
    try:
        asyncio.run(db._dorossi_via_claude_code(
            "問題", session_id, extra_dir=extra_dir, workdir=workdir,
            max_budget_usd=max_budget_usd, model=model, effort=effort,
            loop_system_guidance=loop_system_guidance))
    except _ArgvCaptured as captured:
        return captured.args_seen, captured.kwargs_seen
    raise AssertionError(
        "假的 create_subprocess_exec 沒有被呼叫到——argv 根本沒組起來。")


# 純聊天模式下**必須**被擋掉的工具（第二層，列舉黑名單）。這張表是刻意重寫一次而不是
# 從原始碼抽的：從原始碼抽等於拿它自己驗自己，刪掉一項照樣全綠。
_MUST_BE_BLOCKED = ("Bash", "Edit", "Write", "Read", "Glob", "Grep",
                    "WebFetch", "WebSearch", "Task", "NotebookEdit")


def _tools_allowlist(argv) -> list:
    """argv 裡每一個 `--tools` 後面緊跟的那個值（依出現順序）。"""
    return [argv[i + 1] for i, a in enumerate(argv)
            if a == "--tools" and i + 1 < len(argv)]


def test_chat_only_mode_passes_an_empty_tool_allowlist(monkeypatch):
    """純聊天的主要那一層：`--tools` 後面緊跟**一個空字串**，而且只出現一次。

    白名單是 fail-closed 的——CLI 之後新增的工具不會自己出現在純聊天裡；列舉黑名單
    做不到這件事（2026-09-19 實測它漏了 18～22 個）。三種看起來差不多的寫法都不行，
    所以逐一排除：
      * 值不是 `""` 而是 `"default"`／任何工具名——那是**打開**工具；
      * `--tools` 後面直接接下一個旗標（值被吃掉、或根本沒帶值）——CLI 的變長參數
        會把下一個旗標當成下一個選項，工具清單就回到預設的全部；
      * 出現兩次——後面那個會蓋掉前面的。
    """
    argv, _ = _claude_argv(monkeypatch, tools="off")
    assert _tools_allowlist(argv) == [""], (
        f"純聊天沒有帶「--tools 後面緊跟空字串」：{argv}")
    value = argv[argv.index("--tools") + 1]
    assert isinstance(value, str) and value == "", repr(value)


def test_chat_only_mode_keeps_the_enumerated_denylist_as_a_second_layer(monkeypatch):
    """第二層：`--disallowedTools` 仍然把每一個能碰主機的工具都列進去。

    它單獨存在時是 fail-open 的（上面那一支就是為此而加），但留著不是裝飾：萬一哪天
    `--tools ""` 的語意在 CLI 那一側變了，這一層仍然擋著最危險的那幾個。
    """
    argv, _ = _claude_argv(monkeypatch, tools="off")
    assert "--disallowedTools" in argv, argv
    blocked = argv[argv.index("--disallowedTools") + 1].split(",")
    missing = [t for t in _MUST_BE_BLOCKED if t not in blocked]
    assert not missing, (
        f"這些工具在純聊天模式下沒有被擋：{missing}。"
        "列舉式的保護只擋得住它列出來的東西——要新增就兩邊一起加。")
    assert "--permission-mode" not in argv, (
        "沒開 full 卻帶了 --permission-mode")


def test_full_tools_mode_switches_to_bypass_and_stops_blocking(monkeypatch):
    """`full` → `--permission-mode bypassPermissions`，而且兩層封鎖都不帶。

    `--tools ""` 若跟著跑進 full 模式，Dorossi 會變成一個一個工具都沒有的「完整
    agent」——不會報錯，只是每一件交辦的工作都做不了。"""
    argv, _ = _claude_argv(monkeypatch, tools="full")
    assert "--permission-mode" in argv, argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions", argv
    assert "--disallowedTools" not in argv, argv
    assert "--tools" not in argv, f"full 模式帶了工具白名單：{argv}"


@pytest.mark.parametrize("tools", ["", "OFF", "Full", "FULL", None, "chat"])
def test_only_the_exact_string_full_unlocks_the_tools(monkeypatch, tools):
    """fail-closed：只有剛好 `"full"` 才解鎖，其餘一律走純聊天的兩層封鎖。"""
    argv, _ = _claude_argv(monkeypatch, tools=tools)
    assert "--permission-mode" not in argv, f"tools={tools!r} 竟然解鎖了工具"
    assert _tools_allowlist(argv) == [""], (tools, argv)
    assert "--disallowedTools" in argv, argv


@pytest.mark.parametrize("session_id", [None, "sess-abc"])
def test_the_empty_allowlist_survives_every_other_optional_flag(monkeypatch,
                                                                 session_id):
    """其他選用旗標全開（resume、系統提示、額外目錄、預算、模型、思考力度）時，
    `--tools` 後面仍然緊跟空字串。CLI 的 `--tools <tools...>` 是變長參數，排列順序
    若讓它後面接到一個**非旗標**的值，那個值會被當成工具名吃進去。"""
    argv, _ = _claude_argv(monkeypatch, tools="off", session_id=session_id,
                           extra_dir=str(PKG_ROOT), max_budget_usd=1.5,
                           model="sonnet", effort="high",
                           loop_system_guidance="守則")
    assert _tools_allowlist(argv) == [""], argv
    after = argv[argv.index("--tools") + 2]
    assert after.startswith("--"), (
        f"`--tools \"\"` 後面接的不是另一個旗標，而是 {after!r}——會被當成工具名。")


# ---------------------------------------------------------------------------
# CLI 在開始之前就拒絕我們傳的旗標（`unknown option`）
# ---------------------------------------------------------------------------
# 純聊天加了 `--tools ""` 之後，一個不認得它的舊版 CLI 會讓**每一輪**純聊天都失敗。
# 2026-09-19 用 2.1.276 餵一個不存在的選項實測那種失敗長什麼樣：rc=1、stdout 全空、
# stderr 是 `error: unknown option '<旗標>'`。修正前它落到 resume 重試（白起一次行程、
# 以一模一樣的方式再失敗一次），log 讀起來也只是一個普通的後端錯誤。

_UNKNOWN_TOOLS = "error: unknown option '--tools'\n"


def test_an_unknown_option_is_named_and_not_retried(capsys):
    """rc=1、沒有 result、stderr 是剖析器那一句 → 專屬例外，**不是** resume 重試。"""
    state = db._ClaudeStreamState("sess-abc")
    with pytest.raises(db._DorossiCliOptionError) as got:
        db._claude_stream_verdict(state, 1, _UNKNOWN_TOOLS, "sess-abc")
    assert not isinstance(got.value, db._DorossiResumeError)
    assert got.value.option == "--tools"
    log = capsys.readouterr().err
    assert "does not know the option '--tools'" in log, log
    assert "update it" in log, "stderr 沒有講該做什麼（更新 CLI）"


def test_an_unknown_option_on_a_fresh_session_is_the_same_error():
    """沒有工作階段時也一樣：它不該被說成泛用的 `claude -p exited 1`。"""
    state = db._ClaudeStreamState(None)
    with pytest.raises(db._DorossiCliOptionError):
        db._claude_stream_verdict(state, 1, _UNKNOWN_TOOLS, None)


def test_a_run_that_started_is_not_blamed_on_an_old_cli():
    """近似案例：串流裡**有** result 事件＝回合已經開始了。stderr 裡剛好出現那串字，
    也不能被說成「CLI 太舊」——照舊走 resume 重試。"""
    state = db._ClaudeStreamState("sess-old")
    state.feed(claude_result("", sid="sess-old", subtype="error", is_error=True))
    with pytest.raises(db._DorossiResumeError):
        db._claude_stream_verdict(state, 1, _UNKNOWN_TOOLS, "sess-old")


@pytest.mark.parametrize("stderr", [
    "", "error: something else went wrong\n",
    "unknown option\n",                      # 沒有旗標名
    "error: unknown option 'tools'\n",       # 不是旗標的形狀
])
def test_other_startup_failures_still_take_the_resume_retry(stderr):
    """近似案例：沒有 result，但 stderr 不是剖析器那一句 → 照舊。比對若放寬到「有
    unknown option 這幾個字」，一個別的原因的失敗會被誤導成「更新 CLI」。"""
    state = db._ClaudeStreamState("sess-old")
    with pytest.raises(db._DorossiResumeError):
        db._claude_stream_verdict(state, 1, stderr, "sess-old")


def test_an_unknown_option_stops_an_unattended_loop_at_once():
    """重試沒有機會成功（CLI 不會在重試之間自己變新），所以是致命錯誤。"""
    assert db.dorossi_error_is_fatal(db._DorossiCliOptionError("--tools"))


# ---------------------------------------------------------------------------
# 後端 CLI 沒有可用的登入（2026-09-19 實測後補）
# ---------------------------------------------------------------------------
# 修正前這兩種形狀都落到最後兩條：有工作階段 → `_DorossiResumeError` → 呼叫端丟掉對話
# 重開（同樣失敗）→ `RuntimeError`，而那句文字一個致命字樣都不中，自走迴圈再重試三輪。
# 事件照 CLI 2.1.276 實測的形狀寫（`dorossi_backend._dorossi_cc_auth_failure` 上方的表）。

def auth_init(sid="sess-abc", *, memory=True, source="none"):
    ev = {"type": "system", "subtype": "init", "session_id": sid, "model": "haiku",
          "tools": [], "apiKeySource": source, "claude_code_version": "2.1.276"}
    if memory:
        ev["memory_paths"] = {"auto": "C:/x/memory/"}
    return line(ev)


def claude_api_retry(error="authentication_failed", status=401, attempt=1, sid="sess-abc"):
    return line({"type": "system", "subtype": "api_retry", "attempt": attempt,
                 "max_retries": 10, "retry_delay_ms": 600, "error_status": status,
                 "error": error, "session_id": sid})


# 實測的兩則 result（`--bare` 沒 key／`--bare` ＋ 無效 key）。
MEASURED_NOT_LOGGED_IN = {"type": "result", "subtype": "success", "is_error": True,
                          "api_error_status": None, "terminal_reason": "api_error",
                          "result": "Not logged in \u00b7 Please run /login",
                          "session_id": "sess-abc", "total_cost_usd": 0}
MEASURED_BAD_KEY = {"type": "result", "subtype": "success", "is_error": True,
                    "api_error_status": 401, "terminal_reason": "api_error",
                    "result": "Failed to authenticate. API Error: 401 API key is invalid.",
                    "session_id": "sess-abc", "total_cost_usd": 0}


def _auth_verdict(lines, session_id="sess-abc", err=""):
    state = db._ClaudeStreamState(session_id)
    feed_all(state, lines)
    return state, (lambda: db._claude_stream_verdict(state, 1, err, session_id))


def test_the_measured_not_logged_in_round_is_a_sign_in_failure(capsys):
    """bare 模式（或 `CLAUDE_CODE_SIMPLE=1`）的實測形狀：狀態碼是 null、沒有 api_retry，
    只剩文字。**不是** resume 重試——那會丟掉一段可以續接的對話、以同樣方式再失敗一次。"""
    _state, verdict = _auth_verdict([auth_init(memory=False),
                                     line(MEASURED_NOT_LOGGED_IN)])
    with pytest.raises(db._DorossiAuthError) as got:
        verdict()
    assert not isinstance(got.value, db._DorossiResumeError)
    assert got.value.evidence == "result text"
    assert got.value.bare_suspect is True
    log = capsys.readouterr().err
    assert "no usable sign-in (result text)" in log, log
    assert "/login" in log and "Not retrying with a fresh session" in log, log
    assert "looks like bare mode" in log, "init 沒有 memory_paths，stderr 應該點名 bare 模式"


def test_the_measured_bad_key_round_is_a_sign_in_failure(capsys):
    """401 的實測形狀：十則 api_retry、result 帶 401。判定靠的是 result 的狀態碼。"""
    retries = [claude_api_retry(attempt=n) for n in range(1, 11)]
    _state, verdict = _auth_verdict([auth_init()] + retries + [line(MEASURED_BAD_KEY)])
    with pytest.raises(db._DorossiAuthError) as got:
        verdict()
    assert got.value.evidence == "api_error_status=401"
    assert got.value.bare_suspect is False
    log = capsys.readouterr().err
    assert "no usable sign-in (api_error_status=401)" in log, log
    assert "bare mode" not in log, "一般模式（有 memory_paths）不該被說成 bare"


def test_a_401_status_alone_is_enough():
    """只有狀態碼、沒有 api_retry、文字也不中字樣——結構化欄位單獨就要夠。"""
    ev = dict(MEASURED_BAD_KEY, result="API Error")
    _state, verdict = _auth_verdict([auth_init(), line(ev)])
    with pytest.raises(db._DorossiAuthError) as got:
        verdict()
    assert got.value.evidence == "api_error_status=401"


@pytest.mark.parametrize("category", ["authentication_failed", "oauth_org_not_allowed",
                                      "cloud_credential_error"])
def test_a_credential_retry_category_alone_is_enough(category):
    """狀態碼空的、文字不中字樣，只有最後一則 api_retry 說是憑證類——CLI 自己的分類。"""
    ev = dict(MEASURED_NOT_LOGGED_IN, result="API Error")
    _state, verdict = _auth_verdict([auth_init(), claude_api_retry(category, None),
                                     line(ev)])
    with pytest.raises(db._DorossiAuthError) as got:
        verdict()
    assert got.value.evidence == f"api_retry={category}", got.value.evidence


@pytest.mark.parametrize("category", ["account_on_hold", "verification_required",
                                      "billing_error", "overloaded", "unknown"])
def test_other_retry_categories_are_not_a_sign_in_failure(category):
    """近似案例：CLI 自己也標成「卡住」的帳號類（凍結、需要驗證）同樣不會自己好，但
    「在主機上登入」是錯的處方——照舊走 resume／有上限的重試。"""
    ev = dict(MEASURED_NOT_LOGGED_IN, result="API Error")
    _state, verdict = _auth_verdict([auth_init(), claude_api_retry(category, None),
                                     line(ev)])
    with pytest.raises(db._DorossiResumeError):
        verdict()


def test_a_403_alone_is_not_a_sign_in_failure():
    """403 是 permission_error，未實測，可能只是方案用不到某個模型或中間有代理擋掉。"""
    ev = dict(MEASURED_BAD_KEY, api_error_status=403, result="API Error: 403")
    _state, verdict = _auth_verdict([auth_init(), line(ev)])
    with pytest.raises(db._DorossiResumeError):
        verdict()


@pytest.mark.parametrize("status", [403, 400])
def test_a_structured_non_401_status_outranks_the_text(status):
    """result 帶著別的狀態碼時，文字退路不看：結構化證據已經說了是別的事。403 那一格
    尤其要緊——CLI 的錯誤文字很可能照樣以「Failed to authenticate」開頭，而 403 的處方
    不是登入（見 `_dorossi_cc_auth_failure` 的 docstring）。只踩這一道：文字本身完全
    符合退路的條件（短、`is_error`、含字樣），沒有 api_retry。"""
    ev = dict(MEASURED_BAD_KEY, api_error_status=status,
              result=f"Failed to authenticate. API Error: {status} forbidden")
    _state, verdict = _auth_verdict([auth_init(), line(ev)])
    with pytest.raises(db._DorossiResumeError):
        verdict()


def test_a_403_counts_when_the_cli_names_a_credential_problem():
    """同一個 403，CLI 自己的分類（讀的是錯誤本文）說是憑證問題 → 算。"""
    ev = dict(MEASURED_BAD_KEY, api_error_status=403, result="API Error: 403")
    _state, verdict = _auth_verdict([auth_init(),
                                     claude_api_retry("oauth_org_not_allowed", 403),
                                     line(ev)])
    with pytest.raises(db._DorossiAuthError) as got:
        verdict()
    assert got.value.evidence == "api_retry=oauth_org_not_allowed/403"


def test_a_later_structured_status_overrides_an_earlier_retry_category():
    """result 是最後發生的事：它說 400，早先那則憑證類的重試就不作數。"""
    ev = dict(MEASURED_BAD_KEY, api_error_status=400, result="API Error: 400")
    _state, verdict = _auth_verdict([auth_init(), claude_api_retry(), line(ev)])
    with pytest.raises(db._DorossiResumeError):
        verdict()


def test_a_later_non_credential_retry_clears_an_earlier_one():
    """api_retry 每一則都覆寫：先一次 401（例如登入剛好在換新）、後面改成過載，最後那一次
    才是這一輪失敗的原因。"""
    ev = dict(MEASURED_NOT_LOGGED_IN, result="API Error")
    state, verdict = _auth_verdict([auth_init(), claude_api_retry(),
                                    claude_api_retry("server_error", 500, attempt=2),
                                    line(ev)])
    assert (state.retry_error, state.retry_status) == ("server_error", 500)
    with pytest.raises(db._DorossiResumeError):
        verdict()


def test_a_successful_round_is_never_a_sign_in_failure():
    """早先一次 401 被 CLI 自己重試過去、答案也出來了，行程卻因為別的原因非零離開——那一輪
    **有**登入。成功的 result 一律不算，不管前面的重試事件說什麼。"""
    _state, verdict = _auth_verdict([auth_init(), claude_api_retry(),
                                     claude_result("答案", sid="sess-abc")])
    with pytest.raises(db._DorossiResumeError):
        verdict()


def test_a_long_error_that_mentions_login_is_not_a_sign_in_failure():
    """長度上限：CLI 的通知是一行模板；一段幾百字的錯誤文字剛好提到 /login 不算。"""
    prose = ("The tool reported: please run /login before using the dashboard. " * 6)
    assert len(prose.strip()) > db._DOROSSI_AUTH_NOTICE_MAX_CHARS
    ev = dict(MEASURED_NOT_LOGGED_IN, result=prose)
    _state, verdict = _auth_verdict([auth_init(), line(ev)])
    with pytest.raises(db._DorossiResumeError):
        verdict()


def test_the_text_fallback_needs_an_error_result():
    """文字退路只用在 `is_error` 為真的 result 上：那時文字是 CLI 的錯誤，不是答案。"""
    ev = dict(MEASURED_NOT_LOGGED_IN, subtype="error_during_execution", is_error=False)
    _state, verdict = _auth_verdict([auth_init(), line(ev)])
    with pytest.raises(db._DorossiResumeError):
        verdict()


def test_a_successful_answer_that_talks_about_login_is_just_an_answer():
    """rc == 0 的成功答案一個字都不動——判定只在 rc != 0 那條路上。"""
    state = db._ClaudeStreamState("sess-abc")
    feed_all(state, [auth_init(), claude_result("Not logged in? Please run /login.")])
    assert db._claude_stream_verdict(state, 0, "", "sess-abc") == "ok"


def test_the_measured_stale_session_shape_still_takes_the_resume_retry():
    """resume 重試**真正**該管的形狀（實測 `--resume <不存在的 id>`）不能被新分支吃掉。"""
    ev = {"type": "result", "subtype": "error_during_execution", "is_error": True,
          "result": None, "session_id": "sess-abc",
          "errors": ["No conversation found with session ID: sess-abc"]}
    _state, verdict = _auth_verdict([line(ev)],
                                    err="No conversation found with session ID: sess-abc")
    with pytest.raises(db._DorossiResumeError):
        verdict()


def test_a_sign_in_failure_is_fatal_by_its_type():
    """重試不會成功：登入不會在重試之間自己回來。**型別**要成立，不能靠訊息剛好含致命
    字樣——訊息刻意不含，所以把它從 `_FATAL_ERROR_TYPES` 拿掉這支就紅。"""
    error = db._DorossiAuthError("api_error_status=401")
    assert isinstance(error, db._FATAL_ERROR_TYPES)
    assert db.dorossi_error_is_fatal(error)
    text = f"{type(error).__name__}: {error}".lower()
    assert not any(marker in text for marker in db._FATAL_ERROR_MARKERS), text


def test_a_usage_limit_beats_a_sign_in_failure():
    """兩者都成立時走用量上限：429／402 有自己的等待路徑。"""
    ev = dict(MEASURED_BAD_KEY, result="Claude AI usage limit reached")
    _state, verdict = _auth_verdict([auth_init(), line(ev)])
    with pytest.raises(db._DorossiUsageLimitError):
        verdict()


def test_a_transient_failure_beats_a_sign_in_failure():
    """兩者都成立（文字同時含過載與未登入字樣）時走暫時性故障的退避。"""
    ev = dict(MEASURED_NOT_LOGGED_IN, result="API Error: Overloaded. Not logged in")
    _state, verdict = _auth_verdict([auth_init(), line(ev)])
    with pytest.raises(db._DorossiTransientError):
        verdict()


def test_a_sign_in_failure_beats_the_rejected_option():
    """沒有 result、但有憑證類的 api_retry：CLI 已經開始打後端，旗標顯然收下了。"""
    _state, verdict = _auth_verdict([auth_init(), claude_api_retry()],
                                    err=_UNKNOWN_TOOLS)
    with pytest.raises(db._DorossiAuthError):
        verdict()


def test_the_init_and_retry_fields_are_folded():
    state = db._ClaudeStreamState("sess-abc")
    assert (state.init_memory_paths, state.api_key_source) == (None, None)
    feed_all(state, [auth_init(memory=False, source="ANTHROPIC_API_KEY"),
                     claude_api_retry("oauth_org_not_allowed", 403)])
    assert state.init_memory_paths is False
    assert state.api_key_source == "ANTHROPIC_API_KEY"
    assert (state.retry_error, state.retry_status) == ("oauth_org_not_allowed", 403)
    feed_all(state, [auth_init()])
    assert state.init_memory_paths is True and state.api_key_source == "none"


@pytest.mark.parametrize("error, status", [
    (None, None), (5, "401"), ([], True), ({"a": 1}, 401.0), ("x" * 500, False)])
def test_unreadable_retry_fields_are_dropped(error, status):
    state = db._ClaudeStreamState("sess-abc")
    state.feed(claude_api_retry(error, status))
    assert state.retry_status is None
    assert state.retry_error is None or (isinstance(state.retry_error, str)
                                         and len(state.retry_error) <= 64)


def test_a_bare_start_is_warned_about():
    state = feed_all(db._ClaudeStreamState("s"), [auth_init(memory=False)])
    warnings = db._dorossi_cc_startup_warnings(state)
    assert warnings == [db._DOROSSI_BARE_MODE_WARNING]


def test_a_normal_start_and_a_start_not_seen_are_not_warned_about():
    """必須放行：一般模式有 memory_paths、`apiKeySource` 是 "none"；沒看到 init ＝判斷
    不出來，不是警報。"""
    assert db._dorossi_cc_startup_warnings(
        feed_all(db._ClaudeStreamState("s"), [auth_init()])) == []
    assert db._dorossi_cc_startup_warnings(db._ClaudeStreamState("s")) == []


def test_an_api_key_source_is_warned_about_and_only_a_label_is_printed():
    state = feed_all(db._ClaudeStreamState("s"),
                     [auth_init(source="ANTHROPIC_API_KEY")])
    (warning,) = db._dorossi_cc_startup_warnings(state)
    assert "authenticating with ANTHROPIC_API_KEY" in warning, warning
    odd = feed_all(db._ClaudeStreamState("s"),
                   [auth_init(source="sk-ant-secret\n<value>")])
    (warning,) = db._dorossi_cc_startup_warnings(odd)
    assert "secret" not in warning and "unrecognised" in warning, warning


def test_every_new_stderr_line_is_encodable_on_this_console():
    """這幾行會在 stdout／stderr 是管線時經過 cp950（見 CLAUDE.md 的編碼規則後半）。"""
    lines = [db._DOROSSI_BARE_MODE_WARNING] + list(db._DOROSSI_CC_DROPPED_ENV.values())
    for text in lines:
        text.encode("cp950")


def test_no_mcp_servers_are_ever_loaded(monkeypatch):
    """`--strict-mcp-config` 一定要在。

    註解說明了理由：主機那份全域 MCP 設定會做冷啟動健康檢查，能把 `claude -p`
    卡住好幾分鐘——而那是在 bot 的事件迴圈上。這不是效能微調，是掛掉與不掛掉。
    """
    for tools in ("off", "full"):
        argv, _ = _claude_argv(monkeypatch, tools=tools)
        assert "--strict-mcp-config" in argv, (tools, argv)
        assert not any(a == "--mcp-config" for a in argv), (tools, argv)


_BG_CEILING_ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"


@pytest.mark.parametrize("tools", ["off", "full"])
def test_the_cli_background_wait_ceiling_is_the_bots_own_hard_limit(
        monkeypatch, tools):
    """CLI 自己有一把背景工作等待上限：最後的 result 之後連續閒置 10 分鐘就砍掉還在跑
    的背景 subagent（官方 headless 文件；2.1.276 執行檔 `WS=600000`）。擁有者抱怨的
    「任務一直被殺掉」一半是它——bot 的看門狗放得再寬也沒用。

    設成 bot 這一輪的硬上限（毫秒）：不是 0（bot 必須始終是最外層的有限邊界），也
    不比 bot 緊。值必須來自 `_dorossi_cc_hard_limit_sec()`——看門狗的 deadline 用的
    就是它，兩個數不能各算各的。"""
    monkeypatch.setattr(db, "_dorossi_cc_hard_limit_sec", lambda: 1234.5)
    _argv, kwargs = _claude_argv(monkeypatch, tools=tools)
    env = kwargs.get("env")
    assert isinstance(env, dict), "子行程沒有拿到自己的環境——CLI 會用預設的 10 分鐘"
    assert env.get(_BG_CEILING_ENV) == "1234500", env.get(_BG_CEILING_ENV)


def test_the_cli_ceiling_follows_the_real_mode_aware_limit(monkeypatch):
    """不換掉 `_dorossi_cc_hard_limit_sec`，走真的模式分派；兩個常數設成**預設值**
    （不讀這台機器的 bot_config.json，擁有者調低它時這支不該跟著紅）。full 模式的
    預設要比 CLI 內建的 10 分鐘寬——否則這一條等於沒做。"""
    import _bot_config as bc
    defaults = bc._DEFAULT_BOT_CONFIG
    monkeypatch.setattr(db, "DOROSSI_CC_HARD_LIMIT_FULL_SEC",
                        defaults["dorossi_cc_hard_limit_full_sec"])
    monkeypatch.setattr(db, "DOROSSI_CC_HARD_LIMIT_OFF_SEC",
                        defaults["dorossi_cc_hard_limit_off_sec"])
    for tools, key in (("full", "dorossi_cc_hard_limit_full_sec"),
                       ("off", "dorossi_cc_hard_limit_off_sec")):
        _argv, kwargs = _claude_argv(monkeypatch, tools=tools)
        raw = kwargs["env"][_BG_CEILING_ENV]
        assert raw.isdigit() and int(raw) == int(defaults[key] * 1000), (tools, raw)
    _argv, kwargs = _claude_argv(monkeypatch, tools="full")
    assert int(kwargs["env"][_BG_CEILING_ENV]) > 600_000, (
        "full 模式的預設沒有比 CLI 內建的 10 分鐘寬")


def test_an_inherited_ceiling_in_the_parent_env_does_not_win(monkeypatch):
    """父行程環境裡剛好帶著一個值（0＝無上限、或更短的數）時要**覆寫**，不是
    setdefault——同 `PYTHONIOENCODING` 那條教訓：啟動路徑帶的值不是我們設的。"""
    monkeypatch.setenv(_BG_CEILING_ENV, "0")
    monkeypatch.setattr(db, "_dorossi_cc_hard_limit_sec", lambda: 60.0)
    _argv, kwargs = _claude_argv(monkeypatch, tools="full")
    assert kwargs["env"][_BG_CEILING_ENV] == "60000"


def test_the_child_env_still_inherits_everything_else(monkeypatch):
    """只加一個鍵，其餘照繼承——少了 PATH、登入資訊之類的東西，CLI 根本起不來或變成
    未登入。"""
    monkeypatch.setenv("AXIOMATIC_ENV_PROBE_MARKER", "kept")
    _argv, kwargs = _claude_argv(monkeypatch, tools="full")
    assert kwargs["env"].get("AXIOMATIC_ENV_PROBE_MARKER") == "kept"


def test_the_child_env_ceiling_is_never_zero():
    """0 對 CLI 的意思是「無上限」，不是「立刻」。bot 的邊界再小也不可以變成 0。"""
    assert db._dorossi_cc_child_env(0.0001)[_BG_CEILING_ENV] == "1"


# 子行程**必須**拿不到的變數。刻意重寫一次而不是從 `_DOROSSI_CC_DROPPED_ENV` 抽——
# 從原始碼抽等於拿它自己驗自己，刪掉一項照樣全綠。
_MUST_NOT_REACH_THE_CLI = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_SIMPLE")
_BOGUS = "sk-ant-api03-bogus-value-for-tests"


@pytest.mark.parametrize("name", _MUST_NOT_REACH_THE_CLI)
def test_billing_and_bare_mode_variables_never_reach_the_cli(monkeypatch, name):
    """走真的呼叫點：`-p` 下只要環境有 API key 就一定用它（官方文件；實測
    `apiKeySource` 變成 "ANTHROPIC_API_KEY"），計費從訂閱換成按 token、沒有方案上限；
    `CLAUDE_CODE_SIMPLE=1` 等同 `--bare`。api 後端靠主機上設前兩個變數啟用，所以設一次
    就會安靜地改掉這個後端的計費。"""
    monkeypatch.setenv(name, _BOGUS)
    _argv, kwargs = _claude_argv(monkeypatch, tools="full")
    assert not any(k.upper() == name for k in kwargs["env"]), name
    assert os.environ.get(name) == _BOGUS, "只從子行程拿掉——本行程（api 後端）要照舊讀得到"


@pytest.mark.parametrize("name", ["CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                                  "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"])
def test_subscription_and_provider_choices_still_reach_the_cli(monkeypatch, name):
    """必須放行：OAuth token 就是訂閱憑證（實測 `apiKeySource` 仍是 "none"），雲端供應商
    變數是刻意的選擇。拿掉前者會讓只靠它登入的主機變成未登入。"""
    monkeypatch.setenv(name, "1")
    _argv, kwargs = _claude_argv(monkeypatch)
    assert kwargs["env"].get(name) == "1", name


def test_a_lowercase_name_in_a_plain_dict_is_dropped_too():
    """Windows 的環境變數名不分大小寫：一般 dict 裡的小寫鍵，CLI 照樣讀得到。"""
    env = db._dorossi_cc_child_env(60.0, {"anthropic_api_key": _BOGUS,
                                          "Claude_Code_Simple": "1", "KEEP": "k"})
    assert set(env) == {"KEEP", _BG_CEILING_ENV}, sorted(env)


def test_the_default_environment_is_read_at_call_time(monkeypatch):
    """`base_env=None` → 呼叫當下讀 `os.environ`（不是 `def` 當下綁定的那一份）。"""
    monkeypatch.setenv("AXIOMATIC_CALL_TIME_MARKER", "late")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _BOGUS)
    env = db._dorossi_cc_child_env(60.0)
    assert env.get("AXIOMATIC_CALL_TIME_MARKER") == "late"
    assert "ANTHROPIC_API_KEY" not in env


def test_a_dropped_variable_is_named_once_and_its_value_never_printed(capsys):
    base = {"ANTHROPIC_API_KEY": _BOGUS, "ANTHROPIC_AUTH_TOKEN": _BOGUS + "2"}
    db._dorossi_cc_child_env(60.0, base)
    db._dorossi_cc_child_env(60.0, base)
    err = capsys.readouterr().err
    assert "bogus" not in err, "值絕不能進 stderr（stderr 有 /log tail 這個出口）"
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 2, lines
    assert "ANTHROPIC_API_KEY is set" in err and "ANTHROPIC_AUTH_TOKEN is set" in err, err


def test_nothing_is_said_when_nothing_is_dropped(capsys):
    db._dorossi_cc_child_env(60.0, {"PATH": "x"})
    assert capsys.readouterr().err == ""


def test_a_new_session_carries_the_system_prompt_and_a_resume_does_not(
        monkeypatch):
    """新工作階段補系統提示；resume 沿用原本那份，不再重補。

    這一條有實際後果：重補會讓提示快取前綴失配，而且系統提示會累積。
    """
    argv_new, _ = _claude_argv(monkeypatch)
    assert "--append-system-prompt" in argv_new, argv_new
    assert "--resume" not in argv_new, argv_new

    argv_resume, _ = _claude_argv(monkeypatch, session_id="sess-9")
    assert "--resume" in argv_resume, argv_resume
    assert argv_resume[argv_resume.index("--resume") + 1] == "sess-9", argv_resume
    assert "--append-system-prompt" not in argv_resume, (
        "resume 又補了一次系統提示——那會讓快取前綴失配")


def test_loop_guidance_is_appended_even_when_resuming(monkeypatch):
    """自走守則要**每一輪**都到（含 resume），這是它跟系統提示的關鍵差別。"""
    argv, _ = _claude_argv(monkeypatch, session_id="s", loop_system_guidance="守則")
    assert "--append-system-prompt" in argv, argv
    assert "守則" in argv[argv.index("--append-system-prompt") + 1], argv


def test_optional_flags_are_omitted_rather_than_passed_empty(monkeypatch):
    """`--effort` / `--max-budget-usd` 沒指定就**完全不帶**，不是帶空值。

    帶空值會讓 CLI 用一個我們沒有選的值，或直接拒絕整個叫用。
    """
    argv, _ = _claude_argv(monkeypatch)
    assert "--effort" not in argv, argv
    assert "--max-budget-usd" not in argv, argv

    argv2, _ = _claude_argv(monkeypatch, effort="high", max_budget_usd=1.5)
    assert argv2[argv2.index("--effort") + 1] == "high", argv2
    assert argv2[argv2.index("--max-budget-usd") + 1] == "1.5", argv2

    # 0 與負數等於「沒有上限」，不可以變成 `--max-budget-usd 0`（那是「不准花錢」）。
    for value in (0, 0.0, -1):
        argv3, _ = _claude_argv(monkeypatch, max_budget_usd=value)
        assert "--max-budget-usd" not in argv3, (value, argv3)


def test_a_missing_claude_executable_raises(monkeypatch):
    """找不到 CLI → `FileNotFoundError`，不可以退回去執行別的東西。"""
    monkeypatch.setattr(db._shutil, "which", lambda _n: None)

    async def _boom(*_a, **_k):
        raise AssertionError("找不到執行檔的時候不該起任何行程")

    monkeypatch.setattr(db.asyncio, "create_subprocess_exec", _boom)
    with pytest.raises(FileNotFoundError):
        asyncio.run(db._dorossi_via_claude_code("問題", None))
