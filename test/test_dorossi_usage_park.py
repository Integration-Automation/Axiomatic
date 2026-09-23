"""單輪提問撞到方案用量上限：**停進佇列、時間到自動重跑**，而不是丟掉那一題。

自走迴圈早就會等額度回來再續跑（`test_dorossi_usage_limit.py`）；單輪那條路卻只回一句
「已達方案用量上限」就收工，擁有者得自己記得回來重問。兩條路的形狀不一致，而且不一致的
方向是壞的那一邊——問完就走的人，正是最不可能記得回來的人。

現在單輪走的是「跟斷網那條路同一個形狀」：把那一輪停進既有的排隊佇列
（`dorossi_queue.ndjson`），時間到了由既有的還原機制重跑——per-session 鎖、後端號誌、
`running`／`tries` 帳全部照舊，不另外做一套執行器。

這一份釘住四組決定，每一組都有它專屬的反例：

**一、排定時刻是牆上時鐘，不是行程的單調時鐘。** 迴圈那條路用 `time.monotonic()` 是對的
（等待期間 NTP 校時不該讓它變成幾秒或幾天），但那是因為它整段等待都在同一個行程裡。停放
列要活過三件事：bot 重啟、主機睡過重設時刻、中間斷網。這三件事裡 monotonic 都算不出
「還剩幾秒」——重啟後它從頭算，睡著時它可能根本不走。所以停放列存的是 `run_at`（epoch），
而且**還沒到點的列在還原時一律略過、留在磁碟上**。

**二、重跑時額度還沒回來就再停一次，次數不設限。** 這是擁有者對用量等待的裁定（不得有
上限）。每次重停都是**新的一列**（新 id、沒有 `tries`），所以用量等待吃不到
`_DOROSSI_QUEUE_MAX_TRIES` 那份「會把行程弄死的提問只准再試一次」的額度——那份額度防的是
毒列，跟額度用完完全是兩回事。停過幾次記在 `usage_parks`，只拿來算退避。

**三、到點時平台連不上，不准停進失敗佇列。** 失敗佇列不會自己重跑，而這一列是 bot 明確
答應過「時間到會自己跑」的。網路是通的時候查不回錨點才是真的有問題，那一種照舊。

**四、看得見、關得掉。** `/dorossi queue show`／`detail`／`/dorossi running` 要列出它與它的
排定時刻，`/dorossi queue clear` 與 `/dorossi abort` 要關得掉——一個「幾小時後會自己動」的
東西如果在每一個狀態指令裡都是隱形的，那跟當機沒兩樣。

測試不連網、不起子行程、不碰真的落地檔（佇列／失敗佇列／事件檔全部導到 `tmp_path`）。
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

import discord_bot as b  # noqa: E402
import dorossi_backend as db  # noqa: E402

UID = str(b.DOROSSI_USER_ID)
SID = "s1"
CHANNEL_ID = 55
MESSAGE_ID = 9
BOT_SOURCE = Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py"
_TREE = ast.parse(BOT_SOURCE.read_text(encoding="utf-8"))


def _func(name: str):
    """用 AST 取函式節點——不要用 `getsource()` 做子字串比對：docstring 常常就在解釋
    你要檢查的那條規則，於是把程式碼改壞測試照樣綠。"""
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"discord_bot.py 裡找不到 {name}")


def _body_src(name: str) -> str:
    """函式的原始碼，**去掉 docstring**。

    這個專案的 docstring 常常就在解釋「為什麼用 monotonic 而不是 time.time」，所以
    直接對 `ast.unparse` 的結果做子字串比對，量到的會是散文而不是程式碼——刪掉整段
    程式碼、留著那段話，測試照樣綠。"""
    node = _func(name)
    body = node.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class FakeAuthor:
    def __init__(self, uid: int) -> None:
        self.id = uid


class FakeMessage:
    def __init__(self, uid: int, channel=None, mid: int = MESSAGE_ID) -> None:
        self.author = FakeAuthor(uid)
        self.channel = channel or FakeChannel()
        self.guild = None
        self.mentions = []
        self.content = ""
        self.id = mid
        self.replies: list = []

    async def reply(self, content=None, **kwargs):
        del kwargs
        self.replies.append(content)
        return None


class FakeChannel:
    guild = None

    def __init__(self, cid: int = CHANNEL_ID, *, fetch_error=None) -> None:
        self.id = cid
        self.sent: list = []
        self._fetch_error = fetch_error

    async def send(self, content=None, **kwargs):
        del kwargs
        self.sent.append(content)
        return None

    async def fetch_message(self, mid):
        if self._fetch_error is not None:
            raise self._fetch_error
        return FakeMessage(b.DOROSSI_USER_ID, self, mid=mid)


class _NoHistoryChannel(FakeChannel):
    """抓不到那則訊息、翻歷史也找不到互動回覆——錨點整個查不回來的那一格。"""

    def history(self, **_kw):
        async def _gen():
            for item in ():
                yield item
        return _gen()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """把會落地與會改到模組全域的東西全部導開。

    這台機器上有一個跑了好幾天的正式批次，所以絕對不能讓測試碰到真的佇列檔。"""
    monkeypatch.setattr(b, "DOROSSI_QUEUE_FILE", tmp_path / "q.ndjson")
    monkeypatch.setattr(b, "DOROSSI_FAILED_QUEUE_FILE", tmp_path / "qf.ndjson")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "ev.ndjson")
    saved = (set(b._dorossi_queue_live), dict(b._dorossi_session_locks),
             dict(b._dorossi_session_lock_refs), dict(b._dorossi_loops),
             b._dorossi_queue_restored)
    b._dorossi_queue_live.clear()
    b._dorossi_session_locks.clear()
    b._dorossi_session_lock_refs.clear()
    b._dorossi_loops.clear()
    yield
    live, locks, refs, loops, restored = saved
    b._dorossi_queue_live.clear()
    b._dorossi_queue_live.update(live)
    b._dorossi_session_locks.clear()
    b._dorossi_session_locks.update(locks)
    b._dorossi_session_lock_refs.clear()
    b._dorossi_session_lock_refs.update(refs)
    b._dorossi_loops.clear()
    b._dorossi_loops.update(loops)
    b._dorossi_queue_restored = restored


def _shift_wall_clock(monkeypatch, seconds: float) -> None:
    """只動牆上時鐘，**不動** `time.monotonic`。

    兩個一起假造會讓 `asyncio` 的計時（它就是用 monotonic）跟著錯亂，測試會因為一個
    跟被測行為無關的理由變紅或掛住。這裡要量的正是「這兩個時鐘有沒有被混用」，所以
    只准動一個。"""
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + seconds)


def _park(prompt="停一下這題", *, parks=0, reset_at=None, message=None,
          effort=None, model_tier=None):
    """走正式的停放函式，回 `(run_at, 那一列)`。"""
    exc = db._DorossiUsageLimitError("limit", "3pm", reset_at=reset_at)
    msg = message or FakeMessage(b.DOROSSI_USER_ID)
    run_at = asyncio.run(b._dorossi_usage_park_turn(
        msg, UID, SID, prompt, exc, effort=effort, model_tier=model_tier,
        parks=parks))
    rows = b._dorossi_queue_read()
    return run_at, (rows[0] if rows else None)


def _parked_row(**over) -> dict:
    row = {"id": "p1", "uid": UID, "sid": SID, "channel_id": CHANNEL_ID,
           "message_id": MESSAGE_ID, "prompt": "停一下這題",
           "created_at": time.time(), "status": b._DOROSSI_PARKED_STATUS,
           "run_at": time.time() + 3600, "usage_parks": 1}
    row.update(over)
    return row


def _run_restore(monkeypatch, rows, *, channel=None, turn=None):
    """用正式的開機還原流程跑 `rows`；回 `(跑過的回合, 頻道訊息, channel)`。"""
    channel = channel or FakeChannel()
    ran: list = []

    async def _default_turn(message, prompt, placeholder, uid, sid, **kwargs):
        del message, placeholder
        ran.append((uid, sid, prompt, kwargs))

    monkeypatch.setattr(b, "_dorossi_process_turn", turn or _default_turn)
    monkeypatch.setattr(b, "client", types.SimpleNamespace(
        user=types.SimpleNamespace(id=999),
        get_channel=lambda cid: channel,
        fetch_channel=b.client.fetch_channel))

    async def _body():
        b._dorossi_queue_write(rows)
        b._dorossi_queue_restored = False
        b._dorossi_restore_queued_turns_once()
        for _ in range(60):
            await asyncio.sleep(0)

    asyncio.run(_body())
    return ran, channel.sent, channel


# ===========================================================================
# 一、停進去：排定時刻、錨點、退避
# ===========================================================================

def test_a_usage_limited_turn_lands_on_disk_with_everything_needed_to_re_run():
    """停一題要存四樣東西：問題、哪個對話、回去哪裡（錨點）、以及**什麼時候**可以再跑。

    少任何一樣都不是「延後」而是「丟掉」：少了 prompt 沒得跑，少了 sid 會跑進別的
    對話，少了錨點之後查不回發問者（還原會擋下來），少了時間就沒有人會去叫它。"""
    reset_at = time.time() + 1800
    run_at, row = _park("查一下這個函式", reset_at=reset_at,
                        effort="high", model_tier="opus")
    assert run_at is not None and row is not None
    assert row["prompt"] == "查一下這個函式"
    assert (row["uid"], row["sid"]) == (UID, SID)
    assert (row["channel_id"], row["message_id"]) == (CHANNEL_ID, MESSAGE_ID)
    assert (row["effort"], row["model_tier"]) == ("high", "opus")
    assert row["status"] == b._DOROSSI_PARKED_STATUS
    assert row["run_at"] == pytest.approx(run_at)
    # 睡到重設時刻再加緩衝——跟迴圈那條路同一支 `_dorossi_usage_wait_seconds`。
    assert row["run_at"] == pytest.approx(
        reset_at + db.DOROSSI_USAGE_WAIT_GRACE_SEC, abs=5)


def test_the_scheduled_time_is_a_wall_clock_instant():
    """`run_at` 必須落在牆上時鐘的刻度上，不是 `time.monotonic()` 的刻度。

    兩者在同一個行程裡看起來都「會過去」，所以寫錯**當下完全沒有症狀**；症狀只在重啟
    或主機睡過之後出現，而那正是這個功能存在的理由。這裡直接比對數量級：monotonic 在
    這台機器上是開機以來的秒數，跟 epoch 差了好幾個數量級。"""
    run_at, _row = _park(reset_at=time.time() + 600)
    assert run_at > time.time(), "排在過去，等於立刻重跑"
    assert abs(run_at - time.time()) < 86400, "排到一天以後，不像是用量重設"
    assert run_at - time.monotonic() > 1e8, (
        "`run_at` 看起來是 monotonic 的刻度——重啟之後就沒有意義了")


def test_a_park_with_no_machine_readable_reset_time_backs_off_as_it_repeats():
    """後端只給了「resets 3pm」這種沒有時區的鐘點時不猜，退回退避探測。

    退避的倍數要跟著**這一題已經停過幾次**走：不跟的話，額度真的還沒回來時就變成每
    `FALLBACK` 秒送一次註定失敗的提問，而每一次都會再貼一則訊息給擁有者。"""
    first, row = _park(parks=0)
    assert row["usage_parks"] == 1
    b._dorossi_queue_write([])
    later, row2 = _park(parks=3)
    assert row2["usage_parks"] == 4
    assert later - time.time() > first - time.time(), "停了四次還跟第一次等一樣久"
    assert later - time.time() == pytest.approx(
        db._dorossi_usage_wait_seconds(
            db._DorossiUsageLimitError("limit"), 4), abs=5)


def test_a_turn_with_no_usable_anchor_is_not_parked():
    """沒有錨點就不要停——之後查不回當初那一次請求，那一列只會躺進失敗佇列。

    這時候誠實地回一句「撞到上限」比假裝排好了好：後者會讓擁有者等一個永遠不會來的
    回覆。"""
    msg = FakeMessage(b.DOROSSI_USER_ID)
    msg.id = None
    run_at, row = _park(message=msg)
    assert run_at is None and row is None


def test_a_non_owner_turn_is_not_parked():
    """過不了 Dorossi 自己的閘就不要停（fail-closed）。

    還原時同一道閘會再擋一次，所以停了也只是讓那一列躺進失敗佇列——而且是以「有一題
    排好了」的樣子躺在那裡。"""
    run_at, row = _park(message=FakeMessage(b.DOROSSI_USER_ID + 1))
    assert run_at is None and row is None


def test_the_park_notice_says_when_and_leaks_nothing():
    """通知要講「什麼時候會自己跑」，而且不得帶出原始例外文字（Layer 1）。

    `reset_hint` 是本專案唯一准許組進回覆的後端原始文字（已經收斂過形狀），其餘一律
    不得出現——尤其是 `str(error)`，那是什麼都能夾帶進來的那一條路。"""
    exc = db._DorossiUsageLimitError(
        "Claude AI usage limit reached|1749924000", "3pm",
        reset_at=time.time() + 600)
    text = b._dorossi_usage_park_reply(exc, time.time() + 660)
    assert "會自動重跑" in text and "不必重問" in text
    assert "usage limit reached" not in text and "|1749924000" not in text
    assert "Claude" not in text and "claude" not in text
    assert "3pm" in text, "已經收斂過的重設提示可以講，不講反而少了資訊"


# ===========================================================================
# 二、活過重啟、活過睡眠
# ===========================================================================

def test_a_restart_does_not_run_a_parked_turn_before_its_time(monkeypatch):
    """重啟時看到還沒到點的停放列：留在磁碟上，**不跑**。

    跑了就等於重啟一次就白白撞一次上限、而且會多送一則訊息給擁有者——重啟得夠頻繁的
    話，那就是一個以重啟為節拍的無效重試迴圈。"""
    ran, sent, _ch = _run_restore(monkeypatch, [_parked_row()])
    assert ran == [], "還沒到重設時刻就被跑掉了"
    assert [r["id"] for r in b._dorossi_queue_read()] == ["p1"], "列被弄丟了"
    assert b._dorossi_failed_queue_read() == [], "還沒到點不是失敗"
    assert sent == []


def test_a_host_that_slept_through_the_reset_runs_it_on_the_next_start(
        monkeypatch):
    """主機睡過了重設時刻（或 bot 整段時間沒開）：下一次啟動當場就跑，而且只跑一次。

    這是 monotonic 算不出來的那一格——睡著時它可能根本不走，所以「還剩幾秒」只有牆上
    時鐘答得出來。"""
    ran, sent, _ch = _run_restore(
        monkeypatch, [_parked_row(run_at=time.time() - 7200)])
    assert [(x[0], x[2]) for x in ran] == [(UID, "停一下這題")], ran
    assert ran[0][3]["usage_parks"] == 1, "停過幾次沒有帶回去，退避會從頭算"
    assert b._dorossi_queue_read() == [], "跑完還留在磁碟上——下次重啟會再跑一次"
    assert any("重設" in str(x) for x in sent), (
        "重跑時沒出聲：擁有者只會看到一則憑空出現的『處理中』")


def test_only_the_wall_clock_decides_that_a_parked_turn_is_due(monkeypatch):
    """把牆上時鐘往前撥過 `run_at`（**不動** monotonic）→ 那一列就該被撿起來。

    這一支是「兩個時鐘不得混用」的正面控制：判斷到點的若是 monotonic，這裡的時鐘位移
    對它毫無影響，那一列就會永遠等下去。"""
    b._dorossi_queue_write([_parked_row(run_at=time.time() + 3600)])
    assert b._dorossi_restore_due_parked_rows() == 0
    _shift_wall_clock(monkeypatch, 3700)
    assert b._dorossi_restore_due_parked_rows() == 1


def test_the_park_helper_never_reaches_for_the_process_clock():
    """AST：停放函式裡不得出現 `monotonic`。

    行為測試示範的是「牆上時鐘有效」，但一個同時寫進兩種刻度的版本照樣會通過那一支
    （它只是多存了一個沒人看的欄位），而那個版本在重啟之後會挑錯邊。"""
    src = _body_src("_dorossi_usage_park_turn")
    assert "monotonic" not in src
    assert "time.time()" in src


def test_a_row_this_process_already_holds_is_not_picked_up_again():
    """手上的列（正在跑、或已經排定還原）不得再被掃描撿一次——那就是同一題跑兩遍。"""
    b._dorossi_queue_write([_parked_row(run_at=time.time() - 60)])
    b._dorossi_queue_live.add("p1")
    assert b._dorossi_restore_due_parked_rows() == 0


def test_the_watch_loop_picks_a_turn_up_while_the_bot_stays_up(monkeypatch):
    """bot 一直開著、時間到了——最平常的那一格，也要有人去叫它。

    重啟與醒來各自有一條路會掃，但兩者都不會在「什麼事都沒發生」時觸發。"""
    seen: list = []
    monkeypatch.setattr(b, "_DOROSSI_PARKED_TICK_SEC", 0.01)
    monkeypatch.setattr(b, "_dorossi_restore_rows",
                        lambda rows: seen.extend(r["id"] for r in rows))
    b._dorossi_queue_write([_parked_row(run_at=time.time() - 1)])

    async def _body():
        task = asyncio.ensure_future(b._dorossi_parked_watch_loop())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if seen:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_body())
    assert seen == ["p1"], "停放列到點了卻沒有人去叫它"


def test_the_watch_loop_survives_a_broken_scan(monkeypatch, capsys):
    """掃描炸了不得把整條迴圈帶走——它死了之後沒有任何症狀，只是東西悄悄停止運作。"""
    calls: list = []
    monkeypatch.setattr(b, "_DOROSSI_PARKED_TICK_SEC", 0.01)

    def _boom():
        calls.append(1)
        raise RuntimeError("掃描炸了")

    monkeypatch.setattr(b, "_dorossi_restore_due_parked_rows", _boom)

    async def _body():
        task = asyncio.ensure_future(b._dorossi_parked_watch_loop())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(calls) >= 2:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_body())
    assert len(calls) >= 2, "第一次炸掉就再也沒有下一次了"
    assert "掃描炸了" in capsys.readouterr().err


def test_the_watch_loop_is_started_and_supervised_like_the_others():
    """AST：這條迴圈要跟其他幾條一樣由 `_ensure_background_tasks_alive` 拉起來。

    只在 `on_ready` 起一次的話，它一旦死掉（或第一次建立就失敗）就再也不會回來，而
    症狀是「有一題永遠不跑」——沒有任何錯誤訊息。"""
    src = _body_src("_ensure_background_tasks_alive")
    assert "_dorossi_parked_watch_loop" in src
    assert "_parked_watch_task" in src


# ===========================================================================
# 三、額度還沒回來就再停一次（而且不吃毒列的額度）
# ===========================================================================

def test_a_re_park_counts_up_and_leaves_the_crash_budget_alone():
    """再停一次是**新的一列**：`usage_parks` 加一、沒有 `tries`。

    共用同一列（或把 `tries` 接著加）的話，額度用完幾次之後那一題就會被當成「會把
    行程弄死的毒列」丟進失敗佇列——而它其實一次都沒有把行程弄死過。"""
    _run_at, row = _park(parks=1)
    assert row["usage_parks"] == 2
    assert "tries" not in row, "停放吃掉了毒列防護的額度"
    assert row["status"] == b._DOROSSI_PARKED_STATUS


def test_the_try_cap_only_applies_to_a_row_that_died_mid_turn(monkeypatch):
    """`_DOROSSI_QUEUE_MAX_TRIES` 的判準必須同時看 `status == "running"`。

    放寬成「只要 tries 夠大就停掉」的話，一個停了很多次的用量等待會被誤判成毒列——
    而那是擁有者明文裁定不得設限的那一種等待。"""
    ran, _sent, _ch = _run_restore(monkeypatch, [_parked_row(
        run_at=time.time() - 60, tries=b._DOROSSI_QUEUE_MAX_TRIES + 3)])
    assert [x[2] for x in ran] == ["停一下這題"], ran
    assert b._dorossi_failed_queue_read() == []


def test_a_parked_row_runs_through_the_normal_queued_machinery():
    """AST：重跑走的是既有的 `_dorossi_run_queued_row`／`_dorossi_queue_start`。

    另外寫一支執行器的話，per-session 鎖、後端號誌、`running`／`tries` 帳會各自漏掉
    一部分，而漏掉的症狀是「同一個對話同時跑兩輪」這種只在並行下出現的東西。"""
    src = _body_src("_dorossi_restore_queue_group")
    assert "_dorossi_run_queued_row" in src and "_dorossi_queue_start" in src
    assert "usage_parks" in src, "停過幾次沒有帶回回合，退避會每次從頭算"


# ===========================================================================
# 四、到點時斷網
# ===========================================================================

def _offline_restore(monkeypatch, reachable: bool):
    monkeypatch.setattr(b, "dorossi_network_reachable",
                        lambda target: _async_value(reachable))
    not_found = b.discord.NotFound(
        types.SimpleNamespace(status=404, reason="nf"),
        {"code": 10008, "message": "Unknown Message"})
    return _run_restore(monkeypatch, [_parked_row(run_at=time.time() - 60)],
                        channel=_NoHistoryChannel(fetch_error=not_found))


def _async_value(value):
    async def _coro():
        return value
    return _coro()


def test_a_park_that_comes_due_while_offline_stays_parked(monkeypatch):
    """時間到了但平台連不上：原地再排一小段，**不准**停進失敗佇列。

    失敗佇列不會自己重跑（要擁有者 `/dorossi queue retry_failed`），所以把它丟進去等於
    把「時間到會自己跑」這個承諾悄悄改成「你自己記得回來按一下」。"""
    ran, _sent, _ch = _offline_restore(monkeypatch, reachable=False)
    assert ran == []
    assert b._dorossi_failed_queue_read() == [], "斷網被當成這一列有問題"
    rows = b._dorossi_queue_read()
    assert [r["status"] for r in rows] == [b._DOROSSI_PARKED_STATUS]
    assert rows[0]["run_at"] > time.time(), "沒有重新排時間，會變成零間隔空轉"
    assert "p1" not in b._dorossi_queue_live, (
        "還留在『手上』，之後的掃描會跳過它——那就是永遠不跑")


def test_a_park_that_comes_due_with_the_network_up_still_gets_parked_as_failed(
        monkeypatch):
    """網路是通的卻查不回錨點：那是這一列真的有問題，照舊停進失敗佇列。

    少了這一格，上面那條斷網豁免就會變成「查不回來一律不處理」的全面豁免，於是一個
    錨點被刪掉的列會每 30 秒重試一次，永遠。"""
    ran, sent, _ch = _offline_restore(monkeypatch, reachable=True)
    assert ran == []
    assert [r["error"] for r in b._dorossi_failed_queue_read()] == [
        "unverified:anchor"]
    assert b._dorossi_queue_read() == []
    assert any("沒辦法確認發問者" in str(x) for x in sent), sent


# ===========================================================================
# 五、看得見、關得掉
# ===========================================================================

def _state_with_session(monkeypatch):
    state = {UID: {"sessions": {SID: {"label": "L", "last_used": time.time()}},
                   "active": SID}}
    monkeypatch.setattr(b, "_dorossi_load_state", lambda: state)
    return state


def test_queue_show_lists_a_parked_turn_with_its_time(monkeypatch):
    """`/dorossi queue show` 要看得到它，而且要看得到**幾點跑**。

    看不到的話，擁有者唯一知道它存在的時刻就是它自己跑起來的那一刻。"""
    _state_with_session(monkeypatch)
    b._dorossi_queue_write([_parked_row(run_at=time.time() + 3600)])
    lines = "\n".join(b._dorossi_waiter_lines(UID))
    assert "parked=1" in lines, lines
    assert b._dorossi_parked_clock(_parked_row(run_at=time.time() + 3600)) in lines


def test_queue_show_still_lists_a_park_whose_session_was_deleted(monkeypatch):
    """對話被刪掉、停放列還在：照樣要列出來。

    只照 `sessions` 迭代的話這一列會完全隱形，然後在幾小時後憑空跑起一輪。"""
    _state_with_session(monkeypatch)
    b._dorossi_queue_write([_parked_row(sid="s9")])
    lines = "\n".join(b._dorossi_waiter_lines(UID))
    assert "s9" in lines and "parked=1" in lines, lines


def test_running_report_mentions_a_parked_turn(monkeypatch):
    """`/dorossi running` 也要講——它跟「正在等網路恢復」是同一個性質的東西。"""
    b._dorossi_queue_write([_parked_row()])
    text = b._dorossi_running_report({}, FakeMessage(b.DOROSSI_USER_ID))
    assert "等方案用量重設" in text and "1 件" in text, text


def test_queue_clear_cancels_a_parked_turn(monkeypatch):
    """`/dorossi queue clear` 要一起清掉它。

    不清的話，擁有者看到「已取消 N 筆」之後那一題照樣會在幾小時後跑起來——一個說了
    謊的取消比沒有取消更糟。"""
    _state_with_session(monkeypatch)
    b._dorossi_queue_write([_parked_row()])
    msg = FakeMessage(b.DOROSSI_USER_ID)
    said: list = []
    monkeypatch.setattr(b, "safe_reply",
                        lambda _m, content=None, **_k: _async_value(
                            said.append(content)))
    asyncio.run(b.mcmd_queue(msg, "clear"))
    assert b._dorossi_queue_read() == [], "clear 之後它還會自己跑起來"
    assert any("已取消 1 筆" in str(x) for x in said), said


def test_abort_cancels_a_parked_turn(monkeypatch):
    """`/dorossi abort <id>`／`all` 也要關得掉它——跟「取消自動接續」同一個形狀。"""
    b._dorossi_queue_write([_parked_row()])
    msg = FakeMessage(b.DOROSSI_USER_ID)
    said: list = []
    monkeypatch.setattr(b, "safe_reply",
                        lambda _m, content=None, **_k: _async_value(
                            said.append(content)))

    async def _no_autoresume(uid, names):
        del uid, names
        return []

    monkeypatch.setattr(b, "_dorossi_cancel_autoresume", _no_autoresume)
    handled = asyncio.run(b._dorossi_abort_reply_pending(msg, UID, "all"))
    assert handled is True
    assert b._dorossi_queue_read() == []
    assert any("方案用量重設" in str(x) for x in said), said


def test_abort_says_nothing_when_there_is_nothing_parked(monkeypatch):
    """沒有東西可取消時要回 False，讓呼叫端走原本的路（否則會吃掉產圖那條路）。"""
    msg = FakeMessage(b.DOROSSI_USER_ID)

    async def _no_autoresume(uid, names):
        del uid, names
        return []

    monkeypatch.setattr(b, "_dorossi_cancel_autoresume", _no_autoresume)
    assert asyncio.run(b._dorossi_abort_reply_pending(msg, UID, "all")) is False


def test_undoing_a_cancel_puts_a_park_back_instead_of_running_it(monkeypatch):
    """`/dorossi queue undo` 對停放列要**放回去繼續等**，不是立刻跑。

    立刻跑只會再撞一次同一面牆、再停一次，而且多送一則訊息。"""
    _state_with_session(monkeypatch)
    row = _parked_row(run_at=time.time() + 3600)
    b._dorossi_queue_undo.clear()
    b._dorossi_queue_undo.append([row])
    ran: list = []
    monkeypatch.setattr(b, "_schedule_coro",
                        lambda coro, label="": (ran.append(label), coro.close()))
    monkeypatch.setattr(b, "safe_reply",
                        lambda _m, content=None, **_k: _async_value(None))
    asyncio.run(b.mcmd_queue(FakeMessage(b.DOROSSI_USER_ID), "undo"))
    assert ran == [], "還沒到點卻被排去跑了"
    assert [r["id"] for r in b._dorossi_queue_read()] == ["p1"]


# ===========================================================================
# 六、迴圈那條路沒有被改到
# ===========================================================================

def test_the_loop_path_still_waits_in_place():
    """自走迴圈的用量上限 handler 維持原樣：原地等 ＋ `continue`，不改成停進佇列。

    迴圈整段等待都在同一個行程裡、而且握著 per-session 鎖，改成停放反而會把脈絡與
    鎖的語意一起弄亂。這一支釘住兩條路是**分開**的。"""
    loop = _func("_dorossi_run_loop")
    handlers = [h for node in ast.walk(loop)
                if isinstance(node, ast.Try)
                for h in node.handlers
                if h.type is not None
                and "_DorossiUsageLimitError" in ast.unparse(h.type)]
    assert handlers, "找不到迴圈的用量上限 handler"
    src = "\n".join(ast.unparse(h) for h in handlers)
    assert "_dorossi_wait_for_usage_reset" in src
    assert "_dorossi_usage_park_turn" not in src, (
        "迴圈那條路被改成停進佇列了——那不是這次要動的東西")


def test_the_loop_wait_still_uses_the_process_clock():
    """迴圈的等待維持 `time.monotonic()`：它不需要跨行程，而 NTP 校時不該影響它。"""
    src = _body_src("_dorossi_wait_for_usage_reset")
    assert "time.monotonic()" in src and "time.time()" not in src
