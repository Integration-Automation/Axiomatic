"""`/dorossi model`、`/dorossi effort`：斜線路徑與 token 路徑必須寫同一個地方。

這兩項設定現在有**兩個入口**：

* 提問開頭的 token（`@bot /model opus <提問>`），由
  `dorossi_backend._dorossi_parse_turn_flags` 解析；
* 原生斜線指令 `/dorossi model` / `/dorossi effort`（2026-09-12 新增）。

兩條路各自看起來都會動「這個工作階段的模型」，所以最容易長出來的缺陷不是「壞
掉」而是**分岔**：新的那條寫進另一個欄位、或自己抄一份合法值清單，於是
`/dorossi model sonnet` 回「已更新」、下一輪送給後端的卻還是舊值——兩邊都不會丟
例外，也不會有任何測試變紅，因為兩條路各自都「對」。

所以本檔的核心是一支**對帳**測試：兩條路各設一次，比對存放檔裡真正落地的欄位。
其餘幾支釘的是同一類的靜默分岔：選單的合法值必須就是後端的 allowlist；顯示端只
准有一份（`_dorossi_tuning_labels`），`/dorossi session list` 與 `/dorossi model`
對同一個工作階段必須講出同一個字。
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import discord_bot as b  # noqa: E402
import dorossi_backend as db  # noqa: E402


# ---------------------------------------------------------------------------
# 夾具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_live_state(tmp_path, monkeypatch):
    """把會落地的兩個檔案指到 tmp。

    `dorossi_session.json` 裝的是擁有者**所有**對話的續接資訊，
    `dorossi_events.ndjson` 是正式的診斷紀錄——跑測試把正式的那兩個檔寫髒過一次
    就夠了（`test_bot_helpers` 的 `audit.ndjson` 就是前車之鑑：5,287 行裡 5,281
    行是測試留下的）。autouse 是刻意的：漏掉一支就會寫到正式檔。
    """
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE",
                        tmp_path / "dorossi_session.json")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE",
                        tmp_path / "dorossi_events.ndjson")
    return tmp_path / "dorossi_session.json"


@pytest.fixture
def store(_isolate_live_state):
    return _isolate_live_state


class _Msg:
    """最小的 `discord.Message` 替身：`safe_reply` 只碰 `reply` 與 `channel`。"""

    def __init__(self, uid: int = 0):
        self.author = type("_A", (), {"id": uid or b.DOROSSI_USER_ID})()
        self.channel = type("_C", (), {"id": 0})()
        self.replies: list[str] = []

    async def reply(self, content=None, **_kwargs):
        self.replies.append(content)
        return None

    @property
    def text(self) -> str:
        return "\n".join(str(r) for r in self.replies)


def _run(coro):
    return asyncio.run(coro)


def _slot(store_path: Path) -> dict:
    """存放檔裡目前 active 的那個 slot。"""
    state = json.loads(store_path.read_text(encoding="utf-8"))
    record = state[str(b.DOROSSI_USER_ID)]
    return record["sessions"][record["active"]]


def _tuning_of(store_path: Path) -> dict:
    """slot 裡跟微調有關的欄位——**只看 `tune_*`**。

    刻意不比整個 slot：`last_used` 是時間戳，兩條路跑的時間本來就不同，拿整包
    比會變成一支永遠紅的測試（然後被放寬成永遠綠）。
    """
    return {k: v for k, v in _slot(store_path).items() if k.startswith("tune_")}


# ---------------------------------------------------------------------------
# 核心：兩條路寫同一個地方
# ---------------------------------------------------------------------------
def test_the_slash_path_and_the_token_path_write_the_same_slot_fields(store):
    """**本檔的重點。** 兩個入口寫進去的必須是同一組欄位、同樣的值。

    寫成兩次獨立的執行再比對，而不是「看看斜線那條有沒有呼叫某支函式」——後者
    在「呼叫了但結果被丟掉」時照樣通過（本 repo 已經踩過那個形狀好幾次）。
    """
    # token 路徑：只打指令、沒有提問 ⇒ `mcmd_dorossi` 的純設定更新分支。
    token_msg = _Msg()
    _run(b.mcmd_dorossi(token_msg, "/model sonnet /effort high"))
    token_tuning = _tuning_of(store)
    store.unlink()

    # 斜線路徑：兩支各設一次。
    _run(b.mcmd_model(_Msg(), "sonnet"))
    _run(b.mcmd_effort(_Msg(), "high"))
    slash_tuning = _tuning_of(store)

    # 正面對照：先確定真的有東西被寫進去，否則 `{} == {}` 會讓這支空過。
    assert token_tuning == {"tune_model": "sonnet", "tune_effort": "high"}, (
        f"token 路徑寫出來的欄位不如預期：{token_tuning}")
    assert slash_tuning == token_tuning, (
        "斜線路徑與 token 路徑寫進工作階段的內容不一致："
        f"斜線 {slash_tuning}、token {token_tuning}。兩個入口必須共用"
        "`_dorossi_apply_turn_tuning` 與同一組 slot 欄位，否則其中一條會"
        "「回覆已更新、實際沒生效」。")


@pytest.mark.parametrize("kind, value, field", [
    ("model", "haiku", "tune_model"),
    ("effort", "xhigh", "tune_effort"),
])
def test_default_clears_the_override_on_both_paths(store, kind, value, field):
    """`default` 是唯一的清除語法，兩條路都要真的把鍵**刪掉**。

    留成 `None` 而不是刪掉一樣「看起來清掉了」，但 `_dorossi_session_tuning` 讀
    回來會是 `None` ⇒ 行為碰巧一致，直到有人改成 `key in sess` 的判定。所以這裡
    釘的是鍵不存在，不是值為 None。
    """
    handler = b.mcmd_model if kind == "model" else b.mcmd_effort

    # 斜線路徑
    _run(handler(_Msg(), value))
    assert _tuning_of(store) == {field: value}
    _run(handler(_Msg(), db.DOROSSI_TUNE_DEFAULT))
    assert _tuning_of(store) == {}, "斜線路徑的 default 沒有清掉覆寫"

    # token 路徑
    _run(b.mcmd_dorossi(_Msg(), f"/{kind} {value}"))
    assert _tuning_of(store) == {field: value}
    _run(b.mcmd_dorossi(_Msg(), f"/{kind} {db.DOROSSI_TUNE_DEFAULT}"))
    assert _tuning_of(store) == {}, "token 路徑的 default 沒有清掉覆寫"


def test_the_two_fields_do_not_disturb_each_other(store):
    """設模型不得順手動到力度，反之亦然。

    共用本體是靠兩個三元運算把「本輪要動哪一個」分開的（另一個傳 `None` ＝不動）。
    把那兩個寫反、或兩個都傳 `choice`，在只測單一欄位的測試底下完全看不出來。
    """
    _run(b.mcmd_effort(_Msg(), "low"))
    _run(b.mcmd_model(_Msg(), "opus"))
    assert _tuning_of(store) == {"tune_effort": "low", "tune_model": "opus"}
    _run(b.mcmd_model(_Msg(), db.DOROSSI_TUNE_DEFAULT))
    assert _tuning_of(store) == {"tune_effort": "low"}, (
        "清除模型覆寫時把力度也一起清掉了")


# ---------------------------------------------------------------------------
# 驗證與閘門
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind, bad", [
    ("model", "gpt-4"),
    ("model", "../../etc/passwd"),
    ("effort", "ultra"),
    ("effort", "-1"),
])
def test_an_illegal_value_is_refused_and_nothing_is_written(store, kind, bad):
    """使用者輸入永不原樣落地——這是 allowlist 驗證的硬需求。

    斜線表面有 `choices=` 擋在前面，所以平常打不到這條路；但 handler 兩個表面共
    用，而「存進去的 key 會被 `_dorossi_session_tuning` 查表送進 CLI 參數」。
    """
    handler = b.mcmd_model if kind == "model" else b.mcmd_effort
    msg = _Msg()
    _run(handler(msg, bad))
    assert f"`/{kind}` 只接受" in msg.text, (
        f"非法值 {bad!r} 沒有得到合法值清單：{msg.text!r}")
    assert bad not in msg.text, "錯誤訊息回聲了使用者輸入；原始值只該進 stderr"
    if store.exists():
        assert _tuning_of(store) == {}, f"非法值 {bad!r} 還是被寫進工作階段了"


@pytest.mark.parametrize("handler_name", ["mcmd_model", "mcmd_effort"])
def test_a_stranger_is_refused_before_anything_happens(store, handler_name):
    """非擁有者連工作階段都不該被建出來。"""
    msg = _Msg(uid=b.DOROSSI_USER_ID + 1)
    _run(getattr(b, handler_name)(msg, "opus"))
    assert msg.text == "此指令僅限擁有者使用。"
    assert not store.exists(), "非擁有者觸發了副作用（建了工作階段存放檔）"


@pytest.mark.parametrize("handler_name", ["mcmd_model", "mcmd_effort"])
def test_a_running_task_blocks_a_change_but_not_a_read(store, monkeypatch,
                                                       handler_name):
    """自走任務進行中改設定會半途換模型，所以擋下來；純顯示不受影響。"""
    handler = getattr(b, handler_name)
    _run(handler(_Msg(), ""))          # 先讓 active slot 存在
    uid = str(b.DOROSSI_USER_ID)
    sid = json.loads(store.read_text(encoding="utf-8"))[uid]["active"]
    monkeypatch.setitem(b._dorossi_loops, b._dorossi_session_key(uid, sid),
                        object())

    busy = _Msg()
    _run(handler(busy, "opus" if handler_name == "mcmd_model" else "high"))
    assert "進行中" in busy.text
    assert _tuning_of(store) == {}, "任務進行中還是把設定寫進去了"

    reading = _Msg()
    _run(handler(reading, ""))
    assert "進行中" not in reading.text, "純顯示不該被進行中的任務擋下來"


def test_showing_does_not_change_anything(store):
    """空引數是唯讀的。"""
    _run(b.mcmd_model(_Msg(), "sonnet"))
    before = _tuning_of(store)
    msg = _Msg()
    _run(b.mcmd_model(msg, ""))
    assert _tuning_of(store) == before == {"tune_model": "sonnet"}
    assert "sonnet" in msg.text and "已更新" not in msg.text


# ---------------------------------------------------------------------------
# 合法值只有一個來源
# ---------------------------------------------------------------------------
def test_the_effort_menu_is_generated_from_the_backend_allowlist():
    """力度選單 ＝ allowlist ＋ `default`，不多不少。

    手寫第二份清單的話，後端新增一個值時選單不會跟著長（使用者只能走 token
    路徑），或反過來選單留著一個後端已經拿掉的值（`_dorossi_session_tuning` 查
    表落空 ⇒ 靜默退回預設，使用者以為設定生效了）。

    模型那半 2026-09-23 改成 autocomplete，不再是靜態選單——見下面三支。
    """
    allowed = db.DOROSSI_EFFORT_LEVELS
    assert allowed, "effort 的 allowlist 是空的——這支會空過"
    values = [c.value for c in b._DOROSSI_EFFORT_CHOICES_SLASH]
    assert values == list(allowed) + [db.DOROSSI_TUNE_DEFAULT], (
        f"`/dorossi effort` 的選單與後端 allowlist 不一致：{values}")
    # Discord 的選項上限是 25，而選單是從 allowlist 生出來的。爆掉的位置離
    # allowlist 很遠：指令樹同步被整個打回，`/dorossi` 這一群的更新一起失敗。
    assert len(values) <= 25, (
        f"`/dorossi effort` 的選單有 {len(values)} 個選項，超過對話平台的 25 上限"
        "——整棵指令樹會同步失敗。")


@pytest.mark.parametrize("backend", ["claude_code", "api", "codex"])
def test_the_model_autocomplete_offers_exactly_this_backends_allowlist(backend):
    """模型候選 ＝ **這個後端**的 allowlist ＋ `default`，不多不少。

    靜態 `choices=` 只能列一張表，所以另一個後端的使用者會選到一批送出去必定失敗
    的值——而失敗要等下一次提問才看得到，訊息還是泛用的。
    """
    allowed = db.dorossi_model_choices(backend)
    assert allowed, f"{backend} 的 allowlist 是空的——這支會空過"
    values = [value for _name, value in b._dorossi_model_options(backend, "")]
    assert values == list(allowed) + [db.DOROSSI_TUNE_DEFAULT], (
        f"`/dorossi model` 在 {backend} 上的候選值與 allowlist 不一致：{values}")


def test_the_model_autocomplete_never_exceeds_the_platform_cap():
    """單次回應最多 25 筆——這是 autocomplete 唯一還在的上限。

    合成語料而不是真實資料：真的表現在只有十幾筆，拿真實資料測等於測不到截斷，
    而「表長到 26 筆」正是這次把靜態選單換掉的原因。
    """
    fake = {f"m-{n}.0": f"claude-m-{n}-0" for n in range(40)}
    options = b._dorossi_model_options("claude_code", "")
    assert len(options) <= b._DOROSSI_AUTOCOMPLETE_LIMIT
    original = db.DOROSSI_BACKEND_MODEL_CHOICES.get("claude_code")
    db.DOROSSI_BACKEND_MODEL_CHOICES["claude_code"] = fake
    try:
        many = b._dorossi_model_options("claude_code", "")
    finally:
        db.DOROSSI_BACKEND_MODEL_CHOICES["claude_code"] = original
    assert len(many) == b._DOROSSI_AUTOCOMPLETE_LIMIT, (
        f"41 個候選只截到 {len(many)} 筆——截斷沒有生效，指令回應會被平台打回。")


def test_the_model_autocomplete_filters_by_what_was_typed():
    """打了字就只留含那幾個字的候選；不然超過 25 筆的表只看得到前 25 個。"""
    values = [value for _n, value in
              b._dorossi_model_options("claude_code", "haiku")]
    assert values, "打 `haiku` 一個候選都沒有——過濾把全部都濾掉了"
    assert all("haiku" in value for value in values), values
    assert "opus" not in values


def test_no_full_model_id_can_reach_a_display_label():
    """顯示端只准吐**別名**，完整 model id 一個都不准漏出去。

    2026-09-12 之前這條是**自動成立**的：表裡四個 key 全是恆等映射（value 就是
    key），所以「反查回 key」跟「原樣回傳」看起來一樣，怎麼寫都對。加上版本之後
    value 變成完整 model id，這才第一次成為一個**可以壞掉**的性質——打錯一個
    value、或兩個 key 共用同一個 value，`_model_alias_for` 就會反查落空、改吐
    `後端預設模型`（假訊息），或更糟：若有人把 `_model_alias_for` 改成「查不到就
    原樣回傳」，完整 id 會直接進對話平台。保密裁定（2026-07-02）放行的是別名，
    不是 id。
    """
    table = db.DOROSSI_MODEL_CHOICES
    assert len(table) >= 4, f"allowlist 太小（{len(table)}），這支會空過"

    assert len(set(table.values())) == len(table), (
        "兩個別名共用同一個 value——`_model_alias_for` 的反查只會回其中一個，"
        f"另一個別名從此顯示成別人的名字：{sorted(table.values())}")

    for alias, value in table.items():
        assert not alias.startswith("claude-"), (
            f"別名 {alias!r} 長得像完整 model id。key 是**會被顯示**的那一半，"
            "完整 id 只能待在 value。")
        assert b._model_alias_for(value) == alias, (
            f"{value!r} 反查不回它自己的別名 {alias!r}（得到 "
            f"{b._model_alias_for(value)!r}）——顯示端會講錯模型，或洩漏完整 id。")


def test_every_value_is_derivable_from_its_own_alias():
    """value 必須就是 key 機械推導出來的那一個字。

    **這支是上一支殺不掉的那個變異。** 反查（`_model_alias_for`）用的是**同一張
    表**，所以 value 打錯字也照樣自洽：`opus-4.8` → `claude-opus-4-88` 反查回來
    仍然是 `opus-4.8`，上一支全綠。實測過，那個變異 SURVIVED。

    而打錯的代價不在這裡：allowlist 驗證會放行（key 命中了），值被存進工作階段，
    然後**下一次提問**才由後端 CLI 退回 `unrecognized_model`——使用者拿到的是一句
    泛用失敗訊息，離這張表十萬八千里。

    所以改釘**轉換規則**而不是外部事實（去掃 CLI 的模型目錄要 222MB、而且在別台
    機器上只會 skip，等於裝飾）：不帶版本的 key ⇒ value 就是裸別名（語意是「這族
    最新的」，交給後端解析）；帶版本的 key ⇒ `claude-` ＋ key，小數點換成連字號。
    真有新模型不照這個規則命名時這支會紅——那時要做的是在這裡寫一筆例外並說明，
    不是把這支刪掉。
    """
    table = db.DOROSSI_MODEL_CHOICES
    versioned = [a for a in table if "-" in a]
    bare = [a for a in table if "-" not in a]
    # 正面對照：兩種形狀都要真的有資料，否則下面的迴圈可以一格都不跑就綠。
    assert len(versioned) >= 4, f"帶版本的別名只有 {versioned}——這支會空過"
    assert len(bare) >= 4, f"不帶版本的別名只有 {bare}——這支會空過"

    for alias in bare:
        assert table[alias] == alias, (
            f"不帶版本的別名 {alias!r} 的 value 是 {table[alias]!r}。它的語意是"
            "「這一族當下最新的那個」，必須原樣交給後端解析，釘死就失去意義了。")
    for alias in versioned:
        expected = "claude-" + alias.replace(".", "-")
        assert table[alias] == expected, (
            f"別名 {alias!r} 的 value 是 {table[alias]!r}，但照命名規則推導應該是 "
            f"{expected!r}。打錯的話 allowlist 照樣放行、值照樣存進工作階段，"
            "要等下一次提問才會被後端退回，而使用者只看得到一句泛用失敗訊息。")


def test_the_error_message_lists_exactly_the_legal_values():
    """錯誤訊息也不准自己抄一份清單。"""
    msg = _Msg()
    _run(b.mcmd_model(msg, "nope"))
    for alias in db.DOROSSI_MODEL_CHOICES:
        assert alias in msg.text, f"合法值 {alias} 沒出現在錯誤訊息裡"
    assert db.DOROSSI_TUNE_DEFAULT in msg.text


# ---------------------------------------------------------------------------
# 顯示端只有一份
# ---------------------------------------------------------------------------
def _autocomplete_options(wrapper: str) -> set:
    """模組層 AST 裡 `@<wrapper>.autocomplete("<option>")` 宣告了哪些參數名。

    autocomplete 掛在**另一個**函式上，所以它不在包裝層自己的 `decorator_list`
    裡——照 `choices=` 的找法找會一無所獲，而「一無所獲」跟「沒宣告」長得一模一樣。
    """
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "autocomplete"
                    and getattr(decorator.func.value, "id", None) == wrapper):
                continue
            for arg in decorator.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value)
    return found


def _function(name: str) -> ast.AST:
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"找不到 {name}")


def test_the_session_list_reuses_the_shared_label_helper():
    """`/dorossi session list` 不准自己再算一次模型標籤。

    它原本內嵌了那三層分支；`/dorossi model` 若另外抄一份，兩邊就會在
    「換了後端」「存放檔被手改」這種邊角上各說各話。
    """
    fn = _function("_dorossi_render_session_list")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_dorossi_tuning_labels" in called, (
        "`_dorossi_render_session_list` 沒有呼叫 `_dorossi_tuning_labels`——"
        "模型標籤的判定又長出第二份了")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert not names & {"DOROSSI_CC_MODEL", "DOROSSI_LEGACY_MODEL_KEYS"}, (
        "session 列表又自己碰起模型常數了；那幾層分支只該住在 "
        "`_dorossi_tuning_labels`")


@pytest.mark.parametrize("slot, expected", [
    ({}, "opus"),                                    # 沒設定 ⇒ 後端預設
    ({"tune_model": "sonnet"}, "sonnet"),
    ({"tune_model": "max"}, "opus"),                 # 舊階層 key 讀取時遷移
    ({"tune_model": "已經不合法了"}, "opus"),          # 存放檔被手改
])
def test_the_two_surfaces_say_the_same_word_about_the_model(store, slot,
                                                            expected, monkeypatch):
    """同一個工作階段，`/dorossi model` 與 `/dorossi session list` 要講同一個字。

    兩邊各有自己的測試不算守住——那是「兩份實作、兩份綠色測試、沒有東西在比對
    它們」。這支餵同一批 slot 給兩個表面。
    """
    monkeypatch.setattr(b, "DOROSSI_CC_MODEL", "opus")
    uid = str(b.DOROSSI_USER_ID)
    state = {uid: {"active": "s1", "next_seq": 2, "sessions": {"s1": dict(slot)}}}
    store.write_text(json.dumps(state), encoding="utf-8")

    listing = b._dorossi_render_session_list(db._dorossi_load_state(), uid)
    msg = _Msg()
    _run(b.mcmd_model(msg, ""))

    assert f"模型 {expected}" in listing, f"session 列表講的不是 {expected}：{listing}"
    assert f"`{expected}`" in msg.text, f"`/dorossi model` 講的不是 {expected}：{msg.text}"


# ---------------------------------------------------------------------------
# 斜線宣告本身
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sub, wrapper, handler, option, menu", [
    ("model", "slash_dorossi_model", "mcmd_model", "model", "autocomplete"),
    ("effort", "slash_dorossi_effort", "mcmd_effort", "effort", "choices"),
])
def test_the_slash_wrapper_delegates_and_offers_a_menu(sub, wrapper, handler,
                                                       option, menu):
    """包裝層只負責轉交，而且要把合法值列進選單。

    沒有選單的話使用者得自己記得打什麼；更糟的是非法值會一路走到 handler 的錯誤
    分支，而那正是斜線指令該在送出前就擋掉的事。

    **兩支的選單形狀刻意不同**（2026-09-23）：力度的合法值與後端無關，所以是靜態
    `choices=`；模型的合法值按後端算、而且會被每日的模型目錄檢查在執行期加長，
    靜態選單追不上，所以是 `autocomplete`。`menu` 參數就是這個差別，寫死成同一種
    會讓其中一支永遠假綠。
    """
    fn = _function(wrapper)
    # 從 AST 讀關鍵字，不要對 `ast.unparse` 的引號習慣做假設——它輸出單引號，
    # 拿雙引號去比對會得到一支「永遠紅」的測試，而最省事的修法是把它放寬掉。
    declared = {}
    for decorator in fn.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        declared[ast.unparse(decorator.func)] = {
            kw.arg: kw.value for kw in decorator.keywords}
    command = declared.get("dorossi_group.command")
    assert command is not None, f"沒有掛在 `dorossi_group` 上：{sorted(declared)}"
    assert getattr(command.get("name"), "value", None) == sub, (
        f"宣告出來的名字不是 `{sub}`：{sorted(declared)}")
    if menu == "choices":
        choices = declared.get("discord.app_commands.choices")
        assert choices is not None and option in choices, (
            f"`/dorossi {sub}` 沒有 `choices=`：{sorted(declared)}")
    else:
        # autocomplete 是**另一個函式**掛上去的裝飾器（`@slash_x.autocomplete("y")`），
        # 不在這支函式的 decorator_list 裡，所以從模組層的 AST 找。
        assert _autocomplete_options(wrapper) == {option}, (
            f"`/dorossi {sub}` 沒有替 `{option}` 掛 autocomplete："
            f"{_autocomplete_options(wrapper)}")
    assert "discord.app_commands.describe" in declared, (
        f"`/dorossi {sub}` 的參數沒有說明文字：{sorted(declared)}")
    delegated = {n.id for call in ast.walk(fn)
                 if isinstance(call, ast.Call)
                 and getattr(call.func, "id", None) == "_slash_run"
                 for n in call.args if isinstance(n, ast.Name)}
    assert handler in delegated, (
        f"`/dorossi {sub}` 沒有委派給 `{handler}`：{delegated}")


def test_the_wrapper_scan_actually_reads_a_declaration():
    """canary：抽取器壞掉時上面那支會變成「什麼都沒檢查」。"""
    fn = _function("slash_dorossi_effort")
    assert fn.decorator_list, "抽不到任何裝飾器"
    assert len(fn.decorator_list) >= 3, (
        f"只抽到 {len(fn.decorator_list)} 個裝飾器——command / describe / choices "
        "三個應該都在")
    # 模型那支只有兩個裝飾器（選單搬去 autocomplete 了），所以它的 canary 是
    # 「autocomplete 抽得到東西」——抽不到的話上面那支的 else 分支會空過。
    assert _autocomplete_options("slash_dorossi_model"), (
        "抽不到 `slash_dorossi_model` 的 autocomplete 宣告")
