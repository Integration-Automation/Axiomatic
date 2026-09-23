"""單輪問答的協調（`discord_bot._dorossi_process_turn`）——27 行 docstring，
145 行敘述裡有 144 行從來沒有被任何測試執行過。

它是 `/dorossi` 的主幹：每一則提問都從這裡走。§8.104 那個自走迴圈是它的一個分支
（`_dorossi_should_loop` 命中時整輪交出去），而這支函式本身負責的是**一輪**的全部
決策——重置關鍵字、後端選擇、前置檢查、狀態快照、微調的優先序、後端呼叫、推進存檔、
答案送出、以及兩條收尾例外。

這裡的失敗形態幾乎都是安靜的：

* 重置語意弄反（`/new` 去清脈絡、`/clear` 不清），使用者只會覺得「它忘記我剛說的」；
* 被拒的目錄字串漏進對外訊息 → Layer 1 外洩，而訊息本身看起來很正常；
* 微調的優先序弄反 → 本輪打的 `/effort` 被既存值蓋掉，回覆照常送出；
* 推進沒存回去 → 下一輪 resume 到舊的脈絡，看起來像「它突然失憶」；
* 壓縮的條件放寬 → 對 api／codex 也去壓縮一個不存在的工作階段。

沒有一種會拋例外——整支函式最外層就是「吞掉所有錯、回一句泛用的話」。

測試不連網、不起子行程、**不碰磁碟上的工作階段檔**（`_dorossi_state_rmw` 整支換成
對記憶體 dict 的操作），也不會碰到真正的工作目錄（`_dorossi_resolve_cc_workdir`
換成只讀 slot 的替身）。
"""
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

UID = "owner"
SID = "s1"
OTHER_SID = "s2"
OWNER_ID = 424242
OPEN = db.DOROSSI_LOOP_OPEN_SENTINEL
# 模型別名刻意用查表取得而不是寫死字面量：那些 key 是 2026-07-02 裁定的「功能面
# 例外」，寫死會在表改動時安靜失效，而這裡要的只是「一個合法值」。
MODEL_KEY = sorted(db.DOROSSI_MODEL_CHOICES)[0]
MODEL_VALUE = db.DOROSSI_MODEL_CHOICES[MODEL_KEY]

BOT_SOURCE = Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py"


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------

class _FakeLive:
    """`_DorossiLiveMessage` 的替身：記下每次 finalize／reopen／update 說了什麼。"""

    sink: list = []
    order: list = []

    def __init__(self, message, prime=None):
        self.message = message
        self.prime = prime

    async def finalize(self, content):
        _FakeLive.sink.append(content)
        _FakeLive.order.append("live.finalize")

    def reopen(self):
        _FakeLive.sink.append("<reopen>")
        _FakeLive.order.append("live.reopen")

    def update(self, content):
        _FakeLive.sink.append(f"<update>{content}")


class _Sem:
    """並發號誌的替身——存在的理由是「後端呼叫有沒有在號誌**裡面**」只能靠順序看出來。

    `locked()` 永遠是 False（永遠有空位）：「空位滿了」那幾支用下面的 `_RealSem`。"""

    def __init__(self, order):
        self.order = order

    def locked(self):
        return False

    async def __aenter__(self):
        self.order.append("sem")
        return self

    async def __aexit__(self, *_exc):
        self.order.append("/sem")
        return False


class _Typing:
    def __init__(self, order):
        self.order = order

    async def __aenter__(self):
        self.order.append("typing")
        return self

    async def __aexit__(self, *_exc):
        self.order.append("/typing")
        return False


@pytest.fixture
def turn_env(monkeypatch, tmp_path):
    """把 `_dorossi_process_turn` 的每一個對外動作換成替身，回傳可腳本化的 env。

    `env.script` 是這次要餵給後端的腳本，一個元素一次呼叫：

    * tuple —— 這次呼叫的回傳值（claude_code／codex 是三元組、api 是二元組）；
    * 例外實例 —— 這次呼叫丟那個例外；
    * 可呼叫物 —— 收到 `env`，回傳上面兩種之一（用來在「後端呼叫進行中」動狀態，
      例如把 slot 刪掉）。

    腳本用完就回預設答案，**不會**丟例外：這支函式最外層會吃掉所有 `Exception`，
    丟例外只會變成一句泛用回覆，把真正的斷言失敗藏起來。「有沒有多呼叫一次」改用
    `env.calls` 的長度斷言，那個看得見。
    """
    env = types.SimpleNamespace(
        state={}, script=[], calls=[], order=[], live=[], replies=[],
        channel_sends=[], final_chunks=[], images=0, image_paths=[],
        loop_calls=[], compact_calls=[], compact_turns=[], rmw_names=[],
        reply_raises=None,
    )
    _FakeLive.sink = env.live
    _FakeLive.order = env.order

    rec = db._dorossi_user_record(env.state, UID)
    rec["sessions"][SID] = {"cc_session_id": "cc-old", "label": "L",
                            "last_used": time.time()}
    rec["sessions"][OTHER_SID] = {"cc_session_id": "cc-other", "label": "L2",
                                  "last_used": time.time()}
    rec["active"] = SID

    async def fake_rmw(mutate):
        name = getattr(mutate, "__name__", "?")
        env.rmw_names.append(name)
        env.order.append(f"rmw:{name}")
        return mutate(env.state)

    async def fake_safe_reply(message, content=None, **_kwargs):
        if env.reply_raises and env.reply_raises in str(content):
            raise RuntimeError("送不出去")
        env.replies.append(content)
        env.order.append("safe_reply")
        return types.SimpleNamespace(id=900 + len(env.replies))

    async def fake_channel_send(content=None, **_kwargs):
        env.channel_sends.append(content)

    def _next(kind, default):
        step = env.script.pop(0) if env.script else default
        if callable(step) and not isinstance(step, BaseException):
            step = step(env)
        if isinstance(step, BaseException):
            raise step
        return step

    async def fake_cc(prompt, session_id, **kwargs):
        env.calls.append(("claude_code", prompt, session_id, kwargs))
        env.order.append("backend")
        return _next("claude_code", ("答案", "cc-new", {}))

    async def fake_codex(prompt, session_id, **kwargs):
        env.calls.append(("codex", prompt, session_id, kwargs))
        env.order.append("backend")
        return _next("codex", ("答案", "cx-new", {}))

    async def fake_api(prompt, history, **kwargs):
        # `**kwargs` 收 `model=`（2026-09-23 起這條路也吃 `/model`）。簽章寫死成
        # 兩個參數的話，新增一個關鍵字參數會在**正式程式碼是對的**時候把這裡打紅。
        env.calls.append(("api", prompt, None, {"history": history, **kwargs}))
        env.order.append("backend")
        return _next("api", ("答案", [{"role": "assistant"}]))

    async def fake_send_images(_message, paths):
        env.image_paths.append(list(paths or []))
        env.order.append("images")
        return env.images

    async def fake_reply_final(_message, _live, chunks, **_kwargs):
        env.final_chunks.append(list(chunks))
        env.order.append("reply")

    async def fake_ensure_live(_message, placeholder, initial):
        env.live.append(initial)
        return _FakeLive(placeholder or types.SimpleNamespace(id=5),
                         prime=initial)

    async def fake_run_loop(*args, **kwargs):
        env.loop_calls.append((args, kwargs))
        env.order.append("loop")

    async def fake_compact(uid, sid, cc_session_id, snap, info, *, turn=None):
        env.compact_calls.append((uid, sid, cc_session_id, snap, info))
        env.compact_turns.append(turn)
        env.order.append("compact")

    channel = types.SimpleNamespace(id=42, send=fake_channel_send,
                                    typing=lambda: _Typing(env.order))
    env.message = types.SimpleNamespace(
        id=7, channel=channel, author=types.SimpleNamespace(id=OWNER_ID))
    env.placeholder = types.SimpleNamespace(id=1)

    monkeypatch.setattr(b, "OWNER_USER_ID", OWNER_ID)
    # `OWNER_USER_ID = DOROSSI_USER_ID`（同一個人，兩個名字），所以只改一個會讓這個
    # 替身在「自走閘」眼裡是擁有者、在 `_dorossi_owner_only` 眼裡不是——兩道閘對同一
    # 則訊息給出相反答案，測到的就不是正式路徑。兩個一起改。
    monkeypatch.setattr(b, "DOROSSI_USER_ID", OWNER_ID)
    # 這台機器上有一個跑了好幾天的正式批次，而撞到用量上限的那一輪會**寫**排隊佇列
    # （停進去等重設）與事件檔——三個落地檔全部導到 tmp，測試不准碰到真的。
    monkeypatch.setattr(b, "DOROSSI_QUEUE_FILE", tmp_path / "q.ndjson")
    monkeypatch.setattr(b, "DOROSSI_FAILED_QUEUE_FILE", tmp_path / "qf.ndjson")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "ev.ndjson")
    monkeypatch.setattr(b, "DOROSSI_BACKEND", "claude_code")
    monkeypatch.setattr(b, "DOROSSI_CC_TOOLS", "full")
    monkeypatch.setattr(b, "DOROSSI_SELF_JUDGE_ENABLED", True)
    monkeypatch.setattr(b, "_dorossi_backend_sem", _Sem(env.order))
    monkeypatch.setattr(b, "_dorossi_state_rmw", fake_rmw)
    monkeypatch.setattr(b, "safe_reply", fake_safe_reply)
    monkeypatch.setattr(b, "_DorossiLiveMessage", _FakeLive)
    monkeypatch.setattr(b, "_dorossi_ensure_live", fake_ensure_live)
    monkeypatch.setattr(b, "_dorossi_via_claude_code", fake_cc)
    monkeypatch.setattr(b, "_dorossi_via_codex", fake_codex)
    monkeypatch.setattr(b, "_dorossi_via_api", fake_api)
    monkeypatch.setattr(b, "_dorossi_send_images", fake_send_images)
    monkeypatch.setattr(b, "_dorossi_reply_final", fake_reply_final)
    monkeypatch.setattr(b, "_dorossi_run_loop", fake_run_loop)
    monkeypatch.setattr(b, "_dorossi_maybe_compact_single_turn", fake_compact)
    monkeypatch.setattr(b, "_dorossi_resolve_cc_workdir",
                        lambda sess, _u, _i: sess.get("cc_cwd") or "預設工作目錄")
    monkeypatch.setattr(b, "_failure_diagnostic_summary",
                        lambda kind="runtime": f"DIAG:{kind}")
    monkeypatch.setattr(b, "_shutil",
                        types.SimpleNamespace(which=lambda _n: "/usr/bin/x"))
    monkeypatch.setattr(b, "find_codex_executable", lambda: "/usr/bin/codex")
    monkeypatch.setattr(b, "AsyncAnthropic", object)
    monkeypatch.setattr(b, "traceback",
                        types.SimpleNamespace(print_exc=lambda: None))
    return env


def _turn(env, prompt="查一下這個函式", *, placeholder=True, **kwargs):
    asyncio.run(b._dorossi_process_turn(
        env.message, prompt, env.placeholder if placeholder else None,
        UID, SID, **kwargs))
    return env


def _slot(env, sid=SID):
    return env.state[UID]["sessions"].get(sid)


def _said(env):
    """擁有者實際看到的所有文字——**包含最終答案**。

    這個專案的送出路徑不只一條（live 的 finalize、`safe_reply`、頻道直送、
    `_dorossi_reply_final` 的分段），漏掉任何一條都會讓「有沒有送出去」的斷言
    變成永遠成立的空話（§8.104 就踩過一次）。
    """
    posted = [chunk for chunks in env.final_chunks for chunk in chunks]
    return "\n".join(str(x) for x in
                     env.live + env.replies + env.channel_sends + posted)


def _kwargs_of(env, index=0):
    return env.calls[index][3]


# --------------------------------------------------------------------------
# 一、重置路徑：`/new` 與 `/clear` 必須做相反的事
# --------------------------------------------------------------------------

@pytest.mark.parametrize("keyword, should_clear", [
    ("/clear", True),
    ("/reset", True),
    ("重置", True),
    ("清除對話", True),
    ("/new", False),
    ("新對話", False),
])
def test_only_the_clearing_keywords_drop_the_stored_context(
        turn_env, keyword, should_clear):
    """`/clear` 清空**目前** slot 的脈絡；`/new` 不清——因為派發時已經配了一個新的。

    弄反的後果分兩種，都不會報錯：`/new` 也去清的話，它清掉的是**上一個** slot
    （使用者剛剛才開的新對話反而沒事，舊的被抹掉）；`/clear` 不清的話，使用者按了
    「清除對話」而脈絡原封不動，下一輪照樣 resume 到舊的工作階段。
    """
    _turn(turn_env, keyword)
    slot = _slot(turn_env)
    if should_clear:
        assert "cc_session_id" not in slot, "`/clear` 語意的關鍵字必須清掉脈絡"
    else:
        assert slot.get("cc_session_id") == "cc-old", (
            "`/new` 的新 slot 是派發時配的，這裡再清一次會清到別人的脈絡")
    assert slot.get("label") == "L", "重置只清脈絡，slot 的 id／標籤要留著"


def test_a_reset_turn_stops_before_the_backend(turn_env):
    """重置就是一則確認訊息，不是一次提問——底下整條後端路徑都不該跑。

    `return` 掉了的話，一個 `/clear` 會變成「把『/clear』這四個字送去問後端」：
    花一次叫用、推進工作階段，而使用者剛剛要的正是把脈絡丟掉。
    """
    _turn(turn_env, "/clear")
    assert turn_env.calls == [], "重置輪不該呼叫任何後端"
    assert turn_env.rmw_names == ["_reset_mut"], (
        f"重置輪只該做一次狀態 RMW，實際做了 {turn_env.rmw_names}")
    assert turn_env.compact_calls == [] and turn_env.loop_calls == []


def test_a_usable_directory_becomes_this_conversations_working_directory(
        turn_env, tmp_path):
    """`/new <路徑>` 的直接語法：驗證通過才套用，而且必須存進 slot。

    存不進去的話，後端每一輪都在預設目錄跑——使用者指定的目錄看起來被接受了
    （確認訊息照樣說「已套用」），實際上一輪都沒生效。
    """
    _turn(turn_env, f"/new {tmp_path}")
    assert _slot(turn_env).get("cc_cwd") == str(tmp_path.resolve())
    assert "已套用指定的工作目錄" in _said(turn_env)


def test_an_extra_directory_uses_its_own_branch(turn_env, tmp_path):
    """`dir=` 是舊語意的「額外可存取範圍」，跟 cwd 是**兩個**欄位。

    兩條混在一起的話，使用者以為只是多開一個可讀目錄，實際上後端的工作目錄被換掉
    ——在 full 工具模式下那是「在另一個地方動手」。
    """
    _turn(turn_env, f"/new dir={tmp_path}")
    slot = _slot(turn_env)
    assert slot.get("cc_extra_dir") == str(tmp_path.resolve())
    assert "cc_cwd" not in slot, "`dir=` 不該順手把工作目錄也換掉"
    assert "已套用指定的工作範圍" in _said(turn_env)


def test_a_rejected_directory_never_reaches_the_channel(turn_env):
    """被拒的路徑字串只進 stderr，對外一律泛用（Layer 1）。

    這是一條使用者**自己打的**字串，回音出去就是把主機上的路徑貼進聊天室——而它
    讀起來完全像一句正常的錯誤提示，沒有人會覺得不對。
    """
    bogus = "D:/Work/這個目錄不存在_9f3a"
    _turn(turn_env, f"/new {bogus}")
    said = _said(turn_env)
    assert bogus not in said and "9f3a" not in said, (
        "被拒的原始路徑不得出現在任何對外訊息裡")
    assert "指定的工作目錄無法使用" in said, "被拒還是要說一聲，只是不講是哪一個"
    assert "cc_cwd" not in _slot(turn_env)


def test_a_rejected_extra_directory_is_kept_out_of_the_channel_too(turn_env):
    """兩條路徑各自一段驗證與提示——`dir=` 那一半同樣不得回音路徑。

    這種「同一條規則的第二個實例」是最容易只修一半的形狀：cwd 那半寫對了，讀的人
    很自然會假設下面那半也一樣。
    """
    bogus = "D:/Work/也不存在_7c11"
    _turn(turn_env, f"/new dir={bogus}")
    said = _said(turn_env)
    assert bogus not in said and "7c11" not in said
    assert "指定的存取範圍無法使用" in said
    assert "cc_extra_dir" not in _slot(turn_env)


def test_ordinary_words_after_a_reset_keyword_are_not_a_failed_path(turn_env):
    """`/new 隨便幾個字` 是純重置，不是「目錄無法使用」。

    少了「看起來像不像路徑」這道判斷，每一次帶著說明文字的重置都會多一句莫名其妙的
    警告，而那種噪音的下場是使用者不再讀這則訊息。
    """
    _turn(turn_env, "/new 來聊點別的")
    said = _said(turn_env)
    assert "無法使用" not in said, f"純重置不該冒出目錄警告：{said!r}"
    assert "已開新對話" in said


def test_a_tuning_directive_on_a_reset_turn_lands_in_the_fresh_context(
        turn_env):
    """`/effort high /clear` —— 順序必須是「先清、後套」。

    反過來的話，`_dorossi_reset_session` 會把剛套上的微調一起清掉（它就是負責把
    `tune_effort`／`tune_model` 丟掉的那一支），使用者打的指令靜靜地沒有生效。
    """
    _turn(turn_env, "/clear", effort="high", model_tier=MODEL_KEY)
    slot = _slot(turn_env)
    assert slot.get("tune_effort") == "high"
    assert slot.get("tune_model") == MODEL_KEY
    assert "已更新回覆設定" in _said(turn_env)


def test_the_reset_confirmation_occupies_exactly_one_message(turn_env):
    """整輪一則訊息：排隊的佔位訊息在，就用它；不在才自己發一則。

    兩條都送的話，一次重置變成兩則訊息——而這支函式的整個設計前提就是
    「一輪 ＝ 一則」。
    """
    _turn(turn_env, "/clear")
    assert turn_env.replies == [], "有佔位訊息時不該再另外發一則"
    assert len(turn_env.live) == 1

    turn_env.live.clear()
    turn_env.replies.clear()
    _turn(turn_env, "/clear", placeholder=False)
    assert turn_env.live == [], "沒有佔位訊息時不該去編輯一則不存在的訊息"
    assert len(turn_env.replies) == 1


# --------------------------------------------------------------------------
# 二、後端選擇與前置檢查
# --------------------------------------------------------------------------

def test_the_slot_decides_the_backend_not_the_global_default(turn_env):
    """`ai_provider` 是**每個 session 自己的**，不是全域設定。

    讀錯的話，一個刻意切到另一個後端的對話會被送回預設後端——而它會照常回答，
    所以沒有任何症狀，只有回答的風格變了。
    """
    _slot(turn_env)["ai_provider"] = "codex"
    _turn(turn_env)
    assert [call[0] for call in turn_env.calls] == ["codex"]


@pytest.mark.parametrize("backend, breaker", [
    ("claude_code", lambda m: m.setattr(
        b, "_shutil", types.SimpleNamespace(which=lambda _n: None))),
    ("codex", lambda m: m.setattr(b, "find_codex_executable", lambda: None)),
    ("api", lambda m: m.setattr(b, "AsyncAnthropic", None)),
])
def test_a_missing_prerequisite_is_answered_and_advances_nothing(
        turn_env, monkeypatch, backend, breaker):
    """前置條件不成立 → 一句可行動的泛用回覆，然後**停**。

    沒有這道檢查，缺件會一路走到後端呼叫才炸，使用者拿到的是同一句泛用失敗訊息，
    而診斷得從 log 裡的堆疊往回推。更貴的是：那時候狀態 RMW 已經跑過了。
    """
    monkeypatch.setattr(b, "DOROSSI_BACKEND", backend)
    breaker(monkeypatch)
    _turn(turn_env)
    assert turn_env.calls == [], "前置檢查沒過就不該呼叫後端"
    assert "Dorossi 暫時無法回應" in _said(turn_env)
    assert turn_env.rmw_names == ["_provider_mut"], (
        "前置檢查在狀態快照**之前**，不該留下任何推進")
    assert _slot(turn_env).get("cc_session_id") == "cc-old"


def test_the_prerequisite_reply_also_reuses_the_queue_placeholder(turn_env,
                                                                  monkeypatch):
    """前置失敗一樣是「一輪一則」——排隊時編輯佔位訊息，沒排隊才自己發一則。

    兩半都要測：只測有佔位訊息那一半的話，「沒有佔位訊息時什麼都不送」這個變異
    活得下來，而它的症狀是使用者按了指令完全沒有反應。
    """
    monkeypatch.setattr(b, "_shutil",
                        types.SimpleNamespace(which=lambda _n: None))
    _turn(turn_env)
    assert turn_env.replies == []
    assert "Dorossi 暫時無法回應" in "\n".join(turn_env.live)

    turn_env.live.clear()
    _turn(turn_env, placeholder=False)
    assert turn_env.live == []
    assert "Dorossi 暫時無法回應" in "\n".join(str(x) for x in turn_env.replies)


# --------------------------------------------------------------------------
# 三、狀態快照：微調的優先序與過舊自動重置
# --------------------------------------------------------------------------

@pytest.mark.parametrize("has_context, should_reset", [
    (True, True),
    (False, False),
])
def test_a_stale_slot_is_only_reset_when_it_has_something_to_drop(
        turn_env, has_context, should_reset):
    """過舊自動重置只在 slot 真的有脈絡可清時動作。

    少了那個條件，一個**空的**舊 slot 每一輪都會「重置」一次，使用者每一輪都收到
    一句「這段對話有點久沒用了」——對一個本來就沒有脈絡的對話。
    """
    slot = _slot(turn_env)
    slot["last_used"] = time.time() - 86400 * (db.DOROSSI_SESSION_MAX_AGE_DAYS + 1)
    if not has_context:
        slot.pop("cc_session_id")
    _turn(turn_env)
    notice = "這段對話有點久沒用了" in _said(turn_env)
    assert notice is should_reset
    if should_reset:
        assert turn_env.calls[0][2] is None, (
            "重置過的 slot 不該再把舊的工作階段 id 帶給後端")


def test_a_failed_staleness_notice_does_not_take_the_turn_down_with_it(
        turn_env):
    """那句「有點久沒用了」是附帶說明，送不出去也不能讓整輪失敗。

    它包在自己的 try 裡是有理由的：這一輪的**答案**還沒問、脈絡已經被清掉了，
    此時往外拋等於「因為一句提示送失敗，所以你的問題不回答了」。
    """
    slot = _slot(turn_env)
    slot["last_used"] = time.time() - 86400 * (db.DOROSSI_SESSION_MAX_AGE_DAYS + 1)
    turn_env.reply_raises = "有點久沒用了"
    _turn(turn_env)
    assert turn_env.calls, "提示送失敗之後，這一輪還是要照常問下去"
    assert "答案" in _said(turn_env)


def test_this_turns_directive_outranks_the_stored_one(turn_env):
    """優先序「本輪指令 > 已存值 > 預設」——由「先套用、後快照」自然成立。

    兩件事對調的話，使用者這一則打的 `/effort` 會被 slot 裡的舊值蓋掉：回覆照常送
    出、看起來完全正常，只是力度不是他要的那個。
    """
    _slot(turn_env)["tune_effort"] = "low"
    _turn(turn_env, effort="high")
    assert _kwargs_of(turn_env)["effort"] == "high"
    assert _slot(turn_env).get("tune_effort") == "high", (
        "本輪指令是 session 級的，要留給之後每一輪")


def test_a_stored_directive_survives_a_turn_that_carries_none(turn_env):
    """沒打指令的那幾輪要沿用存著的值——否則微調只對打指令的那一則有效。"""
    _slot(turn_env)["tune_model"] = MODEL_KEY
    _turn(turn_env)
    assert _kwargs_of(turn_env)["model"] == MODEL_VALUE
    assert _kwargs_of(turn_env)["effort"] is None


def test_the_stale_reset_runs_before_this_turns_directive_is_applied(turn_env):
    """「過舊自動重置 ＋ 本輪帶指令」的順序：先重置，再套本輪的。

    倒過來就是本輪的指令先被寫進 slot、再被重置清掉，於是這一輪跑的是預設值。
    這條路只有在「很久沒用的對話 ＋ 這次順手調了力度」時才會走到，而那正是沒有人
    會回頭檢查的組合。
    """
    slot = _slot(turn_env)
    slot["last_used"] = time.time() - 86400 * (db.DOROSSI_SESSION_MAX_AGE_DAYS + 1)
    slot["tune_effort"] = "low"
    _turn(turn_env, effort="max")
    assert _kwargs_of(turn_env)["effort"] == "max"
    assert _slot(turn_env).get("tune_effort") == "max"


def test_the_prompt_is_recorded_on_the_slot_before_the_backend_runs(turn_env):
    """`last_user_prompt` 是「這個對話最後在做什麼」的唯一來源（列表與跨重啟接續都讀它）。

    寫在後端呼叫之後的話，任何一次中斷（例外、用量上限、重啟）都會讓這筆記錄停在
    **上上**一則提問上。
    """
    _turn(turn_env, "把這個函式重構一下")
    slot = _slot(turn_env)
    assert slot.get("last_user_prompt") == "把這個函式重構一下"
    assert isinstance(slot.get("last_user_prompt_ts"), float)


def test_the_stored_working_directory_is_what_the_backend_is_given(turn_env,
                                                                   tmp_path):
    """cwd／額外目錄都是 per-invocation 旗標，`--resume` 不會記得，所以每一輪都要重帶。

    漏帶一輪，那一輪就在別的目錄跑——同一段對話的工作階段索引是 cwd-hash，換了
    目錄等於換了一段對話。
    """
    slot = _slot(turn_env)
    slot["cc_cwd"] = str(tmp_path)
    slot["cc_extra_dir"] = str(tmp_path)
    _turn(turn_env)
    kwargs = _kwargs_of(turn_env)
    assert kwargs["workdir"] == str(tmp_path)
    assert kwargs["extra_dir"] == str(tmp_path)


# --------------------------------------------------------------------------
# 四、狀態 dict 不得跨越後端呼叫（靜態）
# --------------------------------------------------------------------------

def _state_escapes(func: ast.AST) -> str | None:
    """回傳第一個「不在任何 `state` 參數的巢狀函式裡」的 `state` 名稱位置。

    這條不變式（docstring 的 hazard #2）是**時序**的：狀態 dict 只能活在一次 RMW
    的短鎖內，不可以被帶著跨過後端呼叫——因為那段時間另一個 session 的回合會重新
    載入並存回同一個檔案，回來之後用舊的 dict 一寫就把對方的推進蓋掉。行為測試看
    不到這件事（單執行緒跑起來一切正常），所以只能靜態釘。
    """
    inner = [n for n in ast.walk(func)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n is not func]
    owners = [n for n in inner if any(a.arg == "state" for a in n.args.args)]
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id == "state":
            if not any(o.lineno <= node.lineno <= o.end_lineno for o in owners):
                return f"line {node.lineno}"
    return None


def _named_function(source: str, name: str):
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return node
    raise AssertionError(f"找不到 {name}")


@pytest.mark.parametrize("name", ["_dorossi_process_turn", "_dorossi_run_turn"])
def test_no_state_dict_is_carried_across_the_backend_call(turn_env, name):
    """整支函式裡，`state` 這個名字只能出現在 RMW 的 mutator 參數底下。

    兩支都要看：`_dorossi_process_turn` 現在只是登記／移除 `_dorossi_turns` 的外殼，
    一輪的本體在 `_dorossi_run_turn`——只查外殼的話，這支會對著一個空殼永遠綠。"""
    func = _named_function(BOT_SOURCE.read_text(encoding="utf-8"), name)
    problem = _state_escapes(func)
    assert problem is None, (
        f"`{name}` 的 {problem} 把狀態 dict 帶出了 RMW —— "
        "並行的另一個 session 回合會在這段期間重新載入並存回同一個檔案。")


@pytest.mark.parametrize("source, expected_clean", [
    ("""
async def turn():
    def _mut(state):
        state["a"] = 1
    await rmw(_mut)
    await backend()
""", True),
    ("""
async def turn():
    state = await rmw(lambda s: s)
    await backend()
    state["a"] = 1
""", False),
    ("""
async def turn():
    def _mut(state):
        return state
    state = await rmw(_mut)
    await backend()
""", False),
    ("""
async def turn():
    def _mut(state):
        def _inner():
            return state["a"]
        return _inner()
    await rmw(_mut)
""", True),
])
def test_the_state_check_can_tell_the_two_shapes_apart(source, expected_clean):
    """合成對照組——有問題的清單若永遠是空的，上面那支測試刪掉也不會變紅。

    第四個案例是**必須放行**的近似情形：巢狀再巢狀但仍在 mutator 的作用域內。
    放行步驟只有這種案例殺得掉（把範圍判斷改成「只看第一層」的變異）。
    """
    func = _named_function(source, "turn")
    assert (_state_escapes(func) is None) is expected_clean


# --------------------------------------------------------------------------
# 五、交給自走迴圈
# --------------------------------------------------------------------------

def test_an_explicit_self_drive_intent_hands_the_whole_turn_over(turn_env):
    """片語快速路徑：連 live 訊息都不開，整輪交給迴圈。

    這裡若不 return，同一則提問會先跑一輪單輪問答**再**進迴圈——等於第一輪做兩次，
    而使用者只看得到一則回覆。
    """
    _turn(turn_env, "整理測試，不要問我")
    assert turn_env.calls == [], "交給迴圈之後本函式不該再自己呼叫後端"
    assert len(turn_env.loop_calls) == 1
    args, kwargs = turn_env.loop_calls[0]
    assert args[1] == "整理測試，不要問我"
    assert args[2] is turn_env.placeholder, "排隊的佔位訊息要當成進入自走的 ack"
    assert args[3:5] == (UID, SID), "交出去的必須是本輪這個 slot"
    assert kwargs == {}


def test_the_directives_are_persisted_before_the_loop_takes_over(turn_env):
    """微調不隨參數往下傳，是靠「已經寫進 slot」讓迴圈每輪自己重讀。

    所以進迴圈**之前**那次 RMW 非做不可；少了它，`/effort max` 加上一句自走片語
    會整段長任務都跑在預設力度上。
    """
    _turn(turn_env, "做到完成為止", effort="max")
    assert _slot(turn_env).get("tune_effort") == "max"
    assert turn_env.loop_calls, "片語命中卻沒有進迴圈"


# --------------------------------------------------------------------------
# 六、後端自判（混合觸發的第二條路徑）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label, setup, expect_suffix", [
    ("三個條件都成立", lambda env, m: None, True),
    ("kill-switch 關掉",
     lambda env, m: m.setattr(b, "DOROSSI_SELF_JUDGE_ENABLED", False), False),
    ("提問者不是擁有者",
     lambda env, m: setattr(env.message, "author",
                            types.SimpleNamespace(id=OWNER_ID + 1)), False),
    ("工具模式不是 full",
     lambda env, m: m.setattr(b, "DOROSSI_CC_TOOLS", "off"), False),
])
def test_the_self_judge_path_needs_all_of_its_conditions(
        turn_env, monkeypatch, label, setup, expect_suffix):
    """自判 ＝ kill-switch 開 ∧ 前置閘門開 ∧ 提問**沒有**明確片語。

    每一條都有自己的理由，而少任何一條的後果都不一樣：kill-switch 是出事時唯一能
    關掉這條路徑的開關；閘門漏掉等於讓別人的提問也能把 bot 變成長時間自走；而
    片語那一條若不排除，命中片語的提問會在上面就進迴圈、根本走不到這裡。
    """
    setup(turn_env, monkeypatch)
    _turn(turn_env)
    prompt = turn_env.calls[0][1]
    assert (b.DOROSSI_LOOP_SELFJUDGE_SUFFIX in prompt) is expect_suffix, label
    assert prompt.startswith("查一下這個函式"), "自判指示只能附在尾端"


def test_an_explicit_phrase_never_reaches_the_self_judge_path(turn_env):
    """片語命中的提問在上面就進迴圈了——自判是**沒有**片語時的第二條路。"""
    _turn(turn_env, "做到完成為止")
    assert turn_env.loop_calls and turn_env.calls == []


def test_the_open_sentinel_hands_the_conversation_to_the_loop(turn_env):
    """turn-1 自報「這是大任務」→ 把第一輪當成第一輪，從第二輪 resume 續跑。

    `already_ran_first` 漏傳的話，迴圈會把同一個請求**再跑一次**第一輪；
    `ack_override` 漏傳的話，進入自走完全沒有 ack——使用者看到一則答案之後就是
    長時間的安靜，而背景其實正在跑。
    """
    turn_env.script = [(f"{OPEN} 這題我來慢慢做", "cc-new", {})]
    _turn(turn_env)
    assert len(turn_env.loop_calls) == 1
    args, kwargs = turn_env.loop_calls[0]
    assert args[2] is None, "佔位訊息已被 turn-1 用掉，不能再交給迴圈"
    assert kwargs["already_ran_first"] is True
    assert kwargs["ack_override"] == b.DOROSSI_SELF_JUDGE_ACK
    assert turn_env.compact_calls == [], "轉進迴圈就 return，壓縮由迴圈自己做"


def test_the_open_sentinel_never_reaches_the_channel(turn_env):
    """哨符是內部訊號，對外必須看不見——串流預覽與最終答案都是。"""
    turn_env.script = [(f"{OPEN} 這題我來慢慢做", "cc-new", {})]
    _turn(turn_env)
    said = _said(turn_env)
    assert OPEN not in said, f"開場哨符漏進對外訊息：{said!r}"
    assert "這題我來慢慢做" in said, "剝掉哨符不等於把正文一起丟掉"


def test_a_sentinel_only_first_turn_still_closes_its_live_message(turn_env):
    """turn-1 只有哨符、沒有正文 → 先把這則 live 收掉（停掉串流背景任務）。

    不收的話，那則訊息會一直停在「處理中…」，而串流的背景任務還掛在上面——迴圈
    另外發的 ack 出現在下面，使用者看到兩則訊息在講同一件事。
    """
    turn_env.script = [(OPEN, "cc-new", {})]
    _turn(turn_env)
    assert turn_env.final_chunks == [], "沒有正文就沒有東西可貼"
    assert "🔁 收到，開始持續推進…" in _said(turn_env)
    assert turn_env.loop_calls, "空正文的哨符一樣要轉進迴圈"


def test_the_self_judge_preview_strips_the_sentinel_while_it_streams(turn_env):
    """自判路徑的 `on_text` 換成會剝哨符的版本，否則哨符會在預覽裡一閃而過。

    那一閃沒有任何紀錄：最終答案是乾淨的，log 也是乾淨的，只有當下看著訊息的人
    看得到。
    """
    _turn(turn_env)
    on_text = _kwargs_of(turn_env)["on_text"]
    on_text(f"正在想 {OPEN}")
    assert not any(OPEN in str(x) for x in turn_env.live), turn_env.live


def test_without_self_judge_the_preview_is_the_plain_live_update(turn_env,
                                                                 monkeypatch):
    """反面欄杆：kill-switch 關掉時 `on_text` 就是 `live.update` 本身。

    兩條路共用一個回呼的話，上一支測試會在「自判被關掉」時照樣通過，而它要釘的
    正是「自判路徑**換了**回呼」。
    """
    monkeypatch.setattr(b, "DOROSSI_SELF_JUDGE_ENABLED", False)
    _turn(turn_env)
    on_text = _kwargs_of(turn_env)["on_text"]
    assert getattr(on_text, "__name__", "") == "update"


# --------------------------------------------------------------------------
# 七、工作階段消失時的重試
# --------------------------------------------------------------------------

@pytest.mark.parametrize("backend, stored_key, stored", [
    ("claude_code", "cc_session_id", "cc-old"),
    ("codex", "codex_session_id", "cx-old"),
])
def test_a_vanished_backend_session_is_retried_from_scratch(
        turn_env, monkeypatch, backend, stored_key, stored):
    """存著的工作階段不見了（過期／被清）→ 以新工作階段重試一次。

    不重試的話，一段被後端清掉的對話會**永久**壞掉：每一輪都拿同一個死掉的 id 去
    resume，每一輪都回同一句泛用失敗，而使用者完全不知道要去開新對話。
    """
    monkeypatch.setattr(b, "DOROSSI_BACKEND", backend)
    slot = _slot(turn_env)
    slot.pop("cc_session_id", None)
    slot[stored_key] = stored
    turn_env.script = [db._DorossiResumeError("gone"),
                       ("重試的答案", "new-id", {})]
    _turn(turn_env)
    assert [call[2] for call in turn_env.calls] == [stored, None], (
        "重試必須把工作階段 id 丟掉，否則是拿同一個死 id 再試一次")
    assert "重試的答案" in _said(turn_env)


def test_the_failed_attempts_partial_text_is_cleared_before_the_retry(turn_env):
    """重試前先把 live 收掉再重開——失敗那次串出來的半截文字不能留在畫面上。

    留著的話，使用者會看到「上一次失敗的半句話」和「這一次的答案」接在一起，
    而那看起來就只是模型講話顛三倒四。
    """
    turn_env.script = [db._DorossiResumeError("gone"), ("答案", "cc-new", {})]
    _turn(turn_env)
    assert "⏳ 重新整理對話中…" in turn_env.live
    assert "<reopen>" in turn_env.live
    assert turn_env.live.index("⏳ 重新整理對話中…") < turn_env.live.index("<reopen>")


def test_the_retry_keeps_this_turns_directives(turn_env):
    """重試是同一輪的第二次嘗試，微調不能在重試時掉回預設。"""
    turn_env.script = [db._DorossiResumeError("gone"), ("答案", "cc-new", {})]
    _turn(turn_env, effort="high", model_tier=MODEL_KEY)
    retry = _kwargs_of(turn_env, 1)
    assert retry["effort"] == "high" and retry["model"] == MODEL_VALUE


# --------------------------------------------------------------------------
# 八、推進存檔與送出
# --------------------------------------------------------------------------

def test_the_advance_is_persisted_before_the_answer_is_delivered(turn_env):
    """先存檔、再回覆。

    倒過來的話，「送出之後、存檔之前」那一小段被 `!restart` 打斷，使用者拿到了
    答案而工作階段沒有推進——下一輪 resume 回上一輪，模型看起來「忘了自己剛說過
    的話」。
    """
    _turn(turn_env)
    order = turn_env.order
    assert order.index("rmw:_save_mut") < order.index("reply"), order
    assert _slot(turn_env).get("cc_session_id") == "cc-new"


def test_a_slot_deleted_mid_call_swallows_the_advance_instead_of_misfiling_it(
        turn_env):
    """後端呼叫期間 slot 被刪掉 → 這次推進整個丟掉，**不可以**寫到別的 slot。

    寫錯地方的後果是最難查的一種：另一段完全不相干的對話突然 resume 到這一輪的
    工作階段，使用者看到的是「它在回答我沒問過的問題」。
    """
    def _delete(env):
        env.state[UID]["sessions"].pop(SID)
        return ("答案", "cc-new", {})

    turn_env.script = [_delete]
    _turn(turn_env)
    assert _slot(turn_env) is None
    assert _slot(turn_env, OTHER_SID).get("cc_session_id") == "cc-other", (
        "slot 不見了就整筆丟掉，不得退回去寫進目前作用中的那一個")
    assert "答案" in _said(turn_env), "存不進去不代表不用回答"


def test_the_usage_baseline_is_stored_with_the_session_it_belongs_to(turn_env):
    """累計用量的基準值要跟工作階段 id 在**同一次** RMW 寫進去。

    分開寫就有機會只寫到一半：基準值對應的是另一個工作階段，下一輪相減出來的
    「這一輪用了多少」會是一個沒有意義的數字（可能是負的）。
    """
    turn_env.script = [("答案", "cc-new", {"usage_mark": {"cost": 1.5}})]
    _turn(turn_env)
    slot = _slot(turn_env)
    assert slot.get("cc_usage_mark") == {"cost": 1.5}
    assert slot.get("cc_session_id") == "cc-new"


def test_the_api_backend_carries_its_history_in_and_out(turn_env, monkeypatch):
    """api 後端沒有工作階段 id，脈絡**就是**那份對話紀錄本身——進去要帶、出來要存。

    第一版這支只斷言「帶進去的是 `[]`」，而 slot 本來就沒有紀錄——於是把整個欄位
    換成字面量 `[]` 的變異活了下來。**一個等於預設值的斷言等於沒有斷言**：要測
    「有沒有真的讀出來」，讀到的東西就不能跟沒讀到長得一樣。
    """
    monkeypatch.setattr(b, "DOROSSI_BACKEND", "api")
    before = [{"role": "user", "content": "之前說過的話"}]
    _slot(turn_env)["api_history"] = list(before)
    turn_env.script = [("答案", before + [{"role": "assistant"}])]
    _turn(turn_env)
    slot = _slot(turn_env)
    assert turn_env.calls[0][3]["history"] == before, (
        "api 後端沒有 resume，不帶紀錄進去就是每一輪都從頭開始")
    assert slot.get("api_history") == before + [{"role": "assistant"}]
    assert slot.get("cc_session_id") == "cc-old", "api 路徑不該動到另一個後端的 id"


@pytest.mark.parametrize("answer, images, expected", [
    ("有話要說", 0, "reply"),
    ("", 0, "Dorossi 沒有產生任何回覆。"),
    ("", 2, "✅ 圖片已產生。"),
    ("有話要說", 2, "reply"),
])
def test_the_three_endings_are_mutually_exclusive_and_exhaustive(
        turn_env, answer, images, expected):
    """三個分支覆蓋所有組合，而且只走其中一條。

    窮盡性不是風格問題：漏掉「沒有答案也沒有圖」那一格，那一輪就是**完全沒有
    回覆**——佔位訊息永遠停在「處理中…」，使用者無從判斷是還在跑還是壞了。
    """
    turn_env.images = images
    turn_env.script = [(answer, "cc-new", {"images": ["a.png"] * images})]
    _turn(turn_env)
    if expected == "reply":
        assert turn_env.final_chunks == [[answer]]
    else:
        assert turn_env.final_chunks == []
        assert expected in _said(turn_env)


def test_images_are_delivered_before_the_text_ending_is_decided(turn_env):
    """「有沒有送出圖」是三分支的輸入之一，所以送圖一定要在判斷**之前**。

    順序反了的話，`sent_image_count` 永遠是 0：一輪只產圖、沒有文字的回合會被
    說成「沒有產生任何回覆」，而圖其實就在下一則訊息裡。
    """
    turn_env.images = 1
    turn_env.script = [("", "cc-new", {"images": ["a.png"]})]
    _turn(turn_env)
    assert turn_env.image_paths == [["a.png"]]
    assert turn_env.order.index("images") < turn_env.order.index("live.finalize")


@pytest.mark.parametrize("backend, new_sid, expected", [
    ("claude_code", "cc-new", True),
    ("claude_code", None, False),
    ("codex", "cx-new", False),
    ("api", None, False),
])
def test_compaction_only_follows_a_turn_that_advanced_a_claude_code_session(
        turn_env, monkeypatch, backend, new_sid, expected):
    """壓縮的對象是「本輪推進後的那個工作階段 id」，所以兩個條件缺一不可。

    放寬的話會拿 `None` 或另一個後端的 id 去壓縮一個不存在的工作階段——全程靜默
    （這條路刻意吞掉所有錯），於是沒有任何人會發現壓縮其實從來沒成功過。
    """
    monkeypatch.setattr(b, "DOROSSI_BACKEND", backend)
    if backend == "api":
        turn_env.script = [("答案", [{"role": "assistant"}])]
    else:
        turn_env.script = [("答案", new_sid, {})]
    _turn(turn_env)
    assert bool(turn_env.compact_calls) is expected
    if expected:
        uid, sid, cc_sid, _snap, _info = turn_env.compact_calls[0]
        assert (uid, sid, cc_sid) == (UID, SID, "cc-new")


def test_the_backend_call_happens_inside_the_concurrency_semaphore(turn_env):
    """整個後端回合都在號誌裡（hazard #3），而 typing 也在裡面。

    號誌只圍住一小段的話，同時在跑的後端行程就不再有上限——每一個都是一支
    `claude -p` 子行程，壓垮的是這台機器本身，而症狀是「bot 變慢了」。
    """
    _turn(turn_env)
    order = turn_env.order
    assert order.index("sem") < order.index("typing") < order.index("backend")
    assert order.index("backend") < order.index("/sem")


def test_the_state_lock_is_released_while_the_backend_runs(turn_env):
    """狀態 RMW 一次一次地做，後端呼叫夾在兩次之間——不是包在一次裡面。

    包在裡面的話，一輪動輒數分鐘的後端呼叫會把狀態短鎖佔滿全程，其他 session
    的回合全部卡住等它，而外部看起來只是「bot 沒反應」。
    """
    _turn(turn_env)
    assert turn_env.rmw_names == ["_provider_mut", "_prep_mut", "_save_mut"]
    order = turn_env.order
    assert order.index("rmw:_prep_mut") < order.index("backend")
    assert order.index("backend") < order.index("rmw:_save_mut")


# --------------------------------------------------------------------------
# 九、兩條收尾例外
# --------------------------------------------------------------------------

def test_a_usage_limit_parks_the_turn_instead_of_dropping_it(turn_env):
    """用量上限是**可行動**的狀況（知道什麼時候會回來），不是一句「請稍後再試」。

    2026-09-23 起更進一步：不只是講一句「等一下再問」，而是把這一輪**停進佇列**、
    時間到自己重跑。所以這裡不該出現診斷摘要——停進佇列不是失敗，附一句
    「請查看 log」只會讓擁有者以為這一題沒了、再問一次。
    """
    turn_env.script = [db._DorossiUsageLimitError("limit")]
    _turn(turn_env)
    said = _said(turn_env)
    assert "Dorossi 暫時無法回應，請稍後再試。" not in said, said
    assert "會自動重跑" in said, said
    assert "DIAG:dorossi-limit" not in said, said
    rows = b._dorossi_queue_read()
    assert [r["status"] for r in rows] == ["parked"], rows


def test_a_usage_limit_that_cannot_be_parked_still_says_so(turn_env):
    """停不進佇列（錨點不成立——之後查不回當初那一次請求）就退回舊行為。

    退回的那一句必須**仍然**是用量上限專屬的說法 ＋ 診斷摘要：這時候這一題真的沒了，
    不講就變成靜默失敗。
    """
    turn_env.message.id = None      # 沒有可用的錨點
    turn_env.script = [db._DorossiUsageLimitError("limit")]
    _turn(turn_env)
    said = _said(turn_env)
    assert "Dorossi 暫時無法回應，請稍後再試。" not in said, said
    assert "DIAG:dorossi-limit" in said, said
    assert b._dorossi_queue_read() == [], "停不進去卻留了一列"


def test_any_other_failure_is_answered_generically_and_never_escapes(turn_env):
    """其餘的錯一律泛用回覆 ＋ 診斷摘要，而且不往外拋。

    往外拋就會進到 `on_message` 的泛用處理，那一層不知道有一則佔位訊息還停在
    「處理中…」——指令看起來就這樣沒有下文了。
    """
    turn_env.script = [RuntimeError("後端炸了")]
    _turn(turn_env)
    said = _said(turn_env)
    assert "Dorossi 暫時無法回應，請稍後再試。" in said
    assert "DIAG:dorossi" in said
    assert "後端炸了" not in said, "原始例外文字不得進入對外訊息（Layer 1）"


@pytest.mark.parametrize("error", [
    RuntimeError("x"), OSError("x"), ValueError("x"),
    db._DorossiUsageLimitError("x"),
])
def test_no_backend_failure_ever_leaves_the_turn(turn_env, error):
    """窮盡一點：整輪的合約是「絕不往外拋」。"""
    _turn(turn_env)          # 先確認正常路徑本來就不拋
    turn_env.script = [error]
    _turn(turn_env)
    assert turn_env.live, "出錯了至少要說一句話"


def test_the_failure_reply_still_uses_the_same_single_message(turn_env):
    """失敗一樣是「一輪一則」：live 收尾 ＋ 診斷附在後面，不是另開一則失敗訊息。"""
    turn_env.script = [RuntimeError("後端炸了")]
    _turn(turn_env)
    assert turn_env.order.index("live.finalize") < turn_env.order.index(
        "safe_reply"), turn_env.order


# --------------------------------------------------------------------------
# 十、空位滿了：等號誌的那段期間不能說「處理中」
# --------------------------------------------------------------------------

class _RealSem:
    """真的 `asyncio.Semaphore`，外面包一層記下取得／釋放的順序。

    `_Sem` 永遠有空位，測不到「排在號誌上」那段期間；這一個是真的會擋。"""

    def __init__(self, order, sem):
        self.order = order
        self.sem = sem

    def locked(self):
        return self.sem.locked()

    async def __aenter__(self):
        await self.sem.acquire()
        self.order.append("sem")
        return self

    async def __aexit__(self, *_exc):
        self.order.append("/sem")
        self.sem.release()
        return False


def _wait_text(cap):
    return f"⏳ 等候空位（同時執行上限 {cap}）…"


def test_a_free_slot_goes_straight_to_processing(turn_env):
    """對照組：有空位時跟以前一個字都不差。"""
    _turn(turn_env)
    assert turn_env.live[0] == "⏳ 處理中…"
    assert not any("等候空位" in str(x) for x in turn_env.live), turn_env.live


def test_a_full_backend_says_it_is_waiting_until_a_slot_frees(turn_env, monkeypatch):
    """所有空位都被別的回合佔著：先說在等空位（附上限），拿到之後才換成「處理中」。

    以前這段期間顯示的是「⏳ 處理中…」，而後端根本還沒開始——擁有者看到的是一則
    「處理中」卻幾分鐘沒有任何進度。"""
    monkeypatch.setattr(b, "DOROSSI_CC_MAX_PARALLEL", 2)
    monkeypatch.setattr(b, "_dorossi_turns", [])
    seen = {}

    def during_backend(env):
        seen["phase"] = [t.phase for t in b._dorossi_turns]
        return ("答案", "cc-new", {})

    turn_env.script = [during_backend]

    async def scenario():
        sem = asyncio.Semaphore(1)
        await sem.acquire()                      # 唯一的空位被別人拿著
        monkeypatch.setattr(b, "_dorossi_backend_sem", _RealSem(turn_env.order, sem))
        task = asyncio.ensure_future(b._dorossi_process_turn(
            turn_env.message, "查一下", turn_env.placeholder, UID, SID))
        for _ in range(20):
            await asyncio.sleep(0)
        assert turn_env.calls == [], "還沒拿到空位就不該呼叫後端"
        assert turn_env.live == [_wait_text(2)], turn_env.live
        assert [t.phase for t in b._dorossi_turns] == ["wait"]
        sem.release()                            # 別人的回合結束了
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert seen["phase"] == ["run"], "拿到空位之後登記要換成「執行中」"
    assert "<update>⏳ 處理中…" in turn_env.live, turn_env.live
    assert turn_env.live.index(_wait_text(2)) < turn_env.live.index("<update>⏳ 處理中…")
    assert b._dorossi_turns == [], "回合結束要移除登記"


def test_switching_back_to_processing_does_not_hold_the_slot(turn_env, monkeypatch):
    """拿著空位的時候不能 await 一次訊息編輯——空位只該花在後端上。

    換回「處理中」走 `live.update`（背景送出）；若改成 `await live.finalize(...)`，
    每一輪都會多佔一次網路往返的空位，而排在後面的人就多等那麼久。"""
    monkeypatch.setattr(b, "_dorossi_turns", [])

    async def scenario():
        sem = asyncio.Semaphore(1)
        await sem.acquire()
        monkeypatch.setattr(b, "_dorossi_backend_sem", _RealSem(turn_env.order, sem))
        task = asyncio.ensure_future(b._dorossi_process_turn(
            turn_env.message, "查一下", turn_env.placeholder, UID, SID))
        for _ in range(20):
            await asyncio.sleep(0)
        sem.release()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    order = turn_env.order
    held = order[order.index("sem"):order.index("backend")]
    assert "live.finalize" not in held and "live.reopen" not in held, order


def test_a_slot_taken_while_the_message_was_being_sent_still_shows_waiting(
        turn_env, monkeypatch):
    """送出那則訊息的 await 期間，最後一個空位被別的回合拿走：一樣要改口說在等。"""
    monkeypatch.setattr(b, "DOROSSI_CC_MAX_PARALLEL", 1)
    monkeypatch.setattr(b, "_dorossi_turns", [])
    box = {}

    async def ensure_live_then_lose_the_slot(_message, placeholder, initial):
        turn_env.live.append(initial)
        await box["sem"].acquire()               # 別的回合搶走了最後一個空位
        return b._DorossiLiveMessage(placeholder, prime=initial)

    monkeypatch.setattr(b, "_dorossi_ensure_live", ensure_live_then_lose_the_slot)

    async def scenario():
        box["sem"] = asyncio.Semaphore(1)
        monkeypatch.setattr(b, "_dorossi_backend_sem",
                            _RealSem(turn_env.order, box["sem"]))
        task = asyncio.ensure_future(b._dorossi_process_turn(
            turn_env.message, "查一下", turn_env.placeholder, UID, SID))
        for _ in range(20):
            await asyncio.sleep(0)
        assert turn_env.calls == []
        assert turn_env.live == ["⏳ 處理中…", f"<update>{_wait_text(1)}"], turn_env.live
        box["sem"].release()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert turn_env.live[-1:] != [f"<update>{_wait_text(1)}"]
    assert "<update>⏳ 處理中…" in turn_env.live, turn_env.live


def test_the_backend_process_is_handed_to_the_registry(turn_env, monkeypatch):
    """單輪回合也把後端子行程交給登記（`on_proc`），`/dorossi running` 才報得出 PID。"""
    monkeypatch.setattr(b, "_dorossi_turns", [])
    seen = {}

    def during_backend(env):
        env.calls[-1][3]["on_proc"](types.SimpleNamespace(pid=4321, returncode=None))
        owner = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))
        seen["report"] = b._dorossi_running_report(env.state, owner)
        return ("答案", "cc-new", {})

    turn_env.script = [during_backend]
    _turn(turn_env)
    line = next(line for line in seen["report"].splitlines() if f"`{SID}`" in line)
    assert "PID 4321" in line and "執行中" in line and "後端 A" in line, line


@pytest.mark.parametrize("backend", ["claude_code", "codex"])
def test_every_backend_call_that_spawns_a_process_reports_it(turn_env, monkeypatch,
                                                             backend):
    """含「工作階段失效、重開一次」那一條：重試的行程同樣要交出來。"""
    monkeypatch.setattr(b, "DOROSSI_BACKEND", backend)
    fresh = ("答案", "new", {})
    turn_env.script = [db._DorossiResumeError("gone"), fresh]
    _turn(turn_env)
    assert len(turn_env.calls) == 2
    assert all(callable(call[3].get("on_proc")) for call in turn_env.calls)


def test_background_compaction_is_told_which_turn_it_belongs_to(turn_env):
    turn_env.script = [("答案", "cc-new", {})]
    _turn(turn_env)
    assert len(turn_env.compact_turns) == 1
    turn = turn_env.compact_turns[0]
    assert (turn.uid, turn.sid) == (UID, SID)


def test_handing_over_to_the_loop_is_marked_on_the_registry(turn_env, monkeypatch):
    """轉進自走之後由迴圈那一筆代表；登記要標成 `loop`，列表才不會把它列兩次。"""
    monkeypatch.setattr(b, "_dorossi_turns", [])
    phases = []

    async def fake_run_loop(*_args, **_kwargs):
        phases.append([t.phase for t in b._dorossi_turns])

    monkeypatch.setattr(b, "_dorossi_run_loop", fake_run_loop)
    _turn(turn_env, "整理測試，不要問我")
    assert phases == [["loop"]]
    assert b._dorossi_turns == []


# --------------------------------------------------------------------------
# 十一、後端連不上伺服器：等網路回來，用同一個工作階段重跑（2026-09-22）
# --------------------------------------------------------------------------

@pytest.fixture
def online(monkeypatch):
    seen: list = []

    async def fake_wait_online(st, target, *, attempt=1):
        seen.append((target, attempt, st.phase))
        return not st.abort

    monkeypatch.setattr(b, "_dorossi_wait_online", fake_wait_online)
    monkeypatch.setattr(b, "_dorossi_turns", [])
    return seen


@pytest.mark.parametrize("backend, sid_key", [
    ("claude_code", "cc-old"), ("codex", None), ("api", None)])
def test_an_offline_backend_is_retried_with_the_same_session(turn_env, online,
                                                             monkeypatch, backend,
                                                             sid_key):
    """以前：判成工作階段過舊 → 丟掉脈絡開新的（一樣連不上）→ 回一句泛用失敗。"""
    monkeypatch.setattr(b, "DOROSSI_BACKEND", backend)
    offline = db._DorossiOfflineError("ENOTFOUND", backend=backend)
    ok = ("答案", [{"role": "assistant"}]) if backend == "api" else ("答案", "new", {})
    turn_env.script = [offline, offline, ok]
    _turn(turn_env)
    assert len(turn_env.calls) == 3
    if sid_key:
        assert [c[2] for c in turn_env.calls] == [sid_key] * 3, (
            "重跑的必須是同一個工作階段，不是新開的")
    assert online == [(backend, 1, "offline"), (backend, 2, "offline")]
    assert any("網路中斷" in str(x) for x in turn_env.live), turn_env.live
    assert turn_env.final_chunks == [["答案"]]
    assert "DIAG" not in _said(turn_env), "斷網等回來之後不該被當成失敗"


def test_an_offline_turn_can_be_aborted(turn_env, online):
    def offline_and_abort(env):
        b._dorossi_turns[0].request_abort()
        return db._DorossiOfflineError("ENOTFOUND", backend="claude_code")

    turn_env.script = [offline_and_abort]
    _turn(turn_env)
    assert len(turn_env.calls) == 1
    assert any("已中止" in str(x) for x in turn_env.live), turn_env.live
    assert "DIAG" not in _said(turn_env)
    assert b._dorossi_turns == []
