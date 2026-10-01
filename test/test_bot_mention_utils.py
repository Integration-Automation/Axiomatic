"""公開的小工具指令。

`/fun reverse`、`/tool base64`、`/tool unbase64`、`/tool urlencode`、`/tool urldecode`、
`/tool qr`：把使用者給的文字（或由它算出來的文字）原樣送回去的指令都要待在單則訊息的上限內，
截短共用 `_clip_text`；QR code 的連結超過上限是拒絕，不是截短。

圖庫那幾支（`/tag autocomplete`、`/safebooru`、`/e621`）讀的是外部回應：欄位型別不對時回
泛用句、stderr 只記欄位與型別。網路一律換成替身，一個請求都不送。
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



# ---------------------------------------------------------------------------
# 圖庫：回應是外部資料，欄位型別不對時回泛用句
# ---------------------------------------------------------------------------
@pytest.fixture
def tag_query(monkeypatch):
    state = types.SimpleNamespace(calls=[], hits=[])

    async def _query(api, **kwargs):
        state.calls.append((api, kwargs))
        return state.hits

    monkeypatch.setattr(b, "_query_tags_json", _query)
    return state



def test_autocomplete_lists_hits_with_their_category(tag_query, monkeypatch):
    tag_query.hits = [{"name": "lappland_(arknights)", "category": 4, "post_count": 900},
                      {"name": "lappland", "category": 9},
                      {"name": "x", "category": 1, "post_count": 3}]
    sent = _replies(monkeypatch)
    _run(b.mcmd_autocomplete(_STRANGER, "lapp"))
    assert _texts(sent) == ["**`lapp*`** tag 補全（top 3）：\n"
                            "- `lappland_(arknights)` (character, 900 posts)\n"
                            "- `lappland` (?, 0 posts)\n"
                            "- `x` (artist, 3 posts)"]


def test_autocomplete_without_a_prefix_queries_nothing(tag_query, monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_autocomplete(_STRANGER, "  "))
    assert tag_query.calls == [] and _texts(sent)[0].startswith("usage: `/tag autocomplete")


def test_autocomplete_skips_a_tag_without_a_usable_name(tag_query, monkeypatch, capsys):
    """站方回的 tag 物件少了 `name`（或不是字串）時跳過那一筆，分類、張數不是整數就當不知道
    （2026-10-01 修：原本 `t['name']` 丟 KeyError、分類是清單時 dict 查表丟 TypeError）。"""
    tag_query.hits = [{"post_count": 5, "category": 4}, {"name": 7, "category": 1},
                      {"name": "ok", "category": [0], "post_count": "12"},
                      {"name": "fine", "category": 3, "post_count": 8}]
    sent = _replies(monkeypatch)
    _run(b.mcmd_autocomplete(_STRANGER, "o"))
    assert _texts(sent) == ["**`o*`** tag 補全（top 2）：\n- `ok` (?, 0 posts)\n"
                            "- `fine` (copyright, 8 posts)"]
    assert "skipped 2 tag record(s)" in capsys.readouterr().err


def test_autocomplete_with_only_unusable_tags_fails_generically(tag_query, monkeypatch, capsys):
    """全部都不能用不是「找不到」——找不到只能講在站方說沒有的時候。"""
    tag_query.hits = [{"post_count": 5}, {"name": ""}]
    sent = _replies(monkeypatch)
    _run(b.mcmd_autocomplete(_STRANGER, "o"))
    assert _texts(sent) == [b._BOARD_LOOKUP_FAILED]
    assert "skipped 2 tag record(s)" in capsys.readouterr().err


@pytest.fixture
def safebooru(monkeypatch):
    state = types.SimpleNamespace(post=None, asked=[])

    async def _fetch(tags):
        state.asked.append(tags)
        return state.post

    monkeypatch.setattr(b, "_fetch_safebooru_post", _fetch)
    return state




@pytest.mark.parametrize("post,reply", [
    (None, "找不到符合 `cat` 的圖片"),
    ({"id": 7}, "post 7 has no fetchable URL"),
    ({"file_url": "https://img/x.jpg"}, "https://img/x.jpg"),
])
def test_safebooru_explains_a_missing_post_or_link(safebooru, monkeypatch, post, reply):
    safebooru.post = post
    sent = _replies(monkeypatch)
    _run(b.mcmd_safebooru(_STRANGER, "cat"))
    assert _texts(sent) == [reply]


def test_safebooru_without_tags_fetches_nothing(safebooru, monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_safebooru(_STRANGER, ""))
    assert _texts(sent) == ["usage: `/safebooru <tag>`"] and safebooru.asked == []


@pytest.fixture
def e621(monkeypatch):
    state = types.SimpleNamespace(posts={}, asked=[], resolved=None, resolver_calls=[])

    async def _fetch(tags):
        state.asked.append(tags)
        return state.posts.get(tags)

    async def _resolve(api, tags):
        state.resolver_calls.append((api, tags))
        return state.resolved

    monkeypatch.setattr(b, "_fetch_e621_post", _fetch)
    monkeypatch.setattr(b, "_resolve_fuzzy_tags", _resolve)
    return state




def test_e621_with_a_locked_post_says_it_has_no_link(e621, monkeypatch):
    e621.posts = {"wolf": {"id": 4, "file": {"url": None}}}
    sent = _replies(monkeypatch)
    _run(b.mcmd_e621(_STRANGER, "wolf"))
    assert _texts(sent) == ["post 4 no fetchable URL（rating-locked 或匿名看不到）"]
    _run(b.mcmd_e621(_STRANGER, ""))
    assert _texts(sent)[-1].startswith("usage: `/e621 <tag>`")


@pytest.mark.parametrize("handler,post,field,kind", [
    ("e621", {"id": 1, "file": ["not", "a", "dict"]}, "file", "list"),
    ("e621", {"id": 1, "file": {"url": 12345}}, "file.url", "int"),
    ("safebooru", {"id": 2, "file_url": {"u": "D:/secret"}}, "file_url", "dict"),
])
def test_a_malformed_post_gets_the_generic_failure(e621, safebooru, monkeypatch, capsys,
                                                    handler, post, field, kind):
    """欄位型別不對（2026-10-01 修，原本丟 AttributeError／TypeError）：使用者拿泛用句，
    stderr 只記指令、欄位與型別名——不記內容，stderr 會經 `/log tail` 進頻道。"""
    e621.posts = {"x": post}
    safebooru.post = post
    sent = _replies(monkeypatch)
    _run(getattr(b, f"mcmd_{handler}")(_STRANGER, "x"))
    assert _texts(sent) == [b._BOARD_LOOKUP_FAILED]
    err = capsys.readouterr().err
    assert f"/{handler}: post field `{field}` is {kind}" in err
    assert "secret" not in err and "12345" not in err


@pytest.mark.parametrize("handler,post,reply", [
    ("e621", {"id": 4}, "post 4 no fetchable URL（rating-locked 或匿名看不到）"),
    ("e621", {"id": "4", "file": {"url": None}}, "post ? no fetchable URL（rating-locked 或匿名看不到）"),
    ("safebooru", {"id": [7], "file_url": None}, "post ? has no fetchable URL"),
])
def test_a_missing_link_is_not_a_malformed_post(e621, safebooru, monkeypatch, handler, post,
                                                reply):
    """連結是 null 或整個沒有是站方的正常回應（分級鎖住、匿名看不到），照原本那句講；post 編號
    不是整數時寫 `?`，不把不受控的值送出去。"""
    e621.posts = {"x": post}
    safebooru.post = post
    sent = _replies(monkeypatch)
    _run(getattr(b, f"mcmd_{handler}")(_STRANGER, "x"))
    assert _texts(sent) == [reply]

