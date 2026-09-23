"""兩支 webrunner 變體的 `BrowserPort` 必須維持同一個介面。

CLAUDE.md 的核心不變量之一是「兩個 webrunner 變體要保持同步」，但這件事一直只靠
code review 與 agent 檔的敘述在守。`_webrunner_shared.py` 的每一支共用函式都拿
`port` 當參數，兩邊各自提供實作——只在其中一邊加方法，另一邊要到**執行期**才會
`AttributeError`，而那個執行期是「無人值守的批次跑到一半」。

三個方向都掃：

1. 兩邊的方法集合要一致（`__init__` / `set_driver` 是**寫明理由的**例外）。
2. 同名方法的參數名要一致——共用葉子是具名呼叫的（`port.click(el, pause=0.15)`），
   參數改名一樣是執行期才炸。
3. 共用模組對 `port.X` 的每一次存取，兩邊都要有 X。

還有第四個方向，性質不同：`getattr(port, "x", None)` 這種**可選**呼叫，如果兩邊
都沒有 x，就會安靜地什麼都不做——沒有錯誤、沒有記錄。本專案這個 session 已經踩過
兩次同樣形狀的坑（selenium 的 `log_path=` 被 `**kwargs` 吞掉、
`_dump_chromedriver_log_tail` 的 `except OSError: return`），所以這裡直接把它變成
紅燈。

第五個方向是 2026-09-01 用覆蓋率量出來的：上面全部都是 **AST 分析**，一行都沒有真的
`import` 過。實測 `webrunner_je_only.py` 的覆蓋率是 **0%**（458 個 statement，一個都
沒跑過），而 `webrunner_novelai.py` 有 15%——因為 `test_selenium_facade` 會 import 它。
也就是說第二個變體連「import 得起來嗎」都沒人問過：模組層的語法錯誤、寫錯的
`from X import Y`、被移除的第三方符號，全部要等到有人真的切到那個變體、在無人值守的
批次裡才會爆。DoD #1 的冒煙指令原本也只寫 `discord_bot, webrunner_novelai`，剛好漏掉
同一支。所以這裡補兩支：一支真的在乾淨的子行程裡 import 兩個變體，一支釘住 CLAUDE.md
的那行指令必須涵蓋每一個變體。

**本檔還帶著一條「跟變體無關」的規則，理由寫在這裡（2026-09-09）。**
下半段那組 transport／gone 對帳（`_SESSION_GONE_EXC_NAMES` 的每個名字都要被
`TRANSPORT_ERRORS` 抓得到）原本 parametrize 在 `_VARIANTS` 上，也就是**規則跟模組
無關、範圍卻寫死兩個檔名**——同一個家族的第三個實例。實際上
專案裡有**第三個真的 port**：`verify_quota_dialog.Port`。它被丟進 `_webrunner_shared`
的 hot-path helper（`get_generation_block` / `has_blocking_dialog` /
`dismiss_blocking_dialog` / `describe_dialog_controls` / `_is_chrome_crash_page`，
每一支都有 `except port.TRANSPORT_ERRORS`），卻連掃都掃不到：`_browser_port()` 只認
**類別名剛好叫 `BrowserPort`** 的節點，而它叫 `Port`。實測那份 tuple 停在修正前的
`(WebDriverException, OSError)`，gone 名單裡的四個 urllib3 名字一個都抓不到。

所以範圍改成**機制**：掃全專案非測試模組，每一個在類別層指派 `TRANSPORT_ERRORS`
的類別都要過同一條規則。判準留在本檔而不另開新檔，是因為名字→類別的解析機制
（`_EXC_LOOKUP_MODULES` / `_resolve_exception` / `_TRANSPORT_NOT_GONE`）就住在這裡，
搬走等於把它抄成兩份——而「抄本」正是本檔在防的東西。上半段仍然只講兩個變體。
"""
from __future__ import annotations

import ast
import os
import re
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PKG_ROOT.parent
_VARIANTS = ("webrunner_novelai.py", "webrunner_je_only.py")
_SHARED = PKG_ROOT / "_webrunner_shared.py"

# 只在 novelai 那一側存在，且有理由：
#   `__init__` / `set_driver` —— selenium 變體的 driver 物件會在重啟時被**換掉**，
#   所以 port 需要一個重新指向的鉤子；je 變體的 port 包的是 `wr` 這個 singleton，
#   底層 driver 換掉時 port 自動跟著走，不需要（也不該有）這兩個。
_NOVELAI_ONLY = frozenset({"__init__", "set_driver"})

# 類別屬性（不是方法），共用模組讀得到就好。
_CLASS_ATTRS = frozenset({"TRANSPORT_ERRORS"})


def _browser_port(path: Path) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "BrowserPort":
            return node
    raise AssertionError(f"{path.name} 裡找不到 BrowserPort——是不是改名了？"
                         "改名的話這支測試要跟著改。")


def _methods(path: Path) -> dict[str, list[str]]:
    """{方法名: [參數名（去掉 self）]}"""
    out: dict[str, list[str]] = {}
    for item in _browser_port(path).body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in item.args.args if a.arg != "self"]
            out[item.name] = args
    return out


def _class_attr_names(path: Path) -> set[str]:
    names = set()
    for item in _browser_port(path).body:
        if isinstance(item, ast.Assign):
            names.update(t.id for t in item.targets if isinstance(t, ast.Name))
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            names.add(item.target.id)
    return names


def _shared_port_attrs() -> tuple[set[str], set[str]]:
    """共用模組對 `port` 的存取：(直接存取的, 透過 getattr 取的可選的)。"""
    tree = ast.parse(_SHARED.read_text(encoding="utf-8"), str(_SHARED))
    direct: set[str] = set()
    optional: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name) and node.args[0].id == "port"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            optional.add(node.args[1].value)
        elif (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name) and node.value.id == "port"):
            direct.add(node.attr)
    return direct - optional, optional


@pytest.fixture(scope="module")
def ports() -> dict[str, dict[str, list[str]]]:
    return {name: _methods(PKG_ROOT / name) for name in _VARIANTS}


def _stale_port_exemptions(novelai, je, exempt) -> list:
    """`_NOVELAI_ONLY` 裡對不上自己前提的那些。

    前提是「**novelai 有、je 沒有**」。一筆對不上的豁免是 **fail-open**：它不會讓
    任何東西變紅（減掉一個本來就不在集合裡的名字是 no-op），但它會**預先授權**未來
    某個真的違規——哪天有人重新用到那個方法名，一個只有 novelai 有的 port 方法就被
    靜靜放行，而 je 變體會在執行期 `AttributeError`，也就是無人值守的批次跑到一半。
    `CLAUDE.md` 對 `_OWNER_ONLY_SLASH` 記的是同一個形狀。

    抽成純函式的理由同本 repo 其他幾支：真實資料上它恆為空集合，所以斷言本身證明不了
    自己在咬，牙齒要長在一個合成語料問得到的地方。
    """
    return sorted(n for n in exempt if n not in novelai or n in je)


def test_the_port_exemption_staleness_check_actually_bites():
    """對照組：乾淨資料讓上面那句斷言證明不了自己在咬。"""
    both = {"click": [], "quit": []}
    novelai_only = {**both, "set_driver": []}

    # 乾淨：豁免的那個確實是「novelai 有、je 沒有」。
    assert _stale_port_exemptions(novelai_only, both, {"set_driver"}) == []

    # 兩邊都有 → 這筆豁免沒有在豁免任何東西（最常見的過期形狀）。
    assert _stale_port_exemptions(novelai_only, both, {"click"}) == ["click"]

    # 兩邊都沒有 → 改名之後留下的字串。
    assert _stale_port_exemptions(novelai_only, both, {"gone"}) == ["gone"]

    # 多筆時要一次全報，不要只報第一筆。
    assert _stale_port_exemptions(
        novelai_only, both, {"click", "gone", "set_driver"}) == ["click", "gone"]


def test_the_two_ports_expose_the_same_methods(ports):
    """單邊新增方法 = 另一支變體執行到一半才 AttributeError。"""
    novelai, je = ports[_VARIANTS[0]], ports[_VARIANTS[1]]
    # 反方向（2026-09-11 補）。`only_novelai` 那句只看得到「沒被豁免的違規」，
    # 看不到「豁免清單自己過期了」。實測：把 `click`（兩個 port 都有）塞進
    # `_NOVELAI_ONLY`，整個 `test_variant_parity.py` 的 31 支測試**照樣全綠**。
    stale = _stale_port_exemptions(novelai, je, _NOVELAI_ONLY)
    assert not stale, (
        f"`_NOVELAI_ONLY` 這幾筆已經對不上它的前提（novelai 有、je 沒有）：{stale}。"
        "豁免是按**名字**放行的，所以一筆過期的豁免會在有人重新用到那個名字時安靜地"
        "放它過去——守門照跑、集合還在、測試全綠。請刪掉，或改成仍然成立的名字。")
    only_novelai = sorted(set(novelai) - set(je) - _NOVELAI_ONLY)
    only_je = sorted(set(je) - set(novelai))
    assert not only_novelai, (
        f"這些方法只有 webrunner_novelai.BrowserPort 有：{only_novelai}。"
        "共用模組一旦用到，je 變體就會在執行期炸。要嘛兩邊都加，要嘛把它列進 "
        "test_variant_parity._NOVELAI_ONLY 並寫清楚理由。")
    assert not only_je, (
        f"這些方法只有 webrunner_je_only.BrowserPort 有：{only_je}。"
        "兩支變體的 port 介面必須對稱。")


def test_the_two_ports_agree_on_parameter_names(ports):
    """共用葉子是具名呼叫的（`port.click(el, pause=...)`），參數改名一樣會炸。"""
    novelai, je = ports[_VARIANTS[0]], ports[_VARIANTS[1]]
    drift = {name: (novelai[name], je[name])
             for name in sorted(set(novelai) & set(je))
             if novelai[name] != je[name]}
    assert not drift, (
        f"同名方法的參數名兩邊不一致：{drift}。共用模組用關鍵字呼叫，"
        "名字不同就等於只有一邊接得住。")


def test_every_port_call_in_the_shared_module_exists_on_both_ports(ports):
    """`_webrunner_shared` 用到的每個 `port.X`，兩邊都要有。"""
    direct, _ = _shared_port_attrs()
    missing = {}
    for name in _VARIANTS:
        have = set(ports[name]) | _class_attr_names(PKG_ROOT / name) | _CLASS_ATTRS
        gap = sorted(direct - have)
        if gap:
            missing[name] = gap
    assert not missing, (
        f"共用模組會存取這些 port 屬性，但變體沒有：{missing}。"
        "這在無人值守的批次裡是執行期 AttributeError。")


def test_optional_port_calls_exist_on_at_least_one_port(ports):
    """`getattr(port, "x", None)` 的 x 兩邊都沒有 = 安靜地什麼都不做。

    可選呼叫的用意是「舊的／精簡的 port 沒有也無妨」，不是「這個功能其實沒接
    上」。兩邊都缺就代表寫了一段永遠不會執行的程式，而且不會有任何錯誤或記錄。
    """
    _, optional = _shared_port_attrs()
    if not optional:
        pytest.skip("共用模組目前沒有可選的 port 呼叫")
    orphan = []
    for attr in sorted(optional):
        if not any(attr in ports[name] for name in _VARIANTS):
            orphan.append(attr)
    assert not orphan, (
        f"共用模組用 getattr 取這些 port 方法，但**兩支變體都沒有**：{orphan}。"
        "這段程式永遠不會執行，而且是安靜的——沒有例外、沒有 log。")


# ---------------------------------------------------------------------------
# 「分類表」與「捕捉子」要對得起來
# ---------------------------------------------------------------------------
# 2026-09-09：`_webrunner_shared._SESSION_GONE_EXC_NAMES` 列著 `MaxRetryError` 與
# `NewConnectionError`，但兩個變體的 `_DRIVER_TRANSPORT_ERRORS` 是
# `(WebDriverException, ReadTimeoutError, OSError)`——而那兩個類別的 MRO 是
# `RequestError → PoolError → HTTPError` 與 `ConnectTimeoutError → TimeoutError →
# HTTPError`，**都不經過 `OSError`**。於是那兩個名字寫在表裡、表卻永遠沒機會發言：
# hot path 的 `except port.TRANSPORT_ERRORS` 收不到 → `_note_transport_error`
# 不會被呼叫 → 沒有 `BrowserGoneError`。正式環境的樣子是 09-07 三次
# `critical_error`，message 是生的 `MaxRetryError`，沒有分類前綴、沒有 `where`。
#
# 這是本專案反覆出現的同一個形狀：**一條寫在資料表／docstring 裡、沒有任何東西
# 執行的規則**。所以下面兩支釘的是「形狀」而不是這一次的兩個名字。
#
# 名字→類別的解析刻意**不 import 變體模組**（那會把 selenium 與 sibling checkout
# 的 je_web_runner 一起拉進來）：從變體的 AST 讀 tuple 的元素名 ＋ 讀 import 別名，
# 再去真的套件裡把類別取出來，最後用 `issubclass` 判定。

_EXC_LOOKUP_MODULES = ("builtins", "selenium.common.exceptions",
                       "urllib3.exceptions")

# `ReadTimeoutError` 在 tuple 裡但**刻意不**算 session gone：連上了、請求送出了、
# 只是 120 秒沒回話 ＝ chromedriver 還活著，那是「卡頓」的正典。把它列進 gone
# 會讓一次忙碌變成一次重生（外加一次登入 ＋ 重填欄位）。
_TRANSPORT_NOT_GONE = frozenset({"ReadTimeoutError"})


def _resolve_exception(name: str):
    """名字 → 真的例外類別。找不到就 `None`（呼叫端一律當成錯，不得靜靜跳過）。"""
    import importlib
    for mod_name in _EXC_LOOKUP_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:      # pragma: no cover - 套件都是必要相依
            continue
        cls = getattr(mod, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            return cls
    return None


# 掃描範圍：全專案的**非測試**模組。判準是機制不是檔名清單——任何一個真的會被丟進
# `_webrunner_shared` 共用 helper 的 port，它的 `TRANSPORT_ERRORS` 都要抓得到 gone
# 名單的每一個名字。範圍沿用 `test_text_encoding._SKIP_DIRS` 的慣例。
_SKIP_DIRS = {"legacy", ".venv", ".git", "node_modules", "output", "docs",
              ".backup", ".chrome_profile", ".chrome_profile_snap"}


def _project_sources() -> list[Path]:
    """全專案（含 repo root）的 .py，扣掉參考用舊碼、虛擬環境與執行期產物。"""
    return sorted(p for p in REPO_ROOT.rglob("*.py")
                  if not (_SKIP_DIRS & set(p.relative_to(REPO_ROOT).parts)))


def _is_test_source(path: Path) -> bool:
    """測試檔裡的假 port **不算**。

    `test_webrunner_shared.py` 有好幾個替身把 `TRANSPORT_ERRORS` 寫成 `()` 或
    `(RuntimeError,)`——那是**刻意**的：替身要嘛什麼都不抓（讓例外炸穿好斷言），
    要嘛只抓自己造的假例外。它們從來不會碰到真的 chromedriver，套真規則只會全部
    變紅，然後有人把整支守門關掉。排除的是「測試替身」這個類別，不是某幾個名字。
    """
    return path.name.startswith("test_") or path.name == "conftest.py"


class _PortSpec(NamedTuple):
    """一個真的 port 的 `TRANSPORT_ERRORS`。`elements` 的別名已還原成原始類別名。"""
    module: str
    cls: str
    elements: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.module}::{self.cls}"


def _transport_ports_in_source(source: str, filename: str) -> list[_PortSpec]:
    """純函式：原始碼 → 這個檔案裡每一個類別層 `TRANSPORT_ERRORS` 的元素名。

    兩種寫法都要收，因為專案裡兩種都有：
      * 指到模組層的字面 tuple（`TRANSPORT_ERRORS = _DRIVER_TRANSPORT_ERRORS`，
        兩個變體），
      * 直接寫在類別裡的字面 tuple（`verify_quota_dialog.Port`）。

    `import X as Y` 的別名要還原回 `X`，否則 `Urllib3MaxRetryError` 這種名字在任何
    套件裡都查不到。走 AST 是為了不 import 被掃的模組本身（那會把 selenium 與
    sibling checkout 的 `je_web_runner` 一起拉進來）。
    """
    tree = ast.parse(source, filename)
    alias_to_real: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                alias_to_real[alias.asname or alias.name] = alias.name

    module_tuples: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):
            names = [e.id for e in node.value.elts if isinstance(e, ast.Name)]
            for target in node.targets:
                if isinstance(target, ast.Name):
                    module_tuples[target.id] = names

    found: list[_PortSpec] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, ast.Assign):
                assigned = [t.id for t in item.targets if isinstance(t, ast.Name)]
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                assigned = [item.target.id]
            else:
                continue
            if "TRANSPORT_ERRORS" not in assigned:
                continue
            value = item.value
            if isinstance(value, ast.Tuple):
                elements = [e.id for e in value.elts if isinstance(e, ast.Name)]
            elif isinstance(value, ast.Name):
                elements = module_tuples.get(value.id)
                assert elements is not None, (
                    f"{filename} 的 {node.name}.TRANSPORT_ERRORS 指到 "
                    f"{value.id!r}，但那個名字不是同檔案模組層的字面 tuple——"
                    "這支掃描器讀不到它的內容，等於這個 port 沒被檢查。")
            else:
                raise AssertionError(
                    f"{filename} 的 {node.name}.TRANSPORT_ERRORS 不是字面 tuple "
                    f"也不是模組層 tuple 的名字（{ast.dump(value)[:60]}…）——"
                    "掃描器要跟著改，不要讓它安靜地略過一個真的 port。")
            found.append(_PortSpec(
                Path(filename).name, node.name,
                tuple(alias_to_real.get(n, n) for n in elements)))
    return found


def _discover_transport_ports() -> list[_PortSpec]:
    ports: list[_PortSpec] = []
    for path in _project_sources():
        if _is_test_source(path):
            continue
        ports.extend(_transport_ports_in_source(
            path.read_text(encoding="utf-8"), str(path)))
    return sorted(ports)


_TRANSPORT_PORTS = _discover_transport_ports()

# ---- 掃描「範圍」的下限。刻意跟掃描器放在一起 -------------------------------
# 真實資料乾淨的時候，「掃三個 port」與「掃兩個」的結果**一模一樣**——兩支對帳測試
# 都會綠。所以範圍要另外釘，而且下限與掃描器分開放就會有人只改一邊。
_MIN_TRANSPORT_PORTS = 3

# 掃描一定要看得到的 port。少一個就代表 `_SKIP_DIRS` / `_is_test_source` 挖太寬，
# 或掃描器讀不懂某種寫法——兩種都是安靜的。
_MUST_SCAN_PORTS = {
    ("webrunner_novelai.py", "BrowserPort"),
    ("webrunner_je_only.py", "BrowserPort"),
    # 第三個真的 port。它的類別名叫 `Port` 不叫 `BrowserPort`，所以**任何以類別名
    # 找節點的掃描都看不到它**——2026-09-09 之前它的 tuple 就這樣停在修正前的形狀
    # `(WebDriverException, OSError)`，gone 名單裡四個 urllib3 名字一個都抓不到。
    ("verify_quota_dialog.py", "Port"),
}


def _port_transport_classes(spec: _PortSpec) -> dict[str, type]:
    """`_PortSpec` → {原始類別名: 真的例外類別}。解析不到一律算失敗。"""
    resolved: dict[str, type] = {}
    for name in spec.elements:
        cls = _resolve_exception(name)
        assert cls is not None, (
            f"{spec.label} 的 TRANSPORT_ERRORS 有一個 {name!r}，但在 "
            f"{list(_EXC_LOOKUP_MODULES)} 裡都找不到這個例外類別。要嘛打錯字，"
            "要嘛套件把它改名了——後者代表那個 tuple 現在少抓一整類例外。")
        resolved[name] = cls
    return resolved


def _uncatchable_gone_names(catchable: tuple[type, ...],
                            gone_names) -> tuple[list[str], list[str]]:
    """純判定：(解析不到的名字, 抓不到的名字＋MRO)。不讀磁碟、不碰 AST。"""
    unresolved, uncatchable = [], []
    for name in sorted(gone_names):
        cls = _resolve_exception(name)
        if cls is None:
            unresolved.append(name)
        elif not issubclass(cls, catchable):
            uncatchable.append(
                f"{name}（MRO: "
                + " → ".join(c.__name__ for c in cls.__mro__[:4]) + "）")
    return unresolved, uncatchable


@pytest.mark.parametrize("spec", _TRANSPORT_PORTS,
                         ids=[p.label for p in _TRANSPORT_PORTS])
def test_every_session_gone_name_is_actually_catchable(spec):
    """`_SESSION_GONE_EXC_NAMES` 的每一個名字，都要真的被這個 port 的
    `TRANSPORT_ERRORS` 抓得到。

    範圍是**全專案非測試模組裡每一個真的 port**，不是兩個變體——規則跟模組
    無關（理由見本檔 docstring 最後一段）。

    抓不到 = 那個名字寫在分類表裡，但分類程式（`is_browser_gone_error`，hot path
    上唯一的入口是 `_note_transport_error`）永遠拿不到它。這正是 2026-09-09 修掉
    的那個缺陷，而它在正式環境安靜了好幾個月：只表現成 `critical_error` 的 message
    是生的例外文字，沒有 `browser session gone during …` 前綴。

    **解析不到的名字一律算失敗，不准跳過。** 一個匹配不到任何東西的檢查跟一個通過
    的檢查長得一模一樣，本專案已經為了這件事吃過虧（`-k` 打錯名字的變異測試回報
    `0 passed`、rc=0）。
    """
    import _webrunner_shared as ws

    catchable = tuple(_port_transport_classes(spec).values())
    unresolved, uncatchable = _uncatchable_gone_names(
        catchable, ws._SESSION_GONE_EXC_NAMES)
    assert not unresolved, (
        f"`_SESSION_GONE_EXC_NAMES` 裡這些名字在 {list(_EXC_LOOKUP_MODULES)} "
        f"都找不到對應的例外類別：{unresolved}。"
        "可能是打錯字、套件改名，或這個名字所屬的套件沒被列進 "
        "`_EXC_LOOKUP_MODULES`。不論哪一種，那一筆分類規則都是死的。")
    assert not uncatchable, (
        f"{spec.label} 的 `TRANSPORT_ERRORS` 抓不到這些 gone 名單裡的例外："
        f"{uncatchable}。它們會直接炸穿 hot path 的 "
        "`except port.TRANSPORT_ERRORS`，`_note_transport_error` 不會被呼叫，"
        "於是分類表對它們形同不存在。")


@pytest.mark.parametrize("spec", _TRANSPORT_PORTS,
                         ids=[p.label for p in _TRANSPORT_PORTS])
def test_every_urllib3_class_in_the_tuple_is_also_classified_as_gone(spec):
    """反方向：tuple 裡新加的 urllib3 類別必須同時列進 gone 名單。

    這條是「加寬 tuple 不會造成退步」的**唯一**保證。被抓到卻沒被分類成 gone 的
    例外，會被 `_note_transport_error` 當成暫時性卡頓吸收掉（印一行 → 回
    sentinel），呼叫端於是對著一個已經死掉的 driver 把整個重試預算 poll 完——實測
    約 30 分鐘。今天沒被抓到的話它反而是**快速失敗**（rc → 監督者重生 Chrome），
    所以「多抓一個卻不分類」是嚴格的退步。

    只管 urllib3 那一側：`WebDriverException` 與 `OSError` 涵蓋的絕大多數是真正的
    暫時性錯誤（DOM 找不到、元素被蓋住、檔案寫入失敗），本來就該被吸收。
    """
    import _webrunner_shared as ws

    problems = []
    for name, cls in sorted(_port_transport_classes(spec).items()):
        if not cls.__module__.startswith("urllib3"):
            continue
        # 真的呼叫分類器，不要在測試裡重抄一次它的 MRO 比對邏輯。
        # `__new__` 建出來的實例 `str()` 是空字串，所以只有類別名那條路會命中，
        # 正好是要驗的東西。
        gone = ws.is_browser_gone_error(cls.__new__(cls))
        if name in _TRANSPORT_NOT_GONE:
            if gone:
                problems.append(
                    f"{name} 被分類成 gone，但它是「連上了只是沒回話」＝"
                    "chromedriver 還活著。這會把一次卡頓變成一次重生。")
        elif not gone:
            problems.append(
                f"{name} 在 {spec.label} 的 TRANSPORT_ERRORS 裡，卻不在 "
                "`_SESSION_GONE_EXC_NAMES` 裡——它會被當成暫時性卡頓吸收掉，"
                "呼叫端要空轉約 30 分鐘才收工；不抓它反而是快速失敗。"
                "要嘛把它加進 gone 名單，要嘛從 tuple 拿掉，"
                "要嘛列進 `_TRANSPORT_NOT_GONE` 並寫清楚理由。")
    assert not problems, "\n".join(problems)


# ---- 範圍本身的守門 -------------------------------------------------------
# 下面兩支測「範圍」，不測內容：一支量下限（抽不到檔案時「零筆違規」跟「全部乾淨」
# 在輸出上分不出來），一支明確斷言掃描結果**包含 webrunner 以外的模組**。
#
# **兩支互相補位，不要當成重複刪掉其中一支。** 兩個方向都用變異量過（2026-09-09）：
# 停用下限＋must-list 再把掃描縮回兩個變體 → 由範圍 pin 接住；停用範圍 pin 再把掃描
# 挖空 → 由下限接住；**兩支一起停用**則整組測試 10 passed，也就是掃描範圍可以被安靜
# 地縮掉。
#
# 順帶記一個判讀陷阱：`_TRANSPORT_PORTS` 變空的時候，上面兩支 parametrize 的測試在
# 輸出裡是 **skipped**（pytest 對空的 argvalues 就是這樣），不是 failed——`2 failed,
# 2 passed, 2 skipped` 這種行看起來完全正常。所以「零個案例」必須由下限那一支主動
# 報出來，不能指望從測試結果的形狀看出來。


def test_the_transport_scan_finds_every_real_port():
    """下限：掃到的 port 不得少於已知的那幾個。

    這支存在的唯一理由是「空的選取看起來像乾淨的結果」——`_TRANSPORT_PORTS` 變成
    空 list 的話，上面兩支 parametrize 會產生**零個測試案例**，pytest 照樣回
    `passed`，而全專案的 transport 契約等於完全沒人在看。
    """
    found = {(spec.module, spec.cls) for spec in _TRANSPORT_PORTS}
    assert len(_TRANSPORT_PORTS) >= _MIN_TRANSPORT_PORTS, (
        f"只掃到 {len(_TRANSPORT_PORTS)} 個 port：{sorted(found)}，"
        f"少於下限 {_MIN_TRANSPORT_PORTS}（兩個變體 ＋ "
        "verify_quota_dialog.Port）——掃描範圍或掃描器壞了。")
    missing = sorted(_MUST_SCAN_PORTS - found)
    assert not missing, (
        f"這些已知的 port 沒被掃到：{missing}。掃到的是 {sorted(found)}。"
        "檢查 `_SKIP_DIRS`、`_is_test_source`，以及 "
        "`_transport_ports_in_source` 讀不讀得懂它那種寫法。")


def test_the_transport_contract_is_enforced_outside_the_two_variants():
    """範圍 pin：規則不得縮回 `_VARIANTS`。

    `TRANSPORT_ERRORS` 的契約跟「哪個模組」無關——任何一個真的會被丟進
    `_webrunner_shared` 共用 helper 的 port 都適用。範圍縮回兩個變體是**安靜的**
    退步：真實資料乾淨時兩支對帳測試照樣全綠，只是第三個 port 從此沒人看。
    """
    outside = sorted({(spec.module, spec.cls) for spec in _TRANSPORT_PORTS
                      if spec.module not in _VARIANTS})
    assert outside, (
        "掃描結果只剩兩個 webrunner 變體——範圍被縮回檔名清單了。"
        f"至少 {sorted(_MUST_SCAN_PORTS)} 裡的非變體那一筆必須在。")
    # 光是「掃到了」不夠：它得真的被拿去檢查。tuple 是空的 = 規則對它形同不存在。
    for spec in _TRANSPORT_PORTS:
        if spec.module in _VARIANTS:
            continue
        assert spec.elements, (
            f"{spec.label} 被掃到了，但抽出來的 tuple 是空的——"
            "掃描器讀不懂它的寫法，等於沒檢查。")


# ---- 掃描那一半的對照組（純函式，餵合成原始碼）-----------------------------

_SYNTHETIC_NARROW = (
    "from selenium.common.exceptions import WebDriverException\n"
    "\n"
    "\n"
    "class Port:\n"
    "    TRANSPORT_ERRORS = (WebDriverException, OSError)\n"
)

_SYNTHETIC_COMPLETE = (
    "from selenium.common.exceptions import WebDriverException\n"
    "from urllib3.exceptions import (\n"
    "    ConnectTimeoutError as U3ConnectTimeoutError,\n"
    "    MaxRetryError as U3MaxRetryError,\n"
    "    NewConnectionError as U3NewConnectionError,\n"
    "    ProtocolError as U3ProtocolError,\n"
    "    ReadTimeoutError as U3ReadTimeoutError,\n"
    ")\n"
    "\n"
    "_TUPLE = (\n"
    "    WebDriverException,\n"
    "    U3ReadTimeoutError,\n"
    "    U3MaxRetryError,\n"
    "    U3NewConnectionError,\n"
    "    U3ConnectTimeoutError,\n"
    "    U3ProtocolError,\n"
    "    OSError,\n"
    ")\n"
    "\n"
    "\n"
    "class SomePort:\n"
    "    TRANSPORT_ERRORS = _TUPLE\n"
)


def test_the_scanner_reads_both_tuple_shapes_and_undoes_aliases():
    """掃描器對照組：類別內字面 tuple ＋ 指向模組層 tuple 的名字，兩種都要讀到。

    專案裡兩種寫法都有（`verify_quota_dialog.Port` 是前者，兩個變體是後者），只認
    一種就會安靜地漏掉另一種。別名也要還原——`U3MaxRetryError` 這種名字在任何套件
    裡都查不到，還原失敗會讓 `_port_transport_classes` 直接失敗，而不是少檢查一項。
    """
    narrow = _transport_ports_in_source(_SYNTHETIC_NARROW, "synthetic_narrow.py")
    assert [(p.cls, p.elements) for p in narrow] == [
        ("Port", ("WebDriverException", "OSError"))]

    complete = _transport_ports_in_source(_SYNTHETIC_COMPLETE,
                                          "synthetic_complete.py")
    assert len(complete) == 1
    assert complete[0].cls == "SomePort"
    assert set(complete[0].elements) == {
        "WebDriverException", "ReadTimeoutError", "MaxRetryError",
        "NewConnectionError", "ConnectTimeoutError", "ProtocolError", "OSError"}

    # 反面：沒有 TRANSPORT_ERRORS 的類別不得被誤報成 port。
    assert _transport_ports_in_source(
        "class Nothing:\n    OTHER = (OSError,)\n", "synthetic_none.py") == []


def test_the_catchability_check_flags_the_narrow_tuple_and_clears_the_full_one():
    """判定那一半的對照組：同一份 gone 名單，窄的要被抓、完整的不得誤報。

    正面用的就是 2026-09-09 之前 `verify_quota_dialog.Port` 的那個形狀，所以這支
    直接證明「規則自己認得那個缺陷」——不必等真實資料壞掉才知道守門有沒有用。
    反面的完整 tuple 則擋掉「把判定寫成永遠回報一堆東西」那種假守門。
    """
    import _webrunner_shared as ws

    narrow = _transport_ports_in_source(_SYNTHETIC_NARROW, "x.py")[0]
    unresolved, uncatchable = _uncatchable_gone_names(
        tuple(_port_transport_classes(narrow).values()),
        ws._SESSION_GONE_EXC_NAMES)
    assert not unresolved
    flagged = {line.split("（", 1)[0] for line in uncatchable}
    assert flagged == {"MaxRetryError", "NewConnectionError",
                       "ConnectTimeoutError", "ProtocolError"}, flagged

    complete = _transport_ports_in_source(_SYNTHETIC_COMPLETE, "y.py")[0]
    unresolved, uncatchable = _uncatchable_gone_names(
        tuple(_port_transport_classes(complete).values()),
        ws._SESSION_GONE_EXC_NAMES)
    assert not unresolved and not uncatchable, uncatchable


@pytest.mark.parametrize("variant", _VARIANTS)
def test_both_ports_can_send_a_real_escape(variant):
    """`press_escape` 兩邊都要在。

    額度用完的購買對話框上沒有任何可以安全點下去的東西，唯一的出路就是 Escape；
    合成的 KeyboardEvent 是 `isTrusted === false`，driver 層的真按鍵才是最後一
    張牌。少了它就直接退化成整頁重新整理（實測 86 次全中）。
    """
    assert "press_escape" in _methods(PKG_ROOT / variant), (
        f"{variant} 的 BrowserPort 沒有 press_escape——"
        "額度對話框會退回「只送得出合成事件」的舊行為。")


# ---------------------------------------------------------------------------
# Chrome 啟動旗標：兩支變體必須是同一組
#
# novelai 走 `_make_chrome_options()`（`ChromeOptions.add_argument` ＋
# `_MEMORY_FLAGS` 這個 tuple），je 走 `wr.set_driver(options=cli_args)` 的一串**行
# 內字面值**，而且註解直接寫著「see webrunner_novelai._MEMORY_FLAGS for the
# rationale」——也就是說這是一份**已知的抄本**，靠人記得同步。
#
# 漏同步是安靜的：je 那一側照樣起得來、照樣產圖，只是少了那幾個記憶體旗標，然後
# 在長跑的第幾個小時出現 "Aw, Snap! Out of Memory"。那個症狀跟旗標的距離遠到沒人
# 會把兩件事連起來。
# ---------------------------------------------------------------------------

# 帶主機專屬值的旗標，只比對 `=` 左邊的名字（值本來就該不同或會變）。
_VALUE_BEARING = frozenset({"--user-data-dir", "--user-agent"})


def _flag_name(text: str) -> str:
    return text.split("=", 1)[0]


def _literal_flag(node: ast.AST) -> str | None:
    """從 `ast` 取出旗標名。常數與 f-string（`f"--user-data-dir={x}"`）都收。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _flag_name(node.value)
    if isinstance(node, ast.JoinedStr):
        head = node.values[0] if node.values else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return _flag_name(head.value)
    return None


def _tuple_of(tree: ast.Module, name: str) -> list[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return [e.value for e in getattr(node.value, "elts", [])
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _novelai_chrome_flags() -> set[str]:
    path = PKG_ROOT / "webrunner_novelai.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    memory = _tuple_of(tree, "_MEMORY_FLAGS")
    assert memory, ("webrunner_novelai 裡找不到 `_MEMORY_FLAGS` 這個字面 tuple"
                    "——改寫過的話這支測試要跟著改。")
    flags = {_flag_name(f) for f in memory}
    found_loop = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name == "_make_chrome_options"):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.For) and isinstance(inner.iter, ast.Name)
                    and inner.iter.id == "_MEMORY_FLAGS"):
                found_loop = True
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "add_argument" and inner.args):
                name = _literal_flag(inner.args[0])
                if name:
                    flags.add(name)
    assert found_loop, (
        "`_make_chrome_options` 不再走 `for flag in _MEMORY_FLAGS` 了——"
        "這支測試是靠那個迴圈確認記憶體旗標真的有被加上去的。")
    return flags


def _je_chrome_flags() -> set[str]:
    path = PKG_ROOT / "webrunner_je_only.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "cli_args"
                        for t in node.targets)
                and isinstance(node.value, ast.List)):
            out = {_literal_flag(e) for e in node.value.elts}
            out.discard(None)
            return out
    raise AssertionError(
        "webrunner_je_only 裡找不到 `cli_args = [...]` 這個字面清單——"
        "改寫過的話這支測試要跟著改。")


def test_both_variants_launch_chrome_with_the_same_flags():
    """兩支變體的 Chrome 啟動旗標必須一致。

    je 那一側是**手抄**的一份（註解自己寫著 see `_MEMORY_FLAGS`）。只在 novelai
    加一個記憶體旗標，je 會照樣起得來、照樣產圖，然後在長跑的第幾個小時 OOM——
    症狀跟成因之間遠到沒人會連起來。
    """
    novelai, je = _novelai_chrome_flags(), _je_chrome_flags()
    only_novelai = sorted(novelai - je)
    only_je = sorted(je - novelai)
    assert not only_novelai, (
        f"這些 Chrome 旗標只有 novelai 變體有：{only_novelai}。"
        "je 變體的 `cli_args` 要一起補上（那是手抄的一份，沒有任何東西會自動同步）。")
    assert not only_je, (
        f"這些 Chrome 旗標只有 je 變體有：{only_je}。")


def test_the_memory_flags_that_matter_are_still_there():
    """幾個關鍵旗標的存在本身也要守——刪掉是完全無聲的。

    這幾個不是隨手加的：長跑會累積 renderer 記憶體直到 "Aw, Snap! Out of Memory"，
    而在記憶體吃緊的機器上那還會讓**下一個** Chrome 也起不來
    （`SessionNotCreatedException: Chrome instance exited`）。
    """
    flags = _novelai_chrome_flags()
    for required in ("--disable-dev-shm-usage", "--disk-cache-size",
                     "--media-cache-size", "--disable-renderer-backgrounding",
                     "--disable-background-timer-throttling"):
        assert required in flags, (
            f"少了 {required}——長跑的記憶體行為會變，而症狀要好幾小時後才出現。")
    # GPU 一定要留著：站方的畫布不畫出來，找圖那一段（`get_main_image_src` 用
    # `naturalWidth` 挑最大的可見圖）就整個失效。
    for banned in ("--disable-gpu", "--headless", "--headless=new"):
        assert banned not in flags, (
            f"{banned} 不能加——站方的 canvas 不畫出來的話找不到產出的圖。")


def test_the_flag_readers_actually_read_something():
    """canary：兩個讀取器任一邊回空集合，上面兩支就會安靜地全過。"""
    novelai, je = _novelai_chrome_flags(), _je_chrome_flags()
    assert len(novelai) >= 10, f"novelai 只掃到 {len(novelai)} 個旗標：{sorted(novelai)}"
    assert len(je) >= 10, f"je 只掃到 {len(je)} 個旗標：{sorted(je)}"
    for name in _VALUE_BEARING:
        assert name in novelai and name in je, (
            f"{name} 兩邊都該有（只比對 `=` 左邊的名字）："
            f"novelai={name in novelai} je={name in je}")


# ---------------------------------------------------------------------------
# 登入 profile：兩個變體要同步的是**資料**，不是程式碼
# ---------------------------------------------------------------------------

# Chrome 96（2021-12）把 cookie store 從 `Default/` 搬進 network service 自己的
# 目錄 `Default/Network/`。key ＝ 已經不存在的舊路徑，value ＝ 現在的位置。
# 這張表是「路徑寫錯」這一類的**資料側**守門：`_SESSION_CRITICAL` 那兩筆錯了四
# 個月，期間所有行為測試都是綠的，因為它們是照著那個常數寫出來的（用舊路徑造
# 假檔案 → sync-back 照著舊路徑找 → 找到了 → 綠）。測試跟著被測的常數走，就永遠
# 問不到「這個常數對不對」。
_MOVED_BY_CHROME = {
    "Default/Cookies": "Default/Network/Cookies",
    "Default/Cookies-journal": "Default/Network/Cookies-journal",
}


def _session_critical(variant: str) -> tuple[str, ...]:
    """取 `_SESSION_CRITICAL` 的**值**，用 `ast.literal_eval`。

    **刻意不比對文字。** 兩個變體的排版本來就不同——je 把兩個 cookie 路徑寫在同
    一行，novelai 各佔一行——所以字串比對會為了一個換行變紅。一個會為了排版叫的
    守門就是會被關掉的守門。
    """
    path = PKG_ROOT / variant
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name)
                        and t.id == "_SESSION_CRITICAL"
                        for t in node.targets)):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(
        f"{variant} 裡找不到模組層的 _SESSION_CRITICAL——改名的話這一支要跟著改，"
        "不要讓它安靜地退化成「兩邊都沒有 ＝ 兩邊一致」。")


def test_both_variants_agree_on_the_session_critical_files():
    """兩個變體的 `_SESSION_CRITICAL` 必須**逐值相同**。

    這份清單決定「哪些檔案會被寫回 `.chrome_profile/`」，也就是下一輪要不要重新
    登入。它在兩個檔案裡各有一份副本，而**沒有任何測試引用過它**（2026-09-09 全
    repo grep 過），所以一邊改了另一邊沒改的話，要等到有人真的切到另一個變體、在
    無人值守的批次裡才會現形——症狀還會是「莫名其妙每輪都重新登入」，跟這份清單
    看不出關係。
    """
    values = {v: _session_critical(v) for v in _VARIANTS}
    # 正面對照組：空的抽取結果會讓下面那條斷言「兩邊一致」永遠成立。
    for variant, entries in values.items():
        assert len(entries) >= 6, (
            f"{variant} 的 _SESSION_CRITICAL 只抽到 {len(entries)} 筆——"
            "抽取壞了的話，下面的一致性比對是空的。")
        assert all(isinstance(e, str) and e for e in entries), (
            f"{variant} 的 _SESSION_CRITICAL 有非字串或空字串的項目：{entries}")
    novelai, je = (values[v] for v in _VARIANTS)
    assert novelai == je, (
        f"兩個變體的 _SESSION_CRITICAL 漂移了：\n"
        f"  只有 novelai 有: {sorted(set(novelai) - set(je))}\n"
        f"  只有 je_only 有: {sorted(set(je) - set(novelai))}\n"
        f"  順序: {novelai} vs {je}")


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_session_critical_list_names_no_path_chrome_abandoned(variant):
    """清單裡不得出現 Chrome 已經搬走的路徑。

    2026-09-09 修的實際缺陷：`Default/Cookies` / `Default/Cookies-journal` 從
    Chrome 96 起就不是 cookie store 的位置了，而 `_sync_chrome_profile_back` 的
    `if not src.exists(): continue` 是靜默的，所以那兩筆**每一輪都被跳過**、
    四個月沒有人發現。可量的後果：`.chrome_profile/Default/Network/Cookies` 的
    mtime 停在 2026-05-20，`WEBRunner.log` 08-24～09-09 的 34 次 setup 有 **34 次
    走完整 `/login` 流程**、`session restored` 0 次。

    這一支是**資料側**的守門，跟行為測試互補：行為測試問「程式有沒有照這個常數
    做事」，這一支問「這個常數指的地方存不存在」。前者永遠是綠的，因為它是照著
    同一個常數寫出來的。
    """
    entries = set(_session_critical(variant))
    assert entries, f"{variant} 的 _SESSION_CRITICAL 是空的"
    stale = sorted(entries & set(_MOVED_BY_CHROME))
    assert not stale, (
        f"{variant} 的 _SESSION_CRITICAL 還在用 Chrome 已經搬走的路徑 {stale}。"
        f"現在的位置：{ {s: _MOVED_BY_CHROME[s] for s in stale} }。"
        "指向不存在的檔案不會報錯，只會被安靜跳過——那正是這個缺陷活了四個月的"
        "方式。")
    # 反向：搬家表本身也會過期，而過期的守門是安靜的。要求**每一個**新位置都真的
    # 在用（`<=` 不是 `&`）——只要求「至少一個」的話，表裡有兩筆時改壞其中一筆完全
    # 不會紅（實測：把 `Default/Network/Cookies` 的新位置改成 `Default/
    # SomewhereElse/Cookies`，變異存活），因為另一筆還在撐著。
    unused = sorted(set(_MOVED_BY_CHROME.values()) - entries)
    assert not unused, (
        f"_MOVED_BY_CHROME 說 {unused} 是現在的位置，但 {variant} 的 "
        "_SESSION_CRITICAL 沒有在用它們——要嘛這張表過期了，要嘛清單真的不再"
        "同步那些檔案。兩種都要人看一眼，不該安靜通過。")


# ---------------------------------------------------------------------------
# 密文與解密金鑰必須同進同出
#
# `Default/Network/Cookies` 的 `encrypted_value`、`Default/Login Data` 的
# `password_value`、`Default/Web Data` 的自動填入欄位，存的都是密文 blob；解密
# 金鑰在 `Local State` 的 `os_crypt.encrypted_key`（2026-09-09 實測：兩份 profile
# 的 `encrypted_key` 指紋一致，所有 cookie 都是 `v10` 前綴，而 `v10` 這個前綴的
# 意思就是「用 os_crypt 那把金鑰加密的」）。
#
# 所以**只同步其中一邊是壞的**：搬了密文沒搬金鑰，或搬了金鑰沒搬密文，兩種都會
# 得到一份解不開的登入資料。而失效完全沒有症狀——有人為了「少複製一點」把
# `Local State` 從清單裡拿掉，所有行為測試照樣綠、log 照樣印 `synced N/N`，只有
# 登入態安靜地壞掉。現況是四個都在清單裡，這是**趁乾淨釘住**，跟本專案這兩天做
# 的 `KEY_ALIASES` / `_AUTOSTART_TASKS` 同一類。
#
# 註：`Default/Local Storage/leveldb`（登入態真正的所在）**不在**這張表裡，因為
# Chrome 不加密 localStorage。那也正是為什麼它可以單獨同步而不必配任何金鑰。
_OS_CRYPT_CIPHERTEXT_STORES = (
    "Default/Network/Cookies",
    "Default/Login Data",
    "Default/Web Data",
)
_OS_CRYPT_KEY_FILE = "Local State"


def os_crypt_coupling_error(entries) -> str | None:
    """純函式：`entries` 裡有密文檔卻沒有金鑰檔就回一句話，否則回 None。

    抽成純函式是因為**現況是乾淨的**，所以主測試把斷言整條刪掉本來就不會紅。
    合成資料的對照測試餵得到這一支，餵不到那個讀原始碼的版本。
    """
    present = sorted(set(entries) & set(_OS_CRYPT_CIPHERTEXT_STORES))
    if present and _OS_CRYPT_KEY_FILE not in set(entries):
        return (f"清單裡有用 os_crypt 金鑰加密的檔案 {present}，卻沒有帶著金鑰所在"
                f"的 {_OS_CRYPT_KEY_FILE!r}。搬了密文沒搬金鑰 ＝ 一份解不開的登入"
                f"資料，而且完全沒有症狀。")
    return None


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_ciphertext_stores_never_travel_without_their_key(variant):
    """`_SESSION_CRITICAL` 不得只帶密文不帶金鑰。"""
    entries = _session_critical(variant)
    # 正面對照組：抽不到東西的話下面那條永遠成立（空集合交出來還是空的）。
    assert set(entries) & set(_OS_CRYPT_CIPHERTEXT_STORES), (
        f"{variant} 的 _SESSION_CRITICAL 裡一個密文檔都沒有——要嘛抽取壞了，"
        f"要嘛 {list(_OS_CRYPT_CIPHERTEXT_STORES)} 這幾筆真的被拿掉了。"
        "兩種都要人看一眼，不該安靜通過。")
    problem = os_crypt_coupling_error(entries)
    assert problem is None, f"{variant}: {problem}"


def test_the_ciphertext_coupling_check_actually_catches_a_broken_list():
    """合成資料的對照組——比對邏輯本身要真的會抓。

    上面那支跑在**乾淨的**現況上，所以它證明不了自己有效：把 `assert problem is
    None` 整行刪掉，它照樣綠。這一支餵一份「有密文、沒金鑰」的假清單，補上那個
    缺口。反方向（金鑰在、密文在）與空清單也一起釘，免得被「過度修正」成永遠回
    一句話。
    """
    broken = ("Default/Network/Cookies", "Default/Preferences")
    assert os_crypt_coupling_error(broken) is not None, (
        "帶了密文卻沒帶金鑰，比對邏輯卻說沒問題——那上面那支等於沒有在檢查。")
    assert _OS_CRYPT_KEY_FILE in (os_crypt_coupling_error(broken) or "")
    assert os_crypt_coupling_error(broken + (_OS_CRYPT_KEY_FILE,)) is None, (
        "金鑰帶到了卻還在抱怨——會叫狼來了的守門就是會被關掉的守門。")
    assert os_crypt_coupling_error(()) is None, (
        "空清單裡沒有任何密文，不該被判成耦合斷掉。")
    assert os_crypt_coupling_error((_OS_CRYPT_KEY_FILE,)) is None


# ---------------------------------------------------------------------------
# 兩個變體共有的 module-level 常數，值要一致
#
# 上面 `_SESSION_CRITICAL` 那兩支是**列舉制**：一個常數配一支測試。本專案剛在
# `_OWNER_ONLY_GROUPS` 上學過這一課——列舉會漏掉下一個，而漏掉的症狀是零。實測
# （2026-09-09）兩個變體共有 **6** 個 module-level 常數（同日稍晚因為 localStorage
# 同步變成 8 個，而這一支**不必改一個字**就蓋到了新的兩個——那就是「結構性」的意思），
# 而在補這一段之前只有
# **1** 個（`_SESSION_CRITICAL`，當天才補）被比對過：
#
#     IMAGE_URL / LOGIN_URL / REAL_USER_AGENT / STEALTH_JS / _CHROME_LOCK_FILES
#
# 這五個都是「兩邊各抄一份、改一邊會安靜分歧」的形狀，而其中兩個的分歧代價很高：
# `STEALTH_JS` 分歧 ＝ 一個變體被偵測成自動化、另一個沒有；`_CHROME_LOCK_FILES`
# 分歧 ＝ 一個變體清不乾淨 lock 檔，下一次啟動 Chrome 失敗。
#
# 所以這裡改成**結構性**的：取兩邊的交集，全部比值。新增一個共有常數會自動被蓋
# 到，不必有人記得回來加一行。刻意要不同的，寫進 `_ALLOWED_CONSTANT_DIVERGENCE`
# 並附理由。
# ---------------------------------------------------------------------------
# 允許分歧的常數：名字 → 為什麼。**這是清單制，每一筆都要有理由。**
# 目前是空的——六個共有常數的值全部相同。
_ALLOWED_CONSTANT_DIVERGENCE: dict[str, str] = {}

# 核心共有常數。放這一份的理由是「空交集看起來跟乾淨結果一模一樣」：某個常數在
# 一個變體裡被改名之後會直接**掉出交集**，下面那支主測試就再也不會檢查它，而且
# 完全不會紅。這一份把「它應該還在兩邊」釘住。
# 真的要移除某一筆時，連同理由一起從這裡刪掉——那是一個需要被看見的決定。
_CORE_SHARED_CONSTANTS = frozenset({
    "IMAGE_URL", "LOGIN_URL", "REAL_USER_AGENT", "STEALTH_JS",
    "_CHROME_LOCK_FILES", "_SESSION_CRITICAL",
    # 2026-09-09：登入態其實在 localStorage，不在 cookie。這兩筆跟
    # `_SESSION_CRITICAL` 同一個形狀（兩邊各抄一份，分歧了會安靜地讓一個變體
    # 每輪重新登入）。
    "_SESSION_CRITICAL_DIRS", "_LEVELDB_SKIP_NAMES",
})


def _module_constants(variant: str) -> dict:
    """該變體所有 module-level 常數 → 值。

    只收 `ast.literal_eval` 讀得懂的（純量／字串／容器）；由運算式算出來的常數
    （例如 `_SNAPSHOT_IGNORE_NAMES` 用 `_CHROME_LOCK_FILES + (...)` 組出來）讀不
    到，那是刻意的——讀得到的那些才是「兩邊各抄一份字面值」的形狀。
    """
    tree = ast.parse((PKG_ROOT / variant).read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.lstrip("_").isupper():
            continue
        try:
            found[target.id] = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            continue
    return found


def _diverged(left: dict, right: dict, allowed=frozenset()) -> list:
    """兩邊都有、值卻不同、又不在豁免名單裡的常數名（排序）。

    抽成純函式是為了讓它有**自己的**對照組：現況零分歧，所以主測試把斷言整個
    刪掉也不會紅（見 `test_the_constant_comparator_sees_a_divergence`）。
    """
    return sorted(name for name in set(left) & set(right)
                  if name not in allowed and left[name] != right[name])


def test_every_constant_defined_in_both_variants_has_the_same_value():
    """交集裡的每一個常數，兩邊的值都要相同。

    這一支刻意**不列舉**要比哪些。列舉的版本會在下一個共有常數被加進來時安靜地
    漏掉它，而那正是 `_SESSION_CRITICAL` 那個路徑錯了四個月都沒人發現的原因。
    """
    left = _module_constants("webrunner_novelai.py")
    right = _module_constants("webrunner_je_only.py")
    shared = set(left) & set(right)
    # 正面對照組：抽取器壞掉時「零分歧」與「抽不到任何常數」長得一模一樣。
    assert len(shared) >= 5, (
        "只抽到 %d 個共有常數（novelai %d、je_only %d），抽取邏輯壞了——"
        "底下那句斷言的綠色會是假的" % (len(shared), len(left), len(right)))

    diverged = _diverged(left, right, _ALLOWED_CONSTANT_DIVERGENCE.keys())
    assert not diverged, (
        "這些常數兩個變體都有，但值已經分歧：%s。兩個變體必須同步——刻意要不同"
        "的請寫進 `_ALLOWED_CONSTANT_DIVERGENCE` 並附上理由。" % diverged)


def test_the_core_shared_constants_are_still_shared():
    """核心那幾筆必須**仍然**在兩邊都存在。

    需要這一支是因為上面那支只看交集：任何一個常數在其中一個變體裡被改名或刪掉，
    它就靜靜地掉出交集、不再被檢查，而且不會紅。空集合看起來跟乾淨結果一樣。
    """
    left = _module_constants("webrunner_novelai.py")
    right = _module_constants("webrunner_je_only.py")
    for name in sorted(_CORE_SHARED_CONSTANTS):
        assert name in left, f"webrunner_novelai.py 少了 {name}"
        assert name in right, f"webrunner_je_only.py 少了 {name}"


def test_every_divergence_exemption_is_real_and_explained():
    """豁免名單不得長草：列進去的必須真的分歧，而且理由不得留白。

    一個對不上任何東西的豁免只會讓下一個人以為那件事有人想過。
    """
    left = _module_constants("webrunner_novelai.py")
    right = _module_constants("webrunner_je_only.py")
    for name, reason in _ALLOWED_CONSTANT_DIVERGENCE.items():
        assert name in left and name in right, (
            f"{name} 被列為「允許分歧」，但它不是兩個變體都有的常數")
        assert left[name] != right[name], (
            f"{name} 的值其實一樣，不需要豁免——把它從名單裡拿掉")
        assert reason.strip(), f"{name} 的豁免沒有寫理由"


def test_the_constant_comparator_sees_a_divergence():
    """比對器自己的正面對照組：拿合成資料確認它真的看得見分歧。

    跟 `test_bot_helpers._stale_lock_entries`、`test_gui_control.
    _targets_missing_from` 同一個做法——現況乾淨，牙齒要長在純函式這一側。
    """
    same = {"A": 1, "B": "x"}
    assert _diverged(same, dict(same)) == []
    # 值分歧
    assert _diverged(same, {"A": 2, "B": "x"}) == ["A"]
    # 只在一邊的常數**不算**分歧（變體專屬的常數本來就存在）
    assert _diverged(same, {"A": 1, "B": "x", "C": 9}) == []
    # 豁免名單真的會豁免
    assert _diverged(same, {"A": 2, "B": "y"}, allowed={"A"}) == ["B"]
    # 容器型別也要比得出來（`_SESSION_CRITICAL` 就是 tuple）
    assert _diverged({"T": ("a", "b")}, {"T": ("a", "c")}) == ["T"]


# ---------------------------------------------------------------------------
# 真的 import 一次——上面全部是 AST，證明不了模組載得起來
# ---------------------------------------------------------------------------

_SMOKE = ("import sys; sys.path.insert(0, 'axiomatic'); "
          "import discord_bot, webrunner_novelai, webrunner_je_only; print('OK')")


def test_both_variants_actually_import():
    """兩個變體都要能在**乾淨的直譯器**裡 import 起來。

    這一支的價值在於它是本檔唯一真的執行程式碼的測試。其餘全部是 AST 分析——分析
    得再仔細，也證明不了 `import webrunner_je_only` 不會當場丟 `SyntaxError` 或
    `ImportError`。2026-09-01 量到那支的覆蓋率是 0%：沒有任何測試碰過它。

    **用子行程而不是直接 import**，有兩個理由：(1) 同一個 pytest 行程裡別的測試可能
    已經先 import 過相依套件，於是「其實少裝了東西」會被蓋掉；(2) 兩個變體都會在模組
    層動 `sys.path`，污染測試行程不值得。用 `sys.executable` 跑，所以**跑在哪個直譯器
    上就驗哪一個**——本專案同時有 `py -3` 與 `.venv` 兩套相依，這正是差異會出現的地方。
    """
    proc = subprocess.run(  # nosec B603
        [sys.executable, "-c", _SMOKE], cwd=str(REPO_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 0 and "OK" in proc.stdout, (
        f"變體 import 失敗（rc={proc.returncode}）——這台直譯器上有東西載不起來。\n"
        f"stdout: {proc.stdout[-500:]}\nstderr: {proc.stderr[-1500:]}")


def test_the_definition_of_done_smoke_test_names_every_variant():
    """DoD #1 的冒煙指令必須涵蓋**每一個** `webrunner_*` 變體。

    它原本只寫 `discord_bot, webrunner_novelai`，於是「每次改動都跑一次 import 冒煙」
    這條規則對第二個變體從來沒有生效過。漏掉一個變體不會讓任何測試變紅，也不會在
    `git diff` 裡看起來可疑——正是這種漏洞需要機器來守。

    新增第三個變體時，這一支會直接紅，訊息會說要去改 CLAUDE.md 哪一行。
    """
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    match = re.search(r"import sys; sys\.path\.insert\(0, 'axiomatic'\); "
                      r"import ([A-Za-z0-9_, ]+); print", claude_md)
    assert match, "CLAUDE.md 裡找不到 DoD #1 的 import 冒煙指令"
    named = {m.strip() for m in match.group(1).split(",")}
    variants = {p.stem for p in PKG_ROOT.glob("webrunner_*.py")}
    missing = (variants | {"discord_bot"}) - named
    assert not missing, (
        f"DoD #1 的冒煙指令沒有涵蓋 {sorted(missing)}——那些模組的 import 錯誤"
        "不會被任何人發現。請更新 CLAUDE.md 的那一行。")
    assert named <= (variants | {"discord_bot"}), (
        f"冒煙指令 import 了不存在的模組：{sorted(named - variants - {'discord_bot'})}")
