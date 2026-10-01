"""公開的小工具指令。

`/fun …`、`/tool …`、`/info …` 與圖庫那一組（`/booru`、`/nsfw`、`/grid`、`/safebooru`、
`/e621`、`/iqdb`、`/tag autocomplete`）。它們是**公開**指令（不看頻道、不看角色），所以
回覆裡出現的每一個字都是任何伺服器的任何人看得到的：失敗一律泛用句、原始例外只給擁有者。

隨機數換成記錄器（斷言「從哪些選項裡挑」，不是賭一個隨機結果）；網路一律換成替身——
圖庫的抓取函式、反查圖、tag 查詢——一個請求都不送。斷言刻意不寫死外部站台的網域：
那是功能本身帶的資料，不是這裡要守的行為。
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import re
import time
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


def _owner():
    return types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))


class _Rng:
    """`random` 的替身：記下每一次從哪些選項裡挑，並照設定回傳。"""

    def __init__(self, pick=lambda seq: seq[0], number=lambda lo, hi: lo) -> None:
        self.choices: list = []
        self.ranges: list = []
        self._pick = pick
        self._number = number

    def choice(self, seq):
        self.choices.append(list(seq))
        return self._pick(seq)

    def randint(self, lo, hi):
        self.ranges.append((lo, hi))
        return self._number(lo, hi)


@pytest.fixture
def rng(monkeypatch):
    fake = _Rng()
    monkeypatch.setattr(b, "random", fake)
    return fake


# ---------------------------------------------------------------------------
# /fun
# ---------------------------------------------------------------------------
def test_rand_swaps_a_reversed_range(rng, monkeypatch):
    rng._number = lambda lo, hi: hi
    sent = _replies(monkeypatch)
    _run(b.mcmd_rand(_STRANGER, "9 3"))
    assert rng.ranges == [(3, 9)]
    assert _texts(sent) == ["🎲 9 (range 3..9)"]


@pytest.mark.parametrize("rest,reply", [
    ("5", "usage: `/fun rand <min> <max>`"),
    ("1 2 3", "usage: `/fun rand <min> <max>`"),
    ("a 2", "min / max must be integers"),
    ("1 " + "9" * 5000, "min / max must be integers"),
])
def test_rand_rejects_bad_bounds(rng, monkeypatch, rest, reply):
    sent = _replies(monkeypatch)
    _run(b.mcmd_rand(_STRANGER, rest))
    assert _texts(sent) == [reply] and rng.ranges == []


@pytest.mark.parametrize("move,bot,outcome", [
    ("rock", "scissors", "you win"), ("R", "paper", "you lose"),
    ("p", "rock", "you win"), ("scissors", "rock", "you lose"),
    ("  Paper ", "paper", "draw"), ("s", "paper", "you win"),
])
def test_rps_decides_every_outcome(rng, monkeypatch, move, bot, outcome):
    rng._pick = lambda seq: bot
    sent = _replies(monkeypatch)
    _run(b.mcmd_rps(_STRANGER, move))
    user = {"r": "rock", "p": "paper", "s": "scissors"}.get(move.strip().lower(),
                                                           move.strip().lower())
    assert _texts(sent) == [f"you: **{user}** vs bot: **{bot}** → **{outcome}**"]
    assert rng.choices == [["rock", "paper", "scissors"]]


def test_rps_rejects_an_unknown_move(rng, monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_rps(_STRANGER, "lizard"))
    assert _texts(sent)[0].startswith("usage: `/fun rps") and rng.choices == []


def test_choose_picks_among_the_non_empty_options(rng, monkeypatch):
    rng._pick = lambda seq: seq[-1]
    sent = _replies(monkeypatch)
    _run(b.mcmd_choose(_STRANGER, " A | | B b |C "))
    assert rng.choices == [["A", "B b", "C"]]
    assert _texts(sent) == ["🎯 **C**"]


@pytest.mark.parametrize("rest", ["", "only", "A | ", " | | "])
def test_choose_needs_two_options(rng, monkeypatch, rest):
    sent = _replies(monkeypatch)
    _run(b.mcmd_choose(_STRANGER, rest))
    assert _texts(sent) == ["usage: `/fun choose A | B | C`"] and rng.choices == []


def test_8ball_answers_from_its_own_list(rng, monkeypatch):
    rng._pick = lambda seq: seq[3]
    sent = _replies(monkeypatch)
    _run(b.mcmd_8ball(_STRANGER, "will it work?"))
    assert rng.choices == [b._8BALL_RESPONSES]
    assert _texts(sent) == [f"🎱 {b._8BALL_RESPONSES[3]}"]
    _run(b.mcmd_8ball(_STRANGER, "   "))
    assert _texts(sent)[-1] == "usage: `/fun 8ball <question>`"


def test_coinflip_picks_heads_or_tails(rng, monkeypatch):
    rng._pick = lambda seq: seq[1]
    sent = _replies(monkeypatch)
    _run(b.mcmd_coinflip(_STRANGER))
    assert rng.choices == [["heads", "tails"]]
    assert _texts(sent) == ["🪙 **tails**"]


def test_reverse_reverses_and_needs_text(monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_reverse(_STRANGER, "abc 你好"))
    _run(b.mcmd_reverse(_STRANGER, ""))
    assert _texts(sent) == ["好你 cba", "usage: `/fun reverse <text>`"]


class _FakeFiglet:
    def __init__(self, raises=None) -> None:
        self.seen: list = []
        self._raises = raises

    def figlet_format(self, text):
        self.seen.append(text)
        if self._raises is not None:
            raise self._raises
        return f"<<{text}>> ``` tail"


def test_ascii_caps_the_input_and_keeps_the_fence_closed(monkeypatch):
    fig = _FakeFiglet()
    monkeypatch.setattr(b, "pyfiglet", fig)
    sent = _replies(monkeypatch)
    _run(b.mcmd_ascii(_STRANGER, "x" * 100))
    assert fig.seen == ["x" * 60]
    assert _texts(sent) == [f"```\n<<{'x' * 60}>> ʼʼʼ tail\n```"]


def test_ascii_without_the_library_or_text_explains(monkeypatch):
    sent = _replies(monkeypatch)
    monkeypatch.setattr(b, "pyfiglet", None)
    _run(b.mcmd_ascii(_STRANGER, "hi"))
    monkeypatch.setattr(b, "pyfiglet", _FakeFiglet())
    _run(b.mcmd_ascii(_STRANGER, ""))
    assert _texts(sent) == ["ASCII art 功能未安裝相依套件，暫時無法使用。",
                            "usage: `/fun ascii <text>`"]


def test_ascii_failure_shows_the_raw_error_only_to_the_owner(monkeypatch, capsys):
    monkeypatch.setattr(b, "pyfiglet", _FakeFiglet(raises=ValueError("font D:/x missing")))
    sent = _replies(monkeypatch)
    _run(b.mcmd_ascii(_STRANGER, "hi"))
    _run(b.mcmd_ascii(_owner(), "hi"))
    assert _texts(sent) == ["ascii render failed", "ValueError: font D:/x missing"]
    assert "ascii render failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# /tool
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,hex_", [("ff8800", "ff8800"), ("#F80", "ff8800"),
                                      ("  0a0B0c ", "0a0b0c")])
def test_color_builds_a_swatch_card(monkeypatch, raw, hex_):
    sent = _replies(monkeypatch)
    _run(b.mcmd_color(_STRANGER, raw))
    content, kwargs = sent[-1]
    embed = kwargs["embed"]
    assert content is None
    assert embed.title == f"#{hex_.upper()}"
    assert embed.color.value == int(hex_, 16)
    assert embed.image.url.endswith(f"/get/{hex_}/200x200")
    r, g, b_ = (int(hex_[i:i + 2], 16) for i in (0, 2, 4))
    fields = {f.name: f.value for f in embed.fields}
    assert fields == {"RGB": f"`rgb({r}, {g}, {b_})`", "hex": f"`#{hex_.upper()}`"}


@pytest.mark.parametrize("raw", ["", "ggg", "12345", "#1234567", "f8"])
def test_color_rejects_anything_but_three_or_six_hex_digits(monkeypatch, raw):
    sent = _replies(monkeypatch)
    _run(b.mcmd_color(_STRANGER, raw))
    assert _texts(sent) == ["usage: `/tool color <hex>` — 3 or 6 hex digits"]


def test_hash_prints_all_three_digests(monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_hash(_STRANGER, "你好 world"))
    data = "你好 world".encode("utf-8")
    assert _texts(sent) == [
        "```\n"
        f"md5    {hashlib.md5(data, usedforsecurity=False).hexdigest()}\n"
        f"sha1   {hashlib.sha1(data, usedforsecurity=False).hexdigest()}\n"
        f"sha256 {hashlib.sha256(data).hexdigest()}\n"
        "```"]
    _run(b.mcmd_hash(_STRANGER, ""))
    assert _texts(sent)[-1] == "usage: `/tool hash <text>`"


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


@pytest.mark.parametrize("rest", ["abc", "你好"])
def test_unbase64_failure_shows_the_raw_error_only_to_the_owner(monkeypatch, rest):
    """壞的補位（`abc`）與非 ASCII 輸入（`你好`）都會丟例外；回覆不得帶原始訊息。"""
    sent = _replies(monkeypatch)
    _run(b.mcmd_unbase64(_STRANGER, rest))
    _run(b.mcmd_unbase64(_owner(), rest))
    stranger, owner = _texts(sent)
    assert stranger == "decode failed"
    assert re.match(r"(Error|UnicodeEncodeError): ", owner), owner


def test_unbase64_needs_text(monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_unbase64(_STRANGER, ""))
    assert _texts(sent) == ["usage: `/tool unbase64 <base64>`"]


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
# /tool say
# ---------------------------------------------------------------------------
class _Channel:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


def _say_message(author_id: int, guild_owner: int | None):
    guild = None if guild_owner is None else types.SimpleNamespace(owner_id=guild_owner)
    return types.SimpleNamespace(author=types.SimpleNamespace(id=author_id), guild=guild,
                                 channel=_Channel())


def test_say_speaks_for_the_server_owner_with_every_mention_suppressed(monkeypatch):
    """就算是伺服器擁有者，代發的訊息也不得變成 `@everyone` 的轉送器。"""
    sent = _replies(monkeypatch)
    message = _say_message(77, 77)
    _run(b.mcmd_say(message, "  hi @everyone " + "x" * 2000))
    assert sent == []
    (content, kwargs), = message.channel.sent
    assert content == ("hi @everyone " + "x" * 2000)[:1900] + "…"
    mentions = kwargs["allowed_mentions"]
    assert (mentions.everyone, mentions.users, mentions.roles, mentions.replied_user) == (
        False, False, False, False)


@pytest.mark.parametrize("author,owner", [(5, 77), (77, None)])
def test_say_refuses_anyone_but_the_server_owner(monkeypatch, author, owner):
    """拒絕之前不得發出任何東西——否則任何人都能借 bot 的名義說話。"""
    sent = _replies(monkeypatch)
    message = _say_message(author, owner)
    _run(b.mcmd_say(message, "hello"))
    assert _texts(sent) == ["`/tool say` is restricted to the server owner"]
    assert message.channel.sent == []


def test_say_without_text_shows_usage(monkeypatch):
    sent = _replies(monkeypatch)
    message = _say_message(77, 77)
    _run(b.mcmd_say(message, "  "))
    assert _texts(sent) == ["usage: `/tool say <text>`"] and message.channel.sent == []


# ---------------------------------------------------------------------------
# /info
# ---------------------------------------------------------------------------
class _User:
    def __init__(self, uid: int, name: str, url: str | None = "https://cdn/a.png") -> None:
        self.id = uid
        self._name = name
        if url is not None:
            self.display_avatar = types.SimpleNamespace(url=url)

    def __str__(self) -> str:
        return self._name


def test_avatar_skips_the_bot_itself_and_falls_back_to_the_author(monkeypatch):
    bot_user = _User(1, "bot")
    monkeypatch.setattr(b, "client", types.SimpleNamespace(user=bot_user))
    sent = _replies(monkeypatch)
    other = _User(2, "alice", "https://cdn/alice.png")
    author = _User(3, "carol", "https://cdn/carol.png")
    _run(b.mcmd_avatar(types.SimpleNamespace(mentions=[bot_user, other], author=author), ""))
    _run(b.mcmd_avatar(types.SimpleNamespace(mentions=[bot_user], author=author), ""))
    assert _texts(sent) == ["**alice** avatar:\nhttps://cdn/alice.png",
                            "**carol** avatar:\nhttps://cdn/carol.png"]


def test_avatar_without_a_url_says_so(monkeypatch):
    monkeypatch.setattr(b, "client", types.SimpleNamespace(user=None))
    sent = _replies(monkeypatch)
    _run(b.mcmd_avatar(types.SimpleNamespace(mentions=[], author=_User(3, "dave", None)), ""))
    assert _texts(sent) == ["no avatar URL for dave"]


def test_serverinfo_summarises_the_guild(monkeypatch):
    guild = types.SimpleNamespace(
        name="Guild", id=99, member_count=42, channels=[1, 2, 3], roles=[1, 2],
        created_at=datetime.datetime(2024, 5, 6, tzinfo=datetime.timezone.utc),
        icon=None, premium_tier=2)
    sent = _replies(monkeypatch)
    _run(b.mcmd_serverinfo(types.SimpleNamespace(guild=guild)))
    _run(b.mcmd_serverinfo(types.SimpleNamespace(guild=None)))
    assert _texts(sent) == [
        "**Guild** (id 99)\nmembers: 42, channels: 3, roles: 2\n"
        "created: 2024-05-06, boost level: 2\nicon: (no icon)",
        "not in a server"]


def test_channelinfo_caps_the_topic_and_tolerates_missing_fields(monkeypatch):
    full = types.SimpleNamespace(
        id=5, name="general", type="text", topic="t" * 500,
        created_at=datetime.datetime(2023, 1, 2, tzinfo=datetime.timezone.utc))
    bare = types.SimpleNamespace(id=6, type="private")
    sent = _replies(monkeypatch)
    _run(b.mcmd_channelinfo(types.SimpleNamespace(channel=full)))
    _run(b.mcmd_channelinfo(types.SimpleNamespace(channel=bare)))
    assert _texts(sent) == [
        f"**#general** (id 5)\ntype: text, created: 2023-01-02\ntopic: {'t' * 300}",
        "**#6** (id 6)\ntype: private, created: ?\ntopic: (no topic)"]


# ---------------------------------------------------------------------------
# 圖庫那一組
# ---------------------------------------------------------------------------
@pytest.fixture
def booru_calls(monkeypatch):
    calls: list = []

    async def _image(_message, tags):
        calls.append(("image", tags))

    async def _grid(_message, tags, *, latest=False):
        calls.append(("grid", tags, latest))

    monkeypatch.setattr(b, "_send_danbooru_image", _image)
    monkeypatch.setattr(b, "_send_danbooru_grid", _grid)
    return calls


def test_booru_normalises_tags_and_defaults_to_the_bare_mention_image(booru_calls,
                                                                      monkeypatch):
    _replies(monkeypatch)
    _run(b.mcmd_booru(_STRANGER, " rossi (arknights) "))
    _run(b.mcmd_booru(_STRANGER, ""))
    assert booru_calls == [("image", "rossi_(arknights)"), ("image", "rossi_(arknights)")]


def test_nsfw_adds_the_rating_unless_one_was_given(booru_calls, monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.mcmd_nsfw(_STRANGER, "texas (arknights) --latest"))
    _run(b.mcmd_nsfw(_STRANGER, "texas rating:questionable"))
    _run(b.mcmd_nsfw(_STRANGER, "   "))
    assert booru_calls == [("image", "texas_(arknights) --latest rating:explicit"),
                           ("image", "texas rating:questionable")]
    assert _texts(sent)[0].startswith("usage: `/nsfw <tag>`")


def test_grid_asks_for_the_latest_four(booru_calls, monkeypatch):
    _replies(monkeypatch)
    _run(b.mcmd_grid(_STRANGER, "amiya"))
    _run(b.mcmd_grid(_STRANGER, ""))
    assert booru_calls == [("grid", "amiya", True), ("grid", "", True)]


@pytest.fixture
def tag_query(monkeypatch):
    state = types.SimpleNamespace(calls=[], hits=[])

    async def _query(api, **kwargs):
        state.calls.append((api, kwargs))
        return state.hits

    monkeypatch.setattr(b, "_query_tags_json", _query)
    return state


@pytest.mark.parametrize("rest,pattern", [("Lapp  Land", "lapp_land*"),
                                          ("*Deca*", "*deca*")])
def test_autocomplete_builds_a_prefix_glob(tag_query, monkeypatch, rest, pattern):
    sent = _replies(monkeypatch)
    _run(b.mcmd_autocomplete(_STRANGER, rest))
    assert tag_query.calls == [(b._DANBOORU_TAGS,
                                {"name_matches": pattern, "order": "count", "limit": 10})]
    assert _texts(sent) == [f"no tags match `{pattern}`"]


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


def test_safebooru_sends_the_image_link(safebooru, monkeypatch):
    safebooru.post = {"id": 7, "file_url": "https://img/7.jpg"}
    sent = _replies(monkeypatch)
    _run(b.mcmd_safebooru(_STRANGER, " cat (animal) "))
    assert safebooru.asked == ["cat_(animal)"]
    lines = _texts(sent)[-1].split("\n")
    assert lines[-1] == "https://img/7.jpg" and len(lines) == 2
    assert lines[0].startswith("<https://") and lines[0].endswith("id=7>")


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


def test_e621_sends_the_image_link_without_resolving(e621, monkeypatch):
    e621.posts = {"wolf": {"id": 3, "file": {"url": "https://img/3.png"}}}
    sent = _replies(monkeypatch)
    _run(b.mcmd_e621(_STRANGER, "wolf"))
    lines = _texts(sent)[-1].split("\n")
    assert lines[-1] == "https://img/3.png" and lines[0].endswith("/3>")
    assert e621.resolver_calls == []


def test_e621_retries_once_with_the_resolved_tag_and_says_so(e621, monkeypatch):
    e621.posts = {"lucario_(pokemon)": {"id": 9, "file": {"url": "https://img/9.png"}}}
    e621.resolved = "lucario_(pokemon)"
    sent = _replies(monkeypatch)
    _run(b.mcmd_e621(_STRANGER, "lucar"))
    assert e621.asked == ["lucar", "lucario_(pokemon)"]
    assert e621.resolver_calls == [(b._E621_TAGS, "lucar")]
    assert _texts(sent)[-1].startswith("🔍 解析 `lucar` → `lucario_(pokemon)`\n")


@pytest.mark.parametrize("resolved,shown", [(None, "nothing"), ("nothing", "nothing"),
                                            ("other", "other")])
def test_e621_not_found_names_what_it_searched(e621, monkeypatch, resolved, shown):
    """解析出同一個字串時不再打第二次；解析出別的字串也找不到時，講的是解析後那一個。"""
    e621.resolved = resolved
    sent = _replies(monkeypatch)
    _run(b.mcmd_e621(_STRANGER, "nothing"))
    assert _texts(sent) == [f"找不到符合 `{shown}` 的圖片"]
    assert e621.asked == (["nothing", "other"] if resolved == "other" else ["nothing"])


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


@pytest.fixture
def iqdb(monkeypatch):
    state = types.SimpleNamespace(result=(200, []), asked=[])

    async def _search(url):
        state.asked.append(url)
        return state.result

    monkeypatch.setattr(b, "_iqdb_search", _search)
    return state


def test_iqdb_strips_the_no_embed_brackets_and_lists_matches(iqdb, monkeypatch):
    iqdb.result = (200, [{"site": "a.example", "url": "https://a.example/1", "similarity": 97},
                         {"site": "b.example", "url": "https://b.example/2", "similarity": 80}])
    sent = _replies(monkeypatch)
    _run(b.mcmd_iqdb(_STRANGER, " <https://img.example/q.png> "))
    assert iqdb.asked == ["https://img.example/q.png"]
    assert _texts(sent) == ["**以圖搜圖結果**（前 2 筆）：\n"
                            "- `97%` [a.example] <https://a.example/1>\n"
                            "- `80%` [b.example] <https://b.example/2>"]


@pytest.mark.parametrize("rest", ["", "ftp://x/y.png", "not a url", "<>"])
def test_iqdb_only_accepts_a_web_link(iqdb, monkeypatch, rest):
    sent = _replies(monkeypatch)
    _run(b.mcmd_iqdb(_STRANGER, rest))
    assert _texts(sent) == ["usage: `/iqdb <圖片URL>` — 給直連圖片連結"] and iqdb.asked == []


@pytest.mark.parametrize("result,reply", [
    ((503, []), "以圖搜圖失敗 (HTTP 503)"),
    ((-1, []), "以圖搜圖失敗 (HTTP -1)"),
    ((200, []), "沒找到任何吻合（試試其他圖或檢查 URL 是直連）"),
])
def test_iqdb_failure_and_empty_replies_stay_generic(iqdb, monkeypatch, result, reply):
    """連不上（-1）講的是「失敗」，不是「沒有吻合」——把放棄講成否定答案會被相信。"""
    iqdb.result = result
    sent = _replies(monkeypatch)
    _run(b.mcmd_iqdb(_STRANGER, "https://img.example/q.png"))
    assert _texts(sent) == [reply]


# ---------------------------------------------------------------------------
# /fun cat
# ---------------------------------------------------------------------------
def test_cat_link_carries_a_fresh_cache_buster(monkeypatch):
    sent = _replies(monkeypatch)
    before = int(time.time())
    _run(b.mcmd_cat(_STRANGER))
    after = int(time.time())
    match = re.fullmatch(r"🐱 https://\S+\?ts=(\d+)", _texts(sent)[-1])
    assert match, sent
    assert before <= int(match.group(1)) <= after
