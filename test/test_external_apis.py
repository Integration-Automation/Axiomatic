"""Guard tests for `_external_apis.py`.

This module originally had not a single test -- 366 lines, six entry points to
external sites, all of them running only when a user actually invokes them, with
a failure path that is always "return None / return empty list". So when it was
found on 2026-08-30, **all Danbooru features were completely broken** (HTTP 403),
and nothing in the repo went red.

The cause was that the whole Danbooru site moved behind Cloudflare and began
refusing requests without a `User-Agent`, and this project's Danbooru calls all
happened to carry none -- the module even had a comment reading "Danbooru accepts
aiohttp's default", a sentence that was once true.

So this file has two layers:

* **The structural layer** (no network): there may be only one outbound exit,
  every request must carry a UA, and the UA must not pretend to be a browser.
  This layer stops "someone bypassing it again next time".
* **The live layer** (needs network, skips when unreachable): actually hit an
  endpoint once to confirm our current UA is not being blocked. This layer stops
  "the site changes its rules some day" -- which static analysis can never see.

The live layer deliberately **only goes red when blocked, and skips when
unreachable**: an offline environment should have no red, but "reachable and
refused" really is broken and must be loud.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import _external_apis as ex  # noqa: E402
import verify_external_apis as vx  # noqa: E402

_MODULE = Path(ex.__file__)
_BOT_SOURCE = _MODULE.parent / "discord_bot.py"


# ---------------------------------------------------------------------------
# Fake aiohttp: record the headers each request actually sends
# ---------------------------------------------------------------------------

class _FakeBody:
    """A stand-in for `r.content`: a minimal stream that **tracks position**.

    Deliberately returns **bytes** rather than an already-parsed object: since
    2026-09-06 the production code does its own `json.loads` on the bytes (to
    apply the size cap), so a stand-in still stuck on "`json()` returns a Python
    object directly" would not test the parse-and-cap step -- which is the whole
    reason this stand-in exists.

    **`self._pos` is load-bearing, do not mistake it for tidiness.** Before
    2026-09-07 this class had no position, and `read(n)` returned `self._raw[:n]`
    every time -- an infinite vending machine of the same bytes that never
    returned `b""` (EOF). It was correct for a long time only because the
    production code at the time **happened to call `read()` exactly once**; the
    stand-in's correctness was actually propped up by an implementation detail of
    the code under test. Once that line became a loop to EOF (as it should have
    been), the whole set of cap tests would go red together, and **the code under
    test is right, the stand-in is wrong** -- that kind of red is the easiest to
    misread as "the new code has a problem" and to revert the fix.

    The general principle: **a stand-in should follow the contract of the thing
    it imitates, not how the current caller happens to use it.** A stream with no
    EOF is not a stream.
    """

    def __init__(self, raw: bytes):
        self._raw = raw
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk = self._raw[self._pos:]
        else:
            chunk = self._raw[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class _FakeResponse:
    def __init__(self, status, payload, *, content_length=None, raw=None):
        self.status = status
        self._payload = payload
        # `raw` lets a test specify bytes directly (to test the cap, or broken
        # JSON); if not given, serialise the payload into normal JSON like a real
        # response.
        self._raw = (raw if raw is not None
                     else json.dumps(payload).encode("utf-8"))
        self.content_length = content_length
        self.content = _FakeBody(self._raw)

    async def json(self):
        return self._payload

    async def text(self):
        return self._raw.decode("utf-8", errors="replace")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """A recording stand-in for `ClientSession`. `calls` is class-level for easy test access."""

    calls: list[dict] = []
    replies: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _record(self, method, url, params, headers):
        type(self).calls.append({
            "method": method, "url": url,
            "params": dict(params or {}), "headers": dict(headers or {}),
        })
        if type(self).replies:
            status, payload = type(self).replies.pop(0)
        else:
            status, payload = 200, []
        return _FakeResponse(status, payload)

    def get(self, url, *, params=None, headers=None, **kw):
        return self._record("GET", url, params, headers)

    def post(self, url, *, params=None, headers=None, **kw):
        return self._record("POST", url, params, headers)


@pytest.fixture
def fake_http(monkeypatch):
    """Replace the `aiohttp.ClientSession` used by `_external_apis` with a recorder.

    It replaces `ex.aiohttp.ClientSession`, the one the module actually calls --
    so if someone ever switches this module to a different HTTP library, this all
    breaks at once, which is exactly what we want to know.
    """
    _FakeSession.calls = []
    _FakeSession.replies = []
    monkeypatch.setattr(ex.aiohttp, "ClientSession", _FakeSession)
    return _FakeSession


def _run(coro):
    return asyncio.run(coro)


# Each outbound fetch function -> one "how to call it" thunk. Add a fetcher by
# adding a line, and all the header / counting guards below cover it
# automatically.
_FETCHERS = {
    "danbooru_post": lambda: ex._fetch_danbooru_post("tag"),
    "danbooru_bulk": lambda: ex._fetch_danbooru_posts_bulk("tag"),
    "danbooru_random_n": lambda: ex._fetch_danbooru_posts_random("tag", 2),
    "danbooru_latest": lambda: ex._fetch_danbooru_posts_latest("tag"),
    "danbooru_latest_one": lambda: ex._fetch_danbooru_post_latest("tag"),
    "safebooru_post": lambda: ex._fetch_safebooru_post("tag"),
    "e621_post": lambda: ex._fetch_e621_post("tag"),
    "danbooru_tags": lambda: ex._query_tags_json(ex._DANBOORU_TAGS,
                                                 name="tag"),
    "e621_tags": lambda: ex._query_tags_json(ex._E621_TAGS, name="tag"),
}


# ---------------------------------------------------------------------------
# Structural layer: there may be only one outbound exit
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Response size cap
#
# The image-download path had a cap long ago (`discord_bot.GRID_MAX_IMAGE_BYTES`),
# with the reasoning right in the comment: "the bytes on this path are not
# content we control". The JSON path goes through **the same source, the same
# threat**, yet had no cap for a long time -- `await r.json()` slurps the whole
# response into memory. `ClientTimeout(total=...)` does not stop it: it governs
# transfer time, not size, and a huge response that keeps streaming steadily will
# exhaust the process's memory within the timeout. Added 2026-09-06.
# ---------------------------------------------------------------------------

class _SizedSession(_FakeSession):
    """A session stand-in that can specify the response bytes and `content_length`."""

    raw: bytes = b"[]"
    declared = None

    def _record(self, method, url, params, headers):
        type(self).calls.append({
            "method": method, "url": url,
            "params": dict(params or {}), "headers": dict(headers or {}),
        })
        return _FakeResponse(200, None, raw=type(self).raw,
                             content_length=type(self).declared)


@pytest.fixture
def sized_http(monkeypatch):
    _SizedSession.calls = []
    _SizedSession.raw = b"[]"
    _SizedSession.declared = None
    monkeypatch.setattr(ex.aiohttp, "ClientSession", _SizedSession)
    return _SizedSession


def test_a_post_goes_through_the_same_exit_as_a_get(sized_http, monkeypatch):
    """POST shares the exit with GET: same default User-Agent, same one-call
    count, same size cap. The body must really be sent as JSON -- looking only at
    the return value, a version that throws the body away would also pass."""
    bodies: list = []

    class _Recording(sized_http):
        def post(self, url, *, params=None, headers=None, **kw):
            bodies.append(kw.get("json"))
            return super().post(url, params=params, headers=headers, **kw)

    monkeypatch.setattr(ex.aiohttp, "ClientSession", _Recording)
    before = ex._METRICS_API_CALLS
    sized_http.raw = json.dumps({"data": {"ok": True}}).encode("utf-8")
    status, data = _run(ex._http_post_json("https://example.invalid/gql", {"query": "q"}))
    assert (status, data) == (200, {"data": {"ok": True}})
    call = sized_http.calls[-1]
    assert call["method"] == "POST" and bodies == [{"query": "q"}]
    assert call["headers"].get("User-Agent"), call
    assert ex._METRICS_API_CALLS == before + 1

    sized_http.declared = ex._MAX_RESPONSE_BYTES + 1
    assert _run(ex._http_post_json("https://example.invalid/gql", {})) == (200, None)


def test_a_normal_response_still_parses(sized_http):
    sized_http.raw = json.dumps([{"id": 1}]).encode("utf-8")
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert (status, data) == (200, [{"id": 1}])


def test_json_served_as_plain_text_still_parses(sized_http):
    """Originally handled by the `ContentTypeError` -> `r.text()` -> `loads` fallback.

    After switching to `loads` on the bytes directly, that fallback was folded
    in, but **the behaviour must be identical** -- sites labelling JSON as
    `text/plain` is common, and degrading into a parse failure would make the
    whole feature silently return empty.
    """
    sized_http.raw = b'{"ok": true}'
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert (status, data) == (200, {"ok": True})


def test_an_oversized_response_is_dropped(sized_http):
    """Over the cap, drop the whole thing, do not parse.

    The assertion is `data is None` -- not "was there an exception". The symptom
    of the cap failing is not a crash, it is memory being exhausted, which a test
    cannot see, so it can only be verified from the "was it refused" side.
    """
    sized_http.raw = b"[" + b"0," * (ex._MAX_RESPONSE_BYTES // 2) + b"0]"
    assert len(sized_http.raw) > ex._MAX_RESPONSE_BYTES
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert status == 200
    assert data is None, "an over-cap response was still parsed"


def test_a_declared_oversize_is_refused_before_reading(sized_http):
    """When `Content-Length` alone is already over the cap, do not even read.

    This one saves pointlessly reading several MB; the real defence is the
    actual-read cap below (a header can lie).
    """
    sized_http.declared = ex._MAX_RESPONSE_BYTES + 1
    sized_http.raw = b"[1]"          # content is actually tiny but declares huge
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert (status, data) == (200, None)


def test_exactly_at_the_cap_is_still_accepted(sized_http):
    """Boundary: exactly at the cap must be accepted, not refused.

    The +1 in `read(cap + 1)` was there to tell "exactly" from "over"; without
    it, a response exactly at the cap is misjudged as over and dropped.
    """
    payload = b"[" + b"1," * ((ex._MAX_RESPONSE_BYTES - 3) // 2) + b"1]"
    payload += b" " * (ex._MAX_RESPONSE_BYTES - len(payload))
    assert len(payload) == ex._MAX_RESPONSE_BYTES
    sized_http.raw = payload
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert status == 200
    assert isinstance(data, list), "a response exactly at the cap was misjudged as over"


def test_one_bad_byte_does_not_throw_away_the_whole_response(sized_http):
    """The response bytes are not ours: one bad byte should not scrap all the data.

    This test verifies that `errors="replace"` is really applied on this path,
    and **the verification has a trap**: bytes that are "neither valid UTF-8 nor
    valid JSON" cannot verify it -- strict decoding throws `UnicodeDecodeError`,
    lenient decoding throws `JSONDecodeError`, and both are caught by the outer
    `except Exception` and both return `(-1, None)`, so **the difference is not
    observable**. (Mutation testing caught this on the spot: switched to strict
    decoding, the original code was still green.)

    So this uses "JSON structure intact, only one bad byte embedded in a string
    value" -- lenient decoding replaces it with U+FFFD and parses the data
    normally, while strict decoding drops the whole thing. This is also exactly
    the real situation: a tag name returned by the site with one encoding-broken
    character should not make the whole search return empty.
    """
    # \xe9 is latin-1 é, an illegal lone byte in UTF-8.
    sized_http.raw = b'{"name": "caf\xe9", "id": 7}'
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert status == 200
    assert data is not None, (
        "one bad byte threw away the whole response -- decode should use errors='replace'")
    assert data["id"] == 7
    assert data["name"].startswith("caf")


def test_the_cap_is_a_real_positive_number():
    assert isinstance(ex._MAX_RESPONSE_BYTES, int)
    assert ex._MAX_RESPONSE_BYTES > 0


# ---------------------------------------------------------------------------
# Chunked delivery: `read(n)` is read-up-to, not "read a full n"
#
# An active defect measured 2026-09-07. The cap tests above were **all green**,
# because `_FakeBody` hands over the whole body at once -- so `read(n)` always
# finishes in the test, and the real world is not like that. The stand-in in this
# section deliberately delivers in several pieces, which is what
# `aiohttp.StreamReader` really does.
# ---------------------------------------------------------------------------

class _ChunkedBody:
    """A stand-in for `r.content` that hands over the body **in several pieces**.

    This is the real semantics of `aiohttp.StreamReader.read(n)`: *read up to n*
    -- return no more than n bytes, but **possibly fewer than n**, even when more
    data follows. `_FakeBody` gives it all at once, so it can never test this;
    that is why the truncation defect slipped past the whole suite.
    """

    def __init__(self, chunks):
        self._chunks = [bytes(c) for c in chunks]
        self.reads = 0

    async def read(self, n: int = -1) -> bytes:
        self.reads += 1
        if not self._chunks:
            return b""                       # EOF
        head = self._chunks[0]
        if n is None or n < 0 or n >= len(head):
            return self._chunks.pop(0)
        self._chunks[0] = head[n:]           # give only the first n, keep the rest for next time
        return head[:n]


class _ChunkedSession(_FakeSession):
    """A session stand-in that delivers the response body in chunks."""

    chunks: list = [b"[]"]
    declared = None

    def _record(self, method, url, params, headers):
        type(self).calls.append({
            "method": method, "url": url,
            "params": dict(params or {}), "headers": dict(headers or {}),
        })
        resp = _FakeResponse(200, None, raw=b"".join(type(self).chunks),
                             content_length=type(self).declared)
        resp.content = _ChunkedBody(type(self).chunks)
        return resp


@pytest.fixture
def chunked_http(monkeypatch):
    _ChunkedSession.calls = []
    _ChunkedSession.chunks = [b"[]"]
    _ChunkedSession.declared = None
    monkeypatch.setattr(ex.aiohttp, "ClientSession", _ChunkedSession)
    return _ChunkedSession


def _split(raw: bytes, first: int = 100) -> list[bytes]:
    """Split into "first chunk + the rest", simulating real chunked delivery."""
    return [raw[:first], raw[first:]] if len(raw) > first else [raw]


def test_a_chunked_response_is_read_to_the_end(chunked_http):
    """A chunked, valid JSON response must parse in full.

    **Before the fix this test was red**, and red for the exact reason the user
    measured: the old code was a single `await r.content.read(cap + 1)`, `read(n)`
    returns only "the currently buffered piece", so it got only the first 100-byte
    chunk, `json.loads` threw `Unterminated string`, caught by the outer
    `except Exception` and turned into `(-1, None)` -- the caller saw "not found".
    """
    payload = [{"id": i, "tag": "rossi_(arknights)"} for i in range(30)]
    raw = json.dumps(payload).encode("utf-8")
    assert len(raw) > 100, "the fixture must be big enough to span at least two chunks, or nothing is verified"
    chunked_http.chunks = _split(raw)

    status, data = _run(ex._http_get_json("https://example.invalid/x"))

    assert data is not None, (
        "the chunked response was truncated -- `read(n)` is read-up-to, must loop to EOF")
    assert status == 200
    assert len(data) == 30, f"only read {len(data)} entries, the body was cut in half"


def test_a_chunked_response_that_exceeds_the_cap_is_still_dropped(chunked_http):
    """The cap must not stop working because of the switch to a loop.

    The `status == 200` half is the point: `data is None` alone cannot tell "the
    cap stopped it" from "parsing blew up and got caught by the outer except" --
    the latter returns `-1`. Without this half, deleting the whole cap would keep
    this test green, and then the two defences mask each other.
    """
    over = b"[" + b"0," * (ex._MAX_RESPONSE_BYTES // 2) + b"0]"
    assert len(over) > ex._MAX_RESPONSE_BYTES
    chunked_http.chunks = _split(over)

    status, data = _run(ex._http_get_json("https://example.invalid/x"))

    assert status == 200, "should be the cap stopping it (returns r.status), not a parse failure (returns -1)"
    assert data is None, "an over-cap response was still parsed"


# ---------------------------------------------------------------------------
# The shared reading primitive itself
#
# Image download (`discord_bot._send_danbooru_grid`) goes through the same
# function, so verifying it here verifies both sides -- provided "both sides
# really go through this one", which the AST guard below watches.
# ---------------------------------------------------------------------------

def _read(chunks, cap):
    return _run(ex.read_capped_body(_ChunkedBody(chunks), cap))


def test_the_shared_reader_returns_the_whole_body():
    """The complete bytes of a chunked delivery must come back -- that is what the image side needs.

    A truncated image has a different but equally silent symptom: Pillow cannot
    open it, that cell drops out of the grid, and the user just sees "a few images
    are missing".
    """
    body = bytes(range(256)) * 400              # 102400 bytes, spans several chunks
    chunks = [body[i:i + 1000] for i in range(0, len(body), 1000)]
    assert len(chunks) > 1
    assert _read(chunks, 20 * 1024 * 1024) == body


def test_the_shared_reader_drops_an_oversized_body():
    assert _read([b"x" * 50, b"y" * 51], 100) is None


def test_the_shared_reader_accepts_exactly_the_cap():
    """Boundary: exactly at the cap must be accepted, not refused.

    The +1 in the old code's `read(cap + 1)` was there to tell "exactly" from
    "over"; after the switch to a loop, that line is maintained by `total > cap`,
    with unchanged semantics.
    """
    assert _read([b"x" * 60, b"y" * 40], 100) == b"x" * 60 + b"y" * 40


def test_an_empty_body_is_not_confused_with_an_oversized_one():
    """An empty body returns `b""`, over the cap returns `None` -- the caller must tell them apart with `is None`.

    Writing `if not raw:` would misjudge "the site returned an empty 200" as
    "over the 8 MB cap", then print a completely misleading stderr line.
    """
    assert _read([], 100) == b""
    assert _read([b""], 100) == b""


def test_the_shared_reader_stops_reading_once_over_the_cap():
    """Once over the cap, stop reading.

    The cap's purpose is not to slurp a huge third-party response into memory; if
    it pulls the whole thing just to compute "how big", the cap only has the "do
    not parse" effect and the memory is consumed anyway.
    """
    body = _ChunkedBody([b"z" * 10] * 100)
    assert _run(ex.read_capped_body(body, 25)) is None
    assert body.reads < 10, (
        f"still reading after over the cap (read {body.reads} times) -- the cap should stop the moment it is sure it is over")


def test_no_response_body_is_read_with_a_single_capped_read():
    """`<response>.content.read(...)` must not appear directly in any caller again.

    This is the **shape** of the defect, not a typo on one line: `read(cap + 1)`
    looks entirely reasonable, and under a test stand-in that "hands over the
    whole thing at once" it is always right. The same shape existed at the time in
    two files (one for JSON, one for images), which means code review never caught
    it.

    Unbounded `.content.read()` (to EOF) is blocked too -- that is the mistake in
    the other direction: removing the size cap entirely. Both should use
    `read_capped_body`.
    """
    offenders = []
    for path in (_MODULE, _BOT_SOURCE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "read"):
                continue
            owner = func.value
            if isinstance(owner, ast.Attribute) and owner.attr == "content":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"these places read directly from the response body: {offenders}. "
        "`StreamReader.read(n)` is read-up-to, and a single call truncates a "
        "chunked response into valid-but-incomplete bytes (measured 2026-09-07: "
        "38043 / 91898 bytes), with the symptom being the feature silently "
        "returning nothing. Use `_external_apis.read_capped_body(r.content, <cap>)`.")


def test_both_response_readers_go_through_the_shared_helper():
    """The reverse watch: both callers **still** use the shared function.

    Verifying only the "no direct read" rule above is not enough -- deleting the
    whole read, or replacing it with a hand-written loop of one's own, keeps that
    rule green. An exemption (or a refactor) must have someone prove it still does
    the original thing, or it will outlive its reason.
    """
    wanted = {
        "_external_apis.py": "_http_json",
        "discord_bot.py": "_send_danbooru_grid",
    }
    for path in (_MODULE, _BOT_SOURCE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        target = wanted[path.name]
        found = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == target]
        assert found, f"{target} not found in {path.name} (renamed?)"
        names = {c.func.id for c in ast.walk(found[0])
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "read_capped_body" in names, (
            f"{path.name}:{target} does not go through `read_capped_body`. Both "
            "callers read chunked bytes sent by a third party, and the shared "
            "function is the single source of truth -- before 2026-09-07 the same "
            "truncation defect existed on both sides at once.")


def test_only_http_get_json_opens_a_session():
    """In this module **only** `_http_json` (the one under `_http_get_json` /
    `_http_post_json`) may open an `aiohttp.ClientSession`.

    This is the structural cause of the incident, not a style question. The three
    Danbooru fetchers each used to inline an `async with
    aiohttp.ClientSession(...)`, so all three **bypassed** the User-Agent that
    `_http_get_json` fills in, and all three **bypassed** `_METRICS_API_CALLS`.
    "Open another connection" and "omit the header, skip the count" are two sides
    of the same mistake in this module, so the former is blocked directly.
    """
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", ""))
            if name == "ClientSession" and node.name != "_http_json":
                offenders.append(f"{node.name}:{inner.lineno}")
    assert not offenders, (
        f"these functions open a ClientSession of their own: {offenders}. The "
        "outbound exit of this module can only be `_http_json` -- bypassing it "
        "bypasses the User-Agent and the API count, which is exactly why the "
        "2026-08-30 site-wide Danbooru 403 went unnoticed by everyone.")


def test_every_outbound_request_carries_a_user_agent(fake_http):
    """Every request a fetcher sends must have a non-empty `User-Agent`.

    A behavioural re-check: even if someone bypasses the previous test's AST scan
    (opening a session a different way), this goes red as soon as a request is
    missing its UA.
    """
    for label, thunk in _FETCHERS.items():
        fake_http.calls = []
        _run(thunk())
        assert fake_http.calls, f"{label} sent no request at all"
        for call in fake_http.calls:
            ua = call["headers"].get("User-Agent")
            assert ua, f"{label} sent a request with no User-Agent: {call['headers']}"


def test_the_user_agent_does_not_pretend_to_be_a_browser():
    """The UA must "explain who you are" and must not pretend to be a browser.

    This is not only a site rule (Danbooru's Help:Api states in writing "Don't
    impersonate browsers or use the default header of your library"), in practice
    it is also worse: on 2026-08-30 all three were measured -- no UA is 403, **a
    full Chrome 140 UA is also 403**, and only the explanatory UA got 200. The
    challenge page expects a real browser to solve the JS, we cannot, so we are
    judged to be impersonating.

    `_BROWSER_UA` is not subject to this: it is for Safebooru / IQDB, and it uses
    the `Mozilla/5.0 (compatible; <own name>)` form -- "compatible format but
    still self-identifying" -- not impersonating a specific real browser version.
    """
    assert not ex._BOT_UA.lower().startswith("mozilla"), (
        f"_BOT_UA={ex._BOT_UA!r} looks like a browser. Measured: pretending to be a browser gets blocked too.")
    assert "axiomatic" in ex._BOT_UA, (
        f"_BOT_UA={ex._BOT_UA!r} does not identify itself; the site wants someone reachable when things go wrong.")
    assert "compatible;" in ex._BROWSER_UA and "axiomatic" in ex._BROWSER_UA, (
        f"_BROWSER_UA={ex._BROWSER_UA!r} should keep the \"compatible format but "
        "still self-identifying\" form; impersonating a specific real browser "
        "version both breaks the site rules and is more likely to be blocked.")


def test_caller_headers_win_over_the_default(fake_http):
    """A header the caller states explicitly overrides the default, not the other way round.

    e621 has its own UA rule, and `_query_tags_json(_E621_TAGS, ...)` carries
    `_E621_UA`; if the default overrode the caller instead, that rule could never
    apply.
    """
    fake_http.calls = []
    _run(ex._http_get_json("https://example.invalid/x",
                           headers={"User-Agent": "custom-ua/9"}))
    assert fake_http.calls[0]["headers"]["User-Agent"] == "custom-ua/9"

    fake_http.calls = []
    _run(ex._http_get_json("https://example.invalid/x",
                           headers={"Accept": "application/json"}))
    sent = fake_http.calls[0]["headers"]
    assert sent["Accept"] == "application/json", "the caller's other headers must be kept"
    assert sent["User-Agent"] == ex._BOT_UA, "the default must be filled in when no UA is specified"


def test_every_fetcher_is_counted(fake_http):
    """Every fetcher must be counted by `_METRICS_API_CALLS`.

    `/health`'s "api calls" and `!metrics` read it. Before 2026-08-30 the three
    Danbooru fetchers were not counted at all, making that number a wrong answer
    to "how many external APIs did I actually hit".
    """
    for label, thunk in _FETCHERS.items():
        before = ex.api_call_count()
        _run(thunk())
        assert ex.api_call_count() > before, (
            f"{label} was not counted by api_call_count() -- it probably did not go through _http_get_json.")


# ---------------------------------------------------------------------------
# Behavioural layer: after merging the three `_attempt`s, the semantics must be identical
# ---------------------------------------------------------------------------

def test_the_anonymous_two_tag_random_limit_still_falls_back(fake_http):
    """Anonymous Danbooru has a 2-tag limit on `random=true`; hitting it returns 422.

    After a 422, **drop `random`** and retry (fetch the latest N, pick
    client-side), not give up. This is the easiest thing to lose when merging the
    three `_attempt`s into `_danbooru_posts`.
    """
    fake_http.replies = [(422, None), (200, [{"id": 1}, {"id": 2}])]
    posts = _run(ex._fetch_danbooru_posts_bulk("a b c"))
    assert len(fake_http.calls) == 2, "should retry once after a 422"
    assert fake_http.calls[0]["params"].get("random") == "true"
    assert "random" not in fake_http.calls[1]["params"], (
        "the fallback must drop random, or it will hit the same 422 again")
    assert [p["id"] for p in posts] == [1, 2]


def test_a_non_200_is_never_silent(fake_http, capsys):
    """**Every** fetcher must leave one stderr line on a non-200.

    This is the direct reason the incident stayed hidden so long. The originally
    silent path was not just Danbooru's post fetch: `/tags.json` (the whole fuzzy
    tag resolver) and "latest N" also returned empty on a non-200 without a word.
    So all three symptoms -- random image not found, fuzzy tag not resolved,
    `--latest` empty -- left no clue in the log at all.

    This test covers **all** entries of `_FETCHERS`, so a new site is included
    automatically.
    """
    for label, thunk in _FETCHERS.items():
        fake_http.calls = []
        fake_http.replies = [(403, None), (403, None)]
        capsys.readouterr()
        _run(thunk())
        err = capsys.readouterr().err
        assert "403" in err, f"{label} said nothing on HTTP 403: {err!r}"


def test_the_expected_422_stays_quiet(fake_http, capsys):
    """The reverse: an expected 422 must not be noisy.

    Anonymous Danbooru always returns 422 for a multi-tag `random=true`, and we
    are already prepared to step back and retry -- that is not a fault. Printing a
    line on every search would turn this diagnostic into noise, then nobody would
    look at it any more, and we are back to "silent failure".
    """
    fake_http.replies = [(422, None), (200, [{"id": 1}])]
    capsys.readouterr()
    _run(ex._fetch_danbooru_posts_bulk("a b c"))
    err = capsys.readouterr().err
    assert "422" not in err, f"an expected 422 was noisy: {err!r}"


def test_an_unexpected_status_on_the_first_try_still_talks(fake_http, capsys):
    """`quiet_statuses` should only silence 422, not silence the whole first attempt."""
    fake_http.replies = [(500, None)]
    capsys.readouterr()
    _run(ex._fetch_danbooru_posts_bulk("a b c"))
    assert "500" in capsys.readouterr().err


def test_a_422_that_is_not_a_random_query_is_not_retried(fake_http):
    """A query without `random` that gets a 422 should not retry -- the fallback would be identical to the original request."""
    fake_http.replies = [(422, None), (200, [{"id": 9}])]
    _run(ex._danbooru_posts("tag", limit=3, random_order=False))
    assert len(fake_http.calls) == 1, (
        "there is no random to drop, so a retry just sends the same request again")


def test_take_unseen_prefers_fresh_ids_then_reuses_the_pool():
    """When the dedup queue is full, reuse the whole pool, do not return empty.

    `_danbooru_recent` holds only `DANBOORU_HISTORY_SIZE` entries; when the pool
    has all been sent, better to repeat an image than to show the user "not
    found".
    """
    ex._danbooru_recent.clear()
    posts = [{"id": n} for n in range(5)]
    first = ex._take_unseen(posts, 2)
    assert len(first) == 2
    assert all(p["id"] in ex._danbooru_recent for p in first), (
        "picked ones must be recorded in the dedup queue, or the next call picks the same image again")

    ex._danbooru_recent.clear()
    ex._danbooru_recent.extend(p["id"] for p in posts)
    again = ex._take_unseen(posts, 2)
    assert len(again) == 2, "when all have been sent, reuse the whole pool, not return empty"


def test_take_unseen_handles_a_short_pool():
    """When the pool is smaller than requested, return however many there are (`--grid` fills cells with this)."""
    ex._danbooru_recent.clear()
    assert ex._take_unseen([], 4) == []
    assert len(ex._take_unseen([{"id": 1}, {"id": 2}], 4)) == 2


def test_single_post_fetch_returns_none_when_there_is_nothing(fake_http):
    """An empty result must return None, not throw IndexError."""
    fake_http.replies = [(200, [])]
    assert _run(ex._fetch_danbooru_post("tag")) is None


def test_tags_json_drops_non_dict_entries(fake_http):
    """When the site rate-limits / errors, `/tags.json` returns a list with non-dicts inside."""
    fake_http.replies = [(200, ["oops", {"name": "ok", "post_count": 3}, None])]
    hits = _run(ex._query_tags_json(ex._DANBOORU_TAGS, name="x"))
    assert hits == [{"name": "ok", "post_count": 3}]


def test_fuzzy_resolver_skips_search_modifiers(fake_http):
    """A modifier like `rating:general` is not a tag name and must not be sent to `/tags.json` to resolve."""
    fake_http.replies = [(200, [])] * 20
    _run(ex._resolve_fuzzy_tags(ex._DANBOORU_TAGS, "rating:general score:>=5"))
    assert not fake_http.calls, (
        f"a modifier was taken to look up a tag: {fake_http.calls}")


# ---------------------------------------------------------------------------
# Structural layer: sessions the bot opens itself must also carry a UA
# ---------------------------------------------------------------------------

def test_the_bots_own_sessions_pass_headers():
    """A `ClientSession` opened by hand in `discord_bot.py` must also carry headers on the request.

    The image CDN sits behind the same protection as the API -- measured
    2026-08-30, `cdn.donmai.us` also returns 403 to requests with no UA. That is,
    `--grid` is broken in **both** places: the API cannot get the list, and even
    with the list every image fails to download. Fix one and miss the other, and
    the symptom shifts from "not found" to "nothing can be posted", equally hard
    to investigate.
    """
    tree = ast.parse(_BOT_SOURCE.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        session_names = []
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", ""))
            if name == "ClientSession" and isinstance(item.optional_vars,
                                                      ast.Name):
                session_names.append(item.optional_vars.id)
        if not session_names:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if (isinstance(func, ast.Attribute)
                    and func.attr in ("get", "post")
                    and isinstance(func.value, ast.Name)
                    and func.value.id in session_names):
                if not any(kw.arg == "headers" for kw in inner.keywords):
                    offenders.append(f"line {inner.lineno}: .{func.attr}()")
    assert not offenders, (
        f"these requests carry no headers: {offenders}. A session the bot opens "
        "by hand does not go through `_external_apis._http_get_json`, so the "
        "User-Agent must be carried at the call site -- without it, the image CDN "
        "returns 403 too.")


# ---------------------------------------------------------------------------
# Completeness of the endpoint list: adding an external endpoint must not skip the verification entry
# ---------------------------------------------------------------------------

def _literal_url(node, consts):
    """Recover the start of a URL literal from an AST node as best as possible; return None if it cannot."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.Attribute):
        return consts.get(node.attr)
    if isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                return part.value
            return None
    if isinstance(node, ast.BinOp):
        return _literal_url(node.left, consts)
    return None


def _string_bindings(scope, module_level_only=False, seed=None):
    """A map of `NAME = "https://..."` and `NAME = f"https://...{x}"`.

    It must follow **local** variables, not just module-level constants: the most
    common form in this repo is `url = f"https://.../{x}"` then
    `_http_get_json(url)`, and the first version only looked at the module level,
    so half the endpoints were not seen -- and the unseen ones were exactly those
    this rule should most protect.
    """
    # `seed` is the module-level constants. The BinOp branch needs it to resolve
    # `target = _IQDB_URL + "?" + urlencode(...)` -- the local table starts empty,
    # and without the seed `_IQDB_URL` cannot be found and that endpoint is
    # silently unseen.
    out = dict(seed or {})
    nodes = scope.body if module_level_only else ast.walk(scope)
    for node in nodes:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        text = None
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            text = value.value
        elif isinstance(value, ast.JoinedStr) and value.values:
            head = value.values[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                text = head.value
        elif isinstance(value, ast.BinOp):
            text = _literal_url(value.left, out)
        if not text:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = text
    return out


def _fetched_hosts(path):
    """The external hostnames this file actually "fetches".

    Only the calls that really issue a request (`_http_get_json` / a session's
    `.get` / `.post`), so links pasted for people (e.g. a post page URL) are not
    counted -- if those break it only breaks the link, not the feature.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_bindings = _string_bindings(tree, module_level_only=True)
    hosts = set()
    # **One table per function.** Dozens of handlers in this repo name a local
    # variable `url`, and a single module-wide table would keep only the last
    # assignment, so the vast majority of endpoints go silently unseen -- the
    # first version missed the encyclopedia and dictionary sites this way, and the
    # encyclopedia was exactly the one that broke that day.
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hosts |= _hosts_fetched_in(scope, _function_bindings(scope,
                                                                 module_bindings))
    hosts |= _hosts_fetched_in(tree, module_bindings)
    return hosts


def _function_bindings(scope, module_bindings):
    """The URL literals a function can see: module-level constants, plus its own local assignments on top."""
    local = dict(module_bindings)
    local.update(_string_bindings(scope, seed=module_bindings))
    return local


def _request_url(node):
    """If `node` is a request-issuing call (`_http_get_json` / `.get` / `.post`), return its URL node."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    name = (func.attr if isinstance(func, ast.Attribute)
            else getattr(func, "id", ""))
    return (node.args[0] if name in ("_http_get_json", "_http_post_json", "get", "post")
            else None)


def _host_of(url_node, bindings):
    url = _literal_url(url_node, bindings)
    if not url or not url.startswith("http"):
        return None
    return urllib.parse.urlparse(url).netloc.lower()


def _hosts_fetched_in(scope, bindings):
    """The hosts hit by request-issuing calls in `scope` (nested included)."""
    hosts = set()
    for node in ast.walk(scope):
        url_node = _request_url(node)
        if url_node is not None:
            host = _host_of(url_node, bindings)
            if host:
                hosts.add(host)
    return hosts


def test_every_fetched_host_is_in_the_verifier():
    """Every external host the bot fetches must appear in `verify_external_apis`'s list.

    This rule is the **generalisation** of both incidents that day. Neither bug
    was a program-logic error, both were external-contract drift: the site changed
    its rules, our requests began to be refused, and the failure path always
    returned empty silently. Static analysis can never see this, only actually
    hitting it once can -- which is why `verify_external_apis.py` exists.

    But that scan is a **hand-written list**, and a hand-written list goes stale:
    the next time someone adds an endpoint, the scan silently misses it, and that
    endpoint returns to "nothing will notice it broke", which is where this all
    started. So here we go the other way and derive "which hosts are actually
    fetched" from the source, forcing the two sides to match.

    Only calls that really issue a request; links pasted for people do not count
    -- those breaking is only a dead link, not the whole feature vanishing.
    """
    covered = set()
    for entry in vx._ENDPOINTS:
        url = entry.get("url")
        if url:
            covered.add(urllib.parse.urlparse(url).netloc.lower())
    # The CDN entry has no fixed URL (it must first ask the API for an image); it
    # is represented in the list by the `cdn` group.
    covered.add("cdn.donmai.us")

    fetched = set()
    for path in (_BOT_SOURCE, _MODULE):
        fetched |= _fetched_hosts(path)

    missing = sorted(fetched - covered)
    assert not missing, (
        f"these hosts the bot fetches but are not in verify_external_apis.py's "
        f"list: {missing}. Add them to `_ENDPOINTS`, or nothing will notice when "
        "they break -- which is exactly the common cause of the two 2026-08-30 "
        "incidents.")


def test_the_verifier_does_not_list_hosts_nobody_fetches():
    """The reverse direction: the list should not contain hosts nobody hits any more.

    Keeping a stale endpoint means the scan shows a red nobody cares about; once
    red is normal, the whole scan is useless. This is the same lesson
    `test_language.py` recorded -- a guard that cries wolf ends up switched off.
    """
    fetched = set()
    for path in (_BOT_SOURCE, _MODULE):
        fetched |= _fetched_hosts(path)
    fetched.add("cdn.donmai.us")   # taken dynamically from a post's file_url, no literal to scan

    # `embed_only` endpoints do not take part in this comparison: the bot does not
    # fetch them, it just pastes the URL for the platform to fetch, so there is no
    # fetch call in the source to begin with.
    listed = {urllib.parse.urlparse(e["url"]).netloc.lower()
              for e in vx._ENDPOINTS
              if e.get("url") and not e.get("embed_only")}
    # `embed_only` is an exemption, so it must be **narrow**: an endpoint marked
    # embed_only that is actually fetched uses the exemption to switch the guard
    # off. Measured -- without this block, marking any real API embed_only would
    # exempt it from all checking, and nothing would complain.
    mislabelled = sorted(
        urllib.parse.urlparse(e["url"]).netloc.lower()
        for e in vx._ENDPOINTS
        if e.get("embed_only") and e.get("url")
        and urllib.parse.urlparse(e["url"]).netloc.lower() in fetched)
    assert not mislabelled, (
        f"these endpoints are marked embed_only, but the source actually fetches "
        f"them: {mislabelled}. embed_only means \"the bot does not fetch it, it "
        "just pastes the URL for the platform to fetch\" -- using it to exempt an "
        "endpoint that really is fetched switches this guard off.")

    stale = sorted(listed - fetched)
    assert not stale, (
        f"the source no longer hits these listed hosts: {stale}. Remove them, or "
        "the scan shows a red nobody cares about.")


def test_the_verifier_reuses_the_real_request_path():
    """The verifier must go through `_http_get_json`, not write its own HTTP call.

    Both of today's bugs were in the headers. A verifier that assembles its own
    request carries its own headers, so it is always green while the production
    path stays broken -- which is worse than no verification, because it gives a
    false sense that things were checked.
    """
    source = inspect.getsource(vx._check_json)
    assert "_http_get_json" in source, (
        "`_check_json` no longer goes through `_http_get_json` -- then it does not "
        "verify the request the bot actually sends (the headers differ), and it "
        "would catch neither of today's two bugs.")
    raw = inspect.getsource(vx._check_raw)
    assert "_user_agent()" in raw, (
        "`_check_raw` does not use `_user_agent()`, so the UA it verifies differs from the production path.")


def test_a_post_only_endpoint_is_never_checked_with_a_get():
    """A POST-only endpoint must go through the POST checker.

    Under `_check_json` is `_http_get_json`, which sends **GET only**. Using it to
    verify a POST-only GraphQL endpoint verifies a path the bot never walks --
    measured 2026-09-07, that gets a 404 "Use POST request to access graphql
    subdomain", entirely unrelated to the production path's result.

    Why this needs a guard: `_run`'s dispatch is a single
    `if entry.get("method") == "POST"`, and deleting it raises no error, it just
    silently sends that entry down a different path to verify.
    """
    routed = []

    async def fake_post(entry):
        routed.append(("POST", entry["group"]))
        return "OK", ""

    async def fake_json(entry):
        routed.append(("JSON", entry["group"]))
        return "OK", ""

    async def fake_raw(entry):
        routed.append(("RAW", entry["group"]))
        return "OK", ""

    saved = (vx._check_post, vx._check_json, vx._check_raw, vx._QUIET)
    vx._check_post, vx._check_json, vx._check_raw, vx._QUIET = (
        fake_post, fake_json, fake_raw, True)
    try:
        _run(vx._run(None))
    finally:
        (vx._check_post, vx._check_json, vx._check_raw,
         vx._QUIET) = saved

    post_groups = {e["group"] for e in vx._ENDPOINTS
                   if e.get("method") == "POST"}
    assert post_groups, "no POST endpoints in the list any more? then this guard needs rethinking"
    for group in post_groups:
        assert ("POST", group) in routed, (
            f"`{group}` declares method=POST but did not go through the POST checker")
        assert ("JSON", group) not in routed, (
            f"`{group}` is POST-only but was verified by `_check_json` (GET-only) -- "
            "verifying a path the bot never walks")


def _per_call_timeout_sites(path):
    """Every "has its own timeout" external call in this file: `[(NAME, value, host set, lineno), ...]`.

    Two shapes:
    * `_http_get_json(url, timeout=NAME)` -- the host is that call's own URL;
    * `aiohttp.ClientTimeout(total=NAME)` (the few sessions the bot opens itself:
      anime database, grid download, reverse image search) -- the host is taken
      from the request-issuing call **in the same function**, the same one-table-
      per-function scope as `_fetched_hosts`. When the URL is decided at runtime
      (the grid download URL comes from an API response) it is an empty set,
      handled separately by the reconciliation side.

    `ClientTimeout(total=parameter)` is a pass-through (this is how
    `_http_get_json` hands the caller's timeout to the session) and is not counted
    as a timeout source; skip it.

    A timeout written as a literal number is a straight failure: what the verifier
    copies is **the value of that name**, and an unnamed number cannot be
    reconciled, only remembered by a person -- which is exactly what this exists to
    remove. A value that cannot be resolved (not a literal constant) is recorded as
    None, so the reconciliation side can say which one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    consts = {}
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) and node.value
                   else [])
        for target in targets:
            if isinstance(target, ast.Name):
                try:
                    consts[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    consts[target.id] = None
    module_bindings = _string_bindings(tree, module_level_only=True)
    sites = []

    def named(value, lineno, what):
        assert isinstance(value, ast.Name), (
            f"{what} at {path.name}:{lineno} uses an unnamed timeout "
            f"`{ast.unparse(value)}` -- give it a module-level constant so the verifier can copy and reconcile it.")
        return value.id

    def visit(node, func):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node
        if isinstance(node, ast.Call):
            callee = node.func
            name = (callee.attr if isinstance(callee, ast.Attribute)
                    else getattr(callee, "id", ""))
            def bindings():
                return (_function_bindings(func, module_bindings)
                        if func is not None else module_bindings)
            if name in ("_http_get_json", "_http_post_json"):
                for kw in node.keywords:
                    if kw.arg == "timeout":
                        ident = named(kw.value, node.lineno, f"`{name}`")
                        host = (_host_of(node.args[0], bindings()) if node.args
                                else None)
                        sites.append((ident, consts.get(ident),
                                      {host} if host else set(), node.lineno))
            elif name == "ClientTimeout":
                total = next((kw.value for kw in node.keywords if kw.arg == "total"),
                             node.args[0] if node.args else None)
                params = ({a.arg for a in func.args.args + func.args.kwonlyargs}
                          if func is not None else set())
                if total is not None and not (isinstance(total, ast.Name)
                                              and total.id in params):
                    ident = named(total, node.lineno, "`ClientTimeout`")
                    hosts = (_hosts_fetched_in(func, bindings())
                             if func is not None else set())
                    sites.append((ident, consts.get(ident), hosts, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(tree, None)
    return sites


def _per_call_timeouts(path):
    """`_per_call_timeout_sites` collapsed to `NAME -> module-level constant value`."""
    return {name: value for name, value, _hosts, _line
            in _per_call_timeout_sites(path)}


# A timeout whose URL is decided at runtime, so its host cannot be statically
# scanned -> the group that represents it in the verifier. Same reason as the
# `_fetched_hosts` consumer manually adding `cdn.donmai.us`: the grid download URL
# comes from each image's `file_url` in the API response. This table is reconciled
# against the scan result in both directions, so it cannot go stale unnoticed.
_DYNAMIC_HOST_TIMEOUTS = {"GRID_DOWNLOAD_TIMEOUT_SEC": "cdn"}


def test_a_per_call_timeout_in_the_bot_is_mirrored_here():
    """A timeout the bot loosened for some external call, the verifier entry must copy the same value, reconciled both ways.

    On 2026-09-08 the dictionary call was loosened to `DICT_TIMEOUT_SEC` (the
    endpoint stably takes ~20s), while the verifier entry stayed at the default
    15s until noticed on 2026-09-19 -- so it reported "unreachable" while the bot
    was fine. The reverse (verifier looser than the bot) is worse: the bot is
    broken, this reports ok. So both sides must match, and the verifier may not
    have an entry "loosened on its own but matching no bot constant".
    """
    bot = {}
    for path in (_BOT_SOURCE, _MODULE):
        bot.update(_per_call_timeouts(path))
    # Positive control: the scan really finds something. An empty scan result
    # looks identical to "the two sides agree".
    assert "DICT_TIMEOUT_SEC" in bot, (
        f"the dictionary call's timeout can no longer be scanned (scanned: {sorted(bot)}) -- "
        "the scan itself is broken, or that call was renamed; fix the scan first, "
        "do not let this spin idle.")
    unresolved = sorted(name for name, value in bot.items() if value is None)
    assert not unresolved, f"these timeout constants cannot be resolved to a value: {unresolved}"

    mirrored = {e["bot_timeout"]: e.get("timeout")
                for e in vx._ENDPOINTS if e.get("bot_timeout")}
    missing = sorted(set(bot) - set(mirrored))
    assert not missing, (
        f"the bot has its own timeout for these calls but the verifier does not "
        f"copy it: {missing}. Add `timeout` and `bot_timeout` to the matching "
        "`_ENDPOINTS` entry.")
    stale = sorted(set(mirrored) - set(bot))
    assert not stale, f"the verifier copies these timeouts, but the bot side no longer has them: {stale}"
    wrong = {name: (mirrored[name], bot[name]) for name in bot
             if mirrored[name] != bot[name]}
    assert not wrong, f"the verifier's timeout differs from the bot's (verifier, bot): {wrong}"
    loose = sorted(e["group"] for e in vx._ENDPOINTS
                   if "timeout" in e and not e.get("bot_timeout"))
    assert not loose, (
        f"these entries changed their timeout but match no bot constant: {loose} -- "
        "that is \"the verifier looser than the bot\", reporting ok while the bot "
        "is broken.")

    # Added 2026-09-19: the three sessions the bot opens itself
    # (`ClientTimeout(total=NAME)`) are also in the name set above. **Matching
    # names is not enough, it must match the right entry**: hang the anime
    # database's timeout on the reverse-image entry and both name and value still
    # match, but it verifies a different endpoint. So reconcile once more by "the
    # host hit in the same function", both directions.
    sites = [site for path in (_BOT_SOURCE, _MODULE)
             for site in _per_call_timeout_sites(path)]
    assert {"ANIME_TIMEOUT_SEC", "IQDB_TIMEOUT_SEC",
            "GRID_DOWNLOAD_TIMEOUT_SEC"} <= {name for name, *_ in sites}, (
        "the timeouts of the sessions the bot opens itself can no longer be "
        "scanned -- the `ClientTimeout(total=...)` half of the scan is broken "
        f"(scanned: {sorted({name for name, *_ in sites})})")
    hosts_of = {}
    for name, _value, hosts, _line in sites:
        hosts_of.setdefault(name, set()).update(hosts)

    def entry_host(e):
        return urllib.parse.urlparse(e["url"]).netloc.lower() if e.get("url") else None

    dynamic = sorted(name for name, hosts in hosts_of.items() if not hosts)
    assert dynamic == sorted(_DYNAMIC_HOST_TIMEOUTS), (
        f"the timeouts whose host cannot be scanned are {dynamic}, "
        f"`_DYNAMIC_HOST_TIMEOUTS` lists {sorted(_DYNAMIC_HOST_TIMEOUTS)} -- a new "
        "one must say which group it maps to, and an old one that is no longer a "
        "dynamic URL should be removed.")
    wrong_home = []
    for name, hosts in hosts_of.items():
        carriers = [e for e in vx._ENDPOINTS if e.get("bot_timeout") == name]
        if hosts:
            wrong_home += [f"{name} is hung on {e['group']} (host {entry_host(e)})"
                           for e in carriers if entry_host(e) not in hosts]
            wrong_home += [f"{e['group']} ({entry_host(e)}) has no {name} hung on it"
                           for e in vx._ENDPOINTS
                           if entry_host(e) in hosts and e.get("bot_timeout") != name]
        else:
            groups = sorted(e["group"] for e in carriers)
            if groups != [_DYNAMIC_HOST_TIMEOUTS[name]]:
                wrong_home.append(f"{name} is hung on {groups}, should be "
                                  f"{[_DYNAMIC_HOST_TIMEOUTS[name]]}")
    assert not wrong_home, (
        f"a timeout was copied onto the wrong entry: {wrong_home}. Same value does not help -- it verifies a different endpoint.")

    # The non-JSON / POST check functions fall back to `_EMBED_ONLY_TIMEOUT_SEC`
    # when there is no `timeout`, and that default is only allowed for the
    # embed_only entries the chat platform fetches itself (the bot side has no
    # timeout to copy).
    defaulted = sorted(e["group"] for e in vx._ENDPOINTS
                       if (e.get("raw") or e.get("method") == "POST")
                       and "timeout" not in e and not e.get("embed_only"))
    assert not defaulted, (
        f"these entries the bot fetches itself, yet use the verifier's own default timeout: {defaulted}.")


def test_the_dictionary_probe_cannot_be_answered_from_a_cache():
    """The dictionary entry must query a **new** word every run, or the CDN's stale cache lets it report ok.

    Measured 2026-09-19: when the origin is unreachable, a word already queried
    (e.g. the old code's fixed `serendipity`) gets a weeks-old cache (HTTP 200),
    and only a never-queried word exposes the 522.
    """
    words = {vx._fresh_probe_word() for _ in range(50)}
    assert len(words) == 50, "the query word does not change each time; the cache would cover for the broken origin"
    entry = next(e for e in vx._ENDPOINTS if e["group"] == "dict")
    assert entry["url"].rsplit("/", 1)[1] == vx._DICT_PROBE_WORD, (
        f"the dictionary entry does not use a per-run generated query word: {entry['url']}")
    assert 404 in entry.get("ok_statuses", ()), (
        "querying a nonexistent word, a healthy origin returns 404 -- without treating 404 as healthy, this entry is forever red")


@pytest.mark.parametrize("group,status,expected", [
    ("dict", 404, "OK"),     # a declared "non-200 when healthy"
    ("dict", 522, "FAIL"),   # the one actually received 2026-09-19
    ("xkcd", 404, "FAIL"),   # near-miss: an undeclared endpoint, 404 means broken
    ("dict", 200, "OK"),
])
def test_an_expected_non_200_is_healthy_only_where_declared(monkeypatch, group,
                                                            status, expected):
    calls = []

    async def fake_get(url, *, params=None, headers=None,
                       timeout=ex._HTTP_TIMEOUT_SEC, quiet_statuses=()):
        calls.append({"timeout": timeout, "quiet": tuple(quiet_statuses)})
        return status, ([{"word": "x"}] if status == 200 else None)

    async def no_network(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(ex, "_http_get_json", fake_get)
    monkeypatch.setattr(vx, "_error_body", no_network)
    entry = next(e for e in vx._ENDPOINTS if e["group"] == group)
    verdict, _detail = asyncio.run(vx._check_json(entry))
    assert verdict == expected
    assert len(calls) == 1
    # The timeout and quiet statuses must really be passed down, not just written in the list.
    assert calls[0]["timeout"] == entry.get("timeout", ex._HTTP_TIMEOUT_SEC)
    assert calls[0]["quiet"] == tuple(entry.get("ok_statuses", ()))


def test_a_5xx_is_blamed_on_the_upstream_not_our_headers():
    """A 522 is the upstream erroring itself; do not send people to check the User-Agent or `api_contact`."""
    out = vx._diagnose(522, body="error code: 522")
    assert "upstream" in out, f"5xx did not say it is the upstream's problem: {out!r}"
    assert "User-Agent" not in out and ex._UA_CONTACT_KEY not in out, (
        f"5xx was blamed on the headers: {out!r}")
    # near-miss: a 4xx not listed in any branch still gives no advice.
    assert vx._diagnose(404, body=None) == ""


def test_the_diagnosis_quotes_the_upstream_instead_of_guessing():
    """When the upstream gives the reason, stop guessing User-Agent.

    The fixture is the 403 body actually received from the anime database on
    2026-09-07. The diagnosis at the time reported it as "this kind is almost
    always a User-Agent problem" and told the user to set `api_contact` -- while
    no UA, browser UA, and our UA were all measured as 403, so UA was not a
    variable at all.

    **Deliberately GET** to verify this one: that way it is affected only by the
    "quote the upstream" branch, and does not mask the "non-GET does not apply the
    UA story" test below. The two tests each target one branch.
    """
    body = json.dumps({"errors": [{
        "message": "The AniList API has been temporarily disabled due to "
                   "severe stability issues.",
        "status": 403}]})
    out = vx._diagnose(403, body=body, method="GET")
    assert "temporarily disabled" in out, f"did not quote the upstream's explanation: {out!r}"
    assert "User-Agent" not in out, (
        f"the upstream already gave the reason, yet it is still guessing User-Agent: {out!r}")
    assert ex._UA_CONTACT_KEY not in out, (
        f"the upstream already gave the reason, yet it is still telling the user to set a contact: {out!r}")


def test_a_non_get_failure_does_not_blame_the_user_agent():
    """A non-GET endpoint's 4xx does not apply the UA story.

    **Deliberately no body**: that way it is affected only by the "method != GET"
    branch, isolated from the test above.
    """
    out = vx._diagnose(403, body=None, method="POST")
    assert "User-Agent" not in out or "do not assume it is the User-Agent" in out, (
        f"forced the UA story onto a POST endpoint: {out!r}")
    assert ex._UA_CONTACT_KEY not in out, (
        f"told the user to set a contact for a POST endpoint; that key only affects the shared GET path: {out!r}")


def test_a_plain_get_403_still_gets_the_actionable_ua_hint():
    """A reverse guard: the originally **correct** half must not be lost while narrowing the misreport.

    The encyclopedia's 403 really is a UA / contact problem, and its failure page
    yields no JSON message. This ensures "do not guess" was not turned into "say
    nothing".
    """
    out = vx._diagnose(403, body="<html>just a moment</html>", method="GET")
    assert "User-Agent" in out
    assert ex._UA_CONTACT_KEY in out or ex._configured_contact()


def test_the_upstream_extractor_never_invents_a_message():
    """When nothing can be extracted, return an empty string, do not make it up."""
    assert vx._upstream_message(None) == ""
    assert vx._upstream_message("") == ""
    assert vx._upstream_message("<html>Just a moment...</html>") == ""
    assert vx._upstream_message("not json at all") == ""
    assert vx._upstream_message(json.dumps([1, 2, 3])) == ""
    assert vx._upstream_message(json.dumps({"errors": [{}]})) == ""


def test_the_upstream_message_is_bounded_and_single_line():
    """The body is third-party bytes: truncate, squash to one line, do not let a whole page spew into the console."""
    long_msg = ("x" * 5000) + "\n" + ("y" * 5000)
    out = vx._upstream_message(json.dumps({"message": long_msg}), limit=100)
    assert len(out) <= 100
    assert "\n" not in out and "\r" not in out


# ---------------------------------------------------------------------------
# Live layer: actually hit it once, to confirm we are not currently blocked
# ---------------------------------------------------------------------------

def _live_status(url, params, headers):
    """Make one real request, return the HTTP status code; None if unreachable (-> skip, not red)."""
    import aiohttp

    async def go():
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(url, params=params,
                                       headers=headers) as resp:
                    return resp.status
        except Exception:  # pylint: disable=broad-except
            return None

    return asyncio.run(go())


@pytest.mark.parametrize("label,url,params", [
    ("posts", ex.DANBOORU_API, {"tags": "rossi_(arknights)", "limit": 1}),
    ("tags", ex.DANBOORU_TAGS_API, {"search[name]": "yuri", "limit": 1}),
])
def test_our_user_agent_is_still_accepted(label, url, params):
    """Does the site still accept our UA?

    This is the only test in the whole file that catches "the site changed its
    rules" -- a change no static analysis can see, with a failure path that always
    returns empty silently. On 2026-08-30 the answer was 403, and had been 403 for
    a while, with nothing ever going red.

    Unreachable network -> skip (an offline environment should have no red).
    Reachable but refused -> red, because that really is broken.
    """
    status = _live_status(url, params, {"User-Agent": ex._BOT_UA})
    if status is None:
        pytest.skip("cannot reach the external site (offline?) -- this only means anything when reachable")
    assert status == 200, (
        f"Danbooru /{label} returned HTTP {status} with our UA. A 403 means the "
        f"site's protection is blocking us again (UA={ex._BOT_UA!r}); adjust the "
        "UA per Help:Api, do not switch to pretending to be a browser -- measured, "
        "that gets blocked too.")


# ---------------------------------------------------------------------------
# Contact: some sites do not accept a "name only" UA
# ---------------------------------------------------------------------------

def test_the_contact_is_folded_into_the_user_agent(fake_http, monkeypatch):
    """When `api_contact` is set, carry it in the site's required format.

    Measured 2026-08-30 on Wikimedia's REST API, three UAs and three results:
        no UA at all          -> 403 "Please set a user-agent..."
        name-only UA          -> 403 "...Contact bot-traffic@wikimedia.org..."
        UA with a URL or email -> 200
    The second is not saying we did not identify ourselves, it is saying we
    **left no contact**. These are two different things, and this test pins that
    the difference is actually implemented.
    """
    monkeypatch.setattr(ex, "_configured_contact", lambda: "https://example.test/bot")
    fake_http.calls = []
    _run(ex._http_get_json("https://example.invalid/x"))
    ua = fake_http.calls[0]["headers"]["User-Agent"]
    assert "https://example.test/bot" in ua, f"the contact did not make it into the UA: {ua!r}"
    assert "axiomatic" in ua, f"carrying a contact dropped the self-identification: {ua!r}"


def test_no_contact_configured_still_sends_a_usable_user_agent(fake_http,
                                                               monkeypatch):
    """With no contact configured there must **still** be a UA.

    This is the default state, and most sites (the image boards) are satisfied by
    self-identification alone. If "no contact configured" were implemented as
    "then send no UA", it would undo the whole already-fixed 403.
    """
    monkeypatch.setattr(ex, "_configured_contact", lambda: "")
    fake_http.calls = []
    _run(ex._http_get_json("https://example.invalid/x"))
    assert fake_http.calls[0]["headers"]["User-Agent"] == ex._BOT_UA


def test_a_broken_config_does_not_take_the_user_agent_down(monkeypatch):
    """When the config is broken / unreadable, fall back to an empty string, not blow up every external request."""
    def boom():
        raise OSError("config gone")
    monkeypatch.setattr(ex, "load_bot_config", boom)
    assert ex._configured_contact() == ""
    assert ex._user_agent() == ex._BOT_UA


def test_a_403_without_a_contact_says_which_config_key_to_set(fake_http,
                                                              capsys,
                                                              monkeypatch):
    """403 + no contact set -> the diagnosis must name that key.

    "HTTP 403" alone does not reveal this: we **do** carry a UA, and the status
    code will not say "what you lack is a contact". Without this line, the next
    person has to re-run today's whole experiment to learn what to change.
    """
    monkeypatch.setattr(ex, "_configured_contact", lambda: "")
    fake_http.replies = [(403, None)]
    capsys.readouterr()
    _run(ex._http_get_json("https://example.invalid/x"))
    err = capsys.readouterr().err
    assert ex._UA_CONTACT_KEY in err, f"did not say which key to set: {err!r}"


def test_the_contact_hint_stays_quiet_once_it_is_configured(fake_http, capsys,
                                                            monkeypatch):
    """The reverse: once it is set, a 403 should not keep telling the user to set it -- that becomes misleading."""
    monkeypatch.setattr(ex, "_configured_contact", lambda: "https://example.test/bot")
    fake_http.replies = [(403, None)]
    capsys.readouterr()
    _run(ex._http_get_json("https://example.invalid/x"))
    err = capsys.readouterr().err
    assert "403" in err, "the 403 itself must still leave a line"
    assert ex._UA_CONTACT_KEY not in err, f"already set, yet still telling the user to set it: {err!r}"


def test_the_config_key_exists_and_defaults_to_empty():
    """`api_contact` must really be a config key, and default to empty.

    Defaulting to empty is deliberate: this string is sent to third-party sites,
    and what to put there is the owner's decision. The code should not pick one
    for them, and above all should not hardcode their email into the source.
    """
    from _bot_config import load_bot_config as real_load
    cfg = real_load()
    assert ex._UA_CONTACT_KEY in cfg, (
        f"`{ex._UA_CONTACT_KEY}` is not in bot_config's output -- the `_coerce` "
        "section missed it, so no config value ever takes effect.")
    assert isinstance(cfg[ex._UA_CONTACT_KEY], str)


@pytest.mark.parametrize("contact,expected", [
    ("", False),
    ("   ", False),
    ("https://example.test/bot", True),
])
def test_a_blank_contact_counts_as_unset(contact, expected, monkeypatch,
                                         fake_http):
    """A config value that is only whitespace counts as unset -- otherwise the UA becomes `axiomatic-bot/1.0 (   )`."""
    monkeypatch.setattr(ex, "load_bot_config", lambda: {ex._UA_CONTACT_KEY: contact})
    assert bool(ex._configured_contact()) is expected


def test_wikimedia_really_does_want_a_contact_not_just_a_name():
    """Live layer: for Wikimedia, is a name-only UA really still not enough?

    This pins "why `api_contact` as a setting exists at all". The day the site
    loosens, this goes red, which is the signal to rewrite this setting and its
    documentation -- keeping a no-longer-needed config key is worse than not
    having it.

    Unreachable network -> skip.
    """
    url = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
           "Python_(programming_language)")
    bare = _live_status(url, None, {"User-Agent": ex._BOT_UA})
    if bare is None:
        pytest.skip("cannot reach the external site (offline?)")
    withc = _live_status(url, None, {
        "User-Agent": "axiomatic-bot/1.0 (https://example.test/bot)"})
    assert withc == 200, (
        f"even a UA with a contact does not get 200 (HTTP {withc}) -- the site's "
        "rules changed again, re-measure before changing `_user_agent`'s format.")
    assert bare != 200, (
        "a name-only UA now passes too. The site loosened its rules -- rewrite "
        "`api_contact`'s documentation (or drop it entirely), do not keep a config "
        "key that no longer has a reason.")


def test_the_default_user_agent_is_applied_in_the_one_place_it_can_be():
    """The default UA must be filled in inside `_http_get_json`, not scattered across call sites.

    The version written as "each call site remembers to carry it" is exactly the
    one that broke: miss one and it is a silent 403, and missing one is the norm.
    Filling it at the single exit is the structurally sound way.

    (This used to also have a string comparison for "the module must not contain
    an expiring comment like 'site X does not need a UA'". Removed -- this file's
    own docstrings would quote that sentence, so it would be forever red. A
    banned-word list is prone to false positives; `test_language.py` already
    recorded the same lesson: a guard that cries wolf ends up switched off. What
    to really pin is the invariant below.)
    """
    # Look at the **call**, not a string: this used to ask `"_BOT_UA" in source`,
    # and matched the docstring line "see `_BOT_UA`" -- the code actually calls
    # `_user_agent()`, so deleting that line kept it green.
    exit_tree = ast.parse(inspect.getsource(ex._http_json))
    called = {c.func.id for c in ast.walk(exit_tree)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "_user_agent" in called, (
        "`_http_json` no longer fills in the default UA -- that is the one safeguard for every site.")
    for name in ("_danbooru_posts", "_fetch_danbooru_posts_latest",
                 "_query_tags_json"):
        body = inspect.getsource(getattr(ex, name))
        # Look for the **quoted string literal used as a dict key**, not a mention
        # of these words in a comment / docstring -- the first version mistakenly
        # hit its own documentation this way.
        assert chr(34) + 'User-Agent' + chr(34) not in body, (
            f"{name} stuffed in a User-Agent itself. The default belongs to "
            "`_http_get_json`; scattering it back to call sites returns to the "
            "\"miss one and get a silent 403\" road. When a site really has its own "
            "rule, go through an explicit data structure like `_TagsAPI.headers`.")


# ---------------------------------------------------------------------------
# Tag resolution: `_best_tag_for_window`
#
# Found by coverage on 2026-09-01 -- this was the **only** function across all
# shared / support modules with more than 8 statements and not a single line ever
# run. It decides which tag the user's free text maps to, and getting it wrong
# means "search returns completely unrelated images" or "it exists but says not
# found", with neither leaving any error message.
#
# Its docstring records three decisions **bought with real site data**:
# `post_count > 0` (the site has many stale zero-post tags), a single token using
# prefix `tok*` not substring `*tok*` (`*rossi*` pulls `animal_crossing` to the
# top), and `.get("name")` rather than `["name"]` (when the site omits a field,
# KeyError bubbles all the way up to the mention dispatcher and turns "tag not
# found" into an error reply). None of the three had a guard originally.
# ---------------------------------------------------------------------------

def _stub_tags(monkeypatch, *replies):
    """Replace `_query_tags_json`, returning a list of "the keywords each call received".

    The assertion is on **what the sent query looks like**, not just the return
    value -- two of the three decisions above (prefix vs substring, short token
    skips fuzzy) are only visible in the query string.
    """
    calls = []
    queued = list(replies)

    async def _fake(api, **kw):
        del api
        calls.append(kw)
        return queued.pop(0) if queued else []

    monkeypatch.setattr(ex, "_query_tags_json", _fake)
    return calls


def _best(window):
    return _run(ex._best_tag_for_window(ex._DANBOORU_TAGS, window))


def test_a_tag_object_without_a_name_is_not_a_hit(monkeypatch):
    """When the site omits the `name` field, treat it as a miss, do not let KeyError bubble out.

    This is the real fault recorded in the `.get("name")` comment: the exception
    bubbles all the way up to the mention dispatcher's catch-all, so the user sees
    not "this tag is not found" but a generic error message -- same symptom,
    completely different cause, the hardest kind to investigate.
    """
    _stub_tags(monkeypatch, [{"post_count": 500}])
    assert _best(["surtr"]) is None


@pytest.mark.parametrize("name", [None, 123, "", [], {}])
def test_a_non_string_name_is_not_a_hit(monkeypatch, name):
    _stub_tags(monkeypatch, [{"name": name, "post_count": 500}])
    assert _best(["surtr"]) is None


@pytest.mark.parametrize("count", [0, None, -1])
def test_a_zero_post_tag_is_never_returned(monkeypatch, count):
    """The site has many stale tags with post_count 0. Matching them means searching for an empty result."""
    _stub_tags(monkeypatch, [{"name": "stale_tag", "post_count": count}],
               [{"name": "stale_tag", "post_count": count}])
    assert _best(["surtr"]) is None


def test_an_exact_hit_never_runs_the_fuzzy_query(monkeypatch):
    """An exact hit finishes the job -- an extra fuzzy call wastes an outbound request."""
    calls = _stub_tags(monkeypatch, [{"name": "surtr_(arknights)",
                                      "post_count": 900}])
    assert _best(["surtr"]) == "surtr_(arknights)"
    assert len(calls) == 1, f"queried again after an exact hit: {calls}"
    assert calls[0].get("name") == "surtr", calls[0]


def test_a_short_single_token_never_runs_the_fuzzy_query(monkeypatch):
    """For a <=2-char token like `cp` / `bb` / `ru`, the most popular match for
    `cp*` is something completely unrelated like `cpu_(hexivision)`, almost always
    wrong. Exact is still attempted."""
    short = "x" * (ex._FUZZY_MIN_TOKEN_LEN - 1)
    calls = _stub_tags(monkeypatch, [])          # exact misses
    assert _best([short]) is None
    assert len(calls) == 1, f"a short token still ran fuzzy: {calls}"


def test_a_long_single_token_falls_back_to_a_prefix_not_a_substring(monkeypatch):
    """Fuzzy uses `tok*`, **not** `*tok*`.

    A middle substring drags in unrelated tags -- the docstring's example is
    `*rossi*` pulling `animal_crossing` to the top. This is only visible in the
    sent query string, so the assertion here is on the shape of `name_matches`,
    not the return value.
    """
    calls = _stub_tags(monkeypatch, [],          # exact misses
                       [{"name": "surtr_(arknights)", "post_count": 900}])
    assert _best(["surtr"]) == "surtr_(arknights)"
    assert len(calls) == 2, calls
    pattern = calls[1].get("name_matches")
    assert pattern == "surtr*", f"fuzzy used {pattern!r}, not a prefix"
    assert not pattern.startswith("*"), (
        "a leading `*` makes it a middle-substring match -- `*rossi*` matches "
        "`animal_crossing`, whose post_count is far higher than the one the user actually wants")


def test_multiple_tokens_use_an_ordered_substring_pattern(monkeypatch):
    """Only multiple tokens use `*a*b*`: several ordered substrings are precise enough."""
    calls = _stub_tags(monkeypatch,
                       [{"name": "surtr_(arknights)", "post_count": 900}])
    assert _best(["surtr", "arknights"]) == "surtr_(arknights)"
    assert len(calls) == 1, "multiple tokens should not run an exact query first"
    assert calls[0].get("name_matches") == "*surtr*arknights*", calls[0]


def test_an_empty_window_asks_nothing(monkeypatch):
    calls = _stub_tags(monkeypatch)
    assert _best([]) is None
    assert not calls, "an empty window still sent a request"


def test_the_public_contact_predicate_matches_the_configured_value(monkeypatch):
    """`contact_configured()` is what the bot side uses to decide "should it say one more sentence".

    It is a thin wrapper over `_configured_contact()`, so it is easy to treat as
    not worth testing -- but inverting it (`not`) makes no caller's test go red:
    those tests mostly replace it directly. Measured, this mutant survived until
    this test was added.
    """
    for raw, expected in (("", False), ("   ", False), (None, False),
                          ("https://example.test/bot", True),
                          ("someone@example.test", True)):
        monkeypatch.setattr(ex, "load_bot_config",
                            lambda raw=raw: {"api_contact": raw})
        assert ex.contact_configured() is expected, repr(raw)


def test_the_contact_predicate_never_raises(monkeypatch):
    """When config cannot be read, return False (fail-closed: better to say one sentence less than to blow up the caller)."""
    def boom():
        raise OSError("config unreadable")

    monkeypatch.setattr(ex, "load_bot_config", boom)
    assert ex.contact_configured() is False


# ---------------------------------------------------------------------------
# `_resolve_fuzzy_tags`'s greedy longest match
#
# The section above verifies "which tag a single window matches"; this section
# verifies **how the windows are cut**. Measuring coverage on 2026-09-06 found 18
# of these 29 lines never run, and what it does is **rewrite the query the user
# typed** -- the symptom of a wrong cut is not "not found" but "silently find
# something else", because the screen does not show the query was tampered with.
#
# The fake `_query_tags_json` here answers from a table (keyed by the sent `name`
# or `name_matches`), so that "which windows were tried, in what order" can be
# verified.
# ---------------------------------------------------------------------------

def _resolve_with(monkeypatch, table: dict, raw: str):
    """Return `(result, list of sent queries)`. `table`'s keys are `name` or `name_matches`."""
    calls = []

    async def _fake(api, *, name=None, name_matches=None, order=None, limit=5):
        del api, order, limit
        key = name if name is not None else name_matches
        calls.append(key)
        return list(table.get(key, []))

    monkeypatch.setattr(ex, "_query_tags_json", _fake)
    return _run(ex._resolve_fuzzy_tags(ex._DANBOORU_TAGS, raw)), calls


def _tag_hit(name: str, count: int = 100) -> list:
    return [{"name": name, "post_count": count}]


def test_nothing_resolved_returns_none(monkeypatch):
    """When not a single token was rewritten, return None.

    Returning a string identical to the input would make the caller think
    resolution succeeded, run the same query again, and the user sees the same
    "not found" -- just after spending one more round of API quota.
    """
    out, _ = _resolve_with(monkeypatch, {}, "aaa bbb")
    assert out is None


def test_an_empty_query_never_touches_the_api(monkeypatch):
    out, calls = _resolve_with(monkeypatch, {}, "   ")
    assert out is None
    assert calls == []


def test_a_token_that_resolves_to_itself_is_not_a_rewrite(monkeypatch):
    """An exact hit whose name did not change -> no rewrite, still return None."""
    out, _ = _resolve_with(monkeypatch, {"yuri": _tag_hit("yuri")}, "yuri")
    assert out is None


def test_the_longest_window_wins(monkeypatch):
    """`lappland decadenza` must be treated as **one** tag, not matched separately.

    Trying from the longest window down to the shortest is exactly for this:
    matching separately would give two tags that each exist but together are not
    at all what the user wanted, and the search result looks like "there is
    something", so nobody notices the mismatch.
    """
    table = {
        "*lappland*decadenza*": _tag_hit("lappland_the_decadenza_(arknights)"),
        "lappland*": _tag_hit("lappland_(arknights)"),
        "decadenza*": _tag_hit("decadenza_(something_else)"),
    }
    out, calls = _resolve_with(monkeypatch, table, "lappland decadenza")
    assert out == "lappland_the_decadenza_(arknights)"
    assert calls[0] == "*lappland*decadenza*", (
        f"did not start from the longest window: {calls}")


def test_tokens_after_a_matched_window_are_still_resolved(monkeypatch):
    """After matching a window, the cursor must jump to its end and continue, not restart and not stop."""
    table = {
        "*lappland*decadenza*": _tag_hit("lappland_the_decadenza_(arknights)"),
        "yuri": _tag_hit("yuri_tag"),
    }
    out, _ = _resolve_with(monkeypatch, table, "lappland decadenza yuri")
    assert out == "lappland_the_decadenza_(arknights) yuri_tag"


def test_an_unmatched_token_is_kept_verbatim(monkeypatch):
    """An unmatched token is kept as-is. Dropping it silently loosens the user's search."""
    table = {"lappl*": _tag_hit("lappland_(arknights)")}
    out, _ = _resolve_with(monkeypatch, table, "lappl zzzz")
    assert out == "lappland_(arknights) zzzz"


def test_a_window_that_spans_a_modifier_is_never_tried(monkeypatch):
    """A window containing a modifier is skipped entirely -- `rating:general` must not be spliced into `*a*b*`.

    The existing test verifies "do not query when there is only a modifier"; this
    one verifies "when a modifier sits in the middle, the windows spanning it must
    not be queried either", which is a different path.
    """
    table = {"aaa": _tag_hit("aaa_tag")}
    out, calls = _resolve_with(monkeypatch, table, "aaa rating:general bbb")
    assert out == "aaa_tag rating:general bbb"
    assert all("rating:general" not in (c or "") for c in calls), calls


def test_a_modifier_keeps_its_position(monkeypatch):
    """A modifier must stay in place. Moving it changes the scope it applies to."""
    table = {"aaa": _tag_hit("aaa_tag")}
    out, _ = _resolve_with(monkeypatch, table, "score:>=5 aaa")
    assert out == "score:>=5 aaa_tag"


# A token count clearly over `_FUZZY_MAX_TOKENS`, to confirm the cap really truncates.
_FUZZY_OVERFLOW = 40


def test_a_very_long_query_is_capped(monkeypatch):
    """The cap exists so a long input does not fire off dozens of API calls, each of which could be rate-limited."""
    raw = " ".join(f"tok{i}" for i in range(_FUZZY_OVERFLOW))
    out, calls = _resolve_with(monkeypatch, {}, raw)
    assert out is None
    joined = " ".join(str(c) for c in calls)
    assert f"tok{ex._FUZZY_MAX_TOKENS}" not in joined, (
        f"processed a token past the cap (cap {ex._FUZZY_MAX_TOKENS})")



# ---------------------------------------------------------------------------
# The verifier must not be looser than the bot
# ---------------------------------------------------------------------------
# A timeout in the verifier that **has a name but does not come from the
# `_ENDPOINTS` cell** is only allowed in a designated function: `_error_body` is
# "hit it once more after already failing, only to grab the upstream explanation",
# and decides no verdict.
_VERIFIER_NAMED_TIMEOUT_HOMES = {"_ERROR_BODY_TIMEOUT_SEC": "_error_body"}
# The default in `entry.get("timeout", default)` may only be one of these two: the
# default JSON shares with the bot, and the one used only for embed_only (the bot
# does not fetch it, there is no timeout to copy).
_VERIFIER_TABLE_DEFAULTS = {"_HTTP_TIMEOUT_SEC", "_EMBED_ONLY_TIMEOUT_SEC"}


def _verifier_timeout_offenders(tree):
    """Timeouts in the verifier that **do not come from the reconciled table**:
    return `(offender list, _http_get_json call count, ClientTimeout call count)`.

    Looks at two shapes: `_http_get_json(..., timeout=X)` and
    `ClientTimeout(total=X)`. X must be `entry.get("timeout", default)` (written
    directly, or assigned to a name first then passed), with the default only a
    name in `_VERIFIER_TABLE_DEFAULTS`; or a name listed in
    `_VERIFIER_NAMED_TIMEOUT_HOMES`, and only in the function it designates. A
    hardcoded number is always an offence -- that bypasses the reconciliation with
    the bot.
    """
    def default_ok(node):
        ident = (node.attr if isinstance(node, ast.Attribute)
                 else node.id if isinstance(node, ast.Name) else None)
        return ident in _VERIFIER_TABLE_DEFAULTS

    def from_the_table(value, scope):
        if isinstance(value, ast.Name):
            if _VERIFIER_NAMED_TIMEOUT_HOMES.get(value.id) == scope.name:
                return True
            bound = [n.value for n in ast.walk(scope)
                     if isinstance(n, ast.Assign)
                     and any(getattr(t, "id", None) == value.id for t in n.targets)]
            return len(bound) == 1 and from_the_table(bound[0], scope)
        return (isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "get"
                and len(value.args) == 2
                and isinstance(value.args[0], ast.Constant)
                and value.args[0].value == "timeout"
                and default_ok(value.args[1]))

    calls = sessions = 0
    offenders = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", ""))
            if name == "_http_get_json":
                calls += 1
                values = [kw.value for kw in node.keywords if kw.arg == "timeout"]
            elif name == "ClientTimeout":
                sessions += 1
                values = [kw.value for kw in node.keywords if kw.arg == "total"]
                values += node.args[:1]
            else:
                continue
            offenders += [f"{node.lineno}: {ast.unparse(v)}" for v in values
                          if not from_the_table(v, scope)]
    return offenders, calls, sessions


def test_the_verifier_timeout_scan_sees_what_it_should():
    """The check above needs its own control: when the verifier is clean, "no offenders" passes idly."""
    def scan(src):
        return _verifier_timeout_offenders(ast.parse(src))[0]

    assert scan("async def f(entry):\n"
                "    ClientTimeout(total=20)\n") == ["2: 20"], "a hardcoded number was not caught"
    assert scan("async def f(entry):\n"
                "    ClientTimeout(20)\n") == ["2: 20"], "the positional-argument form was not caught"
    assert scan("async def f(entry):\n"
                "    _http_get_json(u, timeout=30.0)\n") == ["2: 30.0"]
    assert scan("async def f(entry):\n"
                "    t = entry.get('timeout', 99)\n"
                "    ClientTimeout(total=t)\n") == ["3: t"], "a default not on the allowlist was not caught"
    assert scan("async def _check_raw(entry):\n"
                "    ClientTimeout(total=_ERROR_BODY_TIMEOUT_SEC)\n") == [
        "2: _ERROR_BODY_TIMEOUT_SEC"], "the diagnostic timeout in the wrong function was not caught"
    assert scan("async def f(entry):\n"
                "    t = entry.get('timeout', _EMBED_ONLY_TIMEOUT_SEC)\n"
                "    ClientTimeout(total=t)\n"
                "    _http_get_json(u, timeout=entry.get('timeout', ex._HTTP_TIMEOUT_SEC))\n"
                "async def _error_body(u):\n"
                "    ClientTimeout(total=_ERROR_BODY_TIMEOUT_SEC)\n") == []


def test_the_verifier_takes_its_timeouts_only_from_the_reconciled_table():
    """The verifier has **no hardcoded timeout**: the only source is the
    `_ENDPOINTS` cell, and that cell is reconciled against the bot by
    `test_a_per_call_timeout_in_the_bot_is_mirrored_here`.

    Before 2026-09-19 this was called
    `test_the_verifier_never_gives_an_endpoint_its_own_timeout`, with the rule
    "the verifier may never specify a timeout". What it prevented was right -- on
    2026-09-08 `/web dict`'s endpoint took ~20s, and the handiest "fix" is to raise
    the timeout in the verifier to make it green, which hides a genuinely broken
    user-facing command. **But that same day the bot side loosened the caller per
    the rule** (`DICT_TIMEOUT_SEC`), and from then "always use the default" meant
    "**narrower** than the bot": the bot fine, this reports unreachable -- the rule
    says "not looser than the bot", but the implementation measured "may not
    specify", and the two diverged once the bot loosened itself. The equivalent
    rule now is "may only copy the bot's value", copying guarded by the
    reconciliation test; this test guards "no second source" -- hardcoding a number
    at a call site bypasses that reconciled table.

    AST rather than string comparison: the reasoning for this rule is written in
    `_check_json`'s docstring, and a string scan would hit that explanation and
    pass itself.

    Added 2026-09-19: the `ClientTimeout(total=...)` half. The non-JSON and POST
    checks originally hardcoded `total=20`, while the bot's anime database POST is
    15s -- the verifier looser than the bot, reporting ok when the bot would time
    out. The original scan only looked at `_http_get_json`'s `timeout=`, so it did
    not see them.
    """
    source = (Path(__file__).resolve().parent.parent / "axiomatic"
              / "verify_external_apis.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders, calls, sessions = _verifier_timeout_offenders(tree)
    assert calls, "not a single `_http_get_json` call can be scanned in the verifier -- the scan is broken, this spins idle"
    assert sessions >= 3, (
        f"only {sessions} `ClientTimeout` scanned in the verifier (`_check_raw`, "
        "`_check_post`, `_error_body`, one each) -- that half of the scan is "
        "broken, this spins idle")
    assert not offenders, (
        f"these calls' timeouts in verify_external_apis.py do not come from the "
        f"`_ENDPOINTS` cell: {offenders}. A hardcoded timeout bypasses the "
        "reconciliation with the bot -- to change a timeout, change the bot-side "
        "constant, then copy the matching `_ENDPOINTS` entry's `timeout` / "
        "`bot_timeout`.")


def test_the_default_timeout_is_a_named_constant():
    """The default timeout must have a name, because **another module prints it**.

    `verify_external_apis` prints this number of seconds when it reports "no
    response" -- without it, the reader cannot tell "cannot connect" from
    "endpoint is alive but slower than this cap", and the two need opposite
    handling.
    """
    # `timeout` is keyword-only (after `*` in the signature), so the default lives
    # in `__kwdefaults__` not `__defaults__` -- using `signature` avoids the
    # distinction.
    default = inspect.signature(ex._http_get_json).parameters["timeout"].default
    assert default == ex._HTTP_TIMEOUT_SEC, (
        f"the signature default {default} disagrees with `_HTTP_TIMEOUT_SEC` "
        f"({ex._HTTP_TIMEOUT_SEC}) -- hardcoded back to a literal?")


# ---------------------------------------------------------------------------
# The verifier's three-way exit code (2026-09-20)
# ---------------------------------------------------------------------------
# "Everything unreachable" was originally exit 0: verified nothing, but a caller
# that only reads the exit code read it as all-fine. Now the same scheme as
# `verify_browser.py`: 0 all verified and healthy, 1 has a FAIL, 3 has unverified.

def _r(verdict: str) -> dict:
    return {"group": "g", "what": "w", "verdict": verdict, "detail": ""}


@pytest.mark.parametrize("verdicts, expected", [
    (["OK", "OK"], 0),
    ([], 0),
    # **`SKIP` moved to the "unverified" side later the same day.** This cell was
    # originally 0, with the comment "a deliberate skip is not unverified" -- but
    # this script has no "deliberately skipped" endpoint: both sources of `SKIP`
    # are the CDN entry failing to get a sample image. Measured `--only cdn` (`cdn`
    # is its own group) with no sample printed "1 ok, 0 failed, 0 unreachable",
    # exit 0, exactly the shape the three-way split exists to kill.
    (["OK", "SKIP"], 3),
    (["SKIP"], 3),
    (["UNREACHABLE", "UNREACHABLE"], 3),       # offline: verified nothing
    (["OK", "UNREACHABLE"], 3),                # partially unverified must not report success
    (["FAIL", "UNREACHABLE"], 1),              # a real refusal takes priority over unverified
    (["OK", "FAIL"], 1),
])
def test_the_verifier_exit_code_is_three_way(verdicts, expected):
    assert vx._exit_code([_r(v) for v in verdicts]) == expected


def test_the_unverified_exit_code_is_not_argparses_usage_error():
    """argparse uses 2 on a bad argument; unverified must be told apart from it, and from FAIL (1)."""
    assert vx.EXIT_UNVERIFIED not in (0, 1, 2)
    assert (vx.EXIT_OK, vx.EXIT_FAIL) == (0, 1)


@pytest.mark.parametrize("as_json", [False, True])
def test_main_reports_unverified_end_to_end_without_touching_the_network(
        monkeypatch, capsys, as_json):
    """All the way in from `main()`: the exit code comes from `_exit_code`, and JSON carries the same number.

    `_run` is replaced with synthetic results, so no network is hit; `argv=[]` also
    pins `main()` not to read pytest's own command line (which would become
    argparse's exit 2).
    """
    async def fake_run(only):
        return [_r("OK"), _r("UNREACHABLE")]

    monkeypatch.setattr(vx, "_run", fake_run)
    monkeypatch.setattr(ex, "_configured_contact", lambda: "x")
    code = vx.main(["--json"] if as_json else [])
    out = capsys.readouterr().out
    assert code == vx.EXIT_UNVERIFIED, out
    if as_json:
        payload = json.loads(out)
        assert payload["exit"] == vx.EXIT_UNVERIFIED
        assert payload["unreachable"] == 1 and payload["failed"] == 0
    else:
        assert f"exit {vx.EXIT_UNVERIFIED}" in out, out


# ---------------------------------------------------------------------------
# The verifier's own layer: the three check functions had never been run by any test (2026-09-20)
# ---------------------------------------------------------------------------
# Measured by coverage: `verify_external_apis.py` at 64%, and the missing 66 lines
# are not scattered -- `_check_raw`, `_check_post`, `_read_body_text`,
# `_error_body` **in full**, plus `_run`'s printing and `main()`'s two branches.
# That is, "whether the verifier itself is telling the truth" had no guard at all.
#
# The stand-ins below only grow the attributes these functions actually touch.
# `aiohttp` is `import`ed inside the functions, obtaining the same module object,
# so replacing `ex.aiohttp.ClientSession` takes effect on both sides at once.


class _BoomBody:
    """A stream whose `read()` throws. A diagnostic read should not take the verification down with it."""

    async def read(self, _n: int = -1) -> bytes:
        raise OSError("stream exploded")


class _VerifierResponse:
    """A stand-in for an `aiohttp` response. `content_type` is what `_check_raw` prints on success."""

    def __init__(self, status, *, body=b"", content_type="application/json",
                 payload=None, boom_body=False):
        self.status = status
        self.content_type = content_type
        self._payload = payload
        self.content = _BoomBody() if boom_body else _FakeBody(body)

    async def json(self):
        return self._payload


class _Ctx:
    """The async context manager returned by `session.get(...)`.

    Putting an **exception instance** in the queue means "this request blows up" --
    real `aiohttp` also throws only at `__aenter__`, so the stand-in follows the
    same contract, not how the caller happens to be written right now.
    """

    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result

    async def __aexit__(self, *_exc):
        return False


class _VerifierSession:
    """A recording `ClientSession` stand-in; responses always come from the `responses` queue."""

    calls: list[dict] = []
    responses: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def _open(self, method, url, params, headers, json_body):
        type(self).calls.append({
            "method": method, "url": url, "params": params,
            "headers": dict(headers or {}), "json": json_body})
        assert type(self).responses, f"the stand-in has no prepared response for {method} {url}"
        return _Ctx(type(self).responses.pop(0))

    def get(self, url, *, params=None, headers=None, **kw):
        return self._open("GET", url, params, headers, kw.get("json"))

    def post(self, url, *, params=None, headers=None, **kw):
        return self._open("POST", url, params, headers, kw.get("json"))


@pytest.fixture
def verifier_http(monkeypatch):
    _VerifierSession.calls = []
    _VerifierSession.responses = []
    monkeypatch.setattr(ex.aiohttp, "ClientSession", _VerifierSession)
    return _VerifierSession


_RAW_ENTRY = {"group": "g", "what": "w", "raw": True,
              "url": "https://example.test/x", "timeout": 20.0}
_POST_ENTRY = {"group": "g", "what": "w", "method": "POST",
               "url": "https://example.test/gql", "json_body": {"query": "q"},
               "timeout": 15.0}
_JSON_ENTRY = {"group": "g", "what": "w",
               "url": "https://example.test/j", "params": None,
               "timeout": 30.0}


def _json_answer(monkeypatch, status, data=None):
    """Replace the shared GET under `_check_json`, returning the given (status, data)."""
    async def fake_get(url, *, params=None, headers=None,
                       timeout=ex._HTTP_TIMEOUT_SEC, quiet_statuses=()):
        return status, data
    monkeypatch.setattr(ex, "_http_get_json", fake_get)


# --- One rule, three implementations: a declared non-200 --------------------

@pytest.mark.parametrize("kind", ["json", "raw", "post"])
def test_every_checker_honours_an_expected_non_200(monkeypatch, verifier_http,
                                                   kind):
    """`ok_statuses` was originally only visible to `_check_json`.

    A key that is **declared but nobody reads** has no symptom -- the same shape as
    that stale `_OWNER_ONLY_SLASH` string, only its failure direction is crying
    wolf: that entry would forever report FAIL, and on the report it looks like the
    site really refused us. All three now share the same wording, and this test is
    that "comparison".
    """
    entry = {"json": _JSON_ENTRY, "raw": _RAW_ENTRY,
             "post": _POST_ENTRY}[kind] | {"ok_statuses": (404,)}
    if kind == "json":
        _json_answer(monkeypatch, 404)
        verdict, detail = _run(vx._check_json(entry))
    else:
        verifier_http.responses.append(_VerifierResponse(404))
        checker = vx._check_raw if kind == "raw" else vx._check_post
        verdict, detail = _run(checker(entry))
    assert verdict == "OK", f"{kind}: a declared 404 was reported as {verdict} ({detail})"
    assert detail == vx._expected_status(404)[1], f"{kind}: {detail!r}"


# --- One rule, three implementations: name the timeout cap when nothing answers ---

@pytest.mark.parametrize("kind, timeout", [
    ("json", 30.0), ("raw", 20.0), ("post", 15.0)])
def test_every_checker_names_the_timeout_when_nothing_answers(
        monkeypatch, verifier_http, kind, timeout):
    """"Cannot connect" and "slower than our cap" are two things, and only `_check_json` had learned it.

    On 2026-09-08 the dictionary entry was the latter (the endpoint returns 200,
    just takes 20s, while the cap was 15s), and the message at the time pointed
    people at a network fault. Before 2026-09-20 `_check_raw` / `_check_post`
    returned a bare `TimeoutError` -- the same misdirection, and their two entries'
    caps (20 / 30s) are exactly the ones most likely to be "alive but slower than
    the cap".
    """
    entry = {"json": _JSON_ENTRY, "raw": _RAW_ENTRY, "post": _POST_ENTRY}[kind]
    if kind == "json":
        _json_answer(monkeypatch, -1)
        verdict, detail = _run(vx._check_json(entry))
    else:
        verifier_http.responses.append(TimeoutError())
        checker = vx._check_raw if kind == "raw" else vx._check_post
        verdict, detail = _run(checker(entry))
    assert verdict == "UNREACHABLE"
    assert f"{timeout:g}" in detail, f"{kind}: did not print the timeout cap: {detail!r}"
    assert "slower than this cap" in detail, f"{kind}: only reported unreachable: {detail!r}"


@pytest.mark.parametrize("kind", ["raw", "post"])
def test_a_connection_that_never_opened_is_not_blamed_on_slowness(
        verifier_http, kind):
    """Near-miss: DNS / blocked is **the connection never opening**, which must be
    told apart from "replies slowly", or both get sent to change the timeout. The
    exception's class name must be kept, it is the only clue that distinguishes
    them."""
    verifier_http.responses.append(OSError("no route to host"))
    entry = _RAW_ENTRY if kind == "raw" else _POST_ENTRY
    checker = vx._check_raw if kind == "raw" else vx._check_post
    verdict, detail = _run(checker(entry))
    assert verdict == "UNREACHABLE"
    assert "OSError" in detail, detail
    assert "the connection itself never opened" in detail, detail
    assert "slower than this cap" not in detail, f"reported unreachable as slow: {detail!r}"


# --- The CDN entry: no sample image means unverified -------------------------

@pytest.mark.parametrize("post", [None, {}, {"id": 1, "md5": "x"}])
def test_the_cdn_check_skips_when_there_is_no_sample_image(monkeypatch, post):
    """The CDN has no fixed URL, it must first ask the API for an existing image. Failing to get one means **unverified**.

    These two `SKIP`s are the only two in the whole script, so "deliberately
    skipped" never exists -- see the cell that changed sides in
    `test_the_verifier_exit_code_is_three_way`.
    """
    async def no_post(*_args, **_kwargs):
        return post
    monkeypatch.setattr(ex, "_fetch_danbooru_post", no_post)
    entry = next(e for e in vx._ENDPOINTS if e.get("cdn"))
    verdict, _detail = _run(vx._check_raw(entry))
    assert verdict == "SKIP"
    assert vx._exit_code([{"verdict": verdict}]) == vx.EXIT_UNVERIFIED


@pytest.mark.parametrize("post, expected", [
    ({"large_file_url": "https://cdn.test/large.png",
      "file_url": "https://cdn.test/file.png"}, "https://cdn.test/large.png"),
    ({"file_url": "https://cdn.test/file.png"}, "https://cdn.test/file.png"),
    ({"preview_file_url": "https://cdn.test/p.png"}, "https://cdn.test/p.png"),
])
def test_the_cdn_check_downloads_the_sample_it_was_given(
        monkeypatch, verifier_http, post, expected):
    """Actually fetch that image, in the order large -> file -> preview.

    The order is not arbitrary: `--grid` download goes through this, and the
    verification must hit **the same** URL.
    """
    async def one_post(*_args, **_kwargs):
        return post
    monkeypatch.setattr(ex, "_fetch_danbooru_post", one_post)
    verifier_http.responses.append(
        _VerifierResponse(200, content_type="image/png"))
    entry = next(e for e in vx._ENDPOINTS if e.get("cdn"))
    verdict, detail = _run(vx._check_raw(entry))
    assert verdict == "OK", detail
    assert [c["url"] for c in verifier_http.calls] == [expected]
    assert "image/png" in detail


# --- raw's headers and failure path -------------------------------------

def test_a_raw_request_always_carries_a_user_agent(verifier_http):
    """A raw endpoint with no declared UA must be given the shared one -- this
    script's whole value is "walk the same path as the bot", and both 2026-08-30
    incidents were in the headers."""
    verifier_http.responses.append(_VerifierResponse(200))
    _run(vx._check_raw(_RAW_ENTRY))
    assert verifier_http.calls[0]["headers"]["User-Agent"] == ex._user_agent()


def test_a_raw_entry_keeps_the_user_agent_it_declared(verifier_http):
    """Near-miss: the iqdb entry wants a browser-style UA, and filling the default must not override it."""
    verifier_http.responses.append(_VerifierResponse(200))
    entry = _RAW_ENTRY | {"headers": {"User-Agent": ex._BROWSER_UA}}
    _run(vx._check_raw(entry))
    assert verifier_http.calls[0]["headers"]["User-Agent"] == ex._BROWSER_UA


def test_a_raw_failure_quotes_what_the_upstream_said(verifier_http):
    """Nine times out of ten a 403's reason is in the body. This path also goes through `_read_body_text`."""
    body = json.dumps({"message": "blocked: missing contact"}).encode("utf-8")
    verifier_http.responses.append(_VerifierResponse(403, body=body))
    verdict, detail = _run(vx._check_raw(_RAW_ENTRY))
    assert verdict == "FAIL"
    assert "HTTP 403" in detail
    assert "blocked: missing contact" in detail, detail


def test_reading_a_failure_body_never_takes_the_verification_down(
        verifier_http):
    """When the body cannot be read, return an empty string. A diagnostic is added value, it must not blow up the whole scan."""
    assert _run(vx._read_body_text(_VerifierResponse(500, boom_body=True))) == ""
    resp = _VerifierResponse(500, body="upstream detail".encode("utf-8"))
    assert _run(vx._read_body_text(resp)) == "upstream detail"


def test_the_second_request_for_a_reason_is_allowed_to_fail(verifier_http):
    """`_error_body` is "hit it once more after already failing". When it fails itself, it just misses one reason line."""
    verifier_http.responses.append(OSError("still down"))
    assert _run(vx._error_body("https://example.test/x")) == ""
    verifier_http.responses.append(
        _VerifierResponse(429, body=b'{"error":"slow down"}'))
    assert "slow down" in _run(vx._error_body("https://example.test/x"))


# --- The POST entry ------------------------------------------------------

def test_the_post_check_sends_the_declared_query_with_a_user_agent(
        verifier_http):
    verifier_http.responses.append(
        _VerifierResponse(200, payload={"data": {"Media": {"id": 1}}}))
    verdict, detail = _run(vx._check_post(_POST_ENTRY))
    assert verdict == "OK", detail
    call = verifier_http.calls[0]
    assert call["method"] == "POST"
    assert call["json"] == _POST_ENTRY["json_body"]
    assert call["headers"]["User-Agent"] == ex._user_agent()


@pytest.mark.parametrize("payload", [{"errors": [{}]}, [], None, "data"])
def test_the_post_check_rejects_a_200_with_the_wrong_shape(verifier_http,
                                                           payload):
    """A 200 from the GraphQL endpoint does not mean it queried -- no `data` means broken."""
    verifier_http.responses.append(_VerifierResponse(200, payload=payload))
    verdict, _detail = _run(vx._check_post(_POST_ENTRY))
    assert verdict == "FAIL"


def test_a_post_failure_does_not_blame_the_user_agent(verifier_http):
    """Walk the whole path (not just test `_diagnose`) to confirm `method="POST"` is really passed down."""
    verifier_http.responses.append(_VerifierResponse(403, body=b"<html>x"))
    verdict, detail = _run(vx._check_post(_POST_ENTRY))
    assert verdict == "FAIL"
    assert ex._UA_CONTACT_KEY not in detail, detail


# --- Diagnosis order: what the upstream says wins, 5xx included --------------

def test_the_diagnosis_prefers_the_upstream_even_for_a_5xx():
    """The cell added 2026-09-20.

    Originally 5xx returned "52x usually means the fronting CDN cannot reach the
    origin" directly, so a 503 carrying a JSON explanation ("maintenance until X")
    would be rewritten by this tool into a guess -- exactly violating the rule set
    in this function's docstring. The same shape already hurt once on a 403
    (2026-09-07 anime database).
    """
    body = json.dumps({"message": "scheduled maintenance until 2026-10-01"})
    out = vx._diagnose(503, body=body)
    assert "scheduled maintenance" in out, f"the guess overwrote the fact: {out!r}"
    assert "CDN" not in out, out


def test_a_5xx_without_a_readable_reason_still_gets_the_cdn_hint():
    """A reverse guard: narrowing the guess must not become "say nothing". A CDN challenge page yields no JSON."""
    out = vx._diagnose(522, body="<html>error code: 522</html>")
    assert "CDN" in out and "upstream" in out, out


# --- `_run`'s selection and printing -------------------------------------

def _record_checks(monkeypatch, verdict="OK"):
    seen = []

    async def fake_check(entry):
        seen.append(entry["group"])
        return verdict, "detail"

    for name in ("_check_json", "_check_raw", "_check_post"):
        monkeypatch.setattr(vx, name, fake_check)
    return seen


def test_only_checks_the_groups_that_were_asked_for(monkeypatch):
    monkeypatch.setattr(vx, "_QUIET", True)
    seen = _record_checks(monkeypatch)
    _run(vx._run(["xkcd"]))
    assert seen == ["xkcd"]
    seen.clear()
    results = _run(vx._run(None))
    assert len(seen) == len(vx._ENDPOINTS) == len(results)


def test_every_endpoint_gets_a_line_and_every_bad_one_gets_a_reason(
        monkeypatch, capsys):
    """The non-`--json` path: one line per entry, plus one reason line for a non-OK.

    `monkeypatch.setattr(vx, "_QUIET", False)` is not only setting a value --
    `main()` writes it True with `global` and leaves it there, so this also fences
    off that contamination (teardown restores it).
    """
    monkeypatch.setattr(vx, "_QUIET", False)
    _record_checks(monkeypatch, verdict="FAIL")
    _run(vx._run(["xkcd"]))
    out = capsys.readouterr().out
    assert out.count("\n") == 2, out
    assert "xkcd" in out and "detail" in out


def test_every_verdict_the_checkers_can_return_is_registered():
    """The verdict strings are scattered across the three check functions' `return`s, and both registrations must match.

    Forgetting to register has two halves: `_run` KeyErrors mid-scan (noisy, but at
    least visible), while the `_exit_code` half is **silent** -- a new verdict not
    in `_UNVERIFIED_VERDICTS` is automatically counted as "verified and healthy",
    which is the shape being fixed all this day.
    """
    tree = ast.parse(Path(vx.__file__).read_text(encoding="utf-8"))
    returned = set()
    tuples = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value,
                                                              ast.Tuple):
            continue
        head = node.value.elts[0] if node.value.elts else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            tuples += 1
            returned.add(head.value)
    assert tuples >= 8, f"the verdict scan caught nothing ({tuples}) -- an empty set looks like clean"
    assert returned == set(vx._VERDICT_MARKS), (
        f"verdicts and display registration disagree: {returned ^ set(vx._VERDICT_MARKS)}")
    classified = vx._UNVERIFIED_VERDICTS | {"OK", "FAIL"}
    assert returned == classified, (
        f"a verdict is not classified by the exit code: {returned ^ classified}")


# --- main()'s statistics -------------------------------------------------

def test_main_counts_every_result_exactly_once(monkeypatch, capsys):
    """The four numbers must **add up**.

    Originally "ok" was subtracted (total - failed - unreachable), so `SKIP` was
    counted into ok, and any future new verdict is automatically counted as good.
    Subtraction silently folds anything unlisted into the normal ones.
    """
    async def fake_run(_only):
        return [_r(v) for v in ("OK", "OK", "FAIL", "UNREACHABLE", "SKIP")]

    monkeypatch.setattr(vx, "_run", fake_run)
    monkeypatch.setattr(vx, "_QUIET", False)
    code = vx.main(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == vx.EXIT_FAIL
    counted = sum(payload[k] for k in ("ok", "failed", "unreachable",
                                       "skipped"))
    assert counted == len(payload["results"]) == 5, payload
    assert (payload["ok"], payload["skipped"]) == (2, 1), payload


def test_main_says_which_config_key_to_set_when_there_is_no_contact(
        monkeypatch, capsys):
    """With no contact set, the opening line must name it -- a 403 is not diagnosable from the status code alone."""
    async def fake_run(_only):
        return [_r("OK")]

    monkeypatch.setattr(vx, "_run", fake_run)
    monkeypatch.setattr(vx, "_QUIET", False)
    monkeypatch.setattr(ex, "_configured_contact", lambda: "")
    assert vx.main([]) == vx.EXIT_OK
    out = capsys.readouterr().out
    assert ex._UA_CONTACT_KEY in out, out
    assert "1 ok, 0 failed, 0 unreachable, 0 skipped" in out, out


def test_a_200_that_is_not_json_is_a_failure(monkeypatch):
    """HTTP 200 but the content will not parse = that endpoint is broken for us, not OK.

    The value of this is in `_http_get_json`'s contract: on a parse failure it
    returns `(200, None)`, and "the status code is pretty" is exactly the kind of
    breakage most easily mistaken for healthy.
    """
    _json_answer(monkeypatch, 200, None)
    verdict, detail = _run(vx._check_json(_JSON_ENTRY))
    assert verdict == "FAIL"
    assert "not JSON" in detail


def test_a_failure_page_bigger_than_the_cap_leaves_no_reason_but_no_crash():
    """The failure page has a cap too -- the body is third-party, and there must be no unbounded read on the error path.

    Over the cap, `read_capped_body` returns `None`, and the diagnosis loses one
    reason line; it **must not** become an exception, and `None` must not be passed
    down as a string.
    """
    oversize = b"x" * (vx._DIAG_BODY_CAP + 1)
    resp = _VerifierResponse(503, body=oversize)
    assert _run(vx._read_body_text(resp)) == ""


def test_everything_unreachable_says_it_is_probably_the_network(monkeypatch,
                                                                capsys):
    """A whole sea of unreachable usually means this end has no network; do not send people to check whether the site blocked us."""
    async def fake_run(_only):
        return [_r("UNREACHABLE"), _r("UNREACHABLE")]

    monkeypatch.setattr(vx, "_run", fake_run)
    monkeypatch.setattr(vx, "_QUIET", False)
    monkeypatch.setattr(ex, "_configured_contact", lambda: "x")
    assert vx.main([]) == vx.EXIT_UNVERIFIED
    out = capsys.readouterr().out
    assert "no network" in out, out
    assert "0 ok, 0 failed, 2 unreachable, 0 skipped" in out, out


def test_the_summary_repeats_every_failure_at_the_bottom(monkeypatch,
                                                        capsys):
    """The scan has 22 entries and the per-entry lines scroll away; the end must list each FAIL with its reason again.

    If only `--json` is tested, this text output never runs -- and this is what
    people read.
    """
    async def fake_run(_only):
        bad = _r("FAIL") | {"group": "wiki", "what": "encyclopedia summary",
                            "detail": "HTTP 403  <- upstream said: no contact"}
        return [_r("OK"), bad]

    monkeypatch.setattr(vx, "_run", fake_run)
    monkeypatch.setattr(vx, "_QUIET", False)
    monkeypatch.setattr(ex, "_configured_contact", lambda: "x")
    assert vx.main([]) == vx.EXIT_FAIL
    out = capsys.readouterr().out
    assert "FAIL  wiki" in out, out
    assert "no contact" in out, f"the end reported only the title, no reason: {out!r}"
    assert "1 ok, 1 failed, 0 unreachable, 0 skipped" in out, out


# ---------------------------------------------------------------------------
# The two single-pick image boards: the random-order meta tag and "pick one from the response"
# ---------------------------------------------------------------------------

_POST_READERS = {
    "danbooru_post": (lambda: ex._fetch_danbooru_post("tag"), False, "one"),
    "danbooru_bulk": (lambda: ex._fetch_danbooru_posts_bulk("tag"), False, "list"),
    "danbooru_random_n": (lambda: ex._fetch_danbooru_posts_random("tag", 2), False, "list"),
    "danbooru_latest": (lambda: ex._fetch_danbooru_posts_latest("tag"), False, "list"),
    "danbooru_latest_one": (lambda: ex._fetch_danbooru_post_latest("tag"), False, "one"),
    "safebooru_post": (lambda: ex._fetch_safebooru_post("tag"), False, "one"),
    "e621_post": (lambda: ex._fetch_e621_post("tag"), True, "one"),
    "danbooru_tags": (lambda: ex._query_tags_json(ex._DANBOORU_TAGS, name="tag"), False, "list"),
}


@pytest.mark.parametrize("name", sorted(_POST_READERS))
def test_every_post_reader_drops_entries_that_are_not_objects(name, fake_http, monkeypatch):
    """When the site errors or changes structure it returns a list with non-objects
    inside. Callers always use `.get(...)`, so they must be filtered on the way in
    -- miss one and that command is left with a single generic failure.

    The single-pick ones use `random.choice`: fix it to "pick the first" and put the
    junk first, so an unfiltered version **always** picks junk -- unfixed, it has a
    one-in-five chance of happening to pick the real one and passing anyway."""
    monkeypatch.setattr(ex.random, "choice", lambda seq: seq[0])
    fetch, wrapped, shape = _POST_READERS[name]
    real = {"id": 987_654_321, "tag_string_general": "a b"}
    body = [None, "junk", 5, ["nested"], real]
    fake_http.replies = [(200, {"posts": body} if wrapped else body)] * 3
    got = _run(fetch())
    assert got == (real if shape == "one" else [real]), got


def test_the_post_reader_table_covers_every_list_reading_fetcher():
    """If `_POST_READERS` omits a list-reading fetcher, the previous test cannot see
    it. Reconcile against `_FETCHERS`: only e621's tag query is absent here -- it
    shares `_query_tags_json` with danbooru_tags."""
    assert set(_FETCHERS) - set(_POST_READERS) == {"e621_tags"}


@pytest.mark.parametrize("fetch, meta, wrap", [
    (ex._fetch_safebooru_post, "sort:", lambda posts: posts),
    (ex._fetch_e621_post, "order:", lambda posts: {"posts": posts}),
])
def test_a_random_post_fetch_asks_for_random_order_and_returns_one_post(
        fetch, meta, wrap, fake_http):
    """Without this meta tag, both sites sort by "newest", so the same tag set draws
    the same batch every time. If the user wrote their own sort, honour it and do not
    stack another -- with two sort orders present, the site only recognises one."""
    fake_http.replies = [(200, wrap([{"id": 7}]))]
    assert _run(fetch("cat_ears")) == {"id": 7}
    assert fake_http.calls[-1]["params"]["tags"] == f"cat_ears {meta}random"

    fake_http.replies = [(200, wrap([{"id": 8}]))]
    _run(fetch(f"cat_ears {meta}score"))
    assert fake_http.calls[-1]["params"]["tags"] == f"cat_ears {meta}score"

    fake_http.replies = [(200, wrap([]))]
    assert _run(fetch("")) is None
    assert fake_http.calls[-1]["params"]["tags"] == f"{meta}random"
