"""Antigravity CLI protocol and accounting contract for Dorossi."""
import sys
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "axiomatic"))
from _dorossi_gemini import (  # noqa: E402
    GeminiStreamState, gemini_argv, gemini_round_info, _api_equivalent_rates,
    parse_agy_usage_report,
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
    argv = gemini_argv("agy.exe", "hello", session_id="sid-1",
                       model="gemini-3.1-pro-high", effort="max", full=True)
    assert argv == ["agy.exe", "-p", "hello", "--output-format", "stream-json",
                    "--conversation", "sid-1", "--model", "gemini-3.1-pro-high",
                    "--effort", "high",
                    "--dangerously-skip-permissions"]


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


class _FakeProc:
    """A child whose stdout is a fixed list of lines; `kill` says it already exited."""

    def __init__(self, lines, *, rc=0, stderr=b"", hang=False):
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
