"""Antigravity CLI (agy) adapter for Dorossi's Gemini provider."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash-medium"


def parse_agy_usage_report(report: str) -> str:
    """Render only validated quota fields from `agy -p /usage`."""
    quota_labels = {
        "Gemini Models": "Gemini",
        "Claude and GPT models": "Claude／GPT",
    }
    quotas = {}
    for line in report.splitlines():
        fields = line.split("\t")
        if (len(fields) != 4 or fields[0] not in quota_labels
                or fields[1] != "Weekly Limit Remaining"):
            continue
        percent, reset = fields[2:]
        if not re.fullmatch(r"(?:100|[1-9]?\d)%", percent):
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", reset):
            continue
        try:
            reset_epoch = int(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp())
        except ValueError:
            continue
        quotas[fields[0]] = f"本週剩餘 {percent}；重設時間 <t:{reset_epoch}:F>"
    return ("\n".join(f"{quota_labels[group]}：{quotas[group]}"
                      for group in quota_labels if group in quotas)
            or "查詢未提供此後端額度。")

# Gemini API Standard text rates per million tokens, used only as an
# API-equivalent estimate. Antigravity subscription charges/credits are not
# exposed by its stream. https://ai.google.dev/gemini-api/docs/pricing
def _api_equivalent_rates(model: str, input_tokens: int, today=None):
    today = today or date.today()
    if model in {f"gemini-{version}-flash-{effort}"
                 for version in ("3.8", "3.7", "3.6")
                 for effort in ("high", "medium", "low")}:
        return ((0.75, 3.75, 0.075) if today < date(2027, 1, 1)
                else (1.50, 7.50, 0.15))
    if model in ("gemini-3.1-pro-high", "gemini-3.1-pro-low"):
        return ((2.00, 12.00, 0.20) if input_tokens <= 200_000
                else (4.00, 18.00, 0.40))
    return None


def find_gemini_executable() -> str | None:
    """Find the current Antigravity CLI, never the retired gemini command."""
    override = os.environ.get("ANTIGRAVITY_CLI_PATH")
    if override and Path(override).is_file():
        return override
    found = shutil.which("agy") or shutil.which("agy.exe")
    if found:
        return found
    if os.name == "nt":
        installed = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"
        if installed.is_file():
            return str(installed)
    return None


def gemini_argv(exe: str, prompt: str, *, session_id: str | None = None,
                model: str | None = None, effort: str | None = None,
                full: bool = False,
                extra_dir: str | None = None) -> list[str]:
    """Build one Antigravity headless turn using its documented flags."""
    args = [exe, "-p", prompt, "--output-format", "stream-json"]
    if session_id:
        args += ["--conversation", session_id]
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", "high" if effort in ("xhigh", "max") else effort]
    if full:
        args.append("--dangerously-skip-permissions")
    if extra_dir:
        # The prompt also names the directory, but only this flag puts it in the
        # CLI's workspace; without it the tools cannot reach the directory at all.
        args += ["--add-dir", str(extra_dir)]
    return args


async def _bounded_stderr(task, timeout: float = 5.0) -> str:
    """Fallback when no `drain_stderr` is injected: collect the stderr task as text.

    Bounded because the pipe's EOF belongs to whoever still holds its write end
    (a grandchild shell can); on timeout the task is cancelled, never orphaned.
    """
    try:
        raw = await asyncio.wait_for(asyncio.shield(task), timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return ""
    finally:
        if not task.done():
            task.cancel()
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage_counts(usage: dict) -> dict:
    usage = usage if isinstance(usage, dict) else {}
    cached = _count(usage.get("cache_read_tokens"))
    total_input = _count(usage.get("input_tokens"))
    return {"in": max(0, total_input - cached), "cr": cached,
            "cc": 0, "out": _count(usage.get("output_tokens"))}


def gemini_round_info(usage: dict, model: str | None,
                      previous_usage: dict | None = None,
                      *, resumed: bool = False,
                      step_usage: dict | None = None) -> dict:
    """Account for AGY's cumulative result counters without treating OAuth as API billing."""
    current = _usage_counts(usage)
    if previous_usage is not None:
        previous = _usage_counts(previous_usage)
        counts = {key: max(0, current[key] - previous[key]) for key in current}
    elif resumed and step_usage is not None:
        counts = _usage_counts(step_usage)
    elif resumed:
        # A pre-existing session with no baseline must not count its entire history.
        counts = {key: 0 for key in current}
    else:
        counts = current
    mark = {key: _count(usage.get(key)) for key in
            ("input_tokens", "output_tokens", "cache_read_tokens")}
    info = {**counts, "cost_usd": 0.0, "cost_unknown": True,
            "backend": "gemini", "gemini_usage_mark": mark}
    if model:
        info["model"] = model
        rates = _api_equivalent_rates(model, counts["in"] + counts["cr"])
        if rates:
            in_rate, out_rate, cache_rate = rates
            info["cost_usd"] = (counts["in"] * in_rate
                                + counts["cr"] * cache_rate
                                + counts["out"] * out_rate) / 1_000_000
            info["cost_estimated"] = True
    return info


class GeminiStreamState:
    def __init__(self, session_id: str | None = None):
        self.session_id = session_id
        self.model = None
        self.answer = ""
        self.usage = {}
        self.step_usage = None
        self.tool_active = False
        self.status = None
        self.errors = []

    def feed(self, raw: str, on_text=None) -> None:
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(event, dict):
            return
        kind = event.get("event")
        if kind == "init":
            sid = event.get("conversation_id")
            if isinstance(sid, str) and sid:
                self.session_id = sid
            init = event.get("init") or {}
            if isinstance(init, dict) and isinstance(init.get("model"), str):
                self.model = init["model"]
        elif kind == "step_update":
            step = event.get("step_update") or {}
            if not isinstance(step, dict):
                return
            if step.get("step_type") == "agent_response":
                delta = step.get("text_delta")
                if isinstance(delta, str) and delta:
                    self.answer += delta
                    if on_text:
                        on_text(self.answer)
            elif step.get("step_type") == "tool":
                self.tool_active = step.get("state") == "ACTIVE"
            if step.get("state") == "DONE" and isinstance(step.get("usage"), dict):
                if self.step_usage is None:
                    self.step_usage = {"input_tokens": 0, "output_tokens": 0,
                                       "cache_read_tokens": 0}
                for key in self.step_usage:
                    self.step_usage[key] += _count(step["usage"].get(key))
        elif kind == "result":
            result = event.get("result") or {}
            if not isinstance(result, dict):
                return
            self.status = result.get("status")
            sid = result.get("conversation_id")
            if isinstance(sid, str) and sid:
                self.session_id = sid
            response = result.get("response")
            if isinstance(response, str):
                self.answer = response
                if on_text:
                    on_text(self.answer)
            self.usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            error = result.get("error")
            if isinstance(error, str) and error:
                self.errors.append(error[:400])


async def via_gemini(prompt: str, session_id: str | None, *, on_text=None,
                     workdir: str, model: str | None = None,
                     effort: str | None = None, full: bool = False,
                     extra_dir: str | None = None, previous_usage=None,
                     on_proc=None, abort_check=None, silence_limit=None,
                     loop_system_guidance=None, system_prompt: str = "",
                     hard_limit: float = 3600.0, reap=None, drain=None,
                     drain_stderr=None,
                     readline_watched=None, clock_factory=None,
                     record=None, usage_error=None, transient_error=None,
                     resume_error=None, silence_error=None) -> tuple:
    """Run one AGY print-mode turn and return (response, conversation ID, info)."""
    exe = find_gemini_executable()
    if exe is None:
        raise FileNotFoundError("Antigravity CLI (agy) not found")
    if not full:
        # AGY auto-allows workspace file reads/writes in headless mode. Its
        # default permission mode therefore cannot implement Dorossi's strict
        # tool-free setting; fail before a subprocess can touch the workspace.
        raise RuntimeError("Antigravity CLI requires Dorossi full tool mode")
    model = model or DEFAULT_GEMINI_MODEL
    wire = prompt
    if prompt.strip().startswith("/compact"):
        # AGY has no documented headless /compact command. Ask it to summarize
        # the current conversation; the caller may resume with this context.
        wire = "Summarize the current conversation and unfinished work concisely. " + prompt[8:]
    elif loop_system_guidance:
        wire += "\n\n" + loop_system_guidance
    if extra_dir:
        wire = ("The owner also authorized work in this directory: "
                + str(extra_dir) + "\n\n" + wire)
    if not session_id:
        wire = system_prompt + "\n\nUser message:\n" + wire
    args = gemini_argv(exe, wire, session_id=session_id, model=model,
                       effort=effort, full=full,
                       extra_dir=extra_dir)
    args += ["--print-timeout", f"{max(1, int(hard_limit))}s"]
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=workdir, limit=16 * 1024 * 1024)
    if on_proc:
        try:
            on_proc(proc)
        except Exception:
            pass
    try:
        aborted = bool(abort_check and abort_check())
    except Exception:
        aborted = False
    if aborted and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass   # exited between the returncode check and the kill
    stderr_task = asyncio.create_task(drain(proc.stderr))
    state = GeminiStreamState(session_id)
    clock = clock_factory() if clock_factory else None
    now = clock.now if clock else asyncio.get_running_loop().time
    deadline = now() + hard_limit
    try:
        while True:
            left = deadline - now()
            timeout = (min(left, silence_limit) if silence_limit and not state.tool_active
                       else left)
            if timeout <= 0:
                raise asyncio.TimeoutError
            raw = (await readline_watched(proc.stdout, timeout, clock)
                   if readline_watched else
                   await asyncio.wait_for(proc.stdout.readline(), timeout))
            if not raw:
                break
            state.feed(raw.decode("utf-8", errors="replace"), on_text)
        rc = await asyncio.wait_for(proc.wait(), max(1, deadline - now()))
    except asyncio.TimeoutError:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass   # exited in the same instant the timeout fired
        if silence_limit is not None:
            raise silence_error("Antigravity output silence")
        raise TimeoutError("Antigravity CLI timed out")
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        if reap:
            await reap(proc)
        stderr = (await drain_stderr(stderr_task) if drain_stderr
                  else await _bounded_stderr(stderr_task))
    info = gemini_round_info(state.usage, state.model or model, previous_usage,
                             resumed=bool(session_id), step_usage=state.step_usage)
    if record and state.usage:
        if rc != 0 or state.status != "SUCCESS":
            info["fail"] = "AntigravityCliError"
        record(info)
    if rc != 0 or state.status != "SUCCESS":
        detail = "\n".join(state.errors) + "\n" + stderr
        lower = detail.lower()
        if session_id and any(x in lower for x in ("conversation not found", "invalid conversation")):
            raise resume_error("Antigravity conversation unavailable")
        if any(x in lower for x in ("quota", "rate limit", "resource_exhausted")):
            raise usage_error(detail[:400], session_id=state.session_id)
        if any(x in lower for x in ("unavailable", "overloaded", "internal error")):
            raise transient_error(detail[:400], session_id=state.session_id)
        print(f"[dorossi] agy failed rc={rc}: {detail[:400]}", file=sys.stderr)
        raise RuntimeError("Antigravity CLI failed")
    if prompt.strip().startswith("/compact") and session_id:
        summary = state.answer.strip()
        if summary:
            seed = ("This is a compacted summary of the previous conversation. "
                    "Keep it as context for later turns and reply only OK.\n\n" + summary)
            _, new_id, seed_info = await via_gemini(
                seed, None, on_text=None, workdir=workdir, model=model,
                effort=effort,
                full=full, extra_dir=extra_dir, previous_usage=None,
                on_proc=on_proc, abort_check=abort_check,
                silence_limit=silence_limit, system_prompt=system_prompt,
                hard_limit=hard_limit, reap=reap, drain=drain,
                drain_stderr=drain_stderr, record=record,
                readline_watched=readline_watched, clock_factory=clock_factory,
                usage_error=usage_error, transient_error=transient_error,
                resume_error=resume_error, silence_error=silence_error)
            for key in ("in", "cr", "cc", "out"):
                seed_info[key] += info[key]
            return "", new_id, seed_info
    return state.answer, state.session_id, info
