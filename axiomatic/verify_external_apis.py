#!/usr/bin/env python3
"""verify_external_apis.py -- a standalone "are the external APIs still alive?" verification script.

    py -3 axiomatic/verify_external_apis.py
    py -3 axiomatic/verify_external_apis.py --only danbooru wiki
    py -3 axiomatic/verify_external_apis.py --json

**Why this is needed (the two 2026-08-30 incidents).** This bot hits a dozen-odd
third-party endpoints, and every entry point's failure path is "return None /
return empty list", which `mcmd_*` then turns into a "not found". So the moment a
site changes its rules, the feature vanishes entirely, while the user assumes
they mistyped and the log may not have a single word. Two were caught on the
same day:

* The biggest image board moved behind a CDN protection and refused requests
  **with no User-Agent** (which is aiohttp's default) -> six entry points wiped
  out, including image download.
* An encyclopedia site required the UA to carry a reachable **contact** (URL or
  email); merely naming yourself still gets a 403.

Neither is a program-logic bug, both are **external-contract drift** -- no
static analysis can see it, only actually hitting the endpoint once. The unit
tests include two live checks (the two known-to-drift sites), but stuffing all
dozen endpoints into `pytest` is wrong: everyone hammering third-party sites on
every test run is slow, noisy, and exactly the behaviour that invites rate
limiting. So, following this repo's existing convention (`verify_browser.py` /
`verify_quota_dialog.py`), the full scan is a **manual verification entry
point**.

**Some hard design constraints:**

* **Walk the path the bot actually uses.** Every JSON endpoint goes out through
  `_external_apis._http_get_json`, so User-Agent, `api_contact`, timeout, and
  exception handling are all identical to production. Writing your own HTTP call
  would not verify either of today's bugs -- they **were** in the headers.
* **Read-only, no side effects.** GET only (the anime database is GraphQL,
  POST-only, so it gets a minimal read-only query). Writes no files, does not
  touch the production login state, needs no credentials.
* **The endpoint list is this script's data, not a comment.**
  `test_external_apis.py` reconciles this list against the URLs that actually
  appear in `discord_bot.py`, and goes red on an unregistered one -- otherwise
  the next time someone adds an endpoint, this scan silently misses it, which is
  exactly the failure mode we are guarding against.
* **Diagnostics must be directly actionable, and must never guess.** A 403 does
  not just print a status code. But the order is "what the upstream itself said"
  first, guess second -- on 2026-09-07 the anime database returned 403 for a
  reason written in the response body (upstream disabled the service itself),
  unrelated to the UA, and the diagnosis at the time reported it as a
  "User-Agent problem" and told the user to set `api_contact`. **A verifier that
  misreports ends up like a gatekeeper who cries wolf: nobody looks at it any
  more.**

**Three-way exit code**, same semantics as `verify_browser.py`: `0` = every
selected item was verified and healthy; `1` = a site really refused (a `FAIL`
like 4xx/5xx); `3` = no FAIL, but some endpoint was **not verified**
(`UNREACHABLE` cannot connect, or `SKIP` missing a precondition). Offline, the
whole run is a sea of `UNREACHABLE`, which is not a failure (so not 1), but must
**not be reported as success** either: before 2026-09-20 this case was exit 0,
and a caller that only reads the exit code (self-driving loops, schedulers) would
read "verified nothing" as "all fine". 3 not 2, because argparse uses 2 itself on
a bad argument.

**`SKIP` is on the same side as `UNREACHABLE` -- this cell was added later the
same day.** When the three-way split was first built, `SKIP` counted as 0, with
the stated reason "a deliberate skip is not the same as unverified"; but there is
nothing "deliberately skipped" in this script: the only two `SKIP`s both come
from the CDN entry failing to get a sample image (the upstream API did not
answer, or the post that came back had no image URL), which means exactly **not
verified**. Measured `--only cdn` (`cdn` is its own group, selecting it does not
also select the image-board APIs) with no sample printed "1 ok, 0 failed, 0
unreachable", exit 0 -- verified nothing, while the exit code says all fine,
exactly the shape the three-way split exists to kill. `verify_browser.py`'s
`SKIP` is exit 3; since the two claim the same semantics, they should not diverge
on this cell.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _external_apis as ex  # noqa: E402


def _fresh_probe_word() -> str:
    """A query word that is different every run and could not be a real word. See the comment on that entry in the `_ENDPOINTS` dict."""
    return "zzverify" + secrets.token_hex(4)


_DICT_PROBE_WORD = _fresh_probe_word()

# --------------------------------------------------------------------------
# Endpoint list
# --------------------------------------------------------------------------
# Each entry: (group, description, method, URL, params, extra headers)
# The group name is for `--only`. The URL must match the literal in
# `discord_bot.py` / `_external_apis.py` -- the guard test reconciles them, and
# an unregistered one goes red.
_ENDPOINTS: list[dict] = [
    {"group": "danbooru", "what": "image-board posts (random image / grid / latest all go through this)",
     "url": ex.DANBOORU_API,
     "params": {"tags": "rossi_(arknights)", "limit": 1}},
    {"group": "danbooru", "what": "image-board tags (the fuzzy tag resolver)",
     "url": ex.DANBOORU_TAGS_API, "params": {"search[name]": "yuri", "limit": 1}},
    {"group": "danbooru", "what": "image-board post count",
     "url": "https://danbooru.donmai.us/counts/posts.json",
     "params": {"tags": "yuri"}},
    {"group": "danbooru", "what": "image-board wiki entry",
     "url": "https://danbooru.donmai.us/wiki_pages/yuri.json", "params": None},
    {"group": "e621", "what": "e621 posts",
     "url": ex._E621_API, "params": {"tags": "canine", "limit": 1},
     "headers": {"User-Agent": ex._E621_UA}},
    {"group": "e621", "what": "e621 tags",
     "url": ex._E621_TAGS_API, "params": {"search[name]": "canine", "limit": 1},
     "headers": {"User-Agent": ex._E621_UA}},
    {"group": "safebooru", "what": "Safebooru posts",
     "url": ex._SAFEBOORU_API,
     "params": {"page": "dapi", "s": "post", "q": "index", "json": 1,
                "tags": "sort:random", "limit": 1},
     "headers": {"User-Agent": ex._BROWSER_UA}},
    {"group": "wiki", "what": "encyclopedia summary (needs a contact in the UA)",
     "url": "https://en.wikipedia.org/api/rest_v1/page/summary/"
            "Python_(programming_language)", "params": None},
    # This dict entry **queries a nonexistent, different word every run, and a
    # healthy origin returns 404**. Measured 2026-09-19: when the origin is
    # unreachable, the fronting CDN returns a weeks-old stale cache (200, after
    # ~20s) for "words already queried"; only a never-cached word exposes the
    # 522. The old code fixed the word to `serendipity`, so once the timeout was
    # aligned to the bot it would report ok while the service was actually
    # broken. A new word each time forces the request all the way to the origin.
    # `timeout` is the timeout the bot's own call uses for this (`bot_timeout` is
    # the name of that constant), reconciled by
    # `test_external_apis.test_a_per_call_timeout_in_the_bot_is_mirrored_here`.
    {"group": "dict", "what": "English dictionary (query a nonexistent word; a healthy origin returns 404)",
     "url": "https://api.dictionaryapi.dev/api/v2/entries/en/" + _DICT_PROBE_WORD,
     "params": None, "ok_statuses": (404,),
     "timeout": 30.0, "bot_timeout": "DICT_TIMEOUT_SEC"},
    {"group": "xkcd", "what": "latest xkcd",
     "url": "https://xkcd.com/info.0.json", "params": None},
    {"group": "quote", "what": "quotation",
     "url": "https://zenquotes.io/api/random", "params": None},
    {"group": "fact", "what": "trivia",
     "url": "https://uselessfacts.jsph.pl/random.json?language=en",
     "params": None},
    {"group": "joke", "what": "joke",
     "url": "https://icanhazdadjoke.com/", "params": None,
     "headers": {"Accept": "application/json"}},
    {"group": "dog", "what": "dog image",
     "url": "https://dog.ceo/api/breeds/image/random", "params": None},
    {"group": "crypto", "what": "coin search",
     "url": "https://api.coingecko.com/api/v3/search", "params": {"query": "btc"}},
    {"group": "crypto", "what": "coin price",
     "url": "https://api.coingecko.com/api/v3/simple/price",
     "params": {"ids": "bitcoin", "vs_currencies": "usd"}},
    {"group": "github", "what": "repository lookup",
     "url": "https://api.github.com/repos/python/cpython", "params": None},
    # Below are not JSON APIs; only "can we reach it, does it return something"
    # is verified.
    #
    # The three `embed_only` entries need special note: the bot **does not fetch
    # them itself**, it just pastes the URL into a message / embed and lets the
    # chat platform fetch the image. So there is no fetch call in the source, and
    # a static scan cannot see them -- but the symptom of a site being down is
    # the same to the user (a broken image), and harder to investigate because
    # there is not even a line in our log. So they stay in this scan, they just
    # do not take part in the "only listed if the source fetches it" comparison.
    {"group": "cat", "what": "cat image (URL pasted, platform fetches it)", "raw": True,
     "embed_only": True,
     "url": "https://cataas.com/cat?ts=1", "params": None},
    {"group": "color", "what": "solid-color image (embed, platform fetches it)", "raw": True,
     "embed_only": True,
     "url": "https://singlecolorimage.com/get/ff0000/200x200", "params": None},
    {"group": "qr", "what": "QR generator (embed, platform fetches it)", "raw": True,
     "embed_only": True,
     "url": "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=hi",
     "params": None},
    {"group": "iqdb", "what": "reverse image search (HTML, needs a browser-style UA)", "raw": True,
     "url": "https://iqdb.org/", "params": None,
     "headers": {"User-Agent": ex._BROWSER_UA},
     "timeout": 20.0, "bot_timeout": "IQDB_TIMEOUT_SEC"},
    {"group": "anime", "what": "anime database (GraphQL, POST only)",
     "url": "https://graphql.anilist.co", "params": None, "method": "POST",
     "json_body": {"query": "query{Media(search:\"Frieren\",type:ANIME)"
                            "{id title{romaji}}}"},
     "timeout": 15.0, "bot_timeout": "ANIME_TIMEOUT_SEC"},
    {"group": "cdn", "what": "the image board's image CDN (`--grid` download goes through this)",
     "raw": True, "cdn": True, "url": None, "params": None,
     "timeout": 30.0, "bot_timeout": "GRID_DOWNLOAD_TIMEOUT_SEC"},
]

_GROUPS = sorted({e["group"] for e in _ENDPOINTS})

# The timeout `_check_raw` / `_check_post` use when an entry has **no** `timeout`.
# The only entries that reach it are the three `embed_only` ones (cat, color,
# QR): those URLs are fetched **by the chat platform itself**, the bot side never
# issues a request, so there is no "bot timeout" to copy -- this is just a finite
# cap for the scan. Non-JSON endpoints the bot does fetch always carry
# `timeout` / `bot_timeout` (required by
# `test_external_apis.test_a_per_call_timeout_in_the_bot_is_mirrored_here`), so
# this value never applies to any endpoint the bot really hits.
_EMBED_ONLY_TIMEOUT_SEC = 20.0

# The timeout for `_error_body`'s "hit it once more after already failing, only
# to grab the upstream's explanation" request. It decides no verdict (the verdict
# was fixed by the first request), it only affects whether one more reason line
# can be printed, so it is not reconciled against the bot; it has a name so the
# "no hardcoded timeout numbers in the verifier" guard holds.
_ERROR_BODY_TIMEOUT_SEC = ex._HTTP_TIMEOUT_SEC


def _unreachable(timeout: float, error: Exception | None = None
                 ) -> tuple[str, str]:
    """The one phrasing for "no response". **Always print the timeout cap.**

    "Cannot connect" and "slower than our timeout" are two completely different
    problems, and the original `_check_json` line ("cannot connect (offline?
    timeout?)") pointed the reader at a network fault. Measured 2026-09-08 the
    `dict` entry was the latter: the endpoint returned **HTTP 200, it just took
    ~20 seconds**, while the default timeout was 15 -- so it failed every time,
    and the log said it "cannot connect". Print the timeout value so it can be
    compared directly.

    **The three check functions share this one because originally only one had
    learned it.** Before 2026-09-20 `_check_raw` and `_check_post` returned a
    bare `TimeoutError` -- the same misdirection, and their two entries' timeouts
    (iqdb 20s, CDN 30s) are exactly the ones most likely to be "alive but slower
    than the cap". When there is an exception object, report its name too:
    "the connection never even opened" (DNS / blocked) must be told apart from
    "no reply arrived".
    """
    if error is not None and not isinstance(error, TimeoutError):
        return "UNREACHABLE", (
            f"{type(error).__name__} (timeout cap {timeout:g}s) -- the "
            "connection itself never opened, most likely offline / DNS / "
            "blocked, not the other end replying slowly.")
    named = f"{type(error).__name__}: " if error is not None else ""
    return "UNREACHABLE", (
        f"{named}no response (timeout cap {timeout:g}s). Two causes to check "
        "separately: **truly unreachable** (offline / DNS / blocked), or "
        "**the endpoint is alive but slower than this cap**. To tell them "
        "apart: hit the same URL with a browser or curl and time it -- a 200 "
        "means the latter, and then the fix is the timeout of that call on the "
        "bot side, not the network.")


def _expected_status(status: int) -> tuple[str, str]:
    """A non-200 that the entry itself declares "is what a healthy origin returns". Shared by all three checks."""
    return "OK", f"HTTP {status} (this is what this endpoint returns when healthy)"


async def _check_json(entry: dict) -> tuple[str, str]:
    """Walk the path the bot actually uses. Returns (verdict, detail).

    **The timeout is always equal to the bot's timeout for the same call, never
    looser.** This script's whole value is "same path and same timeout as the
    bot" -- loosen the timeout here and this script reports OK while the bot is
    plainly broken, which is worse than not having it. When an endpoint is slower
    than `_http_get_json`'s default, the correct fix is **to fix the call on the
    bot side** (give that call a long enough timeout), then copy that value here.
    After the bot's dictionary call was loosened to `DICT_TIMEOUT_SEC` on
    2026-09-08, this script stayed at 15s until it was aligned on 2026-09-19 --
    the same shape in reverse: a timeout **narrower** than the bot reports
    unreachable while the bot is fine. So the two sides are now reconciled by a
    test, rather than relying on someone remembering.

    `ok_statuses` are "the non-200 an endpoint returns when healthy" (the
    dictionary returns 404 for a nonexistent word).
    """
    timeout = entry.get("timeout", ex._HTTP_TIMEOUT_SEC)
    ok_statuses = tuple(entry.get("ok_statuses", ()))
    status, data = await ex._http_get_json(
        entry["url"], params=entry.get("params"),
        headers=entry.get("headers"), timeout=timeout,
        quiet_statuses=ok_statuses)
    if status == -1:
        return _unreachable(timeout)
    if status in ok_statuses:
        return _expected_status(status)
    if status != 200:
        body = await _error_body(entry["url"], params=entry.get("params"),
                                 headers=entry.get("headers"))
        return "FAIL", f"HTTP {status}{_diagnose(status, body=body)}"
    if data is None:
        return "FAIL", "HTTP 200 but the response is not JSON"
    size = len(data) if isinstance(data, (list, dict)) else "?"
    return "OK", f"HTTP 200, {type(data).__name__}[{size}]"


async def _check_raw(entry: dict) -> tuple[str, str]:
    """A non-JSON endpoint: only confirm it is reachable, the status code is fine, and it returns a content type."""
    import aiohttp

    url = entry["url"]
    if entry.get("cdn"):
        # The CDN has no fixed URL to hit -- first ask the API for an existing
        # image, then fetch it. This verifies the real download path (which is
        # what `--grid` does).
        post = await ex._fetch_danbooru_post("rating:general")
        if not post:
            return "SKIP", "cannot get a sample post (the upstream API is already a FAIL)"
        url = (post.get("large_file_url") or post.get("file_url")
               or post.get("preview_file_url"))
        if not url:
            return "SKIP", "the sample post has no image URL"
    headers = dict(entry.get("headers") or {})
    headers.setdefault("User-Agent", ex._user_agent())
    # Copy the timeout from the bot side (`timeout` / `bot_timeout` reconciled);
    # only embed_only entries have none.
    timeout = entry.get("timeout", _EMBED_ONLY_TIMEOUT_SEC)
    # `ok_statuses` is recognised by all three checks. Originally only
    # `_check_json` read it -- a key that is **declared but nobody reads** has no
    # symptom (same shape as that stale `_OWNER_ONLY_SLASH` string), and its
    # failure direction is crying wolf: that entry would forever report FAIL.
    ok_statuses = tuple(entry.get("ok_statuses", ()))
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status in ok_statuses:
                    return _expected_status(resp.status)
                if resp.status >= 400:
                    body = await _read_body_text(resp)
                    why = _diagnose(resp.status, body=body)
                    return "FAIL", f"HTTP {resp.status}{why}"
                return "OK", f"HTTP {resp.status}, {resp.content_type}"
    except Exception as error:  # pylint: disable=broad-except
        return _unreachable(timeout, error)


async def _check_post(entry: dict) -> tuple[str, str]:
    import aiohttp

    headers = {"User-Agent": ex._user_agent()}
    headers.update(entry.get("headers") or {})
    # Same as `_check_raw`: copy the timeout from the bot side. Before 2026-09-19
    # this hardcoded 20s, while the bot's POST was 15s -- the verifier looser
    # than the bot, reporting ok when the bot would time out.
    timeout = entry.get("timeout", _EMBED_ONLY_TIMEOUT_SEC)
    ok_statuses = tuple(entry.get("ok_statuses", ()))
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(entry["url"], json=entry["json_body"],
                                    headers=headers) as resp:
                if resp.status in ok_statuses:
                    return _expected_status(resp.status)
                if resp.status != 200:
                    body = await _read_body_text(resp)
                    why = _diagnose(resp.status, body=body, method="POST")
                    return "FAIL", f"HTTP {resp.status}{why}"
                body = await resp.json()
                if not isinstance(body, dict) or "data" not in body:
                    return "FAIL", "HTTP 200 but the response shape is wrong"
                return "OK", "HTTP 200, data"
    except Exception as error:  # pylint: disable=broad-except
        return _unreachable(timeout, error)


# Read cap for the diagnostic body. A failure page is usually only a few hundred
# bytes, but the body is sent by a third party, and no cap means "there is an
# unbounded read on the error-handling path".
_DIAG_BODY_CAP = 64 * 1024


async def _read_body_text(resp) -> str:
    """Read a failure response's body as text. Return an empty string if it cannot be read -- a diagnostic must not blow up the verification itself.

    Goes through `read_capped_body` rather than `resp.text()`: the latter is
    unbounded, and a single `read(n)` call truncates a chunked response (the very
    defect fixed 2026-09-07). Encoding explicit, `errors="replace"` -- these
    bytes are not content we control (CLAUDE.md hard rule).
    """
    try:
        raw = await ex.read_capped_body(resp.content, _DIAG_BODY_CAP)
    except Exception:  # pylint: disable=broad-except
        return ""
    if raw is None:
        return ""
    return raw.decode("utf-8", errors="replace")


async def _error_body(url, *, params=None, headers=None) -> str:
    """Hit it once more after failing, only to extract the upstream's explanation. Read-only, and only happens after already failing.

    `_http_get_json` returns only `(status, data)` and cannot hand back the
    body -- and nine times out of ten a 4xx's reason is written in the body. The
    cost of one extra request is only paid when things are **already broken**,
    and buys not having to guess.
    """
    import aiohttp

    merged = {"User-Agent": ex._user_agent()}
    merged.update(headers or {})
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_ERROR_BODY_TIMEOUT_SEC)
        ) as session:
            async with session.get(url, params=params, headers=merged) as resp:
                return await _read_body_text(resp)
    except Exception:  # pylint: disable=broad-except
        return ""


def _upstream_message(body: str | None, *, limit: int = 240) -> str:
    """Extract the **upstream's own** explanation from the response body; return an empty string if none.

    Reads JSON only. An HTML failure page (a CDN challenge page) yields nothing,
    and then falling back to the guess below is the right thing -- a challenge
    page really is a UA / protection problem.

    The body is third-party bytes, so always squash to a single line and
    truncate; do not let a whole page spew into the console.
    """
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return ""
    candidates = []
    if isinstance(parsed, dict):
        errors = parsed.get("errors")
        if isinstance(errors, list):
            candidates += [item.get("message") for item in errors
                           if isinstance(item, dict)]
        candidates += [parsed.get(key)
                       for key in ("message", "error", "detail", "title")]
    for text in candidates:
        if isinstance(text, str):
            cleaned = " ".join(text.split())
            if cleaned:
                return cleaned[:limit]
    return ""


def _diagnose(status: int, *, body: str | None = None,
              method: str = "GET") -> str:
    """Translate a status code into "what to do next". A 403 is not diagnosable from the code alone.

    **Two kinds of 403 must be told apart, or this tool sends people the wrong
    way.** The 2026-09-07 counterexample: the anime database entry returned 403,
    and the reason was right there in the response body -- upstream had disabled
    the API itself ("temporarily disabled due to severe stability issues"). It
    had nothing to do with the UA: no UA, browser UA, and our UA were all
    measured returning 403. But the function at the time unconditionally applied
    "this kind is almost always a User-Agent problem" plus "`api_contact` not
    set", so an "upstream is disabled" got reported as "your headers are wrong",
    a direction no amount of investigation could resolve.

    So the order is:

    1. **What the upstream itself said** first -- that is fact, not a guess;
    2. A guess is only for "the shared-UA GET path". A non-GET endpoint's headers
       differ from `_http_get_json`'s anyway (the GraphQL entry is POST-only), so
       applying the UA story to it has no basis at all.
    """
    if not (500 <= status <= 599 or status in (401, 403, 429)):
        return ""
    # "What the upstream itself said" first, **5xx included -- this cell was
    # added 2026-09-20**. Originally 5xx returned the guess below directly, so a
    # 503 carrying a JSON explanation ("maintenance until X") would be rewritten
    # by this tool into "52x usually means the fronting CDN cannot reach the
    # origin", exactly violating the rule set above: the guess overwriting the
    # fact. The same shape already hurt once on a 403 (2026-09-07 anime
    # database).
    upstream = _upstream_message(body)
    if upstream:
        return f"  <- upstream said: {upstream}"
    if 500 <= status <= 599:
        # The dictionary entry's 522 on 2026-09-19: the CDN cannot reach the
        # origin. This kind has nothing to do with our request, so do not send
        # people to check the headers or `api_contact` -- neither direction
        # turns up anything here.
        return ("  <- the upstream side errored (52x usually means the fronting "
                "CDN cannot reach the origin), not a problem with our request; "
                "wait for them to recover, or consider another source")
    if method != "GET":
        return ("  <- a non-GET endpoint, its headers differ from the shared "
                "GET path; read the response body first, do not assume it is "
                "the User-Agent")
    hints = ["this kind is almost always a User-Agent problem, not a wrong URL"]
    if not ex._configured_contact():
        hints.append(f"`{ex._UA_CONTACT_KEY}` is not set -- some sites (e.g. the "
                     "encyclopedia) require the UA to carry a reachable contact, "
                     "and just naming yourself is not enough")
    hints.append("do not pretend to be a browser: measured, that gets blocked too")
    return "  <- " + "; ".join(hints)


# What each verdict looks like on the console. Lifted to module constants so it
# **can be reconciled**: the verdict strings are scattered across the three check
# functions' `return`s, and adding one but forgetting to register it makes `_run`
# KeyError mid-scan; the quieter half is `_exit_code` -- a new verdict not in
# `_UNVERIFIED_VERDICTS` is automatically counted as "verified and healthy". Both
# sides are reconciled by `test_external_apis` against the verdict set derived
# from the source.
_VERDICT_MARKS = {
    "OK": "  ok  ",
    "FAIL": " FAIL ",
    "UNREACHABLE": " ---- ",
    "SKIP": " skip ",
}


async def _run(only: list[str] | None) -> list[dict]:
    results = []
    for entry in _ENDPOINTS:
        if only and entry["group"] not in only:
            continue
        if entry.get("method") == "POST":
            verdict, detail = await _check_post(entry)
        elif entry.get("raw"):
            verdict, detail = await _check_raw(entry)
        else:
            verdict, detail = await _check_json(entry)
        results.append({"group": entry["group"], "what": entry["what"],
                        "verdict": verdict, "detail": detail})
        if not _QUIET:
            print(f"[{_VERDICT_MARKS[verdict]}] {entry['group']:<10} "
                  f"{entry['what']}")
            if verdict != "OK":
                print(f"           {detail}")
            sys.stdout.flush()
    return results


_QUIET = False

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNVERIFIED = 3     # see module docstring: no FAIL, but some endpoint not verified

# The "not verified" verdicts. See module docstring: `SKIP` is on the same side
# as `UNREACHABLE`, because this script has no "deliberately skipped" endpoint --
# both sources of `SKIP` are "a precondition could not be met, so nothing was
# verified".
_UNVERIFIED_VERDICTS = frozenset({"UNREACHABLE", "SKIP"})


def _exit_code(results: list[dict]) -> int:
    """The three-way conclusion. A pure function: FAIL first, then "any unverified", and only otherwise 0."""
    verdicts = {r["verdict"] for r in results}
    if "FAIL" in verdicts:
        return EXIT_FAIL
    if verdicts & _UNVERIFIED_VERDICTS:
        return EXIT_UNVERIFIED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    global _QUIET
    parser = argparse.ArgumentParser(
        description="Verify every external API the bot uses is still alive. Read-only, no side effects.")
    parser.add_argument("--only", nargs="+", metavar="GROUP",
                        choices=_GROUPS,
                        help=f"only verify these groups (choices: {' '.join(_GROUPS)})")
    parser.add_argument("--json", action="store_true",
                        help="output JSON, for a program to read")
    # `argv or []`: `None` always means no flags, do not read `sys.argv` (under
    # pytest that is pytest's own command line). Convention and guard in
    # `test_suite_safety.py`.
    args = parser.parse_args(argv or [])
    _QUIET = args.json

    if not _QUIET:
        print(f"User-Agent: {ex._user_agent()}")
        if not ex._configured_contact():
            print(f"(`{ex._UA_CONTACT_KEY}` is not set -- sites that need a contact will 403)")
        print()

    results = asyncio.run(_run(args.only))
    failed = [r for r in results if r["verdict"] == "FAIL"]
    unreachable = [r for r in results if r["verdict"] == "UNREACHABLE"]
    skipped = [r for r in results if r["verdict"] == "SKIP"]
    # **"ok" is counted, not subtracted.** It originally read "total - failed -
    # unreachable", so `SKIP` was counted into ok: `--only cdn` with no sample
    # image printed "1 ok, 0 failed, 0 unreachable". Subtraction silently folds
    # every unlisted verdict into the good ones, which is the one thing these
    # numbers should make clear.
    ok = [r for r in results if r["verdict"] == "OK"]

    code = _exit_code(results)
    if _QUIET:
        print(json.dumps({"results": results, "failed": len(failed),
                          "unreachable": len(unreachable),
                          "skipped": len(skipped), "ok": len(ok),
                          "exit": code}, ensure_ascii=False, indent=2))
    else:
        print()
        print(f"{len(ok)} ok, {len(failed)} failed, "
              f"{len(unreachable)} unreachable, {len(skipped)} skipped")
        if unreachable and not failed:
            print("(everything unreachable usually means no network, not the site refusing)")
        for r in failed:
            print(f"  FAIL  {r['group']:<10} {r['what']} -- {r['detail']}")
        if code == EXIT_UNVERIFIED:
            print(f"exit {EXIT_UNVERIFIED}: some endpoint was not verified "
                  "(unreachable, or skipped for a missing precondition) -- not a "
                  "failure, but cannot be taken as all-fine either. Try again "
                  "later.")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
