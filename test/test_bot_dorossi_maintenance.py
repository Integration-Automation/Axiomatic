"""`/config reload` 與 Dorossi 的維護指令（`/dorossi status`、`retry`、`compact`）。

這四支處理函式在此之前沒有任何測試直接呼叫過。釘的是：擁有者閘在讀任何狀態之前就擋下
非擁有者；`/config reload` 把新值套進九個模組全域並回報角色有沒有變；`retry`／`compact`
握著那個 session 的鎖跑、跑完鎖與參照數收乾淨，鎖被別人握著時回忙線並把剛加上去的參照數
還回去，有自走迴圈在跑則連鎖都不碰。

Dorossi 的落地狀態全部導到 `tmp_path`，記憶體裡的鎖、佇列、迴圈表換成新的一份。

    py -3 -m pytest test/test_bot_dorossi_maintenance.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import discord_bot as b                              # noqa: E402
import dorossi_backend as db                         # noqa: E402

OWNER = b.OWNER_USER_ID
STRANGER = 7                     # 任何不是擁有者的 id
DUID = str(b.DOROSSI_USER_ID)


class _Channel:
    """`message.channel` 的替身。"""

    def __init__(self) -> None:
        self.id = 4242
        self.sent: list = []

    async def send(self, content=None, **_kw):
        self.sent.append(content)


def _msg(uid: int = STRANGER):
    return types.SimpleNamespace(author=types.SimpleNamespace(id=uid),
                                 channel=_Channel(), guild=None)


def _run(coro, timeout: float = 10.0):
    """跑一段 async 測試本體；有牆鐘上限——回歸時要變紅，不能掛住。"""
    return asyncio.run(asyncio.wait_for(coro, timeout))


@pytest.fixture
def replies(monkeypatch):
    """換掉 `safe_reply`，記下每一則回覆的文字與關鍵字參數（embed 在 kwargs 裡）。"""
    sent: list = []

    async def _reply(_message, content=None, **kwargs):
        sent.append(types.SimpleNamespace(content=content, **kwargs))

    monkeypatch.setattr(b, "safe_reply", _reply)
    return sent


# --------------------------------------------------------------------------
# /config reload（cmd_config_reload）
# --------------------------------------------------------------------------
_RELOADED_GLOBALS = ("BOT_CONFIG", "USER_ROLES", "DAILY_HEALTH_REPORT", "DASHBOARD_CONFIG",
                     "DOROSSI_MODEL_CHECK", "MIN_FREE_DISK_GB", "ALERT_USER_ID",
                     "GUI_LAUNCH_WHITELIST", "GUI_LAUNCH_ALIASES")


@pytest.fixture
def reload_env(monkeypatch):
    """重載會改九個模組全域；先各自登記一次，測試結束時 monkeypatch 會全部換回來。"""
    for name in _RELOADED_GLOBALS:
        monkeypatch.setattr(b, name, getattr(b, name))
    state = types.SimpleNamespace(loads=0, config=json.loads(json.dumps(b.BOT_CONFIG)))

    def load():
        state.loads += 1
        return state.config

    monkeypatch.setattr(b, "load_bot_config", load)
    return state


def test_config_reload_is_owner_only_and_touches_nothing_for_a_stranger(reload_env,
                                                                         replies):
    before = {name: getattr(b, name) for name in _RELOADED_GLOBALS}
    _run(b.cmd_config_reload(_msg()))
    assert replies[-1].content == "此指令僅限擁有者使用。"
    assert reload_env.loads == 0
    assert {name: getattr(b, name) for name in _RELOADED_GLOBALS} == before


def test_config_reload_applies_the_new_values_and_reports_a_role_change(reload_env,
                                                                         replies):
    reload_env.config["alert_user_id"] = 123
    reload_env.config["min_free_disk_gb"] = 9.5
    reload_env.config["gui_control"]["launch_whitelist"] = ["notepad.exe"]
    _run(b.cmd_config_reload(_msg(OWNER)))
    assert (b.ALERT_USER_ID, b.MIN_FREE_DISK_GB) == (123, 9.5)
    assert b.GUI_LAUNCH_WHITELIST == ["notepad.exe"]
    assert b.BOT_CONFIG is reload_env.config
    assert replies[-1].content.endswith("roles changed=`false`。"), replies[-1].content
    reload_env.config = json.loads(json.dumps(reload_env.config))
    reload_env.config["user_roles"]["admin_user_ids"] = [42]
    _run(b.cmd_config_reload(_msg(OWNER)))
    assert b.USER_ROLES["admin_user_ids"] == [42]
    assert replies[-1].content.endswith("roles changed=`true`。"), replies[-1].content


# --------------------------------------------------------------------------
# /dorossi status｜retry｜compact
# --------------------------------------------------------------------------
@pytest.fixture
def dorossi(monkeypatch, tmp_path):
    """Dorossi 的落地狀態全部指到 `tmp_path`，記憶體裡的鎖、佇列、迴圈表換成新的一份。"""
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", tmp_path / "dorossi_session.json")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "dorossi_events.ndjson")
    monkeypatch.setattr(b, "DOROSSI_QUEUE_FILE", tmp_path / "dorossi_queue.ndjson")
    monkeypatch.setattr(b, "DOROSSI_FAILED_QUEUE_FILE",
                        tmp_path / "dorossi_queue_failed.ndjson")
    monkeypatch.setattr(b, "_dorossi_state_lock", asyncio.Lock())
    for name in ("_dorossi_session_locks", "_dorossi_session_lock_refs",
                 "_dorossi_waiters", "_dorossi_loops"):
        monkeypatch.setattr(b, name, {})
    state = types.SimpleNamespace(turns=[], store=tmp_path / "dorossi_session.json",
                                  events=tmp_path / "dorossi_events.ndjson")

    async def fake_turn(message, prompt, placeholder, uid, sid, **kwargs):
        key = b._dorossi_session_key(uid, sid)
        state.turns.append((prompt, placeholder, uid, sid, kwargs,
                            b._dorossi_session_locks[key].locked()))

    monkeypatch.setattr(b, "_dorossi_process_turn", fake_turn)
    return state


def _seed_sessions(count: int = 2, **slot_fields) -> dict:
    """用真正的 primitives 建 slot 並存檔（手寫 JSON 會先被遷移改掉形狀）。"""
    state: dict = {}
    record = db._dorossi_user_record(state, DUID)
    for i in range(count):
        sid = db._dorossi_new_session(record)
        record["sessions"][sid]["last_used"] = 1_000.0 + i
        record["sessions"][sid].update(slot_fields)
    db._dorossi_save_state(state)
    return state


def _slot(sid: str) -> dict:
    return db._dorossi_load_state()[DUID]["sessions"][sid]


def test_dorossi_status_is_owner_only(dorossi, replies):
    """非擁有者只拿到拒絕：不讀狀態、也不留一筆 `status` 事件。"""
    _run(b.mcmd_status(_msg(STRANGER), ""))
    assert replies[-1].content == "此指令僅限擁有者使用。"
    assert not dorossi.events.exists()
    _run(b.mcmd_status(_msg(b.DOROSSI_USER_ID), ""))
    assert dorossi.events.exists(), "正面對照：擁有者那一次要記一筆事件"


def test_dorossi_status_counts_sessions_loops_and_live_waiters(dorossi, replies):
    """排隊數只算沒被取消的；調校顯示的是**使用中**那個 slot 的值。"""
    _seed_sessions(3, tune_effort="high")
    b._dorossi_loops[(DUID, "s1")] = types.SimpleNamespace(injections=[],
                                                           usage_waiting=False)
    b._dorossi_waiters[(DUID, "s2")] = [types.SimpleNamespace(canceled=False),
                                        types.SimpleNamespace(canceled=True)]
    _run(b.mcmd_status(_msg(b.DOROSSI_USER_ID), ""))
    reply = replies[-1].content
    assert "- active: `s3` / provider `claude`" in reply, reply
    assert "- tuning: effort `high`, model `default`" in reply
    assert "- sessions: `3`, running loops `1`" in reply
    assert "- queued turns: `1`," in reply
    assert "\n**sessions**\n- `s1`: loop inject=0/" in reply


def test_dorossi_retry_reruns_the_last_prompt_under_the_session_lock(dorossi, replies):
    """重跑的是那個 slot 存著的上一句，握著那個 session 的鎖；跑完之後鎖與參照數要收乾淨，
    否則那個 session 之後每一輪都會被當成「正在處理」。"""
    _seed_sessions(2, last_user_prompt="再畫一次")
    _run(b.mcmd_retry(_msg(b.DOROSSI_USER_ID), "last s1"))
    (prompt, placeholder, uid, sid, kwargs, locked), = dorossi.turns
    assert (prompt, placeholder, uid, sid, locked) == ("再畫一次", None, DUID, "s1", True)
    assert kwargs == {"effort": None, "model_tier": None}
    assert b._dorossi_session_locks == {} and b._dorossi_session_lock_refs == {}
    assert replies == []


@pytest.mark.parametrize("args, expected", [
    ("again s9", "找不到 session `s9`。"),
    ("twice", "用法：`/dorossi retry last [session]`"),
    ("", "`s2` 沒有可重試的上一個 prompt。"),
])
def test_dorossi_retry_refuses_what_it_cannot_retry(dorossi, replies, args, expected):
    _seed_sessions(2)
    _run(b.mcmd_retry(_msg(b.DOROSSI_USER_ID), args))
    assert replies[-1].content == expected and dorossi.turns == []


def test_dorossi_retry_is_owner_only(dorossi, replies):
    """重跑會以擁有者的對話再跑一輪後端——非擁有者在讀任何狀態之前就要被擋下。"""
    _seed_sessions(1, last_user_prompt="hi")
    _run(b.mcmd_retry(_msg(STRANGER), "last"))
    assert replies[-1].content == "此指令僅限擁有者使用。" and dorossi.turns == []


@pytest.mark.parametrize("handler, busy", [
    (b.mcmd_retry, "這個 session 目前正在處理或排隊，請稍後再 retry。"),
    (b.mcmd_compact, "這個 session 目前正在處理或排隊，請稍後再 compact。"),
])
def test_a_busy_session_is_refused_and_its_lock_ref_is_given_back(dorossi, replies,
                                                                  handler, busy):
    """鎖被別人握著時回忙線——而且剛剛為了看它而加上去的那一個參照數要還回去，否則握著的
    人放手之後鎖永遠回收不了。有自走迴圈在跑則直接拒絕，連鎖都不碰。"""
    _seed_sessions(1, last_user_prompt="hi")
    key = (DUID, "s1")

    async def scenario():
        lock = b._dorossi_acquire_session_lock(key)
        await lock.acquire()
        try:
            await handler(_msg(b.DOROSSI_USER_ID), "")
            refs_while_held = dict(b._dorossi_session_lock_refs)
        finally:
            lock.release()
            b._dorossi_release_session_lock(key)
        return refs_while_held

    assert _run(scenario()) == {key: 1}
    assert replies[-1].content == busy and dorossi.turns == []
    b._dorossi_loops[key] = types.SimpleNamespace()
    _run(handler(_msg(b.DOROSSI_USER_ID), ""))
    assert replies[-1].content == "這個 session 正在跑自走任務，請先 abort 或等它停下。"
    assert b._dorossi_session_lock_refs == {}


def test_dorossi_compact_runs_the_compact_turn_unless_the_backend_cannot(dorossi, replies,
                                                                         monkeypatch):
    _seed_sessions(2)
    _run(b.mcmd_compact(_msg(STRANGER), ""))
    monkeypatch.setattr(db, "DOROSSI_BACKEND", "api")   # 判準在後端模組（`dorossi_session_backend`）
    _run(b.mcmd_compact(_msg(b.DOROSSI_USER_ID), ""))
    assert [r.content for r in replies] == ["此指令僅限擁有者使用。",
                                           "目前後端不支援手動 compact。"]
    monkeypatch.setattr(db, "DOROSSI_BACKEND", "claude_code")
    _run(b.mcmd_compact(_msg(b.DOROSSI_USER_ID), "s9"))
    assert replies[-1].content == "找不到 session `s9`。" and dorossi.turns == []
    _run(b.mcmd_compact(_msg(b.DOROSSI_USER_ID), "s1"))
    (prompt, _placeholder, _uid, sid, _kwargs, locked), = dorossi.turns
    assert (prompt, sid, locked) == ("/compact", "s1", True)
    assert b._dorossi_session_lock_refs == {}
