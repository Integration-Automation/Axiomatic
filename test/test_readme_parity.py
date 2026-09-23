"""四份 README 必須是**同一份文件的四個語言**，不是四份各自漂移的文件。

翻譯的失效形態是安靜的：有人在英文那份加一節、改一個指令名、改一個檔名，其他三份
就地過期，而**沒有任何症狀**——每一份自己讀起來都通順，只有同時讀兩份的人才會發現
它們講的已經不是同一件事。這個 repo 對這個形狀有既有的判語（兩份平行清單，總有一份
會被遺忘），所以這裡把它變成守門。

釘四件事：

1. **章節結構逐節對齊。** 標題文字本來就會因語言而異，所以對齊的依據是每個標題上面
   那一行 `<!-- section: <key> -->` 標記——語言無關、看得見、而且新增一節時不寫就會
   被抓到（標題與標記的數量要一致）。
2. **指令表逐字對齊。** 斜線指令的 token 是語言無關的，所以四份必須寫出**完全相同**
   的那一組。少一個的那一份就是一份會讓人找不到功能的文件。
3. **語言切換器四份都在，而且互相連得回來。** 只有英文那份有切換器的話，讀者進到
   翻譯版就出不去了。
4. **只有英文那份把自己標成當前語言。** 每一份都要標，而且標的必須是自己。

這裡不比對散文——那是翻譯，本來就該不一樣。
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 檔名 → 切換器裡它自己的名稱。**英文是主要語言**，所以它叫 `README.md`。
READMES = {
    "README.md": "English",
    "README.zh-TW.md": "繁體中文",
    "README.zh-CN.md": "简体中文",
    "README.ja.md": "日本語",
}
PRIMARY = "README.md"

_SECTION_RE = re.compile(r"<!--\s*section:\s*([a-z0-9-]+)\s*-->")
_HEADING_RE = re.compile(r"^##\s+\S", re.MULTILINE)
# 與 `test_docs_sync._SLASH_TOKEN_RE` 同一條規則：錨在反引號，最多三段，最後一段
# 允許 `a|b|c` 併寫。刻意各有一份——那一支問的是「指令有沒有被寫進文件」，這一支
# 問的是「四份文件寫的是不是同一組」。
_SLASH_TOKEN_RE = re.compile(
    r"`/([a-z0-9_]+(?:\s+[a-z0-9_]+){0,2}(?:\\?\|[a-z0-9_]+)*)")


def _text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _sections(text: str) -> list[str]:
    return _SECTION_RE.findall(text)


def _slash_tokens(text: str) -> set[str]:
    return {" ".join(m.split()) for m in _SLASH_TOKEN_RE.findall(text)}


def test_every_readme_exists():
    """正面對照：少一個檔案時，下面每一支都會以「空集合相等」的方式通過。"""
    missing = sorted(name for name in READMES if not (REPO_ROOT / name).is_file())
    assert not missing, f"少了這幾份 README：{missing}"


@pytest.mark.parametrize("name", sorted(READMES))
def test_every_section_marker_has_a_heading(name):
    """標記與 `##` 標題一對一。

    漏寫標記的那一節對下面的結構比對是**隱形的**，於是「四份結構一致」會在一份
    多了一整節的情況下照樣通過。
    """
    text = _text(name)
    markers = _sections(text)
    headings = _HEADING_RE.findall(text)
    assert len(markers) == len(headings), (
        f"{name} 有 {len(headings)} 個 `##` 標題，但只有 {len(markers)} 個 "
        "`<!-- section: … -->` 標記。每個 `##` 標題上面都要有一個，否則那一節"
        "不會被結構比對看到。")
    assert len(markers) >= 8, f"{name} 只抽到 {len(markers)} 節，抽取器可能壞了"


@pytest.mark.parametrize("name", sorted(set(READMES) - {PRIMARY}))
def test_the_translations_have_the_same_sections_in_the_same_order(name):
    """章節序列逐節相同。順序也算——目錄與內文的閱讀順序是內容的一部分。"""
    primary = _sections(_text(PRIMARY))
    other = _sections(_text(name))
    assert other == primary, (
        f"{name} 的章節序列跟 {PRIMARY} 對不上。\n"
        f"  {PRIMARY}: {primary}\n  {name}: {other}\n"
        "加一節就要四份一起加（標記相同，標題各自翻譯）。")


@pytest.mark.parametrize("name", sorted(set(READMES) - {PRIMARY}))
def test_the_translations_document_the_same_commands(name):
    """指令 token 是語言無關的，所以四份必須寫出完全相同的那一組。

    少掉的那一份不會報錯，只會讓讀那個語言的人找不到某個功能；多出來的那一份會教
    一個不存在的指令。兩個方向都要看。
    """
    primary = _slash_tokens(_text(PRIMARY))
    other = _slash_tokens(_text(name))
    assert len(primary) >= 30, (
        f"{PRIMARY} 只抽到 {len(primary)} 個指令 token——抽取器壞了，"
        "下面的比對會真空通過。")
    missing = sorted(primary - other)
    extra = sorted(other - primary)
    assert not missing and not extra, (
        f"{name} 的指令表跟 {PRIMARY} 對不上。\n  少了：{missing}\n  多了：{extra}")


@pytest.mark.parametrize("name", sorted(READMES))
def test_every_readme_links_to_every_other_language(name):
    """切換器四份都要有，而且要連得回其他三個語言。

    只有主要語言有切換器的話，讀者進了翻譯版就出不去——那是「四份同一份文件」這件事
    在使用上唯一看得見的接縫。
    """
    head = "\n".join(_text(name).splitlines()[:8])
    for other, label in sorted(READMES.items()):
        if other == name:
            continue
        assert f"({other})" in head, f"{name} 的切換器沒有連到 {other}"
        assert label in head, f"{name} 的切換器沒有寫出 {label}"


@pytest.mark.parametrize("name", sorted(READMES))
def test_every_readme_marks_itself_as_the_current_language(name):
    """自己那一格要粗體、而且不是連結；標錯的話四份會有兩份自稱同一個語言。"""
    head = "\n".join(_text(name).splitlines()[:8])
    label = READMES[name]
    assert f"**{label}**" in head, f"{name} 沒有把 {label} 標成目前的語言"
    assert f"[{label}]" not in head, (
        f"{name} 把自己的語言也寫成連結了——那一格應該是粗體純文字")
    for other, other_label in READMES.items():
        if other == name:
            continue
        assert f"**{other_label}**" not in head, (
            f"{name} 把 {other_label} 也標成了目前的語言")


def test_english_is_the_default_readme():
    """`README.md` 是英文版：那是進到這個 repo 的人第一眼看到的東西。"""
    head = "\n".join(_text(PRIMARY).splitlines()[:8])
    assert "**English**" in head
    assert "[English]" not in head


def test_the_section_extractor_actually_bites():
    """合成對照：真實資料是乾淨的，所以上面幾支**刪掉也會綠**。

    三種輸入各問一次——標記抽得出來、順序不同會被看見、指令 token 的差集兩個方向
    都算得出來。
    """
    a = "<!-- section: one -->\n## A\n`/run` `/todo prompt add|list`\n"
    b = "<!-- section: one -->\n## B\n`/run`\n"
    c = "<!-- section: two -->\n<!-- section: one -->\n## C\n## D\n"
    assert _sections(a) == ["one"]
    assert _sections(c) == ["two", "one"] != _sections(a)
    assert _slash_tokens(a) == {"run", "todo prompt add|list"}
    assert _slash_tokens(a) - _slash_tokens(b) == {"todo prompt add|list"}
    assert _slash_tokens(b) - _slash_tokens(a) == set()
