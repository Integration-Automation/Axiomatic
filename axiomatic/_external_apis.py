"""External-API "fetch / parse" helpers -- a pure data-retrieval module (P2 refactor, extracted from discord_bot.py).

Holds only "get data / parse response" logic: the shared HTTP-get-JSON wrapper
(_http_get_json), the Danbooru / Safebooru / e621 post fetchers, and the fuzzy
tag resolver over /tags.json. The `mcmd_*` dispatch and the Discord reply
assembly (including _send_danbooru_* / grid image composition) still live in
discord_bot.py, which calls this module's helpers to obtain data and then
builds its own replies.

Important (avoiding a circular import): this module MUST NOT import discord_bot.
These helpers do not touch Discord, and do not call the bot-side reply / redact /
log helpers; the one original bot coupling was the API-call counter
_METRICS_API_CALLS, now moved here as well and exposed via `api_call_count()`
for the bot's !metrics.

Non-leak rule: internally the module may use the real names of external
services (the code needs them), but these helpers never produce strings bound
for Discord -- errors only print to stderr, and outward replies are assembled
bot-side and kept generic.
"""
from __future__ import annotations

import json as _json
import random
import sys
from collections import deque
from typing import NamedTuple

import aiohttp

try:
    from _bot_config import load_bot_config
except ImportError:  # package path (`from axiomatic import _external_apis`)
    from axiomatic._bot_config import load_bot_config  # type: ignore


def api_call_count() -> int:
    """Number of external API calls made in this session (read by the bot's `/health` / `!metrics`).

    Before 2026-08-30 this number was leaky: the three Danbooru fetchers opened
    their own `ClientSession`, bypassing `_http_get_json`, so they were not
    counted at all. This module now has **only** one outbound exit,
    `_http_get_json`, so this number is the actual number of requests sent.
    """
    return _METRICS_API_CALLS


_METRICS_API_CALLS: int = 0

# When the bot is @-mentioned we reply with a random rating:general Danbooru
# image of "rossi (arknights)". The last DANBOORU_HISTORY_SIZE post IDs are
# remembered so the same image isn't sent twice in a row.
DANBOORU_API = "https://danbooru.donmai.us/posts.json"

DANBOORU_FETCH_LIMIT = 30

DANBOORU_HISTORY_SIZE = 10

_danbooru_recent: deque[int] = deque(maxlen=DANBOORU_HISTORY_SIZE)

# --------------------------------------------------------------------------
# User-Agent: every outbound request must carry one, and it must be the kind
# that "explains who you are"
# --------------------------------------------------------------------------
# Measured 2026-08-30: the whole Danbooru site is now behind Cloudflare, and to
# **aiohttp's default** (= no User-Agent header at all) it returns 403 plus a
# "Just a moment..." challenge page. All this project's Danbooru calls carried
# no UA at the time, so six entry points -- the random image for `@bot rossi`,
# `--grid`, `--latest`, tag_suggest's bulk fetch, and the entire fuzzy tag
# resolver -- **all failed**, and silently: the user only saw "not found" and
# assumed they had mistyped a tag. e621 and Safebooru were completely fine at
# the same moment; the only difference was this header.
#
# The fix follows the site's stated rules (Help:Api and forum #37341): send a
# UA that **explains who you are**, not a library default and not a pretend
# browser. All three were measured:
#     no UA (aiohttp default)        -> 403
#     a full Chrome 140 browser UA   -> 403
#     an explanatory UA (this one)   -> 200
# Note the middle line: pretending to be a browser not only breaks the site
# rules, in practice it is **more likely to be blocked than an honest
# declaration** -- the challenge page expects a real browser to solve the JS,
# we cannot, so we are judged to be impersonating.
#
# The site also recommends keeping sustained continuous requests at around
# 1/second (short bursts like interactive commands go through their burst pool
# and are exempt). This 403 had nothing to do with rate: the same request
# cadence with this UA is 200, and with a browser UA is 403 again.
_BOT_UA = "axiomatic-bot/1.0 (Discord bot; private use)"

# -- but "able to say who you are" and "reachable" are two different things,
# and some sites only accept the latter.
# On 2026-08-30 Wikimedia's REST API was likewise measured returning 403, and
# it gave two different messages that draw this line very clearly:
#     no UA at all       -> "Please set a user-agent and respect our robot policy"
#     `_BOT_UA` (descriptive) -> "Please respect our robot policy ... Contact
#                            bot-traffic@wikimedia.org if you need higher volumes"
#     a UA with a URL or email -> 200
# That is, the second message is not complaining that we did not identify
# ourselves, it is saying "you left no way to contact you".
#
# The contact goes in `bot_config.json`'s `api_contact`, **defaulting to an
# empty string**. This string is sent to third-party sites, and what to put
# there is the owner's decision, not one the code should make for them. When it
# is not set those sites keep returning 403, and `_http_get_json` prints a line
# saying exactly which key to set.
_UA_CONTACT_KEY = "api_contact"

# `_http_get_json`'s default timeout. **It has a name so others can read it** --
# `verify_external_apis` prints this number when it reports "no response",
# otherwise the reader cannot tell "cannot connect" from "endpoint is alive but
# slower than this cap". The two need opposite handling: the former is a network
# investigation, the latter means loosening the timeout of **that one call**.
# 2026-09-08 hit the latter (`/web dict`'s endpoint returns 200 but takes ~20s).
# Loosening this **default** is not the fix: it applies to every external call,
# which would leave every truly-down site hanging the user for several extra
# seconds. A slow endpoint should specify its timeout at its own call site.
_HTTP_TIMEOUT_SEC = 15.0


def _configured_contact() -> str:
    """`bot_config.json`'s `api_contact`; empty string if unset / unreadable. Never raises."""
    try:
        return str(load_bot_config().get(_UA_CONTACT_KEY) or "").strip()
    except Exception:  # pylint: disable=broad-except
        return ""


def contact_configured() -> bool:
    """Whether `api_contact` is set. Lets the caller decide "is this 403 that particular thing".

    A public wrapper, not a second decision: the actual read still happens only
    in `_configured_contact()`. It exists because the bot side needs this answer
    to decide whether its error message should say one more sentence, and
    reaching into another module's private name would only make the next person
    think that name is a public interface.
    """
    return bool(_configured_contact())


def _user_agent() -> str:
    """The UA to use for this request. If a contact is set, add it in the site's required format."""
    contact = _configured_contact()
    if not contact:
        return _BOT_UA
    return f"axiomatic-bot/1.0 ({contact})"


# Response size cap. **Same reasoning as `discord_bot.GRID_MAX_IMAGE_BYTES`**:
# the bytes on this path are not content we control, they come from public
# third-party APIs. `ClientTimeout(total=...)` does not stop this -- it governs
# transfer time, not size; a huge response that keeps streaming steadily will
# exhaust the process's memory well within the timeout.
#
# Added 2026-09-06. The image path had a cap long ago (`GRID_MAX_IMAGE_BYTES`,
# with the reasoning right there in the comment), but the JSON path did not --
# **same source, same threat, only half of it stopped**.
#
# 8 MB is a very generous ceiling: a normal `posts.json?limit=30` is a few
# hundred KB, and `tags.json` is smaller still.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


# How many bytes to request from the stream at a time. 64 KiB is just a
# compromise between "get enough in one await" and "don't over-read when over
# the cap"; the exact number does not matter, **what matters is that the thing
# below is a loop**.
_READ_CHUNK_BYTES = 64 * 1024


async def read_capped_body(content, cap: int) -> bytes | None:
    """Read the whole response body; give up and return `None` if it exceeds `cap` bytes.

    **Why not `await content.read(cap + 1)`.** The semantics of
    `aiohttp.StreamReader.read(n)` are *read up to n* -- it only waits until
    there is something in the buffer, then returns **whatever is currently
    buffered**; it guarantees neither reading a full n nor reading to EOF. For a
    chunked response with no `Content-Length` (which public image-board APIs
    always are) this means **a valid response gets cut in half**.

    Measured 2026-09-07 (aiohttp 3.14.3, same endpoint, `limit=30`)::

        read(cap + 1)   ->  38043 bytes   (Content-Length: None)
        read() to EOF   ->  91898 bytes
        loop to EOF     ->  91898 bytes

    `json.loads` on the truncated bytes throws `Unterminated string`, so
    `_http_get_json` returns `(status, None)` and every caller becomes "not
    found". **The symptom looks like an intermittent fault** -- where it cuts
    depends on chunk boundaries and network timing: the same probe script cut at
    different points on two runs (38043 / 49507), and the verifier was
    sometimes still green. The ones hit first are those that fetch many entries
    at once (`limit=30` bulk fetches, grid, tag suggestions).

    It is also **not allowed** to switch to `await r.read()`: that is unbounded,
    which removes the size cap entirely. The reason the cap exists is in
    `_MAX_RESPONSE_BYTES` -- these bytes come from third parties, they are not
    content we control.

    There are two possible return values, and the caller must test with
    `is None`, **not with truthiness**:

    * `bytes` -- the complete body. It may be `b""` (the body really was empty),
      which is a different thing from "over the cap"; a truthiness test would
      conflate them;
    * `None`  -- over the cap, already discarded. What to print to stderr is up
      to the caller: the two callers have different diagnostics, and message
      content is bound by Secrecy Layer 1.

    Once over the cap it **stops reading immediately** -- it does not pull the
    rest just to learn "how big" it was, which is exactly what the cap is there
    to avoid. The boundary matches the old code: exactly `cap` is accepted,
    `cap + 1` counts as over.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await content.read(_READ_CHUNK_BYTES)
        if not chunk:                 # EOF
            break
        total += len(chunk)
        if total > cap:
            # Already known to be over the cap: stop reading, and keep none of
            # what was read.
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _http_get_json(url: str, *, params=None, headers=None,
                        timeout: float = _HTTP_TIMEOUT_SEC,
                        quiet_statuses: tuple[int, ...] = ()):
    """GET -> JSON. Returns `(status, data)`; on failure `(status or -1, None)`. Implemented in `_http_json`."""
    return await _http_json("GET", url, params=params, headers=headers,
                            timeout=timeout, quiet_statuses=quiet_statuses)


async def _http_post_json(url: str, payload, *, headers=None,
                         timeout: float = _HTTP_TIMEOUT_SEC):
    """POST a JSON body -> JSON. Same return shape as `_http_get_json`.

    Shares the single exit with GET, so User-Agent, call counting, the response
    size cap, and "leave a stderr line on non-200" are all identical. Before
    2026-09-24 the only POST caller (the anime query) opened its own connection
    and slurped the whole response with `r.json()`, another exit outside this
    size cap."""
    return await _http_json("POST", url, json_body=payload, headers=headers,
                            timeout=timeout)


async def _http_json(method: str, url: str, *, params=None, json_body=None,
                     headers=None, timeout: float = _HTTP_TIMEOUT_SEC,
                     quiet_statuses: tuple[int, ...] = ()):
    """One request -> JSON. Returns `(status, data)`; on failure `(status or -1, None)`.

    **The one outbound exit of this module.** Three reasons, none optional:

    1. The `User-Agent` default is filled in here (see `_BOT_UA`). Without it
       Danbooru returns 403 outright. Before 2026-08-30 three fetchers opened
       their own `ClientSession` and bypassed this, so they bypassed the UA too
       -- "open another connection" and "omit the header" are two sides of the
       same mistake.
    2. `_METRICS_API_CALLS` is incremented here. A call that bypasses this is
       not counted, and `/health`'s "api calls" under-reports.
    3. There is only one copy of the timeout and exception handling.

    A caller's own `headers` take precedence, so overriding the UA is still
    possible, it just has to be explicit. `test_external_apis.py` blocks any
    code that opens another `ClientSession` in this module.

    **Every non-200 leaves one stderr line**, unless the caller says with
    `quiet_statuses` that the code is expected (there is currently one use:
    anonymous Danbooru's `random=true` 2-tag limit returns 422, which we are
    already prepared to retry, so it is not a fault). This was the second cause
    of the incident -- `/tags.json` and "latest N" returned empty on non-200
    without a word, so the whole fuzzy tag resolver could 403 and nobody knew.
    To be quiet you must name the code, you cannot rely on forgetting.
    """
    global _METRICS_API_CALLS
    _METRICS_API_CALLS += 1
    label = f"http_{method.lower()}_json"
    # A caller's headers override the defaults, not the other way round -- this
    # way sites with their own UA rules (e621 / Safebooru) can still specify
    # one, while "gave nothing" no longer turns into "no UA".
    merged = {"User-Agent": _user_agent()}
    if headers:
        merged.update(headers)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as s:
            request = s.get if method == "GET" else s.post
            extra = {} if json_body is None else {"json": json_body}
            async with request(url, params=params, headers=merged, **extra) as r:
                if r.status != 200:
                    if r.status not in quiet_statuses:
                        # Include params: when investigating this kind of fault,
                        # "which tag broke" is almost always the next question.
                        # Goes to stderr / log only, never to the chat platform.
                        detail = repr(params)[:200] if params else ""
                        print(f"{label} {url} {detail}: HTTP {r.status}",
                              file=sys.stderr)
                        if r.status in (403, 429) and not _configured_contact():
                            # Spell out which key to touch. This kind of 403 is
                            # not diagnosable from the status code alone -- we
                            # **do** send a UA, what the site wants is a contact
                            # inside it.
                            print(f"{label}: HTTP {r.status} and "
                                  f"`{_UA_CONTACT_KEY}` is not set. Some sites "
                                  "(e.g. Wikimedia) require the User-Agent to "
                                  "carry a reachable contact (URL or email); "
                                  "just naming yourself is not enough. Set "
                                  f"`{_UA_CONTACT_KEY}` in bot_config.json.",
                                  file=sys.stderr)
                    return r.status, None
                declared = r.content_length
                if declared is not None and declared > _MAX_RESPONSE_BYTES:
                    print(f"{label} {url}: response declares {declared} bytes, "
                          f"over the {_MAX_RESPONSE_BYTES} cap, not reading",
                          file=sys.stderr)
                    return r.status, None
                # Read to EOF. `read(n)` is read-up-to; a single call truncates
                # a chunked response into valid-but-incomplete bytes -- see
                # `read_capped_body`.
                raw = await read_capped_body(r.content, _MAX_RESPONSE_BYTES)
                if raw is None:
                    print(f"{label} {url}: response exceeds the "
                          f"{_MAX_RESPONSE_BYTES}-byte cap, discarding",
                          file=sys.stderr)
                    return r.status, None
                # Parse the bytes directly, folding the old two paths (`r.json()`
                # plus a `ContentTypeError` fallback to `r.text()`) into one:
                # JSON is UTF-8 per RFC 8259, and sites often label JSON as
                # text/plain. The encoding is explicit (CLAUDE.md hard rule),
                # and `errors="replace"` because these bytes are not ours.
                return r.status, _json.loads(
                    raw.decode("utf-8", errors="replace"))
    except Exception as error:  # pylint: disable=broad-except
        print(f"{label} {url}: {error!r}", file=sys.stderr)
        return -1, None

def _dict_entries(data) -> list[dict]:
    """The entries in a response that really are objects; empty if the response itself is not a list.

    On error, rate-limiting, or a structure change, these sites return a list
    containing non-objects (or a body with an error object). Callers always use
    `.get(...)`, so letting a non-object flow through would only turn into an
    exception in the handler, leaving the whole command with one generic
    failure. Everywhere that reads an image-board list goes through this one
    function, so the rule is written once."""
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


async def _danbooru_posts(tags: str, *, limit: int,
                          random_order: bool = True) -> list[dict]:
    """Fetch a batch of Danbooru posts. The **one** implementation of every Danbooru post query.

    Anonymous Danbooru applies a 2-tag limit to `random=true` (and equally to
    `order:random`), so a multi-tag query that gets a 422 falls back to "latest
    N" and lets the caller pick randomly client-side. Slightly newer-biased but
    the UX impact is small, and better than returning a failure.

    Why one copy and not three: `_fetch_danbooru_post` /
    `_fetch_danbooru_posts_bulk` / `_fetch_danbooru_posts_random` each used to
    inline an almost identical `_attempt`, each opening its own `ClientSession`.
    As a result all three carried **no** User-Agent, **none** was counted by
    `_METRICS_API_CALLS`, and two of them did not even make a sound on a non-200.
    Merged into one, those two facts can now only be right for all three or
    wrong for all three, never diverge again.
    """
    base = {"tags": tags, "limit": limit}
    if random_order:
        # 422 is expected here (the 2-tag limit), so the first attempt stays
        # quiet about it; every other code still complains.
        status, data = await _http_get_json(
            DANBOORU_API, params={**base, "random": "true"},
            quiet_statuses=(422,))
        if status == 422:
            status, data = await _http_get_json(DANBOORU_API, params=base)
    else:
        status, data = await _http_get_json(DANBOORU_API, params=base)
    if status != 200:
        return []
    return _dict_entries(data)


def _take_unseen(posts: list[dict], count: int) -> list[dict]:
    """Pick up to `count` from `posts`, preferring ones not sent recently, and record them in the dedup queue.

    `_danbooru_recent` holds only `DANBOORU_HISTORY_SIZE` entries, so when the
    whole pool has been sent, reuse the whole pool (`fresh or posts`) -- better
    to repeat than to return empty.
    """
    fresh = [p for p in posts if p.get("id") not in _danbooru_recent]
    pool = fresh or posts
    if len(pool) > count:
        chosen = random.sample(pool, count)
    else:
        chosen = list(pool)
        random.shuffle(chosen)
    for p in chosen:
        pid = p.get("id")
        if pid is not None:
            _danbooru_recent.append(pid)
    return chosen


async def _fetch_danbooru_post(tags: str) -> dict | None:
    """Return one random Danbooru post matching `tags`, avoiding recent IDs."""
    posts = await _danbooru_posts(tags, limit=DANBOORU_FETCH_LIMIT)
    chosen = _take_unseen(posts, 1)
    return chosen[0] if chosen else None

# Fuzzy tag resolver -- both Danbooru and e621 expose a compatible `/tags.json`
# (`name`, `post_count`, `search[name_matches]` glob), and using its glob match
# is far better than Google at handling near-correct English input like "the
# user typing `lappland decadenza` to find
# `lappland_the_decadenza_(arknights)`". The whole resolver needs no API key
# and no scraping.
_FUZZY_MAX_TOKENS = 8  # cap to avoid a long input firing off too many API calls

DANBOORU_TAGS_API = "https://danbooru.donmai.us/tags.json"

_E621_TAGS_API = "https://e621.net/tags.json"

class _TagsAPI(NamedTuple):
    """Site descriptor for the fuzzy resolver. Both supported sites expose
    a `/tags.json` with compatible `name` / `post_count` /
    `search[name_matches]` semantics; only the URL and (for e621) the
    required descriptive UA differ. Add a new site by adding a constant
    and threading it through the relevant `mcmd_*` handler."""
    url: str
    headers: dict[str, str] | None

# e621's API rules likewise state, in writing, that a descriptive UA is
# required. The same string is fine -- what the site wants is "identifiable, and
# reachable if something goes wrong", not a separate identity per site.
#
# This used to read "Danbooru accepts aiohttp's default". That was once true,
# then the site went behind Cloudflare and it became false, with nothing to tell
# us -- no test hit the real endpoint, and the failure path silently returned
# None. Kept here as a memorial: a comment's "site X does not need one" is an
# assumption that expires, not a fact you can rely on. It is now **carried on
# every request**, so there is no expiry to worry about.
_E621_UA = _BOT_UA

_DANBOORU_TAGS = _TagsAPI(url=DANBOORU_TAGS_API, headers=None)

_E621_TAGS = _TagsAPI(url=_E621_TAGS_API, headers={"User-Agent": _E621_UA})

async def _query_tags_json(api: _TagsAPI, *, name: str | None = None,
                           name_matches: str | None = None,
                           order: str | None = None,
                           limit: int = 5) -> list[dict]:
    """A thin wrapper over `/tags.json`. Returns a list of tag dicts (each with
    `name`, `post_count`, etc.); any error returns []. `api` describes which
    site to hit."""
    params: dict = {"limit": limit}
    if name is not None:
        params["search[name]"] = name
    if name_matches is not None:
        params["search[name_matches]"] = name_matches
    if order is not None:
        params["search[order]"] = order
    status, data = await _http_get_json(
        api.url, params=params, headers=api.headers,
    )
    if status != 200:
        return []
    return _dict_entries(data)

def _is_search_modifier(tok: str) -> bool:
    """Danbooru search modifiers (`rating:general`, `score:>=5`, `order:rank` ...)
    are not tag names; the resolver must pass them through and not fuzzy-resolve
    them. Any token containing `:` is treated as a modifier."""
    return ":" in tok

# Minimum length for prefix fuzzy resolution of a single token. For a token of
# <=2 chars like `CP` / `bb` / `ru`, the most popular match for `cp*` is
# something completely unrelated like `cpu_(hexivision)`, almost always wrong.
# Exact match is not subject to this limit -- `bb` is itself a valid Danbooru
# tag, so a user typing the exact value still hits.
_FUZZY_MIN_TOKEN_LEN = 3

async def _best_tag_for_window(api: _TagsAPI, window: list[str]) -> str | None:
    """Find the tag with the highest post_count whose name contains each token
    in `window` in order. For a single token, first try exact
    (`search[name]=`), then, on a miss, prefix `tok*` (**not** `*tok*`, because
    a middle substring would drag in unrelated tags -- `*rossi*` pulls
    `animal_crossing` to the top). Only for multiple tokens use
    `*tok1*tok2*...*`, because several ordered substrings are precise enough.
    Both exact and fuzzy require `post_count > 0`; the site has many stale tags
    with zero posts. Returns None on a miss.

    An over-short single token (< `_FUZZY_MIN_TOKEN_LEN`) skips prefix fuzzy, to
    avoid a wrong resolution like `cp` -> `cpu_(hexivision)`. Exact match is
    still attempted."""
    if not window:
        return None
    # Take `name` with .get rather than `[...]`: when the site returns a tag
    # object missing the `name` field, the original `hits[0]["name"]` would
    # raise KeyError all the way up to the mention dispatcher's catch-all,
    # turning "tag not found" into an error reply. A missing field counts as a
    # miss.
    def _pick(hits: list[dict]) -> str | None:
        if not hits:
            return None
        top = hits[0]
        if (top.get("post_count") or 0) <= 0:
            return None
        name = top.get("name")
        return name if isinstance(name, str) and name else None

    if len(window) == 1:
        tok = window[0]
        exact = _pick(await _query_tags_json(api, name=tok, limit=1))
        if exact:
            return exact
        if len(tok) < _FUZZY_MIN_TOKEN_LEN:
            return None
        return _pick(await _query_tags_json(
            api, name_matches=f"{tok}*", order="count", limit=3,
        ))
    pattern = "*" + "*".join(window) + "*"
    return _pick(await _query_tags_json(
        api, name_matches=pattern, order="count", limit=3,
    ))

async def _resolve_fuzzy_tags(api: _TagsAPI, raw: str) -> str | None:
    """Greedy longest match: turn a user's loose query into canonical tags.
    Example (Danbooru): `lappland decadenza yuri` ->
    `lappland_the_decadenza_(arknights) yuri`.
    Only returns a new string if at least one token was rewritten; returns None
    if nothing changed (the caller then lets the original 0-results message
    through). `api` decides which site's `/tags.json` to hit."""
    tokens = [t for t in raw.split() if t][:_FUZZY_MAX_TOKENS]
    if not tokens:
        return None
    resolved: list[str] = []
    changed = False
    i = 0
    while i < len(tokens):
        if _is_search_modifier(tokens[i]):
            resolved.append(tokens[i])
            i += 1
            continue
        match: tuple[str, int] | None = None  # (canonical_tag, end_index)
        # Try from the longest window down to the shortest; skip any window
        # containing a modifier.
        for j in range(len(tokens), i, -1):
            window = tokens[i:j]
            if any(_is_search_modifier(t) for t in window):
                continue
            tag = await _best_tag_for_window(api, window)
            if tag:
                match = (tag, j)
                break
        if match:
            tag, j = match
            if tag != " ".join(tokens[i:j]):
                changed = True
            resolved.append(tag)
            i = j
        else:
            resolved.append(tokens[i])
            i += 1
    return " ".join(resolved) if changed else None

async def _fetch_danbooru_posts_bulk(tags: str, limit: int = 30) -> list[dict]:
    """Pull a batch of posts for tag_suggest aggregation: ~30 at once, **no dedup**, return the raw list."""
    return await _danbooru_posts(tags, limit=limit)

async def _fetch_danbooru_posts_latest(tags: str, limit: int = 4) -> list[dict]:
    """Fetch the latest N posts for `tags` (not random; the default order is newest-first)."""
    params = {"tags": tags, "limit": limit}
    status, data = await _http_get_json(DANBOORU_API, params=params)
    if status != 200:
        return []
    return _dict_entries(data)

async def _fetch_danbooru_post_latest(tags: str) -> dict | None:
    """Fetch the **single latest** post for `tags`. No dedup (when the user
    explicitly asks for latest, give the latest and do not skip it). Used by
    `@bot booru <tag> --latest`."""
    posts = await _fetch_danbooru_posts_latest(tags, limit=1)
    return posts[0] if posts else None

async def _fetch_danbooru_posts_random(tags: str, count: int = 4) -> list[dict]:
    """Randomly fetch N posts for `tags`. 30-pool plus 422 fallback, then pick
    client-side. Dedups against `_danbooru_recent`; returns however many it has
    if short of count. Used by `@bot booru <tag> --grid`."""
    posts = await _danbooru_posts(tags, limit=DANBOORU_FETCH_LIMIT)
    return _take_unseen(posts, count)

# UA rules of each booru site for anonymous clients:
# - Safebooru: the urllib / aiohttp default UA gets a 401 at some PoPs; a
#   browser-style UA works.
# - e621: requires a descriptive UA (site name + purpose); ignoring the rule
#   gets you banned; it is stated in writing in their API rules.
# - IQDB: uses JS anti-bot protection; you must add `notabot=1` to the query
#   string to get real results, otherwise it returns a "You look like a bot"
#   notice page.
# - Gelbooru: the anonymous API is now locked behind Cloudflare (401), even a
#   browser UA does not help. Supporting it would need `&api_key=` + `&user_id=`
#   config; not implemented.
_BROWSER_UA = "Mozilla/5.0 (compatible; axiomatic-bot/1.0)"

_SAFEBOORU_API = "https://safebooru.org/index.php"

_E621_API = "https://e621.net/posts.json"

async def _fetch_safebooru_post(tags: str) -> dict | None:
    """Pull one random post from Safebooru. Uses the `sort:random` meta tag over
    the Gelbooru-style dapi."""
    full_tags = tags if "sort:" in tags else f"{tags} sort:random".strip()
    params = {
        "page": "dapi", "s": "post", "q": "index", "json": 1,
        "tags": full_tags, "limit": 30,
    }
    status, data = await _http_get_json(
        _SAFEBOORU_API, params=params, headers={"User-Agent": _BROWSER_UA},
    )
    posts = _dict_entries(data) if status == 200 else []
    return random.choice(posts) if posts else None

async def _fetch_e621_post(tags: str) -> dict | None:
    """Pull one random post from e621. Uses the `order:random` meta tag."""
    full_tags = tags if "order:" in tags else f"{tags} order:random".strip()
    params = {"tags": full_tags, "limit": 30}
    status, data = await _http_get_json(
        _E621_API, params=params, headers={"User-Agent": _E621_UA},
    )
    if status != 200 or not isinstance(data, dict):
        return None
    # `posts` may not be a list when the site changes structure / returns an
    # error object (e.g. a dict) -- `random.choice` raises KeyError on a dict
    # and TypeError on other types.
    posts = _dict_entries(data.get("posts"))
    return random.choice(posts) if posts else None
