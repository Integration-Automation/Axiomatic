"""斷網之後接得回來：判定、等待、停下來的原因、自動接續掃描、`continue all`、abort。

2026-09-22 17:32–18:37 本機 DNS 整段失效，六個正在跑的自走任務全部停了、而且沒有
一個被接回去。四個缺口各有一組測試：

* **後端連不上伺服器被判成「工作階段過舊」**——丟掉脈絡開新的重試（一樣連不上），
  泛用重試三次就停。現在是專屬的 `_DorossiOfflineError`，三個後端都有（第一節）。
* **對話平台連不上時迴圈直接死掉**，而且 `finally` 把標記寫成 `live=False`（第二節是
  等待的零件；迴圈本身的行為在 `test_dorossi_loop.py` 末段）。
* **停下來只記「不是 live」、不記為什麼**，自動接續分不出斷網與 abort（第三節）。
* **自動接續每個行程只掃一次**，同一個行程重新連上之後什麼都不做（第四節）。

另外兩個新入口：`/dorossi session continue all`（第五節）與 `abort` 取消「等著被
自動接續」的任務（第六節）。

測試不連網：連線探測一律換成替身；會落地的檔案全部導到 `tmp_path`。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types

import aiohttp
import discord
import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord_bot as b  # noqa: E402
import dorossi_backend as db  # noqa: E402

UID = str(b.DOROSSI_USER_ID)
STRANGER = max(b.OWNER_USER_ID, b.DOROSSI_USER_ID) + 1

# 2026-09-22 實測的那一則 result（`claude -p exited 1: subtype=success; is_error=True;
# result=API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)`）。
MEASURED_OFFLINE = {"type": "result", "subtype": "success", "is_error": True,
                    "api_error_status": None,
                    "result": "API Error: Can't reach the API server — check your "
                              "internet or DNS (ENOTFOUND)",
                    "session_id": "sess-abc", "total_cost_usd": 0}


def _claude_verdict(result_ev, *, session_id="sess-abc", err="", rc=1):
    state = db._ClaudeStreamState(session_id)
    if result_ev is not None:
        state.feed(json.dumps(result_ev), None)
    return lambda: db._claude_stream_verdict(state, rc, err, session_id)


# ---------------------------------------------------------------------------
# 一、後端的「連不上伺服器」判定
# ---------------------------------------------------------------------------

def test_the_measured_outage_is_offline_and_keeps_the_session():
    """實測的那一則：要是離線，**不是** resume 重試——後者會丟掉脈絡開新的工作階段。"""
    with pytest.raises(db._DorossiOfflineError) as got:
        _claude_verdict(MEASURED_OFFLINE)()
    assert not isinstance(got.value, db._DorossiResumeError)
    assert got.value.session_id == "sess-abc", "要用同一個工作階段重跑"
    assert got.value.backend == "claude_code"


def test_a_fresh_session_that_cannot_connect_is_offline_too():
    """沒有工作階段的那一次（以前是裸 `RuntimeError`，進泛用重試三次就停）。"""
    with pytest.raises(db._DorossiOfflineError) as got:
        _claude_verdict(MEASURED_OFFLINE, session_id=None)()
    assert got.value.session_id is None


def test_the_stderr_alone_is_enough():
    ev = dict(MEASURED_OFFLINE, result="API Error")
    with pytest.raises(db._DorossiOfflineError):
        _claude_verdict(ev, err="getaddrinfo ENOTFOUND api.example")()


def test_a_long_answer_that_talks_about_econnreset_is_not_offline():
    """result 文字那一條有長度上限：一篇剛好討論連線錯誤的長答案不能被判成斷網
    （用量上限與未登入的判定都在同一種寫法上摔過）。"""
    ev = dict(MEASURED_OFFLINE, result="ECONNRESET 的處理方式如下：" + "說明。" * 200)
    with pytest.raises(db._DorossiResumeError):
        _claude_verdict(ev)()


def test_a_successful_round_is_never_offline():
    ev = dict(MEASURED_OFFLINE, is_error=False, result="完成了（ENOTFOUND 已修好）")
    assert _claude_verdict(ev, rc=0)() == "ok"


def test_a_server_overload_still_wins_over_the_network_words():
    """判定順序是規則：暫時性故障（5xx）排在離線前面，它有自己的退避。"""
    ev = dict(MEASURED_OFFLINE, api_error_status=529)
    with pytest.raises(db._DorossiTransientError):
        _claude_verdict(ev)()


def test_the_verdict_docstring_lists_the_new_branch_in_order():
    doc = db._claude_stream_verdict.__doc__
    assert "CLI 拒絕旗標 → 連不上伺服器 → resume 重試" in doc


@pytest.mark.parametrize("blob, expected", [
    ("stream disconnected: error sending request for url (https://x)", db._DorossiOfflineError),
    ("failed to lookup address information", db._DorossiOfflineError),
    ("something else went wrong", db._DorossiResumeError),
])
def test_codex_has_the_same_branch(blob, expected):
    state = db._CodexStreamState("thread-1")
    with pytest.raises(expected) as got:
        db._codex_stream_verdict(state, 1, blob, "thread-1")
    if expected is db._DorossiOfflineError:
        assert got.value.session_id == "thread-1" and got.value.backend == "codex"


def test_codex_usage_limit_still_wins():
    state = db._CodexStreamState("t")
    with pytest.raises(db._DorossiUsageLimitError):
        db._codex_stream_verdict(
            state, 1, "You've hit your usage limit. error sending request for url", "t")


def _api_request():
    """SDK 的例外只把 request 存起來，不檢查型別；不必為了它另外 import 一個
    `requirements.txt` 沒宣告的套件（`test_bot_helpers` 會擋）。"""
    return types.SimpleNamespace(method="POST", url="https://example.invalid/v1/messages")


@pytest.mark.skipif(db.anthropic is None, reason="api SDK not installed")
def test_the_api_connection_error_is_offline_but_a_timeout_is_not():
    """連線層失敗是離線；逾時（連上了、對方一直不回）不是——等網路不會讓它變好。"""
    assert db._dorossi_api_is_offline(db.anthropic.APIConnectionError(request=_api_request()))
    assert not db._dorossi_api_is_offline(db.anthropic.APITimeoutError(request=_api_request()))
    assert not db._dorossi_api_is_offline(RuntimeError("boom"))


@pytest.mark.skipif(db.anthropic is None, reason="api SDK not installed")
def test_the_api_call_raises_offline(monkeypatch):
    class _Messages:
        async def create(self, **_kw):
            raise db.anthropic.APIConnectionError(request=_api_request())

    monkeypatch.setattr(db, "_get_dorossi_client",
                        lambda: types.SimpleNamespace(messages=_Messages()))
    with pytest.raises(db._DorossiOfflineError) as got:
        asyncio.run(db._dorossi_via_api("q", []))
    assert got.value.backend == "api"


def test_the_probe_answers_without_touching_the_network(monkeypatch):
    """探測本身：連得上 → True；解析失敗／逾時 → False；認不得的 target 用平台那台。"""
    seen = []

    class _Writer:
        def close(self):
            seen.append("closed")

        async def wait_closed(self):
            return None

    async def ok(host, port):
        seen.append((host, port))
        return object(), _Writer()

    async def dns_fail(host, port):
        raise OSError(11001, "getaddrinfo failed")

    monkeypatch.setattr(asyncio, "open_connection", ok)
    assert asyncio.run(db.dorossi_network_reachable("claude_code")) is True
    assert asyncio.run(db.dorossi_network_reachable("nonsense")) is True
    assert seen[0] == db.DOROSSI_NETWORK_PROBE_HOSTS["claude_code"]
    assert db.DOROSSI_NETWORK_PROBE_HOSTS["platform"] in seen
    assert "closed" in seen, "探測連上之後要關掉連線"
    monkeypatch.setattr(asyncio, "open_connection", dns_fail)
    assert asyncio.run(db.dorossi_network_reachable("codex")) is False


# ---------------------------------------------------------------------------
# 二、等待的零件
# ---------------------------------------------------------------------------

def _http_exc(status):
    return discord.HTTPException(types.SimpleNamespace(status=status, reason="x"), "x")


@pytest.mark.parametrize("exc, expected", [
    (aiohttp.ClientConnectionError("dns"), True),
    (aiohttp.ServerDisconnectedError(), True),
    (_http_exc(503), True),
    (_http_exc(500), True),
    (_http_exc(404), False),
    (_http_exc(403), False),
    (TimeoutError("backend idle"), False),
    (RuntimeError("x"), False),
    (FileNotFoundError("cli"), False),
])
def test_what_counts_as_the_platform_being_unreachable(exc, expected):
    """4xx 不算（等網路也不會好），裸的 TimeoutError 不算（後端看門狗也丟它）。"""
    assert b._dorossi_is_platform_offline(exc) is expected


@pytest.fixture
def fast_waits(monkeypatch):
    monkeypatch.setattr(b, "_DOROSSI_NETWORK_POLL_SEC", 0.01)
    monkeypatch.setattr(b, "_DOROSSI_OFFLINE_ABORT_POLL_SEC", 0.005)
    monkeypatch.setattr(b, "_DOROSSI_OFFLINE_FALSE_ALARM_BASE_SEC", 0.01)
    monkeypatch.setattr(b, "_DOROSSI_OFFLINE_FALSE_ALARM_CAP_SEC", 0.02)
    monkeypatch.setattr(b, "_dorossi_event", lambda *_a, **_k: None)


def _probe(monkeypatch, answers):
    calls = []

    async def probe(target):
        calls.append(target)
        return answers.pop(0) if answers else True

    monkeypatch.setattr(b, "dorossi_network_reachable", probe)
    return calls


def test_the_wait_polls_until_the_network_is_back(monkeypatch, fast_waits):
    calls = _probe(monkeypatch, [False, False, True])
    st = b._DorossiLoopState(UID, "s1")
    seen = []

    async def scenario():
        async def peek():
            await asyncio.sleep(0.005)
            seen.append((st.offline_since is not None, st.offline_target))
        task = asyncio.ensure_future(peek())
        got = await asyncio.wait_for(b._dorossi_wait_online(st, "claude_code"), 5)
        await task
        return got

    assert asyncio.run(scenario()) is True
    assert calls == ["claude_code"] * 3
    assert seen == [(True, "claude_code")], "等待期間要看得出在等什麼"
    assert st.offline_since is None and st.offline_target is None, "等完要清掉"


def test_only_abort_ends_the_wait(monkeypatch, fast_waits):
    """沒有次數上限：網路一直不回來就一直等，只有 abort 能結束——而且清得乾淨。"""
    _probe(monkeypatch, [False] * 1000)
    st = b._DorossiTurnState(UID, "s1")

    async def scenario():
        async def flip():
            await asyncio.sleep(0.05)
            st.request_abort()
        task = asyncio.ensure_future(flip())
        got = await asyncio.wait_for(b._dorossi_wait_online(st, "api"), 5)
        await task
        return got

    assert asyncio.run(scenario()) is False
    assert st.offline_since is None


def test_a_false_alarm_is_not_retried_at_full_speed(monkeypatch, fast_waits):
    """第一次探測就通（網路一直是好的）：判成離線的其實是別的問題，要先退避一下，
    否則會變成零間隔的空轉。真的斷過網的，回來就立刻重試（上一支）。"""
    _probe(monkeypatch, [True])
    slept = []

    async def fake_sleep(st, seconds):
        slept.append(seconds)
        return True

    monkeypatch.setattr(b, "_dorossi_sleep_unless_aborted", fake_sleep)
    st = b._DorossiLoopState(UID, "s1")
    assert asyncio.run(b._dorossi_wait_online(st, "platform", attempt=3)) is True
    assert slept == [b._dorossi_offline_false_alarm_wait(3)]
    assert b._dorossi_offline_false_alarm_wait(1) < b._dorossi_offline_false_alarm_wait(2)


def test_a_platform_call_is_retried_after_the_network_returns(monkeypatch, fast_waits):
    _probe(monkeypatch, [False, True])
    attempts = []

    async def send():
        attempts.append(1)
        if len(attempts) == 1:
            raise aiohttp.ClientConnectionError("Cannot connect to host [getaddrinfo failed]")
        return "posted"

    st = b._DorossiLoopState(UID, "s1")
    assert asyncio.run(b._dorossi_loop_platform(st, send)) == "posted"
    assert len(attempts) == 2, "連線回來之後要做同一件事"


def test_a_platform_call_that_fails_for_another_reason_still_raises(monkeypatch, fast_waits):
    async def send():
        raise _http_exc(403)

    with pytest.raises(discord.HTTPException):
        asyncio.run(b._dorossi_loop_platform(b._DorossiLoopState(UID, "s1"), send))


def test_an_abort_while_the_platform_is_down_is_reported(monkeypatch, fast_waits):
    _probe(monkeypatch, [False] * 1000)
    st = b._DorossiLoopState(UID, "s1")
    st.abort = True

    async def send():
        raise aiohttp.ClientConnectionError("down")

    assert asyncio.run(b._dorossi_loop_platform(st, send)) is b._DOROSSI_ABORTED


class _Live:
    def __init__(self, landed):
        self.message = object()
        self.landed = landed

    async def finalize(self, content):
        return self.landed


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **_kw):
        self.sent.append(content)


@pytest.mark.parametrize("landed, resend, expected", [
    (False, True, ["第一段", "第二段"]),
    (True, True, ["第二段"]),
    (False, False, ["第二段"]),
])
def test_an_answer_whose_edit_did_not_land_is_sent_again(landed, resend, expected):
    """迴圈送答案時 edit 被吞掉的失敗（斷網、訊息被刪）要改成另發一則；單輪回合不變。"""
    channel = _Channel()
    message = types.SimpleNamespace(channel=channel)
    asyncio.run(b._dorossi_reply_final(message, _Live(landed), ["第一段", "第二段"],
                                       resend_unlanded=resend))
    assert channel.sent == expected


def test_finalize_reports_whether_the_content_landed():
    class _Msg:
        def __init__(self, fail):
            self.fail = fail

        async def edit(self, content=None):
            if self.fail:
                raise aiohttp.ClientConnectionError("down")

    assert asyncio.run(b._DorossiLiveMessage(_Msg(False)).finalize("x")) is True
    assert asyncio.run(b._DorossiLiveMessage(_Msg(True)).finalize("x")) is False
    assert asyncio.run(b._DorossiLiveMessage(None).finalize("x")) is False


# ---------------------------------------------------------------------------
# 三、停下來的原因
# ---------------------------------------------------------------------------

def _pending(**over):
    sess = {"loop_pending": {"ts": time.time(), "task": "整理", "live": True,
                             "channel_id": 11, "message_id": 22}}
    sess["loop_pending"].update(over)
    return sess


@pytest.mark.parametrize("reason, resumable", [
    ("network", True), ("interrupted", True),
    ("abort", False), ("error", False), ("silence", False), ("transient", False),
])
def test_only_network_and_interruption_are_resumed_automatically(reason, resumable):
    sess = _pending()
    db._dorossi_mark_loop_stopped(sess, reason)
    assert sess["loop_pending"]["live"] is False
    assert sess["loop_pending"]["stop"] == reason
    assert (db._dorossi_loop_autoresume_plan(sess) is not None) is resumable
    assert db._dorossi_loop_resume_plan(sess) is not None, "人工接續永遠可以"


def test_an_unknown_reason_is_not_recorded():
    sess = _pending(stop="network")
    db._dorossi_mark_loop_stopped(sess, "whatever")
    assert "stop" not in sess["loop_pending"], "認不得的原因等同沒有原因——不能沿用舊的"
    assert db._dorossi_loop_autoresume_plan(sess) is None


def test_a_legacy_stopped_marker_is_still_left_alone():
    """沒有 `stop` 的舊標記（`live` 為假）：原因不明，自動接續照舊不碰。"""
    assert db._dorossi_loop_autoresume_plan(_pending(live=False)) is None


def test_a_heartbeat_clears_the_stop_reason():
    sess = _pending(live=False, stop="network")
    db._dorossi_touch_loop_pending(sess)
    assert sess["loop_pending"]["live"] is True and "stop" not in sess["loop_pending"]


def test_an_aborted_pending_task_is_never_resumed():
    sess = _pending(live=False, stop="network")
    db._dorossi_mark_loop_aborted(sess)
    assert sess["loop_pending"]["stop"] == "abort"
    assert db._dorossi_loop_autoresume_plan(sess) is None
    empty: dict = {}
    db._dorossi_mark_loop_aborted(empty)
    assert empty == {}, "沒有標記就不建立"


def test_a_network_stopped_task_that_can_no_longer_resume_is_reported():
    """太舊而接不回來的：斷網停下來的也要列進「被遺棄」（以前只看 `live`）。"""
    sess = _pending(live=False, stop="network", ts=1.0)
    rows = db.dorossi_abandoned_loops({UID: {"sessions": {"s1": sess}}})
    assert [r[:2] for r in rows] == [(UID, "s1")]
    aborted = _pending(live=False, stop="abort", ts=1.0)
    assert db.dorossi_abandoned_loops({UID: {"sessions": {"s1": aborted}}}) == []


# ---------------------------------------------------------------------------
# 四、自動接續掃描可以重複跑
# ---------------------------------------------------------------------------

class _Anchor:
    def __init__(self):
        self.author = types.SimpleNamespace(id=b.OWNER_USER_ID)
        self.id = 22


class _AnchorChannel:
    async def fetch_message(self, _mid):
        return _Anchor()


@pytest.fixture
def scan(monkeypatch):
    env = types.SimpleNamespace(scheduled=[], state={}, reports=[])

    def fake_schedule(coro, *, label=""):
        env.scheduled.append(label)
        coro.close()

    async def fake_rmw(mutate):
        return mutate(env.state)

    async def fake_report(sids):
        env.reports.append(list(sids))

    monkeypatch.setattr(b, "_dorossi_resume_inflight", set())
    monkeypatch.setattr(b, "_dorossi_autoresume_reported", set())
    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "client", types.SimpleNamespace(
        get_channel=lambda _cid: _AnchorChannel(), user=None))
    monkeypatch.setattr(b, "_dorossi_load_state", lambda: env.state)
    monkeypatch.setattr(b, "_dorossi_state_rmw", fake_rmw)
    monkeypatch.setattr(b, "_schedule_coro", fake_schedule)
    monkeypatch.setattr(b, "_dorossi_report_autoresume_declined", fake_report)
    return env


def _scan_once():
    asyncio.run(asyncio.wait_for(b._dorossi_resume_scan(), 10))


def test_a_task_stopped_by_the_outage_is_picked_back_up(scan):
    """2026-09-22 的形狀：`live=False`，但原因是 network——要接回去。"""
    scan.state[UID] = {"active": "s1", "sessions": {
        "s1": _pending(live=False, stop="network"),
        "s2": _pending(live=False, stop="abort"),
        "s3": _pending(live=False)}}
    _scan_once()
    assert scan.scheduled == ["dorossi-autoresume-s1"], (
        "只有斷網停下來的該被接回去；abort 與原因不明的舊標記都不碰")


def test_a_second_scan_in_the_same_process_does_not_resume_it_again(scan):
    scan.state[UID] = {"active": "s1", "sessions": {"s1": _pending(live=False,
                                                                     stop="network")}}
    _scan_once()
    _scan_once()
    assert scan.scheduled == ["dorossi-autoresume-s1"]
    b._dorossi_resume_inflight.clear()          # 那一次接續跑完了（結束時會拿掉）
    b._dorossi_loops[(UID, "s1")] = object()    # ……而且迴圈正在跑
    _scan_once()
    assert scan.scheduled == ["dorossi-autoresume-s1"]


def test_the_resume_releases_its_reservation_however_it_ends(monkeypatch):
    """登記的唯一出口是 `_dorossi_resume_loop` 的 `finally`——失敗也要拿掉，否則那個
    工作階段從此再也不會被自動接回去。"""
    monkeypatch.setattr(b, "_dorossi_resume_inflight", {(UID, "s1")})

    async def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(b, "_dorossi_resume_loop_now", boom)
    msg = types.SimpleNamespace(author=types.SimpleNamespace(id=int(UID)))
    with pytest.raises(RuntimeError):
        asyncio.run(b._dorossi_resume_loop(msg, "s1"))
    assert b._dorossi_resume_inflight == set()


def test_a_scan_requested_while_one_is_running_runs_again_afterwards(monkeypatch):
    calls = []
    scheduled = []

    async def body():
        calls.append(1)
        if len(calls) == 1:
            await b._dorossi_resume_scan()     # 掃描中又被觸發（例如 on_resumed）

    monkeypatch.setattr(b, "_dorossi_autoresume_pending_loops", body)
    monkeypatch.setattr(b, "_dorossi_resume_scan_busy", False)
    monkeypatch.setattr(b, "_dorossi_resume_scan_again", False)
    monkeypatch.setattr(b, "_schedule_coro",
                        lambda coro, *, label="": (scheduled.append(label), coro.close()))
    _scan_once()
    assert calls == [1], "同一時間只跑一個掃描"
    assert scheduled == ["dorossi-autoresume-rescan"], "被擋下的那一次要補掃"
    assert b._dorossi_resume_scan_busy is False


def test_a_decline_is_reported_once_per_heartbeat(scan):
    """每次重連都會掃；同一個接不回來的標記不能每斷一次線就再報一次。"""
    marker = _pending(live=False, stop="network")
    scan.state[UID] = {"active": "s1", "sessions": {"s1": marker}}

    class _Gone:
        async def fetch_message(self, _mid):
            raise RuntimeError("404 Not Found")

        async def history(self, **_kw):
            raise RuntimeError("no")

    b.client.get_channel = lambda _cid: _Gone()
    _scan_once()
    _scan_once()
    assert scan.reports == [["s1"], []]
    marker["loop_pending"]["ts"] -= 5   # 心跳變了（任務後來又跑過、又因斷網停下來）
    _scan_once()
    assert scan.reports[-1] == ["s1"]


# ---------------------------------------------------------------------------
# 五、`/dorossi session continue all`
# ---------------------------------------------------------------------------

def _state_for_all():
    now = time.time()
    return {UID: {"active": "s1", "sessions": {
        "s1": dict(_pending(ts=now - 10, live=False, stop="network"), label="主專案",
                   cc_session_id="c1"),
        "s2": dict(_pending(ts=now - 20, live=False, stop="abort"), label="中止的",
                   cc_session_id="c2"),
        "s3": dict(_pending(ts=now - 30, live=False), label="舊標記", cc_session_id="c3"),
        "s4": dict(_pending(ts=now - 40), cc_session_id="c4", archived=True),
        "s5": dict(_pending(ts=now - 50, live=False, stop="error"), cc_session_id="c5"),
        "s6": {"label": "沒有任務"},
    }}}


def test_the_plan_sorts_freshest_first_and_names_every_outcome():
    plan = b._dorossi_resume_all_plan(_state_for_all(), UID, busy={(UID, "s5")},
                                      room=None)
    assert [(sid, verdict) for sid, _l, verdict in plan] == [
        ("s1", "resume"), ("s2", "resume"), ("s3", "resume"), ("s4", "archived"),
        ("s5", "running")]


def test_the_plan_resumes_even_an_aborted_session():
    """`all` 連自己 abort 過的也接。s2 的標記 `stop == "abort"`，以前是 "aborted"
    （略過），現在是 "resume"。只有封存（s4）仍然不接。"""
    plan = b._dorossi_resume_all_plan(_state_for_all(), UID, busy=set(), room=None)
    verdicts = {sid: v for sid, _l, v in plan}
    assert verdicts["s2"] == "resume"
    assert verdicts["s4"] == "archived"
    assert "aborted" not in verdicts.values()


def test_the_plan_respects_the_loop_cap():
    plan = b._dorossi_resume_all_plan(_state_for_all(), UID, busy=set(), room=1)
    verdicts = {sid: v for sid, _l, v in plan}
    assert verdicts["s1"] == "resume" and verdicts["s3"] == "cap"
    assert verdicts["s5"] == "cap"


def test_the_plan_survives_a_broken_record():
    for junk in ({}, {UID: "x"}, {UID: {"sessions": "x"}}):
        assert b._dorossi_resume_all_plan(junk, UID, busy=set(), room=None) == []


@pytest.fixture
def cont(monkeypatch, tmp_path):
    env = types.SimpleNamespace(replies=[], resumed=[], state=_state_for_all())

    async def fake_reply(_message, content=None, **_kw):
        env.replies.append(content)

    def fake_resume(message, sid, **kwargs):
        env.resumed.append((sid, kwargs.get("ack_override")))

        async def _noop():
            return None
        return _noop()

    def fake_schedule(coro, *, label=""):
        asyncio.get_event_loop().create_task(coro)

    monkeypatch.setattr(b, "safe_reply", fake_reply)
    monkeypatch.setattr(b, "_dorossi_resume_loop", fake_resume)
    monkeypatch.setattr(b, "_schedule_coro", fake_schedule)
    monkeypatch.setattr(b, "_dorossi_load_state", lambda: env.state)
    monkeypatch.setattr(b, "_dorossi_resume_inflight", set())
    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "_dorossi_session_locks", {})
    monkeypatch.setattr(b, "DOROSSI_MAX_PARALLEL_LOOPS", 0)
    monkeypatch.setattr(b, "DOROSSI_BACKEND", "claude_code")
    monkeypatch.setattr(b, "DOROSSI_CC_TOOLS", "full")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "events.ndjson")
    return env


def _owner_msg(uid=None):
    return types.SimpleNamespace(
        author=types.SimpleNamespace(id=b.DOROSSI_USER_ID if uid is None else uid),
        channel=types.SimpleNamespace(id=0), id=5)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize("rest", ["all continue", "continue all", "全部 continue"])
def test_continue_all_resumes_what_it_should_and_reports_the_rest(cont, rest):
    async def scenario():
        await b.mcmd_session(_owner_msg(), rest)
        await _settle()

    asyncio.run(scenario())
    # s2 是 abort 過的——`all` 也接它（擁有者裁定）。
    assert sorted(sid for sid, _ack in cont.resumed) == ["s1", "s2", "s3", "s5"]
    text = "\n".join(cont.replies)
    assert "已接續 4 個" in text and "略過 1 個" in text, text
    assert "`s2`" in text and "已接續" in text
    assert "`s4`" in text and "已封存" in text
    assert "`s6`" not in text, "沒有任務的工作階段不列"


def test_continue_all_skips_running_busy_and_capped_ones(cont, monkeypatch):
    """上限 4、s5 在跑 → 還剩三個名額：s1、s2（abort 過的也接了）接續，s3 這個工作
    階段的鎖有人拿著（略過）。s4 已封存。"""
    monkeypatch.setattr(b, "DOROSSI_MAX_PARALLEL_LOOPS", 4)
    cont.state[UID]["sessions"]["s1"]["loop_pending"]["stop"] = "network"
    b._dorossi_loops[(UID, "s5")] = object()
    lock = asyncio.Lock()

    async def scenario():
        await lock.acquire()
        b._dorossi_session_locks[(UID, "s3")] = lock
        await b.mcmd_session(_owner_msg(), "all continue")
        await _settle()

    asyncio.run(scenario())
    assert sorted(sid for sid, _ack in cont.resumed) == ["s1", "s2"]
    text = "\n".join(cont.replies)
    assert "`s5`" in text and "已在進行中" in text
    assert "`s3`" in text and "正在處理別的提問" in text


def test_continue_all_fails_cleanly_when_the_mode_cannot_loop(cont, monkeypatch):
    monkeypatch.setattr(b, "DOROSSI_CC_TOOLS", "off")
    asyncio.run(b.mcmd_session(_owner_msg(), "all continue"))
    assert cont.resumed == []
    # s1／s2（abort 過的）／s3／s5 四個都是可接續候選，模式不能自走 → 全部失敗。
    assert "失敗 4 個" in "\n".join(cont.replies)


def test_continue_all_with_nothing_pending_says_so(cont):
    cont.state = {UID: {"sessions": {"s1": {}}}}
    asyncio.run(b.mcmd_session(_owner_msg(), "all continue"))
    assert cont.replies == ["沒有中斷的自走任務可以接續。"]


def test_continue_all_is_owner_only(cont):
    asyncio.run(b.mcmd_session(_owner_msg(STRANGER), "all continue"))
    assert cont.resumed == [] and len(cont.replies) == 1
    assert "僅限" in cont.replies[0]


def test_the_slash_command_takes_all_as_the_id(monkeypatch):
    seen = []

    async def fake_slash_run(interaction, handler, *args, detach_ack=None):
        seen.append((handler, args))

    monkeypatch.setattr(b, "_slash_run", fake_slash_run)
    asyncio.run(b.slash_dorossi_session_continue.callback(object(), "all"))
    assert seen == [(b.mcmd_session, ("all continue",))]


# ---------------------------------------------------------------------------
# 六、abort 也管得到「等著被自動接續」的與「等網路」的
# ---------------------------------------------------------------------------

@pytest.fixture
def abort_env(monkeypatch):
    env = types.SimpleNamespace(replies=[], state={UID: {"active": "s1", "sessions": {
        "s1": _pending(live=False, stop="network"),
        "s2": _pending(live=False, stop="interrupted"),
        "s3": _pending(live=False, stop="error")}}})

    async def fake_reply(_message, content=None, **_kw):
        env.replies.append(content)

    async def fake_rmw(mutate):
        return mutate(env.state)

    monkeypatch.setattr(b, "safe_reply", fake_reply)
    monkeypatch.setattr(b, "_dorossi_load_state", lambda: env.state)
    monkeypatch.setattr(b, "_dorossi_state_rmw", fake_rmw)
    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "_dorossi_turns", [])
    monkeypatch.setattr(b, "_dorossi_resume_inflight", set())
    return env


def _stop(env, sid):
    return env.state[UID]["sessions"][sid]["loop_pending"].get("stop")


def test_abort_by_id_cancels_a_pending_autoresume(abort_env):
    asyncio.run(b.mcmd_abort(_owner_msg(), "s1"))
    assert _stop(abort_env, "s1") == "abort"
    assert _stop(abort_env, "s2") == "interrupted", "沒指名的不動"
    assert "取消" in abort_env.replies[0] and "〔s1〕" in abort_env.replies[0]


def test_abort_all_cancels_every_pending_autoresume(abort_env):
    asyncio.run(b.mcmd_abort(_owner_msg(), "all"))
    assert _stop(abort_env, "s1") == _stop(abort_env, "s2") == "abort"
    assert _stop(abort_env, "s3") == "error", "本來就不會自動接續的不改"


def test_abort_of_a_session_with_nothing_pending_says_so(abort_env):
    asyncio.run(b.mcmd_abort(_owner_msg(), "s3"))
    assert abort_env.replies == ["該對話沒有進行中的任務。"]
    assert _stop(abort_env, "s3") == "error"


def test_abort_reaches_a_single_turn_waiting_for_the_network(abort_env):
    turn = b._DorossiTurnState(UID, "s4")
    turn.offline_since = time.time()
    b._dorossi_turns.append(turn)
    asyncio.run(b.mcmd_abort(_owner_msg(), "s4"))
    assert turn.abort is True
    assert abort_env.replies == ["已中止〔s4〕的任務。"]


def test_a_single_turn_that_is_simply_running_is_not_an_abort_target(abort_env):
    """一般在跑的單輪回合有看門狗；只有「等網路」那段沒有上限的等待歸 abort 管。"""
    turn = b._DorossiTurnState(UID, "s4")
    b._dorossi_turns.append(turn)
    asyncio.run(b.mcmd_abort(_owner_msg(), "s4"))
    assert turn.abort is False


# ---------------------------------------------------------------------------
# 七、主機休眠：看門狗不把睡著的時間算進去（第二階段）
# ---------------------------------------------------------------------------

class _StuckStream:
    """`readline` 一直等、直到 `gate` 被放開；記下被叫了幾次（看是不是同一個 readline）。"""

    def __init__(self):
        self.calls = 0
        self.gate = None

    async def readline(self):
        self.calls += 1
        if self.gate is None:
            self.gate = asyncio.Event()
        await self.gate.wait()
        return b"line\n"


def test_a_suspension_is_not_counted_against_the_watchdog(monkeypatch):
    """睡著的那一段只算它本來該等的長度；醒著的時間累計到上限才逾時。"""
    monkeypatch.setattr(db, "_DOROSSI_WATCH_SLICE_SEC", 0.01)
    monkeypatch.setattr(db, "DOROSSI_SUSPEND_SEEN", {"count": 0, "seconds": 0.0})
    real = db.dorossi_elapsed_with_gap
    calls = []

    def fake_gap(mono0, wall0, expected):
        calls.append(expected)
        if len(calls) == 1:
            return float(expected), 3600.0      # 第一段：主機睡了一個小時
        return real(mono0, wall0, expected)

    monkeypatch.setattr(db, "dorossi_elapsed_with_gap", fake_gap)
    stream, clock = _StuckStream(), db._DorossiWatchClock()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(db._dorossi_readline_watched(stream, 0.05, clock))
    assert clock.suspended == 3600.0, "睡著的時間要記下來，硬上限也要扣掉它"
    assert db.DOROSSI_SUSPEND_SEEN["count"] == 1
    assert len(calls) >= 2, "睡著那一段不能吃掉醒著的等待時間（算進去的話第一段就逾時了）"
    assert stream.calls == 1, "切成小段等的是同一個 readline，不是每段重開一次"


def test_a_line_that_arrives_between_slices_is_returned(monkeypatch):
    monkeypatch.setattr(db, "_DOROSSI_WATCH_SLICE_SEC", 0.01)
    stream = _StuckStream()

    async def scenario():
        async def release():
            await asyncio.sleep(0.03)
            stream.gate.set()
        task = asyncio.ensure_future(release())
        got = await db._dorossi_readline_watched(stream, 5.0, db._DorossiWatchClock())
        await task
        return got

    assert asyncio.run(scenario()) == b"line\n"
    assert stream.calls == 1


def test_the_gap_helper_only_flags_long_gaps():
    now_m, now_w = time.monotonic(), time.time()
    assert db.dorossi_elapsed_with_gap(now_m, now_w, 5.0)[1] == 0.0
    elapsed, slept = db.dorossi_elapsed_with_gap(now_m - 3605.0, now_w - 3605.0, 5.0)
    assert elapsed == 5.0 and slept > 3000


def test_both_backends_read_through_the_suspend_aware_reader():
    """AST：兩個後端的讀取迴圈都走 `_dorossi_readline_watched`，不再是裸的
    `wait_for(readline)`——後者醒來那一瞬間就會把回合當成閒置砍掉。"""
    import ast
    with open(db.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for name in ("_dorossi_via_claude_code", "_dorossi_via_codex"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
        src = ast.unparse(fn)
        assert "_dorossi_readline_watched(proc.stdout" in src, name
        assert "wait_for(proc.stdout.readline()" not in src, name
        assert "clock.now()" in src, f"{name} 的硬上限要用扣掉休眠的時鐘"
        assert "time.monotonic()" not in src, (
            f"{name} 還有地方直接讀 monotonic——睡著的時間會被算進那一個看門狗")


def test_a_wake_is_detected_and_triggers_the_catch_up(monkeypatch):
    class _Stop(Exception):
        """停住那個不會自己結束的迴圈（它沒有 `except`，所以一般例外就夠）。"""

    woke = []
    monkeypatch.setattr(b, "_WAKE_TICK_SEC", 0.001)
    monkeypatch.setattr(b, "dorossi_elapsed_with_gap",
                        lambda m, w, expected: (expected, 120.0))

    def fake_on_wake(slept):
        woke.append(slept)
        raise _Stop()

    monkeypatch.setattr(b, "_dorossi_on_wake", fake_on_wake)
    with pytest.raises(_Stop):
        # 有上限：偵測壞掉時這一支要變紅，不是掛住（這個迴圈本身不會自己結束）。
        asyncio.run(asyncio.wait_for(b._dorossi_wake_watch_loop(), 5))
    assert woke == [120.0]


def test_waking_up_resumes_the_scan_and_the_catch_up(monkeypatch):
    scheduled = []

    def fake_schedule(coro, *, label=""):
        scheduled.append(label)
        coro.close()

    monkeypatch.setattr(b, "_schedule_coro", fake_schedule)
    monkeypatch.setattr(b, "_dorossi_event", lambda *_a, **_k: None)
    monkeypatch.setattr(db, "DOROSSI_SUSPEND_SEEN", {"count": 0, "seconds": 0.0})
    b._dorossi_on_wake(300.0)
    assert scheduled == ["dorossi-autoresume-wake", "dorossi-after-wake"]


def test_the_wake_watch_is_a_supervised_background_loop():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "_ensure_background_tasks_alive")
    assert "_dorossi_wake_watch_loop" in ast.unparse(fn)
    for handler in ("on_ready", "on_resumed"):
        body = ast.unparse(next(n for n in ast.walk(tree)
                                if isinstance(n, ast.AsyncFunctionDef)
                                and n.name == handler))
        assert "_dorossi_after_reconnect" in body, handler


# ---------------------------------------------------------------------------
# 八、排隊的列跑完才拿掉；平台斷了就放回去
# ---------------------------------------------------------------------------

@pytest.fixture
def qfile(monkeypatch, tmp_path):
    monkeypatch.setattr(b, "DOROSSI_QUEUE_FILE", tmp_path / "q.ndjson")
    monkeypatch.setattr(b, "DOROSSI_FAILED_QUEUE_FILE", tmp_path / "qf.ndjson")
    monkeypatch.setattr(b, "_dorossi_queue_live", set())
    b._dorossi_queue_write([{"id": "r1", "uid": UID, "sid": "s1", "prompt": "q",
                             "status": "queued"}])
    b._dorossi_queue_live.add("r1")
    return tmp_path


def test_a_row_is_removed_only_after_its_turn(qfile):
    seen = []

    async def turn():
        seen.append([(r["status"], r["tries"]) for r in b._dorossi_queue_read()])

    b._dorossi_queue_start("r1")
    asyncio.run(b._dorossi_run_queued_row("r1", turn))
    assert seen == [[("running", 1)]]
    assert b._dorossi_queue_read() == [] and b._dorossi_queue_live == set()


def test_a_platform_failure_puts_the_row_back(qfile, capsys):
    async def turn():
        raise aiohttp.ClientConnectionError("down")

    b._dorossi_queue_start("r1")
    asyncio.run(b._dorossi_run_queued_row("r1", turn))   # 不往上拋
    rows = b._dorossi_queue_read()
    assert [(r["id"], r["status"], r["tries"]) for r in rows] == [("r1", "queued", 1)]
    assert "r1" not in b._dorossi_queue_live, "放回去的列要讓重連的還原看得到"
    assert "putting it back" in capsys.readouterr().err


def test_any_other_failure_consumes_the_row_and_propagates(qfile):
    async def turn():
        raise ValueError("backend exploded")

    with pytest.raises(ValueError):
        asyncio.run(b._dorossi_run_queued_row("r1", turn))
    assert b._dorossi_queue_read() == []


def test_a_cancelled_turn_leaves_its_row_for_the_next_start(qfile):
    async def turn():
        await asyncio.Event().wait()

    async def scenario():
        b._dorossi_queue_start("r1")
        task = asyncio.ensure_future(b._dorossi_run_queued_row("r1", turn))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert [r["status"] for r in b._dorossi_queue_read()] == ["running"]


def test_the_reconnect_restore_only_takes_rows_this_process_is_not_holding(
        qfile, monkeypatch):
    b._dorossi_queue_write([
        {"id": "r1", "uid": UID, "sid": "s1", "prompt": "活著的", "status": "queued"},
        {"id": "r2", "uid": UID, "sid": "s2", "prompt": "放回去的", "status": "queued"}])
    groups = []

    async def fake_group(uid, sid, rows):
        return None

    def recording_group(uid, sid, rows):
        groups.append((sid, [r["id"] for r in rows]))
        return fake_group(uid, sid, rows)

    def fake_schedule(coro, *, label=""):
        coro.close()

    monkeypatch.setattr(b, "_schedule_coro", fake_schedule)
    monkeypatch.setattr(b, "_dorossi_restore_queue_group", recording_group)
    b._dorossi_restore_queued_turns_again()
    assert groups == [("s2", ["r2"])]
    assert b._dorossi_queue_live == {"r1", "r2"}, "排定之後就是這個行程手上的了"
    b._dorossi_restore_queued_turns_again()
    assert groups == [("s2", ["r2"])], "第二次重連不能把同一列再排一次"


def test_a_queued_ask_keeps_its_row_while_it_runs(monkeypatch, tmp_path):
    """走真的 `mcmd_dorossi`：排第二的那一題輪到時，它那一列標成 running，跑完才拿掉。"""
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", tmp_path / "s.json")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "e.ndjson")
    monkeypatch.setattr(b, "DOROSSI_QUEUE_FILE", tmp_path / "q.ndjson")
    monkeypatch.setattr(b, "DOROSSI_FAILED_QUEUE_FILE", tmp_path / "qf.ndjson")
    monkeypatch.setattr(b, "_dorossi_state_lock", asyncio.Lock())
    for name in ("_dorossi_session_locks", "_dorossi_session_lock_refs",
                 "_dorossi_waiters", "_dorossi_loops"):
        monkeypatch.setattr(b, name, {})
    monkeypatch.setattr(b, "_dorossi_queue_live", set())
    monkeypatch.setattr(b, "_dorossi_turns", [])
    during = []
    gate = {}

    class _Ph:
        async def edit(self, content=None, **_kw):
            return None

    async def fake_reply(_message, content=None, **_kw):
        return _Ph()

    async def fake_turn(message, prompt, placeholder, uid, sid, **_kw):
        during.append((prompt, [(r.get("status"), r.get("tries"))
                                for r in b._dorossi_queue_read()]))
        if prompt == "第一題":
            await gate["open"].wait()

    monkeypatch.setattr(b, "safe_reply", fake_reply)
    monkeypatch.setattr(b, "_dorossi_process_turn", fake_turn)
    msg = types.SimpleNamespace(id=1, author=types.SimpleNamespace(id=b.DOROSSI_USER_ID),
                                channel=types.SimpleNamespace(id=0))

    async def scenario():
        gate["open"] = asyncio.Event()
        first = asyncio.ensure_future(b.mcmd_dorossi(msg, "第一題"))
        for _ in range(20):
            await asyncio.sleep(0)
        second = asyncio.ensure_future(b.mcmd_dorossi(msg, "第二題"))
        for _ in range(20):
            await asyncio.sleep(0)
        gate["open"].set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)

    asyncio.run(scenario())
    assert during == [("第一題", []), ("第二題", [("running", 1)])]
    assert b._dorossi_queue_read() == []


# ---------------------------------------------------------------------------
# 九、送不出去的答案停進 outbox，連線回來再送
# ---------------------------------------------------------------------------

@pytest.fixture
def outbox(monkeypatch):
    env = types.SimpleNamespace(state={UID: {"active": "s1", "sessions": {
        "s1": {"label": "主專案"}}}})

    async def fake_rmw(mutate):
        return mutate(env.state)

    monkeypatch.setattr(b, "_dorossi_state_rmw", fake_rmw)
    monkeypatch.setattr(b, "_dorossi_load_state", lambda: env.state)
    monkeypatch.setattr(b, "_dorossi_event", lambda *_a, **_k: None)
    monkeypatch.setattr(b, "_dorossi_outbox_flush_busy", False)
    return env


def _box(env):
    return env.state[UID]["sessions"]["s1"].get("outbox")


def test_an_undeliverable_answer_is_parked_not_lost(outbox, monkeypatch):
    async def down(*_a, **_k):
        raise aiohttp.ClientConnectionError("down")

    monkeypatch.setattr(b, "_dorossi_reply_final", down)
    msg = types.SimpleNamespace(id=77, channel=types.SimpleNamespace(id=55))
    asyncio.run(b._dorossi_deliver_answer(msg, None, UID, "s1", ["第一段", "第二段"]))
    box = _box(outbox)
    assert len(box) == 1 and box[0]["chunks"] == ["第一段", "第二段"]
    assert (box[0]["channel_id"], box[0]["message_id"]) == (55, 77)


def test_a_delivery_failure_that_is_not_the_network_still_raises(outbox, monkeypatch):
    async def boom(*_a, **_k):
        raise ValueError("x")

    monkeypatch.setattr(b, "_dorossi_reply_final", boom)
    msg = types.SimpleNamespace(id=77, channel=types.SimpleNamespace(id=55))
    with pytest.raises(ValueError):
        asyncio.run(b._dorossi_deliver_answer(msg, None, UID, "s1", ["x"]))
    assert _box(outbox) is None


class _SendChannel:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail

    async def send(self, content=None, **_kw):
        if self.fail is not None:
            raise self.fail
        self.sent.append(content)


def _park(env, **over):
    entry = {"ts": 1.0, "chunks": ["答案"], "channel_id": 55, "message_id": 77}
    entry.update(over)
    env.state[UID]["sessions"]["s1"]["outbox"] = [entry]


def _flush_env(monkeypatch, channel, invoker_id):
    notice = _SendChannel()

    async def fake_channel(_cid):
        return channel

    async def fake_resolve(_channel, _mid, *, label=""):
        return object(), types.SimpleNamespace(id=invoker_id), False

    monkeypatch.setattr(b, "_dorossi_restore_channel", fake_channel)
    monkeypatch.setattr(b, "_resolve_trigger_message", fake_resolve)
    monkeypatch.setattr(b, "client", types.SimpleNamespace(get_channel=lambda _cid: notice))
    return notice


def test_the_outbox_is_sent_back_after_the_reconnect(outbox, monkeypatch):
    _park(outbox)
    channel = _SendChannel()
    _flush_env(monkeypatch, channel, b.DOROSSI_USER_ID)
    asyncio.run(b._dorossi_outbox_flush())
    assert channel.sent[0].startswith("📬〔s1〕") and channel.sent[1:] == ["答案"]
    assert _box(outbox) is None, "送出去之後要從 outbox 拿掉"


def test_the_outbox_waits_for_the_next_reconnect_if_the_platform_drops_again(
        outbox, monkeypatch):
    _park(outbox)
    _flush_env(monkeypatch, _SendChannel(fail=aiohttp.ClientConnectionError("down")),
               b.DOROSSI_USER_ID)
    asyncio.run(b._dorossi_outbox_flush())
    assert len(_box(outbox)) == 1, "又斷了就留著，下次連線回來再送"


def test_the_outbox_is_not_sent_where_the_owner_did_not_ask(outbox, monkeypatch):
    """磁碟上的頻道 id 不能自己決定 bot 往哪裡發話：查回來的發起人不是擁有者就不送，
    並在設定檔的頻道講一聲（不含內容）。"""
    _park(outbox)
    channel = _SendChannel()
    notice = _flush_env(monkeypatch, channel, STRANGER)
    asyncio.run(b._dorossi_outbox_flush())
    assert channel.sent == [] and _box(outbox) is None
    assert len(notice.sent) == 1 and "〔s1〕" in notice.sent[0]
    assert "答案" not in notice.sent[0]


def test_a_single_turn_answer_goes_to_the_outbox_when_the_platform_is_down(
        outbox, monkeypatch):
    """走真的 `_dorossi_run_turn`：答案已經存進工作階段，送不出去就停著，不當成失敗。"""
    import test_dorossi_turn as tt   # noqa: PLC0415  沿用那邊的替身組

    sent_diag = []

    async def down(*_a, **_k):
        raise aiohttp.ClientConnectionError("down")

    async def fake_reply(_message, content=None, **_kw):
        sent_diag.append(content)

    async def cc(prompt, session_id, **_kw):
        return "答案", "cc-new", {}

    async def ensure_live(_m, placeholder, initial):
        return tt._FakeLive(placeholder, prime=initial)

    tt._FakeLive.sink, tt._FakeLive.order = [], []
    outbox.state[UID]["sessions"]["s1"].update(cc_session_id="cc-old",
                                               last_used=time.time())
    monkeypatch.setattr(b, "_dorossi_reply_final", down)
    monkeypatch.setattr(b, "safe_reply", fake_reply)
    monkeypatch.setattr(b, "_dorossi_via_claude_code", cc)
    monkeypatch.setattr(b, "_dorossi_ensure_live", ensure_live)
    monkeypatch.setattr(b, "_dorossi_send_images", lambda *_a: asyncio.sleep(0, 0))
    monkeypatch.setattr(b, "_dorossi_maybe_compact_single_turn",
                        lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(b, "_dorossi_backend_sem", tt._Sem([]))
    monkeypatch.setattr(b, "_dorossi_resolve_cc_workdir", lambda s, _u, _i: "wd")
    monkeypatch.setattr(b, "_shutil", types.SimpleNamespace(which=lambda _n: "x"))
    monkeypatch.setattr(b, "DOROSSI_BACKEND", "claude_code")
    monkeypatch.setattr(b, "DOROSSI_SELF_JUDGE_ENABLED", False)
    monkeypatch.setattr(b, "_dorossi_turns", [])
    channel = types.SimpleNamespace(id=55, typing=lambda: tt._Typing([]))
    msg = types.SimpleNamespace(id=77, channel=channel,
                                author=types.SimpleNamespace(id=b.DOROSSI_USER_ID))
    asyncio.run(b._dorossi_process_turn(msg, "問題", None, UID, "s1"))
    assert _box(outbox)[0]["chunks"] == ["答案"]
    assert sent_diag == [], "答案停進 outbox 不是失敗，不附診斷"


def test_the_reconnect_catch_up_does_both_things(monkeypatch):
    calls = []
    monkeypatch.setattr(b, "_dorossi_restore_queued_turns_again",
                        lambda: calls.append("restore"))

    async def flush():
        calls.append("flush")

    monkeypatch.setattr(b, "_dorossi_outbox_flush", flush)
    asyncio.run(b._dorossi_after_reconnect("on_resumed"))
    assert calls == ["restore", "flush"]


# ---------------------------------------------------------------------------
# 十、`/dorossi running` 看得到誰在等網路
# ---------------------------------------------------------------------------

def test_the_running_view_shows_network_waits(monkeypatch):
    monkeypatch.setattr(b, "_dorossi_turns", [])
    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "_dorossi_waiters", {})
    now = time.time()
    turn = b._dorossi_turn_register(UID, "s1", "q")
    turn.phase, turn.backend = "offline", "claude_code"
    turn.offline_since, turn.offline_target = now - 125, "claude_code"
    st = b._DorossiLoopState(UID, "s2")
    st.round_no, st.offline_since, st.offline_target = 4, now - 30, "platform"
    b._dorossi_loops[(UID, "s2")] = st
    owner = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))
    text = b._dorossi_running_report({}, owner, now=now)
    s1 = next(line for line in text.splitlines() if "`s1`" in line)
    s2 = next(line for line in text.splitlines() if "`s2`" in line)
    assert "等網路恢復（後端 A，已等 2m 5s）" in s1, s1
    assert "等網路恢復（對話平台，已等 30s）" in s2, s2
    assert "正在等網路恢復：2 件" in text, text
    assert "執行中 1／" in text, "等網路的單輪回合照樣佔著一個空位"
