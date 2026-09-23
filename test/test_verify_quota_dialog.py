"""`verify_quota_dialog.py` 的結論契約：**一定要印一行，而且只印一行。**

這支工具的存在理由是回答一個很貴的問題——「上線那段關閉對話框的 JS 會不會按到
會花錢（甚至會退訂）的按鈕」。呼叫端讀的是一行機器可讀結論
（`VERIFY-QUOTA-DIALOG: OK (N checks)` / `... FAIL (n/N)`）。

在 2026-09-12 之前，那條契約只在「一切正常」時成立：`__main__` 是
`sys.exit(vb._run_with_slot("quota-dialog", run))`，所以 Chrome 起不來、槽拿不到、
selenium 丟非預期例外時，呼叫端拿到的是 traceback ＋ 非零結束碼、**一行結論都沒有**。
於是「那段 JS 有問題」與「這台機器起不了瀏覽器」在輸出上完全一樣——而這支工具正是
為了區分前者而存在的。同一條契約在 `verify_browser.py` 已經是明文的硬性契約
（見它 `main()` 開頭那段註解），這裡只是補上。

這個模組在此之前**沒有任何測試檔 import 過**（1,083 行）。它驅動真瀏覽器，所以
情境那一半本來就測不了；但結論契約、前綴的單一來源、主控台硬化這三件事都是純
Python，測得了。本檔只釘這三件事，一行情境／JS 邏輯都不碰。
"""
from __future__ import annotations

import ast
import io
import contextlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "axiomatic"))

import verify_quota_dialog as vq  # noqa: E402

_VQ_PATH = REPO_ROOT / "axiomatic" / "verify_quota_dialog.py"


@pytest.fixture(autouse=True)
def _fresh_counters():
    """`FAILURES` / `CHECKS` 是模組層狀態，跑完要還原。

    不還原的話這一檔會影響同一輪裡任何其他人，而症狀是「換個順序就不重現」——
    本專案對這種形狀的判語見 `test_suite_safety` 的下半部。
    """
    saved_failures = list(vq.FAILURES)
    saved_checks = list(vq.CHECKS)
    vq.FAILURES.clear()
    vq.CHECKS[0] = 0
    try:
        yield
    finally:
        vq.FAILURES[:] = saved_failures
        vq.CHECKS[:] = saved_checks


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# 結論行：一定要有，而且只有一行
# ---------------------------------------------------------------------------

def test_an_unexpected_exception_still_prints_a_verdict(monkeypatch):
    """`run()` 炸掉時仍然要有結論行——否則呼叫端分不出是誰壞了。

    這是本檔存在的主因。斷言刻意包含「結束碼非 0」與「結論行在」**兩半**：
    只驗結束碼的話，原本那個會丟例外的寫法也會過（例外本來就是非零）。
    """
    def _boom(*_a, **_k):
        raise RuntimeError("chromedriver 起不來")

    monkeypatch.setattr(vq.vb, "_run_with_slot", _boom)
    rc, out = _capture(vq.main)
    assert rc != 0, "非預期例外卻回 0"
    verdicts = [ln for ln in out.splitlines()
                if ln.startswith(vq.RESULT_PREFIX)]
    assert len(verdicts) == 1, f"結論行有 {len(verdicts)} 行：{out!r}"
    assert "FAIL" in verdicts[0], verdicts[0]


def test_the_exception_verdict_uses_repr_not_str(monkeypatch):
    """例外文字走 `!r`。

    `str(OSError)` 會把完整的主機路徑寫進去，而這一行會被貼進工作紀錄／聊天平台。
    `repr` 的形式是 `OSError(2, '...')`，看得到型別又不會把路徑當成一句白話。
    本專案為這條記過一筆（`repr(OSError)` 藏路徑、`str` 露路徑）。
    """
    def _boom(*_a, **_k):
        raise ValueError("壞掉了")

    monkeypatch.setattr(vq.vb, "_run_with_slot", _boom)
    _rc, out = _capture(vq.main)
    assert "ValueError(" in out, f"沒有用 repr：{out!r}"


@pytest.mark.parametrize("code", [0, 1, 7])
def test_a_normal_run_passes_its_exit_code_through(monkeypatch, code):
    """正常路徑不碰結束碼——`run()` 說幾就是幾。

    參數化到 7 是刻意的：只測 0／1 的話，一個「非 0 一律回 1」的實作也會全綠。
    """
    monkeypatch.setattr(vq.vb, "_run_with_slot", lambda _label, _fn: code)
    rc, out = _capture(vq.main)
    assert rc == code
    assert vq.RESULT_PREFIX not in out, (
        "正常路徑不該自己補一行結論——那一行是 `run()` 印的，"
        f"補了就會變成兩行：{out!r}")


def test_the_verdict_prefix_has_exactly_one_source():
    """那個前綴是**契約**，只能有一個來源。

    它原本寫死在兩個 f-string 裡。寫兩次的東西遲早只改一次，而改錯的那一次沒有
    任何症狀——輸出照樣長得像結果行，只是呼叫端 grep 不到了。用 AST 掃字串常數，
    不要用 `in source`：本檔與那個模組的說明文字裡到處都是這個字串。
    """
    tree = ast.parse(_VQ_PATH.read_text(encoding="utf-8"), str(_VQ_PATH))
    spelled: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if node.value.strip() == vq.RESULT_PREFIX:
            spelled.append(node.lineno)
    assert len(spelled) == 1, (
        f"`{vq.RESULT_PREFIX}` 在 {len(spelled)} 個地方被寫成字串常數"
        f"（行 {spelled}）——它只能有一個來源，其餘一律用 `RESULT_PREFIX`。")


def test_the_docstring_advertises_the_same_prefix():
    """模組 docstring 教呼叫端 grep 什麼，那句話也必須跟常數一致。

    docstring 是人讀的那一份契約；它跟程式碼分岔的時候，照著它寫的呼叫端會安靜地
    grep 不到東西。（這裡刻意用 docstring 而不是整份原始碼——上一支已經釘住原始碼
    裡只能有一個字串常數，這一支問的是**說明**有沒有跟上。）
    """
    doc = ast.get_docstring(ast.parse(
        _VQ_PATH.read_text(encoding="utf-8"), str(_VQ_PATH)))
    assert doc and vq.RESULT_PREFIX in doc, (
        f"模組 docstring 沒有提到 `{vq.RESULT_PREFIX}`")


# ---------------------------------------------------------------------------
# 主控台硬化：跟 `verify_browser` 同一條，理由也同一條
# ---------------------------------------------------------------------------

def test_main_block_hardens_the_console_and_calls_main():
    """`__main__` 要先硬化主控台，再跑 `main()`。

    理由跟 `verify_browser` 完全一樣：這支的契約是一定要印出結論行，而一個 cp950
    編不出來的字元在管線上會讓行程**死掉**（不是印成亂碼），死在半路就是沒有結論。
    用 AST 判：解釋這條規則的註解與 docstring 一定會提到那個名字。
    """
    tree = ast.parse(_VQ_PATH.read_text(encoding="utf-8"), str(_VQ_PATH))
    blocks = [n for n in tree.body
              if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)]
    assert len(blocks) == 1, "找不到唯一的 `__main__` 區塊"
    called = [ast.unparse(sub.func) for sub in ast.walk(blocks[0])
              if isinstance(sub, ast.Call)]
    assert any(c.endswith("_harden_console") for c in called), (
        f"`__main__` 沒有硬化主控台：{called}")
    assert "main" in called, f"`__main__` 沒有呼叫 `main()`：{called}"

    others = [n for n in tree.body if n not in blocks]
    at_import = [sub for node in others for sub in ast.walk(node)
                 if isinstance(sub, ast.Call)
                 and ast.unparse(sub.func).endswith("_harden_console")]
    assert not at_import, (
        "import 的時候就硬化主控台了——那會在別人的行程裡留副作用，"
        "而整套測試就是 import 這個模組的那個行程。")


# ---------------------------------------------------------------------------
# `check()`：一份跑 100+ 次的紀錄，可讀性本身就是需求
# ---------------------------------------------------------------------------

def test_check_counts_every_call_and_records_only_failures():
    """計數涵蓋全部，`FAILURES` 只收失敗的。

    分母要對，否則 `OK (N checks)` 那個 N 會說謊——而那個數字正是「這一輪真的驗了
    多少件事」的唯一證據（本專案記過：一個沒有分母的數字藏得住短少）。
    """
    _rc, _out = _capture(lambda: (vq.check(True, "甲"),
                                  vq.check(False, "乙"),
                                  vq.check(True, "丙")))
    assert vq.CHECKS[0] == 3, vq.CHECKS
    assert len(vq.FAILURES) == 1 and "乙" in vq.FAILURES[0], vq.FAILURES


def test_detail_is_printed_only_when_the_check_fails():
    """`detail` 只在失敗時印。

    這不是排版偏好：一輪跑 100+ 個檢查，每個都把 `clicks=[...]` 印出來的話，真正
    失敗的那一行會被埋掉——**一個把有用訊息趕出記錄的診斷比不診斷還糟**（本專案對
    `_warn_once` 也是同一條判語）。
    """
    _rc, passed = _capture(lambda: vq.check(True, "甲", "細節甲"))
    assert "細節甲" not in passed, passed
    _rc, failed = _capture(lambda: vq.check(False, "乙", "細節乙"))
    assert "細節乙" in failed, failed
    assert "FAIL" in failed and "PASS" in passed
