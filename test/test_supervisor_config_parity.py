"""兩個 webrunner 監督者共用 `bot_config.json` 的同一段設定——那句話要對得上。

`start_webrunner.py` 與 `discord_bot._watch_for_fallback` 是同一套重啟策略的兩份
實作，而它們「保持同步」的機制只有一個：**兩邊都從 `bot_config.json` →
`webrunner_supervisor` 讀同一組鍵**。兩個檔案各自寫了一句話宣告這件事：

* `start_webrunner.py` 的模組 docstring：「All N parameters come from …」，上面
  接著一份逐條說明每個參數在做什麼的清單；
* `discord_bot.py` 那一組常數上面的註解：「All N come from …」。

2026-09-21 量下來，**兩個 N 都是錯的**——bot 那句寫「four」，底下接著**七**行；
啟動器那句寫「five」，它自己讀**六**個。兩個都沒有任何東西在看，而這正是本 repo
反覆修的形狀（`_OWNER_ONLY_GROUPS` 的成員數、`_pid_alive` 的副本數、
`webrunner.pid` 的讀取端數）。

失效形態是**主動誤導**：讀到「All four」的人會以為另外三個常數是從別的地方來的，
於是去找一個不存在的來源；或者加第八個參數時不覺得那句話跟自己有關。

這支守四件事，每一件都是**推導 vs 宣稱**，不是人工謄寫：

1. 兩句話裡的數字，各自等於它介紹的那份清單的長度；
2. 兩邊讀到的鍵都真的存在於 `_bot_config` 的預設表（打錯字＝載入時 KeyError，
   但啟動器那一側要到 `main()` 才炸，而那時使用者已經在等批次起來了）；
3. 啟動器 docstring 的參數清單與它實際讀的鍵**雙向**對得上；
4. 兩邊都不准對這些鍵給預設值——一邊有 fallback、另一邊沒有，就是「改一次設定、
   兩個監督者一起生效」這個承諾失效的起點，而且沒有任何症狀。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PACKAGE_ROOT.parent
sys.path.insert(0, str(PACKAGE_ROOT))

import _bot_config  # noqa: E402

BOT_SOURCE = PACKAGE_ROOT / "discord_bot.py"
LAUNCHER_SOURCE = REPO_ROOT / "start_webrunner.py"
SECTION = "webrunner_supervisor"

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
          "twelve": 12}

# 「All <數字> [parameters ]come from」——兩個檔案的句子形狀一樣，所以一支就夠。
_CLAIM = re.compile(r"All\s+(\w+)\s+(?:of\s+them\s+|parameters\s+)?come\s+from")


def _claimed_count(text: str, *, where: str) -> int:
    matches = _CLAIM.findall(text)
    assert len(matches) == 1, (
        f"{where} 裡找到 {len(matches)} 句「All … come from」，預期剛好一句。"
        "句子改寫過的話，這支測試的錨點要跟著改——別把它刪掉。")
    raw = matches[0]
    value = _WORDS.get(raw.lower()) or (int(raw) if raw.isdigit() else None)
    assert value is not None, f"{where} 的「All {raw} …」數不出是多少"
    return value


def _bot_constant_keys() -> list[str]:
    """bot 那組常數實際讀了哪些 `webrunner_supervisor` 的鍵（依原始碼順序）。

    刻意用 AST 而不是正則：這個檔案裡「webrunner_supervisor」這個字串在註解與
    docstring 裡出現好幾次，字串比對會把它們一起算進來。
    """
    tree = ast.parse(BOT_SOURCE.read_text(encoding="utf-8"), str(BOT_SOURCE))
    keys: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        key = _supervisor_key(node.value)
        if key is not None:
            keys.append(key)
    return keys


def _supervisor_key(node: ast.AST) -> str | None:
    """`X["webrunner_supervisor"]["<鍵>"]` → `<鍵>`，其餘回 None。"""
    if not isinstance(node, ast.Subscript):
        return None
    if not isinstance(node.slice, ast.Constant) or not isinstance(node.slice.value, str):
        return None
    inner = node.value
    if not isinstance(inner, ast.Subscript):
        return None
    if not (isinstance(inner.slice, ast.Constant)
            and inner.slice.value == SECTION):
        return None
    return node.slice.value


def _bot_documented_run() -> list[str]:
    """那句註解**底下緊接著**那一串賦值——它介紹的就是這一串，到空行或別的註解為止。"""
    lines = BOT_SOURCE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if _CLAIM.search(line))
    pattern = re.compile(
        r'^\w+ = BOT_CONFIG\["' + SECTION + r'"\]\["([A-Za-z0-9_]+)"\]')
    run: list[str] = []
    for line in lines[start + 1:]:
        match = pattern.match(line)
        if not match:
            break
        run.append(match.group(1))
    return run


def _launcher_keys() -> list[str]:
    """啟動器實際從 `sup[...]` 讀了哪些鍵（依原始碼順序、去重）。"""
    tree = ast.parse(LAUNCHER_SOURCE.read_text(encoding="utf-8"),
                     str(LAUNCHER_SOURCE))
    seen: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name) and node.value.id == "sup"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and node.slice.value not in seen):
            seen.append(node.slice.value)
    return seen


def _launcher_documented_keys() -> set[str]:
    """啟動器 docstring 的參數清單裡，反引號包起來的那些鍵名。"""
    tree = ast.parse(LAUNCHER_SOURCE.read_text(encoding="utf-8"),
                     str(LAUNCHER_SOURCE))
    doc = ast.get_docstring(tree) or ""
    head = doc.split("All ")[0]          # 只看宣告句**之前**那份逐條說明
    defaults = set(_bot_config.load_bot_config()[SECTION])
    # 反引號裡不一定只有一個識別字——`alias_for < rapid_fail_threshold_sec` 這種
    # 寫法很自然，而要求整段等於鍵名會把它判成「沒寫說明」（第一版就是這樣，量出
    # 一筆假的）。改成在每一段反引號內容裡找識別字。
    found: set[str] = set()
    for span in re.findall(r"`([^`]*)`", head):
        found |= {word for word in re.findall(r"[A-Za-z0-9_]+", span)
                  if word in defaults}
    return found


# ---------------------------------------------------------------------------
# 1. 兩句話各自數得對
# ---------------------------------------------------------------------------

def test_the_bot_comment_counts_the_constants_it_introduces():
    """`discord_bot.py` 的「All N come from …」要等於底下那一串賦值的長度。

    2026-09-21 量到的是 four vs 七行。那三個「多出來」的不是別的來源——
    `rapid_fail_threshold_sec`、`rapid_fail_giveup_count`、
    `zero_progress_giveup_count` 也都來自同一段設定，只是句子沒跟上。
    """
    run = _bot_documented_run()
    assert len(run) >= 4, (
        f"只認出 {len(run)} 行賦值（{run}）——賦值的寫法變了的話這支會變成空的比較，"
        "看起來跟通過一模一樣。")
    claimed = _claimed_count(BOT_SOURCE.read_text(encoding="utf-8"),
                             where="discord_bot.py")
    assert claimed == len(run), (
        f"`discord_bot.py` 那句話說 All {claimed}，底下接著 {len(run)} 行："
        f"{run}。改數字，或把那一行搬出這一串。")


def test_the_launcher_docstring_counts_the_parameters_it_reads():
    """`start_webrunner.py` 的「All N parameters come from …」要等於它真的讀幾個。"""
    keys = _launcher_keys()
    assert len(keys) >= 4, f"只認出 {len(keys)} 個鍵（{keys}），推導看起來失效了"
    claimed = _claimed_count(LAUNCHER_SOURCE.read_text(encoding="utf-8"),
                             where="start_webrunner.py")
    assert claimed == len(keys), (
        f"`start_webrunner.py` 的 docstring 說 All {claimed} parameters，"
        f"`main()` 實際讀了 {len(keys)} 個：{keys}。")


# ---------------------------------------------------------------------------
# 2. 讀到的鍵都要真的存在
# ---------------------------------------------------------------------------

def test_every_key_either_side_reads_really_exists():
    """打錯一個鍵名＝`KeyError`，而兩邊炸的時機差很多。

    bot 那一側是 import 時就炸（整支 bot 起不來，至少很明顯）；啟動器那一側要到
    `main()` 才炸，而那時使用者已經下了 `/run`、正在等批次起來。
    """
    defaults = set(_bot_config.load_bot_config()[SECTION])
    assert len(defaults) >= 6, f"預設表只剩 {len(defaults)} 個鍵，語料看起來不對"
    for label, keys in (("discord_bot.py", _bot_constant_keys()),
                        ("start_webrunner.py", _launcher_keys())):
        unknown = sorted(set(keys) - defaults)
        assert not unknown, (
            f"{label} 讀了 `{SECTION}` 裡不存在的鍵：{unknown}。"
            f"目前有的是 {sorted(defaults)}。")


def test_no_supervisor_key_is_dead_config():
    """預設表裡的每一個鍵都要有人讀。

    沒有人讀的設定鍵是**騙人的旋鈕**：使用者改了、存檔了、重啟了，然後什麼都沒
    發生，而且沒有任何錯誤訊息。
    """
    defaults = set(_bot_config.load_bot_config()[SECTION])
    read = set(_bot_constant_keys()) | set(_launcher_keys())
    unread = sorted(defaults - read)
    assert not unread, (
        f"`{SECTION}` 有沒人讀的鍵：{unread}。把它接起來，或從預設表拿掉。")


# ---------------------------------------------------------------------------
# 3. 啟動器的逐條說明與它實際讀的鍵，雙向
# ---------------------------------------------------------------------------

def test_the_launcher_documents_exactly_the_parameters_it_reads():
    """docstring 上面那份逐條說明，與 `main()` 讀的鍵要雙向對得上。

    單向會留一半永遠綠：只查「說明裡的都存在」，新增一個參數而不寫說明永遠不會紅。
    """
    documented = _launcher_documented_keys()
    actual = set(_launcher_keys())
    assert documented, "docstring 裡一個參數名都沒認出來——錨點失效了"
    missing = sorted(actual - documented)
    extra = sorted(documented - actual)
    assert not missing, (
        f"`start_webrunner.py` 讀了這些參數卻沒有在 docstring 說明它們在做什麼："
        f"{missing}")
    assert not extra, (
        f"`start_webrunner.py` 的 docstring 說明了這些參數，但 `main()` 沒有讀："
        f"{extra}")


# ---------------------------------------------------------------------------
# 4. 兩邊都不准自己給預設值
# ---------------------------------------------------------------------------

def test_neither_side_supplies_its_own_default():
    """`.get(key, 預設值)` 會讓「改一次設定、兩邊一起生效」這個承諾安靜失效。

    一邊有 fallback、另一邊沒有的時候，設定檔缺那個鍵就變成**兩個監督者用不同的
    參數在跑**，而兩邊的 log 看起來都正常。預設值只能有一份，在 `_bot_config`。
    """
    offenders: list[str] = []
    for label, path in (("discord_bot.py", BOT_SOURCE),
                        ("start_webrunner.py", LAUNCHER_SOURCE)):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and len(node.args) == 2):
                continue
            receiver = node.func.value
            if (_supervisor_key(receiver) is not None
                    or (isinstance(receiver, ast.Subscript)
                        and isinstance(receiver.slice, ast.Constant)
                        and receiver.slice.value == SECTION)
                    or (isinstance(receiver, ast.Name)
                        and receiver.id == "sup")):
                offenders.append(f"{label}:{node.lineno}")
    assert not offenders, (
        f"這些地方對監督者參數自帶預設值：{offenders}。預設值的唯一來源是 "
        "`_bot_config`，否則兩個監督者會在設定缺鍵時各跑各的。")


# ---------------------------------------------------------------------------
# 對照組——樹是乾淨的，所以上面每一段「回報問題」的程式碼在真實資料上都不會執行
# ---------------------------------------------------------------------------

_CLAIM_CONTROLS = [
    ("All four come from x", 4),
    ("All 7 of them come from x", 7),
    ("All twelve parameters come from x", 12),
]


@pytest.mark.parametrize("text,expected", _CLAIM_CONTROLS,
                         ids=[row[0] for row in _CLAIM_CONTROLS])
def test_the_claim_parser_reads_words_and_digits(text, expected):
    """句子裡的數字可能是英文字也可能是阿拉伯數字，兩種都要認得。"""
    assert _claimed_count(text, where="控制組") == expected


def test_the_claim_parser_refuses_an_ambiguous_source():
    """兩句「All … come from」＝錨點壞了，不可以默默挑第一句。"""
    with pytest.raises(AssertionError, match="預期剛好一句"):
        _claimed_count("All four come from a\nAll five come from b",
                       where="控制組")


def test_the_subscript_matcher_only_accepts_this_section():
    """`_supervisor_key` 不得把別的設定區塊也算進來。"""
    good = ast.parse('X["webrunner_supervisor"]["healthy_threshold_sec"]',
                     mode="eval").body
    other = ast.parse('X["dorossi"]["healthy_threshold_sec"]', mode="eval").body
    shallow = ast.parse('X["healthy_threshold_sec"]', mode="eval").body
    assert _supervisor_key(good) == "healthy_threshold_sec"
    assert _supervisor_key(other) is None
    assert _supervisor_key(shallow) is None
