"""`_handle_event()` —— 產線事件變成頻道通知的那一段。

這是背景程式**唯一**對外說話的出口：擁有者靠它知道一個角色跑完了、整批收工了、
額度卡住了、排程在休息、出了嚴重錯誤。而它的失敗形態全部是安靜的——一個打錯的
鍵名（`event.get("saved")` 打成 `event.get("save")`）、一個壞掉的格式化、一個沒
接住的型別錯誤，結果都是**通知沒送出或送出錯的內容**，而且要等到那個罕見事件真
的發生、又剛好有人在看，才會被發現。

2026-09-08 用全套測試量了一次逐行覆蓋率：`_handle_event` 的 **131 個陳述式裡只有
1 個被執行過**（0.8%），而 `et == "…"` 的 **20 個分支一個都沒跑過**——不是「大部分
沒跑過」，是**全部**。在那之前，這個函式對外送出的每一句話都沒有任何測試看著。

補測試時抓到一整族真缺陷，共同的形狀是**「訊息整則消失」**：

`_format_duration_short(seconds)` 的本體是 `int(seconds)`，而
`int(None)` / `int(nan)` / `int(inf)` 全都丟例外；`time.localtime(wake_ts)` 對
`nan` 丟 `ValueError`、對 `inf` 或超大值丟 `OverflowError` / `OSError`；
`character_done` 的 `saved >= target` 在 `saved` 是字串時丟 `TypeError`。這些例外
被 `_handle_event` 最外層的 `except Exception` 接住——所以**事件迴圈確實沒有被
炸掉**，這一半原本就是對的——但那則通知**一則都沒有送出去**，使用者只會看到
「什麼都沒發生」。實測 10 種輸入形狀會走到這個結果，其中最容易踩到的是
`"elapsed_sec": null`：`event.get("elapsed_sec", 0)` 的預設值**只在鍵不存在時**
生效，鍵在、值是 `null` 的時候拿到的是 `None`。

而 `schedule_rest` 正是最不能消失的那一則：它的註解自己寫著「講出醒來時間，使用
者才不會把它當卡死」——弄丟它，一段計畫中的休息看起來就跟當機一模一樣。

另外兩個是「送出錯的內容」：`saved` 是 `nan` 時 `nan >= target` 為 False，於是安
靜地報成「⚠️ 沒達標」（本專案已經第五次踩到「浮點特殊值讓比較型防線同時失效」）；
`wake_ts` 是 `True` 時 `isinstance(True, (int, float))` 成立，於是印出一個
1970 年的鐘點——**看起來完全合理**的錯誤答案。

來源是 `events.ndjson`，另一個行程寫的跨行程檔案，而 `json.loads` **預設就吃**
`NaN` / `Infinity`。所以這不是假想的輸入。

修法與這裡釘住的性質：`_format_duration_short` 與新的 `_format_clock_short`
都改成**全函式**（任何不能用的輸入回 `?` / 空字串，絕不丟例外），
`character_done` 的達標判定改走 `_event_number()`。
"""
from __future__ import annotations

import ast
import asyncio
import io
import os
import sys
import time
from contextlib import redirect_stderr
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord_bot as b  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
_BOT_SRC = (PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8")
_BOT_TREE = ast.parse(_BOT_SRC)


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class FakeChannel:
    """只長出 `_handle_event` 真的會碰到的那幾個屬性。

    `send()` 連 **kwargs 一起收：`allowed_mentions` 是警報那幾則的一部分，而
    「有沒有帶」跟「內容是什麼」一樣重要——漏掉它，警報會安靜地不 ping 人。
    """

    guild = None

    def __init__(self, cid: int = 4242) -> None:
        self.id = cid
        self.sent: list[tuple[object, dict]] = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return None

    @property
    def texts(self) -> list[str]:
        return [c for c, _ in self.sent]

    @property
    def blob(self) -> str:
        """所有送出內容串成一段——用在「這段字裡不准出現 X」那類斷言。"""
        return "\n".join(str(c) for c, _ in self.sent)


def deliver(event: dict) -> tuple[FakeChannel, str]:
    """把一筆事件送進 `_handle_event`，回 (假頻道, 這一輪的 stderr)。

    stderr 一起收是刻意的：Secrecy Layer 1 的規則是「原始細節只寫 stderr、頻道
    只收泛用句」，所以**兩邊都要驗**——只驗「頻道沒有原始字串」的話，把那行
    `print` 整個刪掉也會是綠的，而那樣就沒有診斷資訊了。
    """
    chan = FakeChannel()
    err = io.StringIO()
    with redirect_stderr(err):
        asyncio.run(b._handle_event(chan, event))
    return chan, err.getvalue()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """把會寫到磁碟、會讀到正式設定的東西全部導開。

    這台機器上有一個無人值守的批次正在跑，所以測試絕不能碰到 repo root 的
    `events.ndjson` / `audit.ndjson` / 單張產圖請求檔。
    """
    monkeypatch.setattr(b, "ALERT_USER_ID", 777000777)
    # 磁碟探測預設關掉：`character_done` 會順手呼叫 `_maybe_alert_low_disk`，
    # 開著的話送出的訊息數會隨這台機器當下的剩餘空間而變。
    monkeypatch.setattr(b, "MIN_FREE_DISK_GB", 0.0)
    monkeypatch.setattr(b, "_low_disk_alerted", False)
    monkeypatch.setattr(b, "EVENTS_FILE", tmp_path / "events.ndjson")
    monkeypatch.setattr(b, "AUDIT_FILE", tmp_path / "audit.ndjson")
    monkeypatch.setattr(b, "GENERATE_HISTORY_FILE", tmp_path / "gh.ndjson")
    monkeypatch.setattr(b, "SINGLE_IMAGE_REQUEST_FILE", tmp_path / "req.json")
    # `single_image_done` 會 fire-and-forget 一個 pump；在 `asyncio.run` 裡建的
    # task 來不及跑完 loop 就關了，會噴 'Task was destroyed but it is pending'。
    monkeypatch.setattr(b, "_schedule_coro",
                        lambda coro, **_kw: coro.close())


# ---------------------------------------------------------------------------
# 每一個事件型別的代表樣本
# ---------------------------------------------------------------------------
# **照著真實事件的長相寫，不要簡化成「只有測試需要的欄位」**（同
# `test_dorossi_stream.py` 開頭那條慣例）。這裡的鍵集合有兩個來源，都不是自己編的：
#
#   * 正式 `events.ndjson`（645 筆 / 13 種型別）每一種最早的那一筆；
#   * `_webrunner_shared.py` 的 `emit_event(...)` 送出點——正式紀錄裡一筆都沒有的
#     四種（`quota_wait` / `dom_result` / `paused` / `resumed`）只能從這裡推。
#
# **刻意不從 `_handle_event` 的 `event.get("…")` 反推**：那正是要驗的東西，拿它
# 推測資等於自己驗自己。少一個鍵時 `event.get(k, default)` 會安靜地回預設值，
# 測試照樣綠——而那個「安靜地回預設值」就是這個檔案要抓的失效形態。
#
# `ts` / `type` 由 `emit_event` 自己補上，所以每一筆都有。
#
# 這張表同時是「這個檔案到底跑過哪些型別」的登記簿——底下兩支守門拿它跟
# `_handle_event` 的分支、以及 webrunner 的送出點對帳。
_TS = 1_800_000_000.0            # 固定值：斷言要能重現
_WAKE_TS = 1_800_021_600.0       # `_TS` + 6h，一個**絕對時間點**（不是時長）

EVENT_SAMPLES: dict[str, dict] = {
    "character_start": {
        "ts": _TS, "type": "character_start",
        "name": "columbina (genshin impact)", "target": 120,
        "folder": "columbina (genshin impact)", "resumed": 0},
    "character_done": {
        "ts": _TS, "type": "character_done", "name": "surtr (arknights)",
        "saved": 120, "target": 120, "folder": "surtr (arknights)",
        "elapsed_sec": 3671.4},
    "todo_done": {
        "ts": _TS, "type": "todo_done", "total_saved": 480,
        "total_pairs": 4, "elapsed_sec": 7325.2},
    "quota_blocked": {
        "ts": _TS, "type": "quota_blocked", "character": "surtr (arknights)",
        "image_index": 87},
    "quota_wait": {
        "ts": _TS, "type": "quota_wait", "label": "surtr (arknights)",
        "waited_sec": 3600.0, "next_retry_sec": 900.0},
    "quota_resumed": {
        "ts": _TS, "type": "quota_resumed", "character": "surtr (arknights)",
        "image_index": 87, "waited_sec": 3720.0, "last_wait_sec": 3600.0},
    "generation_blocked": {
        "ts": _TS, "type": "generation_blocked", "saved": 42, "produced": 2},
    "critical_error": {
        "ts": _TS, "type": "critical_error", "message": "boom",
        # `code_drift` 是三態（True／False／None）：那份 traceback 印出來的**原始碼
        # 文字**只有在 False 時可信（`linecache` 是列印當下才讀磁碟的）。bot 刻意
        # 不轉述它——這裡放進樣本只是為了讓那個分支真的跑過帶著它的形狀。
        "code_drift": True,
        "traceback": "Traceback…"},
    "duplicate_image": {
        "ts": _TS, "type": "duplicate_image", "character": "surtr (arknights)",
        "image_index": 12},
    "consecutive_failures": {
        "ts": _TS, "type": "consecutive_failures",
        "character": "surtr (arknights)", "count": 5, "image_index": 87,
        "phase": "generate"},
    "dom_result": {
        "ts": _TS, "type": "dom_result", "count": 1,
        "data": [{"index": 0, "tag": "textarea", "visible": True,
                  "value_len": 12, "aria_label": "", "placeholder": "",
                  "value_preview": "", "parent_text": ""}]},
    "resume_mismatch": {
        "ts": _TS, "type": "resume_mismatch", "name": "surtr (arknights)",
        "folder": "surtr (arknights)_2", "fields": ["prompt", "char2"]},
    "resume_unusable": {
        "ts": _TS, "type": "resume_unusable", "name": "surtr (arknights)",
        "reason": "folder_missing", "saved": 81},
    "page_recovery": {
        "ts": _TS, "type": "page_recovery", "character": "surtr (arknights)",
        "image_index": 3},
    # 2026-09-08 正式批次的那一筆：跑完 119/120 之後進入排程休息。
    # `rest_sec` 是**時長**、`wake_ts` 是**絕對時間戳**——底下有一支專門釘住這
    # 兩個不得被搞混。
    "schedule_rest": {
        "ts": _TS, "type": "schedule_rest",
        "character": "exusiai the new covenant (arknights)",
        "rest_sec": 21600.0, "wake_ts": _WAKE_TS, "worked_sec": 83668.1},
    "schedule_resumed": {
        "ts": _TS, "type": "schedule_resumed",
        "character": "exusiai the new covenant (arknights)",
        "rested_sec": 21600.0},
    "chrome_restart": {
        "ts": _TS, "type": "chrome_restart", "character": "surtr (arknights)",
        "chars_completed": 3},
    "paused": {"ts": _TS, "type": "paused", "label": "surtr (arknights)"},
    "resumed": {"ts": _TS, "type": "resumed", "label": "surtr (arknights)"},
    # 單張請求的服務心跳。這個型別**從來不送訊息**（PART 2 之後也只記時間）；
    # id 刻意跟下面那筆一樣不在 correlation map 裡。
    "single_image_serving": {
        "ts": _TS, "type": "single_image_serving",
        "request_id": "nobody-is-waiting-for-this", "in_band": True,
        "phase": "generate", "beat_within_sec": 285.0},
    # correlation map 裡沒有這個 request_id ⇒ 走「安靜忽略」那條路（那正是它
    # 被設計成要做的事），所以它是下面「每個型別都會送訊息」那支的唯一例外。
    "single_image_done": {
        "ts": _TS, "type": "single_image_done",
        "request_id": "nobody-is-waiting-for-this", "ok": True,
        "path": "output/_oneshot/nobody/x.png"},
}

# 同一型別的第二種送出形狀（`emit_event` 有兩個以上呼叫點的那些）。
EVENT_VARIANTS: dict[str, dict] = {
    "todo_done": {"ts": _TS, "type": "todo_done", "total_saved": 30,
                  "total_pairs": 1, "stopped_by_end": True,
                  "elapsed_sec": 900.0},
    "critical_error": {"ts": _TS, "type": "critical_error",
                       "message": "attempted 2 character(s) but saved 0 "
                                  "images (broken session)"},
    "consecutive_failures": {"ts": _TS, "type": "consecutive_failures",
                             "character": "surtr (arknights)", "count": 5,
                             "image_index": 87},
    "resume_unusable": {"ts": _TS, "type": "resume_unusable",
                        "name": "surtr (arknights)", "reason": "bad_folder"},
    "dom_result": {"ts": _TS, "type": "dom_result",
                   "error": "dump failed"},
    "single_image_done": {"ts": _TS, "type": "single_image_done",
                          "request_id": "nobody-is-waiting-for-this",
                          "ok": False, "error": "boom"},
    "single_image_serving": {"ts": _TS, "type": "single_image_serving",
                             "request_id": "nobody-is-waiting-for-this",
                             "in_band": False, "phase": "start",
                             "beat_within_sec": 285.0},
}

# 這兩個不送訊息是**正確**行為，不是漏測——見上面的註解。
_SILENT_BY_DESIGN = {"single_image_done", "single_image_serving"}


# ---------------------------------------------------------------------------
# 一、每個型別都真的送得出訊息
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "etype", sorted(set(EVENT_SAMPLES) - _SILENT_BY_DESIGN))
def test_every_event_type_actually_reaches_the_channel(etype):
    """一則產線通知消失，跟它從來沒發生過，在使用者那一端長得一模一樣。"""
    chan, _ = deliver(EVENT_SAMPLES[etype])
    assert chan.sent, (
        f"`{etype}` 一則訊息都沒送出。這是這個函式最安靜的失敗方式："
        "例外被最外層的 `except Exception` 吃掉，事件迴圈活著、通知不見了。")
    assert all(str(c).strip() for c, _ in chan.sent), \
        f"`{etype}` 送出了空訊息"


@pytest.mark.parametrize("etype", [
    "totally-made-up-event",
    # 真正會發生的形狀是**打錯的鍵名**，而打錯的名字長得跟真的很像。
    "character_finished", "todo_complete", "quota_resume", "schedule_resume",
    "", "CHARACTER_DONE",
])
def test_an_unrecognised_event_type_says_nothing(etype):
    """反面對照：不是「一律送點什麼」。

    少了這一支，一個把每個分支都改成送同一句話的實作也會全綠。多列幾種是因為單一
    字串的版本只證明**那一個**字串是安靜的——變異測試實測：加一個對別的假型別回話
    的分支時，單字串版本存活。（那個變異最後是被
    `test_every_branch_of_the_dispatcher_has_a_sample_here` 擋下來的，兩支互補。）
    """
    chan, _ = deliver({"ts": _TS, "type": etype, "name": "x"})
    assert chan.sent == [], f"不認得的型別 {etype!r} 也送了東西：{chan.texts!r}"


# ---------------------------------------------------------------------------
# 二、訊息裡真的帶著事件的值
# ---------------------------------------------------------------------------
def test_character_start_carries_the_name_and_the_target():
    chan, _ = deliver(EVENT_SAMPLES["character_start"])
    text = chan.texts[0]
    assert "columbina (genshin impact)" in text
    # 連著上下文比對，不是裸的 `"120" in text`——同一則訊息裡出現過的別的數字
    # 可能剛好含有它（`todo_done` 的 `"4" in "480"` 就是這樣讓一個變異活下來的）。
    assert "target `120` images" in text, f"目標張數不見了：{text!r}"


def test_character_done_carries_saved_target_name_and_elapsed():
    chan, _ = deliver(EVENT_SAMPLES["character_done"])
    text = chan.texts[0]
    assert "surtr (arknights)" in text
    assert "120/120" in text, f"存檔數／目標數沒出現在訊息裡：{text!r}"
    assert "1h 1m 11s" in text, f"耗時沒被格式化進訊息：{text!r}"
    assert text.startswith("✅")


def test_character_done_marks_a_short_run_as_not_ok():
    """反面對照：達標與沒達標必須長得不一樣。

    `ok = saved >= target` 是這一則唯一的判斷，而它算錯的時候不會有任何錯誤——
    只是一個安靜錯掉的圖示。
    """
    chan, _ = deliver({"type": "character_done", "name": "surtr",
                       "saved": 7, "target": 120, "elapsed_sec": 60})
    text = chan.texts[0]
    assert text.startswith("⚠️"), f"沒達標卻報成達標：{text!r}"
    assert "7/120" in text


def test_character_done_reports_the_low_disk_check(monkeypatch):
    """`character_done` 是唯一順便檢查磁碟的分支——那個呼叫不得被拿掉。

    低容量警告刻意只在角色邊界檢查、而且一輪只叫一次（`_low_disk_alerted` latch），
    所以它唯一的觸發點就是這裡。拿掉之後一個跑好幾天的無人值守批次會一路把磁碟寫
    滿，而使用者收到的每一則 `character_done` 看起來都正常。
    """
    monkeypatch.setattr(b, "MIN_FREE_DISK_GB", 50.0)
    monkeypatch.setattr(b, "_free_disk_gb", lambda: 1.5)
    chan, _ = deliver(EVENT_SAMPLES["character_done"])
    assert len(chan.sent) == 2, \
        f"少了磁碟警告那一則：{chan.texts!r}"
    assert "1.5" in chan.texts[1], f"沒講剩多少空間：{chan.texts[1]!r}"


def test_a_healthy_disk_adds_no_second_message(monkeypatch):
    """反面對照：磁碟沒問題時不得每個角色都叫一次。"""
    monkeypatch.setattr(b, "MIN_FREE_DISK_GB", 50.0)
    monkeypatch.setattr(b, "_free_disk_gb", lambda: 900.0)
    chan, _ = deliver(EVENT_SAMPLES["character_done"])
    assert len(chan.sent) == 1, f"多送了東西：{chan.texts!r}"


def test_todo_done_carries_the_totals():
    """兩個數字要**照著句子的位置**比對，不是各自 `in text`。

    變異測試抓到的：`total_pairs` 是 4、`total_saved` 是 480，而 `"4" in "480"`
    為真——所以「把 pairs 換成 `some`」那個變異**存活**了。斷言挑錯字串，測到的
    就只是一件本來就成立的事（本專案第三次踩到這個形狀）。
    """
    chan, _ = deliver(EVENT_SAMPLES["todo_done"])
    text = chan.texts[0]
    assert "480 images across 4 pairs" in text, \
        f"總數／對數沒有照句子的位置出現：{text!r}"
    assert "2h 2m 5s" in text
    assert "🏁" in text


def test_todo_done_distinguishes_the_end_marker_stop():
    """反面對照：`stopped_by_end` 的兩種結局必須分得出來。

    「跑完整批」跟「碰到 `end` 標記提前停住」是兩件完全不同的事，而佇列裡還有
    沒跑的東西正是後者唯一的線索。
    """
    normal, _ = deliver({"type": "todo_done", "total_saved": 10,
                         "total_pairs": 1, "elapsed_sec": 60})
    stopped, _ = deliver({"type": "todo_done", "total_saved": 10,
                          "total_pairs": 1, "elapsed_sec": 60,
                          "stopped_by_end": True})
    assert "🛑" in stopped.texts[0] and "🏁" not in stopped.texts[0]
    assert "🏁" in normal.texts[0] and "🛑" not in normal.texts[0]
    assert normal.texts[0] != stopped.texts[0]


def test_quota_wait_carries_both_durations():
    """兩個數字都要在——只帶其中一個，使用者就不知道還要等多久。"""
    chan, _ = deliver(EVENT_SAMPLES["quota_wait"])
    text = chan.texts[0]
    assert "1h 0s" in text, f"已等時間不見了：{text!r}"
    assert "15m 0s" in text, f"下次重試時間不見了：{text!r}"


def test_quota_resumed_carries_the_character_and_the_wait():
    chan, _ = deliver(EVENT_SAMPLES["quota_resumed"])
    text = chan.texts[0]
    assert "surtr" in text
    assert "1h 2m 0s" in text


def test_consecutive_failures_carries_count_character_and_index():
    chan, _ = deliver(EVENT_SAMPLES["consecutive_failures"])
    text = chan.texts[0]
    assert "**5** consecutive" in text, f"連續失敗次數不見了：{text!r}"
    assert "surtr (arknights)" in text
    assert "around image 87" in text, f"第幾張不見了：{text!r}"


def test_schedule_rest_says_how_long_and_until_when():
    """休息通知的整個價值在於「幾點醒來」——沒有它就跟當機沒兩樣。"""
    chan, _ = deliver(EVENT_SAMPLES["schedule_rest"])
    text = chan.texts[0]
    assert "6h 0s" in text, f"休息長度不見了：{text!r}"
    expected = time.strftime("%m-%d %H:%M", time.localtime(_WAKE_TS))
    assert expected in text, f"醒來時間不見了：{text!r}"


def test_schedule_rest_does_not_swap_the_duration_and_the_timestamp():
    """`rest_sec` 是**時長**、`wake_ts` 是**絕對時間戳**，兩者都在同一筆事件裡。

    把它們互換不會丟例外，只會印出一個看起來完全合理的錯誤答案——`rest_sec`
    當成時間戳是 1970 年的某個鐘點，`wake_ts` 當成時長是五十幾萬小時。這種
    錯誤在正式頻道上很難被發現，所以正反兩邊都要釘。
    """
    chan, _ = deliver(EVENT_SAMPLES["schedule_rest"])
    text = chan.texts[0]
    swapped_clock = time.strftime("%m-%d %H:%M", time.localtime(21600.0))
    assert swapped_clock not in text, \
        f"把休息**時長**當成時間戳印出來了：{text!r}"
    swapped_duration = b._format_duration_short(_WAKE_TS)
    assert swapped_duration not in text, \
        f"把**醒來時間戳**當成時長印出來了：{text!r}"


def test_schedule_rest_without_a_wake_time_drops_the_clause():
    """反面對照：沒有 `wake_ts` 時不得憑空生一個鐘點出來。"""
    chan, _ = deliver({"ts": _TS, "type": "schedule_rest",
                       "character": "surtr (arknights)", "rest_sec": 21600.0,
                       "worked_sec": 83668.1})
    text = chan.texts[0]
    assert "6h 0s" in text
    assert "預計" not in text, f"沒有 wake_ts 卻印了醒來時間：{text!r}"


def test_schedule_resumed_says_how_long_it_rested():
    chan, _ = deliver(EVENT_SAMPLES["schedule_resumed"])
    assert "6h 0s" in chan.texts[0]


def test_pause_and_resume_are_two_different_messages():
    """反面對照：兩則狀態相反的通知不得長成同一句。"""
    paused, _ = deliver({"type": "paused"})
    resumed, _ = deliver({"type": "resumed"})
    assert paused.texts[0] != resumed.texts[0]
    assert "⏸️" in paused.texts[0] and "▶️" in resumed.texts[0]


def test_resume_mismatch_names_the_diverging_fields():
    chan, _ = deliver(EVENT_SAMPLES["resume_mismatch"])
    text = chan.texts[0]
    assert "主提示詞" in text and "角色二" in text
    assert "角色一" not in text, f"把沒有分歧的欄位也印出來了：{text!r}"


def test_resume_mismatch_ignores_a_field_name_it_does_not_know():
    """反面對照：欄位名是別的行程寫的，白名單以外的一律不得原樣轉出。"""
    chan, _ = deliver({"type": "resume_mismatch", "name": "surtr",
                       "fields": ["prompt", "D:\\Work\\Example\\x"]})
    text = chan.texts[0]
    assert "主提示詞" in text
    assert "Example" not in text and "D:\\" not in text


def test_resume_unusable_maps_the_reason_to_a_neutral_label():
    chan, _ = deliver(EVENT_SAMPLES["resume_unusable"])
    text = chan.texts[0]
    assert "上一輪存放的位置已經不在了" in text
    assert "81" in text, f"已完成張數不見了：{text!r}"
    assert "folder_missing" not in text, "原因碼原樣送出去了"


def test_resume_unusable_drops_a_reason_code_it_does_not_know():
    """反面對照：認不得的原因碼要退回空字串，不是原樣轉貼。"""
    chan, _ = deliver({"type": "resume_unusable", "name": "surtr",
                       "reason": "output/surtr (arknights)/broken.json"})
    text = chan.texts[0]
    assert "output/" not in text and "broken.json" not in text


# ---------------------------------------------------------------------------
# 三、Secrecy Layer 1
# ---------------------------------------------------------------------------
# 頻道收到的字串不得帶主機路徑、記錄檔名、外部服務名、原始例外文字。
# `critical_error` 是這裡面最尖銳的一支：事件本身帶的就是背景程式的
# `str(error)` 加上一整段 traceback。

_LEAKY_CRITICAL = {
    "type": "critical_error",
    "message": ("SessionNotCreatedException: Chrome instance exited; "
                "user-data-dir=D:\\Work\\Example\\.chrome_profile"),
    "traceback": (
        "Traceback (most recent call last):\n"
        '  File "D:\\Work\\Example\\axiomatic\\webrunner_novelai.py", '
        "line 4242, in run_batch\n"
        "    driver.get('https://novelai.net/image')\n"
        "selenium.common.exceptions.WebDriverException: chrome not reachable\n"),
}

_BANNED_IN_CHANNEL = (
    "D:\\Work", "D:/Work", "Example", "webrunner_novelai",
    ".chrome_profile", "novelai", "Traceback", "SessionNotCreated",
    "WebDriverException", "selenium", "chromedriver", "WEBRunner.log",
    "todo_prompt.md", "batch_config.json",
)


def test_a_critical_error_never_forwards_the_raw_message_or_traceback():
    """事件裡的原始例外＋traceback 一個字都不得進頻道。

    這是 Layer 1 最容易被「好心」改壞的一處：把 `msg` 接到訊息尾巴看起來像是
    在幫使用者除錯，實際上是把主機路徑、瀏覽器驅動名稱與站方網址一起貼進聊天室。
    """
    chan, _ = deliver(_LEAKY_CRITICAL)
    text = chan.texts[0]
    for banned in _BANNED_IN_CHANNEL:
        assert banned.lower() not in text.lower(), (
            f"`critical_error` 把 {banned!r} 送進頻道了：{text!r}")
    assert "log" in text.lower(), \
        f"泛用句至少要告訴使用者去哪裡找細節：{text!r}"


def test_a_critical_error_still_writes_the_detail_to_stderr():
    """反面：泛用化不等於把診斷資訊丟掉。

    只驗「頻道沒有原始字串」的話，把那一行 `print` 整個刪掉也是綠的——然後那段
    traceback 就永遠消失了，而它是事後唯一查得到原因的東西。
    """
    _, err = deliver(_LEAKY_CRITICAL)
    assert "SessionNotCreated" in err, f"原始訊息沒進 stderr：{err!r}"
    assert "webrunner_novelai" in err, f"traceback 沒進 stderr：{err!r}"


def test_a_dom_dump_failure_keeps_the_raw_error_out_of_the_channel():
    ev = {"type": "dom_result",
          "error": "NoSuchElementException at D:\\Work\\Example\\x.py"}
    chan, err = deliver(ev)
    text = chan.texts[0]
    assert "Example" not in text and "NoSuchElement" not in text
    assert "Example" in err, "原始錯誤沒進 stderr"


def test_a_dom_dump_redacts_the_page_text_it_echoes():
    """DOM dump 是少數**必須**把外部內容原樣貼出來的地方，所以要走刷除器。"""
    chan, _ = deliver({
        "type": "dom_result", "count": 1,
        "data": [{"index": 0, "tag": "textarea", "visible": True,
                  "value_len": 3,
                  "aria_label": "D:\\Work\\Example\\output\\a.png",
                  "placeholder": "novelai prompt",
                  "value_preview": "C:/Users/Example/AppData/Local/Temp/x",
                  "parent_text": "saved to D:/Work/Example/output"}]})
    text = chan.blob
    assert "Example" not in text, f"路徑沒被刷掉：{text!r}"
    assert "AppData" not in text, f"正斜線的路徑沒被刷掉：{text!r}"
    assert "novelai" not in text.lower(), f"品牌字沒被刷掉：{text!r}"
    assert "[path]" in text, "整段都沒被認出來是路徑"


def test_a_long_dom_dump_is_split_without_losing_or_splitting_an_entry():
    """一份大的 DOM dump 要切成好幾則，而且**每塊保留完整列**。

    切塊那兩行是 `_handle_event` 裡最後兩個沒被跑到的陳述式：它只在總長超過 1900
    字時才走，而 `/sys introspect_dom` 平常回的欄位不多。切壞的兩種形態都不會丟
    例外——不是少貼幾個欄位，就是把一列切成兩半貼出去。
    """
    data = [{"index": i, "tag": "textarea", "visible": True,
             "value_len": i, "parent_text": "field number %02d " % i + "x" * 60}
            for i in range(40)]
    chan, _ = deliver({"ts": _TS, "type": "dom_result",
                       "count": len(data), "data": data})
    assert len(chan.sent) > 1, \
        f"這份 dump 應該要切塊，卻只送了 {len(chan.sent)} 則"
    for text in chan.texts:
        assert len(text) <= 2000, (
            f"有一塊 {len(text)} 字，超過對話平台單則訊息的上限——"
            "整則會被拒收，而例外會被最外層的 `except` 吃掉。")
    blob = chan.blob
    for i in range(40):
        assert f"`[{i}]`" in blob, f"切塊時把第 {i} 個欄位弄丟了"
    # 每一列都必須完整落在**某一塊**裡，不能跨塊被切成兩半。
    for text in chan.texts:
        for line in text.splitlines():
            if line.startswith("- "):
                assert line.endswith("`"), f"一列被切成兩半了：{line!r}"


def test_no_single_dom_entry_can_overflow_one_message():
    """頁面回來的每一個字串欄位都要截長度——切塊器救不了一列自己就超長。

    切塊只在**列與列之間**切，所以一列自己超過上限時整則會被平台拒收，而例外被
    最外層的 `except Exception` 接住 → 整份 dump 消失，使用者什麼都收不到。
    `/sys introspect_dom` 正是「頁面變得跟預期不一樣」時才會下的指令，也就是這些
    欄位最可能長得很怪的那一刻。

    這一支刻意**不指名欄位**——它把 entry 裡每一個字串欄位都灌長，所以日後多一個
    沒截長度的欄位時它會自己變紅。2026-09-08 抓到 `aria_label` 是四個裡唯一沒截
    的那一個（其他三個是 40/40/60），一個 5000 字的 aria-label 產出 5038 字的訊息。
    """
    entry = {"index": 0, "tag": "textarea", "visible": True, "value_len": 3,
             "aria_label": "A" * 5000, "placeholder": "B" * 5000,
             "value_preview": "C" * 5000, "parent_text": "D" * 5000}
    chan, _ = deliver({"ts": _TS, "type": "dom_result", "count": 1,
                       "data": [entry]})
    assert chan.sent
    for text in chan.texts:
        assert len(text) <= 2000, (
            f"一個 entry 就撐出 {len(text)} 字的訊息——頁面來的某個欄位沒有截"
            "長度，而切塊器只在列與列之間切，救不了它。")


def test_a_dom_dump_still_reports_the_field_shape():
    """反面：刷除不能把整份 dump 變成沒有資訊的一句話。"""
    chan, _ = deliver({
        "type": "dom_result", "count": 2,
        "data": [{"index": 0, "tag": "textarea", "visible": True,
                  "value_len": 12},
                 {"index": 1, "tag": "div", "visible": False,
                  "value_len": 0}]})
    text = chan.blob
    assert "2" in text
    assert "[0]" in text and "[1]" in text
    assert "textarea" in text and "div" in text


# 這兩個欄位是**使用者自己排進佇列的項目名**，經 `character_folder_name()` 正規
# 化過，不是主機內部字串。`chrome_restart` 的註解已經寫明留著它們的理由：留著才
# 看得出進度。所以毒化測試刻意跳過它們——把它們也算成洩漏，測到的會是一條並不
# 存在的規則，而一個會亂叫的守門是會被關掉的守門。
_USER_SUPPLIED_FIELDS = {"name", "character"}


@pytest.mark.parametrize("etype", sorted(EVENT_SAMPLES))
def test_no_event_type_leaks_a_host_string_from_a_machine_written_field(etype):
    """把每一個**機器寫的**字串欄位都塞成主機路徑／品牌字，看有沒有原樣漏出去。

    這一支不預設任何欄位名稱——它把樣本裡除了角色名以外**所有**字串值換掉，所以
    日後新增一個會被印出來的欄位時它會自己跟上。
    """
    poison = "D:\\Work\\Example\\axiomatic\\webrunner_novelai.py"
    event = dict(EVENT_SAMPLES[etype])
    poisoned = []
    for key, value in list(event.items()):
        if key == "type" or key in _USER_SUPPLIED_FIELDS:
            continue
        if isinstance(value, str):
            event[key] = poison
            poisoned.append(key)
    chan, _ = deliver(event)
    text = chan.blob
    for banned in ("D:\\Work", "Example", "webrunner_novelai"):
        assert banned not in text, (
            f"`{etype}` 從 {poisoned} 把 {banned!r} 原樣送進頻道了：{text!r}")


def test_the_character_name_really_is_echoed_verbatim():
    """反面對照，也是上面那個豁免的**存在證明**。

    豁免必須有人證明它還在做它被豁免去做的事——否則哪天角色名也開始被刷除，上面
    那支照樣是綠的，而使用者從此看不出通知講的是哪一個角色。
    """
    chan, _ = deliver({"type": "character_start",
                       "name": "columbina (genshin impact)", "target": 1})
    assert "columbina (genshin impact)" in chan.texts[0]


# ---------------------------------------------------------------------------
# 四、警報前綴：該叫人的要叫，不該叫的不准叫
# ---------------------------------------------------------------------------
_ALERTING = ["critical_error", "consecutive_failures", "generation_blocked",
             "duplicate_image"]
# 例行狀態不得 ping——一個每次跑完角色都在半夜叫人的 bot，最後會被靜音。
_NOT_ALERTING = ["character_start", "character_done", "todo_done",
                 "quota_blocked", "quota_wait", "quota_resumed",
                 "schedule_rest", "schedule_resumed", "page_recovery",
                 "resume_mismatch", "resume_unusable", "chrome_restart",
                 "dom_result", "paused", "resumed"]


@pytest.mark.parametrize("etype", _ALERTING)
def test_an_alerting_event_pings_and_scopes_the_mention(etype):
    chan, _ = deliver(EVENT_SAMPLES[etype])
    content, kwargs = chan.sent[0]
    assert f"<@{b.ALERT_USER_ID}>" in str(content), \
        f"`{etype}` 是警報卻沒有 ping：{content!r}"
    assert "allowed_mentions" in kwargs, (
        f"`{etype}` 沒帶 `allowed_mentions=`。client 層的基準線把 users 全關，"
        "所以少了這個 opt-in，警報會安靜地不 ping 人。")


@pytest.mark.parametrize("etype", _NOT_ALERTING)
def test_a_routine_event_does_not_ping_anyone(etype):
    """反面對照，而且是這一族真正的判準所在。"""
    chan, _ = deliver(EVENT_SAMPLES[etype])
    assert "<@" not in chan.blob, \
        f"例行事件 `{etype}` 去 ping 人了：{chan.blob!r}"


def test_the_alert_families_between_them_cover_every_type():
    """兩張名單加起來要蓋滿所有型別，否則新型別會從縫裡溜過去。"""
    covered = set(_ALERTING) | set(_NOT_ALERTING) | _SILENT_BY_DESIGN
    assert covered == set(EVENT_SAMPLES), (
        f"沒被歸類的型別：{sorted(set(EVENT_SAMPLES) - covered)}；"
        f"名單裡多出來的：{sorted(covered - set(EVENT_SAMPLES))}")


# ---------------------------------------------------------------------------
# 五、壞掉的事件形狀
# ---------------------------------------------------------------------------
# 兩個層次，而且不要混為一談：
#   (a) 不得往外拋——這個函式跑在背景 watcher 上；
#   (b) 該送的通知**還是要送出去**——(a) 靠最外層的 `except Exception` 就能滿足，
#       而那正是 2026-09-08 之前的狀態：迴圈活著、通知安靜消失。

def _with(etype: str, **overrides) -> dict:
    """真實樣本 + 改壞其中一兩個欄位。

    刻意**不從零編一個 dict**：從真實形狀出發，壞掉的才只有指定的那一格，測到的
    也才是「這個欄位算不出來」而不是順便混進「另外三個鍵不見了」。
    """
    event = dict(EVENT_SAMPLES[etype])
    event.update(overrides)
    return event


def _only_type(etype: str) -> dict:
    """只剩 `ts` / `type` ——每一個 `.get(key, default)` 的預設值都會生效。"""
    return {"ts": _TS, "type": etype}


_NAN, _INF = float("nan"), float("inf")

_MALFORMED = [
    ("空 dict", {}),
    ("沒有 type", {"ts": _TS, "name": "x"}),
    ("type 不是字串", {"ts": _TS, "type": 12345}),
    ("type 是 None", {"ts": _TS, "type": None}),
    ("不認得的 type", {"ts": _TS, "type": "brand-new-event"}),
    ("character_start 只剩 type", _only_type("character_start")),
    ("character_start 全 None", _with("character_start", name=None,
                                      target=None, folder=None,
                                      resumed=None)),
    ("character_done 只剩 type", _only_type("character_done")),
    ("character_done 全 None", _with("character_done", name=None, saved=None,
                                     target=None, elapsed_sec=None)),
    ("character_done saved 是字串", _with("character_done", saved="120")),
    ("character_done saved 是 nan", _with("character_done", saved=_NAN)),
    ("character_done target 是 None", _with("character_done", target=None)),
    ("character_done elapsed 是 None", _with("character_done",
                                             elapsed_sec=None)),
    ("character_done elapsed 是 nan", _with("character_done",
                                            elapsed_sec=_NAN)),
    ("character_done elapsed 是 inf", _with("character_done",
                                            elapsed_sec=_INF)),
    ("todo_done 只剩 type", _only_type("todo_done")),
    ("todo_done elapsed 是 None", _with("todo_done", elapsed_sec=None)),
    ("todo_done 總數是 None", _with("todo_done", total_saved=None,
                                    total_pairs=None)),
    ("quota_blocked 只剩 type", _only_type("quota_blocked")),
    ("quota_wait 只剩 type", _only_type("quota_wait")),
    ("quota_wait 兩個都 None", _with("quota_wait", waited_sec=None,
                                     next_retry_sec=None)),
    ("quota_wait 是字串", _with("quota_wait", waited_sec="3600",
                                next_retry_sec="900")),
    ("quota_resumed 只剩 type", _only_type("quota_resumed")),
    ("quota_resumed waited 是 None", _with("quota_resumed", waited_sec=None)),
    ("generation_blocked 只剩 type", _only_type("generation_blocked")),
    ("generation_blocked saved 是 None", _with("generation_blocked",
                                               saved=None)),
    ("critical_error 只剩 type", _only_type("critical_error")),
    ("critical_error 值不是字串", _with("critical_error", message=42,
                                        traceback=["a", "b"])),
    ("duplicate_image 只剩 type", _only_type("duplicate_image")),
    ("consecutive_failures 只剩 type", _only_type("consecutive_failures")),
    ("dom_result 只剩 type", _only_type("dom_result")),
    ("dom_result data 不是 list", _with("dom_result", data="abc")),
    ("dom_result data 裡不是 dict", _with("dom_result", data=["x", 5, None])),
    ("dom_result 欄位型別都不對",
     _with("dom_result", data=[{"index": None, "tag": None, "visible": None,
                                "value_len": None, "aria_label": 5,
                                "placeholder": None, "value_preview": None,
                                "parent_text": None}])),
    ("resume_mismatch 只剩 type", _only_type("resume_mismatch")),
    ("resume_mismatch fields 是字串", _with("resume_mismatch",
                                            fields="prompt")),
    ("resume_mismatch fields 裡不是字串", _with("resume_mismatch",
                                                fields=[None, 5, {"a": 1}])),
    ("resume_unusable 只剩 type", _only_type("resume_unusable")),
    ("resume_unusable saved 是 bool", _with("resume_unusable", saved=True)),
    ("resume_unusable reason 不是字串", _with("resume_unusable", reason=5)),
    ("page_recovery 只剩 type", _only_type("page_recovery")),
    ("schedule_rest 只剩 type", _only_type("schedule_rest")),
    ("schedule_rest wake 是 nan", _with("schedule_rest", wake_ts=_NAN)),
    ("schedule_rest wake 是 inf", _with("schedule_rest", wake_ts=_INF)),
    ("schedule_rest wake 超出範圍", _with("schedule_rest", wake_ts=1e18)),
    ("schedule_rest wake 是 bool", _with("schedule_rest", wake_ts=True)),
    ("schedule_rest rest 是 None", _with("schedule_rest", rest_sec=None)),
    ("schedule_resumed 只剩 type", _only_type("schedule_resumed")),
    ("schedule_resumed 是 None", _with("schedule_resumed", rested_sec=None)),
    ("chrome_restart 只剩 type", _only_type("chrome_restart")),
    ("paused 只剩 type", _only_type("paused")),
    ("resumed 只剩 type", _only_type("resumed")),
    ("single_image_done 沒有 request_id", _only_type("single_image_done")),
]


@pytest.mark.parametrize("label,event", _MALFORMED,
                         ids=[label for label, _ in _MALFORMED])
def test_a_malformed_event_never_escapes_into_the_watcher(label, event):
    """(a) 這個函式跑在背景 watcher 上，往外拋就是整條通知鏈停掉。"""
    deliver(event)      # 不得丟例外


# 上面那批裡，**該送出通知**的那些。`_format_duration_short` / `time.localtime`
# 對這些輸入原本會丟例外，於是訊息整則消失。
_MUST_STILL_NOTIFY = [
    ("character_done elapsed 是 None", _with("character_done",
                                             elapsed_sec=None)),
    ("character_done elapsed 是 nan", _with("character_done",
                                            elapsed_sec=_NAN)),
    ("character_done elapsed 是 inf", _with("character_done",
                                            elapsed_sec=_INF)),
    ("character_done saved 是字串", _with("character_done", saved="120")),
    ("character_done saved 是 nan", _with("character_done", saved=_NAN)),
    ("character_done 只剩 type", _only_type("character_done")),
    ("todo_done elapsed 是 None", _with("todo_done", elapsed_sec=None)),
    ("todo_done 只剩 type", _only_type("todo_done")),
    ("quota_wait 兩個都 None", _with("quota_wait", waited_sec=None,
                                     next_retry_sec=None)),
    ("quota_wait 只剩 type", _only_type("quota_wait")),
    ("quota_resumed waited 是 None", _with("quota_resumed", waited_sec=None)),
    ("schedule_rest wake 是 nan", _with("schedule_rest", wake_ts=_NAN)),
    ("schedule_rest wake 是 inf", _with("schedule_rest", wake_ts=_INF)),
    ("schedule_rest wake 超出範圍", _with("schedule_rest", wake_ts=1e18)),
    ("schedule_rest rest 是 None", _with("schedule_rest", rest_sec=None)),
    ("schedule_resumed 是 None", _with("schedule_resumed", rested_sec=None)),
]


@pytest.mark.parametrize("label,event", _MUST_STILL_NOTIFY,
                         ids=[label for label, _ in _MUST_STILL_NOTIFY])
def test_a_bad_number_does_not_swallow_the_whole_notification(label, event):
    """(b) 一個算不出來的欄位只該讓**那個欄位**變成未知，不是整則通知消失。

    `_handle_event` 最外層的 `except Exception` 讓 (a) 一直都成立，所以這個缺陷
    在事件迴圈那一側完全沒有症狀——它只表現成「那則通知從來沒出現過」。
    """
    chan, _ = deliver(event)
    assert chan.sent, (
        f"{label}：一個壞掉的數字把整則通知吃掉了。"
        "`_format_duration_short` / `time.localtime` 丟出來的例外會被最外層的 "
        "`except Exception` 接住——迴圈活著，使用者什麼都沒收到。")


def test_a_nan_saved_count_is_not_reported_as_a_real_number():
    """`nan >= target` 是 False，所以 `nan` 會安靜地變成「沒達標」。

    這是本專案第五次踩到「浮點特殊值讓比較型防線同時失效」。這裡要的不是「猜一個
    數字」，是**不要把一個算不出來的值印成看起來合理的答案**。
    """
    chan, _ = deliver({"type": "character_done", "name": "x",
                       "saved": float("nan"), "target": 10,
                       "elapsed_sec": 5})
    text = chan.texts[0]
    assert "nan" not in text.lower(), \
        f"把 `nan` 原樣印給使用者看了：{text!r}"


def test_a_bool_wake_time_is_not_rendered_as_a_plausible_clock():
    """`isinstance(True, (int, float))` 成立，於是 `True` 會被印成 1970 年的鐘點。

    這比丟例外更糟：一個看起來完全合理、實際上錯了半個世紀的醒來時間。
    """
    chan, _ = deliver({"type": "schedule_rest", "rest_sec": 60,
                       "wake_ts": True})
    text = chan.texts[0]
    assert "01-01" not in text, f"把 bool 當成時間戳印出來了：{text!r}"


# ---------------------------------------------------------------------------
# 六、`_format_duration_short` 本身
# ---------------------------------------------------------------------------
# 26 個呼叫點，2026-09-08 之前**一支測試都沒有**。其中好幾個餵的是磁碟上的
# 資料列（`now - row.get("created_at", now)`），而 `.get(k, default)` 的預設值
# 只在鍵不存在時生效——鍵在、值是 `null` 的話拿到的是 `None`。

@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"),
    (5, "5s"),
    (59.9, "59s"),
    (60, "1m 0s"),
    (3600, "1h 0s"),
    (3671, "1h 1m 11s"),
    (86400, "24h 0s"),
])
def test_duration_formatting_reads_the_way_a_person_expects(seconds, expected):
    assert b._format_duration_short(seconds) == expected


@pytest.mark.parametrize("bad", [
    None, "", "abc", float("nan"), float("inf"), float("-inf"),
    [], {}, object(),
])
def test_duration_formatting_never_raises_on_a_value_it_cannot_use(bad):
    """全函式：任何輸入都要回一個字串。

    它會丟例外的時候，丟掉的不是這個欄位，是**整則訊息**——呼叫端幾乎都是在組一句
    f-string，例外在字串組好之前就發生了。
    """
    out = b._format_duration_short(bad)
    assert isinstance(out, str) and out, f"{bad!r} 回了 {out!r}"


def test_duration_formatting_marks_an_unusable_value_as_unknown():
    """反面：不得替算不出來的值編一個 `0s` 出來。

    `0s` 是一個**看起來合理**的答案，而使用者沒有辦法分辨它是真的零還是壞掉。
    """
    assert b._format_duration_short(None) != "0s"
    assert b._format_duration_short(float("nan")) != "0s"


def test_a_bool_is_not_a_duration():
    """`True` 是 `int` 的子類別，會安靜地變成 `1s`。"""
    assert b._format_duration_short(True) == b._format_duration_short(None)


# ---------------------------------------------------------------------------
# 七、登記簿對帳
# ---------------------------------------------------------------------------
def _handled_event_types() -> set[str]:
    """AST 掃 `_handle_event` 裡每一個 `et == "…"`。

    用 AST 不用子字串：這個檔案的 docstring 與註解裡到處都是事件型別名字，
    子字串比對會把說明文字本身當成程式碼（本專案已經反覆踩過這個坑）。
    """
    for node in ast.walk(_BOT_TREE):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_handle_event"):
            found = set()
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Compare):
                    continue
                if not (isinstance(sub.left, ast.Name)
                        and sub.left.id == "et"):
                    continue
                for operand in sub.comparators:
                    if isinstance(operand, ast.Constant) \
                            and isinstance(operand.value, str):
                        found.add(operand.value)
                    elif isinstance(operand, (ast.Tuple, ast.List, ast.Set)):
                        found.update(
                            e.value for e in operand.elts
                            if isinstance(e, ast.Constant)
                            and isinstance(e.value, str))
            return found
    raise AssertionError("discord_bot.py 裡找不到 `_handle_event`")


def _emitted_event_shapes() -> dict[str, set[str]]:
    """AST 掃 `_webrunner_shared.py` 的 `emit_event("x", k=…)`，回 型別 → 鍵集合。

    讀**原始碼**不是 import：模組邊界那條規則禁止 bot 這一側匯入 webrunner，而
    測試只是在讀磁碟上的檔案（`test_webrunner_shared.py` 反過來也是這樣讀
    `discord_bot.py`）。
    """
    tree = ast.parse(
        (PKG_ROOT / "_webrunner_shared.py").read_text(encoding="utf-8"))
    shapes: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else "")
        if name != "emit_event":
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant)
                and isinstance(first.value, str)):
            continue
        keys = {kw.arg for kw in node.keywords if kw.arg}
        shapes.setdefault(first.value, set()).update(keys)
    return shapes


def test_the_samples_carry_the_fields_the_producer_actually_sends():
    """樣本的鍵集合要蓋滿送出端真的會送的欄位。

    這是「照著真實長相寫」那條慣例的執行版本。少一個鍵不會讓任何測試變紅——
    `event.get(k, default)` 會安靜地回預設值，於是測到的是**預設值那條路**而不是
    正式跑的那條路。所以只能從送出端反向對帳。

    順帶：新增一個 webrunner 端的事件欄位時這支會紅，逼人回來想一次「bot 要不要
    把它印出去？印出去會不會洩漏？」——`resume_mismatch` 的 `folder` 就是那種
    「送過來但刻意不印」的欄位。
    """
    emitted = _emitted_event_shapes()
    assert len(emitted) >= 15, \
        f"AST 只掃到 {len(emitted)} 種送出型別，掃描器八成壞了"
    problems = []
    for etype, keys in sorted(emitted.items()):
        if etype not in EVENT_SAMPLES:
            continue
        have = set(EVENT_SAMPLES[etype]) | set(EVENT_VARIANTS.get(etype, {}))
        missing = keys - have
        if missing:
            problems.append(f"{etype}: 少了 {sorted(missing)}")
    assert not problems, (
        "樣本沒跟上送出端的欄位：\n  " + "\n  ".join(problems)
        + "\n（少一個鍵時 `event.get()` 會安靜地回預設值，測到的是預設值那條路。）")


def test_no_sample_invents_a_field_the_producer_never_sends():
    """反方向：樣本也不得憑空多出送出端根本不會送的欄位。

    多出來的欄位比少掉的更難察覺——它會讓人以為某個分支有被測到。`ts` / `type`
    是 `emit_event` 自己補的，所以豁免。
    """
    emitted = _emitted_event_shapes()
    problems = []
    for etype, sample in sorted(EVENT_SAMPLES.items()):
        keys = emitted.get(etype)
        if keys is None:
            problems.append(f"{etype}: 送出端根本沒有這個型別")
            continue
        extra = set(sample) - keys - {"ts", "type"}
        if extra:
            problems.append(f"{etype}: 多出 {sorted(extra)}")
    assert not problems, "樣本與送出端對不起來：\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("etype", sorted(EVENT_VARIANTS))
def test_the_second_shape_of_a_two_call_site_event_also_works(etype):
    """有兩個以上送出點的型別，第二種形狀也要真的跑過一次。

    這些是「少一個鍵」的真實案例：`critical_error` 有一個送出點**不帶**
    `traceback`、`resume_unusable` 有一個不帶 `saved`。
    """
    deliver(EVENT_VARIANTS[etype])      # 不得丟例外


def test_every_branch_of_the_dispatcher_has_a_sample_here():
    """新增一個事件型別就要補一筆樣本，否則它會回到「從沒被跑過」的狀態。

    這一支是這個檔案唯一會**隨程式碼變動而變紅**的守門——上面那些都只釘現況。
    """
    handled = _handled_event_types()
    assert len(handled) >= 15, \
        f"AST 只掃到 {len(handled)} 個分支，掃描器八成壞了：{sorted(handled)}"
    missing = handled - set(EVENT_SAMPLES)
    assert not missing, (
        f"`_handle_event` 多了這些分支但這裡沒有樣本：{sorted(missing)}。"
        "沒有樣本＝那個分支從來沒被執行過，而它第一次執行的那天，"
        "正是最不能出錯的那天。")
    stale = set(EVENT_SAMPLES) - handled
    assert not stale, (
        f"這些樣本對應的分支已經不存在了：{sorted(stale)}")


# ---------------------------------------------------------------------------
# 壞掉的計數必須看起來就是壞的
# ---------------------------------------------------------------------------
def test_a_broken_count_reads_as_unknown_not_as_zero():
    """`_format_count` 對算不出來的值要回 `"?"`，**不可以回 `"0"`**。

    這條規則本來只寫在 `_format_count` 的 docstring 裡、沒有人驗：2026-09-08 的
    變異測試把 `return "?"` 改成 `return "0"`，45 支測試全部照樣通過。

    為什麼值得單獨釘：`0` 是一個**看起來完全合理**的答案。使用者收到
    「character X finished — `0/120` images」時沒有任何辦法分辨那是「真的一張都沒
    存到」（要立刻去看發生什麼事）還是「`saved` 欄位壞了」（產線其實好好的）。
    `?` 兩種都不像，所以它逼人去查而不是逼人下結論。這跟本專案反覆記的那條是同一
    件事：**錯的答案比沒有答案糟**（見 `/web dict` 把逾時講成「查無此字」）。
    """
    for bad in (None, float("nan"), float("inf"), float("-inf"), "", "abc",
                [], {}, True, False):
        got = b._format_count(bad)
        assert got == "?", f"{bad!r} 算出 {got!r}，應該是 '?'"

    # 反面：真正的 0 仍然要顯示成 0，否則「一律回 ?」也會讓上面全綠，而那會把
    # 一個真的需要注意的狀況（真的一張都沒存到）藏起來。
    assert b._format_count(0) == "0"
    assert b._format_count(0.0) == "0"
    assert b._format_count(120) == "120"


# ---------------------------------------------------------------------------
# `_poll_events_once` —— 讀取端本身
#
# 上面整份測的是「一筆事件變成什麼通知」。這一段測的是**外面那一圈**：offset 怎麼
# 推進、一行壞資料會不會把後面所有事件一起弄掉。
#
# 為什麼非測不可：那個 `finally` 的註解自己寫著它的存在理由是「即使中途拋例外也
# 要推進 offset，否則同一個壞行會被永遠重讀，之後所有事件都再也送不出去」——而在
# 2026-09-09 之前它在**那個情況下必定失敗**。原因是 `TextIOWrapper.__next__` 會關
# 掉內部的 `telling` 旗標（CPython bpo-37036），只有迭代跑完或再 `seek()` 才復原，
# 所以迴圈提早離開時 `f.tell()` 自己就丟 `OSError('telling position disabled by
# next() call')`。實測（CPython 3.14.4）：文字模式讀 1／3／10 行後 `tell()` 一律
# 丟，二進位模式同樣位置回正確的位元組位移。
#
# 兩層後果都靜默，所以這裡兩層都釘：
#   1. offset 沒推進 → 同一批事件每輪重讀、後面的事件永遠不送（這一支是核心）。
#   2. `finally` 裡的新例外**取代**原例外，於是 `CancelledError`（BaseException，
#      `_event_watcher` 的 `except Exception` 接不到）被換成 `OSError`（接得到）
#      ——取消被吞掉，關機路徑會等一個永遠不結束的 task。
# ---------------------------------------------------------------------------
def _write_events(path: Path, rows: list[str]) -> int:
    """寫出事件檔，回**磁碟上實際的位元組數**。

    ⚠️ 不要在測試裡自己算 `len("\\n".join(...).encode())`：`write_text` 預設
    `newline=None`，在 Windows 上會把每個 `\\n` 翻成 `\\r\\n`，於是手算的數字每行
    少一個位元組。這裡明寫 `newline="\\n"` 讓內容確定，但仍然回 `st_size`——期望值
    要來自被測系統看到的那份檔案，不是測試自己的算術。
    """
    path.write_text("".join(r + "\n" for r in rows),
                    encoding="utf-8", newline="\n")
    return path.stat().st_size


def _run_poll(monkeypatch, tmp_path, rows, *, handler=None, offset=0):
    """跑一次 `_poll_events_once`，回 (逃出來的例外, 事後的 offset, 假頻道, stderr)。

    刻意把 `_handle_event` 換成替身：這一段要測的是**外圈**，用真的 handler 就
    會把它的行為一起綁進來。
    """
    events = tmp_path / "events.ndjson"
    _write_events(events, rows)
    chan = FakeChannel()
    monkeypatch.setattr(b, "EVENTS_FILE", events)
    monkeypatch.setattr(b, "_event_offset", offset)
    monkeypatch.setattr(b.client, "get_channel", lambda *_a, **_k: chan)
    # `client.user` 是 property（沒有 setter），所以換掉 client 上的那個描述子不
    # 可行；改成餵一個 truthy 的 `_connection.user`… 也太黏。直接把整個
    # `client` 換成只長出這兩個屬性的替身最乾淨。
    monkeypatch.setattr(b, "client", _FakeClient(chan))
    if handler is not None:
        monkeypatch.setattr(b, "_handle_event", handler)
    escaped = None
    err = io.StringIO()
    with redirect_stderr(err):
        try:
            asyncio.run(b._poll_events_once())
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
            # 刻意**不是** `except BaseException`：那會連 `KeyboardInterrupt` /
            # `SystemExit` 一起吞掉，而 `test_nothing_swallows_cancellation` 正是
            # 在擋那個形狀（全專案零筆，不要為了這裡去開豁免）。這裡需要接住
            # `CancelledError` 是因為**它就是受測的東西**——上面那支測試要斷言
            # 「逃出來的是不是 CancelledError」，所以必須先把它捕捉下來。
            escaped = exc
    return escaped, b._event_offset, chan, err.getvalue()


class _FakeClient:
    """只長出 `_poll_events_once` 真的會碰到的兩個東西。"""

    def __init__(self, chan):
        self.user = object()          # truthy＝「快取就緒」
        self._chan = chan

    def get_channel(self, _cid):
        return self._chan


def test_a_file_shorter_than_the_offset_is_read_from_the_start(monkeypatch, tmp_path):
    """事件檔被輪替（整份重寫、變小）之後，舊的 offset 比檔案還大——要從頭讀。

    這個重設在整個套件裡從來沒有成立過（2026-09-22 分支覆蓋率），而拿掉它不會炸：
    `size <= _event_offset` 從此一直成立，**所有事件通知安靜地停掉**，直到檔案長回
    舊的大小。`_rotate_ndjson_tail` 就是會把這個檔變小的那一個。"""
    seen = []

    async def record(_chan, event):
        seen.append(event.get("i"))

    rows = ['{"type":"x","i":0}', '{"type":"x","i":1}']
    stale_offset = 10_000          # 輪替之前讀到的位置，遠大於新檔
    escaped, offset, _chan, _err = _run_poll(
        monkeypatch, tmp_path, rows, handler=record, offset=stale_offset)
    assert escaped is None, escaped
    assert seen == [0, 1], "輪替之後的事件沒有被讀到"
    assert offset == (tmp_path / "events.ndjson").stat().st_size


def test_the_offset_advances_even_when_a_handler_blows_up(monkeypatch, tmp_path):
    """**這一支是核心。**

    `_handle_event` 在第二筆炸掉時，offset 仍然必須推進到檔案結尾——那正是那個
    `finally` 唯一的存在理由。在修好之前這裡會失敗，而且是以一種特別誤導人的方式：
    逃出來的不是 handler 丟的 `AttributeError`，而是 `f.tell()` 自己丟的
    `OSError`（原例外被降級成 `__context__`），offset 停在 0。
    """
    seen = []

    async def boom(_chan, event):
        seen.append(event.get("i"))
        if event.get("i") == 1:
            raise AttributeError("simulated handler failure")

    rows = ['{"type":"x","i":0}', '{"type":"x","i":1}', '{"type":"x","i":2}']
    escaped, offset, _chan, _err = _run_poll(
        monkeypatch, tmp_path, rows, handler=boom)
    events_size = (tmp_path / "events.ndjson").stat().st_size

    # 前兩筆真的被送進 handler（正面對照組：否則「什麼都沒讀」也會讓下面成立）。
    assert seen == [0, 1], seen

    # 核心斷言：offset 推進**到已消費的那一段結尾**，也就是壞行的後面。
    #
    # 注意期望值不是整個檔案大小：迴圈在第 2 筆炸掉就離開了，第 3 筆還沒讀。推到
    # 檔尾反而會**吞掉**那筆沒處理過的事件，所以「剛好推過已消費的部分」才是對的
    # 語意——壞行不再重讀，未讀的仍然留著。（第一版這裡寫成檔案大小，是測試的算術
    # 錯，不是實作錯。）
    raw = (tmp_path / "events.ndjson").read_bytes()
    consumed = raw.index(b"\n", raw.index(b"\n") + 1) + 1   # 第 2 個換行之後
    assert 0 < consumed < events_size, "測資本身有問題：第 3 行應該還在後面"
    assert offset == consumed, (
        f"offset 停在 {offset}，應該是 {consumed}（檔案共 {events_size} bytes）——"
        "0 代表那個壞行會被永遠重讀，之後所有事件都再也送不出去")

    # 而且逃出去的是**真正的成因**，不是 `f.tell()` 自己的 OSError。
    assert isinstance(escaped, AttributeError), (
        f"逃出來的是 {escaped!r}；`finally` 裡的新例外把原例外換掉了")


def test_a_handler_failure_does_not_wedge_the_next_poll(monkeypatch, tmp_path):
    """端到端：第一輪炸掉之後，**第二輪**要把後面的事件送出去。

    上面那支看 offset 這個中間量；這支看使用者真正在意的結果。兩者缺一——offset
    的單位／比較邏輯若改壞，只看 offset 的那支未必抓得到。
    """
    calls = {"n": 0}

    async def boom_once(chan, event):
        calls["n"] += 1
        if event.get("i") == 1 and calls["n"] <= 2:
            raise AttributeError("simulated handler failure")
        await chan.send(f"handled {event.get('i')}")

    rows = ['{"type":"x","i":0}', '{"type":"x","i":1}', '{"type":"x","i":2}']
    events = tmp_path / "events.ndjson"
    _write_events(events, rows)
    chan = FakeChannel()
    monkeypatch.setattr(b, "EVENTS_FILE", events)
    monkeypatch.setattr(b, "_event_offset", 0)
    monkeypatch.setattr(b, "client", _FakeClient(chan))
    monkeypatch.setattr(b, "_handle_event", boom_once)

    async def two_ticks():
        for _ in range(2):
            try:
                await b._poll_events_once()
            except Exception:  # noqa: BLE001  ＝ `_event_watcher` 的形狀
                pass

    with redirect_stderr(io.StringIO()):
        asyncio.run(two_ticks())

    # 第 3 筆（i=2）必須送出去。修好之前它永遠送不出來：第二輪從 offset 0 重讀，
    # 又在 i=1 炸掉，無限重複。
    assert "handled 2" in chan.blob, (
        f"壞行後面的事件再也送不出去了：{chan.texts}")


def test_a_cancel_inside_the_loop_is_not_swallowed(monkeypatch, tmp_path):
    """`CancelledError` 不得被 `finally` 換成 `OSError`。

    這條是**取消語意**，不是紀錄語意：`CancelledError` 是 `BaseException`，所以
    `_event_watcher` 的 `except Exception` 本來接不到它；一旦被換成 `OSError` 就
    接得到了，於是取消被吞掉、watcher 繼續跑。實測那個狀態下
    `task.cancelling() == 1` 但 task 永遠不結束，而關機路徑
    （`client.close()` → `asyncio.run` 收尾的 `_cancel_all_tasks` → `gather`）
    會等它——也就是 `/sys restart` 掛住。
    """
    async def cancel_on_second(_chan, event):
        if event.get("i") == 1:
            raise asyncio.CancelledError()

    rows = ['{"type":"x","i":0}', '{"type":"x","i":1}', '{"type":"x","i":2}']
    escaped, _offset, _chan, _err = _run_poll(
        monkeypatch, tmp_path, rows, handler=cancel_on_second)

    assert isinstance(escaped, asyncio.CancelledError), (
        f"逃出來的是 {escaped!r} 而不是 CancelledError——"
        "取消被降級成一個 `except Exception` 接得住的例外，於是會被吞掉")
    # 反過來確認它**沒有**變成 Exception 的子類（那才是被吞掉的成因）。
    assert not isinstance(escaped, Exception), escaped


def test_a_non_dict_line_is_skipped_not_fatal(monkeypatch, tmp_path):
    """一行「解得開但不是 dict」的 JSON 只跳過那一行。

    `123` / `"x"` / `null` / `[]` 都是合法 JSON，所以 `_json.loads` 不會拋；而
    `_handle_event` 的第一行 `event.get("type")` 在它的 `try` **外面**，所以那一行
    會丟 `AttributeError` 逃出整輪輪詢。這正是上面那條 offset 缺陷唯一實際走得到
    的觸發路徑，所以兩邊都要修：擋住這一行（本支），以及萬一還是拋了也要能推進
    offset（前兩支）。

    `_read_events_tail` 一直都有 `isinstance(ev, dict)`，`_poll_events_once` 沒有
    ——同一個檔案的兩個讀取端，一個擋一個不擋。
    """
    delivered = []

    async def record(_chan, event):
        delivered.append(event)

    rows = ['{"type":"good","i":0}', '123', '"x"', 'null', '[]',
            '{"type":"good","i":1}']
    escaped, offset, _chan, err = _run_poll(
        monkeypatch, tmp_path, rows, handler=record)

    assert escaped is None, f"非 dict 的行讓整輪輪詢拋了 {escaped!r}"
    # 正面對照組：合法的兩筆都要送到（否則「一律跳過」也會綠）。
    assert [e.get("i") for e in delivered] == [0, 1], delivered
    assert offset == (tmp_path / "events.ndjson").stat().st_size
    # 被跳過的要留下診斷，而診斷**只能有型別名、不得回聲那一行的內容**。
    # 這行 stderr 會進 `discord_bot.log`，而 `/log tail` 會把那個檔案送進
    # Discord；「解得開但不是 dict」最常見的形狀是一個裸字串，而 `events.ndjson`
    # 的字串欄位裝的正是主機路徑。中間雖然還隔著 `_redact_for_discord`，但那條
    # 保證只由一個靠樣式比對的下游 scrubber 撐著——本專案的規則是先在本地擋掉。
    assert "non-dict" in err, err
    assert "int" in err and "list" in err, f"型別名不見了：{err!r}"
    for leaked in ('"x"', "[]", "123"):
        assert leaked not in err, (
            f"診斷把那一行的內容回聲出來了（{leaked!r}）：{err!r}")


def test_a_non_dict_line_would_reach_handle_event_unguarded():
    """釘住**為什麼**需要上面那道檢查：`_handle_event` 自己接不住它。

    少了這一支，「那道 isinstance 是多餘的」會是一個看起來合理的清理——而它會
    靜靜地把整條事件通知鏈交還給一行壞資料。這裡直接證明機制：`_handle_event`
    的第一行在它的 `try` 外面，所以非 dict 一定會逃出來。
    """
    with pytest.raises(AttributeError):
        asyncio.run(b._handle_event(FakeChannel(), 123))


def test_the_events_reader_uses_binary_mode():
    """AST：`_poll_events_once` 必須用二進位模式開檔。

    行為測試蓋得到「offset 有沒有推進」，但蓋不到「用哪種模式達成的」。有人把
    `open("rb")` 改回 `open("r", encoding=...)` 之後，前面幾支**在正常路徑上仍然
    會綠**（迭代跑完時文字模式的 `tell()` 是好的），只有拋例外那條會紅——所以這裡
    把機制本身也釘住，讓回歸在最直接的地方就變紅。

    同時**反向**釘住 `_read_events_tail` 維持文字模式：壞的是 `tell()` 那一側，
    不是 `seek()`，而它對 `st_size` 算出來的任意整數做文字模式 `seek()` 實測完全
    正確。把這條「推廣」過去只是無謂的改動風險。
    """
    modes = {}
    for node in ast.walk(_BOT_TREE):
        if not (isinstance(node, ast.AsyncFunctionDef)
                or isinstance(node, ast.FunctionDef)):
            continue
        if node.name not in ("_poll_events_once", "_read_events_tail"):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "open"
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "EVENTS_FILE"):
                mode = None
                if sub.args and isinstance(sub.args[0], ast.Constant):
                    mode = sub.args[0].value
                for kw in sub.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                modes[node.name] = mode

    # 正面對照組：兩個都要真的被掃到，否則下面兩句是空轉。
    assert set(modes) == {"_poll_events_once", "_read_events_tail"}, (
        f"AST 只掃到 {sorted(modes)}——掃描器八成壞了（或呼叫形狀變了）")
    assert modes["_poll_events_once"] and "b" in modes["_poll_events_once"], (
        f"`_poll_events_once` 用 {modes['_poll_events_once']!r} 開檔；"
        "文字模式下 `finally` 的 `f.tell()` 會在它唯一存在的理由上失敗")
    assert "b" not in (modes["_read_events_tail"] or ""), (
        "`_read_events_tail` 被改成二進位了——它不需要改（見 docstring）")
