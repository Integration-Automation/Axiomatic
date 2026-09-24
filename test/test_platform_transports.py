"""對話平台介接層與第二個平台的 transport。

這一批守的是**接縫**，不是某一個平台的細節——後面還會有三個平台接在同一個介面
上，所以「介面自己站不站得住」比「某一支 API 呼叫對不對」重要得多。

四件事：

1. **身分映射是 fail-closed 的，而且結構性地是。** 非擁有者一律映成負數，所以
   「不等於 `OWNER_USER_ID`」不是靠記得寫對比較式，是型別上就不可能相等；負數也
   不可能出現在 `user_roles` / `path_reveal_channel_ids`（那兩個的 coercer 只收
   `>= 0`），所以別的平台的使用者拿不到角色、也拿不到可露路徑的表面。
2. **註冊表兩個方向都對帳。** 磁碟上多一個 `_*_transport.py` 卻沒列進
   `TRANSPORT_MODULES`，症狀是「那個平台整個不存在」——而那跟「沒設定所以沒啟用」
   長得一模一樣，這正是本 repo 一再點名的形狀。
3. **做不到的事要看得見。** 能力旗標關著時 `edit()` 丟 `UnsupportedOperation`，
   不是安靜地什麼都沒發生；降級的那一種（`ReplaceOnEditMessage`）行為也釘住。
4. **送出去與印出去的字都不得帶主機內部資訊。** 憑證在網址裡，所以「出錯時印
   `str(error)`」會把整個憑證寫進記錄檔，而記錄檔有 `/log tail` 這個使用者面的
   出口。

這裡**不碰真的網路、也不碰真的憑證**：所有 HTTP 都用替身。
"""
import ast
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _bot_config as bo  # noqa: E402
import _chat_platform as cp  # noqa: E402
import _platform_runtime as pr  # noqa: E402
import _telegram_transport as tg  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PKG_ROOT.parent


# ---------------------------------------------------------------------------
# 註冊表：磁碟 ↔ 清單，兩個方向
# ---------------------------------------------------------------------------
def _transport_files() -> set[str]:
    return {p.stem for p in PKG_ROOT.glob("_*_transport.py")}


def test_every_transport_module_on_disk_is_registered():
    """多一個檔案沒列進清單 ＝ 那個平台整個不存在，而且看起來像「沒設定」。"""
    on_disk = _transport_files()
    assert on_disk, "一個 transport 模組都找不到——glob 壞了，這支等於沒在對帳"
    missing = sorted(on_disk - set(cp.TRANSPORT_MODULES))
    assert not missing, (
        f"這些 transport 模組沒有列進 `_chat_platform.TRANSPORT_MODULES`：{missing}。"
        "沒列到的模組不會被 import，於是它的 `register_transport` 不會跑，那個平台"
        "就算設定檔寫好了也不會啟用——而症狀跟「沒設定」完全一樣。")


def test_the_transport_list_has_no_stale_entry():
    """反方向：列著一個不存在的模組，`import_transport_modules()` 會當場炸掉。"""
    stale = sorted(set(cp.TRANSPORT_MODULES) - _transport_files())
    assert not stale, f"`TRANSPORT_MODULES` 列著磁碟上沒有的模組：{stale}"


def test_importing_the_transport_modules_registers_them():
    """正面對照：import 之後註冊表真的長出東西（空的註冊表跟乾淨長得一樣）。"""
    cp.import_transport_modules()
    assert tg.PLATFORM_NAME in cp.registered_transports()


def test_the_adapter_layer_does_not_import_the_bot():
    """`_chat_platform` import `discord_bot` 就是循環——而且那條循環會在 import 期
    炸掉整支 bot。另外它也刻意不 import 函式庫本身：附件與嵌入訊息一律鴨子型別。"""
    tree = ast.parse((PKG_ROOT / "_chat_platform.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "discord_bot" not in imported
    assert "discord" not in imported, (
        "介接層 import 了聊天函式庫——那會讓「第二個平台」在概念上仍然掛在第一個"
        "平台的型別上，而這個模組存在的理由就是把那層相依拿掉。")


# ---------------------------------------------------------------------------
# 身分：fail-closed
# ---------------------------------------------------------------------------
# 合成的擁有者 id，與 `conftest._TEST_OWNER_USER_ID` 同一個形狀（18 位、開頭 4）。
# **不要放真的 id**：這個 repo 是給別人用的，一個真實身分進到版本庫就是不可逆的。
_OWNER = 400000000000000001


@pytest.mark.parametrize("given", [None, "", "   ", 0, "0"])
def test_an_unresolvable_sender_is_never_the_owner(given):
    """取不到身分一律不是擁有者——與既有三道閘同一個立場。"""
    uid, is_owner = cp.resolve_identity("p", given, ("777",), _OWNER)
    assert is_owner is False
    assert uid != _OWNER


def test_the_configured_owner_maps_onto_the_one_owner_id():
    uid, is_owner = cp.resolve_identity("p", 777, ("777",), _OWNER)
    assert (uid, is_owner) == (_OWNER, True)


def test_an_unknown_sender_gets_a_negative_id():
    """負數是這條規則的**結構**保證，不是巧合。

    正數的話，「別的平台的某個 id 剛好等於 `OWNER_USER_ID`」或「剛好列在
    `user_roles` 裡」就會變成一條沒有人想得到的提權路徑。負數不可能——那兩個鍵的
    coercer 只收 `>= 0`。
    """
    uid, is_owner = cp.resolve_identity("p", "888", ("777",), _OWNER)
    assert is_owner is False
    assert uid < 0
    assert bo._coerce_int_list([uid]) == [], (
        "負數竟然過得了 id 清單的 coercer——那條「拿不到角色」的保證沒了")


def test_the_same_sender_always_gets_the_same_id():
    """稽核記錄與指令計數要分得出人，所以映射必須穩定。"""
    first = cp.resolve_identity("p", "888", (), _OWNER)[0]
    second = cp.resolve_identity("p", "888", (), _OWNER)[0]
    assert first == second
    assert cp.resolve_identity("q", "888", (), _OWNER)[0] != first, (
        "兩個平台上同號碼的兩個人拿到同一個內部 id——平台名沒有進雜湊")


def test_an_allow_listed_conversation_is_the_command_channel():
    uid, is_command = cp.conversation_uid(
        "p", "-100", command_channel_id=4242, allowed_chat_ids=("-100",))
    assert (uid, is_command) == (4242, True)


def test_any_other_conversation_is_not_the_command_channel():
    uid, is_command = cp.conversation_uid(
        "p", "-200", command_channel_id=4242, allowed_chat_ids=("-100",))
    assert is_command is False
    assert uid < 0
    assert bo._coerce_int_list([uid]) == [], (
        "對話 id 竟然進得了頻道清單——那等於別的平台的對話可能變成"
        "「可露路徑的表面」")


# ---------------------------------------------------------------------------
# 切塊與送出引數正規化
# ---------------------------------------------------------------------------
def test_chunking_respects_the_limit():
    chunks = cp.chunk_text("a" * 50, 10)
    assert chunks and all(len(c) <= 10 for c in chunks)
    assert "".join(chunks) == "a" * 50


def test_chunking_prefers_a_line_break():
    chunks = cp.chunk_text("12345\n67890abc", 8)
    assert chunks[0] == "12345"


def test_a_code_block_that_spans_a_cut_is_closed_and_reopened():
    """不補的話下一則會把程式碼當散文顯示，而它的收尾分隔符號會開一個新區塊，
    把後面整段吞進去。"""
    text = "```py\n" + "x = 1\n" * 20 + "```"
    chunks = cp.chunk_text(text, 40)
    assert len(chunks) > 1
    assert chunks[0].endswith("```")
    assert chunks[1].startswith("```py")


def test_chunking_an_empty_string_sends_nothing():
    assert cp.chunk_text("   \n  ", 100) == []


def test_chunking_terminates_even_when_the_limit_is_absurdly_small():
    """重開分隔符號會把字加回去，所以上限小到跟它差不多時會**永遠不結束**。

    不是理論上的角落：切塊是純字串函式，下一個平台的上限由它自己決定，而一個不會
    結束的迴圈跑在事件迴圈上等於整個 bot 停住。切不出進度就放棄排版。
    """
    chunks = cp.chunk_text("```py\n" + "x = 1\n" * 5 + "```", 5)
    assert chunks
    assert "".join(c.replace("\n", "") for c in chunks).count("x = 1") == 5


class _FakeFile:
    def __init__(self, name, data):
        self.filename = name
        self.fp = _Buffer(data)


class _Buffer:
    def __init__(self, data):
        self._data = data
        self._read = False

    def seek(self, _pos):
        self._read = False

    def read(self):
        return self._data


def test_an_attachment_is_read_out_of_the_handler_object():
    text, files = cp.outbound_parts("hi", file=_FakeFile("a.png", b"123"))
    assert text == "hi"
    assert [(f.filename, f.data, f.is_image) for f in files] == \
        [("a.png", b"123", True)]


def test_an_unreadable_attachment_degrades_to_text_only():
    """「有文字沒附件」遠好過「什麼都沒有」——`safe_reply` 對同一件事已經是這個
    立場。讀不出來的附件不得把整則訊息帶走。"""

    class _Broken:
        filename = "x.bin"
        fp = None

    text, files = cp.outbound_parts("hi", file=_Broken())
    assert text == "hi" and files == []


class _FakeEmbed:
    class _Field:
        def __init__(self, name, value):
            self.name, self.value = name, value

    class _Footer:
        text = "腳註"

    title = "標題"
    description = "說明"
    footer = _Footer()

    def __init__(self):
        self.fields = [self._Field("欄位", "值")]


def test_an_embed_becomes_text_instead_of_vanishing():
    """沒有嵌入訊息的平台看到的必須是同樣的內容——整塊消失才是最貴的失敗。"""
    text, _files = cp.outbound_parts("前言", embed=_FakeEmbed())
    for expected in ("前言", "標題", "說明", "欄位: 值", "腳註"):
        assert expected in text


# ---------------------------------------------------------------------------
# 做不到的事要看得見
# ---------------------------------------------------------------------------
class _StubTransport(cp.ChatTransport):
    name = "stub"

    def __init__(self, capabilities):
        self._caps = capabilities
        self.sent: list = []
        self.edits: list = []

    @property
    def capabilities(self):
        return self._caps

    async def run(self):
        await asyncio.sleep(0)

    async def deliver(self, channel, content=None, **kwargs):
        self.sent.append(content)
        return cp.SentChatMessage(channel, str(len(self.sent)), str(content))

    async def revise(self, sent, content, **kwargs):
        self.edits.append(content)


def _stub(**caps):
    transport = _StubTransport(cp.PlatformCapabilities(**caps))
    channel = cp.ChatConversation(transport, "c1", uid=-5, is_direct=True,
                                  is_command_chat=True)
    return transport, channel


@pytest.mark.parametrize("reply_reference", [True, False])
def test_a_sent_message_can_be_replied_to(reply_reference):
    """事後的結果回在 bot 自己送出的那一則底下（`/gen image` 的佔位訊息）。平台支援
    引用就帶上那一則的 id，不支援就單純送進同一個對話——兩種都送得出去。"""
    seen: list = []

    class _Recording(_StubTransport):
        async def deliver(self, channel, content=None, **kwargs):
            seen.append((content, kwargs.get("reply_to")))
            return await super().deliver(channel, content, **kwargs)

    transport = _Recording(cp.PlatformCapabilities(reply_reference=reply_reference))
    channel = cp.ChatConversation(transport, "c1", uid=-5, is_direct=True,
                                  is_command_chat=True)
    placeholder = cp.SentChatMessage(channel, "41", "產圖中…")
    sent = asyncio.run(placeholder.reply("好了", mention_author=True))
    assert sent is not None
    assert seen == [("好了", "41" if reply_reference else None)]


def test_a_generated_image_reaches_the_platform_it_was_asked_on(monkeypatch, tmp_path):
    """`/gen image` 從別的平台下的：結果要回到那個對話，不是既有平台的設定頻道，也不是
    消失。

    修之前的實況：ctx 的 `channel_id` 是負數、`client.get_channel` 找不到，於是退回
    事件監看的頻道；回覆目標是「產圖中…」那一則（`SentChatMessage`），而它沒有
    `reply`——`safe_reply` 丟 `AttributeError`，外層吞進 stderr，整張圖不見、佔位訊息
    永遠停在「產圖中」。這支走真的 `_handle_single_image_done`。"""
    import discord_bot as b

    delivered: list = []

    class _Recording(_StubTransport):
        async def deliver(self, channel, content=None, **kwargs):
            delivered.append((content, sorted(kwargs)))
            return await super().deliver(channel, content, **kwargs)

    transport = _Recording(cp.PlatformCapabilities(reply_reference=True))
    channel = cp.ChatConversation(transport, "c1", uid=-5, is_direct=True,
                                  is_command_chat=True)
    placeholder = cp.SentChatMessage(channel, "41", "產圖中…")
    image = tmp_path / "output" / "_oneshot" / "rid" / "one.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)

    primary: list = []

    class _Primary:
        async def send(self, content=None, **_kwargs):
            primary.append(content)

    monkeypatch.setattr(b, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(b, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(b, "_generate_append_history", lambda *_a, **_k: None)
    monkeypatch.setattr(b, "_schedule_coro",
                        lambda coro=None, *_a, **_k: coro.close() if coro else None)
    monkeypatch.setattr(b.client, "get_channel", lambda *_a, **_k: None)
    monkeypatch.setattr(b, "_single_image_pending", {"rid": {
        "channel_id": channel.id, "message_id": -77, "placeholder": placeholder}})
    asyncio.run(b._handle_single_image_done(
        _Primary(), {"request_id": "rid", "ok": True,
                     "path": "output/_oneshot/rid/one.png"}))
    assert primary == [], "結果跑到既有平台的頻道去了"
    assert len(delivered) == 1 and delivered[0][0] == "🖼️ 你要的圖來了。", delivered
    assert "reply_to" in delivered[0][1], delivered


class _Rebuilding(_StubTransport):
    """`conversation_for` 認得一個對話 id；其他一律找不回來。"""

    def __init__(self, known: str = "c1"):
        super().__init__(cp.PlatformCapabilities())
        self.known = known

    def conversation_for(self, platform_chat_id):
        if platform_chat_id != self.known:
            return None
        return cp.ChatConversation(self, platform_chat_id, uid=-5, is_direct=True,
                                   is_command_chat=False)


def test_the_origin_of_a_conversation_names_its_platform_and_chat():
    transport = _Rebuilding()
    conv = cp.ChatConversation(transport, "c1", uid=-5, is_direct=True,
                               is_command_chat=False)
    assert cp.origin_of(conv) == {"platform": "stub", "platform_chat_id": "c1"}
    assert cp.origin_of(object()) == {}, "既有平台的頻道不帶這兩個欄位"
    assert cp.origin_of(None) == {}


@pytest.mark.parametrize("record, expected", [
    ({"channel_id": 55}, (False, "none")),                       # 既有平台的紀錄
    ({"platform": "", "platform_chat_id": "c1"}, (False, "none")),
    (None, (False, "none")),
    ({"platform": "stub", "platform_chat_id": "c1"}, (True, "conv")),
    ({"platform": "stub", "platform_chat_id": " c1 "}, (True, "conv")),
    ({"platform": "stub", "platform_chat_id": "c2"}, (True, "none")),   # 沒被授權
    ({"platform": "stub", "platform_chat_id": 7}, (True, "none")),      # 壞資料
    ({"platform": "stub"}, (True, "none")),
    ({"platform": "gone", "platform_chat_id": "c1"}, (True, "none")),   # 平台沒開
])
def test_a_stored_origin_is_resolved_only_through_its_own_platform(record, expected):
    """第一個值分得出「不是別的平台的紀錄」（照舊走既有平台）與「是、但找不回來」（**不**
    退回既有平台——那就是送錯地方）。"""
    is_platform, conv = cp.find_conversation([_Rebuilding()], record)
    assert (is_platform, "conv" if conv is not None else "none") == expected


def test_a_transport_that_explodes_while_rebuilding_is_contained(capsys):
    class _Broken(_Rebuilding):
        def conversation_for(self, platform_chat_id):
            raise RuntimeError("secret detail")

    got = cp.find_conversation([_Broken()], {"platform": "stub", "platform_chat_id": "c1"})
    assert got == (True, None)
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "secret detail" not in err


@pytest.mark.parametrize("chat_id, expected", [
    ("-100", ("group", 4242, True)),        # 允許清單 → 就是這個平台的設定頻道
    ("777", ("private", None, False)),      # 擁有者的私訊
    ("888", None),                          # 陌生人的私訊：磁碟不能決定 bot 跟誰說話
    ("", None),
    ("  ", None),
])
def test_the_platform_only_rebuilds_conversations_it_would_answer_in(
        monkeypatch, tmp_path, chat_id, expected):
    transport = _built(monkeypatch, tmp_path)
    conv = transport.conversation_for(chat_id)
    if expected is None:
        assert conv is None
        return
    kind, uid, is_command_chat = expected
    assert conv is not None and conv.platform_chat_id == chat_id
    assert conv.is_direct is (kind == "private")
    assert conv.is_command_chat is is_command_chat
    if uid is not None:
        assert conv.id == uid
    else:
        assert conv.id < 0
    assert transport.conversation_for(chat_id) is conv, "同一個對話要是同一個物件"


def test_a_schedule_made_on_another_platform_reports_back_there(monkeypatch, tmp_path,
                                                                  capsys):
    """排程是事後才回報的東西，跨重啟也要回得去：`cmd_schedule` 存 `platform`／
    `platform_chat_id`，`_schedule_report` 經那個平台找回對話。找不回來就只寫 log，
    **不**落到既有平台的頻道。對話的整數 id 刻意撞上一個既有平台找得到的頻道。"""
    import discord_bot as b
    import types

    transport = _Rebuilding()
    conv = transport.conversation_for("c1")
    primary: list = []

    class _Primary:
        async def send(self, content=None, **_kw):
            primary.append(content)

    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    monkeypatch.setattr(b, "_is_owner", lambda _m: True)
    monkeypatch.setattr(b, "safe_reply", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(b, "_chat_transports", [transport])
    monkeypatch.setattr(b.client, "get_channel", lambda *_a, **_k: _Primary())
    message = types.SimpleNamespace(channel=conv,
                                    author=types.SimpleNamespace(id=7))
    asyncio.run(b.cmd_schedule(message, "add 09:30 sh echo hi"))
    (entry,) = b._load_schedules()["entries"]
    assert (entry["platform"], entry["platform_chat_id"]) == ("stub", "c1"), entry

    asyncio.run(b._schedule_report(entry, "排程跑完了"))
    assert transport.sent == ["排程跑完了"] and primary == []

    # 找不回來（平台沒開）：什麼都不送，尤其不送到既有平台。
    monkeypatch.setattr(b, "_chat_transports", [])
    capsys.readouterr()
    asyncio.run(b._schedule_report(entry, "第二次"))
    assert transport.sent == ["排程跑完了"] and primary == []
    assert "not reachable" in capsys.readouterr().err, "什麼都沒送卻沒留下原因"

    # 對照組：既有平台建立的排程照舊用頻道 id。
    asyncio.run(b._schedule_report({"id": 2, "channel_id": 55}, "既有平台"))
    assert primary == ["既有平台"]


def test_editing_on_a_platform_that_cannot_edit_is_refused_out_loud():
    """安靜地什麼都沒發生是最貴的失敗形態：呼叫端以為那則訊息更新了。"""
    _transport, channel = _stub(edit_message=False)
    sent = cp.SentChatMessage(channel, "1", "舊的")
    with pytest.raises(cp.UnsupportedOperation):
        asyncio.run(sent.edit("新的"))


def test_editing_on_a_platform_that_can_edit_goes_through():
    transport, channel = _stub(edit_message=True)
    sent = cp.SentChatMessage(channel, "1", "舊的")
    asyncio.run(sent.edit("新的"))
    assert transport.edits == ["新的"]


def test_an_edit_to_identical_content_costs_nothing():
    """去重在介面這一層就做掉，每個 transport 不必各寫一次。"""
    transport, channel = _stub(edit_message=True)
    sent = cp.SentChatMessage(channel, "1", "一樣")
    asyncio.run(sent.edit("一樣"))
    assert transport.edits == []


def test_the_replace_mode_degradation_emits_a_new_message_instead():
    """編輯不動的平台用這一支降級：把編輯變成節流過的新訊息。"""
    transport, channel = _stub(edit_message=False)
    sent = cp.ReplaceOnEditMessage(channel, "1", "舊的", min_interval_sec=0.0)
    asyncio.run(sent.edit("新的"))
    assert transport.sent == ["新的"]


def test_the_replace_mode_throttles_intermediate_updates():
    """串流每個 token 送一則新訊息是洗版，不是即時預覽。"""
    transport, channel = _stub(edit_message=False)

    async def _go():
        sent = cp.ReplaceOnEditMessage(channel, "1", "",
                                       min_interval_sec=3600.0)
        await sent.edit("第一次")
        emitted = list(transport.sent)
        # 補送的背景任務會睡一個小時；測試不等它，但也不要留一個被毀掉的 task。
        if sent._flush is not None:
            sent._flush.cancel()
        return emitted

    assert asyncio.run(_go()) == []


def test_a_throttled_update_is_still_delivered_afterwards():
    """**節流不得把最後一次編輯吃掉。**

    呼叫端送最終答案的方式就是再編輯一次；那一次若落在節流窗裡，純節流的版本會
    安靜地丟掉整個答案——使用者看到一則停在半路的進度訊息，而且沒有任何地方會講。
    """
    transport, channel = _stub(edit_message=False)

    async def _go():
        sent = cp.ReplaceOnEditMessage(channel, "1", "", min_interval_sec=0.05)
        await sent.edit("進度")          # 這一則會被擋下來（剛建好就在窗裡）
        await sent.edit("最終答案")      # 覆蓋掉上一份
        await asyncio.sleep(0.2)         # 讓補送的任務跑完
        return list(transport.sent)

    assert asyncio.run(_go()) == ["最終答案"]


def test_typing_on_a_platform_without_it_is_a_no_op_not_a_crash():
    """typing 是純粹的體感——為了它讓一個指令整個失敗是壞交易。"""
    _transport, channel = _stub(typing_indicator=False)

    async def _go():
        async with channel.typing():
            return True

    assert asyncio.run(_go()) is True


def test_a_reply_carries_the_reference_only_when_the_platform_has_one():
    transport, channel = _stub(reply_reference=False)
    message = cp.ChatMessage(
        author=cp.ChatUser(-9, "u", "someone", is_owner=False),
        channel=channel, content="hi", message_id=-1, platform="stub",
        platform_message_id="7")
    asyncio.run(message.reply("答案"))
    assert transport.sent == ["答案"]


def test_the_author_renders_as_something_an_audit_row_can_identify():
    """稽核記錄寫的是 `str(message.author)`；沒有 `__str__` 那一欄會變成
    `<object at 0x…>`——記錄還在、看起來正常，只是再也認不出是誰。"""
    who = cp.ChatUser(-9, "12345", "someone", is_owner=False)
    assert "12345" in str(who) and "object at" not in str(who)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
def test_every_platform_section_has_a_coercer_table():
    """兩個方向：少一列 ＝ 那個平台完全不驗證也不出聲，多一列會變成 `KeyError`。"""
    assert set(bo._PLATFORM_COERCERS) == set(bo._DEFAULT_PLATFORMS)


def test_every_platform_table_covers_its_own_defaults():
    for name, (table, defaults) in bo._PLATFORM_COERCERS.items():
        assert set(table) == set(defaults), name


def test_a_platform_is_off_by_default():
    """預設全關——**預設平台除外**。一個會自己連出去的東西不該因為升級就悄悄開始跑。

    預設平台刻意是開著的，理由寫在 `_DEFAULT_PLATFORM_DISCORD` 旁邊：它的身分設定
    住在頂層，而預設關著會讓 fresh clone 的 bot 什麼都不做。這一支的價值在**另一
    個方向**——新接的平台不得預設開著，也不得預設就有一個擁有者。
    """
    others = [n for n in bo._DEFAULT_PLATFORMS if n != pr.DEFAULT_PLATFORM]
    assert others, "只剩預設平台，下面那個迴圈等於沒在檢查"
    assert bo._DEFAULT_PLATFORMS[pr.DEFAULT_PLATFORM]["enabled"] is True
    for name in others:
        section = bo._DEFAULT_PLATFORMS[name]
        assert section["enabled"] is False, name
        assert section["owner_user_ids"] == [], name


def _load(tmp_path, monkeypatch, payload, name="bot.json"):
    import json
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(bo, "BOT_CONFIG_FILE", path)
    return bo.load_bot_config()


def test_a_platform_block_of_the_wrong_type_says_so(tmp_path, monkeypatch,
                                                    capsys):
    """整段蒸發是單一鍵裡代價最大的一種，而它原本一個字都沒有。"""
    cfg = _load(tmp_path, monkeypatch, {"platforms": {"telegram": 5}})
    assert cfg["platforms"]["telegram"] == bo._DEFAULT_PLATFORM_TELEGRAM
    assert "platforms.telegram" in capsys.readouterr().err


def test_an_unknown_platform_name_says_so(tmp_path, monkeypatch, capsys):
    """打錯平台名的症狀跟打錯值一樣：設定沒生效，而且沒有其他線索。"""
    _load(tmp_path, monkeypatch, {"platforms": {"telegran": {}}})
    assert "telegran" in capsys.readouterr().err


def test_a_good_platform_block_is_silent_and_applied(tmp_path, monkeypatch,
                                                     capsys):
    """正面對照組：少了它，「一律警告並退回預設」也會讓上面兩支通過。"""
    cfg = _load(tmp_path, monkeypatch, {"platforms": {"telegram": {
        "enabled": True, "owner_user_ids": ["777"],
        "allowed_chat_ids": [-100], "poll_timeout_sec": 12.0}}})
    section = cfg["platforms"]["telegram"]
    assert section["enabled"] is True
    assert section["owner_user_ids"] == ["777"]
    assert section["allowed_chat_ids"] == ["-100"]
    assert section["poll_timeout_sec"] == 12.0
    assert capsys.readouterr().err == ""


def test_a_partly_broken_id_list_keeps_the_usable_entries(tmp_path, monkeypatch,
                                                          capsys):
    """壞一筆不該把整份清單清空——那是 fail-closed 裡最不會有人發現的一種。"""
    cfg = _load(tmp_path, monkeypatch, {"platforms": {"telegram": {
        "owner_user_ids": ["777", None, "  "]}}})
    assert cfg["platforms"]["telegram"]["owner_user_ids"] == ["777"]
    assert "owner_user_ids" in capsys.readouterr().err


def test_the_platform_section_does_not_share_state_with_the_defaults(
        tmp_path, monkeypatch):
    """回傳值與模組常數共用同一個容器時，呼叫端 append 一次就永久污染預設值。"""
    cfg = _load(tmp_path, monkeypatch, {})
    cfg["platforms"]["telegram"]["owner_user_ids"].append("intruder")
    assert bo._DEFAULT_PLATFORM_TELEGRAM["owner_user_ids"] == []


# ---------------------------------------------------------------------------
# 憑證檔
# ---------------------------------------------------------------------------
def test_a_missing_token_file_means_the_platform_is_simply_off(tmp_path):
    assert tg.read_platform_token(tmp_path / "nope.md") == ""


def test_an_empty_token_file_means_the_platform_is_simply_off(tmp_path):
    path = tmp_path / "t.md"
    path.write_text("   \n", encoding="utf-8")
    assert tg.read_platform_token(path) == ""


def test_a_token_that_contains_a_colon_is_not_truncated(tmp_path):
    """**這個平台的憑證自己就含冒號**（`<數字>:<字串>`）。

    既有那一支看到冒號就切（它的憑證沒有冒號，所以那樣寫是對的）；照抄過來會把
    憑證的前半截丟掉，而症狀是「認證失敗」——完全看不出原因。
    """
    path = tmp_path / "t.md"
    path.write_text("123456:AAbbCC-ddEE\n", encoding="utf-8")
    assert tg.read_platform_token(path) == "123456:AAbbCC-ddEE"


def test_a_key_value_token_file_still_works(tmp_path):
    path = tmp_path / "t.md"
    path.write_text("token: 123456:AAbbCC\n", encoding="utf-8")
    assert tg.read_platform_token(path) == "123456:AAbbCC"


def test_an_undecodable_token_file_does_not_print_the_bytes(tmp_path, capsys):
    """整個檔案就是一個憑證，而裸的 `UnicodeDecodeError` 會把解不開的位元組印出來。"""
    path = tmp_path / "t.md"
    path.write_bytes(b"\xff\xfe\x00abc")
    assert tg.read_platform_token(path) == ""
    err = capsys.readouterr().err
    assert "abc" not in err and "\\xff" not in err


# ---------------------------------------------------------------------------
# transport：建立與閘門
# ---------------------------------------------------------------------------
def _context(**section):
    base = {"enabled": True, "owner_user_ids": ["777"],
            "allowed_chat_ids": ["-100"], "poll_timeout_sec": 30.0}
    base.update(section)
    return cp.TransportContext(
        config={tg.PLATFORM_NAME: base}, owner_uid=_OWNER,
        command_channel_id=4242, handle_message=None)


def test_a_platform_that_is_off_is_silently_absent(capsys):
    """「沒設定」是絕大多數人的常態；每次啟動都抱怨一次就是下一個雜訊源。"""
    assert tg.build(_context(enabled=False)) is None
    assert capsys.readouterr().err == ""


def test_a_platform_that_is_on_without_a_token_says_so(monkeypatch, tmp_path,
                                                       capsys):
    """使用者以為打開了而其實沒有——症狀跟沒打開一模一樣。"""
    monkeypatch.setattr(tg, "TELEGRAM_TOKEN_FILE", tmp_path / "missing.md")
    assert tg.build(_context()) is None
    assert "token" in capsys.readouterr().err


def _built(monkeypatch, tmp_path, **section):
    path = tmp_path / "t.md"
    path.write_text("123:abc", encoding="utf-8")
    monkeypatch.setattr(tg, "TELEGRAM_TOKEN_FILE", path)
    return tg.build(_context(**section))


def test_a_platform_with_no_owner_id_says_so(monkeypatch, tmp_path, capsys):
    """沒有擁有者 id ＝ 這個平台上沒有人過得了主機控制閘。fail-closed 的失敗正是
    最不會有人發現的那一種，所以要講一聲。"""
    assert _built(monkeypatch, tmp_path, owner_user_ids=[]) is not None
    assert "owner" in capsys.readouterr().err


def _update(*, uid="777", chat="-100", text="!status", date=None,
            is_bot=False, message_id=7):
    import time
    return {"update_id": 1, "message": {
        "message_id": message_id,
        "date": time.time() + 60 if date is None else date,
        "from": {"id": uid, "first_name": "someone", "is_bot": is_bot},
        "chat": {"id": chat, "type": "private"},
        "text": text,
    }}


def _drive(transport, update):
    """把一筆更新餵進 transport，回它交給派發器的那些訊息。不碰網路。"""
    seen: list = []

    async def _handle(message):
        seen.append(message)

    transport._context.handle_message = _handle
    asyncio.run(transport._on_update(update))
    return seen


def test_a_message_from_the_owner_reaches_the_dispatcher(monkeypatch, tmp_path):
    transport = _built(monkeypatch, tmp_path)
    seen = _drive(transport, _update())
    assert len(seen) == 1
    assert seen[0].author.is_owner is True
    assert seen[0].author.id == _OWNER
    assert seen[0].channel.id == 4242, "允許清單裡的對話就是這個平台的設定頻道"


def test_a_stranger_outside_the_allowed_chats_is_ignored_silently(
        monkeypatch, tmp_path):
    """回話等於對任何陌生人確認這個帳號後面有東西在跑。"""
    transport = _built(monkeypatch, tmp_path)
    assert _drive(transport, _update(uid="999", chat="-999")) == []


def test_a_stranger_inside_an_allowed_chat_is_not_the_owner(monkeypatch,
                                                            tmp_path):
    transport = _built(monkeypatch, tmp_path)
    seen = _drive(transport, _update(uid="999"))
    assert len(seen) == 1
    assert seen[0].author.is_owner is False
    assert seen[0].author.id != _OWNER and seen[0].author.id < 0


def test_a_message_sent_before_this_process_started_is_dropped(monkeypatch,
                                                               tmp_path):
    """重啟之後突然重跑一批不知道多久以前的主機控制指令，比漏掉一則訊息危險。"""
    transport = _built(monkeypatch, tmp_path)
    transport._started_at = 10_000.0
    assert _drive(transport, _update(date=9_000.0)) == []


def test_a_message_from_the_first_second_after_start_is_kept(monkeypatch,
                                                              tmp_path):
    """平台的時刻是整秒。跟帶小數的啟動時刻比，啟動那一秒裡送的訊息會被當成舊的丟掉。"""
    transport = _built(monkeypatch, tmp_path)
    transport._started_at = 10_000.7
    assert len(_drive(transport, _update(date=10_000))) == 1
    assert _drive(transport, _update(date=9_999)) == []


@pytest.mark.parametrize("date", ["10001", None, True, float("nan")])
def test_a_message_without_a_usable_date_is_dropped(monkeypatch, tmp_path, date):
    """這道檢查擋的是主機控制指令的重播。看不出時刻時不放行。"""
    transport = _built(monkeypatch, tmp_path)
    transport._started_at = 10_000.0
    update = _update()
    update["message"]["date"] = date
    assert _drive(transport, update) == []


def test_a_long_running_command_does_not_stop_polling(monkeypatch, tmp_path):
    """一輪跑很久的指令不得讓收更新的迴圈停下來——否則 `abort` 送不進來，而這段期間
    送的指令會在它結束之後一口氣照跑。"""
    transport = _built(monkeypatch, tmp_path)
    polls: list = []

    async def _body():
        release = asyncio.Event()
        started: list = []

        async def _slow(message):
            started.append(message.content)
            await release.wait()

        transport._context.handle_message = _slow
        batches = [[_update(text="!slow")], [dict(_update(text="!abort"), update_id=2)]]

        async def _api(method, payload=None, *, data=None):
            del payload, data
            polls.append(method)
            if batches:
                return batches.pop(0)
            await asyncio.sleep(0.05)
            return []

        transport._api = _api
        runner = asyncio.create_task(transport.run())
        for _ in range(100):
            if len(started) >= 2:
                break
            await asyncio.sleep(0.01)
        release.set()
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
        return started

    started = asyncio.run(_body())
    assert started == ["!slow", "!abort"], (started, polls)


@pytest.mark.parametrize("batch", [
    # 最後兩格不是清單：字串照樣疊代得下去（每個字元都不是 dict），整數則會讓迴圈直接
    # 死在 `for` 上——擋那一格的是「不是清單就當成失敗」那一道。
    [{"message": {}}], [{"update_id": "7"}], [{"update_id": True}], "not a list", 5,
])
def test_a_batch_that_cannot_advance_the_offset_backs_off(monkeypatch, tmp_path,
                                                          batch):
    """壞資料讓 offset 不前進時，下一輪會立刻拿回同一批。不等的話就是一條不睡覺的迴圈。"""
    transport = _built(monkeypatch, tmp_path)
    transport._context.handle_message = None
    polls: list = []
    slept: list = []

    async def _api(method, payload=None, *, data=None):
        del payload, data
        polls.append(method)
        if len(polls) > 3:
            raise asyncio.CancelledError
        return batch

    async def _fake_sleep(seconds):
        slept.append(seconds)

    transport._api = _api
    monkeypatch.setattr(tg.asyncio, "sleep", _fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(transport.run())
    assert len(slept) == 3 and all(s > 0 for s in slept), slept


class _Chunks:
    """`aiohttp.StreamReader` 的讀法：`read(n)` 每次只回目前緩衝的一小段。"""

    def __init__(self, body: bytes, piece: int = 1000):
        self.body = body
        self.piece = piece

    async def read(self, n: int = -1) -> bytes:
        size = self.piece if n < 0 else min(n, self.piece)
        out, self.body = self.body[:size], self.body[size:]
        return out


class _Response:
    def __init__(self, status=200, body=b""):
        self.status = status
        self.content = _Chunks(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    closed = False

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def get(self, url):
        del url
        if self.error is not None:
            raise self.error
        return self.response


def _download_env(monkeypatch, tmp_path, session):
    transport = _built(monkeypatch, tmp_path)

    async def _file(method, payload=None, *, data=None):
        del method, payload, data
        return {"file_path": "documents/x.bin"}

    transport._api = _file
    transport._session = session
    return transport


def test_an_attachment_is_read_to_the_end_not_one_buffer(monkeypatch, tmp_path):
    """`read(n)` 只回目前緩衝的那一段。照那樣讀，寫進主機的就是一個被截短的檔案。"""
    body = bytes(range(256)) * 400
    transport = _download_env(monkeypatch, tmp_path,
                              _Session(_Response(body=body)))
    assert asyncio.run(transport._download("f")) == body


def test_an_oversized_attachment_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(tg, "DOWNLOAD_LIMIT_BYTES", 5000)
    transport = _download_env(monkeypatch, tmp_path,
                              _Session(_Response(body=b"x" * 5001)))
    with pytest.raises(OSError, match="too large"):
        asyncio.run(transport._download("f"))


@pytest.mark.parametrize("kind", ["url", "timeout"])
def test_a_failed_download_never_carries_the_token(monkeypatch, tmp_path, kind):
    """網址的路徑裡就是憑證，而好幾種 `aiohttp` 例外的 `str()`／`repr()` 都帶著網址。

    逾時那一格擋的是另一個形狀：`asyncio.TimeoutError` 是 `OSError` 的子類別，而這裡
    自己的錯誤也是 `OSError`——寫成「`OSError` 照原樣往上丟」的話，逾時就繞過了替換。"""
    import aiohttp
    if kind == "url":
        boom = aiohttp.InvalidUrlClientError(
            "https://example.invalid/file/bot123:abc/documents/x.bin")
        assert "123:abc" in str(boom), "前提：這種例外本身真的帶著憑證"
    else:
        boom = asyncio.TimeoutError()
    transport = _download_env(monkeypatch, tmp_path, _Session(error=boom))
    with pytest.raises(OSError) as caught:
        asyncio.run(transport._download("f"))
    assert "123:abc" not in str(caught.value) + repr(caught.value)
    assert str(caught.value) == "attachment download failed"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_non_200_download_is_refused_with_its_status(monkeypatch, tmp_path):
    transport = _download_env(monkeypatch, tmp_path,
                              _Session(_Response(status=404, body=b"nope")))
    with pytest.raises(OSError, match="404"):
        asyncio.run(transport._download("f"))


@pytest.mark.parametrize("field, value", [
    ("forward_origin", {"type": "user"}), ("forward_from", {"id": 5}),
    ("forward_date", 1700000000), ("is_automatic_forward", True),
])
def test_a_forwarded_command_is_not_run(monkeypatch, tmp_path, field, value):
    """寄件者是轉發的人，內容卻是別人寫的——擁有者轉發一則別人的 `!…` 不得以擁有者身分執行。"""
    transport = _built(monkeypatch, tmp_path)
    update = _update(text="!host kill 1")
    update["message"][field] = value
    assert _drive(transport, update) == []


def test_a_forwarded_plain_text_still_arrives(monkeypatch, tmp_path):
    """當成內容拿去問是可以的，擋的只有指令。"""
    transport = _built(monkeypatch, tmp_path)
    update = _update(text="這段是什麼意思")
    update["message"]["forward_origin"] = {"type": "user"}
    assert len(_drive(transport, update)) == 1


def _dispatch(monkeypatch, *, text, is_owner, is_direct, is_command_chat=True):
    import types as _types
    import discord_bot as b
    routed: list = []

    async def _on_message(message):
        routed.append(("bang", message.content))

    async def _mention(message):
        routed.append(("ask", message.content))

    monkeypatch.setattr(b, "on_message", _on_message)
    monkeypatch.setattr(b, "_handle_mention", _mention)
    message = _types.SimpleNamespace(
        content=text,
        author=_types.SimpleNamespace(is_owner=is_owner),
        channel=_types.SimpleNamespace(is_direct=is_direct,
                                       is_command_chat=is_command_chat))
    asyncio.run(b.dispatch_external_message(message))
    return routed


def test_a_strangers_plain_line_in_a_group_is_not_a_question(monkeypatch):
    """既有平台要 @ 到 bot 才算提問。這裡沒有那個訊號，照單全收的話群組裡每個人的
    每一句話都會換來一句拒絕。"""
    assert _dispatch(monkeypatch, text="大家好", is_owner=False,
                     is_direct=False) == []


@pytest.mark.parametrize("is_owner, is_direct", [(True, False), (False, True)])
def test_a_plain_line_from_the_owner_or_in_a_direct_chat_is_a_question(
        monkeypatch, is_owner, is_direct):
    assert _dispatch(monkeypatch, text="問一下", is_owner=is_owner,
                     is_direct=is_direct) == [("ask", "問一下")]


def test_a_strangers_command_in_the_group_still_reaches_the_gates(monkeypatch):
    """`!` 指令照舊交給既有的派發與閘門，拒不拒絕由那裡決定。"""
    assert _dispatch(monkeypatch, text="!status", is_owner=False,
                     is_direct=False) == [("bang", "!status")]


def test_another_bot_is_ignored(monkeypatch, tmp_path):
    transport = _built(monkeypatch, tmp_path)
    assert _drive(transport, _update(is_bot=True)) == []


def test_an_empty_message_is_ignored(monkeypatch, tmp_path):
    transport = _built(monkeypatch, tmp_path)
    assert _drive(transport, _update(text="   ")) == []


def test_two_conversations_do_not_share_a_message_id(monkeypatch, tmp_path):
    """平台的訊息 id 只在同一個對話內唯一，而這個號碼會被當成 Dorossi 佇列的錨點。"""
    transport = _built(monkeypatch, tmp_path)
    first = _drive(transport, _update(chat="-100"))[0]
    second = _drive(transport, _update(chat="-100", message_id=8))[0]
    assert first.id != second.id


# ---------------------------------------------------------------------------
# transport：送出
# ---------------------------------------------------------------------------
class _RecordingApi:
    """把 HTTP 換掉。**測試不碰真的網路、也不碰真的憑證。**"""

    def __init__(self):
        self.calls: list = []
        self.next_id = 100

    async def __call__(self, method, payload=None, *, data=None):
        self.calls.append((method, payload, data))
        self.next_id += 1
        return {"message_id": self.next_id}


def _wired(monkeypatch, tmp_path):
    transport = _built(monkeypatch, tmp_path)
    api = _RecordingApi()
    transport._api = api
    channel = cp.ChatConversation(transport, "-100", uid=4242, is_direct=True,
                                  is_command_chat=True)
    return transport, api, channel


def test_a_long_reply_is_chunked_and_the_last_chunk_is_returned(monkeypatch,
                                                                tmp_path):
    """即時預覽編輯的是**最新**的那一則；回第一塊會讓答案長在一段已經被蓋過去的
    文字上面。"""
    transport, api, channel = _wired(monkeypatch, tmp_path)
    body = "x" * (tg.TEXT_CHUNK_LIMIT * 2 + 10)
    sent = asyncio.run(transport.deliver(channel, body))
    sends = [c for c in api.calls if c[0] == "sendMessage"]
    assert len(sends) == 3
    assert all(len(c[1]["text"]) <= tg.TEXT_CHUNK_LIMIT for c in sends)
    assert sent.platform_message_id == str(api.next_id)


def test_nothing_sent_is_ever_longer_than_the_platform_allows(monkeypatch,
                                                              tmp_path):
    transport, api, channel = _wired(monkeypatch, tmp_path)
    asyncio.run(transport.deliver(channel, "y" * 20_000))
    for method, payload, _data in api.calls:
        if method == "sendMessage":
            assert len(payload["text"]) <= tg.TEXT_HARD_LIMIT


def test_markdown_is_deliberately_not_enabled(monkeypatch, tmp_path):
    """一段沒跳脫好的符號在這個平台不是顯示得醜，是整則訊息被拒——答案直接消失。"""
    transport, api, channel = _wired(monkeypatch, tmp_path)
    asyncio.run(transport.deliver(channel, "**粗體** _底線_ `碼`"))
    assert all("parse_mode" not in (c[1] or {}) for c in api.calls)


def test_an_image_goes_out_as_an_image(monkeypatch, tmp_path):
    transport, api, channel = _wired(monkeypatch, tmp_path)
    asyncio.run(transport.deliver(channel, "看圖",
                                  file=_FakeFile("a.png", b"123")))
    assert [c[0] for c in api.calls] == ["sendMessage", "sendPhoto"]


def test_a_non_image_goes_out_as_a_document(monkeypatch, tmp_path):
    transport, api, channel = _wired(monkeypatch, tmp_path)
    asyncio.run(transport.deliver(channel, None,
                                  file=_FakeFile("a.txt", b"123")))
    assert [c[0] for c in api.calls] == ["sendDocument"]


def test_an_oversized_attachment_degrades_to_a_generic_notice(monkeypatch,
                                                              tmp_path):
    """「有文字沒附件」遠好過「什麼都沒有」，而那句話不得提檔名或路徑。"""
    transport, api, channel = _wired(monkeypatch, tmp_path)
    big = _FakeFile("huge.bin", b"z" * 8)
    monkeypatch.setattr(tg, "UPLOAD_LIMIT_BYTES", 4)
    asyncio.run(transport.deliver(channel, None, file=big))
    texts = [c[1]["text"] for c in api.calls if c[0] == "sendMessage"]
    assert texts and "huge.bin" not in texts[0]


def test_editing_goes_to_the_edit_endpoint(monkeypatch, tmp_path):
    transport, api, channel = _wired(monkeypatch, tmp_path)
    sent = asyncio.run(transport.deliver(channel, "佔位"))
    asyncio.run(sent.edit("答案"))
    assert [c[0] for c in api.calls] == ["sendMessage", "editMessageText"]
    assert api.calls[-1][1]["text"] == "答案"


class _RateLimited:
    """回 429 前幾次、之後成功。**不碰網路**：換掉的是單次 HTTP 往返那一層。"""

    def __init__(self, times, retry_after=2):
        self.left = times
        self.retry_after = retry_after
        self.calls = 0

    async def __call__(self, method, payload, data):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            return {"ok": False, "error_code": 429,
                    "parameters": {"retry_after": self.retry_after}}
        return {"ok": True, "result": {"message_id": 5}}


def _no_sleep(monkeypatch):
    """量的是「等多久」，不是真的等。回一個收集到的秒數清單。"""
    slept: list = []

    async def _fake(seconds):
        slept.append(seconds)

    monkeypatch.setattr(tg.asyncio, "sleep", _fake)
    return slept


def test_a_rate_limited_send_is_retried_once(monkeypatch, tmp_path):
    """長回覆會被切成好幾則連著送，而平台對單一對話大約每秒一則。不重試的話後面
    那幾塊**安靜地消失**，使用者拿到一個被截斷的答案而且沒有任何地方會講。"""
    transport = _built(monkeypatch, tmp_path)
    limited = _RateLimited(times=1)
    transport._request = limited
    slept = _no_sleep(monkeypatch)
    result = asyncio.run(transport._api("sendMessage", {}))
    assert result == {"message_id": 5}
    assert limited.calls == 2
    assert slept == [2.0]


def test_the_rate_limit_wait_is_capped(monkeypatch, tmp_path):
    """等待秒數是**對方**給的。照單全收等於讓對方決定這個協程卡多久，而握著它的
    可能是一輪正在回答的問答。"""
    transport = _built(monkeypatch, tmp_path)
    transport._request = _RateLimited(times=1, retry_after=10_000)
    slept = _no_sleep(monkeypatch)
    asyncio.run(transport._api("sendMessage", {}))
    assert slept == [tg._RATE_LIMIT_WAIT_MAX_SEC]


def test_a_nan_rate_limit_wait_does_not_crash_the_transport(monkeypatch, tmp_path):
    """JSON 解析器收得下 `NaN`，`max`／`min` 夾不住它，`asyncio.sleep(nan)` 會丟例外。"""
    transport = _built(monkeypatch, tmp_path)
    transport._request = _RateLimited(times=1, retry_after=float("nan"))
    slept = _no_sleep(monkeypatch)
    assert asyncio.run(transport._api("sendMessage", {})) == {"message_id": 5}
    assert slept == [1.0]


def test_two_rate_limits_in_a_row_give_up(monkeypatch, tmp_path):
    """再重試只是把限流拉長。放棄，並留一行 stderr。

    放棄回的是 `UNREACHABLE`（「現在送不了」），不是 `None`（「平台說不」）：送出那一側
    靠這個區別決定要不要把答案停起來等之後重送（2026-09-24）。它仍是假值，只問「有沒有
    結果」的呼叫端照舊當成失敗。"""
    transport = _built(monkeypatch, tmp_path)
    limited = _RateLimited(times=5)
    transport._request = limited
    _no_sleep(monkeypatch)
    result = asyncio.run(transport._api("sendMessage", {}))
    assert result is tg.UNREACHABLE and not result
    assert limited.calls == 2


def test_an_unreachable_platform_is_not_reported_as_a_rejection(monkeypatch, tmp_path):
    """連不上（`_request` 回 None）回 `UNREACHABLE`；平台說不（400）回 `None`。送出那一側
    只在前者丟 `DeliveryFailed`——後者等多久再送都一樣是 400。"""
    transport = _built(monkeypatch, tmp_path)

    async def _down(method, payload, data):
        return None

    transport._request = _down
    _no_sleep(monkeypatch)
    assert asyncio.run(transport._api("sendMessage", {})) is tg.UNREACHABLE


@pytest.mark.parametrize("answer, raises", [
    (tg.UNREACHABLE, True), (None, False), ({"message_id": 9}, False)],
    ids=["unreachable", "rejected", "sent"])
def test_sending_raises_only_when_the_platform_cannot_be_reached(
        monkeypatch, tmp_path, answer, raises):
    """文字、附件、編輯三條路一樣：連不上就丟 `DeliveryFailed`（呼叫端把答案停起來等重送），
    被拒絕就照舊回 None／略過。在這之前三條都把連不上吞成 None，答案安靜地消失。"""
    transport, api, channel = _wired(monkeypatch, tmp_path)

    async def _answer(method, payload=None, *, data=None):
        api.calls.append((method, payload, data))
        return answer

    transport._api = _answer
    sent = cp.SentChatMessage(channel, "5", "舊的")
    actions = [
        lambda: transport.deliver(channel, "答案"),
        lambda: transport.deliver(channel, None, file=_FakeFile("a.txt", b"1")),
        lambda: sent.edit("新的"),
    ]
    for action in actions:
        if raises:
            with pytest.raises(cp.DeliveryFailed):
                asyncio.run(action())
        else:
            asyncio.run(action())
    assert isinstance(cp.DeliveryFailed("x"), ConnectionError)
    assert (sent._content == "舊的") is raises, "沒送到的編輯被記成已顯示"


@pytest.mark.parametrize("polls, recovered", [
    ([tg.UNREACHABLE, [], []], 1),
    ([tg.UNREACHABLE, tg.UNREACHABLE, [], tg.UNREACHABLE, []], 2),
    ([[], [], []], 0),
    ([tg.UNREACHABLE, tg.UNREACHABLE], 0),
], ids=["one-outage", "two-outages", "never-down", "still-down"])
def test_the_recovery_hook_fires_once_per_outage(monkeypatch, tmp_path, polls, recovered):
    """收訊從連不上變回連得上時叫一次 `on_recovered`（送出停著的答案）。一直連得上時不叫
    ——每一輪都叫的話，停著的答案清單每幾十秒就被整份讀一次。"""
    transport = _built(monkeypatch, tmp_path)
    fired: list = []

    async def _recovered():
        fired.append(1)

    script = list(polls)

    async def _api(method, payload=None, *, data=None):
        if not script:
            raise asyncio.CancelledError
        return script.pop(0)

    async def _fake_sleep(_seconds):
        return None

    transport._context.on_recovered = _recovered
    transport._api = _api
    monkeypatch.setattr(tg.asyncio, "sleep", _fake_sleep)

    async def _go():
        with pytest.raises(asyncio.CancelledError):
            await transport.run()
        await asyncio.gather(*transport._inflight, return_exceptions=True)

    asyncio.run(_go())
    assert len(fired) == recovered


def test_an_ordinary_rejection_is_not_retried(monkeypatch, tmp_path, capsys):
    """400 重試一百次也是 400。只有限流才等。"""
    transport = _built(monkeypatch, tmp_path)
    calls: list = []

    async def _bad(method, payload, data):
        calls.append(method)
        return {"ok": False, "error_code": 400}

    transport._request = _bad
    _no_sleep(monkeypatch)
    assert asyncio.run(transport._api("sendMessage", {})) is None
    assert calls == ["sendMessage"]
    assert "400" in capsys.readouterr().err


def test_the_live_preview_survives_this_platform(monkeypatch, tmp_path):
    """Dorossi 的即時預覽整回合都在編輯同一則訊息；這一格就是它成立的前提。"""
    transport, _api, channel = _wired(monkeypatch, tmp_path)
    assert transport.capabilities.edit_message is True
    sent = asyncio.run(transport.deliver(channel, "佔位"))
    assert isinstance(sent, cp.SentChatMessage)


# ---------------------------------------------------------------------------
# Secrecy：送出去與印出去的字
# ---------------------------------------------------------------------------
_NEW_MODULES = ("_chat_platform.py", "_telegram_transport.py")


def _string_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


@pytest.mark.parametrize("name", _NEW_MODULES)
def test_no_host_path_literal_in_the_new_modules(name):
    """Layer 1：主機路徑不得出現在任何一段可能被送出去的字面值裡。"""
    banned = ("todo_", "output/", ".chrome_profile", "bot_config.json",
              ".venv", "C:\\", "D:\\")
    hits = [text for text in _string_literals(PKG_ROOT / name)
            if any(one in text for one in banned)]
    assert not hits, f"{name} 有疑似主機路徑的字面值：{hits}"


def test_the_transport_never_interpolates_a_raw_error_into_a_sent_string():
    """原始例外文字可以夾帶任何東西出去——含網址，而網址裡有憑證。

    判準窄：只看**送出點**（`deliver` / `_api` 的 payload 與回覆字串）。診斷走
    stderr 是對的，那一側由下面那支看著。
    """
    tree = ast.parse((PKG_ROOT / "_telegram_transport.py")
                     .read_text(encoding="utf-8"))
    problems = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "deliver"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.JoinedStr):
                problems.append(node.lineno)
    assert not problems, (
        f"第 {problems} 行把一段組出來的字串直接送出去了。送出去的句子必須是"
        "寫死的泛用句；要講細節請寫 stderr。")


def test_nothing_printed_can_carry_the_token():
    """憑證在網址裡，所以 `print(f"... {error}")` 會把整個網址、也就是憑證寫進
    記錄檔——而記錄檔有 `/log tail` 這個使用者面的出口。

    判準：`print` 的引數裡不得出現 `self._token` / `_url(` 這兩個形狀，也不得把
    `except` 綁到的變數整個內插進去（`type(error).__name__` 可以，那只有類別名）。
    """
    tree = ast.parse((PKG_ROOT / "_telegram_transport.py")
                     .read_text(encoding="utf-8"))
    problems = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "print"):
            continue
        rendered = ast.unparse(node)
        if "_token" in rendered or "_url(" in rendered:
            problems.append((node.lineno, "token"))
        for sub in ast.walk(node):
            if isinstance(sub, ast.FormattedValue) and \
                    ast.unparse(sub.value).strip() in ("error", "exc", "err"):
                problems.append((node.lineno, "raw error"))
    assert not problems, (
        f"這些 print 可能把憑證或原始例外文字寫進記錄檔：{problems}")


def test_the_detector_would_actually_catch_a_leak():
    """合成對照：樹是乾淨的時候上面那支恆綠，刪掉偵測邏輯也不會有人發現。"""
    tree = ast.parse('print(f"failed: {self._url(m)}")\n'
                     'print(f"failed: {error}")\n')
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
            rendered = ast.unparse(node)
            if "_token" in rendered or "_url(" in rendered:
                found.append("token")
            for sub in ast.walk(node):
                if isinstance(sub, ast.FormattedValue) and \
                        ast.unparse(sub.value).strip() in ("error", "exc", "err"):
                    found.append("raw error")
    assert sorted(found) == ["raw error", "token"]


@pytest.mark.parametrize("name", _NEW_MODULES)
def test_nothing_printed_is_unencodable_on_this_console(name):
    """`print` 到管線時走的是主機的 OEM 編碼，編不出來的字元會讓**行程死掉**。"""
    tree = ast.parse((PKG_ROOT / name).read_text(encoding="utf-8"), name)
    bad = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "print"):
            continue
        for sub, text in ((s, s.value) for s in ast.walk(node)
                          if isinstance(s, ast.Constant)
                          and isinstance(s.value, str)):
            try:
                text.encode("cp950")
            except UnicodeEncodeError:
                bad.append((sub.lineno, text))
    assert not bad, f"{name} 這些 print 在本機主控台編不出來：{bad}"


# ---------------------------------------------------------------------------
# bot 側的接線
# ---------------------------------------------------------------------------
def _bot_function(name: str):
    source = (PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source, "discord_bot.py")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"discord_bot.py 裡找不到 `{name}`")


def test_the_transports_are_revived_with_the_other_background_loops():
    """復原掛在**重新連線**上。沒接進去的話，一次網路抖動之後那個平台就靜悄悄地
    停了，而唯一的症狀是「沒有回應」。"""
    body = ast.unparse(_bot_function("_ensure_background_tasks_alive"))
    assert "_ensure_chat_transports_alive" in body
    assert "_build_chat_transports" in body


def test_a_transport_task_is_supervised_and_named():
    """安靜死掉的長命迴圈只會留下 asyncio 那句沒有名字的抱怨，而且要等 GC 才出現。

    判準看的是**呼叫節點**而不是原始碼文字：這個函式的註解本身就在解釋為什麼不能
    用裸的 `create_task`，比對文字會被自己的註解絆倒（本 repo 已經為
    `stream_child` 的 docstring 付過一次同樣的學費）。
    """
    fn = _bot_function("_ensure_chat_transports_alive")
    called = {ast.unparse(n.func) for n in ast.walk(fn)
              if isinstance(n, ast.Call)}
    assert "_start_supervised_task" in called
    bare = {n for n in called
            if n.endswith("create_task") or n.endswith("ensure_future")}
    assert not bare, f"用了裸的任務建立方式：{sorted(bare)}"
    body = ast.unparse(fn)
    assert "f'chat-{name}'" in body or 'f"chat-{name}"' in body, (
        "任務沒有帶平台名字——死掉時那一行紀錄就認不出是哪個平台停了")


def test_one_transport_failing_does_not_take_the_others_down():
    """`on_ready` 呼叫這條路時外面沒有 try：一次 `create_task` 拋出就會把**後面
    那幾個平台**一起帶走，而函式庫只會記一句 'Ignoring exception in on_ready'。"""
    fn = _bot_function("_ensure_chat_transports_alive")
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    assert loops, "迴圈不見了"
    assert any(isinstance(n, ast.Try) for n in ast.walk(loops[0])), (
        "每個 transport 要各自 try——一個起不來不得拖垮其他平台")


def test_the_external_entry_point_reuses_both_existing_dispatchers():
    """**刻意不抄一份派發鏈。** 抄一份的代價不是行數，是那四道閘會分叉，而且沒有
    任何症狀——本 repo 對 `_OWNER_ONLY_SLASH` 與 `_pid_alive` 都記過同一個形狀。"""
    body = ast.unparse(_bot_function("dispatch_external_message"))
    assert "on_message" in body and "_handle_mention" in body


def test_the_external_entry_point_has_its_own_conversation_gate():
    """`_handle_mention` 這條路跑在 `on_message` 的頻道閘**之前**，所以少了這道
    檢查，任何一個 transport 忘了擋就等於把自由提問入口對全平台打開——與
    `@bot restart` 當年那個缺口同一個形狀。"""
    body = ast.unparse(_bot_function("dispatch_external_message"))
    assert "is_command_chat" in body and "is_owner" in body
