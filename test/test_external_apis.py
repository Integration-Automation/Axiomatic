"""`_external_apis.py` 的守門測試。

這個模組原本一支測試都沒有——366 行、六個對外站台的進入點、全部只在真的被使用者
呼叫時才會執行，而失敗路徑一律是「回 None／回空 list」。於是 2026-08-30 發現時，
**所有 Danbooru 功能已經整個壞掉**（HTTP 403），而 repo 裡沒有任何東西會紅。

壞掉的原因是 Danbooru 整站移到 Cloudflare 後面，開始拒絕沒有 `User-Agent` 的請求，
而本專案的 Danbooru 呼叫剛好都沒帶——模組裡甚至有一行註解寫著「Danbooru accepts
aiohttp's default」，那句話曾經是對的。

所以這支檔案分成兩層：

* **結構層**（不碰網路）：外送出口只能有一個、每個請求都一定帶 UA、UA 不得假裝成
  瀏覽器。這一層擋的是「下次有人再繞過去」。
* **實測層**（要網路，連不上就 skip）：真的打一次端點，確認我們現在的 UA 沒有被擋。
  這一層擋的是「站方哪天又改規則」——那是靜態分析永遠看不到的。

實測層刻意**只在被擋時紅、連不上時 skip**：沒網路的環境不該有紅字，但「連得上而且
被拒絕」是真的壞了，必須吵。
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
# 假的 aiohttp：記下每一個請求真正送出的標頭
# ---------------------------------------------------------------------------

class _FakeBody:
    """`r.content` 的替身：一個**會記位置**的最小串流。

    刻意回**位元組**而不是已解析的物件：正式程式碼從 2026-09-06 起是自己
    `json.loads` 位元組（為了套用大小上限），所以替身如果還停在「`json()` 直接回
    Python 物件」，就會測不到解析與上限那一段——那正是這個替身存在的意義。

    **`self._pos` 是承重的，別把它當成整理。** 2026-09-07 之前這個類別沒有位置，
    `read(n)` 每次都回 `self._raw[:n]`——也就是同一段位元組的無限自動販賣機，永遠
    不會回 `b""`（EOF）。它之所以能長期是對的，只因為當時正式程式碼**剛好只呼叫
    一次** `read()`；替身的正確性其實是被受測程式的一個實作細節撐著的。等到那行
    改成迴圈讀到 EOF（它本來就該是），整組上限測試會集體變紅，而**受測程式是對
    的、壞的是替身**——這種紅字最容易被誤讀成「新寫法有問題」而把修好的東西改回去。

    一般化的判準：**替身要照著被模仿的那個東西的契約寫，不要照著目前呼叫端剛好會
    怎麼用來寫。** 沒有 EOF 的串流不是串流。
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
        # `raw` 讓測試直接指定位元組（測上限、測壞掉的 JSON）；沒給就把 payload
        # 序列化成正常的 JSON，跟真的回應一樣。
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
    """記錄用的 `ClientSession` 替身。`calls` 是類別層的，方便測試取用。"""

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
    """把 `_external_apis` 用的 `aiohttp.ClientSession` 換成記錄器。

    換的是 `ex.aiohttp.ClientSession`，也就是模組真正會呼叫到的那一個——如果哪天
    有人在本模組裡改用別的 HTTP 函式庫，這裡會整批壞掉，那正是我們要知道的事。
    """
    _FakeSession.calls = []
    _FakeSession.replies = []
    monkeypatch.setattr(ex.aiohttp, "ClientSession", _FakeSession)
    return _FakeSession


def _run(coro):
    return asyncio.run(coro)


# 每個對外抓取函式 → 一個「怎麼呼叫它」的 thunk。新增 fetcher 就加一行，
# 下面所有的標頭／計數守門會自動涵蓋到它。
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
# 結構層：外送出口只能有一個
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 回應大小上限
#
# 圖片下載那條路早就有上限（`discord_bot.GRID_MAX_IMAGE_BYTES`），連理由都寫在註解
# 裡：「這條路徑的位元組不是我們控制的內容」。JSON 這條走的是**同樣的來源、同樣的
# 威脅**，卻一直沒有上限——`await r.json()` 會把整個回應吃進記憶體。
# `ClientTimeout(total=…)` 擋不住：它管的是傳輸時間，不是大小，一個持續穩定送資料
# 的巨大回應會在逾時之內就把行程的記憶體吃光。2026-09-06 補上。
# ---------------------------------------------------------------------------

class _SizedSession(_FakeSession):
    """可以指定回應位元組與 `content_length` 的 session 替身。"""

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


def test_a_normal_response_still_parses(sized_http):
    sized_http.raw = json.dumps([{"id": 1}]).encode("utf-8")
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert (status, data) == (200, [{"id": 1}])


def test_json_served_as_plain_text_still_parses(sized_http):
    """原本靠 `ContentTypeError` → `r.text()` → `loads` 那條退路處理。

    改成直接對位元組 `loads` 之後那條退路併掉了，但**行為必須一樣**——站方把 JSON
    標成 `text/plain` 是常見的事，退化成解析失敗會讓整個功能安靜地回空。
    """
    sized_http.raw = b'{"ok": true}'
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert (status, data) == (200, {"ok": True})


def test_an_oversized_response_is_dropped(sized_http):
    """超過上限就整份丟掉，不解析。

    斷言的是 `data is None`——不是「有沒有例外」。上限失效的症狀不是崩潰，
    是記憶體被吃光，那在測試裡看不出來，所以只能從「有沒有拒收」這一側驗。
    """
    sized_http.raw = b"[" + b"0," * (ex._MAX_RESPONSE_BYTES // 2) + b"0]"
    assert len(sized_http.raw) > ex._MAX_RESPONSE_BYTES
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert status == 200
    assert data is None, "超過上限的回應仍然被解析了"


def test_a_declared_oversize_is_refused_before_reading(sized_http):
    """`Content-Length` 就已經超標時，連讀都不要讀。

    這一道是省下白讀好幾 MB；真正的防線是下面那個實際讀取的上限（標頭可以說謊）。
    """
    sized_http.declared = ex._MAX_RESPONSE_BYTES + 1
    sized_http.raw = b"[1]"          # 內容其實很小，但宣稱很大
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert (status, data) == (200, None)


def test_exactly_at_the_cap_is_still_accepted(sized_http):
    """邊界：剛好等於上限要收，不是拒收。

    `read(cap + 1)` 那個 +1 就是為了分辨「剛好」與「超過」；少了它，剛好到上限的
    回應會被誤判成超標而丟掉。
    """
    payload = b"[" + b"1," * ((ex._MAX_RESPONSE_BYTES - 3) // 2) + b"1]"
    payload += b" " * (ex._MAX_RESPONSE_BYTES - len(payload))
    assert len(payload) == ex._MAX_RESPONSE_BYTES
    sized_http.raw = payload
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert status == 200
    assert isinstance(data, list), "剛好等於上限的回應被誤判成超標"


def test_one_bad_byte_does_not_throw_away_the_whole_response(sized_http):
    """回應位元組不是我們控制的：一個壞位元組不該讓整份資料報廢。

    這一支要驗的是 `errors="replace"` 真的套在這條路上，而**驗法有陷阱**：拿一段
    「既不是合法 UTF-8、也不是合法 JSON」的位元組是驗不出來的——嚴格解碼會丟
    `UnicodeDecodeError`、容錯解碼會丟 `JSONDecodeError`，兩者都被外層的
    `except Exception` 接住、都回 `(-1, None)`，**觀察不到差別**。（變異測試當場
    抓到這一點：改成嚴格解碼時，原本的寫法照樣是綠的。）

    所以這裡用的是「JSON 結構完好、只有字串值裡夾了一個壞位元組」——容錯解碼會把
    它換成 U+FFFD 然後正常解析出資料，嚴格解碼則會整份丟掉。這也正是真實情況：
    站方回的標籤名稱夾了一個編碼壞掉的字元，不該讓整個搜尋回空。
    """
    # \xe9 是 latin-1 的 é，在 UTF-8 裡是非法的孤立位元組。
    sized_http.raw = b'{"name": "caf\xe9", "id": 7}'
    status, data = _run(ex._http_get_json("https://example.invalid/x"))
    assert status == 200
    assert data is not None, (
        "一個壞位元組讓整份回應被丟掉了——解碼應該用 errors='replace'")
    assert data["id"] == 7
    assert data["name"].startswith("caf")


def test_the_cap_is_a_real_positive_number():
    assert isinstance(ex._MAX_RESPONSE_BYTES, int)
    assert ex._MAX_RESPONSE_BYTES > 0


# ---------------------------------------------------------------------------
# 分塊送達（chunked）：`read(n)` 是 read-up-to，不是「讀滿 n」
#
# 2026-09-07 實測到的活躍缺陷。上面那些上限測試**全部是綠的**，因為 `_FakeBody`
# 一次就把整份主體交出來——於是 `read(n)` 在測試裡永遠讀得完，而真實世界不是這樣。
# 這一段的替身刻意分多次交付，那才是 `aiohttp.StreamReader` 真正的行為。
# ---------------------------------------------------------------------------

class _ChunkedBody:
    """`r.content` 的替身，**分多次**把主體交出來。

    這是 `aiohttp.StreamReader.read(n)` 真正的語意：*read up to n*——回傳不超過 n
    個位元組，但**也可能少於 n**，即使後面還有資料。`_FakeBody` 一次全給，所以它
    永遠測不到這件事；那正是截斷缺陷躲過整套測試的原因。
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
        self._chunks[0] = head[n:]           # 只給前 n 個，剩下的下次再拿
        return head[:n]


class _ChunkedSession(_FakeSession):
    """回應主體分塊送達的 session 替身。"""

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
    """切成「第一塊 + 其餘」，模擬真實的分塊送達。"""
    return [raw[:first], raw[first:]] if len(raw) > first else [raw]


def test_a_chunked_response_is_read_to_the_end(chunked_http):
    """分塊送達的合法 JSON 要能完整解析。

    **修好之前這一支是紅的**，而且紅的理由就是使用者實測到的那一個：舊寫法是單次
    `await r.content.read(cap + 1)`，`read(n)` 只回「目前緩衝的那一段」，所以它只
    拿到第一塊 100 個位元組，`json.loads` 丟 `Unterminated string`，被外層的
    `except Exception` 接住變成 `(-1, None)`——呼叫端看到的是「找不到」。
    """
    payload = [{"id": i, "tag": "rossi_(arknights)"} for i in range(30)]
    raw = json.dumps(payload).encode("utf-8")
    assert len(raw) > 100, "測資要大到跨越至少兩塊，否則什麼都驗不到"
    chunked_http.chunks = _split(raw)

    status, data = _run(ex._http_get_json("https://example.invalid/x"))

    assert data is not None, (
        "分塊送達的回應被截斷了——`read(n)` 是 read-up-to，必須迴圈讀到 EOF")
    assert status == 200
    assert len(data) == 30, f"只讀到 {len(data)} 筆，主體被腰斬了"


def test_a_chunked_response_that_exceeds_the_cap_is_still_dropped(chunked_http):
    """上限不得因為改成迴圈而失效。

    `status == 200` 那一半是重點：光看 `data is None` 分不出是「上限擋下的」還是
    「解析炸了被外層 except 接住」——後者回的是 `-1`。少了這一半，把上限整段刪掉
    也能讓這支測試維持綠色，那就變成兩道防護互相遮蔽。
    """
    over = b"[" + b"0," * (ex._MAX_RESPONSE_BYTES // 2) + b"0]"
    assert len(over) > ex._MAX_RESPONSE_BYTES
    chunked_http.chunks = _split(over)

    status, data = _run(ex._http_get_json("https://example.invalid/x"))

    assert status == 200, "應該是上限擋下的（回 r.status），不是解析失敗（回 -1）"
    assert data is None, "超過上限的回應仍然被解析了"


# ---------------------------------------------------------------------------
# 共用的讀取原語本身
#
# 圖片下載（`discord_bot._send_danbooru_grid`）走的是同一支，所以在這裡驗過就等於
# 兩側都驗過——前提是「兩側真的都走這一支」，那由下面的 AST 守門盯著。
# ---------------------------------------------------------------------------

def _read(chunks, cap):
    return _run(ex.read_capped_body(_ChunkedBody(chunks), cap))


def test_the_shared_reader_returns_the_whole_body():
    """分塊送達的完整位元組要拿得到——圖片那一側要的就是這個。

    圖片被截斷的症狀跟 JSON 不一樣但一樣安靜：Pillow 開不起來，那一格就從拼圖裡
    消失，使用者只看到「圖少了幾張」。
    """
    body = bytes(range(256)) * 400              # 102400 bytes，跨好幾塊
    chunks = [body[i:i + 1000] for i in range(0, len(body), 1000)]
    assert len(chunks) > 1
    assert _read(chunks, 20 * 1024 * 1024) == body


def test_the_shared_reader_drops_an_oversized_body():
    assert _read([b"x" * 50, b"y" * 51], 100) is None


def test_the_shared_reader_accepts_exactly_the_cap():
    """邊界：剛好等於上限要收下，不是拒收。

    舊寫法用 `read(cap + 1)` 的那個 +1 就是為了分辨「剛好」與「超過」；改成迴圈
    之後這個界線由 `total > cap` 維持，語意不變。
    """
    assert _read([b"x" * 60, b"y" * 40], 100) == b"x" * 60 + b"y" * 40


def test_an_empty_body_is_not_confused_with_an_oversized_one():
    """空主體回 `b""`，超標回 `None`——呼叫端必須用 `is None` 分辨。

    寫成 `if not raw:` 會把「站方回了空的 200」誤判成「超過 8 MB 上限」，然後印一
    行完全誤導的 stderr。
    """
    assert _read([], 100) == b""
    assert _read([b""], 100) == b""


def test_the_shared_reader_stops_reading_once_over_the_cap():
    """超標就別再讀了。

    上限的目的是不要把巨大的第三方回應吃進記憶體；如果為了算出「到底多大」而把整
    份拉完，那上限就只剩下「不解析」的效果，記憶體照樣被吃掉。
    """
    body = _ChunkedBody([b"z" * 10] * 100)
    assert _run(ex.read_capped_body(body, 25)) is None
    assert body.reads < 10, (
        f"超標之後還在讀（讀了 {body.reads} 次）——上限應該一確定超過就停手")


def test_no_response_body_is_read_with_a_single_capped_read():
    """`<回應>.content.read(...)` 不准再直接出現在任何呼叫端。

    這是這次缺陷的**形狀**，不是某一行的筆誤：`read(cap + 1)` 看起來完全合理，
    而且在「一次就把整份給出來」的測試替身底下永遠是對的。同一個形狀當時同時存在
    於兩個檔案（JSON 一處、圖片一處），也就是說 code review 沒擋下來過。

    無上限的 `.content.read()`（讀到 EOF）一樣擋——那是另一個方向的錯：把大小上限
    整個拿掉。兩者都應該改用 `read_capped_body`。
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
        f"這些地方直接從回應主體讀：{offenders}。`StreamReader.read(n)` 是 "
        "read-up-to，單次呼叫會把 chunked 回應截斷成合法但不完整的位元組"
        "（2026-09-07 實測：38043 / 91898 bytes），而症狀是功能安靜地回不出東西。"
        "改用 `_external_apis.read_capped_body(r.content, <上限>)`。")


def test_both_response_readers_go_through_the_shared_helper():
    """反過來盯：兩個呼叫端**都**還在用共用的那一支。

    只驗上面那條「不准直接 read」是不夠的——把整段讀取刪掉、或改成別的自己手寫
    一份迴圈，那條照樣是綠的。一個豁免（或一次重構）必須有人證明它還在做原本那
    件事，否則它會活得比它的理由久。
    """
    wanted = {
        "_external_apis.py": "_http_get_json",
        "discord_bot.py": "_send_danbooru_grid",
    }
    for path in (_MODULE, _BOT_SOURCE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        target = wanted[path.name]
        found = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == target]
        assert found, f"{path.name} 裡找不到 {target}（改名了？）"
        names = {c.func.id for c in ast.walk(found[0])
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "read_capped_body" in names, (
            f"{path.name}:{target} 沒有走 `read_capped_body`。兩個呼叫端讀的都是"
            "第三方送來的 chunked 位元組，共用那一支才是單一事實來源——"
            "2026-09-07 之前同一個截斷缺陷就是同時存在於兩邊。")


def test_only_http_get_json_opens_a_session():
    """本模組裡**只有** `_http_get_json` 可以開 `aiohttp.ClientSession`。

    這是這次事故的結構性成因，不是風格問題。原本三個 Danbooru fetcher 各自
    inline 一份 `async with aiohttp.ClientSession(...)`，於是三份**都**繞過了
    `_http_get_json` 補上的 User-Agent、也**都**繞過了 `_METRICS_API_CALLS`。
    「另外開一條連線」與「少帶標頭、少算一次」在這個模組裡是同一個錯誤的兩面，
    所以直接擋住前者。
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
            if name == "ClientSession" and node.name != "_http_get_json":
                offenders.append(f"{node.name}:{inner.lineno}")
    assert not offenders, (
        f"這些函式自己開了 ClientSession：{offenders}。本模組的外送出口只能是 "
        "`_http_get_json`——繞過去就等於繞過 User-Agent 與 API 計數，而那正是 "
        "2026-08-30 那次 Danbooru 全站 403 沒被任何人發現的原因。")


def test_every_outbound_request_carries_a_user_agent(fake_http):
    """每一個 fetcher 送出的請求都要有非空的 `User-Agent`。

    行為層的複驗：就算有人繞過上一支測試的 AST 掃描（換個寫法開 session），
    只要請求少了 UA，這裡就會紅。
    """
    for label, thunk in _FETCHERS.items():
        fake_http.calls = []
        _run(thunk())
        assert fake_http.calls, f"{label} 一個請求都沒送出"
        for call in fake_http.calls:
            ua = call["headers"].get("User-Agent")
            assert ua, f"{label} 送出的請求沒有 User-Agent：{call['headers']}"


def test_the_user_agent_does_not_pretend_to_be_a_browser():
    """UA 必須是「說明自己是誰」，不得假裝成瀏覽器。

    這不只是站規（Danbooru 的 Help:Api 明文寫「Don't impersonate browsers or use
    the default header of your library」），實務上也更糟：2026-08-30 三種都實測過，
    沒有 UA 是 403，**完整的 Chrome 140 UA 也是 403**，只有說明式的 UA 拿到 200。
    挑戰頁預期真瀏覽器會去解 JS，我們解不了，於是被判定為冒充。

    `_BROWSER_UA` 不在此限：那是給 Safebooru／IQDB 用的，而且它用的是
    `Mozilla/5.0 (compatible; <自己的名字>)` 這種「相容格式但仍然自報身分」的寫法，
    不是冒充某個真實瀏覽器版本。
    """
    assert not ex._BOT_UA.lower().startswith("mozilla"), (
        f"_BOT_UA={ex._BOT_UA!r} 長得像瀏覽器。實測過：假裝成瀏覽器一樣被 403。")
    assert "axiomatic" in ex._BOT_UA, (
        f"_BOT_UA={ex._BOT_UA!r} 沒有自報身分；站方要的是出事時找得到人。")
    assert "compatible;" in ex._BROWSER_UA and "axiomatic" in ex._BROWSER_UA, (
        f"_BROWSER_UA={ex._BROWSER_UA!r} 應該維持「相容格式但仍自報身分」的寫法；"
        "改成冒充某個真實瀏覽器版本會同時違反站規並且更容易被擋。")


def test_caller_headers_win_over_the_default(fake_http):
    """呼叫端明講的標頭覆寫預設值，而不是反過來。

    e621 有自己的 UA 規範，`_query_tags_json(_E621_TAGS, ...)` 會帶 `_E621_UA`；
    如果預設值反過來蓋掉呼叫端，那個規範就永遠套不上。
    """
    fake_http.calls = []
    _run(ex._http_get_json("https://example.invalid/x",
                           headers={"User-Agent": "custom-ua/9"}))
    assert fake_http.calls[0]["headers"]["User-Agent"] == "custom-ua/9"

    fake_http.calls = []
    _run(ex._http_get_json("https://example.invalid/x",
                           headers={"Accept": "application/json"}))
    sent = fake_http.calls[0]["headers"]
    assert sent["Accept"] == "application/json", "呼叫端的其他標頭要留著"
    assert sent["User-Agent"] == ex._BOT_UA, "沒指定 UA 時要補上預設值"


def test_every_fetcher_is_counted(fake_http):
    """每一個 fetcher 都要被 `_METRICS_API_CALLS` 數到。

    `/health` 的「api calls」與 `!metrics` 讀的就是它。2026-08-30 之前三個
    Danbooru fetcher 完全沒被算到，那個數字對「我到底打了多少外部 API」這個問題
    是錯的答案。
    """
    for label, thunk in _FETCHERS.items():
        before = ex.api_call_count()
        _run(thunk())
        assert ex.api_call_count() > before, (
            f"{label} 沒有被 api_call_count() 數到——它八成沒走 _http_get_json。")


# ---------------------------------------------------------------------------
# 行為層：合併三份 `_attempt` 之後，原本的語意要一模一樣
# ---------------------------------------------------------------------------

def test_the_anonymous_two_tag_random_limit_still_falls_back(fake_http):
    """匿名 Danbooru 對 `random=true` 有 2-tag 上限，撞到會回 422。

    422 之後要**拿掉 `random`** 再打一次（改抓最新 N 筆，由 client 端自己挑），
    不是直接放棄。三份 `_attempt` 合併成 `_danbooru_posts` 時最容易掉的就是這條。
    """
    fake_http.replies = [(422, None), (200, [{"id": 1}, {"id": 2}])]
    posts = _run(ex._fetch_danbooru_posts_bulk("a b c"))
    assert len(fake_http.calls) == 2, "422 之後應該要再試一次"
    assert fake_http.calls[0]["params"].get("random") == "true"
    assert "random" not in fake_http.calls[1]["params"], (
        "退路必須拿掉 random，否則會再撞一次同樣的 422")
    assert [p["id"] for p in posts] == [1, 2]


def test_a_non_200_is_never_silent(fake_http, capsys):
    """**每一個** fetcher 在非 200 時都要留一行 stderr。

    這是事故能藏這麼久的直接原因。原本靜默的路徑不只 Danbooru 的 post 抓取：
    `/tags.json`（整條 fuzzy tag resolver）與「最新 N 筆」同樣是非 200 就回空、
    一個字都不留。所以三個症狀——隨機圖找不到、模糊 tag 解析不出來、`--latest`
    沒東西——沒有任何一個在 log 裡留下線索。

    這支測試涵蓋 `_FETCHERS` 的**全部**條目，新增站台會自動被納入。
    """
    for label, thunk in _FETCHERS.items():
        fake_http.calls = []
        fake_http.replies = [(403, None), (403, None)]
        capsys.readouterr()
        _run(thunk())
        err = capsys.readouterr().err
        assert "403" in err, f"{label} 在 HTTP 403 時什麼都沒說：{err!r}"


def test_the_expected_422_stays_quiet(fake_http, capsys):
    """反面：預期中的 422 不得吵。

    匿名 Danbooru 對多 tag `random=true` 一定回 422，而我們本來就準備好要退一步
    再打一次——那不是故障。每次搜尋都印一行的話，這條診斷會變成雜訊，然後就沒有
    人會再看它，於是又回到「靜默失敗」的原點。
    """
    fake_http.replies = [(422, None), (200, [{"id": 1}])]
    capsys.readouterr()
    _run(ex._fetch_danbooru_posts_bulk("a b c"))
    err = capsys.readouterr().err
    assert "422" not in err, f"預期中的 422 吵了：{err!r}"


def test_an_unexpected_status_on_the_first_try_still_talks(fake_http, capsys):
    """`quiet_statuses` 只該蓋掉 422，不是把第一次嘗試整個靜音。"""
    fake_http.replies = [(500, None)]
    capsys.readouterr()
    _run(ex._fetch_danbooru_posts_bulk("a b c"))
    assert "500" in capsys.readouterr().err


def test_a_422_that_is_not_a_random_query_is_not_retried(fake_http):
    """沒帶 `random` 的查詢收到 422 時不該重試——退路和原本的請求會一模一樣。"""
    fake_http.replies = [(422, None), (200, [{"id": 9}])]
    _run(ex._danbooru_posts("tag", limit=3, random_order=False))
    assert len(fake_http.calls) == 1, (
        "沒有 random 可以拿掉，重試只是把同一個請求再送一次")


def test_take_unseen_prefers_fresh_ids_then_reuses_the_pool():
    """去重佇列滿了就整池重用，不要回空。

    `_danbooru_recent` 只有 `DANBOORU_HISTORY_SIZE` 筆；池子裡剛好全都送過時，
    寧可重複一張也不要讓使用者看到「找不到」。
    """
    ex._danbooru_recent.clear()
    posts = [{"id": n} for n in range(5)]
    first = ex._take_unseen(posts, 2)
    assert len(first) == 2
    assert all(p["id"] in ex._danbooru_recent for p in first), (
        "挑過的要記進去重佇列，否則下一次還會挑到同一張")

    ex._danbooru_recent.clear()
    ex._danbooru_recent.extend(p["id"] for p in posts)
    again = ex._take_unseen(posts, 2)
    assert len(again) == 2, "全部都送過時要整池重用，不是回空"


def test_take_unseen_handles_a_short_pool():
    """池子比要求的張數少就回多少給多少（`--grid` 靠這個補格子）。"""
    ex._danbooru_recent.clear()
    assert ex._take_unseen([], 4) == []
    assert len(ex._take_unseen([{"id": 1}, {"id": 2}], 4)) == 2


def test_single_post_fetch_returns_none_when_there_is_nothing(fake_http):
    """空結果要回 None，不是丟 IndexError。"""
    fake_http.replies = [(200, [])]
    assert _run(ex._fetch_danbooru_post("tag")) is None


def test_tags_json_drops_non_dict_entries(fake_http):
    """站方限流／出錯時 `/tags.json` 會回一個裡面不是 dict 的 list。"""
    fake_http.replies = [(200, ["oops", {"name": "ok", "post_count": 3}, None])]
    hits = _run(ex._query_tags_json(ex._DANBOORU_TAGS, name="x"))
    assert hits == [{"name": "ok", "post_count": 3}]


def test_fuzzy_resolver_skips_search_modifiers(fake_http):
    """`rating:general` 這種修飾詞不是 tag 名，不得送去 `/tags.json` 解析。"""
    fake_http.replies = [(200, [])] * 20
    _run(ex._resolve_fuzzy_tags(ex._DANBOORU_TAGS, "rating:general score:>=5"))
    assert not fake_http.calls, (
        f"修飾詞被拿去查 tag 了：{fake_http.calls}")


# ---------------------------------------------------------------------------
# 結構層：bot 自己開的 session 也要帶 UA
# ---------------------------------------------------------------------------

def test_the_bots_own_sessions_pass_headers():
    """`discord_bot.py` 手工開的 `ClientSession` 也要在請求上帶標頭。

    圖片 CDN 跟 API 掛在同一套防護後面——2026-08-30 實測 `cdn.donmai.us` 對沒有
    UA 的請求同樣回 403。也就是說 `--grid` 是**兩處**都壞：API 拿不到清單，就算
    拿到了每一張圖也下載不了。修好一邊而漏掉另一邊，症狀會從「找不到」變成
    「一張都貼不出來」，一樣難查。
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
        f"這些請求沒帶 headers：{offenders}。bot 手工開的 session 不會經過 "
        "`_external_apis._http_get_json`，所以 User-Agent 要在呼叫點自己帶——"
        "少了它，圖片 CDN 一樣回 403。")


# ---------------------------------------------------------------------------
# 端點清單的完整性：新增一個外部端點，不得漏掉驗證入口
# ---------------------------------------------------------------------------

def _literal_url(node, consts):
    """從一個 AST 節點盡量還原出網址字面值的開頭；還原不出來回 None。"""
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
    """`NAME = "https://…"` 與 `NAME = f"https://…{x}"` 的對應表。

    要跟著**區域**變數走，不能只看模組層常數：這個版本庫最常見的寫法就是
    `url = f"https://…/{x}"` 然後 `_http_get_json(url)`，第一版只查模組層，於是
    有一半的端點掃不到——而掃不到的那些，正好是這條規則最該保護的。
    """
    # `seed` 是模組層常數。BinOp 那一支要靠它才解得開
    # `target = _IQDB_URL + "?" + urlencode(...)` 這種寫法——區域表格一開始是空的，
    # 沒有 seed 就查不到 `_IQDB_URL`，那個端點會靜默掃不到。
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
    """這個檔案實際會去「抓」的外部主機名。

    只看真的發出請求的呼叫（`_http_get_json` / session 的 `.get` / `.post`），
    所以貼給人看的連結（例如 post 頁面網址）不會被算進來——那些壞了也只是連結
    失效，不是功能消失。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_bindings = _string_bindings(tree, module_level_only=True)
    hosts = set()
    # **一個函式一張表。** 這個版本庫裡幾十個 handler 都把區域變數叫 `url`，
    # 用一張全模組的表只會留下最後一個賦值，於是絕大多數端點靜默掃不到——第一版
    # 就是這樣漏掉了百科站與字典站，而百科站正好是今天壞掉的那一個。
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hosts |= _hosts_fetched_in(scope, _function_bindings(scope,
                                                                 module_bindings))
    hosts |= _hosts_fetched_in(tree, module_bindings)
    return hosts


def _function_bindings(scope, module_bindings):
    """一個函式看得到的網址字面值：模組層常數，再疊上它自己的區域指派。"""
    local = dict(module_bindings)
    local.update(_string_bindings(scope, seed=module_bindings))
    return local


def _request_url(node):
    """`node` 是發出請求的呼叫（`_http_get_json` / `.get` / `.post`）就回它的網址節點。"""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    name = (func.attr if isinstance(func, ast.Attribute)
            else getattr(func, "id", ""))
    return node.args[0] if name in ("_http_get_json", "get", "post") else None


def _host_of(url_node, bindings):
    url = _literal_url(url_node, bindings)
    if not url or not url.startswith("http"):
        return None
    return urllib.parse.urlparse(url).netloc.lower()


def _hosts_fetched_in(scope, bindings):
    """`scope` 裡（含巢狀）真的發出請求的呼叫打到的主機。"""
    hosts = set()
    for node in ast.walk(scope):
        url_node = _request_url(node)
        if url_node is not None:
            host = _host_of(url_node, bindings)
            if host:
                hosts.add(host)
    return hosts


def test_every_fetched_host_is_in_the_verifier():
    """bot 會去抓的每一個外部主機，都要出現在 `verify_external_apis` 的清單裡。

    這條規則是今天兩次事故的**通則**。兩個 bug 都不是程式邏輯錯，是外部契約漂移：
    站方改了規則，我們的請求開始被拒，而失敗路徑全是靜默回空。靜態分析永遠看不到
    這種事，只有真的打一次才知道——所以有 `verify_external_apis.py`。

    但那支掃描是**手寫的清單**，而手寫清單會過期：下次有人加一個新端點，掃描會安靜
    地漏掉它，於是那個端點回到「沒有任何東西會發現它壞了」的狀態，也就是今天的起點。
    所以這裡反過來從原始碼推出「實際會抓哪些主機」，強迫兩邊對得上。

    只看真的發出請求的呼叫；貼給人看的連結不算——那些壞掉只是連結失效，不是功能
    整個消失。
    """
    covered = set()
    for entry in vx._ENDPOINTS:
        url = entry.get("url")
        if url:
            covered.add(urllib.parse.urlparse(url).netloc.lower())
    # CDN 那一筆沒有固定網址（要先跟 API 要一張圖），在清單裡以 `cdn` 群組表示。
    covered.add("cdn.donmai.us")

    fetched = set()
    for path in (_BOT_SOURCE, _MODULE):
        fetched |= _fetched_hosts(path)

    missing = sorted(fetched - covered)
    assert not missing, (
        f"這些主機 bot 會去抓，但不在 verify_external_apis.py 的清單裡：{missing}。"
        "加進 `_ENDPOINTS`，否則它壞掉時不會有任何東西發現——那正是 2026-08-30 "
        "兩次事故的共同成因。")


def test_the_verifier_does_not_list_hosts_nobody_fetches():
    """反方向：清單裡不該有已經沒人在打的主機。

    留著一筆過期的端點，掃描結果就會出現一個沒有人在乎的紅字；紅字一旦習以為常，
    整支掃描就沒用了。這跟 `test_language.py` 記過的教訓是同一條——會亂叫的守門，
    最後會被人關掉。
    """
    fetched = set()
    for path in (_BOT_SOURCE, _MODULE):
        fetched |= _fetched_hosts(path)
    fetched.add("cdn.donmai.us")   # 從 post 的 file_url 動態取得，掃不到字面值

    # `embed_only` 的端點不參與這個比對：bot 自己不抓它們，只是把網址貼出去讓
    # 平台自己取圖，所以原始碼裡本來就不會有抓取呼叫。
    listed = {urllib.parse.urlparse(e["url"]).netloc.lower()
              for e in vx._ENDPOINTS
              if e.get("url") and not e.get("embed_only")}
    # `embed_only` 是一個豁免，所以它必須是**窄的**：標成 embed_only 卻其實有在抓
    # 的端點，等於拿豁免把守門關掉。實測過——沒有這一段的話，把任何一個真的 API
    # 標成 embed_only 就能讓它從此不受檢查，而且沒有任何東西會抱怨。
    mislabelled = sorted(
        urllib.parse.urlparse(e["url"]).netloc.lower()
        for e in vx._ENDPOINTS
        if e.get("embed_only") and e.get("url")
        and urllib.parse.urlparse(e["url"]).netloc.lower() in fetched)
    assert not mislabelled, (
        f"這些端點標了 embed_only，但原始碼其實有在抓它們：{mislabelled}。"
        "embed_only 的意思是「bot 自己不抓，只是把網址貼出去讓平台取」——用它來"
        "豁免一個真的會抓的端點，就是把這道守門關掉。")

    stale = sorted(listed - fetched)
    assert not stale, (
        f"清單裡這些主機原始碼已經沒有在打了：{stale}。移掉它們，不然掃描會報一個"
        "沒有人在乎的紅字。")


def test_the_verifier_reuses_the_real_request_path():
    """驗證腳本必須走 `_http_get_json`，不可以自己另外寫一份 HTTP 呼叫。

    今天兩個 bug **都**出在標頭上。一支自己組請求的驗證腳本會帶著自己的標頭，於是
    永遠是綠的，而正式路徑照樣壞——那比沒有驗證更糟，因為它給人一種已經驗過的錯覺。
    """
    source = inspect.getsource(vx._check_json)
    assert "_http_get_json" in source, (
        "`_check_json` 不再走 `_http_get_json` 了——那它驗的就不是 bot 真正發出的"
        "請求（標頭會不一樣），今天這兩個 bug 它一個都抓不到。")
    raw = inspect.getsource(vx._check_raw)
    assert "_user_agent()" in raw, (
        "`_check_raw` 沒有用 `_user_agent()`，驗到的 UA 跟正式路徑不同。")


def test_a_post_only_endpoint_is_never_checked_with_a_get():
    """只收 POST 的端點必須走 POST checker。

    `_check_json` 底下是 `_http_get_json`，而那支**只發 GET**。拿它去驗一個只收
    POST 的 GraphQL 端點，驗到的是一條 bot 從來不會走的路——2026-09-07 實測那樣
    會拿到 404「Use POST request to access graphql subdomain」，跟正式路徑的結果
    毫無關係。

    這條之所以要有守門：`_run` 的分派是一個 `if entry.get("method") == "POST"`，
    刪掉它不會有任何錯誤，只會讓那一筆安靜地換一條路去驗。
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
    assert post_groups, "清單裡已經沒有 POST 端點了？那這條守門要重新想過"
    for group in post_groups:
        assert ("POST", group) in routed, (
            f"`{group}` 宣告了 method=POST，卻沒有走 POST checker")
        assert ("JSON", group) not in routed, (
            f"`{group}` 只收 POST，卻被 `_check_json`（GET-only）驗了——"
            "驗到的是 bot 從來不會走的路")


def _per_call_timeout_sites(path):
    """這個檔案裡每一個「有自己逾時」的外部呼叫：`[(NAME, 值, 主機集合, 行號), …]`。

    兩種形狀：
    * `_http_get_json(url, timeout=NAME)`——主機就是那個呼叫自己的網址；
    * `aiohttp.ClientTimeout(total=NAME)`（bot 自己開 session 的那幾條：動畫資料庫、
      拼圖下載、反查圖）——主機取**同一個函式裡**真的發出請求的呼叫，跟
      `_fetched_hosts` 同一種一個函式一張表的範圍。網址是執行期才決定的（拼圖下載
      的網址來自 API 回應）就是空集合，由對帳那一側另外處理。

    `ClientTimeout(total=參數)` 是轉手（`_http_get_json` 自己把呼叫端的逾時交給
    session 就是這樣），不算一個逾時來源，略過。

    逾時寫成字面數字直接判錯：驗證腳本要照抄的是**那個名字的值**，一個沒有名字的
    數字沒有辦法對帳，只能靠人記得——那正是這支要拿掉的東西。值解不出來（不是字面
    常數）記成 None，讓對帳那一側說清楚是哪一個。
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
            f"{path.name}:{lineno} 的 {what} 用了沒有名字的逾時 "
            f"`{ast.unparse(value)}`——給它一個模組層常數，驗證腳本才能照抄並對帳。")
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
            if name == "_http_get_json":
                for kw in node.keywords:
                    if kw.arg == "timeout":
                        ident = named(kw.value, node.lineno, "`_http_get_json`")
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
    """`_per_call_timeout_sites` 收成 `NAME → 模組層常數值`。"""
    return {name: value for name, value, _hosts, _line
            in _per_call_timeout_sites(path)}


# 網址在執行期才決定、所以靜態掃不出主機的逾時 → 驗證腳本裡代表它的那一群組。
# 跟 `_fetched_hosts` 的消費端手動補 `cdn.donmai.us` 同一個理由：拼圖下載的網址來自
# API 回應裡每一張圖的 `file_url`。這張表跟掃描結果兩個方向對帳，不會過期而不自知。
_DYNAMIC_HOST_TIMEOUTS = {"GRID_DOWNLOAD_TIMEOUT_SEC": "cdn"}


def test_a_per_call_timeout_in_the_bot_is_mirrored_here():
    """bot 某個外部呼叫自己放寬的逾時，驗證腳本那一筆必須照抄同一個值，兩個方向都對帳。

    2026-09-08 字典呼叫放寬到 `DICT_TIMEOUT_SEC`（端點穩定要約 20 秒），而驗證腳本那一筆
    一直停在預設的 15 秒，直到 2026-09-19 才發現——於是它在 bot 好好的時候報「連不上」。
    反方向（驗證腳本比 bot 寬）更糟：bot 壞著、這支報 ok。所以兩邊都要對上，而且
    驗證腳本不准有一筆「自己放寬、卻對不到 bot 任何一個常數」的逾時。
    """
    bot = {}
    for path in (_BOT_SOURCE, _MODULE):
        bot.update(_per_call_timeouts(path))
    # 正對照：掃描真的掃得到東西。空的掃描結果跟「兩邊一致」長得一模一樣。
    assert "DICT_TIMEOUT_SEC" in bot, (
        f"掃不到字典那個呼叫的逾時了（掃到的是 {sorted(bot)}）——掃描本身壞了，"
        "或是那個呼叫改名；先修掃描，不要讓這支變成空轉。")
    unresolved = sorted(name for name, value in bot.items() if value is None)
    assert not unresolved, f"這些逾時常數解不出值：{unresolved}"

    mirrored = {e["bot_timeout"]: e.get("timeout")
                for e in vx._ENDPOINTS if e.get("bot_timeout")}
    missing = sorted(set(bot) - set(mirrored))
    assert not missing, (
        f"bot 這些呼叫有自己的逾時，但驗證腳本沒有照抄：{missing}。在 `_ENDPOINTS` 對應"
        "那一筆加上 `timeout` 與 `bot_timeout`。")
    stale = sorted(set(mirrored) - set(bot))
    assert not stale, f"驗證腳本照抄的這些逾時，bot 那一側已經沒有了：{stale}"
    wrong = {name: (mirrored[name], bot[name]) for name in bot
             if mirrored[name] != bot[name]}
    assert not wrong, f"驗證腳本的逾時跟 bot 不一樣（驗證腳本, bot）：{wrong}"
    loose = sorted(e["group"] for e in vx._ENDPOINTS
                   if "timeout" in e and not e.get("bot_timeout"))
    assert not loose, (
        f"這幾筆自己改了逾時、卻沒有對應到 bot 的任何常數：{loose}——那就是「驗證腳本"
        "比 bot 寬」，會在 bot 壞著的時候報 ok。")

    # 2026-09-19 補：bot 自己開 session 的三條（`ClientTimeout(total=NAME)`）也在上面
    # 的名字集合裡。**名字對得上還不夠，要對到正確的那一筆**：把動畫資料庫的逾時掛到
    # 反查圖那一筆上，名字與值都照樣對得上，驗的卻是另一個端點。所以再以「同一個函式
    # 打到的主機」對帳一次，兩個方向。
    sites = [site for path in (_BOT_SOURCE, _MODULE)
             for site in _per_call_timeout_sites(path)]
    assert {"ANIME_TIMEOUT_SEC", "IQDB_TIMEOUT_SEC",
            "GRID_DOWNLOAD_TIMEOUT_SEC"} <= {name for name, *_ in sites}, (
        "掃不到 bot 自己開 session 的那幾個逾時了——`ClientTimeout(total=…)` 那一半"
        f"的掃描壞了（掃到的是 {sorted({name for name, *_ in sites})}）")
    hosts_of = {}
    for name, _value, hosts, _line in sites:
        hosts_of.setdefault(name, set()).update(hosts)

    def entry_host(e):
        return urllib.parse.urlparse(e["url"]).netloc.lower() if e.get("url") else None

    dynamic = sorted(name for name, hosts in hosts_of.items() if not hosts)
    assert dynamic == sorted(_DYNAMIC_HOST_TIMEOUTS), (
        f"主機掃不出來的逾時是 {dynamic}，`_DYNAMIC_HOST_TIMEOUTS` 列的是 "
        f"{sorted(_DYNAMIC_HOST_TIMEOUTS)}——新的一條要說清楚它對到哪一群組，"
        "舊的一條已經不是動態網址就該拿掉。")
    wrong_home = []
    for name, hosts in hosts_of.items():
        carriers = [e for e in vx._ENDPOINTS if e.get("bot_timeout") == name]
        if hosts:
            wrong_home += [f"{name} 掛在 {e['group']}（主機 {entry_host(e)}）"
                           for e in carriers if entry_host(e) not in hosts]
            wrong_home += [f"{e['group']}（{entry_host(e)}）沒有掛 {name}"
                           for e in vx._ENDPOINTS
                           if entry_host(e) in hosts and e.get("bot_timeout") != name]
        else:
            groups = sorted(e["group"] for e in carriers)
            if groups != [_DYNAMIC_HOST_TIMEOUTS[name]]:
                wrong_home.append(f"{name} 掛在 {groups}，應該是 "
                                  f"{[_DYNAMIC_HOST_TIMEOUTS[name]]}")
    assert not wrong_home, (
        f"逾時照抄到錯的那一筆了：{wrong_home}。值一樣也沒用——驗到的是另一個端點。")

    # 非 JSON／POST 的檢查函式在沒有 `timeout` 時退回 `_EMBED_ONLY_TIMEOUT_SEC`，那個
    # 預設只准給對話平台自己去取的 embed_only 那幾筆用（bot 那一側沒有逾時可以照抄）。
    defaulted = sorted(e["group"] for e in vx._ENDPOINTS
                       if (e.get("raw") or e.get("method") == "POST")
                       and "timeout" not in e and not e.get("embed_only"))
    assert not defaulted, (
        f"這幾筆 bot 自己會抓，卻用驗證腳本自己的預設逾時：{defaulted}。")


def test_the_dictionary_probe_cannot_be_answered_from_a_cache():
    """字典那一筆每次執行都要查一個**新的**字，否則 CDN 的過期快取會讓它報 ok。

    2026-09-19 實測：原站連不上時，查過的字（例如舊寫法固定的 `serendipity`）拿到的是
    幾十天前的快取（HTTP 200），沒查過的字才露出 522。
    """
    words = {vx._fresh_probe_word() for _ in range(50)}
    assert len(words) == 50, "查詢字沒有每次都換，快取會替壞掉的原站擋下來"
    entry = next(e for e in vx._ENDPOINTS if e["group"] == "dict")
    assert entry["url"].rsplit("/", 1)[1] == vx._DICT_PROBE_WORD, (
        f"字典那一筆沒有用每次執行才產生的查詢字：{entry['url']}")
    assert 404 in entry.get("ok_statuses", ()), (
        "查一個不存在的字，健康的原站回 404——沒把 404 當健康，這一筆永遠是紅的")


@pytest.mark.parametrize("group,status,expected", [
    ("dict", 404, "OK"),     # 宣告過的「健康時的非 200」
    ("dict", 522, "FAIL"),   # 2026-09-19 真的收到的那個
    ("xkcd", 404, "FAIL"),   # near-miss：沒宣告的端點，404 就是壞了
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
    # 逾時與安靜狀態碼要真的傳下去，不只是寫在清單裡。
    assert calls[0]["timeout"] == entry.get("timeout", ex._HTTP_TIMEOUT_SEC)
    assert calls[0]["quiet"] == tuple(entry.get("ok_statuses", ()))


def test_a_5xx_is_blamed_on_the_upstream_not_our_headers():
    """522 這種是上游自己出錯，不要把人帶去查 User-Agent 或 `api_contact`。"""
    out = vx._diagnose(522, body="error code: 522")
    assert "上游" in out, f"5xx 沒有說是上游的問題：{out!r}"
    assert "User-Agent" not in out and ex._UA_CONTACT_KEY not in out, (
        f"5xx 被說成標頭的問題：{out!r}")
    # near-miss：沒列在任何分支裡的 4xx 照舊不亂給建議。
    assert vx._diagnose(404, body=None) == ""


def test_the_diagnosis_quotes_the_upstream_instead_of_guessing():
    """上游自己講了原因，就不要再猜 User-Agent。

    測資是 2026-09-07 從動畫資料庫真的收到的 403 主體。當時的診斷把它報成
    「這一類幾乎都是 User-Agent 的問題」並要人去設 `api_contact`——而實測無 UA、
    瀏覽器 UA、我們的 UA 三者都是 403，UA 根本不是變因。

    **刻意用 GET** 來驗這一條：這樣它只會被「引用上游」那一段影響，跟下面那條
    「非 GET 不套 UA 說法」互不遮蔽。兩支測試各自瞄準一個分支。
    """
    body = json.dumps({"errors": [{
        "message": "The AniList API has been temporarily disabled due to "
                   "severe stability issues.",
        "status": 403}]})
    out = vx._diagnose(403, body=body, method="GET")
    assert "temporarily disabled" in out, f"沒有引用上游的說明：{out!r}"
    assert "User-Agent" not in out, (
        f"上游已經說了原因，卻還在猜 User-Agent：{out!r}")
    assert ex._UA_CONTACT_KEY not in out, (
        f"上游已經說了原因，卻還在叫人去設聯絡方式：{out!r}")


def test_a_non_get_failure_does_not_blame_the_user_agent():
    """非 GET 端點的 4xx 不套 UA 那套說法。

    **刻意不給主體**：這樣它只會被「method != GET」那一段影響，與上面那支隔離。
    """
    out = vx._diagnose(403, body=None, method="POST")
    assert "User-Agent" not in out or "不要預設是 User-Agent" in out, (
        f"對 POST 端點硬套 UA 說法：{out!r}")
    assert ex._UA_CONTACT_KEY not in out, (
        f"對 POST 端點叫人去設聯絡方式，那個鍵只影響共用的 GET 路徑：{out!r}")


def test_a_plain_get_403_still_gets_the_actionable_ua_hint():
    """反向守門：原本**正確**的那一半不可以在收斂誤報時被一起弄掉。

    百科站的 403 就是真的 UA／聯絡方式問題，而且它的失敗頁撈不出 JSON 訊息。
    這支確保「不要亂猜」沒有被做成「什麼都不說」。
    """
    out = vx._diagnose(403, body="<html>just a moment</html>", method="GET")
    assert "User-Agent" in out
    assert ex._UA_CONTACT_KEY in out or ex._configured_contact()


def test_the_upstream_extractor_never_invents_a_message():
    """撈不到就回空字串，不要硬掰。"""
    assert vx._upstream_message(None) == ""
    assert vx._upstream_message("") == ""
    assert vx._upstream_message("<html>Just a moment...</html>") == ""
    assert vx._upstream_message("not json at all") == ""
    assert vx._upstream_message(json.dumps([1, 2, 3])) == ""
    assert vx._upstream_message(json.dumps({"errors": [{}]})) == ""


def test_the_upstream_message_is_bounded_and_single_line():
    """主體是第三方位元組：截短、壓成一行，不要讓一整頁噴進主控台。"""
    long_msg = ("x" * 5000) + "\n" + ("y" * 5000)
    out = vx._upstream_message(json.dumps({"message": long_msg}), limit=100)
    assert len(out) <= 100
    assert "\n" not in out and "\r" not in out


# ---------------------------------------------------------------------------
# 實測層：真的打一次，確認我們現在沒有被擋
# ---------------------------------------------------------------------------

def _live_status(url, params, headers):
    """打一次真的請求，回 HTTP 狀態碼；連不上回 None（→ skip，不是紅字）。"""
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
    """站方現在還吃我們的 UA 嗎？

    這是整支檔案裡唯一能抓到「站方改規則」的測試——那種變化沒有任何靜態分析看得
    見，而失敗路徑全是靜默的回空。2026-08-30 這一題的答案是 403，而且已經 403 了
    一段時間，沒有任何東西紅過。

    連不上網路 → skip（離線環境不該有紅字）。連得上但被拒絕 → 紅字，因為那就是
    真的壞了。
    """
    status = _live_status(url, params, {"User-Agent": ex._BOT_UA})
    if status is None:
        pytest.skip("連不到外部站台（離線？）——這一題只在連得上時才有意義")
    assert status == 200, (
        f"Danbooru /{label} 用我們的 UA 回了 HTTP {status}。403 代表站方的防護又"
        f"把我們擋掉了（UA={ex._BOT_UA!r}）；照 Help:Api 的規定調整 UA，不要改成"
        "假裝瀏覽器——實測過那樣一樣被擋。")


# ---------------------------------------------------------------------------
# 聯絡方式：有站台不吃「只自報名稱」的 UA
# ---------------------------------------------------------------------------

def test_the_contact_is_folded_into_the_user_agent(fake_http, monkeypatch):
    """設了 `api_contact` 就要照站方要的格式帶上去。

    2026-08-30 實測維基媒體的 REST API，三種 UA 三種結果：
        完全沒有 UA          -> 403「Please set a user-agent…」
        只自報名稱的 UA      -> 403「…Contact bot-traffic@wikimedia.org…」
        UA 裡有 URL 或 email -> 200
    第二則不是在說我們沒自報身分，是在說**沒留下聯絡方式**。這兩件事不一樣，而
    這支測試釘的就是那個差別有被實作出來。
    """
    monkeypatch.setattr(ex, "_configured_contact", lambda: "https://example.test/bot")
    fake_http.calls = []
    _run(ex._http_get_json("https://example.invalid/x"))
    ua = fake_http.calls[0]["headers"]["User-Agent"]
    assert "https://example.test/bot" in ua, f"聯絡方式沒進 UA：{ua!r}"
    assert "axiomatic" in ua, f"帶了聯絡方式就不自報名稱了：{ua!r}"


def test_no_contact_configured_still_sends_a_usable_user_agent(fake_http,
                                                               monkeypatch):
    """沒設定聯絡方式時**仍然**要有 UA。

    這是預設狀態，而大部分站台（圖庫那幾個）只要求自報名稱就夠了。要是「沒設定
    聯絡方式」被實作成「那就不要帶 UA 了」，就會把已經修好的 403 整組打回去。
    """
    monkeypatch.setattr(ex, "_configured_contact", lambda: "")
    fake_http.calls = []
    _run(ex._http_get_json("https://example.invalid/x"))
    assert fake_http.calls[0]["headers"]["User-Agent"] == ex._BOT_UA


def test_a_broken_config_does_not_take_the_user_agent_down(monkeypatch):
    """設定檔壞掉／讀不到時要退回空字串，不是讓每個外部請求都爆掉。"""
    def boom():
        raise OSError("config gone")
    monkeypatch.setattr(ex, "load_bot_config", boom)
    assert ex._configured_contact() == ""
    assert ex._user_agent() == ex._BOT_UA


def test_a_403_without_a_contact_says_which_config_key_to_set(fake_http,
                                                              capsys,
                                                              monkeypatch):
    """403 ＋ 沒設聯絡方式 → 診斷要指名那個鍵。

    光看「HTTP 403」查不出這一題：我們**有**帶 UA，狀態碼也不會說「你缺的是聯絡
    方式」。少了這一行，下一個人得重跑一次今天這整套實驗才知道要改哪裡。
    """
    monkeypatch.setattr(ex, "_configured_contact", lambda: "")
    fake_http.replies = [(403, None)]
    capsys.readouterr()
    _run(ex._http_get_json("https://example.invalid/x"))
    err = capsys.readouterr().err
    assert ex._UA_CONTACT_KEY in err, f"沒說要設哪個鍵：{err!r}"


def test_the_contact_hint_stays_quiet_once_it_is_configured(fake_http, capsys,
                                                            monkeypatch):
    """反面：設好之後 403 就不該再叫人去設它了——那會變成誤導。"""
    monkeypatch.setattr(ex, "_configured_contact", lambda: "https://example.test/bot")
    fake_http.replies = [(403, None)]
    capsys.readouterr()
    _run(ex._http_get_json("https://example.invalid/x"))
    err = capsys.readouterr().err
    assert "403" in err, "403 本身還是要留一行"
    assert ex._UA_CONTACT_KEY not in err, f"已經設定了還在叫人設：{err!r}"


def test_the_config_key_exists_and_defaults_to_empty():
    """`api_contact` 要真的是設定檔的一個鍵，而且預設為空。

    預設空是刻意的：這個字串會被送到第三方站台，放什麼上去是擁有者的決定。程式
    不該替他挑一個，尤其不該把他的 email 直接寫死進原始碼。
    """
    from _bot_config import load_bot_config as real_load
    cfg = real_load()
    assert ex._UA_CONTACT_KEY in cfg, (
        f"`{ex._UA_CONTACT_KEY}` 不在 bot_config 的輸出裡——`_coerce` 那一段漏了它，"
        "於是設定檔怎麼填都不會生效。")
    assert isinstance(cfg[ex._UA_CONTACT_KEY], str)


@pytest.mark.parametrize("contact,expected", [
    ("", False),
    ("   ", False),
    ("https://example.test/bot", True),
])
def test_a_blank_contact_counts_as_unset(contact, expected, monkeypatch,
                                         fake_http):
    """只有空白的設定值等同沒設定——否則 UA 會變成 `axiomatic-bot/1.0 (   )`。"""
    monkeypatch.setattr(ex, "load_bot_config", lambda: {ex._UA_CONTACT_KEY: contact})
    assert bool(ex._configured_contact()) is expected


def test_wikimedia_really_does_want_a_contact_not_just_a_name():
    """實測層：對維基媒體來說，只自報名稱的 UA 真的還是不夠嗎？

    這一題釘的是「為什麼要有 `api_contact` 這個設定」本身。哪天站方放寬了，這支
    會紅，那就是去把這個設定與它的說明文字重寫的信號——留著一個已經沒必要的設定
    鍵，比沒有它更糟。

    連不上網路 → skip。
    """
    url = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
           "Python_(programming_language)")
    bare = _live_status(url, None, {"User-Agent": ex._BOT_UA})
    if bare is None:
        pytest.skip("連不到外部站台（離線？）")
    withc = _live_status(url, None, {
        "User-Agent": "axiomatic-bot/1.0 (https://example.test/bot)"})
    assert withc == 200, (
        f"連帶了聯絡方式的 UA 都拿不到 200（HTTP {withc}）——站方的規則又變了，"
        "重新實測一次再改 `_user_agent` 的格式。")
    assert bare != 200, (
        "只自報名稱的 UA 現在也通過了。站方放寬了規則——請重寫 `api_contact` 的"
        "說明（或整個拿掉它），不要留著一個已經沒有理由的設定鍵。")


def test_the_default_user_agent_is_applied_in_the_one_place_it_can_be():
    """預設 UA 必須就補在 `_http_get_json` 裡，不能散在各個呼叫點。

    寫成「每個呼叫點自己記得帶」的版本正是壞掉的那一版：漏掉一個就是靜默 403，
    而漏掉是常態。補在唯一出口才是結構上補得起來的做法。

    （原本這裡還有一條「模組裡不得再出現『某某站可以不帶 UA』這種會過期的註解」
    的字串比對。刪掉了——這支檔案自己的說明就會引用那句話，於是它會永遠紅。
    禁用詞清單本來就容易誤傷；`test_language.py` 早就記過同一個教訓：會亂叫的
    守門，最後會被人關掉。真正要釘住的是下面這條不變式。）
    """
    default_ua = inspect.getsource(ex._http_get_json)
    assert "_BOT_UA" in default_ua, (
        "`_http_get_json` 不再補預設 UA 了——那是所有站台的唯一保障。")
    for name in ("_danbooru_posts", "_fetch_danbooru_posts_latest",
                 "_query_tags_json"):
        body = inspect.getsource(getattr(ex, name))
        # 找的是**當成 dict key 的字串字面值**（帶引號），不是註解／docstring
        # 裡提到這四個字——第一版就是這樣誤傷了自己的說明文字。
        assert chr(34) + 'User-Agent' + chr(34) not in body, (
            f"{name} 自己塞了 User-Agent。預設值屬於 `_http_get_json`；散回呼叫點"
            "就會回到「漏一個就靜默 403」的老路。站台真的有自己的規範時，走 "
            "`_TagsAPI.headers` 那種明講的資料結構。")


# ---------------------------------------------------------------------------
# tag 解析：`_best_tag_for_window`
#
# 2026-09-01 用覆蓋率掃出來——這是所有共用／支援模組裡**唯一**一支超過 8 個
# statement 卻一行都沒跑過的函式。它決定使用者打的自由文字要對到哪個 tag，錯了就是
# 「搜出完全無關的圖」或「明明有卻說找不到」，而且兩種都不會留下任何錯誤訊息。
#
# 它的 docstring 記著三個**用實際站台資料換來的**決定：`post_count > 0`（站上有大量
# 零張的 stale tag）、單 token 用 prefix `tok*` 而不是 substring `*tok*`
# （`*rossi*` 會把 `animal_crossing` 拉到第一名）、以及 `.get("name")` 而不是
# `["name"]`（站方少回一個欄位時，KeyError 會一路冒到 mention dispatcher，
# 把「找不到 tag」變成一則錯誤回覆）。三個決定原本都沒有守門。
# ---------------------------------------------------------------------------

def _stub_tags(monkeypatch, *replies):
    """把 `_query_tags_json` 換掉，回傳「每一次呼叫收到的關鍵字」清單。

    斷言的是**送出去的查詢長什麼樣**，不只是回傳值——上面那三個決定裡有兩個
    （prefix vs substring、短 token 不做 fuzzy）只看得出來在查詢字串上。
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
    """站方少回 `name` 欄位時要當成沒命中，不能讓 KeyError 冒出去。

    這是 `.get("name")` 那行註解記載的真實故障：例外會一路冒到 mention dispatcher
    的 catch-all，於是使用者看到的不是「找不到這個 tag」而是一則泛用錯誤訊息——
    同一個症狀，完全不同的原因，最難查的那種。
    """
    _stub_tags(monkeypatch, [{"post_count": 500}])
    assert _best(["surtr"]) is None


@pytest.mark.parametrize("name", [None, 123, "", [], {}])
def test_a_non_string_name_is_not_a_hit(monkeypatch, name):
    _stub_tags(monkeypatch, [{"name": name, "post_count": 500}])
    assert _best(["surtr"]) is None


@pytest.mark.parametrize("count", [0, None, -1])
def test_a_zero_post_tag_is_never_returned(monkeypatch, count):
    """站上有大量 post_count 為 0 的 stale tag。配到它們等於搜出空結果。"""
    _stub_tags(monkeypatch, [{"name": "stale_tag", "post_count": count}],
               [{"name": "stale_tag", "post_count": count}])
    assert _best(["surtr"]) is None


def test_an_exact_hit_never_runs_the_fuzzy_query(monkeypatch):
    """exact 命中就收工——多打一次 fuzzy 是白花一次對外請求。"""
    calls = _stub_tags(monkeypatch, [{"name": "surtr_(arknights)",
                                      "post_count": 900}])
    assert _best(["surtr"]) == "surtr_(arknights)"
    assert len(calls) == 1, f"exact 命中之後還多查了一次：{calls}"
    assert calls[0].get("name") == "surtr", calls[0]


def test_a_short_single_token_never_runs_the_fuzzy_query(monkeypatch):
    """`cp` / `bb` / `ru` 這種 ≤2 字元的 token，`cp*` 最熱門的是
    `cpu_(hexivision)` 之類完全無關的東西，幾乎必錯。exact 仍然照試。"""
    short = "x" * (ex._FUZZY_MIN_TOKEN_LEN - 1)
    calls = _stub_tags(monkeypatch, [])          # exact 沒命中
    assert _best([short]) is None
    assert len(calls) == 1, f"短 token 還是跑了 fuzzy：{calls}"


def test_a_long_single_token_falls_back_to_a_prefix_not_a_substring(monkeypatch):
    """fuzzy 用 `tok*` 而**不是** `*tok*`。

    中間 substring 會撈到無關的 tag——docstring 記的實例是 `*rossi*` 把
    `animal_crossing` 拉到第一名。這條只看得出來在送出去的查詢字串上，所以這裡
    斷言的是 `name_matches` 的形狀，不是回傳值。
    """
    calls = _stub_tags(monkeypatch, [],          # exact 沒命中
                       [{"name": "surtr_(arknights)", "post_count": 900}])
    assert _best(["surtr"]) == "surtr_(arknights)"
    assert len(calls) == 2, calls
    pattern = calls[1].get("name_matches")
    assert pattern == "surtr*", f"fuzzy 用了 {pattern!r}，不是 prefix"
    assert not pattern.startswith("*"), (
        "前面多了 `*` 就變成中間 substring 配對——`*rossi*` 會配到 "
        "`animal_crossing`，而它的 post_count 遠高於使用者真正要的那個")


def test_multiple_tokens_use_an_ordered_substring_pattern(monkeypatch):
    """多 token 才用 `*a*b*`：多個 substring 配 ＋ 順序限制已經夠精確。"""
    calls = _stub_tags(monkeypatch,
                       [{"name": "surtr_(arknights)", "post_count": 900}])
    assert _best(["surtr", "arknights"]) == "surtr_(arknights)"
    assert len(calls) == 1, "多 token 不該先打一次 exact"
    assert calls[0].get("name_matches") == "*surtr*arknights*", calls[0]


def test_an_empty_window_asks_nothing(monkeypatch):
    calls = _stub_tags(monkeypatch)
    assert _best([]) is None
    assert not calls, "空 window 還是送出了請求"


def test_the_public_contact_predicate_matches_the_configured_value(monkeypatch):
    """`contact_configured()` 是 bot 那一側用來決定「要不要多講一句」的依據。

    它是 `_configured_contact()` 的薄包裝，所以很容易被當成不用測——但把它寫反
    （`not`）不會讓任何呼叫端的測試變紅：那些測試多半直接把這支換掉。實測過，
    這個變異在補這一筆之前是存活的。
    """
    for raw, expected in (("", False), ("   ", False), (None, False),
                          ("https://example.test/bot", True),
                          ("someone@example.test", True)):
        monkeypatch.setattr(ex, "load_bot_config",
                            lambda raw=raw: {"api_contact": raw})
        assert ex.contact_configured() is expected, repr(raw)


def test_the_contact_predicate_never_raises(monkeypatch):
    """設定讀不到時要回 False（fail-closed：寧可少講一句，也不要因此炸掉呼叫端）。"""
    def boom():
        raise OSError("config unreadable")

    monkeypatch.setattr(ex, "load_bot_config", boom)
    assert ex.contact_configured() is False


# ---------------------------------------------------------------------------
# `_resolve_fuzzy_tags` 的貪婪最長配對
#
# 上面那一段驗的是「單一個 window 該配到哪個 tag」；這一段驗的是**怎麼切 window**。
# 2026-09-06 量覆蓋率時發現這 29 行裡有 18 行沒被跑過，而它做的事是**改寫使用者
# 打進來的查詢**——切錯的症狀不是「找不到」，是「安靜地找到別的東西」，因為畫面上
# 看不出查詢被動過手腳。
#
# 這裡的假 `_query_tags_json` 用表格回答（鍵是送出去的 `name` 或 `name_matches`），
# 這樣才驗得到「哪些 window 被試過、順序如何」。
# ---------------------------------------------------------------------------

def _resolve_with(monkeypatch, table: dict, raw: str):
    """回 `(結果, 送出去的查詢清單)`。`table` 的鍵是 `name` 或 `name_matches`。"""
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
    """一個 token 都沒被改寫時要回 None。

    回一個跟輸入相同的字串的話，呼叫端會以為解析成功、再打一次同樣的查詢，然後
    使用者看到的是同一句「找不到」——只是多花了一輪 API 額度。
    """
    out, _ = _resolve_with(monkeypatch, {}, "aaa bbb")
    assert out is None


def test_an_empty_query_never_touches_the_api(monkeypatch):
    out, calls = _resolve_with(monkeypatch, {}, "   ")
    assert out is None
    assert calls == []


def test_a_token_that_resolves_to_itself_is_not_a_rewrite(monkeypatch):
    """exact 命中但名字沒變 → 沒有改寫，照樣回 None。"""
    out, _ = _resolve_with(monkeypatch, {"yuri": _tag_hit("yuri")}, "yuri")
    assert out is None


def test_the_longest_window_wins(monkeypatch):
    """`lappland decadenza` 要被當成**一個** tag，不是各配各的。

    由最長 window 往最短試就是為了這件事：分開配會得到兩個各自存在、合起來卻完全
    不是使用者要的東西的 tag，而搜尋結果看起來「有東西」，所以不會有人發現配錯了。
    """
    table = {
        "*lappland*decadenza*": _tag_hit("lappland_the_decadenza_(arknights)"),
        "lappland*": _tag_hit("lappland_(arknights)"),
        "decadenza*": _tag_hit("decadenza_(something_else)"),
    }
    out, calls = _resolve_with(monkeypatch, table, "lappland decadenza")
    assert out == "lappland_the_decadenza_(arknights)"
    assert calls[0] == "*lappland*decadenza*", (
        f"沒有從最長的 window 開始試：{calls}")


def test_tokens_after_a_matched_window_are_still_resolved(monkeypatch):
    """配到一個 window 之後，游標要跳到它的結尾繼續，不是重頭來也不是停下。"""
    table = {
        "*lappland*decadenza*": _tag_hit("lappland_the_decadenza_(arknights)"),
        "yuri": _tag_hit("yuri_tag"),
    }
    out, _ = _resolve_with(monkeypatch, table, "lappland decadenza yuri")
    assert out == "lappland_the_decadenza_(arknights) yuri_tag"


def test_an_unmatched_token_is_kept_verbatim(monkeypatch):
    """配不到就原樣留著。丟掉它等於安靜地放寬使用者的搜尋條件。"""
    table = {"lappl*": _tag_hit("lappland_(arknights)")}
    out, _ = _resolve_with(monkeypatch, table, "lappl zzzz")
    assert out == "lappland_(arknights) zzzz"


def test_a_window_that_spans_a_modifier_is_never_tried(monkeypatch):
    """含修飾詞的 window 整段跳過——不能把 `rating:general` 拼進 `*a*b*` 裡。

    既有的那一支驗的是「只有修飾詞時不查」；這一支驗的是「修飾詞夾在中間時，
    跨過它的那些 window 也不能查」，那是不同的一條路。
    """
    table = {"aaa": _tag_hit("aaa_tag")}
    out, calls = _resolve_with(monkeypatch, table, "aaa rating:general bbb")
    assert out == "aaa_tag rating:general bbb"
    assert all("rating:general" not in (c or "") for c in calls), calls


def test_a_modifier_keeps_its_position(monkeypatch):
    """修飾詞要留在原位。搬動它會改變它作用的範圍。"""
    table = {"aaa": _tag_hit("aaa_tag")}
    out, _ = _resolve_with(monkeypatch, table, "score:>=5 aaa")
    assert out == "score:>=5 aaa_tag"


# 明顯超過 `_FUZZY_MAX_TOKENS` 的 token 數，用來確認上限真的有截斷。
_FUZZY_OVERFLOW = 40


def test_a_very_long_query_is_capped(monkeypatch):
    """上限存在是為了不讓一個長輸入連發數十次 API 呼叫，每一次都可能被限流。"""
    raw = " ".join(f"tok{i}" for i in range(_FUZZY_OVERFLOW))
    out, calls = _resolve_with(monkeypatch, {}, raw)
    assert out is None
    joined = " ".join(str(c) for c in calls)
    assert f"tok{ex._FUZZY_MAX_TOKENS}" not in joined, (
        f"處理到了上限之後的 token（上限 {ex._FUZZY_MAX_TOKENS}）")



# ---------------------------------------------------------------------------
# 驗證腳本不得比 bot 寬鬆
# ---------------------------------------------------------------------------
# 驗證腳本裡**有名字、但不是從 `_ENDPOINTS` 那一格來**的逾時，只准出現在指定的函式：
# `_error_body` 是「已經失敗之後再打一次、只為了撈上游說明」，不決定任何判定。
_VERIFIER_NAMED_TIMEOUT_HOMES = {"_ERROR_BODY_TIMEOUT_SEC": "_error_body"}
# `entry.get("timeout", 預設)` 的預設值只准是這兩個：JSON 那條跟 bot 共用的預設，以及
# 只給 embed_only（bot 自己不抓、沒有逾時可照抄）用的那一個。
_VERIFIER_TABLE_DEFAULTS = {"_HTTP_TIMEOUT_SEC", "_EMBED_ONLY_TIMEOUT_SEC"}


def _verifier_timeout_offenders(tree):
    """驗證腳本裡**不是從對帳過的表來**的逾時：回 `(違規清單, _http_get_json 呼叫數,
    ClientTimeout 呼叫數)`。

    看兩種形狀：`_http_get_json(..., timeout=X)` 與 `ClientTimeout(total=X)`。X 必須是
    `entry.get("timeout", 預設)`（直接寫、或先指派給一個名字再傳），預設值只准是
    `_VERIFIER_TABLE_DEFAULTS` 裡的名字；或是 `_VERIFIER_NAMED_TIMEOUT_HOMES` 列的名字、
    而且只在它指定的那個函式裡。寫死的數字一律算違規——那就繞過了跟 bot 的對帳。
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
    """上面那支的判準自己要有對照組：驗證腳本乾淨的時候，「沒有違規」會空轉通過。"""
    def scan(src):
        return _verifier_timeout_offenders(ast.parse(src))[0]

    assert scan("async def f(entry):\n"
                "    ClientTimeout(total=20)\n") == ["2: 20"], "寫死的數字沒被抓"
    assert scan("async def f(entry):\n"
                "    ClientTimeout(20)\n") == ["2: 20"], "位置引數的寫法沒被抓"
    assert scan("async def f(entry):\n"
                "    _http_get_json(u, timeout=30.0)\n") == ["2: 30.0"]
    assert scan("async def f(entry):\n"
                "    t = entry.get('timeout', 99)\n"
                "    ClientTimeout(total=t)\n") == ["3: t"], "預設值不在允許清單沒被抓"
    assert scan("async def _check_raw(entry):\n"
                "    ClientTimeout(total=_ERROR_BODY_TIMEOUT_SEC)\n") == [
        "2: _ERROR_BODY_TIMEOUT_SEC"], "診斷用的逾時跑到別的函式沒被抓"
    assert scan("async def f(entry):\n"
                "    t = entry.get('timeout', _EMBED_ONLY_TIMEOUT_SEC)\n"
                "    ClientTimeout(total=t)\n"
                "    _http_get_json(u, timeout=entry.get('timeout', ex._HTTP_TIMEOUT_SEC))\n"
                "async def _error_body(u):\n"
                "    ClientTimeout(total=_ERROR_BODY_TIMEOUT_SEC)\n") == []


def test_the_verifier_takes_its_timeouts_only_from_the_reconciled_table():
    """驗證腳本裡**沒有寫死的逾時**：唯一的來源是 `_ENDPOINTS` 那一格，而那一格由
    `test_a_per_call_timeout_in_the_bot_is_mirrored_here` 跟 bot 對帳。

    2026-09-19 前這支叫 `test_the_verifier_never_gives_an_endpoint_its_own_timeout`，
    規則是「驗證腳本一律不准指定逾時」。它防的事是對的——2026-09-08 `/web dict` 的端點
    要約 20 秒，最順手的「修法」是在驗證腳本裡把逾時調大讓它變綠，那會把一個真的壞掉
    的使用者面指令蓋起來。**但同一天 bot 那一側照規則放寬了呼叫端**（`DICT_TIMEOUT_SEC`），
    從那時起「一律用預設」就等於「比 bot **窄**」：bot 好好的，這支報連不上——規則寫的是
    「不准比 bot 寬」，實作量的卻是「不准指定」，兩者在 bot 自己放寬之後就分岔了。
    現在的等價寫法是「只准照抄 bot 的值」，照抄由對帳測試守；這支守的是「沒有第二個
    來源」——在呼叫處寫死一個數字，就繞過了那張對帳過的表。

    用 AST 而不是字串比對：這條規則的理由寫在 `_check_json` 的 docstring 裡，掃字串會
    掃到那段說明然後自己通過。

    2026-09-19 補上 `ClientTimeout(total=…)` 那一半：非 JSON 與 POST 的兩支檢查原本
    寫死 `total=20`，而 bot 的動畫資料庫 POST 是 15 秒——驗證腳本比 bot 寬，會在 bot
    逾時的時候報 ok。原本的掃描只看 `_http_get_json` 的 `timeout=`，所以看不到它們。
    """
    source = (Path(__file__).resolve().parent.parent / "axiomatic"
              / "verify_external_apis.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders, calls, sessions = _verifier_timeout_offenders(tree)
    assert calls, "驗證腳本裡一個 `_http_get_json` 呼叫都掃不到——掃描壞了，這支會空轉"
    assert sessions >= 3, (
        f"驗證腳本裡的 `ClientTimeout` 只掃到 {sessions} 個（`_check_raw`、`_check_post`、"
        "`_error_body` 各一個）——那一半的掃描壞了，這支會空轉")
    assert not offenders, (
        f"verify_external_apis.py 這些呼叫的逾時不是從 `_ENDPOINTS` 那一格來的：{offenders}。"
        "寫死的逾時繞過了跟 bot 的對帳——要改逾時，改 bot 那一側的常數，再把 `_ENDPOINTS`"
        "對應那一筆的 `timeout`／`bot_timeout` 照抄過來。")


def test_the_default_timeout_is_a_named_constant():
    """預設逾時要有名字，因為**別的模組要印它**。

    `verify_external_apis` 回報「沒有回應」時會把這個秒數印出來——沒有它，讀的人
    分不出「連不上」與「端點活著但比這個上限慢」，而那兩者的處置完全相反。
    """
    # `timeout` 是 keyword-only（簽名裡在 `*` 之後），所以預設值住在
    # `__kwdefaults__` 而不是 `__defaults__`——用 `signature` 就不必分辨。
    default = inspect.signature(ex._http_get_json).parameters["timeout"].default
    assert default == ex._HTTP_TIMEOUT_SEC, (
        f"簽名的預設值 {default} 與 `_HTTP_TIMEOUT_SEC` "
        f"({ex._HTTP_TIMEOUT_SEC}) 不一致——寫死回字面值了？")


# ---------------------------------------------------------------------------
# 驗證工具的結束碼三分（2026-09-20）
# ---------------------------------------------------------------------------
# 原本「全部連不上」是 exit 0：一項都沒驗到，只看結束碼的呼叫端卻讀成全部正常。
# 現在跟 `verify_browser.py` 同一套：0 全部驗過且正常、1 有 FAIL、3 有沒驗到的。

def _r(verdict: str) -> dict:
    return {"group": "g", "what": "w", "verdict": verdict, "detail": ""}


@pytest.mark.parametrize("verdicts, expected", [
    (["OK", "OK"], 0),
    ([], 0),
    # **`SKIP` 同一天稍晚換到「沒驗到」那一邊。** 原本這一格是 0，註解寫「刻意跳過的
    # 不算沒驗到」——可是這支腳本裡沒有任何「刻意跳過」的端點：`SKIP` 的兩個來源都
    # 是 CDN 那一筆拿不到樣本圖。實測 `--only cdn`（`cdn` 自成一組）在拿不到樣本時
    # 印「1 ok, 0 failed, 0 unreachable」、exit 0，正是三分要消滅的那個形狀。
    (["OK", "SKIP"], 3),
    (["SKIP"], 3),
    (["UNREACHABLE", "UNREACHABLE"], 3),       # 離線：一項都沒驗到
    (["OK", "UNREACHABLE"], 3),                # 部分沒驗到也不得回報成功
    (["FAIL", "UNREACHABLE"], 1),              # 真的被拒絕優先於沒驗到
    (["OK", "FAIL"], 1),
])
def test_the_verifier_exit_code_is_three_way(verdicts, expected):
    assert vx._exit_code([_r(v) for v in verdicts]) == expected


def test_the_unverified_exit_code_is_not_argparses_usage_error():
    """argparse 打錯參數時用 2；沒驗到必須跟它分得開，也跟 FAIL（1）分得開。"""
    assert vx.EXIT_UNVERIFIED not in (0, 1, 2)
    assert (vx.EXIT_OK, vx.EXIT_FAIL) == (0, 1)


@pytest.mark.parametrize("as_json", [False, True])
def test_main_reports_unverified_end_to_end_without_touching_the_network(
        monkeypatch, capsys, as_json):
    """從 `main()` 一路進去：結束碼來自 `_exit_code`，JSON 也帶得出同一個數字。

    `_run` 換成合成結果，所以不打任何網路；`argv=[]` 也順便釘住 `main()` 不去讀
    pytest 自己的命令列（那會變成 argparse 的 exit 2）。
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
# 驗證腳本自己那一層：三支檢查函式從來沒有被任何測試執行過（2026-09-20）
# ---------------------------------------------------------------------------
# 覆蓋率量出來的：`verify_external_apis.py` 64%，而缺的 66 行不是散的——`_check_raw`、
# `_check_post`、`_read_body_text`、`_error_body` **整段**，加上 `_run` 的列印與
# `main()` 的兩個分支。也就是「驗證工具自己有沒有在說實話」這件事沒有任何守門。
#
# 下面的替身只長出這幾支真的會碰到的屬性。`aiohttp` 是在函式內 `import` 的，取得的
# 是同一個模組物件，所以換掉 `ex.aiohttp.ClientSession` 對兩邊同時生效。


class _BoomBody:
    """`read()` 會炸的串流。診斷用的讀取不該把驗證本身帶下去。"""

    async def read(self, _n: int = -1) -> bytes:
        raise OSError("stream exploded")


class _VerifierResponse:
    """`aiohttp` 回應的替身。`content_type` 是 `_check_raw` 成功時會印的東西。"""

    def __init__(self, status, *, body=b"", content_type="application/json",
                 payload=None, boom_body=False):
        self.status = status
        self.content_type = content_type
        self._payload = payload
        self.content = _BoomBody() if boom_body else _FakeBody(body)

    async def json(self):
        return self._payload


class _Ctx:
    """`session.get(...)` 回的那個非同步情境管理器。

    佇列裡放**例外實例**就是「這次請求炸掉」——真的 `aiohttp` 也是在 `__aenter__`
    那一刻才拋，所以替身照著同一個契約，而不是照著呼叫端目前剛好怎麼寫。
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
    """記錄用的 `ClientSession` 替身，回應一律從 `responses` 佇列取。"""

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
        assert type(self).responses, f"替身沒有準備給 {method} {url} 的回應"
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
    """把 `_check_json` 底下那條共用的 GET 換掉，回指定的 (status, data)。"""
    async def fake_get(url, *, params=None, headers=None,
                       timeout=ex._HTTP_TIMEOUT_SEC, quiet_statuses=()):
        return status, data
    monkeypatch.setattr(ex, "_http_get_json", fake_get)


# --- 一條規則、三個實作：宣告過的非 200 --------------------------------------

@pytest.mark.parametrize("kind", ["json", "raw", "post"])
def test_every_checker_honours_an_expected_non_200(monkeypatch, verifier_http,
                                                   kind):
    """`ok_statuses` 原本只有 `_check_json` 看得到。

    一個**宣告了但沒有人讀**的鍵沒有任何症狀——跟 `_OWNER_ONLY_SLASH` 裡那個過期
    字串同一個形狀，只是它失敗的方向是狼來了：那一筆會永遠報 FAIL，而報表上看起來
    就像站方真的拒絕了我們。三支現在共用同一個說法，這支測試就是那個「比對」。
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
    assert verdict == "OK", f"{kind}: 宣告過的 404 被報成 {verdict}（{detail}）"
    assert detail == vx._expected_status(404)[1], f"{kind}: {detail!r}"


# --- 一條規則、三個實作：沒有回應時要講出逾時上限 -----------------------------

@pytest.mark.parametrize("kind, timeout", [
    ("json", 30.0), ("raw", 20.0), ("post", 15.0)])
def test_every_checker_names_the_timeout_when_nothing_answers(
        monkeypatch, verifier_http, kind, timeout):
    """「連不上」與「比我們的上限慢」是兩件事，而只有 `_check_json` 學會了。

    2026-09-08 字典那一筆就是後者（端點回 200，只是要 20 秒，而上限 15 秒），當時
    的訊息把人指向網路故障。`_check_raw`／`_check_post` 在 2026-09-20 以前回的是光禿
    禿一個 `TimeoutError`——同一個誤導，而它們那兩筆的上限（20／30 秒）恰恰是最可能
    「活著但比上限慢」的。
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
    assert f"{timeout:g}" in detail, f"{kind}: 沒有把逾時上限印出來：{detail!r}"
    assert "比這個上限慢" in detail, f"{kind}: 只報了連不上：{detail!r}"


@pytest.mark.parametrize("kind", ["raw", "post"])
def test_a_connection_that_never_opened_is_not_blamed_on_slowness(
        verifier_http, kind):
    """近似命中：DNS／被擋是**連線沒成立**，跟「回得慢」要分得開，否則兩種都會被
    導去改逾時。例外的類別名要留著，那是唯一能分辨的線索。"""
    verifier_http.responses.append(OSError("no route to host"))
    entry = _RAW_ENTRY if kind == "raw" else _POST_ENTRY
    checker = vx._check_raw if kind == "raw" else vx._check_post
    verdict, detail = _run(checker(entry))
    assert verdict == "UNREACHABLE"
    assert "OSError" in detail, detail
    assert "連線本身沒有成立" in detail, detail
    assert "比這個上限慢" not in detail, f"把連不上說成回得慢：{detail!r}"


# --- CDN 那一筆：拿不到樣本圖等於沒驗到 ---------------------------------------

@pytest.mark.parametrize("post", [None, {}, {"id": 1, "md5": "x"}])
def test_the_cdn_check_skips_when_there_is_no_sample_image(monkeypatch, post):
    """CDN 沒有固定網址，得先跟 API 要一張現有的圖。要不到就是**沒驗到**。

    這兩個 `SKIP` 是整支腳本裡僅有的兩個，所以「刻意跳過」從來不存在——見
    `test_the_verifier_exit_code_is_three_way` 那張表上換邊的那一格。
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
    """真的去抓那張圖，而且照 large → file → preview 的順序。

    順序不是隨便的：`--grid` 下載走的就是這條，驗證要打的是**同一個**網址。
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


# --- raw 的標頭與失敗路徑 ------------------------------------------------

def test_a_raw_request_always_carries_a_user_agent(verifier_http):
    """沒宣告 UA 的 raw 端點要補上共用的那個——這支腳本的全部價值就是「跟 bot 走
    同一條路」，而 2026-08-30 兩個事故都出在標頭上。"""
    verifier_http.responses.append(_VerifierResponse(200))
    _run(vx._check_raw(_RAW_ENTRY))
    assert verifier_http.calls[0]["headers"]["User-Agent"] == ex._user_agent()


def test_a_raw_entry_keeps_the_user_agent_it_declared(verifier_http):
    """近似命中：iqdb 那一筆要的是瀏覽器風格 UA，補預設值不可以把它蓋掉。"""
    verifier_http.responses.append(_VerifierResponse(200))
    entry = _RAW_ENTRY | {"headers": {"User-Agent": ex._BROWSER_UA}}
    _run(vx._check_raw(entry))
    assert verifier_http.calls[0]["headers"]["User-Agent"] == ex._BROWSER_UA


def test_a_raw_failure_quotes_what_the_upstream_said(verifier_http):
    """403 的原因十之八九寫在主體裡。這條同時走過 `_read_body_text`。"""
    body = json.dumps({"message": "blocked: missing contact"}).encode("utf-8")
    verifier_http.responses.append(_VerifierResponse(403, body=body))
    verdict, detail = _run(vx._check_raw(_RAW_ENTRY))
    assert verdict == "FAIL"
    assert "HTTP 403" in detail
    assert "blocked: missing contact" in detail, detail


def test_reading_a_failure_body_never_takes_the_verification_down(
        verifier_http):
    """主體讀不到就回空字串。診斷是附加價值，不能反過來害整支掃描炸掉。"""
    assert _run(vx._read_body_text(_VerifierResponse(500, boom_body=True))) == ""
    resp = _VerifierResponse(500, body="上游說明".encode("utf-8"))
    assert _run(vx._read_body_text(resp)) == "上游說明"


def test_the_second_request_for_a_reason_is_allowed_to_fail(verifier_http):
    """`_error_body` 是「已經失敗之後再打一次」。它自己失敗時只是少一句原因。"""
    verifier_http.responses.append(OSError("still down"))
    assert _run(vx._error_body("https://example.test/x")) == ""
    verifier_http.responses.append(
        _VerifierResponse(429, body=b'{"error":"slow down"}'))
    assert "slow down" in _run(vx._error_body("https://example.test/x"))


# --- POST 那一筆 ---------------------------------------------------------

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
    """GraphQL 端點回 200 不代表查得到——沒有 `data` 就是壞的。"""
    verifier_http.responses.append(_VerifierResponse(200, payload=payload))
    verdict, _detail = _run(vx._check_post(_POST_ENTRY))
    assert verdict == "FAIL"


def test_a_post_failure_does_not_blame_the_user_agent(verifier_http):
    """走完整條路（不是只測 `_diagnose`）確認 `method="POST"` 真的傳下去了。"""
    verifier_http.responses.append(_VerifierResponse(403, body=b"<html>x"))
    verdict, detail = _run(vx._check_post(_POST_ENTRY))
    assert verdict == "FAIL"
    assert ex._UA_CONTACT_KEY not in detail, detail


# --- 診斷的順序：上游自己說的優先，5xx 也不例外 -------------------------------

def test_the_diagnosis_prefers_the_upstream_even_for_a_5xx():
    """2026-09-20 補的那一格。

    原本 5xx 直接回「52x 通常是前面的 CDN 連不到原站」，於是一個帶著 JSON 說明的
    503（「維護到某日」那種）會被這支工具改寫成一句猜測——正好違反本函式 docstring
    立的規矩。同一個形狀在 403 上已經害過一次（2026-09-07 動畫資料庫）。
    """
    body = json.dumps({"message": "scheduled maintenance until 2026-10-01"})
    out = vx._diagnose(503, body=body)
    assert "scheduled maintenance" in out, f"猜測蓋掉了事實：{out!r}"
    assert "CDN" not in out, out


def test_a_5xx_without_a_readable_reason_still_gets_the_cdn_hint():
    """反向守門：收斂猜測不可以做成「什麼都不說」。CDN 的挑戰頁撈不出 JSON。"""
    out = vx._diagnose(522, body="<html>error code: 522</html>")
    assert "CDN" in out and "上游" in out, out


# --- `_run` 的挑選與列印 --------------------------------------------------

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
    """`--json` 以外的那條路：一行一筆，非 OK 的多印一行原因。

    `monkeypatch.setattr(vx, "_QUIET", False)` 不只是設值——`main()` 會用 `global`
    把它寫成 True 並留在那裡，所以這裡同時是在把那個汙染關起來（teardown 會還原）。
    """
    monkeypatch.setattr(vx, "_QUIET", False)
    _record_checks(monkeypatch, verdict="FAIL")
    _run(vx._run(["xkcd"]))
    out = capsys.readouterr().out
    assert out.count("\n") == 2, out
    assert "xkcd" in out and "detail" in out


def test_every_verdict_the_checkers_can_return_is_registered():
    """判定字串散在三支檢查函式的 `return` 裡，兩份登記都得對得上。

    忘了登記的後果分兩半：`_run` 會在掃到一半時 `KeyError`（吵，但至少看得見），
    而 `_exit_code` 那半是**安靜**的——沒被列進 `_UNVERIFIED_VERDICTS` 的新判定會
    自動算成「驗過而且正常」，也就是這一整天在修的那個形狀。
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
    assert tuples >= 8, f"判定掃描沒抓到東西（{tuples}）——空集合看起來跟乾淨一樣"
    assert returned == set(vx._VERDICT_MARKS), (
        f"判定與顯示登記對不上：{returned ^ set(vx._VERDICT_MARKS)}")
    classified = vx._UNVERIFIED_VERDICTS | {"OK", "FAIL"}
    assert returned == classified, (
        f"有判定沒有被結束碼分類：{returned ^ classified}")


# --- main() 的統計 -------------------------------------------------------

def test_main_counts_every_result_exactly_once(monkeypatch, capsys):
    """四個數字要**加得起來**。

    原本「ok」是減出來的（總數 − failed − unreachable），所以 `SKIP` 被算進 ok，
    而任何未來新增的判定也會自動被算成好的。減法把沒列到的東西默默歸成正常。
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
    """沒設聯絡方式時，開頭那行要指名道姓——403 光看狀態碼查不出來。"""
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
    """HTTP 200 但內容解不開＝那個端點對我們而言是壞的，不是 OK。

    這條的價值在 `_http_get_json` 的契約：它解析失敗時回的是 `(200, None)`，而
    「狀態碼漂亮」正是最容易被當成健康的一種壞法。
    """
    _json_answer(monkeypatch, 200, None)
    verdict, detail = _run(vx._check_json(_JSON_ENTRY))
    assert verdict == "FAIL"
    assert "不是 JSON" in detail


def test_a_failure_page_bigger_than_the_cap_leaves_no_reason_but_no_crash():
    """失敗頁也有上限——主體是第三方送來的，錯誤處理路徑上不能有無上限的讀取。

    超過上限時 `read_capped_body` 回 `None`，診斷就少一句原因；**不可以**變成
    例外，也不可以把 `None` 當成字串往下丟。
    """
    oversize = b"x" * (vx._DIAG_BODY_CAP + 1)
    resp = _VerifierResponse(503, body=oversize)
    assert _run(vx._read_body_text(resp)) == ""


def test_everything_unreachable_says_it_is_probably_the_network(monkeypatch,
                                                                capsys):
    """一整片連不上通常是這一端沒網路，別讓人去查站方是不是封鎖了我們。"""
    async def fake_run(_only):
        return [_r("UNREACHABLE"), _r("UNREACHABLE")]

    monkeypatch.setattr(vx, "_run", fake_run)
    monkeypatch.setattr(vx, "_QUIET", False)
    monkeypatch.setattr(ex, "_configured_contact", lambda: "x")
    assert vx.main([]) == vx.EXIT_UNVERIFIED
    out = capsys.readouterr().out
    assert "沒有網路" in out, out
    assert "0 ok, 0 failed, 2 unreachable, 0 skipped" in out, out


def test_the_summary_repeats_every_failure_at_the_bottom(monkeypatch,
                                                        capsys):
    """掃描有 22 筆，逐筆那行會被捲走；結尾必須把 FAIL 連同原因再列一次。

    只有 `--json` 被測過的話，這段文字輸出永遠不會執行——而人讀的是這一段。
    """
    async def fake_run(_only):
        bad = _r("FAIL") | {"group": "wiki", "what": "百科摘要",
                            "detail": "HTTP 403  ← 上游自己說：no contact"}
        return [_r("OK"), bad]

    monkeypatch.setattr(vx, "_run", fake_run)
    monkeypatch.setattr(vx, "_QUIET", False)
    monkeypatch.setattr(ex, "_configured_contact", lambda: "x")
    assert vx.main([]) == vx.EXIT_FAIL
    out = capsys.readouterr().out
    assert "FAIL  wiki" in out, out
    assert "no contact" in out, f"結尾只報了標題、沒有原因：{out!r}"
    assert "1 ok, 1 failed, 0 unreachable, 0 skipped" in out, out


# ---------------------------------------------------------------------------
# 兩個只抽一張的圖庫：隨機排序的 meta tag 與「從回應裡挑一張」
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
    """站方出錯或改結構時會回夾著非物件的 list。呼叫端一律 `.get(...)`，所以讀進來的時候
    就要濾掉——漏一個的話，那個指令只剩一句泛用的失敗。

    抽一張的那幾支用 `random.choice`：固定成「挑第一個」、並把垃圾排在前面，沒濾的版本就
    **一定**挑到垃圾——不固定的話，它有五分之一的機會剛好挑到真的那筆而照樣通過。"""
    monkeypatch.setattr(ex.random, "choice", lambda seq: seq[0])
    fetch, wrapped, shape = _POST_READERS[name]
    real = {"id": 987_654_321, "tag_string_general": "a b"}
    body = [None, "junk", 5, ["nested"], real]
    fake_http.replies = [(200, {"posts": body} if wrapped else body)] * 3
    got = _run(fetch())
    assert got == (real if shape == "one" else [real]), got


def test_the_post_reader_table_covers_every_list_reading_fetcher():
    """`_POST_READERS` 少列一個讀 list 的抓取函式，上一支就看不到它。對著 `_FETCHERS` 對帳：
    只有 e621 的 tag 查詢不在這裡——它與 danbooru_tags 共用 `_query_tags_json`。"""
    assert set(_FETCHERS) - set(_POST_READERS) == {"e621_tags"}


@pytest.mark.parametrize("fetch, meta, wrap", [
    (ex._fetch_safebooru_post, "sort:", lambda posts: posts),
    (ex._fetch_e621_post, "order:", lambda posts: {"posts": posts}),
])
def test_a_random_post_fetch_asks_for_random_order_and_returns_one_post(
        fetch, meta, wrap, fake_http):
    """沒有這個 meta tag，兩個站都照「最新」排序，同一組標籤每次抽到的都是同一批。
    使用者自己寫了排序就照他的，不再疊一個——兩個排序同時出現時站方只認其中一個。"""
    fake_http.replies = [(200, wrap([{"id": 7}]))]
    assert _run(fetch("cat_ears")) == {"id": 7}
    assert fake_http.calls[-1]["params"]["tags"] == f"cat_ears {meta}random"

    fake_http.replies = [(200, wrap([{"id": 8}]))]
    _run(fetch(f"cat_ears {meta}score"))
    assert fake_http.calls[-1]["params"]["tags"] == f"cat_ears {meta}score"

    fake_http.replies = [(200, wrap([]))]
    assert _run(fetch("")) is None
    assert fake_http.calls[-1]["params"]["tags"] == f"{meta}random"
