"""`requirements.txt` 的版本下限守門。

這條守門是實測出來的，不是預防性的。2026-08-29 用 `pip-audit` 查本機的兩個直譯
器，兩邊的 `aiohttp` 都停在 3.13.5、落後 **14 個已知漏洞**，其中一個
（PYSEC-2026-3545）是第三方伺服器回一個畸形回應就能觸發的 C 剖析器越界讀取——而
`_external_apis` 每次 `/booru` 都在對第三方圖庫 API 發 GET。這個狀態存在了好幾週，
期間**沒有任何訊號**：`requirements.txt` 一個版本都沒宣告，所以什麼都沒被違反；
全套測試 870 支綠燈；`pip list --outdated` 要有人主動想到去跑才會看到。

本檔把「有沒有跑在已知有漏洞的版本上」變成一支**會紅的測試**，理由很簡單：這台
機器上同時有三份不同的相依組合——測試直譯器（`py -3`）、正式直譯器（`.venv`）、
fresh clone 拿到的當下最新版——而全套測試是兩個直譯器**各跑一次**的，所以只要哪
一份漂下去，那一次的測試就會紅在那一份上，不需要有人記得去比對。

**下限不是釘版本。** `requirements.txt` 全檔刻意不寫 `==`（fresh clone 應該拿到
最新版），`>=` 擋的是往下漂、不擋往上升，兩者不衝突。這一點由
`test_requirements_declares_no_exact_pins` 一起守著，免得哪天有人為了「重現環境」
把下限改成等號、順手推翻那個決定。
"""
from __future__ import annotations

import ast
import functools
import subprocess  # nosec B404
import importlib.metadata as metadata
import json
import os
import re
import site
import sys
import sysconfig
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

import audit_dependencies as audit

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"

# 必須帶安全下限的套件。**這是清單制**：這裡列出來的，`requirements.txt` 就一定
# 要有 `>=`，否則 `test_the_security_floors_stay_declared` 會紅。存在的理由是刪一
# 個下限太安靜了——把 `aiohttp>=3.14.3` 改回 `aiohttp` 不會讓任何測試變色，而那正
# 是本檔要防的那個狀態。
#
# 判準是「這個版本以下有**本專案搆得到**的已知漏洞」，不是「有新版可以升」。要拿
# 掉一筆，得先說明為什麼那條路徑已經搆不到了。
_MUST_HAVE_A_FLOOR = {
    # 第三方圖庫 API 的回應直接餵進它的 C 剖析器。
    "aiohttp",
    # 第三方圖庫回傳的**影像位元組**直接餵進它。
    "pillow",
    # selenium／requests 底下的 HTTP 層。
    "urllib3",
}


def _declared() -> list[Requirement]:
    """`requirements.txt` 裡的每一行相依（去掉註解與空行）。"""
    out = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(Requirement(line))
    return out


# ---- editable 安裝：dist-info 上的版本**不是**跑起來的那份程式 -------------
#
# `pip install -e <路徑>` 之後，site-packages 裡只留下中繼資料，`import` 實際解析
# 到的是那個原始碼樹。中繼資料的版本是**安裝當下**凍結的，原始碼樹之後再怎麼往前
# 走都不會回頭改它。實測 2026-09-09（本機兩個直譯器都一樣）：
#
#   importlib.metadata.version("je-auto-control")  ->  0.0.195
#   find_spec("je_auto_control").origin            ->  <AutoControlGUI 的本機 checkout>\...
#   那棵樹的 pyproject.toml                          ->  0.0.221
#
# 也就是說本檔問「裝了哪個版本」時，對這個套件拿到的是一個**跟執行中的程式沒有
# 關係的數字**，而且落後 26 個版本。今天還沒出事只因為 `je-auto-control` 剛好沒有
# 宣告下限（沒有 specifier 的套件會被略過）；有人哪天為了某個新 API 補上
# `je-auto-control>=0.0.200`——那正是 `je_web_runner>=0.0.88` 補下限的理由，也是
# 最該相信這道守門的時刻——這裡就會拿 0.0.195 去比，紅得莫名其妙，而把下限調低
# 「修好」它會讓守門從此失效。DoD #4 說的「哪一份漂下去就會紅在那一份上」對
# editable 套件是**靜靜地不成立**的。
#
# 判準用 PEP 610 的 `direct_url.json`（`dir_info.editable is True`），不是去猜
# `__editable__*.pth` 的檔名——那個名字是 setuptools 的實作細節，換一個建置後端
# （hatchling／pdm／flit）就長得不一樣；`direct_url.json` 是安裝器一定會寫的標準
# 欄位，本機這兩個直譯器上都有。
#
# **實作住在 `audit_dependencies.py`，這裡只是取個私有別名。** 同一條規則現在有
# 兩個消費者——這份守門，以及那支工具的 `--fresh` 模式——兩邊各寫一份就會分岔，
# 而分岔的症狀不是壞掉而是「其中一邊安靜地用舊判準」。方向也只能是這樣：工具不該
# 反過來 import 測試模組，否則它在沒有測試檔的環境裡就跑不起來。
# 由 `test_the_editable_lookup_has_exactly_one_implementation` 兩個方向都釘住。
_editable_source_dir = audit.editable_source_dir
_source_tree_version = audit.source_tree_version
_effective_version = audit.effective_version


@functools.lru_cache(maxsize=1)
def _site_packages_versions() -> dict[str, str]:
    """`{正規化套件名: 版本}`，**只看這個直譯器的 site-packages**。

    為什麼要限定範圍：`importlib.metadata.version()` 掃的是當下的 `sys.path`，而
    `webrunner_novelai.py` / `webrunner_je_only.py` 在 import 期會把 sibling
    checkout（`D:\\Work\\WebRunner`）插到 `sys.path[0]`。那份 checkout 自帶
    `je_web_runner` 的中繼資料，版本跟 site-packages 裝的那一份**不一樣**，於是
    「裝了哪個版本」這個問題的答案會隨著「這個 pytest 行程裡誰先被 import」而變。

    2026-09-03 實測：單獨跑這個檔案回 0.0.88（通過），先跑
    `test_selenium_facade.py`（它會 import `webrunner_novelai`）之後再跑就回
    0.0.85（失敗）。目前全套會過只是因為字母序 `d` 排在 `s` 前面——加一個檔名排更
    前面、又會 import webrunner 的測試，這支就會翻臉。**會看順序的守門等於沒有
    守門**：它可能亂叫，也可能在真的該叫的時候剛好安靜。

    這裡問的問題是「pip 對這個直譯器裝了什麼」，所以答案就該只從 site-packages
    來。sibling checkout 是**執行期**的覆蓋，那是 `test_je_facade.py` 負責的另一
    個問題（它刻意分成「這台機器跑起來會不會炸」與「只照 requirements.txt 裝的
    機器會不會炸」兩支）。
    """
    roots = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}
    roots.update(getattr(site, "getsitepackages", lambda: [])())
    roots = sorted(r for r in roots if r and os.path.isdir(r))
    found: dict[str, str] = {}
    for dist in metadata.distributions(path=roots):
        try:
            raw = dist.metadata["Name"]
        except Exception:  # pylint: disable=broad-except
            continue
        if not raw:
            continue
        key = raw.strip().lower().replace("-", "_")
        try:                       # editable 的話中繼資料上的版本不算數，見上面
            direct_url = dist.read_text("direct_url.json")
        except Exception:          # pylint: disable=broad-except
            direct_url = None
        # 同名多份時保留第一個（site-packages 的搜尋順序就是 import 順序）。
        found.setdefault(key, _effective_version(dist.version, direct_url))
    return found


def _installed(name: str) -> str | None:
    key = name.strip().lower().replace("-", "_")
    got = _site_packages_versions().get(key)
    if got is not None:
        return got
    try:                       # site-packages 之外（editable、系統套件）的後路
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _floor_shortfalls(lookup, *, where: str) -> list[str]:
    """`requirements.txt` 的版本條件在某一份環境裡沒被滿足的那幾筆。

    **一份實作，兩個環境。** 這裡原本是兩份：當前直譯器那支抽第一個 `>=` 出來比
    大小，`.venv` 那支用 `req.specifier.contains(...)` 比整組條件。今天兩者答案
    一樣（有條件的四筆都只寫了 `>=`），所以分岔完全沒有症狀——而分岔的代價是往
    後只有其中一邊會看見 `!=` / `<` 這種條件。兩個消費者共用這一支之後，
    `test_both_environments_use_the_same_floor_comparison` 釘住不准再長出第二份。

    判準用 `contains(..., prereleases=True)`。⚠️ **這個旗標在本機目前這版是
    no-op，2026-09-20 實測**（packaging 26.2，六組輸入兩種寫法答案完全相同）：
    `SpecifierSet.prereleases` 在沒有任何 specifier 自己接受預先發行版時回的是
    `None` 而不是 `False`，而 `contains` 的 docstring 寫明 `None` 依 PEP 440 的
    建議**會**比對預先發行版。所以變異測試把這個旗標拿掉會活下來，那是等價變異，
    不是測試不夠力。

    旗標仍然留著，而且理由不是「以防萬一」：舊版 packaging 那個屬性回的是
    `False`，於是每一個預先發行版都會被排除在比對之外——`3.15.0rc1` 明明高於
    `>=3.14.3`，卻會被報成不滿足。那是**亂叫**的方向，而 `requirements.txt` 刻意
    不釘版本、fresh clone 拿到的是當下最新版，所以哪個 packaging 會被解析出來不在
    我們手上。明寫旗標＝不管解析到哪一版，語意都是我們要的那個。

    沒裝的套件在這裡一律跳過：那是 `test_every_requirement_is_actually_installed`
    與 `test_every_declared_dependency_is_installed_in_the_venv` 的守備範圍，在這
    裡一起報只會讓兩個問題共用一句訊息。
    """
    shortfalls = []
    for req in _declared():
        if not req.specifier:
            continue
        version = lookup(req.name)
        if version is None:
            continue
        if not req.specifier.contains(Version(version), prereleases=True):
            shortfalls.append(f"{req.name}：{where} 上是 {version}，"
                              f"不滿足 {req.specifier}")
    return shortfalls


def test_every_declared_floor_is_met():
    """宣告了下限的套件，當前直譯器裝的版本不得低於它。

    紅了就是「這個直譯器正跑在已知有漏洞的版本上」。修法是升級**這個**直譯器，
    不是把下限調低——下限旁邊的註解寫著它擋的是哪一條。
    """
    for req in _declared():
        if not req.specifier:
            continue
        assert _installed(req.name) is not None, (
            f"`{req.name}` 宣告在 requirements.txt 裡卻沒裝在這個直譯器上。"
            "DoD #4：加進 requirements.txt 的相依必須當場 pip install。")
    behind = _floor_shortfalls(_installed, where="這個直譯器")
    assert not behind, (
        "這個直譯器裝的版本低於 requirements.txt 宣告的安全下限："
        + "；".join(behind)
        + "。請對**這個**直譯器 `pip install -U <套件>`。"
        "注意本機有多個直譯器（測試用的 `py -3` 與正式用的 `.venv`），"
        "升級一個不會連帶升另一個。"
        "正式行程活著的時候升級是安全的——已載入的模組留在記憶體裡不受影響，"
        "要下一次重新啟動才會吃到新版。")


# 下限比對的合成對照組。
#
# 為什麼需要：分支覆蓋率量到 `if Version(have) < floor:` **只走過 False 那一邊**
# ——這份守門最核心的那個判斷，在真實資料上永遠成立不了（成立就代表機器上真的有
# 漏洞版本，那是要修的狀態，不是測試該倚賴的狀態）。所以「回報違規」那幾行從來沒
# 有執行過，把它們整段刪掉也不會有任何測試變色。同一個檔案裡其他每一道掃描都配了
# 一支 `..._actually_bites`，唯獨這兩支沒有。
_FLOOR_CASES = [
    ("aiohttp>=3.14.3", "3.13.5", True, "低於下限"),
    ("aiohttp>=3.14.3", "3.14.3", False, "剛好等於下限"),
    ("aiohttp>=3.14.3", "3.15.0", False, "高於下限"),
    # 預先發行版的兩個方向都要釘：低於下限的 rc 要報，高於下限的 rc **不准**報。
    # 後者是 must-allow 那一格——`prereleases=True` 是個放寬步驟，而放寬步驟只會被
    # 「必須放行」的案例殺掉（本 repo 記過這件事）。今天兩種寫法答案相同（見
    # `_floor_shortfalls` 的 ⚠️），所以這兩格現在釘的是語意而不是某個版本的行為。
    ("aiohttp>=3.14.3", "3.14.3rc1", True, "預先發行版低於下限"),
    ("aiohttp>=3.14.3", "3.15.0rc1", False, "預先發行版高於下限"),
    ("aiohttp>=3.14.3", None, False, "沒裝（另一支測試的守備範圍）"),
    ("aiohttp", "0.0.1", False, "沒有宣告任何條件"),
    # `>=` 以外的條件。舊的「抽第一個 `>=` 出來比大小」寫法看不見這一格，
    # 所以它同時是兩份實作合併之後的行為 pin。
    ("aiohttp>=1.0,!=2.0", "2.0", True, "被排除的版本"),
]


@pytest.mark.parametrize("spec,have,expect_bad,label", _FLOOR_CASES,
                         ids=[c[3] for c in _FLOOR_CASES])
def test_the_floor_check_actually_bites(monkeypatch, spec, have,
                                        expect_bad, label):
    """餵合成的「宣告 × 裝了什麼」，看比對會不會如實開火。"""
    monkeypatch.setattr(sys.modules[__name__], "_declared",
                        lambda: [Requirement(spec)])
    found = _floor_shortfalls(lambda _name: have, where="測試環境")
    assert bool(found) is expect_bad, f"{label}：{spec} / {have} → {found}"


def test_the_floor_check_has_something_to_check():
    """語料下限。`_declared()` 回空清單時，上面兩支會綠得跟「全部滿足」一模一樣。

    量到的現值：16 個相依，其中 4 個帶條件（都是 `>=`）。門檻取 4 是因為
    `_MUST_HAVE_A_FLOOR` 那三個加上 `je_web_runner` 就是現在的全部，掉下去代表
    有人把下限拿掉了而 `test_the_security_floors_stay_declared` 沒接到。
    """
    declared = _declared()
    assert len(declared) >= 10, f"只讀到 {len(declared)} 個相依，解析器壞了。"
    with_specifier = [req for req in declared if req.specifier]
    assert len(with_specifier) >= 4, (
        f"只有 {len(with_specifier)} 個相依帶著版本條件"
        f"（{[r.name for r in with_specifier]}）——下限比對沒有語料可比，"
        "那兩支會綠得跟「全部滿足」一模一樣。")


def test_the_corpus_floor_fires_when_the_declarations_dry_up(monkeypatch):
    """上面那道語料下限**自己**也要有對照組。

    變異實測：把 `>= 4` 放寬成 `>= 0` 活了下來——乾淨的資料上量不出下限，所以下限
    本身在真實輸入上永遠不會開火，刪掉它不會有任何症狀。這一支餵一個「宣告還在、
    但沒有任何版本條件」的假 `requirements.txt`，那正是把下限一個個拿掉之後的樣子。
    """
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_declared",
                        lambda: [Requirement(f"pkg{n}") for n in range(12)])
    with pytest.raises(AssertionError, match="沒有語料可比"):
        test_the_floor_check_has_something_to_check()


def test_both_environments_use_the_same_floor_comparison():
    """兩個環境的下限比對只准有一份實作。

    合併之前這裡是兩份：一份抽 `>=` 比大小、一份用 `specifier.contains`。兩份都
    綠，而且**沒有任何東西在比對它們**——這正是本 repo 記過的「平行測試不是對等
    測試」。所以這裡用 AST 盯著：那兩支測試都得把工作交給 `_floor_shortfalls`，
    而且自己身上不准再出現版本比較。
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    wanted = {"test_every_declared_floor_is_met",
              "test_the_venv_also_meets_every_security_floor"}
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in wanted:
            continue
        seen.add(node.name)
        calls = {ast.unparse(sub.func) for sub in ast.walk(node)
                 if isinstance(sub, ast.Call)}
        assert any(name.endswith("_floor_shortfalls") for name in calls), (
            f"`{node.name}` 不再走共用的比對——版本比對只准有一份實作。")
        own = [ast.unparse(sub) for sub in ast.walk(node)
               if (isinstance(sub, ast.Compare)
                   and any(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                           for op in sub.ops))
               or (isinstance(sub, ast.Call)
                   and ast.unparse(sub.func).endswith(".contains"))]
        assert not own, (
            f"`{node.name}` 自己又寫了一次版本比較：{own}。"
            "第二份實作不會壞掉，它只會安靜地跟第一份分岔。")
    assert seen == wanted, f"找不到這兩支測試：{sorted(wanted - seen)}"


def test_every_requirement_is_actually_installed():
    """DoD #4 的可執行版本：宣告了就要裝得起來。

    原本這條只是文件上的規定，沒有任何東西會在漏裝時出聲；漏裝的症狀是啟動器
    起不了 bot，而那要等到下一次重新啟動才看得到。
    """
    missing = [req.name for req in _declared() if _installed(req.name) is None]
    assert not missing, (
        f"requirements.txt 宣告了 {missing} 但這個直譯器沒裝。"
        "DoD #4：加進 requirements.txt 的相依必須當場 pip install，"
        "否則啟動器下一次重新啟動時就起不來。")


def test_the_security_floors_stay_declared():
    """`_MUST_HAVE_A_FLOOR` 裡的每一個都必須真的帶著 `>=`。

    把 `aiohttp>=3.14.3` 改回 `aiohttp` 是完全無聲的——沒有測試會變色，而落後的
    版本要等下一次有人想到跑 `pip-audit` 才會被發現。所以下限的**存在**本身也要
    守。要拿掉一筆，先從上面那個集合裡拿掉，並寫清楚為什麼那條路徑已經搆不到。
    """
    floors = {req.name.lower() for req in _declared()
              if any(s.operator == ">=" for s in req.specifier)}
    missing = sorted(_MUST_HAVE_A_FLOOR - floors)
    assert not missing, (
        f"{missing} 應該在 requirements.txt 帶 `>=` 安全下限，但現在沒有。"
        "下限是刻意加的，見該行上面的註解；要移除請連同 "
        "`test_dependency_floors._MUST_HAVE_A_FLOOR` 一起改。")


def test_requirements_declares_no_exact_pins():
    """全檔不得出現 `==`。

    `requirements.txt` 的檔頭寫明不釘版本，好讓 fresh clone 拿到當下最新版；下限
    （`>=`）與這條不衝突，等號才會。這支存在是因為「為了重現環境把版本釘死」是個
    很自然的念頭，而那會安靜地推翻一個刻意的決定。
    """
    pinned = []
    for req in _declared():
        for spec in req.specifier:
            if spec.operator in ("==", "==="):
                pinned.append(f"{req.name}{spec}")
    assert not pinned, (
        f"requirements.txt 出現了等號釘版本：{pinned}。"
        "本檔刻意不釘版本（見檔頭），要擋往下漂請用 `>=` 下限。")


def test_the_floor_reasons_are_written_down():
    """每一條下限的正上方都要有註解說明它擋的是什麼。

    下限沒有理由的話，下一個人只會看到一個數字，然後在它擋路的時候直接調低它。
    這裡不驗註解的內容（那是人要讀的），只驗它存在——空白行不算隔斷，連續的
    `#` 區塊都算同一段說明。
    """
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    undocumented = []
    for i, raw in enumerate(lines):
        line = raw.split("#", 1)[0].strip()
        if not line or ">=" not in line:
            continue
        j = i - 1
        while j >= 0 and not lines[j].split("#", 1)[0].strip():
            if lines[j].lstrip().startswith("#"):
                break
            j -= 1
        if j < 0 or not lines[j].lstrip().startswith("#"):
            undocumented.append(line)
    assert not undocumented, (
        f"這些下限沒有寫理由：{undocumented}。"
        "在該行正上方補一段註解，寫清楚它擋的是哪一條漏洞、"
        "以及本專案是從哪個呼叫點搆得到它。")


# 註解裡的公告編號，以及它後面那個「修於哪一版 / 搆不到」的標記。
#
# 標記是 2026-09-11 加的約定，理由寫在 `requirements.txt` 的檔頭：在那之前，下限的
# 理由（點名三條公告）和下限本身（一個版本號）之間**沒有任何東西把它們對起來**，
# 所以把 `Pillow>=12.3.0` 調成 `>=12.2.0` 之後註解會變成假話，而全套測試照綠。
_ADVISORY_RE = re.compile(r"(?:PYSEC|GHSA|CVE)-[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*")
_LABELLED_RE = re.compile(
    r"(?P<id>(?:PYSEC|GHSA|CVE)-[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*)"
    r"（(?:修於 (?P<fixed>[0-9][0-9A-Za-z.]*)|搆不到)）")


def _rationale_blocks(*, floors_only: bool) -> dict[str, tuple[Requirement, str]]:
    """每一條相依，配上它正上方那段連續的 `#` 註解。

    `floors_only=True` 只收帶 `>=` 的那些（下限對帳用）；`False` 收全部，給
    「沒有下限卻寫了『修於 X』」那條檢查用。兩者刻意共用**同一段**往上走的規則：
    寫成兩份拷貝的話，段落切割日後的修正只會落在其中一份，而另一份不會有任何症狀
    ——本檔別處已經為了同一個形狀修過好幾次了。

    往上走的規則跟 `test_the_floor_reasons_are_written_down` 一致（空白行可以跳過、
    連續的 `#` 算同一段），差別只在這裡要把整段**收回來**而不是只確認它存在。
    段落在遇到空白行或程式碼行時結束——檔頭那一大段因此不會黏到第一個套件上，
    `test_the_rationale_blocks_do_not_swallow_the_file_header` 釘住這件事。
    """
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    found: dict[str, tuple[Requirement, str]] = {}
    for i, raw in enumerate(lines):
        code = raw.split("#", 1)[0].strip()
        if not code or (floors_only and ">=" not in code):
            continue
        req = Requirement(code)
        j = i - 1
        while j >= 0 and not lines[j].split("#", 1)[0].strip():
            if lines[j].lstrip().startswith("#"):
                break
            j -= 1
        block: list[str] = []
        while j >= 0 and lines[j].lstrip().startswith("#"):
            block.append(lines[j])
            j -= 1
        found[req.name.lower()] = (req, "\n".join(reversed(block)))
    return found


def _floor_rationales() -> dict[str, tuple[Requirement, str]]:
    """帶 `>=` 的相依配上它的理由段——下限對帳用。"""
    return _rationale_blocks(floors_only=True)


def _every_rationale() -> dict[str, tuple[Requirement, str]]:
    """**每一條**相依配上它的理由段，不只帶 `>=` 的那些。

    這個較寬的視野是必要的，因為 `_shortfalls` 在沒有下限時直接早退：一段寫在
    **無下限**套件上方的「（修於 X）」，在只收 `>=` 的視野裡從頭到尾都不存在。
    """
    return _rationale_blocks(floors_only=False)


def _floor_of(req: Requirement) -> Version | None:
    floors = [Version(s.version) for s in req.specifier if s.operator == ">="]
    return max(floors) if floors else None


def _shortfalls(req: Requirement, block: str) -> list[str]:
    """下限沒有蓋住的公告——回傳的是給人看的字串，空 list 代表一致。

    純函式，不碰磁碟，好讓 `test_the_shortfall_check_actually_bites` 能餵合成資料
    進來。守的是「下限被調低、理由卻留在原地」那個**無聲**的狀態：真實資料一致時
    這個比較式刪掉也照綠，所以它必須有自己的正對照。
    """
    floor = _floor_of(req)
    if floor is None:
        return []
    bad = []
    for match in _LABELLED_RE.finditer(block):
        fixed = match.group("fixed")
        if fixed is None:
            continue
        if floor < Version(fixed):
            bad.append(f"{match.group('id')} 修於 {fixed}，但下限只有 {floor}")
    return bad


def test_every_advisory_named_beside_a_floor_carries_its_fix_version():
    """下限註解裡點到的公告編號，後面都要接 `（修於 X.Y.Z）` 或 `（搆不到）`。

    沒有這個標記，「下限夠不夠」就只能靠人去查漏洞資料庫——那要連網，做不成測試。
    標記把那一次查詢的結果留在檔案裡，之後就變成純粹的本檔內部一致性。
    `（搆不到）` 是給「這條有列出來、但本專案的呼叫路徑到不了」用的，寫了它就不會
    被要求抬下限。
    """
    unlabelled: list[str] = []
    for name, (_req, block) in _floor_rationales().items():
        labelled = {m.group("id") for m in _LABELLED_RE.finditer(block)}
        for match in _ADVISORY_RE.finditer(block):
            if match.group(0) not in labelled:
                unlabelled.append(f"{name}: {match.group(0)}")
    assert not unlabelled, (
        f"這些公告編號沒有標記修復版本：{unlabelled}。"
        "在編號後面補上 `（修於 X.Y.Z）`，或是 `（搆不到）` 如果本專案的呼叫路徑"
        "到不了它。約定與理由寫在 requirements.txt 的檔頭。")


def test_the_floor_clears_every_advisory_it_names():
    """下限必須 ≥ 它正上方那段註解裡每一條公告的修復版本。

    這是本檔那個 fail-open 缺口的補丁：`test_every_declared_floor_is_met` 比的是
    「裝著的版本 ≥ 下限」，`test_the_floor_reasons_are_written_down` 明寫不驗註解
    內容，所以「把下限調低、理由整段留著」在 2026-09-11 之前是完全無聲的。
    """
    rationales = _floor_rationales()
    assert len(rationales) >= 4, (
        f"只抓到 {len(rationales)} 條帶下限的相依——剖析大概壞了，"
        "而剖析壞掉的樣子跟「全部一致」一模一樣。")
    failures: list[str] = []
    for name, (req, block) in rationales.items():
        failures += [f"{name}: {msg}" for msg in _shortfalls(req, block)]
    assert not failures, (
        f"下限沒有蓋住它自己點名的公告：{failures}。"
        "要嘛把下限抬到修復版本，要嘛把那條公告改標成 `（搆不到）` 並寫清楚"
        "為什麼本專案的呼叫路徑到不了它。")


def test_every_security_floor_actually_names_an_advisory():
    """`_MUST_HAVE_A_FLOOR` 的每一個都要在註解裡點到至少一條公告。

    沒有這一支，上面那支對一個「有下限、但註解沒點名任何公告」的套件會**靜靜通過**
    ——沒有公告就沒有東西可比，空集合的比較永遠成立，看起來跟一致一模一樣。安全
    下限的定義就是「擋某一條搆得到的漏洞」，所以點不出公告的安全下限本身就可疑。
    """
    rationales = _floor_rationales()
    silent = []
    for name in sorted(_MUST_HAVE_A_FLOOR):
        entry = rationales.get(name)
        if entry is None or not _LABELLED_RE.search(entry[1]):
            silent.append(name)
    assert not silent, (
        f"這些安全下限沒有在註解裡點名任何公告：{silent}。"
        "安全下限的理由就是某一條搆得到的漏洞，點不出來的話它可能只是"
        "一次例行升級，應該連同 `_MUST_HAVE_A_FLOOR` 一起檢討。")


@pytest.mark.parametrize("floor, fixed, expect_bad", [
    ("12.3.0", "12.3.0", False),
    ("12.4.0", "12.3.0", False),
    ("12.2.0", "12.3.0", True),
    ("3.14.3", "3.14.2", False),
    ("2.6.3", "2.7.0", True),
])
def test_the_shortfall_check_actually_bites(floor, fixed, expect_bad):
    """比較式本身的正對照——真實資料一致時，刪掉它也照綠。

    合成一段註解餵進 `_shortfalls`，確認「下限低於修復版本」真的會被指出來、
    而「等於」與「高於」不會誤報。沒有這一支，上面那支就是一個永遠成立的斷言。
    """
    req = Requirement(f"pkg>={floor}")
    block = f"# **下限 {floor}。** PYSEC-2026-9999（修於 {fixed}）——合成的公告。"
    assert bool(_shortfalls(req, block)) is expect_bad


def test_the_unreachable_label_exempts_an_advisory_from_the_floor_check():
    """`（搆不到）` 要真的讓一條公告不參與下限比較。

    這個逃生口是必要的：註解裡可以合理地列出「查過、但搆不到」的公告，而那些不應該
    逼著下限往上抬。但逃生口也是最容易被拿來消紅字的東西，所以它自己要有測試——
    否則「標成搆不到」跟「修好」在測試輸出裡長得一樣。
    """
    req = Requirement("pkg>=1.0.0")
    exempt = "# PYSEC-2026-9999（搆不到）——列出來但路徑到不了。"
    # ⚠️ 先確認這個寫法**被認得**，再確認它被放行。少了這一句，「逃生口有效」與
    # 「正規表達式根本看不到這個寫法」會給出一模一樣的 `== []`——而後者是個 bug：
    # 沒被認得就等於沒標記，`..._carries_its_fix_version` 該對它紅字才對。
    assert _LABELLED_RE.search(exempt) is not None, (
        "`（搆不到）` 沒有被 `_LABELLED_RE` 認出來——那它就不算標記過，"
        "下面那句 `== []` 只是因為根本沒掃到東西。")
    assert _shortfalls(req, exempt) == []
    assert _shortfalls(req, "# PYSEC-2026-9999（修於 2.0.0）——搆得到。") != []


def _unprotected_reachable(req: Requirement, block: str) -> list[str]:
    """註解主張「搆得到、修於 X」但這條相依根本沒有下限——純函式，好餵合成資料。

    `_shortfalls` 補不到這一塊：它拿到 `floor is None` 就早退，而在 2026-09-17
    之前連收集的那一步都只看帶 `>=` 的行，所以這個組合**兩個方向都看不見**。
    症狀是最糟的那種——`requirements.txt` 上看起來有人在管這條公告（編號、修復
    版本、理由都寫齊了），實際上沒有任何東西擋住版本往下漂。
    """
    if _floor_of(req) is not None:
        return []                      # 有下限的交給 `_shortfalls` 管，別重複報
    return [f"{m.group('id')} 標成「修於 {m.group('fixed')}」"
            for m in _LABELLED_RE.finditer(block)
            if m.group("fixed") is not None]


def test_a_reachable_advisory_may_not_sit_above_a_floorless_dependency():
    """寫了「修於 X」就等於主張這條搆得到，那它就必須有下限。

    語意依據本檔既有的約定：`（搆不到）` 才是「查過、但路徑到不了」該用的標記，
    所以那個逃生口不受影響；會紅的只有「主張搆得到、卻沒有下限」這一種組合。
    """
    rationales = _every_rationale()
    assert len(rationales) >= 10, (
        f"只抓到 {len(rationales)} 條相依——剖析大概壞了，"
        "而剖析壞掉的樣子跟「全部一致」一模一樣。")
    failures: list[str] = []
    for name, (req, block) in rationales.items():
        failures += [f"{name}: {msg}"
                     for msg in _unprotected_reachable(req, block)]
    assert not failures, (
        f"這些公告被標成「搆得到、修於某版」，但那條相依沒有任何下限：{failures}。"
        "要嘛加上 `>=` 下限（並視情況加進 `_MUST_HAVE_A_FLOOR`），"
        "要嘛改標成 `（搆不到）` 並寫清楚為什麼路徑到不了。")


@pytest.mark.parametrize("spec, block, expect_bad", [
    # 沒有下限 ＋ 主張搆得到 -> 這正是要抓的
    ("pkg", "# CVE-2026-9999（修於 2.0.0）——搆得到。", True),
    # 沒有下限 ＋ 標成搆不到 -> 放行（逃生口要留著）
    ("pkg", "# CVE-2026-9999（搆不到）——路徑到不了。", False),
    # 有下限 -> 這支不管，交給 `_shortfalls`
    ("pkg>=2.0.0", "# CVE-2026-9999（修於 2.0.0）——搆得到。", False),
    ("pkg>=1.0.0", "# CVE-2026-9999（修於 2.0.0）——下限不夠，但不歸這支管。", False),
    # 沒有公告編號 -> 沒事
    ("pkg", "# 一般說明，沒有點名任何公告。", False),
])
def test_the_floorless_check_actually_bites(spec, block, expect_bad):
    """正對照：真實資料是乾淨的，所以這個判斷式在真實資料上驗不動。

    第三、四個案例是 must-allow，用來殺「把 `_floor_of(req) is not None` 那道早退
    拿掉」的變異——只有 must-block 的語料殺不掉它，拿掉之後真實樹仍然全綠。
    """
    assert bool(_unprotected_reachable(Requirement(spec), block)) is expect_bad


def test_the_rationale_blocks_do_not_swallow_the_file_header():
    """註解段落要在空白行處停住，不能一路吃到檔頭。

    檔頭那一大段解釋了 `（修於 …）` 這個約定，本身就含有公告編號；如果段落切割壞掉
    而把檔頭黏到第一個套件身上，每一個下限都會繼承檔頭裡的公告，
    `test_the_floor_clears_every_advisory_it_names` 就會對著不相干的套件亂紅。
    """
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    header_marker = "公告編號後面的"
    assert any(header_marker in line for line in lines[:60]), (
        "檔頭那段說明不見了——這支測試的前提沒了，"
        "請確認 requirements.txt 檔頭是否被改寫。")
    # 用**較寬**的那個視野：往上走時空白行是可以跨過去的，所以真正有風險的是
    # 「檔頭底下第一個套件」，而它不見得帶 `>=`。只看 `_floor_rationales()` 的話，
    # 一個無下限的第一個套件把整個檔頭吸進來也不會有任何症狀——2026-09-17 實測，
    # 檔案裡第一個套件正好就是無下限的那種。
    # 綁成同一個變數再檢查，是為了讓下面那個迴圈**真的**受這句話管：把兩者寫成
    # 各自呼叫一次的話，「迴圈換成窄視野」這個變異不會碰到這句 assert，於是它照綠
    # ——2026-09-17 變異測試實測過，分開寫的版本 SURVIVED。
    rationales = _every_rationale()
    assert set(rationales) - set(_floor_rationales()), (
        "這支測試拿到的視野一條都沒有比 `_floor_rationales()` 多——那它實際上只看了"
        "有下限的那幾條，而檔頭正下方那個套件不保證有下限（實測：它就是無下限的）。")
    for name, (_req, block) in rationales.items():
        assert header_marker not in block, (
            f"`{name}` 的理由段落吃到了檔頭——往上走的終止條件壞了。")


@pytest.mark.parametrize("name", sorted(_MUST_HAVE_A_FLOOR))
def test_the_floor_names_resolve_to_a_real_distribution(name):
    """`_MUST_HAVE_A_FLOOR` 的名字必須真的對得上一個裝著的套件。

    這個集合是手寫的，拼錯（`pillow` 寫成 `pil`）之後
    `test_the_security_floors_stay_declared` 會永遠紅，看起來像下限不見了，其實
    是名字錯了。分開驗，錯誤訊息才指得到正確的地方。
    """
    assert _installed(name) is not None, (
        f"`{name}` 對不上任何裝著的套件——"
        "`_MUST_HAVE_A_FLOOR` 裡的名字要用套件的發行名稱。")


def test_the_parser_only_sees_real_requirement_lines():
    """檔頭那一大段註解不能被當成相依。

    `requirements.txt` 的註解比相依行還多（每一條都寫了為什麼要它），所以剖析器
    把註解讀成套件名這件事會**安靜地**發生——多出來的「相依」查不到版本，
    `test_every_requirement_is_actually_installed` 會紅在一個完全看不懂的名字上。
    """
    names = [req.name for req in _declared()]
    assert names, "requirements.txt 解不出任何相依，剖析器壞了。"
    assert "aiohttp" in names and "psutil" in names
    for name in names:
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name), (
            f"解出了不像套件名的東西：{name!r}——註解大概被吃進來了。")


# ---------- 兩個直譯器的版本落差 --------------------------------------------
# 上面那幾支守的是「有沒有跑在已知有漏洞的版本上」，只對**列了下限**的套件有效。
# 沒有下限的套件（selenium、je_web_runner、matplotlib…）則完全沒有訊號——而這台
# 機器上有兩份會被實際使用的相依組合：
#
#   `py -3`  = 跑測試的那一份
#   `.venv`  = bot 與 webrunner 真正跑的那一份
#
# 兩份不一致就等於「驗的是一套、出貨的是另一套」。2026-08-29 實測到的狀態：
# selenium 測試端 4.41.0、正式端 4.44.0、fresh clone 拿到 4.48.0 —— 三份全不同，
# 而 selenium 是這個專案行為上最關鍵的相依。整套測試 968 支全綠，沒有任何一支看
# 得到這件事。
#
# 這支不是在管「有沒有升到最新」（那需要連網，而且會天天紅）。它只問一個本機就
# 答得出來、而且答案應該永遠是「一樣」的問題：**同一台機器上的兩份環境，宣告過的
# 相依版本一不一致。** 15 個宣告相依裡當時只有 3 個不一致，所以這是精準的訊號，
# 不是會亂叫的守門。修法也只有一行 `pip install -U <套件>`。
_VENV_SITE_PACKAGES = (REQUIREMENTS.parent / ".venv" / "Lib" / "site-packages")


def _normalise(name: str) -> str:
    """PEP 503／427 的檔名正規化：`discord.py` → `discord_py`。"""
    return re.sub(r"[-_.]+", "_", name).lower()


def _dist_info_versions(site_packages: Path) -> dict[str, str]:
    """一個 `site-packages` 目錄裡每個套件的版本，讀 `*.dist-info` 目錄名。

    刻意**不**開子行程問版本：起一個直譯器只為問版本太慢，而且回來的文字還要處理
    編碼（本機 locale 是 cp950）。目錄名本身就是 PEP 427 規定的
    `名字-版本.dist-info`。
    """
    out: dict[str, str] = {}
    if not site_packages.is_dir():
        return out
    for entry in site_packages.iterdir():
        if not entry.name.endswith(".dist-info"):
            continue
        stem = entry.name[: -len(".dist-info")]
        if "-" not in stem:
            continue
        name, _, version = stem.rpartition("-")
        # 這裡也要問一次 editable：`.venv` 的 `je_auto_control-0.0.195.dist-info`
        # 指的是 `<AutoControlGUI 的本機 checkout>` 那棵樹，目錄名上的 0.0.195 只是安裝當
        # 下的殘影。少了這一行，兩個直譯器「一致」會是**兩邊一起錯**。
        try:
            direct_url = (entry / "direct_url.json").read_text(encoding="utf-8")
        except OSError:
            direct_url = None
        out[_normalise(name)] = _effective_version(version, direct_url)
    return out


def _venv_versions() -> dict[str, str]:
    return _dist_info_versions(_VENV_SITE_PACKAGES)


@functools.lru_cache(maxsize=1)
def _py3_site_packages() -> Path | None:
    """`py -3` 的 `site-packages`；問不到回 `None`。**一個 session 只問一次。**

    這裡非開子行程不可：`.venv` 的位置是算得出來的（repo 底下），`py -3` 的不是。
    輸出是路徑，但仍然明講編碼並帶 `errors="replace"`——它是子行程的 stdout，
    照 `CLAUDE.md` 的跨領域硬規則辦。
    """
    try:
        proc = subprocess.run(                                    # nosec B603
            ["py", "-3", "-c",
             "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    path = Path(proc.stdout.strip())
    return path if path.is_dir() else None


def _py3_versions() -> dict[str, str]:
    site = _py3_site_packages()
    return _dist_info_versions(site) if site else {}


def test_the_two_interpreters_agree_on_every_declared_dependency():
    """`py -3` 與 `.venv` 上，宣告過的相依必須是同一個版本。

    不一致 = 驗的是一套、出貨的是另一套。這在本機是完全沒有訊號的：兩邊的測試都
    會過，因為每一邊都只看得到自己。

    **比對的是兩個具名的直譯器，不是「現在跑著的這個」（2026-09-10 修）。** 舊版
    拿 `_installed()`（＝當前直譯器）去跟 `.venv` 比，並在「當前就是 `.venv`」時
    跳過。那在只有兩個環境時等價，但**第三個環境一出現就會誤報**：拿一個拋棄式
    venv 去評估某個套件能不能升（正是 §8.12 說該做的事）時，這支會紅，而它給的
    修法——「把落後的那一邊升上去」——對那個情境是**錯的建議**：那兩份本來就故意
    不一樣。實際踩到的是 selenium 4.49.0 的評估。

    改成兩邊都用 `site-packages` 目錄讀之後還多賺一件事：用 `.venv` 跑時不再跳過，
    所以這道對帳兩輪都會執行，而不是只掛在 `py -3` 那一輪上。
    """
    venv = _venv_versions()
    if not venv:
        pytest.skip(".venv 不存在（fresh clone）——沒有第二份環境可以比對")
    py3 = _py3_versions()
    if not py3:
        pytest.skip("問不到 `py -3` 的 site-packages——沒有第二份環境可以比對")
    drift = []
    for req in _declared():
        key = _normalise(req.name)
        here = py3.get(key)
        there = venv.get(key)
        if here is None or there is None:
            continue                      # 缺套件由另外兩支測試負責
        if here != there:
            drift.append(f"{req.name}: py -3 {here} / .venv {there}")
    assert not drift, (
        "兩份環境的相依版本不一致——測試驗的是一套、bot 與 webrunner 跑的是另一"
        f"套：{drift}。修法是把落後的那一邊升上去："
        r"`.venv\Scripts\python.exe -m pip install -U <套件>` 或 "
        "`py -3 -m pip install -U <套件>`，然後兩邊各跑一次全套測試。")


def test_the_agreement_check_does_not_depend_on_which_interpreter_runs_it():
    """釘住上一支的**比對對象**：兩個具名環境，不是當前直譯器。

    這條沒有東西守的話會安靜地退回去——把 `py3.get(key)` 換回 `_installed(...)`
    在本機兩個環境上完全看不出差別（那正是它當初能存活的原因），只有第三個環境
    出現時才會誤報，而那時候人會以為是自己的環境有問題。

    用 AST 檢查：那支測試的函式體裡不得出現 `_installed`。
    """
    import ast
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), __file__)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "test_the_two_interpreters_agree_on_every_declared_dependency")
    used = {node.func.id for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "_installed" not in used, (
        "那支對帳又改回讀「當前直譯器」了。比對對象必須是兩個具名環境"
        "（`_py3_versions()` / `_venv_versions()`），否則從第三個環境跑就會誤報，"
        "而且訊息會叫人去升一個不該升的東西。")
    assert {"_py3_versions", "_venv_versions"} <= used, (
        f"沒看到兩個具名環境的讀取器（實際用到：{sorted(used)}）。")


def test_the_two_named_environments_are_actually_two_different_places():
    """比對對象要真的是**兩個**環境。

    這一支補的是上一支看不到的那一半：上一支只驗「呼叫了哪兩個讀取器」，驗不到
    「那兩個讀取器讀的是不是同一個目錄」。把 `_dist_info_versions` 寫成永遠讀
    `.venv`，兩邊就會逐字相同、drift 永遠是空的——對帳**通過**，而且一輩子通過。
    那正是本 repo 最怕的形狀：一道看起來在運作、實際上不可能失敗的防線。
    """
    venv = _VENV_SITE_PACKAGES
    py3 = _py3_site_packages()
    if not venv.is_dir() or py3 is None:
        pytest.skip("這台機器沒有兩個環境可以比（fresh clone 或沒有 py 啟動器）")
    assert py3.resolve() != venv.resolve(), (
        f"兩個「具名環境」指到同一個目錄（{py3}）——對帳於是永遠不可能失敗。")
    # 而且兩邊都要真的讀得到東西；有一邊是空的，迴圈裡的 `is None` 會把每一筆都
    # 跳過，同樣得到一個永遠通過的對帳。
    assert len(_py3_versions()) >= 10, (
        f"py -3 那一邊只讀到 {len(_py3_versions())} 個套件——讀取器壞了。"
        "空的一邊會讓對帳的每一筆都命中 `is None` 而被跳過。")
    assert len(_venv_versions()) >= 10, (
        f".venv 那一邊只讀到 {len(_venv_versions())} 個套件——讀取器壞了。"
        "空的一邊會讓對帳的每一筆都命中 `is None` 而被跳過。")
    # 最後一道，也是變異測試最後才逼出來的：兩個讀取器可以**路徑都對、內容卻接錯
    # 環境**。把 `_py3_versions` 的本體換成讀 `.venv`，上面每一句都還是成立
    # （路徑不同、兩邊都 ≥ 10），但對帳從此變成拿 `.venv` 跟 `.venv` 比、永遠通過。
    # 所以直接問：它回的是不是它自己宣稱的那個目錄的內容。
    assert _py3_versions() == _dist_info_versions(py3), (
        "`_py3_versions()` 回的不是 `_py3_site_packages()` 那個目錄的內容——"
        "它被接到別的環境上了，對帳會變成拿同一份跟自己比。")
    assert _venv_versions() == _dist_info_versions(venv), (
        "`_venv_versions()` 同上。")


def test_the_dist_info_reader_honours_the_directory_it_is_given(tmp_path):
    """`_dist_info_versions` 必須讀**傳進去的那個目錄**。

    這一支是變異測試逼出來的，而且它揭穿了一個我自己寫錯的釘子：上面那支比的是
    兩個**路徑**不同，但變異動的是**讀取器**——把 `_py3_versions` 改成讀 `.venv`，
    路徑檢查照樣通過、`>= 10` 也照樣通過，於是兩邊逐字相同、drift 永遠是空的。
    **對帳通過，而且一輩子通過。**

    所以判準要落在「給它一個目錄，它有沒有讀那一個」。合成一個假的
    `site-packages` 最直接：`名字-版本.dist-info` 是 PEP 427 規定的目錄名格式。
    """
    (tmp_path / "totally_made_up-1.2.3.dist-info").mkdir()
    (tmp_path / "another_one-0.9.dist-info").mkdir()
    (tmp_path / "not-a-dist-info-dir").mkdir()
    got = _dist_info_versions(tmp_path)
    assert got == {"totally_made_up": "1.2.3", "another_one": "0.9"}, got
    # 反方向：不存在的目錄要安靜回空，不是丟例外（呼叫端靠空 dict 決定跳過）。
    assert _dist_info_versions(tmp_path / "nope") == {}


def test_an_empty_side_makes_the_check_skip_rather_than_pass(monkeypatch):
    """有一邊讀不到東西時要**跳過**，不可以安靜地通過。

    這是這一族最陰的一種失效：空 dict 讓迴圈裡每一筆都命中 `here is None` 而被
    `continue` 掉，drift 於是是空的、斷言通過——一次**根本沒發生**的對帳被記成綠燈。
    兩個方向各驗一次，而且要驗**是哪一句**在跳過（跟下一支同一個理由）。

    ⚠️ **`pytest.raises(Exception)` 接不住 `pytest.skip()`。** `Skipped` 與 `Failed`
    繼承的是 `BaseException` 而不是 `Exception`，所以舊版這支的
    `with pytest.raises(Exception)` 從來沒有接到任何東西：`Skipped` 直接穿出去、
    **被記成本支測試自己的 skip**，底下那句 `assert excinfo.typename == "Skipped"`
    是死碼，而第二個方向（`_venv_versions` 空）一次都沒有執行過。

    這個缺陷能活下來，是因為它自己解釋了自己。2026-09-10 查過那一筆 skipped，結論
    寫成「本支刻意造成的，行號與訊息只是指向產品端」，還把判讀方法（用 `-v`、或單獨
    跑那支）寫進這段 docstring。結論的前半是對的——skipped 的確是本支；**但它不是
    刻意的，是接不住。** 一段把症狀描述得很完整的說明，會讓下一個人停在「已知現象」
    而不再往下追一層。2026-09-20 修好之後這個檔案回報 0 skipped，那行誤導人的
    `-rs` 輸出也跟著不見了。
    """
    mod = sys.modules[__name__]
    # 哪一邊空掉，就該由哪一句 `pytest.skip()` 出聲。只斷言「有跳過」不夠：產品端
    # 有兩句 skip，跳錯一句照樣綠，而那是「對帳根本沒跑」的另一種寫法。
    #
    # **兩邊都換掉，不是只換一邊。** 產品端的兩句 skip 有先後順序，所以在一台**本來
    # 就缺其中一個環境**的機器上（fresh clone 沒有 `.venv`，非 Windows 沒有 `py`），
    # 只換一邊的話永遠是排在前面的那一句出聲，另一個方向一次都驗不到——而那正是這
    # 支存在的理由。填一份非空的假資料給「不該空的那一邊」，這支就跟主機有幾個環境
    # 無關了。
    filled = {"pytest": "1.0"}
    expected = {"_py3_versions": "問不到 `py -3` 的 site-packages",
                "_venv_versions": ".venv 不存在"}
    for name, wanted in expected.items():
        other = next(n for n in expected if n != name)
        monkeypatch.setattr(mod, name, dict)              # 這一邊回空 dict
        monkeypatch.setattr(mod, other, lambda: dict(filled))   # 另一邊非空
        with pytest.raises(pytest.skip.Exception) as excinfo:
            test_the_two_interpreters_agree_on_every_declared_dependency()
        assert wanted in str(excinfo.value), (
            f"{name} 是空的時候跳過了，但出聲的不是對應的那一句（實際："
            f"{excinfo.value}）。")
        monkeypatch.undo()


def test_the_two_places_pin_fires_when_a_reader_comes_back_empty(monkeypatch):
    """上面那支「兩邊都讀得到東西」的下限，自己的對照組。

    真實環境兩邊都有幾百個套件，所以 `>= 10` 在正常情況下永遠成立、改不改看不出
    差別——它存在的唯一理由是不正常的那一天。
    """
    monkeypatch.setattr(sys.modules[__name__], "_py3_versions", dict)
    with pytest.raises(AssertionError) as excinfo:
        test_the_two_named_environments_are_actually_two_different_places()
    # **要斷言是哪一句在叫。** 那支測試裡有好幾道依序的斷言，只寫
    # `pytest.raises(AssertionError)` 的話，把這道下限放寬成 0 之後**後面那一句**
    # 會接著炸，控制測試照樣綠——變異測試當場示範了一次。這是今天第三次踩到
    # 「一支控制測試只證得了它真的跑到的那一行」。
    assert "只讀到 0 個套件" in str(excinfo.value), excinfo.value


def test_every_declared_dependency_is_installed_in_the_venv():
    """`.venv` 是 bot 與 webrunner 真正跑的那一份，缺一個就啟動不了。

    `test_every_requirement_is_actually_installed` 只看得到**當前**直譯器；
    正式環境缺套件的話，那一支在 `py -3` 上照樣全綠。
    """
    venv = _venv_versions()
    if not venv:
        pytest.skip(".venv 不存在（fresh clone）")
    missing = sorted(req.name for req in _declared()
                     if _normalise(req.name) not in venv)
    assert not missing, (
        f"`requirements.txt` 宣告了但 .venv 裡沒有：{missing}。"
        r"正式環境跑不起來：`.venv\Scripts\python.exe -m pip install -r "
        "requirements.txt`。")


def test_the_venv_also_meets_every_security_floor():
    """安全下限對**正式環境**才最重要，而現有的下限檢查只看得到當前直譯器。

    2026-08-29 的實際情形正是這樣：漏洞在 `.venv` 上，而 `.venv` 就是對外發 HTTP
    請求的那一份。
    """
    venv = _venv_versions()
    if not venv:
        pytest.skip(".venv 不存在（fresh clone）")
    violations = _floor_shortfalls(lambda name: venv.get(_normalise(name)),
                                   where=".venv")
    assert not violations, (
        "正式環境（.venv）沒有滿足 requirements.txt 的下限："
        f"{violations}。這一份就是實際對外發請求的那一份。")


def test_the_venv_reader_actually_reads_something():
    """反面：讀取器回空 dict 的話，上面三支會全部 skip 而且沒人會發現。

    判斷「要不要 skip」刻意看 `.venv/` 本身，**不看** site-packages 那條路徑——
    否則虛擬環境的版面一改（例如 POSIX 是 `lib/pythonX.Y/site-packages`），這支
    canary 會跟著 skip，四支一起靜音。fresh clone 沒有 `.venv/` 才是真的可以跳過。
    """
    if not (REQUIREMENTS.parent / ".venv").is_dir():
        pytest.skip(".venv 不存在（fresh clone）")
    assert _VENV_SITE_PACKAGES.is_dir(), (
        f"`.venv/` 在，但 {_VENV_SITE_PACKAGES} 不是目錄——虛擬環境的版面變了。"
        "上面三支比對會安靜地全部 skip，等於兩份環境的落差再也沒有人看。")
    venv = _venv_versions()
    assert len(venv) > 20, (
        f"從 .venv 只讀到 {len(venv)} 個套件——目錄結構變了（或讀取器壞了），"
        "上面三支會安靜地全部 skip。")
    assert "pytest" in venv or "selenium" in venv, (
        f"讀到的名字看起來不對：{sorted(venv)[:10]}")


def test_the_version_lookup_ignores_a_sibling_checkout_on_sys_path():
    """版本查詢不得受 `sys.path` 影響。

    `webrunner_novelai.py` / `webrunner_je_only.py` 在 import 期會把 sibling
    checkout（`WEBRUNNER_PATH`，預設 `<parent>/WebRunner`）插到 `sys.path[0]`，而
    那份 checkout 自帶 `je_web_runner` 的中繼資料、版本跟 site-packages 裝的那一份
    不同。`importlib.metadata.version()` 掃的是當下的 `sys.path`，所以「裝了哪個
    版本」的答案會隨著「這個 pytest 行程裡誰先被 import」而改變。

    2026-09-03 實測：單獨跑本檔通過，先跑 `test_selenium_facade.py` 再跑就失敗。
    當時全套會過純粹是因為字母序 `d` 排在 `s` 前面——**會看順序的守門等於沒有守
    門**，它可能亂叫，也可能在真的該叫的時候剛好安靜。

    這支直接製造那個情境：把 sibling 路徑插到最前面，再問一次版本。
    """
    sibling = os.environ.get("WEBRUNNER_PATH") or str(
        REQUIREMENTS.parent.parent / "WebRunner")
    if not os.path.isdir(sibling):
        pytest.skip("這台機器沒有 sibling checkout，製造不出這個情境")

    _site_packages_versions.cache_clear()
    before = _installed("je_web_runner")
    original = list(sys.path)
    try:
        sys.path.insert(0, sibling)
        _site_packages_versions.cache_clear()
        after = _installed("je_web_runner")
    finally:
        sys.path[:] = original
        _site_packages_versions.cache_clear()

    assert before == after, (
        f"把 sibling checkout 插進 sys.path 之後，版本查詢的答案從 {before} 變成 "
        f"{after}。這支守門問的是「pip 對這個直譯器裝了什麼」，答案不該取決於"
        "誰先被 import。")


# ---- editable 安裝的版本要問原始碼樹，不是問中繼資料 -----------------------
#
# 下面四支合成的才是牙齒。真實資料那一支只在**這台機器剛好有 editable 相依**時
# 才驗得到東西，而「掃到零筆」跟「全部都對」在斷言上長得一模一樣——本 repo 已經
# 為這一課吃過好幾次虧，所以純函式的對照組要自己站得住。

def _direct_url(source: Path, *, editable: bool = True) -> str:
    return json.dumps({"dir_info": {"editable": editable},
                       "url": source.as_uri()})


def _fake_source_tree(root: Path, version: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "whatever"\nversion = "{version}"\n',
        encoding="utf-8")
    return root


def test_an_editable_dependency_reports_the_version_it_actually_imports():
    """editable 的相依：查到的版本必須是**原始碼樹**宣告的那個。

    這是本檔最初漏掉的那個洞的真實資料版本。`je-auto-control` 在這台機器上是
    `pip install -e D:\\Work\\AutoControlGUI`，於是 site-packages 只有中繼資料、
    `import` 解析到那棵樹；中繼資料停在安裝當下的 0.0.195，那棵樹早就走到 0.0.221。
    """
    found = []
    for req in _declared():
        key = _normalise(req.name)
        for dist in metadata.distributions():
            try:
                if _normalise(dist.metadata["Name"] or "") != key:
                    continue
                source = _editable_source_dir(
                    dist.read_text("direct_url.json"))
            except Exception:                                  # noqa: BLE001
                continue
            if source is None:
                continue
            declared_in_tree = _source_tree_version(source)
            if declared_in_tree is None:
                continue
            found.append((req.name, dist.version, declared_in_tree))
            break

    if not found:
        pytest.skip("這台機器沒有 editable 的宣告相依，製造不出這個情境")

    for name, in_metadata, in_tree in found:
        assert _installed(name) == in_tree, (
            f"`{name}`：原始碼樹是 {in_tree}，但版本查詢回的是 "
            f"{_installed(name)!r}（中繼資料上寫 {in_metadata}）。editable 安裝的"
            "中繼資料是安裝當下凍結的，不是跑起來的那份程式。")


def test_the_editable_lookup_prefers_the_source_tree_over_stale_metadata(
        tmp_path):
    """合成對照組：中繼資料舊、原始碼樹新 → 要取原始碼樹的。"""
    source = _fake_source_tree(tmp_path / "tree", "9.9.9")
    assert _effective_version("0.0.1", _direct_url(source)) == "9.9.9"


def test_a_normal_install_keeps_its_metadata_version(tmp_path):
    """反方向：**不是** editable 的就不要多事。

    少了這一支，`_effective_version` 直接無條件去讀 pyproject 也會綠——而那會讓
    一般安裝的版本查詢開始受工作目錄影響。
    """
    source = _fake_source_tree(tmp_path / "tree", "9.9.9")
    assert _effective_version("0.0.1", _direct_url(source, editable=False)) \
        == "0.0.1"
    assert _effective_version("0.0.1", None) == "0.0.1"
    assert _effective_version("0.0.1", "not json at all") == "0.0.1"


def test_an_unreadable_source_tree_falls_back_instead_of_exploding(tmp_path):
    """原始碼樹被搬走／`pyproject.toml` 壞掉／版本是動態的 → 退回中繼資料。

    **這條的方向是刻意的**：整份相依守門不該因為某一棵樹的 pyproject 長得不一樣
    就掛掉。退回一個舊數字最多是少抓一次漂移，掛掉是連 `aiohttp` 那種真的會咬人
    的下限都不驗了。
    """
    missing = tmp_path / "gone"
    assert _effective_version("0.0.1", _direct_url(missing)) == "0.0.1"

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "pyproject.toml").write_text("{ not toml", encoding="utf-8")
    assert _effective_version("0.0.1", _direct_url(broken)) == "0.0.1"

    dynamic = tmp_path / "dynamic"
    dynamic.mkdir()
    (dynamic / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndynamic = ["version"]\n', encoding="utf-8")
    assert _effective_version("0.0.1", _direct_url(dynamic)) == "0.0.1"


def test_the_editable_detector_survives_a_windows_drive_letter(tmp_path):
    """`file:///D:/…` 的前導斜線要剝掉，否則 `Path` 拿到的是 `/D:/…`。

    本機的 `direct_url.json` 就是這個形狀，所以這一段錯了的話上面那支真實資料的
    測試會靜靜地 skip（`_source_tree_version` 讀不到檔案就回 None，而回 None 的
    項目會被跳過）——**修不好的守門與跳過的守門長得一樣**。
    """
    source = _fake_source_tree(tmp_path / "tree", "1.2.3")
    resolved = _editable_source_dir(_direct_url(source))
    assert resolved is not None
    assert resolved.resolve() == source.resolve(), (
        f"解出來的路徑是 {resolved}，原本是 {source}")


def test_the_editable_lookup_has_exactly_one_implementation():
    """可編輯安裝的判定只准有一份，而且要住在**工具**那一邊。

    同一條規則（中繼資料上的版本不是跑起來的那一份）現在有兩個消費者：這份守門，
    以及 `audit_dependencies.py --fresh`。兩邊各寫一份的下場不是「壞掉」而是
    **分岔**——其中一邊安靜地用舊判準，而兩邊都是綠的。所以這裡斷言的是 `is`
    同一個物件，不是「行為一樣」：行為比較會被兩份逐字複製的實作騙過去。

    方向也要釘住：工具不得反過來 import 測試模組，否則 `--fresh` 就變成一支要靠
    測試檔才跑得起來的工具。
    """
    assert _editable_source_dir is audit.editable_source_dir
    assert _source_tree_version is audit.source_tree_version
    assert _effective_version is audit.effective_version

    # **用 AST，不用 `in source`。** 第一版寫 `"test_dependency_floors" not in
    # source` 當場就紅了——命中的是那支工具自己**解釋這條規則**的註解。這個 repo
    # 已經吃過這個虧不只一次：字串掃描分不出「規則的說明」和「規則的違反」。
    imported = set()
    for node in ast.walk(ast.parse(
            Path(audit.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {m for m in imported if m.split(".")[0].startswith("test_")}, (
        f"`audit_dependencies.py` 反過來 import 了測試模組（{sorted(imported)}）"
        "——那會讓這支工具在沒有測試檔的環境裡跑不起來。")


# ---------- 「搆不到」這個標記自己要有守門 ------------------------------------
#
# `（搆不到）` 是本檔唯一的逃生口：標了它，一條公告就不必逼著下限往上抬。
# `test_the_unreachable_label_exempts_an_advisory_from_the_floor_check` 守的是
# **標記有沒有生效**，但沒有任何東西守「那個理由是不是還成立」——而理由通常是一句
# 關於**呼叫方式**的話，改一個參數就翻盤，且沒有任何症狀。
#
# 實例（2026-09-17）：simpleeval 1.0.8 修掉三條公告，三條**全部**要求開發者自己把
# 危險物件從 `names=` / `functions=` 餵進沙箱。本專案的唯一呼叫點是
# `discord_bot.mcmd_calc` 的 `_simple_eval(rest)`——只有運算式、沒有第二個參數，
# 所以三條都搆不到，依 `requirements.txt` 的政策不加下限。但那份安全完全建立在
# 「那些參數不存在」上：哪天有人為了支援 `sin()` 之類的東西補一個 `functions=`，
# 三條公告同一秒全部變成搆得到，而 `requirements.txt` 裡那三個 `（搆不到）` 會繼續
# 理直氣壯地寫在那裡。這正是本專案一再修的形狀（`_OWNER_ONLY_SLASH`、
# `_pid_alive` 列舉）：守門照跑、理由已經是假話、測試全綠。
#
# ⚠️ 簽名是 `simple_eval(expr, operators, functions, names, allowed_attrs)`，危險的
# 三個**都可以用位置參數傳**，所以只檢查關鍵字會漏掉 `simple_eval(rest, None, f)`。

# 套件 ＋ `test/` ＋ repo root。測試 2026-09-22 起住在 `test/`，搬家前它們就在套件的
# glob 裡，所以範圍照舊。
_SIMPLEEVAL_PROJECT_FILES = sorted(
    list((Path(__file__).resolve().parent.parent / "axiomatic").glob("*.py"))
    + list(Path(__file__).resolve().parent.glob("*.py"))
    + list(Path(__file__).resolve().parent.parent.glob("*.py")))


def _simpleeval_entry_calls(source: str) -> list[ast.Call]:
    """這份原始碼裡，每一個呼叫到 simpleeval 進入點的 `ast.Call`。

    兩種 import 形狀都要認得，因為只認一種的掃描器**看起來跟乾淨一模一樣**：
    `from simpleeval import simple_eval as _x` 走 `ast.Name`，
    `import simpleeval` 後的 `simpleeval.simple_eval(...)` 走 `ast.Attribute`。
    綁定是按**名字**比對的，所以自己定義一個同名函式不會被誤判——那是刻意的，
    這條守門管的是「餵進第三方沙箱的東西」，不是任何叫這個名字的函式。
    """
    tree = ast.parse(source)
    direct: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "simpleeval":
                direct.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "simpleeval":
                    modules.add(alias.asname or alias.name)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in direct:
            calls.append(node)
        elif (isinstance(func, ast.Attribute)
              and isinstance(func.value, ast.Name)
              and func.value.id in modules):
            calls.append(node)
    return calls


def _sandbox_widening(source: str, filename: str) -> list[str]:
    """把東西餵進 simpleeval 沙箱的呼叫點——空 list 代表三條公告仍然搆不到。"""
    problems: list[str] = []
    for call in _simpleeval_entry_calls(source):
        widened = sorted(kw.arg or "**kwargs" for kw in call.keywords)
        if len(call.args) > 1:
            widened.append(f"{len(call.args) - 1} 個額外的位置參數")
        if widened:
            problems.append(f"{filename}:{call.lineno}: {widened}")
    return problems


def test_the_sandboxed_evaluator_is_only_ever_handed_an_expression():
    """simpleeval 的呼叫點不得傳 `names=` / `functions=` / `operators=`。

    這一支釘的是 `requirements.txt` 裡三個 `（搆不到）` 標記的**前提**，不是下限
    本身。去紅它的正確做法不是把參數搬到別的地方，是回去重讀那三條公告、決定要不要
    改成加下限（`simpleeval>=1.0.8`）。
    """
    found = 0
    problems: list[str] = []
    for path in _SIMPLEEVAL_PROJECT_FILES:
        source = path.read_text(encoding="utf-8")
        if "simpleeval" not in source:
            continue
        calls = _simpleeval_entry_calls(source)
        found += len(calls)
        problems += _sandbox_widening(source, path.name)

    # 正控制：抓不到呼叫點跟「呼叫點很乾淨」在輸出上長得一模一樣。真的沒有呼叫點
    # 了（指令被拿掉）的話，該做的是連同 `requirements.txt` 那段一起刪掉，不是讓
    # 這支測試繼續綠著。
    assert found >= 1, (
        "掃不到任何 simpleeval 呼叫點——要嘛掃描器壞了，要嘛 `/fun calc` 已經拿掉。"
        "後者的話請一併移除 `requirements.txt` 的 simpleeval 那段說明。")
    assert not problems, (
        f"有呼叫點把額外參數餵進 simpleeval 沙箱：{problems}。"
        "GHSA-v7m3-47hp-2wqw／GHSA-r2q2-xmpm-7fvh／GHSA-7g86-mgxq-vq42 三條都是"
        "「開發者自己把危險物件放進 names/functions」才成立的，本專案原本搆不到。"
        "真的需要這個參數的話，請把 `simpleeval` 改成 `simpleeval>=1.0.8` 並更新"
        "`requirements.txt` 裡那三個 `（搆不到）` 標記。")


@pytest.mark.parametrize("snippet, expect_flagged", [
    # 現況：只有運算式，三條公告都搆不到。
    ("from simpleeval import simple_eval as _se\n_se(rest)\n", False),
    # 關鍵字形式——最直覺的那種放寬。
    ("from simpleeval import simple_eval as _se\n_se(rest, names={'x': 1})\n", True),
    ("from simpleeval import simple_eval as _se\n_se(rest, functions=F)\n", True),
    # ⚠️ 位置參數形式：只檢查關鍵字的掃描器會放它過去。
    ("from simpleeval import simple_eval as _se\n_se(rest, None, F)\n", True),
    # `import simpleeval` + 屬性呼叫——另一種 import 形狀。
    ("import simpleeval\nsimpleeval.simple_eval(rest, names=N)\n", True),
    ("import simpleeval\nsimpleeval.simple_eval(rest)\n", False),
    # 類別進入點也會吃 names/functions。
    ("from simpleeval import SimpleEval\nSimpleEval(names=N).eval(rest)\n", True),
    # 近似但不該命中：同名的**自己的**函式，不是從 simpleeval 來的。
    ("from mymath import simple_eval\nsimple_eval(rest, names=N)\n", False),
    # 近似但不該命中：模組名只是開頭像。
    ("import simpleevaluator\nsimpleevaluator.simple_eval(rest, names=N)\n", False),
])
def test_the_sandbox_scan_actually_bites(snippet, expect_flagged):
    """合成語料的正對照——真實樹是乾淨的，所以真實資料驗不動這個判斷式。

    樹上沒有任何違規時，`_sandbox_widening` 裡那行 `problems.append` 一次都不會
    執行，刪掉它上面那支照樣綠。兩個 must-allow 案例（自己的同名函式、開頭相像的
    模組名）是給「把綁定比對拿掉、改成看函式名字」那個變異用的：只有 must-block
    的語料殺不掉它。
    """
    assert bool(_sandbox_widening(snippet, "synthetic.py")) is expect_flagged


# --------------------------------------------------------------------------
# urllib3 的「搆不到」前提，本身要有守門
#
# 2026-09-17：urllib3 2.7.0 上有三條公告（兩條 high）修在 2.8.0，而**下限刻意留在
# 2.7.0**，理由寫在 `requirements.txt`：三條都需要一個有敵意的 HTTP 對端，而本專案
# 的 urllib3 只跟 localhost 的 chromedriver 講話。
#
# 那個理由是一句**關於現在的程式碼**的話，不是關於 urllib3 的話——live stack 只要
# 多一行 `import requests`，或任何直接用 urllib3 連外的程式碼，三條就從「搆不到」
# 變成「搆得到」，而註解會繼續寫著搆不到、下限會繼續停在 2.7.0、整套測試會繼續全綠。
# 這跟 simpleeval 那個守門是同一個形狀：安全性來自「我們沒有做某件事」，所以要守的
# 是那件事沒有被做。

# `test/` 照同一個判準過濾（手動 e2e 腳本搬家前住在套件裡、在範圍內，照舊）。
_LIVE_STACK_FILES = sorted(
    p for p in (list((Path(__file__).resolve().parent.parent / "axiomatic").glob("*.py"))
                + list(Path(__file__).resolve().parent.glob("*.py"))
                + list(Path(__file__).resolve().parent.parent.glob("*.py")))
    if not p.name.startswith("test_") and p.name != "conftest.py")


def _urllib3_exposure(source: str, filename: str) -> list[str]:
    """這支模組有沒有讓 urllib3 面對 localhost chromedriver 以外的對端。

    純函式，好餵合成語料。放行的只有一種寫法：`from urllib3.exceptions import …`
    ——那是**接例外**，不會發出任何請求。其餘（`import urllib3`、
    `from urllib3 import PoolManager`、任何 `requests`）都要人看過再決定。
    """
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root == "requests":
                    problems.append(f"{filename}:{node.lineno}: import {alias.name}")
                elif root == "urllib3" and alias.name != "urllib3.exceptions":
                    problems.append(f"{filename}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            if root == "requests" or (root == "urllib3"
                                      and node.module != "urllib3.exceptions"):
                problems.append(
                    f"{filename}:{node.lineno}: from {node.module} import …")
    return problems


def test_the_live_stack_keeps_urllib3_pointed_at_localhost():
    """守住 urllib3 三條公告標成 `（搆不到）` 所依據的那個前提。"""
    exposures: list[str] = []
    saw_urllib3_exceptions = 0
    for path in _LIVE_STACK_FILES:
        source = path.read_text(encoding="utf-8")
        if "from urllib3.exceptions import" in source:
            saw_urllib3_exceptions += 1
        exposures += _urllib3_exposure(source, path.name)

    # 正對照，兩側都要：掃不到檔案跟「全部乾淨」長得一模一樣。
    assert len(_LIVE_STACK_FILES) >= 25, (
        f"只掃到 {len(_LIVE_STACK_FILES)} 支非測試模組，檔案選取壞了——"
        "這種情況下面那句斷言會空轉通過。")
    assert saw_urllib3_exceptions >= 2, (
        f"只有 {saw_urllib3_exceptions} 支模組接 urllib3 的例外；實測是三支"
        "（兩個 webrunner 變體＋額度對話框驗證）。抽取或檔案清單壞了。")

    assert not exposures, (
        f"live stack 出現了會讓 urllib3 連到外部主機的程式碼：{exposures}。"
        "`requirements.txt` 把 GHSA-vxq7-64xx-v4gw / GHSA-gh4c-6fx4-qh6g / "
        "GHSA-8988-9cw3-xx77 標成「搆不到」，依據就是「urllib3 只跟 localhost 的 "
        "chromedriver 講話」——這一行讓那個依據不成立了。"
        "要嘛改回只接例外，要嘛把下限抬到 `urllib3>=2.8.0` 並改寫那段註解。")


@pytest.mark.parametrize("snippet, expect_flagged", [
    # 唯一放行的寫法：接例外，不發請求
    ("from urllib3.exceptions import ReadTimeoutError", False),
    ("from urllib3.exceptions import MaxRetryError, ReadTimeoutError", False),
    ("import urllib3.exceptions", False),
    # 會讓 urllib3 面對外部對端的
    ("import urllib3", True),
    ("from urllib3 import PoolManager", True),
    ("from urllib3.poolmanager import PoolManager", True),
    ("import requests", True),
    ("from requests import post, get", True),
    ("import requests.adapters", True),
    # must-allow 的近似案例：開頭像但不是同一個套件。少了這兩筆，「把 root 比對
    # 換成 startswith」那個變異殺不掉——真實樹上兩種寫法都不存在，照樣全綠。
    ("import requests_cache", False),
    ("from urllib3_mock import Responses", False),
    # 相對匯入是本專案自己的模組，不是那兩個套件。**名字必須剛好撞到**才驗得到
    # `node.level == 0` 那道條件：寫成 `from .exceptions import …` 的話，模組名是
    # `exceptions`，兩個比對都不會命中，於是「把 level 檢查拿掉」的變異照樣存活
    # ——實測過，先寫成那樣時 SURVIVED。
    ("from .requests import build_headers", False),
    ("from .urllib3 import shim", False),
    ("from .exceptions import ReadTimeoutError", False),
])
def test_the_urllib3_exposure_scan_actually_bites(snippet, expect_flagged):
    """正對照：真實樹是乾淨的，所以這個判斷式在真實資料上驗不動。

    樹上沒有任何違規時，`_urllib3_exposure` 裡那幾行 `problems.append` 一次都不會
    執行，整支刪掉照樣綠。
    """
    assert bool(_urllib3_exposure(snippet, "synthetic.py")) is expect_flagged
