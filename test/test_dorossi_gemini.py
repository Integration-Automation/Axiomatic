"""Antigravity CLI protocol and accounting contract for Dorossi."""
import sys
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "axiomatic"))
from _dorossi_gemini import (  # noqa: E402
    GeminiStreamState, gemini_argv, gemini_round_info, _api_equivalent_rates,
    parse_agy_usage_report, stdin_message,
)
from dorossi_backend import (  # noqa: E402
    dorossi_resolve_model, dorossi_session_backend, _dorossi_loop_resume_plan,
)


def test_agy_stream_response_and_cumulative_usage():
    seen = []
    state = GeminiStreamState()
    for line in (
        '{"event":"init","conversation_id":"sid-1","init":{"model":"gemini-3.8-flash-medium"}}',
        '{"event":"step_update","step_update":{"step_type":"tool","tool_info":{"output":"private"}}}',
        '{"event":"step_update","step_update":{"step_type":"agent_response","state":"ACTIVE","text_delta":"hello"}}',
        '{"event":"step_update","step_update":{"step_type":"agent_response","state":"DONE","text_delta":" world","usage":{"input_tokens":120,"cache_read_tokens":20,"output_tokens":30}}}',
        '{"event":"result","result":{"conversation_id":"sid-1","status":"SUCCESS","response":"hello world","usage":{"input_tokens":120,"cache_read_tokens":20,"output_tokens":30}}}',
    ):
        state.feed(line, seen.append)
    assert state.session_id == "sid-1"
    assert state.answer == "hello world"
    assert seen[-1] == "hello world"
    info = gemini_round_info(state.usage, state.model)
    assert (info["in"], info["cr"], info["out"]) == (100, 20, 30)
    assert info["cost_unknown"] is True
    assert info["cost_estimated"] is True
    assert info["cost_usd"] > 0
    assert _api_equivalent_rates("gemini-3.8-flash-medium", 100, date(2026, 9, 28)) == \
        (0.75, 3.75, 0.075)
    assert _api_equivalent_rates("gemini-3.8-flash-medium", 100, date(2027, 1, 1)) == \
        (1.50, 7.50, 0.15)


def test_resume_uses_only_new_tokens_and_never_claims_zero_bill():
    current = {"input_tokens": 200, "cache_read_tokens": 40, "output_tokens": 50}
    previous = {"input_tokens": 120, "cache_read_tokens": 20, "output_tokens": 30}
    info = gemini_round_info(current, "gemini-3.8-flash-medium", previous,
                             resumed=True)
    assert (info["in"], info["cr"], info["out"]) == (60, 20, 20)
    assert info["gemini_usage_mark"] == current
    assert info["cost_unknown"] is True
    assert info["cost_usd"] > 0


def test_agy_argv_uses_current_headless_flags():
    argv = gemini_argv("agy.exe", session_id="sid-1",
                       model="gemini-3.1-pro-high", effort="max", full=True)
    assert argv == ["agy.exe", "-p", "", "--input-format", "stream-json",
                    "--output-format", "stream-json",
                    "--conversation", "sid-1", "--model", "gemini-3.1-pro-high",
                    "--effort", "high",
                    "--dangerously-skip-permissions"]


def test_the_stdin_message_has_the_shape_the_cli_accepts():
    """量出來的形狀（2026-10-01）：event 必須是 `user`、message 必須是 role/content 的對話訊息。"""
    line = stdin_message("你好 \"quoted\"")
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert json.loads(line) == {"event": "user",
                                "message": {"role": "user", "content": "你好 \"quoted\""}}


def test_gemini_session_selection_model_and_loop_resume():
    sess = {"ai_provider": "gemini", "gemini_session_id": "sid-1",
            "loop_pending": {"task": "continue work"}}
    assert dorossi_session_backend(sess) == "gemini"
    assert dorossi_resolve_model("gemini", "gemini-3.8-flash-medium") == \
        "gemini-3.8-flash-medium"
    assert _dorossi_loop_resume_plan(sess) == ("continue", None)


def test_agy_quota_report_accepts_only_validated_model_rows():
    report = ("Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-10-04T17:04:47Z\n"
              "Gemini Models\tWeekly Limit Remaining\t29%\t2026-10-04T08:49:21Z\n")
    assert parse_agy_usage_report(report) == \
        ("Gemini：本週剩餘 29%；重設時間 <t:1791103761:F>\n"
         "Claude／GPT：本週剩餘 100%；重設時間 <t:1791133487:F>")
    assert parse_agy_usage_report(report.replace("29%", "<script>")) == \
        "Claude／GPT：本週剩餘 100%；重設時間 <t:1791133487:F>"
    assert parse_agy_usage_report(report.replace("2026-10-04T08:49:21Z", "2026-99-04T08:49:21Z")) == \
        "Claude／GPT：本週剩餘 100%；重設時間 <t:1791133487:F>"
    assert parse_agy_usage_report("invalid") == "查詢未提供此後端額度。"


# ---- the process runner (`via_gemini`) ---------------------------------------
#
# Everything above tests pure pieces. The runner is where the lifecycle lives —
# refusing to spawn outside full tool mode, killing a child that may already be
# gone, classifying the CLI's failures — and none of it ran in any test until
# 2026-10-01. The fake process below replaces only `create_subprocess_exec`.

import asyncio  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

import _dorossi_gemini as gm  # noqa: E402


class _UsageLimit(Exception):
    def __init__(self, text, session_id=None):
        super().__init__(text)
        self.session_id = session_id


class _Transient(_UsageLimit):
    pass


class _ResumeGone(Exception):
    pass


class _FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _FakeProc:
    """A child whose stdout is a fixed list of lines; `kill` says it already exited."""

    def __init__(self, lines, *, rc=0, stderr=b"", hang=False):
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(line.encode("utf-8") + b"\n")
        if not hang:
            self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self._rc = rc
        self.returncode = None
        self.kills = 0

    async def wait(self):
        self.returncode = self._rc
        return self._rc

    def kill(self):
        self.kills += 1
        raise ProcessLookupError   # the asyncio transport's answer once it is reaped


def _run(monkeypatch, make_proc, **kwargs):
    """`make_proc` builds the fake inside the loop (a `StreamReader` needs one)."""
    spawned = []

    async def fake_spawn(*args, **_kw):
        spawned.append(list(args))
        spawned.append(make_proc())
        return spawned[-1]

    monkeypatch.setattr(gm, "find_gemini_executable", lambda: "agy.exe")
    monkeypatch.setattr(gm.asyncio, "create_subprocess_exec", fake_spawn)
    recorded = []
    options = dict(workdir=".", full=True, drain=lambda stream: stream.read(),
                   record=recorded.append, usage_error=_UsageLimit,
                   transient_error=_Transient, resume_error=_ResumeGone,
                   silence_error=TimeoutError)
    options.update(kwargs)
    prompt = options.pop("prompt", "hi")
    session_id = options.pop("session_id", None)
    result = asyncio.run(gm.via_gemini(prompt, session_id, **options))
    return result, spawned, recorded


_OK = [
    '{"event":"init","conversation_id":"sid-9","init":{"model":"gemini-3.8-flash-medium"}}',
    '{"event":"result","result":{"conversation_id":"sid-9","status":"SUCCESS",'
    '"response":"done","usage":{"input_tokens":10,"output_tokens":2}}}',
]


def test_a_successful_turn_returns_the_answer_and_records_usage_once(monkeypatch):
    (answer, sid, info), spawned, recorded = _run(
        monkeypatch, lambda: _FakeProc(_OK), extra_dir="D:/elsewhere")
    assert (answer, sid) == ("done", "sid-9")
    assert info["backend"] == "gemini" and info["cost_unknown"] is True
    assert len(recorded) == 1
    argv = spawned[0]
    # the extra directory must reach the CLI's workspace, not only the prompt text
    assert argv[argv.index("--add-dir") + 1] == "D:/elsewhere"


def test_outside_full_tool_mode_nothing_is_spawned(monkeypatch):
    with pytest.raises(RuntimeError):
        _run(monkeypatch, lambda: _FakeProc(_OK), full=False)


@pytest.mark.parametrize("error,expected", [
    ("RESOURCE_EXHAUSTED: quota", _UsageLimit),
    ("model overloaded", _Transient),
])
def test_a_failed_turn_is_classified(monkeypatch, error, expected):
    lines = ['{"event":"result","result":{"status":"ERROR","error":"%s"}}' % error]
    with pytest.raises(expected):
        _run(monkeypatch, lambda: _FakeProc(lines, rc=1))


def test_a_vanished_conversation_is_a_resume_error_only_when_resuming(monkeypatch):
    lines = ['{"event":"result","result":{"status":"ERROR","error":"conversation not found"}}']
    with pytest.raises(_ResumeGone):
        _run(monkeypatch, lambda: _FakeProc(lines, rc=1), session_id="old")
    with pytest.raises(RuntimeError):
        _run(monkeypatch, lambda: _FakeProc(lines, rc=1))


def test_an_unclassified_failure_logs_stderr_as_text(monkeypatch, capsys):
    """stderr used to be rendered with `str(bytes)`, so the log line read `b'\xe9…'`."""
    lines = ['{"event":"result","result":{"status":"ERROR"}}']
    with pytest.raises(RuntimeError):
        _run(monkeypatch, lambda: _FakeProc(lines, rc=1,
                                            stderr="工作目錄不存在".encode("utf-8")))
    assert "工作目錄不存在" in capsys.readouterr().err


def test_re_marking_a_loop_keeps_the_third_backend_as_last_backend():
    """`last_backend` is how the loop notices a mid-task switch; dropping one backend's
    name on re-mark hides the switch away from it."""
    from dorossi_backend import _dorossi_mark_loop_pending
    sess = {"loop_pending": {"task": "t", "last_backend": "gemini"}}
    _dorossi_mark_loop_pending(sess, "")
    assert sess["loop_pending"]["last_backend"] == "gemini"
    sess = {"loop_pending": {"task": "t", "last_backend": "nonsense"}}
    _dorossi_mark_loop_pending(sess, "")
    assert "last_backend" not in sess["loop_pending"]


def test_a_timeout_survives_a_child_that_already_exited(monkeypatch):
    made = []

    def make():
        made.append(_FakeProc([], hang=True))
        return made[-1]

    with pytest.raises(TimeoutError):
        _run(monkeypatch, make, hard_limit=0.05)
    assert made and made[0].kills >= 1


def test_a_long_prompt_goes_over_stdin_not_the_command_line(monkeypatch):
    """命令列上限 32,767 字元；提示放在 stdin，就再也撞不到它。"""
    prompt = "x" * 40000
    (answer, _sid, _info), spawned, _recorded = _run(
        monkeypatch, lambda: _FakeProc(_OK), prompt=prompt)
    argv, proc = spawned[0], spawned[1]
    assert answer == "done"
    assert all(prompt not in arg for arg in argv) and sum(map(len, argv)) < 2000
    assert proc.stdin.closed
    sent = json.loads(proc.stdin.data)
    assert sent["event"] == "user" and prompt in sent["message"]["content"]


# ---------------------------------------------------------------------------
# 2026-10-01 覆蓋率盤點：壓縮、中止、stdin 斷掉、沉默、會丟例外的回呼
# ---------------------------------------------------------------------------

def _answer(sid, text, tokens_in=10, tokens_out=2):
    return [
        f'{{"event":"init","conversation_id":"{sid}","init":{{"model":"gemini-3.8-flash-medium"}}}}',
        f'{{"event":"result","result":{{"conversation_id":"{sid}","status":"SUCCESS",'
        f'"response":"{text}","usage":{{"input_tokens":{tokens_in},"output_tokens":{tokens_out}}}}}}}',
    ]


def _queue(*procs):
    """依序交出事先排好的假子行程（每一次 spawn 一個）。"""
    pending = list(procs)

    def make():
        return pending.pop(0)()
    return make


def test_compact_summarises_then_seeds_a_new_conversation(monkeypatch):
    """沒有文件記載的 headless /compact：先請它摘要，再用摘要開一段新對話，回新的 id，用量兩段相加。"""
    (answer, sid, info), spawned, recorded = _run(
        monkeypatch,
        _queue(lambda: _FakeProc(_answer("sid-old", "the summary", 100, 20)),
               lambda: _FakeProc(_answer("sid-new", "OK", 30, 1))),
        prompt="/compact keep the todo list", session_id="sid-old",
        previous_usage={"input_tokens": 60, "output_tokens": 5})
    assert (answer, sid) == ("", "sid-new")
    first, second = json.loads(spawned[1].stdin.data), json.loads(spawned[3].stdin.data)
    assert first["message"]["content"].startswith(
        "Summarize the current conversation and unfinished work concisely.  keep the todo list")
    assert "compacted summary" in second["message"]["content"]
    assert "the summary" in second["message"]["content"]
    assert "--conversation" in spawned[0] and "--conversation" not in spawned[2]
    assert len(recorded) == 2
    # 摘要那一輪按基準只算新增（100-60、20-5），種子那一輪全算（30、1）；回傳的是兩段相加。
    # `recorded[1]` 與回傳的 info 是同一個 dict，所以拿具體數字比，不拿紀錄相加。
    assert (info["in"], info["out"]) == (70, 16)


def test_compact_with_an_empty_summary_keeps_the_old_conversation(monkeypatch):
    (answer, sid, _info), spawned, _recorded = _run(
        monkeypatch, lambda: _FakeProc(_answer("sid-old", "")),
        prompt="/compact", session_id="sid-old")
    assert (answer, sid) == ("", "sid-old") and len(spawned) == 2


def test_compact_without_a_conversation_is_an_ordinary_turn(monkeypatch):
    (answer, sid, _info), spawned, _recorded = _run(
        monkeypatch, lambda: _FakeProc(_answer("sid-1", "nothing to compact")),
        prompt="/compact")
    assert (answer, sid) == ("nothing to compact", "sid-1") and len(spawned) == 2


def test_an_abort_requested_at_spawn_kills_the_child_and_tolerates_it_being_gone(monkeypatch):
    made = []

    def make():
        made.append(_FakeProc([], rc=1))
        return made[-1]

    with pytest.raises(RuntimeError):
        _run(monkeypatch, make, abort_check=lambda: True)
    assert made[0].kills >= 1


@pytest.mark.parametrize("hook", ["on_proc", "abort_check"])
def test_a_raising_callback_does_not_break_the_turn(monkeypatch, hook):
    def boom(*_args):
        raise ValueError("callback bug")

    (answer, _sid, _info), _spawned, _recorded = _run(
        monkeypatch, lambda: _FakeProc(_OK), **{hook: boom})
    assert answer == "done"


def test_a_child_that_closed_stdin_is_reported_and_classified(monkeypatch, capsys):
    class _BrokenStdin(_FakeStdin):
        def write(self, data):
            raise BrokenPipeError("pipe closed")

    def make():
        proc = _FakeProc(['{"event":"error","error":{"message":"startup crash"}}'], rc=1)
        proc.stdin = _BrokenStdin()
        return proc

    with pytest.raises(RuntimeError):
        _run(monkeypatch, make)
    assert "agy stdin failed" in capsys.readouterr().err


def test_silence_raises_the_callers_silence_error(monkeypatch):
    class _Silence(Exception):
        pass

    made = []

    def make():
        made.append(_FakeProc([], hang=True))
        return made[-1]

    with pytest.raises(_Silence):
        _run(monkeypatch, make, silence_limit=0.05, silence_error=_Silence)
    assert made[0].kills >= 1
