"""`/dorossi` 的目錄輸入驗證——`_dorossi_unquote_dir` / `_dorossi_validate_dir` /
`_dorossi_looks_like_path`。

這三支決定「使用者貼進來的一串字要不要變成後端的工作目錄」。在 `full` 工具模式下
那個目錄就是後端可以無確認讀寫、可以在裡面執行 shell 的範圍，所以驗證錯的方向有
兩種，代價不對稱：

* 該過的沒過 → 使用者看到刻意泛用的「指定的目錄無法使用」，沒有任何線索；
* 不該過的過了 → 後端安靜地在另一個目錄裡幹活。

2026-09-05 量覆蓋率時發現這三支**一行都沒被跑過**，而且當場踩到第一種：三個入口
（`/dorossi allowdir add`、`/dorossi session new … cwd=`、`@bot /new <路徑>` 與
`dir=`）全部經過 `_dorossi_validate_dir`，但剝引號這件事只寫在**第一個入口自己那邊**。
檔案總管的「複製路徑」（Shift ＋右鍵）產出的字串自帶雙引號，於是同一串貼上的路徑在
`allowdir add` 能用、在另外兩個安靜失敗——兩邊都只回一句一模一樣的泛用訊息。修法是
把正規化收進 `_dorossi_validate_dir` 這個單一決策點，本檔釘的就是「收在那裡」而不是
「某個呼叫端記得做」。
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import dorossi_backend as db  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"


# --------------------------------------------------------------------------
# _dorossi_unquote_dir —— 正規化本身
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ('"D:\\Work\\Foo"', "D:\\Work\\Foo"),      # 檔案總管「複製路徑」的原樣輸出
    ("'D:/Work/Foo'", "D:/Work/Foo"),
    ('  "D:\\Work\\Foo"  ', "D:\\Work\\Foo"),  # 引號外面還有空白
    ('"  D:\\Work\\Foo  "', "D:\\Work\\Foo"),  # 引號裡面還有空白
    ("D:\\Work\\Foo", "D:\\Work\\Foo"),        # 沒引號時原樣通過
    ("", ""),
    ("   ", ""),
])
def test_a_pasted_path_loses_its_surrounding_quotes(raw, expected):
    assert db._dorossi_unquote_dir(raw) == expected


def test_none_normalises_to_the_empty_string_instead_of_raising():
    """呼叫端會傳 `None`（`cwd_part` 沒給時就是 None），不能靠 `TypeError` 走。"""
    assert db._dorossi_unquote_dir(None) == ""


@pytest.mark.parametrize("raw", [
    '"D:\\Work\\Foo',      # 只有前引號
    'D:\\Work\\Foo"',      # 只有後引號
    "'D:\\Work\\Foo\"",    # 頭尾引號不同種
    '"',                    # 單一個引號字元——不是一對
])
def test_an_unmatched_quote_is_left_alone(raw):
    """只脫**成對**的一層。落單的引號原樣留著，讓它自然驗不過，而不是猜使用者的意思
    去湊出一個「看起來像」的路徑——猜錯的下場是後端在另一個目錄裡動手。"""
    assert db._dorossi_unquote_dir(raw) == raw.strip()


def test_only_one_layer_of_quotes_comes_off():
    """repo 其他地方的 `.strip('"')` 會把頭尾**所有**引號字元一路刮掉。這裡刻意只
    脫一層：真的叫 `'foo'` 的目錄（POSIX 合法）不該被悄悄改成 `foo`——那是把「拒絕」
    換成「指到另一個存在的目錄」，比拒絕更糟。"""
    assert db._dorossi_unquote_dir('""foo""') == '"foo"'
    assert db._dorossi_unquote_dir("''foo''") == "'foo'"


# --------------------------------------------------------------------------
# 同一條判準的第二份實作：`_gui_control.unquote_path`
#
# 兩份不是重複貼上的意外，是模組邊界的結果——`_gui_control` 是 bot 端桌面自動化
# 的門面、`dorossi_backend` 是後端門面，兩者互不 import。但**判準只有一條**，而
# 判準錯掉的代價在兩邊一模一樣：把「拒絕」換成「指到另一個存在的目錄」。
#
# 在此之前，兩邊各有一組**平行但各自獨立**的測試（`test_gui_control.py:977` 起與
# 本檔上面那幾支），沒有任何東西比較過兩者。也就是說「只修好一邊」在測試上完全
# 看不出來：兩份測試都是綠的，而且各自都完整。下面兩支補的就是那一格。
# --------------------------------------------------------------------------

_UNQUOTE_CORPUS = (
    None, "", "   ",
    "D:\\Work\\Foo",                 # 沒引號
    '"D:\\Work\\Foo"',               # 檔案總管「複製路徑」的原樣輸出
    "'D:/Work/Foo'",
    '  "D:\\Work\\Foo"  ',           # 引號外面有空白
    '"  D:\\Work\\Foo  "',           # 引號裡面有空白
    '"D:\\Work\\Foo',                # 只有前引號
    'D:\\Work\\Foo"',                # 只有後引號
    "'D:\\Work\\Foo\"",              # 頭尾引號不同種
    '"', "'",                         # 單一個引號字元——不是一對
    '""foo""', "''foo''",             # 只准脫一層
    '"\'foo\'"',                      # 兩種引號套疊
    'D:\\a"b',                        # 引號在中間，不該被碰
    '""',                             # 剝完變成空字串
)


@pytest.mark.parametrize("raw", _UNQUOTE_CORPUS)
def test_both_unquote_implementations_answer_identically(raw):
    """`dorossi_backend._dorossi_unquote_dir` 與 `_gui_control.unquote_path` 是同
    一條判準的兩份實作，任何一格答不一樣都是漂移。

    為什麼不合併成一份：兩邊都是「門面」模組，而 `_gui_control` 的呼叫端是
    `/host` 那一族（限擁有者的主機檔案讀寫）、`dorossi_backend` 的是 `/dorossi`
    的工作目錄。要共用得再開一個 passive 共用模組，而這是一支十行、無相依的純
    函式——共用模組的維護成本高於重複本身。**所以留兩份，但用測試釘住它們一致**，
    這正是本專案對兩個 webrunner 變體採取的同一種做法。
    """
    import _gui_control as gui

    assert db._dorossi_unquote_dir(raw) == gui.unquote_path(raw), (
        f"兩份實作對 {raw!r} 給出不同答案——判準漂移了。"
        "改其中一份時另一份要一起改；兩邊各自的測試都不會發現這件事。")


def test_the_shared_corpus_actually_exercises_the_stripping():
    """正面對照組：上面那一組**必須**真的有東西被剝掉。

    兩份實作同時退化成 `raw.strip()`（或同時退化成原樣回傳）時，逐格比較會全部
    相等——「兩邊一致」在那種情況下是真的，只是一致地壞掉。所以另外釘住「這組語料
    對得起它的名字」：至少有一格的輸出與 `strip()` 不同，而且不變的那幾格也還在。
    """
    import _gui_control as gui

    stripped = [raw for raw in _UNQUOTE_CORPUS
                if raw is not None
                and db._dorossi_unquote_dir(raw) != raw.strip()]
    assert len(stripped) >= 6, (
        f"語料裡只有 {len(stripped)} 格真的被剝掉引號——比較等於在比兩個 "
        "`strip()`。")
    # 反面的那一半：不該動的也真的沒動。
    assert gui.unquote_path('D:\\a"b') == 'D:\\a"b'
    assert gui.unquote_path('"D:\\Work\\Foo') == '"D:\\Work\\Foo'


def test_each_unquote_docstring_points_at_the_other():
    """兩份實作要找得到彼此，否則「改一份時看另一份」只存在於口頭。

    這是**唯一**會在有人只修好一邊時提醒他的東西——上面那支比對測試會變紅，但那
    是事後；docstring 的交叉指引是事前。2026-09-10 之前只有 `_gui_control` 那份
    指過去，`dorossi_backend` 那份沒有回指，也就是說從 `/dorossi` 這一側看過去，
    另一份是隱形的。
    """
    import _gui_control as gui

    assert "unquote_path" in (db._dorossi_unquote_dir.__doc__ or ""), (
        "`_dorossi_unquote_dir` 的 docstring 沒有指向 "
        "`_gui_control.unquote_path`——從這一側看不到另一份實作。")
    assert "_dorossi_unquote_dir" in (gui.unquote_path.__doc__ or ""), (
        "`unquote_path` 的 docstring 沒有指向 "
        "`dorossi_backend._dorossi_unquote_dir`。")


# --------------------------------------------------------------------------
# _dorossi_validate_dir —— 三個入口共用的那一支
# --------------------------------------------------------------------------

def test_a_real_directory_resolves_to_an_absolute_path(tmp_path):
    got = db._dorossi_validate_dir(str(tmp_path))
    assert got is not None
    assert Path(got).is_dir()
    assert Path(got).is_absolute()


def test_a_quoted_real_directory_is_accepted_too(tmp_path):
    """**本檔的重點。** 修正前這一支會紅，而且只在三個入口的其中兩個紅——因為剝
    引號寫在 `/dorossi allowdir add` 自己那邊，沒有收進共用的驗證函式。"""
    assert db._dorossi_validate_dir(f'"{tmp_path}"') == db._dorossi_validate_dir(str(tmp_path))


def test_a_single_quoted_real_directory_is_accepted_too(tmp_path):
    assert db._dorossi_validate_dir(f"'{tmp_path}'") == db._dorossi_validate_dir(str(tmp_path))


@pytest.mark.parametrize("raw", [None, "", "   ", '""', "''"])
def test_nothing_useful_in_means_none_out(raw):
    """`'""'` 這一筆是剝完之後才變空的——空字串餵給 `Path()` 會得到 `.`（目前工作
    目錄），那是個**存在的目錄**，會通過驗證。使用者送出一對空引號，後端就把工作
    目錄設成 bot 行程當下的 cwd，而且訊息還說套用成功。"""
    assert db._dorossi_validate_dir(raw) is None


def test_a_file_is_not_a_directory(tmp_path):
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x", encoding="utf-8")
    assert db._dorossi_validate_dir(str(f)) is None


def test_a_missing_path_is_rejected(tmp_path):
    assert db._dorossi_validate_dir(str(tmp_path / "does_not_exist")) is None


def test_a_hostile_string_never_raises(tmp_path):
    """驗證是在使用者訊息的處理路徑上跑的；它自己拋例外會把一句「目錄無法使用」
    換成一次 handler 崩潰。NUL 位元組是 `os.stat` 會 `ValueError` 的經典輸入。"""
    for raw in ["a\x00b", "\x00", "*" * 5000, "\\\\?\\", "con", "~nosuchuser~/x"]:
        got = db._dorossi_validate_dir(raw)
        assert got is None or isinstance(got, str), raw


def test_expanduser_still_happens_after_unquoting(monkeypatch, tmp_path):
    """`~` 展開不能被正規化擋掉：剝引號後仍必須是 `~/...` 才會展成家目錄。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    assert db._dorossi_validate_dir('"~"') == str(home.resolve())


# --------------------------------------------------------------------------
# _dorossi_looks_like_path —— 只決定「要不要出聲」
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "D:\\Work\\Foo", "/tmp/foo", "~", "~/notes", "C:", "d:/x",
    '"D:\\Work\\Foo"', "'C:'",
])
def test_things_that_look_like_a_path(text):
    assert db._dorossi_looks_like_path(text) is True


@pytest.mark.parametrize("text", [
    None, "", "   ", "隨便幾個字", "new session for tuesday", "abc", "12:30",
])
def test_things_that_do_not_look_like_a_path(text):
    """`12:30` 這一筆是刻意的：磁碟機字首的判準要求第一個字元是**字母**，時間字串
    不該被當成路徑，否則 `/new 12:30 開始` 會多噴一句「目錄無法使用」。"""
    assert db._dorossi_looks_like_path(text) is False


def test_the_two_helpers_agree_on_a_quoted_drive_root():
    """兩支必須看同一套正規化過的字串。不一致的後果是靜默：`_dorossi_validate_dir`
    拒絕了，`_dorossi_looks_like_path` 卻說「這不像路徑」，於是連提示都不會出現。"""
    quoted = '"Z:\\definitely\\not\\here"'
    assert db._dorossi_validate_dir(quoted) is None
    assert db._dorossi_looks_like_path(quoted) is True


# --------------------------------------------------------------------------
# 形狀守門：正規化必須留在單一決策點
# --------------------------------------------------------------------------

def _calls_names(fn_node) -> set:
    return {
        n.func.id if isinstance(n.func, ast.Name) else
        (n.func.attr if isinstance(n.func, ast.Attribute) else "")
        for n in ast.walk(fn_node) if isinstance(n, ast.Call)
    }


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def test_both_helpers_route_through_the_one_normaliser():
    src = (PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for name in ("_dorossi_validate_dir", "_dorossi_looks_like_path"):
        fn = _func(tree, name)
        assert fn is not None, f"{name} 不見了——改名的話這支守門要跟著改"
        assert "_dorossi_unquote_dir" in _calls_names(fn), (
            f"{name} 沒有呼叫 `_dorossi_unquote_dir`。兩支必須看同一套正規化過的"
            "字串，否則同一串輸入會得到互相矛盾的答案（驗證過了但『不像路徑』，"
            "或反過來），而使用者那一側是靜默的。")


def test_no_call_site_re_strips_quotes_before_validating():
    """呼叫端不得自己再剝一次引號。這正是修正前的樣子：`/dorossi allowdir add` 在
    自己那邊 `.strip('"').strip("'")`，另外兩個入口沒有，於是同一串貼上的路徑一個
    能用、兩個安靜失敗。把它加回去不會讓任何既有測試變紅——所以由這一支釘。"""
    bot_src = (PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(bot_src)

    def _strips_a_quote(node) -> bool:
        """`<expr>.strip('"')` / `.strip("'")`——連鏈也算（原本那行是
        `.strip().strip('"').strip("'")`，最外層才是引號那一次）。"""
        while isinstance(node, ast.Call):
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "strip"
                    and len(node.args) == 1
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in ('"', "'")):
                return True
            node = node.func.value if isinstance(node.func, ast.Attribute) else None
        return False

    # 刻意只盯「路徑類變數」，不是把整份檔案的剝引號都禁掉。在別的情境剝引號可能是
    # 對的（例如使用者貼的指令引數），而一支會亂叫的守門最後會被人關掉。這裡釘的是
    # 那個 bug 真正的形狀：一個後面要送進 `_dorossi_validate_dir` 的路徑變數，在
    # 呼叫端就先被正規化了一次。
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not _strips_a_quote(node.value):
            continue
        for tgt in node.targets:
            name = tgt.id if isinstance(tgt, ast.Name) else ""
            if any(k in name.lower() for k in ("path", "dir", "cwd")):
                offenders.append((name, node.lineno))
    assert not offenders, (
        f"discord_bot.py {offenders}：路徑變數又在呼叫端被剝了引號。目錄字串的"
        "正規化只能有一份，放在 `dorossi_backend._dorossi_unquote_dir`；"
        "呼叫端各自剝一次就是先前那個 bug 的形狀——三個入口只有一個記得做。")


def _loose_quote_strip_lines(source: str) -> list[int]:
    """`<expr>.strip('"')` / `.strip("'")` 出現在哪幾行。純函式，好讓合成資料
    問得到它。

    **一定要走 AST，不能用子字串。** `dorossi_backend.py` 裡唯一提到這個寫法的
    兩個地方（`_dorossi_unquote_dir` 與 `find_codex_executable` 的 docstring）都是
    在**說明這條規則本身**，子字串掃描會正好紅在那兩行上——一支對著自己的說明文件
    亂叫的守門，最後會被人關掉。
    """
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "strip"):
            continue
        if (len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in ('"', "'")):
            offenders.append(node.lineno)
    return sorted(offenders)


def test_dorossi_backend_never_loosely_strips_quotes():
    """`dorossi_backend.py` **整份**都不得再出現寬鬆的剝引號。

    對帳的形狀本來是不對稱的，而缺的正好是出事的那一格：

    | 掃誰 | 由誰掃 | 判準 |
    |---|---|---|
    | `_gui_control.py` | `test_gui_control` | **整個模組**，任何 `.strip('"')` |
    | `discord_bot.py` | 上面那支 | 只盯名字像路徑的賦值目標 |
    | `dorossi_backend.py` | **沒有人** | — |

    2026-09-10 `find_codex_executable` 就是在這一格出事的：
    `override = os.environ.get("CODEX_CLI_PATH", "").strip().strip('"')`。
    `CODEX_CLI_PATH` 是從 shell 設的環境變數，`CODEX_CLI_PATH='…/codex.exe'` 是
    完全正常的寫法，而那行只脫得掉 `"`，於是單引號留在字串裡、`is_file()` 為
    False，這個被稱作 "the explicit operator override" 的東西**安靜地被忽略**。

    ⚠️ **這支刻意用寬的那個判準，不是照抄 `discord_bot.py` 那支窄的。** 那支只
    標名字含 `path` / `dir` / `cwd` 的賦值目標，而出事那行的目標叫 `override`——
    三個關鍵字一個都不沾。照抄窄的會得到一支**抓不到自己來由**的守門，比沒有更
    糟，因為它看起來像已經守住了。寬的在這裡可行的理由是實測的：本模組今天真正
    的呼叫是 0 個，而剝引號在這個模組裡從來就不該是對的做法——正規化只有
    `_dorossi_unquote_dir` 一份。
    """
    source = (PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8")
    offenders = _loose_quote_strip_lines(source)
    assert not offenders, (
        f"dorossi_backend.py:{offenders} 又出現了寬鬆的剝引號。這個模組裡目錄／"
        "路徑字串的正規化只能有一份，在 `_dorossi_unquote_dir`——它只脫**成對**的"
        "一層，因為 `'` 是合法的 POSIX 檔名字元，一路刮掉它等於指到另一個路徑。")


def test_the_loose_quote_strip_detector_actually_bites():
    """正面對照組：抽取器壞掉回空 list 時，「沒有違規」跟「掃不到東西」在輸出上
    一模一樣。上面那支斷言的是**空集合**，所以它自己完全無法區分這兩件事。

    近似但**不該**觸發的那幾種一併釘住——會亂叫的守門會被關掉，而這支要掃的是
    整個模組，亂叫的機會比窄判準大得多。
    """
    assert _loose_quote_strip_lines(
        'x = raw.strip(\'"\')\n') == [1], "連最直接的形狀都沒抓到"
    assert _loose_quote_strip_lines(
        'x = raw.strip("\'")\n') == [1], "單引號那一種漏了"
    assert _loose_quote_strip_lines(
        'x = raw.strip().strip(\'"\')\n') == [1], "鏈式呼叫的最外層漏了"

    for benign, why in (
            ('x = raw.strip()\n', "無引數的 `.strip()` 是正常的去空白"),
            ('x = raw.strip("\\n")\n', "剝換行不是剝引號"),
            ('x = raw.strip(chars)\n', "引數不是字面值，判不出來就不要猜"),
            ('"""docstring 提到 .strip(\'"\') 這個寫法"""\n',
             "說明規則的文件不是違規——這正是不能用子字串的理由"),
    ):
        assert _loose_quote_strip_lines(benign) == [], why


# ---------------------------------------------------------------------------
# `_dorossi_parse_reset`——路徑是從哪裡來的
#
# 上面那三支驗證的是「這個字串能不能當工作目錄」，而**這個字串本身**是這支切出來
# 的。切錯的兩種方向跟驗證那邊一樣不對稱，只是更隱蔽：切太少（該當重置的被當成
# 一般提問）使用者會看到後端答非所問，至少看得出來；切太多（把路徑從中間剖開）
# 產出的可能是一個**存在的**目錄，於是驗證會通過，後端安靜地在別的地方幹活。
#
# 2026-09-06 量覆蓋率時發現這支一行都沒被跑過，並當場找到後面那種：
# `dir=` 原本是用 `find("dir=")` 找**任何位置**的子字串，所以
# `/new D:\Work\dir=test` 會被剖成 cwd `D:\Work\`（存在！）＋ extra `test`。
# 現在 `dir=` 只在 token 邊界（字串開頭或前面有空白）上才算關鍵字。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("keyword, is_new", [
    ("/new", True), ("新對話", True),
    ("/reset", False), ("/clear", False), ("重置", False), ("清除對話", False),
])
def test_every_reset_keyword_is_classified(keyword, is_new):
    """六個關鍵字都要認得，而且要分得出「開新的」與「原地清空」。

    分錯的代價：`/reset` 被當成 `/new` 會多留一個空的工作階段槽；`/new` 被當成
    `/reset` 會把**現有那一段對話**的續接資訊清掉——後者救不回來。
    """
    assert db._dorossi_parse_reset(keyword) == (True, is_new, None, None)


def test_the_keyword_match_is_case_insensitive():
    assert db._dorossi_parse_reset("/NEW") == (True, True, None, None)


def test_the_new_keywords_are_a_subset_of_the_reset_keywords():
    """不然那個關鍵字永遠走不到 `is_new` 的判定——它連 reset 都不算。"""
    assert db.DOROSSI_NEW_KEYWORDS <= db.DOROSSI_RESET_KEYWORDS


def test_every_keyword_is_stored_lowercase_and_whitespace_free():
    """比對前只對關鍵字做 `lower()`，而切詞是 `split(None, 1)`。

    表裡放一個含大寫的關鍵字，它就**永遠比不中**；放一個含空白的，`split` 只會拿到
    前半段，同樣永遠比不中。兩種都不會有任何錯誤，只是那個關鍵字安靜地失效。
    """
    for kw in db.DOROSSI_RESET_KEYWORDS:
        assert kw == kw.lower(), f"{kw!r} 含大寫，永遠比不中"
        assert kw == kw.strip() and len(kw.split()) == 1, (
            f"{kw!r} 含空白，`split(None, 1)` 只會拿到前半段")


# --- 兩種範圍語法 -----------------------------------------------------------

def test_a_bare_path_becomes_the_working_directory():
    assert db._dorossi_parse_reset(r"/new D:\Work\Foo") == (
        True, True, r"D:\Work\Foo", None)


def test_dir_equals_becomes_the_extra_directory():
    assert db._dorossi_parse_reset(r"/new dir=D:\A") == (
        True, True, None, r"D:\A")


def test_both_syntaxes_can_be_combined():
    assert db._dorossi_parse_reset(r"/new D:\C dir=D:\E") == (
        True, True, r"D:\C", r"D:\E")


def test_the_dir_keyword_itself_is_case_insensitive():
    assert db._dorossi_parse_reset(r"/new DIR=D:\A")[3] == r"D:\A"


def test_an_empty_dir_value_is_none_not_an_empty_string():
    """空字串會一路流到驗證那邊，而 `Path("")` 是 `.`——一個**存在的**目錄。"""
    assert db._dorossi_parse_reset("/new dir=") == (True, True, None, None)


# --- 路徑一律原樣保留 -------------------------------------------------------

def test_paths_are_never_lowercased():
    """只有關鍵字才 `lower()`。路徑被 lower 之後在 POSIX 上就是另一個目錄。"""
    _, _, cwd, extra = db._dorossi_parse_reset(r"/new D:\CamelCase dir=D:\MiXeD")
    assert cwd == r"D:\CamelCase"
    assert extra == r"D:\MiXeD"


def test_a_path_may_contain_spaces():
    assert db._dorossi_parse_reset(r"/new  D:\My Folder\x  ")[2] == r"D:\My Folder\x"


@pytest.mark.parametrize("sep", ["\u3000", "\xa0"])
def test_full_width_and_non_breaking_spaces_separate_the_keyword(sep):
    """全形空格來自中文輸入法，NBSP 來自貼上。兩者 `str.isspace()` 都是 True，
    所以 `split(None, 1)` 吃得掉——這一支釘住的是「不要改成 `split(' ', 1)`」。"""
    assert db._dorossi_parse_reset(f"/new{sep}D:\\Foo")[2] == r"D:\Foo"


def test_quotes_are_left_for_the_validator_to_strip():
    """剝引號是 `_dorossi_unquote_dir` 的事，收在單一決策點（見本檔開頭）。"""
    assert db._dorossi_parse_reset('/new "D:\\Q"')[2] == '"D:\\Q"'


# --- 不是重置的東西不要被當成重置 -------------------------------------------

@pytest.mark.parametrize("prompt", [
    "怎麼用 dir= 這個參數？",      # 一般提問裡剛好有 `dir=`
    "", "   ",
    "newline",                    # 以 `new` 開頭但不是關鍵字
    "/new/x",                     # 沒有空白分隔，整串都是關鍵字位置
    r"dir=D:\A",                  # 只有範圍、沒有關鍵字
])
def test_a_normal_prompt_falls_through_to_the_backend(prompt):
    assert db._dorossi_parse_reset(prompt) == (False, False, None, None)


def test_a_path_containing_dir_equals_is_not_split_in_the_middle():
    r"""**這一支抓到的是實際的缺陷。**

    `dir=` 原本是用 `find("dir=")` 找任何位置的子字串，所以
    `/new D:\Work\dir=test` 會被剖成 cwd `D:\Work\` ＋ extra `test`。
    前者**是個存在的目錄**，所以驗證會通過——結果不是「被拒絕」而是「後端安靜地
    在上一層目錄幹活」，而在 `full` 工具模式下那個目錄就是它可以無確認讀寫、可以
    跑 shell 的範圍。現在 `dir=` 只在 token 邊界上才算關鍵字。
    """
    ok, new, cwd, extra = db._dorossi_parse_reset(r"/new D:\Work\dir=test")
    assert (ok, new) == (True, True)
    assert cwd == r"D:\Work\dir=test", "路徑被從中間剖開了"
    assert extra is None


# --------------------------------------------------------------------------
# _dorossi_cwd_is_managed —— 「這個 cwd 可以自動 mkdir 嗎」
# --------------------------------------------------------------------------
# 這道閘決定要不要替一個目錄 `mkdir(parents=True)`，而它的呼叫端把整段包在
# `except Exception: pass` 裡——判錯的話不會有錯誤訊息，只會多出一個目錄。
#
# 輸入來得到：`discord_bot._dorossi_resolve_cc_workdir` 對 `sess["cc_cwd"]`／
# `sess["cc_workdir"]` **刻意原值回傳**（理由在它的 docstring：狀態短鎖裡不碰檔案
# 系統、原字串是 `--resume` store 的鍵），而 `dorossi_session.json` 是放在專案根
# 目錄、人改得動的 JSON。存著的值是否仍然可用，改在 spawn 前由
# `_dorossi_require_workdir` 確認——見本檔下方「spawn 前的工作目錄檢查」那一族。
#
# ⚠️ 誠實記著：**今天沒有任何程式路徑會送出一個帶 `..` 的字串。** 這幾支是對著那份
# store 的縱深防禦，不是一個現行可利用的缺陷；spawn 前那一道也**刻意沒有**把 `..`
# 本身當成拒絕理由（照例子寫的守門——手改的人直接寫一個絕對路徑就好）。


@pytest.mark.parametrize("tail", [
    r"..\..\..\evil",
    r"..\evil",
    r"sessions\..\..\..\..\C_evil",
    r"..\..\..\..\..\..\..\..\evil",
])
def test_a_parent_escape_is_never_a_managed_cwd(tail):
    r"""帶 `..` 的路徑不得被判成受管目錄。

    **`.resolve()` 是這道閘的一部分，這支就是釘那一點。** `.parents` 做的是
    **字面上**的父目錄列舉，所以 `…\dorossi_workspace\..\..\..\evil` 的 parents
    裡真的含 `DOROSSI_CC_WORKDIR`——不 resolve 的版本會放行。實測四個這種輸入在
    不 resolve 時**全部**誤判放行，resolve 之後 0 個，而正當的輸入兩種寫法都通過。

    這條**不可能**由「該過的要過」那一側的案例守到：實測 `.resolve()` 對尾端分隔
    符、正斜線、`.` 區段、重複分隔符、大小寫變體一格都沒放寬——它在這裡是收緊的
    那一步，不是放寬的那一步。所以擋它的只有這幾個 must-block 案例。
    """
    escape = str(db.DOROSSI_CC_WORKDIR / tail)
    assert db._dorossi_cwd_is_managed(escape) is False, (
        f"{escape!r} 被判成受管目錄——`.resolve()` 不見了的話就是這個結果，"
        "然後呼叫端會在受管子樹**外面** mkdir 一個目錄，全程沒有錯誤訊息。")
    assert db.DOROSSI_CC_WORKDIR not in Path(escape).resolve().parents


def test_the_real_managed_dirs_are_still_managed():
    r"""正當的受管目錄一格都不能被擋掉——這一側的代價一樣是真的故障。

    答 False 只是「不自動建目錄」聽起來無害，實際不是：`create_subprocess_exec`
    的 `cwd=` 指到不存在的目錄會丟 `NotADirectoryError`（實測），而那一行在
    mkdir 的 `try` **外面**。所以一道過度收緊的閘會讓每一個全新 session 的第一輪
    永遠失敗，直到有人手動把目錄建出來。

    最後幾行釘的是 docstring 明寫的 fail-closed 選擇：判不出來時回 False。那個
    方向沒有別的東西在守——把它改成 True 的話，上面那幾個 must-block 案例全部
    照樣通過（實測變異存活），所以只能由這裡釘。
    """
    ok = [
        str(db.DOROSSI_CC_WORKDIR),                                   # 預設那條
        db.dorossi_session_workdir("400000000000000001", "s6"),       # per-session
        str(db.DOROSSI_CC_WORKDIR / "sessions" / "x_s1" / "deeper"),
        str(db.DOROSSI_CC_WORKDIR) + "\\",                            # 尾端分隔符
        str(db.DOROSSI_CC_WORKDIR).replace("\\", "/") + "/sessions/x_s1",
    ]
    bad = [p for p in ok if not db._dorossi_cwd_is_managed(p)]
    assert not bad, (
        f"{bad} 被判成不受管。這些目錄不自動建出來的話，後端 spawn 會丟 "
        "NotADirectoryError，而那一行不在 mkdir 的 try 裡。")
    # 使用者用 `/new <路徑>` 指定的外部目錄**必須**答 False：呼叫端已經確認它存在，
    # 不該對它硬 mkdir。
    assert db._dorossi_cwd_is_managed(r"C:\Windows\Temp") is False
    assert db._dorossi_cwd_is_managed("") is False
    # 判不出來 → False（fail-closed）。手改過的 store 放一個數字進 `cc_cwd`
    # 就是這個形狀。
    assert db._dorossi_cwd_is_managed(None) is False
    assert db._dorossi_cwd_is_managed(123) is False


def test_no_directory_is_created_outside_the_managed_subtree(tmp_path, monkeypatch):
    r"""閘 ＋ mkdir 合起來的行為：受管子樹外面不得長出任何目錄。

    上面兩支問的是述詞的答案，這一支問的是**後果**，而且刻意**不重寫一份**不
    resolve 的版本來對照——同一條判準的兩份實作正是這個 repo 一直在修的形狀。
    這裡直接照呼叫端的寫法（`if 受管: mkdir(parents=True)`）跑一遍，所以
    `.resolve()` 一拿掉，逃脫的那兩個目錄就會真的出現在沙箱裡而被抓到。

    在 `tmp_path` 底下而不是真的常數上做，理由有兩個：專案樹裡不得長出東西，
    而且兩個 `..` 案例都要落在**被觀察的那個根底下**，否則它們真的被建出來也看
    不到（`rglob` 看不到觀察根以外的東西）。

    **那個前提現在自己有樁。** 觀察根單一來源在 `watched`，而且動手之前先確認每個
    逃脫目標真的落在它底下。實測 2026-09-20：把觀察根從 `root` 收窄成 `managed`
    （「幹嘛看比受管子樹更大的範圍」是很自然的順手整理），逃脫目錄照樣被建出來、
    但一個都看不見，於是連同 `.resolve()` 被拿掉的真缺陷一起是綠的。這支測的是
    **範圍**而不是邏輯，所以刪掉判斷式不是唯一的殺法，收窄觀察範圍也是。
    """
    root = tmp_path / "root"
    managed = root / "a" / "b" / "workspace"
    monkeypatch.setattr(db, "DOROSSI_CC_WORKDIR", managed)
    # 先把上層鏈路建好，剩下能長出來的就只有 managed 自己與它底下的東西——
    # 否則 `parents=True` 正當建出來的上層目錄會被下面的判準誤報成「逃脫」。
    managed.parent.mkdir(parents=True)
    escapes = (str(managed) + r"\..\escaped_1_up",
               str(managed) + r"\..\..\escaped_2_up")
    watched = root          # ← 觀察根：只有它底下長出來的東西看得見
    for raw in escapes:
        assert watched in Path(raw).resolve().parents, (
            f"觀察根 {watched} 看不到逃脫目標 {raw}——它真的被建出來也不會被抓到，"
            "這支測試會變成永遠綠的。")
    before = set(watched.rglob("*"))
    for raw in (str(managed),
                str(managed / "sessions" / "u_s1"),
                *escapes,
                str(root / "external_user_dir")):
        cwd_path = Path(raw)
        if db._dorossi_cwd_is_managed(cwd_path):      # ← 呼叫端原樣的形狀
            cwd_path.mkdir(parents=True, exist_ok=True)
    created = set(watched.rglob("*")) - before
    assert created, (
        "觀察根底下一個目錄都沒長出來——`rglob` 或 mkdir 那段沒在做事，"
        "下面那條「外面沒東西」的判準就是拿空集合在問。")
    outside = sorted(str(p) for p in created
                     if p != managed and managed not in p.parents)
    assert not outside, (
        f"受管子樹外面長出了 {outside}。呼叫端把 mkdir 包在 "
        "`except Exception: pass` 裡，所以這件事在正式執行時是完全安靜的。")
    assert (managed / "sessions" / "u_s1").is_dir(), (
        "per-session 子目錄沒有被建出來——後端 spawn 會丟 NotADirectoryError。")
    assert not (root / "external_user_dir").exists(), (
        "使用者指定的外部目錄被自動建出來了；那條路徑由呼叫端負責確認存在。")


# --------------------------------------------------------------------------
# spawn 前的工作目錄檢查：存下來的目錄可能早就不在了
# --------------------------------------------------------------------------
# 上面三支問的是述詞與「照呼叫端形狀」的替身；下面這幾支直接跑**真的**兩個叫用函式，
# 只把 `create_subprocess_exec` 換成記錄器，所以不會起任何行程。這樣才看得到兩件述詞
# 測試看不到的事：真的呼叫點有沒有接上 `_dorossi_require_workdir`，以及真的呼叫點的
# mkdir 閘有沒有被換成無條件（把它換成 `if True:`，上面三支全是綠的——它們從不呼叫它）。
#
# 正反兩側都要有：收緊的那一步（新的檢查）只殺得死 must-block 案例；「檢查要排在
# mkdir 後面」「交給子行程的要是原字串」這兩件只有 must-allow 案例殺得死。


class _SpawnReached(Exception):
    """哨符：已經走到 spawn，別再往下（不起真的行程）。"""


def _arm_spawn(monkeypatch) -> list:
    """把兩個後端的執行檔與 spawn 換成替身；回傳一個 list，記錄每次 spawn 拿到的 `cwd=`。"""
    seen: list = []
    monkeypatch.setattr(db._shutil, "which", lambda _n: r"C:\fake\claude.exe")
    monkeypatch.setattr(db, "find_codex_executable", lambda: r"C:\fake\codex.exe")

    async def _fake_exec(*_args, **kwargs):
        seen.append(kwargs.get("cwd"))
        raise _SpawnReached()

    monkeypatch.setattr(db.asyncio, "create_subprocess_exec", _fake_exec)
    return seen


def _invoke(which: str, workdir) -> None:
    fn = (db._dorossi_via_claude_code if which == "claude"
          else db._dorossi_via_codex)
    asyncio.run(fn("問題", None, workdir=workdir))


_INVOKERS = ("claude", "codex")


@pytest.mark.parametrize("which", _INVOKERS)
def test_a_stored_directory_that_no_longer_exists_is_not_used(
        monkeypatch, tmp_path, which):
    """**must-block。** 存的時候在、現在不在（外接磁碟拔掉、專案搬走）：不 spawn、
    不替它建目錄，丟具名例外讓 bot 講對原因。

    修正前這裡是子行程丟的 `NotADirectoryError`，被判成「CLI 無法使用或未認證」，
    對外說「請稍後再試」——一個永久的狀況被說成暫時的。
    """
    seen = _arm_spawn(monkeypatch)
    gone = tmp_path / "moved_away"
    with pytest.raises(db._DorossiWorkdirError):
        _invoke(which, str(gone))
    assert seen == [], f"目錄不在了還是 spawn 了：cwd={seen}"
    assert not gone.exists(), "替一個使用者指定的外部目錄自動建了目錄"


@pytest.mark.parametrize("which", _INVOKERS)
def test_a_parent_escape_in_the_store_is_neither_created_nor_used(
        monkeypatch, tmp_path, which):
    r"""**must-block。** 手改過的 store 帶著 `..` 逃出受管子樹：不得在外面建出目錄、
    也不得 spawn。

    這一支跑的是**真的**呼叫點，所以它殺得死上面三支殺不死的變異：把呼叫點的
    `if _dorossi_cwd_is_managed(...)` 換成 `if True:`。那樣的話逃脫目錄會被建出來、
    驗證跟著通過、子行程就在受管子樹外面啟動。
    """
    managed = tmp_path / "root" / "a" / "b" / "workspace"
    managed.parent.mkdir(parents=True)
    monkeypatch.setattr(db, "DOROSSI_CC_WORKDIR", managed)
    seen = _arm_spawn(monkeypatch)
    escape = str(managed) + r"\..\..\escaped"
    with pytest.raises(db._DorossiWorkdirError):
        _invoke(which, escape)
    assert seen == [], f"逃脫路徑被拿去 spawn 了：cwd={seen}"
    assert not (tmp_path / "root" / "a" / "escaped").exists(), (
        "受管子樹外面長出了目錄")


@pytest.mark.parametrize("which", _INVOKERS)
def test_a_non_string_stored_directory_raises_the_named_error(
        monkeypatch, which):
    """**must-block。** 手改的 store 放了一個數字：要走同一條具名例外，不是在
    `_dorossi_validate_dir` 的 `.strip()` 上丟 `AttributeError`（那會被 bot 當成沒
    分類的失敗，自走迴圈還會白白重試三輪）。"""
    seen = _arm_spawn(monkeypatch)
    with pytest.raises(db._DorossiWorkdirError):
        _invoke(which, 123)
    assert seen == []


@pytest.mark.parametrize("which", _INVOKERS)
def test_an_existing_stored_directory_reaches_the_child_unchanged(
        monkeypatch, tmp_path, which):
    """**must-ALLOW。** 還在的外部目錄照用，而且交給子行程的是**存著的那一串原文**，
    不是驗證函式 resolve 過的版本。

    故意帶一個尾端分隔符：`_dorossi_validate_dir` 會把它 resolve 掉（實測），所以
    「把 cwd 換成驗證結果」的寫法在這裡會被抓到。後端的 `--resume` store 以工作目錄
    字串為鍵，換一串等於讓那段對話找不到自己的紀錄。
    """
    seen = _arm_spawn(monkeypatch)
    ext = tmp_path / "project"
    ext.mkdir()
    stored = str(ext) + os.sep
    assert db._dorossi_validate_dir(stored) != stored, (
        "前提失效：這個輸入 resolve 之後應該跟原文不同，否則這支分不出兩種寫法")
    with pytest.raises(_SpawnReached):
        _invoke(which, stored)
    assert seen == [stored], f"交給子行程的 cwd 不是存著的原文：{seen}"


@pytest.mark.parametrize("which", _INVOKERS)
def test_a_managed_directory_is_created_before_it_is_checked(
        monkeypatch, tmp_path, which):
    """**must-ALLOW。** 全新工作階段的受管目錄還不存在：要先建出來、再檢查、再 spawn。

    這一支殺的是兩個 must-block 案例殺不死的錯：檢查排到 mkdir **前面**，以及把呼叫點
    的受管 mkdir 拿掉——兩者都會讓每個全新工作階段的第一輪被拒。
    """
    managed = tmp_path / "workspace"
    monkeypatch.setattr(db, "DOROSSI_CC_WORKDIR", managed)
    seen = _arm_spawn(monkeypatch)
    fresh = db.dorossi_session_workdir("u1", "s1")
    assert not Path(fresh).exists()
    with pytest.raises(_SpawnReached):
        _invoke(which, fresh)
    assert seen == [fresh], seen
    assert Path(fresh).is_dir(), "受管目錄沒有被建出來"


def test_the_workdir_error_is_fatal_so_a_loop_stops_at_once():
    """具名例外刻意繼承 `NotADirectoryError`：自走迴圈的泛用 handler 靠
    `dorossi_error_is_fatal` 決定要不要重試，改成繼承 `RuntimeError` 的話，一個永久
    的狀況會被重試三輪才停。"""
    assert db.dorossi_error_is_fatal(
        db._DorossiWorkdirError("working directory is not a usable directory")) is True


def test_the_dir_keyword_still_matches_after_any_kind_of_space():
    """邊界條件收緊之後，既有的寫法一個都不能壞。"""
    for sep in (" ", "  ", "\u3000", "\t"):
        assert db._dorossi_parse_reset(f"/new{sep}dir=D:\\A")[3] == r"D:\A"
