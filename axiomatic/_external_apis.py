"""外部 API 的「抓取／解析」helper——純資料擷取模組（P2 重構，從 discord_bot.py 抽出）。

只含『取資料／解析回應』邏輯：共用的 HTTP 取 JSON 包裝（_http_get_json）、Danbooru／
Safebooru／e621 的 post 抓取、以及 /tags.json 的模糊 tag resolver。`mcmd_*` 的 dispatch
與 Discord 回覆組裝（含 _send_danbooru_* / grid 圖片合成）仍留在 discord_bot.py，由那邊
呼叫本模組的 helper 拿到資料後自行組回覆。

重要（避免循環 import）：本模組**不可** import discord_bot。這些 helper 不碰 Discord、
不呼叫 bot 端的回覆／redact／log helper；唯一原本的 bot 耦合是 API 呼叫計數器
_METRICS_API_CALLS，已一併搬進來、由 `api_call_count()` 供 bot 的 !metrics 讀取。

不洩漏規則：模組內部可用外部服務真實名稱（程式需要），但這些 helper 一律不產生送往
Discord 的字串——錯誤只 print 到 stderr、對外回覆由 bot 端組裝並維持泛用。
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
except ImportError:  # 套件路徑（`from axiomatic import _external_apis`）
    from axiomatic._bot_config import load_bot_config  # type: ignore


def api_call_count() -> int:
    """本 session 內發出的外部 API 呼叫次數（供 bot 的 `/health`／`!metrics` 讀取）。

    2026-08-30 之前這個數字是漏的：三個 Danbooru fetcher 自己開 `ClientSession`、
    繞過 `_http_get_json`，於是完全沒被計數。現在本模組**只有** `_http_get_json`
    一個外送出口，所以這個數字就是實際發出的請求數。
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
# User-Agent：每一個外送請求都要帶，而且要帶「說明自己是誰」的那一種
# --------------------------------------------------------------------------
# 2026-08-30 實測：Danbooru 整站已經在 Cloudflare 後面，對 **aiohttp 的預設**
# （＝根本沒有 User-Agent 標頭）直接回 403 ＋ 一頁 "Just a moment..." 挑戰頁。
# 本專案所有 Danbooru 呼叫當時都沒帶 UA，於是六個進入點——`@bot rossi` 的隨機圖、
# `--grid`、`--latest`、tag_suggest 的 bulk 抓取、以及整條 fuzzy tag resolver
# ——**全部失效**，而且是安靜的：使用者只看到「找不到」，會以為是自己 tag 打錯。
# 同一時刻 e621 與 Safebooru 完全正常，差別就只有這個標頭。
#
# 修法照站方明文規定（Help:Api 與 forum #37341）：帶一個能**說明自己是誰**的 UA，
# 不要用函式庫預設值、也不要假裝成瀏覽器。三種都實測過：
#     沒有 UA（aiohttp 預設）        -> 403
#     完整的 Chrome 140 瀏覽器 UA    -> 403
#     說明式 UA（就是下面這個）      -> 200
# 注意中間那一行：假裝成瀏覽器不只違反站規，實務上還**比誠實申報更容易被擋**——
# 挑戰頁預期真瀏覽器會去解 JS，我們解不了，於是被判定為冒充。
#
# 站方另外建議長時間連續請求維持在每秒 1 次左右（互動指令這種短爆發走的是他們的
# burst pool，不在此限）。這次的 403 與速率無關：同樣的請求節奏換上這個 UA 就是
# 200，換回瀏覽器 UA 就又是 403。
_BOT_UA = "axiomatic-bot/1.0 (Discord bot; private use)"

# ——但「說得出自己是誰」跟「聯絡得到人」是兩件事，而且有站台只吃後者。
# 2026-08-30 同樣實測到 Wikimedia 的 REST API 回 403，而且它給了兩種不同的訊息，
# 剛好把這個分界講得很清楚：
#     完全沒有 UA        -> 「Please set a user-agent and respect our robot policy」
#     `_BOT_UA`（描述式）-> 「Please respect our robot policy … Contact
#                            bot-traffic@wikimedia.org if you need higher volumes」
#     UA 裡有 URL 或 email -> 200
# 也就是說第二則不是在抱怨我們沒自報身分，是在說「你沒留下聯絡方式」。
#
# 聯絡方式放在 `bot_config.json` 的 `api_contact`，**預設空字串**。這個字串會被
# 送到第三方站台，放什麼上去是擁有者的決定，程式不該替他挑一個。沒設定時那些
# 站台會繼續 403，而 `_http_get_json` 會印一行講清楚要設哪個鍵。
_UA_CONTACT_KEY = "api_contact"

# `_http_get_json` 的預設逾時。**取名字是為了讓別人讀得到**——`verify_external_apis`
# 在回報「沒有回應」時要把這個數字印出來，否則讀的人分不出「連不上」與「端點活著
# 但比這個上限慢」。這兩者的處置完全相反：前者查網路，後者要放寬**那一個呼叫**的
# 逾時。2026-09-08 就踩到後者（`/web dict` 的端點回 200 但要約 20 秒）。
# 放寬這個**預設值**不是解法：它套用在每一個外部呼叫上，等於讓所有真正掛掉的站台
# 都多吊使用者好幾秒。慢的端點要在自己的呼叫端指定。
_HTTP_TIMEOUT_SEC = 15.0


def _configured_contact() -> str:
    """`bot_config.json` 的 `api_contact`；沒設定／讀不到回空字串。永不 raise。"""
    try:
        return str(load_bot_config().get(_UA_CONTACT_KEY) or "").strip()
    except Exception:  # pylint: disable=broad-except
        return ""


def contact_configured() -> bool:
    """`api_contact` 有沒有設定。給呼叫端判斷「這個 403 是不是就是那件事」。

    公開包裝，不是第二個判定：實際的讀取仍然只在 `_configured_contact()`。
    存在的理由是 bot 那一側需要這個答案來決定錯誤訊息要不要多講一句，而伸手去拿
    別的模組的底線名稱只會讓下一個人以為那是公開介面。
    """
    return bool(_configured_contact())


def _user_agent() -> str:
    """這次請求要用的 UA。有設定聯絡方式就照站方要的格式帶上去。"""
    contact = _configured_contact()
    if not contact:
        return _BOT_UA
    return f"axiomatic-bot/1.0 ({contact})"


# 回應大小上限。**與 `discord_bot.GRID_MAX_IMAGE_BYTES` 同一條理由**：這條路徑的
# 位元組不是我們控制的內容，它們來自公開的第三方 API。`ClientTimeout(total=…)` 擋
# 不住這件事——它管的是傳輸時間，不是大小；一個持續穩定送資料的巨大回應會在逾時
# 之內就把行程的記憶體吃光。
#
# 2026-09-06 補。圖片那條路早就有上限（`GRID_MAX_IMAGE_BYTES`，連理由都寫在註解
# 裡），JSON 這條卻沒有——**同樣的來源、同樣的威脅，只有一半被擋住**。
#
# 8 MB 是很寬鬆的天花板：正常的 `posts.json?limit=30` 是幾百 KB，`tags.json` 更小。
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


# 一次向串流要多少位元組。64 KiB 只是「一次 await 拿得夠多」與「超標時不會多讀太
# 多」之間的折衷；確切數字不重要，**重要的是下面那支是個迴圈**。
_READ_CHUNK_BYTES = 64 * 1024


async def read_capped_body(content, cap: int) -> bytes | None:
    """把回應主體整個讀完；超過 `cap` 位元組就放棄，回 `None`。

    **為什麼不是 `await content.read(cap + 1)`。** `aiohttp.StreamReader.read(n)`
    的語意是 *read up to n*——它只等到緩衝區裡有東西，就把**目前緩衝的那一段**回
    傳，既不保證讀滿 n，也不保證讀到 EOF。對沒有 `Content-Length` 的 chunked 回應
    （公開圖庫 API 一律如此）這代表**合法回應被腰斬**。

    2026-09-07 實測（aiohttp 3.14.3，同一個端點、`limit=30`）::

        read(cap + 1)   ->  38043 bytes   (Content-Length: None)
        read() 到 EOF   ->  91898 bytes
        迴圈讀到 EOF    ->  91898 bytes

    截斷後的位元組 `json.loads` 會丟 `Unterminated string`，於是 `_http_get_json`
    回 `(status, None)`，每一個呼叫端都變成「找不到」。**症狀看起來像間歇性故障**
    ——截在哪裡取決於區塊邊界與網路時序：同一支探測腳本兩次跑出來的截斷位置就不
    一樣（38043 / 49507），而驗證腳本有時候還是綠的。首當其衝的是一次抓很多筆的
    那些（`limit=30` 的批次抓取、拼圖、tag 建議）。

    也**不可以**改成 `await r.read()`：那是無上限的，等於把大小上限整個拿掉。上限
    存在的理由見 `_MAX_RESPONSE_BYTES`——這些位元組來自第三方，不是我們控制的內容。

    回傳值有兩種，呼叫端必須用 `is None` 判斷、**不能用真假值**：

    * `bytes` ——完整的主體。可能是 `b""`（主體本來就是空的），那跟「超標」是兩
      回事，用真假值判斷會把它們混為一談；
    * `None`  ——超過上限，已丟棄。要印什麼 stderr 由呼叫端自己決定：兩個呼叫端
      的診斷訊息不一樣，而訊息內容受 Secrecy Layer 1 約束。

    超標就**立刻停止讀取**，不會為了知道「到底多大」而把整份拉完——那正是上限要
    避免的事。邊界與舊寫法一致：剛好等於 `cap` 收下，`cap + 1` 才算超標。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await content.read(_READ_CHUNK_BYTES)
        if not chunk:                 # EOF
            break
        total += len(chunk)
        if total > cap:
            # 已經確定超標：不再讀，也不留已讀的部分。
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _http_get_json(url: str, *, params=None, headers=None,
                        timeout: float = _HTTP_TIMEOUT_SEC,
                        quiet_statuses: tuple[int, ...] = ()):
    """GET → JSON。回 `(status, data)`；失敗回 `(status 或 -1, None)`。實作在 `_http_json`。"""
    return await _http_json("GET", url, params=params, headers=headers,
                            timeout=timeout, quiet_statuses=quiet_statuses)


async def _http_post_json(url: str, payload, *, headers=None,
                         timeout: float = _HTTP_TIMEOUT_SEC):
    """POST 一個 JSON 本體 → JSON。回傳形狀與 `_http_get_json` 相同。

    與 GET 共用同一個出口，所以 User-Agent、呼叫計數、回應大小上限與「非 200 留一行
    stderr」都一樣。2026-09-24 之前唯一的 POST 呼叫端（動畫查詢）自己開連線、用
    `r.json()` 把整個回應吃進來，是這道大小上限之外的另一個出口。"""
    return await _http_json("POST", url, json_body=payload, headers=headers,
                            timeout=timeout)


async def _http_json(method: str, url: str, *, params=None, json_body=None,
                     headers=None, timeout: float = _HTTP_TIMEOUT_SEC,
                     quiet_statuses: tuple[int, ...] = ()):
    """一次請求 → JSON。回 `(status, data)`；失敗回 `(status 或 -1, None)`。

    **本模組唯一的外送出口。** 三個理由，缺一不可：

    1. `User-Agent` 的預設值在這裡補（見 `_BOT_UA`）。少了它 Danbooru 直接 403。
       2026-08-30 之前有三個 fetcher 自己開 `ClientSession` 繞過這裡，於是也繞過
       了 UA——「另外開一條連線」與「少帶標頭」是同一個錯誤的兩面。
    2. `_METRICS_API_CALLS` 在這裡加一。繞過去的呼叫不會被算到，`/health` 的
       「api calls」就會少報。
    3. timeout 與例外處理只有一份。

    呼叫端自己給的 `headers` 優先，所以要覆寫 UA 仍然可以，只是必須明講。
    `test_external_apis.py` 會擋下任何在本模組另外開 `ClientSession` 的寫法。

    **非 200 一律留一行 stderr**，除非呼叫端用 `quiet_statuses` 明講那個碼是預期
    的（目前只有一個用途：Danbooru 匿名 `random=true` 的 2-tag 上限會回 422，而
    我們本來就準備好要退一步再打一次，那不是故障）。這條是事故的第二個成因——
    `/tags.json` 與「最新 N 筆」在非 200 時直接回空、一個字都不留，所以整條 fuzzy
    tag resolver 403 了都沒人知道。想安靜就要指名道姓，不能靠忘記。
    """
    global _METRICS_API_CALLS
    _METRICS_API_CALLS += 1
    label = f"http_{method.lower()}_json"
    # 呼叫端的標頭覆寫預設值，而不是反過來——這樣 e621／Safebooru 那種有自己
    # UA 規範的站台仍然可以指定，但「什麼都沒給」不會再變成「沒有 UA」。
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
                        # 帶上 params：查這種故障時「是哪個 tag 壞的」幾乎一定是
                        # 下一個問題。只進 stderr／log，不會到對話平台。
                        detail = repr(params)[:200] if params else ""
                        print(f"{label} {url} {detail}: HTTP {r.status}",
                              file=sys.stderr)
                        if r.status in (403, 429) and not _configured_contact():
                            # 講清楚要動哪個鍵。這種 403 光看狀態碼查不出來——
                            # 我們**有**帶 UA，站方要的是裡面有聯絡方式。
                            print(f"{label}: HTTP {r.status} 且 "
                                  f"`{_UA_CONTACT_KEY}` 未設定。有些站台"
                                  "（例如維基媒體）要求 User-Agent 裡帶得到人的"
                                  "聯絡方式（URL 或 email），只自報名稱不夠。"
                                  "在 bot_config.json 設 "
                                  f"`{_UA_CONTACT_KEY}` 即可。",
                                  file=sys.stderr)
                    return r.status, None
                declared = r.content_length
                if declared is not None and declared > _MAX_RESPONSE_BYTES:
                    print(f"{label} {url}: 回應宣稱 {declared} 位元組，"
                          f"超過 {_MAX_RESPONSE_BYTES} 上限，不讀",
                          file=sys.stderr)
                    return r.status, None
                # 讀到 EOF 為止。`read(n)` 是 read-up-to，單次呼叫會把 chunked
                # 回應截斷成合法但不完整的位元組——見 `read_capped_body`。
                raw = await read_capped_body(r.content, _MAX_RESPONSE_BYTES)
                if raw is None:
                    print(f"{label} {url}: 回應超過 "
                          f"{_MAX_RESPONSE_BYTES} 位元組上限，丟棄",
                          file=sys.stderr)
                    return r.status, None
                # 直接對位元組解析，順便把原本 `r.json()` ＋ `ContentTypeError`
                # 退回 `r.text()` 的兩條路併成一條：JSON 依 RFC 8259 就是 UTF-8，
                # 而站方常把 JSON 標成 text/plain。編碼明寫（CLAUDE.md 硬規則），
                # `errors="replace"` 是因為這些位元組不是我們控制的。
                return r.status, _json.loads(
                    raw.decode("utf-8", errors="replace"))
    except Exception as error:  # pylint: disable=broad-except
        print(f"{label} {url}: {error!r}", file=sys.stderr)
        return -1, None

def _dict_entries(data) -> list[dict]:
    """回應裡真正是物件的那幾筆；回應本身不是 list 就是空的。

    這些站在錯誤、限流或改結構時，會回一個夾著非物件的 list（或帶錯誤物件的 body）。
    呼叫端一律 `.get(...)`，讓非物件流出去只會在 handler 裡變成一個例外，整個指令
    只剩一句泛用的失敗。所有讀圖庫 list 的地方都走這一支，規則只寫一次。"""
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


async def _danbooru_posts(tags: str, *, limit: int,
                          random_order: bool = True) -> list[dict]:
    """抓一批 Danbooru post。所有 Danbooru post 查詢的**唯一**實作。

    匿名 Danbooru 對 `random=true`（同樣對 `order:random`）會套 2-tag 上限，所以
    多 tag 查詢若收到 422，就退回「最新 N 筆」讓呼叫端 client-side 隨機挑。略偏新
    但 UX 影響小，比直接回失敗好。

    為什麼是一份而不是三份：`_fetch_danbooru_post` /
    `_fetch_danbooru_posts_bulk` / `_fetch_danbooru_posts_random` 以前各自 inline
    一段幾乎一樣的 `_attempt`，各自開 `ClientSession`。結果三份**都**沒帶
    User-Agent、**都**沒被 `_METRICS_API_CALLS` 數到，而且其中兩份連非 200 都不吭
    一聲。合成一份之後，這兩件事只可能三個一起對或三個一起錯，不會再分岔。
    """
    base = {"tags": tags, "limit": limit}
    if random_order:
        # 422 是預期中的（2-tag 上限），所以第一次嘗試對它閉嘴；其他碼照吵。
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
    """從 `posts` 挑最多 `count` 張，優先挑最近沒送過的，並記進去重佇列。

    `_danbooru_recent` 只有 `DANBOORU_HISTORY_SIZE` 筆，所以池子裡全都送過時就
    整池重用（`fresh or posts`）——寧可重複也不要回空。
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

# Fuzzy tag resolver — Danbooru / e621 都有相容的 `/tags.json`（`name`、
# `post_count`、`search[name_matches]` glob），用它的 glob 比對遠比 Google
# 適合處理「使用者打 `lappland decadenza` 想找
# `lappland_the_decadenza_(arknights)`」這種接近正確的英文輸入。整個
# resolver 不需要 API key、不需要爬蟲。
_FUZZY_MAX_TOKENS = 8  # 上限避免長輸入連發太多 API 呼叫

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

# e621 的 API rules 同樣明文要求 descriptive UA。用同一個字串就好——站方要的是
# 「看得出是誰、出事找得到人」，不是每站一個獨立識別。
#
# 這裡原本寫著「Danbooru accepts aiohttp's default」。那句話曾經是對的，然後站方
# 上了 Cloudflare，它就變成錯的，而且沒有任何東西會告訴我們——沒有測試打真的端點，
# 失敗路徑又是靜默回 None。留著這行當紀念：註解裡的「某某站可以不用帶」是會過期的
# 假設，不是可以依賴的事實。現在改成**每個請求都帶**，就沒有這個過期問題。
_E621_UA = _BOT_UA

_DANBOORU_TAGS = _TagsAPI(url=DANBOORU_TAGS_API, headers=None)

_E621_TAGS = _TagsAPI(url=_E621_TAGS_API, headers={"User-Agent": _E621_UA})

async def _query_tags_json(api: _TagsAPI, *, name: str | None = None,
                           name_matches: str | None = None,
                           order: str | None = None,
                           limit: int = 5) -> list[dict]:
    """`/tags.json` 的薄包裝。回傳 list of tag dicts（每筆有 `name`、
    `post_count` 等）；任何錯誤回 []。`api` 描述要打哪個站。"""
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
    """Danbooru 搜尋修飾詞（`rating:general`、`score:>=5`、`order:rank` …）
    不是 tag 名，resolver 必須當 passthrough 不要 fuzzy 解析。任何含 `:` 的
    token 一律視為修飾詞。"""
    return ":" in tok

# 單 token 做 prefix fuzzy 解析的最低長度。`CP` / `bb` / `ru` 這種 ≤2 字
# 元的 token，`cp*` 配出來最熱門的是 `cpu_(hexivision)` 之類完全無關的
# 東西，幾乎必錯。Exact match 不受此限制——`bb` 本身就是有效的 Danbooru
# tag，使用者直接打 exact 仍會命中。
_FUZZY_MIN_TOKEN_LEN = 3

async def _best_tag_for_window(api: _TagsAPI, window: list[str]) -> str | None:
    """找出 post_count 最高、名稱依序包含 window 內各 token 的 tag。
    單 token 時先試 exact (`search[name]=`)，沒命中再試 prefix
    `tok*`（**不**用 `*tok*`，因為中間 substring 會撈到無關 tag —
    `*rossi*` 會把 `animal_crossing` 拉到首位）。多 token 才用
    `*tok1*tok2*…*`，因為多個 substring 配 + 順序限制已經夠精確。
    Exact / fuzzy 都要求 `post_count > 0`，站上有大量零張的 stale
    tag。沒命中回 None。

    過短的單 token（< `_FUZZY_MIN_TOKEN_LEN`）跳過 prefix fuzzy，避免
    `cp` → `cpu_(hexivision)` 這種錯誤解析。Exact match 仍會嘗試。"""
    if not window:
        return None
    # `name` 用 .get 取而不是 `[...]`：站方回的 tag 物件少了 `name` 欄時，
    # 原本的 `hits[0]["name"]` 會噴 KeyError 一路冒到 mention dispatcher 的
    # catch-all，把「找不到 tag」變成一則錯誤回覆。缺欄位就當沒命中。
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
    """貪婪最長配對：把使用者鬆散的查詢轉成 canonical tag。
    例（Danbooru）：`lappland decadenza yuri` →
    `lappland_the_decadenza_(arknights) yuri`。
    只有至少一個 token 被改寫才回傳新字串；都沒變化回 None（caller 就讓
    原本的 0-results 訊息出去）。`api` 決定打哪個站的 `/tags.json`。"""
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
        # 由最長 window 往最短 window 試；含修飾詞的 window 跳過。
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
    """拉一批 post 給 tag_suggest 聚合用：一次 ~30 張、**不去重**、回原始 list。"""
    return await _danbooru_posts(tags, limit=limit)

async def _fetch_danbooru_posts_latest(tags: str, limit: int = 4) -> list[dict]:
    """抓 `tags` 最新 N 張 post（不走 random、依預設順序就是 newest-first）。"""
    params = {"tags": tags, "limit": limit}
    status, data = await _http_get_json(DANBOORU_API, params=params)
    if status != 200:
        return []
    return _dict_entries(data)

async def _fetch_danbooru_post_latest(tags: str) -> dict | None:
    """抓 `tags` 的**最新一張**。沒有 dedup（user 明確指定要 latest 就給最新
    的、不該再 skip）。給 `@bot booru <tag> --latest` 用。"""
    posts = await _fetch_danbooru_posts_latest(tags, limit=1)
    return posts[0] if posts else None

async def _fetch_danbooru_posts_random(tags: str, count: int = 4) -> list[dict]:
    """隨機抓 `tags` 的 N 張 post。30-pool ＋ 422 fallback，再 client-side 挑。
    會跟 `_danbooru_recent` 去重；不足 count 時回多少給多少。
    給 `@bot booru <tag> --grid` 用。"""
    posts = await _danbooru_posts(tags, limit=DANBOORU_FETCH_LIMIT)
    return _take_unseen(posts, count)

# 各 booru 站對匿名 client 的 UA 規範：
# - Safebooru：urllib / aiohttp 預設 UA 在某些 PoP 會 401，給瀏覽器風格 UA 即可。
# - e621：要求 descriptive UA（站名 + 用途），不照規會被 ban；他們明文寫
#   在 API rules 裡。
# - IQDB：用 JS 反 bot 防護，要在 query string 加 `notabot=1` 才會回真結
#   果；否則回一段「You look like a bot」的提示頁。
# - Gelbooru：anonymous API 已被 Cloudflare 鎖（401），即使給瀏覽器 UA
#   也沒用。要支援需要 `&api_key=` + `&user_id=` 設定檔，目前未實作。
_BROWSER_UA = "Mozilla/5.0 (compatible; axiomatic-bot/1.0)"

_SAFEBOORU_API = "https://safebooru.org/index.php"

_E621_API = "https://e621.net/posts.json"

async def _fetch_safebooru_post(tags: str) -> dict | None:
    """從 Safebooru 抽一張隨機 post。用 `sort:random` 這個 meta tag 走
    Gelbooru-style dapi。"""
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
    """從 e621 抽一張隨機 post。用 `order:random` meta tag。"""
    full_tags = tags if "order:" in tags else f"{tags} order:random".strip()
    params = {"tags": full_tags, "limit": 30}
    status, data = await _http_get_json(
        _E621_API, params=params, headers={"User-Agent": _E621_UA},
    )
    if status != 200 or not isinstance(data, dict):
        return None
    # `posts` 在站方改結構 / 回錯誤物件時可能不是 list（例如 dict）——
    # `random.choice` 對 dict 會丟 KeyError，對其他型別丟 TypeError。
    posts = _dict_entries(data.get("posts"))
    return random.choice(posts) if posts else None
