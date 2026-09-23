"""`webrunner_je_only.py` 呼叫的每一個 `wr.X`，`je_web_runner` 都要真的有。

本專案已經有兩支同形狀的守門——`test_selenium_facade.py`（呼叫端 vs 裝著的 selenium）
與 `test_gui_facade.py`（呼叫端 vs 裝著的桌面自動化函式庫）——但第三個外部介面
`je_web_runner` 一直沒有，而它剛好是**當時真的對不上**的那一個。

2026-08-30 量出來的狀況：`webrunner_je_only.py` 有四個 `wr.*` 呼叫點
（`get_current_url` / `get_title` / `save_screenshot` /
`add_script_to_evaluate_on_new_document`）在**當時安裝的 0.0.79 上根本不存在**。
它沒有炸，只是因為這台機器上剛好有一份 sibling checkout（`D:\\Work\\WebRunner`），
而模組開頭會把它 `sys.path.insert(0, ...)`——所以**被 import 的是開發用的簽出，不是
宣告出來的那個套件**。checkout 一旦搬走、改名，或換一台只照 `requirements.txt` 裝的
機器，那四個呼叫就會變成 `AttributeError`，而且是在無人值守的批次跑到一半的時候。
（`requirements.txt` 沒有釘版本，所以 fresh clone 本來就會拿到夠新的；壞的是「這台
機器上裝著的那一份太舊」這個中間狀態。已於同日把 `.venv` 升到 0.0.88 補起來，
`selenium` 沒有被動到——它是正式 webrunner 正在用的東西。）

所以這裡查兩個方向，而且**刻意分成兩支**，因為它們回答的是不同的問題：

1. `test_every_wr_call_exists_on_the_imported_package`——「這台機器現在跑起來會不會
   炸」。用模組自己的解析順序（sibling / `WEBRUNNER_PATH` 優先）。
2. `test_every_wr_call_exists_on_the_installed_package`——「只照 `requirements.txt`
   裝的機器會不會炸」。跑一個把 sibling 路徑排掉的子行程去問。

第 2 支才是這一輪紅起來的那一支；只寫第 1 支的話，這個問題會繼續被 checkout 蓋著。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PKG_ROOT.parent
_JE = PKG_ROOT / "webrunner_je_only.py"

# `webrunner_je_only.py` 自己的解析順序，原封不動抄過來——抄錯就等於沒在測同一件事。
_WR_SIBLING = REPO_ROOT.parent / "WebRunner"
_WR_PATH = Path(os.environ["WEBRUNNER_PATH"]) if os.environ.get("WEBRUNNER_PATH") \
    else _WR_SIBLING


def _wr_attributes() -> list[tuple[int, str]]:
    """原始碼裡每一次 `wr.<名字>`，回 [(行號, 名字), …]。

    `wr` 是 `webdriver_wrapper_instance` 的別名（模組開頭就綁死），所以掃
    `Attribute(value=Name(id="wr"))` 就等於掃「對那個 wrapper 的每一次存取」。
    """
    tree = ast.parse(_JE.read_text(encoding="utf-8"), str(_JE))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "wr"):
            out.append((node.lineno, node.attr))
    return sorted(set(out))


def _missing_in_subprocess(sys_path_extra: list[str], drop_sibling: bool
                           ) -> tuple[list[str], str, str]:
    """在子行程裡 import `je_web_runner`，回 (缺的名字, 版本, 檔案位置)。

    用子行程而不是在測試行程裡動 `sys.path`：這個套件會 import 一堆子模組，
    在同一個行程裡換路徑再 import 會被 `sys.modules` 的殘留騙過去。
    """
    names = sorted({name for _lineno, name in _wr_attributes()})
    script = (
        "import json, sys\n"
        f"drop = {drop_sibling!r}\n"
        f"extra = {sys_path_extra!r}\n"
        "if drop:\n"
        "    bad = {str(p).lower() for p in extra}\n"
        "    sys.path = [p for p in sys.path if str(p).lower() not in bad]\n"
        "else:\n"
        "    for p in reversed(extra):\n"
        "        sys.path.insert(0, p)\n"
        "import je_web_runner as m\n"
        "from je_web_runner import webdriver_wrapper_instance as wr\n"
        f"names = {names!r}\n"
        "print(json.dumps({\n"
        "    'missing': [n for n in names if not hasattr(wr, n)],\n"
        "    'file': m.__file__,\n"
        "    'version': getattr(m, '__version__', '?'),\n"
        "}))\n"
    )
    proc = subprocess.run(  # nosec B603
        [sys.executable, "-c", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if proc.returncode != 0:
        pytest.skip(f"這個直譯器 import 不到 je_web_runner：{proc.stderr[-300:]}")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    return data["missing"], data["version"], data["file"]


def test_the_call_sites_are_actually_findable():
    """掃描器本身要先被證明有在掃到東西——空清單通過是最沒用的綠燈。"""
    attrs = _wr_attributes()
    assert len(attrs) >= 8, (
        f"只掃到 {len(attrs)} 個 `wr.*` 呼叫點，太少了。"
        "是不是別名從 `wr` 改掉了？改了的話這支測試要跟著改，不然它會一直是綠的。")
    names = {name for _lineno, name in attrs}
    for expected in ("find_element", "execute_script", "quit"):
        assert expected in names, f"`wr.{expected}` 不見了，掃描器可能壞了"


def test_every_wr_call_exists_on_the_imported_package():
    """這台機器現在跑起來會不會炸——用模組自己的解析順序（sibling 優先）。"""
    extra = [str(_WR_PATH)] if _WR_PATH.exists() else []
    missing, version, where = _missing_in_subprocess(extra, drop_sibling=False)
    assert not missing, (
        f"`webrunner_je_only.py` 呼叫了 {missing}，但 import 到的 je_web_runner"
        f"（{version}，{where}）沒有。這種錯是**執行期**才會炸的 `AttributeError`，"
        "而執行期就是無人值守的批次跑到一半。")


def test_every_wr_call_exists_on_the_installed_package():
    """只照 `requirements.txt` 裝的機器會不會炸——把 sibling checkout 排掉再問。

    這一條跟上一條分開，是因為它們會給出不同的答案，而**分歧本身就是問題**：
    「能跑」如果是靠一份沒有宣告出來的開發簽出，那是巧合不是設計。
    """
    if not _WR_PATH.exists():
        pytest.skip("這台機器沒有 sibling checkout，兩支測試會問到同一個套件")
    missing, version, where = _missing_in_subprocess(
        [str(_WR_PATH)], drop_sibling=True)
    assert "site-packages" in where.lower() or "dist-packages" in where.lower(), (
        f"想問的是安裝的那一份，實際問到 {where}——排除 sibling 的邏輯沒生效")
    assert not missing, (
        f"安裝的 je_web_runner（{version}，{where}）少了 {missing}。"
        "這台機器目前跑得起來只是因為 `webrunner_je_only.py` 會把 sibling checkout "
        f"（{_WR_PATH}）插到 `sys.path[0]`——checkout 一搬走，或換一台只照 "
        "`requirements.txt` 裝的機器，這些呼叫就會變成執行期的 `AttributeError`。"
        "解法是把安裝的套件升級到有這些方法的版本，不是把呼叫點刪掉。")


def test_the_test_object_import_still_resolves():
    """`TestObject` 是每一次 find_element 都會用到的入口型別。"""
    extra = [str(_WR_PATH)] if _WR_PATH.exists() else []
    script = ("from je_web_runner import TestObject\n"
              "TestObject('//x', 'xpath')\n"
              "print('ok')\n")
    if extra:
        script = f"import sys; sys.path.insert(0, {extra[0]!r})\n" + script
    proc = subprocess.run(  # nosec B603
        [sys.executable, "-c", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 0, (
        f"`TestObject(xpath, 'xpath')` 建不起來：{proc.stderr[-300:]}")


# ---------------------------------------------------------------------------
# chromedriver 的記錄檔：je 這一側靠 `set_driver` 的 `**kwargs` 才拿得到
# ---------------------------------------------------------------------------
# 2026-09-09：`webrunner_je_only.py` 原本**完全沒有** chromedriver 記錄檔的處理，
# 於是 Chrome 起不來時 stderr 只有一句猜測（「likely Out of Memory or a
# chromedriver/Chrome version mismatch」）——而 selenium 變體那句一樣的訊息旁邊
# 有整份 driver 記錄可讀。差別很要緊，因為 bot 的 `/run` **預設就是 je 變體**，
# 而 `_watch_for_fallback` 會在 5 分鐘的 startup window 內靜靜轉跑 selenium 變體：
# 使用者看到產圖繼續，永遠不會問「je 為什麼死掉」。
#
# 能補起來的唯一理由是 `WebDriverWrapper.set_driver` 把 `**kwargs` **原封不動**
# 轉給瀏覽器類別：`webdriver_value(options=driver_options, **kwargs)`。所以
# `service=ChromeService(log_output=…)` 會一路變成 chromedriver 的 `--log-path=`。
# 那是**上游的實作細節，不是它承諾的介面**——`set_driver` 的簽名沒有 `service`
# 這個參數，所以下面兩支測試就是那條假設的守門。上游哪天改成只挑幾個 kwargs 往下
# 傳，它們會紅；沒有它們的話，症狀是某次無人值守的 spawn 失敗時記錄檔又是空的。

def _kwargs_passthrough_in_subprocess(sys_path_extra: list[str],
                                      drop_sibling: bool) -> dict:
    """在子行程裡問「`set_driver` 會不會把 `service=` 轉給瀏覽器類別」。

    不開 Chrome：把 je_web_runner 派送用的瀏覽器類別換成一個只記錄 kwargs 的替身，
    順便把 webdriver_manager 的安裝器換掉（它會去網路上找 driver）。
    """
    script = (
        "import json, sys\n"
        f"drop = {drop_sibling!r}\n"
        f"extra = {sys_path_extra!r}\n"
        "if drop:\n"
        "    bad = {str(p).lower() for p in extra}\n"
        "    sys.path = [p for p in sys.path if str(p).lower() not in bad]\n"
        "else:\n"
        "    for p in reversed(extra):\n"
        "        sys.path.insert(0, p)\n"
        "import je_web_runner as m\n"
        "import je_web_runner.webdriver.webdriver_wrapper as ww\n"
        "from je_web_runner import webdriver_wrapper_instance as wr\n"
        "out = {'file': ww.__file__, 'version': getattr(m, '__version__', '?')}\n"
        "if not hasattr(ww, '_webdriver_dict'):\n"
        "    out['error'] = 'no _webdriver_dict — 派送機制換掉了'\n"
        "    print(json.dumps(out)); sys.exit(0)\n"
        "captured = {}\n"
        "class Recorder:\n"
        "    def __init__(self, **kw):\n"
        "        captured.update(kw)\n"
        "        self.capabilities = {}\n"
        "ww._webdriver_dict['chrome'] = Recorder\n"
        "if hasattr(ww, '_webdriver_manager_dict'):\n"
        "    ww._webdriver_manager_dict['chrome'] = (\n"
        "        lambda **kw: type('M', (), {'install': lambda self: 'x'})())\n"
        "sentinel = object()\n"
        "try:\n"
        "    wr.set_driver('chrome', options=['--start-maximized'],\n"
        "                  experimental_options={'useAutomationExtension': False},\n"
        "                  service=sentinel)\n"
        "except Exception as err:\n"
        "    out['error'] = repr(err)\n"
        "out['seen'] = sorted(captured)\n"
        "out['forwarded'] = captured.get('service') is sentinel\n"
        "print(json.dumps(out))\n"
    )
    proc = subprocess.run(  # nosec B603
        [sys.executable, "-c", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if proc.returncode != 0:
        pytest.skip(f"這個直譯器 import 不到 je_web_runner：{proc.stderr[-300:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_forwards(data: dict, where_hint: str) -> None:
    assert not data.get("error"), (
        f"{where_hint}：`set_driver(..., service=…)` 直接失敗了——{data['error']}。"
        "je 變體靠這條路才拿得到 chromedriver 的記錄檔。")
    assert data["forwarded"], (
        f"{where_hint}（{data['version']}，{data['file']}）的 `set_driver` 不再把 "
        f"`service=` 轉給瀏覽器類別了（它收到的是 {data['seen']}）。"
        "`webrunner_je_only.start_driver` 就是靠這個把 "
        "`ChromeService(log_output=…)` 塞進去的；轉不過去的話 chromedriver 的記錄"
        "**不會有任何錯誤**，只是那個檔從此不存在，而它唯一的用途正是 spawn 失敗"
        "時看 root cause。要嘛請上游具名支援，要嘛改走 "
        "`je_web_runner.build_stealth_chrome_driver(chromedriver_log=…)`。")


def test_set_driver_forwards_the_service_kwarg_on_the_imported_package():
    """這台機器現在跑起來，je 變體拿不拿得到 chromedriver 記錄檔。"""
    extra = [str(_WR_PATH)] if _WR_PATH.exists() else []
    _assert_forwards(_kwargs_passthrough_in_subprocess(extra, drop_sibling=False),
                     "import 到的 je_web_runner")


def test_set_driver_forwards_the_service_kwarg_on_the_installed_package():
    """只照 `requirements.txt` 裝的機器上也要成立。

    跟上面 `wr.*` 那一對測試分開是同一個理由：兩者可能給出不同答案，而**分歧本身
    就是問題**——「拿得到記錄檔」如果只在有 sibling checkout 的機器上成立，那是巧合
    不是設計，而症狀會是另一台機器上的 spawn 失敗永遠查不到原因。
    """
    if not _WR_PATH.exists():
        pytest.skip("這台機器沒有 sibling checkout，兩支測試會問到同一個套件")
    data = _kwargs_passthrough_in_subprocess([str(_WR_PATH)], drop_sibling=True)
    where = data["file"].lower()
    assert "site-packages" in where or "dist-packages" in where, (
        f"想問的是安裝的那一份，實際問到 {data['file']}——排除 sibling 的邏輯沒生效")
    _assert_forwards(data, "安裝的 je_web_runner")


def test_the_je_variant_really_asks_for_a_driver_log():
    """AST：`start_driver` 真的把 `service=` 傳進 `wr.set_driver(...)`。

    上面兩支證明「這條路通」，這一支證明「我們真的走了這條路」。兩件事都要——
    上游支援得再好，呼叫點沒傳就等於沒有，而那正是這個缺口原本的樣子。
    """
    tree = ast.parse(_JE.read_text(encoding="utf-8"), str(_JE))
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "start_driver"),
                None)
    assert func is not None, "`start_driver` 不見了——這支測試要跟著改"

    calls = [n for n in ast.walk(func)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "set_driver"]
    assert calls, "`start_driver` 裡找不到 `wr.set_driver(...)`"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords if kw.arg}
        assert "service" in kwargs, (
            f"`wr.set_driver(...)`（第 {call.lineno} 行）沒有傳 `service=`——"
            "chromedriver 的記錄檔就不會被寫出來，spawn 失敗時只剩下一句猜測。"
            f"目前傳的是：{sorted(kwargs)}")
