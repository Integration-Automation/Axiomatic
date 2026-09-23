"""工作階段存放檔（`dorossi_session.json`）的載入／搬移／存檔。

這個檔案裝的是擁有者**所有** Dorossi 對話的續接資訊（後端 session id、工作目錄、
自走任務的 `loop_pending` 標記）。弄丟它不是「少一個設定」，是每一段對話的上下文
一次全部消失，而且救不回來——後端那一側的 session 只認 id。

2026-09-05 量覆蓋率時發現整條載入路徑**一行都沒被跑過**。程式本身寫得很小心
（毀損就搬到 `.bad`、任何一步失敗都 fail-soft），但沒有任何測試在守，而這裡最危險
的一段恰恰是「看起來可以簡化」的那一段：

    毀損 → 搬去 `.bad` → 回空 state

把中間那一步拿掉、直接回空 state，看起來完全合理、所有既有測試照樣綠——然後下一次
`_dorossi_save_state` 會用「空 state」整檔覆寫（存檔是 temp + `os.replace` 的**全檔
取代**），使用者的所有工作階段就此永久消失，連手動救回的機會都沒有。這一支測試存在
的主要理由就是釘住那一步。

其餘釘的是「毀損檔不得讓 bot 起不來」那一類：頂層不是 dict、某個使用者的紀錄是垃圾、
`active` 指向已經不存在的工作階段、`next_seq` 落後於現有 id（重新發號會撞掉一個活著
的工作階段）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import dorossi_backend as db  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """把工作階段存放檔指到 tmp，回傳那個 Path。"""
    path = tmp_path / "dorossi_session.json"
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", path)
    return path


# ---------------------------------------------------------------------------
# 載入
# ---------------------------------------------------------------------------

def test_a_missing_file_is_just_no_sessions_yet(store):
    assert db._dorossi_load_state() == {}
    assert not store.exists(), "檔案不存在時不該順手建一個"
    assert not store.with_name(store.name + ".bad").exists(), (
        "檔案不存在不是毀損，不該搬去 .bad")


def test_a_corrupt_file_is_quarantined_before_the_empty_state_is_returned(store):
    """**這一支是本檔的重點。** 毀損檔必須先被搬走再回空 state。

    不搬走的話它會原樣留在磁碟上，而下一次存檔是 temp + `os.replace` 的全檔取代——
    等於用空 state 蓋掉使用者所有的工作階段，而且沒有第二份。
    """
    store.write_text('{"400000000000000001": {"sessions": ', encoding="utf-8")
    original = store.read_bytes()

    assert db._dorossi_load_state() == {}

    bad = store.with_name(store.name + ".bad")
    assert bad.exists(), (
        "毀損的工作階段檔沒有被搬到 `.bad`——下一次存檔會把它整個蓋掉，"
        "使用者所有對話的續接資訊就永久消失了")
    assert bad.read_bytes() == original, "搬過去的內容必須原封不動，才救得回來"
    assert not store.exists(), "搬走之後原位置不該還留著壞檔"


def test_a_later_save_does_not_touch_the_quarantined_copy(store):
    """搬走之後照常存檔，`.bad` 不得被覆寫——不然搬它就沒有意義了。"""
    store.write_text("} not json {", encoding="utf-8")
    db._dorossi_load_state()
    bad = store.with_name(store.name + ".bad")
    kept = bad.read_bytes()

    db._dorossi_save_state({"1": db._dorossi_empty_user()})
    assert bad.read_bytes() == kept
    assert json.loads(store.read_text(encoding="utf-8"))["1"]["sessions"] == {}


def test_a_failed_quarantine_still_returns_an_empty_state(store, monkeypatch):
    """搬檔本身失敗也不能拋——載入器的契約是「永不 raise」。"""
    store.write_text("nonsense", encoding="utf-8")

    def boom(_src, _dst):
        raise OSError("cannot rename")

    monkeypatch.setattr(db.os, "replace", boom)
    assert db._dorossi_load_state() == {}


def test_an_unreadable_file_is_not_quarantined(store, monkeypatch):
    """讀不到（鎖住／權限）是**暫時性**問題，不是毀損：這次當成沒有工作階段，
    但絕不能把檔案搬走——下一秒可能就讀得到了。"""
    store.write_text('{"1": {"sessions": {}}}', encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise PermissionError("locked by another process")

    monkeypatch.setattr(Path, "read_text", boom)
    assert db._dorossi_load_state() == {}
    assert not store.with_name(store.name + ".bad").exists(), (
        "暫時性讀取失敗被當成毀損搬走了——那會在一次檔案鎖之後丟掉所有工作階段")


def test_a_top_level_list_is_not_a_store(store):
    store.write_text("[1, 2, 3]", encoding="utf-8")
    assert db._dorossi_load_state() == {}


# ---------------------------------------------------------------------------
# 遷移
# ---------------------------------------------------------------------------

def test_a_legacy_flat_record_keeps_its_conversation(store):
    """舊格式（沒有 `sessions` 鍵的單層 dict）要被包成 `s1`，內容原樣保留。

    這是唯一一次性的搬家路徑：包錯了就是把擁有者既有的那段對話丟掉。
    """
    store.write_text(json.dumps({
        "400000000000000001": {"cc_session_id": "abc-123", "cwd": "D:/x"},
    }), encoding="utf-8")

    rec = db._dorossi_load_state()["400000000000000001"]
    assert rec["active"] == "s1"
    assert rec["next_seq"] == 2, "下一個號碼要跳過已經用掉的 s1"
    assert rec["sessions"]["s1"]["cc_session_id"] == "abc-123"
    assert rec["sessions"]["s1"]["cwd"] == "D:/x"


def test_a_garbage_user_record_does_not_take_the_others_down(store):
    store.write_text(json.dumps({
        "1": "this is not a record",
        "2": {"sessions": {"s1": {"cc_session_id": "keep-me"}},
              "active": "s1", "next_seq": 2},
    }), encoding="utf-8")

    state = db._dorossi_load_state()
    assert state["1"] == db._dorossi_empty_user()
    assert state["2"]["sessions"]["s1"]["cc_session_id"] == "keep-me", (
        "一個壞掉的使用者紀錄不該波及別人的對話")


def test_an_active_pointer_to_a_missing_session_is_dropped(store):
    store.write_text(json.dumps({
        "1": {"sessions": {"s1": {}}, "active": "s9", "next_seq": 2},
    }), encoding="utf-8")
    assert db._dorossi_load_state()["1"]["active"] is None, (
        "`active` 指向不存在的工作階段時要清掉，否則每個讀它的地方都要自己防")


def test_the_counter_never_re_issues_a_live_session_id(store):
    """`next_seq` 落後於現有 id 時要往上補。

    不補的話下一個 `new` 會發出一個**已經有人在用**的 id，兩段對話就此合流——
    後端那一側只認 id，合流之後分不開。
    """
    store.write_text(json.dumps({
        "1": {"sessions": {"s1": {}, "s7": {}}, "active": "s1", "next_seq": 2},
    }), encoding="utf-8")
    assert db._dorossi_load_state()["1"]["next_seq"] == 8


def test_a_counter_that_is_already_ahead_is_left_alone(store):
    """號碼只進不退：往回調會讓已經封存過的 id 被重新發出去。"""
    store.write_text(json.dumps({
        "1": {"sessions": {"s1": {}}, "active": "s1", "next_seq": 50},
    }), encoding="utf-8")
    assert db._dorossi_load_state()["1"]["next_seq"] == 50


def test_a_non_numeric_counter_falls_back_without_colliding(store):
    store.write_text(json.dumps({
        "1": {"sessions": {"s3": {}}, "active": "s3", "next_seq": "lots"},
    }), encoding="utf-8")
    assert db._dorossi_load_state()["1"]["next_seq"] == 4


# ---------------------------------------------------------------------------
# 存檔
# ---------------------------------------------------------------------------

def test_a_saved_store_loads_back_unchanged(store):
    state = {"1": {"sessions": {"s1": {"cc_session_id": "x", "label": "測試"}},
                   "active": "s1", "next_seq": 2}}
    db._dorossi_save_state(state)
    assert db._dorossi_load_state() == state


def test_saving_leaves_no_temp_file_behind(store):
    db._dorossi_save_state({"1": db._dorossi_empty_user()})
    assert not store.with_name(store.name + ".tmp").exists(), (
        "temp 檔沒有被 `os.replace` 消化掉——存檔不是原子的")


def test_a_failed_save_does_not_raise(store, monkeypatch):
    """存檔失敗只記 log。這是在自走迴圈的路徑上跑的，拋出去等於一次磁碟打嗝
    就終結整個無人值守任務。"""
    def boom(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(db.os, "replace", boom)
    db._dorossi_save_state({"1": db._dorossi_empty_user()})   # 不得 raise


def test_non_ascii_survives_the_round_trip(store):
    """標籤是使用者自由文字，中文佔絕大多數；`ensure_ascii=False` 掉了會變跳脫字串。"""
    db._dorossi_save_state({"1": {"sessions": {"s1": {"label": "產圖排程"}},
                                  "active": "s1", "next_seq": 2}})
    assert "產圖排程" in store.read_text(encoding="utf-8")
    assert db._dorossi_load_state()["1"]["sessions"]["s1"]["label"] == "產圖排程"


def test_a_user_record_that_explodes_is_isolated_not_fatal():
    """`_dorossi_migrate_state` 的契約是「任何垃圾都不會 raise」，所以逐筆隔離。

    直接呼叫、不經過 JSON：從檔案讀進來的東西受限於 JSON 的型別，構不出「`.get`
    自己會爆」的紀錄，但這個函式的呼叫端不只有載入器。少了逐筆的 `try`，一筆壞
    紀錄會讓**整份** state 拋出去——而它的呼叫者（載入器）宣稱永不 raise。
    """
    class _Hostile(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("this record is cursed")

    state = {"1": _Hostile(), "2": {"sessions": {"s1": {"cc_session_id": "keep"}},
                                    "active": "s1", "next_seq": 2}}
    out = db._dorossi_migrate_state(state)
    assert out["1"] == db._dorossi_empty_user()
    assert out["2"]["sessions"]["s1"]["cc_session_id"] == "keep"


# ---------------------------------------------------------------------------
# 「太久沒用就從新對話開始」——`_dorossi_session_is_stale`
#
# 這支決定的是**要不要把一段對話的續接資訊丟掉**，而丟掉之後使用者不會收到任何
# 通知：下一輪的回答只是突然不記得前面講過的事。所以兩個方向的代價都不小，而且
# 都很難事後查證：判太鬆 → 長壽工作階段無限長大（它存在的理由）；判太嚴 →
# 靜默失憶。2026-09-06 量覆蓋率時發現它一行都沒被跑過。
# ---------------------------------------------------------------------------

def test_a_fresh_session_is_not_stale(monkeypatch):
    monkeypatch.setattr(db, "DOROSSI_SESSION_MAX_AGE_DAYS", 14.0)
    assert not db._dorossi_session_is_stale({"last_used": time.time()})


def test_a_long_idle_session_is_stale(monkeypatch):
    monkeypatch.setattr(db, "DOROSSI_SESSION_MAX_AGE_DAYS", 14.0)
    old = time.time() - 15 * 86400
    assert db._dorossi_session_is_stale({"last_used": old})


def test_the_boundary_counts_as_stale(monkeypatch):
    """`>=`，不是 `>`。差一格在這裡是「多留一天」，無害但要講清楚是哪一邊。"""
    monkeypatch.setattr(db, "DOROSSI_SESSION_MAX_AGE_DAYS", 14.0)
    assert db._dorossi_session_is_stale({"last_used": time.time() - 14 * 86400})


def test_created_at_is_used_when_the_session_was_never_used(monkeypatch):
    """`new` 之後一次都沒問過的工作階段，也該跟著同一把尺老化。"""
    monkeypatch.setattr(db, "DOROSSI_SESSION_MAX_AGE_DAYS", 14.0)
    assert db._dorossi_session_is_stale({"created_at": time.time() - 30 * 86400})
    assert not db._dorossi_session_is_stale({"created_at": time.time()})


@pytest.mark.parametrize("age", [0, -1, -0.5])
def test_zero_or_negative_disables_the_rule(monkeypatch, age):
    """0 ＝停用（與本專案其他門檻同慣例）。負數不該變成「什麼都算過期」。"""
    monkeypatch.setattr(db, "DOROSSI_SESSION_MAX_AGE_DAYS", age)
    assert not db._dorossi_session_is_stale({"last_used": 0.0})


@pytest.mark.parametrize("bad", [
    {}, {"last_used": None}, {"last_used": "x"}, {"last_used": True},
    {"last_used": 0}, {"last_used": -5},
])
def test_a_missing_or_broken_timestamp_never_counts_as_stale(monkeypatch, bad):
    """存放檔是磁碟上的檔案。壞掉的時間戳讓人失憶，比讓人多留一段對話糟得多。

    `True` 那一筆不是湊數：`bool` 是 `int` 的子類別，少了 `isinstance(x, bool)`
    這道，`last_used: true` 會被當成「1970 年用過」——也就是**一律過期**。
    """
    monkeypatch.setattr(db, "DOROSSI_SESSION_MAX_AGE_DAYS", 14.0)
    assert not db._dorossi_session_is_stale(bad)


def test_a_future_timestamp_never_counts_as_stale(monkeypatch):
    """時鐘往回校（NTP 的 step 修正、手動改時鐘、虛擬機快照還原）會讓 `last_used` 落在未來。

    那不是「非常舊」，是「讀到的資料沒意義」，不該拿它當丟掉對話的依據。
    """
    monkeypatch.setattr(db, "DOROSSI_SESSION_MAX_AGE_DAYS", 14.0)
    assert not db._dorossi_session_is_stale({"last_used": time.time() + 86400})


# ---------------------------------------------------------------------------
# 畸形的 slot 在**遷移層**就被丟掉（2026-09-20）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("junk", [
    "junk", None, 0, [], ["x"], 1.5, True, "", (), set(),
])
def test_a_session_slot_that_is_not_a_dict_is_dropped_at_migration(junk):
    """每一個讀取端都直接對 slot 呼叫 `.get(...)`。

    `dorossi_session.json` 是本機檔案、擁有者自己編輯得動，所以一筆手改出來的畸形
    slot 會讓工作階段清單、每一輪問答取的快照、匯出**全部**炸 `AttributeError`
    ——而那正是擁有者最需要那些指令的時候。

    攔在這裡而不是各讀取端各防一次，是因為這裡是整個存放檔的**單一正規化入口**
    （`_dorossi_load_state` 一定走 `_dorossi_migrate_state`）。分散防守會漏掉下一個
    新增的讀取端，而漏掉的那一個不會有任何症狀，直到有人真的手改過那個檔案。
    """
    out = db._dorossi_migrate_user({"active": "s1", "sessions": {"s1": junk}})
    assert out["sessions"] == {}
    assert out["active"] is None


def test_a_good_slot_survives_next_to_a_malformed_one():
    """丟掉的必須**只有**壞的那一筆。整包清空等於因為一個打字錯誤刪光所有對話。"""
    good = {"created_at": 1.0, "label": "留著"}
    out = db._dorossi_migrate_user(
        {"active": "s1", "sessions": {"s1": good, "s2": "junk"}})
    assert set(out["sessions"]) == {"s1"}
    assert out["sessions"]["s1"] == good
    assert out["active"] == "s1"


def test_the_active_pointer_never_survives_the_slot_it_points_at():
    """丟棄發生在修補 `active` **之前**。

    順序反過來的話 `active` 會指著一個已經不存在的 id，而下一輪問答拿它去找 slot
    ——`_dorossi_active_session` 會靜靜開一個新的，使用者只看到「突然失憶」。
    """
    out = db._dorossi_migrate_user(
        {"active": "s2", "next_seq": 3,
         "sessions": {"s1": {"created_at": 1.0}, "s2": "junk"}})
    assert out["active"] is None
    assert set(out["sessions"]) == {"s1"}


def test_a_non_string_session_id_is_dropped_too():
    out = db._dorossi_migrate_user({"sessions": {1: {"created_at": 1.0}}})
    assert out["sessions"] == {}


def test_dropping_a_slot_does_not_rewind_the_id_counter():
    """`next_seq` 是「下一個要發的號碼」，而它記在檔案裡、不是從現有 slot 推出來的。

    丟掉一個 slot 之後把計數器倒回去，之後就會重新發一個**曾經用過**的 id——而磁碟
    上的舊匯出檔還寫著那個 id，兩段不同的對話就此共用一個名字。

    ※ 刻意只斷言「檔案裡有記就要保留」這一半。另一半（檔案裡的 `next_seq` 也壞掉時
    要從哪裡推）目前是從**丟棄後**的 slot 推的，所以理論上會倒退；那需要一個同時
    壞掉兩處的檔案，而且推出來的值仍不會撞到留下來的 slot。寫成斷言會變成把一個
    實作細節釘死，所以留成這一行說明。
    """
    out = db._dorossi_migrate_user({"next_seq": 99, "sessions": {"s9": "junk"}})
    assert out["next_seq"] == 99


def test_healthy_state_is_passed_through_without_rebuilding_it():
    """必須放行的那一面，而且比「輸出相等」更強：**沒有畸形 slot 時連物件都不換**。

    只斷言內容相等的話，一個「每次都重建一份」的實作照樣通過——而重建會讓呼叫端
    先前取得的 slot 參考指向舊的那一份，之後的就地修改寫不回去（本專案的
    `_dorossi_active_session` 明講「回傳的是 state 裡的活 slot」）。
    """
    sessions = {"s1": {"created_at": 1.0}}
    out = db._dorossi_migrate_user(
        {"active": "s1", "next_seq": 2, "sessions": sessions})
    assert out["sessions"] is sessions


def test_dropping_a_slot_does_not_mutate_the_callers_dict():
    """丟棄要做在複本上。就地改的話，呼叫端手上那一份會被悄悄改掉——而
    `_dorossi_migrate_state` 正是拿呼叫端的 state 在跑。
    """
    sessions = {"s1": {"created_at": 1.0}, "s2": "junk"}
    db._dorossi_migrate_user({"sessions": sessions})
    assert set(sessions) == {"s1", "s2"}


def test_a_legacy_flat_record_is_still_wrapped_whole():
    """舊格式（沒有 `sessions` 鍵）整包包成 `s1`，行為不得改變——那是把一段真的
    對話救回來的路徑。"""
    out = db._dorossi_migrate_user({"cc_session_id": "abc", "label": "舊的"})
    assert out["sessions"]["s1"]["cc_session_id"] == "abc"
    assert out["active"] == "s1"


def test_the_drop_diagnostic_withholds_ids_it_cannot_recognise(capsys):
    """診斷只印**形狀像工作階段 id** 的 key。

    這一行會進背景程式的記錄檔，而記錄檔查詢指令會把內容送進聊天平台；中間只有一個
    靠樣式比對的過濾器，認不得手改檔案裡沒見過的字串形狀。手改出來的 key 可能裝著
    任何東西（主機路徑、提示詞片段），所以不認得的一律只計數。
    """
    secret = "D:/Work/Example/auth.md"
    db._dorossi_migrate_user({"sessions": {secret: "junk", "s3": "junk"}})
    err = capsys.readouterr().err
    assert secret not in err, "認不得的 key 被原樣印出來了"
    assert "s3" in err, "認得的 id 應該印出來，否則診斷沒有用"
    assert "2" in err, "沒有報出丟掉的總數"


def test_a_healthy_record_says_nothing(capsys):
    """會亂叫的診斷最後會被人忽略——正常資料一個字都不該印。"""
    db._dorossi_migrate_user({"active": "s1", "next_seq": 2,
                              "sessions": {"s1": {"created_at": 1.0}}})
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("junk", [
    {"sessions": {"s1": object()}},
    {"sessions": {frozenset(): {}}},
    {"sessions": {"s1": float("nan")}},
    {"next_seq": 10 ** 30, "sessions": {"s1": "junk"}},
])
def test_migration_still_never_raises(junk):
    """這支函式的 docstring 寫著 "Never raises"，而它的輸入來自一個人改得動的檔案。"""
    assert isinstance(db._dorossi_migrate_user(junk), dict)
