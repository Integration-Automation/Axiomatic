"""傳給 selenium 的關鍵字必須是它**真的收得下**的。

`selenium.webdriver.chrome.service.Service.__init__` 收 `**kwargs`，一路轉給
`ChromiumService` 再轉給 `common.service.Service`，而最底層那一個把剩下的
`**kwargs` **直接丟掉**——不警告、不報錯。所以打錯一個參數名，或 selenium 改名
了某個參數，程式照跑，功能靜靜地消失。

實際發生過（2026-08-29 查出）：`build_stealth_driver` 一直傳
`ChromeService(log_path=..., service_args=["--verbose"])`（**當時**是 `--verbose`，後來因為量過大小改成 `--log-level=INFO`，見下面那支測試）。`log_path` 從來就不是
selenium 的參數（4.41／4.44 皆然），於是掉進 `**kwargs` 被丟掉，`log_output`
維持 `None` → `DEVNULL`。`--verbose` 照樣有傳，chromedriver 照樣在產生詳細記錄，
然後整份寫進 DEVNULL——`chromedriver.log` **從來沒被建立過**。而它存在的唯一理由
就是「spawn 失敗時去看最後幾行」：2026-08-25 11:38:50 真的失敗過一次
（`SessionNotCreatedException()`，訊息是空的），log 裡一行 tail 都沒有。

這支測試把「呼叫端的關鍵字」對「**當前裝著的** selenium 的簽名」比對，所以它同時
擋兩個方向：本專案打錯字，以及 selenium 哪天把參數改名。同一個手法在
`test_gui_facade.py` 已經用過一次（AST 掃原始碼 vs 已安裝的套件）。
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest
from selenium.webdriver.chrome.service import Service as ChromeService

REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG = REPO_ROOT / "axiomatic"
_WEBRUNNER = _PKG / "webrunner_novelai.py"

# **兩個變體都自己建 `ChromeService`**（je 2026-09-09 接上——在那之前它一個
# chromedriver 記錄檔都沒有，而 `/run` 的預設變體就是 je）。所以每一支 AST 掃描
# 都要對兩份跑：只掃 selenium 變體的話，je 那一側把 `log_output=` 打成 `log_path=`
# 會完全沒有人說話，也就是把本檔開頭那個踩了幾個月的坑換一個變體原封不動重演。
# 對應的 spawn 進入點名字不同，所以連函式名一起列。
_SPAWN_FUNCTION = {
    "webrunner_novelai.py": "build_stealth_driver",
    "webrunner_je_only.py": "start_driver",
}
_VARIANTS = tuple(_SPAWN_FUNCTION)


def _accepted_keywords(func) -> set[str]:
    """整條 `super().__init__` 鏈上真的具名收下的關鍵字。

    只看最外層的簽名不夠：`ChromeService.__init__` 的具名參數只有五個，其餘靠
    `**kwargs` 往下轉，而真正處理 `driver_path_env_key` / `popen_kw` 的是更底層。
    被 `**kwargs` 收走**又沒有人具名接**的才是會被丟掉的那些。
    """
    names: set[str] = set()
    for klass in type(func).__mro__ if not inspect.isclass(func) else func.__mro__:
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        for name, param in inspect.signature(init).parameters.items():
            if param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
                names.add(name)
    names.discard("self")
    return names


def _chrome_service_call_keywords(variant: str) -> list[tuple[int, set[str]]]:
    """該變體原始碼裡每一處 `ChromeService(...)` 用到的關鍵字。"""
    path = _PKG / variant
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in ("ChromeService", "Service") and any(
                kw.arg for kw in node.keywords):
            out.append((node.lineno, {kw.arg for kw in node.keywords if kw.arg}))
    return out


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_driver_service_is_built_with_keywords_selenium_accepts(variant):
    """`ChromeService(...)` 的每個關鍵字都要對得上裝著的 selenium 的具名參數。

    對不上就代表它會被 `**kwargs` 吃掉然後丟棄——功能靜靜地消失，沒有任何錯誤。
    """
    accepted = _accepted_keywords(ChromeService)
    calls = _chrome_service_call_keywords(variant)
    assert calls, (f"`{variant}` 裡找不到帶關鍵字的 `ChromeService(...)`"
                   "——是不是改名或搬走了？搬走的話這支測試要跟著改。")
    swallowed = []
    for lineno, kwargs in calls:
        for kw in sorted(kwargs - accepted):
            swallowed.append(f"{variant}:{lineno} `{kw}=`")
    assert not swallowed, (
        f"這些關鍵字裝著的 selenium 不認得，會被 `**kwargs` 吃掉再丟掉："
        f"{swallowed}。selenium 具名收得下的是：{sorted(accepted)}。"
        "chromedriver 的記錄檔要用 `log_output=`（會變成 `--log-path=` 這個 "
        "chromedriver 參數），`log_path=` 不是 selenium 的參數。")


def test_asking_for_a_driver_log_actually_produces_the_log_flag():
    """行為面驗證：給了記錄檔路徑，chromedriver 的命令列上就要有 `--log-path=`。

    上一支是「參數名對不對」，這一支是「對了之後有沒有真的生效」。兩支都要，因為
    selenium 是把 `log_output=<字串>` 轉成一個 **chromedriver 的命令列參數**，
    而不是用 Python 這一側去接管道——只驗簽名看不出這一層。
    """
    service = ChromeService(log_output=r"D:\tmp\chromedriver.log",
                            service_args=["--log-level=INFO"])
    args = service.command_line_args()
    assert any(a.startswith("--log-path=") for a in args), (
        f"給了 `log_output=` 卻沒有產生 `--log-path=`：{args}。"
        "selenium 換掉了這個轉換方式，`chromedriver.log` 會再次變成空的。")
    assert "--log-level=INFO" in args, f"`service_args` 沒被帶上：{args}"


def test_the_wrong_keyword_really_is_swallowed_silently():
    """反面：確認 `log_path=` 真的是**無聲**失敗，不是會拋錯的那種。

    這一支是這整個檔案的理由。如果 selenium 哪天改成對不認得的關鍵字丟
    `TypeError`，那上面兩支就變成多餘的了——到時候讓這支紅，好讓人重新評估，
    而不是繼續留著一組沒有必要的測試。
    """
    service = ChromeService(log_path=r"D:\tmp\chromedriver.log",
                            service_args=["--log-level=INFO"])
    args = service.command_line_args()
    assert not any(a.startswith("--log-path=") for a in args), (
        f"selenium 現在認得 `log_path=` 了（{args}）——"
        "本檔的前提變了，請重新評估這三支測試還需不需要。")


_SE_DEBUG = "SE_DEBUG"


def _log_args(**kwargs) -> list[str]:
    """本專案 spawn 進入點實際傳的那一組，回 chromedriver 真正吃到的命令列。

    回傳前把 `log_output` / `process` 兩個欄位歸零，是為了讓這個沒被 start 過的
    `Service` 在被回收時**安靜地**收尾：`Service.__del__` 會呼叫 `stop()`，而
    `stop()` 讀的 `self.process` 在沒 start 過的物件上根本不存在，丟出來的
    `AttributeError` 由 `__del__` 自己吞掉——平常無害，但在 `SE_DEBUG` 之下
    `log_output` 是**建構當下的** `sys.stderr`，也就是 pytest 的擷取物件；等到
    它被回收時那個物件可能已經關了，於是變成直譯器層級的
    `lost sys.stderr`（2026-09-10 實測過一次，整個 pytest 行程當場死掉，看起來
    完全不像測試失敗）。`_owns_log_output` 是 False，所以 selenium 不會去關我們
    借給它的串流——會炸的是回收的時機，不是所有權。
    """
    service = ChromeService(log_output=r"D:\tmp\chromedriver.log",
                            service_args=["--log-level=INFO"], **kwargs)
    args = service.command_line_args()
    service.log_output = None
    service.process = None
    return args


def test_this_suite_is_not_running_under_se_debug():
    """前提檢查：本檔所有結論都假設環境裡**沒有** `SE_DEBUG`。

    有的話，下面每一支「命令列上有沒有 `--log-path=`」的斷言量到的都是另一套
    設定，而不是正式批次跑起來的那一套——測試會紅，但紅的理由會指向錯的地方。
    這一支先講清楚成因，省下追錯方向的那一輪。
    """
    assert _SE_DEBUG not in os.environ, (
        f"環境裡設了 `{_SE_DEBUG}`={os.environ.get(_SE_DEBUG)!r}。"
        "selenium 會因此丟掉我們傳的記錄設定（見下一支測試），本檔量到的東西"
        "就不是正式批次的實際組態了。先把它取消再跑。")


def test_se_debug_silently_discards_our_log_configuration(monkeypatch):
    """**第三種「安靜地失效」**：關鍵字是對的，selenium 也收下了，然後被環境丟掉。

    本檔開頭記的是前兩種——本專案打錯參數名、以及 selenium 改名。這一種不在
    原始碼裡：`ChromiumService.__init__` 只要看到環境變數 `SE_DEBUG` 是真值，就會
    把 `service_args` 裡任何含 `log-level` / `log-path` / `silent` 的參數**整條濾
    掉**，改推 `--verbose`，並把輸出接到 `sys.stderr`。呼叫端一個字都不用改，
    `chromedriver.log` 就再也不會被寫出來。

    對本專案的三個後果（2026-09-10 實測確認，都不會有任何錯誤訊息）：

    1. **診斷檔靜靜地停止更新。** `_dump_chromedriver_log_tail()` 讀到的會是
       **上一個工作階段**留下的尾巴，卻被當成這次 spawn 失敗的現場——那正是
       `_trim_chromedriver_log` 的 docstring 已經警告過的「看起來像診斷、其實是
       舊資料」，只是換一扇門進來。而這個檔存在的唯一理由就是 spawn 失敗那一刻。
    2. **量爆掉，而且倒進另一個檔。** 本專案自己量過 `--verbose` 是 INFO 的 9 倍
       （每個命令 4370 vs 484 bytes）。這些位元組會走子行程的 stderr，而監督者是
       `stderr=subprocess.STDOUT` 全部灌進 `webrunner.log`。
    3. `webrunner.log` 是聊天那一側 tail／搜尋讀的檔，chromedriver 的詳細記錄
       裡含實際打進頁面的提示詞內容——本來只落在本機的東西，變成外送得出去的。

    這支測試不是在要求修掉它（那要動正式批次的 spawn 路徑），是把這個**無聲**的
    行為釘住：selenium 哪天改掉判定方式，這裡會紅，而不是等到某次 spawn 失敗、
    有人盯著一份空的（或過期的）記錄檔查半天。
    """
    monkeypatch.setenv(_SE_DEBUG, "1")
    args = _log_args()

    assert not any("log-path" in a for a in args), (
        f"`{_SE_DEBUG}` 設著卻仍保留 `--log-path=`：{args}。"
        "selenium 改掉這個行為是好事，但本檔的敘述要跟著更新。")
    assert not any("log-level" in a for a in args), (
        f"`{_SE_DEBUG}` 設著卻仍保留 `--log-level=`：{args}")
    assert "--verbose" in args, (
        f"`{_SE_DEBUG}` 設著卻沒有推 `--verbose`：{args}")


def test_a_falsy_looking_se_debug_value_still_triggers_it(monkeypatch):
    """`SE_DEBUG=0` 會**開啟**它，不是關閉。

    判定式是 `os.environ.get("SE_DEBUG")` 的真假值，而 `"0"` 是非空字串＝真。
    也就是說想關掉它的人打 `SE_DEBUG=0`，得到的是完全相反的結果，而且沒有任何
    回饋。唯一真的等於「沒設」的是空字串（或整個取消）。
    這一格是分開列的：上一支只證明「設了會出事」，證明不了「以為關掉了其實沒有」。
    """
    monkeypatch.setenv(_SE_DEBUG, "0")
    assert "--verbose" in _log_args(), (
        "`SE_DEBUG=0` 不再觸發詳細模式了——這是好消息，但上面那段敘述要改。")

    monkeypatch.setenv(_SE_DEBUG, "")
    assert any("log-path" in a for a in _log_args()), (
        "空字串的 `SE_DEBUG` 竟然也觸發了——那就連「取消設定」以外沒有安全值了，"
        "本檔的敘述要跟著改。")


def test_passing_an_env_mapping_does_not_immunise_us(monkeypatch):
    """反面：`ChromeService(env=...)` **擋不住**這件事，別以為傳了就沒事。

    selenium 在別處是讀 `self.env`（`--enable-chrome-logs` 那一段就是），唯獨
    `SE_DEBUG` 這一段讀的是行程層級的 `os.environ`。所以「把乾淨的 env 傳進去」
    這個看起來最自然的防法是無效的——真要防只能在 spawn 之前動 `os.environ`
    本身。把這個差別釘住，免得有人寫了一個沒有作用的防護還以為修好了。
    """
    monkeypatch.setenv(_SE_DEBUG, "1")
    args = _log_args(env={})
    assert "--verbose" in args, (
        "傳空的 `env=` 現在擋得住 `SE_DEBUG` 了——selenium 改成讀 `self.env` 了，"
        "那就有一個乾淨的防法可以用，請重新評估這一段。")


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_call_still_asks_for_both_a_log_file_and_verbose_output(variant):
    """`ChromeService(...)` 要同時給 `log_output=` 與 `service_args=`。

    上面那支只驗「用到的關鍵字是對的」，驗不出「關鍵字整個不見了」——而把
    `service_args=["--verbose"]` 拿掉會讓記錄退回預設等級、把 `log_output=`
    拿掉會讓它整個不寫檔，兩種都是這次修好的那個 bug 的另一種長相，而且一樣安靜。
    """
    calls = _chrome_service_call_keywords(variant)
    assert len(calls) == 1, (
        f"`{variant}` 預期只有一處 `ChromeService(...)`，實際 {calls}")
    _, kwargs = calls[0]
    for required in ("log_output", "service_args"):
        assert required in kwargs, (
            f"`ChromeService(...)` 不再傳 `{required}=` 了。"
            "少了它 `chromedriver.log` 就回到「有 `--verbose` 卻沒人收」"
            "或「根本沒開詳細記錄」——spawn 失敗時一樣什麼都看不到。")


def test_accepted_keywords_looks_past_the_outermost_signature():
    """`_accepted_keywords` 必須走完整條 MRO，不能只看最外層那個簽名。

    `chrome.Service.__init__` 只具名收五個參數，其餘靠 `**kwargs` 往下轉；真正
    接住 `driver_path_env_key` 的是更底層。只看最外層的話，一個**合法**的關鍵字
    會被誤報成「會被丟掉」——那就是會亂叫的守門，而亂叫的守門會被關掉。
    """
    outermost = {name for name, p
                 in inspect.signature(ChromeService.__init__).parameters.items()
                 if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    deep = _accepted_keywords(ChromeService)
    assert "driver_path_env_key" not in outermost, (
        "`driver_path_env_key` 現在出現在最外層簽名了——這支測試挑的例子失效，"
        "換一個只存在於底層的參數名。")
    assert "driver_path_env_key" in deep, (
        "`_accepted_keywords` 沒走完 MRO：只存在於底層的參數認不出來，"
        "合法的呼叫會被誤報。")


@pytest.mark.parametrize("required", ["log_output", "service_args"])
def test_the_keywords_this_project_relies_on_still_exist(required):
    """本專案倚賴的參數名還在不在。selenium 每個月都在發版。"""
    assert required in _accepted_keywords(ChromeService), (
        f"裝著的 selenium 不再具名收 `{required}`——"
        "`build_stealth_driver` 的呼叫要跟著改，否則會被安靜地丟掉。")


def _chrome_service_arg_values(variant: str) -> list[str]:
    """該變體 `ChromeService(..., service_args=[...])` 裡那串字面值。"""
    path = _PKG / variant
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in ("ChromeService", "Service"):
            continue
        for kw in node.keywords:
            if kw.arg == "service_args" and isinstance(kw.value, ast.List):
                return [e.value for e in kw.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_driver_log_uses_the_cheap_level_not_verbose(variant):
    """記錄等級要是 `--log-level=INFO`，不是 `--verbose`。

    2026-08-29 實測，同一個真的 spawn 失敗兩種等級各跑一次：

    | 等級                | 平時每個 WebDriver 命令 | 失敗時的記錄 | root cause |
    |---------------------|------------------------|-------------|-----------|
    | `--verbose`         | 4370 bytes             | 1242 bytes  | 有         |
    | `--log-level=INFO`  |  484 bytes             | 1095 bytes  | 有         |

    也就是 `--verbose` 多出來的九成全是**正常運作時**的雜訊，而這份記錄唯一的用途
    是「失敗之後看最後幾行」。`wait_for_new_image` 每張圖最多 poll 180 次、每次兩個
    命令，`--verbose` 一個角色可以寫到 180 MB（常見約 20 MB）——無人值守的機器上這
    是純粹的磁碟耗損。
    """
    args = _chrome_service_arg_values(variant)
    assert args, (f"`{variant}` 的 `ChromeService(...)` 沒有帶 "
                  "`service_args=[...]` 字面值了——"
                  "沒有它 chromedriver 會退回預設等級，spawn 失敗時看不到原因。")
    assert "--verbose" not in args, (
        f"又用回 `--verbose` 了（{args}）。它比 `--log-level=INFO` 貴 9 倍，"
        "而失敗時提供的 root cause 一字不差——多出來的全是正常運作時的雜訊。")
    assert any(a.startswith("--log-level=") for a in args), (
        f"`service_args` 裡沒有 `--log-level=`（{args}）：記錄會退回預設等級。")


def test_the_log_tail_reader_does_not_load_the_whole_file():
    """讀 tail 不得把整份記錄載進記憶體。

    這個 helper 被呼叫的時機正好是 **Chrome 剛剛起不來**——那經常就是記憶體不夠的
    時候，而記錄檔在一個角色之內可以長到數十 MB。要看的只有最後幾行。
    """
    import os
    import sys
    import tempfile
    from pathlib import Path

    sys.path.insert(0, str(REPO_ROOT / "axiomatic"))
    ws = _load_shared()

    source = inspect.getsource(ws._dump_chromedriver_log_tail)
    assert "read_text" not in source, (
        "`_dump_chromedriver_log_tail` 又用 `read_text()` 讀整份了——"
        "Chrome 起不來的當下把數十 MB 讀進記憶體，正是最不該做的事。")

    tmp = Path(tempfile.mkdtemp()) / "big.log"
    with tmp.open("wb") as handle:
        for i in range(20000):
            handle.write(f"line {i}\n".encode("utf-8"))
    size = tmp.stat().st_size
    text = ws._read_tail_text(tmp, max_bytes=4096)
    assert size > 100_000, f"樣本檔太小，測不出東西（{size}）"
    assert len(text) <= 4096, f"讀回來 {len(text)} bytes，超過上限"
    assert text.splitlines()[-1] == "line 19999", text.splitlines()[-3:]
    # 從中間切下去第一行通常是半行，必須丟掉，否則 tail 的第一行是亂的。
    assert text.splitlines()[0].startswith("line "), text.splitlines()[0]

    # 小檔（沒 seek 過）不得丟掉第一行。
    small = tmp.with_name("small.log")
    small.write_bytes(b"first\nsecond\n")
    assert ws._read_tail_text(small, max_bytes=4096).splitlines() == [
        "first", "second"]

    # 空檔不得爆炸。
    empty = tmp.with_name("empty.log")
    empty.write_bytes(b"")
    assert ws._read_tail_text(empty, max_bytes=4096) == ""

    for path in (tmp, small, empty):
        os.remove(path)
    os.rmdir(tmp.parent)


# ---------------------------------------------------------------------------
# `chromedriver.log` 的上限
# ---------------------------------------------------------------------------
def _load_webrunner():
    import sys
    sys.path.insert(0, str(REPO_ROOT / "axiomatic"))
    import webrunner_novelai as wn
    return wn


def _load_shared():
    """chromedriver 記錄檔那一組的**擁有者**是共用模組，不是任一個變體。

    2026-09-09 從 `webrunner_novelai.py` 搬過去，讓 je 變體用同一份（那一側原本
    整組都沒有）。這裡刻意對著共用模組 monkeypatch 而不是對著變體：常數只存在共用
    模組那一份，變體若又綁一份同名常數，patch 就會**安靜地**失效，而修剪會改打到
    repo 根目錄那個正被活著的 chromedriver 握著的真檔。對不上時要立刻
    AttributeError，這就是原因。
    """
    import sys
    sys.path.insert(0, str(REPO_ROOT / "axiomatic"))
    import _webrunner_shared as ws
    return ws


def test_an_oversized_driver_log_is_trimmed_to_its_tail(tmp_path, monkeypatch,
                                                        capsys):
    """行為面：超過上限就要縮小，而且**保留的是尾段**。

    尾段而不是整個清空，理由跟 `trim_log` 的 docstring 一樣：會炸掉的正是「崩潰
    → 重生」那條接縫，清掉就等於把要查的東西丟了。所以這支不只驗「變小了」，
    還驗「最後一行還在、最前面那些不見了」——只斷言變小的話，一個把檔案 unlink
    掉的實作也會過。
    """
    ws = _load_shared()
    log = tmp_path / "chromedriver.log"
    # 樣本大小**由常數推出來**，不要寫死行數：門檻是會隨實測調整的（2026-09-07
    # 就從 8 MB 調成 64 MB），寫死的話樣本會悄悄掉到門檻以下，測試照樣綠、卻什麼
    # 都沒驗到。下面那句 assert 是這件事的最後一道保險。
    line = b"[INFO]: line %07d\n"
    per_line = len(line % 0)
    count = ws._CHROMEDRIVER_LOG_MAX_BYTES // per_line + 1000
    with log.open("wb") as handle:
        for i in range(count):
            handle.write(line % i)
    before = log.stat().st_size
    last_line = (line % (count - 1)).decode("utf-8").rstrip("\n")
    assert before > ws._CHROMEDRIVER_LOG_MAX_BYTES, (
        f"樣本檔 {before} bytes 沒超過上限 "
        f"{ws._CHROMEDRIVER_LOG_MAX_BYTES}，這支測試什麼都沒驗到")

    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG", log)
    ws._trim_chromedriver_log()

    after = log.stat().st_size
    assert after < before, f"沒有縮小：{before} → {after}"
    assert after <= ws._CHROMEDRIVER_LOG_KEEP_BYTES, (
        f"修剪後 {after} bytes，超過保留量 {ws._CHROMEDRIVER_LOG_KEEP_BYTES}")
    assert after > 0, "整個被清空了——尾段保留法的重點就是不要清空"

    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == last_line, (
        f"尾巴不見了，保留到的是 {lines[-1]!r}——修剪保留的必須是**尾段**")
    assert "[INFO]: line 0000000" not in lines, "開頭那些應該要被丟掉才對"
    # 從中間切下去的第一行通常是半行，必須丟掉，否則 tail 的第一行是亂的。
    assert lines[0].startswith("[INFO]: line "), lines[0]

    err = capsys.readouterr().err
    assert "chromedriver.log" in err, (
        "修剪了卻沒說一聲。單一工作階段內的成長是修剪不掉的（中途截斷會被補零），"
        "所以這行警告是那個情形唯一的線索——沒有它，工作階段長到失控（重啟被停用、"
        "或產圖速率掉到剩幾分之一）完全無聲。")
    assert "restart_chrome_every_n_characters=0" not in err.replace(" ", ""), (
        "警告不可以把 `=0` 講成最可能的原因。2026-09-07 實測：該值是 **1**、"
        "`images_per_character=120`、速率 8 張/小時 → 一個角色 15 小時 ≈ 30 MB，"
        "照那句話去查會查到一個設成 1 的設定然後找不到問題。成因是**單一工作階段"
        "的長度**（角色數 × 每角色張數 ÷ 速率），不是那個角色數本身。")


def test_a_small_driver_log_is_left_exactly_as_it_was(tmp_path, monkeypatch,
                                                      capsys):
    """沒超過上限就一個位元組都不准動。

    反面很重要：健康的執行每個角色都會經過這裡（`restart_chrome_every_n_
    characters` 預設 1）。若門檻訂得太低或實作無條件重寫，就會變成每個角色重寫
    一次好幾十 MB 的檔——而且警告每次都響，響到有人把它關掉。門檻本身由
    `test_the_driver_log_ceiling_clears_a_whole_character_session` 顧。
    """
    ws = _load_shared()
    log = tmp_path / "chromedriver.log"
    payload = b"[INFO]: Starting ChromeDriver\n" * 100
    log.write_bytes(payload)

    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG", log)
    ws._trim_chromedriver_log()

    assert log.read_bytes() == payload, "沒超過上限卻被改動了"
    assert "chromedriver.log" not in capsys.readouterr().err, (
        "沒修剪卻印了警告——會亂叫的守門會被關掉")


def test_a_missing_driver_log_is_not_an_error(tmp_path, monkeypatch):
    """檔案不存在（全新 clone、第一次啟動）不得丟例外。

    這支跑在起 Chrome 的必經路徑上，為了一個還沒被建立的記錄檔而讓整輪起不來，
    是比記錄檔太大糟得多的失敗。
    """
    ws = _load_shared()
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG", tmp_path / "nope.log")
    ws._trim_chromedriver_log()          # 不該丟


def test_the_driver_log_ceiling_leaves_room_for_the_tail_reader():
    """保留量必須 ≥ `_read_tail_text` 一次讀的視窗（預設 64 KiB）。

    這兩個數字是耦合的：保留量若比讀取視窗小，spawn 失敗時 tail 讀到的就是被
    修剪切短的一小截，而那正是這個檔存在的唯一理由。挑數字的判準是「還原一次
    失敗現場要多少」——一次完整的失敗現場在 `--log-level=INFO` 下實測 1,090
    bytes，所以 256 KB 綽綽有餘；重點是別讓它掉到讀取視窗以下。
    """
    ws = _load_shared()
    default_window = inspect.signature(
        ws._read_tail_text).parameters["max_bytes"].default
    assert ws._CHROMEDRIVER_LOG_KEEP_BYTES >= default_window, (
        f"保留 {ws._CHROMEDRIVER_LOG_KEEP_BYTES} bytes 比 tail 的讀取視窗 "
        f"{default_window} bytes 還小——修剪過的檔連一個完整視窗都湊不出來。")
    assert (ws._CHROMEDRIVER_LOG_MAX_BYTES
            > ws._CHROMEDRIVER_LOG_KEEP_BYTES), "上限必須大於保留量"


def test_the_driver_log_ceiling_clears_a_whole_character_session():
    """門檻必須高過**一個角色跑完**會寫的量，否則每個角色都會叫一次。

    這支釘的是那個「會亂叫的守門會被關掉」的性質——而它一度是**破的**：門檻原本
    訂 8 MB，依據是「高過正常單角色量 5.4 MB」，但那 5.4 MB 是某個角色**跑到一半**
    的量。實際跑完是 28–32 MB，於是每個角色都觸發，正好變成當初要避免的狼來了。

    下面的數字全部是 2026-09-07 在本機量的，寫死在這裡是刻意的：它們是門檻的
    **依據**，門檻改動的時候應該連著這些一起重新檢查，而不是把 assert 調鬆。

    * 成長率 2.15 MB/小時（5.64 小時寫了 12,747,290 bytes）；
    * 產圖速率 8.0 張/小時（`output/**/*.png` 的 mtime，13 個完整小時）；
    * `images_per_character` 取設定檔的實際值。
    """
    ws = _load_shared()
    import json
    cfg = json.loads(
        (REPO_ROOT / "batch_config.json").read_text(encoding="utf-8"))
    images = cfg.get("images_per_character", 120)

    mb_per_hour = 2.15
    images_per_hour = 8.0
    session_mb = images / images_per_hour * mb_per_hour
    ceiling_mb = ws._CHROMEDRIVER_LOG_MAX_BYTES / (1024 * 1024)

    assert ceiling_mb > session_mb, (
        f"上限 {ceiling_mb:.0f} MB 比一個角色會寫的 {session_mb:.0f} MB 還低"
        f"（{images} 張 ÷ {images_per_hour} 張/小時 × {mb_per_hour} MB/小時）"
        f"——健康的執行每個角色都會觸發一次警告，那個警告就沒有訊號了。")
    # 上限也不該高到失去意義：真的失控（工作階段跑成好幾個角色那麼長）要抓得到。
    assert ceiling_mb < session_mb * 4, (
        f"上限 {ceiling_mb:.0f} MB 是一個角色量的 "
        f"{ceiling_mb / session_mb:.1f} 倍，太寬鬆了——失控要好幾天才會被說一聲。")


def _spawn_call_lines(variant: str) -> dict[str, list[int]]:
    """該變體 spawn 進入點裡，我們在意的那幾種呼叫各出現在哪幾行。

    抽成 helper 是因為兩支順序測試都要用，而且要對**兩個變體**跑：selenium 變體的
    進入點是 `build_stealth_driver`、je 是 `start_driver`。
    """
    path = _PKG / variant
    wanted = _SPAWN_FUNCTION[variant]
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == wanted), None)
    assert func is not None, (
        f"`{variant}` 裡找不到 `{wanted}`——改名了的話 `_SPAWN_FUNCTION` 要跟著改")

    seen: dict[str, list[int]] = {
        "_trim_chromedriver_log": [], "_rotate_chromedriver_log": [],
        "_dump_chromedriver_log_tail": [], "full_error_detail": [],
        "service": []}
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None))
        if name in seen:
            seen[name].append(node.lineno)
        elif name in ("ChromeService", "Service"):
            seen["service"].append(node.lineno)
    return seen


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_spawn_path_actually_trims_before_it_opens_the_service(variant):
    """spawn 進入點必須真的呼叫修剪，而且在 `ChromeService(...)` 之前。

    「函式寫好了但沒有人呼叫」是本專案反覆踩到的無聲失敗（`log_path=` 被
    `**kwargs` 吃掉是同一家族），所以用 AST 釘住呼叫確實存在。順序也要釘：
    `ChromeService(log_output=...)` 之後那個檔就有 chromedriver 握著了，在那之後
    截斷不但收不回空間，還會被作業系統補零、把記錄壓成亂碼（2026-09-06 實測
    5,170 → 746 bytes → 再寫幾行變 7,572 bytes，其中 4,424 個是 NUL）。
    """
    seen = _spawn_call_lines(variant)
    trim_lines, service_lines = seen["_trim_chromedriver_log"], seen["service"]

    assert trim_lines, (
        f"`{variant}` 的 spawn 路徑沒有呼叫 `_trim_chromedriver_log()`——"
        "記錄檔的上限就只剩下 chromedriver 自己啟動時的清空，"
        "而那件事有好幾種無聲失效的方式（見該函式的 docstring）。")
    assert service_lines, "找不到 `ChromeService(...)`——這支測試要跟著改"
    assert min(trim_lines) < min(service_lines), (
        f"{variant}：修剪（第 {min(trim_lines)} 行）排在 `ChromeService`（第 "
        f"{min(service_lines)} 行）之後了。那時候檔案已經交給 chromedriver，"
        "截斷會被補零。")


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_spawn_path_dumps_the_log_when_an_attempt_fails(variant):
    """spawn 失敗時要傾印記錄尾段——否則整組設施等於沒有接上。

    這支釘的是 je 那一側 2026-09-09 之前的實際狀況：記錄檔可以產生、封頂與保留也
    可以做，但**沒有任何人在失敗時去讀它**，於是留在 stderr 的只有一句猜測
    （「likely Out of Memory or a chromedriver/Chrome version mismatch」）。而
    `/run` 的預設變體就是 je，`_watch_for_fallback` 又會在 5 分鐘內靜靜轉跑
    selenium 變體，所以「je 為什麼起不來」在正式環境是查不到的。

    只釘「有呼叫」不釘位置，因為兩個變體的重試迴圈長得不一樣；重要的是它在
    `except` 裡、下一次嘗試把記錄清空之前跑到。
    """
    seen = _spawn_call_lines(variant)
    assert seen["_dump_chromedriver_log_tail"], (
        f"`{variant}` 的 spawn 路徑失敗時沒有呼叫 "
        "`_dump_chromedriver_log_tail()`——記錄檔照樣被寫出來、照樣被封頂，"
        "然後沒有人讀，spawn 失敗只剩下一句猜測。")


def test_the_trim_reuses_the_shared_helper_instead_of_a_third_copy():
    """修剪要走 `_supervisor.trim_log`，不要再抄一份。

    本專案已經有兩份尾段保留的修剪（兩支啟動器共用的 `trim_log`、
    `discord_bot._rotate_ndjson_tail`）。第三份就是第三個要同步的地方，而
    `_supervisor` 是 CLAUDE.md 列冊的被動共用模組，webrunner 本來就可以 import。
    """
    ws = _load_shared()
    import _supervisor

    assert ws.trim_log is _supervisor.trim_log, (
        "`trim_log` 不是共用模組那一份了——是不是又抄了一份本地實作？")
    source = inspect.getsource(ws._trim_chromedriver_log)
    assert "trim_log(" in source, (
        "`_trim_chromedriver_log` 不再呼叫 `trim_log`——自己實作截斷的話，"
        "「保留尾段而不是清空」這條規則就多了一個會漂移的複本。")


def test_selenium_still_hands_the_log_path_to_chromedriver_not_to_an_append_handle():
    """selenium 必須把字串路徑轉成 chromedriver 的 `--log-path=`，**不是**自己
    `open(..., "a+")` 接管那個檔。

    `common.service.Service.__init__` 裡真的有這一段：

        if isinstance(log_output, str):
            self.log_output = open(log_output, "a+", encoding="utf-8")

    附加模式＝永遠只長不消。目前救我們的只是 `ChromiumService.__init__` 搶先一步
    把字串攔下來轉成 `--log-path=`，而 chromedriver 每次啟動會**清空**那個檔
    （2026-09-06 實測：人工墊到 50,368 bytes，下次啟動後回到 366 bytes）。這層
    攔截哪天不見了，`chromedriver.log` 就變成純附加，`_CHROMEDRIVER_LOG_MAX_BYTES`
    也得跟著調緊——讓這支先紅，好讓人重新評估，而不是等磁碟滿了才發現。
    """
    import tempfile
    from pathlib import Path as _Path

    target = _Path(tempfile.mkdtemp()) / "cd.log"
    service = ChromeService(log_output=str(target),
                            service_args=["--log-level=INFO"])
    try:
        assert not hasattr(service.log_output, "write"), (
            f"selenium 現在自己開著這個檔（{service.log_output!r}）。"
            "那條路是 `a+` 附加模式，檔案會永遠只長不消。")
        assert any(a == f"--log-path={target}"
                   for a in service.command_line_args()), (
            f"`--log-path=` 沒被傳給 chromedriver：{service.command_line_args()}")
    finally:
        try:
            service.log_output.close()   # 只有在上面那個斷言紅掉時才需要
        except AttributeError:
            pass


# ---------- 上一個工作階段的 driver 記錄要留下來 ----------------------------
# 2026-09-07：瀏覽器在跑到一半死掉（`ConnectionRefusedError: [WinError 10061]`
# ＝ chromedriver.exe 自己結束了），而崩潰前那份 `chromedriver.log` **已經不在**：
#     崩潰 11:44:54 → 監督者重生 11:44:59 → 磁碟上那份的第一筆是 11:45:02
# chromedriver 每次啟動會清空 `--log-path` 指的檔，所以重生就把唯一可能記載死因的
# 東西蓋掉了。**診斷工具把它存在的目的所需要的證據銷毀了。**

def test_the_previous_driver_log_is_kept_before_the_new_one_truncates_it(
        tmp_path, monkeypatch):
    """行為面：起 driver 之前，舊的 `chromedriver.log` 要變成 `.prev`。"""
    ws = _load_shared()
    log = tmp_path / "chromedriver.log"
    prev = tmp_path / "chromedriver.prev.log"
    log.write_bytes(b"[INFO]: the session that died\n")
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG", log)
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG_PREV", prev)

    ws._rotate_chromedriver_log()

    assert prev.read_bytes() == b"[INFO]: the session that died\n", (
        "上一個工作階段的記錄沒有被保留下來——新的 chromedriver 一啟動就會把它"
        "截斷，於是「跑到一半死掉」那次永遠沒有證據可查。")
    assert not log.exists(), (
        "保留動作應該是**改名**（os.replace），不是複製；留著原檔的話新的 driver "
        "還是會截斷它，只是多佔一份磁碟。")


def test_rotating_without_a_previous_log_is_not_an_error(tmp_path, monkeypatch,
                                                         capsys):
    """第一次啟動沒有舊檔，要安靜地什麼都不做——不得出聲、不得丟例外。

    這是純診斷輔助，絕不能擋住起 driver。而「每次都印一行」等於沒有訊號。
    """
    ws = _load_shared()
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG", tmp_path / "nope.log")
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG_PREV", tmp_path / "nope.prev")
    ws._rotate_chromedriver_log()
    assert capsys.readouterr().err == "", "沒有舊檔是常態，不該印任何東西"


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_spawn_path_keeps_the_previous_log_before_opening_the_service(variant):
    """AST：spawn 進入點要真的呼叫保留，而且順序是「先封頂、再改名、最後才
    `ChromeService`」。

    順序有兩個理由，各自獨立：
    - 改名必須在 `ChromeService(...)` **之前**——那之後路徑就交給 chromedriver 的
      `--log-path=` 了（跟修剪不能放在之後是同一個理由）。
    - 修剪必須在改名**之前**，這樣 `.prev` 拿到的一定是已封頂的那一份；反過來的話
      `.prev` 可以無上限地大，兩份加起來就沒有磁碟上限可言。
    """
    seen = _spawn_call_lines(variant)

    assert seen["_rotate_chromedriver_log"], (
        f"`{variant}` 的 spawn 路徑沒有呼叫 `_rotate_chromedriver_log()`——"
        "每次重生都會把上一個工作階段的記錄截掉，崩潰死因永遠查不到。")
    assert seen["service"], "找不到 `ChromeService(...)`——這支測試要跟著改"
    assert (min(seen["_trim_chromedriver_log"])
            < min(seen["_rotate_chromedriver_log"])
            < min(seen["service"])), (
        f"{variant} 順序不對：trim={seen['_trim_chromedriver_log']} "
        f"rotate={seen['_rotate_chromedriver_log']} "
        f"service={seen['service']}。必須是 trim → rotate → ChromeService。")


def test_the_log_tail_dump_also_reads_the_previous_session(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    """崩潰後的診斷想看的正是**上一份**，所以 tail 兩份都要印、而且要分得出來。

    不標明是哪一份的話，兩段時間戳混在一起，讀的人會以為是同一個工作階段——本專案
    對「log 的敘述本身會誤導後續判斷」已經有過昂貴的教訓。
    """
    ws = _load_shared()
    log = tmp_path / "chromedriver.log"
    prev = tmp_path / "chromedriver.prev.log"
    log.write_text("[INFO]: this attempt failed\n", encoding="utf-8")
    prev.write_text("[INFO]: the session that died\n", encoding="utf-8")
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG", log)
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG_PREV", prev)

    ws._dump_chromedriver_log_tail()
    err = capsys.readouterr().err

    assert "the session that died" in err, (
        "上一個工作階段的尾巴沒被印出來——那正是「跑到一半死掉」要看的東西")
    assert "this attempt failed" in err, "這一次的尾巴也要印"
    assert "chromedriver.prev.log" in err and "chromedriver.log（這一次）" in err, (
        "兩份沒有分別標示，時間戳混在一起會被讀成同一個工作階段。實際輸出：" + err)


def test_the_log_tail_dump_says_so_when_the_log_was_never_written(
        tmp_path, monkeypatch, capsys):
    """記錄檔不存在時**必須出聲**——這正是本檔開頭那個 bug 活了幾個月的原因。

    當時 `ChromeService` 傳的是 selenium 不認得的 `log_path=`，檔案從來沒被建立；
    而這個 helper 原本是 `except OSError: return`，於是「記錄沒被寫出來」跟
    「一切正常、只是沒東西好印」在 stderr 上長得**一模一樣**。唯一會揭穿那件事的
    就是這個 helper，而它選擇沉默。

    2026-09-09 之後這條路多了第二個使用者（je 變體），而它的接線更脆弱——靠的是
    `set_driver` 的 `**kwargs` 轉發，上游一改就會安靜地退回「檔案不存在」。所以
    這個訊息不再只是歷史註腳，它是那條路真正的守門。
    """
    ws = _load_shared()
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG", tmp_path / "nope.log")
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG_PREV", tmp_path / "nope.prev")

    ws._dump_chromedriver_log_tail()
    err = capsys.readouterr().err

    assert err.strip(), (
        "記錄檔不存在卻一個字都沒印——「沒被寫出來」與「沒東西好印」就再也分不"
        "出來了，而前者代表 spawn 失敗永遠查不到 root cause。")
    assert "log_output" in err, (
        f"訊息沒有指向可以動手的地方（`ChromeService` 的 `log_output=`）：{err!r}。"
        "這個檔不存在時，讀的人需要的是「去檢查哪個參數」，不是「檔案不見了」。")


def test_the_log_tail_dump_stays_quiet_about_a_missing_previous_log(
        tmp_path, monkeypatch, capsys):
    """反面：`.prev` 不存在是**第一次啟動的常態**，不得出聲。

    沒有這一條，上面那支會誘使人寫成「兩份都不在就都抱怨一次」，於是每一次全新
    clone 的第一次 spawn 失敗都多一行雜訊——而會亂叫的守門會被關掉。
    """
    ws = _load_shared()
    log = tmp_path / "chromedriver.log"
    log.write_text("[INFO]: this attempt failed\n", encoding="utf-8")
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG", log)
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG_PREV", tmp_path / "nope.prev")

    ws._dump_chromedriver_log_tail()
    err = capsys.readouterr().err

    assert "this attempt failed" in err, "這一次的尾巴還是要印"
    assert "prev" not in err.lower(), (
        f"抱怨了不存在的 `.prev`：{err!r}。第一次啟動沒有上一份是常態。")
# ---------------------------------------------------------------------------
# spawn 失敗的那一行 log：`{err!r}` 會把 selenium 的訊息整段丟掉
#
# selenium 的 `WebDriverException` 把訊息存在 `self.msg`，**`args` 是空的**，所以
# `repr(e)` 只剩一對空括號。2026-08-25 11:38:50 真的因此走進一次鑑識死路：log 裡
# 只有 `Chrome spawn attempt 1/3 failed: SessionNotCreatedException()`「別的什麼都
# 沒有」，於是有人跑去翻一個當時根本還沒被建立的 `chromedriver.log`——而訊息從來
# 就不是空的，是我們自己的格式把它丟掉的（2026-09-12 查出並修正）。
#
# ⚠️ **語料是這一族的重點。** 用 `Exception("boom")` 寫的測試對這個缺陷完全無效：
# 那種例外 `args` 非空，`repr()` 會帶上訊息，**舊格式與新格式都會通過**。下面刻意
# 保留那個反面對照組，讓「為什麼一定要用真的 selenium 例外」看得見。
# ---------------------------------------------------------------------------
_SPAWN_FAIL_MESSAGE = (
    "session not created: This version of ChromeDriver only supports Chrome "
    "version 145\nCurrent browser version is 152.0.7977.84 with binary path "
    r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
_VERSION_TELL = "only supports Chrome version 145"


def _real_selenium_spawn_error():
    """真的 selenium 例外——`args` 空、`msg` 有值。不是自己捏一個假的類別。

    捏假的沒有意義：這支測試要問的正是「**目前裝著的** selenium 是不是還把訊息
    藏在 `repr()` 之外」，而那是套件的性質，不是我們宣告得出來的。
    """
    from selenium.common.exceptions import SessionNotCreatedException
    return SessionNotCreatedException(_SPAWN_FAIL_MESSAGE)


def test_selenium_still_hides_its_message_from_repr():
    """這一族的前提：`repr()` 看不到訊息，`str()` 看得到。

    正面對照組的角色——前提哪天不成立了（selenium 改成把 `msg` 塞進 `args`），
    這支會紅，而那代表整族測試的動機要重新評估，不是預設無害。
    """
    err = _real_selenium_spawn_error()
    assert err.args == (), (
        f"selenium 例外的 `args` 不再是空的了：{err.args!r}。這族測試的前提變了。")
    assert _VERSION_TELL not in repr(err), (
        f"`repr()` 現在帶得出訊息了：{repr(err)}")
    assert _VERSION_TELL in str(err), (
        f"`str()` 也讀不到訊息，那就不只是格式問題了：{str(err)[:200]}")


def test_the_spawn_failure_format_keeps_the_version_mismatch():
    """`full_error_detail` 要同時留住**訊息**與**型別名**。"""
    ws = _load_shared()
    err = _real_selenium_spawn_error()
    line = ws.full_error_detail(err)
    assert _VERSION_TELL in line, (
        f"版本不符那句被丟掉了——這正是 2026-08-25 查不下去的原因：{line!r}")
    assert "152.0.7977.84" in line, f"另一邊的版本號也要在：{line!r}"
    assert line.startswith("SessionNotCreatedException:"), (
        "型別名是 `repr()` 唯一給對的東西，不可以連它一起丟掉："
        f"{line!r}")


def test_a_plain_exception_would_not_have_caught_this():
    """反面對照組：為什麼語料不能是 `Exception("boom")`。

    一般例外 `args` 非空，`repr()` 本來就帶訊息，所以**舊格式也會通過**。拿它寫
    的測試不會在缺陷存在時變紅，是一支不可能失敗的測試。
    """
    ws = _load_shared()
    plain = Exception("boom")
    assert "boom" in f"{plain!r}", (
        "一般例外的 `repr()` 連訊息都沒有的話，這個反面對照組就不成立了")
    assert "boom" in ws.full_error_detail(plain)

    selenium_err = _real_selenium_spawn_error()
    assert _VERSION_TELL not in f"{selenium_err!r}", (
        "舊格式對真的 selenium 例外也通過的話，這族測試就沒有鑑別力了")


def _bare_exception_interpolations(variant: str) -> list[tuple[int, str]]:
    """spawn 進入點裡，把**例外本身**直接插進字串的地方（沒有經過格式化函式）。

    只盯例外名，不是「禁止一切 `!r`」——`f"{path!r}"` 印路徑是合理的，而會亂叫的
    守門遲早會被關掉。例外名的來源是 `except ... as X` 綁出來的名字，外加從那些
    名字轉手過去的指派（`last_err = err`）。屬性存取也算（`err.__cause__`），因為
    根名字仍然是那個例外。

    包在 `full_error_detail(...)` 裡的插值**不會**被抽到：那時候被插進去的是一個
    `Call` 節點，不是例外名本身。所以這支的回傳值就是「沒有走共用格式的例外插值」。
    """
    path = _PKG / variant
    wanted = _SPAWN_FUNCTION[variant]
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == wanted), None)
    assert func is not None, (
        f"`{variant}` 裡找不到 `{wanted}`——改名了的話 `_SPAWN_FUNCTION` 要跟著改")

    names: set[str] = {h.name for h in ast.walk(func)
                       if isinstance(h, ast.ExceptHandler) and h.name}
    assert names, (
        f"`{variant}.{wanted}` 裡一個 `except ... as X` 都抽不到——抽取器壞了。"
        "空的抽取結果跟乾淨的結果長得一模一樣，所以這裡先釘住。")
    for _ in range(3):                      # 轉手可能不只一層
        for node in ast.walk(func):
            if (isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in names):
                names.update(t.id for t in node.targets
                             if isinstance(t, ast.Name))

    def root(node):
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    return [(n.lineno, ast.unparse(n)) for n in ast.walk(func)
            if isinstance(n, ast.FormattedValue) and root(n.value) in names]


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_spawn_path_never_formats_an_exception_with_repr(variant):
    """具名釘住歷史上那個缺陷：`!r`。兩個變體一起釘。

    這個缺陷之所以有兩份，就是因為當初兩邊各寫了一次；訊息刻意點名 `repr()`，
    因為下一個人在 log 裡看到的症狀是一對空括號，不是「少了格式化函式」。
    """
    sites = [s for s in _bare_exception_interpolations(variant)
             if "!r" in s[1]]
    assert not sites, (
        f"{variant}：spawn 路徑又出現 `!r` 格式化例外：{sites}。selenium 的例外"
        "`args` 是空的，`repr()` 會把訊息整段丟掉——改用 `full_error_detail(...)`。")


@pytest.mark.parametrize("variant", _VARIANTS)
def test_every_exception_in_the_spawn_path_goes_through_the_shared_formatter(
        variant):
    """比上一支嚴：裸的 `{err}` 也不行，而且必須是**共用**那一份。

    ⚠️ 這支存在的理由是實測出來的：只釘「沒有 `!r`」的話，把一個站點改成裸的
    `{err}` 兩支都會綠——訊息留住了，型別名沒了，而
    `SessionNotCreatedException` 與 `TimeoutException` 的區別正是第一層診斷。
    只釘「有呼叫 `full_error_detail`」也不行：其他站點還在呼叫，計數照樣非零。
    要咬得住，判準必須是**每一個**例外插值都被包住。

    下限（`>= 2`）是正面對照組：把所有記錄整段刪掉的話，「沒有裸插值」會空著
    通過——而空的抽取結果跟乾淨的結果長得一模一樣。刻意用下限不用精確值，
    合理地少記一行不該讓這支亂叫。
    """
    bare = _bare_exception_interpolations(variant)
    assert not bare, (
        f"{variant}：這些例外沒有走 `full_error_detail(...)`：{bare}。"
        "裸的 `{err}` 會掉型別名，`{err!r}` 會把 selenium 的訊息整段掉光。")

    wrapped = _spawn_call_lines(variant)["full_error_detail"]
    assert len(wrapped) >= 2, (
        f"`{variant}` 的 spawn 路徑只剩 {len(wrapped)} 處 `full_error_detail(...)`"
        "——記錄被刪掉的話，上面那句「沒有裸插值」會空著通過。")

# ---------------------------------------------------------------------------
# 「好心統一成 `_long_error`」——docstring 勸阻得了，守門才擋得住
#
# `full_error_detail` 的 docstring 花了一整段解釋為什麼不要把它接到
# `_one_line_error` 上。2026-09-12 的變異測試證明那段話**一點執行力都沒有**：把
# 函式主體換成 `return _long_error(error)`，這份檔案當時 41 支全綠——訊息還在、
# 版本號還在、型別名也還在，因為版本號排在訊息**前面**，截斷咬不到它。
#
# 真正被丟掉的是另外兩樣：`_one_line_error` 會砍掉 `Stacktrace:` 之後的整段，
# 也會把換行壓成空白再截到 400 字。實測那個例外 `str()` 752 字，砍完剩 350 字，
# 離上限只有 50 字餘裕——今天剛好裝得下，所以「訊息還在」這種斷言永遠分不出兩者。
# 判準必須改成**逐字保留**，才咬得住。
# ---------------------------------------------------------------------------
_REAL_STACKTRACE = ["Symbols not available. Dumping unresolved backtrace:"] + [
    f"\t0x7ff75c{offset:06x}" for offset in (
        0x60DDE5, 0x60DE40, 0x38D67D, 0x3D5E5D, 0x3D4BB5,
        0x3CF0C0, 0x3C99B1, 0x420111, 0x41F8A2)]


def _spawn_error_with_stacktrace():
    """連 `stacktrace=` 一起給——真的從 driver 丟回來的就是這個形狀。

    這個語料不是憑空捏的，是照著套件原始碼組出來的：
    `SessionNotCreatedException.__init__` 先把支援網址接到 `msg` 後面，再交給
    `WebDriverException.__init__`，而那一支呼叫的是 `super().__init__()`
    ——**不帶引數**。`args` 就是這樣空掉的，而 `__str__` 另外把 `stacktrace`
    渲染在 `Stacktrace:` 底下。
    """
    from selenium.common.exceptions import SessionNotCreatedException
    return SessionNotCreatedException(_SPAWN_FAIL_MESSAGE,
                                      stacktrace=_REAL_STACKTRACE)


def test_the_forensic_format_keeps_what_the_one_line_helpers_throw_away():
    """逐字保留，不截斷也不砍 `Stacktrace:`。

    斷言刻意寫成 `str(err) in line` 而不是「某幾個關鍵字還在」：關鍵字那種寫法
    正是讓 `_long_error` 變異活下來的原因。
    """
    ws = _load_shared()
    err = _spawn_error_with_stacktrace()
    detailed = ws.full_error_detail(err)

    assert str(err) in detailed, (
        "例外訊息沒有被逐字保留——有東西在截斷或改寫它。這支格式器的用途是鑑識，"
        f"少一個字都可能是下次查不下去的那個字。得到的是：{detailed[:200]!r}")
    assert "Stacktrace:" in detailed, (
        "chromedriver 附的 `Stacktrace:` 被砍掉了。那正是 `_one_line_error` 的"
        "行為，也正是這支**不可以**接到它上面的理由。")
    assert detailed.startswith("SessionNotCreatedException:"), (
        f"型別名掉了：{detailed[:80]!r}")


def test_the_one_line_helpers_really_do_lose_it():
    """正面對照組：上一支的理由今天還成立嗎。

    如果哪天 `_one_line_error` 不再截斷、也不再砍 `Stacktrace:`，那麼「不要統一
    它們」這個論點就消失了，這支會紅並要求重新評估——而不是讓一段過期的理由繼續
    擋著一次合理的簡化。反過來說，沒有這支的話，上一支就只是在描述現況，讀不出
    它在防什麼。
    """
    ws = _load_shared()
    err = _spawn_error_with_stacktrace()
    condensed = ws._long_error(err)

    assert "Stacktrace:" not in condensed, (
        "`_long_error` 現在保留 `Stacktrace:` 了——`full_error_detail` 的存在理由"
        "少了一半，docstring 那段要重寫。")
    assert len(condensed) < len(ws.full_error_detail(err)), (
        "`_long_error` 現在不會比完整版短了，兩者的差別消失了。")

    # 餘裕是「今天剛好夠」而不是「綽綽有餘」——這是不共用的第二個理由。上限是
    # 寬的：只要求它**薄**，不要求它剛好等於 50，免得套件改一句話就亂叫。
    head_room = ws._LONG_ERROR_LIMIT - len(
        " ".join(str(err).split("Stacktrace:")[0].split()))
    assert head_room < 120, (
        f"400 字上限現在離實際訊息還有 {head_room} 字餘裕，不再是「剛好夠」。"
        "`full_error_detail` 的 docstring 引用了這個數字，請一起更新。")


# ---------------------------------------------------------------------------
# 全面盤點：三個檔裡「把例外直接插進字串」的每一個站點
#
# 上面那兩支只掃 spawn 進入點。2026-09-12 把範圍拉到整個 webrunner 三件組
# （兩個變體 ＋ 共用模組）之後量到 **46 個** `{X!r}` 站點，其中 12 個換成了共用
# 格式器、34 個刻意保留。判準只有一句話——**這個 `try` 區塊有沒有可能丟出
# selenium 例外**：
#
# * 會 → 必須改。`WebDriverException.__init__` 呼叫 `super().__init__()` 時不帶
#   參數，`args` 是空的，`repr()` 只剩一對空括號，訊息 100% 消失。
# * 不會 → `!r` 反而比較好：這一族大多是檔案 I/O，`repr(OSError)` 是
#   `FileNotFoundError(2, 'No such file or directory')`（看不到路徑），而
#   `str(OSError)` 會把完整主機路徑寫進 `webrunner.log`，`/log tail` 會把那個檔
#   送進聊天平台（Secrecy Layer 1）。
#
# ⚠️ **判準是「try 碰不碰得到 driver」，不是「except 寫的是什麼型別」。** 34 個
# 保留站點裡有 20 個的 handler 就是 `except Exception`，但 `try` 裡只有
# `shutil.copy2` / `os.walk` / psutil / ctypes / `emit_event`。所以這裡不能用
# 「型別是不是 stdlib」當自動判準——只能逐站點登記，而登記表要**雙向**對帳：
# 少一筆（新站點）會紅，多一筆（站點被改掉或函式改名）也會紅。後者是這個 repo
# 反覆吃過虧的方向（`_OWNER_ONLY_SLASH` 那一族）：過期的豁免 fail-open，守門
# 照跑、測試照綠，而下一個真的違規就被預先授權了。
#
# 鍵刻意包含 **handler 的例外型別**：把一個 `except OSError` 改寬成
# `except Exception`（那正是讓它有機會收到 selenium 例外的改法），鍵就對不上、
# 當場變紅。值帶**次數**，因為同一個 handler 裡可以有兩個一模一樣的插值
# （`log_code_fingerprint` 就是），只比「有沒有」會漏掉新增的那一個。
# ---------------------------------------------------------------------------

_REPR_SCANNED_MODULES = (
    "_webrunner_shared.py",
    "webrunner_novelai.py",
    "webrunner_je_only.py",
)

# (模組, 函式, handler 的例外型別, 被插進去的運算式) -> (次數, 理由)
_EXCEPTION_REPR_EXEMPT: dict[tuple, tuple[int, str]] = {
    # --- 共用模組 -----------------------------------------------------------
    ("_webrunner_shared.py", "log_code_fingerprint", ("Exception",), "error"): (
        2, "try 裡只有 `_code_fingerprint`（純檔案雜湊），碰不到 driver；"
           "`str(OSError)` 會印出被雜湊的原始碼路徑。"),
    ("_webrunner_shared.py", "report_code_drift", ("Exception",), "error"): (
        1, "同上，`_code_fingerprint` 是純模組。"),
    ("_webrunner_shared.py", "run_with_liveness_signal", ("Exception",), "error"): (
        1, "`claim_liveness_signal` 是檔案 I/O ＋ pid 探測；`str(OSError)` 會帶出"
           "存活訊號檔的完整路徑。"),
    ("_webrunner_shared.py", "emit_event",
     ("OSError", "TypeError", "ValueError"), "error"): (
        1, "三種例外 `args` 都非空；`str(OSError)` 會把事件檔的完整路徑寫進 log。"),
    ("_webrunner_shared.py", "find_browser_pids_for_profile",
     ("Exception",), "error"): (
        1, "try 裡只有 psutil。psutil 的例外（含 `RuntimeError: "
           "SystemExtendedHandleInformation buffer too big`）`args` 非空。"),
    ("_webrunner_shared.py", "hide_browser_windows", ("Exception",), "error"): (
        2, "桌面自動化函式庫的 import 與呼叫，跟 selenium 無關。"),
    ("_webrunner_shared.py", "reconcile_todo_with_disk", ("OSError",), "error"): (
        1, "只收得到 `OSError`；`str()` 會把備份目錄的完整主機路徑帶進 log。"),
    ("_webrunner_shared.py", "acquire", ("Exception",), "error"): (
        1, "`StayAwake.acquire` 的 try 裡只有 ctypes／WinDLL，碰不到 driver。"),
    ("_webrunner_shared.py", "_emit_serving_beat", ("Exception",), "error"): (
        1, "try 裡只有 `emit_event` 與設定檔算術。"),
    ("_webrunner_shared.py", "check_single_image_request",
     ("OSError", "UnicodeDecodeError", "json.JSONDecodeError"), "error"): (
        1, "三種都是讀檔／解析 JSON 的例外，`args` 非空；`str(OSError)` 會印出"
           "請求檔的完整路徑。"),
    ("_webrunner_shared.py", "generate_loop", ("OSError",), "error"): (
        1, "刪除重複圖檔的 `unlink()`；`str()` 會印出那張圖的完整輸出路徑。"),
    # --- 兩個變體共有（`_MIRRORED` 那一族，逐一相同） -----------------------
    ("webrunner_novelai.py", "_kill_orphan_chrome", ("Exception",), "error"): (
        2, "psutil 掃描與 `subprocess.run(['taskkill', ...])`，兩者都在自己的"
           "driver 存在之前／之外執行，收不到 selenium 例外。"),
    ("webrunner_je_only.py", "_kill_orphan_chrome", ("Exception",), "error"): (
        2, "同 selenium 變體。"),
    ("webrunner_novelai.py", "_clear_snapshot_locks", ("OSError",), "error"): (
        1, "`path.unlink()`；`str()` 會印出 snapshot profile 的完整路徑。"),
    ("webrunner_je_only.py", "_clear_snapshot_locks", ("OSError",), "error"): (
        1, "同 selenium 變體。"),
    ("webrunner_novelai.py", "_reclaim_dir_sync_residue", ("OSError",), "error"): (
        1, "`os.replace()`；`str()` 會印出 profile 的完整路徑。"),
    ("webrunner_je_only.py", "_reclaim_dir_sync_residue", ("OSError",), "error"): (
        1, "同 selenium 變體。"),
    ("webrunner_novelai.py", "_sync_profile_dir_back",
     ("OSError", "shutil.Error"), "error"): (
        1, "`shutil.Error` 也是 `OSError` 的子類別；`str()` 會印出完整路徑。"),
    ("webrunner_je_only.py", "_sync_profile_dir_back",
     ("OSError", "shutil.Error"), "error"): (
        1, "同 selenium 變體。"),
    ("webrunner_novelai.py", "_snapshot_chrome_profile",
     ("OSError", "shutil.Error"), "error"): (
        1, "逐檔複製失敗，收進 `skipped` 再印；`str()` 會讓每一筆都帶完整路徑。"),
    ("webrunner_je_only.py", "_snapshot_chrome_profile",
     ("OSError", "shutil.Error"), "error"): (
        1, "同 selenium 變體。"),
    ("webrunner_novelai.py", "_snapshot_chrome_profile", ("Exception",), "error"): (
        1, "try 裡只有 `os.walk` ＋ `shutil.copy2`，而且跑在 Chrome 起來之前。"),
    ("webrunner_je_only.py", "_snapshot_chrome_profile", ("Exception",), "error"): (
        1, "同 selenium 變體。"),
    ("webrunner_novelai.py", "_sync_chrome_profile_back", ("OSError",), "error"): (
        1, "`shutil.copy2` ＋ `os.replace`；`str()` 會印出登入 profile 的完整路徑。"),
    ("webrunner_je_only.py", "_sync_chrome_profile_back", ("OSError",), "error"): (
        1, "同 selenium 變體。"),
    # --- 兩個變體各自的（`main` / `_restart_chrome_session` 是宣告分歧的） ---
    ("webrunner_novelai.py", "_restart_chrome_session", ("Exception",), "error"): (
        1, "⚠️ 曾被列為「該換格式器」的候選，實際上 try 裡只有 "
           "`_sync_chrome_profile_back(...)` ＝ 純檔案 I/O：Chrome 在上面幾行"
           "已經 `quit()` 了。`str(OSError)` 會帶出登入 profile 的完整路徑。"),
    ("webrunner_je_only.py", "_restart_chrome_session", ("Exception",), "error"): (
        1, "同 selenium 變體。"),
    ("webrunner_novelai.py", "main", ("Exception",), "error"): (
        1, "`finally` 的收尾，driver 已經 `quit()`，try 裡只剩 sync-back 的檔案"
           "I/O。"),
    ("webrunner_je_only.py", "main", ("Exception",), "error"): (
        1, "同 selenium 變體的收尾那一站（je 的 `main` 另有一個包著 "
           "`start_driver()` 的 handler，那一個已經走 `full_error_detail`）。"),
    ("webrunner_novelai.py", "_run_setup_verification", ("Exception",), "err"): (
        1, "try 裡只有 `read_credentials(AUTH_FILE)`：收得到 `OSError`"
           "（`str()` 會印出憑證檔路徑）與 `CredentialsError`（合約就是訊息只帶"
           "檔名、位元組位置或缺漏的欄位名），兩種都不含憑證值本身。"),
}

# 改掉的 12 個站點，連「選了哪一支格式器」一起釘住。只釘「沒有 `!r`」的話，
# 把 `_short_error` 換成 `full_error_detail`（讓重試迴圈每次吐 752 字進 log）
# 完全不會有人說話——而那正是這份表存在的另一半理由。
_DRIVER_FACING_FORMATTER: dict[tuple[str, str], str] = {
    # 重複出現的行 -> 180 字
    ("_webrunner_shared.py", "with_retry"): "_short_error",
    ("_webrunner_shared.py", "dump_textareas_diag"): "_short_error",
    ("_webrunner_shared.py", "snap"): "_short_error",
    ("_webrunner_shared.py", "_fill_via_native_setter"): "_short_error",
    ("_webrunner_shared.py", "rest_until"): "_short_error",
    # 一次性 / 終結性 -> 400 字
    ("_webrunner_shared.py", "check_dom_request"): "_long_error",
    ("_webrunner_shared.py", "verify_character_prompt"): "_long_error",
    ("_webrunner_shared.py", "_refill_character_fields"): "_long_error",
    # spawn 鑑識 -> 不截斷
    ("webrunner_novelai.py", "_run_setup_verification"): "full_error_detail",
    ("webrunner_je_only.py", "main"): "full_error_detail",
}
# `serve_single_image_request` 一支裡兩種都有（清空角色框的迴圈用短的、外層的
# 終結性回報用長的），所以單獨列，不塞進上面那個一對一的表。
_MIXED_FORMATTER_SITES = {
    ("_webrunner_shared.py", "serve_single_image_request"):
        ("_short_error", "_long_error"),
}


def _exception_repr_sites(source: str, filename: str) -> dict[tuple, int]:
    """`source` 裡每一個「把 `except ... as X` 綁出來的名字用 `!r` 插進字串」。

    `source` 是**參數**不是寫死的路徑，合成語料才餵得進來——沒有那一步就問不出
    「新冒出來的違規會不會被發現」，只問得出「現況乾不乾淨」。

    鍵刻意不含行號（行號每次編輯都會動），改成 (模組, 函式, 例外型別, 運算式)，
    值是次數。
    """
    tree = ast.parse(source, filename)
    owner: dict[int, ast.AST] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                cur = owner.get(id(node))
                if cur is None or fn.lineno > cur.lineno:
                    owner[id(node)] = fn

    def _root(node):
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    counts: dict[tuple, int] = {}
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        names = {handler.name}
        for _ in range(3):                  # `last_err = err` 這種轉手
            for node in ast.walk(handler):
                if (isinstance(node, ast.Assign)
                        and isinstance(node.value, ast.Name)
                        and node.value.id in names):
                    names.update(t.id for t in node.targets
                                 if isinstance(t, ast.Name))
        if handler.type is None:
            types = ("<bare>",)
        elif isinstance(handler.type, ast.Tuple):
            types = tuple(ast.unparse(e) for e in handler.type.elts)
        else:
            types = (ast.unparse(handler.type),)
        for node in ast.walk(handler):
            if (isinstance(node, ast.FormattedValue)
                    and node.conversion == ord("r")
                    and _root(node.value) in names):
                fn = owner.get(id(node))
                key = (filename, fn.name if fn else "<module>", types,
                       ast.unparse(node.value))
                counts[key] = counts.get(key, 0) + 1
    return counts


def _all_exception_repr_sites() -> dict[tuple, int]:
    merged: dict[tuple, int] = {}
    for name in _REPR_SCANNED_MODULES:
        path = _PKG / name
        assert path.is_file(), f"`_REPR_SCANNED_MODULES` 列到一個不存在的檔：{name}"
        for key, n in _exception_repr_sites(
                path.read_text(encoding="utf-8"), name).items():
            merged[key] = merged.get(key, 0) + n
    return merged


def test_every_exception_repr_site_is_declared_with_a_written_reason():
    """新冒出來的 `{error!r}` 預設是紅的。

    正面對照組先跑：抽取器要抽到夠多站點。抽不到東西跟「全部乾淨」在斷言上
    長得一模一樣，而這支的整個價值就建立在抽得到。
    """
    found = _all_exception_repr_sites()
    assert sum(found.values()) >= 25, (
        f"整個 webrunner 三件組只抽到 {sum(found.values())} 個例外 `!r` 插值"
        "（2026-09-12 實測 34 個）——抽取器壞了，不是程式碼變乾淨了。")

    undeclared = {k: n for k, n in found.items()
                  if _EXCEPTION_REPR_EXEMPT.get(k, (0, ""))[0] < n}
    assert not undeclared, (
        f"這些 `{{X!r}}` 站點沒有登記：{sorted(undeclared)}。"
        "先問一句：這個 `try` 有沒有可能丟出 selenium 例外？會的話改用 "
        "`_short_error` / `_long_error` / `full_error_detail`（選哪一支看那一行"
        "會不會重複印）；不會的話連理由一起加進 `_EXCEPTION_REPR_EXEMPT`。")


def test_no_exemption_outlives_the_site_it_describes():
    """反方向：過期的豁免 fail-open，所以要自己對帳。

    函式改名、站點被改成格式器、handler 的例外型別被改寬——三種都會讓豁免變成
    一個再也對不上任何東西的字串，而守門照跑、測試照綠，下一個真的違規就被預先
    授權了。這個 repo 已經在 `_OWNER_ONLY_SLASH` / `_NOVELAI_ONLY` 上吃過兩次。
    """
    found = _all_exception_repr_sites()
    stale = {k: n for k, (n, _) in _EXCEPTION_REPR_EXEMPT.items()
             if found.get(k, 0) != n}
    assert not stale, (
        f"這些豁免對不上程式碼了：{sorted(stale)}。站點改掉／函式改名／handler "
        "的例外型別改了的話，把這一筆一起刪掉或改掉。")


_MIRROR_REASON = "同 selenium 變體"   # 「同 selenium 變體」


def test_every_exemption_states_why():
    """理由欄不是裝飾——它就是「下一個人不必再數一次這 46 個」的那份說明。

    兩個變體逐一相同的那一族（`test_chrome_recovery._MIRRORED`）刻意只寫「同
    selenium 變體」而不複製一遍——八段一模一樣的理由文字是**保證會漂**的東西。
    代價是那句話本身要被對帳：novelai 那一筆被刪掉或改名的話，je 這一筆就變成
    一個指向不存在的東西的交叉引用，而那正是這個 repo 反覆吃虧的 fail-open
    形狀。所以這裡不是放寬長度下限，是把交叉引用當成一筆要驗的斷言。
    """
    thin, dangling = [], []
    for key, (_, why) in _EXCEPTION_REPR_EXEMPT.items():
        module, funcname, types, expr = key
        if _MIRROR_REASON in why:
            twin = ("webrunner_novelai.py", funcname, types, expr)
            if module == "webrunner_novelai.py":
                thin.append(key)            # novelai 不能拿自己當交叉引用
            elif twin not in _EXCEPTION_REPR_EXEMPT:
                dangling.append(key)
            elif len(_EXCEPTION_REPR_EXEMPT[twin][1].strip()) < 15:
                dangling.append(key)
        elif len(why.strip()) < 15:
            thin.append(key)
    assert not thin, f"這些豁免沒有寫理由：{sorted(thin)}"
    assert not dangling, (
        f"這些豁免寫「{_MIRROR_REASON}」，但 selenium 變體那一筆已經不在了"
        f"（或它自己也沒寫理由）：{sorted(dangling)}")


def test_the_mirror_cross_reference_is_actually_checked():
    """正面對照組：上一支那條交叉引用要真的咬得到人。

    現況是乾淨的，所以「沒有懸空的交叉引用」這句斷言本身永遠成立——刪掉它也會
    綠。用合成的表問一次「novelai 那一筆不見了會怎樣」。
    """
    twinless = ("webrunner_je_only.py", "_nonexistent_twin", ("OSError",), "e")
    assert _MIRROR_REASON not in _EXCEPTION_REPR_EXEMPT.get(
        twinless, (0, ""))[1], "合成鍵不該真的存在於表裡"
    # 把判斷抽出來對合成資料跑一次，不動正式的表。
    fake = {twinless: (1, f"{_MIRROR_REASON}。")}
    dangling = [k for k, (_, why) in fake.items()
                if _MIRROR_REASON in why
                and ("webrunner_novelai.py", k[1], k[2], k[3]) not in fake]
    assert dangling == [twinless], "交叉引用的對帳邏輯抓不到懸空的那一筆"


@pytest.mark.parametrize("key,formatter",
                         sorted(_DRIVER_FACING_FORMATTER.items()))
def test_the_driver_facing_sites_use_the_formatter_they_were_assigned(
        key, formatter):
    """改掉的站點要**留在**改掉的狀態，而且是選定的那一支格式器。

    只釘「沒有 `!r`」擋不住「好心統一成 `full_error_detail`」——那會讓
    `with_retry` 這種每個 DOM 步驟都印一次的行，每次吐一整段 `Stacktrace:`
    進 `webrunner.log`，正是 `_one_line_error` 的 docstring 在防的事。
    """
    module, funcname = key
    path = _PKG / module
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == funcname), None)
    assert func is not None, (
        f"`{module}` 裡找不到 `{funcname}`——改名了的話 "
        "`_DRIVER_FACING_FORMATTER` 要跟著改。")

    used = {ast.unparse(n.func) for n in ast.walk(func)
            if isinstance(n, ast.Call)
            and ast.unparse(n.func).split(".")[-1] in {
                "_short_error", "_long_error", "full_error_detail"}}
    short = {u.split(".")[-1] for u in used}
    assert formatter in short, (
        f"`{module}.{funcname}` 應該用 `{formatter}` 格式化被捕捉的例外，"
        f"現在用的是 {sorted(short) or '（完全沒有用格式器）'}。")


@pytest.mark.parametrize("key", sorted(_MIXED_FORMATTER_SITES))
def test_the_mixed_site_keeps_both_of_its_formatters(key):
    """`serve_single_image_request` 一支裡短的長的都要在。

    清空角色框那一行在 `for area in ...` 迴圈裡（短的），外層那一行是整個請求的
    終結性回報（長的）。統一成任何一支都會弄丟其中一邊的理由。
    """
    module, funcname = key
    path = _PKG / module
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == funcname), None)
    assert func is not None, f"`{module}` 裡找不到 `{funcname}`"
    used = {ast.unparse(n.func).split(".")[-1] for n in ast.walk(func)
            if isinstance(n, ast.Call)}
    for formatter in _MIXED_FORMATTER_SITES[key]:
        assert formatter in used, (
            f"`{module}.{funcname}` 少了 `{formatter}`——"
            f"現在只剩 {sorted(used & {'_short_error', '_long_error'})}。")


def test_the_extractor_catches_a_violation_injected_into_fresh_source():
    """範圍釘樁：抽取器要看得見一個**新**的違規，不只是描述現況。

    只測「把現有站點改壞」的話，一個根本沒掃到生產檔的抽取器照樣滿分。
    """
    sample = (
        "def f(port):\n"
        "    try:\n"
        "        port.execute_script('x')\n"
        "    except Exception as error:\n"
        "        print(f'boom {error!r}')\n"
    )
    found = _exception_repr_sites(sample, "synthetic.py")
    assert found == {("synthetic.py", "f", ("Exception",), "error"): 1}, found


def test_the_extractor_does_not_cry_wolf_on_a_non_exception_repr():
    """`f"{path!r}"` 印路徑是合理的，抽取器不准把它算進來。

    會亂叫的守門遲早會被關掉——本檔的 `_bare_exception_interpolations` 的
    docstring 已經寫過同一句，這裡用合成語料把它釘成可執行的。
    """
    sample = (
        "def f(path):\n"
        "    try:\n"
        "        path.unlink()\n"
        "    except OSError as error:\n"
        "        print(f'{path!r} gone: {error}')\n"
    )
    assert _exception_repr_sites(sample, "synthetic.py") == {}
