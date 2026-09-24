"""兩件「log 只在有話說的時候才出聲」的守門。

**共同的失敗形態是同一個：一個看起來像節流的判準，實際上一次都沒節流到。**

第一組（RPC apply）：實測連續執行 47.9 小時的 `discord_bot.log`，11,768 行裡有
11,314 行（95.8%）是同一句 `rpc apply -> ...`，而且 `key` 從頭到尾都是同一個。
成因不是忘了節流，是節流的判準選錯了——`RichPresenceClient.apply` 有一個刻意的
保活（沒變就回 `unchanged`，超過 `refresh_sec` 就重送並回 `ok`），所以穩態下這兩
個值**永遠交替**，而「跟上次不一樣才印」對交替完全無效。

第二組（程式碼指紋）：漂移是本專案的常態（repo 一直在被編輯），而檢查掛在每分鐘
醒一次的迴圈上。`drifted is False` 時只要印任何東西，它就會變成下一個
`rpc apply ->`。所以這裡把「沒事時完全安靜」當成一等公民來測。

兩組都額外釘一條「不要為了安靜而說謊」：
* RPC 那邊 `_rpc_last_result` 必須保留**原始值**——狀態指令讀它，摺成分類過的值
  等於讓 `/probe_status` 報一個假的答案；
* 指紋那邊 `drifted is None`（判斷不出來）**不可以**被讀成乾淨。本專案在
  `_find_all_chrome_processes` / `_load_pid` / `_webrunner_liveness` 上各踩過一次
  「失敗的掃描長得跟乾淨的掃描一模一樣」。

第三組（`/probe_status` 的遊戲白名單）是同一條的延伸：狀態指令印的必須是比對器
**實際在用**的那一份。它原本自己再濾一次說明 key，而那份複本讓
`presence_probe._is_comment_key` 壞掉時完全看不出來（兩道守護互相遮蔽）。

第四組（presence apply 的去重快取）是第一組的**鏡像**：第一組的毛病是節流判準一次
都沒節流到，這一組的毛病是節流之後再也不解除。`_apply_combined_presence` 的
docstring 記著一次真的事故——去重的 key 原本在送出**之前**就更新，於是網路抖動讓
`change_presence` 丟例外時，快取已經寫成新值，下一個 probe tick 算出同一個 key、
提早 return，狀態就卡在那裡直到 bot 重啟。修法是「只有送成功才更新快取」。

**那個 docstring 有 17 行，而這支函式在 2026-09-20 以前的測試覆蓋率是 0**：整個
函式體沒有任何測試執行過，包括三條 except 與那行快取更新。唯一會跑到它的是
`_test_presence_e2e.py`——一支不連網、不用憑證、跑起來就過的手動腳本，但檔名的
`_test_` 前綴讓 pytest 不收集它，所以它只在有人想起來的時候才跑。

測試不連 Discord、不起子行程、不碰 repo 裡任何檔案（只讀原始碼做 AST 檢查；第三組
的設定檔寫在 `tmp_path`）。
"""
import ast
import asyncio
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord_bot as b  # noqa: E402


# --------------------------------------------------------------------------
# 共用
# --------------------------------------------------------------------------

@pytest.fixture
def rpc_state(monkeypatch):
    """把三個 RPC 記錄用的模組層全域隔離起來。"""
    monkeypatch.setattr(b, "_rpc_last_logged", None, raising=False)
    monkeypatch.setattr(b, "_rpc_last_result", "idle", raising=False)
    monkeypatch.setattr(b, "_rpc_last_key", None, raising=False)
    return b


@pytest.fixture
def drift_state(monkeypatch):
    """指紋檢查的兩個節流狀態；`None` ＝ 還沒檢查過，所以下一次呼叫一定會跑。"""
    monkeypatch.setattr(b, "_code_drift_last_check", None, raising=False)
    monkeypatch.setattr(b, "_code_drift_last_line", "", raising=False)
    return b


def _lines(capsys):
    out = capsys.readouterr()
    return [ln for ln in out.out.splitlines() if ln.strip()], out.err


# 刻意**每次都從 `b.__file__` 重讀**（依路徑快取），不要在 import 時就把樹定死：
# 變異腳本會把 `b.__file__` 指到一份改過的副本，樹定死的話那幾支 AST 守門就永遠
# 看著原始檔、變異全數存活，看起來像是「守門很強」。
_TREE_CACHE = {}


def _bot_tree():
    path = b.__file__
    if path not in _TREE_CACHE:
        _TREE_CACHE[path] = ast.parse(
            Path(path).read_text(encoding="utf-8"), filename=path)
    return _TREE_CACHE[path]


def _func(name):
    for node in ast.walk(_bot_tree()):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return node
    raise AssertionError(f"{name} 不在 discord_bot.py 裡了")


def _called_names(node):
    """`node` 底下所有被呼叫到的名字（含 `a.b()` 的 `a.b` 與 `b`）。"""
    names = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        target = sub.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
            if isinstance(target.value, ast.Name):
                names.add(f"{target.value.id}.{target.attr}")
    return names


# ==========================================================================
# 事情一：RPC apply 的記錄判準
# ==========================================================================

def test_the_keepalive_alternation_is_not_a_state_change(rpc_state, capsys):
    """穩態下 `ok` / `unchanged` 交替 20 次，只准印第一次那一行。

    這正是那 11,314 行的產生方式：舊判準是 `result != _rpc_last_result`，而保活
    讓兩個值輪流出現，所以每個 `refresh_sec` 週期固定產出兩行。
    """
    printed = [b._rpc_log_result(r, "claude|Claude Code")
               for r in ["ok", "unchanged"] * 10]
    lines, _ = _lines(capsys)
    assert printed[0] is True, "第一次要印（這是進入穩態的那一行）"
    assert not any(printed[1:]), "穩態下的保活往返一行都不該印"
    assert len(lines) == 1


def test_a_failure_result_breaks_through_the_throttle(rpc_state, capsys):
    """`ok` / `unchanged` 以外的結果一定要印——那正是節流不可以吃掉的東西。"""
    b._rpc_log_result("ok", "k")
    capsys.readouterr()
    for bad in ("not-connected", "send-failed", "rejected", "disabled"):
        b._rpc_last_logged = ("healthy", "k")   # 每次都從「健康」轉過去
        assert b._rpc_log_result(bad, "k") is True, bad
        lines, _ = _lines(capsys)
        assert len(lines) == 1 and bad in lines[0]


def test_recovery_back_to_healthy_is_announced(rpc_state, capsys):
    """壞掉要印，**復原也要印**——只看得到壞掉、看不到復原比不記錄還糟。"""
    b._rpc_log_result("send-failed", "k")
    capsys.readouterr()
    assert b._rpc_log_result("ok", "k") is True
    lines, _ = _lines(capsys)
    assert len(lines) == 1 and "ok" in lines[0]


def test_two_different_failures_are_both_announced(rpc_state, capsys):
    """`send-failed` → `not-connected` 是兩種不同的壞法，不可以被摺成一類。"""
    b._rpc_log_result("send-failed", "k")
    capsys.readouterr()
    assert b._rpc_log_result("not-connected", "k") is True


def test_a_new_key_is_announced(rpc_state, capsys):
    """健康類別沒變但 key 變了要印：那代表使用者在做的事變了，是有資訊量的。"""
    b._rpc_log_result("ok", "claude|Claude Code")
    capsys.readouterr()
    assert b._rpc_log_result("unchanged", "game|Some Game") is True
    lines, _ = _lines(capsys)
    assert len(lines) == 1 and "Some Game" in lines[0]
    # 換完之後新的 key 一樣要回到安靜。
    capsys.readouterr()
    assert b._rpc_log_result("ok", "game|Some Game") is False


def test_the_health_class_keeps_ok_and_unchanged_together(rpc_state):
    """分類本身：只有這兩個算健康，其餘一律各自成一類（不得摺成一個 'bad'）。"""
    assert b._rpc_health_class("ok") == b._rpc_health_class("unchanged")
    assert b._rpc_health_class("ok") not in ("ok", "unchanged")
    assert b._rpc_health_class("send-failed") != b._rpc_health_class("rejected")


# ---- 走真正的 _apply_rpc_presence，確認狀態指令讀到的仍是原始值 ----------

def _fake_rpc(monkeypatch, *, results, enabled=True, client_id="123"):
    """裝一組假的 RPC 模組與 client；`results` 是每次 apply 依序回傳的值。"""
    seq = list(results)
    calls = []

    class _Client:
        def apply(self, activity, cid, refresh):   # noqa: D401
            calls.append((activity, cid, refresh))
            return seq.pop(0)

        def status(self):
            return {"connected": True, "last_error": ""}

    fake = types.SimpleNamespace(
        load_rpc_config=lambda: {"enabled": enabled, "client_id": client_id,
                                 "refresh_sec": 60.0},
        build_activity=lambda probe, cfg, started: {"name": "x"},
        RichPresenceClient=_Client,
    )
    monkeypatch.setattr(b, "discord_rpc", fake)
    monkeypatch.setattr(b, "_rpc_client", _Client())
    return calls


def test_probe_status_still_sees_the_raw_result(rpc_state, monkeypatch, capsys):
    """**這是這一組最重要的一支。**

    `_rpc_last_result` 是 `/probe_status` 顯示的「最近 apply 結果」。把它換成分類
    過的值（`healthy`）會讓狀態指令開始報一個從來不存在的結果，而且因為指令看起來
    正常運作，沒有人會發現。所以：印不印是一回事，原始值必須原封不動。
    """
    _fake_rpc(monkeypatch, results=["ok", "unchanged", "send-failed"])
    probe = {"kind": "claude", "name": "Claude Code"}
    seen = []
    for _ in range(3):
        asyncio.run(b._apply_rpc_presence(probe))
        seen.append(b._rpc_last_result)
    assert seen == ["ok", "unchanged", "send-failed"], \
        "狀態指令讀的必須是原始值，不是健康類別"
    lines, _ = _lines(capsys)
    assert len(lines) == 2, f"應該只有『進入健康』與『壞掉』兩行，實際：{lines}"


def test_the_disabled_branch_is_announced_once(rpc_state, monkeypatch, capsys):
    """停用時每個 tick 都會走同一條路，但只准印進入停用的那一次。

    這條路以前完全不出聲（print 在函式尾巴、走不到），所以「RPC 被關掉了」在 log
    上看不出來；分類去重之後印它是安全的。
    """
    _fake_rpc(monkeypatch, results=[], enabled=False)
    monkeypatch.setattr(b, "_rpc_client", None)   # 沒連過就不必開 thread 去清
    for _ in range(5):
        asyncio.run(b._apply_rpc_presence({"kind": "claude", "name": "C"}))
    assert b._rpc_last_result == "disabled"
    lines, _ = _lines(capsys)
    assert len(lines) == 1, f"停用只該印一次，實際：{lines}"


# ==========================================================================
# 事情二：程式碼指紋
# ==========================================================================

def _report(drifted, *, at_start="aaa", now="bbb", changed=(), removed=(),
            added=(), why=""):
    return {"drifted": drifted, "why": why, "at_start": at_start, "now": now,
            "changed": list(changed), "added": list(added),
            "removed": list(removed)}


def _fake_fingerprint(monkeypatch, *, report=None, describe="code aaa",
                      snapshot_raises=None, report_raises=None):
    calls = {"snapshot": 0, "drift": 0}

    def _snapshot():
        calls["snapshot"] += 1
        if snapshot_raises is not None:
            raise snapshot_raises
        return "aaa"

    def _drift():
        calls["drift"] += 1
        if report_raises is not None:
            raise report_raises
        return report

    monkeypatch.setattr(b, "_code_fingerprint", types.SimpleNamespace(
        snapshot=_snapshot, drift_report=_drift, describe=lambda: describe))
    return calls


def test_main_freezes_the_fingerprint_at_startup():
    """`main()` 必須取一次啟動快照。

    晚算等於拿磁碟跟磁碟自己比，永遠回報「沒有漂移」——而且測試會全綠。那是
    `_code_fingerprint` 唯一真正的失效方式。
    """
    assert "_log_code_fingerprint" in _called_names(_func("main"))


def test_the_startup_helper_actually_calls_snapshot(monkeypatch, capsys):
    """把 `snapshot()` 那一行拿掉，`main()` 照樣「有呼叫」、log 照樣印得出一行。

    所以光檢查呼叫關係不夠——要證明快照真的被凍住了。
    """
    calls = _fake_fingerprint(monkeypatch, describe="code deadbeef")
    line = b._log_code_fingerprint()
    assert calls["snapshot"] == 1, "沒有真的凍住快照"
    assert "deadbeef" in line
    lines, _ = _lines(capsys)
    assert len(lines) == 1 and "deadbeef" in lines[0]


def test_the_startup_banner_never_raises(monkeypatch, capsys):
    """一行診斷紀錄不得有任何機會把一次正常的啟動變成失敗。"""
    _fake_fingerprint(monkeypatch, snapshot_raises=OSError("boom"))
    line = b._log_code_fingerprint()          # 不得拋出
    assert isinstance(line, str) and line
    out, err = _lines(capsys)
    assert "boom" not in line, "回傳的字串不得夾帶原始例外文字"
    assert "boom" in err, "原始錯誤要留在 stderr"


def test_a_clean_check_says_nothing_at_all(drift_state, monkeypatch, capsys):
    """**最重要的一條**：沒漂移時一個字都不印。

    漂移是常態、迴圈每分鐘醒一次；這裡只要漏出一行，它就會變成下一個
    `rpc apply ->`（同一份 log 裡的 11,314 行前例）。
    """
    _fake_fingerprint(monkeypatch, report=_report(False))
    for _ in range(3):
        b._code_drift_last_check = None       # 每次都真的檢查
        assert asyncio.run(b._check_code_drift()) == ""
    lines, err = _lines(capsys)
    assert lines == [] and err == ""


def test_unknown_is_not_read_as_clean(drift_state, monkeypatch, capsys):
    """`None`（判斷不出來）要出聲，而且措辭要跟「有漂移」分得開。

    這是本專案踩過三次的形狀：**失敗的掃描長得跟乾淨的掃描一模一樣**。
    """
    _fake_fingerprint(monkeypatch, report=_report(
        None, why="2 source file(s) unreadable; cannot tell"))
    line = asyncio.run(b._check_code_drift())
    assert line, "判斷不出來時必須出聲"
    lines, _ = _lines(capsys)
    assert len(lines) == 1

    # 跟「確定有漂移」那句必須看得出差別。
    b._code_drift_last_check = None
    b._code_drift_last_line = ""
    _fake_fingerprint(monkeypatch, report=_report(True, changed=["x.py"]))
    drifted_line = asyncio.run(b._check_code_drift())
    assert drifted_line != line
    assert "無法判斷" in line and "無法判斷" not in drifted_line


def test_real_drift_is_announced_once_per_distinct_report(
        drift_state, monkeypatch, capsys):
    """有漂移要說，但同一份報告不要每分鐘重複——那也是一種雜訊。"""
    _fake_fingerprint(monkeypatch, report=_report(
        True, now="bbb", changed=["discord_bot.py"], why="1 changed, 0 removed"))
    assert asyncio.run(b._check_code_drift())
    b._code_drift_last_check = None
    assert asyncio.run(b._check_code_drift()) == "", "同一份報告不該再印"
    lines, _ = _lines(capsys)
    assert len(lines) == 1 and "discord_bot.py" in lines[0]

    # 又被編輯一次（指紋變了）→ 要再說一次。
    _fake_fingerprint(monkeypatch, report=_report(
        True, now="ccc", changed=["discord_bot.py"]))
    b._code_drift_last_check = None
    assert asyncio.run(b._check_code_drift())


def test_a_clean_check_rearms_the_announcement(drift_state, monkeypatch, capsys):
    """漂移 → 乾淨 → 又漂移（同一份報告）：第三次必須再說一次。

    去重記憶如果不在「乾淨」那一次清掉，回復之後的再次漂移會被永久消音。
    """
    drifted = _report(True, now="bbb", changed=["x.py"])
    _fake_fingerprint(monkeypatch, report=drifted)
    assert asyncio.run(b._check_code_drift())

    _fake_fingerprint(monkeypatch, report=_report(False))
    b._code_drift_last_check = None
    assert asyncio.run(b._check_code_drift()) == ""

    _fake_fingerprint(monkeypatch, report=drifted)
    b._code_drift_last_check = None
    assert asyncio.run(b._check_code_drift()), "回復之後的再次漂移不該被消音"


def test_the_drift_check_is_throttled(drift_state, monkeypatch):
    """間隔內不得重跑：`drift_report()` 要對 `sys.modules` 每一項做 resolve，
    實測約 270 ms，每分鐘一次是白花的。"""
    import time as _time
    calls = _fake_fingerprint(monkeypatch, report=_report(False))
    b._code_drift_last_check = _time.monotonic()
    asyncio.run(b._check_code_drift())
    assert calls["drift"] == 0
    b._code_drift_last_check = _time.monotonic() - b.CODE_DRIFT_CHECK_INTERVAL_SEC - 1
    asyncio.run(b._check_code_drift())
    assert calls["drift"] == 1


def test_the_drift_check_never_raises(drift_state, monkeypatch, capsys):
    """檢查本身壞掉只准損失這一輪，不得把每分鐘那條迴圈帶走。"""
    _fake_fingerprint(monkeypatch, report_raises=RuntimeError("nope"))
    assert asyncio.run(b._check_code_drift()) == ""
    _, err = _lines(capsys)
    assert "nope" in err


def test_the_drift_line_carries_no_host_path(drift_state, monkeypatch, capsys):
    """印出去的字串只放檔名與十六進位摘要，不放主機路徑（保密規則 Layer 1）。

    log 本身不受 Layer 1 約束，但這一行的形狀就是給人貼進狀態回報的形狀，所以
    先把它守住比事後補救便宜。
    """
    _fake_fingerprint(monkeypatch, report=_report(
        True, changed=["discord_bot.py", "_gui_control.py"], removed=["old.py"]))
    line = asyncio.run(b._check_code_drift())
    assert not re.search(r"[A-Za-z]:[\\/]", line), line
    assert "discord_bot.py" in line and "old.py" in line
    assert "added" not in line


def test_added_modules_are_not_reported_as_drift(drift_state, monkeypatch):
    """延遲 import 進來的模組不算落後，它是剛從磁碟載入的、正是最新的。

    這裡只釘「不要把它印進那一行」——判定本身由 `test_code_fingerprint.py` 守。
    """
    _fake_fingerprint(monkeypatch, report=_report(
        True, changed=["a.py"], added=["late_import.py"]))
    line = asyncio.run(b._check_code_drift())
    assert "late_import.py" not in line, "指著一個沒問題的檔名只會讓人查錯方向"


def test_the_health_loop_checks_for_drift_without_acting_on_it():
    """三件事一起釘：迴圈裡有這個呼叫、它在 `enabled` 閘**外面**、回傳值不得
    被拿去做判斷。

    * 少了呼叫 → 整個功能靜默失效，而所有單元測試照樣全綠；
    * 縮排進 `enabled` 閘 → 「關掉健康報告」會連程式碼指紋一起關掉（同一條迴圈
      的 ndjson 輪替就是為了這個才明文寫在閘外面）；
    * 拿回傳值做判斷 → 漂移是常態，接進控制流程只會誤殺。
    """
    loop = _func("_daily_health_loop")

    bare = []
    for node in ast.walk(loop):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Await)
                and isinstance(node.value.value, ast.Call)
                and isinstance(node.value.value.func, ast.Name)
                and node.value.value.func.id == "_check_code_drift"):
            bare.append(node)
    assert len(bare) == 1, \
        "`await _check_code_drift()` 必須是**裸的**運算式陳述，剛好一個"

    # 不得藏在 `if cfg.get("enabled")` 之類的閘裡面。
    for node in ast.walk(loop):
        if not isinstance(node, ast.If):
            continue
        if "_check_code_drift" not in _called_names(node):
            continue
        raise AssertionError(
            "程式碼指紋檢查被關進一個條件分支裡了；它必須無條件每輪執行")


def test_the_fingerprint_module_is_a_passive_import():
    """bot 只 import 那個被動共用模組，沒有把它變成別的東西。"""
    assert b._code_fingerprint.__name__ == "_code_fingerprint"
    assert not hasattr(b._code_fingerprint, "discord_bot")


# ==========================================================================
# 事情三：`/probe_status` 印的遊戲白名單，就是比對器在用的那一份
# ==========================================================================
#
# 「這是說明 key 嗎」只有一份實作（`presence_probe._is_comment_key`，載入時判定
# 原始 key）。bot 原本在 `cmd_probe_status` 裡對**正規化之後**的 key 再濾一次
# `startswith("_")`，於是權威那份拿掉 `.strip()`、甚至永遠回 False，這個指令印出
# 來的東西都一個字不變——唯一看得到白名單內容的地方，被規則的複本遮住了。
#
# 下面兩支的輸入**各自只踩得到一道**：第一支只有權威那份擋得住，第二支只有 bot
# 那一側的第二道過濾會擋。兩者都只用合成資料；拿真的 `presence_games.json` 來比
# 是空轉——它目前沒有任何一個 key 會讓兩種寫法分岔（2026-09-12 量過）。

class _ProbeStatusMsg:
    """`safe_reply` 只會呼叫 `message.reply(...)`，記下內容就夠了。"""

    def __init__(self):
        self.sent = []

    async def reply(self, content=None, **kwargs):
        self.sent.append(content)


@pytest.fixture
def probe_status(tmp_path, monkeypatch):
    """讓 `cmd_probe_status` 只剩「遊戲白名單」那一段是真的。

    白名單走**真的**載入器（`_is_comment_key` ＋ `_normalise_game_key`），只把
    設定檔換到 `tmp_path`；其餘會起子行程（SMTC）或掃全機行程（psutil）的探測
    一律換成安靜的替身。

    一律 patch `b.presence_probe`——bot 綁到的那一份模組物件。bot 是用
    `from axiomatic import presence_probe` 匯入的，另外 `import presence_probe`
    拿到的可能是**另一份**同名模組，patch 它對 bot 完全無效。

    回傳一個函式：給它原始 JSON dict，它寫檔、跑指令、回傳送出的那一則文字。
    """
    pp = b.presence_probe
    games = tmp_path / "presence_games.json"
    monkeypatch.setattr(pp, "GAMES_FILE", games)

    async def _no_smtc():
        return None

    async def _no_signals():
        return {}

    monkeypatch.setattr(pp, "probe_smtc_raw_async", _no_smtc)
    monkeypatch.setattr(pp, "probe_signals_async", _no_signals)
    monkeypatch.setattr(pp, "probe_foreground_window_raw", lambda: "")
    monkeypatch.setattr(pp, "probe_foreground_music", lambda: None)
    monkeypatch.setattr(pp, "probe_game_process", lambda: None)
    monkeypatch.setattr(pp, "probe_priority_game", lambda: None)
    monkeypatch.setattr(pp, "probe_claude_code", lambda: None)
    monkeypatch.setattr(pp, "bot_activity_from_signals", lambda signals: None)
    monkeypatch.setattr(pp, "rpc_activity_from_signals", lambda signals: None)
    monkeypatch.setattr(b, "discord_rpc", None)   # 跳過本機 RPC 那一段

    def _run(raw):
        games.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        msg = _ProbeStatusMsg()
        asyncio.run(b.cmd_probe_status(msg))
        assert len(msg.sent) == 1, f"應該剛好送出一則，實際：{msg.sent}"
        return msg.sent[0]

    return _run


@pytest.mark.parametrize("asker, shows_title", [(None, False), ("owner", True), ("other", False)])
def test_probe_status_shows_the_window_title_only_to_the_owner(probe_status, monkeypatch,
                                                              asker, shows_title):
    """前景視窗的標題常帶主機路徑與程式名，而這個指令在誰都叫得到的檢視那一級。原文只給
    擁有者（`_owner_detail`，身分閘）；取不到提問者一律當成不是擁有者。"""
    title = r"C:\work\secret\notes.txt - Notepad"
    monkeypatch.setattr(b.presence_probe, "probe_foreground_window_raw", lambda: title)
    if asker is not None:
        uid = b.OWNER_USER_ID if asker == "owner" else b.OWNER_USER_ID + 1
        monkeypatch.setattr(_ProbeStatusMsg, "author",
                            types.SimpleNamespace(id=uid), raising=False)
    text = probe_status({})
    assert ("notes.txt" in text) is shows_title, text
    if not shows_title:
        assert "只有擁有者看得到" in text


@pytest.mark.parametrize("asker, shows", [(None, False), ("owner", True), ("other", False)])
def test_probe_status_shows_the_media_session_only_to_the_owner(probe_status, monkeypatch,
                                                               asker, shows):
    """被白名單擋下的媒體工作階段（本機播放器、瀏覽器分頁、檔名當標題的影片）是擁有者不打算
    公開的東西。原文只給擁有者；accepted／rejected 的判斷大家都看得到。"""
    async def _smtc():
        return {"title": "family_trip_2026.mp4", "artist": "home",
                "source": "VLC.exe"}

    monkeypatch.setattr(b.presence_probe, "probe_smtc_raw_async", _smtc)
    if asker is not None:
        uid = b.OWNER_USER_ID if asker == "owner" else b.OWNER_USER_ID + 1
        monkeypatch.setattr(_ProbeStatusMsg, "author",
                            types.SimpleNamespace(id=uid), raising=False)
    text = probe_status({})
    for raw in ("family_trip_2026", "VLC.exe"):
        assert (raw in text) is shows, (raw, text)
    assert "rejected by source whitelist" in text


def test_probe_status_does_not_mask_a_broken_comment_rule(probe_status):
    """`_` 前面帶空白的說明 key，只有 `_is_comment_key` 的 `.strip()` 擋得住。

    它正規化之後長成 `_note`，所以 bot 原本那句 `startswith("_")` 也會把它攔下來
    ——這就是遮蔽：拿掉 `.strip()` 或讓 `_is_comment_key` 永遠回 False，舊版印出
    來的東西一個字都不變（2026-09-12 樹上量過，兩個變異都存活）。

    反面欄杆：`"  Endfield.EXE "` 也前後帶空白、但**不是**說明，必須照樣列出來。
    少了它，「全部清空」與「前後有空白就當說明」的版本都會讓第一個斷言全綠。
    """
    text = probe_status({
        "  _note": "只是說明，不是遊戲",
        "  Endfield.EXE ": "Endfield",
    })
    assert "_note" not in text and "只是說明" not in text, (
        f"說明 key 被當成遊戲列出來了：\n{text}")
    assert "- 遊戲白名單: 1 筆" in text, text
    assert "`endfield.exe` → `Endfield`" in text, text


def test_probe_status_lists_exactly_what_the_matcher_uses(probe_status):
    """名字以底線開頭的 priority 遊戲**不是**說明，必須照樣列出來。

    `*_underscore.exe` 的原始 key 以 `*` 開頭，所以依權威那份的規則它是遊戲；正規化
    之後是 `_underscore.exe`，比對器拿它去對行程名、priority 清單也有它。這一格
    **只踩得到 bot 那一側的第二道過濾**：只要 `cmd_probe_status` 對
    `loaded_game_whitelist()` 的結果再濾一次——inline `startswith("_")`，或改呼叫
    `presence_probe._is_comment_key`（對正規化之後的 key，兩者答案完全相同）——
    這支就會紅。

    舊版在這裡印的是「1 筆（含 1 個 ⭐priority）」，清單卻只列出另一個遊戲：
    指令自己跟自己矛盾，而它存在的理由正是讓人看出「為什麼狀態沒換」。
    """
    text = probe_status({
        "*_underscore.exe": "Underscore Game",
        "endfield.exe": "Endfield",
    })
    assert "- 遊戲白名單: 2 筆（含 1 個 ⭐priority）" in text, text
    assert "`_underscore.exe` → `Underscore Game` ⭐priority" in text, (
        f"比對器在用的條目沒有列出來：\n{text}")
    assert "`endfield.exe` → `Endfield`" in text, text
    assert "空的" not in text, text


# --------------------------------------------------------------------------
# 第四組：presence apply 的去重快取——只有送成功才可以更新
# --------------------------------------------------------------------------

@pytest.fixture
def presence_apply(monkeypatch):
    """把 apply 會讀寫的三個全域隔離起來，並換掉真正的送出動作。

    回傳的 list 記錄每一次 `change_presence` 收到的 `(status, activity)`。
    `raises` 裡放例外實例就換成「這次送出會炸」，一次一個、用完就丟——真實世界的
    失敗也是這樣：抖一下，下一次就好了，而這一組要問的正是「下一次真的會再送嗎」。
    """
    calls = []
    raises = []

    async def fake_change_presence(*, status=None, activity=None, **_kw):
        calls.append((status, activity))
        if raises:
            raise raises.pop(0)

    monkeypatch.setattr(b, "_last_mirrored_presence_key", None, raising=False)
    monkeypatch.setattr(b, "_local_probed_activity", None, raising=False)
    monkeypatch.setattr(b, "_remote_mirrored_activity", None, raising=False)
    monkeypatch.setattr(b.client, "change_presence", fake_change_presence)
    return types.SimpleNamespace(calls=calls, raises=raises)


def _apply():
    asyncio.run(b._apply_combined_presence())


def _a_game(name="Arknights: Endfield"):
    return b.discord.Game(name=name)


def _some_music(name="Bohemian Rhapsody"):
    return b.discord.Activity(type=b.discord.ActivityType.listening, name=name)


def test_the_local_probe_wins_over_the_remote_mirror(presence_apply):
    """本機探測到的優先，鏡像來的只是沒中時的備胎。"""
    b._local_probed_activity = _a_game()
    b._remote_mirrored_activity = _some_music()
    _apply()
    assert [a.name for _s, a in presence_apply.calls] == ["Arknights: Endfield"]


def test_the_status_is_always_online_even_for_an_invisible_target(
        presence_apply):
    """目標使用者多半是隱身；照抄過來會把 bot 自己一起變不可見，反而看不到卡片。"""
    b._remote_mirrored_activity = _some_music()
    _apply()
    assert [s for s, _a in presence_apply.calls] == [b.discord.Status.online]


def test_the_same_state_is_only_sent_once(presence_apply, capsys):
    """去重本身要有效——否則每個 probe tick 都在對平台重送同一件事。"""
    b._local_probed_activity = _a_game()
    _apply()
    _apply()
    assert len(presence_apply.calls) == 1
    assert capsys.readouterr().out.count("presence apply ->") == 1


def test_a_changed_state_is_sent_again(presence_apply):
    b._local_probed_activity = _a_game()
    _apply()
    b._local_probed_activity = _some_music()
    _apply()
    assert [a.name for _s, a in presence_apply.calls] == [
        "Arknights: Endfield", "Bohemian Rhapsody"]


def test_clearing_the_activity_is_itself_a_state_change(presence_apply):
    """「什麼都沒在跑」也是一種狀態，不送出去的話卡片會一直留在上面。"""
    b._local_probed_activity = _a_game()
    _apply()
    b._local_probed_activity = None
    _apply()
    assert [a for _s, a in presence_apply.calls][-1] is None


def _an_http_error():
    """discord.py 的 `HTTPException` 需要一個有 `status`／`reason` 的回應物件。"""
    response = types.SimpleNamespace(status=429, reason="Too Many Requests")
    return b.discord.HTTPException(response, "rate limited")


@pytest.mark.parametrize("make_error, label", [
    (_an_http_error, "HTTPException"),
    (lambda: ConnectionResetError("transport closing"), "ConnectionReset"),
    (lambda: b.aiohttp.ClientError("Cannot write to closing transport"),
     "ClientError"),
    (lambda: RuntimeError("something nobody predicted"), "unexpected"),
])
def test_a_failed_send_never_poisons_the_dedup_cache(presence_apply, capsys,
                                                     make_error, label):
    """**這一組的主角。** 送出失敗時快取不得更新，下一個 tick 要重送。

    原本的寫法是送出前就更新快取，於是一次網路抖動之後：probe 迴圈照跑、本機也
    照樣偵測得到，但每一個 tick 都撞到去重的提早 return，永遠不再真的送出——平台
    那頭看到的是「狀態卡住」，而且要重啟 bot 才會恢復。`on_resumed` 重置快取只救
    得了 discord.py 真的有丟 resume 事件的那一種；被它內部吃掉的短暫斷線救不到。

    四種例外**都**走這條規則，因為它們共用的是同一個保證，不是同一個 except。
    """
    b._local_probed_activity = _a_game()
    presence_apply.raises.append(make_error())
    _apply()
    assert len(presence_apply.calls) == 1, label
    assert b._last_mirrored_presence_key is None, (
        f"{label}：送出失敗卻更新了去重快取，狀態會卡到重啟為止")

    _apply()                       # 下一個 probe tick，狀態完全沒變
    assert len(presence_apply.calls) == 2, (
        f"{label}：失敗之後被去重擋住，不會再重試")
    assert b._last_mirrored_presence_key is not None, (
        f"{label}：重試成功了卻沒有更新快取，之後每一個 tick 都會重送")


@pytest.mark.parametrize("make_error, expected", [
    (_an_http_error, "change_presence failed"),
    (lambda: ConnectionResetError("transport closing"),
     "change_presence transient"),
    (lambda: RuntimeError("something nobody predicted"),
     "presence apply unexpected failure"),
])
def test_a_failed_send_says_which_kind_it_was_and_does_not_raise(
        presence_apply, capsys, make_error, expected):
    """三條 except 各有各的說法——「暫時性斷線」跟「沒人預料到的錯」要分得開，
    否則重連期間的正常雜訊會把真正的意外淹掉。而且一律不得往外拋：這支是被
    背景迴圈呼叫的，拋出去會把整個 probe 迴圈打斷。
    """
    b._local_probed_activity = _a_game()
    presence_apply.raises.append(make_error())
    _apply()
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize("local, remote, expected", [
    (True, False, "src=local"),
    (False, True, "src=remote"),
    (False, False, "src=none"),
])
def test_the_success_line_names_where_the_activity_came_from(
        presence_apply, capsys, local, remote, expected):
    """成功那一行是查「為什麼顯示的是這個」時唯一的線索。"""
    b._local_probed_activity = _a_game() if local else None
    b._remote_mirrored_activity = _some_music() if remote else None
    _apply()
    assert expected in capsys.readouterr().out
# --------------------------------------------------------------------------
# 第五組：probe 迴圈本身——一個 tick 出事不可以讓整個迴圈死掉
# --------------------------------------------------------------------------

class _LoopDone(BaseException):
    """假的 sleep 用它把 `while True` 停在第 N 個 tick 的結尾。

    刻意繼承 `BaseException` 而不是 `Exception`。迴圈體裡有三段
    `except Exception`，而這一組要測的正是「try 的範圍被改掉」這件事——用
    `Exception` 的話，一個把整段迴圈體包起來的變異會**連停止訊號一起吃掉**，
    這幾支就從「紅」變成「掛住」，而掛住要等整批逾時才看得出來。
    """


@pytest.fixture
def probe_loop(monkeypatch):
    """跑 `_presence_probe_loop` 幾個 tick 然後停下來，全程不連網、不起子行程。

    五個對外動作全換成替身：兩個訊號對映、兩個 apply，以及 sleep。三個 queue
    （`signals` / `bot` / `rpc`）的規則是「一次一個、剩最後一個就一直用它」，
    所以放一個進去＝每個 tick 都一樣，放兩個＝第一個 tick 特別；放例外實例進去
    就代表那一個 tick 會炸。`apply_raises` / `rpc_raises` 同理。

    兩道互相獨立的煞車，都丟 `_LoopDone`：sleep 跑滿 `ticks` 次，或者訊號探測被
    呼叫超過 `ticks + 3` 次。第二道是為了「sleep 那一行被改掉」時仍然停得下來
    ——沒有它，那種變異會讓這幾支掛住而不是變紅。
    """
    state = types.SimpleNamespace(
        ticks=1, signals=[], bot=[], rpc=[], apply_raises=[], rpc_raises=[],
        seen_signals=[], applied=[], rpc_applied=[], sleeps=[], probes=0,
    )

    def _next(queue, default):
        if not queue:
            return default
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(value, BaseException):
            raise value
        return value

    async def fake_probe_signals():
        state.probes += 1
        if state.probes > state.ticks + 3:
            raise _LoopDone("sleep 的煞車沒生效——迴圈可能已經不 sleep 了")
        return _next(state.signals, {"game": None, "music_strict": None,
                                     "music_any": None, "claude": None})

    def fake_bot_activity(signals):
        state.seen_signals.append(signals)
        return _next(state.bot, None)

    def fake_rpc_activity(signals):
        return _next(state.rpc, None)

    async def fake_apply():
        state.applied.append(b._local_probed_activity)
        if state.apply_raises:
            raise state.apply_raises.pop(0)

    async def fake_rpc_apply(probe):
        state.rpc_applied.append(probe)
        if state.rpc_raises:
            raise state.rpc_raises.pop(0)

    async def fake_sleep(seconds):
        state.sleeps.append(seconds)
        if len(state.sleeps) >= state.ticks:
            raise _LoopDone

    pp = b.presence_probe
    monkeypatch.setattr(pp, "probe_signals_async", fake_probe_signals)
    monkeypatch.setattr(pp, "bot_activity_from_signals", fake_bot_activity)
    monkeypatch.setattr(pp, "rpc_activity_from_signals", fake_rpc_activity)
    monkeypatch.setattr(b, "_apply_combined_presence", fake_apply)
    monkeypatch.setattr(b, "_apply_rpc_presence", fake_rpc_apply)
    monkeypatch.setattr(b.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(b, "_local_probed_activity", None, raising=False)
    return state


def _run_loop(state, ticks=1):
    """跑 `ticks` 個完整的 tick（每個 tick 以 sleep 收尾），然後停。"""
    state.ticks = ticks
    try:
        asyncio.run(b._presence_probe_loop())
    except _LoopDone:
        pass
    except Exception as error:  # pylint: disable=broad-except
        raise AssertionError(
            "迴圈被一個例外打斷了。它是 presence 唯一的更新來源，斷掉之後狀態會"
            f"凍在最後一個值，而 bot 其他功能一切正常：{error!r}") from error
    else:
        raise AssertionError("`while True` 自己結束了")
    assert len(state.sleeps) == ticks, (
        f"預期跑滿 {ticks} 個 tick，實際只 sleep 了 {len(state.sleeps)} 次")


def _game_probe(name="Arknights: Endfield"):
    return {"kind": "playing", "name": name}


def _claude_probe():
    return {"kind": "claude", "name": "Claude Code"}


def test_each_mapper_drives_its_own_side(probe_loop):
    """兩套優先序存在的唯一理由，是它們在同一組訊號下會給出**不一樣**的答案
    （遊戲和 Claude 同時成立時，bot 顯示遊戲、擁有者自己的帳號顯示 Claude）。
    迴圈這幾行是全專案唯一決定「哪一個餵給哪一邊」的地方，而把兩者交換過來，
    `test_presence_probe.py` 那十幾支純函式測試一支都不會紅。

    順帶釘住順序：`_local_probed_activity` 必須在 apply **之前**寫好，否則每個
    tick 送出的都是上一個 tick 的狀態。這裡的替身記的就是 apply 當下讀到的值。
    """
    probe_loop.bot.append(_game_probe())
    probe_loop.rpc.append(_claude_probe())
    _run_loop(probe_loop)
    assert len(probe_loop.applied) == 1
    mirrored = probe_loop.applied[0]
    assert isinstance(mirrored, b.discord.Game), repr(mirrored)
    assert mirrored.name == "Arknights: Endfield"
    assert probe_loop.rpc_applied == [_claude_probe()], probe_loop.rpc_applied


def test_a_failing_signal_probe_does_not_kill_the_loop(probe_loop, capsys):
    """SMTC 那一段會起 PowerShell 子行程、會逾時、會被防毒擋——這是**預期會
    發生**的事，不是意外。一個 tick 抓不到訊號就跳過一個 tick，不是讓 presence
    從此凍住。代用的 signals 也必須是真的形狀，否則下面兩個對映會拿到一個自己
    不認得的 dict。
    """
    probe_loop.signals.append(RuntimeError("SMTC 子行程逾時"))
    probe_loop.signals.append({"game": "Endfield", "music_strict": None,
                               "music_any": None, "claude": None})
    probe_loop.bot.extend([None, _game_probe()])
    _run_loop(probe_loop, ticks=2)
    assert len(probe_loop.seen_signals) == 2, "第一個 tick 炸掉就沒有第二個了"
    assert all(value is None for value in probe_loop.seen_signals[0].values()), (
        probe_loop.seen_signals[0])
    assert "presence probe iteration failed" in capsys.readouterr().err


def test_a_failing_signal_mapper_does_not_kill_the_loop(probe_loop, capsys):
    """迴圈裡那兩行帶著一段註解，記的是一次真的事故：兩個對映原本在所有 try
    之外，所以 SMTC 回一個 `title=None` 的媒體就能讓整個迴圈死掉，presence 從此
    凍在最後狀態——而 bot 其他功能一切正常，外觀上完全看不出來。

    **在這支之前，把那兩行搬回 try 外面不會有任何測試變紅。**

    另一半是「失敗時兩邊一起清掉」：對映炸掉時 `bot_probe` 和 `rpc_probe` 同時
    設成 None，不可以只清一邊留另一邊用上一輪的值。
    """
    probe_loop.bot.append(TypeError("'NoneType' object is not subscriptable"))
    probe_loop.bot.append(_game_probe())
    probe_loop.rpc.append(_claude_probe())
    _run_loop(probe_loop, ticks=2)
    assert len(probe_loop.applied) == 2
    assert probe_loop.applied[0] is None, "對映失敗的那個 tick 不該顯示任何狀態"
    assert isinstance(probe_loop.applied[1], b.discord.Game)
    assert probe_loop.rpc_applied[0] is None, "只清了 bot 那一邊"
    assert "presence signal mapping failed" in capsys.readouterr().err


@pytest.mark.parametrize("make_error, tail", [
    (lambda: ConnectionResetError("transport closing"),
     "transient in probe loop"),
    (lambda: b.aiohttp.ClientError("Cannot write to closing transport"),
     "transient in probe loop"),
    (lambda: RuntimeError("something nobody predicted"),
     "failed in probe loop"),
])
@pytest.mark.parametrize("side", ["presence", "rpc"])
def test_a_failing_apply_does_not_kill_the_loop(probe_loop, capsys, side,
                                                make_error, tail):
    """兩邊各自一個 try 的理由：其中一邊炸掉，不可以讓另一邊這個 tick 被跳過。

    而「暫時性斷線」要跟「沒人預料到的錯」分得開，否則重連期間的正常雜訊會把
    真正的意外淹掉——重連在這裡是常態，這個迴圈每 N 秒就醒一次。
    """
    queue = (probe_loop.apply_raises if side == "presence"
             else probe_loop.rpc_raises)
    queue.append(make_error())
    _run_loop(probe_loop, ticks=2)
    assert len(probe_loop.applied) == 2, "presence apply 沒有走到下一個 tick"
    assert len(probe_loop.rpc_applied) == 2, "rpc apply 被另一邊的失敗連坐了"
    assert f"{side} apply {tail}" in capsys.readouterr().err


def test_the_probe_line_only_speaks_when_the_local_probe_changes(probe_loop,
                                                                 capsys):
    """這一行是「為什麼 bot 現在顯示這個」在 supervisor 終端上唯一的線索，而它
    掛在一個每 N 秒醒一次的迴圈上——不節流的話，穩態下它就是整份 log（第一組
    量過同一個形狀：11,768 行裡 11,314 行是同一句）。

    它刻意只看 bot 那一側的 key：RPC 那一側有自己的節流輸出（見第一組），所以
    這裡故意讓 RPC 每個 tick 都變，而本機探測沒變就該一行都不多印。
    """
    probe_loop.bot.append(_game_probe())
    probe_loop.rpc.extend([_claude_probe(), None, _claude_probe()])
    _run_loop(probe_loop, ticks=3)
    out = capsys.readouterr().out
    assert out.count("local probe ->") == 1, out


@pytest.mark.parametrize("second", [
    {"kind": "claude", "name": "Claude Code"},      # 換了種類
    {"kind": "playing", "name": "另一款遊戲"},       # 只換了名字
    None,                                           # 什麼都沒在跑了
])
def test_a_changed_probe_is_announced_again(probe_loop, capsys, second):
    """節流的另一半：真的變了就要說。名字換掉也算變——key 是 `種類|名稱`，
    只比種類的話「換一款遊戲」會安靜地不見。
    """
    probe_loop.bot.extend([_game_probe(), second])
    _run_loop(probe_loop, ticks=2)
    out = capsys.readouterr().out
    assert out.count("local probe ->") == 2, out


@pytest.mark.parametrize("probe, shape", [
    ({"kind": "playing", "name": "Endfield"}, "game"),
    ({"kind": "claude", "name": "Claude Code"}, "game"),
    ({"kind": "listening", "name": "Bohemian Rhapsody"}, "listening"),
    ({"kind": "watching", "name": "沒人接線過的種類"}, "none"),
    (None, "none"),
])
def test_the_kind_decides_what_the_bot_shows(probe_loop, probe, shape):
    """`claude` 刻意也走 `discord.Game`（顯示成 Playing Claude Code），而
    **沒見過的 kind 一律當成沒有 activity**——這是 fail-closed：新增一種訊號卻
    忘了在這裡接線，結果是「什麼都不顯示」，不是把一個內部 kind 當名字送出去。
    """
    if probe is not None:
        probe_loop.bot.append(probe)
    _run_loop(probe_loop)
    activity = probe_loop.applied[0]
    if shape == "game":
        assert isinstance(activity, b.discord.Game), repr(activity)
        assert activity.name == probe["name"]
    elif shape == "listening":
        assert isinstance(activity, b.discord.Activity), repr(activity)
        assert activity.type is b.discord.ActivityType.listening
        assert activity.name == probe["name"]
    else:
        assert activity is None, repr(activity)


def test_every_tick_ends_with_the_configured_interval(probe_loop):
    """間隔是設定檔來的（`presence_probe_interval_sec`）。在這裡寫死一個數字，
    `/probe_status` 印出來的那個間隔就會開始說謊，而兩個數字沒有任何東西在比。
    """
    _run_loop(probe_loop, ticks=3)
    assert probe_loop.sleeps == [b.PRESENCE_PROBE_INTERVAL_SEC] * 3


def _fallback_signal_keys() -> set:
    """挖出迴圈裡那個代用 signals dict 的 key（訊號探測炸掉時用的那一個）。"""
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    loops = [node for node in ast.walk(tree)
             if isinstance(node, ast.AsyncFunctionDef)
             and node.name == "_presence_probe_loop"]
    assert len(loops) == 1, f"找到 {len(loops)} 個 _presence_probe_loop"
    literals = [node for node in ast.walk(loops[0]) if isinstance(node, ast.Dict)]
    assert len(literals) == 1, (
        f"迴圈裡的 dict 常值有 {len(literals)} 個，這支挖到的不一定是代用訊號")
    return {key.value for key in literals[0].keys}


def test_the_substitute_signals_have_the_same_shape_as_a_real_probe(monkeypatch):
    """那個代用 dict 是 `probe_signals_async` 回傳形狀的**手抄本**，而兩份抄本
    沒有任何東西在對帳。多一個訊號欄位、或改掉一個欄位名，代用的那一份就會
    在真正需要它的時候（探測炸掉時）交出一個對映看不懂的 dict——而那條路徑正是
    平常不會執行到的那條。
    """
    pp = b.presence_probe

    async def _no_smtc(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pp, "probe_game_process", lambda: None)
    monkeypatch.setattr(pp, "probe_smtc_raw_async", _no_smtc)
    monkeypatch.setattr(pp, "probe_foreground_music", lambda: None)
    monkeypatch.setattr(pp, "probe_claude_code", lambda: None)
    real = set(asyncio.run(pp.probe_signals_async()))
    assert real, "positive control：真的探測回了一個空 dict，下面的比對沒有意義"
    assert _fallback_signal_keys() == real
