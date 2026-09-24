"""api 後端的時間上限：`Retry-After` 夾子 ＋ 本專案自己的外框（2026-09-19）。

**為什麼要有這一份。** anthropic 1.6.0 拿掉了 SDK 內建重試「`0 < retry_after <= 60`
才照伺服器的 `Retry-After` 睡」那道上限（改成最多 4,294,967 秒）。本機假伺服器實測
429 ＋ `retry-after: 3600`：1.4.0 是 0.4／0.8 秒各重試一次、1.3 秒丟 `RateLimitError`；
1.7.0 是每次重試前睡 3600 秒，一輪卡約 2 小時。fresh clone 今天拿到的就是 1.7.0，而
本機兩個直譯器還在 1.4.0——所以這個缺陷在這台機器上**看不到**，只有在另一台機器或下一次
升級時才會出現。

修法是兩層（說明在 `dorossi_backend.DOROSSI_API_TIMEOUT_SEC` 上方）：
  1. http client 的 response hook（`_dorossi_api_clamp_retry_after`）：SDK 會睡超過
     60 秒時補 `x-should-retry: false`，SDK 就不自己重試；
  2. `_dorossi_via_api` 外面一層 `asyncio.timeout`（`_dorossi_api_call_ceiling_sec`）。

**本檔刻意可以在不同的 SDK 版本上跑**（只 import `dorossi_backend` 與標準函式庫），
2026-09-19 在本機 1.4.0 與一個 scratch venv 裡的 1.7.0 各跑過一次。假伺服器那一段在
**兩個版本上都分得出有沒有 hook**：1.4.0 沒有 hook 是 3 次請求（照舊的短退避），
1.7.0 沒有 hook 是卡住——所以每一支都包了 `asyncio.wait_for`，回歸時要**變紅**，
不能變成掛住。

三個寫法上的要點：
  * 必須放行的案例（短等待照舊重試、沒帶等待的 529 照舊重試）與必須攔下的案例一樣
    重要——只測攔下的話，「一律不重試」也會全綠。
  * 對照 SDK 自己的 `_parse_retry_after_header`（私有）用同一份語料問兩邊，而不是兩邊
    各自寫測試：兩份實作各自全綠，仍然沒有東西在比較它們。
  * 假伺服器那幾支的正面對照是「請求真的打到假伺服器」（`hits >= 1`）——少了它，一個
    設定錯了、根本沒送出請求的測試會跟「只送了一次」長得一樣。
"""
from __future__ import annotations

import asyncio
import email.utils
import http.server
import inspect
import math
import os
import sys
import threading
import time
import types

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import dorossi_backend as db  # noqa: E402

_HAS_SDK = db.anthropic is not None
_needs_sdk = pytest.mark.skipif(not _HAS_SDK, reason="anthropic SDK 未安裝（可選相依）")


class _Headers(dict):
    """不分大小寫的標頭（真的 http 標頭就是這樣），夠 hook 與解析器用。"""

    def __init__(self, items=None):
        super().__init__()
        for key, value in (items or {}).items():
            self[key] = value

    def __setitem__(self, key, value):
        super().__setitem__(str(key).lower(), value)

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


def _http_date(offset_sec: float, now: float | None = None) -> str:
    return email.utils.formatdate((time.time() if now is None else now) + offset_sec,
                                  usegmt=True)


# ---------------------------------------------------------------------------
# 一、解析器：`_dorossi_sdk_retry_wait_sec` 回答「SDK 會睡多久」
# ---------------------------------------------------------------------------

def test_seconds_are_read_as_seconds():
    assert db._dorossi_sdk_retry_wait_sec(_Headers({"retry-after": "3600"})) == 3600.0
    assert db._dorossi_sdk_retry_wait_sec(_Headers({"retry-after": " 42 "})) == 42.0
    assert db._dorossi_sdk_retry_wait_sec(_Headers({"retry-after": "1.5"})) == 1.5


def test_milliseconds_win_over_seconds_like_the_sdk():
    """SDK 先看 `retry-after-ms`，轉得成數字就**不再看** `retry-after`。"""
    got = db._dorossi_sdk_retry_wait_sec(
        _Headers({"retry-after-ms": "5000", "retry-after": "3600"}))
    assert got == 5.0


def test_unparsable_milliseconds_fall_through_to_seconds():
    got = db._dorossi_sdk_retry_wait_sec(
        _Headers({"retry-after-ms": "soon", "retry-after": "120"}))
    assert got == 120.0


def test_an_http_date_becomes_seconds_from_now():
    now = 1_790_000_000.0
    far = db._dorossi_sdk_retry_wait_sec(
        _Headers({"retry-after": _http_date(3600, now)}), now=now)
    assert far == pytest.approx(3600.0, abs=1.0)
    past = db._dorossi_sdk_retry_wait_sec(
        _Headers({"retry-after": _http_date(-100, now)}), now=now)
    assert past == pytest.approx(-100.0, abs=1.0)


@pytest.mark.parametrize("headers", [
    None, _Headers(), _Headers({"retry-after": ""}), _Headers({"retry-after": "soon"}),
])
def test_no_usable_header_reads_as_none(headers):
    assert db._dorossi_sdk_retry_wait_sec(headers) is None


def test_the_parser_never_raises():
    class _Hostile:
        def get(self, *_a, **_k):
            raise RuntimeError("boom")

    assert db._dorossi_sdk_retry_wait_sec(_Hostile()) is None


# ---------------------------------------------------------------------------
# 二、hook：只在「SDK 會睡超過 60 秒」時叫它不要重試
# ---------------------------------------------------------------------------

def _run_hook(status, headers):
    resp = types.SimpleNamespace(status_code=status, headers=_Headers(headers))
    asyncio.run(db._dorossi_api_clamp_retry_after(resp))
    return resp


def _clamped(status, headers) -> bool:
    return _run_hook(status, headers).headers.get("x-should-retry") == "false"


@pytest.mark.parametrize("status,headers", [
    (429, {"retry-after": "3600"}),
    (429, {"retry-after": "61"}),
    (429, {"retry-after": "60.5"}),
    (429, {"retry-after": "inf"}),               # 1.7.0 會睡 4,294,967 秒
    (429, {"retry-after-ms": "3600000"}),
    (429, {"retry-after-ms": "90000", "retry-after": "5"}),   # 毫秒優先 → 90 秒
    (429, {"retry-after": "3600", "x-should-retry": "true"}),  # 伺服器說要重試也蓋掉
    (529, {"retry-after": "3600"}),              # 不限 429：任何會被重試的狀態都會睡
    (503, {"retry-after": "3600"}),
    (408, {"retry-after": "120"}),
])
def test_a_long_server_requested_wait_stops_the_sdk_retry(status, headers):
    assert _clamped(status, headers), (status, headers)


def test_a_far_http_date_stops_the_sdk_retry():
    """2026-09-19 實測：只解析秒數的 hook 對 HTTP 日期格式在 1.7.0 上照樣卡住。"""
    assert _clamped(429, {"retry-after": _http_date(3600)})


@pytest.mark.parametrize("status,headers", [
    (429, {"retry-after": "60"}),                # 邊界：舊 SDK 照辦的就是 <= 60
    (429, {"retry-after": "5"}),
    (429, {"retry-after-ms": "5000", "retry-after": "3600"}),  # 毫秒優先 → 5 秒
    (429, {"retry-after": "soon"}),
    (429, {}),
    (429, {"retry-after": "nan"}),               # SDK 的 `> 0` 對 nan 是假 → 退避
    (429, {"retry-after": "-5"}),
    (529, {}),                                   # 沒帶等待的過載照舊由 SDK 重試
    (200, {"retry-after": "3600"}),              # 成功的回應不碰
])
def test_everything_else_is_left_to_the_sdk(status, headers):
    """必須放行的一半：少了這一半，「一律不重試」也會全綠。"""
    assert not _clamped(status, headers), (status, headers)


@pytest.mark.parametrize("offset", [5, -100])
def test_a_near_or_past_http_date_is_left_to_the_sdk(offset):
    assert not _clamped(429, {"retry-after": _http_date(offset)})


def test_the_hook_leaves_retry_after_intact():
    """`_dorossi_api_retry_after_sec` 之後要從例外上讀它來算 `reset_at`。"""
    resp = _run_hook(429, {"retry-after": "3600"})
    assert resp.headers.get("retry-after") == "3600"


def test_the_hook_is_async_and_never_raises(capsys):
    """`AsyncClient` 會 await 每一個 hook；hook 丟例外會把一個正常的回應換成例外。"""
    assert inspect.iscoroutinefunction(db._dorossi_api_clamp_retry_after)

    class _Hostile:
        def get(self, *_a, **_k):
            raise RuntimeError("boom")

        def __setitem__(self, *_a):
            raise RuntimeError("boom")

    for resp in (types.SimpleNamespace(status_code=429, headers=_Hostile()),
                 types.SimpleNamespace(status_code=429),
                 types.SimpleNamespace(status_code="429", headers=_Headers()),
                 object()):
        asyncio.run(db._dorossi_api_clamp_retry_after(resp))
    # 敵意標頭那一格是解析器自己吞掉的；沒有 headers 屬性那一格要由 hook 自己接住，
    # 而且 stderr 只記型別名（這行會進 log，而 log 有對外出口）。
    err = capsys.readouterr().err
    assert "clamp skipped (AttributeError)" in err, err
    assert "boom" not in err, err


# ---------------------------------------------------------------------------
# 三、跟 SDK 自己的解析器對帳（同一份語料問兩邊）
# ---------------------------------------------------------------------------

def _parity_corpus():
    return [
        {}, {"retry-after": "3600"}, {"retry-after": "5"}, {"retry-after": "1.5"},
        {"retry-after": " 42 "}, {"retry-after": "-5"}, {"retry-after": "0"},
        {"retry-after": "nan"}, {"retry-after": "inf"}, {"retry-after": "soon"},
        {"retry-after": ""}, {"retry-after-ms": "5000"},
        {"retry-after-ms": "5000", "retry-after": "3600"},
        {"retry-after-ms": "soon", "retry-after": "120"},
        {"retry-after-ms": "nan", "retry-after": "120"},
        {"retry-after": _http_date(3600)}, {"retry-after": _http_date(5)},
        {"retry-after": _http_date(-100)},
        {"retry-after": "Sat, 19 Sep 2026 10:00:00"},   # 沒有時區
    ]


def _branch_of(headers: dict) -> str:
    try:
        float(headers.get("retry-after-ms"))
        return "ms"
    except (TypeError, ValueError):
        pass
    try:
        float(headers.get("retry-after"))
        return "seconds"
    except (TypeError, ValueError):
        pass
    if email.utils.parsedate_tz(headers.get("retry-after")) is not None:
        return "date"
    return "none"


def test_the_parity_corpus_reaches_every_branch():
    """正面對照：語料只踩到秒數那一支的話，下一支的對帳等於只驗了一半。"""
    branches = [_branch_of(h) for h in _parity_corpus()]
    for branch in ("ms", "seconds", "date", "none"):
        assert branches.count(branch) >= 1, (branch, branches)


@_needs_sdk
def test_the_parser_answers_like_the_installed_sdk():
    """`_dorossi_sdk_retry_wait_sec` 是照抄 SDK 私有的 `_parse_retry_after_header`。
    SDK 哪天改了它（例如多認一個標頭），hook 的判斷就會跟 SDK 真正的行為分岔，而兩邊
    各自的測試都還是綠的。私有方法不見了就讓這支紅，那正是該重新對一次的時候。"""
    from anthropic import _base_client as base

    parse = getattr(base.BaseClient, "_parse_retry_after_header", None)
    assert parse is not None, (
        f"anthropic {db.anthropic.__version__} 沒有 `_parse_retry_after_header` 了——"
        "去讀新版 `_calculate_retry_timeout` 怎麼決定睡多久，再改 "
        "`_dorossi_sdk_retry_wait_sec`。")
    headers_cls = base.httpx2.Headers
    mismatches = []
    for raw in _parity_corpus():
        sdk = parse(None, headers_cls(raw))
        ours = db._dorossi_sdk_retry_wait_sec(headers_cls(raw))
        if sdk is None or ours is None:
            same = sdk is None and ours is None
        elif math.isnan(sdk) or math.isnan(ours):
            same = math.isnan(sdk) and math.isnan(ours)
        else:
            same = sdk == pytest.approx(ours, abs=2.0)
        if not same:
            mismatches.append((raw, sdk, ours))
    assert not mismatches, mismatches


# ---------------------------------------------------------------------------
# 四、client 真的帶著那個 hook；裝不上時照樣建得起來
# ---------------------------------------------------------------------------

@_needs_sdk
def test_the_client_is_built_with_the_clamp_installed(monkeypatch):
    recorded = {}

    class _Recorder:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

    monkeypatch.setattr(db, "AsyncAnthropic", _Recorder)
    monkeypatch.setattr(db, "_dorossi_client", None)
    assert db._get_dorossi_client() is not None
    assert recorded.get("timeout") == db.DOROSSI_API_TIMEOUT_SEC
    assert recorded.get("max_retries") == db.DOROSSI_API_MAX_RETRIES
    http_client = recorded.get("http_client")
    assert http_client is not None, (
        f"`AsyncAnthropic()` 沒有拿到帶 hook 的 http client：{sorted(recorded)}")
    try:
        hooks = http_client.event_hooks.get("response", [])
        assert db._dorossi_api_clamp_retry_after in hooks, hooks
    finally:
        asyncio.run(http_client.aclose())


def test_a_clamp_that_cannot_be_installed_does_not_take_the_backend_down(
        monkeypatch, capsys):
    """夾子是準確度的改善；它裝不上時照樣建 client（外框仍守著最壞時間），stderr 講一句。"""
    recorded = {}

    class _Recorder:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

    def _boom():
        raise AttributeError("no factory")

    monkeypatch.setattr(db, "AsyncAnthropic", _Recorder)
    monkeypatch.setattr(db, "_dorossi_client", None)
    monkeypatch.setattr(db, "_dorossi_api_http_client", _boom)
    assert db._get_dorossi_client() is not None
    assert "http_client" not in recorded
    assert recorded.get("timeout") == db.DOROSSI_API_TIMEOUT_SEC
    err = capsys.readouterr().err
    assert "clamp could not be installed" in err and "AttributeError" in err, err


def test_the_client_is_built_once_and_reused(monkeypatch):
    """client 帶著連線池；每一輪都新建一個，舊的那個連同它的連線就沒人關，一個跑整夜的
    自走任務會一路累積。`_http_client` 換成 None，這支不需要真的 SDK。"""
    built: list = []

    class _Recorder:
        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(db, "AsyncAnthropic", _Recorder)
    monkeypatch.setattr(db, "_dorossi_client", None)
    monkeypatch.setattr(db, "_dorossi_api_http_client", lambda: None)
    first = db._get_dorossi_client()
    assert first is not None and db._get_dorossi_client() is first
    assert len(built) == 1, built


def test_without_the_sdk_the_api_backend_refuses_before_sending_anything(monkeypatch, capsys):
    """SDK 沒裝（它是可選相依）：拿不到 client，這一輪以一個具名的錯誤結束，不去組請求。

    「沒裝」是設定狀態，不是建構失敗：拿 `None` 去呼叫也會落進建構那層的寬 except、同樣回 None，
    差別只在每一輪都多印一行誤導的「client init failed」——所以連 stderr 一起看。"""
    monkeypatch.setattr(db, "AsyncAnthropic", None)
    monkeypatch.setattr(db, "_dorossi_client", None)
    assert db._get_dorossi_client() is None
    with pytest.raises(RuntimeError, match="client unavailable"):
        asyncio.run(db._dorossi_via_api("hi", []))
    assert "init failed" not in capsys.readouterr().err


def test_without_the_sdk_factory_the_client_is_plain(monkeypatch):
    monkeypatch.setattr(db, "anthropic", types.SimpleNamespace())
    assert db._dorossi_api_http_client() is None


# ---------------------------------------------------------------------------
# 五、外框：本專案自己強制的最壞時間
# ---------------------------------------------------------------------------

def test_the_ceiling_is_derived_from_the_bound_constants(monkeypatch):
    """預設 600 × 3 ＋ 60 × 2 ＋ 30。在呼叫時才讀常數（改常數要立刻生效）。"""
    monkeypatch.setattr(db, "DOROSSI_API_TIMEOUT_SEC", 600.0)
    monkeypatch.setattr(db, "DOROSSI_API_MAX_RETRIES", 2)
    assert db._dorossi_api_call_ceiling_sec() == 1950.0
    monkeypatch.setattr(db, "DOROSSI_API_TIMEOUT_SEC", 10.0)
    monkeypatch.setattr(db, "DOROSSI_API_MAX_RETRIES", 0)
    assert db._dorossi_api_call_ceiling_sec() == 40.0


def test_the_ceiling_covers_the_sdk_retry_sleeps():
    """外框至少要容得下「每次請求都用滿逾時 ＋ 每次重試前都睡滿夾子允許的秒數」，
    否則它會在一個照規矩走的回合上提早開火。"""
    worst = (db.DOROSSI_API_TIMEOUT_SEC * (1 + db.DOROSSI_API_MAX_RETRIES)
             + db.DOROSSI_API_MAX_RETRIES * db._DOROSSI_API_SDK_SLEEP_CAP_SEC)
    assert db._dorossi_api_call_ceiling_sec() > worst


class _FakeMessages:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    async def create(self, **_kwargs):
        return await self._behaviour()


def _fake_client(behaviour):
    return types.SimpleNamespace(messages=_FakeMessages(behaviour))


def test_a_call_that_outlives_the_ceiling_fails_generically(monkeypatch):
    """外框到了 → `TimeoutError`（既有的泛用失敗路徑），訊息不含「unavailable」
    「api_key」這類會被 bot 讀成「沒有憑證」的字。"""
    async def _forever():
        await asyncio.sleep(3600)

    monkeypatch.setattr(db, "_get_dorossi_client", lambda: _fake_client(_forever))
    monkeypatch.setattr(db, "_dorossi_api_call_ceiling_sec", lambda: 0.2)

    async def _go():
        return await asyncio.wait_for(db._dorossi_via_api("hi", []), 10)

    t0 = time.monotonic()
    with pytest.raises(TimeoutError) as got:
        asyncio.run(_go())
    assert time.monotonic() - t0 < 5, "外框沒有準時開火"
    text = str(got.value).lower()
    assert "ceiling" in text, text
    for word in ("unavailable", "api_key", "auth_token"):
        assert word not in text, word
    assert not isinstance(got.value, (db._DorossiUsageLimitError,
                                      db._DorossiTransientError))


def test_a_timeout_raised_inside_the_sdk_is_not_blamed_on_the_ceiling(monkeypatch):
    """近似案例：SDK 裡面自己丟出一個 `TimeoutError`（外框還遠遠沒到）→ 照舊原樣
    往上丟，不可以被說成「超過外框」。`bound.expired()` 就是為了分這兩種。"""
    async def _inner_timeout():
        raise TimeoutError("inner")

    monkeypatch.setattr(db, "_get_dorossi_client", lambda: _fake_client(_inner_timeout))
    with pytest.raises(TimeoutError) as got:
        asyncio.run(db._dorossi_via_api("hi", []))
    assert str(got.value) == "inner"


def test_a_fast_answer_is_untouched_by_the_ceiling(monkeypatch):
    async def _answer():
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="好的")])

    monkeypatch.setattr(db, "_get_dorossi_client", lambda: _fake_client(_answer))
    answer, history = asyncio.run(db._dorossi_via_api("hi", []))
    assert answer == "好的"
    assert history[-1] == {"role": "assistant", "content": "好的"}


# ---------------------------------------------------------------------------
# 六、真的 SDK 對上本機假伺服器（不打任何外部服務、用假金鑰）
# ---------------------------------------------------------------------------

class _MockApi:
    """每一個請求都回同一個狀態碼與標頭；記下每一次請求的時刻。`delay` > 0 時先
    等（最多 delay 秒，收尾時會被叫醒），用來量逾時。"""

    def __init__(self, status, headers=None, delay=0.0):
        self.status = status
        self.headers = dict(headers or {})
        self.delay = delay
        self.hits = []
        self.release = threading.Event()
        api = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - 標準函式庫的命名
                self.rfile.read(int(self.headers.get("content-length", 0) or 0))
                api.hits.append(time.monotonic())
                if api.delay:
                    api.release.wait(api.delay)
                kind = "rate_limit_error" if api.status == 429 else "overloaded_error"
                body = ('{"type":"error","error":{"type":"%s","message":"probe"}}'
                        % kind).encode("utf-8")
                try:
                    self.send_response(api.status)
                    self.send_header("content-type", "application/json")
                    for key, value in api.headers.items():
                        self.send_header(key, value)
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # 客戶端已經放棄（逾時那幾支），不必回

            def log_message(self, *_a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def mock_api(monkeypatch):
    """回一個工廠；client 走**正式的** `_get_dorossi_client`，只用環境變數把它指到
    假伺服器（所以 hook 有沒有裝上，量到的就是正式那一條路）。"""
    servers = []
    for key in ("ANTHROPIC_AUTH_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "probe-not-a-real-key")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(db, "_dorossi_client", None)

    def _make(status, headers=None, delay=0.0):
        api = _MockApi(status, headers, delay)
        servers.append(api)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", api.url)
        return api

    yield _make
    for api in servers:
        api.close()


def _call_api(bound_sec=30.0):
    """跑一次正式的 `_dorossi_via_api`，外面再包一層**測試自己的**上限：回歸的形狀是
    「SDK 睡一小時」，沒有這層的話測試會掛住而不是變紅。"""
    async def _go():
        try:
            return await asyncio.wait_for(db._dorossi_via_api("hi", []), bound_sec)
        finally:
            cli = db._dorossi_client
            if cli is not None:
                await cli.close()

    t0 = time.monotonic()
    try:
        result = asyncio.run(_go())
    except asyncio.TimeoutError as exc:
        if "ceiling" in str(exc):
            raise
        pytest.fail(f"{bound_sec:.0f} 秒內沒有結束——SDK 在照伺服器的 Retry-After 睡"
                    "（hook 沒裝上，或 SDK 不再看 x-should-retry）。")
    finally:
        _call_api.elapsed = time.monotonic() - t0
    return result


def _expect(exc_type, **kw):
    with pytest.raises(exc_type) as got:
        _call_api(**kw)
    return got.value


@_needs_sdk
def test_a_long_retry_after_on_a_429_is_one_request_and_a_prompt_usage_limit(mock_api):
    """核心那一支：429 ＋ `retry-after: 3600` → 只送 1 次、立刻變成用量上限，而且
    `reset_at` 是「現在 ＋ 3600」，不是睡完兩小時之後才算。"""
    api = mock_api(429, {"retry-after": "3600"})
    before = time.time()
    exc = _expect(db._DorossiUsageLimitError)
    assert len(api.hits) == 1, (
        f"送了 {len(api.hits)} 次（anthropic {db.anthropic.__version__}）——"
        "hook 沒有叫 SDK 停手。")
    assert _call_api.elapsed < 10
    assert exc.reset_at == pytest.approx(before + 3600, abs=30)


@_needs_sdk
@pytest.mark.parametrize("headers", [
    {"retry-after-ms": "3600000"},
    "http-date",
], ids=["retry-after-ms", "http-date"])
def test_every_way_of_asking_for_a_long_wait_is_one_request(mock_api, headers):
    """SDK 自己三種寫法都讀。2026-09-19 的第一版 hook 只解析秒數，HTTP 日期在 1.7.0
    上照樣卡住——這一格就是那個回歸。"""
    if headers == "http-date":
        headers = {"retry-after": _http_date(3600)}
    api = mock_api(429, headers)
    _expect(db._DorossiUsageLimitError)
    assert len(api.hits) == 1, (headers, len(api.hits))


@_needs_sdk
def test_a_short_retry_after_is_still_retried_by_the_sdk(mock_api):
    """必須放行：短的等待照舊由 SDK 重試（1 ＋ `DOROSSI_API_MAX_RETRIES` 次）。
    少了這支，「一律不重試」會全綠。"""
    api = mock_api(429, {"retry-after": "0.2"})
    _expect(db._DorossiUsageLimitError)
    assert len(api.hits) == 1 + db.DOROSSI_API_MAX_RETRIES, len(api.hits)


@_needs_sdk
def test_an_overload_without_a_wait_is_still_retried_by_the_sdk(mock_api):
    api = mock_api(529)
    exc = _expect(db._DorossiTransientError)
    assert exc.status == 529
    assert len(api.hits) == 1 + db.DOROSSI_API_MAX_RETRIES, len(api.hits)


@_needs_sdk
def test_an_overload_with_a_long_wait_is_one_request(mock_api):
    """不限 429：1.6.0 起任何會被重試的狀態都會照 `Retry-After` 睡滿。"""
    api = mock_api(529, {"retry-after": "3600"})
    _expect(db._DorossiTransientError)
    assert len(api.hits) == 1, len(api.hits)


@_needs_sdk
def test_the_request_timeout_still_applies_with_our_http_client(mock_api, monkeypatch):
    """換成自己的 http client 之後，`timeout=` 仍然管得到每一次請求（SDK 是逐請求帶
    逾時的，不是讀 client 的預設）。量法：假伺服器拖 20 秒，逾時設 1 秒、不重試。"""
    monkeypatch.setattr(db, "DOROSSI_API_TIMEOUT_SEC", 1.0)
    monkeypatch.setattr(db, "DOROSSI_API_MAX_RETRIES", 0)
    api = mock_api(429, delay=20.0)
    exc = _expect(Exception)
    assert isinstance(exc, db.anthropic.APITimeoutError), repr(exc)
    assert len(api.hits) == 1
    assert _call_api.elapsed < 8, _call_api.elapsed


@_needs_sdk
def test_the_outer_ceiling_fires_on_a_real_client(mock_api, monkeypatch):
    """外框對真的 SDK 呼叫一樣有效（不只對假的 `messages.create`）。"""
    monkeypatch.setattr(db, "_dorossi_api_call_ceiling_sec", lambda: 0.5)
    api = mock_api(429, delay=20.0)
    exc = _expect(TimeoutError)
    assert "ceiling" in str(exc)
    assert len(api.hits) >= 1
    assert _call_api.elapsed < 8, _call_api.elapsed
