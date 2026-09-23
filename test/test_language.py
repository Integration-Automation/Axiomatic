"""繁體**用詞**（`CLAUDE.md` 的 HARD REQUIREMENT）的靜態防線。

規則本身很清楚——中文一律用繁體**用詞**，不是「繁體字 ＋ 大陸詞彙」，而且
`CLAUDE.md` 連對照表都列好了。但既有的守門只管**一個方向**：
`test_docs_sync` 檢查 `_help_strings.py` 的 zh-CN 段落有沒有混進繁體字（那份刻意
是簡體）。**反方向從來沒人看**——整個版本庫的註解、docstring、README、進度紀錄
混進大陸用詞不會有任何症狀，只會慢慢讀起來像翻譯稿。這種漂移歷史上真的發生過，
2026-08-23 才手動清過一輪 `點擊` → `點選`；靠 `grep` 清一次不會擋住下一次。

**掃描範圍**是「本專案自己寫的中文」：`axiomatic/*.py`、`test/*.py`、repo root 的啟動腳本、
以及人寫的 `.md`。佇列與提示詞**資料檔**不在內——那些是使用者
自己打的內容，不是本專案的文字。`legacy/` 也不在內（只讀參考，不進 live stack）。

**三種豁免**，都是機械可判定的，所以不需要「整檔白名單」那種鈍器：

1. **反引號裡的**——`` `點擊` `` 是在**引用**這個詞（規則表、清理紀錄、grep
   指令），不是在用它。
2. **同一行也寫了規定用詞的**——對照表那種 `用戶→使用者` 的行必然兩邊都在。
3. **`_help_strings.py` 裡 `*_ZH_CN` 那幾個常數**——`CLAUDE.md` 明文規定那些段落
   刻意維持簡體給大陸使用者，不得「修正」。用 AST 取名字結尾判定，所以之後新增
   的 `*_ZH_CN` 常數自動涵蓋。

**詞表刻意只收沒有歧義的那些。** `通過`／`文件`／`程序` 三個雖然也在
`CLAUDE.md` 的對照表裡，但在台灣中文有完全正當的用法（`通過`＝pass，本專案裡
「測試通過」就有二十幾處；`文件`＝document；`程序`＝procedure，而且 `CLAUDE.md`
自己註明它技術上有效、只是**建議**改用另一個詞）。把它們收進來只會製造誤報，然後
下一個人就會把整支測試關掉——那比沒有守門更糟。要抓那三個要靠人讀。
"""
import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PKG_ROOT.parent

# 大陸用詞 → 本專案規定的繁體用詞。只收**沒有歧義**的（見模組 docstring）。
_MAINLAND_TO_TAIWAN = {
    "用戶": "使用者",
    "進程": "行程",
    "運行": "執行",
    "數據": "資料",
    "設置": "設定",
    "默認": "預設",
    "信息": "訊息",
    "網絡": "網路",
    "服務器": "伺服器",
    "內存": "記憶體",
    "優化": "最佳化",
    "線程": "執行緒",
    "源碼": "原始碼",
    "視頻": "影片",
    "軟件": "軟體",
    "緩存": "快取",
    "隊列": "佇列",
    "鏈接": "連結",
    "字符串": "字串",
    "點擊": "點選",
    "屏幕": "螢幕",
    "打印": "列印",
}

# 有歧義、刻意不收的，連理由一起列管——免得有人「補齊」對照表之後被誤報淹沒。
_DELIBERATELY_NOT_CHECKED = {
    "通過": "台灣中文的正當用法（「測試通過」「通過檢查」），本專案就有二十幾處。",
    "文件": "台灣中文＝document，正當用法。只有指 file 時才該寫「檔案」。",
    "程序": "台灣中文＝procedure；CLAUDE.md 自己註明它技術上有效，只是建議改寫。",
}

# 使用者自己打的內容，不是本專案寫的文字。
_DATA_MARKDOWN = {
    "auth.md", "discord_bot_token.md", "character1.md", "character2.md",
    "default_prompt.md", "prompt.md", "undesired.md",
    "todo_character1.md", "todo_character2.md", "todo_character_2_default.md",
    "todo_prompt.md", "todo_undesired.md",
}


def _sources() -> list[Path]:
    """要掃的檔案。

    **repo root 這一半刻意是 `*.py`，不是 `start_*.py`。** 語言規則是跨領域的
    （`CLAUDE.md`：「All Chinese in this project」），一個模組都沒指名，所以把
    root 收窄成啟動腳本，等於讓規則之書描述一道比實際更寬的閘。2026-09-20 實測
    掃不到的有 `install_autostart.py`（貨真價實的正式腳本，整份 docstring 都是
    中文）與 `docs/conf.py`，兩支今天都乾淨（各 0 筆），所以這是**趁乾淨鎖範圍**，
    不是修缺陷。同一個坑 `test_pid_liveness._live_stack_sources` 在 2026-09-10
    就踩過一次，當時只修了自己那一支——`start_*.py` 的寫法還留在另外六處，
    這一輪一起收掉，並由 `test_suite_safety` 釘住不得再出現。

    測試 2026-09-22 起住在 repo 根目錄的 `test/`，不在套件的 glob 裡，所以另外列
    ——搬家前它們一直在範圍內。
    """
    found: list[Path] = sorted(PKG_ROOT.glob("*.py"))
    found += sorted((REPO_ROOT / "test").glob("*.py"))
    found += sorted(REPO_ROOT.glob("*.py"))
    found += sorted((REPO_ROOT / "docs").glob("*.py"))
    found += [p for p in sorted(REPO_ROOT.glob("*.md"))
              if p.name not in _DATA_MARKDOWN]
    for folder in ("docs", "commands", "bot_prompts"):
        found += sorted((REPO_ROOT / folder).glob("*.md"))
    return found


def _zh_cn_line_ranges(path: Path) -> list[tuple[int, int]]:
    """`.py` 裡 `*_ZH_CN` 常數佔的行號區間（刻意維持簡體，見 `CLAUDE.md`）。"""
    if path.suffix != ".py":
        return []
    ranges: list[tuple[int, int]] = []
    for node in ast.parse(path.read_text(encoding="utf-8"), str(path)).body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id.endswith("_ZH_CN"):
                ranges.append((node.lineno, node.end_lineno or node.lineno))
    return ranges


def _outside_code_spans(line: str) -> str:
    """把反引號 code span 的內容挖掉——那是**引用**這個詞，不是在用它。"""
    return "".join(part for index, part in enumerate(line.split("`"))
                   if index % 2 == 0)


def _replacement_window(lines: list[str], number: int) -> str:
    """要在哪個範圍裡找「規定用詞」——**只有對照表形狀的行**才看前後各一行。

    對照表在原始檔裡會被換行拆開（`CLAUDE.md` 的 `進程(OS` / `process)→**行程**`
    就跨了兩行），所以那種行要看 ±1 行的窗。但這個窗是**放寬**的一步，方向是
    fail-open：它一度對每一行都開，結果一行普通敘述寫了
    `真的左鍵點擊`，只因為**上一行**剛好寫了規定用詞就被放行——Language 硬規則被
    違反，整套測試全綠。實測當時只靠這個窗被豁免的有四行：三行是換行的對照表
    （每行都帶 `→`），一行是那句敘述（不帶）。所以窗只給帶 `→` 的行。
    """
    line = lines[number - 1]
    if "→" not in line:
        return line
    return "".join(lines[max(0, number - 2):number + 1])


def _offenders(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    skip = _zh_cn_line_ranges(path)
    lines = text.splitlines()
    found: list[str] = []
    for number, line in enumerate(lines, start=1):
        if any(start <= number <= end for start, end in skip):
            continue
        visible = _outside_code_spans(line)
        window = _replacement_window(lines, number)
        for bad, good in _MAINLAND_TO_TAIWAN.items():
            if bad not in visible:
                continue
            if good in window:
                continue  # 對照表／清理紀錄：兩邊都寫了
            found.append(f"{path.name}:{number} 「{bad}」→ 應寫「{good}」： "
                         f"{line.strip()[:70]}")
    return found


_SOURCE_IDS = [str(p.relative_to(REPO_ROOT)).replace("\\", "/")
               for p in _sources()]


@pytest.mark.parametrize("source", _SOURCE_IDS)
def test_chinese_uses_taiwan_vocabulary(source):
    """本專案自己寫的中文不得出現大陸用詞。"""
    path = REPO_ROOT / source
    problems = _offenders(path)
    assert not problems, (
        "大陸用詞（`CLAUDE.md` 的 Language 硬規則）：\n  "
        + "\n  ".join(problems)
        + "\n改法：換成右邊那個詞。真的是在**引用**這個詞（規則表、清理紀錄、"
          "grep 指令）就用反引號包起來，或在同一行寫出規定用詞。")


def test_the_zh_cn_sections_are_the_only_carve_out():
    """簡體豁免只給 `*_ZH_CN`，而且那些常數要真的還在。

    豁免是用「名字結尾」判定的，所以常數改名（或整批搬走）會讓豁免默默失效或
    默默擴大。這裡把它釘住：至少要抓到那幾個常數，而且它們只能住在
    `_help_strings.py`——別的檔案冒出 `*_ZH_CN` 代表簡體語料擴散了，那要當面決定，
    不是自動放行。
    """
    holders = {path.name for path in _sources() if _zh_cn_line_ranges(path)}
    assert holders == {"_help_strings.py"}, (
        f"`*_ZH_CN` 常數出現在 {sorted(holders)}。簡體語料只該住在 "
        "`_help_strings.py`（`CLAUDE.md` 的語言例外只涵蓋那兩組 help sections）。")
    ranges = _zh_cn_line_ranges(PKG_ROOT / "_help_strings.py")
    assert len(ranges) >= 2, (
        "`_help_strings.py` 裡找不到預期的 `*_ZH_CN` 常數（channel ＋ mention 各"
        "一組）——豁免的抓法過期了，這代表整份簡體語料正在被當成違規掃。")


def test_the_ambiguous_words_stay_out_of_the_table():
    """有歧義的三個詞不得被「補齊」進詞表。

    它們在台灣中文有正當用法，收進來會製造誤報，而誤報的終點是有人把整支測試
    關掉——那比沒有守門更糟。理由連同詞一起列管在
    `_DELIBERATELY_NOT_CHECKED`，要推翻就先改那裡的理由。
    """
    overlap = sorted(set(_MAINLAND_TO_TAIWAN) & set(_DELIBERATELY_NOT_CHECKED))
    assert not overlap, (
        f"{overlap} 被收進詞表了，但它們在台灣中文有正當用法："
        + "；".join(f"{word}——{why}"
                    for word, why in sorted(_DELIBERATELY_NOT_CHECKED.items())
                    if word in overlap))


def test_the_rule_table_in_claude_md_still_covers_every_checked_word():
    """詞表不得偷偷長出 `CLAUDE.md` 沒寫的規則。

    `CLAUDE.md` 是這條規則的正本。守門自己加詞等於用測試立新規矩，下一個人只會
    看到一個查不到出處的失敗。
    """
    rule = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    section = rule.split("## Language (HARD REQUIREMENT)", 1)
    assert len(section) == 2, "CLAUDE.md 找不到 Language 那一節——抓法過期了。"
    body = section[1].split("\n## ", 1)[0]
    missing = sorted(word for word in _MAINLAND_TO_TAIWAN if word not in body)
    assert not missing, (
        f"這些詞在守門的詞表裡，但 `CLAUDE.md` 的 Language 對照表沒有：{missing}。"
        "先把規則寫進 CLAUDE.md，再讓守門去執行它。")


# `CLAUDE.md` Language 那一節的對照表，左手邊那一欄——也就是「規定要改掉」的詞。
_TABLE_ENTRY = r"([一-鿿]+)(?:\([^)]*\))?→\*\*[^*]+\*\*"


def _claude_md_vocabulary_table(text: str) -> list[str]:
    """抽出對照表左欄。回傳順序＝出現順序，**不**去重（重複也是一種過期）。

    對照表在原始檔裡會被換行拆開（`進程(OS\\nprocess)→**行程**` 就跨了兩行），
    所以先把換行折掉再抽；括號裡的英文註記（`文件(file)`）不算詞的一部分。
    """
    import re as _re  # noqa: PLC0415  只有這一族對帳要用

    section = text.split("## Language (HARD REQUIREMENT)", 1)
    assert len(section) == 2, "CLAUDE.md 找不到 Language 那一節——抓法過期了。"
    return _re.findall(_TABLE_ENTRY,
                       section[1].split("\n## ", 1)[0].replace("\n", ""))


def test_every_word_in_the_rule_table_is_guarded_or_deliberately_skipped():
    """**反方向**：正本寫了規則，就必須有人執行它，或明白列管為「刻意不收」。

    上面那支 `..._still_covers_every_checked_word` 只問了一個方向（守門不得長出
    正本沒寫的規則）。反過來——**正本的對照表多寫一個詞、而守門既沒收也沒列進
    `_DELIBERATELY_NOT_CHECKED`**——在此之前沒有任何東西在看，而那個方向是
    **fail-open** 的：`CLAUDE.md` 上白紙黑字寫著「X→Y」，掃描器根本不認得 X，
    於是整個專案照樣全綠，規則只是一句話。

    ⚠️ 這不是假想的形狀。2026-09-10 同一天，`_OWNER_ONLY_GROUPS` 就是這樣過期的：
    對帳只做了降級那一邊，升級那一邊就永遠綠著，而症狀是零。那次的教訓是
    「單向對帳等於沒對帳」，這一支是把同一條教訓套到語言規則上。

    目前的分割剛好是乾淨的（實測：正本 25 筆 ＝ 守門 22 ＋ 刻意不收 3，無交集、
    無剩餘），所以這一支現在是純粹的**防止未來漂移**。
    """
    documented = _claude_md_vocabulary_table(
        (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8"))
    # 正面對照：抽不到東西的話，下面那個列表推導會得到空集合，**然後通過**——
    # 那跟「每一個詞都有人執行」長得一模一樣（`CLAUDE.md` 自己寫的：an empty
    # extraction looks exactly like a clean result）。先釘住真的抓到了表。
    assert len(documented) >= 20, (
        f"只從 `CLAUDE.md` 的對照表抽到 {len(documented)} 筆（下限 20）——"
        f"抓法過期了，這一支等於沒問：{documented}")

    unenforced = sorted(word for word in documented
                        if word not in _MAINLAND_TO_TAIWAN
                        and word not in _DELIBERATELY_NOT_CHECKED)
    assert not unenforced, (
        f"`CLAUDE.md` 的 Language 對照表寫了這些詞，但沒有任何東西在執行它們："
        f"{unenforced}。兩條路擇一：收進 `_MAINLAND_TO_TAIWAN` 讓掃描去抓，"
        "或收進 `_DELIBERATELY_NOT_CHECKED` 並寫下「為什麼這個詞在台灣中文也"
        "正當」。不要留著——規則之書上一條沒人執行的規則，比沒寫還糟。")


# 合成語料：給下面那支對照組用的最小對照表。刻意包含四種真實形狀——一般詞、
# 帶英文註記的詞（`文件(file)`）、註記被換行拆開的詞（`進程`），以及**詞與箭頭
# 之間**被換行拆開的詞（`軟件`）。
#
# ⚠️ 最後那一種是變異測試逼出來的，別把它當成湊數。原本的語料只有前三種，於是
# 擷取器裡折換行的 `.replace("\n", "")` **拿掉也沒有任何測試會紅**：`進程` 那一種
# 的換行落在 `(OS\nprocess)` 裡，而正規表示式的 `[^)]*` 本來就吃得下換行——也就是
# 說那一格變異是**空轉的**，折換行對它根本沒作用。真正需要折換行的是 `軟件` 這種
# 「詞在行尾、箭頭在下一行」的排法，因為 `[一-鿿]+` 跨不過換行。
# 教訓跟 §8.5 那兩格 SURVIVED 同一條：變異存活要先問「這個變異真的改變了行為嗎」。
_SAMPLE_LANGUAGE_SECTION = (
    "## Language (HARD REQUIREMENT)\n\n"
    "用戶→**使用者**、文件(file)→**檔案**、進程(OS\n"
    "process)→**行程**、軟件\n"
    "→**軟體**、屏幕→**螢幕**。\n\n"
    "## Windows PID liveness\n")


def test_the_vocabulary_table_detector_actually_bites():
    """對帳本身要有對照組，否則「乾淨」跟「沒問到」分不出來。

    真實資料現在是乾淨的，所以上面那支即使把斷言刪掉也會全綠。這一支用合成語料
    證明擷取器與判準真的會咬人。
    """
    # 右邊那串註記不是裝飾：本檔自己的掃描會看到左邊那四個大陸用詞，而豁免規則之一
    # 就是「同一行也寫出規定用詞＝這裡是引用不是使用」。拿掉註記，這一支會自己咬自己。
    expected = ["用戶", "文件", "進程", "軟件", "屏幕"]  # ＝使用者／檔案／行程／軟體／螢幕
    assert _claude_md_vocabulary_table(_SAMPLE_LANGUAGE_SECTION) == expected, (
        "擷取器連基本形狀都抽錯了")

    # 正本多寫一個沒人執行的規則 → 必須被指出來。
    invented = _SAMPLE_LANGUAGE_SECTION.replace("屏幕→**螢幕**", "登錄→**登入**")
    words = _claude_md_vocabulary_table(invented)
    assert "登錄" in words, "合成語料自己就沒被抽到，下面那條斷言會空轉"
    assert "登錄" not in _MAINLAND_TO_TAIWAN, "「登錄」真的被收進詞表了？改個別的詞"
    assert "登錄" not in _DELIBERATELY_NOT_CHECKED
    unenforced = [w for w in words
                  if w not in _MAINLAND_TO_TAIWAN
                  and w not in _DELIBERATELY_NOT_CHECKED]
    assert unenforced == ["登錄"], f"沒抓到那個沒人執行的規則：{unenforced}"

    # 反面：整張表都有人執行時，不得誤報。
    assert not [w for w in _claude_md_vocabulary_table(_SAMPLE_LANGUAGE_SECTION)
                if w not in _MAINLAND_TO_TAIWAN
                and w not in _DELIBERATELY_NOT_CHECKED]


def test_no_stray_nbsp_in_prose():
    """中文散文裡不得殘留 NBSP（`\\xa0`）——它 `strip()` 得掉、肉眼看不出來。

    佇列檔的讀取端本來就會把 NBSP 換成一般空白（`CLAUDE.md` 的 todo 契約），
    但**原始碼與文件**沒有那層清洗：從網頁或聊天視窗貼進來的字很容易夾帶 NBSP，
    之後任何按空白切詞、對齊表格、或逐字元比對兩份提示詞的地方都會安靜地錯開。

    **這支的名字一度是 `..._ideographic_space_or_nbsp_...`，而它從來沒有檢查全角
    空白**（U+3000）——一個承諾兩件事、只做一件的名字比少一道守門更糟，因為下一個人
    會以為那件事有人在管。2026-09-20 選擇改名而不是「補齊」，理由是量出來的：全角
    空白在掃描範圍裡只有 21 行、3 個檔案，而每一行都是**刻意**的視覺分隔——
    `discord_bot.py` 的 15 行全在送去對話平台的訊息裡（中日韓文字之間用全角空白排版
    才對得齊），另外 6 行是 `verify_browser.py` 的參數行與架構圖表格的欄內分隔。收進
    來會是一支當場誤報 21 筆的守門，而那種守門的終點是有人把它整支關掉。

    反引號裡的照樣豁免——架構圖與進度紀錄裡有幾處是**刻意**貼一個真的 NBSP 當
    範例（在講 cp950 那個陷阱），那是在展示這個字元，不是不小心夾帶。
    """
    problems = []
    for path in _sources():
        problems += _nbsp_offenders(path)
    assert not problems, (
        "這些行夾帶了 NBSP（U+00A0），肉眼與一般空白無法區分：\n  "
        + "\n  ".join(problems) + "\n改法：換成一般空白。")


def _nbsp_offenders(path) -> list[str]:
    """夾帶 NBSP 的行，回 `檔名:行號`。

    偵測那幾行**抽出來**才驗得到（2026-09-20）：上面那支跑的是專案自己的檔案，而它們
    全部是乾淨的，所以那個 `append` 一行都沒被執行過——實測覆蓋率上就是一個 miss。
    把整個迴圈換成 `pass`，整套照樣全綠。這與同檔下面 `_offenders()` 需要對照組是
    同一個理由，只是那一條當時補了、這一條沒有。
    """
    return [f"{path.name}:{number}"
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1)
            if "\xa0" in _outside_code_spans(line)]


def test_the_nbsp_detector_fires_on_a_planted_nbsp(tmp_path):
    """對照組一：真的夾一個 NBSP 進去，一定要被指名到**行**。

    NBSP 用 `chr(0xa0)` 在執行期組出來，不是直接打進原始檔：這個檔自己也在上面
    那支的掃描範圍裡，打字面值等於把守門的測試資料變成守門的違規。跳脫寫法
    （`\\u00a0`）也行，但它在幾種編輯管道上會被還原成真的字元，`chr()` 不會。
    """
    probe = tmp_path / "probe.md"
    probe.write_text("第一行沒問題。\n第二行夾了一個" + chr(0xa0)
                     + "在裡面。\n", encoding="utf-8")
    assert _nbsp_offenders(probe) == ["probe.md:2"]


def test_an_nbsp_inside_backticks_is_being_shown_not_smuggled(tmp_path):
    """對照組二（must-allow）：反引號裡的 NBSP 是在**展示**這個字元。

    架構圖與進度紀錄裡真的有幾處刻意貼了一個 NBSP 當範例（在講 cp950 那個陷阱）。
    豁免是放寬步驟，所以只有這一格殺得掉「把 `_outside_code_spans` 拿掉」——只餵
    上一格那種該報的樣本，拿掉豁免照樣全綠。
    """
    probe = tmp_path / "probe.md"
    probe.write_text("這裡展示一個 `" + chr(0xa0) + "` 當範例。\n",
                     encoding="utf-8")
    assert _nbsp_offenders(probe) == []


# ---------------------------------------------------------------------------
# 偵測器自己的對照組（2026-09-10 補）
#
# `_offenders()` 的唯一呼叫端餵的是**專案自己的檔案**，而它們全部是乾淨的——
# 所以把 `_offenders` 整個換成 `return []`，上面那一百多個參數化案例會全部通過。
# 一條硬規則，卻沒有任何東西證明它的偵測器還活著。
#
# 這一支特別需要對照組，因為它有**三個豁免**（反引號、同行寫出規定用詞、
# `*_ZH_CN` 區段），每一個都是「把守門悄悄放寬到失效」的入口：例如把
# `_outside_code_spans` 改成回空字串，所有命中都會消失，而真實資料上完全看不出來。
# ---------------------------------------------------------------------------

def _offenders_of(tmp_path, body: str, name: str = "probe.md") -> list:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return _offenders(path)


def test_the_vocabulary_detector_fires_on_every_word_in_the_table(tmp_path):
    """對照表裡的**每一個**詞都要真的抓得到。

    逐詞跑而不是抽驗：一個打錯字的鍵（或一個永遠不可能出現的詞）在真實資料上
    完全沒有症狀，因為真實資料本來就不含這些詞。
    """
    assert _MAINLAND_TO_TAIWAN, "對照表是空的——那上面每一支測試都會空轉"
    dead = []
    for bad in _MAINLAND_TO_TAIWAN:
        # 刻意不寫出對應的規定用詞，否則會落進「同行對照」那個豁免
        if not _offenders_of(tmp_path, f"這裡故意寫了{bad}兩個字。"):
            dead.append(bad)
    assert not dead, f"對照表這些詞抓不到：{dead}"


def test_a_word_in_backticks_is_quoting_not_using(tmp_path):
    """反面豁免一：反引號裡的詞是在**引用**它，不是在用它。

    規則表、清理紀錄、grep 指令都得寫得出那個詞。沒有這一格，把
    `_outside_code_spans` 拿掉不會有人發現（真實資料上兩者答案相同）。
    """
    bad = next(iter(_MAINLAND_TO_TAIWAN))
    assert not _offenders_of(tmp_path, f"規則表寫著 `{bad}` 這個詞不要用。")


def test_naming_the_replacement_on_the_same_line_is_a_correction(tmp_path):
    """反面豁免二：同一行也寫出規定用詞 ＝ 對照表或更正紀錄。

    對照表形狀的行（帶 `→`）放寬到 ±1 行，見 `_replacement_window`。
    """
    bad, good = next(iter(_MAINLAND_TO_TAIWAN.items()))
    assert not _offenders_of(tmp_path, f"{bad}→**{good}**")


def test_the_replacement_must_actually_be_nearby(tmp_path):
    """反面的反面：規定用詞離太遠就不算對照，否則整份文件只要提過一次就全免。

    這一格釘的是那個 ±1 行的窗。沒有它，把窗放大到「整份文件」不會有任何症狀。
    """
    bad, good = next(iter(_MAINLAND_TO_TAIWAN.items()))
    body = f"{good}\n" + "填充\n" * 5 + f"這裡寫了{bad}。"
    assert _offenders_of(tmp_path, body), (
        "規定用詞在五行之外還被當成同行對照——那個窗形同虛設。")


def test_an_adjacent_prose_line_does_not_excuse_its_neighbour(tmp_path):
    """must-block 近鄰：普通敘述的**隔壁行**寫了規定用詞，不算對照。

    這一格就是抓到過的那個真實形狀：上一行寫了規定用詞、
    本行用的是大陸用詞，兩行都沒有 `→`。窗是對稱的，所以上下各一格。第三格釘住
    對照表形狀的行也**只**看隔壁——沒有它，把窗放寬到 ±2 行不會有任何症狀。
    """
    bad, good = next(iter(_MAINLAND_TO_TAIWAN.items()))
    above = f"上一行提到{good}。\n這一行卻寫了{bad}。\n"
    below = f"這一行寫了{bad}。\n下一行才提到{good}。\n"
    far = f"{bad}→\n填充\n{good}\n"
    assert _offenders_of(tmp_path, above, name="probe_above.md"), (
        "上一行的規定用詞替本行的大陸用詞開脫了")
    assert _offenders_of(tmp_path, below, name="probe_below.md"), (
        "下一行的規定用詞替本行的大陸用詞開脫了")
    assert _offenders_of(tmp_path, far, name="probe_far.md"), (
        "對照表形狀的行，隔兩行的規定用詞也被算進窗裡了")


def test_a_wrapped_rule_table_row_is_still_excused(tmp_path):
    """must-ALLOW 近鄰：對照表被換行拆開時，窗要照舊替它開脫。

    `_replacement_window` 是一個**放寬**的步驟，所以只有 must-ALLOW 的案例殺得掉
    「把窗整個拿掉」那個變異——上面那支 must-block 殺不掉它。形狀照抄 `CLAUDE.md`
    的實際換行：本行以 `進程(OS` 結尾、規定用詞在下一行；另外兩個詞各自同行對照，
    所以這一格只有中間那個詞依賴窗。
    """
    body = ("用戶→**使用者**、進程(OS\n"
            "process)→**行程**、運行→**執行**\n")
    assert not _offenders_of(tmp_path, body), (
        "換行的對照表被當成違規了——窗被拿掉或收得太緊。")


def test_the_zh_cn_carve_out_is_bounded_to_its_own_section(tmp_path):
    """反面豁免三：`*_ZH_CN` 區段刻意用簡體，但豁免不得溢出到區段之外。

    `_zh_cn_line_ranges` 只認 `.py`，所以這一格用 `.py` 檔。
    """
    bad = next(iter(_MAINLAND_TO_TAIWAN))
    body = (f'CHANNEL_HELP_ZH_CN = """\n'
            f'{bad}\n'
            f'"""\n'
            f'OTHER = "{bad}"\n')
    problems = _offenders_of(tmp_path, body, name="probe_zh.py")
    assert problems, "區段外的那一筆也被豁免掉了——豁免範圍溢出了"
    assert all(":4" in p or p.endswith(f'OTHER = "{bad}"') for p in problems), (
        f"抓到的不是區段外那一行：{problems}")


# ---------------------------------------------------------------------------
# 第三個方向：繁體文字裡混進**非台灣標準字形**（`优化`）。
#
# 上面那份詞表兩側都是繁體字，所以連字都換掉的寫法不在詞表裡，穿得過去。判定搬在
# `audit_simplified_chars.py`，它自帶一張內嵌字表、沒有任何第三方 import——所以這裡
# 可以是真的閘門，而不是一支在 `.venv` 上永遠跳過的裝飾。理由見該檔 docstring 與
# ---------------------------------------------------------------------------
import audit_simplified_chars as _simplified  # noqa: E402


def test_the_character_table_is_self_consistent():
    """字表本身要先可信，否則下面那支的乾淨結果毫無意義。

    兩側都驗：只驗「該抓的有抓到」的話，一張**全部都收**的字表也會全過；只驗
    「該放的有放過」的話，一張**空**字表也會全過。
    """
    assert not _simplified.self_check(), _simplified.self_check()
    assert len(_simplified.NOT_TW_STANDARD) > 2500, (
        f"字表只剩 {len(_simplified.NOT_TW_STANDARD)} 個字，像是被截斷了")
    for char in _simplified.AMBIGUOUS:
        assert char not in _simplified.NOT_TW_STANDARD, (
            f"`{char}` 列在 `AMBIGUOUS` 卻還留在字表裡——扣除沒生效")


def test_the_code_span_carve_out_allows_quotation_but_not_use():
    """反引號豁免是**放寬**步驟，只有 must-allow 樣本殺得掉它。

    規則之記錄必須舉得出反例（`CLAUDE.md` 與架構圖都得寫出 `优化` 這種字），而本
    repo 舉例的正確寫法就是包進反引號。只餵「該報的」樣本永遠驗不出這個豁免被刪掉。
    第二格是近似樣本：同一行有反引號、但簡體在**外面**——少了它，一個「整行有反引號
    就整行跳過」的錯誤實作也會在第一格上過關。
    """
    quoted = _simplified.outside_code_spans("這裡引用一個簡體寫法 `优化` 當例子")
    assert not [c for c in quoted if c in _simplified.NOT_TW_STANDARD], (
        "反引號裡的字沒被挖掉——豁免失效了")
    # 這一格的樣本刻意用 `\u` 跳脫寫：它要的就是「簡體字在反引號**外面**」，直接
    # 打出來的話這支守門會抓到自己的測試資料。跳脫之後原始檔裡是 ASCII，執行期才
    # 變成那兩個字（`優化` 的簡體寫法）。
    mixed = _simplified.outside_code_spans(
        "這裡寫 `code` 之外還有 \u4f18\u5316 兩個字")
    assert [c for c in mixed if c in _simplified.NOT_TW_STANDARD], (
        "反引號之外的簡體被一起挖掉了——豁免溢出成整行跳過")


def test_no_simplified_characters_leak_into_traditional_text():
    """真實資料：該寫繁體的地方不得混進非台灣標準字形。

    正面對照三側都要，因為空的結果跟乾淨的結果長得一模一樣：掃到的檔案數要有下限、
    `*_ZH_CN` 的豁免要真的命中過（0 代表 AST 抓法壞了）、列管的刻意簡體要真的命中過
    （0 代表 `DELIBERATE` 的比對壞了，而那會讓下面的斷言變成空轉）。
    """
    review, known, _used, (n_files, exempted) = _simplified.scan()
    assert n_files >= 120, f"只掃到 {n_files} 個檔，檔案選取壞了"
    assert exempted > 0, "`*_ZH_CN` 一行都沒豁免到——AST 抓法壞了"
    assert known, "`DELIBERATE` 一筆都沒命中——比對壞了，下面那句會空轉"
    assert not review, (
        "這些地方混進了非台灣標準字形：\n"
        + "\n".join(f"  {rel}:{n}  [{chars}]  {text}"
                    for rel, n, chars, text in review)
        + "\n改法：確認那個字在台灣中文裡不是正當寫法就改掉；只是在**舉例**的話"
          "包進反引號；真的是刻意的（zh-CN 語料、使用者會打的別名、繁簡對照表），"
          "加進 `audit_simplified_chars.DELIBERATE` 並寫下理由。")


def test_the_deliberate_simplified_list_has_no_stale_entries():
    """豁免清單要對帳——過期的豁免會安靜失效，而守門看起來照常在跑。

    與 `_OWNER_ONLY_SLASH` 同一個形狀：那幾筆比對的是散文裡的子字串，散文一改就
    對不上，而那時它放行的其實已經是別的東西（或什麼都不是）。
    """
    _review, _known, used, _stats = _simplified.scan()
    stale = _simplified.stale_rules(used)
    assert not stale, (
        "這些豁免規則一行都沒對上，可能已經過期："
        + str(stale)
        + "。改掉錨點字串，或整筆刪掉——它保護的那一行可能已經不存在了。")
# ---------------------------------------------------------------------------
# 稽核器自己的對照組與 CLI 契約（2026-09-20 補）
#
# 上面那四支跑的全是**乾淨的真實資料**，於是 `scan()` 裡真正下判定的那一行
# （`review.append(row)`）一行都沒有被執行過——量覆蓋率時它就是一個 miss。把
# 「不在列管清單裡就報出來」整段換成 `pass`，這個檔連同整套照樣全綠，而那支稽核
# 從此永遠回報「需要人看：0」。這是同一個形狀：
# 樹是乾淨的，所以違規回報的程式碼永遠不會在真實資料上跑過。
#
# `main()` 同理，**整支從來沒有被執行過**：它是 `CLAUDE.md` 指名可以單獨跑的工具
# （結束碼 0／1／2 各有意思），而那三個結束碼沒有任何東西驗過。同一個坑
# `verify_browser.py` 在 2026-09-09 踩過一次。
#
# 語料裡的簡體字**不寫字面值**，從字表裡抓兩個出來：這個檔自己也在稽核的掃描範圍
# 內，字面值等於把測試資料變成違規（上面那格為此寫成跳脫）。從字表抓還有第二個
# 好處——字表改了也不會留下一個永遠不可能命中的測試資料。
# ---------------------------------------------------------------------------

def _two_simplified_chars() -> tuple[str, str]:
    """從字表裡借兩個字當語料。"""
    picked = sorted(_simplified.NOT_TW_STANDARD)[:2]
    assert len(picked) == 2, "字表空了——下面每一支都會空轉"
    return picked[0], picked[1]


def _corpus(monkeypatch, tmp_path, body: str, rules=None, name="probe.md"):
    """把 `scan()` 的觀察範圍換成 `tmp_path` 裡的一份合成語料。"""
    (tmp_path / name).write_text(body, encoding="utf-8")
    files = sorted(tmp_path.glob("*.md"))
    monkeypatch.setattr(_simplified, "sources", lambda: list(files))
    monkeypatch.setattr(_simplified, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_simplified, "DELIBERATE", [] if rules is None else rules)
    return files


def test_the_simplified_detector_reports_a_planted_character(
        monkeypatch, tmp_path):
    """對照組：種一個簡體字進去，一定要被指名到**行**。

    沒有這一格，`review.append(row)` 在整個版本庫上一次都不會執行——刪掉它、或把
    `if not rules` 寫反，真實資料上的答案完全一樣（兩邊都是「需要人看：0」）。
    """
    bad, _other = _two_simplified_chars()
    _corpus(monkeypatch, tmp_path,
            "第一行是乾淨的。\n第二行有 " + bad + " 這個字。\n")
    review, known, used, (n_files, exempted) = _simplified.scan()
    assert [(rel, no, chars) for rel, no, chars, _text in review] == [
        ("probe.md", 2, bad)]
    assert not known and not used
    assert (n_files, exempted) == (1, 0)


def test_a_deliberate_rule_moves_the_row_out_of_review(monkeypatch, tmp_path):
    """列管規則對上時，同一行要改走 `known`，而且那條規則要被記成用過。

    兩側都驗：只看「不在 review 裡」的話，一個把整行吞掉的錯誤實作也會過關。
    """
    bad, _other = _two_simplified_chars()
    _corpus(monkeypatch, tmp_path, "這一行是刻意的 " + bad + "。\n",
            rules=[(".md", "是刻意的", "測試用的理由")])
    review, known, used, _stats = _simplified.scan()
    assert not review
    assert [(rel, no) for rel, no, _chars, _text in known] == [("probe.md", 1)]
    assert used == {(".md", "是刻意的")}
    assert _simplified.stale_rules(used) == []


def test_every_rule_that_matched_a_line_is_marked_used(monkeypatch, tmp_path):
    """一行同時對上兩條規則時，**兩條**都要算用過。

    2026-09-20 的回歸測試。`scan()` 原本是拿另一支小函式回傳的**理由文字**當外鍵
    去回查哪幾條規則對上了，於是第二條規則明明正在做事，卻會被
    `stale_rules()` 報成「一行都沒對上，可能已經過期」。方向是**誤報**，而誤報的
    終點是有人把整支守門關掉。判準現在單一來源在 `matching_rules()`。
    """
    first, second = _two_simplified_chars()
    _corpus(monkeypatch, tmp_path,
            "同一行同時對上兩條：" + first + " 與 " + second + "。\n",
            rules=[(".md", first, "理由甲"), (".md", second, "理由乙")])
    _review, known, used, _stats = _simplified.scan()
    assert len(known) == 1
    assert used == {(".md", first), (".md", second)}
    assert _simplified.stale_rules(used) == [], (
        "第二條規則明明對上了那一行卻被報成過期——用理由文字當外鍵的寫法回來了")


def test_a_file_that_cannot_be_read_is_named_and_the_scan_goes_on(
        monkeypatch, tmp_path, capsys):
    """讀不了的檔要**指名**，而且不得讓後面的檔一起消失。

    這條路平常一行都跑不到（專案自己的檔都讀得到），但它的失敗模式是最安靜的那
    一種：`continue` 沒有搭配那一行輸出的話，稽核會少掃幾個檔而總數看起來完全
    正常。
    """
    bad, _other = _two_simplified_chars()
    good = tmp_path / "good.md"
    good.write_text("這一行有 " + bad + " 在裡面。\n", encoding="utf-8")
    missing = tmp_path / "gone.md"
    monkeypatch.setattr(_simplified, "sources", lambda: [missing, good])
    monkeypatch.setattr(_simplified, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_simplified, "DELIBERATE", [])
    review, _known, _used, (n_files, _exempted) = _simplified.scan()
    out = capsys.readouterr().out
    assert "gone.md" in out, f"讀不了的檔沒有被指名：{out!r}"
    assert [rel for rel, _no, _chars, _text in review] == ["good.md"]
    assert n_files == 2, "讀不了的那個檔沒被算進分母，會讓下降看起來像沒事"


def test_the_table_self_check_names_each_way_it_can_be_wrong(monkeypatch):
    """`self_check()` 的三句話各自要真的出得來。

    它們也是只在壞掉時才執行的行。整張表拿掉之後應該同時抱怨「該抓的沒收進來」
    與「像是被截斷了」；把一個台灣正當用字塞進表裡則只該抱怨那一句——分開驗才
    看得出三句話有沒有接錯條件。
    """
    assert not _simplified.self_check(), "基準線就不乾淨，下面兩格沒有意義"
    # 先把真的字表留一份：第二格要拿它當底，而那時 `NOT_TW_STANDARD` 已經被換掉了。
    real = frozenset(_simplified.NOT_TW_STANDARD)

    monkeypatch.setattr(_simplified, "NOT_TW_STANDARD", frozenset())
    empty = _simplified.self_check()
    assert len(empty) == 2, empty
    assert any("沒被收進字表" in p for p in empty), empty
    assert any("像是被截斷了" in p for p in empty), empty

    allowed = next(iter(_simplified._MUST_ALLOW))
    monkeypatch.setattr(_simplified, "NOT_TW_STANDARD", real | {allowed})
    over = _simplified.self_check()
    assert len(over) == 1 and "台灣正當用字" in over[0], over


def test_the_audit_cli_refuses_to_scan_with_a_broken_table(monkeypatch, capsys):
    """結束碼 **2**：字表自己不可信時不得給出「需要人看：0」。

    這是三個結束碼裡最重要的一個——0 與 2 對人類讀起來都像「沒事」，但 2 的意思是
    *什麼都沒查*。這條路必須在 `scan()` 之前就折返。
    """
    monkeypatch.setattr(_simplified, "NOT_TW_STANDARD", frozenset())
    monkeypatch.setattr(_simplified, "sources",
                        lambda: pytest.fail("字表壞了還去掃檔案"))
    assert _simplified.main([]) == 2
    assert "自我檢查失敗" in capsys.readouterr().out


def test_the_audit_cli_exits_1_and_points_at_the_line(
        monkeypatch, tmp_path, capsys):
    """結束碼 **1**：有東西要人看的時候。"""
    bad, _other = _two_simplified_chars()
    _corpus(monkeypatch, tmp_path, "這一行有 " + bad + "。\n")
    assert _simplified.main([]) == 1
    out = capsys.readouterr().out
    assert "需要人看：1" in out, out
    assert "probe.md:1" in out, out
    assert "判定方式" in out, "只報數字不講怎麼判，下一個人只能來問"


def test_the_audit_cli_exits_0_and_says_there_is_none(
        monkeypatch, tmp_path, capsys):
    """結束碼 **0**：乾淨。要印出「（沒有。）」而不是一片空白。

    空白與乾淨長得一樣，是這個 repo 反覆踩到的形狀。
    """
    _corpus(monkeypatch, tmp_path, "這一行整行都是繁體字。\n")
    assert _simplified.main([]) == 0
    out = capsys.readouterr().out
    assert "需要人看：0" in out and "（沒有。）" in out, out


def test_show_known_is_off_by_default(monkeypatch, tmp_path, capsys):
    """`--show-known` 兩個方向都驗：帶了才印，不帶就不印。

    只驗「帶了有印」的話，一個永遠都印的實作也會過關，而列管的刻意簡體每天都在，
    平常那段輸出會把真正要看的那幾行擠掉。
    """
    bad, _other = _two_simplified_chars()
    body = "這一行是刻意的 " + bad + "。\n"
    rules = [(".md", "是刻意的", "測試用的理由")]

    _corpus(monkeypatch, tmp_path, body, rules=rules)
    assert _simplified.main([]) == 0
    quiet = capsys.readouterr().out
    assert "已知刻意（1）" not in quiet, quiet

    _corpus(monkeypatch, tmp_path, body, rules=rules)
    assert _simplified.main(["--show-known"]) == 0
    loud = capsys.readouterr().out
    assert "已知刻意（1）" in loud and "probe.md:1" in loud, loud


def test_the_audit_cli_warns_about_a_rule_that_matched_nothing(
        monkeypatch, tmp_path, capsys):
    """過期的豁免要在 CLI 上講出來，不能只有測試裡紅。

    單獨跑這支工具的人（`CLAUDE.md` 就是這樣教的）看不到 pytest 的輸出。
    """
    _corpus(monkeypatch, tmp_path, "這一行整行都是繁體字。\n",
            rules=[(".md", "這個錨點不可能出現", "測試用的理由")])
    assert _simplified.main([]) == 0
    out = capsys.readouterr().out
    assert "一行都沒對上" in out and "這個錨點不可能出現" in out, out
def test_a_py_file_that_does_not_parse_does_not_take_the_audit_down(
        monkeypatch, tmp_path):
    """`*_ZH_CN` 的豁免是用 AST 抓的，而 AST 會對壞掉的 `.py` 丟例外。

    這條路平常一行都跑不到（樹上的 `.py` 都剖析得開），但它跑的那天正是最不該
    整支倒下的那天：有人存檔存到一半、或一個合併衝突留在檔案裡，稽核應該照常把
    其餘的檔掃完並照常報出違規，而不是拋一個 `SyntaxError` 讓人以為稽核壞了。
    豁免退回「這個檔沒有任何 zh-CN 區段」——**沒有豁免比亂豁免安全**。
    """
    bad, _other = _two_simplified_chars()
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")
    assert _simplified.zh_cn_line_ranges(broken) == []

    good = tmp_path / "probe.md"
    good.write_text("這一行有 " + bad + "。\n", encoding="utf-8")
    monkeypatch.setattr(_simplified, "sources", lambda: [broken, good])
    monkeypatch.setattr(_simplified, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_simplified, "DELIBERATE", [])
    review, _known, _used, (n_files, _exempted) = _simplified.scan()
    assert [rel for rel, _no, _chars, _text in review] == ["probe.md"]
    assert n_files == 2
