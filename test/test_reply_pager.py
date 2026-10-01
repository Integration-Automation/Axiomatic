"""`_reply_pager`：分頁、頁碼、按鈕綁發起人、逾時停用、沒有按鈕的平台逐頁送出。"""
import asyncio
import types

import discord
import pytest

import _reply_pager as pager
from _chat_platform import PlatformCapabilities


def _pages(n):
    return [discord.Embed(title=f"p{i}", description=f"body {i}") for i in range(1, n + 1)]


def _message(author_id=42, capabilities=None):
    channel = types.SimpleNamespace()
    if capabilities is not None:
        channel.capabilities = capabilities
    return types.SimpleNamespace(author=types.SimpleNamespace(id=author_id), channel=channel)


class _Response:
    def __init__(self):
        self.edits = []
        self.private = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_message(self, content, **kwargs):
        self.private.append((content, kwargs))

    async def defer(self):
        return None


def _interaction(user_id):
    return types.SimpleNamespace(user=types.SimpleNamespace(id=user_id), response=_Response())


# ---------------------------------------------------------------------------
# 切頁
# ---------------------------------------------------------------------------

def test_rows_split_by_count_and_by_size():
    rows = [f"row {i}" for i in range(7)]
    assert pager.paginate_rows(rows, per_page=3, max_chars=1000) == [
        rows[0:3], rows[3:6], rows[6:7]]
    # 字數先滿：每列 6 字＋換行 = 7，上限 15 → 一頁兩列
    assert [len(p) for p in pager.paginate_rows(rows, per_page=10, max_chars=15)] == [2, 2, 2, 1]


def test_an_oversized_row_is_clipped_so_every_page_holds_at_least_one():
    pages = pager.paginate_rows(["x" * 50, "short"], per_page=5, max_chars=10)
    assert pages[0] == ["x" * 9 + "…"] and pages[1] == ["short"]


def test_no_rows_means_no_pages():
    assert pager.paginate_rows([], per_page=5, max_chars=100) == []


def test_page_numbers_keep_the_existing_footer():
    pages = _pages(3)
    pages[0].set_footer(text="附註")
    pager.number_pages(pages)
    assert pages[0].footer.text == "附註 · 第 1/3 頁"
    assert pages[2].footer.text == "第 3/3 頁"


def test_a_single_page_gets_no_number():
    page = _pages(1)
    pager.number_pages(page)
    assert page[0].footer.text is None


def test_dropped_pages_are_announced_not_silent():
    pages = pager.number_pages(_pages(2), dropped=4)
    assert pages[-1].footer.text == "第 2/2 頁（後面還有 4 頁沒列出）"


# ---------------------------------------------------------------------------
# 按鈕
# ---------------------------------------------------------------------------

def test_the_buttons_move_and_disable_at_the_ends():
    async def scenario():
        view = pager.PagerView(_pages(3), owner_id=42)
        assert view.previous_page.disabled and not view.next_page.disabled
        assert view.position.label == "1 / 3"
        hit = _interaction(42)
        await view.next_page.callback(hit)
        await view.next_page.callback(hit)
        assert view.index == 2 and view.next_page.disabled and not view.previous_page.disabled
        assert [e["embed"].title for e in hit.response.edits] == ["p2", "p3"]
        assert view.position.label == "3 / 3"
        await view.previous_page.callback(hit)
        assert view.index == 1 and hit.response.edits[-1]["view"] is view
    asyncio.run(scenario())


def test_only_the_invoker_may_flip():
    async def scenario():
        view = pager.PagerView(_pages(2), owner_id=42)
        stranger = _interaction(7)
        assert await view.interaction_check(stranger) is False
        assert stranger.response.private == [(pager.NOT_YOURS, {"ephemeral": True})]
        assert await view.interaction_check(_interaction(42)) is True
    asyncio.run(scenario())


def test_timeout_disables_every_button_on_the_sent_message():
    async def scenario():
        view = pager.PagerView(_pages(2), owner_id=42)
        edits = []

        class _Sent:
            async def edit(self, **kwargs):
                edits.append(kwargs)

        view.message = _Sent()
        await view.on_timeout()
        assert all(item.disabled for item in view.children)
        assert edits == [{"view": view}]
    asyncio.run(scenario())


def test_timeout_survives_a_deleted_message():
    async def scenario():
        view = pager.PagerView(_pages(2), owner_id=42)

        class _Gone:
            async def edit(self, **_kwargs):
                raise discord.NotFound(types.SimpleNamespace(status=404, reason="gone"), "x")

        view.message = _Gone()
        await view.on_timeout()
        view.message = None
        await view.on_timeout()
    asyncio.run(scenario())


def test_a_pager_needs_two_pages():
    async def scenario():
        with pytest.raises(ValueError):
            pager.PagerView(_pages(1), owner_id=1)
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 送出
# ---------------------------------------------------------------------------

def _run_send(message, pages):
    calls = []

    async def reply(msg, **kwargs):
        calls.append(kwargs)
        return "sent-message"

    asyncio.run(pager.send_paged(reply, message, pages))
    return calls


def test_one_page_is_a_plain_card():
    calls = _run_send(_message(), _pages(1))
    assert len(calls) == 1 and set(calls[0]) == {"embed"}


def test_many_pages_on_a_button_platform_are_one_message_with_a_pager():
    calls = _run_send(_message(), _pages(3))
    assert len(calls) == 1
    view = calls[0]["view"]
    assert isinstance(view, pager.PagerView) and view.owner_id == 42
    assert view.message == "sent-message"
    assert calls[0]["embed"].footer.text == "第 1/3 頁"


def test_a_platform_without_buttons_gets_every_page_in_turn():
    calls = _run_send(_message(capabilities=PlatformCapabilities()), _pages(3))
    assert [c["embed"].title for c in calls] == ["p1", "p2", "p3"]
    assert all("view" not in c for c in calls)


def test_a_platform_that_declares_buttons_gets_the_pager():
    calls = _run_send(_message(capabilities=PlatformCapabilities(buttons=True)), _pages(2))
    assert len(calls) == 1 and "view" in calls[0]


@pytest.mark.parametrize("author_id", [None, True, "42"])
def test_no_usable_invoker_means_no_buttons(author_id):
    """綁不了人的按鈕等於誰都能按；`True` 是 int 的子類別，要另外擋。"""
    calls = _run_send(_message(author_id=author_id), _pages(2))
    assert len(calls) == 2 and all("view" not in c for c in calls)


def test_pages_past_the_cap_are_dropped_and_announced():
    calls = _run_send(_message(capabilities=PlatformCapabilities()),
                      _pages(pager.MAX_PAGES + 3))
    assert len(calls) == pager.MAX_PAGES
    assert calls[-1]["embed"].footer.text.endswith("（後面還有 3 頁沒列出）")


def test_no_pages_is_a_caller_bug():
    with pytest.raises(ValueError):
        _run_send(_message(), [])
