"""`audit_dependencies.py` 的守門。

**這支腳本的輸出被當成證據用過**（2026-09-08 的「相依掃描 0 個漏洞」就是它印的），
而它在此之前**一行都沒有被測過**（實測 coverage：92 個敘述、0%）。一支沒被測過、
卻會印出「安全」的稽核工具，是這個 repo 最不該有的東西——它跟
「log 說 dismissed 其實沒關掉」是同一種病：**我們相信了一個從沒被驗證過的回報**。

它會安靜地說謊的方式只有一種，但入口有好幾個：**把一條真的漏洞歸進「已略過」**。
只要名稱正規化、`requirements.txt` 的解析、或傳遞閉包任何一處算錯，那條漏洞就會被
判成「不在本專案相依樹內」，然後腳本印出一個令人安心的 `0` 並回傳結束碼 0。
所以這裡的測試全部壓在「該報的有沒有報」與「查不成不得看起來像安全」兩個方向。
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import audit_dependencies as ad  # noqa: E402


def _run_main(monkeypatch, *, deps, tree, roots=None) -> tuple[int, str]:
    """跑 `main()`，把外部依賴全部換掉。回 `(結束碼, 輸出)`。"""
    roots = roots if roots is not None else set(tree)
    monkeypatch.setattr(ad, "declared_requirements", lambda: set(roots))
    monkeypatch.setattr(ad, "dependency_closure", lambda _r: set(tree))
    monkeypatch.setattr(ad, "run_pip_audit", lambda: deps)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = ad.main()
    return rc, out.getvalue() + err.getvalue()


def _vuln(name, version="1.0", ids=("GHSA-xxxx",), fixes=("1.1",)):
    return {"name": name, "version": version,
            "vulns": [{"id": i, "fix_versions": list(fixes)} for i in ids]}


# ---------------------------------------------------------------------------
# 名稱正規化：這是「真漏洞被歸進已略過」最短的一條路
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("aiohttp", "aiohttp"),
    ("Pillow", "pillow"),
    ("typing_extensions", "typing-extensions"),
    ("typing.extensions", "typing-extensions"),
    ("zope.interface", "zope-interface"),
    ("A__B..C", "a-b-c"),
    ("  spaced  ", "spaced"),
])
def test_names_normalise_the_way_pypi_compares_them(raw, expected):
    """PyPI 的比對規則是大小寫不分、`_` `-` `.` 等價。

    這條錯掉的後果不是「格式不好看」：`requirements.txt` 寫 `typing_extensions`
    而 pip-audit 回 `typing-extensions`，兩邊對不上，那條漏洞就被判成「不在相依樹
    內」→ 進「已略過」→ 腳本回報 0。
    """
    assert ad._normalise(raw) == expected


# ---------------------------------------------------------------------------
# requirements.txt 的解析
# ---------------------------------------------------------------------------

def test_requirements_parsing_strips_everything_that_is_not_the_name(tmp_path):
    """版本指定、extras、環境標記都要剝掉，只留名字。

    剝不乾淨的話 `aiohttp>=3.13.6` 會被當成一個叫做 `aiohttp>=3.13.6` 的套件，
    永遠對不上 pip-audit 回的 `aiohttp`——同樣是把真漏洞推進「已略過」。
    """
    req = tmp_path / "requirements.txt"
    req.write_text(
        "# 註解不算\n"
        "\n"
        "aiohttp>=3.13.6  # 帶理由的下限\n"
        "discord.py\n"
        "Pillow==11.0.0\n"
        "uvicorn[standard]>=0.30\n"
        "psutil~=6.0\n"
        "tomli; python_version < '3.11'\n",
        encoding="utf-8")
    assert ad.declared_requirements(req) == {
        "aiohttp", "discord-py", "pillow", "uvicorn", "psutil", "tomli"}


def test_a_comment_only_requirements_file_yields_nothing_not_a_crash(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("# 全部都是註解\n\n# 真的\n", encoding="utf-8")
    assert ad.declared_requirements(req) == set()


# ---------------------------------------------------------------------------
# 過濾：該報的必須報
# ---------------------------------------------------------------------------

def test_a_vulnerable_direct_dependency_is_reported(monkeypatch):
    rc, out = _run_main(monkeypatch, deps=[_vuln("aiohttp")],
                        tree={"aiohttp"}, roots={"aiohttp"})
    assert rc == 1, "相依樹內有漏洞卻回了 0"
    assert "aiohttp" in out
    assert "[直接]" in out


def test_a_vulnerable_transitive_dependency_is_reported(monkeypatch):
    """傳遞相依也要報，而且要標成「傳遞」。

    只測直接相依的話，一個「只把 roots 當範圍」的錯誤實作也會全綠——而那正是
    這支腳本存在的理由（`aiohttp` 那次就是傳遞而來的）。
    """
    rc, out = _run_main(monkeypatch, deps=[_vuln("yarl")],
                        tree={"aiohttp", "yarl"}, roots={"aiohttp"})
    assert rc == 1
    assert "[傳遞]" in out


def test_an_unrelated_package_is_skipped_but_still_listed(monkeypatch):
    """不相干的套件要略過——**但必須列出名字**。

    列出來才看得出過濾條件對不對。默默消失的話，過濾器過頭了也沒人看得出來。
    """
    rc, out = _run_main(monkeypatch, deps=[_vuln("torch")], tree={"aiohttp"})
    assert rc == 0
    assert "已略過：1" in out
    assert "torch" in out


def test_case_and_separator_differences_do_not_hide_a_vulnerability(monkeypatch):
    """pip-audit 回 `Typing_Extensions`、相依樹裡是 `typing-extensions` —— 要對得上。

    這支是整份檔案裡最重要的一條：它是「真漏洞被靜默歸進已略過」那條路的直接反例。
    """
    rc, out = _run_main(monkeypatch, deps=[_vuln("Typing_Extensions")],
                        tree={"typing-extensions"})
    assert rc == 1, "大小寫／分隔符不同就對不上了——真漏洞會被當成不相干"
    assert "已略過：0" in out


def test_an_entry_with_no_vulns_is_not_counted(monkeypatch):
    """pip-audit 會把**每一個**套件都列出來，沒漏洞的 `vulns` 是空的。

    不濾掉的話每個套件都變成一筆「漏洞」，結束碼永遠是 1——一個永遠在叫的稽核
    工具跟沒有稽核是一樣的。
    """
    rc, out = _run_main(
        monkeypatch,
        deps=[{"name": "aiohttp", "version": "3.13.6", "vulns": []},
              {"name": "psutil", "version": "6.0", "vulns": None}],
        tree={"aiohttp", "psutil"})
    assert rc == 0
    assert "之內、有已知漏洞的：0" in out


# ---------------------------------------------------------------------------
# 查不成 ≠ 安全
# ---------------------------------------------------------------------------

def test_a_failed_audit_returns_2_and_says_it_is_not_safe(monkeypatch):
    """`run_pip_audit` 回 None 時必須回 2，而且要明講這不是「安全」。

    摺成 0 的話，一台沒裝 pip-audit 或連不上公告資料庫的機器會**每次都回報安全**。
    這正是本專案反覆踩到的形狀：失敗的掃描長得跟乾淨的掃描一模一樣。
    """
    rc, out = _run_main(monkeypatch, deps=None, tree={"aiohttp"})
    assert rc == 2, "查不成卻回了 0（＝看起來像安全）"
    assert "不是「安全」" in out


@pytest.mark.parametrize("stdout,stderr", [
    ("", "No module named pip_audit"),          # 沒裝
    ("not json at all", ""),                    # 輸出解析不了
    ("", "connection refused"),                 # 連不上
    ('{"unexpected": "shape"}', ""),            # 是 JSON 但沒有 dependencies
])
def test_unparseable_audit_output_never_reads_as_clean(monkeypatch, stdout, stderr):
    """pip-audit 的各種壞輸出都要導向「查不成」，不得變成一份空的乾淨結果。

    **`{"unexpected": "shape"}` 是四種裡最危險的一種**：它解析得開，所以**不會走到
    任何 except**；原本的 `json.loads(...).get("dependencies", [])` 會回 `[]`，
    也就是一份完全合法的「零漏洞」報告 ＋ 結束碼 0。pip-audit 換輸出格式（或哪天改成
    回一個錯誤物件）就會讓這支稽核工具靜默變成「永遠安全」——而它存在的唯一理由就是
    不要靜默地說安全。2026-09-08 補上形狀檢查後改為回 None。
    """
    class _Done:
        returncode = 1

    done = _Done()
    done.stdout, done.stderr = stdout, stderr
    monkeypatch.setattr(ad.subprocess, "run", lambda *a, **k: done)
    err = io.StringIO()
    with redirect_stderr(err):
        result = ad.run_pip_audit()
    assert result is None, f"壞輸出被讀成了 {result!r}"


@pytest.mark.parametrize("payload", [
    '{"dependencies": {}}',            # 有那個鍵，但不是 list
    '{"dependencies": null}',
    '[]',                              # 頂層是陣列
    '"just a string"',
    '42',
])
def test_a_wrong_shaped_but_valid_json_is_not_a_clean_report(monkeypatch, payload):
    """解析得開但形狀不對 → 「查不成」，不得變成零漏洞。

    這一組跟上面那支是同一條性質的不同入口。分開寫是因為它們**都不會走到 except**，
    而那正是這個缺陷當初躲過去的原因。
    """
    class _Done:
        returncode = 0
        stderr = ""

    done = _Done()
    done.stdout = payload
    monkeypatch.setattr(ad.subprocess, "run", lambda *a, **k: done)
    with redirect_stderr(io.StringIO()):
        assert ad.run_pip_audit() is None, f"{payload} 被讀成了乾淨的結果"


def test_a_well_formed_empty_report_is_still_clean(monkeypatch):
    """反面：形狀正確的空報告**就是**乾淨，不可以被新的檢查誤判成查不成。

    少了這一支，「一律回 None」也會讓上面全綠——而那會讓這支稽核永遠回 2，
    等於再也不會回報安全，一樣沒用。
    """
    class _Done:
        returncode = 0
        stderr = ""

    done = _Done()
    done.stdout = '{"dependencies": []}'
    monkeypatch.setattr(ad.subprocess, "run", lambda *a, **k: done)
    with redirect_stderr(io.StringIO()):
        assert ad.run_pip_audit() == []


def test_a_missing_pip_audit_is_reported_not_swallowed(monkeypatch):
    """沒裝 pip-audit 時要說得出「怎麼裝」，不能只丟一句失敗。"""
    class _Done:
        returncode = 1
        stdout = ""
        stderr = "No module named pip_audit"

    monkeypatch.setattr(ad.subprocess, "run", lambda *a, **k: _Done())
    err = io.StringIO()
    with redirect_stderr(err):
        assert ad.run_pip_audit() is None
    assert "pip install pip-audit" in err.getvalue()


def test_a_timeout_is_reported_as_could_not_check(monkeypatch):
    """逾時（連不上公告資料庫）也是「查不成」，不是「安全」。"""
    def boom(*_a, **_k):
        raise ad.subprocess.TimeoutExpired(cmd="pip-audit", timeout=1)

    monkeypatch.setattr(ad.subprocess, "run", boom)
    err = io.StringIO()
    with redirect_stderr(err):
        assert ad.run_pip_audit() is None
    assert "超過" in err.getvalue()


# ---------------------------------------------------------------------------
# 傳遞閉包
# ---------------------------------------------------------------------------

def test_the_closure_includes_the_roots_themselves():
    """閉包必須含 roots——少了這一條，直接相依的漏洞會全部被判成不相干。"""
    assert {"psutil"} <= ad.dependency_closure({"psutil"})


def test_an_uninstalled_package_does_not_break_the_closure():
    """查不到的套件停在那裡不往下走，不得讓整支掛掉。

    `requirements.txt` 可以宣告一個這個直譯器沒裝的東西（三份相依組合，見 DoD #4），
    那時候整份稽核不該直接死掉。
    """
    got = ad.dependency_closure({"a-package-that-is-definitely-not-installed-xyzzy"})
    assert "a-package-that-is-definitely-not-installed-xyzzy" in got


def test_the_closure_actually_walks_down_at_least_one_level():
    """下限釘樁：閉包必須真的比 roots 大。

    如果 `dependency_closure` 因為某次重構退化成「回傳 roots」，上面每一支都還會綠
    ——而過濾範圍會縮到只剩直接相依，傳遞而來的漏洞全部靜默進「已略過」。
    `aiohttp` 一定有下游相依（`yarl` / `multidict` / …），拿它當錨。
    """
    roots = {"aiohttp"}
    closure = ad.dependency_closure(roots)
    assert len(closure) > len(roots), (
        "傳遞閉包沒有往下走——過濾範圍會縮成只有直接相依")


# ---------------------------------------------------------------------------
# `--fresh`：fresh clone 會拿到的版本，與本機的差距
#
# 這一段守的是同一種病的另一個入口：**把一筆真的落後歸進「不是直接相依」**。
# `pip list --outdated` 在這台機器上一次回 207 筆，其中絕大多數跟本專案無關，
# 所以過濾條件錯一點點，唯一一筆有意義的就會消失在雜訊裡——而輸出看起來一樣乾淨。
# ---------------------------------------------------------------------------

def _row(name, version, latest, editable=None):
    row = {"name": name, "version": version, "latest_version": latest,
           "latest_filetype": "wheel"}
    if editable:
        row["editable_project_location"] = editable
    return row


def test_a_direct_dependency_behind_the_index_is_reported():
    normal, editable = ad.fresh_clone_drift(
        {"anthropic"}, [_row("anthropic", "1.3.0", "1.4.0")])
    assert normal == [("anthropic", "1.3.0", "1.4.0")]
    assert editable == []


def test_a_package_that_is_not_a_direct_dependency_is_ignored():
    """`pip list --outdated` 這台機器一次回兩百多筆，絕大多數不相干。

    不過濾的話這支工具會跟沒收斂範圍的 `pip-audit` 一樣，被當成雜訊整份忽略。
    """
    normal, editable = ad.fresh_clone_drift(
        {"anthropic"},
        [_row("torch", "1.0", "2.0"), _row("jupyter", "1.0", "2.0")])
    assert (normal, editable) == ([], [])


def test_the_index_name_is_matched_the_way_pypi_compares_it():
    """**這是這一段真正在守的東西。**

    `requirements.txt` 寫 `je-auto-control`，pip 印的是 `je_auto_control`。
    2026-09-09 的人工普查用字面比對掃過一輪、回報「只有 anthropic 落後」——
    而那一輪漏掉的，正好就是這一筆。用字面比對的版本在這支測試下會紅。
    """
    normal, _ = ad.fresh_clone_drift(
        {"je-auto-control"}, [_row("je_auto_control", "0.0.195", "0.0.222")])
    assert [n for n, *_ in normal] == ["je-auto-control"], (
        "pip 印的底線名沒有對上 `requirements.txt` 的連字號名——"
        "唯一一筆有意義的落後會安靜地被歸進「不相干」。")


def test_an_editable_install_is_listed_separately_and_never_counted_as_drift():
    """可編輯安裝的 `version` 是安裝當下凍住的中繼資料，比了不算數。

    拿它去跟套件庫比，得到的是「落後 27 版」這種完全誤導的數字——實際 import 到的
    是原始碼樹。所以它要進另一份清單，而且不得讓結束碼變成 1。
    """
    normal, editable = ad.fresh_clone_drift(
        {"je-auto-control"},
        [_row("je_auto_control", "0.0.195", "0.0.222",
              editable=r"<AutoControlGUI 的本機 checkout>")],
        source_version=lambda _n: "0.0.221")
    assert normal == []
    assert editable == [("je-auto-control", "0.0.195", "0.0.221", "0.0.222",
                         r"<AutoControlGUI 的本機 checkout>")]


def test_a_malformed_row_does_not_take_the_whole_sweep_down():
    """pip 換輸出格式時，寧可少報一筆，也不要整支炸掉而讓人以為「沒查」。"""
    normal, _ = ad.fresh_clone_drift(
        {"anthropic"},
        ["not a dict", None, _row("anthropic", "1.3.0", "1.4.0")])
    assert normal == [("anthropic", "1.3.0", "1.4.0")]


def _run_fresh(monkeypatch, *, rows, roots) -> tuple[int, str]:
    monkeypatch.setattr(ad, "declared_requirements", lambda: set(roots))
    monkeypatch.setattr(ad, "run_pip_outdated", lambda: rows)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = ad.main(["--fresh"])
    return rc, out.getvalue() + err.getvalue()


def test_fresh_returns_1_when_a_direct_dependency_is_behind(monkeypatch):
    rc, text = _run_fresh(monkeypatch, roots={"anthropic"},
                          rows=[_row("anthropic", "1.3.0", "1.4.0")])
    assert rc == 1
    assert "anthropic 1.3.0 → 1.4.0" in text


def test_fresh_returns_0_when_only_an_editable_is_behind(monkeypatch):
    """可編輯安裝不是「fresh clone 會拿到舊版」——它根本不從套件庫裝。

    替身回的是一個**真的原始碼樹不可能有**的版本號。原本寫的是 `"0.0.221"`——正好是
    本機可編輯安裝的原始碼樹當時的版本——而 `fresh_clone_drift` 的預設參數在 def 當下
    就綁死了真的函式，所以這個替身**從來沒生效過**：它在這台機器上會過，只因為真的那
    一支剛好回同一個數字；換到沒有可編輯安裝的 fresh clone 相依組合上就紅（2026-09-12
    第一次真的建出那一份相依組合來跑整套時抓到）。用一個不可能撞號的值，替身沒生效時
    在任何機器上都一定會紅。
    """
    monkeypatch.setattr(ad, "installed_source_version", lambda _n: "9.9.9-stub")
    rc, text = _run_fresh(
        monkeypatch, roots={"je-auto-control"},
        rows=[_row("je_auto_control", "0.0.195", "0.0.222",
                   editable=r"<AutoControlGUI 的本機 checkout>")])
    assert rc == 0
    assert "可編輯安裝" in text and "9.9.9-stub" in text, text


def test_fresh_returns_2_and_does_not_read_as_clean_when_it_cannot_check(
        monkeypatch):
    """查不成必須跟「沒有落後」分得開——這是 `run_pip_audit` 同一條教訓。"""
    rc, text = _run_fresh(monkeypatch, roots={"anthropic"}, rows=None)
    assert rc == 2
    assert "沒有查到任何東西" in text
    assert "不是「沒有落後」" in text


@pytest.mark.parametrize("payload", ['{"not": "a list"}', 'null', '"text"'])
def test_a_non_list_pip_output_is_a_failed_check_not_an_empty_result(
        monkeypatch, payload):
    """一份解析得開、但形狀不對的輸出，不得變成一份完美的綠燈報告。

    `run_pip_audit` 曾經就是這樣：`.get("dependencies", [])` 讓格式一變就永遠安全。
    這支從**上線那一支**的角度重驗同一個形狀，不是抄一份判斷邏輯。
    """
    class _Done:
        stdout = payload
        stderr = ""

    monkeypatch.setattr(ad.subprocess, "run", lambda *a, **k: _Done())
    err = io.StringIO()
    with redirect_stderr(err):
        assert ad.run_pip_outdated() is None
    assert "查不成" in err.getvalue()


def test_a_well_formed_pip_output_survives_the_shape_check(monkeypatch):
    """反面：上面那支不能是靠「永遠回 None」才綠的。"""
    class _Done:
        stdout = '[{"name": "anthropic", "version": "1.3.0", ' \
                 '"latest_version": "1.4.0"}]'
        stderr = ""

    monkeypatch.setattr(ad.subprocess, "run", lambda *a, **k: _Done())
    assert ad.run_pip_outdated() == [
        {"name": "anthropic", "version": "1.3.0", "latest_version": "1.4.0"}]


def test_calling_main_without_arguments_does_not_read_pytests_command_line(
        monkeypatch):
    """`main()` 沒帶引數時一律當「沒有旗標」，不得去讀 `sys.argv`。

    argparse 的預設行為是讀 `sys.argv`——而測試呼叫 `main()` 時那是 pytest 的
    命令列（`-q --timeout=900`），argparse 會報 unrecognized arguments 然後
    `SystemExit(2)`。實際踩到過：加上 `--fresh` 的那一刻，既有的六支測試全紅。
    """
    monkeypatch.setattr(sys, "argv", ["pytest", "-q", "--timeout=900"])
    rc, _text = _run_main(monkeypatch, deps=[], tree={"psutil"})
    assert rc == 0


# ---------------------------------------------------------------------------
# 倉庫層級公告：版本區間的剖析
#
# `vulnerable_version_range` 是公告作者手寫的自由文字。2026-09-17 實測本專案 14 個
# 倉庫共 78 個 pip 生態系區間，其中 **9 個（12%）** 直接餵 `SpecifierSet` 會丟例外，
# 而且全部集中在 Pillow。天真的 `try/except: continue` 會讓那 9 條無聲消失，輸出跟
# 「查過、乾淨」長得一模一樣——所以這一段的重點不只是「解得開」，是「解不開的時候
# 要回 None 讓呼叫端印出來」。

@pytest.mark.parametrize("raw, expected", [
    # 直接就吃得下的
    ("<1.0.8", "<1.0.8"),
    (">=2.6.2, <2.8.0", ">=2.6.2, <2.8.0"),
    ("<=3.14.2", "<=3.14.2"),
    # 真實資料裡出現過、但 SpecifierSet 不認得的三種寫法
    ("≤ 12.2.0", "<= 12.2.0"),          # Unicode 的 ≤
    ("≥ 11.2.1", ">= 11.2.1"),          # 對稱的 ≥
    ("5.2.0 - 12.2.0", ">=5.2.0,<=12.2.0"),  # 破折號區間
    ("11.2.0", "==11.2.0"),                  # 光禿禿一個版本號
    ("= 4.14.0", "==4.14.0"),                # 單一個等號（PEP 440 要兩個）
    ("=3.1.59", "==3.1.59"),
    # 連續空白收成一個（不是全部拿掉——`SpecifierSet` 本來就吃得下運算子後面的空白）
    ("<=  3.14.1", "<= 3.14.1"),
])
def test_the_hand_written_version_ranges_are_understood(raw, expected):
    assert ad.normalise_version_range(raw) == expected


@pytest.mark.parametrize("raw", [
    "看不懂的東西",
    "所有版本",
    "",
    ">>1.0",
])
def test_an_unreadable_range_returns_none_rather_than_a_guess(raw):
    """解不開一定要回 None。

    ⚠️ 這裡**不可以**改成回一個「不匹配任何版本」的區間來讓流程順下去：那會把
    「看不懂」變成「沒事」，而這支工具的整個用途就是不要靜默地說安全。
    """
    assert ad.normalise_version_range(raw) is None


@pytest.mark.parametrize("version, raw_range, expected", [
    # 真實案例：urllib3 2.7.0 落在三條公告的區間裡
    ("2.7.0", ">=2.6.2, <2.8.0", True),
    ("2.8.0", ">=2.6.2, <2.8.0", False),
    ("1.0.7", "<1.0.8", True),
    ("1.0.8", "<1.0.8", False),
    # 需要正規化才判得出來的三種
    ("12.2.0", "≤ 12.2.0", True),
    ("12.3.0", "≤ 12.2.0", False),
    ("11.0.0", "5.2.0 - 12.2.0", True),
    ("4.0.0", "5.2.0 - 12.2.0", False),
    ("11.2.0", "11.2.0", True),
    ("11.2.1", "11.2.0", False),
    # 預先發行版一律「不知道」。PEP 440 規定排他性的 `<V` 不得納入 V 自己的預先
    # 發行版，於是 `1.0.8a1` 對著 `<1.0.8` 會算出「不受影響」——而它明明在修正之前。
    # 那個方向是**假陰性**，所以這裡拒答而不是猜。
    ("2.8.0rc1", ">=1.10.3, <2.8.0", None),
    ("2.8.0rc1", ">=1.10.3, <2.8.1", None),
    ("1.0.8a1", "<1.0.8", None),
    ("1.0.8.dev1", "<1.0.8", None),
    # 早退的**邊界**。這裡原本是 `("1.0.8", "<1.0.8", False)` 與
    # `("1.0.7", "<1.0.8", True)` 兩筆，註解寫著「少了這兩筆，『一律回 None』也會
    # 全綠」——**那句話是假的**：它們跟上面第 3、第 4 筆一字不差，刪掉不會少測到
    # 任何東西（而且光前十筆就有一堆非 None 的答案，「一律回 None」本來就綠不了）。
    # 換成真的 near-miss：post-release 與 local version 看起來也「不是純數字」，但
    # PEP 440 說它們**不是**預先發行版，所以必須照常判定、不得回 None。實測
    # 2026-09-20：`1.0.7.post1` 在區間內、`1.0.8.post1` 不在，local 版一樣。少了
    # 這四筆，把早退寫成「版本字串含非數字就回 None」、或順手加上
    # `or parsed.is_postrelease`，都不會有任何症狀。
    ("1.0.7.post1", "<1.0.8", True),
    ("1.0.8.post1", "<1.0.8", False),
    ("1.0.7+local.1", "<1.0.8", True),
    ("1.0.8+local.1", "<1.0.8", False),
    # 看不懂的區間 -> 不知道，不是「沒事」
    ("1.0.0", "看不懂", None),
    ("看不懂", "<1.0.8", None),
])
def test_whether_the_installed_version_is_inside_the_range(
        version, raw_range, expected):
    assert ad.version_is_in_range(version, raw_range) is expected


def test_a_repo_override_names_a_package_that_is_actually_scanned():
    """對照表裡的套件名必須還在**掃描範圍**內（＝相依閉包）。

    跟 `_OWNER_ONLY_SLASH` 同一個形狀：套件被移除或改名之後，這裡的鍵會變成一個
    永遠對不上的字串，而工具照跑、測試照綠，那個套件從此靜悄悄地走 PyPI 解析
    （而它當初被放進來就是因為 PyPI 解析不出來）。

    比對的是**閉包**不是直接相依：掃描範圍 2026-09-17 從直接相依擴到整個閉包，
    而 `sortedcontainers` 正是傳遞而來的（selenium → trio → sortedcontainers）。
    這支測試當時就紅了一次，是它自己把範圍改動抓出來的。
    """
    closure = ad.dependency_closure(ad.declared_requirements())
    assert len(closure) >= 10, (
        f"閉包只有 {len(closure)} 個，剖析大概壞了——這支測試會空轉通過")
    for name in ad._REPO_OVERRIDES:
        assert ad._normalise(name) in closure, (
            f"`_REPO_OVERRIDES` 裡的 `{name}` 已經不在相依閉包裡了——"
            "刪掉它，或改成現在的名字。")


def test_an_override_short_circuits_the_network_lookup(monkeypatch):
    """有對照表的套件不該再去打 PyPI。

    正對照：沒有這一句的話，「對照表有沒有被用到」跟「PyPI 剛好也解得出同一個
    倉庫」在結果上分不出來。
    """
    def _boom(*_args, **_kwargs):
        raise AssertionError("有對照表還去打了網路")

    monkeypatch.setattr(ad, "_get_json", _boom)
    name, repo = next(iter(ad._REPO_OVERRIDES.items()))
    assert ad.source_repo(name, "1.0.0") == repo


# ---------------------------------------------------------------------------
# `patched_versions`：跟區間**相反**的正規化，以及開口區間的假陽性
#
# 2026-09-17 把掃描範圍拉到整個相依閉包時發現：很多公告的 `vulnerable_version_range`
# 是**開口**的（`>= 44.0.0`，沒有上界），真正的界線只寫在 `patched_versions`。只看
# 區間的話，12 筆「命中」裡有 10 筆是假陽性（`cryptography` 50.0.1 對著 `>= 44.0.0`
# ——而它的 patched 是 50.0.0，早就修好了）。一支喊狼來了的工具會被關掉。

@pytest.mark.parametrize("raw, expected", [
    # 光禿禿一個版本號在這裡是「這一版之後就安全」-> `>=`（跟區間的 `==` 相反）
    ("1.0.8", ">=1.0.8"),
    ("2.8.0", ">=2.8.0"),
    # 本來就是條件式的照用
    (">=46.0.7", ">=46.0.7"),
    ("≥ 44.0.1", ">= 44.0.1"),
    # 多分支：`v1.8.2,v1.7.4,v1.6.2` 是三個並列的修正版，取最大的當門檻
    ("v1.8.2,v1.7.4,v1.6.2", ">=1.8.2"),
    ("1.26.19, 2.2.2", ">=2.2.2"),
    # 沒填 / 看不懂
    ("", None),
    ("（未提供）", None),
])
def test_the_patched_versions_field_is_normalised_the_other_way(raw, expected):
    assert ad.normalise_patched_versions(raw) == expected


def test_the_two_fields_normalise_a_bare_version_in_opposite_directions():
    """釘住這一段最容易寫反、而且寫反不會有症狀的地方。

    兩個欄位共用一支正規化函式的話其中一邊一定是錯的，而錯的方向是**靜默翻轉
    結論**——不是報錯。
    """
    assert ad.normalise_version_range("1.0.8") == "==1.0.8"
    assert ad.normalise_patched_versions("1.0.8") == ">=1.0.8"


@pytest.mark.parametrize("installed, rng, patched, expected", [
    # 真實假陽性：開口區間 ＋ 已經修好了
    ("50.0.1", ">= 44.0.0", "50.0.0", False),
    ("50.0.1", ">=45.0.0", ">=46.0.7", False),
    ("4.62.1", ">=4.33.0", ">=4.60.2", False),
    # 真實真陽性：有界區間 ＋ 還沒修
    ("1.0.7", "<1.0.8", "1.0.8", True),
    ("2.7.0", ">=2.6.2, <2.8.0", "2.8.0", True),
    ("4.13.0", "<=4.14.1", "4.14.2", True),
    # 區間剖不開，但 patched 判得出「已經安全」-> 從「不知道」升級成確定沒事
    ("0.16.0", "0.15.0 and earlier", "0.16.0", False),
    # 區間剖不開、patched 也不能證明安全 -> 老實說不知道
    ("4.13.0", "看不懂的寫法", "4.14.2", None),
    # 預先發行版一律拒答（PEP 440 的 `<V` 會給假陰性）
    ("1.0.8rc1", "<1.0.8", "1.0.8", None),
])
def test_whether_the_installed_version_is_affected(installed, rng, patched, expected):
    assert ad.is_affected(installed, rng, patched) is expected


def test_the_patched_check_runs_before_the_range_check():
    """順序不能反——這是 10 筆假陽性的成因。

    正對照：如果先看區間、命中就回 True，下面這個案例會是 True。
    """
    assert ad.version_is_in_range("50.0.1", ">= 44.0.0") is True   # 只看區間＝命中
    assert ad.is_affected("50.0.1", ">= 44.0.0", "50.0.0") is False  # 先看 patched


# ---------------------------------------------------------------------------
# 從 project_urls 解析倉庫：贊助連結長得跟倉庫一模一樣

@pytest.mark.parametrize("urls, expected", [
    # 贊助連結排在前面也不可以被挑走（attrs / pydantic / pydantic-core /
    # audioop-lts 四個真實案例都是這個形狀，而正確的倉庫就在同一張表裡）
    ({"Funding": "https://github.com/sponsors/hynek",
      "GitHub": "https://github.com/python-attrs/attrs"}, "python-attrs/attrs"),
    ({"Funding": "https://github.com/sponsors/samuelcolvin",
      "Homepage": "https://github.com/pydantic/pydantic",
      "Source": "https://github.com/pydantic/pydantic"}, "pydantic/pydantic"),
    # 其他非倉庫命名空間
    ({"A": "https://github.com/orgs/python/repositories",
      "Source": "https://github.com/psf/requests"}, "psf/requests"),
    # 明講是原始碼的鍵優先，不受 dict 順序影響
    ({"Issues": "https://github.com/AbstractUmbra/audioop/issues",
      "Source": "https://github.com/AbstractUmbra/audioop"},
     "AbstractUmbra/audioop"),
    # ⚠️ 上面那筆**驗不到優先序**：兩個網址指向同一個倉庫，所以後備的「照順序挑
    # 第一個」也會得到同樣答案（變異測試實測 SURVIVED）。要驗優先序，第一個出現的
    # 必須是一個**看起來合法但不對**的倉庫——例如另外一個放文件的倉庫。
    ({"Documentation": "https://github.com/pydantic/pydantic-docs",
      "Source": "https://github.com/pydantic/pydantic"}, "pydantic/pydantic"),
    ({"Changelog": "https://github.com/urllib3/urllib3-mirror",
      "Repository": "https://github.com/urllib3/urllib3"}, "urllib3/urllib3"),
    # `.git` 結尾要去掉
    ({"Source": "https://github.com/psf/requests.git"}, "psf/requests"),
    # 完全沒有 GitHub 位址 -> None（要當成「沒查到」，不是「乾淨」）
    ({"Homepage": "https://www.selenium.dev"}, None),
    ({}, None),
    # 只有贊助連結 -> 沒有倉庫可查，一樣是 None 而不是 `sponsors/x`
    ({"Funding": "https://github.com/sponsors/hynek"}, None),
])
def test_a_funding_link_is_never_mistaken_for_a_source_repository(urls, expected):
    assert ad.repo_from_project_urls(urls) == expected


# ---------------------------------------------------------------------------
# 環境標記：閉包不該混進這台機器永遠不會有的東西

def test_a_marker_that_cannot_hold_here_is_not_walked_into():
    """`sys_platform == 'emscripten'` 是 Pyodide 專用，Windows 上不可能裝。"""
    raw = "httpx2-jsfetch; sys_platform == 'emscripten'"
    assert ad._requirement_applies(raw, "httpx2-jsfetch") is False


def test_an_extras_only_requirement_is_skipped_even_when_it_is_installed():
    """extras 專屬的相依一律不算，**而且不套「裝著就保留」那個保險**。

    這一條是量出來的，不是風格：`py -3` 是通用直譯器，套上保險會讓閉包從 63
    暴增到 259，掃出 89 筆命中而絕大多數落在本專案一行都沒用到的套件上。
    """
    # `pytest` 一定裝著——這正是保險會誤放行的形狀
    assert ad._requirement_applies("pytest; extra == 'test'", "pytest") is False


def test_a_plain_requirement_and_a_holding_marker_are_both_walked_into():
    """反面：沒有標記、或標記成立的，都要往下走（否則「一律回 False」也會全綠）。"""
    assert ad._requirement_applies("idna>=2.8", "idna") is True
    assert ad._requirement_applies("idna; python_version >= '3.0'", "idna") is True


def test_a_marker_that_fails_but_whose_package_is_installed_is_kept():
    """標記不成立但套件真的裝著時仍要往下走——少掃一個存在的套件是漏報。"""
    raw = "pytest; python_version < '3.0'"      # 在 3.14 上不成立
    assert ad._requirement_applies(raw, "pytest") is True


# ---------------------------------------------------------------------------
# 從這裡往下是 2026-09-20 補的：整個**對外那一層**在此之前一行都沒有被執行過。
#
# 量出來的（全樹覆蓋率）：`audit_dependencies.py` 67.8%，而缺的 136 行幾乎全部擠在
# 三個地方——`github_token()`、`_get_json()`、`report_repository_advisories()`。
# 它們共同的形狀就是這個 repo 已經吃過幾次虧的那一種：**要嘛需要網路、要嘛只在
# 出事時才跑**，所以單元測試從來沒碰過它們，而它們恰好是這支工具「會不會安靜地
# 說謊」與「會不會把憑證送錯地方」的全部。
#
# 三件事在這裡被第一次釘住：
#   1. `Authorization` 標頭**只准**往 `https://api.github.com/` 送。這是硬規則，
#      而在此之前唯一在守它的是 `_get_json` 裡那一行 `url.startswith(...)`——沒有
#      任何測試執行過它。把那個條件刪掉，token 就會跟著每一次 PyPI 查詢一起送出，
#      而輸出、結束碼、整套測試完全不變。
#   2. 「沒查到」不得回 0。`report_repository_advisories()` 的結束碼註解裡已經寫著
#      2026-09-17 那次實跑的教訓（限速砍掉 16 個裡的 6 個，工具照樣回 0），但那段
#      修正本身**沒有測試**，所以它可以被改回去而沒有任何症狀。
#   3. token 的值不准出現在輸出裡。函式的 docstring 用 ⚠️ 寫了這條，而 ⚠️ 不是
#      執行力。


class _FakeResponse:
    """`urlopen` 的替身；`json.load` 只要 `.read()`，`with` 只要進出。"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, *args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _recording_urlopen(monkeypatch, payload=b'{"ok": true}', boom=None):
    """攔住 `urlopen`，把每一個 `Request` 收起來。回傳那份清單。"""
    seen = []

    def _urlopen(request, *_args, **_kwargs):
        seen.append(request)
        if boom is not None:
            raise boom
        return _FakeResponse(payload)

    monkeypatch.setattr(ad.urllib.request, "urlopen", _urlopen)
    return seen


# 測試用的假值。長得不像真 token，而且**刻意**在名字裡寫清楚它不該被印出來——
# 萬一哪天真的漏進輸出裡，grep 得到的字串會自己解釋問題。
_FAKE_TOKEN = "tok-THIS-MUST-NEVER-BE-PRINTED"


@pytest.mark.parametrize("url, carries_token", [
    # 唯一該帶的一種
    ("https://api.github.com/repos/o/r/security-advisories", True),
    # PyPI 不需要、也不該拿到 GitHub 的憑證
    ("https://pypi.org/pypi/aiohttp/3.14.0/json", False),
    # 近似命中：網域只是**開頭**像，實際主機是別人的
    ("https://api.github.com.evil.test/repos/o/r", False),
    # 近似命中：明文 http。帶著 Bearer 走明文等於把 token 廣播出去
    ("http://api.github.com/repos/o/r", False),
    # 近似命中：子網域不是 api
    ("https://raw.github.com/o/r", False),
])
def test_the_github_token_only_ever_goes_to_the_github_api(
        monkeypatch, url, carries_token):
    """憑證的去向是硬規則，而守著它的是一行從沒被執行過的 `startswith`。

    前四格裡有三格是**放寬**方向的近似命中（`api.github.com.evil.test`、明文
    `http`、`raw.github.com`），因為 `startswith("https://api.github.com/")` 是一個
    收窄步驟——只餵「該帶」與「明顯不該帶」兩種，把條件改寬鬆（例如換成
    `"github.com" in url`）的變異會全綠。
    """
    seen = _recording_urlopen(monkeypatch)
    ad._get_json(url, _FAKE_TOKEN)
    assert len(seen) == 1, f"送出了 {len(seen)} 個請求"
    header = seen[0].get_header("Authorization")
    if carries_token:
        assert header == f"Bearer {_FAKE_TOKEN}", (
            f"該帶憑證的請求沒帶：{header!r}")
    else:
        # 失敗訊息裡**不放**標頭的值：那個值就是 token，而失敗訊息會被貼出來。
        assert header is None, (
            f"憑證被送到 {url} 了——這是不可逆的洩漏，貼出去就救不回來")


def test_without_a_token_no_request_carries_an_authorization_header(monkeypatch):
    """反面：沒有 token 時，連該帶的那個網址也不准長出標頭。

    少了這一格，「只往 GitHub 送」與「永遠都送」在上面那支裡分得出來，但
    「`token` 是空字串時仍然送出 `Bearer `」分不出來。
    """
    seen = _recording_urlopen(monkeypatch)
    ad._get_json("https://api.github.com/repos/o/r", None)
    ad._get_json("https://api.github.com/repos/o/r", "")
    assert len(seen) == 2
    assert [r.get_header("Authorization") for r in seen] == [None, None]


def test_every_request_names_a_user_agent(monkeypatch):
    """GitHub 對沒有 User-Agent 的請求直接回 403，而 403 在這裡會被歸進
    「查不到」——跟「查過沒事」只差一個結束碼。"""
    seen = _recording_urlopen(monkeypatch)
    ad._get_json("https://pypi.org/pypi/x/json")
    assert seen[0].get_header("User-agent") == ad._HTTP_UA


def test_a_failed_fetch_reads_as_could_not_check_not_as_nothing_found(
        monkeypatch):
    """任何失敗都回 None——**不是** `[]`、不是 `{}`。

    呼叫端靠 `None` 把該套件歸進「沒有查到」並讓結束碼變 2；回一個空容器會讓它
    走進「查過、乾淨」那條路。
    """
    _recording_urlopen(monkeypatch, boom=OSError("no network"))
    assert ad._get_json("https://api.github.com/repos/o/r") is None

    _recording_urlopen(monkeypatch, payload=b"<html>not json</html>")
    assert ad._get_json("https://api.github.com/repos/o/r") is None


# ---------------------------------------------------------------------------
# `github_token()`：拿得到就用，拿不到就明說沒有

def _no_token_env(monkeypatch):
    """⚠️ 先把開發者自己 shell 裡的值清掉再量。

    這台機器上的 shell **真的**可能帶著 `GH_TOKEN`，那時每一支測試都會走進第一
    條路，而 `gh auth token` 那半永遠沒被執行過卻看起來有在測。跟
    `test_supervisor` 對 `PYTHONIOENCODING` 做的事情是同一招。
    """
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _fake_gh(monkeypatch, stdout="", boom=None):
    calls = []

    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if boom is not None:
            raise boom
        return type("Done", (), {"stdout": stdout, "stderr": "",
                                 "returncode": 0})()

    monkeypatch.setattr(ad.subprocess, "run", _run)
    return calls


def test_the_first_environment_variable_wins(monkeypatch):
    """`GH_TOKEN` 排在 `GITHUB_TOKEN` 前面，而順序是有意義的：兩個都設的時候
    必須是同一個贏，否則同一台機器上的兩次執行會用不同的身分。"""
    _no_token_env(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "  from-gh-token  ")
    monkeypatch.setenv("GITHUB_TOKEN", "from-github-token")
    _fake_gh(monkeypatch, stdout="from-cli")
    assert ad.github_token() == "from-gh-token"          # 順便釘住 strip


def test_a_blank_environment_variable_falls_through_to_the_cli(monkeypatch):
    """空字串不算「設了」。設成空值卻走進第一條路的話，回傳的是一個空 token，
    而帶著空 token 的請求跟未認證一樣會被限速——卻看起來像認證過了。"""
    _no_token_env(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "")
    calls = _fake_gh(monkeypatch, stdout="from-cli\n")
    assert ad.github_token() == "from-cli"
    assert calls and calls[0][0] == ["gh", "auth", "token"]


def test_no_gh_cli_at_all_is_no_token_rather_than_a_crash(monkeypatch):
    """`gh` 沒裝是常態（fresh clone、別人的機器）。這支工具的降級路徑是
    「未認證照跑、在輸出裡說清楚」，不是把整個稽核炸掉。"""
    _no_token_env(monkeypatch)
    _fake_gh(monkeypatch, boom=FileNotFoundError("gh"))
    assert ad.github_token() is None


def test_an_empty_answer_from_the_cli_is_no_token(monkeypatch):
    """`gh` 裝了但沒登入時 stdout 是空的。回空字串的話，呼叫端的
    「有沒有認證」判斷會說「有」，而實際上沒有。"""
    _no_token_env(monkeypatch)
    _fake_gh(monkeypatch, stdout="   \n")
    assert ad.github_token() is None


def test_the_cli_lookup_is_bounded(monkeypatch):
    """`gh auth token` 要有 timeout。沒有的話，一個卡住的子行程會讓整支稽核
    永遠掛著——而掛著比失敗難查，因為沒有任何輸出。"""
    _no_token_env(monkeypatch)
    calls = _fake_gh(monkeypatch, stdout="x")
    ad.github_token()
    assert calls[0][1].get("timeout"), f"沒有設 timeout：{calls[0][1]}"


# ---------------------------------------------------------------------------
# `repository_advisories()`：只收已發布的，查不成回 None

def test_only_published_advisories_are_returned(monkeypatch):
    """草稿（`state != "published"`）是還沒公開的東西，拿它當命中是誤報。"""
    payload = [{"ghsa_id": "GHSA-a", "state": "published"},
               {"ghsa_id": "GHSA-b", "state": "draft"},
               {"ghsa_id": "GHSA-c", "state": "triage"},
               "不是 dict"]
    monkeypatch.setattr(ad, "_get_json", lambda *_a, **_k: payload)
    got = ad.repository_advisories("o/r")
    assert [a["ghsa_id"] for a in got] == ["GHSA-a"]


@pytest.mark.parametrize("payload", [None, {"message": "Not Found"}, "字串"])
def test_a_payload_that_is_not_a_list_is_could_not_check(monkeypatch, payload):
    """404／限速／錯誤訊息都不是 list。回 `[]` 會讓呼叫端把它當成「查過、
    沒有公告」，那正是這支工具最不該做的事。"""
    monkeypatch.setattr(ad, "_get_json", lambda *_a, **_k: payload)
    assert ad.repository_advisories("o/r") is None


def test_the_advisory_query_hits_the_repository_endpoint_with_the_token(
        monkeypatch):
    """網址與 token 一起往下傳。倉庫層級端點跟全域資料庫是**不同**的來源——
    這支工具存在的理由就是前者查得到後者還沒收進去的東西。"""
    seen = []
    monkeypatch.setattr(ad, "_get_json",
                        lambda url, token=None: seen.append((url, token)) or [])
    ad.repository_advisories("SeleniumHQ/selenium", _FAKE_TOKEN)
    assert seen == [
        ("https://api.github.com/repos/SeleniumHQ/selenium/security-advisories",
         _FAKE_TOKEN)]


# ---------------------------------------------------------------------------
# `report_repository_advisories()`：結束碼就是這支工具的全部結論

def _advisory(ghsa="GHSA-test", package="demo", rng="<1.0.8", patched="1.0.8",
              severity="high", ecosystem="pip"):
    return {"ghsa_id": ghsa, "severity": severity, "state": "published",
            "vulnerabilities": [{
                "package": {"ecosystem": ecosystem, "name": package},
                "vulnerable_version_range": rng,
                "patched_versions": patched}]}


def _run_repo_advisories(monkeypatch, packages, *, roots=None,
                         token=_FAKE_TOKEN):
    """跑 `report_repository_advisories()`，外部的東西全部換掉。

    `packages`：`{名稱: (裝著的版本, 倉庫 or None, 公告 list or None)}`。
    倉庫是 None ＝ 解析不出來；公告是 None ＝ 查詢失敗。
    """
    by_name = dict(packages)
    monkeypatch.setattr(ad, "github_token", lambda: token)
    monkeypatch.setattr(ad, "declared_requirements",
                        lambda: set(roots if roots is not None else by_name))
    monkeypatch.setattr(ad, "dependency_closure", lambda _r: set(by_name))
    monkeypatch.setattr(ad, "_installed_version", lambda n: by_name[n][0])
    monkeypatch.setattr(ad, "installed_source_version", lambda _n: None)
    monkeypatch.setattr(ad, "source_repo", lambda n, _v: by_name[n][1])
    repos = {info[1]: info[2] for info in by_name.values() if info[1]}
    monkeypatch.setattr(ad, "repository_advisories",
                        lambda repo, _token=None: repos[repo])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = ad.report_repository_advisories()
    return rc, out.getvalue() + err.getvalue()


def test_an_advisory_matching_the_installed_version_returns_1(monkeypatch):
    rc, text = _run_repo_advisories(monkeypatch, {
        "demo": ("1.0.7", "o/demo", [_advisory()])})
    assert rc == 1, text
    assert "GHSA-test" in text
    assert "[直接]" in text, "沒有標出這是直接相依還是傳遞而來的"


def test_everything_checked_and_nothing_matching_returns_0(monkeypatch):
    """已經修好的版本不得算命中——這是 2026-09-17 那 10 筆假陽性的方向。"""
    rc, text = _run_repo_advisories(monkeypatch, {
        "demo": ("1.0.9", "o/demo", [_advisory()])})
    assert rc == 0, text


@pytest.mark.parametrize("field, value", [
    ("ecosystem", "npm"),          # 同一個倉庫裡的 JS 套件
    ("package", "someone-else"),   # 同一個倉庫裡的另一個 Python 套件
])
def test_an_advisory_about_something_else_in_the_same_repo_is_not_a_hit(
        monkeypatch, field, value):
    """一個倉庫可以放好幾個套件。不比對生態系與名稱的話，`aiohttp` 的倉庫裡
    任何一條公告都會被算到 `aiohttp` 頭上。"""
    rc, text = _run_repo_advisories(monkeypatch, {
        "demo": ("1.0.7", "o/demo", [_advisory(**{field: value})])})
    assert rc == 0, text
    assert "GHSA-test" not in text


def test_a_package_whose_repository_cannot_be_resolved_never_reads_as_clean(
        monkeypatch):
    """解析不出倉庫 ＝ **沒有查**。結束碼 2，而且要在輸出裡點名是哪一個。"""
    rc, text = _run_repo_advisories(monkeypatch, {
        "demo": ("1.0.9", "o/demo", []),
        "mystery": ("2.0.0", None, None)})
    assert rc == 2, text
    assert "mystery" in text
    assert "不是乾淨" in text, "沒有說清楚這不等於安全"


def test_a_repository_query_that_failed_never_reads_as_clean(monkeypatch):
    """限速／沒網路 ＝ 沒有查到。

    這一格守的是 2026-09-17 那次實跑：未認證的限速砍掉 16 個裡的 6 個，而當時
    的寫法是「一個都沒查成才算查不成」，於是工具印出「打到的公告：0」並回
    **0**。修正已經在程式碼裡，但在此之前沒有任何測試執行過它——也就是可以被
    改回去而完全沒有症狀。
    """
    rc, text = _run_repo_advisories(monkeypatch, {
        "demo": ("1.0.9", "o/demo", []),
        "blocked": ("2.0.0", "o/blocked", None)})
    assert rc == 2, text
    assert "blocked" in text


def test_a_hit_outranks_an_unchecked_package(monkeypatch):
    """同時有命中與沒查到時回 1，不是 2。

    這是刻意的優先序：1 表示**有東西要修**，比「這次沒查全」更需要先被看到。
    寫在這裡是因為讀程式碼時很容易以為 2 比較嚴重。
    """
    rc, _text = _run_repo_advisories(monkeypatch, {
        "demo": ("1.0.7", "o/demo", [_advisory()]),
        "blocked": ("2.0.0", "o/blocked", None)})
    assert rc == 1


def test_a_range_nobody_can_parse_never_reads_as_clean(monkeypatch):
    """剖不開的區間既不是命中、也不是乾淨——結束碼要是 2。

    **這一格是 2026-09-20 修掉的一個真缺陷。** 在此之前 `unparseable` 只會被印
    出來，完全不影響結束碼，所以一份「有 9 筆需要人工判斷」的報告對只看結束碼
    的自動化來說跟「全部乾淨」一模一樣。而它不是邊角案例：2026-09-17 實測 78 個
    區間裡有 **9 個（12%）** 剖不開，也就是回 0 才是那天的實際結果。

    預設模式（`pip-audit`）早就是這樣判的——
    `test_unparseable_audit_output_never_reads_as_clean` 守著同一條線；這條路只是
    一直沒有人對齊。
    """
    rc, text = _run_repo_advisories(monkeypatch, {
        "demo": ("1.0.7", "o/demo",
                 [_advisory(rng="所有版本", patched="")])})
    assert rc == 2, text
    assert "需要人工判斷" in text
    assert "GHSA-test" in text
    assert "不是「安全」" in text


def test_the_token_value_never_appears_in_the_output(monkeypatch):
    """⚠️ 只說有沒有認證，不說是什麼。

    這條規則原本只寫在 `github_token()` 的 docstring 裡，而 docstring 不是執行
    力。這支工具的輸出是拿來貼給人看的，token 一旦印出去就等於帳號被接管，而且
    刪訊息救不回來。
    """
    rc, text = _run_repo_advisories(monkeypatch, {
        "demo": ("1.0.7", "o/demo", [_advisory()])})
    assert rc == 1
    assert _FAKE_TOKEN not in text, "token 的值被印出來了"
    assert "GitHub 認證：有" in text, "連有沒有認證都沒說——那會看不出限速風險"


def test_without_a_token_the_output_says_the_quota_will_not_be_enough(
        monkeypatch):
    """未認證是每小時 60 次，而一輪就要上百次。不講的話，使用者會把一份被限速
    砍掉一半的結果當成完整的結果。"""
    _rc, text = _run_repo_advisories(monkeypatch, {
        "demo": ("1.0.9", "o/demo", [])}, token=None)
    assert "GitHub 認證：沒有" in text


# ---------------------------------------------------------------------------
# PyPI 查詢的形狀，以及命令列那一層
#
# `source_repo()` 的 ⚠️ 寫著「一定要打帶版本號的那個端點」，理由是實測——不帶版本
# 的端點會把**每一個版本的每一個檔案**都列出來，aiohttp 那種大的 120 秒拉不完。
# 那是一條寫在 docstring 裡、沒有任何測試執行過的效能契約：改成不帶版本，結果
# 完全正確，只是這支工具從 90 秒變成跑不完，而跑不完的工具就是沒有人跑的工具。


def test_the_pypi_lookup_asks_for_the_exact_version_when_one_is_known(
        monkeypatch):
    """帶版本與不帶版本是兩個不同的端點，差的是兩個數量級的回應大小。"""
    seen = []
    monkeypatch.setattr(ad, "_get_json",
                        lambda url, *_a, **_k: seen.append(url) or None)
    name = "a-package-with-no-override"
    assert ad._normalise(name) not in ad._REPO_OVERRIDES
    ad.source_repo(name, "1.2.3")
    ad.source_repo(name, None)
    assert seen == [f"https://pypi.org/pypi/{name}/1.2.3/json",
                    f"https://pypi.org/pypi/{name}/json"]


@pytest.mark.parametrize("payload", [
    None,                        # 查不成
    [],                          # 形狀不對
    {"no_info": {}},             # 沒有 info
    {"info": "不是 dict"},       # info 的型別不對
])
def test_a_pypi_payload_without_an_info_block_is_no_repository(
        monkeypatch, payload):
    """解不出倉庫要回 None，讓呼叫端把它歸進「沒有查到」並讓結束碼變 2。"""
    monkeypatch.setattr(ad, "_get_json", lambda *_a, **_k: payload)
    assert ad.source_repo("demo", "1.0.0") is None


def test_the_legacy_home_page_field_is_a_last_resort_source_of_the_repository(
        monkeypatch):
    """舊套件沒有 `project_urls`，只有一個 `home_page`。

    不看它的話，那些套件會被歸進「解析不出倉庫」——也就是**永遠**沒查到，而那
    會讓結束碼永遠回不到 0。
    """
    monkeypatch.setattr(ad, "_get_json", lambda *_a, **_k: {
        "info": {"project_urls": None,
                 "home_page": "https://github.com/owner/thing.git"}})
    assert ad.source_repo("demo", "1.0.0") == "owner/thing"


# ---------------------------------------------------------------------------
# 命令列的分派：三種模式各回各的結束碼
#
# `main()` 是 `CLAUDE.md` 與這支的 docstring 都在教人用的那一層，而它的分派在
# 2026-09-20 之前只有預設模式被執行過。兩個旗標各自走進一支**會跑幾十秒網路**的
# 函式，所以「按錯支」不會當場現形——會現形的是輸出長得不太一樣，而那是人眼。

def _route(monkeypatch, argv):
    calls = []
    monkeypatch.setattr(ad, "report_fresh_clone_drift",
                        lambda: calls.append("fresh") or 7)
    monkeypatch.setattr(ad, "report_repository_advisories",
                        lambda: calls.append("repo") or 9)
    monkeypatch.setattr(ad, "run_pip_audit",
                        lambda: calls.append("default") or None)
    # 預設模式那條路上的真東西也換掉：這支測的是分派，不是相依樹。
    monkeypatch.setattr(ad, "declared_requirements", lambda: {"demo"})
    monkeypatch.setattr(ad, "dependency_closure", lambda _r: {"demo"})
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = ad.main(argv)
    return rc, calls


def test_the_repo_advisories_flag_routes_to_the_repository_sweep(monkeypatch):
    assert _route(monkeypatch, ["--repo-advisories"]) == (9, ["repo"])


def test_the_fresh_flag_routes_to_the_drift_report(monkeypatch):
    assert _route(monkeypatch, ["--fresh"]) == (7, ["fresh"])


def test_no_flag_runs_the_vulnerability_scan(monkeypatch):
    """反面：沒有旗標時兩支特殊模式都不准被呼叫到。

    少了這一格，「分派對不對」跟「三條路都跑了一遍」在結束碼上分不出來。
    """
    rc, calls = _route(monkeypatch, [])
    assert calls == ["default"]
    assert rc == 2, "`run_pip_audit` 回 None 是查不成，不得讀成安全"


def test_fresh_wins_when_both_flags_are_given(monkeypatch):
    """兩個旗標同時給的優先序是**固定**的，不是偶然。

    argparse 不會擋這種組合，所以順序由 `main()` 裡那兩個 `if` 決定；寫在這裡是
    因為把它們對調不會有任何症狀——兩種模式都會印出一份看起來很合理的報告。
    """
    assert _route(monkeypatch, ["--fresh", "--repo-advisories"]) == (
        7, ["fresh"])


def test_the_repo_advisories_help_describes_the_gate_that_actually_runs():
    """`--help` 是使用者唯一讀得到的規則之書，它不得描述一個比實際寬鬆的閘。

    2026-09-17 這支的範圍從直接相依拉到整個閉包、結束碼 2 的條件從「一個都沒查成」
    放寬成「有任何一個沒查到」，但說明文字兩件都沒改，而且**沒有任何症狀**——跟
    `_OWNER_ONLY_GROUPS` 那份少一個成員的清單、`_pid_alive` 那個「三份」的數字
    完全同一個形狀。
    """
    parser_help = _repo_advisories_help()
    assert "一個都沒查成" not in parser_help, (
        "說明文字還在講 2026-09-17 之前的結束碼規則（`checked == 0` 才算查不成）")
    assert "閉包" in parser_help, (
        "說明文字還在說範圍是直接相依，而實際上掃的是整個相依閉包")


def _repo_advisories_help() -> str:
    """把說明文字從**真的那個 parser** 裡取出來，並且把所有空白拿掉。

    兩件事各有理由。不去讀原始碼字串：那樣測的是檔案內容，不是使用者真的會看到
    的東西。拿掉空白：argparse 會用 `textwrap` 折行，而 `break_long_words` 預設是
    真的——中文沒有空格，一整串會被硬切在寬度上。不正規化的話，`"一個都沒查成"
    not in help` 可能只是因為那六個字剛好被切成兩行，於是這支守門會在**修好之前**
    就變綠。
    """
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            ad.main(["--help"])
        except SystemExit:
            pass
    return "".join(out.getvalue().split())
