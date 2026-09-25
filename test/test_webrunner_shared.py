"""`_webrunner_shared` 的靜態防線 + 純函式煙霧測試（無 Selenium、無瀏覽器）。

P6 C1 把兩支 webrunner 完全相同、根本不碰 driver 的純函式抽到
`_webrunner_shared.py`。本測試守住兩件事：

1. **driver-agnostic 硬規則**：共用模組原始碼不可出現 `import selenium` /
   `from selenium` / `je_web_runner`（一旦有人把需要 driver 的東西搬進來，
   這條會立刻變紅）。
2. **純函式行為**：抽過去的 `pair_todos` / `character_folder_name` /
   `read_todo_characters` 仍維持原語義（padding、首段命名、NBSP 正規化）。

可直接 `py -3 test/test_webrunner_shared.py`（自帶 runner），也可 pytest。
"""

import ast
import contextlib
import functools
import io
import json
import os
import random
import re
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _webrunner_shared as ws  # noqa: E402


class _FakeClock:
    """`_webrunner_shared` 的 `time` 替身——`sleep()` 推進虛擬時鐘而不真的睡。

    比 `ws.time.sleep = lambda _s: None` 好在兩件事：

    1. **不動到 stdlib。** `ws.time` 就是 `time` 模組本身，換掉它的 `sleep` 是
       全行程生效的（`conftest.py` 有守門）。這裡換的是 `ws.time` 這個**名字**，
       只影響被測模組。
    2. **牆鐘迴圈會真的結束。** 把 `sleep` 換成 no-op 只是拔掉節流閥：
       `click_generate` 的 `while time.time() < end` 仍然要等真實的 15 秒，中間
       空轉一千八百萬圈。時鐘跟著 `sleep` 前進之後，同一段變成 50 圈就走完，
       判定完全一樣。
    """

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start
        self.slept: list[float] = []
        # 牆鐘相對於單調時鐘的偏移。平常是 0（兩個時鐘一起走）；要模擬 NTP 校時／
        # 手動改時間就在 `sleep` 的第 n 次呼叫時動它，只有 `time()` 會跳，
        # `monotonic()` 不受影響——真實世界就是這樣。
        self.wall_skew = 0.0
        self.on_sleep = None      # callable(clock, seconds)，每次 sleep 後呼叫

    def time(self) -> float:
        return self.now + self.wall_skew

    def monotonic(self) -> float:
        return self.now

    # 連續幾次「睡了 0 秒」就判定為卡住。被測的迴圈普遍長成
    # `while <還沒到> : ... time.sleep(min(片長, 剩餘))`，剩餘一旦算成 0 或負數，
    # 虛擬時鐘就再也不前進，迴圈永遠不會結束——**測試不是紅的，是整個回合掛在那裡**。
    # 這個 session 實測過兩次：一個把截止時刻改回牆鐘的 mutation，兩支測試都是「靠掛住
    # 被抓到」而不是靠斷言。一句紅色訊息一行就講完問題，一次掛住要 stack dump 才看得
    # 出來，而且在無人值守的批次裡會把後面排隊的東西全部擋住。
    # 1000 遠高於任何正當用途（正當的 0 秒睡眠是節流閥被算到剛好，不會連續上千次）。
    _ZERO_SLEEP_LIMIT = 1000

    def sleep(self, seconds) -> None:
        self.slept.append(seconds)
        step = max(0.0, float(seconds))
        self.now += step
        if step > 0:
            self._zero_run = 0
        else:
            self._zero_run = getattr(self, "_zero_run", 0) + 1
            if self._zero_run > self._ZERO_SLEEP_LIMIT:
                raise AssertionError(
                    f"假時鐘連續 {self._zero_run} 次睡 0 秒——虛擬時間不再前進，"
                    "被測迴圈永遠不會結束。多半是「還剩多久」算成 0 或負數："
                    "檢查剩餘時間用的時鐘（time.time() vs time.monotonic()）"
                    "跟截止時刻是不是同一個。")
        if self.on_sleep is not None:
            self.on_sleep(self, seconds)

    def localtime(self, seconds=None):
        return time.localtime(self.now if seconds is None else seconds)

    def strftime(self, fmt, struct=None):
        return time.strftime(fmt, struct if struct is not None
                             else time.localtime(self.now))


@contextlib.contextmanager
def _fake_clock(clock=None):
    """在 `_webrunner_shared` 眼中把時間換成假的，離開時還原。

    `clock` 可以傳一個現成的 `_FakeClock`，好讓 stub 函式在測試裡讀得到同一份
    虛擬時間（例如記錄「探測發生在第幾秒」）。

    這個模組裡好幾支測試會走到以牆鐘判逾時的迴圈
    （`click_generate`、`wait_for_new_image`、`wait_for_quota_recovery`、
    以及散落各處的 `human_pause`）。用真時鐘的話它們會真的等——`_FakeClock`
    出現之前，光是四支這樣的測試就佔掉整個模組 36 秒裡的 28 秒。

    **不要改回 `ws.time.sleep = lambda _s: None`。** 那有兩個問題：`ws.time`
    就是 stdlib 的 `time` 模組，指派會全行程生效（`conftest.py` 有守門）；
    而且把 `sleep` 變成 no-op 只是拔掉節流閥，`while time.time() < end` 那種
    迴圈仍然要等滿真實秒數，只是改成全速空轉。
    """
    saved = ws.time
    clock = clock if clock is not None else _FakeClock()
    ws.time = clock
    try:
        yield clock
    finally:
        ws.time = saved


# ---------- 靜態防線：driver-agnostic ---------------------------------------

def test_shared_is_driver_agnostic():
    """共用模組不可 import 任何 driver 套件（selenium / je_web_runner）。
    用 ast 檢查真正的 import 節點，而非原始碼子字串，才不會被 docstring /
    註解裡正當提到的套件名稱誤觸。"""
    print("test_shared_is_driver_agnostic")
    src = Path(ws.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden_roots = {"selenium", "je_web_runner"}
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in forbidden_roots:
                    bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in forbidden_roots:
                bad.append(node.module or "")
    assert not bad, (
        f"_webrunner_shared.py 必須 driver-agnostic，卻 import 了：{bad}")
    print("  PASS\n")


# ---------- 純函式行為 ------------------------------------------------------

def test_pair_todos_padding():
    print("test_pair_todos_padding")
    # 全空 → []
    assert ws.pair_todos([], [], [], []) == []
    # 短清單重複最後一筆做 padding；空清單變空字串。
    assert ws.pair_todos(["P"], ["a", "b", "c"], [], []) == [
        ("P", "a", "", ""),
        ("P", "b", "", ""),
        ("P", "c", "", ""),
    ]
    print("  PASS\n")


def test_character_folder_name():
    print("test_character_folder_name")
    # 取首個半形逗號前的片段；非法檔名字元換成底線。
    assert ws.character_folder_name("Alice, blue hair, smile") == "Alice"
    assert ws.character_folder_name("a/b:c") == "a_b_c"
    # 空字串退回 'character'。
    assert ws.character_folder_name("") == "character"
    print("  PASS\n")


# --------------------------------------------------------------------------
# 資料夾名投影：逃逸與保留裝置名
#
# 2026-09-10 修的實際缺陷。`character_folder_name` 原本是字元黑名單
# （`re.sub(r'[\\/:*?"<>|]+', ...)`，**沒有 `.`**），於是 `todo_character1.md`
# 裡一行 `..` 會原封不動變成資料夾名，`Path("output") / ".."` 不被 pathlib
# 正規化，`allocate_output_dir` 直接回傳它，`.resolve()` 就是 `PROJECT_ROOT`
# ——一整個角色的圖安靜地寫進 repo 根目錄。
#
# ⚠️ **上面那支 `test_character_folder_name` 為什麼沒抓到，是這一批測試存在的
# 真正理由**：它四格裡**沒有一格含 `.`**。它測的是 docstring 舉的例子，不是威脅
# 類別——「guards written from examples」那條教訓的教科書形狀。所以下面刻意從
# **機制**反推語料（點、磁碟機錨定、保留裝置名、控制字元、尾端點），而不是從
# 新 docstring 舉的例子反推。
# --------------------------------------------------------------------------

# 每一格都附「為什麼這一格會出事」，因為光看字串看不出來。
_ESCAPING_INPUTS = [
    ("..", "output/.. 解析成 PROJECT_ROOT——真正的逃逸"),
    ("..,blue eyes", "首段就是 ..，後面的 tag 不影響"),
    ("  ..  , tag", "head.strip() 之後還是 .."),
    ("../../Windows/Temp/x", "黑名單版本給 .._.._Windows_Temp_x（bot 打不開）"),
    (".", "output/. 解析成 output 自己——圖倒進 output/ 根目錄"),
    ("...", "同上；Windows 砍掉尾端點之後等於 ."),
    ("....", "同上"),
    ("a..b", "留在 output/ 裡，但 bot 的 '..' in name 會擋 → 鏡像不一致"),
    ("..foo", "同上"),
    ("a\tb", "mkdir 會拋 OSError 123，而 generate_loop 的 mkdir 外面沒有 try"),
    ("a\x01b", "同上"),
]

_RESERVED_INPUTS = [
    "NUL", "con,tag", "CON", "prn", "AUX", "nul.txt", "COM1", "LPT9",
    "COM0", "conin$",
]

# 反向邊界：這些**一個都不准**被動到。全是 output/ 與 WEBRunner.log 裡真的
# 出現過的形狀，或使用者實際會填的東西。
_UNTOUCHED_INPUTS = [
    ("surtr (arknights), large breasts", "surtr (arknights)"),
    ("columbina (genshin impact)", "columbina (genshin impact)"),
    ("pramanix the prerita (arknights)", "pramanix the prerita (arknights)"),
    ("kei (student) (blue archive), smile", "kei (student) (blue archive)"),
    ("名字裡有中文, tag", "名字裡有中文"),
    ("角色名（全形括號）", "角色名（全形括號）"),
    ("CONS", "CONS"),            # 不是保留名——只有整段相符才算
    ("NULL", "NULL"),
    ("COMA", "COMA"),
    (".hidden", ".hidden"),      # 開頭的點合法，不是穿越
    ("a/b:c", "a_b_c"),          # 原本的替換行為必須留著
    ("Alice, blue hair, smile", "Alice"),
]


def test_character_folder_name_never_escapes_the_output_root():
    """**這一支釘的是真正的不變式，不是字串長相。**

    字串斷言會漂——有人換個退回預設值、或改成別的消毒方式，斷言就得跟著改，
    而「圖到底有沒有寫在 `output/` 裡面」這個問題會在改的過程中不見。所以這裡
    問的是組合性質：`(OUTPUT_ROOT / character_folder_name(x)).resolve()` 一定
    嚴格落在 `OUTPUT_ROOT.resolve()` 底下。

    用 `in resolved.parents` 而不是字串 `startswith`：`parents` 比對的是已經
    `resolve()` 過的形式，而字串前綴會把 `output_2/` 誤判成在 `output/` 底下。
    也刻意排除等於 `OUTPUT_ROOT` 自己——`output/.` 與 `output/...` 都解析成
    `output` 本身，那不是逃逸卻同樣有害（圖倒進 `output/` 根目錄，把
    `allocate_output_dir` 的編號邏輯一起弄亂），所以它必須算失敗。
    """
    print("test_character_folder_name_never_escapes_the_output_root")
    root = ws.OUTPUT_ROOT.resolve()
    corpus = ([s for s, _ in _ESCAPING_INPUTS] + _RESERVED_INPUTS
              + [s for s, _ in _UNTOUCHED_INPUTS]
              + ["", "   ", ",", ",,,", "C:/Windows/Temp/x", "C:", "c:x",
                 r"\\server\share", "x" * 300, "y" * 119 + "." + "z" * 50,
                 "a. . .", "Mr. Smith., tag", "a ", "a."])
    for raw in corpus:
        name = ws.character_folder_name(raw)
        assert name, f"投影絕不可以回空字串（{raw!r}）"
        resolved = (root / name).resolve()
        assert root in resolved.parents, (
            f"{raw!r} → {name!r} → {resolved}，跑出 {root} 之外或等於它本身")
    print(f"  {len(corpus)} 格全部落在 output/ 底下")
    print("  PASS\n")


def test_character_folder_name_rejects_the_dot_traversals():
    """點的那一族全部退回安全預設。

    `'..'` 是唯一真的逃得出 `output/` 的一格（實測 `.resolve()` 等於
    `PROJECT_ROOT`），其餘幾格解析成 `output` 自己或只是讓 bot 打不開，
    但四種都必須擋——判準是「合法的單一層資料夾名」，不是「會不會逃逸」。
    """
    print("test_character_folder_name_rejects_the_dot_traversals")
    for raw, why in _ESCAPING_INPUTS:
        got = ws.character_folder_name(raw)
        assert got == "character", f"{raw!r} → {got!r}（{why}）"
    print("  PASS\n")


def test_character_folder_name_rejects_the_windows_device_names():
    """`NUL` 那一族退回預設。

    ⚠️ **本機實測的壞法跟直覺不同，別把這支的理由寫成「mkdir 會拋」**（Windows
    11 / CPython 3.14）：`CON`／`AUX`／`PRN`／`COM1`／`NUL.txt` 當目錄其實都建
    得起來也寫得進去，真正壞掉的只有 `NUL`——而它
    `mkdir(parents=True, exist_ok=True)` **不會拋**（`exists()` True、`is_dir()`
    False，因為那是 null 裝置），然後**每一張圖**的寫入都 `FileNotFoundError`。
    症狀是「整個角色一張都存不下來」，最後由 `consecutive_fail_abort` 收場。
    整組都擋是刻意的：那是平台與版本相關的行為。
    """
    print("test_character_folder_name_rejects_the_windows_device_names")
    for raw in _RESERVED_INPUTS:
        got = ws.character_folder_name(raw)
        assert got == "character", f"{raw!r} → {got!r} 是 Windows 保留裝置名"
    print("  PASS\n")


def test_character_folder_name_leaves_ordinary_names_alone():
    """反向邊界。收緊的代價必須是零——退回 `"character"` 會讓不同角色的圖擠進
    同一串編號資料夾，所以誤擋比漏擋更容易被忽略（沒有例外、沒有警告，只是
    資料夾名變醜）。含 CJK、全形括號、開頭的點、以及三個**看起來像**保留名但
    不是的（整段相符才算）。"""
    print("test_character_folder_name_leaves_ordinary_names_alone")
    for raw, want in _UNTOUCHED_INPUTS:
        got = ws.character_folder_name(raw)
        assert got == want, f"{raw!r} → {got!r}，期望 {want!r}"

    # 截斷仍是 120，而且截斷之後如果剛好留下尾端點也要收乾淨（Windows 會默默
    # 砍掉它，不砍的話磁碟上的名字跟檢查點記的名字會不一樣）。
    assert len(ws.character_folder_name("x" * 300)) == 120
    trimmed = ws.character_folder_name("y" * 119 + "." + "z" * 50)
    assert trimmed == "y" * 119, trimmed
    assert ws.character_folder_name("Mr. Smith., tag") == "Mr. Smith"
    print("  PASS\n")


def test_the_folder_component_rules_do_not_mask_each_other():
    """`_is_safe_folder_component` 的每一條規則都要有一個**只踩它**的輸入。

    這一支不是為了覆蓋率，是為了讓變異測試講真話：規則互相重疊的時候，刪掉
    任何一條都還是紅的，於是「守住了」是假的（本 repo 反覆出現的「兩道防護
    互相遮蔽」）。下面每一格都驗過——把對應那一條規則單獨拿掉，只有這一格會
    從 False 變 True。

    ⚠️ **「非空」那條在這張表裡沒有隔離輸入，那是刻意的，不是漏網。** 今天
    `PureWindowsPath("").parts` 是 `()`，所以「剛好一層」那條已經先擋掉空字串
    ——也就是說在**今天的 stdlib 下**，任何輸入都踩不到「只有非空那條擋得住」
    的狀態，這張表本來就造不出那一格。它的隔離輸入在下一支測試
    `test_the_empty_name_rule_holds_if_pathlib_reverts_to_its_pre_312_shape`，
    那支把 stdlib 的行為換掉再問同一個問題。下面那句 `_PWP("")` 斷言把「今天
    為什麼是重複的」釘住：它一旦紅了，代表 stdlib 真的變了、那條規則從重複
    變成唯一防線，這張表要跟著補一格。
    """
    print("test_the_folder_component_rules_do_not_mask_each_other")
    safe = ws._is_safe_folder_component

    # 空字串必須被擋（`".."` 經過尾端點正規化就是空字串，所以真的到得了）。
    # 今天擋它的是「剛好一層」那條，不是「非空」那條——把這個事實釘住。
    assert safe("") is False
    from pathlib import PureWindowsPath as _PWP
    assert len(_PWP("").parts) != 1, (
        "stdlib 變了：`PureWindowsPath('')` 現在是單一層。那表示"
        "`_is_safe_folder_component` 的「非空」規則從重複變成唯一防線"
        "（`OUTPUT_ROOT / '' == OUTPUT_ROOT`），上面那張表要跟著更新。")

    isolating = {
        "a.":          "尾端點（Windows 會默默砍掉 → 名字與磁碟不一致）",
        "a ":          "尾端空白（同上）",
        " a":          "開頭空白",
        "a\tb":        "控制字元（mkdir 會拋 OSError 123）",
        "a..b":        "含 ..（bot 的子字串判定會擋 → 鏡像不一致）",
        "..foo":       "含 ..",
        "C:x":         "磁碟機錨定",
        "a/b":         "不只一層",
        "NUL":         "保留裝置名",
        "nul.txt":     "保留裝置名 + 副檔名",
    }
    for name, why in isolating.items():
        assert safe(name) is False, f"{name!r} 應該被擋（{why}）"

    # 正面對照組：斷言把上面整批全擋掉的實作（例如 `return False`）會紅。
    for name in ["surtr (arknights)", "名字", ".hidden", "CONS", "a_b_c",
                 "columbina (genshin impact)_2", "x" * 120]:
        assert safe(name) is True, f"{name!r} 是合法的單一層資料夾名"
    print("  PASS\n")


class _pathlib_before_312:
    """3.12 之前 `PurePath('')` 的形狀：`parts == ('.',)`、無 drive、無 root。

    ⚠️ **不能靠包真的 `PureWindowsPath(".")` 來造。** 現代 pathlib 連那一個也
    正規化掉了（本機實測 `PureWindowsPath(".").parts == ()`），所以要模擬的那
    個形狀在今天的 stdlib 裡**根本造不出來**——只能自己端一個 stub 出來。

    只有「空 parts」那一格跟真貨不同（今天只有 `""` 與 `"."` 這類指向目前目錄
    的輸入會落到那裡，而它們在 3.12 之前給的正是 `PurePath('.')`），其餘欄位
    原封不動轉發。多換一格就變成在測一個沒人主張過的世界。
    """

    def __init__(self, name: str):
        from pathlib import PureWindowsPath as _PWP
        real = _PWP(name)
        self.drive = real.drive
        self.root = real.root
        self.parts = real.parts or (".",)


def test_the_empty_name_rule_holds_if_pathlib_reverts_to_its_pre_312_shape():
    """`_is_safe_folder_component` 的「非空」那條要**自己**擋得住空字串。

    這支存在的理由很具體：2026-09-10 對 `character_folder_name` 跑 10 個變異，
    唯一存活的就是「拿掉 `if not name: return False`」。那個存活是列冊的、理由
    也是對的（今天有「剛好一層」那條頂著），但**變異報告上一個長期存在的
    SURVIVED 就是在邀請下一個人把它刪掉**——沒有任何東西證明它有用。

    ⚠️ **單純 `assert safe("") is False` 殺不掉那個變異**：變異套用之後它照樣
    回 False，因為擋它的是別條規則。唯一問得出「非空那條自己行不行」的方法，
    是把它今天依賴的那個前提拿掉——也就是模擬 stdlib 變回去。

    為什麼那個前提真的會變：`PureWindowsPath("").parts` 在本機（CPython
    3.14.4）是 `()`，但 `PurePath("")` 在 3.12 之前給的是 `PurePath('.')`
    ——**這是 stdlib 的實作細節，而且真的變過一次**。變回去的話空字串會一路
    通過，而 `OUTPUT_ROOT / ""` **就等於 `OUTPUT_ROOT` 自己**，一整個角色的圖
    無聲倒進 `output/` 根目錄，順便把 `allocate_output_dir` 的編號邏輯弄亂。
    空字串**確實到得了這裡**：`".."` 經過尾端點正規化就是空字串。
    """
    print("test_the_empty_name_rule_holds_if_pathlib_reverts_to_its_pre_312_shape")
    # 先確認替身真的重現了 3.12 之前那個形狀（無 drive、無 root、剛好一層）。
    # 不驗的話，替身自己壞掉會讓這支測試變成「什麼都沒問」的綠燈。
    fake_empty = _pathlib_before_312("")
    assert fake_empty.parts == (".",), fake_empty.parts
    assert not fake_empty.drive and not fake_empty.root

    original = ws.PureWindowsPath
    ws.PureWindowsPath = _pathlib_before_312
    try:
        # 正面對照組：確認「剛好一層」那條在這個世界裡**真的已經不擋了**。
        # 沒有這句，替身沒生效（例如函式改成 local import）也會綠。
        assert len(ws.PureWindowsPath("").parts) == 1, (
            "替身沒生效——那接下來那句斷言就不是在問「非空那條自己行不行」")
        assert ws._is_safe_folder_component("") is False, (
            "stdlib 變回 3.12 之前的形狀之後，空字串一路通過了。"
            "`OUTPUT_ROOT / \"\"` 就等於 `OUTPUT_ROOT` 自己——一整個角色的圖會"
            "無聲倒進 output/ 根目錄。`_is_safe_folder_component` 的"
            "「非空」那條不能拿掉。")
        # 連帶：投影出來的名字也不能變成空字串（`".."` 會走到這條路）。
        assert ws.character_folder_name("..") == "character"
    finally:
        ws.PureWindowsPath = original
    print("  PASS\n")


def test_the_folder_projection_agrees_with_the_bot_guard():
    """投影的值域必須被 bot 的 `_is_unsafe_folder_name` 全部放行。

    這是**跨模組耦合**，不是本地性質，所以要有一支測試釘住：webrunner 建資料夾、
    bot 讀它（`/out sample`、`/out list`、收藏）。2026-09-10 之前兩邊對不起來
    ——`character_folder_name('../../Windows')` 給 `'.._.._Windows'`，webrunner
    會照建，而 bot 用子字串 `".." in name` 判定，所以**bot 打不開一個 webrunner
    自己建出來的資料夾**，症狀只是 `/out sample` 對某個角色安靜地失敗。

    刻意不 import `discord_bot`（那會拖進 discord.py 與整個 bot 模組，而且
    webrunner 測試不該依賴它）——改成在這裡就地重做 bot 那支守衛的判準，並用
    AST 確認它還是同一組條件。判準本身很短，重做比 import 便宜。
    """
    print("test_the_folder_projection_agrees_with_the_bot_guard")
    from pathlib import PureWindowsPath as _PWP

    def _bot_is_unsafe(name: str) -> bool:
        if not name:
            return False
        if "/" in name or "\\" in name or ".." in name:
            return True
        pure = _PWP(name)
        if pure.drive or pure.root:
            return True
        return len(pure.parts) != 1

    # 先確認上面這份重做仍與 bot 的原始碼同構（否則這支會在 bot 改過之後
    # 繼續綠著，測一個已經不存在的守衛）。
    bot_src = ((Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py")
               .read_text(encoding="utf-8"))
    tree = ast.parse(bot_src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef)
              and n.name == "_is_unsafe_folder_name")
    body_src = ast.unparse(fn.body)
    for token in ['"/" in name', "'\\\\' in name", "'..' in name",
                  "pure.drive", "pure.root", "len(pure.parts) != 1"]:
        assert token in body_src, (
            f"bot 的 _is_unsafe_folder_name 已經改了（找不到 {token}），"
            "這支測試裡的重做要跟著更新")

    corpus = ([s for s, _ in _ESCAPING_INPUTS] + _RESERVED_INPUTS
              + [s for s, _ in _UNTOUCHED_INPUTS]
              + ["", "C:/Windows/Temp/x", "x" * 300, "a. . .", "a<@123>b"])
    for raw in corpus:
        name = ws.character_folder_name(raw)
        assert not _bot_is_unsafe(name), (
            f"{raw!r} → {name!r}：webrunner 建得出來，bot 卻打不開")
    print(f"  {len(corpus)} 格投影出來的名字 bot 全部放行")
    print("  PASS\n")


def test_the_folder_projection_still_neutralises_mention_syntax():
    """`<` `>` 必須繼續被換成底線——這是**別的模組的安全性質靠在這裡**。

    `discord_bot._alert_mentions()` 的 docstring 明寫警報訊息之所以不是 ping
    放大器，靠的就是這裡的 `re.sub` 順手讓 `<@123>` 湊不成 mention（角色名是
    使用者用 `/todo char1 add` 填的），並警告「哪天有人放寬它，或改成 POSIX
    的字元集，這裡就會安靜地變回一個 ping 放大器」。

    所以 2026-09-10 收緊的作法是**保留原本的替換、在後面加白名單斷言**。這支
    釘住那個方向：把黑名單換成一組更寬鬆的字元集會讓它變紅。
    """
    print("test_the_folder_projection_still_neutralises_mention_syntax")
    for raw in ["<@123456789012345678>", "a<@&999>b", "tag <@!42>",
                "<@everyone>", "@everyone <@1>"]:
        got = ws.character_folder_name(raw)
        assert "<" not in got and ">" not in got, f"{raw!r} → {got!r}"
    print("  PASS\n")


def test_read_todo_characters_normalises():
    print("test_read_todo_characters_normalises")
    import tempfile
    d = tempfile.mkdtemp(prefix="ws_test_")
    try:
        p = Path(d) / "todo.md"
        # NBSP（U+00A0）正規化成一般空白、跳過空白行。
        p.write_text("a\xa0b\n\n   \nc\n", encoding="utf-8")
        assert ws.read_todo_characters(p) == ["a b", "c"]
        assert ws.read_todo_characters(
            p, preserve_blank=True) == ["a b", "", "", "c"]
        # 缺檔 → []
        assert ws.read_todo_characters(Path(d) / "missing.md") == []
    finally:
        for q in Path(d).iterdir():
            try:
                q.unlink()
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass
    print("  PASS\n")


def test_set_character2_enabled_removes_and_readds_slot():
    """Empty char2 removes its card; a later non-empty char2 adds it back."""
    print("test_set_character2_enabled_removes_and_readds_slot")
    saved = {
        "count_characters": ws.count_characters,
        "remove_character_slot": ws.remove_character_slot,
        "_click_gender": ws._click_gender,
        "human_pause": ws.human_pause,
        "snap": ws.snap,
    }
    state = {"count": 2, "removed": [], "added": 0}

    class _Button:
        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

    class _Port:
        def find_elements_xpath(self, xpath):
            assert "Add Character" in xpath
            return [_Button()]

        def execute_script(self, *_a):
            return None

        def click(self, _button):
            state["count"] = 2
            state["added"] += 1

    try:
        ws.count_characters = lambda _port: state["count"]

        def _remove(_port, label):
            state["removed"].append(label)
            state["count"] = 1
            return True

        ws.remove_character_slot = _remove
        ws._click_gender = lambda *_a, **_k: True
        ws.human_pause = lambda *_a, **_k: None
        ws.snap = lambda *_a, **_k: None
        port = _Port()
        assert ws.set_character2_enabled(port, False) is True
        assert state["removed"] == ["Character 2"]
        assert state["count"] == 1
        assert ws.set_character2_enabled(port, True) is True
        assert state["added"] == 1
        assert state["count"] == 2
    finally:
        for name, value in saved.items():
            setattr(ws, name, value)
    print("  PASS\n")


_CHARACTER2_CASES = [
    # (enabled, 依序的卡片數, 動作回報, 期望回傳, 動作次數, 快照次數)
    # 已經是要的狀態：不動任何控制項（多點一次「加角色」會變出第三張卡）。
    (True, [2], True, True, 0, 0),
    (True, [3], True, True, 0, 0),
    (False, [1], True, True, 0, 0),
    (False, [0], True, True, 0, 0),
    # 動作本身失敗。
    (True, [1], False, False, 1, 0),
    (False, [2], False, False, 1, 1),
    # 動作回報成功，但卡片數沒有變成要的樣子——以**結果**為準，不以點選為準。
    (True, [1, 1], True, False, 1, 1),
    (False, [2, 2], True, False, 1, 1),
    # 正常路徑。
    (True, [1, 2], True, True, 1, 0),
    (False, [2, 1], True, True, 1, 0),
]


def test_character2_enabled_reports_what_the_page_ended_up_with():
    """回 False 的那幾條是呼叫端唯一的訊號：Character 2 的卡該拿掉卻還在，上一對的角色
    提示詞會漏進下一張圖；該加卻沒加，char2 的提示詞會寫進不存在的卡片。

    不用 `parametrize`／`monkeypatch`：這個檔案有自帶 runner，只餵得起 `tmp_path`。"""
    print("test_character2_enabled_reports_what_the_page_ended_up_with")
    names = ("count_characters", "click_add_character_control",
             "remove_character_slot", "snap", "human_pause")
    saved = {name: getattr(ws, name) for name in names}
    try:
        for case in _CHARACTER2_CASES:
            enabled, counts, action_ok, expected, acted, snapped = case
            seq = iter(counts)
            last = [None]
            actions: list = []
            snaps: list = []

            def _count(_port, seq=seq, last=last):
                last[0] = next(seq, last[0])
                return last[0]

            ws.count_characters = _count
            ws.click_add_character_control = (
                lambda _port, gender, actions=actions, ok=action_ok:
                actions.append(("add", gender)) or ok)
            ws.remove_character_slot = (
                lambda _port, label, actions=actions, ok=action_ok:
                actions.append(("remove", label)) or ok)
            ws.snap = lambda _port, name, snaps=snaps: snaps.append(name)
            ws.human_pause = lambda *_a, **_k: None
            assert ws.set_character2_enabled(object(), enabled) is expected, case
            assert len(actions) == acted, (case, actions)
            if actions:
                assert actions[0] == (("add", "Female") if enabled
                                      else ("remove", "Character 2")), case
            assert len(snaps) == snapped, (case, snaps)
    finally:
        for name, value in saved.items():
            setattr(ws, name, value)
    print("  PASS\n")


def test_expand_character_section_is_result_driven_not_click_driven():
    """展開角色卡是「以結果為準」的操作，不是「點固定次數」的操作。

    V4.5 的語意是 focus（只有 focus 的卡看得到欄位）、V5 是展開（展開後所有卡
    的欄位同時可見），但兩者**可觀察的結果**一樣：這張卡自己的 prompt 欄拿不拿
    得到。所以：

    * 卡片已經開著 → **一次都不該點**。舊實作靠「文字有沒有變」猜狀態，相等或
      空白的 prompt 會讓它沿著祖先連點最多五層，最後 focus 到錯的卡。
    * 卡片收合中 → 點 unfold，並以「欄位是否出現」判定成功，不是以點選次數。
    """
    print("test_expand_character_section_is_result_driven_not_click_driven")
    old_sleep = ws.time.sleep
    saved_pause = ws.human_pause
    ws.time.sleep = lambda _seconds: None
    ws.human_pause = lambda *_a, **_k: None
    try:
        port = FakeBrowserPort()
        assert ws.expand_character_section(port, "Character 1") is True
        assert port.clicks == 0, (
            f"already-open card must not be clicked at all, got {port.clicks}")

        collapsed = FakeBrowserPort()
        collapsed.collapsed = {2}
        assert ws.character_card_area(collapsed, "Character 2") is None
        assert ws.expand_character_section(collapsed, "Character 2") is True
        assert collapsed.clicks == 1, (
            f"collapsed card should need exactly one unfold click, "
            f"got {collapsed.clicks}")
        assert ws.character_card_area(collapsed, "Character 2") is not None

        missing = FakeBrowserPort()
        missing.char_count = 1
        assert ws.expand_character_section(missing, "Character 2") is False
    finally:
        ws.time.sleep = old_sleep
        ws.human_pause = saved_pause
    print("  PASS\n")


# ---------- FakeBrowserPort + single-image serve dry-run (P6 C4) ------------
# Drives `serve_single_image_request` with NO browser. The fake answers every
# `port.*` the serve path reaches (through the shared DOM leaves) plausibly
# enough to run end-to-end, and records what happened so the two character
# strategies (idle delete+clear vs in-band fill+verify) and the
# "exactly ONE single_image_done per request" invariant can be asserted.

import base64 as _base64  # noqa: E402


class _FakeElement:
    """Minimal stand-in for a Selenium WebElement (textarea / button)."""

    def __init__(self, tag="TEXTAREA"):
        self.tag_name = tag
        self.value = ""
        self.text = ""

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def click(self):
        pass

    def send_keys(self, *_a):
        pass


class FakeBrowserPort:
    """No-browser BrowserPort: services serve_single_image_request's needs and
    logs effects. `get_main_image_src` returns a stable src that advances by
    exactly one step whenever the Generate button is located (so
    wait_for_new_image sees a single new, stable image)."""

    TRANSPORT_ERRORS = ()  # the fake never raises transport-level errors

    def __init__(self):
        self.main = _FakeElement()
        self.char1 = _FakeElement()
        self.char2 = _FakeElement()
        self.areas = [self.main, self.char1, self.char2]
        self.undesired_area = _FakeElement()
        self.char_count = 2
        self.collapsed = set()      # 收合中的角色卡 index（V5 語意）
        self._src_n = 0
        self.set_values = []        # every native-setter write (text)
        self.count_called = False   # remove_all_character_slots ran?
        self.clicks = 0
        self.native_clicks = []     # click_native 收到的元素（依序）

    # ---- 角色卡 helper（V5）----
    def _char_index(self, label):
        try:
            return int(str(label).strip().split()[-1])
        except (ValueError, IndexError):
            return None

    def _has_char(self, label):
        n = self._char_index(label)
        return n is not None and 1 <= n <= self.char_count

    def _char_area(self, label):
        return {1: self.char1, 2: self.char2}.get(self._char_index(label))

    def _card_is_open(self, label):
        """卡片展開＝拿得到它自己的 prompt 欄。預設全開。"""
        return self._char_index(label) not in self.collapsed

    # ---- JS ----
    def execute_script(self, script, *args):
        s = script
        # 存活探針（_abort_if_browser_gone）。假 port 對未知 script 一律回 None，
        # 而「回 None」正是探針判定「瀏覽器沒了」的依據——不回 token 的話，
        # 每一個跑到失敗分支的測試都會被誤判成 BrowserGoneError。
        if ws._ALIVE_PROBE_TOKEN in s:
            return ws._ALIVE_PROBE_TOKEN
        # ↓↓ V5 角色卡 API：**必須排在下面那些粗略的子字串比對之前**。
        # `_JS_CHAR_HELPERS` 的註解本身就含有 'trash'／'check' 字樣，排在後面
        # 會被 remove_character_slot 的分支攔截，回一顆按鈕給預期是清單的呼叫端。
        if "return __findCharName(arguments[0]);" in s:
            label = args[0] if args else ""
            return _FakeElement("INPUT") if self._has_char(label) else None
        if "return __cardIcon(arguments[0], 'unfold');" in s:
            # 收合中才給得出 unfold 鈕；已展開就沒有東西可按。
            return _FakeElement("BUTTON") if self.collapsed else None
        if "return __charHeaderRow(arguments[0]);" in s:
            return None
        # 標記要夠精確： 也出現在 helpers 的**函式定義**裡，
        # 拿它當標記等於攔截所有含 helpers 的 script。
        if "const card = __charCard(nameEl);" in s:  # character_card_area
            label = args[0] if args else ""
            if not self._has_char(label) or not self._card_is_open(label):
                return None
            return self._char_area(label)
        if "b.outerHTML.slice" in s:                # debug_character_buttons
            return []
        if "scrollIntoView" in s or ".blur()" in s:
            return None
        if "getOwnPropertyDescriptor" in s and "execCommand" in s:
            # native-setter fill: record + reflect on the element
            if args:
                el, val = args[0], (args[1] if len(args) > 1 else "")
                self.set_values.append(val)
                if isinstance(el, _FakeElement):
                    el.value = val
            return None
        if "? e.value :" in s:                      # _read_textarea_value
            el = args[0] if args else None
            return el.value if isinstance(el, _FakeElement) else ""
        if "KeyboardEvent" in s:                    # _dismiss_autocomplete
            return None
        if "match(/^Character" in s:                # count_characters
            self.count_called = True
            return self.char_count
        if "delete|remove|trash|bin" in s:          # remove_character_slot finder
            return _FakeElement("BUTTON")
        if "Generate" in s:                         # find_generate_button
            self._src_n += 1                        # a generate produced a new image
            return _FakeElement("BUTTON")
        if "blob:" in s or "naturalWidth" in s:     # get_main_image_src
            return f"blob:fake-{self._src_n}"
        return None

    def execute_async_script(self, script, *args):  # download_image fetch
        # **每一條 src 要餵出不同的 bytes**。原本不論 src 是什麼都回同一串
        # `b"fakepng"`，也就是這個假瀏覽器聲稱「站方每次都端出完全一樣的圖」
        # ——那是正式環境裡的異常狀況（2026-08-27 在正式輸出裡實測到的發生率
        # 是 120 張裡 1 張），拿它當所有迴圈測試的常態，內容層去重的行為就永遠
        # 測不出來。src 每張都不同，就用它派生。
        body = b"fakepng:" + str(args[0] if args else "").encode()
        return "data:image/png;base64," + _base64.b64encode(body).decode()

    # ---- element lookup ----
    def find_elements_xpath(self, xpath):
        x = xpath.lower()
        if "undesired" in x:
            return [self.undesired_area]
        if "character " in x:                       # expand_character_section header
            return [_FakeElement("DIV")]
        if "contenteditable" in x:                  # find_prompt_areas
            return list(self.areas)
        return []

    def find_element_xpath(self, xpath):
        return None

    # ---- interaction / page ----
    def click(self, element, pause: float = 0.15):
        self.clicks += 1
        # 按 unfold 就把收合的卡打開（V5：展開後該卡的 prompt 欄才拿得到）。
        if getattr(element, "tag_name", "") == "BUTTON" and self.collapsed:
            self.collapsed.clear()

    def click_native(self, element):
        # 真實 port 走 W3C element click（會做遮擋檢查）。這裡只記下**被交出去的
        # 是哪一顆**——安全性質的判定點已經從「不會 click 它」搬成「JS 不會回傳
        # 它」，所以要能斷言的是這份清單。
        self.native_clicks.append(element)

    def ancestors(self, element, depth: int = 4):
        return [element]

    def mouse_wiggle(self):
        return None

    def press_enter(self, element):
        pass

    def press_escape(self):
        # 真實 port 會送 driver 層的按鍵；這裡只記次數，讓呼叫端測得到。
        self.real_escapes = getattr(self, "real_escapes", 0) + 1
        return True

    def get(self, url):
        pass

    def current_url(self):
        return ""

    def get_title(self):
        return ""

    def refresh(self):
        pass

    def save_screenshot(self, path):
        return True


class _ServeHarness:
    """Patch ws.emit_event (recorder), ws.human_pause (no-op for speed), and
    ws.SINGLE_IMAGE_OUTPUT_ROOT (tmp) for one serve; restore on exit."""

    def __enter__(self):
        self.events = []
        self._saved = (ws.emit_event, ws.human_pause,
                       ws.SINGLE_IMAGE_OUTPUT_ROOT,
                       ws.verify_character_prompt)
        self._dir = tempfile.mkdtemp(prefix="serve_test_")
        ws.emit_event = lambda etype, **kw: self.events.append((etype, kw))
        ws.human_pause = lambda *a, **k: None
        ws.SINGLE_IMAGE_OUTPUT_ROOT = Path(self._dir)
        ws.verify_character_prompt = lambda *_a, **_k: True
        return self

    def __exit__(self, *exc):
        (ws.emit_event, ws.human_pause, ws.SINGLE_IMAGE_OUTPUT_ROOT,
         ws.verify_character_prompt) = self._saved
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def single_image_events(self):
        return [kw for et, kw in self.events if et == "single_image_done"]


def test_serve_in_band_fills_and_verifies():
    print("test_serve_in_band_fills_and_verifies")
    port = FakeBrowserPort()
    req = {"request_id": "rid-inband", "prompt": "P",
           "char1": "C1", "char2": "C2", "undesired": "U"}
    with _ServeHarness() as h, _fake_clock():
        ws.serve_single_image_request(port, req, in_band=True)
        evs = h.single_image_events()
    assert len(evs) == 1, f"in-band must emit exactly ONE event, got {len(evs)}"
    assert evs[0]["ok"] is True, evs[0]
    assert evs[0].get("path"), "success event must carry a path"
    # in-band fills char1/char2 (and never removes slots)
    assert "C1" in port.set_values and "C2" in port.set_values, port.set_values
    assert "P" in port.set_values and "U" in port.set_values, port.set_values
    assert port.count_called is False, "in-band must NOT remove character slots"
    print("  PASS\n")


# ---------- 數不出角色框的時候不得回 None ------------------------------------
# `port.execute_script` 在型別上是 Any，而 Chrome 在頁面轉場或記憶體壓力下**真的**
# 會把 script 結果交回 null（OOM 是這台機器的主要故障模式）。raw None 會直接流進
# `remove_all_character_slots` 的 `while have > 0`，炸成
# `'>' not supported between instances of 'NoneType' and 'int'`。
#
# 這不是假想：`events.ndjson` 裡有一筆真的 —— 2026-06-27 一位使用者的即時產圖請求
# 就是被這個 raw TypeError 整個中止的。防線後來補在 `count_characters` 裡（把非 int
# 一律當 0），但一直沒有測試釘住它：所有既有測試都把 `count_characters` 整支換掉，
# 所以有人把 `int(raw)` 那層「簡化」回去也不會有任何東西變紅。
# ---------------------------------------------------------------------------


class _UncountablePort(FakeBrowserPort):
    """數角色框那段 script 回 None 的假站台（其餘照常）。"""

    def execute_script(self, script, *args):
        if "match(/^Character" in script:
            return None
        return super().execute_script(script, *args)


def test_count_characters_never_hands_back_none():
    print("test_count_characters_never_hands_back_none")
    with contextlib.redirect_stderr(io.StringIO()):
        got = ws.count_characters(_UncountablePort())
    assert got == 0 and isinstance(got, int), (
        f"數不出來要當成 0，不能把 None 交給呼叫端的 `while have > 0`，"
        f"實際回了 {got!r}")
    # 對照組：數得出來的時候照實回報，別把防線做成「永遠回 0」。
    assert ws.count_characters(FakeBrowserPort()) == 2
    print("  PASS\n")


def test_remove_all_slots_survives_an_uncountable_page():
    print("test_remove_all_slots_survives_an_uncountable_page")
    # 真正的墜機點。`remove_all_character_slots` 只在 idle 單圖那條路上呼叫——
    # 也正是 2026-06-27 那筆失敗事件所在的路徑。
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
        ws.remove_all_character_slots(_UncountablePort())   # 不得 raise
    print("  PASS\n")


def test_serve_idle_removes_and_clears():
    print("test_serve_idle_removes_and_clears")
    port = FakeBrowserPort()
    req = {"request_id": "rid-idle", "prompt": "P",
           "char1": "C1", "char2": "C2", "undesired": "U"}
    with _ServeHarness() as h, _fake_clock():
        ws.serve_single_image_request(port, req, in_band=False)
        evs = h.single_image_events()
    assert len(evs) == 1, f"idle must emit exactly ONE event, got {len(evs)}"
    assert evs[0]["ok"] is True, evs[0]
    # idle removes slots (count_characters ran) and clears residual areas to ""
    assert port.count_called is True, "idle must remove character slots"
    assert "" in port.set_values, "idle must clear surviving character areas to ''"
    # idle NEVER writes the request's char1/char2 content into a character box
    assert "C1" not in port.set_values and "C2" not in port.set_values, port.set_values
    assert "P" in port.set_values, "idle still fills the main prompt"
    print("  PASS\n")


def test_serve_empty_prompt_one_failure_event():
    print("test_serve_empty_prompt_one_failure_event")
    port = FakeBrowserPort()
    with _ServeHarness() as h:
        ws.serve_single_image_request(port, {"request_id": "rid-empty",
                                             "prompt": "   "}, in_band=False)
        evs = h.single_image_events()
    assert len(evs) == 1, f"empty prompt must emit exactly ONE event, got {len(evs)}"
    assert evs[0]["ok"] is False and evs[0].get("error") == "empty prompt", evs[0]
    print("  PASS\n")


def test_serve_rejects_a_request_id_that_would_escape_the_output_root():
    """`request_id` **就是輸出資料夾名**，而它來自磁碟上的請求檔。

    寫入端（`discord_bot._generate_request_id`）只產十六進位，所以今天不會出事
    ——但那是**寫入端**的性質，中間還隔著一個跨行程的 JSON 檔。讀取端必須自己
    講，這正是 `cmd_fav_show` 的 docstring 記下來的那條。

    每一格都要 emit **剛好一個** `ok=False`：呼叫端
    `check_single_image_request` 不論如何都會刪掉請求檔，所以少發一則事件等於
    讓 bot 那側的閘門卡到 TTL 逾時才自癒。
    """
    print("test_serve_rejects_a_request_id_that_would_escape_the_output_root")
    hostile = [
        "../../..",                 # 走出 repo
        "..\\..\\evil",             # Windows 分隔符
        "C:\\Windows\\Temp\\x",     # **整個換掉 base**，`output/_oneshot` 消失
        "a/b",                      # 不只一層
        "..",
        "NUL",                      # 保留裝置名
        "a.",                       # 尾端點：Windows 會默默砍掉，磁碟與檢查點分岔
    ]
    for rid in hostile:
        port = FakeBrowserPort()
        with _ServeHarness() as h:
            ws.serve_single_image_request(
                port, {"request_id": rid, "prompt": "P"}, in_band=False)
            evs = h.single_image_events()
        assert len(evs) == 1, f"{rid!r} 必須剛好發一則事件，實際 {len(evs)}"
        assert evs[0]["ok"] is False, (rid, evs[0])
        assert evs[0].get("error") == "invalid request id", (rid, evs[0])
        # 早退要早到連主 prompt 都沒填——否則白花一次填字與一次欄位清洗。
        assert not port.set_values, (
            f"{rid!r} 應該在碰瀏覽器之前就退出，實際填了 {port.set_values}")

    # 正面對照組：**正常形狀的 id 不可以被擋掉。** 少了這一格，一支「永遠回
    # False」的守衛在上面每一格都是綠的，而整條單圖產線靜靜地全掛。
    port = FakeBrowserPort()
    with _ServeHarness() as h:
        ws.serve_single_image_request(
            port, {"request_id": "19a4f2c1b8e-3f9c2a1d7e04", "prompt": "P"},
            in_band=False)
        evs = h.single_image_events()
    assert len(evs) == 1 and evs[0]["ok"] is True, evs
    print("  PASS\n")


def test_is_single_path_component_is_weaker_than_the_folder_guard():
    """`snap` 的檔名守衛刻意比 `_is_safe_folder_component` **弱**，兩者不可互換。

    弱的方向要有一個**只踩這個差別**的輸入，否則「用了比較弱的那支」這件事在
    測試上看不出來，下一個人「順手統一」成嚴的那支也不會有東西變紅——而代價是
    合法的診斷截圖無聲消失。
    """
    print("test_is_single_path_component_is_weaker_than_the_folder_guard")
    # 只踩差別的輸入：尾端點與 `..` 子字串對**檔名**是合法的，對資料夾名不是。
    # 注意 `NUL.png` 而不是 `debug_NUL.png`：保留裝置名那條比對的是**第一個點
    # 之前**的整段，所以加了前綴之後 `DEBUG_NUL` 根本不是裝置名，兩支都會放行，
    # 那個輸入就證明不了任何差別。（`snap` 永遠會補 `debug_` 前綴，所以這一格
    # 是在驗兩支判準的差異，不是在驗 `snap` 到得了的輸入。）
    for only_in_the_gap in ("debug_ready_a..png", "debug_ready_a.", "NUL.png"):
        assert ws._is_single_path_component(only_in_the_gap), only_in_the_gap
        assert not ws._is_safe_folder_component(only_in_the_gap), (
            f"{only_in_the_gap!r} 兩支都放行的話，這個測試就沒有在證明差別了")

    # 兩支都必須擋掉的：會改到目錄的那些。
    #
    # ⚠️ 這個迴圈原本只斷言弱的那支，而它的註解寫的是「兩支都」——於是
    # `_is_safe_folder_component` 這一半從來沒有被問過（2026-09-11 補上）。
    #
    # 裸的 `".."` 以前**不在**這個清單裡，而弱的那支放行它（`..` 原樣通過
    # `PureWindowsPath` 的 round-trip）。實測 `base / ".."` 的 `mkdir` 與寫入都會
    # 成功、落在上一層，所以這一格不是形式主義。`"..\\"` 今天也是 False，但理由是
    # **附帶的**（尾端分隔符讓 round-trip 失敗），不是因為 `..`——一起釘住，免得
    # 哪天前面那條被拿掉還以為有人守著。
    for escaping in ("..", ".", "../../evil", "..\\evil", "..\\", "a/b", ""):
        assert not ws._is_single_path_component(escaping), (
            f"{escaping!r} 會改到目錄，弱的那支也必須擋掉它")
        assert not ws._is_safe_folder_component(escaping), (
            f"{escaping!r} 嚴的那支更不該放行——`serve_single_image_request` "
            "後面緊接著 `mkdir(parents=True)`")

    # `"."` 今天是被 pathlib 擋掉的，不是被政策擋掉的。把那個前提釘住，否則 stdlib
    # 哪天改了，這一條會安靜失效（同 `_PWP("")` 那一格的作法）。
    from pathlib import PureWindowsPath as _PWP

    assert _PWP(".").name != ".", (
        "stdlib 變了：`PureWindowsPath('.').name` 現在是 '.'，"
        "`_is_single_path_component` 要跟著把 '.' 列成明文特例。")
    assert _PWP("..").name == "..", (
        "stdlib 變了：`..` 不再原樣通過 round-trip。那上面那條明文的 `..` 特例就"
        "從必要變成重複，函式的 docstring 要跟著重寫理由。")

    # 邊界：這些**不可以**被擋掉。它們長得像 `..`，但性質不同（本機實測，
    # CPython 3.14.4 / Windows 11）：
    #   `"a.." / "..a" / "a..b"` -> 留在 base 底下，寫得進去 ＝ 合法檔名
    #   `"..." / " .."`          -> 塌回 base，但寫入是 FileNotFoundError
    #                               ＝ **大聲失敗**，跟 `".."` 無聲寫到上一層不同級
    # 往這個方向收緊會當場擋掉 `debug_ready_a.`（上面證明「弱」用的輸入），也就是
    # 把 §8.33 裁定不可合併的三層嚴格度合併掉。要改這個政策的話這一格會變紅——
    # 那是刻意的，它應該是一個明確的決定，不是搭便車。
    for benign in ("...", "a..", " ..", "..a", "a..b", "debug_ready_a..png"):
        assert ws._is_single_path_component(benign), (
            f"{benign!r} 不是「會改到目錄」，只是難看的檔名")

    # 真實的 tag 形狀一律放行（26 個呼叫端的三種代表）。
    for legit in ("debug_no_model_selector.png", "debug_char1_0003.png",
                  "debug_ready_columbina (genshin impact).png"):
        assert ws._is_single_path_component(legit), legit


def test_no_snap_tag_can_steer_the_screenshot_out_of_the_project_root():
    """`snap` 是弱守衛唯一一個吃**自由字串**的呼叫點——範圍側的釘子。

    §8.33 裁定 `snap` 可以用弱的那支（`_is_single_path_component`），理由是 `tag`
    只貢獻檔名片段。那條裁定建立在兩個前提上，而在此之前**一個都沒有測試**：全
    repo 從來沒有任何一支測試真的執行過 `snap` 的守衛，`ws.snap` 一律被換成 stub。

      1. 守衛真的被套在這個呼叫點上；
      2. 送進守衛的是 `f"debug_{tag}.png"` 而不是 `tag`——那個**字面前綴**才是
         「裸 `..` 到不了這裡」的結構性理由（`tag=".."` 產出的是 `"debug_...png"`，
         實測寫在 repo 根目錄裡，不是上一層）。

    拿掉 (1) 的話，`tag="../../evil"` 會讓截圖寫到 repo 外面，而且父目錄存在所以是
    **寫得成功**的、不是報錯——跟本專案其他無聲寫錯地方的缺陷同一族。

    這支不驗判準本身（那是上一支的事），它驗的是**呼叫端有沒有維持那個前提**。
    假 port 只記路徑、不寫檔，所以跑這支不會在 repo 根目錄留下任何 `debug_*.png`。
    """
    print("test_no_snap_tag_can_steer_the_screenshot_out_of_the_project_root")

    class _Recorder:
        def __init__(self):
            self.paths = []

        def save_screenshot(self, path):
            self.paths.append(path)
            return True

    hostile = ["..", ".", "../../evil", "..\\..\\evil", "C:\\Windows\\Temp\\x",
               "a/b", "../..", "..\\"]
    port = _Recorder()
    # `_DEBUG_SCREENSHOTS` 預設 False（`snap` 直接早退），不打開的話下面每一格都是
    # 空的——而「守衛擋掉了」與「功能整個沒開」在斷言上長得一模一樣。
    with _ws_patched(_DEBUG_SCREENSHOTS=True):
        for tag in hostile:
            ws.snap(port, tag)
        # 正面對照組：真實形狀的 tag 必須真的拍得到。少了這兩格，一支「永遠拒絕」
        # 的守衛在上面每一格都是綠的，而診斷截圖靜靜地整個功能沒了。
        ws.snap(port, "ready_surtr (arknights)")
        ws.snap(port, "generate_fail_0003")

    names = [Path(p).name for p in port.paths]
    assert "debug_ready_surtr (arknights).png" in names, (
        f"合法 tag 沒拍到（實際 {names}）——守衛變成永遠拒絕，或 "
        "`_DEBUG_SCREENSHOTS` 沒被打開。那上面每一格都不算數。")
    assert "debug_generate_fail_0003.png" in names, names

    root = ws.PROJECT_ROOT.resolve()
    for path in port.paths:
        resolved = Path(path).resolve()
        assert resolved.parent == root, (
            f"{path!r} 落在 {resolved.parent}，不是 PROJECT_ROOT。`snap` 的守衛被"
            '拿掉了，或送進守衛的不再是 f"debug_{tag}.png"。')
        assert resolved.name.startswith("debug_"), resolved.name


# ---------- generate_loop dry-run (P6 C5) -----------------------------------
# Drives the shared per-character loop with the FakeBrowserPort. Patches the
# event sink, human_pause, and _run_progress.update_saved so it runs fast with
# no browser and no real checkpoint, and (per test) overrides generate /
# crash / in-band hooks on the shared module.

import _run_progress as _rp  # noqa: E402


def _cfg(**over):
    cfg = {"images_per_character": 2, "inter_image_delay_sec": (0, 0),
           "generate_max_retries": 1, "generate_retry_delay_sec": (0, 0),
           "download_max_retries": 1, "consecutive_fail_abort": 10}
    cfg.update(over)
    return cfg


class _GenHarness:
    """Patch the shared symbols generate_loop reaches so the loop is fast and
    side-effect-free; restore everything on exit. Tests may reassign any
    ws.* / _rp.* attribute inside the block — all are snapshot/restored."""

    # `time` 也在名單裡：`generate_loop` 底下的重試退避與逾時判定都讀
    # `ws.time`，用真時鐘會讓每一支迴圈測試真的等。換成 `_FakeClock` 之後
    # `sleep()` 只是推進虛擬時鐘，`h.clock.slept` 還能拿來斷言「等了多久」。
    # 快照整個模組的命名空間，而不是一份寫死的名單。名單版的 docstring 寫著
    # 「all are snapshot/restored」，實際上只還原列出來的那幾個——漏網的補丁會
    # 活到**別的**測試裡，於是紅的是一支跟這次改動毫無關係的測試（實際踩過：
    # 在這個 harness 裡補 `has_blocking_dialog`，紅的是
    # `test_dismiss_reports_failure_when_dialog_survives`）。要嘛讓承諾成真，
    # 要嘛把承諾改掉；這裡選前者，因為 dict 複製的成本可以忽略。
    def _snapshot(self, mod):
        return dict(vars(mod))

    def _restore(self, mod, snap):
        ns = vars(mod)
        for k, v in snap.items():
            if ns.get(k, object()) is not v:
                setattr(mod, k, v)
        for k in [k for k in ns if k not in snap]:
            delattr(mod, k)

    def __enter__(self):
        self.events = []
        self.saved_calls = []
        self._ws_saved = self._snapshot(ws)
        self._rp_saved = self._snapshot(_rp)
        self._dir = tempfile.mkdtemp(prefix="gl_test_")
        # defaults: no-op pacing, recording event sink + update_saved, and
        # the iteration-boundary pollers do nothing (no request files).
        ws.emit_event = lambda et, **kw: self.events.append((et, kw))
        ws.human_pause = lambda *a, **k: None
        ws.check_dom_request = lambda port: None
        ws.check_single_image_request = lambda port, in_band=True: False
        _rp.update_saved = lambda n: self.saved_calls.append(n)
        self.clock = _FakeClock()
        ws.time = self.clock
        return self

    def __exit__(self, *exc):
        self._restore(ws, self._ws_saved)
        self._restore(_rp, self._rp_saved)
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def out_dir(self):
        return Path(self._dir)

    def events_of(self, etype):
        return [kw for et, kw in self.events if et == etype]


def test_generate_loop_normal_saves_n():
    print("test_generate_loop_normal_saves_n")
    port = FakeBrowserPort()
    with _GenHarness() as h:
        saved = ws.generate_loop(port, "Alice", _cfg(images_per_character=2),
                                 batch_start=0.0, out_dir=h.out_dir(),
                                 resume_count=0, refill=None, minimize_fn=None)
        done = h.events_of("character_done")
        # two image files actually written (check INSIDE the with — __exit__
        # rmtree's the tmp dir on exit).
        pngs = list(h.out_dir().glob("*.png"))
    assert saved == 2, f"expected saved==2, got {saved}"
    assert h.saved_calls == [1, 2], f"update_saved should run per image: {h.saved_calls}"
    assert len(done) == 1 and done[0]["saved"] == 2, done
    assert len(pngs) == 2, f"expected 2 png, got {len(pngs)}"
    print("  PASS\n")


class _ProbePort:
    """只回答崩潰探測與關閉對話框需要的那幾個問題；每個動作都記下來。"""

    def __init__(self, url="https://example.invalid/image", title="Image",
                 url_error=None, native_error=None, script_error=None):
        # 這兩個替身定義在檔案後段，所以在建構時才取，不放在類別屬性。
        self.TRANSPORT_ERRORS = (_FakeReadTimeout, _FakeNoSuchWindow)
        self.url, self.title = url, title
        self.url_error = url_error
        self.native_error, self.script_error = native_error, script_error
        self.calls: list = []

    def current_url(self):
        if self.url_error is not None:
            raise self.url_error
        return self.url

    def get_title(self):
        return self.title

    def click_native(self, element):
        self.calls.append(("native", element))
        if self.native_error is not None:
            raise self.native_error

    def execute_script(self, script, *args):
        self.calls.append(("script", script))
        if self.script_error is not None:
            raise self.script_error


def test_the_crash_page_is_recognised_by_its_url_or_its_title():
    """認出崩潰頁，第一張失敗的圖就能交給監督者重生；認不出來就對著一個死掉的分頁
    把重試預算慢慢燒完（四、五分鐘的空轉）。"""
    print("test_the_crash_page_is_recognised_by_its_url_or_its_title")
    cases = [
        (_ProbePort(url="chrome-error://chromewebdata/"), True),
        (_ProbePort(title="Aw, Snap!"), True),
        (_ProbePort(title="He's dead, Jim!"), True),
        (_ProbePort(), False),
        (_ProbePort(url=None, title=None), False),
        # 只是卡頓：當作沒崩，照舊回落到呼叫端的失敗計數器。
        (_ProbePort(url_error=_FakeReadTimeout()), False),
    ]
    for port, expected in cases:
        assert ws._is_chrome_crash_page(port) is expected, (
            port.url, port.title, port.url_error)
    gone = _ProbePort(url_error=_FakeNoSuchWindow())
    try:
        ws._is_chrome_crash_page(gone)
    except ws.BrowserGoneError:
        pass
    else:
        raise AssertionError("視窗真的沒了要升級成 BrowserGoneError，不是回「沒崩」")
    print("  PASS\n")


def test_a_dialog_click_that_finds_the_browser_gone_does_not_fall_back():
    """真點選時發現瀏覽器已經沒了，就不要再試合成點選、也不要送 Escape——那些都是對著一個
    死掉的 session 空轉，而那一輪該做的是立刻收掉、交給監督者。"""
    print("test_a_dialog_click_that_finds_the_browser_gone_does_not_fall_back")
    element = object()
    for kind in ("native", "script"):
        port = (_ProbePort(native_error=_FakeNoSuchWindow())
                if kind == "native" else
                _ProbePort(native_error=RuntimeError("element click intercepted"),
                           script_error=_FakeNoSuchWindow()))
        try:
            ws._click_dismiss_target(port, element)
        except ws.BrowserGoneError:
            pass
        else:
            raise AssertionError(kind + "：瀏覽器沒了卻沒有往上拋")
        expected = [("native", element)] if kind == "native" else [
            ("native", element), ("script", "arguments[0].click();")]
        assert port.calls == expected, (kind, port.calls)

    # 對照組：一般的「被蓋住」只是退回合成點選，不是瀏覽器沒了。
    port = _ProbePort(native_error=RuntimeError("element click intercepted"))
    assert ws._click_dismiss_target(port, element) == "synthetic"
    assert [c[0] for c in port.calls] == ["native", "script"]
    port = _ProbePort()
    assert ws._click_dismiss_target(port, element) == "real"
    assert [c[0] for c in port.calls] == ["native"]
    # JS 挑中了卻拿不到元素：什麼都不按，讓呼叫端改送 Escape。
    port = _ProbePort()
    assert ws._click_dismiss_target(port, None) == ""
    assert port.calls == []
    print("  PASS\n")


def _serve_once_between_images(refill_ok: bool, refill=("P", "C1", "", "U")):
    """跑一個三張圖的角色，第一張與第二張之間插播一次單圖請求。回
    `(存了幾張, 重填收到的參數, 丟出的 RuntimeError 文字)`。"""
    port = FakeBrowserPort()
    refills: list = []
    raised = ""
    saved = None
    with _GenHarness() as h:
        served = iter([True])
        ws.check_single_image_request = lambda port, in_band=True: next(served, False)

        def _refill(_port, *args):
            refills.append(args)
            return refill_ok

        ws._refill_character_fields = _refill
        try:
            saved = ws.generate_loop(port, "Alice", _cfg(images_per_character=3),
                                     batch_start=0.0, out_dir=h.out_dir(),
                                     resume_count=0, refill=refill,
                                     minimize_fn=None)
        except RuntimeError as error:
            raised = str(error)
        pngs = len(list(h.out_dir().glob("*.png")))
    return saved, pngs, refills, raised


def test_an_in_band_request_that_cannot_be_undone_stops_the_character():
    """插播的單圖會覆寫主 prompt／角色／undesired 欄位。填不回去還繼續的話，這個角色剩下的
    圖全部用 one-shot 的提示詞產出，存進這個角色的資料夾——沒有任何錯誤，只有一批錯的圖。
    所以要往上拋，讓監督者重生並重跑 setup，而且在**下一張圖之前**就停。"""
    saved, pngs, refills, raised = _serve_once_between_images(refill_ok=False)
    assert "in-band request" in raised, raised
    assert saved is None and pngs == 1, (saved, pngs)
    assert refills == [("P", "C1", "", "U")]


def test_an_in_band_request_that_is_undone_lets_the_character_finish():
    """正面對照組：少了它，「一律往上拋」也會讓上面那支通過。"""
    saved, pngs, refills, raised = _serve_once_between_images(refill_ok=True)
    assert raised == "" and saved == 3 and pngs == 3, (saved, pngs, raised)
    assert refills == [("P", "C1", "", "U")], "填回去的必須是**這個角色**的欄位"


def test_an_in_band_request_without_a_refill_source_touches_nothing():
    """je 變體與單獨呼叫沒有 `refill`：沒有可信的來源可填，硬填反而更糟。"""
    saved, pngs, refills, raised = _serve_once_between_images(refill_ok=False,
                                                              refill=None)
    assert raised == "" and saved == 3 and refills == []


def test_a_session_that_never_produced_an_image_reloads_early():
    """一張圖都沒出現過就放棄 → 立刻 reload ＋ 重填，不要慢慢爬到 abort。

    2026-08-24 實測（`WEBRunner.log` 760-930 行）：站方把工作階段收掉之後，40
    次嘗試每一次都是 `timed out waiting for new image (last src=None)`，連續放棄
    十張圖、燒掉 **2 小時 28 分鐘**才由 `consecutive_fail_abort` 收工；真正修好
    它的是監督者重生時的重新登入。

    三件事要一起成立才算修好：
    1. 第一張失敗就動作（不是等門檻）；
    2. 一個角色只做一次（否則每張失敗的圖都 reload 一次）；
    3. 工作階段真的沒了（重填失敗）就往上拋，讓監督者重生——這才是把 148 分鐘
       壓成一張圖的關鍵。
    """
    print("test_a_session_that_never_produced_an_image_reloads_early")
    # --- 1 + 2：重填成功 → 只 reload 一次，然後照原本的節奏繼續失敗到 abort ---
    port = FakeBrowserPort()
    with _GenHarness() as h:
        ws.generate_one_image = lambda port, prev, **k: None
        ws._is_chrome_crash_page = lambda port: False
        # Chrome 每個角色都重啟（預設設定），所以迴圈開始時畫面上本來就
        # 沒有圖——這正是 `previous_src is None` 想表達的狀態。
        ws.get_main_image_src = lambda port: None
        ws.has_blocking_dialog = lambda port: None
        refresh_calls = []
        ws._try_chrome_refresh = lambda port: refresh_calls.append(1)
        refills = []
        ws._refill_character_fields = (
            lambda port, *a: refills.append(a) or True)
        try:
            ws.generate_loop(port, "Bob", _cfg(images_per_character=5,
                                               consecutive_fail_abort=3),
                             batch_start=0.0, out_dir=h.out_dir(),
                             refill=("P", "C1", "", ""), minimize_fn=None)
        except RuntimeError:
            pass
        rec = h.events_of("page_recovery")
    assert len(refresh_calls) == 1, (
        f"一個角色只 reload 一次，實際 {len(refresh_calls)} 次")
    assert len(refills) == 1, f"reload 完一定要重填欄位: {refills}"
    assert len(rec) == 1 and rec[0]["image_index"] == 1, (
        f"要在**第一張**失敗時就動作: {rec}")

    # --- 3：重填失敗（＝工作階段沒了）→ 往上拋，讓監督者重生並重新登入 ---
    port = FakeBrowserPort()
    with _GenHarness() as h:
        ws.generate_one_image = lambda port, prev, **k: None
        ws._is_chrome_crash_page = lambda port: False
        # Chrome 每個角色都重啟（預設設定），所以迴圈開始時畫面上本來就
        # 沒有圖——這正是 `previous_src is None` 想表達的狀態。
        ws.get_main_image_src = lambda port: None
        ws.has_blocking_dialog = lambda port: None
        ws._try_chrome_refresh = lambda port: True
        ws._refill_character_fields = lambda port, *a: False
        raised = ""
        try:
            ws.generate_loop(port, "Bob", _cfg(images_per_character=99,
                                               consecutive_fail_abort=99),
                             batch_start=0.0, out_dir=h.out_dir(),
                             refill=("P", "C1", "", ""), minimize_fn=None)
        except RuntimeError as error:
            raised = str(error)
    assert "restore the fields" in raised, (
        f"重填失敗必須往上拋（否則又回到燒 99 張的老路）: {raised!r}")

    # --- 續跑的角色：`saved` 一開始就 > 0，可是頁面是全新的 → 照樣要 reload。
    #     判準若誤用 `saved` 會在這裡整個反過來，而續跑正是工作階段最容易已經
    #     死掉的時機（上一輪就是被砍掉才要續跑的）。
    port = FakeBrowserPort()
    with _GenHarness() as h:
        ws.generate_one_image = lambda port, prev, **k: None
        ws._is_chrome_crash_page = lambda port: False
        ws.has_blocking_dialog = lambda port: None
        ws.get_main_image_src = lambda port: None
        refresh_calls = []
        ws._try_chrome_refresh = lambda port: refresh_calls.append(1)
        ws._refill_character_fields = lambda port, *a: True
        try:
            ws.generate_loop(port, "Bob", _cfg(images_per_character=5,
                                               consecutive_fail_abort=2),
                             batch_start=0.0, out_dir=h.out_dir(),
                             resume_count=3, refill=("P", "C1", "", ""),
                             minimize_fn=None)
        except RuntimeError:
            pass
        rec = h.events_of("page_recovery")
    assert len(refresh_calls) == 1, (
        f"續跑角色（saved 起始 3）頁面一樣是空的，照樣要 reload: {refresh_calls}")
    assert len(rec) == 1, rec

    # --- 反例：這個工作階段已經產出過圖 → 偶發失敗，不該多付 reload 的成本 ---
    port = FakeBrowserPort()
    with _GenHarness() as h:
        calls = {"n": 0}
        def _gen(port, prev, **k):
            calls["n"] += 1
            return "blob:ok" if calls["n"] == 1 else None
        ws.generate_one_image = _gen
        ws._is_chrome_crash_page = lambda port: False
        # Chrome 每個角色都重啟（預設設定），所以迴圈開始時畫面上本來就
        # 沒有圖——這正是 `previous_src is None` 想表達的狀態。
        ws.get_main_image_src = lambda port: None
        ws.has_blocking_dialog = lambda port: None
        refresh_calls = []
        ws._try_chrome_refresh = lambda port: refresh_calls.append(1)
        ws._refill_character_fields = lambda port, *a: True
        try:
            ws.generate_loop(port, "Bob", _cfg(images_per_character=4,
                                               consecutive_fail_abort=2),
                             batch_start=0.0, out_dir=h.out_dir(),
                             refill=("P", "C1", "", ""), minimize_fn=None)
        except RuntimeError:
            pass
        rec = h.events_of("page_recovery")
    assert not refresh_calls, (
        "產出過圖就代表 app 是活的，這次失敗屬於偶發，不該 reload")
    assert not rec, f"不該發 page_recovery: {rec}"
    print("  PASS\n")


def test_generate_loop_crash_fast_path_raises():
    print("test_generate_loop_crash_fast_path_raises")
    port = FakeBrowserPort()
    with _GenHarness() as h:
        ws.generate_one_image = lambda port, prev, **k: None   # every image fails
        ws._is_chrome_crash_page = lambda port: True           # crash interstitial
        refresh_calls = []
        ws._try_chrome_refresh = lambda port: refresh_calls.append(1)
        raised = False
        try:
            ws.generate_loop(port, "Bob", _cfg(images_per_character=5),
                             batch_start=0.0, out_dir=h.out_dir(), minimize_fn=None)
        except RuntimeError as e:
            raised = True
            msg = str(e)
        assert raised, "crash fast-path must raise on the first failed image"
        assert "crashed" in msg.lower(), msg
        assert refresh_calls, "best-effort _try_chrome_refresh should run before raise"
        # raised on the FIRST image → no consecutive_failures alert yet
        assert h.events_of("consecutive_failures") == []
    print("  PASS\n")


def test_generate_loop_consecutive_fail_aborts_after_alert():
    print("test_generate_loop_consecutive_fail_aborts_after_alert")
    port = FakeBrowserPort()
    with _GenHarness() as h:
        ws.generate_one_image = lambda port, prev, **k: None   # every image fails
        ws._is_chrome_crash_page = lambda port: False          # NOT a crash page
        raised = False
        try:
            ws.generate_loop(port, "Carol",
                             _cfg(images_per_character=20, consecutive_fail_abort=6),
                             batch_start=0.0, out_dir=h.out_dir(), minimize_fn=None)
        except RuntimeError as e:
            raised = True
            msg = str(e)
        assert raised, "must raise once consecutive_fail_abort is reached"
        assert "consecutive image failures" in msg, msg
        alerts = h.events_of("consecutive_failures")
        # alert fires once at CONSECUTIVE_FAIL_ALERT (5), abort raises at 6
        assert len(alerts) == 1 and alerts[0]["count"] == ws.CONSECUTIVE_FAIL_ALERT, alerts
    print("  PASS\n")


def test_generate_loop_in_band_serve_refills():
    print("test_generate_loop_in_band_serve_refills")
    port = FakeBrowserPort()
    refill = ("P", "C1", "C2", "U")
    with _GenHarness() as h:
        # one in-band single-image request served during the inter-image gap
        ws.check_single_image_request = lambda port, in_band=True: True
        refilled = []
        ws._refill_character_fields = (
            lambda port, *a: refilled.append(a) or True)
        saved = ws.generate_loop(port, "Dave", _cfg(images_per_character=2),
                                 batch_start=0.0, out_dir=h.out_dir(),
                                 resume_count=0, refill=refill, minimize_fn=None)
    assert saved == 2, saved
    assert refilled and refilled[0] == refill, (
        f"in-band serve must refill the current character: {refilled}")
    print("  PASS\n")


def test_generate_loop_minimize_fn_called():
    print("test_generate_loop_minimize_fn_called")
    port = FakeBrowserPort()
    calls = []
    with _GenHarness() as h:
        ws.generate_loop(port, "Eve", _cfg(images_per_character=2),
                         batch_start=0.0, out_dir=h.out_dir(), resume_count=0,
                         refill=None, minimize_fn=lambda: calls.append(1))
    # called once per inter-image gap (between the 2 images)
    assert calls, "minimize_fn must be invoked in the inter-image gap"
    print("  PASS\n")


# ---------- run_batch wiring (P6 C6) ----------------------------------------
# Drives the shared orchestration `run_batch` with NO browser: redirects the
# queue / checkpoint / output paths to a tmp dir, fakes generate_loop +
# setup_fn, and exercises the loop's WIRING — `end` sentinel (rc=0),
# zero-save (rc=3), padding-aware pop/skip, and resume. (`_queue_consume.
# simulate` tests the DECISION math; this tests run_batch's plumbing.)


def _bcfg(**over):
    cfg = {"images_per_character": 1, "inter_image_delay_sec": (0, 0),
           "generate_max_retries": 1, "generate_retry_delay_sec": (0, 0),
           "download_max_retries": 1, "consecutive_fail_abort": 10,
           "restart_chrome_every_n_characters": 0, "min_save_ratio": 1.0,
           "schedule_limit_hours": 9999, "rest_hours": 1}
    cfg.update(over)
    return cfg


class _RunBatchHarness:
    """Patch every shared symbol run_batch reaches (paths, event sink,
    load_batch_config, the fill/verify/poll leaves, generate_loop) and the
    _run_progress checkpoint, so the loop runs against a tmp dir with a fake
    generate_loop. Restores everything on exit."""

    def __enter__(self):
        self.events = []
        self.gen_calls = []
        self.char2_states = []
        self.main_values = []
        self.character_values = []
        self.undesired_values = []
        self.gen_saved = 999          # generate_loop's return (>= threshold → pop)
        self.cfg_over = {}
        self._dir = tempfile.mkdtemp(prefix="rb_test_")
        self.dir = Path(self._dir)
        self._ws = {}

        def setws(name, val):
            self._ws[name] = getattr(ws, name)
            setattr(ws, name, val)

        d = self.dir
        setws("TODO_PROMPT_FILE", d / "todo_prompt.md")
        setws("TODO_FILE_1", d / "todo_character1.md")
        setws("TODO_FILE_2", d / "todo_character2.md")
        setws("TODO_UNDESIRED_FILE", d / "todo_undesired.md")
        setws("PROMPT_FILE", d / "prompt.md")
        # 每一個 fallback 常數都要導進 tmp。漏一個的話測試會讀到 repo root 那份
        # **有內容**的真檔，於是「空佇列」的案例悄悄變成「有 fallback」。
        setws("CHARACTER1_FALLBACK_FILE", d / "character1.md")
        setws("CHARACTER2_FALLBACK_FILE", d / "character2.md")
        setws("UNDESIRED_FILE", d / "undesired.md")
        setws("OUTPUT_ROOT", d / "output")
        setws("SINGLE_IMAGE_REQUEST_FILE", d / "single_image_request.json")
        setws("emit_event", lambda et, **kw: self.events.append((et, kw)))
        setws("human_pause", lambda *a, **k: None)
        setws("load_batch_config", lambda: _bcfg(**self.cfg_over))
        setws("fill_main_prompt",
              lambda _port, value: self.main_values.append(value) or True)
        setws("fill_main_undesired",
              lambda _port, value: self.undesired_values.append(value) or True)
        setws("fill_character_prompt",
              lambda _port, idx, value:
                  self.character_values.append((idx, value)) or True)
        setws("verify_character_prompt", lambda *a, **k: True)
        setws("set_character2_enabled",
              lambda _port, enabled:
                  self.char2_states.append(enabled) or True)
        setws("check_dom_request", lambda port: None)
        setws("check_single_image_request", lambda port, in_band=True: False)
        setws("_abort_if_chrome_crashed", lambda port, where: None)
        setws("snap", lambda port, tag: None)

        def fake_gen(port, character_name, batch_cfg, batch_start,
                     out_dir=None, resume_count=0, refill=None,
                     minimize_fn=None, seen_srcs=None, seen_digests=None):
            self.gen_calls.append({"name": character_name, "out_dir": out_dir,
                                   "resume_count": resume_count,
                                   "seen_srcs": seen_srcs,
                                   "seen_digests": seen_digests})
            return self.gen_saved
        setws("generate_loop", fake_gen)

        self._rp = (_rp.PROGRESS_FILE, _rp._PROGRESS_TMP)
        _rp.PROGRESS_FILE = d / "webrunner_progress.json"
        _rp._PROGRESS_TMP = _rp.PROGRESS_FILE.with_suffix(".json.tmp")
        return self

    def __exit__(self, *exc):
        for k, v in self._ws.items():
            setattr(ws, k, v)
        _rp.PROGRESS_FILE, _rp._PROGRESS_TMP = self._rp
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def write_queue(self, fname, lines):
        (self.dir / fname).write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def patch(self, name, val):
        """暫時換掉一個 `ws` 模組層符號；`__exit__` 會一起還原。"""
        if name not in self._ws:
            self._ws[name] = getattr(ws, name)
        setattr(ws, name, val)

    def read_lines(self, fname):
        p = self.dir / fname
        if not p.exists():
            return []
        return [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
                if l.strip()]

    def events_of(self, etype):
        return [kw for et, kw in self.events if et == etype]


def _run_batch(port, **kw):
    """`**kw` 只為了讓少數幾支測試宣告 `mode=`；不傳就是預設的批次模式，
    也就是絕大多數呼叫端看到的樣子。"""
    return ws.run_batch(port, "email", "pw",
                        setup_fn=lambda: True, minimize_fn=lambda: None,
                        **kw)


def test_run_batch_end_sentinel_rc0():
    print("test_run_batch_end_sentinel_rc0")
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P1", "end", "P3"])
        h.write_queue("todo_character1.md", ["a", "b", "c"])
        rc = _run_batch(FakeBrowserPort())
        done = h.events_of("todo_done")
        prompt_left = h.read_lines("todo_prompt.md")
        gen = list(h.gen_calls)
    assert rc == 0, f"end sentinel must exit rc=0, got {rc}"
    assert [c["name"] for c in gen] == ["a"], f"only the pre-end pair runs: {gen}"
    assert prompt_left == ["P3"], f"only the first 'end' line consumed: {prompt_left}"
    assert len(done) == 1 and done[0].get("stopped_by_end") is True, done
    print("  PASS\n")


def test_run_batch_zero_save_rc3():
    print("test_run_batch_zero_save_rc3")
    with _RunBatchHarness() as h:
        h.gen_saved = 0                 # generate_loop saves nothing
        h.write_queue("todo_prompt.md", ["P1"])
        h.write_queue("todo_character1.md", ["a"])
        rc = _run_batch(FakeBrowserPort())
        crit = h.events_of("critical_error")
        char1_left = h.read_lines("todo_character1.md")
    assert rc == 3, f"zero-save must exit rc=3, got {rc}"
    assert len(crit) == 1 and "saved 0" in crit[0].get("message", ""), crit
    assert char1_left == ["a"], f"below-threshold entry retained: {char1_left}"
    print("  PASS\n")


def test_generate_loop_consecutive_download_failures_abort():
    print("test_generate_loop_consecutive_download_failures_abort")
    port = FakeBrowserPort()
    with _GenHarness() as h:
        ws.download_image_with_retry = lambda *_a, **_k: False
        raised = False
        try:
            ws.generate_loop(
                port, "DownloadFail",
                _cfg(images_per_character=10, consecutive_fail_abort=3),
                batch_start=0.0, out_dir=h.out_dir(), minimize_fn=None)
        except RuntimeError as error:
            raised = True
            message = str(error)
    assert raised, "persistent download failure must abort before full batch"
    assert "download failures" in message, message
    print("  PASS\n")


def test_serve_main_prompt_failure_never_generates():
    print("test_serve_main_prompt_failure_never_generates")
    saved = (ws.fill_main_prompt, ws.time.sleep)
    port = FakeBrowserPort()
    try:
        ws.fill_main_prompt = lambda *_a, **_k: False
        ws.time.sleep = lambda _seconds: None
        with _ServeHarness() as h:
            ws.serve_single_image_request(
                port, {"request_id": "rid-fill-fail", "prompt": "P"},
                in_band=True)
            evs = h.single_image_events()
    finally:
        ws.fill_main_prompt, ws.time.sleep = saved
    assert len(evs) == 1 and evs[0]["ok"] is False, evs
    assert evs[0]["error"] == "main prompt replacement failed", evs
    assert port.clicks == 0, "Generate must not be clicked after fill failure"
    print("  PASS\n")


def test_refill_clears_empty_fields_and_verifies_them():
    print("test_refill_clears_empty_fields_and_verifies_them")
    names = ("fill_main_prompt", "fill_character_prompt",
             "fill_main_undesired", "verify_character_prompt", "human_pause")
    saved = {name: getattr(ws, name) for name in names}
    calls = []
    try:
        ws.fill_main_prompt = lambda _p, value: calls.append(("main", value)) or True
        ws.fill_character_prompt = (
            lambda _p, idx, value: calls.append((f"char{idx}", value)) or True)
        ws.fill_main_undesired = (
            lambda _p, value: calls.append(("undesired", value)) or True)
        ws.verify_character_prompt = (
            lambda _p, idx, value: calls.append((f"verify{idx}", value)) or True)
        ws.human_pause = lambda *_a, **_k: None
        ok = ws._refill_character_fields(FakeBrowserPort(), "P", "", "", "")
    finally:
        for name, value in saved.items():
            setattr(ws, name, value)
    assert ok is True
    assert ("char1", "") in calls and ("char2", "") in calls, calls
    assert ("undesired", "") in calls, calls
    assert ("verify1", "") in calls and ("verify2", "") in calls, calls
    print("  PASS\n")


def test_every_fill_finishes_before_the_first_verify_runs():
    """填 Character 2 會清空 Character 1——所以 verify 不可以跟 fill 交錯。

    這是實測出來的站台行為，不是假想：2026-09-08 從 `WEBRunner.log` 數 90 次重填，
    Character 1 在 verify 讀到不符 **90/90**（而且全是 `0 vs N`，欄位是空的），
    Character 2 **0/90**，逐次對照 char1 都是「寫入前正確、char2 填完之後變空」。

    這支用一個把那個行為寫進去的假 port 走完整條 `_refill_character_fields`，
    斷言**結束時兩個欄位都是對的**。守的是行為不是步驟名稱的字面順序，所以
    「填一個、驗一個」那種看起來更整齊的重構會直接紅——那個重構會讓 verify 在
    char2 清空 char1 **之前**就通過，於是 char1 整批空白而且一行 log 都沒有。
    """
    print("test_every_fill_finishes_before_the_first_verify_runs")
    names = ("fill_main_prompt", "fill_character_prompt",
             "fill_main_undesired", "verify_character_prompt", "human_pause")
    saved = {name: getattr(ws, name) for name in names}
    fields = {1: "", 2: ""}
    try:
        def _fill(_p, idx, value):
            fields[idx] = value
            if idx == 2:
                # 站方實際的行為：填 char2 會把 char1 清掉（單向，90/90）。
                fields[1] = ""
            return True

        def _verify(_p, idx, expected):
            if fields[idx] == expected:
                return True
            fields[idx] = expected          # verify 讀到不符就重填一次
            return fields[idx] == expected

        ws.fill_main_prompt = lambda _p, _v: True
        ws.fill_main_undesired = lambda _p, _v: True
        ws.fill_character_prompt = _fill
        ws.verify_character_prompt = _verify
        ws.human_pause = lambda *_a, **_k: None
        ok = ws._refill_character_fields(FakeBrowserPort(), "P", "C1", "C2", "U")
    finally:
        for name, value in saved.items():
            setattr(ws, name, value)
    assert ok is True, "重填回報失敗"
    # 正面對照組：如果假 port 從來沒被呼叫到，下面兩條會因為還是空字串而紅，
    # 不會變成一個「什麼都沒驗到卻通過」的測試。
    assert fields[2] == "C2", f"Character 2 收尾不對：{fields!r}"
    assert fields[1] == "C1", (
        f"Character 1 收尾是空的：{fields!r}。"
        "verify 跟 fill 交錯了嗎？填 char2 會清空 char1，所以 verify 必須排在"
        "**兩個 fill 都做完之後**才看得到最後的狀態。")
    print("  PASS\n")


def test_wait_for_new_image_stops_on_visible_generation_error():
    print("test_wait_for_new_image_stops_on_visible_generation_error")
    saved = (ws.get_main_image_src, ws.get_generation_error, ws.time.sleep)
    checks = []
    try:
        ws.get_main_image_src = lambda _port: "blob:old"
        ws.get_generation_error = (
            lambda _port: checks.append(1) or
            {"id": 1, "text": "An error occurred; try again"})
        ws.time.sleep = lambda _seconds: None
        got = ws.wait_for_new_image(
            FakeBrowserPort(), "blob:old", timeout=180,
            baseline_error=None)
    finally:
        ws.get_main_image_src, ws.get_generation_error, ws.time.sleep = saved
    assert got is None
    assert len(checks) == 1, "visible error must end the wait immediately"
    print("  PASS\n")


def test_wait_for_new_image_detects_new_same_text_error():
    print("test_wait_for_new_image_detects_new_same_text_error")
    saved = (ws.get_main_image_src, ws.get_generation_error, ws.time.sleep)
    checks = []
    try:
        ws.get_main_image_src = lambda _port: "blob:old"
        ws.get_generation_error = (
            lambda _port: checks.append(1) or
            {"id": 1, "version": 2,
             "text": "An error occurred; try again"})
        ws.time.sleep = lambda _seconds: None
        got = ws.wait_for_new_image(
            FakeBrowserPort(), "blob:old", timeout=180,
            baseline_error={"id": 1, "version": 1,
                            "text": "An error occurred; try again"})
    finally:
        ws.get_main_image_src, ws.get_generation_error, ws.time.sleep = saved
    assert got is None
    assert len(checks) == 1, "a new same-text toast must end the wait immediately"
    print("  PASS\n")


def test_configure_sampler_settings_reports_partial_failure():
    print("test_configure_sampler_settings_reports_partial_failure")
    names = ("expand_advanced_settings", "ensure_rescale_visible",
             "dump_advanced_labels", "set_numeric_setting_verified",
             "set_variety_plus", "human_pause")
    saved = {name: getattr(ws, name) for name in names}
    try:
        ws.expand_advanced_settings = lambda _port: True
        ws.ensure_rescale_visible = lambda _port: True
        ws.dump_advanced_labels = lambda _port: None
        ws.set_numeric_setting_verified = lambda *_a, **_k: True
        ws.set_variety_plus = lambda *_a, **_k: False
        ws.human_pause = lambda *_a, **_k: None
        ok = ws.configure_sampler_settings(FakeBrowserPort())
    finally:
        for name, value in saved.items():
            setattr(ws, name, value)
    assert ok is False, "setup must fail if any required sampler control fails"
    print("  PASS\n")


def test_generate_one_image_retries_until_success():
    print("test_generate_one_image_retries_until_success")
    saved = (ws.click_generate, ws.wait_for_new_image,
             ws.get_generation_error, ws.time.sleep)
    clicks = []
    waits = [None, None, "blob:new"]
    try:
        ws.click_generate = lambda _port: clicks.append(1) or True
        ws.get_generation_error = lambda _port: None
        ws.wait_for_new_image = lambda *_a, **_k: waits.pop(0)
        ws.time.sleep = lambda _seconds: None
        got = ws.generate_one_image(
            FakeBrowserPort(), "blob:old", max_retries=3,
            retry_delay=(0, 0))
    finally:
        (ws.click_generate, ws.wait_for_new_image,
         ws.get_generation_error, ws.time.sleep) = saved
    assert got == "blob:new"
    assert len(clicks) == 3, f"expected 3 generate attempts, got {len(clicks)}"
    print("  PASS\n")


def test_default_generation_retry_delay_is_25_to_30_seconds():
    print("test_default_generation_retry_delay_is_25_to_30_seconds")
    import _batch_config as bc
    assert bc._DEFAULT_BATCH_CONFIG["generate_retry_delay_sec"] == (25.0, 30.0)
    print("  PASS\n")


def test_model_candidates_is_config_driven_in_both_variants():
    """產圖模型的候選字面必須來自 `batch_config.json`，兩個 webrunner 變體都不
    准再各存一份模組常數。

    為什麼值得釘：這是整套系統裡吞吐量差距最大的一個選擇（站方 2026 年的方案
    政策只對 V5 加使用量上限，其餘模型對頂級訂閱不限量；本機實測排空後是
    4.65 張／小時），而它原本是寫死在**兩個檔案**裡的常數——要改得動兩處原始碼
    再重啟，漏改一處就是 CLAUDE.md 點名的變體漂移。

    掃原始碼而不是掃 import 後的模組：常數被搬回去時 `import` 不會有任何症狀，
    要看的就是「檔案裡還有沒有那個名字」。"""
    print("test_model_candidates_is_config_driven_in_both_variants")
    import _batch_config as bc
    here = Path(__file__).resolve().parent.parent / "axiomatic"
    for name in ("webrunner_novelai.py", "webrunner_je_only.py"):
        src = (here / name).read_text(encoding="utf-8")
        for banned in ("TARGET_MODEL_CANDIDATES", "TARGET_MODEL ="):
            assert banned not in src, (
                f"{name} 又長回模組常數 {banned!r}——模型候選的唯一來源是 "
                f"batch_config.json 的 model_candidates，兩個變體都得從那裡讀")
        assert 'ws.load_batch_config()["model_candidates"]' in src, (
            f"{name} 沒有從設定讀模型候選")
    # 預設值要維持現狀（換模型是使用者的畫質決定，不是這支測試能替他做的）。
    assert bc._DEFAULT_BATCH_CONFIG["model_candidates"][0] == (
        "NAI Diffusion V5 Full")
    # 這個鍵**不得**出現在 bot 的可設定清單裡：值是外部服務的模型名，
    # `/config show` 會把整份清單印進頻道（Secrecy Layer 1）。
    import discord_bot as db
    assert "model_candidates" not in db._BATCH_SETTERS, (
        "model_candidates 不可設成 bot 可改的鍵——`/config show` 會把值印給"
        "頻道看，那是外部服務的模型名")
    print("  PASS\n")


def test_a_reversed_or_negative_range_and_a_non_boolean_flag_fall_back():
    """`(lo, hi)` 是 `random.uniform` 的兩端：反過來寫（`[30, 20]`）不會出錯，只會安靜地
    在錯的範圍裡抽；負數會讓等待變成「不等」。開關寫成字串 `"false"` 時，照 Python 的真假值
    它是 **True**——使用者寫了關，實際是開。這三種都退回預設，交給載入端出聲。"""
    print("test_a_reversed_or_negative_range_and_a_non_boolean_flag_fall_back")
    import _batch_config as bc
    default = (20.0, 30.0)
    for bad in ([30, 20], (5.0, 4.999), [-1, 5], [-5, -1], [1], [1, 2, 3], ["1", "2"],
                [True, 2], "20,30", None):
        assert bc._coerce_pair(bad, default) == default, bad
    for good, want in (([0, 0], (0.0, 0.0)), ([2, 2], (2.0, 2.0)), ((1, 5.5), (1.0, 5.5))):
        assert bc._coerce_pair(good, default) == want, good
    for bad in ("false", "true", 0, 1, None, [], "no"):
        assert bc._coerce_bool(bad, True) is True, bad
        assert bc._coerce_bool(bad, False) is False, bad
    assert bc._coerce_bool(False, True) is False
    assert bc._coerce_bool(True, False) is True
    print("  PASS\n")


def test_model_candidates_coercion_rejects_a_bare_string():
    """壞值一律退回預設，而且**單一字串不算合法**。

    `"NAI Diffusion V5 Full"` 是可迭代的，若順手放行就會被拆成 22 個單字元
    候選，一個都不會命中，而 log 只會說「模型選不到」——使用者改了設定、看到
    的卻是一個看似無關的失敗。這是最容易寫錯的一種設定值，所以獨立釘一條。"""
    print("test_model_candidates_coercion_rejects_a_bare_string")
    import _batch_config as bc
    default = ("D1", "D2")
    for bad in (None, 5, True, "NAI Diffusion V5 Full", "", [], (),
                ["", "   "], {"a": 1}, [5, 6]):
        assert bc._coerce_str_list(bad, default) == default, bad
    # 合法值：逐項 strip、丟掉空的與非字串的，順序保留。
    assert bc._coerce_str_list(["  A  ", "B"], default) == ("A", "B")
    assert bc._coerce_str_list(["A", 5, "", "B"], default) == ("A", "B")
    print("  PASS\n")


def test_run_batch_prompt_verify_failure_retries_without_generation():
    """A stale/default character prompt must never reach generate_loop."""
    print("test_run_batch_prompt_verify_failure_retries_without_generation")
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P1"])
        h.write_queue("todo_character1.md", ["wanted character"])
        attempts = []
        old_sleep = ws.time.sleep
        ws.time.sleep = lambda _seconds: None
        ws.verify_character_prompt = (
            lambda *_a, **_k: attempts.append(1) and False)
        try:
            rc = _run_batch(FakeBrowserPort())
        finally:
            ws.time.sleep = old_sleep
        char1_left = h.read_lines("todo_character1.md")
        gen = list(h.gen_calls)
    assert rc == 0, rc
    assert len(attempts) == 3, f"verification must retry 3 times: {attempts}"
    assert not gen, f"generation must not run with an unverified prompt: {gen}"
    assert char1_left == ["wanted character"], (
        f"failed prompt entry must stay queued: {char1_left}")
    print("  PASS\n")


def test_run_batch_padding_aware_pop():
    print("test_run_batch_padding_aware_pop")
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P"])          # short → padded
        h.write_queue("todo_character1.md", ["a", "b"])
        rc = _run_batch(FakeBrowserPort())
        gen = [c["name"] for c in h.gen_calls]
        prompt_left = h.read_lines("todo_prompt.md")
        char1_left = h.read_lines("todo_character1.md")
    assert rc == 0, rc
    assert gen == ["a", "b"], f"both char1 entries consumed: {gen}"
    # the padded 'P' is only popped on the FINAL pair, not the first
    assert prompt_left == [], f"prompt popped on last pair: {prompt_left}"
    assert char1_left == [], f"char1 fully popped: {char1_left}"
    print("  PASS\n")


def test_run_batch_short_char2_does_not_repeat_stale_tail():
    print("test_run_batch_short_char2_does_not_repeat_stale_tail")
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P"])
        h.write_queue("todo_character1.md", ["a", "b"])
        h.write_queue("todo_character2.md", ["C2-once"])
        rc = _run_batch(FakeBrowserPort())
        states = list(h.char2_states)
        char2_left = h.read_lines("todo_character2.md")
    assert rc == 0, rc
    assert states == [True, False], (
        f"second batch must remove Character 2 instead of reusing it: {states}")
    assert char2_left == [], char2_left
    print("  PASS\n")


def test_run_batch_blank_char2_row_removes_slot_without_shifting():
    print("test_run_batch_blank_char2_row_removes_slot_without_shifting")
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P1", "P2"])
        h.write_queue("todo_character1.md", ["a", "b"])
        h.write_queue("todo_character2.md", ["", "C2-second"])
        rc = _run_batch(FakeBrowserPort())
        states = list(h.char2_states)
        char2_raw = (h.dir / "todo_character2.md").read_text(
            encoding="utf-8")
    assert rc == 0, rc
    assert states == [False, True], (
        f"blank first row must remove Character 2 before the next row: {states}")
    assert char2_raw == "", f"both positional rows must be consumed: {char2_raw!r}"
    print("  PASS\n")


def test_run_batch_empty_fields_are_explicitly_cleared():
    print("test_run_batch_empty_fields_are_explicitly_cleared")
    with _RunBatchHarness() as h:
        # A char2-only queue drives a real pair while main/char1/undesired are
        # empty. All three empty values must be written, never inherited.
        h.write_queue("todo_character2.md", ["C2"])
        rc = _run_batch(FakeBrowserPort())
        main_values = list(h.main_values)
        character_values = list(h.character_values)
        undesired_values = list(h.undesired_values)
    assert rc == 0, rc
    assert main_values == [""], main_values
    assert (1, "") in character_values, character_values
    assert undesired_values == [""], undesired_values
    print("  PASS\n")


def test_run_batch_undesired_failure_never_generates():
    print("test_run_batch_undesired_failure_never_generates")
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P"])
        h.write_queue("todo_character1.md", ["C1"])
        h.write_queue("todo_undesired.md", ["U"])
        attempts = []
        old_sleep = ws.time.sleep
        ws.fill_main_undesired = (
            lambda *_a, **_k: attempts.append(1) and False)
        ws.time.sleep = lambda _seconds: None
        try:
            rc = _run_batch(FakeBrowserPort())
        finally:
            ws.time.sleep = old_sleep
        gen = list(h.gen_calls)
    assert rc == 0, rc
    assert len(attempts) == 3, attempts
    assert not gen, f"generation must not run with stale undesired: {gen}"
    print("  PASS\n")


def test_run_batch_resume_from_checkpoint():
    print("test_run_batch_resume_from_checkpoint")
    with _RunBatchHarness() as h:
        h.cfg_over = {"images_per_character": 5}
        h.write_queue("todo_prompt.md", ["P"])
        h.write_queue("todo_character1.md", ["a"])
        folder = h.dir / "output" / "a"
        folder.mkdir(parents=True)
        for i in (1, 2):                # 2 of 5 already on disk
            (folder / f"a_{i:04d}_20260101_000000.png").write_bytes(b"x")
        _rp.write_progress("P", "a", "", "", "a", 5)   # matching checkpoint
        rc = _run_batch(FakeBrowserPort())
        gen = list(h.gen_calls)
    assert rc == 0, rc
    assert len(gen) == 1, gen
    assert gen[0]["resume_count"] == 2, f"must resume from 2 existing: {gen[0]}"
    assert gen[0]["out_dir"] is not None and gen[0]["out_dir"].name == "a", gen[0]
    print("  PASS\n")


def test_run_batch_survives_a_checkpoint_whose_folder_is_corrupt():
    """身分四欄**全等**、`folder` 欄位卻壞掉的檢查點，不可以炸掉整批作業，
    也不可以把圖寫到 `output/` 外面。

    `matches()` 只比對 prompt / char1 / char2 / undesired——它從來不看
    `folder`。所以這種檢查點在它眼裡是「同一個配對」，接續照走，然後呼叫端
    拿那個壞值去接路徑。實測六種壞法：欄位不存在 → `KeyError`；值是 None／
    int／list → `TypeError`（三種都會讓例外從 `run_batch` 的起始路徑往上丟、
    整批停擺）；值是 `../` 或絕對路徑 → 路徑跑到 `output/` 外面，
    `generate_loop` 會 `mkdir(parents=True)` 然後把 120 張圖寫進去，而且沒有
    任何一行 log 會提到這件事。

    現在一律退回「不接續、從第 1 張重來」，並發一筆 `resume_unusable`。"""
    print("test_run_batch_survives_a_checkpoint_whose_folder_is_corrupt")
    escaped = None
    for bad_folder in (None, 5, ["x"], "../../ESCAPED", "", "   "):
        with _RunBatchHarness() as h:
            h.cfg_over = {"images_per_character": 5}
            h.write_queue("todo_prompt.md", ["P"])
            h.write_queue("todo_character1.md", ["a"])
            escaped = h.dir.parent / "ESCAPED"
            payload = {"prompt": "P", "char1": "a", "char2": "",
                       "undesired": "", "target": 5, "saved": 3}
            payload["folder"] = bad_folder
            _rp._atomic_write(payload)
            rc = _run_batch(FakeBrowserPort())
            gen = list(h.gen_calls)
            bad = h.events_of("resume_unusable")
            out_root = h.dir / "output"
        assert rc == 0, f"folder={bad_folder!r} 讓整批作業掛了: rc={rc}"
        assert len(gen) == 1, (bad_folder, gen)
        assert gen[0]["resume_count"] == 0, (bad_folder, gen[0])
        assert gen[0]["out_dir"].parent == out_root, (
            f"folder={bad_folder!r} 讓輸出跑出 output/: {gen[0]['out_dir']}")
        assert len(bad) == 1 and bad[0].get("reason") == "bad_folder", (
            f"folder={bad_folder!r} 必須留下一筆 resume_unusable: {bad}")
    # 欄位整個不存在（原本是 KeyError，最會炸的一種）也走同一條路。
    with _RunBatchHarness() as h:
        h.cfg_over = {"images_per_character": 5}
        h.write_queue("todo_prompt.md", ["P"])
        h.write_queue("todo_character1.md", ["a"])
        _rp._atomic_write({"prompt": "P", "char1": "a", "char2": "",
                           "undesired": "", "target": 5, "saved": 3})
        rc = _run_batch(FakeBrowserPort())
        gen = list(h.gen_calls)
        bad = h.events_of("resume_unusable")
    assert rc == 0 and len(gen) == 1 and gen[0]["resume_count"] == 0, (rc, gen)
    assert len(bad) == 1 and bad[0].get("reason") == "bad_folder", bad
    assert not (escaped and escaped.exists()), f"寫到 output/ 外面了: {escaped}"
    print("  PASS\n")


def test_run_batch_reports_a_checkpoint_folder_that_vanished():
    """檢查點對得上、但它指的資料夾已經不在磁碟上（最常見：使用者自己清了
    輸出目錄）。這一輪只能從頭重產——但「上一輪那 7 張到哪去了」必須留下
    紀錄，否則使用者只會看到進度莫名其妙倒退。

    與 `bad_folder` 分開報：那個是紀錄本身壞了，這個是紀錄好好的、東西不見
    了，使用者能做的事不一樣。"""
    print("test_run_batch_reports_a_checkpoint_folder_that_vanished")
    with _RunBatchHarness() as h:
        h.cfg_over = {"images_per_character": 5}
        h.write_queue("todo_prompt.md", ["P"])
        h.write_queue("todo_character1.md", ["a"])
        # 資料夾名合法，但沒有真的建出來。
        _rp.write_progress("P", "a", "", "", "a_2", 5)
        _rp.update_saved(3)
        rc = _run_batch(FakeBrowserPort())
        gen = list(h.gen_calls)
        gone = h.events_of("resume_unusable")
    assert rc == 0, rc
    assert len(gen) == 1 and gen[0]["resume_count"] == 0, gen
    assert len(gone) == 1, f"必須剛好發一筆 resume_unusable: {gone}"
    assert gone[0].get("reason") == "folder_missing", gone[0]
    assert gone[0].get("saved") == 3, f"要講清楚丟掉幾張: {gone[0]}"
    print("  PASS\n")


def test_run_batch_announces_the_scheduled_rest():
    """跑滿 `schedule_limit_hours` 之後那段休息**必須**發事件。

    2026-08-28 實測：`character_done` 在 06:50:54，下一則事件是 12:50:54 的
    `chrome_restart`——中間整整六小時的事件串流是空的，外面看到的跟卡死完全
    一樣。預設 16h 工作 / 6h 休息＝**每 22 小時有 27% 的時間**處在這個狀態，
    而 `/rate` 的「超過一小時沒有新圖」警告在其中的每一分鐘都會亮。

    `wake_ts` 是必要欄位而不是裝飾：bot 端靠它判斷「這則 `schedule_rest` 是不
    是還有效」，沒有它就沒辦法分辨「正在休息」與「休息中被砍掉、
    `schedule_resumed` 永遠不會來」。
    """
    print("test_run_batch_announces_the_scheduled_rest")
    with _RunBatchHarness() as h:
        # limit 0 → 第一個角色跑完就一定超時；休息長度用真值 6h，但把
        # `rest_until` 換成假的，測試不真的睡。
        h.cfg_over = {"schedule_limit_hours": 0, "rest_hours": 6}
        seen = []
        h.patch("rest_until", lambda port, wake_ts, **kw:
                seen.append(wake_ts) or False)
        h.write_queue("todo_prompt.md", ["P"])
        h.write_queue("todo_character1.md", ["a"])
        before = time.time()
        rc = _run_batch(FakeBrowserPort())
        after = time.time()
        rest = h.events_of("schedule_rest")
        back = h.events_of("schedule_resumed")
    assert rc == 0, rc
    assert len(rest) == 1, f"休息必須剛好發一筆 schedule_rest: {h.events}"
    assert rest[0]["rest_sec"] == 6 * 3600, rest[0]
    wake = rest[0]["wake_ts"]
    # 事件把秒數 round 到小數一位，所以區間兩端各留 1 秒；重點是 `wake_ts` 為
    # **絕對** epoch 秒（bot 直接拿去跟 now 比），不是「還要睡幾秒」。
    assert before + 6 * 3600 - 1 <= wake <= after + 6 * 3600 + 1, (
        f"wake_ts 要是絕對時間: {wake}")
    assert seen and abs(seen[0] - wake) < 1.0, (
        f"rest_until 收到的醒來時間要跟事件裡那個一致: {seen} vs {wake}")
    assert len(back) == 1, f"休息結束也要發一筆: {h.events}"
    # 實測值，不是設定值——暫停標記與插播都會把休息拉長。
    assert back[0]["rested_sec"] < 60, back[0]
    print("  PASS\n")


def test_a_zero_length_rest_stays_silent():
    """`rest_hours: 0` 是合法設定（＝不休息、只把排程計時器歸零）。這種情況下
    發事件會讓對話平台冒出一則「休息 0 秒」的廢話，而且 bot 會把 `wake_ts`
    收在過去的時間點——白繞一圈。計時器照樣要歸零。"""
    print("test_a_zero_length_rest_stays_silent")
    with _RunBatchHarness() as h:
        h.cfg_over = {"schedule_limit_hours": 0, "rest_hours": 0}
        called = []
        h.patch("rest_until", lambda *a, **k: called.append(1) or False)
        h.write_queue("todo_prompt.md", ["P"])
        h.write_queue("todo_character1.md", ["a"])
        rc = _run_batch(FakeBrowserPort())
        rest = h.events_of("schedule_rest")
        back = h.events_of("schedule_resumed")
    assert rc == 0, rc
    assert not rest and not back, f"零長度休息不該發事件: {h.events}"
    assert not called, "零長度休息不該進切片睡"
    print("  PASS\n")


def test_a_rest_that_served_a_single_image_forces_a_refill():
    """休息中插播的單圖會覆寫主 prompt／角色／undesired 欄位。休息結束後如果
    下一個 pair 的提示詞剛好**沒變**，per-pair diff 會判定「不用填」，於是整個
    角色用的是那張 one-shot 殘留的提示詞——安靜地產出一整批錯的圖。

    同一個陷阱在「角色之間插播」（`check_single_image_request` 回 True）與
    「Chrome 重啟後」都已經處理過了，休息這條路是第三個入口。"""
    print("test_a_rest_that_served_a_single_image_forces_a_refill")
    def _run(served):
        with _RunBatchHarness() as h:
            h.cfg_over = {"schedule_limit_hours": 0, "rest_hours": 6,
                          "restart_chrome_every_n_characters": 0}
            h.patch("rest_until", lambda *a, **k: served)
            # 兩個 pair、**同一個提示詞** → 正常情況下第二個不會重填。
            h.write_queue("todo_prompt.md", ["P", "P"])
            h.write_queue("todo_character1.md", ["a", "b"])
            _run_batch(FakeBrowserPort())
            return list(h.main_values)
    assert _run(False) == ["P"], (
        f"沒插播就不該多填一次: {_run(False)}")
    assert _run(True) == ["P", "P"], (
        f"插播過就必須強制重填主提示詞: {_run(True)}")
    print("  PASS\n")


def test_rest_until_keeps_serving_while_it_sleeps():
    """休息不能是一句 `time.sleep(21600)`。

    單一長睡眠同時擋掉三件事：暫停標記、DOM 請求、單圖插播。第三項是**必定**
    失效——bot 的單圖請求 TTL 是 600 秒，預設休息 21,600 秒，所以休息期間送出
    的單圖請求一次都不可能被服務到。等額度那條路早就是切片睡＋每片服務一次
    （`_serve_pending_requests` 的 docstring 寫明理由就是睡得比 TTL 久），休息
    比它久六倍卻反而沒做。

    插播丟例外也不能把整輪 batch 打掉：閒置數小時的 Chrome 偶爾抽風是常態，而
    休息結束後緊接著就是週期性重啟，瀏覽器本來就會換一份乾淨的。"""
    print("test_rest_until_keeps_serving_while_it_sleeps")
    dom, paused = [], []
    real = (ws.check_dom_request, ws.check_single_image_request,
            ws.wait_if_paused)
    try:
        ws.wait_if_paused = lambda label="": paused.append(label)
        ws.check_dom_request = lambda port: dom.append(1)
        ws.check_single_image_request = lambda port, in_band=True: False
        with _fake_clock() as clk:
            served = ws.rest_until(object(), clk.now + 300.0, slice_sec=30.0)
        assert served is False, served
        assert len(clk.slept) == 10, f"300s / 30s 切片 = 10 次: {clk.slept}"
        assert set(clk.slept) == {30.0}, clk.slept
        assert len(dom) == 10, f"每一片都要服務一次 DOM 請求: {len(dom)}"
        assert paused and all("rest" in p for p in paused), paused

        # 有插播到單圖 → 回 True（呼叫端據此強制重填欄位）。
        hits = [False, True, False]
        ws.check_single_image_request = (
            lambda port, in_band=True: hits.pop(0) if hits else False)
        with _fake_clock() as clk:
            assert ws.rest_until(object(), clk.now + 90.0,
                                 slice_sec=30.0) is True

        # 插播炸掉 → 記 log、繼續睡完，不往上拋。
        def _boom(port, in_band=True):
            raise RuntimeError("driver went away")
        ws.check_single_image_request = _boom
        with _fake_clock() as clk:
            assert ws.rest_until(object(), clk.now + 60.0,
                                 slice_sec=30.0) is False
            assert len(clk.slept) == 2, clk.slept
    finally:
        (ws.check_dom_request, ws.check_single_image_request,
         ws.wait_if_paused) = real
    print("  PASS\n")


def test_rest_length_survives_the_wall_clock_being_changed():
    """六小時的休息要是**六小時**，不能被 NTP 校時改掉長度。

    `rest_hours` 的語意是時長：呼叫端算的就是 `time.time() + rest_s`。若整段用
    `wake_ts - time.time()` 判斷，時鐘往回撥一小時就多睡一小時，往前撥就少睡一
    小時——而這台開發機的休息預設六小時，正好是最容易跨到一次校時的長度。

    這一支刻意讓假時鐘的**牆鐘跳、單調時鐘不跳**（真實世界就是這樣），所以它只有
    在 `rest_until` 用單調截止時刻時才會過。`wake_ts` 本身仍是牆鐘值，呼叫端要拿它
    印「幾點醒來」、也寫進 `schedule_rest` 事件——所以換算必須在 `rest_until` 裡做，
    不是把呼叫端改成傳時長。
    """
    print("test_rest_length_survives_the_wall_clock_being_changed")
    real = (ws.check_dom_request, ws.check_single_image_request,
            ws.wait_if_paused)
    try:
        ws.wait_if_paused = lambda label="": None
        ws.check_dom_request = lambda port: None
        ws.check_single_image_request = lambda port, in_band=True: False
        for skew, label in ((-3600.0, "往回撥一小時"), (+3600.0, "往前撥一小時")):
            with _fake_clock() as clk:
                # 第一片睡完之後，牆鐘跳一次（單調時鐘不受影響）。
                def _step(clock, _sec, _skew=skew):
                    if len(clock.slept) == 1:
                        clock.wall_skew = _skew
                clk.on_sleep = _step
                ws.rest_until(object(), clk.now + 300.0, slice_sec=30.0)
            assert len(clk.slept) == 10, (
                f"{label}：休息長度被牆鐘帶著跑了（切片數 {len(clk.slept)}，"
                f"應為 10）——`rest_until` 還在用 time.time() 判剩餘")
            assert set(clk.slept) == {30.0}, clk.slept
    finally:
        (ws.check_dom_request, ws.check_single_image_request,
         ws.wait_if_paused) = real
    print("  PASS\n")


def test_rest_until_uses_a_monotonic_deadline():
    """形狀守門：`rest_until` 裡不可以有任何拿 `time.time()` 算剩餘的地方。

    上一支是行為測試，但它只在時鐘真的跳的時候才分得出差別；有人把單調截止時刻
    改回牆鐘、同時「順手」也把測試的 skew 拿掉，就沒有東西會紅。這一支用 AST 直接
    釘住：函式裡對 `time` 模組的呼叫，只有 `monotonic` 與 `sleep` 是允許的
    （`time.time()` 只准出現在把 `wake_ts` 換算成截止時刻的那一次）。
    """
    pkg = Path(__file__).resolve().parent.parent / "axiomatic"
    tree = ast.parse((pkg / "_webrunner_shared.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "rest_until")
    attrs = [n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "time"]
    assert attrs.count("monotonic") >= 3, (
        f"截止時刻、迴圈剩餘、睡眠長度三處都要用 monotonic：{attrs}")
    assert attrs.count("time") == 1, (
        f"`time.time()` 只准用一次（把牆鐘目標換算成截止時刻）：{attrs}")
    print("  PASS test_rest_until_uses_a_monotonic_deadline")


# ---------- 遙測不可以打斷批次 -----------------------------------------------

@contextlib.contextmanager
def _events_to(tmp: Path):
    """把 `EVENTS_FILE` 指到 tmp，離開時還原。"""
    saved = ws.EVENTS_FILE
    ws.EVENTS_FILE = tmp
    try:
        yield tmp
    finally:
        ws.EVENTS_FILE = saved


def _event_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [l for l in path.read_text(encoding="utf-8").splitlines() if l]


def test_emit_event_never_lets_a_bad_payload_kill_the_run():
    """`emit_event` 是盡力而為的旁路：寫不出去就少一則通知，**絕不可以往上炸**。

    原本 except 只接 `OSError`，但 `json.dumps` 的失敗根本不是 OSError，會直接從
    這裡逸出，把一個無人值守的批次弄死在一次**遙測**上。

    兩個案例刻意分開，因為它們釘的是 except 元組裡**不同**的成員——只測 `Path`
    的話，有人把它收窄成 `(OSError, TypeError)` 仍然全綠：

    * `Path` / `set` → `dumps` 丟 `TypeError`；
    * 落單代理字元（lone surrogate）→ `dumps` **會過**，是 `f.write()` 在 encode
      時丟 `UnicodeEncodeError`（`ValueError` 的子類）。瀏覽器回來的字串出現這種
      東西比循環參照實際得多。
    """
    print("test_emit_event_never_lets_a_bad_payload_kill_the_run")
    tmp = Path(tempfile.mkdtemp(prefix="ev_test_"))
    try:
        path = tmp / "events.ndjson"
        with _events_to(path):
            # 先寫一則正常的，確認 (a) 正常路徑還會寫、(b) 壞的那則不會把已經
            # 落地的內容弄壞。
            ws.emit_event("ok_event", name="a", saved=3, ratio=0.5, done=True)
            assert len(_event_lines(path)) == 1, _event_lines(path)

            for label, payload in (
                    ("Path", {"folder": Path("x")}),
                    ("set", {"fields": {"prompt", "char1"}}),
                    ("lone surrogate", {"text": "\ud800"}),
            ):
                buf = io.StringIO()
                before = _event_lines(path)
                with contextlib.redirect_stderr(buf):
                    ws.emit_event("bad_event", **payload)   # 不可以拋
                after = _event_lines(path)
                assert after == before, (
                    f"{label}：壞的事件不該寫進任何東西（含半行）：{after}")
                assert "bad_event" in buf.getvalue(), (
                    f"{label}：吞掉可以，但一定要在 stderr 留話："
                    f"{buf.getvalue()!r}")

            # 壞的那幾則之後，正常事件仍然要寫得進去。
            ws.emit_event("ok_event", name="b")
            lines = _event_lines(path)
        assert len(lines) == 2, lines
        assert all(json.loads(l)["type"] == "ok_event" for l in lines), lines
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("  PASS\n")


def test_emit_event_catches_what_json_actually_raises():
    """形狀守門：釘住 except 元組真的含 `TypeError` 與 `ValueError`。

    上一支是行為測試，但它只走得到「目前想得到的」壞值。這一支直接讀 AST，讓
    「把元組收窄」這個動作本身變紅——包含未來新增的失敗型別（循環參照也是
    `ValueError`）。
    """
    pkg = Path(__file__).resolve().parent.parent / "axiomatic"
    tree = ast.parse((pkg / "_webrunner_shared.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "emit_event")
    caught = set()
    for handler in (h for n in ast.walk(fn) if isinstance(n, ast.Try)
                    for h in n.handlers):
        node = handler.type
        names = node.elts if isinstance(node, ast.Tuple) else [node]
        caught.update(n.id for n in names if isinstance(n, ast.Name))
    assert {"OSError", "TypeError", "ValueError"} <= caught, (
        f"`emit_event` 少接了東西：{sorted(caught)}。`json.dumps` 丟 TypeError"
        "（不可序列化的值）與 ValueError（循環參照、以及 write 時的 "
        "UnicodeEncodeError）——這兩個都不是 OSError，逸出去會把無人值守的批次"
        "殺在一次遙測上。")
    print("  PASS test_emit_event_catches_what_json_actually_raises")


# ---------- 量間隔一律用單調時鐘（牆鐘可調整） -------------------------------
#
# `time.time()` 在本機是 `GetSystemTimePreciseAsFileTime()`、`adjustable=True`
# （`time.get_clock_info('time')` 實測）；`time.monotonic()` 是
# `QueryPerformanceCounter()`、`adjustable=False`。NTP 校時、使用者改時鐘、換時
# 區、虛擬機還原都會讓牆鐘往前或往後跳，拿它量**間隔**就會整段偏掉。
#
# 反過來，要**離開本行程**的時間點（寫進事件、落地到檔案、跟別的行程或檔案 mtime
# 比對）必須留在牆鐘——monotonic 的零點每個行程都不一樣。下面三支測試分別釘住
# 「行為不受牆鐘影響」與「這個形狀在原始碼裡一個都不剩」。


def test_the_schedule_rest_decision_ignores_a_wall_clock_jump():
    """排程休息的判定用的是**工作了多久**，不是「牆上現在幾點」。

    `run_batch` 累計 `schedule_limit_hours`（預設 16h）之後要休息 `rest_hours`
    （預設 6h）。這個判定若用牆鐘算間隔，一次 NTP 校時就能：往後跳＝休息被無限
    延後（整批一直跑下去，而那個休息時段是刻意的）；往前跳＝沒必要地提早休息。

    兩個階段合起來才有意義——只有第一階段的話，「把休息整個關掉」也會讓測試變綠。

    1. 牆鐘往後跳 100 小時、單調時鐘不動 → **不可以**休息。
    2. 單調時鐘真的走了 9 小時（> 8h 上限）→ **必須**休息。
    """
    print("test_the_schedule_rest_decision_ignores_a_wall_clock_jump")

    def _run(wall_jump_h, mono_advance_h):
        with _RunBatchHarness() as h:
            h.cfg_over = {"schedule_limit_hours": 8, "rest_hours": 6}
            h.patch("rest_until", lambda port, wake_ts, **kw: False)
            h.write_queue("todo_prompt.md", ["P1", "P2"])
            h.write_queue("todo_character1.md", ["a", "b"])
            with _fake_clock() as clk:
                def fake_gen(port, character_name, *a, **k):
                    h.gen_calls.append({"name": character_name})
                    # 角色跑完的當下動時鐘。`wall_skew` 只影響 `time()`，
                    # `monotonic()` 走的是 `now`——真實世界就是這樣。
                    clk.wall_skew += wall_jump_h * 3600
                    clk.now += mono_advance_h * 3600
                    return h.gen_saved
                h.patch("generate_loop", fake_gen)
                rc = _run_batch(FakeBrowserPort())
            return rc, h.events_of("schedule_rest"), [c["name"]
                                                      for c in h.gen_calls]

    rc, rest, names = _run(wall_jump_h=100, mono_advance_h=0)
    assert rc == 0, rc
    assert names == ["a", "b"], names
    assert not rest, (
        f"牆鐘往後跳不該觸發休息（實際工作 0 小時）：{rest}——"
        "`run_batch` 還在用 time.time() 算 schedule 已經跑了多久")

    rc, rest, names = _run(wall_jump_h=0, mono_advance_h=9)
    assert rc == 0, rc
    assert rest, "單調時鐘真的超過上限時仍然必須休息（上一段斷言才有意義）"
    print("  PASS\n")


def test_the_single_image_server_idle_shutdown_ignores_a_wall_clock_jump():
    """單圖伺服器的閒置關閉量的是「距離上次服務過了多久」。

    牆鐘往後跳＝伺服器立刻收工（bot 剛寫進來的請求全部落空，要等它自己的
    `_SINGLE_IMAGE_PENDING_TTL_SEC` 逾時才會被掃掉）；往前跳＝關不掉。

    這裡讓牆鐘在第一次空轉的 poll 就往後跳一小時（遠大於 120 秒的閒置上限），
    然後看伺服器實際上撐了多久**單調**時間才 break。

    模式要**宣告**（pass 3 之後閘門只看 argv 旗標，不看磁碟上有沒有請求檔）；
    請求檔照樣寫出來，因為第一圈的 serve 是這支測試的起點。
    """
    print("test_the_single_image_server_idle_shutdown_ignores_a_wall_clock_jump")
    idle_timeout = 120.0
    with _RunBatchHarness() as h:
        ws.SINGLE_IMAGE_REQUEST_FILE.write_text(
            '{"request_id": "r1", "prompt": "p"}', encoding="utf-8")
        polls = []
        with _fake_clock() as clk:
            started_mono = clk.now

            def fake_check(port, in_band=True):
                polls.append(clk.now - started_mono)
                if len(polls) == 1:
                    return True          # 第一圈服務掉那個請求
                if len(polls) == 2:
                    # 第一次「沒東西可服務」的當下，牆鐘往後跳一小時。
                    clk.wall_skew += 3600.0
                return False
            h.patch("check_single_image_request", fake_check)
            rc = _run_batch(FakeBrowserPort(),
                            mode=ws.RUN_MODE_SINGLE_IMAGE_SERVER)
            waited = clk.now - started_mono

    assert rc == 0, rc
    assert waited >= idle_timeout, (
        f"牆鐘往後跳讓伺服器提早收工（只撐了 {waited:.1f}s，"
        f"閒置上限是 {idle_timeout:.0f}s）——閒置時鐘還在用 time.time()")
    # 每圈睡 1~2 秒，撐滿 120 秒至少要幾十圈；順手擋掉「把 idle_timeout 調大
    # 就會綠」這種假修法。
    assert len(polls) > 50, f"poll 次數太少，迴圈沒有真的跑滿閒置期：{len(polls)}"
    print("  PASS\n")


# 形狀守門的豁免名單。key 是 `<檔名>:<函式名>:<變數名>`，value 是**理由**。
# 只有「這個錨點確實需要是牆鐘」才可以進來（例如它同時要被寫進事件當絕對時間點
# ——但那種情況正確的做法是拆成兩個變數，不是豁免）。
_WALL_CLOCK_INTERVAL_EXEMPT: dict[str, str] = {}


def _wall_clock_interval_anchors(source: str, label: str) -> list[str]:
    """找出「先把 `time.time()` 指派給區域變數，之後再拿 `time.time()` 減掉它」。

    回傳 `<label>:<函式名>:<變數名>` 的清單。這是量間隔用錯時鐘最好認的形狀：
    兩次讀數之間只要牆鐘被調整過，差值就是錯的。

    **刻意不涵蓋** `end = time.time() + timeout` / `while time.time() < end` 那種
    截止時刻形狀——那是同一類缺陷的另一個面貌，但範圍大得多（本模組還有十來處），
    要另外處理。加進來之前先想清楚：有些逾時迴圈的測試依賴「逾時**不會**到」，
    換成會前進的時鐘會翻掉行為。
    """
    def is_time_time(node) -> bool:
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "time"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time"
                and not node.args)

    hits: list[str] = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        anchors = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and is_time_time(node.value):
                anchors.update(t.id for t in node.targets
                               if isinstance(t, ast.Name))
        if not anchors:
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)):
                continue
            # 兩個方向都算：`time.time() - anchor`（經過多久）與
            # `anchor - time.time()`（還剩多久）是同一個毛病。
            for a, b in ((node.left, node.right), (node.right, node.left)):
                if is_time_time(a) and isinstance(b, ast.Name) and b.id in anchors:
                    key = f"{label}:{fn.name}:{b.id}"
                    if key not in hits:
                        hits.append(key)
    return hits


def test_no_interval_is_measured_with_the_adjustable_wall_clock():
    """形狀守門：共用模組與兩個變體裡，這個形狀應該一個都不剩。

    上面兩支是行為測試，但它們只蓋到 `run_batch` 的兩個判定；`generate_loop` 的
    `elapsed_sec`、`reject_cookies` 的 settle 等待沒有專屬的行為測試，靠這一支守。

    **自我檢查**（合成原始碼）是必要的：比對條件寫壞了的話，這支測試會安靜地
    永遠綠——掃不到東西跟「真的沒東西」在輸出上長得一模一樣。
    """
    print("test_no_interval_is_measured_with_the_adjustable_wall_clock")

    # --- 自我檢查 1：壞形狀一定要被抓到 -------------------------------------
    bad = _wall_clock_interval_anchors(
        "import time\n"
        "def f():\n"
        "    t0 = time.time()\n"
        "    while True:\n"
        "        if time.time() - t0 > 5:\n"
        "            break\n",
        "synthetic")
    assert bad == ["synthetic:f:t0"], f"比對條件抓不到壞形狀了: {bad}"

    # 除法包在外面（`run_batch` 的 `elapsed_h` 就長這樣）也要抓到。
    nested = _wall_clock_interval_anchors(
        "import time\n"
        "def g():\n"
        "    s = time.time()\n"
        "    h = (time.time() - s) / 3600\n",
        "synthetic")
    assert nested == ["synthetic:g:s"], f"包在別的運算裡就漏掉了: {nested}"

    # --- 自我檢查 2：修好的寫法不可以被誤報 ---------------------------------
    good = _wall_clock_interval_anchors(
        "import time\n"
        "def f():\n"
        "    t0 = time.monotonic()\n"
        "    while True:\n"
        "        if time.monotonic() - t0 > 5:\n"
        "            break\n"
        "def h():\n"
        # 絕對時間點：算出來就送走，不拿來量間隔 → 必須留在牆鐘，不可誤報。
        "    wake_ts = time.time() + 3600\n"
        "    emit(wake_ts=wake_ts)\n",
        "synthetic")
    assert good == [], f"修好的寫法被誤報了: {good}"

    # --- 真正的掃描 ---------------------------------------------------------
    pkg = Path(__file__).resolve().parent.parent / "axiomatic"
    found: list[str] = []
    for name in ("_webrunner_shared.py", "webrunner_novelai.py",
                 "webrunner_je_only.py"):
        found += _wall_clock_interval_anchors(
            (pkg / name).read_text(encoding="utf-8"), name)
    unexpected = [k for k in found if k not in _WALL_CLOCK_INTERVAL_EXEMPT]
    assert not unexpected, (
        "用可調整的牆上時鐘量經過多久（NTP 的 step 修正／手動改時鐘／VM 快照還原"
        "都會讓差值出錯；換時區與日光節約時間不會，那是 UTC epoch 秒）："
        f"{unexpected}。改用 time.monotonic()；若這個錨點**同時**要當絕對時間點"
        "（寫進事件、跟檔案 mtime 比），拆成兩個變數，不要折衷。")
    # 名單裡的項目若已經修掉就要移除，否則豁免會越積越多。
    stale = [k for k in _WALL_CLOCK_INTERVAL_EXEMPT if k not in found]
    assert not stale, f"豁免名單有過期項目，請刪掉：{stale}"
    print("  PASS\n")


# ---------- DOM 輪詢逾時：截止時刻必須用單調時鐘 -----------------------------
#
# 形狀是 `end = <時鐘>() + timeout` 配 `while <時鐘>() < end`，共用模組有 7 處：
# `select_model`（等下拉觸發器、等選項各一）、`_click_gender`、
# `click_add_character_control`、`_click_option_by_text`、`select_sampler`、
# `click_generate`。這些全是「量經過多久」→ 一律單調時鐘。
#
# 為什麼值得專門測：牆鐘往回跳一小時，一次幾秒的 DOM 逾時就變成一小時的停頓
# ——**卡住比失敗更難查**，監督者看到的是一個還活著、卻什麼都不做的行程；往前
# 跳則讓逾時立刻觸發，把重試迴圈退化成只試一次。這台機器是無人值守跑好幾天
# 的，兩個方向都真的會遇到。
#
# 上面那支 `_wall_clock_interval_anchors` 的形狀守門**看不到**這一類：它找的是
# `time.time() - anchor` 的相減，截止時刻用的是相加＋比較。下面另有一支形狀守
# 門專門收這個形狀，兩支合起來才把「只改一半」的兩種寫法都蓋住。


class _BoundedHits(list):
    """會自己喊停的計數 list。

    逾時判定要是被改成「永不逾時」（例如截止時刻用牆鐘的 epoch 秒、判定基準用
    單調時鐘的開機秒數），迴圈就再也不會結束。**掛住比紅字難查太多**——紅字一行
    講完問題，掛住要 stack dump 才看得出來，而且會把後面排隊的東西全部擋住。
    """

    _MAX = 100_000

    def append(self, item):
        list.append(self, item)
        if len(self) > self._MAX:
            raise AssertionError(
                f"輪詢超過 {self._MAX} 圈仍未逾時——截止時刻的判定失效了"
                "（截止時刻與判定基準是不是用了不同的時鐘？）")


class _DeadlineProbePort:
    """所有查詢一律「找不到」的極簡 port，讓逾時迴圈必然跑滿。

    `_MAX_POLLS` 是防呆，不是效能考量：逾時判定要是被改成「永不逾時」，這裡會
    丟出一句講清楚原因的錯誤，而不是讓整個回合掛在那裡。**掛住比紅字難查太多**
    ——紅字一行講完問題，掛住要 stack dump 才看得出來，而且會把後面排隊的東西
    全部擋住。
    """

    TRANSPORT_ERRORS = ()
    _MAX_POLLS = 100_000

    def __init__(self):
        self.polls = 0

    def _poll(self):
        self.polls += 1
        if self.polls > self._MAX_POLLS:
            raise AssertionError(
                f"輪詢超過 {self._MAX_POLLS} 圈仍未逾時——截止時刻的判定失效了")
        return None

    def execute_script(self, script, *_args):
        # 比對要**夠精確**：選項那段 JS 本文裡就含有 `el.scrollIntoView(...)`，
        # 用子字串比對會把整段選項查詢誤判成捲動、輪詢一圈都數不到。
        if script.strip().startswith("arguments[0].scrollIntoView"):
            return None
        return self._poll()

    def find_elements_xpath(self, _xpath):
        self._poll()
        return []

    def find_element_xpath(self, _xpath):
        return self._poll()

    def click(self, _element, pause: float = 0.15):
        pass

    def save_screenshot(self, _path):
        pass


@contextlib.contextmanager
def _ws_patched(**names):
    """暫時換掉 `_webrunner_shared` 的模組層名字，離開時原樣還原。"""
    saved = {k: getattr(ws, k) for k in names}
    for key, value in names.items():
        setattr(ws, key, value)
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(ws, key, value)


def _case_select_model_trigger(hits):
    """`select_model` 第一個迴圈：等模型下拉的觸發器出現。"""
    port = _DeadlineProbePort()

    def _no_trigger(_port):
        hits.append(1)
        return None

    with _ws_patched(_find_model_trigger=_no_trigger,
                     _dump_model_options=lambda *_a, **_k: None,
                     snap=lambda *_a, **_k: None):
        return ws.select_model(port, "NAI Diffusion V5 Full", timeout=12.0)


def _case_select_model_option(hits):
    """`select_model` 第二個迴圈：觸發器找到了，等選項出現。"""
    port = _DeadlineProbePort()

    def _script(script, *_args):
        if script.strip().startswith("arguments[0].scrollIntoView"):
            return None
        hits.append(1)
        return None

    port.execute_script = _script
    with _ws_patched(_find_model_trigger=lambda _p: _FakeElement("DIV"),
                     _dump_model_options=lambda *_a, **_k: None,
                     snap=lambda *_a, **_k: None):
        return ws.select_model(port, "NAI Diffusion V5 Full", timeout=12.0)


def _case_click_gender(hits):
    port = _DeadlineProbePort()

    def _script(_script_text, *_args):
        hits.append(1)
        return None

    port.execute_script = _script
    return ws._click_gender(port, "Female", timeout=4.0)


def _case_add_character(hits):
    port = _DeadlineProbePort()

    def _find(_xpath):
        hits.append(1)
        return []

    port.find_elements_xpath = _find
    port.execute_script = lambda *_a, **_k: None
    with _ws_patched(snap=lambda *_a, **_k: None):
        return ws.click_add_character_control(port, "Female", timeout=8.0)


def _case_click_option_by_text(hits):
    port = _DeadlineProbePort()

    def _find(_xpath):
        hits.append(1)
        return []

    port.find_elements_xpath = _find
    return ws._click_option_by_text(port, "Female", timeout=5.0)


def _case_select_sampler(hits):
    port = _DeadlineProbePort()

    def _no_option(_port, _target):
        hits.append(1)
        return None

    with _ws_patched(read_current_sampler=lambda _p: "Euler",
                     _find_sampler_trigger=lambda _p: _FakeElement("DIV"),
                     _find_sampler_option=_no_option,
                     _dump_sampler_options=lambda *_a, **_k: None,
                     snap=lambda *_a, **_k: None):
        return ws.select_sampler(port, "DPM++ 2M", timeout=10.0)


def _case_click_generate(hits):
    port = _DeadlineProbePort()

    def _no_button(_port):
        hits.append(1)
        return None

    with _ws_patched(find_generate_button=_no_button):
        return ws.click_generate(port, timeout=15.0)


# (名稱, timeout 秒, 每圈 sleep 秒, 執行函式)。執行函式收一個 list，每輪詢一圈
# 就 append 一次，回傳被測函式的回傳值；條件刻意永遠不成立，迴圈才會跑滿。
_DEADLINE_CASES = (
    ("select_model:end", 12.0, 0.3, _case_select_model_trigger),
    ("select_model:option_end", 12.0, 0.3, _case_select_model_option),
    ("_click_gender:end", 4.0, 0.2, _case_click_gender),
    ("click_add_character_control:end", 8.0, 0.3, _case_add_character),
    ("_click_option_by_text:end", 5.0, 0.2, _case_click_option_by_text),
    ("select_sampler:end", 10.0, 0.3, _case_select_sampler),
    ("click_generate:end", 15.0, 0.3, _case_click_generate),
)


def _run_deadline_case(case, wall_jump: float = 0.0, jump_after: int = 3,
                       realistic_origins: bool = False):
    """跑一次逾時迴圈，回傳 `(圈數, 虛擬單調秒數, 回傳值)`。

    `wall_jump` 非 0 時，第 `jump_after` 次 `sleep` 之後推一下**牆鐘**——
    `_FakeClock.wall_skew` 只影響 `time()`，`monotonic()` 照走，這正是 NTP 的
    step 修正／手動改時鐘在真實世界的樣子。

    `realistic_origins=True` 時把兩個時鐘的**原點**拉開成真實的樣子：牆鐘讀的是
    epoch 秒（約 1.7e9），單調時鐘讀的是開機後秒數（這裡取 42000）。真實世界本來
    就是這樣，而這正是「只改一半」唯一測得出來的方式——截止時刻與判定基準用了不同
    時鐘的話，兩者沒有共同的零點：不是永遠不逾時（會卡死），就是一進迴圈立刻逾時。

    `random` 要固定種子：兩個 case 的迴圈前面有 `human_pause`，長度隨機就會讓
    虛擬時鐘的浮點累加路徑在三次執行之間分岔，圈數差一圈，斷言只好放寬到看不
    出東西。存回原狀態，不污染其他測試。
    """
    hits = _BoundedHits()
    if realistic_origins:
        clock = _FakeClock(start=42_000.0)          # monotonic ＝ 開機後秒數
        clock.wall_skew = 1_700_000_000.0 - 42_000.0  # time() ＝ epoch 秒
    else:
        clock = _FakeClock()
    sleeps = {"n": 0}

    def _on_sleep(clk, _seconds):
        sleeps["n"] += 1
        if wall_jump and sleeps["n"] == jump_after:
            clk.wall_skew += wall_jump

    clock.on_sleep = _on_sleep
    started = clock.monotonic()
    rand_state = random.getstate()
    random.seed(20260907)
    try:
        with _fake_clock(clock):
            result = case[3](hits)
    finally:
        random.setstate(rand_state)
    return len(hits), clock.monotonic() - started, result


def test_dom_poll_timeouts_ignore_a_wall_clock_jump():
    """7 處逾時迴圈：牆鐘怎麼跳都不得改變判定，而且逾時仍然要正確觸發。

    四個方向缺一不可，各自蓋不同的改壞方式：

    1. **不跳**：逾時要真的在設定的秒數觸發。少了這一條，把逾時改成**永不逾時**
       也會綠——那比原本的缺陷更糟，會真的永久卡住。
    2. **牆鐘往回跳**：用牆鐘的話迴圈會多轉一小時（幾千圈）。
    3. **牆鐘往前跳**：用牆鐘的話會立刻逾時，重試退化成只試一次。
    4. **兩個時鐘原點不同**（真實世界本來就是：牆鐘是 epoch 秒、單調時鐘是開機
       後秒數）：這是唯一測得出「只改一半」的方向。2 與 3 的跳動發生在迴圈裡、
       截止時刻已經算完了，所以**只把賦值那行退回牆鐘**在那兩個方向下完全沒有
       症狀；原點一拉開就立刻現形——不是永遠不逾時，就是一進迴圈立刻逾時。
    """
    print("test_dom_poll_timeouts_ignore_a_wall_clock_jump")
    for case in _DEADLINE_CASES:
        name, timeout, poll, _fn = case
        expected = round(timeout / poll)

        # (1) 沒有時鐘跳動：逾時要真的觸發，圈數要跟 timeout/poll 對得起來。
        base_polls, base_elapsed, base_result = _run_deadline_case(case)
        assert not base_result, f"{name}: 條件永遠不成立卻回了成功值"
        assert abs(base_polls - expected) <= 2, (
            f"{name}: 輪詢 {base_polls} 圈，預期約 {expected} 圈"
            f"（timeout {timeout}s / poll {poll}s）——逾時判定不對")
        assert timeout <= base_elapsed <= timeout + 5.0, (
            f"{name}: 迴圈只走了 {base_elapsed:.2f}s 虛擬時間，逾時是 {timeout}s")

        # (2) 牆鐘往回跳一小時：用牆鐘的話這裡會多轉一小時（幾千圈）。
        back_polls, back_elapsed, _ = _run_deadline_case(case, wall_jump=-3600.0)
        assert abs(back_polls - base_polls) <= 1, (
            f"{name}: 牆鐘往回跳 1h 之後圈數從 {base_polls} 變成 {back_polls}"
            "——逾時判定還在用 time.time()，幾秒的等待會變成幾小時的停頓")
        assert back_elapsed <= timeout + 5.0, (
            f"{name}: 牆鐘往回跳讓迴圈跑了 {back_elapsed:.0f}s")

        # (3) 牆鐘往前跳一小時：用牆鐘的話這裡會立刻逾時，退化成只試幾圈。
        fwd_polls, fwd_elapsed, _ = _run_deadline_case(case, wall_jump=3600.0)
        assert abs(fwd_polls - base_polls) <= 1, (
            f"{name}: 牆鐘往前跳 1h 之後圈數從 {base_polls} 變成 {fwd_polls}"
            "——逾時判定還在用 time.time()，重試迴圈退化成只試一次")
        assert fwd_elapsed >= timeout - poll * 2, (
            f"{name}: 牆鐘往前跳讓迴圈只跑了 {fwd_elapsed:.2f}s")

        # (4) 兩個時鐘原點不同：截止時刻與判定基準必須是**同一個**時鐘。
        mix_polls, mix_elapsed, _ = _run_deadline_case(
            case, realistic_origins=True)
        assert abs(mix_polls - base_polls) <= 1, (
            f"{name}: 牆鐘與單調時鐘的原點拉開之後圈數從 {base_polls} 變成 "
            f"{mix_polls}——截止時刻與判定基準用了不同的時鐘（只改了一半）")
        assert timeout <= mix_elapsed <= timeout + 5.0, (
            f"{name}: 原點拉開之後迴圈跑了 {mix_elapsed:.2f}s，逾時是 {timeout}s")
        print(f"  {name}: {base_polls} 圈 / {base_elapsed:.1f}s "
              f"(往回 {back_polls}、往前 {fwd_polls}、異原點 {mix_polls})")
    print("  PASS\n")


def test_the_deadline_probe_clock_actually_advances():
    """證明上面那支不是假綠——**假時鐘必須真的在走，而且兩個時鐘要分得開**。

    把假的 `monotonic` 釘成常數是這類測試最典型的假綠：`end` 是用第一次讀數加
    出來的，讀數不動等於 `end` 跟著不動，條件永遠成立或永遠不成立，測試就為了
    錯的理由變綠（或紅）。
    """
    print("test_the_deadline_probe_clock_actually_advances")
    clock = _FakeClock()
    t0_mono, t0_wall = clock.monotonic(), clock.time()
    clock.sleep(5.0)
    assert clock.monotonic() - t0_mono == 5.0, "sleep 沒有推動單調時鐘"
    assert clock.time() - t0_wall == 5.0, "sleep 沒有推動牆鐘"
    clock.wall_skew -= 3600.0
    assert clock.monotonic() - t0_mono == 5.0, "wall_skew 不該影響單調時鐘"
    assert clock.time() - t0_wall == -3595.0, "wall_skew 沒有讓牆鐘分岔"

    # 而且它要真的驅動被測迴圈：15s / 0.3s = 50 圈，虛擬時間也要真的走滿。
    polls, elapsed, result = _run_deadline_case(
        ("click_generate:end", 15.0, 0.3, _case_click_generate))
    assert result is False, result
    assert 48 <= polls <= 52, f"圈數 {polls} 不像跑了 15s／每圈 0.3s"
    assert 15.0 <= elapsed <= 15.5, f"虛擬時間只走了 {elapsed}s"
    print("  PASS\n")


# 形狀守門的豁免名單，key 是 `<檔名>:<函式名>:<變數名>`，value 是**理由**。
# 唯一正當的理由是「這個變數不是逾時判定，是要離開本行程的絕對時間點」——而那
# 種情況正確的做法是拆成兩個變數（見 `run_batch` 的 `rest_started_mono` /
# `wake_ts`），不是豁免。
_WALL_CLOCK_DEADLINE_EXEMPT: dict[str, str] = {}


def _clock_attr(node) -> str | None:
    """`time.time()` → `"time"`；`time.monotonic()` → `"monotonic"`；其餘 None。"""
    if not (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "time"
            and not node.args):
        return None
    return node.func.attr if node.func.attr in ("time", "monotonic") else None


def _wall_clock_deadline_pairs(source: str, label: str) -> list[str]:
    """找出「截止時刻」形狀裡碰到牆鐘的：`X = <時鐘>() + …` 之後 `<時鐘>() < X`。

    回傳 `<label>:<函式名>:<變數名>` 的清單。**賦值端與比較端只要任一個是
    `time.time()` 就算命中**，兩邊用不同時鐘（＝只改了一半）也一樣要抓：截止
    時刻與判定基準沒有共同的零點，逾時可能立刻觸發、也可能永遠不觸發，比兩邊
    都用牆鐘更糟。

    刻意要求**兩件事同時成立**（有這個賦值、而且有拿時鐘去跟它比），所以
    `wake_ts = time.time() + rest_s` 這種「算出來就送走的絕對時間點」不會被
    誤報——那一處是刻意保留的牆鐘值。
    """
    hits: list[str] = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # 變數名 → 賦值時用的時鐘。
        deadlines: dict[str, str] = {}
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.BinOp)
                    and isinstance(node.value.op, ast.Add)):
                continue
            clock = (_clock_attr(node.value.left)
                     or _clock_attr(node.value.right))
            if clock is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    deadlines[target.id] = clock
        if not deadlines:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left] + list(node.comparators)
            for left, right in zip(operands, operands[1:]):
                for a, b in ((left, right), (right, left)):
                    cmp_clock = _clock_attr(a)
                    if cmp_clock is None or not isinstance(b, ast.Name):
                        continue
                    assign_clock = deadlines.get(b.id)
                    if assign_clock is None:
                        continue
                    if "time" not in (cmp_clock, assign_clock):
                        continue
                    key = f"{label}:{fn.name}:{b.id}"
                    if key not in hits:
                        hits.append(key)
    return hits


def test_no_poll_deadline_is_measured_with_the_adjustable_wall_clock():
    """形狀守門：截止時刻形狀在共用模組與兩個變體裡應該一個牆鐘都不剩。

    **自我檢查是必要的**：比對條件寫壞了的話，這支測試會安靜地永遠綠——掃不到
    東西跟「真的沒東西」在輸出上長得一模一樣。
    """
    print("test_no_poll_deadline_is_measured_with_the_adjustable_wall_clock")

    # --- 自我檢查 1：原本的缺陷（兩邊都是牆鐘）一定要被抓到 -----------------
    both = _wall_clock_deadline_pairs(
        "import time\n"
        "def f(timeout):\n"
        "    end = time.time() + timeout\n"
        "    while time.time() < end:\n"
        "        pass\n",
        "synthetic")
    assert both == ["synthetic:f:end"], f"抓不到原本的缺陷: {both}"

    # --- 自我檢查 2：「只改一半」的兩種寫法都要抓到 -------------------------
    # 只把 `while` 那行退回牆鐘。
    half_cmp = _wall_clock_deadline_pairs(
        "import time\n"
        "def f(timeout):\n"
        "    end = time.monotonic() + timeout\n"
        "    while time.time() < end:\n"
        "        pass\n",
        "synthetic")
    assert half_cmp == ["synthetic:f:end"], f"只退比較端沒抓到: {half_cmp}"
    # 只把賦值那行退回牆鐘。
    half_assign = _wall_clock_deadline_pairs(
        "import time\n"
        "def f(timeout):\n"
        "    end = time.time() + timeout\n"
        "    while time.monotonic() < end:\n"
        "        pass\n",
        "synthetic")
    assert half_assign == ["synthetic:f:end"], f"只退賦值端沒抓到: {half_assign}"

    # --- 自我檢查 3：修好的寫法與刻意的例外都不可以被誤報 -------------------
    good = _wall_clock_deadline_pairs(
        "import time\n"
        "def f(timeout):\n"
        "    end = time.monotonic() + timeout\n"
        "    while time.monotonic() < end:\n"
        "        pass\n"
        "def g(rest_s):\n"
        # 絕對時間點：算出來就寫進事件給別的行程比對 → 必須留牆鐘，不可誤報。
        "    wake_ts = time.time() + rest_s\n"
        "    emit(wake_ts=wake_ts)\n",
        "synthetic")
    assert good == [], f"修好的寫法或刻意的例外被誤報了: {good}"

    # --- 真正的掃描 ---------------------------------------------------------
    pkg = Path(__file__).resolve().parent.parent / "axiomatic"
    found: list[str] = []
    for name in ("_webrunner_shared.py", "webrunner_novelai.py",
                 "webrunner_je_only.py"):
        found += _wall_clock_deadline_pairs(
            (pkg / name).read_text(encoding="utf-8"), name)
    unexpected = [k for k in found if k not in _WALL_CLOCK_DEADLINE_EXEMPT]
    assert not unexpected, (
        "逾時的截止時刻用了可調整的牆上時鐘（NTP 的 step 修正／手動改時鐘／VM "
        "快照還原都會讓它跳；換時區與日光節約時間不會，那是 UTC epoch 秒）："
        f"{unexpected}。往回跳會把幾秒的 DOM 逾時變成幾小時的停頓，往前跳會讓"
        "重試退化成只試一次——改用 time.monotonic()，而且**兩邊要一起改**。")
    stale = [k for k in _WALL_CLOCK_DEADLINE_EXEMPT if k not in found]
    assert not stale, f"豁免名單有過期項目，請刪掉：{stale}"
    print("  PASS\n")


# ---------- 輸出資料夾配置：崩潰續跑必須落回**同一個**資料夾 -----------------

@contextlib.contextmanager
def _output_root(tmp: Path):
    """把 `OUTPUT_ROOT` 指到 tmp，離開時還原。"""
    saved = ws.OUTPUT_ROOT
    ws.OUTPUT_ROOT = tmp
    try:
        yield tmp
    finally:
        ws.OUTPUT_ROOT = saved


def _folder_with_file(root: Path, name: str, mtime: float) -> Path:
    """建一個含單一檔案的資料夾，並把該檔的 mtime 設成指定值。

    刻意用 `os.utime` 而不是「真的等一下再寫」：判準是檔案 mtime 與 `batch_start`
    的先後，用真實時間寫會讓測試依賴檔案系統的時間解析度（FAT32 是 2 秒），
    在別台機器上變成間歇性失敗。
    """
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "x_0001.png"
    f.write_bytes(b"x")
    os.utime(f, (mtime, mtime))
    return folder


def test_allocate_output_dir_reuses_the_folder_of_the_current_batch():
    """**這一支是為了續跑存在的。** 硬殺之後重開，同一個角色要配回**同一個**資料夾。

    配到 `<角色>_2` 的話，檢查點裡記的仍是 `<角色>`：圖散在兩個目錄、`saved` 數不
    對，而且完全不會有錯誤訊息——只會多出一個編號資料夾，而那正是最難發現的症狀。
    """
    print("test_allocate_output_dir_reuses_the_folder_of_the_current_batch")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        with _output_root(root):
            batch_start = 1_700_000_000.0
            _folder_with_file(root, "Alice", batch_start + 60)   # 本輪寫的
            assert ws.allocate_output_dir("Alice", batch_start) == root / "Alice"
            # 邊界：剛好等於 batch_start 也算本輪（判準是 `>=`）。
            _folder_with_file(root, "Bea", batch_start)
            assert ws.allocate_output_dir("Bea", batch_start) == root / "Bea"
    print("  PASS\n")


def test_allocate_output_dir_numbers_past_an_older_batch():
    """上一輪留下的資料夾不能被寫進去——那會把舊圖跟新圖混在一起。"""
    print("test_allocate_output_dir_numbers_past_an_older_batch")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        with _output_root(root):
            batch_start = 1_700_000_000.0
            _folder_with_file(root, "Alice", batch_start - 3600)      # 上一輪
            assert ws.allocate_output_dir("Alice", batch_start) == root / "Alice_2"
            _folder_with_file(root, "Alice_2", batch_start - 3600)    # 更早的
            assert ws.allocate_output_dir("Alice", batch_start) == root / "Alice_3"
            # `_3` 是本輪的 → 停在這裡重用，不再往上編號。
            _folder_with_file(root, "Alice_3", batch_start + 5)
            assert ws.allocate_output_dir("Alice", batch_start) == root / "Alice_3"
    print("  PASS\n")


def test_an_empty_or_missing_folder_is_taken_as_is():
    """空資料夾算「本輪的」——否則每次重跑都會多長一個編號目錄出來。"""
    print("test_an_empty_or_missing_folder_is_taken_as_is")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        with _output_root(root):
            assert ws.allocate_output_dir("Bob", 1_700_000_000.0) == root / "Bob"
            (root / "Bob").mkdir()
            assert ws.allocate_output_dir("Bob", 1_700_000_000.0) == root / "Bob"
            # 只有子目錄、沒有檔案 → 一樣算空：`_folder_belongs_to_batch` 只看
            # **直屬的檔案**。這是刻意的（輸出目錄裡只會有圖），寫下來是因為它
            # 從函式名字看不出來。
            (root / "Bob" / "sub").mkdir()
            assert ws.allocate_output_dir("Bob", 1_700_000_000.0) == root / "Bob"
    print("  PASS\n")


def test_a_folder_we_cannot_list_is_never_written_into():
    """列不出內容 ⇒ 不能斷定它是本輪的 ⇒ 讓號。

    誤判的代價不對稱：多開一個編號目錄只是難看，判錯成「本輪的」則是把上一輪的圖
    跟這一輪混在同一個資料夾裡。所以 `OSError` 一律回 False。
    """
    print("test_a_folder_we_cannot_list_is_never_written_into")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        with _output_root(root):
            (root / "Carol").mkdir()
            orig_iterdir = Path.iterdir

            def _raise(self):
                if self.name == "Carol":
                    raise OSError("permission denied")
                return orig_iterdir(self)

            try:
                Path.iterdir = _raise
                assert ws._folder_belongs_to_batch(
                    root / "Carol", 1_700_000_000.0) is False
                assert ws.allocate_output_dir(
                    "Carol", 1_700_000_000.0) == root / "Carol_2"
            finally:
                Path.iterdir = orig_iterdir
    print("  PASS\n")


def test_run_batch_resume_mismatch_emits_event():
    """檢查點在、但配對對不起來（佇列被編輯過）→ 不接續，而且要**留下證據**。

    這一條擋的是 2026-08-23 的實際慘案：接續失敗的唯一線索只印在主控台，視窗
    關掉就再也查不出原因。事件必須只帶欄位**名稱**——它會被寫進 events.ndjson
    這種跨行程、bot 會轉貼的檔案，欄位內容是提示詞全文，不該跟著跑。"""
    print("test_run_batch_resume_mismatch_emits_event")
    with _RunBatchHarness() as h:
        h.cfg_over = {"images_per_character": 5}
        h.write_queue("todo_prompt.md", ["P"])
        h.write_queue("todo_character1.md", ["a"])
        folder = h.dir / "output" / "a"
        folder.mkdir(parents=True)
        for i in (1, 2):
            (folder / f"a_{i:04d}_20260101_000000.png").write_bytes(b"x")
        # 檢查點記的角色一是舊的（＝佇列被改過）→ 四欄不再全等。
        _rp.write_progress("P", "OLD-CHARACTER", "", "", "a", 5)
        rc = _run_batch(FakeBrowserPort())
        gen = list(h.gen_calls)
        events = list(h.events)
    assert rc == 0, rc
    assert len(gen) == 1, gen
    assert gen[0]["resume_count"] == 0, f"對不起來就不可以接續: {gen[0]}"
    mismatch = [kw for et, kw in events if et == "resume_mismatch"]
    assert len(mismatch) == 1, f"必須剛好發一筆 resume_mismatch: {events}"
    assert mismatch[0].get("fields") == ["char1"], mismatch[0]
    assert "OLD-CHARACTER" not in repr(mismatch[0]), (
        f"事件不得帶欄位內容: {mismatch[0]}")
    print("  PASS\n")


def test_batch_min_save_ratio_clamp():
    """安全不變式：`min_save_ratio` 必須落在 (0,1]，否則 pop 門檻失去意義——設成 >1
    會讓「已全數產出」的角色永遠達不到門檻、每輪被無限重產（佇列 desync）；設成 <=0
    則門檻恆退為 1。驗證 loader 對每種壞值都退回安全預設，且任何合法比率下、完整產出
    的角色一定會 pop（pop_threshold ≤ target），杜絕無限重產。"""
    print("test_batch_min_save_ratio_clamp")
    import math
    import _batch_config as bc
    # 合法 (0,1] 原樣通過。
    for good in (0.01, 0.5, 0.9, 1.0):
        assert bc._coerce_ratio(good, 0.9) == good, f"valid {good} must pass"
    # 壞值（>1／0／負／非數／bool）一律退回預設（仍在 (0,1]）。
    for bad in (1.0001, 2, 5, 0, -0.5, "0.9", None, True, False, [0.9]):
        got = bc._coerce_ratio(bad, 0.9)
        assert got == 0.9, f"bad {bad!r} must fall back to default, got {got}"
        assert 0 < got <= 1, f"clamp broken for {bad!r}: {got}"
    # 端到端載入值恆在 (0,1]（不管磁碟上的 batch_config 內容為何）。
    live = bc.load_batch_config()["min_save_ratio"]
    assert 0 < live <= 1, f"live min_save_ratio out of range: {live!r}"
    # 端到端不變式：任何合法比率、任何 target 下，完整產出的角色一定會 pop
    # （門檻不超過 target），永不無限重產——這正是 clamp 存在的理由。
    for ratio in (0.01, 0.5, 0.9, 1.0):
        for target in (1, 5, 240):
            thr = max(1, math.ceil(target * ratio))
            assert thr <= target, (
                f"ratio={ratio} target={target} -> thr {thr} > target "
                f"(a full character would never pop!)")
    print("  PASS\n")


def test_char2_helpers_never_touch_char1_when_slot_missing():
    """空的 todo_character2 列會讓 set_character2_enabled 移除整張 Character 2
    卡；此時 NovelAI 只剩一個角色欄位，find_prompt_areas()[1] 其實是
    **Character 1**。若 fill/verify_character_prompt 照樣往 areas[1] 寫，會把
    char1 的 prompt 洗掉（in-band 單圖插隊後的 _refill_character_fields 正是
    這條路），該角色剩下的圖全部缺 char1。守衛：Character N (N>=2) 的標題找
    不到就不碰任何欄位——要求清空視為已達成，要求寫入非空值回 False。"""
    print("test_char2_helpers_never_touch_char1_when_slot_missing")

    class _NoChar2Port(FakeBrowserPort):
        """只有 Character 1 卡：'Character 2' 標題查無，角色欄位只有一個。"""

        def __init__(self):
            super().__init__()
            self.areas = [self.main, self.char1]
            self.char_count = 1     # Character 2 那張卡整個不存在

        def find_elements_xpath(self, xpath):
            if "Character 2" in xpath:
                return []
            return super().find_elements_xpath(xpath)

    saved = ws.human_pause
    try:
        ws.human_pause = lambda *a, **k: None
        port = _NoChar2Port()
        port.char1.value = "BATCH_CHAR1"
        # 清空請求：視為已達成，且完全不寫入。
        assert ws.fill_character_prompt(port, 2, "") is True
        assert ws.verify_character_prompt(port, 2, "") is True
        # 寫入非空值：回 False，仍不得寫入。
        assert ws.fill_character_prompt(port, 2, "X") is False
        assert ws.verify_character_prompt(port, 2, "X") is False
        assert port.set_values == [], (
            f"char2 helpers must not write anything: {port.set_values}")
        assert port.char1.value == "BATCH_CHAR1", (
            f"Character 1 prompt was clobbered: {port.char1.value!r}")
        # Character 1 不受此守衛影響（單角色模式下標題可能不渲染，但欄位存在）。
        assert ws.fill_character_prompt(port, 1, "NEW1") is True
        assert port.char1.value == "NEW1", port.char1.value
    finally:
        ws.human_pause = saved
    print("  PASS\n")


# ---------- verify_character_prompt 的記錄合約 -------------------------------
# 這裡的缺陷不是功能面的，是**敘述面**的：reload 之後的每一個額度週期都會印
#     WARN: Character 1 drifted after fill (0 vs 178 chars); refilling
#     [Character 1] card area head: '\n'
# 然後就沒有下文——重填成功時舊版直接 return True，一個字都不印。人讀到的是
# 「欄位是空的、重填了、結果不明」，而那正好是本專案記錄在案最貴的失效形態
# （欄位被部分打回去，接著安靜產出一整批用錯提示詞的圖）。實測 2026-09-03～
# 09-08：drift 90 次、`still mismatched after refill` **0 次**、`char*_drift`
# 快照 **0 張**，90 次 drift 與 90 次 reload 一對一——也就是重填 90/90 全部成功，
# 批次一直是好的。要確認這件事得讀完兩個函式。
#
# 這是 `discord_bot.log` 那一課的鏡像：那次是
# 無害的事情喊了一萬次把訊號淹掉，這次是嚇人的事情只喊了一半、把讓人安心的那
# 一半吞掉。結果一樣——記錄不再能被信任。下面五支釘住三種結局都要出聲、分流用
# 的是欄位內容、以及那句「寫入前的讀值」不會再被讀成結果。


class _StickyRefillPort(FakeBrowserPort):
    """角色欄一開始是空的（reload 之後的實測樣態），重填會**吃進去**。"""

    def __init__(self):
        super().__init__()
        self.char1.value = ""


class _RejectingRefillPort(FakeBrowserPort):
    """重填寫不進去：native setter 照記錄，但欄位值不動（模擬 React 打回來）。"""

    def __init__(self):
        super().__init__()
        self.char1.value = ""

    def execute_script(self, script, *args):
        if "getOwnPropertyDescriptor" in script and "execCommand" in script:
            # 記下有人試圖寫，但**不反映到元素上**——欄位維持空的。
            self.set_values.append(args[1] if len(args) > 1 else "")
            return None
        return super().execute_script(script, *args)


def _verify_output(port, index, expected):
    """跑 verify_character_prompt，回 (回傳值, stdout 全文)。"""
    saved = ws.human_pause
    try:
        ws.human_pause = lambda *a, **k: None
        with _fake_clock():
            with contextlib.redirect_stdout(io.StringIO()) as out:
                got = ws.verify_character_prompt(port, index, expected)
    finally:
        ws.human_pause = saved
    return got, out.getvalue()


def test_a_successful_refill_says_so_instead_of_going_silent():
    """重填成功必須**講出結局**，不能沉默地 return True。

    沉默的成功等於要求讀記錄的人去讀原始碼才知道有沒有出事，而這條路徑上
    「不知道結局」的預設解讀剛好是最壞的那一種。
    """
    print("test_a_successful_refill_says_so_instead_of_going_silent")
    port = _StickyRefillPort()
    got, text = _verify_output(port, 1, "EXPECTED_CHAR1")

    assert got is True, got
    assert port.char1.value == "EXPECTED_CHAR1", port.char1.value
    # 核心斷言：偵測那一行之後**還有**一行講結局。
    assert "refilled OK" in text, (
        "重填成功卻什麼都沒說——記錄停在「欄位是空的、重填了」，讀的人只能去讀"
        f"原始碼才知道結局。實際輸出：\n{text}")
    # 而且要說得出是哪個角色、補了多少，否則兩個角色交錯時分不出來。
    result_line = next(ln for ln in text.splitlines() if "refilled OK" in ln)
    assert "Character 1" in result_line, result_line
    assert str(len("EXPECTED_CHAR1")) in result_line, result_line
    print("  PASS\n")


def test_a_routine_blank_field_is_not_dressed_up_as_an_alarm():
    """reload 之後讀回空欄位是**穩態**（實測 90/90 重填就好），措辭要照實。

    每個正常週期都會響一次的 WARN 等於沒有 WARN。這裡釘的是「降級」不是
    「消音」：偵測、重填、結局行一個都不能少。
    """
    print("test_a_routine_blank_field_is_not_dressed_up_as_an_alarm")
    port = _StickyRefillPort()
    got, text = _verify_output(port, 1, "EXPECTED_CHAR1")

    assert got is True, got
    assert "WARN" not in text, (
        "空欄位是 reload 之後的穩態，不該用 WARN 喊——每輪都響的警告會被忽略，"
        f"連帶把真正異常的那些一起淹掉：\n{text}")
    # 但**必須仍然報告**：不得為了讓記錄乾淨就把偵測藏起來。
    assert "blank after fill" in text, (
        f"偵測不得消音，只能降級措辭：\n{text}")
    print("  PASS\n")


def test_a_contaminated_field_still_shouts():
    """非空但不同 ＝ autocomplete 污染，那才是 docstring 講的真實威脅。

    分流判準是**欄位裡的內容**，不是呼叫端傳進來的旗標——旗標會在新增呼叫點
    時被忘記（症狀是嚴重度靜靜地標錯），內容則不會跟現實脫節。
    """
    print("test_a_contaminated_field_still_shouts")
    port = _StickyRefillPort()
    port.char1.value = "EXPECTED_CHAR1, some_suggested_tag"
    got, text = _verify_output(port, 1, "EXPECTED_CHAR1")

    assert got is True, got
    assert "WARN" in text and "drifted after fill" in text, (
        "被插入建議 tag 是實測 0 次、真的發生時要看得見的那一種，必須維持 "
        f"WARN：\n{text}")
    assert "blank after fill" not in text, (
        f"非空的欄位不得走空白那條敘述：\n{text}")
    # 污染這條路修好之後同樣要講出結局。
    assert "refilled OK" in text, text
    print("  PASS\n")


def test_a_failed_refill_keeps_its_existing_alarm_and_returns_false():
    """重填**失敗**才是真正異常：既有的 WARN、回傳 False、快照都要在。

    降級空欄位的措辭不得順手把這條路一起弄安靜——它是「欄位狀態不明還繼續
    產圖」的唯一守門。
    """
    print("test_a_failed_refill_keeps_its_existing_alarm_and_returns_false")
    port = _RejectingRefillPort()
    snaps = []
    saved_snap = ws.snap
    try:
        ws.snap = lambda _p, tag: snaps.append(tag)
        got, text = _verify_output(port, 1, "EXPECTED_CHAR1")
    finally:
        ws.snap = saved_snap

    assert got is False, "重填救不回來必須回 False"
    assert "still mismatched after refill" in text, (
        f"既有的失敗訊息不得消失：\n{text}")
    assert "refilled OK" not in text, (
        f"失敗的重填不得印出成功行：\n{text}")
    assert snaps, (
        "真正需要現場畫面的就是「重填也救不回來」這一條。快照從每輪都發生的"
        "空白路徑挪過來之後，這裡必須真的有快照，否則等於把診斷刪掉了")
    print("  PASS\n")


def test_the_pre_write_read_cannot_be_misread_as_the_result():
    """`fill_character_prompt` 那句「寫入前的讀值」必須自己說明它是寫入前。

    它是通用的 fill 記錄，但在 verify → refill 這條路上緊接在偵測行後面出現，
    語意剛好相反：舊措辭 `card area head` 會被讀成「重填之後的狀態」，於是一個
    空值看起來像「重填完還是空的」。這是這次缺陷裡最會騙人的一行。
    """
    print("test_the_pre_write_read_cannot_be_misread_as_the_result")
    port = _StickyRefillPort()
    _got, text = _verify_output(port, 1, "EXPECTED_CHAR1")

    assert "card area head" not in text, (
        f"`head` 沒有時間性，緊接在偵測行後面會被讀成結果：\n{text}")
    assert "card area before write" in text, (
        f"寫入前的讀值必須自己標明是寫入前：\n{text}")

    # 順序本身就是敘述的一部分：偵測 → 寫入前的狀態 → 結局。
    lines = [ln for ln in text.splitlines() if ln.strip()]
    idx_detect = next(i for i, ln in enumerate(lines)
                      if "blank after fill" in ln)
    idx_before = next(i for i, ln in enumerate(lines)
                      if "card area before write" in ln)
    idx_result = next(i for i, ln in enumerate(lines) if "refilled OK" in ln)
    assert idx_detect < idx_before < idx_result, (
        f"敘述順序應為 偵測 → 寫入前狀態 → 結局，實際：\n{text}")
    print("  PASS\n")


# ---------- dead browsing context（chrome 中途被截斷）------------------------
# 視窗／分頁在跑到一半被關掉時，chromedriver 之後每一次呼叫都丟
# NoSuchWindowException（"target window already closed" / "web view not
# found"）。它是 WebDriverException 的子類，所以會被 hot-path 的
# `except port.TRANSPORT_ERRORS` 當成暫時性卡頓吸收掉——會一路空轉到
# consecutive_fail_abort（約 30 分鐘）才收工。這一組守住「認得出來、立刻收工」。


class _FakeNoSuchWindow(Exception):
    """站在 selenium 立場的替身：類別名 + 訊息都照抄真實回報內容。"""

    def __init__(self):
        super().__init__(
            "Message: no such window: target window already closed\n"
            "from unknown error: web view not found\n"
            "  (Session info: chrome=151.0.7922.170)\n"
            "Stacktrace:\n\tchromedriver!GetHandleVerifier [0x7ff7f691]")


_FakeNoSuchWindow.__name__ = "NoSuchWindowException"


class _FakeReadTimeout(Exception):
    """暫時性卡頓的替身（urllib3 的 ReadTimeoutError）。不得被判成 gone。"""

    def __init__(self):
        super().__init__("HTTPConnectionPool(host=127.0.0.1, port=51234): "
                         "Read timed out. (read timeout=120)")


_FakeReadTimeout.__name__ = "ReadTimeoutError"


class _DeadPort(FakeBrowserPort):
    """每一次 driver 呼叫都丟 NoSuchWindowException（selenium 變體的行為）。"""

    TRANSPORT_ERRORS = (_FakeNoSuchWindow, _FakeReadTimeout)

    def execute_script(self, script, *args):
        raise _FakeNoSuchWindow()

    def execute_async_script(self, script, *args):
        raise _FakeNoSuchWindow()

    def current_url(self):
        raise _FakeNoSuchWindow()

    def get_title(self):
        raise _FakeNoSuchWindow()


class _StalledPort(FakeBrowserPort):
    """每一次 driver 呼叫都逾時（暫時性）。既有的 sentinel 行為必須不變。"""

    TRANSPORT_ERRORS = (_FakeNoSuchWindow, _FakeReadTimeout)

    def execute_script(self, script, *args):
        raise _FakeReadTimeout()

    def execute_async_script(self, script, *args):
        raise _FakeReadTimeout()


class _SilentDeadPort(FakeBrowserPort):
    """je 變體的行為：wrapper 把例外全吞掉、一律回 None（連探針也是）。"""

    def execute_script(self, script, *args):
        return None

    def execute_async_script(self, script, *args):
        return None


def test_is_browser_gone_error_classifies():
    print("test_is_browser_gone_error_classifies")
    assert ws.is_browser_gone_error(_FakeNoSuchWindow())
    assert ws.is_browser_gone_error(RuntimeError("invalid session id"))
    assert ws.is_browser_gone_error(
        RuntimeError("disconnected: not connected to DevTools"))
    # 暫時性的一律不算——誤判會讓一次卡頓變成整輪重生。
    assert not ws.is_browser_gone_error(_FakeReadTimeout())
    assert not ws.is_browser_gone_error(RuntimeError("element not interactable"))
    print("  PASS\n")


def test_short_error_drops_chromedriver_stacktrace():
    print("test_short_error_drops_chromedriver_stacktrace")
    text = ws._short_error(_FakeNoSuchWindow())
    assert "Stacktrace" not in text and "GetHandleVerifier" not in text, text
    assert chr(10) not in text, text
    assert "no such window" in text, text
    print("  PASS\n")


def test_hot_path_readers_escalate_gone_but_absorb_stalls():
    print("test_hot_path_readers_escalate_gone_but_absorb_stalls")
    dead, stalled = _DeadPort(), _StalledPort()
    for name in ("get_main_image_src", "find_generate_button",
                 "get_generation_error"):
        fn = getattr(ws, name)
        try:
            fn(dead)
        except ws.BrowserGoneError:
            pass
        else:
            raise AssertionError(name + " must raise BrowserGoneError")
        assert fn(stalled) is None, name + " must still absorb a stall"
    assert ws.download_image(stalled, "blob:x", Path("nope.png")) is False
    try:
        ws.download_image(dead, "blob:x", Path("nope.png"))
    except ws.BrowserGoneError:
        pass
    else:
        raise AssertionError("download_image must raise BrowserGoneError")
    print("  PASS\n")


def test_a_download_that_brought_back_nothing_usable_writes_no_file(tmp_path):
    """頁面裡的 fetch 失敗時回 null；站方回了不是圖的東西時可能連逗號都沒有。兩種都要回
    False 交給下載重試，**而且不留下檔案**——留下一個空檔或半個檔，存檔計數與續跑都會把它
    當成一張圖。"""
    print("test_a_download_that_brought_back_nothing_usable_writes_no_file")

    class _Answers(FakeBrowserPort):
        def __init__(self, answer):
            super().__init__()
            self.answer = answer

        def execute_async_script(self, script, *args):
            return self.answer

    target = tmp_path / "out" / "001.png"
    for answer in (None, "", "data:image/png;base64", "not a data url"):
        assert ws.download_image(_Answers(answer), "blob:x", target) is False, answer
        assert not target.exists() and not target.parent.exists(), answer
    good = "data:image/png;base64," + _base64.b64encode(b"PNGDATA").decode()
    assert ws.download_image(_Answers(good), "blob:x", target) is True
    assert target.read_bytes() == b"PNGDATA"
    print("  PASS\n")


def test_with_retry_does_not_retry_a_gone_browser():
    print("test_with_retry_does_not_retry_a_gone_browser")
    calls = []

    def _boom():
        calls.append(1)
        raise _FakeNoSuchWindow()

    try:
        ws.with_retry("setup_step", _boom, max_attempts=3, sleep_range=(0, 0))
    except ws.BrowserGoneError:
        pass
    else:
        raise AssertionError("with_retry must not swallow a gone browser")
    assert calls == [1], "gone browser must abort on attempt 1: " + repr(calls)
    # 一般失敗照舊燒完 3 次、回 False。
    calls.clear()

    def _flaky():
        calls.append(1)
        return False

    assert ws.with_retry("x", _flaky, max_attempts=3, sleep_range=(0, 0)) is False
    assert len(calls) == 3, calls
    # 一般例外（不是瀏覽器沒了）是**可重試**的：下一次成功就算成功，不往上拋。
    calls.clear()

    def _recovers():
        calls.append(1)
        if len(calls) == 1:
            raise _FakeReadTimeout()
        return True

    assert ws.with_retry("x", _recovers, max_attempts=3, sleep_range=(0, 0)) is True
    assert len(calls) == 2, calls
    print("  PASS\n")


def test_abort_if_browser_gone_covers_silent_je_wrapper():
    print("test_abort_if_browser_gone_covers_silent_je_wrapper")
    # 活著：探針回 token，不 raise。
    ws._abort_if_browser_gone(FakeBrowserPort(), "probe")
    # je 變體：wrapper 吞例外、回 None → 仍必須被判成 gone。
    for port in (_SilentDeadPort(), _DeadPort()):
        try:
            ws._abort_if_browser_gone(port, "probe")
        except ws.BrowserGoneError:
            continue
        raise AssertionError(type(port).__name__ + " must be detected as gone")
    # 純卡頓不算 gone（呼叫端自己的失敗計數器照舊負責）。
    ws._abort_if_browser_gone(_StalledPort(), "probe")
    print("  PASS\n")


def test_abort_if_chrome_crashed_catches_gone_window():
    print("test_abort_if_chrome_crashed_catches_gone_window")
    # crash interstitial 偵測讀不到 URL／title 時只會回「沒崩」，所以
    # _abort_if_chrome_crashed 必須自己先問「session 還在不在」。
    try:
        ws._abort_if_chrome_crashed(_SilentDeadPort(), "setup")
    except ws.BrowserGoneError:
        pass
    else:
        raise AssertionError("setup guard must abort on a gone session")
    ws._abort_if_chrome_crashed(FakeBrowserPort(), "setup")   # 活著 → 不 raise
    print("  PASS\n")


def test_generate_one_image_aborts_instead_of_burning_retries():
    print("test_generate_one_image_aborts_instead_of_burning_retries")
    saved_pause, saved_time = ws.human_pause, ws.time
    clock = _FakeClock()
    try:
        ws.human_pause = lambda *a, **k: None
        # je 變體：什麼都不丟、什麼都回 None → 探針判定 gone。
        ws.time = clock
        try:
            ws.generate_one_image(_SilentDeadPort(), None, max_retries=4,
                                  retry_delay=(25.0, 30.0))
        except ws.BrowserGoneError:
            pass
        else:
            raise AssertionError(
                "generate_one_image must abort on a gone browser")
        # 重點是「沒有燒掉 25-30 秒的重試等待」。
        assert not [s for s in clock.slept if s >= 5], clock.slept
    finally:
        ws.human_pause, ws.time = saved_pause, saved_time
    print("  PASS\n")


def test_generate_loop_reports_gone_browser_as_critical():
    print("test_generate_loop_reports_gone_browser_as_critical")
    with _GenHarness() as h:
        def _gone(port, prev, **k):
            raise ws.BrowserGoneError("window closed")
        ws.generate_one_image = _gone
        try:
            ws.generate_loop(FakeBrowserPort(), "Zoe",
                             _cfg(images_per_character=5), batch_start=0.0,
                             out_dir=h.out_dir(), minimize_fn=None)
        except ws.BrowserGoneError:
            pass
        else:
            raise AssertionError(
                "generate_loop must let BrowserGoneError through")
        # 沒有 character_done：這一輪不是乾淨收工，監督者必須重生。
        assert h.events_of("character_done") == []
    print("  PASS\n")


# ---------- 額度用完：關掉購買框、等額度回補、接著跑 --------------------------
# 額度用完時站方跳出購買資訊，生成從此不會成功。舊行為：每張燒完 4 次重試、連續
# 10 張才 abort、rc 非 0 → 監督者重生 → 看到同一個對話框 → 無限循環（還每輪重跑
# 一次登入 ＋ setup）。新行為：偵測到 → **關掉對話框** → 等 quota_wait_poll_sec →
# 重試同一張；行程全程不結束，所以監督者根本不會介入。
#
# 這一組守四件事：關閉動作**絕不點到會花錢的按鈕**、關不掉要據實回 False、
# 等到回補要能自己接續、以及不認得的 modal 仍有「停下來等人」那條退路。

# ---------- Tier 1 的字面清單：對得起 log 裡的**實際文案** --------------------
# 2026-09-09 對 `WEBRunner.log` 全檔量出來的事實：08-24 → 09-09、259 次被擋，
# `[blocked] dialog text:` 的相異內容**只有一種**（`_REAL_COPY`），而字面清單開頭
# 那條 `/not enough anlas/i` 描述的舊文案（`_LEGACY_COPY`）在同一份 log 裡出現
# **0 次**。也就是說這份清單長期以來只有一條真的命中過，餘裕是零。
#
# 這一組是**兩個方向**的守門，而反面那半跟正面一樣重要：Tier 1 誤判在批次路徑上
# 不是「乾淨停止」——它走 `wait_for_quota_recovery`，`quota_wait_max_sec` 預設 0
# ＝ 無上限，所以一次誤判就是每小時醒來一次、永不結束的等待迴圈，而且從事件串流
# 上看起來跟真的額度用完一模一樣。

_BLOCK_RE_CACHE = []


def _block_patterns():
    """從 `_GENERATION_BLOCK_JS` **原始碼**抽出 PATTERNS 陣列，轉成 Python `re`。

    跟 `_dismiss_regexes()` 同一個理由：**刻意不在測試裡另抄一份 pattern——抄一份
    就只是在測抄本。** 抽不到就直接 fail，不准跳過：一個選不到任何東西的抽取器
    會讓下面每一支「不誤判」都變成空集合上的斷言，全綠而什麼都沒驗。
    """
    if _BLOCK_RE_CACHE:
        return _BLOCK_RE_CACHE
    import re as _re
    block = _re.search(r"const PATTERNS = \[(.*?)\n\];",
                       ws._GENERATION_BLOCK_JS, _re.S)
    assert block, "could not find the PATTERNS array in _GENERATION_BLOCK_JS"
    for line in block.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("//"):
            continue
        m = _re.fullmatch(r"/(.*)/([a-z]*)", line)
        assert m, f"could not parse this regex literal: {line!r}"
        _BLOCK_RE_CACHE.append(
            (line, _re.compile(m.group(1), _re.I if "i" in m.group(2) else 0)))
    # 正面對照：抽取器活著才有資格談「零誤判」。11 是補這兩條之前的數量。
    assert len(_BLOCK_RE_CACHE) >= 12, (
        f"只抽到 {len(_BLOCK_RE_CACHE)} 條 pattern——抽取器八成過期了")
    return _BLOCK_RE_CACHE


def _block_hits(text):
    return [src for src, rx in _block_patterns() if rx.search(text)]


# 站方實際端出來的文案（log 裡 259 次，撇號是 U+2019）。
_REAL_COPY = ("The paint’s run dry. You need a subscription or to purchase "
              "Anlas to continue. Compare and pick the right plan for you.")
# 舊的猜想文案（log 裡 0 次）。守 `/not enough anlas/i`，**不要因為它沒命中過就
# 刪掉**——它零誤判，而站方換回類似措辭時仍然接得住。
_LEGACY_COPY = ("Not enough Anlas. Purchasing more Anlas lets you keep "
                "generating at this resolution.")

# 反面語料：正常頁面／非付費牆的文字，一條 pattern 都不准命中。最後一筆是 log
# 實測的第二層帳號對話框（不是猜的）。
_NON_BLOCKING_TEXTS = (
    ("導覽列",
     "Home Generate Image Text Adventure Shop Subscription Settings "
     "Account Log Out Anlas 1,234"),
    ("頁尾",
     "About Terms of Service Privacy Policy Refund Policy Contact Support "
     "Careers Subscription FAQ Anlas FAQ Shop © 2026 All rights reserved"),
    ("帳號設定頁",
     "Account Settings Email Change Password Two-Factor Authentication "
     "Subscription Tier Opus Renews on 2026-10-01 Manage Subscription "
     "Payment Method Visa ending 4242 Update Payment Details Billing History"),
    ("產圖介面",
     "Prompt Undesired Content Character 1 Character 2 Model Resolution "
     "Steps Guidance Sampler Seed Generate Anlas 1,234 "
     "Free generations remaining today: 26"),
    ("一般吐司",
     "Image saved to your gallery. Settings updated successfully. "
     "Prompt copied to clipboard."),
    # 方案比較頁：這一筆就是 `/pick the right plan/i` 這個候選被否決掉的理由——
    # 真正的方案頁就寫著那句話，而它的邊際貢獻是零（每一份會命中它的文案，新加的
    # 兩條都已經命中）。零收益 ＋ 有誤判風險 → 不收。
    ("方案比較頁",
     "Explore Our Plans Tablet $10 /mo USD Opus $25 /mo USD Unlimited Images "
     "Image Gen Access Access to our image generation features. Compare plans "
     "and features side by side. Pick the right plan for you. "
     "Anlas Purchase Discount"),
    ("第二層帳號對話框（log 實測）",
     "Unsubscribe Update Payment Details Activate a Gift Key"),
)


def test_the_block_patterns_match_the_copy_the_site_actually_shows():
    print("test_the_block_patterns_match_the_copy_the_site_actually_shows")
    for label, copy in (("實際文案", _REAL_COPY), ("舊文案", _LEGACY_COPY)):
        hits = _block_hits(copy)
        assert hits, (
            f"{label}一條 pattern 都沒接住——站方文案漂移了就照 log 裡的"
            f"`[blocked] dialog text:` 全文回來收斂：{copy!r}")
        print(f"  {label}: {len(hits)} 命中")
    print("  PASS\n")


def test_a_reworded_paywall_still_matches_after_the_purchase_clause_goes():
    """站方把「or to purchase Anlas」換掉，整份清單就從 1/11 掉到 0/11。

    兩個樣本刻意各自**只**命中一條新 pattern（實測驗過），否則兩條會互相遮蔽：
    任一條被刪掉時，另一條仍然接得住，測試照樣綠。
    """
    print("test_a_reworded_paywall_still_matches_after_the_purchase_clause_goes")
    # (a) 語序相反、沒有標題句 → 只有「需要訂閱」那條接得住。
    #     舊的 `/(subscribe|subscription) (is )?(required|needed|to continue)/`
    #     要求 subscription **後面緊接**那幾個詞，所以接不到 requires a …。
    reversed_order = ("Generating requires a subscription. "
                      "Compare and pick the right plan for you.")
    hits = _block_hits(reversed_order)
    assert hits, ("語序相反的「requires a subscription」沒有被接住——"
                  "「需要 ←→ 訂閱」那條 pattern 被刪掉或改窄了")
    assert len(hits) == 1, (
        f"這個樣本應該只命中一條（否則遮蔽掉真正要驗的那條）：{hits}")

    # (b) 只剩標題句、沒有任何訂閱語意 → 只有站方專用那條接得住。
    headline_only = "The paint’s run dry. Head to the shop to keep going."
    hits = _block_hits(headline_only)
    assert hits, "站方標題句那條 pattern 被刪掉了"
    assert len(hits) == 1, (
        f"這個樣本應該只命中一條（否則遮蔽掉真正要驗的那條）：{hits}")

    # 撇號的三種寫法都要吃得下——log 裡是彎的（U+2019），手打很容易寫成直的。
    for apostrophe in ("’", "'", ""):
        text = f"The paint{apostrophe}s run dry. Head to the shop."
        assert _block_hits(text), f"撇號寫成 {apostrophe!r} 就接不住了"
    print("  PASS\n")


def test_no_block_pattern_fires_on_ordinary_page_text():
    print("test_no_block_pattern_fires_on_ordinary_page_text")
    for label, text in _NON_BLOCKING_TEXTS:
        hits = _block_hits(text)
        assert not hits, (
            f"「{label}」被誤判成付費牆，命中 {hits}。Tier 1 誤判 ＝ 每小時醒來"
            f"一次、永不結束的等待迴圈，而且看起來跟真的額度用完一樣。"
            f"加 pattern 要用**片語**、不要用單字。")
    # 正面對照：上面七筆全部不命中，也可能是因為 pattern 全被改壞了（那樣同樣
    # 「零誤判」）。沒有這一句的話，把整個 PATTERNS 陣列清空會讓這支測試變綠。
    assert _block_hits(_REAL_COPY), (
        "偵測本身已經壞了——上面每一個「不誤判」都是空集合上的斷言")
    print(f"  {len(_NON_BLOCKING_TEXTS)} 筆正常頁面文字全部不誤判  PASS\n")


_DISMISS_RE_CACHE = {}


def _dismiss_regexes():
    """從 `_DISMISS_DIALOG_JS` **原始碼**抽出那兩條 regex 字面，轉成 Python 的
    `re` 來測。刻意不在測試裡另抄一份 pattern —— 抄一份就只是在測抄本。"""
    if _DISMISS_RE_CACHE:
        return _DISMISS_RE_CACHE
    import re as _re
    js = ws._DISMISS_DIALOG_JS
    for name in ("FORBIDDEN", "DISMISS"):
        m = _re.search(r"const " + name + r" = /(.*)/i;", js)
        assert m, f"could not find the {name} regex in _DISMISS_DIALOG_JS"
        _DISMISS_RE_CACHE[name] = _re.compile(m.group(1), _re.I)
    return _DISMISS_RE_CACHE


def _js_would_pick(label):
    """複製 `_DISMISS_DIALOG_JS` 第一條規則的判準：完整命中 DISMISS 且不命中
    FORBIDDEN 才**挑中**。

    刻意叫 `pick` 不叫 `click`：那段 JS 已經不按了，它只把元素交回 Python。安全
    性質的判定點跟著搬成「不會**回傳**會花錢的控制項」——名字留在 `click` 會讓下
    一個讀的人以為只要 JS 不呼叫 `.click()` 就安全了。
    """
    res = _dismiss_regexes()
    return bool(res["DISMISS"].fullmatch(label)) and not res["FORBIDDEN"].search(label)


def test_dismiss_never_clicks_a_paying_button():
    print("test_dismiss_never_clicks_a_paying_button")
    # 這是整份改動裡風險最高的一段：購買對話框上「購買」通常比「取消」更顯眼，
    # 寬鬆的選擇器會直接花掉使用者的錢。模稜兩可的字（OK／Yes／Confirm／
    # Continue）在購買框上就是「確認扣款」，所以一律不點。
    must_not_click = [
        "Purchase Anlas", "Purchase", "Buy more Anlas", "Buy", "Subscribe",
        "Upgrade", "Upgrade your plan", "Confirm", "Continue", "OK", "Okay",
        "Yes", "Proceed", "Accept", "Renew", "Top up", "Get more Anlas",
        "Pay now", "Checkout", "Add funds",
    ]
    for label in must_not_click:
        assert not _js_would_pick(label), (
            f"dismiss must NEVER click {label!r} — that spends money")
    may_click = ["Cancel", "Close", "Not now", "Later", "Maybe later",
                 "Dismiss", "No thanks", "Back", "×"]
    for label in may_click:
        assert _js_would_pick(label), f"dismiss should accept {label!r}"
    print("  PASS\n")


class _QuotaPort(FakeBrowserPort):
    """額度用完的假站台。

    被擋的時候 Generate 鈕被 modal 蓋住（找不到），兩層偵測都命中。**每關掉一次
    對話框就少一輪**，`blocked_waits` 輪之後額度回補、一切照常——對應真實行為：
    關掉視窗不會讓額度回來，得等；等到回來為止每次嘗試都會再跳一次。
    """

    dialog = ("Not enough Anlas Purchasing more Anlas lets you keep "
              "generating. Purchase Anlas Cancel")

    def __init__(self, blocked_waits=2):
        super().__init__()
        self.remaining_waits = blocked_waits
        self.dismissals = 0
        self.modal_open = blocked_waits > 0

    @property
    def blocked(self):
        return self.remaining_waits > 0

    def execute_script(self, script, *args):
        if "FORBIDDEN" in script:                       # 挑一顆可以安全按的
            if not self.modal_open:
                return None
            self.modal_open = False
            self.dismissals += 1
            self.remaining_waits = max(0, self.remaining_waits - 1)
            return {"action": "clicked:Cancel", "el": _FakeElement("BUTTON")}
        if "PATTERNS" in script:                        # Tier 1
            return ({"text": self.dialog, "pattern": "/not enough anlas/i"}
                    if self.modal_open else None)
        if "alertdialog" in script:                     # Tier 2
            return self.dialog if self.modal_open else None
        if "Generate" in script and self.blocked:
            self.modal_open = True                      # 又跳出來了
            return None                                 # 鈕被蓋住
        return super().execute_script(script, *args)


class _StuckModalPort(FakeBrowserPort):
    """有 modal 擋著、字面不命中任何 pattern、而且**關不掉**（Tier 2 停止路徑）。"""

    dialog = "\u5e33\u865f\u9700\u8981\u8655\u7406 \u78ba\u5b9a \u53d6\u6d88"

    def execute_script(self, script, *args):
        if "FORBIDDEN" in script:
            return "escape"          # 點不到東西 → 送 Escape…
        if "Escape" in script:
            return True              # …但關不掉
        if "PATTERNS" in script:
            return None              # Tier 1 不命中
        if "alertdialog" in script:
            return self.dialog       # Tier 2 命中，而且一直都在
        return super().execute_script(script, *args)


def test_generation_block_detected():
    print("test_generation_block_detected")
    port = _QuotaPort()
    hit = ws.get_generation_block(port)
    assert hit and "Anlas" in hit["text"], hit
    try:
        ws._abort_if_generation_blocked(port, "probe")
    except ws.GenerationBlockedError as error:
        assert "blocked" in str(error).lower(), error
    else:
        raise AssertionError("a purchase dialog must be detected")
    # 正常頁面（假 port 對這些 script 回 None）不得誤判。
    ws._abort_if_generation_blocked(FakeBrowserPort(), "probe")
    assert ws.has_blocking_dialog(FakeBrowserPort()) is None
    print("  PASS\n")


# ---------- 兩層守門不可以共用同一個前置條件（2026-09-09）------------------
# Tier 1 與 Tier 2 本來**兩邊都有同一行** `if (!text || text.length > 1200)
# continue;`。於是一個 innerText 超過 1200 的 modal 會同時讓 Tier 1 跳過它、
# Tier 2 回 None——**兩層一起瞎掉，而「兩層」正是這個設計的全部價值**。
#
# 而且 Tier 2 的漏判不是漏偵測，是**靜默的錯誤結果**：`dismiss_blocking_dialog`
# 把 None 讀成「關乾淨了」（`stop_reason = "closed"` → `return True`），所以它會
# 對著一個它根本沒碰到的對話框回報成功。
#
# 站方的付費牆正好是最容易超過上限的形狀（整張定價表）。log 裡那 260 筆是被
# `slice(0, 400)` 截斷的，全文長度從來沒有人量過——那也是為什麼 Tier 1 現在會把
# 全文長度印出來。


def _js_text_length_cap(js):
    """從 JS **原始碼**抽出 `text.length > <數字>` 這個「跳過」形狀的上限。

    跟 `_block_patterns()` / `_dismiss_regexes()` 同一個理由：**不在測試裡另抄一
    份，抄一份就只是在測抄本。** 抽不到回 None（＝這段 JS 沒有長度上限）。

    只認**數字字面量**。Tier 2 的截斷門檻走具名常數（`EXCERPT`），所以不會被誤
    認成上限——那是刻意的：留一個「數字字面量 ＝ 上限」的形狀，這支抽取器才問得
    出「有沒有人把 cap 抄回來」。
    """
    import re as _re
    m = _re.search(r"text\.length\s*>\s*(\d+)", js)
    return int(m.group(1)) if m else None


# 站方實際端出來的付費牆：整張定價表。前 400 字照抄 log（那是 `slice(0, 400)` 看
# 得到的全部），後面補上功能比較表的其餘列——真實那一份就是斷在比較表第一列。
_REAL_PAYWALL_HEAD = (
    _REAL_COPY + " SubscribePay As You Go Subscribe Pay As You Go "
    "Explore Our Plans Tablet $10 /mo USD Get Started Opus $25 /mo USD "
    "Get Started Unlimited Images Image Gen Access Access to our image "
    "generation features. Pay As You Go Unlimited* Anlas Purchase Discount "
    "The amount taken off our on-demand Anlas purchases.")
_LONG_PAYWALL = _REAL_PAYWALL_HEAD + " " + (
    "Text Gen Access Access to our text generation features. Included "
    "Included Image Director Tools Included Included Custom AI Modules "
    "Included Included Opus Discount Included Included " * 6)


class _LongModalPort(FakeBrowserPort):
    """付費牆超過 1200 字元的假站台。**上限是從上線的 JS 原始碼抽出來的，不是抄
    的**——Tier 2 的 cap 一旦被抄回去，這個 port 的行為就會自己跟著退回舊的。

    `tier2_cap` 只在「明確重現修正前行為」的那一支測試裡指定。
    """

    dialog = _LONG_PAYWALL

    def __init__(self, tier2_cap=-1):
        super().__init__()
        self.tier2_cap = (_js_text_length_cap(ws._BLOCKING_DIALOG_JS)
                          if tier2_cap == -1 else tier2_cap)

    @staticmethod
    def _excerpt(text):
        return (text[:400] + f" …[truncated; full length {len(text)}]"
                if len(text) > 400 else text)

    def execute_script(self, script, *args):
        # 順序照 `_QuotaPort`：`_GENERATION_BLOCK_JS` 的 SELECTOR 裡也有
        # 'alertdialog'，所以 'PATTERNS' 必須先比。控制項清單用**身分**比對——
        # 它自己的 selector 裡也有 'alertdialog'，用子字串會被下面攔走。
        if script is ws._DIALOG_CONTROLS_DIAG_JS:
            return None
        if "FORBIDDEN" in script:
            return "escape"                  # 沒有安全可按的東西 → 送 Escape
        if "Escape" in script:
            return True                      # …而且關不掉（實測 218/218）
        if "PATTERNS" in script:             # Tier 1
            cap = _js_text_length_cap(ws._GENERATION_BLOCK_JS)
            if cap is not None and len(self.dialog) > cap:
                return None                  # 太長 → Tier 1 當作沒看到
            return {"text": self.dialog[:400],
                    "pattern": "/(purchase|buy) (more )?(anlas|credits)/i",
                    "length": len(self.dialog)}
        if "alertdialog" in script:          # Tier 2
            if self.tier2_cap is not None and len(self.dialog) > self.tier2_cap:
                return None
            return self._excerpt(self.dialog)
        return super().execute_script(script, *args)


def test_only_tier1_gates_the_dialog_text_on_length():
    print("test_only_tier1_gates_the_dialog_text_on_length")
    # 正面對照先跑：抽取器死掉的話，下面那句「Tier 2 沒有上限」會在空集合上成立，
    # 全綠而什麼都沒驗。
    tier1 = _js_text_length_cap(ws._GENERATION_BLOCK_JS)
    assert tier1 == 1200, (
        f"抽取器沒在 _GENERATION_BLOCK_JS 找到那道上限（拿到 {tier1!r}）——"
        "抽取器過期了，下面的斷言全部作廢")
    assert tier1 == ws._TIER1_TEXT_CAP, (
        f"JS 裡的上限是 {tier1}，Python 鏡像 _TIER1_TEXT_CAP 是 "
        f"{ws._TIER1_TEXT_CAP}——log 印出來的「餘裕」會是錯的")
    assert _js_text_length_cap(ws._BLOCKING_DIALOG_JS) is None, (
        "Tier 2 又有長度上限了。它的 selector 只認 role=dialog/alertdialog/"
        "aria-modal，長度不做任何收斂工作；而 Tier 2 回 None 會被 "
        "dismiss_blocking_dialog 讀成「關乾淨了」——這一行等於讓兩層守門共用同一"
        "個盲點，然後在盲點上回報成功")
    print("  PASS\n")


def test_tier2_stays_narrow_enough_to_do_without_a_length_cap():
    print("test_tier2_stays_narrow_enough_to_do_without_a_length_cap")
    # 拿掉 Tier 2 的長度上限，**前提是它的 selector 已經把收斂做完了**。前提沒了
    # 結論就沒了：把 Tier 1 那些寬 selector（吐司、`class*=modal`、`aria-live`）
    # 抄過來，正常頁面上那些隱藏的 modal 容器就會開始被算成「有東西擋著」，而那
    # 正是那道上限當初在 Tier 1 存在的理由。
    tier2, tier1 = ws._BLOCKING_DIALOG_JS, ws._GENERATION_BLOCK_JS
    assert '[role="dialog"],[role="alertdialog"],[aria-modal="true"]' in tier2, (
        "Tier 2 的 selector 變了——長度上限是照著「只認真正的 modal 語意」拿掉的，"
        "selector 一動就要重新論證")
    wide = ('class*="modal"', 'class*="Modal"', 'class*="toast"',
            'class*="Toast"', 'aria-live', 'role="alert"')
    for token in wide:
        assert token not in tier2, (
            f"Tier 2 的 selector 被放寬到 {token!r} 了。那是 Tier 1 的形狀，而 "
            "Tier 1 需要長度上限正是因為它這麼寬——兩件事要嘛一起來、要嘛都不來")
    # 正面對照：那些寬 selector 確實還在 Tier 1，否則上面整圈是空驗。
    missing = [t for t in wide if t not in tier1]
    assert not missing, (
        f"這些 selector 在 Tier 1 也不見了（{missing}）——上面那圈「Tier 2 沒有」"
        "就變成了在空集合上的斷言")
    print("  PASS\n")


def test_both_tiers_report_the_measured_full_length():
    print("test_both_tiers_report_the_measured_full_length")
    # 這兩句釘的是**上線的 JS 本身**，不是假 port。假 port 是自己造回傳值的，
    # 所以下面那幾支行為測試證明得了「Python 這側怎麼用這個欄位」，證明不了
    # 「JS 有沒有把它算出來」——那正是「抄本永遠是綠的」在這裡的樣子。
    assert "length: text.length" in ws._GENERATION_BLOCK_JS, (
        "Tier 1 不再回報全文長度。被擋時看得到的只剩 `slice(0, 400)`，"
        "「離 1200 還有多少餘裕」就又變回用猜的——而跨過去是靜默的")
    assert ("text.slice(0, EXCERPT) + ' …[truncated; full length ' "
            "+ text.length + ']'") in ws._BLOCKING_DIALOG_JS, (
        "Tier 2 退回純截斷了。它的合約是 `str | None`（三個消費者都靠這個形狀），"
        "所以全文長度只能 in-band 帶出來；拿掉它 log 就再也量不到對話框有多長")
    print("  PASS\n")


def test_a_long_paywall_dialog_is_not_reported_as_dismissed():
    print("test_a_long_paywall_dialog_is_not_reported_as_dismissed")
    import contextlib
    import io
    saved = _no_pause()
    try:
        port = _LongModalPort()
        assert len(port.dialog) > ws._TIER1_TEXT_CAP, (
            f"這個 fixture 必須超過 Tier 1 的上限才驗得到東西，"
            f"實際 {len(port.dialog)} 字元")
        # Tier 1 對它是瞎的——那是刻意保留的（它的 selector 很寬，需要這道上限）。
        # 正因為如此，Tier 2 是唯一還看得見這個對話框的東西。
        assert ws.get_generation_block(port) is None, (
            "這支測試的前提是「Tier 1 因為長度跳過了它」；Tier 1 的上限沒了的話，"
            "要改的是這句話而不是把它刪掉")
        seen = ws.has_blocking_dialog(port)
        assert seen is not None, (
            "超過 1200 字元的 modal 必須被 Tier 2 看見")
        assert "full length" in seen and str(len(port.dialog)) in seen, (
            f"截斷時要把全文長度帶出來，實際回傳：{seen!r}")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            closed = ws.dismiss_blocking_dialog(port)
        assert closed is False, (
            "整張定價表關不掉，卻回報「關乾淨了」——呼叫端會跳過整頁 reload，"
            "帶著同一個對話框繼續等額度")
        assert "could not dismiss" in buf.getvalue(), buf.getvalue()
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_the_shared_length_cap_made_the_dismiss_loop_report_a_phantom_success():
    print("test_the_shared_length_cap_made_the_dismiss_loop_report_a_phantom_"
          "success")
    # 反面對照：把修正前的那道 cap 明確接回 Tier 2（只在這個假 port 裡），
    # 重現當時的失效——**不是漏偵測，是回報成功**。這一支不靠 mutation 也看得懂
    # 代價，所以不要因為上面那支綠就把它刪掉。
    saved = _no_pause()
    try:
        port = _LongModalPort(tier2_cap=1200)
        assert ws.has_blocking_dialog(port) is None, (
            "反面對照沒有重現到舊行為——這支測試接下來的斷言就沒有意義了")
        assert ws.dismiss_blocking_dialog(port) is True, (
            "舊行為就是這樣：Tier 2 回 None → 迴圈判定 closed → 回報關乾淨了")
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_tier1_logs_the_measured_full_length_not_just_the_excerpt():
    print("test_tier1_logs_the_measured_full_length_not_just_the_excerpt")
    import contextlib
    import io

    class _Port(_LongModalPort):
        # 剛好塞得進 Tier 1 的上限，好走到 log 那條路。
        dialog = _REAL_PAYWALL_HEAD

    port = _Port()
    assert len(port.dialog) <= ws._TIER1_TEXT_CAP
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        try:
            ws._abort_if_generation_blocked(port, "probe")
        except ws.GenerationBlockedError:
            pass
        else:
            raise AssertionError("Tier 1 應該命中")
    out = buf.getvalue()
    assert f"full length {len(port.dialog)} chars" in out, (
        "被擋時要印出**全文**長度（不是截斷後的 400），否則「離上限還有多少餘裕」"
        "永遠只能用猜的。實際輸出：" + out)
    assert f"margin {ws._TIER1_TEXT_CAP - len(port.dialog)}" in out, out
    print("  PASS\n")


def test_dismiss_reports_failure_when_dialog_survives():
    print("test_dismiss_reports_failure_when_dialog_survives")
    # 以結果為準：「點了某個東西」不等於「關掉了」。
    with _fake_clock():
        assert ws.dismiss_blocking_dialog(_QuotaPort(blocked_waits=1)) is True
        assert ws.dismiss_blocking_dialog(_StuckModalPort()) is False
        assert ws.dismiss_blocking_dialog(FakeBrowserPort()) is True  # 本來就沒有
    print("  PASS\n")


def test_generate_loop_waits_for_quota_then_resumes():
    print("test_generate_loop_waits_for_quota_then_resumes")
    port = _QuotaPort(blocked_waits=2)   # 等兩輪之後額度回補
    saved_wait = ws.wait_if_paused
    # 假時鐘由 `_GenHarness` 一併裝好：`click_generate` / `wait_for_new_image`
    # 都以 `time.time()` 判逾時，用真時鐘會讓這支測試真的空轉十幾秒。
    with _GenHarness() as h:
        try:
            ws.wait_if_paused = lambda label="": None
            saved = ws.generate_loop(
                port, "Nia",
                _cfg(images_per_character=2, quota_wait_poll_sec=600.0,
                     quota_wait_max_sec=0.0),
                batch_start=0.0, out_dir=h.out_dir(), minimize_fn=None)
        finally:
            ws.wait_if_paused = saved_wait
        # 等完之後自己接續，兩張都產出來——沒有 raise、行程沒有結束。
        assert saved == 2, saved
        assert h.events_of("quota_blocked"), "should announce the block once"
        assert h.events_of("quota_resumed"), "should announce the recovery"
        assert len(h.events_of("character_done")) == 1
        # 購買框確實被關掉了（每輪一次），而且真的等了 poll 秒數。
        assert port.dismissals == 2, port.dismissals
        assert sum(h.clock.slept) >= 1200.0, h.clock.slept
    print("  PASS\n")


def test_quota_reload_puts_the_character_fields_back():
    print("test_quota_reload_puts_the_character_fields_back")
    # 關不掉的對話框 → reload。reload 會把頁面打回站方持久化的版本，剛填好、
    # 站方還沒存起來的提示詞會整段消失。正式 log 的 65 次 reload 裡，61 次發生
    # 在「這個角色已經存過圖」之後（＝提示詞早就持久化了）全部順利接回去；唯一
    # 一次發生在角色剛填完欄位、還沒產出任何一張圖的時候，就連燒十張圖、兩個
    # 小時。所以 reload 之後一定要把欄位填回去。
    calls = []
    cfg = {"quota_wait_poll_sec": 1.0, "quota_wait_max_sec": 0.0}
    saved_wait = ws.wait_if_paused
    try:
        ws.wait_if_paused = lambda label="": None
        with _fake_clock():
            ws.wait_for_quota_recovery(_StuckModalPort(), cfg, label="x",
                                       on_reload=lambda: calls.append("refill"))
    finally:
        ws.wait_if_paused = saved_wait
    assert calls == ["refill"], (
        f"reload 過就必須重填欄位，實際呼叫：{calls!r}")
    print("  PASS\n")


def test_quota_wait_does_not_refill_when_the_dialog_just_closed():
    print("test_quota_wait_does_not_refill_when_the_dialog_just_closed")
    # 對照組：對話框關掉了就沒有 reload，頁面狀態沒被動過。重填一次要花十幾秒
    # （提示詞一萬多字），沒必要就別付。
    calls = []
    cfg = {"quota_wait_poll_sec": 1.0, "quota_wait_max_sec": 0.0}
    saved_wait = ws.wait_if_paused
    try:
        ws.wait_if_paused = lambda label="": None
        with _fake_clock():
            ws.wait_for_quota_recovery(_QuotaPort(blocked_waits=1), cfg,
                                       label="x",
                                       on_reload=lambda: calls.append("refill"))
    finally:
        ws.wait_if_paused = saved_wait
    assert calls == [], f"沒 reload 就不該重填，實際呼叫：{calls!r}"
    print("  PASS\n")


def test_quota_recovery_is_only_claimed_when_an_image_really_arrived():
    print("test_quota_recovery_is_only_claimed_when_an_image_really_arrived")
    # 「回復了」要以結果為準。`generate_one_image` 重試燒完是回 None、不是
    # raise，所以額度等待那圈照樣會 break 出來——原本無條件印 recovered 並發
    # `quota_resumed`。正式 log 裡 62 次 recovered 有 1 次是假的，而那一次正是
    # 後面兩小時空轉的開頭：使用者收到「額度回復了」，背景卻正要開始連續放棄
    # 十張圖。
    with _GenHarness() as h:
        seen = {"n": 0}

        def fake_gen(port, prev, **kwargs):
            seen["n"] += 1
            if seen["n"] == 1:
                raise ws.GenerationBlockedError("out of quota")
            return None          # 等完之後重試，還是什麼都沒產出

        ws.generate_one_image = fake_gen
        ws._is_chrome_crash_page = lambda port: False
        saved_wait = ws.wait_if_paused
        try:
            ws.wait_if_paused = lambda label="": None
            ws.generate_loop(_QuotaPort(blocked_waits=0), "Nia",
                             _cfg(images_per_character=1,
                                  quota_wait_poll_sec=1.0),
                             batch_start=0.0, out_dir=h.out_dir(),
                             minimize_fn=None)
        except ws.GenerationBlockedError:
            pass                 # 收尾防線可能再擋一次，不是這支測試的重點
        finally:
            ws.wait_if_paused = saved_wait
        assert h.events_of("quota_blocked"), "被擋住還是要通知一次"
        assert h.events_of("quota_resumed") == [], (
            "重試什麼都沒產出的時候不得宣稱額度回復了——那是假訊息，"
            f"實際發出：{h.events_of('quota_resumed')!r}")
    print("  PASS\n")


def test_quota_wait_keeps_serving_single_image_requests():
    print("test_quota_wait_keeps_serving_single_image_requests")
    # 等額度的那一小時裡，插播的單圖請求照樣要服務得到。bot 那邊的
    # `_SINGLE_IMAGE_PENDING_TTL_SEC` 是 600 秒，而這裡預設睡 3600 秒——沒有這條
    # 的話使用者的即時產圖會在十分鐘後被掃成「webrunner 沒服務就退出了」。而額度
    # 用完在這個帳號上是常態：實測一個週期約 68 分鐘、其中 60 分鐘在等。
    port = _QuotaPort(blocked_waits=1)
    polls = []
    saved_check = ws.check_single_image_request
    saved_wait = ws.wait_if_paused
    with _GenHarness() as h:
        try:
            ws.wait_if_paused = lambda label="": None
            ws.check_single_image_request = (
                lambda p, in_band=True: polls.append(1) or False)
            ws.generate_loop(port, "Nia",
                             _cfg(images_per_character=1,
                                  quota_wait_poll_sec=90.0,
                                  quota_wait_max_sec=0.0),
                             batch_start=0.0, out_dir=h.out_dir(),
                             minimize_fn=None)
        finally:
            ws.check_single_image_request = saved_check
            ws.wait_if_paused = saved_wait
    # images_per_character=1 → 圖與圖之間那個呼叫點根本走不到，所以這些全部來自
    # 額度等待的睡眠切片（90 秒 / 每片 30 秒 = 3 次）。
    assert len(polls) >= 3, (
        f"等額度期間必須持續 poll 插播請求，實際只 poll 了 {len(polls)} 次")
    print("  PASS\n")


def test_quota_wait_slices_call_on_idle_every_slice():
    print("test_quota_wait_slices_call_on_idle_every_slice")
    # 切片睡的用意就是讓等待可以被打斷／有反應；`on_idle` 掛在每一片上。
    calls = []
    cfg = {"quota_wait_poll_sec": 90.0, "quota_wait_max_sec": 0.0}
    saved_wait = ws.wait_if_paused
    try:
        ws.wait_if_paused = lambda label="": None
        with _fake_clock():
            ws.wait_for_quota_recovery(_QuotaPort(blocked_waits=1), cfg,
                                       label="x",
                                       on_idle=lambda: calls.append(1))
    finally:
        ws.wait_if_paused = saved_wait
    assert len(calls) == 3, f"90 秒 / 每片 30 秒應該是 3 次，實際 {len(calls)}"
    print("  PASS\n")


# ---------- 額度等待期間的存活探測 ------------------------------------------
# 等待迴圈整整一輪（預設 3600 秒）不對 driver 下任何指令，所以瀏覽器在等待期間死
# 掉的話，我們要到等完之後的第一個指令才會發現——最久晚一個小時。實測
# （`WEBRunner.log` 2026-09-07 03:02:27 與 06:18:14 兩次）：
#     ConnectionRefusedError: [WinError 10061] 無法連線，因為目標電腦拒絕連線。
# **10061 ＝ 那個埠上沒有東西在聽 ＝ chromedriver.exe 自己已經結束了**，不是逾時
# 也不是 session 卡住。兩次的時間戳都剛好落在 60 分鐘等待結束後的第一個指令上，
# 所以真正的死亡時刻在那一小時裡不可知，跟任何活動對照都是猜的。
#
# 探測的價值是**時間**（把死亡時刻釘在 30 秒內），不是省時間；所以三條性質：
#   1. 探測失敗絕對不能把批次弄死；
#   2. 偵測到死亡只記錄、不當場重啟（重啟＝重新登入＋重填欄位，是最貴那條路）；
#   3. 正常時完全不出聲——120 圈每圈一行等於沒有訊號。

class _DeadAfterPort(_QuotaPort):
    """第 `dies_at` 次存活探測開始回「瀏覽器沒了」。"""

    def __init__(self, *, dies_at=2, probe_raises=False, **kw):
        super().__init__(**kw)
        self.dies_at = dies_at
        self.probe_raises = probe_raises
        self.probes = 0

    def execute_script(self, script, *args):
        if ws._ALIVE_PROBE_TOKEN in script:
            self.probes += 1
            if self.probe_raises:
                raise RuntimeError("probe blew up")
            if self.probes >= self.dies_at:
                # 真實形狀：chromedriver 行程已死 → 連線被拒。
                raise ConnectionRefusedError(
                    "[WinError 10061] 無法連線，因為目標電腦拒絕連線。")
            return ws._ALIVE_PROBE_TOKEN
        return super().execute_script(script, *args)


def _run_quota_wait(port, *, poll=90.0):
    """跑完一輪額度等待，回傳 (stdout, stderr, 事件清單)。"""
    import contextlib
    import io
    events = []
    saved_wait, saved_emit = ws.wait_if_paused, ws.emit_event
    out, err = io.StringIO(), io.StringIO()
    try:
        ws.wait_if_paused = lambda label="": None
        ws.emit_event = lambda kind, **kw: events.append((kind, kw))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with _fake_clock():
                ws.wait_for_quota_recovery(
                    port, {"quota_wait_poll_sec": poll,
                           "quota_wait_max_sec": 0.0}, label="x")
    finally:
        ws.wait_if_paused = saved_wait
        ws.emit_event = saved_emit
    return out.getvalue(), err.getvalue(), events


def test_a_healthy_quota_wait_probes_silently():
    print("test_a_healthy_quota_wait_probes_silently")
    port = _DeadAfterPort(dies_at=10 ** 9, blocked_waits=1)
    out, err, events = _run_quota_wait(port, poll=300.0)
    assert port.probes == 10, (
        f"300 秒 / 每片 30 秒應該探測 10 次，實際 {port.probes} 次——"
        "探測被拿掉了，死亡時刻又會退回「最久晚一小時」")
    assert "died while waiting for quota" not in err and "died while waiting for quota" not in out, (
        "探測正常時一個字都不該印。120 圈每圈一行的警告等於沒有訊號。"
        "實際 stderr：" + err)
    assert not [e for e in events if e[0] not in ("quota_wait",)], (
        f"探測不得產生新事件（會把對話平台洗版）：{events}")
    print("  PASS\n")


def test_a_browser_that_dies_mid_wait_is_logged_exactly_once():
    print("test_a_browser_that_dies_mid_wait_is_logged_exactly_once")
    # 90 秒 = 3 片；第 2 片開始死。轉換只有一次，所以只該記一次。
    port = _DeadAfterPort(dies_at=2, blocked_waits=1)
    out, err, events = _run_quota_wait(port, poll=90.0)
    hits = err.count("died while waiting for quota")
    assert hits == 1, (
        f"「活著 → 死了」這個轉換只該記一次，實際 {hits} 次。"
        "每片都記的話，正式環境一輪會印 120 行——那就是沒有訊號。stderr：" + err)
    assert "10061" in err, (
        "原因字串沒帶進 log。10061 ＝ chromedriver 行程已結束，那一句就是下次"
        "查同一個症狀時最省時間的線索。stderr：" + err)
    assert "not restarting on the spot" in err, (
        "log 沒說清楚「只記錄、不重啟」——看 log 的人會以為系統已經處理過了")
    assert not [e for e in events if e[0] not in ("quota_wait",)], (
        f"偵測到死亡也不發新事件：{events}")
    print("  PASS\n")


def test_a_probe_that_raises_never_kills_the_quota_wait():
    print("test_a_probe_that_raises_never_kills_the_quota_wait")
    # 這一條最重要：探測跑在等待迴圈裡，往上丟例外會取代掉原本乾淨的
    # 「等完再重試」路徑，把一個會自己好的狀況變成需要人介入。這裡讓**探針自己**
    # （不是 port）爆掉，因為 port 丟出來的例外早就被 `_browser_gone_reason` 接住
    # 了——那條路測不到 `_probe_browser_alive` 的那層防護。
    saved = ws._browser_gone_reason
    port = _DeadAfterPort(dies_at=10 ** 9, blocked_waits=1)
    try:
        def _boom(_port):
            raise ValueError("probe helper blew up")
        ws._browser_gone_reason = _boom
        out, err, events = _run_quota_wait(port, poll=90.0)
    finally:
        ws._browser_gone_reason = saved
    assert "sleep 90" not in err          # 只是確認我們真的跑到了迴圈
    assert err.count("died while waiting for quota") == 1, (
        "探針自己壞掉要看得見（一次），但不能吞成沉默、也不能每片印一次。"
        "stderr：" + err)
    assert "probe helper blew up" in err, (
        "探針自己的錯誤訊息要帶進 log，否則沒人知道它壞在哪。stderr：" + err)
    print("  PASS\n")


def test_a_transient_transport_hiccup_during_the_wait_stays_silent():
    print("test_a_transient_transport_hiccup_during_the_wait_stays_silent")
    # 刻意的：`_browser_gone_reason` 把「不是 gone 形狀」的例外一律回 None。等待
    # 迴圈沒有自己的失敗計數器，但也不該為了一次暫時性的抖動印東西——它會在等完
    # 之後的第一個指令上被既有路徑接住。誤報一次「瀏覽器死了」的成本是把真正的
    # 訊號稀釋掉。
    port = _DeadAfterPort(probe_raises=True, blocked_waits=1)
    out, err, events = _run_quota_wait(port, poll=90.0)
    assert port.probes == 3, f"迴圈沒有跑完（只探測了 {port.probes} 片）"
    assert "died while waiting for quota" not in err, (
        "暫時性的 transport 抖動不該被報成「瀏覽器死了」。stderr：" + err)
    print("  PASS\n")


def test_the_probe_helper_never_raises():
    print("test_the_probe_helper_never_raises")

    class _Exploding:
        TRANSPORT_ERRORS = (RuntimeError,)

        def execute_script(self, script, *args):
            raise MemoryError("boom")

    # 不論 port 怎麼壞，這支都要回字串或 None，不能往外丟。
    assert isinstance(ws._probe_browser_alive(_Exploding()), (str, type(None)))
    assert ws._probe_browser_alive(object()) is None, (
        "連 execute_script 都沒有的 port 算「判不出來」，而判不出來要當成活著："
        "誤報一次死亡的成本是把真正的訊號稀釋掉，而漏報的成本只是回到原本的"
        "「等完才發現」")
    saved = ws._browser_gone_reason
    try:
        def _boom(_port):
            raise ValueError("helper blew up")
        ws._browser_gone_reason = _boom
        got = ws._probe_browser_alive(object())
    finally:
        ws._browser_gone_reason = saved
    assert got is not None and "helper blew up" in got, (
        f"探針自己壞掉要吞成一句原因、不是吞成沉默：{got!r}")
    print("  PASS\n")


def test_quota_wait_cap_falls_back_to_stopping():
    print("test_quota_wait_cap_falls_back_to_stopping")
    # `quota_wait_max_sec` > 0 且已經等超過 → 不是會自己回補的額度，回到
    # 「乾淨停止、不重生」那條路。
    try:
        ws.wait_for_quota_recovery(
            _QuotaPort(), {"quota_wait_poll_sec": 600.0,
                           "quota_wait_max_sec": 300.0},
            label="x", waited_sec=600.0)
    except ws.GenerationBlockedError as error:
        assert "quota_wait_max_sec" in str(error), error
    else:
        raise AssertionError("cap must fall back to stopping")
    print("  PASS\n")


# ---------- 額度等待：輪詢間隔固定，但要把它留在事件串流裡 --------------------
# 2026-09-07 一度想把等待改成退避（由短而長），理由是「固定間隔讓真正的回補週期
# 結構上不可觀測」。**那個前提被資料推翻了**：`events.ndjson` 裡 08-24～09-07 的
# 214 個 `quota_blocked` 剛好橫跨兩個設定值（08-25 07:10 之前約 14 分鐘、之後約
# 60 分鐘），以「同一角色相鄰兩次被擋之間」為窗口量下來——短輪詢 7.87 張/小時
# （n=43）、長輪詢 7.84 張/小時（n=151，只取長度 ≤ 2 小時者；原始 153 個裡有一個
# 是 36.5 小時的停機空窗，不濾掉會把總和法拉到 6.50），194 個窗口差 0.4%。
# 上限在帳號那一側，
# 輪詢間隔動不了它。退避的收益是零，成本卻是把每天約 24 次「關對話框 → 關不掉 →
# 整頁 reload → 重填欄位」變成約 200 次，而那條路正是本專案最嚴重那次故障的觸發
# 路徑。所以**間隔維持固定**，只留下當初為了收資料而加的那個欄位。


def test_quota_resumed_records_the_poll_interval_that_was_in_force():
    print("test_quota_resumed_records_the_poll_interval_that_was_in_force")
    # `waited_sec` 是累計，單獨看不出當時的輪詢間隔是多少。上面那次複驗要判斷
    # 「這段記錄是哪個設定跑出來的」，只能比對相鄰事件的時間戳去反推設定變更點；
    # 有 `last_wait_sec` 就是直接讀得到。收集成本是零。
    #
    # 值必須是**量出來的**（`wait_for_quota_recovery` 回傳值與傳入值的差），不是
    # 在呼叫端自己再讀一次 `quota_wait_poll_sec`——一份設定讀兩處遲早分歧，而分歧
    # 的症狀是事件裡記著一個從來沒真的等過的數字。
    port = _QuotaPort(blocked_waits=2)
    saved_wait = ws.wait_if_paused
    with _GenHarness() as h:
        try:
            ws.wait_if_paused = lambda label="": None
            ws.generate_loop(
                port, "Nia",
                _cfg(images_per_character=1, quota_wait_poll_sec=600.0,
                     quota_wait_max_sec=0.0),
                batch_start=0.0, out_dir=h.out_dir(), minimize_fn=None)
        finally:
            ws.wait_if_paused = saved_wait
        resumed = h.events_of("quota_resumed")
        slept = list(h.clock.slept)
    assert len(resumed) == 1, resumed
    event = resumed[0]
    assert "last_wait_sec" in event, (
        "只送累計值的話，事後無從得知這段記錄是哪個輪詢間隔跑出來的："
        f"{sorted(event)}")
    # 等兩輪 × 600 秒 → 累計 1200、最後一輪 600。
    assert event["waited_sec"] == 1200.0, event
    assert event["last_wait_sec"] == 600.0, event
    # 而且真的睡了那麼久（不是只把設定值抄進事件裡）。
    assert sum(slept) >= 1200.0, slept
    # 換一個設定值，欄位要跟著動——寫死成預設 3600 也會過的話等於沒驗。
    port2 = _QuotaPort(blocked_waits=1)
    with _GenHarness() as h2:
        try:
            ws.wait_if_paused = lambda label="": None
            ws.generate_loop(
                port2, "Nia",
                _cfg(images_per_character=1, quota_wait_poll_sec=90.0,
                     quota_wait_max_sec=0.0),
                batch_start=0.0, out_dir=h2.out_dir(), minimize_fn=None)
        finally:
            ws.wait_if_paused = saved_wait
        second = h2.events_of("quota_resumed")
    assert second and second[0]["last_wait_sec"] == 90.0, second
    print("  PASS\n")


def test_quota_wait_reports_about_once_an_hour_whatever_the_poll_is():
    print("test_quota_wait_reports_about_once_an_hour_whatever_the_poll_is")
    # 等待中的回報要**大約每小時一則**，免得長時間等待把頻道洗版。輪數必須跟著
    # `poll` 換算（`every = round(3600 / poll)`）、不能寫死——原本就是寫死每 6 輪
    # 配 10 分鐘輪詢，預設一改成 1 小時就變成六小時才回報一次。這段行為以前完全
    # 沒有測試守著，而 2026-09-07 的文件更正正好開始依賴它（「輪詢間隔固定」是
    # 這個換算成立的前提），所以補上。
    #
    # 第 0 輪不得發：呼叫端剛在被擋的當下發過 `quota_blocked`，同一時刻再發一則
    # 等於連貼兩句一樣的話。
    def rounds_that_report(poll, n):
        cfg = {"quota_wait_poll_sec": poll, "quota_wait_max_sec": 0.0}
        events, waited = [], 0.0
        saved_wait, saved_emit = ws.wait_if_paused, ws.emit_event
        try:
            ws.wait_if_paused = lambda label="": None
            ws.emit_event = lambda et, **kw: events.append((et, kw))
            with _fake_clock():
                for _ in range(n):
                    waited = ws.wait_for_quota_recovery(
                        _QuotaPort(blocked_waits=999), cfg, label="x",
                        waited_sec=waited)
        finally:
            ws.wait_if_paused, ws.emit_event = saved_wait, saved_emit
        return [kw["waited_sec"] for et, kw in events if et == "quota_wait"]

    # 1 小時輪詢：每一輪都是一小時，所以每輪都報（第 0 輪除外）。
    assert rounds_that_report(3600.0, 4) == [3600, 7200, 10800], \
        rounds_that_report(3600.0, 4)
    # 10 分鐘輪詢：每 6 輪一則。寫死 `every = 6` 也會過這一項——所以下面那兩項
    # 才是真正的判準。
    assert rounds_that_report(600.0, 13) == [3600, 7200], \
        rounds_that_report(600.0, 13)
    # 30 分鐘輪詢：每 2 輪一則。寫死 6 的話要到第 6 輪（3 小時）才報。
    assert rounds_that_report(1800.0, 5) == [3600, 7200], \
        rounds_that_report(1800.0, 5)
    # 比一小時還長的輪詢：不可能更密，每輪一則。
    assert rounds_that_report(7200.0, 3) == [7200, 14400], \
        rounds_that_report(7200.0, 3)
    print("  PASS\n")


def test_generate_loop_stops_on_a_modal_it_cannot_close():
    print("test_generate_loop_stops_on_a_modal_it_cannot_close")
    # Tier 2（不看字面）：關不掉的 modal → 停下來等人，而不是無限重生。
    with _GenHarness() as h:
        ws.generate_one_image = lambda port, prev, **k: None
        ws._is_chrome_crash_page = lambda port: False
        try:
            ws.generate_loop(_StuckModalPort(), "Ann",
                             _cfg(images_per_character=9,
                                  consecutive_fail_abort=3),
                             batch_start=0.0, out_dir=h.out_dir(),
                             minimize_fn=None)
        except ws.GenerationBlockedError as error:
            assert "modal" in str(error).lower(), error
        else:
            raise AssertionError("an unclosable modal must stop the run")
        # 沒有 modal 的一般連續失敗照舊是 RuntimeError（監督者重生一次）。
        try:
            ws.generate_loop(FakeBrowserPort(), "Ann",
                             _cfg(images_per_character=9,
                                  consecutive_fail_abort=3),
                             batch_start=0.0, out_dir=h.out_dir(),
                             minimize_fn=None)
        except ws.GenerationBlockedError:
            raise AssertionError("no modal → must stay a plain RuntimeError")
        except RuntimeError as error:
            assert "consecutive" in str(error), error
        else:
            # 沒有這一句的話，`generate_loop` 哪天改成「安靜回 0」就沒有任何東西
            # 會紅：兩個 except 都不會進，斷言從此是死的，而這一段的整個重點正是
            # 「它一定要丟」。
            raise AssertionError(
                "連續失敗必須丟 RuntimeError，不能安靜收工")
    print("  PASS\n")


def test_short_character_still_notices_a_blocking_modal():
    print("test_short_character_still_notices_a_blocking_modal")
    # Tier 2 的主要檢查點掛在 `consecutive_fail_abort` 門檻上，所以
    # `images_per_character` 比門檻小的角色根本走不到它：舊行為是安靜地回
    # saved=0，讓 run_batch 的零產出 backstop 慢慢兜，診斷也退化成「什麼都沒
    # 產出」。收尾防線補的就是這個缺口。
    with _GenHarness() as h:
        ws.generate_one_image = lambda port, prev, **k: None
        ws._is_chrome_crash_page = lambda port: False
        try:
            ws.generate_loop(_StuckModalPort(), "Bo",
                             _cfg(images_per_character=2,      # < abort 門檻
                                  consecutive_fail_abort=10),
                             batch_start=0.0, out_dir=h.out_dir(),
                             minimize_fn=None)
        except ws.GenerationBlockedError as error:
            assert "no images" in str(error), error
        else:
            raise AssertionError(
                "a short character must still notice the blocking modal")
        # 沒有 modal 的零產出照舊安靜回 0（那是 run_batch 零產出 backstop 的事）。
        assert ws.generate_loop(FakeBrowserPort(), "Bo",
                                _cfg(images_per_character=2,
                                     consecutive_fail_abort=10),
                                batch_start=0.0, out_dir=h.out_dir(),
                                minimize_fn=None) == 0
    print("  PASS\n")


def test_rc_contract_values_are_distinct():
    print("test_rc_contract_values_are_distinct")
    # 監督者靠這兩個值分辨「重生一次」與「完全不要重生」。撞在一起就失效。
    assert ws.RC_ZERO_PROGRESS == 3
    assert ws.RC_GENERATION_BLOCKED == 4
    assert ws.RC_ZERO_PROGRESS != ws.RC_GENERATION_BLOCKED
    import _supervisor as sup
    assert sup.webrunner_exit_needs_human(ws.RC_GENERATION_BLOCKED)
    assert not sup.webrunner_exit_needs_human(ws.RC_ZERO_PROGRESS)
    assert not sup.webrunner_exit_needs_human(0)
    assert not sup.webrunner_exit_needs_human(1)
    print("  PASS\n")


# ---------- 覆蓋率守門：新的產圖路徑不得漏接 ---------------------------------
# 上面那些測試證明「`generate_loop` 這條路」會等額度、「監督者」不會亂重生。
# 但真正會出事的是**下一個人另外開一條產圖路徑**——例如再寫一個直接呼叫
# `generate_one_image` 的驗證／工具函式——然後忘了處理被擋的情況。那種漏接不會
# 讓任何既有測試變紅，只會在額度用完的那天變成一個看不懂的失敗。
# 這兩支用 AST 靜態掃描把「有沒有接上」變成會變紅的規則。

# 範圍是**算出來的**：上面那段註解說的危險是「下一個人另外開一條產圖路徑——例如再
# 寫一個直接呼叫 `generate_one_image` 的驗證／工具函式」，而那句話沒有提到任何模組。
# 寫死三個檔名的話，那條新路徑正好落在掃描範圍外，也就是這道守門宣稱要防的東西。
# `verify_quota_dialog.py` 已經是那種工具了（它把五支共用 hot-path helper 丟進真
# DOM 跑），只是今天還沒呼叫到 `generate_one_image`。
#
# 實測 2026-09-10：加寬之後全專案仍然只有 3 個呼叫點
# （`_webrunner_shared.py:5056`／`:5391`、`webrunner_novelai.py:1465`；
# `webrunner_je_only.py` 一個都沒有，所以舊清單的第三筆本來就是零產出），
# 所以這是**趁乾淨把範圍鎖起來**、不是在修東西——正因為乾淨，範圍才需要自己的釘子
# 。
_GEN_CALL_SITE_FLOOR = 3
_GEN_SOURCE_FLOOR = 25


def _prod_sources(pkg_root=None, repo_root=None) -> tuple:
    """專案的非測試模組（含 repo root）。

    兩個目錄是參數，好讓範圍釘樁餵得進 `tmp_path`——今天的答案剛好對，不代表列舉
    是算出來的。
    """
    package = (Path(ws.__file__).resolve().parent if pkg_root is None
               else pkg_root)
    root = package.parent if repo_root is None else repo_root
    out = []
    for path in sorted(package.glob("*.py")) + sorted(root.glob("*.py")):
        if path.name.startswith(("test_", "_test_")):
            continue
        if path.name in ("conftest.py", "__init__.py"):
            continue
        out.append(path)
    return tuple(out)

# 允許不處理的呼叫點：`(檔名, 函式名): 理由`。**每一筆都要寫理由**；清單變長就是
# 規則在鬆動的訊號，不是把新東西塞進去的地方。
_BLOCK_HANDLING_EXEMPT: dict[tuple, str] = {}


def _enclosing_functions(tree):
    """回 {node: 最近的外層 def 名稱}，用來報告漏接的位置。"""
    owner = {}

    def walk(node, fname):
        for child in ast.iter_child_nodes(node):
            nxt = (child.name
                   if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                   else fname)
            owner[child] = nxt
            walk(child, nxt)
    walk(tree, "<module>")
    return owner


def _handles(node, tree, exc_name):
    """`node` 是否被某個「body 含有它、且 handler 接得住 `exc_name`」的 try 包住。"""
    for tnode in ast.walk(tree):
        if not isinstance(tnode, ast.Try):
            continue
        in_body = any(node is d for stmt in tnode.body for d in ast.walk(stmt))
        if not in_body:
            continue
        for handler in tnode.handlers:
            names = [h.attr if isinstance(h, ast.Attribute) else
                     getattr(h, "id", "")
                     for h in ast.walk(handler.type or ast.Pass())
                     if isinstance(h, (ast.Name, ast.Attribute))]
            if exc_name in names:
                return True
    return False


def test_every_generation_call_site_handles_a_quota_block():
    print("test_every_generation_call_site_handles_a_quota_block")
    missed = []
    sources = _prod_sources()
    assert len(sources) >= _GEN_SOURCE_FLOOR, (
        f"只抽到 {len(sources)} 個模組（下限 {_GEN_SOURCE_FLOOR}）——抽取器壞了，"
        "或範圍被縮回一份寫死的清單。掃到 0 個時這支永遠會綠。")
    seen_calls = 0
    for path in sources:
        name = path.name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            called = (fn.attr if isinstance(fn, ast.Attribute)
                      else getattr(fn, "id", ""))
            if called != "generate_one_image":
                continue
            seen_calls += 1
            where = (name, owner.get(node, "?"))
            if where in _BLOCK_HANDLING_EXEMPT:
                continue
            if not _handles(node, tree, "GenerationBlockedError"):
                missed.append(f"{where[0]}::{where[1]}")
    # 下限二：**只違反這一道**的語料是「模組數夠、但一個呼叫點都沒抽到」——例如
    # 有人把 `generate_one_image` 改名。分開一句才擋得住「把後面那道放寬成 0」。
    assert seen_calls >= _GEN_CALL_SITE_FLOOR, (
        f"全專案只找到 {seen_calls} 個 `generate_one_image` 呼叫點（下限 "
        f"{_GEN_CALL_SITE_FLOOR}）——是不是改名了？改名之後這支會安靜地永遠綠。")
    assert not missed, (
        "這些產圖呼叫點沒有處理 GenerationBlockedError —— 額度用完時它們會炸成"
        f"看不懂的失敗，而不是關掉購買框等回補：{missed}")
    print("  PASS\n")


def test_both_supervisors_honour_the_webrunner_rc_contract():
    print("test_both_supervisors_honour_the_webrunner_rc_contract")
    # 兩支監督者是各自獨立的迴圈（一支是 launcher、一支在 bot 的事件迴圈裡），
    # 很容易只改到一邊。rc=4「不要重生」與 rc=3「連續零產出就放棄」缺任何一邊，
    # 被擋住時那條路就會回到無限重生。
    repo = Path(ws.__file__).resolve().parent.parent
    for rel in ("start_webrunner.py", "axiomatic/discord_bot.py"):
        src = (repo / rel).read_text(encoding="utf-8")
        assert "webrunner_exit_needs_human" in src, (
            f"{rel} 沒有判斷 rc=4（被擋住）——它會照常重生，"
            "而新的 Chrome 只會看到同一個對話框")
        assert "RC_ZERO_PROGRESS" in src, (
            f"{rel} 沒有數連續零產出的輪數——慢速失敗會繞過 rapid-fail 那道閘")
    print("  PASS\n")


# ---------------------------------------------------------------------------
# 「站方換回舊圖」——2026-08-27 從正式 log 抓出來的無聲錯圖
#
# `get_main_image_src` 挑的是「面積最大的可見 blob:/data: 圖」，面積算的是
# `naturalWidth`——而縮圖的 naturalWidth 就是原圖的寬。歷史區的縮圖因此跟主圖
# 同分，DOM 順序一變，挑中的就換成另一張**舊**圖。舊圖的 blob: URL 在同一份
# 文件裡是活的、而且跟當初存下來時一模一樣。
#
# 舊的判準只有「跟 previous_src 不一樣」，所以那張舊圖會被當成「新圖」立刻
# 接受、再下載存一次。正式資料裡的證據：402 次生成有 92 次在 1 秒內就「完成」
# （生成本身要 4-7 秒，光穩定性確認就 0.6 秒），而第 57 張與第 43 張的 png
# byte 完全相同。
# ---------------------------------------------------------------------------


def _wait_with(srcs, *, previous, seen=(), block=None, probes=None,
               clock=None):
    """跑 `wait_for_new_image`，`get_main_image_src` 依序吐出 `srcs`。

    `block` 給值就讓 `get_generation_block` 一直回那個 dict（＝畫面上有購買
    對話框）；`probes` 傳一個 list 進來就會記下每次探測當下的虛擬時間。
    """
    saved = (ws.get_main_image_src, ws.get_generation_error,
             ws.get_generation_block)
    queue = list(srcs)
    clock = clock if clock is not None else _FakeClock()

    def _probe(_port):
        if probes is not None:
            probes.append(clock.now)
        return block

    try:
        ws.get_main_image_src = lambda _port: (
            queue.pop(0) if queue else (srcs[-1] if srcs else None))
        ws.get_generation_error = lambda _port: None
        ws.get_generation_block = _probe
        with _fake_clock(clock):
            return ws.wait_for_new_image(
                FakeBrowserPort(), previous, timeout=60,
                baseline_error=None, seen_srcs=seen)
    finally:
        (ws.get_main_image_src, ws.get_generation_error,
         ws.get_generation_block) = saved


def test_wait_for_new_image_refuses_an_image_we_already_saved():
    print("test_wait_for_new_image_refuses_an_image_we_already_saved")
    # 畫面換成第 43 張（存過了），接著才出現真正的新圖。
    got = _wait_with(["blob:43", "blob:43", "blob:99", "blob:99"],
                     previous="blob:56", seen=("blob:43", "blob:56"))
    assert got == "blob:99", (
        f"存過的 src 不該被當成新圖，這一輪應該等到 blob:99，卻回了 {got!r}")
    print("  PASS\n")


def test_wait_for_new_image_still_takes_a_genuinely_new_image():
    print("test_wait_for_new_image_still_takes_a_genuinely_new_image")
    # 對照組：seen_srcs 不能把正常那條路一起擋掉。
    got = _wait_with(["blob:99", "blob:99"], previous="blob:56",
                     seen=("blob:43", "blob:56"))
    assert got == "blob:99", f"沒存過的新圖必須照收，卻回了 {got!r}"
    print("  PASS\n")


def test_wait_for_new_image_says_so_when_it_refuses_one():
    print("test_wait_for_new_image_says_so_when_it_refuses_one")
    # 這個 bug 在 log 裡原本只表現成「這張圖產得特別快」，沒有任何一行說出
    # 發生了什麼——所以它安靜地跑了好幾天。拒絕的當下必須留話。
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _wait_with(["blob:43", "blob:43", "blob:99", "blob:99"],
                   previous="blob:56", seen=("blob:43", "blob:56"))
    printed = buffer.getvalue()
    assert "already" in printed, (
        f"拒絕舊圖時必須在 log 留下觀測點，實際輸出：{printed!r}")
    assert printed.count("already") == 1, (
        f"同一條 src 只該抱怨一次，別把 log 洗版：{printed!r}")
    print("  PASS\n")


# ---------- 額度用完的時候不要空等滿整個 timeout ---------------------------
# 購買／方案對話框一跳出來，圖就再也不會來了。這一題原本只在 `wait_for_new_
# image` 整個 timeout（正式路徑 180 秒）燒完之後、由 `generate_one_image` 問，
# 所以每一次額度用完都固定空轉三分鐘。正式 log 的實測：2026-08-24～08-27 共
# 65 次被擋，每一次都是 180~182 秒，合計 11749 秒＝3 小時 16 分；使用者的通知
# 也跟著慢三分鐘才發得出去。
#
# 這一組守三件事：被擋住要**早早**停下來、寬限期內不要多花 round-trip、以及
# 「有圖為大」的優先順序不因為這個探測而改變。
# ---------------------------------------------------------------------------

_FAKE_BLOCK = {"text": "Purchase Anlas to continue", "pattern": "/(purchase)/i"}


def test_wait_for_new_image_stops_waiting_when_the_purchase_dialog_appears():
    print("test_wait_for_new_image_stops_waiting_when_the_purchase_dialog_appears")
    probes = []
    clock = _FakeClock()
    start = clock.now
    # 畫面一直是同一張舊圖（＝這次生成根本沒發生），而對話框擋在上面。
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            _wait_with(["blob:56"], previous="blob:56", block=_FAKE_BLOCK,
                       probes=probes, clock=clock)
        except ws.GenerationBlockedError:
            pass
        else:
            raise AssertionError(
                "購買對話框擋著的時候必須 raise GenerationBlockedError，"
                "而不是空等到 timeout 再回 None")
    waited = clock.now - start
    assert waited < 20, (
        f"被擋住之後應該幾秒內就收手，實際等了 {waited:.1f} 秒——"
        f"正式路徑的 timeout 是 180 秒，這正是要修掉的 3 分鐘空轉")
    assert waited >= ws.BLOCK_PROBE_GRACE_SEC, (
        f"寬限期是刻意的（見 BLOCK_PROBE_GRACE_SEC），不該在 "
        f"{waited:.1f} 秒就探測")
    assert probes and probes[0] - start >= ws.BLOCK_PROBE_GRACE_SEC, (
        f"第一次探測必須落在寬限期之後，實際在 {probes!r}")
    print("  PASS\n")


def test_wait_for_new_image_does_not_probe_while_a_normal_image_is_cooking():
    print("test_wait_for_new_image_does_not_probe_while_a_normal_image_is_cooking")
    # 正式 log 的 410 次成功生成 p50=5 秒，遠在寬限期之內。探測是為了病態情況
    # 加的，不該讓正常那條路每張圖多打好幾次 JS——`_GENERATION_BLOCK_JS` 會掃
    # 十幾種選擇器再讀 innerText（強迫 layout），不是免費的。
    probes = []
    got = _wait_with(["blob:56"] * 4 + ["blob:99", "blob:99"],
                     previous="blob:56", probes=probes)
    assert got == "blob:99", f"正常的新圖必須照收，卻回了 {got!r}"
    assert probes == [], (
        f"寬限期內不該探測，實際探測了 {len(probes)} 次")
    print("  PASS\n")


def test_wait_for_new_image_takes_the_image_even_when_the_dialog_is_up():
    print("test_wait_for_new_image_takes_the_image_even_when_the_dialog_is_up")
    # 優先順序：迴圈裡圖片檢查排在探測之前，所以就算對話框已經在畫面上、而站方
    # 同時算完了一張真的圖，也是圖先被收下。這條是探測唯一的誤殺風險，把它釘住。
    # 圖故意拖到寬限期**之後**才出現（每圈 0.5 秒，寬限期 10 秒＝約 20 圈）。
    # 每圈 0.5 秒、寬限期 10 秒 → 第 21 圈（t=10.0）正好是第一個探測點；
    # 讓新圖剛好在那一圈出現，測的就是同一圈裡誰先被檢查到。
    slow = ["blob:56"] * 20 + ["blob:99", "blob:99"]
    with contextlib.redirect_stderr(io.StringIO()):
        got = _wait_with(slow, previous="blob:56", block=_FAKE_BLOCK)
    assert got == "blob:99", (
        f"畫面上真的出現新圖的時候必須收下它，不能因為對話框在就丟掉，"
        f"卻回了 {got!r}")
    print("  PASS\n")


def test_wait_for_new_image_keeps_probing_when_the_wall_clock_jumps_back():
    """牆鐘往回撥不可以讓等圖迴圈變成「多等一小時、而且期間不再探測」。

    這是本模組**唯一**改成單調截止時刻的逾時迴圈。差別在後果不同級：`next_probe` 也是
    截止時刻，牆鐘往回跳 Δ 會讓額度對話框的探測**停擺 Δ**，而 `end` 同時也不會
    到——被擋住卻沒人在看，正好把 `BLOCK_PROBE_*` 當初要解決的病態搬回來。

    這裡不放對話框（放了會 raise，就測不到「時間到了才收工」）；測的是時鐘本身。
    """
    print("test_wait_for_new_image_keeps_probing_when_the_wall_clock_jumps_back")
    clock = _FakeClock()
    start = clock.now
    jumped = []

    def _step(clk, _sec):
        # 撐過寬限期、確定探測已經開始之後，把**牆鐘**往回撥一小時
        # （單調時鐘不受影響——真實世界就是這樣）。
        if not jumped and clk.now - start >= 20.0:
            jumped.append(clk.now)
            clk.wall_skew -= 3600.0
    clock.on_sleep = _step

    probes = []
    with contextlib.redirect_stdout(io.StringIO()):
        got = _wait_with(["blob:56"] * 400, previous="blob:56",
                         probes=probes, clock=clock)
    elapsed = clock.now - start

    assert got is None, f"沒有新圖就該逾時回 None，卻回了 {got!r}"
    assert jumped, "測試沒設好：牆鐘根本沒跳"
    # `_wait_with` 用 timeout=60。牆鐘往回撥一小時的話這裡會變成約 3660。
    assert elapsed <= 62.0, (
        f"牆鐘往回撥讓等圖多空轉了 {elapsed - 60:.0f} 秒（總共 {elapsed:.0f}s，"
        f"timeout 只有 60s）——`end` 還在用 time.time()")
    # 探測必須在跳之後繼續。寬限期 10 秒、間隔 2 秒、迴圈 0.5 秒 → 約 25 次。
    after = [p for p in probes if p > jumped[0]]
    assert len(after) >= 15, (
        f"牆鐘往回撥之後探測停擺了（跳之前 "
        f"{len(probes) - len(after)} 次、之後只有 {len(after)} 次）——"
        "`next_probe` 還在用 time.time()，這正是額度被擋卻沒人在看的那個病態")
    print("  PASS\n")


def test_wait_for_new_image_uses_monotonic_deadlines():
    """形狀守門：`wait_for_new_image` 的兩個截止時刻不可以退回牆鐘。

    上一支是行為測試，但它只在時鐘真的跳的時候才分得出差別；有人把截止時刻改回
    牆鐘、同時「順手」也把測試裡的 `wall_skew` 拿掉，就沒有東西會紅（同
    `test_rest_until_uses_a_monotonic_deadline` 的理由）。

    這裡**只**釘這一個函式。同檔另外六個逾時迴圈刻意留在牆鐘，所以不能寫成
    「全模組不准有 time.time()」那種掃法。
    """
    pkg = Path(__file__).resolve().parent.parent / "axiomatic"
    tree = ast.parse((pkg / "_webrunner_shared.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "wait_for_new_image")
    attrs = [n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "time"]
    assert "time" not in attrs, (
        f"`wait_for_new_image` 裡不該再有 time.time()：{attrs}。`end` 與 "
        "`next_probe` 都必須是單調截止時刻——牆鐘往回跳 Δ 會讓額度對話框的探測"
        "停擺 Δ，而 `end` 同時也不會到。")
    # end、next_probe、while、迴圈裡的 now，共四處。
    assert attrs.count("monotonic") >= 4, (
        f"少了一處單調時鐘（應有 end / next_probe / while / now 四處）：{attrs}")
    print("  PASS test_wait_for_new_image_uses_monotonic_deadlines")


def test_generate_one_image_baselines_on_what_is_on_screen_now():
    print("test_generate_one_image_baselines_on_what_is_on_screen_now")
    # 呼叫端的 previous_src 是「上一張產完時」的值，中間隔著 20-30 秒的圖間
    # 等待；額度回補那條路還隔著一次 port.refresh()，而 blob: URL 綁定文件，
    # reload 之後舊的那條就作廢。拿它當基準等於「畫面上任何東西都算新圖」。
    saved = (ws.click_generate, ws.wait_for_new_image,
             ws.get_generation_error, ws.get_main_image_src)
    seen_baselines = []
    try:
        ws.click_generate = lambda _port: True
        ws.get_generation_error = lambda _port: None
        ws.get_main_image_src = lambda _port: "blob:on-screen-now"
        ws.wait_for_new_image = (
            lambda _port, previous, **_k:
            seen_baselines.append(previous) or "blob:new")
        with _fake_clock():
            got = ws.generate_one_image(
                FakeBrowserPort(), "blob:stale-from-before-the-reload",
                max_retries=2, retry_delay=(0, 0))
    finally:
        (ws.click_generate, ws.wait_for_new_image,
         ws.get_generation_error, ws.get_main_image_src) = saved
    assert got == "blob:new", got
    assert seen_baselines == ["blob:on-screen-now"], (
        f"基準必須是按下去那一刻螢幕上的圖，實際傳的是 {seen_baselines}")
    print("  PASS\n")


def test_generate_one_image_keeps_the_callers_value_when_the_screen_is_unreadable():
    print("test_generate_one_image_keeps_the_callers_value_when_the_screen_is_unreadable")
    # 傳輸卡頓時 `get_main_image_src` 回 None。用 None 當基準比用過期的值更
    # 危險——那會讓畫面上**任何**東西都通過「跟基準不一樣」這一關。
    saved = (ws.click_generate, ws.wait_for_new_image,
             ws.get_generation_error, ws.get_main_image_src)
    seen_baselines = []
    try:
        ws.click_generate = lambda _port: True
        ws.get_generation_error = lambda _port: None
        ws.get_main_image_src = lambda _port: None
        ws.wait_for_new_image = (
            lambda _port, previous, **_k:
            seen_baselines.append(previous) or "blob:new")
        with _fake_clock():
            ws.generate_one_image(FakeBrowserPort(), "blob:prev",
                                  max_retries=2, retry_delay=(0, 0))
    finally:
        (ws.click_generate, ws.wait_for_new_image,
         ws.get_generation_error, ws.get_main_image_src) = saved
    assert seen_baselines == ["blob:prev"], (
        f"讀不到畫面時要退回呼叫端給的值，不能拿 None 當基準：{seen_baselines}")
    print("  PASS\n")


def test_generate_loop_remembers_every_src_it_saved():
    print("test_generate_loop_remembers_every_src_it_saved")
    # 端到端：迴圈必須把每一張存過的 src 累積起來往下傳，否則上面那道防線
    # 拿不到資料，等於沒有。
    port = FakeBrowserPort()
    handed = []
    with _GenHarness() as h:
        counter = {"n": 0}

        def _gen(_port, _prev, *, seen_srcs=(), **_k):
            handed.append(list(seen_srcs))
            counter["n"] += 1
            return f"blob:{counter['n']}"

        ws.generate_one_image = _gen
        ws.download_image_with_retry = lambda *_a, **_k: True
        ws.generate_loop(port, "Alice", _cfg(images_per_character=3),
                         batch_start=0.0, out_dir=h.out_dir(),
                         resume_count=0, refill=None, minimize_fn=None)
    assert [len(s) for s in handed[1:]] == [len(handed[0]) + 1,
                                            len(handed[0]) + 2], (
        f"每存一張，往下傳的『存過的 src』就該多一筆：{handed}")
    assert handed[-1][-2:] == ["blob:1", "blob:2"], (
        f"最後一輪必須看得到前兩張的 src：{handed[-1]}")
    print("  PASS\n")


def test_generate_loop_flags_a_byte_identical_image():
    print("test_generate_loop_flags_a_byte_identical_image")
    # 內容層防線：URL 層擋不到「站方替同一張舊圖重新造了一條 blob URL」。
    port = FakeBrowserPort()
    with _GenHarness() as h:
        counter = {"n": 0}
        ws.generate_one_image = (
            lambda _port, _prev, **_k:
            counter.__setitem__("n", counter["n"] + 1) or f"blob:{counter['n']}")

        def _same_bytes_every_time(_port, _src, save_path, **_k):
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(b"IDENTICAL")
            return True

        ws.download_image_with_retry = _same_bytes_every_time
        ws.generate_loop(port, "Alice", _cfg(images_per_character=3),
                         batch_start=0.0, out_dir=h.out_dir(),
                         resume_count=0, refill=None, minimize_fn=None)
        dupes = h.events_of("duplicate_image")
    assert len(dupes) == 1, (
        f"內容相同要發事件，而且一個角色只發一次（避免洗版），實際：{dupes}")
    assert dupes[0]["image_index"] == 2, dupes


def test_a_duplicate_image_does_not_occupy_a_slot():
    """重複的那張要從磁碟上消失，而且**不能算進 `saved`**。

    2026-08-27 在正式輸出裡實際抓到一組：同一個角色的 #0043 與 #0057 是完全
    相同的 1,451,588 bytes，隔了 14 張、7 分鐘。也就是使用者要的 120 張裡，
    有一格是重複內容——而檔案數與計數都不會說。

    兩件事必須一起做：`folder_image_stats` 拿**資料夾檔數**當續跑的權威，所以
    只退計數不刪檔，磁碟上仍是滿的＝「做完了」，續跑不會補；只刪檔不退計數，
    檢查點的 `saved` 會比實際多。這條測試把兩邊一起釘住。

    刪的是第二份、內容與留下的第一份逐 byte 相同，所以沒有任何資訊消失。"""
    print("test_a_duplicate_image_does_not_occupy_a_slot")
    port = FakeBrowserPort()
    with _GenHarness() as h:
        counter = {"n": 0}
        ws.generate_one_image = (
            lambda _port, _prev, **_k:
            counter.__setitem__("n", counter["n"] + 1) or f"blob:{counter['n']}")

        def _third_repeats_the_first(_port, _src, save_path, **_k):
            save_path.parent.mkdir(parents=True, exist_ok=True)
            body = b"A" if counter["n"] in (1, 3) else b"B"
            save_path.write_bytes(body)
            return True

        ws.download_image_with_retry = _third_repeats_the_first
        out = h.out_dir()
        saved = ws.generate_loop(port, "Alice", _cfg(images_per_character=3),
                                 batch_start=0.0, out_dir=out,
                                 resume_count=0, refill=None, minimize_fn=None)
        on_disk = sorted(f.name for f in out.glob("*.png"))
        dupes = h.events_of("duplicate_image")
    assert saved == 2, f"重複的那張不可以算進去，應為 2/3：{saved}"
    assert len(on_disk) == 2, f"重複的檔案要刪掉，實際留下：{on_disk}"
    assert len(dupes) == 1 and dupes[0]["image_index"] == 3, dupes
    # 留下的必須是第一份與那張不同的，不是把兩份都刪掉。
    assert "0003" not in "".join(on_disk), (
        f"該刪的是後來的那一份：{on_disk}")
    assert "0001" in "".join(on_disk), f"第一份要留著當證據：{on_disk}"
    print("  PASS\n")


def test_generate_loop_stays_quiet_when_every_image_differs():
    print("test_generate_loop_stays_quiet_when_every_image_differs")
    # 對照組：正常情況不能誤報。
    port = FakeBrowserPort()
    with _GenHarness() as h:
        counter = {"n": 0}
        ws.generate_one_image = (
            lambda _port, _prev, **_k:
            counter.__setitem__("n", counter["n"] + 1) or f"blob:{counter['n']}")

        def _unique_bytes(_port, src, save_path, **_k):
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(src.encode())
            return True

        ws.download_image_with_retry = _unique_bytes
        ws.generate_loop(port, "Alice", _cfg(images_per_character=3),
                         batch_start=0.0, out_dir=h.out_dir(),
                         resume_count=0, refill=None, minimize_fn=None)
        dupes = h.events_of("duplicate_image")
    assert dupes == [], f"每張都不一樣就不該有任何提醒：{dupes}"


# ---------------------------------------------------------------------------
# 事件型別的兩邊契約：發出的 / 接住的
#
# 沒接住的事件是**靜默的 no-op**——webrunner 照寫進 events.ndjson，頻道什麼都
# 不會出現，而且沒有任何錯誤。
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _emitted_event_types() -> set[str]:
    """AST 掃出所有 `emit_event("…")` / `ws.emit_event("…")` 的字面型別名。"""
    found: set[str] = set()
    pkg = Path(ws.__file__).resolve().parent
    # `test/` 照同一個判準過濾：`conftest.py` 與手動 e2e 腳本 2026-09-22 之前住在
    # 套件裡、在這個範圍內，搬家之後範圍照舊。
    tests = Path(__file__).resolve().parent
    for path in sorted(pkg.glob("*.py")) + sorted(tests.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else "")
            if name != "emit_event":
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    # lru_cache 會把同一個物件交給每一個呼叫端——回可變的 set 等於把快取
    # 的內容攤在外面給人改。
    return frozenset(found)


@functools.lru_cache(maxsize=None)
def _handled_event_types() -> set[str]:
    """bot 的 `_handle_event` 裡每一個 `et == "…"` / `et in (…)`。"""
    bot = Path(ws.__file__).resolve().parent / "discord_bot.py"
    tree = ast.parse(bot.read_text(encoding="utf-8"), str(bot))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "et"):
            continue
        for operand in node.comparators:
            if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                found.add(operand.value)
            elif isinstance(operand, (ast.Tuple, ast.List, ast.Set)):
                found.update(e.value for e in operand.elts
                             if isinstance(e, ast.Constant)
                             and isinstance(e.value, str))
    return frozenset(found)


def test_every_emitted_event_is_handled_by_the_bot():
    print("test_every_emitted_event_is_handled_by_the_bot")
    emitted, handled = _emitted_event_types(), _handled_event_types()
    assert emitted, "AST 一個 emit_event 都沒掃到——掃描本身壞了"
    missing = emitted - handled
    assert not missing, (
        f"這些事件發得出去但 bot 沒接："
        f"{sorted(missing)}。沒接住＝靜默 no-op：檔案照寫、頻道沒反應、"
        f"也不會報錯，只有翻 events.ndjson 才看得出來。")


def test_the_event_scanners_are_not_quietly_broken():
    print("test_the_event_scanners_are_not_quietly_broken")
    # 上面兩支唯一的斷言都是「現況一致」，那種形狀在**掃描器自己壞掉**的時候
    # 一樣是綠的（兩邊都掃到空集合就相等了）。拿已知的樣本釘住下限。
    emitted, handled = _emitted_event_types(), _handled_event_types()
    for known in ("character_done", "quota_blocked", "todo_done"):
        assert known in emitted, f"emit 掃描漏了已知的 {known}：{sorted(emitted)}"
        assert known in handled, f"handle 掃描漏了已知的 {known}：{sorted(handled)}"
    assert len(emitted) >= 15, f"事件型別只掃到 {len(emitted)} 個，掃描八成壞了"


def test_sampler_log_names_the_label_that_actually_worked():
    print("test_sampler_log_names_the_label_that_actually_worked")
    # 正式 log 每次啟動都寫「setting Steps:=23 final -> True」，但 `Steps:` 每次
    # 都失敗、成功的是 `Steps`——以結果為準的判定配上以嘗試為準的敘述。
    names = ("expand_advanced_settings", "ensure_rescale_visible",
             "dump_advanced_labels", "set_numeric_setting_verified",
             "set_variety_plus", "_has_variety_plus", "human_pause")
    saved = {name: getattr(ws, name) for name in names}
    buffer = io.StringIO()
    try:
        ws.expand_advanced_settings = lambda _port: True
        ws.ensure_rescale_visible = lambda _port: True
        ws.dump_advanced_labels = lambda _port: None
        ws._has_variety_plus = lambda _port: False
        ws.set_variety_plus = lambda *_a, **_k: True
        ws.human_pause = lambda *_a, **_k: None
        # 只有不帶冒號的標籤有效——這就是實機上的情況。
        ws.set_numeric_setting_verified = (
            lambda _port, label, _value, **_k: not label.endswith(":"))
        with contextlib.redirect_stdout(buffer):
            ws.configure_sampler_settings(FakeBrowserPort())
    finally:
        for name, value in saved.items():
            setattr(ws, name, value)
    printed = buffer.getvalue()
    assert "setting Steps=23 final -> True" in printed, (
        f"『final ->』那行要報真正成功的候選，實際輸出：{printed!r}")
    assert "setting Steps:=23 final -> True" not in printed, (
        f"不能報一個其實失敗了的候選：{printed!r}")


def test_sampler_log_lists_the_candidates_when_none_worked():
    print("test_sampler_log_lists_the_candidates_when_none_worked")
    names = ("expand_advanced_settings", "ensure_rescale_visible",
             "dump_advanced_labels", "set_numeric_setting_verified",
             "set_variety_plus", "_has_variety_plus", "human_pause")
    saved = {name: getattr(ws, name) for name in names}
    buffer = io.StringIO()
    try:
        ws.expand_advanced_settings = lambda _port: True
        ws.ensure_rescale_visible = lambda _port: True
        ws.dump_advanced_labels = lambda _port: None
        ws._has_variety_plus = lambda _port: False
        ws.set_variety_plus = lambda *_a, **_k: True
        ws.human_pause = lambda *_a, **_k: None
        ws.set_numeric_setting_verified = lambda *_a, **_k: False
        with contextlib.redirect_stdout(buffer):
            ws.configure_sampler_settings(FakeBrowserPort())
    finally:
        for name, value in saved.items():
            setattr(ws, name, value)
    printed = buffer.getvalue()
    assert "Steps: / Steps=23 final -> False" in printed, (
        f"全失敗時要把試過的候選列出來，實際輸出：{printed!r}")


def test_the_numeric_setter_says_which_label_it_was_trying():
    print("test_the_numeric_setter_says_which_label_it_was_trying")
    # 沒有標籤的話，這行字在 log 裡是孤兒：連著兩行「失敗」接一行「成功」，
    # 看起來像同一個標籤時好時壞，其實是兩個不同的候選。
    names = ("set_numeric_setting", "read_numeric_setting", "human_pause")
    saved = {name: getattr(ws, name) for name in names}
    buffer = io.StringIO()
    try:
        ws.set_numeric_setting = lambda *_a, **_k: False
        ws.read_numeric_setting = lambda *_a, **_k: None
        ws.human_pause = lambda *_a, **_k: None
        with contextlib.redirect_stdout(buffer):
            got = ws.set_numeric_setting_verified(
                FakeBrowserPort(), "Steps:", 23, retries=2)
    finally:
        for name, value in saved.items():
            setattr(ws, name, value)
    assert got is False
    assert "[Steps:]" in buffer.getvalue(), (
        f"每次失敗都要說是哪個標籤：{buffer.getvalue()!r}")


def test_run_batch_shares_one_seen_set_across_characters():
    print("test_run_batch_shares_one_seen_set_across_characters")
    # 瀏覽器文件在角色之間不會重建（只有週期性的記憶體沖洗 `port.restart` 才
    # 會），所以上一個角色的圖還躺在站方的歷史區裡。每個角色各開一份「看過的」
    # 等於每換一個角色就把防線清空一次。
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P"])
        h.write_queue("todo_character1.md", ["a", "b"])
        _run_batch(FakeBrowserPort())
        calls = list(h.gen_calls)
    assert len(calls) == 2, calls
    for key in ("seen_srcs", "seen_digests"):
        first, second = calls[0][key], calls[1][key]
        assert first is not None, f"run_batch 必須把 {key} 傳下去：{calls[0]}"
        assert first is second, (
            f"{key} 在角色之間必須是**同一個**物件，否則每換一個角色防線就"
            f"清空一次")


# ---------- 關不掉的購買對話框：真 Escape + 控制項清單 ----------------------
# 實測：2026-08-29 之前的 86 次額度對話框，`dismiss_blocking_dialog` 一次都沒關
# 掉過，100% 走到「關不掉 → 整頁重新整理」。三個成因各有對策，這一組守後兩個：
#   1. 關閉鈕不是 <button>（選擇器認不出來）→ 放寬元素形狀，字面白名單不動。
#   2. Escape 只派給 `document`，掛在 modal 上的監聽器收不到 → 派給三個目標，
#      並先送 driver 層的真按鍵。
#   3. 關不掉時 log 什麼線索都沒有 → 印出對話框上的控制項清單。

class _ScriptedDialogPort:
    """依「是哪一段 JS」回答的假 port。不解析 JS，只認常數的身分。

    刻意不重用 `FakeBrowserPort`：這裡要精準控制「第幾次問對話框還在不在」的答
    案，用整頁假 DOM 反而看不出測的是什麼。
    """

    TRANSPORT_ERRORS = (RuntimeError,)

    def __init__(self, *, dismiss_action="escape", clears_when=None,
                 inventory=None, layers=None, native_error=None):
        self.dismiss_action = dismiss_action
        # `click_native` 要丟的例外（模擬 ElementClickInterceptedException）。
        self.native_error = native_error
        self.native_clicks = []      # driver 真點選收到的元素
        self.synthetic_clicks = []   # 退回 JS 合成點選的元素
        # None = 永遠關不掉；數字 N = 第 N 次（1-based）問的時候已經關掉了。
        self.clears_when = clears_when
        self.inventory = inventory
        # 疊了好幾層的對話框：每按一次就少一層，用來測「關到沒有為止」的迴圈。
        # 每一項是那一層的文字（`has_blocking_dialog` 的回傳）。
        self.layers = list(layers) if layers else None
        self.blocking_checks = 0
        self.real_escapes = 0
        self.synthetic_escapes = 0
        self.diag_calls = 0
        self.dismiss_calls = 0

    # 被挑中的元素。用具名哨兵而不是 `object()`，斷言失敗時看得懂是哪一顆。
    ELEMENT = "«the element the JS picked»"

    def _plan(self):
        """JS 現在回的是**計畫**（`{action, el}`），不是「已經按過了」。"""
        if self.dismiss_action == "escape":
            return "escape"
        return {"action": self.dismiss_action, "el": self.ELEMENT}

    def click_native(self, element):
        self.native_clicks.append(element)
        if self.native_error is not None:
            raise self.native_error

    def press_escape(self):
        self.real_escapes += 1
        return True

    def execute_script(self, script, *args):
        if script == "arguments[0].click();":
            self.synthetic_clicks.append(args[0] if args else None)
            return None
        if script is ws._DISMISS_DIALOG_JS:
            self.dismiss_calls += 1
            if self.layers is not None:
                if not self.layers:
                    return None          # 全部關光了
                self.layers.pop(0)       # 按下去 → 這一層關掉
                return self._plan()
            return self._plan()
        if script is ws._SEND_ESCAPE_JS:
            self.synthetic_escapes += 1
            return 3
        if script is ws._BLOCKING_DIALOG_JS:
            self.blocking_checks += 1
            if self.layers is not None:
                return self.layers[0] if self.layers else None
            if (self.clears_when is not None
                    and self.blocking_checks >= self.clears_when):
                return None
            return "Not enough Anlas. Purchase Anlas to continue."
        if script is ws._DIALOG_CONTROLS_DIAG_JS:
            self.diag_calls += 1
            return self.inventory
        raise AssertionError("unexpected script: " + repr(script[:60]))


def _no_pause():
    """關對話框那一組測試共用的前置：不要真的睡，而且從乾淨的記錄狀態開始。

    第二件事是必要的，不是順手。`_report_dismiss_outcome` 有一個**跨呼叫**的形狀
    快取（同一個形狀第二次起只印一行摘要），而測試共用同一個行程：上一支測試用過的
    形狀會讓下一支拿到摘要行、然後在「log 有沒有說出 X」那類斷言上失敗——**而且失敗
    訊息會指向完全無關的地方**（實測就是這樣：`test_the_log_names_which_click_
    mechanism_was_used` 抱怨 log 沒說 `[real]`，真正的原因是七支測試以前有人用過同一
    個形狀）。所以每一支測試都要從空的快取開始。
    """
    ws._DISMISS_LOG_SEEN.clear()
    saved = ws.human_pause
    ws.human_pause = lambda *_a, **_k: None
    return saved


def test_dismiss_sends_a_real_escape_before_the_synthetic_one():
    print("test_dismiss_sends_a_real_escape_before_the_synthetic_one")
    saved = _no_pause()
    try:
        port = _ScriptedDialogPort(clears_when=1)
        assert ws.dismiss_blocking_dialog(port) is True
        assert port.real_escapes == 1, (
            "driver 層的真 Escape 沒被送出去。合成的 KeyboardEvent 是 "
            "`isTrusted === false`，有些 focus-trap 會忽略它——真按鍵是最後一張牌")
        assert port.synthetic_escapes == 1, (
            "合成 Escape 仍然要送：真按鍵可能被 driver 擋掉，兩者是互補不是取代")
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_dismiss_still_works_on_a_port_without_press_escape():
    print("test_dismiss_still_works_on_a_port_without_press_escape")
    # 驗證腳本用的精簡 port、測試用的假 port 都可能沒有 press_escape；
    # 共用模組用 getattr 取它，缺了要跳過而不是 AttributeError。
    saved = _no_pause()
    try:
        port = _ScriptedDialogPort(clears_when=1)
        port.press_escape = None          # 不可呼叫 → 走 getattr 的退路
        assert ws.dismiss_blocking_dialog(port) is True
        assert port.real_escapes == 0
        assert port.synthetic_escapes == 1
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_a_clicked_dialog_never_gets_an_escape():
    print("test_a_clicked_dialog_never_gets_an_escape")
    # 點得到安全的關閉鈕就不該再送 Escape——Escape 在別的畫面上有別的意義
    # （例如取消正在打的提示詞），沒必要就不要送。
    saved = _no_pause()
    try:
        port = _ScriptedDialogPort(dismiss_action="clicked:Cancel",
                                   clears_when=1)
        assert ws.dismiss_blocking_dialog(port) is True
        assert port.real_escapes == 0 and port.synthetic_escapes == 0, (
            "已經點到關閉鈕了就不該再送 Escape")
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_dismiss_prints_the_control_inventory_when_it_fails():
    print("test_dismiss_prints_the_control_inventory_when_it_fails")
    import contextlib
    import io
    saved = _no_pause()
    inventory = {
        "w": 640, "h": 420, "total": 12,
        "controls": [
            {"tag": "div", "role": "", "aria": "Close", "title": "",
             "text": "", "cls": "sc-9f2a modal-x", "icon": 1,
             "dx": 596, "dy": 12, "w": 28, "h": 28, "cursor": "pointer",
             "corner": 16},
            {"tag": "button", "role": "", "aria": "", "title": "",
             "text": "Get Started", "cls": "sc-1b", "icon": 0,
             "dx": 40, "dy": 300, "w": 120, "h": 40, "cursor": "pointer",
             "corner": 520},
        ],
    }
    try:
        port = _ScriptedDialogPort(clears_when=None, inventory=inventory)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert ws.dismiss_blocking_dialog(port) is False
        out = buf.getvalue()
    finally:
        ws.human_pause = saved
    assert port.diag_calls == 1, "關不掉的時候要去問對話框上有哪些控制項"
    assert "could not dismiss" in out, out
    assert "aria='Close'" in out, (
        "控制項清單沒有印出 aria-label——那正是要拿來判斷「站方到底有沒有放關閉"
        "鈕」的欄位。實際輸出：" + out)
    assert "cursor=pointer" in out and "icon=1" in out, out
    assert "640x420" in out, "對話框尺寸沒印出來，位置欄位就沒有參考座標"
    print("  PASS\n")


def test_a_successful_dismiss_costs_no_extra_round_trip():
    print("test_a_successful_dismiss_costs_no_extra_round_trip")
    saved = _no_pause()
    try:
        port = _ScriptedDialogPort(dismiss_action="clicked:Cancel",
                                   clears_when=1)
        assert ws.dismiss_blocking_dialog(port) is True
        assert port.diag_calls == 0, (
            "控制項清單只在失敗那條路上跑；happy path 多一次 JS 往返沒有意義")
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_the_widened_selector_still_refuses_every_paying_label():
    print("test_the_widened_selector_still_refuses_every_paying_label")
    # 放寬的是**元素形狀**（div/span 也算候選），不是字面白名單。這一支把
    # 真實對話框上出現過的字（2026-08-29 的 log）再跑一次，確認放寬之後那條性質
    # 原封不動——這是整個改動裡唯一不能出錯的地方。
    for label in ("Purchase Anlas", "Buy more Anlas", "Subscribe",
                  "Pay As You Go", "Get Started", "Explore Our Plans",
                  "Upgrade", "Continue", "OK", "Yes"):
        assert not _js_would_pick(label), (
            "放寬選擇器之後 " + repr(label) + " 變成可點了——這會花掉使用者的錢")
    print("  PASS\n")


def test_the_dismiss_js_only_widened_the_element_shapes():
    print("test_the_dismiss_js_only_widened_the_element_shapes")
    js = ws._DISMISS_DIALOG_JS
    # 選擇器要收得到「不是 button 的關閉鈕」——站方的 × 常常是包著 svg 的 div。
    for needed in ("[aria-label]", "[title]", "[tabindex]", "[onclick]"):
        assert needed in js, (
            "候選選擇器少了 " + needed + "：非 <button> 的關閉鈕又會認不出來")
    # 但白名單本身不准動。
    assert "const DISMISS = /^(cancel|close|not now|" in js, (
        "DISMISS 白名單被改過了——放寬元素形狀可以，放寬字面不行")
    # 放寬之後一定要取最內層，否則會點到只是包著 Cancel 的外框（等於沒點）。
    assert "function innermost(" in js, (
        "少了 innermost：wrapper 的 innerText 也會等於 'Cancel'，"
        "而 el.click() 的 target 就是那個 wrapper，不會傳給裡面的按鈕")
    print("  PASS\n")


# ---------- 規則 3：右上角的無字圖示鈕 --------------------------------------
# 2026-09-07 的實測：`dismiss_blocking_dialog` **218/218 次全部關不掉**（13 天），
# 每一次都落到 'escape' → Escape 無效 → 整頁 reload ＋ `on_reload` 重填欄位。而那條
# reload 路徑正是本專案最嚴重一次故障的觸發路徑（欄位只被部分填回去，接著安靜地
# 用錯提示詞連燒 10 張圖、兩個小時）。
#
# **這個數字差點被 log 訊息本身騙掉。** `dismissed` 153 筆對 `could not dismiss`
# 65 筆，看起來成功率 70%——那 153 筆全是假的（舊版在驗證之前就無條件印
# 「dismissed」，是這個專案先前修掉的缺陷）。看得出真相的是一個**結構上的**矛盾：
# reload 只在 `not dismissed` 時發生，而每一天的 reload 次數都剛好等於當天的嘗試
# 次數，包括那些「成功」的日子。用 log 訊息做統計會把那些訊息說過的每一個謊一起
# 繼承下來，而且 log 格式會隨時間改變、縱向統計會無聲地混世代——要另找一個獨立的
# 量交叉驗證。
#
# 附帶的驗收基準線。這裡要記的是一個**結構性質**，不是一個會過期的數字：
# `page was reloaded` 與 verify 的 drift 行是 **1:1 對應**，而且 drift **沒有第二
# 個來源**。當初寫下時是 65/65；2026-09-08 重新量了一次，已經長到 **90/90**，兩個
# 方向都零例外（沒有一筆 drift 找不到前置 reload，也沒有一次 reload 之後沒跟著
# drift，全部相隔 6 行以內）。數字變了，性質沒變——所以引用的時候請引性質，別引
# 數字，不然下一個人看到 90≠65 會以為基準線壞了。
#
# **要 grep 的字串 2026-09-08 換過。** 那 90 次全部是「欄位讀回空的」，而它是
# reload 之後的穩態、90/90 自己重填就好，所以訊息從 `WARN: … drifted after fill`
# 降級成 `note: Character N was blank after fill`（理由寫在
# `verify_character_prompt` 裡）。`drifted after fill` 現在專指**非空但不同**＝
# autocomplete 污染，實測 0 次。用這條驗收時兩個字串都要數，否則會把「訊息改了」
# 誤讀成「drift 歸零了」。
#
# **這個性質才是驗收條件能成立的前提。** 「規則 3 修好之後 drift 應該歸零」只有在
# reload 是 drift 的唯一來源時才對；只要有第二個來源，它就永遠歸不了零，而看到它
# 沒歸零的人會誤判成修正失敗。所以每次要用這條驗收之前，先確認 1:1 還成立。
#
# 順帶量到的另一件事：`still mismatched after refill`（重填之後仍然對不上）在
# 全部 90 次裡是 **0 次**——reload 後的重填從來沒有真的失敗過。這會下修「與其關
# 對話框不如不要撞上它」那筆待辦的急迫性：它的價值主張是「拿掉 reload ＋ 重填這
# 條風險路徑」，而實測顯示這條路徑目前的失敗率是 0/90。風險仍在（最嚴重那次事故
# 就是它），但要用實際的失敗率去評估，不要用直覺。
#
# `describe_dialog_controls` 印出來的清單已經回答了「站方到底有沒有放關閉鈕」：
# 有，就是右上角那顆 32x32——只是它語意上完全是空的（text/aria/title 全空、
# icon=0，X 是 CSS 畫的），所以規則 1（文字）與規則 2（aria-label）對它一律失效。

# 正式 log（2026-09-03～09-07，每次額度用完印一次、內容完全一致）那份 856x917
# 購買對話框的 11 個可點候選：(編號, dx, dy, w, h, text)。
# 這份是**證據**，不是舉例——規則 3 的兩個門檻就是照它挑的，改門檻要回來對照。
_MEASURED_DIALOG = (856, 917)
_MEASURED_CONTROLS = [
    ("#1", 805, 21, 32, 32, ""),                    # <button> 關閉鈕
    ("#2", 805, 21, 32, 32, ""),                    # 它裡面那層裸 <div>
    ("#3", 410, 244, 126, 38, "Pay As You Go"),
    ("#4", 306, 244, 100, 38, "Subscribe"),
    ("#5", 571, 513, 197, 42, "Get Started"),
    ("#6", 571, 513, 197, 42, "Get Started"),
    ("#7", 331, 514, 199, 42, "Get Started"),
    ("#8", 331, 514, 199, 42, "Get Started"),
    ("#9", 57, 669, 37, 24, "Anlas"),               # 37x24——尺寸條件擋不住它
    ("#10", 103, 763, 37, 24, "Anlas"),
    ("#11", 21, 981, 108, 31, "Activate a Gift Key"),
]


def _corner_thresholds():
    """從 `_DISMISS_DIALOG_JS` **原始碼**抽出規則 3 的兩個門檻。

    同 `_dismiss_regexes` 的理由：測試裡另抄一份數字就只是在測抄本，門檻被放寬
    的時候不會有任何東西變紅。
    """
    import re as _re
    js = ws._DISMISS_DIALOG_JS
    out = {}
    for name in ("CORNER_MAX_PX", "CORNER_FRAC"):
        m = _re.search(r"const " + name + r" = ([0-9.]+);", js)
        assert m, f"could not find {name} in _DISMISS_DIALOG_JS"
        out[name] = float(m.group(1))
    return out


def _corner_rule_source():
    """規則 3 那一段 filter 的原始碼，**註解已剝掉**。

    註解裡本來就會逐條說明「尺寸上限擋什麼、右上角範圍擋什麼」，不剝掉的話
    刪掉真正的判斷式、只留註解，這支測試照樣綠——本專案已經記過這個坑。
    """
    js = ws._DISMISS_DIALOG_JS
    seg = js[js.index("const byCorner"):js.index("const hitCorner")]
    return "\n".join(line.split("//")[0] for line in seg.splitlines())


def _corner_rule_accepts(dx, dy, w, h, text, *, aria="", title="",
                         require_empty=True):
    """用**抽出來的**門檻重現規則 3 的幾何 ＋ 語意判定。

    `require_empty=False` ＝ 把 (a)「文字必須為空」那一條關掉，用來驗
    **縱深防禦**：即使有人放寬了 (a)，`FORBIDDEN` 仍然要擋下每一顆會花錢的按鈕。
    那正是 2026-09-07 補 `get started` / `gift key` / `anlas` 進 FORBIDDEN、以及讓
    (d) 同時掃 `readableOf` 所要買的東西——只測現狀的話等於沒測到新增的價值。
    """
    th = _corner_thresholds()
    box_w, box_h = _MEASURED_DIALOG
    readable = " ".join(x for x in (text, aria, title) if x).strip()
    if require_empty and readable:
        return False                                   # (a) 語意空白
    if _dismiss_regexes()["FORBIDDEN"].search(readable):
        return False                                   # (d) 縱深防禦
    if w < 8 or h < 8:
        return False
    if w > th["CORNER_MAX_PX"] or h > th["CORNER_MAX_PX"]:
        return False                                   # (b) 尺寸上限
    if dx + w < box_w - box_w * th["CORNER_FRAC"]:
        return False                                   # (c) 右上角（水平）
    if dy > box_h * th["CORNER_FRAC"]:
        return False                                   # (c) 右上角（垂直）
    return True


def test_the_corner_rule_picks_only_the_measured_close_button():
    print("test_the_corner_rule_picks_only_the_measured_close_button")
    accepted = [c[0] for c in _MEASURED_CONTROLS
                if _corner_rule_accepts(*c[1:5], c[5])]
    # #2 是裸 <div>（沒有任何屬性），根本不在 SELECTOR 裡，所以真正會被點的只有
    # #1。這裡的判定只看幾何 ＋ 語意，兩者都過是對的。
    assert accepted == ["#1", "#2"], (
        "規則 3 在正式那份對話框上挑中的不是右上角那顆關閉鈕："
        f"{accepted}——會花錢的每一顆都有文字，被挑中就是安全性質破了")
    for probe, dx, dy, w, h, text in _MEASURED_CONTROLS:
        if probe in ("#1", "#2"):
            continue
        assert text, (
            f"{probe} 在正式清單裡是有文字的；把它記成無文字會讓「語意空白」"
            "這條安全性質失去依據")
    print("  PASS\n")


def test_the_corner_rule_needs_both_geometry_conditions():
    print("test_the_corner_rule_needs_both_geometry_conditions")
    # 兩個門檻擋的是**不同**的東西。少了任何一個，下面就有一顆會被點下去——
    # 而它們單獨看都很像「多餘的條件」，這正是最容易被順手拿掉的一種。
    th = _corner_thresholds()
    box_w, box_h = _MEASURED_DIALOG

    # 只有「尺寸上限」擋得住：整片遮罩的右上角座標與關閉鈕**完全一樣**（右邊界
    # 貼齊、上邊界貼齊），兩個位置條件都過，只有尺寸分得開。
    assert not _corner_rule_accepts(0, 0, box_w, box_h, "")
    # 位置條件的**兩個方向**要各自驗，而且要挑「只違反其中一個」的座標。中央那顆
    # （412,442）水平垂直都違反，拿它當證據的話拿掉任一個方向都還是不會被點——
    # 兩道守門互相遮蔽，變異測試會誤判成「有守住」。
    assert not _corner_rule_accepts(12, 21, 32, 32, ""), (
        "左上角：垂直過、水平不過——只有水平條件擋得住")
    assert not _corner_rule_accepts(805, 860, 32, 32, ""), (
        "右下角：水平過、垂直不過——只有垂直條件擋得住")
    # 兩個都過 → 才是關閉鈕。
    assert _corner_rule_accepts(805, 21, 32, 32, "")
    # 門檻本身不准悄悄放寬：實測那顆是 32x32、對話框 856x917。
    assert 32 <= th["CORNER_MAX_PX"] <= 64, (
        f"CORNER_MAX_PX={th['CORNER_MAX_PX']}：太小會漏掉實測那顆 32x32，"
        "太大會開始收得到整片遮罩")
    assert 0.05 <= th["CORNER_FRAC"] <= 0.3, (
        f"CORNER_FRAC={th['CORNER_FRAC']}：放大到 0.5 就等於「對話框右半邊」，"
        "幾何條件形同虛設")
    print("  PASS\n")


def test_the_corner_rule_keeps_every_condition_in_the_js():
    print("test_the_corner_rule_keeps_every_condition_in_the_js")
    seg = _corner_rule_source()
    for needed, why in (
            # `readableOf` 現在有**兩個**用途，而且必須分開釘：功能條件（文字要空）
            # 與縱深防禦（FORBIDDEN 掃那段文字）。只寫 `"readableOf(el)" in seg`
            # 的話，拿掉功能條件之後 FORBIDDEN 那一行裡的 `readableOf(el)` 會讓
            # 守門繼續綠——實測過，這正是 2026-09-07 加縱深防禦時自己踩到的。
            ("if (readableOf(el)) return false;",
             "沒有「語意空白」這條，規則 3 就不再是「只挑純圖示鈕」——語意無害但"
             "有標籤的控制項會被當成關閉鈕按下去（FORBIDDEN 只擋得住帶付款語意的"
             "那一類，擋不住 `Manage Account` 這種）"),
            ("FORBIDDEN.test(readableOf(el))",
             "少了縱深防禦：那三顆（Get Started / Anlas / Activate a Gift Key）"
             "就會退回「完全只靠語意空白那一條」擋著，而那一條同時是功能條件"),
            ("FORBIDDEN.test(identityOf(el))",
             "少了對 id / name / data-testid 的縱深防禦"),
            ("CORNER_MAX_PX",
             "少了尺寸上限：整片遮罩的右上角座標跟關閉鈕一模一樣"),
            ("CORNER_FRAC",
             "少了右上角範圍：對話框別處的無字圖示鈕尺寸跟關閉鈕一模一樣")):
        assert needed in seg, f"規則 3 少了 {needed}——{why}"
    # 尺寸與位置各要**兩個**方向，只擋一邊等於沒擋。
    assert seg.count("CORNER_MAX_PX") >= 2, "尺寸上限只擋了寬或高其中一邊"
    assert seg.count("CORNER_FRAC") >= 2, "右上角範圍只擋了水平或垂直其中一邊"
    # 語意空白要收得比 innerText 廣，否則 <input value='Purchase'> 與
    # <button><img alt='Buy'></button> 都會被當成無字圖示鈕。
    body = ws._DISMISS_DIALOG_JS
    body = "\n".join(line.split("//")[0] for line in body.splitlines())
    for needed in ("'value'", "'alt'", "'aria-label'", "'title'"):
        assert needed in body, (
            f"readableOf 沒有收 {needed}：innerText 是空的不等於沒有字")
    print("  PASS\n")


# ---------- 站方疊了第二層對話框：要關到沒有為止 --------------------------
# 2026-09-07：規則 3 上線後連續五個額度週期都印「關不掉」，看起來像「找對了卻按
# 不動」。推翻那個結論的不是任何一則 log 訊息，而是 `describe_dialog_controls`
# 印出來的**對話框尺寸**——它在規則 3 上線的那一刻換了：
#     11:44 之前 856x917 / 11 個候選（Subscribe、Pay As You Go、Get Started）× 65
#     11:44 之後 420x322 /  5 個候選（Unsubscribe、Update Payment Details）×  5
# 而那份清單是**關閉動作跑完之後**才抓的。第一層真的被關掉了，露出後面第二層。

def test_a_second_dialog_underneath_gets_closed_too():
    print("test_a_second_dialog_underneath_gets_closed_too")
    saved = _no_pause()
    try:
        port = _ScriptedDialogPort(
            dismiss_action="clicked-corner:button@805,21 32x32",
            layers=["付費牆：Not enough Anlas…",
                    "帳號管理：Unsubscribe / Update Payment Details"])
        assert ws.dismiss_blocking_dialog(port) is True, (
            "只關了第一層就回報失敗——第二層對話框會讓整個額度週期退化成整頁 "
            "reload ＋ 重填欄位，而那是本專案最貴一次故障的觸發路徑")
        assert port.dismiss_calls == 2, (
            "應該剛好兩輪：關第一層（發現底下還有）→ 關第二層（確認乾淨了）。"
            "多一輪代表白花一次 JS 往返 ＋ 一次 human_pause。"
            f"實際 {port.dismiss_calls} 次")
        assert port.real_escapes == 0 and port.synthetic_escapes == 0, (
            "每一層都按得到關閉鈕，不該送 Escape")
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_a_button_that_changes_nothing_stops_the_loop_immediately():
    print("test_a_button_that_changes_nothing_stops_the_loop_immediately")
    # 進度判準是**對話框文字有沒有變**，不是「按過了沒」。一顆按不動的按鈕如果
    # 不當場收手，就會白白吃掉整組 round（每一輪還有 human_pause）。
    import contextlib
    import io
    saved = _no_pause()
    try:
        port = _ScriptedDialogPort(
            dismiss_action="clicked-corner:button@805,21 32x32",
            clears_when=None)          # 永遠關不掉，而且文字一直一樣
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert ws.dismiss_blocking_dialog(port) is False
        out = buf.getvalue()
        assert port.dismiss_calls == 2, (
            "第 2 輪就該發現「畫面一個字都沒變」並收手，"
            f"實際按了 {port.dismiss_calls} 輪")
        assert "did not change the screen" in out, (
            "收手的理由要寫進 log，否則看起來就像 round 用完了。實際輸出：" + out)
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_the_dismiss_loop_is_bounded():
    print("test_the_dismiss_loop_is_bounded")
    # 每一層文字都不一樣（站方一直疊新的），迴圈必須停在 max_rounds，不能無限跑。
    import contextlib
    import io
    saved = _no_pause()
    try:
        port = _ScriptedDialogPort(
            dismiss_action="clicked-corner:button@1,1 32x32",
            layers=[f"層 {i}" for i in range(50)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert ws.dismiss_blocking_dialog(port, max_rounds=3) is False
        assert port.dismiss_calls == 3, (
            f"max_rounds=3 就該按 3 輪，實際 {port.dismiss_calls}")
        assert "still blocked by a dialog after closing 3 layers in a row" in buf.getvalue(), buf.getvalue()
        assert port.diag_calls == 1, "用完 round 也要留下控制項清單"
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_escape_is_sent_at_most_once_across_the_whole_loop():
    print("test_escape_is_sent_at_most_once_across_the_whole_loop")
    # Escape 在別的畫面上有別的意義（例如取消正在打的提示詞），沒必要就不要送；
    # 而且對這個站台實測 218/218 無效，連送四次只是四倍的副作用。
    saved = _no_pause()
    try:
        port = _ScriptedDialogPort(dismiss_action="escape", clears_when=None)
        assert ws.dismiss_blocking_dialog(port) is False
        assert port.real_escapes == 1 and port.synthetic_escapes == 1, (
            f"Escape 應該只送一次，實際 real={port.real_escapes} "
            f"synthetic={port.synthetic_escapes}")
    finally:
        ws.human_pause = saved
    print("  PASS\n")


# 站方那兩層對話框上**每一顆會花錢／會造成損害**的按鈕，照 log 原文。
# 第一層（856x917 付費牆）＋ 第二層（420x322 帳號管理）。
_REAL_PAYING_LABELS = (
    "Pay As You Go", "Subscribe", "Get Started", "Anlas",
    "Activate a Gift Key",                      # 第一層
    "Unsubscribe", "Update Payment Details",    # 第二層（最壞：訂閱被退掉）
)


def test_forbidden_alone_still_blocks_every_paying_button():
    print("test_forbidden_alone_still_blocks_every_paying_button")
    # **這一支才是縱深防禦真正買到的東西。** 把 (a)「文字必須為空」關掉之後，
    # `FORBIDDEN` 必須自己擋下每一顆——因為 (a) 同時是規則 3 的**功能**條件
    # （關閉鈕是純圖示鈕），放寬它的人不會意識到自己在動安全性質。
    #
    # 2026-09-07 之前這一支會紅：`Get Started` / `Anlas` / `Activate a Gift Key`
    # 三顆**完全只靠 (a)** 擋住，連 `Unsubscribe` 那種「剛好含 subscribe」的第二層
    # 都沒有。
    forbidden = _dismiss_regexes()["FORBIDDEN"]
    for label in _REAL_PAYING_LABELS:
        assert forbidden.search(label), (
            f"{label!r} 不在 FORBIDDEN 的守備範圍內——它現在**只靠**「文字必須為空」"
            "擋著，而那一條同時是規則 3 的功能條件，有人放寬它就會直接去按這顆")
        assert not _corner_rule_accepts(805, 21, 32, 32, label,
                                        require_empty=False), (
            f"把「文字必須為空」關掉之後，規則 3 會去按 {label!r}")
    print("  PASS\n")


def test_the_added_forbidden_words_do_not_shadow_a_real_close_button():
    print("test_the_added_forbidden_words_do_not_shadow_a_real_close_button")
    # 加字的代價要是零：關閉鈕要嘛文字為空（規則 3），要嘛是 DISMISS 白名單裡的字
    # （規則 1）。沒有任何合理的關閉鈕會叫 `Get Started` 或 `Anlas`。
    for label in ("Cancel", "Close", "Not now", "Later", "Maybe later",
                  "Dismiss", "No thanks", "Back", "×"):
        assert _js_would_pick(label), (
            f"新增的 FORBIDDEN 字眼把合法的關閉字樣 {label!r} 也擋掉了")
    # 規則 3 的正常路徑（語意全空）不受影響。
    assert _corner_rule_accepts(805, 21, 32, 32, "")
    print("  PASS\n")


def test_the_account_dialog_buttons_are_never_clicked():
    print("test_the_account_dialog_buttons_are_never_clicked")
    # 第二層（帳號管理）比第一層更危險：第一層最壞是花錢，這一層最壞是**把訂閱
    # 退掉**，整條產線會停擺。三顆各自被不同的機制擋住，所以分開講清楚——
    # 「剛好擋到」跟「特意擋住」在下一次改 FORBIDDEN 的時候差很多。
    res = _dismiss_regexes()
    assert res["FORBIDDEN"].search("Unsubscribe"), (
        "`Unsubscribe` 沒被 FORBIDDEN 擋住——它含 `subscribe`，是**剛好**擋到的，"
        "所以任何縮窄 FORBIDDEN 的改動都要先過這一條")
    assert res["FORBIDDEN"].search("Update Payment Details"), (
        "`Update Payment Details` 沒被擋住（靠 `payment` 這個字）")
    assert res["FORBIDDEN"].search("Activate a Gift Key"), (
        "`Activate a Gift Key` 沒被擋住。它原本不含任何 FORBIDDEN 字眼、"
        "完全只靠「文字必須為空」擋著，2026-09-07 才補上 `gift key` 當縱深防禦")
    for label in ("Unsubscribe", "Update Payment Details",
                  "Activate a Gift Key"):
        assert not _js_would_pick(label), (
            f"第二層對話框上的 {label!r} 變成可點了")
    print("  PASS\n")


def test_a_corner_click_never_gets_an_escape():
    print("test_a_corner_click_never_gets_an_escape")
    import contextlib
    import io
    saved = _no_pause()
    try:
        port = _ScriptedDialogPort(
            dismiss_action="clicked-corner:button@805,21 32x32", clears_when=1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert ws.dismiss_blocking_dialog(port) is True
        out = buf.getvalue()
        assert port.real_escapes == 0 and port.synthetic_escapes == 0, (
            "規則 3 已經點到關閉鈕了就不該再送 Escape")
        assert "clicked-corner" in out and "dismissed" in out, (
            "log 要說出是靠哪一條規則關掉的——關閉率是這個功能唯一的成效指標，"
            "而 log 是唯一的觀測點。實際輸出：" + out)
    finally:
        ws.human_pause = saved
    print("  PASS\n")


# ---------- 按的動作搬到 driver：JS 只挑，Python 才按 ----------------------
# 2026-09-07。「關到沒有為止」的迴圈上線後，四個額度週期的 log 形狀完全一致：
# 第 1 層 @(805,21) 按下去畫面真的換了、第 2 層 @(369,21) 按下去**一個字都沒變**。
# 兩層那顆關閉鈕的 class 一模一樣，所以「元件不同」解釋不了；解釋得了的只有
# 「元素之上有東西蓋著」與「handler 掛在 pointerdown/mousedown」，兩者都只有
# driver 的真點選補得起來。
#
# **這批測試同時守住一件更貴的事：安全性質的判定點搬家了。** 以前是「JS 不會
# `click()` 到會花錢的按鈕」，現在是「JS 不會**回傳**會花錢的按鈕」。釘在 click
# 事件上的斷言在新版裡必然通過（那段 JS 一次都不按），等於什麼都沒驗。


class _NoNativeClickPort(_ScriptedDialogPort):
    """沒有 `click_native` 的精簡 port（其他驗證腳本、舊的假 port）。

    `getattr(port, "click_native", None)` 取到 None → 退回合成點選，行為與舊版
    相同。這一支存在的理由是那條退路不能安靜地整個不見。
    """

    click_native = None


class _InterceptedClick(Exception):
    """`ElementClickInterceptedException` 的形狀——訊息尾巴會指名蓋住目標的元素。

    刻意用自訂例外而不是 import selenium 的：`_webrunner_shared` 是 driver
    agnostic 的，測試也不該把 selenium 拉進來（`test_shared_is_driver_agnostic`
    守著那條線）。這裡要驗的是「訊息尾巴那一句有沒有被留下來」，跟型別無關。
    """


def test_the_dismiss_click_goes_through_the_driver_not_javascript():
    print("test_the_dismiss_click_goes_through_the_driver_not_javascript")
    saved = _no_pause()
    try:
        port = _ScriptedDialogPort(
            dismiss_action="clicked-corner:button@369,21 32x32", clears_when=1)
        assert ws.dismiss_blocking_dialog(port) is True
        assert port.native_clicks == [port.ELEMENT], (
            "沒有走 driver 的真點選。合成的 `el.click()` 只往上冒泡、而且只派送 "
            "click 一種事件，站方第二層對話框對它毫無反應（實測四個額度週期）"
            f"——實際 native_clicks={port.native_clicks}")
        assert port.synthetic_clicks == [], (
            "真點選成功了還多按一次合成點選——那會在別的畫面上造成多餘的副作用")
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_an_intercepted_real_click_falls_back_to_a_synthetic_one():
    print("test_an_intercepted_real_click_falls_back_to_a_synthetic_one")
    import contextlib
    import io
    saved = _no_pause()
    try:
        # W3C 的 element click 會先檢查「收到點選的是不是目標本身或它的後代」，
        # 不是就丟這個。訊息尾巴那一句是唯一能直接回答「是不是有東西蓋在上面」的
        # 證據——而那正是把點選搬到 driver 所要換來的診斷。
        boom = _InterceptedClick(
            "Message: element click intercepted: Element <button "
            "class=\"sc-2f2fb315-2 sc-29539429-20 sc-1336beac-0 eTBYIC "
            "jjGTfR fJg\"> is not clickable at point (821, 33). "
            "Other element would receive the click: "
            "<div class=\"overlay-that-covers-it\"></div>\n"
            "Stacktrace:\n  #0 0x00007ff6 <unknown>\n  #1 0x00007ff7 <unknown>")
        port = _ScriptedDialogPort(
            dismiss_action="clicked-corner:button@369,21 32x32",
            clears_when=1, native_error=boom)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert ws.dismiss_blocking_dialog(port) is True
        assert port.native_clicks == [port.ELEMENT], "真點選沒被試過"
        assert port.synthetic_clicks == [port.ELEMENT], (
            "真點選被擋下來之後沒有退回合成點選——舊行為（合成點選）本來就關得掉"
            "第一層，退路少了就是純粹的退步")
        text = err.getvalue()
        assert "overlay-that-covers-it" in text, (
            "log 沒有留下「是誰蓋在上面」。這一句是整個改動唯一的診斷產出，"
            "`_short_error` 的 180 字上限剛好會把它切掉，所以這裡用 "
            f"`_long_error`。實際輸出：{text!r}")
        assert "Stacktrace" not in text, (
            "chromedriver 的 15 行 C++ 位址被原樣印出來了")
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_a_port_without_click_native_still_uses_the_synthetic_click():
    print("test_a_port_without_click_native_still_uses_the_synthetic_click")
    saved = _no_pause()
    try:
        port = _NoNativeClickPort(
            dismiss_action="clicked:Cancel", clears_when=1)
        assert ws.dismiss_blocking_dialog(port) is True
        assert port.synthetic_clicks == [port.ELEMENT], (
            "精簡 port（沒有 click_native）應該退回合成點選，而不是安靜地什麼都"
            "不按——不按的話對話框永遠關不掉，而且回傳值會誤報")
        assert port.real_escapes == 0, "按得到就不該送 Escape"
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_a_target_that_cannot_be_clicked_at_all_falls_through_to_escape():
    print("test_a_target_that_cannot_be_clicked_at_all_falls_through_to_escape")
    import contextlib
    import io

    class _BothFail(_ScriptedDialogPort):
        def execute_script(self, script, *args):
            if script == "arguments[0].click();":
                self.synthetic_clicks.append(args[0] if args else None)
                raise _InterceptedClick("Message: element not interactable")
            return super().execute_script(script, *args)

    saved = _no_pause()
    try:
        port = _BothFail(
            dismiss_action="clicked-corner:button@369,21 32x32",
            native_error=_InterceptedClick("Message: element click intercepted"),
            clears_when=None)
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            assert ws.dismiss_blocking_dialog(port) is False
        assert port.real_escapes == 1 and port.synthetic_escapes == 1, (
            "兩種按法都失敗之後沒有退到 Escape。階梯的每一階都要接得住，"
            "少一階就等於直接掉到整頁 reload"
            f"（real={port.real_escapes} synthetic={port.synthetic_escapes}）")
        assert "unclickable -> escape" in buf.getvalue(), (
            "log 沒有說出「挑中了但按不動、改送 Escape」。"
            "只印 `clicked-corner:…` 會讓人以為按下去了——這正是本專案記過四次的"
            f"『報的是嘗試不是結果』。實際輸出：{buf.getvalue()!r}")
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_a_browser_gone_error_during_the_dismiss_click_is_escalated():
    print("test_a_browser_gone_error_during_the_dismiss_click_is_escalated")
    saved = _no_pause()
    try:
        port = _ScriptedDialogPort(
            dismiss_action="clicked-corner:button@369,21 32x32",
            native_error=RuntimeError(
                "Message: no such window: target window already closed"))
        try:
            ws.dismiss_blocking_dialog(port)
        except ws.BrowserGoneError:
            pass
        else:
            raise AssertionError(
                "視窗已經關掉了還被當成「這一顆按不動」吞掉——接著會退回合成點選、"
                "送 Escape、印控制項清單，全部對著一個不存在的 session 打，"
                "而真正的原因會被埋在一堆 transport 例外底下")
        assert port.synthetic_clicks == [], (
            "session 都沒了還去試合成點選")
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_the_dismiss_js_hands_the_element_back_instead_of_clicking_it():
    print("test_the_dismiss_js_hands_the_element_back_instead_of_clicking_it")
    # 這一支釘的就是安全性質的**判定點**。JS 一旦自己按下去，Python 這一側再怎麼
    # 檢查都來不及——所以「那段 JS 完全不呼叫 .click()」必須是可驗證的事實。
    body = "\n".join(line.split("//")[0]
                     for line in ws._DISMISS_DIALOG_JS.splitlines())
    assert ".click()" not in body, (
        "`_DISMISS_DIALOG_JS` 又自己按下去了。按的動作必須留在 Python "
        "（`_click_dismiss_target`）：JS 自己按的話 (1) 只派送 click 一種事件、"
        "只往上冒泡，站方第二層對話框對它沒反應；(2) 安全性質的判定點會退回"
        "「不會 click 它」，而那條線在真 DOM 裡驗不到「按到的是不是別的元素」")
    for hit in ("hitText", "hitAria", "hitCorner"):
        assert f"el: {hit}" in body, (
            f"規則沒有把 {hit} 交回 Python——回傳裡少了元素，"
            "呼叫端只會拿到一個字串然後什麼都按不到（安靜地退化成整頁 reload）")
    print("  PASS\n")


def test_the_log_names_which_click_mechanism_was_used():
    print("test_the_log_names_which_click_mechanism_was_used")
    import contextlib
    import io
    # 真點選成功 vs 悄悄退回合成點選，是判斷「這一層到底吃不吃合成事件」唯一的
    # 觀測點。兩者印一樣的話，正式 log 又會變成一則說不出機制的訊息——本專案已經
    # 為同一個毛病付過四次代價。
    saved = _no_pause()
    try:
        seen = {}
        for label, kwargs in (
                ("real", {}),
                ("synthetic", {"native_error": _InterceptedClick("nope")})):
            port = _ScriptedDialogPort(
                dismiss_action="clicked-corner:button@369,21 32x32",
                clears_when=1, **kwargs)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(io.StringIO()):
                ws.dismiss_blocking_dialog(port)
            seen[label] = buf.getvalue()
            assert f"[{label}]" in seen[label], (
                f"log 沒有說出這一按是 {label}：{seen[label]!r}")
        assert seen["real"] != seen["synthetic"], (
            "兩種按法印出一模一樣的訊息")
    finally:
        ws.human_pause = saved
    print("  PASS\n")


def test_the_escape_js_reaches_more_than_the_document():
    print("test_the_escape_js_reaches_more_than_the_document")
    js = ws._SEND_ESCAPE_JS
    assert "activeElement" in js, (
        "Escape 沒派給焦點元素：事件從 document 只往上冒泡到 window，不會往下傳")
    assert 'role="dialog"' in js, "Escape 沒派給 modal 本身"
    print("  PASS\n")


def _captured_script(call) -> str:
    """跑 `call(port)`，把它送給 `port.execute_script` 的**實際**字串抓回來。

    抓真的送出去那一份、而不是掃原始碼，是刻意的：`_JS_VISIBLE` 加上一段
    原始字串的串接，在原始碼文字上看不出結果，而「有沒有帶上判準」問的正是
    結果。
    """

    class _Capture:
        TRANSPORT_ERRORS = (RuntimeError,)

        def execute_script(self, script, *args):
            raise _ScriptCaptured(script)

    try:
        call(_Capture())
    except _ScriptCaptured as captured:
        return captured.script
    raise AssertionError("那支函式沒有呼叫 port.execute_script")


class _ScriptCaptured(BaseException):
    """刻意繼承 BaseException：受測程式碼的 `except Exception` 不可以吃掉它。"""

    def __init__(self, script: str):
        super().__init__(script)
        self.script = script


# 「**容器**」選擇器——查詢對話框／吐司**本身**的那些。
#
# 判準刻意下在**機制**上，不是列一份名單：`offsetParent` 只在元素**自己**的
# computed position 是 `fixed` 時回 null（規範如此），所以
#   * 查詢容器本身的掃描（modal 遮罩、吐司幾乎都是 fixed）→ **不能**用它；
#   * 查詢容器**裡面**子元素的掃描 → 可以，子元素是 static，`offsetParent` 拿到的
#     是那個 fixed 祖先，非 null。
# `_COOKIE_CONSENT_JS` 正是後者（掃橫幅裡的 `<button>`），所以它用 `offsetParent`
# 是對的，而且不是「掃描剛好漏掉它」——見
# `test_the_cookie_banner_scan_is_excluded_by_mechanism_not_by_luck`，那支附了正面
# 對照組：把它的 selector 換成容器，這個判準立刻咬得到它。
_OVERLAY_CONTAINER_SELECTOR = re.compile(
    r'\[role="(?:dialog|alertdialog|alert)"\]'
    r'|\[aria-modal="true"\]'
    r'|\[aria-live="'
    r'|\[class\*="(?:[Mm]odal|[Tt]oast|[Nn]otification)"\]')

# 這六個今天在集合裡。**這是 canary 不是清單**：純數字回答不了「範圍有沒有縮掉」
# ——集合從 6 掉到 5 跟從 6 長到 7 在一個 `len()` 上長得一樣，而縮掉才是危險的那
# 個方向。少任何一個都要當場點名。
_KNOWN_OVERLAY_SCANS = frozenset({
    "_GENERATION_BLOCK_JS", "_DISMISS_DIALOG_JS", "_SEND_ESCAPE_JS",
    "_DIALOG_CONTROLS_DIAG_JS", "_BLOCKING_DIALOG_JS", "get_generation_error",
})


def _js_string_literals(source: str) -> list[tuple[str, str]]:
    """`(擁有者名稱, 完整 script)`——模組裡每一段 JS 字面。

    兩件事讓它比「讀模組常數」準確：

    * `_JS_VISIBLE + r"…"` 會**還原成串接後的完整字串**。串接在原始碼文字上看不
      出結果，而「這段 script 有沒有帶上判準」問的正是結果。
    * **內嵌在函式裡的 script 一樣收得到**，擁有者記成函式名。2026-09-10 之前
      `get_generation_error` 的吐司掃描就是因為只掃模組層常數而漏了四個月。

    docstring 跳過：本檔與受測模組的說明文字本來就要引用 `role="dialog"` 之類的
    字樣來解釋規則，收進來會讓判準被自己的說明絆倒。
    """
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    found: list[tuple[str, str]] = []

    def visit(node, owner: str, in_func: bool) -> None:
        for child in ast.iter_child_nodes(node):
            name, nested = owner, in_func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name, nested = child.name, True
            elif (not in_func and isinstance(child, ast.Assign)
                  and child.targets
                  and isinstance(child.targets[0], ast.Name)):
                # 只有模組層的 assign 才拿來當名字；函式裡的區域變數不該蓋掉
                # 函式名（那才是讀 log 的人找得到的東西）。
                name = child.targets[0].id
            if (isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and id(child) not in docstrings):
                full = child.value
                if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
                        and isinstance(node.left, ast.Name)
                        and node.left.id == "_JS_VISIBLE"
                        and node.right is child):
                    full = ws._JS_VISIBLE + child.value
                found.append((name, full))
            visit(child, name, nested)

    visit(tree, "<module>", False)
    return found


def _visibility_scans(source: str | None = None) -> dict[str, str]:
    """所有「掃對話框／吐司**容器**」的 JS，name → 真正送出去的字串。

    ⚠️ **這個集合是推導出來的，不是寫死的名單。** 2026-09-10 之前這裡是五個常數
    名 ＋ 一個內嵌，配一句 `len(scans) == 6`——而那**只擋得住縮水，擋不住新增**：
    實測注入一個「新的第七個掃描點、而且退回 `offsetParent`」之後，本族三支測試
    **全部照樣綠**，完全沒看到它。全 repo 只有這一族在看 `offsetParent`
    （`grep offsetParent test/test_*.py`），所以那等於沒有人在看。
    這正是那個家族：「規則的文字沒有提到任何模組，掃描
    範圍卻寫死成一份清單」。

    `source` 是參數，就是為了讓範圍釘樁餵得進合成語料（§8.8(A7)）——一份寫死的
    常數名清單不可能含有 `_BRAND_NEW_DIALOG_JS` 這種名字，所以只有「換一份原始碼
    進來」才問得出「新增的掃描點會不會被發現」。

    **兩個來源都要看，因為它們各自漏一半**：原始碼 AST 才看得到**函式裡內嵌**的
    script（`get_generation_error` 就是那樣漏掉四個月的），而模組屬性才看得到
    「值不是在這份原始碼裡寫成字面」的常數（組出來的、匯入進來的、測試期間裝上去
    的）。取聯集，任一邊發現就算數。
    """
    scans = {}
    if source is None:
        # 模組屬性：值是 str 而且查詢了容器選擇器的，一律收。
        scans.update({name: value for name, value in vars(ws).items()
                      if isinstance(value, str)
                      and _OVERLAY_CONTAINER_SELECTOR.search(value)})
        source = Path(ws.__file__).read_text(encoding="utf-8")
    scans.update({name: script
                  for name, script in _js_string_literals(source)
                  if _OVERLAY_CONTAINER_SELECTOR.search(script)})
    return scans


def _offsetparent_violations(scans: dict[str, str]) -> list[str]:
    """回報哪些對話框掃描退回了 `offsetParent` 或掉了判準。

    抽成函式是為了讓「真實原始碼」與「合成語料」跑**同一段**判定——否則釘樁測的
    會是它自己抄的一份規則，而不是上線的那一份。
    """
    return sorted(
        name for name, js in scans.items()
        if "offsetParent" in js
        or "function visible(" not in js or "function onScreen(" not in js)


def test_the_dialog_scans_never_use_offsetparent():
    print("test_the_dialog_scans_never_use_offsetparent")
    # `offsetParent` 對 `position: fixed` 的元素**一律回 null**，而 modal 與吐司
    # 幾乎都是 fixed。2026-08-29 在真 DOM 裡量到：同一份 DOM 加上 fixed，Tier 1 /
    # Tier 2 / dismiss / 控制項清單四層一起回空，log 上只看得到「生成逾時」。
    scans = _visibility_scans()
    # 具名 canary，不是 `len(...) == 6`：集合從 6 掉到 5 跟從 6 長到 7 在一個數字
    # 上長得一樣，而縮掉才是危險的方向，所以少了誰要當場點名。
    missing = sorted(_KNOWN_OVERLAY_SCANS - set(scans))
    assert not missing, (
        f"掃描範圍縮水了，這些已知的對話框掃描不見了：{missing}"
        f"（現在找到的是 {sorted(scans)}）——是改名了，還是判準被收窄了？")
    violations = _offsetparent_violations(scans)
    assert not violations, (
        f"{violations} 退回 offsetParent 或掉了 `_JS_VISIBLE` 的判準——"
        "position:fixed 的對話框會整個看不到（Tier 1／Tier 2／dismiss／控制項"
        "清單一起瞎掉，log 上只看得到「生成逾時」）。")
    print("  PASS\n")


def test_a_newly_added_dialog_scan_is_found_without_being_registered():
    """**範圍是推導的**：第七個掃描點不必登記就會被發現，退回 `offsetParent` 就紅。

    這支才是 2026-09-10 那個缺口的釘子。舊版是五個常數名 ＋ 一句
    `len(scans) == 6`，於是新增一個掃描點**完全不會被看到**（實測三支全綠）。
    合成語料走的是 `_visibility_scans(source=...)`，判定走的是跟真實原始碼**同一支**
    `_offsetparent_violations`。
    """
    print("test_a_newly_added_dialog_scan_is_found_without_being_registered")
    real = Path(ws.__file__).read_text(encoding="utf-8")
    seventh = '\n\n_BRAND_NEW_DIALOG_JS = _JS_VISIBLE + r"""\n' + (
        'const nodes = document.querySelectorAll('
        '\'[role="dialog"],[aria-modal="true"]\');\n'
        'for (const node of nodes) {\n'
        '  if (node.offsetParent === null) continue;\n'
        '  return node.innerText;\n'
        '}\n'
        'return null;\n') + '"""\n'

    scans = _visibility_scans(real + seventh)
    assert "_BRAND_NEW_DIALOG_JS" in scans, (
        "新增的對話框掃描沒有被發現——`_visibility_scans` 是不是又退回寫死名單了？"
        f"（找到的是 {sorted(scans)}）")
    assert _offsetparent_violations(scans) == ["_BRAND_NEW_DIALOG_JS"], (
        "推導到了卻沒有判它違規——判定那一段壞了")

    # 正面對照：同一段語料**不用** offsetParent 的話必須乾淨過關，否則上面那句
    # 可能只是因為「什麼都判成違規」。
    clean = seventh.replace("node.offsetParent === null", "!onScreen(node)")
    assert clean != seventh
    clean_scans = _visibility_scans(real + clean)
    assert "_BRAND_NEW_DIALOG_JS" in clean_scans
    assert _offsetparent_violations(clean_scans) == [], (
        "合規的第七個掃描點也被判違規了——判準太寬")
    print("  PASS\n")


def test_the_ast_reconstruction_matches_what_is_actually_sent():
    """AST 還原出來的 script 必須跟**真的送出去**的那一份逐字相同。

    這是推導式範圍的隱藏破口：`_js_string_literals` 是自己把
    `_JS_VISIBLE + r"…"` 接回去的，一旦那段還原寫錯（最可能是忘了接前言），
    本族每一句「有沒有帶上判準」都會改成在檢查**另一個字串**——而且三支照樣綠，
    因為它們檢查的東西彼此一致，只是跟現實無關。所以要有一支拿真貨對帳。

    `get_generation_error` 是唯一的內嵌 script，也就是唯一需要還原的那一種，
    所以拿它當樣本。（順帶：這也是 `_captured_script` 現在的用途——2026-09-10 把
    範圍改成推導式時它一度變成沒有呼叫端的孤兒，正是本專案反覆記過的
    「替換一個訊號的時候，要 grep 舊 helper 的名字」。）
    """
    print("test_the_ast_reconstruction_matches_what_is_actually_sent")
    sent = _captured_script(ws.get_generation_error)
    derived = _visibility_scans()["get_generation_error"]
    assert derived == sent, (
        "AST 還原出來的 script 跟真正送給 port.execute_script 的不一樣——"
        "`_js_string_literals` 的 `_JS_VISIBLE` 串接還原壞了，本族其他測試現在"
        "檢查的是一個不存在的字串。")
    # 正面對照：樣本真的是「需要還原」的那一種（前言 ＋ 自己的 body），不是一段
    # 剛好不必還原的字面——否則這支測不到還原邏輯。
    assert sent.startswith(ws._JS_VISIBLE) and len(sent) > len(ws._JS_VISIBLE), (
        "樣本不再是「前言 ＋ 內嵌 body」的形狀，這支測試證明不了還原是對的")
    print("  PASS\n")


def test_the_cookie_banner_scan_is_excluded_by_mechanism_not_by_luck():
    """`_COOKIE_CONSENT_JS` 用 `offsetParent` 是**對的**，而排除它必須是有理由的。

    這是把範圍改成推導式時最容易踩的雷：判準寫寬一點就會把同意橫幅一起收進來，
    然後「修好」一段本來就正確的程式。

    它為什麼可以用 `offsetParent`：它掃的是橫幅**裡面**的 `<button>`。`offsetParent`
    只在元素**自己**是 fixed 時回 null，靜態定位的子元素拿到的是那個 fixed 祖先
    （非 null）。這不是規範推論——2026-08-30 用全新暫時 profile 實測時
    `Reject Non-Essential` 是**真的被點到**的，而那條路徑的條件就是
    `b.offsetParent !== null`；按鈕被點到就證明它非 null。
    """
    print("test_the_cookie_banner_scan_is_excluded_by_mechanism_not_by_luck")
    cookie = ws._COOKIE_CONSENT_JS
    assert "offsetParent" in cookie, (
        "同意橫幅那段不再用 offsetParent 了——這支測試的前提要重寫")
    assert "_COOKIE_CONSENT_JS" not in _visibility_scans(), (
        "同意橫幅被收進對話框掃描集合了。它掃的是橫幅裡的 <button>，"
        "`offsetParent` 對它是正確的；把它一起『修好』會改壞一段實測有效的程式。")
    assert not _OVERLAY_CONTAINER_SELECTOR.search(cookie), (
        "排除的理由必須是「它沒有查詢容器」這個機制")

    # **正面對照組——這一句才是「不是靠掃描剛好漏掉」的證據。** 判準永遠不咬人
    # 的話上面兩句照樣綠；把同一段的 selector 換成容器，它必須立刻被選中。
    as_container = cookie.replace(
        "document.querySelectorAll('button')",
        "document.querySelectorAll('[role=\"dialog\"]')")
    assert as_container != cookie, "對照組的錨點沒對上，這支測試證明不了任何事"
    assert _OVERLAY_CONTAINER_SELECTOR.search(as_container), (
        "把 selector 換成容器之後判準還是不咬——那它排除同意橫幅只是巧合")
    print("  PASS\n")


def test_the_container_layer_is_stricter_than_the_control_layer():
    print("test_the_container_layer_is_stricter_than_the_control_layer")
    # 兩種誤判的代價相反，所以兩層用不同判準：
    #   容器（對話框本身）誤判成「有」→ 白等一小時 → 用嚴的 onScreen。
    #   控制項誤判成「沒有」→ 漏掉真的 Cancel → 用寬的 visible。
    # 吐司也是**容器**（agent 檔明寫「吐司幾乎都是 position:fixed」），所以
    # `get_generation_error` 一起用嚴的那一支。
    # **範圍跟著推導走，不再是寫死的五個名字。** 判準本身就是「這段 script 查詢了
    # 容器選擇器」，而查了容器就必須用嚴的那一支——所以「被 `_visibility_scans`
    # 選中」與「必須有 `onScreen`」是同一件事的兩面，沒有理由再列一次名單。
    # 條件寫成 `onScreen(` 而不是 `onScreen(node)`：迴圈變數叫什麼是無關的，而一個
    # 會為了變數改名而亂叫的守門就是會被關掉的守門。
    #
    # ⚠️ **但要先把 `_JS_VISIBLE` 那段前言拿掉再找。** 前言裡有
    # `function onScreen(el) {`，所以直接在完整 script 上找 `onScreen(` **對每一段
    # 串接過的 script 都恆真**——那是一句被「宣告」餵飽的空斷言，測不到有沒有人真的
    # **呼叫**它。（寫這支時實際踩到：注入一個用 `offsetParent` 的第七段，這一句照樣
    # 綠。舊版寫 `onScreen(node)` 反而沒事，因為宣告的參數叫 `el`。）
    scans = _visibility_scans()
    loose = sorted(name for name, js in scans.items()
                   if "onScreen(" not in js.replace(ws._JS_VISIBLE, ""))
    assert not loose, (
        f"{loose} 查詢了對話框／吐司**容器**卻沒有用嚴的 `onScreen`——站方頁面裡"
        "opacity:0 或移到畫面外的 modal 殼會被當成真的擋住，代價是白等一小時"
        "（`quota_wait_max_sec` 預設 0 ＝ 無上限）。")
    assert ".filter(visible)" in ws._DISMISS_DIALOG_JS, (
        "對話框裡的控制項不該用 onScreen——長對話框裡捲到視窗外的 Cancel "
        "會點不到，那才是更大的害處")
    print("  PASS\n")


def test_the_visibility_predicate_rejects_the_three_hidden_shapes():
    print("test_the_visibility_predicate_rejects_the_three_hidden_shapes")
    js = ws._JS_VISIBLE
    for token, why in (
            ("getClientRects", "display:none 與未佈局要靠它擋"),
            ("visibility", "visibility:hidden 舊判準漏掉（offsetParent 是非 null）"),
            ("opacity", "opacity:0 的 modal 殼會被誤判成真的擋住"),
            ("innerWidth", "移到畫面外的 modal 殼要靠視窗交集擋掉")):
        assert token in js, f"可見性判準少了 {token}：{why}"
    print("  PASS\n")


def test_only_one_copy_of_the_visibility_predicate_exists():
    """全模組只准有**一份**可見性判準，就是 `_JS_VISIBLE`。

    這支取代了舊的 `test_the_toast_scan_keeps_its_own_copy_in_step`。那支守的
    是「抄本要跟本尊同步」，而抄本本身才是缺陷：它存在的理由（「內嵌 JS 接不
    到模組層常數」）是**假的**，同檔五個常數都是 `_JS_VISIBLE + r"…"` 串出來
    的，在函式裡串接完全一樣。而且它同步得再好也還是錯的——抄本裡那個
    `function visible` 做的其實是 `onScreen` 的事，名字卻叫 `visible`；一旦跟
    真的常數串在同一段 script 裡，**JS 的函式宣告提升會讓後宣告的抄本覆蓋掉
    常數自己的版本**，連 `onScreen` 內部呼叫到的都變成抄本。這次兩者剛好等價，
    所以沒有出事，但那是巧合。

    所以現在守的是「不准再抄第二份」，而不是「抄本要同步」。
    """
    print("test_only_one_copy_of_the_visibility_predicate_exists")
    source = Path(ws.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 掃**字串常數**而不是原始碼文字：註解與 docstring 本來就要講到
    # `function visible`（解釋這條規則本身），拿子字串掃會被自己的說明絆倒
    # ——CLAUDE.md 說的「會亂叫的守門就是會被關掉的守門」。
    copies = [node for node in ast.walk(tree)
              if isinstance(node, ast.Constant) and isinstance(node.value, str)
              and ("function visible(" in node.value
                   or "function onScreen(" in node.value)]
    assert len(copies) == 1, (
        f"可見性判準有 {len(copies)} 份（行號 "
        f"{[n.lineno for n in copies]}）——只准有 `_JS_VISIBLE` 那一份。"
        "同名的 `function visible` 會因為 JS 函式宣告提升覆蓋掉常數自己的版本，"
        "連 `onScreen` 內部呼叫到的都會變成抄本。內嵌 JS 一樣串得到常數，"
        "直接 `_JS_VISIBLE + r\"…\"`。")

    # 正面對照組：那唯一一份必須真的是 `_JS_VISIBLE` 的值（不然「只有一份」
    # 可能是因為連本尊都被改成別的形狀，掃描器什麼都沒看到）。
    assert copies[0].value == ws._JS_VISIBLE, (
        "找到的那一份不是 `_JS_VISIBLE` 的值——掃描器可能已經看不到本尊了")
    print("  PASS\n")


# ---------------------------------------------------------------------------
# 事件**欄位**的兩邊契約：webrunner 送出的 / bot 讀取的
#
# 上面那組守的是**型別**：沒接住的事件是靜默的 no-op。欄位這一層更安靜——型別對
# 得上、handler 照跑、訊息照發，只是內容變成 `None`。改個欄位名（例如
# `character_start` 的 `name` 改叫 `character`）不會讓任何測試變色，頻道上會出現
# 一則「角色 None 開始了」，而沒有任何錯誤。
#
# 這兩個事實一起讓它更值得守：欄位名**本來就不一致**（`character_start` /
# `character_done` 用 `name`，`quota_blocked` / `chrome_restart` /
# `consecutive_failures` / `schedule_rest` 用 `character`），而 CLAUDE.md 的模組
# 邊界規定兩側只能靠磁碟溝通——也就是說沒有任何型別檢查會幫忙。
# ---------------------------------------------------------------------------

_BOT_EVENT_READERS = ("ev", "event", "rec")


@functools.lru_cache(maxsize=None)
def _emitted_event_fields() -> tuple[dict, frozenset]:
    """AST 掃 `emit_event("型別", 欄位=…)`，回 ({型別: 欄位集合}, 用 ** 展開的型別)。

    `**payload` 展開的沒辦法靜態看出欄位，所以另外回報——那是掃描器的盲點，要讓它
    看得見而不是假裝沒有。
    """
    fields: dict[str, set[str]] = {}
    starred: set[str] = set()
    pkg = Path(ws.__file__).resolve().parent
    tests = Path(__file__).resolve().parent     # `conftest.py`：搬家前在範圍內，照舊
    for path in sorted(pkg.glob("*.py")) + sorted(tests.glob("*.py")):
        if path.name.startswith(("test_", "_test_")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None))
            if name != "emit_event":
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant)
                    and isinstance(first.value, str)):
                continue
            bucket = fields.setdefault(first.value, set())
            for kw in node.keywords:
                if kw.arg is None:
                    starred.add(first.value)
                else:
                    bucket.add(kw.arg)
    return fields, frozenset(starred)


@functools.lru_cache(maxsize=None)
def _bot_read_event_fields() -> dict:
    """bot 的事件派發裡，每個 `et == "型別"` 分支讀了哪些欄位。"""
    bot = Path(ws.__file__).resolve().parent / "discord_bot.py"
    tree = ast.parse(bot.read_text(encoding="utf-8"), str(bot))
    read: dict[str, set[str]] = {}

    def branch_type(test):
        for node in ast.walk(test):
            if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                    and node.left.id in ("et", "t")):
                for comparator in node.comparators:
                    if (isinstance(comparator, ast.Constant)
                            and isinstance(comparator.value, str)):
                        return comparator.value
        return None

    def walk(node, current):
        if isinstance(node, ast.If):
            found = branch_type(node.test)
            inner = found or current
            for stmt in node.body:
                walk(stmt, inner)
            for stmt in node.orelse:
                walk(stmt, current)
            return
        if (current and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in _BOT_EVENT_READERS
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            read.setdefault(current, set()).add(node.args[0].value)
        for child in ast.iter_child_nodes(node):
            walk(child, current)

    walk(tree, None)
    return read


def test_every_event_field_the_bot_reads_is_actually_emitted():
    print("test_every_event_field_the_bot_reads_is_actually_emitted")
    emitted, starred = _emitted_event_fields()
    gaps = []
    for event_type, names in sorted(_bot_read_event_fields().items()):
        if event_type in starred or event_type not in emitted:
            continue          # ** 展開的看不出來；沒人發的型別由上面那組守
        missing = sorted(names - emitted[event_type] - {"ts", "type"})
        if missing:
            gaps.append(f"{event_type}: bot 讀 {missing}，webrunner 只送 "
                        f"{sorted(emitted[event_type])}")
    assert not gaps, (
        "事件欄位對不上——這是**靜默**的：型別對得上、handler 照跑、訊息照發，"
        f"只是內容變成 None。{gaps}")
    print("  PASS\n")


def test_the_event_field_scanners_actually_see_both_sides():
    print("test_the_event_field_scanners_actually_see_both_sides")
    # 兩邊任何一邊掃成空的，上面那支就會**安靜地全過**。bot 的事件派發是一長串
    # `elif et == "…"`，重構一次就可能讓 `_bot_read_event_fields` 什麼都認不出來。
    emitted, _ = _emitted_event_fields()
    read = _bot_read_event_fields()
    assert len(emitted) >= 15, f"送出端只掃到 {len(emitted)} 個型別，掃描八成壞了"
    assert sum(len(v) for v in emitted.values()) >= 30, (
        f"送出端只掃到 {sum(len(v) for v in emitted.values())} 個欄位")
    assert len(read) >= 8, f"bot 端只掃到 {len(read)} 個型別的欄位讀取，掃描八成壞了"
    # 具體釘幾個一定要看得到的，光看數量還不夠。
    for event_type, field in (("character_start", "name"),
                              ("character_done", "saved"),
                              ("quota_blocked", "character")):
        assert field in emitted.get(event_type, set()), (
            f"送出端掃不到 {event_type}.{field}——欄位改名了，還是掃描壞了？")
    assert "character_done" in read, (
        "bot 端掃不到 character_done 的欄位讀取——事件派發被重構過了？"
        "重構沒關係，但 `_bot_read_event_fields` 要跟著改，否則這組守門靜音。")
    print("  PASS\n")


def test_no_event_is_emitted_through_an_unscannable_payload():
    print("test_no_event_is_emitted_through_an_unscannable_payload")
    # `emit_event("x", **payload)` 靜態看不出欄位，等於在契約上開一個洞。目前一個
    # 都沒有；要加的話得先想清楚怎麼守，不是預設允許。
    _, starred = _emitted_event_fields()
    assert not starred, (
        f"這些事件用 `**` 展開 payload：{sorted(starred)}。"
        "欄位就再也靜態掃不出來，bot 那一側讀錯名字不會有人發現。")
    print("  PASS\n")


# ---------------------------------------------------------------------------
# Cookie 同意橫幅：字面要對得上實機，沒有橫幅時要早退
#
# 2026-08-30 用全新暫時 profile 實測站方首頁：橫幅上只有 `Accept All` 與
# `Reject Non-Essential` 兩顆，**沒有 `Reject All`**。原本主要規則寫死比對
# `Reject All`，從來沒命中過；橫幅是靠寬鬆退路（含 `Reject`）關掉的。同一次
# 實測也量到：沒有橫幅時整整空等 15.2 秒，而正式 profile 是常駐的、同意早就
# 存過，所以「沒有橫幅」才是常態——正式 log 16/16 次都是這一種。
# ---------------------------------------------------------------------------


class _ScriptedConsentPort:
    """只回一個固定 payload 的 port，用來量 `reject_cookies` 的控制流。"""

    TRANSPORT_ERRORS = ()

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.script_args = []

    def execute_script(self, script, *args):
        self.calls += 1
        self.script_args.append(args)
        return dict(self.reply)


def test_the_cookie_reject_label_matches_what_the_site_actually_shows():
    print("test_the_cookie_reject_label_matches_what_the_site_actually_shows")
    labels = ws.COOKIE_REJECT_LABELS
    assert "Reject Non-Essential" in labels, (
        "`COOKIE_REJECT_LABELS` 裡沒有實測到的字面 `Reject Non-Essential`"
        f"（目前是 {labels}）。少了它，主要規則就又只剩永遠不命中的 "
        "`Reject All`，同意橫幅只能靠寬鬆退路關掉。")
    assert tuple(labels)[0] == "Reject Non-Essential", (
        f"實測存在的字面要排在前面（目前是 {labels}）：候選是**依序**比對的，"
        "把只存在於歷史的字面放第一個，等於每次都先白跑一輪。")
    print("  PASS\n")


def test_no_consent_prompt_returns_fast_instead_of_burning_the_timeout():
    print("test_no_consent_prompt_returns_fast_instead_of_burning_the_timeout")
    port = _ScriptedConsentPort(
        {"clicked": None, "present": False, "ready": True})
    buffer = io.StringIO()
    # `monotonic` 而不是 `time()`：這裡量的是「過了多久」，而牆鐘會被 NTP 校時／
    # 改時區／虛擬機還原往前後撥。下面那支的失敗方向更要緊——它斷言的是
    # `took >= 1.0`，時鐘往前跳會讓它**假綠**，也就是早退條件放寬了卻沒人發現。
    start = time.monotonic()
    with contextlib.redirect_stdout(buffer):
        result = ws.reject_cookies(port, timeout=6.0, settle=0.2)
    took = time.monotonic() - start
    assert result is False, result
    assert took < 2.0, (
        f"沒有同意提示卻花了 {took:.1f} 秒——早退失效了。實測正式環境每次 setup "
        "會呼叫兩次（登入頁 5 秒、產圖頁 15 秒），退化回去就是每個角色白花 20 秒。")
    assert "no cookie consent prompt" in buffer.getvalue(), (
        "早退要印自己的訊息，不能跟等滿 timeout 的那則混在一起——"
        f"看 log 的人得分得出是哪一種：{buffer.getvalue()!r}")
    print("  PASS\n")


def test_a_consent_prompt_that_is_not_clickable_yet_still_waits():
    print("test_a_consent_prompt_that_is_not_clickable_yet_still_waits")
    # 橫幅在、但這一刻點不到（動畫中／還沒綁好事件）。這正是 timeout 存在的
    # 理由，早退**不可以**在這種情況觸發。
    port = _ScriptedConsentPort(
        {"clicked": None, "present": True, "ready": True})
    buffer = io.StringIO()
    start = time.monotonic()
    with contextlib.redirect_stdout(buffer):
        result = ws.reject_cookies(port, timeout=1.2, settle=0.1)
    took = time.monotonic() - start
    assert result is False, result
    assert took >= 1.0, (
        f"橫幅還在卻只等了 {took:.1f} 秒就放棄——早退條件太寬，"
        "會在橫幅慢一步出現時直接跳過，然後它整頁蓋著讓後面的點選隨機失敗。")
    assert "cookie banner not found" in buffer.getvalue(), buffer.getvalue()
    print("  PASS\n")


def test_the_consent_scan_is_anchored_so_the_footer_link_does_not_defeat_it():
    print("test_the_consent_scan_is_anchored_so_the_footer_link_does_not_defeat_it")
    match = re.search(r"const ACTION = /(.+?)/i;", ws._COOKIE_CONSENT_JS)
    assert match, ("`_COOKIE_CONSENT_JS` 裡找不到 `const ACTION = /…/i;`——"
                   "改寫過的話這支測試要跟著改，否則它會安靜地失去意義。")
    action = re.compile(match.group(1), re.IGNORECASE)
    # 站方頁尾**常駐**一顆 `Manage Cookie Preferences`（2026-08-30 實測：橫幅
    # 關掉之後它仍然在）。少了 `^` 錨定它永遠符合，`present` 就永遠是 True，
    # 早退條件永遠不成立——最佳化靜默失效，而測試看起來還是綠的。
    assert not action.search("Manage Cookie Preferences"), (
        "行動型判斷把常駐的 `Manage Cookie Preferences` 也算進去了："
        "`present` 會永遠是 True，早退條件永遠不成立。")
    for label in ("Reject Non-Essential", "Accept All"):
        assert action.search(label), (
            f"實測存在的同意按鈕 {label!r} 判不出來——`present` 會漏報，"
            "橫幅慢一步出現時會被早退跳過。")
    # `\b` 必須是真的字界。這條看起來像在測正規表示式的常識，實際上是在守一個
    # 踩過的坑：`\b` 寫進非 raw 的 Python 字串會變成**退格字元**（0x08），而
    # Python 不會警告（`\b` 是合法跳脫），JS 那一側也照跑不誤——只是再也不會
    # 符合任何東西。
    assert not action.search("Rejected items"), (
        r"`\b` 失效了（很可能被寫成退格字元 0x08）："
        "字界不成立的話 `Rejected items` 這種字面也會被當成同意按鈕。")
    assert chr(8) not in ws._COOKIE_CONSENT_JS, (
        r"`_COOKIE_CONSENT_JS` 裡有退格字元——`\b` 被非 raw 字串吃掉了。")
    print("  PASS\n")


def test_the_known_label_is_tried_before_the_loose_fallback():
    """已知字面要排在寬鬆退路前面，而且候選常數真的有被傳進 JS。

    這支守得到「完全比對整段被刪掉」與「候選沒傳進去」（兩種都實際突變驗過）。
    **守不到**的是把那段用 `false &&` 之類短路掉——要靜態看穿 JS 的語義才行。
    真要擋那種人為破壞，得在真瀏覽器裡放兩顆按鈕（一顆完全相符、一顆只是含
    `Reject` 且排在前面）跑一次，那屬於 `verify_*` 那一類的實機驗證。
    """
    print("test_the_known_label_is_tried_before_the_loose_fallback")
    code = ws._COOKIE_CONSENT_JS
    exact = code.index("labelOf(b) === want")
    loose = code.index("/reject/i.test(t)")
    assert exact < loose, (
        "寬鬆退路跑到已知字面前面了。順序有意義：完全比對命中的是**這一顆**，"
        "含 `Reject` 的第一顆則是頁面順序決定的，兩者不保證同一個元素。")
    port = _ScriptedConsentPort({"clicked": "Reject Non-Essential"})
    saved_pause = ws.human_pause
    ws.human_pause = lambda *_a, **_k: None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            assert ws.reject_cookies(port, timeout=2.0) is True
    finally:
        ws.human_pause = saved_pause
    assert port.script_args and port.script_args[0], (
        "`reject_cookies` 沒有把候選字面當參數傳進 JS——"
        "`COOKIE_REJECT_LABELS` 就成了沒人讀的裝飾。")
    assert list(port.script_args[0][0]) == list(ws.COOKIE_REJECT_LABELS), (
        f"傳進 JS 的候選跟常數對不上：{port.script_args[0][0]}")
    print("  PASS\n")


# ---------------------------------------------------------------------------
# critical_error 的診斷內容（2026-09-09）
# ---------------------------------------------------------------------------
# 舊版是 `traceback.format_exc()[-1500:]`——只留尾段。實測全部 7 筆
# `critical_error`：例外來自我們自己的程式時尾段有 2–5 個我們的 frame；例外來自
# 函式庫深處時（09-07 三次 `MaxRetryError`）是 **0 個我們的、4 個 urllib3 的**。
# 恰好在最需要它的那一類故障上，我們的 frame 一個都不剩。
#
# 而且「改成留頭段」也修不好：實測我們的 frame 落在 7,452 字裡的 2564–4025，
# **頭尾都不是**（例外鏈先印最內層的成因）。所以是按來源篩，不是按位置切。

_OUR_DIR = str(Path(ws.__file__).resolve().parent)
_VENV_DIR = str(Path(ws.__file__).resolve().parent.parent
                / ".venv" / "Lib" / "site-packages")


def _fake_chained_traceback(foreign_frames: int = 12) -> str:
    """照 09-07 那三次的形狀造一份：兩段第三方成因 → 我們的 frame → 更多第三方。"""
    def frame(path, line, func, echo):
        return (f'  File "{path}", line {line}, in {func}' + chr(10)
                + f"    {echo}")

    lines = ["Traceback (most recent call last):"]
    for i in range(foreign_frames):
        lines.append(frame(_VENV_DIR + chr(92) + "urllib3" + chr(92)
                           + "connectionpool.py", 700 + i, f"_pool_{i}",
                           "raise NewConnectionError(conn, message)"))
    lines.append("ConnectionRefusedError: [WinError 10061] refused")
    lines.append("")
    lines.append("The above exception was the direct cause of the following "
                 "exception:")
    lines.append("")
    lines.append("Traceback (most recent call last):")
    lines.append(frame(_OUR_DIR + chr(92) + "webrunner_novelai.py", 1466,
                       "main", "return ws.run_batch(port, email, password)"))
    lines.append(frame(_OUR_DIR + chr(92) + "_webrunner_shared.py", 4936,
                       "run_batch", "saved = generate_loop(port, name, cfg)"))
    lines.append(frame(_OUR_DIR + chr(92) + "_webrunner_shared.py", 3738,
                       "generate_one_image", "err = get_generation_error(port)"))
    for i in range(foreign_frames):
        lines.append(frame(_VENV_DIR + chr(92) + "selenium" + chr(92)
                           + "webdriver.py", 500 + i, f"_sel_{i}",
                           "response = self._request(method, url, body=body)"))
    lines.append("urllib3.exceptions.MaxRetryError: HTTPConnectionPool("
                 "host='localhost', port=63237): Max retries exceeded")
    return chr(10).join(lines)


def test_the_traceback_excerpt_keeps_our_frames_when_the_raise_is_deep():
    print("test_the_traceback_excerpt_keeps_our_frames_when_the_raise_is_deep")
    raw = _fake_chained_traceback()
    out = ws._traceback_excerpt(raw)
    for func in ("main", "run_batch", "generate_one_image"):
        assert f"in {func}" in out, (
            f"我們自己的 frame `{func}` 被丟掉了——這正是舊版 `[-1500:]` 的缺陷。"
            f"實際輸出：{out!r}")
    # 例外鏈的骨架與最終死因是判讀的起點，不得被折疊掉。
    assert "The above exception was the direct cause" in out, out
    assert "MaxRetryError" in out and "ConnectionRefusedError" in out, out
    # 第三方那一長串要折疊，而且要說出折了幾個——沒有數字的話「折疊」與「本來就
    # 只有一個」分不出來。
    assert "omitted 11 third-party frames" in out, (
        "第三方 frame 沒有被折疊，或折疊時沒有說出省略了幾個。"
        f"實際輸出：{out!r}")
    assert len(out) < len(raw) // 2, (
        f"完全沒有收斂：{len(raw)} -> {len(out)}")
    print("  PASS" + chr(10))


def test_a_venv_frame_under_the_project_root_is_not_mistaken_for_ours():
    print("test_a_venv_frame_under_the_project_root_is_not_mistaken_for_ours")
    # `.venv/` 就在 `PROJECT_ROOT` 底下，所以拿專案根去比對的話，每一個
    # site-packages 的 frame 都會被認成「我們的」，摘要完全失去意義——而且**不會有
    # 任何症狀**，只是事件裡的 traceback 又變回一整片函式庫雜訊。
    raw = _fake_chained_traceback(foreign_frames=20)
    out = ws._traceback_excerpt(raw)
    kept_foreign = out.count("site-packages")
    assert kept_foreign == 2, (
        "每一段連續的第三方 frame 只該留第一個（交棒出去的那一格），"
        f"實際留了 {kept_foreign} 個。判準用的是**套件**目錄不是專案根嗎？"
        f"輸出：{out!r}")
    print("  PASS" + chr(10))


def test_the_traceback_excerpt_is_capped_and_says_how_much_it_dropped():
    print("test_the_traceback_excerpt_is_capped_and_says_how_much_it_dropped")
    # 保險絲：遞迴爆炸（`RecursionError` 上千個 frame）不得把事件檔灌爆。
    # 全部都是我們的 frame，所以折疊機制救不了它——只剩上限這一道。
    ours = (f'  File "{_OUR_DIR}{chr(92)}_webrunner_shared.py", line 1, in loop'
            + chr(10) + "    keep_going()")
    raw = ("Traceback (most recent call last):" + chr(10)
           + (chr(10).join([ours] * 400)) + chr(10)
           + "RecursionError: maximum recursion depth exceeded")
    out = ws._traceback_excerpt(raw)
    assert len(out) <= ws._TRACEBACK_BUDGET + 80, len(out)
    assert "omitted in the middle" in out, f"截斷了卻沒說：{out[-200:]!r}"
    assert out.startswith("Traceback (most recent call last):"), out[:80]
    assert out.rstrip().endswith("RecursionError: maximum recursion depth "
                                 "exceeded"), (
        "尾段沒留住——traceback 的**最後一行**就是例外型別與訊息，"
        f"那是最重要的一行。實際結尾：{out[-120:]!r}")
    print("  PASS" + chr(10))


def test_a_one_line_error_keeps_the_caused_by_tail():
    print("test_a_one_line_error_keeps_the_caused_by_tail")
    # `MaxRetryError` 的關鍵資訊在**尾巴**：`(Caused by NewConnectionError(…
    # [WinError 10061] …))` 是唯一能直接回答「chromedriver.exe 是不是已經結束了」
    # 的證據。180 字剛好會把它切掉，所以終結性的 raise 用 `_long_error`。
    message = ("HTTPConnectionPool(host='localhost', port=63237): Max retries "
               "exceeded with url: /session/b6c23d69fb5763719c2aee0b289598c3"
               "/execute/sync (Caused by NewConnectionError(\"HTTPConnection("
               "host='localhost', port=63237): Failed to establish a new "
               "connection: [WinError 10061] refused\"))")
    error = RuntimeError(message)
    assert "WinError 10061" not in ws._short_error(error), (
        "180 字的那一版竟然放得下——測資太短了，改不改都測不出差別")
    assert "WinError 10061" in ws._long_error(error), (
        "`_long_error` 也把 `Caused by …` 切掉了，那它就沒有存在的理由。"
        f"實際：{ws._long_error(error)!r}")
    # 兩者都要把 chromedriver 的 C++ Stacktrace 砍掉、壓成一行。
    noisy = RuntimeError("Message: invalid session id" + chr(10)
                         + "Stacktrace:" + chr(10) + chr(9)
                         + "chromedriver!GetHandleVerifier [0x7ff7f691]")
    for text in (ws._short_error(noisy), ws._long_error(noisy)):
        assert "Stacktrace" not in text and chr(10) not in text, text
    print("  PASS" + chr(10))


def test_a_critical_error_carries_a_usable_traceback_and_a_drift_verdict():
    print("test_a_critical_error_carries_a_usable_traceback_and_a_drift_verdict")

    # 要重現的是**正式環境那個形狀**：例外從第三方深處拋出來，我們自己的 frame
    # 在上面。用 `compile(..., <假路徑>, ...)` 讓那些 frame 的檔名指向
    # site-packages——這是在本行程裡唯一造得出「第三方 frame」的辦法，而少了它，
    # traceback 短到連舊版的 `[-1500:]` 都放得下，測試就驗不出差別了。
    foreign = str(Path(ws.__file__).resolve().parent.parent
                  / ".venv" / "Lib" / "site-packages" / "urllib3"
                  / "connectionpool.py")
    namespace: dict = {}
    exec(compile("def deep(n, hit):" + chr(10)                # nosec B102
                 + "    if n:" + chr(10)
                 + "        return deep(n - 1, hit)" + chr(10)
                 + "    return hit()" + chr(10),
                 foreign, "exec"), namespace)

    def hit():
        # 訊息刻意又長又髒：chromedriver 每個例外後面都附十幾行 C++ Stacktrace，
        # 而事件的 message 欄位以前是裸的 `str(error)`——沒有長度上限也不清那一段。
        raise RuntimeError(
            "Message: chrome not reachable" + chr(10) + "Stacktrace:" + chr(10)
            + chr(9) + "chromedriver!GetHandleVerifier [0x7ff7b781e035+5745]"
            + chr(10) + chr(9) + ("chromedriver!(No symbol) [0x7ff7b7746a90]"
                                  + chr(10) + chr(9)) * 20)

    def boom(*_a, **_k):
        return namespace["deep"](25, hit)

    # 漂移判定用替身：這裡要驗的是**值有沒有傳過去**，不是指紋模組本身（那是
    # `test_code_fingerprint.py` 的事）。用真的會取到 None（測試行程沒取過快照），
    # 而 None 是三個合法值之一——那樣寫的話「一律回 None」的退化版也會全綠。
    saved_report = ws._code_fingerprint.drift_report
    try:
        ws._code_fingerprint.drift_report = lambda: {
            "drifted": True, "why": "1 changed", "at_start": "abc",
            "now": "def", "changed": ["x.py"], "added": [], "removed": []}
        with _RunBatchHarness() as h:
            h.write_queue("todo_prompt.md", ["P1"])
            h.write_queue("todo_character1.md", ["a"])
            h.patch("generate_loop", boom)
            try:
                _run_batch(FakeBrowserPort())
            except Exception:  # noqa: BLE001 - 這裡就是要讓它炸出來
                pass
            crit = h.events_of("critical_error")
    finally:
        ws._code_fingerprint.drift_report = saved_report

    assert len(crit) == 1, crit
    event = crit[0]
    tb = event.get("traceback") or ""
    assert "run_batch" in tb, (
        "事件裡的 traceback 沒有我們自己的 frame——那正是這個改動要修的東西。"
        "（舊版 `format_exc()[-1500:]` 在這個形狀下留下的全是第三方 frame。）"
        f"實際：{tb!r}")
    assert "_webrunner_shared.py" in tb, tb
    assert "omitted" in tb and "third-party frames" in tb, (
        f"第三方那一長串沒有被折疊：{tb!r}")
    # message 要壓成一行、而且不得帶 chromedriver 的 C++ Stacktrace。
    message = event.get("message") or ""
    assert message and chr(10) not in message, f"message 不是一行：{message!r}"
    assert "Stacktrace" not in message, message
    assert len(message) <= ws._LONG_ERROR_LIMIT + 80, len(message)
    # 漂移旗標：三態，而且鍵一定要在——沒有它的話讀事件檔的人無從判斷 traceback
    # 印出來的**原始碼文字**可不可信（`linecache` 是列印當下才讀磁碟的）。
    assert "code_drift" in event, (
        "`critical_error` 沒帶 `code_drift`。行號一律可信，原始碼文字只在"
        "沒漂移時可信——沒有這個欄位，讀 events.ndjson 的人只能用猜的。")
    assert event["code_drift"] is True, (
        "漂移判定沒有被傳進事件裡。注意「三態之一」那種鬆斷言連「寫死成 None」"
        f"都擋不住，所以這裡釘的是實際的值。實際：{event['code_drift']!r}")
    print("  PASS" + chr(10))


def test_the_drift_flag_never_raises_even_when_the_fingerprint_module_breaks():
    print("test_the_drift_flag_never_raises_even_when_the_fingerprint_module_breaks")
    # 這個旗標跑在**死亡路徑**上。它自己再丟一個例外的話，會把原本要送出去的
    # `critical_error` 整個換掉——使用者會收到一個與真正死因完全無關的錯誤。
    saved = ws._code_fingerprint.drift_report
    try:
        ws._code_fingerprint.drift_report = lambda: (_ for _ in ()).throw(
            RuntimeError("fingerprint module exploded"))
        assert ws.code_drift_flag() is None, (
            "探測自己壞掉時要回「不知道」，不是往上丟")
    finally:
        ws._code_fingerprint.drift_report = saved
    print("  PASS" + chr(10))


# ---------------------------------------------------------------------------
# 關對話框的記錄量：穩態下每次被擋一行（2026-09-09）
# ---------------------------------------------------------------------------
# 分層關閉上線後（09-07 17:17 起）實測 25 個被擋區塊 **0 次關得掉**，每個區塊固定
# 產出 2 行 `could not dismiss …（第 N/4 層）`（合計 50 行）外加整份控制項清單。
# 完全可預測、資訊量為零，而且會隨 `max_rounds` 等比例長大——正是 `discord_bot.log`
# 那 96% 都是同一句 `rpc apply -> ok` 的開頭形狀。要收的是**敘述**不是迴圈。

_SITE_INVENTORY = {
    "w": 420, "h": 322, "total": 5,
    "controls": [
        {"tag": "button", "role": "", "aria": "", "title": "", "text": "",
         "cls": "sc-9f2a", "icon": 0, "dx": 369, "dy": 21, "w": 32, "h": 32,
         "cursor": "pointer", "corner": 21},
    ],
}


def _stuck_port(inventory=None):
    """站方那個關不掉的兩層對話框：按得到關閉鈕，但畫面一個字都不會變。"""
    return _ScriptedDialogPort(
        dismiss_action="clicked-corner:button@369,21 32x32",
        clears_when=None,
        inventory=inventory if inventory is not None else _SITE_INVENTORY)


def _dismiss_lines(port, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(
            io.StringIO()):
        ok = ws.dismiss_blocking_dialog(port, **kwargs)
    return ok, [ln for ln in buf.getvalue().splitlines() if ln.strip()]


def test_the_first_time_a_dialog_shape_appears_it_is_logged_in_full():
    print("test_the_first_time_a_dialog_shape_appears_it_is_logged_in_full")
    saved = _no_pause()
    try:
        ok, lines = _dismiss_lines(_stuck_port())
    finally:
        ws.human_pause = saved
    text = chr(10).join(lines)
    assert ok is False
    assert "could not dismiss" in text, text
    # 逐層的按法與結果、收手的理由、控制項清單——第一次都要在。安靜是為了穩態，
    # 不是為了讓第一次故障也查不到。
    assert "layer 1 via" in text, f"沒有逐層經過：{text!r}"
    assert "clicked-corner:button@369,21" in text, f"沒說按了哪一顆：{text!r}"
    assert "did not change the screen" in text, f"沒說為什麼收手：{text!r}"
    assert "420x322" in text, f"沒有控制項清單：{text!r}"
    print("  PASS" + chr(10))


def test_a_repeated_dialog_shape_shrinks_to_a_single_line():
    print("test_a_repeated_dialog_shape_shrinks_to_a_single_line")
    saved = _no_pause()
    try:
        _dismiss_lines(_stuck_port())                 # 第一次：完整
        ok, lines = _dismiss_lines(_stuck_port())     # 第二次：一行
    finally:
        ws.human_pause = saved
    assert ok is False
    assert len(lines) == 1, (
        "同一個形狀第二次還是印了好幾行。穩態下每次被擋只該留一行——"
        f"實際 {len(lines)} 行：{lines!r}")
    assert "could not dismiss" in lines[0], (
        "摘要行沒有帶 `could not dismiss`：歷史統計是照這個字串數的，"
        f"而且「關掉了」與「關不掉」必須分得出來。實際：{lines[0]!r}")
    assert "occurrence 2" in lines[0], (
        f"摘要行沒說這是第幾次，讀 log 的人看不出這是穩態：{lines[0]!r}")
    print("  PASS" + chr(10))


def test_the_steady_state_log_volume_does_not_grow_with_max_rounds():
    print("test_the_steady_state_log_volume_does_not_grow_with_max_rounds")
    # 這是這個改動要達成的性質本身。舊版是「每輪一行」，所以把 `max_rounds` 從 4
    # 調到 8 就等於把穩態的記錄量加倍。
    saved = _no_pause()
    try:
        for rounds in (2, 4, 8):
            ws._DISMISS_LOG_SEEN.clear()
            # 每一層文字都不一樣（站方一直疊新的）→ 一定用完整組 round。
            def fresh():
                return _ScriptedDialogPort(
                    dismiss_action="clicked-corner:button@1,1 32x32",
                    layers=[f"層 {i}" for i in range(50)],
                    inventory=_SITE_INVENTORY)
            first_port = fresh()
            _, first = _dismiss_lines(first_port, max_rounds=rounds)
            assert first_port.dismiss_calls == rounds, (
                f"嘗試次數被改掉了：max_rounds={rounds} 卻只按了 "
                f"{first_port.dismiss_calls} 輪。要收的是敘述，不是迴圈。")
            _, second = _dismiss_lines(fresh(), max_rounds=rounds)
            assert len(second) == 1, (
                f"max_rounds={rounds} 的穩態記錄是 {len(second)} 行：{second!r}")
            assert len(first) >= rounds, (
                f"第一次反而沒有完整記錄：max_rounds={rounds} -> {first!r}")
    finally:
        ws.human_pause = saved
    print("  PASS" + chr(10))


def test_a_changed_dialog_shape_is_logged_in_full_again():
    print("test_a_changed_dialog_shape_is_logged_in_full_again")
    # **這條是載重的。** 09-07 那次「其實關掉了第一層、露出第二層」的突破，靠的不是
    # 任何一則訊息，而是控制項清單裡的**對話框尺寸**變了（856x917 → 420x322）。
    # 把清單排除在形狀之外的話，站方改版之後我們只會繼續看到「與先前相同」。
    bigger = dict(_SITE_INVENTORY, w=856, h=917)
    saved = _no_pause()
    try:
        _dismiss_lines(_stuck_port())
        _, again = _dismiss_lines(_stuck_port())
        _, changed = _dismiss_lines(_stuck_port(inventory=bigger))
    finally:
        ws.human_pause = saved
    assert len(again) == 1, again
    assert len(changed) > 1, (
        "對話框換了一個尺寸，記錄卻還是那句「與先前相同」。"
        f"實際：{changed!r}")
    assert "856x917" in chr(10).join(changed), changed
    print("  PASS" + chr(10))


def test_a_successful_dismiss_stays_distinguishable_in_both_forms():
    print("test_a_successful_dismiss_stays_distinguishable_in_both_forms")
    # 收斂敘述不得把「關掉了」與「關不掉」混成同一句——那是這個函式唯一的結論。
    saved = _no_pause()
    try:
        def fresh():
            return _ScriptedDialogPort(
                dismiss_action="clicked:Cancel", clears_when=1)
        ok_first, first = _dismiss_lines(fresh())
        ok_again, again = _dismiss_lines(fresh())
    finally:
        ws.human_pause = saved
    assert ok_first is True and ok_again is True
    for lines in (first, again):
        text = chr(10).join(lines)
        assert "dismissed" in text and "could not dismiss" not in text, text
    assert len(again) == 1, again
    print("  PASS" + chr(10))


class _TransportStallPort(_ScriptedDialogPort):
    """挑選用的 JS 打不通（chromedriver 卡頓，**不是** session 死掉）。

    訊息刻意避開 `_SESSION_GONE_MESSAGE_MARKERS`：帶那些字的話
    `_note_transport_error` 會升級成 `BrowserGoneError` 往上丟，走的就不是這裡要
    測的「回 False 並且什麼結論都不印」那條路了。
    """

    def execute_script(self, script, *args):
        if script is ws._DISMISS_DIALOG_JS:
            raise RuntimeError("Read timed out. (read timeout=120)")
        return super().execute_script(script, *args)


# `(名稱, 造 port, dismiss_blocking_dialog 的 kwargs, 這條路會不會印出結論)`
#
# 最後一欄不是裝飾品：**「不出聲」本身就是契約的一部分**。本來就沒有對話框那條
# 路刻意一個字都不印（穩態下每張圖都會問一次，出聲等於洗版），而 transport 卡頓
# 那條路在 `_report_dismiss_outcome` **之前**就 return，只留一行 `[warn]`。兩條都
# 回傳得出結果卻沒有結論行，所以「回傳值 ↔ 用詞」的比對必須先問「這一輪有沒有
# 說話」，否則會拿 False 去跟 True 比而誤報。
#
# ⚠️ 順帶一個對統計的修正：transport 那條路回 False 卻**不留結論行**，所以照
# `could not dismiss` 去數關閉失敗次數在原理上會低估。實測（`webrunner.log`
# 08-24～09-09 全檔）那條路**一次都沒發生**（`transport error` 0 行），而結論行
# 共 309 筆（153 `dismissed` ＋ 156 `could not dismiss`）——所以那一窗的
# 「成功 0 次」是完整的，不是被這條silent path 削掉的。下次重數要先確認這一點。
_DISMISS_VERDICT_CASES = (
    ("closed：第一輪就關乾淨",
     lambda: _ScriptedDialogPort(dismiss_action="clicked:Cancel", clears_when=1),
     {}, True),
    ("closed：關到 JS 挑不出東西（底下沒有下一層了）",
     lambda: _ScriptedDialogPort(dismiss_action="clicked:Cancel",
                                 layers=["只有這一層"]),
     {}, True),
    ("same：按得到但畫面一個字都沒變",
     _stuck_port, {}, True),
    ("escape-spent：一顆都挑不中，Escape 送過一次就收手",
     lambda: _ScriptedDialogPort(dismiss_action="escape", clears_when=None),
     {}, True),
    ("rounds：每一層都不一樣，用完 max_rounds",
     lambda: _ScriptedDialogPort(
         dismiss_action="clicked-corner:button@1,1 32x32",
         layers=[f"第 {i} 層" for i in range(50)]),
     {"max_rounds": 3}, True),
    ("本來就沒有對話框 —— 刻意不印結論",
     FakeBrowserPort, {}, False),
    ("transport 卡頓 —— 刻意不印結論，只留一行 [warn]",
     _TransportStallPort, {}, False),
)

# 這張表必須真的踩到每一個「會印出結論」的 stop_reason。少一個就代表某個情境被
# 別的情境吸收掉了——而那是**安靜**的：`_ScriptedDialogPort(layers=[])` 實測會因為
# `list(layers) if layers else None` 而退化成 `layers=None`，於是那個案例其實在測
# 別的東西，表面上完全看不出來（§8.8「空的選取看起來像乾淨的結果」的同一個坑）。
_DISMISS_REPORTED_REASONS = {"closed", "same", "escape-spent", "rounds"}


def test_the_dismiss_verdict_word_is_bound_to_the_return_value():
    """log 印的字必須跟回傳值是**同一個判斷**——這是實際壞過的那一條。

    2026-09-03 之前 `dismiss_blocking_dialog` 在驗證之前就無條件印
    `[quota] dismissed blocking dialog via 'escape'`。回傳值一直是對的，錯的只有
    敘述。`webrunner.log`（08-24 → 09-09 全檔）量到的樣子：`dismissed` 153 筆，
    而**153/153 的下一行都是** `dialog would not close; reloading the page`，零例外。
    照 log 統計成功率會得到 153/266 = 58%，真實的成功率是 **0**（309 次結論行裡
    零次成功，而且 `transport error` 0 行，所以沒有被 silent path 削掉的部分）。

    今天兩邊都由 `stop_reason == "closed"` 導出，但那是**兩份**導出（一份在
    `dismiss_blocking_dialog` 的 return，一份在 `_report_dismiss_outcome` 的
    `dismissed`），也就是當年漂移掉的那個形狀。所以這裡不逐一寫死「這個情境應該
    印哪個字」——那樣只是把同一份期望抄第三遍——而是**把兩邊拿來互相比對**：任何
    一邊單獨改動都會紅。

    **為什麼既有測試不夠（2026-09-10 量的）。** `closed` / `same` / `escape-spent`
    三條路確實已經有測試同時斷言兩邊；缺的是 `rounds`——
    `test_the_dismiss_loop_is_bounded` 只斷言輪數、收手理由與 `diag_calls`，
    一個字都沒看結論。而那個缺口今天之所以還是被殺掉，靠的是
    `inventory = "" if dismissed else …` 這個**附帶**效果：`dismissed` 兼了第二份
    工作。把清單改成一律抓（很合理的重構，docstring 自己說那份清單才是解開兩層
    對話框之謎的東西），`rounds` 誤報成 `dismissed` 就沒有任何東西攔得住了——實測
    那個組合下只有這支測試會紅。

    跑的是**真的** `dismiss_blocking_dialog`，用本檔既有的 `_ScriptedDialogPort`。
    自己另外做一個假的關閉流程只會測到自己的心智模型。
    """
    print("test_the_dismiss_verdict_word_is_bound_to_the_return_value")
    import contextlib
    import io

    reasons = []
    real_report = ws._report_dismiss_outcome

    def _spy(port, rounds, stop_reason, max_rounds):
        reasons.append(stop_reason)
        return real_report(port, rounds, stop_reason, max_rounds)

    saved = _no_pause()
    ws._report_dismiss_outcome = _spy
    try:
        for name, make_port, kwargs, expect_verdict in _DISMISS_VERDICT_CASES:
            # 每個案例都從空的形狀快取開始。同一個形狀第二次只會印摘要行，而摘要
            # 行**也**帶著結論字（歷史統計靠它），所以這裡不清也不會誤判——但清掉
            # 之後失敗訊息一定指向這個案例本身，不會指到七支測試以前的某一支。
            ws._DISMISS_LOG_SEEN.clear()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(io.StringIO()):
                returned = ws.dismiss_blocking_dialog(make_port(), **kwargs)
            text = buf.getvalue()
            # 兩個字串是**互斥**的：`"could not dismiss blocking dialog"` 裡面沒有
            # `"dismissed blocking dialog"`（是 `dismiss ` 不是 `dismissed `）。
            said_ok = "dismissed blocking dialog" in text
            said_bad = "could not dismiss blocking dialog" in text

            assert returned is True or returned is False, (
                f"[{name}] `dismiss_blocking_dialog` 回的不是 bool 而是 "
                f"{returned!r}——呼叫端是拿它當條件用的，任何非空字串都會被當成"
                "「關掉了」")
            assert (said_ok or said_bad) is expect_verdict, (
                f"[{name}] 這條路" + ("應該" if expect_verdict else "不應該")
                + "印出結論行，實際輸出：" + repr(text))
            if not expect_verdict:
                continue
            assert said_ok != said_bad, (
                f"[{name}] log 同時（或都不）出現兩種結論，讀的人分不出來："
                + repr(text))
            # ---- 這一行就是這支測試的全部理由 ----
            assert said_ok == returned, (
                f"[{name}] 回傳值是 {returned}，log 卻說 "
                f"{'dismissed' if said_ok else 'could not dismiss'}。"
                "這正是 2026-09-03 修掉的那個缺陷：回傳值對、敘述錯，而看 log 的"
                "人是照敘述判斷的（實測 153 筆假的 `dismissed`，真實成功率 0）。"
                "`dismiss_blocking_dialog` 的 return 與 `_report_dismiss_outcome` "
                "的 `dismissed` 必須是同一個判斷。")
    finally:
        ws._report_dismiss_outcome = real_report
        ws.human_pause = saved

    # 正面對照組：情境表真的踩到了每一個會出聲的 stop_reason。少一個是**安靜**的
    # ——某個案例被別的案例吸收掉之後，上面每一句斷言照樣全過。
    missing = _DISMISS_REPORTED_REASONS - set(reasons)
    assert not missing, (
        f"情境表沒有踩到這些 stop_reason：{sorted(missing)}（實際踩到 "
        f"{sorted(set(reasons))}）。有案例退化成跟別的案例一樣了，"
        "而那不會讓任何一句斷言變紅。")
    print("  PASS\n")


def _stop_reason_drift(code_reasons, canary) -> tuple:
    """`(程式認得但 canary 沒釘的, canary 釘了但程式不認得的)`。

    純函式 ＋ 下面那支合成對照，是為了讓「把這個比較放鬆成單向」**殺得掉**。實測
    2026-09-11：本來寫成主測試裡一句 `==` 時，把它改回單向減法在 239 支測試上完全沒有
    症狀（兩個方向在真實資料上都是空的）——也就是這個修法自己可以靜靜退回它剛修掉的
    那個缺陷。
    """
    uncovered = sorted(set(code_reasons) - set(canary))
    stale = sorted(set(canary) - set(code_reasons))
    return uncovered, stale


def test_the_stop_reason_drift_comparison_actually_bites():
    """對照組：乾淨資料讓上面那句斷言兩個方向都是空的。"""
    print("test_the_stop_reason_drift_comparison_actually_bites")
    assert _stop_reason_drift({"a", "b"}, {"a", "b"}) == ([], [])
    # 程式長出新理由而 canary 沒跟上——這是真正發生過的那個方向。
    assert _stop_reason_drift({"a", "b"}, {"a"}) == (["b"], [])
    # canary 釘了一個程式已經不會回報的理由（過期）。
    assert _stop_reason_drift({"a"}, {"a", "b"}) == ([], ["b"])
    # 兩邊各有一個，訊息要分得開。
    assert _stop_reason_drift({"a", "x"}, {"a", "y"}) == (["x"], ["y"])
    print("  PASS\n")


def test_every_stop_reason_the_code_can_report_is_in_the_scenario_table():
    """反方向：**程式**長出第五個收手理由而情境表沒跟上，是安靜的。

    上面那支尾端的 `_DISMISS_REPORTED_REASONS - set(reasons)` 守的是「情境表縮水」。
    它看不到相反的事——程式多一條收手路徑時，`_DISMISS_REPORTED_REASONS`（手寫）與
    `reasons`（情境表跑出來的）**兩邊都不會變**，所以那句話恆為空集合。

    ⚠️ 實測 2026-09-11：把第五個 `stop_reason`（`"stale-frame"`）注進
    `_DISMISS_STOP_TEXT`，`test_webrunner_shared.py` 的 **238 支測試照樣全綠**。

    代價不是抽象的。那支測試存在的理由是 2026-09-03 的「回傳值對、敘述錯」缺陷
    （實測 153 筆假的 `dismissed`、真實成功率 0）。第五條路沒有情境踩到，就等於同一個
    缺陷可以在新那條路上原封不動重演。

    對帳的對象刻意選 `_DISMISS_STOP_TEXT` 而不是剖原始碼找字面值：每一個會**印出
    結論**的 `stop_reason` 都必須在那張表裡有鍵（`_report_dismiss_outcome` 直接
    `_DISMISS_STOP_TEXT[stop_reason]`，少一個鍵是 `KeyError`），所以那張表就是
    「程式認得幾個收手理由」的權威來源，而且不必維護第二份 AST 抽取器。
    """
    print("test_every_stop_reason_the_code_can_report_is_in_the_scenario_table")
    uncovered, stale = _stop_reason_drift(ws._DISMISS_STOP_TEXT,
                                          _DISMISS_REPORTED_REASONS)
    assert not uncovered, (
        f"程式認得這些收手理由，但情境表的 canary 沒釘：{uncovered}"
        f"（canary 釘的是 {sorted(_DISMISS_REPORTED_REASONS)}）。"
        "多出來的那個沒有任何情境踩到——2026-09-03 那個「回傳值對、敘述錯」的缺陷"
        "可以在新那條路上原封不動重演。補一個踩得到它的情境，然後把它加進 "
        "`_DISMISS_REPORTED_REASONS`。")
    assert not stale, (
        f"canary 釘了這些理由，但程式已經不會回報它們：{stale}。"
        "改名或拿掉之後留下的字串，請一併更新——留著等於替未來的回歸先開一張"
        "免死金牌（`CLAUDE.md` 對 `_OWNER_ONLY_SLASH` 記的是同一個形狀）。")
    print("  PASS\n")


def test_the_generation_call_site_scan_is_a_glob_not_a_hardcoded_list(tmp_path):
    """釘住「範圍是算出來的」。

    今天全專案只有 3 個 `generate_one_image` 呼叫點，全在原本那三個檔案裡，所以
    加寬與不加寬在真實資料上分不出來。唯一分辨得出來的辦法是餵它一個**清單裡
    不可能有的名字**——而那正是這道守門的註解自己描述的情境（「再寫一個直接呼叫
    `generate_one_image` 的驗證／工具函式」）。
    """
    print("test_the_generation_call_site_scan_is_a_glob_not_a_hardcoded_list")
    pkg = tmp_path / "pkg"
    root = tmp_path / "root"
    pkg.mkdir()
    root.mkdir()
    (pkg / "verify_generation_probe.py").write_text("X = 1\n", encoding="utf-8")
    (root / "brand_new_entry_point.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "test_not_a_product_module.py").write_text("", encoding="utf-8")

    names = {p.name for p in _prod_sources(pkg_root=pkg, repo_root=root)}
    assert "verify_generation_probe.py" in names, (
        "套件裡新出現的工具沒被算進來——範圍是寫死的清單。"
        "`verify_quota_dialog.py` 已經是這種工具了（它驅動五支共用 hot-path "
        "helper），只是今天還沒呼叫 `generate_one_image`。")
    assert "brand_new_entry_point.py" in names, "repo root 那一半沒有被掃"
    assert "test_not_a_product_module.py" not in names, "測試檔被算成產品端模組"
    print("  PASS\n")


def test_the_quota_handling_detector_can_still_see_a_violation():
    """canary：`_handles` 一直沒有自己的對照組。

    真實原始碼是乾淨的，所以把 `_handles` 改成 `return True`（或
    `_enclosing_functions` 回 `{}`）之後，上面那支照樣全綠——牙齒在這裡。
    四種形狀正反都釘，包含 `except (A, B)` 這種 tuple 寫法。
    """
    print("test_the_quota_handling_detector_can_still_see_a_violation")
    tree = ast.parse(
        "def uncovered():\n"
        "    generate_one_image(port, src)\n"
        "def wrong_exception():\n"
        "    try:\n"
        "        generate_one_image(port, src)\n"
        "    except RuntimeError:\n"
        "        pass\n"
        "def covered():\n"
        "    try:\n"
        "        generate_one_image(port, src)\n"
        "    except GenerationBlockedError:\n"
        "        pass\n"
        "def covered_in_a_tuple():\n"
        "    try:\n"
        "        generate_one_image(port, src)\n"
        "    except (OSError, GenerationBlockedError):\n"
        "        pass\n"
        # 名字**包含**目標字串、但不是同一個類別。子字串比對會把它算成接住了，
        # 精確比對不會——沒有這一格，「`exc_name in name`」改成「子字串」的變異
        # 完全看不出來（實測存活過一次）。
        "def a_different_class_that_merely_contains_the_name():\n"
        "    try:\n"
        "        generate_one_image(port, src)\n"
        "    except MyGenerationBlockedErrorWrapper:\n"
        "        pass\n"
        # 反方向：目標字串**包含**它。前綴比對會誤中。
        "def a_prefix_of_the_name():\n"
        "    try:\n"
        "        generate_one_image(port, src)\n"
        "    except GenerationBlocked:\n"
        "        pass\n")
    owner = _enclosing_functions(tree)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "generate_one_image"]
    assert len(calls) == 6, f"合成語料抽不到六個呼叫點：{len(calls)}"
    verdict = {owner.get(c, "?"): _handles(c, tree, "GenerationBlockedError")
               for c in calls}
    assert verdict == {
        "uncovered": False,
        "wrong_exception": False,
        "covered": True,
        "covered_in_a_tuple": True,
        "a_different_class_that_merely_contains_the_name": False,
        "a_prefix_of_the_name": False,
    }, verdict
    # `_enclosing_functions` 也要有牙齒：報不出位置的話，錯誤訊息會是一串 "?"。
    assert len(verdict) == 6, (
        "`_enclosing_functions` 認不出外層函式名——漏接的位置報不出來")
    print("  PASS\n")


def _sources_without_any_generation_call() -> tuple:
    """真的模組，但**一個 `generate_one_image` 呼叫點都沒有**。

    這是「只違反呼叫點下限」的語料：模組數照樣夠，抽不到的只有呼叫點。用空清單
    是**測不出**那一道的——空清單會先把模組數下限炸掉，第二道一次都不會被執行到
    （§8.8(A4)：一支控制測試只證得了它真的跑到的那一行）。
    """
    import ast as _ast

    out = []
    for path in _prod_sources():
        try:
            tree = _ast.parse(path.read_text(encoding="utf-8"), path.name)
        except (SyntaxError, UnicodeDecodeError):     # pragma: no cover
            continue
        calls = any(
            isinstance(n, _ast.Call)
            and (n.func.attr if isinstance(n.func, _ast.Attribute)
                 else getattr(n.func, "id", "")) == "generate_one_image"
            for n in _ast.walk(tree))
        if not calls:
            out.append(path)
    return tuple(out)


def test_the_generation_scan_floors_each_fire_on_their_own():
    """兩道下限**依序**排列，所以各給一份剛好只違反它的語料（§8.8(A4)）。

    這一支是變異測試逼出來的：兩道下限原本都沒有對照組，把它們放寬成 0 之後三支
    測試照樣全綠。而且只餵一份空語料是不夠的——那只會讓**第一道**炸。
    """
    print("test_the_generation_scan_floors_each_fire_on_their_own")
    import pytest as _pytest

    module = sys.modules[__name__]
    real = _prod_sources

    # (a) 模組數不夠：空的列舉。
    module._prod_sources = lambda **_kw: ()
    try:
        with _pytest.raises(AssertionError) as first:
            test_every_generation_call_site_handles_a_quota_block()
    finally:
        module._prod_sources = real
    assert "抽取器壞了" in str(first.value), (
        f"紅的不是模組數那一句，而是：{first.value}")

    # (b) 模組數夠、但一個呼叫點都沒有——例如有人把 `generate_one_image` 改名。
    clean = _sources_without_any_generation_call()
    assert len(clean) >= _GEN_SOURCE_FLOOR, (
        f"沒有呼叫點的模組只有 {len(clean)} 個，湊不出這個情境——"
        "這一格本身就失去意義了，要重想語料")
    module._prod_sources = lambda **_kw: clean
    try:
        with _pytest.raises(AssertionError) as second:
            test_every_generation_call_site_handles_a_quota_block()
    finally:
        module._prod_sources = real
    assert "是不是改名了" in str(second.value), (
        f"紅的不是呼叫點數那一句，而是：{second.value}")
    print("  PASS\n")


def _run_all():
    """自帶 runner：掃 `globals()` 裡的 `test_*` 全部跑一遍。

    **跑之前先核對數量。** 這個 runner 是掃 `globals()` 的，所以只看得到「在
    `_run_all()` 被呼叫的那一刻已經 bind 好的」名字——2026-08-30 實測，
    `if __name__ == "__main__"` 那一段當時卡在檔案中間，於是它後面定義的 5 支測試
    （V4.5／V5 版面探測，也就是**最新、最沒被驗證過**的那幾支）從來沒有被這條路跑
    到，而畫面上印的是 `ALL 107 TEST GROUPS PASSED`——一個看起來完全正常的綠燈。
    模組 docstring 又明寫「可直接 `py -3` 執行」，所以那個綠燈是有人會信的。
    現在數量對不上就當場紅，而不是安靜地少跑。
    """
    import ast as _ast

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    declared = sum(
        1 for node in _ast.parse(
            Path(__file__).read_text(encoding="utf-8")).body
        if isinstance(node, _ast.FunctionDef) and node.name.startswith("test_"))
    if len(tests) != declared:
        raise SystemExit(
            f"self-runner 只看得到 {len(tests)} 支測試，檔案裡卻定義了 {declared} 支。"
            "多半是有人把 `if __name__ == \"__main__\"` 之後又加了測試——那些在"
            "呼叫 `_run_all()` 的當下還沒 bind，會被安靜地略過。把 main 區塊移到"
            "檔尾即可。")
    # 有些測試收 pytest 的 fixture。這條路沒有 pytest，所以要自己餵——**不是
    # 跳過**：這支 runner 存在的理由就是 2026-08-30 那次「5 支測試被安靜略過，
    # 畫面卻印著 ALL 107 TEST GROUPS PASSED」，把當掉換成靜默略過等於把那個坑
    # 原封不動蓋回來。所以認得的 fixture 就餵、不認得的就大聲炸掉。
    # 實測（2026-09-10）：222 支裡只有 1 支收參數、收的是 `tmp_path`，
    # 而且整個檔案沒有任何 `parametrize`。
    import inspect as _inspect
    import tempfile as _tempfile

    def _fixtures_for(func):
        params = list(_inspect.signature(func).parameters)
        supplied = []
        for name in params:
            if name == "tmp_path":
                supplied.append(Path(_tempfile.mkdtemp(prefix="selfrun_")))
            else:
                raise SystemExit(
                    f"{func.__name__} 收了一個這條路餵不出來的 fixture："
                    f"`{name}`。self-runner 沒有 pytest，請在上面的 "
                    "`_fixtures_for` 裡替它加一個對應的替身（或把那支測試改成"
                    "不收 fixture）——**不要**改成跳過它，那正是這支 runner "
                    "當初要防的那件事。")
        return supplied

    for t in tests:
        t(*_fixtures_for(t))
    print(f"ALL {len(tests)} TEST GROUPS PASSED")




# ---------------------------------------------------------------------------
# 數值設定的標籤：先問頁面，不要把順序排死
#
# 候選清單裡帶冒號與不帶冒號的兩種寫法**各自對應一個站方版面**（2026-08-30 從
# 正式 log 的 `visible setting labels:` 直接讀出來的）：
#     V4.5：'Steps:'、'Prompt Guidance:'、'Prompt Guidance Rescale:'
#     V5  ：'Steps'、 'Prompt Guidance'、 'Prompt Guidance Rescale'
# 兩種都還活著，所以**固定順序必定有一邊每次都踩空**——11 次 setup 裡跑 V5 的
# 那 10 次，每個數值設定都先讓 `Steps:` 白跑兩輪「寫入＋等待＋驗證」才輪到
# `Steps`（log 裡那 60 行 `attempt N: set returned False, actual=None` 就是它），
# 三個設定合計約 12 秒。把順序對調只是把成本換給 V4.5，不是修好。
# ---------------------------------------------------------------------------

_V45_LAYOUT = {"Steps:": 28, "Prompt Guidance:": 7,
               "Prompt Guidance Rescale:": 0.6}
_V5_LAYOUT = {"Steps": 23, "Prompt Guidance": 6,
              "Prompt Guidance Rescale": 0}


def _setting_targets_from_source():
    """把 `configure_sampler_settings` 裡的候選表用 AST 取出來。

    不用字串比對、也不抄一份到測試裡：候選表是**資料**，抄了就會各自漂移。
    """
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(
        ws.configure_sampler_settings)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "setting_targets"
                        for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError(
        "`configure_sampler_settings` 裡找不到 `setting_targets` 的字面指派——"
        "改寫過的話這幾支測試要跟著改，否則它們會安靜地失去意義。")


def _drive_settings(layout):
    """用 `layout` 當「頁面上有哪些標籤」跑一次 `configure_sampler_settings`。

    回「試過的標籤序列」。
    """
    names = ("expand_advanced_settings", "ensure_rescale_visible",
             "dump_advanced_labels", "read_numeric_setting",
             "set_numeric_setting_verified", "set_variety_plus",
             "_has_variety_plus", "select_sampler", "human_pause")
    saved = {name: getattr(ws, name) for name in names}
    tried = []
    try:
        ws.expand_advanced_settings = lambda _port: True
        ws.ensure_rescale_visible = lambda _port: True
        ws.dump_advanced_labels = lambda _port: None
        ws._has_variety_plus = lambda _port: False
        ws.set_variety_plus = lambda *_a, **_k: True
        ws.select_sampler = lambda *_a, **_k: True
        ws.human_pause = lambda *_a, **_k: None
        ws.read_numeric_setting = lambda _port, label: layout.get(label)

        def _verified(_port, label, _value, **_k):
            tried.append(label)
            return label in layout
        ws.set_numeric_setting_verified = _verified
        with contextlib.redirect_stdout(io.StringIO()):
            ok = ws.configure_sampler_settings(FakeBrowserPort())
    finally:
        for name, value in saved.items():
            setattr(ws, name, value)
    assert ok is True, "三個設定都設得成，`configure_sampler_settings` 卻回 False"
    return tried


def test_the_label_probe_picks_whichever_layout_the_page_actually_has():
    print("test_the_label_probe_picks_whichever_layout_the_page_actually_has")
    saved = ws.read_numeric_setting
    try:
        for name, layout in (("V4.5", _V45_LAYOUT), ("V5", _V5_LAYOUT)):
            ws.read_numeric_setting = (
                lambda _p, label, _l=layout: _l.get(label))
            for candidates, _value in _setting_targets_from_source():
                want = next((c for c in candidates if c in layout), None)
                assert want is not None, (
                    f"{name} 版面的標籤不在候選表裡了：{candidates}")
                got = ws.first_present_numeric_label(FakeBrowserPort(),
                                                     candidates)
                assert got == want, (
                    f"{name} 版面上探測挑了 {got!r}，但頁面上真的存在的是 "
                    f"{want!r}。探測挑錯就等於沒探，白跑的候選照樣白跑。")
    finally:
        ws.read_numeric_setting = saved
    print("  PASS\n")


def test_the_label_probe_is_read_only():
    print("test_the_label_probe_is_read_only")
    # 探測跑在**設定值之前**，而且掃的是整份候選清單。要是它會寫，那就等於把
    # 「只寫一次」換成「先亂寫一輪再寫一次」——比修之前更糟。
    saved = (ws.read_numeric_setting, ws.set_numeric_setting)
    wrote = []
    try:
        ws.read_numeric_setting = lambda _p, label: _V5_LAYOUT.get(label)
        ws.set_numeric_setting = lambda *a, **k: wrote.append(a) or True
        ws.first_present_numeric_label(
            FakeBrowserPort(), ("Steps:", "Steps", "Rescale"))
    finally:
        ws.read_numeric_setting, ws.set_numeric_setting = saved
    assert wrote == [], f"探測寫了值：{wrote}"
    print("  PASS\n")


def test_no_write_is_wasted_on_a_label_this_layout_does_not_have():
    print("test_no_write_is_wasted_on_a_label_this_layout_does_not_have")
    for name, layout in (("V4.5", _V45_LAYOUT), ("V5", _V5_LAYOUT)):
        tried = _drive_settings(layout)
        assert len(tried) == 3, (
            f"{name} 版面試了 {len(tried)} 次（{tried}），理想是 3 次。"
            "每一次多出來的嘗試都是一輪「寫入＋等待＋驗證」——正式 log 上"
            "三個設定合計約 12 秒，而且每次 setup 都付一遍。")
        assert all(label in layout for label in tried), (
            f"{name} 版面還是試到了不存在的標籤：{tried}")
    print("  PASS\n")


def test_both_label_spellings_stay_in_the_candidate_table():
    print("test_both_label_spellings_stay_in_the_candidate_table")
    # 「`Steps:` 從來沒成功過」是**在 V5 上**的觀察，不是「這個寫法死了」。
    # 正式 log 裡唯一一次 V4.5 setup，`Steps:` 一發命中。把它當殘留清掉，就等於
    # 讓 V4.5 完全設不了值——而那是靜默的：產出的圖只是「參數不一樣」。
    targets = _setting_targets_from_source()
    flat = [label for candidates, _ in targets for label in candidates]
    for spelling in ("Steps:", "Steps", "Prompt Guidance:", "Prompt Guidance",
                     "Prompt Guidance Rescale:", "Prompt Guidance Rescale"):
        assert spelling in flat, (
            f"候選表裡少了 {spelling!r}。帶冒號的是 V4.5 版面、不帶的是 V5，"
            "兩種都還活著；清掉任何一邊都會讓那個版面靜默地設不了值。")
    print("  PASS\n")


def test_a_probe_that_finds_nothing_leaves_the_old_ordered_loop_intact():
    print("test_a_probe_that_finds_nothing_leaves_the_old_ordered_loop_intact")
    # 探測是最佳化，不是新的判準。頁面回不出東西（唯讀讀取失敗、或空輸入框讓
    # `parseFloat` 變 NaN）時行為必須退回「照原順序全部試過去」，否則一個探不到
    # 的版面會從「慢但會成功」變成「直接不設值」。
    names = ("expand_advanced_settings", "ensure_rescale_visible",
             "dump_advanced_labels", "read_numeric_setting",
             "set_numeric_setting_verified", "set_variety_plus",
             "_has_variety_plus", "select_sampler", "human_pause")
    saved = {name: getattr(ws, name) for name in names}
    tried = []
    try:
        ws.expand_advanced_settings = lambda _port: True
        ws.ensure_rescale_visible = lambda _port: True
        ws.dump_advanced_labels = lambda _port: None
        ws._has_variety_plus = lambda _port: False
        ws.set_variety_plus = lambda *_a, **_k: True
        ws.select_sampler = lambda *_a, **_k: True
        ws.human_pause = lambda *_a, **_k: None
        ws.read_numeric_setting = lambda *_a, **_k: None      # 探測全空
        ws.set_numeric_setting_verified = (
            lambda _p, label, _v, **_k: tried.append(label)
            or not label.endswith(":"))
        with contextlib.redirect_stdout(io.StringIO()):
            ok = ws.configure_sampler_settings(FakeBrowserPort())
    finally:
        for name, value in saved.items():
            setattr(ws, name, value)
    assert ok is True, "探測失敗就整個設定失敗了——退路沒接上"
    expected = [label for candidates, _ in _setting_targets_from_source()
                for label in candidates[:2]]
    assert tried == expected, (
        f"探測失敗時沒有照原順序試：{tried}，預期 {expected}")
    print("  PASS\n")


# ---------------------------------------------------------------------------
# 每次 spawn 記下瀏覽器與 driver 的版本
#
# Chrome 從 2026 年 9 月起改成**兩週一個主版本**，而 chromedriver 的主版號必須完全
# 相符，否則 `webdriver.Chrome(...)` 丟 `SessionNotCreatedException`。本專案
# 2026-08-25 真的踩過一次，log 裡就只有
# `Chrome spawn attempt 1/3 failed: SessionNotCreatedException()`。
# ⚠️ 這段原本寫「那個例外的**訊息是空的**」——2026-09-12 實測推翻並撤回：空的是
# `args`，訊息在 `msg` 裡（351 字元），是 `{err!r}` 把它丟掉的。格式已改由
# `full_error_detail` 處理（守門在 `test_selenium_facade.py`）。這一行版本紀錄仍然
# 留著：它在 spawn **成功**時就先記下來，不必仰賴失敗那一刻的例外格式化。
# ---------------------------------------------------------------------------

def test_the_version_line_reads_both_versions():
    line = ws.driver_version_line({
        "browserVersion": "151.0.7922.174",
        "chrome": {"chromedriverVersion": "151.0.7922.138 (abc123def)"},
    })
    assert "151.0.7922.174" in line, line
    assert "151.0.7922.138" in line, line
    assert "abc123def" not in line, f"build hash 不該進 log：{line}"
    print("  PASS test_the_version_line_reads_both_versions")


def test_a_mismatched_major_version_is_called_out():
    line = ws.driver_version_line({
        "browserVersion": "152.0.1.0",
        "chrome": {"chromedriverVersion": "151.0.2.0 (x)"},
    })
    assert "major version mismatch" in line, (
        f"主版號不同卻沒有標出來：{line}。這正好是兩週一版之後最常見的那種失敗。")
    print("  PASS test_a_mismatched_major_version_is_called_out")


def test_a_matching_major_version_is_not_flagged():
    line = ws.driver_version_line({
        "browserVersion": "151.0.7922.174",
        "chrome": {"chromedriverVersion": "151.0.7922.138"},
    })
    assert "major version mismatch" not in line, f"次版號不同不算不符：{line}"
    print("  PASS test_a_matching_major_version_is_not_flagged")


def test_broken_capabilities_never_raise():
    """這一行跑在**成功**的 spawn 之後——絕不可以把成功的 spawn 變成失敗。"""
    for caps in (None, {}, "nonsense", 42, [],
                 {"browserVersion": None},
                 {"browserVersion": "151", "chrome": "not-a-dict"},
                 {"chrome": {"chromedriverVersion": None}},
                 {"browserVersion": "", "chrome": {}}):
        line = ws.driver_version_line(caps)
        assert isinstance(line, str) and line, f"{caps!r} -> {line!r}"
    print("  PASS test_broken_capabilities_never_raise")


def test_the_version_line_is_actually_printed():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        returned = ws.log_driver_versions(
            {"browserVersion": "151.0.1",
             "chrome": {"chromedriverVersion": "151.0.2"}})
    out = buf.getvalue()
    assert "151.0.1" in out and "151.0.2" in out, out
    assert returned in out, "回傳值要跟印出去的是同一行"
    print("  PASS test_the_version_line_is_actually_printed")


def test_both_variants_log_the_driver_versions():
    """兩支變體都要記——CLAUDE.md 的核心不變量是它們保持同步，而「只有一邊有 log」
    的下場是：出事的那一次剛好跑的是沒有 log 的那一支。

    用 AST 找**呼叫**，不是字串比對：兩邊的呼叫上面都有一段提到
    `ws.log_driver_versions` 的註解，所以 `"log_driver_versions" in source` 在呼叫被
    刪掉之後照樣是真的——實測過，那個版本的 mutation 逃掉了。同一個坑這個 session
    已經踩過一次（一支測試斷言某個字串不在原始碼裡，結果匹配到自己的 docstring）。
    """
    pkg = Path(__file__).resolve().parent.parent / "axiomatic"
    for name in ("webrunner_novelai.py", "webrunner_je_only.py"):
        tree = ast.parse((pkg / name).read_text(encoding="utf-8"), name)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "log_driver_versions"]
        assert calls, (
            f"{name} 沒有在 spawn 之後呼叫 `log_driver_versions`。"
            "註解裡提到它不算——要真的有那個呼叫。")
    print("  PASS test_both_variants_log_the_driver_versions")




# ---------------------------------------------------------------------------
# 批次期間不要讓作業系統打斷這個行程
#
# 2026-09-03 補、2026-09-20 訂正。這台機器只支援 S0 低電源閒置（Modern Standby），
# 系統記錄裡從 05-26 起有 1400 次「進入待命」——平均一天 14 次。沒有保護的話，
# 待命期間行程會被 PLM 暫停、批次就不會前進，而且從外面完全看不出來：行程還
# 活著、沒有崩潰、log 就只是停在那裡。
#
# 訂正的內容：`PowerRequestExecutionRequired` 保的是**行程**（待命期間不被 PLM
# 暫停），**不是**「系統不進入待命」。實測見 `_webrunner_shared` 那段區塊註解。
# ---------------------------------------------------------------------------


def test_stay_awake_is_a_no_op_off_windows():
    """非 Windows 上安靜跳過，不得丟例外——這是加分項，不是批次的前置條件。

    刻意**不**用 `monkeypatch` fixture：本檔的自帶 runner 是直接 `t()` 呼叫的，
    帶 fixture 的測試在那條路上會 `TypeError`。用 `mock.patch.object` 就兩邊都跑
    得起來。
    """
    from unittest import mock
    with mock.patch.object(ws.os, "name", "posix"):
        awake = ws.StayAwake()
        assert awake.acquire() is None
        assert awake.active is None
        awake.release()      # 沒拿到也要能安全釋放


def test_stay_awake_release_is_idempotent_and_never_raises():
    """會從 `finally` 被呼叫，可能已經放過一次了。"""
    awake = ws.StayAwake()
    awake.release()
    awake.release()


def test_stay_awake_does_not_keep_the_display_on():
    """刻意不要求螢幕保持開啟：批次不需要看得見的螢幕，讓別人的螢幕整夜亮著
    是很沒禮貌的事。用 AST 掃常數，不是掃字串（註解裡就有這些名字）。"""
    import ast
    src = Path(ws.__file__).read_text(encoding="utf-8")
    names = {n.targets[0].id for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Assign) and len(n.targets) == 1
             and isinstance(n.targets[0], ast.Name)}
    for banned in ("_ES_DISPLAY_REQUIRED", "_POWER_REQUEST_DISPLAY_REQUIRED"):
        assert banned not in names, (
            f"{banned} 被加進來了——批次不需要螢幕亮著。")


def test_the_power_request_handle_is_not_truncated():
    """`argtypes`／`restype` 一定要寫。

    `PowerCreateRequest` 回的是 64 位元 HANDLE，預設 `c_int` 會截斷它，而**截斷
    後的 handle 仍然非零**，所以 `if not handle` 抓不到——整個功能會安靜地退化成
    「以為要求成功了、其實沒有」。這與 CLAUDE.md 那條 PID 存活探測是同一個坑。
    """
    import ast
    src = Path(ws.__file__).read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "acquire")
    assigned = {ast.unparse(n.targets[0]) for n in ast.walk(fn)
                if isinstance(n, ast.Assign) and len(n.targets) == 1}
    for required in ("kernel32.PowerCreateRequest.argtypes",
                     "kernel32.PowerCreateRequest.restype",
                     "kernel32.PowerSetRequest.argtypes",
                     "kernel32.SetThreadExecutionState.argtypes",
                     "kernel32.SetThreadExecutionState.restype"):
        assert required in assigned, (
            f"少了 `{required}`——64 位元 HANDLE 會被預設的 c_int 截斷，"
            "而截斷後仍然非零，探測抓不到。")


def test_the_batch_asks_to_stay_awake_and_always_releases():
    """功能寫好卻沒人呼叫就只是死碼；沒有 `finally` 就會在例外路徑上漏掉。

    用 AST 找 `run_batch` 裡的 `StayAwake()` 與外層 `try` 的 `finally`。
    """
    import ast
    src = Path(ws.__file__).read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "run_batch")
    made = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == "StayAwake"]
    assert made, "`run_batch` 沒有建立 StayAwake——兩個變體都不會請求保持喚醒。"
    # 建出來卻沒 acquire 是最容易發生的死法：物件在、`finally` 在、測試全綠，
    # 而系統照睡不誤。變異測試補的就是這一條。
    acquired = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "acquire"]
    assert acquired, (
        "`run_batch` 建了 StayAwake 卻從來沒有 `acquire()`——功能等於沒接上，"
        "而外觀上完全看不出來。")
    released = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "release"]
    assert released, "`run_batch` 沒有呼叫 release()"
    in_finally = [n for t in ast.walk(fn) if isinstance(t, ast.Try)
                  for n in ast.walk(ast.Module(body=t.finalbody, type_ignores=[]))
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "release"]
    assert in_finally, (
        "release() 不在 `finally` 裡——例外路徑（rc=3／rc=4／往上炸）會漏掉它。")


def test_keep_system_awake_defaults_to_on_and_coerces():
    """設定檔是給人手動編輯的，型別檢查就是它的全部防線。"""
    from _batch_config import load_batch_config, _DEFAULT_BATCH_CONFIG
    assert _DEFAULT_BATCH_CONFIG["keep_system_awake"] is True
    assert isinstance(load_batch_config()["keep_system_awake"], bool)


def _stay_awake_region() -> str:
    """`StayAwake` 那一整段（區塊註解 ＋ 常數 ＋ 類別，含 docstring）的原始碼。

    用「上一個 `# ---------- ` 區段標題」與「下一個」夾出來，而不是比對標題文字
    ——標題本身就是這次訂正改掉的東西之一。
    """
    lines = Path(ws.__file__).read_text(encoding="utf-8").splitlines()
    const_at = next(i for i, ln in enumerate(lines)
                    if ln.startswith("_POWER_REQUEST_CONTEXT_VERSION"))
    start = max(i for i, ln in enumerate(lines[:const_at])
                if ln.startswith("# ---------- "))
    end = next(i for i, ln in enumerate(lines)
               if i > const_at and ln.startswith("# ---------- "))
    return "\n".join(lines[start:end])


def test_the_stay_awake_notes_do_not_claim_the_request_blocks_standby():
    """電源要求保的是**行程**，不是**系統**——註解不得再宣稱它擋得住待命。

    這裡原本寫著 `"power-request"`（Modern Standby 也擋得住）。2026-09-20 實測
    推翻了它：在持有電源要求的情況下，系統照樣於 `04:25:49` 進入 Modern Standby
    （Kernel-Power `506`，中間沒有 `507`），而批次一路產圖到 `09:32`。微軟文件也
    只承諾「the calling process continues to run」，並且只有在 S3 機器上
    `PowerRequestExecutionRequired` 才順帶隱含 `PowerRequestSystemRequired`。

    **對批次而言結果一樣，所以很容易覺得這只是措辭。** 不是：那句話當天真的把
    一次當機調查帶偏（去追「待命是不是顯示卡當機的主因」，最後靠基準率排掉
    ——待命佔 34.6% 的時間，12 次可判定的當機有 4 次落在待命中）。

    **這支守門的範圍要誠實**：它只擋得住「把原本那句話原樣寫回來」，擋不住有人
    用新的措辭重新宣稱同一件事——沒有任何測試能替一句散文驗證作業系統的行為。
    它真正的價值在另一半：要求那段註解**仍然記著**兩件最容易在重寫時被順手刪掉
    的事（保的是行程／DC 電源下 5 分鐘會被撤銷），所以重寫的人會被逼著重讀結論。
    """
    region = _stay_awake_region()
    # 正面對照組：切出來的是空的話，下面每一句都會恆綠。
    assert "class StayAwake:" in region and len(region) > 1500, (
        "切不出 `StayAwake` 那一段——區段標題或常數名改過了，抽取器要跟著改，"
        "不然底下的檢查全部變成恆綠的裝飾。")
    for claim in ("blocks Modern Standby too", "can block Modern Standby"):
        assert claim not in region, (
            f"註解又宣稱電源要求「{claim}」了。它保的是本行程不被 PLM 暫停；"
            "系統照樣會進入 Modern Standby（2026-09-20 實測）。")
    for needed, why in (
            ("PLM", "電源要求實際保證的東西（行程不被 PLM 暫停）沒有寫出來，"
                    "讀的人只會退回『它擋得住待命』那個錯誤理解。"),
            ("DC", "DC 電源那條限制不見了。"),
            ("5 minutes", "DC 電源下電源要求會在睡眠逾時後 5 分鐘被系統撤銷——"
                       "少了這句，用電池跑的無人值守批次會被安靜暫停，而症狀"
                       "（行程還在、log 只是停住）正是這段註解當初要防的那一個。")):
        assert needed in region, f"`StayAwake` 註解少了「{needed}」：{why}"


def test_the_power_log_lines_name_the_active_value_they_branched_on():
    """`run_batch` 比對的字面值，必須跟 `acquire()` 真的寫進 `active` 的一致，
    而且要原樣印進 log。

    兩個獨立的理由：

    1. **改名漂移。** `acquire` 裡的 `"power-request"` 與 `run_batch` 裡的
       `_got == "power-request"` 是兩個檔案位置上的兩份字面值，中間沒有任何東西
       對帳。改掉一邊，成功分支就永遠不會成立，log 會印「兩種電源要求都拿不到」
       ——**明明拿到了卻回報沒拿到**，正是本專案一再記錄的「log 報的不是結果」。
    2. **敘述會被重寫。** 這三行中文 2026-09-20 就被重寫過一次。把 `active` 的
       字面值一起印出來，事後 grep log 才問得出「那一輪到底拿到了哪一個」，
       而這件事不能只靠註解請人記得。
    """
    import ast
    tree = ast.parse(Path(ws.__file__).read_text(encoding="utf-8"))
    acquire = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "acquire")
    assigned = {n.value.value for n in ast.walk(acquire)
                if isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Attribute)
                and n.targets[0].attr == "active"
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)}
    assert len(assigned) == 2, (
        f"`acquire()` 寫進 `active` 的字面值抽到 {sorted(assigned)}——預期兩個"
        "（`power-request` / `execution-state`）。抽取器壞了，或是多了一種狀態"
        "而下面的對帳還不認得它。")

    run_batch = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "run_batch")
    branches: dict[str, str] = {}
    for node in ast.walk(run_batch):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name) and test.left.id == "_got"
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)):
            continue
        # 只看這一條分支的 body：`orelse` 裡是下一個 elif，會重複算。
        printed = "".join(
            c.value for stmt in node.body for c in ast.walk(stmt)
            if isinstance(c, ast.Constant) and isinstance(c.value, str))
        branches[test.comparators[0].value] = printed
    assert set(branches) == assigned, (
        f"`run_batch` 比對的是 {sorted(branches)}，`acquire()` 寫的是 "
        f"{sorted(assigned)}——其中一邊被改名了，成功分支會永遠不成立，"
        "而 log 會回報「拿不到」。")
    for value, printed in branches.items():
        assert value in printed, (
            f"`_got == {value!r}` 那條分支印出來的字串裡沒有 `{value}`。"
            "中文敘述會被重寫，這個 token 才是事後從 log 判斷實際拿到哪一種"
            "電源要求的唯一依據。")


# ---------------------------------------------------------------------------
# 「這個行程跑的是哪一版程式碼」的接線（`_code_fingerprint`）
#
# 這一組守的是**接線**，不是模組本身（模組的行為由 `test_code_fingerprint.py`
# 守）。接線有兩種壞法，而且兩種壞掉都完全無聲：
#   1. 啟動時忘了取樣 → 之後每次查詢都只回「不知道」，log 照樣印得出一行；
#   2. 漂移檢查每個角色印一行 → 變成下一個 `rpc apply ->` 雜訊源，然後被人關掉。
# ---------------------------------------------------------------------------

class _StubFingerprint:
    """假的 `_code_fingerprint`，回一份指定的 drift_report。"""

    def __init__(self, report):
        self._report = report
        self.snapshots = 0

    def snapshot(self):
        self.snapshots += 1
        return self._report.get("at_start") or "stub"

    def drift_report(self):
        return dict(self._report)

    def describe(self):
        return "code stub (matches disk)"


@contextlib.contextmanager
def _stub_fingerprint(report):
    """暫時把共用模組看到的 `_code_fingerprint` 換掉。

    換的是 `_webrunner_shared` 的模組全域名字，不是 `sys.modules`——後者會讓真的
    模組在別的測試裡被重新 import 回來（本專案在 psutil 上為此弄掉過一個跑了
    78.7 小時的正式批次）。
    """
    saved = ws._code_fingerprint
    stub = _StubFingerprint(report)
    ws._code_fingerprint = stub
    try:
        yield stub
    finally:
        ws._code_fingerprint = saved


def _clean_fingerprint_report():
    return {"drifted": False, "why": "", "at_start": "aaaaaaaaaaaa",
            "now": "aaaaaaaaaaaa", "changed": [], "added": [], "removed": []}


def test_both_variants_take_a_code_fingerprint_at_startup():
    """兩支變體都要在 `main()` 裡取一次啟動指紋。

    用 AST 找**呼叫**而不是字串比對——同 `test_both_variants_log_the_driver_versions`
    的教訓：呼叫點上面那段註解就提到函式名，所以刪掉呼叫之後子字串照樣命中。

    第二半同樣重要：`log_code_fingerprint` 自己必須真的呼叫 `snapshot()`。把那一行
    拿掉，兩個變體照樣「有呼叫」、log 照樣印得出一行，但指紋從來沒有被凍住，於是
    之後每一次 `drift_report()` 都只回「不知道」。那正是這個模組唯一真正的失效
    方式，而且外觀上完全看不出來。
    """
    print("test_both_variants_take_a_code_fingerprint_at_startup")
    pkg = Path(__file__).resolve().parent.parent / "axiomatic"
    for name in ("webrunner_novelai.py", "webrunner_je_only.py"):
        tree = ast.parse((pkg / name).read_text(encoding="utf-8"), name)
        main_fn = next((n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == "main"),
                       None)
        assert main_fn is not None, f"{name} 找不到 main()"
        calls = [n for n in ast.walk(main_fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "log_code_fingerprint"]
        assert calls, (
            f"{name} 的 main() 沒有呼叫 `ws.log_code_fingerprint()`。註解裡提到"
            "它不算——要真的有那個呼叫，否則這個行程事後答不出「我跑的是哪一版"
            "程式碼」，而 traceback 印出來的原始碼文字也就無從判斷可不可信。")
    shared = ast.parse(Path(ws.__file__).read_text(encoding="utf-8"))
    helper = next((n for n in ast.walk(shared)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "log_code_fingerprint"), None)
    assert helper is not None, "_webrunner_shared 少了 log_code_fingerprint"
    took = [n for n in ast.walk(helper)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "snapshot"]
    assert took, (
        "`log_code_fingerprint` 沒有呼叫 `snapshot()`——指紋從來沒被凍住，之後"
        "每一次 drift_report() 都只會回「不知道」，而 log 上完全看不出來。")
    print("  PASS\n")


def test_a_clean_code_check_prints_absolutely_nothing():
    """沒有漂移時，角色邊界的檢查必須**完全**安靜。

    這是本組最重要的一條。漂移檢查每個角色跑一次，而漂移是**常態**（repo 一直在
    被編輯）；`discord_bot.log` 已經有 11,250/11,746 行都是同一句 `rpc apply ->`
    的前例——會被關掉的 log 等於沒有 log。
    """
    print("test_a_clean_code_check_prints_absolutely_nothing")
    buf = io.StringIO()
    with _stub_fingerprint(_clean_fingerprint_report()):
        with contextlib.redirect_stdout(buf):
            returned = ws.report_code_drift()
    assert buf.getvalue() == "", (
        "沒有漂移卻印了東西——這會變成下一個 `rpc apply ->` 雜訊源："
        f"{buf.getvalue()!r}")
    assert returned == "", f"沒有漂移卻回了一行：{returned!r}"
    print("  PASS\n")


def test_drift_is_reported_and_names_the_changed_files():
    """真的漂移了就要說清楚兩件事：變了哪些檔、以及**本行程跑的仍是舊版**。

    後者才是使用者當下真正要知道的答案（這兩個行程都只在啟動時讀程式碼，換角色
    重啟的是瀏覽器、不是行程）。只說「檔案變了」會被讀成無關痛癢的提醒。
    """
    print("test_drift_is_reported_and_names_the_changed_files")
    # `added` 故意放一筆：那是 `snapshot()` 之後才延遲 import 進來的模組，**不算
    # 漂移**（見 `_code_fingerprint.drift_report`），所以它不可以出現在這一行裡
    # ——指著一個沒有問題的檔名會讓讀 log 的人往錯的方向查。
    report = {"drifted": True,
              "why": "1 changed, 0 removed since start",
              "at_start": "aaaaaaaaaaaa", "now": "bbbbbbbbbbbb",
              "changed": ["_webrunner_shared.py"],
              "added": ["_process_control.py"], "removed": []}
    buf = io.StringIO()
    with _stub_fingerprint(report):
        with contextlib.redirect_stdout(buf):
            ws.report_code_drift()
    out = buf.getvalue()
    assert "_webrunner_shared.py" in out, out
    assert "aaaaaaaaaaaa" in out and "bbbbbbbbbbbb" in out, out
    assert "old version" in out, ("漂移那一行沒有講出「本行程跑的仍是舊版」——那是這一行"
                        f"存在的理由：{out!r}")
    assert "_process_control.py" not in out, (
        "延遲 import 進來的模組被當成變動列出來了。它是 `snapshot()` 之後才從磁碟"
        "載入的，也就是**最新**的那一份，不是「我落後了」的證據；混進來只會讓人"
        f"去查一個沒有問題的檔案：{out!r}")
    print("  PASS\n")


def test_undecidable_drift_never_reads_as_a_clean_run():
    """判斷不出來要出聲，而且措辭必須跟「有漂移」分得開。

    本專案在 `_find_all_chrome_processes` / `_load_pid` / `dashboard_server` 上
    各踩過一次同一個形狀：**失敗的掃描長得跟乾淨的掃描一模一樣**。這裡不重蹈。
    """
    print("test_undecidable_drift_never_reads_as_a_clean_run")
    report = {"drifted": None,
              "why": "2 source file(s) unreadable; cannot tell",
              "at_start": None, "now": None,
              "changed": [], "added": [], "removed": []}
    buf = io.StringIO()
    with _stub_fingerprint(report):
        with contextlib.redirect_stdout(buf):
            ws.report_code_drift()
    out = buf.getvalue()
    assert out.strip(), "判斷不出來卻完全不出聲——那跟乾淨的掃描長得一模一樣了"
    assert "cannot tell whether the code changed" in out, (
        f"「不知道」被寫得像「有漂移」或像「一切正常」：{out!r}")
    print("  PASS\n")


def test_the_character_loop_checks_for_drift_without_acting_on_it():
    """角色邊界有檢查，而且**只是**檢查。

    後半是刻意釘的：漂移是常態（我們一直在編輯 repo），把它接進控制流程——重啟、
    中止、拒絕產圖——只會製造誤殺。所以 `run_batch` 裡那個呼叫必須是一個裸的運算式
    陳述，不可以是 `if` 的條件、也不可以先指派給誰再拿去判斷。

    共用一份就同時涵蓋兩個變體（P6 之後角色迴圈只有 `run_batch` 這一份），所以這裡
    不需要、也不應該再去兩個變體裡各找一次。
    """
    print("test_the_character_loop_checks_for_drift_without_acting_on_it")
    src = Path(ws.__file__).read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "run_batch")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "report_code_drift"]
    assert calls, (
        "`run_batch` 的角色迴圈沒有呼叫 `report_code_drift()`——兩個變體會同時"
        "失去週期性的漂移檢查。")
    bare = [n.value for n in ast.walk(fn) if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == "report_code_drift"]
    assert len(bare) == len(calls), (
        "`report_code_drift()` 的回傳值被拿去用了——它是純診斷，不得改變任何行為。")
    print("  PASS\n")


def test_the_fingerprint_helpers_never_kill_a_run():
    """兩支都是診斷用的一行紀錄，不得有任何機會把正常的啟動／角色變成失敗。

    同 `log_driver_versions` 的理由：這是無人值守跑好幾天的行程，為了一行 log 而
    中斷一輪批次完全不划算。壞掉的時候往 stderr 講一聲就好。
    """
    print("test_the_fingerprint_helpers_never_kill_a_run")

    class _Boom:
        def snapshot(self):
            raise RuntimeError("boom")

        def describe(self):
            raise RuntimeError("boom")

        def drift_report(self):
            raise RuntimeError("boom")

    saved = ws._code_fingerprint
    ws._code_fingerprint = _Boom()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                ws.log_code_fingerprint()
                assert ws.report_code_drift() == ""
    finally:
        ws._code_fingerprint = saved
    print("  PASS\n")


# ---------------------------------------------------------------------------
# `wait_if_paused` — 暫停機制本身
# ---------------------------------------------------------------------------
# 這一支在批次的每個切片都會被呼叫（每張圖、每個配對邊界、額度等待與排程休息的
# 每個 30 秒切片），但在此之前**一行都沒有被執行過**：整個測試檔到處把它換成
# `lambda label="": None`，於是覆蓋率是 36/46 未覆蓋。
#
# 它沒生效的症狀是**安靜的**——批次照跑，只是暫停沒作用。而且它還承載了一個尚未
# 實作的計畫：那個計畫的
# 安全性完全建立在「暫停中的產圖程式**不會下任何 driver 指令、也不會走到角色
# 邊界**」這個假設上，因為角色邊界會呼叫 `_kill_orphan_chrome`（全機
# `taskkill /F /T /IM chrome.exe`），會把驗證用的瀏覽器一起殺掉。
# 那個假設在此之前沒有任何測試在守。


def _shared_function_node(name: str):
    """從磁碟上的 `_webrunner_shared.py` 撈出某支函式的 AST 節點。"""
    tree = ast.parse(Path(ws.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`_webrunner_shared.py` 裡找不到 `{name}`")


@contextlib.contextmanager
def _pause_marker_at(path: Path):
    """把 `ws.WEBRUNNER_PAUSE_FILE` 指到 tmp 的一個路徑，離開時還原。"""
    saved = ws.WEBRUNNER_PAUSE_FILE
    ws.WEBRUNNER_PAUSE_FILE = path
    try:
        yield path
    finally:
        ws.WEBRUNNER_PAUSE_FILE = saved


@contextlib.contextmanager
def _captured_events():
    """收走 `emit_event`，回一個 `[(type, payload), …]`。"""
    saved = ws.emit_event
    events: list[tuple] = []
    ws.emit_event = lambda event_type, **data: events.append((event_type, data))
    try:
        yield events
    finally:
        ws.emit_event = saved


class _ExplodingClock:
    """碰到就爆炸的時鐘替身。

    `__getattr__` 只在屬性**找不到**時觸發，而這個類別一個屬性都沒定義，所以
    `time.sleep` / `time.time` / `time.monotonic` 任何一個被碰到都會當場紅。
    """

    def __init__(self, why: str):
        self._why = why

    def __getattr__(self, name):
        raise AssertionError(f"{self._why}（碰到了 time.{name}）")


def _release_after(marker: Path, slices: int, cap: int = 50):
    """回一個 `on_sleep`：第 `slices` 個切片之後移除暫停標記。

    `cap` 是為了把「暫停迴圈不會結束」變成一行紅色訊息，而不是掛住整個回合。
    `wait_if_paused` 的**唯一**出口就是標記消失，所以任何「不再每個切片重讀
    標記」的回歸都會無限迴圈——掛住的測試比紅的測試難查得多，而且在無人值守的
    批次裡會把後面排隊的東西全部擋住。
    """
    def _step(clock, _seconds):
        if len(clock.slept) >= slices and marker.exists():
            marker.unlink()
        if len(clock.slept) > cap:
            raise AssertionError(
                f"暫停迴圈跑了 {len(clock.slept)} 個切片還沒結束——標記早就移除了，"
                "代表切片之間沒有重新讀取它。")
    return _step


class _CountingClock(_FakeClock):
    """會數「牆鐘被讀了幾次」的假時鐘。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.time_calls = 0

    def time(self) -> float:
        self.time_calls += 1
        return super().time()


def test_no_pause_marker_costs_nothing():
    """沒有標記時要**立刻**回來：不睡、不寫檔、不發事件。

    只斷言回傳值分不出來——`wait_if_paused` 回的永遠是 `None`，暫停與不暫停一模
    一樣。所以這裡改成把「不該被碰到的東西」全部換成會爆炸的替身：時鐘、
    `emit_event`、以及原子寫入。它是每張圖都會跑一次的熱路徑，多付任何一次 I/O
    都要乘上 `images_per_character`。
    """
    print("test_no_pause_marker_costs_nothing")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        saved_clock = ws.time
        saved_write = ws._run_progress.atomic_write_text
        ws.time = _ExplodingClock("沒有暫停標記時不該睡、也不該讀時鐘")

        def _boom_write(path, text):
            raise AssertionError("沒有暫停標記時不該寫任何檔案")

        ws._run_progress.atomic_write_text = _boom_write
        try:
            with _pause_marker_at(root / "webrunner.pause"):
                with _captured_events() as events:
                    ws.emit_event = lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError("沒有暫停標記時不該發事件"))
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        assert ws.wait_if_paused("some image") is None
                        assert ws.wait_if_paused("pair boundary") is None
                    assert events == []
            assert out.getvalue() == "", (
                f"沒有暫停標記時不該印任何東西，卻印了 {out.getvalue()!r}")
            assert list(root.iterdir()) == [], (
                "沒有暫停標記時不該在磁碟上留下任何東西")
        finally:
            ws.time = saved_clock
            ws._run_progress.atomic_write_text = saved_write
    print("  PASS\n")


def test_a_pause_waits_in_slices_and_rechecks_the_marker_every_slice():
    """暫停要**切片地**睡，而且每個切片之間重新讀一次標記。

    一次睡到底的話，`/resume` 要等那一整段睡完才生效——這正是 `rest_until`
    當初修掉的那個病態（單一 `time.sleep(21600)` 讓暫停最久六小時才生效）。
    這裡釘的是反方向：標記被移除之後**一個切片內**就要回來。
    """
    print("test_a_pause_waits_in_slices_and_rechecks_the_marker_every_slice")
    with tempfile.TemporaryDirectory() as td:
        marker = Path(td) / "webrunner.pause"
        marker.write_text(json.dumps({"mode": "now"}), encoding="utf-8")
        with _pause_marker_at(marker), _captured_events() as events:
            with _fake_clock() as clk:
                def _step(clock, _seconds):
                    if len(clock.slept) == 3:      # 第 3 次切片之後解除暫停
                        marker.unlink()
                clk.on_sleep = _step
                with contextlib.redirect_stdout(io.StringIO()):
                    ws.wait_if_paused("pair boundary")
            assert len(clk.slept) == 3, (
                f"標記移除後應該在一個切片內回來，實際睡了 {len(clk.slept)} 次")
            assert set(clk.slept) == {2.0}, (
                f"切片長度應該固定而且很短，實際 {clk.slept}")
            assert [kind for kind, _ in events] == ["paused", "resumed"], (
                f"暫停與恢復各要發一次，實際 {events}")
            assert all(data.get("label") == "pair boundary"
                       for _, data in events), events
    print("  PASS\n")


def test_the_pause_wait_never_touches_the_browser():
    """**暫停期間不得對 driver 下任何指令。**

    這是「用暫停標記開一個驗證窗口」的安全前提：
    暫停中的產圖程式必須是完全被動的，否則 `verify_browser` / `verify_quota_dialog`
    在那個窗口裡開的瀏覽器隨時會被撞到。

    兩層一起釘，因為它們擋的東西不一樣：

    1. **行為層**：把 `_webrunner_shared` 裡**每一支第一個參數叫 `port` 的函式**
       換成會爆炸的替身（自動涵蓋日後新增的），再跑完整的暫停迴圈。
    2. **形狀層**：`wait_if_paused` 呼叫得到的名字是一份封閉清單。行為層測不到
       「有人加了一個新的 port 參數進來」——那會改簽章而不是呼叫既有函式；
       形狀層則會因為多出一個名字而紅。
    """
    print("test_the_pause_wait_never_touches_the_browser")
    # ---- 1. 行為層 ----
    port_takers = {}
    for name, value in list(vars(ws).items()):
        if not isinstance(value, types.FunctionType):
            continue
        args = value.__code__.co_varnames[:value.__code__.co_argcount]
        if args and args[0] == "port":
            port_takers[name] = value

    assert len(port_takers) > 20, (
        f"只找到 {len(port_takers)} 支吃 port 的函式——掃描器自己壞了，"
        "這支測試會安靜地什麼都沒測到")

    def _boom(name):
        def _f(*_a, **_k):
            raise AssertionError(f"暫停期間呼叫了 {name}()——那會碰到瀏覽器")
        return _f

    with tempfile.TemporaryDirectory() as td:
        marker = Path(td) / "webrunner.pause"
        marker.write_text(json.dumps({"mode": "now"}), encoding="utf-8")
        try:
            for name in port_takers:
                setattr(ws, name, _boom(name))
            with _pause_marker_at(marker), _captured_events():
                with _fake_clock() as clk:
                    clk.on_sleep = lambda c, _s: (
                        marker.unlink() if len(c.slept) == 2 else None)
                    with contextlib.redirect_stdout(io.StringIO()):
                        ws.wait_if_paused("pair boundary")
                assert len(clk.slept) == 2
        finally:
            for name, original in port_takers.items():
                setattr(ws, name, original)

    # ---- 2. 形狀層 ----
    expected = {
        '(label or \'\').lower',
        'WEBRUNNER_PAUSE_FILE.exists',
        'WEBRUNNER_PAUSE_FILE.read_text',
        "WEBRUNNER_PAUSE_FILE.read_text(encoding='utf-8').strip",
        '_read_pause',
        '_run_progress.atomic_write_text',
        '_write_pause',
        'emit_event',
        'isinstance',
        'json.dumps',
        'json.loads',
        'marker.get',
        'print',
        'str',
        "str(marker.get('mode') or 'now').lower",
        'time.sleep',
    }
    node = _shared_function_node("wait_if_paused")
    actual = {ast.unparse(call.func)
              for call in ast.walk(node) if isinstance(call, ast.Call)}
    assert actual == expected, (
        "`wait_if_paused` 呼叫得到的名字變了。這是封閉清單，因為暫停期間**唯一**\n"
        "允許發生的事是讀／寫暫停標記、印一行、發一則事件、睡一個切片。\n"
        f"  多出來：{sorted(actual - expected)}\n"
        f"  不見了：{sorted(expected - actual)}\n"
        "如果新加的東西真的不碰瀏覽器（也不碰角色邊界），把它加進 expected 並在"
        "這裡寫一句理由。")
    print("  PASS\n")


def test_a_wall_clock_jump_cannot_extend_a_pause():
    """暫停量的是**間隔**（切片長度），所以不該有牆鐘參與。

    這一支釘的是「現在沒有、將來也不要有」：`wait_if_paused` 目前是純輪詢，
    完全不讀 `time.time()`，所以 NTP 校時／手動改時鐘動不到它。有人日後加一個
    `end = time.time() + X` 的整體上限進來，牆鐘往回撥一小時就會讓暫停多掛一
    小時，而症狀是「`/resume` 按了沒反應」——最難查的那一種。
    """
    print("test_a_wall_clock_jump_cannot_extend_a_pause")
    with tempfile.TemporaryDirectory() as td:
        marker = Path(td) / "webrunner.pause"
        marker.write_text(json.dumps({"mode": "now"}), encoding="utf-8")
        clk = _CountingClock()
        with _pause_marker_at(marker), _captured_events():
            with _fake_clock(clk):
                def _step(clock, _seconds):
                    if len(clock.slept) == 1:
                        clock.wall_skew -= 24 * 3600.0   # 牆鐘往回撥一天
                    if len(clock.slept) == 4:
                        marker.unlink()
                clk.on_sleep = _step
                with contextlib.redirect_stdout(io.StringIO()):
                    ws.wait_if_paused("pair boundary")
        assert len(clk.slept) == 4, (
            f"牆鐘往回撥不該改變暫停長度，實際睡了 {len(clk.slept)} 次")
        assert clk.time_calls == 0, (
            f"`wait_if_paused` 讀了 {clk.time_calls} 次牆鐘——它量的是切片長度，"
            "不該有牆鐘參與（要量間隔請用 time.monotonic）")
    print("  PASS\n")


def test_an_unreadable_pause_marker_still_pauses():
    """**標記讀不出來時要當成「有暫停」，不能長得跟「沒有暫停」一樣。**

    這個專案已經在 `_find_all_chrome_processes`／`_load_pid`／
    `dashboard_server._read_pid` 各踩過一次「失敗的掃描長得跟乾淨的掃描一模一樣」。
    暫停這一側的誤判方向是不對稱的：讀不出來卻照跑 ＝ 使用者按了暫停但批次沒停，
    而那正是「開一個驗證窗口」會賠掉整個批次的情況；讀不出來就停 ＝ 最多白停一下。

    現況（本測試確認）是**安全的那一邊**：`_read_pause` 的 `except Exception`
    退回 `{"mode": "now"}`，也就是立刻暫停。四種壞法都走同一條路。
    """
    print("test_an_unreadable_pause_marker_still_pauses")
    cases = {
        "不是合法 UTF-8": lambda p: p.write_bytes(b"\xff\xfe\x00 not utf-8"),
        "不是合法 JSON": lambda p: p.write_text("{半個 JSON", encoding="utf-8"),
        "合法 JSON 但不是 dict": lambda p: p.write_text("[1, 2]", encoding="utf-8"),
        "空檔案（半寫入）": lambda p: p.write_text("", encoding="utf-8"),
    }
    with tempfile.TemporaryDirectory() as td:
        for label, make in cases.items():
            marker = Path(td) / "webrunner.pause"
            make(marker)
            with _pause_marker_at(marker), _captured_events() as events:
                with _fake_clock() as clk:
                    clk.on_sleep = lambda c, _s: (
                        marker.unlink() if len(c.slept) == 1 else None)
                    with contextlib.redirect_stdout(io.StringIO()):
                        ws.wait_if_paused("some image")
                assert len(clk.slept) == 1, f"{label}：應該要暫停，卻直接回來了"
                assert [kind for kind, _ in events] == ["paused", "resumed"], (
                    f"{label}：壞掉的標記也要發 paused／resumed，實際 {events}")

        # 讀檔本身丟例外（權限）也一樣要暫停。`exists()` 仍要回 True，所以只換
        # `read_text`——這才是「檔案在、但讀不出來」。
        marker = Path(td) / "webrunner.pause"
        marker.write_text(json.dumps({"mode": "now"}), encoding="utf-8")
        real_read_text = Path.read_text

        def _denied(self, *args, **kwargs):
            if self.name == "webrunner.pause":
                raise PermissionError(13, "Access is denied")
            return real_read_text(self, *args, **kwargs)

        try:
            Path.read_text = _denied
            with _pause_marker_at(marker), _captured_events() as events:
                with _fake_clock() as clk:
                    clk.on_sleep = lambda c, _s: (
                        marker.unlink() if len(c.slept) == 1 else None)
                    with contextlib.redirect_stdout(io.StringIO()):
                        ws.wait_if_paused("some image")
                assert len(clk.slept) == 1, "權限錯誤：應該要暫停，卻直接回來了"
                assert [kind for kind, _ in events] == ["paused", "resumed"]
        finally:
            Path.read_text = real_read_text
    print("  PASS\n")


def test_after_current_only_stops_at_a_pair_boundary():
    """`after_current` ＝「跑完這個角色再停」，所以只在配對邊界生效。

    判準是 label 裡有沒有 `pair`——`run_batch` 傳的是 `"pair boundary"`，
    `generate_loop` 傳的是 `"<角色> image"`。圖與圖之間收到 `after_current`
    必須**直接回來**（那一張圖還沒跑完），而且不得改寫標記。
    """
    print("test_after_current_only_stops_at_a_pair_boundary")
    with tempfile.TemporaryDirectory() as td:
        marker = Path(td) / "webrunner.pause"
        original = json.dumps({"mode": "after_current", "ts": 1.0})
        marker.write_text(original, encoding="utf-8")
        with _pause_marker_at(marker), _captured_events() as events:
            # 非配對邊界：立刻回來，標記原封不動，也不發事件。
            saved_clock = ws.time
            ws.time = _ExplodingClock("非配對邊界收到 after_current 不該睡")
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    ws.wait_if_paused("surtr (arknights) image")
            finally:
                ws.time = saved_clock
            assert events == []
            assert marker.read_text(encoding="utf-8") == original, (
                "非配對邊界不該改寫標記")

            # 配對邊界：標記翻成 `now` 並真的停下來。
            with _fake_clock() as clk:
                clk.on_sleep = lambda c, _s: (
                    marker.unlink() if len(c.slept) == 2 else None)
                with contextlib.redirect_stdout(io.StringIO()):
                    ws.wait_if_paused("pair boundary")
            assert len(clk.slept) == 2
            assert [kind for kind, _ in events] == ["paused", "resumed"]
    print("  PASS\n")


def test_after_pairs_counts_down_one_pair_at_a_time():
    """`after_pairs` ＝「再跑 N 個配對」：每個配對邊界扣一次，扣到 0 才停。

    順便釘住 `remaining` 不是整數時的退路（0 ＝ 立刻停）。bot 寫的一定是 int，
    但半寫入的 JSON 或手改過的檔案不保證——而這裡誤判成「還有很多」就等於暫停
    永遠不會發生。
    """
    print("test_after_pairs_counts_down_one_pair_at_a_time")
    with tempfile.TemporaryDirectory() as td:
        marker = Path(td) / "webrunner.pause"
        marker.write_text(
            json.dumps({"mode": "after_pairs", "remaining": 2}),
            encoding="utf-8")
        with _pause_marker_at(marker), _captured_events() as events:
            saved_clock = ws.time
            ws.time = _ExplodingClock("倒數還沒到 0 不該睡")
            try:
                # 圖與圖之間（非配對邊界）：直接回來，倒數**不動**。少了這一條，
                # 240 張圖會把 `remaining` 一路扣穿。
                with contextlib.redirect_stdout(io.StringIO()):
                    ws.wait_if_paused("surtr (arknights) image")
                assert json.loads(marker.read_text(encoding="utf-8"))[
                    "remaining"] == 2, "非配對邊界不該動到倒數"

                for expected_left in (1, 0):
                    with contextlib.redirect_stdout(io.StringIO()):
                        ws.wait_if_paused("pair boundary")
                    left = json.loads(marker.read_text(encoding="utf-8"))
                    assert left["remaining"] == expected_left, left
                    assert left["mode"] == "after_pairs", left
                assert events == [], "倒數期間不該發 paused"
            finally:
                ws.time = saved_clock

            # 第三個配對邊界：remaining 已經是 0 → 翻成 now 並停下來。
            with _fake_clock() as clk:
                clk.on_sleep = lambda c, _s: (
                    marker.unlink() if len(c.slept) == 1 else None)
                with contextlib.redirect_stdout(io.StringIO()):
                    ws.wait_if_paused("pair boundary")
            assert len(clk.slept) == 1
            assert [kind for kind, _ in events] == ["paused", "resumed"]

        # `remaining` 不是整數 → 當成 0 → 立刻停。
        marker.write_text(
            json.dumps({"mode": "after_pairs", "remaining": "三"}),
            encoding="utf-8")
        with _pause_marker_at(marker), _captured_events() as events:
            with _fake_clock() as clk:
                clk.on_sleep = lambda c, _s: (
                    marker.unlink() if len(c.slept) == 1 else None)
                with contextlib.redirect_stdout(io.StringIO()):
                    ws.wait_if_paused("pair boundary")
            assert len(clk.slept) == 1
            assert [kind for kind, _ in events] == ["paused", "resumed"]
    print("  PASS\n")


def test_a_failed_marker_write_disarms_the_after_pairs_countdown():
    """**已知缺陷的現況紀錄**：標記寫不進去時，`after_pairs` 的倒數永遠不會落地。

    `_write_pause` 走 `_run_progress.atomic_write_text`，那是 best-effort ——
    失敗回 `False` 而不是丟例外。`after_pairs` 的倒數**只存在磁碟上**，所以寫
    失敗 ⇒ 下一個配對邊界又讀到同一個 `remaining` ⇒ 再扣一次 ⇒ 再寫失敗……
    使用者下的 `/pause after N pairs` 就這樣永遠不會生效。

    嚴重度被兩件事壓下來：要觸發得整個 repo root 不可寫（那時壞掉的東西遠不止
    這個），而且 `atomic_write_text` 至少會往 stderr 講一聲。所以這裡**不修**，
    只把現況釘住——真的去修的時候這支會紅，讀到這段 docstring 就知道為什麼。
    對照組：`after_current` 不受影響，因為它翻成 `now` 之後是靠**區域變數**繼續
    走完那一圈的，不需要磁碟上的狀態。
    """
    print("test_a_failed_marker_write_disarms_the_after_pairs_countdown")
    with tempfile.TemporaryDirectory() as td:
        marker = Path(td) / "webrunner.pause"
        marker.write_text(
            json.dumps({"mode": "after_pairs", "remaining": 1}),
            encoding="utf-8")
        real_replace = os.replace

        def _read_only(src, dst):
            if str(dst).endswith("webrunner.pause"):
                raise OSError(30, "Read-only file system")
            return real_replace(src, dst)

        try:
            os.replace = _read_only
            with _pause_marker_at(marker), _captured_events() as events:
                saved_clock = ws.time
                ws.time = _ExplodingClock("倒數沒落地時不該睡")
                try:
                    for _ in range(3):
                        with contextlib.redirect_stdout(io.StringIO()):
                            with contextlib.redirect_stderr(io.StringIO()) as err:
                                ws.wait_if_paused("pair boundary")
                finally:
                    ws.time = saved_clock
                assert events == [], (
                    "現況：倒數寫不下去，所以暫停一次都不會發生")
                assert json.loads(marker.read_text(encoding="utf-8"))[
                    "remaining"] == 1, "磁碟上的倒數應該完全沒動"
                assert "failed" in err.getvalue(), (
                    "寫入失敗至少要留在 stderr——這是目前唯一的線索")
        finally:
            os.replace = real_replace
    print("  PASS\n")


def test_an_unknown_pause_mode_does_not_pause():
    """認不得的 `mode` ＝ 不暫停。這是 bot 側新增模式時必須一起改這裡的耦合。

    bot 目前只寫三種（`now`／`after_current`／`after_pairs`，見
    `discord_bot.cmd_pause`），所以這條路今天走不到。釘它是因為那個耦合從程式碼
    上完全看不出來：bot 加第四種模式，webrunner 這側不會報錯、不會警告，只是
    暫停安靜地失效。
    """
    print("test_an_unknown_pause_mode_does_not_pause")
    with tempfile.TemporaryDirectory() as td:
        marker = Path(td) / "webrunner.pause"
        marker.write_text(
            json.dumps({"mode": "after_the_heat_death_of_the_universe"}),
            encoding="utf-8")
        with _pause_marker_at(marker), _captured_events() as events:
            saved_clock = ws.time
            ws.time = _ExplodingClock("認不得的 mode 不該睡")
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    ws.wait_if_paused("pair boundary")
            finally:
                ws.time = saved_clock
            assert events == []
    print("  PASS\n")


# ---------------------------------------------------------------------------
# `find_browser_pids_for_profile` — 哪些行程「屬於這個 profile」
# ---------------------------------------------------------------------------
# 它答錯的方向不對稱：**漏掉** ＝ 殘留的瀏覽器佔著 profile（只是少清一輪）；
# **多抓** ＝ 動到不該動的行程。目前唯一的呼叫端是 `hide_browser_windows`
# （多抓只是把別人的視窗搬到螢幕外），但契約是「屬於這個 profile 的行程」，接到終止
# 那一側就是災難——這個 repo 已經為了「殺太多」賠掉過一個跑了 78.7 小時的批次。


class _FakeNoSuchProcess(Exception):
    pass


class _FakeAccessDenied(Exception):
    pass


class _ScanProc:
    """`process_iter` 掃到的行程替身（本節專用）。

    刻意**不提供** `kill()` / `terminate()`：`find_browser_pids_for_profile`
    只讀不動，替身多長一隻手只會讓下一個人以為那條路存在。
    """

    def __init__(self, pid, name, cmdline=(), cmdline_error=None):
        self.info = {"pid": pid, "name": name}
        # `None` 要原樣留著——某些驅動版本的 `cmdline()` 真的會回 None，而被測
        # 程式的 `(proc.cmdline() or [])` 就是為它寫的。
        self._cmdline = None if cmdline is None else list(cmdline)
        self._cmdline_error = cmdline_error

    def cmdline(self):
        if self._cmdline_error is not None:
            raise self._cmdline_error
        return None if self._cmdline is None else list(self._cmdline)


def _scan_psutil(procs=(), *, process_iter_error=None, iter_calls=None):
    module = types.ModuleType("psutil")
    module.NoSuchProcess = _FakeNoSuchProcess
    module.AccessDenied = _FakeAccessDenied

    def process_iter(attrs=None):
        if iter_calls is not None:
            iter_calls.append(attrs)
        if process_iter_error is not None:
            raise process_iter_error
        return list(procs)

    module.process_iter = process_iter
    return module


_MODULE_ABSENT = "__absent__"


@contextlib.contextmanager
def _psutil_is(replacement):
    """把 `sys.modules["psutil"]` 換成 `replacement`（`None` ＝ 模擬未安裝）。

    還原走 `_MODULE_ABSENT` 哨兵，**不能**用 `sys.modules.get()` 的 `None`：
    `None` 正是「讓 `import psutil` 丟 ImportError」的標準寫法，拿它當「本來就
    不在」的判準會把還原變成 `pop`——而 `pop` 清掉的只是快取，下一個
    `import psutil` 會從磁碟載入**真的**那一個。2026-09-07 就是這樣弄掉一個跑了
    78.7 小時的正式批次。
    """
    saved = sys.modules.get("psutil", _MODULE_ABSENT)
    sys.modules["psutil"] = replacement
    try:
        yield
    finally:
        if saved is _MODULE_ABSENT:
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = saved


def test_only_chrome_processes_naming_the_profile_are_picked():
    """只挑 cmdline 真的帶那個 profile 路徑的 `chrome.exe`。

    三個負面案例各擋一件事：沒有 `--user-data-dir` 的 chrome（別的視窗）、跑在
    別的 profile 上的 chrome（使用者自己開的）、以及**名字不是 chrome.exe 但
    cmdline 裡就是有那條路徑**的行程——最後這個是最危險的一種，本專案自己的
    python 行程命令列裡就帶著 repo 路徑。
    """
    print("test_only_chrome_processes_naming_the_profile_are_picked")
    root = "D:/Work/Example"
    procs = [
        _ScanProc(101, "chrome.exe",
                  ["chrome.exe", f"--user-data-dir={root}/.chrome_profile_snap",
                   "--no-first-run"]),
        _ScanProc(102, "chrome.exe", ["chrome.exe", "--type=renderer",
                                      f"--user-data-dir={root}/.chrome_profile_snap"]),
        _ScanProc(103, "chrome.exe", ["chrome.exe"]),
        _ScanProc(104, "chrome.exe",
                  ["chrome.exe", "--user-data-dir=C:/Users/Example/AppData/Chrome"]),
        _ScanProc(105, "msedge.exe",
                  ["msedge.exe", f"--user-data-dir={root}/.chrome_profile_snap"]),
        _ScanProc(106, "python.exe",
                  ["python.exe", f"{root}/axiomatic/webrunner_novelai.py",
                   f"{root}/.chrome_profile_snap"]),
    ]
    iter_calls = []
    with _psutil_is(_scan_psutil(procs, iter_calls=iter_calls)):
        got = ws.find_browser_pids_for_profile(
            [Path(root) / ".chrome_profile_snap"])
    assert got == {101, 102}, got
    # 效能契約（`test_process_control` 那條規則的行為面）：`attrs=` 只取便宜的
    # `name`，`cmdline()` 留給通過篩選的那幾筆。放進 `attrs=` 就是替全機每一個
    # 行程讀一次 PEB（本機實測 334 ms vs 121 ms），而這支每個角色都跑一次。
    assert iter_calls == [["pid", "name"]], iter_calls

    # 空清單 → 直接回空，連掃都不掃。
    ran = []
    with _psutil_is(_scan_psutil(procs, iter_calls=ran)):
        assert ws.find_browser_pids_for_profile([]) == set()
    assert ran == [], "沒有要找的 profile 時不該掃行程"
    print("  PASS\n")


def test_the_profile_match_survives_case_and_separator_differences():
    """cmdline 的寫法與 `Path` 的字串形式不見得一致，比對前要正規化。

    Windows 上同一個目錄可能寫成反斜線或正斜線、大小寫也不保證一致；直接字串
    比對會**漏**，而漏掉的症狀是「最小化沒作用」——看得見但查不出來。
    """
    print("test_the_profile_match_survives_case_and_separator_differences")
    procs = [
        _ScanProc(201, "chrome.exe",
                  ["chrome.exe",
                   "--user-data-dir=D:\\WORK\\EXAMPLE\\.Chrome_Profile_Snap"]),
        # 子路徑（後面接分隔符）也算同一個 profile。
        _ScanProc(202, "chrome.exe",
                  ["chrome.exe",
                   "--user-data-dir=D:/work/example/.chrome_profile_snap/Default"]),
        # profile 路徑是最後一個 arg（後面什麼都沒有）。
        _ScanProc(203, "chrome.exe",
                  ["chrome.exe", "--user-data-dir=D:\\Work\\Example\\.chrome_profile_snap"]),
    ]
    with _psutil_is(_scan_psutil(procs)):
        got = ws.find_browser_pids_for_profile(
            [Path("D:/Work/Example/.chrome_profile_snap")])
    assert got == {201, 202, 203}, got
    print("  PASS\n")


def test_the_profile_match_never_confuses_chrome_profile_with_chrome_profile_snap():
    """**`.chrome_profile` 不得比對到 `.chrome_profile_snap`。**

    這兩個目錄在本專案裡同時存在而且**其中一個是另一個的嚴格前綴**：
    `.chrome_profile/` 是登入用的來源，`.chrome_profile_snap/` 才是 Chrome 真正
    開的那一份。隔離驗證模式再加一個 `.chrome_profile_verify/`。所以

        any(path in cmd for path in wanted)      # 子字串比對

    會讓 `wanted = [.chrome_profile]` 選中跑在 `_snap` 上的**正式批次瀏覽器**。
    實際影響到的組合是驗證模式的 `[.chrome_profile, .chrome_profile_verify]`
    ——它會把正式批次的視窗一起縮起來，而 `verify_browser` 的隔離契約寫著
    「絕不干擾正在跑的正式作業」。

    同一組前綴已經害過一次：`_cleanup_chrome_locks` 清的是 `.chrome_profile/`，
    Chrome 開的是 `.chrome_profile_snap/`，於是那條四階梯復原鏈就算接上去也等於沒做
    （2026-09-07 移除）。所以這一條在**來源**收緊，不是要求每個呼叫端自己小心。
    """
    print("test_the_profile_match_never_confuses_chrome_profile_with_chrome_profile_snap")
    root = "D:/Work/Example"
    procs = [
        _ScanProc(301, "chrome.exe",                       # 正式批次
                  ["chrome.exe", f"--user-data-dir={root}/.chrome_profile_snap"]),
        _ScanProc(302, "chrome.exe",                       # 隔離驗證
                  ["chrome.exe", f"--user-data-dir={root}/.chrome_profile_verify"]),
        _ScanProc(303, "chrome.exe",                       # 真的開在來源 profile
                  ["chrome.exe", f"--user-data-dir={root}/.chrome_profile"]),
    ]
    with _psutil_is(_scan_psutil(procs)):
        assert ws.find_browser_pids_for_profile(
            [Path(root) / ".chrome_profile"]) == {303}, (
            "`.chrome_profile` 選到了 `_snap` 或 `_verify`——前綴誤配")
        # 驗證模式實際傳進去的那一組：不得選中正式批次（301）。
        assert ws.find_browser_pids_for_profile(
            [Path(root) / ".chrome_profile",
             Path(root) / ".chrome_profile_verify"]) == {302, 303}, (
            "隔離驗證模式選中了正式批次的瀏覽器")
        # 反方向：正式模式那一組照樣要選滿，收緊不得變成漏掉。
        assert ws.find_browser_pids_for_profile(
            [Path(root) / ".chrome_profile",
             Path(root) / ".chrome_profile_snap"]) == {301, 303}
    print("  PASS\n")


def test_a_process_that_vanishes_mid_scan_does_not_stop_the_scan():
    """一筆讀不到就跳過那一筆，不得中斷整輪掃描。

    行程在 `process_iter` 與 `cmdline()` 之間結束是常態（`_kill_orphan_chrome`
    才剛砍過一輪），權限不足也是。整輪中斷的症狀是「有時候最小化有用、有時候
    沒用」，而順序是隨機的——所以把會爆的那一筆放在**要找的那一筆前面**，
    不然這支測試會安靜地什麼都沒測到。
    """
    print("test_a_process_that_vanishes_mid_scan_does_not_stop_the_scan")
    root = "D:/Work/Example"
    procs = [
        _ScanProc(401, "chrome.exe", cmdline_error=_FakeNoSuchProcess("gone")),
        _ScanProc(402, "chrome.exe", cmdline_error=_FakeAccessDenied("denied")),
        _ScanProc(403, "chrome.exe",
                  ["chrome.exe", f"--user-data-dir={root}/.chrome_profile_snap"]),
    ]
    with _psutil_is(_scan_psutil(procs)):
        got = ws.find_browser_pids_for_profile(
            [Path(root) / ".chrome_profile_snap"])
    assert got == {403}, got

    # `cmdline()` 回 None（某些驅動版本會）也不得炸。
    with _psutil_is(_scan_psutil([_ScanProc(404, "chrome.exe", cmdline=None)])):
        assert ws.find_browser_pids_for_profile(
            [Path(root) / ".chrome_profile_snap"]) == set()
    print("  PASS\n")


def test_a_failed_scan_is_not_confused_with_an_empty_one():
    """掃不成 vs 掃到空的：至少要在 stderr 分得出來。

    `process_iter` 整個爆掉會印一行再回目前為止的結果——那一行是唯一的線索。
    psutil **沒安裝**那條則是安靜地回空集合，跟「掃過了，沒有殘留的瀏覽器」
    完全一樣；那筆已經列在 `test_exception_handlers._ALLOWED_SILENT_SCANS`
    裡（後果是少清一輪，可接受），這裡把它釘成**已知**行為，免得日後有人把它
    當成「掃描正常」的證據。
    """
    print("test_a_failed_scan_is_not_confused_with_an_empty_one")
    # (a) 掃描中途整個爆掉 → 出聲，而且保留已經找到的那幾筆。
    root = "D:/Work/Example"

    class _BoomIter(list):
        def __iter__(self):
            yield _ScanProc(
                501, "chrome.exe",
                ["chrome.exe", f"--user-data-dir={root}/.chrome_profile_snap"])
            raise RuntimeError("WMI query failed")

    module = types.ModuleType("psutil")
    module.NoSuchProcess = _FakeNoSuchProcess
    module.AccessDenied = _FakeAccessDenied
    module.process_iter = lambda attrs=None: _BoomIter()
    with _psutil_is(module):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            got = ws.find_browser_pids_for_profile(
                [Path(root) / ".chrome_profile_snap"])
    assert got == {501}, got
    assert "find_browser_pids_for_profile failed" in err.getvalue(), (
        "掃描爆掉必須留在 stderr，否則跟「掃到空的」分不出來")

    # (b) psutil 沒安裝 → 安靜地回空集合（已知且已列冊的取捨）。
    #     `sys.modules["x"] = None` 才是模擬「沒安裝」的正確寫法：`pop` 清掉的
    #     只是快取，下一個 import 會把真的載回來。
    with _psutil_is(None):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert ws.find_browser_pids_for_profile(
                [Path(root) / ".chrome_profile_snap"]) == set()
    assert err.getvalue() == "", (
        "現況：psutil 缺席是安靜的。要改成出聲的話這支會紅——那是好事，"
        "順手把 test_exception_handlers._ALLOWED_SILENT_SCANS 那筆一起拿掉。")
    print("  PASS\n")


# ---------------------------------------------------------------------------
# 裸跑時自己認領「有批次在跑」的存活訊號
# ---------------------------------------------------------------------------
# ⚠️ 每一支都把 `ws.WEBRUNNER_PID_FILE` 指到暫存目錄。**絕對不可以碰 repo 裡真的
# 那一個**——正式批次靠它當存活訊號，誤刪等於讓驗證程式在批次還跑著的時候開出
# 第二套瀏覽器，而那正是這批改動要防的事。


@contextlib.contextmanager
def _temp_pid_file():
    """把存活訊號檔改指到一個用完即丟的位置。"""
    original = ws.WEBRUNNER_PID_FILE
    d = Path(tempfile.mkdtemp(prefix="liveness_"))
    ws.WEBRUNNER_PID_FILE = d / "webrunner.pid"
    try:
        yield ws.WEBRUNNER_PID_FILE
    finally:
        ws.WEBRUNNER_PID_FILE = original


def test_no_signal_on_disk_means_the_bare_run_claims_it():
    """沒有人認領就自己認領，收尾再清掉。"""
    print("test_no_signal_on_disk_means_the_bare_run_claims_it")
    with _temp_pid_file() as pid_file:
        claimed = ws.claim_liveness_signal()
        assert claimed == os.getpid(), f"認領到的是 {claimed}"
        assert pid_file.read_text(encoding="utf-8") == str(os.getpid())
        ws.release_liveness_signal(claimed)
        assert not pid_file.exists(), "收尾沒有把自己那筆清掉"
    print("  PASS\n")


def test_a_live_signal_is_never_overwritten():
    """檔案記著一個**活著**的 pid（＝正常由啟動器帶起來的那條路）→ 一個位元組都不動。

    這是整批改動裡最要緊的一條：認領寫成「一律覆寫」的話，webrunner 會把父行程
    剛寫好的 pid 換成自己的，而收尾時兩邊都以為那筆是自己的。
    """
    print("test_a_live_signal_is_never_overwritten")
    with _temp_pid_file() as pid_file:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        before = pid_file.read_bytes()
        assert ws.claim_liveness_signal() is None, "不該認領"
        assert pid_file.read_bytes() == before, "別人的訊號被動到了"
        ws.release_liveness_signal(None)
        assert pid_file.read_bytes() == before, "收尾把別人的訊號刪掉了"
    print("  PASS\n")


def test_a_live_foreign_signal_is_never_overwritten():
    """檔案記著**別的**活行程的 pid——正式執行時每一次走的都是這條路。

    上面那支寫的是 `os.getpid()`，在 `other == os.getpid()` 就短路了，根本走不到
    `_chrome_slot._pid_alive(other)`。而正式執行時檔案裡放的是**轉接殼**的 pid：
    `.venv` 的 `python.exe` 是 CPython 的 venv redirector，webrunner 是它的子行程，
    所以 webrunner 看到的是一個活著、但不是自己的 pid。實測（2026-09-12，樹上
    變異）：把 `or _chrome_slot._pid_alive(other)` 整段刪掉，**舊的三支照樣全綠**
    ——正式執行每次都走的那一支，在這一格補上之前沒有任何東西在看。

    `os.getppid()` 正好是同一個形狀——在 `.venv` 底下跑 pytest 時，父行程就是那個
    轉接殼，而它會活到本行程結束。
    """
    print("test_a_live_foreign_signal_is_never_overwritten")
    foreign = os.getppid()
    assert foreign != os.getpid(), "父行程的 pid 竟然等於自己"
    assert ws._chrome_slot._pid_alive(foreign), (
        f"父行程 {foreign} 判成死的——前提不成立，下面等於沒在測")
    with _temp_pid_file() as pid_file:
        pid_file.write_text(str(foreign), encoding="utf-8")
        before = pid_file.read_bytes()
        assert ws.claim_liveness_signal() is None, (
            "檔案記著一個活著的別人（轉接殼），webrunner 卻把它當成沒人認領")
        assert pid_file.read_bytes() == before, "別人的存活訊號被覆寫了"
        ws.release_liveness_signal(None)
        assert pid_file.read_bytes() == before, "收尾把別人的訊號刪掉了"
    print("  PASS\n")


def test_a_dead_signal_does_not_block_the_claim():
    """上一輪被硬砍掉留下的死 pid 不算數，否則裸跑永遠認領不到。"""
    print("test_a_dead_signal_does_not_block_the_claim")
    with _temp_pid_file() as pid_file:
        pid_file.write_text("999999999", encoding="utf-8")
        claimed = ws.claim_liveness_signal()
        assert claimed == os.getpid(), f"認領到的是 {claimed}"
        ws.release_liveness_signal(claimed)
    print("  PASS\n")


def test_an_unreadable_signal_is_left_alone():
    """內容壞掉時**不認領、也不覆寫**——判不出來就當作有人在跑（fail-closed）。

    ⚠️ 正面對照組是「原內容還在」而不只是「沒有認領」：少了它，「保守地沒認領」跟
    「認領了但寫檔失敗」在斷言上長得一模一樣。
    """
    print("test_an_unreadable_signal_is_left_alone")
    with _temp_pid_file() as pid_file:
        pid_file.write_text("not-a-pid", encoding="utf-8")
        before = pid_file.read_bytes()
        assert ws.claim_liveness_signal() is None, "不該認領"
        assert pid_file.read_bytes() == before, "壞內容被覆寫了"
    print("  PASS\n")


def test_the_parent_overwriting_after_our_claim_still_wins_at_cleanup():
    """競態：我們先寫、父行程後寫 → 收尾**不得**刪掉父行程那一筆。

    父行程是在 `Popen` 回來**之後**才寫 pid 的（`_supervisor.stream_child`：先
    `Popen`、再 `on_spawn`），所以子行程確實有機會先寫到。收尾用的是
    「檔案還記著我那筆嗎」，與 `start_webrunner._clear_pid_if_ours` 同一個判準。
    """
    print("test_the_parent_overwriting_after_our_claim_still_wins_at_cleanup")
    with _temp_pid_file() as pid_file:
        claimed = ws.claim_liveness_signal()
        assert claimed is not None
        pid_file.write_text(str(os.getpid() + 1), encoding="utf-8")
        ws.release_liveness_signal(claimed)
        assert pid_file.exists(), "父行程的存活訊號被收尾刪掉了"
        assert pid_file.read_text(encoding="utf-8") == str(os.getpid() + 1)
    print("  PASS\n")


def test_the_signal_is_released_on_every_exit_path():
    """`entry()` 丟例外、或回非零，收尾都要跑，而且回傳值要原樣透傳。"""
    print("test_the_signal_is_released_on_every_exit_path")
    with _temp_pid_file() as pid_file:
        def boom():
            raise RuntimeError("炸了")

        try:
            ws.run_with_liveness_signal(boom)
        except RuntimeError:
            pass
        else:  # pragma: no cover
            raise AssertionError("例外被吞掉了")
        assert not pid_file.exists(), "例外之後訊號沒清掉"

        assert ws.run_with_liveness_signal(lambda: 7) == 7, "回傳值沒有透傳"
        assert not pid_file.exists(), "非零結束之後訊號沒清掉"
    print("  PASS\n")


def test_a_broken_claim_never_blocks_the_batch():
    """認領炸掉只能退化成「沒有訊號」，不可以讓 webrunner 起不來。

    這是一個純旁路訊號。它如果擋住 `main()`，supervisor 會一直重生、最後 rapid-fail
    放棄——等於拿整個批次去換一個可靠度改善。
    """
    print("test_a_broken_claim_never_blocks_the_batch")
    original = ws.claim_liveness_signal
    try:
        def explode():
            raise OSError("模擬壞掉")

        ws.claim_liveness_signal = explode
        assert ws.run_with_liveness_signal(lambda: 3) == 3, "批次被擋住了"
    finally:
        ws.claim_liveness_signal = original
    print("  PASS\n")


def test_both_variants_publish_the_liveness_signal():
    """兩個變體的 `__main__` 都要把 `main` 交給 `run_with_liveness_signal`。

    ⚠️ 正面對照組：先確認抽取器真的看得到 novelai 既有的
    `_run_setup_verification` 分支。抽不到東西的話，下面那句斷言會空轉通過。
    """
    print("test_both_variants_publish_the_liveness_signal")
    pkg = Path(__file__).resolve().parent.parent / "axiomatic"
    seen = {}
    for name in ("webrunner_novelai.py", "webrunner_je_only.py"):
        tree = ast.parse((pkg / name).read_text(encoding="utf-8"))
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(tree) if isinstance(node, ast.Call)
        }
        seen[name] = calls
        assert "ws.run_with_liveness_signal" in calls, (
            f"{name} 的 `__main__` 沒有走 `ws.run_with_liveness_signal`，"
            "裸跑時兩個互斥訊號會一個都沒有。")
    assert "_run_setup_verification" in seen["webrunner_novelai.py"], (
        "抽取器連既有的 `_run_setup_verification` 都沒抽到——它壞了，"
        "上面那兩句斷言是空轉的。")
    print("  PASS\n")


def test_the_isolated_verification_path_never_claims_the_signal():
    """`--full` 的隔離驗證不可以認領存活訊號。

    它是個短命的子行程；認領了會讓 bot 的監督者把它當成正式批次去做行程管理，
    而它跑完就沒了。結構上它走的是 `__main__` 裡**另一條**分支，這裡把那件事釘住。
    """
    print("test_the_isolated_verification_path_never_claims_the_signal")
    pkg = Path(__file__).resolve().parent.parent / "axiomatic"
    tree = ast.parse(
        (pkg / "webrunner_novelai.py").read_text(encoding="utf-8"))
    verify = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == "_run_setup_verification"), None)
    assert verify is not None, "找不到 `_run_setup_verification`"
    called = {ast.unparse(node.func)
              for node in ast.walk(verify) if isinstance(node, ast.Call)}
    for banned in ("ws.claim_liveness_signal", "claim_liveness_signal",
                   "ws.run_with_liveness_signal"):
        assert banned not in called, (
            f"隔離驗證呼叫了 {banned}——它會被監督者誤認成正式批次。")
    print("  PASS\n")


# ---------------------------------------------------------------------------
# 單張產圖「服務中」訊號（`single_image_serving`）
#
# bot 要靠它量「距離最後一次有人在服務這一筆多久」，而不是「距離送出多久」——後者
# 對帶內服務是錯的量：一次服務本身就能跑滿 `generate_max_retries` 次嘗試（預設約
# 15 分鐘），遠超過原本那個 600 秒的 TTL。
# ---------------------------------------------------------------------------

_BEAT = "single_image_serving"
_BEAT_FIELDS = {"ts", "type", "request_id", "in_band", "phase", "beat_within_sec"}


def test_the_serving_beat_is_sent_before_the_browser_is_touched():
    """「開始服務」必須是這一筆的第一則事件，而且發的時候還沒寫任何欄位。

    兩條路（帶內／閒置 one-shot）都要發：它們走同一支 `serve_single_image_request`，
    bot 那側也就只需要一條規則。驗證沒過的請求不發——它們根本不會被服務，宣稱「在
    服務」只會讓 bot 多等一段。
    """
    print("test_the_serving_beat_is_sent_before_the_browser_is_touched")
    for in_band in (True, False):
        rid = f"19a4f2c1b8e-3f9c2a1d7e0{int(in_band)}"
        port = FakeBrowserPort()
        writes_when_announced = []
        with _ServeHarness() as h, _fake_clock():
            record = ws.emit_event

            def _emit(etype, **kw):
                if etype == _BEAT and kw.get("phase") == "start":
                    writes_when_announced.append(len(port.set_values))
                record(etype, **kw)

            ws.emit_event = _emit
            ws.serve_single_image_request(
                port, {"request_id": rid, "prompt": "P", "char1": "C1"},
                in_band=in_band)
        events = list(h.events)
        kinds = [et for et, _ in events]
        assert kinds and kinds[0] == _BEAT and events[0][1]["phase"] == "start", (
            in_band, kinds)
        assert writes_when_announced == [0], (
            f"in_band={in_band}：「開始服務」發出去的時候已經寫了 "
            f"{writes_when_announced} 個欄位，那就不是「開始」了")
        assert kinds[-1] == "single_image_done", (in_band, kinds)
        assert kinds.count("single_image_done") == 1, (in_band, kinds)
        assert events[-1][1]["ok"] is True, events[-1]
        assert {kw["request_id"] for _, kw in events} == {rid}, events
        assert all(kw["in_band"] is in_band
                   for et, kw in events if et == _BEAT), (in_band, events)

    for req in ({"request_id": "19a4f2c1b8e-3f9c2a1d7e04", "prompt": "   "},
                {"request_id": "../../..", "prompt": "P"}):
        with _ServeHarness() as h:
            ws.serve_single_image_request(FakeBrowserPort(), req, in_band=True)
        assert [et for et, _ in h.events] == ["single_image_done"], (
            req, h.events)
    print("  PASS\n")


def test_a_beat_that_cannot_be_written_never_costs_the_image():
    """「服務中」寫不出去，最多讓 bot 提早放棄——**不可以**讓這張圖失敗。

    `emit_event` 自己已經吞掉寫檔／序列化的錯，所以這裡用一個會拋任意例外的替身：
    訊號是在服務的 `try` 裡、甚至在 `generate_one_image` 裡面發的，逸出的東西會被
    serve 的 broad except 接住，把一次好好的服務變成 `ok=false`。
    """
    print("test_a_beat_that_cannot_be_written_never_costs_the_image")
    for in_band in (True, False):
        port = FakeBrowserPort()
        err = io.StringIO()
        with _ServeHarness() as h, _fake_clock():
            record = ws.emit_event

            def _emit(etype, **kw):
                if etype == _BEAT:
                    raise RuntimeError("the events file said no")
                record(etype, **kw)

            ws.emit_event = _emit
            with contextlib.redirect_stderr(err):
                ws.serve_single_image_request(
                    port, {"request_id": "19a4f2c1b8e-3f9c2a1d7e05",
                           "prompt": "P"}, in_band=in_band)
            evs = h.single_image_events()
        assert len(evs) == 1 and evs[0]["ok"] is True, (in_band, evs)
        # 吞掉可以，但要留話——否則「bot 為什麼提早放棄」查不到原因。
        assert _BEAT in err.getvalue(), err.getvalue()
    print("  PASS\n")


def test_the_serving_beat_lands_on_disk_in_the_shape_the_bot_reads():
    """從 bot 那一側看：`events.ndjson` 裡的每一則「服務中」都在同一筆結果之前，
    而且鍵集合就是契約寫的那幾個。

    走**真的** `emit_event` 寫進暫存檔再逐行解析，因為 bot 讀的是檔案不是函式呼叫。
    鍵集合釘死是刻意的：bot 的派發與 `test_event_notifications` 的樣本都照它寫，
    多一個欄位就要回來想一次「bot 要不要讀它、讀了會不會洩漏」。
    """
    print("test_the_serving_beat_lands_on_disk_in_the_shape_the_bot_reads")
    import math
    import shutil
    real_emit = ws.emit_event
    tmp = Path(tempfile.mkdtemp(prefix="beat_disk_"))
    rid = "19a4f2c1b8e-3f9c2a1d7e06"
    try:
        path = tmp / "events.ndjson"
        with _ServeHarness(), _fake_clock(), _events_to(path):
            ws.emit_event = real_emit
            ws.serve_single_image_request(
                FakeBrowserPort(), {"request_id": rid, "prompt": "P"},
                in_band=True)
            records = [json.loads(line) for line in _event_lines(path)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    kinds = [r["type"] for r in records]
    assert kinds == [_BEAT, _BEAT, _BEAT, "single_image_done"], kinds
    beats = records[:-1]
    assert [b["phase"] for b in beats] == ["start", "generate", "download"], beats
    for b in beats:
        assert set(b) == _BEAT_FIELDS, f"契約的鍵集合變了：{sorted(set(b))}"
        assert b["request_id"] == rid and b["in_band"] is True, b
        promise = b["beat_within_sec"]
        assert (isinstance(promise, float) and math.isfinite(promise)
                and promise > 0), b
    assert records[-1]["request_id"] == rid and records[-1]["ok"] is True
    print("  PASS\n")


def test_an_in_band_pickup_announces_the_request_it_read_from_disk():
    """帶內撿起磁碟上的請求時，訊號帶的是**檔案裡**那個 request_id。

    那是 bot 用來對帳的鍵，它只認得自己寫進去的那個 id。從讀檔、到 serve、到發訊號
    這條線上任何一處把 id 弄丟，bot 那側就對不上，而那個「對不上」是安靜的。
    """
    print("test_an_in_band_pickup_announces_the_request_it_read_from_disk")
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="beat_req_"))
    saved_req = ws.SINGLE_IMAGE_REQUEST_FILE
    rid = "19a4f2c1b8e-3f9c2a1d7e07"
    try:
        req_file = tmp / "single_image_request.json"
        req_file.write_text(json.dumps({"request_id": rid, "prompt": "P"}),
                            encoding="utf-8")
        ws.SINGLE_IMAGE_REQUEST_FILE = req_file
        with _ServeHarness() as h, _fake_clock():
            served = ws.check_single_image_request(FakeBrowserPort())
        left_on_disk = req_file.exists()
    finally:
        ws.SINGLE_IMAGE_REQUEST_FILE = saved_req
        shutil.rmtree(tmp, ignore_errors=True)
    beats = [kw for et, kw in h.events if et == _BEAT]
    assert served is True and not left_on_disk, (served, left_on_disk)
    assert beats and beats[0]["phase"] == "start", beats
    assert {b["request_id"] for b in beats} == {rid}, beats
    assert all(b["in_band"] is True for b in beats), (
        "兩個 batch 內的呼叫點都不傳 in_band，預設就是帶內", beats)
    print("  PASS\n")


def test_a_serve_longer_than_ten_minutes_keeps_beating_within_its_promise():
    """一筆超過 600 秒的服務，每兩則訊號之間都不能超過前一則自己送出的承諾。

    這是「判定完成」的 webrunner 半邊：bot 照「距離上一則訊號
    多久 vs. 那一則帶的承諾」判斷，就不會取消一筆正在被服務的長請求。情境照正式
    最壞的健康路徑走：按鈕找到逾時前一刻、前三次嘗試都等滿逾時沒有新圖、第四次才
    出來。

    跑兩種重試間隔：預設的 (25, 30)，以及固定 (100, 100)。後者的單次嘗試是 295 秒，
    已經超過預設設定下的承諾（285 秒）——承諾若是寫死的，這一格就會紅。
    """
    print("test_a_serve_longer_than_ten_minutes_keeps_beating_within_its_promise")
    saved = (ws.load_batch_config, ws.click_generate, ws.wait_for_new_image,
             ws.get_generation_error)
    try:
        for delay in ((25.0, 30.0), (100.0, 100.0)):
            port = FakeBrowserPort()
            stamps = []
            waits = [None, None, None, "blob:late"]
            cfg = {"generate_max_retries": 4, "generate_retry_delay_sec": delay,
                   "download_max_retries": 3}
            with _ServeHarness() as h, _fake_clock() as clock:
                record = ws.emit_event

                def _emit(etype, **kw):
                    if etype in (_BEAT, "single_image_done"):
                        stamps.append((clock.monotonic(),
                                       kw.get("phase", "done"),
                                       kw.get("beat_within_sec")))
                    record(etype, **kw)

                def _slow_click(_port):
                    clock.sleep(ws.GENERATE_CLICK_TIMEOUT_SEC)
                    return True

                def _slow_wait(*_a, **_k):
                    clock.sleep(ws.GENERATE_WAIT_TIMEOUT_SEC)
                    return waits.pop(0)

                ws.emit_event = _emit
                ws.load_batch_config = lambda: dict(cfg)
                ws.click_generate = _slow_click
                ws.wait_for_new_image = _slow_wait
                ws.get_generation_error = lambda _port: None
                ws.serve_single_image_request(
                    port, {"request_id": "19a4f2c1b8e-3f9c2a1d7e08",
                           "prompt": "P"}, in_band=True)
                evs = h.single_image_events()
            phases = [phase for _, phase, _ in stamps]
            assert phases == ["start", "generate", "generate", "generate",
                              "generate", "download", "done"], (delay, phases)
            assert len(evs) == 1 and evs[0]["ok"] is True, (delay, evs)
            total = stamps[-1][0] - stamps[0][0]
            assert total > 600.0, (
                f"delay={delay}：情境本身沒有重現「超過 600 秒的服務」（只有 "
                f"{total:.0f} 秒），這一格就證明不了任何事")
            for (t0, p0, promise), (t1, p1, _) in zip(stamps, stamps[1:]):
                assert t1 - t0 <= promise, (
                    f"delay={delay}：{p0} → {p1} 隔了 {t1 - t0:.0f} 秒，超過前一則"
                    f"承諾的 {promise:.0f} 秒；bot 照承諾判斷會把這筆正在被服務的"
                    "請求取消掉")
    finally:
        (ws.load_batch_config, ws.click_generate, ws.wait_for_new_image,
         ws.get_generation_error) = saved
    print("  PASS\n")


def test_the_beat_promise_is_built_from_the_timeouts_actually_in_force():
    """承諾的輸入必須就是 `generate_one_image` 真正在用的那幾個逾時。

    上面那支長服務的測試把按鈕與等圖都換成替身（照常數睡），所以有人在
    `generate_one_image` 裡把逾時寫回字面值——例如 `timeout=300.0`——它照樣綠，
    而正式環境的單次嘗試從此比承諾長。這一支從原始碼確認兩個逾時都是**名字**、
    而且就是承諾用的那個名字，再從行為面確認承諾隨設定走、而且有正的餘裕。
    """
    print("test_the_beat_promise_is_built_from_the_timeouts_actually_in_force")
    tree = ast.parse(Path(ws.__file__).read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    gen = funcs["generate_one_image"]

    def calls_to(name):
        return [n for n in ast.walk(gen) if isinstance(n, ast.Call)
                and getattr(n.func, "id", "") == name]

    waits, clicks = calls_to("wait_for_new_image"), calls_to("click_generate")
    # 正面對照：抽不到呼叫點的話，下面每一句都會空轉通過。
    assert len(waits) == 1 and len(clicks) == 1, (len(waits), len(clicks))
    timeout = {kw.arg: kw.value for kw in waits[0].keywords}.get("timeout")
    assert (isinstance(timeout, ast.Name)
            and timeout.id == "GENERATE_WAIT_TIMEOUT_SEC"), ast.unparse(waits[0])
    assert len(clicks[0].args) == 1 and not clicks[0].keywords, (
        "`click_generate` 要用它自己的預設逾時，否則承諾算的不是實際那一個："
        f"{ast.unparse(clicks[0])}")
    click_default = funcs["click_generate"].args.defaults[-1]
    assert (isinstance(click_default, ast.Name)
            and click_default.id == "GENERATE_CLICK_TIMEOUT_SEC"), (
        ast.unparse(click_default))
    lo = ws._serving_beat_within_sec({"generate_retry_delay_sec": (25.0, 30.0)})
    hi = ws._serving_beat_within_sec({"generate_retry_delay_sec": (90.0, 100.0)})
    assert hi - lo == 70.0, (lo, hi)
    assert lo > (ws.GENERATE_CLICK_TIMEOUT_SEC + ws.GENERATE_WAIT_TIMEOUT_SEC
                 + 30.0), lo
    print("  PASS\n")


def test_no_variant_serves_single_images_on_its_own():
    """單張產圖的服務只准有共用層那一份，兩個變體都不可以自己再寫一份。

    「服務中」訊號只從 `serve_single_image_request` 發。某個變體哪天自己實作一次
    serve（或自己發 `single_image_done`），那條路就安靜地少掉訊號，而 bot 那側會把
    它正在服務的請求當成沒人理而取消——圖產出來、沒有人收。P6 之前這段就是兩份。
    """
    print("test_no_variant_serves_single_images_on_its_own")
    pkg = Path(__file__).resolve().parent.parent / "axiomatic"
    owned = {"serve_single_image_request", "check_single_image_request",
             "_refill_character_fields", "_emit_serving_beat"}
    serve_events = {_BEAT, "single_image_done"}

    def scan(name):
        tree = ast.parse((pkg / name).read_text(encoding="utf-8"))
        defs = {n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        emits = set()
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and n.args):
                continue
            f = n.func
            fname = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if (fname == "emit_event" and isinstance(n.args[0], ast.Constant)
                    and isinstance(n.args[0].value, str)):
                emits.add(n.args[0].value)
        return defs, emits

    shared_defs, shared_emits = scan("_webrunner_shared.py")
    # 正面對照：掃描器在共用層要看得到它們，否則下面對變體的斷言是空轉的。
    assert owned <= shared_defs, sorted(owned - shared_defs)
    assert serve_events <= shared_emits, sorted(serve_events - shared_emits)
    for name in ("webrunner_novelai.py", "webrunner_je_only.py"):
        defs, emits = scan(name)
        assert not defs & owned, f"{name} 自己定義了 {sorted(defs & owned)}"
        assert not emits & serve_events, (
            f"{name} 自己發了 {sorted(emits & serve_events)}")
    print("  PASS\n")


# ---------- 這一輪是批次還是單圖伺服器：模式由 argv 宣告（pass 1）------------
# 事故紀錄在 `_webrunner_shared.RUN_MODE_BATCH` 上面那一段（`webrunner.log` 第
# 1–125 行，2026-06-27）。pass 1 只加「認旗標」與「收參數」，閘門還沒換過去，所以
# 這一批測試釘的是介面與預設值，不是最終行為。

def test_parse_run_mode_without_the_flag_is_a_batch():
    """沒有旗標 ＝ 批次。這是預設，也是絕大多數 spawn 的樣子。"""
    print("test_parse_run_mode_without_the_flag_is_a_batch")
    for argv in ([], ["webrunner_novelai.py"],
                 ["webrunner_novelai.py", "--variant", "je"]):
        assert ws.parse_run_mode(argv) == ws.RUN_MODE_BATCH, argv
    print("  PASS\n")


def test_parse_run_mode_reads_the_flag_wherever_it_sits_after_argv0():
    """旗標出現在 `argv[1:]` 的任何位置（含重複）都算——它是一個是非題。"""
    print("test_parse_run_mode_reads_the_flag_wherever_it_sits_after_argv0")
    flag = ws.SINGLE_IMAGE_SERVER_FLAG
    for argv in (["prog", flag],
                 ["prog", "--variant", "je", flag],
                 ["prog", flag, "--variant", "je"],
                 ["prog", flag, flag]):
        assert ws.parse_run_mode(argv) == ws.RUN_MODE_SINGLE_IMAGE_SERVER, argv
    print("  PASS\n")


def test_parse_run_mode_never_matches_the_script_path_in_argv0():
    """`argv[0]` 是腳本路徑不是選項，所以永遠不算命中。

    這條不是潔癖：把 `argv[0]` 一起掃進來，就等於多開一個「repo 剛好被放在一個
    名字等於這個旗標的路徑底下」也能翻轉模式的開關，而那是沒有人會想到要去查的
    地方。**這也是唯一一條只有「必須擋下」的輸入才殺得掉的規則**——把切片裡的
    `[1:]` 拿掉之後，上面那兩支測試照樣全綠。
    """
    print("test_parse_run_mode_never_matches_the_script_path_in_argv0")
    flag = ws.SINGLE_IMAGE_SERVER_FLAG
    assert ws.parse_run_mode([flag]) == ws.RUN_MODE_BATCH
    assert ws.parse_run_mode([flag, "--variant", "je"]) == ws.RUN_MODE_BATCH
    # 正面對照：同一個字串挪到 argv[0] 之後就會命中，所以上面兩句擋下來的是
    # 位置，不是「這個旗標根本認不得」。
    assert (ws.parse_run_mode(["prog", flag])
            == ws.RUN_MODE_SINGLE_IMAGE_SERVER)
    print("  PASS\n")


def test_parse_run_mode_needs_the_whole_argument_to_match():
    """近似形狀一律不算命中——比對的是整個元素，不是前綴或子字串。"""
    print("test_parse_run_mode_needs_the_whole_argument_to_match")
    for arg in ("--single-image-server=1",    # 帶值的形狀，今天沒有支援
                "--no-single-image-server",   # 意思剛好相反
                "--single-image-serverx",
                "single-image-server",        # 少了前面的 `--`
                " --single-image-server"):    # 前面多一個空白
        assert ws.parse_run_mode(["prog", arg]) == ws.RUN_MODE_BATCH, arg
    print("  PASS\n")


def test_run_batch_takes_a_keyword_only_mode_defaulting_to_batch():
    """`run_batch(..., mode=...)` 存在、是 keyword-only、預設批次。

    預設值是載重的：「所有既有呼叫端（兩支變體的 `main()`、本檔的 `_run_batch`）
    一個字都不必改」這句話的全部依據就是它。keyword-only 則是為了讓 pass 2 接線
    的時候，沒辦法把模式誤傳到 `email` / `password` 的位置上。
    """
    print("test_run_batch_takes_a_keyword_only_mode_defaulting_to_batch")
    import inspect
    params = inspect.signature(ws.run_batch).parameters
    assert "mode" in params, sorted(params)
    assert params["mode"].kind is inspect.Parameter.KEYWORD_ONLY, (
        params["mode"].kind)
    assert params["mode"].default == ws.RUN_MODE_BATCH, params["mode"].default
    print("  PASS\n")


def test_run_batch_defaults_to_batch_and_says_so_in_the_log():
    """不傳 `mode` 跑一輪：確實跑了批次，而且 log 裡有一行說得出它是誰。

    那一行不是裝飾。2026-06-27 那次事故是靠 log 重建的，而當時整份 log **沒有**
    任何一句說得出「這個行程認為自己是誰」，只看得到它做了什麼。
    """
    print("test_run_batch_defaults_to_batch_and_says_so_in_the_log")
    buf = io.StringIO()
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P1"])
        h.write_queue("todo_character1.md", ["a"])
        with contextlib.redirect_stdout(buf):
            rc = _run_batch(FakeBrowserPort())       # 刻意不傳 mode
        names = [c["name"] for c in h.gen_calls]
    assert rc == 0, rc
    assert names == ["a"], names        # 預設真的走批次，不是伺服器
    assert f"webrunner run mode: {ws.RUN_MODE_BATCH}" in buf.getvalue(), (
        buf.getvalue()[-400:])
    print("  PASS\n")


def test_run_batch_falls_back_to_batch_on_an_unknown_mode():
    """認不得的 `mode` 退回批次 ＋ 印一行 stderr，不丟例外。

    方向是刻意的：誤判成批次頂多是多跑了佇列上本來就要跑的東西（pass 3 之後批次
    仍會在配對邊界 in-band 服務掉待處理的單圖請求），而誤判成伺服器正是這一整刀
    要修掉的那個事故——整條佇列被安靜跳過，還附一個假的成功 rc。丟例外更糟：時機
    在 Chrome 都開起來之後，那時多半已經超過 `rapid_fail_threshold_sec`，於是把
    一個字串打錯會變成監督者永遠重生。
    """
    print("test_run_batch_falls_back_to_batch_on_an_unknown_mode")
    out, err = io.StringIO(), io.StringIO()
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P1"])
        h.write_queue("todo_character1.md", ["a"])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ws.run_batch(FakeBrowserPort(), "email", "pw",
                              setup_fn=lambda: True,
                              minimize_fn=lambda: None,
                              mode="single_image_server")  # 底線，打錯了
        names = [c["name"] for c in h.gen_calls]
    assert rc == 0, rc
    assert names == ["a"], names
    assert "unknown run mode" in err.getvalue(), err.getvalue()
    assert f"webrunner run mode: {ws.RUN_MODE_BATCH}" in out.getvalue(), (
        out.getvalue()[-400:])
    print("  PASS\n")



# ---------- 閘門換成看旗標（pass 3）------------------------------------------
# 2026-06-27 那次事故的完整紀錄在 `_webrunner_shared.RUN_MODE_BATCH` 上面那一段。
# 這一批釘的是**最終行為**：模式由 argv 宣告，磁碟上的請求檔只決定「已經在跑的批次
# 下一個要服務什麼」，不再決定這個行程的身分。

def _server_mode_harness_polls(h, *, mode, serve_once):
    """在 `_RunBatchHarness` 裡跑一輪，回 `(rc, 每一次 poll 的 in_band, 角色名)`。

    `serve_once` 為真時第一圈服務掉一個請求（模擬磁碟上真的有一筆），之後一律空轉
    到閒置逾時。假時鐘讓那 120 秒在毫秒內走完。
    """
    polls = []

    def fake_check(port, in_band=True):
        polls.append(in_band)
        return serve_once and len(polls) == 1

    h.patch("check_single_image_request", fake_check)
    with _fake_clock():
        rc = _run_batch(FakeBrowserPort(), mode=mode)
    return rc, polls, [c["name"] for c in h.gen_calls]


def test_the_declared_server_mode_serves_and_never_enters_the_batch_loop():
    """宣告成單圖伺服器 ＋ 磁碟上有請求 → 服務它、rc=0、一個角色都不跑。

    佇列裡刻意放著兩組配對當**正面對照**：跑得起來的佇列才證明「沒有跑批次」是
    閘門擋下來的，不是根本沒東西可跑。
    """
    print("test_the_declared_server_mode_serves_and_never_enters_the_batch_loop")
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P1", "P2"])
        h.write_queue("todo_character1.md", ["a", "b"])
        ws.SINGLE_IMAGE_REQUEST_FILE.write_text(
            '{"request_id": "r1", "prompt": "p"}', encoding="utf-8")
        rc, polls, names = _server_mode_harness_polls(
            h, mode=ws.RUN_MODE_SINGLE_IMAGE_SERVER, serve_once=True)
        left_prompt = h.read_lines("todo_prompt.md")
        left_char1 = h.read_lines("todo_character1.md")
    assert rc == 0, rc
    assert names == [], f"單圖伺服器跑了批次角色：{names}"
    assert polls and all(b is False for b in polls), (
        "每一次 serve 都必須是 idle one-shot（in_band=False），"
        f"否則會把上一批的角色框留著：{polls[:5]}")
    # 佇列原封不動——這一輪從頭到尾沒有資格 pop 任何東西。
    assert left_prompt == ["P1", "P2"], left_prompt
    assert left_char1 == ["a", "b"], left_char1
    print("  PASS\n")


def test_the_declared_server_mode_serves_even_when_no_request_has_landed_yet():
    """**這一支是「好心 `and` 上 `exists()`」那個變異唯一的殺手。**

    bot 是**先 spawn、再把請求 pump 到磁碟上**的，所以一個真的單圖伺服器跑到閘門
    時，請求檔很可能還沒落地。閘門若寫成 `mode == ... and
    SINGLE_IMAGE_REQUEST_FILE.exists()`，它就會掉進批次迴圈去跑整條 todo 佇列——
    正是 2026-06-27 那個缺陷的鏡像，而且一樣安靜。

    所以這裡**不寫**請求檔，但佇列裡放著真的配對；伺服器必須照樣進 poll 迴圈、
    等到閒置逾時才 rc=0，一個角色都不能跑。
    """
    print("test_the_declared_server_mode_serves_even_when_no_request_has_landed_yet")
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P1", "P2"])
        h.write_queue("todo_character1.md", ["a", "b"])
        assert not ws.SINGLE_IMAGE_REQUEST_FILE.exists()   # 刻意什麼都還沒寫
        rc, polls, names = _server_mode_harness_polls(
            h, mode=ws.RUN_MODE_SINGLE_IMAGE_SERVER, serve_once=False)
        # 正面對照：**同一份佇列**在批次模式下確實跑得起來。少了這一句，
        # 「一個角色都沒跑」跟「佇列根本是空的」在輸出上一模一樣。
        h.gen_calls.clear()
        _, _, batch_names = _server_mode_harness_polls(
            h, mode=ws.RUN_MODE_BATCH, serve_once=False)
    assert rc == 0, rc
    assert names == [], (
        "請求檔還沒落地就掉進批次迴圈了——閘門多 `and` 了一個 `exists()`："
        f"{names}")
    assert len(polls) > 50, (
        f"伺服迴圈沒有真的等滿閒置期，只 poll 了 {len(polls)} 次")
    assert batch_names, "正面對照失敗：這份佇列在批次模式下也沒跑出任何角色"
    print("  PASS\n")


def test_a_batch_is_not_turned_into_a_server_by_a_file_on_disk():
    """沒有旗標 ＋ 磁碟上躺著請求檔 → 照樣跑批次。這就是 2026-06-27 的回歸測試。

    當時的行為是：57 組配對的 `/run` 撿到一個請求檔，服務兩張一次性圖後 rc=0 收工，
    而兩支監督者都把 rc=0 讀成「乾淨跑完」，所以整條佇列安靜地沒跑。

    ⚠️ 假時鐘不是為了跑得快，是為了**壞掉的時候快速變紅**。閘門一旦被改回看
    `exists()`（或 `or` 上它），這一輪就會掉進單圖伺服迴圈，用真時鐘的話那是
    120 秒真的 `sleep` ——測試會**卡住**而不是失敗，而卡住的測試比紅掉的測試難
    判讀得多。
    """
    print("test_a_batch_is_not_turned_into_a_server_by_a_file_on_disk")
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P1"])
        h.write_queue("todo_character1.md", ["a"])
        ws.SINGLE_IMAGE_REQUEST_FILE.write_text(
            '{"request_id": "r1", "prompt": "p"}', encoding="utf-8")
        with _fake_clock():
            rc = _run_batch(FakeBrowserPort())      # 刻意不宣告模式 ＝ 批次
        names = [c["name"] for c in h.gen_calls]
        done = h.events_of("todo_done")
    assert rc == 0, rc
    assert names == ["a"], f"批次被磁碟上那個檔劫持了：{names}"
    assert len(done) == 1, done      # 事故那次連 `todo_done` 都沒發
    print("  PASS\n")


def test_the_batch_leaves_the_request_file_alone_and_says_so_on_stderr():
    """批次路徑上**不刪**請求檔，而且要留下一行說明。

    不刪是因為那個檔是帶內服務的輸入：批次迴圈頂端與圖與圖之間的
    `check_single_image_request` 會把它撿走。印一行則是因為「批次啟動時磁碟上已經
    躺著一個請求」正是那次事故的現場特徵——log 裡要看得出這一輪是**刻意**沒有把它
    當成身分宣告，而不是根本沒注意到。

    ⚠️ 假時鐘不是為了跑得快，是為了**壞掉的時候快速變紅**。閘門一旦被改回看
    `exists()`（或 `or` 上它），這一輪就會掉進單圖伺服迴圈，用真時鐘的話那是
    120 秒真的 `sleep` ——測試會**卡住**而不是失敗，而卡住的測試比紅掉的測試難
    判讀得多。
    """
    print("test_the_batch_leaves_the_request_file_alone_and_says_so_on_stderr")
    err = io.StringIO()
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P1"])
        h.write_queue("todo_character1.md", ["a"])
        ws.SINGLE_IMAGE_REQUEST_FILE.write_text(
            '{"request_id": "r1", "prompt": "p"}', encoding="utf-8")
        with contextlib.redirect_stderr(err), _fake_clock():
            rc = _run_batch(FakeBrowserPort())
        still_there = ws.SINGLE_IMAGE_REQUEST_FILE.exists()
        text = ws.SINGLE_IMAGE_REQUEST_FILE.read_text(encoding="utf-8") \
            if still_there else ""
    assert rc == 0, rc
    assert still_there, "批次把請求檔刪掉了——那筆請求從此沒有人會服務"
    assert "r1" in text, text          # 內容也沒被動過
    assert "in-band" in err.getvalue(), err.getvalue()[-400:]
    print("  PASS\n")


def test_a_stale_request_file_no_longer_boots_chrome_for_an_empty_queue():
    """空佇列 ＋ 磁碟上一個舊請求檔 → 批次在**開 Chrome 之前**就回 rc=1。

    `run_preflight` 收的是一個布林值，兩支變體現在餵給它的是
    `mode == RUN_MODE_SINGLE_IMAGE_SERVER`（以前餵的是
    `SINGLE_IMAGE_REQUEST_FILE.exists()`）。差別就在這裡：一個沒人清掉的舊請求檔
    以前會讓一輪空佇列的批次白開一次 Chrome、再走進上面那個劫持路徑。
    """
    print("test_a_stale_request_file_no_longer_boots_chrome_for_an_empty_queue")
    out = io.StringIO()
    with _RunBatchHarness() as h:            # 四條佇列與四個 fallback 全在 tmp
        ws.SINGLE_IMAGE_REQUEST_FILE.write_text("{}", encoding="utf-8")
        with contextlib.redirect_stdout(out):
            as_batch = ws.run_preflight(
                ws.RUN_MODE_BATCH == ws.RUN_MODE_SINGLE_IMAGE_SERVER)
            # 正面對照：同樣空的佇列，宣告成伺服器就必須放行——否則上面那個 False
            # 可能只是因為 preflight 從頭到尾都回 False。
            as_server = ws.run_preflight(
                ws.RUN_MODE_SINGLE_IMAGE_SERVER
                == ws.RUN_MODE_SINGLE_IMAGE_SERVER)
        _ = h
    assert as_batch is False, "空佇列的批次仍然會開 Chrome"
    assert as_server is True, "宣告成單圖伺服器卻被 preflight 擋下來了"
    print("  PASS\n")


def _gate_ifs(source: str):
    """找出 `run_batch` 裡「主體會呼叫 `_serve_single_image_queue`」的那個 `if`。"""
    fn = next((n for n in ast.walk(ast.parse(source))
               if isinstance(n, ast.FunctionDef) and n.name == "run_batch"), None)
    assert fn is not None, "抽不到 run_batch——擷取器壞了"
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        called = {c.func.id for c in ast.walk(node)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        if "_serve_single_image_queue" in called:
            out.append(node)
    return out


def test_the_server_gate_looks_at_the_declared_mode_and_nothing_else():
    """閘門必須**恰好**是 `mode == RUN_MODE_SINGLE_IMAGE_SERVER`。

    行為測試擋得住「加了 `and exists()`」，但擋不住「加了 `or exists()`」——那個形狀
    在行為上跟今天完全一樣，只是把 2026-06-27 的劫持路徑原封不動加回來。所以這裡直接
    比對判斷式的形狀。
    """
    print("test_the_server_gate_looks_at_the_declared_mode_and_nothing_else")
    want = ast.dump(ast.parse("mode == RUN_MODE_SINGLE_IMAGE_SERVER",
                              mode="eval").body)
    gates = _gate_ifs(Path(ws.__file__).read_text(encoding="utf-8"))
    assert len(gates) == 1, f"閘門抽到 {len(gates)} 個，應該剛好一個"
    assert ast.dump(gates[0].test) == want, (
        "閘門的判斷式被改過了。**不可以**再 `and` 上 "
        "`SINGLE_IMAGE_REQUEST_FILE.exists()`（真的伺服器跑到這裡時檔案可能還沒"
        "落地），也不可以 `or` 上它（那是把事故路徑加回來）。"
        f"實際：{ast.unparse(gates[0].test)}")
    # 正面對照：同一支擷取器對兩個變異都咬得到，所以上面那句不是恆真。
    for mutant in ("mode == RUN_MODE_SINGLE_IMAGE_SERVER and "
                   "SINGLE_IMAGE_REQUEST_FILE.exists()",
                   "mode == RUN_MODE_SINGLE_IMAGE_SERVER or "
                   "SINGLE_IMAGE_REQUEST_FILE.exists()"):
        fake = ("def run_batch(port, *, mode):\n"
                f"    if {mutant}:\n"
                "        _serve_single_image_queue(port)\n"
                "        return 0\n")
        got = _gate_ifs(fake)
        assert len(got) == 1 and ast.dump(got[0].test) != want, mutant
    print("  PASS\n")


def _variant_main(name: str):
    path = Path(ws.__file__).resolve().parent / name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert fn is not None, f"{name} 抽不到 main()"
    return tree, fn


def test_both_variants_declare_the_run_mode_and_thread_it_through():
    """兩支變體都要：從 argv 算出 `mode`、餵給 `run_preflight`、傳進 `run_batch`。

    ⚠️ 只改一邊是**完全無聲**的：`/run` 預設跑的是 je 變體，而 selenium 那支才是
    長跑用的，所以漏掉哪一邊都要等到那條路真的被走到才會發現。
    """
    print("test_both_variants_declare_the_run_mode_and_thread_it_through")
    for name in ("webrunner_novelai.py", "webrunner_je_only.py"):
        _, fn = _variant_main(name)
        srcs = {ast.unparse(n) for n in ast.walk(fn)}
        assert "mode = ws.parse_run_mode(sys.argv)" in srcs, name

        pre = [c for c in ast.walk(fn)
               if isinstance(c, ast.Call) and ast.unparse(c.func)
               == "ws.run_preflight"]
        assert len(pre) == 1, f"{name}: run_preflight 呼叫抽到 {len(pre)} 個"
        arg = ast.unparse(pre[0].args[0])
        assert arg == "mode == ws.RUN_MODE_SINGLE_IMAGE_SERVER", (
            f"{name}: preflight 的判準仍然是推論出來的 → {arg}")
        assert "exists" not in arg, f"{name}: {arg}"

        run = [c for c in ast.walk(fn)
               if isinstance(c, ast.Call) and ast.unparse(c.func) == "ws.run_batch"]
        assert len(run) == 1, f"{name}: run_batch 呼叫抽到 {len(run)} 個"
        kw = {k.arg: ast.unparse(k.value) for k in run[0].keywords}
        assert kw.get("mode") == "mode", f"{name}: run_batch 沒有帶 mode → {kw}"
    print("  PASS\n")


def test_neither_variant_keeps_its_own_copy_of_the_request_file_constant():
    """兩支變體都不得再自己持有 `SINGLE_IMAGE_REQUEST_FILE`。

    ⚠️ **只從一邊刪掉是看不見的。** `test_variant_parity._module_constants` 只收
    `ast.literal_eval` 讀得懂的常數，而這一個是 `PROJECT_ROOT / "..."`（BinOp），
    根本沒被收進去；它也不在 `_CORE_SHARED_CONSTANTS` 裡。所以這條規則只有這支測試
    在看。
    """
    print("test_neither_variant_keeps_its_own_copy_of_the_request_file_constant")

    def module_assigns(tree):
        return {t.id for n in tree.body if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}

    for name in ("webrunner_novelai.py", "webrunner_je_only.py"):
        tree, _ = _variant_main(name)
        names = module_assigns(tree)
        # 正面對照：擷取器真的看得到這個模組的常數（空集合會讓下面那句恆真）。
        assert "LOGIN_URL" in names and len(names) >= 20, (
            f"{name}: 擷取器只看到 {len(names)} 個模組層常數，壞了")
        assert "SINGLE_IMAGE_REQUEST_FILE" not in names, (
            f"{name} 又自己抄了一份 SINGLE_IMAGE_REQUEST_FILE——"
            "身分改由 argv 宣告之後，變體沒有任何理由碰這個檔")
    # 共用層仍然持有它（帶內服務與伺服迴圈都靠它），bot 也各持一份。
    shared = module_assigns(ast.parse(
        Path(ws.__file__).read_text(encoding="utf-8")))
    assert "SINGLE_IMAGE_REQUEST_FILE" in shared, (
        "共用層把它一起刪掉了——帶內服務會整個壞掉")
    print("  PASS\n")


if __name__ == "__main__":
    _run_all()
