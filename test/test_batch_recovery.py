"""批次的自動復原（2026-09-22）：bot 重啟、`/run at`、斷網、主機待命。

每一條原本的失敗形態都是**安靜的**——批次停在那裡、沒有人監督、也沒有任何訊息：

1. **bot 重啟後**，`on_ready` 只收養 pid，`_watch_for_fallback` 看到
   `_webrunner_proc is None` 就返回：那一輪批次從此沒有人監督。
2. **`/run`（含 `/run at` 到點）**先送說明訊息、後掛監督者：聊天平台連不上的那一刻
   送訊息丟例外，監督者永遠不會被建起來。
3. **`/run at` 的行程內等待**用牆鐘；主機睡過了到點時刻，醒來時會晚好幾小時才突然
   啟動——與重啟還原那條路的「超過 10 分鐘就算錯過」不一致。
4. **斷網**讓每一輪都秒崩或零產出，兩道放棄閘在斷網四十分鐘內就會響，網路回來之後
   沒有任何東西會把批次重新跑起來。獨立監督者（`start_webrunner.py`）同一個問題。
5. **待命**期間 bot 沒有持有電源要求，監督者跟著被暫停。

這一檔逐條驗。時鐘與等待的做法沿用 `test_webrunner_supervisor`（只換掉 bot 命名空間
裡的 `time`／`asyncio.sleep`，事件迴圈用的仍是真的時鐘），監督者的替身也直接借用它的
夾具。
"""
from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _connectivity  # noqa: E402
import _power_request as pr  # noqa: E402
import discord_bot as b  # noqa: E402
from test_webrunner_supervisor import (  # noqa: E402,F401  (夾具要在模組命名空間裡)
    RC_BLOCKED, RC_ZERO, _said, _run, _start, sup_env)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Channel:
    def __init__(self, cid=4242, order=None, raise_on_send=False):
        self.id = cid
        self.sent: list[str] = []
        self.order = order
        self.raise_on_send = raise_on_send

    async def send(self, content=None, **_kwargs):
        if self.order is not None:
            self.order.append("send")
        if self.raise_on_send:
            raise RuntimeError("聊天平台連不上")
        self.sent.append(content)


class _FakeBackend:
    def __init__(self):
        self.acquires: list[str] = []
        self.releases = 0

    def acquire(self, reason):
        self.acquires.append(reason)
        return "power-request"

    def release(self):
        self.releases += 1


def _fake_power(monkeypatch):
    """換一個乾淨的計數器（假後端），並打開 bot 的開關。回傳假後端。"""
    backend = _FakeBackend()
    monkeypatch.setattr(pr, "_MANAGER", pr._Manager(backend_factory=lambda: backend))
    monkeypatch.setattr(b, "KEEP_BOT_AWAKE", True)
    return backend


def _run_and_settle(env):
    """跑完監督者；它若把「等網路」交給了新的 task，也等那個 task 跑完。"""
    async def _body():
        b._webrunner_fallback_task = asyncio.current_task()
        await b._watch_for_fallback(env.channel)
        handed = b._webrunner_fallback_task
        if handed is not None and handed is not asyncio.current_task():
            await handed
        return handed

    return asyncio.run(_body())


def _probe_script(monkeypatch, answers, *, on_probe=None):
    """`_batch_network_is_down` 依序回 `answers`，用完之後一律「網路在」。"""
    calls: list[bool] = []

    async def _down():
        if on_probe is not None:
            on_probe()
        answer = answers.pop(0) if answers else False
        calls.append(answer)
        return answer

    monkeypatch.setattr(b, "_batch_network_is_down", _down)
    return calls


def _fake_runs(monkeypatch):
    runs: list = []

    async def _run_fake(channel):
        runs.append(types.SimpleNamespace(
            channel=channel, task=b._webrunner_fallback_task,
            marker=b.NETWORK_RESUME_FILE.exists()))

    monkeypatch.setattr(b, "_do_webrunner_run", _run_fake)
    return runs


@pytest.fixture
def marker(monkeypatch, tmp_path):
    path = tmp_path / "network_resume.json"
    monkeypatch.setattr(b, "NETWORK_RESUME_FILE", path)
    monkeypatch.setattr(b, "_webrunner_liveness", lambda: (
        b._webrunner_proc is not None and b._webrunner_proc.poll() is None, True))
    return path


# ===========================================================================
# 一、`_connectivity`：「明確斷網」的判準
# ===========================================================================

def test_any_reachable_address_means_online_and_the_socket_is_closed():
    closed: list[str] = []
    tried: list = []

    class _Sock:
        def close(self):
            closed.append("x")

    def _connect(address, timeout):
        tried.append(address)
        if address[0] == "a":
            raise OSError("unreachable")
        return _Sock()

    assert _connectivity.is_online([("a", 1), ("b", 2), ("c", 3)],
                                   connect=_connect) is True
    assert tried == [("a", 1), ("b", 2)], "連上一個就該停"
    assert closed == ["x"]


def test_only_when_every_address_fails_is_it_offline():
    def _connect(address, timeout):
        raise OSError("down")

    assert _connectivity.is_online([("a", 1), ("b", 2)], connect=_connect) is False


def test_nothing_to_probe_or_a_non_network_error_leans_online():
    """誤判成斷網會讓一個真的壞掉的批次一直等下去；誤判成有網路只是回到原本的行為。"""
    assert _connectivity.is_online([]) is True

    def _broken(address, timeout):
        raise TypeError("替身寫錯")

    assert _connectivity.is_online([("a", 1)], connect=_broken) is True


def test_the_default_probes_are_literal_ip_addresses():
    """不經過 DNS：斷網時 DNS 查詢本身可能要卡好幾秒才失敗。"""
    assert len(_connectivity.PROBE_ADDRESSES) >= 2
    for host, port in _connectivity.PROBE_ADDRESSES:
        ipaddress.ip_address(host)
        assert 0 < port < 65536


def test_the_synchronous_wait_has_no_cap_and_returns_when_online():
    answers = [False] * 50 + [True]
    naps: list[float] = []
    waits: list[float] = []
    _connectivity.wait_until_online(
        poll_sec=5.0, probe=lambda: answers.pop(0), sleep=naps.append,
        on_wait=waits.append)
    assert len(naps) == 50 and all(n == 5.0 for n in naps), (
        "等了 50 輪還在等，而且每輪都照設定的間隔——沒有被次數上限切掉")
    assert len(waits) == 50


def test_ctrl_c_ends_the_synchronous_wait():
    def _interrupt(_seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _connectivity.wait_until_online(probe=lambda: False, sleep=_interrupt)


# ===========================================================================
# 二、bot 的電源要求
# ===========================================================================

def test_bot_power_hold_follows_the_config_switch(monkeypatch):
    backend = _fake_power(monkeypatch)
    held = b.bot_power_hold("batch supervision")
    assert pr.status()["count"] == 1
    assert backend.acquires == ["axiomatic bot: batch supervision"]
    held.release()
    assert pr.status()["count"] == 0 and backend.releases == 1

    monkeypatch.setattr(b, "KEEP_BOT_AWAKE", False)
    with b.bot_power_hold("off") as inert:
        assert inert.active is None
        assert pr.status()["count"] == 0
    assert backend.acquires == ["axiomatic bot: batch supervision"]


def test_the_supervisor_holds_power_while_watching_and_lets_go_after(
        sup_env, monkeypatch):
    backend = _fake_power(monkeypatch)
    proc = _start(sup_env, rc=0, alive=10.0)
    counts: list[int] = []
    original = proc.poll

    def _poll():
        counts.append(pr.status()["count"])
        return original()

    proc.poll = _poll
    _run(sup_env)
    assert counts and all(c == 1 for c in counts), (
        f"監督期間電源要求的份數：{counts}——應該一直是 1")
    assert pr.status()["count"] == 0 and backend.releases == 1


def test_cancelling_the_supervisor_releases_the_power_request(monkeypatch):
    """`/stop` 與 `/run` 都是用 cancel 結束監督者的；那一刻要把電源要求放掉。"""
    backend = _fake_power(monkeypatch)
    monkeypatch.setattr(b, "_webrunner_stop_requested", False)
    monkeypatch.setattr(b, "_webrunner_variant", "selenium")
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    monkeypatch.setattr(b, "_webrunner_proc",
                        types.SimpleNamespace(poll=lambda: None, returncode=None))

    async def _body():
        task = asyncio.create_task(b._watch_for_fallback(_Channel()))
        for _ in range(5):
            await asyncio.sleep(0)
        assert pr.status()["count"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_body())
    assert pr.status()["count"] == 0 and backend.releases == 1


# ===========================================================================
# 三、斷網：停下來等，網路回來就接續
# ===========================================================================

@pytest.mark.parametrize("rc", [1, RC_ZERO])
def test_a_failure_while_offline_parks_the_batch_and_resumes_when_online(
        sup_env, monkeypatch, marker, rc):
    backend = _fake_power(monkeypatch)
    seen: list[tuple[bool, int]] = []
    calls = _probe_script(
        monkeypatch, [True, True, True, False],
        on_probe=lambda: seen.append((marker.exists(), pr.status()["count"])))
    runs = _fake_runs(monkeypatch)
    _start(sup_env, rc=rc, alive=3.0)
    _run_and_settle(sup_env)

    assert calls == [True, True, True, False], calls
    assert sup_env.spawns == [], (
        "斷網時不該照退避規則重生——那只會再撞一次同一個斷網")
    assert len(runs) == 1, "網路回來之後沒有接續"
    assert runs[0].channel is sup_env.channel
    assert runs[0].task is None, (
        "交棒給 `/run` 那條路之前沒有把自己從 `_webrunner_fallback_task` 摘下來——"
        "它會取消並等待自己")
    assert runs[0].marker is False, "開始接續時落地檔還在"
    assert not marker.exists()
    # 第一次探測在寫落地檔之前（那時還不知道要停），之後每一次都在等待中。
    assert [m for m, _ in seen] == [False, True, True, True], seen
    assert all(count >= 1 for _, count in seen[1:]), (
        f"等網路的期間沒有持有電源要求：{seen}")
    assert pr.status()["count"] == 0
    assert backend.releases == len(backend.acquires) == 2, (
        "監督者一份、等網路的 task 一份，各自放掉")
    said = _said(sup_env)
    assert "網路中斷" in said and "網路已恢復" in said, said
    assert "放棄" not in said
    assert sum(sup_env.sleeps) >= 2 * b.NETWORK_RESUME_POLL_SEC - 1


def test_the_marker_records_when_and_where(sup_env, monkeypatch, marker):
    record: list = []
    _probe_script(monkeypatch, [True, True],
                  on_probe=lambda: record.append(
                      marker.read_text(encoding="utf-8")
                      if marker.exists() else None))
    _fake_runs(monkeypatch)
    before = time.time()
    _start(sup_env, rc=1, alive=1.0)
    _run_and_settle(sup_env)
    data = json.loads(record[1])
    assert data["channel_id"] == sup_env.channel.id
    assert before - 1 <= data["since"] <= time.time() + 1


def test_offline_failures_never_reach_the_give_up_gate(sup_env, monkeypatch, marker):
    """兩次在線的秒崩之後、第三次是斷網：放棄閘（門檻 3）不能響。"""
    _probe_script(monkeypatch, [False, False, True])
    runs = _fake_runs(monkeypatch)
    _start(sup_env, rc=1, alive=0.0)
    sup_env.script = [(1, 0.0), (1, 0.0)]
    _run_and_settle(sup_env)
    assert "放棄重啟" not in _said(sup_env)
    assert len(runs) == 1


def test_control_the_same_failures_online_do_give_up(sup_env, monkeypatch, marker):
    """正面對照：少了它，一個「永遠不放棄」的實作也會讓上一支全綠。"""
    _probe_script(monkeypatch, [False, False, False])
    runs = _fake_runs(monkeypatch)
    _start(sup_env, rc=1, alive=0.0)
    sup_env.script = [(1, 0.0), (1, 0.0)]
    _run_and_settle(sup_env)
    assert "放棄重啟" in _said(sup_env)
    assert runs == [] and not marker.exists()


def test_a_je_crash_while_offline_does_not_switch_to_the_backup_mode(
        sup_env, monkeypatch, marker):
    """斷網時 je 秒崩不代表 je 壞了——不該換成備援模式。"""
    _probe_script(monkeypatch, [True])
    runs = _fake_runs(monkeypatch)
    _start(sup_env, rc=1, alive=5.0, variant="je")
    _run_and_settle(sup_env)
    assert sup_env.spawns == [], f"換成了 {sup_env.spawns}"
    assert "備援" not in _said(sup_env)
    assert len(runs) == 1


def test_a_blocked_generation_stops_even_while_offline(sup_env, monkeypatch, marker):
    """需要人處理排在斷網之前：那是批次在頁面上**看到**的，網路當時是通的。"""
    calls = _probe_script(monkeypatch, [True])
    runs = _fake_runs(monkeypatch)
    _start(sup_env, rc=RC_BLOCKED, alive=100.0)
    _run_and_settle(sup_env)
    assert calls == [], "需要人處理的結束碼還去探網路"
    assert runs == [] and not marker.exists()


def test_stop_ends_the_wait_and_nothing_restarts(sup_env, monkeypatch, marker):
    _probe_script(monkeypatch, [True] * 1000)
    runs = _fake_runs(monkeypatch)

    def _stop_after_a_while(_delay):
        if len(sup_env.sleeps) >= 150:
            b._webrunner_stop_requested = True

    sup_env.on_sleep = _stop_after_a_while
    _start(sup_env, rc=1, alive=1.0)
    _run_and_settle(sup_env)
    assert runs == [], "`/stop` 之後還接續了"
    assert marker.exists(), "監督者自己不刪落地檔——那是 `/stop` 的事（下一支）"


def test_a_stop_already_requested_ends_the_wait_without_probing(monkeypatch):
    """`/stop` 可能落在探測進行中（探測丟在執行緒裡）；下一圈一開始就要看到它，
    不能再探一次、再睡一整個間隔。"""
    calls = _probe_script(monkeypatch, [True] * 5)
    monkeypatch.setattr(b, "_webrunner_stop_requested", True)
    assert asyncio.run(b._wait_until_online()) is False
    assert calls == []


def test_the_stop_command_deletes_the_marker_and_says_so(monkeypatch, marker):
    marker.write_text(json.dumps({"since": time.time(), "channel_id": 1}),
                      encoding="utf-8")
    replies: list[str] = []

    async def _reply(_message, content=None, **_kwargs):
        replies.append(content)

    async def _terminate(_proc, _pid):
        return []

    async def _acquire(label, timeout):
        return True

    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "find_launcher_pids", lambda: ([], True))
    monkeypatch.setattr(b, "_terminate_all_webrunner_instances", _terminate)
    monkeypatch.setattr(b, "_acquire_chrome_slot", _acquire)
    monkeypatch.setattr(b, "_release_chrome_slot", lambda: None)
    monkeypatch.setattr(b, "_clear_pid", lambda: None)
    monkeypatch.setattr(b, "SINGLE_IMAGE_REQUEST_FILE",
                        marker.parent / "req.json")
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    monkeypatch.setattr(b, "_webrunner_oneshot_reaper_task", None)
    monkeypatch.setattr(b, "_webrunner_proc", None)
    monkeypatch.setattr(b, "_webrunner_pid", None)
    message = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))
    asyncio.run(b.cmd_stop(message))
    assert not marker.exists()
    assert any("網路恢復後自動接續" in (text or "") for text in replies), replies


def _restore_env(monkeypatch, channel):
    monkeypatch.setattr(b, "_network_resume_restored", False)
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    monkeypatch.setattr(b, "_webrunner_stop_requested", False)
    monkeypatch.setattr(b, "_webrunner_proc", None)
    monkeypatch.setattr(b, "_webrunner_pid", None)
    monkeypatch.setattr(b, "_webrunner_oneshot", False)
    monkeypatch.setattr(b, "KEEP_BOT_AWAKE", False)
    resolved: list = []

    async def _resolve(channel_id, *, label):
        resolved.append(channel_id)
        return channel

    monkeypatch.setattr(b, "_resolve_origin_channel", _resolve)
    return resolved


def _restore_and_settle():
    async def _body():
        await b._restore_network_resume_once()
        task = b._webrunner_fallback_task
        if task is not None:
            await asyncio.wait_for(task, timeout=10)
        return task

    return asyncio.run(_body())


def test_after_a_restart_the_parked_batch_resumes_when_online(monkeypatch, marker):
    channel = _Channel(4242)
    resolved = _restore_env(monkeypatch, channel)
    _probe_script(monkeypatch, [False])
    runs = _fake_runs(monkeypatch)
    marker.write_text(json.dumps({"since": time.time() - 3600, "channel_id": 4242}),
                      encoding="utf-8")
    task = _restore_and_settle()
    assert task is not None, "沒有接回等待"
    assert resolved == [4242]
    assert len(runs) == 1 and runs[0].channel is channel
    assert not marker.exists()
    assert any("網路已恢復" in text for text in channel.sent), channel.sent


def test_a_queue_file_that_is_not_utf8_is_explained_after_the_network_returns(
        monkeypatch, marker, tmp_path):
    """等網路的 task 是被交出去跑的，`/run` 前置檢查的 `_QueueFileNotUtf8` 只有它接得到；
    接不到的話使用者只會看到一句泛用的「沒有成功」，不知道是哪一份清單、怎麼修。"""
    channel = _Channel(4242)
    _restore_env(monkeypatch, channel)
    _probe_script(monkeypatch, [False])
    error = b._QueueFileNotUtf8(
        tmp_path / "todo_character1.md",
        UnicodeDecodeError("utf-8", b"\xb3\x44", 0, 1, "invalid start byte"))

    async def _refuse(_channel):
        raise error

    monkeypatch.setattr(b, "_do_webrunner_run", _refuse)
    monkeypatch.setattr(b, "_alert_prefix", lambda: "")
    marker.write_text(json.dumps({"since": time.time(), "channel_id": 4242}),
                      encoding="utf-8")
    _restore_and_settle()
    said = "\n".join(channel.sent)
    assert "自動接續沒有開始" in said and "UTF-8" in said, said
    assert "角色1 佇列" in said, "沒說是哪一份清單"
    assert "todo_character1.md" not in said and str(tmp_path) not in said


def test_a_damaged_marker_still_resumes_rather_than_dropping_the_run(
        monkeypatch, marker):
    """讀不懂內容不是停止指令——只有明確的停止能結束這個等待。"""
    channel = _Channel(b.CHANNEL_ID)
    resolved = _restore_env(monkeypatch, channel)
    _probe_script(monkeypatch, [False])
    runs = _fake_runs(monkeypatch)
    marker.write_text("{半截", encoding="utf-8")
    _restore_and_settle()
    assert resolved == [b.CHANNEL_ID]
    assert len(runs) == 1


_NOW = 1_800_000_000.0


@pytest.mark.parametrize("body, expected", [
    ('{"since": %r, "channel_id": 123}' % (_NOW - 100), (_NOW - 100, 123)),
    ('{"since": %r, "channel_id": 123}' % (_NOW + 30), (_NOW + 30, 123)),
    ('{"since": %r, "channel_id": 123}' % (_NOW + 3600), None),
    ('{"since": 0, "channel_id": 123}', None),
    ('{"since": -5, "channel_id": 123}', None),
    ('{"since": NaN, "channel_id": 123}', None),
    ('{"since": 1e400, "channel_id": 123}', None),
    ('{"since": %r, "channel_id": true}' % (_NOW - 100), None),
    ('{"since": %r, "channel_id": 0}' % (_NOW - 100), None),
    ('{"since": %r, "channel_id": "123"}' % (_NOW - 100), None),
], ids=["valid", "clock-skew-allowed", "future", "zero", "negative", "nan", "inf",
        "bool-channel", "zero-channel", "string-channel"])
def test_a_marker_field_out_of_range_falls_back_to_now_and_the_configured_channel(
        marker, body, expected):
    """起點與頻道的範圍檢查在整個套件裡從來沒有成立過（2026-09-22 分支覆蓋率）。起點會拿來
    量等了多久；一個未來或非有限的起點讓那段時間變成負的或 nan。壞掉的欄位不是停止指令，
    所以退回「現在＋設定的頻道」照樣接續，不是丟掉這一筆。時鐘誤差 60 秒以內照收。"""
    marker.write_text(body, encoding="utf-8")
    got = b._read_network_resume(_NOW)
    assert got == (*(expected or (_NOW, b.CHANNEL_ID)), {})


@pytest.mark.parametrize("channel_id", [0, -5])
def test_a_marker_with_an_origin_keeps_it_even_without_a_positive_channel(marker, channel_id):
    """沒有設定頻道的部署（設定頻道是 0）寫下的落地檔頻道就是 0。帶著來源時那只是退路：
    判成「格式壞了」會連來源一起丟掉，重啟後「網路已恢復」就沒有地方講。沒有來源的 0
    照舊是壞的（上一支的 zero-channel）。"""
    marker.write_text(
        '{"since": %r, "channel_id": %d, "platform": "stubplat", "platform_chat_id": "777"}'
        % (_NOW - 100, channel_id), encoding="utf-8")
    assert b._read_network_resume(_NOW) == (
        _NOW - 100, channel_id, {"platform": "stubplat", "platform_chat_id": "777"})


def test_a_marker_that_is_not_an_object_falls_back_instead_of_raising(marker):
    """`[1, 2]` 過得了 `json.loads`，但它不是物件——漏接的話這支丟例外，而不是照
    「讀不懂就用預設接續」那條規則走。"""
    marker.write_text("[1, 2]", encoding="utf-8")
    assert b._read_network_resume(_NOW) == (_NOW, b.CHANNEL_ID, {})


def test_a_batch_parked_from_another_platform_reports_back_there_after_a_restart(
        monkeypatch, marker):
    """斷網停下的批次是從別的平台下的：落地檔帶著來源，重啟後「網路已恢復」講在那個
    對話；找不回來（平台沒開）才退回設定頻道——批次通知本來就以它為預設去處。"""
    import _chat_platform as cp
    delivered: list = []

    class _Platform(cp.ChatTransport):
        name = "stubplat"

        @property
        def capabilities(self):
            return cp.PlatformCapabilities()

        async def run(self):
            return None

        async def deliver(self, channel, content=None, **kwargs):
            delivered.append(content)

        def conversation_for(self, platform_chat_id):
            return cp.ChatConversation(self, platform_chat_id, uid=-5, is_direct=True,
                                       is_command_chat=False)

    conv = _Platform().conversation_for("777")
    assert b._save_network_resume(conv.id, time.time() - 3600, cp.origin_of(conv))
    stored = json.loads(marker.read_text(encoding="utf-8"))
    assert stored["channel_id"] == b.CHANNEL_ID and stored["platform_chat_id"] == "777"

    primary = _Channel(b.CHANNEL_ID)
    resolved = _restore_env(monkeypatch, primary)
    monkeypatch.setattr(b, "_chat_transports", [_Platform()])
    _probe_script(monkeypatch, [False])
    runs = _fake_runs(monkeypatch)
    _restore_and_settle()
    assert resolved == [], "找得回對話卻還去找設定頻道"
    assert len(runs) == 1 and isinstance(runs[0].channel, cp.ChatConversation)
    assert any("網路已恢復" in text for text in delivered), delivered
    assert primary.sent == []

    # 平台沒開：退回設定頻道，批次照樣接續。
    assert b._save_network_resume(conv.id, time.time() - 3600, cp.origin_of(conv))
    resolved = _restore_env(monkeypatch, primary)
    monkeypatch.setattr(b, "_chat_transports", [])
    _probe_script(monkeypatch, [False])
    runs = _fake_runs(monkeypatch)
    _restore_and_settle()
    assert resolved == [b.CHANNEL_ID] and len(runs) == 1 and runs[0].channel is primary


def test_parking_a_batch_from_another_platform_records_its_conversation(monkeypatch, marker):
    """停下來的那一刻就要把來源寫進落地檔。`_clear_pid` 換成記錄用的替身——真的那一支會
    刪掉**正式批次**的存活訊號；停止旗標設起來，讓它在排定接續之前就返回。"""
    import _chat_platform as cp

    class _Platform(cp.ChatTransport):
        name = "stubplat"

        @property
        def capabilities(self):
            return cp.PlatformCapabilities()

        async def run(self):
            return None

        async def deliver(self, channel, content=None, **kwargs):
            return None

    cleared: list = []

    async def _notify(*_a, **_k):
        return None

    monkeypatch.setattr(b, "_clear_pid", lambda: cleared.append(1))
    monkeypatch.setattr(b, "_supervisor_notify", _notify)
    monkeypatch.setattr(b, "_webrunner_stop_requested", True)
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    for name in ("_webrunner_proc", "_webrunner_pid", "_webrunner_variant"):
        monkeypatch.setattr(b, name, None)
    conv = cp.ChatConversation(_Platform(), "777", uid=-5, is_direct=True,
                               is_command_chat=False)
    asyncio.run(b._park_batch_for_network(conv))
    stored = json.loads(marker.read_text(encoding="utf-8"))
    assert (stored["platform"], stored["platform_chat_id"]) == ("stubplat", "777"), stored
    assert cleared == [1] and b._webrunner_fallback_task is None


def test_a_marker_is_discarded_when_a_run_is_already_alive(monkeypatch, marker):
    channel = _Channel()
    _restore_env(monkeypatch, channel)
    runs = _fake_runs(monkeypatch)
    monkeypatch.setattr(b, "_webrunner_liveness", lambda: (True, True))
    marker.write_text(json.dumps({"since": time.time(), "channel_id": 1}),
                      encoding="utf-8")
    task = _restore_and_settle()
    assert task is None and runs == []
    assert not marker.exists()


@pytest.mark.parametrize("own_oneshot, expect_resume", [(False, False), (True, True)])
def test_a_run_that_started_during_the_wait_is_left_alone(
        monkeypatch, marker, own_oneshot, expect_resume):
    """等網路的期間，別人用 `/launcher start` 起了一個批次：網路回來時不能再走 `/run`
    （那一步會先把正在跑的那個殺掉）。自己的 `/gen image` 單張伺服器不算——`/run`
    本來就會接手它。"""
    channel = _Channel()
    _restore_env(monkeypatch, channel)
    _probe_script(monkeypatch, [False])
    runs = _fake_runs(monkeypatch)
    answers = iter([(False, True)])
    monkeypatch.setattr(b, "_webrunner_liveness",
                        lambda: next(answers, (True, True)))
    monkeypatch.setattr(b, "_webrunner_oneshot", own_oneshot)
    marker.write_text(json.dumps({"since": time.time(), "channel_id": 1}),
                      encoding="utf-8")
    _restore_and_settle()
    assert bool(runs) is expect_resume, runs
    assert not marker.exists()


def test_the_restore_happens_once_per_process(monkeypatch, marker):
    channel = _Channel()
    _restore_env(monkeypatch, channel)
    _probe_script(monkeypatch, [])
    runs = _fake_runs(monkeypatch)
    _restore_and_settle()                      # 沒有檔案：什麼都不做，但記下「做過了」
    marker.write_text(json.dumps({"since": time.time(), "channel_id": 1}),
                      encoding="utf-8")
    assert _restore_and_settle() is None
    assert runs == [] and marker.exists()


def test_a_new_run_supersedes_the_wait_and_deletes_the_marker(monkeypatch, marker):
    """`/run` 取代了等待；留著落地檔的話，bot 下次重啟會把它接回來。"""
    env = _run_env(monkeypatch, marker)
    marker.write_text(json.dumps({"since": time.time(), "channel_id": 1}),
                      encoding="utf-8")
    asyncio.run(b._do_webrunner_run(_Channel(order=env.order)))
    assert not marker.exists()
    assert env.spawns == ["je"]


# ===========================================================================
# 四、`/run`：先掛監督、再說話
# ===========================================================================

def _run_env(monkeypatch, marker_path):
    env = types.SimpleNamespace(order=[], spawns=[], pumps=0, watched=[])

    async def _scan():
        return [], True

    async def _acquire(label, timeout):
        return True

    async def _terminate(_proc, _pid):
        return []

    def _spawn(variant, *, single_image_server=False):
        env.spawns.append(variant)
        b._webrunner_proc = types.SimpleNamespace(poll=lambda: None, pid=77,
                                                  returncode=None)
        return True, "已啟動背景產圖程式"

    def _watch(channel, **kwargs):
        env.order.append("watch")
        env.watched.append((channel, kwargs))
        return asyncio.sleep(0)

    async def _pump():
        env.pumps += 1

    monkeypatch.setattr(b, "_launcher_scan", _scan)
    monkeypatch.setattr(b, "_compute_run_plan",
                        lambda: ([("p", "c1", "c2", "u")], {}))
    monkeypatch.setattr(b, "_free_disk_gb", lambda: None)
    monkeypatch.setattr(b, "_acquire_chrome_slot", _acquire)
    monkeypatch.setattr(b, "_release_chrome_slot", lambda: None)
    monkeypatch.setattr(b, "_terminate_all_webrunner_instances", _terminate)
    monkeypatch.setattr(b, "_clear_pid", lambda: None)
    monkeypatch.setattr(b, "_spawn_webrunner", _spawn)
    monkeypatch.setattr(b, "_watch_for_fallback", _watch)
    monkeypatch.setattr(b, "_generate_pump", _pump)
    monkeypatch.setattr(b, "_get_batch_label", lambda: "")
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    monkeypatch.setattr(b, "_webrunner_oneshot_reaper_task", None)
    monkeypatch.setattr(b, "_webrunner_stop_requested", False)
    monkeypatch.setattr(b, "_webrunner_proc", None)
    monkeypatch.setattr(b, "_webrunner_pid", None)
    monkeypatch.setattr(b, "_webrunner_oneshot", False)
    return env


def test_run_attaches_supervision_before_saying_anything(monkeypatch, marker):
    env = _run_env(monkeypatch, marker)
    channel = _Channel(order=env.order)
    asyncio.run(b._do_webrunner_run(channel))
    assert env.order[:2] == ["watch", "send"], env.order
    assert "已啟動" in channel.sent[0]
    assert env.pumps == 1


def test_a_failed_notice_leaves_the_batch_supervised(monkeypatch, marker):
    """聊天平台連不上的那一刻按下 `/run at`：批次要照樣有人監督，例外不得往上冒。"""
    env = _run_env(monkeypatch, marker)
    channel = _Channel(order=env.order, raise_on_send=True)

    async def _body():
        await b._do_webrunner_run(channel)          # 不得丟例外
        return b._webrunner_fallback_task

    task = asyncio.run(_body())
    assert env.order == ["watch", "send"], env.order
    assert task is not None and env.watched, "送訊息失敗之後沒有監督者"
    assert env.pumps == 1, "送訊息失敗之後 `/gen image` 佇列沒有被推一次"


def test_a_scheduled_run_whose_notice_fails_is_not_reported_as_failed(
        monkeypatch, marker, tmp_path):
    """排程那條路把 `_do_webrunner_run` 的例外讀成「沒有順利開始、排程已取消」——
    而批次其實已經在跑了。送不出去的說明訊息不能再讓它這樣誤判。"""
    env = _run_env(monkeypatch, marker)
    monkeypatch.setattr(b, "SCHEDULED_RUN_FILE", tmp_path / "scheduled_run.json")
    stderr: list[str] = []

    class _FlakyChannel(_Channel):
        async def send(self, content=None, **kwargs):
            if "已啟動" in str(content):
                raise RuntimeError("聊天平台連不上")
            self.sent.append(content)

    channel = _FlakyChannel()

    async def _body():
        task = asyncio.create_task(
            b._scheduled_run_loop(channel, time.time() - 1))
        b._scheduled_run_task = task
        await asyncio.wait_for(task, timeout=10)

    monkeypatch.setattr(b, "_scheduled_run_task", None)
    monkeypatch.setattr(b, "traceback",
                        types.SimpleNamespace(print_exc=lambda: stderr.append("tb")))
    asyncio.run(_body())
    assert env.spawns == ["je"] and env.watched
    assert not any("沒有順利開始" in text for text in channel.sent), channel.sent


# ===========================================================================
# 五、`/run at`：睡過頭就不要晚好幾小時才突然啟動
# ===========================================================================

def _sleepy_clock(monkeypatch, jump_sec):
    """假牆鐘：第一次 `asyncio.sleep` 時跳 `jump_sec`（主機在那段時間睡著了）。"""
    now = [1_000_000.0]
    fake_time = types.SimpleNamespace(
        time=lambda: now[0], monotonic=time.monotonic,
        strftime=time.strftime, localtime=time.localtime)

    class _AsyncioProxy:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay):
            now[0] += jump_sec if jump_sec else delay

    monkeypatch.setattr(b, "time", fake_time)
    monkeypatch.setattr(b, "asyncio", _AsyncioProxy())
    return now


def _drive_schedule(monkeypatch, tmp_path, *, due_in, jump_sec):
    now = _sleepy_clock(monkeypatch, jump_sec)
    started: list = []

    async def _run_fake(channel):
        started.append(channel)

    monkeypatch.setattr(b, "_do_webrunner_run", _run_fake)
    monkeypatch.setattr(b, "_get_batch_label", lambda: "")
    schedule_file = tmp_path / "scheduled_run.json"
    schedule_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(b, "SCHEDULED_RUN_FILE", schedule_file)
    monkeypatch.setattr(b, "_scheduled_run_task", None)
    monkeypatch.setattr(b, "_scheduled_run_ts", None)
    channel = _Channel()

    async def _body():
        task = asyncio.create_task(
            b._scheduled_run_loop(channel, now[0] + due_in))
        b._scheduled_run_task = task
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(_body())
    return channel, started, schedule_file


def test_a_run_the_host_slept_through_is_reported_not_started(monkeypatch, tmp_path):
    channel, started, schedule_file = _drive_schedule(
        monkeypatch, tmp_path, due_in=60, jump_sec=5 * 3600)
    assert started == [], "睡過頭五小時還照樣啟動了"
    assert len(channel.sent) == 1 and "錯過" in channel.sent[0], channel.sent
    assert not schedule_file.exists()


def test_a_normal_wait_still_starts_the_run(monkeypatch, tmp_path):
    """正面對照：沒有睡過頭（每次 sleep 只過了它要求的秒數）就照常啟動。"""
    channel, started, _ = _drive_schedule(
        monkeypatch, tmp_path, due_in=95, jump_sec=0)
    assert started == [channel]
    assert not any("錯過" in text for text in channel.sent)


@pytest.mark.parametrize("late_by, expect_start", [
    (-60, True),      # cap 以內
    (+60, False),     # 超過 cap
])
def test_the_in_process_wait_uses_the_same_cap_as_the_restore(
        monkeypatch, tmp_path, late_by, expect_start):
    jump = 30 + b.SCHEDULED_RUN_CATCHUP_SEC + late_by
    _channel, started, _ = _drive_schedule(
        monkeypatch, tmp_path, due_in=30, jump_sec=jump)
    assert bool(started) is expect_start, (late_by, started)


# ===========================================================================
# 六、bot 重啟後把正在跑的批次接回監督
# ===========================================================================

def _reattach_env(monkeypatch, *, launchers=([], True), inspected=("je", 1000.0),
                  adopted="default", pid=4242):
    channel = _Channel(order=[])
    env = types.SimpleNamespace(channel=channel, watched=[], spawns=[],
                                closed=[], order=channel.order)
    if adopted == "default":
        adopted = types.SimpleNamespace(
            pid=pid, adopted=True, returncode=None, poll=lambda: None,
            _close=lambda: env.closed.append(1))
    env.adopted = adopted

    async def _scan():
        return launchers

    async def _resolve(channel_id, *, label):
        return channel

    def _watch(ch, **kwargs):
        env.order.append("watch")
        env.watched.append((ch, kwargs))
        return asyncio.sleep(0)

    def _spawn(*args, **kwargs):
        env.spawns.append(args)
        return False, "不該被叫到"

    monkeypatch.setattr(b, "_launcher_scan", _scan)
    monkeypatch.setattr(b, "_inspect_adopted_webrunner", lambda _pid: inspected)
    monkeypatch.setattr(b, "_open_adopted_webrunner",
                        lambda _pid, *, started_mono=None: adopted)
    monkeypatch.setattr(b, "_resolve_origin_channel", _resolve)
    monkeypatch.setattr(b, "_watch_for_fallback", _watch)
    monkeypatch.setattr(b, "_spawn_webrunner", _spawn)
    monkeypatch.setattr(b, "_batch_reattach_done", False)
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    monkeypatch.setattr(b, "_webrunner_stop_requested", False)
    monkeypatch.setattr(b, "_webrunner_proc", None)
    monkeypatch.setattr(b, "_webrunner_pid", pid)
    monkeypatch.setattr(b, "_webrunner_variant", None)
    monkeypatch.setattr(b, "_webrunner_oneshot", False)
    return env


def _reattach(pid=4242):
    async def _body():
        await b._reattach_adopted_batch_once(pid)
        task = b._webrunner_fallback_task
        if task is not None:
            await task
        return task

    return asyncio.run(_body())


def test_an_adopted_batch_gets_its_supervision_back(monkeypatch):
    env = _reattach_env(monkeypatch)
    task = _reattach()
    assert task is not None, "收養之後沒有掛監督者"
    assert env.watched == [(env.channel, {"adopted": True})], env.watched
    assert b._webrunner_proc is env.adopted
    assert b._webrunner_variant == "je"
    assert env.spawns == [], "接回監督不應該 spawn 任何東西"
    assert env.order[:2] == ["watch", "send"], (
        f"要先掛監督、再說話：{env.order}")
    assert any("接回" in text for text in env.channel.sent)


@pytest.mark.parametrize("launchers", [([7], True), ([], False)])
def test_no_reattach_while_a_standalone_supervisor_may_be_watching(
        monkeypatch, launchers):
    """兩套監督者盯同一個批次會互相重生、互相終止——掃不完整也一樣讓位。"""
    env = _reattach_env(monkeypatch, launchers=launchers)
    assert _reattach() is None
    assert env.watched == [] and b._webrunner_proc is None


@pytest.mark.parametrize("inspected", [(None, 1000.0), (None, None)])
def test_no_reattach_when_the_process_is_not_a_batch(monkeypatch, inspected):
    """單張伺服器、被重用的 pid、查不到——都不能當成批次來重生。"""
    env = _reattach_env(monkeypatch, inspected=inspected)
    assert _reattach() is None
    assert env.watched == []


def test_no_reattach_when_the_process_cannot_be_held(monkeypatch):
    env = _reattach_env(monkeypatch, adopted=None)
    assert _reattach() is None
    assert env.watched == [] and b._webrunner_proc is None


def test_a_stop_that_lands_during_the_reattach_wins(monkeypatch):
    env = _reattach_env(monkeypatch)

    def _inspect(_pid):
        b._webrunner_stop_requested = True
        return "je", 1000.0

    monkeypatch.setattr(b, "_inspect_adopted_webrunner", _inspect)
    assert _reattach() is None
    assert env.watched == [] and env.closed == [1], "握住的 handle 沒有放掉"


def test_the_reattach_happens_once_per_process(monkeypatch):
    env = _reattach_env(monkeypatch)
    _reattach()
    monkeypatch.setattr(b, "_webrunner_proc", None)
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    _reattach()
    assert len(env.watched) == 1


def test_an_adopted_je_batch_is_not_switched_to_the_backup_mode(sup_env):
    """收養來的批次早就過了 je 的啟動視窗；崩潰要照一般規則重生同一個變體。"""
    _start(sup_env, rc=1, alive=10.0, variant="je")
    sup_env.script = []

    async def _body():
        b._webrunner_fallback_task = asyncio.current_task()
        await b._watch_for_fallback(sup_env.channel, adopted=True)

    asyncio.run(_body())
    assert sup_env.spawns == ["je"], sup_env.spawns
    assert "備援" not in _said(sup_env)


def test_an_adopted_batch_is_timed_from_when_it_really_started(sup_env):
    """跑了一小時的批次在 bot 重啟後十秒死掉，不是一次秒崩。"""
    proc = _start(sup_env, rc=1, alive=10.0)
    proc.started_mono = sup_env.clock.now - 3600
    _run(sup_env)
    said = _said(sup_env)
    assert "rapid-fail" not in said, said
    assert "執行 3610s" in said, said


def test_control_a_process_without_a_start_time_is_timed_from_now(sup_env):
    _start(sup_env, rc=1, alive=10.0)
    _run(sup_env)
    assert "rapid-fail 1/" in _said(sup_env)


class _FakePsutilProcess:
    cmdline_value: list = []
    raise_error = None

    def __init__(self, pid):
        if _FakePsutilProcess.raise_error is not None:
            raise _FakePsutilProcess.raise_error
        self.pid = pid

    def cmdline(self):
        return list(_FakePsutilProcess.cmdline_value)

    def create_time(self):
        return 1234.5


@pytest.mark.parametrize("cmdline, expected", [
    (["python.exe", "-u", r"D:\x\axiomatic\webrunner_je_only.py"], "je"),
    (["python.exe", "-u", "D:/x/axiomatic/webrunner_novelai.py"], "selenium"),
    (["python.exe", "-u", "D:/x/axiomatic/webrunner_je_only.py",
      b.SINGLE_IMAGE_SERVER_FLAG], None),
    (["python.exe", "-m", "black", "D:/x/axiomatic/webrunner_je_only.py"], None),
    (["notepad.exe", "todo_prompt.md"], None),
])
def test_the_adopted_process_is_recognised_by_what_it_runs(
        monkeypatch, cmdline, expected):
    _FakePsutilProcess.cmdline_value = cmdline
    _FakePsutilProcess.raise_error = None
    monkeypatch.setitem(sys.modules, "psutil",
                        types.SimpleNamespace(Process=_FakePsutilProcess))
    variant, created = b._inspect_adopted_webrunner(4242)
    assert variant == expected
    assert created == 1234.5


def test_an_unreadable_process_is_not_treated_as_a_batch(monkeypatch):
    _FakePsutilProcess.raise_error = PermissionError("denied")
    monkeypatch.setitem(sys.modules, "psutil",
                        types.SimpleNamespace(Process=_FakePsutilProcess))
    try:
        assert b._inspect_adopted_webrunner(4242) == (None, None)
    finally:
        _FakePsutilProcess.raise_error = None


def _sleeper(seconds: float, code: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c",
         f"import sys, time; time.sleep({seconds}); sys.exit({code})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.skipif(os.name != "nt", reason="握住 handle 問結束碼是 Windows 的做法")
def test_an_adopted_process_reports_its_real_exit_code():
    """握住 handle 之後，行程結束了也問得到真正的結束碼——監督者靠它分辨
    收工（0）、零產出（3）、需要人（4）與崩潰。"""
    child = _sleeper(1.0, 7)
    try:
        adopted = b._open_adopted_webrunner(child.pid, started_mono=12.5)
        assert adopted is not None
        assert adopted.pid == child.pid and adopted.started_mono == 12.5
        assert adopted.poll() is None
        with pytest.raises(subprocess.TimeoutExpired):
            adopted.wait(0.01)
        assert adopted.wait(30) == 7
        assert adopted.poll() == 7 and adopted.returncode == 7
    finally:
        child.wait(30)


@pytest.mark.skipif(os.name != "nt", reason="握住 handle 問結束碼是 Windows 的做法")
def test_the_handle_stays_open_after_the_process_ends():
    """結束之後也不關 handle：另一條執行緒可能正等在同一個 handle 上，而關掉的
    handle 數值會被作業系統重新配給別的物件。只有丟掉物件（或放棄收養）才關。"""
    child = _sleeper(0.2, 3)
    try:
        adopted = b._open_adopted_webrunner(child.pid)
        assert adopted is not None
        assert adopted.wait(30) == 3
        assert adopted._handle is not None, "行程一結束就關了 handle"
        assert adopted.poll() == 3 and adopted.wait(0) == 3
        adopted._close()
        assert adopted._handle is None
        assert adopted.poll() == 3, "放棄收養之後仍要回報已知的結束碼"
    finally:
        child.wait(30)


def test_an_abandoned_adoption_never_reads_as_alive():
    adopted = b._AdoptedWebrunner(4242, None, None)
    assert adopted.poll() == b._ADOPTED_RC_UNKNOWN


@pytest.mark.skipif(os.name != "nt", reason="握住 handle 問結束碼是 Windows 的做法")
def test_an_adopted_process_can_be_terminated():
    child = _sleeper(3.0, 0)
    try:
        adopted = b._open_adopted_webrunner(child.pid)
        assert adopted is not None
        adopted.terminate()
        assert adopted.wait(30) == 1
    finally:
        child.wait(30)


@pytest.mark.skipif(os.name != "nt", reason="握住 handle 問結束碼是 Windows 的做法")
def test_a_process_that_is_already_gone_is_not_adopted():
    child = _sleeper(0.0, 0)
    child.wait(30)
    pid = child.pid
    del child
    assert b._open_adopted_webrunner(pid) is None


@pytest.mark.skipif(os.name != "nt", reason="握住 handle 問結束碼是 Windows 的做法")
def test_a_process_that_has_exited_but_is_still_held_is_not_adopted():
    """上面那支在 `del child` 之後 `Popen` 的 handle 已經關了，`OpenProcess` 當場失敗，
    「開得到 handle、但行程早就結束了」那一條從來沒跑過——而那才是常態：還有別人（例如
    它的父行程）握著 handle 時，結束的行程物件還在，開得到。收養一個已經結束的行程，
    監督者會把它的結束碼當成「剛剛結束」處理，對一個早就停了的批次做重生與通知。"""
    child = _sleeper(0.0, 5)
    try:
        child.wait(30)                      # 結束了，但 `child` 還握著它的 handle
        assert b._open_adopted_webrunner(child.pid) is None
    finally:
        child.wait(30)


# ===========================================================================
# 七、獨立監督者（`start_webrunner.py`）：斷網不算進放棄閘
# ===========================================================================

def _load_launcher():
    path = REPO_ROOT / "start_webrunner.py"
    spec = importlib.util.spec_from_file_location("start_webrunner_recovery",
                                                  str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sw = _load_launcher()


def _launcher_env(monkeypatch, *, rcs, network, wait_raises=None):
    """`rcs`：每一輪子行程的結束碼；`network`：每次失敗時探到的「網路在不在」。"""
    env = types.SimpleNamespace(said=[], waits=0, sleeps=[], spawns=0,
                                probes=0, now=[0.0])

    def _stream_child(cmd, log, *, cwd, on_spawn=None, pump_name=None):
        env.spawns += 1
        if on_spawn is not None:
            on_spawn(types.SimpleNamespace(pid=5000 + env.spawns))
        env.now[0] += 1.0                    # 每一輪都「秒崩」
        return rcs.pop(0)

    def _network_is_up():
        env.probes += 1
        return network.pop(0) if network else True

    def _wait_for_network():
        env.waits += 1
        if wait_raises is not None:
            raise wait_raises
        env.now[0] += 600.0
        return 600.0

    def _sleep(seconds):
        env.sleeps.append(seconds)
        env.now[0] += seconds

    monkeypatch.setattr(sw, "stream_child", _stream_child)
    monkeypatch.setattr(sw, "_network_is_up", _network_is_up)
    monkeypatch.setattr(sw, "_wait_for_network", _wait_for_network)
    monkeypatch.setattr(sw, "_save_pid", lambda pid: None)
    monkeypatch.setattr(sw, "_clear_pid_if_ours", lambda pid: None)
    monkeypatch.setattr(sw, "_live_webrunner_pid", lambda: (None, True))
    monkeypatch.setattr(sw, "_chrome_slot", types.SimpleNamespace(
        acquire=lambda *a, **k: True, release=lambda *a, **k: None))
    monkeypatch.setattr(sw, "time", types.SimpleNamespace(
        monotonic=lambda: env.now[0], sleep=_sleep))
    monkeypatch.setattr(sw, "say",
                        lambda log, message, err=False: env.said.append(message))
    return env


def _supervise(**overrides):
    params = dict(backoff_min=5.0, backoff_max=300.0, healthy_sec=60.0,
                  rapid_threshold_sec=30.0, rapid_giveup=2,
                  zero_progress_giveup=2)
    params.update(overrides)
    return sw._supervise(["python", "webrunner.py"], "selenium", None, **params)


def test_launcher_offline_failures_do_not_count_toward_giving_up(monkeypatch):
    env = _launcher_env(monkeypatch, rcs=[1, 1, 1, 1, 0],
                        network=[False, False, False, False])
    assert _supervise() == 0
    assert env.waits == 4 and env.spawns == 5
    assert not any("giving up" in text for text in env.said), env.said
    assert env.sleeps == [], "斷網時不走退避——網路回來就立刻重生"


def test_launcher_control_online_failures_still_give_up(monkeypatch):
    env = _launcher_env(monkeypatch, rcs=[1, 1, 1, 0], network=[True, True, True])
    assert _supervise() == 1
    assert env.spawns == 2 and env.waits == 0
    assert any("giving up" in text for text in env.said)


def test_launcher_offline_zero_progress_does_not_count(monkeypatch):
    env = _launcher_env(monkeypatch, rcs=[RC_ZERO, RC_ZERO, RC_ZERO, 0],
                        network=[False, False, False])
    assert _supervise(rapid_threshold_sec=0.5) == 0
    assert env.waits == 3
    assert not any("zero images" in text for text in env.said)


def test_launcher_counters_restart_after_a_network_wait(monkeypatch):
    """斷網前那幾次「剛好探到通」的失敗不能留到網路回來之後，一次正常失敗就放棄。"""
    env = _launcher_env(monkeypatch, rcs=[1, 1, 1, 0],
                        network=[True, False, True])
    assert _supervise() == 0, env.said
    assert env.waits == 1


def test_launcher_ctrl_c_during_the_network_wait_stops_cleanly(monkeypatch):
    env = _launcher_env(monkeypatch, rcs=[1], network=[False],
                        wait_raises=KeyboardInterrupt())
    assert _supervise() == 0
    assert any("Ctrl+C" in text for text in env.said)


def test_launcher_blocked_generation_stops_without_probing(monkeypatch):
    env = _launcher_env(monkeypatch, rcs=[RC_BLOCKED], network=[False])
    assert _supervise() == 1
    assert env.probes == 0 and env.waits == 0


def test_launcher_clean_exit_never_probes(monkeypatch):
    env = _launcher_env(monkeypatch, rcs=[0], network=[False])
    assert _supervise() == 0
    assert env.probes == 0
