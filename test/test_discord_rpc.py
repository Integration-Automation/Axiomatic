"""Tests for `discord_rpc.RichPresenceClient.apply` 的回應處理。

可直接 `py -3 test/test_discord_rpc.py`（自帶 runner），也可 pytest。

重點是那個**靜默成功**的坑：桌面端拒絕一個 activity 時，回的是**正常 FRAME**
帶 `{"evt": "ERROR", …}`，不是 CLOSE。只判斷 op 的話會一路回 `ok`，還把 payload
key 記進去重快取——接下來整個 refresh_sec 都不再重送，狀態指令顯示成功，實際上
狀態從頭到尾沒設起來。這種錯誤沒有任何外顯徵兆，只能靠測試釘住。

匯入走「把本檔所在目錄放進 sys.path，再直接 import」這條路（與其餘 test_*.py
一致），單獨執行才不會 ModuleNotFoundError。
"""
import ast as _ast
import pathlib as _pathlib
import builtins
import io
import json
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import pytest  # noqa: E402

import discord_rpc  # noqa: E402

# 下面新增的那一批用短別名，跟其餘 test_*.py 的寫法一致。
rpc = discord_rpc


def _frame(op: int, payload: dict) -> bytes:
    data = json.dumps(payload).encode("utf-8")
    return struct.pack("<II", op, len(data)) + data


_READY = _frame(1, {"cmd": "DISPATCH", "evt": "READY", "data": {}})
_ACCEPTED = _frame(1, {"cmd": "SET_ACTIVITY", "evt": None,
                       "data": {"name": "X"}, "nonce": "n"})
_REJECTED = _frame(1, {"cmd": "SET_ACTIVITY", "evt": "ERROR",
                       "data": {"code": 4000,
                                "message": "Invalid activity payload"},
                       "nonce": "n"})


class _FakeTransport:
    """照 frame 協定回答預先排好的回應，並記下送出去的每一個 frame。"""

    def __init__(self):
        self.path = r"\\fake\pipe"
        self._inbox = b""
        self.sent: list[bytes] = []
        self.closed = False

    def queue(self, *frames: bytes) -> None:
        self._inbox += b"".join(frames)

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    def read(self, n: int) -> bytes:
        chunk, self._inbox = self._inbox[:n], self._inbox[n:]
        return chunk

    def close(self) -> None:
        self.closed = True


def _client(monkeypatch_like) -> tuple:
    """回 `(client, transport)`，已排好 handshake 的 READY 回應。
    `monkeypatch_like` 是個接受 (module, name, value) 的 setter。"""
    transport = _FakeTransport()
    transport.queue(_READY)
    monkeypatch_like(discord_rpc, "_open_transport", lambda: transport)
    return discord_rpc.RichPresenceClient(), transport


_ACTIVITY = {"name": "X", "type": 0}

# apply() 的回傳值會被呼叫端原樣貼進聊天訊息，所以只能是這組固定詞彙。
_VOCABULARY = {"disabled", "not-connected", "unchanged", "ok",
               "send-failed", "rejected"}


def test_a_rejected_activity_is_not_reported_as_success(setattr_fn):
    client, transport = _client(setattr_fn)
    transport.queue(_REJECTED)
    result = client.apply(_ACTIVITY, "123", refresh_sec=60.0)
    assert result == "rejected", f"被拒卻回了 {result!r}"
    assert result in _VOCABULARY


def test_a_rejection_keeps_the_connection_and_retries_next_tick(setattr_fn):
    """被拒是 payload 的問題、不是連線的問題：不可以斷線（會變成重連迴圈），
    也不可以把 payload key 記進去重快取（會整個 refresh_sec 不再重送）。"""
    client, transport = _client(setattr_fn)
    transport.queue(_REJECTED)
    client.apply(_ACTIVITY, "123", refresh_sec=60.0)
    assert client.status()["connected"] is True, "被拒不該把連線收掉"

    sent_before = len(transport.sent)
    transport.queue(_ACCEPTED)
    # 同一個 activity、同一個 refresh 視窗內再送一次：因為上次被拒沒有記進
    # 去重快取，這次必須真的重送，而不是回 unchanged。
    result = client.apply(_ACTIVITY, "123", refresh_sec=60.0)
    assert len(transport.sent) > sent_before, "被拒後下個 tick 沒有重試"
    assert result == "ok"


def test_an_accepted_activity_is_deduped_within_the_refresh_window(setattr_fn):
    """去重本身要照舊有效，別為了修上面那條把保活節流也一起弄壞。"""
    client, transport = _client(setattr_fn)
    transport.queue(_ACCEPTED)
    assert client.apply(_ACTIVITY, "123", refresh_sec=60.0) == "ok"
    sent_after_first = len(transport.sent)
    assert client.apply(_ACTIVITY, "123", refresh_sec=60.0) == "unchanged"
    assert len(transport.sent) == sent_after_first, "unchanged 不該做 I/O"


def test_rejection_detail_never_leaks_into_the_return_value(setattr_fn):
    """回傳值會被貼進聊天訊息；原始錯誤文字只能進 stderr / status()。"""
    client, transport = _client(setattr_fn)
    transport.queue(_REJECTED)
    result = client.apply(_ACTIVITY, "123", refresh_sec=60.0)
    assert "Invalid activity payload" not in result
    assert "4000" not in result
    # 細節該留在診斷用的欄位裡，讓 log 查得到。
    assert "Invalid activity payload" in client.status()["last_error"]


_TESTS = [
    ("test_a_rejected_activity_is_not_reported_as_success",
     test_a_rejected_activity_is_not_reported_as_success),
    ("test_a_rejection_keeps_the_connection_and_retries_next_tick",
     test_a_rejection_keeps_the_connection_and_retries_next_tick),
    ("test_an_accepted_activity_is_deduped_within_the_refresh_window",
     test_an_accepted_activity_is_deduped_within_the_refresh_window),
    ("test_rejection_detail_never_leaks_into_the_return_value",
     test_rejection_detail_never_leaks_into_the_return_value),
]


# pytest 走 fixture；standalone 走下面自己做的 setter + 還原。
try:
    import pytest

    @pytest.fixture(name="setattr_fn")
    def _setattr_fn(monkeypatch):
        return monkeypatch.setattr
except ImportError:  # pragma: no cover - standalone runner only
    pass


def main():
    original = discord_rpc._open_transport
    try:
        for name, fn in _TESTS:
            print(name)
            fn(setattr)
            print("  PASS\n")
    finally:
        discord_rpc._open_transport = original
    # 自帶 runner 一定要能回答「檔案裡到底宣告了幾支」，否則少跑的時候會印出一句
    # 不帶條件的「ALL N …PASSED」，被讀成「全部都過了」。差額可以存在（吃 pytest
    # fixture／parametrize 的測試 standalone 叫不動），不可以不講。
    # 由 `test_self_runners.test_the_runner_knows_how_many_tests_the_file_declares` 守著。
    try:
        _declared = sum(
            1 for _n in _ast.parse(
                _pathlib.Path(__file__).read_text(encoding="utf-8")).body
            if isinstance(_n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
            and _n.name.startswith("test_"))
        _missing = _declared - len(_TESTS)
        if _missing > 0:
            print(f"注意：另有 {_missing} 支測試 standalone 跑不到"
                  "（需要 pytest fixture／parametrize）；完整結果請跑 pytest。")
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    print(f"ALL {len(_TESTS)} TESTS PASSED")
    return 0


# ===========================================================================
# `load_rpc_config` —— 設定檔是手改的，而且這裡的產物會顯示在擁有者自己的個人檔案上
# ===========================================================================

@pytest.fixture
def rpc_file(tmp_path, monkeypatch):
    path = tmp_path / "presence_rpc.json"
    monkeypatch.setattr(rpc, "RPC_CONFIG_FILE", path)
    return path


def test_a_missing_config_is_disabled_not_broken(rpc_file, capsys):
    """沒設定就停用——`enabled` 預設 False，避免在沒填 client_id 時一直噴錯。"""
    cfg = rpc.load_rpc_config()
    assert cfg["enabled"] is False
    assert cfg["client_id"] == ""
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("payload, stream", [
    ("{ not json", "parse"),
    ("[]", None), ("null", None), ('"text"', None),
])
def test_a_broken_config_falls_back(rpc_file, capsys, payload, stream):
    rpc_file.write_text(payload, encoding="utf-8")
    cfg = rpc.load_rpc_config()
    assert cfg["enabled"] is False
    if stream:
        assert stream in capsys.readouterr().err


def test_a_numeric_client_id_is_accepted_as_a_string(rpc_file):
    """Application ID 是一串數字，使用者很容易在 JSON 裡寫成 number。

    留成 int 的話後面組 handshake 時會拼出 `12345` 而不是 `"12345"`——那是
    **對方**拒絕連線，不是我們這邊報錯，所以查起來完全不像設定問題。
    """
    rpc_file.write_text('{"client_id": 123456789012345678}', encoding="utf-8")
    got = rpc.load_rpc_config()["client_id"]
    assert got == "123456789012345678" and isinstance(got, str)


@pytest.mark.parametrize("bad", [0, -1, "60", None, True, float("nan"),
                                 float("inf"), float("-inf")])
def test_a_bad_refresh_interval_falls_back(rpc_file, bad):
    """0 或負數會讓保活迴圈變成不睡的熱迴圈；`inf` 則是把保活**永久關掉**。

    ⚠️ **`nan` 早就在這份清單裡，而 `inf` 不在——那不是巧合，是這一族的陷阱。**
    `nan` 擋得掉是**碰巧**：所有比較對 `nan` 都是假，所以 `rs > 0` 為假。`inf` 走
    的是同一行檢查卻**通過**（`inf > 0` 為真）。兩個值長得像同一類東西、由同一個
    述詞判定、結果卻相反——列一個而漏另一個的測試會一路綠著（2026-09-10 之前正是
    如此）。這正是「守門是照著例子寫的，不是照著機制寫的」那個坑。
    """
    import json
    rpc_file.write_text(json.dumps({"refresh_sec": bad}), encoding="utf-8")
    cfg = rpc.load_rpc_config()
    assert cfg["refresh_sec"] == rpc._DEFAULT_RPC_CONFIG["refresh_sec"]


@pytest.mark.parametrize("literal", ["1e400", "1e999", "Infinity"])
def test_an_overflowing_literal_never_becomes_an_infinite_interval(rpc_file,
                                                                   literal):
    """**沒有人會手打 `Infinity`，但 `1e400` 看起來完全正常。**

    上面那支餵的是 Python 的 `float("inf")`，經過 `json.dumps` 會變成 `Infinity`
    ——那是使用者不會打出來的形狀。真實的進入點是一個多打幾個零的指數字面值：
    `1e400` 是合法 JSON、`json.loads` 直接給你 `inf`。這一支釘的是那條路。
    """
    rpc_file.write_text('{"refresh_sec": %s}' % literal, encoding="utf-8")
    got = rpc.load_rpc_config()["refresh_sec"]
    assert got == rpc._DEFAULT_RPC_CONFIG["refresh_sec"], (
        f"`{literal}` 被收成 {got!r}——`apply()` 的保活節流是 "
        "`(now - _last_send_ts) < refresh_sec`，吃到 inf 就恆為真，保活從此"
        "再也不會送，而保活的工作正是偵測桌面端已經關掉。")


def test_a_huge_integer_interval_is_rejected_without_raising(rpc_file):
    """JSON 的整數沒有位數上限，而 `float(10**400)` 會丟 `OverflowError`。

    `load_rpc_config` 的合約是「絕不往外拋」——presence 迴圈每個 tick 都重讀它，
    拋出去等於整支背景迴圈結束，而那的外顯症狀是「狀態不再更新」，跟「現在沒事
    發生」分不出來。所以有限性檢查要用**比較**，不能直接呼叫 `math.isfinite()`。
    """
    rpc_file.write_text('{"refresh_sec": %s}' % ("1" + "0" * 400),
                        encoding="utf-8")
    assert rpc.load_rpc_config()["refresh_sec"] == (
        rpc._DEFAULT_RPC_CONFIG["refresh_sec"])


def test_an_infinite_interval_would_switch_the_keepalive_off_for_good(
        setattr_fn):
    """反面對照組：證明上面那幾支擋的東西**真的**有那個後果。

    沒有這一支的話，「`inf` 要被擋掉」只是一句斷言；有了它才知道放行的代價是什麼
    ——而且它同時釘住「正常值下保活真的會再送」，所以節流本身壞掉也會紅。

    用的是本檔既有的 `_client()` 假傳輸（已排好 handshake 的 READY 回應）。
    我第一版自己捏了一個 `client._sock = object()` 的替身，結果 handshake 直接被
    拒（`Invalid Client ID`）、一個封包都沒送出去——**替身要模擬平台，不能照著
    被測程式的心智模型捏**。
    """
    client, transport = _client(setattr_fn)
    transport.queue(_ACCEPTED)
    assert client.apply(_ACTIVITY, "123", refresh_sec=60.0) == "ok"
    after_first = len(transport.sent)

    # 內容沒變、但已經過了兩倍的 refresh_sec —— 保活應該再送一次。
    client._last_send_ts -= 120.0
    transport.queue(_ACCEPTED)
    assert client.apply(_ACTIVITY, "123", refresh_sec=60.0) == "ok", (
        "正常設定下保活沒有再送——節流條件本身壞了")
    after_keepalive = len(transport.sent)
    assert after_keepalive > after_first

    # 同樣的狀態，換成 inf：`(now - last) < inf` 恆為真，永遠不再送。
    client._last_send_ts -= 120.0
    transport.queue(_ACCEPTED)
    assert client.apply(_ACTIVITY, "123", refresh_sec=float("inf")) == (
        "unchanged"), "`inf` 竟然還送得出去——節流條件已經不是原來那一條了"
    assert len(transport.sent) == after_keepalive, (
        "回了 unchanged 卻還是做了 I/O")


def test_a_valid_refresh_interval_is_taken_as_a_float(rpc_file):
    rpc_file.write_text('{"refresh_sec": 15}', encoding="utf-8")
    assert rpc.load_rpc_config()["refresh_sec"] == 15.0


def test_a_large_but_finite_interval_is_still_accepted(rpc_file):
    """誤殺的方向也要釘。

    只用小數字（15）測「合法值會被收下」的話，把有限性上界從 1.8e308 調成 100
    也不會有任何測試變紅——變異實測存活過一次。一個把合法值擋掉的守門，症狀是
    「設定沒生效」，跟「設定寫錯了」分不出來。
    """
    rpc_file.write_text('{"refresh_sec": 1e308}', encoding="utf-8")
    assert rpc.load_rpc_config()["refresh_sec"] == 1e308


@pytest.mark.parametrize("value, want, why", [
    (60, True, "一般整數"),
    (60.5, True, "一般浮點數"),
    (0, True, "零是有限的（是不是 > 0 由呼叫端判斷，不是這支的事）"),
    (-60, True, "負數也是有限的"),
    (1.7976931348623157e+308, True, "float 的最大值，仍然有限"),
    (float("inf"), False, "上界擋掉"),
    (float("-inf"), False, "下界擋掉"),
    (float("nan"), False, "nan 的所有比較都是假，所以過不了左邊那一半"),
    (True, False, "bool 是 int 的子類，會一路變成 1"),
    (False, False, "同上"),
    (10 ** 400, False, "轉不成 float 的超大 int"),
    (-(10 ** 400), False, "同上，負的"),
    ("60", False, "字串"),
    (None, False, "缺值"),
])
def test_the_finiteness_predicate_itself(value, want, why):
    """直接釘述詞，不要只經過 `load_rpc_config`。

    只透過載入器測的話，`nan` 是被**後面那句** `rs > 0` 擋掉的，述詞裡負責它的
    那一半即使拿掉也全綠（變異實測存活過一次）。兩層各自的責任要分開釘，否則
    「哪一句在做事」永遠問不出來——而下一個人會照著那個誤解去改。
    """
    assert rpc._is_finite_number(value) is want, why


def test_the_returned_config_shares_no_mutable_state_with_the_defaults(
        rpc_file):
    """**這一支抓到的是實際的缺陷。**

    退路原本是 `dict(_DEFAULT_RPC_CONFIG["claude"])`——只複製外層，裡面的
    `process_names` 仍與模組常數是**同一個 list**。呼叫端 append 一次，預設值就被
    永久污染，而這支每個 probe tick 都會被叫一次，所以髒的會一路傳下去。
    `_bot_config._fallback_bot_config` 為了完全相同的理由早就改用 `deepcopy`
    （那次是 `user_roles` 的三份 id 清單）。
    """
    cfg = rpc.load_rpc_config()
    cfg["claude"]["process_names"].append("POISON")
    cfg["kinds"]["playing"]["name"] = "MUTATED"
    assert rpc._DEFAULT_RPC_CONFIG["claude"]["process_names"] == ["claude.exe"]
    assert rpc._DEFAULT_KIND_CONFIG["playing"]["name"] == "{name}"
    assert rpc.load_rpc_config()["claude"]["process_names"] == ["claude.exe"]


def test_two_loads_do_not_share_mutable_state_with_each_other(rpc_file):
    a, b = rpc.load_rpc_config(), rpc.load_rpc_config()
    a["claude"]["process_names"].append("x")
    assert b["claude"]["process_names"] == ["claude.exe"]


def test_an_empty_process_name_list_keeps_the_default(rpc_file):
    """空清單代表「什麼都不比對」＝ Claude 偵測靜默失效，那不會是使用者的意思。"""
    rpc_file.write_text('{"claude": {"process_names": []}}', encoding="utf-8")
    assert rpc.load_rpc_config()["claude"]["process_names"] == ["claude.exe"]


def test_process_names_drop_only_the_non_strings(rpc_file):
    rpc_file.write_text('{"claude": {"process_names": ["a.exe", 1, null]}}',
                        encoding="utf-8")
    assert rpc.load_rpc_config()["claude"]["process_names"] == ["a.exe"]


def test_a_single_process_name_written_as_a_string_keeps_the_default(rpc_file):
    """少打一對中括號很常見。字串照樣可以迭代，不擋的話會被拆成一個字一個名稱
    （`["c", "o", "d", …]`），什麼行程都比對不到——Claude 偵測安靜失效。"""
    rpc_file.write_text('{"claude": {"process_names": "code.exe"}}', encoding="utf-8")
    assert rpc.load_rpc_config()["claude"]["process_names"] == ["claude.exe"]


def test_the_file_is_re_read_on_every_call(rpc_file):
    """沒有快取是刻意的——改了設定下一個 probe tick 就生效，不必重啟。"""
    rpc_file.write_text('{"enabled": true}', encoding="utf-8")
    assert rpc.load_rpc_config()["enabled"] is True
    rpc_file.write_text('{"enabled": false}', encoding="utf-8")
    assert rpc.load_rpc_config()["enabled"] is False


# --- `_merge_kind` ----------------------------------------------------------

def test_a_kind_override_only_replaces_what_it_names(rpc_file):
    out = rpc._merge_kind({"name": "N"}, rpc._DEFAULT_KIND_CONFIG["playing"])
    assert out["name"] == "N"
    assert out["type"] == rpc._DEFAULT_KIND_CONFIG["playing"]["type"]
    assert out["show_timestamp"] is True


@pytest.mark.parametrize("raw", [None, "x", 1, [], {"name": 1},
                                 {"show_timestamp": "yes"}, {"type": True}])
def test_a_wrongly_typed_kind_override_is_ignored(raw):
    """半套用比整個忽略糟：使用者會以為設定生效了。

    `{"type": True}` 那一筆是重點——`bool` 是 `int` 的子類別，少了
    `isinstance(x, bool)` 這道，`true` 會變成 activity type 1（Streaming）。
    """
    default = rpc._DEFAULT_KIND_CONFIG["playing"]
    assert rpc._merge_kind(raw, default) == dict(default)


# ===========================================================================
# `build_activity` —— 送到擁有者個人檔案上的那份 payload
#
# 這支的產物是**別人看得到的東西**，而它的錯誤方式有兩種都很安靜：欄位空掉
# （Discord 拒收整份，狀態就是不更新）與欄位過長（同樣被拒）。
# ===========================================================================

def test_no_probe_means_no_activity():
    """回 None 才會讓呼叫端清掉狀態；回一個空 dict 會卡在最後一個狀態。"""
    cfg = rpc.load_rpc_config()
    for probe in (None, {}, False):
        assert rpc.build_activity(probe, cfg, 0) is None


def test_an_unknown_kind_yields_no_activity():
    """設定裡沒有這個 kind 就不要硬送——送出去只會被對方拒絕。"""
    cfg = rpc.load_rpc_config()
    assert rpc.build_activity({"kind": "dancing", "name": "X"}, cfg, 0) is None


def test_the_name_placeholder_is_filled_in():
    cfg = rpc.load_rpc_config()
    act = rpc.build_activity({"kind": "playing", "name": "Foo"}, cfg, 0)
    assert act["name"] == "Foo"


def test_the_listening_kind_carries_its_activity_type():
    """0=Playing、2=Listening、3=Watching。型別錯了顯示的動詞就錯了。"""
    cfg = rpc.load_rpc_config()
    assert rpc.build_activity({"kind": "listening", "name": "S"},
                              cfg, 0)["type"] == 2
    assert rpc.build_activity({"kind": "playing", "name": "G"},
                              cfg, 0)["type"] == 0


def test_a_timestamp_is_only_sent_when_the_kind_asks_for_it():
    cfg = rpc.load_rpc_config()
    assert "timestamps" in rpc.build_activity(
        {"kind": "playing", "name": "G"}, cfg, 1700000000000)
    assert "timestamps" not in rpc.build_activity(
        {"kind": "listening", "name": "S"}, cfg, 1700000000000)


def test_empty_fields_are_omitted_not_sent_as_empty_strings():
    """Discord 不接受空字串欄位——送出去整份會被拒，狀態就停在原地不動。"""
    cfg = rpc.load_rpc_config()
    act = rpc.build_activity({"kind": "playing", "name": "G"}, cfg, 0)
    assert "details" not in act and "state" not in act
    assert "assets" not in act, "沒有圖片就不要送空的 assets"
    assert all(v != "" for v in act.values())


def test_an_activity_always_has_something_displayable(rpc_file):
    """name / details / state 全空時要用偵測名補上，不能送一個空殼。"""
    rpc_file.write_text(
        '{"kinds": {"playing": {"name": "", "details": "", "state": ""}}}',
        encoding="utf-8")
    cfg = rpc.load_rpc_config()
    act = rpc.build_activity({"kind": "playing", "name": "Foo"}, cfg, 0)
    assert act["name"] == "Foo"


def test_a_nameless_probe_still_produces_something(rpc_file):
    rpc_file.write_text(
        '{"kinds": {"playing": {"name": "", "details": "", "state": ""}}}',
        encoding="utf-8")
    cfg = rpc.load_rpc_config()
    act = rpc.build_activity({"kind": "playing", "name": ""}, cfg, 0)
    assert act["name"] == "Active"


def test_a_broken_placeholder_is_left_as_written(rpc_file):
    """設定裡打錯的佔位符（`{nmae}`）不能讓整輪炸掉。

    `str.format` 對未知的鍵丟 `KeyError`，而這支跑在 presence 迴圈裡——拋出去
    等於狀態從此停止更新。原樣送出至少看得出來是哪裡打錯了。
    """
    rpc_file.write_text('{"kinds": {"playing": {"details": "{nmae}"}}}',
                        encoding="utf-8")
    cfg = rpc.load_rpc_config()
    act = rpc.build_activity({"kind": "playing", "name": "Foo"}, cfg, 0)
    assert act["details"] == "{nmae}"


@pytest.mark.parametrize("field, key", [
    ("name", "name"), ("details", "details"), ("state", "state"),
])
def test_every_text_field_is_capped(rpc_file, field, key):
    """超過上限的欄位會被對方拒收——而拒收的是**整份** activity。"""
    import json
    rpc_file.write_text(
        json.dumps({"kinds": {"playing": {field: "{name}"}}}),
        encoding="utf-8")
    cfg = rpc.load_rpc_config()
    act = rpc.build_activity({"kind": "playing", "name": "x" * 500}, cfg, 0)
    assert len(act[key]) == rpc._MAX_LEN


def test_asset_text_is_capped_too(rpc_file):
    import json
    rpc_file.write_text(
        json.dumps({"kinds": {"playing": {"large_image": "img",
                                          "large_text": "{name}"}}}),
        encoding="utf-8")
    cfg = rpc.load_rpc_config()
    act = rpc.build_activity({"kind": "playing", "name": "y" * 500}, cfg, 0)
    assert len(act["assets"]["large_text"]) == rpc._MAX_LEN


def test_assets_appear_only_when_an_image_key_is_configured(rpc_file):
    import json
    rpc_file.write_text(
        json.dumps({"kinds": {"playing": {"large_image": "cover"}}}),
        encoding="utf-8")
    cfg = rpc.load_rpc_config()
    act = rpc.build_activity({"kind": "playing", "name": "G"}, cfg, 0)
    assert act["assets"] == {"large_image": "cover"}


# ===========================================================================
# 失敗與重連 —— 這一半的程式碼「第一次執行是在出事的時候」
# ===========================================================================
#
# 判準不是行數，是**「這段程式碼第一次執行是什麼時候」**。上面那批測的是順風
# 路徑；下面這批測的是連不上、pipe 中途死掉、讀到半截 frame、握手被拒、斷線
# 重連——也就是唯一擋在「presence 靜靜停住、沒有任何徵兆」前面的那些程式碼。
#
# 這條 IPC 特別容易壞：它接的是 Discord **桌面程式**開的本機 pipe，而使用者
# 隨時會關掉／重開／更新它。整個保活設計（內容沒變也要每隔 refresh_sec 重送
# 一次）存在的唯一理由就是偵測這件事——而那正是原本一行都沒被執行過的地方。
#
# 所有 I/O 都走假的 transport：不連 Discord、不開真的 named pipe、不起子行程。

_PING_FRAME = _frame(rpc._OP_PING, {"seq": 1})
_CLOSE_INVALID_ID = _frame(rpc._OP_CLOSE,
                           {"code": 4000, "message": "Invalid Client ID"})
_CLOSE_BYE = _frame(rpc._OP_CLOSE, {"code": 1000, "message": "bye"})
_OTHER_ACTIVITY = {"name": "Y", "type": 2}


class _ScriptedTransport:
    """可注入失敗的假 transport。

    `read` 有硬性次數上限。`_read_exact` 的迴圈只靠「讀到空 bytes 就丟
    OSError」收斂，把那個條件弄壞的改動會讓它永遠讀下去——而**掛住的測試比紅
    的測試難查**，所以這裡把無限迴圈換成一個講得出原因的失敗。

    `largest_read` 記下被要求過的最大讀取量。這是「長度欄位不合理」那條路唯一
    測得到的東西：照單全收也會回 `send-failed`（讀不滿就 EOF），差別只在有沒有
    真的去要那 4 GiB。
    """

    _MAX_READS = 200

    def __init__(self, *frames, write_error=None, write_error_after=None):
        self.path = r"\\.\pipe\fake-ipc"
        self._inbox = b"".join(frames)
        self.sent: list[bytes] = []
        self.closed = False
        self.reads = 0
        self.largest_read = 0
        self._write_error = write_error
        self._write_error_after = write_error_after

    def queue(self, *frames: bytes) -> None:
        self._inbox += b"".join(frames)

    def write(self, data: bytes) -> None:
        if self._write_error is not None and (
                self._write_error_after is None
                or len(self.sent) >= self._write_error_after):
            raise self._write_error
        self.sent.append(bytes(data))

    def read(self, n: int) -> bytes:
        self.reads += 1
        self.largest_read = max(self.largest_read, n)
        if self.reads > self._MAX_READS:
            raise AssertionError(
                f"讀了 {self.reads} 次還沒收斂——`_read_exact` 的 EOF 出口壞了。"
                "（把無限迴圈換成失敗，不然這支測試會掛住而不是變紅。）")
        chunk, self._inbox = self._inbox[:n], self._inbox[n:]
        return chunk

    def close(self) -> None:
        self.closed = True


class _ExplodingTransport:
    """碰到就算失敗。用來證明「完全沒有 I/O」而不必去斷言內部計數器——
    數送出筆數只能證明「沒多送」，證不了「連讀都沒讀」。"""

    path = r"\\.\pipe\must-not-be-touched"

    def _boom(self, *_args, **_kwargs):
        raise AssertionError(
            "這一輪不該碰到 transport：內容沒變且還在 refresh 視窗內，"
            "保活節流應該直接回 unchanged。")

    write = read = close = _boom


def _client_over(monkeypatch, *transports):
    """把 `_open_transport` 換成「依序交出這幾個 transport」。

    用完之後再要就回 `None`（＝桌面程式沒開），所以「多連了一次」不會安靜地
    成功，而是變成一個看得見的 `not-connected`。"""
    pending = list(transports)
    monkeypatch.setattr(rpc, "_open_transport",
                        lambda: pending.pop(0) if pending else None)
    return rpc.RichPresenceClient()


# --- 1. 連不上 --------------------------------------------------------------

def test_a_missing_desktop_app_is_reported_not_raised(monkeypatch):
    """桌面程式沒開是**常態**，不是例外。丟出去的話會從 `asyncio.to_thread`
    一路冒到 probe loop 的 broad except，每個 tick 一次。"""
    monkeypatch.setattr(rpc, "_open_transport", lambda: None)
    client = rpc.RichPresenceClient()
    assert client.apply(_ACTIVITY, "123", 60.0) == "not-connected"


def test_a_failed_connect_leaves_no_phantom_connection(monkeypatch):
    """回 `not-connected` 卻把 `connected` 留成 True 的話，下一個 tick 就不會
    再試著連線——presence 從此再也回不來，而狀態指令顯示一切正常。"""
    monkeypatch.setattr(rpc, "_open_transport", lambda: None)
    client = rpc.RichPresenceClient()
    client.apply(_ACTIVITY, "123", 60.0)
    status = client.status()
    assert status["connected"] is False
    assert status["pipe"] == "", "連都沒連上卻報得出一條 pipe"


def test_a_repeating_failure_is_logged_once_not_on_every_tick(
        monkeypatch, capsys):
    """桌面程式沒開是**穩態**，不是一次性事件——probe 每 8 秒一個 tick，照實印
    的話一小時 450 行，而且每一行都一樣。所以同一個錯誤連續發生時只印第一次。

    注意這個去重比的是「上一次記到的錯誤字串」，不是「上一個 tick」。中間夾了
    一次成功、之後同一個錯誤再犯時就不會再印——那是一個已知的觀測缺口，記在
    已知落差（`_last_error` 在成功之後沒有被清掉）。"""
    monkeypatch.setattr(rpc, "_open_transport", lambda: None)
    client = rpc.RichPresenceClient()
    for _ in range(5):
        assert client.apply(_ACTIVITY, "123", 60.0) == "not-connected"
    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(lines) == 1, f"同一個錯誤印了 {len(lines)} 次，log 會被洗掉"


def test_the_desktop_app_coming_back_is_picked_up(monkeypatch):
    """沒開 → 開起來。第二個 tick 要自己連上，不該需要重啟 bot。"""
    transport = _ScriptedTransport(_READY, _ACCEPTED)
    attempts = []

    def _open():
        attempts.append(1)
        return transport if len(attempts) > 1 else None

    monkeypatch.setattr(rpc, "_open_transport", _open)
    client = rpc.RichPresenceClient()
    assert client.apply(_ACTIVITY, "123", 60.0) == "not-connected"
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"


# --- 2. 送出失敗，以及「壞掉之後回得來嗎」-----------------------------------

def test_a_dead_pipe_mid_session_is_reported_and_disconnected(monkeypatch):
    """使用者把桌面程式關掉：握手時還在，送 activity 時已經不在了。"""
    dead = _ScriptedTransport(_READY, write_error=OSError("pipe gone"),
                              write_error_after=1)
    client = _client_over(monkeypatch, dead)
    assert client.apply(_ACTIVITY, "123", 60.0) == "send-failed"
    assert client.status()["connected"] is False
    assert dead.closed is True, "送出失敗要把 transport 關掉，否則 fd 會累積"


def test_a_send_failure_recovers_on_the_next_tick(monkeypatch):
    """**這一支是重點。** 只證明「回了 `send-failed`」不夠——那只說明它偵測到
    了，沒說明它回得來。桌面程式重開之後 presence 必須自己接回去，否則使用者
    唯一的辦法是重啟 bot，而他根本不會知道要重啟。"""
    dead = _ScriptedTransport(_READY, write_error=OSError("pipe gone"),
                              write_error_after=1)
    fresh = _ScriptedTransport(_READY, _ACCEPTED)
    client = _client_over(monkeypatch, dead, fresh)
    assert client.apply(_ACTIVITY, "123", 60.0) == "send-failed"
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok", "壞掉之後回不來"
    assert client.status()["connected"] is True


def test_the_dedup_cache_does_not_survive_a_reconnect(monkeypatch):
    """斷線重連之後去重快取必須清掉。

    留著上一條連線的 payload key 的話，重連後第一個 tick 會被判成 `unchanged`
    ——連線是新的、上面什麼都沒設過，presence 卻整個 refresh 視窗都是空的。
    而且回傳值是「健康」的，所以記錄那一側也不會出聲。

    **這支只看得到「兩行一起不見」，看不到單獨拿掉其中一行——那不是漏洞。**
    `_connect` 用兩行各自保證同一件事（`_last_send_ts = float("-inf")` 讓時間
    條件永遠不成立，`_last_payload_key = None` 讓 key 條件永遠不成立），而節流
    是那兩個條件的 `and`，所以任一行單獨留著都足夠。變異測試實測過：兩個單行變
    異都存活、兩行一起拿掉才被抓到。原始碼那份冗餘是刻意的（註解寫著 `0.0` 曾
    經在剛開機時失效過），所以要的是別把它「清理」掉，而不是再寫一支測試去綁
    某一行的實作細節。"""
    first = _ScriptedTransport(_READY, _ACCEPTED,
                               write_error=OSError("pipe gone"),
                               write_error_after=2)
    second = _ScriptedTransport(_READY, _ACCEPTED)
    client = _client_over(monkeypatch, first, second)
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"
    assert client.apply(_ACTIVITY, "123", refresh_sec=0.0) == "send-failed"
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok", (
        "重連後同一個 activity 被當成「沒變」——新連線上其實一片空白")
    assert len(second.sent) == 2, "新連線要重新握手，然後真的把 activity 送出去"


def test_a_reconnect_loop_does_not_leak_transports(monkeypatch):
    """連續失敗時，每一輪拿到的 transport 都要被關掉。probe 每 8 秒一個 tick，
    漏掉的話一小時就是 450 個沒關的 handle。"""
    made = []

    def _open():
        t = _ScriptedTransport(_READY, write_error=OSError("gone"),
                               write_error_after=1)
        made.append(t)
        return t

    monkeypatch.setattr(rpc, "_open_transport", _open)
    client = rpc.RichPresenceClient()
    for _ in range(5):
        assert client.apply(_ACTIVITY, "123", 60.0) == "send-failed"
    assert len(made) == 5, "每一輪都該重新連一次"
    assert all(t.closed for t in made), "有 transport 沒被關掉"


# --- 3. `_recv` 讀到半截 / EOF / 長度欄位不合理 ------------------------------

@pytest.mark.parametrize("tail, why", [
    (b"", "回應整個沒來（對端直接 EOF）"),
    (b"\x01\x00\x00", "8 bytes 的 header 只讀到 3 bytes"),
    (struct.pack("<II", 1, 40) + b"{}", "header 說有 40 bytes，body 只有 2"),
    (struct.pack("<II", 1, (1 << 20) + 1), "長度欄位剛好越過上限"),
])
def test_a_truncated_frame_never_escapes_apply(monkeypatch, tail, why):
    """`_recv` 內部丟 `OSError` 是對的，但**不可以逸出 `apply()`**——那會從
    worker thread 一路冒到 probe loop。也不得無限等待。"""
    transport = _ScriptedTransport(_READY, tail)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "send-failed", why
    assert client.status()["connected"] is False, "讀壞了卻沒斷線 → 不會重連"


def test_an_absurd_frame_length_is_refused_before_it_is_allocated(monkeypatch):
    """長度欄位是 4 GiB 時，重點**不是**「回了 send-failed」——照單全收也會回
    send-failed（讀不滿就 EOF），兩者從回傳值上分不出來。

    重點是有沒有真的**去要**那麼多 bytes：`_read_exact` 會把讀到的東西累進一個
    `bytearray`，等於在 worker thread 裡試著配置 4 GiB。所以這裡斷言的是「要求
    的讀取量沒有超過上限」。"""
    transport = _ScriptedTransport(_READY, struct.pack("<II", 1, 0xFFFFFFFF))
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "send-failed"
    assert transport.largest_read <= rpc._MAX_FRAME, (
        f"要了 {transport.largest_read} bytes；長度上限檢查沒有生效")


def test_a_garbage_payload_does_not_take_the_connection_down(monkeypatch):
    """frame 長度是對的、body 不是合法 JSON。連線本身沒問題（框架對得上），
    所以不該斷線——斷線會變成「一則壞回應害我們重連一輪」。"""
    junk = b"\xff\xfe not json at all"
    transport = _ScriptedTransport(_READY, struct.pack("<II", 1, len(junk)) + junk)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) in _VOCABULARY
    assert client.status()["connected"] is True


def test_a_non_object_payload_does_not_raise(monkeypatch):
    """合法 JSON 但不是物件（`[1,2,3]`）。後面會對它 `.get("evt")`，不先收斂成
    dict 的話是 AttributeError。"""
    arr = b"[1, 2, 3]"
    transport = _ScriptedTransport(_READY, struct.pack("<II", 1, len(arr)) + arr)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) in _VOCABULARY


# --- 4. 握手 ----------------------------------------------------------------

def test_a_rejected_handshake_does_not_look_like_a_live_connection(monkeypatch):
    """`client_id` 無效時桌面端回 CLOSE。

    注意這條路回的是 `not-connected`，**不是** `rejected`——後者保留給「連線好
    好的，但這一張 activity 被退回來」。兩者的處置完全相反（一個要重連、一個
    刻意不斷線），混在同一個字裡呼叫端就分不出來了。"""
    transport = _ScriptedTransport(_CLOSE_INVALID_ID)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "bogus", 60.0) == "not-connected"
    assert client.status()["connected"] is False
    assert transport.closed is True


def test_a_rejected_handshake_is_retried_not_latched(monkeypatch):
    """設定檔改對之後不必重啟 bot——`client_id` 是每個 tick 重讀的。"""
    bad = _ScriptedTransport(_CLOSE_INVALID_ID)
    good = _ScriptedTransport(_READY, _ACCEPTED)
    client = _client_over(monkeypatch, bad, good)
    assert client.apply(_ACTIVITY, "bogus", 60.0) == "not-connected"
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"


def test_a_handshake_that_cannot_even_be_written_is_not_a_connection(monkeypatch):
    """pipe 開得起來但寫不進去（對端正在收攤）。"""
    transport = _ScriptedTransport(write_error=OSError("broken pipe"))
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "not-connected"
    assert client.status()["connected"] is False
    assert transport.closed is True


def test_a_close_after_send_is_not_reported_as_success(monkeypatch):
    """對方收下 frame 之後把連線收掉。

    不斷線的話 `connected` 會停在 True，接下來每一次都以為送成功（還把 payload
    key 記進去重快取），presence 實際上沒設起來卻無聲無息。"""
    transport = _ScriptedTransport(_READY, _CLOSE_BYE)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "send-failed"
    assert client.status()["connected"] is False


# --- 5. client_id 換掉 ------------------------------------------------------

def test_a_changed_client_id_reconnects_to_the_new_one(monkeypatch):
    """`client_id` 綁的是 Developer Portal 上的那個 application；換了之後舊連
    線上的一切（含已上傳的圖片 asset key）都不再適用，必須整條重開。"""
    first = _ScriptedTransport(_READY, _ACCEPTED)
    second = _ScriptedTransport(_READY, _ACCEPTED)
    client = _client_over(monkeypatch, first, second)
    assert client.apply(_ACTIVITY, "111", 60.0) == "ok"
    assert client.apply(_ACTIVITY, "222", 60.0) == "ok"
    assert first.closed is True, "換 client_id 要把舊連線關掉"
    assert client.status()["client_id"] == "222"
    assert len(second.sent) == 2, "新連線要重新握手再送 activity"


def test_an_unchanged_client_id_does_not_reconnect_every_tick(monkeypatch):
    """`_client_id` 存的是字串。比較時若不先 `str()`，設定檔把 id 寫成數字的人
    每個 tick 都會被判定「換了 id」→ 每 8 秒重連一次，而症狀只是「有點慢」。

    這裡只餵一個 transport，所以任何一次多餘的重連都會拿到 `None` →
    `not-connected`，藏不住。"""
    only = _ScriptedTransport(_READY, _ACCEPTED, _ACCEPTED)
    client = _client_over(monkeypatch, only)
    assert client.apply(_ACTIVITY, 12345, 60.0) == "ok"
    assert client.apply(_OTHER_ACTIVITY, 12345, 60.0) == "ok"


# --- 6. 保活（唯一會發現「桌面程式被關掉」的動作）---------------------------

def test_the_keepalive_window_does_no_io_at_all(monkeypatch):
    """內容沒變且還在 refresh 視窗內 → 直接回 `unchanged`。

    這裡不去數送出筆數（那只證明得了「沒多送」），改成把 transport 換成「碰到
    就爆炸」的假貨：只要有任何一次 read / write / close 就失敗。斷言只看回傳
    值，跟真的有 I/O 的那條路完全分不出來——分得出來的是那個爆炸。"""
    transport = _ScriptedTransport(_READY, _ACCEPTED)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"
    client._t = _ExplodingTransport()
    assert client.apply(_ACTIVITY, "123", 60.0) == "unchanged"


def test_the_keepalive_resends_once_the_window_has_passed(monkeypatch):
    """視窗過了就重送一次。這不是多餘的流量——見下一支。"""
    transport = _ScriptedTransport(_READY, _ACCEPTED, _ACCEPTED)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"
    before = len(transport.sent)
    assert client.apply(_ACTIVITY, "123", refresh_sec=0.0) == "ok"
    assert len(transport.sent) == before + 1


def test_the_keepalive_is_what_notices_a_closed_desktop_app(monkeypatch):
    """保活重送存在的**唯一**理由：內容一直沒變的時候（穩態就是這樣），若永遠
    不重送，桌面程式被關掉這件事就永遠不會被發現——沒有人會來通知我們。"""
    transport = _ScriptedTransport(_READY, _ACCEPTED,
                                   write_error=OSError("pipe gone"),
                                   write_error_after=2)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"
    assert client.apply(_ACTIVITY, "123", refresh_sec=0.0) == "send-failed"
    assert client.status()["connected"] is False


def test_the_keepalive_clock_is_immune_to_a_wall_clock_jump(monkeypatch):
    """節流基準是單調時鐘，不是牆上時鐘。

    牆上時鐘被 NTP／手動往回撥時，`now - _last_send_ts` 會變負數，於是**永遠**
    判定「還沒到 refresh_sec」——presence 就卡在最後一次的內容，直到時鐘追回
    來。這支把兩個時鐘反向撥開，讓兩種寫法分得出來。"""
    transport = _ScriptedTransport(_READY, _ACCEPTED, _ACCEPTED)
    client = _client_over(monkeypatch, transport)
    fake_monotonic = [1_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_monotonic[0])
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"
    # 牆上時鐘被撥回 1970；單調時鐘照常前進 100 秒。
    monkeypatch.setattr(time, "time", lambda: 0.0)
    fake_monotonic[0] += 100.0
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok", (
        "保活重送沒有發生——節流若是看牆上時鐘，時鐘往回撥之後就永遠不會到期")


# --- 7. 停用 ----------------------------------------------------------------

def test_an_empty_client_id_disables_and_clears_the_card(monkeypatch):
    """設定被關掉時要把殘留的 presence 收掉。

    IPC 的活動卡片是綁在**連線**上的，所以把連線關掉就等於清掉那張卡；留著連
    線的話卡片會一直掛在使用者的個人檔案上，而 bot 已經不再更新它了。"""
    transport = _ScriptedTransport(_READY, _ACCEPTED)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"
    assert client.apply(None, "", 60.0) == "disabled"
    assert transport.closed is True, "停用了卻沒把連線收掉 → 卡片留在檔案上"
    assert client.status()["connected"] is False


def test_a_disabled_tick_never_opens_a_connection(monkeypatch):
    """從來沒啟用過的情況下，每個 tick 都會走到這裡。為了回一句 `disabled` 去
    開一條 IPC 連線是純粹的浪費。"""
    def _must_not_open():
        raise AssertionError("停用中不該去開 IPC 連線")

    monkeypatch.setattr(rpc, "_open_transport", _must_not_open)
    client = rpc.RichPresenceClient()
    assert client.apply(None, "", 60.0) == "disabled"


def test_re_enabling_after_a_disable_reconnects(monkeypatch):
    """關掉 → 再打開。presence 要自己回來。"""
    first = _ScriptedTransport(_READY, _ACCEPTED)
    second = _ScriptedTransport(_READY, _ACCEPTED)
    client = _client_over(monkeypatch, first, second)
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"
    assert client.apply(None, "", 60.0) == "disabled"
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"
    assert client.status()["connected"] is True


# --- 8. 回傳值是固定詞彙 ----------------------------------------------------

def test_apply_only_ever_returns_the_agreed_vocabulary():
    """`apply()` 的回傳值有兩個呼叫端在依賴**字面值**：狀態指令原樣貼進聊天訊
    息，記錄那一側用 `_rpc_health_class` 把它分成健康／不健康。多一個沒被分類
    的值會靜默落進「不健康」，於是每個 tick 印一行——那正是那層分類要消滅的。

    這裡掃 AST 不掃字串：這幾個詞在同一個檔案的註解與 docstring 裡都出現過，
    子字串比對會被註解餵飽而永遠是綠的。"""
    tree = _ast.parse(
        _pathlib.Path(rpc.__file__).read_text(encoding="utf-8"), rpc.__file__)
    apply_fn = None
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ClassDef) and node.name == "RichPresenceClient":
            for sub in node.body:
                if isinstance(sub, _ast.FunctionDef) and sub.name == "apply":
                    apply_fn = sub
    assert apply_fn is not None, "找不到 RichPresenceClient.apply"

    returned = set()
    for node in _ast.walk(apply_fn):
        if not isinstance(node, _ast.Return):
            continue
        value = node.value
        assert (isinstance(value, _ast.Constant)
                and isinstance(value.value, str)), (
            f"{rpc.__file__}:{node.lineno} 的 return 不是字串字面值。"
            "呼叫端會把回傳值原樣貼進聊天訊息，動態組出來的值可能夾帶失敗細節；"
            "而分類那一側是照字面值比對的。")
        returned.add(value.value)
    assert returned == _VOCABULARY, (
        f"apply() 回得出 {sorted(returned)}，約定的是 {sorted(_VOCABULARY)}。"
        "動這組字的話，`discord_bot._RPC_HEALTHY_RESULTS` 與 `_rpc_health_class` "
        "要一起改，否則新的值會被靜默當成異常。")


# --- 保密規則 Layer 1：失敗細節不得夾在回傳值裡 -----------------------------

# 一個把所有禁忌都塞進來的例外訊息：主機路徑、pipe 路徑、專案相對路徑、外部服務
# 名。真實世界的 `OSError` 訊息確實會帶 pipe 路徑，而回傳值會被貼進聊天訊息。
_POISON = (
    r"C:\Users\Example\AppData\Local\Discord\app-1.0\resources",
    r"\\.\pipe\discord-ipc-0",
    "output/columbina (genshin impact)/x.png",
    "NovelAI",
    "webrunner",
)
_POISONED = " ".join(_POISON)

# 細節是用 `{error!r}` 記下去的，而 `repr()` 會把反斜線加倍。所以「細節有沒有被
# 留下來」只能拿**經過 repr 之後長得一樣**的片段來問——拿含反斜線的去問會得到一
# 個跟「細節被丟掉了」分不出來的失敗。
_POISON_REPR_STABLE = tuple(f for f in _POISON if repr(f)[1:-1] == f)


# 每個 builder 回 `(transport, 預期回傳值, 細節會不會被留在 last_error)`。
# 最後那個旗標不是為了讓測試好過——「送出後被對方關閉」那條路刻意只記一句固定
# 的訊息、完全不看對端的 payload，那是這幾條路裡唯一這樣做的。把它跟其他四條
# 混成同一個斷言，等於用一個永遠成立的條件去問一件根本不同的事。

def _poisoned_handshake_write():
    return _ScriptedTransport(write_error=OSError(_POISONED)), "not-connected", True


def _poisoned_handshake_close():
    return (_ScriptedTransport(_frame(rpc._OP_CLOSE,
                                      {"code": 4000, "message": _POISONED})),
            "not-connected", True)


def _poisoned_send():
    return (_ScriptedTransport(_READY, write_error=OSError(_POISONED),
                               write_error_after=1),
            "send-failed", True)


def _poisoned_close_after_send():
    # 對端在收下 frame 之後才關線。這裡記的是固定訊息，對端的 payload 不進
    # `last_error`——所以只驗「沒漏」，不驗「有留」。
    return (_ScriptedTransport(_READY,
                               _frame(rpc._OP_CLOSE, {"message": _POISONED})),
            "send-failed", False)


def _poisoned_rejection():
    return (_ScriptedTransport(_READY,
                               _frame(1, {"evt": "ERROR",
                                          "data": {"code": 4000,
                                                   "message": _POISONED}})),
            "rejected", True)


@pytest.mark.parametrize("build", [
    _poisoned_handshake_write,
    _poisoned_handshake_close,
    _poisoned_send,
    _poisoned_close_after_send,
    _poisoned_rejection,
], ids=lambda fn: fn.__name__)
def test_no_failure_path_leaks_its_detail_into_the_return_value(
        monkeypatch, capsys, build):
    """**保密規則 Layer 1。** `apply()` 的回傳值會被呼叫端原樣貼進聊天訊息，所
    以主機路徑、pipe 路徑、原始例外文字、外部服務名一律不得串進去——這條寫在
    `apply` 的 docstring 裡，但 docstring 攔不住任何人。

    細節該去的地方是 stderr 與 `status()["last_error"]`（呼叫端已知那兩欄只能
    進 log）。所以這裡同時驗兩件事：**沒漏出去**，而且**沒被丟掉**——只把細節
    刪光也會讓「沒漏」那半變綠，那是修錯方向。"""
    transport, expected, keeps_detail = build()
    client = _client_over(monkeypatch, transport)
    result = client.apply(_ACTIVITY, "123", 60.0)

    assert result == expected
    assert result in _VOCABULARY, f"{result!r} 不在約定的詞彙裡"
    for fragment in _POISON:
        assert fragment not in result, f"回傳值夾帶了 {fragment!r}"
        # `repr()` 過的形式（反斜線加倍）也算漏，那才是實際會被串進去的樣子。
        assert fragment.replace("\\", "\\\\") not in result

    assert transport.path not in result, "回傳值夾帶了 pipe 路徑"

    detail = client.status()["last_error"]
    stderr = capsys.readouterr().err
    assert detail, "連一句診斷都沒記 → 出事時 log 上什麼都沒有"
    assert stderr, "失敗完全沒寫進 stderr"
    if keeps_detail:
        assert _POISON_REPR_STABLE, "測資裡至少要有一個 repr 之後不變形的片段"
        for fragment in _POISON_REPR_STABLE:
            assert fragment in detail, (
                "細節連 last_error 都沒留下 → 出事時查不到原因")
            assert fragment in stderr, "細節沒有寫進 stderr"


def test_the_pipe_path_is_diagnostic_only_and_never_a_result(monkeypatch):
    """`status()` 會報 pipe 路徑（給 log 用）。那是主機路徑，所以它出現在
    `status()` 是對的、出現在 `apply()` 的回傳值就是外洩。"""
    transport = _ScriptedTransport(_READY, _ACCEPTED)
    client = _client_over(monkeypatch, transport)
    result = client.apply(_ACTIVITY, "123", 60.0)
    assert client.status()["pipe"] == transport.path
    assert transport.path not in result


# --- PING / PONG ------------------------------------------------------------

def test_a_ping_is_answered_and_the_real_reply_is_still_read(monkeypatch):
    """桌面端可能在我們等 SET_ACTIVITY 回應時插一個 PING。不回 PONG 對方會斷
    線；把 PING 當成回應則是回報成功卻其實沒收到答案。"""
    transport = _ScriptedTransport(_READY, _PING_FRAME, _ACCEPTED)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"
    ops = [struct.unpack("<II", f[:8])[0] for f in transport.sent]
    assert rpc._OP_PONG in ops, "收到 PING 卻沒回 PONG"


def test_an_unparsable_ping_body_does_not_break_the_round(monkeypatch):
    """PING 的 body 不是合法 JSON（或不是合法 UTF-8）。回不了 PONG 是小事，讓
    整輪失敗才是大事。注意 `UnicodeDecodeError` 不是 `JSONDecodeError`。"""
    junk = b"\xff\xfe"
    transport = _ScriptedTransport(
        _READY, struct.pack("<II", rpc._OP_PING, len(junk)) + junk, _ACCEPTED)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "ok"


def test_a_flood_of_pings_does_not_recurse_forever(monkeypatch):
    """`_recv` 處理 PING 的方式是遞迴。沒有深度上限的話，一條錯位的 pipe（每
    8 bytes 都被讀成一個 PING header）會把 worker thread 的 stack 吃掉。

    斷言的是**做了多少工**，不是「有沒有活下來」——60 個 PING 遞迴下去也不會爆
    stack，所以「回傳值在詞彙裡」對有沒有深度上限完全分不出來。有上限時只會讀
    十來次就放手；沒有的話它會被對端一路牽著走。"""
    pings = [_PING_FRAME for _ in range(60)]
    transport = _ScriptedTransport(_READY, *pings, _ACCEPTED)
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) in _VOCABULARY
    assert transport.reads <= 20, (
        f"讀了 {transport.reads} 次——PING 沒有深度上限，"
        "對端只要一直送 PING 就能把我們無限拖著走")


# --- 被拒的形狀 -------------------------------------------------------------

def test_a_rejection_with_an_unexpected_shape_is_still_a_rejection(monkeypatch):
    """`data` 不是 dict（協定變動 / 對端有 bug）。硬去 `.get("code")` 會丟
    AttributeError，一路冒到 probe loop——而這只是一則被拒的回應而已。"""
    transport = _ScriptedTransport(
        _READY, _frame(1, {"evt": "ERROR", "data": "just a string"}))
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "rejected"
    assert client.status()["connected"] is True, "被拒是 payload 的問題，不是連線的"


def test_a_rejection_without_a_data_field_is_still_a_rejection(monkeypatch):
    transport = _ScriptedTransport(_READY, _frame(1, {"evt": "ERROR"}))
    client = _client_over(monkeypatch, transport)
    assert client.apply(_ACTIVITY, "123", 60.0) == "rejected"


# --- close() ----------------------------------------------------------------

def test_close_clears_the_card_before_dropping_the_connection(monkeypatch):
    """關機時先明確送一張空的 activity。只把 pipe 關掉通常也會讓卡片消失，但
    那是在賭桌面端的收尾行為；明講一次不花錢。"""
    transport = _ScriptedTransport(_READY, _ACCEPTED)
    client = _client_over(monkeypatch, transport)
    client.apply(_ACTIVITY, "123", 60.0)
    client.close()
    last = json.loads(transport.sent[-1][8:].decode("utf-8"))
    assert last["cmd"] == "SET_ACTIVITY"
    assert last["args"]["activity"] is None
    assert transport.closed is True
    assert client.status()["connected"] is False


def test_close_survives_a_pipe_that_is_already_gone(monkeypatch):
    """`close()` 走在關機路徑上。它自己丟例外會把關機流程打斷，而這時候 pipe
    早就死了正是最常見的情況。"""
    transport = _ScriptedTransport(_READY, _ACCEPTED,
                                   write_error=OSError("already gone"),
                                   write_error_after=2)
    client = _client_over(monkeypatch, transport)
    client.apply(_ACTIVITY, "123", 60.0)
    client.close()
    assert client.status()["connected"] is False


def test_close_on_a_client_that_never_connected_is_a_no_op(monkeypatch):
    monkeypatch.setattr(rpc, "_open_transport", lambda: None)
    rpc.RichPresenceClient().close()


# --- `_Transport`：兩種平台底層包成同一組介面 --------------------------------

def test_a_short_write_is_retried_until_the_frame_is_whole():
    """`buffering=0` 的 `FileIO` 依契約允許**短寫**並回傳實際寫入量。當成「寫
    完了」的話 frame 會少幾個 byte，於是接下來每一個 frame 都錯位——症狀是讀
    到垃圾長度，離真正的原因很遠。"""
    class _Dribble:
        def __init__(self):
            self.buf = bytearray()
            self.flushed = 0

        def write(self, view):
            taken = bytes(view[:3])
            self.buf += taken
            return len(taken)

        def flush(self):
            self.flushed += 1

    sink = _Dribble()
    payload = b"x" * 25
    rpc._Transport(file_obj=sink).write(payload)
    assert bytes(sink.buf) == payload
    assert sink.flushed == 1, "沒有 flush 的話 frame 可能停在 buffer 裡"


@pytest.mark.parametrize("returned, why", [
    (0, "回 0（寫不進去）"),
    (None, "回 None（非阻塞的 raw write 允許這樣）"),
])
def test_a_write_that_makes_no_progress_is_an_error_not_a_silent_loop(
        returned, why):
    """既不能當成「寫完了」（frame 會少 byte），也不能無限重試（活鎖，而且是在
    一條 worker thread 上）。丟例外讓上層斷線重連才是對的。

    `_Stuck` 自己有次數上限：拿掉那個檢查之後 `view = view[0:]` 就是原地打轉，
    測試會**掛住**而不是變紅。掛住的測試比紅的難查，所以把活鎖換成一個講得出
    原因的失敗。"""
    class _Stuck:
        def __init__(self):
            self.calls = 0

        def write(self, _view):
            self.calls += 1
            if self.calls > 1_000:
                raise AssertionError(
                    "寫了 1000 次還在原地——「沒有進展就丟例外」那道檢查沒了，"
                    "這在正式執行時是一條 worker thread 的活鎖。")
            return returned

        def flush(self):
            pass

    with pytest.raises(OSError):
        rpc._Transport(file_obj=_Stuck()).write(b"abc")


def test_the_named_pipe_side_reads_straight_off_the_file_object():
    """Windows 那半把 named pipe 當成 binary 檔讀寫。這是 bot 實際跑的那條路，
    而它跟 socket 那半是兩段不同的程式碼。

    最後那個「EOF 要回空 bytes」不是形式：`_read_exact` 的迴圈就是靠它收斂成
    `OSError`。回別的東西（或丟例外）都會讓斷線變成別的症狀。"""
    handle = io.BytesIO(b"abcdef")
    transport = rpc._Transport(file_obj=handle, path=r"\\.\pipe\discord-ipc-0")
    assert transport.read(4) == b"abcd"
    assert transport.read(4) == b"ef", "短讀照實回傳，由 `_read_exact` 收拾"
    assert transport.read(4) == b"", "EOF 要回空 bytes —— `_read_exact` 靠它收斂"
    transport.close()
    assert handle.closed is True


def test_the_socket_side_goes_through_sendall_and_recv():
    """Unix 那半用 socket。`sendall` 自己處理短寫，所以不必套上面那個迴圈。"""
    class _Sock:
        def __init__(self):
            self.sent = b""
            self.closed = False

        def sendall(self, data):
            self.sent += data

        def recv(self, n):
            return b"z" * n

        def close(self):
            self.closed = True

    sock = _Sock()
    transport = rpc._Transport(sock=sock, path="/run/user/1000/discord-ipc-0")
    transport.write(b"hi")
    assert sock.sent == b"hi"
    assert transport.read(3) == b"zzz"
    transport.close()
    assert sock.closed is True


@pytest.mark.parametrize("kwargs", [
    {"file_obj": "file"},
    {"sock": "sock"},
    {},
])
def test_closing_a_transport_never_raises(kwargs):
    """`close()` 出現在每一條失敗路徑上。它自己丟例外會把**真正的**失敗原因蓋
    掉——使用者看到的會是 close 的錯，不是斷線的錯。"""
    class _Bad:
        def close(self):
            raise OSError("handle already invalid")

    resolved = {key: _Bad() for key in kwargs}
    rpc._Transport(**resolved).close()


# --- `_open_transport`：掃 discord-ipc-0 ~ 9 --------------------------------

def test_the_named_pipe_scan_stops_at_the_first_one_that_opens(monkeypatch):
    """桌面程式可能開在 0 ~ 9 的任何一個（多開 / Canary / 之前沒收乾淨）。

    這裡換掉 `builtins.open`，但只攔 discord pipe 路徑，其餘一律轉給真的
    `open`——否則 pytest 自己的 I/O 會一起被攔走。"""
    if sys.platform != "win32":
        pytest.skip("named pipe 掃描是 Windows 專屬分支")
    tried = []
    real_open = builtins.open

    def _fake_open(path, *args, **kwargs):
        if isinstance(path, str) and "discord-ipc-" in path:
            tried.append(path)
            if path.endswith("-3"):
                return io.BytesIO()
            raise OSError("pipe busy")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _fake_open)
    transport = rpc._open_transport()
    assert transport is not None
    assert transport.path.endswith("-3")
    assert len(tried) == 4, "找到就該停手，不該把 10 條都掃完"


def test_no_named_pipe_at_all_is_none_not_an_exception(monkeypatch):
    """桌面程式沒開就是這條路。回 None 讓上層安靜重試，不是丟例外。"""
    if sys.platform != "win32":
        pytest.skip("named pipe 掃描是 Windows 專屬分支")
    tried = []
    real_open = builtins.open

    def _fake_open(path, *args, **kwargs):
        if isinstance(path, str) and "discord-ipc-" in path:
            tried.append(path)
            raise OSError("no such pipe")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _fake_open)
    assert rpc._open_transport() is None
    assert len(tried) == 10, "0 ~ 9 都要掃過才能說沒有"


def test_the_unix_socket_scan_closes_the_ones_it_could_not_connect(
        monkeypatch, tmp_path):
    """連不上就把 socket 關掉再試下一個。只靠 refcount 回收會留下
    `ResourceWarning`，而這支每個 probe tick 都跑、每次最多掃 40 條路徑。

    這條分支在 Windows 上跑不到（`sys.platform` 卡住它，而且沒有 `AF_UNIX`），
    所以兩個都換掉。換 `sys.platform` 是全行程生效的，但測試是序列執行、
    `monkeypatch` 會還原，臨界區裡也沒有別的東西在讀它。"""
    monkeypatch.setattr(rpc, "_running_on_windows", lambda: False)
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "discord-ipc-0").write_bytes(b"")
    (tmp_path / "discord-ipc-1").write_bytes(b"")
    made = []

    class _Sock:
        def __init__(self, *_args):
            made.append(self)
            self.closed = False
            self.timeout = None

        def settimeout(self, value):
            self.timeout = value

        def connect(self, path):
            if path.endswith("-0"):
                raise OSError("connection refused")

        def close(self):
            self.closed = True

    monkeypatch.setattr(socket, "socket", _Sock)
    transport = rpc._open_transport()
    assert transport is not None
    assert transport.path.endswith("-1")
    assert made[0].closed is True, "連不上的 socket 沒被關掉 → fd 會累積"
    assert made[1].timeout == rpc._IPC_TIMEOUT_SEC, (
        "socket 沒設 timeout：對端卡死時會把呼叫端的 worker thread 永久掛住")


def test_a_socket_that_also_fails_to_close_does_not_stop_the_scan(
        monkeypatch, tmp_path):
    """收拾失敗路徑的程式碼自己也會失敗。

    這裡的 `close()` 是在收拾一條**本來就連不上**的 socket；讓它的例外逸出的
    話，整個 `_open_transport` 會炸掉，於是後面那條真的連得上的 pipe 永遠掃不
    到——presence 從此不會再啟動，而原因是一個 fd 沒關成功。"""
    monkeypatch.setattr(rpc, "_running_on_windows", lambda: False)
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "discord-ipc-0").write_bytes(b"")
    (tmp_path / "discord-ipc-1").write_bytes(b"")

    class _Sock:
        def __init__(self, *_args):
            self.path = ""

        def settimeout(self, value):
            del value

        def connect(self, path):
            self.path = path
            if path.endswith("-0"):
                raise OSError("connection refused")

        def close(self):
            raise OSError("handle already invalid")

    monkeypatch.setattr(socket, "socket", _Sock)
    transport = rpc._open_transport()
    assert transport is not None, "收拾第一條的例外把整輪掃描帶走了"
    assert transport.path.endswith("-1")


def test_a_socket_that_cannot_even_be_created_is_skipped(monkeypatch, tmp_path):
    """`socket()` 自己就失敗（fd 用完、AF_UNIX 被禁）。這時候還沒有東西可以
    關，收拾的程式碼不可以假設一定有。"""
    monkeypatch.setattr(rpc, "_running_on_windows", lambda: False)
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "discord-ipc-0").write_bytes(b"")

    def _cannot_create(*_args):
        raise OSError("too many open files")

    monkeypatch.setattr(socket, "socket", _cannot_create)
    assert rpc._open_transport() is None


def test_the_unix_scan_returns_none_when_nothing_is_there(monkeypatch, tmp_path):
    monkeypatch.setattr(rpc, "_running_on_windows", lambda: False)
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def _must_not_construct(*_args):
        raise AssertionError("路徑都不存在，不該去建 socket")

    monkeypatch.setattr(socket, "socket", _must_not_construct)
    assert rpc._open_transport() is None

def test_the_posix_seam_is_the_one_production_actually_reads(monkeypatch, tmp_path):
    """接縫換成「不是 Windows」之後，正式碼**絕對不可以**去碰具名管道。

    這支釘的是 2026-09-12 真的發生過的事，而且它是這一族測試裡唯一**不看人臉色**的
    那一支。當天的情況：`_open_transport` 的述詞從 `sys.platform == "win32"` 改成
    `os.name == "nt"`，四支模擬 POSIX 的測試卻還在換 `sys.platform`——**替身換掉的
    接縫已經不是正式碼在讀的那一個**，於是 Windows 分支照跑、連上真的桌面程式、
    回傳一個真的 transport。

    **為什麼不能只斷言回傳值**：桌面程式**沒開**的時候，Windows 分支每一條管道都開
    失敗、乖乖回 `None`，於是「斷言回 None」那兩支會一路全綠。同一份程式碼、同一支
    測試，答案取決於當下桌面上有沒有開一個聊天程式——那不是測試，那是擲骰子。
    所以這裡斷言的是**有沒有去開那個管道**，而那件事與桌面程式開不開完全無關。

    （四支原測試已經改成 patch `rpc._running_on_windows`，本支是防止有人把述詞再
    inline 回去而沒有人發現。）
    """
    monkeypatch.setattr(rpc, "_running_on_windows", lambda: False)
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    opened: list[str] = []
    real_open = builtins.open

    def _spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _spy)
    rpc._open_transport()

    pipes = [p for p in opened if "pipe" in p.lower()]
    assert not pipes, (
        f"接縫說「不是 Windows」，正式碼卻還是去開了具名管道：{pipes}。"
        "代表 `_open_transport` 沒有讀 `_running_on_windows()`，而是自己又寫了一次"
        "平台判斷——測試替身於是換不到它。這正是 2026-09-12 那次的形狀，"
        "當時的症狀是連上真的桌面程式並回傳一個真的 transport。")
def test_the_platform_seam_actually_reads_the_platform():
    """接縫本體必須真的去問平台，不能是一個常數。

    這支補的是上一支補不到的那一半。四支 POSIX 測試（與那支金絲雀）都會把
    `_running_on_windows` 換掉，所以**它的本體在那些測試裡一次都沒執行過**——
    把它改成 `return True` 或 `return False`，五支照樣全綠（2026-09-12 變異測試
    實測 SURVIVED）。而它回錯值的後果是正式環境整條 RPC 走錯分支。

    本專案對這個形狀的判語是「被死掉的儀式滿足的守門」：檢查一個計算**在不在**，
    在它的結果是結構性常數時照樣會過。所以這裡兩半都問——這台機器上的真實答案，
    以及「它的回傳值到底會不會隨平台變」。
    """
    assert rpc._running_on_windows() is (os.name == "nt"), (
        "接縫在這台機器上答錯了——它必須與 `os.name` 一致。")

    src = _pathlib.Path(rpc.__file__).read_text(encoding="utf-8")
    tree = _ast.parse(src, rpc.__file__)
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef)
              and n.name == "_running_on_windows")
    body = _ast.unparse(fn)
    assert "os.name" in body, (
        f"`_running_on_windows` 沒有讀 `os.name`：{body}")
    constant_returns = [n for n in _ast.walk(fn)
                        if isinstance(n, _ast.Return)
                        and isinstance(n.value, _ast.Constant)]
    assert not constant_returns, (
        "`_running_on_windows` 直接回一個常數——那不是接縫，是寫死的答案。"
        "它會讓所有換掉它的測試繼續全綠，而正式環境走錯分支。")

# --- 設定檔的失敗路徑 -------------------------------------------------------

def test_an_undecodable_config_falls_back_instead_of_raising(rpc_file, capsys):
    """`UnicodeDecodeError` 是 `ValueError` 的子類別、**不是** `OSError`。本機
    locale 是 cp950 而這個檔幾乎一定含中文，被別的編輯器另存成 Big5 就會走到
    這裡；只接 `OSError` 的話它會從 probe loop 逸出。"""
    rpc_file.write_bytes('{"client_id": "\u5f35"}'.encode("big5"))
    cfg = rpc.load_rpc_config()
    assert cfg["enabled"] is False
    assert cfg["client_id"] == ""
    assert "presence_rpc.json" in capsys.readouterr().err


def test_a_config_that_cannot_be_read_falls_back(rpc_file, capsys):
    """讀得到路徑但讀不出內容（這裡用「那其實是個目錄」製造）。"""
    rpc_file.mkdir()
    cfg = rpc.load_rpc_config()
    assert cfg["enabled"] is False
    assert capsys.readouterr().err != "", "退回預設卻一聲不吭 → 沒人知道設定沒生效"


def test_the_small_asset_slot_is_wired_up_too(rpc_file):
    """大圖那組有測，小圖那組是另一段程式碼。"""
    rpc_file.write_text(
        json.dumps({"kinds": {"playing": {"small_image": "badge",
                                          "small_text": "{name} 進行中"}}}),
        encoding="utf-8")
    cfg = rpc.load_rpc_config()
    act = rpc.build_activity({"kind": "playing", "name": "G"}, cfg, 0)
    assert act["assets"]["small_image"] == "badge"
    assert act["assets"]["small_text"] == "G 進行中"



# --- 成功之後要把上一次的失敗細節清掉（2026-09-08 修的缺陷）-----------------

def test_a_success_clears_the_previous_failure_detail(monkeypatch):
    """`rejected` 那條路**刻意不斷線**，所以它記下的錯誤等不到 `_connect()` 來清。

    原本 `_last_error` 只在 `_connect()` 成功時歸零，於是被拒過一次之後，
    `status()["last_error"]` 會一直掛著那句 rejection，狀態指令看起來像 RPC 還壞著。
    """
    transport = _ScriptedTransport(_READY, _REJECTED, _ACCEPTED)
    monkeypatch.setattr(rpc, "_open_transport", lambda: transport)
    client = rpc.RichPresenceClient()
    assert client.apply({"details": "a"}, "cid", 0.0) == "rejected"
    assert client.status()["last_error"], "被拒卻沒記下細節"
    assert client.apply({"details": "b"}, "cid", 0.0) == "ok"
    assert client.status()["last_error"] == "", (
        "成功之後 `last_error` 還留著上一次的失敗——狀態指令會一直報一個過去式")


def test_a_recurring_failure_is_logged_again_after_an_intervening_success(
        monkeypatch, capsys):
    """**這一支才是那個缺陷真正痛的地方。**

    `_fail()` 的去重比的是「上一次記到的字串」，不是「上一個 tick」。所以只要
    `_last_error` 沒被成功清掉，同一個錯誤在夾了一次成功之後再犯就**不會**再寫進
    stderr——log 上只留得下第一次。也就是說**會重複發生的問題，正好是最看不見的
    那一種**，而那正是最需要看見的。

    只驗上一支（`last_error` 有沒有清）是不夠的：那只擋住顯示層的陳舊，擋不住
    這裡的靜默。
    """
    transport = _ScriptedTransport(_READY, _REJECTED, _ACCEPTED, _REJECTED)
    monkeypatch.setattr(rpc, "_open_transport", lambda: transport)
    client = rpc.RichPresenceClient()
    assert client.apply({"details": "a"}, "cid", 0.0) == "rejected"
    capsys.readouterr()
    assert client.apply({"details": "b"}, "cid", 0.0) == "ok"
    assert client.apply({"details": "c"}, "cid", 0.0) == "rejected"
    assert "4000" in capsys.readouterr().err, (
        "同一個錯誤再犯卻沒有再進 stderr——重複發生的問題會完全消失在 log 裡")



if __name__ == "__main__":
    sys.exit(main())
