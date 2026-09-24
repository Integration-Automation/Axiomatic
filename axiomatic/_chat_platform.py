"""對話平台介接層（adapter seam）：讓同一批 handler 在別的平台上也跑得起來。

bot 的 handler 從來沒有真的綁死在某一個函式庫的訊息型別上——它們吃的是**鴨子型別**，
而且那個型別小到可以整個寫下來。`_InteractionMessageProxy` 的 docstring 量過：全模組
只用到 `reply`、`author`、`channel`（`.send` / `.id` / `.typing()`）、`mentions`、
`guild`、`content`、`id`、`attachments`，其餘一個都沒有。`_DorossiRestoredMessage` 是
第二個代理，形狀一樣。**那個形狀就是這個模組要一般化的接縫。**

本模組只有標準函式庫，而且**刻意不 import `discord`**（也不 import `discord_bot`——
那會是循環）。對外送的附件與嵌入訊息一律用鴨子型別讀（`.fp` / `.filename` /
`.title` / `.description` / `.fields`），所以既有 handler 交出來的
`discord.File` / `discord.Embed` 不必改寫就餵得進來。

## 介面的五件事

1. **身分**（`ChatUser`）──`id` 是**bot 內部的整數身分**，不是平台上的那一個。
   對照見 `resolve_identity()`：擁有者映成 `OWNER_USER_ID`，其他人一律映成一個
   **負數**。整個 repo 的閘門都寫成 `author.id != OWNER_USER_ID`，所以把對照放在
   這一個決策點，既有的每一道閘不改一個字就對每個平台成立；負數則保證撞不到任何
   一個設定得出來的 Discord id（`_coerce_int_list` 只收 `>= 0`）。
2. **對話**（`ChatConversation`）──`.id` / `.send()` / `.typing()`。允許清單裡的
   對話拿的 `.id` 就是 `CHANNEL_ID`：那個對話**就是**這個平台的「設定頻道」，
   `!` 的頻道閘與 help 的 `include_channel_only` 因此自動成立。
3. **收到的訊息**（`ChatMessage`）──`content` / `attachments` / `reply()`。
4. **送出去的訊息**（`SentChatMessage`）──`.edit()`。Dorossi 的即時預覽整回合都在
   編輯同一則訊息，所以這一格是必要的，不是裝飾。
5. **做不到的事**（`PlatformCapabilities` ＋ `UnsupportedOperation`）──能力用旗標
   問得到，呼叫端**明確降級**；做不到的事丟 `UnsupportedOperation`，不是當掉，也
   不是安靜地什麼都沒發生。`ReplaceOnEditMessage` 是「不能編輯」那一種平台
   （例如只能推播的平台）的降級實作。

## 為什麼不是「再寫一套指令樹」

擁有者已裁定：其他平台重用既有的隱藏文字派發器（`!` 指令與 `@bot <文字>`），
斜線那棵樹維持原樣。所以這個模組**不認識任何一個指令**——它只負責把一則平台訊息
變成一個 handler 認得的物件，再交給 `discord_bot.dispatch_external_message`。
"""
from __future__ import annotations

import abc
import asyncio
import hashlib
import importlib
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

# 每新增一個平台就多一列。**這份清單是註冊的唯一入口**：`build_transports()` 只
# import 這裡列到的模組，沒列到的檔案放在套件裡也不會被載入。反過來也對帳——
# `test_platform_transports` 會比對磁碟上的 `_*_transport.py` 與這份清單，兩個方
# 向都釘：多一個檔案沒列進來會**安靜地整個平台不存在**，而那跟「沒設定所以不啟用」
# 長得一模一樣。
TRANSPORT_MODULES: tuple[str, ...] = ("_telegram_transport",)


class UnsupportedOperation(RuntimeError):
    """這個平台做不到這件事。

    **這是介面的一部分，不是意外。** 平台之間的能力差很多（有的不能編輯已送出的
    訊息、有的不能傳檔案），而「安靜地什麼都沒發生」是最貴的失敗形態：使用者以為
    送出去了。所以做不到的事要嘛由呼叫端先問 `capabilities` 再決定怎麼做，要嘛
    在這裡丟出來被看見。
    """


class DeliveryFailed(ConnectionError):
    """平台**連不上**（或一直限流），這一則沒送出去。跟「平台說了不」（4xx）分開：
    前者等連線回來再送就會好，後者再送也一樣。

    是 `ConnectionError` 的子類別，所以呼叫端把它當「對話平台斷線」處理——Dorossi 的
    答案會停進 outbox，等平台回來再送（2026-09-24）。在這之前 transport 把失敗吞成
    `None`，答案安靜地消失。"""


@dataclass(frozen=True)
class PlatformCapabilities:
    """一個平台做得到什麼。呼叫端拿這個**明確降級**，不要靠 try/except 試出來。

    `text_limit` 是單則訊息的字元上限（送出前由 `chunk_text()` 切好）；
    `file_bytes_limit` 是單一附件的位元組上限，0 代表這個平台不收附件。
    """

    edit_message: bool = False
    send_files: bool = False
    send_images: bool = False
    typing_indicator: bool = False
    reply_reference: bool = False
    read_attachments: bool = False
    text_limit: int = 2000
    file_bytes_limit: int = 0


# ---------------------------------------------------------------------------
# 身分對照（fail-closed）
# ---------------------------------------------------------------------------
# `OWNER_USER_ID` 是**某一個平台上的** id，而全 repo 的閘門都拿它直接比對
# （`message.author.id != OWNER_USER_ID`，三個表面各一份，`test_bot_helpers` 兩個
# 方向對帳）。所以其他平台的身分要嘛在每一道閘上多開一條分支，要嘛在**進來的那一
# 刻**就映成同一個號碼系統。選後者：閘門一個字都不用改，而「誰是擁有者」只有這裡
# 一個決策點——`CLAUDE.md` 對 `_owner_detail()` 寫的就是同一條理由。
#
# 非擁有者映成**負數**，三個理由：
#   * 永遠不等於 `OWNER_USER_ID`（那是正的），所以 fail-closed 是結構性的，不是
#     靠記得寫對比較式；
#   * `_coerce_int_list` 只收 `>= 0`，所以負數不可能出現在 `user_roles` 或
#     `path_reveal_channel_ids` 裡——別的平台的使用者拿不到角色，也拿不到「可露
#     路徑的表面」；
#   * 同一個人每次進來拿到同一個號碼（雜湊自平台名＋平台 id），所以稽核記錄與
#     指令計數仍然分得出人。
_UID_DIGEST_BYTES = 6
# 取不到發話者身分時用這一個。**不是 0**：`alert_user_id` 的預設就是 0，讓兩件
# 不同的事共用一個號碼遲早會有人把它們接在一起。
UNKNOWN_SENDER_UID = -1


def external_uid(platform: str, platform_id: str) -> int:
    """平台上的 id → 這個 bot 內部的負數身分。同樣的輸入永遠得到同樣的號碼。"""
    raw = f"{platform}:{platform_id}".encode("utf-8")
    digest = hashlib.blake2b(raw, digest_size=_UID_DIGEST_BYTES).digest()
    return -(2 + int.from_bytes(digest, "big"))


def resolve_identity(platform: str, platform_id: Any, owner_ids: Iterable[str],
                     owner_uid: int) -> tuple[int, bool]:
    """回 `(內部 uid, 是不是擁有者)`。

    比對用**字串**：平台的使用者 id 不見得是整數（有的平台是英數字串），而把它
    轉成 int 再比會讓一個合法的 id 在轉換失敗時安靜地變成「不是擁有者」——方向
    雖然安全，但原因會消失。取不到 id、id 是空的、或不在設定的擁有者清單裡，
    一律不是擁有者（fail-closed，與既有三道閘同一個立場）。
    """
    if platform_id is None:
        return UNKNOWN_SENDER_UID, False
    text = str(platform_id).strip()
    if not text:
        return UNKNOWN_SENDER_UID, False
    if text in {str(one).strip() for one in owner_ids if str(one).strip()}:
        return owner_uid, True
    return external_uid(platform, text), False


def conversation_uid(platform: str, chat_id: Any, *, command_channel_id: int,
                     allowed_chat_ids: Iterable[str]) -> tuple[int, bool]:
    """回 `(內部頻道 id, 這個對話是不是「設定頻道」)`。

    在允許清單裡的對話拿的就是 `CHANNEL_ID`——它**就是**這個平台的設定頻道，所以
    `!` 的頻道閘、help 的 `include_channel_only` 全部自動成立，不必在每一處多寫
    一條「或者這是 Telegram」。其餘對話拿一個負數（理由同 `external_uid`：撞不到
    任何一個設定得出來的頻道 id，也永遠不會落進 `path_reveal_channel_ids`）。
    """
    text = "" if chat_id is None else str(chat_id).strip()
    allowed = {str(one).strip() for one in allowed_chat_ids if str(one).strip()}
    if text and text in allowed:
        return command_channel_id, True
    return external_uid(f"{platform}#chat", text), False


# ---------------------------------------------------------------------------
# 送出去的東西：文字切塊、附件正規化、嵌入訊息攤平
# ---------------------------------------------------------------------------
_CODE_FENCE = "```"


def _unclosed_fence(text: str) -> str:
    """`text` 結尾停在程式碼區塊裡時，回一個可以重開同一個區塊的分隔符號。"""
    if text.count(_CODE_FENCE) % 2 == 0:
        return ""
    tail = text[text.rfind(_CODE_FENCE) + len(_CODE_FENCE):]
    lang = tail.split("\n", 1)[0].strip() if "\n" in tail else ""
    ok = lang and len(lang) <= 20 and all(c.isalnum() or c in "+#.-" for c in lang)
    return _CODE_FENCE + (lang if ok else "")


def chunk_text(text: str, limit: int) -> list[str]:
    """把一段長回覆切成平台吃得下的幾則，優先切在換行。

    跨切點的程式碼區塊會在前一則收尾、下一則重開（語言標籤一起帶過去）：每一則
    訊息各自算一次 markdown，不補的話下一則會把程式碼當散文顯示，而它的收尾分隔
    符號又會開一個新的區塊，把後面整段吞進去。與 `discord_bot._chunk_for_discord`
    同一條規則；**刻意各有一份**，因為上限與 markdown 方言是逐平台的，而共用一份
    會讓其中一邊遷就另一邊。
    """
    text = (text or "").strip()
    if not text:
        return []
    limit = max(1, int(limit))
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        head, rest = rest[:cut], rest[cut:].lstrip("\n")
        reopen = _unclosed_fence(head)
        # **重開分隔符號會把字加回 `rest`，所以它必須比切掉的那一段短。** 上限大到
        # 正常值（3900）時這永遠成立；但上限小到跟分隔符號差不多時，每一圈切掉四個
        # 字元又加回四個，迴圈就**永遠不會結束**。這不是理論上的角落：切塊函式是純
        # 字串函式，下一個平台把上限設多少由它自己決定。切不出進度就放棄排版——
        # 訊息送得出去永遠比程式碼區塊完不完整重要。
        if reopen and len(reopen) + 1 < cut:
            head = f"{head}\n{_CODE_FENCE}"
            rest = f"{reopen}\n{rest}"
        chunks.append(head)
    if rest:
        chunks.append(rest)
    return chunks


@dataclass
class OutboundFile:
    """要送出去的一個附件。`data` 已經在記憶體裡，因為每個平台的上傳方式都不同。"""

    filename: str
    data: bytes
    is_image: bool = False


_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _read_outbound_file(obj: Any) -> OutboundFile | None:
    """鴨子型別讀一個附件物件（handler 交出來的是函式庫的 `File`）。

    讀不出來就回 `None`，讓呼叫端走「有文字沒附件」那條路——`safe_reply` 對同一
    個情況早就是這個立場：「有文字沒附件」遠好過「什麼都沒有」。
    """
    if obj is None:
        return None
    name = str(getattr(obj, "filename", "") or "").strip()
    fp = getattr(obj, "fp", None)
    data: bytes | None = None
    if fp is not None and hasattr(fp, "read"):
        try:
            if hasattr(fp, "seek"):
                fp.seek(0)
            raw = fp.read()
            data = bytes(raw) if raw is not None else None
        except Exception:  # pylint: disable=broad-except
            data = None
    if data is None:
        return None
    if not name:
        name = "upload.bin"
    lowered = name.lower()
    return OutboundFile(name, data,
                        is_image=lowered.endswith(_IMAGE_SUFFIXES))


def flatten_embed(embed: Any) -> str:
    """把一個嵌入訊息攤平成純文字。

    只有這個平台**有**嵌入訊息的時候才輪得到原生的那一份；其餘平台看到的必須是
    同樣的內容，不能因為沒有嵌入訊息就整塊消失——那正是「安靜地什麼都沒發生」。
    一樣走鴨子型別（`.title` / `.description` / `.fields` / `.footer`）。
    """
    if embed is None:
        return ""
    lines: list[str] = []
    title = getattr(embed, "title", None)
    if title:
        lines.append(str(title))
    description = getattr(embed, "description", None)
    if description:
        lines.append(str(description))
    for one in list(getattr(embed, "fields", None) or []):
        name = str(getattr(one, "name", "") or "").strip()
        value = str(getattr(one, "value", "") or "").strip()
        if name and value:
            lines.append(f"{name}: {value}")
        elif name or value:
            lines.append(name or value)
    footer = getattr(embed, "footer", None)
    footer_text = getattr(footer, "text", None) if footer is not None else None
    if footer_text:
        lines.append(str(footer_text))
    return "\n".join(lines)


def outbound_parts(content: Any = None, *, embed: Any = None, file: Any = None,
                   files: Any = None) -> tuple[str, list[OutboundFile]]:
    """把 handler 那一套送出引數正規化成 `(文字, 附件清單)`。

    handler 呼叫 `reply(content, embed=..., file=..., files=...)` 的寫法散在全模組
    兩百多處，逐一改成平台中立是不可能的；所以正規化放在這一個地方。
    """
    text = "" if content is None else str(content)
    extra = flatten_embed(embed)
    if extra:
        text = f"{text}\n{extra}".strip() if text else extra
    out: list[OutboundFile] = []
    for one in ([file] if file is not None else []) + list(files or []):
        parsed = _read_outbound_file(one)
        if parsed is not None:
            out.append(parsed)
    return text, out


# ---------------------------------------------------------------------------
# handler 看得到的四個物件
# ---------------------------------------------------------------------------
class ChatAttachment:
    """收到的一個附件。handler 只用 `filename` 與 `await read()`（量過，就這兩個）。"""

    __slots__ = ("filename", "size", "_fetch")

    def __init__(self, filename: str, size: int,
                 fetch: Callable[[], Awaitable[bytes]]) -> None:
        self.filename = filename
        self.size = size
        self._fetch = fetch

    async def read(self) -> bytes:
        return await self._fetch()


class ChatUser:
    """發話者。`id` 是**內部 uid**（見 `resolve_identity`），不是平台上的那一個。"""

    __slots__ = ("id", "platform_id", "name", "display_name", "mention",
                 "is_owner")

    def __init__(self, uid: int, platform_id: str, display_name: str, *,
                 is_owner: bool) -> None:
        self.id = uid
        self.platform_id = platform_id
        self.name = display_name or str(platform_id)
        self.display_name = self.name
        # 別的平台沒有「@ 一個人」的通用寫法，所以這裡只給顯示名稱。唯一的呼叫端
        # 是警示訊息的稱呼，拿不到真正的 mention 也不影響它能不能送出去。
        self.mention = self.name
        self.is_owner = is_owner

    def __str__(self) -> str:
        # 稽核記錄寫的是 `str(message.author)`。沒有這一支的話那一欄會變成
        # `<object at 0x…>`——記錄還在、看起來正常，只是再也認不出是誰。
        return f"{self.name}@{self.platform_id}"


class _TypingScope:
    """`async with channel.typing():`。平台不支援就是一個什麼都不做的殼。

    不支援時**不丟例外**：typing 是純粹的體感，為了它讓一個指令整個失敗是壞交易。
    要知道支不支援的呼叫端問 `capabilities.typing_indicator`。
    """

    __slots__ = ("_channel", "_task")

    def __init__(self, channel: "ChatConversation") -> None:
        self._channel = channel
        self._task = None

    async def __aenter__(self):
        if self._channel.capabilities.typing_indicator:
            self._task = asyncio.ensure_future(self._channel._typing_loop())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass
        return False


class ChatConversation:
    """一個對話（頻道／群組／私訊）。handler 用 `.id` / `.send()` / `.typing()`。"""

    __slots__ = ("id", "platform_chat_id", "is_direct", "is_command_chat",
                 "guild", "_transport")

    def __init__(self, transport: "ChatTransport", platform_chat_id: str, *,
                 uid: int, is_direct: bool, is_command_chat: bool) -> None:
        self._transport = transport
        self.platform_chat_id = platform_chat_id
        self.id = uid
        self.is_direct = is_direct
        self.is_command_chat = is_command_chat
        # 別的平台沒有「伺服器」這一層。`None` 正是既有兩個代理給的值，handler 早就
        # 處理得了（私訊本來就沒有）。
        self.guild = None

    @property
    def capabilities(self) -> PlatformCapabilities:
        return self._transport.capabilities

    @property
    def transport(self) -> "ChatTransport":
        return self._transport

    async def send(self, content: Any = None, **kwargs) -> "SentChatMessage | None":
        return await self._transport.deliver(self, content, **kwargs)

    def typing(self) -> _TypingScope:
        return _TypingScope(self)

    async def _typing_loop(self) -> None:
        await self._transport.typing_loop(self)


class ChatMessage:
    """收到的一則訊息——就是 handler 吃的那個鴨子型別。

    屬性刻意只有 `_InteractionMessageProxy` 量到的那幾個。**不要加投機性的屬性**：
    每多一個沒人用的欄位，就是各平台之間多一條沒有測試涵蓋的分歧，而那正是那個
    代理的 docstring 從第一版就寫著的話。
    """

    __slots__ = ("author", "channel", "guild", "mentions", "content", "id",
                 "attachments", "platform", "platform_message_id")

    def __init__(self, *, author: ChatUser, channel: ChatConversation,
                 content: str, message_id: int, platform: str,
                 platform_message_id: str,
                 attachments: list[ChatAttachment] | None = None) -> None:
        self.author = author
        self.channel = channel
        self.guild = channel.guild
        self.mentions: list = []
        self.content = content
        self.id = message_id
        self.platform = platform
        self.platform_message_id = platform_message_id
        self.attachments: list[ChatAttachment] = list(attachments or [])

    async def reply(self, content: Any = None, **kwargs) -> "SentChatMessage | None":
        kwargs.pop("mention_author", None)
        if self.channel.capabilities.reply_reference:
            kwargs.setdefault("reply_to", self.platform_message_id)
        return await self.channel.send(content, **kwargs)


class SentChatMessage:
    """已經送出去的一則訊息。兩個用途：Dorossi 的即時預覽要編輯它，以及事後的結果要
    **回在它底下**（`/gen image` 的「產圖中…」佔位訊息就是這樣用的）。

    `reply()` 與 `ChatMessage.reply` 同一個形狀（2026-09-24 補）。少了它，呼叫端對一則
    bot 自己送出的訊息 `safe_reply` 會丟 `AttributeError`，而那條路的外層把例外吞進
    stderr——產好的圖整張消失、佔位訊息永遠停在「產圖中」，只有這個平台會這樣。

    `edit()` 的行為由平台能力決定，而且**兩種都不是當掉**：
      * 平台編輯得動 → 真的編輯；
      * 編輯不動 → 丟 `UnsupportedOperation`，呼叫端要嘛事先問過
        `capabilities.edit_message` 而根本不走即時預覽，要嘛改用
        `ReplaceOnEditMessage`（下面那一個）把編輯降級成「偶爾補一則新訊息」。
    """

    __slots__ = ("id", "channel", "platform_message_id", "_content")

    def __init__(self, channel: ChatConversation, platform_message_id: str,
                 content: str) -> None:
        self.channel = channel
        self.platform_message_id = platform_message_id
        self.id = external_uid(f"{channel.transport.name}#msg",
                               str(platform_message_id))
        self._content = content

    async def reply(self, content: Any = None, **kwargs) -> "SentChatMessage | None":
        kwargs.pop("mention_author", None)
        if self.channel.capabilities.reply_reference:
            kwargs.setdefault("reply_to", self.platform_message_id)
        return await self.channel.send(content, **kwargs)

    async def edit(self, content: Any = None, **kwargs) -> "SentChatMessage":
        if not self.channel.capabilities.edit_message:
            raise UnsupportedOperation(
                f"{self.channel.transport.name} cannot edit a sent message")
        text = "" if content is None else str(content)
        if text == self._content:
            return self
        await self.channel.transport.revise(self, text, **kwargs)
        self._content = text
        return self


class ReplaceOnEditMessage(SentChatMessage):
    """「編輯不動」那一種平台的降級實作：把編輯變成節流過的新訊息。

    **這是明說出來的取捨，不是修好了。** 即時預覽在這種平台上只剩下每隔
    `min_interval_sec` 一則的進度訊息，而最後那一則答案是獨立的一則訊息；不想要這
    種行為的呼叫端應該在**建立預覽之前**先問 `capabilities.edit_message`，直接不
    開串流。把它放進介面裡，是為了讓下一個接平台的人有一個現成、而且行為寫得明白
    的選擇，而不是自己在 transport 裡臨時湊一個。

    ⚠️ **節流一定要配一次「補送」，否則最後一次編輯會被吃掉。** 呼叫端
    （`discord_bot._DorossiLiveMessage.finalize`）送最終答案的方式就是再編輯一次，
    而那一次若剛好落在節流窗裡，純節流的版本會**安靜地丟掉整個答案**——使用者看到
    的是一則停在半路的進度訊息，而且沒有任何地方會講。所以被擋下來的內容會存著，
    由一個背景任務在窗口結束時補送最新的那一份。代價只是最終答案最多晚
    `min_interval_sec` 秒出現。
    """

    __slots__ = ("_last_emitted", "_min_interval", "_pending", "_flush")

    def __init__(self, channel: ChatConversation, platform_message_id: str,
                 content: str, *, min_interval_sec: float = 20.0) -> None:
        super().__init__(channel, platform_message_id, content)
        self._min_interval = float(min_interval_sec)
        self._last_emitted = _monotonic()
        self._pending: str | None = None
        self._flush = None

    async def edit(self, content: Any = None, **kwargs) -> "SentChatMessage":
        text = "" if content is None else str(content)
        if text == self._content:
            return self
        now = _monotonic()
        if now - self._last_emitted < self._min_interval:
            self._pending = text
            if self._flush is None or self._flush.done():
                self._flush = asyncio.ensure_future(self._flush_later())
            return self
        return await self._emit(text)

    async def _emit(self, text: str) -> "SentChatMessage":
        self._last_emitted = _monotonic()
        self._content = text
        self._pending = None
        sent = await self.channel.send(text)
        return sent or self

    async def _flush_later(self) -> None:
        """窗口結束時補送最新的那一份。被取消是正常收場，永不往外拋。"""
        try:
            while True:
                wait = self._min_interval - (_monotonic() - self._last_emitted)
                if wait > 0:
                    await asyncio.sleep(wait)
                text = self._pending
                if text is None or text == self._content:
                    return
                await self._emit(text)
                if self._pending is None:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-except
            # 補送失敗就算了：它是「盡力而為」的那一層，而這是背景任務，往外拋只會
            # 變成一句沒有上下文的 'Task exception was never retrieved'。
            return


def _monotonic() -> float:
    # 抽成一支是為了讓測試換得掉；`time` 只在這裡用到。
    import time
    return time.monotonic()


# ---------------------------------------------------------------------------
# Transport 與註冊表
# ---------------------------------------------------------------------------
@dataclass
class TransportContext:
    """建立 transport 需要的一切。**由 `discord_bot` 注入，反過來就是循環相依。**

    `handle_message` 是「收到一則訊息要做什麼」——實際上就是
    `discord_bot.dispatch_external_message`。transport 不 import bot，bot 也不必
    知道任何一個平台的細節。
    """

    config: dict = field(default_factory=dict)
    owner_uid: int = 0
    command_channel_id: int = 0
    handle_message: Callable[[ChatMessage], Awaitable[None]] | None = None
    project_root: Any = None
    # 平台斷線之後又連得上時叫一次（送出停著的答案）。不得 raise、不得卡住收訊。
    on_recovered: Callable[[], Awaitable[None]] | None = None


class ChatTransport(abc.ABC):
    """一個平台的長命背景迴圈。

    生命週期與既有的幾條背景迴圈完全一樣：啟動時建立、
    `_ensure_background_tasks_alive` 每次重新連線時救活、死掉時留一行**帶名字**的
    紀錄。所以 `run()` 必須是「會一直跑下去」的協程；它自己 return 就等於這個平台
    停了。
    """

    name: str = "chat"

    @property
    @abc.abstractmethod
    def capabilities(self) -> PlatformCapabilities:
        """這個平台做得到什麼。"""

    @abc.abstractmethod
    async def run(self) -> None:
        """長命迴圈。"""

    @abc.abstractmethod
    async def deliver(self, channel: ChatConversation, content: Any,
                      **kwargs) -> SentChatMessage | None:
        """把一則訊息送到 `channel`。"""

    async def revise(self, sent: SentChatMessage, content: str, **kwargs) -> None:
        """編輯一則已送出的訊息。編輯不動的平台不必實作（`SentChatMessage.edit`
        在呼叫到這裡之前就會丟 `UnsupportedOperation`）。"""
        raise UnsupportedOperation(f"{self.name} cannot edit a sent message")

    async def typing_loop(self, channel: ChatConversation) -> None:
        """「正在輸入」的持續回報。被取消是正常收場，不要在這裡吞掉取消。"""
        await asyncio.sleep(0)

    def conversation_for(self, platform_chat_id: str) -> ChatConversation | None:
        """從存下來的對話 id 重建一個對話，給「事後才送」的東西用（排程回報之類）。

        回 None ＝這個平台不支援，或那個對話不是它肯回話的地方。**授權在這裡決定**：
        那個 id 來自磁碟，而磁碟不能自己決定 bot 往哪裡說話——所以只放行這個平台本來
        就會回話的對話（允許清單、擁有者的私訊）。預設不支援。"""
        del platform_chat_id
        return None

    async def close(self) -> None:
        """關掉這個 transport 自己開的資源。永不 raise。"""


def origin_of(channel: Any) -> dict:
    """事後要回到這個對話時該存的欄位：`{"platform", "platform_chat_id"}`。

    既有平台的頻道回空 dict——那邊照舊只存整數頻道 id。別的平台不能只存整數：私訊
    的 id 是負數（既有平台找不到），允許清單裡的對話等於 `CHANNEL_ID`（找到的是既有
    平台的頻道）。"""
    if isinstance(channel, ChatConversation):
        return {"platform": channel.transport.name,
                "platform_chat_id": str(channel.platform_chat_id)}
    return {}


def find_conversation(transports: Iterable[ChatTransport],
                      record: Any) -> tuple[bool, ChatConversation | None]:
    """`origin_of` 存下來的紀錄 → `(這是不是別的平台的紀錄, 對話或 None)`。

    第一個值讓呼叫端分得出兩件事：「不是別的平台的紀錄」要照舊走既有平台；「是、
    但找不回來」（平台沒開、對話沒被授權）**不可以**退回既有平台的頻道——那正是
    這一支要修的「送錯地方」。永不 raise。"""
    if not isinstance(record, dict):
        return False, None
    platform = record.get("platform")
    if not isinstance(platform, str) or not platform:
        return False, None
    chat_id = record.get("platform_chat_id")
    if not isinstance(chat_id, str) or not chat_id.strip():
        return True, None
    for transport in transports:
        if getattr(transport, "name", None) != platform:
            continue
        try:
            return True, transport.conversation_for(chat_id.strip())
        except Exception as error:  # pylint: disable=broad-except
            print(f"chat transport {platform!r} could not rebuild a conversation: "
                  f"{type(error).__name__}", file=sys.stderr)
            return True, None
    return True, None


_FACTORIES: dict[str, Callable[[TransportContext], ChatTransport | None]] = {}


def register_transport(
        name: str,
        factory: Callable[[TransportContext], ChatTransport | None]) -> None:
    """註冊一個平台。`factory` 回 `None` 代表「沒設定」——**那必須是安靜的**。

    「沒設定」與「設定壞了」是兩件事：前者是絕大多數人的常態（一個 repo 不會同時
    接四個平台），每次啟動都抱怨一次就是下一個洗掉記錄檔的雜訊源；後者才要出聲，
    而且由 factory 自己在那一刻講清楚是哪一個鍵。
    """
    _FACTORIES[name] = factory


def registered_transports() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def import_transport_modules() -> None:
    """把 `TRANSPORT_MODULES` 列到的模組載進來（註冊是 import 的副作用）。

    兩種 import 形狀都試，理由與 `_bot_config` 對 `_warn_dedup` 的那一段一模一樣：
    直接跑 `discord_bot.py` 時 `axiomatic/` 自己在 `sys.path` 上（裸名成立），走
    套件路徑時不在（只有 `axiomatic.x` 成立）。只寫一種的話，另一條路會在 import
    期整支炸掉，而本機常走的那一側永遠是綠的。
    """
    for name in TRANSPORT_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            importlib.import_module(f"axiomatic.{name}")


def build_transports(context: TransportContext) -> list[ChatTransport]:
    """建出所有「有設定而且開著」的 transport。沒設定的平台安靜缺席。

    一個 factory 自己爆掉不得拖垮其他平台，也不得拖垮 bot 的啟動——這是
    `_ensure_background_tasks_alive` 對背景迴圈立的同一條規則（每一條各自 try，
    失敗只留一行帶名字的紀錄，下一次重新連線還會再試）。
    """
    import_transport_modules()
    built: list[ChatTransport] = []
    for name in sorted(_FACTORIES):
        try:
            transport = _FACTORIES[name](context)
        except Exception as error:  # pylint: disable=broad-except
            print(f"chat transport {name!r} could not be built: {error!r}",
                  file=sys.stderr)
            continue
        if transport is not None:
            built.append(transport)
    return built
