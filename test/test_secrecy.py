"""Secrecy Layer 1 的靜態防線：送到對話平台的字串不得帶主機內部資訊。

`CLAUDE.md` 的規則是硬要求，但在這支測試出現之前**只有 code review 在擋**，而
它擋的正好是最難用眼睛看出來的東西：一行 `f"❌ {error}"` 看起來人畜無害，實際上
會把任何底層例外的原文（含絕對路徑、外部服務名、憑證片段）原封不動送出去。

四類檢查，全部是 AST 靜態掃描（不需要真的連線）：

1. **原始例外文字**——`except` 綁到的變數不得被內插進送出去的字串。例外：本專案
   自己定義、訊息是自己寫的泛用中文句的型別（`GuiError` 家族）；以及
   `isinstance(error, GuiError)` 條件式裡的那一支。
2. **主機路徑字面**——`todo_*.md`、`output/`、`.chrome_profile`、`*.json` 設定
   檔、`*.log`、Windows 絕對路徑。
3. **檔名物件**——`{path.name}` / `{path.stem}` 這種把磁碟上的命名慣例回貼出去
   的寫法。對話平台自己的物件（伺服器 / 頻道 / 使用者名稱）不在此限。
4. **外部服務名**——出圖服務、圖庫 API、瀏覽器驅動、辨識引擎、後端 AI 供應商，
   以及使用者面的名詞「webrunner」。

允許清單（`_ALLOWED_*`）每一筆都要寫理由。清單變長就是規則在鬆動的訊號，不是
把新的東西塞進去的地方。

**掃描範圍**：`discord_bot.py` 的送出點，以及 `_help_strings.py` 的整份說明語料
（help 本身就是送出去的字串）。
"""
import ast
import collections
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _help_strings as HELP  # noqa: E402

import discord_bot as BOT  # noqa: E402

# `!` 指令名從指令樹推導，不另外抄一份——抄一份就會在改名之後
# 安靜地失效（與 `_OWNER_ONLY_SLASH` 同一個形狀）。
from test_docs_sync import BANG_ALIASES, BANG_PRIMARY  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
BOT_SOURCE = PKG_ROOT / "discord_bot.py"

# 送出點。`safe_reply` 是本專案的包裝；其餘一律以**屬性呼叫**認定
# （`message.reply` / `channel.send` / `followup.send` / `response.send_message`），
# 這樣才不會把區域 helper（例如與子行程對話的 `send()`）誤判成對外送出。
_SENDER_FUNCTIONS = {"safe_reply"}
# `edit` 也算送出：`placeholder.edit(content=...)` 是 Dorossi 串流與巨集進度更新
# 既有訊息的方式，讀者看到的東西跟 `send` 完全一樣，只是換一則訊息承載。
# `Embed` / `add_field` / `set_footer` 是嵌入式訊息的文字，同樣會顯示出來——
# 它們是建構呼叫而不是送出呼叫，但只要建出來就是要送的，掃描點放這裡才抓得到。
_SENDER_METHODS = {"reply", "send", "send_message", "edit",
                   "Embed", "add_field", "set_footer"}

# 訊息文字所在的位置：位置引數，以及這些關鍵字。`file=` 不在此列（附件內容不是
# 本測試的範圍，檔名另有 `_list_label()` 把關）。
_TEXT_KEYWORDS = {"content", "value", "text", "description", "title", "name"}

# 擁有者無限制揭露的**唯一合法出口**（`CLAUDE.md` → Secrecy Layer 1 的擁有者
# 例外）。掃描到這些呼叫時，只放行「擁有者才看得到」的那一個引數，其餘照掃——
# 尤其是泛用句本身（非擁有者看到的字串），那一個**必須**繼續受檢。
#
# 值是「豁免第幾個位置引數」，`None` 代表整個呼叫都豁免（函式內部自己判身分、
# 引數是路徑物件而不是要送出的字面）。
#
# 這張表就是規則的落地：`CLAUDE.md` 說「不要在別處自己再寫一次
# `== OWNER_USER_ID`」，而這裡是唯一承認的名單。要新增出口就得動這一行，
# 不能靠「包進某個函式呼叫」讓掃描器看不見。
_OWNER_GATE_EXITS = {
    "_owner_detail": 1,          # (source, raw, generic) —— raw 豁免，generic 照掃
    "_owner_error": 1,           # (source, error, generic) —— 同上
    "_list_label": None,         # (path, source) —— 回傳值是對照表查出來的標籤
    "_dorossi_dir_display": None,  # (path_str, message) —— 內含 _paths_visible_here
    # (source, error) —— 這**就是**一個擁有者出口：`_queue_not_utf8_reply` 內部
    # 同時走了 `_list_label(error.path, source)` 與 `_owner_error(source,
    # error.cause, "")`，也就是 CLAUDE.md 說的「任何要在原始／泛用之間二選一的
    # 送出點都走它」那個單一決策點。豁免第 1 個位置引數（那個 except 綁到的
    # 例外），其餘照掃。
    #
    # 刻意**不**改成把 `_QueueFileNotUtf8` 塞進 `_SAFE_EXCEPTION_TYPES`：那份
    # 白名單的意思是「這個型別的訊息可以整個原樣送出去」，範圍比這裡需要的寬，
    # 而且旁邊那支 `test_the_refusal_exception_really_has_a_hard_coded_message`
    # 只驗得了 `_UndoBackupUnavailable` 一個——多一個型別進去就多一個沒人驗的
    # 前提。
    "_queue_not_utf8_reply": 1,
}

# 訊息是本專案自己寫的泛用句，內插它們是安全的。
#
# 收下一個型別的**前提**是：它的建構子把訊息寫死成泛用句，一個字都不從輸入來。
# 這樣「送出點寫了 `f"{error}"`」就安全，安全性由建構子保證，不必靠每個呼叫端
# 記得。把檔名或 `repr(cause)` 拼回那個訊息裡，白名單會當場失效而且沒有任何測試
# 會變紅——所以要加新型別之前，先去讀它的 `__init__`。
#
# `_UndoBackupUnavailable`（`discord_bot._safe_write` 拒絕寫入時丟的）符合這個
# 合約：訊息是類別常數 `GENERIC`，出事的檔案與底層例外分別放在 `path` / `cause`
# 兩個屬性上，只透過既有的單一決策點（`_list_label` / `_owner_error`，見
# `_backup_unreadable_reply`）依提問者身分決定要不要露出；`_safe_write` 在 raise
# 之前已經把細節印到 stderr。
_SAFE_EXCEPTION_TYPES = {"GuiError", "_GuiError", "GuiAborted", "_GuiAborted",
                         "_UndoBackupUnavailable"}

# 對話平台自己的物件：`.name` 是伺服器／頻道／使用者名稱，不是主機檔名。
_PLATFORM_OBJECT_BASES = {
    "g", "guild", "channel", "author", "user", "member", "role", "emoji",
    "interaction", "message", "client",
}

_HOST_PATH_PATTERNS = [
    r"todo_[a-z0-9_]*\.md",
    r"\boutput[/\\]",
    r"\.chrome_profile",
    r"batch_config\.json",
    r"bot_config\.json",
    r"schedules\.json",
    r"favorites\.json",
    r"[A-Za-z]:\\",
    r"\.venv",
    r"\.log\b",
]

# 使用者面不得出現的名詞。只比對**整字**，避免 `booru` 命中 `safebooru` 之外的
# 無關字。
_BANNED_WORDS = [
    "webrunner", "novelai", "selenium", "chromedriver", "chrome",
    "playwright", "tesseract", "danbooru", "gelbooru", "anthropic", "openai",
    # ⚠️ 下面六個是 2026-09-11 補進來的，在那之前**完全不在掃描範圍內**。
    # 它們原本只出現在下面的允許清單裡，而允許清單是用減法套用的
    # （`hits - 允許清單`）——`hits` 永遠是 `_BANNED_WORDS` 的子集，所以減掉一個
    # 不在 `_BANNED_WORDS` 裡的字是 no-op。實測交集是空集合：那兩行減法從來
    # 沒有拿掉過任何東西，整個豁免機制是裝飾，而「有豁免」讀起來像「有在掃」。
    # 這是 `_OWNER_ONLY_SLASH` 的親戚，但更安靜：那個至少掃得到，這個連掃都沒掃。
    #
    # 圖庫服務名（Layer 1 的「image source／外部服務」）。裁定過的既有指令名
    # 由 `_strip_sanctioned` 按形狀放行，不是整個字免死。
    "booru", "safebooru", "e621", "iqdb",
    # 後端別名。2026-07-02 的窄範圍例外只放行「功能面的合法值列舉」
    # （`<claude|codex>`），而那條裁定自己寫著「僅此一處，不得外推」——
    # 要做到「僅此一處」，這個字就得先在掃描範圍內。
    "claude", "codex",
    # ⚠️ **後端模型別名，2026-09-12 補進來——與上面六個是同一個缺陷，只是晚了一輪
    # 才被看到。** 2026-07-02 那條裁定放行的其實是兩種值：`/dorossi ai` 的**後端**
    # 別名（上一行），以及 `/model` 的**模型**別名（這一行，＝
    # `dorossi_backend.DOROSSI_MODEL_CHOICES` 的 key）。上一輪只把後端那半放進
    # 掃描範圍，模型那半漏了，於是「僅此一處，不得外推」對模型別名**完全沒有
    # 執行力**：在任何 help 語料或指令說明裡寫「這題交給 opus」都不會被抓到。
    # 實測 2026-09-12（補進來之前先量）：`_help_strings.py` 與 `discord_bot.py`
    # 裡這四個字各出現 **0 次**，所以加進來不會有任何存量誤報要清——這也是現在
    # 補最便宜的原因。四個字確實都是通用英文字（詩體、寓言、樂曲編號），但掃描
    # 範圍只有**指令樹的公開字串**與**六份 help 語料**兩處，不是整個 repo 的散文，
    # 在那兩處出現通用用法的機率低到可以接受；真的誤報了，處置是改那句話，不是
    # 把字從這裡拿掉。
    "opus", "sonnet", "haiku", "fable",
]

# 允許清單。每一筆都要寫「為什麼這不是洩漏」或「為什麼現在不改」。
#
# `booru` / `safebooru` / `e621` / `iqdb`：**指令名稱本身**。這些是既有的公開
# 斜線指令名，出現在 help 與用法字串裡是因為使用者得打得出來。要收掉的話是改
# 指令名——使用者面的破壞性變更，不是這支測試該自己決定的事，所以先在這裡列管
# 並留著這段說明。
#
# `claude` / `codex`：`@bot ai <claude|codex>` 的合法值。與 `CLAUDE.md`
# 2026-07-02 對 `/model` 開的窄範圍例外同一性質（功能面需要讓使用者打得出合法
# 值），但那條例外寫明「僅此一處，不得外推」，所以同樣列管在這裡而不是擴大解釋。
_ALLOWED_COMMAND_NAME_WORDS = {"booru", "safebooru", "e621", "iqdb"}
_ALLOWED_BACKEND_ALIASES = {"claude", "codex"}
# `/model` 的合法值。與上一份同屬 2026-07-02 的窄範圍例外，但**是兩份而不是一份**：
# `/dorossi ai` 收後端別名、`/model` 收模型別名，兩邊的合法值來源不同，反查也要各查
# 各的來源。
#
# ⚠️ **這一份是推導的，不是手打的**，而那不是潔癖：裁定放行的本來就是「那張表的
# key」，手抄一份就會漂移。實際發生過——2026-09-12 稍早這裡寫死四個字
# （`opus`／`sonnet`／`haiku`／`fable`），同一天那張表就擴成 14 個 key，多出來的
# 全是帶版號的（`opus-5`、`sonnet-4.6`、`fable-5.1`…）。手打的版本會把它們**誤報**：
# `\bopus\b` 在 `opus-5` 裡面是命中的，而整段列舉又因為 `opus-5` 不在清單裡而不被
# 放行。當時還沒有任何送出字串列出帶版號的 key，所以測試是綠的——只是還沒發生。
# `default` 不在這張表裡，照樣能一起列，理由見下面 `_drop_if_all_aliases`。
def _model_alias_values() -> set:
    """`DOROSSI_MODEL_CHOICES` 的 key（小寫）。

    在函式裡 import 是刻意的：這個模組已經 import 了 `discord_bot`，而
    `dorossi_backend` 是 bot-only helper，放模組層會讓 import 順序多一條約束。
    """
    import dorossi_backend as _DB
    return {str(key).lower() for key in _DB.DOROSSI_MODEL_CHOICES}


_ALLOWED_MODEL_ALIASES = _model_alias_values()

# 兩份裁定過的別名清單。**刻意不取聯集**：`<claude|opus>` 這種跨裁定的混合列舉
# 不該被放行，因為那兩條裁定各自涵蓋的是各自那個功能面的合法值。
_SANCTIONED_ALIAS_SETS = (_ALLOWED_BACKEND_ALIASES, _ALLOWED_MODEL_ALIASES)

# 角括號裡的合法值列舉——`<claude|codex>`、`<opus|sonnet|haiku|fable|default>`。
# 只有在列舉裡**被禁的那些**選項全部落在**同一份**裁定清單裡時才整段拿掉；
# `<novelai|x>` 不會被放行。
_ALTERNATION_RE = re.compile(r"<[^<>]*>")


def _has_banned_word(text: str) -> bool:
    """`text` 裡面有沒有任何 `_BANNED_WORDS`（以詞為邊界的子字串比對）。

    抽出來是因為有兩個地方要問同一個問題，而它們**必須**問得一模一樣：
    `_drop_if_all_aliases` 判斷一個列舉選項乾不乾淨，以及
    `test_every_sanctioned_word_is_actually_a_banned_word` 判斷一筆豁免有沒有作用。
    兩邊各寫一份的話，「帶版號的 key 算不算被掃到」就會有兩個答案。
    """
    return any(re.search(rf"\b{re.escape(word)}\b", text.lower())
               for word in _BANNED_WORDS)


def _strip_sanctioned(text: str) -> str:
    """把「裁定涵蓋的那個形狀」從文字裡拿掉，回傳**探測用**字串。

    2026-09-11 之前，豁免是整個字從結果裡減掉，等於「這個字在任何地方都免死」。
    那比兩條裁定都寬：2026-08-17 放行的是**既有公開指令的名稱本身**（使用者得打
    得出來），2026-07-02 放行的是**功能面的合法值列舉**，兩條都是形狀，不是字。
    差別是真的：`圖片來自 booru` 這種泛稱服務的句子，在舊寫法下永遠不會被抓到。

    實測（2026-09-11，決定這個範圍之前先量的）：把六個字加進 `_BANNED_WORDS`
    之後，扣掉這裡放行的形狀，整棵樹剩下 **0** 筆——help 語料 6 個字全是
    `/指令` 的寫法，送出點只有 `用法：`/dorossi ai <claude|codex>`` 一句，斜線
    表面只有那四個 `name=` 宣告本身。所以這個範圍不是猜的，是量出來剛好貼合的。
    """
    probe = text
    # (1) 2026-08-17 裁定：既有公開指令的名稱本身。兩種形狀——指令引用
    #     （`/booru`、`` `/booru` ``）與宣告裡的 `name="booru"`（整串就是名字）。
    stripped = probe.strip().strip("`").lower()
    if stripped in _ALLOWED_COMMAND_NAME_WORDS:
        return ""
    for word in _ALLOWED_COMMAND_NAME_WORDS:
        probe = re.sub(rf"/{re.escape(word)}\b", " ", probe, flags=re.IGNORECASE)

    # (2) 2026-07-02 裁定：功能面的合法值列舉。
    def _drop_if_all_aliases(match: "re.Match[str]") -> str:
        options = {part.strip().lower()
                   for part in match.group(0)[1:-1].split("|")}
        # 判準：**每一個**選項要嘛是這份裁定清單裡的合法值，要嘛整段**不含任何
        # 禁字**；而且至少要有一個選項真的是這份清單裡的合法值（否則這段列舉跟
        # 這條裁定無關，不該被它放行）。
        #
        # 「不含任何禁字」是逐字做子字串比對而不是相等比對，這一點是關鍵：
        # `default` 乾淨 → 可以一起列；`novelai-x` **不**乾淨（`\bnovelai\b` 在
        # 裡面）→ `<opus|novelai-x>` 整段不放行。用相等比對的話 `novelai-x` 會因為
        # 「不等於 `novelai`」而被當成乾淨的，於是整段被放行、把它藏起來。
        for allowed in _SANCTIONED_ALIAS_SETS:
            if options & allowed and all(
                    option in allowed or not _has_banned_word(option)
                    for option in options):
                return " "
        return match.group(0)

    return _ALTERNATION_RE.sub(_drop_if_all_aliases, probe)


def _bot_tree() -> ast.Module:
    return ast.parse(BOT_SOURCE.read_text(encoding="utf-8"), str(BOT_SOURCE))


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_sender(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _SENDER_FUNCTIONS
    if isinstance(func, ast.Attribute):
        return func.attr in _SENDER_METHODS
    return False


def _message_arguments(node: ast.Call) -> list[ast.expr]:
    """送出呼叫裡「會變成訊息文字」的每一個引數。

    **所有**位置引數都收，不看節點型別。這裡曾經只收
    `Constant / JoinedStr / BinOp / IfExp` 四種，於是頂層是函式呼叫的寫法整個
    掉出掃描範圍——`reply(str(error))`、`reply(repr(error))`、
    `reply("失敗：{}".format(error))`、`reply("".join([..., str(error)]))`
    全都靜悄悄地過關，而
    `test_no_raw_exception_text_in_replies` 的說明卻寫著這些形式「都算」。
    更難察覺的是它還會因為引數寫成位置還是關鍵字而給出不同答案：
    `reply(content=str(error))` 抓得到，`reply(str(error))` 抓不到。

    收得太寬的代價由 `_walk()` 負責——合法的擁有者出口在那裡剪掉。"""
    return list(node.args) + [kw.value for kw in node.keywords
                              if kw.arg in _TEXT_KEYWORDS]


def _walk(node: ast.AST):
    """`ast.walk`，但不走進擁有者出口的豁免引數。

    直接用 `ast.walk` 的話，`_owner_error(message, error, "泛用句")` 底下那個
    `error` 會被判成洩漏——但它正是規則允許的：擁有者才看得到。反過來，整個呼叫
    都跳過也不對，那樣泛用句（非擁有者看到的字串）就沒人檢查了。所以這裡剪的是
    **單一引數**，不是整棵子樹。"""
    todo = collections.deque([node])
    while todo:
        current = todo.popleft()
        yield current
        skip: set[int] = set()
        if isinstance(current, ast.Call) and isinstance(current.func, ast.Name)                 and current.func.id in _OWNER_GATE_EXITS:
            index = _OWNER_GATE_EXITS[current.func.id]
            if index is None:
                skip = {id(child) for child in ast.iter_child_nodes(current)}
            elif len(current.args) > index:
                skip = {id(current.args[index])}
        todo.extend(child for child in ast.iter_child_nodes(current)
                    if id(child) not in skip)


def _sender_calls() -> list[ast.Call]:
    return [node for node in ast.walk(_bot_tree())
            if isinstance(node, ast.Call) and _is_sender(node)]


SENDER_CALLS = _sender_calls()


# --------------------------------------------------------------------------
# 送出的字面值要跟著「先存進變數再送出」走一步（2026-09-21）
# --------------------------------------------------------------------------
# `_message_arguments()` 收的是送出呼叫的**引數節點**，只有節點底下的字面值才被
# 掃到。於是最常見的一種寫法整段隱形：
#
#     usage = "用法：…"
#     …
#     await safe_reply(message, usage)
#
# 引數是一個 `ast.Name`，字面值在二十行之外。2026-09-21 量到的：跟著名字走一步
# （同一個函式裡對它的指派，找不到才看模組層）多收進 675 段字面值，其中 3 段踩到
# 規則——`cmd_win` 的用法說明教了 `!win`、`/iqdb` 的結果標題寫了服務名、`/booru`
# 的來源頁連結寫死了圖庫網域。前兩段是真的違規（同日改掉），第三段是功能本身，
# 列在 `_PENDING_OWNER_DECISIONS` 等擁有者決定。
#
# 只走**一步**是刻意的：追到底要做的是資料流分析，而一個會叫狼來了的守門是會被
# 關掉的守門；那一步多收的 675 段只命中 3 段，量過不吵。追的是**字面值**，所以
# `text = _redact(raw)` 這種經過處理的指派不會把 `raw` 的內容帶進來。
#
# 兩份語料，因為兩條規則對擁有者出口的態度相反：Layer 1（路徑、服務名）放行
# `_owner_detail` 的 raw 引數，所以用 `_walk` 剪掉；「不得教 `!` 指令」對擁有者也
# 成立，所以用 `ast.walk` 什麼都不剪。


def _node_parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]):
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _name_bindings(scope: ast.AST) -> tuple[dict[str, list[ast.expr]], set[str]]:
    """`scope` 裡（**不含**巢狀的函式／類別）每個名字被指派過的右手邊，以及所有
    在這裡被綁定的名字。

    巢狀的不算：裡面的 `x = "…"` 綁的是另一個作用域的 `x`，算進來就會把別人的
    字面值栽到這個送出點上。第二個回傳值涵蓋**所有**綁定形式（參數、迴圈變數、
    `with … as`、`except … as`）——那些沒有字面值可追，但它們會遮住同名的模組常數，
    不算進來的話，一個恰好與模組常數同名的參數會被栽上那個常數的內容。
    """
    found: dict[str, list[ast.expr]] = collections.defaultdict(list)
    bound: set[str] = set()
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        arguments = scope.args
        for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                    arguments.vararg, arguments.kwarg):
            if arg is not None:
                bound.add(arg.arg)
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id].append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name) and node.value is not None:
                found[node.target.id].append(node.value)
        todo.extend(ast.iter_child_nodes(node))
    return found, bound


def _sent_literals(tree: ast.Module, walker) -> list[tuple[int, int, str, str | None]]:
    """`(送出點行號, 字面值行號, 文字, 經由的名字)`。

    直接寫在引數裡的字面值，名字是 None；經由變數進來的，名字是那個變數——失敗
    訊息要指得出來，否則讀的人會到送出那一行去找一句不在那裡的話。區域的指派
    優先，**找不到**才看模組層（區域變數遮住同名的模組常數）。
    """
    parents = _node_parents(tree)
    module_bindings, _module_bound = _name_bindings(tree)
    scope_bindings: dict[int, tuple[dict[str, list[ast.expr]], set[str]]] = {}
    rows: list[tuple[int, int, str, str | None]] = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and _is_sender(call)):
            continue
        scope = _enclosing_function(call, parents)
        if scope is not None and id(scope) not in scope_bindings:
            scope_bindings[id(scope)] = _name_bindings(scope)
        local, bound = (scope_bindings[id(scope)] if scope is not None
                        else ({}, set()))
        for argument in _message_arguments(call):
            for node in walker(argument):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    rows.append((call.lineno, node.lineno, node.value, None))
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    values = (local.get(node.id, []) if node.id in bound
                              else module_bindings.get(node.id, []))
                    for value in values:
                        for sub in walker(value):
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                rows.append((call.lineno, sub.lineno, sub.value, node.id))
    return rows


def _via(name: str | None, lineno: int) -> str:
    return "" if name is None else f"（經由 `{name}`，字面值在第 {lineno} 行）"


_SENT_TREE = _bot_tree()
# Layer 1（主機路徑、外部服務名）：擁有者出口的 raw 引數剪掉。
SENT_LITERALS = _sent_literals(_SENT_TREE, _walk)
# 「不得教 `!` 指令」：對擁有者也成立，什麼都不剪。
SENT_LITERALS_UNPRUNED = _sent_literals(_SENT_TREE, ast.walk)

# 送出點掃描跟上「一步變數」之後浮出、但屬於**擁有者決定**而不是清理的命中。
# key 是 `(禁字, 字面值開頭)`——比對文字的形狀而不是行號，行號每天在動。
# 這不是豁免清單的開端：每一筆都是一件等人決定的事，決定之後就刪。
_PENDING_OWNER_DECISIONS: dict[tuple[str, str], str] = {
    ("danbooru", "https://danbooru.donmai.us/posts/"): (
        "`/booru` 每一次成功回覆都附來源頁連結，那是功能本身而不是外洩；"
        "2026-08-17 的裁定只涵蓋指令名稱。要不要拿掉連結由擁有者決定"
        "。"),
}


def _is_pending_owner_decision(word: str, text: str) -> bool:
    return any(word == pending and text.startswith(prefix)
               for pending, prefix in _PENDING_OWNER_DECISIONS)


_ONE_HOP_CONTROL = '''
MODULE_HINT = "模組層的提示"


async def direct(message):
    await safe_reply(message, "直接寫在引數裡")


async def via_local(message):
    usage = "先存進區域變數"
    await safe_reply(message, usage)


async def via_augassign(message):
    text = "第一段"
    text += "第二段"
    await safe_reply(message, text)


async def via_annotated(message):
    note: str = "有型別註記的指派"
    await safe_reply(message, note)


async def via_module(message):
    await safe_reply(message, MODULE_HINT)


async def local_shadows_module(message):
    MODULE_HINT = "區域的那一個"
    await safe_reply(message, MODULE_HINT)


async def parameter_shadows_module(message, MODULE_HINT):
    await safe_reply(message, MODULE_HINT)


async def loop_variable_shadows_module(message, items):
    for MODULE_HINT in items:
        await safe_reply(message, MODULE_HINT)


async def owner_raw(message):
    raw = "只有擁有者看得到"
    await safe_reply(message, _owner_detail("來源", raw, "泛用句"))


async def bound_elsewhere(message):
    await safe_reply(message, usage)


async def nested_binding_is_not_ours(message):
    def inner():
        hidden = "巢狀函式裡的指派"
        return hidden
    hidden = inner()
    await safe_reply(message, hidden)
'''


def test_the_sent_literal_corpus_follows_a_sent_name_exactly_one_hop():
    """合成對照組：該跟的每一種指派都要跟到，不該跟的一個都不能帶進來。

    真實樹在修掉那兩筆之後是乾淨的，所以「跟著名字走」那段在真實資料上只會多收
    字面值、不會讓任何斷言失敗——刪掉它整組照樣綠。必須在這裡看得見。
    """
    tree = ast.parse(_ONE_HOP_CONTROL)
    public = _sent_literals(tree, _walk)
    unpruned = _sent_literals(tree, ast.walk)
    expected_public = {
        ("直接寫在引數裡", None), ("先存進區域變數", "usage"),
        ("第一段", "text"), ("第二段", "text"), ("有型別註記的指派", "note"),
        ("模組層的提示", "MODULE_HINT"), ("區域的那一個", "MODULE_HINT"),
        ("來源", None), ("泛用句", None),
    }
    assert {(text, via) for _c, _l, text, via in public} == expected_public
    # 區域變數、參數、迴圈變數都會遮住模組常數；別的函式裡的綁定不算；各自只出現一次。
    texts = [text for _c, _l, text, _v in public]
    assert texts.count("模組層的提示") == 1, texts
    assert texts.count("先存進區域變數") == 1, texts
    # 擁有者出口的 raw 引數：Layer 1 剪掉、教 `!` 的規則不剪。
    assert {(text, via) for _c, _l, text, via in unpruned} == (
        expected_public | {("只有擁有者看得到", "raw")})
    # 經由變數進來的，行號指向字面值那一行而不是送出那一行。
    via_rows = [row for row in public if row[3] == "usage"]
    assert via_rows and via_rows[0][1] != via_rows[0][0], via_rows


def test_every_pending_owner_decision_still_matches_a_real_hit():
    """待決清單過期會變成一個永遠對不上任何東西的豁免——而它看起來仍在管事。"""
    live = {(word, prefix) for word, prefix in _PENDING_OWNER_DECISIONS
            for _c, _l, text, _v in SENT_LITERALS
            if text.startswith(prefix) and word in _banned_words_in(text)}
    stale = sorted(set(_PENDING_OWNER_DECISIONS) - live)
    assert not stale, (
        f"這些待決項目已經對不上任何送出字串：{stale}；擁有者裁定過或程式改掉了，"
        "把那一筆刪掉。")


@pytest.mark.parametrize("word,text,pending", [
    ("danbooru", "https://danbooru.donmai.us/posts/", True),
    # 同一個字、不同形狀：泛稱服務的一句話不在待決範圍內。
    ("danbooru", "圖片來自 danbooru", False),
    # 同一個開頭、不同的字：待決的是那一個字，不是那一整段。
    ("donmai", "https://danbooru.donmai.us/posts/", False),
    ("danbooru", "見 https://danbooru.donmai.us/posts/1", False),
])
def test_a_pending_owner_decision_covers_exactly_its_own_shape(word, text, pending):
    """真實樹上唯一的一筆恰好被放行，所以「放行」寫成恆真也照樣綠——要合成。"""
    assert _is_pending_owner_decision(word, text) is pending


def test_the_two_sent_corpora_differ_exactly_by_the_owner_only_text():
    """兩份語料用錯走法時沒有任何症狀，所以直接比它們。

    Layer 1 那份剪掉擁有者出口的 raw 引數，必須是另一份的**真子集**：兩份相等代表
    有一份用錯了走法（Layer 1 開始誤報擁有者才看得到的內容，或教 `!` 的規則開始
    漏看它們）。另外，教 `!` 的規則實際讀的語料必須是不剪的那一份。
    """
    public = {(lineno, text) for _c, lineno, text, _v in SENT_LITERALS}
    unpruned = {(lineno, text) for _c, lineno, text, _v in SENT_LITERALS_UNPRUNED}
    assert public < unpruned, (
        f"Layer 1 語料 {len(public)} 筆、不剪的語料 {len(unpruned)} 筆——"
        "前者必須是後者的真子集。")
    bang_corpus = {(lineno, text) for module, lineno, text in BOT_AUTHORED_STRINGS
                   if module == "discord_bot.py"}
    assert unpruned <= bang_corpus, (
        f"教 `!` 的規則少讀了 {len(unpruned - bang_corpus)} 段送出字串，"
        "它必須讀不剪擁有者出口的那一份。")


# --------------------------------------------------------------------------
# 掃描本身要站得住腳
# --------------------------------------------------------------------------
def test_scanner_found_the_send_sites():
    # 現值 882（`reply` / `send` / `send_message` / `edit` / 嵌入三種）。門檻拉到
    # 750 是為了讓「送出寫法整批改掉、掃描器卻靜悄悄地只剩一半範圍」當場現形；
    # 真正逐種寫法把關的是下面
    # `test_the_scanner_sees_every_way_of_smuggling_a_name_out`，這裡只是總量的
    # 煙霧偵測器。
    assert len(SENDER_CALLS) >= 750, (
        f"只掃到 {len(SENDER_CALLS)} 個送出點，`discord_bot.py` 的送出寫法可能"
        "變了；掃描邏輯要跟著改，否則這支測試等於沒作用。")


# --------------------------------------------------------------------------
# 1. 原始例外文字
# --------------------------------------------------------------------------
_TEXT_RENDERING_CALLS = {"str", "repr", "ascii", "format"}
# 例外身上「就是那段原文」的屬性。`args` 是訊息元組，`strerror` / `filename` 是
# `OSError` 的路徑欄位（`str(OSError)` 會帶路徑，`repr()` 不會——本專案付過學費）。
_TEXT_ATTRIBUTES = {"args", "message", "reason", "strerror", "filename",
                    "filename2", "stderr", "output", "stdout"}


def _renders_text_of(value: ast.expr, bound: str) -> bool:
    """`value` 是不是把 `bound` 這個例外**轉成文字**（而不只是從它取出一個數）。

    判準刻意窄。把「任何提到那個例外的賦值」都當成污染會當場開始亂叫：樹上真的
    有 `delay = _dorossi_usage_wait_seconds(exc, …)`（一個秒數，會被印進等待訊息）
    與 `keep_sid = getattr(exc, "session_id", None)`（一個 id），那些都不是原文。
    要擋的是原文，所以只認「整個例外物件」「字串化」「f-string」「相加／join」
    與那幾個就是原文的屬性。
    """
    if isinstance(value, ast.Name):
        return value.id == bound
    if isinstance(value, ast.JoinedStr):
        return any(isinstance(n, ast.Name) and n.id == bound
                   for n in ast.walk(value))
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        return (_renders_text_of(value.left, bound)
                or _renders_text_of(value.right, bound))
    if isinstance(value, ast.Attribute):
        return (_dotted(value).split(".")[0] == bound
                and value.attr in _TEXT_ATTRIBUTES)
    if isinstance(value, ast.Call):
        tail = _dotted(value.func).split(".")[-1]
        if tail not in _TEXT_RENDERING_CALLS and tail != "join":
            return False
        return any(isinstance(n, ast.Name) and n.id == bound
                   for n in ast.walk(value))
    return False


def _enclosing_span(node: ast.AST, ancestors: dict[ast.AST, ast.AST],
                    fallback: tuple[int, int]) -> tuple[int, int]:
    """轉手出來的名字活多久：到所在函式結束為止。

    只算 `except` 區塊的範圍會漏掉真實案例——`discord_bot` 的 log 輪替就是在
    handler 裡把例外改名，然後在 handler **外面**用掉。
    """
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.lineno, current.end_lineno or current.lineno
        current = ancestors.get(current)
    return fallback


def _exception_bindings(tree: ast.Module) -> list[tuple[str, set[str], int, int]]:
    """`except X as name:` → `(name, 型別名集合, 起, 迄)`。

    **也追一跳的轉手。** 原本只認 `except … as name` 綁到的那個名字本身，於是
    `detail = str(error)` 之後送 `{detail}` 整個掉出掃描範圍。實測樹上真的有這個
    形狀：`discord_bot` 的 log 輪替把一個 `OSError` 直接改名成 `rename_reason`，
    在 `except` 區塊外面用掉（它目前只進 stderr 而且用 `!r`，所以不是洩漏——但掃描
    器本來也看不見它，這一段補的就是那個盲點）。
    """
    bindings: list[tuple[str, set[str], int, int]] = []
    ancestors = _ancestor_map(tree)
    module_span = (1, max((getattr(n, "lineno", 1) for n in ast.walk(tree)),
                          default=1))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not node.name:
            continue
        raw = node.type
        candidates = raw.elts if isinstance(raw, ast.Tuple) else [raw]
        types = {_dotted(c).split(".")[-1] for c in candidates if c is not None}
        bindings.append((node.name, types, node.lineno,
                         node.end_lineno or node.lineno))
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign):
                value, targets = inner.value, inner.targets
            elif isinstance(inner, ast.AnnAssign) and inner.value is not None:
                value, targets = inner.value, [inner.target]
            else:
                continue
            if not _renders_text_of(value, node.name):
                continue
            reach = _enclosing_span(inner, ancestors, module_span)
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings.append((target.id, types, inner.lineno, reach[1]))
    return bindings


def _exception_types_for(variable: str, line: int,
                         bindings=None) -> set[str] | None:
    """行號落在哪個 `except … as variable` 區塊裡（取最內層）。"""
    best: tuple[set[str], int] | None = None
    for name, types, start, end in (EXCEPTION_BINDINGS if bindings is None
                                    else bindings):
        if name == variable and start <= line <= end:
            if best is None or start > best[1]:
                best = (types, start)
    return best[0] if best else None


def _guarded_by_isinstance(node: ast.expr, ancestors: dict[ast.AST, ast.AST],
                           variable: str) -> bool:
    """`f"{error}" if isinstance(error, GuiError) else "…"` 這種寫法算安全。

    這是本專案既有的慣用寫法（同一個 `except` 同時接自訂例外與 `ValueError`
    時，只有自訂那支的訊息可以回給使用者）。
    """
    current: ast.AST | None = node
    while current is not None:
        parent = ancestors.get(current)
        if isinstance(parent, ast.IfExp) and parent.body is current:
            test = parent.test
            if isinstance(test, ast.Call) and _dotted(test.func) == "isinstance" \
                    and len(test.args) == 2:
                target = test.args[0]
                checked = test.args[1]
                names = {_dotted(c).split(".")[-1]
                         for c in (checked.elts
                                   if isinstance(checked, ast.Tuple) else [checked])}
                if isinstance(target, ast.Name) and target.id == variable \
                        and names <= _SAFE_EXCEPTION_TYPES:
                    return True
        current = parent
    return False


def _ancestor_map(root: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(root):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


# **順序有意義**：`_exception_bindings` 會用到 `_ancestor_map`（一跳轉手出來的名字活到所在
# 函式結束，所以要往上找函式節點），因此這一行必須排在它下面，不能跟著函式定義一起擺。
EXCEPTION_BINDINGS = _exception_bindings(_bot_tree())


def _is_type_name_read(node: ast.AST, ancestors: dict[ast.AST, ast.AST]) -> bool:
    """`type(error).__name__` —— 只有型別名、沒有訊息內容。

    這是本專案刻意用來取代 `{error}` 的寫法（例如 `!launch` 失敗時回
    `` `OSError` ``），放行。型別名仍是不受控字串，但它是識別字、不會夾帶路徑或
    憑證；真要收緊的話是另一個決定，不是這條規則。
    """
    call = ancestors.get(node)
    if not (isinstance(call, ast.Call) and _dotted(call.func) == "type"):
        return False
    attribute = ancestors.get(call)
    return isinstance(attribute, ast.Attribute) and attribute.attr == "__name__"


def _inside_isinstance_test(node: ast.AST,
                            ancestors: dict[ast.AST, ast.AST]) -> bool:
    """名字出現在 `isinstance(error, GuiError)` 的判斷式裡，不是被送出去。"""
    current: ast.AST | None = node
    while current is not None:
        parent = ancestors.get(current)
        if isinstance(parent, ast.Call) and _dotted(parent.func) == "isinstance":
            return True
        current = parent
    return False


def test_no_raw_exception_text_in_replies():
    """任何形式的內插都算：f-string、`str()` / `repr()`、字串相加、`.format()`。

    只看 `FormattedValue` 是不夠的——`"failed: " + str(error)` 一樣會把原文送出
    去，所以這裡掃的是送出引數底下**每一個名字**。
    """
    violations: list[str] = []
    for call in SENDER_CALLS:
        for argument in _message_arguments(call):
            ancestors = _ancestor_map(argument)
            for node in _walk(argument):
                if not isinstance(node, ast.Name):
                    continue
                types = _exception_types_for(node.id, call.lineno)
                if types is None or types <= _SAFE_EXCEPTION_TYPES:
                    continue
                if _is_type_name_read(node, ancestors):
                    continue
                if _inside_isinstance_test(node, ancestors):
                    continue
                if _guarded_by_isinstance(node, ancestors, node.id):
                    continue
                violations.append(
                    f"discord_bot.py:{call.lineno} 送出 `{node.id}`"
                    f"（`except {sorted(types)} as {node.id}`）")
    assert not violations, (
        "以下送出點會把原始例外文字送到對話平台，那可能含絕對路徑、外部服務名或"
        "憑證片段：\n  " + "\n  ".join(violations)
        + "\n改法：詳細內容 `print(..., file=sys.stderr)`，回覆只給泛用句；"
          "自訂例外（GuiError 家族）才可以直接內插。")


def _exempted_classes() -> dict:
    """`_SAFE_EXCEPTION_TYPES` 裡每一個名字 → 它的 `ClassDef`（找得到的話）。

    **為什麼要推導而不是寫死一個名字。** 這份白名單的代價是「掃描器對這些型別
    完全停止追問」，而那個代價的前提（訊息一個字都不從輸入來）只有**有建構子的
    型別**能用這個方法驗。以前這裡寫死了 `_UndoBackupUnavailable` 一個，於是白名單
    多收一個有建構子的型別就多一個沒人驗的前提——而那正是這一族豁免反覆出現的
    失效形狀。

    兩個模組都找：`_GuiError` / `_GuiAborted` 是 `_gui_control` 那兩個類別的別名
    （`from _gui_control import GuiError as _GuiError`），所以名字前面的底線要剝掉
    再試一次。找不到不是失敗——別名與 import 本來就不會有 `ClassDef`；真正會紅的是
    「一個都找不到」。
    """
    trees = {"discord_bot.py": _bot_tree()}
    gui = PKG_ROOT / "_gui_control.py"
    if gui.exists():
        trees["_gui_control.py"] = ast.parse(gui.read_text(encoding="utf-8"),
                                             str(gui))
    by_name: dict = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                by_name.setdefault(node.name, node)
    found: dict = {}
    for name in _SAFE_EXCEPTION_TYPES:
        cls = by_name.get(name) or by_name.get(name.lstrip("_"))
        if cls is not None:
            found[name] = cls
    return found


def _constructor_message_problems(name: str, cls: ast.ClassDef):
    """`None` ＝ 沒有建構子；否則回一份「把輸入拼進訊息」的清單（可能是空的）。

    抽成 helper 的理由是**樹是乾淨的**：寫在測試裡的話，回報違規那幾行永遠不會
    在真實資料上執行，整段刪掉照樣全綠。下面的合成對照組餵的就是這一支。
    """
    init = next((n for n in cls.body
                 if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
    if init is None:
        return None
    supers = [n for n in ast.walk(init)
              if isinstance(n, ast.Call)
              and ast.unparse(n.func) == "super().__init__"]
    if not supers:
        return [f"`{name}` 的建構子沒有呼叫 `super().__init__`，訊息從哪來？"]
    return [f"`{name}`：super().__init__({ast.unparse(argument)})"
            for call in supers for argument in call.args
            if not isinstance(argument, (ast.Constant, ast.Attribute))]


_CTOR_CONTROL = """
class Clean(Exception):
    GENERIC = "泛用句"

    def __init__(self, path, cause):
        super().__init__(self.GENERIC)
        self.path = path


class LeaksAnFString(Exception):
    def __init__(self, path, cause):
        super().__init__(f"讀不到 {path}")


class LeaksAConcat(Exception):
    def __init__(self, path, cause):
        super().__init__("讀不到 " + str(path))


class NoSuperCall(Exception):
    def __init__(self, path, cause):
        self.path = path


class NoConstructor(Exception):
    pass
"""


def test_the_constructor_check_catches_every_shape_it_claims_to():
    """合成對照組。**沒有這一支，把偵測整段刪掉會照樣全綠**——白名單裡今天所有
    型別的建構子都是乾淨的，所以那幾行在真實資料上一次都不會執行。

    `NoConstructor` 要回 `None`（不是空清單）：那是「這個方法驗不到它」，與「驗過
    了、沒問題」是兩件事，而上面那支的 `checked` 計數正是靠這個分得開。
    """
    tree = ast.parse(_CTOR_CONTROL)
    classes = {node.name: node for node in ast.walk(tree)
               if isinstance(node, ast.ClassDef)}
    verdict = {name: _constructor_message_problems(name, cls)
               for name, cls in classes.items()}

    assert verdict["NoConstructor"] is None, "沒有建構子要回 None，不是空清單"
    assert verdict["Clean"] == [], f"乾淨的建構子被誤報了：{verdict['Clean']}"
    for leaky in ("LeaksAnFString", "LeaksAConcat", "NoSuperCall"):
        assert verdict[leaky], f"`{leaky}` 沒有被抓到"


def test_the_whitelist_is_really_walked_not_hardcoded(monkeypatch):
    """`_exempted_classes()` 要對**白名單裡的每一個名字**查一次，不是查寫死的那個。

    這一格與下面那組「壞掉的前提」的差別很重要：那組把 `_exempted_classes` 整支
    換掉，所以那支函式**自己**的邏輯一行都沒被執行到——變異實測，「把迴圈寫死成
    `("_UndoBackupUnavailable",)`」在那組底下活了下來。對照組要**餵它輸入**，不是
    取代它。

    餵法：把它讀的那棵樹換成合成語料，並把白名單換成語料裡的兩個名字。真的走過
    白名單的實作會回兩個；寫死的那個回空。
    """
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_bot_tree", lambda: ast.parse(_CTOR_CONTROL))
    monkeypatch.setattr(module, "_SAFE_EXCEPTION_TYPES",
                        {"Clean", "LeaksAnFString"})
    found = _exempted_classes()
    assert set(found) == {"Clean", "LeaksAnFString"}, (
        f"`_exempted_classes()` 沒有走過整份白名單，只拿到 {sorted(found)}。")


def _ctor_control_classes() -> dict:
    tree = ast.parse(_CTOR_CONTROL)
    return {node.name: node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)}


_BROKEN_PREMISES = [
    ("一個型別都解析不到", lambda c: {}),
    ("沒有任何一個有建構子",
     lambda c: {"_UndoBackupUnavailable": c["NoConstructor"]}),
    ("把輸入拼進了例外訊息",
     lambda c: {"_UndoBackupUnavailable": c["Clean"],
                "SecondWhitelistedType": c["LeaksAnFString"]}),
]


@pytest.mark.parametrize("phrase,build", _BROKEN_PREMISES,
                         ids=[p for p, _b in _BROKEN_PREMISES])
def test_the_whitelist_premise_check_fires_on_each_broken_premise(
        monkeypatch, phrase, build):
    """三種壞掉的前提，各要有自己的紅燈。

    為什麼需要：白名單裡**有建構子的型別今天只有一個**，所以在真實資料上——
    「把迴圈寫死成那一個」與「推導整份白名單」結果完全一樣，兩道下限也永遠不會
    開火。變異實測：那三個改動原本**全部活了下來**。

    第三格是這裡最重要的一格：它塞進第二個被豁免的型別（而且是會洩漏的那種），
    所以只有**真的走過整份白名單**的實作才抓得到——那正是這支測試改成推導的理由。
    """
    classes = _ctor_control_classes()
    monkeypatch.setattr(sys.modules[__name__], "_exempted_classes",
                        lambda: build(classes))
    with pytest.raises(AssertionError, match=phrase):
        test_the_refusal_exception_really_has_a_hard_coded_message()


def test_the_refusal_exception_really_has_a_hard_coded_message():
    """白名單那一筆的**前提**要被驗證，不能只寫在註解裡。

    `_UndoBackupUnavailable` 被放進 `_SAFE_EXCEPTION_TYPES`，代價是上面那支掃描器
    對它完全停止追問——所以只要有人為了 debug 方便把 `f"...{path.name}...{cause!r}"`
    塞回建構子，檔名與原始例外就可以從任何送出點流到對話平台，而**整份測試照樣
    全綠**（豁免本身就是「不要再看」的意思）。這一支就是那個豁免的機制檢查。

    `GuiError` 家族沒有 `__init__`——訊息由 raise 端提供，所以**建構子這一招對它
    無效**。這裡只驗得了有建構子的這一個。

    ⚠️ 這段原本接著寫「那也正是唯一需要驗的一個」，**那句是錯的**（2026-09-20
    更正）：它是唯一**能用這個方法**驗的一個，不是唯一需要驗的。`GuiError` 家族
    的豁免範圍比它大得多（`_gui_control.py` 有 174 個 raise，`discord_bot.py` 8
    個），而在此之前那 182 句「靠慣例」沒有任何東西在看。下一節就是補上的那一半：
    同樣三條規則，改在 raise 端執行。
    """
    classes = _exempted_classes()
    assert classes, (
        "白名單裡一個型別都解析不到類別定義——抽取器壞了，而『沒有型別需要驗』"
        "跟『全部都驗過了』在輸出上長得一模一樣。")
    assert "_UndoBackupUnavailable" in classes, (
        "`_UndoBackupUnavailable` 不見了（改名？）。它列在 "
        "`_SAFE_EXCEPTION_TYPES` 裡，名字對不上就等於那筆豁免在保護一個不存在的"
        "型別，而真正在跑的那個型別沒人守。")
    checked, problems = 0, []
    for name, cls in sorted(classes.items()):
        verdict = _constructor_message_problems(name, cls)
        if verdict is None:
            continue          # 沒有建構子（`GuiError` 家族）——另一節負責它們
        checked += 1
        problems.extend(verdict)
    assert not problems, (
        "這些型別的建構子把輸入拼進了例外訊息：\n  " + "\n  ".join(problems)
        + "\n它們能列進 `_SAFE_EXCEPTION_TYPES` 靠的就是「訊息一個字都不從輸入"
          "來」——檔名與原始例外請留在 `path` / `cause` 這類屬性上，只經由 "
          "`_list_label` / `_owner_error` 這兩個既有的單一決策點露出。")
    assert checked >= 1, (
        f"白名單裡 {sorted(classes)} 沒有任何一個有建構子——那這支測試什麼都沒驗，"
        "而它的名字讓人以為驗過了。")


# --------------------------------------------------------------------------
# 1b. 豁免的前提（另一半）：`GuiError` 家族的訊息真的是本模組寫死的
#
# `_SAFE_EXCEPTION_TYPES` 讓上面那支掃描器對 `GuiError` / `GuiAborted` **完全停止
# 追問**，也就是 `discord_bot` 十幾處 `except _GuiError as error:` 之後直接把
# `{error}` 送出去是靠這份豁免才合法的。豁免的代價寫在上一支測試的 docstring 裡：
# 只要有人把原始例外拼進那個訊息，原文就可以從任何送出點流到對話平台，而**整份
# 測試照樣全綠**。
#
# 所以這一節把同樣三條規則搬到 **raise 端**執行：原始例外文字、主機路徑字面、外部
# 服務名。共用的是同一批常數與同一支 `_exception_bindings` ／ `_banned_words_in`
# ——不是再寫一份平行實作，那樣兩邊各自綠、卻沒有任何東西在比對它們。
# --------------------------------------------------------------------------
_GUI_ERROR_FAMILY = {"GuiError", "_GuiError", "GuiAborted", "_GuiAborted"}

# 會 raise 這個家族的正式模組。由 `_modules_raising_the_exempted_family()` 反查，
# 兩個方向對帳——新的模組開始 raise `GuiError` 時必須加進來，否則它那些訊息會在
# 沒有任何守門的情況下取得同一份豁免。
_GUI_ERROR_RAISERS = ("_gui_control.py", "discord_bot.py")


def _family_raises(tree: ast.Module) -> list[ast.Raise]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        name = None
        if isinstance(exc, ast.Call):
            name = _dotted(exc.func).split(".")[-1]
        elif isinstance(exc, ast.Name):
            name = exc.id
        if name in _GUI_ERROR_FAMILY:
            found.append(node)
    return found


def _raiser_trees() -> list[tuple[str, ast.Module]]:
    trees = []
    for name in _GUI_ERROR_RAISERS:
        path = PKG_ROOT / name
        assert path.exists(), f"`_GUI_ERROR_RAISERS` 指到一個不存在的模組：{name}"
        trees.append((name, ast.parse(path.read_text(encoding="utf-8"), str(path))))
    return trees


def _modules_raising_the_exempted_family() -> set[str]:
    """樹上實際會 raise 這個家族的正式模組（測試檔不算——它們不送訊息）。"""
    found = set()
    # `test/`（本檔所在的目錄）照同一個判準過濾：`conftest.py` 在 2026-09-22 之前住在
    # 套件裡、在範圍內，搬家之後照舊。
    for path in (sorted(PKG_ROOT.glob("*.py"))
                 + sorted(Path(__file__).resolve().parent.glob("*.py"))
                 + sorted(PKG_ROOT.parent.glob("*.py"))):
        if path.name.startswith("test_") or path.name.startswith("_test_"):
            continue
        if _family_raises(ast.parse(path.read_text(encoding="utf-8"), str(path))):
            found.add(path.name)
    return found


def test_the_exempted_family_raisers_are_all_scanned():
    """新的模組開始 raise `GuiError` → 必須加進 `_GUI_ERROR_RAISERS`。

    這是豁免的**範圍**那一半。規則寫的是型別，而型別可以在任何模組被 raise；
    掃描器卻只讀一份名單，所以名單漏一個模組＝那個模組的訊息拿著同一份豁免、
    完全沒人看，而且沒有任何症狀（與 `_OWNER_ONLY_SLASH` 同一個失效形狀）。
    """
    actual = _modules_raising_the_exempted_family()
    declared = set(_GUI_ERROR_RAISERS)
    assert actual, "一個 raise 都沒掃到——抽取器壞了，空的結果跟乾淨長得一樣。"
    assert actual - declared == set(), (
        f"這些模組會 raise 被豁免的型別卻不在掃描範圍內：{sorted(actual - declared)}。"
        "加進 `_GUI_ERROR_RAISERS`。")
    assert declared - actual == set(), (
        f"這些模組已經不 raise 那個家族了：{sorted(declared - actual)}。"
        "留著會讓名單看起來比實際大。")


def _string_constant_assignments(tree: ast.Module) -> dict:
    """模組裡「名字 → 它被指派過的所有字串字面」，但只收**確定是常數**的名字。

    為什麼需要：`_OCR_REASON` 先在模組層指派一句，再由 `_load_ocr()` 用 `global`
    改寫成另外兩句之一，最後 `raise GuiError(_OCR_REASON)`。只看 raise 那一行的引數
    看不到任何字面，所以那三句在此之前**完全沒被掃到**——變異實測：把它改成辨識引擎
    的實名，守門不叫。

    篩選條件是「這個名字只被指派過字串字面，而且從來不是某個函式的參數」。沒有這道
    篩選就會開始亂叫：`verb` / `label` 這類名字在別處是參數或由切片得來，它們的值
    不是模組寫死的，硬拿模組裡某個同名字面去比對只會給出錯的答案。
    """
    string_values: dict = {}
    disqualified: set = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in (args.posonlyargs + args.args + args.kwonlyargs
                        + ([args.vararg] if args.vararg else [])
                        + ([args.kwarg] if args.kwarg else [])):
                disqualified.add(arg.arg)
            continue
        if isinstance(node, ast.Assign):
            value, targets = node.value, node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value, targets = node.value, [node.target]
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            target = node.target
            if isinstance(target, ast.Name):
                disqualified.add(target.id)
            continue
        elif isinstance(node, ast.For):
            for target in ast.walk(node.target):
                if isinstance(target, ast.Name):
                    disqualified.add(target.id)
            continue
        else:
            continue
        literal = (isinstance(value, ast.Constant)
                   and isinstance(value.value, str))
        for target in targets:
            for sub in ast.walk(target):
                if not isinstance(sub, ast.Name):
                    continue
                if literal and isinstance(target, ast.Name):
                    string_values.setdefault(sub.id, []).append(value.value)
                else:
                    disqualified.add(sub.id)
    return {name: values for name, values in string_values.items()
            if name not in disqualified}


def _family_message_violations(name: str, tree: ast.Module) -> list[str]:
    """一個模組裡，`GuiError` 家族的訊息踩到三條規則的地方。

    抽成 helper 是因為樹是乾淨的：直接寫在測試裡的話，**回報違規那幾行永遠不會
    執行**，把 `violations.append` 刪掉照樣全綠。下面的合成對照組餵的就是這支。
    """
    bindings = _exception_bindings(tree)
    constants = _string_constant_assignments(tree)
    violations: list[str] = []
    for node in _family_raises(tree):
        exc = node.exc
        if not isinstance(exc, ast.Call):
            continue                      # `raise error` —— 轉手既有的例外
        message_args = list(exc.args) + [kw.value for kw in exc.keywords]
        for argument in message_args:
            ancestors = _ancestor_map(argument)
            for sub in ast.walk(argument):
                if isinstance(sub, ast.Name):
                    types = _exception_types_for(sub.id, node.lineno, bindings)
                    if types is not None and not types <= _SAFE_EXCEPTION_TYPES:
                        if not _is_type_name_read(sub, ancestors):
                            violations.append(
                                f"{name}:{node.lineno} 把 `{sub.id}` 拼進訊息"
                                f"（except {sorted(types)} as {sub.id}）")
                        continue
                    for text in constants.get(sub.id, ()):
                        violations.extend(
                            _text_rule_violations(name, node.lineno, text,
                                                  via=sub.id))
                elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    violations.extend(
                        _text_rule_violations(name, node.lineno, sub.value))
    return violations


def _text_rule_violations(name: str, lineno: int, text: str,
                          via: str | None = None) -> list[str]:
    """一段訊息文字踩到的「主機路徑字面」與「外部服務名」兩條規則。

    `via` 是這段文字**不是**直接寫在 raise 那一行、而是經由某個模組常數進來時的
    那個常數名字——訊息要指得出來，否則讀的人會到 raise 那一行去找一句不在那裡的話。
    """
    where = f"{name}:{lineno}" + (f"（經由 `{via}`）" if via else "")
    found = []
    for pattern in _HOST_PATH_PATTERNS:
        if re.search(pattern, text):
            found.append(f"{where} 主機路徑字面 `{pattern}` → {text[:60]!r}")
    for word in _banned_words_in(text):
        found.append(f"{where} 外部服務名 `{word}` → {text[:60]!r}")
    return found


def test_the_exempted_family_messages_are_written_by_us():
    """182 句「可以直接送給使用者」的訊息，現在真的被檢查了。

    三條規則與 bot 那一側**完全一樣**，只是執行的位置從送出點移到 raise 端：
    原始例外文字不得內插（`except GuiError as error` 之後再包一層是合法的——那
    是把一句安全的話包進另一句安全的話裡，樹上有三處）、不得有主機路徑字面、
    不得出現外部服務／驅動／辨識引擎的名字。
    """
    counted = 0
    violations: list[str] = []
    for name, tree in _raiser_trees():
        counted += len(_family_raises(tree))
        violations.extend(_family_message_violations(name, tree))
    assert counted >= 150, (
        f"只掃到 {counted} 個 raise——抽取器壞了。`_gui_control.py` 一支就有 170 "
        "幾個，數字掉下來代表 raise 的寫法變了而掃描器沒跟上。")
    assert not violations, (
        "以下 `GuiError` 訊息不是本專案寫死的泛用句，而 `_SAFE_EXCEPTION_TYPES` "
        "讓送出點對它們完全不設防：\n  " + "\n  ".join(violations)
        + "\n改法：細節 `print(..., file=sys.stderr)`，訊息只留泛用句。")


def test_the_raise_floors_fire_when_the_extractor_comes_back_empty(monkeypatch):
    """兩道下限都要有一支把前提打壞的對照組。

    `counted >= 150` 與「一個 raise 都沒掃到」在**乾淨的資料上量不出來**：變異實測
    顯示把前者放寬成 `>= 0` 照樣全綠。空的抽取結果跟「全部合規」在輸出上長得一模
    一樣，所以下限是這兩支測試唯一的煙霧偵測器，而下限自己也需要被測。
    """
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_family_raises", lambda _tree: [])
    with pytest.raises(AssertionError, match="抽取器壞了"):
        test_the_exempted_family_messages_are_written_by_us()
    with pytest.raises(AssertionError, match="一個 raise 都沒掃到"):
        test_the_exempted_family_raisers_are_all_scanned()


_FAMILY_CONTROL = '''
class GuiError(Exception):
    pass


_REASON = "文字辨識未啟用：tesseract 無法使用。"
# 同名的模組字面帶著禁字，而底下那支把它當**參數**收進來——所以這一格會在
# 「參數不算常數」那道篩選壞掉時亂叫，也只有那時。
_NOT_A_CONSTANT = "tesseract 只是這裡的假資料。"


def rebinds_the_name(_NOT_A_CONSTANT):
    raise GuiError(_NOT_A_CONSTANT)


def clean():
    raise GuiError("桌面控制功能無法使用。")


def compose():
    try:
        clean()
    except GuiError as error:
        raise GuiError(f"第 1 步不合法：{error}") from error


def leaks_raw_text():
    try:
        clean()
    except OSError as error:
        raise GuiError(f"失敗：{error}")


def leaks_after_one_hop():
    try:
        clean()
    except OSError as error:
        detail = str(error)
    raise GuiError(f"失敗：{detail}")


def leaks_a_path():
    raise GuiError("請看 D:\\\\Codes\\\\out.log。")


def leaks_a_service_name():
    raise GuiError("tesseract 沒有安裝。")


def leaks_through_a_module_constant():
    raise GuiError(_REASON)


def reports_only_the_type_name():
    try:
        clean()
    except OSError as error:
        raise GuiError(f"失敗（{type(error).__name__}）。")
'''


def test_the_family_scanner_catches_every_shape_it_claims_to():
    """合成對照組。**樹是乾淨的，所以偵測那一段從來不會在真實資料上執行**——
    沒有這一支，把 `violations.append` 整段刪掉會照樣全綠（本 repo 已經為這個
    形狀付過一次學費，見 `CLAUDE.md` 對 `_TEXT_FOR_STDOUT` 那一段的記載）。

    四個必須抓到、三個必須放行。`leaks_after_one_hop` 是這一輪才抓得到的那一個
    ——它就是既有掃描器的盲點：原始例外先被轉手給另一個名字，而那個名字在
    `except` 區塊**外面**才被用掉。
    """
    tree = ast.parse(_FAMILY_CONTROL)
    found = _family_message_violations("control.py", tree)
    # 用正規式而不是 `split(":")`：訊息可能帶「（經由 某常數）」後綴，而拆字串的
    # 寫法會在那一天變成 `ValueError` 而不是一句看得懂的斷言失敗。
    blamed = {int(match.group(1))
              for match in (re.match(r"[^:]+:(\d+)", line) for line in found)
              if match}
    assert len(blamed) >= 4, f"抽不出行號，對照組沒在測東西：{found}"
    by_function = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if any(node.lineno <= line <= (node.end_lineno or node.lineno)
                   for line in blamed):
                by_function.add(node.name)
    assert by_function == {"leaks_raw_text", "leaks_after_one_hop",
                           "leaks_a_path", "leaks_a_service_name",
                           "leaks_through_a_module_constant"}, (
        f"抓到的是 {sorted(by_function)}。`clean` / `compose` / "
        "`reports_only_the_type_name` 必須放行（第二個是把一句安全的話包進另一句，"
        "第三個是本專案刻意用來取代原始例外的寫法）。")


def test_no_stack_trace_in_replies():
    violations = []
    for call in SENDER_CALLS:
        for argument in _message_arguments(call):
            for node in _walk(argument):
                rendered = ast.unparse(node) if isinstance(
                    node, (ast.FormattedValue, ast.Call)) else ""
                if "format_exc" in rendered or "print_exception" in rendered:
                    violations.append(f"discord_bot.py:{call.lineno}")
    assert not violations, (
        f"這些送出點帶了 traceback：{sorted(set(violations))}。"
        "traceback 一定含主機路徑，只能進 log。")


# --------------------------------------------------------------------------
# 2. 主機路徑字面
# --------------------------------------------------------------------------
def _host_path_violations(rows) -> list[str]:
    violations = []
    for call_line, lineno, text, via in rows:
        for pattern in _HOST_PATH_PATTERNS:
            if re.search(pattern, text):
                violations.append(
                    f"discord_bot.py:{call_line}{_via(via, lineno)} `{pattern}` → "
                    f"{text[:60]!r}")
    return violations


def test_no_host_path_literals_in_replies():
    violations = _host_path_violations(SENT_LITERALS)
    assert not violations, (
        "以下送出點含主機路徑字面：\n  " + "\n  ".join(violations)
        + "\n改法：用 `_list_label()` 之類的泛用標籤，不要回貼檔名或路徑。")


# --------------------------------------------------------------------------
# 3. 檔名物件
# --------------------------------------------------------------------------
_FS_ATTRIBUTES = {"name", "stem", "parent"}


def _attribute_root(node: ast.expr) -> str:
    """`a[0].parent.name` → `a`：一路剝到最左邊那個名字。"""
    while True:
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            break
    return node.id if isinstance(node, ast.Name) else ast.unparse(node)


def test_no_filesystem_names_in_replies():
    """任何位置的 `.name` / `.stem` / `.parent` 都算，不只 f-string 裡的。

    這裡原本只看 `ast.FormattedValue`，於是 `reply("檔案：" + path.name)` 與
    `reply(str(path.name))` 這兩種同樣會把磁碟命名慣例送出去的寫法完全掃不
    到。改成直接看節點本身之後，包在什麼寫法裡就不再影響判定。"""
    violations = []
    for call in SENDER_CALLS:
        for argument in _message_arguments(call):
            for node in _walk(argument):
                if not isinstance(node, ast.Attribute) \
                        or node.attr not in _FS_ATTRIBUTES:
                    continue
                if _attribute_root(node.value) in _PLATFORM_OBJECT_BASES:
                    continue
                violations.append(
                    f"discord_bot.py:{call.lineno} → `{ast.unparse(node)}`")
    assert not violations, (
        "以下送出點回貼了磁碟上的檔名／目錄名：\n  " + "\n  ".join(violations)
        + "\n附件本身就會帶檔名，文字裡不需要再講一次；狀態回覆請用泛用標籤。"
          "若那其實是對話平台自己的物件，把它的變數名加進 `_PLATFORM_OBJECT_BASES`。")


# --------------------------------------------------------------------------
# 4. 外部服務名
# --------------------------------------------------------------------------
def _banned_words_in(text: str) -> set[str]:
    """文字裡出現了哪些禁字——**扣掉裁定涵蓋的形狀之後**才比對。

    ⚠️ 順序很重要：先 `_strip_sanctioned` 再比對，不是先比對再減。減法那個寫法
    在 2026-09-11 之前是 no-op（見 `_BANNED_WORDS` 上的註解），而且就算不是
    no-op，它給的也是「整個字免死」——比兩條裁定都寬。
    """
    probe = _strip_sanctioned(text).lower()
    return {word for word in _BANNED_WORDS
            if re.search(rf"\b{re.escape(word)}\b", probe)}


def _declared_slash_names() -> set[str]:
    """指令樹上宣告出來的 `name=`（含群組與 `Choice`）。

    直接用既有的 `SLASH_SURFACE_STRINGS`，不另寫一份擷取器——抄本會漂移。
    """
    return {text.lower() for _lineno, field, text in SLASH_SURFACE_STRINGS
            if field == "name"}


def _ai_token_legal_values() -> set[str]:
    """`@bot ai <…>` 實際接受的值，從 `mcmd_ai` 的比較式抽出來。"""
    for node in ast.walk(_bot_tree()):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "mcmd_ai"):
            continue
        for inner in ast.walk(node):
            if not (isinstance(inner, ast.Compare)
                    and len(inner.ops) == 1
                    and isinstance(inner.ops[0], ast.NotIn)
                    and inner.comparators
                    and isinstance(inner.comparators[0], ast.Tuple)):
                continue
            values = {element.value.lower()
                      for element in inner.comparators[0].elts
                      if isinstance(element, ast.Constant)
                      and isinstance(element.value, str) and element.value}
            if values:
                return values
    return set()


def _stale_entries(sanctioned, real) -> tuple[list[str], int]:
    """-> (在 `real` 裡已經不存在的豁免條目, **實際檢查了幾筆**)。

    ⚠️ 為什麼要回傳分母：真實資料乾淨的時候，「每一筆都檢查」與「只檢查第一筆」
    給出一模一樣的空清單。變異實測（2026-09-11，S5）把比較式換成
    `set(list(_ALLOWED_COMMAND_NAME_WORDS)[:1]) - declared` —— **存活**，87 支全綠。
    一個乾淨的清單讓真實資料上的斷言變得無法驗證，所以比較本身要放進一個有自己
    合成對照組的 helper（`CLAUDE.md` 對擁有者閘那組對帳寫的就是這個做法），再由
    呼叫端釘住「問過的筆數 ＝ 清單長度」。只有前者擋得住比較式被改壞，只有後者
    擋得住呼叫端在傳進來之前就先把清單截短。
    """
    sanctioned = set(sanctioned)
    return sorted(sanctioned - set(real)), len(sanctioned)


def test_the_staleness_comparison_actually_bites():
    """`_stale_entries` 的合成對照組——真實清單乾淨時它是永遠成立的斷言。"""
    assert _stale_entries({"booru"}, {"booru", "iqdb"}) == ([], 1)
    assert _stale_entries({"booru", "gone"}, {"booru"}) == (["gone"], 2)
    # **每一筆**都要被問到，不是只問其中一筆。
    assert _stale_entries({"a", "b", "c"}, {"a"}) == (["b", "c"], 3)
    assert _stale_entries(set(), {"a"}) == ([], 0)


def test_every_sanctioned_word_is_actually_a_banned_word():
    """⚠️ 這一支就是 2026-09-11 那個缺陷的守門，**它比下面兩支重要**。

    兩份允許清單在此之前完全沒有作用：豁免是用 `hits - 允許清單` 套用的，而
    `hits ⊆ _BANNED_WORDS`，所以減掉一個不在 `_BANNED_WORDS` 裡的字是 no-op。
    實測交集是空集合——四個圖庫指令名與兩個後端別名**從來沒有被掃描過**，而
    `CLAUDE.md` 把這兩份清單寫成 2026-08-17／2026-07-02 兩條裁定的實作方式。
    「有一份寫滿理由的豁免清單」讀起來像「這件事有在管」，實際上兩者無關。

    所以豁免的前提要被釘住：**能被豁免的字，必須先是被禁的字**。
    """
    sanctioned = (_ALLOWED_COMMAND_NAME_WORDS | _ALLOWED_BACKEND_ALIASES
                  | _ALLOWED_MODEL_ALIASES)
    assert sanctioned, "允許清單是空的——這支測試會退化成空集合比較，永遠成立。"
    # 判準是「**掃得到**才值得豁免」，不是「等於某個禁字」。帶版號的模型 key
    # （`opus-5`）本身不在 `_BANNED_WORDS` 裡，但它**含有**禁字、確實會被掃到，
    # 所以豁免它是有作用的。用相等比對的話這一整批會被判成裝飾而誤紅。
    inert = sorted(word for word in sanctioned if not _has_banned_word(word))
    assert not inert, (
        f"這些字列在允許清單裡，卻不含任何 `_BANNED_WORDS` 的字：{inert}。"
        "豁免是從掃描結果裡扣掉東西，掃不到的字扣了也是白扣——那份清單會變成"
        "純粹的裝飾，而且讀起來像是這件事有人在管。要嘛把字加進 `_BANNED_WORDS`，"
        "要嘛把它從允許清單拿掉並說明為什麼它本來就不需要管。")


def test_the_sanctioned_command_words_still_name_real_commands():
    """`_ALLOWED_COMMAND_NAME_WORDS` 的每一筆都要還是一個真的指令名。

    那份清單的理由是「這是既有公開指令的名稱，使用者得打得出來」。指令改名或
    移除之後，理由就不成立了，但那個字會**繼續**在所有表面上被放行——守門照跑、
    測試照綠，跟 `_OWNER_ONLY_SLASH` 同一個形狀（`CLAUDE.md` 記著）。
    """
    declared = _declared_slash_names()
    assert len(declared) >= 50, (
        f"只抽到 {len(declared)} 個宣告出來的指令名，擷取器大概壞了——"
        "而擷取器壞掉的樣子跟「每一筆都對得上」一模一樣。")
    stale, checked = _stale_entries(_ALLOWED_COMMAND_NAME_WORDS, declared)
    assert checked == len(_ALLOWED_COMMAND_NAME_WORDS), (
        f"只問了 {checked} 筆，清單裡有 "
        f"{len(_ALLOWED_COMMAND_NAME_WORDS)} 筆——比對的範圍被縮小了。")
    assert not stale, (
        f"這些字還列在 `_ALLOWED_COMMAND_NAME_WORDS`，但指令樹上已經沒有同名的"
        f"指令了：{stale}。那份豁免的理由是「它是既有指令的名稱」，理由沒了就該"
        "把它拿掉，否則那個服務名會在所有表面上繼續被放行。")


def test_the_sanctioned_backend_aliases_are_still_legal_values():
    """`_ALLOWED_BACKEND_ALIASES` 的每一筆都要還是 `ai` token 接受的值。"""
    legal = _ai_token_legal_values()
    assert legal, (
        "抽不到 `mcmd_ai` 的合法值——比較式的寫法大概變了。抽不到的話下面那句"
        "會退化成「空集合減法」，永遠通過。")
    stale, checked = _stale_entries(_ALLOWED_BACKEND_ALIASES, legal)
    assert checked == len(_ALLOWED_BACKEND_ALIASES), (
        f"只問了 {checked} 筆，清單裡有 "
        f"{len(_ALLOWED_BACKEND_ALIASES)} 筆——比對的範圍被縮小了。")
    assert not stale, (
        f"這些別名還列在 `_ALLOWED_BACKEND_ALIASES`，但 `mcmd_ai` 已經不接受"
        f"它們了：{stale}。2026-07-02 的窄範圍例外放行的是**功能面的合法值**，"
        "不再是合法值就沒有理由繼續放行。")


def test_the_sanctioned_model_aliases_are_still_legal_values():
    """`_ALLOWED_MODEL_ALIASES` 的每一筆都要還是 `/model` 接受的值。

    與上一支同形，但**來源不同**：後端別名的來源是 `mcmd_ai` 的比較式，模型別名
    的來源是 `dorossi_backend.DOROSSI_MODEL_CHOICES` 的 key。兩者共用一支反查就會
    在其中一邊退化成空集合比較——那正是這個 repo 反覆記著的那個形狀。
    """
    import dorossi_backend as DB
    legal = {str(key).lower() for key in DB.DOROSSI_MODEL_CHOICES}
    assert legal, (
        "`DOROSSI_MODEL_CHOICES` 是空的——下面那句會退化成空集合減法，永遠通過。")
    stale, checked = _stale_entries(_ALLOWED_MODEL_ALIASES, legal)
    assert checked == len(_ALLOWED_MODEL_ALIASES), (
        f"只問了 {checked} 筆，清單裡有 "
        f"{len(_ALLOWED_MODEL_ALIASES)} 筆——比對的範圍被縮小了。")
    assert not stale, (
        f"這些別名還列在 `_ALLOWED_MODEL_ALIASES`，但 `DOROSSI_MODEL_CHOICES` "
        f"已經沒有它們了：{stale}。模型改名之後那個豁免會繼續生效而沒有任何症狀，"
        "所以理由沒了就要一起拿掉。")


@pytest.mark.parametrize("text, expected", [
    # 裁定涵蓋的形狀——放行。
    ("booru", set()),
    ("`/booru <tag>` 用圖庫搜圖", set()),
    ("/safebooru 搜安全圖", set()),
    ("用法：`/dorossi ai <claude|codex>`", set()),
    # 模型別名（2026-09-12 補）。**這一格是放寬那一步的唯一殺手**：把
    # `_ALLOWED_MODEL_ALIASES` 整份刪掉之後，只有「本來該放行的」會變紅，
    # 「本來該抓的」不會——只餵必須擋的語料，那個變異會存活。
    ("用法：`/model <opus|sonnet|haiku|fable|default>`", set()),
    # 帶版號的 key（`DOROSSI_MODEL_CHOICES` 2026-09-12 擴成 14 個）。豁免清單是
    # **推導**的，所以這些不必手動補；這一格釘住「推導」這件事本身——改回手打四個
    # 字的話，`opus-5` 不在清單裡 ⇒ 整段不放行 ⇒ `\bopus\b` 又被抓到。
    ("用法：`/model <opus-5|sonnet-4.6|fable-5.1>`", set()),
    # ⚠️ 含禁字的**非**合法值：`novelai-x` 不等於 `novelai`，但 `\bnovelai\b` 在它
    # 裡面。判乾淨的方式若用「相等比對」就會把整段放行、把它藏起來，所以這裡用的
    # 是子字串比對。這一格就是那個差別的殺手。
    ("模型選項：<opus|novelai-x>", {"novelai", "opus"}),
    # 裁定**沒有**涵蓋的形狀——要抓到。這幾筆在舊的減法寫法下全部會漏掉。
    ("圖片來自 booru", {"booru"}),
    ("e621 目前沒有回應，請稍後再試", {"e621"}),
    ("已切換到 claude 後端", {"claude"}),
    ("後端選項：<claude|novelai>", {"claude", "novelai"}),
    # 模型別名的散文用法——放行的是列舉那個形狀，不是這個字。
    ("這題我交給 opus 跑", {"opus"}),
    ("目前用的是 haiku，比較省", {"haiku"}),
    # 列舉裡混進一個沒被裁定過的禁字 → 整段不放行。
    ("模型選項：<opus|novelai>", {"novelai", "opus"}),
    # ⚠️ **跨裁定的混合列舉也不放行。** 兩份清單刻意不取聯集：`claude` 是
    # `/dorossi ai` 的合法值、`opus` 是 `/model` 的，把它們寫成同一個列舉代表
    # 那句用法說明本身就錯了。這一格釘住 `_SANCTIONED_ALIAS_SETS` 不可以被
    # 「順手」改成一個聯集。
    ("選項：<claude|opus>", {"claude", "opus"}),
    # 原本就禁的字不受影響。
    ("danbooru 回傳空結果", {"danbooru"}),
])
def test_the_sanctioned_exemption_is_scoped_not_a_blanket_pass(text, expected):
    """豁免是**形狀**，不是「這個字在任何地方都免死」。

    沒有這一支，把 `_strip_sanctioned` 換回「整個字減掉」也照樣全綠：真實語料裡
    那六個字**只**以裁定涵蓋的形狀出現（2026-09-11 實測扣掉之後剩 0 筆），所以
    寬鬆版與精確版在真實資料上給出一樣的答案。差別只在合成資料上看得見，而那個
    差別正是這次改動的全部價值。
    """
    assert _banned_words_in(text) == expected


def _service_name_violations(rows) -> list[str]:
    violations = []
    for call_line, lineno, text, via in rows:
        for word in _banned_words_in(text):
            if _is_pending_owner_decision(word, text):
                continue
            violations.append(
                f"discord_bot.py:{call_line}{_via(via, lineno)} `{word}` → "
                f"{text[:60]!r}")
    return violations


def test_no_external_service_names_in_replies():
    violations = _service_name_violations(SENT_LITERALS)
    assert not violations, (
        "以下送出點提到了外部服務／驅動／引擎的名字：\n  " + "\n  ".join(violations)
        + "\n改用泛用說法（出圖服務／目標網站／辨識引擎／後端）。")


def test_the_literal_rules_report_a_hit_that_arrived_through_a_variable():
    """合成列：經由變數進來的字面值也要被報，而且要報出是經由哪個名字。

    真實樹除了一筆待決項目之外是乾淨的，所以「規則有沒有吃經由變數的那幾列」在
    真實資料上量不出來——變異實測：把兩條規則改成只看直接寫在引數裡的列，整組
    照樣綠。
    """
    rows = [
        (10, 8, "請看 D:\\Work\\out.log", "usage"),
        (20, 18, "tesseract 沒有安裝", "text"),
        (30, 30, "https://danbooru.donmai.us/posts/", "page"),
        (40, 40, "完成", None),
    ]
    paths = _host_path_violations(rows)
    assert len(paths) >= 1 and all("discord_bot.py:10" in v for v in paths), paths
    assert "經由 `usage`" in paths[0] and "第 8 行" in paths[0], paths
    words = _service_name_violations(rows)
    assert [v.split(" ")[0] for v in words] == [
        "discord_bot.py:20（經由"], words
    assert "`text`" in words[0], words


def _slash_surface_strings() -> list[tuple[int, str, str]]:
    """斜線指令的 `name=` / `description=`，以及參數說明 `describe(...)`。

    這些是**任何伺服器都看得到**的公開字串，跟送出去的訊息同一級，但它們不在
    送出點上，所以上面那些檢查一個都掃不到。
    """
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(_bot_tree()):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        is_command = dotted.endswith(".command") or dotted.endswith("Group")
        is_describe = dotted.endswith("describe")
        # `Choice(name=…, value=…)` 會出現在選項的下拉清單裡，`rename(param=…)`
        # 決定選項顯示出來的名字——兩者都是任何伺服器都看得到的公開字串，卻在
        # 2026-08-19 之前完全掃不到（它們的 dotted name 不以 .command / Group /
        # describe 結尾）。本輪的分組改寫引入了十幾個 Choice，所以補上。
        is_choice = dotted.endswith("Choice")
        is_rename = dotted.endswith("rename")
        if not (is_command or is_describe or is_choice or is_rename):
            continue
        for keyword in node.keywords:
            if not isinstance(keyword.value, ast.Constant):
                continue
            if not isinstance(keyword.value.value, str):
                continue
            if (is_describe or is_rename or is_choice
                    or keyword.arg in ("name", "description")):
                found.append((node.lineno, keyword.arg or "<param>",
                              keyword.value.value))
    return found


SLASH_SURFACE_STRINGS = _slash_surface_strings()


def test_scanner_found_the_slash_surface_strings():
    assert len(SLASH_SURFACE_STRINGS) >= 500, (
        f"只掃到 {len(SLASH_SURFACE_STRINGS)} 個斜線指令字串，宣告寫法可能變了。"
        "斜線是唯一對外介面之後，光是 name ＋ description 就有兩百多對。")


def test_no_app_command_group_subclass():
    """不得用子類別定義指令群。

    子類別會把 `name=` / `description=` 搬進 `super().__init__` 或 class
    keyword，**同時**逃出這支測試的公開字串掃描與 `test_docs_sync.py` 的指令
    抽取。一個寫法讓兩道守門一起失效，所以直接禁掉。
    """
    offenders = []
    for node in ast.walk(_bot_tree()):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if _dotted(base).endswith("Group"):
                offenders.append(f"discord_bot.py:{node.lineno} {node.name}")
    assert not offenders, (
        "這些類別繼承了指令群：" + ", ".join(offenders)
        + "。請改用 `Group(name=…, description=…, parent=…)` 的建構寫法。")


def test_no_external_service_names_in_the_slash_surface():
    """指令名稱與說明也受 Secrecy Layer 1 約束。

    既有的幾個以外部圖庫服務命名的指令是**使用者裁定維持現狀**的（見 CLAUDE.md
    2026-08-17 的窄範圍例外），列在 `_ALLOWED_COMMAND_NAME_WORDS` 裡；重點是新增
    的指令不能再這樣命名，而在這支測試出現之前，那件事完全沒有東西在擋。
    """
    violations = []
    for lineno, field, text in SLASH_SURFACE_STRINGS:
        for word in _banned_words_in(text):
            violations.append(
                f"discord_bot.py:{lineno} {field}=`{word}` → {text[:60]!r}")
    assert not violations, (
        "斜線指令的公開字串提到了外部服務：\n  " + "\n  ".join(violations)
        + "\n指令名稱與說明都會顯示在任何伺服器的指令選單裡。改用泛用說法；"
          "既有的例外是列管制，不得擴大。")


@pytest.mark.parametrize("corpus_name", [
    "CHANNEL_HELP_SECTIONS", "CHANNEL_HELP_SECTIONS_ZH_CN",
    "CHANNEL_HELP_SECTIONS_ZH_TW", "MENTION_HELP_SECTIONS",
    "MENTION_HELP_SECTIONS_ZH_CN", "MENTION_HELP_SECTIONS_ZH_TW",
])
def test_help_corpus_has_no_external_service_names(corpus_name):
    text = "\n".join(getattr(HELP, corpus_name))
    hits = _banned_words_in(text)
    assert not hits, (
        f"{corpus_name} 提到了 {sorted(hits)}。help 是送到對話平台的字串，"
        "同樣受 Secrecy Layer 1 約束（2026-07-02 的 STRICT 裁決撤銷了舊的"
        "頻道內例外，只留下 zh-CN 維持簡體那一條語言例外）。")


def test_help_corpus_has_no_host_paths():
    violations = []
    for corpus_name in ("CHANNEL_HELP_SECTIONS", "CHANNEL_HELP_SECTIONS_ZH_CN",
                        "CHANNEL_HELP_SECTIONS_ZH_TW", "MENTION_HELP_SECTIONS",
                        "MENTION_HELP_SECTIONS_ZH_CN",
                        "MENTION_HELP_SECTIONS_ZH_TW"):
        text = "\n".join(getattr(HELP, corpus_name))
        for pattern in _HOST_PATH_PATTERNS:
            found = re.search(pattern, text)
            if found:
                violations.append(f"{corpus_name}: /{pattern}/ → {found.group(0)!r}")
    assert not violations, (
        "說明文字含主機路徑：\n  " + "\n  ".join(violations))


# --------------------------------------------------------------------------
# 5. 掃描器自己也要被掃
# --------------------------------------------------------------------------
# 上面四支測試都只對著 `discord_bot.py` 斷言「沒有違規」。那種形狀有一個致命的
# 失敗模式：**掃描器壞掉時，它一樣是綠的。**
#
# 這不是假設。`_message_arguments()` 原本只收
# `Constant / JoinedStr / BinOp / IfExp` 四種位置引數，於是頂層寫成函式呼叫的
# 送出點整個掉出範圍——`reply(str(error))` 抓不到，`reply(content=str(error))`
# 抓得到，差別只在引數寫成位置還是關鍵字。四支測試全綠了很久，而
# `test_no_raw_exception_text_in_replies` 的說明白紙黑字寫著這些形式「都算」。
#
# 所以這裡餵合成的**壞**原始碼，斷言掃描器真的看得到。任何人再把收集邏輯縮窄，
# 這裡會先紅。
_SMUGGLING_FORMS = [
    ('message.reply(f"failed: {error}")', "f-string 內插"),
    ('message.reply("failed: " + str(error))', "字串相加"),
    ('message.reply(str(error))', "頂層 str()"),
    ('message.reply(repr(error))', "頂層 repr()"),
    ('message.reply("failed: {}".format(error))', "頂層 .format()"),
    ('message.reply("".join(["x", str(error)]))', "頂層 join()"),
    ('message.reply(content=str(error))', "content= 關鍵字"),
    ('message.reply(error if flag else "泛用句")', "三元運算"),
    ('placeholder.edit(content=f"{error}")', "編輯既有訊息"),
    ('embed.add_field(name="狀態", value=str(error))', "嵌入欄位值"),
    ('embed.set_footer(text=str(error))', "嵌入頁尾"),
    ('discord.Embed(description=str(error))', "嵌入本文"),
    ('safe_reply(message, str(error))', "專案自己的送出包裝"),
]


def _only_sender_call(source: str) -> ast.Call:
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and _is_sender(node)]
    assert len(calls) == 1, f"{source!r} 應該剛好是一個送出點，掃到 {len(calls)} 個"
    return calls[0]


def _names_reached(source: str) -> set[str]:
    call = _only_sender_call(source)
    return {node.id for argument in _message_arguments(call)
            for node in _walk(argument) if isinstance(node, ast.Name)}


@pytest.mark.parametrize("source,label", _SMUGGLING_FORMS,
                         ids=[label for _, label in _SMUGGLING_FORMS])
def test_the_scanner_sees_every_way_of_smuggling_a_name_out(source, label):
    assert "error" in _names_reached(source), (
        f"掃描器看不到「{label}」寫法送出的 `error`：{source}\n"
        "這代表 `_message_arguments()` / `_SENDER_METHODS` / `_TEXT_KEYWORDS` "
        "又縮窄了，上面四支洩漏檢查對這種寫法等於不存在。")


def test_the_owner_gate_exits_are_the_only_blind_spot():
    """`_owner_detail()` 家族的 raw 引數豁免，泛用句照掃。

    豁免必須剛好切在那一個引數上。整個呼叫都跳過的話，非擁有者看到的泛用句就
    沒人檢查了——那正是 Layer 1 唯一還在管的東西。"""
    raw_exempt = _names_reached('message.reply(_owner_error(message, error, "泛用句"))')
    assert "error" not in raw_exempt, (
        "`_owner_error()` 的原始例外引數不該被判成洩漏——擁有者看得到是規則允許的。")

    call = _only_sender_call(
        'message.reply(_owner_error(message, error, "寫入 batch_config.json 失敗"))')
    constants = [node.value for argument in _message_arguments(call)
                 for node in _walk(argument)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert any("batch_config.json" in text for text in constants), (
        "泛用句（非擁有者看到的那一句）沒有被掃到。豁免只能切在 raw 那一個引數，"
        "不是整個呼叫。")


def test_every_owner_gate_exit_actually_exists_in_the_bot():
    """豁免名單不得留下已經改名或刪掉的函式。

    留著一個不存在的名字本身無害，但它會讓人以為某個出口還受管制。真正的風險
    是反過來：名單是**唯一**承認的繞道，所以它必須跟程式碼對得上。"""
    defined = {node.name for node in ast.walk(_bot_tree())
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = sorted(set(_OWNER_GATE_EXITS) - defined)
    assert not missing, (
        f"`_OWNER_GATE_EXITS` 列了 `discord_bot.py` 裡不存在的函式：{missing}。"
        "改名的話請一起改這裡，否則掃描器會繼續豁免一個不存在的出口。")


# ---------------------------------------------------------------------------
# Layer 1 的**執行期**那一半：`_redact_for_discord`
# ---------------------------------------------------------------------------
#
# 這支檔案原本整份都是靜態 AST 掃描——擋的是「有沒有人寫了會送出原始字串的
# 程式碼」。但真正在執行期把主機資訊刷掉的那個函式（27 個呼叫點，含
# `/log tail`、`/log errors`、`cmd_sh`、`cmd_job`、`_handle_event`）**一支測試
# 都沒有**。2026-08-30 補上，同時抓到兩個它一直沒擋住的東西：
#
# 1. `_REDACT_WIN_PATH_RE` 寫的是 `[A-Za-z]:\\`——**只認反斜線**。於是
#    `C:/Users/Example/AppData/Local/Temp/x` 一個字都沒被刷掉。
# 2. 路徑用 `[^\s'"<>|]*` 一路吃到第一個**空白**為止，而這個專案每一個角色
#    資料夾名都含空白，所以 `output\columbina (genshin impact)\…_0087.png`
#    只被刷掉 `output\columbina`，後面整串照送。
#
# 修的時候差點引進一個更糟的回歸：把分隔符放寬成 `[\\/]` 之後，`https://` 裡的
# `s:/` 也符合「字母冒號斜線」。實測 `WEBRunner.log` 裡符合那個形狀的東西**全部**
# 是網址，所以下面「不得誤刷」那組測試比「必須刷掉」那組還重要。

_MUST_REDACT = [
    # (輸入, 不得殘留在輸出裡的字串)
    ("C:/Users/Example/AppData/Local/Temp/x", "AppData"),
    ("C:\\Users\\Example\\AppData\\Local\\Temp\\x", "AppData"),
    ("D:/Work/Example/axiomatic/webrunner_novelai.py", "Example"),
    ("loading profile from D:/Work/Example/.chrome_profile", "Work"),
    ("D:\\Work\\Example\\output\\alice (genshin impact)\\alice_0001.png",
     "genshin impact"),
    # 專案裡**每一個**角色資料夾名都含空白，所以這是常態不是邊界
    ("saved output\\columbina (genshin impact)\\columbina (genshin impact)"
     "_0087_20260829_231614.png", "genshin impact"),
    ("saved output/columbina (genshin impact)/columbina (genshin impact)"
     "_0087.png in 3.2s", "genshin impact"),
    # 這一筆的 banned 是 `_snap` 而不是 `chrome_profile`：只刷掉前綴的話輸出會是
    # `[path]_snap/`，`chrome_profile` 確實不見了，但殘骸仍然透露目錄命名慣例。
    # 一開始把 banned 寫成 `chrome_profile`，於是「後綴不一起吃」那個 mutation
    # 逃掉了——斷言挑錯字串，測試就只是在測一件已經成立的事。
    # 副檔名不在清單上（`.exe`）→ 副檔名分支接不到；名字含空白 → 不吃空白那條
    # 停在第一個空白。這就是 `/log tail` 會原樣送出去的那一行。
    # ⚠️ banned 必須挑**後面**的段落：`Program` 是第一段，一定會被 `[path]` 吃
    # 掉，所以拿 `Program Files` 當 banned 會在**壞掉的**程式碼上通過（實測：壞
    # 掉時輸出是 `[path] Files\\Tesseract-OCR\\…`，`Program Files` 確實不在裡
    # 面）。同 `_snap` 那一筆的教訓：斷言挑錯字串，就只是在測一件已經成立的事。
    ("C:\\Program Files\\Tesseract-OCR\\tesseract.exe is not installed",
     "Tesseract-OCR"),
    # ⚠️ 這一筆是**唯一**分得出「結構性修法」與「只在副檔名清單再加兩個字」的：
    # 它根本沒有副檔名，加再多副檔名都救不了。少了它，只加 `exe|dll` 的半套修法
    # 會是一個全綠的 diff，而整類缺陷還在。
    ("C:\\Program Files\\Tesseract-OCR\\tessdata", "Tesseract-OCR"),
    # 真實形狀是 repr（反斜線加倍）。
    ("[gui] ocr find_text failed: TesseractNotFoundError(\"C:\\\\Program Files"
     "\\\\Tesseract-OCR\\\\tesseract.exe is not installed\")", "Tesseract-OCR"),
    # 相對分支的同一個病——`output/` 底下每個子資料夾名都含空白。
    ("output\\bin sub\\helper.exe crashed", "helper.exe"),
    # 2026-09-11 相對分支接上結構分支之後才刷得掉的兩型。四筆的 banned 都是路徑
    # **後面**的段落：挑前面的段落會在壞掉的程式碼上就通過（舊行為是
    # `[path] (genshin impact)...`，前綴本來就已經不見了）。
    # A1：路徑還沒結束，中段後面跟著分隔符＝結構上證明它還在路徑裡。
    ("du -sh output/alice (genshin impact)/thumbs", "thumbs"),
    ("output\\alice (genshin impact)\\sub dir\\y", "sub dir"),
    ("templates\\my set\\cheat sheet", "cheat sheet"),
    # A2：結尾帶分隔符。
    ("scanning output/columbina (genshin impact)/ done", "genshin impact"),
]


@pytest.mark.parametrize("text,banned", _MUST_REDACT,
                         ids=[t[:34] for t, _ in _MUST_REDACT])
def test_a_host_path_never_survives_redaction(text, banned):
    out = BOT._redact_for_discord(text)
    assert banned not in out, (
        f"刷除之後還看得到 {banned!r}：{out!r}。"
        "這條字串會原樣送進頻道（`/log tail` 不是擁有者限定）。")
    assert "[path]" in out, f"整段都沒被認出來是路徑：{out!r}"


# 2026-09-24：只刷品牌字留下了三個洞，三個都在 `/log tail` 的預設 20 行裡量到過。
_MUST_REDACT_VOCAB = [
    # (真實形狀的輸入, 不得殘留的字串)
    ("[09-24 07:51:10]   [blocked] dialog text: 'The paint\u2019s run dry. You need a "
     "subscription or to purchase Anlas to continue. Tablet $10 /mo USD Opus $25 /mo USD'",
     "paint"),
    ("[09-24 07:51:10]   [blocked] dialog text: 'The paint\u2019s run dry. You need a "
     "subscription or to purchase Anlas", "subscription"),       # 被截斷、沒有收尾引號
    ("  [blocked] generation is blocked by a purchase/account dialog; matched "
     "'/(purchase|buy) (more )?(anlas|credits)/i'", "anlas"),
    ("session restored — redirected to https://novelai.net/stories", ".net"),
    ("timed out waiting for new image (last src=blob:https://novelai.net/0f1e)", ".net"),
    ("redirect chain: http://a.example/x -> https://novelai.net/stories", ".net"),
    ("supervisor: 已經有另一個批次監督者在執行（既有 pid: 27240），這次不啟動。", "27240"),
    ("reaped orphan chrome pid=31337 after restart", "31337"),
    ("terminate: chrome survivors pids=[101, 202]", "202"),
]


@pytest.mark.parametrize("text,banned", _MUST_REDACT_VOCAB,
                         ids=[t[:30] for t, _ in _MUST_REDACT_VOCAB])
def test_the_services_own_words_and_process_ids_never_survive_redaction(text, banned):
    out = BOT._redact_for_discord(text)
    assert banned.lower() not in out.lower(), (
        f"刷除之後還看得到 {banned!r}：{out!r}。`/log tail` 在檢視那一級，頻道裡誰都叫得到。")


@pytest.mark.parametrize("shape", [
    "a" * 60_000, "dialog text: '" + "x" * 60_000, "ab://" * 12_000,
    "a:" * 30_000, "x_config" * 7_500, "pid " * 15_000,
], ids=["letters", "open-quote", "scheme-like", "colons", "config-like", "pid-words"])
def test_redaction_stays_linear_on_hostile_input(shape):
    """刷除跑在事件迴圈上，送出去之前每一段 log／指令輸出都過它。一條會從每個起點各掃一次
    的正規式，對一段長的連續字就是平方時間——實測 2 萬字母 4 秒（舊的相對路徑規則）與 3 秒
    （品牌網址規則），6 萬就是半分鐘以上，整個 bot 卡在那裡。這裡給 5 秒：線性的版本在這些
    輸入上是幾十毫秒，平方的版本會超過一個數量級。"""
    import time as _time
    started = _time.perf_counter()
    BOT._redact_for_discord(shape)
    assert _time.perf_counter() - started < 5.0


@pytest.mark.parametrize("text, kept", [
    ("rapid 5 fails in a row; giving up", "rapid 5"),
    ("see https://www.selenium.dev/documentation for the driver", "https://www.selenium.dev"),
    ("[blocked] dialog innerText full length 870 chars (margin 330)", "870 chars"),
    ("pidgin is not a pid", "pidgin"),
    # 同一行裡既有品牌網址又有別家網址：只換掉前者；網址前面的字（`src=blob:`）照留。
    ("redirect http://a.example/x -> https://novelai.net/stories", "http://a.example/x"),
    ("(last src=blob:https://novelai.net/0f1e)", "src=blob:[svc url]"),
])
def test_the_vocabulary_redaction_leaves_ordinary_diagnostics_alone(text, kept):
    """反向邊界：pid 規則不得吃掉 `rapid 5`；別家的網址照留；只講長度、不含原文的那一行
    不需要刷。"""
    assert kept in BOT._redact_for_discord(text)


@pytest.mark.parametrize("text", [
    "https://novelai.net/03d1f67f-4576-42d5-baae-c3bbb1452f32",
    "see http://example.com/a/b and https://x.y/z.png for details",
    "ws://localhost:9222/devtools/browser/abc",
])
def test_a_url_is_not_mistaken_for_a_host_path(text):
    """`https://` 裡的 `s:/` 也符合「字母冒號斜線」。

    實測 `WEBRunner.log` 裡符合那個形狀的東西**全部**是網址，一個路徑都沒有——
    所以少了 `(?<![A-Za-z])` 這道前瞻，修好的刷除器會把每一個網址變成
    `[path]`，那比原本的漏洞更糟（診斷輸出整個報廢）。
    """
    out = BOT._redact_for_discord(text)
    assert "[path]" not in out, f"網址被當成主機路徑刷掉了：{out!r}"


@pytest.mark.parametrize("text", [
    "[08-24 16:04:21] [tangtang (arknights)] generating 1/120 -> "
    "tangtang (arknights)_0001_20260824_160421.png",
    "resume: `surtr (arknights)` 81/120 in surtr (arknights)/ "
    "(folder=81, checkpoint=81); continuing",
    "  [quota] recovered after 60 min; resuming `alice (genshin impact)`",
])
def test_ordinary_log_lines_are_not_shredded(text):
    """反向邊界：刷除是為了診斷輸出還能用而存在的，不是為了把整行變成 `[path]`。

    這三行都是 `WEBRunner.log` 裡真的出現過的，不含主機路徑。
    """
    out = BOT._redact_for_discord(text)
    assert "[path]" not in out, f"沒有路徑的一行被刷掉了：{out!r}"


def test_the_trailing_context_after_a_path_is_kept():
    """允許路徑含空白之後最容易踩的坑：貪婪吃掉後面的散文。

    刷除只吃到第一個已知副檔名為止，所以時間、次數這些診斷資訊要留著。
    """
    out = BOT._redact_for_discord(
        "saved output/columbina (genshin impact)/x_0087.png in 3.2s (attempt 1)")
    assert "in 3.2s (attempt 1)" in out, f"把路徑後面的診斷一起吃掉了：{out!r}"


def test_redaction_does_not_blow_up_on_a_long_line():
    """加了允許空白的分支之後要確認沒有災難性回溯——log 行可以很長。"""
    import time as _time
    line = "x " * 4000 + "D:\\a\\b.png"
    started = _time.perf_counter()
    BOT._redact_for_discord(line)
    elapsed = _time.perf_counter() - started
    assert elapsed < 1.0, f"刷除一行花了 {elapsed:.2f}s，正規表示式在回溯"


@pytest.mark.parametrize("text,keep", [
    # 中段容許空白之後最容易踩的坑：跨出路徑尾端、抓住散文裡的下一個斜線。
    # `1/120`、`81/120` 這種分數在這個專案的 log 裡到處都是——上面
    # `test_ordinary_log_lines_are_not_shredded` 的語料裡就有，只是那三行剛好
    # 不含 Windows 路徑，所以同一行放一條路徑才問得到這件事。
    (r"saved D:\Work\a\b.png 3/4 done", "3/4"),
    (r"config at D:\Work\x.json failed 81/120 retry", "81/120"),
    (r"D:\a\b.png and/or fallback", "and/or"),
    # 2026-09-11：弱錨點（`output[/\\]`）比強錨點更容易被中段的空白放大，所以中段
    # 不得以空白開頭或結尾。這兩半**各要一筆**才咬得到——第一版四筆輸入沒有一筆在
    # 分隔符**前面**帶空白，於是「允許結尾空白」那個變異存活了。
    ("writing output/ 3/4 done", "3/4"),                  # 不得以空白開頭
    ("copying output/note for me /var later", "/var"),    # 不得以空白結尾
    # 副檔名分支必須留在前面（也必須still存在）：反過來排會把 `3/4` 吃掉。
    ("saved output/a/b.png 3/4 done", "3/4"),
    # 放大器：錨點後面一個空白，然後兩千字元的散文。
    ("writing output/ " + "word/word " * 20 + "END", "END"),
    # 同一個收緊順手修掉的絕對路徑舊行為（原本刷成 `[path] done` / `saved [path] fallback`）。
    (r"D:\a\ 3/4 done", "3/4"),
    (r"saved D:\Work\out /tmp fallback", "/tmp"),
])
def test_prose_after_a_path_is_not_swallowed_by_a_later_slash(text, keep):
    """結構分支的負對照組：它**必須**排在副檔名分支後面。

    這幾筆在修好之前就是綠的——它們不是在證明缺陷存在，而是在擋下「把副檔名那條
    分支刪掉、只留結構分支比較乾淨」這種後續簡化。純結構版實測會把這三個都吃掉。
    """
    out = BOT._redact_for_discord(text)
    assert keep in out, f"把路徑後面的散文一起吃掉了：{out!r}"


@pytest.mark.parametrize("text,keep", [
    (r"failed to open C:\a\b.png, retrying in 3s", "[path],"),
    (r"error at D:\x\y.log: permission denied", "[path]:"),
    (r"(see C:\tmp\out.txt) for details", "[path])"),
    # 真實的錯誤訊息就是這樣帶引號的，收尾引號不見會讓輸出看起來像被截斷。
    # ⚠️ 這兩筆的路徑**刻意沒有副檔名**。帶副檔名的版本測不到這件事：
    # `exe` 在 `_REDACT_PATH_EXTS` 裡，於是 leftmost-first 讓副檔名分支先接掉，
    # 末段的字元類根本沒被用到——實測把 `'` 從末段拿掉，`…\x.exe'` 那種輸入
    # 輸出**完全不變**，變異存活。
    # ⚠️ 單引號那一筆也是**獨立的一筆**，不能只放雙引號：末段少了 `'` 時
    # 雙引號那筆照樣通過，只有單引號這筆會紅。
    (r"cannot find: 'C:\Program Files\Tesseract-OCR\tessdata'", "[path]'"),
    (r'cannot find: "C:\Program Files\Tesseract-OCR\tessdata"', '[path]"'),
])
def test_the_punctuation_that_ends_a_path_survives(text, keep):
    """路徑後面緊接的標點要留著，否則輸出讀起來像被截斷。"""
    out = BOT._redact_for_discord(text)
    assert keep in out, f"路徑後面的標點被吃掉了：{out!r}"


@pytest.mark.parametrize("text", [
    r"C:\a\x.txt and D:\b\y.txt",
    # ⚠️ 這一筆是承重的那一筆，而且**刻意沒有副檔名**：另外兩筆都以已知副
    # 檔名結尾，副檔名分支就接掉了，把中段的 `:` 排除拿掉它們照樣是綠的。實測連
    # `…\a\x.exe and D:\…\b\y.exe` 都測不到（`exe` 已在清單裡），只有兩邊都
    # 沒有副檔名、真的落到結構分支時，少了冒號才會把整行併成一個 `[path]`。
    r"C:\Program Files\a\sub and D:\Program Files\b\other",
    "copied C:/a/b.png to D:/c/d.png ok",
])
def test_two_paths_on_one_line_stay_two(text):
    """一行兩條路徑要刷成兩個 `[path]`，不能併成一個。

    中段的字元類必須排除 `:`——不排除的話，` and D:` 會被當成一個合法的中段吃
    進來（Windows 路徑除了磁碟機那個冒號之外不能含冒號）。併成一個不算洩漏，但
    把「這裡有兩條路徑」這個診斷資訊吃掉了。
    """
    out = BOT._redact_for_discord(text)
    assert out.count("[path]") == 2, f"兩條路徑沒有各自刷成一個：{out!r}"


# ---------------------------------------------------------------------------
# 「只給擁有者看的補充說明」不得漏到別人那裡
#
# `_api_contact_hint` 是刻意違反第 2 類檢查的字串：它提到 `bot_config.json`，而
# `*_config.json` 正是這支測試明文禁止送出去的形狀之一。之所以還是寫了它，是因為
# 擁有者例外（2026-08-27）——`/web wiki` 回一個裸的 `HTTP 403` 對擁有者毫無用處，
# 而可行動的那一行原本只進 stderr。
#
# 例外的代價是它現在依賴「呼叫端有沒有包 `_owner_detail`」這一件事。沒有包就是一條
# 完整的洩漏路徑，而且看起來一切正常（非擁有者也會收到一則很有幫助的訊息）。所以
# 這裡把那個依賴釘死：**在原始碼上**確認每一處 `_api_contact_hint` 都在
# `_owner_detail` 的引數裡面。
# ---------------------------------------------------------------------------

def test_the_contact_hint_only_ever_reaches_the_owner():
    tree = _bot_tree()
    owner_gated = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("_owner_detail", "_owner_error")):
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "_api_contact_hint"):
                    owner_gated.add(inner.lineno)
    all_uses = {
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_api_contact_hint"
    }
    assert all_uses, "找不到任何 `_api_contact_hint` 呼叫——這支測試已經失效"
    leaked = sorted(all_uses - owner_gated)
    assert not leaked, (
        f"第 {leaked} 行的 `_api_contact_hint` 沒有包在 `_owner_detail` 裡。"
        "那段文字提到設定檔名，對非擁有者是 Layer 1 禁止送出的內容。")


def test_the_contact_hint_stays_quiet_unless_it_is_that_problem(monkeypatch):
    """只有「403／429 ＋ 聯絡方式沒設定」才講話，其餘一律空字串。

    亂講的補充比不講更糟：一個 500 配上「去設 `api_contact`」會把查的人帶去
    完全錯誤的方向——今天早上那個把 529 過載報成「認證有問題」的診斷就是這樣。
    """
    import _external_apis as EX

    monkeypatch.setattr(EX, "contact_configured", lambda: False)
    assert BOT._api_contact_hint(403), "403 ＋ 未設定時應該要有補充"
    assert BOT._api_contact_hint(429), "429（節流）也是同一個原因"
    for other in (200, 404, 500, 502, 529, -1):
        assert BOT._api_contact_hint(other) == "", other

    monkeypatch.setattr(EX, "contact_configured", lambda: True)
    for status in (403, 429):
        assert BOT._api_contact_hint(status) == "", (
            "聯絡方式已經設定了，403 就是別的原因——這時候還講同一句只會誤導")


def test_the_generic_half_of_the_wiki_failure_says_nothing_internal():
    """非擁有者拿到的那一半必須乾淨。

    `_owner_detail` 的第三個引數就是那一半，所以直接把它挑出來檢查，而不是相信
    「呼叫端應該有做對」。
    """
    tree = _bot_tree()
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "mcmd_wiki")
    generics = [
        node.args[2] for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_owner_detail" and len(node.args) >= 3
    ]
    assert generics, "`mcmd_wiki` 沒有走 `_owner_detail`"
    for arg in generics:
        rendered = ast.unparse(arg)
        for banned in ("bot_config", "api_contact", "_config.json", "User-Agent"):
            assert banned not in rendered, (
                f"非擁有者那一半帶了 {banned!r}：{rendered}")


# ---------------------------------------------------------------------------
# 送出去的字串不得教使用者打 `!` 指令
# ---------------------------------------------------------------------------
# `CLAUDE.md` DoD #3 講了兩件事：使用者文件不得再教 `!cmd`，**而且**「The same
# ban applies to strings the bot sends: never tell a user to type `!cmd`」。
# 前半有守門——`test_docs_sync.test_hidden_surfaces_are_not_advertised` 掃五份
# 使用者語料。**後半在 2026-09-21 之前沒有任何東西在看**，而樹上剛好有四筆真的
# 違規（兩個送出點）：
#
#   * `_gui_control.py` 在巨集被改空時回「要整個刪掉請用 `!macro delete`」。那句
#     話會**原樣**送到聊天平台——`GuiError` 屬於 `_SAFE_EXCEPTION_TYPES`，送出點
#     依規則可以直接內插它，所以「這句話是我們自己寫的、而且合規」這個承諾是在
#     `raise` 那一行做的。
#   * `cmd_config` 的 embed footer 一口氣教了 `!config_set`、`!stop`、`!run`
#     三個，而它掛在**對外宣傳的** `/config` 上，任何使用者跑一次就看得到。
#     更說明問題的是：同一個檔案往下十二行的 `cmd_config_set` 寫的是
#     `/config set <key> <value>`——正確的講法就在旁邊，兩邊還是漂開了。
#
# 判準刻意窄，避免叫狼來了：`!` 後面接的那個字必須**真的是** `on_message` 派發鏈
# 裡的指令名（正式名或別名），所以「太好了!」「rc!=0」「!nonsense」都不會命中。
# 指令清單從 `test_docs_sync` 匯入而不是抄一份：指令改名時這道守門要跟著改，
# 抄一份的話它會變成一個永遠對不上任何東西的字串，而測試照樣全綠。
#
# 不設豁免清單是刻意的。規則沒有例外（`@bot <文字>` 那個唯一例外是 `@bot` 那一
# 條，不是 `!`），而豁免清單一開就會越積越多——這正是 DoD #3 存在的理由。

_BANG_IN_TEXT_RE = re.compile(r"!([A-Za-z0-9_]+)")


def _literal_texts(node: ast.AST):
    """節點底下所有**字面**文字，含 f-string 的固定片段。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub, sub.value


def _bot_authored_strings() -> list[tuple[str, int, str]]:
    """`(模組, 行號, 文字)`——bot 會原樣送出去的字串字面值。

    兩個來源缺一不可：`discord_bot` 的送出呼叫，以及 `GuiError`／`GuiAborted`
    的訊息。後者不在送出點上看得到（送出的是 `except` 抓到的變數），所以只掃
    送出點會整批漏掉——那正是 2026-09-20 修 raise site 掃描時記下的同一個教訓。
    """
    found: list[tuple[str, int, str]] = []
    # 送出點這一半跟著「先存進變數再送出」走一步，而且**不剪**擁有者出口——
    # 教 `!` 的規則對擁有者也成立。見 `SENT_LITERALS_UNPRUNED` 上面那段。
    for _call_line, lineno, text, _via_name in SENT_LITERALS_UNPRUNED:
        found.append(("discord_bot.py", lineno, text))
    for name, tree in _raiser_trees():
        for raise_node in _family_raises(tree):
            for node, text in _literal_texts(raise_node):
                found.append((name, node.lineno, text))
    return found


BOT_AUTHORED_STRINGS = _bot_authored_strings()


def _bang_command_names() -> set[str]:
    return {token.lstrip("!")
            for token in set(BANG_PRIMARY) | set(BANG_ALIASES)}


def _bang_teachings(rows: list[tuple[str, int, str]]) -> list[str]:
    names = _bang_command_names()
    problems = []
    for module, lineno, text in rows:
        for hit in sorted(set(_BANG_IN_TEXT_RE.findall(text))):
            if hit in names:
                problems.append(
                    f"{module}:{lineno} 教了 `!{hit}`：{text.strip()[:70]}")
    return sorted(set(problems))


def test_the_sent_string_corpus_is_not_empty():
    """正向對照：語料空掉的話下面那支會無聲通過。

    現值：1,734 個字串、155 個指令名。門檻拉低是為了容忍正常增減，但「送出寫法
    整批改掉、掃描器只剩一半範圍」仍然當場現形。
    """
    assert len(BOT_AUTHORED_STRINGS) >= 1200, (
        f"只收到 {len(BOT_AUTHORED_STRINGS)} 個送出字串，掃描範圍可能縮了。")
    assert len(_bang_command_names()) >= 50, (
        f"只推導出 {len(_bang_command_names())} 個 `!` 指令名，"
        "`test_docs_sync._bang_commands` 可能抽不到東西了。")


def test_no_bot_string_teaches_a_hidden_bang_command():
    """bot 送出去的字串不得叫使用者去打 `!` 指令。

    斜線指令是唯一對外介面；`!` 是給手機打字／多行貼上／回覆脈絡留的隱藏相容
    路徑。一句「請用 `!macro delete`」等於把它重新公開，而使用者照著學會之後
    就會一直用——這是文件那一側早就守住、送出字串這一側卻一直沒人看的半邊。
    """
    problems = _bang_teachings(BOT_AUTHORED_STRINGS)
    assert not problems, (
        "這些送出去的字串在教隱藏的 `!` 指令，改成對應的斜線指令：\n  "
        + "\n  ".join(problems))


@pytest.mark.parametrize("text,caught", [
    ("要整個刪掉請用 `!macro delete`。", True),
    ("!config_set <key> <value> to change", True),
    ("太好了!", False),
    ("rc!=0 代表失敗", False),
    ("!definitelynotacommand 不是指令", False),
    ("請用 `/macro delete`。", False),
])
def test_the_bang_detector_tells_the_shapes_apart(text, caught):
    """對照組：真的指令名要抓到，普通驚嘆號與不存在的指令不能誤報。

    沒有這一格，把述詞改成「永遠回空清單」上面那支照樣綠——而一個永遠不叫的
    守門跟一棵乾淨的樹長得一模一樣。
    """
    hits = _bang_teachings([("probe.py", 1, text)])
    assert bool(hits) is caught, f"{text!r} 判成 {hits}"
