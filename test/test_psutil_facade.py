"""本專案呼叫的每一個 `psutil.X`，裝著的 psutil 都要真的有。

這是第四支同形狀的守門，前三支是 `test_selenium_facade.py`、`test_gui_facade.py`、
`test_je_facade.py`。psutil 之前被判為「風險較低」，理由是它缺屬性會大聲丟
`AttributeError`，不像 `je_web_runner` 那樣被 `**kwargs` 吞掉。

**那個判斷是錯的，而且錯在本專案自己的寫法上。** 每一處 psutil 掃描都包在寬 except
裡，因為行程隨時會消失是常態：

    try:
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            ...
    except Exception as error:              # pylint: disable=broad-except
        print(f"... failed: {error!r}", file=sys.stderr)
    return found                            # ← 空的

所以某一天 psutil 改掉一個 API，症狀**不是**當掉，而是：

- `_find_all_webrunner_pids()` 回 `[]` → `/gen stop` 的 sweep 認為「沒有漏網的背景
  程式」→ **不殺**，兩個背景產圖程式同時搶同一個瀏覽器設定檔；
- `presence_probe` 的偵測全部回 None → 狀態顯示安靜地空掉。

兩個症狀都是「安靜地少做事」，而且錯誤只進 stderr。這正是靜態守門存在的理由。

原本這裡還有第三條：`_find_all_chrome_processes()` 回 `[]` → nuclear sweep
什麼都不做 → 下一次 `webdriver.Chrome(...)` 撞上舊的 singleton lock。
**2026-09-07 已經從根上修掉**——那支現在回 `(名單, 掃描是否完整)`，掃不成會直接
說出來，所以它不再是「安靜地」少做事。留這段是因為它示範了一件事：
**這支守門守的是「某一個成因」（psutil API 漂移），不是「後果」。**
權限不足、WMI 卡住、列舉途中的暫時性 OS 錯誤都會走到同一個 `except Exception`，
而那些成因靜態掃描一個都看不到。把成因寫進文件不等於守住了後果——後果要由
回傳值上的訊號來守，現在 `test_exception_handlers.py` 有一道 repo 層級的守門
（`test_a_failed_enumeration_is_not_reported_as_an_empty_one`）盯這個形狀。
`_find_all_webrunner_pids` 也已於同日比照辦理（回 `(名單, 掃描是否完整)`，五個呼叫端各自決定掃描不完整時要說什麼），並已從那支守門的豁免清單移除。

**時機也對**：psutil **8.0.0** 正在開發中，變更記錄裡已經有破壞性變更與棄用
（`Process.attrs` 這個新的類別屬性、`process_iter(attrs=[])` 棄用、
`Process.memory_full_info` 棄用改用 `memory_footprint`）。2026-08-30 逐條對過，
本專案目前一個都沒用到——這支測試就是用來確保「以後也還是沒用到，或用到了會紅」。
"""
from __future__ import annotations

import ast
from pathlib import Path

import psutil
import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PKG_ROOT.parent


def _project_files() -> list[Path]:
    # repo root 那一半是 `*.py`，不是 `start_*.py`——這條規則沒有指名任何模組。
    files = [p for p in PKG_ROOT.glob("*.py") if not p.name.startswith("test_")]
    # `test/`（本檔所在的目錄）照同一個判準過濾：`conftest.py` 與手動 e2e 腳本在
    # 2026-09-22 之前住在套件裡、在範圍內，搬家之後照舊。
    files += [p for p in Path(__file__).resolve().parent.glob("*.py")
              if not p.name.startswith("test_")]
    files += list(REPO_ROOT.glob("*.py"))
    return sorted(files)


def _psutil_bindings(tree: ast.Module) -> set[str]:
    """這個檔案把 psutil 綁到哪些名字上。

    **不要寫死 `"psutil"`**：`import psutil as _ps` 會讓寫死名字的掃描器完全看不到
    那個檔案，而且是無聲的——測試照樣全綠，只是什麼都沒掃到。這是實測出來的：
    mutation「把某個檔案的 import 改成別名」原本逃掉了，因為其他檔案仍然用著
    `psutil.`，`test_the_scanner_actually_found_uses` 的下限照樣過關。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "psutil":
                    names.add(alias.asname or "psutil")
    return names


def _module_level_uses() -> dict[str, list[str]]:
    """`<psutil 綁定名>.<name>` 的每一個呼叫點 → {屬性名: [檔案:行, …]}。"""
    uses: dict[str, list[str]] = {}
    for path in _project_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        bindings = _psutil_bindings(tree)
        if not bindings:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in bindings):
                uses.setdefault(node.attr, []).append(
                    f"{path.name}:{node.lineno}")
    return uses


# `process_iter(attrs=[...])` 要的屬性，以及對 `Process` 物件直接呼叫的方法。
# 這兩類 AST 認不出來（`proc.cmdline()` 的 `proc` 只是個區域變數），所以列舉，
# 並由下面的測試確認列舉的每一個都還存在。列舉不是懶惰，是把「本專案依賴
# psutil 的哪些介面」寫成一份可以被檢查的清單。
#
# `info` **不在**這份清單裡：它不是 `Process` 的類別屬性，而是
# `process_iter(attrs=[...])` 事後掛到**實例**上的。第一次跑這支測試就是被這一點
# 打紅的——`hasattr(psutil.Process, "info")` 是 False。它由下面
# `test_process_iter_still_accepts_the_attrs_keyword` 從實際回傳值那一側守。
_PROCESS_MEMBERS = [
    "pid", "ppid", "name", "cmdline",
    "memory_info", "terminate", "kill", "wait",
    # `/proc usage` 的資源報告（`_resource_report._psutil_rows`）用的四個。
    # `cpu_percent` 沒有 `interval` 引數時回的是「距離上一次呼叫」的平均，所以那
    # 支要呼叫兩次；`children(recursive=True)` 在 Windows 上只重建一次全系統的
    # 父子對照表，是本專案唯一負擔得起的家譜查法（逐行程問 `ppid` 是 O(N) 次
    # 重建，實測每次約 21 毫秒）。
    "cpu_percent", "create_time", "children",
]


def test_the_scanner_actually_found_uses():
    """空清單通過是最沒用的綠燈。"""
    uses = _module_level_uses()
    assert len(uses) >= 5, (
        f"只掃到 {len(uses)} 個 `psutil.*` 呼叫點，掃描條件可能壞了：{sorted(uses)}")


@pytest.mark.parametrize("attr", sorted(_module_level_uses()))
def test_every_psutil_attribute_the_project_calls_exists(attr):
    uses = _module_level_uses()[attr]
    assert hasattr(psutil, attr), (
        f"裝著的 psutil {psutil.__version__} 沒有 `psutil.{attr}`，"
        f"而本專案在 {uses[:3]} 用它。"
        "注意症狀不是當掉——這些呼叫全都包在寬 except 裡，所以會變成"
        "「掃不到任何行程」而只在 stderr 留一行。")


@pytest.mark.parametrize("member", _PROCESS_MEMBERS)
def test_every_process_member_the_project_uses_exists(member):
    assert hasattr(psutil.Process, member), (
        f"psutil {psutil.__version__} 的 `Process` 沒有 `{member}`。"
        f"（這份清單是手列的——`proc.{member}` 的接收者是區域變數，AST 認不出來。"
        "新增用法時請一起補進 `_PROCESS_MEMBERS`。）")


def test_the_exception_types_the_project_catches_are_still_exceptions():
    """`NoSuchProcess` / `AccessDenied` 若哪天不再是例外類別，
    `except (psutil.NoSuchProcess, psutil.AccessDenied)` 會在**執行期**丟 TypeError，
    而那一行正好在掃描迴圈裡面——又是一個安靜地掃不到東西的死法。"""
    for name in ("NoSuchProcess", "AccessDenied"):
        exc = getattr(psutil, name, None)
        assert isinstance(exc, type) and issubclass(exc, BaseException), (
            f"`psutil.{name}` 不再是例外類別（{exc!r}）")


def test_process_iter_still_accepts_the_attrs_keyword():
    """本專案每一處掃描都靠 `attrs=` 一次取好。psutil 8.0 已經在棄用
    `attrs=[]`（代表「全部取回」），所以這個關鍵字本身正在變動——真的呼叫一次，
    確認帶具名屬性的用法還通。"""
    procs = list(psutil.process_iter(attrs=["pid", "name"]))
    assert procs, "process_iter 一個行程都沒回，這不可能"
    sample = procs[0]
    assert set(sample.info) >= {"pid", "name"}, (
        f"`attrs=` 沒有照要求填 info：{sample.info!r}")


def test_ppid_is_still_worth_avoiding_in_bulk():
    """把「為什麼不把 ppid 放進 attrs」這件事釘成可執行的量測。

    psutil 在 Windows 的 `Process.ppid()` 是 `ppid_map()[self.pid]`，而 `ppid_map()`
    每次呼叫都重建整台機器的對照表 → `attrs=[…, "ppid"]` 是 O(N²)。上游 8.0.0 的
    變更記錄說 Windows 的 `Process.ppid` 快了約 58 倍，所以這個成本**預期會消失**。
    這支不是效能門檻（那在別人的機器上會 flaky），只是確認那條路還在、還能跑；
    真正擋住寫法的是 `test_process_control.py` 的 AST 掃描。
    """
    procs = list(psutil.process_iter(attrs=["pid", "name"]))[:5]
    for proc in procs:
        try:
            assert isinstance(proc.ppid(), int)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
