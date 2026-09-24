"""The chat-platform adapter seam: lets the same set of handlers run on other
platforms too.

The bot's handlers were never really tied to one library's message type — they
consume a **duck type**, and that type is small enough to write down in full.
`_InteractionMessageProxy`'s docstring measured it: the whole module uses only
`reply`, `author`, `channel` (`.send` / `.id` / `.typing()`), `mentions`,
`guild`, `content`, `id`, `attachments`, and nothing else.
`_DorossiRestoredMessage` is a second proxy with the same shape. **That shape is
the seam this module generalises.**

This module is standard library only, and **deliberately does not import
`discord`** (nor `discord_bot` — that would be circular). Outbound attachments
and embeds are read by duck typing throughout (`.fp` / `.filename` / `.title` /
`.description` / `.fields`), so the `discord.File` / `discord.Embed` an existing
handler hands over feed in without any rewrite.

## The five things in the interface

1. **Identity** (`ChatUser`) — `id` is the **bot's internal integer identity**,
   not the one on the platform. See `resolve_identity()` for the mapping: the
   owner maps to `OWNER_USER_ID`, everyone else to a **negative** number. Every
   gate in the repo is written `author.id != OWNER_USER_ID`, so putting the
   mapping at this one decision point makes every existing gate hold on every
   platform without a word changing; a negative number is guaranteed never to
   collide with any Discord id that could be configured (`_coerce_int_list`
   accepts only `>= 0`).
2. **Conversation** (`ChatConversation`) — `.id` / `.send()` / `.typing()`. A
   conversation in the allow list gets `.id` equal to `CHANNEL_ID`: that
   conversation **is** this platform's "command channel", so `!`'s channel gate
   and help's `include_channel_only` hold automatically.
3. **Received message** (`ChatMessage`) — `content` / `attachments` / `reply()`.
4. **Sent message** (`SentChatMessage`) — `.edit()`. Dorossi's live preview edits
   the same message for the whole round, so this slot is necessary, not
   decorative.
5. **Things it cannot do** (`PlatformCapabilities` + `UnsupportedOperation`) —
   capabilities can be queried by flag and the caller **degrades explicitly**;
   something it cannot do raises `UnsupportedOperation`, not a crash and not a
   silent nothing-happened. `ReplaceOnEditMessage` is the degraded
   implementation for the "cannot edit" kind of platform (e.g. a push-only one).

## Why not "write another command tree"

The owner has ruled: other platforms reuse the existing hidden text dispatcher
(`!` commands and `@bot <text>`), and the slash tree stays as-is. So this module
**knows no commands at all** — it only turns a platform message into an object a
handler recognises, then hands it to `discord_bot.dispatch_external_message`.
"""
from __future__ import annotations

import abc
import asyncio
import hashlib
import importlib
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

# One row per new platform. **This list is the sole entry point for
# registration**: `build_transports()` only imports the modules listed here, and
# a file not listed is never loaded even if it sits in the package. It is
# reconciled the other way too — `test_platform_transports` compares the
# `_*_transport.py` files on disk against this list in both directions: an extra
# file not listed here means **that whole platform silently does not exist**,
# which looks exactly like "not configured, so not enabled".
TRANSPORT_MODULES: tuple[str, ...] = ("_telegram_transport",)


class UnsupportedOperation(RuntimeError):
    """This platform cannot do this.

    **This is part of the interface, not an accident.** Platforms differ a lot in
    capability (some cannot edit an already-sent message, some cannot send
    files), and "silently nothing happened" is the most expensive failure mode:
    the user thinks it was sent. So something that cannot be done either has the
    caller query `capabilities` first and decide accordingly, or is raised here
    to be seen.
    """


class DeliveryFailed(ConnectionError):
    """The platform is **unreachable** (or keeps rate-limiting) and this message
    did not go out. Kept separate from "the platform said no" (4xx): the former
    succeeds once the connection returns, the latter fails again on resend.

    A subclass of `ConnectionError`, so the caller treats it as "the chat
    platform disconnected" — Dorossi's answer parks in the outbox and is resent
    when the platform returns (2026-09-24). Before this the transport swallowed
    the failure into `None` and the answer silently vanished."""


@dataclass(frozen=True)
class PlatformCapabilities:
    """What a platform can do. The caller uses this to **degrade explicitly**,
    rather than feeling it out with try/except.

    `text_limit` is the per-message character limit (chunked before sending by
    `chunk_text()`); `file_bytes_limit` is the byte limit for a single
    attachment, and 0 means this platform accepts no attachments.
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
# Identity mapping (fail-closed)
# ---------------------------------------------------------------------------
# `OWNER_USER_ID` is an id **on one particular platform**, and every gate in the
# repo compares against it directly (`message.author.id != OWNER_USER_ID`, one
# copy per surface, reconciled both ways by `test_bot_helpers`). So identity on
# another platform must either open an extra branch on every gate, or be mapped
# into the same number system **at the moment it arrives**. The latter is chosen:
# no gate changes a word, and "who is the owner" has a single decision point here
# — the same reasoning `CLAUDE.md` gives for `_owner_detail()`.
#
# A non-owner maps to a **negative** number, for three reasons:
#   * never equal to `OWNER_USER_ID` (which is positive), so fail-closed is
#     structural, not a matter of remembering to write the comparison right;
#   * `_coerce_int_list` accepts only `>= 0`, so a negative number can never
#     appear in `user_roles` or `path_reveal_channel_ids` — a user on another
#     platform gets no role and no "path-revealing surface";
#   * the same person gets the same number every time (hashed from platform name
#     + platform id), so audit records and command counts can still tell people
#     apart.
_UID_DIGEST_BYTES = 6
# Used when the sender's identity cannot be read. **Not 0**: `alert_user_id`'s
# default is 0, and letting two different things share one number means someone
# will eventually wire them together.
UNKNOWN_SENDER_UID = -1


def external_uid(platform: str, platform_id: str) -> int:
    """Platform id → this bot's internal negative identity. The same input always
    gives the same number."""
    raw = f"{platform}:{platform_id}".encode("utf-8")
    digest = hashlib.blake2b(raw, digest_size=_UID_DIGEST_BYTES).digest()
    return -(2 + int.from_bytes(digest, "big"))


def resolve_identity(platform: str, platform_id: Any, owner_ids: Iterable[str],
                     owner_uid: int) -> tuple[int, bool]:
    """Return `(internal uid, is owner)`.

    Compares as **strings**: a platform's user id is not necessarily an integer
    (some platforms use alphanumeric strings), and converting to int before
    comparing would make a valid id silently become "not the owner" on a
    conversion failure — the direction is safe, but the reason vanishes. No id,
    an empty id, or an id not in the configured owner list is always not the
    owner (fail-closed, the same stance as the three existing gates).
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
    """Return `(internal channel id, whether this conversation is the "command
    channel")`.

    A conversation in the allow list gets exactly `CHANNEL_ID` — it **is** this
    platform's command channel, so `!`'s channel gate and help's
    `include_channel_only` all hold automatically, without an extra "or is this
    Telegram" written in every place. Every other conversation gets a negative
    number (same reason as `external_uid`: it collides with no configurable
    channel id, and can never land in `path_reveal_channel_ids`).
    """
    text = "" if chat_id is None else str(chat_id).strip()
    allowed = {str(one).strip() for one in allowed_chat_ids if str(one).strip()}
    if text and text in allowed:
        return command_channel_id, True
    return external_uid(f"{platform}#chat", text), False


# ---------------------------------------------------------------------------
# Outbound: text chunking, attachment normalisation, embed flattening
# ---------------------------------------------------------------------------
_CODE_FENCE = "```"


def _unclosed_fence(text: str) -> str:
    """When `text` ends inside a code block, return a delimiter that can reopen
    the same block."""
    if text.count(_CODE_FENCE) % 2 == 0:
        return ""
    tail = text[text.rfind(_CODE_FENCE) + len(_CODE_FENCE):]
    lang = tail.split("\n", 1)[0].strip() if "\n" in tail else ""
    ok = lang and len(lang) <= 20 and all(c.isalnum() or c in "+#.-" for c in lang)
    return _CODE_FENCE + (lang if ok else "")


def chunk_text(text: str, limit: int) -> list[str]:
    """Split a long reply into a few messages the platform can accept, preferring
    to cut at a newline.

    A code block that spans a cut point is closed in the previous message and
    reopened in the next (carrying the language tag along): each message is
    rendered as markdown on its own, and without this the next message would show
    the code as prose, while its closing delimiter would open a new block and
    swallow everything after it. Same rule as
    `discord_bot._chunk_for_discord`; **deliberately a separate copy**, because
    the limit and the markdown dialect are per-platform, and sharing one copy
    would make one side accommodate the other.
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
        # **A reopen delimiter adds characters back to `rest`, so it must be
        # shorter than the piece just cut.** When the limit is at a normal value
        # (3900) this always holds; but when the limit is about as small as the
        # delimiter, each loop cuts four characters and adds four back, and the
        # loop **never terminates**. This is not a theoretical corner: the
        # chunker is a pure string function and the next platform decides its own
        # limit. If it cannot make progress, give up the formatting — a message
        # getting out is always more important than a code block staying intact.
        if reopen and len(reopen) + 1 < cut:
            head = f"{head}\n{_CODE_FENCE}"
            rest = f"{reopen}\n{rest}"
        chunks.append(head)
    if rest:
        chunks.append(rest)
    return chunks


@dataclass
class OutboundFile:
    """An attachment to send. `data` is already in memory, because every platform
    uploads differently."""

    filename: str
    data: bytes
    is_image: bool = False


_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _read_outbound_file(obj: Any) -> OutboundFile | None:
    """Duck-type read an attachment object (the handler hands over the library's
    `File`).

    If it cannot be read, return `None` and let the caller take the "text but no
    attachment" path — `safe_reply` has long held this stance for the same case:
    "text but no attachment" is far better than "nothing at all".
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
    """Flatten an embed into plain text.

    The native embed is only used on a platform that **has** embeds; every other
    platform must see the same content, and it cannot vanish wholesale just
    because there are no embeds — that would be exactly "silently nothing
    happened". Duck-typed as usual (`.title` / `.description` / `.fields` /
    `.footer`).
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
    """Normalise the handler's send arguments into `(text, attachment list)`.

    The handler's `reply(content, embed=..., file=..., files=...)` form is spread
    across two hundred-plus places in the module, and rewriting each one to be
    platform-neutral is impossible; so the normalisation lives in this one place.
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
# The four objects the handler sees
# ---------------------------------------------------------------------------
class ChatAttachment:
    """A received attachment. The handler uses only `filename` and `await read()`
    (measured, just those two)."""

    __slots__ = ("filename", "size", "_fetch")

    def __init__(self, filename: str, size: int,
                 fetch: Callable[[], Awaitable[bytes]]) -> None:
        self.filename = filename
        self.size = size
        self._fetch = fetch

    async def read(self) -> bytes:
        return await self._fetch()


class ChatUser:
    """The sender. `id` is the **internal uid** (see `resolve_identity`), not the
    one on the platform."""

    __slots__ = ("id", "platform_id", "name", "display_name", "mention",
                 "is_owner")

    def __init__(self, uid: int, platform_id: str, display_name: str, *,
                 is_owner: bool) -> None:
        self.id = uid
        self.platform_id = platform_id
        self.name = display_name or str(platform_id)
        self.display_name = self.name
        # Other platforms have no universal way to "@ a person", so this gives
        # only the display name. The one caller is the salutation in an alert
        # message, and not having a real mention does not affect whether it can
        # be sent.
        self.mention = self.name
        self.is_owner = is_owner

    def __str__(self) -> str:
        # Audit records write `str(message.author)`. Without this method that
        # column becomes `<object at 0x…>` — the record is still there and looks
        # normal, only nobody can tell who it was any more.
        return f"{self.name}@{self.platform_id}"


class _TypingScope:
    """`async with channel.typing():`. On a platform that does not support it,
    this is a do-nothing shell.

    When unsupported it **does not raise**: typing is pure feel, and failing a
    whole command for it is a bad trade. A caller that needs to know asks
    `capabilities.typing_indicator`.
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
    """A conversation (channel / group / DM). The handler uses `.id` / `.send()`
    / `.typing()`."""

    __slots__ = ("id", "platform_chat_id", "is_direct", "is_command_chat",
                 "guild", "_transport")

    def __init__(self, transport: "ChatTransport", platform_chat_id: str, *,
                 uid: int, is_direct: bool, is_command_chat: bool) -> None:
        self._transport = transport
        self.platform_chat_id = platform_chat_id
        self.id = uid
        self.is_direct = is_direct
        self.is_command_chat = is_command_chat
        # Other platforms have no "server" layer. `None` is exactly what the two
        # existing proxies give, and the handler already copes with it (a DM has
        # never had one).
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
    """A received message — the very duck type the handler consumes.

    The attributes are deliberately only those `_InteractionMessageProxy`
    measured. **Do not add speculative attributes**: every extra field nobody
    uses is one more untested divergence between platforms, which is exactly what
    that proxy's docstring has said since its first version.
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
    """A message that has already been sent. Two uses: Dorossi's live preview
    edits it, and a later result **replies underneath it** (the `/gen image`
    "generating…" placeholder message is used exactly this way).

    `reply()` has the same shape as `ChatMessage.reply` (added 2026-09-24).
    Without it, `safe_reply` on a message the bot sent itself would raise
    `AttributeError`, and the outer layer of that path swallows the exception into
    stderr — the finished image vanishes entirely and the placeholder stays stuck
    at "generating" forever, on this platform alone.

    `edit()`'s behaviour is decided by platform capability, and **neither is a
    crash**:
      * the platform can edit → it really edits;
      * it cannot edit → raises `UnsupportedOperation`, and the caller either
        queried `capabilities.edit_message` beforehand and never opens a live
        preview at all, or uses `ReplaceOnEditMessage` (the one below) to degrade
        editing into "occasionally post a new message".
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
    """The degraded implementation for the "cannot edit" kind of platform: turn
    an edit into a throttled new message.

    **This is a stated trade-off, not a fix.** On such a platform the live
    preview is only a progress message every `min_interval_sec`, and the final
    answer is a separate message; a caller that does not want this behaviour
    should query `capabilities.edit_message` **before creating the preview** and
    simply not stream. It is placed in the interface so the next person wiring up
    a platform has a ready-made choice with clearly-written behaviour, rather than
    improvising one inside a transport.

    ⚠️ **Throttling must always come with a "flush", or the last edit gets
    eaten.** The caller (`discord_bot._DorossiLiveMessage.finalize`) sends the
    final answer by editing one more time, and if that lands inside the throttle
    window, a pure-throttle version would **silently drop the whole answer** — the
    user sees a progress message stopped halfway, with nothing anywhere saying so.
    So the held-back content is stored and a background task flushes the latest
    version when the window ends. The only cost is the final answer appearing up
    to `min_interval_sec` seconds late.
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
        """Flush the latest version when the window ends. Being cancelled is a
        normal ending, and it never propagates outward."""
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
            # A failed flush is let go: it is the "best effort" layer, and this is
            # a background task, so propagating would only become a context-less
            # 'Task exception was never retrieved'.
            return


def _monotonic() -> float:
    # Extracted into a function so tests can swap it; `time` is used only here.
    import time
    return time.monotonic()


# ---------------------------------------------------------------------------
# Transport and registry
# ---------------------------------------------------------------------------
@dataclass
class TransportContext:
    """Everything needed to build a transport. **Injected by `discord_bot`; the
    reverse would be a circular dependency.**

    `handle_message` is "what to do with a received message" — in practice
    `discord_bot.dispatch_external_message`. The transport does not import the
    bot, and the bot does not need to know any platform's details.
    """

    config: dict = field(default_factory=dict)
    owner_uid: int = 0
    command_channel_id: int = 0
    handle_message: Callable[[ChatMessage], Awaitable[None]] | None = None
    project_root: Any = None
    # Called once when the platform reconnects after a disconnect (to send parked
    # answers). Must not raise, must not block receiving.
    on_recovered: Callable[[], Awaitable[None]] | None = None


class ChatTransport(abc.ABC):
    """A platform's long-lived background loop.

    Its lifecycle is exactly the same as the existing background loops: created
    at startup, revived by `_ensure_background_tasks_alive` on every reconnect,
    and leaving a **named** log line when it dies. So `run()` must be a coroutine
    that "keeps running forever"; its returning on its own means this platform
    stopped.
    """

    name: str = "chat"

    @property
    @abc.abstractmethod
    def capabilities(self) -> PlatformCapabilities:
        """What this platform can do."""

    @abc.abstractmethod
    async def run(self) -> None:
        """The long-lived loop."""

    @abc.abstractmethod
    async def deliver(self, channel: ChatConversation, content: Any,
                      **kwargs) -> SentChatMessage | None:
        """Send a message to `channel`."""

    async def revise(self, sent: SentChatMessage, content: str, **kwargs) -> None:
        """Edit an already-sent message. A platform that cannot edit need not
        implement it (`SentChatMessage.edit` raises `UnsupportedOperation` before
        it ever reaches here)."""
        raise UnsupportedOperation(f"{self.name} cannot edit a sent message")

    async def typing_loop(self, channel: ChatConversation) -> None:
        """The ongoing "is typing" signal. Being cancelled is a normal ending; do
        not swallow the cancellation here."""
        await asyncio.sleep(0)

    def conversation_for(self, platform_chat_id: str) -> ChatConversation | None:
        """Rebuild a conversation from a stored conversation id, for things sent
        after the fact (scheduled reports and the like).

        Returns None = this platform does not support it, or that conversation is
        not somewhere it will reply. **Authorisation is decided here**: the id
        came from disk, and disk cannot decide where the bot speaks — so only
        conversations this platform would reply to anyway are allowed (the allow
        list, the owner's DM). Unsupported by default."""
        del platform_chat_id
        return None

    async def close(self) -> None:
        """Close the resources this transport opened itself. Never raises."""


def origin_of(channel: Any) -> dict:
    """The fields to store for returning to this conversation later:
    `{"platform", "platform_chat_id"}`.

    An existing-platform channel returns an empty dict — that side still stores
    only the integer channel id. Another platform cannot store only an integer: a
    DM id is negative (the existing platform cannot find it), and a conversation
    in the allow list equals `CHANNEL_ID` (which would find the existing
    platform's channel)."""
    if isinstance(channel, ChatConversation):
        return {"platform": channel.transport.name,
                "platform_chat_id": str(channel.platform_chat_id)}
    return {}


def find_conversation(transports: Iterable[ChatTransport],
                      record: Any) -> tuple[bool, ChatConversation | None]:
    """A record stored by `origin_of` → `(whether this is another platform's
    record, the conversation or None)`.

    The first value lets the caller tell two things apart: "not another
    platform's record" takes the existing platform as before; "yes, but cannot be
    recovered" (the platform is off, the conversation is not authorised) **must
    not** fall back to the existing platform's channel — which is exactly the
    "sent to the wrong place" this function fixes. Never raises."""
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
    """Register a platform. A `factory` returning `None` means "not configured" —
    **and that must be silent**.

    "Not configured" and "misconfigured" are two different things: the former is
    the norm for almost everyone (one repo does not wire up four platforms at
    once), and complaining once on every startup is the next source of log-washing
    noise; the latter is what should make a sound, and the factory says clearly at
    that moment which key it was.
    """
    _FACTORIES[name] = factory


def registered_transports() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def import_transport_modules() -> None:
    """Import the modules listed in `TRANSPORT_MODULES` (registration is a side
    effect of the import).

    Both import shapes are tried, for exactly the same reason as `_bot_config`'s
    passage on `_warn_dedup`: running `discord_bot.py` directly puts `axiomatic/`
    itself on `sys.path` (the bare name holds), while the package path does not
    (only `axiomatic.x` holds). Writing only one would blow up the whole import on
    the other path, while the side taken most often locally stays green.
    """
    for name in TRANSPORT_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            importlib.import_module(f"axiomatic.{name}")


def build_transports(context: TransportContext) -> list[ChatTransport]:
    """Build every transport that "is configured and enabled". Unconfigured
    platforms are silently absent.

    One factory blowing up must not drag down the other platforms, nor the bot's
    startup — the same rule `_ensure_background_tasks_alive` sets for background
    loops (each one tries on its own, a failure leaves one named log line, and the
    next reconnect tries again).
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
