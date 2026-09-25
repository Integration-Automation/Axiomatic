"""第二個對話平台的 transport：長輪詢，走 `aiohttp`，沒有新的相依。

**為什麼是長輪詢而不是 webhook。** webhook 要一個公開網址與憑證；這台機器是家用
桌機，`_connectivity` 那一整套存在的理由就是它的網路會斷。長輪詢反過來：由本機
主動連出去，斷線自己重連，什麼都不必對外開。代價是每次要多一個閒置中的 HTTP 連線
——對一個本來就長時間跑著的 bot 行程來說不算代價。

**為什麼不裝那個平台的 SDK。** 這裡用到的是四個 HTTP 方法（收更新、送訊息、編輯
訊息、送檔案），`aiohttp` 已經是既有相依。多一個 SDK 會讓每個 fresh clone 都付
錢，而 `CLAUDE.md` DoD #4 對這件事寫得很明白。

**重開機之後不補跑舊指令。** 平台會把沒確認過的更新留著，所以重新連上去的第一批
裡可能有 bot 停機期間累積的訊息。那些一律丟掉（比啟動時刻早的都丟）：這條路可以
在主機上執行指令，而「重啟之後突然重跑一批不知道多久以前的主機控制指令」比「漏掉
一則訊息」危險得多。這與 Dorossi 排隊提問跨重啟還原的立場一致——那一條每一列都要
向平台查回發起人才跑，理由是同一個。

**送出去的字一律泛用（Secrecy Layer 1）。** 這個檔案裡沒有任何一句會把主機路徑、
原始例外文字或外部服務名送出去；診斷全部走 stderr。平台自己的名字也不出現在送出
字串裡。
"""
from __future__ import annotations

import asyncio
import math
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

try:
    from _chat_platform import (  # noqa: E402
        ChatAttachment, ChatConversation, ChatMessage, ChatTransport, ChatUser,
        PlatformCapabilities, SentChatMessage, TransportContext,
        DeliveryFailed, chunk_text, conversation_uid, external_uid, outbound_parts,
        register_transport, resolve_identity,
    )
except ImportError:  # 套件路徑（`from axiomatic import _telegram_transport`）
    from axiomatic._chat_platform import (  # type: ignore  # noqa: E402
        ChatAttachment, ChatConversation, ChatMessage, ChatTransport, ChatUser,
        PlatformCapabilities, SentChatMessage, TransportContext,
        DeliveryFailed, chunk_text, conversation_uid, external_uid, outbound_parts,
        register_transport, resolve_identity,
    )

# 附件下載讀到 EOF 且有上限：`read(n)` 只回「目前緩衝的那一段」，理由與實測在那一支。
try:
    from _external_apis import read_capped_body  # noqa: E402
except ImportError:  # 套件路徑
    from axiomatic._external_apis import read_capped_body  # type: ignore  # noqa: E402

# 重連退避用**共用的那一份**，不要自己再寫一次 `min(d * 2, cap)`。
# `test_bot_helpers.test_no_hand_rolled_doubling_backoff_survives_anywhere` 釘住
# 這件事，理由是實測過的：全專案曾經有兩份加倍退避，而它們的**第一次等待**語意
# 不一樣（一份等最小值、一份等兩倍），兩份都「看起來對」。
try:
    from _supervisor import restart_backoff  # noqa: E402
except ImportError:  # 套件路徑
    from axiomatic._supervisor import restart_backoff  # type: ignore  # noqa: E402

PLATFORM_NAME = "telegram"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 憑證檔，形狀與既有那一個一樣（整份檔案 strip 之後就是憑證，或一行
# `key: value`）。**永不追蹤**，與 `auth.md`、`discord_bot_token.md` 同一個處置：
# repo 只帶 `telegram_bot_token.example.md` 範本，第一次設定就是把它複製成同名的
# 正式檔再填。分類寫在 `test_gitignore_coverage._IGNORED_RUNTIME`（`CLAUDE.md` 的
# Git Commits：新的根目錄執行期檔案一加進來就要分類，這是 fail-closed 的）。
# 檔案是空的就等於「沒設定」，這個平台安靜缺席。
TELEGRAM_TOKEN_FILE = _PROJECT_ROOT / "telegram_bot_token.md"

# 平台硬上限。文字 4096、說明文字 1024、上傳 50 MB。切塊留餘裕（與既有那一側的
# 1900/2000 同一個理由：附加的提示字不該把整則訊息撐破上限而整則送不出去）。
TEXT_HARD_LIMIT = 4096
TEXT_CHUNK_LIMIT = 3900
UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
# 收進來的附件：平台的下載端點上限是 20 MB，再加一個我們自己的上限免得一則訊息
# 就把記憶體吃掉。
DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024

# 長輪詢的預設秒數。HTTP 逾時一定要比它大，否則每一輪都會在對方還在等的時候被
# 自己切掉，看起來像「網路一直斷」。
DEFAULT_POLL_TIMEOUT_SEC = 30.0
_HTTP_TIMEOUT_MARGIN_SEC = 20.0
# 「正在輸入」在對方那邊只撐 5 秒，所以要持續補。
_TYPING_REFRESH_SEC = 4.0
# 連線失敗的退避。與監督者那一套同一個形狀：起步小、指數成長、封頂，健康一段時間
# 就歸零——沒有封頂的話一次長時間斷網會讓重連間隔長到網路回來也醒不過來。
_BACKOFF_MIN_SEC = 2.0
_BACKOFF_MAX_SEC = 120.0
# 被限流時最多等這麼久再重試一次。**必須有上限**：等待秒數是對方給的，照單全收
# 等於讓對方決定這個協程卡多久，而握著它的可能是一輪正在回答的問答。
_RATE_LIMIT_WAIT_MAX_SEC = 30.0


def read_platform_token(path: Path) -> str:
    """讀憑證檔。檔案不在、是空的、解不開 → 回空字串（＝沒設定）。

    **解不開時不丟例外、也不印內容。** 這整個檔案就是一個憑證，而裸的
    `UnicodeDecodeError` 會把解不開的那個位元組印出來；既有的 `read_token` 為了
    同一件事寫了一整段註解。差別在方向：那一支讀不成就讓 bot 停下來（沒有它 bot
    根本上不了線），這一支讀不成只是這個平台不啟用，bot 其餘部分照常。
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as error:
        print(f"telegram: token file is unusable ({type(error).__name__}); "
              "this platform stays off", file=sys.stderr)
        return ""
    if not raw:
        return ""
    if ":" in raw.splitlines()[0] and "\n" not in raw:
        head, _, value = raw.partition(":")
        # 平台的憑證本身**含冒號**（`<數字>:<字串>`），所以「第一段是不是一個欄位
        # 名」要真的問一次，不能像既有那一支一樣看到冒號就切——切下去會把憑證的前
        # 半截丟掉，而症狀是「認證失敗」，完全看不出原因。
        if head.strip() and not head.strip().isdigit():
            return value.strip()
    return raw


class _Unreachable:
    """`_api` 連不上平台（或連兩次被限流）時回的標記，不是 `None`。

    假值、不是 dict 也不是 list，所以只問「有沒有結果」的呼叫端（收訊、下載、打字中）
    照舊把它當成失敗；要分辨「平台不在」與「平台說了不」的（送出、編輯）才認它，並丟
    `DeliveryFailed`。不用一個共用的旗標記錄上一次的原因：收訊與送出是同時進行的，
    旗標會被另一條路蓋掉。"""

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "UNREACHABLE"


UNREACHABLE = _Unreachable()


class TelegramTransport(ChatTransport):
    """一個平台的長輪詢迴圈。長命、受監督、死掉時留一行帶名字的紀錄。"""

    name = PLATFORM_NAME

    def __init__(self, token: str, *, context: TransportContext,
                 owner_ids: tuple[str, ...], allowed_chat_ids: tuple[str, ...],
                 poll_timeout_sec: float = DEFAULT_POLL_TIMEOUT_SEC) -> None:
        self._token = token
        self._context = context
        self._owner_ids = tuple(owner_ids)
        self._allowed_chat_ids = tuple(allowed_chat_ids)
        self._poll_timeout = max(1.0, float(poll_timeout_sec))
        self._session: aiohttp.ClientSession | None = None
        self._offset: int | None = None
        self._started_at = 0.0
        self._chats: dict[str, ChatConversation] = {}
        self._unreachable_since_recovery = False
        # 處理中的更新。參照留在這裡，工作才不會在跑到一半時被回收。
        self._inflight: set[asyncio.Task] = set()

    # -- 能力 --------------------------------------------------------------
    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            edit_message=True,
            send_files=True,
            send_images=True,
            typing_indicator=True,
            # 回覆引用送得出去，但平台不會把「引用的那則被刪掉」當錯誤，所以不需要
            # 既有那一側的退路。
            reply_reference=True,
            read_attachments=True,
            text_limit=TEXT_CHUNK_LIMIT,
            file_bytes_limit=UPLOAD_LIMIT_BYTES,
        )

    # -- HTTP --------------------------------------------------------------
    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self._poll_timeout + _HTTP_TIMEOUT_MARGIN_SEC)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _api(self, method: str, payload: dict | None = None, *,
                   data: Any = None) -> Any:
        """呼叫一個 API 方法，回 `result`；失敗回 `None` 並留一行 stderr。

        **憑證不進任何一行紀錄。** 網址裡帶著它，所以出錯時印的是方法名與狀態碼，
        不是 `str(error)`（那會把整個網址、也就是憑證印出來）——這是 Layer 1
        「raw uncontrolled internal strings」在本機記錄檔這一側的同一條理由，而且
        記錄檔有 `/log tail` 這個使用者面的出口。

        **被限流時等一下再試一次。** 平台對單一對話大約每秒一則，而一段長回覆會被
        切成好幾則連著送——不處理的話後面那幾塊會**安靜地消失**，使用者拿到的是一
        個被截斷的答案，而且沒有任何地方會講。等待秒數用對方給的 `retry_after`，
        但夾一個上限：那個值是對方說的，不設上限等於讓對方決定這個協程卡多久。
        只重試一次，連兩次都被限流就放棄——再重試只是把限流拉長。
        """
        for attempt in (0, 1):
            body = await self._request(method, payload, data)
            if body is None:
                return UNREACHABLE
            if body.get("ok"):
                return body.get("result")
            code = body.get("error_code")
            retry_after = 0.0
            if code == 429 and attempt == 0:
                parameters = body.get("parameters")
                if isinstance(parameters, dict):
                    raw = parameters.get("retry_after")
                    # `NaN` 要擋：JSON 解析器收得下它，`max`／`min` 夾不住它，
                    # 而 `asyncio.sleep(nan)` 會丟 `ValueError`——在收更新那條路上
                    # 等於整個平台停掉。
                    if (isinstance(raw, (int, float)) and not isinstance(raw, bool)
                            and not math.isnan(raw)):
                        retry_after = min(max(float(raw), 1.0),
                                          _RATE_LIMIT_WAIT_MAX_SEC)
                    else:
                        retry_after = 1.0
            if retry_after <= 0:
                print(f"telegram: {method} rejected (error_code={code})",
                      file=sys.stderr)
                # 第二次還是被限流：那不是「平台說不」，是「現在送不了」。
                return UNREACHABLE if code == 429 else None
            await asyncio.sleep(retry_after)
        return None

    async def _request(self, method: str, payload: dict | None,
                       data: Any) -> dict | None:
        """一次 HTTP 往返。回解析出來的 JSON 物件，連不上／解不開回 `None`。"""
        session = await self._ensure_session()
        try:
            if data is not None:
                response = await session.post(self._url(method), data=data)
            else:
                response = await session.post(self._url(method),
                                              json=payload or {})
            async with response:
                body = await response.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pylint: disable=broad-except
            print(f"telegram: {method} failed ({type(error).__name__})",
                  file=sys.stderr)
            return None
        if not isinstance(body, dict):
            print(f"telegram: {method} returned something that is not a "
                  "response object", file=sys.stderr)
            return None
        return body

    # -- 迴圈 --------------------------------------------------------------
    async def run(self) -> None:
        self._started_at = time.time()
        backoff = _BACKOFF_MIN_SEC
        print(f"telegram transport started (poll={self._poll_timeout:.0f}s)")
        try:
            while True:
                updates = await self._api("getUpdates", {
                    "timeout": int(self._poll_timeout),
                    "offset": self._offset,
                    "allowed_updates": ["message"],
                })
                offset_before = self._offset
                self._note_reachability(updates is not UNREACHABLE)
                if updates is not None and not isinstance(updates, list):
                    updates = None
                if updates is not None:
                    for update in updates:
                        if not isinstance(update, dict):
                            continue
                        update_id = update.get("update_id")
                        if isinstance(update_id, int) and not isinstance(update_id, bool):
                            self._offset = update_id + 1
                        self._start_update(update)
                    if updates and self._offset == offset_before:
                        # 有東西卻一筆都沒讓 offset 前進（壞資料）：下一輪會立刻拿回同一批，
                        # 不等的話就是一條不睡覺的迴圈。當成失敗退避。
                        print("telegram: a batch of updates carried no usable id",
                              file=sys.stderr)
                        updates = None
                if updates is None:
                    # 連不上／被拒。退避之後再試——這條迴圈是長命的，不能因為一次
                    # 失敗就 return（return 等於這個平台默默地停了，而
                    # `_ensure_background_tasks_alive` 只有重新連線時才會救它）。
                    wait, backoff = restart_backoff(
                        backoff, minimum=_BACKOFF_MIN_SEC,
                        maximum=_BACKOFF_MAX_SEC, healthy=False)
                    await asyncio.sleep(wait)
                    continue
                _wait, backoff = restart_backoff(
                    backoff, minimum=_BACKOFF_MIN_SEC,
                    maximum=_BACKOFF_MAX_SEC, healthy=True)
        finally:
            await self.close()

    def _start_update(self, update: dict) -> None:
        """一筆更新交給自己的工作，收更新的迴圈**不等它**。

        等的話，一輪跑幾十分鐘的 Dorossi 問答會讓這段期間一次 `getUpdates` 都不發：
        `abort` 送不進來，而這段期間送的每一則都留在平台上，等那一輪結束才一口氣
        照跑——包括擁有者早就放棄的主機控制指令，正是模組 docstring 說要防的事。
        既有那一側也是每則訊息各自一個工作，這裡對齊它。
        """
        task = asyncio.get_running_loop().create_task(
            self._handle_update(update), name="telegram-update")
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _handle_update(self, update: dict) -> None:
        try:
            await self._on_update(update)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pylint: disable=broad-except
            # 一則訊息處理失敗不得讓整條迴圈死掉：那會讓平台安靜地停在一則壞訊息上，
            # 而使用者看到的只是「沒有回應」。
            print("telegram: update handling raised "
                  f"{type(error).__name__}", file=sys.stderr)

    async def _on_update(self, update: dict) -> None:
        raw = update.get("message")
        if not isinstance(raw, dict):
            return
        sent_at = raw.get("date")
        # 停機期間累積的訊息丟掉（見模組 docstring）。平台給的是**整秒**，所以跟啟動
        # 時刻的整秒比——跟帶小數的啟動時刻比，會把啟動那一秒裡送的訊息也丟掉。
        # 沒有可用的時刻就丟：這道檢查擋的是主機控制指令的重播，看不出來時不放行。
        if (not isinstance(sent_at, (int, float)) or isinstance(sent_at, bool)
                or not sent_at >= math.floor(self._started_at)):
            return
        sender = raw.get("from") or {}
        if not isinstance(sender, dict) or sender.get("is_bot"):
            return
        chat = raw.get("chat") or {}
        if not isinstance(chat, dict):
            return
        chat_id = chat.get("id")
        if chat_id is None:
            return

        uid, is_owner = resolve_identity(
            PLATFORM_NAME, sender.get("id"), self._owner_ids,
            self._context.owner_uid)
        channel_id, is_command_chat = conversation_uid(
            PLATFORM_NAME, chat_id,
            command_channel_id=self._context.command_channel_id,
            allowed_chat_ids=self._allowed_chat_ids)
        if not is_owner and not is_command_chat:
            # 頻道閘。與既有那一側逐字同一條規則：不是設定頻道、又不是擁有者，
            # **安靜**返回（不回話、不計數、不寫稽核）。回話等於對任何陌生人確認
            # 這個帳號後面有東西在跑。
            return

        text = raw.get("text") or raw.get("caption") or ""
        if not isinstance(text, str) or not text.strip():
            return
        if _is_forwarded(raw) and text.lstrip().startswith("!"):
            # 轉發進來的訊息，寄件者是**轉發的人**，內容卻是別人寫的。照跑的話，擁有者
            # 轉發一則別人的 `!…` 就會以擁有者身分在主機上執行它。當成內容可以（拿去問），
            # 當成指令不行。
            print("telegram: a forwarded command was not run", file=sys.stderr)
            return

        conversation = self._conversation(chat_id, chat.get("type"),
                                          channel_id, is_command_chat)
        author = ChatUser(uid, str(sender.get("id")),
                          str(sender.get("first_name")
                              or sender.get("username") or "user"),
                          is_owner=is_owner)
        message = ChatMessage(
            author=author, channel=conversation, content=text.strip(),
            message_id=_message_uid(chat_id, raw.get("message_id")),
            platform=PLATFORM_NAME,
            platform_message_id=str(raw.get("message_id") or ""),
            attachments=self._attachments(raw),
        )
        handler = self._context.handle_message
        if handler is None:
            return
        await handler(message)

    def _conversation(self, chat_id: Any, chat_type: Any, channel_id: int,
                      is_command_chat: bool) -> ChatConversation:
        key = str(chat_id)
        existing = self._chats.get(key)
        if existing is not None:
            return existing
        conversation = ChatConversation(
            self, key, uid=channel_id,
            is_direct=(str(chat_type) == "private"),
            is_command_chat=is_command_chat)
        self._chats[key] = conversation
        return conversation

    def _note_reachability(self, reachable: bool) -> None:
        """收訊那一次連不連得上。從連不上變回連得上時，叫一次 `on_recovered`（送出停著的
        答案）；交給一個獨立的 task，收訊不等它。"""
        if not reachable:
            self._unreachable_since_recovery = True
            return
        if not self._unreachable_since_recovery:
            return
        self._unreachable_since_recovery = False
        callback = self._context.on_recovered
        if callback is None:
            return
        task = asyncio.ensure_future(callback())
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    def conversation_for(self, platform_chat_id: str) -> ChatConversation | None:
        """見 `ChatTransport.conversation_for`。放行的只有兩種：允許清單裡的對話，以及
        擁有者的私訊——這個平台上私訊的對話 id 就是對方的使用者 id，所以「擁有者清單
        裡的 id」就是擁有者的私訊。其他一律 None。"""
        key = str(platform_chat_id or "").strip()
        if not key:
            return None
        allowed = {str(one).strip() for one in self._allowed_chat_ids}
        owners = {str(one).strip() for one in self._owner_ids}
        if key not in allowed and key not in owners:
            return None
        channel_id, is_command_chat = conversation_uid(
            PLATFORM_NAME, key, command_channel_id=self._context.command_channel_id,
            allowed_chat_ids=self._allowed_chat_ids)
        chat_type = "private" if key in owners and key not in allowed else "group"
        return self._conversation(key, chat_type, channel_id, is_command_chat)

    def _attachments(self, raw: dict) -> list[ChatAttachment]:
        """Files / photos attached to the message. Takes the biggest photo (the platform sends
        the same picture in several sizes).

        Sizes go through `_as_int`: `int()` raises on an unreadable value, and raising here
        means the whole message (text included) never reaches the dispatcher —
        `_handle_update` only logs the exception type. The size is informational (the download
        has its own cap), so an unreadable one counts as 0."""
        found: list[ChatAttachment] = []
        document = raw.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            found.append(self._attachment(
                document.get("file_id"),
                str(document.get("file_name") or "upload.bin"),
                _as_int(document.get("file_size"))))
        photos = raw.get("photo")
        if isinstance(photos, list) and photos:
            biggest = max(
                (p for p in photos if isinstance(p, dict) and p.get("file_id")),
                key=lambda p: _as_int(p.get("file_size")), default=None)
            if biggest is not None:
                found.append(self._attachment(
                    biggest.get("file_id"), "photo.jpg",
                    _as_int(biggest.get("file_size"))))
        return found

    def _attachment(self, file_id: Any, filename: str,
                    size: int) -> ChatAttachment:
        async def _fetch() -> bytes:
            return await self._download(str(file_id))
        return ChatAttachment(filename, size, _fetch)

    async def _download(self, file_id: str) -> bytes:
        """下載一個附件。失敗一律丟**不帶網址**的 `OSError`。

        網址的路徑裡就是憑證，而 `aiohttp` 的好幾種例外（`ClientResponseError`、
        `TooManyRedirects`、`InvalidUrlClientError`）在 `str()`／`repr()` 裡都帶著
        `url=`。呼叫端會把接到的例外印進記錄檔，對擁有者還會照實送出去——所以在這裡
        換成一句自己寫的話，並用 `from None` 切掉例外鏈（traceback 會印出鏈上的那一個）。
        """
        info = await self._api("getFile", {"file_id": file_id})
        remote = (info or {}).get("file_path") if isinstance(info, dict) else None
        if not remote:
            raise OSError("attachment is not retrievable")
        session = await self._ensure_session()
        url = f"https://api.telegram.org/file/bot{self._token}/{remote}"
        try:
            async with session.get(url) as response:
                status = response.status
                # 讀到 EOF 且有上限。**不是** `read(上限 + 1)`：那只回目前緩衝的那一段，
                # 附件會被安靜地截短，而寫進主機的就是一個壞掉的檔案。
                data = (await read_capped_body(response.content, DOWNLOAD_LIMIT_BYTES)
                        if status == 200 else b"")
        # `OSError` 一起接：`asyncio.TimeoutError` 與 `aiohttp` 的連線錯誤都是它的子類別。
        # 自己的 `OSError` 刻意丟在這個 try 外面，才不必在這裡分辨哪些是自己的。
        except (aiohttp.ClientError, OSError, ValueError) as error:
            print(f"telegram: attachment download failed ({type(error).__name__})",
                  file=sys.stderr)
            raise OSError("attachment download failed") from None
        if status != 200:
            raise OSError(f"attachment download returned {status}")
        if data is None:
            raise OSError("attachment is too large")
        return data

    # -- 送出 --------------------------------------------------------------
    async def deliver(self, channel: ChatConversation, content: Any = None,
                      **kwargs) -> SentChatMessage | None:
        """送一則訊息。回最後一則的 `SentChatMessage`（即時預覽要編輯它）。

        文字超過上限就切塊，**回傳最後一塊**：即時預覽編輯的是最新的那一則，編輯
        第一塊會讓答案長在一段已經被後面蓋過去的文字上。
        """
        reply_to = kwargs.pop("reply_to", None)
        text, files = outbound_parts(content, embed=kwargs.pop("embed", None),
                                     file=kwargs.pop("file", None),
                                     files=kwargs.pop("files", None))
        sent: SentChatMessage | None = None
        for chunk in chunk_text(text, TEXT_CHUNK_LIMIT):
            payload: dict = {
                "chat_id": channel.platform_chat_id,
                "text": chunk,
                # markdown **刻意不開**：兩個平台的方言不一樣，而一段沒跳脫好的
                # 符號在這裡不是顯示得醜，是整則訊息被拒——答案直接消失。純文字
                # 送得出去永遠比排版好看重要。
                "disable_web_page_preview": True,
            }
            if reply_to:
                payload["reply_to_message_id"] = _as_int(reply_to)
                # 引用的那則被刪掉時照樣送出去，不要整則失敗。
                payload["allow_sending_without_reply"] = True
                reply_to = None
            result = await self._api("sendMessage", payload)
            if result is UNREACHABLE:
                # 整則當成沒送出去（前面幾塊可能已經送了——重送時會重複，但重複好過
                # 答案少一截而且沒有人知道）。
                raise DeliveryFailed(f"{PLATFORM_NAME} is unreachable")
            if isinstance(result, dict) and result.get("message_id") is not None:
                sent = SentChatMessage(channel, str(result["message_id"]), chunk)
        for one in files:
            posted = await self._send_file(channel, one)
            sent = posted or sent
        return sent

    async def _send_file(self, channel: ChatConversation,
                         one) -> SentChatMessage | None:
        if len(one.data) > UPLOAD_LIMIT_BYTES:
            # 「有文字沒附件」遠好過「什麼都沒有」——`safe_reply` 對同一個情況已經
            # 是這個立場。理由寫 stderr，對外只給泛用句。
            print(f"telegram: attachment {len(one.data)} bytes exceeds the "
                  "platform limit; sending the notice instead", file=sys.stderr)
            return await self.deliver(channel, "※ 檔案太大，這次沒有附上。")
        form = aiohttp.FormData()
        form.add_field("chat_id", str(channel.platform_chat_id))
        method = "sendPhoto" if one.is_image else "sendDocument"
        field = "photo" if one.is_image else "document"
        form.add_field(field, one.data, filename=one.filename)
        result = await self._api(method, data=form)
        if result is UNREACHABLE:
            raise DeliveryFailed(f"{PLATFORM_NAME} is unreachable")
        if isinstance(result, dict) and result.get("message_id") is not None:
            return SentChatMessage(channel, str(result["message_id"]), "")
        return None

    async def revise(self, sent: SentChatMessage, content: str, **kwargs) -> None:
        text = content if len(content) <= TEXT_HARD_LIMIT \
            else content[:TEXT_HARD_LIMIT]
        result = await self._api("editMessageText", {
            "chat_id": sent.channel.platform_chat_id,
            "message_id": _as_int(sent.platform_message_id),
            "text": text,
            "disable_web_page_preview": True,
        })
        if result is UNREACHABLE:
            # 丟出去，`SentChatMessage.edit` 才不會把沒送到的內容記成「已顯示」。
            raise DeliveryFailed(f"{PLATFORM_NAME} is unreachable")

    async def typing_loop(self, channel: ChatConversation) -> None:
        while True:
            await self._api("sendChatAction", {
                "chat_id": channel.platform_chat_id,
                "action": "typing",
            })
            await asyncio.sleep(_TYPING_REFRESH_SEC)

    async def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:  # pylint: disable=broad-except  # nosec B110
                pass


# 轉發訊息的欄位。新舊兩套都認：新版的 API 只給 `forward_origin`，舊版給其餘那幾個，
# 而連結頻道自動轉發進群組的訊息另外帶 `is_automatic_forward`。
_FORWARD_FIELDS = ("forward_origin", "forward_from", "forward_from_chat",
                   "forward_sender_name", "forward_date", "is_automatic_forward")


def _is_forwarded(raw: dict) -> bool:
    """這則訊息是不是轉發進來的（內容不是寄件者自己寫的）。"""
    return any(raw.get(field) for field in _FORWARD_FIELDS)


def _as_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _message_uid(chat_id: Any, message_id: Any) -> int:
    """訊息 id 的內部號碼。

    平台的訊息 id 只在**同一個對話內**唯一，所以要連對話一起併進去，否則兩個對話
    的第 7 則訊息會是同一個號碼——而這個號碼會被拿去當 Dorossi 佇列的錨點。
    """
    return external_uid(f"{PLATFORM_NAME}#msg", f"{chat_id}:{message_id}")


def _str_list(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(one).strip() for one in raw if str(one).strip())


def build(context: TransportContext) -> TelegramTransport | None:
    """設定檔 ＋ 憑證檔 → transport，或 `None`（沒設定／沒開）。

    **沒設定必須安靜**（見 `register_transport` 的 docstring）。只有「開關打開了、
    憑證卻讀不出來」這一種才出聲：那是使用者以為打開了而其實沒有的情況，症狀跟沒
    打開一模一樣。
    """
    section = (context.config or {}).get(PLATFORM_NAME) or {}
    if not section.get("enabled"):
        return None
    token = read_platform_token(TELEGRAM_TOKEN_FILE)
    if not token:
        print("telegram: enabled in the config but the token file is empty; "
              "this platform stays off", file=sys.stderr)
        return None
    owner_ids = _str_list(section.get("owner_user_ids"))
    if not owner_ids:
        # 沒有擁有者 id ＝ 這個平台上沒有人過得了主機控制閘。仍然啟動（唯讀指令還
        # 能用），但要講一聲——這正是 fail-closed 最不會有人發現的那一種。
        print("telegram: no owner id configured; host-control commands will be "
              "refused for everyone on this platform", file=sys.stderr)
    return TelegramTransport(
        token, context=context, owner_ids=owner_ids,
        allowed_chat_ids=_str_list(section.get("allowed_chat_ids")),
        poll_timeout_sec=section.get("poll_timeout_sec")
        or DEFAULT_POLL_TIMEOUT_SEC)


register_transport(PLATFORM_NAME, build)
