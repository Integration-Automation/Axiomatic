"""一個平台一個行程：狀態的分隔、批次監督權的歸屬、關掉一個平台會怎樣。

這一批守的是**行程模型**，不是某一個平台的細節（那一批在
`test_platform_transports.py`）。四件事：

1. **每個平台的狀態都在自己的目錄裡，而且「哪些檔案逐平台」是列冊的。**
   漏掉一個的症狀是兩個行程寫同一個檔——而那不會當掉，只會讓其中一個行程的工作
   階段、佇列或稽核紀錄被另一個安靜地蓋掉。所以這裡兩個方向都對帳：新增一個根目錄
   狀態檔就得選邊站（逐平台／刻意共用），而刻意共用那份清單裡的每一筆都要寫理由。
2. **批次只有一份，由鎖決定誰監督。** 沒拿到鎖的行程對批次控制指令**讓位**，而不是
   spawn 第二個監督者、也不是回報失敗——那兩種都是真的發生過的形狀（兩套監督者互相
   終止、互相重生，而兩邊的紀錄看起來都正常）。
3. **關掉一個平台就是關掉。** 設定裡 `enabled: false`、或憑證檔是空的，那個平台就
   不起行程、不註冊排程工作，而且**兩者的原因說得出來**——「開著卻沒生效」與「根本
   沒設定」從外面看一模一樣，這正是本 repo 一再點名的形狀。
4. **不合法的平台名不得變成路徑。** 平台名直接構成目錄與檔名，所以 `..`、路徑分隔
   符號、空白都要在唯一的正規化點被擋掉。

這裡不起任何真的行程、不連網路、也不碰真的排程器。
"""
import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _platform_runtime as pr  # noqa: E402
import _supervisor as sv  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PKG_ROOT.parent


# ---------------------------------------------------------------------------
# 平台名 → 路徑
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("given", [
    "..", "../../etc", "a/b", "a\\b", "", "   ", None, "Discord ", "TELEGRAM",
    "x" * 80, "9leading", "-leading",
])
def test_a_platform_name_can_never_become_a_path(given):
    """平台名直接構成目錄與檔名，所以**唯一**的正規化點要擋掉所有路徑形狀。

    大小寫與前後空白是順便收的（`Discord ` → `discord`）：不收的話同一個平台會
    因為設定檔裡多打一個空白而拿到**第二組**狀態檔，而兩組都「看起來正常」。
    """
    name = pr.normalise_platform(given)
    assert name in {pr.DEFAULT_PLATFORM, "telegram"} or name.isalnum(), name
    assert "/" not in name and "\\" not in name and ".." not in name
    path = pr.platform_file(REPO_ROOT / "x.json", platform=given)
    assert REPO_ROOT.resolve() in path.resolve().parents, path


def test_two_platforms_never_share_a_state_file():
    """同一個基底檔名，兩個平台必須拿到兩個不同的路徑——這是整個模型的前提。"""
    a = pr.platform_file(REPO_ROOT / "dorossi_session.json", platform="discord")
    b = pr.platform_file(REPO_ROOT / "dorossi_session.json", platform="telegram")
    assert a != b
    assert a.parent != b.parent, "只有檔名不同的話，一個 rmtree 會清掉兩個平台"


def test_every_platform_file_carries_its_platform_name():
    """目錄與檔名**都**帶平台名，是刻意的重複。

    目錄保證兩個行程不會寫到同一個檔；檔名保證一個被複製到別處的檔案仍然說得出
    自己屬於誰（備份、貼進 issue、丟給別人看的時候）。
    """
    for platform in ("discord", "telegram"):
        path = pr.platform_file(REPO_ROOT / "audit.ndjson", platform=platform)
        assert path.parent.name == platform
        assert path.name.startswith(platform + ".")


def test_a_dotted_base_name_does_not_grow_a_double_dot():
    """鎖檔是點開頭的。`discord..discord_bot.lock` 能用，但一看就像壞掉了，
    而看起來壞掉的東西會被人「修」。"""
    path = pr.platform_file(REPO_ROOT / ".discord_bot.lock", platform="discord")
    assert ".." not in path.name, path.name
    assert path.name.startswith(".discord.")


def test_computing_a_platform_path_does_not_touch_the_disk(tmp_path, monkeypatch):
    """`platform_file()` 是純路徑計算。

    在 import 期建目錄的話，本專案那道「測試不得寫進 repo」的夾具會對一堆無辜的
    測試開火——而會亂叫的守門會被人關掉。目錄由行程的 `main()` 用
    `ensure_state_dir()` 建一次。
    """
    monkeypatch.setattr(pr, "STATE_ROOT", tmp_path / "state")
    path = pr.platform_file(REPO_ROOT / "x.json", platform="telegram")
    assert not path.parent.exists(), "算一個路徑就把目錄建出來了"
    assert pr.ensure_state_dir("telegram").is_dir()
    assert path.parent.exists()


# ---------------------------------------------------------------------------
# 哪些根目錄狀態檔是逐平台的：兩個方向都對帳
# ---------------------------------------------------------------------------
# **刻意共用**的根目錄常數，每一筆寫理由。這份清單是 fail-closed 的那一半：新增一個
# `PROJECT_ROOT / "…"` 而沒有包進 `_platform_state(...)` 的常數，就必須在這裡表態。
#
# 判準只有一條：**這個檔案是不是 bot 這個行程自己的狀態？** 是 → 逐平台。不是（批次
# 的磁碟契約、使用者自己的內容、憑證、程式檔）→ 共用，而且共用是對的：批次全機只有
# 一份，把 `webrunner.pid` 或 `todo_prompt.md` 逐平台化會當場拆掉 bot↔webrunner 的
# 磁碟契約。
_DELIBERATELY_SHARED = {
    "TOKEN_FILE": "憑證，使用者自己放的檔案（逐平台的憑證檔由各自的 transport 宣告）",
    "BATCH_SUPERVISOR_LOCK_FILE": "批次監督權——**全機一把就是它的重點**，見下面那一節",
    "PROMPT_FILE": "使用者內容：佇列空時的 fallback 提示詞",
    "DEFAULT_PROMPT_FILE": "使用者內容：主提示詞範本",
    "CHARACTER1_FILE": "使用者內容：角色 1 的 fallback",
    "CHARACTER2_FILE": "使用者內容：角色 2 的 fallback",
    "UNDESIRED_FILE": "使用者內容：負面提示詞的 fallback",
    "TODO_PROMPT_FILE": "批次佇列，bot 寫、webrunner 讀（磁碟契約）",
    "TODO_FILE_1": "同上，批次佇列的磁碟契約",
    "TODO_FILE_2": "同上（而且是位置性的，兩份會讓配對整個錯開）",
    "TODO_UNDESIRED_FILE": "同上，批次佇列的磁碟契約",
    "TEMPLATES_DIR": "使用者自己放的提示詞範本，bot 只讀",
    "LAUNCHER_SCRIPT": "程式檔，不是狀態",
    "WEBRUNNER_LOG": "批次的記錄檔（一次批次一份，不是一個平台一份）",
    "WEBRUNNER_LOG_PREV": "同上，批次上一輪的記錄檔",
    "WEBRUNNER_PID_FILE": "批次的 pid 檔（磁碟契約）",
    "EVENTS_FILE": "webrunner 寫、bot 讀的事件串流（磁碟契約）",
    "DOM_REQUEST_FILE": "bot 寫、webrunner 讀的請求檔（磁碟契約）",
    "SINGLE_IMAGE_REQUEST_FILE": "同上；而且它是**單槽**的，兩份會讓兩個平台各排一張",
    "WEBRUNNER_PAUSE_FILE": "批次的暫停旗標（磁碟契約）",
    "BATCH_LABEL_FILE": "這一輪批次的標籤（磁碟契約）",
    "OUTPUT_ROOT": "批次的輸出目錄，全機一份",
    "BACKUP_DIR": "`/sys undo` 的備份堆疊，備份的**就是**上面那幾份共用的佇列檔",
    "CHROME_PROFILE_DIR": "登入 session，全機一份（兩份會讓兩套瀏覽器互搶）",
}

_STATE_MODULES = ("discord_bot.py", "dorossi_backend.py")
_ROOT_NAMES = {"PROJECT_ROOT", "_PROJECT_ROOT"}


def _root_state_constants(sources=None) -> tuple[set[str], set[str]]:
    """回 `(逐平台的常數名, 直接落在根目錄的常數名)`。

    `sources` 可換，是為了讓抽取器自己有合成對照組——真實語料兩邊都非空，所以把
    分類邏輯改成「永遠算成逐平台」在真實資料上仍然全綠。
    """
    paths = ([PKG_ROOT / name for name in _STATE_MODULES]
             if sources is None else list(sources))
    per_platform: set[str] = set()
    shared: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id.isupper()):
                continue
            name = node.targets[0].id
            value = node.value
            wrapped = False
            if (isinstance(value, ast.Call) and len(value.args) == 1
                    and not value.keywords):
                value, wrapped = value.args[0], True
            if not (isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div)
                    and isinstance(value.left, ast.Name)
                    and value.left.id in _ROOT_NAMES
                    and isinstance(value.right, ast.Constant)
                    and isinstance(value.right.value, str)):
                continue
            (per_platform if wrapped else shared).add(name)
    return per_platform, shared


def test_the_state_constant_extractor_tells_the_two_shapes_apart(tmp_path):
    """合成對照：包了一層呼叫的算逐平台，裸的算共用，其他形狀兩邊都不收。"""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "A = _platform_state(PROJECT_ROOT / 'a.json')\n"
        "B = PROJECT_ROOT / 'b.json'\n"
        "C = PROJECT_ROOT / 'pkg' / 'c.py'\n"
        "D = SOMEWHERE_ELSE / 'd.json'\n"
        "e = PROJECT_ROOT / 'e.json'\n",
        encoding="utf-8")
    per_platform, shared = _root_state_constants([probe])
    assert per_platform == {"A"}, per_platform
    assert shared == {"B"}, shared


def test_every_root_state_constant_picks_a_side():
    """新增一個根目錄狀態檔就必須表態：逐平台，或寫進 `_DELIBERATELY_SHARED`。

    漏掉的症狀**不是當掉**：兩個平台的行程會寫同一個檔，其中一個的工作階段、佇列或
    稽核紀錄被另一個安靜地蓋掉，而兩邊看起來都正常。這是 fail-closed 的那一半。
    """
    per_platform, shared = _root_state_constants()
    # 正面對照：抽取器壞掉回空集合時，「零筆未分類」跟「全部分類好」長得一樣。
    assert len(per_platform) >= 10, per_platform
    assert len(shared) >= 15, shared
    unclassified = sorted(shared - set(_DELIBERATELY_SHARED))
    assert not unclassified, (
        f"這些根目錄常數沒有表態：{unclassified}。它是 bot 這個行程自己的狀態嗎？"
        "是 → 包成 `_platform_state(PROJECT_ROOT / \"…\")`；不是 → 加進 "
        "`_DELIBERATELY_SHARED` 並寫下理由。")
    assert not (per_platform & set(_DELIBERATELY_SHARED)), (
        "同一個常數同時被算成逐平台與刻意共用")


def test_the_shared_list_has_no_stale_entry():
    """反方向：一個常數改名或改成逐平台之後，它在清單裡那一筆就永遠對不上任何東西
    ——而清單看起來仍然有人在管（`_OWNER_ONLY_SLASH` 同一個形狀）。"""
    _per_platform, shared = _root_state_constants()
    stale = sorted(set(_DELIBERATELY_SHARED) - shared)
    assert not stale, f"`_DELIBERATELY_SHARED` 列著已經不存在的常數：{stale}"
    for name, reason in _DELIBERATELY_SHARED.items():
        assert len(reason.strip()) >= 6, f"{name} 的理由太短"


def test_the_bot_state_files_really_land_under_the_platform_directory():
    """真實資料：import 之後那些常數的路徑真的在 `state/<平台>/` 底下。

    上面那支只看**寫法**。寫法對、`platform_file()` 卻回一個根目錄路徑的話，上面
    那支照樣綠——所以這裡量的是算出來的值。
    """
    import discord_bot as b  # noqa: PLC0415
    import dorossi_backend as db  # noqa: PLC0415

    # `conftest._SIDE_EFFECT_LOGS` 那幾個常數被一道 autouse 夾具導到暫存目錄了
    # （正式程式碼會「順手」append 它們，測試不能寫進 repo）。在這裡讀到的是暫存
    # 路徑，所以改成直接從 `_platform_runtime` 重算——量的仍然是同一件事，只是繞過
    # 那道夾具。**不是豁免**：下面照樣逐個檔名比對。
    import conftest  # noqa: PLC0415

    redirected = {attr for _mod, attr in conftest._SIDE_EFFECT_LOGS}
    per_platform, _shared = _root_state_constants()
    state_root = (REPO_ROOT / "state").resolve()
    checked = 0
    for module in (b, db):
        for name in per_platform:
            value = getattr(module, name, None)
            if value is None:
                continue
            if name in redirected:
                value = pr.platform_file(REPO_ROOT / Path(value).name,
                                         platform=b.ACTIVE_PLATFORM)
            checked += 1
            resolved = Path(value).resolve()
            assert state_root in resolved.parents, f"{name} 不在 state/ 底下：{value}"
            assert b.ACTIVE_PLATFORM in resolved.parts, name
    assert checked >= 10, f"只量到 {checked} 個常數"


def test_the_batch_disk_contract_stays_at_the_repo_root():
    """反方向的真實資料：批次那幾份**不得**被逐平台化。

    把 `webrunner.pid` 或佇列檔搬進 `state/<平台>/` 會當場拆掉 bot↔webrunner 的磁碟
    契約，而症狀是批次「啟動了但什麼都沒做」——webrunner 讀的是另一個路徑。
    """
    import discord_bot as b  # noqa: PLC0415

    root = Path(b.PROJECT_ROOT).resolve()
    # `EVENTS_FILE` 被 `conftest` 的 autouse 夾具導到暫存目錄了（見上一支的註解），
    # 所以這裡列的是**寫法**看得到的那幾個；它自己的位置由上面那支的重算涵蓋。
    for name in ("WEBRUNNER_PID_FILE", "TODO_PROMPT_FILE",
                 "SINGLE_IMAGE_REQUEST_FILE", "WEBRUNNER_PAUSE_FILE",
                 "OUTPUT_ROOT", "BATCH_SUPERVISOR_LOCK_FILE"):
        assert Path(getattr(b, name)).resolve().parent == root, name


# ---------------------------------------------------------------------------
# 批次監督權：一把鎖，誰拿到誰監督
# ---------------------------------------------------------------------------
def test_a_second_process_does_not_get_the_batch_supervision_lock(tmp_path,
                                                                  monkeypatch):
    """鎖被別人握著時，第二個行程**拿不到**，而且它自己知道。

    這一支量的是鎖本身（不 import bot）：`acquire_single_instance_lock` 回 `None`
    專指「已經被別人持有」，而那正是 `_claim_batch_supervision()` 的判準。
    """
    lock_file = tmp_path / ".batch_supervisor.lock"
    first = sv.acquire_single_instance_lock(lock_file)
    assert first is not None
    if first.degraded:
        first.release()
        pytest.skip("這台機器拿不到檔案鎖，測不出互斥")
    try:
        assert sv.acquire_single_instance_lock(lock_file) is None, (
            "第二個行程也拿到了批次監督權——兩套監督者會互相終止、互相重生")
    finally:
        first.release()
    # 放掉之後下一個行程拿得到：鎖由 OS 持有，行程一消失就釋放，所以不需要任何
    # 清理或逾時，也不會有「監督者當掉了但旗標還在」的狀態。
    again = sv.acquire_single_instance_lock(lock_file)
    assert again is not None
    again.release()


def _bot_with_batch_claim(monkeypatch, claimed: bool):
    import discord_bot as b  # noqa: PLC0415

    monkeypatch.setattr(b, "batch_supervision_claimed", lambda: claimed)
    return b


class _Recorder:
    """收下每一則送出的訊息。`send` 與 `safe_reply` 兩條路都用得到。"""

    def __init__(self):
        self.sent: list = []

    async def send(self, content=None, **_kw):
        self.sent.append(content)

    async def reply(self, _message, content=None, **_kw):
        self.sent.append(content)


def test_a_non_owning_process_stands_aside_instead_of_starting_a_batch(
        monkeypatch):
    """`/run` 的實作在拿不到監督權時**一句話讓位**，而且完全不碰任何行程。

    讓位的判斷刻意放在最前面（連獨立監督者掃描都不跑）：這條路的下一步就是清掃與
    spawn，而對面那個行程正在監督同一份批次。
    """
    import asyncio  # noqa: PLC0415

    b = _bot_with_batch_claim(monkeypatch, False)
    channel = _Recorder()

    def _boom(*_a, **_k):
        raise AssertionError("讓位的路徑竟然去掃描／啟動了東西")

    monkeypatch.setattr(b, "_launcher_scan", _boom)
    monkeypatch.setattr(b, "_spawn_webrunner", _boom)
    asyncio.run(b._do_webrunner_run(channel))
    assert channel.sent == [b.BATCH_STAND_ASIDE_NOTICE], channel.sent


def test_a_non_owning_process_refuses_to_stop_someone_elses_batch(monkeypatch):
    """**停止也是監督動作。** 在這裡停掉的是**另一個行程**生的子行程，而它的看門狗
    會把它們重生回來——使用者看到「停了又回來了」，兩邊的紀錄都正常。"""
    import asyncio  # noqa: PLC0415
    import types  # noqa: PLC0415

    b = _bot_with_batch_claim(monkeypatch, False)
    recorder = _Recorder()
    monkeypatch.setattr(b, "safe_reply", recorder.reply)

    def _boom(*_a, **_k):
        raise AssertionError("讓位的路徑竟然動了行程")

    monkeypatch.setattr(b, "_terminate_all_webrunner_instances", _boom)
    before = b._webrunner_stop_requested
    message = types.SimpleNamespace(
        author=types.SimpleNamespace(id=b.OWNER_USER_ID), attachments=[],
        guild=None, channel=types.SimpleNamespace(id=b.CHANNEL_ID))
    asyncio.run(b.cmd_stop(message))
    assert recorder.sent == [b.BATCH_STAND_ASIDE_NOTICE], recorder.sent
    assert b._webrunner_stop_requested is before, (
        "讓位的路徑改了停止旗標——那會讓**本行程**從此拒絕啟動批次")


def test_the_stand_aside_notice_says_who_is_doing_it_and_what_to_do():
    """讓位的字串必須說「有別人在做」而不是「失敗了」——那兩件事要使用者做的處置
    完全不同。同時它受 Secrecy Layer 1 約束：沒有主機路徑、沒有 PID。"""
    import discord_bot as b  # noqa: PLC0415

    text = b.BATCH_STAND_ASIDE_NOTICE
    assert "另一個行程" in text
    assert "未受影響" in text
    for banned in (".lock", "state/", "pid", "PID", "\\", ".py"):
        assert banned not in text, f"讓位訊息裡出現了 {banned!r}：{text}"


def test_the_batch_lock_is_not_per_platform():
    """批次**全機一份**，所以它的鎖刻意不帶平台名。

    帶了的話每個平台都會拿到自己的一把，而「誰監督批次」就沒有答案了——那正是這把
    鎖要回答的唯一問題。
    """
    import discord_bot as b  # noqa: PLC0415

    assert b.ACTIVE_PLATFORM not in Path(b.BATCH_SUPERVISOR_LOCK_FILE).name
    assert b.ACTIVE_PLATFORM in Path(b.BOT_LOCK_FILE).name, (
        "bot 自己的實例鎖**要**帶平台名，否則第二個平台會被第一個平台擋掉")


# ---------------------------------------------------------------------------
# 關掉一個平台，以及沒填憑證的平台
# ---------------------------------------------------------------------------
_BOTH_ON = {"platforms": {"discord": {"enabled": True},
                          "telegram": {"enabled": True}}}


def _all_credentials(monkeypatch, present=True):
    monkeypatch.setattr(pr, "has_credentials", lambda name=None: present)


def test_a_platform_switched_off_is_absent(monkeypatch):
    """`enabled: false` ＝ 不起行程、不註冊工作。**預設平台也一樣**——「每個平台都
    要能各自關掉」包含它。"""
    _all_credentials(monkeypatch)
    off_default = {"platforms": {"discord": {"enabled": False},
                                 "telegram": {"enabled": True}}}
    assert pr.enabled_platforms(off_default) == ["telegram"]
    off_other = {"platforms": {"discord": {"enabled": True},
                               "telegram": {"enabled": False}}}
    assert pr.enabled_platforms(off_other) == ["discord"]
    assert pr.enabled_platforms(_BOTH_ON) == ["discord", "telegram"]


def test_the_default_platform_is_on_without_a_section(monkeypatch):
    """沒有 `platforms` 那一段時，預設平台照常啟動。

    預設關著會讓 fresh clone 的 bot 什麼都不做，而症狀跟「設定沒生效」一模一樣。
    """
    _all_credentials(monkeypatch)
    assert pr.enabled_platforms({}) == [pr.DEFAULT_PLATFORM]
    assert pr.enabled_platforms(None) == [pr.DEFAULT_PLATFORM]


def test_a_platform_without_credentials_is_absent_not_broken(monkeypatch):
    """憑證沒填 ＝ 缺席，不是失敗。

    每次啟動都抱怨一次就是下一個把記錄檔洗掉的雜訊源（一個 repo 不會同時接四個
    平台）。但**原因要問得出來**，否則「開著卻沒生效」與「根本沒設定」分不出來。
    """
    monkeypatch.setattr(pr, "has_credentials",
                        lambda name=None: name == "discord")
    assert pr.enabled_platforms(_BOTH_ON) == ["discord"]
    reasons = {name: why for name, _ok, why in pr.platform_survey(_BOTH_ON)}
    assert "telegram_bot_token.md" in reasons["telegram"], reasons
    # 不看憑證時它是「開著的」——兩個原因分得開，才說得出是哪一個。
    assert pr.enabled_platforms(_BOTH_ON, require_credentials=False) == [
        "discord", "telegram"]


def test_the_survey_says_why_for_every_platform(monkeypatch):
    """每一個設定檔提到的平台都要有一列，關著的那些也要有原因。"""
    _all_credentials(monkeypatch)
    rows = pr.platform_survey(
        {"platforms": {"discord": {"enabled": True},
                       "telegram": {"enabled": False}}})
    names = [name for name, _ok, _why in rows]
    assert names == ["discord", "telegram"], names
    assert rows[0][1] is True and rows[1][1] is False
    for _name, _ok, why in rows:
        assert why.strip(), "有一列沒有原因——那正是這支工具要回答的問題"


def test_a_credential_file_that_is_empty_counts_as_not_configured(tmp_path,
                                                                  monkeypatch):
    """空檔案 ＝ 沒設定。複製範本卻忘了填是最常見的第一次設定錯誤。"""
    monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
    token = tmp_path / "telegram_bot_token.md"
    assert pr.has_credentials("telegram") is False       # 檔案不存在
    token.write_text("   \n", encoding="utf-8")
    assert pr.has_credentials("telegram") is False       # 空白
    token.write_text("a-token", encoding="utf-8")
    assert pr.has_credentials("telegram") is True


# ---------------------------------------------------------------------------
# 排程工作：開著的平台各一筆
# ---------------------------------------------------------------------------
def test_each_enabled_platform_gets_its_own_autostart_task(monkeypatch):
    """一個平台一個行程、一個行程一筆排程工作。

    共用一個工作名的話，第二次 `schtasks /Create /F` 會把第一筆覆寫掉，而症狀是
    「裝好了，但只有最後一個平台會自己起來」——要等下一次重開機才看得到。
    """
    _all_credentials(monkeypatch)
    names = pr.autostart_task_names(_BOTH_ON)
    assert len(names) == len(set(names)) == 3, names
    assert pr.AUTOSTART_BATCH_TASK in names
    assert pr.autostart_bot_task("discord") in names
    assert pr.autostart_bot_task("telegram") in names


def test_a_disabled_platform_has_no_autostart_task(monkeypatch):
    _all_credentials(monkeypatch)
    names = pr.autostart_task_names(
        {"platforms": {"discord": {"enabled": True},
                       "telegram": {"enabled": False}}})
    assert pr.autostart_bot_task("telegram") not in names
    assert names == (pr.autostart_bot_task("discord"), pr.AUTOSTART_BATCH_TASK)


def test_the_batch_task_is_there_even_with_no_platform(monkeypatch):
    """批次跟平台無關：一個平台都沒開的時候，批次那一筆照樣要註冊。"""
    monkeypatch.setattr(pr, "has_credentials", lambda name=None: False)
    assert pr.autostart_task_names(_BOTH_ON) == (pr.AUTOSTART_BATCH_TASK,)
