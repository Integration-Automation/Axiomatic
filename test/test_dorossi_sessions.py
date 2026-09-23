"""`/dorossi session …`——整個工作階段管理介面，一行都沒有被跑過。

這支指令是擁有者手上唯一能對「已經存在的對話」動手的東西：開新的、切過去、清空、
刪掉、封存、改標籤、匯出、接續未完成的自走任務。三支函式
（`mcmd_session`／`_dorossi_apply_session_action`／`_dorossi_export_session`）合計
**131 行敘述**，量覆蓋率時**幾乎整片是零**——全樹唯一碰過這一帶的測試碰的是唯讀的
`_dorossi_render_session_list`，也就是「列出來長什麼樣」，而不是「按下去會發生什麼」。

弄錯的後果全都不會當場爆炸：刪錯一個 slot 就是刪掉了（沒有 undo，`.backup/` 那套只
保護佇列檔），切換沒存回磁碟就是下一輪又回到舊的那個，封存之後 `active` 指到一個已
封存的 slot 就是「開新對話結果接到一個看不見的東西上」。

#### 三個結構性的決定，各自有一支反例測試

**一、列表是刻意無鎖的。** 自走迴圈一跑就是幾個小時，全程握著 `_dorossi_state_lock`。
列表若也去搶那把鎖，`/dorossi session list` 會在**整個長任務期間**只回一句「忙線中」
——而那正是最需要看清單的時候。所以唯讀那條路直接讀、容忍偶爾讀到舊值。

**二、`continue` 與 `export` 都必須在拿鎖之前就 return。** `continue` 要的是**另一把**
鎖（該 session 自己的），而且它會跑上好幾個小時；`export` 只讀不寫。任何一支掉進下面
那段「短 RMW」區，都會把一把設計成握幾毫秒的鎖握成幾小時。

**三、回覆送在鎖外面。** `safe_reply` 要等對話平台回應，被限流時可以卡上好幾秒；在鎖
裡面送就等於讓每一輪問答排在一次網路往返後面。這條沒辦法靠「現在的分支都對」守住
——會漂的是**未來新增的分支**，所以另外用靜態守門釘住形狀（§11）。

#### 一整半的程式碼在今天是走不到的，而它必須留著

`_paths_visible_here` 第一條就是「提問者是擁有者 → True」，而 `mcmd_session` 的第一行
是「不是 `DOROSSI_USER_ID` 就拒絕」，且 `OWNER_USER_ID = DOROSSI_USER_ID`。兩件事合起
來：**任何走得到列表與匯出的人都是擁有者，所以 `reveal_paths` 永遠是 True**——把完整
主機路徑收成末段名稱的那一半，透過 `mcmd_session` 進來時一次都不會執行。

那不是「可以刪掉的死碼」。`discord_bot.py` 在 `OWNER_USER_ID = DOROSSI_USER_ID` 上面
就寫著這兩個 id 是「同一個人，但語意不同……未來若分家時」，也就是這道收斂是為了那一天
留的。所以本檔的做法是**直接呼叫底下那兩支**並自己給 `reveal_paths`／把
`_paths_visible_here` 換掉，讓那一半有真的斷言；不是繞過守門，是承認它今天只有從下面
才進得去。
"""
from __future__ import annotations

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
import dorossi_backend as db  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
UID = str(b.DOROSSI_USER_ID)


# ---------------------------------------------------------------------------
# 夾具
# ---------------------------------------------------------------------------

class _Msg:
    """最小的 `discord.Message` 替身。

    `mcmd_session` 只讀 `author.id` 與 `channel`；回覆走被換掉的 `safe_reply`。
    """

    def __init__(self, uid: int | None = None, channel=None):
        self.author = types.SimpleNamespace(id=b.DOROSSI_USER_ID if uid is None else uid)
        self.channel = channel if channel is not None else types.SimpleNamespace(id=0)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """把會落地的兩個檔案指到 `tmp_path`，並換掉三個會碰到外界的東西。

    `dorossi_session.json` 是擁有者**所有**對話的續接資訊——跑測試把正式那一份寫髒
    一次就夠了。`_dorossi_state_lock` 每支測試換一把新的：`asyncio.Lock` 第一次
    `acquire` 時才綁事件迴圈，共用同一把會讓第二支測試撞上「綁在別的迴圈上」。
    """
    rec = types.SimpleNamespace(
        replies=[], kwargs=[], locked_at_reply=[], store_at_reply=[], resumes=[],
    )

    async def fake_reply(message, content=None, **kwargs):
        rec.replies.append("" if content is None else str(content))
        rec.kwargs.append(kwargs)
        # 兩件事在**送出的那一刻**才量得到：鎖還握著嗎？異動存進磁碟了嗎？
        rec.locked_at_reply.append(b._dorossi_state_lock.locked())
        try:
            rec.store_at_reply.append(rec.store.read_text(encoding="utf-8"))
        except OSError:
            rec.store_at_reply.append(None)

    async def fake_resume(message, sid):
        rec.resumes.append(sid)

    monkeypatch.setattr(b, "safe_reply", fake_reply)
    monkeypatch.setattr(b, "_dorossi_resume_loop", fake_resume)
    monkeypatch.setattr(b, "_dorossi_state_lock", asyncio.Lock())
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", tmp_path / "dorossi_session.json")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "dorossi_events.ndjson")
    rec.store = tmp_path / "dorossi_session.json"
    rec.tmp = tmp_path
    rec.text = lambda: "\n".join(rec.replies)
    return rec


def _seed(count: int = 1, labels=None, **slot_fields) -> dict:
    """用真正的 primitives 建 `count` 個 slot 並存檔；回傳 in-memory 的 state。

    刻意不自己手寫 JSON：存放檔進來時會先過 `_dorossi_migrate_state`，手寫的形狀
    很容易在遷移那一步被改掉，於是測試量到的其實是遷移的結果而不是自己擺的輸入。
    """
    state: dict = {}
    record = db._dorossi_user_record(state, UID)
    labels = labels or [None] * count
    for i in range(count):
        sid = db._dorossi_new_session(record, label=labels[i])
        # 每個 slot 的 last_used 明確錯開，讓「最近使用的那個」是可預期的。
        record["sessions"][sid]["last_used"] = 1_000.0 + i
        record["sessions"][sid].update(slot_fields)
    db._dorossi_save_state(state)
    return state


def _disk() -> dict:
    """磁碟上那一份——所有「有沒有真的存回去」的斷言都讀這裡。"""
    return db._dorossi_load_state()


def _rec_on_disk() -> dict:
    return _disk().get(UID) or {}


def _sessions_on_disk() -> dict:
    return _rec_on_disk().get("sessions") or {}


def _run(coro):
    return asyncio.run(coro)


async def _holding_the_lock(inner):
    """模擬「有一輪問答正握著狀態鎖」，再跑 `inner()`。"""
    await b._dorossi_state_lock.acquire()
    try:
        return await inner()
    finally:
        b._dorossi_state_lock.release()


# ---------------------------------------------------------------------------
# 一、身分閘
# ---------------------------------------------------------------------------

def test_the_counter_skips_ids_that_do_not_follow_the_pattern():
    """手改或舊版留下的工作階段編號（`legacy`、`s2x`）不參與計數，也不能讓計數壞掉——
    少了比對失敗那一格的處理，一筆這樣的編號就讓重建計數整個丟例外。"""
    sessions = {"s3": {}, "legacy": {}, "s10": {}, "s2x": {}}
    assert db._dorossi_derive_next_seq(sessions) == 11


def test_a_stranger_gets_a_generic_refusal_and_the_store_is_never_read(env, monkeypatch):
    """拒絕要發生在**讀狀態之前**。

    存放檔裡是擁有者所有對話的續接資訊；把它讀進記憶體再判斷身分，等於把「誰能
    讓 bot 去碰那個檔案」交給後面每一條分支各自記得。
    """
    def _boom():
        raise AssertionError("身分閘之前不該讀狀態")

    monkeypatch.setattr(b, "_dorossi_load_state", _boom)
    _run(b.mcmd_session(_Msg(uid=b.DOROSSI_USER_ID + 1), ""))
    assert env.replies == ["此功能僅限特定使用者。"]


def test_the_gate_runs_before_the_verb_is_even_parsed(env):
    """一個**破壞性**動詞也一樣擋在門外，而且什麼都沒改。"""
    _seed(1)
    before = json.loads(env.store.read_text(encoding="utf-8"))
    _run(b.mcmd_session(_Msg(uid=b.DOROSSI_USER_ID + 1), "s1 delete"))
    assert env.replies == ["此功能僅限特定使用者。"]
    assert json.loads(env.store.read_text(encoding="utf-8")) == before


# ---------------------------------------------------------------------------
# 二、列表是刻意無鎖的
# ---------------------------------------------------------------------------

def test_listing_answers_while_a_long_running_turn_holds_the_state_lock(env):
    """**本檔最重要的一條。** 自走迴圈會握著狀態鎖好幾個小時。

    列表若去搶鎖，擁有者在整個長任務期間都只會拿到「忙線中」——偏偏那正是他最想
    知道「哪個 slot 在跑」的時候。這支測試在鎖被握著的情況下要求拿到真的清單。
    """
    _seed(2)
    _run(_holding_the_lock(lambda: b.mcmd_session(_Msg(), "")))
    assert "忙線中" not in env.text()
    assert "`s1`" in env.text() and "`s2`" in env.text()


def test_an_empty_store_lists_the_how_to_open_one_hint(env):
    _run(b.mcmd_session(_Msg(), ""))
    assert "目前沒有 session" in env.text()


def test_listing_shows_the_full_directory_when_the_surface_allows_it(env, monkeypatch):
    """可露路徑的表面：完整主機路徑原樣印出（擁有者裁定 2026-08-25／08-27）。"""
    workdir = env.tmp / "工作目錄名"
    workdir.mkdir()
    _seed(1, cc_cwd=str(workdir))
    monkeypatch.setattr(b, "_paths_visible_here", lambda _m: True)
    _run(b.mcmd_session(_Msg(), ""))
    assert str(workdir) in env.text()


def test_listing_hides_the_full_directory_when_the_surface_does_not(env, monkeypatch):
    """不可露路徑的表面：只剩末段名稱，完整路徑一個字都不能出現。

    今天從 `mcmd_session` 進來的人一定是擁有者，所以這一半走不到（見模組 docstring）
    ——只能把 `_paths_visible_here` 換掉才量得到。那一半必須留著：兩個 id 未來分家
    的那天，它就是唯一擋住主機路徑外流的東西。
    """
    workdir = env.tmp / "工作目錄名"
    workdir.mkdir()
    _seed(1, cc_cwd=str(workdir))
    monkeypatch.setattr(b, "_paths_visible_here", lambda _m: False)
    _run(b.mcmd_session(_Msg(), ""))
    assert "工作目錄名" in env.text(), "末段名稱應該還在"
    assert str(workdir) not in env.text(), "完整主機路徑外流了"
    assert str(env.tmp) not in env.text()


def test_the_surface_decision_is_not_hardcoded(env, monkeypatch):
    """兩種表面必須得到**不同**的輸出。

    上面兩支各自單看都可能被一個寫死 `reveal_paths=True`（或 False）的版本騙過去：
    寫死 True 時「末段名稱還在」照樣成立，因為完整路徑裡本來就含末段名稱。
    """
    workdir = env.tmp / "工作目錄名"
    workdir.mkdir()
    _seed(1, cc_cwd=str(workdir))
    seen = []
    for verdict in (True, False):
        env.replies.clear()
        monkeypatch.setattr(b, "_paths_visible_here", lambda _m, v=verdict: v)
        _run(b.mcmd_session(_Msg(), ""))
        seen.append(env.text())
    assert seen[0] != seen[1], (
        "可露路徑與不可露路徑的表面拿到一模一樣的清單——`reveal_paths` 很可能被寫死了")


@pytest.mark.parametrize("visible", [True, False])
def test_the_extra_accessible_directory_follows_the_same_rule(env, monkeypatch, visible):
    """`cc_extra_dir` 是**第二個**會帶主機路徑的欄位，而它走的是另外一組 if。

    工作目錄那一組寫對、額外目錄那一組寫錯，看起來一切正常——畫面上照樣兩行，只是
    其中一行多了整串主機路徑。
    """
    extra = env.tmp / "額外目錄名"
    extra.mkdir()
    _seed(1, cc_extra_dir=str(extra))
    monkeypatch.setattr(b, "_paths_visible_here", lambda _m: visible)
    _run(b.mcmd_session(_Msg(), ""))
    assert "額外可存取" in env.text()
    assert (str(extra) in env.text()) is visible


def test_a_session_carrying_an_old_system_prompt_is_flagged(env):
    """指紋對不上時列表要出聲；指紋**還沒開始記**的舊工作階段則要保持安靜。

    把 unknown 當成 stale 會讓每一個舊工作階段都亮紅燈，而會亂叫的提示最後會被人
    無視——這條理由在 `session_prompt_state` 的 docstring 裡寫過一次，這裡釘住它。
    """
    _seed(1, sys_prompt_fp="這不是現在那一份指紋")
    _run(b.mcmd_session(_Msg(), ""))
    assert "舊版設定" in env.text()

    env.replies.clear()
    _seed(1)  # 沒有 `sys_prompt_fp` ＝ unknown
    _run(b.mcmd_session(_Msg(), ""))
    assert "舊版設定" not in env.text(), "指紋還沒開始記的舊工作階段被誤報成過期"


@pytest.mark.parametrize("junk", ["整包壞掉的字串", ["不是 dict"], 7, True])
def test_a_malformed_user_record_degrades_the_listing_instead_of_killing_it(junk):
    """清單是**唯讀**的，而擁有者正是在狀態出問題時最需要它。

    壞掉的紀錄要降級成「目前沒有 session」那一段（含用法提示），不是讓整份清單列
    不出來。`rec` 是 `True` 也算——bool 是真值而且沒有 `.get`。
    """
    text = b._dorossi_render_session_list({UID: junk}, UID)
    assert "目前沒有 session" in text
    assert "刪除。" in text, "降級之後連用法提示都沒了"


@pytest.mark.parametrize("junk", [["s1"], "s1", 7, ("s1",), {"s1"}])
def test_a_sessions_field_that_is_not_a_dict_degrades_the_listing_too(junk):
    """**這一半原本是真的會炸的。** 真值的非 dict（list／str／非零 int／tuple／set）
    會穿過舊寫法的 `or {}` 與 `if not sessions`，一路跑進 for 迴圈才由 `sessions[sid]`
    丟 `TypeError`——那不是防禦性整理，那是一個活的缺陷。
    """
    state = {UID: {"active": None, "next_seq": 1, "sessions": junk}}
    assert "目前沒有 session" in b._dorossi_render_session_list(state, UID)


def test_a_healthy_listing_is_unchanged_by_the_robustness_guard(env):
    """必須放行的那一面：正常資料的輸出一個字都不能變（多支測試在比對字面）。"""
    _seed(2, labels=["甲", "乙"])
    text = b._dorossi_render_session_list(_disk(), UID)
    assert "`s1`" in text and "`s2`" in text
    assert "甲" in text and "乙" in text
    assert "目前沒有 session" not in text


def test_a_session_scoped_effort_override_shows_up_in_the_list(env):
    """微調是 slot 層級的；列表不顯示的話，擁有者會拿一個記得舊力度的 slot 繼續問，
    而畫面上完全看不出來。"""
    _seed(1, tune_effort="high")
    _run(b.mcmd_session(_Msg(), ""))
    assert "思考力度" in env.text()


# ---------------------------------------------------------------------------
# 三、`continue` 走另一條路，不進短鎖區
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verb", list(b._DOROSSI_CONTINUE_VERBS))
def test_every_continue_alias_resumes_the_active_session(env, verb):
    """四個別名都要通到同一支。少一個 → 擁有者打了字卻只拿到一句用法說明。"""
    _seed(2)
    _run(b.mcmd_session(_Msg(), verb))
    assert env.resumes == [None], f"`{verb}` 沒有接到接續流程"
    assert env.replies == []


def test_a_continue_after_an_id_resumes_that_session(env):
    _seed(3)
    _run(b.mcmd_session(_Msg(), "s2 continue"))
    assert env.resumes == ["s2"]


def test_continue_does_not_wait_for_the_state_lock(env):
    """接續要的是**該 session 自己**那把鎖，不是狀態短鎖。

    掉進下面的短鎖區會有兩個後果：一是被還在跑的那一輪擋掉（回「忙線中」），二是
    真的拿到之後會把一把設計成握幾毫秒的鎖握上好幾個小時。
    """
    _seed(1)
    _run(_holding_the_lock(lambda: b.mcmd_session(_Msg(), "continue")))
    assert env.resumes == [None]
    assert "忙線中" not in env.text()


# ---------------------------------------------------------------------------
# 四、匯出也不進短鎖區
# ---------------------------------------------------------------------------

def test_export_does_not_wait_for_the_state_lock(env):
    """匯出只讀不寫，沒有理由排在一輪問答後面。"""
    _seed(1)
    _run(_holding_the_lock(lambda: b.mcmd_session(_Msg(), "s1 export")))
    assert "忙線中" not in env.text()
    assert env.kwargs and "file" in env.kwargs[-1]


# ---------------------------------------------------------------------------
# 五、動詞表
# ---------------------------------------------------------------------------

def _verb_cases():
    """`(輸入, 檢查函式)`——每一筆都驗**磁碟上的結果**，不是回覆的字面。"""
    def _switched(sessions, record):
        assert record["active"] == "s1"

    def _reset(sessions, record):
        assert "cc_session_id" not in sessions["s1"], "重置沒有把後端續接丟掉"
        assert "s1" in sessions, "重置不應該刪掉 slot"

    def _deleted(sessions, record):
        assert "s1" not in sessions and "s2" in sessions

    def _archived(sessions, record):
        assert sessions["s1"].get("archived") is True

    def _renamed(sessions, record):
        assert sessions["s1"].get("label") == "新名字"

    def _label_cleared(sessions, record):
        assert "label" not in sessions["s1"]

    return [
        ("s1", _switched),
        ("s1 reset", _reset),
        ("s1 clear", _reset),
        ("s1 delete", _deleted),
        ("s1 archive", _archived),
        ("s1 rename 新名字", _renamed),
        ("s1 label 新名字", _renamed),
        ("s1 rename", _label_cleared),
        ("S1 RESET", _reset),
    ]


@pytest.mark.parametrize("rest, check", _verb_cases(),
                         ids=[c[0] for c in _verb_cases()])
def test_the_verb_table_reaches_the_right_action(env, rest, check):
    """一個動詞接錯分支，使用者會拿到一句正確的確認訊息和一個錯誤的結果。

    `S1 RESET` 那一筆釘的是大小寫：id 與動詞都經過 `lower()`，手機鍵盤的自動大寫
    不該把 `/dorossi session reset` 變成一句用法說明。
    """
    _seed(2, labels=["原名字", None], cc_session_id="abc")
    # 先切到 s2，這樣 `s1` 那一筆的「切換過去」才是可觀察的變化而不是本來就如此。
    state = _disk()
    state[UID]["active"] = "s2"
    db._dorossi_save_state(state)

    _run(b.mcmd_session(_Msg(), rest))
    assert "忙線中" not in env.text()
    check(_sessions_on_disk(), _rec_on_disk())


def test_an_unknown_verb_after_a_real_id_explains_the_id_form(env):
    _seed(1)
    _run(b.mcmd_session(_Msg(), "s1 婆婆媽媽"))
    assert "用法" in env.text() and "switch" in env.text()
    assert _sessions_on_disk()["s1"].get("archived") is None


def test_an_unknown_head_explains_the_whole_grammar(env):
    """不是 id、也不是 `new` ——例如打錯成 `sesion` 或貼了一句話。"""
    _seed(1)
    _run(b.mcmd_session(_Msg(), "隨便打的東西"))
    assert "用法" in env.text() and "session new" in env.text()


def test_nothing_is_written_when_the_input_is_not_understood(env):
    _seed(1)
    before = env.store.read_text(encoding="utf-8")
    _run(b.mcmd_session(_Msg(), "隨便打的東西"))
    assert env.store.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# 六、`new` 與 `cwd=`
# ---------------------------------------------------------------------------

def test_new_opens_a_session_and_switches_to_it(env):
    _seed(1)
    _run(b.mcmd_session(_Msg(), "new"))
    assert set(_sessions_on_disk()) == {"s1", "s2"}
    assert _rec_on_disk()["active"] == "s2"


def test_a_label_survives_into_the_listing(env):
    """標籤是自由文字（可含空格），所以工作目錄只能靠 `cwd=` 這個鍵切。"""
    _run(b.mcmd_session(_Msg(), "new 週末 的 實驗"))
    assert _sessions_on_disk()["s1"]["label"] == "週末 的 實驗"
    env.replies.clear()
    _run(b.mcmd_session(_Msg(), ""))
    assert "週末 的 實驗" in env.text()


def test_a_usable_cwd_is_applied_to_the_new_session(env):
    target = env.tmp / "指定目錄"
    target.mkdir()
    _run(b.mcmd_session(_Msg(), f"new 標籤 cwd={target}"))
    slot = _sessions_on_disk()["s1"]
    assert slot["cc_cwd"] == str(target.resolve())
    assert slot["label"] == "標籤"
    assert "已套用" in env.text()


def test_a_rejected_cwd_is_reported_generically_and_logged_in_full(env, capsys):
    """兩半都要驗：對外泛用，細節進 stderr。

    被拒的那一串是**未經控制的使用者輸入**——貼什麼進來它就是什麼。把它原樣回音
    到訊息裡，等於讓輸入的人決定 bot 送出去的內容。所以對外只說「無法使用」，
    原字串只寫 stderr。反過來，stderr 那一半若也省掉，擁有者就完全查不出自己
    貼錯在哪裡——那是這條分支唯一的診斷來源。
    """
    bogus = str(env.tmp / "這個目錄不存在_xyzzy")
    _run(b.mcmd_session(_Msg(), f"new cwd={bogus}"))
    assert "無法使用" in env.text()
    assert bogus not in env.text(), "被拒的原始輸入被回音到訊息裡了"
    assert "cc_cwd" not in _sessions_on_disk()["s1"]
    # 診斷走 `!r`（本專案的既定寫法），所以比對的是 `repr()` 之後的字串——直接拿
    # 原字串比會因為反斜線被跳脫而找不到，然後把一支其實是綠的測試判成紅的。
    assert repr(bogus) in capsys.readouterr().err, "被拒的原始輸入沒有寫進 stderr"


def test_a_label_before_cwd_is_kept_and_the_path_is_not_lowercased(env):
    """`cwd=` **之後整段**都是路徑，而且永不 `lower()`——大小寫在這台機器上不影響
    解析，但在別的檔案系統上會，且回寫進 slot 的字串會一路傳給後端。"""
    target = env.tmp / "MixedCase目錄"
    target.mkdir()
    _run(b.mcmd_session(_Msg(), f"new My Label cwd={target}"))
    slot = _sessions_on_disk()["s1"]
    assert slot["label"] == "My Label"
    assert "MixedCase" in slot["cc_cwd"]


# ---------------------------------------------------------------------------
# 七、落地與鎖的邊界
# ---------------------------------------------------------------------------

def test_the_change_is_already_on_disk_when_the_confirmation_is_sent(env):
    """順序要是「改 → 存 → 回覆」，不是「改 → 回覆 → 存」。

    後者在「送出的那一刻行程被砍掉」時會送出一句確認，而異動並沒有留下——使用者
    被告知成功，下一次開機卻回到舊狀態。長期無人值守的主機本來就會突然重開，所以這不是
    假設性的時序。
    """
    _seed(2)
    _run(b.mcmd_session(_Msg(), "s1 delete"))
    assert env.store_at_reply, "沒有送出任何回覆"
    at_reply = json.loads(env.store_at_reply[-1])
    assert "s1" not in at_reply[UID]["sessions"], (
        "回覆送出去的時候，刪除還沒有寫進磁碟")


def test_the_confirmation_is_sent_after_the_lock_is_released(env):
    """`safe_reply` 要等對話平台；被限流時可以卡好幾秒。在鎖裡面送，等於讓每一輪
    問答都排在一次網路往返後面。"""
    _seed(1)
    _run(b.mcmd_session(_Msg(), "s1 reset"))
    assert env.locked_at_reply == [False], (
        f"回覆是在握著狀態鎖的時候送出去的：{env.locked_at_reply}")


def test_a_held_lock_answers_busy_and_changes_nothing(env):
    """短鎖被握著時回「忙線中」，而且什麼都沒改。

    ※ 這一支**分不開**兩道守門：鎖被握到底的話，就算把 `locked()` 那道快篩拿掉，
    下面的 `wait_for` 一樣會逾時、一樣回同一句話（只是慢了幾秒）。分得開的輸入在
    下一支。
    """
    _seed(2)
    before = env.store.read_text(encoding="utf-8")
    _run(_holding_the_lock(lambda: b.mcmd_session(_Msg(), "s1 delete")))
    assert env.text() == "Dorossi 忙線中，請稍後再試。"
    assert env.store.read_text(encoding="utf-8") == before


def test_a_lock_that_frees_a_moment_later_still_answers_busy(env):
    """把上一支分不開的那兩道守門分開。

    唯一分得開的輸入是**馬上就會放掉的鎖**：有 `locked()` 快篩時回「忙線中」而且
    什麼都沒動；少了它則會安靜地等到拿得到再執行——使用者按下去先是沒反應，然後
    那個破壞性動作在他已經改去做別的事之後才生效。
    """
    _seed(2)

    async def go():
        await b._dorossi_state_lock.acquire()

        async def _free_it_soon():
            await asyncio.sleep(0.02)
            b._dorossi_state_lock.release()

        task = asyncio.create_task(_free_it_soon())
        await b.mcmd_session(_Msg(), "s1 delete")
        await task

    _run(go())
    assert env.text() == "Dorossi 忙線中，請稍後再試。"
    assert "s1" in _sessions_on_disk(), (
        "指令沒有回報忙線，而是安靜地排隊等鎖——按下去沒反應，然後過一陣子才生效")


def test_a_lock_that_never_frees_times_out_and_answers_busy(env, monkeypatch):
    """`locked()` 那道快篩之外還有一道 `wait_for` 逾時。

    兩道看起來重複，其實分得開：`locked()` 只看**當下**，而拿鎖是 await——在那個
    await 點上別人可以先搶到。這支用一把「說自己沒被鎖、但永遠給不出來」的假鎖
    走到第二道；它同時釘住「逾時之後不可以去 release 一把沒拿到的鎖」。
    """
    class _NeverFree:
        def locked(self):
            return False

        async def acquire(self):
            await asyncio.sleep(3600)

        def release(self):
            raise AssertionError("release 了一把從來沒拿到的鎖")

    monkeypatch.setattr(b, "_dorossi_state_lock", _NeverFree())
    monkeypatch.setattr(b, "DOROSSI_SESSION_LOCK_TIMEOUT_SEC", 0.01)
    _seed(1)
    _run(b.mcmd_session(_Msg(), "s1 reset"))
    assert env.text() == "Dorossi 忙線中，請稍後再試。"


def test_the_lock_is_released_even_when_applying_the_action_raises(env, monkeypatch):
    """沒有 `finally` 的話，一次例外會讓狀態鎖**永遠**握著：之後每一輪問答、每一個
    管理指令都回「忙線中」，而且只有重啟 bot 才會好。"""
    def _boom(*_a, **_k):
        raise RuntimeError("套用動作時炸了")

    monkeypatch.setattr(b, "_dorossi_apply_session_action", _boom)
    _seed(1)

    async def go():
        with pytest.raises(RuntimeError):
            await b.mcmd_session(_Msg(), "s1 reset")
        assert not b._dorossi_state_lock.locked(), "例外之後狀態鎖沒有被釋放"

    _run(go())


# ---------------------------------------------------------------------------
# 八、`_dorossi_apply_session_action`——純函式那一層
# ---------------------------------------------------------------------------

def _fresh(count: int = 1, **slot_fields) -> dict:
    state: dict = {}
    record = db._dorossi_user_record(state, UID)
    for i in range(count):
        sid = db._dorossi_new_session(record, label=None)
        record["sessions"][sid]["last_used"] = 1_000.0 + i
        record["sessions"][sid].update(slot_fields)
    return state


def test_new_returns_the_id_and_marks_it_active():
    state = _fresh(1)
    reply = b._dorossi_apply_session_action(state, UID, ("new", None, None))
    assert state[UID]["active"] == "s2"
    assert "`s2`" in reply


def test_new_with_a_label_names_it_in_the_confirmation():
    state: dict = {}
    reply = b._dorossi_apply_session_action(state, UID, ("new", "實驗", None))
    assert "實驗" in reply
    assert state[UID]["sessions"]["s1"]["label"] == "實驗"


def test_an_unknown_id_is_reported_without_guessing():
    """不要退回「用最近那個」——那會讓一個打錯的 id 安靜地去動別人的對話。"""
    state = _fresh(2)
    reply = b._dorossi_apply_session_action(state, UID, ("delete", "s99"))
    assert "找不到" in reply
    assert set(state[UID]["sessions"]) == {"s1", "s2"}


def test_a_user_with_no_record_at_all_reads_as_no_such_session():
    reply = b._dorossi_apply_session_action({}, UID, ("switch", "s1"))
    assert "找不到" in reply


def test_a_malformed_user_record_is_refused_instead_of_raising():
    """畸形紀錄要走**同一句泛用訊息**，不能讓 `AttributeError` 往上冒。

    今天從 `mcmd_session` 進來的 state 一定是遷移過的（每個紀錄都是 dict），所以
    這條在線上走不到——但它是寫給「哪天有人從別的地方呼叫這支」準備的，而一道從來
    不會生效的防護等於沒有防護。
    """
    for junk in ("整包壞掉的字串", ["不是 dict"], 7):
        reply = b._dorossi_apply_session_action({UID: junk}, UID, ("switch", "s1"))
        assert "找不到" in reply, f"畸形紀錄 {junk!r} 沒有被擋下來"


def test_a_record_whose_sessions_is_not_a_dict_is_refused_too():
    reply = b._dorossi_apply_session_action(
        {UID: {"active": None, "sessions": "壞掉了"}}, UID, ("switch", "s1"))
    assert "找不到" in reply


def test_switch_touches_last_used():
    """`last_used` 同時是列表排序與刪除後改指哪一個的依據；切過去卻不更新，等於
    「剛剛才用的那個」排在最後面。"""
    state = _fresh(2)
    before = state[UID]["sessions"]["s1"]["last_used"]
    b._dorossi_apply_session_action(state, UID, ("switch", "s1"))
    assert state[UID]["active"] == "s1"
    assert state[UID]["sessions"]["s1"]["last_used"] > before


def test_reset_keeps_the_id_and_drops_the_continuity():
    state = _fresh(1, cc_session_id="abc", api_history=[{"role": "user"}],
                   tune_model="x", loop_pending={"live": True})
    reply = b._dorossi_apply_session_action(state, UID, ("reset", "s1"))
    slot = state[UID]["sessions"]["s1"]
    assert "s1" in state[UID]["sessions"], "重置不是刪除"
    for gone in ("cc_session_id", "api_history", "tune_model", "loop_pending"):
        assert gone not in slot, f"{gone} 應該被清掉"
    assert "`s1`" in reply


def test_deleting_the_active_session_repoints_to_the_most_recent():
    """刪掉正在用的那個之後，`active` 不能懸空：下一輪問答會拿 `active` 去找 slot，
    指到一個不存在的 id 等於安靜地開一個新對話。"""
    state = _fresh(3)
    state[UID]["active"] = "s3"
    reply = b._dorossi_apply_session_action(state, UID, ("delete", "s3"))
    assert state[UID]["active"] == "s2", "應該改指到最近使用的那個"
    assert "`s2`" in reply


def test_deleting_an_idle_session_leaves_the_active_one_alone():
    state = _fresh(3)
    state[UID]["active"] = "s3"
    reply = b._dorossi_apply_session_action(state, UID, ("delete", "s1"))
    assert state[UID]["active"] == "s3"
    assert "s3" not in reply, "刪掉別的 slot 不該順便宣告切換"


def test_deleting_the_last_session_says_there_are_none_left():
    state = _fresh(1)
    reply = b._dorossi_apply_session_action(state, UID, ("delete", "s1"))
    assert state[UID]["sessions"] == {}
    assert "沒有 session" in reply


def test_archiving_the_active_session_skips_the_other_archived_ones():
    """封存的意思是「從常用中退場」。改指的時候若不排除已封存的，擁有者會在下一句
    話裡接到一個他明確收起來的對話。"""
    state = _fresh(3)
    state[UID]["sessions"]["s2"]["archived"] = True
    state[UID]["active"] = "s3"
    b._dorossi_apply_session_action(state, UID, ("archive", "s3"))
    assert state[UID]["sessions"]["s3"]["archived"] is True
    assert state[UID]["active"] == "s1", "改指到了一個已封存的 slot"


def test_archiving_when_every_other_session_is_archived_leaves_none_active():
    """全部都收起來之後 `active` 是 None——下一輪問答會自己開一個新的，這是對的：
    比硬指回一個剛被收起來的好。"""
    state = _fresh(2)
    state[UID]["sessions"]["s1"]["archived"] = True
    state[UID]["active"] = "s2"
    b._dorossi_apply_session_action(state, UID, ("archive", "s2"))
    assert state[UID]["active"] is None


def test_archiving_an_idle_session_does_not_move_the_active_one():
    state = _fresh(2)
    state[UID]["active"] = "s2"
    b._dorossi_apply_session_action(state, UID, ("archive", "s1"))
    assert state[UID]["active"] == "s2"


def test_rename_sets_the_label_and_a_bare_rename_clears_it():
    state = _fresh(1)
    b._dorossi_apply_session_action(state, UID, ("rename", "s1", "新標籤"))
    assert state[UID]["sessions"]["s1"]["label"] == "新標籤"
    reply = b._dorossi_apply_session_action(state, UID, ("rename", "s1", None))
    assert "label" not in state[UID]["sessions"]["s1"]
    assert "清除" in reply


def test_an_unrecognised_action_falls_back_to_the_grammar():
    """動詞表加了一個新的、卻忘記在這裡接上時的落點。回一句用法比安靜地什麼都不做
    好——後者會讓使用者以為成功了。"""
    state = _fresh(1)
    reply = b._dorossi_apply_session_action(state, UID, ("蒸發", "s1"))
    assert "用法" in reply


# ---------------------------------------------------------------------------
# 九、匯出
# ---------------------------------------------------------------------------

def _exported(env) -> dict:
    payload = env.kwargs[-1]["file"]
    payload.fp.seek(0)
    return json.loads(payload.fp.read().decode("utf-8"))


def test_exporting_an_unknown_id_says_so(env):
    _seed(1)
    _run(b.mcmd_session(_Msg(), "s9 export"))
    assert "找不到" in env.text()
    assert not env.kwargs[-1], "不該附上任何檔案"


def test_the_exported_attachment_carries_the_whole_slot(env):
    _seed(1, cc_session_id="abc", label="週末")
    _run(b.mcmd_session(_Msg(), "s1 export"))
    payload = _exported(env)
    assert payload["uid"] == UID and payload["sid"] == "s1"
    assert payload["session"]["cc_session_id"] == "abc"
    assert payload["session"]["label"] == "週末"
    assert isinstance(payload["exported_at"], (int, float))
    assert env.kwargs[-1]["file"].filename == "session_s1.json"


def test_the_export_strips_host_paths_on_a_surface_that_must_not_see_them(
        env, monkeypatch):
    """附件的內容也是「送到對話平台的字串」。

    列表那一側早就在收斂路徑了，附件若沒有，同一組值就會從另一個出口整包送出去
    ——而且是 JSON，比訊息更容易被整段轉貼。判準必須跟列表**同一支**
    （`_paths_visible_here`），兩個出口各有一套遲早會各說各話。
    """
    workdir = env.tmp / "工作目錄名"
    extra = env.tmp / "額外目錄名"
    workdir.mkdir()
    extra.mkdir()
    _seed(1, cc_cwd=str(workdir), cc_extra_dir=str(extra))
    monkeypatch.setattr(b, "_paths_visible_here", lambda _m: False)
    _run(b.mcmd_session(_Msg(), "s1 export"))
    slot = _exported(env)["session"]
    assert slot["cc_cwd"] == "工作目錄名"
    assert slot["cc_extra_dir"] == "額外目錄名"
    assert str(env.tmp) not in json.dumps(_exported(env), ensure_ascii=False)


def test_the_export_keeps_host_paths_where_they_are_allowed(env, monkeypatch):
    workdir = env.tmp / "工作目錄名"
    workdir.mkdir()
    _seed(1, cc_cwd=str(workdir))
    monkeypatch.setattr(b, "_paths_visible_here", lambda _m: True)
    _run(b.mcmd_session(_Msg(), "s1 export"))
    assert _exported(env)["session"]["cc_cwd"] == str(workdir)


def test_the_export_does_not_mutate_the_state_it_was_handed(env, monkeypatch):
    """收斂是給**這一份附件**用的複本；寫回 state 就等於把擁有者的工作目錄弄丟了。

    ※ 斷言必須看**傳進去的那個 state 物件**，不能看磁碟。匯出只讀不寫，所以磁碟上
    那一份無論如何都不會變——拿它當斷言對象時，把 `dict(sess)` 換成 `sess` 的變異
    照樣活著（量過）。今天這個缺陷是潛伏的（`_dorossi_export_session` 自己載入、
    自己丟棄），但它離「有人把載入提到外面共用」只有一步，而那一步不會有任何症狀。
    """
    workdir = env.tmp / "工作目錄名"
    workdir.mkdir()
    _seed(1, cc_cwd=str(workdir))
    live = db._dorossi_load_state()
    monkeypatch.setattr(b, "_dorossi_load_state", lambda: live)
    monkeypatch.setattr(b, "_paths_visible_here", lambda _m: False)
    _run(b.mcmd_session(_Msg(), "s1 export"))
    assert _exported(env)["session"]["cc_cwd"] == "工作目錄名", "附件沒有被收斂"
    assert live[UID]["sessions"]["s1"]["cc_cwd"] == str(workdir), (
        "收斂寫回了傳進來的 state——那一份是要留著的值，不是附件的複本")


# ---------------------------------------------------------------------------
# 十、靜態釘（一）：匯出的收斂清單要涵蓋每一個帶目錄的 slot 欄位
# ---------------------------------------------------------------------------

_DIR_KEY_RE = re.compile(r"^[a-z0-9_]+(_cwd|_dir)$")

# 形狀對得上、但**不是** slot 欄位的字串常數，列在這裡並寫理由。目前是空的：
# 量過（2026-09-20），兩個模組裡符合這個形狀的字串常數只有那兩個 slot 欄位。
_NOT_A_SESSION_KEY: frozenset[str] = frozenset()


def _source(name: str) -> str:
    return (PKG_ROOT / name).read_text(encoding="utf-8")


def _directory_bearing_keys(sources) -> set[str]:
    found: set[str] = set()
    for src in sources:
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and _DIR_KEY_RE.match(node.value)):
                found.add(node.value)
    return found - _NOT_A_SESSION_KEY


def _export_sanitised_keys() -> set[str]:
    """`_dorossi_export_session` 裡那個 for 迴圈走訪的鍵。"""
    tree = ast.parse(_source("discord_bot.py"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_dorossi_export_session"):
            for inner in ast.walk(node):
                if isinstance(inner, ast.For) and isinstance(inner.iter, ast.Tuple):
                    return {e.value for e in inner.iter.elts
                            if isinstance(e, ast.Constant)}
    return set()


def test_the_directory_key_extractor_is_live():
    """先證明抽取器真的會抓到東西——一個抓不到任何東西的掃描，讀起來跟乾淨一模一樣。"""
    synthetic = 'x = sess.get("cc_cwd")\ny = sess["proj_dir"]\nz = "不是鍵"\n'
    assert _directory_bearing_keys([synthetic]) == {"cc_cwd", "proj_dir"}
    assert _export_sanitised_keys(), "沒有從匯出那支抽到任何鍵"


def test_the_export_sanitiser_covers_every_directory_bearing_session_key():
    """新增一個帶目錄的 slot 欄位時，匯出這一側會**安靜地**漏掉它。

    列表那一側是一行一行手寫的，漏了看得出來（畫面上少一行）；附件是整包 dump，
    多出來的那個欄位會原樣帶著完整主機路徑送出去，而且沒有任何症狀。所以這裡把
    「有哪些帶目錄的欄位」從原始碼推導出來，而不是再抄一份清單。
    """
    known = _directory_bearing_keys(
        [_source("discord_bot.py"), _source("dorossi_backend.py")])
    sanitised = _export_sanitised_keys()
    assert known, "抽不到任何帶目錄的欄位——抽取器壞了，不是真的沒有"
    missing = known - sanitised
    assert not missing, (
        f"這些帶目錄的 slot 欄位沒有被匯出的收斂清單涵蓋：{sorted(missing)}。"
        "要嘛加進 `_dorossi_export_session` 的鍵組，要嘛（若它其實不是 slot 欄位）"
        "加進本檔的 `_NOT_A_SESSION_KEY` 並寫下理由。")


# ---------------------------------------------------------------------------
# 十一、靜態釘（二）：握著狀態鎖的時候不得送訊息
# ---------------------------------------------------------------------------

_SEND_CALLS = frozenset({"safe_reply", "send", "reply", "ack"})


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _releases_a_lock(stmts) -> bool:
    for stmt in stmts:
        for node in ast.walk(stmt):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "release"):
                return True
    return False


def _sends_while_holding_a_lock(func: ast.AST) -> list[str]:
    """函式裡「`finally` 會 release 某把鎖」的 try，其 body 內的送出呼叫。

    刻意只看 `finally` 有 release 的那個 try：拿鎖失敗那條路上的 `safe_reply` 是
    **對的**（那時候鎖根本沒拿到），用「函式裡出現過 release」當判準會把它一起誤殺。
    """
    bad: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Try) or not _releases_a_lock(node.finalbody):
            continue
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Call) and _call_name(inner) in _SEND_CALLS:
                    bad.append(_call_name(inner))
    return bad


def _func_named(source: str, name: str) -> ast.AST:
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return node
    raise AssertionError(f"找不到 {name}")


def test_nothing_is_sent_to_the_chat_platform_while_the_state_lock_is_held():
    """行為測試只管得到**今天**的分支；會漂的是明天新增的那一條。

    下一個在鎖裡面加一句「順便提醒一下」的人，不會有任何測試變紅——而後果是那把
    設計成握幾毫秒的鎖，從此每次都要等一次對話平台的往返（被限流時是好幾秒），
    而所有問答與管理指令都排在它後面。
    """
    offenders = _sends_while_holding_a_lock(
        _func_named(_source("discord_bot.py"), "mcmd_session"))
    assert offenders == [], (
        f"`mcmd_session` 在握著狀態鎖的區段裡送訊息：{offenders}。"
        "把送出移到 `finally` 之後——臨界區只做讀改寫。")


_LOCK_SCOPE_MUST_BLOCK = [
    ("直接在鎖裡回覆", """
async def f(m):
    try:
        r = g()
        await safe_reply(m, r)
    finally:
        _lock.release()
"""),
    ("在鎖裡走 channel.send", """
async def f(m):
    try:
        r = g()
        await m.channel.send(r)
    finally:
        _lock.release()
"""),
    ("藏在鎖裡的 if 分支", """
async def f(m):
    try:
        r = g()
        if r:
            await safe_reply(m, "順便提醒一下")
    finally:
        _lock.release()
"""),
]

_LOCK_SCOPE_MUST_ALLOW = [
    ("回覆在鎖外面（真正的形狀）", """
async def f(m):
    try:
        r = g()
    finally:
        _lock.release()
    await safe_reply(m, r)
"""),
    ("拿鎖逾時那條路上的回覆", """
async def f(m):
    try:
        await asyncio.wait_for(_lock.acquire(), timeout=5)
    except asyncio.TimeoutError:
        await safe_reply(m, "忙線中")
        return
    try:
        r = g()
    finally:
        _lock.release()
    await safe_reply(m, r)
"""),
    ("沒有鎖的 try/finally", """
async def f(m):
    try:
        await safe_reply(m, "x")
    finally:
        fp.close()
"""),
]


@pytest.mark.parametrize("label, source", _LOCK_SCOPE_MUST_BLOCK,
                         ids=[c[0] for c in _LOCK_SCOPE_MUST_BLOCK])
def test_the_lock_scope_check_catches_a_send_moved_inside(label, source):
    assert _sends_while_holding_a_lock(_func_named(source, "f")), (
        f"「{label}」沒有被抓到——這道守門漏了它想擋的形狀")


@pytest.mark.parametrize("label, source", _LOCK_SCOPE_MUST_ALLOW,
                         ids=[c[0] for c in _LOCK_SCOPE_MUST_ALLOW])
def test_the_lock_scope_check_allows_the_shapes_that_are_fine(label, source):
    """放行的那幾種**必須**有自己的測試：一道什麼都擋的守門在真實程式碼上永遠是紅的，
    於是被放寬，於是變成裝飾品。逾時分支那一筆尤其重要——它的 `safe_reply` 長得跟
    違規的一模一樣，差別只在那時候鎖根本沒拿到。"""
    assert _sends_while_holding_a_lock(_func_named(source, "f")) == [], (
        f"「{label}」被誤判成違規")
# ---------------------------------------------------------------------------
# 12. 新工作階段的編號：防撞迴圈與序號推導
# ---------------------------------------------------------------------------
# 分支覆蓋率（2026-09-21）指出 `_dorossi_new_session` 裡那個 `while sid in sessions`
# 的**迴圈本體從來沒有被執行過**，而它的 docstring 明寫著存在理由：「defensively
# skips any id that somehow already exists」。
#
# 它壞掉的後果不是例外，是 `sessions[sid] = sess` **直接蓋掉一個已經存在的工作
# 階段**——使用者的整段對話狀態就這樣沒了，而且沒有 undo（`.backup/` 那套只保護
# 佇列檔）。會讓計數器與實際 slot 對不上的情況不是理論：狀態檔被手改過、寫到一半
# 被砍、或從封存還原回來，`next_seq` 就可能落後。


def test_a_stale_counter_does_not_overwrite_an_existing_session():
    """`next_seq` 落後時要跳過已經存在的編號，不是蓋掉它。"""
    state: dict = {}
    record = db._dorossi_user_record(state, UID)
    first = db._dorossi_new_session(record)
    second = db._dorossi_new_session(record)
    assert [first, second] == ["s1", "s2"], (first, second)
    record["sessions"][first]["label"] = "不可以被蓋掉"
    record["next_seq"] = 1                       # 計數器倒退（手改／還原）
    third = db._dorossi_new_session(record)
    assert third == "s3", f"防撞迴圈沒有跳過已存在的編號：{third}"
    assert record["sessions"]["s1"]["label"] == "不可以被蓋掉", (
        "已經存在的工作階段被新的蓋掉了——那等於使用者的對話狀態不見了")
    assert set(record["sessions"]) == {"s1", "s2", "s3"}, record["sessions"]


def test_the_counter_moves_past_the_collision_it_just_skipped():
    """跳過之後計數器也要跟上，否則下一次又要從頭撞一遍。

    這一格是近似反例：只看「這次回的編號對不對」的話，把 `rec["next_seq"] = seq + 1`
    改成寫回原本那個 seq 也是綠的，而那會讓每一次開新對話的成本隨 slot 數增長。
    """
    state: dict = {}
    record = db._dorossi_user_record(state, UID)
    for _ in range(3):
        db._dorossi_new_session(record)
    record["next_seq"] = 1
    db._dorossi_new_session(record)
    assert record["next_seq"] == 5, (
        f"撞號之後 next_seq 停在 {record['next_seq']}，下一次會再撞一遍")


@pytest.mark.parametrize("bad", [None, 0, -3, "2", 1.5, True],
                         ids=["缺", "零", "負", "字串", "浮點", "布林"])
def test_a_broken_counter_is_derived_from_the_slots_that_exist(bad):
    """`next_seq` 壞掉時要從現有 slot 推導，不能從 1 重來。

    `True` 那一格是刻意的：`bool` 是 `int` 的子類，`isinstance(True, int)` 為真，
    所以型別檢查放不住它——擋它的是 `seq < 1` 之外的那一半，也就是推導出來的值。
    這個專案已經為同一個坑寫過一條硬規則（每個 `_coerce_int` 都要先排除 `bool`）。
    """
    state: dict = {}
    record = db._dorossi_user_record(state, UID)
    for _ in range(4):
        db._dorossi_new_session(record)
    for sid in ("s2", "s3"):
        record["sessions"].pop(sid)              # 中間被刪掉的編號不重用
    record["next_seq"] = bad
    fresh = db._dorossi_new_session(record)
    assert db._dorossi_is_session_id(fresh), (
        f"配號器產出了一個自己的驗證器不認得的 id：{fresh!r}。"
        "這比「編號不對」更糟——`_DOROSSI_SESSION_ID_RE` 認不得它，所以"
        "`_dorossi_derive_next_seq` 之後永遠看不到它，排序也會把它丟到最後。")
    assert fresh == "s5", f"壞掉的計數器讓新工作階段撿到 {fresh}"
    assert set(record["sessions"]) == {"s1", "s4", "s5"}, record["sessions"]
