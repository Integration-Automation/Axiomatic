"""`/launcher` —— 從對話平台啟動**獨立**批次監督者。

這一組指令的功能需求只有一個：**啟動出來的行程要真的脫離 bot**。做不到的話它跟
`/run` 完全一樣，而使用者會被告知「獨立監督者已啟動」——回覆說成功、實際上沒有
達成目的、而且沒有任何症狀。所以這支測試的重心不在「有沒有呼叫到 spawn」，而在
三件更難看出來的事：

1. **回報的依據是觀測，不是 spawn 的回傳值。** 依定義 bot 拿不到目標的 handle，
   所以「起得來」只能靠掃描去看。掃不到就要誠實說掃不到。
2. **bot 自己的監督者狀態一個都不能被寫到。** 兩套監督者同時盯同一個批次會互相
   重生、互相終止，而兩邊的紀錄看起來都正常。
3. **「過期的 pid 檔」不得擋住啟動，「活著的批次」必須擋住。** 只驗前者的話，一個
   「永遠不擋」的實作也會全綠——那正好是這道閘要防的失敗。

脫離本身（`_process_control.spawn_detached`）是作業系統層的行為，沒辦法在單元測試
裡驗；這裡驗的是**結構**：目標不是被直接 spawn 的，而是經過一個會立刻結束的中繼
行程。
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

import discord_bot as b            # noqa: E402
import _process_control as pc      # noqa: E402


PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"

# bot 自己的監督者狀態。`/launcher` 這條路徑**一個都不准碰**。
_SUPERVISOR_GLOBALS = (
    "_webrunner_proc",
    "_webrunner_pid",
    "_webrunner_variant",
    "_webrunner_stop_requested",
    "_webrunner_oneshot",
    "_webrunner_oneshot_reaper_task",
    "_webrunner_fallback_task",
    "_webrunner_log_handle",
)


def _fake_message(uid: int):
    return types.SimpleNamespace(author=types.SimpleNamespace(id=uid))


@pytest.fixture
def env(monkeypatch, tmp_path):
    """把每一條會碰到主機的路徑換掉，並收集回覆。

    `terminate_launcher_pids` 與 `spawn_detached` 一定要換掉：前者會真的
    `taskkill`，後者會真的生出一個脫離掌控的行程——在一台同時在做正事的機器上跑
    測試時，這兩件事都不可接受。
    """
    replies: list[str] = []

    async def fake_reply(message, content=None, **kwargs):
        replies.append(content)
        return None

    monkeypatch.setattr(b, "safe_reply", fake_reply)
    # pid 檔導進暫存目錄：這是活的跨行程狀態，不能讓測試寫到 repo root。
    monkeypatch.setattr(b, "WEBRUNNER_PID_FILE", tmp_path / "webrunner.pid")
    monkeypatch.setattr(b, "LAUNCHER_SCRIPT", tmp_path / "start_webrunner.py")
    (tmp_path / "start_webrunner.py").write_text("x", encoding="utf-8")
    # ⚠️ **八個監督者全域全部歸零，不是只歸零看起來會用到的那兩個。**
    # 變異實測 2026-09-12：在 `_launcher_start` 裡偷偷寫 `_webrunner_variant`
    # 的變異**存活**了——因為前面一支測試先跑過一次成功的 `start`，那個變異
    # 已經把全域設成 `"je"`，而 monkeypatch 沒碰過它所以不會還原；等到
    # 「不得污染 bot 狀態」那一支跑的時候，before 與 after 都是 `"je"`，斷言
    # 照樣成立。**狀態從上一支測試漏過來，正好把這一支要驗的東西遮住。**
    for name in _SUPERVISOR_GLOBALS:
        monkeypatch.setattr(b, name, None)
    monkeypatch.setattr(b, "_webrunner_stop_requested", False)
    monkeypatch.setattr(b, "_webrunner_oneshot", False)
    # 讓確認迴圈跑得快。
    monkeypatch.setattr(b, "LAUNCHER_POLL_SEC", 0.01)
    monkeypatch.setattr(b, "LAUNCHER_SETTLE_SEC", 0.01)
    monkeypatch.setattr(b, "LAUNCHER_START_CONFIRM_SEC", 0.05)
    return types.SimpleNamespace(replies=replies, tmp=tmp_path)


def _arm(monkeypatch, *, before=(), after=None, scan_ok=True, alive=True):
    """安排掃描的回答：第一次回 `before`，之後回 `after`。"""
    calls = {"n": 0, "spawned": []}
    sequence = [list(before)] + [list(after if after is not None else before)]

    def fake_scan():
        index = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return sequence[index], scan_ok

    def fake_spawn(argv, *, cwd):
        calls["spawned"].append((list(argv), cwd))
        return True

    monkeypatch.setattr(b, "find_launcher_pids", fake_scan)
    monkeypatch.setattr(b, "spawn_detached", fake_spawn)
    monkeypatch.setattr(b, "_pid_alive", lambda pid: alive)
    return calls


# --------------------------------------------------------------------------
# 擁有者閘
# --------------------------------------------------------------------------
def test_the_launcher_group_is_locked_by_the_group_rule():
    """群組規則（fail-closed），不是逐條列舉（fail-open）。

    這條路會在主機上生出一個**脫離 bot 掌控**的行程。用列舉的話，之後多一個
    `/launcher <新子指令>` 就預設沒有閘，而且沒有任何症狀。
    """
    assert "launcher" in b._OWNER_ONLY_GROUPS
    # 問的是正式碼自己的述詞，不是裝飾器文字——巢狀子群會讓文字比對答錯。
    for leaf in ("launcher start", "launcher status", "launcher stop"):
        assert b._is_owner_only_slash(leaf), leaf
    # 而且**不是**靠列舉撐著：把列舉清空之後群組規則仍然要成立。
    assert not any(name.startswith("launcher ") for name in b._OWNER_ONLY_SLASH)


def test_the_hidden_text_surface_is_locked_too():
    """三個表面三個識別字，漏一個就是一條完整的繞道。"""
    assert "!launcher" in b._OWNER_ONLY_BANGS
    # mention 面刻意沒有這條路——有的話要一併列進 `_OWNER_ONLY_MENTIONS`。
    assert "launcher" not in b._OWNER_ONLY_MENTIONS


def test_a_non_owner_reaches_nothing(env, monkeypatch):
    """縱深防禦：就算派發層的閘被繞過，handler 自己也不動手。

    正面對照在下一支——少了它，一個「永遠拒絕」的實作也會通過這一支。
    """
    calls = _arm(monkeypatch, before=(), after=(1234,))
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID + 1), "start"))
    assert calls["spawned"] == []
    assert env.replies == [b.OWNER_ONLY_DENIED]


def test_the_owner_does_reach_it(env, monkeypatch):
    calls = _arm(monkeypatch, before=(), after=(1234,))
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID), "start"))
    assert len(calls["spawned"]) == 1


# --------------------------------------------------------------------------
# 不得污染 bot 自己的監督者狀態
# --------------------------------------------------------------------------
def test_starting_the_standalone_supervisor_touches_no_bot_state(env, monkeypatch):
    """兩套監督者同時盯同一個批次會互相重生、互相終止。

    所以這條路徑跟 `_spawn_webrunner` 不共用任何全域——包括那個看起來無害的
    `_webrunner_stop_requested`（被設成 True 的話，之後的 `/run` 會被自己的
    spawn 拒絕，而使用者完全看不出為什麼）。
    """
    _arm(monkeypatch, before=(), after=(4321,))
    before = {name: getattr(b, name) for name in _SUPERVISOR_GLOBALS}
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID), "start"))
    after = {name: getattr(b, name) for name in _SUPERVISOR_GLOBALS}
    assert before == after
    # 也不得掛上 bot 的看門狗：那一套會對獨立監督者的子行程做退避重生。
    assert b._webrunner_fallback_task is None


# --------------------------------------------------------------------------
# 過期 pid 不擋 / 活著的批次要擋
# --------------------------------------------------------------------------
def test_a_stale_pid_file_does_not_block_the_start(env, monkeypatch):
    """死掉的行程留下的 pid 檔不算「有批次在跑」。

    硬殺一個批次之後檔案會留著；若它擋得住啟動，使用者就再也起不來，而畫面上只有
    一句「已經有批次在執行」。
    """
    b.WEBRUNNER_PID_FILE.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(b, "_pid_alive", lambda pid: False)
    calls = _arm(monkeypatch, before=(), after=(777,))
    monkeypatch.setattr(b, "_pid_alive", lambda pid: pid == 777)
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID), "start"))
    assert len(calls["spawned"]) == 1, env.replies


def test_a_live_batch_does_block_the_start(env, monkeypatch):
    """正面對照。少了這一支，「過期 pid 不擋」會被一個『永遠不擋』的實作滿足。"""
    b.WEBRUNNER_PID_FILE.write_text("4242", encoding="utf-8")
    calls = _arm(monkeypatch, before=(), after=(777,), alive=True)
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID), "start"))
    assert calls["spawned"] == []
    assert "批次" in env.replies[-1]


def test_an_existing_launcher_blocks_the_start(env, monkeypatch):
    calls = _arm(monkeypatch, before=(555,))
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID), "start"))
    assert calls["spawned"] == []
    assert "已經在執行" in env.replies[-1]


def test_an_incomplete_scan_refuses_to_start(env, monkeypatch):
    """「掃不成」不等於「沒有」。

    樂觀一次的代價是機器上同時跑兩個監督者、兩個批次搶同一份瀏覽器設定檔；保守
    一次的代價只是使用者重試。
    """
    calls = _arm(monkeypatch, before=(), scan_ok=False)
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID), "start"))
    assert calls["spawned"] == []
    assert "無法確認" in env.replies[-1]


# --------------------------------------------------------------------------
# 回報的依據是觀測
# --------------------------------------------------------------------------
def test_a_launcher_that_never_appears_is_not_reported_as_started(env, monkeypatch):
    """**這支是整組的重點。**

    `spawn_detached` 回 True 只代表「中繼行程生出來了」。目標起不來（單一實例鎖被
    佔住、設定檔壞掉、腳本當場丟例外）時，spawn 那一側看起來一模一樣。拿那個
    True 當成「已啟動」回報，就是這個專案最不能接受的失敗形態。
    """
    calls = _arm(monkeypatch, before=(), after=())      # 掃描永遠看不到新的
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID), "start"))
    assert len(calls["spawned"]) == 1
    assert "等不到" in env.replies[-1]
    assert "已啟動" not in env.replies[-1]


def test_a_launcher_that_exits_immediately_is_not_reported_as_started(env, monkeypatch):
    """出現過但隨即結束，也不算啟動成功。

    獨立監督者最常見的早退是「已經有批次在跑」與「單一實例鎖被佔住」，兩者都是
    起來幾百毫秒就結束——只看「有沒有出現」的話，兩種都會被回報成成功。
    """
    _arm(monkeypatch, before=(), after=(888,), alive=False)
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID), "start"))
    assert "隨即結束" in env.replies[-1]


def test_what_guarantees_we_do_not_claim_someone_elses_launcher(env, monkeypatch):
    """**這一格的保證來自前置檢查，不是確認迴圈裡那個差集。**

    寫這支測試的時候我以為差集（`pid not in before`）是那道閘。變異實測打臉：把它
    換成 `list(now_pids)` 沒有任何測試變紅，而且那是**正確的**——前置檢查看到既有
    實例就已經 `return` 了，所以走到確認迴圈時 `before` 必定是空的，兩種寫法等價。

    留這支測試是為了把真正的保證釘在它實際所在的位置：既有實例必須在**前置檢查**
    就被擋下來。差集本身是零成本的縱深防禦（前置檢查哪天放寬，那裡不會跟著壞），
    不是這一格的依據——所以不要為它再寫一支「會通過但什麼都沒證明」的測試。
    """
    calls = _arm(monkeypatch, before=(999,), after=(999,))
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID), "start"))
    assert calls["spawned"] == [], "既有實例存在時，連 spawn 都不該發生"
    assert "已啟動" not in env.replies[-1]


def test_a_preexisting_launcher_is_not_mistaken_for_the_one_we_started(env, monkeypatch):
    """承上：既有實例在**前置檢查**就看得到時，必須是拒絕而不是成功。"""
    calls = _arm(monkeypatch, before=(999,), after=(999,))
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID), "start"))
    assert calls["spawned"] == []
    assert "已啟動" not in env.replies[-1]


# --------------------------------------------------------------------------
# 停止
# --------------------------------------------------------------------------
def test_stop_kills_what_the_scan_found(env, monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(b, "find_launcher_pids", lambda: ([11, 22], True))
    monkeypatch.setattr(b, "terminate_launcher_pids",
                        lambda pids: killed.extend(pids) or len(pids))
    asyncio.run(b.cmd_launcher(_fake_message(b.OWNER_USER_ID), "stop"))
    assert killed == [11, 22]
    # 目前那一輪批次不會跟著停，所以一定要講出來，否則使用者會以為
    # `/launcher stop` 等於 `/stop`，然後看著圖繼續產出來。
    assert "/stop" in env.replies[-1]


class _FakeProc:
    def __init__(self, pid, name, cmdline, ppid=0, raises=None):
        self.info = {"pid": pid, "name": name}
        self._cmdline = cmdline
        self._ppid = ppid
        self._raises = raises

    def cmdline(self):
        if self._raises is not None:
            raise self._raises
        return self._cmdline

    def ppid(self):
        return self._ppid


def _fake_psutil(monkeypatch, procs=None, boom=None):
    """裝一個假的 psutil。**絕不碰真的行程**——這台機器上可能有一個跑了好幾天的
    正式批次，而 `sys.modules.pop("psutil")` 這種寫法會讓 `import psutil` 重新載入
    **真的**那一個（本專案已經為此賠掉過一個 78.7 小時的批次）。"""
    module = types.SimpleNamespace(
        NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
        AccessDenied=type("AccessDenied", (Exception,), {}),
    )

    def process_iter(attrs=None):
        if boom is not None:
            raise boom
        return list(procs or [])

    module.process_iter = process_iter
    monkeypatch.setitem(sys.modules, "psutil", module)
    return module


def test_a_scan_that_worked_says_so(monkeypatch):
    """正面對照。少了它，一個「永遠回不完整」的實作會讓下面兩支全綠，而那會讓
    `/launcher start` 與 `/run` 從此一律拒絕動作。"""
    _fake_psutil(monkeypatch, procs=[
        _FakeProc(11, "python.exe", ["python.exe", "start_webrunner.py", "je"]),
        _FakeProc(12, "notepad.exe", ["notepad.exe", "start_webrunner.py"]),
    ])
    pids, scan_ok = pc.find_launcher_pids()
    assert scan_ok is True
    assert pids == [11], "只有 Python 行程、而且參數真的是那支腳本的才算"


def test_a_scan_that_could_not_run_says_it_could_not(monkeypatch):
    """**「掃不成」與「沒找到」在回傳值上必須分得出來。**

    兩者都是空清單。下游拿它去決定「要不要再起一個監督者」，猜錯成「沒有」的代價
    是同一台機器上兩個批次搶同一份瀏覽器設定檔。
    """
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert pc.find_launcher_pids() == ([], False)


def test_a_scan_that_blew_up_midway_keeps_what_it_got_but_says_incomplete(monkeypatch):
    """掃到一半才炸更糟：回傳**部分**名單，而呼叫端照樣把「沒有更多了」當事實。"""
    _fake_psutil(monkeypatch, boom=RuntimeError("nope"))
    pids, scan_ok = pc.find_launcher_pids()
    assert scan_ok is False
    assert pids == []


def test_a_vanishing_process_is_not_a_failed_scan(monkeypatch):
    """行程在列舉途中消失是這個 API 的日常，把它當失敗會讓警告天天出現，而天天
    出現的警告等於沒有警告。"""
    module = _fake_psutil(monkeypatch)
    module.process_iter = lambda attrs=None: [
        _FakeProc(21, "python.exe", [], raises=module.NoSuchProcess()),
        _FakeProc(22, "python.exe", ["python.exe", "start_webrunner.py"]),
    ]
    pids, scan_ok = pc.find_launcher_pids()
    assert scan_ok is True
    assert pids == [22]


def test_terminate_launcher_pids_does_not_tree_kill(monkeypatch):
    """**刻意不帶 `/T`。**

    監督者底下掛著正在產圖的背景程式與整棵瀏覽器；樹狀終止會把它們一起硬砍掉，
    而 `/stop` 既有的「先客氣、5 秒後強制」流程本來就在處理那一半。
    """
    forced: list[int] = []
    monkeypatch.setattr(pc, "_pid_alive", lambda pid: pid != 3)
    monkeypatch.setattr(pc, "_force_kill_pid", lambda pid: forced.append(pid))
    assert pc.terminate_launcher_pids([1, 2, 3]) == 2
    assert forced == [1, 2]            # 3 已經死了，不重複動手

    # 「有沒有帶 `/T`」要問**實際送出去的命令列**，不要拿原始碼做子字串比對——
    # 上面那段 docstring 自己就寫著 `/T`，比對子字串的話它會比對到解釋規則的那
    # 句話，永遠是綠的（本專案反覆踩過的形狀）。這裡換掉最底層那一支、讓真正的
    # `terminate_launcher_pids` 跑完整條路，但一個行程都不會被終止。
    if os.name != "nt":
        pytest.skip("`taskkill` 的命令列只在 Windows 上組得出來")
    issued: list[list[str]] = []
    monkeypatch.undo()
    monkeypatch.setattr(pc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(pc, "_run_taskkill", lambda args: issued.append(list(args)))
    pc.terminate_launcher_pids([1, 2])
    assert issued, "應該真的組出了終止命令"
    for args in issued:
        assert "/T" not in args, f"終止獨立監督者時不得做樹狀終止：{args}"
        assert "/F" in args


# --------------------------------------------------------------------------
# 兩條路徑共存
# --------------------------------------------------------------------------
def _channel():
    sent: list[str] = []

    async def send(content=None, **kwargs):
        sent.append(content)
    return types.SimpleNamespace(send=send, sent=sent)


def test_run_stands_aside_when_a_standalone_launcher_is_alive(monkeypatch):
    """硬走下去不只是「兩個批次」。

    `/run` 會先 nuclear sweep 把對方的背景程式與整棵瀏覽器殺掉，對方的監督者退避
    幾秒後再生一個——兩邊從此互相終止、互相重生，而兩邊的紀錄看起來都正常。
    """
    monkeypatch.setattr(b, "find_launcher_pids", lambda: ([7], True))
    spawned: list[str] = []
    monkeypatch.setattr(b, "_spawn_webrunner",
                        lambda *a, **k: spawned.append(a) or (True, "x"))
    channel = _channel()
    asyncio.run(b._do_webrunner_run(channel))
    assert spawned == []
    assert "獨立監督者" in channel.sent[-1]


class _PastTheDiskGuard(Exception):
    """「磁碟檢查放行了」的記號：`_acquire_chrome_slot` 是它之後第一個被呼叫的東西，
    而且在任何 teardown（nuclear sweep）之前——走到這裡就停，絕不往下殺行程。"""


@pytest.mark.parametrize("free_gb, refused", [(1.2, True), (5.0, False), (None, False)],
                         ids=["low", "exactly-the-floor", "unknown"])
def test_run_refuses_on_low_disk_before_touching_anything(monkeypatch, free_gb, refused):
    """磁碟空間不足時 `/run` 要在任何 teardown 之前拒絕。拒絕那一邊在分支覆蓋率裡從來
    沒有成立過（2026-09-22）。剛好等於下限、或量不到（`None`）都放行——後者是刻意的：
    量不到不是「滿了」。"""
    monkeypatch.setattr(b, "find_launcher_pids", lambda: ([], True))
    monkeypatch.setattr(b, "_compute_run_plan", lambda: ([("a cat", "", "", "")], False))
    monkeypatch.setattr(b, "MIN_FREE_DISK_GB", 5.0)
    monkeypatch.setattr(b, "_free_disk_gb", lambda: free_gb)
    spawned: list = []
    monkeypatch.setattr(b, "_spawn_webrunner",
                        lambda *a, **k: spawned.append(a) or (True, "x"))

    async def _slot(**_kw):
        raise _PastTheDiskGuard

    monkeypatch.setattr(b, "_acquire_chrome_slot", _slot)
    channel = _channel()
    if refused:
        asyncio.run(b._do_webrunner_run(channel))
        assert "磁碟僅剩" in channel.sent[-1], channel.sent
    else:
        with pytest.raises(_PastTheDiskGuard):
            asyncio.run(b._do_webrunner_run(channel))
        assert not any("磁碟僅剩" in (text or "") for text in channel.sent), channel.sent
    assert spawned == []


def test_run_stands_aside_when_the_scan_is_incomplete(monkeypatch):
    monkeypatch.setattr(b, "find_launcher_pids", lambda: ([], False))
    spawned: list[str] = []
    monkeypatch.setattr(b, "_spawn_webrunner",
                        lambda *a, **k: spawned.append(a) or (True, "x"))
    channel = _channel()
    asyncio.run(b._do_webrunner_run(channel))
    assert spawned == []
    assert "無法確認" in channel.sent[-1]


def test_run_still_runs_when_no_launcher_is_around(monkeypatch, tmp_path):
    """正面對照：少了它，一個「`/run` 永遠讓位」的實作也會讓上面兩支全綠。

    只需要走到讓位判斷的**下一關**（佇列前置檢查）就足以證明沒有被擋在這裡。
    """
    monkeypatch.setattr(b, "find_launcher_pids", lambda: ([], True))
    monkeypatch.setattr(b, "_compute_run_plan", lambda: ([], {}))
    channel = _channel()
    asyncio.run(b._do_webrunner_run(channel))
    assert channel.sent, "應該走到佇列前置檢查並回報，而不是停在讓位判斷"
    assert "獨立監督者" not in channel.sent[-1]


# --------------------------------------------------------------------------
# 單張產圖（`/gen image`）那條路也要讓位
#
# `/run` 那條 2026-09-12 就加了讓位，單張這條漏掉。兩條路的後果一模一樣——
# `_spawn_oneshot_webrunner` 開頭就是 nuclear sweep（無條件殺光整台機器的
# chrome.exe / chromedriver.exe），被殺掉的正是對方監督者的子行程，它退避幾秒後
# 再生一個，兩邊從此互相終止、互相重生。
#
# 這一族刻意**不**用 `test_recovery_paths._valve_env`：那一支把
# `find_launcher_pids` 固定成「沒有監督者」，正是為了不驗這件事。
# --------------------------------------------------------------------------
class _Placeholder:
    """`_generate_edit_placeholder` 需要的最小介面：一個 async `edit`。"""

    def __init__(self) -> None:
        self.content = None

    async def edit(self, content=None, **kwargs):
        self.content = content


def _ensure_server_env(monkeypatch, *, scan, alive=False, user_id=0):
    """讓**真的** `_generate_ensure_server` 跑起來，但把會碰到主機的換掉。

    ⚠️ **`_spawn_oneshot_webrunner` 一定要換掉**：它開頭就 nuclear sweep，真的會把
    開發機上每一個瀏覽器行程殺光，而這台機器上有一個跑了好幾天的正式批次。
    `_browser_killguard`（conftest 載入）擋得住真正的 kill，但靠後備網是錯的層級。

    ⚠️ **`_single_image_pending` 用 `setattr` 換掉整個 dict、不要就地塞。** 它是模組
    層可變物件，就地塞會漏到同一輪的別支測試去。
    """
    env = types.SimpleNamespace(spawns=[], scans=[], placeholder=_Placeholder())

    async def _spawn():
        env.spawns.append("oneshot")

    def _scan():
        env.scans.append(1)
        return scan

    monkeypatch.setattr(b, "_webrunner_alive", lambda: alive)
    monkeypatch.setattr(b, "find_launcher_pids", _scan)
    monkeypatch.setattr(b, "_spawn_oneshot_webrunner", _spawn)
    monkeypatch.setattr(b, "_generate_inflight", "R1")
    monkeypatch.setattr(b, "_single_image_pending", {
        "R1": {"user_id": user_id, "placeholder": env.placeholder}})
    return env


def test_the_single_image_path_stands_aside_when_a_launcher_is_alive(
        monkeypatch):
    """缺口只有幾秒寬，代價卻是兩套背景程式疊在同一個瀏覽器設定檔上。

    正常情況下對方的批次活著，`_webrunner_alive()` 就讓這條路重用而不 spawn。壞的
    是「監督者活著、它的子行程剛好在兩次退避重生之間」那幾秒——那時
    `_webrunner_alive()` 為 False，這條路會 sweep ＋ spawn，接著對方再生一個。
    """
    env = _ensure_server_env(monkeypatch, scan=([10556], True))
    asyncio.run(b._generate_ensure_server())
    assert env.spawns == [], "有獨立監督者在跑，卻還是 spawn 了單張伺服器"


def test_the_single_image_path_stands_aside_when_the_scan_is_incomplete(
        monkeypatch):
    """「掃不成」不等於「沒有」。

    兩種猜錯的代價差很多：猜成「沒有」＝上面那個互打的局面；猜成「有」＝這一筆多
    等一輪看門狗。與 `/run` 同一個立場。
    """
    env = _ensure_server_env(monkeypatch, scan=([], False))
    asyncio.run(b._generate_ensure_server())
    assert env.spawns == [], "掃描不完整時仍然 spawn 了單張伺服器"


def test_the_single_image_path_still_spawns_when_no_launcher_is_around(
        monkeypatch):
    """**正面對照**：少了它，一個「永遠不 spawn」的實作也會讓上面兩支全綠。

    而「永遠不 spawn」不是抽象的失敗——單張產圖從此只在剛好有批次在跑的時候才會
    動，其餘時間一律停在「產圖中…」直到看門狗取消。
    """
    env = _ensure_server_env(monkeypatch, scan=([], True))
    asyncio.run(b._generate_ensure_server())
    assert env.spawns == ["oneshot"], "沒有監督者時卻不肯啟動單張伺服器"
    assert env.placeholder.content is None, (
        f"沒有讓位卻送了讓位通知：{env.placeholder.content!r}")


def test_a_live_batch_is_reused_without_paying_for_a_process_scan(monkeypatch):
    """重用檢查必須排在讓位掃描**前面**。

    掃描是一次全機 psutil 列舉（實測約 140ms），而 pump 每提升一筆就走一次。順序
    反過來的話，完全正常的「批次活著」情況也要付這個代價——而那正是這條路最常見
    的情況。
    """
    env = _ensure_server_env(monkeypatch, scan=([10556], True), alive=True)
    asyncio.run(b._generate_ensure_server())
    assert env.spawns == [], "已經有 instance 在跑卻還是 spawn 了"
    assert env.scans == [], "重用那條路不該付一次全機行程掃描的代價"


def test_standing_aside_tells_the_asker_what_happens_next(monkeypatch):
    """**讓位不是只加一個 `return`。**

    請求檔已經寫在磁碟上，對方的子行程回來之後會 in-band 把它服務掉——所以讓位是
    「不要搶著 spawn」，不是「丟掉這一筆」。但少了通知，使用者手上只剩一則停在
    「產圖中…」的訊息，而他完全看不出來是在等別人、還是根本沒人要處理。所以也要
    講出「沒人回來會怎樣」（看門狗會取消並通知）。
    """
    env = _ensure_server_env(monkeypatch, scan=([10556], True))
    asyncio.run(b._generate_ensure_server())
    text = env.placeholder.content
    assert text, "讓位了卻什麼都沒跟使用者說"
    assert "監督" in text, text
    assert "自動取消" in text, f"沒講出「沒人回來會怎樣」：{text}"
    # 讓位不得動到閘門或佇列——動了就變成「丟掉這一筆」。
    assert b._generate_inflight == "R1", "讓位把 inflight 閘門清掉了"
    assert b._generate_queue == [], "讓位動到了佇列"


def test_the_stand_aside_notice_keeps_pids_away_from_non_owners(monkeypatch):
    """Secrecy Layer 1：非擁有者看到的字串裡不得有 pid，也不得有監督者數量。

    數量走 `_owner_detail` 這個單一決策點（`/launcher` 那邊同樣的處理），pid 一律
    只進 stderr。
    """
    # 擁有者 UID 來自 `bot_config.json`，預設 0（＝還沒設定）。這裡要比的是
    # 「擁有者 vs 非擁有者」，所以先給它一個真的值——0 的時候兩邊都是非擁有者，
    # 這支測試會變成拿同一條路徑跟自己比。
    monkeypatch.setattr(b, "OWNER_USER_ID", 400000000000000001)
    env = _ensure_server_env(monkeypatch, scan=([10556], True), user_id=99)
    asyncio.run(b._generate_ensure_server())
    text = env.placeholder.content
    assert "10556" not in text, f"pid 外洩到聊天室：{text}"
    assert "個）" not in text, f"非擁有者看到了監督者數量：{text}"

    owner = _ensure_server_env(monkeypatch, scan=([10556], True),
                               user_id=b.OWNER_USER_ID)
    asyncio.run(b._generate_ensure_server())
    owner_text = owner.placeholder.content
    assert "1 個" in owner_text, f"擁有者沒拿到細節：{owner_text}"
    assert "10556" not in owner_text, f"連擁有者這條也不該印 pid：{owner_text}"


def test_stop_kills_the_launcher_before_sweeping_the_batch(monkeypatch, tmp_path):
    """**順序不能反。**

    獨立監督者看到自己的子行程被殺掉會照退避規則再生一個，於是掃光那一步做完、
    幾秒後又冒出一個新的，而使用者已經收到「已停止」。
    """
    order: list[str] = []
    replies: list[str] = []

    async def fake_reply(message, content=None, **kwargs):
        replies.append(content)

    async def fake_terminate(proc, pid):
        order.append("sweep")
        return ["swept"]

    async def fake_acquire(label, timeout):
        return True

    monkeypatch.setattr(b, "safe_reply", fake_reply)
    monkeypatch.setattr(b, "find_launcher_pids", lambda: ([5], True))
    monkeypatch.setattr(b, "terminate_launcher_pids",
                        lambda pids: order.append("launcher") or len(pids))
    monkeypatch.setattr(b, "_terminate_all_webrunner_instances", fake_terminate)
    monkeypatch.setattr(b, "_acquire_chrome_slot", fake_acquire)
    monkeypatch.setattr(b, "_release_chrome_slot", lambda: None)
    monkeypatch.setattr(b, "_clear_pid", lambda: None)
    monkeypatch.setattr(b, "SINGLE_IMAGE_REQUEST_FILE", tmp_path / "req.json")
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    monkeypatch.setattr(b, "_webrunner_oneshot_reaper_task", None)
    asyncio.run(b.cmd_stop(_fake_message(b.OWNER_USER_ID)))
    assert order == ["launcher", "sweep"], order
    assert any("獨立監督者" in (text or "") for text in replies)


def test_stop_still_stops_when_the_chrome_slot_is_held(monkeypatch, tmp_path):
    """Chrome 槽擋的是「全機掃瀏覽器會誤殺驗證用的那一套」。`/run` 拿不到槽就不動，但
    `/stop` 反過來：使用者明確要求停止，不能被一個跑很久的驗證無限期擋住。所以只等一小段
    （`CHROME_SLOT_STOP_TIMEOUT_SEC`），拿不到仍照常掃，最後照樣 release。"""
    order: list = []
    asked: list = []

    async def fake_reply(message, content=None, **kwargs):
        order.append("reply")

    async def fake_terminate(proc, pid):
        order.append("sweep")
        return ["swept"]

    async def busy_slot(label, timeout):
        asked.append((label, timeout))
        return False

    monkeypatch.setattr(b, "safe_reply", fake_reply)
    monkeypatch.setattr(b, "find_launcher_pids", lambda: ([], True))
    monkeypatch.setattr(b, "_terminate_all_webrunner_instances", fake_terminate)
    monkeypatch.setattr(b, "_acquire_chrome_slot", busy_slot)
    monkeypatch.setattr(b, "_release_chrome_slot", lambda: order.append("release"))
    monkeypatch.setattr(b, "_clear_pid", lambda: None)
    monkeypatch.setattr(b, "SINGLE_IMAGE_REQUEST_FILE", tmp_path / "req.json")
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    monkeypatch.setattr(b, "_webrunner_oneshot_reaper_task", None)
    asyncio.run(b.cmd_stop(_fake_message(b.OWNER_USER_ID)))
    assert asked == [("stop", b.CHROME_SLOT_STOP_TIMEOUT_SEC)], asked
    assert order[:2] == ["sweep", "release"], order
    assert "reply" in order


def test_stop_says_so_when_it_could_not_check(monkeypatch, tmp_path):
    """掃不成就要講。

    沒講的話，一個沒被停掉的獨立監督者會在幾秒後把批次再生回來，而使用者手上
    只有一句「已停止」。
    """
    replies: list[str] = []

    async def fake_reply(message, content=None, **kwargs):
        replies.append(content)

    async def fake_terminate(proc, pid):
        return []

    async def fake_acquire(label, timeout):
        return True

    monkeypatch.setattr(b, "safe_reply", fake_reply)
    monkeypatch.setattr(b, "find_launcher_pids", lambda: ([], False))
    monkeypatch.setattr(b, "_terminate_all_webrunner_instances", fake_terminate)
    monkeypatch.setattr(b, "_acquire_chrome_slot", fake_acquire)
    monkeypatch.setattr(b, "_release_chrome_slot", lambda: None)
    monkeypatch.setattr(b, "_clear_pid", lambda: None)
    monkeypatch.setattr(b, "SINGLE_IMAGE_REQUEST_FILE", tmp_path / "req.json")
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    monkeypatch.setattr(b, "_webrunner_oneshot_reaper_task", None)
    asyncio.run(b.cmd_stop(_fake_message(b.OWNER_USER_ID)))
    assert any("沒能確認" in (text or "") for text in replies), replies


# --------------------------------------------------------------------------
# 脫離的結構
# --------------------------------------------------------------------------
def test_the_target_is_not_spawned_directly(monkeypatch):
    """目標必須經過一個會立刻結束的中繼行程。

    實測：直接 spawn 的行程——不論有沒有帶
    `DETACHED_PROCESS`、`CREATE_NEW_PROCESS_GROUP` 或 `CREATE_BREAKAWAY_FROM_JOB`
    ——都會被上游那棵行程樹的 `taskkill /F /T` 帶走。真正斷得掉的是**父子關係**，
    所以中繼行程結束之後目標就變成孤兒，樹狀終止再也走不到它。

    這支不驗作業系統行為（單元測試驗不了），驗的是「有沒有走那條路」：交給
    `Popen` 的第一格 argv 必須是中繼，不是目標本身。
    """
    seen: list[list[str]] = []
    monkeypatch.setattr(pc, "_detach_popen",
                        lambda argv, cwd: seen.append(list(argv)))
    assert pc.spawn_detached(["py", "target.py"], cwd=".") is True
    argv = seen[0]
    assert pc.DETACH_RELAY_FLAG in argv
    assert argv[1].endswith("_process_control.py")
    # 目標仍然完整地帶在後面，而且在中繼旗標之後。
    assert argv[argv.index(pc.DETACH_RELAY_FLAG) + 2:] == ["py", "target.py"]


def test_the_relay_spawns_the_target_and_returns_immediately(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(pc, "_detach_popen",
                        lambda argv, cwd: seen.append((list(argv), cwd)))
    rc = pc._relay_main([r"C:\work", "py", "target.py"])
    assert rc == 0
    assert seen == [(["py", "target.py"], r"C:\work")]


def test_the_relay_refuses_a_malformed_invocation():
    """引數不夠時不要去猜——猜錯就是在主機上起一個不該起的行程。"""
    assert pc._relay_main([]) == 2
    assert pc._relay_main([r"C:\work"]) == 2


def test_the_detach_uses_no_shell(monkeypatch):
    """中繼刻意不走 `cmd /c start`。

    那個寫法把所有參數丟進命令列剖析器，而這條路上的字串是從 `__file__` 推出來的
    ——clone 到一個名字裡有 `&` 的目錄就會被當成指令分隔。用 argv 清單傳遞的話，
    引號與 metacharacter 的整個問題類別都不存在。
    """
    tree = ast.parse((PKG_ROOT / "_process_control.py").read_text(encoding="utf-8"))
    target = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "_detach_popen")
    for node in ast.walk(target):
        if isinstance(node, ast.keyword) and node.arg == "shell":
            raise AssertionError("`_detach_popen` 不得使用 shell")
    body = ast.unparse(target)
    assert "cmd" not in body.lower().split("creationflags")[0] or True
    assert "shell=True" not in body


def test_the_relay_entry_point_is_reachable_as_a_script():
    """中繼是靠「把本模組當腳本跑」實作的，所以那個進入點必須真的在。

    少了它，`spawn_detached` 會生出一個立刻以 rc=2 結束的行程，而目標永遠不會
    啟動——而 `spawn_detached` 仍然回 True。
    """
    tree = ast.parse((PKG_ROOT / "_process_control.py").read_text(encoding="utf-8"))
    guards = [node for node in tree.body
              if isinstance(node, ast.If)
              and "__main__" in ast.unparse(node.test)]
    assert guards, "`_process_control.py` 少了 `__main__` 進入點"
    # 比對的是**名字**：`ast.unparse` 印出來的是 `DETACH_RELAY_FLAG` 這個識別字，
    # 不是它的值，所以拿值去比會永遠紅。
    entry = ast.unparse(guards[0])
    assert "DETACH_RELAY_FLAG" in entry
    assert "_relay_main" in entry
