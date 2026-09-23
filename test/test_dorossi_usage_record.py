"""Token 用量帳本的落地與修剪——`_dorossi_record_usage` / `_dorossi_trim_usage_file`。

這兩支是 `/dorossi tokens` 唯一的資料來源，而它們的失敗模式全部是**安靜**的：

* 記錄那一支的合約明寫「絕不打斷一輪對話、絕不冒到對話平台」，所以它把每個例外都
  吞掉。吞得對，但吞掉之後如果連 stderr 都沒留，用量報表就會從某一天起悄悄停止
  成長，而沒有任何人會收到通知。
* 修剪那一支**只在檔案已經長過門檻時才會執行**——也就是說它平常一行都跑不到，
  等它真的跑的那天，才是它第一次被執行。這正是本輪一直在抓的那類程式碼：只在
  「已經出事」之後才走到的路徑，錯了也看不出來。

2026-09-06 量覆蓋率時發現 `_dorossi_trim_usage_file` **整支沒有任何測試提過它**，
順手抓到一個實際的小缺陷：`os.replace` 只有成功時才會把 `.tmp` 搬走，而這支的
合約是絕不往外拋，於是搬失敗時 `dorossi_usage.ndjson.tmp` 會一直留在 repo root
——一份看起來像真資料的半份資料，而且正是 `test_gitignore_coverage.py` 在盯的
那種執行期產物。

遲滯（`_DOROSSI_USAGE_TRIM_AT` > `_DOROSSI_USAGE_MAX_LINES`）也在這裡釘住：兩個
數字如果被「整理」成同一個，每追加一列就要整檔重寫一次。
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import dorossi_backend as db  # noqa: E402


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """把帳本指到 tmp_path，回傳那個 Path。"""
    path = tmp_path / "dorossi_usage.ndjson"
    monkeypatch.setattr(db, "DOROSSI_USAGE_FILE", path)
    return path


def _fill(path: Path, n: int) -> None:
    path.write_text(
        "".join(json.dumps({"ts": float(i), "in": i}) + "\n" for i in range(n)),
        encoding="utf-8")


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------
# 修剪：門檻、遲滯、保留的是哪一端
# --------------------------------------------------------------------------

def test_a_file_under_the_threshold_is_left_alone(ledger):
    _fill(ledger, 10)
    before = ledger.read_text(encoding="utf-8")
    db._dorossi_trim_usage_file()
    assert ledger.read_text(encoding="utf-8") == before


def test_a_file_exactly_at_the_threshold_is_left_alone(ledger):
    """邊界：`<=` 不是 `<`。差一格在這裡是「白白整檔重寫一次」。"""
    _fill(ledger, db._DOROSSI_USAGE_TRIM_AT)
    before = ledger.read_text(encoding="utf-8")
    db._dorossi_trim_usage_file()
    assert ledger.read_text(encoding="utf-8") == before


def test_crossing_the_threshold_cuts_down_to_the_keep_size(ledger):
    _fill(ledger, db._DOROSSI_USAGE_TRIM_AT + 1)
    db._dorossi_trim_usage_file()
    assert len(_rows(ledger)) == db._DOROSSI_USAGE_MAX_LINES


def test_the_trim_keeps_the_newest_rows_not_the_oldest(ledger):
    """報表看的是「最近用了多少」。留最舊的等於報表從此凍結在過去某一天。"""
    total = db._DOROSSI_USAGE_TRIM_AT + 500
    _fill(ledger, total)
    db._dorossi_trim_usage_file()
    rows = _rows(ledger)
    assert rows[-1]["ts"] == float(total - 1)
    assert rows[0]["ts"] > 0.0


def test_the_two_thresholds_leave_room_for_hysteresis():
    """`TRIM_AT` 必須嚴格大於 `MAX_LINES`。

    兩個數字被「整理」成同一個的話，修剪完就正好卡在門檻上，於是**每追加一列都要
    整檔重寫一次**——而追加是每一輪對話都會發生的事。這是效能問題，不會有任何測試
    自然變紅，所以在這裡明講。
    """
    assert db._DOROSSI_USAGE_TRIM_AT > db._DOROSSI_USAGE_MAX_LINES


def test_a_missing_ledger_is_not_an_error(ledger):
    assert not ledger.exists()
    db._dorossi_trim_usage_file()          # 不得拋
    assert not ledger.exists()


def test_the_rewrite_is_atomic(ledger, monkeypatch):
    """半寫入的帳本會讀成「用量突然歸零」，所以必須是 temp ＋ `os.replace`。

    直接盯 `os.replace` 有沒有被叫到，而不是盯原始碼字面——換成別的原子寫法一樣
    通過，換成 truncate-then-write 就會紅。
    """
    seen = []
    real = db.os.replace
    monkeypatch.setattr(db.os, "replace",
                        lambda a, b: (seen.append((str(a), str(b))), real(a, b))[1])
    _fill(ledger, db._DOROSSI_USAGE_TRIM_AT + 1)
    db._dorossi_trim_usage_file()
    assert seen, "沒有經過 `os.replace`——半寫入的帳本會被讀成用量歸零"
    assert seen[0][1] == str(ledger)


def test_a_failed_replace_leaves_no_temp_behind(ledger, monkeypatch):
    """`os.replace` 只有**成功**時才會把 temp 搬走。

    這支的合約是絕不往外拋，所以失敗時不重拋——但也不能把半份資料留在 repo root。
    留著的話它會一直躺在那裡看起來像真資料，而且它正是
    `test_gitignore_coverage.py` 在盯的那種執行期產物。
    """
    def _boom(_a, _b):
        raise OSError("replace failed")

    monkeypatch.setattr(db.os, "replace", _boom)
    _fill(ledger, db._DOROSSI_USAGE_TRIM_AT + 1)
    db._dorossi_trim_usage_file()          # 不得拋
    leftovers = sorted(p.name for p in ledger.parent.glob("*.tmp"))
    assert leftovers == [], f"留下了 {leftovers}"


def test_a_failed_trim_still_leaves_the_original_intact(ledger, monkeypatch):
    """修剪失敗的代價只能是「這次沒修剪」，不能是「帳本沒了」。"""
    monkeypatch.setattr(db.os, "replace",
                        lambda _a, _b: (_ for _ in ()).throw(OSError("nope")))
    _fill(ledger, db._DOROSSI_USAGE_TRIM_AT + 1)
    db._dorossi_trim_usage_file()
    assert len(_rows(ledger)) == db._DOROSSI_USAGE_TRIM_AT + 1


def test_a_failed_trim_leaves_a_line_on_stderr(ledger, monkeypatch, capsys):
    monkeypatch.setattr(db.os, "replace",
                        lambda _a, _b: (_ for _ in ()).throw(OSError("nope")))
    _fill(ledger, db._DOROSSI_USAGE_TRIM_AT + 1)
    db._dorossi_trim_usage_file()
    assert "trim failed" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 記錄：schema、缺欄位、壞值、以及「絕不打斷一輪對話」
# --------------------------------------------------------------------------

def test_a_round_is_appended_with_the_split_schema(ledger):
    db._dorossi_record_usage(
        {"in": 1, "cr": 2, "cc": 3, "out": 4, "cost_usd": 0.5})
    row = _rows(ledger)[0]
    assert {"ts", "in", "cr", "cc", "out", "cost_usd"} <= set(row)
    assert (row["in"], row["cr"], row["cc"], row["out"]) == (1, 2, 3, 4)
    assert row["cost_usd"] == 0.5


def test_missing_fields_become_zero_not_absent(ledger):
    """讀取端把這些欄位當數字加總。缺欄位會變成 `KeyError` 或 `None + int`。"""
    db._dorossi_record_usage({})
    row = _rows(ledger)[0]
    assert row["in"] == row["cr"] == row["cc"] == row["out"] == 0
    assert row["cost_usd"] == 0.0


@pytest.mark.parametrize("bad", [
    {"in": "12"}, {"in": None}, {"in": True}, {"cost_usd": "x"},
    {"cost_usd": True}, {"out": [1]},
])
def test_a_non_numeric_field_does_not_poison_the_ledger(ledger, bad):
    """帳本是 `/dorossi tokens` 的唯一資料來源，一列壞值會讓整份報表算不出來。"""
    db._dorossi_record_usage(bad)
    row = _rows(ledger)[0]
    for key in ("in", "cr", "cc", "out"):
        assert isinstance(row[key], int) and not isinstance(row[key], bool)
    assert isinstance(row["cost_usd"], float)


def test_a_bool_is_not_recorded_as_one(ledger):
    """`True` 在 Python 裡是 `int` 的子類別，`isinstance(v, int)` 會放它過去。"""
    db._dorossi_record_usage({"in": True, "cost_usd": True})
    row = _rows(ledger)[0]
    assert row["in"] == 0
    assert row["cost_usd"] == 0.0


def test_a_non_dict_info_is_tolerated(ledger):
    """`info` 是從後端事件組出來的，型別不由我們保證。"""
    db._dorossi_record_usage(None)          # 不得拋
    db._dorossi_record_usage("nonsense")
    assert len(_rows(ledger)) == 2


def test_the_maintenance_kind_is_recorded_but_bounded(ledger):
    db._dorossi_record_usage({"kind": "compact"})
    assert _rows(ledger)[0]["k"] == "compact"
    db._dorossi_record_usage({"kind": "x" * 200})
    assert len(_rows(ledger)[1]["k"]) == 32


def test_an_ordinary_round_has_no_kind_field(ledger):
    """一般工作輪的列要維持原樣，舊資料與舊讀取端都不受影響。"""
    db._dorossi_record_usage({"in": 1})
    assert "k" not in _rows(ledger)[0]


def test_recording_never_raises_even_when_the_write_fails(ledger, monkeypatch,
                                                          capsys):
    """記錄用量絕不能打斷一輪對話——這條是它存在的前提，不是好意。"""
    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(db, "open", _boom, raising=False)
    monkeypatch.setattr("builtins.open", _boom)
    db._dorossi_record_usage({"in": 1})     # 不得拋
    assert "record failed" in capsys.readouterr().err


def test_recording_trims_on_the_way_out(ledger):
    """追加之後要順手修剪，否則帳本的界限只是紙上的。"""
    _fill(ledger, db._DOROSSI_USAGE_TRIM_AT)
    db._dorossi_record_usage({"in": 1})
    assert len(_rows(ledger)) == db._DOROSSI_USAGE_MAX_LINES


def test_the_failure_notice_never_reaches_the_chat_platform():
    """Layer 1：這兩支只能對 stderr 說話。

    它們的訊息帶著原始例外文字（`{exc!r}`），那是 Layer 1 明文禁止外送的東西之一
    ——而且這裡沒有提問者可以判定，連擁有者例外都用不上。
    """
    import ast
    tree = ast.parse(
        (Path(db.__file__)).read_text(encoding="utf-8"))
    banned = ("reply", "send", "safe_reply", "followup", "edit")
    for name in ("_dorossi_trim_usage_file", "_dorossi_record_usage"):
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == name), None)
        assert fn is not None, f"{name} 改名了——這支守門要跟著改"
        prints = [c for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                  and c.func.id == "print"]
        assert prints, f"{name} 把錯誤完全吞掉了，連 stderr 都沒留"
        for call in prints:
            assert any(kw.arg == "file" and ast.unparse(kw.value) == "sys.stderr"
                       for kw in call.keywords), (
                f"{name}：{ast.unparse(call)[:70]} 沒有指定 `file=sys.stderr`")
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            attr = (call.func.attr if isinstance(call.func, ast.Attribute)
                    else getattr(call.func, "id", ""))
            assert attr not in banned, (
                f"{name}：{ast.unparse(call)[:70]} 把內部狀態送去對話平台了")


# --------------------------------------------------------------------------
# 脈絡大小：最後一次 API 呼叫，不是整次叫用的加總（2026-09-19）
#
# `usage` 與 `modelUsage` 都是**這次叫用所有 API 呼叫的加總**，`usage.iterations`
# 只有一筆＝最後一次呼叫（2.1.276 與 2.1.277 各實測一次）。壓縮觸發原本拿加總當
# 「脈絡大小」，full 模式一輪幾十次工具呼叫就把同一段前綴算幾十次：帳本末 186 筆的
# 工作輪量到 250 萬～1 億，永遠過 300k 門檻，末 40 筆工作輪與壓縮輪嚴格交替。
# --------------------------------------------------------------------------

# 2026-09-19 用本機 CLI（2.1.276、haiku、一次叫用裡 4 次 API 呼叫）抓下來的 result
# 事件，只留這裡用得到的欄位。
REAL_TOOL_ROUND = {
    "type": "result", "subtype": "success", "is_error": False,
    "total_cost_usd": 0.0213713, "num_turns": 4,
    "usage": {"input_tokens": 34, "cache_creation_input_tokens": 7992,
              "cache_read_input_tokens": 23043, "output_tokens": 405,
              "iterations": [{"input_tokens": 8, "output_tokens": 71,
                              "cache_read_input_tokens": 7844,
                              "cache_creation_input_tokens": 148,
                              "type": "message"}]},
    "modelUsage": {"claude-haiku-4-5-20251001": {
        "inputTokens": 958, "outputTokens": 425, "cacheReadInputTokens": 23043,
        "cacheCreationInputTokens": 7992, "costUSD": 0.0213713}},
}


def _with_iterations(iterations):
    ev = json.loads(json.dumps(REAL_TOOL_ROUND))
    ev["usage"]["iterations"] = iterations
    return ev


def test_the_context_is_the_last_call_not_the_sum():
    info = db._dorossi_cc_round_info(REAL_TOOL_ROUND)
    assert info["ctx"] == 8 + 7844 + 148
    # 對照：加總是 4 倍左右——這正是舊觸發量到的東西。
    assert info["in"] + info["cr"] + info["cc"] == 958 + 23043 + 7992
    assert db._dorossi_context_tokens(info) == 8000


@pytest.mark.parametrize("iterations", [
    None, "nope", {}, [], [None], ["x"], [{"type": "message"}],
    [{"type": "message", "input_tokens": "8", "cache_read_input_tokens": None}],
])
def test_an_unreadable_iterations_block_falls_back_to_the_sum(iterations):
    """讀不到就退回舊的加總估計（方向是壓得太早，不會漏壓），**不是**當成 0——
    當成 0 會讓壓縮觸發永遠不開火。"""
    ev = _with_iterations(iterations)
    if iterations is None:
        del ev["usage"]["iterations"]
    info = db._dorossi_cc_round_info(ev)
    assert "ctx" not in info, info
    assert db._dorossi_context_tokens(info) == 958 + 23043 + 7992


def test_the_last_message_entry_wins_and_other_entry_types_are_skipped():
    ev = _with_iterations([
        {"type": "message", "input_tokens": 1, "cache_read_input_tokens": 100,
         "cache_creation_input_tokens": 0},
        {"type": "message", "input_tokens": 2, "cache_read_input_tokens": 200,
         "cache_creation_input_tokens": 3},
        {"type": "something_new", "input_tokens": 999999}])
    assert db._dorossi_last_call_context(ev) == 205


def test_a_malformed_last_entry_is_not_papered_over_by_an_older_one():
    """最後一筆壞掉就是讀不到——拿更早的一筆會給出一個看起來合理、其實過期的數字。"""
    ev = _with_iterations([
        {"type": "message", "input_tokens": 1, "cache_read_input_tokens": 100},
        "garbage"])
    assert db._dorossi_last_call_context(ev) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5, True])
def test_non_finite_or_negative_counts_never_raise(bad):
    """`json.loads` 收 NaN／Infinity；`int(nan)` 丟 ValueError。這幾支都在答案已經
    拿到之後才跑，在那裡丟例外等於把答完的回合打成失敗。"""
    ev = _with_iterations([{"type": "message", "input_tokens": bad,
                            "cache_read_input_tokens": 7000}])
    ev["modelUsage"]["claude-haiku-4-5-20251001"]["inputTokens"] = bad
    ev["total_cost_usd"] = bad if not isinstance(bad, bool) else 0.1
    info = db._dorossi_cc_round_info(ev)
    assert db._dorossi_context_tokens(info) == 7000
    assert info["cost_usd"] >= 0.0 and info["cost_usd"] == info["cost_usd"]


def test_context_tokens_prefers_ctx_and_falls_back_cleanly():
    assert db._dorossi_context_tokens({"ctx": 0, "in": 9, "cr": 9}) == 0
    assert db._dorossi_context_tokens({"ctx": True, "in": 1, "cr": 2, "cc": 3}) == 6
    assert db._dorossi_context_tokens({"in": 1, "cr": 2, "cc": 3, "out": 99}) == 6
    assert db._dorossi_context_tokens(None) == 0


def test_a_big_summed_round_with_a_small_last_call_does_not_compact(monkeypatch):
    """事故的直接反面：一輪工作 60 次工具呼叫、加總 300 萬，但最後一次呼叫的脈絡只有
    5 萬——**不得**觸發壓縮（兩個觸發點都是）。正面對照：最後一次呼叫過門檻就要壓；
    讀不到 iterations 時照舊用加總（所以也要壓）。"""
    monkeypatch.setattr(db, "DOROSSI_COMPACT_CONTEXT_TOKENS", 300000)
    monkeypatch.setattr(db, "DOROSSI_LOOP_COMPACT_EVERY_ROUNDS", 10)
    monkeypatch.setattr(db, "DOROSSI_LOOP_COMPACT_COST_USD", 10.0)
    ev = _with_iterations([{"type": "message", "input_tokens": 12,
                            "cache_read_input_tokens": 49000,
                            "cache_creation_input_tokens": 988}])
    ev["modelUsage"]["claude-haiku-4-5-20251001"]["cacheReadInputTokens"] = 3_000_000
    small = db._dorossi_cc_round_info(ev)
    assert db._dorossi_context_tokens(small) == 50000
    assert not db._dorossi_context_compaction_due(db._dorossi_context_tokens(small))
    assert not db._dorossi_loop_compaction_due(1, 0.5, db._dorossi_context_tokens(small))

    ev["usage"]["iterations"][0]["cache_read_input_tokens"] = 349000
    big = db._dorossi_cc_round_info(ev)
    assert db._dorossi_context_compaction_due(db._dorossi_context_tokens(big))
    assert db._dorossi_loop_compaction_due(1, 0.5, db._dorossi_context_tokens(big))

    del ev["usage"]["iterations"]
    legacy = db._dorossi_cc_round_info(ev)
    assert db._dorossi_context_compaction_due(db._dorossi_context_tokens(legacy))


# --------------------------------------------------------------------------
# 每次叫用的金額／token：兩種 CLI 語意都要對（2026-09-19）
#
# 2.1.277 起 `--resume` 回報的 `total_cost_usd`／`modelUsage` 是**工作階段累計**，
# 2.1.276 以前是每次叫用；頂層 `usage` 與 `iterations` 兩版都是每次叫用。實測數字：
# 2.1.276 金額 0.0141 → 0.0010 → 0.0010，2.1.277 0.0134 → 0.0144 → 0.0154。
# --------------------------------------------------------------------------

def _round(cost, *, cr, tok_in=20, cc=6500, out=80, top=(10, 6500, 80, 30),
           model_usage=True):
    """一次 resume 的 result 事件。`cost`／modelUsage 由呼叫端決定是每次還是累計；
    `top` 是頂層 usage（兩版都是每次叫用）。"""
    ev = {"type": "result", "subtype": "success", "is_error": False,
          "total_cost_usd": cost,
          "usage": {"input_tokens": top[0], "cache_read_input_tokens": top[1],
                    "cache_creation_input_tokens": top[2], "output_tokens": top[3]}}
    if model_usage:
        ev["modelUsage"] = {"m": {"inputTokens": tok_in, "cacheReadInputTokens": cr,
                                  "cacheCreationInputTokens": cc, "outputTokens": out,
                                  "costUSD": cost}}
    return ev


def _account(ev, **kw):
    kw.setdefault("sid", "sess-abc")
    return db._dorossi_cc_account_round(db._dorossi_cc_round_info(ev), ev, **kw)


def _chain(version, events):
    """照 bot 的做法跑一串 resume：每一輪的 `usage_mark` 當下一輪的基準。"""
    mark, out = None, []
    for i, ev in enumerate(events):
        info = _account(ev, resumed_id=("sess-abc" if i else None),
                        cli_version=version, baseline=mark)
        out.append(info)
        mark = info["usage_mark"]
    return out


def test_the_cumulative_cli_is_turned_back_into_per_call_numbers():
    """2.1.277 實測的三次：累計值 0.0134 → 0.0144 → 0.0154，每次叫用應該是
    0.0134、0.0010、0.0010——跟 2.1.276 的形狀一樣。"""
    infos = _chain("2.1.277", [
        _round(0.0134, cr=0, tok_in=10, cc=6497, out=42, top=(10, 0, 6497, 42)),
        _round(0.0144, cr=6497, tok_in=20, cc=6584, out=79, top=(10, 6497, 87, 37)),
        _round(0.0154, cr=13081, tok_in=30, cc=6665, out=113,
               top=(10, 6584, 81, 34))])
    assert [round(i["cost_usd"], 6) for i in infos] == [0.0134, 0.001, 0.001]
    assert [i["acct"] for i in infos] == ["call", "delta", "delta"]
    assert (infos[1]["in"], infos[1]["cr"], infos[1]["cc"], infos[1]["out"]) == \
        (10, 6497, 87, 37)
    assert (infos[2]["in"], infos[2]["cr"], infos[2]["cc"], infos[2]["out"]) == \
        (10, 6584, 81, 34)


def test_the_per_call_cli_is_left_alone():
    """2.1.276 實測的三次：回報值本來就是每次叫用，基準存了也不能拿來減。"""
    infos = _chain("2.1.276", [
        _round(0.0141, cr=0, cc=6577), _round(0.0010, cr=6577, cc=85),
        _round(0.0010, cr=6662, cc=79)])
    assert [i["cost_usd"] for i in infos] == [0.0141, 0.0010, 0.0010]
    assert {i["acct"] for i in infos} == {"call"}
    assert infos[-1]["usage_mark"]["mode"] == "per_call"


def test_a_counter_that_went_backwards_starts_over_from_the_current_value():
    """負差值＝計數器重新起算過（壓縮、工作階段重建）：這一次用回報值本身。"""
    base = {"sid": "sess-abc", "mode": "cumulative", "cost": 5.0, "tok": "model",
            "in": 900, "cr": 900000, "cc": 90000, "out": 9000}
    info = _account(_round(0.02, cr=6500), resumed_id="sess-abc",
                    cli_version="2.1.277", baseline=base)
    assert info["acct"] == "reset"
    assert info["cost_usd"] == 0.02
    assert info["cr"] == 6500
    # 只有金額倒退、token 差值看起來合理：同一個基準，token 也一起用原值。
    tokens_fine = dict(base, **{"in": 10, "cr": 0, "cc": 0, "out": 0})
    joint = _account(_round(0.02, cr=6500), resumed_id="sess-abc",
                     cli_version="2.1.277", baseline=tokens_fine)
    assert joint["acct"] == "reset", joint
    assert joint["in"] == 20, "token 還在用那個已經被金額否決的基準求差"


def test_a_delta_smaller_than_this_calls_own_usage_is_a_reset_not_a_delta():
    """差值比頂層 usage（這次叫用**一定**至少有的量）還小很多＝基準不屬於這一段。
    容差內的小差距不算（兩份數字來自 CLI 兩個不同的加總）。"""
    base = {"sid": "sess-abc", "mode": "cumulative", "cost": 0.01, "tok": "model",
            "in": 19, "cr": 6490, "cc": 6500, "out": 79}
    far = _account(_round(0.02, cr=6500, top=(10, 6500, 80, 30)),
                   resumed_id="sess-abc", cli_version="2.1.277", baseline=base)
    # 金額的差值（0.01）自己看起來沒問題，但它跟 token 用的是同一個基準——token 說
    # 基準不屬於這一段，金額就一起從原值起算。
    assert far["acct"] == "reset", far
    assert far["cost_usd"] == 0.02 and far["cr"] == 6500, far
    near = dict(base, cr=0, cc=6490)
    close = _account(_round(0.02, cr=6480, top=(10, 6500, 80, 30)),
                     resumed_id="sess-abc", cli_version="2.1.277", baseline=near)
    assert close["acct"] == "delta", close


def test_no_baseline_on_a_cumulative_cli_does_not_inflate():
    """升版後第一次 resume、或槽裡的基準遺失：分不出這次叫用佔多少。**金額記 0、
    token 記頂層 usage**，絕不把整個工作階段的總額（這裡 $42）算成一輪。"""
    info = _account(_round(42.0, cr=9_000_000, top=(10, 6500, 80, 30)),
                    resumed_id="sess-abc", cli_version="2.1.277", baseline=None)
    assert info["acct"] == "base"
    assert info["cost_usd"] == 0.0
    assert (info["in"], info["cr"], info["cc"], info["out"]) == (10, 6500, 80, 30)
    # 下一輪就有基準了——而且存的是原始累計值。
    assert info["usage_mark"]["cost"] == 42.0
    assert info["usage_mark"]["mode"] == "cumulative"


def test_a_per_call_baseline_is_not_a_cumulative_baseline():
    """升版當天：槽裡的基準是 2.1.276 存的**每次叫用**值。拿它當累計基準會把整個工作
    階段減掉一輪之後算成這一輪——必須當成沒有基準。"""
    old = {"sid": "sess-abc", "mode": "per_call", "cost": 0.001, "tok": "model",
           "in": 10, "cr": 6500, "cc": 80, "out": 30}
    info = _account(_round(3.5, cr=2_000_000), resumed_id="sess-abc",
                    cli_version="2.1.277", baseline=old)
    assert info["acct"] == "base"
    assert info["cost_usd"] == 0.0


def test_a_baseline_for_another_backend_session_is_ignored():
    base = {"sid": "sess-OTHER", "mode": "cumulative", "cost": 0.01, "tok": "model",
            "in": 0, "cr": 0, "cc": 0, "out": 0}
    info = _account(_round(3.5, cr=2_000_000), resumed_id="sess-abc",
                    cli_version="2.1.277", baseline=base)
    assert info["acct"] == "base"


def test_a_missing_version_inherits_the_sessions_last_mode():
    """init 沒帶版本：沿用同一個工作階段上一次的模式（CLI 不會在兩次叫用之間
    悄悄換語意、又同時不報版本）。上一次是累計 → 照樣求差。"""
    base = {"sid": "sess-abc", "mode": "cumulative", "cost": 0.0144, "tok": "model",
            "in": 20, "cr": 6497, "cc": 6584, "out": 79}
    info = _account(_round(0.0154, cr=13081, tok_in=30, cc=6665, out=113,
                           top=(10, 6584, 81, 34)),
                    resumed_id="sess-abc", cli_version=None, baseline=base)
    assert info["acct"] == "delta"
    assert round(info["cost_usd"], 6) == 0.001
    assert info["usage_mark"]["mode"] == "cumulative"


def test_a_missing_version_with_nothing_to_inherit_keeps_the_old_behaviour():
    info = _account(_round(0.02, cr=6500), resumed_id="sess-abc",
                    cli_version=None, baseline=None)
    assert info["acct"] == "call"
    assert info["cost_usd"] == 0.02
    assert "v" not in info


def test_a_fresh_session_is_per_call_on_every_version():
    """新工作階段（沒有 `--resume`）的累計就是這一次，兩版都一樣。"""
    for version in ("2.1.276", "2.1.277", None):
        info = _account(_round(0.0134, cr=0), resumed_id=None, cli_version=version,
                        baseline={"sid": "sess-abc", "mode": "cumulative",
                                  "cost": 9.0, "tok": "model",
                                  "in": 1, "cr": 1, "cc": 1, "out": 1})
        assert info["acct"] == "call" and info["cost_usd"] == 0.0134, version


def test_without_model_usage_only_the_cost_is_cumulative():
    """`modelUsage` 缺席時 token 來自頂層 usage——那一份本來就是每次叫用，不能拿去減。
    金額照樣求差。"""
    base = {"sid": "sess-abc", "mode": "cumulative", "cost": 0.0144, "tok": "usage",
            "in": 10, "cr": 6497, "cc": 87, "out": 37}
    info = _account(_round(0.0154, cr=0, top=(10, 6584, 81, 34), model_usage=False),
                    resumed_id="sess-abc", cli_version="2.1.277", baseline=base)
    assert round(info["cost_usd"], 6) == 0.001
    assert (info["in"], info["cr"], info["cc"], info["out"]) == (10, 6584, 81, 34)
    assert info["acct"] == "delta/call"
    assert info["usage_mark"]["tok"] == "usage"


@pytest.mark.parametrize("mark", [
    None, "x", [], {}, {"sid": ""}, {"sid": 5},
    {"sid": "sess-abc", "mode": "weird", "cost": 0.1, "in": 0, "cr": 0, "cc": 0,
     "out": 0},
    {"sid": "sess-abc", "mode": "cumulative", "cost": True, "in": 0, "cr": 0,
     "cc": 0, "out": 0},
    {"sid": "sess-abc", "mode": "cumulative", "cost": float("nan"), "in": 0,
     "cr": 0, "cc": 0, "out": 0},
    {"sid": "sess-abc", "mode": "cumulative", "cost": -1.0, "in": 0, "cr": 0,
     "cc": 0, "out": 0},
    {"sid": "sess-abc", "mode": "cumulative", "cost": 0.1, "in": 0, "cr": 0,
     "cc": 0},
    {"sid": "sess-abc", "mode": "cumulative", "cost": 0.1, "in": "0", "cr": 0,
     "cc": 0, "out": 0},
])
def test_a_hand_edited_baseline_is_treated_as_missing(mark):
    """基準來自 `dorossi_session.json`（本機可手改）。壞掉的一律當成沒有——在累計
    模式下那是「這一輪不計金額」的方向，不會膨脹，也不會 raise。"""
    assert db._dorossi_usage_mark_of(mark) is None
    info = _account(_round(3.5, cr=2_000_000), resumed_id="sess-abc",
                    cli_version="2.1.277", baseline=mark)
    assert info["acct"] == "base" and info["cost_usd"] == 0.0


@pytest.mark.parametrize("version,mode", [
    ("2.1.276", "per_call"), ("2.1.277", "cumulative"), ("2.1.300", "cumulative"),
    ("2.2.0", "cumulative"), ("10.0.0", "cumulative"), ("1.9.999", "per_call"),
    ("2.1.277-beta.1", "cumulative"), (" 2.1.277 ", "cumulative"),
    ("dev", None), ("", None), (None, None), (2.1, None), ("2.1", None),
])
def test_the_version_threshold(version, mode):
    assert db._dorossi_cc_totals_mode(version) == mode


def test_accounting_never_raises_on_garbage():
    for info, ev in ((None, None), ("x", 5), ({"cost_usd": float("inf")}, {}),
                     ({"cost_usd": True, "in": -3}, {"usage": "nope"})):
        out = db._dorossi_cc_account_round(info, ev, resumed_id="s", sid="s",
                                           cli_version="2.1.277", baseline=None)
        assert out["cost_usd"] == 0.0 and out["in"] >= 0


def test_no_mark_without_a_backend_session_id():
    info = _account(_round(0.01, cr=0), resumed_id=None, sid=None,
                    cli_version="2.1.277")
    assert "usage_mark" not in info


def test_the_ledger_records_the_audit_columns(ledger):
    """`ctx` 對得出「這一輪為什麼（沒）壓縮」、`acct` 說明數字怎麼換算的、`v` 分得出
    哪幾列橫跨了 2.1.277 的語意變更。`acct == "call"` 不寫（跟舊列同義）；`usage_mark`
    不進帳本。"""
    db._dorossi_round_info_and_record(
        _with_iterations([{"type": "message", "input_tokens": 1,
                           "cache_read_input_tokens": 7000,
                           "cache_creation_input_tokens": 0}]),
        cli_command="", resumed_id=None, sid="sess-abc", cli_version="2.1.277")
    db._dorossi_round_info_and_record(
        _round(42.0, cr=9_000_000), cli_command="", resumed_id="sess-abc",
        sid="sess-abc", cli_version="2.1.277", baseline=None)
    first, second = _rows(ledger)
    assert first["ctx"] == 7001 and first["v"] == "2.1.277"
    assert "acct" not in first and "usage_mark" not in first
    assert second["acct"] == "base" and second["cost_usd"] == 0.0
    assert "ctx" not in second


# --------------------------------------------------------------------------
# bot 端的接線：基準要存回同一個槽、下一輪要讀出來交給後端
#
# 換算本身是純函式，但它只在「呼叫端每一輪都把 `usage_mark` 存回槽、下一輪又把它
# 讀出來當 `usage_baseline`」時才有意義。任何一段斷掉，2.1.277 上回傳的就又是工作
# 階段累計——沒有例外、沒有 log，只是花費觸發每一輪都過門檻。
# --------------------------------------------------------------------------
_BOT_SOURCE = Path(db.__file__).with_name("discord_bot.py")


def _bot_functions() -> dict:
    tree = ast.parse(_BOT_SOURCE.read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls(node, name):
    return [c for c in ast.walk(node) if isinstance(c, ast.Call)
            and (getattr(c.func, "id", None) == name
                 or getattr(c.func, "attr", None) == name)]


def test_every_resumed_backend_call_passes_its_baseline():
    """`_dorossi_via_claude_code(prompt, <非 None 的 session>, …)` 一律帶
    `usage_baseline=`；新工作階段（第二個引數是字面 None）不必。自走迴圈走
    `invoke(prompt, stored_id, **common)`，所以另外釘 `common.update(...)`。"""
    fns = _bot_functions()
    resumed = {}
    for fn in fns.values():
        for call in _calls(fn, "_dorossi_via_claude_code"):
            second = call.args[1] if len(call.args) > 1 else None
            if isinstance(second, ast.Constant) and second.value is None:
                continue
            resumed[call.lineno] = call     # 巢狀函式會被走到兩次，用行號去重
    resumed = list(resumed.values())
    assert len(resumed) >= 2, f"只抽到 {len(resumed)} 個 resume 呼叫——抽取器壞了"
    for call in resumed:
        assert "usage_baseline" in {kw.arg for kw in call.keywords}, (
            f"第 {call.lineno} 行 resume 呼叫沒帶 usage_baseline——2.1.277 上這一輪的"
            "金額會是整個工作階段的累計")
    updates = [c for c in _calls(fns["_dorossi_loop_one_round"], "update")]
    assert any("usage_baseline" in {kw.arg for kw in c.keywords} for c in updates), (
        "自走迴圈的 common 參數沒帶 usage_baseline")


def test_every_save_after_a_backend_call_persists_the_mark():
    """三條「後端答完、把推進後的 id 存回槽」的路都要一起存 `usage_mark`；兩個快照
    都要把它讀出來。用量上限／暫時性故障那兩條存的是例外帶回的 id、沒有 info，刻意
    不在名單裡。"""
    fns = _bot_functions()
    for name in ("_save_mut", "_compact_save_mut", "_loop_save_mut"):
        assert name in fns, f"{name} 改名了——這支守門要跟著改"
        calls = _calls(fns[name], "_dorossi_persist_advance")
        assert calls, f"{name} 不再呼叫 _dorossi_persist_advance"
        for call in calls:
            assert "usage_mark" in {kw.arg for kw in call.keywords}, (
                f"{name}（第 {call.lineno} 行）沒把 usage_mark 存回槽")
    for name in ("_prep_mut", "_snap_mut"):
        keys = {k.value for d in ast.walk(fns[name]) if isinstance(d, ast.Dict)
                for k in d.keys if isinstance(k, ast.Constant)}
        assert "cc_usage_mark" in keys, f"{name} 的快照沒有讀出 cc_usage_mark"


def test_the_mark_is_stored_in_the_slot_and_dropped_on_reset():
    import discord_bot as b
    state: dict = {}
    rec = b._dorossi_user_record(state, "u1")
    s1 = b._dorossi_new_session(rec)
    mark = {"sid": "cc-1", "mode": "cumulative", "cost": 0.5, "tok": "model",
            "in": 1, "cr": 2, "cc": 3, "out": 4}
    assert b._dorossi_persist_advance(state, "u1", s1, new_sid="cc-1",
                                      usage_mark=mark)
    assert rec["sessions"][s1]["cc_usage_mark"] == mark
    # 沒帶 mark（用量上限那條）不得把既有的基準清掉。
    assert b._dorossi_persist_advance(state, "u1", s1, new_sid="cc-1")
    assert rec["sessions"][s1]["cc_usage_mark"] == mark
    db._dorossi_reset_session(rec["sessions"][s1])
    assert "cc_usage_mark" not in rec["sessions"][s1]


def test_new_ledger_rows_still_split_for_the_chart():
    """圖表讀取端（保留的 `_token_record_split`）看到多出來的欄位要照舊分桶。"""
    import discord_bot as b
    row = {"ts": 1.0, "in": 10, "cr": 6500, "cc": 80, "out": 30, "cost_usd": 0.001,
           "ctx": 6590, "acct": "delta", "v": "2.1.277"}
    split = b._token_record_split(row)
    assert (split["fresh"], split["cr"], split["cc"], split["out"]) == \
        (10, 6500, 80, 30)
