"""Commit 訊息不得洩漏 AI 作者身分（`CLAUDE.md` §Git Commits 的靜態防線）。

規則本身寫在 `CLAUDE.md`：commit 訊息、PR 標題與內文都**不得**出現
`Co-Authored-By` 行，也不得提到 AI 工具／模型名稱作為**作者**。在這之前這條
完全沒有機器檢查，只靠人記得——而它偏偏是這個 repo 裡**最容易被自動加上**的一
條：協作工具的預設行為就是在 commit 訊息尾端補一行 `Co-Authored-By:`。專案規則
必須贏過那個預設，而「贏」不能靠每次都記得。

**為什麼特別值得守：這條是不可逆的。** 其他硬規則違反了就改一行程式重跑測試；
commit 訊息一旦推出去，要清掉就得改寫已發布的歷史（所有下游要重新 clone），
成本高到實務上就是不會做。所以防線必須擋在**寫進去之前**。

掃描範圍刻意**只認作者身分標記**，不認「Claude」「AI」這些字本身。理由是本
repo 的歷史裡這些字大量出現，而且全部是**正當的主題內容**：

* `CLAUDE.md` —— 專案指示檔的檔名；
* `@bot ai [claude|codex]` —— 指令名與後端別名（`CLAUDE.md` 的既有例外）；
* `Dorossi（Claude 問答助理）` —— 功能本身的描述；
* `新增「任何回覆都不得提外部服務／模型／AI」硬規則` —— 在引述規則。

拿這些去比對會得到一大堆假警報，而 `CLAUDE.md` 自己就寫著「a guard that cries
wolf is a guard someone switches off」（`test_language.py`／`test_text_encoding.py`
都是照這個原則收窄的）。所以這裡只比對**零歧義**的作者身分標記。
"""
from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 零歧義的作者身分標記。每一條都是「只會在宣告作者時出現」的字面，不會誤傷
# 把 AI 當**主題**在談的 commit 訊息。
_AUTHORSHIP_MARKERS = (
    re.compile(r"^\s*co-authored-by\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*signed-off-by\s*:\s*(claude|gpt|copilot|codex)",
               re.IGNORECASE | re.MULTILINE),
    re.compile(r"generated with \[?(claude|chatgpt|copilot|codex)",
               re.IGNORECASE),
    re.compile(r"\U0001F916"),                       # 🤖 —— 工具署名常用的表情
    re.compile(r"\b(written|authored|generated|created)\s+(by|with)\s+"
               r"(claude|chatgpt|copilot|codex|an?\s+ai)\b", re.IGNORECASE),
    re.compile(r"\bai[- ]generated\b", re.IGNORECASE),
    re.compile(r"\bwith the help of (claude|chatgpt|copilot|an? ai)\b",
               re.IGNORECASE),
)

_SEPARATOR = "\x1e"          # record separator —— commit 訊息不會出現這個字元


def _git(*args: str) -> str | None:
    """跑一次 git，失敗回 None（沒裝 git／不是 repo／淺 clone 都算）。"""
    try:
        proc = subprocess.run(
            ("git", *args), cwd=REPO_ROOT, capture_output=True,
            # 主旨是中文，本機 locale 是 cp950——不指定就是拿 UTF-8 當 cp950 解。
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _commits() -> list[tuple[str, str]]:
    """`[(hash, 完整訊息), …]`，涵蓋所有 ref。"""
    out = _git("log", "--all", f"--format=%H%n%B{_SEPARATOR}")
    if out is None:
        return []
    commits = []
    for chunk in out.split(_SEPARATOR):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, _, body = chunk.partition("\n")
        if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
            commits.append((head, body))
    return commits


def _offenders(commits) -> list[str]:
    hits = []
    for sha, message in commits:
        for pattern in _AUTHORSHIP_MARKERS:
            found = pattern.search(message)
            if found:
                subject = message.splitlines()[0] if message else ""
                hits.append(f"{sha[:10]} {subject[:60]!r} → "
                            f"{found.group(0).strip()[:60]!r}")
                break
    return hits


def test_no_commit_message_claims_ai_authorship():
    """任何**新** commit 都不得帶作者身分標記。

    這條是不可逆的：訊息推出去之後要清掉就得改寫已發布的歷史。所以要擋在寫進
    去之前，而不是事後才發現。
    """
    commits = _commits()
    if not commits:
        pytest.skip("讀不到 git 歷史（沒裝 git／不是 repo／淺 clone）")
    offenders = _offenders(commits)
    assert not offenders, (
        "這些 commit 訊息帶了 AI 作者身分標記，違反 `CLAUDE.md` §Git Commits："
        f"{offenders}。協作工具**預設**就會補 `Co-Authored-By:` 尾行，專案規則要"
        "贏過那個預設。還沒推出去的話用 `git commit --amend` 把那幾行拿掉；已經"
        "推出去了就得改寫歷史（破壞性，要先問過使用者）。")


def test_the_working_tree_head_is_clean_too():
    """單獨再釘 HEAD 一次。

    上面那支掃 `--all`，理論上涵蓋 HEAD。分開一支的理由是**訊息的精準度**：
    剛剛做完 commit 的人要的是「你這一筆有問題」，不是在一串 hash 裡自己找。
    """
    out = _git("log", "-1", "--format=%H%n%B")
    if out is None:
        pytest.skip("讀不到 git 歷史")
    sha, _, body = out.strip("\n").partition("\n")
    for pattern in _AUTHORSHIP_MARKERS:
        found = pattern.search(body)
        assert not found, (
            f"剛做好的這筆 commit（{sha[:10]}）訊息裡有 "
            f"{found.group(0).strip()!r}——`CLAUDE.md` §Git Commits 禁止。"
            "還沒推的話 `git commit --amend` 拿掉即可。")


def test_the_markers_catch_what_they_are_meant_to_catch():
    """反面：標記表要真的認得出典型的工具署名，也要放過正當的主題內容。

    第二半是重點。本 repo 的歷史裡「Claude」「AI」大量出現而且全部正當
    （`CLAUDE.md` 這個檔名、`@bot ai` 這個指令、Dorossi 這個功能本身）。守門
    如果連這些都叫，就會被關掉——`CLAUDE.md` 自己寫過這個道理。
    """
    should_catch = (
        "feat: x\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
        "feat: x\n\nco-authored-by: someone <a@b.c>",
        "feat: x\n\n\U0001F916 Generated with [Claude Code](https://x)",
        "docs: y\n\nWritten by Claude.",
        "docs: y\n\nThis is an AI-generated change.",
        "feat: z\n\nSigned-off-by: Claude <noreply@anthropic.com>",
    )
    for message in should_catch:
        assert any(p.search(message) for p in _AUTHORSHIP_MARKERS), (
            f"認不出這種工具署名：{message!r}")

    should_pass = (
        "docs: CLAUDE.md 精簡為硬規則索引，新增架構圖與工作進度檔",
        "bot: 新增 @bot Dorossi（Claude 問答助理），工具模式可切換 + 看門狗硬上限",
        "- CLAUDE.md：新增「任何回覆都不得提外部服務／模型／AI」硬規則",
        "bot：@bot ai [claude|codex] per-session 切換後端",
        "chore: 補上 AI Settings 面板展開後的標籤 dump",
    )
    for message in should_pass:
        hit = [p.pattern for p in _AUTHORSHIP_MARKERS if p.search(message)]
        assert not hit, (
            f"把正當的主題內容誤判成作者身分：{message!r} → {hit}。"
            "這種假警報會讓人把守門關掉，連真正的違規也一起放行。")


# ---------------------------------------------------------------------------
# 偵測器自己的對照組（2026-09-10 補）
#
# `_offenders()` 唯一的呼叫端餵的是**真實 git 歷史**，而真實歷史是乾淨的（十筆
# 舊 commit 已列冊跳過）。也就是說把 `_offenders` 整個換成 `return []`，這支
# 檔案裡的每一支測試都還是綠的——一條「不可逆」的硬規則，卻沒有任何東西證明它的
# 偵測器還活著。同一個形狀 2026-09-10 在 `test_verify_browser` 上實際發生過三次。
#
# 這一支的運氣比多數守門好：**列冊的那十筆就是貨真價實的已知壞資料**。拿它們當
# 正面對照，比合成字串強，而且順便對帳——列冊的 sha 如果不存在了，或那筆訊息已經
# 不再命中任何 pattern，那一筆就是死條目，而死條目在這裡是 fail-open。
# ---------------------------------------------------------------------------

_MARKER_SAMPLES = (
    ("co-authored-by 尾行", "fix: something\n\nCo-Authored-By: Claude <x@y>"),
    ("大小寫與空白變形", "fix\n\n  CO-AUTHORED-BY :  Someone <a@b>"),
    ("signed-off-by 掛 AI 名字", "fix\n\nSigned-off-by: Claude <x@y>"),
    ("generated with", "chore: tidy\n\nGenerated with [Claude Code](https://x)"),
    ("機器人表情", "docs: update \U0001F916"),
    ("written by an ai", "feat: add thing, written by an AI"),
    ("ai-generated", "feat: thing\n\nAI-generated patch"),
    ("with the help of", "fix: thing with the help of Claude"),
)


@pytest.mark.parametrize("label,message", _MARKER_SAMPLES,
                         ids=[s[0] for s in _MARKER_SAMPLES])
def test_the_detector_still_fires_on_each_marker_shape(label, message):
    """每一種作者身分標記都要被 `_offenders` 認出來。

    """
    hits = _offenders([("f" * 40, message)])
    assert hits, f"{label}：這種寫法沒有被認出來"


_INNOCENT_SAMPLES = (
    ("把 AI 當主題在談", "feat: 改善 AI 提示詞的載入順序"),
    ("提到工具名但不是署名", "docs: 說明 claude code 的工作目錄設定"),
    ("一般訊息", "fix: 佇列讀取改成只切換行符號"),
    ("co-authored 但不是尾行格式",
     "docs: 解釋為什麼不寫 co-authored-by 這種尾行"),
)


@pytest.mark.parametrize("label,message", _INNOCENT_SAMPLES,
                         ids=[s[0] for s in _INNOCENT_SAMPLES])
def test_the_detector_does_not_cry_wolf(label, message):
    """反面：把 AI 當**主題**談的訊息不得被判成署名。

    會亂叫的守門是會被關掉的守門，而這一條關掉就等於整條規則消失。
    注意「co-authored 但不是尾行格式」那一格——第一個 pattern 錨在行首，
    所以句子中間提到它是合法的；沒有這一格，把 `^\\s*` 拿掉不會有人發現。
    """
    assert not _offenders([("e" * 40, message)]), f"{label}：誤判了"


# ---------------------------------------------------------------------------
# 同一條規則的另外兩份語料：程式碼註解與文件（2026-09-21）
# ---------------------------------------------------------------------------
#
# `CLAUDE.md` §Git Commits 的原句禁了**五個地方**：「anywhere in the message
# body, PR titles, PR descriptions, **code comments, or documentation**」。上面
# 那三支只掃 `git log`，也就是只掃了第一份。程式碼註解與文件那兩份在 2026-09-21
# 之前**沒有任何東西在看**。
#
# 這是本 repo 前一天才記過一次的形狀（`test_secrecy` 2026-09-21：「不得教
# `!cmd`」的文件那半有守門、bot 送出去的字串那半沒有，而樹上有四筆真的違規）。
# 一條規則寫一次、執行卻是按語料各寫一份，所以讀規則看不出覆蓋率。
#
# **這次量到的是乾淨的**（101 個 `.py` 的註解與字串常值、66 個 `.md`，扣掉本檔
# 之後 0 筆），所以這裡補的是守門而不是修復。值得補的理由與 commit 那半相同，
# 是**不可逆**：協作工具的預設署名最容易落在哪裡？產生出來的文件尾巴、README
# 尾巴、新模組的 docstring——每一個都會被 commit 進去，而清掉一份已發佈文件裡的
# 署名跟改寫歷史是同一種成本。
#
# 語料用 `git ls-files`：問的是「**會不會被複製出去**」。沒有 git 就跳過，與上面
# 那三支同樣的處置。
_AUTHORSHIP_PROSE_EXEMPT = {
    # 標記表與它的正反面合成語料就住在這個檔案裡。這不是豁免一個違規，是排除
    # **登記處本身**——不排除的話這支測試第一次跑就會指著自己紅。
    "test/test_commit_authorship.py":
        "標記表 `_AUTHORSHIP_MARKERS` 與正反面對照組的合成 commit 訊息都在這裡",
}

# 註解前面的 `#` 要先拿掉再比對。
#
# ⚠️ 這不是美化，是**這道掃描能不能看見東西**的問題：`_AUTHORSHIP_MARKERS` 裡
# `co-authored-by` 與 `signed-off-by` 兩條都錨在行首（`^\s*`），而 Python 註解
# 一定長成 `# Co-Authored-By: …`——那個 `#` 不是空白，所以錨點對不上。實測：
# 「`Co-Authored-By: x`」命中，「`# Co-Authored-By: x`」**沒有命中**。規則禁的
# 正是 code comments，而沒有這一行的話那條路上最典型的形狀剛好是看不見的。
_COMMENT_PREFIX_RE = re.compile(r"^#+\s?")


def _outside_code_spans(line: str) -> str:
    """把反引號 code span 的內容挖掉——那是在**引用**這個標記，不是在署名。

    ⚠️ **這一步是必要的，而且第一個需要它的就是這道守門自己的文件。** 加上散文
    掃描之後第一次跑全套件就紅了，命中的兩處是內部文件與當日
    的封存條目，兩邊都寫著 `` `🤖` ``——在解釋這條規則。任何「不得出現某個字面」
    的規則都會遇到這件事：第一個引用那個字面的地方，就是解釋它的文件。本 repo 為
    同一個形狀記過一次（`"name" in source` 的測試一直命中解釋規則的那行註解），
    那次的解法是改用 AST；散文沒有 AST，對應的解法就是「反引號＝引用」。

    **刻意複製 `test_language._outside_code_spans` 而不是匯入。** 三行純邏輯、沒
    有會過期的資料，而兩邊的語料需要各自調整：那邊管的是用詞，這邊管的是署名，
    把一道署名守門的鬆緊綁在一道用詞守門的調整上，改錯一次兩邊一起鬆。

    放寬得夠不夠窄：工具自動補上的署名**不會**被反引號包住，而行內 code span 要
    在**同一行**成對才算——三個反引號的 fenced block 那一行自己沒有反引號，所以
    整段照樣會被掃到。
    """
    return "".join(part for index, part in enumerate(line.split("`"))
                   if index % 2 == 0)


def _tracked_prose_files() -> list:
    """`git ls-files` 裡的 `.py` 與 `.md`，repo 相對、posix 斜線。"""
    out = _git("ls-files")
    if out is None:
        return []
    return [rel for rel in out.splitlines()
            if rel.endswith((".py", ".md")) and (REPO_ROOT / rel).is_file()]


def _prose_of(rel: str, root: Path) -> list:
    """`[(行號, 文字)]`。

    `.py` 只取**註解與字串常值**（docstring 也是字串常值），不是整份原始碼：規則
    禁的是「code comments or documentation」，而識別字與模組名裡出現 `codex` /
    `claude` 是正當的（`@bot ai codex`、`CLAUDE.md`），拿整份原始碼去比對就是在
    製造假警報。`.md` 整份都算文件。

    `root` 是參數而不是直接用 `REPO_ROOT`，這樣合成對照組才餵得進自己的語料——
    把模組常數 monkeypatch 掉也做得到，但參數不會在別處留下副作用。
    """
    path = root / rel
    text = path.read_text(encoding="utf-8", errors="replace")
    if rel.endswith(".md"):
        return list(enumerate(text.splitlines(), 1))
    chunks = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                chunks.append((token.start[0],
                               _COMMENT_PREFIX_RE.sub("", token.string)))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass                      # 壞檔由別的守門負責，這裡不該跟著死
    try:
        tree = ast.parse(text, rel)
    except SyntaxError:
        return chunks
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            chunks.append((node.lineno, node.value))
    return chunks


def _prose_offenders(files: list, root: Path = REPO_ROOT) -> list:
    """`[(檔案, 行號, 命中的字)]`——豁免的檔案不進來。"""
    hits = []
    for rel in files:
        if rel in _AUTHORSHIP_PROSE_EXEMPT:
            continue
        for lineno, text in _prose_of(rel, root):
            # 逐行挖掉 code span：行首錨定的那幾條要靠「一行還是一行」才管用。
            text = "\n".join(_outside_code_spans(line)
                              for line in text.splitlines()) or ""
            for pattern in _AUTHORSHIP_MARKERS:
                found = pattern.search(text)
                if found:
                    hits.append((rel, lineno, found.group(0).strip()[:60]))
                    break
    return hits


def test_no_code_comment_or_document_claims_ai_authorship():
    """程式碼註解與文件同樣不得帶作者身分標記。

    2026-09-21 首次量測：101 個 `.py`、66 個 `.md`，扣掉登記處本身之後 **0 筆**。

    ⚠️ 加上這一支之後第一次跑全套件就紅了，而紅的是**解釋這道守門的那兩份文件**
    。所以
    比對前會先挖掉反引號 code span——細節與「為什麼這不算放寬過頭」寫在
    `_outside_code_spans` 的 docstring 裡。
    """
    files = _tracked_prose_files()
    if not files:
        pytest.skip("讀不到 `git ls-files`（沒裝 git／不是 repo）")
    # 正面對照組：語料空掉的話下面那行會安靜地全過。
    assert len(files) >= 60, (
        f"只掃到 {len(files)} 個 `.py`／`.md`——這不像這個 repo，等於沒在檢查。")
    offenders = _prose_offenders(files)
    assert not offenders, (
        "這些註解／文件帶了 AI 作者身分標記，違反 `CLAUDE.md` §Git Commits："
        + "；".join(f"{rel}:{lineno} -> {text!r}"
                    for rel, lineno, text in offenders)
        + "。規則禁的不只是 commit 訊息，還包括 code comments 與 documentation。")


def test_the_prose_exemption_list_is_not_stale():
    """豁免的檔案要存在、要有理由，而且**還是真的**帶著標記。

    一筆對不上任何東西的豁免會安靜失效：它不再遮蔽任何東西，但讀起來像「這裡有
    已知例外」，而下一個真的違規落進同一個檔案時它就變成一張免死金牌。與
    `_OWNER_ONLY_SLASH` 同一個形狀。
    """
    files = _tracked_prose_files()
    if not files:
        pytest.skip("讀不到 `git ls-files`")
    for rel, why in _AUTHORSHIP_PROSE_EXEMPT.items():
        assert len(why) >= 10, f"{rel} 的豁免理由太短，寫清楚為什麼"
        assert rel in files, f"豁免清單裡的 {rel} 已經不是被追蹤的檔案了"
        found = [lineno for lineno, text in _prose_of(rel, REPO_ROOT)
                 for pattern in _AUTHORSHIP_MARKERS if pattern.search(text)]
        assert found, (
            f"{rel} 裡已經沒有任何作者身分標記了——那筆豁免變成死的，請把它從 "
            "`_AUTHORSHIP_PROSE_EXEMPT` 拿掉，否則下一筆真的違規落在這個檔案裡"
            "就再也沒人看得到。")


def test_the_prose_scanner_can_actually_see_a_claim(tmp_path):
    """合成對照組：掃描器真的抓得到註解／文件裡的署名，也真的放過正當內容。

    真實的樹是乾淨的，所以 `_prose_offenders()` 裡 `hits.append(...)` 那一行在上
    面那支測試裡**一次都不會執行**——整行刪掉也照樣全綠。這支把那條路走一次。

    `mod.py` 那一格同時釘住 `_COMMENT_PREFIX_RE`：拿掉它，`# Co-Authored-By:`
    就對不上行首錨點，而那是這條規則在程式碼裡最典型的形狀。
    """
    (tmp_path / "doc.md").write_text(
        "# 標題\n\n\U0001F916 Generated with [Claude Code](https://x)\n",
        encoding="utf-8")
    (tmp_path / "mod.py").write_text(
        '"""模組說明。"""\n# Co-Authored-By: someone <a@b.c>\nX = 1\n',
        encoding="utf-8")
    # 字串常值那一半要單獨有一格：只有註解那格的話，把 `ast` 那段整個刪掉也照樣
    # 全綠，而 docstring 正是新模組最容易被補上署名的地方。
    (tmp_path / "doc_string.py").write_text(
        '"""這個模組 written by Claude。"""\nX = 1\n', encoding="utf-8")
    (tmp_path / "clean.md").write_text(
        "# 乾淨\n\n這份文件談 `CLAUDE.md` 與 `@bot ai codex`，都是正當的主題。\n",
        encoding="utf-8")
    # must-allow：解釋這條規則的文件會**引用**標記。反引號豁免是放寬的一步，
    # 只有這一格殺得掉「把 `_outside_code_spans` 拿掉」那個變異——而少了它，
    # 第一份寫下這條規則的架構文件就會讓整套測試紅掉（實測過了）。
    (tmp_path / "quoting.md").write_text(
        "# 在講規則\n\n標記表裡最容易命中的是 `\U0001F916`，"
        "另外兩條是 `Co-Authored-By:` 與 `Signed-off-by: Claude`。\n",
        encoding="utf-8")
    # must-catch 的近似樣本：同一行有反引號，但標記在**外面**。
    (tmp_path / "near_miss.md").write_text(
        "# 近似\n\n改了 `CLAUDE.md` 之後 \U0001F916 Generated with "
        "[Claude Code](https://x)\n",
        encoding="utf-8")
    (tmp_path / "clean.py").write_text(
        '"""談 `CLAUDE.md` 與 `@bot ai codex`，都是正當的主題。"""\n'
        "# 這裡也只是在談規則，不是在宣告作者\nX = 1\n", encoding="utf-8")
    got = {rel for rel, _lineno, _text in _prose_offenders(
        ["doc.md", "mod.py", "doc_string.py", "clean.md", "clean.py",
         "quoting.md", "near_miss.md"],
        root=tmp_path)}
    assert got == {"doc.md", "mod.py", "doc_string.py", "near_miss.md"}, got
