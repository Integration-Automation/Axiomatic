#!/usr/bin/env python3
"""verify_external_apis.py — 獨立的「外部 API 還活著嗎」驗證腳本。

    py -3 axiomatic/verify_external_apis.py
    py -3 axiomatic/verify_external_apis.py --only danbooru wiki
    py -3 axiomatic/verify_external_apis.py --json

**為什麼需要這支（2026-08-30 的兩次事故）。** 這個 bot 打十幾個第三方端點，而每
一個進入點的失敗路徑都是「回 None／回空 list」，`mcmd_*` 再把它組成一句「找不
到」。所以站方一改規則，功能就整個消失，而使用者以為是自己輸入錯，log 裡也可能
一個字都沒有。同一天抓到兩個：

* 最大的圖庫站移到 CDN 防護後面，拒絕**沒有 User-Agent** 的請求（aiohttp 的預設就
  是沒有）→ 六個進入點全滅，包含圖片下載。
* 百科站要求 UA 裡帶得到人的**聯絡方式**（URL 或 email），只自報名稱一樣 403。

兩個都不是程式邏輯錯，是**外部契約漂移**——沒有任何靜態分析看得到，只有真的打一次
才知道。單元測試裡放了兩支實測（那兩個已知會漂的站台），但把十幾個端點全部塞進
`pytest` 不對：每個人每次跑測試都去敲第三方站台，慢、吵、而且正是會招來限流的行
為。所以照這個 repo 既有的慣例（`verify_browser.py` / `verify_quota_dialog.py`），
把完整掃描做成一支**手動驗證入口**。

**設計上的幾條硬性條件：**

* **走 bot 真正在用的那條路。** 每個 JSON 端點都透過
  `_external_apis._http_get_json` 發出，所以 User-Agent、`api_contact`、逾時、
  例外處理全部跟正式執行一模一樣。自己另外寫一份 HTTP 呼叫就驗不到今天這兩個
  bug——它們**就是**出在標頭上。
* **唯讀、無副作用。** 只發 GET（動畫資料庫是 GraphQL，只收 POST，所以送一個最小
  的唯讀查詢）。不寫任何檔案、不碰正式登入態、不需要憑證。
* **端點清單是這支的資料，不是註解。** `test_external_apis.py` 會比對這份清單與
  `discord_bot.py` 裡實際出現的網址，漏登記就紅——否則下次有人加一個新端點，這支
  掃描會安靜地漏掉它，而那正是我們要防的失敗模式。
* **診斷要能直接行動，而且不准亂猜。** 403 不只印狀態碼。但順序是「上游自己說了
  什麼」優先、猜測其次——2026-09-07 實測到動畫資料庫回 403 的原因寫在回應主體裡
  （上游自行停用服務），跟 UA 無關，而當時的診斷把它報成「User-Agent 問題」並要人
  去設 `api_contact`。**會誤報的驗證工具跟會狼來了的守門下場一樣：沒有人再看它。**

**結束碼三分**，跟 `verify_browser.py` 同一套語意：`0`＝選到的每一項都驗過而且正常；
`1`＝有站方真的拒絕（4xx/5xx 之類的 `FAIL`）；`3`＝沒有 FAIL，但有端點**沒驗到**
（`UNREACHABLE` 連不上，或 `SKIP` 缺前置條件）。離線時整支會是一片 `UNREACHABLE`，
那不算失敗（所以不是 1），但也**不得回報成功**：2026-09-20 以前這種情況是 exit 0，
只看結束碼的呼叫端（自走迴圈、排程）會把「一項都沒驗到」讀成「全部正常」。用 3 不用
2，是因為 argparse 打錯參數時自己用 2。

**`SKIP` 跟 `UNREACHABLE` 同一邊——這一格是同一天稍晚才補上的。** 三分剛做好時
`SKIP` 算 0，寫下的理由是「刻意跳過的不算沒驗到」；但這支裡根本沒有「刻意跳過」的
東西：唯二兩個 `SKIP` 都出自 CDN 那一筆拿不到樣本圖（上游 API 沒回，或回來的 post
沒有圖片網址），意思正是**沒驗到**。實測 `--only cdn`（`cdn` 自成一組，選它不會連帶
選到圖庫 API）在拿不到樣本時印的是「1 ok, 0 failed, 0 unreachable」、exit 0——一項都
沒驗到，而結束碼說全部正常，正是三分要消滅的那個形狀。`verify_browser.py` 的 `SKIP`
本來就是 exit 3；兩支既然宣稱同一套語意，就不該在這一格分岔。
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
    """每次執行都不一樣、不可能是真字的查詢字。見 `_ENDPOINTS` 字典那一筆的註解。"""
    return "zzverify" + secrets.token_hex(4)


_DICT_PROBE_WORD = _fresh_probe_word()

# --------------------------------------------------------------------------
# 端點清單
# --------------------------------------------------------------------------
# 每一筆：(群組, 說明, 方法, 網址, params, 額外標頭)
# 群組名給 `--only` 用。網址要跟 `discord_bot.py` / `_external_apis.py` 裡的字面值
# 對得起來——守門測試會比對，漏登記就紅。
_ENDPOINTS: list[dict] = [
    {"group": "danbooru", "what": "圖庫 posts（隨機圖／grid／latest 都走這條）",
     "url": ex.DANBOORU_API,
     "params": {"tags": "rossi_(arknights)", "limit": 1}},
    {"group": "danbooru", "what": "圖庫 tags（模糊 tag 解析器）",
     "url": ex.DANBOORU_TAGS_API, "params": {"search[name]": "yuri", "limit": 1}},
    {"group": "danbooru", "what": "圖庫 post 計數",
     "url": "https://danbooru.donmai.us/counts/posts.json",
     "params": {"tags": "yuri"}},
    {"group": "danbooru", "what": "圖庫 wiki 條目",
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
    {"group": "wiki", "what": "百科摘要（需要 UA 裡有聯絡方式）",
     "url": "https://en.wikipedia.org/api/rest_v1/page/summary/"
            "Python_(programming_language)", "params": None},
    # 字典這一筆**每次查一個不存在、每次都不同的字，健康的原站回 404**。2026-09-19 實測：
    # 原站連不上時，前面的 CDN 會對「查過的字」回幾十天前的過期快取（200，約 20 秒後），
    # 只有沒快取過的字才會露出 522。舊寫法固定查 `serendipity`，把逾時對齊 bot 之後就會
    # 在服務其實壞著的時候報 ok。每次換一個字，請求就一定得打到原站。
    # `timeout` 是 bot 那一側這個呼叫**自己**用的逾時（`bot_timeout` 是那個常數的名字），
    # 兩者由 `test_external_apis.test_a_per_call_timeout_in_the_bot_is_mirrored_here` 對帳。
    {"group": "dict", "what": "英文字典（查一個不存在的字，健康的原站回 404）",
     "url": "https://api.dictionaryapi.dev/api/v2/entries/en/" + _DICT_PROBE_WORD,
     "params": None, "ok_statuses": (404,),
     "timeout": 30.0, "bot_timeout": "DICT_TIMEOUT_SEC"},
    {"group": "xkcd", "what": "xkcd 最新一則",
     "url": "https://xkcd.com/info.0.json", "params": None},
    {"group": "quote", "what": "名言",
     "url": "https://zenquotes.io/api/random", "params": None},
    {"group": "fact", "what": "冷知識",
     "url": "https://uselessfacts.jsph.pl/random.json?language=en",
     "params": None},
    {"group": "joke", "what": "笑話",
     "url": "https://icanhazdadjoke.com/", "params": None,
     "headers": {"Accept": "application/json"}},
    {"group": "dog", "what": "狗圖",
     "url": "https://dog.ceo/api/breeds/image/random", "params": None},
    {"group": "crypto", "what": "幣種搜尋",
     "url": "https://api.coingecko.com/api/v3/search", "params": {"query": "btc"}},
    {"group": "crypto", "what": "幣價",
     "url": "https://api.coingecko.com/api/v3/simple/price",
     "params": {"ids": "bitcoin", "vs_currencies": "usd"}},
    {"group": "github", "what": "版本庫查詢",
     "url": "https://api.github.com/repos/python/cpython", "params": None},
    # 以下不是 JSON API，只驗「連得到、回得出東西」。
    #
    # `embed_only` 的三筆特別說明：bot **自己不抓**它們，只是把網址貼進訊息／embed，
    # 由對話平台自己去取圖。所以原始碼裡沒有任何抓取呼叫，靜態掃描也看不到它們——
    # 但站台掛掉的症狀對使用者是一樣的（一張破圖），而且更難查，因為連我們的 log
    # 都不會有一行。所以它們留在這份掃描裡，只是不參與「原始碼有抓才准列」的比對。
    {"group": "cat", "what": "貓圖（貼網址，平台自己取）", "raw": True,
     "embed_only": True,
     "url": "https://cataas.com/cat?ts=1", "params": None},
    {"group": "color", "what": "純色圖（embed，平台自己取）", "raw": True,
     "embed_only": True,
     "url": "https://singlecolorimage.com/get/ff0000/200x200", "params": None},
    {"group": "qr", "what": "QR 產生器（embed，平台自己取）", "raw": True,
     "embed_only": True,
     "url": "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=hi",
     "params": None},
    {"group": "iqdb", "what": "反查圖（HTML，需要瀏覽器風格 UA）", "raw": True,
     "url": "https://iqdb.org/", "params": None,
     "headers": {"User-Agent": ex._BROWSER_UA},
     "timeout": 20.0, "bot_timeout": "IQDB_TIMEOUT_SEC"},
    {"group": "anime", "what": "動畫資料庫（GraphQL，只收 POST）",
     "url": "https://graphql.anilist.co", "params": None, "method": "POST",
     "json_body": {"query": "query{Media(search:\"Frieren\",type:ANIME)"
                            "{id title{romaji}}}"},
     "timeout": 15.0, "bot_timeout": "ANIME_TIMEOUT_SEC"},
    {"group": "cdn", "what": "圖庫的圖片 CDN（`--grid` 下載走這條）",
     "raw": True, "cdn": True, "url": None, "params": None,
     "timeout": 30.0, "bot_timeout": "GRID_DOWNLOAD_TIMEOUT_SEC"},
]

_GROUPS = sorted({e["group"] for e in _ENDPOINTS})

# `_check_raw`／`_check_post` 在那一筆**沒有** `timeout` 時用的逾時。會用到它的只有
# `embed_only` 的三筆（貓圖、純色圖、QR）：那些網址是**對話平台自己去取**的，bot 這一側
# 根本沒有發出請求，所以沒有「bot 的逾時」可以照抄——這裡只是給掃描一個有限的上限。
# bot 自己會抓的非 JSON 端點一律帶 `timeout`／`bot_timeout`（由
# `test_external_apis.test_a_per_call_timeout_in_the_bot_is_mirrored_here` 要求），
# 所以這個值不會套到任何 bot 真的在打的端點上。
_EMBED_ONLY_TIMEOUT_SEC = 20.0

# `_error_body` 那一次「已經失敗之後再打一次、只為了撈上游說明」的逾時。它不決定任何
# 判定（判定在第一次請求就定了），只影響能不能多印一句原因，所以不跟 bot 對帳；
# 有名字是為了讓「驗證腳本裡沒有寫死的逾時數字」這條守門成立。
_ERROR_BODY_TIMEOUT_SEC = ex._HTTP_TIMEOUT_SEC


def _unreachable(timeout: float, error: Exception | None = None
                 ) -> tuple[str, str]:
    """「沒有回應」的統一說法。**一定要把逾時上限印出來。**

    「連不上」與「比我們的逾時慢」是兩個完全不同的問題，而原本 `_check_json` 那一句
    （「連不上（離線？逾時？）」）把讀的人指向網路故障。2026-09-08 實測 `dict` 這一
    筆就是後者：端點回的是 **HTTP 200，只是要花約 20 秒**，而預設逾時是 15 秒——於是
    它每次都失敗，而 log 說它「連不上」。把逾時值印出來，讓人可以直接拿去比。

    **三支檢查函式共用這一份，是因為原本只有一支學到這件事。** `_check_raw` 與
    `_check_post` 在 2026-09-20 以前回的是光禿禿一個 `TimeoutError`——同一個誤導，
    而它們那兩筆的逾時（iqdb 20 秒、CDN 30 秒）恰恰是最可能「活著但比上限慢」的。
    有例外物件時連名字一起報：連線根本沒成立（DNS／被擋）跟等不到回應要分得開。
    """
    if error is not None and not isinstance(error, TimeoutError):
        return "UNREACHABLE", (
            f"{type(error).__name__}（逾時上限 {timeout:g} 秒）——連線本身沒有成立，"
            "多半是離線／DNS／被擋，不是對方回得慢。")
    named = f"{type(error).__name__}：" if error is not None else ""
    return "UNREACHABLE", (
        f"{named}沒有回應（逾時上限 {timeout:g} 秒）。兩種成因要分開查："
        "**真的連不上**（離線／DNS／被擋），或是**端點活著但比這個上限慢**。"
        "分辨法：用瀏覽器或 curl 打同一個網址並計時——回得出 200 就是後者，"
        "那要修的是 bot 那一側該呼叫的逾時，不是網路。")


def _expected_status(status: int) -> tuple[str, str]:
    """那一筆自己宣告「健康時就是回這個碼」的非 200。三支共用同一句。"""
    return "OK", f"HTTP {status}（這個端點健康時就是這樣回）"


async def _check_json(entry: dict) -> tuple[str, str]:
    """走 bot 真正在用的那條路。回 (verdict, detail)。

    **逾時一律等於 bot 那一側同一個呼叫的逾時，不准比它寬。** 這支的價值全在「跟
    bot 用同一條路、同一個逾時」——只要在這裡把逾時放寬，這支就會在 bot 明明壞著的
    時候報 OK，那比沒有這支還糟。端點慢到超過 `_http_get_json` 的預設，正確的處理是
    **去修 bot 那一側的呼叫**（給那個呼叫一個夠長的逾時），然後在這裡照抄那個值。
    2026-09-08 bot 的字典呼叫放寬到 `DICT_TIMEOUT_SEC` 之後，這支一直用 15 秒，直到
    2026-09-19 才對齊——同一個形狀，反方向：比 bot **窄**的逾時會在 bot 好好的時候報
    連不上。所以現在兩邊由測試對帳，而不是靠人記得。

    `ok_statuses` 是「這個端點健康時本來就會回的非 200」（字典查不存在的字回 404）。
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
        return "FAIL", "HTTP 200 但回應不是 JSON"
    size = len(data) if isinstance(data, (list, dict)) else "?"
    return "OK", f"HTTP 200, {type(data).__name__}[{size}]"


async def _check_raw(entry: dict) -> tuple[str, str]:
    """非 JSON 的端點：只確認連得到、狀態碼正常、回得出內容型別。"""
    import aiohttp

    url = entry["url"]
    if entry.get("cdn"):
        # CDN 沒有固定網址可以打——先跟 API 要一張現有的圖再抓它。這樣驗到的才是
        # 真正的下載路徑（`--grid` 就是這樣做的）。
        post = await ex._fetch_danbooru_post("rating:general")
        if not post:
            return "SKIP", "拿不到樣本 post（上游 API 已經是 FAIL 了）"
        url = (post.get("large_file_url") or post.get("file_url")
               or post.get("preview_file_url"))
        if not url:
            return "SKIP", "樣本 post 沒有圖片網址"
    headers = dict(entry.get("headers") or {})
    headers.setdefault("User-Agent", ex._user_agent())
    # 逾時照抄 bot 那一側（`timeout`／`bot_timeout` 對帳過）；沒有的只有 embed_only。
    timeout = entry.get("timeout", _EMBED_ONLY_TIMEOUT_SEC)
    # `ok_statuses` 三支都認得。它原本只有 `_check_json` 看——一個**宣告了但沒有人
    # 讀**的鍵沒有任何症狀（跟 `_OWNER_ONLY_SLASH` 那個過期字串同一個形狀），而它
    # 失敗的方向是狼來了：那一筆會永遠報 FAIL。
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
    # 同 `_check_raw`：逾時照抄 bot 那一側。2026-09-19 前這裡寫死 20 秒，而 bot 的
    # 那次 POST 是 15 秒——驗證腳本比 bot 寬，會在 bot 逾時的時候報 ok。
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
                    return "FAIL", "HTTP 200 但回應形狀不對"
                return "OK", "HTTP 200, data"
    except Exception as error:  # pylint: disable=broad-except
        return _unreachable(timeout, error)


# 診斷用的主體讀取上限。失敗頁通常只有幾百位元組，但主體是第三方送來的，不設限就
# 等於「錯誤處理路徑上有一個沒有上限的讀取」。
_DIAG_BODY_CAP = 64 * 1024


async def _read_body_text(resp) -> str:
    """把失敗回應的主體讀成文字。讀不到就回空字串——診斷不該讓驗證本身炸掉。

    走 `read_capped_body` 而不是 `resp.text()`：後者無上限，而且 `read(n)` 單次呼
    叫會截斷 chunked 回應（那正是 2026-09-07 修掉的缺陷）。編碼明寫、`errors=
    "replace"`——這些位元組不是我們控制的內容（CLAUDE.md 硬規則）。
    """
    try:
        raw = await ex.read_capped_body(resp.content, _DIAG_BODY_CAP)
    except Exception:  # pylint: disable=broad-except
        return ""
    if raw is None:
        return ""
    return raw.decode("utf-8", errors="replace")


async def _error_body(url, *, params=None, headers=None) -> str:
    """失敗之後再打一次，只為了把上游的說明撈出來。唯讀，且只在已經失敗時發生。

    `_http_get_json` 只回 `(status, data)`，拿不到主體——而 4xx 的原因十之八九就
    寫在主體裡。多打這一次的代價只有在**已經壞掉**的時候才付得出去，換到的是不用
    再靠猜。
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
    """從回應主體裡撈出**上游自己寫的**那句說明；撈不到回空字串。

    只讀 JSON。HTML 的失敗頁（CDN 的挑戰頁那種）撈不出東西，那時候退回下面的猜測
    才是對的——挑戰頁確實就是 UA／防護的問題。

    主體是第三方位元組，所以一律壓成單行並截短，不要讓一整頁噴進主控台。
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
    """把狀態碼翻成「接下來該做什麼」。403 光看碼是查不出原因的。

    **兩種 403 要分開，否則這支工具會把人帶去錯的方向。** 2026-09-07 的反例：動畫
    資料庫那一筆回 403，而原因就寫在回應主體裡——上游自己把 API 停用了
    （"temporarily disabled due to severe stability issues"）。跟 UA 一點關係都沒
    有：實測無 UA、瀏覽器 UA、我們的 UA，三者都是 403。可是當時這支函式無條件套
    「這一類幾乎都是 User-Agent 的問題」＋「`api_contact` 沒設定」，於是一個「上游
    停用中」被報成「你的標頭有問題」，而那個方向再怎麼查都查不出來。

    所以順序是：

    1. **上游自己說了什麼**優先——那是事實，不是猜測；
    2. 猜測只留給「走共用 UA 的那條 GET 路徑」。非 GET 的端點標頭本來就跟
       `_http_get_json` 不同（GraphQL 那一筆只收 POST），對它套 UA 那套說法沒有
       任何根據。
    """
    if not (500 <= status <= 599 or status in (401, 403, 429)):
        return ""
    # 「上游自己說了什麼」優先，**5xx 也不例外——這一格 2026-09-20 才補**。原本 5xx
    # 直接回下面那句猜測，於是一個帶著 JSON 說明的 503（「維護到某日」那種）會被這支
    # 工具改寫成「52x 通常是前面的 CDN 連不到原站」，正好違反上面立的規矩：猜測蓋掉
    # 事實。同一個形狀在 403 上已經害過一次（2026-09-07 動畫資料庫）。
    upstream = _upstream_message(body)
    if upstream:
        return f"  ← 上游自己說：{upstream}"
    if 500 <= status <= 599:
        # 2026-09-19 字典那一筆的 522：CDN 連不到原站。這一類跟我們的請求無關，
        # 別讓人去查標頭或 `api_contact`——那兩個方向在這裡查不出任何東西。
        return ("  ← 上游那一側出錯（52x 通常是前面的 CDN 連不到原站），不是我們的"
                "請求有問題；等對方恢復，或考慮換來源")
    if method != "GET":
        return ("  ← 非 GET 端點，標頭跟共用的 GET 路徑不同；先看回應主體怎麼說，"
                "不要預設是 User-Agent")
    hints = ["這一類幾乎都是 User-Agent 的問題，不是網址錯"]
    if not ex._configured_contact():
        hints.append(f"`{ex._UA_CONTACT_KEY}` 目前沒設定——有站台（例如百科站）"
                     "要求 UA 裡帶得到人的聯絡方式，只自報名稱不夠")
    hints.append("不要改成假裝瀏覽器：實測過，那樣一樣被擋")
    return "  ← " + "；".join(hints)


# 每一種判定在主控台上的樣子。抬成模組常數是為了**能被對帳**：判定字串散在三支檢查
# 函式的 `return` 裡，多一種而忘了登記，`_run` 會在掃到一半時 KeyError；而更安靜的一
# 半是 `_exit_code` ——沒被列進 `_UNVERIFIED_VERDICTS` 的新判定會自動算成「驗過而且
# 正常」。兩邊都由 `test_external_apis` 從原始碼推出的判定集合反查。
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
EXIT_UNVERIFIED = 3     # 見模組 docstring：沒有 FAIL，但有端點沒驗到

# 「沒驗到」的判定。見模組 docstring：`SKIP` 跟 `UNREACHABLE` 同一邊，因為這支裡
# 沒有任何「刻意跳過」的端點——`SKIP` 的兩個來源都是「前置條件拿不到，所以沒驗」。
_UNVERIFIED_VERDICTS = frozenset({"UNREACHABLE", "SKIP"})


def _exit_code(results: list[dict]) -> int:
    """三分結論。純函式：FAIL 優先，其次「有沒驗到的」，都沒有才是 0。"""
    verdicts = {r["verdict"] for r in results}
    if "FAIL" in verdicts:
        return EXIT_FAIL
    if verdicts & _UNVERIFIED_VERDICTS:
        return EXIT_UNVERIFIED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    global _QUIET
    parser = argparse.ArgumentParser(
        description="驗證 bot 用到的每一個外部 API 還活著。唯讀、無副作用。")
    parser.add_argument("--only", nargs="+", metavar="GROUP",
                        choices=_GROUPS,
                        help=f"只驗這幾組（可選：{' '.join(_GROUPS)}）")
    parser.add_argument("--json", action="store_true",
                        help="輸出 JSON，給程式讀")
    # `argv or []`：`None` 一律當成沒有旗標，不去讀 `sys.argv`（在 pytest 底下那是
    # pytest 自己的命令列）。約定與守門見 `test_suite_safety.py`。
    args = parser.parse_args(argv or [])
    _QUIET = args.json

    if not _QUIET:
        print(f"User-Agent: {ex._user_agent()}")
        if not ex._configured_contact():
            print(f"（`{ex._UA_CONTACT_KEY}` 未設定——需要聯絡方式的站台會 403）")
        print()

    results = asyncio.run(_run(args.only))
    failed = [r for r in results if r["verdict"] == "FAIL"]
    unreachable = [r for r in results if r["verdict"] == "UNREACHABLE"]
    skipped = [r for r in results if r["verdict"] == "SKIP"]
    # **「ok」是數出來的，不是減出來的。** 原本寫的是「總數 − failed −
    # unreachable」，於是 `SKIP` 被算進 ok：`--only cdn` 拿不到樣本圖時印的是
    # 「1 ok, 0 failed, 0 unreachable」。減法把每一種沒列到的判定都默默歸成好的，
    # 而那正是這幾個數字唯一該講清楚的事。
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
            print("（全部連不上通常代表沒有網路，不是站方拒絕）")
        for r in failed:
            print(f"  FAIL  {r['group']:<10} {r['what']} — {r['detail']}")
        if code == EXIT_UNVERIFIED:
            print(f"exit {EXIT_UNVERIFIED}：有端點沒驗到（連不上，或缺前置條件而"
                  "跳過）——不是失敗，但也不能當成全部正常。晚點再跑一次。")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
