"""自走迴圈要**撐過 bot 重啟**，而不是只撐過用量上限。

`test_dorossi_usage_limit.py` 釘的是「後端擋住了 → 存檔、釋放名額、到時續跑」。這一份釘的是
另一半：**行程本身沒了**。`/sys restart`、supervisor 重啟、主機當機都會讓迴圈連同它那場長達數小時的等待一起蒸發，而舊
行為只在 slot 留一個 `loop_pending` 等擁有者事後手動 `/dorossi session continue`。無人
值守的長任務因此常常整夜卡在原地；磁碟上的續跑標記讓重啟後能接回來。

四個決定，每一個都有反例測試：

**一、「被砍死」與「自己停的」用 `finally` 區分。** 標記的 `live` 旗標只有兩個寫入點：
`_dorossi_mark_loop_pending` 設 True，`_dorossi_mark_loop_stopped` 設 False，而後者**只
從迴圈的 `finally` 呼叫**。`finally` 在任何自願結束（abort／沉默 backstop／例外／放棄／
自然收尾）都會跑，在行程被 kill 時一定不會跑。所以「重啟後 `live` 還是 True」精確等於
「上一個行程不是自己停的」。擁有者按過 `/dorossi abort` 的任務不會在下次重啟時自己活
過來——那是使用者明確表達過的意圖，不能被自動化推翻。

**二、權限用抓回來的訊息重驗，不信磁碟。** `dorossi_session.json` 是本機檔案、擁有者
自己就編輯得動。若拿存在裡面的 uid 當授權依據，手改一個 id 就能讓 bot 以擁有者身分起
一個 `full` 工具模式（bypassPermissions、可在主機無確認執行 shell）的自走迴圈。所以磁碟
上只存 `channel_id`／`message_id`，開機時把**那則真的訊息**抓回來，作者是不是擁有者以
抓回來的為準，slot 的 uid 也必須對得上。抓不回來就不接——fail-closed。斜線指令存的
`message_id` 其實是 interaction id，要從 bot 自己那則回覆的 `interaction_metadata` 找回
發起人（第五節；2026-09-19 以前這一條因此從來沒成功過）。

**三、心跳，不是啟動時間戳。** 年齡窗若從「迴圈啟動」起算，一個跑了三天的任務在重啟後
會因為「標記太舊」被拒絕——而那正是最該接回去的情形。所以每跑完一輪、以及每次睡進用量
等待之前都推一次心跳，`ts` 的語意是「最後一次被證實還活著」。

**四、當機迴圈要有斷路器，而且計數必須先落地。** 若「接續」本身就會把 bot 弄死，
重啟→接續→死掉→重啟 是一個無限迴圈。`auto_tries` 在**起跑之前**就寫進磁碟，所以死掉
那次也算數；任何一輪真的跑完就歸零，健康的長任務永遠累加不到上限。
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord  # noqa: E402
import discord_bot as b  # noqa: E402
import dorossi_backend as db  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"


def _marker(**over) -> dict:
    """一個「剛好可以自動接續」的 slot——每支測試各自破壞其中一項。"""
    slot = {"loop_pending": {"ts": time.time(), "task": "整理測試", "live": True,
                             "channel_id": 111, "message_id": 222}}
    slot["loop_pending"].update(over)
    return slot


# ---------------------------------------------------------------------------
# 一、標記的三個寫入點：mark / touch / stopped
# ---------------------------------------------------------------------------

def test_mark_records_the_anchor_and_marks_the_loop_live():
    sess: dict = {}
    db._dorossi_mark_loop_pending(sess, "任務", channel_id=7, message_id=8)
    marker = sess["loop_pending"]
    assert marker["live"] is True
    assert (marker["channel_id"], marker["message_id"]) == (7, 8)
    assert marker["task"] == "任務"


def test_a_re_mark_without_an_anchor_keeps_the_old_one():
    """接續走的是 `_dorossi_resume_loop` → `_dorossi_run_loop`，會再 mark 一次。
    若那次沒帶到 id（例如錨點訊息物件沒有 channel）就把舊的洗掉，這個 slot 從此
    再也不能自動接續——一次接續反而拆掉了接續能力。"""
    sess = _marker()
    db._dorossi_mark_loop_pending(sess, "", channel_id=None, message_id=None)
    marker = sess["loop_pending"]
    assert (marker["channel_id"], marker["message_id"]) == (111, 222)
    assert marker["task"] == "整理測試", "空描述要保留舊描述"


def test_a_re_mark_keeps_the_crash_loop_counter():
    """自動接續 → mark → 若計數被洗掉，斷路器就永遠是 1，等於沒有斷路器。"""
    sess = _marker(auto_tries=3)
    db._dorossi_mark_loop_pending(sess, "任務", channel_id=1, message_id=2)
    assert sess["loop_pending"]["auto_tries"] == 3


@pytest.mark.parametrize("fn", [
    lambda s: db._dorossi_touch_loop_pending(s),
    lambda s: db._dorossi_touch_loop_pending(s, reset_tries=True),
    lambda s: db._dorossi_mark_loop_stopped(s),
    lambda s: db._dorossi_count_autoresume(s),
])
def test_no_helper_ever_resurrects_a_finished_task(fn):
    """自然收尾（連續無進展）那條路會把整個 `loop_pending` 清掉。之後任何一個
    helper 若「順手建一個」，那個已經做完的任務就會在下次重啟時被自動跑起來。
    四個 helper 全部只准修改既有標記，不准建立。"""
    sess: dict = {"cc_session_id": "x"}
    fn(sess)
    assert "loop_pending" not in sess


def test_only_a_completed_round_clears_the_crash_loop_counter():
    sess = _marker(auto_tries=2)
    db._dorossi_touch_loop_pending(sess)
    assert sess["loop_pending"]["auto_tries"] == 2, (
        "睡進用量等待的心跳不算『跑完一輪』——它證明不了接續這個 slot 不會弄死 bot")
    db._dorossi_touch_loop_pending(sess, reset_tries=True)
    assert "auto_tries" not in sess["loop_pending"]


def test_a_touch_moves_the_heartbeat_forward():
    sess = _marker(ts=time.time() - 5000.0)
    db._dorossi_touch_loop_pending(sess)
    assert time.time() - sess["loop_pending"]["ts"] < 5.0


def test_stopped_only_flips_live_and_keeps_the_task_for_manual_resume():
    """自願停止**不清掉標記**——擁有者仍然可以手動 `/dorossi session continue`。
    改的只有「要不要自動接」這一件事。"""
    sess = _marker()
    db._dorossi_mark_loop_stopped(sess)
    assert sess["loop_pending"]["live"] is False
    assert sess["loop_pending"]["task"] == "整理測試"
    assert db._dorossi_loop_resume_plan(sess) is not None, "人工接續不受影響"


def test_the_counter_increments_from_junk():
    for junk in (None, "3", True, -1, [1]):
        sess = _marker(auto_tries=junk)
        db._dorossi_count_autoresume(sess)
        assert sess["loop_pending"]["auto_tries"] == 1, f"{junk!r}"


# ---------------------------------------------------------------------------
# 二、閘門：`_dorossi_loop_autoresume_plan`
# ---------------------------------------------------------------------------

def test_a_live_fresh_anchored_marker_is_resumable():
    assert db._dorossi_loop_autoresume_plan(_marker()) == (111, 222, 0)


def test_a_loop_that_stopped_on_its_own_is_never_auto_resumed():
    """`/dorossi abort`、沉默 backstop、例外、放棄——全都會走到迴圈的 `finally`，
    把 `live` 寫成 False。使用者明確喊過停的任務不該在重啟後自己活過來。"""
    assert db._dorossi_loop_autoresume_plan(_marker(live=False)) is None


@pytest.mark.parametrize("over", [
    {"channel_id": None}, {"message_id": None},
    {"channel_id": True}, {"message_id": True},          # bool 是 int 的子類別
    {"channel_id": 0}, {"message_id": -5},
    {"channel_id": "111"},
])
def test_a_marker_without_a_usable_anchor_is_left_for_manual_resume(over):
    """舊版標記沒有這兩個鍵——刻意只能人工接續，不是漏洞。順帶擋掉手改成怪值。"""
    assert db._dorossi_loop_autoresume_plan(_marker(**over)) is None


def test_a_marker_older_than_the_window_is_not_resumed():
    stale = _marker(ts=time.time() - db.DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC - 60)
    assert db._dorossi_loop_autoresume_plan(stale) is None


def test_a_heartbeat_from_the_future_is_treated_as_stale():
    """時鐘倒退（NTP 的 step 修正、手動改時鐘、虛擬機快照還原）會讓年齡變成負數。若只檢查 `age <= max`，負數
    永遠通過，等於整個年齡窗被繞過。"""
    assert db._dorossi_loop_autoresume_plan(
        _marker(ts=time.time() + 86400 * 30)) is None


@pytest.mark.parametrize("ts", [None, "now", True, float("nan")])
def test_a_junk_heartbeat_is_not_resumable(ts):
    assert db._dorossi_loop_autoresume_plan(_marker(ts=ts)) is None


def test_the_circuit_breaker_stops_a_crash_loop(monkeypatch):
    monkeypatch.setattr(db, "DOROSSI_LOOP_AUTORESUME_MAX_TRIES", 3)
    assert db._dorossi_loop_autoresume_plan(_marker(auto_tries=2)) == (111, 222, 2)
    assert db._dorossi_loop_autoresume_plan(_marker(auto_tries=3)) is None
    assert db._dorossi_loop_autoresume_plan(_marker(auto_tries=9)) is None


def test_zero_tries_means_unlimited(monkeypatch):
    monkeypatch.setattr(db, "DOROSSI_LOOP_AUTORESUME_MAX_TRIES", 0)
    assert db._dorossi_loop_autoresume_plan(_marker(auto_tries=99)) is not None


def test_a_zero_window_disables_the_feature(monkeypatch):
    """`dorossi_loop_autoresume_max_age_sec = 0` 是「關掉自動接續」的官方開關。

    `now` 刻意取標記自己的心跳，讓年齡**剛好是 0**。這是唯一能把這道閘與下面的
    年齡比對分開的輸入：任何真實的時間差都 > 0，於是 `age <= 0` 自己就擋掉了，
    兩道防護互相遮蔽——把「0 ＝關閉」整段刪掉，用真實時間跑的測試照樣全綠。
    """
    monkeypatch.setattr(db, "DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC", 0)
    sess = _marker()
    assert db._dorossi_loop_autoresume_plan(
        sess, now=sess["loop_pending"]["ts"]) is None


@pytest.mark.parametrize("age", [True, False])
def test_a_boolean_window_is_disabled_not_a_one_second_window(age, monkeypatch):
    """`True` 是 `int` 的子類別，所以 `isinstance(x, (int, float))` 會放行它，
    而 `True == 1`——設定檔被手改成 `true` 就會變成「心跳一秒內才接」，看起來像
    功能壞掉而不是設定寫錯。當成關閉處理。"""
    monkeypatch.setattr(db, "DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC", age)
    assert db._dorossi_loop_autoresume_plan(_marker()) is None


@pytest.mark.parametrize("age", ["86400", None, [], float("nan"), -1])
def test_a_non_numeric_window_disables_instead_of_raising(age, monkeypatch):
    """設定檔 loader 已經擋掉這些，這裡再擋一次是因為這個函式的契約是「純函式、
    永不 raise」——而它是 `on_ready` 的一環，在那裡拋例外會連帶影響 presence 與
    背景任務。沒有這道閘，`age <= "86400"` 會直接丟 TypeError。"""
    monkeypatch.setattr(db, "DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC", age)
    assert db._dorossi_loop_autoresume_plan(_marker()) is None


@pytest.mark.parametrize("marker", [None, "yes", [], 3, {"live": True}])
def test_a_malformed_marker_is_never_resumable(marker):
    assert db._dorossi_loop_autoresume_plan({"loop_pending": marker}) is None
    assert db._dorossi_loop_autoresume_plan({}) is None


# ---------------------------------------------------------------------------
# 三、開機掃描的真實行為（stub 掉 Discord 與 store）
# ---------------------------------------------------------------------------

OWNER = 400000000000000001


class _StubAuthor:
    def __init__(self, uid: int):
        self.id = uid


class _StubMessage:
    def __init__(self, uid: int):
        self.author = _StubAuthor(uid)
        self.id = 222


class _StubChannel:
    def __init__(self, author_id, boom=None):
        self._author_id, self._boom = author_id, boom

    async def fetch_message(self, mid):
        del mid
        if self._boom is not None:
            raise self._boom
        return _StubMessage(self._author_id)


class _StubClient:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, cid):
        del cid
        return self._channel


def _install(monkeypatch, state, *, author_id=OWNER, boom=None):
    """把開機掃描架在 stub 上，回傳 (scheduled, saved_state)。"""
    scheduled: list = []
    saved = {"state": state}

    def _fake_schedule(coro, *, label=""):
        scheduled.append(label)
        coro.close()          # 不真的跑迴圈，也避免 'never awaited' 警告

    async def _fake_rmw(mutate):
        return mutate(saved["state"])

    monkeypatch.setattr(b, "_dorossi_resume_inflight", set())
    monkeypatch.setattr(b, "_dorossi_autoresume_reported", set())
    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(b, "client", _StubClient(_StubChannel(author_id, boom)))
    monkeypatch.setattr(b, "_dorossi_load_state", lambda: saved["state"])
    monkeypatch.setattr(b, "_dorossi_state_rmw", _fake_rmw)
    monkeypatch.setattr(b, "_schedule_coro", _fake_schedule)
    return scheduled, saved


def _store(slot: dict, uid: str = str(OWNER)) -> dict:
    return {uid: {"active": "s1", "sessions": {"s1": slot}}}


def _run_scan() -> None:
    asyncio.run(asyncio.wait_for(b._dorossi_autoresume_pending_loops(), 10))


def test_a_killed_loop_is_picked_back_up_and_counted(monkeypatch):
    scheduled, saved = _install(monkeypatch, _store(_marker()))
    _run_scan()
    assert len(scheduled) == 1, "被砍死的迴圈沒有被接回去"
    assert saved["state"][str(OWNER)]["sessions"]["s1"][
        "loop_pending"]["auto_tries"] == 1, (
        "計數必須在起跑**之前**就落地，否則接續本身會弄死 bot 時就是無限重啟")


def test_a_loop_the_owner_aborted_is_left_alone(monkeypatch):
    scheduled, _ = _install(monkeypatch, _store(_marker(live=False)))
    _run_scan()
    assert not scheduled


def test_an_anchor_written_by_someone_else_is_refused(monkeypatch):
    """磁碟上的 uid 說是擁有者，抓回來的訊息作者卻不是。store 是本機檔案、手改得
    動，所以授權只能以抓回來的那則訊息為準——否則改一個數字就能讓 bot 以擁有者
    身分起一個 full 工具模式的自走迴圈。"""
    scheduled, saved = _install(monkeypatch, _store(_marker()), author_id=99)
    _run_scan()
    assert not scheduled
    assert "auto_tries" not in saved["state"][str(OWNER)]["sessions"]["s1"][
        "loop_pending"], "拒絕的嘗試不該消耗斷路器的額度"


def test_a_slot_filed_under_another_uid_is_refused(monkeypatch):
    """反方向：訊息作者是擁有者，但標記掛在別的 uid 底下（`_dorossi_resume_loop`
    會用作者 id 去找 slot，兩者不一致就會接到別人的 session）。"""
    scheduled, _ = _install(monkeypatch, _store(_marker(), uid="7"))
    _run_scan()
    assert not scheduled


def test_a_deleted_anchor_message_falls_back_to_manual(monkeypatch):
    scheduled, saved = _install(monkeypatch, _store(_marker()),
                                boom=RuntimeError("404 Not Found"))
    _run_scan()
    assert not scheduled
    assert saved["state"][str(OWNER)]["sessions"]["s1"]["loop_pending"], (
        "抓不到錨點時標記要留著，人工 `/dorossi session continue` 仍然可用")


def test_a_repeated_scan_never_resumes_the_same_loop_twice(monkeypatch):
    """掃描會在同一個行程裡跑很多次（`on_ready`、re-IDENTIFY、`on_resumed`、醒來）。

    舊版用「每個行程只做一次」的旗標擋重接，而那個旗標在 2026-09-22 斷網之後擋掉了
    唯一該做的那一次。現在擋重接的是「已經排定、還沒結束」的登記：第二次掃描看到
    同一個 slot 已經排進去了，就不再排一次（這裡的替身不真的跑迴圈，所以它不會出現
    在 `_dorossi_loops`——擋下它的只有那份登記）。"""
    scheduled, _ = _install(monkeypatch, _store(_marker()))
    _run_scan()
    _run_scan()
    assert len(scheduled) == 1


class _SlowChannel(_StubChannel):
    """抓錨點訊息要等網路。`during` 在那段空檔裡執行——模擬同一時間發生的另一件事。"""

    def __init__(self, author_id, during):
        super().__init__(author_id)
        self._during = during

    async def fetch_message(self, mid):
        await asyncio.sleep(0)
        self._during()
        await asyncio.sleep(0)
        return await super().fetch_message(mid)


_KEY = (str(OWNER), "s1")


@pytest.mark.parametrize("claim", [
    lambda: b._dorossi_resume_inflight.add(_KEY),     # `continue all` 排定了它
    lambda: b._dorossi_loops.__setitem__(_KEY, object()),  # 手動接續已經在跑
], ids=["continue-all", "manual"])
def test_a_resume_claimed_while_the_anchor_is_fetched_is_not_scheduled_again(
        monkeypatch, claim):
    """重啟之後擁有者最常做的事是 `/dorossi session continue all`，而那正是這個掃描在
    抓錨點（好幾個網路往返）的時候。迴圈開頭查過一次「沒人在接」，那個答案在空檔之後
    已經過時；照舊答案排下去，同一個 session 會被排兩次——第二個被迴圈的單一登記擋下、
    對擁有者貼一則「已有任務進行中」，斷路器的計數還白白多算一格。

    另一種重疊（兩次掃描同時跑）由 `_dorossi_resume_scan` 的忙碌旗標序列化掉了，
    正式路徑上碰不到，所以這裡測的是碰得到的那一種。"""
    scheduled, saved = _install(monkeypatch, _store(_marker()))
    monkeypatch.setattr(b, "client", _StubClient(_SlowChannel(OWNER, claim)))
    _run_scan()
    assert scheduled == []
    assert "auto_tries" not in saved["state"][str(OWNER)]["sessions"]["s1"][
        "loop_pending"], "沒有排定卻消耗了斷路器的額度"
    held = _KEY in b._dorossi_resume_inflight or _KEY in b._dorossi_loops
    assert held, "把別人的登記拿掉了"


def test_a_failed_count_releases_the_claim(monkeypatch):
    """計數寫不進去就不排定；那一格登記也要拿掉，否則這個 session 在這個行程裡再也不會
    被自動接回去（登記只有排定出去的那個迴圈結束時才會解除）。"""
    scheduled, _ = _install(monkeypatch, _store(_marker()))

    async def _broken_rmw(_mutate):
        raise OSError("disk full")

    monkeypatch.setattr(b, "_dorossi_state_rmw", _broken_rmw)
    _run_scan()
    assert scheduled == []
    assert _KEY not in b._dorossi_resume_inflight


def test_a_loop_already_running_is_not_resumed_twice(monkeypatch):
    scheduled, _ = _install(monkeypatch, _store(_marker()))
    monkeypatch.setattr(b, "_dorossi_loops", {(str(OWNER), "s1"): object()})
    _run_scan()
    assert not scheduled


def test_the_parallel_cap_keeps_the_freshest_markers(monkeypatch):
    now = time.time()
    slots = {f"s{i}": _marker(ts=now - i * 100) for i in range(1, 5)}
    state = {str(OWNER): {"active": "s1", "sessions": slots}}
    scheduled, _ = _install(monkeypatch, state)
    monkeypatch.setattr(b, "DOROSSI_MAX_PARALLEL_LOOPS", 2)
    _run_scan()
    assert scheduled == ["dorossi-autoresume-s1", "dorossi-autoresume-s2"]


def test_a_broken_store_does_not_take_down_on_ready(monkeypatch):
    """這個掃描是 `on_ready` 的一環；在這裡拋例外會連帶影響 presence 與背景任務。"""
    for junk in ({str(OWNER): "not-a-record"},
                 {str(OWNER): {"sessions": "nope"}},
                 {str(OWNER): {"sessions": {"s1": None}}},
                 {}):
        scheduled, _ = _install(monkeypatch, junk)
        _run_scan()
        assert not scheduled, f"{junk!r}"


# ---------------------------------------------------------------------------
# 三之二、清單上的措辭要跟實際行為一致
# ---------------------------------------------------------------------------

def _render(slot: dict) -> str:
    state = {str(OWNER): {"active": "s1", "sessions": {"s1": slot}}}
    return b._dorossi_render_session_list(state, str(OWNER))


def test_a_killed_task_is_advertised_as_self_resuming():
    """會自己接回去的標記，就不能再叫擁有者去按 `session continue`——他照做只會
    得到「已在進行中」。措辭錯了不會有任何測試變紅，但會讓人以為功能沒生效。"""
    out = _render(_marker())
    assert "自動接續" in out, out
    assert "session continue" not in out, out


def test_a_task_the_owner_aborted_still_points_at_manual_resume():
    out = _render(_marker(live=False))
    assert "session continue" in out, out
    assert "自動接續" not in out, out


def test_a_legacy_marker_without_an_anchor_points_at_manual_resume():
    """舊標記沒有錨點 → 只能人工接。清單必須照實說。"""
    marker = _marker()
    marker["loop_pending"].pop("channel_id")
    out = _render(marker)
    assert "session continue" in out, out


def test_the_list_uses_the_same_predicate_as_the_startup_scan():
    """兩邊各寫一份條件，遲早會各說各話（清單說會自動接、開機掃描卻不接）。"""
    fn = _find_func(_tree(), "_dorossi_render_session_list")
    assert _calls_named(fn, "_dorossi_loop_autoresume_plan"), (
        "清單自己判斷了『會不會自動接續』，而不是問開機掃描用的那支函式")


# ---------------------------------------------------------------------------
# 四、形狀守門（AST）——擋的是「以後有人搬動這幾行」
# ---------------------------------------------------------------------------

def _tree() -> ast.Module:
    return ast.parse((PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8"))


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"找不到 {name}")


def _dotted(node) -> str:
    bits = []
    while isinstance(node, ast.Attribute):
        bits.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        bits.append(node.id)
    return ".".join(reversed(bits))


def _calls_named(node, name):
    return [c for c in ast.walk(node)
            if isinstance(c, ast.Call) and _dotted(c.func) == name]


def _rmw_mutators(fn):
    """交給 `_dorossi_state_rmw` 執行的那些 mutator 的 AST 節點。

    只問「名字有沒有出現在函式裡」擋不住「那個值有沒有被用到」——把
    `await _dorossi_state_rmw(_mut)` 整行刪掉，巢狀的 `_mut` 仍然定義著，掃名字
    照樣掃得到。這個 helper 走機制：mutator 必須真的被當成引數交出去。"""
    handed = {a.id for c in _calls_named(fn, "_dorossi_state_rmw")
              for a in c.args if isinstance(a, ast.Name)}
    return [n for n in ast.walk(fn)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name in handed]


def _enclosing_finally_ids(fn) -> set:
    """`fn` 裡所有位於某個 `try/finally` 的 finalbody 中的節點 id。"""
    ids = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            for stmt in node.finalbody:
                ids |= {id(n) for n in ast.walk(stmt)}
    return ids


def test_the_loop_marks_itself_stopped_from_the_finally_block():
    """整個功能的判準就靠這一件事：`finally` 在自願結束時一定跑、在行程被砍時
    一定不跑。若有人把它搬進 try 本體（例如「提早存檔比較安全」），被砍死的迴圈
    就會留下 `live=False`，自動接續從此永遠不觸發——而且不會有任何測試變紅，
    因為所有自願結束的行為都沒變。"""
    # 一次 parse：底下要比對節點**身分**（不是名字），兩棵樹的 id 永遠對不起來。
    # 比名字不夠——在 finally 外面再定義一個同名的 mutator，名字集合照樣相符。
    loop = _find_func(_tree(), "_dorossi_run_loop")
    stoppers = [n for n in _rmw_mutators(loop)
                if _calls_named(n, "_dorossi_mark_loop_stopped")]
    assert stoppers, (
        "沒有把 `_dorossi_mark_loop_stopped` 的 mutator 交給 _dorossi_state_rmw")
    in_finally = _enclosing_finally_ids(loop)
    stray = [n for n in stoppers if id(n) not in in_finally]
    assert not stray, (
        f"第 {[n.lineno for n in stray]} 行的 mutator 不在 finally 裡——行程被砍死"
        "時就分不出『自己停的』與『被砍的』，abort 過的任務會在重啟後自己活過來")


def test_a_completed_round_resets_the_crash_loop_counter():
    """`reset_tries=True` 若被拿掉，一個健康的長任務只要經歷 5 次重啟就再也不會
    被自動接續——而那正是這個功能存在的情境。"""
    loop = _find_func(_tree(), "_dorossi_run_loop")
    resets = [c for n in _rmw_mutators(loop)
              for c in _calls_named(n, "_dorossi_touch_loop_pending")
              if any(kw.arg == "reset_tries"
                     and getattr(kw.value, "value", None) is True
                     for kw in c.keywords)]
    assert resets, "沒有任何一條路會在跑完一輪時把 auto_tries 歸零"


def test_the_scan_verifies_the_fetched_author_not_the_stored_one():
    """權限比對必須發生在向平台查回紀錄**之後**，而且比對的是 `OWNER_USER_ID`。

    2026-09-19 起查回紀錄的是 `_resolve_trigger_message`（斜線指令存的是 interaction
    id，要從 bot 的回覆反查），所以這裡先確認那支真的會 `fetch_message` 並翻歷史，
    再確認掃描是在叫過它之後才比對擁有者。"""
    tree = _tree()
    resolver = _find_func(tree, "_resolve_trigger_message")
    assert _calls_named(resolver, "channel.fetch_message"), (
        "查回紀錄的那支不再抓訊息——那就只剩磁碟上的 id 可信")
    assert _calls_named(resolver, "channel.history"), (
        "查回紀錄的那支不再翻歷史——斜線起的任務又會全部 404")
    scan = _find_func(tree, "_dorossi_autoresume_pending_loops")
    fetches = _calls_named(scan, "_resolve_trigger_message")
    assert fetches, "掃描沒有向平台查回錨點——那就只剩磁碟上的 id 可信"
    owner_cmps = [n for n in ast.walk(scan) if isinstance(n, ast.Compare)
                  and any(isinstance(x, ast.Name) and x.id == "OWNER_USER_ID"
                          for x in [n.left, *n.comparators])]
    assert owner_cmps, "沒有拿 OWNER_USER_ID 重驗作者"
    assert min(c.lineno for c in fetches) < min(c.lineno for c in owner_cmps)


def test_the_counter_lands_before_the_loop_is_scheduled():
    scan = _find_func(_tree(), "_dorossi_autoresume_pending_loops")
    counts = _calls_named(scan, "_dorossi_state_rmw")
    spawns = _calls_named(scan, "_schedule_coro")
    assert counts and spawns
    assert min(c.lineno for c in counts) < min(s.lineno for s in spawns), (
        "計數在起跑之後才寫——接續若當場弄死 bot，計數永遠寫不進去，"
        "重啟→接續→死掉 會是無限迴圈")


@pytest.mark.parametrize("handler", ["on_ready", "on_resumed"])
def test_every_reconnect_kicks_off_the_scan(handler):
    """`on_resumed` 也要掃：斷線期間因斷網停下來的任務，要在連線回來的那一刻接回去。"""
    assert _calls_named(_find_func(_tree(), handler), "_dorossi_resume_scan"), (
        f"{handler} 沒有叫這個掃描——功能寫好了但永遠不會執行")
    assert _calls_named(_find_func(_tree(), "_dorossi_resume_scan"),
                        "_dorossi_autoresume_pending_loops")


# ---------------------------------------------------------------------------
# 「被中斷、而且已經接不回來」的任務要被看見（2026-09-05）
#
# `_dorossi_loop_autoresume_plan` 有四道條件，任何一道不過就回 None。其中三種
# （太舊／重試用完／少了錨點）發生時，標記仍然停在 `live: True`，但**沒有任何東西
# 會再去接它**，也沒有任何地方會講。對無人值守的長任務來說，那等於「它其實早就
# 停了，而你以為還在跑」——這正是最難自己發現的狀態。
# ---------------------------------------------------------------------------


def _live(now, **over):
    m = {"live": True, "ts": now - 10, "channel_id": 1, "message_id": 2}
    m.update(over)
    return {"loop_pending": m}


@pytest.mark.parametrize("label,over", [
    ("太舊", {"ts": -999999}),                 # ts 由下面換算成絕對時間
    ("重試次數用完", {"auto_tries": 999}),
    ("少了頻道錨點", {"channel_id": None}),
])
def test_every_unresumable_reason_is_reported(label, over):
    now = time.time()
    if "ts" in over:
        over = dict(over, ts=now + over["ts"])
    state = {"u1": {"sessions": {"s1": _live(now, **over)}}}
    got = db.dorossi_abandoned_loops(state, now=now)
    assert [row[1] for row in got] == ["s1"], f"{label} 沒有被回報"


def test_a_resumable_loop_is_not_reported():
    """還接得回來的不算異常——會亂叫的診斷最後會被人忽略。"""
    now = time.time()
    state = {"u1": {"sessions": {"s1": _live(now)}}}
    assert db.dorossi_abandoned_loops(state, now=now) == []


def test_a_voluntarily_stopped_loop_is_not_reported():
    """`live` 為假 ＝ 迴圈的 `finally` 有跑到 ＝ 它是自己停的，不是被中斷的。
    把這種也報出來的話，每一個正常結束的任務都會變成一則警告。"""
    now = time.time()
    state = {"u1": {"sessions": {"s1": {"loop_pending":
                                        {"live": False, "ts": now - 10}}}}}
    assert db.dorossi_abandoned_loops(state, now=now) == []


@pytest.mark.parametrize("state", [
    {}, None, "not a dict", {"u1": None}, {"u1": {"sessions": None}},
    {"u1": {"sessions": {"s1": None}}}, {"u1": {"sessions": {"s1": {}}}},
    {"u1": {"sessions": {"s1": {"loop_pending": "junk"}}}},
])
def test_the_scan_never_raises_on_a_malformed_store(state):
    """狀態檔是跨行程寫的，半寫入／舊格式都可能出現。診斷不該把 doctor 弄掛。"""
    assert db.dorossi_abandoned_loops(state) == []


def test_the_worst_offender_comes_first():
    now = time.time()
    state = {"u1": {"sessions": {
        "s1": _live(now, ts=now - 100000, auto_tries=999),
        "s2": _live(now, ts=now - 500000, auto_tries=999),
    }}}
    assert [row[1] for row in db.dorossi_abandoned_loops(state, now=now)] == \
        ["s2", "s1"]


def test_the_doctor_reports_abandoned_loops():
    """偵測寫好卻沒人顯示，就只是一段沒人跑的程式碼。"""
    tree = ast.parse((PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8"))
    doctor = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "cmd_doctor")
    called = {c.func.id for c in ast.walk(doctor)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "dorossi_abandoned_loops" in called, (
        "`/sys doctor` 沒有回報「被中斷且接不回來」的自走任務。")


def test_the_report_leaks_no_path_or_id():
    """Layer 1：對外訊息只講數量與時間，不得帶使用者 id、session 內容或路徑。"""
    tree = ast.parse((PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8"))
    doctor = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "cmd_doctor")
    for node in ast.walk(doctor):
        if (isinstance(node, ast.JoinedStr)
                and "被中斷的自走任務" in ast.unparse(node)):
            rendered = ast.unparse(node)
            for banned in ("uid", "row[0]", ".md", "PROJECT_ROOT"):
                assert banned not in rendered, (
                    f"警告字串帶了 {banned!r}：{rendered[:120]}")


# ---------------------------------------------------------------------------
# 拒絕接續時要出聲（2026-09-06）
#
# 那天實際發生的事：自走迴圈撞到用量上限、排了一場 12,251 秒（三小時二十四分）的
# 等待；等待開始才**五秒**，一次排定的 bot 重啟就把行程砍了。重啟後 autoresume
# 確實跑了、也確實找到 `loop_pending`，但錨點訊息 404（`Unknown Message`），
# 於是 fail-closed 放棄——**唯一的訊號是一行 stderr**：
#
#   [09-06 02:37:09] [dorossi] autoresume s6: anchor unavailable
#                    (NotFound('404 Not Found (error code: 10008): Unknown Message'))
#
# 那行躺在 `discord_bot.log` 裡沒人看，任務就停了十七個小時，直到擁有者自己來問。
#
# **拒絕本身是對的**（權限要用抓回來的訊息重驗，不能信磁碟上的 uid）。錯的是安靜：
# 一個 fail-closed 的決定如果沒人知道它發生過，跟當機沒有兩樣。
# ---------------------------------------------------------------------------

class _NoticeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return None


def test_a_declined_autoresume_is_reported_to_the_owner(monkeypatch):
    ch = _NoticeChannel()
    monkeypatch.setattr(b.client, "get_channel", lambda cid: ch)
    asyncio.run(b._dorossi_report_autoresume_declined(["s6"]))
    assert len(ch.sent) == 1, "拒絕接續時什麼都沒說"
    text = ch.sent[0][0]
    assert "s6" in text
    assert "continue" in text, "沒有告訴擁有者怎麼手動接回去"


def test_nothing_is_said_when_nothing_was_declined(monkeypatch):
    """沒有被拒的就不要吵——每次重啟都發一則沒事的訊息，下場是沒人再看它。"""
    ch = _NoticeChannel()
    monkeypatch.setattr(b.client, "get_channel", lambda cid: ch)
    asyncio.run(b._dorossi_report_autoresume_declined([]))
    assert ch.sent == []


def test_the_notice_goes_to_the_configured_channel_not_the_stored_one():
    """刻意用設定檔的 `CHANNEL_ID`，不是標記裡那個 `channel_id`。

    這支函式的前提就是「磁碟上的東西不能當依據」——用磁碟指定的頻道發話，等於讓
    一個被改過的檔案決定 bot 往哪裡講話。所以在原始碼上釘死：只能讀 `CHANNEL_ID`。
    """
    import ast
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "_dorossi_report_autoresume_declined"), None)
    assert fn is not None, "函式改名了——這支守門要跟著改"
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "CHANNEL_ID" in names, "沒有用設定檔的頻道"
    assert "cid" not in names and "channel_id" not in names, (
        "用到了標記裡的頻道 id——那是磁碟來的，不能拿來決定往哪發話")


def test_the_notice_never_leaks_a_path_or_raw_error(monkeypatch):
    """Layer 1：通知本身不得帶主機路徑、原始例外或服務名。"""
    ch = _NoticeChannel()
    monkeypatch.setattr(b.client, "get_channel", lambda cid: ch)
    asyncio.run(b._dorossi_report_autoresume_declined(["s6", "s7"]))
    text = ch.sent[0][0]
    for banned in ("C:\\", "D:\\", "Traceback", "NotFound", ".json", ".log"):
        assert banned not in text, f"通知帶了 {banned!r}：{text!r}"


def test_the_notice_survives_a_missing_channel(monkeypatch):
    """`on_ready` 裡跑的東西不得往外拋——拿不到頻道就安靜記一筆。"""
    monkeypatch.setattr(b.client, "get_channel", lambda cid: None)
    asyncio.run(b._dorossi_report_autoresume_declined(["s6"]))


def test_the_notice_survives_a_channel_that_raises(monkeypatch):
    class _Boom:
        async def send(self, *a, **k):
            raise RuntimeError("channel gone")

    monkeypatch.setattr(b.client, "get_channel", lambda cid: _Boom())
    asyncio.run(b._dorossi_report_autoresume_declined(["s6"]))


def test_every_decline_path_records_the_session():
    """兩條拒絕路徑（錨點抓不回來／作者不是擁有者）都必須記進 `declined`。

    漏掉其中一條就等於那一類拒絕仍然是靜默的——而 2026-09-06 那次正好是第一條。
    """
    import ast
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "_dorossi_autoresume_pending_loops"), None)
    assert fn is not None
    appends = [n for n in ast.walk(fn)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "append"
               and isinstance(n.func.value, ast.Name)
               and n.func.value.id == "declined"]
    assert len(appends) >= 2, (
        f"只有 {len(appends)} 條拒絕路徑會記錄下來，預期至少兩條"
        "（錨點抓不回來、作者不是擁有者）")
    called = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "_dorossi_report_autoresume_declined"]
    assert called, "掃描結束後沒有把拒絕清單報出去"


# ---------------------------------------------------------------------------
# 五、斜線指令起的任務：錨點是 interaction id（2026-09-19）
#
# `_InteractionMessageProxy.id` 是 interaction id，不是訊息 id，所以
# `fetch_message(那個 id)` 必定 404。擁有者幾乎只用斜線驅動 Dorossi，於是跨重啟
# 自動接續**從來沒有成功過一次**（`discord_bot.log` 4 次全是
# `anchor unavailable … 10008 Unknown Message`）。平台上留下的紀錄是 bot 自己那則
# 回覆：`interaction_metadata.id` 就是那個 interaction id、`.user` 是按下指令的人。
#
# 這一族測試的替身都跑真正的 `_resolve_trigger_message`，只換掉平台那一端。
# 每一個「拒絕」案例都**只踩一道條件**——同時踩兩道的輸入會讓刪掉其中一道的變異
# 照樣全綠。
# ---------------------------------------------------------------------------

BOT = 999_000_111
SLASH_ID = 600000000000000003       # 一個真實錨點的形狀（18 位數雪花 ID）


def _not_found():
    return discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"),
                            {"code": 10008, "message": "Unknown Message"})


class _Reply:
    """一則頻道訊息的替身；`reply` 會記下來，讓測試看得到回覆掛在誰底下。"""

    def __init__(self, *, author, meta=None, channel=None):
        self.author = types.SimpleNamespace(id=author)
        self.interaction_metadata = meta
        self.channel = channel
        self.guild = None
        self.id = 4242
        self.replies: list = []

    async def reply(self, content=None, **kwargs):
        self.replies.append((content, kwargs))
        return self


def _meta(mid=SLASH_ID, invoker=OWNER,
          kind=discord.InteractionType.application_command):
    return types.SimpleNamespace(id=mid, type=kind,
                                 user=types.SimpleNamespace(id=invoker))


class _PlatformChannel:
    """`fetch_message` 與 `history` 都照真的平台行為走的頻道替身。

    `fetched` 是 None 時 `fetch_message` 丟真的 `discord.NotFound`（斜線錨點就是
    這樣）；`history_error` 會在**迭代途中**丟，那才是真的 HTTP 錯誤會出現的地方。
    """

    def __init__(self, *, fetched=None, fetch_error=None, history=(),
                 history_error=None, cid=111):
        self.id = cid
        self._fetched, self._fetch_error = fetched, fetch_error
        self._history, self._history_error = list(history), history_error
        self.history_calls: list = []
        self.sent: list = []

    async def fetch_message(self, mid):
        if self._fetch_error is not None:
            raise self._fetch_error
        if self._fetched is None:
            raise _not_found()
        return self._fetched

    def history(self, *, limit, around):
        self.history_calls.append((limit, getattr(around, "id", None)))
        items, boom = self._history, self._history_error

        async def _gen():
            for item in items:
                yield item
            if boom is not None:
                raise boom
        return _gen()

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs.get("reference")))


def _with_bot_identity(monkeypatch, channel):
    monkeypatch.setattr(b, "client", types.SimpleNamespace(
        user=types.SimpleNamespace(id=BOT),
        get_channel=lambda cid: channel))


def _resolve(channel, stored=SLASH_ID):
    return asyncio.run(asyncio.wait_for(
        b._resolve_trigger_message(channel, stored, label="test"), 10))


def test_a_slash_anchor_is_found_through_the_bots_own_reply(monkeypatch):
    """404 → 翻 `history(around=那個 id)` → 認得出 bot 自己回覆那一次互動的那一則。

    歷史裡刻意放了三個近似的干擾項在前面；挑錯任何一個都會讓回傳的訊息不是那一則。"""
    right = _Reply(author=BOT, meta=_meta())
    channel = _PlatformChannel(history=[
        _Reply(author=OWNER),                                  # 擁有者自己的閒聊
        _Reply(author=BOT, meta=None),                         # bot 的一般訊息
        _Reply(author=BOT, meta=_meta(mid=SLASH_ID + 1)),      # 別的互動
        right,
    ])
    _with_bot_identity(monkeypatch, channel)
    found = _resolve(channel)
    assert found is not None
    message, invoker, via_interaction = found
    assert message is right and via_interaction is True
    assert invoker.id == OWNER, "發起人要取平台說的 interaction_metadata.user"
    assert channel.history_calls == [(b._TRIGGER_LOOKUP_LIMIT, SLASH_ID)]


def test_an_at_bot_anchor_is_used_as_is_and_history_is_not_touched(monkeypatch):
    """`@bot` 走的是真的使用者訊息：抓得到就用它、作者就是發起人，不翻歷史。"""
    own = _Reply(author=OWNER)
    channel = _PlatformChannel(fetched=own)
    _with_bot_identity(monkeypatch, channel)
    assert _resolve(channel, 222) == (own, own.author, False)
    assert channel.history_calls == []


@pytest.mark.parametrize("label,history", [
    ("bot 的訊息但沒有互動紀錄", [_Reply(author=BOT, meta=None)]),
    ("bot 回覆的是另一次互動", [_Reply(author=BOT, meta=_meta(mid=SLASH_ID + 1))]),
    ("形狀對、作者不是 bot", [_Reply(author=OWNER, meta=_meta())]),
    ("互動種類不是應用程式指令",
     [_Reply(author=BOT, meta=_meta(kind=discord.InteractionType.component))]),
    ("發起人沒有 id", [_Reply(author=BOT, meta=types.SimpleNamespace(
        id=SLASH_ID, type=discord.InteractionType.application_command, user=None))]),
    ("歷史是空的", []),
])
def test_anything_but_the_bots_own_reply_to_that_interaction_is_refused(
        monkeypatch, label, history):
    """每一格只踩一道條件。第三格最要緊：一個不是 bot 的帳號就算做出一模一樣的
    `interaction_metadata` 形狀（平台上做不到，但替身做得到），也不能被當成紀錄。"""
    channel = _PlatformChannel(history=history)
    _with_bot_identity(monkeypatch, channel)
    assert _resolve(channel) is None, label


def test_a_failing_platform_is_refused_without_crashing(monkeypatch, capsys):
    """三種查不成：翻歷史途中出錯、抓訊息不是 404 的錯、bot 自己的 id 不知道。
    每一種都是 None、而且 stderr 講得出是哪一種；非 404 的錯不該再去翻歷史。"""
    boom = _PlatformChannel(history=[], history_error=RuntimeError("503"))
    _with_bot_identity(monkeypatch, boom)
    assert _resolve(boom) is None
    assert "history lookup failed" in capsys.readouterr().err

    forbidden = _PlatformChannel(fetch_error=RuntimeError("403 Forbidden"),
                                 history=[_Reply(author=BOT, meta=_meta())])
    _with_bot_identity(monkeypatch, forbidden)
    assert _resolve(forbidden) is None
    assert forbidden.history_calls == [], "非 404 的錯不是『沒有這則訊息』"
    assert "message fetch failed" in capsys.readouterr().err

    # 不知道 bot 自己是誰時，根本不該去翻歷史——拿 None 去比作者，碰巧會把每一則
    # 都當成「不是 bot」而回 None，結果一樣但理由錯了，還白打一次 API。所以這一格
    # 釘的是「沒翻、而且講得出原因」，不只是回 None。
    anonymous = _PlatformChannel(history=[_Reply(author=BOT, meta=_meta())])
    monkeypatch.setattr(b, "client", types.SimpleNamespace(user=None))
    assert _resolve(anonymous) is None
    assert anonymous.history_calls == []
    assert "bot's own id is unknown" in capsys.readouterr().err

    missing = _PlatformChannel(history=[])
    _with_bot_identity(monkeypatch, missing)
    assert _resolve(missing) is None
    assert "no interaction reply" in capsys.readouterr().err


def _slash_scan(monkeypatch, history, *, uid=str(OWNER)):
    """用真的掃描跑一次；把排進來的 `_dorossi_resume_loop` 參數攔下來。"""
    channel = _PlatformChannel(history=history)
    for item in history:
        item.channel = channel          # 真的訊息一定帶著它所在的頻道
    scheduled, saved = _install(monkeypatch, _store(_marker(message_id=SLASH_ID),
                                                    uid=uid))
    _with_bot_identity(monkeypatch, channel)
    calls: list = []

    def _fake_resume(message, sid, **kwargs):
        calls.append((message, sid, kwargs))

        async def _noop():
            return None
        return _noop()

    monkeypatch.setattr(b, "_dorossi_resume_loop", _fake_resume)
    _run_scan()
    return scheduled, saved, calls, channel


def test_a_slash_started_loop_is_resumed_as_the_owner_not_as_the_bot(monkeypatch):
    """斜線起的任務要接得回來，而且接回去的 `message` 代表的是擁有者、不是 bot。

    找回來的錨點是 bot 自己的回覆；直接傳下去，每一道閘都會看到 bot。替身的作者
    是平台說的發起人、id 是**原本的** interaction id（接續後會再記回標記，下次
    重啟照樣找得回來），回覆掛在那則 bot 回覆底下。最後把真的閘門跑一次：替身
    過得了、原始錨點過不了——後者證明替身不是多餘的。"""
    right = _Reply(author=BOT, meta=_meta())
    scheduled, saved, calls, channel = _slash_scan(monkeypatch, [right])
    assert scheduled == ["dorossi-autoresume-s1"], "斜線起的任務沒被接回去"
    assert saved["state"][str(OWNER)]["sessions"]["s1"]["loop_pending"][
        "auto_tries"] == 1
    trigger = calls[0][0]
    assert trigger.author.id == OWNER, "接回去的迴圈以 bot 為發起人"
    assert trigger.id == SLASH_ID, "id 要沿用原本那個，不然下次重啟又找不回來"
    assert trigger.channel is channel

    asyncio.run(b.safe_reply(trigger, "hi"))
    assert channel.sent == [("hi", right)], "回覆沒有掛在原本那則 bot 回覆底下"

    monkeypatch.setattr(b, "DOROSSI_CC_TOOLS", "full")
    assert b._dorossi_loop_gate_open(trigger, "claude_code")
    assert b._owner_unrestricted(trigger) and b._paths_visible_here(trigger)
    assert not b._dorossi_loop_gate_open(right, "claude_code"), (
        "原始錨點（bot 的回覆）竟然過得了閘——那替身就沒有存在的理由了")


def test_a_slash_anchor_invoked_by_someone_else_is_declined(monkeypatch):
    """平台說按下那個指令的不是擁有者 → 不接，也不消耗斷路器額度。"""
    scheduled, saved, calls, _ = _slash_scan(
        monkeypatch, [_Reply(author=BOT, meta=_meta(invoker=4242))])
    assert not scheduled and not calls
    assert "auto_tries" not in saved["state"][str(OWNER)]["sessions"]["s1"][
        "loop_pending"]


def test_a_non_owner_whose_own_slot_matches_is_still_declined(monkeypatch):
    """發起人與 slot 的 uid **一致**、但那個人不是擁有者。

    上一支（發起人 4242、slot 掛在擁有者底下）單靠「uid 對不上」就會被擋掉，所以
    把「必須是擁有者」那一半整段刪掉照樣全綠——兩道防護互相遮蔽。這一格只踩
    擁有者那一道。"""
    scheduled, _, calls, _ = _slash_scan(
        monkeypatch, [_Reply(author=BOT, meta=_meta(invoker=4242))], uid="4242")
    assert not scheduled and not calls


def test_a_slash_anchor_filed_under_another_uid_is_declined(monkeypatch):
    """發起人是擁有者，但標記掛在別的 uid 底下——接了就會接到別人的 session。"""
    scheduled, _, calls, _ = _slash_scan(
        monkeypatch, [_Reply(author=BOT, meta=_meta())], uid="7")
    assert not scheduled and not calls


def test_an_at_bot_anchor_still_resumes_with_the_real_message(monkeypatch):
    """`@bot` 那一條不變：傳下去的就是抓回來的那則使用者訊息本身，不是替身。"""
    own = _Reply(author=OWNER)
    channel = _PlatformChannel(fetched=own)
    scheduled, _ = _install(monkeypatch, _store(_marker()))
    _with_bot_identity(monkeypatch, channel)
    calls: list = []

    def _fake_resume(message, sid, **kwargs):
        calls.append(message)

        async def _noop():
            return None
        return _noop()

    monkeypatch.setattr(b, "_dorossi_resume_loop", _fake_resume)
    _run_scan()
    assert scheduled and calls == [own]


# ---------------------------------------------------------------------------
# 六、撞到方案用量上限：存檔、收掉迴圈、到點接回來（2026-09-26）
# ---------------------------------------------------------------------------
# 原地等待那一版（2026-08-31～09-26）一次睡五、六個小時，整段握著對話的鎖、並行名額與
# 電源要求。現在迴圈把接續點寫進標記（stop＝`usage_wait`、`resume_at`）就結束，到點由
# 同一套自動接續接回來——這一段釘住那套機制多出來的規則。

def _usage_wait(resume_at, **over) -> dict:
    slot = _marker(**over)
    db._dorossi_mark_loop_usage_wait(slot, resume_at=resume_at, usage_waits=2,
                                     resume_prompt="重跑這一輪", injections=["補充"],
                                     after=["提交"])
    return slot


def test_marking_a_usage_wait_keeps_what_the_resume_needs_and_clears_the_breaker():
    slot = _marker(auto_tries=3)
    db._dorossi_mark_loop_usage_wait(slot, resume_at=123.0, usage_waits=2,
                                     resume_prompt="p", injections=["a", 5, ""],
                                     after=["c"])
    m = slot["loop_pending"]
    assert (m["live"], m["stop"], m["resume_at"], m["usage_waits"]) == (
        False, "usage_wait", 123.0, 2)
    assert (m["resume_prompt"], m["resume_inject"], m["resume_after"]) == ("p", ["a"], ["c"])
    assert "auto_tries" not in m, (
        "迴圈是自己乾淨停下來的，證明接續沒有弄死 bot——斷路器要從頭算，否則幾次用量等待"
        "之後自動接續就永久放棄")
    empty: dict = {}
    db._dorossi_mark_loop_usage_wait(empty, resume_at=1.0, usage_waits=1,
                                     resume_prompt="p", injections=[])
    assert empty == {}, "不得建立標記"


def test_the_saved_resume_state_is_taken_once_and_shape_checked():
    slot = _usage_wait(time.time())
    assert db._dorossi_take_loop_resume(slot) == ("重跑這一輪", ["補充"], ["提交"], 2)
    assert db._dorossi_take_loop_resume(slot) == (None, [], [], 0), "只能用一次"
    slot["loop_pending"].update(resume_prompt=5, resume_inject="x", resume_after=[1, "ok"],
                                usage_waits=True)
    assert db._dorossi_take_loop_resume(slot) == (None, [], ["ok"], 0)
    assert db._dorossi_take_loop_resume({}) == (None, [], [], 0)


def test_a_usage_wait_is_resumed_only_once_its_time_has_come():
    now = time.time()
    slot = _usage_wait(now + 600)
    assert db._dorossi_loop_marker_wants_autoresume(slot["loop_pending"])
    assert db._dorossi_loop_autoresume_plan(slot, now=now) is None, "還沒到點就接了"
    assert db._dorossi_loop_autoresume_plan(slot, now=now + 601) == (111, 222, 0)


def test_the_age_of_a_usage_wait_counts_from_its_resume_time(monkeypatch):
    """一次等待可能五、六個小時。年齡若照舊從停下來時的心跳算，一個等得比窗口久的任務會在
    到點那一刻被判「太舊」，正好在最該接的時候不接。從到點那一刻算。"""
    monkeypatch.setattr(db, "DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC", 3600)
    now = time.time()
    slot = _usage_wait(now - 60, ts=now - 6 * 3600)
    assert db._dorossi_loop_autoresume_plan(slot, now=now) is not None
    stale = _usage_wait(now - 7200, ts=now - 6 * 3600)
    assert db._dorossi_loop_autoresume_plan(stale, now=now) is None, (
        "到點之後又過了比窗口還久（例如 bot 整段沒開）——那照舊算太舊，交給人工")


def test_switching_off_crash_resume_does_not_strand_a_usage_wait(monkeypatch):
    """`dorossi_loop_autoresume_max_age_sec = 0` 關掉的是「被砍死之後自動接」。原地等待那一版
    本來就不看這個設定，改成存檔等待不該讓用量等待多一個會把任務永遠留在原地的開關。"""
    monkeypatch.setattr(db, "DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC", 0)
    now = time.time()
    assert db._dorossi_loop_autoresume_plan(_marker(ts=now - 10), now=now) is None
    assert db._dorossi_loop_autoresume_plan(
        _usage_wait(now - 1, ts=now - 10), now=now) is not None


@pytest.mark.parametrize("at", ["later", float("nan"), None, True])
def test_a_broken_resume_time_counts_as_due(at):
    """手改壞的時刻當作已到點：把一個擁有者交代過的任務永遠擋住，比早探一次糟。"""
    slot = _usage_wait(time.time() + 600)
    slot["loop_pending"]["resume_at"] = at
    assert db._dorossi_loop_usage_wait_due(slot["loop_pending"])
    assert db._dorossi_loop_usage_resume_at(slot["loop_pending"]) is None


def test_a_paused_usage_stop_is_never_resumed_by_itself():
    slot = _marker()
    db._dorossi_mark_loop_usage_wait(slot, resume_at=0.0, usage_waits=1,
                                     resume_prompt="p", injections=[], paused=True)
    assert slot["loop_pending"]["stop"] == "paused"
    assert not db._dorossi_loop_marker_wants_autoresume(slot["loop_pending"])
    assert db._dorossi_loop_autoresume_plan(slot) is None


def test_yield_turns_a_pending_resume_into_a_manual_one_and_keeps_the_saved_round():
    slot = _usage_wait(time.time() - 1)
    db._dorossi_mark_loop_paused(slot)
    assert slot["loop_pending"]["stop"] == "paused"
    assert db._dorossi_loop_autoresume_plan(slot) is None
    assert slot["loop_pending"]["resume_prompt"] == "重跑這一輪"
    empty: dict = {}
    db._dorossi_mark_loop_paused(empty)
    assert empty == {}


def test_a_usage_wait_that_is_not_due_is_not_reported_as_abandoned():
    now = time.time()
    waiting = _usage_wait(now + 600)
    due_without_anchor = _usage_wait(now - 1, channel_id=None)
    state = {"7": {"sessions": {"s1": waiting, "s2": due_without_anchor}}}
    found = [sid for _uid, sid, _age in db.dorossi_abandoned_loops(state, now=now)]
    assert found == ["s2"], "還在等的任務被報成被遺棄；或到點了卻接不回來的沒被報"


def _resume_recorder(monkeypatch):
    calls: list = []

    async def _noop():
        return None

    def _fake_resume(message, sid, *, ack_override=None):
        calls.append((message, sid, ack_override))
        return _noop()

    monkeypatch.setattr(b, "_dorossi_resume_loop", _fake_resume)
    return calls


class _NoChannelClient:
    def get_channel(self, cid):
        raise AssertionError("手上有原本那則訊息，卻還去查平台")


def test_the_scan_resumes_a_due_usage_wait_with_the_message_it_kept(monkeypatch):
    """同一個行程裡到點：用迴圈停下時留下的那則訊息接回去，不查錨點（查不回來的任務——
    訊息被刪了、沒有斜線選單的平台——在原地等待那一版照樣接得回來）。接回去時講的是
    「用量已重設」，不是「bot 重啟或斷線」。"""
    slot = _usage_wait(time.time() - 1)
    _install(monkeypatch, _store(slot))
    calls = _resume_recorder(monkeypatch)
    kept = object()
    key = b._dorossi_session_key(str(OWNER), "s1")
    monkeypatch.setattr(b, "_dorossi_usage_resume_messages", {key: kept})
    monkeypatch.setattr(b, "client", _NoChannelClient())
    _run_scan()
    assert len(calls) == 1 and calls[0][0] is kept and calls[0][1] == "s1"
    assert "用量已重設" in calls[0][2], calls[0][2]
    assert key not in b._dorossi_usage_resume_messages


def test_the_scan_leaves_a_usage_wait_alone_until_it_is_due(monkeypatch):
    _install(monkeypatch, _store(_usage_wait(time.time() + 600)))
    calls = _resume_recorder(monkeypatch)
    key = b._dorossi_session_key(str(OWNER), "s1")
    monkeypatch.setattr(b, "_dorossi_usage_resume_messages", {key: object()})
    _run_scan()
    assert calls == [] and key in b._dorossi_usage_resume_messages


def test_after_a_restart_a_due_usage_wait_comes_back_through_its_anchor(monkeypatch):
    scheduled, saved = _install(monkeypatch, _store(_usage_wait(time.time() - 1)))
    monkeypatch.setattr(b, "_dorossi_usage_resume_messages", {})
    _run_scan()
    assert len(scheduled) == 1, "重啟之後到點的用量等待沒有被接回去"
    assert saved["state"][str(OWNER)]["sessions"]["s1"]["loop_pending"][
        "auto_tries"] == 1


class _HeldLock:
    def locked(self):
        return True


def test_the_scan_skips_a_session_that_is_busy_with_another_turn(monkeypatch):
    """同一次重設時到點的停放提問可能正在同一個對話上跑。接續那一條看到鎖被拿著只會回
    「忙線中」並放棄，還白白算進斷路器——先跳過，下一拍再接。"""
    scheduled, saved = _install(monkeypatch, _store(_usage_wait(time.time() - 1)))
    key = b._dorossi_session_key(str(OWNER), "s1")
    monkeypatch.setattr(b, "_dorossi_session_locks", {key: _HeldLock()})
    _run_scan()
    assert not scheduled
    assert "auto_tries" not in saved["state"][str(OWNER)]["sessions"]["s1"]["loop_pending"]


def _kick_env(monkeypatch, slot, *, held=True, cap=0, busy=()):
    key = b._dorossi_session_key(str(OWNER), "s1")
    kicked: list = []

    def _fake_schedule(coro, *, label=""):
        kicked.append(label)
        coro.close()

    monkeypatch.setattr(b, "_dorossi_load_state", lambda: _store(slot))
    monkeypatch.setattr(b, "_schedule_coro", _fake_schedule)
    monkeypatch.setattr(b, "_dorossi_loops", {k: object() for k in busy})
    monkeypatch.setattr(b, "_dorossi_resume_inflight", set())
    monkeypatch.setattr(b, "_dorossi_session_locks", {})
    monkeypatch.setattr(b, "_dorossi_autoresume_reported", set())
    monkeypatch.setattr(b, "DOROSSI_MAX_PARALLEL_LOOPS", cap)
    monkeypatch.setattr(b, "_dorossi_usage_resume_messages",
                        {key: object()} if held else {})
    return kicked, key


def test_the_watch_tick_kicks_the_scan_only_when_a_usage_wait_is_due(monkeypatch):
    now = time.time()
    kicked, _ = _kick_env(monkeypatch, _usage_wait(now + 600))
    assert b._dorossi_kick_due_usage_resumes(now=now) is False and kicked == []
    assert b._dorossi_kick_due_usage_resumes(now=now + 601) is True
    assert kicked == ["dorossi-usage-resume"]


def test_the_watch_tick_does_not_hammer_the_platform_for_a_declined_marker(monkeypatch):
    """重啟之後手上沒有原本的訊息、錨點又查不回來：掃描會報一次「接不回來」。之後每 30 秒
    再掃一次只是白打平台 API，所以已經報過的同一個標記不再觸發。"""
    slot = _usage_wait(time.time() - 1)
    kicked, _ = _kick_env(monkeypatch, slot, held=False)
    b._dorossi_autoresume_reported.add((str(OWNER), "s1", slot["loop_pending"]["ts"]))
    assert b._dorossi_kick_due_usage_resumes() is False and kicked == []


def test_the_watch_tick_waits_for_a_free_loop_slot(monkeypatch):
    kicked, _ = _kick_env(monkeypatch, _usage_wait(time.time() - 1), cap=1,
                          busy=[("9", "s9")])
    assert b._dorossi_kick_due_usage_resumes() is False and kicked == []


def test_the_watch_tick_drops_a_kept_message_nobody_will_use(monkeypatch):
    """任務被 abort、讓出、手動接回去或整個對話被刪之後，留著的觸發訊息要丟掉。"""
    slot = _usage_wait(time.time() - 1)
    db._dorossi_mark_loop_aborted(slot)
    _kicked, key = _kick_env(monkeypatch, slot)
    assert b._dorossi_kick_due_usage_resumes() is False
    assert key not in b._dorossi_usage_resume_messages


def test_the_parked_watch_loop_also_brings_back_usage_waits():
    tree = _tree()
    watch = _find_func(tree, "_dorossi_parked_watch_loop")
    assert _calls_named(watch, "_dorossi_kick_due_usage_resumes"), (
        "沒有任何東西會在到點時把存檔等待的自走任務叫回來")


class _YieldMsg:
    def __init__(self):
        self.author = _StubAuthor(OWNER)


def test_yield_also_holds_back_a_task_that_would_come_back_by_itself(monkeypatch):
    """讓出的用意是「另一位編輯者要改同一批檔案」。存檔等方案用量重設的任務此刻沒在改檔，
    但到點會自己接回來繼續改——讓出要把它改成暫停，並且講一聲。"""
    slot = _usage_wait(time.time() + 600)
    replies: list = []

    async def _reply(_message, content=None, **_kw):
        replies.append(content)

    _install(monkeypatch, _store(slot))
    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_dorossi_event", lambda *_a, **_k: None)
    asyncio.run(asyncio.wait_for(b.mcmd_yield(_YieldMsg(), ""), 5))
    assert slot["loop_pending"]["stop"] == "paused", "讓出沒有擋住會自己回來的任務"
    assert len(replies) == 1 and "`s1`" in replies[0] and "暫停" in replies[0], replies
