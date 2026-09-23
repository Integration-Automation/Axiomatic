"""`_code_fingerprint` 的行為測試。

這個模組要回答的問題是「這個行程正在跑的是哪一版程式碼」，而它唯一會失效的方式
**不是丟例外，是安靜地永遠回報『沒有漂移』**。所以這裡的重點全部放在那個方向：

* 沒取過快照 → 必須是 `None`（不知道），**不可以**是 `False`（沒漂移）；
* 有檔案讀不到 → 同樣必須是 `None`；
* 快照必須在**取樣當下**定住，而不是查詢時才算——後者會拿磁碟跟磁碟自己比，
  結果永遠是「沒漂移」，而且測試也會全綠。這是本模組唯一真正的陷阱，
  `test_snapshot_is_frozen_at_snapshot_time` 就是為它寫的。

測試全部在**暫存目錄**裡造假模組，不碰 repo 裡任何真的檔案。
"""
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _code_fingerprint as cf  # noqa: E402


@pytest.fixture
def fake_pkg(tmp_path, monkeypatch):
    """把 `PACKAGE_ROOT` 換到暫存目錄，並提供「造一個已載入的模組」的工具。

    走 `sys.modules` 是這個模組的設計核心，所以測試也必須從那裡餵——直接在磁碟上
    放檔案是不夠的（那正是它刻意不掃目錄的原因）。
    """
    monkeypatch.setattr(cf, "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(cf, "_SNAPSHOT", None)
    monkeypatch.setattr(cf, "_SNAPSHOT_UNREADABLE", 0)

    def add(name: str, body: str, *, suffix: str = ".py") -> Path:
        """在假的套件根目錄下造一個檔，並把它登記成「已載入的模組」。

        用 `monkeypatch.setitem` 而不是直接指派 `sys.modules[name]`：teardown 會
        自動還原（原本不存在的鍵會被刪掉），**連測試中途丟例外也還原**。手寫的
        清理做不到這件事，而 `sys.modules.pop` 更是 `test_suite_safety` 明文禁止
        的寫法——`pop` 清掉的只是快取，下一次 import 會把真的模組載回來。
        """
        path = tmp_path / f"{name}{suffix}"
        path.write_text(body, encoding="utf-8")
        module = types.ModuleType(name)
        module.__file__ = str(path)
        monkeypatch.setitem(sys.modules, name, module)
        return path

    def add_module(name: str, module) -> None:
        """直接登記一個現成的模組物件（不落地成檔案）。"""
        monkeypatch.setitem(sys.modules, name, module)

    yield types.SimpleNamespace(add=add, add_module=add_module, root=tmp_path)


def test_no_snapshot_means_unknown_not_clean(fake_pkg):
    """沒取過快照時必須回 `None`。

    這是三態最重要的一態。摺成 `False` 的話，一個**忘了呼叫 `snapshot()`** 的進入點
    會永遠回報「程式碼沒有漂移」——那是本模組最糟的失效方式，因為它看起來完全正常。
    """
    fake_pkg.add("fake_alpha", "X = 1\n")
    assert cf.snapshot_taken() is False
    report = cf.drift_report()
    assert report["drifted"] is None
    assert "no snapshot" in report["why"]


def test_clean_when_nothing_changed(fake_pkg):
    fake_pkg.add("fake_alpha", "X = 1\n")
    fake_pkg.add("fake_beta", "Y = 2\n")
    cf.snapshot()
    assert cf.snapshot_taken() is True
    report = cf.drift_report()
    assert report["drifted"] is False
    assert report["changed"] == []
    assert report["at_start"] == report["now"]


def test_edit_after_snapshot_is_detected_and_named(fake_pkg):
    path = fake_pkg.add("fake_alpha", "X = 1\n")
    fake_pkg.add("fake_beta", "Y = 2\n")
    cf.snapshot()
    path.write_text("X = 2\n", encoding="utf-8")
    report = cf.drift_report()
    assert report["drifted"] is True
    assert report["changed"] == ["fake_alpha.py"]
    assert report["at_start"] != report["now"]


def test_snapshot_is_frozen_at_snapshot_time(fake_pkg):
    """**本模組唯一真正的陷阱。**

    如果 `snapshot()` 只記下「怎麼算」而不是「算出來的值」，`drift_report()` 就會
    拿磁碟跟磁碟自己比，永遠回 `False`。而那個錯誤版本會通過上面每一支測試——因為
    它們都在快照之後才改檔案，兩邊仍然一致……不對，是因為它們比的是同一個東西。
    這支測試靠**改檔案再改回來**分辨不了，所以改成直接檢查快照值本身有沒有被凍住。
    """
    path = fake_pkg.add("fake_alpha", "X = 1\n")
    first = cf.snapshot()
    stored = dict(cf._SNAPSHOT)  # pylint: disable=protected-access
    path.write_text("X = 999\n", encoding="utf-8")
    # 快照不可以跟著磁碟動。
    assert cf._SNAPSHOT == stored  # pylint: disable=protected-access
    assert cf.drift_report()["at_start"] == first
    # 而磁碟那一側必須是新的值。
    assert cf.current_fingerprint()[0] != first


def test_unreadable_file_is_unknown_not_clean(fake_pkg, monkeypatch):
    """讀不到檔案時必須回 `None`。

    失敗的掃描不可以長得跟乾淨的掃描一樣——本專案在 `_find_all_chrome_processes`、
    `_load_pid`、`dashboard_server._read_pid` 各踩過一次同一個形狀。
    """
    fake_pkg.add("fake_alpha", "X = 1\n")
    cf.snapshot()
    real_read = Path.read_bytes

    def boom(self):
        if self.name == "fake_alpha.py":
            raise OSError("nope")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", boom)
    report = cf.drift_report()
    assert report["drifted"] is None
    assert "unreadable" in report["why"]


def test_swapping_two_files_contents_changes_the_fingerprint(fake_pkg):
    """內容在兩個檔案之間互換，必須算漂移。

    注意這支**證明不了**「合併指紋有把路徑算進去」——`sorted(files.items())` 是按
    **檔名**排序的，所以互換之後摘要的**順序**就變了，連不含路徑的版本也看得出來。
    真正需要路徑的是改名，見下一支。（這個錯誤的理由原本就寫在這裡當作論據，是變異
    測試把它揪出來的：拿掉路徑的變異體讓這支照樣全綠。）
    """
    a = fake_pkg.add("fake_alpha", "X = 1\n")
    b = fake_pkg.add("fake_beta", "Y = 2\n")
    before = cf.snapshot()
    a.write_text("Y = 2\n", encoding="utf-8")
    b.write_text("X = 1\n", encoding="utf-8")
    assert cf.current_fingerprint()[0] != before


def test_renaming_a_module_changes_the_fingerprint(fake_pkg, monkeypatch):
    """合併指紋必須把**路徑**也算進去，而改名才是分辨得出來的那個情境。

    內容原封不動搬到另一個檔名：所有摘要的多重集完全一樣，只有名字變了。不把路徑
    餵進合併的版本會回報「沒有漂移」——但那是一次貨真價實的重構，而且正是最需要
    知道「跑的是哪一版」的時候（模組改名之後，舊行程的 traceback 會指向一個磁碟上
    已經不存在的檔名）。
    """
    body = "X = 1\n"
    fake_pkg.add("fake_alpha", body)
    before = cf.snapshot()
    (fake_pkg.root / "fake_alpha.py").unlink()
    monkeypatch.delitem(sys.modules, "fake_alpha")
    fake_pkg.add("fake_zeta", body)
    assert cf.current_fingerprint()[0] != before, (
        "同樣的內容換一個檔名，合併指紋卻沒變——路徑沒有被算進去")
    report = cf.drift_report()
    assert report["drifted"] is True
    assert report["added"] == ["fake_zeta.py"]
    assert report["removed"] == ["fake_alpha.py"]


def test_only_project_files_count(fake_pkg):
    """站外的模組（標準庫／site-packages）不算數。

    把它們算進去會讓「更新了一個相依套件」也變成程式碼漂移，而那對「這個行程跑的是
    哪一版**我們的**程式碼」這個問題是雜訊。會亂叫的警報最後會被關掉。
    """
    fake_pkg.add("fake_alpha", "X = 1\n")
    outsider = types.ModuleType("fake_outsider")
    outsider.__file__ = str(Path(os.__file__).resolve())
    fake_pkg.add_module("fake_outsider", outsider)
    files = cf._project_source_files()  # pylint: disable=protected-access
    names = {p.name for p in files}
    assert "fake_alpha.py" in names
    assert Path(os.__file__).name not in names


def test_modules_without_a_file_do_not_crash_the_scan(fake_pkg):
    """內建模組、namespace package、被設成 None 的項目都不能讓掃描炸掉。

    `sys.modules` 裡什麼都有：`__file__` 不存在的內建模組、`.pyd`、
    以及被別的測試塞成 `None` 的項目（本專案的 `_NoModule` 就是這樣做的）。
    """
    fake_pkg.add("fake_alpha", "X = 1\n")
    # `types.ModuleType` 本來就沒有 `__file__`（要有得自己設），所以這一行造出來的
    # 就是「內建模組那一類」。
    fake_pkg.add_module("fake_nofile", types.ModuleType("fake_nofile"))
    fake_pkg.add_module("fake_none", None)             # 刻意的 None（`_NoModule` 的手法）
    ext = types.ModuleType("fake_ext")
    ext.__file__ = str(fake_pkg.root / "fake_ext.pyd")  # 不是 .py
    fake_pkg.add_module("fake_ext", ext)
    report = cf.snapshot() and cf.drift_report()
    assert report["drifted"] is False
    assert "fake_ext.pyd" not in report["changed"]


def test_snapshot_can_be_retaken(fake_pkg):
    """重新取樣要覆寫舊的，不可以拒絕或疊加。"""
    path = fake_pkg.add("fake_alpha", "X = 1\n")
    cf.snapshot()
    path.write_text("X = 2\n", encoding="utf-8")
    assert cf.drift_report()["drifted"] is True
    cf.snapshot()
    assert cf.drift_report()["drifted"] is False


def test_describe_never_leaks_a_host_path(fake_pkg):
    """`describe()` 會被送到對話平台，受保密規則 Layer 1 約束。

    只能出現套件內的相對檔名與十六進位摘要——不可以有絕對路徑、磁碟機代號、
    或原始例外文字。
    """
    path = fake_pkg.add("fake_alpha", "X = 1\n")
    cf.snapshot()
    assert "matches disk" in cf.describe()
    path.write_text("X = 2\n", encoding="utf-8")
    line = cf.describe()
    assert "fake_alpha.py" in line
    for banned in (str(fake_pkg.root), ":\\", ":/", "Traceback"):
        assert banned not in line, f"`describe()` 洩漏了 {banned!r}：{line}"


def test_describe_says_unknown_rather_than_clean_when_it_cannot_tell(fake_pkg):
    """判斷不出來的時候，那一行不可以讀起來像「一切正常」。"""
    line = cf.describe()
    assert "unknown" in line
    assert "matches disk" not in line


def test_a_module_imported_after_the_snapshot_is_not_drift(fake_pkg):
    """**延遲 import 不算漂移。**

    `_project_source_files()` 走 `sys.modules`，所以任何在 `snapshot()` 之後才被
    import 進來的本專案模組都會出現在 `added` 裡。如果 `added` 算漂移，那麼**一次**
    延遲 import 就會讓這個回報從此永遠是 True——在 webrunner 那一側是每個角色印一
    行，而且每一行都是錯的。會亂叫的警報最後會被人關掉。

    語意上也該如此：那個模組是**剛剛才從磁碟載入的**，它是最新的。這裡問的是
    「我正在跑的還是不是磁碟上那一份」，而它正是。

    這條同時取代了一個守不住的外部約束——原本得要求呼叫端保證「`snapshot()` 之後
    不得再有本專案的 import」，而那種約束沒有任何東西擋得住，遲早被一個看起來人畜
    無害的延遲 import 破壞。
    """
    fake_pkg.add("fake_alpha", "X = 1\n")
    cf.snapshot()
    fake_pkg.add("fake_late", "Y = 2\n")        # 模擬延遲 import
    report = cf.drift_report()
    assert report["added"] == ["fake_late.py"], "應該仍要看得到它是新增的"
    assert report["drifted"] is False, (
        "延遲 import 被當成漂移了——這會讓警報從此永遠為真")
    assert cf.describe().endswith("(matches disk)")


def test_a_late_import_does_not_mask_a_real_edit(fake_pkg):
    """但延遲 import **不可以**把真正的修改蓋掉。

    上一支容易被過度修正成「有 added 就一律回 False」，那會讓「延遲 import 的同時
    另一個檔案被改了」變成靜默。兩件事要各自成立。
    """
    path = fake_pkg.add("fake_alpha", "X = 1\n")
    cf.snapshot()
    fake_pkg.add("fake_late", "Y = 2\n")
    path.write_text("X = 2\n", encoding="utf-8")
    report = cf.drift_report()
    assert report["drifted"] is True
    assert report["changed"] == ["fake_alpha.py"]
    assert "fake_alpha.py" in cf.describe()
    assert "fake_late.py" not in cf.describe(), (
        "`added` 不該出現在摘要裡——它不是問題，列出來會讓人以為那個檔案有事")
