"""後端模型目錄：每個後端一張表、每日檢查、把發現的東西併回表。

三件事在 2026-09-23 之前是**靜默壞掉**的，本檔一支一支釘住：

1. `/model` 只有一個後端真的吃得到。另外兩條路一條把模型寫死（`messages.create`
   的 `model=` 是常數）、一條根本不帶 `-m`，而顯示端只會說「後端預設模型」——指令
   回「已更新」，值被存起來，然後每一輪都被丟掉，使用者看不到任何差別。
2. 一張表服務兩家廠商的模型名。`/model` 因此會提供一批在目前後端上送出去必定失敗
   的值，而失敗要等下一次提問、訊息還是泛用的。
3. 表是手寫的常數，新模型上線只能靠人記得改。

所以本檔的核心是三組對帳：**解析**（別名 ↔ 完整 id 必須是可逆的，不可逆就不准併
進表）、**路由**（每個後端只收自己那張表的值，收不下要講出來）、**落地**（目錄只
新增不覆寫，內建表永遠是地板）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import discord_bot as b  # noqa: E402
import dorossi_backend as db  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate_catalog(tmp_path, monkeypatch):
    """目錄檔指到 tmp，兩張表各給一份**複本**。

    兩張表是模組層的可變 dict，而合併是就地長大的——不換成複本的話，一支測試併進去
    的別名會留在表裡影響同一輪的其他測試（以及 `test_dorossi_tuning` 那幾支對帳）。
    """
    monkeypatch.setattr(db, "DOROSSI_MODEL_CATALOG_FILE",
                        tmp_path / "dorossi_models.json")
    # 從**內建**表複製，不是從現在的表：模組載入時已經把這台主機上的目錄檔併進去了，
    # 而那份檔是正在跑的 bot 每天改寫的。那次檢查第一次真的找到模型之後，「新發現的
    # 別名」在這裡全都成了「已經有了」，四支測試跟著紅——程式一行沒動。
    claude = dict(db._DOROSSI_BUILTIN_MODEL_CHOICES)
    codex = dict(db._DOROSSI_BUILTIN_CODEX_MODEL_CHOICES)
    monkeypatch.setattr(db, "DOROSSI_MODEL_CHOICES", claude)
    monkeypatch.setattr(db, "DOROSSI_CODEX_MODEL_CHOICES", codex)
    monkeypatch.setattr(db, "DOROSSI_BACKEND_MODEL_CHOICES",
                        {"claude_code": claude, "api": claude, "codex": codex})
    # 聯集是**就地**更新的一個 dict，所以也要換成自己的一份，否則這裡併進去的別名
    # 會留在正式那一份裡影響同一輪的其他測試。**兩個模組要換成同一個物件**——正式
    # 環境裡它們本來就是同一個（`discord_bot` 以名字 import 它），各給一份複本的話
    # 這裡量到的就不是正式的行為（第一版真的各給一份，於是一支本來該綠的測試紅了，
    # 而紅的原因跟正式碼無關）。
    union = {**claude, **codex}
    monkeypatch.setattr(db, "DOROSSI_ALL_MODEL_CHOICES", union)
    monkeypatch.setattr(b, "DOROSSI_ALL_MODEL_CHOICES", union)
    monkeypatch.setattr(db, "_DOROSSI_MODEL_CATALOG", {})
    return tmp_path


def test_both_modules_hold_the_same_union_object():
    """⚠️ 不受上面那個夾具影響的前提檢查：正式環境裡兩個模組的聯集必須是**同一個
    物件**。`dorossi_backend` 就地更新它、`discord_bot` 以名字 import 它——哪天有人
    把重建改成「重新指派一個新 dict」，bot 那邊就會永遠抓著舊的那一份，而症狀只有
    「每日檢查發現的新別名在 bot 這邊不認得」，沒有任何錯誤。
    """
    import importlib
    fresh_db = importlib.import_module("dorossi_backend")
    fresh_b = importlib.import_module("discord_bot")
    assert fresh_b.DOROSSI_ALL_MODEL_CHOICES is fresh_db.DOROSSI_ALL_MODEL_CHOICES


# ---------------------------------------------------------------------------
# 1. 解析：別名 ↔ 完整 id 必須可逆
# ---------------------------------------------------------------------------
# 前四筆是 2026-09-23 那次真的探測讀回來的字串，不是編的。
@pytest.mark.parametrize("namespace, model_id, alias", [
    ("claude", "claude-opus-5-5", "opus-5.5"),
    ("claude", "claude-sonnet-5", "sonnet-5"),
    ("claude", "claude-haiku-4-5-20251001", "haiku-4.5.20251001"),
    ("claude", "claude-fable-5-1", "fable-5.1"),
    ("codex", "gpt-5.6-sol", "sol-5.6"),
])
def test_an_alias_round_trips_back_to_its_model_id(namespace, model_id, alias):
    """兩家的 id 形狀不同（一家「廠商-族-版」、一家「廠商-版-族」），但別名的規則
    是同一條：拿掉廠商前綴、族名在前、版號在後。來回都要對得上。"""
    assert db.dorossi_model_alias_from_id(model_id) == alias
    assert db.dorossi_model_id_from_alias(namespace, alias) == model_id


def test_an_alias_never_carries_a_vendor_prefix():
    """別名是**會被顯示**的那一半。完整 id 與廠商字樣只能待在 value。"""
    for model_id in ("claude-opus-5-5", "gpt-5.6-sol"):
        alias = db.dorossi_model_alias_from_id(model_id)
        assert alias and not alias.startswith(("claude", "gpt")), alias


@pytest.mark.parametrize("bad", [
    None, "", "   ", "claude", 17, "claude opus 5", "vendor/model-1",
])
def test_a_model_id_that_makes_no_sense_is_refused_not_guessed(bad):
    """餵進來的是別的行程印出來的字串，看不懂就回 None，不要猜。"""
    assert db.dorossi_model_alias_from_id(bad) is None


# ---------------------------------------------------------------------------
# 2. 落地：只新增、不覆寫，而且不可逆的條目不准進來
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("snapshot, literal", [
    ("_DOROSSI_BUILTIN_MODEL_CHOICES", "DOROSSI_MODEL_CHOICES"),
    ("_DOROSSI_BUILTIN_CODEX_MODEL_CHOICES", "DOROSSI_CODEX_MODEL_CHOICES"),
])
def test_the_built_in_snapshot_is_exactly_what_the_source_says(snapshot, literal):
    """上面那個夾具的前提：內建表複本就是原始碼裡寫的那張字面表，**不含**載入時從這台
    主機的目錄檔併進來的東西。複本若在合併之後才取、或只是同一個物件的別名，主機上的
    目錄檔就又會滲進每一支測試——而這台主機的目錄檔裡可能真的有東西。"""
    import ast  # noqa: PLC0415
    source = Path(db.__file__).read_text(encoding="utf-8")
    table = next(node.value for node in ast.parse(source).body
                 if isinstance(node, ast.Assign)
                 and any(isinstance(tg, ast.Name) and tg.id == literal
                         for tg in node.targets)
                 and isinstance(node.value, ast.Dict))
    written = {ast.literal_eval(k): ast.literal_eval(v)
               for k, v in zip(table.keys, table.values)}
    assert len(written) >= 2, written   # 正面對照：抽到的是一張表，不是空的
    assert getattr(db, snapshot) == written


def test_a_discovered_model_becomes_a_pinned_alias():
    catalog = {"resolved": {"claude": {"opus": "claude-opus-5-5"}}}
    added = db.dorossi_merge_model_catalog(catalog)
    assert added == ["opus-5.5"]
    assert db.DOROSSI_MODEL_CHOICES["opus-5.5"] == "claude-opus-5-5"
    # 裸別名**不准**被改掉：它的語意是「這一族當下最新的那個」，釘死就失去意義了。
    assert db.DOROSSI_MODEL_CHOICES["opus"] == "opus"


def test_the_builtin_table_is_the_floor_and_never_gets_overwritten():
    """內建的那幾筆是地板。目錄說 `opus-5` 是別的東西也不准改——全新 clone 沒有這個
    檔也要有一組可用的別名，而「值被目錄改掉」會讓同一個別名在兩台機器上指不同模型。"""
    before = dict(db.DOROSSI_MODEL_CHOICES)
    added = db.dorossi_merge_model_catalog(
        {"resolved": {"claude": {"opus": "claude-opus-5"}}})
    assert added == []
    assert db.DOROSSI_MODEL_CHOICES == before


def test_a_model_id_that_does_not_round_trip_is_not_merged():
    """⚠️ **這支擋的是一個會讓別人變紅的缺陷。**

    `test_dorossi_tuning.test_every_value_is_derivable_from_its_own_alias` 釘住
    「value ＝ key 機械推導出來的那一個字」。一個沒見過的命名形狀（版號排在族名
    前面的那種）推出來的別名推不回原本的 id——併進去的話那支會紅，而紅的位置離這裡
    很遠；更糟的是那個條目本身是壞的：allowlist 放行、值存進工作階段，要等下一次
    提問才由後端退回，使用者只看得到一句泛用失敗訊息。
    """
    before = dict(db.DOROSSI_MODEL_CHOICES)
    added = db.dorossi_merge_model_catalog(
        {"resolved": {"claude": {"sonnet": "claude-3-5-sonnet-20241022"}}})
    assert added == []
    assert db.DOROSSI_MODEL_CHOICES == before


def test_the_same_model_never_gets_a_second_alias():
    """一個值兩個別名的話，`_model_alias_for` 的反查只會回其中一個，另一個從此顯示
    成別人的名字。"""
    db.DOROSSI_MODEL_CHOICES["opus-9.9"] = "claude-opus-9-9"
    added = db.dorossi_merge_model_catalog(
        {"resolved": {"claude": {"opus": "claude-opus-9-9"}}})
    assert added == []


@pytest.mark.parametrize("junk", [
    {}, {"resolved": None}, {"resolved": {"claude": "not a dict"}},
    {"resolved": {"nosuchnamespace": {"x": "claude-x-1"}}},
])
def test_a_corrupt_catalog_changes_nothing(junk):
    """目錄檔是磁碟上的東西，可能被手改、可能半寫入。壞掉就當成「還沒檢查過」。"""
    before = dict(db.DOROSSI_MODEL_CHOICES)
    assert db.dorossi_merge_model_catalog(junk) == []
    assert db.DOROSSI_MODEL_CHOICES == before


def test_the_catalog_is_written_atomically_and_read_back(tmp_path):
    catalog = {"schema": 1, "checked_at": 1.0,
               "resolved": {"codex": {"sol": "gpt-5.6-sol"}}}
    assert db.dorossi_save_model_catalog(catalog) is True
    assert db.dorossi_load_model_catalog() == catalog
    # 同目錄 temp → `os.replace`，所以寫完不該留下 `.tmp`（留下來的話 `.gitignore`
    # 那條就得涵蓋它——確實有涵蓋，但這裡先確認正常路徑是乾淨的）。
    assert not (tmp_path / "dorossi_models.json.tmp").exists()


def test_a_corrupt_catalog_file_reads_as_never_checked(tmp_path):
    (tmp_path / "dorossi_models.json").write_text("{ not json",
                                                  encoding="utf-8")
    assert db.dorossi_load_model_catalog() == {}


# ---------------------------------------------------------------------------
# 3. 路由：每個後端只收自己那張表的值
# ---------------------------------------------------------------------------
def test_each_backend_offers_its_own_table():
    assert db.dorossi_model_choices("claude_code") is db.DOROSSI_MODEL_CHOICES
    # 兩個後端共用同一張 claude 表——它們吃的是同一批模型名。
    assert db.dorossi_model_choices("api") is db.DOROSSI_MODEL_CHOICES
    assert db.dorossi_model_choices("codex") is db.DOROSSI_CODEX_MODEL_CHOICES
    # 兩張表**不准有交集**，否則同一個別名在不同後端上指不同的東西。
    assert not (set(db.DOROSSI_MODEL_CHOICES)
                & set(db.DOROSSI_CODEX_MODEL_CHOICES))


def test_a_pinned_alias_resolves_to_its_own_model_id():
    assert db.dorossi_resolve_model("claude_code", "opus-4.8") == "claude-opus-4-8"
    assert db.dorossi_resolve_model("api", "opus-4.8") == "claude-opus-4-8"
    assert db.dorossi_resolve_model("codex", "sol-5.6") == "gpt-5.6-sol"


def test_a_bare_alias_stays_bare_only_where_the_cli_resolves_it():
    """裸別名的語意是「這一族當下最新的」。**只有一條路**的 CLI 自己認得它；另外兩條
    路原樣送出去的話，一條會被 SDK 退回、一條會被伺服器 400（實測，不是推測）。"""
    assert db.dorossi_resolve_model("claude_code", "opus") == "opus"
    for backend in ("api", "codex"):
        resolved = db.dorossi_resolve_model(backend, "opus" if backend == "api"
                                            else "sol")
        assert resolved and resolved not in ("opus", "sol"), (
            f"{backend} 拿到的還是裸別名 {resolved!r}——它不會自己解析，這會失敗")


def test_a_bare_alias_falls_back_to_the_newest_pinned_one_without_a_catalog():
    """目錄還沒建立（全新 clone、第一次檢查之前、探測失敗）時的地板。"""
    assert db.dorossi_resolve_model("api", "opus") == "claude-opus-5"
    assert db.dorossi_resolve_model("api", "sonnet") == "claude-sonnet-5"


def test_the_catalog_wins_over_the_floor_for_a_bare_alias(monkeypatch):
    monkeypatch.setattr(db, "_DOROSSI_MODEL_CATALOG",
                        {"resolved": {"claude": {"opus": "claude-opus-5-5"}}})
    assert db.dorossi_resolve_model("api", "opus") == "claude-opus-5-5"
    # claude 那條路仍然原樣送裸別名——它自己解析得比我們準，而且新模型上線時不必
    # 等每日檢查跑完。
    assert db.dorossi_resolve_model("claude_code", "opus") == "opus"


@pytest.mark.parametrize("backend, key", [
    ("codex", "opus"),          # 另一家的模型名
    ("claude_code", "sol-5.6"),  # 反過來
    ("api", "根本不是別名"),
    ("claude_code", None),
])
def test_a_key_this_backend_cannot_take_resolves_to_nothing(backend, key):
    """查不到就回 None ＝不帶旗標、用後端預設。**使用者輸入永遠不會原樣走到 CLI
    參數上**，這是最後一道 allowlist 驗證。"""
    assert db.dorossi_resolve_model(backend, key) is None


def test_the_legacy_tier_keys_still_migrate_on_read():
    assert db.dorossi_resolve_model("claude_code", "max") == "opus"
    assert db.dorossi_resolve_model("claude_code", "fast") == "haiku"


# ---------------------------------------------------------------------------
# 4. 講出來：吃不下的設定不准安靜失效
# ---------------------------------------------------------------------------
def test_a_session_on_another_backend_is_told_its_model_does_not_apply():
    """這是 2026-09-23 之前唯一沒有任何症狀的那一半：值存著、每輪被丟掉、沒人講。"""
    sess = {"ai_provider": "codex", "tune_model": "opus"}
    _label, _effort, unsupported = b._dorossi_tuning_labels(sess)
    assert unsupported is True


def test_a_model_this_backend_does_take_is_not_flagged():
    sess = {"ai_provider": "codex", "tune_model": "sol-5.6"}
    label, _effort, unsupported = b._dorossi_tuning_labels(sess)
    assert unsupported is False
    assert label == "sol-5.6"


def test_a_hand_edited_junk_value_is_not_reported_as_a_backend_mismatch():
    """存放檔被手改成垃圾字串照舊安靜退回預設——對那種值講「換個後端就生效」是假
    訊息，而假訊息比沒訊息更難查。"""
    sess = {"tune_model": "已經不合法了"}
    _label, _effort, unsupported = b._dorossi_tuning_labels(sess)
    assert unsupported is False


def test_no_display_label_can_ever_be_a_full_model_id():
    """保密裁定放行的是**別名**，不是完整 id，也不是廠商前綴。"""
    for sess in ({}, {"tune_model": "opus-4.8"}, {"ai_provider": "codex"},
                 {"ai_provider": "codex", "tune_model": "sol-5.6"},
                 {"tune_model": "opus"}):
        label, _effort, _unsupported = b._dorossi_tuning_labels(sess)
        assert not label.startswith(("claude", "gpt")), label
        assert label in db.DOROSSI_ALL_MODEL_CHOICES or label == "後端預設模型"


# ---------------------------------------------------------------------------
# 5. argv：使用者輸入永不原樣進 CLI 參數
# ---------------------------------------------------------------------------
def test_the_other_backends_argv_carries_the_model_flag():
    args = db._dorossi_codex_argv("codex", workdir="C:/w", model="gpt-5.6-sol",
                                  tools_mode="off")
    assert "-m" in args and args[args.index("-m") + 1] == "gpt-5.6-sol"


def test_the_other_backends_argv_omits_the_flag_when_nothing_is_set():
    args = db._dorossi_codex_argv("codex", workdir="C:/w", tools_mode="off")
    assert "-m" not in args and "--model" not in args


def test_the_model_flag_survives_a_resume():
    """續談那條路是另一個子指令（`exec resume`），它也收 `-m`。少帶的話同一段對話
    會在第二輪悄悄換回預設模型。"""
    args = db._dorossi_codex_argv("codex", session_id="t1", model="gpt-5.6-sol",
                                  tools_mode="off")
    assert args[:4] == ["codex", "exec", "resume", "--json"]
    assert "-m" in args and args[args.index("-m") + 1] == "gpt-5.6-sol"
    assert args[-2:] == ["t1", "-"]


class _FakeMessages:
    def __init__(self, seen: list):
        self._seen = seen

    async def create(self, **kwargs):
        self._seen.append(kwargs)
        block = type("_B", (), {"type": "text", "text": "ok"})()
        return type("_R", (), {"content": [block]})()


@pytest.mark.parametrize("model, expected", [
    ("claude-haiku-4-5", "claude-haiku-4-5"),
    (None, db.DOROSSI_MODEL),
])
def test_the_sdk_path_sends_the_model_it_was_given(monkeypatch, model, expected):
    """⚠️ 這條路沒有 CLI 可以打（本機沒有憑證），所以驗的是**送出去的參數**。

    2026-09-23 之前這裡寫死 `DOROSSI_MODEL`，`/model` 在這個後端上是安靜失效的：
    指令回「已更新」、送出去的卻永遠是同一個模型，而且沒有任何地方看得出來。
    """
    seen: list = []
    monkeypatch.setattr(db, "_get_dorossi_client",
                        lambda: type("_C", (), {"messages": _FakeMessages(seen)})())
    answer, _hist = _run(db._dorossi_via_api("問題", [], model=model))
    assert answer == "ok"
    assert seen and seen[0]["model"] == expected


def test_the_read_only_sandbox_is_still_reasserted_every_time():
    """加 `-m` 不可以把既有的沙箱重申弄丟——那是每次叫用都要重講一次的東西。"""
    args = db._dorossi_codex_argv("codex", workdir="C:/w", model="gpt-5.6-sol",
                                  tools_mode="off")
    assert 'sandbox_mode="read-only"' in args
    full = db._dorossi_codex_argv("codex", workdir="C:/w", model="gpt-5.6-sol",
                                  tools_mode="full")
    assert "--dangerously-bypass-approvals-and-sandbox" in full
    assert 'sandbox_mode="read-only"' not in full


# ---------------------------------------------------------------------------
# 6. 探測：讀一行就收工，而且絕不開權限
# ---------------------------------------------------------------------------
def test_the_probe_never_unlocks_tools_whatever_the_mode_is(monkeypatch):
    """⚠️ 探測刻意**不共用** `_dorossi_cc_argv`：那一支在 full 模式會帶上
    `bypassPermissions`，而一個只為了讀一行 init 就開全權限的子行程沒有任何理由存在。
    共用回去的話這支會紅。"""
    monkeypatch.setattr(db, "DOROSSI_CC_TOOLS", "full")
    args = db._dorossi_model_probe_argv("claude", "opus")
    assert "--permission-mode" not in args
    assert "bypassPermissions" not in args
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--model") + 1] == "opus"
    assert "--strict-mcp-config" in args


def test_the_probe_reads_the_resolved_model_out_of_the_init_event():
    """2026-09-23 那次實測的真實那一行（截掉不相干的欄位）。**裸別名進去、完整
    id 出來**——那正是「這一族今天最新的那個」。"""
    raw = json.dumps({"type": "system", "subtype": "init",
                      "model": "claude-opus-5-5",
                      "tools": [], "apiKeySource": "none"}).encode("utf-8")
    assert db._dorossi_model_from_init_line(raw) == "claude-opus-5-5"


@pytest.mark.parametrize("raw", [
    b"not json at all\n",
    b'{"type":"assistant","message":{"model":"claude-opus-5-5"}}\n',
    b'{"type":"system","subtype":"init"}\n',
    b'{"type":"system","subtype":"init","model":"   "}\n',
    b"[1,2,3]\n",
])
def test_only_the_init_event_counts(raw):
    """`assistant` 事件也帶 model，但它在**請求之後**——讀它就等於每天白跑一整輪。"""
    assert db._dorossi_model_from_init_line(raw) is None


def test_the_other_backends_header_line_is_read_off_stderr_shape():
    """⚠️ 表頭印在 **stderr**，而且 CLI 是先把 stdin 讀完才印的。兩件事各讓第一版
    白跑一次（一次讀錯串流、一次等到逾時），所以真實的那一行釘在這裡。"""
    assert db._dorossi_model_from_header_line(b"model: gpt-5.6-sol\n") == "gpt-5.6-sol"
    for other in (b"provider: openai\n", b"workdir: C:\\x\n", b"--------\n",
                  b"reasoning summaries: none\n", b"model:\n"):
        assert db._dorossi_model_from_header_line(other) is None


# ---------------------------------------------------------------------------
# 7. 節流與公告
# ---------------------------------------------------------------------------
def test_the_check_is_due_when_nothing_was_ever_checked():
    assert db.dorossi_model_catalog_due(24.0) is True


def test_the_check_is_not_due_again_inside_the_interval(monkeypatch):
    monkeypatch.setattr(db, "_DOROSSI_MODEL_CATALOG", {"checked_at": 1000.0})
    assert db.dorossi_model_catalog_due(24.0, now=1000.0 + 3600.0) is False
    assert db.dorossi_model_catalog_due(24.0, now=1000.0 + 24 * 3600.0) is True


def test_a_timestamp_from_the_future_does_not_switch_the_check_off(monkeypatch):
    """時鐘被調過、或目錄檔從別台機器複製過來。一個未來的時刻會把這個檢查**永久**
    關掉，而且沒有任何症狀。"""
    monkeypatch.setattr(db, "_DOROSSI_MODEL_CATALOG",
                        {"checked_at": 9e12})
    assert db.dorossi_model_catalog_due(24.0, now=1000.0) is True


def test_a_nan_timestamp_does_not_switch_the_check_off(monkeypatch):
    """目錄檔被手改成 `NaN`：`json.loads` 照收，而 NaN 跟任何數字比都是 False，
    上面那道「未來時刻」的檢查也攔不到它。"""
    import json
    stored = json.loads('{"checked_at": NaN}')
    monkeypatch.setattr(db, "_DOROSSI_MODEL_CATALOG", stored)
    assert db.dorossi_model_catalog_due(24.0, now=1000.0) is True


def test_a_probe_that_raises_still_waits_a_full_interval(monkeypatch):
    """探測本身丟例外（建探測目錄或起子行程失敗）：昨天的結果要留著，而且**時刻要蓋上**。

    不蓋的話節流永遠說「到期了」，每分鐘的健康迴圈就每分鐘重試一次。"""
    previous = {"checked_at": 1.0,
                "resolved": {"claude": {"opus": "claude-opus-5-5"}}}
    monkeypatch.setattr(db, "_DOROSSI_MODEL_CATALOG", previous)

    async def _spawn_fails(_timeout=None):
        raise FileNotFoundError("gone")
    monkeypatch.setattr(db, "dorossi_probe_model_catalog", _spawn_fails)
    _run(db.dorossi_refresh_model_catalog())
    stored = json.loads(db.DOROSSI_MODEL_CATALOG_FILE.read_text(encoding="utf-8"))
    assert stored["resolved"] == previous["resolved"]
    assert db.dorossi_model_catalog_due(24.0) is False


def test_the_switch_really_switches_it_off(monkeypatch):
    monkeypatch.setattr(b, "DOROSSI_MODEL_CHECK", {"enabled": False})
    monkeypatch.setattr(b, "dorossi_refresh_model_catalog", _boom)
    assert _run(b._dorossi_model_check_tick()) == ""


def test_nothing_is_announced_when_nothing_is_new(monkeypatch):
    monkeypatch.setattr(b, "DOROSSI_MODEL_CHECK",
                        {"enabled": True, "interval_hours": 24.0})
    monkeypatch.setattr(b, "dorossi_model_catalog_due", lambda *_a, **_k: True)

    async def _nothing():
        return []
    monkeypatch.setattr(b, "dorossi_refresh_model_catalog", _nothing)
    assert _run(b._dorossi_model_check_tick()) == ""


def test_a_newly_merged_alias_is_immediately_usable_and_announceable():
    """⚠️ **這支釘的是一個真的發生過的缺陷（2026-09-23，第一次端對端跑完當場抓到）。**

    聯集表第一版是 import 時算一次的快照，所以每日檢查在執行期併進來的新別名**不在
    聯集裡**。後果有兩個，而且兩個都不會丟例外：公告以「不在聯集裡」為由把剛發現的
    別名整筆濾掉（實測兩個新別名一個都沒公告），以及 token 路徑
    `@bot /model <新別名>` 把它當成非法值退回——選單裡選得到、打出來卻不認得。
    """
    added = db.dorossi_merge_model_catalog(
        {"resolved": {"claude": {"opus": "claude-opus-5-5"}}})
    assert added == ["opus-5.5"]
    assert "opus-5.5" in db.DOROSSI_ALL_MODEL_CHOICES, (
        "新別名沒有進聯集——token 路徑會把它當成非法值退回")
    assert b._model_alias_for("claude-opus-5-5") == "opus-5.5", (
        "反查不回新別名——公告會把它濾掉，顯示端會講成「後端預設模型」")
    _effort, key, _sid, rest, errors = db._dorossi_parse_turn_flags(
        "/model opus-5.5 請問")
    assert (key, rest, errors) == ("opus-5.5", "請問", [])


def test_the_union_is_updated_in_place_not_replaced():
    """`discord_bot` 是以名字 import 聯集的，重新指派一個新 dict 只會換掉
    `dorossi_backend` 這邊的名字，bot 那邊仍然抓著舊的那一份、永遠不會更新。"""
    before = db.DOROSSI_ALL_MODEL_CHOICES
    db.dorossi_merge_model_catalog(
        {"resolved": {"claude": {"opus": "claude-opus-5-5"}}})
    assert db.DOROSSI_ALL_MODEL_CHOICES is before


def test_a_new_model_is_announced_by_alias_only(monkeypatch):
    """公告是一個**新的送出點**，所以它受同一條保密規則管：只准出現別名。"""
    monkeypatch.setattr(b, "DOROSSI_MODEL_CHECK",
                        {"enabled": True, "interval_hours": 24.0})
    monkeypatch.setattr(b, "dorossi_model_catalog_due", lambda *_a, **_k: True)

    # 第一個是表裡本來就有的別名，第二個是完整 id（上游若改壞了可能回這種東西）。
    async def _found():
        return ["opus-4.8", "claude-opus-5-5"]
    monkeypatch.setattr(b, "dorossi_refresh_model_catalog", _found)
    text = _run(b._dorossi_model_check_tick())
    assert "opus-4.8" in text
    # 上游就算回了一個完整 id，這裡也把它擋下來——`_model_alias_for` 反查不到表裡的
    # 別名就降級，而降級的值不在表裡，於是整筆被丟掉。
    assert "claude-opus-5-5" not in text
    assert "後端預設模型" not in text


def test_the_check_is_throttled_by_the_configured_interval(monkeypatch):
    """`interval_hours` 真的有被讀進去——寫死 24 小時的話這支會紅。"""
    seen: list = []
    monkeypatch.setattr(b, "DOROSSI_MODEL_CHECK",
                        {"enabled": True, "interval_hours": 6.0})
    monkeypatch.setattr(b, "dorossi_model_catalog_due",
                        lambda hours, *_a, **_k: seen.append(hours) or False)
    assert _run(b._dorossi_model_check_tick()) == ""
    assert seen == [6.0]


def test_a_slow_check_does_not_hold_up_the_health_loop(monkeypatch):
    """探測逾時時會跑好幾分鐘。健康迴圈若直接 await 它，同一輪後面的事（日報）就跟著停。

    用一個永遠不回來的檢查，確認迴圈照樣走到日報那一步，而且檢查真的有起來。"""
    reached: list = []

    class _Report(dict):
        def get(self, key, default=None):
            reached.append(key)
            return False

    async def _body():
        began = asyncio.Event()

        async def _hangs():
            began.set()
            await asyncio.Event().wait()
            return ""

        monkeypatch.setattr(b, "_dorossi_model_check_tick", _hangs)
        monkeypatch.setattr(b, "_dorossi_model_check_task", None)
        monkeypatch.setattr(b, "_rotate_periodic_ndjson_logs", lambda: None)

        async def _no_drift():
            return None
        monkeypatch.setattr(b, "_check_code_drift", _no_drift)
        monkeypatch.setattr(b, "_sweep_stale_inflight_serve", lambda: None)
        monkeypatch.setattr(b, "DAILY_HEALTH_REPORT", _Report())
        try:
            await asyncio.wait_for(b._daily_health_loop(), timeout=1.0)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        return began.is_set()

    assert asyncio.run(_body()) is True
    assert "enabled" in reached, "健康迴圈卡在模型檢查上，沒有走到日報那一步"


def test_a_check_still_running_is_not_started_twice(monkeypatch):
    calls: list = []

    async def _body():
        async def _hangs():
            calls.append(1)
            await asyncio.Event().wait()
            return ""

        monkeypatch.setattr(b, "_dorossi_model_check_tick", _hangs)
        monkeypatch.setattr(b, "_dorossi_model_check_task", None)
        b._start_dorossi_model_check()
        await asyncio.sleep(0)
        b._start_dorossi_model_check()
        await asyncio.sleep(0)
        b._dorossi_model_check_task.cancel()

    asyncio.run(_body())
    assert calls == [1]


class _FlakyChannel:
    def __init__(self, failures):
        self.failures = list(failures)
        self.sent: list = []

    async def send(self, text):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(text)


def _announce_env(monkeypatch, channel, announcements):
    queue = list(announcements)

    async def _tick():
        return queue.pop(0) if queue else ""

    monkeypatch.setattr(b, "_dorossi_model_check_tick", _tick)
    monkeypatch.setattr(b, "_dorossi_model_announce_pending", [])
    monkeypatch.setattr(b.client, "get_channel", lambda _cid: channel)


def test_an_announcement_that_failed_to_send_is_sent_on_the_next_tick(monkeypatch):
    """新別名在公告**之前**就落地了，下一次檢查不會再發現它們——送不出去那一次若不留著，
    這則公告就永遠不會有人看到。"""
    channel = _FlakyChannel([RuntimeError("gateway hiccup")])
    _announce_env(monkeypatch, channel, ["🆕 A"])
    _run(b._dorossi_model_check_and_announce())
    assert channel.sent == []
    _run(b._dorossi_model_check_and_announce())
    assert channel.sent == ["🆕 A"]
    _run(b._dorossi_model_check_and_announce())
    assert channel.sent == ["🆕 A"], "送出去之後不該再送一次"


def test_an_unreachable_channel_keeps_the_announcement(monkeypatch, capsys):
    """頻道拿不到時留著，而且在 log 裡說**一次**——每分鐘都會走到這裡，一個設錯的頻道
    原本會讓公告永遠停在記憶體裡、log 裡什麼都沒有。送出去之後也要留一行，否則「送出去
    了」與「還卡著」在 log 裡分不出來。"""
    channel = _FlakyChannel([])
    _announce_env(monkeypatch, None, ["🆕 A"])
    _run(b._dorossi_model_check_and_announce())
    _run(b._dorossi_model_check_and_announce())
    err = capsys.readouterr().err
    assert err.count("model announcement waiting") == 1, err
    monkeypatch.setattr(b.client, "get_channel", lambda _cid: channel)
    _run(b._dorossi_model_check_and_announce())
    assert channel.sent == ["🆕 A"]
    assert "model announcement sent (1 item(s))" in capsys.readouterr().err


def test_a_failed_send_does_not_claim_it_was_sent(monkeypatch, capsys):
    channel = _FlakyChannel([RuntimeError("gateway hiccup")])
    _announce_env(monkeypatch, channel, ["🆕 A"])
    _run(b._dorossi_model_check_and_announce())
    err = capsys.readouterr().err
    assert "will retry" in err and "announcement sent" not in err, err


def test_a_forbidden_channel_drops_the_announcement(monkeypatch):
    """權限不足是永久的：留著只會每分鐘印同一行。"""
    import types as _types
    import discord
    denied = discord.Forbidden(_types.SimpleNamespace(status=403, reason="x"), "m")
    channel = _FlakyChannel([denied])
    _announce_env(monkeypatch, channel, ["🆕 A"])
    _run(b._dorossi_model_check_and_announce())
    assert b._dorossi_model_announce_pending == []


async def _boom(*_args, **_kwargs):
    raise AssertionError("關掉了還去探測")


# ---------------------------------------------------------------------------
# 探測本身：真的起一個子行程（一支假的 CLI），讀到答案就砍掉
# ---------------------------------------------------------------------------
# 兩支探測的「怎麼判斷那一行」早就有測試（`_dorossi_model_from_init_line` ／
# `_dorossi_model_from_header_line`），但「起行程、寫 stdin、逐行讀、讀到就砍、
# 讀不到就逾時」那一層在整套測試裡一次都沒跑過（2026-09-24 單行程覆蓋率：
# 各 34 行裡 33 行沒跑）——而那正是昨天在正式 bot 上真的跑了、找到新模型的那一段。
# 假 CLI 是 `sys.executable` 跑的小腳本，不碰網路、不花錢。

_INIT = json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-9-9"})


def _fake_claude(monkeypatch, script: str) -> None:
    import os
    monkeypatch.setattr(db, "_dorossi_model_probe_argv",
                        lambda exe, family: [sys.executable, "-c", script])
    monkeypatch.setattr(db, "_dorossi_cc_child_env",
                        lambda _timeout: {**os.environ, "PYTHONIOENCODING": "utf-8"})


def _timed(coro):
    import time as _time
    started = _time.monotonic()
    result = asyncio.run(asyncio.wait_for(coro, 60))
    return result, _time.monotonic() - started


def test_the_family_probe_returns_the_model_and_does_not_wait_for_the_cli(
        monkeypatch, tmp_path):
    """init 事件在請求之前就到，讀到就收工：假 CLI 之後睡一分鐘，探測不得等它。前面那幾行
    不是 init（雜訊、別的事件），要跳過繼續讀。"""
    _fake_claude(monkeypatch, (
        "import sys, time\n"
        "sys.stdin.readline()\n"
        "print('warming up', flush=True)\n"
        "print('{\"type\": \"system\", \"subtype\": \"hook\"}', flush=True)\n"
        f"print({_INIT!r}, flush=True)\n"
        "time.sleep(60)\n"))
    model, elapsed = _timed(db._dorossi_probe_claude_family(
        "claude", "opus", str(tmp_path), 30.0))
    assert model == "claude-opus-9-9"
    assert elapsed < 20, f"探測等到假 CLI 自己結束才回來（{elapsed:.1f}s）"


@pytest.mark.parametrize("script", [
    "import sys\nsys.stdin.readline()\nprint('no init here', flush=True)\n",
    "import sys\nsys.exit(3)\n",
], ids=["no-init", "crash"])
def test_the_family_probe_gives_up_quietly_when_there_is_no_init(monkeypatch, tmp_path,
                                                                 script):
    _fake_claude(monkeypatch, script)
    model, _elapsed = _timed(db._dorossi_probe_claude_family(
        "claude", "opus", str(tmp_path), 30.0))
    assert model is None


def test_a_family_probe_that_hears_nothing_times_out(monkeypatch, tmp_path, capsys):
    """什麼都不印的 CLI（卡在登入、卡在網路）：到點就放棄，不會把每日檢查整個卡住。"""
    _fake_claude(monkeypatch, "import time\ntime.sleep(60)\n")
    model, elapsed = _timed(db._dorossi_probe_claude_family(
        "claude", "opus", str(tmp_path), 1.5))
    assert model is None and elapsed < 20, elapsed
    assert "timed out" in capsys.readouterr().err


def test_the_codex_probe_reads_the_header_from_stderr(tmp_path):
    """另一個 CLI 把表頭印在 **stderr**、而且要先讀完 stdin 才印——兩件都是實測才知道的，
    寫錯任何一件探測就一路等到逾時。探測跑的是 `[exe, "exec", …]`、工作目錄是 `cwd`，
    所以用 `sys.executable` 當 exe、在 cwd 放一支叫 `exec` 的腳本，就是真的那條起行程的路。"""
    (tmp_path / "exec").write_text(
        "import sys, time\n"
        "sys.stdin.read()\n"
        "print('answer on stdout', flush=True)\n"
        "sys.stderr.write('workdir: x\\nmodel: gpt-9.9-sol\\n'); sys.stderr.flush()\n"
        "time.sleep(60)\n", encoding="utf-8")
    model, elapsed = _timed(db._dorossi_probe_codex_default(
        sys.executable, str(tmp_path), 30.0))
    assert model == "gpt-9.9-sol"
    assert elapsed < 20, elapsed


def test_the_catalog_probe_asks_each_family_only_without_the_api_route(monkeypatch):
    """有 API 那條路就不必起 CLI；沒有才逐族問。另一個後端的答案按族名歸位，讀不到就不寫。"""
    asked: list = []

    async def _probe_family(exe, family, cwd, timeout):
        asked.append(family)
        return f"claude-{family}-9-9"

    async def _codex(exe, cwd, timeout):
        return "gpt-9.9-sol"

    monkeypatch.setattr(db, "_dorossi_probe_claude_family", _probe_family)
    monkeypatch.setattr(db, "_dorossi_probe_codex_default", _codex)
    monkeypatch.setattr(db, "_dorossi_model_probe_dir", lambda: Path("."))
    monkeypatch.setattr(db._shutil, "which", lambda name: "claude.exe")
    monkeypatch.setattr(db, "find_codex_executable", lambda: "codex.exe")

    async def _api_empty(timeout):
        return {}

    monkeypatch.setattr(db, "_dorossi_probe_api_catalog", _api_empty)
    got = asyncio.run(db.dorossi_probe_model_catalog(5.0))
    assert asked == list(db.DOROSSI_MODEL_PROBE_FAMILIES)
    assert got["claude"] == {f: f"claude-{f}-9-9" for f in db.DOROSSI_MODEL_PROBE_FAMILIES}
    assert got["codex"] == {"sol": "gpt-9.9-sol"}

    asked.clear()

    async def _api_full(timeout):
        return {"opus": "claude-opus-9-9"}

    monkeypatch.setattr(db, "_dorossi_probe_api_catalog", _api_full)
    monkeypatch.setattr(db, "find_codex_executable", lambda: None)
    got = asyncio.run(db.dorossi_probe_model_catalog(5.0))
    assert asked == [] and got == {"claude": {"opus": "claude-opus-9-9"}}


@pytest.mark.parametrize("override_shape", ["plain", "single-quoted", "double-quoted"])
def test_the_codex_override_wins_even_when_quoted(monkeypatch, tmp_path, override_shape):
    """`CODEX_CLI_PATH` 是操作者明確指定的那一支，排在 PATH 前面。從 shell 設的值常常帶著
    引號——2026-09-10 以前單引號版本會安靜地被忽略、退回 PATH。"""
    exe = tmp_path / "my codex.exe"
    exe.write_bytes(b"")
    other = tmp_path / "other.exe"
    other.write_bytes(b"")
    value = {"plain": str(exe), "single-quoted": f"'{exe}'",
             "double-quoted": f'"{exe}"'}[override_shape]
    monkeypatch.setenv("CODEX_CLI_PATH", value)
    monkeypatch.setattr(db._shutil, "which", lambda name: str(other))
    assert db.find_codex_executable() == str(exe.resolve())


def test_a_codex_override_that_is_not_a_file_falls_back_to_path(monkeypatch, tmp_path):
    """指到不存在的檔或一個資料夾：不回那個值（回了等於叫起行程時才爆），照順序往下找。"""
    found = tmp_path / "codex.exe"
    found.write_bytes(b"")
    monkeypatch.setattr(db._shutil, "which", lambda name: str(found))
    for bad in (str(tmp_path / "gone.exe"), str(tmp_path)):
        monkeypatch.setenv("CODEX_CLI_PATH", bad)
        assert db.find_codex_executable() == str(found.resolve()), bad


def test_codex_is_found_in_the_desktop_install_when_path_is_stale(monkeypatch, tmp_path):
    """長命的監督行程拿的是開機時的 PATH；桌面安裝版在 LocalAppData 底下，最後才找。"""
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setattr(db._shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    if db.os.name != "nt":
        assert db.find_codex_executable() is None
        return
    assert db.find_codex_executable() is None, "安裝位置還沒有檔案"
    exe = tmp_path / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert db.find_codex_executable() == str(exe.resolve())


class _Model:
    def __init__(self, model_id):
        self.id = model_id


def _api_client(monkeypatch, *, ids=None, error=None):
    class _Models:
        async def list(self, limit):
            if error is not None:
                raise error
            return type("Page", (), {"data": [_Model(i) for i in ids or []]})()

    monkeypatch.setattr(db, "_dorossi_api_credentials_present", lambda: True)
    monkeypatch.setattr(db, "_get_dorossi_client",
                        lambda: type("Client", (), {"models": _Models()})())


def test_the_api_catalog_keeps_the_newest_of_each_known_family(monkeypatch):
    """清單是新的在前：每一族取第一筆；內建表沒有的族（別的產品線）不收。"""
    families = list(db.DOROSSI_MODEL_PROBE_FAMILIES)
    first = families[0]
    ids = [f"claude-{first}-9-9", f"claude-{first}-9-1", "claude-unknownfam-1-0",
           "not-a-model-id", None]
    _api_client(monkeypatch, ids=ids)
    got = asyncio.run(db._dorossi_probe_api_catalog(5.0))
    assert got == {first: f"claude-{first}-9-9"}, got


@pytest.mark.parametrize("case", ["no-credentials", "no-client", "api-error"])
def test_the_api_catalog_steps_aside_so_the_cli_probe_can_run(monkeypatch, capsys, case):
    """沒憑證、SDK 不在、清單拿不到——都回空字典，呼叫端才會退回 CLI 探測。失敗的原因只印
    例外的型別（訊息可能帶金鑰前綴或網址）。"""
    _api_client(monkeypatch, ids=["claude-opus-9-9"],
                error=RuntimeError("key sk-secret rejected") if case == "api-error" else None)
    if case == "no-credentials":
        monkeypatch.setattr(db, "_dorossi_api_credentials_present", lambda: False)
    if case == "no-client":
        monkeypatch.setattr(db, "_get_dorossi_client", lambda: None)
    assert asyncio.run(db._dorossi_probe_api_catalog(5.0)) == {}
    err = capsys.readouterr().err
    assert "sk-secret" not in err
    if case == "api-error":
        assert "RuntimeError" in err
