"""分頁的卡片回覆：一頁一張卡片，有按鈕的平台用 ◀ ▶ 翻頁。

bot-only helper（與 `_reply_card` 同一類）：**不得 import `discord_bot`**。真正送出訊息的
那一步由呼叫端注入（`send_paged(reply, …)`），所以這裡只決定「分成哪幾頁、按鈕怎麼動」，
不決定訊息怎麼送——錯誤處理、引用失效的退路、平台差異都留在呼叫端的 `safe_reply`。

三條規則（總覽見 `architecture.md`）：
  * **只有下指令的人能翻頁。** 別人按會收到一則只有他看得到的提示，訊息不動。
  * **逾時就把按鈕停用**（`PAGE_TIMEOUT_SEC`），不留一排按了沒反應的按鈕。
  * **沒有按鈕的平台把每一頁依序送出**（上限 `MAX_PAGES`），而不是只送第一頁——
    那些平台不認得 `view=`，會安靜地丟掉它，使用者就只看得到第一頁還不知道後面有東西。

改到這裡之前要知道的三件事：
  * **呼叫端不要自己傳 `view=`**，一律走 `send_paged`；判斷有沒有按鈕的是
    `PlatformCapabilities.buttons`，每個 transport 都明寫（`test_platform_transports` 守）。
  * **元件互動不經 `tree.interaction_check`**，所以 `PagerView.interaction_check` 是唯一的閘。
    翻頁沒關係——它只是重新顯示已經送出的內容；哪天加了**會做事**的按鈕（確認、刪除），
    callback 裡要重新套擁有者閘與角色閘。
  * 測試替身：`types.SimpleNamespace()` 當訊息時沒有 `author`，走的是逐頁送出、每頁一則。
    按鈕在 discord.py 2.7 是實例上的 `Button`，測試直接
    `await view.next_page.callback(interaction)`；`interaction_check` 由派發器呼叫、要單獨測。
"""
from __future__ import annotations

import sys
from typing import Any, Awaitable, Callable

import discord

PAGE_TIMEOUT_SEC = 300.0
MAX_PAGES = 25
NOT_YOURS = "只有下這個指令的人可以翻頁。"
_FOOTER_LIMIT = 2048


def paginate_rows(rows: list[str], *, per_page: int, max_chars: int) -> list[list[str]]:
    """把列切成頁：每頁最多 `per_page` 列、合計最多 `max_chars` 字（含換行）。

    單一列本身就超過 `max_chars` 時截短並加 `…`，所以每一頁一定放得下至少一列；
    空清單回空清單。頁數不在這裡設上限——要不要截、截了怎麼講是呼叫端的事。
    """
    if per_page < 1 or max_chars < 2:
        raise ValueError("per_page must be >= 1 and max_chars >= 2")
    pages: list[list[str]] = []
    current: list[str] = []
    size = 0
    for row in rows:
        if len(row) > max_chars:
            row = row[:max_chars - 1] + "…"
        cost = len(row) + 1
        if current and (len(current) >= per_page or size + cost > max_chars):
            pages.append(current)
            current, size = [], 0
        current.append(row)
        size += cost
    if current:
        pages.append(current)
    return pages


def number_pages(pages: list[discord.Embed], *, dropped: int = 0) -> list[discord.Embed]:
    """在每張卡片的頁尾補上「第 i/n 頁」（原本的頁尾保留在前面）；只有一頁時不動。

    `dropped` 是超過上限、沒送出的頁數：最後一頁的標籤會講出來，不讓截斷是安靜的。
    """
    total = len(pages)
    if total <= 1 and not dropped:
        return pages
    for index, page in enumerate(pages, 1):
        label = f"第 {index}/{total} 頁"
        if dropped and index == total:
            label += f"（後面還有 {dropped} 頁沒列出）"
        existing = page.footer.text if page.footer and page.footer.text else ""
        text = f"{existing} · {label}" if existing else label
        if len(text) > _FOOTER_LIMIT:
            keep = _FOOTER_LIMIT - len(label) - 4
            text = f"{existing[:keep]}… · {label}"
        page.set_footer(text=text)
    return pages


def has_buttons(message: Any) -> bool:
    """這則訊息所在的平台能不能放按鈕。

    介接層的對話（`_chat_platform.ChatConversation`）帶 `capabilities`，照它的 `buttons`；
    沒有 `capabilities` 的就是有斜線選單的那個原生平台，它有按鈕。
    """
    capabilities = getattr(getattr(message, "channel", None), "capabilities", None)
    if capabilities is None:
        return True
    return bool(getattr(capabilities, "buttons", False))


class PagerView(discord.ui.View):
    """◀ ／頁碼／▶ 三顆按鈕。頁碼那顆停用，只顯示目前在第幾頁。

    `message` 由呼叫端在送出之後設上；逾時要靠它把按鈕停用。設不上（送出失敗、平台沒回
    訊息物件）時逾時就只是不動，不會丟例外。
    """

    def __init__(self, pages: list[discord.Embed], *, owner_id: int,
                 timeout: float = PAGE_TIMEOUT_SEC) -> None:
        super().__init__(timeout=timeout)
        if len(pages) < 2:
            raise ValueError("a pager needs at least two pages")
        self.pages = pages
        self.owner_id = owner_id
        self.index = 0
        self.message: Any = None
        self._sync()

    def _sync(self) -> None:
        self.previous_page.disabled = self.index == 0
        self.next_page.disabled = self.index >= len(self.pages) - 1
        self.position.label = f"{self.index + 1} / {len(self.pages)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if getattr(interaction.user, "id", None) == self.owner_id:
            return True
        await interaction.response.send_message(NOT_YOURS, ephemeral=True)
        return False

    async def _show(self, interaction: discord.Interaction, index: int) -> None:
        self.index = max(0, min(index, len(self.pages) - 1))
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction,
                            _button: discord.ui.Button) -> None:
        await self._show(interaction, self.index - 1)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def position(self, interaction: discord.Interaction,
                       _button: discord.ui.Button) -> None:
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction,
                        _button: discord.ui.Button) -> None:
        await self._show(interaction, self.index + 1)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException as error:
            # 訊息已經被刪掉、或權限沒了：按鈕本來就按不到了，記一行就好。
            print(f"[pager] could not disable buttons: {error!r}", file=sys.stderr)


Reply = Callable[..., Awaitable[Any]]


async def send_paged(reply: Reply, message: Any, pages: list[discord.Embed]) -> None:
    """送出分頁的卡片。`reply(message, **kwargs)` 是呼叫端的安全送出（`safe_reply`）。

    一頁 → 直接送那張卡片。多頁而且平台有按鈕、取得到發起人 → 一則訊息加翻頁按鈕。
    其他情況（沒有按鈕的平台、取不到發起人）→ 每一頁依序送出，最多 `MAX_PAGES` 則；
    取不到發起人時不放按鈕是 fail-closed：綁不了人的按鈕等於誰都能按。
    """
    if not pages:
        raise ValueError("send_paged needs at least one page")
    pages = number_pages(pages[:MAX_PAGES], dropped=max(0, len(pages) - MAX_PAGES))
    if len(pages) == 1:
        await reply(message, embed=pages[0])
        return
    owner_id = getattr(getattr(message, "author", None), "id", None)
    # bool 是 int 的子類別，`True` 會穿過 isinstance(…, int)，所以另外排除。
    if (not has_buttons(message) or not isinstance(owner_id, int)
            or isinstance(owner_id, bool)):
        for page in pages:
            await reply(message, embed=page)
        return
    view = PagerView(pages, owner_id=owner_id)
    view.message = await reply(message, embed=pages[0], view=view)
