"""三個指令表面的**主機控制閘**——行為測試，不是靜態掃描。

`CLAUDE.md` 把主機控制列為硬規則：`_OWNER_ONLY_GROUPS`（`input`／`screen`／
`win`／`clip`／`locate`／`macro`／`watch`／`proc`／`host`）涵蓋那些群組**現在與
未來**的每一個子指令，因為「能在設定的頻道發言」不該等於「能敲這台機器的鍵盤、
截桌面、讀剪貼簿、終止行程」。

在這個檔案出現以前，三個表面的守門強度是**不平均**的：

| 表面 | 入口 | 原本有的東西 |
|---|---|---|
| 斜線 | `tree.interaction_check` → `_tree_check` | `test_bot_helpers.py` 的 `_run_gate` 真的跑過閘（8 支） |
| `!` | `on_message` | **只有 AST**：別名有沒有列進 `_OWNER_ONLY_BANGS` |
| `@bot` | `_handle_mention` | **只有 AST**：`_OWNER_ONLY_MENTIONS` 有沒有過期項目 |

而**歷史上真的破過的那個表面，正是完全沒有行為測試的那個**：`_handle_mention`
跑在 `on_message` 的頻道閘**之前**、原本完全沒有閘，`mcmd_restart` 內部也沒有
擁有者檢查，所以 `@bot restart` 一度是「bot 看得到的任何伺服器、任何人」都叫得
動的。AST 守門看得到「名字有沒有列在集合裡」，看不到「那個集合有沒有真的被查」
——本專案已經踩過一次 `if False and not decided:`：名字還在、AST 照樣過、行為卻
沒了。

所以這裡做三件事：

1. **`!` 表面**：把派發鏈裡**每一個**被鎖的 head（含 `!config_set` / `!cfg_set`
   這種只存在於派發器 tuple、宣告上看不到的別名）真的送進 `on_message`，確認被
   擋、而且**那個動作沒有真的執行**。
2. **`@bot` 表面**：同上，另外證明那條路**真的不受頻道閘管**——這正是「少了擁有
   者閘就是完整繞道」的前提，也是把它跟 `!` 表面分開測的理由。
3. **`_tree_check` 沒被執行過的分支**：實測 34 個陳述式裡有 11 個從未執行，包含
   **整段角色閘**、`command is None` 早退、以及三個 `send_message` 失敗的吞例外
   分支。順序性質（頻道拒絕不計數不稽核、角色拒絕會回話但仍不計數）也一併釘住。

**測試不連對話平台、不送任何訊息、不起子行程、不終止任何行程。**

⚠️ **安全機制是「先把動作換掉」，不是斷言。** 被鎖的 45 個 `!` head 裡有
`!kill`／`!sh`／`!restart`／`!panic` 這種真的會動到這台機器的東西，而閘一旦被改
壞（正是本檔要偵測的情況）派發器就會呼叫到真正的 handler。所以每一支測試都在呼
叫 `on_message` **之前**就把那些 handler 全部換成只記錄不做事的替身；斷言只是偵
測器。替身刻意**不丟例外**：`on_message` 有一個包山包海的 `except` 會把它吞成一
句「內部錯誤」，而 `_handle_mention` 那條路沒有外層保護、會直接往外拋——同一個替
身在兩個表面上的行為會不一致，所以偵測器選記錄器而不是例外。
"""
import ast
import asyncio
import os
import sys
import types
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord  # noqa: E402
import discord_bot as b  # noqa: E402


HERE = Path(__file__).resolve().parent.parent / "axiomatic"
BOT_SOURCE = HERE / "discord_bot.py"

# 這個 uid 不是擁有者、也不在任何角色清單裡——三個表面上最沒有特權的身分。
STRANGER = 999_000_111
# 隨便一個「不是設定的那個」頻道。
OTHER_CHANNEL = 111_222_333
# bot 自己的帳號 id（假的），用來組 `<@id>` 前綴與判斷 mention。
BOT_UID = 777_000_777


# ---------------------------------------------------------------------------
# 從派發器抽出真正的路由表（不手寫清單——手寫的清單會過期）
# ---------------------------------------------------------------------------
def _bot_tree() -> ast.Module:
    return ast.parse(BOT_SOURCE.read_text(encoding="utf-8"), str(BOT_SOURCE))


def _named_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"`discord_bot.py` 裡找不到 `{name}`——派發器被改名了？")


def _heads_of(test) -> list[str]:
    """`head == "!x"` / `head in ("!x", "!y")` → `["!x", "!y"]`。

    別名只存在於這種 tuple 裡，宣告端（`extras={"bang": …}`）看不到；漏掉一個
    別名就是一條完整的繞道，所以路由表一定要從這裡抽。
    """
    if not (isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "head"
            and test.comparators):
        return []
    target = test.comparators[0]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [e.value for e in target.elts if isinstance(e, ast.Constant)]
    if isinstance(target, ast.Constant):
        return [target.value]
    return []


def _bang_routes() -> dict[str, list[str]]:
    """-> {`!head`: [該分支會呼叫的模組層函式名]}。"""
    dispatcher = _named_function(_bot_tree(), "on_message")
    routes: dict[str, set[str]] = {}
    for node in ast.walk(dispatcher):
        if not isinstance(node, ast.If):
            continue
        heads = _heads_of(node.test)
        if not heads:
            continue
        # 只走 `node.body`（then 分支）；elif 掛在 `orelse`，不會被算進來。
        called = {c.func.id
                  for stmt in node.body
                  for c in ast.walk(stmt)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        for head in heads:
            routes.setdefault(head, set()).update(called)
    return {head: sorted(names) for head, names in routes.items()}


def _mention_routes() -> dict[str, list[str]]:
    """-> {mention 子指令: [handler 函式名]}，抽自 `handlers` 字典。"""
    handler_fn = _named_function(_bot_tree(), "_handle_mention")
    routes: dict[str, list[str]] = {}
    for node in ast.walk(handler_fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant)
                    and isinstance(key.value, str)):
                continue
            names = sorted({c.func.id for c in ast.walk(value)
                            if isinstance(c, ast.Call)
                            and isinstance(c.func, ast.Name)})
            if names:
                routes[key.value] = names
    return routes


BANG_ROUTES = _bang_routes()
MENTION_ROUTES = _mention_routes()
LOCKED_BANGS = sorted(h for h in BANG_ROUTES if h in b._OWNER_ONLY_BANGS)
LOCKED_MENTIONS = sorted(k for k in MENTION_ROUTES
                         if k in b._OWNER_ONLY_MENTIONS)

# 「反面」對照組：這些**不該**被擋，否則「一律擋」也會全綠。
UNLOCKED_BANG = "!queue"
UNLOCKED_MENTION = "ping"

# 每一支測試開跑前都要換掉的 handler：被鎖的那些（安全用）＋對照組（決定性用）。
_HANDLERS_TO_DISARM = sorted(
    {name for head in LOCKED_BANGS for name in BANG_ROUTES[head]}
    | {name for key in LOCKED_MENTIONS for name in MENTION_ROUTES[key]}
    | set(BANG_ROUTES.get(UNLOCKED_BANG, []))
    | set(MENTION_ROUTES.get(UNLOCKED_MENTION, []))
    | {"mcmd_dorossi"}
)


# ---------------------------------------------------------------------------
# 假的訊息／頻道／client
# ---------------------------------------------------------------------------
class _FakeChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.sent: list = []

    async def send(self, content=None, **kwargs):
        # 走到這裡代表 `safe_reply` 的「原訊息被刪」退路被觸發了。閘門測試裡不
        # 該發生，但吞下來比讓例外冒出去更能讓失敗訊息指向真正的原因。
        self.sent.append(content)
        return types.SimpleNamespace(id=1)

    def typing(self):
        raise AssertionError("閘門測試不該走到 typing()——代表閘沒有擋住")


class _FakeMessage:
    """`on_message` / `_handle_mention` 真正碰得到的那幾個屬性。"""

    def __init__(self, content: str, uid: int, channel_id: int,
                 mentions=()):
        self.content = content
        self.author = types.SimpleNamespace(id=uid)
        self.channel = _FakeChannel(channel_id)
        self.mentions = list(mentions)
        self.guild = None
        self.id = 424_242
        self.attachments: list = []
        self.replies: list = []

    async def reply(self, content=None, **kwargs):
        self.replies.append(content)
        return types.SimpleNamespace(id=1)


def _bang(content: str, uid: int, channel_id: int) -> _FakeMessage:
    return _FakeMessage(content, uid, channel_id)


def _mention(text: str, uid: int, channel_id: int) -> _FakeMessage:
    """組一則真的帶著 `<@id>` 的訊息，讓 `on_message` 自己判定它是 mention。

    刻意**不直接呼叫** `_handle_mention`：這個表面最關鍵的性質是「它在頻道閘
    之前就被派發出去」，而那個順序只有從 `on_message` 進去才看得到。
    """
    bot_user = types.SimpleNamespace(id=BOT_UID)
    return _FakeMessage(f"<@{BOT_UID}> {text}".strip(), uid, channel_id,
                        mentions=[bot_user])


def _deliver(message: _FakeMessage) -> None:
    asyncio.run(b.on_message(message))


# ---------------------------------------------------------------------------
# 夾具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def armed(monkeypatch, tmp_path):
    """把三個表面**先解除武裝**，再把會落地的狀態導到 tmp。

    回傳一個 `touched` 清單：任何被鎖的 handler 只要被呼叫到，名字就會進去。
    閘沒壞的話它永遠是空的。
    """
    touched: list[str] = []

    for name in _HANDLERS_TO_DISARM:
        assert hasattr(b, name), (
            f"派發器指向 `{name}`，但 `discord_bot` 裡沒有這個名字——"
            "路由抽取失準的話，下面每一筆都會變成空轉通過。")

        def _record(*_args, _name=name, **_kwargs):
            async def _noop():
                touched.append(_name)
            return _noop()

        monkeypatch.setattr(b, name, _record)

    # 稽核紀錄會**寫進 repo root 的 `audit.ndjson`**，也就是使用者的真實資料。
    # 2026-08-23 量過一次：5,287 筆裡 5,281 筆是測試留下的假紀錄，`/sys audit`
    # 因此形同失效。導到 tmp。
    monkeypatch.setattr(b, "AUDIT_FILE", tmp_path / "audit.ndjson")
    # 計數器換成新的一份，避免污染正在執行的行程的 `/sys metrics`，順便讓
    # 「有沒有計數」的斷言可以直接看整份而不必算差值。
    monkeypatch.setattr(b, "_METRICS_CMD_COUNTS", Counter())
    monkeypatch.setattr(b, "_METRICS_ERRORS", 0)
    # `on_message` 用 `client.user` 判斷「這則訊息是不是在叫我」。真正的 client
    # 沒連線時 `user` 是 None，那樣就永遠走不到 mention 那條路。
    monkeypatch.setattr(b, "client",
                        types.SimpleNamespace(
                            user=types.SimpleNamespace(id=BOT_UID)))
    # 角色系統預設是停用的（三份清單都空）。每一支測試都從那個狀態開始，
    # 角色閘要另外自己打開。
    monkeypatch.setattr(b, "USER_ROLES", {})
    return types.SimpleNamespace(touched=touched,
                                 audit=tmp_path / "audit.ndjson")


def _audit_lines(armed) -> list[str]:
    if not armed.audit.exists():
        return []
    return [line for line in
            armed.audit.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# 抽取本身要站得住腳——空的路由表會讓底下每一筆都空轉通過
# ---------------------------------------------------------------------------
def test_the_dispatch_maps_were_actually_extracted():
    assert len(BANG_ROUTES) >= 100, len(BANG_ROUTES)
    assert len(MENTION_ROUTES) >= 15, len(MENTION_ROUTES)
    # 集合裡列的每一個 head 都要真的在派發鏈上；對不上就是拼錯字的空閘。
    missing = sorted(set(b._OWNER_ONLY_BANGS) - set(BANG_ROUTES))
    assert not missing, f"`_OWNER_ONLY_BANGS` 列了派發器不認得的 head：{missing}"
    assert len(LOCKED_BANGS) >= 40, LOCKED_BANGS
    assert sorted(b._OWNER_ONLY_MENTIONS) == LOCKED_MENTIONS
    # 別名確實被抽到了——這是 AST 守門唯一守著、卻從沒被實際執行過的東西。
    for alias_pair in (("!config_set", "!cfg_set"),
                       ("!config_reset", "!cfg_reset")):
        for alias in alias_pair:
            assert alias in LOCKED_BANGS, alias
        assert BANG_ROUTES[alias_pair[0]] == BANG_ROUTES[alias_pair[1]], (
            f"{alias_pair} 應該指向同一個 handler，否則這一對不是別名")
    assert UNLOCKED_BANG in BANG_ROUTES
    assert UNLOCKED_MENTION in MENTION_ROUTES


# ---------------------------------------------------------------------------
# A. `!` 表面（`on_message`）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("head", LOCKED_BANGS)
def test_a_stranger_cannot_run_any_locked_bang_command(head, armed):
    """**每一個**被鎖的 `!` head，含派發器裡的別名，都要真的被擋下來。

    刻意用**設定的那個頻道** ＋ 沒有設定任何角色——預設狀態下最寬鬆的情況。
    這一筆證明的是：擋住它的是擁有者閘，不是頻道閘、也不是角色閘（角色閘在
    `_roles_configured()` 為 False 時對所有人回 True，等於不存在）。
    """
    message = _bang(f"{head} whatever", STRANGER, b.CHANNEL_ID)
    _deliver(message)
    assert armed.touched == [], (
        f"`{head}` 真的被執行了——擁有者閘沒擋住。這是「能發言 ＝ 能敲鍵盤」，"
        "也是 `CLAUDE.md` 主機控制那條硬規則要防的事。")
    assert message.replies == [b.OWNER_ONLY_DENIED], message.replies


@pytest.mark.parametrize("head", LOCKED_BANGS)
def test_the_owner_still_reaches_every_locked_bang_handler(head, armed):
    """反面：閘擋的是「非擁有者」，不是「這個指令」。

    少了這一筆，把閘改成「一律拒絕」也會全綠——而那會讓擁有者連自己的機器都
    控制不了。
    """
    message = _bang(f"{head} whatever", b.OWNER_USER_ID, b.CHANNEL_ID)
    _deliver(message)
    assert armed.touched == BANG_ROUTES[head], (
        f"擁有者跑 `{head}` 應該走到 {BANG_ROUTES[head]}，"
        f"實際走到 {armed.touched}")
    assert message.replies == []


def test_a_refused_bang_is_neither_counted_nor_audited(armed):
    """順序是規則的一部分：擁有者閘排在計數（17062）與稽核（17064）之前。

    把閘往下挪到那兩行後面，指令仍然會被擋——但每一次被拒的嘗試都會灌進
    `/sys metrics` 的指令排行，而稽核檔會被不曾發生的動作填滿。只斷言回傳值
    的測試對這種改動是全綠的。
    """
    _deliver(_bang("!kill 1234", STRANGER, b.CHANNEL_ID))
    assert b._METRICS_CMD_COUNTS == Counter(), b._METRICS_CMD_COUNTS
    assert _audit_lines(armed) == []


def test_an_unlocked_bang_still_runs_for_a_stranger(armed):
    """鎖定範圍不得蔓延：唯讀的佇列查詢仍然要給其他人用。"""
    message = _bang(UNLOCKED_BANG, STRANGER, b.CHANNEL_ID)
    _deliver(message)
    assert armed.touched == BANG_ROUTES[UNLOCKED_BANG]
    assert message.replies == []
    # 有跑到就要留痕——這是被拒那條路**沒有**的東西，兩邊互為對照。
    assert b._METRICS_CMD_COUNTS[UNLOCKED_BANG] == 1
    assert len(_audit_lines(armed)) == 1


def test_a_stranger_outside_the_configured_channel_is_dropped_in_silence(armed):
    """頻道閘是**靜默**的：不回話、不計數、不稽核。

    跟擁有者閘的拒絕（會回一句話）刻意不同，所以兩者是分得出來的——這正是
    mention 那條路的順序證明所依賴的差異。
    """
    message = _bang(UNLOCKED_BANG, STRANGER, OTHER_CHANNEL)
    _deliver(message)
    assert armed.touched == []
    assert message.replies == [], "頻道閘應該安靜地丟掉，不該回話"
    assert message.channel.sent == []
    assert b._METRICS_CMD_COUNTS == Counter()
    assert _audit_lines(armed) == []


def test_the_owner_keeps_the_cross_channel_bypass_on_the_bang_surface(armed):
    """擁有者可以從任何頻道下 `!` 指令——與斜線那一側同一條例外。"""
    message = _bang(UNLOCKED_BANG, b.OWNER_USER_ID, OTHER_CHANNEL)
    _deliver(message)
    assert armed.touched == BANG_ROUTES[UNLOCKED_BANG]


# ---------------------------------------------------------------------------
# B. `@bot` 表面（`_handle_mention`）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", LOCKED_MENTIONS)
def test_a_stranger_cannot_run_a_locked_mention_command(key, armed):
    message = _mention(key, STRANGER, b.CHANNEL_ID)
    _deliver(message)
    assert armed.touched == [], (
        f"`@bot {key}` 真的被執行了——mention 那條路的擁有者閘沒擋住。")
    assert message.replies == [b.OWNER_ONLY_DENIED], message.replies


@pytest.mark.parametrize("key", LOCKED_MENTIONS)
def test_the_mention_gate_fires_before_the_channel_gate(key, armed):
    """**順序本身就是歷史缺陷所在。**

    `on_message` 先把 mention 交給 `_handle_mention` 然後 `return`，整段在頻道
    閘**之前**——所以 mention 在任何頻道都會被派發出去。證明方式是那兩道閘的
    症狀不一樣：頻道閘靜默丟掉（見上面那一筆），擁有者閘會回一句話。在**不是**
    設定的那個頻道收到那句話，就代表擋住它的一定是擁有者閘。
    """
    message = _mention(key, STRANGER, OTHER_CHANNEL)
    _deliver(message)
    assert armed.touched == []
    assert message.replies == [b.OWNER_ONLY_DENIED], (
        f"`@bot {key}` 在非設定頻道既沒被擋也沒回話——那代表這條路只剩下"
        "「碰巧沒人叫得到」在保護，而它其實在任何伺服器都叫得到。")


def test_the_mention_surface_really_is_not_channel_gated(armed):
    """上一筆的前提：mention 真的不受頻道閘管。

    少了這一筆，`test_the_mention_gate_fires_before_the_channel_gate` 就可能只是
    在測「頻道閘擋住了它」——那樣把擁有者閘整個拿掉也會是綠的（訊息會被靜默丟
    掉，`touched` 一樣是空的），完全反過來。
    """
    message = _mention(UNLOCKED_MENTION, STRANGER, OTHER_CHANNEL)
    _deliver(message)
    assert armed.touched == MENTION_ROUTES[UNLOCKED_MENTION], (
        "非設定頻道的 `@bot ping` 應該照樣執行——mention 刻意不限頻道。"
        "如果這裡開始被擋，上面那筆順序測試就失去了意義，要一起重想。")


@pytest.mark.parametrize("key", LOCKED_MENTIONS)
def test_the_owner_still_reaches_every_locked_mention_handler(key, armed):
    message = _mention(key, b.OWNER_USER_ID, OTHER_CHANNEL)
    _deliver(message)
    assert armed.touched == MENTION_ROUTES[key], armed.touched


def test_a_refused_mention_is_not_counted(armed):
    """mention 面的擁有者閘同樣排在計數之前。"""
    _deliver(_mention("restart", STRANGER, b.CHANNEL_ID))
    assert b._METRICS_CMD_COUNTS == Counter(), b._METRICS_CMD_COUNTS


def test_free_text_mentions_still_reach_the_question_entry_point(armed):
    """`@bot <文字>` 是公開入口（權限閘在 handler 內），不得被這道閘掃到。"""
    message = _mention("今天天氣如何", STRANGER, OTHER_CHANNEL)
    _deliver(message)
    assert armed.touched == ["mcmd_dorossi"], armed.touched
    assert message.replies == []
    assert b._METRICS_CMD_COUNTS["@dorossi"] == 1


# ---------------------------------------------------------------------------
# C. `_tree_check` 從未被執行過的分支
# ---------------------------------------------------------------------------
# 實測（既有測試全跑一遍）：34 個陳述式裡 11 個從未執行。扣掉 docstring，剩下的
# 是 `command is None` 早退、**整段角色閘**、以及三個 `send_message` 失敗時的吞
# 例外分支。下面把它們一一跑起來。
class _FakeResponse:
    def __init__(self, boom: bool = False):
        self.sent: list = []
        self._boom = boom

    async def send_message(self, content, **kwargs):
        if self._boom:
            # 真實世界的成因：這個 interaction 已經被回應過／已經逾時。
            raise RuntimeError("interaction has already been acknowledged")
        self.sent.append((content, kwargs))


class _FakeInteraction:
    def __init__(self, command, uid, channel_id, itype=None, boom=False):
        self.command = command
        self.user = types.SimpleNamespace(id=uid)
        self.channel = types.SimpleNamespace(id=channel_id)
        self.channel_id = channel_id
        self.guild = None
        self.id = 12_345
        self.type = itype or discord.InteractionType.application_command
        self.response = _FakeResponse(boom)
        self.extras: dict = {}
        self.namespace = types.SimpleNamespace()


def _command(qualified: str, extras: dict):
    return types.SimpleNamespace(qualified_name=qualified, extras=extras)


def _gate(command, uid, channel_id, *, boom=False):
    interaction = _FakeInteraction(command, uid, channel_id, boom=boom)
    return asyncio.run(b._tree_check(interaction)), interaction


# `!todo_prompt_add` 不在 `_VIEWER_COMMANDS` 也不在 `_ADMIN_COMMANDS` → 落到
# `operator`；`!queue` 在 viewer 表裡。兩者都**不是**主機控制指令，所以擁有者閘
# 不會先攔截，角色閘才跑得到。
_OPERATOR_CMD = _command("todo prompt add", {"bang": "!todo_prompt_add"})
_VIEWER_CMD = _command("queue", {"bang": "!queue"})
VIEWER_UID = 123_456


def test_an_interaction_without_a_command_is_allowed_through(armed):
    """`interaction.command` 是 None 時直接放行。

    這條路在 discord.py 找不到對應指令時會走到；把它改成 `return False` 不會有
    任何現有測試變紅，但每一次都會多送一則拒絕訊息。
    """
    allowed, interaction = _gate(None, STRANGER, OTHER_CHANNEL)
    assert allowed is True
    assert interaction.response.sent == []
    assert b._METRICS_CMD_COUNTS == Counter()


def test_the_role_gate_refuses_a_user_below_the_required_tier(armed,
                                                              monkeypatch):
    """整段角色閘在此之前**一行都沒跑過**——因為預設三份角色清單都空著。

    那不代表它是死碼：擁有者一旦真的設定角色，它就是唯一在管一般指令的東西。
    """
    monkeypatch.setattr(b, "USER_ROLES", {"viewer_user_ids": [VIEWER_UID]})
    assert b._roles_configured() is True
    allowed, interaction = _gate(_OPERATOR_CMD, VIEWER_UID, b.CHANNEL_ID)
    assert allowed is False
    assert len(interaction.response.sent) == 1
    content, kwargs = interaction.response.sent[0]
    assert "operator" in content and "viewer" in content
    assert kwargs.get("ephemeral") is True


def test_a_role_refusal_replies_but_is_still_not_counted(armed, monkeypatch):
    """順序：角色閘（17543）在計數（17555）與稽核（17556）之前。

    跟頻道拒絕的差別只有「會不會回話」，兩者都不留痕。
    """
    monkeypatch.setattr(b, "USER_ROLES", {"viewer_user_ids": [VIEWER_UID]})
    _gate(_OPERATOR_CMD, VIEWER_UID, b.CHANNEL_ID)
    assert b._METRICS_CMD_COUNTS == Counter(), b._METRICS_CMD_COUNTS
    assert _audit_lines(armed) == []


def test_the_role_gate_lets_a_sufficient_tier_through(armed, monkeypatch):
    """反面：角色閘不是「設定了角色就全擋」。"""
    monkeypatch.setattr(b, "USER_ROLES", {"viewer_user_ids": [VIEWER_UID]})
    allowed, interaction = _gate(_VIEWER_CMD, VIEWER_UID, b.CHANNEL_ID)
    assert allowed is True
    assert interaction.response.sent == []
    assert b._METRICS_CMD_COUNTS["/queue"] == 1
    assert len(_audit_lines(armed)) == 1


def test_the_owner_still_reaches_a_locked_slash_command(armed):
    """反面，斜線側。**這一筆是變異測試逼出來的**：把擁有者閘的
    `and interaction.user.id != OWNER_USER_ID` 拿掉（＝對所有人一律拒絕，含擁
    有者），本檔原本 118 筆全綠——三個表面裡只有斜線這一側缺了「擁有者走得
    通」的對照，於是「閘壞成全擋」在這裡是看不見的。

    擁有者被自己的主機控制指令擋在門外，症狀是「`/proc kill` 在任何地方都說我
    不是擁有者」，而所有拒絕類的測試都會照樣是綠的。
    """
    allowed, interaction = _gate(_command("proc kill", {"bang": "!kill"}),
                                 b.OWNER_USER_ID, b.CHANNEL_ID)
    assert allowed is True, "擁有者被自己的主機控制指令擋住了"
    assert interaction.response.sent == []
    # 有跑到就要留痕——擁有者不是例外。
    assert b._METRICS_CMD_COUNTS["/proc.kill"] == 1
    assert len(_audit_lines(armed)) == 1


def test_a_channel_refusal_is_neither_counted_nor_audited(armed):
    allowed, _ = _gate(_VIEWER_CMD, STRANGER, OTHER_CHANNEL)
    assert allowed is False
    assert b._METRICS_CMD_COUNTS == Counter()
    assert _audit_lines(armed) == []


def test_an_owner_only_refusal_is_neither_counted_nor_audited(armed):
    allowed, _ = _gate(_command("proc kill", {"bang": "!kill"}),
                       STRANGER, b.CHANNEL_ID)
    assert allowed is False
    assert b._METRICS_CMD_COUNTS == Counter()
    assert _audit_lines(armed) == []


@pytest.mark.parametrize("label,command,uid,channel_id,roles", [
    ("channel", _VIEWER_CMD, STRANGER, OTHER_CHANNEL, {}),
    ("owner-only", _command("proc kill", {"bang": "!kill"}),
     STRANGER, None, {}),
    ("role", _OPERATOR_CMD, VIEWER_UID, None,
     {"viewer_user_ids": [VIEWER_UID]}),
])
def test_a_refusal_still_refuses_when_the_reply_itself_fails(
        label, command, uid, channel_id, roles, armed, monkeypatch):
    """三個 `except Exception: pass` 分支——全部從未執行過。

    重點不是「例外有沒有被吞掉」，是**吞掉之後還是要回 False**。把 `return
    False` 誤縮排進 `try`（或改成在 `except` 裡 `raise`），使用者不會收到拒絕
    訊息、指令卻照樣執行——一個在回覆失敗時自己打開的閘。
    """
    if roles:
        monkeypatch.setattr(b, "USER_ROLES", roles)
    allowed, _ = _gate(command, uid,
                       b.CHANNEL_ID if channel_id is None else channel_id,
                       boom=True)
    assert allowed is False, f"{label}：回覆失敗時閘門自己打開了"
    assert b._METRICS_CMD_COUNTS == Counter()
    assert _audit_lines(armed) == []


# ---------------------------------------------------------------------------
# D. 拒絕訊息本身要能在任何伺服器顯示（保密規則 Layer 1）
# ---------------------------------------------------------------------------
def test_every_refusal_string_is_safe_to_show_anywhere(armed, monkeypatch):
    """收集三個表面**實際送出**的拒絕字串，逐一過濾。

    靜態讀常數是不夠的：角色閘那句是 f-string，內容由執行期的角色名稱組出來。
    """
    strings: list[str] = []

    # 斜線：頻道閘、擁有者閘、角色閘。
    _, interaction = _gate(_VIEWER_CMD, STRANGER, OTHER_CHANNEL)
    strings += [content for content, _ in interaction.response.sent]
    _, interaction = _gate(_command("proc kill", {"bang": "!kill"}),
                           STRANGER, b.CHANNEL_ID)
    strings += [content for content, _ in interaction.response.sent]
    monkeypatch.setattr(b, "USER_ROLES", {"viewer_user_ids": [VIEWER_UID]})
    _, interaction = _gate(_OPERATOR_CMD, VIEWER_UID, b.CHANNEL_ID)
    strings += [content for content, _ in interaction.response.sent]
    monkeypatch.setattr(b, "USER_ROLES", {})

    # `!` 與 `@bot`。
    message = _bang("!kill 1", STRANGER, b.CHANNEL_ID)
    _deliver(message)
    strings += [str(r) for r in message.replies]
    message = _mention("restart", STRANGER, OTHER_CHANNEL)
    _deliver(message)
    strings += [str(r) for r in message.replies]

    assert len(strings) >= 5, f"沒收集到全部的拒絕訊息：{strings}"

    banned = (
        # 主機路徑與專案相對路徑
        ":\\", ":/", "..", "/axiomatic", "\\axiomatic",
        ".py", ".log", ".md", ".json", ".ndjson", "output/", "todo_",
        ".chrome_profile",
        # 原始例外文字的痕跡
        "Traceback", "Error(", "Exception",
        # 外部服務／後端
        "NovelAI", "webrunner", "Danbooru", "danbooru", "Anthropic",
        "anthropic", "Claude", "claude", "Codex", "codex", "OpenAI",
    )
    for text in strings:
        for token in banned:
            assert token not in text, (
                f"拒絕訊息 {text!r} 含有 {token!r}——這個字串會被送到任何一個"
                "伺服器的任何一個人面前（保密規則 Layer 1）。")
        # 拒絕訊息不該夾帶行程識別碼之類的長數字。
        assert not any(len(run) >= 5 for run in
                       __import__("re").findall(r"\d+", text)), text


def test_autocomplete_keystrokes_never_reach_the_owner_only_gate(armed, capsys):
    """自動補全的早退必須排在擁有者閘**之前**，不只是排在計數之前。

    `CommandTree._call` 在第一行就叫這個鉤子，而補全分支在二十幾行之後——所以
    這個鉤子會在補全欄位的**每一次按鍵**上觸發。既有測試已經釘住「不計數、不
    稽核、不回訊息」；這裡補的是第四個出口：擁有者閘會對每次拒絕印一行 stderr。
    早退若挪到它後面，一個非擁有者在 `/proc kill` 的補全欄位裡打字，每一個按鍵
    都會多一行「denied owner-only」。本專案量過一次 log 被單一句子灌到 95.8%
    的下場——被淹掉的診斷等於沒有診斷。
    """
    capsys.readouterr()
    interaction = _FakeInteraction(
        _command("proc kill", {"bang": "!kill"}), STRANGER, OTHER_CHANNEL,
        itype=discord.InteractionType.autocomplete)
    allowed = asyncio.run(b._tree_check(interaction))
    assert allowed is True
    assert interaction.response.sent == []
    assert b._METRICS_CMD_COUNTS == Counter()
    assert _audit_lines(armed) == []
    captured = capsys.readouterr()
    assert captured.err == "", (
        f"補全的每一次按鍵都會印這個：{captured.err!r}")


# ---------------------------------------------------------------------------
# `!` 表面上三個從來沒觸發過的閘（2026-09-21 分支覆蓋率盤點）
# ---------------------------------------------------------------------------
# `on_message` 開頭三道 `return`——bot 自己的訊息、不是 `!` 開頭的閒聊、角色閘拒絕——
# 在整套測試裡一次都沒走過。角色閘在斜線那一面有測（上面三支），`!` 這一面沒有：刪掉
# 拒絕之後那個 `return`，全套件照樣綠，而被拒絕的人的指令會照跑。
_OPERATOR_BANG = "!c1"          # `cmd_character_add`，operator 級


@pytest.fixture(name="disarmed_add")
def _disarmed_add_fixture(monkeypatch, tmp_path):
    """`!c1` 背後的 handler 換成記錄器，佇列檔也導到 tmp——閘一旦失守，這個測試不能
    真的往正式佇列寫一筆（變異測試會把閘拿掉，所以這不是多慮）。"""
    calls: list = []

    async def _recorder(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(b, "cmd_character_add", _recorder)
    monkeypatch.setattr(b, "TODO_FILE_1", tmp_path / "todo_character1.md")
    return calls


def test_the_bang_role_gate_refuses_a_user_below_the_required_tier(
        armed, monkeypatch, disarmed_add):
    monkeypatch.setattr(b, "USER_ROLES", {"viewer_user_ids": [VIEWER_UID]})
    message = _bang(f"{_OPERATOR_BANG} Amiya", VIEWER_UID, b.CHANNEL_ID)
    _deliver(message)
    assert disarmed_add == [], "被角色閘拒絕的 `!` 指令還是跑了"
    assert len(message.replies) == 1
    assert "operator" in message.replies[0] and "viewer" in message.replies[0]
    assert b._METRICS_CMD_COUNTS == Counter()
    assert _audit_lines(armed) == []


def test_the_bang_role_gate_lets_a_sufficient_tier_through(
        armed, monkeypatch, disarmed_add):
    """反面：否則「設定了角色就一律擋」也會通過上面那支。"""
    operator = VIEWER_UID + 1
    monkeypatch.setattr(b, "USER_ROLES", {"viewer_user_ids": [VIEWER_UID],
                                          "operator_user_ids": [operator]})
    _deliver(_bang(f"{_OPERATOR_BANG} Amiya", operator, b.CHANNEL_ID))
    assert len(disarmed_add) == 1
    assert b._METRICS_CMD_COUNTS[_OPERATOR_BANG] == 1


def test_the_bot_ignores_its_own_messages(armed):
    """bot 自己送出的 `!…`（例如回覆裡引用了指令）不得被當成指令再派發一次。"""
    message = _bang(UNLOCKED_BANG, BOT_UID, b.CHANNEL_ID)
    _deliver(message)
    assert armed.touched == []
    assert message.replies == []
    assert b._METRICS_CMD_COUNTS == Counter()


def test_ordinary_chatter_in_the_channel_is_ignored(armed):
    """頻道裡不是 `!` 開頭、也沒有叫 bot 的一般對話：什麼都不做、不回話、不計數。"""
    message = _bang("queue 還有幾張？", STRANGER, b.CHANNEL_ID)
    _deliver(message)
    assert armed.touched == []
    assert message.replies == []
    assert b._METRICS_CMD_COUNTS == Counter()
    assert _audit_lines(armed) == []
