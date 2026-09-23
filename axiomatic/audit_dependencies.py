"""查「本專案的相依樹裡有沒有已知漏洞」，並且只回報**搆得到的**那些。

    py -3 axiomatic/audit_dependencies.py
    .venv/Scripts/python.exe axiomatic/audit_dependencies.py

為什麼需要這支：`test_dependency_floors.py` 守的是**已經寫進 `requirements.txt`
的下限**——它比對「裝著的版本」與「宣告的下限」，所以能擋往下漂，但**沒辦法發現
一條新的漏洞公告**。發現那一半一直是手動的，而 `requirements.txt` 自己記著那次
教訓：兩個直譯器的 `aiohttp` 都停在 3.13.5、落後 14 個已知漏洞，整整幾週沒有任何
訊號，因為 `pip list --outdated` 要有人想到去跑才會看到。

而手動跑 `pip-audit` 有個實際的障礙：**這台機器的 `py -3` 是通用直譯器**，裝著
一堆跟本專案無關的東西。2026-09-06 實測，整份 audit 報 25 個有漏洞的套件，
其中**零個**在本專案的相依樹裡——torch、starlette、tornado、jupyter、langchain
之類。那種輸出的下場只有兩種，都很糟：被嚇到而去升級不相干的東西，或者整份當雜訊
忽略掉——而下一次真的有一條打中本專案時，它會躺在同一堆雜訊裡。

所以這支做的事就是把範圍收斂到 `requirements.txt` 宣告的直接相依**加上它們的傳遞
閉包**，其餘一律列在「已略過」那一段（列出來而不是消失，才看得出過濾條件對不對）。

跑在哪個直譯器上就查哪一份相依。這是刻意的：這台機器上有**三份**不同的相依組合
（測試用的 `py -3`、正式跑 bot 的 `.venv`、fresh clone 會拿到的最新版），前兩份都
要查。

第三份（fresh clone）走 `--fresh`：

    py -3 axiomatic/audit_dependencies.py --fresh
    .venv/Scripts/python.exe axiomatic/audit_dependencies.py --fresh

它比對「這個直譯器裝著的直接相依」與「套件庫上的最新版」。因為
`requirements.txt` 全檔不釘版本，右邊那一欄**就是** fresh clone 會拿到的東西，
而整套測試跑的是兩份安裝當下就凍住的環境——差距只要沒人量，就一直只是推論。
2026-09-09 首次量：15 個直接相依裡只有 `anthropic` 落後（1.3.0 → 1.4.0，兩個
直譯器都是），升上去之後只剩可編輯安裝的那一個，而它本來就不該拿去跟套件庫比。

`--fresh` **只看直接相依**，這是刻意的。傳遞相依的版本是 pip 從直接相依各自宣告的
範圍解出來的，fresh clone 會自己解一次；本專案對它們唯一該問的問題是「有沒有已知
漏洞」，而那正是預設模式在答的。把 207 筆落後全報出來只會把唯一有意義的那一筆
埋掉——跟這支腳本一開始要收斂 `pip-audit` 範圍的理由完全相同。

結束碼：0 ＝ 相依樹內沒有已知漏洞；1 ＝ 有（詳情印在上面）；2 ＝ 查不成
（`pip-audit` 沒裝、沒有網路、輸出解析不了）。**2 不是「安全」**——它是「這次什麼
都沒查到」，跟 1 要分開看，別把它當成綠燈。

`pip-audit` 本身**不列進 `requirements.txt`**：那份檔案宣告的是「直接 import 得到
的東西」，而這支是用子行程呼叫它的稽核工具，不是執行期相依。沒裝的話：
`py -3 -m pip install pip-audit`。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 — 固定命令，引數不含使用者輸入
import sys
import tomllib
import urllib.request
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"

# `pip-audit` 要連 PyPI 的公告資料庫，慢的時候幾十秒都有可能。
_AUDIT_TIMEOUT_SEC = 900
# `pip list --outdated` 每個套件都要問一次套件庫；這台機器的 `py -3` 裝了幾百個，
# 實測要一分多鐘。上限存在的理由跟上面那條一樣：每一個等待都要有上限。
_OUTDATED_TIMEOUT_SEC = 600


def _normalise(name: str) -> str:
    """PyPI 的名稱比對規則：大小寫不分，`_`、`-`、`.` 等價。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def declared_requirements(path: Path = REQUIREMENTS) -> set[str]:
    """`requirements.txt` 裡宣告的直接相依名稱（去掉版本指定與註解）。"""
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(_normalise(re.split(r"[<>=!~\[;]", line)[0]))
    return names


def dependency_closure(roots: set[str]) -> set[str]:
    """從直接相依往下走完整棵樹，回傳所有套件名稱（含 roots 自己）。

    查不到的套件（沒裝、或名稱對不上）就停在那裡不往下走——寧可少報，也不要因為
    一個查不到的名字就整支掛掉。extras 專屬的相依會跳過：本專案沒裝任何 extras，
    把它們算進來只會虛增範圍、讓過濾失去意義。
    """
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        name = _normalise(todo.pop())
        if name in seen:
            continue
        seen.add(name)
        try:
            dist = metadata.distribution(name)
        except Exception:  # pylint: disable=broad-except
            continue      # 沒裝／名稱對不上——不往下走，也不算失敗
        for raw in (dist.requires or []):
            dep = _normalise(re.split(r"[<>=!~\[;( ]", raw)[0])
            if dep and dep not in seen and _requirement_applies(raw, dep):
                todo.append(dep)
    return seen


def _requirement_applies(raw: str, dep: str) -> bool:
    """這條相依在**這個**環境成不成立（PEP 508 的環境標記）。

    原本只用字串比對擋掉 `extra ==`，其餘標記一律當成立，於是閉包裡會混進這台機器
    永遠不會有的東西。實例（2026-09-17）：`httpx2` 宣告
    `httpx2-jsfetch; sys_platform == 'emscripten'`——那是 Pyodide 專用，Windows 上不
    可能裝，但它一路被算進閉包，然後在倉庫層級掃描裡變成一個**永遠**解析不出倉庫的
    項目。而「有任何一個沒查到就回 2」的規則會因此讓結束碼**永遠回不到 0**，
    一個永遠非零的結束碼跟永遠是零一樣沒有資訊。

    **標記不成立、但套件其實裝著時仍然往下走**：少掃一個真的存在的套件是漏報，
    方向不對。所以這一步只會剔掉「不成立**而且**沒裝」的。
    """
    try:
        marker = Requirement(raw).marker
    except Exception:  # pylint: disable=broad-except
        return True    # 剖不開就當成立——寧可多掃
    if marker is None:
        return True
    # extras 專屬的相依**一律不算**，而且刻意**不套**下面那個「沒成立但裝著就保留」
    # 的保險。理由是量出來的：`py -3` 是這台機器的通用直譯器，幾百個不相干的套件
    # 剛好裝著，套上保險會讓閉包從 69 暴增到 **259**，掃出 89 筆「命中」而其中絕大
    # 多數落在 gitpython／mistune／pypdf／ipython／pyside6 這些本專案一行都沒用到的
    # 套件上。那正是這類稽核最典型的失效方式：一份全是雜訊的
    # 報告，下場不是被嚇到去升不相干的東西，就是整份被忽略。
    if re.search(r"\bextra\b", str(marker)):
        return False
    try:
        # 明確給空的 `extra`：不指定時各版本 `packaging` 的行為不一致（有的回 False、
        # 有的丟 `UndefinedEnvironmentName`），而丟例外會走到下面的 `return True`，
        # 把 extras 專屬相依整批放進來——那正是原本那行字串比對要擋的東西。
        if marker.evaluate({"extra": ""}):
            return True
    except Exception:  # pylint: disable=broad-except
        return True
    try:
        metadata.distribution(dep)
        return True
    except Exception:  # pylint: disable=broad-except
        return False


# ---------------------------------------------------------------------------
# 可編輯安裝：中繼資料上的版本 **不是** 跑起來的那一份
#
# `pip install -e` 只在安裝當下寫一次 dist-info，之後原始碼樹再怎麼改版號都不會
# 回寫。所以 `pip list` 印的、`importlib.metadata` 回的，都可能落後好幾十版——
# 本機的 `je_auto_control` 中繼資料寫 0.0.195，實際 import 到的原始碼樹宣告 0.0.221。
# 判斷可編輯安裝的**標準**做法是 PEP 610 的 `direct_url.json`
# （`dir_info.editable is True`）；`__editable__*.pth` 那種檔名是 setuptools 的實作
# 細節，換個建置後端就沒了。
#
# 這三支住在這裡（而不是測試檔裡）是刻意的：`test_dependency_floors.py` 從這裡
# import，所以只有一份實作。方向也只能是這樣——工具不該去 import 測試模組。
# ---------------------------------------------------------------------------

def editable_source_dir(direct_url_text: str | None) -> Path | None:
    """`direct_url.json` 的內容 → editable 安裝的原始碼樹路徑（否則 None）。

    純函式，吃字串不吃檔案，這樣合成對照組可以直接餵它。
    """
    if not direct_url_text:
        return None
    try:
        info = json.loads(direct_url_text)
    except Exception:  # pylint: disable=broad-except
        return None
    if not isinstance(info, dict):
        return None
    if not (info.get("dir_info") or {}).get("editable"):
        return None
    url = info.get("url")
    if not isinstance(url, str) or not url.startswith("file:"):
        return None
    # `file:///<AutoControlGUI 的本機 checkout>` → `<AutoControlGUI 的本機 checkout>`。
    # `url2pathname` 在 Windows 上對這個形狀是對的，但它的行為跨平台不一致，
    # 而這份中繼資料只會由本機的 pip 寫；自己剝比較好預測。
    path = unquote(urlparse(url).path)
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return Path(path) if path else None


def source_tree_version(source: Path | None) -> str | None:
    """原始碼樹自己宣告的版本（`pyproject.toml` 的 `[project] version`）。

    讀不到就回 None，呼叫端會退回中繼資料上的版本——**寧可退回一個舊數字，也不要
    讓整個相依守門因為一棵樹的 pyproject 長得不一樣就掛掉**。動態版本
    （`[project] dynamic = ["version"]`）也走這條路。
    """
    if source is None:
        return None
    try:
        data = tomllib.loads(
            (source / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception:  # pylint: disable=broad-except
        return None
    version = (data.get("project") or {}).get("version")
    return version if isinstance(version, str) and version else None


def effective_version(declared: str, direct_url_text: str | None) -> str:
    """這個套件**跑起來實際是哪一版**：editable 就問原始碼樹，否則就是中繼資料。"""
    return (source_tree_version(editable_source_dir(direct_url_text))
            or declared)


def installed_source_version(name: str) -> str | None:
    """裝著的套件若是可編輯安裝，回傳它**原始碼樹**宣告的版本，否則 None。"""
    try:
        dist = metadata.distribution(name)
        direct_url = dist.read_text("direct_url.json")
    except Exception:  # pylint: disable=broad-except
        return None
    return source_tree_version(editable_source_dir(direct_url))


# ---------------------------------------------------------------------------
# 「fresh clone 會拿到什麼」——本機測不到的那第三份相依組合
# ---------------------------------------------------------------------------

def run_pip_outdated() -> list | None:
    """跑 `pip list --outdated --format=json`；查不成回 None。"""
    try:
        done = subprocess.run(  # nosec B603 — 固定命令、無 shell、引數皆為常數
            [sys.executable, "-m", "pip", "list", "--outdated",
             "--format=json", "--disable-pip-version-check"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_OUTDATED_TIMEOUT_SEC, check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except FileNotFoundError:
        print("查不成：找不到直譯器本身", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print(f"查不成：pip 超過 {_OUTDATED_TIMEOUT_SEC}s 沒回應"
              "（通常是連不上套件庫）", file=sys.stderr)
        return None
    # 形狀不對一律當「查不成」，不是「沒有落後」——理由同 `run_pip_audit`：
    # 一份解析得開但不是清單的輸出，會安靜地變成一份完美的綠燈報告。
    try:
        parsed = json.loads(done.stdout)
    except Exception:  # pylint: disable=broad-except
        print("查不成：pip 的輸出解析不了。", file=sys.stderr)
        print(f"  stdout: {done.stdout[:300]!r}", file=sys.stderr)
        print(f"  stderr: {done.stderr[-300:]!r}", file=sys.stderr)
        return None
    if not isinstance(parsed, list):
        print("查不成：pip 的輸出解析得開，但不是預期的清單（格式可能變了）。",
              file=sys.stderr)
        print(f"  stdout: {done.stdout[:300]!r}", file=sys.stderr)
        return None
    return parsed


def fresh_clone_drift(roots: set[str], outdated_rows: list,
                      source_version=None):
    """落後套件庫最新版的**直接**相依 → `(一般安裝, 可編輯安裝)` 兩份清單。

    **名稱一定要正規化再比。** `requirements.txt` 寫 `je-auto-control`，pip 印的是
    `je_auto_control`；用字面 grep 去比，這一筆會安靜地消失——2026-09-09 的人工普查
    就是這樣漏掉它的，而漏掉的正是唯一一筆。過濾條件本身就是這支存在的理由，所以
    它走 `_normalise`，不走字串相等。

    可編輯安裝**分開回報**：pip 印的 `version` 是安裝當下凍住的中繼資料，跟實際
    import 到的原始碼樹可能差很多，拿它去跟套件庫比較說明不了任何事。

    `source_version` 的預設值**在呼叫時**才解析。原本寫成
    `source_version=installed_source_version`，那個預設值在 def 當下就綁死了原本的函式
    物件，於是 `monkeypatch.setattr(ad, "installed_source_version", …)` 對經由
    `main(["--fresh"])` 走進來的路徑完全無效——替身從來沒生效，測試在這台機器上會過，
    只因為本機可編輯安裝的原始碼樹剛好就是測試寫死的版本號（2026-09-12 在 fresh clone
    那一份相依組合上抓到）。
    """
    if source_version is None:
        source_version = installed_source_version
    normal, editable = [], []
    for row in outdated_rows:
        if not isinstance(row, dict):
            continue
        name = _normalise(str(row.get("name", "")))
        if name not in roots:
            continue
        installed = str(row.get("version", "?"))
        latest = str(row.get("latest_version", "?"))
        where = row.get("editable_project_location")
        if where:
            editable.append((name, installed, source_version(name), latest,
                             str(where)))
        else:
            normal.append((name, installed, latest))
    return sorted(normal), sorted(editable)


def report_fresh_clone_drift() -> int:
    """`--fresh`：量「這個直譯器裝著的直接相依」與「套件庫最新版」的差距。

    為什麼要有這一支：`requirements.txt` 全檔不釘版本，所以 **fresh clone 拿到的是
    當下最新版**。但整套測試跑的是兩份**安裝當下就凍住**的環境（`py -3` 與
    `.venv`），fresh clone 解析出來的是**第三份**，沒有任何東西在驗它——「fresh
    clone 拿到的版本」因此一直是推論而不是量測。這支不會、也不該變成測試：它要連
    網路，而一支會因為對方伺服器不通就變紅的測試，最後一定被關掉。

    結束碼：0 ＝ 沒有落後；1 ＝ 有；2 ＝ 查不成（**不是「沒有落後」**）。
    """
    roots = declared_requirements()
    rows = run_pip_outdated()
    if rows is None:
        print("\n結果：**沒有查到任何東西**（不是「沒有落後」）。", file=sys.stderr)
        return 2

    normal, editable = fresh_clone_drift(roots, rows)
    print(f"直譯器：{sys.executable}")
    print(f"直接相依 {len(roots)} 個；這個直譯器共有 {len(rows)} 個套件落後，"
          f"其中屬於本專案直接相依的 {len(normal) + len(editable)} 個")

    print()
    print(f"=== 落後套件庫最新版的直接相依：{len(normal)} ===")
    for name, installed, latest in normal:
        print(f"  {name} {installed} → {latest}")
    if normal:
        print()
        print("fresh clone 現在拿到的是右邊那一欄，而本機兩個直譯器都沒在跑它。"
              "升上去之前先看該套件的變更紀錄有沒有破壞性改版；升完之後把**量到的**"
              "結果寫回 `requirements.txt` 對應那一段，不要抄上一次的結論。")

    if editable:
        print()
        print(f"=== 可編輯安裝（另計，{len(editable)} 個）===")
        for name, meta_version, tree_version, latest, where in editable:
            print(f"  {name}：中繼資料 {meta_version}、原始碼樹 "
                  f"{tree_version or '（讀不到）'}、套件庫 {latest}")
            print(f"           {where}")
        print("  中繼資料的版本是安裝當下凍住的，**不是** import 到的那一份；"
              "跟套件庫比較說明不了任何事。要改就改原始碼樹。")

    return 1 if normal else 0


def run_pip_audit() -> list | None:
    """跑 `pip-audit` 並回傳它的 dependencies 清單；查不成回 None。"""
    try:
        done = subprocess.run(  # nosec B603 — 固定命令、無 shell、引數皆為常數
            [sys.executable, "-m", "pip_audit", "-f", "json",
             "--progress-spinner", "off"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_AUDIT_TIMEOUT_SEC, check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except FileNotFoundError:
        print("查不成：找不到直譯器本身", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print(f"查不成：pip-audit 超過 {_AUDIT_TIMEOUT_SEC}s 沒回應"
              "（通常是連不上公告資料庫）", file=sys.stderr)
        return None
    # pip-audit 找到漏洞時結束碼是 1，那是正常結果不是錯誤，所以只看 stdout
    # 解不解得開。真正的失敗（沒裝、沒網路）會讓 stdout 不是 JSON。
    try:
        parsed = json.loads(done.stdout)
        # **形狀不對要當成「查不成」，不是「零漏洞」。** 原本寫
        # `json.loads(...).get("dependencies", [])`：一份解析得開、但沒有
        # `dependencies` 的 JSON 會回 `[]` ——一份完全合法的「零漏洞」報告，
        # 結束碼 0，而且**不會走到任何 except**。pip-audit 換了輸出格式（或哪天
        # 改成回一個錯誤物件）就會靜默變成「永遠安全」。這正是本專案在
        # `_external_apis` 上被第三方漂移咬過兩次的形狀，而這支的整個用途就是
        # 不要靜默地說安全。
        if not isinstance(parsed, dict) or not isinstance(
                parsed.get("dependencies"), list):
            print("查不成：pip-audit 的輸出解析得開，但沒有預期的 "
                  "`dependencies` 清單（格式可能變了）。", file=sys.stderr)
            print(f"  stdout: {done.stdout[:300]!r}", file=sys.stderr)
            return None
        return parsed["dependencies"]
    except Exception:  # pylint: disable=broad-except
        if "No module named" in done.stderr:
            print("查不成：這個直譯器沒裝 pip-audit。"
                  f"請跑 `{sys.executable} -m pip install pip-audit`",
                  file=sys.stderr)
        else:
            print("查不成：pip-audit 的輸出解析不了。", file=sys.stderr)
            print(f"  stdout: {done.stdout[:300]!r}", file=sys.stderr)
            print(f"  stderr: {done.stderr[-300:]!r}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# 倉庫層級的公告：全域資料庫還沒收進去的那一類
#
# 上面那套（pip-audit / OSV）答的是「**公告資料庫今天知道什麼**」。專案自己在 GitHub
# 上發布公告之後，要過一段時間才會被收進全域資料庫，而那段時間裡每一支掃描器都會
# 說乾淨。2026-09-17 實測兩次，間隔五天、不同套件，形狀一模一樣：
#
#   simpleeval 1.0.7  三條（發布 5 天後）  pip-audit 0、OSV 0、全域 GET /advisories 404
#   urllib3    2.7.0  三條（發布 2 天後）  pip-audit 0、OSV 0、全域 GET /advisories 404
#                     其中兩條 high，全部修在 2.8.0
#
# `GET /repos/{owner}/{repo}/security-advisories` 直接問專案自己的倉庫，看得到這些。
# 未認證也能查（限速每小時 60 次，15 個直接相依綽綽有餘），整輪實測 15 秒。

_GITHUB_TIMEOUT_SEC = 30
_HTTP_UA = "axiomatic-dependency-audit"

# `X.Y.Z - A.B.C` 這種破折號區間，以及光禿禿一個版本號。兩種都是公告作者手寫的，
# `SpecifierSet` 不認得——見 `normalise_version_range` 的說明。
_DASH_RANGE_RE = re.compile(
    r"([0-9][0-9A-Za-z.!+*]*)\s*-\s*([0-9][0-9A-Za-z.!+*]*)")
_BARE_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.!+]*")


def normalise_version_range(raw: str) -> str | None:
    """把公告的 `vulnerable_version_range` 整理成 `SpecifierSet` 吃得下的樣子。

    **這個欄位是公告作者手寫的自由文字，不是機器欄位。** 實測 78 個 pip 生態系的
    區間裡有 9 個（12%）直接餵 `SpecifierSet` 會丟例外，而且全部集中在同一個套件
    （Pillow）：`≤ 12.2.0`（Unicode 的 ≤）、`5.2.0 - 12.2.0`（破折號區間）、
    `11.2.0`（光禿禿一個版本號，意思是「就是這一版」）。

    ⚠️ **整不出來時回 None，呼叫端必須把它印出來，不可以安靜跳過。** 一個
    `try/except: continue` 會讓 Pillow 20 條公告裡的 9 條無聲消失，而輸出看起來
    跟「查過、很乾淨」一模一樣——這正是這支工具存在的理由的反面。
    """
    text = raw.strip().replace("≤", "<=").replace("≥", ">=")
    text = re.sub(r"\s+", " ", text)
    # ⚠️ 空字串一定要在這裡擋掉。`SpecifierSet("")` 是**合法**的，而且它**符合任何
    # 版本**——所以一條沒有填區間的公告會變成「命中每一個版本」。發現的方式是先寫
    # 了 `("", None)` 這個案例然後看它紅掉；沒有那個案例的話，這條路只會在某天遇到
    # 一條欄位空著的公告時，安靜地多報一筆。
    if not text:
        return None
    dash = _DASH_RANGE_RE.fullmatch(text)
    single = re.fullmatch(r"=\s*([0-9][0-9A-Za-z.!+]*)", text)
    if dash:
        text = f">={dash.group(1)},<={dash.group(2)}"
    elif single:
        # `= 4.14.0`（單一個等號）是作者手寫的「就是這一版」，PEP 440 要兩個等號。
        text = f"=={single.group(1)}"
    elif _BARE_VERSION_RE.fullmatch(text):
        text = f"=={text}"
    try:
        SpecifierSet(text)
    except Exception:  # pylint: disable=broad-except
        return None
    return text


def normalise_patched_versions(raw: str) -> str | None:
    """`patched_versions` -> 「安全」的條件式；整不出來回 None。

    ⚠️ **這裡的光禿禿版本號跟 `vulnerable_version_range` 的意思相反**，是整段最容易
    寫反的地方，而寫反會**靜默地翻轉結論**：

        vulnerable_version_range 的 `11.2.0` = 「就是這一版有問題」 -> `==11.2.0`
        patched_versions        的 `1.0.8`  = 「這一版之後就安全」 -> `>=1.0.8`

    兩個欄位共用一支正規化函式的話，其中一邊一定是錯的，所以刻意分成兩支。
    """
    text = re.sub(r"\s+", " ", (raw or "").strip()
                  .replace("≤", "<=").replace("≥", ">="))
    if not text:
        return None
    # 多分支修正版本：`v1.8.2,v1.7.4,v1.6.2` 是**三個並列的**修正版（各分支一個），
    # 不是 `SpecifierSet` 那種「而且」。取最大的當門檻是刻意保守的：門檻訂高只會讓
    # 判斷退回去看區間，不會把受影響的說成沒事。（真實案例：pydantic 那條 2021 年的
    # 公告，區間寫 `all`，裝著的 2.12.5 遠高於所有 1.x 修正版。）
    parts = [p.strip().lstrip("vV") for p in text.split(",") if p.strip()]
    if len(parts) > 1 and all(_BARE_VERSION_RE.fullmatch(p) for p in parts):
        try:
            return f">={max(Version(p) for p in parts)}"
        except Exception:  # pylint: disable=broad-except
            return None
    if len(parts) == 1 and _BARE_VERSION_RE.fullmatch(parts[0]):
        text = f">={parts[0]}"
    try:
        SpecifierSet(text)
    except Exception:  # pylint: disable=broad-except
        return None
    return text


def is_affected(version: str, raw_range: str, raw_patched: str = "") -> bool | None:
    """裝著的版本受不受這條公告影響。答不出來回 None（＝不知道，不是沒事）。

    **先問「已經修好了嗎」，再問「落在區間裡嗎」**，順序不能反——因為很多公告的
    `vulnerable_version_range` 是**開口**的（`>= 44.0.0`，沒有上界），真正的界線只寫
    在 `patched_versions`。2026-09-17 實測：只看區間的話，`cryptography` 50.0.1 會
    被判成落在 `>= 44.0.0` 裡＝受影響，但它的 patched 是 50.0.0，早就修好了。
    整個相依閉包 12 筆「命中」裡有 **10 筆是這種假陽性**；補上這一步之後 15 筆降到
    5 筆，真陽性一筆沒少，而且還把一筆「不知道」（`h11` 的區間寫成
    `0.15.0 and earlier`，剖不開）確定成「沒事」。

    會叫的守門才有人看——這一步是為了不要變成一支喊狼來了的工具。
    """
    try:
        parsed = Version(version)
    except Exception:  # pylint: disable=broad-except
        return None
    if parsed.is_prerelease:
        return None
    patched = normalise_patched_versions(raw_patched)
    if patched is not None and parsed in SpecifierSet(patched):
        return False
    return version_is_in_range(version, raw_range)


def version_is_in_range(version: str, raw_range: str) -> bool | None:
    """裝著的版本落不落在公告的區間內。答不出來時回 None（＝不知道，不是沒事）。

    **預先發行版一律回 None，這是刻意的。** PEP 440 規定排他性的 `<V`
    **不得**納入 V 自己的預先發行版，所以 `1.0.8a1` 對著 `<1.0.8` 會算出「不受
    影響」——而 1.0.8a1 明明在修正之前，那是個**假陰性**，正好是這支工具最不該犯的
    方向。

    ⚠️ 這裡原本寫 `SpecifierSet(spec, prereleases=True)`，看起來像是已經處理過這件
    事。實測 8 組（含所有真實出現過的區間形狀）**有差別的是 0 組**：那個參數在這個
    用法下是個不做事的裝飾，而一個「看起來考慮過」的空動作比沒寫還糟，因為下一個
    人會相信它。真正管用的是上面那個早退，而它有自己的測試。
    """
    spec = normalise_version_range(raw_range)
    if spec is None:
        return None
    try:
        parsed = Version(version)
    except Exception:  # pylint: disable=broad-except
        return None
    if parsed.is_prerelease:
        return None
    try:
        return parsed in SpecifierSet(spec)
    except Exception:  # pylint: disable=broad-except
        return None


def github_token() -> str | None:
    """拿一個 GitHub token 來提高限速；拿不到回 None（改走未認證）。

    **為什麼一定要有**：未認證是每小時 60 次，而一輪 16 個直接相依要 32 次
    （PyPI 一次、GitHub 一次），跑兩輪就滿了。2026-09-17 第一次實跑就被砍掉 6 個
    倉庫，其中正是 urllib3——也就是這支工具唯一一次有東西可報的那個套件。
    認證之後是每小時 5000 次。

    ⚠️ **拿到的值不准印出來、不准寫進 log。** 回傳值只往 `Authorization` 標頭送。
    """
    for variable in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(variable)
        if value:
            return value.strip()
    try:
        done = subprocess.run(  # nosec B603 B607 — 固定命令、無 shell、無使用者輸入
            ["gh", "auth", "token"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, check=False)
    except Exception:  # pylint: disable=broad-except
        return None
    token = done.stdout.strip()
    return token or None


def _get_json(url: str, token: str | None = None) -> object | None:
    """拿 JSON；任何失敗一律回 None，由呼叫端當成「查不成」而不是「沒有」。"""
    headers = {"User-Agent": _HTTP_UA, "Accept": "application/json"}
    if token and url.startswith("https://api.github.com/"):
        # 只往 GitHub 送。PyPI 那幾個請求不需要、也不該帶著這個標頭。
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(  # nosec B310 — 常數 https 網址
                request, timeout=_GITHUB_TIMEOUT_SEC) as response:
            return json.load(response)
    except Exception:  # pylint: disable=broad-except
        return None


# 少數套件的 PyPI metadata 裡沒有任何原始碼位址，只能在這裡補。
#
# 每一筆都要寫**為什麼**，而且補完要確認倉庫真的在——一個打錯字或改過名的倉庫會
# 讓查詢失敗，而失敗被歸進「沒有查到」，看起來跟「查過沒事」差很多但很容易被跳過。
_REPO_OVERRIDES = {
    # `project_urls` 只有 `{"Homepage": "https://www.selenium.dev"}`，沒有倉庫位址。
    # 2026-09-17 確認 `SeleniumHQ/selenium` 存在、未封存、目前 0 筆倉庫層級公告。
    "selenium": "SeleniumHQ/selenium",
    # 同樣只有 `{"Homepage": "http://www.grantjenks.com/docs/sortedcontainers/"}`。
    # 2026-09-17 確認 `grantjenks/python-sortedcontainers` 存在、未封存。
    "sortedcontainers": "grantjenks/python-sortedcontainers",
}


# `github.com/<這些>/…` 長得跟倉庫一模一樣，但**不是**倉庫。
#
# 這不是理論上的顧慮：`https://github.com/sponsors/<人>`（贊助連結）在 `project_urls`
# 裡很常見，而且常常排在真正的倉庫位址前面。2026-09-17 實測，照 dict 順序挑第一個
# 含 `github.com/` 的網址，會讓 **attrs／pydantic／pydantic-core／audioop-lts** 四個
# 都解析成 `sponsors/<人>`——而每一個的**正確倉庫就在同一張表裡**。症狀是查詢失敗
# （至少誠實地被歸進「沒有查到」），但那四個套件實際上從來沒被掃過，其中包含 pydantic。
_NON_REPO_NAMESPACES = frozenset({
    "sponsors", "orgs", "users", "apps", "marketplace", "features",
    "about", "pricing", "security", "enterprise", "collections", "topics",
})

# 挑鍵的優先序：明講是原始碼的先挑，避免再被別的網址插隊。
_SOURCE_URL_KEYS = ("source", "repository", "repo", "source code", "code",
                    "github", "homepage")


def repo_from_project_urls(urls: dict) -> str | None:
    """從一堆 `project_urls` 挑出 `owner/repo`；挑不出回 None。純函式，好餵合成資料。"""
    def _repo(value: object) -> str | None:
        if not isinstance(value, str) or "github.com/" not in value:
            return None
        tail = value.split("github.com/", 1)[1].strip("/")
        parts = [p for p in tail.split("/") if p]
        if len(parts) < 2 or parts[0].lower() in _NON_REPO_NAMESPACES:
            return None
        return f"{parts[0]}/{parts[1].removesuffix('.git')}"

    for wanted in _SOURCE_URL_KEYS:
        for key, value in urls.items():
            if str(key).lower() == wanted:
                if repo := _repo(value):
                    return repo
    for value in urls.values():
        if repo := _repo(value):
            return repo
    return None


def source_repo(name: str, version: str | None) -> str | None:
    """從 PyPI metadata 的 `project_urls` 解析出 `owner/repo`；解不出回 None。

    ⚠️ **一定要打帶版本號的那個端點。** `pypi.org/pypi/{name}/json` 會把該套件
    **每一個版本的每一個檔案**都列出來，aiohttp 那種大的實測 120 秒拉不完。
    """
    override = _REPO_OVERRIDES.get(_normalise(name))
    if override:
        return override
    url = (f"https://pypi.org/pypi/{name}/{version}/json" if version
           else f"https://pypi.org/pypi/{name}/json")
    data = _get_json(url)
    if not isinstance(data, dict) or not isinstance(data.get("info"), dict):
        return None
    info = data["info"]
    candidates = dict(info.get("project_urls") or {})
    if info.get("home_page"):
        candidates.setdefault("__home_page__", info["home_page"])
    return repo_from_project_urls(candidates)


def repository_advisories(repo: str, token: str | None = None) -> list | None:
    """倉庫層級的**已發布**公告；查不成回 None（不是空 list）。"""
    payload = _get_json(
        f"https://api.github.com/repos/{repo}/security-advisories", token)
    if not isinstance(payload, list):
        return None
    return [a for a in payload
            if isinstance(a, dict) and a.get("state") == "published"]


def report_repository_advisories() -> int:
    """`--repo-advisories`：查**整個相依閉包**的倉庫層級公告，比對裝著的版本。

    結束碼 0 ＝ 每一個都**得到答案**且沒打到；1 ＝ 有打到；2 ＝ 有任何一個沒有得到
    答案（＝這次的結果不是「安全」）。「沒有得到答案」有三種：解析不出倉庫、倉庫查
    詢失敗，以及**拿到公告但判不出受不受影響**。第三種在 2026-09-20 之前不算，而它
    佔實測區間的 12%。

    **範圍是閉包不是只有直接相依**，跟預設模式一致。一開始只做直接相依，理由是
    網路往返；2026-09-17 把範圍拉開量了一次就推翻了——傳遞相依 `anyio` 上有一條
    **critical**（GHSA-82r6-8w77-94w6，TLS 憑證偽冒，修在 4.14.2），而它 2026-07-07
    就發布了、兩個月後全域資料庫仍然 404。整個閉包一輪約 90 秒，換這個沒得商量。
    """
    token = github_token()
    roots = {_normalise(name) for name in declared_requirements()}
    targets = sorted(dependency_closure(declared_requirements()))
    print(f"直譯器：{sys.executable}")
    print(f"直接相依 {len(roots)} 個、傳遞閉包 {len(targets)} 個；"
          "查的是**倉庫層級**公告（全域資料庫可能還沒收進去的那些）")
    # 只說有沒有，不說是什麼——值本身不准出現在任何輸出或 log 裡。
    print("GitHub 認證：" + ("有（每小時 5000 次）" if token
                            else "沒有（每小時 60 次，一輪一定不夠）"))

    hits, unresolved, unreadable, unparseable = [], [], [], []
    checked = 0
    for name in targets:
        # 兩個版本刻意分開用：**比對**要用實際跑起來的那一版（可編輯安裝時是原始碼
        # 樹宣告的），**問 PyPI** 要用中繼資料那一版——可編輯安裝的版本字串常常是
        # 套件庫上不存在的開發版，拿去打帶版本的端點會 404，於是整個套件會被歸進
        # 「解析不出倉庫」，看起來像查過。
        metadata_version = _installed_version(name)
        installed = installed_source_version(name) or metadata_version
        repo = source_repo(name, metadata_version)
        if repo is None:
            unresolved.append(name)
            continue
        advisories = repository_advisories(repo, token)
        if advisories is None:
            unreadable.append(f"{name}（{repo}）")
            continue
        checked += 1
        for advisory in advisories:
            for vuln in advisory.get("vulnerabilities") or []:
                package = vuln.get("package") or {}
                if package.get("ecosystem") != "pip":
                    continue
                if _normalise(package.get("name", "")) != _normalise(name):
                    continue
                raw_range = vuln.get("vulnerable_version_range") or ""
                raw_patched = vuln.get("patched_versions") or ""
                verdict = (is_affected(installed, raw_range, raw_patched)
                           if installed else None)
                row = (name, installed, advisory.get("ghsa_id"),
                       advisory.get("severity"), raw_range, raw_patched,
                       "直接" if _normalise(name) in roots else "傳遞")
                if verdict is None:
                    unparseable.append(row)
                elif verdict:
                    hits.append(row)

    print()
    print(f"=== 打到目前裝著的版本的公告：{len(hits)} ===")
    # 嚴重度高的排前面——一份把 critical 夾在 low 中間的清單，讀的人會先看到不重要的。
    _RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for name, installed, ghsa, severity, raw_range, patched, where in sorted(
            hits, key=lambda r: (_RANK.get(str(r[3]).lower(), 9), r[0])):
        print(f"  [{where}] {name} {installed}  {ghsa}  {severity}")
        print(f"      影響區間 {raw_range!r}；修正版本 {patched or '（未提供）'}")
    if hits:
        print()
        print("下一步跟 pip-audit 報出來時一樣：先判斷**本專案搆不搆得到**"
              "（哪個 API、哪個呼叫點），搆得到才抬下限。"
              "另外記得這些公告全域資料庫可能還查不到，所以"
              "「掃描乾淨」不能拿來當作已經處理完的證據。")

    # 下面三段都是「這次沒答出來的部分」。不印出來的話，它們看起來會跟「查過、
    # 沒事」一模一樣——而這支工具的整個用途就是不要靜默地說安全。
    if unparseable:
        print()
        # 兩種成因：區間寫法看不懂，或**裝著的是預先發行版**（那時 PEP 440 的比較
        # 會給出假陰性，所以這支拒答）。標題不寫死其中一種，免得讀的人排除錯方向。
        print(f"=== 判不出受不受影響、需要人工判斷：{len(unparseable)} ===")
        for name, installed, ghsa, severity, raw_range, _patched, where in unparseable:
            print(f"  [{where}] {name} {installed}  {ghsa}  {severity}  "
                  f"區間 {raw_range!r}")
    if unresolved:
        print()
        print(f"=== 解析不出原始碼倉庫、**沒有查到**：{len(unresolved)} ===")
        print("  " + ", ".join(unresolved))
        print("  （這幾個不是乾淨，是沒查。PyPI metadata 裡找不到 GitHub 網址。）")
    if unreadable:
        print()
        print(f"=== 倉庫查詢失敗、**沒有查到**：{len(unreadable)} ===")
        print("  " + ", ".join(unreadable))
        print("  （沒網路、被限速、或倉庫不存在。同樣不是乾淨。）")

    if hits:
        return 1
    # ⚠️ **0 只能代表「每一個直接相依都查過了、都沒事」。** 這裡原本寫的是
    # `if checked == 0: return 2`，也就是只有「一個都沒查成」才算查不成——2026-09-17
    # 第一次實跑當場現形：未認證的 GitHub 限速砍掉 16 個裡的 6 個（其中正是
    # urllib3，那兩條 high 就在裡面），工具照樣印出「打到的公告：0」並回 **0**。
    # 沒查到跟沒事只差一個結束碼，而自動化只看結束碼。
    #
    # **`unparseable` 也算沒有答案**（2026-09-20 補）。它跟上面兩種不一樣——資料
    # 拿到了，只是判不出受不受影響——但對**只讀結束碼**的自動化來說是同一件事：
    # 0 的意思是安全，而這裡沒有任何人能說出安全。在此之前它只被印出來，完全不
    # 影響結束碼，所以一份「有 9 筆需要人工判斷」的報告回的是 0。
    #
    # 而它不是邊角案例：2026-09-17 實測本專案 14 個倉庫的 78 個 pip 區間，**9 個
    # （12%）** 直接餵 `SpecifierSet` 會丟例外，也就是「印了一段需要人工判斷、然後
    # 回 0」在那天就是實際發生的事。預設模式（`pip-audit`）早就是這樣判的
    # （`test_unparseable_audit_output_never_reads_as_clean`），這條路一直沒對齊。
    #
    # 數的是**閉包**不是直接相依——範圍 2026-09-17 就拉開了，這句文案漏改。
    missed = len(unresolved) + len(unreadable) + len(unparseable)
    if missed:
        print(f"\n結果：{missed} 個套件**沒有得到答案**（限速、沒網路、解析不出"
              "倉庫，或區間判不出受不受影響），所以這次的結果不是「安全」。",
              file=sys.stderr)
        return 2
    return 0


def _installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except Exception:  # pylint: disable=broad-except
        return None


def main(argv: list[str] | None = None) -> int:
    # `argv=None` 一律當成「沒有旗標」，**不是**「去讀 `sys.argv`」。argparse 的
    # 預設行為是後者，而這支會被測試直接 `main()` 呼叫——那時 `sys.argv` 是
    # pytest 自己的命令列，於是 argparse 對著 `-q --timeout=900` 報
    # unrecognized arguments 然後 `SystemExit(2)`。真正的命令列在最下面明寫
    # `sys.argv[1:]` 傳進來。
    parser = argparse.ArgumentParser(
        description="查本專案相依樹的已知漏洞（預設），或量 fresh clone 會拿到"
                    "的版本與本機的差距（--fresh）。")
    parser.add_argument(
        "--fresh", action="store_true",
        help="改成比對「這個直譯器裝著的直接相依」與「套件庫最新版」。"
             "結束碼 0＝沒落後、1＝有、2＝查不成。")
    parser.add_argument(
        "--repo-advisories", action="store_true",
        # 這段說明文字在 2026-09-20 之前是**過期的**，而且過期的正好是兩件在
        # 2026-09-17 就改掉的事：範圍從直接相依拉到整個閉包，結束碼 2 的條件從
        # 「一個都沒查成」放寬成「有任何一個沒有得到答案」。`--help` 是使用者唯一
        # 會讀的規則之書，而它描述的是一個比實際寬鬆的閘——跟 `_OWNER_ONLY_GROUPS`
        # 那份少一個成員的清單是同一個形狀：沒有任何症狀。
        help="改成查整個相依閉包的**倉庫層級**公告（全域公告資料庫可能還沒收進去"
             "的那些）。結束碼 0＝每一個都得到答案且沒打到、1＝有打到、"
             "2＝有任何一個沒有得到答案。")
    args = parser.parse_args(argv or [])
    if args.fresh:
        return report_fresh_clone_drift()
    if args.repo_advisories:
        return report_repository_advisories()

    roots = declared_requirements()
    tree = dependency_closure(roots)
    print(f"直譯器：{sys.executable}")
    print(f"直接相依 {len(roots)} 個，傳遞閉包 {len(tree)} 個")

    dependencies = run_pip_audit()
    if dependencies is None:
        print("\n結果：**沒有查到任何東西**（不是「安全」）。", file=sys.stderr)
        return 2

    relevant, skipped = [], []
    for entry in dependencies:
        vulns = entry.get("vulns") or []
        if not vulns:
            continue
        name = _normalise(entry.get("name", ""))
        row = (name, entry.get("version"),
               [v.get("id") for v in vulns],
               sorted({fix for v in vulns for fix in (v.get("fix_versions") or [])}))
        (relevant if name in tree else skipped).append(row)

    print()
    print(f"=== 本專案相依樹之內、有已知漏洞的：{len(relevant)} ===")
    for name, version, ids, fixes in sorted(relevant):
        where = "直接" if name in roots else "傳遞"
        print(f"  [{where}] {name} {version}")
        print(f"           {', '.join(str(i) for i in ids)}")
        print(f"           修正版本：{', '.join(fixes) or '（公告未提供）'}")
    if relevant:
        print()
        print("下一步不是無腦加下限。照 requirements.txt 的策略：先判斷"
              "**本專案搆不搆得到**那條漏洞（哪個 API、哪個呼叫點），"
              "搆得到才加 `>=` 下限並把理由與呼叫點寫在該行上面。")

    print()
    print(f"=== 不在本專案相依樹內、已略過：{len(skipped)} ===")
    if skipped:
        print("  " + ", ".join(sorted(name for name, *_ in skipped)))
        print("  （這台機器的其他套件。列出來是為了看得出過濾條件對不對，"
              "不是要你去升級它們。）")

    # 這一段不是客套話。上面答的是「公告資料庫**今天**知道什麼」，而 2026-09-17
    # 實測兩次（simpleeval、urllib3，間隔五天）：專案自己發布的公告要過幾天才會被
    # 收進全域資料庫，那幾天裡這裡會印出乾淨的零。urllib3 那次漏掉的是兩條 high。
    print()
    print("※ 上面查的是**全域公告資料庫**。專案自己剛發布、還沒被收進去的公告"
          "這裡看不到（實測過兩次，其中一次是兩條 high）。"
          "要補這一塊：再跑一次 `--repo-advisories`。")
    return 1 if relevant else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
