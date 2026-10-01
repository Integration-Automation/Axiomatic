"""對話平台回覆的卡片版面（bot-only helper，2026-10-01 起）。

bot 的回覆原本幾乎全是純文字，少數幾個嵌入訊息各自決定顏色、各自用英文或中文的欄位名。
這裡把「一張卡片長什麼樣子」收成一個地方：

* **狀態決定顏色**：`ok`／`warn`／`bad`／`info`／`idle` 五種，呼叫端講狀態，不講色碼。
* **平台的長度上限在這裡一次處理**：標題 256、描述 4096、最多 25 個欄位、欄位名 256、欄位值
  1024、頁尾 2048、整張 6000 字元。超過的截斷並加「…」；放不下的欄位不送，**頁尾講還有幾項沒顯示**
  ——平台對超過上限的嵌入訊息是整則拒收，那正是「安靜地什麼都沒發生」。
* **沒有卡片的平台不必另寫一份**：`_chat_platform.flatten_embed` 會把同一張卡片攤成純文字。

不 import `discord_bot`（循環）；`discord` 本身可以——這是 bot-only 的模組。
"""
from __future__ import annotations

import re
from typing import Callable, Iterable

import discord

STATUS_COLORS = {
    "ok": 0x57F287,
    "warn": 0xFEE75C,
    "bad": 0xED4245,
    "info": 0x5865F2,
    "idle": 0x99AAB5,
}
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FIELD_LIMIT = 25
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
FOOTER_LIMIT = 2048
TOTAL_LIMIT = 6000
# 頁尾一定要留得下「另有幾項放不下」那一句，所以欄位用到總量的這麼多就停。
_FOOTER_RESERVE = 200
_EMPTY = "​"   # 平台不收空字串的欄位名／值，用零寬空白佔位


def clip(text, limit: int) -> str:
    """把 `text` 截到 `limit` 個字元以內，截掉時以「…」結尾。"""
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + "…"


def _rows(fields: Iterable) -> list[tuple[str, str, bool]]:
    out = []
    for row in fields or ():
        name, value, *rest = row
        out.append((str(name or "").strip() or _EMPTY,
                    str(value or "").strip() or "—",
                    bool(rest[0]) if rest else False))
    return out


def card(title: str, *, status: str = "info", description: str | None = None,
         fields: Iterable = (), footer: str | None = None, overflow: str = "clip",
         omitted: Callable[[int], str] | None = None) -> discord.Embed:
    """一張卡片。`fields` 是 `(名稱, 值)` 或 `(名稱, 值, 同一列)` 的序列，依序放。

    放不下的欄位（超過 25 個，或整張會超過 6000 字元）不送，頁尾補一句還有幾項沒顯示
    （`omitted(數量)` 給了就用它的句子，每個指令可以講自己的下一步）。`overflow="drop"` 時，
    名稱或值本身超過上限的欄位也是整項不送、算進那個數字，而不是截斷——給「寧可少一項、
    不要半項」的報告用（`/sys health`）。認不得的 `status` 當成 `info`。
    """
    embed = discord.Embed(title=clip(title, TITLE_LIMIT),
                          color=STATUS_COLORS.get(status, STATUS_COLORS["info"]))
    budget = TOTAL_LIMIT - len(embed.title or "") - _FOOTER_RESERVE
    if description:
        text = clip(description, min(DESCRIPTION_LIMIT, max(1, budget)))
        embed.description = text
        budget -= len(text)
    dropped = 0
    for name, value, inline in _rows(fields):
        if overflow == "drop" and (len(name) > FIELD_NAME_LIMIT or len(value) > FIELD_VALUE_LIMIT):
            dropped += 1
            continue
        name = clip(name, FIELD_NAME_LIMIT)
        value = clip(value, FIELD_VALUE_LIMIT)
        if len(embed.fields) >= FIELD_LIMIT or len(name) + len(value) > budget:
            dropped += 1
            continue
        embed.add_field(name=name, value=value, inline=inline)
        budget -= len(name) + len(value)
    note = None
    if dropped:
        note = omitted(dropped) if omitted else f"另有 {dropped} 項放不下，沒有顯示。"
    parts = [part for part in (footer, note) if part]
    if parts:
        room = budget + _FOOTER_RESERVE
        embed.set_footer(text=clip(" ".join(parts), max(1, min(FOOTER_LIMIT, room))))
    return embed


# `- **名稱**: 值` 一行一項的條列報告（`/sys health` 那種）。
_BULLET_RE = re.compile(r"^- \*\*(?P<name>[^*]+)\*\*: ?(?P<value>.*)$")
# 狀態標記 → 卡片狀態，由重到輕。`(err` 是各段落「這一段算不出來」的固定寫法。
_SEVERITY = (("bad", ("❌", "(err")), ("warn", ("⚠️", "❓")))


def bullets_status(lines: Iterable[str]) -> str:
    """一份條列報告的整體狀態：有 `❌`／`(err` 是 `bad`，有 `⚠️`／`❓` 是 `warn`，否則 `ok`。"""
    text = "\n".join(lines)
    for status, markers in _SEVERITY:
        if any(marker in text for marker in markers):
            return status
    return "ok"


def from_bullets(title: str, lines: Iterable[str], *, footer: str | None = None,
                 omitted: Callable[[int], str] | None = None) -> discord.Embed:
    """把 `- **名稱**: 值` 的條列報告排成卡片：每項一個欄位、其餘行放描述，狀態由標記決定。

    放不下的項目整項不送（`overflow="drop"`），由 `omitted` 講還有幾項——這種報告「少一項、
    說清楚少了」比「送出半項」好，原本的文字版就是這條規則。
    """
    lines = list(lines)
    fields, other = [], []
    for line in lines:
        match = _BULLET_RE.match(line)
        if match:
            fields.append((match["name"].strip(), match["value"].strip()))
        elif line.strip():
            other.append(line)
    return card(title, status=bullets_status(lines), description="\n".join(other) or None,
                fields=fields, footer=footer, overflow="drop", omitted=omitted)
