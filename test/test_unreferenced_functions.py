"""定義了卻沒有任何地方引用的模組層函式，必須是**列冊**的。

死碼不是潔癖問題：它看起來是「系統會做的事」，而讀的人不會知道它永遠不會發生。
本專案已經為此付過兩次代價——

* 2026-09-05：三個具名錯誤 handler 被排到泛用處理後面，整段變成死碼，
  用量上限／暫時性過載／輸出靜默三條路全部退化，**而且沒有任何訊號**；
* 2026-09-07：`_cleanup_chrome_locks` 曾被判「刻意保留」，後來發現保留的理由只問了
  「有沒有人呼叫」，沒問「接上去會不會有效」——答案是不會（它清 `.chrome_profile/`，
  Chrome 開的是 `.chrome_profile_snap/`），於是刪掉。

早就有一份候選名單，但守門一直沒有擴到全套件，理由是「一開始就帶
七筆例外的守門沒有說服力」。2026-09-08 實測全套件（1,318 個模組層函式）之後，
**沒有裝飾器的候選只有 4 個**，不是七筆以上——所以這支守門現在成立了。

## 判準與刻意的取捨

* **只看模組層函式。** 方法與巢狀函式的引用方式太多（`self.x()`、事件迴圈、
  duck typing），掃它們會製造大量假警報，而會亂叫的守門是會被關掉的守門
  （`test_language.py` 記過同一個教訓）。
* **帶裝飾器的一律跳過。** 指令樹（`@tree.command`）、`@pytest.fixture`、
  `@app_commands.describe` 這些全是**註冊式叫用**——名字確實只出現一次，但它們每天
  都在跑。實測 266 個「只出現一次」的候選裡有 262 個是這一類。
* **字串字面值算引用；註解與 docstring 不算。** 指令表、`getattr(mod, "name")`、
  字串轉發都只會讓名字出現在字串裡，不算就會大量誤報；而註解與 docstring 是
  **在講**那個函式、不是在用它，算進去會讓「把死碼的名字寫進散文」直接讓守門對那個
  名字失明（2026-09-08 實測到這個漏報並修掉，理由與踩過的坑寫在 `_countable_text`）。
* **測試檔與 `verify_*` 腳本不列入被檢查的「定義」**，但它們的內容仍然算進引用——
  只被測試呼叫的函式**不算**死碼，那是刻意的：那多半是還沒接上的功能或防線。
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys
import tokenize
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
PROJECT_ROOT = PACKAGE_ROOT.parent

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# 允許存在的未引用函式。**每一筆都要有理由**，而且理由要說明「為什麼留著比刪掉好」。
# 加一筆進來之前先問 `_cleanup_chrome_locks` 那個問題：**接上去會不會有效？**
# 答案是「不會」的話，留著只是把一個壞掉的東西偽裝成備援。
_ALLOWED_UNREFERENCED = {
    "_grab_virtual_logical": (
        "一行包裝，但 docstring 記著一個**仍然成立**的座標坑（截圖是實體像素、"
        "點選是邏輯像素，本機差 116 px）。刪掉會連那段知識一起沒。"),
    "record_elapsed": (
        "巨集錄製的狀態存取器，就在有人用的 `record_active()` 旁邊。"
        "成對的存取器刪掉一半比留著更難讀。"),
    # 2026-09-08 `is_logged_in` 已刪除（原本在這裡，理由寫著「留著但已知有缺陷」）。
    # 那正是這份清單不該有的形狀：**一個接上去就會壞的備援，比沒有備援更糟。**
    # 判準跑完之後答案是「不會有效」——它掃 `innerText` 的四個站方字面沒有任何
    # 實機證據還活著，而唯一該用它的問題（現在登入了沒有）`login_if_needed`
    # 早就用結構性訊號（`/login` 會不會轉址）答得更好。知識搬進兩個變體的
    # `login_if_needed` docstring 了。
    #
    # **順帶記一個本守門的已知漏報，因為它正是上面那個搬家造成的**（2026-09-08
    # 變異實測）：那兩份 docstring 裡寫著 `is_logged_in` 這個名字，而本守門
    # **連字串與註解一起算**（`_identifier_counts` 的刻意取捨，寧可漏報不要誤報），
    # 所以哪天有人把 `is_logged_in` 原封不動加回來，這支**不會**再抓到它。
    # 一般化：**把死碼的名字寫進任何 `.py` 的散文裡，就等於讓這支守門對那個名字
    # 失明**（`.md` 不在掃描範圍，寫在 `.md` 沒有這個副作用）。這裡仍然選擇留著
    # 名字——`webrunner-expert.md` 明文要求「換掉脆弱訊號時要 grep 舊 helper 的
    # 名字」，名字不可 grep 的知識等於沒寫。記在這裡是為了讓下一個人知道**這一個
    # 名字已經沒有保護**，而不是誤以為有。
    "probe_smtc_media_async": (
        "`probe_signals_async` 內聯了它的內容，所以是重複的便利包裝；"
        "它與 `probe_smtc_raw_async` 是「過濾後／原始」的對稱配對，"
        "docstring 記著瀏覽器分頁的過濾規則。"),
}


def _project_py_files() -> list[Path]:
    # 測試裡的引用也算數，而測試 2026-09-22 起住在 repo 根目錄的 `test/`（本檔所在的
    # 目錄），不在套件的 glob 裡——漏了這一項，只被測試引用的函式全都會變成「沒人用」。
    files = (list(PACKAGE_ROOT.glob("*.py")) + list(Path(__file__).resolve().parent.glob("*.py"))
             + list(PROJECT_ROOT.glob("*.py")))
    return [p for p in files if "legacy" not in p.parts]


def _identifier_counts(paths) -> Counter:
    """全專案每個識別字出現幾次（含字串與註解內容）。

    一次掃完建表，而不是對每個函式名各跑一次正規表示式——後者是
    O(函式數 × 檔案大小)，在這個 repo 上是 1,318 × 約 3 MB。

    **本檔自己要排除掉。** `_ALLOWED_UNREFERENCED` 裡寫著那四個名字，算進去的話它們
    每一個都變成「出現兩次」＝有被引用，於是 `test_the_allow_list_does_not_go_stale`
    會判定整份清單都過期了——實測就是這樣紅的。
    **一支會列出它所守護對象的守門，會把那些對象變成「看起來有人用」。**
    這跟 `test_language.py` / `test_suite_safety.py` 必須用 AST 而不是子字串是同一個
    形狀：解釋規則的文字本身會符合規則。
    """
    counts: Counter = Counter()
    for path in paths:
        if path.name == Path(__file__).name:
            continue
        try:
            counts.update(_IDENT.findall(_countable_text(path)))
        except (OSError, UnicodeDecodeError):
            continue
    return counts


def _countable_text(path: Path) -> str:
    """檔案內容，但**拿掉註解與 docstring**。

    這條界線是刻意畫在「散文」與「字面值」之間，不是「程式碼」與「非程式碼」：

    * **字串字面值要算**——指令表、`getattr(mod, "name")`、字串轉發都只會讓函式名
      出現在字串裡，不算就會產生大量假警報，而會亂叫的守門是會被關掉的守門。
    * **註解與 docstring 不算**——那是**在講**那個函式，不是在用它。

    這不是理論上的區別，是實測出來的：2026-09-08 刪掉 `is_logged_in` 之後，把它的
    名字寫進 `login_if_needed` 的 docstring（為了保住那段知識），結果**把同名的函式
    重新加回來也不會讓這支守門變紅**——散文把它偽裝成「有人用」。
    （`.md` 檔本來就不掃，所以寫在架構文件裡一直是安全的。）

    **註解一定要用 `tokenize` 拿掉，不可以用 `line.split("#", 1)[0]`。**
    第一版就是那樣寫的，然後它把這一行

        lines = ["### `%s`" % _signature(command), "", description, ""]

    從第一個 `#` 切掉——那個 `#` 在**字串字面值裡面**（Markdown 的標題記號），於是
    `_signature` 這個**真的呼叫**消失了，守門把一個活著的函式報成死碼。我差一點就
    把它刪掉，是 `gen_command_docs.py --check` 當場 `NameError` 才擋下來。
    這正是本專案反覆記的那條規則的又一個變體：**要分辨語法就用真的解析器，
    不要用字串切割**——而這一次踩到它的是「守門自己」。

    解析失敗時退回原文（寧可少報也不要因為一個檔案就整支掛掉）。
    """
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    dropped = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            dropped.update(range(first.lineno, (first.end_lineno or
                                                first.lineno) + 1))
    kept = [line for lineno, line in enumerate(text.splitlines(), 1)
            if lineno not in dropped]
    body_text = "\n".join(kept)
    try:
        comments = []
        for tok in tokenize.generate_tokens(io.StringIO(body_text).readline):
            if tok.type == tokenize.COMMENT:
                comments.append(tok.string)
        for comment in comments:
            body_text = body_text.replace(comment, "", 1)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 拿掉 docstring 之後的殘骸不一定還是合法的 token 串（例如函式本體只剩
        # docstring）。退回「保留註解」——那只會讓守門更寬鬆，不會誤報。
        pass
    return body_text


def _module_level_functions(paths):
    """`(檔名, 行號, 名稱)`——只取沒有裝飾器的模組層函式。"""
    out = []
    for path in paths:
        if path.name.startswith(("test_", "verify_", "conftest")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.decorator_list or node.name.startswith("__"):
                continue
            out.append((path.name, node.lineno, node.name))
    return out


def _unreferenced():
    paths = _project_py_files()
    counts = _identifier_counts(paths)
    return [(f, ln, name) for f, ln, name in _module_level_functions(paths)
            if counts[name] <= 1]


def test_no_unlisted_function_is_left_unreferenced():
    """沒有列冊的未引用函式一律不准存在。

    新增一個「先寫好、之後再接上」的函式時這支會紅——那是刻意的。要嘛當場接上，
    要嘛寫進 `_ALLOWED_UNREFERENCED` 並說明為什麼留著比刪掉好。
    """
    surprises = [(f, ln, name) for f, ln, name in _unreferenced()
                 if name not in _ALLOWED_UNREFERENCED]
    assert not surprises, (
        "這些模組層函式全專案只出現一次（＝只有定義，沒有任何引用）：\n"
        + "\n".join(f"  {f}:{ln}  {name}()" for f, ln, name in surprises)
        + "\n\n死碼看起來是「系統會做的事」，而讀的人不會知道它永遠不會發生。"
          "請接上去、刪掉、或寫進 `_ALLOWED_UNREFERENCED` 並附理由。"
          "\n判準（`_cleanup_chrome_locks` 那次換來的）：不要只問「有沒有人呼叫」，"
          "要問**「接上去會不會有效」**——答案是「不會」的話，留著只是把一個壞掉的"
          "東西偽裝成備援。")


def test_the_allow_list_does_not_go_stale():
    """允許清單上的函式一旦真的被接上去，就要從清單移除。

    少了這一條，清單只會單向長大：接上去的照樣留在裡面，下一個人會以為它還是死碼。
    這跟 `test_exception_handlers` 的 `_ALLOWED_SILENT_SCANS` 是同一個形狀。
    """
    still_dead = {name for _f, _ln, name in _unreferenced()}
    stale = sorted(set(_ALLOWED_UNREFERENCED) - still_dead)
    assert not stale, (
        f"這些已經不是未引用函式了，請從 `_ALLOWED_UNREFERENCED` 移除：{stale}")


def test_every_allowance_has_a_reason():
    """每一筆例外都要有理由，而且理由不能是敷衍的一句話。"""
    for name, reason in _ALLOWED_UNREFERENCED.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 20, (
            f"`{name}` 的理由太短——寫清楚為什麼留著比刪掉好，"
            "不然下一個人只能重做一次同樣的判斷")


def test_the_scanner_still_finds_things():
    """掃描器自己的下限釘樁。

    如果 `_module_level_functions` 或 `_identifier_counts` 因為某次重構而回空，
    上面兩支會**全部變綠**——一個掃不到任何東西的守門看起來跟「一切都好」一模一樣。
    本專案在事件型別的三邊對拉上記過同一個 canary 的必要性。
    """
    paths = _project_py_files()
    assert len(paths) > 40, f"專案 .py 檔只掃到 {len(paths)} 個"
    funcs = _module_level_functions(paths)
    assert len(funcs) > 500, f"模組層函式只掃到 {len(funcs)} 個"
    counts = _identifier_counts(paths)
    # 隨便挑一個一定到處都在用的名字，確認計數真的有在數。
    assert counts["print"] > 100, "識別字計數看起來沒在運作"
