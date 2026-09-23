"""指令表面與文件不得失同步。

斜線指令是**唯一對外宣傳的介面**；`!` 與 `@bot` 留著當隱藏的相容路徑，但不寫進
任何使用者文件。這支測試因此守四件事：

1. **覆蓋率**——每個斜線指令（含群內子指令）都要在三語 help ＋ `README.md` ＋
   `COMMANDS.md` ＋ `docs/commands_*.md` ＋ `commands/*.md`（產生出來的）出現，
   也就是 `CLAUDE.md` DoD #2 的五份語料；反向也驗孤兒條目。
2. **隱藏面不得外洩**——使用者文件裡不得再出現 `!cmd` 或 `@bot <子指令>`。少了
   這一條，`!` 會慢慢爬回文件裡，三個表面又變成三份要維護的東西。
3. **相容面不得斷**——`!` 與 `@bot` 的派發表必須還在，而且**每一個**都要有對應
   的斜線指令（靠宣告上的 `extras={"bang": …}` 比對）。漏掉一個就代表有功能只
   剩沒人看得到的入口。
4. **結構與數字**——頂層指令不得超過平台上限 100（指令群算一個、群內子指令不
   計）、每群子項 ≤25、巢狀 ≤2 層、名稱合法；文件裡引用的數字要與實際相符。

指令一律用 AST 從宣告抽出來，不手寫清單：新增指令自動納入守備範圍。
"""
import ast
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _help_strings as HELP  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
# 測試 2026-09-22 起住在 repo 根目錄的 `test/`（本檔所在的目錄），不在套件裡。
TEST_ROOT = Path(__file__).resolve().parent
BOT_SOURCE = PKG_ROOT / "discord_bot.py"

# 對話平台的硬上限：每個應用程式 100 個**頂層** CHAT_INPUT 指令。
SLASH_TOP_LEVEL_LIMIT = 100

def _bot_tree() -> ast.Module:
    return ast.parse(BOT_SOURCE.read_text(encoding="utf-8"), str(BOT_SOURCE))


def _named_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"discord_bot.py 裡找不到 `{name}`，抽取邏輯要跟著改")


def _bang_commands() -> tuple[list[str], set[str]]:
    """從 `on_message` 的派發鏈抽出 `!` 指令。

    回 `(primary, aliases)`：`head in ("!character1_add", "!c1")` 的第一個是正式
    名稱、其餘是別名（斜線指令只鏡射正式名稱，help 也是這樣寫的）。
    """
    dispatcher = _named_function(_bot_tree(), "on_message")
    primary: list[str] = []
    alias: set[str] = set()
    for node in ast.walk(dispatcher):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "head":
            continue
        values: list[str] = []
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) \
                    and isinstance(comparator.value, str):
                values.append(comparator.value)
            elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                values += [e.value for e in comparator.elts
                           if isinstance(e, ast.Constant)
                           and isinstance(e.value, str)]
        values = [v for v in values if v.startswith("!")]
        if not values:
            continue
        primary.append(values[0])
        alias.update(values[1:])
    unique_primary = sorted(set(primary))
    return unique_primary, alias - set(unique_primary)


def _mention_commands() -> set[str]:
    """`_handle_mention` 的 `handlers` 字典鍵。"""
    handler_fn = _named_function(_bot_tree(), "_handle_mention")
    for node in ast.walk(handler_fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict) \
                and any(isinstance(t, ast.Name) and t.id == "handlers"
                        for t in node.targets):
            return {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    raise AssertionError("`_handle_mention` 裡找不到 `handlers` 字典")


def _dotted_name(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _group_vars(tree: ast.Module) -> dict[str, tuple[str, str | None]]:
    """`變數名 -> (指令群名稱, 父群變數名)`。

    只認 `Group(..., parent=<Name>)` 這個建構寫法，**不支援**用 add_command 掛
    子群：`parent=` 是同一個 AST 節點上的關鍵字，抽取器一眼看得到；分成兩處寫
    就變成要跨節點追蹤，而那正是抽取器會悄悄漏掉東西的地方。
    """
    found: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if not _dotted_name(node.value.func).endswith("Group"):
            continue
        name = parent = None
        for keyword in node.value.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                name = keyword.value.value
            if keyword.arg == "parent" and isinstance(keyword.value, ast.Name):
                parent = keyword.value.id
        if name is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = (name, parent)
    return found


def _slash_surface() -> tuple[set[str], set[str], set[str], set[str], dict]:
    """回 `(頂層指令, 頂層指令群, 子群完整名, 葉指令完整名, 完整名->extras)`。

    舊版只看頂層（`tree.command` ＋ `tree.add_command`），因為當時只需要守平台
    上限。現在斜線是唯一表面，覆蓋率要比對到**每一個葉指令**，所以要一路走進
    群裡面。
    """
    tree = _bot_tree()
    gvars = _group_vars(tree)

    def path_of(var: str) -> list[str]:
        name, parent = gvars[var]
        return (path_of(parent) + [name]) if parent else [name]

    flat: set[str] = set()
    registered: set[str] = set()
    leaves: set[str] = set()
    extras: dict[str, dict] = {}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                dotted = _dotted_name(decorator.func)
                if not dotted.endswith(".command"):
                    continue
                owner = dotted[: -len(".command")]
                name = node.name
                extra: dict = {}
                for keyword in decorator.keywords:
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                        name = keyword.value.value
                    if keyword.arg == "extras":
                        try:
                            extra = ast.literal_eval(keyword.value)
                        except ValueError:
                            extra = {}
                if owner == "tree":
                    flat.add(name)
                    leaves.add(name)
                    extras[name] = extra
                elif owner in gvars:
                    qualified = " ".join(path_of(owner) + [name])
                    leaves.add(qualified)
                    extras[qualified] = extra
        if isinstance(node, ast.Call) and _dotted_name(node.func) == "tree.add_command":
            if node.args and isinstance(node.args[0], ast.Name):
                registered.add(node.args[0].id)

    groups_top = {gvars[v][0] for v in registered}
    subgroups = {" ".join(path_of(v)) for v, (_n, parent) in gvars.items()
                 if parent is not None}
    # 沒有被 tree.add_command 掛上、也沒有 parent 的群等於**完全看不見**：它底下
    # 的指令不會同步出去，而且不會有任何錯誤。
    orphan = sorted(name for var, (name, parent) in gvars.items()
                    if parent is None and var not in registered)
    assert not orphan, (
        f"這些指令群沒有 `tree.add_command`，也沒有 parent：{orphan}。"
        "它們底下的指令不會被同步出去，而且不會有任何錯誤訊息。")
    return flat, groups_top, subgroups, leaves, extras


BANG_PRIMARY, BANG_ALIASES = _bang_commands()
MENTION_COMMANDS = _mention_commands()
SLASH_FLAT, SLASH_GROUPS_TOP, SLASH_SUBGROUPS, SLASH_QUALIFIED, SLASH_EXTRAS = _slash_surface()


# --------------------------------------------------------------------------
# 文字語料
# --------------------------------------------------------------------------
def _doc(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _sphinx_command_docs(root=REPO_ROOT) -> list:
    """`docs/commands_*.md`——**用 glob，不是寫死兩個檔名**。

    `CLAUDE.md` DoD #2 寫的就是 `docs/commands_*.md`，而 `_coverage_corpora` 裡
    `commands/*.md` 那一份早就寫明「寫死清單的話，新增的那一檔會靜默地不受任何
    檢查」。這一份原本卻在**兩處**各寫死一對檔名（覆蓋率、群描述逐字），同一個
    教訓只套用在隔壁那份語料上（2026-09-12 改）。

    下限 2 是實測值：限頻道／跨頻道各一份，少了任一份覆蓋率必然缺一半，所以抽
    不到就要吵，而不是當成一份比較短的語料。`root` 可換，是為了讓
    `test_the_sphinx_doc_glob_and_its_floor_actually_bite` 餵得進合成目錄——真實
    資料永遠抽到兩份，下限只餵乾淨資料的話跟一句註解沒有差別。
    """
    files = sorted((root / "docs").glob("commands_*.md"))
    assert len(files) >= 2, (
        f"`docs/commands_*.md` 只抽到 {[p.name for p in files]}——限頻道／跨頻道"
        "應該各一份，這批文件是守門語料的一部分")
    return files


def _coverage_corpora() -> dict[str, str]:
    """覆蓋率用：三語 help 各併成一包，文件各自一份。"""
    out = {}
    for lang_suffix, label in (("", "en"), ("_ZH_TW", "zh-tw"), ("_ZH_CN", "zh-cn")):
        out[f"help[{label}]"] = "\n".join(
            "\n".join(getattr(HELP, f"{kind}{lang_suffix}"))
            for kind in ("CHANNEL_HELP_SECTIONS", "MENTION_HELP_SECTIONS"))
    for name in ("README.md", "COMMANDS.md"):
        out[name] = _doc(name)
    # Sphinx 那邊刻意拆成「限頻道 / 跨頻道」兩份，跟三語 help 同一個結構，所以
    # 覆蓋率也要合起來看——單看一份必然缺另一半。
    out["docs/commands_*.md"] = "\n".join(
        path.read_text(encoding="utf-8") for path in _sphinx_command_docs())
    # `commands/` 一個指令群一檔（＋索引＋直接指令），所以同樣要併起來看：
    # 單獨一檔本來就只涵蓋自己那一群。**用 glob 而不是寫死清單**——新增指令群
    # 時會多一個檔，寫死的話那一檔會靜默地不受任何檢查。
    files = sorted((REPO_ROOT / "commands").glob("*.md"))
    assert files, "commands/ 是空的或不存在；那批文件是守門語料的一部分"
    out["commands/*.md"] = "\n".join(
        path.read_text(encoding="utf-8") for path in files)
    return out


_BANG_TOKEN_RE = re.compile(r"![A-Za-z0-9_]+[=+]?")
# `/群 子群 指令`，最後一段允許 `a|b|c` 併寫。文件因此可以用
# `/todo prompt add|list|remove` 一行講完十個指令，覆蓋率仍然是逐一比對的。
#
# **必須錨定在反引號開頭**：文件裡到處都是 `docs/_build/html/index.html`、
# `axiomatic/discord_bot.py` 這種路徑，不錨定的話每個路徑片段都會被當成不存在
# 的指令而報孤兒。markdown 表格裡的 `|` 要跳脫成 `\|`，所以兩種都收。
_SLASH_TOKEN_RE = re.compile(
    r"`/([a-z0-9_]+(?:\s+[a-z0-9_]+){0,2}(?:\\?\|[a-z0-9_]+)*)")


def _expand_slash_token(token: str) -> set[str]:
    """`todo prompt add|list` -> {`todo prompt add`, `todo prompt list`}。"""
    parts = token.replace(chr(92) + "|", "|").split()
    if not parts:
        return set()
    head, last = parts[:-1], parts[-1]
    return {" ".join(head + [alt]) for alt in last.split("|") if alt}


def _documented_slash(text: str) -> set[str]:
    found: set[str] = set()
    for match in _SLASH_TOKEN_RE.findall(text):
        found |= _expand_slash_token(" ".join(match.split()))
    return found


# --------------------------------------------------------------------------
# 抽取本身要站得住腳
# --------------------------------------------------------------------------
def test_extraction_found_a_plausible_command_surface():
    assert len(SLASH_QUALIFIED) >= 200, (
        f"只抽到 {len(SLASH_QUALIFIED)} 個斜線指令；宣告寫法可能換了，抽取邏輯"
        "要跟著改，否則覆蓋率測試等於沒作用。")
    assert len(SLASH_FLAT) + len(SLASH_GROUPS_TOP) >= 30
    assert len(SLASH_GROUPS_TOP) >= 20, "沒抽到足夠的指令群"
    assert SLASH_SUBGROUPS, "沒抽到任何子群——巢狀抽取可能壞了"


def test_hidden_compat_surface_is_intact():
    """`!` 與 `@bot` 留著當隱藏相容路徑（使用者裁定），不能被順手刪掉。"""
    assert len(BANG_PRIMARY) >= 100, (
        f"只抽到 {len(BANG_PRIMARY)} 個 `!` 指令；`on_message` 的派發寫法可能"
        "換了，或相容路徑被移除了。")
    assert len(MENTION_COMMANDS) >= 15


# --------------------------------------------------------------------------
# 覆蓋率：斜線指令 → 文件
# --------------------------------------------------------------------------
@pytest.mark.parametrize("corpus_name", sorted(_coverage_corpora()))
def test_every_slash_command_is_documented(corpus_name):
    documented = _documented_slash(_coverage_corpora()[corpus_name])
    missing = sorted(SLASH_QUALIFIED - documented)
    assert not missing, (
        f"{corpus_name} 沒有寫到這些斜線指令：{missing[:20]}"
        f"{'…' if len(missing) > 20 else ''}（共 {len(missing)} 個）。"
        "DoD #2／#3：新指令必須同步寫進三語 help、README.md、COMMANDS.md 與 "
        "docs/commands_*.md。可以用 `/todo prompt add|list` 併寫。")


@pytest.mark.parametrize("corpus_name", sorted(_coverage_corpora()))
def test_no_orphan_slash_command_in_docs(corpus_name):
    known = SLASH_QUALIFIED | SLASH_GROUPS_TOP | SLASH_SUBGROUPS
    documented = _documented_slash(_coverage_corpora()[corpus_name])
    orphans = sorted(token for token in documented if token not in known)
    assert not orphans, (
        f"{corpus_name} 寫了這些不存在的斜線指令：{orphans}。"
        "指令改名或移除時，三語 help 與四份文件都要一起改。")


# --------------------------------------------------------------------------
# 隱藏面不得外洩到使用者文件
# --------------------------------------------------------------------------
# `@bot <文字>` 是唯一保留下來的 mention 用法（自由提問入口），文件要講得到它。
_ALLOWED_MENTION_PHRASES = ("@bot <文字>", "@bot <text>", "@bot <提問>",
                            "@bot <问题>")


@pytest.mark.parametrize("corpus_name", sorted(_coverage_corpora()))
def test_hidden_surfaces_are_not_advertised(corpus_name):
    """使用者文件不得再教 `!cmd` 或 `@bot <子指令>`。

    這是「三個表面收斂成一個」的機械保證。少了它，`!` 會一次一句地爬回文件
    裡，而使用者又要面對兩套講法。
    """
    text = _coverage_corpora()[corpus_name]
    for phrase in _ALLOWED_MENTION_PHRASES:
        text = text.replace(phrase, "")
    bang = sorted(set(_BANG_TOKEN_RE.findall(text)))
    mention = sorted({m.lower() for m in
                      re.findall(r"@bot\s+([A-Za-z_][A-Za-z0-9_]*)", text)})
    assert not bang and not mention, (
        f"{corpus_name} 還在教隱藏介面——`!` 指令：{bang}；"
        f"`@bot` 子指令：{mention}。斜線指令是唯一對外介面；"
        "相容路徑刻意不寫進文件。")


# --------------------------------------------------------------------------
# 沒有斜線選單的平台是例外，而且只有那些平台
# --------------------------------------------------------------------------
# 上面那支掃的是**斜線平台**的五份語料，一個字都沒改。但那五份不是全部的使用者
# 文件：`docs/` 底下還有別的頁。bot 也在沒有斜線選單的平台上運作，而文字指令在
# 那些平台**不是**相容路徑、是唯一的入口，所以那一頁必須教得了它——而「哪一頁
# 可以教」必須是列舉的，否則 `!` 會一頁一頁爬回文件裡，正是上面那支存在的理由。
#
# 名單兩個方向都對帳：列了卻其實沒教文字指令的條目會被報成過期。一個永遠對不上
# 任何東西的豁免會安靜失效，而守門看起來照常在跑——`_OWNER_ONLY_SLASH` 與
# `_DELIBERATE` 都是同一個形狀。
_TEXT_SURFACE_DOCS = {
    # 沒有斜線選單的平台的使用者說明。那些平台上文字指令不是相容路徑，是入口。
    "docs/platforms.md",
}


def _user_doc_corpora() -> dict[str, str]:
    """所有使用者面的 markdown（repo root ＋ `docs/` ＋ `commands/`）。

    **用 glob 而不是寫死清單**：新增一頁文件時寫死的那一份會讓它靜默地不受任何
    檢查，而這支測試的全部意義就是「每一頁都要選邊站」。`architecture.md`
    是內部文件、不是使用者文件，所以排除。
    """
    skip = {"CLAUDE.md", "architecture.md"}
    out: dict[str, str] = {}
    for path in (*sorted(REPO_ROOT.glob("*.md")),
                 *sorted((REPO_ROOT / "docs").glob("*.md")),
                 *sorted((REPO_ROOT / "commands").glob("*.md"))):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if path.name in skip:
            continue
        out[rel] = path.read_text(encoding="utf-8")
    return out


def _text_commands_taught(text: str) -> list[str]:
    for phrase in _ALLOWED_MENTION_PHRASES:
        text = text.replace(phrase, "")
    known = set(BANG_PRIMARY) | set(BANG_ALIASES)
    return sorted({token for token in _BANG_TOKEN_RE.findall(text)
                   if token in known})


def test_only_the_declared_non_slash_doc_may_teach_text_commands():
    """斜線平台的文件不得教文字指令；非斜線平台的那一頁可以，但必須先列冊。"""
    corpora = _user_doc_corpora()
    assert len(corpora) >= 10, (
        f"只掃到 {len(corpora)} 份使用者文件——glob 壞了，這支等於沒在檢查。")
    offenders = {name: hits for name, text in corpora.items()
                 if name not in _TEXT_SURFACE_DOCS
                 and (hits := _text_commands_taught(text))}
    assert not offenders, (
        f"這些文件在教文字指令：{offenders}。斜線指令是那個平台唯一的對外介面；"
        f"只有 {sorted(_TEXT_SURFACE_DOCS)} 這些沒有斜線選單的平台說明可以教，"
        "而且要先列進 `_TEXT_SURFACE_DOCS` 並寫理由。")


def test_the_text_surface_exemption_is_not_stale():
    """列了卻其實沒教文字指令的豁免 ＝ 一個安靜失效的豁免。

    也順便釘住檔案真的存在：改名之後那一筆會變成一個永遠對不上任何東西的字串，
    而掃描照樣跑、每一支照樣綠。
    """
    corpora = _user_doc_corpora()
    stale = sorted(name for name in _TEXT_SURFACE_DOCS
                   if not _text_commands_taught(corpora.get(name, "")))
    assert not stale, (
        f"`_TEXT_SURFACE_DOCS` 列著 {stale}，但它們現在沒有教任何文字指令"
        "（檔案不見了、改名了、或那一段被刪掉了）。把那一筆刪掉。")


@pytest.mark.parametrize("text,caught", [
    ("用 `!status` 看狀態", True),
    ("用 `/status` 看狀態", False),
    ("太好了!", False),
    ("`!definitelynotacommand` 不是指令", False),
])
def test_the_text_command_detector_tells_the_shapes_apart(text, caught):
    """對照組：真的指令名要抓到，普通驚嘆號與不存在的指令不能誤報。

    沒有這一格，把述詞改成「永遠回空清單」上面兩支照樣綠——而一個永遠不叫的守門
    跟一棵乾淨的樹長得一模一樣。
    """
    assert bool(_text_commands_taught(text)) is caught


# --------------------------------------------------------------------------
# 相容面：每個隱藏指令都要有斜線對應
# --------------------------------------------------------------------------
def test_every_hidden_command_has_a_slash_equivalent():
    """漏掉一個，就代表那個功能只剩沒人看得到的入口。

    對應關係寫在宣告上的 `extras={"bang": "!kill"}`——鍵長在它管的指令旁邊，
    不會跟一張放在別處的對照表漂移。
    """
    mapped = {extra["bang"] for extra in SLASH_EXTRAS.values()
              if isinstance(extra, dict) and extra.get("bang")}
    missing = sorted(set(BANG_PRIMARY) - mapped)
    assert not missing, (
        f"這些 `!` 指令沒有任何斜線對應：{missing}。"
        "每個相容路徑上的指令都要有斜線入口，否則使用者從文件上找不到它。")
    mentions = {extra["mention"] for extra in SLASH_EXTRAS.values()
                if isinstance(extra, dict) and extra.get("mention")}
    missing_mention = sorted(set(MENTION_COMMANDS) - mentions)
    assert not missing_mention, (
        f"這些 `@bot` 子指令沒有任何斜線對應：{missing_mention}。")


def test_slash_permission_keys_are_complete():
    """每個斜線指令都要標明它是公開的、還是對到某個 `!` 權限鍵。

    `_tree_check` 預設 fail-closed：沒標 `public` 就受頻道閘與角色閘。沒標
    `bang` 的會落到 `operator`——那是安全的預設，但「忘了標」與「刻意不標」看
    起來一模一樣，所以這裡要求每個都明確表態。
    """
    unmarked = sorted(name for name, extra in SLASH_EXTRAS.items()
                      if not (isinstance(extra, dict)
                              and (extra.get("public") or extra.get("bang")
                                   or extra.get("mention"))))
    assert not unmarked, (
        f"這些斜線指令沒有 `extras`：{unmarked}。"
        '公開指令標 `extras={"public": True}`，其餘標 '
        '`extras={"bang": "!對應指令"}` 或 `extras={"mention": "子指令"}`。')


# --------------------------------------------------------------------------
# 斜線指令：平台限制
# --------------------------------------------------------------------------
def test_slash_top_level_count_is_under_the_platform_limit():
    total = len(SLASH_FLAT) + len(SLASH_GROUPS_TOP)
    assert total <= SLASH_TOP_LEVEL_LIMIT, (
        f"頂層斜線指令 {total} 個，超過平台上限 {SLASH_TOP_LEVEL_LIMIT}"
        f"（{len(SLASH_FLAT)} 個直接指令 ＋ {len(SLASH_GROUPS_TOP)} 個指令群；"
        "群內子指令不計）。超過的話 `tree.sync()` 會整批被拒——壞掉的不是新指令"
        "而是**全部**斜線指令。要再加就把低頻的收進既有指令群。")


def test_slash_group_shape():
    """每群 ≤25 個子項、巢狀 ≤2 層、完整名稱不得重複。

    discord.py 在 decorator／建構當下就會丟 ValueError，所以匯入得成功就代表這
    三條沒破。這支測試的價值是**不必匯入**也擋得到（匯入會讀設定檔、建 client）。
    """
    children: dict[str, int] = {}
    for qualified in SLASH_QUALIFIED:
        parts = qualified.split()
        if len(parts) > 1:
            children[" ".join(parts[:-1])] = children.get(" ".join(parts[:-1]), 0) + 1
        assert len(parts) <= 3, (
            f"`/{qualified}` 有 {len(parts)} 層；平台最多 `/群 子群 指令` 三層。")
    for subgroup in SLASH_SUBGROUPS:
        parent = " ".join(subgroup.split()[:-1])
        children[parent] = children.get(parent, 0) + 1
    over = sorted((name, n) for name, n in children.items() if n > 25)
    assert not over, f"這些指令群超過 25 個子項：{over}"


def test_slash_names_are_platform_legal():
    """名稱與說明不合法會讓整棵樹被拒，而不是只拒那一個。"""
    pattern = re.compile(r"^[a-z0-9_-]{1,32}$")
    bad = sorted(segment for qualified in SLASH_QUALIFIED
                 for segment in qualified.split() if not pattern.match(segment))
    assert not bad, f"這些名稱不合平台規則（小寫、數字、`_`、`-`，1-32 字）：{bad}"


def test_no_send_site_advertises_a_text_command():
    """送出去的字串不得再教使用者打 `!cmd` 或 `@bot cmd`。

    這批「用法：…」提示是 `!` 時代寫的，數量約 158 處。斜線成為唯一介面之後，
    它們會教使用者一個文件上查不到的寫法。掃的是送出點底下的字串常數，跟
    `test_secrecy.py` 同一套判斷送出點的方法。
    """
    senders = {"reply", "send", "send_message"}
    violations: list[str] = []
    for node in ast.walk(_bot_tree()):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_send = ((isinstance(func, ast.Name) and func.id == "safe_reply")
                   or (isinstance(func, ast.Attribute) and func.attr in senders))
        if not is_send:
            continue
        args = list(node.args) + [kw.value for kw in node.keywords
                                  if kw.arg == "content"]
        for argument in args:
            for sub in ast.walk(argument):
                if not (isinstance(sub, ast.Constant)
                        and isinstance(sub.value, str)):
                    continue
                text = sub.value
                for phrase in _ALLOWED_MENTION_PHRASES:
                    text = text.replace(phrase, "")
                if re.search(r"`!\w|^!\w|@bot\s+[A-Za-z_]", text):
                    violations.append(
                        f"discord_bot.py:{node.lineno} → {sub.value[:70]!r}")
    assert not violations, (
        "以下送出點還在教隱藏介面的寫法：\n  "
        + "\n  ".join(sorted(set(violations))[:40])
        + f"\n（共 {len(set(violations))} 處）改寫成對應的斜線指令。")


# --------------------------------------------------------------------------
# 文件數字
# --------------------------------------------------------------------------
def _count_bang_primary() -> int:
    return len(BANG_PRIMARY)


def _count_bang_alias() -> int:
    return len(BANG_ALIASES)


def _count_mention() -> int:
    return len(MENTION_COMMANDS)


def _count_slash_top() -> int:
    """只算**直接**指令：指令群另外算，群內子指令不佔頂層額度。"""
    return len(SLASH_FLAT)


def _count_slash_groups() -> int:
    return len(SLASH_GROUPS_TOP)


def _count_slash_leaves() -> int:
    return len(SLASH_QUALIFIED)


def _count_prompt_files() -> int:
    # `bot_prompts/` 在**版本庫根目錄**，不在 axiomatic/ 底下（見 `_bot_prompts`
    # 的 `BOT_PROMPTS_DIR`）。
    return len([path for path in (REPO_ROOT / "bot_prompts").glob("*")
                if path.is_file()])


# (檔案, 抓數字的樣式, 這個數字是什麼, 怎麼量)
CITATIONS = [
    # README 是使用者文件：只講斜線面，不引用 `!` / `@bot` 的數字。
    ("README.md", r"(\d+) 個頂層 slash 指令", "頂層斜線指令", _count_slash_top),
    ("README.md", r"(\d+) 個指令群", "斜線指令群", _count_slash_groups),
    ("README.md", r"(\d+) 個斜線子指令", "斜線葉指令", _count_slash_leaves),
    ("README.md", r"(\d+) 個純文字檔", "prompt 檔", _count_prompt_files),
    ("axiomatic/discord_bot.py", r"目前 (\d+)/100",
     "頂層斜線指令總數", lambda: _count_slash_top() + _count_slash_groups()),
]


@pytest.mark.parametrize(
    "doc,pattern,label,measure", CITATIONS,
    ids=[f"{doc}:{label}" for doc, _p, label, _m in CITATIONS])
def test_quoted_number_matches_reality(doc, pattern, label, measure):
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    found = re.findall(pattern, text)
    assert found, (
        f"{doc} 裡找不到 {label} 的數字（樣式 {pattern!r}）。"
        "如果是刻意改寫句子，就把這裡的樣式跟著改；數字本身要留在文件裡。")
    actual = measure()
    for quoted in found:
        assert int(quoted) == actual, (
            f"{doc} 寫 {label} 有 {quoted} 個，實際是 {actual} 個。"
            "重新量過並更新每一份引用到它的文件。")


def test_every_citation_target_exists():
    """檔案改名不能讓這道守門靜默失效。"""
    for doc, _pattern, _label, _measure in CITATIONS:
        assert (REPO_ROOT / doc).is_file(), f"文件不存在：{doc}"


def test_commands_docs_are_generated():
    """`commands/*.md` 必須就是產生器的輸出。

    DoD #2 說那批檔案是**生成**的，但在 `gen_command_docs.py` 出現之前根本沒有
    產生器，「生成」實務上等於手改——而本檔其餘的覆蓋率測試只比對指令**名稱**，
    參數表、型別、預設值、擁有者標記怎麼漂移都不會有人發現。實際上也已經漂了：
    `/dorossi` 全族在 handler 內硬綁擁有者，文件卻只標了其中兩個。

    這一條把那個洞補起來：手改會被擋下來，改法是重跑產生器。
    """
    import gen_command_docs  # noqa: PLC0415  # 只有這支測試要用，不進模組層

    expected = gen_command_docs.build()
    on_disk = {path.name for path in (REPO_ROOT / "commands").glob("*.md")}
    orphan = sorted(on_disk - set(expected))
    assert not orphan, (
        f"`commands/` 有指令樹產不出來的檔案：{orphan}。"
        "指令群改名或刪掉時會留下這種孤兒，要手動移除。")
    stale = sorted(
        name for name, text in expected.items()
        if (REPO_ROOT / "commands" / name).read_text(encoding="utf-8") != text)
    assert not stale, (
        f"這些檔案跟指令樹不同步：{stale}。"
        "`commands/*.md` 是產生出來的，不要手改——跑 "
        "`py -3 axiomatic/gen_command_docs.py` 重新產生。"
        "（編輯性質的補充說明寫進 `gen_command_docs.NOTES` / `COMMAND_NOTES`。）")


def _orphan_notes(notes: dict, valid: set, allow=frozenset()) -> list:
    """回「指向不存在的東西」的那些 key。

    判斷抽成一支，才有辦法對它做對照組——樹是乾淨的，所以「回報孤兒」那幾行在
    真實資料上一次都不會執行，整段刪掉照樣全綠。
    """
    return sorted(key for key in notes
                  if key not in valid and key not in allow)


def test_every_editorial_note_still_names_something_that_exists():
    """`NOTES` / `COMMAND_NOTES` 的每一個 key 都要對得上真的指令群／指令。

    這兩份 dict 是 DoD #2 指定的**編輯性補充說明**存放處——指令樹產不出來、由人
    寫的那一半（上面那支測試的失敗訊息就是這樣告訴人的）。它們失效的方式是安靜
    的：指令群改名、子指令搬到別的群之後，那個 key 再也不會命中任何東西，產生器
    照跑、檔案照生、每一支測試照綠，只是那段說明從文件裡消失了。

    跟 `_OWNER_ONLY_SLASH` 同一個形狀，而 `CLAUDE.md` 對那個形狀寫得很清楚：
    「the gate still runs, the set is still there, every test stays green」。
    這兩份在 2026-09-21 之前沒有任何東西在看（當時量：兩份都還是乾淨的，所以這支
    是純粹的防迴歸，不是修 bug）。

    **只守一個方向**：不得有指向不存在的東西的 key。反方向（每個群都要有註解）
    刻意不守——註解是編輯決定，絕大多數指令群本來就不需要補充說明。
    """
    import gen_command_docs  # noqa: PLC0415  # 只有這支測試要用，不進模組層

    surface = gen_command_docs.Surface()
    group_names = {info["name"] for info in surface.groups.values()}
    qualified = {command["qualified"] for command in surface.commands}
    assert len(group_names) >= 20 and len(qualified) >= 200, (
        f"指令樹只抽到 {len(group_names)} 個群 / {len(qualified)} 個指令，"
        "推導看起來失效了——空集合的比較跟通過長得一模一樣。")
    assert gen_command_docs.NOTES and gen_command_docs.COMMAND_NOTES, (
        "兩份註解 dict 都空了，這支測試沒有東西可以比。真的不再需要註解的話，"
        "把這支跟那兩份 dict 一起拿掉，不要留一個空的比較在這裡。")
    # `_direct` 是給「不屬於任何指令群」那一頁用的，不是群名。`render_direct` 用
    # `if "_direct" in NOTES` 取，所以它可以不存在；存在時也不算過期。
    orphan_groups = _orphan_notes(
        gen_command_docs.NOTES, group_names, {"_direct"})
    assert not orphan_groups, (
        f"`gen_command_docs.NOTES` 這幾個 key 不是任何指令群的名字："
        f"{orphan_groups}。指令群改名時要一起改，否則那段編輯說明會安靜地從"
        "產生出來的 `commands/*.md` 裡消失。")
    orphan_commands = _orphan_notes(gen_command_docs.COMMAND_NOTES, qualified)
    assert not orphan_commands, (
        f"`gen_command_docs.COMMAND_NOTES` 這幾個 key 不是任何指令的完整名稱："
        f"{orphan_commands}。子指令改名或搬到別的群時要一起改。")


def test_the_orphan_note_check_actually_fires():
    """對照組：三種輸入各對應判斷裡的一件事。"""
    assert _orphan_notes({"config": "x"}, {"config"}) == []
    assert _orphan_notes({"was_renamed": "x"}, {"config"}) == ["was_renamed"]
    assert _orphan_notes({"_direct": "x"}, {"config"}, {"_direct"}) == []


def test_fully_owner_only_groups_say_so_in_their_description():
    """整群都限擁有者的指令群，`description` 要講出來。

    群描述是**唯一**會同時出現在指令選單、三語 help、`COMMANDS.md`、
    `docs/commands_*.md` 與 `commands/*.md` 的字串，所以標記掛在它上面就自動
    五處同步（`/schedule` 一直是這樣做的）。

    2026-08-27 的實況是只有 `/schedule` 有標：`/dorossi` 全族 32 個子指令都限
    擁有者，卻列在**跨頻道**的公開 help 裡完全沒有標記——任何伺服器的任何人都
    看得到一整排自己叫不動的指令。另外九個桌面控制群同理。

    只查「全群都是」的情形。部分子指令限擁有者的群（`/sys`、`/config`、`/log`、
    `/gen`）不在此列——那種要逐指令標，`commands/*.md` 已經有了。
    """
    import gen_command_docs  # noqa: PLC0415

    surface = gen_command_docs.Surface()
    unmarked = []
    for var in surface.registered:
        info = surface.groups[var]
        members = [c for c in surface.commands if c["path"][0] == info["name"]]
        if not members or not all(surface.owner_line(c) for c in members):
            continue
        if "限擁有者" not in info["description"]:
            unmarked.append("/%s — %s" % (info["name"], info["description"]))
    assert not unmarked, (
        "這些指令群的子指令**全部**限擁有者，描述卻沒說：\n  "
        + "\n  ".join(unmarked)
        + "\n改法：在 `discord_bot.py` 的 `Group(description=…)` 加上「（限擁有者）」，"
        "再同步三語 help／`COMMANDS.md`／`docs/commands_*.md`，最後重跑 "
        "`gen_command_docs.py`。")


# 本專案 zh-TW help 語料實際用到、且**有對應簡體寫法**的繁體字。用 OpenCC 的
# `t2s` 對 zh-TW 兩份 sections 掃一次產出後凍在這裡——測試因此不需要任何轉換
# 套件（requirements.txt 沒有 dev/test 區塊，連 pytest 都沒宣告，不該為一條斷言
# 多一個相依）。要更新：對 zh-TW 語料重跑一次 `t2s`，把差異字補進來。
_TRADITIONAL_ONLY = set(
    "佇來傳內刪則動務區參問啟單圖執實寫尋對帶幾庫張後徑復態撐擁數斷時暫會條業"
    "標樣機檔檢歷為狀產畫監盤碼稱筆節紀細終結絡統維線編總脈與螢補裡視覽計訊記"
    "設診詞詢話誤說議讀變貼資載輯輸進運過選還錄錯鍵開間階際隨雜項預頭頻顯餘體點")


def test_zh_cn_help_is_actually_simplified():
    """zh-CN 段落要真的是簡體，不能只有標題是簡體、內文照抄繁體。

    `CLAUDE.md` 明訂這兩份 sections 刻意維持簡體給大陸使用者。但群描述那幾行是
    從指令樹**照抄**過來的，所以每次有人改 `Group(description=…)` 再同步 help，
    繁體就會被貼回 zh-CN——2026-08-27 之前 24 個群的描述行全部是繁體，標題卻是
    簡體，看起來像翻譯到一半。

    反引號內是指令名，三語一致是刻意的，所以不看。
    """
    offenders = []
    for kind in ("channel", "mention"):
        for section in HELP.HELPS["zh-cn"][kind]:
            for line in section.split("\n"):
                plain = "".join(part for index, part in enumerate(line.split("`"))
                                if index % 2 == 0)
                found = sorted(set(plain) & _TRADITIONAL_ONLY)
                if found:
                    offenders.append("[%s] %s ← %s"
                                     % (kind, line.strip(), "".join(found)))
    assert not offenders, (
        "zh-CN help 裡出現繁體字：\n  " + "\n  ".join(offenders)
        + "\n改法：把那幾行改寫成簡體＋大陸用詞（檔案→文件、執行→运行、"
        "設定→设置、視窗→窗口、佇列→队列、巨集→宏…）。**不要**改成繁體去"
        "「統一」——那兩份是刻意給大陸使用者的。")


def _description_corpora() -> dict[str, str]:
    """群描述要逐字出現的語料。**刻意不含 zh-CN**——那份是翻譯過的。

    `docs/commands_*.md` 跟覆蓋率那邊一樣要合起來看：它照「限頻道 / 跨頻道」拆成
    兩份，單看一份必然缺另一半。
    """
    out = {}
    for lang in ("zh-tw", "en"):
        out["help[%s]" % lang] = "\n".join(
            HELP.HELPS[lang]["channel"] + HELP.HELPS[lang]["mention"])
    out["COMMANDS.md"] = _doc("COMMANDS.md")
    out["docs/commands_*.md"] = "\n".join(
        path.read_text(encoding="utf-8") for path in _sphinx_command_docs())
    return out


def test_the_sphinx_doc_glob_and_its_floor_actually_bite(tmp_path):
    """合成對照：下限擋得住「抽到太少」，glob 也真的收得到**新增**的那一份。

    兩件事在真實資料上都看不出來：永遠剛好兩份，所以把下限放寬成 0、或退回
    寫死那兩個檔名，都不會有任何測試變紅。第三份那一格就是寫死清單的反例。
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    with pytest.raises(AssertionError):
        _sphinx_command_docs(tmp_path)
    (docs / "commands_channel.md").write_text("a", encoding="utf-8")
    with pytest.raises(AssertionError):
        _sphinx_command_docs(tmp_path)
    (docs / "commands_mention.md").write_text("b", encoding="utf-8")
    (docs / "commands_owner.md").write_text("c", encoding="utf-8")
    (docs / "index.md").write_text("d", encoding="utf-8")
    assert [p.name for p in _sphinx_command_docs(tmp_path)] == [
        "commands_channel.md", "commands_mention.md", "commands_owner.md"]


@pytest.mark.parametrize("corpus_name", sorted(_description_corpora()))
def test_group_descriptions_are_echoed_verbatim(corpus_name):
    """指令群的 `description` 要**逐字**出現在每一份語料裡。

    群描述是唯一同時出現在指令選單、三語 help、`COMMANDS.md`、
    `docs/commands_*.md` 與 `commands/*.md` 的字串，而後四份是**手抄**的。抄漏
    一份就開始漂——2026-08-27 為十個群加「（限擁有者）」時就要同時改四個檔，
    少改一個不會有任何錯誤訊息，只會有一份文件開始說謊。

    `commands/*.md` 不在這裡，它由 `gen_command_docs.py` 產生、另有守門。
    zh-CN 也不在這裡，那份刻意翻成簡體。
    """
    import gen_command_docs  # noqa: PLC0415

    surface = gen_command_docs.Surface()
    corpus = _description_corpora()[corpus_name]
    missing = sorted(
        "/%s — %s" % (surface.groups[var]["name"], surface.groups[var]["description"])
        for var in surface.registered
        if surface.groups[var]["description"] not in corpus)
    assert not missing, (
        "這些指令群的描述沒有逐字出現在 `%s`：\n  " % corpus_name
        + "\n  ".join(missing)
        + "\n改法：把 `discord_bot.py` 裡 `Group(description=…)` 的字串原封不動"
        "抄過去（zh-CN 那份才要翻譯）。")


# 平台對 CHAT_INPUT 指令的硬性長度上限。超過的後果不是「那一個指令壞掉」而是
# `tree.sync()` 整批被拒——壞掉的是**全部**斜線指令，而且要到下一次 sync 才看得到。
SLASH_DESCRIPTION_LIMIT = 100
SLASH_OPTION_NAME_LIMIT = 32
SLASH_OPTIONS_PER_COMMAND = 25


def test_slash_descriptions_and_options_are_within_platform_limits():
    """`description` 與參數說明都要是 1–100 字元，參數名 ≤32、每指令 ≤25 個參數。

    既有的 `test_slash_names_are_platform_legal` 只驗**名稱**，docstring 卻寫
    「名稱與說明不合法會讓整棵樹被拒」——說明那一半一直沒人在看。2026-08-27 為
    十個指令群的描述加上「（限擁有者）」時就直接踩在這個盲點上（結果沒事，最長
    的一筆是 69 字元，離上限還遠），但下一次未必這麼幸運。

    空字串同樣違法：平台要求 description 至少 1 個字元。
    """
    import gen_command_docs  # noqa: PLC0415

    surface = gen_command_docs.Surface()
    problems: list[str] = []

    def check_description(label: str, text: str) -> None:
        if not text:
            problems.append("%s：description 是空的（平台要求至少 1 字元）" % label)
        elif len(text) > SLASH_DESCRIPTION_LIMIT:
            problems.append("%s：description %d 字元，上限 %d"
                            % (label, len(text), SLASH_DESCRIPTION_LIMIT))

    for info in surface.groups.values():
        check_description("群 /%s" % info["name"], info["description"])

    for command in surface.commands:
        label = "/" + command["qualified"]
        check_description(label, command["description"])
        if len(command["params"]) > SLASH_OPTIONS_PER_COMMAND:
            problems.append("%s：%d 個參數，上限 %d"
                            % (label, len(command["params"]),
                               SLASH_OPTIONS_PER_COMMAND))
        for param in command["params"]:
            check_description("%s 的 `%s`" % (label, param["name"]), param["desc"])
            if len(param["name"]) > SLASH_OPTION_NAME_LIMIT:
                problems.append("%s：參數名 `%s` %d 字元，上限 %d"
                                % (label, param["name"], len(param["name"]),
                                   SLASH_OPTION_NAME_LIMIT))

    assert not problems, (
        "這些宣告會讓 `tree.sync()` **整批**被拒（不是只拒那一個）：\n  "
        + "\n  ".join(problems))

