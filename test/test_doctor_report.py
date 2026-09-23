"""`/sys doctor` 整份報告：每一格說的話，以及「結論那一行」的判準。

`cmd_doctor` 是這套東西唯一一支**主動回答「現在有沒有問題」**的指令，而它最重要
的一行是最後那句 `- OK no obvious blocker found.`——看到它的人會停止追查。所以這
一檔量的不是「某一格會不會講話」，而是兩條推導出來的不變式：

1. **每一句報告得出來的話，都要有一個場景真的產出它。** 場景表不是手寫的清單，是
   跟 `cmd_doctor` 的原始碼對帳出來的：`_report_patterns()` 用 AST 抽出每一個
   `findings.append(...)` 的字串運算式，算成一個比對樣式，然後要求表裡每一筆各認
   領一個、不多不少。新增一個分支而忘了加場景，這裡當場紅——而不是靠覆蓋率下次
   有人想起來才看。
2. **標籤與結論一致。** `WARN`／`STOP` ＝有阻礙，那一則回覆就不該同時出現
   「沒發現阻礙」；`INFO`／`NOTE` ＝不是阻礙，結論那一行就該留著。目前有三筆例外，
   全部列在 `_TAG_SCORE_EXCEPTIONS` 並且各自寫著理由——那
   「給擁有者的決定」——不是遺漏，是還沒裁定。

## 為什麼要有這一檔（2026-09-20 量的）

`cmd_doctor` 的 127 行敘述裡有 **43 行從來沒有被執行過**，而那 43 行裡藏著一族真
缺陷：三個探測用**手寫的 `try/except`** 把失敗吞成一個**與「查過了，沒問題」完全
分不出來**的預設值（`stale = []`、`recovery = {"gap": None}`、`abandoned = []`），
而且一句話都不 append。實測任一個丟例外時，整份報告是

    **doctor**
    - OK no obvious blocker found.

一個字都沒提。那正好違反這支函式自己在三個地方寫下的判準——「doctor 的工作是
『我全部查過了，沒發現問題』。查不成就不可以把 `ok` 留著——那等於用沉默宣告一個
沒有根據的結論。」而且那三格問的正是這份報告裡最要緊的問題：元件是不是比磁碟上的
程式碼舊（程式碼自己寫著「這是本專案唯一回答這個問題的地方」）、主機重開後回不
回得來、有沒有接不回來的自走任務。

同一輪還量到第二個：文字辨識那一格把「探測丟例外」與「這台機器真的沒裝」送成
**同一句話**，而隔壁的桌面輸入那格特地把這兩件事分開（「查不成就不要宣稱『鎖定
了』——那是一個沒有根據的結論」）。兩個都已修掉，這一檔釘住它們。

## 夾具沿用 `test_bot_helpers`，不另外做一份

`_quiet_doctor` 把每一格都換成「沒事」，所以一支測試只動它要量的那一格；它自己的
正面對照是 `test_the_quiet_doctor_harness_is_quiet`。**再做一份平行的夾具會讓兩份
各自綠著、卻沒有任何東西在比較它們**，而新增一個探測時漏改的一定是沒人看的那份。
"""
import ast
import re

import pytest

import discord_bot as b
from test_bot_helpers import _quiet_doctor, _run_reply

_OK_LINE = "- OK no obvious blocker found."
_HEADER = "**doctor**"

# 這份報告的詞彙表。多一個標籤就要在這裡加，而且下面那條不變式會問它算不算阻礙。
_TAGS = ("STOP", "WARN", "INFO", "NOTE", "OK")
# 哪些標籤代表「有阻礙」——也就是結論那一行不該出現。
_BLOCKING = ("STOP", "WARN")


# ---------------------------------------------------------------------------
# 從原始碼抽出「這份報告說得出口的每一句話」
# ---------------------------------------------------------------------------

def _render(node) -> str | None:
    """把一個字串運算式算成 regex；算不出來的片段用 `.*`。

    只取開頭的常值是不夠的：`f"- WARN {len(x)} 個…"` 的第一個常值只有
    `"- WARN "`，拿它當鍵的話任何一行 WARN 都比對得到，對帳就變成裝飾
    （本專案對這個形狀的判語是「空的選集看起來跟乾淨的結果一模一樣」）。
    """
    if isinstance(node, ast.Constant):
        return re.escape(node.value) if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            re.escape(part.value)
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
            else ".*"
            for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left is None and right is None:
            return None
        return (left or ".*") + (right or ".*")
    if isinstance(node, ast.Lambda):
        return _render(node.body)
    return None


def _appended(func: ast.AST) -> list:
    """`findings.append(X)` 的每一個 X；`_owner_detail(...)` 拆成 raw ＋ generic。

    **不要用 `ast.walk` 掃所有字串常值**：f-string 的每一段自己也是一個
    `ast.Constant`，會被當成獨立的一句抽出來（`'- WARN '`），而那種碎片什麼都
    比對得到。
    """
    out = []
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "findings"
                and node.args):
            continue
        arg = node.args[0]
        if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                and arg.func.id == "_owner_detail" and len(arg.args) == 3):
            # 擁有者那一半與泛用那一半是**兩句不同的話**，各自要有場景。
            out.extend(arg.args[1:])
        else:
            out.append(arg)
    return out


def _doctor_ast(source: str | None = None, func: str = "cmd_doctor"):
    import inspect
    source = inspect.getsource(b) if source is None else source
    return next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == func)


def _report_patterns(source: str | None = None) -> dict[str, str]:
    """`{人讀得懂的標籤: regex}`，一句一筆。"""
    out: dict[str, str] = {}
    for node in _appended(_doctor_ast(source)):
        pattern = _render(node)
        assert pattern, (
            "有一句 finding 算不出比對樣式，抽取器跟不上 `cmd_doctor` 的寫法了："
            f"{ast.dump(node)[:200]}")
        out[re.sub(r"\\(.)", r"\1", pattern).replace(".*", "…")] = pattern
    return out


_PATTERNS = _report_patterns()


def _pattern_for(prefix: str) -> str:
    hits = [label for label in _PATTERNS if label.startswith(prefix)]
    assert len(hits) == 1, (
        f"{prefix!r} 對到 {len(hits)} 句，場景表認領不了一個唯一的目標：{hits}")
    return hits[0]


# ---------------------------------------------------------------------------
# 場景表：每一筆注入一個故障，並宣告它應該讓報告多出哪一句
# ---------------------------------------------------------------------------

def _raiser(marker="boom-4242"):
    def _boom(*_a, **_k):
        raise RuntimeError(f"D:\\secret\\host {marker}")
    return _boom


def _bot(name, value):
    return lambda mp, _tmp: mp.setattr(b, name, value)


def _gui(name, value):
    return lambda mp, _tmp: mp.setattr(b._gui, name, value)


def _pause_marker(mp, tmp_path):
    marker = tmp_path / "pause-on"
    marker.write_text("", encoding="utf-8")
    mp.setattr(b, "WEBRUNNER_PAUSE_FILE", marker)


# 兩列**同一個元件**。轉接殼（`.venv\\Scripts\\python.exe`）與它 spawn 出來的真
# 直譯器跑同一個腳本、啟動時間相同，所以 `find_stale_components` 一定成對回報——
# 對人顯示時要去重。只放一列的話「有沒有去重」量不出來。
_STALE_ROWS = [("bot", 4242, 100.0, 100.0 + 7200.0, "discord_bot.py"),
               ("bot", 4243, 100.0, 100.0 + 3600.0, "discord_bot.py")]

# (場景名稱, 注入, 這一句的開頭, 提問者是不是擁有者)
#
# 最後一欄刻意是一個**欄位**，不是從場景名稱推出來的。第一版寫成
# `case_id.endswith("owner")`，而 `"stale components, non-owner"` 也以 `owner`
# 結尾——於是那一筆用擁有者的身分跑，量到的是它想排除的那一半。
_CASES = [
    ("pause marker", _pause_marker, "- WARN pause marker exists", False),
    ("disk unreadable", _bot("_free_disk_gb", lambda: None),
     "- WARN could not read the free disk space", False),
    ("disk low", _bot("_free_disk_gb", lambda: 1.0), "- WARN disk free", False),
    ("queue read raises", _bot("read_todo_entries", _raiser()),
     "- WARN could not read the todo queues", False),
    ("end marker first", _bot("read_todo_entries", lambda *a, **k: ["end"]),
     "- STOP first prompt entry", False),
    ("no effective pairs", _bot("read_todo_entries", lambda *a, **k: []),
     "- INFO todo queues have no effective pairs", False),
    ("stranded single image",
     _bot("_single_image_request_pending_on_disk", lambda: True),
     "- WARN single-image request file exists", False),
    ("orphans found",
     _bot("_find_all_webrunner_pids",
          lambda *a, **k: ([(4242, "webrunner_novelai.py")], True)),
     "- WARN … untracked background process(es)", False),
    ("orphan scan incomplete",
     _bot("_find_all_webrunner_pids", lambda *a, **k: ([], False)),
     "- WARN could not finish scanning", False),
    ("log scan raises", _bot("_recent_log_error_count", _raiser()),
     "- WARN could not scan the recent log", False),
    ("log has errors", _bot("_recent_log_error_count", lambda *a, **k: 7),
     "- WARN recent log has", False),
    ("staleness probe raises", _bot("find_stale_components", _raiser()),
     "- WARN could not check whether the running components", False),
    ("stale components, non-owner",
     _bot("find_stale_components", lambda *a, **k: _STALE_ROWS),
     "- WARN … 個執行中的元件比磁碟上的程式碼舊", False),
    ("autostart probe raises", _bot("autostart_recovery_status", _raiser()),
     "- WARN could not check the boot-recovery chain", False),
    ("autostart not registered",
     _bot("autostart_recovery_status", lambda *a, **k: {"gap": "not_registered"}),
     "- WARN 開機自動啟動沒有註冊完整", False),
    ("autostart needs logon",
     _bot("autostart_recovery_status", lambda *a, **k: {"gap": "needs_logon"}),
     "- NOTE 開機自動啟動是「登入時」觸發", False),
    ("autologon cleared by an update",
     _bot("autostart_recovery_status",
          lambda *a, **k: {"gap": "autologon_expires",
                           "expiry": "update_restart"}),
     "- NOTE 自動登入現在是開著的，但系統更新", False),
    ("autologon use count runs out",
     _bot("autostart_recovery_status",
          lambda *a, **k: {"gap": "autologon_expires",
                           "expiry": "count_limited"}),
     "- NOTE 自動登入現在是開著的，但設了使用次數上限", False),
    ("autologon expires for a new reason",
     _bot("autostart_recovery_status",
          lambda *a, **k: {"gap": "autologon_expires", "expiry": "brand-new"}),
     "- NOTE 自動登入現在是開著的，但之後的某一次重開", False),
    ("manual restart vetoable",
     _bot("autostart_recovery_status",
          lambda *a, **k: {"gap": "restart_vetoable"}),
     "- NOTE 手動重新啟動可能被常駐程式擋下", False),
    ("session store raises", _bot("_dorossi_load_state", _raiser()),
     "- WARN could not check for abandoned background tasks", False),
    ("abandoned self-loops",
     _bot("dorossi_abandoned_loops", lambda *a, **k: [("u", "s1", 7200.0)]),
     "- WARN 有 … 個被中斷的自走任務", False),
    ("full agent mode", _bot("DOROSSI_CC_TOOLS", "full"),
     "- WARN Dorossi full-agent mode", False),
    ("desktop probe raises", _gui("input_desktop_available", _raiser()),
     "- WARN could not check whether the desktop takes input", False),
    ("desktop locked", _gui("input_desktop_available", lambda: False),
     "- WARN the machine is locked", False),
    ("ocr probe raises", _gui("ocr_status", _raiser()),
     "- WARN could not check whether text recognition", False),
    ("ocr unavailable", _gui("ocr_status", lambda: (False, "沒裝")),
     "- INFO text recognition unavailable", False),
    ("input reach probe raises", _gui("input_reaches_system", _raiser()),
     "- WARN could not check whether sent input reaches", False),
    ("input silently filtered", _gui("input_reaches_system", lambda: False),
     "- WARN sent input is not reaching the system", False),
    ("held probe raises", _gui("held_inputs", _raiser()),
     "- WARN could not check for keys/buttons", False),
    ("keys held down", _gui("held_inputs", lambda: ["ctrl"]),
     "- WARN … key(s)/button(s) still held down", False),
    ("job list raises", _gui("job_list", _raiser()),
     "- INFO could not list background jobs", False),
    ("jobs running",
     _gui("job_list", lambda: [{"running": True}, {"running": False}]),
     "- INFO … background job(s) running", False),
    ("macro still recording", _bot("_MACRO_RECORDING", object()),
     "- WARN a macro recording is still capturing input", False),
    ("dependency probe raises", _bot("_missing_dependencies", _raiser()),
     "- WARN could not check the declared dependencies", False),
    ("dependencies missing", _bot("_missing_dependencies", lambda: ["somepkg"]),
     "- WARN … declared dependenc(ies) missing", False),
]

# 擁有者那一半只有把提問者換成擁有者才走得到，所以它自己一筆。
_OWNER_CASE = ("stale components, as the owner",
               _bot("find_stale_components", lambda *a, **k: _STALE_ROWS),
               "- WARN 執行中的元件比磁碟上的程式碼舊：", True)

_ALL_CASES = _CASES + [_OWNER_CASE]
_IDS = [case[0] for case in _ALL_CASES]

# 目前標籤與 `ok` 不一致的三筆。**不是遺漏，是還沒決定**——這
# 2026-09-19 那筆「`/sys doctor` 周邊還剩下的事」第 1 項已經把它們登記成「給擁有者
# 的決定」（各往哪邊對齊由擁有者決定），所以這裡只把現狀釘住，不自行改字串的嚴重度。
# 值是「結論那一行在不在」。
_TAG_SCORE_EXCEPTIONS = {
    "- WARN Dorossi full-agent mode": True,
    "- WARN a macro recording is still capturing input": True,
    "- INFO text recognition unavailable": False,
}


def _report(monkeypatch, tmp_path, inject, *, message=None):
    _quiet_doctor(monkeypatch, tmp_path)
    inject(monkeypatch, tmp_path)
    asker = object() if message is None else message
    return _run_reply(monkeypatch, lambda: b.cmd_doctor(asker))


def _findings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


def _tag_of(line: str) -> str:
    return line.split(" ", 2)[1]


# ---------------------------------------------------------------------------
# 不變式一：報告說得出口的每一句，都有一個場景真的產出它
# ---------------------------------------------------------------------------

def test_the_scenario_table_claims_every_line_the_report_can_contain():
    """場景表跟 `cmd_doctor` 的原始碼對帳，**兩個方向都要**。

    只檢查「表裡每一筆都對得到一句」的話，新增一個分支而忘了加場景不會有任何症狀
    ——這一檔照樣全綠，只是那一句從來沒有被送出來過驗證，正是本專案一再踩到的
    單向對帳形狀。反過來只檢查「每一句都被認領」也不夠：一筆認領了不存在的字串
    （改名之後）會變成一個永遠比對不到的死條目。

    `- OK …` 不在場景表裡，它由「沒有任何故障」那條路產生，另有一支量它。
    """
    assert len(_PATTERNS) >= 30, (
        f"只抽到 {len(_PATTERNS)} 句——抽取器跟不上 `cmd_doctor` 的寫法了，"
        "後面每一支都會在一份幾乎空的清單上「通過」")
    claimed = [_pattern_for(case[2]) for case in _ALL_CASES]
    assert len(claimed) == len(set(claimed)), "有兩筆場景認領同一句"
    missing = sorted(set(_PATTERNS) - set(claimed) - {_OK_LINE})
    assert not missing, (
        "`cmd_doctor` 送得出這幾句，但沒有任何場景產出它們——"
        f"加進 `_CASES`：{missing}")


def test_every_line_the_report_can_contain_carries_a_known_tag():
    """報告的詞彙表是封閉的。

    讀的人靠 `WARN`／`INFO` 之類的字決定要不要停下來處理；多一個沒人定義過的標籤
    （或少了標籤）會讓下面那條「標籤與結論一致」的不變式失去依據，而它不會紅——
    `_tag_of` 只是切出第二個詞，切出什麼都不會報錯。
    """
    for label in _PATTERNS:
        tag = _tag_of(label)
        assert tag in _TAGS, f"沒見過的嚴重度標籤 {tag!r}：{label[:60]!r}"


@pytest.mark.parametrize("case_id, inject, prefix, as_owner", _ALL_CASES,
                         ids=_IDS)
def test_each_scenario_adds_exactly_the_one_line_it_claims(
        monkeypatch, tmp_path, case_id, inject, prefix, as_owner):
    """其餘各格都安靜時，一個故障只能多出**一行**，而且是它認領的那一行。

    「只多一行」是承重的：doctor 的每一格互不相干，一個故障波及第二格代表兩件事
    之間有沒寫下來的耦合。也因為這樣，上面那條對帳才能用「一句對一筆」。
    """
    text = _report(monkeypatch, tmp_path, inject,
                   message=b.OWNER_USER_ID if as_owner else None)
    assert text.startswith(_HEADER + "\n"), text[:80]
    body = [line for line in _findings(text) if line != _OK_LINE]
    assert len(body) == 1, f"預期只多一行，實際 {len(body)} 行：{body}"
    assert re.fullmatch(_PATTERNS[_pattern_for(prefix)], body[0]), (
        f"送出的字跟原始碼裡那一句對不起來：{body[0]!r}")


def test_a_clean_machine_gets_the_conclusion_line_and_nothing_else(
        monkeypatch, tmp_path):
    """反面對照：沒有注入任何故障時，報告只有標題與結論。

    少了這一支，上面每一支都可能是在「所有情況都會多一行」的世界裡通過的。
    """
    text = _report(monkeypatch, tmp_path, lambda _mp, _tmp: None)
    assert text.splitlines() == [_HEADER, _OK_LINE], text


# ---------------------------------------------------------------------------
# 不變式二：標籤與結論一致
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case_id, inject, prefix, as_owner", _ALL_CASES,
                         ids=_IDS)
def test_the_conclusion_line_agrees_with_the_severity_tag(
        monkeypatch, tmp_path, case_id, inject, prefix, as_owner):
    """`WARN`／`STOP` 出現時就不該同時說「沒發現阻礙」，反之亦然。

    這一條是**推導**出來的，不是一格一格寫死的斷言：只要有人新增一個分支，上面那條
    對帳會逼他加一筆場景，而這一條會立刻問他「你這一行算不算阻礙」。一個新增的
    早退路徑繞不過它。

    三筆已知的不一致列在 `_TAG_SCORE_EXCEPTIONS`，各自寫著理由
    登記的「給擁有者的決定」——要往哪邊對齊不是我能替他裁定的，所以這裡只釘現狀。
    """
    label = _pattern_for(prefix)
    text = _report(monkeypatch, tmp_path, inject,
                   message=b.OWNER_USER_ID if as_owner else None)
    says_ok = _OK_LINE in _findings(text)
    expected = _tag_of(label) not in _BLOCKING
    for known, value in _TAG_SCORE_EXCEPTIONS.items():
        if label.startswith(known):
            expected = value
            break
    assert says_ok is expected, (
        f"{label[:50]!r} 是 {_tag_of(label)}，而結論那一行"
        f"{'在' if says_ok else '不在'}——兩者對不起來。"
        "真的要改的話，先看 `_TAG_SCORE_EXCEPTIONS` 裡那三筆的理由。")


def test_every_declared_tag_score_exception_still_names_a_real_line():
    """例外清單會**失效**而沒有任何症狀：那一句改了名之後，條目變成一個永遠比對
    不到的字串，守門照跑、整套照綠，而真正的不一致從此沒人看著。

    這跟 `_OWNER_ONLY_SLASH`、原子寫入那份常數清單是同一個形狀，本專案已經為它付過
    一次代價，所以每一筆都要反查得到。
    """
    for known in _TAG_SCORE_EXCEPTIONS:
        hits = [label for label in _PATTERNS if label.startswith(known)]
        assert len(hits) == 1, (
            f"`_TAG_SCORE_EXCEPTIONS` 的 {known!r} 對到 {len(hits)} 句——"
            "那一句被改掉了，這筆例外已經失效")


# ---------------------------------------------------------------------------
# 「查不成」不可以長得像「查過了，沒問題」
# ---------------------------------------------------------------------------

_FAILURE_CASES = [case for case in _ALL_CASES if "raises" in case[0]]


@pytest.mark.parametrize("case_id, inject, prefix, as_owner",
                         _FAILURE_CASES,
                         ids=[case[0] for case in _FAILURE_CASES])
def test_no_probe_failure_is_reported_as_a_clean_result(
        monkeypatch, tmp_path, capsys, case_id, inject, prefix, as_owner):
    """探測丟例外時，報告一定要講一句 `could not …`，而且原始例外只進 stderr。

    2026-09-20 修掉的就是這一族：元件過期、開機恢復鏈、被遺棄的自走任務三個探測
    原本用手寫的 `try/except`，把值設成 `[]` / `{"gap": None}`——跟「查過了，沒有」
    **完全分不出來**——而且一句話都不 append。於是探測一丟例外，整份報告就變成
    `**doctor**` 加一行 `- OK no obvious blocker found.`。

    這裡刻意**不逐格寫字面斷言**：涵蓋範圍由 `_FAILURE_CASES` 從場景表推導，而場景
    表又跟原始碼對帳，所以新增一個探測就自動被這條蓋到。
    """
    text = _report(monkeypatch, tmp_path, inject)
    body = [line for line in _findings(text) if line != _OK_LINE]
    assert body, "探測失敗了，報告卻一個字都沒提"
    assert "could not" in body[0], (
        f"查不成應該講成「could not …」，實際是：{body[0]!r}")
    err = capsys.readouterr().err
    assert "boom-4242" in err, (
        "原始例外沒有進 stderr——替身可能根本沒被叫到，這一支就等於沒測")


@pytest.mark.parametrize("case_id, inject, prefix, as_owner", _ALL_CASES,
                         ids=_IDS)
def test_no_report_leaks_host_detail_to_a_non_owner(
        monkeypatch, tmp_path, case_id, inject, prefix, as_owner):
    """洩漏規則第 1 層：主機路徑、PID、原始例外文字、模組檔名一律不得外送。

    `/sys doctor` 是**公開**指令，而它每一格的輸入都是主機上的東西——路徑、行程、
    例外。擁有者那一筆例外（`_owner_detail`）不在這裡量，它自己一支。
    """
    if as_owner:
        pytest.skip("擁有者本來就看得到完整細節，由 `_owner_detail` 那一支量")
    text = _report(monkeypatch, tmp_path, inject)
    for banned in ("boom-4242", "secret", "RuntimeError", "D:\\",
                   "novelai", "webrunner_", ".py", "4242"):
        assert banned not in text, f"{case_id}：{banned!r} 進了回覆\n{text}"


def test_the_owner_sees_which_components_are_stale_and_others_see_a_count(
        monkeypatch, tmp_path):
    """元件過期這一格是這份報告裡唯一走 `_owner_detail` 的：擁有者要的是**哪一個**
    元件、落後多久，其他人只能知道「有幾個」。

    兩邊都要量。只量擁有者那半的話，把 generic 換成 raw（也就是把主機細節送給所有
    人）不會紅；只量泛用那半的話，`_owner_detail` 整個拿掉、永遠送 generic 也不會
    紅——而那會讓擁有者失去這份報告裡唯一可以直接動手的資訊。
    """
    inject = _bot("find_stale_components", lambda *a, **k: _STALE_ROWS)
    generic = _report(monkeypatch, tmp_path, inject)
    raw = _report(monkeypatch, tmp_path, inject, message=b.OWNER_USER_ID)
    assert generic != raw, "擁有者與其他人拿到同一句——閘門沒有生效"
    # 兩列是**同一個元件**（轉接殼與本尊），所以對人顯示時算一個。不去重的話
    # 擁有者每次都會看到「bot、bot」，而且數量永遠是真實元件數的兩倍。
    assert "1 個執行中的元件" in generic, generic
    assert "discord_bot.py" not in generic and "bot" not in generic.split("\n")[1]
    # 擁有者那一半：元件名稱（去重後只有一個）、落後**最久**的小時數、以及是哪個
    # 檔案比較新。取 max 不是 min——報最短的落後會讓問題看起來比實際輕。
    assert raw.count("bot（") == 1, f"元件名稱沒有去重：{raw}"
    assert "discord_bot.py" in raw, raw
    assert "2.0 小時" in raw, f"落後時數不是最久的那一筆：{raw}"
    assert "supervisor" in raw, "少了「重啟 bot 不會重載 supervisor」那句提醒"


@pytest.mark.parametrize("inject, needle", [
    (_bot("_find_all_webrunner_pids",
          lambda *a, **k: ([(1, "a.py"), (2, "b.py"), (3, "c.py")], True)),
     "3 untracked"),
    (_bot("dorossi_abandoned_loops",
          lambda *a, **k: [("u", "s1", 3600.0), ("u", "s2", 9000.0)]),
     "2 個被中斷的自走任務"),
    (_bot("dorossi_abandoned_loops",
          lambda *a, **k: [("u", "s1", 3600.0), ("u", "s2", 9000.0)]),
     "2.5 小時"),
    (_gui("held_inputs", lambda: ["ctrl", "shift", "lmb"]), "3 key(s)"),
    (_gui("job_list", lambda: [{"running": True}, {"running": False},
                               {"running": True}]),
     "2 background job(s)"),
    (_bot("_missing_dependencies", lambda: ["alpha", "beta"]),
     "2 declared dependenc(ies)"),
])
def test_the_numbers_in_the_report_are_the_real_ones(
        monkeypatch, tmp_path, inject, needle):
    """帶數字的那幾句要報真的數字。

    一個錯的計數比沒有計數更糟：讀的人會照著它決定要不要動手（「只有一個殘留行程，
    殺掉就好」），而數字錯了完全看不出來——那一行的格式、標籤、嚴重度全都正常。

    每一筆都刻意讓輸入的長度 ≠ 1，否則把 `len(...)` 換成常數 `1` 的實作照樣通過；
    自走任務那一筆給兩個不同的年紀，把 `max` 換成 `min` 才會現形。背景工作那一筆
    混了沒在跑的列，篩選拿掉就會多算。
    """
    text = _report(monkeypatch, tmp_path, inject)
    assert needle in text, f"報告裡的數字不對，找不到 {needle!r}：{text}"


def test_a_desktop_that_cannot_take_input_is_never_sent_a_probe_keystroke(
        monkeypatch, tmp_path):
    """桌面用不了的時候不可以送那一下探測按鍵。

    `/sys doctor` 會真的注入一個 F13 按壓（那是偵測「輸入被無聲過濾」的唯一辦法，
    寫在 `cmd_doctor` 的 docstring）。**焦點在哪個視窗，那一下就送進哪個視窗**，
    所以它必須以「桌面真的收得到輸入」為前提；螢幕鎖著、或前一格根本查不成的時候
    送出去，既量不到東西，又對著一台狀態不明的機器做了一次真的輸入。

    兩種都要擋：明確回報鎖定（`False`），以及探測自己丟例外（查不成——「查不成就
    不要宣稱鎖定了」的另一面是「也不要當成沒鎖」）。反面對照放在最後：桌面正常時
    那一下**必須**送出去，否則把這個判斷寫死成「永遠不送」也會通過。
    """
    pressed = []

    def _reaches():
        pressed.append(True)
        return True

    for label, desktop in (("locked", lambda: False),
                           ("unknown", _raiser())):
        pressed.clear()

        def _inject(mp, _tmp, _desktop=desktop):
            mp.setattr(b._gui, "input_desktop_available", _desktop)
            mp.setattr(b._gui, "input_reaches_system", _reaches)

        _report(monkeypatch, tmp_path, _inject)
        assert not pressed, f"桌面 {label} 卻還是送了一次探測按鍵"

    pressed.clear()
    _report(monkeypatch, tmp_path,
            lambda mp, _tmp: mp.setattr(b._gui, "input_reaches_system", _reaches))
    assert pressed, "桌面正常時反而沒送——這一格等於整個關掉了"


# ---------------------------------------------------------------------------
# 靜態守門：doctor 裡的每一個 except 都必須說話
# ---------------------------------------------------------------------------

def _silent_handlers(func: ast.AST) -> list[int]:
    """`cmd_doctor` 裡「攔到例外卻不 append 任何 finding」的 `except` 行號。"""
    silent = []
    for node in ast.walk(func):
        if not isinstance(node, ast.ExceptHandler):
            continue
        appends = [inner for inner in ast.walk(node)
                   if isinstance(inner, ast.Call)
                   and isinstance(inner.func, ast.Attribute)
                   and inner.func.attr == "append"
                   and isinstance(inner.func.value, ast.Name)
                   and inner.func.value.id == "findings"]
        if not appends:
            silent.append(node.lineno)
    return silent


def test_every_hand_rolled_except_in_the_doctor_says_something():
    """手寫的 `try/except` 必須 append 一句，不能只寫 stderr 就把值設成健康預設。

    行為測試蓋不到這一條的**未來**：新增一個手寫的 `except` 而忘了講話，只有在有人
    想到要替它寫一個故障場景時才會現形。這一支不必有人想到。

    走 `_doctor_probe` 的那些不受這條約束——它們的失敗由呼叫端的 `if not ran:` 處理，
    而那一段本來就在 append。
    """
    silent = _silent_handlers(_doctor_ast())
    assert not silent, (
        f"`cmd_doctor` 第 {silent} 行的 except 吞了例外卻沒有 append 任何 finding："
        "那會讓查不成長得跟「查過了，沒問題」一模一樣")


@pytest.mark.parametrize("body, expected", [
    # 只寫 stderr、把值設成一個健康的預設——2026-09-20 修掉的正是這個形狀。
    ("        print('x')\n        stale = []", ["except Exception:"]),
    ("        findings.append('- WARN could not; see the log.')", []),
    ("        print('x')\n        findings.append('- WARN x')", []),
    # 巢狀的 try 也要看得到，否則把沉默的那個往裡面挪一層就繞過去了；而且外層
    # 那個**不**該被算進去，它自己有 append。
    ("        try:\n            pass\n        except OSError:\n"
     "            pass\n        findings.append('- WARN x')",
     ["except OSError:"]),
])
def test_the_silent_except_detector_is_not_decoration(body, expected):
    """守門自己的正負對照。

    沒有這一支的話，`_silent_handlers` 寫成永遠回空 list 也會通過——樹裡現在剛好
    一個違規都沒有，而「乾淨的結果」與「什麼都沒掃到」在輸出上一模一樣。

    比對的是**被指到的那一行原始碼**，不是行號減掉一個常數：合成語料前面多一行，
    行號算術就會安靜地指到別處，而斷言照樣可能通過。
    """
    source = ("async def cmd_doctor(message):\n"
              "    findings = []\n"
              "    try:\n        pass\n    except Exception:\n" + body + "\n")
    lines = source.splitlines()
    got = [lines[no - 1].strip() for no in _silent_handlers(_doctor_ast(source))]
    assert got == expected, (source, got)
