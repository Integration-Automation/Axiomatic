"""`verify_dorossi_cli.py` 的單元測試——**不打真的 CLI**。

那支驗證工具本身要花錢、要登入，所以只能手動跑；這裡釘的是它**判斷對不對**，用的全是
假串流：

* 結論行／結束碼的契約（比照 `verify_browser`：OK=0、FAIL=1、SKIP=3，SKIP 非零、不撞
  argparse 的 2，永遠單行）；找不到 CLI 是 SKIP，而且一個行程都不起。
* 四項檢查各自的通過／失敗條件，每一道條件都有「只踩它一道」的輸入——尤其是三個警報：
  純聊天的工具列表不是空的、像 bare 模式、每次叫用的數字被算成累計。
* argv 與 bot 是**同一份**：驗證工具送出去的 argv 跟 `_dorossi_via_claude_code` 真的會
  exec 的逐元素相同，而且兩邊都經過 `_dorossi_cc_argv`（任何一邊自己再組一份就紅）。
* 不碰正式狀態：帳本在 repo 底下時 `_Call.finish` 拒絕計帳；工作目錄不准落在 repo 裡。
* 端到端：用一個照 argv 回應的假 CLI 跑完整的 `main()`，兩種總額語意（每次叫用／累計）
  都要 OK——累計那組只有在基準真的一輪一輪往下傳時才會是 `delta`。
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import dorossi_backend as db  # noqa: E402
import verify_dorossi_cli as v  # noqa: E402

PKG_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "axiomatic"
VERIFY_SOURCE = PKG_ROOT / "verify_dorossi_cli.py"
BACKEND_SOURCE = PKG_ROOT / "dorossi_backend.py"
# 卡住的測試比紅的測試更糟：每一個 asyncio.run 都包牆鐘上限。
TEST_DEADLINE = 20.0


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """帳本／工作階段檔導到 tmp，而且**先登記還原**：`isolate_bot_state` 直接改模組
    全域，不先用 monkeypatch 記下原值的話，這裡的 tmp 路徑會漏到後面的測試去。"""
    monkeypatch.setattr(db, "DOROSSI_USAGE_FILE", db.DOROSSI_USAGE_FILE)
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", db.DOROSSI_SESSION_FILE)
    monkeypatch.setattr(db, "_DOROSSI_REAP_TIMEOUT_SEC", 1.0)
    v.isolate_bot_state(tmp_path / "state")


def _emit(verdict, checks, reason=""):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = v._emit(verdict, checks, reason)
    return code, buffer.getvalue()


# ---------------------------------------------------------------------------
# 假串流
# ---------------------------------------------------------------------------
def _j(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False)


def _init(version="2.1.276", *, tools=(), memory=True, key_source="none",
          sid="s1") -> str:
    ev = {"type": "system", "subtype": "init", "session_id": sid,
          "claude_code_version": version, "tools": list(tools),
          "apiKeySource": key_source}
    if memory:
        ev["memory_paths"] = {"auto": "C:/x/memory/"}
    return _j(ev)


def _text(text: str) -> list:
    half = max(1, len(text) // 2)
    return [
        _j({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text"}}}),
        _j({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text[:half]}}}),
        _j({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text[half:]}}}),
    ]


def _rate(status="allowed", reset=1789792200) -> str:
    info = {"status": status, "rateLimitType": "five_hour"}
    if reset is not None:
        info["resetsAt"] = reset
    return _j({"type": "rate_limit_event", "rate_limit_info": info})


def _tool_use(tool_id: str) -> str:
    return _j({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": "Glob", "input": {}}]}})


def _bg(ids) -> str:
    return _j({"type": "system", "subtype": "background_tasks_changed",
               "session_id": "s1",
               "tasks": [{"task_id": i, "task_type": "local_bash"} for i in ids]})


def _result(answer="PONG", *, sid="s1", cost=0.01, usage=(10, 5000, 1000, 40),
            model=None, iterations="last", is_error=False, subtype="success") -> str:
    """`usage`／`model` 都是 (in, cr, cc, out)。`iterations="last"` ＝最後一次呼叫就是
    整個 usage（沒有工具的單次呼叫）；給 tuple 就用它；None ＝整個鍵不存在。"""
    fresh, cread, ccreate, out = usage
    top = {"input_tokens": fresh, "cache_read_input_tokens": cread,
           "cache_creation_input_tokens": ccreate, "output_tokens": out}
    if iterations == "last":
        top["iterations"] = [dict(top, type="message")]
    elif iterations is not None:
        i_in, i_cr, i_cc, i_out = iterations
        top["iterations"] = [{"input_tokens": i_in, "cache_read_input_tokens": i_cr,
                              "cache_creation_input_tokens": i_cc,
                              "output_tokens": i_out, "type": "message"}]
    ev = {"type": "result", "subtype": subtype, "is_error": is_error,
          "session_id": sid, "result": answer, "total_cost_usd": cost, "usage": top}
    if model is not None:
        m_in, m_cr, m_cc, m_out = model
        ev["modelUsage"] = {"m": {"inputTokens": m_in, "cacheReadInputTokens": m_cr,
                                  "cacheCreationInputTokens": m_cc,
                                  "outputTokens": m_out, "costUSD": cost}}
    return _j(ev)


def _chat_stream(version="2.1.276", answer="PONG", **result_kw) -> list:
    return [_init(version), _rate(), *_text(answer), _result(answer, **result_kw)]


def _call(lines, *, label="t", prompt="p", session_id=None, baseline=None, rc=0,
          err="", kill_reason=None) -> v._Call:
    call = v._Call(label, prompt, session_id, baseline)
    for line in lines:
        call.feed(line)
    call.state.kill_reason = kill_reason
    call.finish(rc, err, 180.0)
    return call


# 2026-09-19 實測的形狀（本機 2.1.276 與隔離的 2.1.277）改寫成一組三次純聊天＋一次工具
# 回合。PER_CALL[k] ＝第 k 次叫用「自己」的量。
_PER_CALL = [
    # (cost, top usage, modelUsage)
    (0.0204, (10, 5234, 1143, 45), (914, 5234, 1143, 57)),
    (0.0101, (8, 6300, 200, 20), (900, 6300, 200, 30)),
    (0.0027, (8, 6500, 150, 20), (900, 6500, 150, 30)),
]
_TOOL_PER_CALL = (0.0340, (30, 20000, 1000, 300), (930, 20000, 1000, 310))
_TOOL_LAST = (8, 7000, 200, 60)


def _cumulative(rows):
    """把每次叫用的量累加成 2.1.277 起 resume 回報的那種工作階段累計。"""
    total_cost, total = 0.0, (0, 0, 0, 0)
    out = []
    for cost, usage, model in rows:
        total_cost += cost
        total = tuple(a + b for a, b in zip(total, model))
        out.append((round(total_cost, 6), usage, total))
    return out


def _session(version: str, *, cumulative_raw: bool, chain=True, tool_iterations="set",
             tool_uses=3):
    """跑一整組（3 次純聊天＋1 次工具回合），照 bot 的做法把基準一輪一輪往下傳。"""
    rows = list(_PER_CALL) + [_TOOL_PER_CALL]
    reported = _cumulative(rows) if cumulative_raw else rows
    calls, sid, mark = [], None, None
    for index, (cost, usage, model) in enumerate(reported):
        is_tool = index == len(rows) - 1
        iterations = "last"
        if is_tool:
            iterations = _TOOL_LAST if tool_iterations == "set" else tool_iterations
        lines = [_init(version, tools=["Glob"] if is_tool else [])]
        lines += [_tool_use(f"t{n}") for n in range(tool_uses if is_tool else 0)]
        lines += [_rate(), *_text("DONE" if is_tool else "OK"),
                  _result("DONE" if is_tool else "OK", cost=cost, usage=usage,
                          model=model, iterations=iterations)]
        call = _call(lines, label=f"c{index + 1}", session_id=sid,
                     baseline=mark if chain else None)
        calls.append(call)
        sid = call.state.sid
        mark = call.info.get("usage_mark") if call.info else None
    return calls[:-1], calls[-1]


# ---------------------------------------------------------------------------
# 結論行與結束碼
# ---------------------------------------------------------------------------
def test_the_three_verdicts_are_mutually_exclusive():
    codes, words = [], []
    for verdict in ("OK", "FAIL", "SKIP"):
        code, out = _emit(verdict, 4, "reason" if verdict != "OK" else "")
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 1, out
        assert lines[0].startswith(v.RESULT_PREFIX + " "), lines[0]
        codes.append(code)
        words.append(lines[0].split()[1])
    assert codes == [v.EXIT_OK, v.EXIT_FAIL, v.EXIT_SKIP] == [0, 1, 3]
    assert words == ["OK", "FAIL", "SKIP"]


def test_skip_is_non_zero_and_not_the_argparse_code():
    """沒驗到不能回報成功；2 是 argparse 自己的碼，而那條路一行結論都不印。"""
    assert v.EXIT_SKIP not in (0, 2, v.EXIT_FAIL)


def test_the_result_line_names_the_check_count_and_stays_one_line():
    code, out = _emit("FAIL", 4, "failed: 1,4\nsecond line\n" + "x" * 500)
    assert code == 1
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, out
    assert lines[0].startswith("VERIFY-DOROSSI-CLI: FAIL (4 checks) failed: 1,4"), lines[0]
    assert len(lines[0]) < 260
    _, ok = _emit("OK", 4)
    assert ok.strip() == "VERIFY-DOROSSI-CLI: OK (4 checks)"


def test_summarize_lists_the_failed_check_numbers():
    ok = v.CheckResult(1, "a", True, "fine")
    bad = v.CheckResult(4, "b", False, "broken")
    assert v.summarize([ok, ok]) == ("OK", "")
    assert v.summarize([ok, bad]) == ("FAIL", "failed: 4")


def test_a_missing_cli_is_a_skip_and_starts_nothing(monkeypatch, capsys):
    monkeypatch.setattr(v.shutil, "which", lambda _name: None)

    async def _must_not_spawn(*_a, **_k):
        raise AssertionError("找不到 CLI 時不該起任何行程")

    monkeypatch.setattr(v.asyncio, "create_subprocess_exec", _must_not_spawn)
    assert v.main([]) == v.EXIT_SKIP
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("VERIFY-DOROSSI-CLI: SKIP (0 checks)"), last


def test_a_nonexistent_exe_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as got:
        v.main(["--exe", str(tmp_path / "no-such-cli.exe")])
    assert got.value.code == 2


def test_main_without_arguments_does_not_read_the_test_runner_argv(monkeypatch):
    """`main()` 無參數＝沒有旗標，不是去解析 pytest 的命令列。"""
    monkeypatch.setattr(sys, "argv", ["pytest", "--definitely-not-a-flag"])
    monkeypatch.setattr(v.shutil, "which", lambda _name: None)
    assert v.main() == v.EXIT_SKIP


# ---------------------------------------------------------------------------
# 第 1 項：純聊天旗標
# ---------------------------------------------------------------------------
def test_a_healthy_pure_chat_stream_passes():
    result = v.check_pure_chat(_call(_chat_stream()))
    assert result.ok, result.lines()


def test_the_tool_list_canary_fires_when_pure_chat_exposes_tools():
    """CLI 哪天又把工具放進純聊天——這是那一天唯一會亮的燈。"""
    lines = [_init(tools=["ListAgents", "SendMessage"]), _rate(), *_text("PONG"),
             _result()]
    result = v.check_pure_chat(_call(lines))
    assert not result.ok
    assert "2 個工具" in result.summary and "ListAgents" in result.summary


@pytest.mark.parametrize("lines, words", [
    # 每一筆只踩一道，而且比對的字只出現在**那一道**的訊息裡——「預覽」「rate_limit_event」
    # 這種好幾道共用的字會讓拿掉其中一道的變異活下來（實測過兩次）。
    ([_init(), _rate(), _result()], "沒有累積出"),                   # 沒有 text_delta
    ([_init(), _rate(), *_text("PING"), _result()], "不一致"),       # 預覽與答案不同
    ([_init(), *_text("PONG"), _result()], "沒有 rate_limit_event"),
    ([_init(), _rate(reset=None), *_text("PONG"), _result()], "讀不出重設時刻"),
    ([_init(), _rate(status="weird"), *_text("PONG"), _result()], "詞彙"),
    ([_init(version="dev"), _rate(), *_text("PONG"), _result()], "claude_code_version"),
    ([_rate(), *_text("PONG"), _result()], "沒有 init 事件"),
    ([_init(), _rate(), *_text("PONG"), _result(answer="")], "沒有答案文字"),
    ([_init(sid=None), _rate(), *_text("PONG"), _result(sid=None)], "工作階段 id"),
    ([_init(), _rate(), *_text("PONG"),
      _result(is_error=True, subtype="error_during_execution")], "不是成功完成"),
])
def test_each_pure_chat_condition_fails_on_its_own(lines, words):
    result = v.check_pure_chat(_call(lines))
    assert not result.ok
    assert words in result.summary, result.lines()


def test_an_allowed_warning_status_is_still_a_known_status():
    """近似案例：`allowed_warning` 在已知詞彙裡，不能被當成認不得。"""
    lines = [_init(), _rate(status="allowed_warning"), *_text("PONG"), _result()]
    assert v.check_pure_chat(_call(lines)).ok


def test_a_failed_call_fails_the_pure_chat_check_with_the_bots_verdict():
    """rc=1、沒有 result：bot 的判定丟例外，這一項要講出型別，不能說「通過」。"""
    call = _call([_init()], rc=1, err="boom")
    result = v.check_pure_chat(call)
    assert not result.ok
    assert "RuntimeError" in result.summary, result.summary
    assert call.info is None, "判定沒通過就不該走計帳（bot 的順序也是這樣）"


def test_a_timed_out_call_fails_even_when_it_answered():
    """被自己的牆鐘砍掉時，bot 的判定會收下答案（看門狗出口）——但對驗證工具來說，
    串流沒有自己結束就是一個發現，不能報 OK。"""
    call = _call(_chat_stream(), kill_reason="hard", rc=1)
    assert call.verdict == "ok"
    assert not v.check_pure_chat(call).ok


# ---------------------------------------------------------------------------
# 第 2 項：逐次計帳
# ---------------------------------------------------------------------------
def test_per_call_totals_pass_as_call():
    chat, tool = _session("2.1.276", cumulative_raw=False)
    result = v.check_accounting(chat, tool)
    assert result.ok, result.lines()
    assert [c.info["acct"] for c in chat + [tool]] == ["call"] * 4


def test_cumulative_totals_pass_as_delta_when_the_baseline_is_carried():
    chat, tool = _session("2.1.277", cumulative_raw=True)
    result = v.check_accounting(chat, tool)
    assert result.ok, result.lines()
    assert [c.info["acct"] for c in chat + [tool]] == ["call", "delta", "delta", "delta"]


def test_cumulative_totals_read_as_per_call_are_caught():
    """版本說每次叫用、數字其實是累計（版本門檻錯了）→ token 遠大於頂層 usage。"""
    chat, tool = _session("2.1.276", cumulative_raw=True)
    result = v.check_accounting(chat, tool)
    assert not result.ok
    assert "累計" in result.summary, result.summary


def test_per_call_totals_read_as_cumulative_are_caught():
    """反方向：版本說累計、數字其實是每次叫用 → 差值是負的 → reset，標籤對不上。"""
    chat, tool = _session("2.1.277", cumulative_raw=False)
    result = v.check_accounting(chat, tool)
    assert not result.ok
    assert "reset" in result.summary, result.summary


def test_a_broken_baseline_chain_is_caught():
    """累計語意但基準沒往下傳 → bot 記 `base`、金額 0。驗證工具要紅，不然它就不是在
    驗 bot 的接線。"""
    chat, tool = _session("2.1.277", cumulative_raw=True, chain=False)
    result = v.check_accounting(chat, tool)
    assert not result.ok
    assert "base" in result.summary, result.summary


def test_growing_per_call_tokens_are_caught_even_inside_the_usage_slack():
    """「不會逐輪長大」那一道要自己擋得住：floor 很大（容差寬）而換算後逐輪倍增。"""
    rows = [
        (0.01, (10, 3000, 100, 10), (10, 3000, 100, 10)),
        (0.01, (10, 3000, 100, 10), (10, 3000, 100, 10)),
        (0.01, (10, 20000, 100, 10), (10, 20000, 100, 10)),
    ]
    calls, sid, mark = [], None, None
    for cost, usage, model in rows:
        call = _call([_init(), _rate(), *_text("OK"),
                      _result("OK", cost=cost, usage=usage, model=model)],
                     session_id=sid, baseline=mark)
        calls.append(call)
        sid, mark = call.state.sid, call.info["usage_mark"]
    tool = _session("2.1.276", cumulative_raw=False)[1]
    result = v.check_accounting(calls, tool)
    assert not result.ok
    assert "長大" in " ".join([result.summary, *result.details])


def test_a_resumed_call_that_costs_nothing_is_caught():
    """標籤對、token 對，只有金額是 0——那一道要自己擋得住（累計基準遺失時 bot 就記 0，
    但那種情況標籤也會錯，會把這一道遮住，所以另外用「每次叫用、回報 0」單獨踩它）。"""
    first = _call(_chat_stream(cost=0.02))
    second = _call(_chat_stream(cost=0.0), session_id=first.state.sid,
                   baseline=first.info["usage_mark"])
    tool = _session("2.1.276", cumulative_raw=False)[1]
    result = v.check_accounting([first, second], tool)
    assert not result.ok
    assert "金額不是正數" in result.summary, result.lines()


def test_an_infinite_cost_is_not_a_positive_amount():
    """`inf > 0` 為真：只比大小的話，一個壞掉的無限大金額會被當成正數放行。bot 的換算
    本身會把非有限值擋成 0，所以這裡直接改 `info` 去問檢查自己那一層。"""
    chat, tool = _session("2.1.276", cumulative_raw=False)
    chat[1].info["cost_usd"] = float("inf")
    result = v.check_accounting(chat, tool)
    assert not result.ok
    assert "金額不是正數" in result.summary, result.lines()


def test_the_tool_round_needs_the_last_iteration():
    """`iterations` 讀不到 → bot 的壓縮觸發退回加總 → 每一輪都會壓縮（§8.71）。"""
    chat, tool = _session("2.1.276", cumulative_raw=False, tool_iterations=None)
    result = v.check_accounting(chat, tool)
    assert not result.ok
    assert "iterations" in result.summary, result.summary


def test_a_last_iteration_that_equals_the_sum_is_caught():
    """有工具呼叫，最後一次呼叫的脈絡卻等於整次加總——量到的是加總。"""
    summed = _TOOL_PER_CALL[1]
    chat, tool = _session("2.1.276", cumulative_raw=False,
                          tool_iterations=(summed[0], summed[1], summed[2], 0))
    result = v.check_accounting(chat, tool)
    assert not result.ok
    assert "加總" in result.summary, result.summary


def test_a_tool_round_without_any_tool_call_cannot_compare():
    chat, tool = _session("2.1.276", cumulative_raw=False, tool_uses=0)
    result = v.check_accounting(chat, tool)
    assert not result.ok
    assert "工具都沒呼叫" in result.summary, result.summary


def test_accounting_without_a_first_session_fails_cleanly():
    first = _call([_init()], rc=1)
    result = v.check_accounting([first], None)
    assert not result.ok and "resume" in result.summary


def test_a_missing_tool_round_is_not_a_pass():
    chat, _tool = _session("2.1.276", cumulative_raw=False)
    assert not v.check_accounting(chat, None).ok


# ---------------------------------------------------------------------------
# 第 3 項：背景工作訊號
# ---------------------------------------------------------------------------
def _bg_stream(*, with_bg=True, answer_ok=True, tools=("Bash",)) -> list:
    lines = [_init(tools=tools), _rate(), _tool_use("t1")]
    if with_bg:
        lines.append(_bg(["b45nerqy6"]))
    lines += [*_text("STARTED"),
              _result("STARTED" if answer_ok else "boom", is_error=not answer_ok,
                      subtype="success" if answer_ok else "error_during_execution")]
    if with_bg:
        lines.append(_bg([]))       # 實測：result 之後約 5 秒 CLI 收掉背景 shell
    return lines


def test_a_background_task_seen_mid_stream_passes():
    call = _call(_bg_stream())
    result = v.check_background(call)
    assert result.ok, result.lines()
    assert call.max_background == 1 and not call.state.background_tasks


def test_no_background_signal_fails():
    result = v.check_background(_call(_bg_stream(with_bg=False)))
    assert not result.ok
    assert "background_tasks" in result.summary


def test_a_background_stream_without_a_successful_result_fails():
    """rc=0 而最後的 result 不是成功：bot 的判定放行（rc==0 那條只查用量上限），所以只有
    這一道擋得住它。用 rc=1 的話判定會先丟例外，把這一道遮住。"""
    call = _call(_bg_stream(answer_ok=False), rc=0)
    assert call.verdict == "ok"
    result = v.check_background(call)
    assert not result.ok
    assert "成功的 result" in result.summary, result.lines()


def test_a_background_stream_that_had_to_be_killed_fails():
    result = v.check_background(_call(_bg_stream(), kill_reason="hard", rc=1))
    assert not result.ok
    assert "砍掉" in result.summary


def test_a_missing_bash_tool_is_named():
    result = v.check_background(_call(_bg_stream(tools=())))
    assert not result.ok
    assert any("Bash" in text for text in [result.summary, *result.details])


# ---------------------------------------------------------------------------
# 第 4 項：不是 bare 模式（判準來自實測，見模組說明的表）
# ---------------------------------------------------------------------------
# 2026-09-19 本機 2.1.276、沒有設定 API key：一般模式與 `--bare` 的 init／result。
_MEASURED_NORMAL_INIT = {"type": "system", "subtype": "init", "apiKeySource": "none",
                         "memory_paths": {"auto": "C:/Users/x/.claude/projects/y/memory/"},
                         "tools": [], "claude_code_version": "2.1.276"}
_MEASURED_BARE_INIT = {"type": "system", "subtype": "init", "apiKeySource": "none",
                       "tools": [], "claude_code_version": "2.1.276"}
_MEASURED_BARE_RESULT = {"type": "result", "subtype": "success", "is_error": True,
                         "api_error_status": None, "terminal_reason": "api_error",
                         "result": "Not logged in \u00b7 Please run /login",
                         "total_cost_usd": 0}


def test_the_measured_normal_session_is_not_bare():
    assert v.bare_mode_findings(_MEASURED_NORMAL_INIT, {"is_error": False}) == []


def test_the_measured_bare_session_is_named_as_bare():
    findings = v.bare_mode_findings(_MEASURED_BARE_INIT, _MEASURED_BARE_RESULT)
    assert len(findings) == 2, findings
    assert "memory_paths" in findings[0] and "登入" in findings[1]


@pytest.mark.parametrize("init, result, words", [
    # 每一筆只踩一道。
    (_MEASURED_BARE_INIT, {"is_error": False}, "memory_paths"),
    (dict(_MEASURED_NORMAL_INIT, apiKeySource="ANTHROPIC_API_KEY"), {"is_error": False},
     "ANTHROPIC_API_KEY"),
    (_MEASURED_NORMAL_INIT, _MEASURED_BARE_RESULT, "登入類"),
    (None, None, "init"),
])
def test_each_bare_signal_fires_on_its_own(init, result, words):
    findings = v.bare_mode_findings(init, result)
    assert len(findings) == 1, findings
    assert words in findings[0]


@pytest.mark.parametrize("result", [
    {"is_error": True, "result": "Overloaded"},                  # 錯誤，但不是登入類
    {"is_error": False, "result": "please run /login to fix it"},  # 成功答案剛好提到
])
def test_near_misses_are_not_bare(result):
    assert v.bare_mode_findings(_MEASURED_NORMAL_INIT, result) == []


def test_the_api_key_finding_prints_no_odd_value():
    """來源標籤形狀不對（混進換行、角括號）就整個不印——那個欄位哪天改成帶值也漏不出來。"""
    init = dict(_MEASURED_NORMAL_INIT, apiKeySource="ANTHROPIC_API_KEY\n<sk-secret>")
    findings = v.bare_mode_findings(init, {"is_error": False})
    assert len(findings) == 1
    assert "sk-secret" not in findings[0] and "\n" not in findings[0]
    assert "形狀不明" in findings[0]


@pytest.mark.parametrize("source", sorted(db._DOROSSI_CC_DROPPED_ENV)
                         + [name.lower() for name in sorted(db._DOROSSI_CC_DROPPED_ENV)])
def test_a_source_the_bot_strips_is_said_to_come_from_the_clis_own_settings(source):
    """bot 的子行程環境不帶這些變數（`_DOROSSI_CC_DROPPED_ENV`），所以 CLI 還說它用的是
    其中之一，值就一定來自 CLI 自己的設定。原本的「這個環境設了 ANTHROPIC_API_KEY」在
    builder 拿掉變數之後永遠不會出現（`api_key_in_env` 恆假），換成這一句。清單從 bot
    推出來，不在這裡抄一份。"""
    findings = v.bare_mode_findings(dict(_MEASURED_NORMAL_INIT, apiKeySource=source),
                                    {"is_error": False})
    assert len(findings) == 1 and "來自 CLI 自己的設定" in findings[0], findings


def test_a_source_the_bot_does_not_strip_gets_no_such_claim():
    """近似案例：`apiKeyHelper` 是 CLI 設定檔裡的東西，不是 bot 拿掉的變數——照樣是警報，
    但不能說成「bot 不交給子行程」。"""
    plain = v.bare_mode_findings(dict(_MEASURED_NORMAL_INIT, apiKeySource="apiKeyHelper"),
                                 {"is_error": False})
    assert "apiKeyHelper" in plain[0] and "來自 CLI 自己的設定" not in plain[0]


# 2026-09-19 實測的兩句「沒有可用的登入」（見 `dorossi_backend._dorossi_cc_auth_failure`
# 上方的表）與一句不相干的錯誤。驗證工具的第 4 項與 bot 的判定是**兩份實作**，各自的
# 測試都綠不代表它們對同一句話給同一個答案——這裡拿同一份語料問兩邊。
_AUTH_PARITY_CORPUS = [
    ({"type": "result", "subtype": "success", "is_error": True, "api_error_status": None,
      "terminal_reason": "api_error", "result": "Not logged in \u00b7 Please run /login"},
     True),
    ({"type": "result", "subtype": "success", "is_error": True, "api_error_status": None,
      "terminal_reason": "api_error",
      "result": "Failed to authenticate. API Error: 401 API key is invalid."}, True),
    ({"type": "result", "subtype": "success", "is_error": True, "api_error_status": None,
      "terminal_reason": "api_error", "result": "API Error: Overloaded"}, False),
]


def test_the_tool_and_the_bot_agree_on_the_measured_sign_in_failures():
    for result_ev, expected in _AUTH_PARITY_CORPUS:
        tool = any("登入類" in f for f in v.bare_mode_findings(_MEASURED_NORMAL_INIT,
                                                               result_ev))
        bot = db._dorossi_cc_auth_failure(result_ev) is not None
        assert (tool, bot) == (expected, expected), (result_ev["result"], tool, bot)


def test_the_parity_corpus_actually_bites():
    """正面對照：語料兩種答案都要有，否則「兩邊一致」可能只是兩邊都什麼都沒認出來。"""
    assert {expected for _ev, expected in _AUTH_PARITY_CORPUS} == {True, False}


def test_the_bare_check_reads_the_first_calls_init():
    call = _call([json.dumps(_MEASURED_BARE_INIT), json.dumps(_MEASURED_BARE_RESULT)],
                 rc=1)
    result = v.check_bare_mode(call)
    assert not result.ok
    assert result.summary.startswith("這個工作階段像是 --bare 模式")
    assert v.check_bare_mode(_call(_chat_stream())).ok


# ---------------------------------------------------------------------------
# 印出來的東西：cp950 印得出來、單行
# ---------------------------------------------------------------------------
def _all_rendered_lines() -> list:
    chat, tool = _session("2.1.277", cumulative_raw=True)
    bad_chat, bad_tool = _session("2.1.276", cumulative_raw=True, tool_iterations=None)
    results = [
        v.check_pure_chat(_call(_chat_stream())),
        v.check_pure_chat(_call([_init(tools=["X"] * 9)], rc=1, err="\u2603 boom")),
        v.check_accounting(chat, tool),
        v.check_accounting(bad_chat, bad_tool),
        v.check_accounting([_call([_init()], rc=1)], None),
        v.check_background(_call(_bg_stream())),
        v.check_background(_call(_bg_stream(with_bg=False), kill_reason="hard", rc=1)),
        v.check_bare_mode(_call(_chat_stream())),
        v.check_bare_mode(_call([json.dumps(_MEASURED_BARE_INIT),
                                 json.dumps(_MEASURED_BARE_RESULT)], rc=1)),
    ]
    lines = []
    for result in results:
        lines += result.lines()
    return lines


def test_every_report_line_is_encodable_on_this_console():
    """外來的值（CLI 的錯誤文字、工具名）一律經過 `_console_safe`；本檔自己的字面值由
    `test_text_encoding` 靜態管。這支量的是**組起來之後**的整行。"""
    lines = _all_rendered_lines()
    assert len(lines) > 20
    for line in lines:
        line.encode("cp950")
        assert "\n" not in line


def test_console_safe_turns_foreign_text_into_one_ascii_line():
    out = v._console_safe("a\u00b7b\n\u2603 " + "y" * 400)
    out.encode("ascii")
    assert "\n" not in out and len(out) < 200


def test_every_string_literal_in_the_tool_is_cp950_encodable():
    tree = ast.parse(VERIFY_SOURCE.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node.value.encode("cp950")
            except UnicodeEncodeError:
                bad.append(node.lineno)
    assert not bad, f"這幾行的字串 cp950 編不出來：{bad}"


# ---------------------------------------------------------------------------
# argv：驗證工具送的就是 bot 會送的那一份
# ---------------------------------------------------------------------------
class _ArgvCaptured(Exception):
    def __init__(self, args):
        super().__init__("captured")
        self.args_seen = list(args)


def _bot_argv(monkeypatch, tools, **kwargs) -> list:
    """`_dorossi_via_claude_code` 真的會 exec 的 argv（假 exec 收到就丟哨符例外）。"""
    monkeypatch.setattr(db._shutil, "which", lambda _n: r"C:\fake\claude.exe")
    monkeypatch.setattr(db, "DOROSSI_CC_TOOLS", tools)

    async def _fake_exec(*args, **_kw):
        raise _ArgvCaptured(args)

    monkeypatch.setattr(db.asyncio, "create_subprocess_exec", _fake_exec)
    try:
        asyncio.run(db._dorossi_via_claude_code(
            "q", kwargs.pop("session_id", None), workdir=str(PKG_ROOT), **kwargs))
    except _ArgvCaptured as captured:
        return captured.args_seen
    raise AssertionError("假的 create_subprocess_exec 沒被呼叫到")


@pytest.mark.parametrize("session_id", [None, "sess-abc"])
def test_the_pure_chat_argv_is_the_bots_own(monkeypatch, session_id):
    exe = r"C:\fake\claude.exe"
    bot = _bot_argv(monkeypatch, "off", session_id=session_id, model=v._model())
    assert v.pure_chat_argv(exe, session_id) == bot


@pytest.mark.parametrize("session_id", [None, "sess-abc"])
def test_the_tool_round_argv_is_the_full_mode_argv_plus_a_narrowed_allowlist(
        monkeypatch, session_id):
    exe = r"C:\fake\claude.exe"
    bot = _bot_argv(monkeypatch, "full", session_id=session_id, model=v._model())
    mine = v.tool_round_argv(exe, session_id, "Glob")
    assert mine[:len(bot)] == bot
    assert mine[len(bot):] == ["--tools", "Glob", "--allowedTools", "Glob"]


@pytest.mark.parametrize("tools", ["off", "full", "", None])
@pytest.mark.parametrize("session_id", [None, "sess-abc"])
@pytest.mark.parametrize("extras", [
    {},
    {"effort": "high", "max_budget_usd": 1.5, "model": "sonnet",
     "extra_dir": r"D:\extra dir", "loop_system_guidance": "rules"},
])
def test_the_builder_is_what_the_bot_executes(monkeypatch, tools, session_id, extras):
    """`_dorossi_cc_argv` 與 `_dorossi_via_claude_code` 實際 exec 的 argv 逐元素相同。

    這支在 bot 走 builder 時必然成立——它存在的理由是**以後**：有人把 argv 的組法搬回
    `_dorossi_via_claude_code` 裡、改了其中一邊，驗證工具驗的就變成一份副本。"""
    bot = _bot_argv(monkeypatch, tools, session_id=session_id, **dict(extras))
    built = db._dorossi_cc_argv(r"C:\fake\claude.exe", session_id=session_id,
                                **dict(extras))
    assert built == bot


def test_the_builder_reads_the_tool_mode_at_call_time(monkeypatch):
    """`tools_mode` 省略 ＝ 呼叫當下讀模組全域（跟抽出來之前一樣）；明給就以明給的為準。"""
    monkeypatch.setattr(db, "DOROSSI_CC_TOOLS", "full")
    assert "bypassPermissions" in db._dorossi_cc_argv("x")
    assert "bypassPermissions" not in db._dorossi_cc_argv("x", tools_mode="off")
    monkeypatch.setattr(db, "DOROSSI_CC_TOOLS", "off")
    off = db._dorossi_cc_argv("x")
    assert "--disallowedTools" in off and "bypassPermissions" not in off
    assert off[off.index("--tools") + 1] == ""
    assert "bypassPermissions" in db._dorossi_cc_argv("x", tools_mode="full")


def _function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"找不到 {name}")


def _string_constants(node) -> set:
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def test_the_bot_invocation_builds_its_argv_only_through_the_builder():
    """`_dorossi_via_claude_code` 呼叫 `_dorossi_cc_argv`、把它的結果 `*` 展開交給 exec，
    而且本體裡沒有任何一個 builder 擁有的旗標字面值（＝沒有第二份）。"""
    tree = ast.parse(BACKEND_SOURCE.read_text(encoding="utf-8"))
    invoke = _function(tree, "_dorossi_via_claude_code")
    builder = _function(tree, "_dorossi_cc_argv")
    owned = {s for s in _string_constants(builder) if s.startswith("--")}
    assert {"--tools", "--disallowedTools", "--permission-mode", "--resume",
            "--output-format"} <= owned, owned
    assert not owned & _string_constants(invoke), (
        "`_dorossi_via_claude_code` 又自己寫了 builder 擁有的旗標："
        f"{sorted(owned & _string_constants(invoke))}")
    targets = set()
    for node in ast.walk(invoke):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_dorossi_cc_argv"):
            targets.update({t.id for t in node.targets if isinstance(t, ast.Name)})
    assert targets, "`_dorossi_via_claude_code` 沒有用 `_dorossi_cc_argv` 組 argv"
    starred = set()
    for node in ast.walk(invoke):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_subprocess_exec"):
            starred |= {a.value.id for a in node.args
                        if isinstance(a, ast.Starred) and isinstance(a.value, ast.Name)}
    assert targets & starred, "exec 收到的不是 builder 組出來的那一份 argv"


def test_the_tool_never_writes_its_own_argv_or_parser():
    """驗證工具不准自己組 bot 的旗標、也不准自己讀 bot 已經在讀的欄位——那樣驗到的是
    副本。旗標清單從 builder 推出來（`--tools` 例外：工具回合在後面接一段收斂用的白名單）。"""
    tree = ast.parse(VERIFY_SOURCE.read_text(encoding="utf-8"))
    builder = _function(ast.parse(BACKEND_SOURCE.read_text(encoding="utf-8")),
                        "_dorossi_cc_argv")
    owned = {s for s in _string_constants(builder) if s.startswith("--")} - {"--tools"}
    mine = _string_constants(tree)
    assert not owned & mine, f"驗證工具自己寫了 bot 的旗標：{sorted(owned & mine)}"
    bot_fields = {"total_cost_usd", "modelUsage", "iterations", "text_delta",
                  "rate_limit_info", "background_tasks_changed", "resetsAt"}
    assert not bot_fields & mine, (
        f"驗證工具自己讀了 bot 在讀的欄位：{sorted(bot_fields & mine)}——交給 bot 的函式")


def test_the_tool_goes_through_the_bots_own_functions():
    tree = ast.parse(VERIFY_SOURCE.read_text(encoding="utf-8"))
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "db"}
    required = {"_ClaudeStreamState", "_claude_stream_verdict",
                "_dorossi_round_info_and_record", "_dorossi_context_tokens",
                "_dorossi_last_call_context", "_dorossi_cc_argv", "_dorossi_cc_child_env",
                "_dorossi_reap_proc", "_dorossi_drain_stderr", "_read_stream_all"}
    assert required <= used, sorted(required - used)


# ---------------------------------------------------------------------------
# 環境與正式狀態
# ---------------------------------------------------------------------------
def test_the_child_env_is_the_bots_minus_the_interactive_session(monkeypatch):
    """實測：正在跑的 bot 行程沒有任何 CLAUDE*／AI_AGENT 變數；互動式工作階段裡的殼帶
    著 11 個（其中一個會改子行程的思考力度）。拿掉它們，但背景工作等待上限那一個是
    bot 自己設的，要留著；其他變數照樣繼承——bot 也會繼承。API key 變數**不**繼承：
    bot 的 builder 把它拿掉（2026-09-19 起，見 `_DOROSSI_CC_DROPPED_ENV`），驗證工具
    以它為底，所以也看不到。"""
    for key, value in (("CLAUDECODE", "1"), ("CLAUDE_CODE_SESSION_ID", "s"),
                       ("CLAUDE_EFFORT", "max"), ("AI_AGENT", "x"),
                       ("ANTHROPIC_API_KEY", "k"), ("AXIOMATIC_KEEP", "p"),
                       (db._DOROSSI_CC_BG_WAIT_CEILING_ENV, "0")):
        monkeypatch.setenv(key, value)
    env = v.child_env(120.0)
    assert env[db._DOROSSI_CC_BG_WAIT_CEILING_ENV] == "120000", (
        "父行程帶的值（0＝無上限）不能贏過這一輪的上限")
    assert env["AXIOMATIC_KEEP"] == "p" and "ANTHROPIC_API_KEY" not in env
    leftover = sorted(k for k in env
                      if k.upper().startswith("CLAUDE") or k.upper() == "AI_AGENT")
    assert leftover == [db._DOROSSI_CC_BG_WAIT_CEILING_ENV], leftover
    assert "DISABLE_AUTOUPDATER" not in env
    assert v.child_env(120.0, pinned_exe=True)["DISABLE_AUTOUPDATER"] == "1"


def test_the_child_env_starts_from_the_bots_env_builder(monkeypatch):
    seen = []

    def _fake(hard):
        seen.append(hard)
        return {"FROM_BOT": "1", db._DOROSSI_CC_BG_WAIT_CEILING_ENV: "5",
                "CLAUDECODE": "1"}

    monkeypatch.setattr(db, "_dorossi_cc_child_env", _fake)
    env = v.child_env(99.0)
    assert seen == [99.0]
    assert env == {"FROM_BOT": "1", db._DOROSSI_CC_BG_WAIT_CEILING_ENV: "5"}


def test_the_ledger_is_redirected_and_a_finished_call_writes_there(tmp_path):
    assert Path(db.DOROSSI_USAGE_FILE).parent == tmp_path / "state"
    _call(_chat_stream())
    rows = db._dorossi_read_usage(10)
    assert len(rows) == 1 and rows[0]["cost_usd"] == pytest.approx(0.01)


def test_accounting_refuses_a_ledger_inside_the_repo(monkeypatch):
    """帳本指回 repo 底下（還沒導走的狀態）→ `finish` 拒絕計帳。

    ⚠️ 目標刻意放在一個**不存在的子目錄**裡：守衛若壞了，bot 的帳本寫入是 fail-soft 的
    `open(..., "a")`，目錄不存在就寫不進去——測試照樣變紅，但 repo 裡不會多一個檔。
    （2026-09-19 手動變異時第一版放在 repo 根目錄，拿掉守衛的那個變異就真的在 repo 根
    寫了一個 `.ndjson`，而且因為副檔名被忽略，`git status` 看不到它。）"""
    target = Path(db.PROJECT_ROOT) / "no_such_dir_verify_dorossi_cli" / "ledger.ndjson"
    monkeypatch.setattr(db, "DOROSSI_USAGE_FILE", target)
    call = v._Call("t", "p")
    for line in _chat_stream():
        call.feed(line)
    with pytest.raises(RuntimeError):
        call.finish(0, "", 180.0)
    assert not target.parent.exists()


@pytest.fixture
def _no_repo_writes(monkeypatch):
    """下面兩支驗「拒絕落在 repo 裡」。守衛若壞了，被測程式會真的在 repo 裡建目錄／寫檔
    ——所以先把那兩個動作換成「落在 repo 裡就丟 AssertionError」：測試照樣變紅，但樹上
    一個位元組都不會多。"""
    real_mkdir, real_write = Path.mkdir, Path.write_text

    def _mkdir(self, *args, **kwargs):
        if v._inside_repo(self):
            raise AssertionError(f"tried to create a directory inside the repo: {self.name}")
        return real_mkdir(self, *args, **kwargs)

    def _write(self, *args, **kwargs):
        if v._inside_repo(self):
            raise AssertionError(f"tried to write a file inside the repo: {self.name}")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _mkdir)
    monkeypatch.setattr(Path, "write_text", _write)


def test_isolation_refuses_a_state_dir_inside_the_repo(tmp_path, _no_repo_writes):
    """先驗再動：目標在 repo 底下時連目錄都不建、模組全域也不改。"""
    before = db.DOROSSI_USAGE_FILE
    target = Path(db.PROJECT_ROOT) / "verify_state_never_created"
    with pytest.raises(RuntimeError):
        v.isolate_bot_state(target)
    assert not target.exists()
    assert db.DOROSSI_USAGE_FILE == before


def test_the_scratch_dir_is_outside_the_repo_and_holds_the_glob_files(tmp_path,
                                                                        _no_repo_writes):
    path = v.scratch_dir(tmp_path)
    assert sorted(p.name for p in path.iterdir()) == sorted(v._GLOB_FILES)
    with pytest.raises(RuntimeError):
        v.scratch_dir(Path(db.PROJECT_ROOT))
    assert not (Path(db.PROJECT_ROOT) / v._SCRATCH_NAME).exists()


# ---------------------------------------------------------------------------
# 讀取迴圈與端到端（假 CLI）
# ---------------------------------------------------------------------------
class _Stream:
    def __init__(self, lines=(), *, hang=False):
        self._lines = [(line + "\n").encode("utf-8") for line in lines]
        self._hang = hang

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._hang:
            await asyncio.get_running_loop().create_future()
        return b""

    async def read(self):
        return b""


class _Stdin:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        pass


class _Proc:
    def __init__(self, lines, *, rc=0, hang=False):
        self.stdin = _Stdin()
        self.stdout = _Stream(lines, hang=hang)
        self.stderr = _Stream()
        self.returncode = None if hang else rc
        self.kills = 0

    def kill(self):
        self.kills += 1
        self.returncode = 1

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=TEST_DEADLINE))


def test_the_read_loop_feeds_the_bot_and_sends_the_prompt(monkeypatch, tmp_path):
    proc = _Proc(_chat_stream())
    seen = {}

    async def _fake_exec(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return proc

    monkeypatch.setattr(v.asyncio, "create_subprocess_exec", _fake_exec)
    call = _run(v._run_call(v._Call("t", "hello"), ["x", "-p"], tmp_path, {"E": "1"}))
    assert proc.stdin.data == b"hello"
    assert seen["args"] == ("x", "-p")
    assert seen["kwargs"]["cwd"] == str(tmp_path) and seen["kwargs"]["env"] == {"E": "1"}
    assert call.verdict == "ok" and call.state.answer == "PONG" and call.info is not None


def test_the_read_loop_is_bounded(monkeypatch, tmp_path):
    """stdout 永遠不結束 → 牆鐘到了就砍，回來的呼叫標成被砍掉（不是掛住）。"""
    proc = _Proc([_init()], hang=True)

    async def _fake_exec(*_a, **_k):
        return proc

    monkeypatch.setattr(v.asyncio, "create_subprocess_exec", _fake_exec)
    call = _run(v._run_call(v._Call("t", "p"), ["x"], tmp_path, {}, timeout_sec=0.3))
    assert call.timed_out and proc.kills >= 1


class _FakeCli:
    """照 argv 回應的假 CLI：新開／resume／工具回合各回一種串流，總額照 `cumulative`
    決定是每次叫用還是工作階段累計（2.1.276／2.1.277 兩種語意）。"""

    def __init__(self, version: str, *, cumulative: bool, pure_tools=()):
        self.version = version
        self.cumulative = cumulative
        self.pure_tools = list(pure_tools)
        self.calls = []
        self._total_cost = 0.0
        self._total = (0, 0, 0, 0)

    def _reported(self, cost, model):
        if not self.cumulative:
            self._total_cost, self._total = cost, model
        else:
            self._total_cost += cost
            self._total = tuple(a + b for a, b in zip(self._total, model))
        return round(self._total_cost, 6), self._total

    async def exec(self, *args, **kwargs):
        argv = list(args)
        self.calls.append((argv, kwargs))
        tools = argv[argv.index("--tools") + 1] if "--tools" in argv else None
        resumed = "--resume" in argv
        sid = argv[argv.index("--resume") + 1] if resumed else f"s{len(self.calls)}"
        if not resumed:
            self._total_cost, self._total = 0.0, (0, 0, 0, 0)
        if tools == "Bash":
            return _Proc([_init(self.version, tools=["Bash"], sid=sid), _rate(),
                          _tool_use("t1"), _bg(["b1"]), *_text("STARTED"),
                          _result("STARTED", sid=sid), _bg([])])
        if tools == "Glob":
            cost, usage, model = _TOOL_PER_CALL
            cost, model = self._reported(cost, model)
            return _Proc([_init(self.version, tools=["Glob"], sid=sid), _rate(),
                          _tool_use("g1"), _tool_use("g2"), _tool_use("g3"),
                          *_text("DONE"),
                          _result("DONE", sid=sid, cost=cost, usage=usage, model=model,
                                  iterations=_TOOL_LAST)])
        index = sum(1 for a, _k in self.calls if "--tools" in a
                    and a[a.index("--tools") + 1] == "") - 1
        cost, usage, model = _PER_CALL[min(index, 2)]
        cost, model = self._reported(cost, model)
        return _Proc([_init(self.version, tools=self.pure_tools, sid=sid), _rate(),
                      *_text("OK"),
                      _result("OK", sid=sid, cost=cost, usage=usage, model=model)])


def _main_with(monkeypatch, tmp_path, cli: _FakeCli, argv=()):
    monkeypatch.setattr(v.shutil, "which", lambda _n: r"C:\fake\claude.exe")
    monkeypatch.setattr(v.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(v.asyncio, "create_subprocess_exec", cli.exec)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = v.main(list(argv))
    return code, buffer.getvalue()


@pytest.mark.parametrize("version, cumulative", [("2.1.276", False), ("2.1.277", True)])
def test_main_passes_on_both_totals_semantics(monkeypatch, tmp_path, version, cumulative):
    """累計那組會是 OK **只因為** `_verify` 把每一次的 `usage_mark` 交給下一次——漏傳
    的話 bot 記 `base`、金額 0，第 2 項會紅。"""
    cli = _FakeCli(version, cumulative=cumulative)
    code, out = _main_with(monkeypatch, tmp_path, cli)
    assert code == v.EXIT_OK, out
    assert out.strip().splitlines()[-1] == "VERIFY-DOROSSI-CLI: OK (4 checks)"
    assert len(cli.calls) == 5
    for argv, kwargs in cli.calls:
        env = kwargs["env"]
        assert not any(k.upper().startswith("CLAUDECODE") for k in env)
        assert env[db._DOROSSI_CC_BG_WAIT_CEILING_ENV] == str(int(v.CALL_TIMEOUT_SEC * 1000))
        assert "DISABLE_AUTOUPDATER" not in env
        cwd = Path(kwargs["cwd"]).resolve()
        assert Path(db.PROJECT_ROOT).resolve() not in cwd.parents
        assert argv[argv.index("--model") + 1] == v._model()
    resumes = [a[a.index("--resume") + 1] for a, _k in cli.calls if "--resume" in a]
    assert resumes == ["s1", "s1", "s1"], resumes
    for line in out.splitlines():
        line.encode("cp950")


def test_main_fails_and_names_the_check_when_pure_chat_lists_tools(monkeypatch, tmp_path):
    cli = _FakeCli("2.1.276", cumulative=False, pure_tools=["ListAgents"])
    code, out = _main_with(monkeypatch, tmp_path, cli)
    assert code == v.EXIT_FAIL
    assert out.strip().splitlines()[-1] == "VERIFY-DOROSSI-CLI: FAIL (4 checks) failed: 1"


def test_a_pinned_exe_turns_the_autoupdater_off(monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    cli = _FakeCli("2.1.277", cumulative=True)
    code, _out = _main_with(monkeypatch, tmp_path, cli, ["--exe", str(exe)])
    assert code == v.EXIT_OK
    assert all(k["env"].get("DISABLE_AUTOUPDATER") == "1" for _a, k in cli.calls)
    assert all(a[0] == str(exe) for a, _k in cli.calls)


def test_an_unexpected_error_still_prints_a_verdict(monkeypatch, tmp_path):
    async def _boom(*_a, **_k):
        raise OSError("spawn failed: C:\\secret\\path")

    monkeypatch.setattr(v.shutil, "which", lambda _n: r"C:\fake\claude.exe")
    monkeypatch.setattr(v.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(v.asyncio, "create_subprocess_exec", _boom)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = v.main([])
    last = buffer.getvalue().strip().splitlines()[-1]
    assert code == v.EXIT_FAIL
    assert last == "VERIFY-DOROSSI-CLI: FAIL (0 checks) unexpected OSError", last
