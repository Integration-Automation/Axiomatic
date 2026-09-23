"""撞到方案用量上限之後：**等額度回來再續跑**，而不是停掉整個自走任務。

後端的用量額度是每 5 小時滾動重設的。舊行為是撞到上限就結束整個迴圈、在 slot 留一個
`loop_pending` 標記，等擁有者事後手動 `/dorossi session continue <id>` 接續——對一個
無人值守的長任務來說，那代表一天要人工接好幾次，實質上跑不完。

這一份釘住新行為的三個決定，每一個都有反例測試：

**一、只信機器可讀的時間戳，不猜鐘點。** 上游實際會印兩種寫法：
`Claude AI usage limit reached|1749924000`（epoch 秒）與「Your limit will reset at
1pm (Etc/GMT+5)」。前者精確；後者**帶的是別人的時區**，把 `1pm` 當本機時間換算可能整整
差好幾小時。猜錯的兩個方向代價不對稱——猜早了只是多送一次會立刻失敗的探測（便宜），
猜晚了是整個任務白停數小時（昂貴）。所以拿不到時間戳時走「短等待起跳、每次加倍」的
退避探測，而不是相信一個沒有時區的鐘點。`reset_hint`（給人看的字串）不受影響。

**二、等待秒數一定 clamp 進 `[60s, max]`。** 下限不是潔癖：用量上限的判定字樣比對得
很寬（`"rate limit"`、`"limit reached"`…），哪天有別的錯誤被誤判成用量上限，0 秒等待
就會變成不停 spawn 後端行程的熱迴圈。上限則保證「後端報了一個荒謬的未來時刻」最多也
只睡 max 就再探一次。

**三、等待前必須把後端當下的 session id 存回 slot。** 用量上限幾乎都是「做到一半」才
撞上的；不存的話，等額度回來 resume 的是**上一輪**的舊 id，這一輪已經做完的事全部
白做。這一條在 `_dorossi_cc_usage_limit` 帶出 `session_id`、在迴圈的 handler 寫回。

另外釘住兩個純粹是「以後別人改壞」的形狀（AST，不是字串比對）：迴圈的
`except _DorossiUsageLimitError` 區塊必須以 `continue` 收尾（不是 `return`），而且必須
真的呼叫等待函式；等待函式必須輪詢 `st.abort` 並且用 `time.monotonic`——等待可能長達
數小時，期間的 NTP 校時不該讓它變成幾秒或幾天。
"""
from __future__ import annotations

import ast
import asyncio
import math
import os
import re
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord_bot as b  # noqa: E402
import dorossi_backend as db  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"

# 上游 issue 標題裡實際出現過的形狀（#2087 / #3169 / #9046 / #11429 都是這個樣子）。
REAL_PIPE_NOTICE = "Claude AI usage limit reached|1749924000"
# 上游另一種寫法：人類可讀、**帶著別人的時區**。刻意不換算成 epoch。
REAL_CLOCK_NOTICE = ("Claude usage limit reached. Your limit will reset at "
                     "1pm (Etc/GMT+5)")


# ---------------------------------------------------------------------------
# 一、epoch 擷取：只認 `|<epoch>`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    (REAL_PIPE_NOTICE, 1749924000.0),
    ("usage limit reached|1749924000\n", 1749924000.0),
    # 毫秒變體（目前實測是秒，但兩種都收，免得哪天靜悄悄失準）。
    ("usage limit reached|1749924000000", 1749924000.0),
    # 前後有雜訊也要找得到（`|` 不保證在最後）。
    ("is_error=True; result=limit reached|1749924000; subtype=error", 1749924000.0),
])
def test_the_pipe_timestamp_is_read(text, expected):
    assert db._dorossi_extract_reset_epoch(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "",
    None,
    "usage limit reached",                 # 沒有時間戳
    "usage limit reached|12345",           # 太短，不是 epoch
    "usage limit reached|0000000001",      # 10 位但落在合理區間外
    "usage limit reached|9999999999999",   # 13 位毫秒 → 換算後仍在區間外
    "usage limit reached|abcdefghij",      # 不是數字
])
def test_a_non_timestamp_is_not_mistaken_for_one(text):
    assert db._dorossi_extract_reset_epoch(text) is None


def test_the_clock_wording_deliberately_yields_no_epoch():
    """這是**刻意的**，不是漏掉的功能。

    「reset at 1pm (Etc/GMT+5)」沒有可靠的時區可用。把它當本機時間換算，猜晚了就是
    整個任務白停數小時；退避探測猜早了只多一次立刻失敗的呼叫。所以這裡必須是 None，
    由 `_dorossi_usage_wait_seconds` 走退避那條路。
    """
    assert db._dorossi_extract_reset_epoch(REAL_CLOCK_NOTICE) is None


def test_the_epoch_reader_never_raises_on_junk():
    for junk in (123, [], {}, object(), b"bytes"):
        assert db._dorossi_extract_reset_epoch(junk) is None


# ---------------------------------------------------------------------------
# 二、給人看的 hint 沒有被重構弄壞（它會被原樣貼進 Discord）
# ---------------------------------------------------------------------------

def test_the_pipe_variant_still_renders_a_human_hint():
    hint = db._dorossi_extract_reset_hint(REAL_PIPE_NOTICE)
    assert hint and hint[:2] == "20"        # "YYYY-MM-DD HH:MM"
    assert "|" not in hint


def test_the_clock_variant_still_produces_a_hint():
    """epoch 不換算，但**顯示**照舊——擁有者仍看得到後端說的時間。"""
    assert db._dorossi_extract_reset_hint("… · resets 3:45pm") == "resets 3:45pm"


def test_the_hint_stops_at_the_end_of_the_sentence():
    """擷取出來的片段在句點處截斷。白名單前綴擋得住換行與全形句號，但擋不住「句點＋空格」——
    字母、空格與句點都在白名單裡——所以少了這一刀，擁有者看到的是
    `resets 3:45pm. Please upgrade your plan`（2026-09-23 實測）。這一格在整個套件裡從來沒跑過。"""
    text = "limit hit · resets 3:45pm. Please upgrade your plan"
    assert db._dorossi_extract_reset_hint(text) == "resets 3:45pm"


@pytest.mark.parametrize("text", [
    # 這一筆是 `_dorossi_sanitize_reset_hint` 存在的原因：一段其實是別的錯誤、
    # 只是剛好含 "reset" 的後端文字，原樣回傳就把主機路徑送進 Discord。
    r"rate limit... connection reset by peer while writing D:\logs\x.log",
    # 帶數字的版本。上面那一筆**同時**踩到「含磁碟機字首」與「不含數字」兩道檢查，
    # 於是拿掉任何一道都還有另一道擋著，兩個 mutation 互相遮蔽、雙雙存活。這一筆
    # 讓數字那道通過，磁碟機字首才是唯一擋得住它的東西。
    r"connection reset by peer at 3:45 while writing D:\logs\x.log",
    "resets https://example.test/quota",
    "reset " + "x" * 200,
])
def test_a_hint_never_smuggles_a_path_or_url(text):
    hint = db._dorossi_extract_reset_hint(text)
    if hint is None:
        return
    # 磁碟機字首（`D:`）與 URL scheme（`https:`）是同一個形狀，一條規則涵蓋兩者。
    # 真正的重設時間不會長這樣：`resets 3:45pm` 的冒號前面是數字，
    # `2026-08-31 14:00` 也是。
    assert not re.search(r"[A-Za-z]:", hint), f"hint 夾帶了磁碟機字首或 URL：{hint!r}"
    for banned in (":\\", ":/", "//", ".log"):
        assert banned not in hint, f"hint 夾帶了 {banned!r}：{hint!r}"


@pytest.mark.parametrize("text", [
    "connection reset by peer while streaming the response",
    "the pipe was reset during handshake",
])
def test_prose_that_merely_contains_reset_is_not_offered_as_a_time(text):
    """收斂器改成「取白名單字元的最長前綴」之後，一段普通英文散文很容易**整段**
    落在白名單裡（全是字母與空白）。沒有「必須含數字」那道，擁有者就會收到
    「用量預計於 reset by peer while streaming the response 重設」。
    重設時間一定帶數字，散文通常不帶——這就是那道閘的全部理由。"""
    assert db._dorossi_extract_reset_hint(text) is None


# ---------------------------------------------------------------------------
# 三、偵測器：把 epoch 與 session id 帶出來
# ---------------------------------------------------------------------------

def test_the_detector_carries_the_epoch_and_the_session_id():
    exc = db._dorossi_cc_usage_limit(
        {"subtype": "error", "result": REAL_PIPE_NOTICE}, "", "sess-abc")
    assert exc is not None
    assert exc.reset_at == pytest.approx(1749924000.0)
    assert exc.session_id == "sess-abc"
    assert exc.reset_hint


def test_the_epoch_is_found_outside_the_result_field():
    """`|<epoch>` 不保證落在 `result` 裡——terminal_reason / subtype 也可能帶著它。
    `reset_hint` 刻意維持只看 result/answer（那條路徑會被貼進 Discord，掃描範圍越窄
    越安全），所以這兩者的來源必須分開，不能「順手合併」。"""
    exc = db._dorossi_cc_usage_limit(
        {"subtype": "error_usage_limit",
         "terminal_reason": REAL_PIPE_NOTICE,
         "result": "usage limit reached"}, "", "s1")
    assert exc is not None and exc.reset_at == pytest.approx(1749924000.0)


def test_an_empty_session_id_becomes_none():
    """空字串會讓呼叫端的 `if limit_sid:` 誤判——一律正規化成 None。"""
    exc = db._dorossi_cc_usage_limit({"result": "usage limit reached"}, "", "")
    assert exc is not None and exc.session_id is None


def test_an_ordinary_failure_is_still_not_a_usage_limit():
    assert db._dorossi_cc_usage_limit(
        {"subtype": "error", "result": "file not found"}, "", "s1") is None


def test_a_limit_with_no_timestamp_leaves_reset_at_none():
    exc = db._dorossi_cc_usage_limit({"result": REAL_CLOCK_NOTICE}, "", "s1")
    assert exc is not None
    assert exc.reset_at is None          # → 呼叫端走退避探測
    assert exc.reset_hint                # 但仍看得到後端說的鐘點


# ---------------------------------------------------------------------------
# 三之二、串流事件才是機器可讀的重設時刻（2026-08-31）
#
# 上面那一整節建立在通知文字裡的 `…|<epoch>` 上，而那個形狀來自上游 issue 標題。
# 2026-08-31 在本機實測的 CLI（2.1.247）**整支執行檔裡搜不到那串字**——也就是說
# 那條路早就等同永遠回 None，每次撞上限都只能退避猜。同一版的 CLI 改成在
# `--output-format stream-json` 裡**每一次呼叫**都發一則 `rate_limit_event`，帶著
# 伺服器配額標頭給的真正重設時刻。底下這一節釘的是「優先用它」。
# ---------------------------------------------------------------------------

# 2026-08-31 實跑 `claude -p --output-format stream-json` 收到的真實事件（原樣）。
REAL_RATE_LIMIT_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 1788199200,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.49, "resetsAt": 1788199200},
            "seven_day": {"utilization": 0.33, "resetsAt": 1788357600},
        },
    },
    "uuid": "cf23672b-219a-4152-a095-d621695aadf8",
    "session_id": "8d3fa3c1-3bc9-4f18-bb6e-8347edbaabdb",
}


def test_the_real_rate_limit_event_yields_its_reset_time():
    assert db._dorossi_rate_limit_reset(REAL_RATE_LIMIT_EVENT) == \
        pytest.approx(1788199200.0)


def test_the_top_level_window_wins_over_the_unified_ones():
    """CLI 已經依 `rateLimitType` 挑好當下綁住的那個視窗，頂層的 `resetsAt` 就是
    它的結論。若反過來優先取 `unifiedWindows.five_hour`，週上限被擋住時會睡到
    五小時視窗的重設時刻——那個時刻早就過了，等於完全沒等。"""
    ev = {"rate_limit_info": {
        "resetsAt": 1788357600, "rateLimitType": "seven_day",
        "unifiedWindows": {"five_hour": {"resetsAt": 1788199200}}}}
    assert db._dorossi_rate_limit_reset(ev) == pytest.approx(1788357600.0)


@pytest.mark.parametrize("info,expected", [
    ({"unifiedWindows": {"five_hour": {"resetsAt": 1788199200}}}, 1788199200.0),
    ({"unifiedWindows": {"seven_day": {"resetsAt": 1788357600}}}, 1788357600.0),
    ({"resetsAt": 1788199200000}, 1788199200.0),          # 毫秒也收
])
def test_the_reset_time_is_found_in_the_fallback_places(info, expected):
    assert db._dorossi_rate_limit_reset({"rate_limit_info": info}) == \
        pytest.approx(expected)


@pytest.mark.parametrize("ev", [
    {}, {"rate_limit_info": None}, {"rate_limit_info": "x"},
    {"rate_limit_info": {"resetsAt": None}},
    {"rate_limit_info": {"resetsAt": True}},        # bool 是 int 的子類別
    {"rate_limit_info": {"resetsAt": "1788199200"}},
    {"rate_limit_info": {"resetsAt": float("nan")}},
    {"rate_limit_info": {"resetsAt": 5}},           # 不在合理 epoch 區間
    {"rate_limit_info": {"unifiedWindows": {"five_hour": "x"}}},
])
def test_a_malformed_rate_limit_event_yields_nothing(ev):
    assert db._dorossi_rate_limit_reset(ev) is None


def test_the_stream_reset_is_used_when_the_text_has_no_epoch():
    """這是實務上唯一會發生的組合：現在的 CLI 文字裡沒有 epoch，時刻只在串流事件裡。"""
    exc = db._dorossi_cc_usage_limit(
        {"result": REAL_CLOCK_NOTICE}, "", "s1", stream_reset=1788199200.0)
    assert exc is not None
    assert exc.reset_at == pytest.approx(1788199200.0)


def test_the_stream_reset_beats_a_timestamp_in_the_text():
    """兩個都有時以串流事件為準——它來自伺服器的配額標頭，文字是人寫給人看的。"""
    exc = db._dorossi_cc_usage_limit(
        {"result": REAL_PIPE_NOTICE}, "", "s1", stream_reset=1788199200.0)
    assert exc is not None
    assert exc.reset_at == pytest.approx(1788199200.0)


def test_a_machine_reset_time_always_produces_a_human_hint():
    """有了時刻卻沒有 hint 的話，擁有者會收到「會自動續跑」但不知道大概什麼時候。
    現在的 CLI 文字連 `reset` 這個字都不一定有，所以 hint 要能從時刻補出來。"""
    exc = db._dorossi_cc_usage_limit(
        {"result": "Usage limit reached"}, "", "s1", stream_reset=1788199200.0)
    assert exc is not None and exc.reset_hint


def test_without_either_source_the_caller_still_backs_off():
    exc = db._dorossi_cc_usage_limit(
        {"result": "Usage limit reached"}, "", "s1", stream_reset=None)
    assert exc is not None and exc.reset_at is None


def test_the_stream_event_is_actually_captured_and_handed_over():
    """「有讀到」與「有用到」是兩件事。把 `rate_limit_event` 的分支留著、卻不把
    抓到的值傳給 `_dorossi_cc_usage_limit`，所有純函式測試照樣全綠，而行為已經退化
    成永遠退避猜。這裡走機制：分支裡指派的那個變數，必須是兩個偵測呼叫的
    `stream_reset=` 引數。"""
    tree = ast.parse((PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8"))
    # 折疊（擷取）與判定（交出去）在 2026-09-07 被拆成兩支——擷取進
    # `_ClaudeStreamState.feed` 的一個屬性，判定在 `_claude_stream_verdict` 讀它。
    # 所以機制從「同一個函式裡的區域變數」改成「同一個屬性」，但要證明的事一樣：
    # 抓到的值必須真的走到偵測呼叫的 `stream_reset=`。
    fold = _find_method(tree, "_ClaudeStreamState", "feed")
    assigns = [n for n in ast.walk(fold) if isinstance(n, ast.Assign)]
    # 值「來自」擷取函式的名字，含間接（`got = f(ev)` 之後 `self.x = got`）。
    # 取到不動點為止，因為擷取與存起來之間可以隔任意多次改名。
    locals_: set = set()
    attrs: set = set()
    for _ in range(len(assigns) + 1):
        grown_l, grown_a = set(locals_), set(attrs)
        for node in assigns:
            sourced = any(_dotted(c.func) == "_dorossi_rate_limit_reset"
                          for c in ast.walk(node.value)
                          if isinstance(c, ast.Call)) or any(
                isinstance(x, ast.Name) and x.id in locals_
                for x in ast.walk(node.value))
            if not sourced:
                continue
            grown_l |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            grown_a |= {t.attr for t in node.targets if isinstance(t, ast.Attribute)}
        if (grown_l, grown_a) == (locals_, attrs):
            break
        locals_, attrs = grown_l, grown_a
    assert attrs, ("串流折疊沒有呼叫 `_dorossi_rate_limit_reset` 並把結果存進狀態"
                   "物件的屬性——那個值就到不了判定端")
    verdict = _find_func(tree, "_claude_stream_verdict")
    calls = [c for c in ast.walk(verdict) if isinstance(c, ast.Call)
             and _dotted(c.func) == "_dorossi_cc_usage_limit"]
    assert calls, "找不到偵測呼叫"
    for call in calls:
        handed = {kw.value.attr for kw in call.keywords
                  if kw.arg == "stream_reset" and isinstance(kw.value, ast.Attribute)}
        assert handed & attrs, (
            f"第 {call.lineno} 行的偵測呼叫沒有帶上串流事件抓到的重設時刻——"
            "撞上限時會退回退避猜，而那正是這一節要修掉的東西")
    # 兩半都對了，還要證明它們真的被接起來：叫用端必須同時餵折疊、也問判定。
    caller = _find_func(tree, "_dorossi_via_claude_code")
    called = {_dotted(c.func) for c in ast.walk(caller) if isinstance(c, ast.Call)}
    assert "_claude_stream_verdict" in called, (
        "`_dorossi_via_claude_code` 沒有呼叫判定函式——上面兩段檢查會變成在驗一段"
        "沒有人執行的程式碼")
    assert any(name.endswith(".feed") for name in called), (
        "`_dorossi_via_claude_code` 沒有把讀到的行餵進折疊——判定端拿到的會是空狀態")


# ---------------------------------------------------------------------------
# 三之三、成功回合的答案不是用量上限通知（2026-09-19）
#
# 比對表（"rate limit"、"limit reached"、"five_hour"…）是為**錯誤文字**寫的，卻也被
# 套在成功回合的答案上。2026-09-17 22:44 與 09-19 05:42 各有一輪 rc==0、
# subtype=success 的正常回答，只因為內文討論到某個 SDK 的 rate limit 就被判成用量
# 上限：答案丟掉、自走迴圈睡到五小時視窗重設，白等一小時四十八分。
#
# 這一節**大半是必須放行的案例**。「文字證據要過一道關」是一個放寬步驟，而放寬步驟
# 只有「必須放行」的近似案例殺得死——只測「必須攔下」的話，把整道關拿掉照樣全綠。
# 每一道條件都有一支**只踩它一道**的輸入：長文沒有串流狀態（只靠長度）、短文帶著
# allowed（只靠否決）、錯誤結果的長文（只靠「成功才套新規則」）。
# ---------------------------------------------------------------------------

_OK = {"subtype": "success", "is_error": False}

# 05:42 那一輪的真實答案節錄（原文 3892 字，比對表在第 2298 字的 "rate limit" 命中；
# 從該工作階段的對話紀錄逐字取出，只刪掉中間與此無關的條目）。
_REAL_0542_EXCERPT = (
    "This round is done, and nothing has been committed. The item that matters "
    "most is a CLI change still to come: once the Claude CLI auto-updates to "
    "2.1.277, Dorossi will start compacting after almost every turn. That "
    "regression needs no bot restart; the fix will, and I'll write it first thing "
    "next round.\n\n"
    "**Problems found :**\n"
    "2. **The Anthropic SDK (1.6.0 and later) now waits out a long "
    "`Retry-After`.** On a mock server, a rate limit of `Retry-After: 3600` made "
    "1.4.0 fail after about 1.3 seconds; 1.7.0 would hang for about 2 hours.\n")

# 單行、沒有換行、超過上限的長答案：確保擋住它的是**長度**，不是別的形狀特徵。
_LONG_ONE_LINE = ("I checked the five_hour window, the rate_limit headers and why "
                  "the log said limit reached; you've hit your stride on this one. "
                  + "Nothing else changed in this round. " * 8).strip()

# CLI 的真實通知（前兩則是本機 log 原文，後面是上游與本 repo 既有的樣本）。
_REAL_NOTICES = [
    "You've hit your session limit · resets 4:50pm (Asia/Taipei)",
    "You've hit your weekly limit · resets Sep 16, 10pm (Asia/Taipei)",
    "You've hit your session limit · resets 1pm (Etc/GMT+5)",
    "Claude AI usage limit reached|1749924000",
    "5-hour limit reached ∙ resets 3pm",
    "Weekly limit reached ∙ resets Oct 9",
    REAL_CLOCK_NOTICE,
]


def _ok_result(text):
    return {**_OK, "result": text}


def test_the_0542_answer_is_not_a_usage_limit():
    """事故原文。沒有串流狀態可以否決，所以擋住它的只有長度那一道。"""
    assert len(_REAL_0542_EXCERPT) > db._DOROSSI_USAGE_NOTICE_MAX_CHARS
    assert "rate limit" in _REAL_0542_EXCERPT.lower()   # 前提：它真的會命中比對表
    assert db._dorossi_cc_usage_limit(
        _ok_result(_REAL_0542_EXCERPT), _REAL_0542_EXCERPT, "s1") is None


def test_a_long_one_line_answer_hitting_every_broad_marker_is_not_a_limit():
    low = _LONG_ONE_LINE.lower()
    hits = [m for m in db._DOROSSI_USAGE_LIMIT_MARKERS if m in low]
    # 前提：這段真的踩到好幾個最寬的字樣，而且沒有換行——擋住它的只能是長度。
    assert {"five_hour", "rate_limit", "limit reached", "you've hit your"} <= set(hits)
    assert "\n" not in _LONG_ONE_LINE
    assert len(_LONG_ONE_LINE) > db._DOROSSI_USAGE_NOTICE_MAX_CHARS
    assert db._dorossi_cc_usage_limit(
        _ok_result(_LONG_ONE_LINE), _LONG_ONE_LINE, "s1") is None


def test_the_answer_is_measured_even_when_the_result_field_is_absent():
    """`result` 不是字串時，長度改量 `answer`——兩者只要有一個是長篇散文就不是通知。"""
    assert db._dorossi_cc_usage_limit(dict(_OK), _LONG_ONE_LINE, "s1") is None


@pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
def test_a_short_answer_that_mentions_a_rate_limit_is_vetoed_by_the_stream(status):
    """短到像通知、又踩到比對表的**答案**：長度擋不住，只有串流的配額狀態擋得住。
    伺服器說這次呼叫放行了，這一輪就不可能是配額拒絕。"""
    text = "Yes, that SDK has a rate limit."
    assert db._dorossi_cc_usage_limit(_ok_result(text), text, "s1") is not None, (
        "前提不成立：沒有串流狀態時這句應該會被當成通知，否則下面證明不了否決")
    assert db._dorossi_cc_usage_limit(
        _ok_result(text), text, "s1", stream_status=status) is None


@pytest.mark.parametrize("notice", _REAL_NOTICES)
@pytest.mark.parametrize("status", [None, "rejected"])
def test_a_real_notice_with_rc_zero_is_still_a_usage_limit(notice, status):
    """反方向：真的通知以 rc==0、subtype=success 回來時照樣要攔。串流說 rejected
    （或沒有事件）時不可以被否決——否決只屬於 allowed／allowed_warning。"""
    exc = db._dorossi_cc_usage_limit(
        _ok_result(notice), notice, "s1", stream_status=status)
    assert isinstance(exc, db._DorossiUsageLimitError), notice


@pytest.mark.parametrize("ev", [
    # 本機 log 裡 42 則真實通知全是這個形狀。
    {"subtype": "success", "is_error": True, "api_error_status": 429},
    # 狀態碼單獨也夠：結構化欄位不經過任何文字條件。
    {"subtype": "success", "is_error": False, "api_error_status": 429},
    {"subtype": "success", "is_error": False, "api_error_status": 402},
])
def test_a_structured_limit_beats_the_stream_veto_and_the_length(ev):
    """429／402 是伺服器說的，文字與長度都不參與；串流前面某一則說 allowed 也不能
    否決它——一輪裡可能打了好幾次後端，最後那次才被擋。"""
    exc = db._dorossi_cc_usage_limit(
        {**ev, "result": _LONG_ONE_LINE}, _LONG_ONE_LINE, "s1",
        stream_status="allowed")
    assert isinstance(exc, db._DorossiUsageLimitError)


@pytest.mark.parametrize("ev", [
    {"subtype": "success", "is_error": True},
    {"subtype": "error", "is_error": True},
    {"subtype": "error_during_execution"},
    {},
])
def test_an_error_result_keeps_the_broad_text_rule(ev):
    """錯誤結果的文字是 CLI 的錯誤訊息，不是模型的答案——沿用整張表，長度與否決都
    不適用。rc != 0 的行為因此一個字都沒變。"""
    exc = db._dorossi_cc_usage_limit(
        {**ev, "result": _LONG_ONE_LINE}, _LONG_ONE_LINE, "s1",
        stream_status="allowed")
    assert isinstance(exc, db._DorossiUsageLimitError), ev


def test_the_notice_length_bound_is_inclusive():
    cap = db._DOROSSI_USAGE_NOTICE_MAX_CHARS
    at_cap = ("usage limit reached " + "x" * cap)[:cap]
    over = at_cap + "x"
    assert len(at_cap) == cap and len(over) == cap + 1
    assert db._dorossi_cc_usage_limit(_ok_result(at_cap), at_cap, "s1") is not None
    assert db._dorossi_cc_usage_limit(_ok_result(over), over, "s1") is None


def test_the_rate_status_vocabulary_is_read_from_the_real_event():
    assert db._dorossi_rate_limit_status(REAL_RATE_LIMIT_EVENT) == "allowed"
    for word in sorted(db._DOROSSI_RATE_STATUSES):
        assert db._dorossi_rate_limit_status(
            {"rate_limit_info": {"status": word}}) == word
    # 只有放行的那兩個能否決；rejected 絕不能在裡面。
    assert db._DOROSSI_RATE_STATUS_ALLOWED == {"allowed", "allowed_warning"}
    assert db._DOROSSI_RATE_STATUS_ALLOWED < db._DOROSSI_RATE_STATUSES


@pytest.mark.parametrize("ev", [
    {}, None, "x", {"rate_limit_info": None}, {"rate_limit_info": "allowed"},
    {"rate_limit_info": {}}, {"rate_limit_info": {"status": None}},
    {"rate_limit_info": {"status": 1}}, {"rate_limit_info": {"status": "ALLOWED"}},
    {"rate_limit_info": {"status": "allowed_later"}},
])
def test_an_unknown_rate_status_is_not_read_as_allowed(ev):
    """認不得的值一律 None（＝不否決）。這個值唯一的用途是否決文字判定，所以認不得
    的失敗方向必須是退回文字判定，而不是把一則真的通知放過去。"""
    assert db._dorossi_rate_limit_status(ev) is None


# ---------------------------------------------------------------------------
# 四、等待秒數
# ---------------------------------------------------------------------------

def _exc(**kw):
    return db._DorossiUsageLimitError("limit", None, **kw)


def test_a_known_reset_time_is_slept_through_plus_grace():
    now = 1_700_000_000.0
    delay = db._dorossi_usage_wait_seconds(
        _exc(reset_at=now + 3600.0), 1, now=now)
    assert delay == pytest.approx(3600.0 + db.DOROSSI_USAGE_WAIT_GRACE_SEC)


def test_a_long_past_reset_time_falls_back_instead_of_retrying_at_once():
    """時鐘偏移／後端報了舊視窗時，「已經過去」不代表「不用等」——代表那個時間戳
    沒有參考價值。立刻重試只會再撞一次牆。"""
    now = 1_700_000_000.0
    delay = db._dorossi_usage_wait_seconds(
        _exc(reset_at=now - 3600.0), 1, now=now)
    assert delay == pytest.approx(db.DOROSSI_USAGE_WAIT_FALLBACK_SEC)


def test_a_just_expired_reset_time_still_waits_the_floor():
    """剛剛才過期（幾秒前）跟「過期一小時」是兩回事：前者代表視窗剛翻，緩衝把它推成
    一個很短的正數，於是等下限那 60 秒就重試——不是立刻重試，也不必等滿 15 分鐘。"""
    now = 1_700_000_000.0
    delay = db._dorossi_usage_wait_seconds(
        _exc(reset_at=now - 10.0), 1, now=now)
    assert delay == pytest.approx(db.DOROSSI_USAGE_WAIT_MIN_SEC)


def test_the_fallback_doubles_each_consecutive_wait():
    now = 1_700_000_000.0
    base = db.DOROSSI_USAGE_WAIT_FALLBACK_SEC
    seen = [db._dorossi_usage_wait_seconds(_exc(), n, now=now)
            for n in (1, 2, 3, 4)]
    ceiling = max(db.DOROSSI_USAGE_WAIT_MAX_SEC, db.DOROSSI_USAGE_WAIT_MIN_SEC)
    assert seen == [min(base * 2 ** k, ceiling) for k in range(4)]


def test_a_huge_attempt_number_does_not_overflow():
    """`fallback * 2 ** attempt` 若不封頂，attempt 大一點 `float * 2**5000` 就直接
    丟 OverflowError——而這個乘法就發生在「已經出事了才會走到」的用量上限處理路徑上。
    封頂之後值不變（早就被 clamp 住了），只是不讓它有機會溢位。"""
    for attempt in (100, 5_000, 10 ** 6):
        delay = db._dorossi_usage_wait_seconds(_exc(), attempt)
        assert delay == pytest.approx(
            max(db.DOROSSI_USAGE_WAIT_MAX_SEC, db.DOROSSI_USAGE_WAIT_MIN_SEC))


@pytest.mark.parametrize("attempt", [0, -3, None, "x", 2.7, True])
def test_a_weird_attempt_value_never_raises(attempt):
    delay = db._dorossi_usage_wait_seconds(_exc(), attempt)
    assert math.isfinite(delay) and delay >= db.DOROSSI_USAGE_WAIT_MIN_SEC


@pytest.mark.parametrize("reset_at", [
    float("nan"), float("inf"), float("-inf"), True, False, "later", [], None,
])
def test_a_nonsense_reset_at_still_yields_a_sane_wait(reset_at):
    """`reset_at` 一路從後端文字／HTTP 標頭流過來，是不可信輸入。

    兩個特別容易漏的：`nan` 過不了 `> 0`（`nan > 0` 是 False）所以會落到退避那條路，
    這是對的；而 `True` 是 `int` 的子類別，`isinstance(x, (int, float))` 會放行它，
    於是 `True - now` 算出一個巨大的負數——必須用 `not isinstance(x, bool)` 擋掉。
    """
    delay = db._dorossi_usage_wait_seconds(_exc(reset_at=reset_at), 1)
    ceiling = max(db.DOROSSI_USAGE_WAIT_MAX_SEC, db.DOROSSI_USAGE_WAIT_MIN_SEC)
    assert math.isfinite(delay)
    assert db.DOROSSI_USAGE_WAIT_MIN_SEC <= delay <= ceiling


def test_a_boolean_is_never_read_as_a_timestamp():
    """`True` 是 `int` 的子類別，所以 `isinstance(x, (int, float))` 會放行它。

    這一支要用一個**小的 `now`** 才看得出差別，而那正是先前漏掉的原因：拿真實的
    epoch 當 `now`，`True`（＝epoch 第 1 秒）算出來是一個巨大的負數，落到退避那條
    路，結果跟「有擋」完全一樣，於是「只斷言結果落在合理區間」的測試怎麼改都是綠的。
    改成直接斷言「布林值算出來的等待，必須跟根本沒給 reset_at 一樣」。
    """
    for value in (True, False):
        assert (db._dorossi_usage_wait_seconds(_exc(reset_at=value), 1, now=0.0)
                == db._dorossi_usage_wait_seconds(_exc(), 1, now=0.0)), (
            f"{value!r} 被當成時間戳了——後端傳一個布林值就能決定要等多久")


def test_the_floor_holds_even_if_someone_configures_a_tiny_max(monkeypatch):
    """設定檔把 max 設成 10 秒不該把空轉防護關掉。"""
    monkeypatch.setattr(db, "DOROSSI_USAGE_WAIT_MAX_SEC", 10.0)
    assert db._dorossi_clamp_usage_wait(1.0) == db.DOROSSI_USAGE_WAIT_MIN_SEC
    assert db._dorossi_clamp_usage_wait(10_000.0) == db.DOROSSI_USAGE_WAIT_MIN_SEC


def test_the_wait_is_never_zero_no_matter_the_input():
    """這一條是「誤判成用量上限」的最後防線：無論輸入多離譜，兩次重試之間至少
    隔 MIN 秒，不會變成不停 spawn 後端行程的熱迴圈。"""
    for exc in (_exc(), _exc(reset_at=0), _exc(reset_at=-1), object(), None):
        assert db._dorossi_usage_wait_seconds(exc, 1) >= db.DOROSSI_USAGE_WAIT_MIN_SEC


def test_the_clamp_itself_refuses_to_pass_a_nan_through():
    """夾擠是這條路上的最後一道，所以它自己就得守住合約，不能靠上游剛好擋著。

    2026-09-08 實測舊版：`nan` 進、**`nan` 出**。而 `asyncio.sleep(nan)` 會丟
    ValueError，落點正是「已經撞到用量上限」的處理流程——那條路本來就是出事之後
    才走的，不該再被自己炸一次。
    """
    assert db._dorossi_clamp_usage_wait(float("nan")) == db.DOROSSI_USAGE_WAIT_MIN_SEC


def test_why_the_clamp_cannot_be_left_to_argument_order():
    """把「為什麼要一道明確的閘」釘在原地，而不只是釘現況。

    CPython 的 `max(a, b)` 是「先取 a，再看 `b > a`」，而 nan 的比較恆為 False，
    所以**結果完全取決於引數順序**。舊版寫的是 `max(secs, MIN)`——變數在前——於是
    nan 一路穿過去。這一支存在的理由是：下一個人把引數對調（或把 `min(max(...))`
    「整理」成別的寫法）不會有任何症狀，只有把這個機制寫下來才擋得住。
    """
    nan = float("nan")
    assert math.isnan(max(nan, 1.0)), "變數在前 → nan 穿過去"
    assert max(1.0, nan) == 1.0, "常數在前 → nan 被丟掉"
    assert math.isnan(min(nan, 5.0))
    assert min(5.0, nan) == 5.0


def test_the_nan_gate_did_not_swallow_the_infinities_too():
    """閘必須是 `isnan`，不是 `isfinite`。

    `inf` 與 `-inf` 現在的行為是對的**而且有意義**：`inf` 夾到上限（「就算後端報
    了一個荒謬的未來時刻，也最多睡 max 就再探一次」正是上限的用途），`-inf` 夾到
    下限。用 `isfinite` 一併擋掉會讓 `inf` 回下限——把「等到上限再探」偷偷換成
    「60 秒後再撞一次牆」，而且不會有任何症狀。
    """
    ceiling = max(db.DOROSSI_USAGE_WAIT_MAX_SEC, db.DOROSSI_USAGE_WAIT_MIN_SEC)
    assert db._dorossi_clamp_usage_wait(float("inf")) == ceiling
    assert db._dorossi_clamp_usage_wait(float("-inf")) == db.DOROSSI_USAGE_WAIT_MIN_SEC


def test_a_nan_reset_time_falls_back_exactly_like_a_missing_one():
    """上游那道 `if remain > 0` 是目前唯一擋著 nan 的東西，值得自己一支。

    它**不是為 nan 而寫的**（註解講的是「時間戳已經過去」），nan 過不了只是因為
    `nan > 0` 是 False。而來源真的給得出 nan——`reset_at` 一路從後端事件流過來，
    而 `json.loads` 預設就吃裸的 `NaN`。

    斷言寫成「跟根本沒給 reset_at 一模一樣」而不是「落在合理區間」：後者在把那道
    判斷放寬成 `>=` 之後照樣是綠的（`nan >= 0` 也是 False），區間斷言分不出
    「有擋」跟「剛好也擋住了」。
    """
    now = 1_700_000_000.0
    assert (db._dorossi_usage_wait_seconds(_exc(reset_at=float("nan")), 1, now=now)
            == db._dorossi_usage_wait_seconds(_exc(), 1, now=now)), (
        "nan 被當成時間戳算進等待秒數了")


def test_a_nan_reset_time_cannot_produce_a_nan_wait_at_any_attempt():
    """退避那條路每次加倍，所以要確認 nan 不會在某個 attempt 上突然漏出來。"""
    for attempt in (1, 2, 5, 50, 10 ** 6):
        delay = db._dorossi_usage_wait_seconds(
            _exc(reset_at=float("nan")), attempt, now=0.0)
        assert math.isfinite(delay), f"attempt={attempt} 算出非有限的等待秒數"
        assert delay >= db.DOROSSI_USAGE_WAIT_MIN_SEC


# ---------------------------------------------------------------------------
# 五、`retry-after` 標頭（API 後端）
# ---------------------------------------------------------------------------

def _sdk_error(retry_after):
    headers = {} if retry_after is None else {"retry-after": retry_after}
    return types.SimpleNamespace(
        response=types.SimpleNamespace(headers=headers))


@pytest.mark.parametrize("raw,expected", [
    ("30", 30.0),
    ("1.5", 1.5),
    (" 42 ", 42.0),
])
def test_retry_after_is_read_as_seconds(raw, expected):
    assert db._dorossi_api_retry_after_sec(_sdk_error(raw)) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [
    None, "", "0", "-5", "nan", "inf", "Infinity", "soon", "Wed, 21 Oct 2026 07:28:00 GMT",
])
def test_an_unusable_retry_after_is_dropped(raw):
    """`float("nan")` / `float("inf")` 都是**合法**的 float() 輸入，而 `nan <= 0` 是
    False、`inf` 更是直接放行——標頭是外部來的字串，不能只靠 `<= 0` 擋。"""
    assert db._dorossi_api_retry_after_sec(_sdk_error(raw)) is None


def test_retry_after_survives_an_sdk_object_with_no_response():
    assert db._dorossi_api_retry_after_sec(RuntimeError("boom")) is None


# ---------------------------------------------------------------------------
# 六、等待本身：abort 要秒級生效
# ---------------------------------------------------------------------------

class _FakeLoopState:
    def __init__(self, abort=False):
        self.abort = abort
        self.proc = None


# 每一支等待測試都必須有牆鐘上限。理由是實地踩到的：把 `deadline` 從
# `time.monotonic()` 換成 `time.time()` 之後，`remain` 變成「epoch 秒 - 開機
# 秒」＝十幾億，於是等待函式**永遠不會結束**。沒有上限的話這不是一支紅燈測試，
# 而是整個測試回合掛在那裡不動——比失敗難查得多，而且在無人值守的批次裡會把後面
# 全部卡住。有上限就退化成一句「等待沒有在 N 秒內結束」。
_WAIT_TEST_CAP_SEC = 5.0


def _run_wait(st, delay: float, cap: float = _WAIT_TEST_CAP_SEC):
    async def _body():
        return await asyncio.wait_for(
            b._dorossi_wait_for_usage_reset(st, delay), cap)
    try:
        return asyncio.run(_body())
    except TimeoutError:
        raise AssertionError(
            f"等待沒有在 {cap} 秒內結束（delay={delay}）——"
            "多半是 deadline 的時鐘用錯了，`time.time()` 減 `time.monotonic()` "
            "會得到十幾億秒") from None


def test_a_completed_wait_returns_true():
    st = _FakeLoopState()
    assert _run_wait(st, 0.05) is True


def test_an_already_aborted_wait_returns_false_immediately():
    st = _FakeLoopState(abort=True)
    assert _run_wait(st, 3600.0) is False


def test_an_abort_mid_wait_cuts_the_wait_short():
    """等待可能長達數小時，而這條路徑上 `st.proc` 是 None（沒有行程可殺），旗標輪詢
    是 `/dorossi abort` 唯一的著力點。這裡把輪詢間隔調小再實測它真的會醒。"""
    st = _FakeLoopState()

    async def _body():
        async def _flip():
            await asyncio.sleep(0.02)
            st.abort = True
        task = asyncio.ensure_future(_flip())
        got = await asyncio.wait_for(
            b._dorossi_wait_for_usage_reset(st, 3600.0), _WAIT_TEST_CAP_SEC)
        await task
        return got

    original = b._DOROSSI_USAGE_WAIT_POLL_SEC
    try:
        b._DOROSSI_USAGE_WAIT_POLL_SEC = 0.01
        assert asyncio.run(_body()) is False
    except TimeoutError:
        raise AssertionError(
            "abort 沒有在等待期間生效——旗標輪詢是這條路徑上唯一的著力點") from None
    finally:
        b._DOROSSI_USAGE_WAIT_POLL_SEC = original


def test_the_wait_uses_a_monotonic_clock():
    """等待橫跨數小時，期間的 NTP 校時／手動改時間／日光節約不該讓它變成幾秒或幾天。"""
    src = ast.parse((PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8"))
    fn = _find_func(src, "_dorossi_wait_for_usage_reset")
    calls = {_dotted(node.func) for node in ast.walk(fn)
             if isinstance(node, ast.Call)}
    assert "time.monotonic" in calls, "等待沒有用 time.monotonic"
    assert "time.time" not in calls, "等待用了牆鐘時間，會被校時打亂"


# ---------------------------------------------------------------------------
# 七、迴圈的 handler 形狀（AST——擋「以後有人改回去停掉」）
# ---------------------------------------------------------------------------

def _find_func(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"找不到 {name}()——它被改名或刪掉了")


def _find_method(tree: ast.AST, cls_name: str, name: str):
    """類別裡的那一支。**不能**用 `_find_func` 代替：兩個串流狀態類別各有一支
    `feed`，`_find_func` 會回檔案裡先出現的那一支（codex），檢查就跑去驗錯的對象
    而且照樣是綠的。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and sub.name == name:
                    return sub
            raise AssertionError(f"{cls_name} 裡找不到 {name}()")
    raise AssertionError(f"找不到 class {cls_name}——它被改名或刪掉了")


def _classifier_for(tree: ast.AST, entry: str, *needles: str):
    """回傳「真正做錯誤分類」的那一支函式。

    從叫用端出發：分類邏輯若還留在它自己身上就用它，否則跟著它呼叫的模組層函式
    找下去（2026-09-07 把 claude／codex 兩條的分類抽成 `*_stream_verdict`）。
    **這是刻意的**——寫死函式名的守門在「有人把邏輯搬走」時會失效，而搬走正是最
    容易發生的事；跟著呼叫走則連帶證明了那一支真的被叫用端執行到。
    """
    fn = _find_func(tree, entry)
    def _calls(node):
        return {_dotted(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)}
    if all(n in _calls(fn) for n in needles):
        return fn
    for name in sorted(_calls(fn)):
        try:
            sub = _find_func(tree, name)
        except AssertionError:
            continue
        if all(n in _calls(sub) for n in needles):
            return sub
    raise AssertionError(
        f"`{entry}` 自己沒有做分類，它呼叫的函式裡也沒有一支同時用到 {needles}")


def _dotted(node) -> str:
    bits = []
    while isinstance(node, ast.Attribute):
        bits.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        bits.append(node.id)
    return ".".join(reversed(bits))


def _usage_handler():
    src = ast.parse((PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8"))
    loop = _find_func(src, "_dorossi_run_loop")
    for node in ast.walk(loop):
        if isinstance(node, ast.ExceptHandler) \
                and _dotted(node.type) == "_DorossiUsageLimitError":
            return node
    raise AssertionError("_dorossi_run_loop 裡找不到用量上限的 except 區塊")


def test_the_loop_continues_instead_of_stopping():
    """這一支就是整個改動的重點，也是最容易被「順手簡化」掉的一行。

    舊版這個 handler 只有 `return`（貼一句「已達方案用量上限」就結束整個自走任務）。
    現在必須以 `continue` 收尾——等完額度回來，用同一個 prompt 重跑這一輪。
    """
    handler = _usage_handler()
    assert any(isinstance(n, ast.Continue) for n in ast.walk(handler)), (
        "用量上限的 handler 沒有 `continue`——它又變回「撞到就停」了")


def test_the_handler_actually_waits():
    handler = _usage_handler()
    calls = {_dotted(n.func) for n in ast.walk(handler) if isinstance(n, ast.Call)}
    assert "_dorossi_wait_for_usage_reset" in calls, "handler 沒有真的等待"
    assert "_dorossi_usage_wait_seconds" in calls, "handler 沒有算等待秒數"


def _calls_named(node, name):
    return [c for c in ast.walk(node)
            if isinstance(c, ast.Call) and _dotted(c.func) == name]


def test_the_handler_persists_the_session_before_waiting():
    """不存回 session id，等額度回來 resume 的就是上一輪的舊 id，這一輪做完的事
    全部白做。用量上限幾乎都是「做到一半」才撞上，所以這不是邊界案例。

    這一支刻意**不是**「`_dorossi_persist_advance` 這個名字有出現在 handler 裡」。
    那個版本寫過，而且被 mutation 打穿：把 `await _dorossi_state_rmw(...)` 整行拿
    掉，巢狀的 `_limit_save_mut` 仍然定義在 handler 裡，名字照樣掃得到，於是守門
    通過、行為卻已經退化成「等完額度 resume 舊 id」。要釘的是**機制**——那個
    mutator 真的被交給 rmw 執行過，而且發生在等待之前。
    """
    handler = _usage_handler()
    assert _calls_named(handler, "_dorossi_persist_advance"), (
        "handler 沒有把後端當下的 session id 寫回 slot")

    # handler 內部定義、且會寫回 session id 的 mutator（具名的那些）
    savers = {n.name for n in ast.walk(handler)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and _calls_named(n, "_dorossi_persist_advance")}

    rmw = [n for n in _calls_named(handler, "_dorossi_state_rmw")
           if any((isinstance(a, ast.Name) and a.id in savers)
                  or _calls_named(a, "_dorossi_persist_advance")
                  for a in n.args)]
    assert rmw, ("handler 準備好了 mutator 卻沒有交給 _dorossi_state_rmw 真的執行"
                 "——等額度回來會 resume 上一輪的舊 id，這一輪白做")

    waits = _calls_named(handler, "_dorossi_wait_for_usage_reset")
    assert waits, "handler 沒有真的等待"
    assert min(n.lineno for n in rmw) < min(n.lineno for n in waits), (
        "session id 是等待**之後**才寫回的——等待期間被 kill 就白做一輪")


def test_an_abort_during_the_wait_still_ends_the_loop():
    """等待函式回 False 時必須 `return`，不能忽略回傳值繼續跑——否則 `/dorossi abort`
    在等待期間按下去會沒有反應。"""
    handler = _usage_handler()
    guarded = [n for n in ast.walk(handler)
               if isinstance(n, ast.If)
               and any(_dotted(c.func) == "_dorossi_wait_for_usage_reset"
                       for c in ast.walk(n.test) if isinstance(c, ast.Call))]
    assert guarded, "等待函式的回傳值沒有被檢查"
    assert any(isinstance(x, ast.Return) for n in guarded for x in ast.walk(n)), (
        "等待被 abort 打斷時沒有結束迴圈")


def test_the_backoff_counter_is_reset_once_per_completed_round():
    """`usage_waits` 只在「有一輪真的跑完」時歸零。若歸零寫在 handler 裡，退避就永遠
    停在第一級，一個真的要等 5 小時的視窗會被拆成 20 次 15 分鐘的無效探測。

    這裡要找的是**迴圈裡**那一次歸零，不是迴圈外的初始化。先前的版本只問「有沒有
    一個在 handler 外面的 `usage_waits = 0`」——迴圈外的初始化自己就滿足了它，於是
    把每輪的歸零整段刪掉，測試照樣全綠。條件必須綁在包住 handler 的那個回合迴圈上。
    """
    # 一次 parse。`_usage_handler()` 自己會再 parse 一遍，兩棵樹的節點 id 永遠
    # 對不起來——底下要比對「handler 在不在這個迴圈裡」，就必須同一棵樹。
    loop = _find_func(ast.parse(
        (PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8")),
        "_dorossi_run_loop")
    handler = next(n for n in ast.walk(loop)
                   if isinstance(n, ast.ExceptHandler)
                   and _dotted(n.type) == "_DorossiUsageLimitError")
    in_handler = {id(n) for n in ast.walk(handler)}

    rounds = [n for n in ast.walk(loop)
              if isinstance(n, (ast.While, ast.For, ast.AsyncFor))
              and any(id(x) == id(handler) for x in ast.walk(n))]
    assert rounds, "用量上限的 handler 不在任何迴圈裡——它沒辦法重跑這一輪"
    innermost = min(rounds, key=lambda n: len(list(ast.walk(n))))

    resets = [n for n in ast.walk(innermost)
              if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "usage_waits"
                      for t in n.targets)
              and isinstance(n.value, ast.Constant) and n.value.value == 0
              and id(n) not in in_handler]
    assert resets, (
        "回合迴圈裡沒有「這一輪跑完就把 usage_waits 歸零」那一步"
        "（迴圈外的初始化不算）——退避會一路累加，或永遠停在第一級")


# ---------------------------------------------------------------------------
# 八、對外字串（Layer 1）
# ---------------------------------------------------------------------------

def test_the_wait_notice_says_it_will_resume_by_itself():
    """措辭必須讓擁有者知道**不必**手動接續，否則他仍然會去按
    `/dorossi session continue`，等於這個功能沒做。"""
    text = b._dorossi_usage_limit_wait_reply(_exc(), 900.0)
    assert "自動" in text and "續跑" in text
    assert "/dorossi abort" in text          # 不想等的出口


@pytest.mark.parametrize("exc", [
    db._DorossiUsageLimitError(
        r"claude -p exited 1: D:\Work\Example\x.log rate limit", None),
    db._dorossi_cc_usage_limit(
        {"result": r"rate limit; reset by peer writing D:\a\b.log"}, "", "s1"),
])
def test_the_wait_notice_never_leaks_the_raw_backend_text(exc):
    """例外訊息本身帶著原始診斷字串（主機路徑、CLI 名稱）。回覆只能用已經收斂過的
    `reset_hint`，絕不可把 `str(exc)` 組進去。"""
    text = b._dorossi_usage_limit_wait_reply(exc, 900.0)
    low = text.lower()
    for banned in (":\\", ":/", ".log", "claude", "exited", "rate limit"):
        assert banned not in low, f"{banned!r} 漏進了對外字串：{text!r}"


# ---------------------------------------------------------------------------
# 九、設定鍵
# ---------------------------------------------------------------------------

def test_the_new_config_keys_exist_with_the_documented_defaults():
    import _bot_config as bc
    d = bc._DEFAULT_BOT_CONFIG
    assert d["dorossi_usage_wait_fallback_sec"] == 900.0
    assert d["dorossi_usage_wait_max_sec"] == 21600.0
    # 0 ＝不設限。擁有者裁決不得有回合／花費類上限，而等待本身不花錢。
    assert d["dorossi_usage_wait_max_consecutive"] == 0


@pytest.mark.parametrize("bad", [0, -1, 0.5, "600", None, True, float("inf")])
def test_a_too_small_wait_cannot_be_configured(bad, tmp_path, monkeypatch):
    """把等待設成 0 不是「關掉功能」，是把空轉防護關掉。載入器必須 clamp。"""
    import json
    import _bot_config as bc
    cfg = tmp_path / "bot_config.json"
    cfg.write_text(json.dumps({"dorossi_usage_wait_fallback_sec": bad}),
                   encoding="utf-8")
    monkeypatch.setattr(bc, "BOT_CONFIG_FILE", cfg)
    loaded = bc.load_bot_config()
    assert loaded["dorossi_usage_wait_fallback_sec"] >= bc.DOROSSI_USAGE_WAIT_FLOOR_SEC


def test_a_reasonable_wait_still_gets_through(tmp_path, monkeypatch):
    """反向：正常值不能被 clamp 邏輯吃掉（不然上面那支只是在測一個永遠成立的事）。"""
    import json
    import _bot_config as bc
    cfg = tmp_path / "bot_config.json"
    cfg.write_text(json.dumps({"dorossi_usage_wait_fallback_sec": 1800.0,
                               "dorossi_usage_wait_max_sec": 7200.0,
                               "dorossi_usage_wait_max_consecutive": 4}),
                   encoding="utf-8")
    monkeypatch.setattr(bc, "BOT_CONFIG_FILE", cfg)
    loaded = bc.load_bot_config()
    assert loaded["dorossi_usage_wait_fallback_sec"] == 1800.0
    assert loaded["dorossi_usage_wait_max_sec"] == 7200.0
    assert loaded["dorossi_usage_wait_max_consecutive"] == 4


# ---------------------------------------------------------------------------
# 伺服器側暫時性故障（529 Overloaded／5xx）要「等一下再試」，不是停掉整個迴圈
#
# 2026-09-03 補，起因是實際發生的一次停擺：自走迴圈在 21:31:48 撞到
# `api_error_status=529`（訊息本身寫著 "usually temporary — try again in a
# moment"），既有的一次性重試把**工作階段丟掉重開**（對伺服器過載毫無幫助），
# 21:36:28 又一次 529，接著就落進泛用 `except` → `return`，整個無人值守的迴圈
# 就地結束，而且 `loop_pending.live` 被設成 false，連跨重啟自動接續都接不回來。
#
# 使用者當時問的正是「為什麼無法自動執行，有問題就重試？」——答案是這一類錯誤
# 當時被歸成致命錯誤。
# ---------------------------------------------------------------------------

_REAL_529 = {
    "subtype": "success", "is_error": True, "api_error_status": 529,
    "result": ("API Error: 529 Overloaded. This is a server-side issue, "
               "usually temporary — try again in a moment."),
}


def test_the_exact_529_that_stopped_the_loop_is_classified_transient():
    exc = db._dorossi_cc_transient_error(_REAL_529, "", "sess-abc")
    assert exc is not None, "這正是 2026-09-03 停掉迴圈的那一則，必須判成暫時性"
    assert exc.status == 529
    assert exc.session_id == "sess-abc", (
        "工作階段 id 沒帶上——等待後要用**同一個**工作階段續跑，"
        "否則這一輪做到一半的進度會被丟掉。")


@pytest.mark.parametrize("status", sorted(db.DOROSSI_TRANSIENT_STATUSES))
def test_every_declared_transient_status_is_detected(status):
    assert db._dorossi_cc_transient_error({"api_error_status": status}) is not None


def test_text_markers_work_without_a_status_code():
    """狀態碼不一定會被帶進 result 事件，有時只剩英文訊息。"""
    for text in ("Overloaded", "internal server error", "Bad Gateway",
                 "Service Unavailable", "gateway timeout"):
        assert db._dorossi_cc_transient_error({"result": text}) is not None, text


@pytest.mark.parametrize("label,ev", [
    ("用量上限 429", {"api_error_status": 429, "result": "rate limit exceeded"}),
    ("額度用盡 402", {"api_error_status": 402, "result": "insufficient credit"}),
    ("認證失敗", {"api_error_status": 401, "result": "invalid api key"}),
    ("一般崩潰", {"subtype": "error", "result": "Traceback ... ValueError"}),
    ("成功", {"subtype": "success", "result": "done"}),
    ("空事件", {}),
])
def test_other_failures_are_not_swallowed_as_transient(label, ev):
    """**反面**：這條路會讓迴圈無限等下去，所以判定寬了比窄了危險得多。

    429／402 尤其重要——它們有專屬的等待策略（等到額度重設，有確切時間），被誤判
    成暫時性故障就會退回瞎猜的指數退避。
    """
    assert db._dorossi_cc_transient_error(ev) is None, label


def test_the_usage_limit_check_runs_before_the_transient_check():
    """順序就是規則本身：用量上限也可能帶著 5xx 以外的字樣，而它等得比較準。

    用 AST 比對兩個呼叫在 rc!=0 那條路上的先後，不看原始碼字面順序。
    """
    src = (PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    usage_lines = [n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_dorossi_cc_usage_limit"]
    trans_lines = [n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_dorossi_cc_transient_error"]
    assert usage_lines and trans_lines
    # 暫時性判定必須排在**某一個**用量上限判定之後
    assert min(trans_lines) > min(usage_lines), (
        "暫時性故障的判定排到用量上限前面了——用量上限會被誤判成過載，"
        "然後用瞎猜的退避取代『等到額度重設』。")


def test_the_transient_check_runs_before_the_stale_session_retry():
    """resume 重試是「把工作階段丟掉重開」，對伺服器過載毫無幫助——只是再燒一次
    呼叫、又丟掉可以續接的對話。實際發生過：21:31 的 529 觸發了它，21:36 又一次。"""
    src = (PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 只比對**同一個函式內**的先後：`_DorossiResumeError` 在別的程式路徑也有一處
    # （2026-09-03 實測有兩個 raise 點），拿全檔最小行號比會對到不相干的那一個。
    pairs = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        trans = [n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_dorossi_cc_transient_error"]
        if not trans:
            continue
        resumes = [n.lineno for n in ast.walk(fn)
                   if isinstance(n, ast.Raise) and n.exc is not None
                   and isinstance(n.exc, ast.Call)
                   and isinstance(n.exc.func, ast.Name)
                   and n.exc.func.id == "_DorossiResumeError"]
        if resumes:
            pairs.append((fn.name, min(trans), min(resumes)))
    assert pairs, (
        "找不到「同一個函式裡同時有暫時性判定與 resume 重試」的地方——"
        "程式重構過的話這支測試要跟著改，否則它會安靜地什麼都不檢查。")
    for name, first_trans, first_resume in pairs:
        assert first_trans < first_resume, (
            f"{name}：暫時性判定排到 `_DorossiResumeError` 後面了——"
            "過載會再次觸發「丟掉工作階段重開」那條路。")


def test_the_backoff_grows_and_is_capped():
    waits = [db._dorossi_transient_wait_seconds(i) for i in range(1, 10)]
    assert waits[0] >= 30, "起步太短：伺服器過載時立刻重打只會加重它"
    assert all(b >= a for a, b in zip(waits, waits[1:])), "退避必須單調不減"
    assert max(waits) <= 900, "上限太長會錯過恢復"
    assert waits[-1] == waits[-2] == 900, "應該要封頂"


@pytest.mark.parametrize("junk", [0, -5, None, "x", 1.7])
def test_the_backoff_never_raises_on_junk(junk):
    assert db._dorossi_transient_wait_seconds(junk) >= 30


def test_the_loop_waits_and_continues_instead_of_returning():
    """行為的核心：這條路必須 `continue`（續跑），不可以 `return`（停掉迴圈）。

    用 AST 看 `_dorossi_run_loop` 裡那個 handler 的**直屬**語句——這正是原本的
    bug：泛用 handler 走的就是 `return`。
    """
    tree = ast.parse((PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_dorossi_run_loop")
    handler = None
    for node in ast.walk(fn):
        if (isinstance(node, ast.ExceptHandler) and node.type is not None
                and "_DorossiTransientError" in ast.unparse(node.type)):
            handler = node
    assert handler is not None, (
        "`_dorossi_run_loop` 沒有攔 `_DorossiTransientError`——"
        "伺服器暫時性故障會落進泛用 except，整個迴圈就地停掉。")
    body = ast.unparse(ast.Module(body=handler.body, type_ignores=[]))
    assert "continue" in body, (
        "handler 裡沒有 `continue`：等完之後必須續跑同一輪，不是結束迴圈。")

    # 這兩個 helper **定義了還不夠，要真的被呼叫**。變異測試抓到過：把
    # `await _dorossi_state_rmw(_tr_touch_mut)` 那一行刪掉，巢狀函式仍然在、
    # 字串比對照樣通過，而心跳從此不再被推。同一個坑在別處也踩過（建了物件卻
    # 沒呼叫它的方法），所以這裡比對的是「有沒有把它交給 `_dorossi_state_rmw`」。
    nested = {n.name: n for n in ast.walk(handler)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    applied = set()
    for call in ast.walk(handler):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "_dorossi_state_rmw"):
            for arg in call.args:
                if isinstance(arg, ast.Name) and arg.id in nested:
                    applied.add(arg.id)
    applied_src = " ".join(ast.unparse(nested[name]) for name in applied)
    assert "_dorossi_touch_loop_pending" in applied_src, (
        "心跳沒有真的被推（helper 可能定義了卻沒交給 `_dorossi_state_rmw`）——"
        "一次長等待會讓跨重啟自動接續把標記判定成過舊，"
        "正好在最該接回去的時候不接。")
    assert "_dorossi_persist_advance" in applied_src, (
        "工作階段 id 沒有真的被保住（helper 可能定義了卻沒被呼叫），"
        "等待後會 resume 到上一輪的舊 id，這一輪做完的事全部白做。")


# ---------------------------------------------------------------------------
# codex（GPT）那一側也要「等到額度回復再續跑」
#
# 2026-09-05 補。在這之前 codex 路徑**完全沒有**用量上限與暫時性故障的判定：
# `_dorossi_via_codex` 的 rc!=0 只有「有 session_id → 丟掉工作階段重開一次 →
# 否則 RuntimeError」。GPT 撞到上限時，迴圈會先白白開一個新工作階段（一樣撞上），
# 然後把整個無人值守任務判死。Claude 那側 2026-08-31 就已經改成等待續跑。
#
# 下面的訊息字樣取自上游 issue 與官方說明的實際輸出，不是憑空編的。
# ---------------------------------------------------------------------------

_CODEX_TPM = ("Rate limit reached for o4-mini in organization org-x on tokens "
              "per min (TPM): Limit 200000, Used 162582, Requested 45297. "
              "Please try again in 2.363s.")
_CODEX_WEEKLY = "You've hit your usage limit. Try again in 4 days 2 hours 46 minutes."
_CODEX_5H = "usage limit reached for your 5h limit; resets in 47 minutes"


@pytest.mark.parametrize("text", [_CODEX_TPM, _CODEX_WEEKLY, _CODEX_5H,
                                  "stream error: 429 Too Many Requests",
                                  "insufficient_quota"])
def test_codex_usage_limits_are_recognised(text):
    exc = db._dorossi_codex_usage_limit(text, "th-1")
    assert exc is not None, f"沒認出用量上限：{text[:60]}"
    assert isinstance(exc, db._DorossiUsageLimitError), (
        "必須丟既有的用量上限例外——自走迴圈已經知道怎麼等它，"
        "換成新例外等於要重寫一次等待邏輯。")
    assert exc.session_id == "th-1", (
        "沒帶上 thread id：等額度回來後會 resume 到上一輪的舊對話，"
        "這一輪做到一半的進度全部白做。")


@pytest.mark.parametrize("text,expect_sec", [
    (_CODEX_TPM, 2),
    (_CODEX_WEEKLY, 4 * 86400 + 2 * 3600 + 46 * 60),
    (_CODEX_5H, 47 * 60),
    ("Rate limit reached. Please try again in 45.622s.", 45),
])
def test_relative_reset_times_are_converted(text, expect_sec):
    """相對寫法可以安全換算——這與 `_dorossi_extract_reset_epoch` 拒絕換算
    「resets 3:45pm」不衝突：那支拒絕的理由是**沒有時區**，而「in 4 days」不管在
    哪個時區都是同一段長度。

    **這支測試抓到過真的 bug**：第一版的擷取用限縮字元集，會在 "4 days" 的 `a`
    就停下來，2 小時 46 分無聲消失，等待長度短了將近兩小時。
    """
    got = db._dorossi_extract_retry_after_seconds(text)
    assert got is not None, f"沒抓到相對時間：{text[:60]}"
    assert abs(got - expect_sec) < 2, f"{got} != {expect_sec}"


@pytest.mark.parametrize("text", [
    "Limit 200000, Used 162582, Requested 45297",   # 有數字但不是等待時間
    "thread panicked at src/main.rs:42",
    "usage limit reached",                           # 是上限，但沒給時間
    "", None,
])
def test_no_duration_is_invented_from_unrelated_numbers(text):
    """猜錯等待長度比不猜更糟：猜長了整個任務白停，猜短了瘋狂重打。
    抓不到就回 None，讓迴圈走退避探測。"""
    assert db._dorossi_extract_retry_after_seconds(text) is None


@pytest.mark.parametrize("text", [
    "Rate limit reached. Please try again in 0s.",
    "Usage limit reached. Resets in 0 minutes.",
])
def test_a_zero_length_wait_is_not_a_duration(text):
    """「0 秒後再試」不是一個等待長度：回 None，讓迴圈走退避探測。這一格在分支覆蓋率
    裡從來沒有成立過（2026-09-22 量），而它擋的正是 0 秒等待的熱迴圈。"""
    assert db._dorossi_extract_retry_after_seconds(text) is None


def test_an_absurd_wait_is_clamped_to_a_week():
    """上游偶爾吐出離譜的長度，而這個值會直接變成 sleep 的秒數——夾在 7 天。沒有這支，
    把上緣拿掉照樣全綠，一句「try again in 400 days」就會讓自走迴圈睡一年多。"""
    got = db._dorossi_extract_retry_after_seconds(
        "Usage limit reached. Please try again in 400 days.")
    assert got == 7 * 86400.0, got
    # 對照：上緣以內的照實換算，證明上面那個 7 天是夾出來的，不是算錯。
    assert db._dorossi_extract_retry_after_seconds(
        "Usage limit reached. Please try again in 6 days.") == 6 * 86400.0


def test_codex_transient_errors_are_separated_from_usage_limits():
    """兩者的等待策略不同：上限等到重設（有時間可以算），過載只能指數退避。"""
    for text in ("stream error: 503 Service Unavailable",
                 "upstream overloaded", "502 Bad Gateway"):
        assert db._dorossi_codex_transient(text, "th") is not None, text
        assert db._dorossi_codex_usage_limit(text, "th") is None, text


@pytest.mark.parametrize("text", [_CODEX_TPM, _CODEX_WEEKLY,
                                  "stream error: 429 Too Many Requests"])
def test_a_usage_limit_is_never_downgraded_to_transient(text):
    """順序就是規則本身。429／配額被當成「伺服器忙碌」的話，會用瞎猜的退避取代
    「等到額度重設」，而那個差別可以是好幾天。"""
    assert db._dorossi_codex_transient(text, "th") is None


@pytest.mark.parametrize("text", ["thread panicked at src/main.rs:42",
                                  "no such file or directory", ""])
def test_ordinary_codex_failures_still_fall_through(text):
    """**反面**：這兩條路都會讓迴圈等下去，所以判定寬了比窄了危險得多。
    一般崩潰必須繼續走原本的 resume 重試／RuntimeError。"""
    assert db._dorossi_codex_usage_limit(text, "th") is None
    assert db._dorossi_codex_transient(text, "th") is None


def test_the_codex_path_classifies_before_the_stale_session_retry():
    """resume 重試是「丟掉工作階段重開」——對額度用完與伺服器過載都毫無幫助，
    只是再燒一次呼叫。2026-09-05 之前 codex 這側**只有**那條路。"""
    tree = ast.parse((PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8"))
    fn = _classifier_for(tree, "_dorossi_via_codex",
                         "_dorossi_codex_usage_limit", "_dorossi_codex_transient")
    usage = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_dorossi_codex_usage_limit"]
    trans = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_dorossi_codex_transient"]
    resume = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Raise)
              and n.exc is not None and isinstance(n.exc, ast.Call)
              and isinstance(n.exc.func, ast.Name)
              and n.exc.func.id == "_DorossiResumeError"]
    assert usage and trans and resume, (usage, trans, resume)
    assert max(usage) < min(trans) < min(resume), (
        f"分類順序錯了（usage={usage} transient={trans} resume={resume}）。"
        "必須是「用量上限 → 暫時性 → resume 重試」。")


def test_codex_failure_event_text_is_collected():
    """codex 走 JSON 事件輸出時，上限訊息常常只在事件裡、stderr 是空的。
    收不到那段文字，分類就會全部落空、退回舊行為。"""
    tree = ast.parse((PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8"))
    # 三處分別在三個單元裡（2026-09-07 抽離之後）：狀態物件初始化、折疊時收集、
    # 判定時使用。**三處都要**——少了收集那一處，分類拿到的永遠是空字串，而
    # 「初始化 ＋ 使用」兩處還在，只看某一支函式的守門會全綠。
    where = {
        "初始化": _find_method(tree, "_CodexStreamState", "__init__"),
        "折疊時收集": _find_method(tree, "_CodexStreamState", "feed"),
        "rc!=0 時使用": _find_func(tree, "_codex_stream_verdict"),
    }
    for label, node in where.items():
        assert "failure_texts" in ast.unparse(node), (
            f"`{label}` 那一段沒有碰到 failure_texts——codex 走 JSON 事件輸出時，"
            "上限訊息常常只在事件裡，收不到就會退回舊行為")
    # 收集與使用之間靠的是同一個屬性，不是同名的區域變數。
    fold_attrs = {n.attr for n in ast.walk(where["折疊時收集"])
                  if isinstance(n, ast.Attribute) and n.attr == "failure_texts"}
    use_attrs = {n.attr for n in ast.walk(where["rc!=0 時使用"])
                 if isinstance(n, ast.Attribute) and n.attr == "failure_texts"}
    assert fold_attrs and use_attrs, (
        "failure_texts 出現了，但不是狀態物件的屬性——那兩半就沒有接起來")


# ---------------------------------------------------------------------------
# 任務異常 → 有上限地自動重試（2026-09-05）
#
# 迴圈原本只有三種會續跑的錯誤（用量上限、暫時性故障、以及 09-03 加的那條），
# 其餘**任何**例外都是「貼一句錯誤、`return`、整個無人值守任務結束」；輸出靜默
# 也一樣直接結束。對一個跑整夜的任務來說，那代表後端行程被系統殺掉、一次網路
# 抖動、或後端某一輪卡住，都會讓它整夜停在那裡等人接。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [
    FileNotFoundError("codex CLI not found on PATH"),
    PermissionError("permission denied"),
    RuntimeError("claude_code CLI backend unavailable or not authenticated."),
])
def test_configuration_failures_are_fatal(exc):
    """設定／環境本身壞掉時重試沒有意義，只會把「壞掉」變成「安靜地一直壞」。"""
    assert db.dorossi_error_is_fatal(exc) is True


@pytest.mark.parametrize("exc", [
    RuntimeError("codex invocation failed"),
    TimeoutError("claude -p exceeded 3600s hard wall-clock limit"),
    OSError("connection reset by peer"),
    Exception("something unexpected"),
])
def test_incidental_failures_are_retryable(exc):
    """保守方向刻意選「可重試」：判錯成致命 → 無人值守任務白停一整夜；
    判錯成可重試 → 最多多試幾次（有上限）然後照樣停下來。代價差很多。"""
    assert db.dorossi_error_is_fatal(exc) is False


def test_the_fatal_check_never_raises():
    class Nasty(Exception):
        def __str__(self):
            raise ValueError("boom")
    assert db.dorossi_error_is_fatal(Nasty()) in (True, False)
    for junk in (None, 123, "str"):
        assert db.dorossi_error_is_fatal(junk) in (True, False)


def test_the_error_retry_backoff_grows_and_is_capped():
    waits = [db.dorossi_error_retry_wait_seconds(i) for i in range(1, 8)]
    assert all(b >= a for a, b in zip(waits, waits[1:])), "退避必須單調不減"
    assert waits[0] >= 10, "起步太短會變成熱迴圈"
    assert max(waits) <= 300, (
        "封頂太長：這裡多半是本機偶發失敗，不像伺服器過載需要給對方時間恢復")


@pytest.mark.parametrize("junk", [0, -1, None, "x"])
def test_the_error_retry_backoff_never_raises(junk):
    assert db.dorossi_error_retry_wait_seconds(junk) > 0


@pytest.mark.parametrize("handler,counter,cap_const", [
    ("Exception", "error_retries", "DOROSSI_ERROR_RETRY_MAX"),
    ("_DorossiLoopSilence", "silence_retries", "DOROSSI_SILENCE_RETRY_MAX"),
])
def test_the_loop_retries_before_giving_up(handler, counter, cap_const):
    """兩條路都必須「先退避重試、超過上限才 return」。

    用 AST 檢查 handler 的**直屬**內容：`continue`（會續跑）、計數器、上限常數、
    以及心跳推送——少了心跳，一次長等待會讓跨重啟自動接續把標記判定成過舊。
    """
    tree = ast.parse((PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_dorossi_run_loop")
    # `_dorossi_run_loop` 裡有**好幾個** `except Exception`（貼訊息失敗的吞例外等
    # 等），所以不能用「最後一個」來選——要用內容選出這一輪的那一個。
    target = None
    for node in ast.walk(fn):
        if (isinstance(node, ast.ExceptHandler) and node.type is not None
                and ast.unparse(node.type).strip() == handler
                and counter in ast.unparse(
                    ast.Module(body=node.body, type_ignores=[]))):
            target = node
    assert target is not None, (
        f"`_dorossi_run_loop` 找不到帶著 `{counter}` 的 `except {handler}`")
    body = ast.unparse(ast.Module(body=target.body, type_ignores=[]))
    assert "continue" in body, (
        f"`except {handler}` 沒有 `continue`——一次失敗就結束整個無人值守任務。")
    assert counter in body, f"沒有重試計數器 {counter}"
    assert "return" in body, (
        "完全沒有 `return`——超過上限之後必須停下來，不能無限重試。")

    # **只檢查「字有沒有出現」是不夠的**（變異測試實測）：在計數器後面插一行
    # 無條件 `return`，`continue`／計數器／上限常數全都還在，字面比對照樣通過，
    # 而行為已經退回「一次失敗就結束」。所以這裡看的是**控制流**。
    top_level = list(target.body)
    for stmt in top_level:
        if isinstance(stmt, ast.Return):
            raise AssertionError(
                f"`except {handler}` 的最外層有一個**無條件** `return`"
                f"（第 {stmt.lineno} 行）——重試那條路永遠走不到。"
                "放棄一定要包在上限判斷的 `if` 裡。")

    # 上限常數必須用在**判斷式**裡，不能只出現在訊息字串。把
    # `if (... or n > MAX):` 改成 `if False:` 之後常數仍在 f-string 裡，
    # 字面比對照樣通過，而重試就變成無限次了。
    in_condition = any(
        cap_const in ast.unparse(node.test)
        for node in ast.walk(target) if isinstance(node, ast.If))
    assert in_condition, (
        f"{cap_const} 沒有出現在任何 `if` 的判斷式裡（只出現在訊息字串不算）"
        "——那等於沒有上限，壞掉的後端會被無限重試。")

    # 心跳必須真的被推（不是只定義一個 mutator 卻沒交出去）。
    nested = {n.name: n for n in ast.walk(target)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    applied = " ".join(
        ast.unparse(nested[a.id])
        for c in ast.walk(target)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        and c.func.id == "_dorossi_state_rmw"
        for a in c.args if isinstance(a, ast.Name) and a.id in nested)
    assert "_dorossi_touch_loop_pending" in applied, (
        f"`except {handler}` 沒有真的推心跳——長等待會讓跨重啟自動接續"
        "把標記判定成過舊，正好在最該接回去的時候不接。")


def test_a_fatal_error_still_stops_immediately():
    """反面：致命錯誤不可以被退避重試拖 N 次才停——那只是延後 stop 並洗版。"""
    tree = ast.parse((PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_dorossi_run_loop")
    target = None
    for node in ast.walk(fn):
        if (isinstance(node, ast.ExceptHandler) and node.type is not None
                and ast.unparse(node.type).strip() == "Exception"
                and "error_retries" in ast.unparse(
                    ast.Module(body=node.body, type_ignores=[]))):
            target = node
    assert target is not None, "找不到這一輪的泛用 handler"
    body = ast.unparse(ast.Module(body=target.body, type_ignores=[]))
    assert "dorossi_error_is_fatal" in body, (
        "泛用 handler 沒有分辨致命錯誤——CLI 不存在／未認證會被重試三次才停。")


# ---------------------------------------------------------------------------
# 診斷不可以說謊（2026-09-05）
#
# `_dorossi_error_hint` 對外送的是泛用字串（Layer 1，正確），但 stderr 那一行原本
# **無論什麼失敗**都印「CLI backend unavailable or not authenticated」。2026-09-03
# 那次 529 打死自走迴圈時，log 留下的正是這句——於是查的人先去看認證，而真正的原因
# 是對方在忙。診斷說謊比沒有診斷更貴。
# ---------------------------------------------------------------------------


def _hint_stderr(exc, backend="claude_code"):
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = b._dorossi_error_hint(backend, exc)
    return out, err.getvalue()


def test_an_overload_is_not_reported_as_an_auth_problem():
    _out, log = _hint_stderr(
        db._DorossiTransientError("API Error: 529 Overloaded", status=529))
    assert "not authenticated" not in log, (
        "把伺服器過載診斷成認證問題——2026-09-03 就是被這句帶偏的。")
    # 光是「log 裡有 overload 這個字」不夠：把整條分支停用之後，泛用分支會印
    # `repr(exc)`，而那串字本身就含 "Overloaded"，斷言照樣成立（變異測試實測
    # 存活）。要釘的是**這一條分支特有的判斷**——它告訴讀的人「這會自己好」，
    # 泛用分支給不出這個資訊。
    assert "clears by itself" in log, (
        "沒有走過載那條專屬診斷——讀 log 的人看不出這是暫時性的，"
        "會跑去查認證或設定。")
    assert "status=529" in log, "沒有把狀態碼帶進診斷"


def test_a_real_configuration_failure_still_says_so():
    """**反面**：不要為了修上面那條就把真的認證／設定問題也含糊掉。"""
    _out, log = _hint_stderr(FileNotFoundError("codex CLI not found on PATH"))
    assert "not authenticated" in log or "unavailable" in log


def test_an_unclassified_failure_reports_the_actual_exception():
    _out, log = _hint_stderr(RuntimeError("something odd happened"))
    assert "something odd happened" in log, (
        "沒分類的失敗至少要把原始例外印出來，不要猜一個原因。")
    assert "not authenticated" not in log


@pytest.mark.parametrize("exc", [
    db._DorossiTransientError("API Error: 529 Overloaded", status=529),
    FileNotFoundError("codex CLI not found on PATH"),
    RuntimeError("connection reset by peer while writing D:/logs/x.log"),
])
def test_the_discord_facing_text_stays_generic(exc):
    """Layer 1：改的是 stderr，對外字串一個字都不該變——它不得夾帶原始例外、
    主機路徑或後端名稱。"""
    out, _log = _hint_stderr(exc)
    assert out == "Dorossi 暫時無法回應，請稍後再試。"
    for banned in ("529", "claude", "codex", "D:/", "Overloaded", "PATH"):
        assert banned not in out


def test_a_rejected_cli_option_is_diagnosed_as_an_old_cli_not_as_auth():
    """`_DorossiCliOptionError` 也在致命清單裡，所以它的專屬診斷必須排在「無法使用或
    未認證」那條**前面**——否則讀 log 的人會去查認證，而該做的事是更新 CLI。
    對外字串照舊一個字都不變（Layer 1）。"""
    out, log = _hint_stderr(db._DorossiCliOptionError("--tools"))
    assert out == "Dorossi 暫時無法回應，請稍後再試。"
    assert "not authenticated" not in log, log
    assert "rejected the option '--tools'" in log, log
    assert "update it" in log, log
    assert "--tools" not in out


@pytest.mark.parametrize("backend", ["claude_code", "codex"])
def test_a_vanished_working_directory_is_named_as_such(backend):
    """存著的工作目錄不在了（`_DorossiWorkdirError`）：stderr 講工作目錄、不講認證；
    對外那一句要讓人知道**這不會自己好**，並指一條使用者做得到的路。

    修正前這個狀況是子行程丟的 `NotADirectoryError`——`dorossi_error_is_fatal` 判成
    致命，於是 stderr 印「CLI backend unavailable or not authenticated」，對外說「請
    稍後再試」。兩句都錯：一個指錯原因，一個把永久的狀況說成暫時的。
    """
    out, log = _hint_stderr(
        db._DorossiWorkdirError("working directory is not a usable directory"),
        backend=backend)
    assert "working directory" in log, log
    assert "not authenticated" not in log, (
        "把「目錄不在了」診斷成認證問題——這正是修正前的那一句。")
    assert out != "Dorossi 暫時無法回應，請稍後再試。", (
        "對外還是「請稍後再試」——這個狀況不會自己好。")
    assert "/dorossi session new" in out, out
    for banned in ("claude", "codex", ":\\", "PATH"):
        assert banned not in out, (banned, out)
# ---------------------------------------------------------------------------
# 泛用 `except` 必須排在具名例外**後面**
#
# 這一段的分類順序（用量上限 → 暫時性故障 → 輸出靜默 → 未預期錯誤 → 致命）在後端
# 那一側已經有守門（`rc != 0` 那條路上的呼叫先後）。迴圈這一側卻沒有：本檔其餘的
# AST 測試比對的是每個 handler 的**內容**，順序換了它們照樣過。
#
# Python 的 `except` 是逐一比對的，所以把泛用那條搬到前面，三個具名 handler 就整段
# 變成死碼：用量上限會被當成「未預期錯誤」重試三次然後放棄（後端根本還沒放行，
# 三次退避加起來連兩分鐘都不到）、暫時性過載不再走它自己的長退避、輸出靜默不再重生。
# **而且沒有任何症狀**——不會拋錯、不會有警告、型別檢查也不管。
# ---------------------------------------------------------------------------

_LOOP_NAMED_HANDLERS = ("_DorossiLoopSilence", "_DorossiUsageLimitError",
                        "_DorossiTransientError")


def _round_try_handlers():
    """自走迴圈那個 `try` 的 handler 型別名稱，依原始順序。"""
    tree = ast.parse((PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_dorossi_run_loop")
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        names = ["bare" if h.type is None else ast.unparse(h.type).strip()
                 for h in node.handlers]
        if any(n in _LOOP_NAMED_HANDLERS for n in names):
            return names
    raise AssertionError(
        "`_dorossi_run_loop` 裡找不到攔具名 Dorossi 例外的那個 `try`")


def test_the_catch_all_handler_stays_last_in_the_round_try():
    names = _round_try_handlers()
    missing = [n for n in _LOOP_NAMED_HANDLERS if n not in names]
    assert not missing, (
        f"自走迴圈的 `try` 少了具名 handler {missing}——那一類錯誤會掉進泛用的"
        f"那條路，用它的退避與上限。目前的順序：{names}")
    broad = [i for i, n in enumerate(names) if n in ("Exception", "bare")]
    assert broad, f"沒有泛用 handler，未預期錯誤會直接炸出迴圈：{names}"
    assert max(broad) == len(names) - 1 and len(broad) == 1, (
        f"泛用 handler 不是最後一個：{names}")
    last_named = max(names.index(n) for n in _LOOP_NAMED_HANDLERS)
    assert last_named < broad[0], (
        f"泛用 handler 排在具名的前面，後面那些整段是死碼：{names}")


# ---------------------------------------------------------------------------
# 三、api 後端的分類要跟另外兩條後端一致
#
# 2026-09-05：`_dorossi_via_api` 只把 429／402 映成用量上限，**5xx／529 一律裸
# `raise`**。落進自走迴圈的泛用 `except` 之後用的是 20s→40s→80s 最多三次的短退避
# （合計不到兩分半），而另外兩條後端走的是 30s 起、封頂 15 分、最多 20 輪的專屬
# 長退避。同一場過載，換一個後端就從「撐三小時」變成「撐兩分半」——而且完全看不
# 出來，因為兩種都會重試、只是次數與間隔不同。
#
# 這一段釘的是**跨後端一致**，不是某個字串：三條路都要把「伺服器忙」與「額度用完」
# 分成兩件事，而且順序都是用量上限先判。
# ---------------------------------------------------------------------------

class _FakeAPIError(Exception):
    """模仿 SDK 的 `APIStatusError`：帶 `status_code`，訊息是外部文字。"""

    def __init__(self, message: str, status_code=None):
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize("status", sorted(db.DOROSSI_TRANSIENT_STATUSES))
def test_a_server_side_status_is_transient_on_the_api_backend(status):
    exc = db._dorossi_api_transient_error(
        _FakeAPIError("something went wrong", status_code=status))
    assert exc is not None, status
    assert isinstance(exc, db._DorossiTransientError)
    assert exc.status == status


@pytest.mark.parametrize("text", [
    "Overloaded",
    "overloaded_error: the service is overloaded",
    "Internal server error",
    "Bad gateway",
    "Service Unavailable",
    "gateway timeout",
    "temporarily unavailable, try again",
])
def test_the_api_backend_also_reads_the_message_when_there_is_no_status(text):
    """連線層的失敗沒有狀態碼，只留下文字——狀態碼那一半擋不住它。"""
    exc = db._dorossi_api_transient_error(_FakeAPIError(text))
    assert exc is not None, text


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_client_side_status_is_not_transient(status):
    """4xx 是我們自己送錯了，重試一百次還是錯——不能吃掉重試預算。"""
    assert db._dorossi_api_transient_error(
        _FakeAPIError("bad request", status_code=status)) is None


def test_a_rate_limit_is_not_classified_as_a_server_hiccup():
    """429 不在暫時性清單裡：它有 `retry-after`，等得比指數退避準得多。"""
    assert 429 not in db.DOROSSI_TRANSIENT_STATUSES
    assert db._dorossi_api_transient_error(
        _FakeAPIError("rate_limit_error", status_code=429)) is None


def test_an_ordinary_error_is_not_transient():
    for exc in (ValueError("nope"), RuntimeError("anthropic SDK client unavailable"),
                _FakeAPIError("model not found", status_code=404)):
        assert db._dorossi_api_transient_error(exc) is None, repr(exc)


def test_the_api_transient_probe_never_raises():
    class _Hostile(Exception):
        @property
        def status_code(self):
            raise RuntimeError("boom")

    # `getattr` 會把 property 的例外原樣丟出來，所以這一支同時證明呼叫端不會被
    # 一個壞掉的 SDK 例外物件拖垮。
    try:
        db._dorossi_api_transient_error(_Hostile("x"))
    except RuntimeError as err:
        pytest.fail(f"探測本身炸了：{err!r}")


def test_every_backend_classifies_a_server_hiccup(tmp_path):
    """三條後端都必須有「伺服器暫時性故障」的判定，而且都排在用量上限後面。

    這是本輪補 api 那一條的理由：少一條路就等於「換個後端，同一場過載的耐受度
    從三小時掉到兩分半」，而且不會有任何訊號。
    """
    tree = ast.parse((PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8"))
    for fn_name, usage_fn, transient_fn in (
            ("_dorossi_via_api", "_dorossi_api_is_usage_limit",
             "_dorossi_api_transient_error"),
            ("_dorossi_via_codex", "_dorossi_codex_usage_limit",
             "_dorossi_codex_transient"),
            ("_dorossi_via_claude_code", "_dorossi_cc_usage_limit",
             "_dorossi_cc_transient_error")):
        # 分類可能就在叫用端，也可能被抽到它呼叫的判定函式裡（claude／codex 於
        # 2026-09-07 抽離）。跟著呼叫走，而不是寫死名字——見 `_classifier_for`。
        fn = _classifier_for(tree, fn_name, usage_fn, transient_fn)
        order = [n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id in (usage_fn, transient_fn)]
        assert transient_fn in order, (
            f"`{fn_name}` 沒有判定伺服器暫時性故障——那條路的過載會被當成未預期"
            "錯誤，用短很多的退避處理。")
        assert usage_fn in order, f"`{fn_name}` 沒有判定用量上限"
        assert order.index(usage_fn) < order.index(transient_fn), (
            f"`{fn_name}` 先問了「是不是伺服器忙」——用量上限會被吃掉，"
            "改用瞎猜的退避取代它自己精準的等待時間。")


# ---------------------------------------------------------------------------
# api 後端的「用量上限」那一半
#
# `_dorossi_api_transient_error` 上面已經測過了，但它在 `_dorossi_via_api` 的
# `except` 區塊裡**排在第二**——真正先跑的是 `_dorossi_api_is_usage_limit`。
# 2026-09-05 補這一段時才發現：兩支吃的是同一個第三方例外物件、面對同樣的敵意
# 輸入，卻只有第二支有自我保護。第一支先炸的話，第二支那層防護根本不會被執行到。
# ---------------------------------------------------------------------------

class _FakeHeaders(dict):
    """`resp.headers`：SDK 給的是個支援 `.get()` 的映射。"""


class _FakeResponse:
    def __init__(self, headers):
        self.headers = _FakeHeaders(headers or {})


class _FakeRateLimit(_FakeAPIError):
    """站在 `rate_err` 位置的假 SDK 型別。"""


@pytest.mark.parametrize("status", [429, 402])
def test_a_quota_status_is_a_usage_limit(status):
    assert db._dorossi_api_is_usage_limit(
        _FakeAPIError("nope", status_code=status), ()) is True


@pytest.mark.parametrize("text", [
    "rate_limit_error", "Rate limit exceeded",
    "You have hit your usage limit", "billing: payment required",
    "RATE LIMIT",
])
def test_the_message_alone_is_enough_when_there_is_no_status(text):
    """連線層／包裝過的例外沒有狀態碼，只留下文字。"""
    assert db._dorossi_api_is_usage_limit(_FakeAPIError(text), ()) is True


def test_the_sdk_type_is_honoured_when_the_sdk_is_present():
    assert db._dorossi_api_is_usage_limit(
        _FakeRateLimit("anything at all"), _FakeRateLimit) is True


def test_an_absent_sdk_does_not_break_the_probe():
    """`rate_err` 是 `()`（SDK 沒裝時的值）。`isinstance(x, ())` 永遠是 False，
    但這裡靠的是前面那個 `if rate_err` 短路——空 tuple 是 falsy。"""
    assert db._dorossi_api_is_usage_limit(_FakeAPIError("boom", status_code=500), ()) is False


@pytest.mark.parametrize("status", sorted(db.DOROSSI_TRANSIENT_STATUSES))
def test_a_server_hiccup_is_not_a_usage_limit(status):
    """方向很重要：把過載誤判成用量上限，自走迴圈會為了一個永遠不會到的額度重設
    時間睡上好幾個小時；反過來只是退避得笨一點。"""
    assert db._dorossi_api_is_usage_limit(
        _FakeAPIError("Overloaded", status_code=status), ()) is False


def test_the_usage_limit_probe_never_raises_on_a_hostile_status_code():
    """`getattr(exc, "status_code", None)` 的預設值**只吃 AttributeError**；
    property 拋出來的其他例外照樣往外丟。這一支在修正前是紅的。"""
    class _Hostile(Exception):
        @property
        def status_code(self):
            raise RuntimeError("boom")

    try:
        assert db._dorossi_api_is_usage_limit(_Hostile("x"), ()) is False
    except RuntimeError as err:
        pytest.fail(f"用量上限判定本身炸了：{err!r}")


def test_the_usage_limit_probe_never_raises_on_a_hostile_str():
    class _Hostile(Exception):
        status_code = None

        def __str__(self):
            raise ValueError("nope")

    try:
        assert db._dorossi_api_is_usage_limit(_Hostile(), ()) is False
    except ValueError as err:
        pytest.fail(f"用量上限判定本身炸了：{err!r}")


def test_the_usage_limit_probe_never_raises_on_a_hostile_rate_err():
    """`rate_err` 是從 SDK `getattr` 來的，不保證是型別；`isinstance` 對非型別
    第二引數會丟 `TypeError`。"""
    try:
        assert db._dorossi_api_is_usage_limit(_FakeAPIError("x"), "not-a-type") is False
    except TypeError as err:
        pytest.fail(f"用量上限判定本身炸了：{err!r}")


def test_both_api_probes_protect_themselves():
    """**這一支是這一段的重點。** 兩支分類函式都在同一個 `except` 區塊裡被呼叫、
    吃的是同一個第三方例外物件，所以必須有同一層自我保護——而且是「整段包住」，
    不是包住其中一行。

    只測其中一支不夠：修正前 `_dorossi_api_transient_error` 有防護、
    `_dorossi_api_is_usage_limit` 沒有，而後者**排在前面**，於是前者那層防護
    在真正遇到敵意物件時一次也不會被執行到。兩支的行為測試各自都是綠的。
    """
    tree = ast.parse((PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8"))
    for name in ("_dorossi_api_is_usage_limit", "_dorossi_api_transient_error"):
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == name), None)
        assert fn is not None, f"{name} 不見了——改名的話這支守門要跟著改"
        body = [n for n in fn.body
                if not (isinstance(n, ast.Expr)
                        and isinstance(n.value, ast.Constant)
                        and isinstance(n.value.value, str))]
        assert len(body) == 1 and isinstance(body[0], ast.Try), (
            f"`{name}` 的函式本體不是「一個把整段包起來的 try」。它在 `except` "
            "區塊裡被呼叫，自己拋例外會把一個可以復原的錯誤換成一個看不懂的新"
            "例外，原始錯誤連同 traceback 一起消失。")
        handlers = body[0].handlers
        assert handlers and any(
            h.type is None or (isinstance(h.type, ast.Name) and h.type.id == "Exception")
            for h in handlers), f"`{name}` 的 try 沒有接住所有例外"


# --- retry-after 的讀取與呈現 ------------------------------------------------

def test_the_retry_after_header_becomes_a_wait_in_seconds():
    exc = _FakeAPIError("rate_limit", status_code=429)
    exc.response = _FakeResponse({"retry-after": "90"})
    assert db._dorossi_api_retry_after_sec(exc) == 90.0


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "0", "-5", "", "soon", None])
def test_an_unusable_retry_after_reads_as_absent(raw):
    """標頭是外部字串。`float("nan")` / `float("inf")` 都是合法輸入，而
    `nan <= 0` 是 False——用 `<= 0` 擋不掉它們。放行的後果：等待秒數是 inf
    （永遠不醒）或 nan（clamp 的比較全是 False，一路漏到下游）。"""
    exc = _FakeAPIError("rate_limit", status_code=429)
    exc.response = _FakeResponse({} if raw is None else {"retry-after": raw})
    assert db._dorossi_api_retry_after_sec(exc) is None


def test_no_response_object_reads_as_absent():
    assert db._dorossi_api_retry_after_sec(_FakeAPIError("x", status_code=429)) is None


def test_a_hostile_response_never_raises():
    class _Hostile(Exception):
        @property
        def response(self):
            raise RuntimeError("boom")

    try:
        assert db._dorossi_api_retry_after_sec(_Hostile()) is None
    except RuntimeError as err:
        pytest.fail(f"retry-after 讀取本身炸了：{err!r}")


def test_the_reset_hint_renders_a_wall_clock_time():
    exc = _FakeAPIError("rate_limit", status_code=429)
    exc.response = _FakeResponse({"retry-after": "3600"})
    hint = db._dorossi_api_reset_hint(exc)
    assert hint is not None
    # 只驗格式：實際時刻取決於執行當下的本地時間。
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", hint), hint


def test_the_reset_hint_is_absent_when_the_header_is():
    assert db._dorossi_api_reset_hint(_FakeAPIError("x", status_code=429)) is None


# ---------------------------------------------------------------------------
# 「我們自己的花費上限」不是「方案的用量上限」——`_dorossi_cc_budget_exceeded`
#
# 兩者長得很像（都是「不能再跑了」），但處置完全相反：方案用量上限要**等額度回來
# 再續跑**，自訂花費上限要**當場停下來並告訴人**——等再久額度也不會回來，因為那個
# 上限是我們自己設的。認錯方向的代價是不對稱的：把花費上限誤判成用量上限，自走
# 迴圈會排一場數小時的等待，醒來再撞一次，然後永遠這樣下去。
#
# 2026-09-06 量覆蓋率時發現這支一行都沒被跑過。
# ---------------------------------------------------------------------------

def test_the_dedicated_subtype_is_recognised():
    assert db._dorossi_cc_budget_exceeded({"subtype": "error_max_budget_usd"})


def test_the_errors_list_wording_is_recognised():
    """上游同時用兩種訊號，只認其中一個就會漏。"""
    assert db._dorossi_cc_budget_exceeded(
        {"errors": ["Reached maximum budget ($5.00)"]})


def test_the_wording_match_is_case_insensitive():
    assert db._dorossi_cc_budget_exceeded({"errors": ["reached MAXIMUM BUDGET"]})


def test_a_non_string_entry_in_the_errors_list_does_not_blow_up():
    """`errors` 來自後端的 JSON，內容不由我們保證。"""
    assert not db._dorossi_cc_budget_exceeded({"errors": [None, 1, {"a": 2}]})
    assert db._dorossi_cc_budget_exceeded(
        {"errors": [None, "reached maximum budget"]})


@pytest.mark.parametrize("ev", [
    {}, None, "nonsense", {"subtype": "success"}, {"errors": "not a list"},
    {"errors": []}, {"errors": ["rate limit exceeded"]},
])
def test_everything_else_is_not_a_budget_stop(ev):
    """尤其是最後一筆：方案用量上限**不能**被算進來。

    算進來的話，撞到用量上限會被當成「我們自己喊停」，於是不等額度、直接結束整個
    無人值守任務——而那正是本檔第一段在講的、要避免的舊行為。
    """
    assert not db._dorossi_cc_budget_exceeded(ev)


def test_the_budget_wording_does_not_trip_the_usage_limit_markers():
    """自訂花費上限的字樣不可以命中方案用量上限的比對表。

    `_DOROSSI_USAGE_LIMIT_MARKERS` 比對得**很寬**（`"limit reached"`、`"rate limit"`
    這種子字串），而兩者的處置正好相反：方案用量上限要等額度回來再續跑，自訂花費
    上限要當場停下來——等再久也不會回來，因為那個上限是我們自己設的。誤判的代價是
    自走迴圈排一場數小時的等待、醒來再撞一次，然後永遠這樣下去。
    """
    for text in ("Reached maximum budget ($5.00)",
                 "reached maximum budget",
                 "error_max_budget_usd"):
        low = text.lower()
        hits = [m for m in db._DOROSSI_USAGE_LIMIT_MARKERS if m in low]
        assert not hits, (
            f"{text!r} 命中了用量上限比對表的 {hits}——那會讓「我們自己喊停」被"
            "當成「等額度回來」")


# ---------------------------------------------------------------------------
# 九、等待期間，外面看得出這個迴圈在等什麼（2026-09-20）
#
# 2026-09-19 20:27 自走任務撞上方案用量上限、睡到 22:30。擁有者 20:38 在同一個對話補
# 了一句——那條路是**中途補充**（迴圈在第一個 await 之前就註冊了，同一個對話的新提問
# 一律進注入緩衝），回覆是「📨 已加入目前任務，下一輪會帶進去。」而下一輪在兩小時後。
# 20:39 他按了 abort、再問「為甚麼加入任務會不執行?」（事件檔那段時間沒有任何
# `queued` 事件，只有 `prompt_received`，所以不是排隊）。已經在排隊的人（在迴圈接手
# 之前排進來的）也一樣只看到「處理中…」。這一段釘住：等待期間兩種回覆都講出原因、
# 知道時刻就講時刻、不知道就不寫；一般情形逐字不變。
# ---------------------------------------------------------------------------

_NORMAL_INJECT_ACK = "📨 已加入目前任務，下一輪會帶進去。"


def _local_epoch(*ymdhm) -> float:
    return time.mktime((*ymdhm, 0, 0, 0, -1))


def _expected_clock(reset_at: float, now: float) -> str:
    """刻意在測試裡**另寫一次**規則，不呼叫被測函式——拿它自己驗自己等於沒驗。"""
    then, here = time.localtime(reset_at), time.localtime(now)
    return time.strftime("%H:%M" if then[:3] == here[:3] else "%m-%d %H:%M", then)


def test_the_reset_clock_names_a_known_future_time():
    now = _local_epoch(2026, 9, 19, 20, 27)
    assert b._dorossi_reset_clock(_local_epoch(2026, 9, 19, 22, 30),
                                  now=now) == "22:30"
    # 跨日要帶日期，否則「預計 01:05」讀起來像今天凌晨（已經過了）。
    assert b._dorossi_reset_clock(_local_epoch(2026, 9, 20, 1, 5),
                                  now=now) == "09-20 01:05"


@pytest.mark.parametrize("reset_at", [
    None, float("nan"), float("inf"), float("-inf"), True, "22:30", 1e30,
    "past", "now",
])
def test_the_reset_clock_does_not_guess(reset_at):
    """不知道、已經過去、或根本不是時刻 ⇒ None（呼叫端就不寫時間）。

    `"past"`／`"now"` 兩格是必要的近似案例：時刻**有值**、型別也對，只是不在未來。
    已經過去的時刻寫成「預計 xx:xx」是錯的——`_dorossi_usage_wait_seconds` 對它也是
    退回退避探測，不是睡到那時。拿掉 `<= now` 那道閘，只有這兩格會紅。
    """
    now = _local_epoch(2026, 9, 19, 20, 27)
    if reset_at == "past":
        reset_at = now - 60
    elif reset_at == "now":
        reset_at = now
    assert b._dorossi_reset_clock(reset_at, now=now) is None


class _Placeholder:
    """排隊佔位訊息的替身：只記它最後被改成什麼。"""

    def __init__(self, content: str = "") -> None:
        self.content = content

    async def edit(self, content=None, **_kw):
        self.content = content


def _waiting_state(uid, sid, *, reset_at=None, waiting=True):
    st = b._DorossiLoopState(uid, sid)
    st.usage_waiting = waiting
    st.usage_reset_at = reset_at
    return st


def _assert_generic(text: str) -> None:
    """Layer 1：不點名服務／後端，不帶路徑。"""
    low = text.lower()
    for banned in ("claude", "codex", "anthropic", "openai", ":\\", ".json"):
        assert banned not in low, (banned, text)


def test_queued_placeholders_explain_a_usage_wait(monkeypatch):
    """已經在排隊的佔位訊息：重新編號時要講「前面在等用量重設、預計何時」與做法；
    位置照樣要對。反面對照：沒有在等時，字串與原本逐字相同。"""
    uid, sid = "7", "s1"
    key = b._dorossi_session_key(uid, sid)
    first, second = _Placeholder(), _Placeholder()
    reset_at = time.time() + 2 * 3600
    monkeypatch.setattr(b, "_dorossi_waiters", {
        key: [b._DorossiWaiter(first), b._DorossiWaiter(second)]})
    monkeypatch.setattr(b, "_dorossi_loops", {
        key: _waiting_state(uid, sid, reset_at=reset_at)})

    asyncio.run(asyncio.wait_for(b._dorossi_refresh_waiters(key), 5))
    clock = _expected_clock(reset_at, time.time())
    for msg, ahead in ((first, 1), (second, 2)):
        assert f"前面還有 {ahead} 筆" in msg.content, msg.content
        assert "等方案用量重設" in msg.content, msg.content
        assert f"預計 {clock}" in msg.content, msg.content
        for option in ("/dorossi abort", "/dorossi session new", "/dorossi ai"):
            assert option in msg.content, (option, msg.content)
        _assert_generic(msg.content)

    # 反面對照：同一個迴圈不在等了 ⇒ 回到原本的字串，一個字都不多。
    b._dorossi_loops[key].usage_waiting = False
    asyncio.run(asyncio.wait_for(b._dorossi_refresh_waiters(key), 5))
    assert first.content == "⏳ 已排入佇列，處理中…（前面還有 1 筆）"
    assert second.content == "⏳ 已排入佇列，處理中…（前面還有 2 筆）"
    # 沒有迴圈的一般排隊（首次回覆用的就是這一支）也逐字不變。
    monkeypatch.setattr(b, "_dorossi_loops", {})
    assert (b._dorossi_queue_position_text(key, 3)
            == "⏳ 已排入佇列，處理中…（前面還有 3 筆）")


class _InjectMsg:
    """`mcmd_dorossi` 的最小替身：`safe_reply` 只碰 `reply`。"""

    def __init__(self) -> None:
        self.author = types.SimpleNamespace(id=b.DOROSSI_USER_ID)
        self.channel = types.SimpleNamespace(id=0)
        self.id = 1
        self.replies: list[str] = []

    async def reply(self, content=None, **_kw):
        self.replies.append(content)


@pytest.mark.parametrize("already", [b.DOROSSI_LOOP_INJECT_MAX, b.DOROSSI_LOOP_INJECT_MAX - 1])
def test_the_injection_buffer_refuses_one_more_than_its_cap(monkeypatch, tmp_path, already):
    """注入緩衝的上限，拒絕那一邊在整個套件裡從來沒有成立過（2026-09-22 分支覆蓋率）。
    差一格那一格是對照：證明拒絕是上限給的，也擋 `>=` 改成 `>`。"""
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "events.ndjson")
    uid, sid = str(b.DOROSSI_USER_ID), "s1"

    async def _slot(*_a, **_k):
        return sid

    monkeypatch.setattr(b, "_dorossi_resolve_turn_slot", _slot)
    st = _waiting_state(uid, sid, waiting=False)
    st.injections.extend(f"舊補充{i}" for i in range(already))
    monkeypatch.setattr(b, "_dorossi_loops", {b._dorossi_session_key(uid, sid): st})

    msg = _InjectMsg()
    asyncio.run(asyncio.wait_for(b.mcmd_dorossi(msg, "再補一句"), 5))
    assert len(msg.replies) == 1, msg.replies
    if already >= b.DOROSSI_LOOP_INJECT_MAX:
        assert "中途補充佇列已滿" in msg.replies[0], msg.replies
        assert "再補一句" not in st.injections and len(st.injections) == already
    else:
        assert "已滿" not in msg.replies[0], msg.replies
        assert st.injections[-1] == "再補一句" and len(st.injections) == already + 1


def test_a_full_session_queue_refuses_and_gives_its_lock_reference_back(monkeypatch, tmp_path):
    """同一段對話已經有一輪在跑、佇列也滿了：拒絕，而且把參照計數還回去。

    拒絕那一邊在分支覆蓋率裡從來沒有成立過（2026-09-22）。少還一次參照，那把鎖與它的
    佇列就永遠不會被回收——`_dorossi_release_session_lock` 只在歸零時清。"""
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "events.ndjson")
    uid, sid = str(b.DOROSSI_USER_ID), "s1"

    async def _slot(*_a, **_k):
        return sid

    monkeypatch.setattr(b, "_dorossi_resolve_turn_slot", _slot)
    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "_dorossi_session_locks", {})
    monkeypatch.setattr(b, "_dorossi_session_lock_refs", {})
    monkeypatch.setattr(b, "_dorossi_waiters", {})
    key = b._dorossi_session_key(uid, sid)

    async def _body():
        lock = b._dorossi_acquire_session_lock(key)      # 正在跑的那一輪
        await lock.acquire()
        b._dorossi_waiters[key] = [object()] * b.DOROSSI_MAX_WAITING
        msg = _InjectMsg()
        await asyncio.wait_for(b.mcmd_dorossi(msg, "再問一題"), 5)
        return msg

    msg = asyncio.run(_body())
    assert len(msg.replies) == 1 and "佇列已滿" in msg.replies[0], msg.replies
    assert b._dorossi_session_lock_refs.get(key) == 1, "拒絕之後參照計數沒有還回去"
    assert len(b._dorossi_waiters[key]) == b.DOROSSI_MAX_WAITING, "被拒絕的那一輪還是排進去了"


@pytest.mark.parametrize("state", ["waiting_timed", "waiting_untimed",
                                   "not_waiting"])
def test_an_injection_during_a_usage_wait_says_when_it_will_run(
        monkeypatch, tmp_path, state):
    """**這一條是 2026-09-19 實際走到的路。** 迴圈在等用量重設時，同一個對話的新提問
    進注入緩衝；回覆必須講清楚「重設後才會跑下一輪」，不能只說「下一輪會帶進去」。"""
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "events.ndjson")
    uid, sid = str(b.DOROSSI_USER_ID), "s1"

    async def _slot(*_a, **_k):
        return sid

    monkeypatch.setattr(b, "_dorossi_resolve_turn_slot", _slot)
    reset_at = time.time() + 2 * 3600
    st = _waiting_state(uid, sid,
                        reset_at=reset_at if state == "waiting_timed" else None,
                        waiting=state != "not_waiting")
    monkeypatch.setattr(b, "_dorossi_loops",
                        {b._dorossi_session_key(uid, sid): st})

    msg = _InjectMsg()
    asyncio.run(asyncio.wait_for(b.mcmd_dorossi(msg, "補一句"), 5))
    assert st.injections == ["補一句"], "補充沒有進注入緩衝"
    assert len(msg.replies) == 1, msg.replies
    text = msg.replies[0]
    if state == "not_waiting":
        assert text == _NORMAL_INJECT_ACK, text
        return
    assert "等方案用量重設" in text and "重設後才會跑下一輪" in text, text
    assert "下一輪會帶進去" not in text, text
    for option in ("/dorossi abort", "/dorossi session new", "/dorossi ai"):
        assert option in text, (option, text)
    if state == "waiting_timed":
        assert f"預計 {_expected_clock(reset_at, time.time())}" in text, text
    else:
        assert "預計" not in text, "重設時刻不知道卻寫了時間——那是猜的"
    _assert_generic(text)


def test_the_status_line_shows_a_usage_wait(monkeypatch, tmp_path):
    """`/dorossi status` 的那一行也要看得出來：running 之外多一個 `usage-wait`。"""
    store = tmp_path / "session.json"
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", store)
    uid, sid = "7", "s1"
    store.write_text(
        '{"7": {"active": "s1", "next_seq": 2, "sessions": {"s1": {}}}}',
        encoding="utf-8")
    reset_at = time.time() + 2 * 3600
    st = _waiting_state(uid, sid, reset_at=reset_at)
    monkeypatch.setattr(b, "_dorossi_loops",
                        {b._dorossi_session_key(uid, sid): st})
    line = "\n".join(b._dorossi_waiter_lines(uid))
    assert f"usage-wait until {_expected_clock(reset_at, time.time())}" in line, line
    st.usage_waiting = False
    assert "usage-wait" not in "\n".join(b._dorossi_waiter_lines(uid))


class _SentMsg:
    def __init__(self, content=None) -> None:
        self.content = content

    async def edit(self, content=None, **_kw):
        self.content = content


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _LoopChannel:
    id = 0

    async def send(self, content=None, **_kw):
        return _SentMsg(content)

    def typing(self):
        return _Typing()


class _LoopMsg:
    id = 1

    def __init__(self) -> None:
        self.author = types.SimpleNamespace(id=b.DOROSSI_USER_ID)
        self.channel = _LoopChannel()

    async def reply(self, content=None, **_kw):
        return _SentMsg(content)


def test_the_loop_marks_its_usage_wait_and_clears_it(monkeypatch, tmp_path):
    """走真的 `_dorossi_run_loop`：撞上用量上限 → 等待期間旗標與重設時刻都在、
    已經排隊的佔位訊息被改成講原因 → 等完旗標清掉、佔位訊息回到一般字串。

    後端與等待函式換成替身：第一輪丟用量上限，等待替身在「等待中」那一刻拍照；
    第二輪設 abort 再丟一次，handler 開頭就 return，迴圈乾淨結束。
    """
    store = tmp_path / "session.json"
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", store)
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", tmp_path / "events.ndjson")
    uid, sid = str(b.DOROSSI_USER_ID), "s1"
    store.write_text(
        f'{{"{uid}": {{"active": "s1", "next_seq": 2, "sessions": {{"s1": {{}}}}}}}}',
        encoding="utf-8")
    key = b._dorossi_session_key(uid, sid)
    queued = _Placeholder("⏳ 已排入佇列，處理中…（前面還有 1 筆）")
    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "_dorossi_waiters", {key: [b._DorossiWaiter(queued)]})
    reset_at = time.time() + 2 * 3600
    rounds = {"n": 0}
    seen: dict = {}

    async def _round(*_args):
        rounds["n"] += 1
        if rounds["n"] >= 2:
            b._dorossi_loops[key].abort = True
        raise db._DorossiUsageLimitError("limit", None, reset_at=reset_at)

    async def _wait(st, _delay):
        seen.update(st=st, waiting=st.usage_waiting, reset_at=st.usage_reset_at,
                    placeholder=queued.content)
        return True

    monkeypatch.setattr(b, "_dorossi_loop_one_round", _round)
    monkeypatch.setattr(b, "_dorossi_wait_for_usage_reset", _wait)
    asyncio.run(asyncio.wait_for(
        b._dorossi_run_loop(_LoopMsg(), "任務", None, uid, sid), 15))

    assert rounds["n"] == 2 and seen, "迴圈沒有走到等待那一步"
    assert seen["waiting"] is True, "等待期間沒有標記在等用量重設"
    assert seen["reset_at"] == reset_at, "重設時刻沒有交給迴圈狀態"
    assert "等方案用量重設" in seen["placeholder"], (
        "進入等待時沒有重新編號，已經在排隊的人看不到原因："
        f"{seen['placeholder']!r}")
    assert f"預計 {_expected_clock(reset_at, time.time())}" in seen["placeholder"]
    st = seen["st"]
    assert st.usage_waiting is False and st.usage_reset_at is None, (
        "等待結束後旗標沒清掉——之後的補充會一直被說成「在等用量重設」")
    assert queued.content == "⏳ 已排入佇列，處理中…（前面還有 1 筆）", (
        f"等完之後佔位訊息還掛著原因：{queued.content!r}")
    assert key not in b._dorossi_loops
