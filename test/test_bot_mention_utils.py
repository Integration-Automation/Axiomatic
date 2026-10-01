"""公開的小工具指令。

`/fun reverse`、`/tool base64`、`/tool unbase64`、`/tool urlencode`、`/tool urldecode`、
`/tool qr`：把使用者給的文字（或由它算出來的文字）原樣送回去的指令都要待在單則訊息的上限內，
截短共用 `_clip_text`；QR code 的連結超過上限是拒絕，不是截短。
"""
from __future__ import annotations

import asyncio
import base64
import types

import pytest

import discord_bot as b


def _run(coro, timeout: float = 10.0):
    """跑一段 async 本體；有牆鐘上限——回歸時要變紅，不能掛住。"""
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _replies(monkeypatch) -> list:
    """把 `safe_reply` 換成記錄器，回 `[(content, kwargs), …]`。"""
    sent: list = []

    async def _reply(_message, content=None, **kwargs):
        sent.append((content, kwargs))

    monkeypatch.setattr(b, "safe_reply", _reply)
    return sent


def _texts(sent) -> list:
    return [content for content, _kw in sent]


_STRANGER = types.SimpleNamespace(author=types.SimpleNamespace(id=12345))



def test_base64_round_trips_through_unbase64(monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_base64(_STRANGER, "你好"))
    encoded = base64.b64encode("你好".encode("utf-8")).decode("ascii")
    assert _texts(sent) == [f"```\n{encoded}\n```"]
    _run(b.mcmd_unbase64(_STRANGER, encoded))
    assert _texts(sent)[-1] == "```\n你好\n```"


def test_base64_caps_a_long_output(monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_base64(_STRANGER, "x" * 3000))
    reply = _texts(sent)[-1]
    assert reply.endswith("…\n```") and len(reply) == len("```\n") + 1800 + len("…\n```")


def test_unbase64_keeps_the_fence_closed_and_caps_the_output(monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_unbase64(_STRANGER, base64.b64encode(b"a```b").decode()))
    assert _texts(sent)[-1] == "```\naʼʼʼb\n```"
    _run(b.mcmd_unbase64(_STRANGER, base64.b64encode(b"y" * 3000).decode()))
    assert _texts(sent)[-1] == "```\n" + "y" * 1800 + "…\n```"



def test_urlencode_encodes_every_reserved_character(monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_urlencode(_STRANGER, "a b/c?d=你"))
    _run(b.mcmd_urldecode(_STRANGER, "a%20b%2Fc%3Fd%3D%E4%BD%A0"))
    _run(b.mcmd_urlencode(_STRANGER, ""))
    _run(b.mcmd_urldecode(_STRANGER, ""))
    assert _texts(sent) == ["`a%20b%2Fc%3Fd%3D%E4%BD%A0`", "`a b/c?d=你`",
                            "usage: `/tool urlencode <text>`",
                            "usage: `/tool urldecode <text>`"]


def test_qr_puts_the_encoded_text_into_the_image_link(monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_qr(_STRANGER, "  hello world&x=1  "))
    link = _texts(sent)[-1]
    assert link.startswith("https://") and "size=300x300" in link
    assert link.endswith("&data=hello%20world%26x%3D1"), link
    _run(b.mcmd_qr(_STRANGER, "   "))
    assert _texts(sent)[-1] == "usage: `/tool qr <text>`"


@pytest.mark.parametrize("handler,expected", [
    ("mcmd_reverse", "b" * 1900 + "…"),
    ("mcmd_urlencode", "`" + "b" * 1900 + "…`"),
    ("mcmd_urldecode", "`" + "b" * 1900 + "…`"),
])
def test_a_long_input_is_clipped_to_fit_in_one_message(monkeypatch, handler, expected):
    """斜線選項最多收 6000 字，原樣送回去會超過單則上限、整則被拒（2026-10-01 修）。
    截短用的是 base64／unbase64／say 同一支 `_clip_text`。"""
    sent = _replies(monkeypatch)
    _run(getattr(b, handler)(_STRANGER, "b" * 3000))
    assert _texts(sent) == [expected]


@pytest.mark.parametrize("handler", ["mcmd_reverse", "mcmd_urlencode", "mcmd_urldecode"])
def test_an_input_at_the_limit_is_not_clipped(monkeypatch, handler):
    sent = _replies(monkeypatch)
    _run(getattr(b, handler)(_STRANGER, "c" * 1900))
    assert "…" not in _texts(sent)[-1] and "c" * 1900 in _texts(sent)[-1]


def test_qr_refuses_text_that_would_not_fit_instead_of_clipping_it(monkeypatch):
    """截短 QR code 的內容會讓它安靜地編進一段不完整的文字，所以超過上限是拒絕，不是截短。
    上限算的是整條連結（編碼後）：剛好等於上限照常送出。"""
    sent = _replies(monkeypatch)
    prefix = len("https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=")
    _run(b.mcmd_qr(_STRANGER, "d" * (b.QR_LINK_MAX - prefix)))
    assert len(_texts(sent)[-1]) == b.QR_LINK_MAX and _texts(sent)[-1].startswith("https://")
    _run(b.mcmd_qr(_STRANGER, "d" * (b.QR_LINK_MAX - prefix + 1)))
    reply = _texts(sent)[-1]
    assert reply.startswith("text too long for a QR code link") and "https://" not in reply
    assert f"the limit is {b.QR_LINK_MAX}" in reply

