"""耐久知識文件不得引用**已經不存在**的符號。

守的是 `CLAUDE.md`——它的「Durable knowledge goes into the tree」那一節
把它們指定成耐久知識的存放處（判準：「下次有人（含冷啟動的 subagent）改到同一塊
程式碼，會不會踩到同樣的坑？」）。它們沒有任何守門，而失效形態是**主動誤導**：
一個被改名或刪掉的函式留在文件裡當成現況在描述，讀的人會照著去找。

2026-09-08 第一次跑就抓到兩筆真的，其中一筆有實害：
`discord-bot-expert.md` 一邊說「`_slash_in_channel_only(interaction)` 是每個頻道
限定斜線指令都會走的單一咽喉點」，另一邊在同一份檔案裡說「`_slash_in_channel_only`
已經沒了，不要寫回來」——**同一份文件對整個 bot 最敏感的那道閘自相矛盾**，而先讀到
前者的人會去找一個不存在的咽喉點、甚至把它重新實作出來。另一筆是
`_reload_bot_config()`（真正在改 `ALERT_USER_ID` 的是 `cmd_config_reload()`）。

**判準刻意很窄**：只認文件裡寫成「呼叫形式」的**底線開頭**名字——也就是
`` `_name(` ``。理由是這個專案已經學過「會亂叫的守門會被人關掉」：

* 放寬到所有反引號識別字 → 2,504 個候選、135 個「找不到」，而其中絕大多數是環境
  變數（`ANTHROPIC_API_KEY`）、設定鍵（`api_contact`）、外部 JSON 欄位
  （`cache_creation_input_tokens`）、第三方屬性（`add_reaction`）。訊噪比低到沒用。
* 收成 `` `_name(` `` → 136 個候選、3 個命中，其中 2 個是真的。加底線前綴又能把
  builtins（`len(`／`print(`）與第三方 API（`ChromeService(`／`CreateFile(`）整批
  排除，不必維護一張排除清單。

漏掉的部分是刻意的：文件寫成 `` `_name` ``（沒有括號）不會被檢查。那類多半在講
「一個常數」或「一個概念」，硬要檢查就會把設定鍵與環境變數全部掃進來。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
PROJECT_ROOT = PACKAGE_ROOT.parent
# 測試 2026-09-22 起住在 repo 根目錄的 `test/`（本檔所在的目錄）。搬家前它們在
# `PACKAGE_ROOT` 的 glob 裡，所以下面每一處掃描都把這個目錄一起列進來，範圍照舊。
TEST_ROOT = Path(__file__).resolve().parent

# 文件裡「寫成呼叫」的底線開頭名字。
_DOCUMENTED_CALL = re.compile(r"`(_[A-Za-z0-9_]+)\(")
# 只認**不帶前綴**的私有名字：反引號後面第一個字元就是 `_`。所以
# `` `Process._raise_if_pid_reused` `` 這種**指名擁有者**的寫法天生不在掃描範圍內
# ——那不是漏洞，那是給第三方私有符號的正確寫法。這一道 2026-09-09 一天內在
# `BaseSubprocessTransport._wait`（CPython）與 `Process._raise_if_pid_reused`
# （psutil）上各叫了一次，兩次的正解都是**把擁有者寫出來**，不是加進下面的墓碑
# 清單——墓碑是給「本專案曾經有、現在刻意沒有」的名字，混進外部符號會讓它失去意義。
# 順帶一提，加上擁有者本來就是比較好的文件：讀的人不必猜那是我們的還是別人的。

# 明知已經不存在、而且**文件正是在說它不存在**的名字。每一筆都要寫理由。
#
# 目前是空的，而那是正常狀態，不是壞掉：只有當某份耐久知識文件刻意寫著「這東西
# 已經移除、不要寫回來」時，那個名字才該進來。空集合讓下面兩支反向對帳暫時無事可
# 做，但它們一加進第一筆就立刻有牙齒——刪掉它們才是把規則拿掉。
_DELIBERATE_TOMBSTONES: set[str] = set()


def _docs() -> list[Path]:
    """耐久知識文件：規則的正本。

    目前只有 `CLAUDE.md`——使用者文件（`README.md`、`docs/`、`commands/`）刻意
    不在內：它們講的是指令怎麼用，不寫私有符號名，實測一個候選都抽不到，收進來
    只會把下限稀釋掉。
    """
    return [p for p in (PROJECT_ROOT / "CLAUDE.md",) if p.is_file()]


def _defined_names() -> set[str]:
    """專案自己定義得出來的名字（函式／類別／賦值目標／屬性）。

    刻意把**屬性**也算進去：文件常寫 `` `self._last_error` `` 那種，而屬性只在
    賦值時出現、不會有 `def`。寧可寬鬆，這支的目的是抓「完全不存在」的名字。
    """
    names: set[str] = set()
    sources = (list(PACKAGE_ROOT.glob("*.py")) + list(TEST_ROOT.glob("*.py"))
               + list(PROJECT_ROOT.glob("*.py")))
    for path in sources:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        names.add(path.stem)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
    return names


def test_the_durable_docs_do_not_cite_symbols_that_are_gone():
    """文件寫成 `_name(` 的每一個名字都要真的存在（或列在墓碑清單裡）。"""
    known = _defined_names()
    # 正面對照組：抽取器壞掉的話下面會是「零個候選、全部通過」，跟真的乾淨
    # 長得一模一樣。先確認真的掃到東西。
    candidates: set[str] = set()
    for doc in _docs():
        candidates |= set(_DOCUMENTED_CALL.findall(
            doc.read_text(encoding="utf-8")))
    assert len(candidates) > 5, (
        f"只抽到 {len(candidates)} 個候選，抽取器多半壞了——"
        "文件格式變了嗎？（零候選會讓這支測試變成永遠通過。）"
        "下限是按語料大小訂的：語料只有 `CLAUDE.md` 一份，實測 8 個候選。")

    offenders: list[str] = []
    for doc in _docs():
        text = doc.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for name in _DOCUMENTED_CALL.findall(line):
                if name in known or name in _DELIBERATE_TOMBSTONES:
                    continue
                offenders.append(f"{doc.name}:{lineno} `{name}()`")
    assert not offenders, (
        "耐久知識文件引用了不存在的符號：\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n改法二選一：改成現在真正的名字；或者，如果那一段的重點正是"
          "「這東西已經移除、不要寫回來」，把名字加進 `_DELIBERATE_TOMBSTONES`"
          "並寫下理由。**不要**只是把反引號拿掉——那樣下一個人會以為它還在。")


def test_every_tombstone_is_really_gone():
    """反方向：墓碑清單裡的名字**不可以**又出現在程式碼裡。

    少了這一條，清單就變成一張「永久豁免」——某天有人真的把
    `_slash_in_channel_only` 寫回來（文件明文禁止的事），上面那支還是綠的，
    而文件會從「正確地描述一個已移除的東西」變成「錯誤地描述一個現存的東西」。
    """
    known = _defined_names()
    resurrected = sorted(n for n in _DELIBERATE_TOMBSTONES if n in known)
    assert not resurrected, (
        f"這些名字被列為「已移除」，但程式碼裡又有了：{resurrected}。"
        "要嘛那次復活是錯的（文件說過不要寫回來），要嘛文件該更新、"
        "同時把它從 `_DELIBERATE_TOMBSTONES` 移掉。")


def test_the_tombstone_list_does_not_rot():
    """墓碑清單裡的每一筆都必須真的還被某份文件提到。

    否則它會累積成一張沒人看得懂的名字清單，而清單本身又會讓
    `test_the_durable_docs_do_not_cite_symbols_that_are_gone` 對那些名字閉嘴。
    """
    mentioned: set[str] = set()
    for doc in _docs():
        text = doc.read_text(encoding="utf-8")
        mentioned |= {n for n in _DELIBERATE_TOMBSTONES if n in text}
    stale = sorted(_DELIBERATE_TOMBSTONES - mentioned)
    assert not stale, (
        f"`_DELIBERATE_TOMBSTONES` 裡這幾筆已經沒有任何文件提到了：{stale}。"
        "文件不再講它們就把它們從清單移掉——留著只是讓清單看起來很有道理。")


# ---------------------------------------------------------------------------
# 第二組：「這條有 `test_x` 守著」——那 `test_x` 最好真的存在（2026-09-09）
# ---------------------------------------------------------------------------
# 上面那組守的是文件引用不存在的**實作**符號。這一組守的是同一個失效形態、但後果
# 更尖銳的一種：**註解宣稱某條規則有守門，而那支守門叫不出來。**
#
# 差別在讀的人會怎麼反應。看到一個找不到的實作名字，人會去查、會懷疑自己。看到
# 「`test_x` 釘住這條」，人會**停止查證**——那正是這句話存在的目的。所以引用錯的
# 測試名字比引用錯的函式名字更容易讓一條規則從此沒人守：它不只是誤導，它還讓人
# 放心。
#
# 第一次跑抓到三筆，三筆的失效方式各不相同：
#
# * `_batch_config.py` 寫「`test_batch_config` 另外釘住這張表要涵蓋每一個鍵」——
#   **那支測試從來不存在**（真正在做這件事的是
#   `test_config_numbers.py::test_every_default_key_goes_through_a_coercer`）。
# * `test_undecodable_files.py` 用「下面的 `test_the_remaining_sites_do_not_grow`」
#   指路，而那支測試在 2026-09-07 升級成零容忍時改名成
#   `test_no_read_text_site_is_left_unguarded`——**改名時只改了定義處**。
# * `discord_bot.py` 把名字折行時在折行處多打了一個底線，於是
#   `..._appended__synchronously` 這個名字 grep 不到任何東西。折行本身就是這個
#   形態的溫床，所以掃描**會把相鄰兩行接起來再看一次**。
#
# 判準同樣刻意很窄（理由與上面那組一樣：會亂叫的守門會被人關掉）：
#
# * 只認**閉合反引號**裡、以 `test_` 開頭的識別字。實測 115 個候選、4 個命中，
#   其中 3 個是真的、1 個是刻意的墓碑。放寬到「所有反引號識別字」在上面那組已經
#   量過是 2,504 個候選、訊噪比低到沒用。
# * **有日期的工作日誌刻意不掃。** 一行紀錄寫著
#   當天刪掉了哪支測試，那是**歷史**，不是指路，改掉反而是竄改紀錄。它也會引用
#   別的 repo 的測試名（`test_usb_acl_prompt` 之類），那些在這裡本來就不存在。
#   實測把它掃進來會多出 5 筆全是這兩類的雜訊。
_CITED_TEST = re.compile(r"`(test_[A-Za-z0-9_]+)`")

# 明知已經不存在、而且引用它的那一句**正是在講它不存在**的測試名字。
_DELIBERATE_TEST_TOMBSTONES = {
    # `test_undecodable_files.py` 的 `test_no_read_text_site_is_left_unguarded`
    # docstring 裡那句「這支原本叫 …」。**同一個名字在同一個檔案裡有兩種用法**：
    # 指路的那一句是錯的（已改），講歷史的這一句是對的。墓碑清單的代價就是它同時
    # 讓兩種用法閉嘴——所以加進來之前要先確認指路的那些都改掉了。
    "test_the_remaining_sites_do_not_grow",
    # 2026-09-10 被 `test_only_one_copy_of_the_visibility_predicate_exists` 取代。
    # 引用它的兩處（新測試的 docstring、`webrunner-expert.md`）整句的重點正是
    # 「舊的那支守錯了題目所以拿掉了」——舊守門守的是「抄本要跟本尊同步」，而
    # **抄本本身才是缺陷**。把名字拿掉那兩段就沒頭沒尾，讀的人會以為只是改名。
    "test_the_toast_scan_keeps_its_own_copy_in_step",
    # 2026-09-19 擁有者「現在等待太短了，任務一直被殺掉」之後**反轉**的兩支：原本釘的是
    # 「硬上限／自走沉默砍掉時，就算已經收到成功的 result 也照樣丟例外」，現在的規則相反
    # （收下答案），改名為 `test_a_hard_kill_after_a_successful_result_keeps_the_answer`
    # 與 `test_a_loop_silence_kill_after_a_successful_result_keeps_the_answer`。引用它們的
    # 三處（新測試的 docstring 兩處、`discord-bot-expert.md` 一處）整句的重點正是「這條
    # 規則是刻意反轉的」——拿掉舊名，讀的人會以為只是改名，而不知道舊規則曾經是反過來的。
    # 加進來之前確認過：三處都是講歷史，沒有任何一處在指路說「由它守著」。
    "test_the_hard_ceiling_still_fails_even_after_a_successful_result",
    "test_output_silence_still_fails_even_after_a_successful_result",
    # 2026-09-19 改名成 `test_the_verifier_takes_its_timeouts_only_from_the_reconciled_table`。
    # 舊規則「驗證腳本一律不准指定逾時」在 bot 自己放寬字典呼叫的逾時之後，就等於「比 bot
    # 窄」；新名字的 docstring 引用舊名字是為了講這段歷史，不是指路。
    "test_the_verifier_never_gives_an_endpoint_its_own_timeout",
}


def _test_citation_sources() -> list[Path]:
    """耐久知識文件 ＋ 專案自己的 `.py`。

    兩個排除，理由不一樣：

    * 有日期的工作日誌，見上面那段。
    * **這個檔案自己。** 一支「抓引用錯的測試名」的守門，它的說明必然要舉出那些
      引用錯的名字當例子，於是它會抓到自己。這不是規避，是這類自我描述守門的固有
      性質（`test_language` 對「反引號裡的詞」開同樣的例外，理由一樣）。代價是本
      檔案裡真正的過期引用不會被抓到——本檔案很短，人看得完。
    """
    here = Path(__file__).resolve()
    return _docs() + [p for p in (*sorted(PACKAGE_ROOT.glob("*.py")),
                                  *sorted(TEST_ROOT.glob("*.py")))
                      if p.resolve() != here]


def _defined_test_names() -> set[str]:
    """真的存在的 `def test_*`，外加測試模組自己的檔名（`test_x.py` → `test_x`）。"""
    names: set[str] = set()
    for path in sorted(TEST_ROOT.glob("test_*.py")):
        names.add(path.stem)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("test_")):
                names.add(node.name)
    return names


def _scan_citations(lines: list[str]) -> list[tuple[int, str]]:
    """回 `[(行號, 被引用的測試名), …]`。純函式，好讓自我檢查餵得進合成樣本。

    每一行看兩次：原樣，以及**接上下一行**（去掉下一行的縮排與註解記號）。折行是
    這類錯誤的主要來源——`discord_bot.py` 那筆就是折行處多打一個底線造成的，只看
    單行永遠抓不到。接行只做一層：真實的名字不會長到跨三行。
    """
    found: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        here = set(_CITED_TEST.findall(line))
        there = set(_CITED_TEST.findall(following))
        for name in sorted(here):
            found.append((index + 1, name))
        joined = line + re.sub(r"^\s*(?:#\s*)?", "", following)
        # **只收「接起來才出現」的那些**。整個名字本來就在其中一行時，接行版會
        # 再看到它一次，於是同一筆引用會被回報兩次（一次在 N、一次在 N-1），而
        # 那個 N-1 是錯的行號。第一版就是這樣，訊息裡每一筆都出現兩行。
        for name in sorted(set(_CITED_TEST.findall(joined)) - here - there):
            found.append((index + 1, name))
    return found


def _cited_tests(path: Path) -> list[tuple[int, str]]:
    return _scan_citations(
        path.read_text(encoding="utf-8", errors="replace").splitlines())


def _unknown_citations(named_lines, known: set[str]) -> list[str]:
    """`[(檔名, 該檔的行), …]` → 找不到對應測試的那些引用。

    抽成純函式**只是為了讓它餵得進合成樣本**（見
    `test_the_offender_check_actually_flags_a_bogus_name`）。沒有那支自我檢查的話，
    「把 `offenders` 直接設成空清單」這種改動會全綠通過——因為守門現在本來就沒有
    東西要抓，「守門被關掉」與「守門沒事做」在輸出上完全一樣。
    """
    return sorted({
        f"{name}:{lineno} `{cited}`"
        for name, lines in named_lines
        for lineno, cited in _scan_citations(lines)
        if cited not in known and cited not in _DELIBERATE_TEST_TOMBSTONES
    })


def test_the_offender_check_actually_flags_a_bogus_name():
    """合成樣本：一個叫不出來的名字必須被列出來，而已知的名字必須不被列出來。"""
    known = {"test_that_really_exists"}
    flagged = _unknown_citations(
        [("sample.py", ["由 `test_that_really_exists` 釘住",
                        "由 `test_that_never_existed` 釘住"])], known)
    assert flagged == ["sample.py:2 `test_that_never_existed`"], flagged
    # 墓碑要真的讓它閉嘴，否則清單只是裝飾。
    tombstoned = next(iter(_DELIBERATE_TEST_TOMBSTONES))
    assert _unknown_citations(
        [("sample.py", [f"由 `{tombstoned}` 釘住"])], known) == []


def test_the_scanner_sees_a_name_that_was_split_across_two_lines():
    """**合成樣本的自我檢查：接行那一半必須真的在做事。**

    沒有這一支，「只看單行」的版本會全綠——因為現存的折行引用都已經修好了，掃描器
    退化成單行版不會有任何症狀。這正是這個 repo 反覆吃虧的形狀：一個守門的一半悄悄
    失效，而失效跟「沒有東西可抓」長得一模一樣。
    """
    sample = [
        "# 這條由 `test_something_that_is_written_",
        "# across_two_lines` 釘住。",
    ]
    names = {name for _, name in _scan_citations(sample)}
    assert "test_something_that_is_written_across_two_lines" in names, (
        f"折行的引用沒被接起來（抓到 {names}）——掃描器退化成單行版了。")
    # 反面：單行的正常引用當然也要抓得到，否則上面那條可能只是碰巧。
    assert {name for _, name in _scan_citations(["由 `test_plain_one` 釘住"])} \
        == {"test_plain_one"}


def test_no_comment_claims_a_guard_that_does_not_exist():
    """被反引號引用的每一個 `test_*` 都要真的存在（或列在墓碑清單裡）。"""
    known = _defined_test_names()
    # 正面對照組：抽取器壞掉會變成「零候選、全部通過」，跟真的乾淨長得一模一樣。
    candidates = {name for path in _test_citation_sources()
                  for _, name in _cited_tests(path)}
    assert len(candidates) > 40, (
        f"只抽到 {len(candidates)} 個被引用的測試名，抽取器多半壞了。"
        "（零候選會讓這支測試變成永遠通過。）")
    assert len(known) > 500, (
        f"只找到 {len(known)} 個 `def test_*`，AST 那一半多半壞了——"
        "那會讓每一個引用都變成「找不到」，噪音蓋過訊號。")

    offenders = _unknown_citations(
        [(path.name,
          path.read_text(encoding="utf-8", errors="replace").splitlines())
         for path in _test_citation_sources()], known)
    assert not offenders, (
        "這些地方宣稱有一支守門，但那個名字叫不出任何東西：\n  "
        + "\n  ".join(offenders)
        + "\n改法：改成現在真正的測試名（`def test_…` 或 `test_….py`）；"
          "如果那一句的重點正是「這支已經刪掉／改名了」，加進 "
          "`_DELIBERATE_TEST_TOMBSTONES` 並寫下理由。**不要**只把反引號拿掉——"
          "那樣下一個人仍然會以為有東西在守。")


def test_every_test_tombstone_is_really_gone():
    """反方向：墓碑清單裡的測試名**不可以**又存在。

    否則清單會變成永久豁免：某天有人用同一個名字寫了一支新測試，上面那支照樣綠，
    而文件會從「正確地說它被刪了」變成「錯誤地說一個現存的東西被刪了」。
    """
    known = _defined_test_names()
    resurrected = sorted(n for n in _DELIBERATE_TEST_TOMBSTONES if n in known)
    assert not resurrected, (
        f"這些測試名被列為「已刪除／已改名」，但現在又存在了：{resurrected}。"
        "把它們從 `_DELIBERATE_TEST_TOMBSTONES` 移掉，並更新那幾句敘述。")


# --------------------------------------------------------------------------
# 程式碼裡不得留行內待辦
#
# `CLAUDE.md`「Durable knowledge」那一節：**要記的事寫在它該在的地方，不是
# 待辦與進度來源**。一行 `# TODO: 之後再處理` 正是那條規則要防的東西——沒有日期、
# 沒有「判定完成的條件」、fresh session（含冷啟動的 subagent）根本不會去讀它，
# 而那三件事正是那條規則明文要求的。
#
# 補這一支時整個專案是**零個**（`.py` 全掃、不加任何過濾），所以它是零誤報。
# 現在補，第一個想留 TODO 的人當場就知道該把它寫去哪裡；等累積了五個再補，補的
# 人得先替那五個各自判斷該怎麼辦——那時候多半就不補了。
# --------------------------------------------------------------------------
_WORK_MARKER = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")


def _inline_work_markers(lines: list[str]) -> list[tuple[int, str, str]]:
    """`(行號, 標記, 該行內容)`。抽成純函式，牙齒才長得出來——見下面的對照組。"""
    found = []
    for number, line in enumerate(lines, 1):
        for match in _WORK_MARKER.finditer(line):
            found.append((number, match.group(1), line.strip()))
    return found


def test_no_source_file_carries_an_inline_todo():
    """`.py` 裡不得出現 `TODO`／`FIXME`／`XXX`／`HACK`。

    要記的事寫清楚：**哪個檔案／符號要改**，以及**判定完成的條件**。
    行內標記缺的正是這兩樣，所以它不是「比較輕量的待辦」，是一個**看不見**的待辦。

    本檔案自己排除掉，理由與 `_test_citation_sources()` 那段相同：一支禁止某些字
    出現的守門，它的說明必然要寫出那些字。
    """
    here = Path(__file__).resolve()
    sources = [p for p in (*sorted(PACKAGE_ROOT.glob("*.py")),
                           *sorted(TEST_ROOT.glob("*.py")))
               if p.resolve() != here]
    sources += sorted(PROJECT_ROOT.glob("*.py"))
    # 正面對照組：檔案清單空掉的話，下面那句永遠通過。
    assert len(sources) >= 50, f"只掃到 {len(sources)} 個 .py，掃描範圍壞了"

    offenders = []
    for path in sources:
        for number, marker, line in _inline_work_markers(
                path.read_text(encoding="utf-8").splitlines()):
            offenders.append(f"{path.name}:{number} [{marker}] {line[:70]}")
    assert not offenders, (
        "這些地方留了行內待辦——請改寫成一句說清楚的說明（要改哪個檔案／"
        "符號、判定完成的條件）：\n  " + "\n  ".join(offenders))


def test_the_inline_todo_scanner_actually_sees_one():
    """對照組：現況零違規，所以主測試把斷言刪掉也不會紅。牙齒長在這裡。"""
    assert _inline_work_markers(["x = 1", "y = 2"]) == []
    # 先斷言「有抓到東西」再看內容：掃描器失明時 `[0]` 會丟 IndexError，而一個
    # IndexError 沒有告訴讀的人「掃描器瞎了」——那正是這一支要講的話。
    for line, expected in (("# TODO: fix", "TODO"),
                           ("    z = 3  # FIXME later", "FIXME")):
        found = _inline_work_markers([line])
        assert found, f"掃描器沒看見 {expected}：{line!r}"
        assert found[0][1] == expected
    both = [m for _n, m, _l in _inline_work_markers(["# XXX", "# HACK"])]
    assert both == ["XXX", "HACK"], f"四個標記沒有全部認得：{both}"
    # 行號要對，否則錯誤訊息會把人指到別的地方
    numbered = _inline_work_markers(["a", "b", "# TODO"])
    assert numbered and numbered[0][0] == 3, f"行號不對：{numbered}"
    # **只認整個字**：`TODOS_FILE`、`todo_prompt.md`、`read_todo_characters`
    # 這些是本專案的正常詞彙（佇列檔案就叫 todo），不可以誤判。
    assert _inline_work_markers(["TODO_FILES = ()", "read_todo_characters()",
                                 "path = 'todo_prompt.md'", "XXXL = 1",
                                 "HACKER_NEWS = 2"]) == []


def test_the_test_tombstone_list_does_not_rot():
    """墓碑清單裡的每一筆都必須真的還被某個地方引用著。"""
    cited = {name for path in _test_citation_sources()
             for _, name in _cited_tests(path)}
    stale = sorted(_DELIBERATE_TEST_TOMBSTONES - cited)
    assert not stale, (
        f"`_DELIBERATE_TEST_TOMBSTONES` 裡這幾筆已經沒有任何地方引用了：{stale}。"
        "沒人提到就把它們移掉——留著只是讓清單看起來很有道理。")


# ---------------------------------------------------------------------------
# 那三道下限自己的對照組（§8.8(A4)）
# ---------------------------------------------------------------------------
# 上面三支各自「先確認真的抽到東西，再斷言沒有違規」。**那個下限在真實資料上永遠
# 成立，所以它自己是量不出來的**：把 `> 50` 放寬成 `> 0`，整個檔案照樣全綠——
# 抽取器壞掉的那天才會發現它其實沒在保護任何東西，而那正是它要防的情況。
#
# 判準跟別處一樣：把前提直接打壞（讓列舉回空），然後斷言**是哪一句在叫**。只斷言
# 「有 AssertionError」不夠——這幾支測試裡下限後面還有別的斷言，而一個空語料同樣
# 會讓後面那些炸掉，於是控制組會綠著、下限的變異照樣存活。

def _tiny_doc_corpus(tmp_path: Path, citations: int) -> list:
    """一份**小而非空**的文件語料：剛好 `citations` 個「存在的符號」引用。

    ⚠️ **不能餵空的。** 空語料對 `> 50` 與 `> 0` 是同一個答案（0 兩邊都不過），
    所以「把下限放寬成 `> 0`」那個變異會存活——實測就是這樣溜掉的。語料必須落在
    兩者**之間**：放寬過的下限會過、真正的下限會叫。

    引用的名字刻意挑真的存在的（`_module_imports`），這樣「沒有違規」那一句不會
    先炸掉——否則控制測試會拿到一個 `AssertionError`、訊息卻是別的東西，然後
    綠著放走真正要抓的變異（§8.8(A4) 那個反覆出現的坑）。
    """
    doc = tmp_path / "tiny.md"
    doc.write_text("\n".join(f"第 {i} 行提到 `_module_imports()`。"
                             for i in range(citations)) + "\n",
                   encoding="utf-8")
    return [doc]


@pytest.mark.parametrize("citations", [1, 3, 5])
def test_the_documented_symbol_floor_fires_on_a_small_but_real_corpus(
        monkeypatch, tmp_path, citations):
    """候選數下限：語料小但非空時必須叫。

    三個大小都試，因為下限是 `> 5`——只試一個值的話，改成 `> 2` 之類的中間值
    仍然溜得掉。最大的那個剛好等於下限，所以「把 `>` 寫成 `>=`」也會被抓到。
    """
    monkeypatch.setattr(sys.modules[__name__], "_docs",
                        lambda: _tiny_doc_corpus(tmp_path, citations))
    with pytest.raises(AssertionError) as excinfo:
        test_the_durable_docs_do_not_cite_symbols_that_are_gone()
    assert "抽取器多半壞了" in str(excinfo.value), (
        f"紅的不是候選數下限那一句，而是：{excinfo.value}")


def test_the_cited_test_name_floor_fires_on_a_small_but_real_corpus(
        monkeypatch, tmp_path):
    """被引用測試名的下限（`> 40`），同樣要用小而非空的語料。"""
    src = tmp_path / "probe_source.py"
    src.write_text(
        "# 這裡提到 `test_the_durable_docs_do_not_cite_symbols_that_are_gone`\n"
        "# 還有 `test_no_comment_claims_a_guard_that_does_not_exist`\n",
        encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_test_citation_sources",
                        lambda: [src])
    with pytest.raises(AssertionError) as excinfo:
        test_no_comment_claims_a_guard_that_does_not_exist()
    assert "抽取器多半壞了" in str(excinfo.value), (
        f"紅的不是候選數那一句，而是：{excinfo.value}")


def test_the_known_test_names_floor_fires_on_its_own(monkeypatch):
    """第二支測試裡有**兩道**下限（候選數、已知測試名），依序排列。

    只把語料換小只會讓第一道炸——第二道一次都沒被執行過，於是把它放寬照樣全綠。
    所以這一支專門餵「候選夠多、但已知的測試名幾乎沒有」的語料。
    """
    monkeypatch.setattr(sys.modules[__name__], "_defined_test_names",
                        lambda: {"test_only_one"})
    with pytest.raises(AssertionError) as excinfo:
        test_no_comment_claims_a_guard_that_does_not_exist()
    assert "AST 那一半多半壞了" in str(excinfo.value), (
        f"紅的不是「已知測試名」那一道，而是：{excinfo.value}")


def test_the_inline_todo_scan_floor_fires_on_a_small_but_real_corpus(
        monkeypatch, tmp_path):
    """第三道下限的語料是**在測試函式裡就地 glob 出來的**，不是某支 helper 回的。

    所以要打壞它得換掉那三個根目錄常數。同樣不能換成空目錄——`>= 50` 與 `>= 0`
    對 0 個檔案是同一個答案。放三個乾淨的 `.py` 進去：數量遠低於 50，但不是零。
    """
    for i in range(3):
        (tmp_path / f"mod_{i}.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "PROJECT_ROOT", tmp_path)
    # 2026-09-22 測試搬到 `test/` 時加的第三個根目錄。漏換它的話真的測試檔有八十幾支，
    # 語料永遠超過下限，這一支就不再證明任何事。
    monkeypatch.setattr(sys.modules[__name__], "TEST_ROOT", tmp_path)
    with pytest.raises(AssertionError) as excinfo:
        test_no_source_file_carries_an_inline_todo()
    assert "掃描範圍壞了" in str(excinfo.value), (
        f"紅的不是掃描範圍那一句，而是：{excinfo.value}")
