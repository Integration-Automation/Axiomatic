"""派發層剝 code fence——每一個指令的自由文字參數都先經過它。

`discord_bot._strip_surrounding_code_fence` 的 docstring 有 19 行、寫著八條規則，
而到 2026-09-20 為止**一支測試都沒有**（全樹 grep 零命中），26 行敘述裡 18 行從來
沒有被執行過。位置讓這件事比數字更嚴重：它在**派發層**，`!`／`@bot`／斜線三個表面
的自由文字參數都會先經過它，包括整段貼給 Dorossi 的問題。剝錯一個字元，使用者送出
的內容就被改掉，而沒有任何地方會說一句話。

兩組：

* **規格那一組**照著 docstring 逐條釘。那份 docstring 是唯一的權威，而規格與實作
  從來沒有被放在一起比對過。
* **前提那一組**問「整段被包住」這個判準本身。它實際上是
  `startswith("```") and endswith("```")`，而那句話對**兩個相鄰的區塊**同樣成立：
  第一個區塊的開頭與最後一個區塊的結尾把整段夾住了，於是外圍那一對被剝掉、中間
  那兩個留在原地，內容變成一團壞掉的東西。docstring 講的正好相反——「不是整段被
  包住的，原樣回傳」，理由還寫著「Dorossi 問題裡內嵌的程式碼區塊必須保留」。

單反引號那一支早就有這道守門（剝完之後 `inner` 還含反引號就原樣回傳），三重那一支
沒有。同一條規則的兩個實作，其中一個漏掉，而兩邊各自都是綠的。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord_bot as b  # noqa: E402

FENCE = "```"
strip_fence = b._strip_surrounding_code_fence


# --------------------------------------------------------------------------
# 規格：docstring 的八條規則
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    # 多行、沒有語言標籤
    (f"{FENCE}\ncode\n{FENCE}", "code"),
    # 有語言標籤 → 那一整行丟掉
    (f"{FENCE}python\nx = 1\n{FENCE}", "x = 1"),
    # 單行型 ```code```
    (f"{FENCE}x{FENCE}", "x"),
    # 第一行帶空白 → 它不是語言標籤，是內容
    (f"{FENCE}hello world\nsecond\n{FENCE}", "hello world\nsecond"),
    # 偵測前先 strip 外圍空白
    (f"  \n{FENCE}\ncode\n{FENCE}\n  ", "code"),
    # 只剝外圍一層：內層的 inline span 留著
    (f"{FENCE}\n`x`\n{FENCE}", "`x`"),
    # 單一反引號的 inline span
    ("`inline`", "inline"),
])
def test_a_wrapped_input_loses_exactly_its_wrapper(raw, expected):
    """整段被一層 fence 包住時，剝掉的只有那一層。"""
    assert strip_fence(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "plain text",
    "  plain text  ",                      # 不剝時連外圍空白都不動
    f"  {FENCE}x{FENCE} and more  ",
    f"explain {FENCE}x{FENCE}",            # 前面還有字
    f"{FENCE}x{FENCE} and more",           # 後面還有字
    FENCE,                                 # 只有一個 fence
    FENCE + FENCE,                         # ``````：fence 內空無一物
    f"{FENCE}\n\n{FENCE}",                 # 內容只有換行
    f"{FENCE}python\n{FENCE}",             # 只有語言標籤，沒有內容
    "`",                                   # 退化
    "``",
    "``x``",                               # 雙反引號不是本函式管的形狀
    "`a` and `b`",                         # 兩個 inline span，不是整段包住
    "````x````",                           # 四重：剝掉外三層之後還黏著邊界
    "````\n x \n````",
])
def test_an_input_that_is_not_wholly_wrapped_comes_back_untouched(raw):
    """模稜兩可與退化輸入一律原值回傳——**原值，不是 strip 過的值**。

    這一點值得單獨講：不剝的時候回的是 `text` 而不是 `stripped`，所以前後的空白
    原封不動。派發層對「使用者到底打了什麼」是有意見的，偷偷 trim 也是改內容。
    帶空白的那兩格是後補的——第一版這段話寫在 docstring 裡，而**沒有任何一個輸入
    前後帶空白**，所以「回傳 `stripped`」那個變異活了下來。說明寫了不等於測到。

    最後兩格（四重反引號）踩的是**另一道**守門——剝掉外三層之後內層還黏著反引號
    邊界。它跟「內層還有三重分隔符號」那一道相鄰但不重疊：四重那兩個輸入的 `inner`
    是 `` `x` ``，裡面沒有三重分隔符號，所以新的那道抓不到它。兩道守門不會互相遮蔽
    ——刻意確認過，否則拿掉其中一道會沒有症狀。
    """
    assert strip_fence(raw) == raw


def test_only_one_layer_comes_off_per_call():
    """「只剝一層」代表這支函式**刻意不是冪等的**。

    派發層只呼叫它一次。有人把它「補強」成迴圈剝到底，看起來更乾淨，實際上會把
    使用者真正想送出的 inline span 也吃掉——而那種改動不會有任何測試變紅，除非
    像這樣把兩次呼叫的差別寫下來。
    """
    once = strip_fence(f"{FENCE}\n`x`\n{FENCE}")
    assert once == "`x`"
    assert strip_fence(once) == "x"


# --------------------------------------------------------------------------
# 前提：「整段被包住」這個判準
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    f"{FENCE}a{FENCE}\n{FENCE}b{FENCE}",
    f"{FENCE}\nold\n{FENCE}\n{FENCE}\nnew\n{FENCE}",
    f"{FENCE}python\nbefore\n{FENCE}\n{FENCE}python\nafter\n{FENCE}",
    f"{FENCE}a{FENCE} {FENCE}b{FENCE}",        # 同一行上的兩個
])
def test_two_separate_blocks_are_not_one_wrapper(raw):
    """兩個相鄰的區塊會滿足「開頭是 fence、結尾也是 fence」，但它們不是一層包裝。

    實測（2026-09-20，修之前）：

        '```py\\nbefore\\n```\\n```py\\nafter\\n```'
            → 'before\\n```py\\nafter'

    第一個區塊的語言標籤被當成外圍那一行吃掉、外圍那一對 fence 被剝掉，中間兩個
    留在原地——送出去的東西既不是使用者打的，也不是合法的 markdown。這正是
    docstring 說「不是整段被包住的，原樣回傳」要擋的情形，而它擋不到。
    """
    assert strip_fence(raw) == raw


def test_the_inner_delimiter_guard_exists_on_both_branches():
    """單反引號那一支有這道守門，三重那一支沒有——同一條規則的兩個實作。

    兩個輸入的形狀完全相同（整段的頭尾都是分隔符號，中間還有一對），差別只在
    分隔符號是一個反引號還是三個。答案必須一樣，否則就是其中一邊漏了。
    """
    inline = "`a` `b`"
    fenced = f"{FENCE}a{FENCE} {FENCE}b{FENCE}"
    assert strip_fence(inline) == inline
    assert strip_fence(fenced) == fenced


def test_a_single_backtick_inside_a_fence_is_still_content():
    """反面欄杆：擋兩個區塊的判準必須看**三重**分隔符號，不是「有沒有反引號」。

    把它寫成「`inner` 裡有任何反引號就不剝」會過掉上面那幾支，卻把最常見的用法
    弄壞——在一段程式碼裡提到 `x` 是再正常不過的事。
    """
    assert strip_fence(f"{FENCE}\nuse `x` here\n{FENCE}") == "use `x` here"
    assert strip_fence(f"{FENCE}py\nprint(`x`)\n{FENCE}") == "print(`x`)"
